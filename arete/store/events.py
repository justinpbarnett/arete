from __future__ import annotations

from contextlib import nullcontext

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from .. import principals, util
from . import db as foundation

TABLES = {"events_log": "durable kept; derived 90 days; notice 7 days"}

VERSION = 8
CLASSES = ("durable", "derived", "notice")
VIA = ContextVar("event_via", default=None)
RECEIPTS = ContextVar("event_receipts", default=None)
CAUSE_SEQ = ContextVar("event_cause_seq", default=None)


@contextmanager
def caused_by(seq):
    token = CAUSE_SEQ.set(seq)
    try:
        yield
    finally:
        CAUSE_SEQ.reset(token)


def provenance():
    return VIA.get() if VIA.get() is not None else os.environ.get("ARETE_VIA")


@contextmanager
def collect_receipts():
    parent = RECEIPTS.get()
    collected = []
    token = RECEIPTS.set(collected)
    try:
        yield collected
    finally:
        RECEIPTS.reset(token)
        if parent is not None:
            parent.extend(collected)


@contextmanager
def request_scope(via):
    origin = VIA.set(via)
    receipts = RECEIPTS.set([])
    try:
        yield
    finally:
        VIA.reset(origin)
        RECEIPTS.reset(receipts)


def committed_receipt(subject=None, *, first=False):
    if foundation.in_write() or not RECEIPTS.get():
        return None
    with foundation.reader() as c:
        collected = RECEIPTS.get() or []
        pending = collected if first else reversed(collected)
        for seq, found, ver in pending:
            if subject is not None and found != subject:
                continue
            if c.execute("SELECT 1 FROM events_log WHERE seq = ? AND subject = ? AND ver = ?",
                         (seq, found, ver)).fetchone():
                epoch = c.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0]
                return {"event_seq": f"{epoch}.{seq}", "subject": found, "ver": ver}
    return None


def reuse_receipt(subject, ver):
    """Collect an existing version's receipt while its entity is write-locked."""
    c = foundation.write_connection()
    row = c.execute("SELECT seq FROM events_log WHERE subject = ? AND ver = ? ORDER BY seq DESC LIMIT 1",
                    (subject, ver)).fetchone()
    if not row:
        return False
    if RECEIPTS.get() is not None:
        RECEIPTS.get().append((row["seq"], subject, ver))
    return True


def emit(kind, subject, *, cls, data, ver=None, task=None, cause_seq=None, via=None, connection=None):
    c = foundation.write_connection() if connection is None else connection
    if not c.in_transaction:
        raise RuntimeError("event emission needs an open write transaction")
    actor = principals.current()
    if actor == "owner" or "@" in actor:
        raise ValueError("events need a principal id, not a login or owner")
    if cls not in CLASSES:
        raise ValueError("invalid event class")
    if cause_seq is None:
        cause_seq = CAUSE_SEQ.get()
    seq = c.execute("INSERT INTO events_log (at, kind, subject, ver, task, class, shape, actor, cause_seq, data, via) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (util.iso(), kind, subject, ver, task, cls, actor, cause_seq, json.dumps(data),
                     provenance() if via is None else via)).lastrowid
    if RECEIPTS.get() is not None and ver is not None:
        RECEIPTS.get().append((seq, subject, ver))
    return seq


def metadata(c):
    meta = dict(c.execute("SELECT key, value FROM schema_meta"))
    epoch = str(meta["restore_epoch"])
    head = c.execute("SELECT COALESCE(MAX(seq), 0) FROM events_log").fetchone()[0]
    return {"complete": meta.get("oldest_writer") is None or int(meta["oldest_writer"]) >= VERSION,
            "trim": {cls: f"{epoch}.{meta.get('trim_' + cls, '0')}" for cls in CLASSES},
            "head": f"{epoch}.{head}"}


def trim():
    c = foundation.write_connection()
    for cls, days in (("derived", 90), ("notice", 7)):
        old = int(c.execute("SELECT value FROM schema_meta WHERE key = ?", (f"trim_{cls}",)).fetchone()[0])
        mark = c.execute("SELECT COALESCE(MAX(seq), 0) FROM events_log WHERE class = ? AND at < ?",
                         (cls, util.iso(util.now() - timedelta(days=days)))).fetchone()[0]
        mark = max(old, mark)
        c.execute("UPDATE schema_meta SET value = ? WHERE key = ?", (str(mark), f"trim_{cls}"))
        c.execute("DELETE FROM events_log WHERE class = ? AND seq <= ?", (cls, mark))


def cursor(value):
    if value is None:
        return None, None
    if not re.fullmatch(r"(?:[0-9]+\.)?[0-9]+", value):
        raise ValueError("invalid event cursor")
    parts = value.rsplit(".", 1)
    return (parts[0], int(parts[1])) if len(parts) == 2 else (None, int(value))


def filters(query):
    clauses, args = [], []
    classes = query.get("class", "").split(",") if query.get("class") else list(CLASSES)
    if any(c not in CLASSES for c in classes):
        raise ValueError("invalid event class")
    for key, column in (("subject", "subject"), ("task", "task"), ("class", "class"), ("idem", "idem")):
        if query.get(key):
            values = query[key].split(",")
            clauses.append(f"e.{column} IN ({','.join('?' for _ in values)})")
            args.extend(values)
    if query.get("kinds"):
        kinds = []
        for kind in query["kinds"].split(","):
            family = kind.endswith(".*")
            kinds.append("e.kind LIKE ? ESCAPE '\\'" if family else "e.kind = ?")
            args.append(kind[:-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                        if family else kind)
        clauses.append("(" + " OR ".join(kinds) + ")")
    if query.get("run"):
        clauses.append("(e.subject = ? OR json_extract(e.data, '$.run') = ?)")
        args.extend((f"run:{query['run']}", query["run"]))
    turns = query.get("turns", "").split(",") if query.get("turns") else []
    clauses.append("(e.kind != 'turn.appended'" +
                   (f" OR json_extract(e.data, '$.run') IN ({','.join('?' for _ in turns)})" if turns else "") + ")")
    args.extend(turns)
    return " AND ".join(clauses) or "1", args, classes


def scan(c, epoch, seq, selected):
    where, args, classes = selected
    with (nullcontext() if c.in_transaction else foundation.read_snapshot(c)):
        meta = metadata(c)
        now_epoch, top = cursor(meta["head"])
        seq = top if seq is None else seq
        reason = "epoch" if epoch is not None and epoch != now_epoch else None
        if not reason and any(seq < cursor(meta["trim"][cls])[1] for cls in classes):
            reason = "too_old"
        if not reason:
            count = c.execute("SELECT COUNT(*) FROM (SELECT 1 FROM events_log e WHERE e.seq > ? AND e.seq <= ? AND " +
                              where + " LIMIT 10001)", (seq, top, *args)).fetchone()[0]
            if count > 10000:
                reason = "too_far"
        rows, after = [], seq
        while not reason:
            page = c.execute("SELECT e.* FROM events_log e WHERE e.seq > ? AND e.seq <= ? AND " + where +
                             " ORDER BY e.seq LIMIT 500", (after, top, *args)).fetchall()
            rows.extend(dict(r) for r in page)
            if len(page) < 500:
                break
            after = page[-1]["seq"]
        return now_epoch, top, [] if reason else rows, reason, meta


def envelope(c, row, epoch):
    person = c.execute("SELECT name FROM principals WHERE id = ?", (row["actor"],)).fetchone()
    return {**row, "data": json.loads(row["data"]), "id": f"{epoch}.{row['seq']}", "epoch": epoch,
            "actor_name": person[0] if person else None}
