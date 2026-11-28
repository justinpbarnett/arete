from __future__ import annotations

PHASE = "contract"

TRIAGE_VIEW_SQL = """
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
    COALESCE(v.cost, 0.0) AS cost,
    COALESCE(v.at, r.updated, '') AS ts,
    COALESCE(v.kind, '') AS kind,
    COALESCE(v.repos, '[]') AS repos,
    COALESCE(v.assumptions, '[]') AS assumptions,
    json_extract(r.facts, '$.sprint') AS in_sprint,
    COALESCE(json_extract(r.facts, '$.parent'), '') AS parent,
    '' AS closed,
    COALESCE(v.checks, '[]') AS checks,
    COALESCE(v.id, r.version, 1) AS version,
    r.id AS ref_id
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
    COALESCE(v.cost, 0.0) AS cost,
    COALESCE(v.at, rs.ts, r.updated, '') AS ts,
    COALESCE(v.kind, '') AS kind,
    COALESCE(v.repos, '[]') AS repos,
    COALESCE(v.assumptions, '[]') AS assumptions,
    json_extract(r.facts, '$.sprint') AS in_sprint,
    COALESCE(json_extract(r.facts, '$.parent'), '') AS parent,
    rs.delivered AS closed,
    COALESCE(v.checks, '[]') AS checks,
    COALESCE(v.id, r.version, 1) AS version,
    r.id AS ref_id
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


def apply(conn) -> None:
    from arete.store import db
    from arete.store.migrations.m0066_triage_verdicts_expand import backfill_triage_rows

    backfill_triage_rows(conn)

    tbl = conn.execute("SELECT type FROM sqlite_master WHERE name = 'triage'").fetchone()
    if tbl is not None:
        if tbl[0] == "table":
            conn.execute("DROP TABLE triage")
        elif tbl[0] == "view":
            conn.execute("DROP VIEW triage")

    conn.execute(TRIAGE_VIEW_SQL)

    target = str(db.TRIAGE_VERDICTS_CONTRACT_SCHEMA)
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < ? "
        "THEN ? ELSE value END WHERE key = 'oldest_writer'",
        (db.TRIAGE_VERDICTS_CONTRACT_SCHEMA, target),
    )
