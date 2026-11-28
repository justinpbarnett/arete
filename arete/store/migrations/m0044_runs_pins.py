PHASE = "expand"

SQL = """
ALTER TABLE runs ADD COLUMN config_commit TEXT;
ALTER TABLE runs ADD COLUMN resolved_sha TEXT;
ALTER TABLE steps ADD COLUMN settings_sha TEXT;

CREATE TABLE blobs (
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created TEXT NOT NULL
) STRICT;

CREATE TABLE settings_snapshots (
    sha256 TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    created TEXT NOT NULL
) STRICT;

CREATE TABLE step_inputs (
    step_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    call INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX step_inputs_step ON step_inputs (step_id);
CREATE INDEX step_inputs_sha ON step_inputs (sha256) WHERE sha256 != '';
"""
