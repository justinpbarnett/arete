PHASE = "expand"

SQL = """
ALTER TABLE workspaces ADD COLUMN legacy_mtime_ns INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workspaces ADD COLUMN legacy_owner TEXT NOT NULL DEFAULT 'null'
    CHECK (json_type(legacy_owner) IN ('object', 'null'));
"""
