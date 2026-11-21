from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from datetime import timedelta

from . import codebase, config, gh, oncall, paths, pilot, runlimits, runner, sandbox, scratch, tracker, util, workspace, zte
from .store import orchestration
from .store import runs as run_store

LIMIT = 3
WINDOW = timedelta(hours=24)
REASONS = {
    "scratch": ("retained verification scratch", "the verification scratch settled"),
    "worktree": ("a held worktree", "the worktree is free"),
    "worktree-removal": ("a worktree that could not be removed", "the worktree is gone"),
    "engine-crash": ("an Arete engine crash", "the Arete engine stamp changed"),
    "runner-start": ("a runner start failure", "the runner answered its startup check"),
    "runner-login": ("a runner login failure", "the runner login is available again"),
    "sandbox-start": ("a sandbox start failure", "the local sandbox started successfully"),
}
DETAILS = {
    "scratch": {"workspace": str, "selectors": list},
    "worktree": {"workspace": str, "branch": str},
    "worktree-removal": {"workspace": str},
    "runner-start": {"runner": str},
    "runner-login": {"role": str, "routes": list},
}


def recorded_failure(run: dict) -> dict:
    try:
        failure = json.loads(run.get("failure") or "null")
        if not isinstance(failure, dict) or failure.get("class") != "environment":
            return {}
        reason = failure.get("reason")
        if reason in REASONS and all(isinstance(failure.get(key), kind) for key, kind in DETAILS.get(reason, {}).items()):
            return failure
    except (TypeError, ValueError):
        pass
    return {}


def workspace_ready(repo: config.Repo, layer: config.RepoLayer, issue: str, failure: dict) -> bool:
    ws = paths.WORKTREES_DIR / f"{repo.name}-{issue}"
    if failure.get("workspace") != str(ws):
        return False
    checkouts = [workspace.Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir, "")
                 for p in workspace.parts_of(repo)]
    if workspace.scratch_left(ws, checkouts, strict=True):
        return False
    reason = failure["reason"]
    if reason == "scratch":
        return not failure.get("unidentifiable_record") and all(
            scratch.settled_record(selector, ws) for selector in failure["selectors"])
    try:
        ws.lstat()
    except FileNotFoundError:
        return True
    if reason == "worktree-removal":
        return False
    workspace.kept_onto(ws, checkouts, failure["branch"])
    return not workspace.kept_work(ws, checkouts, repo.path, layer.copy)


def cleared(cfg: config.Config, repo: config.Repo, layer: config.RepoLayer, ticket: tracker.Ticket,
            run: dict, failure: dict) -> bool:
    try:
        match failure["reason"]:
            case "scratch" | "worktree" | "worktree-removal":
                return workspace_ready(repo, layer, ticket.id, failure)
            case "engine-crash":
                return bool(run.get("engine_sha") and codebase.STAMP and run["engine_sha"] != codebase.STAMP)
            case "runner-start":
                return runner.started_now(failure["runner"])
            case "runner-login":
                return runner.login_restored(cfg, repo, layer, ticket.kind, ticket.id, failure)
            case "sandbox-start":
                if sandbox.can_confine(False):
                    command = sandbox.write_confinement(repo.path, []) + ["/usr/bin/true"]
                    return util.run_whole_tree(command, 10, None, cwd=repo.path).returncode == 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError, workspace.RunFailed):
        pass
    return False


def comment(source: tracker.GitHubTracker, issue: str, run_id: str, event: str, body: str) -> None:
    marker = f"<!-- arete:environment-{event}={run_id} -->"
    source.comment_once(issue, f"{tracker.ARETE_TAG} {body}", marker)


def announce(source: tracker.GitHubTracker, run: dict, evidence: str) -> None:
    comment(source, run["issue"], run["id"], "requeue", f"Requeued this issue because {evidence}.")
    run_store.note_requeue_comment(run)


def reconcile(source: tracker.GitHubTracker, entry: dict) -> None:
    issue, run_id = entry["issue"], entry["run_id"]
    with util.lock(run_store.run_lock("pipeline", source.repo.name, issue)) as free:
        if not free:
            return
        run = run_store.run_get(run_id)
        if not run or not (failure := recorded_failure(run)):
            return
        if pending := entry["edit_pending"]:
            newest = run_store.newest_live_whole_run(source.repo.name, issue)
            if not newest or newest["id"] != run_id:
                run_store.settle_requeue_edit(run_id)
                return
            ticket = gh.issue(source.repo.github, int(issue))
            if source.label_name("failed") in {label["name"] for label in ticket["labels"]}:
                return
            run_store.settle_requeue_edit(run_id, pending)
        announce(source, run, REASONS[failure["reason"]][1])


def remove_failed_label(source: tracker.GitHubTracker, run: dict) -> None:
    issue, run_id = run["issue"], run["id"]
    run_store.start_requeue_edit(run, util.iso())
    try:
        gh.issue_labels(source.repo.github, int(issue), remove=[source.label_name("failed")], single_attempt=True)
    except gh.GhUnsure:
        raise
    except gh.GhError:
        run_store.settle_requeue_edit(run_id)
        raise
    run_store.settle_requeue_edit(run_id, util.iso())


def reconsider(cfg: config.Config, source: tracker.GitHubTracker, layer: config.RepoLayer,
               issue: str, dry_run: bool) -> tracker.Ticket | None:
    from . import tasks

    repo = source.repo
    if issue in oncall.parked(repo.name) or pilot.is_pilot_task(issue) or zte.reserved(issue):
        return None
    lock = run_store.run_lock("pipeline", repo.name, issue)
    with (nullcontext(not util.lock_is_held(lock)) if dry_run else util.lock(lock)) as free:
        if (not free or tasks.pending_start(repo.name, issue) or run_store.issue_running(repo.name, issue)
                or run_store.pending_label(repo.name, issue) or run_store.failed_finish_pending(repo.name, issue)):
            return None
        run = run_store.newest_live_whole_run(repo.name, issue)
        if (not run or run["status"] != "failed" or run.get("pr_url") or not (failure := recorded_failure(run))
                or orchestration.publication_pending(run["id"])):
            return None
        ticket = source.get(issue)
        if not ticket.open or ticket.blocker or not source.queued_failure(ticket.labels):
            return None
        if runlimits.admission(repo, issue, source, runlimits.Policy.of(layer), False)[0]:
            return None
        now = util.now()
        entry, count = run_store.environment_requeue(run, util.iso(now - WINDOW))
        if entry.get("requeued"):
            return None
        reason, evidence = REASONS[failure["reason"]]
        name = tracker.ticket_name(repo.name, ticket.ref)
        if count >= LIMIT:
            if dry_run:
                print(f"{util.iso(now)} {name}: would keep failed after {LIMIT} environment requeues in 24 hours "
                      f"({reason})")
            elif not entry.get("cap_notified"):
                comment(source, issue, run["id"], "cap",
                        f"Leaving this issue failed after {LIMIT} automatic requeues in 24 hours. "
                        f"The latest failure was {reason}.")
                run_store.note_requeue_comment(run, capped=True)
            return None
        if not cleared(cfg, repo, layer, ticket, run, failure):
            return None
        if dry_run:
            print(f"{util.iso(now)} {name}: would requeue because {evidence}")
            return ticket
        current = source.get(issue)
        if not current.open or current.blocker or not source.queued_failure(current.labels):
            return None
        remove_failed_label(source, run)
        print(f"{util.iso()} {name}: requeued because {evidence}")
        announce(source, run, evidence)
        return None
