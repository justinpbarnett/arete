PHASE = "expand"

SQL = """
ALTER TABLE runs ADD COLUMN failure TEXT;
CREATE TABLE environment_requeues (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    repo TEXT NOT NULL,
    issue TEXT NOT NULL,
    attempted TEXT,
    requeued INTEGER NOT NULL DEFAULT 0,
    cap_notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX environment_requeues_window ON environment_requeues (repo, issue, attempted);
"""
