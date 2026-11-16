PHASE = "contract"
SQL = """
ALTER TABLE events DROP COLUMN data;
UPDATE schema_meta SET value = '4' WHERE key = 'oldest_writer';
"""
