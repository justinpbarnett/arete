from __future__ import annotations

PHASE = "contract"


def apply(conn) -> None:
    from arete.store import db

    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "issue" in cols:
        bad = conn.execute(
            "SELECT count(*) FROM runs "
            "WHERE issue IS NOT NULL "
            "  AND CAST(issue AS TEXT) != '' "
            "  AND issue_key IS NOT CAST(issue AS TEXT) "
            "  AND NOT ("
            "      CAST(issue AS TEXT) GLOB 't-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]*' "
            "      AND task_id IS NOT NULL "
            "      AND issue_key = 'T-' || task_id"
            "  )"
        ).fetchone()[0]
        if bad:
            raise RuntimeError(
                f"runs.issue_key disagrees with runs.issue on {bad} row(s); backfill before contracting"
            )
        conn.execute("DROP INDEX IF EXISTS runs_ticket")
        conn.execute("DROP INDEX IF EXISTS runs_ticket_seq")
        conn.execute("ALTER TABLE runs DROP COLUMN issue")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_ticket ON runs (repo, issue_key, kind, started)")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_ticket_seq ON runs (repo, issue_key, seq)")

    target = str(db.RUNS_ISSUE_CONTRACT_SCHEMA)
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < ? "
        "THEN ? ELSE value END WHERE key = 'oldest_writer'",
        (db.RUNS_ISSUE_CONTRACT_SCHEMA, target),
    )
