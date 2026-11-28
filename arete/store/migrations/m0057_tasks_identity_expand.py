from __future__ import annotations

PHASE = "expand"

SQL = """
CREATE TABLE refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK (system IN ('github', 'jira', 'slack', 'email', 'meeting', 'intake')),
    kind TEXT NOT NULL CHECK (kind IN ('issue', 'ticket', 'pr', 'thread', 'email', 'transcript', 'paste')),
    key TEXT NOT NULL,
    connector TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    ext_state TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    remote_updated TEXT,
    fetched_at TEXT,
    sync TEXT NOT NULL DEFAULT 'local' CHECK (sync IN ('local', 'synced', 'ahead', 'behind', 'conflict', 'off')),
    version INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    UNIQUE (system, key)
) STRICT;

CREATE TABLE task_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    ref_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'related' CHECK (role IN ('primary', 'origin', 'related', 'parent')),
    interpretation TEXT NOT NULL DEFAULT '',
    claims TEXT NOT NULL DEFAULT '{}',
    anchor TEXT NOT NULL DEFAULT '[]',
    source_rev TEXT NOT NULL DEFAULT '',
    linked_by TEXT NOT NULL DEFAULT '',
    confidence REAL,
    version INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    UNIQUE (task_id, ref_id)
) STRICT;
CREATE INDEX task_refs_ref ON task_refs (ref_id, task_id);
CREATE UNIQUE INDEX task_refs_one_primary ON task_refs (task_id) WHERE role = 'primary';
CREATE UNIQUE INDEX task_refs_primary_once ON task_refs (ref_id) WHERE role = 'primary';

CREATE TABLE task_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('waits_on', 'blocks', 'relates', 'duplicate')),
    other_task INTEGER,
    other_key TEXT,
    origin TEXT NOT NULL DEFAULT 'person' CHECK (origin IN ('person', 'agent', 'body', 'jira_link')),
    created TEXT NOT NULL,
    CHECK ((other_task IS NOT NULL AND other_key IS NULL) OR (other_task IS NULL AND other_key IS NOT NULL))
) STRICT;
CREATE INDEX task_links_task ON task_links (task_id, kind);

CREATE TABLE task_labels (
    task INTEGER NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (task, label)
) STRICT;
CREATE INDEX task_labels_by_label ON task_labels (label, task);

ALTER TABLE tasks ADD COLUMN num INTEGER;
ALTER TABLE tasks ADD COLUMN project TEXT;
ALTER TABLE tasks ADD COLUMN line TEXT;
ALTER TABLE tasks ADD COLUMN fault INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN fault_detail TEXT;
ALTER TABLE tasks ADD COLUMN priority INTEGER;
ALTER TABLE tasks ADD COLUMN rank REAL;
ALTER TABLE tasks ADD COLUMN owner TEXT;
ALTER TABLE tasks ADD COLUMN origin TEXT;
ALTER TABLE tasks ADD COLUMN delegate TEXT;
ALTER TABLE tasks ADD COLUMN estimate TEXT;
ALTER TABLE tasks ADD COLUMN answer TEXT;

ALTER TABLE task_comments ADD COLUMN num INTEGER;
ALTER TABLE task_comments ADD COLUMN audience TEXT;
ALTER TABLE task_comments ADD COLUMN ref INTEGER;
ALTER TABLE task_comments ADD COLUMN remote_id TEXT;
ALTER TABLE task_comments ADD COLUMN outbox_id INTEGER;
ALTER TABLE task_comments ADD COLUMN created TEXT;
ALTER TABLE task_comments ADD COLUMN updated TEXT;

UPDATE tasks SET num = rowid WHERE num IS NULL;
UPDATE task_comments SET num = rowid, created = COALESCE(created, ts), updated = COALESCE(updated, ts) WHERE num IS NULL;

CREATE UNIQUE INDEX tasks_num ON tasks (num);
CREATE UNIQUE INDEX task_comments_num ON task_comments (num);

ALTER TABLE runs ADD COLUMN task_id INTEGER;
ALTER TABLE runs ADD COLUMN issue_key TEXT;
ALTER TABLE runs ADD COLUMN capabilities TEXT;
CREATE INDEX runs_task ON runs (task_id, kind, started);

ALTER TABLE asks ADD COLUMN task_id INTEGER;
ALTER TABLE feedback ADD COLUMN task_id INTEGER;
ALTER TABLE mined ADD COLUMN task_id INTEGER;
ALTER TABLE passed_over ADD COLUMN task_id INTEGER;
ALTER TABLE triage ADD COLUMN ref_id INTEGER;
ALTER TABLE research ADD COLUMN ref_id INTEGER;
ALTER TABLE triage_approvals ADD COLUMN ref_id INTEGER;

CREATE TRIGGER tasks_num_fill AFTER INSERT ON tasks
WHEN NEW.num IS NULL
BEGIN
    UPDATE tasks SET num = COALESCE((SELECT MAX(num) FROM tasks), 0) + 1
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER task_comments_num_fill AFTER INSERT ON task_comments
WHEN NEW.num IS NULL
BEGIN
    UPDATE task_comments
    SET num = COALESCE((SELECT MAX(num) FROM task_comments), 0) + 1,
        created = COALESCE(created, ts),
        updated = COALESCE(updated, ts)
    WHERE rowid = NEW.rowid;
END;
"""
