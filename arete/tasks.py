from __future__ import annotations

import time
from collections import Counter

from . import chat, config, control, gh, jira, paths, pipeline, state, tracker, util

TITLE_CHARS = 80
START_LOCK = "task-start"
LATER = {True: "the poller starts it when a slot frees", False: "send it again when a slot frees"}


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


def start(repo_name: str, prompt: str = "", title: str = "", kind: str = "", ticket: str = "") -> dict:
    prompt, ticket = prompt.strip(), ticket.strip()
    if not prompt and not ticket:
        raise control.Refused("Give a prompt to add a task and start it, or a ticket to start one that exists.")
    if prompt and ticket:
        raise control.Refused("Give a prompt or a ticket, not both.")
    cfg = config.load()
    repo = repo_named(cfg, repo_name.strip())
    if refusal := pipeline.run_refusal(repo)[1]:
        raise control.Refused(refusal)
    if kind not in ("", *config.KINDS):
        raise control.Refused(f"The kind is {' or '.join(config.KINDS)}, not '{kind}'.")
    if ticket:
        return start_ticket(cfg, repo, ticket)
    tid = state.task_add(repo.name, prompt, title.strip() or title_of(prompt), kind or "feat")
    if waits := tracker.LocalTracker(repo).blocker(tid):
        return answer(tid, repo.name, "", "queued", f"{waits}; " + ("the poller starts it once the wait ends"
                                                                    if repo.pipeline else "send its ticket to start it"))
    return begin(cfg, repo, tid, force=False, poller_picks=repo.pipeline)


def start_ticket(cfg: config.Config, repo: config.Repo, ticket: str) -> dict:
    try:
        tid = tracker.for_ticket(cfg, repo, ticket).normalize(ticket)
    except ValueError as e:
        raise control.Refused(f"{repo.name}: {e}") from None
    if tracker.is_task_id(tid):
        if (row := state.task_get(tid)) is None:
            raise control.NotFound(f"no task {tid}")
        if row["repo"] != repo.name:
            raise control.Refused(f"{tid} is a task of {row['repo']}, not {repo.name}")
    return begin(cfg, repo, tid, force=True, poller_picks=False)


def live_run(repo: str, tid: str) -> dict | None:
    row = state.running_run(repo, tid)
    return row if row and pipeline.holding([row], "pipeline") else None


def run_ids(repo: str, tid: str) -> list[str]:
    return [r["id"] for r in state.ticket_runs(repo, tid)]


def held(cfg: config.Config, repo: config.Repo) -> str:
    going = Counter(r["repo"] for r in pipeline.live_pipeline_runs())
    return pipeline.held_back(cfg, repo.name, going, pipeline.slots_free_to_start(cfg.max_parallel))


def queued(repo: config.Repo, tid: str, why: str, poller_picks: bool) -> dict:
    state.pass_over(repo.name, tid)
    return answer(tid, repo.name, "", "queued", f"{why}; {LATER[poller_picks]}")


def begin(cfg: config.Config, repo: config.Repo, tid: str, force: bool, poller_picks: bool) -> dict:
    with util.lock(START_LOCK, wait=True):
        if run := live_run(repo.name, tid):
            return answer(tid, repo.name, run["id"], "running", "already running")
        if why := held(cfg, repo):
            return queued(repo, tid, why, poller_picks)
        before = set(run_ids(repo.name, tid))
        proc = pipeline.spawn_run(repo.name, tid, force)
        give_up = time.monotonic() + pipeline.RUN_START_SECONDS
        while time.monotonic() < give_up:
            exited = proc.poll() is not None
            if new := next((r for r in run_ids(repo.name, tid) if r not in before), ""):
                return answer(tid, repo.name, new, "running", "started")
            if exited:
                break
            time.sleep(0.1)
        if why := held(cfg, repo):
            return queued(repo, tid, why, poller_picks)
    raise control.Refused(f"{tracker.ticket_name(repo.name, tracker.ref_for(tid))} did not show as running; the end of "
                          f"{paths.short_path(paths.LOG_DIR / pipeline.RUNS_LOG)} says why")


def view(task_id: str, repo_name: str = "") -> dict:
    tid = task_id.strip()
    if tracker.is_task_id(tid):
        tid = tid.lower()
        if (row := state.task_get(tid, connect=state.reader)) is None:
            raise control.NotFound(f"no task {tid}")
        item, repo, refs = {**row, "comments": state.task_comments(tid, connect=state.reader)}, row["repo"], [tid]
    else:
        item, repo = tracker_item(config.load(), tid, repo_name.strip())
        refs = [item["id"], item["ref"]]
    runs = state.ticket_runs(repo, item["id"], limit=50, connect=state.reader)
    paid = state.paid_by_run([r["id"] for r in runs], connect=state.reader)
    children = {c["id"]: c for ref in dict.fromkeys(refs) for c in state.task_children(ref, connect=state.reader)}
    return {**item,
            "runs": [{"id": r["id"], "status": r["status"], "started": r["started"], "finished": r["finished"],
                      "prs": (r["pr_url"] or "").split(), "cost_usd": paid.get(r["id"], 0.0)} for r in runs],
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
        found += [(run["started"] or "", r) for run in state.ticket_runs(r.name, issue, limit=1, connect=state.reader)]
    if not found:
        raise control.Refused(f"No run names {tid}, so say which repo it belongs to with ?repo=NAME")
    return max(found, key=lambda pair: pair[0])[1]
