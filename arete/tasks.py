from __future__ import annotations

import os
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from . import (
    asks,
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
    runlimits,
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
        why, _ = runlimits.admission(repo, tid, tracker.for_ticket(cfg, repo, tid),
                                     runlimits.Policy.of(config.load_layer(repo.layer_dir)), force)
        if why:
            raise control.Refused(why)
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


def free_to_start(cfg: config.Config, repo: config.Repo, tid: str, after: str = "", ahead: bool = False) -> None:
    if run := live_run(repo.name, tid):
        raise control.Busy(f"run {run['id']} of {tracker.ticket_name(repo.name, tracker.ref_for(tid))}")
    if not ahead and (why := held(cfg, repo)):
        raise pipeline.NoFreeSlot(why + after)
    why, _ = runlimits.admission(repo, tid, tracker.for_ticket(cfg, repo, tid),
                                 runlimits.Policy.of(config.load_layer(repo.layer_dir)), True)
    if why:
        raise control.Refused(why)


def launch(cfg: config.Config, repo: config.Repo, tid: str, command: list[str], ahead: bool = False) -> dict:
    with util.lock(START_LOCK, wait=True):
        free_to_start(cfg, repo, tid, ahead=ahead)
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
    return next((str(path) for read in factory.chain(row["id"]) if (path := factory.first(read, "task").get("path"))),
                "")


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
        if waiting := state.asks_of(run_id, "open"):
            return reply_to_asks(cfg, repo, row, waiting, text)
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


def reply_to_asks(cfg: config.Config, repo: config.Repo, row: dict, waiting: list[dict], text: str) -> dict:
    if any(asks.has_secret(a["questions"]) for a in waiting):
        raise control.Refused(f"run {row['id']} asks a secret question, which a text reply cannot answer; answer it "
                              f"on the dashboard or with `arete answer {row['id']} --json`")
    tid = str(row["issue"])
    free_to_start(cfg, repo, tid, "; the answer was not stored", ahead=True)
    command = continuing(cfg, repo, row)
    kept = store_answer(cfg, repo, tid, text)
    for a in waiting:
        state.ask_close(a["id"], "answered", asks.text_answers(a, text))
    started = continue_asked(cfg, repo, row, command)
    return {**started, "why": f"{started['why']}; {kept}"} if kept else started


def asks_view() -> dict:
    keys = ("id", "repo", "ticket", "run", "step", "asked", "why", "tried", "questions")
    return {"asks": [{k: a[k] for k in keys} for a in state.open_asks(connect=state.reader)]}


def answer_ask(ask_id: str, body) -> dict:
    with util.lock(STEER_LOCK, wait=True):
        ask = state.ask_get(ask_id)
        if not ask:
            raise control.NotFound(f"no ask {ask_id}")
        if ask["status"] != "open":
            raise control.Refused(f"ask {ask_id} is {ask['status']}, so it takes no answer{answers_held(ask)}")
        cfg, repo, row = run_of(ask["run"])
        if row["status"] != "needs-info":
            raise control.Refused(f"run {row['id']} is {row['status']}, so ask {ask_id} takes no answer")
        no_later_run(row)
        tid = str(row["issue"])
        if run := live_run(repo.name, tid):
            raise control.Busy(f"run {run['id']} of {tracker.ticket_name(repo.name, tracker.ref_for(tid))}")
        if isinstance(body, dict) and "stop" in body:
            if body != {"stop": True}:
                raise control.Refused('To stop the run, send {"stop": true} alone')
            return let_go(cfg, repo, row, "stopped")
        try:
            answers = asks.answers_for(ask, body)
        except (ValueError, TypeError) as e:
            raise control.Refused(str(e)) from None
        free_to_start(cfg, repo, tid, "; the answer was not stored", ahead=True)
        command = continuing(cfg, repo, row)
        state.ask_close(ask_id, "answered", answers)
        if more := state.asks_of(row["id"], "open"):
            return answer(tid, repo.name, "", "waiting",
                          f"{util.count(len(more), 'more ask')} of run {row['id']} wait for an answer")
        return continue_asked(cfg, repo, row, command)


def answers_holder(ask: dict) -> dict | None:
    newest = state.ticket_runs(ask["repo"], ask["ticket"], limit=1, whole=True)
    if newest and ask["run"] in {newest[0]["id"], *flows.chain_runs(newest[0]["id"])}:
        return newest[0]
    return None


def answers_held(ask: dict) -> str:
    if ask["status"] != "answered" or ask["taken_by"] or not (holder := answers_holder(ask)):
        return ""
    run_id = holder["id"]
    held = f"; its answers are stored for run {run_id}"
    if still_running(holder):
        return f"{held}, which is starting with them"
    if more := len(state.asks_of(run_id, "open")):
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
    path = script_path(row) or ("revise" if pipeline.pending_feedback(repo.name, tid) else "feature")
    return command_of(cfg, path, repo.name, tid, "--force", "--continue", row["id"])


def continue_asked(cfg: config.Config, repo: config.Repo, row: dict, command: list[str] | None = None) -> dict:
    return launch(cfg, repo, str(row["issue"]), command or continuing(cfg, repo, row), ahead=True)


def kept_folder_removed(repo: config.Repo, tid: str, run_id: str) -> bool:
    try:
        checkouts = [pipeline.Checkout(**{**c, "src": Path(c["src"]), "wt": Path(c["wt"])})
                     for c in flows.prepared_data(run_id).get("checkouts") or []]
    except (TypeError, KeyError):
        return False
    return pipeline.remove_workspace(paths.WORKTREES_DIR / f"{repo.name}-{tid}", checkouts)


def let_go(cfg: config.Config, repo: config.Repo, row: dict, status: str) -> dict:
    tid, run_id = str(row["issue"]), row["id"]
    name = tracker.ticket_name(repo.name, tracker.ref_for(tid))
    with util.lock(state.run_lock("pipeline", repo.name, tid)) as got:
        if not got:
            raise control.Busy(f"a run of {name}")
        state.asks_retire(run_id, status)
        latest = not state.later_run(repo.name, tid, run_id)
        removed = latest and kept_folder_removed(repo, tid, run_id)
        folder = "its work folder is removed" if removed else "its work folder was left in place"
        run = pipeline.Run(run_id, repo, paths.RUNS_DIR / run_id, tracker=tracker.for_ticket(cfg, repo, tid), tid=tid)
        run.dir.mkdir(parents=True, exist_ok=True)
        if status == "stopped":
            record.note(run_id, status="stopped", summary=record.STOPPED)
            run.give_up("was stopped by its owner instead of an answer")
            pipeline.write_report(state.run_get(run_id) or row)
            return answer(tid, repo.name, run_id, "stopped", f"run {run_id} stopped; {folder}")
        if latest:
            run.say(f"No answer came in {cfg.ask.expire_days:g} days, so I let go of the kept work{'' if removed else ' as far as I could'}. "
                    f"Reply here and remove the `{run.tracker.label_name('needs-info')}` label to start again.")
        return answer(tid, repo.name, run_id, "needs-info", f"run {run_id} expired; {folder}")


def expire_asks(cfg: config.Config) -> list[str]:
    cutoff = util.iso(util.now() - timedelta(days=cfg.ask.expire_days))
    notes = []
    for run_id in dict.fromkeys(a["run"] for a in state.asks_asked_before(cutoff)):
        with util.lock(STEER_LOCK, wait=True):
            if not state.asks_of(run_id, "open"):
                continue
            try:
                _, repo, row = run_of(run_id)
            except (control.NotFound, control.Refused):
                row = None
            if row is None or row["status"] != "needs-info" or state.later_run(row["repo"], row["issue"], run_id):
                state.asks_retire(run_id, "expired")
                notes.append(f"asks of run {run_id} expired; the run is not the ticket's latest")
                continue
            if live_run(repo.name, str(row["issue"])):
                continue
            try:
                notes.append(let_go(cfg, repo, row, "expired")["why"])
            except control.Busy:
                continue
    return notes


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
        if parked_with_answers(row):
            return continue_asked(cfg, repo, row)
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
