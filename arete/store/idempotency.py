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


def claim(context, seq):
    with tx() as c:
        c.execute("INSERT INTO idempotency (actor,key,route,request_sha,status,response,first_seq,at,via) "
                  "VALUES (?,?,?,?,0,?,?,?,?)", (*context, b"", seq, util.iso(), events.provenance()))


def complete(context, status, payload, seq):
    with tx() as c:
        c.execute("UPDATE idempotency SET status = ?, response = ?, last_seq = ? "
                  "WHERE actor = ? AND key = ? AND status = 0", (status, payload, seq, *context[:2]))


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


def prune():
    with tx() as c:
        c.execute("DELETE FROM idempotency WHERE at < ?", (util.iso(util.now() - timedelta(days=30)),))
