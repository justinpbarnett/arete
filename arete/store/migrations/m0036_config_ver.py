PHASE = "expand"
SQL = """
ALTER TABLE config_index ADD COLUMN ver INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX config_ver ON config_index (kind, name, ver) WHERE ver > 0;
"""
