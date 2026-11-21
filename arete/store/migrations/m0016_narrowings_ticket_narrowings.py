PHASE = "expand"

SQL = """
CREATE TABLE ticket_narrowings (
    run_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    issue TEXT NOT NULL,
    plan TEXT NOT NULL,
    phase TEXT NOT NULL,
    child INTEGER
);
CREATE INDEX ticket_narrowings_ticket ON ticket_narrowings (project, issue);
CREATE UNIQUE INDEX ticket_narrowings_pending ON ticket_narrowings (project, issue)
    WHERE phase NOT IN ('complete', 'cancelled');
"""
