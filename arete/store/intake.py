from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext

from .. import util
from ._rows import json_dict
from . import blobs as blob_store
from .db import INTAKE_EXPAND_SCHEMA, migration_applied, reader

TABLES = {
    "intake_items": "kept",
    "intake_candidates": "kept",
}

INLINE_RAW_LIMIT = 256 * 1024
ID_RE = re.compile(r"^IN-(\d+)$", re.IGNORECASE)
STATUSES = frozenset(("new", "held", "extracted", "linked", "dismissed", "refused"))
TRUST_LEVELS = frozenset(("untrusted", "internal", "owner"))
CANDIDATE_STATES = frozenset(("proposed", "accepted", "linked", "dismissed"))
CANDIDATE_ACTIONS = frozenset(("create", "link", "ignore"))

_DDL = """
CREATE TABLE IF NOT EXISTS intake_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_version TEXT,
    poll_id INTEGER,
    external_key TEXT NOT NULL,
    external_rev TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    origin_url TEXT NOT NULL DEFAULT '',
    trust TEXT NOT NULL DEFAULT 'untrusted' CHECK (trust IN ('untrusted', 'internal', 'owner')),
    quarantined INTEGER NOT NULL DEFAULT 1 CHECK (quarantined IN (0, 1)),
    raw TEXT,
    raw_blob TEXT,
    raw_sha256 TEXT NOT NULL,
    rule TEXT,
    rule_version TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (
        status IN ('new', 'held', 'extracted', 'linked', 'dismissed', 'refused')
    ),
    refuse_reason TEXT,
    shape TEXT NOT NULL DEFAULT '',
    received TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source, external_key, external_rev),
    CHECK (trust != 'untrusted' OR quarantined = 1),
    CHECK ((raw IS NOT NULL AND raw_blob IS NULL) OR (raw IS NULL AND raw_blob IS NOT NULL))
) STRICT;

CREATE INDEX IF NOT EXISTS intake_items_source_status ON intake_items (source, status, id);
CREATE INDEX IF NOT EXISTS intake_items_source_key ON intake_items (source, external_key, received DESC);
CREATE INDEX IF NOT EXISTS intake_items_poll ON intake_items (poll_id);

CREATE TABLE IF NOT EXISTS intake_candidates (
    item INTEGER NOT NULL,
    n INTEGER NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'link', 'ignore')),
    title TEXT NOT NULL,
    quote_start INTEGER,
    quote_end INTEGER,
    fields TEXT NOT NULL DEFAULT '{}',
    match_task TEXT,
    match_confidence REAL,
    state TEXT NOT NULL DEFAULT 'proposed' CHECK (
        state IN ('proposed', 'accepted', 'linked', 'dismissed')
    ),
    task_id TEXT,
    extracted_by INTEGER,
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (item, n)
) STRICT;

CREATE INDEX IF NOT EXISTS intake_candidates_task ON intake_candidates (task_id, item, n);
"""


def format_id(item_id: int) -> str:
    return f"IN-{int(item_id)}"


def parse_id(value: str | int | None) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if match := ID_RE.fullmatch(text):
        num = int(match.group(1))
        return num if num > 0 else None
    if text.isascii() and text.isdecimal():
        num = int(text)
        return num if num > 0 else None
    return None


def available(c) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'intake_items'"
    ).fetchone() is not None


def has_expand(c) -> bool:
    return migration_applied(INTAKE_EXPAND_SCHEMA, c) or available(c)


def apply_expand(conn) -> None:
    for stmt in _DDL.split(";"):
        sql = stmt.strip()
        if sql:
            conn.execute(sql)
    ref_cols = {r["name"] for r in conn.execute("PRAGMA table_info(refs)")}
    if ref_cols and "intake_item" not in ref_cols:
        conn.execute("ALTER TABLE refs ADD COLUMN intake_item TEXT")
    if ref_cols:
        conn.execute("CREATE INDEX IF NOT EXISTS refs_intake_item ON refs (intake_item)")
    tr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_refs)")}
    if tr_cols:
        if "intake_item" not in tr_cols:
            conn.execute("ALTER TABLE task_refs ADD COLUMN intake_item TEXT")
        if "intake_n" not in tr_cols:
            conn.execute("ALTER TABLE task_refs ADD COLUMN intake_n INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS task_refs_intake ON task_refs (intake_item, intake_n)")


def prepare_raw(raw_bytes: bytes) -> tuple[str | None, str | None, str]:
    """Return (raw_text, raw_blob_sha, raw_sha256). Writes blob file before SQLite tx when > 256 KB."""
    sha = hashlib.sha256(raw_bytes).hexdigest()
    if len(raw_bytes) > INLINE_RAW_LIMIT:
        with blob_store.lock():
            blob_store.write_file(raw_bytes, sha)
        return None, sha, sha
    return raw_bytes.decode("utf-8", "replace"), None, sha


def record_raw_blob(conn, raw_blob: str | None, size: int) -> None:
    if raw_blob is not None:
        blob_store.record_row(conn, raw_blob, "intake", size)


def raw_text(item: dict) -> str:
    if item.get("raw") is not None:
        return str(item["raw"])
    sha = item.get("raw_blob")
    if isinstance(sha, str) and sha:
        data = blob_store.read(sha)
        if data is not None:
            return data.decode("utf-8", "replace")
    return ""


def decode_item(row) -> dict:
    d = dict(row)
    num = int(d["id"])
    return {
        "id": format_id(num),
        "num": num,
        "source": str(d["source"]),
        "source_version": d.get("source_version"),
        "poll_id": int(d["poll_id"]) if d.get("poll_id") is not None else None,
        "external_key": str(d["external_key"]),
        "external_rev": str(d.get("external_rev") or ""),
        "kind": str(d["kind"]),
        "title": str(d.get("title") or ""),
        "author": str(d.get("author") or ""),
        "origin_url": str(d.get("origin_url") or ""),
        "trust": str(d.get("trust") or "untrusted"),
        "quarantined": bool(d.get("quarantined", 1)),
        "raw": d.get("raw"),
        "raw_blob": d.get("raw_blob"),
        "raw_sha256": str(d["raw_sha256"]),
        "rule": d.get("rule"),
        "rule_version": d.get("rule_version"),
        "status": str(d.get("status") or "new"),
        "refuse_reason": d.get("refuse_reason"),
        "shape": str(d.get("shape") or ""),
        "received": str(d["received"]),
        "version": int(d.get("version") or 1),
    }


def public_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "num"}


def decode_candidate(row) -> dict:
    d = dict(row)
    item_num = int(d["item"])
    return {
        "item": format_id(item_num),
        "n": int(d["n"]),
        "kind": str(d["kind"]),
        "action": str(d["action"]),
        "title": str(d["title"]),
        "quote_start": int(d["quote_start"]) if d.get("quote_start") is not None else None,
        "quote_end": int(d["quote_end"]) if d.get("quote_end") is not None else None,
        "fields": json_dict(d.get("fields")),
        "match_task": d.get("match_task"),
        "match_confidence": float(d["match_confidence"]) if d.get("match_confidence") is not None else None,
        "state": str(d["state"]),
        "task_id": d.get("task_id"),
        "extracted_by": int(d["extracted_by"]) if d.get("extracted_by") is not None else None,
        "version": int(d.get("version") or 1),
    }


def insert_item(
    conn,
    *,
    source: str,
    external_key: str,
    external_rev: str = "",
    kind: str,
    title: str = "",
    author: str = "",
    origin_url: str = "",
    trust: str = "untrusted",
    quarantined: int | bool | None = None,
    raw: str | None = None,
    raw_blob: str | None = None,
    raw_sha256: str,
    source_version: str | None = None,
    poll_id: int | None = None,
    rule: str | None = None,
    rule_version: str | None = None,
    status: str = "new",
    refuse_reason: str | None = None,
    shape: str = "",
    received: str | None = None,
) -> tuple[int | None, bool]:
    trust_val = trust if trust in TRUST_LEVELS else "untrusted"
    if quarantined is None:
        quar_val = 1 if trust_val == "untrusted" else 0
    else:
        quar_val = 1 if (trust_val == "untrusted" or bool(quarantined)) else 0
    ts = received or util.iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO intake_items ("
        "source, source_version, poll_id, external_key, external_rev, kind, title, author, origin_url, "
        "trust, quarantined, raw, raw_blob, raw_sha256, rule, rule_version, status, refuse_reason, "
        "shape, received, version"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            source,
            source_version,
            poll_id,
            external_key,
            external_rev,
            kind,
            title,
            author,
            origin_url,
            trust_val,
            quar_val,
            raw,
            raw_blob,
            raw_sha256,
            rule,
            rule_version,
            status,
            refuse_reason,
            shape,
            ts,
        ),
    )
    if cur.rowcount == 1:
        return int(cur.lastrowid), True
    existing = conn.execute(
        "SELECT id FROM intake_items WHERE source = ? AND external_key = ? AND external_rev = ?",
        (source, external_key, external_rev),
    ).fetchone()
    return (int(existing["id"]) if existing is not None else None), False


def get_item(item_id: str | int, *, connect=reader) -> dict | None:
    num = parse_id(item_id)
    if num is None:
        return None
    with connect() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM intake_items WHERE id = ?", (num,)).fetchone()
        return decode_item(row) if row is not None else None


def get_item_conn(c, item_id: str | int) -> dict | None:
    return get_item(item_id, connect=lambda: nullcontext(c))


def list_items(
    *,
    source: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    connect=reader,
) -> list[dict]:
    with connect() as c:
        if not available(c):
            return []
        clauses: list[str] = []
        args: list[object] = []
        if source:
            clauses.append("source = ?")
            args.append(source)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(max(1, min(int(limit), 1000)))
        rows = c.execute(
            f"SELECT * FROM intake_items{where} ORDER BY id DESC LIMIT ?",  # noqa: S608
            args,
        ).fetchall()
        return [decode_item(r) for r in rows]


def candidates_for(item_id: str | int, *, connect=reader) -> list[dict]:
    num = parse_id(item_id)
    if num is None:
        return []
    with connect() as c:
        if not available(c):
            return []
        rows = c.execute(
            "SELECT * FROM intake_candidates WHERE item = ? ORDER BY n",
            (num,),
        ).fetchall()
        return [decode_candidate(r) for r in rows]


def candidates_for_conn(c, item_id: str | int) -> list[dict]:
    return candidates_for(item_id, connect=lambda: nullcontext(c))


def item_identities(source: str, *, connect=reader) -> set[tuple[str, str]]:
    with connect() as c:
        if not available(c):
            return set()
        return {
            (str(r["external_key"]), str(r["external_rev"] or ""))
            for r in c.execute(
                "SELECT external_key, external_rev FROM intake_items WHERE source = ?",
                (source,),
            )
        }


def seen_driver_keys(source: str, *, connect=reader) -> set[str]:
    """Return recorded driver keys for a source from intake_items."""
    keys: set[str] = set()
    with connect() as c:
        if not available(c):
            return keys
        for row in c.execute(
            "SELECT external_key, external_rev, raw FROM intake_items WHERE source = ?",
            (source,),
        ):
            ext_key = str(row["external_key"])
            ext_rev = str(row["external_rev"] or "")
            keys.add(ext_key)
            if ext_key.startswith("["):
                try:
                    parsed = json.loads(ext_key)
                except ValueError:
                    parsed = None
                if isinstance(parsed, list) and len(parsed) == 3 and isinstance(parsed[2], str):
                    driver_key = parsed[2]
                    keys.add(driver_key)
                    if ext_rev:
                        keys.add(f"{driver_key}@{ext_rev}")
            raw_val = row["raw"]
            if isinstance(raw_val, str) and raw_val.startswith("{"):
                try:
                    payload = json.loads(raw_val)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and payload.get("key") is not None:
                    keys.add(str(payload["key"]))
    return keys


def newest_for_key(c, source: str, external_key: str) -> dict | None:
    row = c.execute(
        "SELECT * FROM intake_items WHERE source = ? AND external_key = ? "
        "ORDER BY received DESC, id DESC LIMIT 1",
        (source, external_key),
    ).fetchone()
    return decode_item(row) if row is not None else None


def task_intake_ids(c, task_id_str: str, refs_rows: list[dict] | None = None) -> list[str]:
    """Return deduplicated IN-<id> strings linked to a task."""
    found: list[str] = []
    recorded: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text not in recorded:
                recorded.add(text)
                found.append(text)

    if refs_rows is not None:
        for row in refs_rows:
            add(row.get("intake_item"))
            ref_detail = row.get("ref_detail")
            if isinstance(ref_detail, dict):
                add(ref_detail.get("intake_item"))
    if available(c):
        for row in c.execute(
            "SELECT DISTINCT item FROM intake_candidates WHERE task_id = ? AND state = 'linked' ORDER BY item",
            (task_id_str,),
        ):
            add(format_id(int(row["item"])))
    return found


def prune_unused_source(c, sid: str) -> int:
    if not available(c):
        return 0
    return c.execute(
        "DELETE FROM intake_items WHERE source = ? "
        "AND NOT EXISTS (SELECT 1 FROM intake_candidates ic WHERE ic.item = intake_items.id) "
        "AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.intake_item = 'IN-' || intake_items.id)",
        (sid,),
    ).rowcount
