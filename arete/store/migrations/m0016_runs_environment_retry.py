PHASE = "expand"

SQL = """
ALTER TABLE runs ADD COLUMN environment_retry_of TEXT;
CREATE UNIQUE INDEX runs_environment_retry_once ON runs (environment_retry_of)
    WHERE environment_retry_of IS NOT NULL;
"""
