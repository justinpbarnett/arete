PHASE = "expand"

SQL = """
CREATE TABLE derived (
    name TEXT PRIMARY KEY,
    code_version INTEGER NOT NULL,
    source_mark TEXT NOT NULL,
    rebuilt TEXT,
    checked TEXT,
    diff_rows INTEGER NOT NULL DEFAULT 0
) STRICT;
"""
