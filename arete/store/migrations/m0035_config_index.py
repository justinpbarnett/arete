PHASE = "expand"
SQL = """
CREATE TABLE IF NOT EXISTS config_index (
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    origin TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    version TEXT NOT NULL,
    n INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    author TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    live INTEGER NOT NULL DEFAULT 0,
    fields TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (origin, path, sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS config_live_one ON config_index (origin, path) WHERE live = 1;
"""
