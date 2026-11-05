from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from . import paths, util

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- pipeline | miner | audit | eval
    repo TEXT,
    issue INTEGER,
    status TEXT NOT NULL,          -- running | pr | merged | failed | rejected | noop
    started TEXT NOT NULL,
    finished TEXT,
    attempts INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    pr_url TEXT,
    branch TEXT,
    summary TEXT
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
"""
MIGRATIONS = [("evals", "source", "ALTER TABLE evals ADD COLUMN source TEXT DEFAULT ''")]


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


def new_run_id(kind: str) -> str:
    return f"{kind[:3]}-{util.now().strftime('%Y%m%d-%H%M%S')}"


def start_run(run_id: str, kind: str, repo: str | None = None, issue: int | str | None = None) -> None:
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO runs (id, kind, repo, issue, status, started) VALUES (?,?,?,?,?,?)",
            (run_id, kind, repo, issue, "running", util.iso()),
        )


def finish_run(run_id: str, status: str, **fields) -> None:
    fields = {"status": status, "finished": util.iso(), **fields}
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def update_run(run_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def runs(kind: str | None = None, repo: str | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT * FROM runs WHERE 1=1", []
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    if repo:
        q += " AND repo = ?"
        args.append(repo)
    q += " ORDER BY started DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        return [dict(r) for r in c.execute(q, args)]


def open_runs(repo: str, issue: int | str) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE repo = ? AND issue = ? AND status = 'pr' "
                                           "ORDER BY started DESC", (repo, issue))]


def run_lock(kind: str, repo: str | None = None, issue: int | str | None = None) -> str:
    return {"pipeline": f"run-{repo}-{issue}", "eval": f"eval-{repo}", "miner": "mine"}[kind]


def runs_marked_running() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY started")]


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


def alerts() -> list[dict]:
    with db() as c:
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


def proposals(status: list[str] | None = None, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM proposals", []
    if status:
        q += f" WHERE status IN ({','.join('?' * len(status))})"
        args += status
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as c:
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


def task_history(repo: str, task: str, limit: int = 5) -> list[bool]:
    with db() as c:
        rows = c.execute("SELECT passed FROM evals WHERE repo = ? AND task = ? ORDER BY ts DESC LIMIT ?",
                         (repo, task, limit))
        return [bool(r[0]) for r in rows]


def eval_history(repo: str, limit: int = 20) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT run_id, MIN(ts) AS ts, MAX(source) AS source, SUM(passed) AS passed, COUNT(*) AS total,"
            " SUM(cost_usd) AS cost FROM evals WHERE repo = ? GROUP BY run_id ORDER BY ts DESC LIMIT ?",
            (repo, limit),
        )
        return [dict(r) for r in rows]
