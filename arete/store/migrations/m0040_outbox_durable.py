PHASE = "expand"

_DDL = """
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marker TEXT UNIQUE,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    object TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT 'self' CHECK (audience IN ('self', 'team', 'client', 'public')),
    posts_as TEXT,
    task_id TEXT,
    ref_id TEXT,
    run_id TEXT,
    step TEXT,
    loop TEXT,
    drafted_by TEXT NOT NULL,
    slot TEXT NOT NULL UNIQUE,
    rev INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'draft' CHECK (
        state IN ('draft', 'edited', 'approved', 'held', 'sending', 'sent', 'failed', 'rejected', 'discarded')
    ),
    opened_at TEXT,
    approved_by TEXT,
    approved_rule TEXT,
    approved_rule_version TEXT,
    send_at TEXT,
    base_remote_updated TEXT,
    sent_at TEXT,
    sent_ref TEXT,
    sent_url TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,
    summary TEXT NOT NULL DEFAULT '',
    plain_summary TEXT NOT NULL DEFAULT '',
    review TEXT NOT NULL DEFAULT '',
    shape INTEGER NOT NULL DEFAULT 1,
    legacy_approval INTEGER UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    CHECK (audience = 'self' OR approved_rule IS NULL),
    CHECK (audience = 'self' OR state NOT IN ('approved', 'held', 'sending', 'sent') OR opened_at IS NOT NULL),
    CHECK (state != 'sent' OR sent_ref IS NOT NULL),
    CHECK (state NOT IN ('approved', 'held', 'sending', 'sent') OR marker IS NOT NULL OR legacy_approval IS NOT NULL)
) STRICT;

CREATE INDEX outbox_due ON outbox (state, send_at);
CREATE INDEX outbox_task ON outbox (task_id, id);

CREATE TABLE outbox_revisions (
    item INTEGER NOT NULL,
    rev INTEGER NOT NULL,
    base_rev INTEGER,
    by TEXT NOT NULL,
    body TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (item, rev)
) STRICT;

CREATE TABLE outbox_redactions (
    item INTEGER NOT NULL,
    rev INTEGER NOT NULL,
    n INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('secret', 'token', 'email', 'path') OR kind LIKE 'rule:%'),
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    why TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (item, rev, n)
) STRICT;

CREATE TABLE delivery_attempts (
    item INTEGER NOT NULL,
    n INTEGER NOT NULL,
    started TEXT NOT NULL,
    ended TEXT,
    outcome TEXT CHECK (outcome IS NULL OR outcome IN ('sent', 'refused', 'unknown', 'found')),
    http_status INTEGER,
    retry_after INTEGER,
    external_id TEXT,
    external_url TEXT,
    lookup TEXT,
    error TEXT,
    PRIMARY KEY (item, n)
) STRICT;

CREATE UNIQUE INDEX delivery_one_open ON delivery_attempts (item) WHERE ended IS NULL;
"""


def apply(conn) -> None:
    from arete.store import db

    db.execute_statements(conn, _DDL)
    if conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone() is not None:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('accepts_bare_mark', '1')"
        )
