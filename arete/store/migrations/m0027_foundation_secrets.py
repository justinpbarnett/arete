PHASE = "expand"

SQL = """
CREATE TABLE secrets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    provider TEXT,
    account_id TEXT REFERENCES accounts(id),
    managed_by TEXT,
    store TEXT NOT NULL CHECK(store IN ('keychain', 'pve_store', 'owner_file')),
    reference TEXT NOT NULL UNIQUE,
    expose INTEGER NOT NULL DEFAULT 0 CHECK(expose IN (0, 1)),
    copy_of INTEGER REFERENCES secrets(id),
    required INTEGER NOT NULL DEFAULT 0 CHECK(required IN (0, 1)),
    fingerprint TEXT,
    status TEXT NOT NULL CHECK(status IN ('set', 'missing')),
    set_at TEXT,
    set_by TEXT,
    rotated_at TEXT,
    rotate_every_days INTEGER,
    expires_at TEXT,
    checked_at TEXT,
    check_ok INTEGER CHECK(check_ok IN (0, 1)),
    missing_since TEXT,
    last_used_at TEXT,
    last_used_run TEXT,
    last_used_step TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (name, scope)
) STRICT;

CREATE TABLE secret_events (
    id INTEGER PRIMARY KEY,
    secret_id INTEGER NOT NULL REFERENCES secrets(id),
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK(ok IN (0, 1)),
    fingerprint_after TEXT,
    reference_after TEXT,
    detail TEXT,
    at TEXT NOT NULL
) STRICT;
CREATE INDEX secret_events_secret ON secret_events(secret_id, id);

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    identity TEXT NOT NULL,
    mode TEXT NOT NULL,
    holder TEXT NOT NULL,
    usage TEXT NOT NULL CHECK(usage IN ('personal', 'workspace')),
    fingerprint TEXT NOT NULL,
    usage_scope TEXT,
    secret_id INTEGER REFERENCES secrets(id),
    version INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    CHECK (mode != 'subscription' OR usage = 'personal'),
    CHECK (mode != 'subscription' OR holder LIKE 'u:%'),
    UNIQUE (provider, mode, identity)
) STRICT;
"""
