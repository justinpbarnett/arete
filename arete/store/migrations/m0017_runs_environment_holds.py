PHASE = "expand"

SQL = """
DROP INDEX runs_environment_retry_once;
CREATE UNIQUE INDEX runs_environment_retry_once ON runs (environment_retry_of)
    WHERE environment_retry_of IS NOT NULL AND dry_run = 0 AND status != 'dry-run';

CREATE TABLE environment_holds (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    waits TEXT NOT NULL DEFAULT '{}',
    released INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE environment_hold_prs (
    run_id TEXT NOT NULL REFERENCES environment_holds(run_id) ON DELETE CASCADE,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    url TEXT NOT NULL,
    head TEXT NOT NULL,
    PRIMARY KEY (run_id, repo, number)
);
CREATE TABLE environment_gap_creations (
    repo TEXT NOT NULL,
    signature TEXT NOT NULL,
    day TEXT NOT NULL,
    PRIMARY KEY (repo, signature, day)
);
"""
