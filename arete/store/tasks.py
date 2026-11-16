from __future__ import annotations

import secrets
import sqlite3

from .. import util
from .db import db, reader

TABLES = {
    "tasks": "kept",
    "task_comments": "kept",
}


def task_add(repo: str, body: str, title: str = "", kind: str = "feat", parent: str = "") -> str:
    ts = util.iso()
    digits = 4
    with db() as c:
        while True:
            tid = f"t-{secrets.randbits(4 * digits):0{digits}x}"
            try:
                c.execute("INSERT INTO tasks (id, repo, title, body, kind, state, parent, created, updated) "
                          "VALUES (?,?,?,?,?,'todo',?,?,?)", (tid, repo, title, body, kind, parent, ts, ts))
                return tid
            except sqlite3.IntegrityError:
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


def task_children(parent: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks WHERE parent = ? ORDER BY created, rowid", (parent,))]


def task_set_state(tid: str, new: str, from_states: tuple[str, ...] = ()) -> bool:
    q, args = "UPDATE tasks SET state = ?, updated = ? WHERE id = ?", [new, util.iso(), tid]
    if from_states:
        q += f" AND state IN ({','.join('?' * len(from_states))})"
        args.extend(from_states)
    with db() as c:
        return c.execute(q, args).rowcount > 0


def task_comment(tid: str, author: str, body: str) -> bool:
    ts = util.iso()
    with db() as c:
        if not c.execute("UPDATE tasks SET updated = ? WHERE id = ?", (ts, tid)).rowcount:
            return False
        c.execute("INSERT INTO task_comments (task, author, body, ts) VALUES (?,?,?,?)", (tid, author, body, ts))
        return True


def task_comments(tid: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT author, body, ts FROM task_comments WHERE task = ? ORDER BY rowid",
                                           (tid,))]


def tasks_replied(repo: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE repo = ? AND state = 'needs-info' AND (SELECT author FROM task_comments c "
            "WHERE c.task = tasks.id ORDER BY c.rowid DESC LIMIT 1) = 'owner' ORDER BY created, rowid", (repo,))]
