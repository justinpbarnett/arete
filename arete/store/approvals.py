from __future__ import annotations

import json
import secrets

from .. import util
from . import insights
from .db import db, reader

TABLES = {
    "approvals": "kept",
    "proposals": "kept",
    "mined": "kept",
}


DECIDABLE = ("pending", "failed")


APPROVAL_NOTES = ("plain_summary", "plain_summary_tried", "review", "review_tried")


def add_approval(loop: str, run_id: str, emit: dict, summary: str, failed: str = "") -> int:
    with db() as c:
        cur = c.execute("INSERT INTO approvals (loop, run_id, emit, summary, status, result, created) "
                        "VALUES (?,?,?,?,?,?,?)", (loop, run_id, json.dumps(emit, default=str), summary[:2000],
                                                   "failed" if failed else "pending", failed[:1000], util.iso()))
        return cur.lastrowid


def approvals(connect=reader) -> list[dict]:
    with connect() as c:
        return [approval_of(r) for r in c.execute("SELECT a.*, COALESCE(r.repo, '') AS run_repo FROM approvals a "
                                                 "LEFT JOIN runs r ON r.id = a.run_id "
                                                 f"WHERE a.status IN ({','.join('?' * len(DECIDABLE))}) "
                                                 "ORDER BY a.id DESC LIMIT 100", DECIDABLE)]


def set_approval_note(approval_id: int, note: str, value: str, while_in: tuple[str, ...] = DECIDABLE) -> bool:
    if note not in APPROVAL_NOTES:
        raise ValueError(f"an approval has no note {note}; it has {', '.join(APPROVAL_NOTES)}")
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET {note} = ? WHERE id = ? AND status IN ({','.join('?' * len(while_in))})",
                  (value, approval_id, *while_in))
        return c.total_changes > before


def approval(approval_id: int) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return approval_of(r) if r else None


def approval_of(row) -> dict:
    return {**dict(row), "emit": json.loads(row["emit"] or "{}"), "review": json.loads(row["review"] or "{}")}


def claim_approval(approval_id: int, until: str, came_from: tuple[str, ...] = DECIDABLE, *, previous="") -> str:
    token = secrets.token_hex(8)
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET status = 'approved', result = '', decided = ?, claimed_by = ?, "
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
        c.execute(f"UPDATE approvals SET status = ?, result = ?, decided = ?, claimed_by = '', claimed_until = '' "
                  f"WHERE id = ? AND {' AND '.join(held)}", (status, result[:1000], util.iso(), approval_id, *args))
        if c.total_changes == before:
            return False
        if judged:
            insights.judge_approval(c, approval_id, outcome)
        return True


def add_proposal(run_id: str, p: dict, status: str, pr_url: str = "") -> None:
    with db() as c:
        c.execute(
            "INSERT INTO proposals (run_id, layer, repo, file, change, why, evidence, confidence, status, pr_url, created)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                p.get("layer"),
                p.get("repo"),
                p.get("file"),
                p.get("change"),
                p.get("why"),
                json.dumps(p.get("evidence", [])),
                float(p.get("confidence", 0)),
                status,
                pr_url,
                util.iso(),
            ),
        )


def check_proposal_revisions(layer: str, repo: str, family: str, revisions: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT id, file, evidence, status FROM proposals WHERE layer = ? AND repo = ? "
                                          "AND (file = ? OR file GLOB ?)", (layer, repo, family, revisions))]


def record_check_proposal(run_id: str, p: dict, family: str, revisions: str, legacy_ids: list[int]) -> None:
    scope = (p["layer"], p["repo"])
    with db() as c:
        if c.execute("SELECT 1 FROM proposals WHERE layer = ? AND repo = ? AND file = ? LIMIT 1",
                     (*scope, p["file"])).fetchone():
            return
        c.execute("UPDATE proposals SET status = 'superseded' WHERE layer = ? AND repo = ? AND status = 'hand' "
                  "AND (file = ? OR file GLOB ?)", (*scope, family, revisions))
        c.executemany("UPDATE proposals SET status = 'superseded' WHERE id = ? AND status = 'hand'",
                      ((proposal_id,) for proposal_id in legacy_ids))
        add_proposal(run_id, p, "hand")


RELEASING_PROPOSALS = ("rejected", "skipped", "filed", "superseded")


def runs_cited_by_kept_proposals(run_ids, connect=reader) -> set[str]:
    marks = ",".join("?" * len(RELEASING_PROPOSALS))
    with connect() as c:
        rows = c.execute(f"SELECT run_id, evidence FROM proposals WHERE status IS NULL OR status NOT IN ({marks})",
                         RELEASING_PROPOSALS).fetchall()
    return {r for r in run_ids if any(r == row["run_id"] or r in (row["evidence"] or "") for row in rows)}


def proposals(status: list[str] | None = None, limit: int = 200, connect=reader) -> list[dict]:
    q, args = "SELECT * FROM proposals", []
    if status:
        q += f" WHERE status IN ({','.join('?' * len(status))})"
        args += status
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def proposal(proposal_id: int, connect=reader) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    return dict(row) if row else None


def move_proposal(proposal_id: int, before: tuple[str, ...], after: str) -> bool:
    with db() as c:
        return c.execute(f"UPDATE proposals SET status = ? WHERE id = ? AND status IN ({','.join('?' * len(before))})",
                         (after, proposal_id, *before)).rowcount > 0


def forget_proposal(proposal_id: int) -> bool:
    return move_proposal(proposal_id, ("memory", "hand"), "filed")


def set_proposal_status_by_pr(pr_url: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE proposals SET status = ? WHERE pr_url = ?", (status, pr_url))


def add_mined(pr_url: str, repo: str, issue: str, run_id: str, status: str, why: str = "", task: str = "") -> None:
    with db() as c:
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
