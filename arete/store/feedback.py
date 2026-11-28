"""Durable cursor, deduplication, clarification, and routing state for ticket-thread feedback."""
from __future__ import annotations

import json
import sqlite3

from .. import paths, util
from .db import NoDatabase, reader, tx

TABLES = {
    "ticket_comments": "kept",
    "ticket_clarifications": "kept",
}

COMMENT_COLUMNS = (
    "repo", "ticket", "tracker", "comment_id", "author", "author_display", "relayed_by",
    "body", "url", "created", "updated", "ticket_state", "authorized", "auth_reason",
    "intent", "route", "status", "action_id", "parent_ticket", "run_id", "data", "recorded",
)

UPDATABLE_COMMENT_FIELDS = frozenset({
    "ticket_state", "authorized", "auth_reason", "intent", "route", "status",
    "action_id", "parent_ticket", "run_id", "data",
})


def installed(c=None) -> bool:
    if c is not None:
        return bool(c.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'ticket_comments' AND type = 'table'"
        ).fetchone())
    if not paths.DB_FILE.exists():
        return False
    try:
        with reader() as conn:
            return installed(conn)
    except (NoDatabase, sqlite3.Error, OSError):
        return False


def _decode(row) -> dict:
    item = dict(row)
    if "authorized" in item:
        item["authorized"] = bool(item["authorized"])
    if "data" in item:
        try:
            item["data"] = json.loads(item["data"] or "{}")
        except ValueError:
            item["data"] = {}
    return item


def get_comment(repo: str, ticket: str, tracker: str, comment_id: str) -> dict | None:
    if not installed():
        return None
    with reader() as c:
        if not installed(c):
            return None
        row = c.execute(
            "SELECT * FROM ticket_comments WHERE repo = ? AND ticket = ? AND tracker = ? AND comment_id = ?",
            (repo, str(ticket), tracker, str(comment_id)),
        ).fetchone()
        return _decode(row) if row else None


def record_comment(row: dict) -> bool:
    """Atomically record a ticket comment once. Returns True if newly inserted, False on replay."""
    payload = dict(row)
    payload["ticket"] = str(payload["ticket"])
    payload["comment_id"] = str(payload["comment_id"])
    payload["authorized"] = int(bool(payload.get("authorized", False)))
    payload["data"] = json.dumps(payload.get("data") or {}, sort_keys=True)
    payload.setdefault("recorded", util.iso())
    values = [payload.get(col, "") for col in COMMENT_COLUMNS]
    with tx() as c:
        if not installed(c):
            return False
        before = c.total_changes
        c.execute(
            f"INSERT OR IGNORE INTO ticket_comments ({', '.join(COMMENT_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(COMMENT_COLUMNS))})",
            values,
        )
        return c.total_changes > before


def update_comment(repo: str, ticket: str, tracker: str, comment_id: str, **fields) -> bool:
    bad = set(fields) - UPDATABLE_COMMENT_FIELDS
    if bad:
        raise ValueError(f"unsupported ticket_comments field(s): {', '.join(sorted(bad))}")
    if not fields:
        return False
    sets, args = [], []
    for key, value in fields.items():
        sets.append(f"{key} = ?")
        if key == "authorized":
            args.append(int(bool(value)))
        elif key == "data":
            args.append(json.dumps(value or {}, sort_keys=True))
        else:
            args.append(value)
    args.extend([repo, str(ticket), tracker, str(comment_id)])
    with tx() as c:
        if not installed(c):
            return False
        return bool(c.execute(
            f"UPDATE ticket_comments SET {', '.join(sets)} "
            "WHERE repo = ? AND ticket = ? AND tracker = ? AND comment_id = ?",
            args,
        ).rowcount)


def comments_for_ticket(repo: str, ticket: str, *, status: str = "", intent: str = "",
                        route: str = "", authorized: bool | None = None) -> list[dict]:
    if not installed():
        return []
    q = "SELECT * FROM ticket_comments WHERE repo = ? AND ticket = ?"
    args: list = [repo, str(ticket)]
    if status:
        q += " AND status = ?"
        args.append(status)
    if intent:
        q += " AND intent = ?"
        args.append(intent)
    if route:
        q += " AND route = ?"
        args.append(route)
    if authorized is not None:
        q += " AND authorized = ?"
        args.append(int(bool(authorized)))
    q += " ORDER BY created, rowid"
    with reader() as c:
        if not installed(c):
            return []
        return [_decode(r) for r in c.execute(q, args)]


def tickets_needing_worker(repo: str) -> list[str]:
    """Open tickets with queued authorized corrections and no open product clarification."""
    if not installed():
        return []
    with reader() as c:
        if not installed(c):
            return []
        rows = c.execute(
            "SELECT ticket FROM ticket_comments tc "
            "WHERE tc.repo = ? AND tc.authorized = 1 AND tc.route = 'worker' "
            "AND tc.status = 'queued' AND tc.ticket_state = 'open' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM ticket_clarifications cl "
            "  WHERE cl.repo = tc.repo AND cl.ticket = tc.ticket AND cl.status IN ('asking', 'open')"
            ") "
            "GROUP BY tc.ticket ORDER BY MIN(tc.created), MIN(tc.rowid)",
            (repo,),
        ).fetchall()
        return [str(r["ticket"]) for r in rows]


def pending_worker_comments(repo: str, ticket: str) -> list[dict]:
    if not installed():
        return []
    with reader() as c:
        if not installed(c):
            return []
        rows = c.execute(
            "SELECT * FROM ticket_comments WHERE repo = ? AND ticket = ? "
            "AND authorized = 1 AND route = 'worker' AND status IN ('queued', 'steered') "
            "ORDER BY created, rowid",
            (repo, str(ticket)),
        ).fetchall()
        return [_decode(r) for r in rows]


def claim_worker_comments(repo: str, ticket: str, run_id: str) -> list[dict]:
    """Atomically claim queued/steered worker comments for a single-ticket run."""
    if not installed():
        return []
    with tx() as c:
        if not installed(c):
            return []
        rows = c.execute(
            "SELECT * FROM ticket_comments WHERE repo = ? AND ticket = ? "
            "AND authorized = 1 AND route = 'worker' "
            "AND (status IN ('queued', 'steered') OR (status = 'taken' AND run_id = ?)) "
            "ORDER BY created, rowid",
            (repo, str(ticket), run_id),
        ).fetchall()
        if not rows:
            return []
        for row in rows:
            c.execute(
                "UPDATE ticket_comments SET status = 'taken', run_id = ? "
                "WHERE repo = ? AND ticket = ? AND tracker = ? AND comment_id = ? "
                "AND (status IN ('queued', 'steered') OR (status = 'taken' AND run_id = ?))",
                (run_id, row["repo"], row["ticket"], row["tracker"], row["comment_id"], run_id),
            )
        claimed = c.execute(
            "SELECT * FROM ticket_comments WHERE repo = ? AND ticket = ? "
            "AND route = 'worker' AND status = 'taken' AND run_id = ? ORDER BY created, rowid",
            (repo, str(ticket), run_id),
        ).fetchall()
        return [_decode(r) for r in claimed]


def settle_worker_comments(run_id: str, status: str = "applied") -> int:
    if not installed():
        return 0
    with tx() as c:
        if not installed(c):
            return 0
        return c.execute(
            "UPDATE ticket_comments SET status = ? WHERE run_id = ? AND route = 'worker' AND status IN ('taken', 'steered')",
            (status, run_id),
        ).rowcount


def release_worker_comments(run_id: str) -> int:
    """Return unapplied worker comments back to queued if a run fails or is interrupted."""
    if not installed():
        return 0
    with tx() as c:
        if not installed(c):
            return 0
        return c.execute(
            "UPDATE ticket_comments SET status = 'queued', run_id = '' "
            "WHERE run_id = ? AND route = 'worker' AND status = 'taken'",
            (run_id,),
        ).rowcount


def pending_corrections(repo: str, ticket: str) -> list[dict]:
    """Authorized open-ticket corrections or unresolved product clarifications blocking promotion."""
    if not installed():
        return []
    with reader() as c:
        if not installed(c):
            return []
        rows = c.execute(
            "SELECT * FROM ticket_comments WHERE repo = ? AND ticket = ? AND authorized = 1 "
            "AND ticket_state = 'open' AND ("
            "  (route = 'worker' AND status IN ('queued', 'steered', 'taken')) "
            "  OR (route = 'clarify' AND status IN ('asking', 'waiting_answer'))"
            ") ORDER BY created, rowid",
            (repo, str(ticket)),
        ).fetchall()
        return [_decode(r) for r in rows]


def production_instructions(repo: str, ticket: str) -> list[dict]:
    """Authorized explicit production instructions recorded for the release workflow."""
    if not installed():
        return []
    with reader() as c:
        if not installed(c):
            return []
        rows = c.execute(
            "SELECT * FROM ticket_comments WHERE repo = ? AND ticket = ? AND authorized = 1 "
            "AND intent = 'production_instruction' ORDER BY created, rowid",
            (repo, str(ticket)),
        ).fetchall()
        return [_decode(r) for r in rows]


def pending_follow_ups(repo: str = "") -> list[dict]:
    """Authorized actionable requests on closed/completed tickets awaiting follow-up creation."""
    if not installed():
        return []
    q = ("SELECT * FROM ticket_comments WHERE authorized = 1 AND route = 'follow_up' "
         "AND status = 'follow_up_pending'")
    args: list = []
    if repo:
        q += " AND repo = ?"
        args.append(repo)
    q += " ORDER BY created, rowid"
    with reader() as c:
        if not installed(c):
            return []
        return [_decode(r) for r in c.execute(q, args)]


def mark_follow_up(repo: str, ticket: str, tracker: str, comment_id: str,
                   status: str, *, data: dict | None = None) -> bool:
    fields: dict = {"status": status}
    if data is not None:
        fields["data"] = data
    return update_comment(repo, ticket, tracker, comment_id, **fields)


def get_clarification(cid: str) -> dict | None:
    if not installed():
        return None
    with reader() as c:
        if not installed(c):
            return None
        row = c.execute("SELECT * FROM ticket_clarifications WHERE id = ?", (cid,)).fetchone()
        return _decode(row) if row else None


def open_clarifications(repo: str, ticket: str) -> list[dict]:
    if not installed():
        return []
    with reader() as c:
        if not installed(c):
            return []
        rows = c.execute(
            "SELECT * FROM ticket_clarifications WHERE repo = ? AND ticket = ? "
            "AND status IN ('asking', 'open') ORDER BY asked_at, rowid",
            (repo, str(ticket)),
        ).fetchall()
        return [_decode(r) for r in rows]


def has_open_clarification(repo: str, ticket: str) -> bool:
    if not installed():
        return False
    with reader() as c:
        if not installed(c):
            return False
        return bool(c.execute(
            "SELECT 1 FROM ticket_clarifications WHERE repo = ? AND ticket = ? "
            "AND status IN ('asking', 'open') LIMIT 1",
            (repo, str(ticket)),
        ).fetchone())


def begin_clarification(cid: str, repo: str, ticket: str, tracker: str, source_comment_id: str,
                        question: str, marker: str, *, run_id: str = "", data: dict | None = None) -> dict:
    """Write-ahead durable record for posting a focused clarification question once."""
    with tx() as c:
        row = c.execute("SELECT * FROM ticket_clarifications WHERE id = ?", (cid,)).fetchone()
        if row:
            return _decode(row)
        now = util.iso()
        c.execute(
            "INSERT INTO ticket_clarifications "
            "(id, repo, ticket, tracker, source_comment_id, question, marker, status, asked_at, run_id, data) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, repo, str(ticket), tracker, str(source_comment_id), question, marker, "asking",
             now, run_id, json.dumps(data or {}, sort_keys=True)),
        )
        row = c.execute("SELECT * FROM ticket_clarifications WHERE id = ?", (cid,)).fetchone()
        return _decode(row)


def mark_clarification_open(cid: str, comment_url: str) -> bool:
    with tx() as c:
        if not installed(c):
            return False
        return bool(c.execute(
            "UPDATE ticket_clarifications SET status = 'open', comment_url = ? "
            "WHERE id = ? AND status IN ('asking', 'open')",
            (comment_url, cid),
        ).rowcount)


def abort_clarification(cid: str) -> bool:
    with tx() as c:
        if not installed(c):
            return False
        return bool(c.execute(
            "DELETE FROM ticket_clarifications WHERE id = ? AND status = 'asking' AND comment_url = ''",
            (cid,),
        ).rowcount)


def resolve_clarification(cid: str, answer_comment_id: str, answer_author: str, answer_body: str) -> bool:
    with tx() as c:
        if not installed(c):
            return False
        return bool(c.execute(
            "UPDATE ticket_clarifications SET status = 'answered', answer_comment_id = ?, "
            "answer_author = ?, answer_body = ?, answered_at = ? "
            "WHERE id = ? AND status IN ('asking', 'open')",
            (str(answer_comment_id), answer_author, answer_body, util.iso(), cid),
        ).rowcount)
