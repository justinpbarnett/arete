from __future__ import annotations

import json
import secrets

from .. import util
from . import insights
from ._rows import json_dict
from .db import db, reader

TABLES = {
    "approvals": "kept",
    "mined": "kept",
}


DECIDABLE = ("pending", "failed")


APPROVAL_NOTES = ("plain_summary", "plain_summary_tried", "review", "review_tried")


def _sync_inbox(c, source: str) -> None:
    from .. import inbox

    inbox.sync(c, source)


def is_view(c) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'approvals'").fetchone() is not None


def add_approval(loop: str, run_id: str, emit: dict, summary: str, failed: str = "") -> int:
    with db() as c:
        cur = c.execute("INSERT INTO approvals (loop, run_id, emit, summary, status, result, created) "
                        "VALUES (?,?,?,?,?,?,?)", (loop, run_id, json.dumps(emit, default=str), summary[:2000],
                                                   "failed" if failed else "pending", failed[:1000], util.iso()))
        if is_view(c):
            row = c.execute(
                "SELECT legacy_approval FROM outbox WHERE legacy_approval IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            approval_id = int(row["legacy_approval"])
        else:
            approval_id = int(cur.lastrowid)
        _sync_inbox(c, "approvals")
        _sync_inbox(c, "outbox")
        return approval_id


def approvals(connect=reader) -> list[dict]:
    with connect() as c:
        return [approval_of(r) for r in c.execute("SELECT a.*, COALESCE(r.repo, '') AS run_repo FROM approvals a "  # noqa: S608
                                                 "LEFT JOIN runs r ON r.id = a.run_id "
                                                 f"WHERE a.status IN ({','.join('?' * len(DECIDABLE))}) "
                                                 "ORDER BY a.id DESC LIMIT 100", DECIDABLE)]


def set_approval_note(approval_id: int, note: str, value: str, while_in: tuple[str, ...] = DECIDABLE) -> bool:
    if note not in APPROVAL_NOTES:
        raise ValueError(f"an approval has no note {note}; it has {', '.join(APPROVAL_NOTES)}")
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET {note} = ? WHERE id = ? AND status IN ({','.join('?' * len(while_in))})",  # noqa: S608
                  (value, approval_id, *while_in))
        changed = c.total_changes > before
        if changed:
            _sync_inbox(c, "approvals")
        return changed


def approval(approval_id: int) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return approval_of(r) if r else None


def approval_of(row) -> dict:
    return {**dict(row), "emit": json_dict(row["emit"]), "review": json_dict(row["review"])}


def claim_approval(approval_id: int, until: str, came_from: tuple[str, ...] = DECIDABLE, *, previous="") -> str:
    token = secrets.token_hex(8)
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET status = 'approved', result = '', decided = ?, claimed_by = ?, "  # noqa: S608
                  f"claimed_until = ? WHERE id = ? AND (status IN ({','.join('?' * len(came_from))}) "
                  "OR (status = 'approved' AND claimed_by = ? AND claimed_by != ''))",
                  (util.iso(), token, until, approval_id, *came_from, previous))
        return token if c.total_changes > before else ""


def bind_publication(approval_id: int, token: str, marker: str) -> bool:
    with db() as c:
        return c.execute("UPDATE approvals SET emit = json_set(emit, '$._publication_marker', ?) "
                         "WHERE id = ? AND status = 'approved' AND claimed_by = ? "
                         "AND json_extract(emit, '$._publication_marker') IS NULL",
                         (marker, approval_id, token)).rowcount == 1


def renew_claim(approval_id: int, token: str, until: str) -> bool:
    with db() as c:
        before = c.total_changes
        c.execute("UPDATE approvals SET claimed_until = ? WHERE id = ? AND status = 'approved' AND claimed_by = ?",
                  (until, approval_id, token))
        return c.total_changes > before


def lapsed_claims() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT id, claimed_by, claimed_until FROM approvals "
                                           "WHERE status = 'approved' AND claimed_until < ? ORDER BY id", (util.iso(),))]


def settle_approval(approval_id: int, status: str, outcome: str, result: str = "", *, judged: bool,
                    came_from: tuple[str, ...] = ("approved",), token: str | None = None,
                    until: str | None = None) -> bool:
    held = [f"status IN ({','.join('?' * len(came_from))})", *(["claimed_by = ?"] if token is not None else []),
            *(["claimed_until = ?"] if until is not None else [])]
    args = [*came_from, *(a for a in (token, until) if a is not None)]
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET status = ?, result = ?, decided = ?, claimed_by = '', claimed_until = '' "  # noqa: S608
                  f"WHERE id = ? AND {' AND '.join(held)}", (status, result[:1000], util.iso(), approval_id, *args))
        if c.total_changes == before:
            return False
        if judged:
            insights.judge_approval(c, approval_id, outcome)
        _sync_inbox(c, "approvals")
        _sync_inbox(c, "outbox")
        return True


def add_mined(pr_url: str, repo: str, issue: str, run_id: str, status: str, why: str = "", task: str = "") -> None:
    from . import tasks as task_store

    with db() as c:
        if task_store.has_identity_expand(c):
            tnum: int | None = None
            if run_id:
                rrow = c.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
                if rrow and rrow["task_id"] is not None:
                    tnum = int(rrow["task_id"])
            if tnum is None and repo and issue:
                tnum = task_store.TaskStore.find_task_num(c, repo, str(issue))
            c.execute(
                "INSERT OR REPLACE INTO mined (pr_url, repo, issue, run_id, status, why, task, ts, task_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (pr_url, repo, issue, run_id, status, why, task, util.iso(), tnum),
            )
        else:
            c.execute("INSERT OR REPLACE INTO mined (pr_url, repo, issue, run_id, status, why, task, ts) "
                      "VALUES (?,?,?,?,?,?,?,?)", (pr_url, repo, issue, run_id, status, why, task, util.iso()))


def mined_row(pr_url: str) -> dict:
    with reader() as c:
        row = c.execute("SELECT * FROM mined WHERE pr_url = ?", (pr_url,)).fetchone()
    return dict(row) if row else {}


def mined_urls() -> set[str]:
    with reader() as c:
        return {r["pr_url"] for r in c.execute("SELECT pr_url FROM mined")}


def stale_approved(c, cutoff):
    return c.execute("SELECT id FROM approvals WHERE status = 'approved' AND decided < ? ORDER BY id",
                     (cutoff,)).fetchall()
