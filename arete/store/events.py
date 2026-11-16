from __future__ import annotations

import json
import re
from datetime import timedelta

from .. import principals, util
from . import db as foundation

TABLES = {"events_log": "durable kept; derived 90 days; notice 7 days"}

VERSION = 5
CLASSES = ("durable", "derived", "notice")


def emit(kind, subject, *, cls, data, ver=None, task=None, cause_seq=None):
    c = foundation.write_connection()
    actor = principals.current()
    if actor == "owner" or "@" in actor:
        raise ValueError("events need a principal id, not a login or owner")
    if cls not in CLASSES:
        raise ValueError("invalid event class")
    return c.execute("INSERT INTO events_log (at, kind, subject, ver, task, class, shape, actor, cause_seq, data) "
                     "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                     (util.iso(), kind, subject, ver, task, cls, actor, cause_seq, json.dumps(data))).lastrowid


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
    with foundation.read_snapshot(c):
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
