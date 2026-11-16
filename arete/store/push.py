from __future__ import annotations

from .. import util
from .db import db, reader

TABLES = {
    "push_subscriptions": "until removed",
}


def push_add(endpoint: str, p256dh: str, auth: str, label: str, contact: str) -> None:
    with db() as c:
        c.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth, label, contact, created) "
                  "VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
                  "auth = excluded.auth, label = excluded.label, contact = excluded.contact",
                  (endpoint, p256dh, auth, label, contact, util.iso()))


def push_list(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM push_subscriptions ORDER BY created, rowid")]


def push_drop(endpoints: list[str]) -> int:
    with db() as c:
        return sum(c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (e,)).rowcount for e in endpoints)


def push_succeeded(endpoints: list[str]) -> None:
    with db() as c:
        c.executemany("UPDATE push_subscriptions SET last_success = ? WHERE endpoint = ?",
                      [(util.iso(), e) for e in endpoints])
