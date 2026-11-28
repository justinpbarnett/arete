from __future__ import annotations

import json
from datetime import timedelta

from .. import util
from . import factory
from .db import db, reader

TABLES = {
    "queue": "dead rows: 56 days",
    "events": "56 days",
    "loop_state": "kept",
    "metrics": "kept",
    "findings": "kept",
}


def loop_state(name: str, connect=reader) -> dict:
    with connect() as c:
        r = c.execute("SELECT * FROM loop_state WHERE loop = ?", (name,)).fetchone()
    if not r:
        return {"loop": name, "data": {}, "last_fire": None, "next_due": None, "metric_on": 0}
    return {**dict(r), "data": json.loads(r["data"] or "{}")}


def loop_states(connect=reader) -> dict[str, dict]:
    with connect() as c:
        return {r["loop"]: {**dict(r), "data": json.loads(r["data"] or "{}")} for r in c.execute("SELECT * FROM loop_state")}


def loop_marks(connect=reader) -> dict[str, dict]:
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM loop_state")]
    return {r["loop"]: {"ts": r["mark_at"], "ok": bool(r.get("mark_ok")), "detail": r.get("mark_detail") or ""}
            for r in rows if r.get("mark_at")}


LOOP_STATE_FIELDS = frozenset({"data", "last_fire", "next_due", "metric_on", "updated",
                               "mark_ok", "mark_detail", "mark_at"})


def set_loop_state(name: str, **fields) -> None:
    if unknown := set(fields) - LOOP_STATE_FIELDS:
        raise ValueError(f"invalid loop_state fields: {', '.join(sorted(unknown))}")
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


def enqueue(loop: str, queue: str, grp: str, trigger: str, payload: list | None = None, connect=db) -> int:
    with connect() as c:
        if not payload and c.execute("SELECT 1 FROM queue WHERE loop = ? AND run_id IS NULL AND payload = '[]' "
                                     "AND trigger = ?", (loop, trigger)).fetchone():
            return 0
        cur = c.execute("INSERT INTO queue (loop, queue, grp, trigger, payload, enqueued) VALUES (?,?,?,?,?,?)",
                        (loop, queue, grp, trigger, json.dumps(payload or [], default=str), util.iso()))
        return cur.lastrowid


def pending(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM queue WHERE run_id IS NULL ORDER BY id")]


def queue_item(item_id: int) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    return {**dict(r), "payload": json.loads(r["payload"] or "[]")} if r else None


def take_queue_item(item_id: int, run_id: str) -> bool:
    with db() as c:
        return c.execute("UPDATE queue SET run_id = ? WHERE id = ? AND run_id IS NULL",
                         (run_id, item_id)).rowcount == 1


def drop_queue_item(item_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM queue WHERE id = ?", (item_id,))


def release_claimed_items(run_id: str) -> None:
    with db() as c:
        c.execute("UPDATE queue SET run_id = NULL WHERE run_id = ?", (run_id,))


def finish_claim(c, row: dict, how: str, summary: str) -> None:
    if how == "retry":
        duplicate = not json.loads(row["payload"] or "[]") and c.execute(
            "SELECT 1 FROM queue WHERE loop = ? AND trigger = ? AND payload = '[]' AND run_id IS NULL",
            (row["loop"], row["trigger"])).fetchone()
        if duplicate:
            how = "drop"
        elif row["attempts"] + 1 < 2:
            c.execute("UPDATE queue SET run_id = NULL, attempts = attempts + 1 WHERE id = ?", (row["id"],))
            return
        else:
            how = "dead"
    if how == "drop" or not json.loads(row["payload"] or "[]"):
        c.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
    else:
        c.execute("UPDATE queue SET dead_at = ?, dead_reason = ? WHERE id = ?",
                  (util.iso(), summary, row["id"]))


def end_claim(run_id: str, how: str, summary: str) -> None:
    with db() as c:
        for row in c.execute("SELECT * FROM queue WHERE run_id = ? AND dead_at IS NULL", (run_id,)).fetchall():
            finish_claim(c, dict(row), how, summary)


def release_settled_claims() -> None:
    with db() as c:
        rows = c.execute("SELECT q.*, r.status, r.summary FROM queue q LEFT JOIN runs r ON r.id = q.run_id "  # noqa: S608
                         "WHERE q.run_id IS NOT NULL AND q.dead_at IS NULL "
                         f"{factory.exclude_retained(c, 'q.run_id')} "
                         "AND (r.status IS NULL OR r.status != 'running')").fetchall()
        for row in rows:
            finish_claim(c, dict(row), "dead" if row["status"] == "stopped" else "retry",
                         row["summary"] or f"run {row['run_id']} {row['status'] or 'is missing'}")


def dead_items(connect=reader) -> list[dict]:
    with connect() as c:
        return [{**{k: row[k] for k in ("id", "loop", "run_id", "dead_reason", "dead_at")},
                 "events": len(json.loads(row["payload"] or "[]"))}
                for row in c.execute("SELECT * FROM queue WHERE dead_at IS NOT NULL ORDER BY id")]


def requeue_item(item_id: int) -> None:
    from .. import control

    with db() as c:
        if not c.execute("UPDATE queue SET run_id = NULL, attempts = 0, dead_at = NULL, dead_reason = '' "
                         "WHERE id = ? AND dead_at IS NOT NULL", (item_id,)).rowcount:
            raise control.NotFound(f"no dead queue item {item_id}")


def loop_lock(name: str) -> str:
    return "loop-" + name.replace("/", ".")


def prune(c, days: float = 14) -> None:
    c.execute("DELETE FROM queue WHERE dead_at < ?", (util.iso(util.now() - timedelta(days=days * 4)),))
    c.execute("DELETE FROM events WHERE ts < ?", (util.iso(util.now() - timedelta(days=days * 4)),))


def settled_claims(c):
    return c.execute("SELECT q.id FROM queue q LEFT JOIN runs ON runs.id = q.run_id "
                     "WHERE q.run_id IS NOT NULL AND q.run_id <> '' AND q.dead_at IS NULL "
                     "AND (runs.id IS NULL OR runs.status <> 'running') ORDER BY q.id").fetchall()
