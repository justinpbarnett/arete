PHASE = "expand"

SQL = """
ALTER TABLE asks ADD COLUMN recovery_route TEXT NOT NULL DEFAULT 'owner'
    CHECK(recovery_route IN ('owner', 'unblock'));
ALTER TABLE asks ADD COLUMN staged_answers TEXT NOT NULL DEFAULT '{}';
CREATE TABLE unblock_cases (
    repo TEXT NOT NULL,
    ticket TEXT NOT NULL,
    source TEXT NOT NULL,
    cause TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    generation INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    phase TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    due REAL NOT NULL DEFAULT 0,
    updated TEXT NOT NULL,
    PRIMARY KEY(repo, ticket)
) STRICT;
CREATE TABLE unblock_attempts (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    ticket TEXT NOT NULL,
    generation INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    observed TEXT NOT NULL,
    launched INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL DEFAULT '{}',
    grant TEXT NOT NULL DEFAULT '{}',
    child TEXT NOT NULL DEFAULT '',
    used TEXT NOT NULL DEFAULT ''
) STRICT;
CREATE INDEX unblock_attempts_ticket ON unblock_attempts(repo, ticket, fingerprint);
CREATE UNIQUE INDEX unblock_attempts_child ON unblock_attempts(child) WHERE child <> '';
CREATE TABLE unblock_queue (
    repo TEXT NOT NULL,
    tracker TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    PRIMARY KEY(repo, tracker)
) STRICT;
CREATE TABLE unblock_notices (
    run TEXT PRIMARY KEY,
    created TEXT NOT NULL
) STRICT;
"""
