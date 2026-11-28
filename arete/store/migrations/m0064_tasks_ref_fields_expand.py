from __future__ import annotations

PHASE = "expand"

SQL = """
CREATE TABLE ref_fields (
    ref INTEGER NOT NULL,
    field TEXT NOT NULL,
    base TEXT,
    remote TEXT,
    remote_at TEXT,
    remote_by TEXT,
    owner TEXT NOT NULL,
    conflict INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ref, field)
) STRICT;

ALTER TABLE refs ADD COLUMN remote_actor TEXT;
"""
