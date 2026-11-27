PHASE = "expand"

SQL = """
CREATE TABLE factory_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    version INTEGER NOT NULL,
    actor TEXT NOT NULL,
    changed TEXT NOT NULL,
    resumed TEXT NOT NULL DEFAULT ''
);
INSERT INTO factory_state (id, enabled, version, actor, changed)
VALUES (1, 1, 1, 'system', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

CREATE TABLE factory_targets (
    id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    phase TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    successor TEXT NOT NULL DEFAULT '',
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE factory_processes (
    id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    identity TEXT NOT NULL,
    kind TEXT NOT NULL,
    generation INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    signalled TEXT NOT NULL DEFAULT ''
);
CREATE TABLE session_checkpoints (
    run TEXT PRIMARY KEY,
    turn INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    session TEXT NOT NULL,
    phase TEXT NOT NULL
);
"""
