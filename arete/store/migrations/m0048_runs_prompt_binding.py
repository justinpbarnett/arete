PHASE = "expand"

SQL = """
ALTER TABLE steps ADD COLUMN prompt_version TEXT;
ALTER TABLE steps ADD COLUMN prompt_level TEXT;
ALTER TABLE steps ADD COLUMN schema_version TEXT;

CREATE INDEX steps_prompt ON steps (prompt_version, ts);
"""
