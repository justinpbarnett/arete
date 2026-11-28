from __future__ import annotations

PHASE = "contract"


def apply(conn) -> None:
    from arete.store import db
    from arete.store.migrations.m0073_intake_seen_backfill import backfill_seen_rows

    backfill_seen_rows(conn)
    conn.execute("DROP TABLE IF EXISTS seen")
    target = str(db.INTAKE_SEEN_CONTRACT_SCHEMA)
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < ? "
        "THEN ? ELSE value END WHERE key = 'oldest_writer'",
        (db.INTAKE_SEEN_CONTRACT_SCHEMA, target),
    )
