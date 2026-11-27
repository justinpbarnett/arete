from . import workspace
from .store import workspaces


def list() -> list[dict]:
    return [_workspace_item(row) for row in workspaces.by_state("held")]


def _workspace_item(row: dict) -> dict:
    entry = workspace.held_summary_row(row)
    if not row["last_sweep_error"]:
        entry["why"] = "workspace removal blocked"
        if row["held_by"]:
            entry["why"] += f" ({', '.join(row['held_by'][:5])})"
    entry["names"] = entry["names"][:10]
    return {"kind": "workspace", "id": row["id"], "repo": row["repo"], "ticket": row["ticket"], **entry}
