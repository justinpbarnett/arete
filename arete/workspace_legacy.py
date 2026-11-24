from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from . import paths, util
from .store import workspaces
from . import workspace


OWNER_KEY = "\0owner"


@dataclass
class WorkspaceOwner:
    run: str
    repo: str
    ticket: str
    created: str
    checkouts: list[workspace.Checkout]

    @classmethod
    def from_row(cls, row: dict) -> workspace.WorkspaceOwner:
        checkouts = [workspace.Checkout.from_row({**{k: c[k] for k in workspace.OWNER_CHECKOUT_FIELDS}, "base": ""})
                     for c in row["checkouts"]]
        return cls(row.get("run") or "", row["repo"], str(row["ticket"]), row.get("created") or "", checkouts)

    def to_row(self) -> dict:
        return {"run": self.run, "repo": self.repo, "ticket": self.ticket, "created": self.created,
                "checkouts": self.checkout_rows()}

    def checkout_rows(self) -> list[dict]:
        return [{k: row[k] for k in workspace.OWNER_CHECKOUT_FIELDS} for row in (c.to_row() for c in self.checkouts)]


def remove_workspace(ws: Path, checkouts: list[workspace.Checkout], *, by: str = "",
                     excluded: Collection[Path] = (), edits: bool = False) -> workspace.Release:
    if workspace.termination_pending:
        return workspace.Release("held", ["SIGTERM unwinding"])
    with workspaces.folder_lock(ws):
        options = {"edits": True} if edits else {}
        try:
            snapshot_error = None
            try:
                row = workspaces.release(ws, checkouts, excluded=excluded)
            except workspaces.SnapshotUnavailable as e:
                row = workspaces.release(ws, checkouts, excluded=excluded, reconcile=False)
                snapshot_error = e
            if row:
                excluded = {Path(wt) for wt in row["release_excluded"]}
            if snapshot_error is not None:
                if (not isinstance(snapshot_error, workspaces.UnstatableSnapshot) or by != "sweep"
                        or not row or not row["remove_asked_by"]):
                    raise snapshot_error
                options["check_legacy"] = lambda: workspaces.require_unstatable_record(ws)
            if row and by and by != "sweep" and by == row["owner_run"]:
                workspace.note_made(ws, checkouts, workspaces.owner_of(row), excluded=excluded)
            made = workspaces.made_before(ws) if row and snapshot_error is None else {}
        except workspaces.SnapshotUnavailable as e:
            if by == "sweep" and not isinstance(e, workspaces.UnreadableSnapshot):
                raise
            row = workspaces.open_for(ws.name)
            force = by == "sweep" and bool(row and row["remove_asked_by"])
            names = workspace.workspace_blockers(ws, checkouts, edits, excluded=excluded, made={}, force=force, strict=by == "sweep")
            if by == "sweep" and not names:
                raise
            release = workspace.Release("held", names or [str(ws)])
        else:
            force = by == "sweep" and bool(row and row["remove_asked_by"])
            try:
                release = workspace.remove_checkouts(ws, checkouts, made, excluded=excluded, force=force, **options)
            except OSError as e:
                if row:
                    workspaces.record_release_error(ws, row, e)
                raise
        workspaces.record_release(ws, checkouts, row, release, excluded=excluded)
        return release


def remove_checkouts(ws: Path, checkouts: list[workspace.Checkout], made: dict, *, excluded: Collection[Path],
                     force: bool, check_legacy: Callable[[], None] | None = None, edits: bool = False) -> workspace.Release:
    if names := workspace.workspace_blockers(ws, checkouts, edits, excluded=excluded, made=made, force=force, strict=True):
        return workspace.Release("held", names)
    for index, c in enumerate(checkouts):
        target_last = checkouts[:index] + checkouts[index + 1:] + [c]
        with util.repo_lock(c.src):
            if names := workspace.workspace_blockers(ws, target_last, edits, excluded=excluded, made=made, target=c, force=force,
                                           strict=True):
                return workspace.Release("held", names)
            attached, unsafe = workspace.checkout_attachment(ws, c.wt)
            if unsafe is not None:
                return workspace.Release("held", [str(unsafe)])
            if attached:
                if check_legacy is not None:
                    check_legacy()
                try:
                    workspace.git(["worktree", "remove", "--force", str(c.wt)], c.src)
                except RuntimeError as e:
                    raise OSError(f"{c.wt}: {e}") from e
    if names := workspace.workspace_blockers(ws, checkouts, excluded=excluded, made=made, after_removal=True, force=force):
        return workspace.Release("held", names)
    if excluded:
        return workspace.Release("removed")
    if check_legacy is not None:
        check_legacy()
    if os.path.lexists(ws):
        shutil.rmtree(ws)
    if os.path.lexists(ws):
        return workspace.Release("held", workspace.workspace_blockers(ws, checkouts, made=made, after_removal=True, force=force))
    workspaces.unlink_record(ws)
    for c in checkouts:
        with util.repo_lock(c.src):
            workspace.git(["worktree", "prune"], c.src, check=False)
    return workspace.Release("removed")


def made_record(ws: Path) -> Path:
    return paths.STATE_DIR / "workspaces" / f"{ws.name}.json"


def made_before(ws: Path) -> dict:
    try:
        return workspaces.made_before(ws)
    except workspaces.UnreadableSnapshot:
        return {}
    except workspaces.SnapshotUnavailable as e:
        raise workspace.worktree_failure(f"arete could not reconcile the existing workspace {ws}: {e}", ws, "") from e


def owner_in(made: dict, *, strict: bool = False) -> workspace.WorkspaceOwner | None:
    row = made.get(workspace.OWNER_KEY)
    if isinstance(row, dict):
        try:
            return workspace.WorkspaceOwner.from_row(row)
        except (TypeError, KeyError):
            if strict and row.get("repo") and row.get("checkouts"):
                raise
    return None


def _note_made(ws: Path, checkouts: list[workspace.Checkout], row: dict, owner: workspace.WorkspaceOwner | None,
               preserve: Collection[str], excluded: Collection[Path]) -> None:
    excluded = set(excluded) | {Path(wt) for wt in row["release_excluded"] or ()}
    if excluded:
        checkouts = [c for c in checkouts if c.wt not in excluded]
    owner = owner or workspaces.owner_of(row)
    if not owner.checkouts:
        owner.checkouts = checkouts
    try:
        before = workspaces.made_before(ws, strict=True) if excluded else {}
        salt = workspace.salt_of(before) if excluded else ""
        excluded_records = {n: digest for n, digest in before.items() if salt and not n.startswith("\0")
                            and any((ws / n).is_relative_to(wt) for wt in excluded)}
        names, held = [], []
        if ws.is_dir():
            names, held = workspace.leftovers(ws, checkouts, excluded=excluded), workspace.scratch_left(ws, checkouts)
            names = workspace.outside_scratch(ws, names, held)
            if preserve:
                protected = {Path(n) for n in preserve}
                protected |= {parent for path in protected for parent in path.parents}
                names = [n for n in names if Path(n) not in protected]
        heads = {workspace.head_key(c): workspace.git(["rev-parse", "HEAD"], c.wt).strip() for c in checkouts if (c.wt / ".git").exists()}
        heads |= {workspace.onto_key(c): c.onto for c in checkouts if c.onto and (c.wt / ".git").exists()}
        if names or heads or excluded_records:
            salt = salt if excluded_records else os.urandom(16).hex()
            made = excluded_records | {n: workspace.fingerprint(ws / n, salt) for n in names} | heads | {workspace.SALT_KEY: salt}
            made[workspace.OWNER_KEY] = owner.to_row()
            if held:
                made[workspace.RETAINED_KEY] = workspace.retained(ws, checkouts, names, held)
            workspaces.save_snapshot(ws, row, made, owner)
            if held and not workspace.scratch_left(ws, checkouts):
                for root in made[workspace.RETAINED_KEY]["roots"]:
                    workspace.refresh_released(ws, root)
            return
    except workspaces.SnapshotUnavailable as e:
        workspace.log_snapshot_error(ws, owner.run, e)
        workspaces.clear_snapshot(ws, row, unlink_legacy=isinstance(e, workspaces.UnreadableSnapshot))
        return
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as e:
        workspace.log_snapshot_error(ws, owner.run, e)
    workspaces.clear_snapshot(ws, row)
