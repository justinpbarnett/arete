from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from .. import util
from ._rows import json_dict, json_list, validate_columns
from .db import db, reader, tx

TABLES = {
    "probes": "kept",
    "holds": "until cleared",
    "device_probes": "90 days",
    "leases": "kept",
    "lease_tokens": "7 days after lease released",
    "run_controls": "kept",
    "run_streams": "kept",
    "waits": "90 days after cleared",
}

LEASE_MUTABLE_COLUMNS = frozenset((
    "vm_name",
    "vmid",
    "ready",
    "address",
    "host_key_fingerprint",
    "released",
    "outcome",
    "kept_reason",
    "sweep_attempts",
    "failing_since",
    "last_sweep",
    "last_sweep_error",
    "sweep_gave_up",
    "token_hash",
))

DEVICE_PROBES_RETENTION_DAYS = 90
LEASE_TOKENS_RETENTION_DAYS = 7
WAITS_RETENTION_DAYS = 90

REFRESH_SCOPE = "credential-refresh-v1:"
LEGACY_SCOPES: dict[str, str] = {}
LEGACY_SCOPE_LIMIT = 256


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def remember_legacy_scope(current: str, legacy: str) -> None:
    if not current or not legacy or current == legacy:
        return
    if len(LEGACY_SCOPES) >= LEGACY_SCOPE_LIMIT:
        LEGACY_SCOPES.clear()
    LEGACY_SCOPES[current] = legacy


def hold(scope: str, until: str, why: str) -> None:
    with tx() as c:
        links = refresh_links(c)
        ts = util.iso()
        for linked in linked_scopes(scope, links):
            save_hold(c, linked, until, why, ts)


def save_hold(c, scope: str, until: str, why: str, ts: str) -> None:
    c.execute("INSERT INTO holds (scope, until, why, ts) VALUES (?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
              "until = CASE WHEN holds.until = '' OR excluded.until = '' THEN '' "
              "ELSE max(holds.until, excluded.until) END, "
              "why = CASE WHEN holds.until != '' AND (excluded.until = '' OR excluded.until > holds.until) "
              "THEN excluded.why ELSE holds.why END, ts = max(holds.ts, excluded.ts)", (scope, until, why, ts))


def holds(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM holds WHERE scope NOT LIKE ? "
                                           "AND (until > ? OR until = '') ORDER BY until",
                                           (REFRESH_SCOPE + "%", util.iso()))]


def migrate_holds(aliases: dict[str, str]) -> list[dict]:
    with tx() as c:
        links = refresh_links(c)
        now = util.iso()
        for before, after in aliases.items():
            row = c.execute("SELECT * FROM holds WHERE scope = ? AND (until = '' OR until > ?)",
                            (before, now)).fetchone()
            if row:
                for linked in linked_scopes(after, links):
                    save_hold(c, linked, row["until"], row["why"], row["ts"])
                c.execute("DELETE FROM holds WHERE scope = ?", (before,))
        return holds()


def clear_holds(scopes: list[str], set_before: str) -> None:
    expanded = list(scopes)
    for scope in scopes:
        if legacy := LEGACY_SCOPES.get(scope):
            expanded.append(legacy)
    with tx() as c:
        links = refresh_links(c)
        linked = {s for scope in expanded for s in linked_scopes(scope, links)}
        c.executemany("DELETE FROM holds WHERE scope = ? AND ts < ?", [(s, set_before) for s in linked])


def refresh_links(c) -> list[tuple[str, str, str, str]]:
    links = []
    for row in c.execute("SELECT scope, why FROM holds WHERE scope LIKE ?", (REFRESH_SCOPE + "%",)):
        try:
            before, after = row["scope"][len(REFRESH_SCOPE):].split(":")
            previous, latest = json.loads(row["why"])
            if before and after and all(isinstance(s, str) and s for s in (previous, latest)):
                links.append((before, after, previous, latest))
        except (ValueError, TypeError):
            continue
    return links


def linked_scopes(scope: str, links: list[tuple[str, str, str, str]]) -> set[str]:
    found, pending = set(), [scope]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        for _, _, previous, latest in links:
            for before, after in ((previous, latest), (latest, previous)):
                if current.startswith(before):
                    linked = after + current[len(before):]
                    if linked not in found:
                        pending.append(linked)
    return found


def refresh_continues(previous: str, latest: str, connect=reader) -> bool:
    if not previous or not latest:
        return False
    if previous == latest:
        return True
    with connect() as c:
        links = refresh_links(c)
    found, pending = set(), [previous]
    while pending:
        current = pending.pop()
        if current == latest:
            return True
        if current not in found:
            found.add(current)
            pending.extend(after for before, after, _, _ in links if before == current and after not in found)
    return False


def record_refresh(before: str, after: str, previous: str, latest: str) -> None:
    if not all((before, after, previous, latest)) or before == after:
        return
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO holds (scope, until, why, ts) VALUES (?,?,?,?)",
                  (REFRESH_SCOPE + before + ":" + after, "", json.dumps([previous, latest]), util.iso()))
        links = refresh_links(c)
        rows = c.execute("SELECT * FROM holds WHERE scope NOT LIKE ? AND (until = '' OR until > ?)",
                         (REFRESH_SCOPE + "%", util.iso())).fetchall()
        for row in rows:
            for scope in linked_scopes(row["scope"], links) - {row["scope"]}:
                save_hold(c, scope, row["until"], row["why"], row["ts"])


def probe_get(name: str) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM probes WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None


def probe_set(name: str, ok: bool, detail: str) -> None:
    ts = util.iso()
    prev = probe_get(name) or {}
    last_ok = ts if ok else prev.get("last_ok")
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO probes (name, last_run, last_ok, ok, detail) VALUES (?,?,?,?,?)",
            (name, ts, last_ok, int(ok), detail[:500]),
        )


def probes(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM probes ORDER BY name")]


# --- Device probes (rows in device_probes) ---

def _decode_device_probe(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["ok"] = bool(d["ok"])
    d["tools"] = json_list(d.get("tools"))
    d["gpus"] = json_list(d.get("gpus"))
    d["facts"] = json_dict(d.get("facts"))
    return d


def insert_device_probe(
    c: sqlite3.Connection,
    *,
    device: str,
    device_version: str,
    at: str,
    ok: bool,
    seconds: float,
    system: str = "",
    home: str = "",
    tools: list | None = None,
    gpus: list | None = None,
    detail: str = "",
    facts: dict | None = None,
) -> dict:
    cur = c.execute(
        "INSERT INTO device_probes "
        "(device, device_version, at, ok, seconds, system, home, tools, gpus, detail, facts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            device,
            device_version,
            at,
            int(bool(ok)),
            float(seconds),
            str(system or ""),
            str(home or ""),
            json.dumps(list(tools or ())),
            json.dumps(list(gpus or ())),
            str(detail or ""),
            json.dumps(facts or {}),
        ),
    )
    row = c.execute("SELECT * FROM device_probes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _decode_device_probe(row)


def latest_device_probe(device: str, connect=reader) -> dict | None:
    with connect() as c:
        if not _has_table(c, "device_probes"):
            return None
        row = c.execute(
            "SELECT * FROM device_probes WHERE device = ? ORDER BY at DESC, id DESC LIMIT 1",
            (device,),
        ).fetchone()
        return _decode_device_probe(row) if row else None


def latest_device_probes(connect=reader) -> dict[str, dict]:
    with connect() as c:
        if not _has_table(c, "device_probes"):
            return {}
        rows = c.execute(
            "SELECT dp.* FROM device_probes dp "
            "JOIN (SELECT device, MAX(id) AS max_id FROM device_probes GROUP BY device) latest "
            "ON latest.max_id = dp.id ORDER BY dp.device"
        ).fetchall()
        return {row["device"]: _decode_device_probe(row) for row in rows}


# --- Leases & Lease Tokens ---

def insert_lease(
    c: sqlite3.Connection,
    *,
    run_id: str | None,
    device: str,
    device_version: str,
    created: str,
    expires: str,
) -> int:
    cur = c.execute(
        "INSERT INTO leases (run_id, device, device_version, epoch, token_hash, created, renewed, expires, version) "
        "VALUES (?, ?, ?, 1, '', ?, '', ?, 1)",
        (run_id, device, device_version, created, expires),
    )
    return int(cur.lastrowid)


def set_lease_token(c: sqlite3.Connection, lease_id: int, token_hash: str, created: str) -> None:
    c.execute("UPDATE leases SET token_hash = ? WHERE id = ?", (token_hash, lease_id))
    c.execute(
        "INSERT INTO lease_tokens (lease_id, token_hash, created, dead_at) VALUES (?, ?, ?, NULL)",
        (lease_id, token_hash, created),
    )


def lease_get(lease_id: int, connect=reader) -> dict | None:
    with connect() as c:
        if not _has_table(c, "leases"):
            return None
        row = c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        return dict(row) if row else None


def leases_list(
    *,
    run_id: str | None = None,
    device: str | None = None,
    only_open: bool | None = None,
    connect=reader,
) -> list[dict]:
    with connect() as c:
        if not _has_table(c, "leases"):
            return []
        clauses: list[str] = []
        args: list[object] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            args.append(run_id)
        if device is not None:
            clauses.append("device = ?")
            args.append(device)
        if only_open is True:
            clauses.append("released IS NULL")
        elif only_open is False:
            clauses.append("released IS NOT NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            dict(r)
            for r in c.execute(f"SELECT * FROM leases {where} ORDER BY id", args)  # noqa: S608
        ]


def open_lease_ids_by_device(connect=reader) -> dict[str, list[str]]:
    with connect() as c:
        if not _has_table(c, "leases"):
            return {}
        found: dict[str, list[str]] = {}
        for row in c.execute("SELECT id, device FROM leases WHERE released IS NULL ORDER BY id"):
            found.setdefault(row["device"], []).append(f"L-{row['id']}")
        return found


def update_lease_fields(c: sqlite3.Connection, lease_id: int, fields: dict[str, object]) -> dict | None:
    if not fields:
        row = c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        return dict(row) if row else None
    validate_columns(fields.keys(), LEASE_MUTABLE_COLUMNS, "leases")
    assignments = ", ".join(f"{col} = ?" for col in fields)
    values = [*fields.values(), lease_id]
    c.execute(f"UPDATE leases SET {assignments}, version = version + 1 WHERE id = ?", values)  # noqa: S608
    row = c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
    return dict(row) if row else None


def kill_lease_tokens(c: sqlite3.Connection, lease_id: int, at: str) -> None:
    c.execute("UPDATE lease_tokens SET dead_at = ? WHERE lease_id = ? AND dead_at IS NULL", (at, lease_id))


def renew_lease(c: sqlite3.Connection, lease_id: int, epoch: int, now_iso: str, new_expires: str) -> bool:
    cur = c.execute(
        "UPDATE leases SET renewed = ?, expires = ? "
        "WHERE id = ? AND epoch = ? AND released IS NULL AND expires >= ?",
        (now_iso, new_expires, lease_id, epoch, now_iso),
    )
    return cur.rowcount == 1


def extend_all_open_leases(c: sqlite3.Connection, new_expires: str) -> int:
    if not _has_table(c, "leases"):
        return 0
    cur = c.execute("UPDATE leases SET expires = ? WHERE released IS NULL", (new_expires,))
    return cur.rowcount


def expired_open_leases(now_iso: str, connect=reader) -> list[dict]:
    with connect() as c:
        if not _has_table(c, "leases"):
            return []
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM leases WHERE released IS NULL AND expires < ? ORDER BY id",
                (now_iso,),
            )
        ]


def sweepable_leases(
    lapsed_before: str,
    unanswered_before: str,
    connect=reader,
) -> list[dict]:
    with connect() as c:
        if not _has_table(c, "leases"):
            return []
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM leases "
                "WHERE vm_name != '' AND sweep_gave_up = 0 AND ("
                "  (outcome = 'expired' AND released IS NOT NULL AND released < ?) OR "
                "  (outcome = 'never_answered' AND created < ?) OR "
                "  (outcome = 'delete_failed')"
                ") ORDER BY created, id",
                (lapsed_before, unanswered_before),
            )
        ]


def delete_failed_leases_of_run(run_id: str, connect=reader) -> list[dict]:
    with connect() as c:
        if not _has_table(c, "leases"):
            return []
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM leases WHERE run_id = ? AND outcome = 'delete_failed' AND vm_name != '' "
                "ORDER BY created, id",
                (run_id,),
            )
        ]


def leases_of_run_for_release(run_id: str, connect=reader) -> list[dict]:
    with connect() as c:
        if not _has_table(c, "leases"):
            return []
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM leases WHERE run_id = ? AND ("
                "  released IS NULL OR (outcome = 'delete_failed' AND vm_name != '' AND sweep_gave_up = 0)"
                ") ORDER BY created, id",
                (run_id,),
            )
        ]


def latest_lease_for_run(run_id: str, connect=reader) -> dict | None:
    with connect() as c:
        if not _has_table(c, "leases"):
            return None
        row = c.execute(
            "SELECT * FROM leases WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return dict(row) if row else None


# --- Waits ---

def open_wait(c: sqlite3.Connection, subject: str, blocker: str, reason: str = "") -> dict | None:
    if not _has_table(c, "waits"):
        return None
    now = util.iso()
    c.execute(
        "INSERT OR IGNORE INTO waits (subject, blocker, reason, opened, cleared) VALUES (?, ?, ?, ?, NULL)",
        (subject, blocker, str(reason or "")[:500], now),
    )
    row = c.execute(
        "SELECT * FROM waits WHERE subject = ? AND blocker = ? AND cleared IS NULL ORDER BY id DESC LIMIT 1",
        (subject, blocker),
    ).fetchone()
    return dict(row) if row else None


def clear_waits_for_blocker(c: sqlite3.Connection, blocker: str) -> int:
    if not _has_table(c, "waits"):
        return 0
    cur = c.execute(
        "UPDATE waits SET cleared = ? WHERE blocker = ? AND cleared IS NULL",
        (util.iso(), blocker),
    )
    return cur.rowcount


def open_waits_for_subject(subject: str, connect=reader) -> list[dict]:
    with connect() as c:
        if not _has_table(c, "waits"):
            return []
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM waits WHERE subject = ? AND cleared IS NULL ORDER BY opened, id",
                (subject,),
            )
        ]


def open_device_wait_blockers(connect=reader) -> list[str]:
    with connect() as c:
        if not _has_table(c, "waits"):
            return []
        return [
            row["blocker"].removeprefix("device:")
            for row in c.execute(
                "SELECT DISTINCT blocker FROM waits WHERE cleared IS NULL AND blocker LIKE 'device:%' ORDER BY blocker"
            )
        ]


# --- Retention Prune ---

def prune(c: sqlite3.Connection) -> None:
    now = util.now()
    if _has_table(c, "device_probes"):
        c.execute(
            "DELETE FROM device_probes WHERE at < ?",
            (util.iso(now - timedelta(days=DEVICE_PROBES_RETENTION_DAYS)),),
        )
    if _has_table(c, "lease_tokens") and _has_table(c, "leases"):
        c.execute(
            "DELETE FROM lease_tokens WHERE lease_id IN ("
            "  SELECT id FROM leases WHERE released IS NOT NULL AND released < ?"
            ")",
            (util.iso(now - timedelta(days=LEASE_TOKENS_RETENTION_DAYS)),),
        )
    if _has_table(c, "waits"):
        c.execute(
            "DELETE FROM waits WHERE cleared IS NOT NULL AND cleared < ?",
            (util.iso(now - timedelta(days=WAITS_RETENTION_DAYS)),),
        )
