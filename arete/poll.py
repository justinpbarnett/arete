from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from . import (
    config,
    environment_gaps,
    flows,
    intake,
    jira,
    launch_lease,
    oncall,
    outcomes,
    owner_asks,
    patching,
    paths,
    pilot,
    pipeline,
    queueing,
    requeue,
    routing,
    runlimits,
    steps,
    tracker,
    util,
    zte,
)
from .agent import REPLAY
from .heartbeat import mark_loop, network_up_or_skip
from .pipeline import (
    held_back,
    live_pipeline_runs,
    pending_feedback,
    repaired_by,
    retry_scratch,
    runnable,
    settle_abandoned_runs,
    spawn_run,
    sync_labels,
)
from .slots import slots_free_to_start
from .store import asks as ask_store
from .store import db
from .store import insights as insights_store
from .store import runs as run_store
from .store import sources as source_store
from .workspace import cleanup_worktrees

MAX_STARTS_PER_POLL = 64
SECRET_ASK_REPLY = "its ask holds a secret, which a tracker reply cannot answer; answer it on the dashboard"


@dataclass(frozen=True)
class Pick:
    tid: str
    why: str
    from_queue: bool = False
    from_feedback: bool = False


def ticket_queues(repo: config.Repo, tk: tracker.Tracker) -> list[tracker.Tracker]:
    if isinstance(tk, tracker.LocalTracker):
        return [tk]
    return [*([tk] if repo.parts or isinstance(tk, tracker.JiraTracker) else []), tracker.LocalTracker(repo)]


def next_ticket(repo: config.Repo, tk: tracker.Tracker, skips: pipeline.SkipLog,
                other_routes: set[str] = frozenset(), excluded: set[str] = frozenset(),
                recovered: tuple[tracker.Ticket, ...] = ()) -> tracker.Ticket | None:
    for source in ticket_queues(repo, tk):
        listed = source.todo(parked=lambda tid: bool(runlimits.latest_park(repo.name, tid)[1]))
        known = {str(t.id) for t in listed}
        extra = [t for t in recovered if str(t.id) not in known] if source is tk else []
        open_tickets = source.open_agent_tickets if isinstance(source, tracker.GitHubTracker) else ()
        for t in queueing.prioritized([*listed, *extra], open_tickets=open_tickets):
            tid = str(t.id)
            if (tid in excluded or other_routes & set(t.labels) or run_store.issue_running(repo.name, t.id)
                    or not pilot.pickable(tid, repo.name)
                    or (zte.reserved(tid) and not (zte.scheduled_poll() and zte.pickable(tid, repo.name)))):
                continue
            if owed := run_store.pending_label(repo.name, t.id):
                skips.note(tracker.ticket_name(repo.name, t.ref), t, f"its {owed} label is not written yet", repo.name)
                continue
            if blocker := source.blocker(t.id):
                skips.note(tracker.ticket_name(repo.name, t.ref), t, blocker, repo.name)
                continue
            return t
    return None


def jira_routes_by_project(cfg: config.Config) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in cfg.repos:
        if r.pipeline and runnable(r) and r.tracker == "jira":
            out.setdefault(r.jira_project or cfg.jira.project, set()).add(tracker.route_label(r))
    return out


def next_pick(repo: config.Repo, tk: tracker.Tracker, skips: pipeline.SkipLog,
              other_routes: set[str], excluded: set[str] = frozenset(),
              recovered: tuple[tracker.Ticket, ...] = ()) -> Pick | None:
    excluded = excluded | oncall.parked(repo.name)
    fresh = next_ticket(repo, tk, skips, other_routes, excluded, recovered)
    def eligible_again(tid):
        return (str(tid) not in excluded and not pilot.is_pilot_task(str(tid)) and not zte.reserved(str(tid))
                and not run_store.issue_running(repo.name, tid))

    def ordinary_history(tid):
        return eligible_again(tid) and (not isinstance(tk, tracker.GitHubTracker) or tracker.is_task_id(tid)
                                        or not tk.in_zte_queue(str(tid)))

    for tid in run_store.interrupted_tickets(repo.name):
        if (ordinary_history(tid) and (row := run_store.newest_live_whole_run(repo.name, tid))
                and not environment_gaps.was_retry(row)):
            return Pick(tid, "restarting after an interruption")
    for tid in insights_store.feedback_due(repo.name, util.iso(util.now() - timedelta(minutes=pipeline.FEEDBACK_QUIET_MINUTES))):
        if ordinary_history(tid):
            return Pick(tid, "revising the PR for your review comments", from_feedback=True)
    for source in ticket_queues(repo, tk):
        for t in source.replied():
            if not eligible_again(t.id):
                continue
            if other_routes & set(t.labels):
                skips.note(t.ref, t, tracker.route_blocker(other_routes | {tracker.route_label(repo)}, t.labels))
                continue
            if blocker := source.blocker(t.id):
                skips.note(tracker.ticket_name(repo.name, t.ref), t, blocker, repo.name)
                continue
            return Pick(t.id, f"{t.title} (you replied to the question)")
    return Pick(fresh.id, fresh.title, from_queue=True) if fresh else None


def step_without_route(cfg: config.Config, repo: config.Repo, ticket: tracker.Ticket,
                  discovery: routing.Discovery, layer: config.RepoLayer) -> str:
    if cfg.replay.all:
        return ""
    mode = pipeline.dispatch_path(repo, str(ticket.id))
    adaptive = mode in (pipeline.ORCHESTRATED, "orchestrate")
    first = ("orchestrate", "build") if adaptive else ("plan", "build")
    if (not adaptive and layer.factory_path == pipeline.ADW and len(repo.parts) <= 1
            and patching.requested(layer, ticket, automatic=cfg.triage.route_fixes)
            and not patching.specification_refusal(ticket)):
        first = ("build",)
    newest = run_store.newest_whole_run(repo.name, ticket.id)
    if (layer.factory_path == pipeline.ADW and newest and newest["status"] == "interrupted"
            and repaired_by(newest["id"])):
        first = ("review_repair",)
    elif pending_feedback(repo.name, ticket.id):
        source = next(iter(run_store.open_runs(repo.name, ticket.id)), None)
        if not source or pipeline.recorded_path(source["id"]) not in (pipeline.ORCHESTRATED, "orchestrate"):
            first = ("build",)
    for role in (*first, *(("verify",) if layer.verify else ()), "review"):
        setting = steps.resolve(cfg, layer, role, ticket.kind, ticket=(repo.name, str(ticket.id)))
        if setting.runner == REPLAY:
            continue
        routes = discovery.routes(setting)
        if not any(route.state == "ready" for route in routes):
            reason = routing.held_words(routes) or "; ".join(dict.fromkeys(route.detail for route in routes))
            return f"{role} has no eligible route" + (f": {reason}" if reason else "")
    return ""


def tracker_reply(repo: config.Repo, tk: tracker.Tracker, tid: str, dry_run: bool, *,
                  from_queue: bool, from_feedback: bool) -> str:
    newest = run_store.newest_live_whole_run(repo.name, tid)
    if not newest or newest["status"] != "needs-info":
        return ""
    run_id = newest["id"]
    asks = ask_store.asks_of(run_id)
    waiting = [a for a in asks if a["status"] == "open"]
    if any(owner_asks.has_secret(a["questions"]) for a in waiting):
        return SECRET_ASK_REPLY
    if dry_run:
        return ""
    if waiting:
        since = waiting[0]["asked"]
    elif not asks:
        since = pipeline.question_time(newest)
    elif all(a["status"] == "expired" and a["answered"] and not owner_asks.has_secret(a["questions"]) for a in asks):
        since = max(a["answered"] for a in asks)
    else:
        return ""
    if not waiting and pipeline.answer_text(run_id):
        return ""
    text = tk.reply_since(tid, since) if since else ""
    if not waiting and not text:
        return ""
    with util.lock(owner_asks.STEER_LOCK, wait=True), db.tx():
        current = run_store.newest_live_whole_run(repo.name, tid)
        if not current or current["id"] != run_id or current["status"] != "needs-info":
            return "its waiting run changed during tracker reply lookup"
        current_asks = ask_store.asks_of(run_id)
        if waiting:
            open_asks = [a for a in current_asks if a["status"] == "open"]
            if any(owner_asks.has_secret(a["questions"]) for a in open_asks):
                return SECRET_ASK_REPLY
            waiting_ids = {a["id"] for a in waiting}
            if not text and from_feedback and open_asks and (latest_ask := open_asks[-1]["asked"]):
                text = "\n\n".join(body for row in pending_feedback(repo.name, tid)
                                   if row["status"] == "new" and (row["created"] or "") > latest_ask
                                   and (body := (row["body"] or "").strip()))
            if text:
                for a in open_asks:
                    if a["id"] in waiting_ids:
                        ask_store.ask_close(a["id"], "answered", owner_asks.text_answers(a, text))
            elif from_queue and current_asks == asks:
                return ""
            if ask_store.asks_of(run_id, "open"):
                return "its asks are still waiting for an owner answer"
            if any(a["status"] == "expired" for a in current_asks if a["id"] in waiting_ids):
                return "its asks expired during tracker reply lookup"
        elif current_asks != asks:
            return "its asks changed during tracker reply lookup"
        elif not pipeline.answer_text(run_id):
            util.write_bytes_atomic(paths.RUNS_DIR / run_id / pipeline.ANSWER_FILE, text.encode())
    return ""


def spawn_unparked(repo_name: str, tid: str | int, readmission: runlimits.Readmission | None = None) -> bool | None:
    """Return True for a spawn, None for existing work, False for an oncall refusal."""
    from . import tasks

    with util.lock("oncall") as got:
        if not got or str(tid) in oncall.parked(repo_name):
            return False
        with tasks.PENDING_LOCK, util.lock(tasks.START_LOCK, wait=True):
            if tasks.pending_start(repo_name, str(tid)) or run_store.issue_running(repo_name, tid):
                return None
            def spawn():
                lease = launch_lease.CURRENT.get()
                if zte.reserved(str(tid)):
                    zte.bind_launch(repo_name, str(tid), lease)
                if readmission:
                    lease.readmission = json.dumps(asdict(readmission))
                    readmission.attempted(record=True)
                return spawn_run(repo_name, tid)

            tasks.spawn_pending(repo_name, str(tid), spawn)
            return True


def failed(errors: list[str], where: str, error: Exception) -> None:
    errors.append(f"{where}: {type(error).__name__}: {error}")
    print(f"{util.iso()} {errors[-1]}")


def guard(errors: list[str], where: str, step: Callable[[], None]) -> None:
    try:
        step()
    except Exception as e:
        failed(errors, where, e)


@dataclass
class RepoQueue:
    repo: config.Repo
    tracker: tracker.Tracker
    other_routes: set[str]
    layer: config.RepoLayer
    discovery: routing.Discovery
    selected: set[str] = field(default_factory=set)
    recovered: tuple[tracker.Ticket, ...] = ()


def requeue_failures(cfg: config.Config, queue: RepoQueue, dry_run: bool, errors: list[str]) -> None:
    source = queue.tracker
    if not isinstance(source, tracker.GitHubTracker) or not queue.repo.parts:
        return
    if not dry_run:
        for entry in run_store.unfinished_environment_requeues(queue.repo.name):
            guard(errors, tracker.issue_name(queue.repo.name, entry["issue"]), lambda: requeue.reconcile(source, entry))
    recovered = []
    try:
        for issue in source.failed_queue():
            try:
                if ticket := requeue.reconsider(cfg, source, queue.layer, issue, dry_run):
                    recovered.append(ticket)
            except Exception as e:
                failed(errors, tracker.issue_name(queue.repo.name, issue), e)
    finally:
        if not dry_run:
            queue.selected.update(entry["issue"] for entry in run_store.unfinished_environment_requeues(queue.repo.name)
                                  if entry["edit_pending"])
    queue.recovered = tuple(recovered)


def repo_queues(cfg: config.Config, pipeline_repos: list[config.Repo], routes: dict[str, set[str]],
                skips: pipeline.SkipLog, errors: list[str]) -> tuple[list[RepoQueue], dict[str, jira.Client]]:
    starts = run_store.last_pipeline_starts()
    restarting = {r.name for r in pipeline_repos if run_store.interrupted_tickets(r.name)}
    queues, reached_clients = [], {}
    for repo in sorted(pipeline_repos, key=lambda r: (r.name not in restarting, starts.get(r.name, ""))):
        try:
            tk = tracker.for_repo(cfg, repo)
            other_routes: set[str] = set()
            if isinstance(tk, tracker.JiraTracker):
                other_routes = routes.get(tk.project, set()) - {tk.route}
                reached_clients.setdefault(tk.project, tk.client)
            layer = config.load_layer(repo.layer_dir)
            queues.append(RepoQueue(repo, tk, other_routes, layer,
                                    routing.Discovery(cfg, repo, priced=False, layer=layer)))
        except Exception as e:
            failed(errors, repo.name, e)
            skips.keep_repo(repo.name)
    return queues, reached_clients


def start_pick(cfg: config.Config, queue: RepoQueue, pick: Pick, skips: pipeline.SkipLog,
               dry_run: bool, going: Counter, slots: int) -> str:
    from . import tasks

    repo, layer = queue.repo, queue.layer
    tid = pick.tid
    if not pilot.pickable(str(tid), repo.name) or (
            zte.reserved(str(tid)) and not (zte.scheduled_poll() and zte.pickable(str(tid), repo.name))):
        return "skipped"
    with tasks.PENDING_LOCK, (nullcontext() if dry_run else util.lock(tasks.START_LOCK, wait=True)):
        if tasks.pending_start(repo.name, str(tid)) or run_store.issue_running(repo.name, tid):
            return "skipped"
    source = tracker.for_ticket(cfg, repo, tid)
    name = tracker.ticket_name(repo.name, source.ref(tid))
    if isinstance(source, tracker.GitHubTracker) and source.in_zte_queue(str(tid)):
        return "skipped"
    reply = tracker_reply(repo, source, str(tid), dry_run, from_queue=pick.from_queue, from_feedback=pick.from_feedback)
    answering = not reply and pipeline.answered_run(repo.name, str(tid)) is not None
    if not answering and not reply and (waiting := held_back(cfg, repo.name, going, slots)):
        if not dry_run:
            source_store.pass_over(repo.name, str(tid))
        print(f"{util.iso()} {name}: waiting, {waiting}")
        return "held"
    ticket = source.get(tid)
    blocked, readmission = runlimits.poll_admission(repo, str(tid), ticket, runlimits.Policy.of(layer))
    blocked = blocked or source.blocker(tid) or reply
    if not blocked:
        newest = run_store.newest_whole_run(repo.name, tid)
        if not (newest and newest["status"] == "interrupted" and flows.pending_catch_up(newest["id"])):
            if not zte.reserved(str(tid)):
                ticket = intake.route_ticket(cfg, repo, source, ticket, dry_run=dry_run)
            blocked = step_without_route(cfg, repo, ticket, queue.discovery, layer)
    if blocked:
        skips.note(name, ticket, blocked, repo.name)
        print(f"{util.iso()} {name}: {blocked}")
        return "blocked"
    print(f"{util.iso()} {name}: {pick.why}" + (" (dry run, not started)" if dry_run else ""))
    if not dry_run:
        source_store.drop_passed_over(repo.name, keep=str(tid))
        spawned = spawn_unparked(repo.name, tid, readmission)
        if spawned is None:
            return "skipped"
        if not spawned:
            return "refused"
    return "started"


def fill_slots(cfg: config.Config, queues: list[RepoQueue], skips: pipeline.SkipLog, dry_run: bool,
               errors: list[str], going: Counter, slots: int) -> bool:
    examined, exhausted = 0, set()
    selected_work = False
    while examined < MAX_STARTS_PER_POLL:
        progressed = False
        for queue in queues:
            repo = queue.repo
            if repo.name in exhausted or examined >= MAX_STARTS_PER_POLL:
                continue
            try:
                pick = next_pick(repo, queue.tracker, skips, queue.other_routes, queue.selected, queue.recovered)
                if not pick:
                    exhausted.add(repo.name)
                    if not dry_run:
                        source_store.drop_passed_over(repo.name)
                    continue
                queue.selected.add(str(pick.tid))
                examined += 1
                progressed = True
                result = start_pick(cfg, queue, pick, skips, dry_run, going, slots)
                if result in ("held", "refused"):
                    exhausted.add(repo.name)
                elif result == "started":
                    selected_work = True
                    slots -= 1
                    going[repo.name] += 1
            except Exception as e:
                failed(errors, repo.name, e)
                skips.keep_repo(repo.name)
                exhausted.add(repo.name)
        if not progressed:
            break
    return selected_work


def resume_environment_holds(cfg: config.Config, queues: list[RepoQueue], dry_run: bool,
                             errors: list[str], going: Counter, slots: int) -> int:
    started = 0
    for queue in queues:
        repo = queue.repo
        for row in run_store.environment_holds(repo.name):
            try:
                can_start = started < MAX_STARTS_PER_POLL and not held_back(cfg, repo.name, going, slots - started)
                if environment_gaps.handle(cfg, repo, row, dry_run, can_start=can_start):
                    queue.selected.add(str(row["issue"]))
                    going[repo.name] += 1
                    started += 1
            except Exception as error:
                failed(errors, row["id"], error)
    return started


def report_unrouted(routes: dict[str, set[str]], reached_clients: dict[str, jira.Client],
                    skips: pipeline.SkipLog, errors: list[str]) -> None:
    for project, client in reached_clients.items():
        try:
            for t in tracker.jira_unrouted(project, routes.get(project, set()), client):
                skips.note(t.ref, t, t.blocker)
        except Exception as e:
            failed(errors, f"jira {project}", e)
            skips.keep_project(project)
    for project in routes.keys() - reached_clients.keys():
        skips.keep_project(project)


def save_skips_and_landings(cfg: config.Config, skips: pipeline.SkipLog,
                           sync_landings: Callable[[config.Config], None]) -> None:
    util.write_json(pipeline.SKIPPED_FILE, skips.current)
    sync_landings(cfg)


def poll(dry_run: bool = False) -> int:
    if not dry_run:
        paths.ensure_dirs()
    cfg = config.load()
    with (nullcontext(True) if dry_run else util.lock("poll")) as got:
        if not got:
            print(f"{util.iso()} poll already running")
            return 0
        if not dry_run:
            retry_scratch(cfg.retention.days)
        if any(r.parts or r.tracker != "local" for r in cfg.repos) and not network_up_or_skip("poll", dry_run):
            return 0
        errors: list[str] = []
        if not dry_run:
            guard(errors, "settling abandoned runs", lambda: settle_abandoned_runs(cfg))
            guard(errors, "syncing PR outcomes", lambda: outcomes.sync_outcomes(cfg))
            guard(errors, "labeling tickets", lambda: sync_labels(cfg))
        going = Counter(r["repo"] for r in live_pipeline_runs())
        slots = slots_free_to_start(cfg.max_parallel)
        pipeline_repos = [r for r in cfg.repos if r.pipeline and runnable(r)]
        skips = pipeline.SkipLog({} if dry_run else util.read_json(pipeline.SKIPPED_FILE, {}))
        routes = jira_routes_by_project(cfg)
        queues, reached_clients = repo_queues(cfg, pipeline_repos, routes, skips, errors)
        for queue in queues:
            guard(errors, f"{queue.repo.name} environment requeues", lambda: requeue_failures(cfg, queue, dry_run, errors))
        resumed = 0
        try:
            resumed = resume_environment_holds(cfg, queues, dry_run, errors, going, slots)
        except Exception as error:
            failed(errors, "environment holds", error)
        selected_work = fill_slots(cfg, queues, skips, dry_run, errors, going, slots - resumed) or resumed > 0
        report_unrouted(routes, reached_clients, skips, errors)
        if not dry_run:
            guard(errors, "moving tickets", lambda: outcomes.sync_moves(cfg))
            guard(errors, "poll", lambda: save_skips_and_landings(cfg, skips, outcomes.sync_landings))
            guard(errors, "sweeping worktrees", lambda: cleanup_worktrees(cfg=cfg))
            mark_loop("poll", not errors, "; ".join(errors))
        elif not errors and not selected_work:
            print(f"{util.iso()} poll dry run: no new run would start")
        return 1 if errors else 0
