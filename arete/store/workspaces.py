from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Collection, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from .. import config, paths, util, workspace
from . import events, runs, writes
from .db import migrate_on_demand, reader

TABLES = {"workspaces": "kept, including removed rows", "workspace_files": "with workspace"}

JSON_FIELDS = ("checkouts", "held_by", "heads", "onto", "retained", "legacy_owner", "release_excluded")
CHANGE_FIELDS = ("state", "kept_reason", "owner_run")
WRITABLE = frozenset((*JSON_FIELDS, *CHANGE_FIELDS, "repo", "ticket", "remove_asked_by", "made_at",
                      "released_at", "removed_at", "bytes", "salt", "sweep_attempts", "last_sweep_at",
                      "last_sweep_error", "legacy_mtime_ns"))
FOLDER_LOCKS: ContextVar[frozenset[Path]] = ContextVar("workspace_folder_locks", default=frozenset())
TICKET_LOCKS: ContextVar[frozenset[str]] = ContextVar("workspace_ticket_locks", default=frozenset())
SWEEP_STATES = ("live", "released", "held", "kept")
NO_LEGACY_RECORD = -1
LEGACY_RESUME = "legacy resume"


class SnapshotUnavailable(RuntimeError):
    pass


class UnreadableSnapshot(SnapshotUnavailable):
    pass


class UnstatableSnapshot(UnreadableSnapshot):
    pass


@contextmanager
def ticket_lock(repo: str, ticket: str | int):
    name = runs.run_lock("pipeline", repo, ticket)
    held = TICKET_LOCKS.get()
    if name in held:
        yield True
        return
    with util.lock(name) as got:
        token = TICKET_LOCKS.set(held | {name}) if got else None
        try:
            yield got
        finally:
            if token is not None:
                TICKET_LOCKS.reset(token)


@contextmanager
def folder_lock(ws: Path, *, wait: bool = True):
    ws = ws.absolute()
    held = FOLDER_LOCKS.get()
    if ws in held:
        yield True
        return
    key = sha256(os.fsencode(ws)).hexdigest()
    with util.lock(f"workspace-{key}", wait=wait) as got:
        token = FOLDER_LOCKS.set(held | {ws}) if got else None
        try:
            yield got
        finally:
            if token is not None:
                FOLDER_LOCKS.reset(token)


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
    migrate_on_demand()
    with reader() as c:
        return _decode(c.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone())


def open_for(name: str) -> dict | None:
    migrate_on_demand()
    with reader() as c:
        return _decode(c.execute("SELECT * FROM workspaces WHERE name = ? AND state != 'removed'", (name,)).fetchone())


def by_state(*states: str) -> list[dict]:
    migrate_on_demand()
    with reader() as c:
        marks = ", ".join("?" for _ in states)
        return [_decode(row) for row in c.execute(
            f"SELECT * FROM workspaces WHERE state IN ({marks}) ORDER BY released_at, id", states)]


def files(workspace_id: int) -> dict[str, str]:
    migrate_on_demand()
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
    migrate_on_demand()
    return _create(name, _encode(fields), dict(files or {}))


def _create(name: str, fields: dict, fingerprints: dict[str, str]) -> dict:
    values = {"name": name, "state": "live", "made_at": util.iso(), "legacy_mtime_ns": NO_LEGACY_RECORD, **fields}
    _state_time(values)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with writes.atomic() as c:
        row = c.execute(f"INSERT INTO workspaces ({columns}) VALUES ({marks}) RETURNING *",
                        tuple(values.values())).fetchone()
        _write_files(c, row["id"], fingerprints)
        _emit(None, row)
        return _decode(row)


def update(workspace_id: int, *, base_version: int, files: Mapping[str, str] | None = None, **fields) -> dict | None:
    migrate_on_demand()
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


def change(row: dict, **fields) -> dict | None:
    return update(row["id"], base_version=row["version"], **fields)


def _change_current(row: dict, **fields) -> dict | None:
    with writes.atomic():
        if current := get(row["id"]):
            return change(current, **fields)
    return None


def claim(ws: Path, repo: str, ticket: str, owner_run: str, *,
          checkouts: list[workspace.Checkout] | None = None) -> int:
    migrate_on_demand()
    with folder_lock(ws), writes.atomic():
        row = open_for(ws.name)
        fields = dict(repo=repo, ticket=str(ticket), owner_run=owner_run, state="live",
                      kept_reason="", held_by=[], released_at="", release_excluded=None,
                      remove_asked_by="", last_sweep_error="")
        if checkouts is not None:
            fields["checkouts"] = workspace.WorkspaceOwner("", "", "", "", checkouts).checkout_rows()
        return (change(row, **fields) if row else create(ws.name, **fields))["id"]


def owner_of(row: dict) -> workspace.WorkspaceOwner:
    return workspace.WorkspaceOwner.from_row({
        "run": row["owner_run"], "repo": row["repo"], "ticket": row["ticket"],
        "created": row["made_at"], "checkouts": row["checkouts"],
    })


def keep(ws: Path, status: str) -> None:
    migrate_on_demand()
    with folder_lock(ws):
        if (row := open_for(ws.name)) and row["state"] == "live":
            _change_current(row, state="kept", kept_reason=status, bytes=workspace.tree_bytes(ws))


def legacy_owner(cfg: config.Config, folder: Path, made: dict, *, require_history: bool = False) -> workspace.WorkspaceOwner:
    if owner := workspace.owner_in(made):
        return owner
    for repo in sorted(cfg.repos, key=lambda r: len(r.name), reverse=True):
        ticket = folder.name.removeprefix(f"{repo.name}-")
        if ticket == folder.name or (require_history and not runs.ticket_runs(repo.name, ticket, limit=1)):
            continue
        checkouts = [workspace.Checkout(p.dir, p.github, repo.path / p.dir, folder / p.dir, "")
                     for p in workspace.parts_of(repo)]
        return workspace.WorkspaceOwner("", repo.name, ticket, "", checkouts)
    return workspace.WorkspaceOwner("", "", "", "", [])


def _run_for(owner: workspace.WorkspaceOwner) -> dict:
    if owner.run and (run := runs.run_get(owner.run)):
        return run
    if owner.repo and owner.ticket:
        return next(iter(runs.ticket_runs(owner.repo, owner.ticket, limit=1, whole=True)), {})
    return {}


def _state_for(status: str) -> tuple[str, str]:
    if status == "running":
        return "live", ""
    return ("kept", status) if status else ("held", "")


def snapshot_fields(made: dict) -> dict:
    return dict(
        salt=workspace.salt_of(made),
        heads={k.removeprefix("\0head "): v for k, v in made.items() if k.startswith("\0head ")},
        onto={k.removeprefix("\0onto "): v for k, v in made.items() if k.startswith("\0onto ")},
        retained=workspace.retained_in(made) or {},
        legacy_owner=made.get(workspace.OWNER_KEY) if isinstance(made.get(workspace.OWNER_KEY), dict) else None,
        files={name: digest for name, digest in made.items() if not name.startswith("\0")},
    )


def _snapshot(row: dict) -> dict:
    made = files(row["id"])
    made.update({f"\0head {prefix}": head for prefix, head in row["heads"].items()})
    made.update({f"\0onto {prefix}": onto for prefix, onto in row["onto"].items()})
    if row["salt"]:
        made[workspace.SALT_KEY] = row["salt"]
    if row["retained"]:
        made[workspace.RETAINED_KEY] = row["retained"]
    if row["legacy_owner"] is not None:
        made[workspace.OWNER_KEY] = row["legacy_owner"]
    return made


def made_before(ws: Path, *, strict: bool = False) -> dict:
    migrate_on_demand()
    with folder_lock(ws):
        row, _, _ = _reconcile(ws, strict=strict)
        with reader():
            if row:
                return _snapshot(row)
    return {}


def _release_selection(checkouts: list[workspace.Checkout], excluded: Collection[Path]) -> dict:
    owner = workspace.WorkspaceOwner("", "", "", "", checkouts)
    return dict(checkouts=owner.checkout_rows(), release_excluded=sorted(map(str, excluded)))


def release(ws: Path, checkouts: list[workspace.Checkout] | None = None, *,
            excluded: Collection[Path] = (), reconcile: bool = True) -> dict | None:
    with folder_lock(ws):
        row = open_for(ws.name)
        if row and reconcile:
            row, _, _ = _reconcile(ws)
        if row:
            selection = {}
            if checkouts is not None:
                selected = {c.wt for c in checkouts}
                omitted = {c.wt for c in owner_of(row).checkouts
                           if selected and c.wt not in selected and os.path.lexists(c.wt)}
                earlier = {Path(wt) for wt in row["release_excluded"] or ()}
                selection = _release_selection(checkouts, set(excluded) | omitted | (earlier - selected))
            return _change_current(row, state="released", kept_reason="",
                                   legacy_mtime_ns=row["legacy_mtime_ns"] or NO_LEGACY_RECORD, **selection)
    return None


def unlink_record(ws: Path) -> None:
    source = workspace.made_record(ws)
    source.unlink(missing_ok=True)
    with suppress(OSError):
        source.parent.rmdir()


def save_snapshot(ws: Path, row: dict, made: dict, owner: workspace.WorkspaceOwner | None = None) -> None:
    with writes.atomic():
        if (current := get(row["id"])) and current["state"] != "removed":
            _save_snapshot(ws, current, made, owner)


def clear_snapshot(ws: Path, row: dict, *, unlink_legacy: bool = True) -> None:
    with writes.atomic():
        if (current := get(row["id"])) and current["state"] != "removed":
            modified = current["legacy_mtime_ns"]
            if unlink_legacy:
                try:
                    unlink_record(ws)
                except OSError:
                    with suppress(SnapshotUnavailable):
                        if st := _record_stat(workspace.made_record(ws)):
                            modified = max(modified, st.st_mtime_ns)
            change(current, legacy_mtime_ns=modified, **snapshot_fields({}))


def _save_snapshot(ws: Path, row: dict, made: dict, owner: workspace.WorkspaceOwner | None = None,
                   *, before_replace=None) -> None:
    fields = snapshot_fields(made)
    if owner is not None:
        fields.update(repo=owner.repo, ticket=owner.ticket, owner_run=owner.run)
        if row["release_excluded"] is None:
            fields["checkouts"] = owner.checkout_rows()
    if made:
        source = workspace.made_record(ws)

        def record_mtime(draft: Path) -> None:
            fields["legacy_mtime_ns"] = draft.stat().st_mtime_ns

        util.write_durably(source, json.dumps(made, indent=2).encode(), 0o600, check=record_mtime,
                           before_replace=before_replace)
    else:
        unlink_record(ws)
    change(row, **fields)


def _refresh_released(ws: Path, root: str, row: dict, version: tuple[int, ...] | None) -> None:
    roots = row["retained"].get("roots")
    if not isinstance(roots, list) or root not in roots:
        return
    with writes.atomic():
        current = get(row["id"])
        if not current or current["state"] == "removed":
            return
        if current["version"] != row["version"]:
            raise SnapshotUnavailable(f"{ws}: workspace row changed during scratch finalization")
        made = _snapshot(current)
        held = made.pop(workspace.RETAINED_KEY)
        for name in held.get("names") or []:
            if isinstance(name, str) and name in made:
                try:
                    made[name] = workspace.fingerprint(ws / name, row["salt"])
                except OSError:
                    made.pop(name)
        source = workspace.made_record(ws)
        _save_snapshot(ws, current, made, before_replace=lambda: _require_record_version(source, version))


def _record_version(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_ctime_ns, st.st_mtime_ns


def _record_stat(source: Path) -> os.stat_result | None:
    try:
        return source.lstat()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise UnreadableSnapshot(f"{source}: {e}") from e


def _require_record_version(source: Path, version: tuple[int, ...] | None) -> None:
    current = _record_stat(source)
    if (None if current is None else _record_version(current)) != version:
        raise SnapshotUnavailable(f"{source}: legacy workspace record changed")


def require_unstatable_record(ws: Path) -> None:
    source = workspace.made_record(ws)
    try:
        _record_stat(source)
    except UnreadableSnapshot:
        return
    raise SnapshotUnavailable(f"{source}: legacy workspace record can now be checked")


def _read_record(source: Path, *, strict: bool = False) -> tuple[dict | None, int, tuple[int, ...]] | None:
    try:
        before = _record_stat(source)
    except UnreadableSnapshot as e:
        raise UnstatableSnapshot(str(e)) from e
    if before is None:
        return None
    version = _record_version(before)
    try:
        if not stat.S_ISREG(before.st_mode):
            raise UnreadableSnapshot(f"{source}: expected a regular file")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if _record_version(os.fstat(fd)) != version:
                raise ValueError("file changed before reading")
            try:
                made = json.load(f)
            except (ValueError, RecursionError):
                made = None
            if _record_version(os.fstat(fd)) != version:
                raise ValueError("file changed while reading")
        _require_record_version(source, version)
    except OSError as e:
        _require_record_version(source, version)
        if strict:
            raise UnreadableSnapshot(f"{source}: {e}") from e
        made = None
    except ValueError as e:
        raise SnapshotUnavailable(f"{source}: {e}") from e
    if not isinstance(made, dict) or any(
        not name or name.startswith("/") or ".." in name.split("/") or "\0" in name or not isinstance(digest, str)
        for name, digest in made.items() if not name.startswith("\0")
    ):
        made = None
    return made, before.st_mtime_ns, version


def _reconcile(ws: Path, cfg: config.Config | None = None, *,
               strict: bool = False, untracked_only: bool = False) -> tuple[dict | None, tuple[int, ...] | None, bool]:
    with reader() as c:
        row = _decode(c.execute("SELECT * FROM workspaces WHERE name = ? "
                                "ORDER BY state = 'removed', id DESC LIMIT 1", (ws.name,)).fetchone())
    if untracked_only and (row is not None or not ws.is_dir() or ws.is_symlink()):
        return row, None, False
    source = workspace.made_record(ws)
    recorded = _read_record(source, strict=strict)
    version = recorded[2] if recorded else None
    if recorded is None:
        _require_record_version(source, None)
    current = _reconcile_record(ws, row, recorded, cfg)
    return current, version, current is not None and current != row


def import_legacy(ws: Path) -> tuple[dict | None, bool]:
    migrate_on_demand()
    with folder_lock(ws):
        row, _, imported = _reconcile(ws, untracked_only=True)
        return row, imported


def _legacy_takeover(row: dict, owner: workspace.WorkspaceOwner | None) -> dict | None:
    if not owner or not owner.run or owner.run == row["owner_run"]:
        return None
    if row["owner_run"]:
        if later := runs.later_run(owner.repo, owner.ticket, row["owner_run"], candidate=owner.run):
            return later
        if runs.run_get(row["owner_run"]):
            return None
    run = runs.run_get(owner.run)
    if run and (run["repo"], str(run["issue"])) == (owner.repo, owner.ticket) and run["started"] >= row["released_at"]:
        return run
    return None


def _has_attached_onto(ws: Path, checkouts: list[workspace.Checkout], onto: dict, *, allow_root: bool) -> bool:
    if not checkouts:
        return bool(allow_root and onto.get("") and workspace.checkout_attachment(ws, ws)[0])
    return any(onto.get(c.prefix) and workspace.checkout_attachment(ws, c.wt)[0] for c in checkouts)


def _reconcile_record(ws: Path, row: dict | None, recorded: tuple[dict | None, int, tuple[int, ...]] | None,
                      cfg: config.Config | None) -> dict | None:
    active = row if row and row["state"] != "removed" else None
    if (active and active["state"] == "released" and active["legacy_mtime_ns"] == 0
            and not active["remove_asked_by"] and active["kept_reason"] != "operator"):
        status = _run_for(owner_of(active)).get("status") or ""
        active = change(active, state="kept", kept_reason=status, released_at="")
    if recorded is None:
        if active and active["legacy_mtime_ns"] > 0:
            return change(active, **snapshot_fields({}))
        return active
    made, modified, _ = recorded
    invalid = made is None
    if invalid:
        if active:
            return change(active, legacy_mtime_ns=max(modified, active["legacy_mtime_ns"]), **snapshot_fields({}))
        if cfg is None or not ws.is_dir() or ws.is_symlink():
            return None
        made = {}
    if row and modified <= row["legacy_mtime_ns"]:
        if active and modified < row["legacy_mtime_ns"] and made != _snapshot(active):
            raise SnapshotUnavailable(f"{ws}: legacy workspace record is older than its recorded version")
        return active
    if row and not active:
        removed = util.parse_iso(row["removed_at"]).timestamp()
        if modified // 1_000_000_000 < removed or not ws.is_dir() or ws.is_symlink():
            return None
    snapshot = snapshot_fields(made)
    owner = workspace.owner_in(made)
    has_selection = active is not None and active["release_excluded"] is not None
    takeover = _legacy_takeover(active, owner) if has_selection else None
    preserve_selection = has_selection and not takeover
    if active and active["state"] == "released" and (
        snapshot["heads"] != active["heads"] or snapshot["onto"] != active["onto"]
        or snapshot["files"] != files(active["id"])
    ):
        owner = owner or owner_of(active)
        selection = owner_of(active) if preserve_selection else owner
        excluded = active["release_excluded"] if preserve_selection else None
        reason = LEGACY_RESUME
        if _has_attached_onto(ws, selection.checkouts, snapshot["onto"], allow_root=not excluded):
            reason = (takeover or _run_for(owner)).get("status") or ""
        return change(active, legacy_mtime_ns=modified, state="kept", released_at="",
                      release_excluded=excluded,
                      remove_asked_by="", held_by=[], last_sweep_error="", kept_reason=reason,
                      repo=owner.repo, ticket=owner.ticket, owner_run=owner.run, checkouts=selection.checkout_rows(),
                      **snapshot)
    if preserve_selection:
        return change(active, legacy_mtime_ns=modified, **snapshot)
    owner = owner or legacy_owner(cfg or config.load(), ws, made, require_history=True)
    identity = dict(repo=owner.repo, ticket=owner.ticket, owner_run=owner.run, checkouts=owner.checkout_rows())
    if not active:
        return _import_new(ws, owner, modified, identity, snapshot) if owner.ticket or not invalid else None
    if not snapshot["legacy_owner"] and active["owner_run"]:
        identity = {}
    if takeover:
        identity.update(released_at="", release_excluded=None, remove_asked_by="", held_by=[], last_sweep_error="")
    if any(active[key] != identity[key] for key in ("repo", "ticket", "owner_run") if key in identity):
        state, reason = _state_for((takeover or _run_for(owner)).get("status") or "")
        identity.update(state=state, kept_reason=reason)
    return change(active, legacy_mtime_ns=modified, **identity, **snapshot)


@contextmanager
def _background_lock(ws: Path, cfg: config.Config | None = None):
    row = open_for(ws.name)
    if row:
        repo, ticket = row["repo"], row["ticket"]
    elif recorded := _read_record(workspace.made_record(ws)):
        owner = legacy_owner(cfg or config.load(), ws, recorded[0] or {}, require_history=True)
        repo, ticket = owner.repo, owner.ticket
    else:
        yield None
        return
    with ticket_lock(repo, ticket) as ticket_held:
        if not ticket_held:
            yield None
            return
        with folder_lock(ws, wait=False) as got:
            current = open_for(ws.name) if got else None
            matches = current is None or (current["repo"], current["ticket"]) == (repo, ticket)
            yield (repo, ticket) if got and matches else None


def refresh_released(ws: Path, root: str) -> bool:
    with _background_lock(ws) as identity:
        if identity is None:
            return open_for(ws.name) is None and _record_stat(workspace.made_record(ws)) is None
        row, version, _ = _reconcile(ws)
        if row:
            if (row["repo"], row["ticket"]) != identity:
                return False
            _refresh_released(ws, root, row, version)
        return True


def _import_new(folder: Path, owner: workspace.WorkspaceOwner, modified: int, identity: dict, snapshot: dict) -> dict:
    run = _run_for(owner)
    state, reason = _state_for(run.get("status") or "")
    made_at = owner.created or run.get("started") or util.iso(datetime.fromtimestamp(modified / 1e9, UTC))
    return create(folder.name, state=state, kept_reason=reason, made_at=made_at,
                  bytes=0 if folder.is_symlink() else workspace.tree_bytes(folder),
                  legacy_mtime_ns=modified, **identity, **snapshot)


def backfill(cfg: config.Config, *, new_only: bool = False) -> int:
    migrate_on_demand()
    record_dir = paths.STATE_DIR / "workspaces"
    names = {p.name.removesuffix(".json") for p in record_dir.glob("*.json")}
    if not new_only:
        with reader() as c:
            names.update(r[0] for r in c.execute("SELECT name FROM workspaces "
                                                "WHERE state != 'removed' AND legacy_mtime_ns > 0"))
    imported = 0
    for name in sorted(names - {"", ".", ".."}):
        ws = paths.WORKTREES_DIR / name
        with _isolated_sweep(ws), _background_lock(ws, cfg) as identity:
            if identity is not None and (not new_only or not open_for(name)):
                _, _, changed = _reconcile(ws, cfg)
                imported += changed
    return imported


def blockers(ws: Path, names: list[str]) -> list[str]:
    relative = []
    for name in names:
        path = Path(name)
        if path.is_absolute():
            name = path.relative_to(ws.absolute()).as_posix() if path.is_relative_to(ws.absolute()) else path.name
        if name and ".." not in Path(name).parts and "\0" not in name:
            relative.append(name)
    return list(dict.fromkeys(relative))[:20]


def record_release(ws: Path, checkouts: list[workspace.Checkout], row: dict | None,
                   result: workspace.Release, *, excluded: Collection[Path]) -> None:
    size = workspace.tree_bytes(ws) if result.state == "held" or excluded else 0
    with writes.atomic():
        row = get(row["id"]) if row else None
        if result.state == "held":
            fields = dict(state="held", held_by=blockers(ws, result.held_by), bytes=size)
            if row:
                change(row, **fields)
            else:
                create(ws.name, **_release_selection(checkouts, excluded), **fields)
        elif row and excluded:
            change(row, state="kept", kept_reason="operator", held_by=[], bytes=size, last_sweep_error="")
        elif row:
            change(row, state="removed", held_by=[], bytes=0, last_sweep_error="")


def record_release_error(ws: Path, row: dict, error: OSError) -> None:
    _change_current(row, bytes=workspace.tree_bytes(ws), last_sweep_error=util.first_line(str(error), 300))


def _ticket_ended(row: dict) -> bool:
    ticket = row["ticket"]
    latest = next(iter(runs.ticket_runs(row["repo"], ticket, limit=1, whole=True)), {}) if ticket else {}
    status = latest.get("status")
    return (status in workspace.ENDED or
            (status not in workspace.WAITING and workspace.ticket_closed(ticket)))


@contextmanager
def _isolated_sweep(ws: Path):
    try:
        yield
    except Exception as e:
        print(f"{util.iso()} sweeping {ws}: {type(e).__name__}: {util.first_line(str(e), 300)}")


def _sweep_attempt(row: dict, error: str = "", **fields) -> dict | None:
    return _change_current(row, sweep_attempts=row["sweep_attempts"] + 1, last_sweep_at=util.iso(),
                           last_sweep_error=error, **fields)


def _owner_status(row: dict) -> str:
    owner = runs.run_get(row["owner_run"]) if row["owner_run"] else None
    return (owner or {}).get("status") or ""


def _sweep_row(ws: Path, row: dict, *, missing: bool = False) -> None:
    if row["state"] == "live":
        status = _owner_status(row)
        if status == "running":
            return
        row = _change_current(row, state="kept", kept_reason=status, bytes=workspace.tree_bytes(ws))
        if row is None:
            return
    if missing:
        removed_through = max(row["legacy_mtime_ns"], time.time_ns())
        with suppress(UnreadableSnapshot):
            if source := _record_stat(workspace.made_record(ws)):
                removed_through = max(removed_through, source.st_mtime_ns)
        _change_current(row, state="removed", held_by=[], bytes=0, last_sweep_error="",
                        legacy_mtime_ns=removed_through)
        unlink_record(ws)
        return
    if row["kept_reason"] == "operator":
        return
    checkouts = owner_of(row).checkouts
    excluded = {Path(wt) for wt in row["release_excluded"] or ()}
    may_remove = row["kept_reason"] != LEGACY_RESUME or row["remove_asked_by"]
    remove = may_remove and (
        row["state"] == "released" or (
            not _has_attached_onto(ws, checkouts, row["onto"], allow_root=not excluded) and _ticket_ended(row)
        )
    )
    if not remove:
        size = workspace.tree_bytes(ws)
        if row["last_sweep_error"]:
            _sweep_attempt(row, bytes=size)
        elif size != row["bytes"]:
            change(row, bytes=size)
        return
    if _sweep_attempt(row) is None:
        return
    try:
        if not checkouts and not excluded:
            checkouts = workspace.registered_root_checkout(ws)
        workspace.clear_workspace(ws, checkouts, checkouts[0].src if checkouts else ws, [], by="sweep",
                                  excluded=excluded)
    except workspace.RunFailed as e:
        if isinstance(e.__cause__, SnapshotUnavailable):
            _change_current(row, last_sweep_error=util.first_line(str(e), 300))
            return
        names = e.held_by
        if names is None:
            names = workspace.workspace_blockers(ws, checkouts, edits=True, excluded=excluded)
        _change_current(row, state="held", held_by=blockers(ws, names), bytes=workspace.tree_bytes(ws),
                        last_sweep_error=util.first_line(str(e), 300))
    except (OSError, RuntimeError, sqlite3.Error) as e:
        _change_current(row, bytes=workspace.tree_bytes(ws), last_sweep_error=util.first_line(str(e), 300))


def _sweep_owned(cfg: config.Config, ws: Path, row: dict) -> None:
    repo, ticket = row["repo"], row["ticket"]
    with ticket_lock(repo, ticket) as ticket_held:
        if not ticket_held or runs.issue_running(repo, ticket):
            return
        with folder_lock(ws, wait=False) as got:
            current = open_for(ws.name) if got else None
            if not current or current["id"] != row["id"] or (current["repo"], current["ticket"]) != (repo, ticket):
                return
            if current["state"] == "live" and _owner_status(current) == "running":
                return
            try:
                try:
                    ws.lstat()
                except FileNotFoundError:
                    _sweep_row(ws, current, missing=True)
                    return
                try:
                    reconciled = _reconcile(ws, cfg)[0]
                except UnstatableSnapshot:
                    if not current["remove_asked_by"]:
                        raise
                    reconciled = current
                if (reconciled and reconciled["id"] == current["id"]
                        and (reconciled["repo"], reconciled["ticket"]) == (repo, ticket)):
                    _sweep_row(ws, reconciled)
            except Exception as e:
                latest = get(current["id"])
                if latest and latest["state"] in SWEEP_STATES and latest["kept_reason"] != "operator":
                    error = util.first_line(str(e), 300) or type(e).__name__
                    if latest["state"] == "live":
                        if (status := _owner_status(latest)) != "running":
                            _sweep_attempt(latest, error, state="kept", kept_reason=status)
                    elif latest["sweep_attempts"] == current["sweep_attempts"]:
                        _sweep_attempt(latest, error)
                    else:
                        change(latest, last_sweep_error=error)
                raise


def _sweep_unowned(cfg: config.Config, ws: Path, cutoff: float) -> None:
    if open_for(ws.name) or not ws.is_dir() or ws.is_symlink():
        return
    owner = legacy_owner(cfg, ws, {})
    with ticket_lock(owner.repo, owner.ticket) as ticket_held, folder_lock(ws, wait=False) as got:
        if not ticket_held or not got or open_for(ws.name):
            return
        try:
            modified = ws.stat().st_mtime
        except FileNotFoundError:
            return
        if modified >= cutoff:
            return
        source = workspace.made_record(ws)
        try:
            recorded = _read_record(source)
        except UnreadableSnapshot:
            if names := workspace.unexamined(ws, set()):
                _record_orphan(ws, owner.repo, modified, names)
            return
        if recorded and (recorded[0] is not None or legacy_owner(cfg, ws, {}, require_history=True).ticket):
            return
        version = recorded[2] if recorded else None
        if names := workspace.unexamined(ws, set()):
            _require_record_version(source, version)
            _record_orphan(ws, owner.repo, modified, names)
        elif not workspace.scratch_left(ws, []) and not workspace.unexamined(ws, set()):
            _require_record_version(source, version)
            shutil.rmtree(ws, ignore_errors=True)


def _record_orphan(ws: Path, repo: str, modified: float, names: list[str]) -> None:
    create(ws.name, repo=repo, state="held", held_by=names, bytes=workspace.tree_bytes(ws),
           made_at=util.iso(datetime.fromtimestamp(modified, UTC)))


def sweep(cfg: config.Config) -> None:
    try:
        backfill(cfg, new_only=True)
        for row in by_state(*SWEEP_STATES):
            ws = paths.WORKTREES_DIR / row["name"]
            with _isolated_sweep(ws):
                _sweep_owned(cfg, ws, row)
        cutoff = util.now().timestamp() - 7 * 86400
        for folder in paths.WORKTREES_DIR.glob("*"):
            with _isolated_sweep(folder):
                _sweep_unowned(cfg, folder, cutoff)
    finally:
        workspace.prune_repos(cfg)


def held_entry(row: dict) -> dict:
    run_id = row["owner_run"]
    run = runs.run_get(run_id) if run_id else None
    run = run or {}
    status = run.get("status") or row["kept_reason"]
    why = row["last_sweep_error"]
    blocked = row["state"] == "held" or bool(why)
    if not why:
        if blocked:
            why = "workspace removal blocked"
            if row["held_by"]:
                why += f" ({', '.join(row['held_by'][:5])})"
        elif row["onto"] or row["kept_reason"] == LEGACY_RESUME:
            why = "the workspace holds edits and waits for a resume"
        else:
            why = f"run {run_id} ended {status or 'unknown'}, so the workspace waits for the ticket"
    return dict(name=row["name"], path=str(paths.WORKTREES_DIR / row["name"]), repo=row["repo"], ticket=row["ticket"],
                run=run_id, status=status, since=run.get("finished") or run.get("started") or row["made_at"],
                bytes=row["bytes"], blocked=blocked, why=why, names=row["held_by"][:10])
