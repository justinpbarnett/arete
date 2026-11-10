from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from . import paths, util
from .agent import SUBSCRIPTION_RUNNER

OPEN_PR_RUN = "(runs.status = 'pr' OR (runs.status = 'failed' AND runs.pr_url != ''))"
DECIDABLE = ("pending", "failed")
APPROVAL_NOTES = ("plain_summary", "plain_summary_tried", "review", "review_tried")
PAID_STEP = f"COALESCE(steps.runner, '') != '{SUBSCRIPTION_RUNNER}'"
USAGE_COLUMNS = ("at", "tab", "device", "kind", "view", "name", "via", "ms")
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
    fingerprint_text TEXT DEFAULT '',
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
CREATE TABLE IF NOT EXISTS source_owners (
    source TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
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
    decided TEXT,
    plain_summary TEXT DEFAULT '',
    plain_summary_tried TEXT DEFAULT '',
    review TEXT DEFAULT '',
    review_tried TEXT DEFAULT ''
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
CREATE TABLE IF NOT EXISTS holds (
    scope TEXT PRIMARY KEY,        -- claude | opus | sonnet
    until TEXT NOT NULL,
    why TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    issue INTEGER,
    opened TEXT,
    last_seen TEXT,
    repo TEXT DEFAULT ''
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
    status TEXT,                   -- new | taken | done | failed | recorded | skipped
    run_id TEXT,
    asks REAL,
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
    ts TEXT,
    kind TEXT DEFAULT '',
    repos TEXT DEFAULT '[]',
    assumptions TEXT DEFAULT '[]',
    in_sprint INTEGER DEFAULT 0,
    parent TEXT DEFAULT '',
    closed TEXT DEFAULT ''         -- delivery time of a research ticket shown for 7 days after it left the open ones
);
CREATE TABLE IF NOT EXISTS triage_approvals (
    ref TEXT PRIMARY KEY,
    repo TEXT,
    route TEXT,
    title TEXT,
    url TEXT,
    kind TEXT,
    size TEXT,
    plan TEXT,
    assumptions TEXT,
    why TEXT,
    ts TEXT,
    tracker TEXT DEFAULT 'jira',
    sent_by TEXT DEFAULT 'triage'  -- triage | owner
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    run_id TEXT,
    loop TEXT,
    kind TEXT,                     -- push | proof | fixed
    subject TEXT,
    about TEXT,
    answers TEXT,
    held TEXT,
    outcome TEXT DEFAULT '',       -- '' | replaced | waiting | landed | failed | approved | rejected | skipped | went on
    outcome_at TEXT,
    sha TEXT DEFAULT '',
    approval INTEGER,
    UNIQUE (run_id, kind, subject)
);
CREATE INDEX IF NOT EXISTS verdicts_at ON verdicts (at);
CREATE TABLE IF NOT EXISTS research (
    ref TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    url TEXT,
    updated TEXT,
    status TEXT,                   -- running | done | failed | held | parent | answered
    summary TEXT,
    report TEXT,
    error TEXT,
    run_id TEXT,
    cost REAL DEFAULT 0,
    ts TEXT,
    ticket TEXT DEFAULT '',
    basis TEXT DEFAULT '',
    answered_by TEXT DEFAULT '',
    filed_path TEXT DEFAULT '',
    filed_url TEXT DEFAULT '',
    moved TEXT DEFAULT '',         -- the state arete last moved the ticket into: running | failed | pr | done
    delivered TEXT DEFAULT ''      -- when arete moved the ticket to its done status
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    tab TEXT,                      -- one browser tab's random id
    device TEXT,                   -- desktop | phone
    kind TEXT,                     -- visit | action
    view TEXT,
    name TEXT,                     -- the action's name; empty on a visit
    via TEXT,                      -- how a visit began: start | sidebar | key | palette | link | back | close | step | return
    ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_at ON usage (at);
CREATE TABLE IF NOT EXISTS vms (
    name TEXT PRIMARY KEY,         -- a VM arete asked for and has not yet seen deleted
    run_id TEXT,                   -- NULL for a probe
    device TEXT NOT NULL,          -- JSON of the device that asked, so its host stays reachable if devices.toml changes
    vmid INTEGER,                  -- proxmox only
    created TEXT NOT NULL,
    answered INTEGER DEFAULT 0,    -- 1 once the host answered the create, so it cannot still finish it later
    failing_since TEXT             -- when a delete of it first failed
);
"""
MIGRATIONS = [("evals", "source", "ALTER TABLE evals ADD COLUMN source TEXT DEFAULT ''"),
              ("runs", "landed", "ALTER TABLE runs ADD COLUMN landed TEXT"),
              ("triage", "code", "ALTER TABLE triage ADD COLUMN code INTEGER"),
              ("triage", "kind", "ALTER TABLE triage ADD COLUMN kind TEXT DEFAULT ''"),
              ("triage", "repos", "ALTER TABLE triage ADD COLUMN repos TEXT DEFAULT '[]'"),
              ("triage", "assumptions", "ALTER TABLE triage ADD COLUMN assumptions TEXT DEFAULT '[]'"),
              ("triage", "in_sprint", "ALTER TABLE triage ADD COLUMN in_sprint INTEGER DEFAULT 0"),
              ("triage", "parent", "ALTER TABLE triage ADD COLUMN parent TEXT DEFAULT ''"),
              ("feedback", "asks", "ALTER TABLE feedback ADD COLUMN asks REAL"),
              ("triage_approvals", "tracker", "ALTER TABLE triage_approvals ADD COLUMN tracker TEXT DEFAULT 'jira'"),
              ("triage_approvals", "sent_by", "ALTER TABLE triage_approvals ADD COLUMN sent_by TEXT DEFAULT 'triage'"),
              *[("research", col, f"ALTER TABLE research ADD COLUMN {col} TEXT DEFAULT ''")
                for col in ("ticket", "basis", "answered_by", "filed_path", "filed_url", "moved", "delivered")],
              ("triage", "closed", "ALTER TABLE triage ADD COLUMN closed TEXT DEFAULT ''"),
              *[("approvals", col, f"ALTER TABLE approvals ADD COLUMN {col} TEXT DEFAULT ''")
                for col in ("plain_summary", "plain_summary_tried", "review", "review_tried")],
              ("findings", "fingerprint_text", "ALTER TABLE findings ADD COLUMN fingerprint_text TEXT DEFAULT ''"),
              ("alerts", "repo", "ALTER TABLE alerts ADD COLUMN repo TEXT DEFAULT ''"),
              *[("runs", col, f"ALTER TABLE runs ADD COLUMN {col} {kind}") for col, kind in (
                  ("loop", "TEXT"), ("loop_kind", "TEXT"), ("model", "TEXT"), ("runner", "TEXT"), ("device", "TEXT"),
                  ("tokens_in", "INTEGER DEFAULT 0"), ("tokens_out", "INTEGER DEFAULT 0"), ("log", "TEXT"),
                  ("trigger", "TEXT"), ("queue", "TEXT"), ("grp", "TEXT"), ("pid", "INTEGER"), ("lock", "TEXT"),
                  ("stop_asked", "TEXT"), ("moved", "TEXT"))]]


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


def ensure_schema() -> None:
    with db():
        pass


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
         firings: bool = False, statuses: tuple[str, ...] = (), running_first: bool = False) -> list[dict]:
    q, args = "SELECT * FROM runs WHERE 1=1", []
    if not firings:
        q += f" AND NOT {FIRINGS}"
    for column, value in (("kind", kind), ("loop", loop), ("repo", repo)):
        if value:
            q += f" AND {column} = ?"
            args.append(value)
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    q += " ORDER BY " + ("status = 'running' DESC, " if running_first else "") + "started DESC, rowid DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def run_facets(connect=db) -> dict[str, list[str]]:
    with connect() as c:
        return {column: [r[0] for r in c.execute(f"SELECT DISTINCT {column} FROM runs WHERE NOT {FIRINGS} "
                                                 f"AND COALESCE({column}, '') != '' ORDER BY 1")]
                for column in ("kind", "repo", "loop")}


def paid_by_run(run_ids: list[str], connect=db) -> dict[str, float]:
    if not run_ids:
        return {}
    with connect() as c:
        rows = c.execute(f"SELECT run_id, SUM(cost_usd) FROM steps WHERE run_id IN ({','.join('?' * len(run_ids))}) "
                         f"AND {PAID_STEP} GROUP BY run_id", run_ids)
        return {r[0]: round(float(r[1] or 0), 6) for r in rows}


def runs_since(since: str, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE started >= ? ORDER BY started", (since,))]


def runs_reading_pr_comments(rejected_since: str, limit: int = 200) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND pr_url != '' "
                                           f"AND ({OPEN_PR_RUN} OR (status = 'rejected' AND finished >= ?)) "
                                           "ORDER BY started DESC LIMIT ?", (rejected_since, limit))]


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


def steps_since(since: str, connect=db, paid_only: bool = False) -> list[dict]:
    paid = f" AND {PAID_STEP}" if paid_only else ""
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT steps.*, r.loop, r.kind, r.repo FROM steps LEFT JOIN runs r "
                                           f"ON r.id = steps.run_id WHERE steps.ts >= ?{paid} ORDER BY steps.ts",
                                           (since,))]


def add_feed(run_id: str, kind: str, text: str) -> None:
    with db() as c:
        insert_feed(c, run_id, kind, text)


def insert_feed(c: sqlite3.Connection, run_id: str, kind: str, text: str) -> None:
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


def record_finding(loop: str, fingerprint: str, fingerprint_text: str, title: str, url: str, run_id: str) -> bool:
    with db() as c:
        before = c.total_changes
        c.execute("INSERT OR IGNORE INTO findings (loop, fingerprint, fingerprint_text, title, url, run_id, first_seen, "
                  "last_seen) VALUES (?,?,?,?,?,?,?,?)",
                  (loop, fingerprint, fingerprint_text, title, url, run_id, util.iso(), util.iso()))
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


def source_owner(source: str) -> str | None:
    with db() as c:
        r = c.execute("SELECT fingerprint FROM source_owners WHERE source = ?", (source,)).fetchone()
        return r[0] if r else None


def set_source_owner(source: str, fingerprint: str) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO source_owners (source, fingerprint) VALUES (?,?)", (source, fingerprint))


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


def release_claimed_items(run_id: str) -> None:
    with db() as c:
        c.execute("UPDATE queue SET run_id = NULL WHERE run_id = ?", (run_id,))


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


def add_approval(loop: str, run_id: str, emit: dict, summary: str, failed: str = "") -> int:
    with db() as c:
        cur = c.execute("INSERT INTO approvals (loop, run_id, emit, summary, status, result, created) "
                        "VALUES (?,?,?,?,?,?,?)", (loop, run_id, json.dumps(emit, default=str), summary[:2000],
                                                   "failed" if failed else "pending", failed[:1000], util.iso()))
        return cur.lastrowid


def approvals(connect=db) -> list[dict]:
    with connect() as c:
        return [approval_of(r) for r in c.execute("SELECT a.*, COALESCE(r.repo, '') AS run_repo FROM approvals a "
                                                 "LEFT JOIN runs r ON r.id = a.run_id "
                                                 f"WHERE a.status IN ({','.join('?' * len(DECIDABLE))}) "
                                                 "ORDER BY a.id DESC LIMIT 100", DECIDABLE)]


def set_approval_note(approval_id: int, note: str, value: str, while_in: tuple[str, ...] = DECIDABLE) -> bool:
    if note not in APPROVAL_NOTES:
        raise ValueError(f"an approval has no note {note}; it has {', '.join(APPROVAL_NOTES)}")
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET {note} = ? WHERE id = ? AND status IN ({','.join('?' * len(while_in))})",
                  (value, approval_id, *while_in))
        return c.total_changes > before


def approval(approval_id: int) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return approval_of(r) if r else None


def approval_of(row) -> dict:
    return {**dict(row), "emit": json.loads(row["emit"] or "{}"), "review": json.loads(row["review"] or "{}")}


def decide_approval(approval_id: int, status: str, result: str = "", came_from: tuple[str, ...] = DECIDABLE) -> bool:
    came_from = ("approved",) if status in ("done", "failed") else came_from
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET status = ?, result = ?, decided = ? WHERE id = ? AND status IN "
                  f"({','.join('?' * len(came_from))})", (status, result[:1000], util.iso(), approval_id, *came_from))
        return c.total_changes > before


def add_verdict(run_id: str, kind: str, subject: str, about: str, answers: dict, held: str,
                outcome: str = "") -> None:
    with db() as c:
        c.execute("INSERT INTO verdicts (at, run_id, loop, kind, subject, about, answers, held, outcome, outcome_at) "
                  "VALUES (?,?,(SELECT loop FROM runs WHERE id = ?),?,?,?,?,?,?,?) "
                  "ON CONFLICT (run_id, kind, subject) DO UPDATE SET at = excluded.at, about = excluded.about, "
                  "answers = excluded.answers, held = excluded.held, outcome = excluded.outcome, "
                  "outcome_at = excluded.outcome_at, sha = '', approval = NULL",
                  (util.iso(), run_id, run_id, kind, subject, about[:300], json.dumps(answers), held[:1000], outcome,
                   util.iso() if outcome else None))


def settle_verdicts(run_id: str, outcome: str, approval: int | None = None, was: tuple[str, ...] = ("",)) -> None:
    with db() as c:
        c.execute(f"UPDATE verdicts SET outcome = ?, outcome_at = ?, approval = COALESCE(?, approval) "
                  f"WHERE run_id = ? AND kind IN ('push', 'proof') AND outcome IN ({','.join('?' * len(was))})",
                  (outcome, util.iso(), approval, run_id, *was))


def set_verdict_sha(run_id: str, sha: str) -> None:
    with db() as c:
        c.execute("UPDATE verdicts SET sha = ? WHERE run_id = ? AND kind IN ('push', 'proof') AND outcome != 'replaced'",
                  (sha, run_id))


def verdicts_since(since: str, connect=db) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "answers": json.loads(r["answers"] or "{}")}
                for r in c.execute("SELECT * FROM verdicts WHERE at >= ? ORDER BY at", (since,))]


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
    q = ("SELECT COALESCE(SUM(steps.cost_usd), 0) FROM steps JOIN runs r ON r.id = steps.run_id "
         f"WHERE steps.ts >= ? AND r.kind = 'loop' AND {PAID_STEP}")
    args: list = [since]
    if loop:
        q += " AND r.loop = ?"
        args.append(loop)
    with db() as c:
        return float(c.execute(q, args).fetchone()[0])


def hold(scope: str, until: str, why: str) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO holds (scope, until, why, ts) VALUES (?,?,?,?)", (scope, until, why, util.iso()))


def holds(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM holds WHERE until > ? ORDER BY until", (util.iso(),))]


def hold_until(scope: str) -> str:
    with db() as c:
        r = c.execute("SELECT until FROM holds WHERE scope = ? AND until > ?", (scope, util.iso())).fetchone()
    return r["until"] if r else ""


def clear_holds(scopes: list[str], set_before: str) -> None:
    with db() as c:
        c.executemany("DELETE FROM holds WHERE scope = ? AND ts < ?", [(s, set_before) for s in scopes])


def runs_marked_running(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY started")]


def add_vm(name: str, run_id: str | None, device: dict, vmid: int | None = None, connect=db) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO vms (name, run_id, device, vmid, created) VALUES (?,?,?,?,?)",
                  (name, run_id, json.dumps(device), vmid, util.iso()))


def note_vm_answered(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("UPDATE vms SET answered = 1 WHERE name = ?", (name,))


def forget_vm(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM vms WHERE name = ?", (name,))


def forget_gone_vm(name: str, unanswered_before: str, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM vms WHERE name = ? AND (answered = 1 OR created < ?)", (name, unanswered_before))


def note_delete_failed(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("UPDATE vms SET failing_since = COALESCE(failing_since, ?) WHERE name = ?", (util.iso(), name))


def vms_of_run(run_id: str, connect=db) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "device": json.loads(r["device"])}
                for r in c.execute("SELECT * FROM vms WHERE run_id = ? ORDER BY created", (run_id,))]


def vms_left_over(probes_created_before: str, connect=db) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "device": json.loads(r["device"])} for r in c.execute(
            "SELECT * FROM vms v WHERE (v.run_id IS NULL AND v.created < ?) OR (v.run_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM runs r WHERE r.id = v.run_id AND r.status = 'running')) ORDER BY created",
            (probes_created_before,))]


def ticket_runs(repo: str, issue: int | str, limit: int = 10, connect=db) -> list[dict]:
    with connect() as c:
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


def alert_set(key: str, issue: int | None, repo: str = "") -> None:
    prev = alert_get(key)
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO alerts (key, issue, opened, last_seen, repo) VALUES (?,?,?,?,?)",
            (key, issue, (prev or {}).get("opened") or util.iso(), util.iso(), repo or (prev or {}).get("repo") or ""),
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


def forget_proposal(proposal_id: int) -> bool:
    with db() as c:
        return c.execute("UPDATE proposals SET status = 'filed' WHERE id = ? AND status IN ('memory', 'hand')",
                         (proposal_id,)).rowcount > 0


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
                    "created", "pr_state", "status", "asks")


def feedback_keys(pr_url: str) -> set[tuple[str, int]]:
    with db() as c:
        return {(r["kind"], r["gh_id"]) for r in c.execute("SELECT kind, gh_id FROM feedback WHERE pr_url = ?",
                                                           (pr_url,))}


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
                                           "MAX(CASE WHEN at_once THEN created END) AS submitted, "
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
                  "questions", "plan", "why", "cost", "ts", "kind", "repos", "assumptions", "in_sprint", "parent",
                  "closed")
TRIAGE_LISTS = ("questions", "plan", "repos", "assumptions")
APPROVAL_COLUMNS = ("ref", "repo", "route", "title", "url", "kind", "size", "plan", "assumptions", "why", "ts", "tracker",
                    "sent_by")
APPROVAL_LISTS = ("plan", "assumptions")
RESEARCH_COLUMNS = ("ref", "source", "title", "url", "updated", "status", "summary", "report", "error", "run_id",
                    "cost", "ts", "ticket", "basis", "answered_by", "filed_path", "filed_url", "moved", "delivered")


def lists_read(row: dict, keys: tuple[str, ...]) -> dict:
    return {**row, **{k: json.loads(row.get(k) or "[]") for k in keys}}


def lists_written(row: dict, columns: tuple[str, ...], keys: tuple[str, ...]) -> tuple:
    return tuple(json.dumps(row.get(k) or []) if k in keys else row.get(k) for k in columns)


def insert_sql(table: str, columns: tuple[str, ...], verb: str = "INSERT") -> str:
    return f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"


def triage_rows(connect=db) -> list[dict]:
    with connect() as c:
        return [lists_read(dict(r), TRIAGE_LISTS) for r in c.execute("SELECT * FROM triage")]


def triage_replace(source: str, rows: list[dict]) -> None:
    refs = [r["ref"] for r in rows]
    with db() as c:
        c.execute("DELETE FROM triage WHERE source = ?", (source,))
        c.executemany("DELETE FROM triage WHERE ref = ?", [(ref,) for ref in refs])
        c.executemany(insert_sql("triage", TRIAGE_COLUMNS),
                      [lists_written(r, TRIAGE_COLUMNS, TRIAGE_LISTS) for r in rows])


def triage_put(row: dict) -> None:
    with db() as c:
        c.execute(insert_sql("triage", TRIAGE_COLUMNS, "INSERT OR REPLACE"), lists_written(row, TRIAGE_COLUMNS, TRIAGE_LISTS))


def triage_drop(ref: str) -> None:
    with db() as c:
        c.execute("DELETE FROM triage WHERE ref = ?", (ref,))


def triage_approve(row: dict, sent_by: str) -> None:
    with db() as c:
        c.execute(insert_sql("triage_approvals", APPROVAL_COLUMNS, "INSERT OR REPLACE"),
                  lists_written({**row, "ts": util.iso(), "sent_by": sent_by}, APPROVAL_COLUMNS, APPROVAL_LISTS))


def triage_approvals(since: str = "", connect=db) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM triage_approvals WHERE ts >= ? ORDER BY ts DESC", (since,))
        return [lists_read(dict(r), APPROVAL_LISTS) for r in rows]


def triage_approval(ref: str, connect=db) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM triage_approvals WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return lists_read(dict(r), APPROVAL_LISTS) if r else None


def research_rows(connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM research ORDER BY ts DESC")]


def research_get(ref: str, connect=db) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM research WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return dict(r) if r else None


def research_set(ref: str, **fields) -> None:
    row = {**(research_get(ref) or {"ref": ref, "cost": 0.0}), **fields, "ts": util.iso()}
    with db() as c:
        c.execute(insert_sql("research", RESEARCH_COLUMNS, "INSERT OR REPLACE"),
                  tuple(row.get(k) for k in RESEARCH_COLUMNS))


def research_keep(ref: str, **fields) -> None:
    with db() as c:
        c.execute(f"UPDATE research SET {', '.join(f'{k} = ?' for k in fields)} WHERE ref = ? COLLATE NOCASE",
                  (*fields.values(), ref))


def research_prune(refs: list[str]) -> None:
    with db() as c:
        c.execute(f"DELETE FROM research WHERE status != 'running' AND ref NOT IN ({','.join('?' * len(refs))})", refs)


def triage_prune(sources: list[str]) -> None:
    with db() as c:
        c.execute(f"DELETE FROM triage WHERE source NOT IN ({','.join('?' * len(sources))})", sources)


def add_usage(row: dict, keep_days: float) -> None:
    with db() as c:
        c.execute(insert_sql("usage", USAGE_COLUMNS), tuple(row.get(k) for k in USAGE_COLUMNS))
        c.execute("DELETE FROM usage WHERE at < ?", (util.iso(util.now() - timedelta(days=keep_days)),))


def usage_since(since: str, connect=db) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM usage WHERE at >= ? ORDER BY at", (since,))]
