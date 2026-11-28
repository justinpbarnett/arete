PHASE = "expand"

SQL = """
CREATE TABLE inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    line TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 2,
    title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    verbs TEXT NOT NULL DEFAULT '[]',
    options TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'open',
    snooze_until TEXT NOT NULL DEFAULT '',
    snooze_watch TEXT NOT NULL DEFAULT '',
    snooze_watch_ver INTEGER NOT NULL DEFAULT 0,
    shelve_reason TEXT NOT NULL DEFAULT '',
    shelve_until TEXT NOT NULL DEFAULT '',
    shelves INTEGER NOT NULL DEFAULT 0,
    answer TEXT NOT NULL DEFAULT '',
    answered_by TEXT NOT NULL DEFAULT '',
    answered_at TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT '',
    reminded TEXT NOT NULL DEFAULT '',
    raised TEXT NOT NULL,
    resolved TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX inbox_one_open ON inbox (kind, subject) WHERE state != 'resolved';
CREATE INDEX inbox_by_state ON inbox (state, priority, id);
CREATE INDEX inbox_by_subject ON inbox (subject, id);

CREATE TABLE pending_commands (
    actor TEXT NOT NULL,
    key TEXT NOT NULL,
    request TEXT NOT NULL,
    expires TEXT NOT NULL,
    PRIMARY KEY (actor, key)
);

ALTER TABLE push_subscriptions ADD COLUMN failures INTEGER NOT NULL DEFAULT 0;
"""
