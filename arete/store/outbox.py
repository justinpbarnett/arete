from __future__ import annotations

import json
import re
from contextlib import nullcontext

from .. import util
from . import db as foundation
from . import events
from ._rows import validate_columns
from .db import reader

TABLES = {
    "outbox": "kept",
    "outbox_revisions": "kept",
    "outbox_redactions": "kept",
    "delivery_attempts": "kept",
}

AUDIENCES = frozenset(("self", "team", "client", "public"))
STATES = frozenset(("draft", "edited", "approved", "held", "sending", "sent", "failed", "rejected", "discarded"))
ID_RE = re.compile(r"^(?:[Oo][Bb]-)?([1-9][0-9]{0,17})$")

_OUTBOX_UPDATABLE = frozenset((
    "marker",
    "rev",
    "state",
    "opened_at",
    "approved_by",
    "approved_rule",
    "approved_rule_version",
    "send_at",
    "base_remote_updated",
    "sent_at",
    "sent_ref",
    "sent_url",
    "error",
    "attempts",
    "reject_reason",
    "summary",
    "plain_summary",
    "review",
    "updated",
))

_ATTEMPT_UPDATABLE = frozenset((
    "ended",
    "outcome",
    "http_status",
    "retry_after",
    "external_id",
    "external_url",
    "lookup",
    "error",
))


def format_id(numeric_id: int) -> str:
    return f"OB-{numeric_id}"


def parse_id(value: str | int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 < value < 2**63 else None
    if not isinstance(value, str):
        return None
    m = ID_RE.fullmatch(value.strip())
    return int(m[1]) if m else None


def available(c) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbox'").fetchone() is not None


def decode(row, *, sending_since: str | None = None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    return {
        **d,
        "id": format_id(d["id"]),
        "sending_since": sending_since,
    }


def _event_data(row: dict, extra: dict | None = None) -> dict:
    data = {
        "id": format_id(row["id"]),
        "target": row["target"],
        "kind": row["kind"],
        "object": row["object"],
        "audience": row["audience"],
        "state": row["state"],
        "rev": row["rev"],
        "version": row["version"],
    }
    if row.get("sent_ref"):
        data["sent_ref"] = row["sent_ref"]
    if row.get("sent_url"):
        data["sent_url"] = row["sent_url"]
    if row.get("error"):
        data["error"] = row["error"]
    if extra:
        data.update(extra)
    return data


def emit_event(c, event_kind: str, row: dict, extra: dict | None = None) -> int:
    return events.emit(
        event_kind,
        f"outbox:{row['id']}",
        cls="durable",
        ver=row["version"],
        task=row["task_id"] or None,
        data=_event_data(row, extra),
        connection=c,
    )


def get_row(c, numeric_id: int) -> dict | None:
    if not available(c):
        return None
    row = c.execute("SELECT * FROM outbox WHERE id = ?", (numeric_id,)).fetchone()
    return dict(row) if row is not None else None


def get_raw(item_id: str | int, connect=reader) -> dict | None:
    numeric = parse_id(item_id)
    if numeric is None:
        return None
    with connect() as c:
        return get_row(c, numeric)


def get_by_slot(c, slot: str) -> dict | None:
    row = c.execute("SELECT * FROM outbox WHERE slot = ?", (slot,)).fetchone()
    return dict(row) if row is not None else None


def get_by_legacy_approval(c, approval_id: int) -> dict | None:
    row = c.execute("SELECT * FROM outbox WHERE legacy_approval = ?", (approval_id,)).fetchone()
    return dict(row) if row is not None else None


def open_attempt(c, numeric_id: int) -> dict | None:
    row = c.execute(
        "SELECT * FROM delivery_attempts WHERE item = ? AND ended IS NULL ORDER BY n DESC LIMIT 1",
        (numeric_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def first_attempt_started(c, numeric_id: int) -> str | None:
    row = c.execute(
        "SELECT started FROM delivery_attempts WHERE item = ? ORDER BY n ASC LIMIT 1",
        (numeric_id,),
    ).fetchone()
    return str(row["started"]) if row is not None else None


def last_attempt(c, numeric_id: int, *, before_n: int | None = None) -> dict | None:
    if before_n is None:
        row = c.execute(
            "SELECT * FROM delivery_attempts WHERE item = ? ORDER BY n DESC LIMIT 1",
            (numeric_id,),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT * FROM delivery_attempts WHERE item = ? AND n < ? ORDER BY n DESC LIMIT 1",
            (numeric_id, before_n),
        ).fetchone()
    return dict(row) if row is not None else None


def latest_revision(c, numeric_id: int, rev: int | None = None) -> dict | None:
    if rev is None:
        row = c.execute(
            "SELECT * FROM outbox_revisions WHERE item = ? ORDER BY rev DESC LIMIT 1",
            (numeric_id,),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT * FROM outbox_revisions WHERE item = ? AND rev = ?",
            (numeric_id, rev),
        ).fetchone()
    return dict(row) if row is not None else None


def revisions(c, numeric_id: int) -> list[dict]:
    return [
        dict(r)
        for r in c.execute(
            "SELECT * FROM outbox_revisions WHERE item = ? ORDER BY rev ASC",
            (numeric_id,),
        ).fetchall()
    ]


def redactions(c, numeric_id: int) -> list[dict]:
    return [
        dict(r)
        for r in c.execute(
            "SELECT * FROM outbox_redactions WHERE item = ? ORDER BY rev ASC, n ASC",
            (numeric_id,),
        ).fetchall()
    ]


def attempts(c, numeric_id: int) -> list[dict]:
    return [
        dict(r)
        for r in c.execute(
            "SELECT * FROM delivery_attempts WHERE item = ? ORDER BY n ASC",
            (numeric_id,),
        ).fetchall()
    ]


def sending_since_of(c, row: dict) -> str | None:
    if row["state"] != "sending":
        return None
    attempt = open_attempt(c, row["id"])
    return str(attempt["started"]) if attempt is not None else None


def decode_on(c, row: dict | None) -> dict | None:
    if row is None:
        return None
    return decode(row, sending_since=sending_since_of(c, row))


def get(item_id: str | int, connect=reader) -> dict | None:
    numeric = parse_id(item_id)
    if numeric is None:
        return None
    with connect() as c:
        row = get_row(c, numeric)
        return decode_on(c, row)


def get_detail(item_id: str | int, connect=reader) -> dict | None:
    numeric = parse_id(item_id)
    if numeric is None:
        return None
    with connect() as c:
        row = get_row(c, numeric)
        if row is None:
            return None
        since = sending_since_of(c, row)
        return {
            "item": decode(row, sending_since=since),
            "revisions": revisions(c, numeric),
            "redactions": redactions(c, numeric),
            "attempts": attempts(c, numeric),
            "sending_since": since,
        }


def list_items(
    *,
    state: str | tuple[str, ...] | None = None,
    task: str | None = None,
    run: str | None = None,
    connect=reader,
) -> list[dict]:
    with connect() as c:
        if not available(c):
            return []
        clauses: list[str] = []
        args: list[str] = []
        if state is not None and state != "all":
            states = (state,) if isinstance(state, str) else tuple(state)
            if len(states) == 1 and "," in states[0]:
                states = tuple(s.strip() for s in states[0].split(",") if s.strip())
            if states:
                clauses.append(f"o.state IN ({','.join('?' for _ in states)})")
                args.extend(states)
        if task:
            clauses.append("o.task_id = ?")
            args.append(str(task))
        if run:
            clauses.append("o.run_id = ?")
            args.append(str(run))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = c.execute(
            f"SELECT o.*, da.started AS _open_started "  # noqa: S608
            f"FROM outbox o "
            f"LEFT JOIN delivery_attempts da ON da.item = o.id AND da.ended IS NULL"
            f"{where} ORDER BY o.id ASC",
            args,
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            raw = dict(r)
            open_started = raw.pop("_open_started", None)
            since = str(open_started) if raw["state"] == "sending" and open_started else None
            out.append(decode(raw, sending_since=since))
        return out


def history(item_id: str | int, connect=reader) -> list[dict] | None:
    numeric = parse_id(item_id)
    if numeric is None:
        return None
    with connect() as c:
        if not available(c):
            return None
        if not c.execute("SELECT 1 FROM outbox WHERE id = ?", (numeric,)).fetchone():
            return None
        epoch = str(c.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0])
        rows = c.execute(
            "SELECT * FROM events_log WHERE subject = ? AND class = 'durable' ORDER BY seq ASC",
            (f"outbox:{numeric}",),
        ).fetchall()
        return [events.envelope(c, dict(r), epoch) for r in rows]


def _insert_redactions(c, numeric_id: int, rev: int, spans: list[dict[str, int | str]]) -> None:
    for idx, span in enumerate(spans, start=1):
        c.execute(
            "INSERT INTO outbox_redactions (item, rev, n, kind, start, end, why) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (numeric_id, rev, idx, str(span["kind"]), int(span["start"]), int(span["end"]), str(span.get("why") or "")),
        )


def insert_draft(
    c,
    *,
    target: str,
    kind: str,
    object: str,
    audience: str,
    posts_as: str | None,
    task_id: str | None,
    ref_id: str | None,
    run_id: str | None,
    step: str | None,
    loop: str | None,
    drafted_by: str,
    slot: str,
    base_remote_updated: str | None,
    summary: str,
    body: str,
    spans: list[dict[str, int | str]],
    now: str,
    review: str = "",
    state: str = "draft",
    opened_at: str | None = None,
    approved_by: str | None = None,
    approved_rule: str | None = None,
    approved_rule_version: str | None = None,
    send_at: str | None = None,
) -> dict:
    tag = foundation.metadata(c)["instance_tag"]
    cur = c.execute(
        "INSERT INTO outbox "
        "(marker, target, kind, object, audience, posts_as, task_id, ref_id, run_id, step, loop, "
        " drafted_by, slot, rev, state, base_remote_updated, summary, plain_summary, review, "
        " shape, version, created, updated) "
        "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'draft', ?, ?, '', ?, 1, 1, ?, ?)",
        (
            target,
            kind,
            object,
            audience,
            posts_as,
            task_id,
            ref_id,
            run_id,
            step,
            loop,
            drafted_by,
            slot,
            base_remote_updated,
            summary,
            review,
            now,
            now,
        ),
    )
    numeric_id = int(cur.lastrowid)
    marker = f"arete:{tag}:ob-{numeric_id}"
    if state == "approved":
        c.execute(
            "UPDATE outbox SET marker = ?, state = 'approved', opened_at = ?, approved_by = ?, "
            "approved_rule = ?, approved_rule_version = ?, send_at = ? WHERE id = ?",
            (marker, opened_at, approved_by, approved_rule, approved_rule_version, send_at, numeric_id),
        )
    else:
        c.execute("UPDATE outbox SET marker = ? WHERE id = ?", (marker, numeric_id))
    c.execute(
        "INSERT INTO outbox_revisions (item, rev, base_rev, by, body, at) VALUES (?, 1, NULL, ?, ?, ?)",
        (numeric_id, drafted_by, body, now),
    )
    _insert_redactions(c, numeric_id, 1, spans)
    row = get_row(c, numeric_id)
    emit_event(c, "outbox.drafted", row)
    return row


def append_revision(
    c,
    numeric_id: int,
    *,
    base_rev: int,
    by: str,
    body: str,
    spans: list[dict[str, int | str]],
    now: str,
    next_state: str | None = None,
    summary: str | None = None,
    expected_version: int | None = None,
) -> dict | None:
    row = get_row(c, numeric_id)
    if row is None:
        return None
    new_rev = row["rev"] + 1
    updates: dict[str, object] = {"rev": new_rev, "updated": now}
    if next_state is not None:
        updates["state"] = next_state
    if summary:
        updates["summary"] = summary
    validate_columns(updates.keys(), _OUTBOX_UPDATABLE, "outbox")
    set_sql = ", ".join(f"{col} = ?" for col in updates) + ", version = version + 1"
    where = "id = ? AND rev = ?"
    params: list[object] = [*updates.values(), numeric_id, base_rev]
    if expected_version is not None:
        where += " AND version = ?"
        params.append(expected_version)
    changed = c.execute(f"UPDATE outbox SET {set_sql} WHERE {where}", params).rowcount  # noqa: S608
    if changed != 1:
        return None
    c.execute(
        "INSERT INTO outbox_revisions (item, rev, base_rev, by, body, at) VALUES (?, ?, ?, ?, ?, ?)",
        (numeric_id, new_rev, base_rev, by, body, now),
    )
    _insert_redactions(c, numeric_id, new_rev, spans)
    after = get_row(c, numeric_id)
    emit_event(c, "outbox.changed", after)
    return after


def cas_update(
    c,
    numeric_id: int,
    *,
    expected_version: int,
    expected_states: tuple[str, ...] | None = None,
    expected_rev: int | None = None,
    fields: dict[str, object],
    event_kind: str = "outbox.changed",
    event_extra: dict | None = None,
) -> dict | None:
    validate_columns(fields.keys(), _OUTBOX_UPDATABLE, "outbox")
    set_sql = ", ".join(f"{col} = ?" for col in fields) + ", version = version + 1"
    clauses = ["id = ?", "version = ?"]
    params: list[object] = [*fields.values(), numeric_id, expected_version]
    if expected_states is not None:
        clauses.append(f"state IN ({','.join('?' for _ in expected_states)})")
        params.extend(expected_states)
    if expected_rev is not None:
        clauses.append("rev = ?")
        params.append(expected_rev)
    where = " AND ".join(clauses)
    changed = c.execute(f"UPDATE outbox SET {set_sql} WHERE {where}", params).rowcount  # noqa: S608
    if changed != 1:
        return None
    after = get_row(c, numeric_id)
    emit_event(c, event_kind, after, event_extra)
    return after


def due_candidates(now_iso: str, connect=reader) -> list[tuple[int, int]]:
    with connect() as c:
        if not available(c):
            return []
        rows = c.execute(
            "SELECT id, version FROM outbox "
            "WHERE state = 'approved' AND send_at <= ? AND legacy_approval IS NULL "
            "ORDER BY send_at, id",
            (now_iso,),
        ).fetchall()
        return [(int(r["id"]), int(r["version"])) for r in rows]


def claim_due(c, numeric_id: int, expected_version: int, now_iso: str) -> tuple[dict, int] | None:
    changed = c.execute(
        "UPDATE outbox SET state = 'sending', version = version + 1, attempts = attempts + 1, updated = ? "
        "WHERE id = ? AND state = 'approved' AND send_at <= ? AND version = ? AND legacy_approval IS NULL "
        "AND (object = '' OR NOT EXISTS ("
        "    SELECT 1 FROM outbox earlier "
        "    WHERE earlier.object = outbox.object "
        "      AND earlier.id < outbox.id "
        "      AND earlier.state IN ('approved', 'sending') "
        "      AND earlier.legacy_approval IS NULL"
        "))",
        (now_iso, numeric_id, now_iso, expected_version),
    ).rowcount
    if changed != 1:
        return None
    row = get_row(c, numeric_id)
    attempt_n = int(row["attempts"])
    c.execute(
        "INSERT INTO delivery_attempts (item, n, started) VALUES (?, ?, ?)",
        (numeric_id, attempt_n, now_iso),
    )
    emit_event(c, "outbox.changed", row, {"attempt": attempt_n})
    return row, attempt_n


def is_state_label_body(body: str) -> bool:
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("family") == "state"


def _row_is_state_label(c, row: dict) -> bool:
    if row["kind"] != "label":
        return False
    rev = latest_revision(c, int(row["id"]), int(row["rev"]))
    return rev is not None and is_state_label_body(rev["body"])


def discard_item(
    c,
    numeric_id: int,
    now_iso: str,
    *,
    expected_states: tuple[str, ...] = ("draft", "edited", "approved", "held", "failed"),
) -> dict | None:
    row = get_row(c, numeric_id)
    if row is None or row["state"] not in expected_states:
        return None
    return cas_update(
        c,
        numeric_id,
        expected_version=int(row["version"]),
        expected_states=expected_states,
        fields={"state": "discarded", "send_at": None, "updated": now_iso},
    )


def discard_older_state_labels(c, obj: str, before_id: int, now_iso: str) -> list[int]:
    if not obj:
        return []
    rows = c.execute(
        "SELECT * FROM outbox WHERE object = ? AND kind = 'label' AND id < ? "
        "AND state IN ('draft', 'edited', 'approved', 'held', 'failed') AND legacy_approval IS NULL "
        "ORDER BY id ASC",
        (obj, before_id),
    ).fetchall()
    discarded: list[int] = []
    for raw in rows:
        row = dict(raw)
        if _row_is_state_label(c, row):
            if discard_item(c, int(row["id"]), now_iso) is not None:
                discarded.append(int(row["id"]))
    return discarded


def has_newer_active_state_label(c, obj: str, after_id: int) -> bool:
    if not obj:
        return False
    rows = c.execute(
        "SELECT * FROM outbox WHERE object = ? AND kind = 'label' AND id > ? "
        "AND state NOT IN ('rejected', 'discarded') AND legacy_approval IS NULL "
        "ORDER BY id ASC",
        (obj, after_id),
    ).fetchall()
    return any(_row_is_state_label(c, dict(raw)) for raw in rows)


def discard_older_board_items(c, obj: str, before_id: int, now_iso: str) -> list[int]:
    if not obj:
        return []
    rows = c.execute(
        "SELECT id FROM outbox WHERE object = ? AND kind = 'issue' AND slot LIKE 'board:%' AND id < ? "
        "AND state IN ('draft', 'edited', 'approved', 'held', 'failed') AND legacy_approval IS NULL "
        "ORDER BY id ASC",
        (obj, before_id),
    ).fetchall()
    discarded: list[int] = []
    for r in rows:
        if discard_item(c, int(r["id"]), now_iso) is not None:
            discarded.append(int(r["id"]))
    return discarded


def run_kind_count(c, run_id: str, kind: str) -> int:
    if not available(c):
        return 0
    row = c.execute(
        "SELECT COUNT(*) FROM outbox WHERE run_id = ? AND kind = ? AND legacy_approval IS NULL",
        (run_id, kind),
    ).fetchone()
    return int(row[0]) if row else 0


def pending_label_item(obj: str, connect=reader) -> int | None:
    if not obj:
        return None
    with connect() as c:
        if not available(c):
            return None
        row = c.execute(
            "SELECT id FROM outbox WHERE kind = 'label' AND object = ? "
            "AND state NOT IN ('sent', 'rejected', 'discarded') AND legacy_approval IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (obj,),
        ).fetchone()
        return int(row["id"]) if row is not None else None


def newest_sent_board_issue(repo: str, connect=reader) -> int | None:
    with connect() as c:
        if not available(c):
            return None
        row = c.execute(
            "SELECT sent_ref FROM outbox WHERE kind = 'issue' AND object = ? AND slot LIKE ? "
            "AND state = 'sent' AND sent_ref IS NOT NULL AND legacy_approval IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (f"github:{repo}", f"board:{repo}:create:%"),
        ).fetchone()
        if row is None or not str(row["sent_ref"]).isdigit():
            return None
        return int(row["sent_ref"])


def settle_sending(
    c,
    numeric_id: int,
    expected_version: int,
    attempt_n: int,
    *,
    item_fields: dict[str, object],
    attempt_fields: dict[str, object],
    event_kind: str,
    now_iso: str,
) -> dict | None:
    validate_columns(item_fields.keys(), _OUTBOX_UPDATABLE, "outbox")
    validate_columns(attempt_fields.keys(), _ATTEMPT_UPDATABLE, "delivery_attempts")
    full_item = {**item_fields, "updated": now_iso}
    set_item = ", ".join(f"{col} = ?" for col in full_item) + ", version = version + 1"
    changed = c.execute(
        f"UPDATE outbox SET {set_item} WHERE id = ? AND state = 'sending' AND version = ?",  # noqa: S608
        (*full_item.values(), numeric_id, expected_version),
    ).rowcount
    if changed != 1:
        return None
    full_attempt = {"ended": now_iso, **attempt_fields}
    set_attempt = ", ".join(f"{col} = ?" for col in full_attempt)
    c.execute(
        f"UPDATE delivery_attempts SET {set_attempt} WHERE item = ? AND n = ? AND ended IS NULL",  # noqa: S608
        (*full_attempt.values(), numeric_id, attempt_n),
    )
    after = get_row(c, numeric_id)
    emit_event(c, event_kind, after, {"attempt": attempt_n, "outcome": attempt_fields.get("outcome")})
    return after


def stale_sending(c, since_iso: str) -> list[dict]:
    if not available(c):
        return []
    rows = c.execute(
        "SELECT o.id, o.version, a.n "
        "FROM outbox o "
        "JOIN delivery_attempts a ON a.item = o.id AND a.ended IS NULL "
        "WHERE o.state = 'sending' AND o.legacy_approval IS NULL AND a.started < ? "
        "ORDER BY o.id",
        (since_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


def sweep_one(c, numeric_id: int, expected_version: int, attempt_n: int, now_iso: str) -> dict | None:
    changed = c.execute(
        "UPDATE outbox SET state = 'approved', version = version + 1, updated = ? "
        "WHERE id = ? AND state = 'sending' AND version = ? AND legacy_approval IS NULL",
        (now_iso, numeric_id, expected_version),
    ).rowcount
    if changed != 1:
        return None
    c.execute(
        "UPDATE delivery_attempts SET ended = ?, outcome = 'unknown', error = ? "
        "WHERE item = ? AND n = ? AND ended IS NULL",
        (now_iso, "cut off: the process sending it stopped", numeric_id, attempt_n),
    )
    after = get_row(c, numeric_id)
    emit_event(c, "outbox.changed", after, {"attempt": attempt_n, "swept": True})
    return after


def inbox_pending_items(c) -> list[dict]:
    if not available(c):
        return []
    return [
        dict(r)
        for r in c.execute(
            "SELECT * FROM outbox WHERE state IN ('draft', 'edited') AND legacy_approval IS NULL ORDER BY id ASC"
        ).fetchall()
    ]


def inbox_stale_sending_items(c, cutoff_iso: str) -> list[dict]:
    if not available(c):
        return []
    return [
        dict(r)
        for r in c.execute(
            "SELECT o.*, a.started AS sending_since "
            "FROM outbox o "
            "JOIN delivery_attempts a ON a.item = o.id AND a.ended IS NULL "
            "WHERE o.state = 'sending' AND a.started < ? "
            "ORDER BY o.id ASC",
            (cutoff_iso,),
        ).fetchall()
    ]


def run_with_tx(c, fn):
    if c is not None:
        return fn(c)
    with foundation.tx() as conn:
        return fn(conn)


def reader_for(c):
    return (lambda: nullcontext(c)) if c is not None else reader


def now_iso() -> str:
    return util.iso()
