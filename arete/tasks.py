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
    repairs,
    run_lines,
    runlimits,
    runner,
    slots,
    tracker,
    util,
    vault,
    workspace,
)
from .refusals import TrackerFailed
from .store import asks as ask_store
from .store import db as foundation
from .store import idempotency as claims
from .store import runs as run_store
from .store import sources as source_store
from .store import tasks as task_store
from .store import writes

TITLE_CHARS = 80
START_LOCK = "task-start"
STEER_LOCK = owner_asks.STEER_LOCK
POLLER_LATER = "the poller starts it when a slot frees"
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
        return start_ticket(cfg, repo, ticket, force, claim=claim)
    tid = task_store.task_add(repo.name, prompt, title.strip() or title_of(prompt), kind or "feat", claim=claim)
    return begin_prompt(cfg, repo, tid)


def begin_prompt(cfg, repo, tid):
    if waits := tracker.LocalTracker(repo).blocker(tid):
        return answer(tid, repo.name, "", "queued", f"{waits}; " + ("the poller starts it once the wait ends"
                                                                    if repo.pipeline else "send its ticket to start it"))
    return begin(cfg, repo, tid, force=False, poller_picks=repo.pipeline)


def start_ticket(cfg: config.Config, repo: config.Repo, ticket: str, force: bool = True, claim=None) -> dict:
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
    return begin(cfg, repo, tid, force=force, poller_picks=False)


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
    return row if row and slots.holding([row], "pipeline") else None


def run_ids(repo: str, tid: str) -> list[str]:
    return [r["id"] for r in run_store.ticket_runs(repo, tid)]


def held(cfg: config.Config, repo: config.Repo) -> str:
    going = Counter(r["repo"] for r in pipeline.live_pipeline_runs())
    return pipeline.held_back(cfg, repo.name, going, slots.slots_free_to_start(cfg.max_parallel))


def queued(repo: config.Repo, tid: str, why: str, poller_picks: bool) -> dict:
    source_store.pass_over(repo.name, tid)
    later = POLLER_LATER if poller_picks else (
        f'start task {tid} with {{"repo": "{repo.name}", "ticket": "{tid}"}} when a slot frees')
    return answer(tid, repo.name, "", "queued", f"{why}; {later}")


def begin(cfg: config.Config, repo: config.Repo, tid: str, force: bool, poller_picks: bool) -> dict:
    with PENDING_LOCK, util.lock(START_LOCK, wait=True):
        if pending := pending_start(repo.name, tid):
            return pending
        if run := live_run(repo.name, tid):
            return answer(tid, repo.name, run["id"], "running", "already running")
        if why := held(cfg, repo):
            return queued(repo, tid, why, poller_picks)
        why, _ = runlimits.admission(repo, tid, tracker.for_ticket(cfg, repo, tid),
                                     runlimits.Policy.of(config.load_layer(repo.layer_dir)), force)
        if why:
            raise control.Refused(why)
        new, state = spawned(repo.name, tid, lambda: pipeline.spawn_run(repo.name, tid, force))
        if state != "exited":
            return answer(tid, repo.name, new, state, "started" if new else "child is starting")
        if why := held(cfg, repo):
            return queued(repo, tid, why, poller_picks)
    raise did_not_show(repo, tid)


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
            exited = proc.poll() is not None
            row = next((r for r in run_store.ticket_runs(repo_name, tid) if r["id"] not in before), None)
            if row or exited:
                del PENDING[identity]
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
    global PENDING_REAPER

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


def spawned(repo_name: str, tid: str, spawn: Callable[[], subprocess.Popen]) -> tuple[str, str]:
    spawn_pending(repo_name, tid, spawn)
    give_up = time.monotonic() + pipeline.RUN_START_SECONDS
    while True:
        run, state = pending_state(repo_name, tid)
        if state != "starting" or time.monotonic() >= give_up:
            return run, state
        time.sleep(0.1)


def did_not_show(repo: config.Repo, tid: str) -> control.Refused:
    return control.Refused(f"{tracker.issue_name(repo.name, tid)} did not show as running; the end "
                           f"of {paths.short_path(paths.LOG_DIR / pipeline.RUNS_LOG)} says why")


def free_to_start(cfg: config.Config, repo: config.Repo, tid: str, after: str = "", ahead: bool = False) -> None:
    if run := live_run(repo.name, tid):
        raise control.Busy(f"run {run['id']} of {tracker.issue_name(repo.name, tid)}")
    if not ahead and (why := held(cfg, repo)):
        raise slots.NoFreeSlot(why + after)
    why, _ = runlimits.admission(repo, tid, tracker.for_ticket(cfg, repo, tid),
                                 runlimits.Policy.of(config.load_layer(repo.layer_dir)), True)
    if why:
        raise control.Refused(why)


def launch(cfg: config.Config, repo: config.Repo, tid: str, command: list[str], ahead: bool = False,
           admission=None) -> dict:
    def admitted_spawn():
        if admission and not writes.check_run(*admission, public=True):
            raise control.NotFound(f"no run {admission[0]}")
        return control.launch(command, pipeline.RUNS_LOG)

    with PENDING_LOCK, util.lock(START_LOCK, wait=True):
        if pending := pending_start(repo.name, tid):
            return pending
        free_to_start(cfg, repo, tid, ahead=ahead)
        new, state = spawned(repo.name, tid, admitted_spawn)
        if state != "exited":
            return answer(tid, repo.name, new, state, "started" if new else "child is starting")
    raise did_not_show(repo, tid)


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


def no_later_run(row: dict) -> None:
    if later := run_store.later_run(row["repo"], row["issue"], row["id"]):
        raise control.Refused(f"run {row['id']} is not the ticket's latest run: run {later['id']} "
                              f"({later['status']}) came after it")


def step_named(step: str) -> str:
    if step not in factory.STEPS:
        raise control.Refused(f"there is no step '{step}'; the steps are {', '.join(sorted(factory.STEPS))}")
    return step


def script_path(row: dict) -> str:
    path = pipeline.recorded_path(row["id"])
    return "" if path == "classic" else path


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
        waiting = ask_store.asks_of(run_id, "open")
        if not waiting and ask_store.asks_of(run_id):
            raise AlreadyResolved(f"run {run_id}'s asks were already resolved, so this answer was not stored")
        if row["status"] != "needs-info":
            raise control.Refused(f"run {run_id} is {row['status']}, so it has no question to answer")
        no_later_run(row)
        tid = str(row["issue"])
        if waiting:
            return reply_to_asks(cfg, repo, row, waiting, text)
        free_to_start(cfg, repo, tid, "; the answer was not stored")
        kept = store_answer(cfg, repo, tid, text)
        (folder := paths.RUNS_DIR / run_id).mkdir(parents=True, exist_ok=True)
        (folder / pipeline.ANSWER_FILE).write_text(text)
        started = begin(cfg, repo, tid, force=True, poller_picks=repo.pipeline)
        return {**started, "why": f"{started['why']}; {kept}"} if kept else started


def store_answer(cfg: config.Config, repo: config.Repo, tid: str, text: str) -> str:
    if tracker.is_task_id(tid):
        if not task_store.task_comment(tid, principals.current(), text):
            raise control.NotFound(f"no task {tid}")
        return ""
    tk = tracker.for_ticket(cfg, repo, tid)
    if error := tk.comment(tid, text):
        raise TrackerFailed(f"could not post the answer on {tk.ref(tid)}, so it was not stored: {error}")
    if isinstance(tk, tracker.GitHubTracker) and (error := tk.label(tid, remove=["needs-info"])):
        return f"the {tk.label_name('needs-info')} label stayed on {tk.ref(tid)}: {util.first_line(error, 300)}"
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
    with writes.atomic():
        for a in waiting:
            if not ask_store.ask_close(a["id"], "answered", owner_asks.text_answers(a, text)):
                raise AlreadyResolved(f"ask {a['id']} was already resolved; the answer was stored as a comment, "
                                      "but this reply did not update the asks or resume the run")
    started = continue_asked(cfg, repo, row, command)
    return {**started, "why": f"{started['why']}; {kept}"} if kept else started


def asks_view() -> dict:
    keys = ("id", "repo", "ticket", "run", "step", "asked", "why", "tried", "questions", "reminded")
    return {"asks": [{k: a[k] for k in keys} for a in ask_store.open_asks(connect=foundation.reader)]}


def answer_ask(ask_id: str, body) -> dict:
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
        try:
            answers = owner_asks.answers_for(ask, body)
        except (ValueError, TypeError) as e:
            raise control.Refused(str(e)) from None
        given = owner_asks.secret_values(ask, body)
        if problems := [f"{name}: {why}" for name, value in given.items()
                        if (why := runner.reserved(name) or vault.unfit(value))]:
            raise control.Refused("; ".join(problems) + "; nothing was stored")
        free_to_start(cfg, repo, tid, "; the answer was not stored", ahead=True)
        command = continuing(cfg, repo, row)
        try:
            vault.put_all(repo.name, given)
        except OSError as e:
            raise control.Refused(f"could not store {', '.join(given)} ({e.strerror}); the ask stays open") from None
        if not ask_store.ask_close(ask_id, "answered", answers):
            raise AlreadyResolved(f"ask {ask_id} was answered from the tracker a moment ago, so this answer was not stored")
        if more := ask_store.asks_of(row["id"], "open"):
            return answer(tid, repo.name, "", "waiting",
                          f"{util.count(len(more), 'more ask')} of run {row['id']} wait for an answer")
        return continue_asked(cfg, repo, row, command)


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
    path = script_path(row) or ("revise" if pipeline.pending_feedback(repo.name, tid) else "feature")
    return command_of(cfg, path, repo.name, tid, "--force", "--continue", row["id"])


def continue_asked(cfg: config.Config, repo: config.Repo, row: dict, command: list[str] | None = None,
                   admission=None) -> dict:
    return launch(cfg, repo, str(row["issue"]), command or continuing(cfg, repo, row), ahead=True,
                  admission=admission)


def kept_folder_removed(repo: config.Repo, tid: str, run_id: str) -> bool:
    try:
        checkouts = [workspace.Checkout.from_row(c)
                     for c in flows.prepared_data(run_id).get("checkouts") or []]
    except (TypeError, KeyError):
        return False
    return workspace.remove_workspace(paths.WORKTREES_DIR / f"{repo.name}-{tid}", checkouts)


def let_go(cfg: config.Config, repo: config.Repo, row: dict, status: str) -> dict:
    tid, run_id = str(row["issue"]), row["id"]
    name = tracker.issue_name(repo.name, tid)
    with util.lock(run_store.run_lock("pipeline", repo.name, tid)) as got:
        if not got:
            raise control.Busy(f"a run of {name}")
        if status == "stopped" and not record.move(run_id, "stopped", expect=("needs-info",), summary=record.STOPPED):
            raise control.Refused(f"run {run_id} is no longer waiting for an answer, so it was not stopped")
        if status == "closed" and not record.move(run_id, "stopped", expect=("needs-info",), summary=TICKET_CLOSED):
            raise control.Refused(f"run {run_id} is no longer waiting for an answer, so it was not stopped")
        ask_store.asks_retire(run_id, "stopped" if status == "closed" else status)
        latest = not run_store.later_run(repo.name, tid, run_id)
        removed = latest and kept_folder_removed(repo, tid, run_id)
        folder = "its work folder is removed" if removed else "its work folder was left in place"
        if status == "closed":
            pipeline.write_report(run_store.run_get(run_id) or row)
            return answer(tid, repo.name, run_id, "stopped", f"run {run_id} stopped because {name} closed; {folder}")
        run = pipeline.Run(run_id, repo, paths.RUNS_DIR / run_id, tracker=tracker.for_ticket(cfg, repo, tid), tid=tid)
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
        if not workspace.ticket_closed(cfg, repo.name, tid, row["status"]):
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


def resume(run_id: str, step: str = "", base_version=None) -> dict:
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = command_run(run_id, base_version)
        if row.get("files_purged"):
            raise control.Refused(f"run {run_id} files were removed on {row['files_purged']}; "
                                  "start the ticket again with POST /api/tasks")
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
        if row["status"] not in RESUMABLE:
            raise control.Refused(f"run {run_id} ended {row['status']}; a run resumes only when it ended "
                                  f"{', '.join(RESUMABLE[:-1])} or {RESUMABLE[-1]}")
        no_later_run(row)
        if script_path(row) == "orchestrate":
            if step:
                adaptive_action(row, step)
            return launch(cfg, repo, str(row["issue"]), command_of(
                cfg, "orchestrate", repo.name, str(row["issue"]), "--force", "--continue", run_id,
                *(["--action", step] if step else [])), admission=(run_id, base_version))
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
                                                 *(["--from", step] if step else [])),
                      admission=(run_id, base_version))


def repair(run_id: str, base_version=None) -> dict:
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = command_run(run_id, base_version)
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
        path = "orchestrate" if script_path(row) == "orchestrate" else "repair"
        if refusal := pipeline.script_refusal(cfg, repo, tid, pipeline.Script(path, None, repairs=run_id)):
            raise control.Refused(refusal)
        return launch(cfg, repo, tid, command_of(cfg, path, repo.name, tid, "--force", "--repair", run_id),
                      admission=(run_id, base_version))


def adaptive_action(row: dict, action: str) -> dict:
    from .store import orchestration

    source = orchestration.find(row["id"])
    if source:
        lease = orchestration.Lease(source["root"], source["current_run"], source["generation"])
        found = next((a for a in orchestration.actions(lease) if a["id"] == action), None)
        if found and found["op"] in ("worker.start", "worker.continue", "checks.run", "verification.run", "review.run"):
            return found
    raise control.Refused("adaptive controls require a worker/check/review/verification action ID from run details, "
                          "not a phase name or publication action")


def run_step(run_id: str, step: str, sets, base_version=None) -> dict:
    with util.lock(STEER_LOCK, wait=True):
        cfg, repo, row = command_run(run_id, base_version)
        if row["status"] == "running":
            raise control.Refused(f"run {run_id} is running; a step runs alone only on a run that ended")
        if not (path := script_path(row)):
            raise control.Refused(f"run {run_id} did not run from a script in adws/, so a step cannot run alone on it")
        if path == "orchestrate":
            no_later_run(row)
            adaptive_action(row, step)
            if why := flows.action_workspace_refusal(run_id):
                raise control.Refused(why)
            if sets:
                raise control.Refused("adaptive action reruns keep their accepted settings; select new settings through a resumed controller")
            return launch(cfg, repo, str(row["issue"]), command_of(
                cfg, "orchestrate", repo.name, str(row["issue"]), "--force", "--continue", run_id,
                "--action", step, "--only-action"), admission=(run_id, base_version))
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
        return launch(cfg, repo, tid, command_of(cfg, "step", run_id, step, *settings), admission=(run_id, base_version))


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
    if tracker.is_task_id(tid):
        tid = tid.lower()
        if (row := task_store.task_get(tid, connect=foundation.reader)) is None:
            raise control.NotFound(f"no task {tid}")
        item, repo, refs = {**row, "comments": task_store.task_comments(tid, connect=foundation.reader)}, row["repo"], [tid]
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
