PHASE = "expand"

SQL = """
ALTER TABLE steps ADD COLUMN runner_version TEXT;
ALTER TABLE steps ADD COLUMN unread INTEGER NOT NULL DEFAULT 0;

CREATE TABLE turn_index (
    step_id INTEGER NOT NULL,
    n INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    seconds REAL NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_nusd INTEGER,
    file TEXT NOT NULL DEFAULT '',
    member_offset INTEGER NOT NULL DEFAULT 0,
    member_len INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (step_id, n)
) STRICT;

CREATE INDEX turn_index_run ON turn_index (run_id, step_id, n);
"""
