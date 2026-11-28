from __future__ import annotations

import hashlib
import json

PHASE = "backfill"

_EMPTY_RAW_SHA = hashlib.sha256(b"{}").hexdigest()


def _open_rescope_held(conn) -> dict[tuple[str, str], dict]:
    """Return {(source, key): event_dict} from open #306 rescope inbox items."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inbox'"
    ).fetchone():
        return {}
    found: dict[tuple[str, str], dict] = {}
    rows = conn.execute(
        "SELECT subject, detail FROM inbox WHERE kind = 'rescope' AND state != 'resolved' ORDER BY id"
    ).fetchall()
    for row in rows:
        subject = str(row["subject"] or "")
        if not subject.startswith("source:"):
            continue
        sid = subject.removeprefix("source:")
        try:
            detail = json.loads(row["detail"] or "{}")
        except ValueError:
            continue
        if not isinstance(detail, dict):
            continue
        for ev in detail.get("held") or ():
            if isinstance(ev, dict) and ev.get("key") is not None:
                found[(sid, str(ev["key"]))] = ev
    return found


def backfill_seen_rows(conn) -> int:
    """Idempotently copy rows from legacy seen table into intake_items."""
    from arete import config, loops, sources, util
    from arete.store import intake as intake_store

    if not intake_store.available(conn):
        return 0
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'seen'"
    ).fetchone():
        return 0

    rows = conn.execute('SELECT source, key, ts FROM "seen" ORDER BY source, key').fetchall()
    if not rows:
        return 0

    cfg, problem = config.load_or_problem()
    loaded_by_id: dict[str, sources.Src] = {}
    if not problem:
        try:
            all_loops = loops.load_all(cfg, check=False)
            loaded_by_id = {s.id: s for s in sources.load_all(cfg, all_loops, include_derived=False)}
        except Exception:
            loaded_by_id = {}

    held_map = _open_rescope_held(conn)
    inserted = 0
    for row in rows:
        sid = str(row["source"])
        raw_key = str(row["key"])
        received = str(row["ts"] or util.iso())
        src = loaded_by_id.get(sid)

        if (sid, raw_key) in held_map:
            ev = held_map[(sid, raw_key)]
            origin_id, ext_key, ext_rev, kind = sources.intake_identity_for_event(src, cfg, ev, source_id=sid)
            raw_obj = {**ev, "origin": origin_id}
            raw_bytes = json.dumps(raw_obj, sort_keys=True, default=str).encode("utf-8")
            raw_text, raw_blob, raw_sha = intake_store.prepare_raw(raw_bytes)
            intake_store.record_raw_blob(conn, raw_blob, len(raw_bytes))
            trust = str(ev.get("trust") or "untrusted")
            _, created = intake_store.insert_item(
                conn,
                source=sid,
                source_version=src.version if src is not None else None,
                external_key=ext_key,
                external_rev=ext_rev,
                kind=kind,
                title=str(ev.get("title") or ""),
                origin_url=str(ev.get("url") or ""),
                trust=trust,
                raw=raw_text,
                raw_blob=raw_blob,
                raw_sha256=raw_sha,
                status="held",
                shape="event",
                received=received,
            )
            if created:
                inserted += 1
            continue

        ext_key, ext_rev, kind, shape = sources.legacy_seen_identity(src, cfg, sid, raw_key)
        _, created = intake_store.insert_item(
            conn,
            source=sid,
            source_version=src.version if src is not None else None,
            external_key=ext_key,
            external_rev=ext_rev,
            kind=kind,
            trust="untrusted",
            quarantined=1,
            raw="{}",
            raw_blob=None,
            raw_sha256=_EMPTY_RAW_SHA,
            status="dismissed",
            shape=shape,
            received=received,
        )
        if created:
            inserted += 1
    return inserted


def apply(conn) -> None:
    backfill_seen_rows(conn)
