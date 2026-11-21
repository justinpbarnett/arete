from __future__ import annotations

import secrets
import sqlite3

from .. import util
from . import idempotency, writes
from .db import reader

TABLES = {
    "tasks": "kept",
    "task_comments": "kept",
}


def task_add(repo: str, body: str, title: str = "", kind: str = "feat", parent: str = "", claim=None,
             *, reserved_id: str = "") -> str:
    ts = util.iso()
    digits = 4
    with writes.atomic() as c:
        while True:
            tid = reserved_id or f"t-{secrets.randbits(4 * digits):0{digits}x}"
            try:
                writes.insert("tasks", {"repo": repo, "title": title, "body": body, "kind": kind, "state": "todo",
                                        "parent": parent, "created": ts, "updated": ts}, tid)
                if claim:
                    seq = c.execute("SELECT seq FROM events_log WHERE subject = ? AND kind = 'task.created' "
                                    "ORDER BY seq DESC LIMIT 1", (f"task:{tid}",)).fetchone()[0]
                    idempotency.claim(claim, seq)
                return tid
            except sqlite3.IntegrityError as e:
                if reserved_id or str(e) != "UNIQUE constraint failed: tasks.id":
                    raise
                digits += 1


def task_get(tid: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        return dict(r) if r else None


def tasks(repo: str = "", in_state: str = "", connect=reader) -> list[dict]:
    q, args = "SELECT * FROM tasks WHERE 1=1", []
    for column, value in (("repo", repo), ("state", in_state)):
        if value:
            q += f" AND {column} = ?"
            args.append(value)
    with connect() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY created, rowid", args)]


def task_summaries(connect=reader) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT id, repo, title, kind, state, updated FROM tasks ORDER BY updated DESC, id")
        return [dict(row) for row in rows]


def task_children(parent: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks WHERE parent = ? ORDER BY created, rowid", (parent,))]


def task_set_state(tid: str, new: str, from_states: tuple[str, ...] = (), base_version=None) -> bool:
    return writes.update("tasks", tid, {"state": new, "updated": util.iso()},
                         expect=from_states, base_version=base_version) is not None


def task_update(tid: str, base_version: int, fields: dict) -> dict | None:
    return writes.update("tasks", tid, {**fields, "updated": util.iso()}, base_version=base_version)


def task_comment(tid: str, author: str, body: str) -> bool:
    ts = util.iso()
    with writes.atomic():
        if not writes.update("tasks", tid, {"updated": ts}):
            return False
        writes.insert("task_comments", {"task": tid, "author": author, "body": body, "ts": ts})
        return True


def task_comments(tid: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT author, body, ts FROM task_comments WHERE task = ? ORDER BY rowid",
                                           (tid,))]


def versioned_comments(tid: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT version, author, body, ts FROM task_comments "
                                           "WHERE task = ? ORDER BY rowid", (tid,))]


def tasks_replied(repo: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE repo = ? AND state = 'needs-info' AND "
            "(SELECT author = 'owner' OR author LIKE 'u:%' FROM task_comments c "
            "WHERE c.task = tasks.id ORDER BY c.rowid DESC LIMIT 1) ORDER BY created, rowid", (repo,))]
