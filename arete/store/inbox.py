from __future__ import annotations

import json
import re

from .. import util
from . import events
from .db import after_commit, reader, tx, tx_state

TABLES = {
    "inbox": "kept",
    "pending_commands": "until resolved or expired",
}

KINDS = frozenset(("approval", "choice", "continue", "freeform", "proposal", "candidate", "alert", "workspace"))
STATES = frozenset(("open", "snoozed", "shelved", "paused", "resolved"))
PRIORITIES = (1, 2, 3)
ID_RE = re.compile(r"^(?:[Tt][Rr]-)?([1-9][0-9]{0,17})$")


def format_id(numeric_id: int) -> str:
    return f"TR-{numeric_id}"


def parse_id(value: str | int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 < value < 2**63 else None
    if not isinstance(value, str):
        return None
    m = ID_RE.fullmatch(value.strip())
    return int(m[1]) if m else None


def available(c) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inbox'").fetchone() is not None


def decode(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    return {
        **d,
        "id": format_id(d["id"]),
        "detail": json.loads(d["detail"] or "{}"),
        "verbs": json.loads(d["verbs"] or "[]"),
        "options": json.loads(d["options"] or "[]"),
    }


def _event_data(row, extra: dict | None = None) -> dict:
    item = decode(row)
    data = {
        "id": item["id"],
        "kind": item["kind"],
        "subject": item["subject"],
        "priority": item["priority"],
        "state": item["state"],
        "title": item["title"],
        "version": item["version"],
    }
    if item["resolution"]:
        data["resolution"] = item["resolution"]
    if item["answered_by"]:
        data["answered_by"] = item["answered_by"]
    if extra:
        data.update(extra)
    return data


def _emit(c, event_kind: str, row, extra: dict | None = None) -> int:
    return events.emit(
        event_kind,
        f"inbox:{row['id']}",
        cls="durable",
        ver=row["version"],
        task=row["task_id"] or None,
        data=_event_data(row, extra),
        connection=c,
    )


def _notify_high(bucket: list[int]) -> None:
    if not bucket:
        return
    from .. import webpush

    webpush.notify_raised(min(bucket))


def _queue_high_push(numeric_id: int) -> None:
    state = tx_state()
    bucket = state.get("inbox_high_raised")
    if bucket is None:
        bucket = []
        state["inbox_high_raised"] = bucket
        after_commit(lambda b=bucket: _notify_high(b))
    bucket.append(numeric_id)


def _encode_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def raise_item(
    c,
    *,
    kind: str,
    subject: str,
    priority: int = 2,
    title: str = "",
    detail: dict | None = None,
    verbs: list[str] | None = None,
    options: list | None = None,
    task_id: str = "",
    run_id: str = "",
    line: str = "",
) -> tuple[dict, int | None, bool]:
    now = util.iso()
    cur = c.execute(
        "INSERT OR IGNORE INTO inbox "
        "(kind, subject, task_id, run_id, line, priority, title, detail, verbs, options, state, raised, version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 1)",
        (
            kind,
            subject,
            str(task_id or ""),
            str(run_id or ""),
            str(line or ""),
            int(priority),
            str(title or ""),
            _encode_json(detail if detail is not None else {}),
            _encode_json(list(verbs or ())),
            _encode_json(list(options or ())),
            now,
        ),
    )
    if cur.rowcount == 1:
        row = c.execute("SELECT * FROM inbox WHERE id = ?", (cur.lastrowid,)).fetchone()
        seq = _emit(c, "inbox.raised", row)
        if row["priority"] == 1:
            _queue_high_push(row["id"])
        return decode(row), seq, True
    existing = c.execute(
        "SELECT * FROM inbox WHERE kind = ? AND subject = ? AND state != 'resolved' ORDER BY id DESC LIMIT 1",
        (kind, subject),
    ).fetchone()
    return decode(existing), None, False


def update_item(
    c,
    numeric_id: int,
    *,
    title: str,
    detail: dict,
    priority: int,
    verbs: list[str],
    options: list,
    task_id: str,
    run_id: str,
    line: str,
) -> tuple[dict, int] | None:
    row = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if row is None or row["state"] == "resolved":
        return None
    enc_detail = _encode_json(detail)
    enc_verbs = _encode_json(list(verbs))
    enc_options = _encode_json(list(options))
    if (
        row["title"] == title
        and row["detail"] == enc_detail
        and row["priority"] == int(priority)
        and row["verbs"] == enc_verbs
        and row["options"] == enc_options
        and row["task_id"] == str(task_id or "")
        and row["run_id"] == str(run_id or "")
        and row["line"] == str(line or "")
    ):
        return None
    new_ver = row["version"] + 1
    reopen = (
        row["state"] == "snoozed"
        and row["snooze_watch"] == f"inbox:{numeric_id}"
        and new_ver > row["snooze_watch_ver"]
    )
    if reopen:
        c.execute(
            "UPDATE inbox SET title = ?, detail = ?, priority = ?, verbs = ?, options = ?, "
            "task_id = ?, run_id = ?, line = ?, state = 'open', snooze_until = '', "
            "snooze_watch = '', snooze_watch_ver = 0, version = ? WHERE id = ?",
            (title, enc_detail, int(priority), enc_verbs, enc_options, str(task_id or ""), str(run_id or ""),
             str(line or ""), new_ver, numeric_id),
        )
    else:
        c.execute(
            "UPDATE inbox SET title = ?, detail = ?, priority = ?, verbs = ?, options = ?, "
            "task_id = ?, run_id = ?, line = ?, version = ? WHERE id = ?",
            (title, enc_detail, int(priority), enc_verbs, enc_options, str(task_id or ""), str(run_id or ""),
             str(line or ""), new_ver, numeric_id),
        )
    after = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    seq = _emit(c, "inbox.changed", after, {"reopened": True} if reopen else None)
    return decode(after), seq


def resolve_item(
    c,
    numeric_id: int,
    *,
    resolution: str,
    answer: str = "",
    answered_by: str = "",
    answered_at: str = "",
    base_version: int | None = None,
) -> tuple[dict | None, int | None, bool]:
    """Resolve an inbox item. Returns (item_or_current, seq, ok)."""
    row = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if row is None:
        return None, None, False
    if row["state"] == "resolved":
        return decode(row), None, False
    if base_version is not None and row["version"] != base_version:
        return decode(row), None, False
    now = util.iso()
    new_ver = row["version"] + 1
    where = "id = ? AND state != 'resolved'"
    args = [
        resolution,
        str(answer or ""),
        str(answered_by or ""),
        str(answered_at or ""),
        now,
        new_ver,
        numeric_id,
    ]
    if base_version is not None:
        where += " AND version = ?"
        args.append(base_version)
    changed = c.execute(
        f"UPDATE inbox SET state = 'resolved', resolution = ?, answer = ?, answered_by = ?, answered_at = ?, "  # noqa: S608
        f"snooze_until = '', snooze_watch = '', snooze_watch_ver = 0, shelve_until = '', "
        f"resolved = ?, version = ? WHERE {where}",
        args,
    ).rowcount
    after = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if not changed:
        return decode(after), None, False
    seq = _emit(c, "inbox.resolved", after)
    return decode(after), seq, True


def record_answer(
    c,
    numeric_id: int,
    *,
    answer: str,
    answered_by: str,
    answered_at: str,
    base_version: int | None = None,
) -> tuple[dict | None, int | None, bool]:
    """Set answer metadata on an item while leaving it open (e.g., approve_rule)."""
    row = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if row is None or row["state"] == "resolved":
        return decode(row), None, False
    if base_version is not None and row["version"] != base_version:
        return decode(row), None, False
    new_ver = row["version"] + 1
    where = "id = ? AND state != 'resolved'"
    args = [str(answer), str(answered_by), str(answered_at), new_ver, numeric_id]
    if base_version is not None:
        where += " AND version = ?"
        args.append(base_version)
    changed = c.execute(
        f"UPDATE inbox SET answer = ?, answered_by = ?, answered_at = ?, version = ? WHERE {where}",  # noqa: S608
        args,
    ).rowcount
    after = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if not changed:
        return decode(after), None, False
    seq = _emit(c, "inbox.changed", after)
    return decode(after), seq, True


def snooze_item(
    c,
    numeric_id: int,
    *,
    base_version: int,
    until: str = "",
    until_changed: bool = False,
) -> tuple[dict | None, int | None, bool]:
    row = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if row is None or row["state"] == "resolved" or row["version"] != base_version:
        return decode(row), None, False
    new_ver = row["version"] + 1
    watch = f"inbox:{numeric_id}" if until_changed else ""
    watch_ver = new_ver if until_changed else 0
    changed = c.execute(
        "UPDATE inbox SET state = 'snoozed', snooze_until = ?, snooze_watch = ?, snooze_watch_ver = ?, "
        "shelve_until = '', version = ? WHERE id = ? AND state != 'resolved' AND version = ?",
        (until, watch, watch_ver, new_ver, numeric_id, base_version),
    ).rowcount
    after = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if not changed:
        return decode(after), None, False
    seq = _emit(c, "inbox.changed", after, {"snooze_until": until, "until_changed": until_changed})
    return decode(after), seq, True


def shelve_item(
    c,
    numeric_id: int,
    *,
    base_version: int,
    reason: str,
    until: str = "",
) -> tuple[dict | None, int | None, bool]:
    row = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if row is None or row["state"] == "resolved" or row["version"] != base_version:
        return decode(row), None, False
    new_ver = row["version"] + 1
    changed = c.execute(
        "UPDATE inbox SET state = 'shelved', shelve_reason = ?, shelve_until = ?, shelves = shelves + 1, "
        "snooze_until = '', snooze_watch = '', snooze_watch_ver = 0, version = ? "
        "WHERE id = ? AND state != 'resolved' AND version = ?",
        (reason, until, new_ver, numeric_id, base_version),
    ).rowcount
    after = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
    if not changed:
        return decode(after), None, False
    seq = _emit(c, "inbox.changed", after, {"shelve_reason": reason, "shelve_until": until})
    return decode(after), seq, True


def reopen_due(c, now: str | None = None) -> list[dict]:
    if not available(c):
        return []
    now = now or util.iso()
    rows = c.execute(
        "SELECT * FROM inbox WHERE (state = 'snoozed' AND snooze_until != '' AND snooze_until <= ?) "
        "OR (state = 'shelved' AND shelve_until != '' AND shelve_until <= ?) ORDER BY id",
        (now, now),
    ).fetchall()
    reopened = []
    for row in rows:
        new_ver = row["version"] + 1
        c.execute(
            "UPDATE inbox SET state = 'open', snooze_until = '', snooze_watch = '', snooze_watch_ver = 0, "
            "shelve_until = '', version = ? WHERE id = ?",
            (new_ver, row["id"]),
        )
        after = c.execute("SELECT * FROM inbox WHERE id = ?", (row["id"],)).fetchone()
        _emit(c, "inbox.changed", after, {"reopened": True})
        reopened.append(decode(after))
    return reopened


def mark_reminded(numeric_id: int) -> dict | None:
    now = util.iso()
    with tx() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM inbox WHERE id = ? AND state = 'open' AND reminded = ''", (numeric_id,)).fetchone()
        if row is None:
            return None
        new_ver = row["version"] + 1
        changed = c.execute(
            "UPDATE inbox SET reminded = ?, version = ? WHERE id = ? AND state = 'open' AND reminded = ''",
            (now, new_ver, numeric_id),
        ).rowcount
        if not changed:
            return None
        after = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric_id,)).fetchone()
        _emit(c, "inbox.changed", after, {"reminded": now})
        return decode(after)


def high_due_reminders(cutoff: str, connect=reader) -> list[dict]:
    with connect() as c:
        if not available(c):
            return []
        rows = c.execute(
            "SELECT * FROM inbox WHERE state = 'open' AND priority = 1 AND reminded = '' AND raised <= ? "
            "ORDER BY id",
            (cutoff,),
        ).fetchall()
        return [decode(r) for r in rows]


def get(item_id: str | int, connect=reader) -> dict | None:
    numeric = parse_id(item_id)
    if numeric is None:
        return None
    with connect() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM inbox WHERE id = ?", (numeric,)).fetchone()
        return decode(row)


def get_open_by_subject(subject: str, connect=reader) -> dict | None:
    with connect() as c:
        if not available(c):
            return None
        row = c.execute(
            "SELECT * FROM inbox WHERE subject = ? AND state != 'resolved' ORDER BY id DESC LIMIT 1",
            (subject,),
        ).fetchone()
        return decode(row)


def newest_resolved_by_subject(c, subject: str) -> dict | None:
    row = c.execute(
        "SELECT * FROM inbox WHERE subject = ? AND state = 'resolved' ORDER BY id DESC LIMIT 1",
        (subject,),
    ).fetchone()
    return decode(row)


def unresolved_for_pattern(c, pattern: str) -> list[dict]:
    rows = c.execute(
        "SELECT * FROM inbox WHERE state != 'resolved' AND subject LIKE ? ORDER BY id",
        (pattern,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_items(
    *,
    state: str | tuple[str, ...] | None = "open",
    kind: str | None = None,
    priority: int | None = None,
    task: str | None = None,
    run: str | None = None,
    line: str | None = None,
    subject_prefix: str | None = None,
    connect=reader,
) -> list[dict]:
    with connect() as c:
        if not available(c):
            return []
        clauses, args = [], []
        if state is not None and state != "all":
            states = (state,) if isinstance(state, str) else tuple(state)
            if len(states) == 1 and "," in states[0]:
                states = tuple(s.strip() for s in states[0].split(",") if s.strip())
            if states:
                clauses.append(f"state IN ({','.join('?' for _ in states)})")
                args.extend(states)
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        if priority is not None:
            clauses.append("priority = ?")
            args.append(int(priority))
        if task:
            clauses.append("task_id = ?")
            args.append(str(task))
        if run:
            clauses.append("run_id = ?")
            args.append(str(run))
        if line:
            clauses.append("line = ?")
            args.append(str(line))
        if subject_prefix:
            clauses.append("subject LIKE ?")
            args.append(subject_prefix + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = c.execute(f"SELECT * FROM inbox{where} ORDER BY priority ASC, id ASC", args).fetchall()  # noqa: S608
        return [decode(r) for r in rows]


def count_open(connect=reader) -> int:
    with connect() as c:
        if not available(c):
            return 0
        return int(c.execute("SELECT COUNT(*) FROM inbox WHERE state = 'open'").fetchone()[0])


def counts_by_state(connect=reader) -> dict[str, int]:
    counts = dict.fromkeys(("open", "snoozed", "shelved", "paused", "resolved"), 0)
    with connect() as c:
        if not available(c):
            return counts
        for state, n in c.execute("SELECT state, COUNT(*) FROM inbox GROUP BY state"):
            counts[state] = int(n)
    return counts


def history(item_id: str | int, connect=reader) -> list[dict] | None:
    numeric = parse_id(item_id)
    if numeric is None:
        return None
    with connect() as c:
        if not available(c):
            return None
        if not c.execute("SELECT 1 FROM inbox WHERE id = ?", (numeric,)).fetchone():
            return None
        epoch = str(c.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0])
        rows = c.execute(
            "SELECT * FROM events_log WHERE subject = ? AND class = 'durable' ORDER BY seq ASC",
            (f"inbox:{numeric}",),
        ).fetchall()
        return [events.envelope(c, dict(r), epoch) for r in rows]


def add_pending(c, actor: str, key: str, request: dict, expires: str) -> None:
    c.execute(
        "INSERT OR REPLACE INTO pending_commands (actor, key, request, expires) VALUES (?, ?, ?, ?)",
        (actor, key, json.dumps(request, sort_keys=True, separators=(",", ":"), default=str), expires),
    )


def get_pending(c, actor: str, key: str) -> dict | None:
    if not available(c):
        return None
    row = c.execute("SELECT * FROM pending_commands WHERE actor = ? AND key = ?", (actor, key)).fetchone()
    if row is None:
        return None
    return {**dict(row), "request": json.loads(row["request"])}


def delete_pending(c, actor: str, key: str) -> bool:
    return c.execute("DELETE FROM pending_commands WHERE actor = ? AND key = ?", (actor, key)).rowcount > 0


def list_pending(c) -> list[dict]:
    if not available(c):
        return []
    return [{**dict(r), "request": json.loads(r["request"])}
            for r in c.execute("SELECT * FROM pending_commands ORDER BY expires, actor, key").fetchall()]
