from __future__ import annotations

import json
from datetime import timedelta

from .. import util
from . import factory
from . import inbox as inbox_store
from ._rows import json_dict, json_list, validate_columns
from .db import db, reader, tx

DEFAULT_MAX_ATTEMPTS = 2

TABLES = {
    "queue": "dead rows: 56 days",
    "events": "56 days",
    "loop_state": "kept",
    "metrics": "kept",
    "findings": "kept",
}

LOOP_STATE_UPDATABLE_FIELDS = frozenset((
    "data", "last_fire", "next_due", "metric_on", "updated", "mark_ok", "mark_detail", "mark_at",
))


def loop_state(name: str, connect=reader) -> dict:
    with connect() as c:
        r = c.execute("SELECT * FROM loop_state WHERE loop = ?", (name,)).fetchone()
    if not r:
        return {"loop": name, "data": {}, "last_fire": None, "next_due": None, "metric_on": 0}
    return {**dict(r), "data": json_dict(r["data"])}


def loop_states(connect=reader) -> dict[str, dict]:
    with connect() as c:
        return {r["loop"]: {**dict(r), "data": json_dict(r["data"])} for r in c.execute("SELECT * FROM loop_state")}


def loop_marks(connect=reader) -> dict[str, dict]:
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM loop_state")]
    return {r["loop"]: {"ts": r["mark_at"], "ok": bool(r.get("mark_ok")), "detail": r.get("mark_detail") or ""}
            for r in rows if r.get("mark_at")}


def set_loop_state(name: str, **fields) -> None:
    validate_columns(fields, LOOP_STATE_UPDATABLE_FIELDS, "loop_state")
    if "data" in fields:
        fields["data"] = json.dumps(fields["data"], default=str)
    fields["updated"] = util.iso()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO loop_state (loop) VALUES (?)", (name,))
        c.execute(f"UPDATE loop_state SET {', '.join(f'{k} = ?' for k in fields)} WHERE loop = ?",  # noqa: S608
                  (*fields.values(), name))


def finding_seen(loop: str, fingerprint: str) -> bool:
    with reader() as c:
        return c.execute("SELECT 1 FROM findings WHERE loop = ? AND fingerprint = ?", (loop, fingerprint)).fetchone() \
            is not None


def finding_settled(loop: str, fingerprint: str) -> bool:
    with reader() as c:
        return c.execute(
            "SELECT 1 FROM findings WHERE loop = ? AND fingerprint = ? AND (emitted != '' OR COALESCE(url, '') != '')",
            (loop, fingerprint),
        ).fetchone() is not None


def record_finding(loop: str, fingerprint: str, fingerprint_text: str, title: str, url: str, run_id: str) -> bool:
    with db() as c:
        before = c.total_changes
        c.execute("INSERT OR IGNORE INTO findings (loop, fingerprint, fingerprint_text, title, url, run_id, first_seen, "
                  "last_seen, emitted) VALUES (?,?,?,?,?,?,?,?,'')",
                  (loop, fingerprint, fingerprint_text, title, url, run_id, util.iso(), util.iso()))
        if c.total_changes > before:
            return True
        c.execute("UPDATE findings SET run_id = ? WHERE loop = ? AND fingerprint = ? AND emitted = '' "
                  "AND COALESCE(url, '') = '' AND COALESCE(run_id, '') != ?", (run_id, loop, fingerprint, run_id))
        taken_over = c.total_changes > before
        c.execute("UPDATE findings SET seen = seen + 1, last_seen = ? WHERE loop = ? AND fingerprint = ?",
                  (util.iso(), loop, fingerprint))
        return taken_over


def mark_emitted(loop: str, fingerprints: list[str], run_id: str) -> None:
    with db() as c:
        c.executemany("UPDATE findings SET emitted = ? WHERE loop = ? AND fingerprint = ? AND run_id = ? "
                      "AND emitted = ''", [(run_id, loop, fingerprint, run_id) for fingerprint in fingerprints])


def set_finding_url(loop: str, fingerprint: str, url: str) -> None:
    with db() as c:
        c.execute("UPDATE findings SET url = ? WHERE loop = ? AND fingerprint = ?", (url, loop, fingerprint))


def findings(loop: str, limit: int = 50, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM findings WHERE loop = ? ORDER BY last_seen DESC LIMIT ?",
                                           (loop, limit))]


def add_event(loop: str, e: dict, connect=db) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO events (loop, type, source, key, repo, title, url, trust, ts) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (loop, e.get("type"), e.get("source"), str(e.get("key")), e.get("repo"), e.get("title"),
                         e.get("url"), e.get("trust"), util.iso()))
        return cur.lastrowid


def _loop_max_attempts() -> dict[str, int]:
    from .. import config, loops

    cfg, problem = config.load_or_problem()
    if problem:
        return {}
    return {
        loop.name: DEFAULT_MAX_ATTEMPTS if loop.error else loop.max_attempts
        for loop in loops.load_all(cfg)
    }


def enqueue(
    loop: str,
    queue: str,
    grp: str,
    trigger: str,
    payload: list | None = None,
    connect=tx,
    *,
    c=None,
) -> int:
    if c is not None:
        cur = c.execute(
            "INSERT OR IGNORE INTO queue (loop, queue, grp, trigger, payload, enqueued) VALUES (?,?,?,?,?,?)",
            (loop, queue, grp, trigger, json.dumps(payload or [], default=str), util.iso()),
        )
        return cur.lastrowid if cur.rowcount == 1 else 0
    with connect() as conn:
        return enqueue(loop, queue, grp, trigger, payload, c=conn)


def pending(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM queue WHERE run_id IS NULL AND dead_at IS NULL ORDER BY id")]


def queue_item(item_id: int, c=None) -> dict | None:
    if c is not None:
        r = c.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
        return {**dict(r), "payload": json_list(r["payload"])} if r else None
    with reader() as conn:
        return queue_item(item_id, conn)


def claim(c, item_id: int, run_id: str) -> bool:
    return c.execute(
        "UPDATE queue SET run_id = ? WHERE id = ? AND run_id IS NULL AND dead_at IS NULL",
        (run_id, item_id),
    ).rowcount == 1


def drop_queue_item(c, item_id: int) -> None:
    c.execute("DELETE FROM queue WHERE id = ?", (item_id,))


def finish_claim(
    c,
    row: dict,
    how: str,
    summary: str,
    *,
    count_attempt: bool = True,
    max_attempts: int | None = None,
) -> None:
    payload = json_list(row.get("payload"))
    if how in ("release", "retry"):
        if not count_attempt:
            if c.execute("UPDATE OR IGNORE queue SET run_id = NULL WHERE id = ?", (row["id"],)).rowcount == 0:
                c.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
            return
        limit = _loop_max_attempts().get(row["loop"], DEFAULT_MAX_ATTEMPTS) if max_attempts is None else max_attempts
        if int(row.get("attempts") or 0) + 1 < limit:
            if c.execute(
                "UPDATE OR IGNORE queue SET run_id = NULL, attempts = attempts + 1, last_run = ? WHERE id = ?",
                (row["run_id"], row["id"]),
            ).rowcount == 0:
                c.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
            return
        how = "dead"
    if how == "drop" or not payload:
        c.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
        return
    c.execute("UPDATE queue SET dead_at = ?, dead_reason = ? WHERE id = ?", (util.iso(), summary, row["id"]))
    dead_row = c.execute(
        "SELECT id, loop, run_id, dead_reason, attempts, COALESCE(json_array_length(payload), 0) AS events_count "
        "FROM queue WHERE id = ?",
        (row["id"],),
    ).fetchone()
    if dead_row is not None:
        from .. import inbox

        inbox.raise_queue_dead(c, dict(dead_row), dead_row["events_count"])


def end_claim(
    c,
    run_id: str,
    how: str,
    summary: str = "",
    *,
    count_attempt: bool = True,
    max_attempts: int | None = None,
) -> None:
    for row in c.execute("SELECT * FROM queue WHERE run_id = ? AND dead_at IS NULL ORDER BY id", (run_id,)).fetchall():
        finish_claim(c, dict(row), how, summary, count_attempt=count_attempt, max_attempts=max_attempts)


def release_settled_claims(c=None) -> int:
    if c is None:
        with tx() as conn:
            return release_settled_claims(conn)
    rows = c.execute(
        "SELECT q.*, r.status, r.summary FROM queue q LEFT JOIN runs r ON r.id = q.run_id "  # noqa: S608
        "WHERE q.run_id IS NOT NULL AND q.dead_at IS NULL "
        f"{factory.exclude_retained(c, 'q.run_id')} "
        "AND (r.status IS NULL OR r.status != 'running') "
        "ORDER BY q.id",
    ).fetchall()
    if not rows:
        return 0
    limits: dict[str, int] | None = None
    for row in rows:
        row_dict = dict(row)
        how = "dead" if row_dict["status"] == "stopped" else "release"
        if how == "release" and limits is None:
            limits = _loop_max_attempts()
        limit = limits.get(row_dict["loop"], DEFAULT_MAX_ATTEMPTS) if limits is not None else None
        finish_claim(
            c,
            row_dict,
            how,
            row_dict["summary"] or f"run {row_dict['run_id']} {row_dict['status'] or 'is missing'}",
            max_attempts=limit,
        )
    return len(rows)


def dead_items(connect=reader) -> list[dict]:
    with connect() as c:
        inbox_ids: dict[str, str] = {}
        if inbox_store.available(c):
            for r in c.execute(
                "SELECT id, subject FROM inbox WHERE kind = 'queue.dead' AND state != 'resolved' ORDER BY id"
            ):
                inbox_ids[r["subject"]] = inbox_store.format_id(r["id"])
        return [
            {
                **{k: row[k] for k in ("id", "loop", "run_id", "dead_reason", "dead_at")},
                "events": len(json_list(row["payload"])),
                "inbox_id": inbox_ids.get(f"queue:{row['id']}", ""),
            }
            for row in c.execute("SELECT * FROM queue WHERE dead_at IS NOT NULL ORDER BY id")
        ]


def _resolve_dead_inbox(
    c,
    item_id: int,
    resolution: str,
    *,
    answer: str = "",
    answered_by: str = "",
    answered_at: str = "",
    base_version: int | None = None,
) -> dict | None:
    if not inbox_store.available(c):
        return None
    row = c.execute(
        "SELECT id FROM inbox WHERE kind = 'queue.dead' AND subject = ? AND state != 'resolved' ORDER BY id DESC LIMIT 1",
        (f"queue:{item_id}",),
    ).fetchone()
    if row is None:
        return None
    resolved, _, ok = inbox_store.resolve_item(
        c,
        row["id"],
        resolution=resolution,
        answer=answer,
        answered_by=answered_by,
        answered_at=answered_at,
        base_version=base_version,
    )
    return resolved if ok else None


def requeue_item(
    c,
    item_id: int,
    *,
    answered_by: str = "",
    answered_at: str = "",
    base_version: int | None = None,
) -> dict | None:
    from .. import control

    if not c.execute(
        "UPDATE queue SET run_id = NULL, attempts = 0, dead_at = NULL, dead_reason = '' "
        "WHERE id = ? AND dead_at IS NOT NULL",
        (item_id,),
    ).rowcount:
        raise control.NotFound(f"no dead queue item {item_id}")
    return _resolve_dead_inbox(
        c,
        item_id,
        "requeued",
        answer="requeue",
        answered_by=answered_by,
        answered_at=answered_at,
        base_version=base_version,
    )


def dismiss_item(
    c,
    item_id: int,
    *,
    answered_by: str = "",
    answered_at: str = "",
    base_version: int | None = None,
) -> dict | None:
    from .. import control

    if not c.execute(
        "DELETE FROM queue WHERE id = ? AND dead_at IS NOT NULL",
        (item_id,),
    ).rowcount:
        raise control.NotFound(f"no dead queue item {item_id}")
    return _resolve_dead_inbox(
        c,
        item_id,
        "dismissed",
        answer="dismiss",
        answered_by=answered_by,
        answered_at=answered_at,
        base_version=base_version,
    )


def loop_lock(name: str) -> str:
    return "loop-" + name.replace("/", ".")


def prune(c, days: float = 14) -> None:
    cutoff = util.iso(util.now() - timedelta(days=days * 4))
    expired = [
        row["id"]
        for row in c.execute(
            "SELECT id FROM queue WHERE dead_at IS NOT NULL AND dead_at < ? ORDER BY id",
            (cutoff,),
        ).fetchall()
    ]
    for item_id in expired:
        c.execute("DELETE FROM queue WHERE id = ?", (item_id,))
        _resolve_dead_inbox(c, item_id, "expired")
    c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))


def settled_claims(c):
    return c.execute("SELECT q.id FROM queue q LEFT JOIN runs ON runs.id = q.run_id "
                     "WHERE q.run_id IS NOT NULL AND q.run_id <> '' AND q.dead_at IS NULL "
                     "AND (runs.id IS NULL OR runs.status <> 'running') ORDER BY q.id").fetchall()
