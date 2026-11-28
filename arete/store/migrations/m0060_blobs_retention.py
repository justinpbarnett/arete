PHASE = "expand"

SQL = """
ALTER TABLE blobs ADD COLUMN purged_at TEXT;
ALTER TABLE blobs ADD COLUMN purge_reason TEXT;

CREATE INDEX blobs_purged ON blobs (purged_at) WHERE purged_at IS NOT NULL;
"""
