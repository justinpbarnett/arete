PHASE = "expand"

SQL = """
ALTER TABLE secrets ADD COLUMN expose_rules TEXT;
"""
