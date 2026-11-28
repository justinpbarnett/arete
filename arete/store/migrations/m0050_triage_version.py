from __future__ import annotations

PHASE = "expand"


def apply(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(triage)").fetchall()}
    if "version" not in cols:
        conn.execute("ALTER TABLE triage ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
