PHASE = "expand"

SQL = """
INSERT INTO schema_meta VALUES ('token_key_ref', ''), ('token_generation', '0');
CREATE TABLE tokens (
    id TEXT PRIMARY KEY,
    principal TEXT NOT NULL REFERENCES principals(id),
    audience TEXT NOT NULL,
    label TEXT NOT NULL,
    scopes TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    expires TEXT,
    revoked_at TEXT,
    rotated_from TEXT REFERENCES tokens(id),
    last_used_at TEXT,
    last_used_node TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    kind TEXT CHECK(kind IN ('token', 'session', 'claim', 'ask')),
    profile TEXT,
    commands TEXT,
    lease_id TEXT,
    run_id TEXT,
    uses_left INTEGER,
    grace_until TEXT,
    created_by TEXT,
    revoke_reason TEXT,
    spend_cap_nusd INTEGER
) STRICT;
CREATE INDEX tokens_principal ON tokens(principal, created);
CREATE INDEX tokens_expiry ON tokens(expires) WHERE revoked_at IS NULL;
"""
