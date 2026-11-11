from __future__ import annotations

import functools
import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from . import config, control, delivery, loops, paths, pipeline, sessions, state, tracker, triage, util
from .agent import SUBSCRIPTION_RUNNER
from .alerts import issue_link, opened_in

RECENT_RUNS = 25
TREND_DAYS = 30
LOG_TAIL_LINES = 40
PROPOSALS_SHOWN = 10
LOG_PREFIX = re.compile(r"^(?:\S+ \[[^\]]+\] |\d{4}-\d\d-\d\dT\S+ )")
OUTCOME_OF_STATUS = {"merged": "shipped", "pr": "shipped", "failed": "failed"}
STATE_OF_SEVERITY = {"fail": "fault", "warn": "caution"}
STATUSES_OF_FILTER = {"running": ("running",), "failed": ("failed", "interrupted")}
FEED_KINDS = ("loop", "session", "draft")
MOST_RUNS_SHOWN = 500
RUNS_TO_FIND = 40
SESSIONS_TO_FIND = 30
USAGE_DAYS = 30
KEEP_USAGE_DAYS = 90
LANDED = ("landed", "replaced", "approved")


class NotARunId(ValueError):
    pass


class UnknownFilter(ValueError):
    pass


def snapshot(recent_runs: int = RECENT_RUNS) -> dict:
    running = functools.cache(lambda: state.runs_marked_running(connect=state.reader))
    return {
        "now": util.iso(),
        "serve": panel(control.serve_beat),
        "live": panel(lambda: live(running())),
        "skipped": panel(lambda: skipped(running())),
        "waiting": panel(waiting),
        "recent": panel(lambda: run_rows(state.runs(limit=recent_runs, connect=state.reader))),
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
    in_slots = {r["id"] for r in pipeline.holding(running, "slot")}
    waiting_for_slot = {r["id"] for r in pipeline.holding(running, "slot-wait")}
    paid = state.paid_by_run([r["id"] for r in running], connect=state.reader)
    rows = []
    for r in running:
        lines = log_lines(r)
        rows.append({
            "id": r["id"], "kind": r["kind"], "ticket": ticket(r), "repo": r["repo"] or "",
            "started": r["started"], "seconds": seconds_between(r["started"], None),
            "step": LOG_PREFIX.sub("", lines[-1]) if lines else "",
            "slot": slot_note(r, in_slots, waiting_for_slot),
            "paid": paid.get(r["id"], 0), "stopping": bool(r["stop_asked"]),
        })
    return {"runs": rows, "slots": len(in_slots), "waiting": len(waiting_for_slot),
            "max_parallel": config.load().max_parallel}


def slot_note(r: dict, in_slots: set[str], waiting_for_slot: set[str]) -> str:
    if r["kind"] != "pipeline" or r["id"] in in_slots:
        return ""
    if r["id"] in waiting_for_slot:
        return "waiting for a run slot"
    return "waiting on CI without a run slot" if pipeline.freed_slot_for_ci(r["id"]) else "without a run slot"


def skipped(running: list[dict]) -> list[dict]:
    tickets_running = {(r["repo"], str(r["issue"])) for r in running if r["issue"]}
    return [{"ticket": name, "repo": v.get("repo", ""), "why": v["why"], "since": v.get("since", ""),
             "url": v.get("url", ""), "can_run": bool(v.get("repo"))}
            for name, v in sorted(pipeline.skipped_tickets().items()) if (v.get("repo"), v.get("id")) not in tickets_running]


def waiting() -> dict:
    return {
        "tickets": [{"ticket": tracker.run_ticket_name(w), "comments": w["n"], "at_once": bool(w["at_once"]),
                     "submitted": w["submitted"], "last": w["last"]} for w in state.feedback_waiting(connect=state.reader)],
        "quiet_minutes": pipeline.FEEDBACK_QUIET_MINUTES,
    }


def loops_running() -> set[str]:
    return {r["loop"] for r in state.runs_marked_running(connect=state.reader) if r["kind"] == "loop"}


def checks() -> dict:
    beat = util.read_json(paths.HEARTBEAT_FILE, {})
    return {
        "heartbeat": beat.get("ts", ""), "running": "heartbeat" in loops_running(),
        "checks": [{**c, "state": "ok" if c["ok"] else STATE_OF_SEVERITY.get(c["severity"], "fault")}
                   for c in sorted(beat.get("checks", []), key=lambda c: (c["ok"], c["key"]))],
        "loops": [{"name": k, **v} for k, v in sorted(util.read_json(paths.LOOP_MARKS_FILE, {}).items())],
    }


def alerts() -> dict:
    alert_repo = config.load().alert_repo
    return {
        "alerts": [{**a, "repo": opened_in(a, alert_repo), "url": issue_link(a, alert_repo)}
                   for a in state.alerts(connect=state.reader)],
        "probes": state.probes(connect=state.reader),
    }


def triage_board() -> dict:
    v = triage.view(connect=state.reader)
    srcs = {s.name: s for s in triage.sources(config.load())}
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
            "questions": r.get("questions") or [], "assumptions": r["assumptions"] or [], "plan": r["plan"] or [],
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
                   for p in state.proposals(status=statuses, limit=PROPOSALS_SHOWN, connect=state.reader)]
            for kind, statuses in (("memory", ["memory"]), ("hand", ["hand"]), ("approve", ["approve", "filing"]))}


def run_log(run_id: str) -> list[str]:
    if not re.fullmatch(r"\w[\w.-]*", run_id):
        raise NotARunId(f"'{run_id}' is not a run id.")
    return log_lines(state.run_get(run_id, connect=state.reader) or {"id": run_id})[-LOG_TAIL_LINES:]


def sidebar(talk: dict) -> dict:
    running = state.runs_marked_running(connect=state.reader)
    board = functools.cache(lambda: triage.view(connect=state.reader))
    beat = control.serve_beat()
    asks = needs_you(running, board, talk["draft"])
    with_questions = panel(lambda: triage.groups(board())[triage.WITH_QUESTIONS])
    return {"now": util.iso(), "needs_you": needs_you_count(asks), "running": len(running),
            "questions": len(with_questions.get("rows", [])), "health": health_word(beat, checks_and_alerts()),
            "serve": beat, "chat": {"busy": talk["busy"], "draft": bool(talk["draft"])}}


def live_board(draft: dict | None) -> dict:
    running = state.runs_marked_running(connect=state.reader)
    board = functools.cache(lambda: triage.view(connect=state.reader))
    return {"now": util.iso(), "live": panel(lambda: live(running)), "waiting": panel(waiting),
            "needs_you": needs_you(running, board, draft)}


def needs_you(running: list[dict], board, draft: dict | None) -> dict:
    return {"approvals": panel(lambda: delivery.waiting(connect=state.reader)),
            "skipped": panel(lambda: skipped(running)), "proposals": panel(proposals),
            "ready": panel(lambda: ready_in_one_click(board())), "draft": draft}


def needs_you_count(asks: dict) -> int:
    rows = [asks[part].get("rows") or [] for part in ("approvals", "skipped", "ready")]
    by_hand = asks["proposals"].get("rows") or {}
    return sum(map(len, rows)) + sum(map(len, by_hand.values())) + bool(asks["draft"])


def ready_in_one_click(v: dict) -> list[dict]:
    srcs = {s.name: s for s in triage.sources(config.load())}
    ready = [triage_entry(r, v, triage.READY, srcs) for r in triage.groups(v)[triage.READY] if not r.get("closed")]
    return [r for r in ready if r["send"] and not r["send"]["pick"]]


def checks_and_alerts() -> dict:
    return {"checks": panel(checks), "alerts": panel(alerts)}


def health_word(beat: dict, found: dict) -> str:
    c, a = found["checks"].get("rows"), found["alerts"].get("rows")
    if c is None or a is None or not beat.get("alive") or a["alerts"] or any(x["state"] == "fault" for x in c["checks"]):
        return "fault"
    if not c["heartbeat"] or beat.get("broken") or beat.get("poll_errors") or any(x["state"] != "ok" for x in c["checks"]):
        return "caution"
    return "ok"


def health_board() -> dict:
    beat = control.serve_beat()
    found = checks_and_alerts()
    return {**found, "now": util.iso(), "serve": beat, "state": health_word(beat, found),
            "devices": panel(control.fleet)}


def runs_board(filters: dict[str, str], limit: int = RECENT_RUNS) -> dict:
    status = filters.get("status", "")
    if status and status not in STATUSES_OF_FILTER:
        raise UnknownFilter(f"Runs filter by {', '.join(STATUSES_OF_FILTER)}, not '{status}'.")
    limit = max(1, min(limit, MOST_RUNS_SHOWN))
    rows = state.runs(kind=filters.get("kind"), repo=filters.get("repo"), loop=filters.get("loop"),
                      statuses=STATUSES_OF_FILTER.get(status, ()), running_first=True, limit=limit + 1, connect=state.reader)
    return {"now": util.iso(), "runs": run_rows(rows[:limit]), "more": len(rows) > limit,
            "facets": state.run_facets(state.reader)}


def run_rows(rows: list[dict]) -> list[dict]:
    paid = state.paid_by_run([r["id"] for r in rows], connect=state.reader)
    return [{"id": r["id"], "kind": r["kind"], "ticket": ticket(r), "status": r["status"], "started": r["started"],
             "finished": r["finished"], "seconds": seconds_between(r["started"], r["finished"]),
             "paid": paid.get(r["id"], 0), "pr_url": r["pr_url"] or "", "summary": r["summary"] or "",
             "loop": r.get("loop") or "", "repo": r["repo"] or ""} for r in rows]


def run_board(run_id: str) -> dict:
    if not re.fullmatch(r"\w[\w.-]*", run_id):
        raise NotARunId(f"'{run_id}' is not an id.")
    r = state.run_get(run_id, connect=state.reader)
    if not r:
        raise control.NotFound(f"nothing has the id {run_id}")
    models: dict[tuple[str, str], dict] = {}
    for s in state.steps(run_id, connect=state.reader):
        key = (s["model"] or "no model", s["runner"] or "no runner")
        row = models.setdefault(key, {"model": key[0], "runner": key[1], "metered": s["runner"] != SUBSCRIPTION_RUNNER,
                                      "steps": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
        row["steps"] += 1
        row["tokens_in"] += s["tokens_in"] or 0
        row["tokens_out"] += s["tokens_out"] or 0
        row["cost_usd"] += s["cost_usd"] or 0
    spec = util.read_json(sessions.spec_file(run_id), {}) if r["kind"] == "session" else {}
    return {"now": util.iso(),
            "run": {**r, "ticket": ticket(r), "paid": state.paid_by_run([run_id], state.reader).get(run_id, 0)},
            "models": sorted(({**m, "cost_usd": round(m["cost_usd"], 4)} for m in models.values()),
                             key=lambda m: (-m["cost_usd"], m["model"])),
            "approvals": [a for a in delivery.waiting(connect=state.reader) if a["run_id"] == run_id],
            "lines_from": "feed" if r["kind"] in FEED_KINDS else "log",
            "session": {"prompt": spec.get("prompt", ""), "tools": spec.get("tools", []),
                        "cwd": paths.short_path(spec.get("cwd", "")), "repo": spec.get("repo", "")} if spec else None}


def find() -> dict:
    v = triage.view(connect=state.reader)
    runs = state.runs(limit=RUNS_TO_FIND, connect=state.reader)
    return {
        "loops": [{"name": loop.name, "description": loop.description} for loop in loops.load_all(config.load())],
        "runs": [{"id": r["id"], "ticket": ticket(r), "kind": r["kind"], "status": r["status"]} for r in runs],
        "sessions": [{"id": s["id"], "prompt": s["prompt"], "status": s["status"]}
                     for s in sessions.views(limit=SESSIONS_TO_FIND, connect=state.reader)],
        "tickets": [{"ref": r["ref"], "title": r["title"] or "", "standing": triage.standing(r)}
                    for r in v["rows"] if not r.get("closed")],
    }


def outcomes() -> dict:
    figures = trend_figures()
    return {**figures, "decisions": decisions(figures["since"])}


def decisions(since: str) -> dict:
    pushes = Counter(push_decision(v) for v in state.verdicts_since(since, connect=state.reader) if v["kind"] == "push")
    sent = Counter(a["sent_by"] for a in state.triage_approvals(since, connect=state.reader))
    return {"pushes": {k: pushes[k] for k in ("auto", "approved", "rejected", "waiting", "other")},
            "tickets": {"triage": sent["triage"], "owner": sent["owner"]}}


def push_decision(v: dict) -> str:
    if v["outcome"] in ("rejected", "waiting"):
        return v["outcome"]
    if v["outcome"] in LANDED:
        return "auto" if v["approval"] is None else "approved"
    return "other"


def usage_board(days: int = USAGE_DAYS) -> dict:
    days = max(1, min(days, KEEP_USAGE_DAYS))
    rows = state.usage_since(util.iso(util.now() - timedelta(days=days)), connect=state.reader)
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
    runs = state.runs_since(since, connect=state.reader)
    evals = state.evals_since(since, connect=state.reader)

    spend = control.paid_since(since, state.reader)
    paid_on = {d["name"]: d["cost_usd"] for d in spend["by_day"]}
    by_day = {d: {"day": d, "paid": paid_on.get(d, 0.0), "shipped": 0, "failed": 0, "other": 0} for d in days}
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        if r["kind"] != "pipeline" or r["status"] == "running":
            continue
        by_repo[r["repo"]].append(r)
        if day := by_day.get(local_day(r["started"])):
            day[outcome(r["status"])] += 1
    paid = state.paid_by_run([r["id"] for rows in by_repo.values() for r in rows], connect=state.reader)

    eval_repos: dict[str, list[dict]] = defaultdict(list)
    for e in evals:
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
