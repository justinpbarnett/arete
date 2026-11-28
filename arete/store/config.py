from __future__ import annotations

import json
import sqlite3

from . import db as foundation

TABLES = {"config_index": "kept", "config_drafts": "kept"}

COLUMNS = (
    "kind", "name", "origin", "path", "sha256", "version", "n",
    "commit_sha", "author", "source", "note", "at", "live", "fields", "ver",
)
DELETED_SUFFIX = "@deleted"


def available(conn: sqlite3.Connection | None = None) -> bool:
    if conn is not None:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'config_index'"
        ).fetchone() is not None
    try:
        with foundation.reader() as c:
            return available(c)
    except (foundation.NoDatabase, sqlite3.Error):
        return False


def has_ver(conn: sqlite3.Connection) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(config_index)")}
    return "ver" in cols


def all_rows(conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        with foundation.reader() as c:
            return all_rows(c)
    if not available(conn):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM config_index ORDER BY origin, path, n, sha256"
    )]


def live_rows(conn: sqlite3.Connection | None = None, *, kind: str | None = None) -> list[dict]:
    if conn is None:
        with foundation.reader() as c:
            return live_rows(c, kind=kind)
    if not available(conn):
        return []
    if kind is not None:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM config_index WHERE live = 1 AND kind = ? ORDER BY name, path", (kind,)
        )]
    return [dict(r) for r in conn.execute(
        "SELECT * FROM config_index WHERE live = 1 ORDER BY kind, name, path"
    )]


def live_item(kind: str, name: str, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        with foundation.reader() as c:
            return live_item(kind, name, c)
    if not available(conn):
        return None
    row = conn.execute(
        "SELECT * FROM config_index WHERE live = 1 AND kind = ? AND name = ? ORDER BY ver DESC, n DESC LIMIT 1",
        (kind, name),
    ).fetchone()
    return dict(row) if row else None


def live_by_path(path: str, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        with foundation.reader() as c:
            return live_by_path(path, c)
    if not available(conn):
        return None
    row = conn.execute(
        "SELECT * FROM config_index WHERE live = 1 AND path = ? ORDER BY ver DESC, n DESC LIMIT 1",
        (path,),
    ).fetchone()
    return dict(row) if row else None


def latest_item(kind: str, name: str, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        with foundation.reader() as c:
            return latest_item(kind, name, c)
    if not available(conn):
        return None
    order = "ver DESC, n DESC" if has_ver(conn) else "n DESC"
    row = conn.execute(
        f"SELECT * FROM config_index WHERE kind = ? AND name = ? ORDER BY {order} LIMIT 1",  # noqa: S608
        (kind, name),
    ).fetchone()
    return dict(row) if row else None


def latest_by_path(path: str, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        with foundation.reader() as c:
            return latest_by_path(path, c)
    if not available(conn):
        return None
    order = "ver DESC, n DESC" if has_ver(conn) else "n DESC"
    row = conn.execute(
        f"SELECT * FROM config_index WHERE path = ? ORDER BY {order} LIMIT 1",  # noqa: S608
        (path,),
    ).fetchone()
    return dict(row) if row else None


def versions_of(kind: str, name: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        with foundation.reader() as c:
            return versions_of(kind, name, c)
    if not available(conn):
        return []
    sql = ("SELECT * FROM config_index WHERE kind = ? AND name = ? ORDER BY ver DESC, n DESC"
           if has_ver(conn) else
           "SELECT * FROM config_index WHERE kind = ? AND name = ? ORDER BY n DESC")
    return [dict(r) for r in conn.execute(sql, (kind, name))]


def by_selector(kind: str, name: str, selector: str, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        with foundation.reader() as c:
            return by_selector(kind, name, selector, c)
    if not available(conn):
        return None
    if selector.isascii() and selector.isdecimal() and len(selector) <= 18 and has_ver(conn):
        row = conn.execute(
            "SELECT * FROM config_index WHERE kind = ? AND name = ? AND ver = ? LIMIT 1",
            (kind, name, int(selector)),
        ).fetchone()
        if row:
            return dict(row)
    row = conn.execute(
        "SELECT * FROM config_index WHERE kind = ? AND name = ? AND (version = ? OR commit_sha = ?) LIMIT 1",
        (kind, name, selector, selector),
    ).fetchone()
    return dict(row) if row else None


def indexed_commits(conn: sqlite3.Connection) -> set[str]:
    if not available(conn):
        return set()
    return {row[0] for row in conn.execute("SELECT DISTINCT commit_sha FROM config_index")}


def needs_ver_rebuild(conn: sqlite3.Connection) -> bool:
    if not available(conn) or not has_ver(conn):
        return False
    return conn.execute(
        "SELECT 1 FROM config_index WHERE origin != 'engine' AND sha256 != 'absent' AND ver = 0 LIMIT 1"
    ).fetchone() is not None


def max_ver(conn: sqlite3.Connection, kind: str, name: str) -> int:
    if not has_ver(conn):
        return 0
    row = conn.execute(
        "SELECT COALESCE(MAX(ver), 0) FROM config_index WHERE kind = ? AND name = ?",
        (kind, name),
    ).fetchone()
    return int(row[0]) if row else 0


def next_n(conn: sqlite3.Connection, origin: str, path: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(n), 0) FROM config_index WHERE origin = ? AND path = ?",
        (origin, path),
    ).fetchone()
    return int(row[0]) + 1 if row else 1


def upsert_live(conn: sqlite3.Connection, *, kind: str, name: str, origin: str, path: str, sha256: str,
                version: str, commit_sha: str, author: str, source: str, note: str, at: str,
                fields: dict | str = "{}", ver: int | None = None) -> int:
    with_ver = has_ver(conn)
    if ver is None:
        ver = 0 if origin == "engine" else (max_ver(conn, kind, name) + 1 if with_ver else 0)
    fields_json = fields if isinstance(fields, str) else json.dumps(fields, sort_keys=True)
    conn.execute("UPDATE config_index SET live = 0 WHERE origin = ? AND path = ? AND live = 1", (origin, path))
    existing = conn.execute(
        "SELECT n FROM config_index WHERE origin = ? AND path = ? AND sha256 = ?",
        (origin, path, sha256),
    ).fetchone()
    if existing:
        if with_ver:
            conn.execute(
                "UPDATE config_index SET kind = ?, name = ?, version = ?, commit_sha = ?, author = ?, source = ?, "
                "note = ?, at = ?, live = 1, fields = ?, ver = ? WHERE origin = ? AND path = ? AND sha256 = ?",
                (kind, name, version, commit_sha, author, source, note, at, fields_json, ver, origin, path, sha256),
            )
        else:
            conn.execute(
                "UPDATE config_index SET kind = ?, name = ?, version = ?, commit_sha = ?, author = ?, source = ?, "
                "note = ?, at = ?, live = 1, fields = ? WHERE origin = ? AND path = ? AND sha256 = ?",
                (kind, name, version, commit_sha, author, source, note, at, fields_json, origin, path, sha256),
            )
    else:
        n = next_n(conn, origin, path)
        if with_ver:
            conn.execute(
                "INSERT INTO config_index "
                "(kind, name, origin, path, sha256, version, n, commit_sha, author, source, note, at, live, fields, ver) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (kind, name, origin, path, sha256, version, n, commit_sha, author, source, note, at, fields_json, ver),
            )
        else:
            conn.execute(
                "INSERT INTO config_index "
                "(kind, name, origin, path, sha256, version, n, commit_sha, author, source, note, at, live, fields) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (kind, name, origin, path, sha256, version, n, commit_sha, author, source, note, at, fields_json),
            )
    return ver


def mark_deleted(conn: sqlite3.Connection, origin: str, path: str) -> None:
    conn.execute("UPDATE config_index SET live = 0 WHERE origin = ? AND path = ? AND live = 1", (origin, path))


def record_deleted(
    conn: sqlite3.Connection,
    *,
    kind: str,
    name: str,
    origin: str,
    path: str,
    commit_sha: str,
    author: str,
    source: str,
    note: str,
    at: str,
    ver: int | None = None,
) -> int:
    with_ver = has_ver(conn)
    if ver is None:
        ver = 0 if origin == "engine" else (max_ver(conn, kind, name) + 1 if with_ver else 0)
    tombstone = f"{kind}/{name}{DELETED_SUFFIX}"
    fields_json = '{"deleted":true}'
    mark_deleted(conn, origin, path)
    existing = conn.execute(
        "SELECT n FROM config_index WHERE origin = ? AND path = ? AND sha256 = ''",
        (origin, path),
    ).fetchone()
    if existing:
        if with_ver:
            conn.execute(
                "UPDATE config_index SET kind = ?, name = ?, version = ?, commit_sha = ?, author = ?, source = ?, "
                "note = ?, at = ?, live = 0, fields = ?, ver = ? WHERE origin = ? AND path = ? AND sha256 = ''",
                (kind, name, tombstone, commit_sha, author, source, note, at, fields_json, ver, origin, path),
            )
        else:
            conn.execute(
                "UPDATE config_index SET kind = ?, name = ?, version = ?, commit_sha = ?, author = ?, source = ?, "
                "note = ?, at = ?, live = 0, fields = ? WHERE origin = ? AND path = ? AND sha256 = ''",
                (kind, name, tombstone, commit_sha, author, source, note, at, fields_json, origin, path),
            )
    else:
        n = next_n(conn, origin, path)
        if with_ver:
            conn.execute(
                "INSERT INTO config_index "
                "(kind, name, origin, path, sha256, version, n, commit_sha, author, source, note, at, live, fields, ver) "
                "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (kind, name, origin, path, tombstone, n, commit_sha, author, source, note, at, fields_json, ver),
            )
        else:
            conn.execute(
                "INSERT INTO config_index "
                "(kind, name, origin, path, sha256, version, n, commit_sha, author, source, note, at, live, fields) "
                "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (kind, name, origin, path, tombstone, n, commit_sha, author, source, note, at, fields_json),
            )
    return ver


def replace_all(conn: sqlite3.Connection, rows: list[dict]) -> None:
    with_ver = has_ver(conn)
    conn.execute("DELETE FROM config_index")
    cols = COLUMNS if with_ver else COLUMNS[:-1]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO config_index ({', '.join(cols)}) VALUES ({placeholders})"  # noqa: S608
    for row in rows:
        conn.execute(sql, tuple(row[c] for c in cols))
