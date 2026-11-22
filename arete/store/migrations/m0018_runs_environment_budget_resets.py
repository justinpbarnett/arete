PHASE = "expand"

SQL = """
CREATE TABLE environment_budget_resets (
    repo TEXT NOT NULL,
    issue TEXT NOT NULL,
    gap_repo TEXT NOT NULL,
    gap_issue INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (repo, issue, gap_repo, gap_issue)
);
"""
