from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .. import config, paths, util, workspace
from . import events, runs, writes
from .db import reader

TABLES = {"workspaces": "kept, including removed rows", "workspace_files": "with workspace"}

JSON_FIELDS = ("checkouts", "held_by", "heads", "onto", "retained")
CHANGE_FIELDS = ("state", "kept_reason", "owner_run")
WRITABLE = frozenset((*JSON_FIELDS, *CHANGE_FIELDS, "repo", "ticket", "remove_asked_by", "made_at",
                      "released_at", "removed_at", "bytes", "salt", "sweep_attempts", "last_sweep_at",
                      "last_sweep_error"))


def _decode(row) -> dict | None:
    if row is None:
        return None
    return {**row, **{key: json.loads(row[key]) for key in JSON_FIELDS}}


def _encode(fields: dict) -> dict:
    if unknown := fields.keys() - WRITABLE:
        raise ValueError(f"invalid workspace fields: {', '.join(sorted(unknown))}")
    if "held_by" in fields:
        held = fields["held_by"][:20]
        if any(not isinstance(name, str) or not name or name.startswith("/") or ".." in name.split("/")
               or "\0" in name for name in held):
            raise ValueError("workspace blockers must be relative to the workspace")
        fields = {**fields, "held_by": held}
    return {key: json.dumps(value) if key in JSON_FIELDS else value for key, value in fields.items()}


def get(workspace_id: int) -> dict | None:
    with reader() as c:
        return _decode(c.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone())


def open_for(name: str) -> dict | None:
    with reader() as c:
        return _decode(c.execute("SELECT * FROM workspaces WHERE name = ? AND state != 'removed'", (name,)).fetchone())


def by_state(state: str) -> list[dict]:
    with reader() as c:
        return [_decode(row) for row in c.execute("SELECT * FROM workspaces WHERE state = ? ORDER BY released_at, id",
                                                  (state,))]


def open_rows() -> list[dict]:
    with reader() as c:
        return [_decode(row) for row in c.execute("SELECT * FROM workspaces WHERE state != 'removed' ORDER BY id")]


def snapshot_for(name: str) -> tuple[dict | None, dict[str, str]]:
    with reader() as c:
        row = _decode(c.execute("SELECT * FROM workspaces WHERE name = ? AND state != 'removed'", (name,)).fetchone())
        if row:
            return row, _files(c, row["id"])
        return _decode(c.execute("SELECT * FROM workspaces WHERE name = ? ORDER BY id DESC LIMIT 1",
                                 (name,)).fetchone()), {}


def files(workspace_id: int) -> dict[str, str]:
    with reader() as c:
        return _files(c, workspace_id)


def _files(c, workspace_id: int) -> dict[str, str]:
    return {name.decode(errors="surrogateescape"): digest for name, digest in c.execute(
        "SELECT CAST(name AS BLOB), fingerprint FROM workspace_files WHERE workspace = ?", (workspace_id,))}


def _write_files(c, workspace_id: int, fingerprints: dict[str, str]) -> None:
    c.executemany("INSERT INTO workspace_files (workspace, name, fingerprint) VALUES (?, CAST(? AS TEXT), ?)",
                  ((workspace_id, name.encode(errors="surrogateescape"), digest)
                   for name, digest in fingerprints.items()))


def _event_side(row) -> dict | None:
    return {key: row[key] for key in CHANGE_FIELDS} if row is not None else None


def _emit(before, after) -> None:
    events.emit("workspace.changed", f"workspace:{after['id']}", cls="derived", ver=after["version"],
                data={"old": _event_side(before), "new": _event_side(after)})


def _state_time(fields: dict, previous: str | None = None) -> None:
    target = fields.get("state")
    if target != previous and target in ("released", "removed"):
        key = f"{target}_at"
        if key not in fields:
            fields[key] = util.iso()


def create(name: str, *, files: Mapping[str, str] | None = None, **fields) -> dict:
    return _create(name, _encode(fields), dict(files or {}))


def _create(name: str, fields: dict, fingerprints: dict[str, str], *, backfill: bool = False) -> dict | None:
    values = {"name": name, "state": "live", "made_at": util.iso(), **fields}
    _state_time(values)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with writes.atomic() as c:
        if backfill and c.execute("SELECT 1 FROM workspaces WHERE name = ? LIMIT 1", (name,)).fetchone():
            return None
        row = c.execute(f"INSERT INTO workspaces ({columns}) VALUES ({marks}) RETURNING *",
                        tuple(values.values())).fetchone()
        _write_files(c, row["id"], fingerprints)
        _emit(None, row)
        return _decode(row)


def update(workspace_id: int, *, base_version: int, files: Mapping[str, str] | None = None, **fields) -> dict | None:
    writes.validate_version(base_version)
    values = _encode(fields)
    fingerprints = dict(files) if files is not None else None
    with writes.atomic() as c:
        before = c.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if before is None:
            return None
        if before["version"] != base_version:
            raise writes.VersionConflict(_decode(before))
        if before["state"] == "removed":
            return None
        _state_time(values, before["state"])
        changed = {key: value for key, value in values.items() if value != before[key]}
        if fingerprints is not None and fingerprints != _files(c, workspace_id):
            c.execute("DELETE FROM workspace_files WHERE workspace = ?", (workspace_id,))
            _write_files(c, workspace_id, fingerprints)
        elif not changed:
            return _decode(before)
        setters = ", ".join([*(f"{key} = ?" for key in changed), "version = version + 1"])
        after = c.execute(f"UPDATE workspaces SET {setters} WHERE id = ? RETURNING *",
                          (*changed.values(), workspace_id)).fetchone()
        _emit(before, after)
        return _decode(after)


def legacy_owner(cfg: config.Config, folder: Path, made: dict) -> workspace.WorkspaceOwner:
    if owner := workspace.owner_in(made):
        return owner
    repo = max((r for r in cfg.repos if folder.name.startswith(f"{r.name}-")),
               key=lambda r: len(r.name), default=None)
    if repo is None:
        return workspace.WorkspaceOwner("", "", "", "", [])
    checkouts = [workspace.Checkout(p.dir, p.github, repo.path / p.dir, folder / p.dir, "")
                 for p in workspace.parts_of(repo)]
    return workspace.WorkspaceOwner("", repo.name, folder.name.removeprefix(f"{repo.name}-"), "", checkouts)


def _run_for(owner: workspace.WorkspaceOwner) -> dict:
    if owner.run and (run := runs.run_get(owner.run)):
        return run
    if owner.repo and owner.ticket:
        return next(iter(runs.ticket_runs(owner.repo, owner.ticket, limit=1)), {})
    return {}


def _state_for(status: str) -> tuple[str, str]:
    if status == "running":
        return "live", ""
    if status in ("done", "merged", "rejected"):
        return "released", ""
    return ("kept", status) if status else ("held", "")


def _mtime(path: Path) -> str:
    return util.iso(datetime.fromtimestamp(path.stat().st_mtime, UTC))


def snapshot_fields(made: dict) -> dict:
    return dict(
        salt=workspace.salt_of(made),
        heads={k.removeprefix("\0head "): v for k, v in made.items() if k.startswith("\0head ")},
        onto={k.removeprefix("\0onto "): v for k, v in made.items() if k.startswith("\0onto ")},
        retained=workspace.retained_in(made) or {},
        files={name: digest for name, digest in made.items() if not name.startswith("\0")},
    )


def import_record(cfg: config.Config, source: Path, folder: Path) -> dict | None:
    if source.is_symlink() or not source.is_file():
        return None
    try:
        made = json.loads(source.read_text())
        if not isinstance(made, dict):
            raise ValueError("expected a JSON object")
        recorded_at = _mtime(source)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"{util.iso()} workspace backfill skipped {source}: {util.first_line(str(e), 300)}")
        return None
    owner = legacy_owner(cfg, folder, made)
    run = _run_for(owner)
    state, reason = _state_for(run.get("status") or "")
    made_at = owner.created or run.get("started") or recorded_at
    snapshot = snapshot_fields(made)
    fingerprints = snapshot.pop("files")
    fields = dict(repo=owner.repo, ticket=owner.ticket, owner_run=owner.run, checkouts=owner.to_row()["checkouts"],
                  state=state, kept_reason=reason, made_at=made_at,
                  released_at=(run.get("finished") or run.get("started") or made_at) if state == "released" else "",
                  bytes=workspace.tree_bytes(folder), **snapshot)
    return _create(folder.name, _encode(fields), fingerprints, backfill=True)


def _has_files(folder: Path) -> bool:
    return any(files or any((Path(root) / name).is_symlink() for name in dirs)
               for root, dirs, files in os.walk(folder))


def backfill(cfg: config.Config) -> int:
    with reader() as c:
        known = {row[0] for row in c.execute("SELECT DISTINCT name FROM workspaces")}
    record_dir = paths.STATE_DIR / "workspaces"
    imported = 0
    for source in record_dir.glob("*.json"):
        name = source.name.removesuffix(".json")
        if name in known or name in ("", ".", ".."):
            continue
        if import_record(cfg, source, paths.WORKTREES_DIR / name) is not None:
            imported += 1
            known.add(name)
    for folder in paths.WORKTREES_DIR.iterdir():
        if folder.name in known or folder.is_symlink() or not folder.is_dir():
            continue
        source = workspace.made_record(folder)
        if source.exists() or source.is_symlink() or not _has_files(folder):
            continue
        try:
            made_at = _mtime(folder)
        except FileNotFoundError:
            continue
        fields = dict(state="held", made_at=made_at, bytes=workspace.tree_bytes(folder))
        if _create(folder.name, fields, {}, backfill=True) is not None:
            imported += 1
    return imported
