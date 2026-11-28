from datetime import UTC, datetime

from arete import paths

PHASE = "backfill"


def _iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply(conn):
    if conn.execute("SELECT 1 FROM runs WHERE id = 'chat'").fetchone():
        return
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(NULLIF(ts, '')) AS started, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
        "COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out "
        "FROM steps WHERE run_id = 'chat'"
    ).fetchone()
    if not row or not row["n"]:
        return
    owner_row = conn.execute(
        "SELECT id FROM principals WHERE kind = 'human' AND status = 'active' AND role = 'owner' "
        "ORDER BY created, oid LIMIT 1"
    ).fetchone()
    owner_id = owner_row[0] if owner_row else None
    now = _iso()
    started = row["started"] or now
    files_purged = None if (paths.RUNS_DIR / "chat").is_dir() else now
    conn.execute(
        "INSERT INTO runs (id, kind, status, started, finished, cost_usd, tokens_in, tokens_out, "
        "payer, chain, restart, files_purged) VALUES ('chat', 'chat', 'done', ?, ?, ?, ?, ?, ?, 'chat', '', ?)",
        (started, started, row["cost_usd"], row["tokens_in"], row["tokens_out"], owner_id, files_purged),
    )
