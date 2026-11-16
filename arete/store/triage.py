from __future__ import annotations

from .. import util
from ._rows import insert_sql, lists_read, lists_written
from .db import db, reader

TABLES = {
    "triage": "current source rows",
    "triage_approvals": "kept",
}


TRIAGE_COLUMNS = ("ref", "source", "tracker", "repo", "route", "title", "url", "updated", "ready", "code", "size",
                  "questions", "plan", "why", "cost", "ts", "kind", "repos", "assumptions", "in_sprint", "parent",
                  "closed", "checks")


TRIAGE_LISTS = ("questions", "plan", "repos", "assumptions", "checks")


APPROVAL_COLUMNS = ("ref", "repo", "route", "title", "url", "kind", "size", "plan", "assumptions", "why", "ts", "tracker",
                    "sent_by", "checks")


APPROVAL_LISTS = ("plan", "assumptions", "checks")


def triage_rows(connect=reader) -> list[dict]:
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


def triage_approvals(since: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM triage_approvals WHERE ts >= ? ORDER BY ts DESC", (since,))
        return [lists_read(dict(r), APPROVAL_LISTS) for r in rows]


def triage_approval(ref: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM triage_approvals WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return lists_read(dict(r), APPROVAL_LISTS) if r else None


def triage_prune(sources: list[str]) -> None:
    with db() as c:
        c.execute(f"DELETE FROM triage WHERE source NOT IN ({','.join('?' * len(sources))})", sources)
