from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from . import config, control, gh, paths, pipeline, state, tracker, triage, util

RECENT_RUNS = 25
TREND_DAYS = 30
LOG_TAIL_LINES = 40
PROPOSALS_SHOWN = 10
LOG_PREFIX = re.compile(r"^\S+ \[[^\]]+\] ")
OUTCOME_OF_STATUS = {"merged": "shipped", "pr": "shipped", "failed": "failed"}
STATE_OF_SEVERITY = {"fail": "fault", "warn": "caution"}


class NotARunId(ValueError):
    pass


def snapshot(recent_runs: int = RECENT_RUNS) -> dict:
    return {
        "now": util.iso(),
        "serve": panel(control.serve_beat),
        "live": panel(live),
        "skipped": panel(skipped),
        "waiting": panel(waiting),
        "recent": panel(lambda: [recent(r) for r in state.runs(limit=recent_runs, connect=state.reader)]),
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


def live() -> dict:
    running = state.runs_marked_running(connect=state.reader)
    in_slots = {r["id"] for r in pipeline.holding(running, "slot")}
    waiting_for_slot = {r["id"] for r in pipeline.holding(running, "slot-wait")}
    rows = []
    for r in running:
        lines = log_lines(r)
        rows.append({
            "id": r["id"], "kind": r["kind"], "ticket": ticket(r), "repo": r["repo"] or "",
            "started": r["started"], "seconds": seconds_between(r["started"], None),
            "step": LOG_PREFIX.sub("", lines[-1]) if lines else "",
            "slot": slot_note(r, in_slots, waiting_for_slot),
            "cost": round(r["cost_usd"] or 0, 2), "stopping": bool(r["stop_asked"]),
        })
    return {"runs": rows, "slots": len(in_slots), "waiting": len(waiting_for_slot),
            "max_parallel": config.load().max_parallel}


def slot_note(r: dict, in_slots: set[str], waiting_for_slot: set[str]) -> str:
    if r["kind"] != "pipeline" or r["id"] in in_slots:
        return ""
    if r["id"] in waiting_for_slot:
        return "waiting for a run slot"
    return "waiting on CI without a run slot" if pipeline.freed_slot_for_ci(r["id"]) else "without a run slot"


def skipped() -> list[dict]:
    running = {(r["repo"], str(r["issue"])) for r in state.runs_marked_running(connect=state.reader) if r["issue"]}
    return [{"ticket": name, "why": v["why"], "since": v.get("since", ""), "url": v.get("url", ""),
             "can_run": bool(v.get("repo")), "running": (v.get("repo"), v.get("id")) in running}
            for name, v in sorted(pipeline.skipped_tickets().items())]


def waiting() -> dict:
    return {
        "tickets": [{"ticket": tracker.run_ticket_name(w), "comments": w["n"], "at_once": bool(w["at_once"]),
                     "last": w["last"]} for w in state.feedback_waiting(connect=state.reader)],
        "quiet_minutes": pipeline.FEEDBACK_QUIET_MINUTES,
    }


def recent(r: dict) -> dict:
    return {
        "id": r["id"], "kind": r["kind"], "ticket": ticket(r), "status": r["status"], "started": r["started"],
        "seconds": seconds_between(r["started"], r["finished"]),
        "cost": round(r["cost_usd"] or 0, 2),
        "pr_url": r["pr_url"] or "", "summary": r["summary"] or "",
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
        "alerts": [{**a, "url": gh.issue_url(alert_repo, a["issue"]) if a["issue"] and alert_repo else ""}
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
    return {status: [{k: p[k] for k in ("id", "repo", "file", "change")}
                     for p in state.proposals(status=[status], limit=PROPOSALS_SHOWN, connect=state.reader)]
            for status in ("memory", "hand")}


def run_log(run_id: str) -> list[str]:
    if not re.fullmatch(r"\w[\w.-]*", run_id):
        raise NotARunId(f"'{run_id}' is not a run id.")
    return log_lines(state.run_get(run_id, connect=state.reader) or {"id": run_id})[-LOG_TAIL_LINES:]


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

    by_day = {d: {"day": d, "cost": 0.0, "shipped": 0, "failed": 0, "other": 0} for d in days}
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        day = by_day.get(local_day(r["started"]))
        if day:
            day["cost"] += r["cost_usd"] or 0
        if r["kind"] != "pipeline" or r["status"] == "running":
            continue
        by_repo[r["repo"]].append(r)
        if day:
            day[outcome(r["status"])] += 1

    eval_repos: dict[str, list[dict]] = defaultdict(list)
    for e in evals:
        eval_repos[e["repo"]].append(e)
    return {
        "since": since,
        "days": [{**d, "cost": round(d["cost"], 2)} for d in by_day.values()],
        "repos": [repo_trend(name, rows) for name, rows in sorted(by_repo.items())],
        "evals": [eval_trend(name, rows) for name, rows in sorted(eval_repos.items())],
        "total_cost": round(sum(d["cost"] for d in by_day.values()), 2),
    }


def repo_trend(name: str, rows: list[dict]) -> dict:
    outcomes = Counter(outcome(r["status"]) for r in rows)
    shipped, failed = outcomes["shipped"], outcomes["failed"]
    finished = [seconds_between(r["started"], r["finished"]) for r in rows if r["finished"]]
    return {
        "repo": name, "runs": len(rows), "shipped": shipped, "failed": failed,
        "pass_rate": round(shipped / (shipped + failed), 2) if shipped + failed else None,
        "median_seconds": int(statistics.median(finished)) if finished else None,
        "cost": round(sum(r["cost_usd"] or 0 for r in rows), 2),
    }


def eval_trend(name: str, rows: list[dict]) -> dict:
    passed = sum(1 for e in rows if e["passed"])
    return {"repo": name, "tasks": len(rows), "passed": passed, "pass_rate": round(passed / len(rows), 2),
            "last": rows[-1]["ts"]}
