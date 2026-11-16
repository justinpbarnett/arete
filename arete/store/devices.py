from __future__ import annotations

import json

from .. import util
from .db import db, reader, tx

TABLES = {
    "vms": "until forgotten",
    "probes": "kept",
    "holds": "until cleared",
}


REFRESH_SCOPE = "credential-refresh-v1:"


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


def clear_holds(scopes: list[str], set_before: str) -> None:
    with tx() as c:
        links = refresh_links(c)
        linked = {s for scope in scopes for s in linked_scopes(scope, links)}
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


def add_vm(name: str, run_id: str | None, device: dict, vmid: int | None = None, connect=db) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO vms (name, run_id, device, vmid, created) VALUES (?,?,?,?,?)",
                  (name, run_id, json.dumps(device), vmid, util.iso()))


def note_vm_answered(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("UPDATE vms SET answered = 1 WHERE name = ?", (name,))


def forget_vm(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM vms WHERE name = ?", (name,))


def forget_gone_vm(name: str, unanswered_before: str, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM vms WHERE name = ? AND (answered = 1 OR created < ?)", (name, unanswered_before))


def note_delete_failed(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("UPDATE vms SET failing_since = COALESCE(failing_since, ?) WHERE name = ?", (util.iso(), name))


def vms_of_run(run_id: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "device": json.loads(r["device"])}
                for r in c.execute("SELECT * FROM vms WHERE run_id = ? ORDER BY created", (run_id,))]


def vms_left_over(probes_created_before: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "device": json.loads(r["device"])} for r in c.execute(
            "SELECT * FROM vms v WHERE (v.run_id IS NULL AND v.created < ?) OR (v.run_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM runs r WHERE r.id = v.run_id AND r.status = 'running')) ORDER BY created",
            (probes_created_before,))]


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
