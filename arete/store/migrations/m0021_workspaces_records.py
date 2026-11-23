PHASE = "expand"

SQL = """
CREATE TABLE workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK (name NOT IN ('', '.', '..') AND instr(name, '/') = 0 AND instr(name, char(0)) = 0),
    repo TEXT NOT NULL DEFAULT '',
    ticket TEXT NOT NULL DEFAULT '',
    owner_run TEXT NOT NULL DEFAULT '',
    checkouts TEXT NOT NULL DEFAULT '[]' CHECK (json_type(checkouts) = 'array'),
    state TEXT NOT NULL CHECK (state IN ('live', 'kept', 'released', 'held', 'removed')),
    kept_reason TEXT NOT NULL DEFAULT '',
    held_by TEXT NOT NULL DEFAULT '[]' CHECK (json_type(held_by) = 'array' AND json_array_length(held_by) <= 20),
    remove_asked_by TEXT NOT NULL DEFAULT '',
    made_at TEXT NOT NULL,
    released_at TEXT NOT NULL DEFAULT '',
    removed_at TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0 CHECK (bytes >= 0),
    salt TEXT NOT NULL DEFAULT '',
    heads TEXT NOT NULL DEFAULT '{}' CHECK (json_type(heads) = 'object'),
    onto TEXT NOT NULL DEFAULT '{}' CHECK (json_type(onto) = 'object'),
    retained TEXT NOT NULL DEFAULT '{}' CHECK (json_type(retained) = 'object'),
    sweep_attempts INTEGER NOT NULL DEFAULT 0 CHECK (sweep_attempts >= 0),
    last_sweep_at TEXT NOT NULL DEFAULT '',
    last_sweep_error TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
) STRICT;

CREATE UNIQUE INDEX workspaces_one_open ON workspaces (name) WHERE state != 'removed';
CREATE INDEX workspaces_by_state ON workspaces (state, released_at);

CREATE TABLE workspace_files (
    workspace INTEGER NOT NULL REFERENCES workspaces(id),
    name TEXT NOT NULL CHECK (name != '' AND substr(name, 1, 1) != '/' AND instr(name, char(0)) = 0
                              AND instr('/' || name || '/', '/../') = 0),
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (workspace, name)
) STRICT;
"""
