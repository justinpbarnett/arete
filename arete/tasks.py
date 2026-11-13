from __future__ import annotations

import os
import subprocess
import time
from collections import Counter
from collections.abc import Callable

from . import (
    chat,
    config,
    control,
    factory,
    flows,
    gh,
    jira,
    lines,
    paths,
    pipeline,
    record,
    repairs,
    state,
    tracker,
    util,
)

TITLE_CHARS = 80
START_LOCK = "task-start"
STEER_LOCK = "task-steer"
POLLER_LATER = "the poller starts it when a slot frees"
RESUMABLE = ("interrupted", "stopped", "failed")
MAX_ANSWER_CHARS = 20000


class TrackerFailed(RuntimeError):
    pass


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
          force: bool = True) -> dict:
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
        return start_ticket(cfg, repo, ticket, force)
    tid = state.task_add(repo.name, prompt, title.strip() or title_of(prompt), kind or "feat")
    if waits := tracker.LocalTracker(repo).blocker(tid):
        return answer(tid, repo.name, "", "queued", f"{waits}; " + ("the poller starts it once the wait ends"
                                                                    if repo.pipeline else "send its ticket to start it"))
    return begin(cfg, repo, tid, force=False, poller_picks=repo.pipeline)


def start_ticket(cfg: config.Config, repo: config.Repo, ticket: str, force: bool = True) -> dict:
    try:
        tid = tracker.for_ticket(cfg, repo, ticket).normalize(ticket)
    except ValueError as e:
        raise control.Refused(f"{repo.name}: {e}") from None
    if tracker.is_task_id(tid):
        if (row := state.task_get(tid)) is None:
            raise control.NotFound(f"no task {tid}")
        if row["repo"] != repo.name:
            raise control.Refused(f"{tid} is a task of {row['repo']}, not {repo.name}")
        if row["state"] in config.TASK_FINISHED:
            raise control.Refused(f"task {tid} is {row['state']}, so it does not start again; add a new task")
    return begin(cfg, repo, tid, force=force, poller_picks=False)


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
    later = POLLER_LATER if poller_picks else (
        f'start task {tid} with {{"repo": "{repo.name}", "ticket": "{tid}"}} when a slot frees')
    return answer(tid, repo.name, "", "queued", f"{why}; {later}")


def begin(cfg: config.Config, repo: config.Repo, tid: str, force: bool, poller_picks: bool) -> dict:
    with util.lock(START_LOCK, wait=True):
        if run := live_run(repo.name, tid):
            return answer(tid, repo.name, run["id"], "running", "already running")
        if why := held(cfg, repo):
            return queued(repo, tid, why, poller_picks)
        if new := spawned(repo.name, tid, lambda: pipeline.spawn_run(repo.name, tid, force)):
            return answer(tid, repo.name, new, "running", "started")
        if why := held(cfg, repo):
            return queued(repo, tid, why, poller_picks)
    raise did_not_show(repo, tid)


def spawned(repo_name: str, tid: str, spawn: Callable[[], subprocess.Popen]) -> str:
    before = set(run_ids(repo_name, tid))
    proc = spawn()
    give_up = time.monotonic() + pipeline.RUN_START_SECONDS
    while time.monotonic() < give_up:
        exited = proc.poll() is not None
        if new := next((r for r in run_ids(repo_name, tid) if r not in before), ""):
            return new
        if exited:
            break
        time.sleep(0.1)
    return ""


def did_not_show(repo: config.Repo, tid: str) -> control.Refused:
    return control.Refused(f"{tracker.ticket_name(repo.name, tracker.ref_for(tid))} did not show as running; the end "
                           f"of {paths.short_path(paths.LOG_DIR / pipeline.RUNS_LOG)} says why")


def free_to_start(cfg: config.Config, repo: config.Repo, tid: str, after: str = "") -> None:
    if run := live_run(repo.name, tid):
        raise control.Busy(f"run {run['id']} of {tracker.ticket_name(repo.name, tracker.ref_for(tid))}")
    if why := held(cfg, repo):
        raise pipeline.NoFreeSlot(why + after)


def launch(cfg: config.Config, repo: config.Repo, tid: str, command: list[str]) -> dict:
    with util.lock(START_LOCK, wait=True):
        free_to_start(cfg, repo, tid)
        if new := spawned(repo.name, tid, lambda: control.launch(command, pipeline.RUNS_LOG)):
            return answer(tid, repo.name, new, "running", "started")
    raise did_not_show(repo, tid)


def run_of(run_id: str) -> tuple[config.Config, config.Repo, dict]:
    if not (row := state.run_get(run_id)):
        raise control.NotFound(f"no run {run_id}")
    if row["kind"] != "pipeline":
        raise control.Refused(f"run {run_id} is a {row['kind']} run, not the run of a ticket")
    cfg = config.load()
    try:
        return cfg, cfg.repo(row["repo"]), row
    except config.UnknownRepo:
        raise control.Refused(f"run {run_id} is of {row['repo']}, which is not a repo in "
                              f"{paths.short_path(paths.CONFIG_FILE)}") from None


def no_later_run(row: dict) -> None:
    if later := state.later_run(row["repo"], row["issue"], row["id"]):
        raise control.Refused(f"run {row['id']} is not the ticket's latest run: run {later['id']} "
                              f"({later['status']}) came after it")


def step_named(step: str) -> str:
    if step not in factory.STEPS:
        raise control.Refused(f"there is no step '{step}'; the steps are {', '.join(sorted(factory.STEPS))}")
    return step


def script_path(row: dict) -> str:
    return str(factory.first(lines.read(row["id"]), "task").get("path") or "")


def command_of(cfg: config.Config, name: str, *args: str) -> list[str]:
    try:
        return pipeline.script_command(cfg, name, *args)
    except FileNotFoundError as e:
        raise control.Refused(str(e)) from None


def reply(run_id: str, text: str) -> dict:
    if not (text := text.strip()):
        raise control.Refused("Give the answer in text")
    if len(text) > MAX_ANSWER_CHARS:
        raise control.Refused(f"the answer is {len(text)} characters; the most is {MAX_ANSWER_CHARS}")
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = run_of(run_id)
        if row["status"] != "needs-info":
            raise control.Refused(f"run {run_id} is {row['status']}, so it has no question to answer")
        no_later_run(row)
        tid = str(row["issue"])
        free_to_start(cfg, repo, tid, "; the answer was not stored")
        kept = store_answer(cfg, repo, tid, text)
        (folder := paths.RUNS_DIR / run_id).mkdir(parents=True, exist_ok=True)
        (folder / pipeline.ANSWER_FILE).write_text(text)
        started = begin(cfg, repo, tid, force=True, poller_picks=repo.pipeline)
        return {**started, "why": f"{started['why']}; {kept}"} if kept else started


def store_answer(cfg: config.Config, repo: config.Repo, tid: str, text: str) -> str:
    if tracker.is_task_id(tid):
        if not state.task_comment(tid, "owner", text):
            raise control.NotFound(f"no task {tid}")
        return ""
    tk = tracker.for_ticket(cfg, repo, tid)
    if error := tk.comment(tid, text):
        raise TrackerFailed(f"could not post the answer on {tk.ref(tid)}, so it was not stored: {error}")
    if isinstance(tk, tracker.GitHubTracker) and (error := tk.label(tid, remove=["needs-info"])):
        return f"the {tk.label_name('needs-info')} label stayed on {tk.ref(tid)}: {util.first_line(error, 300)}"
    return ""


def resume(run_id: str, step: str = "") -> dict:
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = run_of(run_id)
        if row["status"] == "running":
            with record.dead(row) as gone:
                if gone and (settled := record.interrupt(row)):
                    pipeline.tell_ticket_run_died(cfg, settled)
            row = state.run_get(run_id) or row
        if row["status"] == "running":
            raise control.Refused(f"run {run_id} is running; stop it first")
        if row.get("only_step"):
            raise control.Refused(f"run {run_id} ran {row['only_step']} alone; resume the run it continued")
        if row["status"] not in RESUMABLE:
            raise control.Refused(f"run {run_id} ended {row['status']}; a run resumes only when it ended "
                                  f"{', '.join(RESUMABLE[:-1])} or {RESUMABLE[-1]}")
        no_later_run(row)
        if step and step_named(step) and not factory.started(run_id, step):
            raise control.Refused(f"run {run_id} never started {step}, so it cannot resume from it")
        if not (path := script_path(row)) and flows.ran_on_seam(run_id):
            path = "revise" if pipeline.pending_feedback(row["repo"], str(row["issue"])) else "feature"
        if not path:
            raise control.Refused(f"run {run_id} ran on the classic path, so it cannot resume; start the ticket again "
                                  "with POST /api/tasks")
        tid = str(row["issue"])
        if not step and row["status"] == "failed":
            step = next((str(line.get("step") or "") for line in reversed(factory.history(factory.chain(run_id)))
                         if line.get("result") != "pass"), "")
        return launch(cfg, repo, tid, command_of(cfg, path, repo.name, tid, "--force", "--continue", run_id,
                                                 *(["--from", step] if step else [])))


def repair(run_id: str) -> dict:
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = run_of(run_id)
        tid = str(row["issue"])
        if (later := state.later_run(row["repo"], row["issue"], run_id)) and pipeline.repaired_by(later["id"]) == run_id:
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
        if refusal := pipeline.script_refusal(cfg, repo, tid, pipeline.Script("repair", None, repairs=run_id)):
            raise control.Refused(refusal)
        return launch(cfg, repo, tid, command_of(cfg, "repair", repo.name, tid, "--force", "--repair", run_id))


def run_step(run_id: str, step: str, sets) -> dict:
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = run_of(run_id)
        if row["status"] == "running":
            raise control.Refused(f"run {run_id} is running; a step runs alone only on a run that ended")
        if not (path := script_path(row)):
            raise control.Refused(f"run {run_id} did not run from a script in adws/, so a step cannot run alone on it")
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
        return launch(cfg, repo, tid, command_of(cfg, "step", run_id, step, *settings))


def removed_tree(run_id: str, step: str) -> str:
    for line in factory.history(factory.chain(run_id)):
        if line.get("step") == step:
            return ""
        if line.get("tree") and not os.path.isdir(line["tree"]):
            why = flows.remade(line)[2]
            return f"{line['tree']}, and {why}" if why else ""
    return ""


def last_report(run_id: str) -> dict:
    return next((line for line in reversed(lines.read(run_id)) if line.get("event") == lines.END), {})


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
        found += [(run["started"] or "", r) for run in state.ticket_runs(r.name, issue, limit=1, connect=state.reader)]
    if not found:
        raise control.Refused(f"No run names {tid}, so say which repo it belongs to with ?repo=NAME")
    return max(found, key=lambda pair: pair[0])[1]
