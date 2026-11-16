from contextlib import ExitStack

from arete.store.migrations.m0006_runs_budget_backfill import apply as backfill

PHASE = "contract"


def apply(conn):
    from arete import record

    with ExitStack() as held:
        running = []
        for rid, pid, lock in conn.execute("SELECT id, pid, lock FROM runs WHERE kind='pipeline' "
                                          "AND status='running' AND measured_at IS NULL").fetchall():
            if held.enter_context(record.dead({"pid": pid, "lock": lock})):
                running.append(rid)
        backfill(conn, running=running)
    conn.execute("UPDATE schema_meta SET value = '7' WHERE key = 'oldest_writer'")
