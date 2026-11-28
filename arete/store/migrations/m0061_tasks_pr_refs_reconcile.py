from __future__ import annotations

PHASE = "contract"


def apply(conn) -> None:
    from arete.store import db, tasks

    for _ in range(4):
        stats = tasks.reconcile_pr_refs(conn)
        if not any(stats.values()):
            break
    target = str(db.PR_REFS_RECONCILE_SCHEMA)
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < ? "
        "THEN ? ELSE value END WHERE key = 'oldest_writer'",
        (db.PR_REFS_RECONCILE_SCHEMA, target),
    )
