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
                  "closed", "checks", "version")


TRIAGE_LISTS = ("questions", "plan", "repos", "assumptions", "checks")


APPROVAL_COLUMNS = ("ref", "repo", "route", "title", "url", "kind", "size", "plan", "assumptions", "why", "ts", "tracker",
                    "sent_by", "checks")


APPROVAL_LISTS = ("plan", "assumptions", "checks")


def _has_version(c) -> bool:
    return "version" in {row[1] for row in c.execute("PRAGMA table_info(triage)").fetchall()}


def triage_rows(connect=reader) -> list[dict]:
    with connect() as c:
        return [lists_read({**dict(r), "version": int(r["version"]) if "version" in r.keys() and r["version"] else 1}, TRIAGE_LISTS)
                for r in c.execute("SELECT * FROM triage")]


def triage_replace(source: str, rows: list[dict]) -> None:
    refs = [r["ref"] for r in rows]
    with db() as c:
        has_ver = _has_version(c)
        cols = TRIAGE_COLUMNS if has_ver else TRIAGE_COLUMNS[:-1]
        prev_versions: dict[str, int] = {}
        if has_ver:
            for existing in c.execute("SELECT ref, version FROM triage"):
                prev_versions[existing["ref"]] = int(existing["version"] or 1)
        c.execute("DELETE FROM triage WHERE source = ?", (source,))
        c.executemany("DELETE FROM triage WHERE ref = ?", [(ref,) for ref in refs])
        written = []
        for r in rows:
            item = dict(r)
            if has_ver:
                prev = prev_versions.get(r["ref"])
                item["version"] = prev + 1 if prev is not None else 1
            written.append(lists_written(item, cols, TRIAGE_LISTS))
        c.executemany(insert_sql("triage", cols), written)


def triage_put(row: dict) -> None:
    with db() as c:
        has_ver = _has_version(c)
        cols = TRIAGE_COLUMNS if has_ver else TRIAGE_COLUMNS[:-1]
        item = dict(row)
        if has_ver:
            prev = c.execute("SELECT version FROM triage WHERE ref = ?", (row["ref"],)).fetchone()
            item["version"] = int(prev["version"] or 1) + 1 if prev is not None else 1
        c.execute(insert_sql("triage", cols, "INSERT OR REPLACE"), lists_written(item, cols, TRIAGE_LISTS))


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
        c.execute(f"DELETE FROM triage WHERE source NOT IN ({','.join('?' * len(sources))})", sources)  # noqa: S608
