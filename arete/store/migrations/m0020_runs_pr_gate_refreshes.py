PHASE = "expand"

SQL = """
CREATE TABLE pr_gate_refreshes (
    repo TEXT NOT NULL,
    issue TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    head TEXT NOT NULL,
    evidence TEXT NOT NULL,
    PRIMARY KEY (repo, issue, pr_url)
);
"""
