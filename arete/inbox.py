from .store import workspaces


def list() -> list[dict]:
    return [{"kind": "workspace", "id": row["id"], **workspaces.held_entry(row)}
            for row in workspaces.by_state("held")]
