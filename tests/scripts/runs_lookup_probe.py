import json
from datetime import UTC, datetime, timedelta

from arete import paths, state

BASE = datetime(2026, 1, 1, tzinfo=UTC)
rows = []
ids = {}
clock = [0]


def stamp(offset=None):
    if offset is None:
        clock[0] += 1
        offset = clock[0]
    return (BASE + timedelta(seconds=offset)).isoformat(timespec="seconds")


def add(ticket, status, *, only_step="", pr=False, started=None):
    run_id = f"r{len(rows):05d}"
    ids.setdefault(ticket, []).append(run_id)
    rows.append((run_id, "pipeline", "arete", ticket, status, started or stamp(), only_step,
                 f"https://example.test/arete/pull/{ticket}" if pr else "", None, None, 0))


def whole(ticket):
    return None if ticket % 2 else ""


for ticket in range(1, 1001):
    for _ in range(3):
        add(ticket, "failed", only_step=whole(ticket))
for ticket in range(1001, 1021):
    add(ticket, "interrupted", only_step=whole(ticket))
for ticket in range(1021, 1031):
    add(ticket, "interrupted", only_step=whole(ticket))
    add(ticket, "pr", only_step=whole(ticket), pr=True)
for ticket in range(1031, 1036):
    add(ticket, "pr", only_step=whole(ticket), pr=True)
    add(ticket, "interrupted", only_step="verify")
for ticket in range(1036, 1039):
    same = stamp()
    add(ticket, "interrupted", only_step=whole(ticket), started=same)
    add(ticket, "pr", only_step=whole(ticket), pr=True, started=same)
for ticket in range(1039, 2001):
    add(ticket, "merged", only_step=whole(ticket))
for n in range(7000):
    rows.append((f"loop{n:05d}", "loop", None, None, "done", stamp(), None, "", "tick", "command", 0))

with state.db() as c:
    c.executemany("INSERT INTO runs (id, kind, repo, issue, status, started, only_step, pr_url, loop, loop_kind, "
                  "cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    c.execute("INSERT INTO feedback (pr_url, kind, gh_id, repo, issue, body, at_once, created, status) "
              "VALUES (?, 'comment', 1, 'arete', '1021', 'please change', 1, ?, 'new')",
              ("https://example.test/arete/pull/1021", stamp()))

counts = {"progress": 0}
captured = {}
active = [None]


def progress():
    counts["progress"] += 1
    return 0


def trace(sql):
    if active[0] and sql.lstrip().upper().startswith("SELECT"):
        captured.setdefault(active[0], []).append(sql)


original = paths.connect_db


def connect_db(*args, **kwargs):
    conn = original(*args, **kwargs)
    conn.set_trace_callback(trace)
    conn.set_progress_handler(progress, 1000)
    return conn


paths.connect_db = connect_db


def call(name, function, *args, **kwargs):
    active[0] = name
    try:
        return function(*args, **kwargs)
    finally:
        active[0] = None


counts["progress"] = 0
interrupted = call("interrupted_tickets", state.interrupted_tickets, "arete")
first_progress = counts["progress"]

first_interrupted = ids[1001][0]
results = {
    "interrupted_tickets": interrupted,
    "issue_running": call("issue_running", state.issue_running, "arete", 1001),
    "running_run": call("running_run", state.running_run, "arete", 1001),
    "ticket_runs": [r["id"] for r in call("ticket_runs", state.ticket_runs, "arete", 1036, whole=True)],
    "open_runs": [r["id"] for r in call("open_runs", state.open_runs, "arete", 1021)],
    "later_run": (call("later_run", state.later_run, "arete", 1021, ids[1021][0]) or {}).get("id"),
    "last_pipeline_starts": call("last_pipeline_starts", state.last_pipeline_starts),
    "feedback_due": call("feedback_due", state.feedback_due, "arete", "2100-01-01"),
    "runs": [r["id"] for r in call("runs", state.runs, limit=50)],
    "runs_since": len(call("runs_since", state.runs_since, stamp(0))),
}
with state.db() as c:
    results["history"] = {
        "default_has_firings": any(r["kind"] == "loop" for r in state.runs(limit=20000)),
        "firings_has_firings": any(r["kind"] == "loop" for r in state.runs(limit=20000, firings=True)),
    }
    results["latest_whole"] = {
        str(t): state.ticket_runs("arete", t, limit=1, whole=True)[0]["id"] for t in (1031, 1036, 1037, 1038, 1001, 1002)}
    results["expected_latest_whole"] = {str(t): ids[t][1 if t >= 1036 else 0] for t in (1031, 1036, 1037, 1038, 1001, 1002)}

plans = {}
with state.db() as c:
    for name, statements in captured.items():
        plans[name] = [{"sql": sql, "plan": [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql)]}
                       for sql in statements if "runs" in sql]


def inspect():
    with state.db() as c:
        return {"runs": c.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "loops": c.execute("SELECT COUNT(*) FROM runs WHERE kind = 'loop'").fetchone()[0],
                "indexes": {r[0]: r[1] for r in c.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name IN ('runs_ticket', 'runs_started')")}}


seed = inspect()
reopened = inspect()
print("BEHAVIOR_RESULT=" + json.dumps({
    "results": results, "progress": first_progress, "plans": plans, "seed": seed, "reopened": reopened,
    "expected": [str(t) for t in range(1001, 1021)], "ids": {str(t): ids[t] for t in (1021, 1031, 1036, 1037, 1038)},
}, default=str))
