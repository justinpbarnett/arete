from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from contextvars import ContextVar

from .. import paths, util
from . import db as foundation

TABLES = {"principals": "kept", "principal_logins": "kept"}

_actor = ContextVar("principal", default="hand")


def current():
    return _actor.get()


def admit(principal):
    _actor.set(principal)


@contextmanager
def acting(principal):
    token = _actor.set(principal)
    try:
        yield
    finally:
        _actor.reset(token)


def owner():
    with foundation.reader() as c:
        row = c.execute("SELECT id FROM principals WHERE kind = 'human' AND status = 'active' "
                        "AND role = 'owner' ORDER BY created, rowid LIMIT 1").fetchone()
        return row[0] if row else "hand"


def cli_actor():
    if actor := os.environ.get("ARETE_ACTOR"):
        return actor
    if not paths.DB_FILE.exists():
        return "hand"
    with foundation.reader() as c:
        if c.execute("PRAGMA user_version").fetchone()[0] < 5:
            return "hand"
    return owner()


def is_person(value):
    if value == "owner":
        return True
    with foundation.reader() as c:
        return c.execute("SELECT 1 FROM principals WHERE id = ? AND kind = 'human'", (value,)).fetchone() is not None


def active_human():
    with foundation.reader() as c:
        return c.execute("SELECT id FROM principals WHERE kind = 'human' AND status = 'active' LIMIT 1").fetchone()


def for_login(login):
    with foundation.reader() as c:
        row = c.execute("SELECT p.id FROM principals p JOIN principal_logins l ON l.principal = p.id "
                        "WHERE l.method = 'tailnet' AND l.subject = ? AND p.kind = 'human' "
                        "AND p.status = 'active' AND (l.expires IS NULL OR l.expires > ?)", (login, util.iso())).fetchone()
        return row[0] if row else None


def contact(principal):
    with foundation.reader() as c:
        row = c.execute("SELECT subject FROM principal_logins WHERE principal = ? AND method = 'tailnet' "
                        "ORDER BY created LIMIT 1", (principal,)).fetchone()
        return row[0] if row else ""


def changed(c, principal):
    from .. import events

    row = dict(c.execute("SELECT * FROM principals WHERE id = ?", (principal,)).fetchone())
    events.emit("principal.changed", f"principal:{principal}", cls="durable", ver=row["version"],
                data={k: row[k] for k in ("id", "kind", "role", "status")})


def seed_owner(login, name=""):
    with foundation.tx() as c:
        if row := c.execute("SELECT id FROM principals WHERE kind = 'human' AND status = 'active' LIMIT 1").fetchone():
            return row[0]
        if not login:
            raise ValueError("the first person needs a tailnet login")
        principal, at = f"u:{secrets.token_hex(4)}", util.iso()
        c.execute("INSERT INTO principals (id, kind, role, name, status, created) VALUES (?, 'human', 'owner', ?, 'active', ?)",
                  (principal, name or login.split('@')[0], at))
        c.execute("INSERT INTO principal_logins (id, principal, method, subject, created) VALUES (?, ?, 'tailnet', ?, ?)",
                  (secrets.token_hex(8), principal, login, at))
        with acting("hand"):
            changed(c, principal)
        return principal


def update(principal, **fields):
    if not fields or set(fields) - {"name", "kind", "role", "status", "external", "removed_at"}:
        raise ValueError("invalid principal fields")
    with foundation.tx() as c:
        if not c.execute(f"UPDATE principals SET {', '.join(k + ' = ?' for k in fields)}, version = version + 1 "
                         "WHERE id = ?", (*fields.values(), principal)).rowcount:
            raise KeyError(principal)
        changed(c, principal)


def listing(**filters):
    filters = {k: v for k, v in filters.items() if k in ("id", "kind", "role", "status") and v}
    with foundation.reader() as c:
        rows = c.execute("SELECT id, version, kind, name, role, status, created FROM principals" +
                         (" WHERE " + " AND ".join(k + ' = ?' for k in filters) if filters else "") +
                         " ORDER BY created, id", tuple(filters.values()))
        return [{**dict(r), "subject": f"principal:{r['id']}"} for r in rows]
