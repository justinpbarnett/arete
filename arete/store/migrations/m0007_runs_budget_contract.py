from arete.store.migrations.m0006_runs_budget_backfill import apply as backfill

PHASE = "contract"


def apply(conn):
    backfill(conn)
    conn.execute("UPDATE schema_meta SET value = '7' WHERE key = 'oldest_writer'")
