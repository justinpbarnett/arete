from __future__ import annotations

PHASE = "backfill"


def apply(conn) -> None:
    from arete.store import tasks

    tasks.backfill_identities(conn)
