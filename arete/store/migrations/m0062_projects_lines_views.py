from __future__ import annotations

PHASE = "expand"


def apply(conn) -> None:
    from arete.store import projects

    projects.apply_migration(conn)
