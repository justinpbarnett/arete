from __future__ import annotations

PHASE = "backfill"


def apply(conn) -> None:
    from arete import sources

    sources.backfill_conn(conn)
