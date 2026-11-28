from __future__ import annotations

from .. import util
from .db import INBOX_SCHEMA, db, migration_applied, reader

TABLES = {
    "push_subscriptions": "until removed",
}


def push_add(endpoint: str, p256dh: str, auth: str, label: str, contact: str) -> int:
    with db() as c:
        c.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth, label, contact, created) "
                  "VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
                  "auth = excluded.auth, label = excluded.label, contact = excluded.contact",
                  (endpoint, p256dh, auth, label, contact, util.iso()))
        row = c.execute("SELECT rowid FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        return int(row[0])


def push_list(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT rowid, * FROM push_subscriptions ORDER BY created, rowid")]


def push_get(rowid: int, connect=reader) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT rowid, * FROM push_subscriptions WHERE rowid = ?", (rowid,)).fetchone()
        return dict(row) if row else None


def push_drop(endpoints: list[str]) -> int:
    if not endpoints:
        return 0
    with db() as c:
        return sum(c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (e,)).rowcount for e in endpoints)


def push_drop_by_rowid(rowid: int) -> bool:
    with db() as c:
        return c.execute("DELETE FROM push_subscriptions WHERE rowid = ?", (rowid,)).rowcount > 0


def push_succeeded(endpoints: list[str]) -> None:
    if not endpoints:
        return
    with db() as c:
        if migration_applied(INBOX_SCHEMA, c):
            c.executemany("UPDATE push_subscriptions SET last_success = ?, failures = 0 WHERE endpoint = ?",
                          [(util.iso(), e) for e in endpoints])
        else:
            c.executemany("UPDATE push_subscriptions SET last_success = ? WHERE endpoint = ?",
                          [(util.iso(), e) for e in endpoints])


def push_failed(endpoints: list[str]) -> list[dict]:
    if not endpoints:
        return []
    with db() as c:
        if not migration_applied(INBOX_SCHEMA, c):
            return []
        updated = []
        for endpoint in endpoints:
            c.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE endpoint = ?", (endpoint,))
            row = c.execute("SELECT rowid, * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
            if row:
                updated.append(dict(row))
        return updated
