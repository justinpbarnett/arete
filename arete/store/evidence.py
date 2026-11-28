"""Durable storage for private evidence requests and private Triage uploads."""
from __future__ import annotations

import json
import sqlite3

from .. import paths, util
from ._rows import json_dict
from .db import IncompatibleSchema, NoDatabase, reader, tx

TABLES = {
    "private_evidence": "kept",
}

STATUSES = frozenset(("open", "uploaded", "access_restored", "stopped"))


def available(c=None) -> bool:
    if c is not None:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'private_evidence'"
        ).fetchone() is not None
    if not paths.DB_FILE.exists():
        return False
    try:
        with reader() as conn:
            return available(conn)
    except (NoDatabase, IncompatibleSchema, sqlite3.Error, OSError):
        return False


def _decode(row) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["resume_pending"] = bool(item.get("resume_pending"))
    item["data"] = json_dict(item.get("data"))
    return item


def _sync_inbox(c) -> None:
    from .. import inbox

    inbox.sync(c, "evidence")
    inbox.sync(c, "runs")


def upsert_missing(
    ev_id: str,
    *,
    repo: str,
    ticket: str,
    ticket_ref: str,
    tracker: str,
    run_id: str,
    ref_key: str,
    ref_kind: str,
    source: str,
    name: str,
    original_url: str = "",
    attachment_id: str = "",
    media_kind: str = "",
    failure_status: str = "inaccessible",
    failure_detail: str = "",
    data: dict | None = None,
) -> tuple[dict | None, bool]:
    """Create or update a private evidence request without duplicating on retries.

    Returns (row, created). If an existing record is already satisfied ('uploaded'),
    it is returned unchanged unless reopened explicitly.
    """
    if not paths.DB_FILE.exists():
        return None, False
    now = util.iso()
    encoded_data = json.dumps(data or {}, sort_keys=True, separators=(",", ":"), default=str)
    with tx() as c:
        if not available(c):
            return None, False
        existing = c.execute(
            "SELECT * FROM private_evidence WHERE repo = ? AND ticket = ? AND ref_key = ?",
            (repo, str(ticket), ref_key),
        ).fetchone()
        if existing is not None:
            if existing["status"] == "uploaded" and existing["private_path"]:
                return _decode(existing), False
            # Keep original opened timestamp when already open; reset opened if reopening a prior stopped/restored row.
            opened = existing["opened"] if existing["status"] == "open" else now
            effective_run = run_id or existing["run_id"]
            c.execute(
                "UPDATE private_evidence SET ticket_ref = ?, tracker = ?, run_id = ?, ref_kind = ?, source = ?, "
                "name = ?, original_url = ?, attachment_id = ?, media_kind = ?, failure_status = ?, "
                "failure_detail = ?, status = 'open', resume_pending = 0, opened = ?, updated = ?, data = ? "
                "WHERE id = ?",
                (
                    ticket_ref,
                    tracker,
                    effective_run,
                    ref_kind,
                    source,
                    name,
                    original_url,
                    attachment_id,
                    media_kind,
                    failure_status,
                    failure_detail,
                    opened,
                    now,
                    encoded_data,
                    existing["id"],
                ),
            )
            after = c.execute("SELECT * FROM private_evidence WHERE id = ?", (existing["id"],)).fetchone()
            _sync_inbox(c)
            return _decode(after), existing["status"] != "open"

        c.execute(
            "INSERT INTO private_evidence "
            "(id, repo, ticket, ticket_ref, tracker, run_id, ref_key, ref_kind, source, name, original_url, "
            "attachment_id, media_kind, failure_status, failure_detail, status, opened, updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (
                ev_id,
                repo,
                str(ticket),
                ticket_ref,
                tracker,
                run_id,
                ref_key,
                ref_kind,
                source,
                name,
                original_url,
                attachment_id,
                media_kind,
                failure_status,
                failure_detail,
                now,
                now,
                encoded_data,
            ),
        )
        row = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        _sync_inbox(c)
        return _decode(row), True


def get(ev_id: str, connect=reader) -> dict | None:
    if not paths.DB_FILE.exists():
        return None
    with connect() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        return _decode(row)


def get_by_ref(repo: str, ticket: str, ref_key: str, connect=reader) -> dict | None:
    if not paths.DB_FILE.exists():
        return None
    with connect() as c:
        if not available(c):
            return None
        row = c.execute(
            "SELECT * FROM private_evidence WHERE repo = ? AND ticket = ? AND ref_key = ?",
            (repo, str(ticket), ref_key),
        ).fetchone()
        return _decode(row)


def list_for_ticket(repo: str, ticket: str, *, status: str = "", connect=reader) -> list[dict]:
    if not paths.DB_FILE.exists():
        return []
    with connect() as c:
        if not available(c):
            return []
        if status:
            rows = c.execute(
                "SELECT * FROM private_evidence WHERE repo = ? AND ticket = ? AND status = ? ORDER BY opened, id",
                (repo, str(ticket), status),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM private_evidence WHERE repo = ? AND ticket = ? ORDER BY opened, id",
                (repo, str(ticket)),
            ).fetchall()
        return [item for r in rows if (item := _decode(r)) is not None]


def list_open(connect=reader, *, repo: str = "") -> list[dict]:
    if not paths.DB_FILE.exists():
        return []
    with connect() as c:
        if not available(c):
            return []
        if repo:
            rows = c.execute(
                "SELECT * FROM private_evidence WHERE status = 'open' AND repo = ? ORDER BY opened, id",
                (repo,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM private_evidence WHERE status = 'open' ORDER BY opened, id"
            ).fetchall()
        return [item for r in rows if (item := _decode(r)) is not None]


def has_open(repo: str, ticket: str, connect=reader) -> bool:
    if not paths.DB_FILE.exists():
        return False
    try:
        with connect() as c:
            if not available(c):
                return False
            return c.execute(
                "SELECT 1 FROM private_evidence WHERE repo = ? AND ticket = ? AND status = 'open' LIMIT 1",
                (repo, str(ticket)),
            ).fetchone() is not None
    except (NoDatabase, IncompatibleSchema, sqlite3.Error, OSError):
        return False


def tickets_ready_to_resume(repo: str, connect=reader) -> list[str]:
    """Return tickets whose private evidence was resolved and still need automatic resumption."""
    if not paths.DB_FILE.exists():
        return []
    try:
        with connect() as c:
            if not available(c):
                return []
            rows = c.execute(
                "SELECT DISTINCT e.ticket FROM private_evidence e "
                "WHERE e.repo = ? AND e.resume_pending = 1 AND e.status IN ('uploaded', 'access_restored') "
                "AND NOT EXISTS (SELECT 1 FROM private_evidence other "
                "  WHERE other.repo = e.repo AND other.ticket = e.ticket AND other.status = 'open') "
                "ORDER BY e.resolved_at, e.id",
                (repo,),
            ).fetchall()
            return [str(r["ticket"]) for r in rows]
    except (NoDatabase, IncompatibleSchema, sqlite3.Error, OSError):
        return []


def record_upload(
    ev_id: str,
    *,
    private_path: str,
    sha256: str,
    mime_type: str,
    filename: str,
    size: int,
    actor: str = "",
    data: dict | None = None,
) -> dict | None:
    now = util.iso()
    with tx() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        if row is None or row["status"] != "open":
            return None
        merged = {**json_dict(row["data"]), **(data or {})}
        c.execute(
            "UPDATE private_evidence SET status = 'uploaded', private_path = ?, private_sha256 = ?, "
            "private_mime = ?, private_name = ?, private_size = ?, last_error = '', resume_pending = 1, "
            "updated = ?, resolved_at = ?, resolved_by = ?, data = ? WHERE id = ? AND status = 'open'",
            (
                private_path,
                sha256,
                mime_type,
                filename,
                int(size),
                now,
                now,
                actor,
                json.dumps(merged, sort_keys=True, separators=(",", ":"), default=str),
                ev_id,
            ),
        )
        after = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        _sync_inbox(c)
        return _decode(after)


def record_access_restored(ev_id: str, *, actor: str = "", resume_pending: bool = True) -> dict | None:
    now = util.iso()
    with tx() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        if row is None or row["status"] != "open":
            return None
        c.execute(
            "UPDATE private_evidence SET status = 'access_restored', last_error = '', resume_pending = ?, "
            "updated = ?, resolved_at = ?, resolved_by = ? WHERE id = ? AND status = 'open'",
            (1 if resume_pending else 0, now, now, actor, ev_id),
        )
        after = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        _sync_inbox(c)
        return _decode(after)


def record_failure(ev_id: str, error_text: str) -> dict | None:
    now = util.iso()
    with tx() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        if row is None or row["status"] != "open":
            return _decode(row)
        c.execute(
            "UPDATE private_evidence SET last_error = ?, updated = ? WHERE id = ? AND status = 'open'",
            (error_text, now, ev_id),
        )
        after = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        _sync_inbox(c)
        return _decode(after)


def dismiss(ev_id: str, *, actor: str = "") -> dict | None:
    now = util.iso()
    with tx() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        if row is None or row["status"] != "open":
            return None
        c.execute(
            "UPDATE private_evidence SET status = 'stopped', resume_pending = 0, updated = ?, "
            "resolved_at = ?, resolved_by = ? WHERE id = ? AND status = 'open'",
            (now, now, actor, ev_id),
        )
        after = c.execute("SELECT * FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        _sync_inbox(c)
        return _decode(after)


def mark_resumed(repo: str, ticket: str, run_id: str = "") -> int:
    if not paths.DB_FILE.exists():
        return 0
    now = util.iso()
    with tx() as c:
        if not available(c):
            return 0
        return c.execute(
            "UPDATE private_evidence SET resume_pending = 0, resumed_run_id = CASE WHEN ? != '' THEN ? ELSE resumed_run_id END, "
            "updated = ? WHERE repo = ? AND ticket = ? AND resume_pending = 1",
            (run_id, run_id, now, repo, str(ticket)),
        ).rowcount
