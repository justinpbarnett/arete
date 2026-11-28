from __future__ import annotations

import sqlite3

from .. import util
from . import db as foundation

TABLES = {"derived": "kept"}


def available(conn: sqlite3.Connection | None = None) -> bool:
    if conn is not None:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'derived'"
            ).fetchone()
            is not None
        )
    try:
        with foundation.reader() as c:
            return available(c)
    except (foundation.NoDatabase, sqlite3.Error):
        return False


def get(name: str, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        with foundation.reader() as c:
            return get(name, c)
    if not available(conn):
        return None
    row = conn.execute("SELECT * FROM derived WHERE name = ?", (name,)).fetchone()
    return dict(row) if row is not None else None


def all_rows(conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        with foundation.reader() as c:
            return all_rows(c)
    if not available(conn):
        return []
    return [dict(r) for r in conn.execute("SELECT * FROM derived ORDER BY name")]


def record_rebuilt(
    conn: sqlite3.Connection,
    name: str,
    *,
    code_version: int,
    source_mark: str,
    rebuilt: str | None = None,
    checked: str | None = None,
    diff_rows: int = 0,
) -> dict:
    stamp = util.iso() if rebuilt is None else rebuilt
    conn.execute(
        "INSERT INTO derived (name, code_version, source_mark, rebuilt, checked, diff_rows) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "code_version = excluded.code_version, "
        "source_mark = excluded.source_mark, "
        "rebuilt = excluded.rebuilt, "
        "checked = excluded.checked, "
        "diff_rows = excluded.diff_rows",
        (name, int(code_version), str(source_mark), stamp, checked, int(diff_rows)),
    )
    row = conn.execute("SELECT * FROM derived WHERE name = ?", (name,)).fetchone()
    return dict(row)


def record_checked(
    conn: sqlite3.Connection,
    name: str,
    *,
    code_version: int,
    source_mark: str,
    diff_rows: int,
    checked: str | None = None,
) -> dict:
    stamp = util.iso() if checked is None else checked
    updated = conn.execute(
        "UPDATE derived SET source_mark = ?, checked = ?, diff_rows = ? WHERE name = ?",
        (str(source_mark), stamp, int(diff_rows), name),
    ).rowcount
    if not updated:
        conn.execute(
            "INSERT INTO derived (name, code_version, source_mark, rebuilt, checked, diff_rows) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (name, int(code_version), str(source_mark), stamp, int(diff_rows)),
        )
    row = conn.execute("SELECT * FROM derived WHERE name = ?", (name,)).fetchone()
    return dict(row)
