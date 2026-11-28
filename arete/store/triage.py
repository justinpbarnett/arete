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
    from .. import principals
    from . import tasks as task_store

    refs = [r["ref"] for r in rows]
    with db() as c:
        has_ver = _has_version(c)
        has_ref = task_store.has_identity_expand(c)
        cols = TRIAGE_COLUMNS if has_ver else TRIAGE_COLUMNS[:-1]
        if has_ref:
            cols = (*cols, "ref_id")
        prev_versions: dict[str, int] = {}
        if has_ver:
            for existing in c.execute("SELECT ref, version FROM triage"):
                prev_versions[existing["ref"]] = int(existing["version"] or 1)
        c.execute("DELETE FROM triage WHERE source = ?", (source,))
        c.executemany("DELETE FROM triage WHERE ref = ?", [(ref,) for ref in refs])
        written = []
        actor = principals.current()
        for r in rows:
            item = dict(r)
            if has_ver:
                prev = prev_versions.get(r["ref"])
                item["version"] = prev + 1 if prev is not None else 1
            if has_ref and item.get("ref_id") is None:
                item["ref_id"] = task_store.TaskStore.ref_for_triage(c, item, actor=actor)
            written.append(lists_written(item, cols, TRIAGE_LISTS))
        c.executemany(insert_sql("triage", cols), written)


def triage_put(row: dict) -> None:
    from .. import principals
    from . import tasks as task_store

    with db() as c:
        has_ver = _has_version(c)
        has_ref = task_store.has_identity_expand(c)
        cols = TRIAGE_COLUMNS if has_ver else TRIAGE_COLUMNS[:-1]
        if has_ref:
            cols = (*cols, "ref_id")
        item = dict(row)
        if has_ver:
            prev = c.execute("SELECT version FROM triage WHERE ref = ?", (row["ref"],)).fetchone()
            item["version"] = int(prev["version"] or 1) + 1 if prev is not None else 1
        if has_ref and item.get("ref_id") is None:
            item["ref_id"] = task_store.TaskStore.ref_for_triage(c, item, actor=principals.current())
        c.execute(insert_sql("triage", cols, "INSERT OR REPLACE"), lists_written(item, cols, TRIAGE_LISTS))


def triage_drop(ref: str) -> None:
    with db() as c:
        c.execute("DELETE FROM triage WHERE ref = ?", (ref,))


def triage_approve(row: dict, sent_by: str) -> None:
    from .. import principals
    from . import tasks as task_store

    with db() as c:
        has_ref = task_store.has_identity_expand(c)
        cols = (*APPROVAL_COLUMNS, "ref_id") if has_ref else APPROVAL_COLUMNS
        item = {**row, "ts": util.iso(), "sent_by": sent_by}
        if has_ref and item.get("ref_id") is None:
            item["ref_id"] = task_store.TaskStore.ref_for_triage(c, item, actor=principals.current())
        c.execute(insert_sql("triage_approvals", cols, "INSERT OR REPLACE"),
                  lists_written(item, cols, APPROVAL_LISTS))


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
