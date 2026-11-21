PHASE = "expand"

SQL = """
CREATE TABLE failed_finishes (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    ticket_pending INTEGER NOT NULL DEFAULT 1,
    comment TEXT NOT NULL,
    prs TEXT NOT NULL
);
CREATE INDEX failed_finishes_pending ON failed_finishes (run_id)
    WHERE ticket_pending = 1 OR comment != '' OR prs != '{}';
"""
