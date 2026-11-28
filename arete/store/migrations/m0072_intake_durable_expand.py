from __future__ import annotations

PHASE = "expand"


def apply(conn) -> None:
    from arete.store import intake

    intake.apply_expand(conn)
