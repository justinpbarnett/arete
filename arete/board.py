from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from . import config, paths, pipeline, state, tracker, triage, util

RECENT_RUNS = 25
TREND_DAYS = 30
LOG_TAIL_LINES = 40
STEP_COST = re.compile(r"\(\d+ turns?, \$([\d.]+)\)")
LOG_PREFIX = re.compile(r"^\S+ \[[^\]]+\] ")
SHIPPED = {"pr", "merged"}
STATE_OF_STATUS = {"merged": "ok", "pr": "ok", "done": "ok", "failed": "fault", "interrupted": "fault",
                   "needs-info": "caution"}
STATE_OF_SEVERITY = {"fail": "fault", "warn": "caution"}
RUNNING = "SELECT * FROM runs WHERE status = 'running' ORDER BY started"
LATEST = "SELECT * FROM runs ORDER BY started DESC LIMIT ?"


def snapshot() -> dict:
    cfg = config.load()
    return {
        "now": util.iso(),
        "live": panel(lambda: live(cfg, rows_of(RUNNING))),
        "queue": panel(queue),
        "recent": panel(lambda: [recent(r) for r in rows_of(LATEST, RECENT_RUNS)]),
        "health": panel(lambda: health(cfg)),
        "triage": panel(triage_board),
    }


def rows_of(query: str, *args) -> list[dict]:
    with state.reader() as db:
        return [dict(r) for r in db.execute(query, args)]


def panel(read) -> dict:
    try:
        return {"rows": read()}
    except Exception as e:
        return {"error": util.first_line(f"{type(e).__name__}: {e}", 300)}


def ticket(r: dict) -> str:
    return tracker.run_ticket_name(r) if r["issue"] else (r["repo"] or r["kind"])


def seconds_between(start: str, end: str | None) -> int:
    finish = util.parse_iso(end) if end else util.now()
    return int((finish - util.parse_iso(start)).total_seconds())


def log_lines(run_id: str) -> list[str]:
    try:
        return (paths.RUNS_DIR / run_id / "run.log").read_text().splitlines()
    except (FileNotFoundError, NotADirectoryError):
        return []


def logged_cost(lines: list[str]) -> float:
    return round(sum(float(m.group(1)) for line in lines if (m := STEP_COST.search(line))), 2)


def live(cfg: config.Config, running: list[dict]) -> dict:
    rows = []
    for r in running:
        lines = log_lines(r["id"])
        rows.append({
            "id": r["id"], "kind": r["kind"], "ticket": ticket(r), "repo": r["repo"] or "",
            "started": r["started"], "seconds": seconds_between(r["started"], None),
            "step": LOG_PREFIX.sub("", lines[-1]) if lines else "",
            "cost": logged_cost(lines),
        })
    in_slots = sum(1 for r in running if r["kind"] == "pipeline")
    return {"runs": rows, "slots": in_slots, "max_parallel": cfg.max_parallel}


def queue() -> dict:
    with state.reader() as db:
        waiting = [dict(r) for r in db.execute(state.FEEDBACK_WAITING)]
    return {
        "skipped": [{"ticket": name, "why": v["why"], "since": v.get("since", ""), "url": v.get("url", "")}
                    for name, v in sorted(pipeline.skipped_tickets().items())],
        "waiting": [{"ticket": tracker.run_ticket_name(w), "comments": w["n"],
                     "when": "review submitted" if w["at_once"] else f"last comment {w['last']}"} for w in waiting],
        "quiet_minutes": pipeline.FEEDBACK_QUIET_MINUTES,
    }


def recent(r: dict) -> dict:
    return {
        "id": r["id"], "kind": r["kind"], "ticket": ticket(r), "status": r["status"],
        "state": STATE_OF_STATUS.get(r["status"], ""), "started": r["started"],
        "seconds": seconds_between(r["started"], r["finished"]),
        "cost": logged_cost(log_lines(r["id"])) if r["status"] == "running" else round(r["cost_usd"] or 0, 2),
        "pr_url": r["pr_url"] or "", "summary": r["summary"] or "",
    }


def health(cfg: config.Config) -> dict:
    beat = util.read_json(paths.HEARTBEAT_FILE, {})
    checks = sorted(beat.get("checks", []), key=lambda c: (c["ok"], c["key"]))
    with state.reader() as db:
        alerts = [dict(r) for r in db.execute("SELECT * FROM alerts ORDER BY opened")]
        probes = [dict(r) for r in db.execute("SELECT * FROM probes ORDER BY name")]
    return {
        "heartbeat": beat.get("ts", ""),
        "checks": [{**c, "state": "ok" if c["ok"] else STATE_OF_SEVERITY.get(c["severity"], "fault")} for c in checks],
        "loops": [{"name": k, **v} for k, v in sorted(util.read_json(paths.STATE_DIR / "loops.json", {}).items())],
        "alerts": [{**a, "url": f"https://github.com/{cfg.alert_repo}/issues/{a['issue']}"
                    if a["issue"] and cfg.alert_repo else ""} for a in alerts],
        "probes": probes,
    }


def triage_board() -> dict:
    with state.reader() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM triage")]
    for r in rows:
        r["questions"] = json.loads(r["questions"] or "[]")
    rows.sort(key=lambda r: triage.natural(r["ref"]))
    return {
        "refreshed": util.read_json(triage.META_FILE, {}).get("refreshed", ""),
        "groups": [{"standing": name, "rows": [{"ref": r["ref"], "repo": r["repo"] or "", "title": r["title"] or "",
                                                "url": r["url"] or "", "verdict": triage.verdict(r),
                                                "questions": r["questions"]} for r in members]}
                   for name, members in triage.groups(rows).items()],
    }


def run_log(run_id: str) -> list[str]:
    if not re.fullmatch(r"\w[\w.-]*", run_id):
        raise ValueError(f"'{run_id}' is not a run id")
    return log_lines(run_id)[-LOG_TAIL_LINES:]


def local_day(ts: str) -> str:
    return util.parse_iso(ts).astimezone().date().isoformat()


def trends() -> dict:
    return panel(trend_figures)


def trend_figures() -> dict:
    today = datetime.now().astimezone().date()
    days = [(today - timedelta(days=n)).isoformat() for n in range(TREND_DAYS - 1, -1, -1)]
    since = util.iso(util.now() - timedelta(days=TREND_DAYS + 1))
    with state.reader() as db:
        runs = [dict(r) for r in db.execute("SELECT * FROM runs WHERE started >= ? ORDER BY started", (since,))]
        evals = [dict(r) for r in db.execute("SELECT * FROM evals WHERE ts >= ? ORDER BY ts", (since,))]

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
            day["shipped" if r["status"] in SHIPPED else "failed" if r["status"] == "failed" else "other"] += 1

    eval_repos: dict[str, list[dict]] = defaultdict(list)
    for e in evals:
        eval_repos[e["repo"]].append(e)
    return {
        "days": [{**d, "cost": round(d["cost"], 2)} for d in by_day.values()],
        "repos": [repo_trend(name, rows) for name, rows in sorted(by_repo.items())],
        "evals": [{"repo": name, "tasks": len(rows), "passed": sum(1 for e in rows if e["passed"]),
                   "last": rows[-1]["ts"]} for name, rows in sorted(eval_repos.items())],
        "total_cost": round(sum(d["cost"] for d in by_day.values()), 2),
    }


def repo_trend(name: str, rows: list[dict]) -> dict:
    shipped = sum(1 for r in rows if r["status"] in SHIPPED)
    failed = sum(1 for r in rows if r["status"] == "failed")
    finished = [seconds_between(r["started"], r["finished"]) for r in rows if r["finished"]]
    return {
        "repo": name, "runs": len(rows), "shipped": shipped, "failed": failed,
        "pass_rate": round(shipped / (shipped + failed), 2) if shipped + failed else None,
        "median_seconds": int(statistics.median(finished)) if finished else None,
        "cost": round(sum(r["cost_usd"] or 0 for r in rows), 2),
    }
