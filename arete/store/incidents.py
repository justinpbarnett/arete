from __future__ import annotations

import json
from datetime import timedelta

from .. import util
from ..sighting import Sighting
from .db import db, reader

TABLES = {
    "incidents": "inactive: 56 days; closed evidence: configured keep_days",
    "incident_notes": "with incident; seen-again: 14 days; closed: configured keep_days",
    "incident_repairs": "with incident",
    "incident_aliases": "with incident",
    "alerts": "kept",
}


SEEN_AGAIN = "seen again"
CLOSED_AT = "COALESCE(NULLIF(closed, ''), last_seen)"


def write_incident(c, sighting: Sighting) -> tuple[int, bool]:
    fingerprint, kind, source, repo = sighting.fingerprint, sighting.kind, sighting.source, sighting.repo
    signature, count, evidence, policies = sighting.signature, sighting.count, sighting.evidence, sighting.policies
    now = util.iso()
    row = c.execute("SELECT id, count FROM incidents WHERE state = 'open' AND (fingerprint = ? OR id IN "
                    "(SELECT incident FROM incident_aliases WHERE fingerprint = ?)) ORDER BY fingerprint = ? DESC "
                    "LIMIT 1", (fingerprint, fingerprint, fingerprint)).fetchone()
    if row:
        c.execute("UPDATE incidents SET count = count + ?, last_seen = ?, evidence = ?, "
                  "policies = COALESCE(policies, ?) WHERE id = ?", (count, now, evidence, policies, row["id"]))
        note = f"{SEEN_AGAIN}: {count} more from {source}, {row['count'] + count} in all"
        opened, incident_id = False, row["id"]
    else:
        incident_id = c.execute("INSERT INTO incidents (fingerprint, class, source, repo, signature, first_seen, "
                                "last_seen, count, evidence, policies) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (fingerprint, kind, source, repo, signature, now, now, count, evidence,
                                 policies)).lastrowid
        note, opened = f"opened: {kind}, {count} from {source}", True
    c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)", (incident_id, now, note))
    return incident_id, opened


def record_incident(sighting: Sighting) -> tuple[int, bool]:
    with db() as c:
        return write_incident(c, sighting)


def add_incident_note(incident_id: int, note: str) -> None:
    with db() as c:
        c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)", (incident_id, util.iso(), note))


def open_incidents(limit: int = 500) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM incidents WHERE state = 'open' "
                                           "ORDER BY last_seen DESC, id DESC LIMIT ?", (limit,))]


def incidents_to_decide() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM incidents WHERE state = 'open' AND injection = 0 AND class != 'expected' "
            "AND decision IN ('', 'ignore') ORDER BY id")]


def known_fingerprints() -> dict[str, dict]:
    with reader() as c:
        held = {r[0]: {"source": r[1], "repo": r[2], "policies": r[3]} for r in c.execute(
            "SELECT a.fingerprint, i.source, i.repo, i.policies FROM incident_aliases a JOIN incidents i "
            "ON i.id = a.incident WHERE i.state = 'open'")}
        held.update((r[0], {"source": r[1], "repo": r[2], "policies": r[3]}) for r in c.execute(
            "SELECT fingerprint, source, repo, policies FROM incidents WHERE state = 'open'"))
        return held


INCIDENT_FIELDS = frozenset({"fingerprint", "class", "source", "repo", "signature", "state", "first_seen",
                             "last_seen", "count", "evidence", "decision", "confidence", "reason", "answers",
                             "injection", "decided", "tries", "retry_at", "decided_count", "shapes", "policies",
                             "phase", "waiting", "rearmed", "landed", "recovery", "alerted", "closed"})
REPAIR_FIELDS = frozenset({"incident", "repo", "task", "started", "ended", "outcome"})


def _check_fields(fields: dict, allowed: frozenset[str], label: str) -> None:
    if unknown := set(fields) - allowed:
        raise ValueError(f"invalid {label} fields: {', '.join(sorted(unknown))}")


def update_incident(incident_id: int, note: str = "", **fields) -> None:
    _check_fields(fields, INCIDENT_FIELDS, "incident")
    if fields.get("state", "open") != "open" and "closed" not in fields:
        fields["closed"] = util.iso()
    with db() as c:
        if fields:
            c.execute(f"UPDATE incidents SET {', '.join(f'{name} = ?' for name in fields)} WHERE id = ?",  # noqa: S608
                      (*fields.values(), incident_id))
        if note:
            c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)",
                      (incident_id, util.iso(), note))


def woken_incidents() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM incidents WHERE state = 'open' AND decision = 'wake' "
                                           "AND injection = 0 ORDER BY id")]


def closed_alerted() -> list[int]:
    with reader() as c:
        return [r[0] for r in c.execute("SELECT id FROM incidents WHERE alerted = 1 AND state != 'open' ORDER BY id")]


def add_repair(incident_id: int, repo: str) -> int:
    with db() as c:
        return c.execute("INSERT INTO incident_repairs (incident, repo, started) VALUES (?,?,?)",
                         (incident_id, repo, util.iso())).lastrowid


def set_repair(repair_id: int, **fields) -> None:
    _check_fields(fields, REPAIR_FIELDS, "incident_repairs")
    if not fields:
        return
    with db() as c:
        c.execute(f"UPDATE incident_repairs SET {', '.join(f'{name} = ?' for name in fields)} WHERE id = ?",  # noqa: S608
                  (*fields.values(), repair_id))


def incident_repairs(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM incident_repairs ORDER BY id")]


def parsed(row: dict) -> dict:
    return {**row, "answers": json.loads(row["answers"] or "{}")}


def incidents(connect=reader, limit: int = 200, open_first: bool = False) -> list[dict]:
    order = "state != 'open', " if open_first else ""
    with connect() as c:
        return [parsed(dict(r)) for r in c.execute(f"SELECT * FROM incidents ORDER BY {order}last_seen DESC, id DESC "  # noqa: S608
                                                   "LIMIT ?", (limit,))]


def blocked_incidents(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT id, class, source, signature, waiting, last_seen FROM incidents "
                                           "WHERE state = 'open' AND phase = 'blocked' ORDER BY id")]


def incident(incident_id: int, connect=reader, last_notes: int = 0) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not r:
            return None
        total = c.execute("SELECT COUNT(*) FROM incident_notes WHERE incident = ?", (incident_id,)).fetchone()[0]
        if last_notes > 0:
            notes = c.execute("SELECT ts, note FROM (SELECT id, ts, note FROM incident_notes WHERE incident = ? "
                              "ORDER BY id DESC LIMIT ?) ORDER BY id", (incident_id, last_notes))
        else:
            notes = c.execute("SELECT ts, note FROM incident_notes WHERE incident = ? ORDER BY id", (incident_id,))
        repairs = c.execute("SELECT * FROM incident_repairs WHERE incident = ? ORDER BY id", (incident_id,))
        return {**parsed(dict(r)), "notes": [dict(n) for n in notes], "notes_total": total,
                "repairs": [dict(x) for x in repairs]}


def alert_get(key: str) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
        return dict(r) if r else None


def alert_set(key: str, issue: int | None, repo: str = "") -> None:
    prev = alert_get(key)
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO alerts (key, issue, opened, last_seen, repo) VALUES (?,?,?,?,?)",
            (key, issue, (prev or {}).get("opened") or util.iso(), util.iso(), repo or (prev or {}).get("repo") or ""),
        )


def alert_clear(key: str) -> None:
    with db() as c:
        c.execute("DELETE FROM alerts WHERE key = ?", (key,))


def alerts(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY opened")]


def prune(c, days: float = 14, keep_days: float = 30) -> None:
    since = util.iso(util.now() - timedelta(days=days))
    old = util.iso(util.now() - timedelta(days=days * 4))
    kept = util.iso(util.now() - timedelta(days=keep_days))
    done = f"SELECT id FROM incidents WHERE state != 'open' AND {CLOSED_AT} < ?"  # noqa: S608
    c.execute("DELETE FROM incident_notes WHERE ts < ? AND note LIKE ?", (since, f"{SEEN_AGAIN}:%"))
    c.execute(f"DELETE FROM incident_notes WHERE incident IN ({done})", (kept,))  # noqa: S608
    c.execute(f"UPDATE incidents SET evidence = '' WHERE id IN ({done})", (kept,))  # noqa: S608
    c.execute(f"DELETE FROM incidents WHERE last_seen < ? AND (state = 'open' OR {CLOSED_AT} < ?)", (old, kept))  # noqa: S608
    c.execute("DELETE FROM incident_notes WHERE incident NOT IN (SELECT id FROM incidents)")
    c.execute("DELETE FROM incident_aliases WHERE incident NOT IN (SELECT id FROM incidents)")
    c.execute("DELETE FROM incident_repairs WHERE incident NOT IN (SELECT id FROM incidents)")


def write_alias(c, fingerprint: str, incident_id: int, note: str) -> None:
    c.execute("INSERT OR REPLACE INTO incident_aliases (fingerprint, incident, ts) VALUES (?,?,?)",
              (fingerprint, incident_id, util.iso()))
    c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)",
              (incident_id, util.iso(), note))


def write_mark(c, incident_id: int, fields: dict, note: str) -> None:
    _check_fields(fields, INCIDENT_FIELDS, "incident")
    c.execute(f"UPDATE incidents SET {', '.join(f'{name} = ?' for name in fields)} WHERE id = ?",  # noqa: S608
              (*fields.values(), incident_id))
    c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)",
              (incident_id, util.iso(), note))
