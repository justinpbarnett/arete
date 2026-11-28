from __future__ import annotations

PHASE = "expand"


def apply(conn) -> None:
    from arete.store import sources

    sources.apply_expand(conn)
