from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from . import (
    cohorts,
    config,
    environment_gaps,
    flows,
    gh,
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
    pr_recovery,
    private_evidence,
    queueing,
    record,
    requeue,
    retention,
    routing,
    run_lines,
    run_timing,
    runlimits,
    spec_delivery,
    spec_graph,
    steps,
    tracker,
    util,
    zte,
)
from .agent import REPLAY
from .heartbeat import mark_loop, network_up_or_skip
from .pipeline import (
    held_back,
    line_held_back,
    live_pipeline_runs,
    pending_feedback,
    retry_scratch,
    runnable,
    settle_abandoned_runs,
    spawn_run,
    sync_labels,
)
from .slots import slots_free_to_start
from .store import asks as ask_store
from .store import db
from .store import evidence as evidence_store
from .store import feedback as feedback_store
from .store import insights as insights_store
from .store import orchestration as orchestration_store
from .store import runs as run_store
from .store import sources as source_store
from .store import specs as spec_store
from .store import tasks as task_store

MAX_STARTS_PER_POLL = 64
FEEDBACK_PICK = "revising the PR for your review comments"
TICKET_FEEDBACK_PICK = "addressing authorized ticket feedback"
CLARIFICATION_WAIT = "waiting for an authorized product clarification in the ticket thread"
PRIVATE_EVIDENCE_WAIT = private_evidence.PRIVATE_EVIDENCE_WAIT
PRIVATE_EVIDENCE_PICK = "resuming after private evidence resolution in Triage"
SECRET_ASK_REPLY = "its ask holds a secret, which a tracker reply cannot answer; answer it on the dashboard"


@dataclass(frozen=True)
class Pick:
    tid: str
    why: str
    from_queue: bool = False
    from_feedback: bool = False
    from_ticket_feedback: bool = False
    from_private_evidence: bool = False


def ticket_queues(repo: config.Repo, tk: tracker.Tracker) -> list[tracker.Tracker]:
    if isinstance(tk, tracker.LocalTracker):
        return [tk]
    return [*([tk] if repo.parts or isinstance(tk, tracker.JiraTracker) else []), tracker.LocalTracker(repo)]


def _conflicts_other_routes(repo: config.Repo, source: tracker.Tracker,
                            ticket: tracker.Ticket, other_routes: set[str]) -> bool:
    hits = other_routes & set(ticket.labels)
    if not hits:
        return False
    if ticket.is_spec and (enrolled := getattr(source, "enrolled_repos", ())):
        from . import cross_repo

        authorized, error = cross_repo.authorized_repos(config.Config(repos=list(enrolled)), repo, ticket)
        if not error and len(authorized) > 1 and authorized[0].name == repo.name:
            allowed_routes = {tracker.route_label(r) for r in authorized}
            if hits <= allowed_routes:
                return False
    return True


def fresh_candidates(repo: config.Repo, tk: tracker.Tracker, skips: pipeline.SkipLog,
                     other_routes: set[str], excluded: set[str],
                     recovered: tuple[tracker.Ticket, ...], observe) -> Iterator[tracker.Ticket]:
    for source in ticket_queues(repo, tk):
        def retry_failed(tid: str, *, source: tracker.Tracker = source) -> bool:
            if runlimits.latest_park(repo.name, tid)[1]:
                return True
            if (isinstance(source, tracker.GitHubTracker)
                    or not requeue.policy_retry(run_store.newest_live_whole_run(repo.name, tid))):
                return False
            now = util.now()
            since = util.iso(now - requeue.WINDOW)
            failures = run_store.failed_ticket_runs_since(repo.name, tid, since)
            if sum(requeue.recorded_failure(run).get("reason") == "policy-changed" for run in failures) <= requeue.LIMIT:
                return True
            print(f"{util.iso(now)} {tracker.issue_name(repo.name, tid)}: "
                  f"kept failed after {requeue.LIMIT} policy requeues in 24 hours")
            return False

        listed = source.todo(parked=retry_failed)
        known = {str(t.id) for t in listed}
        extra = [t for t in recovered if str(t.id) not in known] if source is tk else []
        open_tickets = source.open_agent_tickets if isinstance(source, tracker.GitHubTracker) else ()
        if observe:
            observe(source, list({t.id: t for t in (*listed, *extra, *open_tickets)}.values()))
        for t in queueing.prioritized([*listed, *extra], open_tickets=open_tickets):
            tid = str(t.id)
            if (tid in excluded or _conflicts_other_routes(repo, source, t, other_routes)
                    or run_store.issue_running(repo.name, t.id)
                    or not pilot.pickable(tid, repo.name)
                    or (zte.reserved(tid) and not (zte.scheduled_poll() and zte.pickable(tid, repo.name)))):
                continue
            if not isinstance(source, tracker.LocalTracker):
                claim = spec_store.find(repo.name, tid)
                if claim and (not t.is_spec or claim["state"] != "building"):
                    continue
            if owned := spec_graph.queue_blocker(source, tid):
                skips.note(tracker.ticket_name(repo.name, t.ref), t, owned, repo.name)
                continue
            if isinstance(source, tracker.GitHubTracker):
                from .store import outbox as outbox_store

                if item_id := outbox_store.pending_label_item(f"github:{source.repo.github}#{int(t.id)}"):
                    skips.note(
                        tracker.ticket_name(repo.name, t.ref),
                        t,
                        f"its label item OB-{item_id} is not sent yet",
                        repo.name,
                    )
                    continue
            elif isinstance(source, tracker.JiraTracker):
                from .store import outbox as outbox_store

                if item_id := outbox_store.pending_label_item(f"jira:{t.id}"):
                    skips.note(
                        tracker.ticket_name(repo.name, t.ref),
                        t,
                        f"its label item OB-{item_id} is not sent yet",
                        repo.name,
                    )
                    continue
            elif owed := run_store.pending_label(repo.name, t.id):
                skips.note(tracker.ticket_name(repo.name, t.ref), t, f"its {owed} label is not written yet", repo.name)
                continue
            if feedback_store.has_open_clarification(repo.name, tid):
                skips.note(tracker.ticket_name(repo.name, t.ref), t, CLARIFICATION_WAIT, repo.name)
                continue
            blocker, blocker_kind = tracker.blocker_of(source, t.id)
            if blocker:
                skips.note(tracker.ticket_name(repo.name, t.ref), t, blocker, repo.name, blocker_kind=blocker_kind)
                continue
            yield t


def next_ticket(repo: config.Repo, tk: tracker.Tracker, skips: pipeline.SkipLog,
                other_routes: set[str] = frozenset(), excluded: set[str] = frozenset(),
                recovered: tuple[tracker.Ticket, ...] = (), observe=None) -> tracker.Ticket | None:
    return next(fresh_candidates(repo, tk, skips, other_routes, excluded, recovered, observe), None)


def jira_routes_by_project(cfg: config.Config) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in cfg.repos:
        if r.pipeline and runnable(r) and r.tracker == "jira":
            out.setdefault(r.jira_project or cfg.jira.project, set()).add(tracker.route_label(r))
    return out


def priority_pick(repo: config.Repo, tk: tracker.Tracker, skips: pipeline.SkipLog,
                  other_routes: set[str], excluded: set[str]) -> Pick | None:
    def eligible_again(tid):
        return (str(tid) not in excluded and not pilot.is_pilot_task(str(tid)) and not zte.reserved(str(tid))
                and not run_store.issue_running(repo.name, tid) and not spec_graph.queue_blocker(tk, tid))

    def ordinary_history(tid):
        return eligible_again(tid) and (not isinstance(tk, tracker.GitHubTracker) or tracker.is_task_id(tid)
                                        or not tk.in_zte_queue(str(tid)))

    quiet_since = util.iso(util.now() - timedelta(minutes=pipeline.FEEDBACK_QUIET_MINUTES))
    feedback_due = insights_store.feedback_due(repo.name, quiet_since)
    for tid in run_store.interrupted_tickets(repo.name):
        if (ordinary_history(tid) and (row := run_store.newest_live_whole_run(repo.name, tid))
                and not environment_gaps.was_retry(row)):
            if pipeline.recorded_path(row["id"]) not in ("feature", "patch"):
                pending = pending_feedback(repo.name, tid)
                if tid in feedback_due or insights_store.feedback_turn_due(pending, quiet_since):
                    return Pick(tid, FEEDBACK_PICK, from_feedback=True)
                if insights_store.has_new_feedback(pending):
                    excluded.add(tid)
                    continue
            return Pick(tid, "restarting after an interruption")
    for tid in spec_store.answering(repo.name):
        if eligible_again(tid) and pipeline.answered_run(repo.name, tid):
            return Pick(tid, "the spec owner answered the question")
    for tid in ask_store.ready_runner_tickets(repo.name):
        if ordinary_history(tid):
            source = tracker.LocalTracker(repo) if tracker.is_task_id(tid) else tk
            ticket = source.get(tid)
            if ticket.open and not ticket.blocker and not other_routes.intersection(ticket.labels):
                return Pick(tid, "the owner confirmed the shared check runner is ready")
    for tid in feedback_due:
        if ordinary_history(tid):
            return Pick(tid, FEEDBACK_PICK, from_feedback=True)
    for tid in insights_store.feedback_due_for_open_asks(repo.name, quiet_since):
        if ordinary_history(tid):
            return Pick(tid, FEEDBACK_PICK, from_feedback=True)
    for tid in feedback_store.tickets_needing_worker(repo.name):
        if ordinary_history(tid):
            return Pick(tid, TICKET_FEEDBACK_PICK, from_ticket_feedback=True)
    for tid in evidence_store.tickets_ready_to_resume(repo.name):
        if eligible_again(tid):
            return Pick(tid, PRIVATE_EVIDENCE_PICK, from_private_evidence=True)
    for source in ticket_queues(repo, tk):
        for t in source.replied():
            if not eligible_again(t.id):
                continue
            if _conflicts_other_routes(repo, source, t, other_routes):
                skips.note(t.ref, t, tracker.route_blocker(other_routes | {tracker.route_label(repo)}, t.labels))
                continue
            blocker, blocker_kind = tracker.blocker_of(source, t.id)
            if blocker:
                skips.note(tracker.ticket_name(repo.name, t.ref), t, blocker, repo.name, blocker_kind=blocker_kind)
                continue
            return Pick(t.id, f"{t.title} (you replied to the question)")
    return None


def pick_candidates(repo: config.Repo, tk: tracker.Tracker, skips: pipeline.SkipLog,
                    other_routes: set[str], excluded: set[str],
                    recovered: tuple[tracker.Ticket, ...], observe) -> Iterator[Pick]:
    from .store import factory

    excluded = set(excluded).union(oncall.parked(repo.name), factory.held_tickets(repo.name))
    if pick := priority_pick(repo, tk, skips, other_routes, excluded):
        yield pick
    for ticket in fresh_candidates(repo, tk, skips, other_routes, excluded, recovered, observe):
        yield Pick(ticket.id, ticket.title, from_queue=True)


def next_pick(repo: config.Repo, tk: tracker.Tracker, skips: pipeline.SkipLog,
              other_routes: set[str], excluded: set[str] = frozenset(),
              recovered: tuple[tracker.Ticket, ...] = (), observe=None) -> Pick | None:
    return next(pick_candidates(repo, tk, skips, other_routes, excluded, recovered, observe), None)


def step_without_route(cfg: config.Config, repo: config.Repo, ticket: tracker.Ticket,
                  discovery: routing.Discovery, layer: config.RepoLayer, *, mode: str = "", repairing=False) -> str:
    if cfg.replay.all:
        return ""
    mode = mode or pipeline.dispatch_path(repo, str(ticket.id))
    adaptive = mode in (pipeline.ORCHESTRATED, "orchestrate")
    overrides: dict[str, dict] = {}
    check_verify = bool(layer.verify or ticket.is_spec)
    check_review = True
    if adaptive:
        first: tuple[str, ...] = ("review_repair",) if repairing else ("orchestrate", "build")
    elif layer.factory_path == pipeline.ADW:
        from . import adw

        routed = adw.route(
            cfg, repo, str(ticket.id),
            repairs="repair" if repairing else "",
            kind=ticket.kind,
            layer=layer,
            read_only=True,
        )
        overrides = routed.overrides
        if routed.adw == "orchestrate":
            first = ("review_repair",) if repairing else ("orchestrate", "build")
        elif routed.loaded is not None:
            first = adw.first_roles(routed.loaded)
            if (routed.adw == "feature" and len(repo.parts) <= 1
                    and patching.requested(layer, ticket, automatic=cfg.triage.route_fixes)
                    and not patching.specification_refusal(ticket)):
                first = ("build",)
            check_verify = check_verify and adw._adw_has_step(routed.loaded.steps, "verify")
            check_review = adw._adw_has_step(routed.loaded.steps, "review")
        else:
            first = ("plan", "build")
    else:
        feedback = pending_feedback(repo.name, ticket.id)
        if repairing:
            first = ("review_repair",)
        elif feedback:
            source = next(iter(task_store.open_pr_runs(repo.name, ticket.id)), None)
            if not source or pipeline.recorded_path(source["id"]) not in (pipeline.ORCHESTRATED, "orchestrate"):
                first = ("build",)
            else:
                first = ("plan", "build")
        else:
            first = ("plan", "build")
    roles = (*first, *(("verify",) if check_verify else ()), *(("review",) if check_review else ()))
    for role in dict.fromkeys(roles):
        own = overrides.get(role)
        inh = overrides.get(config.INHERITS.get(role, ""))
        setting = steps.resolve(cfg, layer, role, ticket.kind, overrides=own, inherited=inh,
                                ticket=(repo.name, str(ticket.id)))
        if setting.runner == REPLAY:
            continue
        routes = discovery.routes(setting)
        if not any(route.state == "ready" for route in routes):
            reason = routing.held_words(routes) or "; ".join(dict.fromkeys(route.detail for route in routes))
            return f"{role} has no eligible route" + (f": {reason}" if reason else "")
    return ""


def tracker_reply(repo: config.Repo, tk: tracker.Tracker, tid: str, dry_run: bool, *,
                  from_queue: bool, from_feedback: bool, spec: bool = False) -> str:
    if evidence_store.has_open(repo.name, tid):
        return PRIVATE_EVIDENCE_WAIT
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
    text = (tk.reply_since(tid, since, strict=True) if spec and isinstance(tk, tracker.JiraTracker)
            else tk.reply_since(tid, since)) if since else ""
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
                        answers = owner_asks.text_answers(owner_asks.unstaged(a), text)
                        ask_store.ask_close(a["id"], "answered", owner_asks.combine_staged(a, answers))
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


def spawn_unparked(repo_name: str, tid: str | int, readmission: runlimits.Readmission | None = None, *,
                   spec: bool = False, source: tracker.Tracker | None = None, continues: str = "",
                   line: str = "") -> bool | None:
    """Return True for a spawn, None for existing work, False for an oncall refusal."""
    from . import configrepo, control, principals, tasks
    from . import lines as lines_mod
    from .store import projects as projects_store

    with util.lock("oncall") as got:
        if not got or str(tid) in oncall.parked(repo_name):
            return False
        with tasks.PENDING_LOCK, util.lock(tasks.START_LOCK, wait=True):
            if ((source and spec_graph.queue_blocker(source, tid)) or tasks.pending_start(repo_name, str(tid))
                    or run_store.issue_running(repo_name, tid)):
                return None
            command = None
            if continues:
                latest = run_store.newest_live_whole_run(repo_name, tid) or {}
                if (latest.get("id") != continues or latest.get("stop_asked")
                        or orchestration_store.publication_pending(continues)):
                    return None
                command = pipeline.adw_command(repo_name, str(tid), "--continue", continues)

            # Hold the instance fence across parent admission revalidation and queued-run insert,
            # releasing both transaction and fence before child launch.
            resolved_line = str(line or "").strip()
            with configrepo.maybe_instance_fence(), db.tx() as c:
                if projects_store.available(c):
                    if not resolved_line:
                        found_line = lines_mod.resolve_line_for_ticket(c, repo_name, tid)
                        if found_line is not None:
                            resolved_line = found_line["id"]
                    if resolved_line:
                        lines_mod.assert_line_active(c, resolved_line)
                with principals.acting("rule:poll"):
                    run_id = pipeline.queue_run(
                        repo_name,
                        str(tid),
                        continues=continues or None,
                        restart="resume" if continues else "",
                        line=resolved_line,
                    )
            if command:
                full_command = [*command, "--run-id", run_id, *(["--line", resolved_line] if resolved_line else [])]
                record.write_launch(run_id, full_command, pipeline.RUNS_LOG)
            else:
                full_command = None

            def spawn():
                lease = launch_lease.CURRENT.get()
                lease.poll = True
                if zte.reserved(str(tid)):
                    zte.bind_launch(repo_name, str(tid), lease)
                if readmission:
                    lease.readmission = json.dumps(asdict(readmission))
                    readmission.attempted(record=True)
                with principals.acting("rule:poll"):
                    return (
                        control.launch(full_command, pipeline.RUNS_LOG)
                        if full_command
                        else pipeline.invoke_spawn(
                            spawn_run,
                            repo_name,
                            tid,
                            **({"spec": True} if spec else {}),
                            run_id=run_id,
                            line=resolved_line,
                        )
                    )

            tasks.spawn_queued(repo_name, str(tid), spawn)
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
            guard(errors, tracker.issue_name(queue.repo.name, entry["issue"]),
                  lambda entry=entry: requeue.reconcile(source, entry))
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
               dry_run: bool, going: Counter, slots: int, wip: Counter | None = None,
               started_lines: list[str] | None = None) -> str:
    from . import tasks, unblock

    repo, layer = queue.repo, queue.layer
    tid = pick.tid
    if not pilot.pickable(str(tid), repo.name) or (
            zte.reserved(str(tid)) and not (zte.scheduled_poll() and zte.pickable(str(tid), repo.name))):
        return "skipped"
    with tasks.PENDING_LOCK, (nullcontext() if dry_run else util.lock(tasks.START_LOCK, wait=True)):
        if tasks.pending_start(repo.name, str(tid)) or run_store.issue_running(repo.name, tid):
            return "skipped"
    newest = run_store.newest_live_whole_run(repo.name, str(tid)) or {}
    held = pr_recovery.blocked(newest)
    if held and (not (pick.from_queue or pick.from_feedback) or repo.name in cfg.unblock_repos
                 or newest.get("stop_asked") or orchestration_store.publication_pending(newest["id"])):
        return "skipped"
    source = tracker.for_ticket(cfg, repo, tid)
    name = tracker.ticket_name(repo.name, source.ref(tid))
    if isinstance(source, tracker.GitHubTracker) and source.in_zte_queue(str(tid)):
        return "skipped"
    if owned := spec_graph.queue_blocker(source, tid, native=True):
        skips.note(name, tracker.Ticket(str(tid), source.ref(tid), ""), owned, repo.name)
        return "skipped"
    ticket = source.get(tid)
    if pick.from_ticket_feedback and not ticket.open:
        return "skipped"
    if feedback_store.has_open_clarification(repo.name, str(tid)):
        skips.note(name, ticket, CLARIFICATION_WAIT, repo.name)
        return "blocked"
    if evidence_store.has_open(repo.name, str(tid)):
        if not private_evidence.probe_restored_for_ticket(repo.name, source, ticket, dry_run=dry_run):
            skips.note(name, ticket, PRIVATE_EVIDENCE_WAIT, repo.name)
            return "blocked"
    claim = (spec_store.find(repo.name, str(tid), identity=ticket.spec_identity)
             if not isinstance(source, tracker.LocalTracker) else {})
    if claim or ticket.is_spec:
        why = tracker.spec_blocker(cfg, source, ticket)
        if claim and (claim["repo"], claim["issue"], claim["identity"]) != (repo.name, str(tid), ticket.spec_identity):
            why = "the spec's original checkout or tracker identity changed"
        if not why and not claim and run_store.newest_live_whole_run(repo.name, tid):
            why = "a spec cannot adopt the ordinary builder's history"
        if why:
            skips.note(name, ticket, why, repo.name)
            return "blocked"
        if claim and (claim["state"] in ("evidenced", "complete")
                      or claim["state"] == "published" and not (pick.from_feedback or pick.from_ticket_feedback)):
            return "skipped"
    continues = ""
    held_feedback = False
    if held:
        if ticket.blocker or queue.other_routes.intersection(ticket.labels) or not unblock.approval(source, ticket)[0]:
            return "skipped"
        if pick.from_feedback:
            attached = unblock.recovery_checkouts(repo, newest)[1]
            feedback = unblock.replies_after_hold(newest, held, pending_feedback(repo.name, str(tid)), attached)
            held_feedback = insights_store.has_fresh_feedback(feedback)
        if not held_feedback:
            queued = (tracker.task_state(tid) in ("todo", "ready") if isinstance(source, tracker.LocalTracker)
                      else tracker.item_state(source, ticket) == "open")
            from .store import outbox as outbox_store

            pending_label = (
                bool(outbox_store.pending_label_item(f"github:{source.repo.github}#{int(tid)}"))
                if isinstance(source, tracker.GitHubTracker)
                else bool(outbox_store.pending_label_item(f"jira:{tid}"))
                if isinstance(source, tracker.JiraTracker)
                else bool(run_store.pending_label(repo.name, tid))
            )
            if (not queued or pending_label or not unblock.recovery_changed(
                    held, unblock.pr_recovery_observation(repo, source, ticket, newest, held))):
                skips.note(name, ticket, unblock.HELD_REPLY_RETRY, repo.name)
                return "skipped"
            continues = newest["id"]
    reply = tracker_reply(repo, source, str(tid), dry_run, from_queue=pick.from_queue,
                          from_feedback=pick.from_feedback and not ticket.is_spec,
                          **({"spec": True} if ticket.is_spec else {}))
    if not reply and claim and ask_store.asks_of(claim["run"], "open"):
        reply = "its asks are still waiting for an owner answer"
    from . import lines as lines_mod

    line_row = lines_mod.resolve_line_read(repo.name, str(tid), cfg=cfg)
    line_id = line_row["id"] if line_row is not None else ""
    if why := config.repo_paused(cfg, repo.name, kind=ticket.kind):
        if not dry_run:
            source_store.pass_over(repo.name, str(tid))
        print(f"{util.iso()} {name}: waiting, {why}")
        return "held"
    answered = pipeline.answered_run(repo.name, str(tid)) if not reply else None
    if answered is None and not reply:
        if line_waiting := line_held_back(line_row, going, wip):
            if not dry_run:
                source_store.pass_over(repo.name, str(tid))
            skips.note(name, ticket, line_waiting, repo.name)
            print(f"{util.iso()} {name}: waiting, {line_waiting}")
            return "line_held"
        if slot_waiting := held_back(cfg, None, going, slots, wip):
            if not dry_run:
                source_store.pass_over(repo.name, str(tid))
            print(f"{util.iso()} {name}: waiting, {slot_waiting}")
            return "held"
    fresh_feedback = False
    if (pick.from_feedback and (not held or held_feedback) and not dry_run
            and insights_store.has_fresh_feedback(pending_feedback(repo.name, tid))):
        mode = ("orchestrate" if ticket.is_spec else pipeline.recorded_path(answered["id"]) if answered
                 else pipeline.dispatch_path(repo, str(tid)))
        fresh_feedback = mode in (pipeline.ORCHESTRATED, "orchestrate")
    if pick.from_ticket_feedback and not held and not dry_run and feedback_store.pending_worker_comments(repo.name, tid):
        fresh_feedback = True
    blocked, readmission = ("", None) if fresh_feedback else runlimits.poll_admission(
        repo, str(tid), ticket, runlimits.Policy.of(layer))
    blocker_kind = ""
    if not blocked:
        if ticket.is_spec:
            blocked, blocker_kind = ticket.blocker, ticket.blocker_kind
        else:
            blocked, blocker_kind = tracker.blocker_of(source, tid)
    if not blocked and reply:
        blocked, blocker_kind = reply, ""
    route_blocked = ""
    if not blocked and continues:
        blocked = route_blocked = step_without_route(cfg, repo, ticket, queue.discovery, layer, mode="orchestrate")
    elif not blocked:
        newest = run_store.newest_whole_run(repo.name, tid)
        if not (newest and newest["status"] == "interrupted" and flows.pending_catch_up(newest["id"])):
            if not zte.reserved(str(tid)):
                ticket = intake.route_ticket(cfg, repo, source, ticket, dry_run=dry_run)
            blocked = route_blocked = step_without_route(cfg, repo, ticket, queue.discovery, layer,
                                                         **({"mode": "orchestrate"} if ticket.is_spec else {}))
    if blocked:
        skips.note(name, ticket, blocked, repo.name, blocker_kind=blocker_kind)
        if route_blocked and not dry_run:
            waiting = answered or run_store.newest_live_whole_run(repo.name, tid) or {}
            if waiting.get("status") == "needs-info" and (answered or pipeline.answer_text(waiting["id"])):
                rid = waiting["id"]
                skips.current[name]["answer_run"] = rid
                if skips.before.get(name, {}).get("answer_run") != rid:
                    skips.current[name]["since"] = util.iso()
        print(f"{util.iso()} {name}: {blocked}")
        return "blocked"
    why = "continuing the held PR after an explicit requeue" if continues else pick.why
    print(f"{util.iso()} {name}: {why}" + (" (dry run, not started)" if dry_run else ""))
    if not dry_run:
        source_store.drop_passed_over(repo.name, keep=str(tid))
        spawned = spawn_unparked(
            repo.name,
            tid,
            readmission,
            source=source,
            **({"spec": True} if ticket.is_spec else {}),
            **({"continues": continues} if continues else {}),
            **({"line": line_id} if line_id else {}),
        )
        if spawned is None:
            return "skipped"
        if not spawned:
            return "refused"
        evidence_store.mark_resumed(repo.name, str(tid))
    if started_lines is not None:
        started_lines.append(line_id or repo.name)
    return "started"


def fill_slots(cfg: config.Config, queues: list[RepoQueue], skips: pipeline.SkipLog, dry_run: bool,
               errors: list[str], going: Counter, slots: int, wip: Counter | None = None) -> bool:
    examined, exhausted, line_held_repos = 0, set(), set()
    selected_work = False
    while examined < MAX_STARTS_PER_POLL:
        progressed = False
        for queue in queues:
            repo = queue.repo
            if repo.name in exhausted or examined >= MAX_STARTS_PER_POLL:
                continue
            try:
                observe = None
                if not dry_run and (repo.name in cfg.unblock_repos or isinstance(queue.tracker, tracker.GitHubTracker)):
                    def observe(source, tickets, *, repo: config.Repo = repo):
                        if repo.name in cfg.unblock_repos:
                            from . import unblock

                            unblock.publish_queue(repo, source, tickets)
                        if isinstance(source, tracker.GitHubTracker):
                            guard(errors, f"{repo.name} dependency discovery",
                                  lambda source=source: environment_gaps.observe_dependencies(cfg, source))

                candidates = pick_candidates(repo, queue.tracker, skips, queue.other_routes, queue.selected,
                                             queue.recovered, observe)
                pick = next(candidates, None)
                if not pick:
                    exhausted.add(repo.name)
                    if not dry_run and repo.name not in line_held_repos:
                        source_store.drop_passed_over(repo.name)
                    continue
                queue.selected.add(str(pick.tid))
                examined += 1
                progressed = True
                started_lines: list[str] = []
                result = start_pick(cfg, queue, pick, skips, dry_run, going, slots, wip, started_lines)
                if result == "line_held":
                    line_held_repos.add(repo.name)
                elif result in ("held", "refused"):
                    exhausted.add(repo.name)
                    if result == "held" and not dry_run:
                        for _ in candidates:
                            pass
                elif result == "started":
                    selected_work = True
                    slots -= 1
                    lid = started_lines[0] if started_lines else repo.name
                    going[lid] += 1
                    if wip is not None:
                        wip[lid] += 1
            except Exception as e:
                failed(errors, repo.name, e)
                skips.keep_repo(repo.name)
                exhausted.add(repo.name)
        if not progressed:
            break
    return selected_work


def resume_environment_holds(cfg: config.Config, queues: list[RepoQueue], dry_run: bool,
                             errors: list[str], going: Counter, slots: int,
                             wip: Counter | None = None) -> int:
    from . import lines as lines_mod

    started = 0
    for queue in queues:
        repo = queue.repo
        for row in run_store.environment_holds(repo.name):
            try:
                if spec_graph.queue_blocker(queue.tracker, row["issue"], native=True):
                    continue
                line_row = lines_mod.resolve_line_read(repo.name, str(row["issue"]), cfg=cfg)
                slot_blocker = config.repo_paused(cfg, repo.name) or held_back(
                    cfg, line_row, going, slots - started, wip,
                )
                can_start = started < MAX_STARTS_PER_POLL and not slot_blocker
                if environment_gaps.handle(cfg, repo, row, dry_run, can_start=can_start, slot_blocker=slot_blocker):
                    queue.selected.add(str(row["issue"]))
                    lid = line_row["id"] if line_row is not None else repo.name
                    going[lid] += 1
                    if wip is not None:
                        wip[lid] += 1
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


def pr_ready(url: str) -> bool | None:
    try:
        pr = gh.pr_merge_details(url, timeout=15, retry=False)
    except (gh.GhError, ValueError):
        return None
    if not isinstance(pr, dict):
        return None
    state, draft = pr.get("state"), pr.get("isDraft")
    if state in ("MERGED", "CLOSED"):
        return True
    return not draft if state == "OPEN" and type(draft) is bool else None

def observe_drafts(cfg: config.Config) -> None:
    repos, states = {repo.name for repo in cfg.repos}, {}
    for row in run_store.ticket_attention_runs():
        if (row["repo"] in repos and not zte.reserved(str(row["issue"]))
                and run_timing.draft_report(row, run_timing.read_log(rid := row["id"]))):
            urls = set(row["pr_url"].split())
            states.update((url, pr_ready(url)) for url in sorted(urls - states.keys()))
            observed = {states[url] for url in urls}
            if observed == {True} or False in observed:
                run_store.note_draft_review(rid, row["pr_url"], ready=observed == {True})

def save_skips(skips: pipeline.SkipLog) -> None:
    now = util.iso()
    for name, old in skips.before.items():
        current = skips.current.get(name, {})
        if ((rid := old.get("answer_run")) and isinstance(since := old.get("since"), str) and since < now
                and (rid, since) != (current.get("answer_run"), current.get("since"))):
            run_lines.write(rid, "wait", event_id=f"provider-wait:{since}", cause="provider", phase="end", since=since, at=now)
    util.write_json(pipeline.SKIPPED_FILE, skips.current)
    from . import inbox

    inbox.sync_source("skipped")


def poll(dry_run: bool = False) -> int:
    from . import factory_control
    from .store import factory

    try:
        with factory_control.activity("poll"):
            return poll_admitted(dry_run)
    except factory.Off as error:
        print(str(error))
        return 0


def poll_admitted(dry_run: bool) -> int:
    if not dry_run:
        paths.ensure_dirs()
    cfg = config.load()
    with (nullcontext(True) if dry_run else util.lock("poll")) as got:
        if not got:
            print(f"{util.iso()} poll already running")
            return 0
        if not dry_run:
            retry_scratch(retention.load_live().records.days)
        if any(r.parts or r.tracker != "local" for r in cfg.repos) and not network_up_or_skip("poll", dry_run):
            return 0
        errors: list[str] = []
        if not dry_run:
            from . import lines as lines_mod

            guard(errors, "ensuring repo lines", lambda: lines_mod.ensure(cfg))
            guard(errors, "capturing cohort arrivals", cohorts.sync)
            guard(errors, "settling abandoned runs", lambda: settle_abandoned_runs(cfg))
            guard(errors, "syncing PR outcomes", lambda: outcomes.sync_outcomes(cfg))
            guard(errors, "observing draft status", lambda: observe_drafts(cfg))
            guard(errors, "syncing spec deliveries", lambda: spec_delivery.sync(cfg))
            guard(errors, "labeling tickets", lambda: sync_labels(cfg))
        from . import lines as lines_mod

        going = lines_mod.live_runs_by_line(live_pipeline_runs())
        wip = lines_mod.wip_counts_by_line()
        slots = slots_free_to_start(cfg.max_parallel)
        pipeline_repos = [r for r in cfg.repos if r.pipeline and runnable(r)]
        skips = pipeline.SkipLog({} if dry_run else util.read_json(pipeline.SKIPPED_FILE, {}))
        routes = jira_routes_by_project(cfg)
        queues, reached_clients = repo_queues(cfg, pipeline_repos, routes, skips, errors)
        for queue in queues:
            guard(errors, f"{queue.repo.name} environment requeues",
                  lambda queue=queue: requeue_failures(cfg, queue, dry_run, errors))
        resumed = 0
        try:
            resumed = resume_environment_holds(cfg, queues, dry_run, errors, going, slots, wip)
        except Exception as error:
            failed(errors, "environment holds", error)
        selected_work = fill_slots(cfg, queues, skips, dry_run, errors, going, slots - resumed, wip) or resumed > 0
        report_unrouted(routes, reached_clients, skips, errors)
        if not dry_run:
            def finish_poll():
                save_skips(skips)
                outcomes.sync_landings(cfg)

            guard(errors, "moving tickets", lambda: outcomes.sync_moves(cfg))
            guard(errors, "poll", finish_poll)
            mark_loop("poll", not errors, "; ".join(errors))
        elif not errors and not selected_work:
            print(f"{util.iso()} poll dry run: no new run would start")
        return 1 if errors else 0
