from __future__ import annotations

import math
from datetime import timedelta

from .. import util
from .db import db, reader

TABLES = {
    "seen": "until source forgets",
    "cursors": "kept",
    "source_owners": "kept",
    "source_polls": "until retention prunes",
    "passed_over": "until taken or dropped",
}

_UNSET = object()
_CURSOR_ADD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source_version", "TEXT"),
    ("scope_sha", "TEXT"),
    ("advanced", "TEXT"),
    ("error_streak", "INTEGER NOT NULL DEFAULT 0"),
    ("backoff_until", "TEXT"),
    ("version", "INTEGER NOT NULL DEFAULT 1"),
)


def apply_expand(conn) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(cursors)")}
    for col, col_type in _CURSOR_ADD_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE cursors ADD COLUMN {col} {col_type}")  # noqa: S608
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started TEXT NOT NULL,
            ended TEXT NOT NULL,
            ok INTEGER NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            new INTEGER NOT NULL DEFAULT 0,
            refused INTEGER,
            error TEXT,
            rate_remaining INTEGER,
            retry_after INTEGER,
            backoff_s INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS source_polls_source ON source_polls (source, id)")


def has_durable_sources(c) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'source_polls'"
    ).fetchone() is not None


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return math.ceil(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return math.ceil(parsed) if math.isfinite(parsed) else None
    return None


def _rate_values(rate: object) -> tuple[int | None, int | None]:
    if rate is None:
        return None, None
    if isinstance(rate, dict):
        return _int_or_none(rate.get("remaining")), _int_or_none(rate.get("retry_after"))
    return _int_or_none(getattr(rate, "remaining", None)), _int_or_none(getattr(rate, "retry_after", None))


def source_seen(source: str, connect=reader) -> set[str]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT key FROM seen WHERE source = ?", (source,))}


def claim_seen(source: str, keys: list[str], connect=db) -> list[str]:
    with connect() as c:
        now = util.iso()
        return [
            str(k)
            for k in keys
            if c.execute(
                "INSERT OR IGNORE INTO seen (source, key, ts) VALUES (?,?,?)",
                (source, str(k), now),
            ).rowcount == 1
        ]


def forget_seen(source: str, key: str, before, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM seen WHERE source = ? AND key = ? AND ts < ?", (source, key, util.iso(before)))


def cursor(source: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM cursors WHERE source = ?", (source,)).fetchone()
        return dict(r) if r else None


def set_cursor(source: str, value: str | None, ok: bool, detail: str = "", connect=db, started=None) -> None:
    now = util.iso()
    last_ok_insert = (started or now) if ok else None
    with connect() as c:
        if has_durable_sources(c):
            c.execute(
                "INSERT INTO cursors (source, value, polled, ok, detail, last_ok, version) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(source) DO UPDATE SET "
                "value = COALESCE(excluded.value, cursors.value), "
                "polled = excluded.polled, "
                "ok = excluded.ok, "
                "detail = excluded.detail, "
                "version = COALESCE(cursors.version, 1) + 1",
                (source, value, now, int(ok), detail[:500], last_ok_insert),
            )
        else:
            c.execute(
                "INSERT INTO cursors (source, value, polled, ok, detail, last_ok) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET "
                "value = COALESCE(excluded.value, cursors.value), "
                "polled = excluded.polled, "
                "ok = excluded.ok, "
                "detail = excluded.detail",
                (source, value, now, int(ok), detail[:500], last_ok_insert),
            )


def cursors(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cursors ORDER BY source")]


def source_poll(
    c,
    src,
    started: str,
    ok: bool,
    read: int,
    new: int,
    error: str | None,
    rate: object,
    backoff_s: int | float | None,
    *,
    value: object = _UNSET,
    advanced: bool = False,
    detail: str = "",
    ended: str | None = None,
) -> dict:
    sid = src.id if hasattr(src, "id") else str(src)
    src_version = getattr(src, "version", None)
    scope_sha = getattr(src, "scope_sha", None)
    ended_ts = ended or util.iso()
    rate_remaining, retry_after = _rate_values(rate)
    if ok:
        keep_val = 1 if value is _UNSET else 0
        val_arg = None if value is _UNSET else value
        adv_ts = started if advanced else None
        detail_text = (detail or "ok")[:500]
        c.execute(
            "INSERT INTO cursors ("
            "source, value, polled, ok, detail, last_ok, "
            "source_version, scope_sha, advanced, error_streak, backoff_until, version"
            ") VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 0, NULL, 1) "
            "ON CONFLICT(source) DO UPDATE SET "
            "value = CASE WHEN ? THEN cursors.value ELSE excluded.value END, "
            "polled = excluded.polled, "
            "ok = 1, "
            "detail = excluded.detail, "
            "last_ok = excluded.last_ok, "
            "source_version = excluded.source_version, "
            "scope_sha = excluded.scope_sha, "
            "advanced = COALESCE(excluded.advanced, cursors.advanced), "
            "error_streak = 0, "
            "backoff_until = NULL, "
            "version = COALESCE(cursors.version, 1) + 1",
            (sid, val_arg, ended_ts, detail_text, started, src_version, scope_sha, adv_ts, keep_val),
        )
        c.execute(
            "INSERT INTO source_polls ("
            "source, started, ended, ok, read, new, refused, error, rate_remaining, retry_after, backoff_s"
            ") VALUES (?, ?, ?, 1, ?, ?, NULL, NULL, ?, ?, NULL)",
            (sid, started, ended_ts, int(read), int(new), rate_remaining, retry_after),
        )
    else:
        backoff_int = _int_or_none(backoff_s)
        backoff_until = (
            util.iso(util.parse_iso(ended_ts) + timedelta(seconds=backoff_int))
            if backoff_int is not None and backoff_int > 0
            else None
        )
        err_text = (error or detail or "poll failed")[:500]
        c.execute(
            "INSERT INTO cursors ("
            "source, value, polled, ok, detail, last_ok, "
            "source_version, scope_sha, advanced, error_streak, backoff_until, version"
            ") VALUES (?, NULL, ?, 0, ?, NULL, ?, NULL, NULL, 1, ?, 1) "
            "ON CONFLICT(source) DO UPDATE SET "
            "polled = excluded.polled, "
            "ok = 0, "
            "detail = excluded.detail, "
            "source_version = COALESCE(excluded.source_version, cursors.source_version), "
            "error_streak = COALESCE(cursors.error_streak, 0) + 1, "
            "backoff_until = excluded.backoff_until, "
            "version = COALESCE(cursors.version, 1) + 1",
            (sid, ended_ts, err_text, src_version, backoff_until),
        )
        c.execute(
            "INSERT INTO source_polls ("
            "source, started, ended, ok, read, new, refused, error, rate_remaining, retry_after, backoff_s"
            ") VALUES (?, ?, ?, 0, ?, ?, NULL, ?, ?, ?, ?)",
            (sid, started, ended_ts, int(read), int(new), err_text, rate_remaining, retry_after, backoff_int),
        )
    row = c.execute("SELECT * FROM cursors WHERE source = ?", (sid,)).fetchone()
    return dict(row)


def recent_polls(c, source: str, limit: int = 20) -> list[dict]:
    if not has_durable_sources(c):
        return []
    rows = c.execute(
        "SELECT id, source, started, ended, ok, read, new, refused, error, rate_remaining, retry_after, backoff_s "
        "FROM source_polls WHERE source = ? ORDER BY id DESC LIMIT ?",
        (source, int(limit)),
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "source": r["source"],
            "started": r["started"],
            "ended": r["ended"],
            "ok": bool(r["ok"]),
            "read": int(r["read"]),
            "new": int(r["new"]),
            "refused": int(r["refused"]) if r["refused"] is not None else None,
            "error": r["error"],
            "rate_remaining": int(r["rate_remaining"]) if r["rate_remaining"] is not None else None,
            "retry_after": int(r["retry_after"]) if r["retry_after"] is not None else None,
            "backoff_s": int(r["backoff_s"]) if r["backoff_s"] is not None else None,
        }
        for r in rows
    ]


def latest_rate_limit(c, source: str) -> dict | None:
    if not has_durable_sources(c):
        return None
    row = c.execute(
        "SELECT rate_remaining, retry_after, ended, started FROM source_polls "
        "WHERE source = ? AND (rate_remaining IS NOT NULL OR retry_after IS NOT NULL) "
        "ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return {
        "remaining": int(row["rate_remaining"]) if row["rate_remaining"] is not None else None,
        "retry_after": int(row["retry_after"]) if row["retry_after"] is not None else None,
        "at": row["ended"] or row["started"],
    }


def poll_counts_since(c, source: str, since: str) -> dict[str, int]:
    if not has_durable_sources(c):
        return {"polls": 0, "read": 0, "new": 0}
    row = c.execute(
        "SELECT COUNT(*) AS polls, COALESCE(SUM(read), 0) AS read, COALESCE(SUM(new), 0) AS new "
        "FROM source_polls WHERE source = ? AND started >= ?",
        (source, since),
    ).fetchone()
    if row is None:
        return {"polls": 0, "read": 0, "new": 0}
    return {"polls": int(row["polls"]), "read": int(row["read"]), "new": int(row["new"])}


def prune(c, *, polls_days: float, unused_days: float, active_ids: set[str] | None = None) -> int:
    if not has_durable_sources(c):
        return 0
    now = util.now()
    polls_since = util.iso(now - timedelta(days=polls_days))
    unused_since = util.iso(now - timedelta(days=unused_days))
    removed = c.execute("DELETE FROM source_polls WHERE started < ?", (polls_since,)).rowcount
    if active_ids is None:
        from .. import config, loops, sources

        cfg, problem = config.load_or_problem()
        if problem:
            return removed
        active_ids = {s.id for s in sources.load_all(cfg, loops.load_all(cfg))}
    rows = c.execute("SELECT source, polled FROM cursors WHERE polled IS NOT NULL AND polled < ?", (unused_since,)).fetchall()
    for row in rows:
        sid = str(row["source"])
        if sid.startswith("oncall:") or sid in active_ids:
            continue
        removed += c.execute("DELETE FROM source_polls WHERE source = ?", (sid,)).rowcount
        removed += c.execute("DELETE FROM seen WHERE source = ?", (sid,)).rowcount
        removed += c.execute("DELETE FROM source_owners WHERE source = ?", (sid,)).rowcount
        removed += c.execute("DELETE FROM cursors WHERE source = ?", (sid,)).rowcount
    return removed


def pass_over(repo: str, issue: str) -> None:
    from .. import principals
    from . import tasks as task_store

    with db() as c:
        if task_store.has_identity_expand(c):
            task_num: int | None = None
            try:
                task_num, _ = task_store.TaskStore.task_for_ticket(c, repo, str(issue), principals.current())
            except LookupError:
                task_num = task_store.TaskStore.find_task_num(c, repo, str(issue))
            c.execute(
                "INSERT OR IGNORE INTO passed_over (repo, issue, since, task_id) VALUES (?,?,?,?)",
                (repo, str(issue), util.iso(), task_num),
            )
            if task_num is not None:
                c.execute(
                    "UPDATE passed_over SET task_id = ? WHERE repo = ? AND issue = ? AND task_id IS NULL",
                    (task_num, repo, str(issue)),
                )
        else:
            c.execute("INSERT OR IGNORE INTO passed_over (repo, issue, since) VALUES (?,?,?)",
                      (repo, str(issue), util.iso()))


def tickets_passed_over() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM passed_over ORDER BY since")]


def take_passed_over(repo: str, issue: str) -> str:
    with db() as c:
        r = c.execute("SELECT since FROM passed_over WHERE repo = ? AND issue = ?", (repo, str(issue))).fetchone()
        c.execute("DELETE FROM passed_over WHERE repo = ? AND issue = ?", (repo, str(issue)))
    return r["since"] if r else ""


def drop_passed_over(repo: str, keep: str = "") -> None:
    with db() as c:
        c.execute("DELETE FROM passed_over WHERE repo = ? AND issue != ?", (repo, str(keep)))


def write_scan_cursor(c, source: str, value: str | None, ok: bool, detail: str) -> None:
    now = util.iso()
    if has_durable_sources(c):
        c.execute(
            "INSERT INTO cursors (source, value, polled, ok, detail, version) "
            "VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(source) DO UPDATE SET "
            "value = excluded.value, "
            "polled = excluded.polled, "
            "ok = excluded.ok, "
            "detail = excluded.detail, "
            "version = COALESCE(cursors.version, 1) + 1",
            (source, value, now, int(ok), detail[:500]),
        )
    else:
        c.execute(
            "INSERT INTO cursors (source, value, polled, ok, detail) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "value = excluded.value, "
            "polled = excluded.polled, "
            "ok = excluded.ok, "
            "detail = excluded.detail",
            (source, value, now, int(ok), detail[:500]),
        )
