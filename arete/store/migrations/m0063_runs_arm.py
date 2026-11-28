PHASE = "expand"

SQL = """
ALTER TABLE runs ADD COLUMN arm TEXT;
CREATE INDEX runs_arm ON runs (arm) WHERE arm IS NOT NULL;
"""
