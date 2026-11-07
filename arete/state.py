from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from . import paths, util

OPEN_PR_RUN = "(runs.status = 'pr' OR (runs.status = 'failed' AND runs.pr_url != ''))"
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- pipeline | miner | eval | draft | loop | session
    repo TEXT,
    issue INTEGER,
    status TEXT NOT NULL,          -- running | pr | needs-info | skipped | noop | dry-run | failed | interrupted
                                   -- | superseded | merged | rejected | done | stopped
    started TEXT NOT NULL,
    finished TEXT,
    attempts INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    pr_url TEXT,
    branch TEXT,
    summary TEXT,
    landed TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step TEXT,
    runner TEXT,
    model TEXT,
    provider TEXT,
    ok INTEGER,
    cost_usd REAL DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    turns INTEGER DEFAULT 0,
    seconds REAL,
    error TEXT,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS steps_ts ON steps (ts);
CREATE INDEX IF NOT EXISTS steps_run ON steps (run_id);
CREATE TABLE IF NOT EXISTS feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT,
    kind TEXT,
    text TEXT
);
CREATE INDEX IF NOT EXISTS feed_run ON feed (run_id, id);
CREATE TABLE IF NOT EXISTS loop_state (
    loop TEXT PRIMARY KEY,
    data TEXT DEFAULT '{}',
    last_fire TEXT,
    next_due TEXT,
    metric_on INTEGER DEFAULT 0,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    loop TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    title TEXT,
    url TEXT,
    run_id TEXT,
    seen INTEGER DEFAULT 1,
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (loop, fingerprint)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT,
    type TEXT,
    source TEXT,
    key TEXT,
    repo TEXT,
    title TEXT,
    url TEXT,
    trust TEXT,
    data TEXT,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    ts TEXT,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS cursors (
    source TEXT PRIMARY KEY,
    value TEXT,
    polled TEXT,
    ok INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT NOT NULL,
    queue TEXT NOT NULL,
    grp TEXT DEFAULT '',
    trigger TEXT,
    payload TEXT DEFAULT '[]',
    enqueued TEXT,
    run_id TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    name TEXT NOT NULL,
    value REAL,
    loop TEXT,
    run_id TEXT,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS metrics_name ON metrics (name, ts);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT,
    run_id TEXT,
    emit TEXT,
    summary TEXT,
    status TEXT,                   -- pending | approved | rejected | done | failed
    result TEXT,
    created TEXT,
    decided TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    text TEXT,
    ts TEXT,
    taken TEXT
);
CREATE TABLE IF NOT EXISTS probes (
    name TEXT PRIMARY KEY,
    last_run TEXT,
    last_ok TEXT,
    ok INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    issue INTEGER,
    opened TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    layer TEXT,
    repo TEXT,
    file TEXT,
    change TEXT,
    why TEXT,
    evidence TEXT,
    confidence REAL,
    status TEXT,                   -- applied | pending | merged | rejected | skipped
    pr_url TEXT,
    created TEXT
);
CREATE TABLE IF NOT EXISTS evals (
    run_id TEXT,
    repo TEXT,
    task TEXT,
    passed INTEGER,
    seconds REAL,
    cost_usd REAL,
    ts TEXT,
    source TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS feedback (
    pr_url TEXT NOT NULL,
    kind TEXT NOT NULL,            -- review | line | comment
    gh_id INTEGER NOT NULL,
    repo TEXT,
    issue TEXT,
    body TEXT,
    path TEXT,
    line INTEGER,
    hunk TEXT,
    thread INTEGER,
    at_once INTEGER DEFAULT 0,
    created TEXT,
    pr_state TEXT,                 -- OPEN | MERGED | CLOSED when recorded
    status TEXT,                   -- new | taken | done | failed | recorded
    run_id TEXT,
    PRIMARY KEY (pr_url, kind, gh_id)
);
CREATE TABLE IF NOT EXISTS mined (
    pr_url TEXT PRIMARY KEY,
    repo TEXT,
    issue TEXT,
    run_id TEXT,
    status TEXT,                   -- accepted | rejected | skipped
    why TEXT,
    task TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS triage (
    ref TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    tracker TEXT,
    repo TEXT,
    route TEXT,
    title TEXT,
    url TEXT,
    updated TEXT,
    ready INTEGER,
    code INTEGER,
    size TEXT,
    questions TEXT,
    plan TEXT,
    why TEXT,
    cost REAL DEFAULT 0,
    ts TEXT
);
"""
MIGRATIONS = [("evals", "source", "ALTER TABLE evals ADD COLUMN source TEXT DEFAULT ''"),
              ("runs", "landed", "ALTER TABLE runs ADD COLUMN landed TEXT"),
              ("triage", "code", "ALTER TABLE triage ADD COLUMN code INTEGER"),
              *[("runs", col, f"ALTER TABLE runs ADD COLUMN {col} {kind}") for col, kind in (
                  ("loop", "TEXT"), ("loop_kind", "TEXT"), ("model", "TEXT"), ("runner", "TEXT"), ("device", "TEXT"),
                  ("tokens_in", "INTEGER DEFAULT 0"), ("tokens_out", "INTEGER DEFAULT 0"), ("log", "TEXT"),
                  ("trigger", "TEXT"), ("queue", "TEXT"), ("grp", "TEXT"), ("pid", "INTEGER"), ("lock", "TEXT"),
                  ("stop_asked", "TEXT"))]]


@contextmanager
def db():
    paths.ensure_dirs()
    conn = sqlite3.connect(paths.DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, column, sql in MIGRATIONS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(sql)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def reader():
    conn = sqlite3.connect(f"{paths.DB_FILE.absolute().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


FIRINGS = ("(runs.kind = 'loop' AND runs.loop_kind = 'command' AND runs.status IN ('done', 'skipped') "
           "AND COALESCE(runs.cost_usd, 0) = 0)")


def runs(kind: str | None = None, repo: str | None = None, limit: int = 50, connect=db, loop: str | None = None,
         firings: bool = False) -> list[dict]:
    q, args = "SELECT * FROM runs WHERE 1=1", []
    if not firings:
        q += f" AND NOT {FIRINGS}"
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    if loop:
        q += " AND loop = ?"
        args.append(loop)
    if repo:
        q += " AND repo = ?"
        args.append(repo)
    q += " ORDER BY started DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def runs_since(since: str, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE started >= ? ORDER BY started", (since,))]


def has_open_pr(r: dict) -> bool:
    return r["status"] == "pr" or (r["status"] == "failed" and bool(r.get("pr_url")))


def open_runs(repo: str, issue: int | str) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE repo = ? AND issue = ? AND {OPEN_PR_RUN} "
                                           "ORDER BY started DESC", (repo, issue))]


def run_lock(kind: str, repo: str | None = None, issue: int | str | None = None) -> str:
    return {"pipeline": f"run-{repo}-{issue}", "slot": f"slot-{repo}-{issue}", "slot-wait": f"slot-wait-{repo}-{issue}",
            "eval": f"eval-{repo}", "eval-mine": "eval-mine", "miner": "mine"}[kind]


def loop_lock(name: str) -> str:
    return "loop-" + name.replace("/", ".")


def run_get(run_id: str, connect=db) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(r) if r else None


def steps(run_id: str, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY id", (run_id,))]


def steps_since(since: str, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT s.*, r.loop, r.kind, r.repo FROM steps s LEFT JOIN runs r "
                                           "ON r.id = s.run_id WHERE s.ts >= ? ORDER BY s.ts", (since,))]


def add_feed(run_id: str, kind: str, text: str) -> None:
    with db() as c:
        c.execute("INSERT INTO feed (run_id, ts, kind, text) VALUES (?,?,?,?)", (run_id, util.iso(), kind, text[:4000]))


def feed(run_id: str, after: int = 0, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feed WHERE run_id = ? AND id > ? ORDER BY id",
                                           (run_id, after))]


def loop_state(name: str, connect=db) -> dict:
    with connect() as c:
        r = c.execute("SELECT * FROM loop_state WHERE loop = ?", (name,)).fetchone()
    if not r:
        return {"loop": name, "data": {}, "last_fire": None, "next_due": None, "metric_on": 0}
    return {**dict(r), "data": json.loads(r["data"] or "{}")}


def loop_states(connect=db) -> dict[str, dict]:
    with connect() as c:
        return {r["loop"]: {**dict(r), "data": json.loads(r["data"] or "{}")} for r in c.execute("SELECT * FROM loop_state")}


def set_loop_state(name: str, **fields) -> None:
    if "data" in fields:
        fields["data"] = json.dumps(fields["data"], default=str)
    fields["updated"] = util.iso()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO loop_state (loop) VALUES (?)", (name,))
        c.execute(f"UPDATE loop_state SET {', '.join(f'{k} = ?' for k in fields)} WHERE loop = ?",
                  (*fields.values(), name))


def finding_seen(loop: str, fingerprint: str) -> bool:
    with db() as c:
        return c.execute("SELECT 1 FROM findings WHERE loop = ? AND fingerprint = ?", (loop, fingerprint)).fetchone() \
            is not None


def record_finding(loop: str, fingerprint: str, title: str, url: str, run_id: str) -> bool:
    with db() as c:
        before = c.total_changes
        c.execute("INSERT OR IGNORE INTO findings (loop, fingerprint, title, url, run_id, first_seen, last_seen) "
                  "VALUES (?,?,?,?,?,?,?)", (loop, fingerprint, title, url, run_id, util.iso(), util.iso()))
        fresh = c.total_changes > before
        if not fresh:
            c.execute("UPDATE findings SET seen = seen + 1, last_seen = ? WHERE loop = ? AND fingerprint = ?",
                      (util.iso(), loop, fingerprint))
        return fresh


def set_finding_url(loop: str, fingerprint: str, url: str) -> None:
    with db() as c:
        c.execute("UPDATE findings SET url = ? WHERE loop = ? AND fingerprint = ?", (url, loop, fingerprint))


def findings(loop: str, limit: int = 50, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM findings WHERE loop = ? ORDER BY last_seen DESC LIMIT ?",
                                           (loop, limit))]


def add_event(loop: str, e: dict) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO events (loop, type, source, key, repo, title, url, trust, data, ts) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (loop, e.get("type"), e.get("source"), str(e.get("key")), e.get("repo"), e.get("title"),
                         e.get("url"), e.get("trust"), json.dumps(e.get("data") or {}, default=str)[:20000], util.iso()))
        return cur.lastrowid


def recent_events(limit: int = 50, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT id, loop, type, source, key, repo, title, url, trust, ts FROM events "
                                           "ORDER BY id DESC LIMIT ?", (limit,))]


def seen_keys(source: str) -> set[str]:
    with db() as c:
        return {r[0] for r in c.execute("SELECT key FROM seen WHERE source = ?", (source,))}


def mark_seen(source: str, keys: list[str]) -> None:
    with db() as c:
        c.executemany("INSERT OR IGNORE INTO seen (source, key, ts) VALUES (?,?,?)",
                      [(source, str(k), util.iso()) for k in keys])


def cursor(source: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM cursors WHERE source = ?", (source,)).fetchone()
        return dict(r) if r else None


def set_cursor(source: str, value: str | None, ok: bool, detail: str = "") -> None:
    prev = cursor(source) or {}
    with db() as c:
        c.execute("INSERT OR REPLACE INTO cursors (source, value, polled, ok, detail) VALUES (?,?,?,?,?)",
                  (source, value if value is not None else prev.get("value"), util.iso(), int(ok), detail[:500]))


def cursors(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cursors ORDER BY source")]


def enqueue(loop: str, queue: str, grp: str, trigger: str, payload: list | None = None) -> int:
    with db() as c:
        if not payload and c.execute("SELECT 1 FROM queue WHERE loop = ? AND run_id IS NULL AND payload = '[]' "
                                     "AND trigger = ?", (loop, trigger)).fetchone():
            return 0
        cur = c.execute("INSERT INTO queue (loop, queue, grp, trigger, payload, enqueued) VALUES (?,?,?,?,?,?)",
                        (loop, queue, grp, trigger, json.dumps(payload or [], default=str), util.iso()))
        return cur.lastrowid


def pending(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM queue WHERE run_id IS NULL ORDER BY id")]


def queue_item(item_id: int) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    return {**dict(r), "payload": json.loads(r["payload"] or "[]")} if r else None


def take_queue_item(item_id: int, run_id: str | None) -> None:
    with db() as c:
        c.execute("UPDATE queue SET run_id = ? WHERE id = ?", (run_id, item_id))


def drop_queue_item(item_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM queue WHERE id = ?", (item_id,))


def drop_claimed_items(run_id: str) -> None:
    with db() as c:
        c.execute("DELETE FROM queue WHERE run_id = ?", (run_id,))


def prune(days: float = 14) -> None:
    since = util.iso(util.now() - timedelta(days=days))
    with db() as c:
        c.execute(f"DELETE FROM feed WHERE run_id IN (SELECT id FROM runs WHERE {FIRINGS} AND started < ?)", (since,))
        c.execute(f"DELETE FROM runs WHERE {FIRINGS} AND started < ?", (since,))
        c.execute("DELETE FROM queue WHERE run_id IS NOT NULL AND enqueued < ?", (since,))
        c.execute("DELETE FROM events WHERE ts < ?", (util.iso(util.now() - timedelta(days=days * 4)),))


def add_metric(name: str, value: float, loop: str = "", run_id: str = "") -> None:
    with db() as c:
        c.execute("INSERT INTO metrics (name, value, loop, run_id, ts) VALUES (?,?,?,?,?)",
                  (name, value, loop, run_id, util.iso()))


def latest_metric(name: str) -> float | None:
    with db() as c:
        r = c.execute("SELECT value FROM metrics WHERE name = ? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
        return r[0] if r else None


def metrics_since(since: str, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM metrics WHERE ts >= ? ORDER BY ts", (since,))]


def add_approval(loop: str, run_id: str, emit: dict, summary: str) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO approvals (loop, run_id, emit, summary, status, created) VALUES (?,?,?,?,?,?)",
                        (loop, run_id, json.dumps(emit, default=str), summary[:2000], "pending", util.iso()))
        return cur.lastrowid


def approvals() -> list[dict]:
    with db() as c:
        return [{**dict(r), "emit": json.loads(r["emit"] or "{}")}
                for r in c.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id DESC LIMIT 100")]


def approval(approval_id: int) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return {**dict(r), "emit": json.loads(r["emit"] or "{}")} if r else None


def decide_approval(approval_id: int, status: str, result: str = "") -> bool:
    with db() as c:
        before = c.total_changes
        c.execute("UPDATE approvals SET status = ?, result = ?, decided = ? WHERE id = ? AND status IN ('pending', ?)",
                  (status, result[:1000], util.iso(), approval_id, "approved" if status in ("done", "failed") else ""))
        return c.total_changes > before


def add_message(run_id: str, text: str) -> None:
    with db() as c:
        c.execute("INSERT INTO messages (run_id, text, ts) VALUES (?,?,?)", (run_id, text, util.iso()))


def take_messages(run_id: str) -> list[str]:
    with db() as c:
        rows = c.execute("SELECT id, text FROM messages WHERE run_id = ? AND taken IS NULL ORDER BY id",
                         (run_id,)).fetchall()
        c.executemany("UPDATE messages SET taken = ? WHERE id = ?", [(util.iso(), r["id"]) for r in rows])
        return [r["text"] for r in rows]


def loop_spent_since(since: str, loop: str | None = None) -> float:
    q = ("SELECT COALESCE(SUM(s.cost_usd), 0) FROM steps s JOIN runs r ON r.id = s.run_id "
         "WHERE s.ts >= ? AND r.kind = 'loop'")
    args: list = [since]
    if loop:
        q += " AND r.loop = ?"
        args.append(loop)
    with db() as c:
        return float(c.execute(q, args).fetchone()[0])


def runs_marked_running(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY started")]


def ticket_runs(repo: str, issue: int | str, limit: int = 10) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
                                           "ORDER BY started DESC LIMIT ?", (repo, issue, limit))]


def interrupted_tickets(repo: str) -> list[str]:
    with db() as c:
        rows = c.execute("SELECT issue, status FROM runs WHERE kind = 'pipeline' AND repo = ? "
                         "AND started = (SELECT MAX(started) FROM runs r2 WHERE r2.kind = 'pipeline' "
                         "AND r2.repo = runs.repo AND r2.issue = runs.issue) ORDER BY started", (repo,))
        return [str(r["issue"]) for r in rows if r["status"] == "interrupted"]


def last_pipeline_starts() -> dict[str, str]:
    with db() as c:
        return {r[0]: r[1] for r in c.execute("SELECT repo, MAX(started) FROM runs WHERE kind = 'pipeline' "
                                              "GROUP BY repo")}


def issue_running(repo: str, issue: int | str) -> bool:
    with db() as c:
        return c.execute("SELECT 1 FROM runs WHERE repo = ? AND issue = ? AND status = 'running' LIMIT 1",
                         (repo, issue)).fetchone() is not None


def probe_get(name: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM probes WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None


def probe_set(name: str, ok: bool, detail: str) -> None:
    ts = util.iso()
    prev = probe_get(name) or {}
    last_ok = ts if ok else prev.get("last_ok")
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO probes (name, last_run, last_ok, ok, detail) VALUES (?,?,?,?,?)",
            (name, ts, last_ok, int(ok), detail[:500]),
        )


def alert_get(key: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
        return dict(r) if r else None


def alert_set(key: str, issue: int | None) -> None:
    prev = alert_get(key)
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO alerts (key, issue, opened, last_seen) VALUES (?,?,?,?)",
            (key, issue, (prev or {}).get("opened") or util.iso(), util.iso()),
        )


def alert_clear(key: str) -> None:
    with db() as c:
        c.execute("DELETE FROM alerts WHERE key = ?", (key,))


def probes(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM probes ORDER BY name")]


def alerts(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY opened")]


def add_proposal(run_id: str, p: dict, status: str, pr_url: str = "") -> None:
    with db() as c:
        c.execute(
            "INSERT INTO proposals (run_id, layer, repo, file, change, why, evidence, confidence, status, pr_url, created)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                p.get("layer"),
                p.get("repo"),
                p.get("file"),
                p.get("change"),
                p.get("why"),
                json.dumps(p.get("evidence", [])),
                float(p.get("confidence", 0)),
                status,
                pr_url,
                util.iso(),
            ),
        )


def proposals(status: list[str] | None = None, limit: int = 200, connect=db) -> list[dict]:
    q, args = "SELECT * FROM proposals", []
    if status:
        q += f" WHERE status IN ({','.join('?' * len(status))})"
        args += status
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def set_proposal_status_by_pr(pr_url: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE proposals SET status = ? WHERE pr_url = ?", (status, pr_url))


def add_eval(run_id: str, repo: str, task: str, passed: bool, seconds: float, cost: float, source: str = "") -> None:
    with db() as c:
        c.execute(
            "INSERT INTO evals (run_id, repo, task, passed, seconds, cost_usd, ts, source) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, repo, task, int(passed), seconds, cost, util.iso(), source),
        )


BASELINE_SOURCE = "worktree"


def task_history(repo: str, task: str, limit: int = 5, source: str = BASELINE_SOURCE) -> list[bool]:
    with db() as c:
        rows = c.execute("SELECT passed FROM evals WHERE repo = ? AND task = ? AND source = ? ORDER BY ts DESC LIMIT ?",
                         (repo, task, source, limit))
        return [bool(r[0]) for r in rows]


def eval_history(repo: str, limit: int = 20) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT run_id, MIN(ts) AS ts, MAX(source) AS source, SUM(passed) AS passed, COUNT(*) AS total,"
            " SUM(cost_usd) AS cost FROM evals WHERE repo = ? GROUP BY run_id ORDER BY ts DESC LIMIT ?",
            (repo, limit),
        )
        return [dict(r) for r in rows]


def evals_since(since: str, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM evals WHERE ts >= ? ORDER BY ts", (since,))]


FEEDBACK_COLUMNS = ("pr_url", "kind", "gh_id", "repo", "issue", "body", "path", "line", "hunk", "thread", "at_once",
                    "created", "pr_state", "status")


def add_feedback(rows: list[dict]) -> int:
    with db() as c:
        before = c.total_changes
        c.executemany(f"INSERT OR IGNORE INTO feedback ({', '.join(FEEDBACK_COLUMNS)}) "
                      f"VALUES ({', '.join('?' * len(FEEDBACK_COLUMNS))})",
                      [tuple(r.get(k) for k in FEEDBACK_COLUMNS) for r in rows])
        return c.total_changes - before


def feedback_due(repo: str, quiet_since: str) -> list[str]:
    with db() as c:
        rows = c.execute("SELECT issue, MAX(at_once) AS at_once, MAX(created) AS last FROM feedback f "
                         "WHERE repo = ? AND status = 'new' AND EXISTS (SELECT 1 FROM runs WHERE runs.repo = f.repo "
                         f"AND runs.issue = f.issue AND {OPEN_PR_RUN} "
                         "AND ' ' || runs.pr_url || ' ' LIKE '% ' || f.pr_url || ' %') "
                         "GROUP BY issue ORDER BY last", (repo,))
        return [str(r["issue"]) for r in rows if r["at_once"] or r["last"] <= quiet_since]


def close_feedback(pr_url: str, pr_state: str) -> None:
    with db() as c:
        c.execute("UPDATE feedback SET pr_state = ?, status = CASE WHEN status IN ('new', 'failed') THEN 'recorded' "
                  "ELSE status END WHERE pr_url = ?", (pr_state, pr_url))


def feedback_waiting(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT repo, issue, COUNT(*) AS n, MAX(at_once) AS at_once, "
                                           "MAX(created) AS last FROM feedback WHERE status = 'new' "
                                           "GROUP BY repo, issue ORDER BY last")]


def open_feedback(pr_urls: list[str]) -> list[dict]:
    if not pr_urls:
        return []
    with db() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM feedback WHERE pr_url IN ({','.join('?' * len(pr_urls))}) "
            "AND status IN ('new', 'taken', 'failed') ORDER BY created", pr_urls)]


def take_feedback(rows: list[dict], run_id: str) -> None:
    with db() as c:
        c.executemany("UPDATE feedback SET status = 'taken', run_id = ? WHERE pr_url = ? AND kind = ? AND gh_id = ?",
                      [(run_id, r["pr_url"], r["kind"], r["gh_id"]) for r in rows])


def settle_feedback(run_id: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE feedback SET status = ? WHERE run_id = ? AND status = 'taken'", (status, run_id))


def feedback_since(since: str) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback WHERE created >= ? ORDER BY pr_url, created",
                                           (since,))]


def add_mined(pr_url: str, repo: str, issue: str, run_id: str, status: str, why: str = "", task: str = "") -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO mined (pr_url, repo, issue, run_id, status, why, task, ts) "
                  "VALUES (?,?,?,?,?,?,?,?)", (pr_url, repo, issue, run_id, status, why, task, util.iso()))


def mined_urls() -> set[str]:
    with db() as c:
        return {r["pr_url"] for r in c.execute("SELECT pr_url FROM mined")}


TRIAGE_COLUMNS = ("ref", "source", "tracker", "repo", "route", "title", "url", "updated", "ready", "code", "size",
                  "questions", "plan", "why", "cost", "ts")


def triage_rows(connect=db) -> list[dict]:
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM triage")]
    for r in rows:
        r["questions"] = json.loads(r["questions"] or "[]")
        r["plan"] = json.loads(r["plan"] or "[]")
    return rows


def triage_replace(source: str, rows: list[dict]) -> None:
    refs = [r["ref"] for r in rows]
    with db() as c:
        c.execute("DELETE FROM triage WHERE source = ?", (source,))
        c.executemany("DELETE FROM triage WHERE ref = ?", [(ref,) for ref in refs])
        c.executemany(f"INSERT INTO triage ({', '.join(TRIAGE_COLUMNS)}) VALUES ({', '.join('?' * len(TRIAGE_COLUMNS))})",
                      [tuple(json.dumps(r[k]) if k in ("questions", "plan") else r[k] for k in TRIAGE_COLUMNS)
                       for r in rows])


def triage_prune(sources: list[str]) -> None:
    with db() as c:
        c.execute(f"DELETE FROM triage WHERE source NOT IN ({','.join('?' * len(sources))})", sources)
