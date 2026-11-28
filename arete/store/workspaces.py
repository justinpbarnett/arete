from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Collection, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from .. import config, paths, util, workspace
from . import asks, events, runs, writes
from .db import reader, require_workspace_contract

TABLES = {"workspaces": "kept, including removed rows", "workspace_files": "with workspace"}

JSON_FIELDS = ("checkouts", "held_by", "heads", "onto", "retained", "release_excluded")
CHANGE_FIELDS = ("state", "kept_reason", "owner_run")
WRITABLE = frozenset((*JSON_FIELDS, *CHANGE_FIELDS, "repo", "ticket", "remove_asked_by", "made_at",
                      "released_at", "removed_at", "bytes", "salt", "sweep_attempts", "last_sweep_at",
                      "last_sweep_error"))
FOLDER_LOCKS: ContextVar[frozenset[Path]] = ContextVar("workspace_folder_locks", default=frozenset())
TICKET_LOCKS: ContextVar[frozenset[str]] = ContextVar("workspace_ticket_locks", default=frozenset())
SWEEP_STATES = ("live", "released", "held", "kept")
LEGACY_RESUME = "legacy resume"
LEGACY_WAITING = "imported waiting run"
SNAPSHOT_UNAVAILABLE = "legacy workspace snapshot quarantined"


class SnapshotUnavailable(OSError):
    pass


@contextmanager
def ticket_lock(repo: str, ticket: str | int, *, task_id: int | None = None):
    from contextlib import ExitStack
    from . import tasks as task_store

    resolved = task_id
    if resolved is None:
        try:
            with reader() as c:
                resolved = task_store.TaskStore.find_task_num(c, repo, ticket)
        except Exception:
            resolved = None
    contracted = task_store.is_contracted()
    if resolved is not None:
        new_name = runs.run_lock("pipeline", task=int(resolved))
        old_name = runs.run_lock("pipeline", repo, ticket)
        names = (new_name,) if contracted else (old_name, new_name)
    else:
        names = (runs.run_lock("pipeline", repo, ticket),)
    held = TICKET_LOCKS.get()
    needed = [n for n in names if n not in held]
    if not needed:
        yield True
        return
    with ExitStack() as stack:
        acquired = []
        for n in needed:
            got = stack.enter_context(util.lock(n))
            if not got:
                yield False
                return
            acquired.append(n)
        token = TICKET_LOCKS.set(held | set(acquired))
        try:
            yield True
        finally:
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
    require_workspace_contract()
    with reader() as c:
        return _decode(c.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone())


def open_for(name: str) -> dict | None:
    require_workspace_contract()
    with reader() as c:
        return _decode(c.execute("SELECT * FROM workspaces WHERE name = ? AND state != 'removed'", (name,)).fetchone())


def retained_rows(c) -> list[dict]:
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'workspaces' AND type = 'table'").fetchone():
        return []
    return [{**r, "held_by": json.loads(r["held_by"])} for r in c.execute(
        "SELECT name, owner_run, state, kept_reason, made_at, bytes, last_sweep_error, held_by "
        "FROM workspaces WHERE state IN ('kept', 'held') ORDER BY released_at, id")]


def by_state(*states: str) -> list[dict]:
    require_workspace_contract()
    with reader() as c:
        marks = ", ".join("?" for _ in states)
        return [_decode(row) for row in c.execute(
            f"SELECT * FROM workspaces WHERE state IN ({marks}) ORDER BY released_at, id", states)]  # noqa: S608


def files(workspace_id: int) -> dict[str, str]:
    require_workspace_contract()
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


def _emit(c, before, after) -> None:
    events.emit("workspace.changed", f"workspace:{after['id']}", cls="derived", ver=after["version"],
                data={"old": _event_side(before), "new": _event_side(after)}, connection=c)


def _state_time(fields: dict, previous: str | None = None) -> None:
    target = fields.get("state")
    if target != previous and target in ("released", "removed"):
        key = f"{target}_at"
        if key not in fields:
            fields[key] = util.iso()


def create(name: str, *, files: Mapping[str, str] | None = None, **fields) -> dict:
    require_workspace_contract()
    with writes.atomic() as c:
        return _insert(c, name, _encode(fields), dict(files or {}))


def _insert(c, name: str, fields: dict, fingerprints: dict[str, str]) -> dict:
    values = {"name": name, "state": "live", "made_at": util.iso(), **fields}
    _state_time(values)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    row = c.execute(f"INSERT INTO workspaces ({columns}) VALUES ({marks}) RETURNING *",  # noqa: S608
                    tuple(values.values())).fetchone()
    _write_files(c, row["id"], fingerprints)
    _emit(c, None, row)
    if row["state"] == "held":
        from .. import inbox

        inbox.sync(c, "workspaces")
    return _decode(row)


def update(workspace_id: int, *, base_version: int, files: Mapping[str, str] | None = None, **fields) -> dict | None:
    require_workspace_contract()
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
        return _update(c, before, values, fingerprints)


def _update(c, before, values: dict, fingerprints: dict[str, str] | None) -> dict:
    workspace_id = before["id"]
    _state_time(values, before["state"])
    changed = {key: value for key, value in values.items() if value != before[key]}
    if fingerprints is not None and fingerprints != _files(c, workspace_id):
        c.execute("DELETE FROM workspace_files WHERE workspace = ?", (workspace_id,))
        _write_files(c, workspace_id, fingerprints)
    elif not changed:
        return _decode(before)
    setters = ", ".join([*(f"{key} = ?" for key in changed), "version = version + 1"])
    after = c.execute(f"UPDATE workspaces SET {setters} WHERE id = ? RETURNING *",  # noqa: S608
                      (*changed.values(), workspace_id)).fetchone()
    _emit(c, before, after)
    if before["state"] == "held" or after["state"] == "held":
        from .. import inbox

        inbox.sync(c, "workspaces")
    return _decode(after)


def change(row: dict, **fields) -> dict | None:
    return update(row["id"], base_version=row["version"], **fields)


def _change_current(row: dict, **fields) -> dict | None:
    require_workspace_contract()
    with writes.atomic():
        if current := get(row["id"]):
            return change(current, **fields)
    return None


def claim(ws: Path, repo: str, ticket: str, owner_run: str, *,
          checkouts: list[workspace.Checkout] | None = None) -> int:
    require_workspace_contract()
    with folder_lock(ws), writes.atomic():
        row = open_for(ws.name)
        if row and row["kept_reason"] == SNAPSHOT_UNAVAILABLE:
            raise workspace.worktree_failure(SNAPSHOT_UNAVAILABLE, ws, "")
        fields = {"repo": repo, "ticket": str(ticket), "owner_run": owner_run, "state": "live",
                      "kept_reason": "", "held_by": [], "released_at": "", "release_excluded": None,
                      "remove_asked_by": "", "last_sweep_error": ""}
        if checkouts is not None:
            fields["checkouts"] = workspace.WorkspaceOwner("", "", "", checkouts).checkout_rows()
        return (change(row, **fields) if row else create(ws.name, **fields))["id"]


def owner_of(row: dict) -> workspace.WorkspaceOwner:
    checkouts = [workspace.Checkout.from_row({**{k: c[k] for k in workspace.OWNER_CHECKOUT_FIELDS}, "base": ""})
                 for c in row["checkouts"]]
    return workspace.WorkspaceOwner(row["owner_run"], row["repo"], row["ticket"], checkouts)


def keep(ws: Path, status: str) -> None:
    require_workspace_contract()
    with folder_lock(ws):
        if (row := open_for(ws.name)) and row["state"] == "live":
            _change_current(row, state="kept", kept_reason=status, bytes=workspace.tree_bytes(ws))


def _repo_for(cfg: config.Config, ws: Path) -> tuple[str, str]:
    for repo in sorted(cfg.repos, key=lambda r: len(r.name), reverse=True):
        ticket = ws.name.removeprefix(f"{repo.name}-")
        if ticket != ws.name:
            return repo.name, ticket
    return "", ""


def snapshot_fields(made: dict) -> dict:
    return {
        "salt": workspace.salt_of(made),
        "heads": {k.removeprefix("\0head "): v for k, v in made.items() if k.startswith("\0head ")},
        "onto": {k.removeprefix("\0onto "): v for k, v in made.items() if k.startswith("\0onto ")},
        "retained": workspace.retained_in(made) or {},
        "files": {name: digest for name, digest in made.items() if not name.startswith("\0")},
    }


def _snapshot(row: dict) -> dict:
    made = files(row["id"])
    made.update({f"\0head {prefix}": head for prefix, head in row["heads"].items()})
    made.update({f"\0onto {prefix}": onto for prefix, onto in row["onto"].items()})
    if row["salt"]:
        made[workspace.SALT_KEY] = row["salt"]
    if row["retained"]:
        made[workspace.RETAINED_KEY] = row["retained"]
    return made


def made_before(ws: Path) -> dict:
    require_workspace_contract()
    with reader():
        row = open_for(ws.name)
        return _snapshot(row) if row and row["kept_reason"] != SNAPSHOT_UNAVAILABLE else {}


def retire_quarantine(name: str) -> None:
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise ValueError("supply a workspace name from Health")
    row = open_for(name)
    if not row or row["state"] != "held" or row["kept_reason"] != SNAPSHOT_UNAVAILABLE:
        raise ValueError(f"{name} has no quarantined workspace")
    ws = paths.WORKTREES_DIR / name
    with ticket_lock(row["repo"], row["ticket"]) as ticket_held:
        if not ticket_held:
            raise ValueError(f"{name} is in use")
        with folder_lock(ws, wait=False) as locked:
            if not locked:
                raise ValueError(f"{name} is in use")
            with writes.atomic():
                current = open_for(name)
                if current != row or runs.issue_running(row["repo"], row["ticket"]):
                    raise ValueError(f"{name} changed or is in use")
                with paths.directory_fd(paths.WORKTREES_DIR) as parent:
                    try:
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        change(row, state="removed", bytes=0)
                    else:
                        raise ValueError(f"preserve the workspace outside {ws} before retiring its quarantine")


def retry_quarantine(name: str, folder: Path) -> None:
    from .migrations import m0028_workspaces_contract as handoff

    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise ValueError("supply a workspace name from Health")
    row = open_for(name)
    if not row or row["state"] != "held" or row["kept_reason"] != SNAPSHOT_UNAVAILABLE:
        raise ValueError(f"{name} has no quarantined workspace")
    ws = paths.WORKTREES_DIR / name
    repo, ticket = row["repo"], row["ticket"]
    if not repo or not ticket:
        with handoff.recovery_snapshot(folder, ws) as (owner, _, _):
            repo, ticket = owner.repo, owner.ticket
            if not repo or not ticket:
                raise ValueError(f"{name} snapshot has no recorded ticket")
    with ticket_lock(repo, ticket) as ticket_held:
        if not ticket_held:
            raise ValueError(f"{name} is in use")
        with folder_lock(ws, wait=False) as locked:
            if not locked:
                raise ValueError(f"{name} is in use")
            with writes.atomic() as conn, handoff.recovery_snapshot(folder, ws) as (owner, fields, modified):
                if open_for(name) != row or runs.issue_running(repo, ticket):
                    raise ValueError(f"{name} changed or is in use")
                if ws.is_symlink() or not ws.is_dir():
                    raise ValueError(f"{name} or its snapshot is missing")
                run = runs.run_get(owner.run) if owner.run else None
                if (not run or (owner.repo, owner.ticket) != (repo, ticket)
                        or (run["repo"], str(run["issue"])) != (owner.repo, owner.ticket)
                        or any(row[key] and row[key] != value for key, value in
                               (("repo", owner.repo), ("ticket", owner.ticket), ("owner_run", owner.run)))):
                    raise ValueError(f"{name} snapshot owner does not match its workspace")
                removed = conn.execute("SELECT * FROM workspaces WHERE name = ? AND state = 'removed' "
                                       "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
                if removed and (modified // 1_000_000_000 < util.parse_iso(removed["removed_at"]).timestamp()
                                or not handoff.newer_owner(removed, owner, run, lambda: nullcontext(conn))):
                    raise ValueError(f"{name} has a removed predecessor; preserve and retire it instead")
                fields.update(handoff.keep_fields())
                fields.update(repo=owner.repo, ticket=owner.ticket, owner_run=owner.run)
                if not row["checkouts"]:
                    fields["checkouts"] = owner.checkout_rows()
                change(row, **fields)


def _release_selection(checkouts: list[workspace.Checkout], excluded: Collection[Path]) -> dict:
    owner = workspace.WorkspaceOwner("", "", "", checkouts)
    return {"checkouts": owner.checkout_rows(), "release_excluded": sorted(map(str, excluded))}


def release(ws: Path, checkouts: list[workspace.Checkout] | None = None, *,
            excluded: Collection[Path] = ()) -> dict | None:
    with folder_lock(ws):
        row = open_for(ws.name)
        if row:
            if row["kept_reason"] == SNAPSHOT_UNAVAILABLE:
                raise SnapshotUnavailable(SNAPSHOT_UNAVAILABLE)
            selection = {}
            if checkouts is not None:
                selected = {c.wt for c in checkouts}
                omitted = {c.wt for c in owner_of(row).checkouts
                           if selected and c.wt not in selected and os.path.lexists(c.wt)}
                earlier = {Path(wt) for wt in row["release_excluded"] or ()}
                selection = _release_selection(checkouts, set(excluded) | omitted | (earlier - selected))
            reason = LEGACY_WAITING if row["kept_reason"] == LEGACY_WAITING else ""
            return _change_current(row, state="released", kept_reason=reason, **selection)
    return None


def save_snapshot(row: dict, made: dict, owner: workspace.WorkspaceOwner | None = None) -> None:
    require_workspace_contract()
    fields = snapshot_fields(made)
    with writes.atomic():
        current = get(row["id"])
        if not current or current["state"] == "removed" or current["kept_reason"] == SNAPSHOT_UNAVAILABLE:
            return
        if owner is not None:
            fields.update(repo=owner.repo, ticket=owner.ticket, owner_run=owner.run)
            if current["release_excluded"] is None:
                fields["checkouts"] = owner.checkout_rows()
        change(current, **fields)


def refresh_released(ws: Path, root: str) -> bool:
    row = open_for(ws.name)
    if not row:
        return True
    with ticket_lock(row["repo"], row["ticket"]) as ticket_held:
        if not ticket_held:
            return False
        with folder_lock(ws, wait=False) as got:
            if not got:
                return False
            return _refresh_released(ws, root, row)


def _refresh_released(ws: Path, root: str, row: dict) -> bool:
    with writes.atomic():
        current = open_for(ws.name)
        if current is None:
            return True
        if (current["kept_reason"] == SNAPSHOT_UNAVAILABLE
                or (current["id"], current["repo"], current["ticket"]) != (row["id"], row["repo"], row["ticket"])):
            return False
        held = current["retained"]
        roots = held.get("roots")
        if not isinstance(roots, list) or root not in roots:
            return True
        made = _snapshot(current)
        made.pop(workspace.RETAINED_KEY)
        for name in held.get("names") or ():
            if isinstance(name, str) and name in made:
                try:
                    made[name] = workspace.fingerprint(ws / name, current["salt"])
                except OSError:
                    made.pop(name)
        change(current, **snapshot_fields(made))
        return True


def _has_attached_onto(ws: Path, checkouts: list[workspace.Checkout], onto: dict, *, allow_root: bool) -> bool:
    if not checkouts:
        return bool(allow_root and onto.get("") and workspace.checkout_attachment(ws, ws)[0])
    return any(onto.get(c.prefix) and workspace.checkout_attachment(ws, c.wt)[0] for c in checkouts)


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
            fields = {"state": "held", "held_by": blockers(ws, result.held_by), "bytes": size}
            if row:
                change(row, **fields)
            else:
                create(ws.name, **_release_selection(checkouts, excluded), **fields)
        elif row and excluded:
            change(row, state="kept", kept_reason="operator", held_by=[], bytes=size, last_sweep_error="")
        elif row:
            change(row, state="removed", kept_reason="", held_by=[], bytes=0, last_sweep_error="")


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


def remove_imported_waiting(ws: Path, row: dict) -> workspace.Release:
    checkouts = owner_of(row).checkouts
    excluded = {Path(wt) for wt in row["release_excluded"] or ()}
    return workspace.remove_workspace(ws, checkouts, excluded=excluded, edits=True)


def _waiting_ask_retired(row: dict) -> bool:
    latest = runs.newest_whole_run(row["repo"], row["ticket"])
    if not latest or latest["id"] != row["owner_run"] or latest["status"] not in ("needs-info", "stopped"):
        return False
    history = asks.asks_of(latest["id"])
    return (any(a["status"] in ("stopped", "expired") for a in history)
            and not any(a["status"] == "open" or (a["status"] == "answered" and not a["taken_by"]) for a in history))


def _sweep_row(ws: Path, row: dict, *, missing: bool = False) -> None:
    if row["kept_reason"] == SNAPSHOT_UNAVAILABLE:
        return
    if row["state"] == "live":
        status = _owner_status(row)
        if status == "running":
            return
        row = _change_current(row, state="kept", kept_reason=status, bytes=workspace.tree_bytes(ws))
        if row is None:
            return
    if missing:
        _change_current(row, state="removed", held_by=[], bytes=0, last_sweep_error="")
        return
    if row["kept_reason"] == "operator":
        return
    if row["kept_reason"] == LEGACY_WAITING:
        if _waiting_ask_retired(row) and _sweep_attempt(row) is not None:
            remove_imported_waiting(ws, row)
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
        names = e.held_by
        if names is None:
            names = workspace.workspace_blockers(ws, checkouts, edits=True, excluded=excluded)
        _change_current(row, state="held", held_by=blockers(ws, names), bytes=workspace.tree_bytes(ws),
                        last_sweep_error=util.first_line(str(e), 300))
    except (OSError, RuntimeError, sqlite3.Error) as e:
        _change_current(row, bytes=workspace.tree_bytes(ws), last_sweep_error=util.first_line(str(e), 300))


def _sweep_owned(ws: Path, row: dict) -> None:
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
                _sweep_row(ws, current)
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
    repo, ticket = _repo_for(cfg, ws)
    with ticket_lock(repo, ticket) as ticket_held, folder_lock(ws, wait=False) as got:
        if not ticket_held or not got or open_for(ws.name):
            return
        try:
            modified = ws.stat().st_mtime
        except FileNotFoundError:
            return
        if modified >= cutoff:
            return
        names = workspace.unexamined(ws, set())
        if not names and workspace.scratch_left(ws, []):
            return
        if names:
            create(ws.name, repo=repo, state="held", held_by=names, bytes=workspace.tree_bytes(ws),
                   made_at=util.iso(datetime.fromtimestamp(modified, UTC)))
            return
        for _, children, _, parent in os.fwalk(ws, topdown=False):
            for child in children:
                os.rmdir(child, dir_fd=parent)
        ws.rmdir()


def sweep(cfg: config.Config) -> None:
    require_workspace_contract()
    try:
        for row in by_state(*SWEEP_STATES):
            ws = paths.WORKTREES_DIR / row["name"]
            with _isolated_sweep(ws):
                _sweep_owned(ws, row)
        cutoff = util.now().timestamp() - 7 * 86400
        for folder in paths.WORKTREES_DIR.glob("*"):
            with _isolated_sweep(folder):
                _sweep_unowned(cfg, folder, cutoff)
    finally:
        workspace.prune_repos(cfg)
