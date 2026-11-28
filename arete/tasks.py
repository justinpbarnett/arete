from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import timedelta

from . import (
    chat,
    config,
    control,
    events,
    factory,
    flows,
    gh,
    jira,
    launch_lease,
    owner_asks,
    paths,
    pipeline,
    principals,
    record,
    refusals,
    repairs,
    run_lines,
    runlimits,
    runner,
    secret_move,
    secretstore,
    slots,
    tracker,
    util,
    vault,
    workspace,
)
from .refusals import TrackerFailed
from .store import asks as ask_store
from .store import blobs as blob_store
from .store import db as foundation
from .store import idempotency as claims
from .store import migrations
from .store import runs as run_store
from .store import sources as source_store
from .store import tasks as task_store
from .store import workspaces, writes

TITLE_CHARS = 80
START_LOCK = "task-start"
STEER_LOCK = owner_asks.STEER_LOCK
POLLER_LATER = "the poller starts it when a slot frees"
SLOT_QUEUE_FAILED = "slot-queue-failed"
NO_SLOT_AT_START = "no run slot was free when the child tried to start"
RESUMABLE = ("interrupted", "stopped", "failed")
MAX_ANSWER_CHARS = 20000
TICKET_CLOSED = "its ticket closed while it waited for an answer"
PENDING = {}
PENDING_LOCK = threading.RLock()
PENDING_SWEEP_SECONDS = 1
PENDING_REAPER = None


class AlreadyResolved(control.Refused):
    status = 409
    code = "already_resolved"


def title_of(prompt: str) -> str:
    line = next((x for x in prompt.splitlines() if x.strip() and not tracker.WAIT_LINE.match(x)), "")
    return chat.title_of(line, TITLE_CHARS)


def answer(task: str, repo: str, run: str, state: str, why: str) -> dict:
    return {"task": task, "repo": repo, "run": run, "state": state, "why": why}


def repo_named(cfg: config.Config, name: str) -> config.Repo:
    names = ", ".join(r.name for r in cfg.repos) or f"none yet; add one to {paths.short_path(paths.CONFIG_FILE)}"
    if not name:
        raise control.Refused(f"Name the repo to work in; the repos are {names}")
    try:
        return cfg.repo(name)
    except config.UnknownRepo:
        raise control.Refused(f"There is no repo '{name}'; the repos are {names}") from None


def start(repo_name: str, prompt: str = "", title: str = "", kind: str = "", ticket: str = "",
          force: bool = True, claim=None) -> dict:
    from .store import factory as factory_state

    factory_state.admit()
    prompt, ticket = prompt.strip(), ticket.strip()
    if not prompt and not ticket:
        raise control.Refused("Give a prompt to add a task and start it, or a ticket to start one that exists.")
    if prompt and ticket:
        raise control.Refused("Give a prompt or a ticket, not both.")
    cfg, problem = config.load_or_problem()
    if problem:
        raise control.Refused(problem)
    repo = repo_named(cfg, repo_name.strip())
    if refusal := pipeline.run_refusal(repo)[1]:
        raise control.Refused(refusal)
    if kind not in ("", *config.KINDS):
        raise control.Refused(f"The kind is {' or '.join(config.KINDS)}, not '{kind}'.")
    if ticket:
        return start_ticket(cfg, repo, ticket, force, claim=claim)
    tid = task_store.task_add(repo.name, prompt, title.strip() or title_of(prompt), kind or "feat", claim=claim)
    return begin_prompt(cfg, repo, tid)


def begin_prompt(cfg, repo, tid):
    if waits := tracker.LocalTracker(repo).blocker(tid):
        return answer(tid, repo.name, "", "queued", f"{waits}; " + ("the poller starts it once the wait ends"
                                                                    if repo.pipeline else "send its ticket to start it"))
    return begin(cfg, repo, tid, force=False, poller_picks=repo.pipeline)


def start_ticket(cfg: config.Config, repo: config.Repo, ticket: str, force: bool = True, claim=None) -> dict:
    from .store import factory as factory_state

    factory_state.admit()
    try:
        tid = tracker.for_ticket(cfg, repo, ticket).normalize(ticket)
    except ValueError as e:
        raise control.Refused(f"{repo.name}: {e}") from None
    if tracker.is_task_id(tid):
        if (row := task_store.task_get(tid)) is None:
            raise control.NotFound(f"no task {tid}")
        if row["repo"] != repo.name:
            raise control.Refused(f"{tid} is a task of {row['repo']}, not {repo.name}")
        if row["state"] in config.TASK_FINISHED:
            raise control.Refused(f"task {tid} is {row['state']}, so it does not start again; add a new task")
    if claim:
        with writes.atomic():
            seq = events.emit("task.start_requested", f"task:{tid}", cls="derived", task=tid,
                              data={"repo": repo.name, "ticket": tid})
            claims.claim(claim, seq)
    return begin(cfg, repo, tid, force=force, poller_picks=False, owner_forcing=force)


def recover_start(claim, force=True):
    name, tid, prompt = claims.target(claim)
    cfg = config.load()
    repo = repo_named(cfg, name)
    if rows := run_store.ticket_runs(name, tid, limit=1):
        row = rows[0]
        return answer(tid, name, row["id"], row["status"], "recovered task start")
    return begin_prompt(cfg, repo, tid) if prompt else begin(cfg, repo, tid, force, False)


def live_run(repo: str, tid: str) -> dict | None:
    row = run_store.running_run(repo, tid)
    if not row:
        return None
    if row["status"] == "queued":
        return row
    return row if slots.holding([row], "pipeline") else None


def run_ids(repo: str, tid: str) -> list[str]:
    return [r["id"] for r in run_store.ticket_runs(repo, tid)]


def held(cfg: config.Config, repo: config.Repo) -> str:
    if why := config.repo_paused(cfg, repo.name):
        return why
    going = Counter(r["repo"] for r in pipeline.live_pipeline_runs())
    return pipeline.held_back(cfg, repo.name, going, slots.slots_free_to_start(cfg.max_parallel))


def queued(repo: config.Repo, tid: str, why: str, poller_picks: bool) -> dict:
    source_store.pass_over(repo.name, tid)
    later = POLLER_LATER if poller_picks else (
        f'start task {tid} with {{"repo": "{repo.name}", "ticket": "{tid}"}} when a slot frees')
    return answer(tid, repo.name, "", "queued", f"{why}; {later}")


def spawn_queued(repo_name: str, tid: str, spawn: Callable[[], subprocess.Popen]) -> subprocess.Popen:
    from .store import factory as factory_state

    factory_state.admit()
    try:
        foundation.require_workspace_contract()
    except foundation.IncompatibleSchema as e:
        raise control.Refused(str(e)) from e
    with PENDING_LOCK, launch_lease.acquiring(repo_name, tid):
        return spawn()


def begin(cfg: config.Config, repo: config.Repo, tid: str, force: bool, poller_picks: bool, *, owner_forcing=False) -> dict:
    from .store import factory as factory_state

    factory_state.admit()
    with PENDING_LOCK, util.lock(START_LOCK, wait=True):
        if run := live_run(repo.name, tid):
            return answer(tid, repo.name, run["id"], "running", "already running")
        if why := held(cfg, repo):
            return queued(repo, tid, why, poller_picks)
        if why := runlimits.admission(repo, tid, tracker.for_ticket(cfg, repo, tid),
                                      runlimits.Policy.of(config.load_layer(repo.layer_dir)), force).reason:
            raise control.Refused(why)
        try:
            foundation.require_workspace_contract()
        except foundation.IncompatibleSchema as e:
            raise control.Refused(str(e)) from e
        from contextlib import nullcontext

        with foundation.tx() as c:
            if task_store.has_identity_expand(c):
                task_num, issue_key = task_store.TaskStore.task_for_ticket(
                    c, repo, tid, principals.current(), cfg=cfg,
                )
                existing = run_store.running_run(task_num, connect=lambda: nullcontext(c))
                if existing and (existing["status"] == "queued" or slots.holding([existing], "pipeline")):
                    return answer(tid, repo.name, existing["id"], "running", "already running")
                run_id = pipeline.queue_run(
                    repo.name, tid, cfg=cfg, repo=repo, task_id=task_num, issue_key=issue_key,
                )
            else:
                run_id = pipeline.queue_run(repo.name, tid, cfg=cfg, repo=repo)
        from . import inbox

        if missing := inbox.check_run_missing_secrets(run_id, repo=repo.name):
            cmd = pipeline.run_command(repo.name, tid, force, run_id=run_id)
            record.write_launch(run_id, cmd, pipeline.RUNS_LOG)
            names = ", ".join(f"{s['scope']}/{s['name']}" for s in missing)
            return answer(tid, repo.name, run_id, "queued", f"waiting on missing secret {names}")
        spawn_queued(repo.name, tid, lambda: pipeline.spawn_run(repo.name, tid, force, run_id=run_id))
        result = answer(tid, repo.name, run_id, "running", "started")
        if owner_forcing:
            from . import cohorts

            cohorts.owner_intervention(result, "owner-forced start")
        return result


def pending_start(repo_name, tid):
    run, state = pending_state(repo_name, tid)
    if state in ("running", "starting"):
        return answer(tid, repo_name, run, state, "started" if run else "child is starting")
    return None


def pending_state(repo_name: str, tid: str) -> tuple[str, str]:
    with PENDING_LOCK:
        identity = repo_name, tid
        if identity in PENDING:
            proc, before = PENDING[identity]
            exit_code = proc.poll()
            row = next((r for r in run_store.ticket_runs(repo_name, tid) if r["id"] not in before), None)
            if row or exit_code is not None:
                del PENDING[identity]
                if not row and exit_code == pipeline.SLOT_QUEUE_FAILED_EXIT:
                    return "", SLOT_QUEUE_FAILED
                return (row["id"], row["status"]) if row else ("", "exited")
            return "", "starting"
        return "", "starting" if launch_lease.held(repo_name, tid) else "exited"


def prune_pending() -> None:
    with PENDING_LOCK:
        for (repo, tid), (proc, before) in list(PENDING.items()):
            if proc.poll() is not None:
                del PENDING[repo, tid]
                continue
            try:
                if any(r["id"] not in before for r in run_store.ticket_runs(repo, tid)):
                    del PENDING[repo, tid]
            except (OSError, sqlite3.Error, foundation.NoDatabase, foundation.IncompatibleSchema):
                # A temporarily unavailable database is not proof of exit.
                continue


def reap_pending() -> None:
    global PENDING_REAPER

    while True:
        time.sleep(PENDING_SWEEP_SECONDS)
        prune_pending()
        with PENDING_LOCK:
            if not PENDING:
                PENDING_REAPER = None
                return


def spawn_pending(repo_name: str, tid: str, spawn: Callable[[], subprocess.Popen]) -> subprocess.Popen:
    from .store import factory as factory_state

    factory_state.admit()
    global PENDING_REAPER

    try:
        foundation.require_workspace_contract()
    except foundation.IncompatibleSchema as e:
        raise control.Refused(str(e)) from e
    with PENDING_LOCK, launch_lease.acquiring(repo_name, tid) as lease:
        before = set(run_ids(repo_name, tid))
        proc = spawn()
        if not lease.passed:
            raise RuntimeError("task launch must inherit its lease through control.launch")
        PENDING[repo_name, tid] = proc, before
        if PENDING_REAPER is None:
            PENDING_REAPER = threading.Thread(target=reap_pending, name="pending-starts", daemon=True)
            PENDING_REAPER.start()
        return proc


def free_to_start(cfg: config.Config, repo: config.Repo, tid: str, after: str = "", ahead: bool = False) -> None:
    if run := live_run(repo.name, tid):
        raise control.Busy(f"run {run['id']} of {tracker.issue_name(repo.name, tid)}")
    if why := config.repo_paused(cfg, repo.name):
        raise control.Refused(why + after)
    if not ahead and (why := held(cfg, repo)):
        raise slots.NoFreeSlot(why + after)
    if why := runlimits.admission(repo, tid, tracker.for_ticket(cfg, repo, tid),
                                  runlimits.Policy.of(config.load_layer(repo.layer_dir)), True).reason:
        raise control.Refused(why)


def launch(cfg: config.Config, repo: config.Repo, tid: str, command: list[str], ahead: bool = False,
           admission=None, *, continues: str | None = None, restart: str = "", only_step: str = "") -> dict:
    with PENDING_LOCK, util.lock(START_LOCK, wait=True):
        free_to_start(cfg, repo, tid, ahead=ahead)
        try:
            foundation.require_workspace_contract()
        except foundation.IncompatibleSchema as e:
            raise control.Refused(str(e)) from e
        with writes.atomic():
            if admission and not writes.check_run(*admission, public=True):
                raise control.NotFound(f"no run {admission[0]}")
            run_id = pipeline.queue_run(repo.name, tid, continues=continues, restart=restart, only_step=only_step)
        full_command = [*command, "--run-id", run_id]
        record.write_launch(run_id, full_command, pipeline.RUNS_LOG)
        from . import inbox

        if missing := inbox.check_run_missing_secrets(run_id, repo=repo.name):
            names = ", ".join(f"{s['scope']}/{s['name']}" for s in missing)
            return answer(tid, repo.name, run_id, "queued", f"waiting on missing secret {names}")
        spawn_queued(repo.name, tid, lambda: control.launch(full_command, pipeline.RUNS_LOG))
        return answer(tid, repo.name, run_id, "running", "started")


def launch_recovery(cfg: config.Config, repo: config.Repo, row: dict, attempt: dict) -> dict:
    from . import oncall, poll, routing, unblock

    tid = str(row["issue"])
    with util.lock("oncall") as got:
        if not got:
            return answer(tid, repo.name, "", "queued", "Waiting for the on-call interlock")
        with util.lock(STEER_LOCK, wait=True), PENDING_LOCK, util.lock(START_LOCK, wait=True):
            if pending := pending_start(repo.name, tid):
                return pending
            if live_run(repo.name, tid) or held(cfg, repo):
                return answer(tid, repo.name, "", "queued", "Waiting for an available run slot")
            if tid in oncall.parked(repo.name):
                raise control.Refused("on-call already owns this ticket")
            source = tracker.for_ticket(cfg, repo, tid)
            ticket = source.get(tid)
            if ticket.blocker or not unblock.approval(source, ticket)[0] or not unblock.active(cfg, repo.name, tid):
                raise control.Refused("normal ticket admission no longer passes")
            layer = config.load_layer(repo.layer_dir)
            mode = pipeline.recorded_path(row["id"])
            repairing = attempt["grant"].get("entry") == "repair"
            path = "repair" if repairing else mode
            discovery = routing.Discovery(cfg, repo, priced=False, layer=layer)
            if why := poll.step_without_route(cfg, repo, ticket, discovery, layer, mode=mode, repairing=repairing):
                return answer(tid, repo.name, "", "queued", why)
            if path == "classic":
                command = control.command(["run", repo.name, tid, "--local"])
            else:
                command = pipeline.adw_command(repo.name, tid, "--repair" if repairing else "--continue", row["id"])

            run_id = pipeline.queue_run(repo.name, tid, continues=row["id"], restart="resume")
            full_command = [*command, "--run-id", run_id]
            record.write_launch(run_id, full_command, pipeline.RUNS_LOG)

            def spawn():
                launch_lease.CURRENT.get().recovery = attempt["id"]
                return control.launch(full_command, pipeline.RUNS_LOG)

            spawn_queued(repo.name, tid, spawn)
            return answer(tid, repo.name, run_id, "running", "Recovery child started")


def run_of(run_id: str) -> tuple[config.Config, config.Repo, dict]:
    if not (row := run_store.run_get(run_id)):
        raise control.NotFound(f"no run {run_id}")
    if row["kind"] != "pipeline":
        raise control.Refused(f"run {run_id} is a {row['kind']} run, not the run of a ticket")
    cfg = config.load()
    try:
        return cfg, cfg.repo(row["repo"]), row
    except config.UnknownRepo:
        raise control.Refused(f"run {run_id} is of {row['repo']}, which is not a repo in "
                              f"{paths.short_path(paths.CONFIG_FILE)}") from None


def command_run(run_id, base_version):
    writes.check_run(run_id, base_version, public=True)
    cfg, repo, row = run_of(run_id)
    if base_version is not None and row["version"] != base_version:
        raise writes.VersionConflict(row)
    return cfg, repo, row


def no_later_run(row: dict, *, live: bool = False) -> None:
    later = (
        run_store.later_run(int(row["task_id"]), row["id"], live=live)
        if row.get("task_id") is not None
        else run_store.later_run(row["repo"], row["issue"], row["id"], live=live)
    )
    if later:
        raise control.Refused(f"run {row['id']} is not the ticket's latest run: run {later['id']} "
                              f"({later['status']}) came after it")


def step_named(step: str) -> str:
    if step not in factory.STEPS:
        raise control.Refused(f"there is no step '{step}'; the steps are {', '.join(sorted(factory.STEPS))}")
    return step


def reply(run_id: str, text: str) -> dict:
    if not (text := text.strip()):
        raise control.Refused("Give the answer in text")
    if len(text) > MAX_ANSWER_CHARS:
        raise control.Refused(f"the answer is {len(text)} characters; the most is {MAX_ANSWER_CHARS}")
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = run_of(run_id)
        waiting = ask_store.asks_of(run_id, "open")
        if not waiting and ask_store.asks_of(run_id):
            raise AlreadyResolved(f"run {run_id}'s asks were already resolved, so this answer was not stored")
        if row["status"] != "needs-info":
            from .store import inbox as inbox_store

            has_stop = inbox_store.get_open_by_kind_and_subject("stop", f"run:{run_id}") is not None
            if not has_stop:
                raise control.Refused(f"run {run_id} is {row['status']}, so it has no question to answer")
            tid = str(row["issue"])
            if row["status"] == "running":
                run_dir = paths.RUNS_DIR / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                util.write_bytes_atomic(run_dir / pipeline.ANSWER_FILE, text.encode())
                run_store.add_feed(run_id, "redirect", text)
                if tracker.is_task_id(tid):
                    task_store.task_comment(tid, principals.current(), text)
                return answer(tid, repo.name, run_id, "running", "redirected")
        no_later_run(row)
        tid = str(row["issue"])
        if waiting:
            return reply_to_asks(cfg, repo, row, waiting, text)
        free_to_start(cfg, repo, tid, "; the answer was not stored")
        if tracker.is_task_id(tid):
            with PENDING_LOCK, util.lock(START_LOCK, wait=True):
                with writes.atomic():
                    if not task_store.task_comment(tid, principals.current(), text):
                        raise control.NotFound(f"no task {tid}")
                    new_run_id = pipeline.queue_run(repo.name, tid, continues=row["id"], restart="answer")
                (paths.RUNS_DIR / run_id).mkdir(parents=True, exist_ok=True)
                util.write_bytes_atomic(paths.RUNS_DIR / run_id / pipeline.ANSWER_FILE, text.encode())
                spawn_queued(repo.name, tid, lambda: pipeline.spawn_run(repo.name, tid, True, run_id=new_run_id))
            return answer(tid, repo.name, new_run_id, "running", "started")
        kept = store_answer(cfg, repo, tid, text)
        (paths.RUNS_DIR / run_id).mkdir(parents=True, exist_ok=True)
        util.write_bytes_atomic(paths.RUNS_DIR / run_id / pipeline.ANSWER_FILE, text.encode())
        started = begin(cfg, repo, tid, force=True, poller_picks=repo.pipeline)
        return {**started, "why": f"{started['why']}; {kept}"} if kept else started


def store_answer(cfg: config.Config, repo: config.Repo, tid: str, text: str) -> str:
    if tracker.is_task_id(tid):
        if not task_store.task_comment(tid, principals.current(), text):
            raise control.NotFound(f"no task {tid}")
        return ""
    tk = tracker.for_ticket(cfg, repo, tid)
    if isinstance(tk, tracker.GitHubTracker):
        import json
        import secrets

        from . import outbox

        actor = principals.current()
        obj = f"github:{tk.repo.github}#{int(tid)}"
        nonce = secrets.token_hex(6)
        now = util.iso()
        with foundation.tx() as c:
            outbox.draft(
                c,
                target="github",
                kind="comment",
                object=obj,
                body=text,
                drafted_by=actor,
                approved_by=actor,
                opened_at=now,
                slot=f"answer:{tk.repo.github}#{int(tid)}:{nonce}:comment",
                task_id=f"{repo.name}#{int(tid)}",
                summary=f"post answer on {tk.repo.github}#{int(tid)}",
            )
            outbox.draft(
                c,
                target="github",
                kind="label",
                object=obj,
                body=json.dumps({"add": [], "remove": [tk.label_name("needs-info")], "family": "state"}),
                drafted_by=actor,
                approved_by=actor,
                opened_at=now,
                slot=f"answer:{tk.repo.github}#{int(tid)}:{nonce}:label",
                task_id=f"{repo.name}#{int(tid)}",
                summary=f"remove {tk.label_name('needs-info')} on {tk.repo.github}#{int(tid)}",
            )
        return ""
    if error := tk.comment(tid, text):
        raise TrackerFailed(f"could not post the answer on {tk.ref(tid)}, so it was not stored: {error}")
    return ""


def reply_to_asks(cfg: config.Config, repo: config.Repo, row: dict, waiting: list[dict], text: str) -> dict:
    if any(owner_asks.has_secret(a["questions"]) for a in waiting):
        raise control.Refused(f"run {row['id']} asks a secret question, which a text reply cannot answer; answer it "
                              f"on the dashboard or with `arete answer {row['id']}`")
    tid = str(row["issue"])
    free_to_start(cfg, repo, tid, "; the answer was not stored", ahead=True)
    command = continuing(cfg, repo, row)
    # Admission can resolve an ask. Check again before posting, and leave asks
    # open until persistence succeeds so the poller cannot consume a failed reply.
    with foundation.reader():
        for a in waiting:
            current = ask_store.ask_get(a["id"])
            if not current or current["status"] != "open":
                raise AlreadyResolved(f"ask {a['id']} was already resolved, so this answer was not stored")
    kept = store_answer(cfg, repo, tid, text)
    with PENDING_LOCK, util.lock(START_LOCK, wait=True):
        with writes.atomic():
            for a in waiting:
                answers = owner_asks.text_answers(owner_asks.unstaged(a), text)
                if not ask_store.ask_close(a["id"], "answered", owner_asks.combine_staged(a, answers)):
                    raise AlreadyResolved(f"ask {a['id']} was already resolved; the answer was stored as a comment, "
                                          "but this reply did not update the asks or resume the run")
            new_run_id = pipeline.queue_run(repo.name, tid, continues=row["id"], restart="answer")
        full_command = [*command, "--run-id", new_run_id]
        record.write_launch(new_run_id, full_command, pipeline.RUNS_LOG)
        spawn_queued(repo.name, tid, lambda: control.launch(full_command, pipeline.RUNS_LOG))
    started = answer(tid, repo.name, new_run_id, "running", "started")
    return {**started, "why": f"{started['why']}; {kept}"} if kept else started


def asks_view(*, owner_only=True) -> dict:
    from contextlib import nullcontext
    from .store import inbox as inbox_store

    keys = ("id", "repo", "ticket", "run", "step", "asked", "why", "tried", "questions", "reminded")
    with foundation.one_read() as c:
        if inbox_store.available(c):
            items = inbox_store.list_items(
                state=("open", "snoozed", "shelved"), subject_prefix="ask:", connect=lambda: nullcontext(c)
            )
            seen = set()
            result = []
            for item in items:
                detail = item["detail"]
                ask_id = detail.get("id") or item["subject"].removeprefix("ask:")
                seen.add(ask_id)
                row = {**detail, "reminded": item["reminded"]}
                if row.get("questions"):
                    result.append({k: row.get(k, "") for k in keys})
            if not owner_only:
                for raw in ask_store.open_asks(connect=lambda: nullcontext(c), owner_only=False, group_runners=True):
                    if raw["id"] not in seen and (ask := owner_asks.unstaged(raw))["questions"]:
                        result.append({k: ask[k] for k in keys})
            result.sort(key=lambda r: (r["asked"], r["id"]))
            return {"asks": result}
    rows = ask_store.open_asks(connect=foundation.reader, owner_only=owner_only, group_runners=True)
    return {"asks": [{k: ask[k] for k in keys} for row in rows
                     if (ask := owner_asks.unstaged(row))["questions"]]}


def stage_recovery_answers(group: list[dict], decision: dict, *, scrub) -> None:
    open_asks = {a["id"]: a for a in group if a["status"] == "open"}
    for ask_id, answers in decision["answers"].items():
        if ask_id not in open_asks or not isinstance(answers, dict) or not answers:
            raise ValueError("recovery answered an unknown ask or returned an empty answer")
        ask = open_asks[ask_id]
        staged = dict(ask["staged_answers"])
        for index, entry in answers.items():
            if (not isinstance(index, str) or index not in {str(i) for i in range(len(ask["questions"]))}
                    or index in staged or not isinstance(entry, dict)):
                raise ValueError("recovery answered an unknown or already settled question")
            q = ask["questions"][int(index)]
            if q["kind"] not in ("choice", "text") or set(entry) != {q["kind"]}:
                raise ValueError("recovery may answer only text and choice questions")
            if q["kind"] == "text" and isinstance(entry["text"], str):
                entry = {"text": scrub(entry["text"], owner_asks.MAX_TEXT)}
            answer_row = owner_asks.answers_for({"questions": [q]}, {"answers": [entry]})[0]
            staged[index] = {**answer_row, "by": "rule:unblock", "basis": decision["reason"],
                             "evidence": decision["evidence"]}
        if len(staged) == len(ask["questions"]):
            ok = ask_store.ask_close(ask_id, "answered", [staged[str(i)] for i in range(len(staged))])
        else:
            ok = ask_store.stage(ask_id, staged)
        if not ok:
            raise AlreadyResolved(f"ask {ask_id} changed before recovery settlement")


def answer_ask(ask_id: str, body) -> dict:
    from .store import factory as factory_state

    factory_state.admit()
    with util.lock(STEER_LOCK, wait=True):
        ask = ask_store.ask_get(ask_id)
        if not ask:
            raise control.NotFound(f"no ask {ask_id}")
        if ask["status"] != "open":
            raise AlreadyResolved(f"ask {ask_id} is {ask['status']}, so it takes no answer{answers_held(ask)}")
        cfg, repo, row = run_of(ask["run"])
        if row["status"] != "needs-info":
            raise control.Refused(f"run {row['id']} is {row['status']}, so ask {ask_id} takes no answer")
        no_later_run(row)
        tid = str(row["issue"])
        if run := live_run(repo.name, tid):
            raise control.Busy(f"run {run['id']} of {tracker.issue_name(repo.name, tid)}")
        if isinstance(body, dict) and "stop" in body:
            if body != {"stop": True}:
                raise control.Refused('To stop the run, send {"stop": true} alone')
            return let_go(cfg, repo, row, "stopped")
        remaining = owner_asks.unstaged(ask)
        try:
            answers = owner_asks.combine_staged(ask, owner_asks.answers_for(remaining, body))
        except (ValueError, TypeError) as e:
            raise control.Refused(str(e)) from None
        given = owner_asks.secret_values(remaining, body)
        scope = f"repo:{repo.name}"
        store = config.secret_settings().store if given else None
        for name, value in given.items():
            try:
                if why := runner.reserved(name):
                    raise ValueError(why)
                if store:
                    secretstore.validate_set(scope, name, value, "repo_answer")
                elif why := vault.unfit(value):
                    raise ValueError(why)
            except (ValueError, secretstore.InternalSecret) as error:
                raise control.Refused(f"{name}: {error}; nothing was stored") from None
        free_to_start(cfg, repo, tid, "; the answer was not stored", ahead=True)
        command = continuing(cfg, repo, row)
        if given:
            try:
                if store:
                    with secretstore.registry(write=True):
                        for name, value in given.items():
                            secretstore.put(scope, name, value, "repo_answer", principals.current(), managed_by="external")
                else:
                    secret_move.put_legacy_repo(repo.name, given)
            except (OSError, ValueError, secretstore.StoreMissing) as error:
                raise control.Refused(f"could not store {', '.join(given)} ({error}); the ask stays open") from None
        with PENDING_LOCK, util.lock(START_LOCK, wait=True):
            with writes.atomic():
                if not ask_store.ask_close(ask_id, "answered", answers):
                    raise AlreadyResolved(f"ask {ask_id} was answered from the tracker a moment ago, so this answer was not stored")
                if more := ask_store.asks_of(row["id"], "open"):
                    return answer(tid, repo.name, "", "waiting",
                                  f"{util.count(len(more), 'more ask')} of run {row['id']} wait for an answer")
                new_run_id = pipeline.queue_run(repo.name, tid, continues=row["id"], restart="answer")
            full_command = [*command, "--run-id", new_run_id]
            record.write_launch(new_run_id, full_command, pipeline.RUNS_LOG)
            spawn_queued(repo.name, tid, lambda: control.launch(full_command, pipeline.RUNS_LOG))
        return answer(tid, repo.name, new_run_id, "running", "started")


def answers_holder(ask: dict) -> dict | None:
    newest = run_store.newest_whole_run(ask["repo"], ask["ticket"])
    if newest and ask["run"] in {newest["id"], *factory.chain_runs(newest["id"])}:
        return newest
    return None


def answers_held(ask: dict) -> str:
    if ask["status"] != "answered" or ask["taken_by"] or not (holder := answers_holder(ask)):
        return ""
    run_id = holder["id"]
    held = f"; its answers are stored for run {run_id}"
    if still_running(holder):
        return f"{held}, which is starting with them"
    if more := len(ask_store.asks_of(run_id, "open")):
        waiting = f"{util.count(more, 'other ask')} {'is' if more == 1 else 'are'}"
        return f"{held}, which goes on with them when its {waiting} answered"
    if holder["status"] in (*RESUMABLE, "running") or parked_with_answers(holder):
        return f"{held}, and `arete resume {run_id}` continues it with them"
    return held


def still_running(row: dict) -> bool:
    if row["status"] != "running":
        return False
    with record.dead(row) as gone:
        return not gone


def parked_with_answers(row: dict) -> bool:
    held = pipeline.answered_run(row["repo"], str(row["issue"]))
    return bool(held) and held["id"] == row["id"]


def continuing(cfg: config.Config, repo: config.Repo, row: dict) -> list[str]:
    tid = str(row["issue"])
    return pipeline.adw_command(repo.name, tid, "--force", "--continue", row["id"], "--restart", "answer")


def continue_asked(cfg: config.Config, repo: config.Repo, row: dict, command: list[str] | None = None,
                   admission=None) -> dict:
    return launch(cfg, repo, str(row["issue"]), command or continuing(cfg, repo, row), ahead=True,
                  admission=admission, continues=row["id"], restart="answer")


def kept_folder_removed(repo: config.Repo, tid: str, run_id: str, *, task_id: int | None = None) -> workspace.Release | None:
    ws = paths.WORKTREES_DIR / f"{repo.name}-{tid}"
    with workspaces.folder_lock(ws):
        row = workspaces.open_for(ws.name)
        if not row:
            return None if os.path.lexists(ws) else workspace.Release("removed")
        if row["owner_run"] != run_id or row["kept_reason"] in (workspaces.LEGACY_RESUME, "operator"):
            return None
        if row["kept_reason"] == workspaces.LEGACY_WAITING:
            latest = (
                run_store.newest_whole_run(int(task_id))
                if task_id is not None
                else run_store.newest_whole_run(repo.name, tid)
            )
            if latest and latest["id"] == run_id and (row["repo"], row["ticket"]) == (repo.name, tid):
                return workspaces.remove_imported_waiting(ws, row)
            return None
        try:
            checkouts = [workspace.Checkout.from_row(c)
                         for c in flows.prepared_data(run_id).get("checkouts") or []]
        except (TypeError, KeyError):
            checkouts = None
        if not checkouts:
            checkouts = workspaces.owner_of(row).checkouts
            if not checkouts and os.path.lexists(ws):
                return None
            made = workspace.made_before(ws)
            for c in checkouts:
                attached, unsafe = workspace.checkout_attachment(ws, c.wt)
                if unsafe is not None or (attached and not saved_checkout_head(made, c)):
                    return None
        return workspace.remove_workspace(ws, checkouts, edits=True)


def saved_checkout_head(made: dict, checkout: workspace.Checkout) -> bool:
    head = workspace.head_recorded(made, checkout)
    return bool(head and util.git(["symbolic-ref", "-q", "HEAD"], checkout.wt, check=False).strip()
                and util.git(["rev-parse", "HEAD"], checkout.wt, check=False).strip() == head)


def let_go(cfg: config.Config, repo: config.Repo, row: dict, status: str) -> dict:
    raw_tid, run_id = str(row["issue"]), row["id"]
    task_id = row.get("task_id")
    tid = str(row.get("issue_key") or raw_tid)
    if tracker.is_legacy_task_id(tid) and task_id is not None:
        tid = task_store.format_task_id(int(task_id))
    name = tracker.issue_name(repo.name, tid)
    with workspaces.ticket_lock(repo.name, tid, task_id=task_id) as got:
        if not got:
            raise control.Busy(f"a run of {name}")
        if status == "stopped" and not record.move(run_id, "stopped", expect=("needs-info",), summary=record.STOPPED):
            raise control.Refused(f"run {run_id} is no longer waiting for an answer, so it was not stopped")
        if status == "closed" and not record.move(run_id, "stopped", expect=("needs-info",), summary=TICKET_CLOSED):
            raise control.Refused(f"run {run_id} is no longer waiting for an answer, so it was not stopped")
        ask_store.asks_retire(run_id, "stopped" if status == "closed" else status)
        latest = not (
            run_store.later_run(int(task_id), run_id)
            if task_id is not None
            else run_store.later_run(repo.name, tid, run_id)
        )
        removed = False
        folder = "its work folder was left in place"
        try:
            release = kept_folder_removed(repo, raw_tid, run_id, task_id=task_id) if latest else None
        except (OSError, sqlite3.Error, foundation.IncompatibleSchema, migrations.Refused) as e:
            folder += f" (removal failed: {util.first_line(str(e), 300)})"
        else:
            if release and release.state == "removed":
                removed = True
                folder = "its work folder is removed"
            elif release and release.held_by:
                folder += f" (removal blocked by {', '.join(release.held_by[:10])})"
        if status == "closed":
            pipeline.write_report(run_store.run_get(run_id) or row)
            return answer(tid, repo.name, run_id, "stopped", f"run {run_id} stopped because {name} closed; {folder}")
        run = pipeline.Run(run_id, repo, paths.RUNS_DIR / run_id, tracker=tracker.for_ticket(cfg, repo, tid),
                           tid=tid, task_id=task_id)
        run.dir.mkdir(parents=True, exist_ok=True)
        if status == "stopped":
            run.give_up("was stopped by its owner instead of an answer")
            pipeline.write_report(run_store.run_get(run_id) or row)
            return answer(tid, repo.name, run_id, "stopped", f"run {run_id} stopped; {folder}")
        if latest:
            run.say(f"No answer came in {cfg.ask.expire_days:g} days, so I let go of the kept work{'' if removed else ' as far as I could'}. "
                    f"Reply here and remove the `{run.tracker.label_name('needs-info')}` label to start again.")
        return answer(tid, repo.name, run_id, "needs-info", f"run {run_id} expired; {folder}")


def expire_asks(cfg: config.Config) -> list[str]:
    cutoff = util.iso(util.now() - timedelta(days=cfg.ask.expire_days))
    notes = []
    for run_id in dict.fromkeys(a["run"] for a in ask_store.asks_asked_before(cutoff)):
        with util.lock(STEER_LOCK, wait=True):
            if not ask_store.asks_of(run_id, "open"):
                continue
            try:
                _, repo, row = run_of(run_id)
            except (control.NotFound, control.Refused):
                row = None
            if row is None or row["status"] != "needs-info" or run_store.later_run(row["repo"], row["issue"], run_id):
                ask_store.asks_retire(run_id, "expired")
                notes.append(f"asks of run {run_id} expired; the run is not the ticket's latest")
                continue
            if live_run(repo.name, str(row["issue"])):
                continue
            try:
                notes.append(let_go(cfg, repo, row, "expired")["why"])
            except control.Busy:
                continue
    return notes


def asked_ticket_is_closed(cfg: config.Config, repo: str, tid: str) -> bool:
    if tracker.is_task_id(tid):
        return workspace.ticket_closed(tid)
    configured = next((r for r in cfg.repos if r.name == repo), None)
    if configured is None:
        return False
    try:
        return not tracker.for_repo(cfg, configured).get(tid).open
    except Exception:
        return False


def release_closed(cfg: config.Config) -> list[str]:
    notes = []
    for run_id in dict.fromkeys(a["run"] for a in ask_store.open_asks()):
        try:
            _, repo, row = run_of(run_id)
        except (control.NotFound, control.Refused):
            continue
        tid = str(row["issue"])
        if row["status"] != "needs-info" or run_store.later_run(repo.name, row["issue"], run_id) or live_run(repo.name, tid):
            continue
        if not asked_ticket_is_closed(cfg, repo.name, tid):
            continue
        with util.lock(STEER_LOCK, wait=True):
            row = run_store.run_get(run_id) or row
            if (not ask_store.asks_of(run_id, "open") or row["status"] != "needs-info"
                    or run_store.later_run(repo.name, row["issue"], run_id) or live_run(repo.name, tid)):
                continue
            try:
                notes.append(let_go(cfg, repo, row, "closed")["why"])
            except (control.Busy, control.Refused):
                continue
    return notes


def resume_factory_run(row):
    """Respect single-step scope and the original workflow's continuation contract."""
    key = row["id"]
    if row.get("only_step"):
        lines = run_lines.read(key)
        execution = factory.first(lines, "execution")
        source = execution.get("continues") or factory.first(lines, "route").get("continues")
        if not source:
            raise control.Refused("Cannot identify the source of this single-step run")
        cfg, repo, _ = command_run(key, row["version"])
        path = pipeline.recorded_path(source)
        step = row["only_step"]
        select = ["--action", step, "--only-action"] if path == "orchestrate" else ["--only", step]
        settings = [f"--set={setting}" for setting in execution.get("settings", [])]
        return launch(cfg, repo, str(row["issue"]), pipeline.adw_command(
            repo.name, str(row["issue"]), "--continue", source, *select, *settings,
            *(["--dry-run"] if row.get("dry_run") else [])), admission=(key, row["version"]))
    path = pipeline.recorded_path(key)
    if row.get("dry_run") and path != "classic":
        cfg, repo, _ = command_run(key, row["version"])
        return launch(cfg, repo, str(row["issue"]), pipeline.adw_command(
            repo.name, str(row["issue"]), "--dry-run", "--continue", key), admission=(key, row["version"]))
    if path == "classic":
        with util.lock(STEER_LOCK, wait=True):
            cfg, repo, row = command_run(key, row["version"])
            no_later_run(row)
            if row["status"] != "interrupted" or row.get("files_purged"):
                raise control.Refused("The interrupted classic workspace is no longer available")
            if (paths.RUNS_DIR / key / "factory-publishing.json").exists():
                raise control.Refused("Inspect the interrupted classic publication before retrying")
            return launch(cfg, repo, str(row["issue"]), control.command(
                ["run", repo.name, str(row["issue"]), "--local", "--force", *(["--dry-run"] if row.get("dry_run") else [])]), admission=(key, row["version"]))
    return resume(key, base_version=row["version"])


def resume(run_id: str, step: str = "", base_version=None, *, environment_release: str = "") -> dict:
    from .store import factory as factory_state

    factory_state.admit()
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = command_run(run_id, base_version)
        if row.get("files_purged"):
            raise refusals.Gone(
                f"run {run_id} files were removed on {row['files_purged']}; "
                "start the ticket again with POST /api/tasks",
                purged_at=row["files_purged"],
            )
        if row["status"] == "running":
            with record.dead(row) as gone:
                if gone:
                    with events.collect_receipts() as recovered:
                        if settled := record.interrupt(row, base_version=base_version):
                            pipeline.tell_ticket_run_died(cfg, settled)
                    if base_version is not None:
                        base_version += sum(subject == f"run:{run_id}" for _, subject, _ in recovered)
            row = run_store.run_get(run_id) or row
        if row["status"] == "running":
            raise control.Refused(f"run {run_id} is running; stop it first")
        if parked_with_answers(row):
            return continue_asked(cfg, repo, row, admission=(run_id, base_version))
        if row.get("only_step"):
            raise control.Refused(f"run {run_id} ran {row['only_step']} alone; resume the run it continued")
        environment_retry = bool(environment_release and row["status"] == "pr" and step == "verify")
        if row["status"] not in RESUMABLE and not environment_retry:
            raise control.Refused(f"run {run_id} ended {row['status']}; a run resumes only when it ended "
                                  f"{', '.join(RESUMABLE[:-1])} or {RESUMABLE[-1]}")
        no_later_run(row, live=environment_retry)
        path = pipeline.recorded_path(run_id)
        if environment_retry:
            from . import environment_gaps

            if path not in environment_gaps.RESUME_PATHS or environment_gaps.live_release() != environment_release:
                raise control.Refused(f"the release for run {run_id}'s verify retry is no longer live")
            if path == "orchestrate":
                if not (step := environment_gaps.verification_action(run_id)):
                    raise control.Refused(f"run {run_id} has no saved verification action to rerun")
            elif path == "classic":
                tid = str(row["issue"])
                return launch(cfg, repo, tid, control.command(["run", repo.name, tid, "--local", "--force",
                              "--continue", run_id, "--environment-release", environment_release]),
                              admission=(run_id, base_version), continues=run_id, restart="resume")
        if path == "classic":
            path = ""
        if path == "orchestrate":
            if step:
                adaptive_action(row, step)
            return launch(cfg, repo, str(row["issue"]), pipeline.adw_command(
                repo.name, str(row["issue"]), "--force", "--continue", run_id,
                *(["--action", step] if step else []),
                *(["--environment-release", environment_release] if environment_retry else [])),
                admission=(run_id, base_version), continues=run_id, restart="resume")
        if step and step_named(step) and not factory.started(run_id, step):
            raise control.Refused(f"run {run_id} never started {step}, so it cannot resume from it")
        if not path and not flows.ran_on_seam(run_id):
            raise control.Refused(f"run {run_id} ran on the classic path, so it cannot resume; start the ticket again "
                                  "with POST /api/tasks")
        tid = str(row["issue"])
        if not step and row["status"] == "failed":
            step = next((str(line.get("step") or "") for line in reversed(factory.history(factory.chain(run_id)))
                         if line.get("result") != "pass"), "")
        return launch(cfg, repo, tid, pipeline.adw_command(repo.name, tid, "--force", "--continue", run_id,
                                                           *(["--from", step] if step else []),
                                                           *(["--environment-release", environment_release] if environment_retry else [])),
                      admission=(run_id, base_version), continues=run_id, restart="resume")


def repair(run_id: str, base_version=None) -> dict:
    from .store import factory as factory_state

    factory_state.admit()
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = command_run(run_id, base_version)
        if row.get("files_purged"):
            raise refusals.Gone(
                f"run {run_id} files were removed on {row['files_purged']}; "
                "start the ticket again with POST /api/tasks",
                purged_at=row["files_purged"],
            )
        tid = str(row["issue"])
        if (later := run_store.later_run(row["repo"], row["issue"], run_id)) and pipeline.repaired_by(later["id"]) == run_id:
            return answer(tid, repo.name, later["id"], later["status"],
                          f"run {later['id']} already repairs run {run_id}")
        if row["status"] == "running":
            raise control.Refused(f"run {run_id} is running; stop it first")
        no_later_run(row)
        try:
            found = repairs.source(row)
            repairs.open_heads(repo, found)
        except repairs.Refused as e:
            raise control.Refused(str(e)) from None
        path = "orchestrate" if pipeline.recorded_path(run_id) == "orchestrate" else "repair"
        if refusal := pipeline.script_refusal(cfg, repo, tid, pipeline.Script(path, None, repairs=run_id)):
            raise control.Refused(refusal)
        return launch(cfg, repo, tid, pipeline.adw_command(repo.name, tid, "--force", "--repair", run_id),
                      admission=(run_id, base_version), continues=run_id, restart="resume")


def adaptive_action(row: dict, action: str) -> dict:
    from . import worker_turns
    from .orchestration_api import WORKER_PROTOCOL
    from .store import orchestration, worker_exchange

    with foundation.reader() as connection:
        source = orchestration.find(row["id"])
        if source:
            lease = orchestration.Lease(source["root"], source["current_run"], source["generation"])
            known = {a["id"]: a for a in orchestration.actions(lease)}
            found = known.get(action)
            if found and orchestration.unlaunched_start(found):
                raise control.Refused("the action has no finished invocation to rerun")
            if found and source["data"].get("protocol") == WORKER_PROTOCOL:
                if worker_turns.private_rerun_blocked(known, worker_exchange.workers(lease, connection)):
                    raise control.Refused("resume the controller to settle outstanding workers before rerunning an action")
                if found["state"] not in orchestration.TERMINAL or (
                        found["op"] in ("worker.start", "worker.continue") and not found["result"].get("effective")):
                    raise control.Refused("the action has no finished invocation to rerun")
            if found and found["op"] in ("worker.start", "worker.continue", "checks.run", "verification.run", "review.run"):
                return found
    raise control.Refused("adaptive controls require a worker/check/review/verification action ID from run details, "
                          "not a phase name or publication action")


def run_step(run_id: str, step: str, sets, base_version=None) -> dict:
    from .store import factory as factory_state

    factory_state.admit()
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = command_run(run_id, base_version)
        if row.get("files_purged"):
            raise refusals.Gone(
                f"run {run_id} files were removed on {row['files_purged']}; "
                "start the ticket again with POST /api/tasks",
                purged_at=row["files_purged"],
            )
        if purged_blob := blob_store.purged_input_for_step(run_id, step):
            blob_sha, purged_at = purged_blob
            raise refusals.Gone(
                f"input blob {blob_sha} for run {run_id} step {step} was removed on {purged_at}",
                purged_at=purged_at,
            )
        if row["status"] == "running":
            raise control.Refused(f"run {run_id} is running; a step runs alone only on a run that ended")
        path = str(row.get("adw") or "")
        if not path:
            from .store import orchestration

            if orchestration.find(run_id):
                path = "orchestrate"
        if not path:
            raise control.Refused(f"run {run_id} has no recorded ADW, so a step cannot run alone on it")
        if path == "orchestrate":
            no_later_run(row)
            adaptive_action(row, step)
            if why := flows.action_workspace_refusal(run_id):
                raise control.Refused(why)
            if sets:
                raise control.Refused("adaptive action reruns keep their accepted settings; select new settings through a resumed controller")
            return launch(cfg, repo, str(row["issue"]), pipeline.adw_command(
                repo.name, str(row["issue"]), "--force", "--continue", run_id,
                "--action", step, "--only-action"), admission=(run_id, base_version),
                continues=run_id, restart="step", only_step=step)
        step_named(step)
        if not factory.started(run_id, step):
            raise control.Refused(f"run {run_id} never started {step}, so it cannot run it alone")
        if not isinstance(sets, dict) or not all(isinstance(k, str) and type(v) in (str, int, float)
                                                 for k, v in sets.items()):
            raise control.Refused('Give set as an object of setting names to strings or numbers, such as '
                                  '{"model": "opus"}')
        if sets and step not in flows.ROLE_OF:
            raise control.Refused(f"{step} calls no agent, so it takes no settings")
        settings, tid = [f"{k}={v}" for k, v in sets.items()], str(row["issue"])
        try:
            table = factory.overrides([f"{step}.{s}" for s in settings], flows.ROLE_OF)
        except (ValueError, TypeError) as e:
            raise control.Refused(str(e)) from None
        if refusal := pipeline.script_refusal(cfg, repo, tid, pipeline.Script(path, None, run_id, step, table)):
            raise control.Refused(refusal)
        if gone := removed_tree(run_id, step):
            raise control.Refused(f"run {run_id} ended {row['status']} and removed its work folder {gone}, so a step "
                                  "cannot run alone on it")
        return launch(cfg, repo, tid, pipeline.adw_command(
            repo.name, tid, "--continue", run_id, "--only", step,
            *(f"--set={step}.{s}" for s in settings)), admission=(run_id, base_version),
            continues=run_id, restart="step", only_step=step)


def removed_tree(run_id: str, step: str) -> str:
    for line in factory.history(factory.chain(run_id)):
        if line.get("step") == step:
            return ""
        if line.get("tree") and not os.path.isdir(line["tree"]):
            why = flows.remade(line)[2]
            return f"{line['tree']}, and {why}" if why else ""
    return ""


def last_report(run_id: str) -> dict:
    return next((line for line in reversed(run_lines.read(run_id)) if line.get("event") == run_lines.END), {})


def task_summaries() -> list[dict]:
    return task_store.task_summaries(connect=foundation.reader)


def view(task_id: str, repo_name: str = "") -> dict:
    tid = task_id.strip()
    if tracker.is_legacy_task_id(tid) and task_store.is_contracted():
        raise control.Refused(task_store.LEGACY_TASK_REFUSAL)
    if tracker.is_task_id(tid):
        if (row := task_store.task_get(tid, connect=foundation.reader)) is None:
            raise control.NotFound(f"no task {tid}")
        item = {**row, "comments": task_store.task_comments(row["id"], connect=foundation.reader)}
        repo = row["repo"]
        refs = [row["id"]]
        runs = (
            run_store.ticket_runs(int(row["num"]), limit=50, connect=foundation.reader)
            if row.get("num") is not None
            else run_store.ticket_runs(repo, row["id"], limit=50, connect=foundation.reader)
        )
    else:
        item, repo = tracker_item(config.load(), tid, repo_name.strip())
        refs = [item["id"], item["ref"]]
        runs = run_store.ticket_runs(repo, item["id"], limit=50, connect=foundation.reader)
    paid = run_store.paid_by_run([r["id"] for r in runs], connect=foundation.reader)
    children = {c["id"]: c for ref in dict.fromkeys(refs) for c in task_store.task_children(ref, connect=foundation.reader)}
    return {**item,
            "runs": [{"id": r["id"], "status": r["status"], "started": r["started"], "finished": r["finished"],
                      "prs": (r["pr_url"] or "").split(), "cost_usd": paid.get(r["id"], 0.0),
                      "report": last_report(r["id"])} for r in runs],
            "children": [{k: c[k] for k in ("id", "title", "state", "kind")} for c in children.values()]}


def tracker_item(cfg: config.Config, tid: str, repo_name: str) -> tuple[dict, str]:
    repo = repo_named(cfg, repo_name) if repo_name else newest_repo(cfg, tid)
    tk = tracker.for_repo(cfg, repo)
    try:
        tid = tk.normalize(tid)
    except ValueError as e:
        raise control.Refused(f"{repo.name}: {e}") from None
    try:
        t = tk.get(tid)
    except (gh.GhError, jira.JiraError) as e:
        if gh.NO_ISSUE in str(e) or getattr(e, "status", 0) == 404:
            raise control.NotFound(f"no {tk.ref(tid)} in {repo.name}") from None
        raise
    return {"id": t.id, "ref": t.ref, "repo": repo.name, "title": t.title, "url": t.url,
            "state": tracker.item_state(tk, t)}, repo.name


def newest_repo(cfg: config.Config, tid: str) -> config.Repo:
    found = []
    for r in cfg.repos:
        try:
            issue = tracker.for_repo(cfg, r).normalize(tid)
        except ValueError:
            continue
        found += [(run["started"] or "", r) for run in run_store.ticket_runs(r.name, issue, limit=1, connect=foundation.reader)]
    if not found:
        raise control.Refused(f"No run names {tid}, so say which repo it belongs to with ?repo=NAME")
    return max(found, key=lambda pair: pair[0])[1]
