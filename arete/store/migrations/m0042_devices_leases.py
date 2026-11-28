PHASE = "expand"

SQL = """
CREATE TABLE device_probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device TEXT NOT NULL,
    device_version TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    system TEXT NOT NULL DEFAULT '',
    home TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '[]',
    gpus TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX device_probes_device ON device_probes (device, at DESC, id DESC);

CREATE TABLE leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    device TEXT NOT NULL,
    device_version TEXT NOT NULL DEFAULT '',
    epoch INTEGER NOT NULL DEFAULT 1,
    token_hash TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    renewed TEXT NOT NULL DEFAULT '',
    expires TEXT NOT NULL,
    released TEXT,
    outcome TEXT NOT NULL DEFAULT '' CHECK (
        outcome IN ('', 'deleted', 'delete_failed', 'kept', 'never_answered', 'expired', 'collided')
    ),
    kept_reason TEXT NOT NULL DEFAULT '',
    vm_name TEXT NOT NULL DEFAULT '',
    vmid INTEGER,
    ready TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    host_key_fingerprint TEXT NOT NULL DEFAULT '',
    sweep_attempts INTEGER NOT NULL DEFAULT 0,
    failing_since TEXT,
    last_sweep TEXT NOT NULL DEFAULT '',
    last_sweep_error TEXT NOT NULL DEFAULT '',
    sweep_gave_up INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX leases_open ON leases (device, expires) WHERE released IS NULL;
CREATE INDEX leases_run ON leases (run_id) WHERE run_id IS NOT NULL;

CREATE TABLE lease_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id INTEGER NOT NULL REFERENCES leases(id),
    token_hash TEXT NOT NULL,
    created TEXT NOT NULL,
    dead_at TEXT
);
CREATE INDEX lease_tokens_lease ON lease_tokens (lease_id);

CREATE TABLE run_controls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    lease_id INTEGER REFERENCES leases(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL,
    acked TEXT
);
CREATE INDEX run_controls_run ON run_controls (run_id, id);

CREATE TABLE run_streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    lease_id INTEGER REFERENCES leases(id),
    name TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    offset INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    closed TEXT
);
CREATE INDEX run_streams_lease ON run_streams (lease_id, id);

CREATE TABLE waits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    blocker TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    opened TEXT NOT NULL,
    cleared TEXT
);
CREATE UNIQUE INDEX waits_open ON waits (subject, blocker) WHERE cleared IS NULL;
CREATE INDEX waits_blocker ON waits (blocker) WHERE cleared IS NULL;

ALTER TABLE runs ADD COLUMN lease_id INTEGER;
"""
