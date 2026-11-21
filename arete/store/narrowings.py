from __future__ import annotations

import json

from .db import reader, tx

TABLES = {"ticket_narrowings": "kept"}
LIMIT = 2
APPLIED = ("edited", "comment_sent", "complete")


def entry(row) -> dict:
    return {**dict(row), "plan": json.loads(row["plan"])}


def count(conn, project: str, issue: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM ticket_narrowings WHERE project = ? AND issue = ? AND phase IN (?, ?, ?)",
        (project, issue, *APPLIED),
    ).fetchone()[0]


def capped(project: str, issue: str) -> bool:
    with reader() as conn:
        return count(conn, project, issue) >= LIMIT


def pending(project: str, issue: str | None = None) -> list[dict]:
    with reader() as conn:
        query = "SELECT * FROM ticket_narrowings WHERE project = ? AND phase NOT IN ('complete', 'cancelled')"
        args = (project,)
        if issue is not None:
            query += " AND issue = ?"
            args += (issue,)
        return [entry(row) for row in conn.execute(query, args)]


def reserve(run_id: str, project: str, issue: str, plan: dict) -> dict:
    with tx() as conn:
        if count(conn, project, issue) >= LIMIT:
            return {}
        conn.execute(
            "INSERT OR IGNORE INTO ticket_narrowings (run_id, project, issue, plan, phase) VALUES (?, ?, ?, ?, 'ready')",
            (run_id, project, issue, json.dumps(plan)),
        )
        row = conn.execute("SELECT * FROM ticket_narrowings WHERE run_id = ?", (run_id,)).fetchone()
        return entry(row) if row and row["phase"] == "ready" else {}


def advance(operation: dict, phase: str, child: int | None = None) -> dict:
    with tx() as conn:
        changed = conn.execute(
            "UPDATE ticket_narrowings SET phase = ?, child = COALESCE(?, child), plan = COALESCE(?, plan) "
            "WHERE run_id = ? AND phase = ?",
            (phase, child, "{}" if phase in ("complete", "cancelled") else None,
             operation["run_id"], operation["phase"]),
        )
        if changed.rowcount != 1:
            raise RuntimeError("the ticket narrowing phase changed")
    return {**operation, "phase": phase, "child": child if child is not None else operation["child"]}


def completed(project: str, issue: str, run_id: str) -> bool:
    with reader() as conn:
        return conn.execute(
            "SELECT 1 FROM ticket_narrowings WHERE project = ? AND issue = ? AND run_id = ? AND phase = 'complete'",
            (project, issue, run_id),
        ).fetchone() is not None
