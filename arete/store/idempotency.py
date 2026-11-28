from __future__ import annotations

import json
from datetime import timedelta

from .. import util
from . import events
from .db import reader, tx

TABLES = {"idempotency": "30 days"}


def lookup(actor, key):
    with reader() as c:
        row = c.execute("SELECT * FROM idempotency WHERE actor = ? AND key = ?", (actor, key)).fetchone()
        return dict(row) if row else None


def claim(context, seq, pending=b""):
    with tx() as c:
        c.execute("INSERT INTO idempotency (actor,key,route,request_sha,status,response,first_seq,at,via) "
                  "VALUES (?,?,?,?,0,?,?,?,?)", (*context, pending, seq, util.iso(), events.provenance()))


def update_pending(context, pending):
    with tx() as c:
        changed = c.execute("UPDATE idempotency SET response = ? WHERE actor = ? AND key = ? "
                            "AND route = ? AND request_sha = ? AND status = 0", (pending, *context)).rowcount
        if not changed:
            raise RuntimeError("publication claim is no longer pending")


def abandon(context):
    with tx() as c:
        c.execute("DELETE FROM idempotency WHERE actor = ? AND key = ? AND route = ? AND request_sha = ? AND status = 0",
                  context)


def complete(context, status, payload, seq):
    with tx() as c:
        c.execute("INSERT INTO idempotency (actor,key,route,request_sha,status,response,last_seq,at,via) "
                  "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (actor,key) DO UPDATE SET "
                  "status = excluded.status, response = excluded.response, last_seq = excluded.last_seq "
                  "WHERE idempotency.status = 0 AND idempotency.route = excluded.route "
                  "AND idempotency.request_sha = excluded.request_sha",
                  (*context, status, payload, seq, util.iso(), events.provenance()))
        row = c.execute("SELECT * FROM idempotency WHERE actor = ? AND key = ?", context[:2]).fetchone()
        return dict(row)


def overwrite(actor, key, status, payload, seq, *, expected_status=202, connection=None):
    def _apply(c):
        if expected_status is not None:
            return c.execute(
                "UPDATE idempotency SET status = ?, response = ?, last_seq = ? WHERE actor = ? AND key = ? AND status = ?",
                (status, payload, seq, actor, key, expected_status),
            ).rowcount
        return c.execute(
            "UPDATE idempotency SET status = ?, response = ?, last_seq = ? WHERE actor = ? AND key = ?",
            (status, payload, seq, actor, key),
        ).rowcount

    if connection is not None:
        return _apply(connection)
    with tx() as c:
        return _apply(c)


def target(row):
    with reader() as c:
        event = c.execute("SELECT * FROM events_log WHERE seq = ?", (row["first_seq"],)).fetchone()
        if not event:
            raise RuntimeError("idempotency target event is missing")
        data = json.loads(event["data"])
        return data["repo"], str(data.get("ticket") or data["id"]), event["kind"] == "task.created"


def receipt(seq):
    with reader() as c:
        event = c.execute("SELECT subject, ver FROM events_log WHERE seq = ?", (seq,)).fetchone()
        if not event:
            return None
        epoch = c.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0]
        return {"event_seq": f"{epoch}.{seq}", **dict(event)}


def prune(c=None, *, days: float = 30) -> int:
    cutoff = util.iso(util.now() - timedelta(days=days))
    if c is not None:
        return c.execute("DELETE FROM idempotency WHERE at < ?", (cutoff,)).rowcount
    with tx() as conn:
        return conn.execute("DELETE FROM idempotency WHERE at < ?", (cutoff,)).rowcount
