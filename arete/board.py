from __future__ import annotations

import functools
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from itertools import islice
from pathlib import Path

from . import (
    config,
    control,
    delivery,
    flows,
    loops,
    oncall,
    outbound,
    paths,
    pipeline,
    principals,
    refusals,
    run_lines,
    run_timing,
    sessions,
    slots,
    tracker,
    triage,
    util,
    workspace,
)
from .alerts import issue_link, opened_in
from .store import approvals as approvals_store
from .store import asks as asks_store
from .store import db as foundation
from .store import devices as devices_store
from .store import incidents as incident_store
from .store import insights as insights_store
from .store import loops as loop_store
from .store import orchestration as orchestration_store
from .store import runs as run_store
from .store import sources as source_store
from .store import ticket_progress
from .store import triage as triage_store
from .store import unblock as unblock_store

RECENT_RUNS = 25
TREND_DAYS = 30
LOG_TAIL_LINES = 40
PROPOSALS_SHOWN = 10
LOG_PREFIX = re.compile(r"^(?:\S+ \[[^\]]+\] |\d{4}-\d\d-\d\dT\S+ )")
OUTCOME_OF_STATUS = {"merged": "shipped", "pr": "shipped", "done": "shipped", "failed": "failed"}
STATE_OF_SEVERITY = {"fail": "fault", "warn": "caution"}
STATUSES_OF_FILTER = {"running": ("running",), "failed": ("failed", "interrupted")}
FEED_KINDS = ("loop", "session", "draft")
INCIDENT_NOTES = 500
MOST_RUNS_SHOWN = 500
RUNS_TO_FIND = 40
SESSIONS_TO_FIND = 30
USAGE_DAYS = 30
KEEP_USAGE_DAYS = 90
LANDED = ("landed", "replaced", "approved")
TIMING_FIELDS = ("run_ids", "as_of", "elapsed_seconds", "elapsed_complete", "category_seconds",
                 "unattributed_seconds", "overlap_seconds", "overlap")
TIMING_CATEGORIES = {
    "writing": "Writing", "checks": "Checks / verification", "review": "Review",
    "owner_wait": "Owner wait", "queue_wait": "Queue wait", "retry": "Retry",
}
CHECK_WAIT_BLOCKERS = {"check-slot": "A check slot is unavailable", "full-suite-lock": "The full-suite lock is in use"}
SLOT_QUEUED = "Queued for a run slot"


class NotARunId(refusals.Invalid, ValueError):
    pass


class UnknownFilter(refusals.Invalid, ValueError):
    pass


def snapshot(recent_runs: int = RECENT_RUNS) -> dict:
    with foundation.one_read():
        running = functools.cache(lambda: run_store.runs_marked_running(connect=foundation.reader))
        return {
            "now": util.iso(),
            "serve": panel(control.serve_beat),
            "live": panel(lambda: live(running())),
            "skipped": panel(lambda: skipped(running())),
            "waiting": panel(waiting),
            "recovery": panel(recovery_queue),
            "ticket_progress": panel(lambda: ticket_status(running())),
            "recent": panel(lambda: run_rows(run_store.runs(limit=recent_runs, connect=foundation.reader))),
            "checks": panel(checks),
            "alerts": panel(alerts),
            "triage": panel(triage_board),
            "proposals": panel(proposals),
        }


def panel(read) -> dict:
    try:
        return {"rows": read()}
    except Exception as e:
        return {"error": util.first_line(f"{type(e).__name__}: {e}", 300)}


def outcome(status: str) -> str:
    return OUTCOME_OF_STATUS.get(status, "other")


def ticket(r: dict) -> str:
    return tracker.run_ticket_name(r) if r["issue"] else (r.get("loop") or r["repo"] or r["kind"])


def seconds_between(start: str, end: str | None) -> int:
    finish = util.parse_iso(end) if end else util.now()
    return int((finish - util.parse_iso(start)).total_seconds())


def log_lines(r: dict) -> list[str]:
    try:
        return log_file(r).read_text().splitlines()
    except (FileNotFoundError, NotADirectoryError):
        return []


def log_file(r: dict) -> Path:
    return paths.expand(r["log"]) if r.get("log") else paths.RUNS_DIR / r["id"] / "run.log"


def live(running: list[dict]) -> dict:
    in_slots = {r["id"] for r in slots.runs_in_slots(running)}
    waiting_for_slot = {r["id"] for r in slots.holding(running, "slot-wait")}
    paid = run_store.paid_by_run([r["id"] for r in running], connect=foundation.reader)
    rows = []
    for r in running:
        lines = log_lines(r)
        rows.append({
            "id": r["id"], "version": r["version"], "kind": r["kind"], "ticket": ticket(r), "repo": r["repo"] or "",
            "started": r["started"], "seconds": seconds_between(r["started"], None),
            "step": LOG_PREFIX.sub("", lines[-1]) if lines else "",
            "slot": slot_note(r, in_slots, waiting_for_slot),
            "paid": paid.get(r["id"], 0), "stopping": bool(r["stop_asked"]),
        })
    return {"runs": rows, "slots": len(in_slots), "waiting": len(waiting_for_slot),
            "max_parallel": config.load_or_problem()[0].max_parallel}


def slot_note(r: dict, in_slots: set[str], waiting_for_slot: set[str]) -> str:
    if r["kind"] != "pipeline" or r["id"] in in_slots:
        return ""
    if r["id"] in waiting_for_slot:
        return "waiting for a run slot"
    return "waiting on CI without a run slot" if slots.freed_slot_for_ci(r["id"]) else "without a run slot"


def skipped(running: list[dict]) -> list[dict]:
    tickets_running = {(r["repo"], str(r["issue"])) for r in running if r["issue"]}
    return [{"ticket": name, "repo": v.get("repo", ""), "why": v["why"], "since": v.get("since", ""),
             "url": v.get("url", ""), "can_run": bool(v.get("repo"))}
            for name, v in sorted(pipeline.skipped_tickets().items()) if (v.get("repo"), v.get("id")) not in tickets_running]


def waiting() -> dict:
    return {
        "tickets": [{"ticket": tracker.run_ticket_name(w), "comments": w["n"], "at_once": bool(w["at_once"]),
                     "submitted": w["submitted"], "last": w["last"]} for w in insights_store.feedback_waiting(connect=foundation.reader)],
        "quiet_minutes": pipeline.FEEDBACK_QUIET_MINUTES,
    }


def ticket_identity(row: dict) -> tuple[str, str]:
    return row["repo"], str(row["issue"])


def tracked_run(row: dict) -> bool:
    return (row["kind"] == "pipeline" and bool(row["repo"] and row["issue"]) and not row.get("only_step")
            and not row.get("dry_run") and row["status"] != "dry-run")


def ticket_queue() -> dict:
    queued = {ticket_identity(row): {"activity": SLOT_QUEUED, "blocker": "No run slot was available",
                                    "next_event": "A run slot becomes available", "since": row["since"]}
              for row in source_store.tickets_passed_over()}
    return queued | {(row["repo"], str(row["id"])): {
        **row, "activity": "Blocked before start", "blocker": outbound.sanitize(row["why"], 300),
        "next_event": "The blocker clears and the poller rechecks the ticket",
    } for row in pipeline.skipped_tickets().values() if row.get("repo") and row.get("id")}


def adaptive_activity(run_id: str) -> str:
    adaptive = orchestration_store.view(run_id, connect=foundation.reader) or {}
    if adaptive.get("current_run") != run_id:
        return ""
    capacity = adaptive.get("capacity", {})
    builders = capacity.get("active_builders", 0)
    workers = capacity.get("active_workers", 0) - builders
    stages = [f"{util.count(n, role)} {state}" for n, role, state in (
        (builders, "builder", "working"), (workers, "other worker", "active")) if n]
    active = {a["op"] for a in adaptive["actions"] if a["run"] == run_id and a["state"] == "running"}
    if not capacity and active.intersection(("worker.start", "worker.continue")):
        stages.append("Worker running")
    stages.extend(stage for op, stage in (("checks.run", "Checks running"), ("verification.run", "Verification running"),
                                          ("review.run", "Review running"), ("publication.request", "Publishing"))
                  if op in active)
    return " · ".join(stages)


def running_activity(row: dict, blockers: list[dict]) -> tuple[str, str, str]:
    if row.get("stop_asked"):
        return "Stopping", "", "The run stops and records its result"
    waits = [b for b in reversed(blockers) if b["run_id"] == row["id"]]
    wait = next((b["cause"] for b in waits if not b.get("resource") and b["cause"] in run_lines.WAIT_CAUSES), "")
    if wait in ("", "slot"):
        if slots.holding([row], "slot-wait"):
            wait = "slot"
        elif slots.holding([row], "merge-wait"):
            return "Waiting", "The repository merge lease is in use", "The repository merge lease becomes available"
        elif check_wait := next((b for b in waits if b.get("resource") in CHECK_WAIT_BLOCKERS), None):
            return "Waiting", CHECK_WAIT_BLOCKERS[check_wait["resource"]], check_wait["next_event"]
    if wait:
        return "Waiting", *{
            "slot": ("A run slot is unavailable", "A run slot becomes available"),
            "ci": ("CI is pending", "CI reports its result"),
            "limit": ("A provider limit is active", run_timing.WAITS["limit"][1]),
            "person": ("An owner answer is needed", "The owner answers the open question"),
            "unblock": ("Recovery is pending", "Automatic recovery reports its result"),
        }[wait]
    if activity := adaptive_activity(row["id"]):
        return activity, "", "An active worker or action reports its result"
    ended = set()
    for line in reversed(run_timing.read_log(row["id"])):
        if line.get("carried") or not (key := run_timing.step_attempt(line, 1)):
            continue
        if line.get("event") == "step_end":
            ended.add(key)
        elif line.get("event") == "step_start" and key not in ended and (label := outbound.sanitize(key[0], 80)):
            return f"Running {label}", "", f"{label} finishes"
    return "Running", "", "The run reports its next result"


def classic_tracker_question(run_id: str) -> str:
    question = run_timing.tracker_question(run_timing.read_log(run_id))
    return (outbound.sanitize(str(question.get("summary") or ""), 300) or "An owner answer is needed") if question else ""


def ticket_activity(run: dict, queued: dict, recovery: dict, blockers: list[dict], *,
                    draft: run_timing.DraftReview | None) -> tuple[str, str, str]:
    status = run.get("status")
    if status == "running":
        return running_activity(run, blockers)
    if status == "needs-info" and not run.get("stop_asked"):
        run_id = run["id"]
        route = asks_store.routes_of([run_id], connect=foundation.reader).get(run_id)
        if route == "owner":
            return "Waiting for the owner", "An owner answer is needed", "The owner answers the open question"
        answered = route == "closed" and asks_store.asks_due([run_id])
        if not route and (question := classic_tracker_question(run_id)):
            answered = bool(pipeline.answer_text(run_id))
            if not answered:
                return "Waiting for the owner", question, "The owner replies to the question on the tracker"
        if answered:
            if queued.get("answer_run") == run_id:
                return "Owner answered", queued["blocker"], run_timing.WAITS["provider"][1]
            if queued and not queued.get("answer_run"):
                return "Owner answered", queued["blocker"], queued["next_event"]
            return "Owner answered", "", "A continuation starts with the answer"
    if queued:
        return queued["activity"], queued["blocker"], queued["next_event"]
    if (phase := recovery.get("phase")) and phase not in ("resolved", "cancelled"):
        event = {"needs_owner": "The owner responds", "queued": "The recovery action becomes eligible",
                 "diagnosing": "Recovery completes its diagnosis", "resuming": "Delivery is rechecked"}
        detail = outbound.sanitize(recovery.get("detail") or recovery["cause"], 300)
        return f"Recovery: {phase.replace('_', ' ')}", detail, event.get(phase, "Recovery rechecks the blocker")
    if draft:
        activity = "Draft awaiting readiness decision" if draft.cause == "draft-ready" else "Draft held at publication"
        return activity, outbound.sanitize(draft.reason, 300), run_timing.WAITS[draft.cause][1]
    if status == "pr":
        return "Follow-up PR recorded", "", "Review, merge, or new feedback"
    words = {"merged": "Merged", "done": "Done", "failed": "Last run failed", "stopped": "Stopped",
             "needs-info": "Needs input", "interrupted": "Interrupted"}
    return words.get(status, "No run recorded" if not status else f"Last run: {status}"), "", ""


def ticket_updates(repo: str, issue: str) -> list[dict]:
    with foundation.reader() as c:
        updates = ({"seq": row["seq"], "at": row["at"], **update}
                   for row in ticket_progress.rows(c, repo, issue)
                   for update in reversed(json.loads(row["data"])["updates"]))
        return list(reversed(list(islice(updates, 3))))


def timing_duration(seconds: float) -> str:
    seconds = int(seconds)
    return " ".join(f"{n}{unit}" for n, unit in ((seconds // 3600, "h"), (seconds // 60 % 60, "m"),
                                                 (seconds % 60, "s")) if n) or "0s"


def timing_lines(timing: dict) -> list[str]:
    bound = "" if timing["elapsed_complete"] else "at least "
    categories = timing["category_seconds"]
    breakdown = " · ".join(f"{label}: {timing_duration(categories.get(key, 0))}"
                            for key, label in TIMING_CATEGORIES.items())
    overlap = timing["overlap_seconds"]
    lines = [f"Run lineage elapsed: {bound}{timing_duration(timing['elapsed_seconds'])}",
             breakdown, f"Unattributed: {timing_duration(timing['unattributed_seconds'])}",
             f"Overlap: {timing_duration(overlap)}" + (" (already counted in elapsed)" if overlap else "")]
    for entry in timing["overlap"]:
        activity = " + ".join(label + (f" ×{count}" if count > 1 else "")
                              for key, label in TIMING_CATEGORIES.items() if (count := entry["activity"].get(key)))
        lines.append(f"{activity}: {timing_duration(entry['seconds'])}")
    return lines


def ordinary_drafts(attention: list[dict]) -> dict[tuple[str, str], run_timing.DraftReview]:
    drafts = {}
    for row, draft in run_timing.pending_draft_reviews(attention):
        drafts.setdefault(ticket_identity(row), draft)
    return drafts


def ticket_projection(repo: str, issue: str, *, queued: dict, recovery: dict, as_of: datetime,
                      draft: run_timing.DraftReview | None, timing_run: str = "",
                      recorded_only: bool = False) -> dict | None:
    saved = ticket_progress.get(repo, issue)
    run = run_store.newest_live_whole_run(repo, issue) or {}
    run_id, status = run.get("id", ""), run.get("status")
    if saved.get("active"):
        draft = None
    if recorded_only and not (saved or draft):
        return None
    timing = run_timing.project(timing_id, as_of=as_of) if (timing_id := timing_run or run_id) else None
    hold = saved.get("follow_up") or {}
    if status != "running" and saved.get("active"):
        activity = {"waiting": "Draft waiting to retry verification",
                    "owner-review": "Draft awaiting owner review after automatic retry"}[hold["state"]]
        blocker = "; ".join((saved.get("blocker") or {}).get("causes", {}).values())
        next_event = saved["next_event"]
    else:
        current = timing or {}
        if status == "running" and timing_id != run_id and run_id not in current.get("run_ids", ()):
            current = run_timing.project(run_id, as_of=as_of) or {}
        activity, blocker, next_event = ticket_activity(run, queued, recovery, current.get("blockers", []), draft=draft)
        if (status == "running" and not run.get("stop_asked") and hold.get("draft")
                and run.get("environment_retry_of") == saved.get("run")):
            hold = {**hold, "state": "verifying"}
            if activity == "Running":
                activity, next_event = "Verifying the draft follow-up", "Automatic verification finishes"
        else:
            prs = (run.get("pr_url") or "").split() if status == "pr" else []
            hold = (dict(state="owner-review", prs=list(draft.prs), draft=True) if draft else
                    dict(state="pr", prs=prs, draft=None) if prs else None)
    answered_and_blocked = activity == "Owner answered" and bool(blocker)
    since = queued.get("since", "") if activity == queued.get("activity") or answered_and_blocked else ""
    if answered_and_blocked and queued.get("answer_run"):
        since = next((wait["since"] for wait in (timing or {}).get("blockers", ())
                      if wait["cause"] == "provider" and wait["run_id"] == run_id), since)
    return {"repo": repo, "issue": issue, "ticket": tracker.issue_name(repo, issue), "run": run_id,
            "activity": activity, "blocker": blocker, "next_event": next_event, "since": since,
            "url": queued.get("url", ""), "follow_up": hold,
            "prerequisites": saved.get("prerequisites", []), "prerequisite_issues": saved.get("prerequisite_issues", {}),
            "updates": ticket_updates(repo, issue) if saved else [],
            "timing": {**{key: timing[key] for key in TIMING_FIELDS}, "lines": timing_lines(timing)} if timing else None}


def ticket_status(running: list[dict], *, as_of: datetime | None = None) -> list[dict]:
    with foundation.one_read():
        as_of = as_of or util.now()
        queued = ticket_queue()
        recovery = {(row["repo"], row["ticket"]): row for row in recovery_queue()}
        repos = [repo.name for repo in config.load_or_problem()[0].repos]
        holds = (ticket_identity(row) for repo in repos for row in run_store.environment_holds(repo) if tracked_run(row)
                 and (not row["released"] or run_store.environment_owner_review_active(row)))
        declared = ((repo, issue) for repo in repos for issue in ticket_progress.declared_prerequisite_tickets(repo))
        asking = {(row["repo"], row["ticket"]) for row in asks_store.open_asks(connect=foundation.reader) if row["repo"]}
        attention = run_store.ticket_attention_runs(questions=True)
        classic = (ticket_identity(row) for row in attention if row["status"] == "needs-info"
                   and not asks_store.routes_of([rid := row["id"]]) and classic_tracker_question(rid) and not pipeline.answer_text(rid))
        current = dict.fromkeys([*(ticket_identity(row) for row in running if tracked_run(row)), *queued, *recovery,
                                 *sorted(asking), *classic, *holds, *declared])
        drafts = ordinary_drafts(attention)
        recent = run_store.runs(kind="pipeline", limit=RECENT_RUNS, connect=foundation.reader)
        candidates = dict.fromkeys([*current, *drafts, *(ticket_identity(row) for row in recent if tracked_run(row))])
        return [row for repo, issue in candidates
                if (row := ticket_projection(repo, issue, queued=queued.get((repo, issue), {}),
                                             recovery=recovery.get((repo, issue), {}), as_of=as_of,
                                             draft=drafts.get((repo, issue)),
                                             recorded_only=(repo, issue) not in current)) is not None]


def loops_running() -> set[str]:
    return {r["loop"] for r in run_store.runs_marked_running(connect=foundation.reader) if r["kind"] == "loop"}


def checks() -> dict:
    beat = util.read_json(paths.HEARTBEAT_FILE, {})
    return {
        "heartbeat": beat.get("ts", ""), "running": "heartbeat" in loops_running(),
        "checks": [{**c, "state": "ok" if c["ok"] else STATE_OF_SEVERITY.get(c["severity"], "fault")}
                   for c in sorted(beat.get("checks", []), key=lambda c: (c["ok"], c["key"]))],
        "loops": [{"name": k, **v} for k, v in sorted(loop_store.loop_marks(connect=foundation.reader).items())],
    }


def alerts() -> dict:
    alert_repo = config.load_or_problem()[0].alert_repo
    return {
        "alerts": [{**a, "repo": opened_in(a, alert_repo), "url": issue_link(a, alert_repo)}
                   for a in incident_store.alerts(connect=foundation.reader)],
        "probes": devices_store.probes(connect=foundation.reader),
    }


def triage_board() -> dict:
    v = triage.view(connect=foundation.reader)
    srcs = {s.name: s for s in triage.sources(config.load_or_problem()[0])}
    meta = util.read_json(triage.META_FILE, {})
    running = loops_running()
    return {
        "refreshing": "triage" in running, "researching": "research" in running,
        "refreshed": meta.get("refreshed", ""), "posted": meta.get("posted", ""), "errors": meta.get("errors", []),
        "approved": meta.get("approved", []), "held": meta.get("held", {}),
        "summary": triage.summary(v),
        "groups": [{"standing": name, "rows": [triage_entry(r, v, name, srcs) for r in members]}
                   for name, members in triage.groups(v).items()],
    }


def triage_entry(r: dict, v: dict, standing: str, srcs: dict[str, triage.TriageSource]) -> dict:
    own = v["research"].get(r["ref"]) if standing == triage.RESEARCH_WORK else None
    report = v["research"].get(own["answered_by"]) if own and own.get("answered_by") else own
    verdict = (triage.sent_note(r, False) if standing == triage.SENT
               else triage.research_progress(r, own) if standing == triage.RESEARCH_WORK else triage.verdict(r))
    return {"ref": r["ref"], "repo": r["repo"] or "", "title": r["title"] or "", "url": r["url"] or "",
            "kind": r["kind"] or "", "size": r["size"] or "", "verdict": verdict,
            "prs": " ".join(triage.sent_prs(r)) if standing == triage.SENT else (report or {}).get("filed_url") or "",
            "questions": r.get("questions") or [], "assumptions": r["assumptions"] or [], "checks": r.get("checks") or [], "plan": r["plan"] or [],
            "summary": report["summary"] if report else "", "report": report["report"] if report else "",
            "send": send_form(r, srcs.get(r.get("source", ""))) if standing in triage.SENDABLE else None,
            "research": triage.research_offer(r, own) if standing == triage.RESEARCH_WORK else None}


def send_form(r: dict, src: triage.TriageSource | None) -> dict | None:
    choices = triage.repo_choices(src, r) if src else []
    if not choices:
        return None
    return {"pick": not r["repo"], "repo_question": triage.repo_question(r),
            "choices": [{"repo": n, "labels": triage.labels_to_add({"route": src.route(n)})} for n in choices]}


def proposals() -> dict:
    return {kind: [{k: p[k] for k in ("id", "repo", "file", "change", "status")}
                   for p in approvals_store.proposals(status=statuses, limit=PROPOSALS_SHOWN, connect=foundation.reader)]
            for kind, statuses in (("memory", ["memory"]), ("hand", ["hand"]), ("approve", ["approve", "filing"]))}


def run_log(run_id: str) -> list[str]:
    if not re.fullmatch(r"\w[\w.-]*", run_id):
        raise NotARunId(f"'{run_id}' is not a run id.")
    return log_lines(run_store.run_get(run_id, connect=foundation.reader) or {"id": run_id})[-LOG_TAIL_LINES:]


def sidebar(talk: dict) -> dict:
    running = run_store.runs_marked_running(connect=foundation.reader)
    board = functools.cache(lambda: triage.view(connect=foundation.reader))
    beat = control.serve_beat()
    asks = needs_you(running, board, talk["draft"])
    with_questions = panel(lambda: triage.groups(board())[triage.WITH_QUESTIONS])
    return {"now": util.iso(), "needs_you": needs_you_count(asks), "running": len(running),
            "questions": len(with_questions.get("rows", [])), "health": health_word(beat, checks_and_alerts()),
            "serve": beat, "chat": {"busy": talk["busy"], "draft": bool(talk["draft"])}}


def live_board(draft: dict | None) -> dict:
    running = run_store.runs_marked_running(connect=foundation.reader)
    board = functools.cache(lambda: triage.view(connect=foundation.reader))
    return {"now": util.iso(), "live": panel(lambda: live(running)), "waiting": panel(waiting),
            "ticket_progress": panel(lambda: ticket_status(running)),
            "recovery": panel(recovery_queue), "recovering_tasks": recovering_tasks(),
            "needs_you": needs_you(running, board, draft)}


def recovering_tasks() -> list[str]:
    asks = asks_store.open_asks(connect=foundation.reader)
    owner = {a["ticket"] for a in asks if a["recovery_route"] == "owner"}
    return sorted({a["ticket"] for a in asks if tracker.is_task_id(a["ticket"])} - owner)


def recovery_queue() -> list[dict]:
    return [{k: row[k] for k in ("repo", "ticket", "source", "cause", "phase", "detail", "updated")}
            for row in unblock_store.cases(connect=foundation.reader)]


def skipped_for_owner(running: list[dict]) -> list[dict]:
    enabled = config.load_or_problem()[0].unblock_repos
    return [row for row in skipped(running) if row["repo"] not in enabled]


def needs_you(running: list[dict], board, draft: dict | None) -> dict:
    return {"approvals": panel(lambda: delivery.waiting(connect=foundation.reader)),
            "skipped": panel(lambda: skipped_for_owner(running)), "proposals": panel(proposals),
            "ready": panel(lambda: ready_in_one_click(board())), "draft": draft}


def needs_you_count(asks: dict) -> int:
    rows = [asks[part].get("rows") or [] for part in ("approvals", "skipped", "ready")]
    by_hand = asks["proposals"].get("rows") or {}
    return sum(map(len, rows)) + sum(map(len, by_hand.values())) + bool(asks["draft"])


def ready_in_one_click(v: dict) -> list[dict]:
    srcs = {s.name: s for s in triage.sources(config.load_or_problem()[0])}
    ready = [triage_entry(r, v, triage.READY, srcs) for r in triage.groups(v)[triage.READY] if not r.get("closed")]
    return [r for r in ready if r["send"] and not r["send"]["pick"]]


def checks_and_alerts() -> dict:
    return {"checks": panel(checks), "alerts": panel(alerts),
            "incidents": panel(lambda: incident_store.blocked_incidents(connect=foundation.reader))}


def health_word(beat: dict, found: dict) -> str:
    c, a = found["checks"].get("rows"), found["alerts"].get("rows")
    if (c is None or a is None or not beat.get("alive") or beat.get("config_problem") or beat.get("code_problem")
            or beat.get("stuck") or a["alerts"] or any(x["state"] == "fault" for x in c["checks"])):
        return "fault"
    blocked = "incidents" in found and (found["incidents"].get("rows") is None or bool(found["incidents"]["rows"]))
    if (not c["heartbeat"] or beat.get("broken") or beat.get("poll_errors") or beat.get("failing") or blocked
            or any(x["state"] != "ok" for x in c["checks"])):
        return "caution"
    return "ok"


def incident_number(text: str) -> int:
    if not (text.isascii() and text.isdecimal() and len(text) <= 18):
        raise control.NotFound(f"no incident {text}")
    return int(text)


def watcher(cfg: config.Config) -> dict:
    now = oncall.status(cfg, connect=foundation.reader)
    word = "disabled" if not now["enabled"] else "switched_off" if now["wakes"] == "off" else "on"
    return {"state": word, "config": paths.short_path(paths.CONFIG_FILE), "last_scan": now["last_scan"],
            "ok": now["ok"], "detail": now["detail"], "expected": now["expected"], "switched": now["switched"]}


def incident_row(r: dict) -> dict:
    keys = ("id", "state", "phase", "waiting", "class", "source", "repo", "signature", "first_seen", "last_seen",
            "count", "decision", "confidence", "reason", "injection", "decided", "retry_at", "recovery", "closed",
            "alerted")
    return {**{k: r[k] for k in keys}, "decision_line": oncall.decision_line(r), "repair_line": oncall.repair_line(r)}


def incidents_board() -> dict:
    cfg = config.load_or_problem()[0]
    rows = incident_store.incidents(connect=foundation.reader, open_first=True)
    return {"now": util.iso(), "watcher": watcher(cfg), "incidents": [incident_row(r) for r in rows]}


def incident_repair(repair: dict, number: int, runs: list[dict], paid: dict[str, float]) -> dict:
    listed = [{"id": r["id"], "status": r["status"], "started": r["started"], "finished": r["finished"],
               "pr_url": r["pr_url"] or "", "paid": paid.get(r["id"], 0)} for r in runs]
    return {"attempt": number, "repo": repair["repo"], "task": repair["task"], "started": repair["started"],
            "ended": repair["ended"], "outcome": repair["outcome"], "runs": listed,
            "paid": sum(r["paid"] for r in listed)}


def incident_board(text: str) -> dict:
    cfg = config.load_or_problem()[0]
    r = incident_store.incident(incident_number(text), connect=foundation.reader, last_notes=INCIDENT_NOTES)
    if r is None:
        raise control.NotFound(f"no incident {text}")
    made = {x["id"]: [] if not x["task"] else run_store.ticket_runs(x["repo"], x["task"], limit=50, connect=foundation.reader)
            for x in r["repairs"]}
    ids = [run["id"] for runs in made.values() for run in runs]
    paid = run_store.paid_by_run(ids, connect=foundation.reader, local_providers=cfg.local_providers)
    numbers = {x["id"]: n for n, x in enumerate(oncall.attempts(r), 1)}
    repairs = [incident_repair(x, numbers.get(x["id"], 0), made[x["id"]], paid) for x in r["repairs"]]
    open_to_steer = r["state"] == "open" and not oncall.live_repair(r)
    return {"now": util.iso(), "watcher": watcher(cfg), "incident": {
        **incident_row(r), "evidence": outbound.redact(r["evidence"] or ""), "answers": r["answers"],
        "notes": r["notes"], "notes_total": r["notes_total"], "max_attempts": cfg.oncall.max_attempts,
        "repairs": repairs, "paid": sum(x["paid"] for x in repairs),
        "can": {"dismiss": open_to_steer, "retry": open_to_steer and r["decision"] == "wake",
                "cancel": oncall.live_repair(r) is not None}}}


def health_board(*, include_held: bool = True) -> dict:
    beat = control.serve_beat()
    found = checks_and_alerts()
    workspaces = panel(workspace.held_summary)
    if include_held and "rows" in workspaces:
        workspaces["held"] = workspaces["rows"]["count"]
    return {**found, "now": util.iso(), "serve": beat, "state": health_word(beat, found),
            "devices": panel(control.fleet), "workspaces": workspaces}


def runs_board(filters: dict[str, str], limit: int = RECENT_RUNS) -> dict:
    status = filters.get("status", "")
    if status and status not in STATUSES_OF_FILTER:
        raise UnknownFilter(f"Runs filter by {', '.join(STATUSES_OF_FILTER)}, not '{status}'.")
    limit = max(1, min(limit, MOST_RUNS_SHOWN))
    rows = run_store.runs(kind=filters.get("kind"), repo=filters.get("repo"), loop=filters.get("loop"),
                      statuses=STATUSES_OF_FILTER.get(status, ()), running_first=True, limit=limit + 1, connect=foundation.reader)
    return {"now": util.iso(), "runs": run_rows(rows[:limit]), "more": len(rows) > limit,
            "facets": run_store.run_facets(foundation.reader)}


def run_rows(rows: list[dict]) -> list[dict]:
    paid = run_store.paid_by_run([r["id"] for r in rows], connect=foundation.reader)
    routes = asks_store.routes_of([r["id"] for r in rows if r["status"] == "needs-info"], connect=foundation.reader)
    return [{"id": r["id"], "kind": r["kind"], "ticket": ticket(r), "status": r["status"], "started": r["started"],
             "ask_route": routes.get(r["id"], ""),
             "finished": r["finished"], "seconds": seconds_between(r["started"], r["finished"]),
             "paid": paid.get(r["id"], 0), "pr_url": r["pr_url"] or "", "summary": r["summary"] or "",
             "loop": r.get("loop") or "", "repo": r["repo"] or ""} for r in rows]


def run_board(run_id: str, *, messages_after=0) -> dict:
    with foundation.one_read():
        if not re.fullmatch(r"\w[\w.-]*", run_id):
            raise NotARunId(f"'{run_id}' is not an id.")
        r = run_store.run_get(run_id, connect=foundation.reader)
        if not r:
            raise control.NotFound(f"nothing has the id {run_id}")
        models: dict[tuple[str, str, str], dict] = {}
        local_providers = config.load_or_problem()[0].local_providers
        for s in run_store.steps(run_id, connect=foundation.reader):
            billing = control.billing_of(s, local_providers)
            metered = billing == "api_key"
            key = (s["model"] or "no model", s["runner"] or "no runner", billing)
            row = models.setdefault(key, {"model": key[0], "runner": key[1], "metered": metered,
                                          "auth_mode": billing, "billing": billing, "paid": 0.0,
                                          "steps": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
            row["steps"] += 1
            row["tokens_in"] += s["tokens_in"] or 0
            row["tokens_out"] += s["tokens_out"] or 0
            row["cost_usd"] += 0 if billing in ("local", "replay") else s["cost_usd"] or 0
            row["paid"] += (s["cost_usd"] or 0) if metered else 0
        rows = sorted(({**m, "cost_usd": round(m["cost_usd"], 4), "paid": round(m["paid"], 4)}
                       for m in models.values()),
                      key=lambda m: (-m["cost_usd"], m["model"], m["runner"], m["billing"]))
        spec = util.read_json(sessions.spec_file(run_id), {}) if r["kind"] == "session" else {}
        adaptive = orchestration_store.view(run_id, connect=foundation.reader, messages_after=messages_after)
        if adaptive:
            adaptive["workspace_retained"] = not flows.action_workspace_refusal(run_id)
        recovery = unblock_store.get(r["repo"], str(r["issue"]), connect=foundation.reader) if r["issue"] else {}
        routes = asks_store.routes_of([run_id], connect=foundation.reader) if r["status"] == "needs-info" else {}
        progress = None
        if tracked_run(r):
            key = ticket_identity(r)
            drafts = ordinary_drafts(run_store.ticket_attention_runs(ticket=key))
            progress = ticket_projection(*key, queued=ticket_queue().get(key, {}), recovery=recovery,
                                         as_of=util.now(), draft=drafts.get(key), timing_run=run_id)
        return {"now": util.iso(),
                "ticket_progress": progress,
                "run": {**r, "ticket": ticket(r), "paid": round(sum(m["paid"] for m in rows), 4),
                        "ask_route": routes.get(run_id, "")},
                "recovery": {k: recovery[k] for k in ("phase", "cause", "detail")} if recovery else None,
                "models": rows,
                "orchestration": adaptive,
                "approvals": [a for a in delivery.waiting(connect=foundation.reader) if a["run_id"] == run_id],
                "lines_from": "feed" if r["kind"] in FEED_KINDS else "log",
                "session": {"prompt": spec.get("prompt", ""), "tools": spec.get("tools", []),
                            "cwd": paths.short_path(spec.get("cwd", "")), "repo": spec.get("repo", "")} if spec else None}


def find() -> dict:
    v = triage.view(connect=foundation.reader)
    runs = run_store.runs(limit=RUNS_TO_FIND, connect=foundation.reader)
    return {
        "loops": [{"name": loop.name, "description": loop.description} for loop in loops.load_all(config.load_or_problem()[0])],
        "runs": [{"id": r["id"], "ticket": ticket(r), "kind": r["kind"], "status": r["status"]} for r in runs],
        "sessions": [{"id": s["id"], "prompt": s["prompt"], "status": s["status"]}
                     for s in sessions.views(limit=SESSIONS_TO_FIND, connect=foundation.reader)],
        "tickets": [{"ref": r["ref"], "title": r["title"] or "", "standing": triage.standing(r)}
                    for r in v["rows"] if not r.get("closed")],
    }


def outcomes() -> dict:
    figures = trend_figures()
    return {**figures, "decisions": decisions(figures["since"])}


def decisions(since: str) -> dict:
    pushes = Counter(push_decision(v) for v in insights_store.verdicts_since(since, connect=foundation.reader) if v["kind"] == "push")
    sent = Counter(a["sent_by"] for a in triage_store.triage_approvals(since, connect=foundation.reader))
    return {"pushes": {k: pushes[k] for k in ("auto", "approved", "rejected", "waiting", "other")},
            "tickets": {"triage": sent["triage"], "owner": sum(n for actor, n in sent.items() if principals.is_person(actor))}}


def push_decision(v: dict) -> str:
    if v["outcome"] in ("rejected", "waiting"):
        return v["outcome"]
    if v["outcome"] in LANDED:
        return "auto" if v["approval"] is None else "approved"
    return "other"


def usage_board(days: int = USAGE_DAYS) -> dict:
    days = max(1, min(days, KEEP_USAGE_DAYS))
    rows = insights_store.usage_since(util.iso(util.now() - timedelta(days=days)), connect=foundation.reader)
    visits = whole_visits([r for r in rows if r["kind"] == "visit"])
    by_view: dict[str, list[dict]] = defaultdict(list)
    for v in visits:
        by_view[v["view"]].append(v)
    actions: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r["kind"] == "action":
            actions[r["name"]][r["device"]] += 1
    return {
        "now": util.iso(), "days": days, "keep_days": KEEP_USAGE_DAYS, "first": rows[0]["at"] if rows else "",
        "views": sorted(({"view": name, "visits": len(vs), "desktop": sum(v["device"] == "desktop" for v in vs),
                          "phone": sum(v["device"] == "phone" for v in vs), "ms": sum(v["ms"] for v in vs),
                          "median_ms": int(statistics.median(v["ms"] for v in vs))}
                         for name, vs in by_view.items()), key=lambda v: (-v["visits"], v["view"])),
        "ways_in": sorted(({"via": via, "visits": n} for via, n in Counter(v["via"] for v in visits).items()),
                          key=lambda w: (-w["visits"], w["via"])),
        "actions": sorted(({"name": name, "count": sum(c.values()), "desktop": c["desktop"], "phone": c["phone"]}
                           for name, c in actions.items()), key=lambda a: (-a["count"], a["name"])),
    }


def whole_visits(stints: list[dict]) -> list[dict]:
    visits, open_visit = [], {}
    for s in stints:
        last = open_visit.get(s["tab"])
        if s["via"] == "return" and last and last["view"] == s["view"]:
            last["ms"] += s["ms"] or 0
            continue
        open_visit[s["tab"]] = visit = {"view": s["view"], "device": s["device"], "via": s["via"], "ms": s["ms"] or 0}
        visits.append(visit)
    return visits


def local_day(ts: str) -> str:
    return util.parse_iso(ts).astimezone().date().isoformat()


def trends() -> dict:
    return panel(trend_figures)


def trend_figures() -> dict:
    first_day = datetime.now().astimezone().date() - timedelta(days=TREND_DAYS - 1)
    days = [(first_day + timedelta(days=n)).isoformat() for n in range(TREND_DAYS)]
    since = util.iso(datetime.combine(first_day, time.min).astimezone().astimezone(UTC))
    runs = run_store.runs_since(since, connect=foundation.reader)
    evals = insights_store.evals_since(since, connect=foundation.reader)

    spend = control.paid_since(since, foundation.reader)
    paid_on = {d["name"]: d["cost_usd"] for d in spend["by_day"]}
    by_day = {d: {"day": d, "paid": paid_on.get(d, 0.0), "shipped": 0, "failed": 0, "other": 0} for d in days}
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        if r["kind"] != "pipeline" or r["status"] == "running":
            continue
        by_repo[r["repo"]].append(r)
        if day := by_day.get(local_day(r["started"])):
            day[outcome(r["status"])] += 1
    paid = run_store.paid_by_run([r["id"] for rows in by_repo.values() for r in rows], connect=foundation.reader)

    eval_repos: dict[str, list[dict]] = defaultdict(list)
    for e in evals:
        if e["source"] == insights_store.BASELINE_SOURCE:
            eval_repos[e["repo"]].append(e)
    return {
        "since": since,
        "days": list(by_day.values()),
        "repos": [repo_trend(name, rows, paid) for name, rows in sorted(by_repo.items())],
        "evals": [eval_trend(name, rows) for name, rows in sorted(eval_repos.items())],
        "total_paid": spend["total_usd"],
        **{group: spend[group] for group in ("by_model", "by_runner", "by_loop")},
    }


def repo_trend(name: str, rows: list[dict], paid: dict[str, float]) -> dict:
    outcomes = Counter(outcome(r["status"]) for r in rows)
    shipped, failed = outcomes["shipped"], outcomes["failed"]
    finished = [seconds_between(r["started"], r["finished"]) for r in rows if r["finished"]]
    return {
        "repo": name, "runs": len(rows), "shipped": shipped, "failed": failed,
        "pass_rate": round(shipped / (shipped + failed), 2) if shipped + failed else None,
        "median_seconds": int(statistics.median(finished)) if finished else None,
        "paid": round(sum(paid.get(r["id"], 0) for r in rows), 4),
    }


def eval_trend(name: str, rows: list[dict]) -> dict:
    passed = sum(1 for e in rows if e["passed"])
    return {"repo": name, "tasks": len(rows), "passed": passed, "pass_rate": round(passed / len(rows), 2),
            "last": rows[-1]["ts"]}
