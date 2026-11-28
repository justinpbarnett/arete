from __future__ import annotations

import json

PHASE = "backfill"

_SIMPLE_TWINS: tuple[tuple[str, str, str], ...] = (
    ("steps", "cost_usd", "cost_nusd"),
    ("evals", "cost_usd", "cost_nusd"),
    ("triage_verdicts", "cost", "cost_nusd"),
    ("research", "cost", "cost_nusd"),
    ("chats", "spent", "spent_nusd"),
    ("chat_messages", "cost", "cost_nusd"),
    ("chat_messages", "paid_usd", "paid_nusd"),
    ("captures", "jev_cost_usd", "jev_cost_nusd"),
    ("captures", "inspect_cost_usd", "inspect_cost_nusd"),
)

TRIAGE_VIEW_NUSD_SQL = """
CREATE VIEW triage AS
SELECT
    COALESCE(NULLIF(json_extract(r.facts, '$.triage_ref'), ''), v.ref, r.key) AS ref,
    r.connector AS source,
    r.system AS tracker,
    COALESCE(v.repo, '') AS repo,
    COALESCE(v.route, '') AS route,
    r.title AS title,
    r.url AS url,
    COALESCE(r.remote_updated, '') AS updated,
    v.ready AS ready,
    v.code AS code,
    COALESCE(v.size, '') AS size,
    COALESCE(v.questions, '[]') AS questions,
    COALESCE(v.plan, '[]') AS plan,
    CASE
        WHEN v.id IS NULL THEN 'waiting: more than 20 tickets were due in one refresh'
        WHEN v.ready IS NULL AND COALESCE(v.reason, '') != '' THEN 'triage failed: ' || v.reason
        ELSE COALESCE(v.why, '')
    END AS why,
    COALESCE(v.cost_nusd, CAST(round(COALESCE(v.cost, 0.0) * 1e9) AS INTEGER)) / 1000000000.0 AS cost,
    COALESCE(v.at, r.updated, '') AS ts,
    COALESCE(v.kind, '') AS kind,
    COALESCE(v.repos, '[]') AS repos,
    COALESCE(v.assumptions, '[]') AS assumptions,
    json_extract(r.facts, '$.sprint') AS in_sprint,
    COALESCE(json_extract(r.facts, '$.parent'), '') AS parent,
    '' AS closed,
    COALESCE(v.checks, '[]') AS checks,
    COALESCE(v.id, r.version, 1) AS version,
    r.id AS ref_id,
    v.cost_nusd AS cost_nusd
FROM refs r
JOIN cursors cur ON cur.source = 'triage:' || r.connector
                AND cur.value IS NOT NULL
                AND r.listed_at = cur.value
LEFT JOIN triage_verdicts v ON v.id = (
    SELECT MAX(tv.id) FROM triage_verdicts tv WHERE tv.ref_id = r.id
)
WHERE NOT EXISTS (
    SELECT 1 FROM triage_approvals a
    WHERE a.ref_id = r.id
      AND a.ts >= COALESCE(r.remote_updated, '')
)
UNION ALL
SELECT
    COALESCE(NULLIF(json_extract(r.facts, '$.triage_ref'), ''), v.ref, rs.ref, r.key) AS ref,
    COALESCE(NULLIF(r.connector, ''), rs.source, '') AS source,
    r.system AS tracker,
    COALESCE(v.repo, '') AS repo,
    COALESCE(v.route, '') AS route,
    COALESCE(NULLIF(r.title, ''), rs.title, '') AS title,
    COALESCE(NULLIF(r.url, ''), rs.url, '') AS url,
    COALESCE(NULLIF(rs.updated, ''), r.remote_updated, '') AS updated,
    v.ready AS ready,
    v.code AS code,
    COALESCE(v.size, '') AS size,
    COALESCE(v.questions, '[]') AS questions,
    COALESCE(v.plan, '[]') AS plan,
    CASE
        WHEN v.id IS NULL THEN ''
        WHEN v.ready IS NULL AND COALESCE(v.reason, '') != '' THEN 'triage failed: ' || v.reason
        ELSE COALESCE(v.why, '')
    END AS why,
    COALESCE(v.cost_nusd, CAST(round(COALESCE(v.cost, 0.0) * 1e9) AS INTEGER)) / 1000000000.0 AS cost,
    COALESCE(v.at, rs.ts, r.updated, '') AS ts,
    COALESCE(v.kind, '') AS kind,
    COALESCE(v.repos, '[]') AS repos,
    COALESCE(v.assumptions, '[]') AS assumptions,
    json_extract(r.facts, '$.sprint') AS in_sprint,
    COALESCE(json_extract(r.facts, '$.parent'), '') AS parent,
    rs.delivered AS closed,
    COALESCE(v.checks, '[]') AS checks,
    COALESCE(v.id, r.version, 1) AS version,
    r.id AS ref_id,
    v.cost_nusd AS cost_nusd
FROM research rs
JOIN refs r ON r.id = rs.ref_id
JOIN cursors cur ON cur.source = 'triage:' || COALESCE(NULLIF(r.connector, ''), rs.source, '')
                AND cur.value IS NOT NULL
LEFT JOIN triage_verdicts v ON v.id = (
    SELECT MAX(tv.id) FROM triage_verdicts tv WHERE tv.ref_id = r.id
)
WHERE COALESCE(rs.delivered, '') != ''
  AND rs.delivered >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')
  AND COALESCE(r.listed_at, '') != cur.value
  AND NOT EXISTS (
      SELECT 1 FROM triage_approvals a
      WHERE a.ref_id = r.id
        AND a.ts >= COALESCE(r.remote_updated, '')
  )
"""


def backfill_legacy_nusd(conn, before_nusd: dict[str, int] | None = None) -> int:
    conn.execute(
        "UPDATE steps SET price_version = 'legacy' "
        "WHERE (runner = 'legacy' OR kind = 'legacy') AND COALESCE(price_version, '') = ''"
    )
    conn.execute(
        "UPDATE steps SET price_version = 'correction' "
        "WHERE (runner = 'correction' OR kind = 'correction') AND COALESCE(price_version, '') = ''"
    )
    step_cols = {row[1] for row in conn.execute("PRAGMA table_info(steps)")}
    has_lifecycle = "node" in step_cols and "status" in step_cols and "version" in step_cols
    before_json = json.dumps(before_nusd or {})
    if has_lifecycle:
        conn.execute(
            """
            WITH totals AS MATERIALIZED (
                SELECT run_id,
                       SUM(COALESCE(cost_nusd, CAST(round(COALESCE(cost_usd, 0) * 1e9) AS INTEGER))) AS nusd,
                       SUM(tokens_in) AS tin,
                       SUM(tokens_out) AS tout
                FROM steps GROUP BY run_id
            ), eligible AS MATERIALIZED (
                SELECT runs.id, runs.started,
                       COALESCE(runs.cost_nusd, CAST(round(COALESCE(runs.cost_usd, 0) * 1e9) AS INTEGER)) AS run_nusd,
                       COALESCE(totals.nusd, 0) AS spent_nusd,
                       COALESCE(runs.tokens_in, 0) - COALESCE(totals.tin, 0) AS tin,
                       COALESCE(runs.tokens_out, 0) - COALESCE(totals.tout, 0) AS tout,
                       CAST(edits.value AS INTEGER) AS old_nusd
                FROM runs
                LEFT JOIN totals ON totals.run_id = runs.id
                LEFT JOIN json_each(?) AS edits ON edits.key = runs.id
                WHERE runs.started < (SELECT MIN(ts) FROM steps)
                  AND COALESCE(runs.cost_nusd, CAST(round(COALESCE(runs.cost_usd, 0) * 1e9) AS INTEGER))
                      != COALESCE(totals.nusd, 0)
            )
            INSERT INTO steps (
                run_id, step, node, kind, runner, ok, status, auth_mode, ts, queued_at,
                cost_usd, cost_nusd, paid_nusd, price_version, tokens_in, tokens_out, error, version
            )
            SELECT id, 'legacy', 'legacy', 'legacy', 'legacy', 1, 'done', 'subscription', started, started,
                   (COALESCE(old_nusd, run_nusd) - spent_nusd) / 1000000000.0,
                   COALESCE(old_nusd, run_nusd) - spent_nusd,
                   0, 'legacy', tin, tout, '', 1
            FROM eligible
            UNION ALL
            SELECT id, 'correction', 'correction', 'correction', 'correction', 1, 'done', 'subscription',
                   started, started,
                   (run_nusd - old_nusd) / 1000000000.0,
                   run_nusd - old_nusd,
                   0, 'correction', 0, 0, 'legacy repricing from supplied before value', 1
            FROM eligible
            WHERE old_nusd IS NOT NULL AND run_nusd != old_nusd
            """,
            (before_json,),
        )
    else:
        conn.execute(
            """
            WITH totals AS MATERIALIZED (
                SELECT run_id,
                       SUM(COALESCE(cost_nusd, CAST(round(COALESCE(cost_usd, 0) * 1e9) AS INTEGER))) AS nusd,
                       SUM(tokens_in) AS tin,
                       SUM(tokens_out) AS tout
                FROM steps GROUP BY run_id
            ), eligible AS MATERIALIZED (
                SELECT runs.id, runs.started,
                       COALESCE(runs.cost_nusd, CAST(round(COALESCE(runs.cost_usd, 0) * 1e9) AS INTEGER)) AS run_nusd,
                       COALESCE(totals.nusd, 0) AS spent_nusd,
                       COALESCE(runs.tokens_in, 0) - COALESCE(totals.tin, 0) AS tin,
                       COALESCE(runs.tokens_out, 0) - COALESCE(totals.tout, 0) AS tout,
                       CAST(edits.value AS INTEGER) AS old_nusd
                FROM runs
                LEFT JOIN totals ON totals.run_id = runs.id
                LEFT JOIN json_each(?) AS edits ON edits.key = runs.id
                WHERE runs.started < (SELECT MIN(ts) FROM steps)
                  AND COALESCE(runs.cost_nusd, CAST(round(COALESCE(runs.cost_usd, 0) * 1e9) AS INTEGER))
                      != COALESCE(totals.nusd, 0)
            )
            INSERT INTO steps (
                run_id, step, kind, runner, ok, auth_mode, ts,
                cost_usd, cost_nusd, paid_nusd, price_version, tokens_in, tokens_out, error
            )
            SELECT id, 'legacy', 'legacy', 'legacy', 1, 'subscription', started,
                   (COALESCE(old_nusd, run_nusd) - spent_nusd) / 1000000000.0,
                   COALESCE(old_nusd, run_nusd) - spent_nusd,
                   0, 'legacy', tin, tout, ''
            FROM eligible
            UNION ALL
            SELECT id, 'correction', 'correction', 'correction', 1, 'subscription', started,
                   (run_nusd - old_nusd) / 1000000000.0,
                   run_nusd - old_nusd,
                   0, 'correction', 0, 0, 'legacy repricing from supplied before value'
            FROM eligible
            WHERE old_nusd IS NOT NULL AND run_nusd != old_nusd
            """,
            (before_json,),
        )
    return int(conn.execute("SELECT changes()").fetchone()[0])


def apply(conn) -> None:
    from arete import agent, config

    # (1) Fill every NULL nanodollar twin from its own REAL column.
    for table, real_col, nusd_col in _SIMPLE_TWINS:
        conn.execute(
            f"UPDATE {table} SET {nusd_col} = CAST(round({real_col} * 1e9) AS INTEGER) "  # noqa: S608
            f"WHERE {nusd_col} IS NULL AND {real_col} IS NOT NULL"
        )

    # For runs, if cost_nusd is NULL and the run's float total matches its float step sum
    # within 1e-9, take the sum of its steps' nanodollars; otherwise convert its own column.
    conn.execute(
        """
        UPDATE runs
        SET cost_nusd = COALESCE(
            (
                SELECT CASE
                    WHEN COUNT(*) > 0
                         AND ABS(COALESCE(runs.cost_usd, 0.0) - COALESCE(SUM(steps.cost_usd), 0.0)) < 1e-9
                    THEN SUM(COALESCE(steps.cost_nusd, 0))
                    ELSE NULL
                END
                FROM steps
                WHERE steps.run_id = runs.id
            ),
            CAST(round(runs.cost_usd * 1e9) AS INTEGER)
        )
        WHERE runs.cost_nusd IS NULL AND runs.cost_usd IS NOT NULL
        """
    )

    # (2) Run legacy cost backfill in nanodollars and stamp existing legacy/correction rows.
    backfill_legacy_nusd(conn)

    # (3) Apply the frozen paid rule once on every steps row with NULL paid_nusd.
    cfg, _ = config.load_or_problem()
    local_providers = tuple(cfg.local_providers)
    pending = conn.execute(
        "SELECT id, runner, provider, model, auth_mode, kind, cost_nusd, price_version "
        "FROM steps WHERE paid_nusd IS NULL ORDER BY id"
    ).fetchall()
    for row in pending:
        runner = str(row["runner"] or "")
        provider = str(row["provider"] or "")
        model = str(row["model"] or "")
        auth_mode = str(row["auth_mode"] or "")
        kind = str(row["kind"] or "")
        cost_nusd = int(row["cost_nusd"] or 0)
        price_version = str(row["price_version"] or "")
        billing = agent.billing_of(
            {"runner": runner, "provider": provider, "model": model, "auth_mode": auth_mode},
            local_providers,
        )
        paid_nusd = cost_nusd if billing == "api_key" else 0
        new_auth_mode = auth_mode if runner in ("guard", "replay") else billing
        if not price_version:
            if runner == "legacy" or kind == "legacy":
                price_version = "legacy"
            elif runner == "correction" or kind == "correction":
                price_version = "correction"
            else:
                price_version = "inferred"
        conn.execute(
            "UPDATE steps SET paid_nusd = ?, auth_mode = ?, price_version = ? WHERE id = ?",
            (paid_nusd, new_auth_mode, price_version, row["id"]),
        )

    # Update triage view to read cost from triage_verdicts.cost_nusd when present.
    triage_obj = conn.execute("SELECT type FROM sqlite_master WHERE name = 'triage'").fetchone()
    if triage_obj is not None and triage_obj[0] == "view":
        conn.execute("DROP VIEW triage")
        conn.execute(TRIAGE_VIEW_NUSD_SQL)
