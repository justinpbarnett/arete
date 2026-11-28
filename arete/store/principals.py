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
    actor = _actor.get()
    if callable(actor):
        actor = actor()
        _actor.set(actor)
    return actor


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


def create_owner(c, name):
    principal = f"u:{secrets.token_hex(4)}"
    c.execute("INSERT INTO principals (id, kind, role, name, status, created) VALUES (?, 'human', 'owner', ?, 'active', ?)",
              (principal, name, util.iso()))
    return principal


def ensure_local_owner(c):
    principal = owner()
    if principal != "hand":
        return principal
    if c.execute("SELECT 1 FROM principals WHERE kind = 'human' LIMIT 1").fetchone():
        return None
    principal = create_owner(c, "Local owner")
    with acting("hand"):
        changed(c, principal)
    return principal


def has_login(c, principal):
    return c.execute("SELECT 1 FROM principal_logins WHERE principal = ?", (principal,)).fetchone() is not None


def needs_owner_login():
    with foundation.reader() as c:
        principal = owner()
        return not active_human() if principal == "hand" else not has_login(c, principal)


def seed_owner(login, name=""):
    with foundation.tx() as c:
        principal = owner()
        if principal == "hand" and (person := active_human()):
            return person[0]
        if principal != "hand" and has_login(c, principal):
            return principal
        if not login:
            raise ValueError("the first person needs a tailnet login")
        name = name or login.split("@")[0]
        if principal == "hand":
            principal = create_owner(c, name)
        else:
            c.execute("UPDATE principals SET name = ?, version = version + 1 WHERE id = ?", (name, principal))
        c.execute("INSERT INTO principal_logins (id, principal, method, subject, created) VALUES (?, ?, 'tailnet', ?, ?)",
                  (secrets.token_hex(8), principal, login, util.iso()))
        with acting("hand"):
            changed(c, principal)
        return principal


def update(principal, **fields):
    if not fields or set(fields) - {"name", "kind", "role", "status", "external", "removed_at"}:
        raise ValueError("invalid principal fields")
    with foundation.tx() as c:
        if not c.execute(f"UPDATE principals SET {', '.join(k + ' = ?' for k in fields)}, version = version + 1 "  # noqa: S608
                         "WHERE id = ?", (*fields.values(), principal)).rowcount:
            raise KeyError(principal)
        if fields.keys() & {"kind", "role", "status"}:
            from .tokens import bump_generation

            bump_generation(c)
        changed(c, principal)


def listing(**filters):
    filters = {k: v for k, v in filters.items() if k in ("id", "kind", "role", "status") and v}
    with foundation.reader() as c:
        rows = c.execute("SELECT id, version, kind, name, role, status, created FROM principals" +  # noqa: S608
                         (" WHERE " + " AND ".join(k + ' = ?' for k in filters) if filters else "") +
                         " ORDER BY created, id", tuple(filters.values()))
        return [{**dict(r), "subject": f"principal:{r['id']}"} for r in rows]
