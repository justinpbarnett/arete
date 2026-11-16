PHASE = "expand"
SQL = """
ALTER TABLE schema_meta RENAME TO schema_meta_old;
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT CHECK(value IS NOT NULL OR key = 'oldest_writer')) STRICT;
INSERT INTO schema_meta SELECT * FROM schema_meta_old;
DROP TABLE schema_meta_old;
INSERT INTO schema_meta VALUES ('trim_derived', '0'), ('trim_notice', '0');
CREATE TABLE events_log (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
 subject TEXT NOT NULL, ver INTEGER, task TEXT, class TEXT NOT NULL,
 shape INTEGER NOT NULL, actor TEXT NOT NULL, cause_seq INTEGER, idem TEXT, data TEXT NOT NULL
);
CREATE INDEX events_subject_ver ON events_log(subject, ver);
CREATE INDEX events_task ON events_log(task);
CREATE INDEX events_kind ON events_log(kind);
CREATE TABLE principals (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, role TEXT, name TEXT NOT NULL,
 status TEXT NOT NULL, external TEXT, version INTEGER NOT NULL DEFAULT 1,
 created TEXT NOT NULL, removed_at TEXT,
 CHECK ((kind = 'human') = (role IS NOT NULL))
);
CREATE TABLE principal_logins (
 id TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES principals(id),
 method TEXT NOT NULL, subject TEXT NOT NULL, public_key TEXT, expires TEXT,
 created TEXT NOT NULL, last_used TEXT, UNIQUE(method, subject)
);
CREATE TRIGGER principals_gate BEFORE INSERT ON principals
WHEN NEW.kind = 'human' AND NEW.status = 'active'
 AND COALESCE((SELECT value FROM schema_meta WHERE key = 'multi_user'), '') != 'ready'
 AND EXISTS(SELECT 1 FROM principals WHERE kind = 'human' AND status = 'active')
BEGIN SELECT RAISE(ABORT, 'a second person needs multi_user = ready'); END;
CREATE TRIGGER principals_gate_update BEFORE UPDATE OF kind, status ON principals
WHEN NEW.kind = 'human' AND NEW.status = 'active'
 AND COALESCE((SELECT value FROM schema_meta WHERE key = 'multi_user'), '') != 'ready'
 AND EXISTS(SELECT 1 FROM principals WHERE kind = 'human' AND status = 'active' AND id != OLD.id)
BEGIN SELECT RAISE(ABORT, 'a second person needs multi_user = ready'); END;
"""
