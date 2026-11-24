import json
import os
import stat
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

PHASE = "contract"


class InvalidSnapshot(ValueError):
    pass


class MalformedSnapshot(InvalidSnapshot):
    pass


def signature(st):
    return st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_ctime_ns, st.st_mtime_ns


def unique_keys(pairs):
    made = dict(pairs)
    if len(made) != len(pairs):
        raise ValueError("duplicate workspace snapshot key")
    return made


def read_snapshot(folder, fd, name, before):
    path = folder / name
    if not stat.S_ISREG(before.st_mode):
        raise InvalidSnapshot(f"{path}: expected a regular workspace snapshot")
    version = signature(before)
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(source, "rb") as src:
        if signature(os.fstat(source)) != version:
            raise InvalidSnapshot(f"{path}: workspace snapshot changed before handoff")
        invalid = False
        try:
            made = json.load(src, object_pairs_hook=unique_keys)
        except (ValueError, RecursionError):
            made, invalid = None, True
        if (signature(os.fstat(source)) != version
                or signature(os.stat(name, dir_fd=fd, follow_symlinks=False)) != version):
            raise InvalidSnapshot(f"{path}: workspace snapshot changed during handoff")
    if invalid:
        raise MalformedSnapshot(f"{path}: invalid workspace snapshot")
    if not isinstance(made, dict) or any(not valid_field(name, value) for name, value in made.items()):
        raise MalformedSnapshot(f"{path}: invalid workspace snapshot fields")
    return made


def text_field(value, errors="strict"):
    if not isinstance(value, str) or "\0" in value:
        return False
    try:
        value.encode(errors=errors)
    except UnicodeEncodeError:
        return False
    return True


def relative_name(name):
    return text_field(name, "surrogateescape") and bool(name) and not name.startswith("/") and (
        ".." not in name.split("/"))


def valid_field(name, value):
    if name == "\0owner":
        return isinstance(value, dict)
    if name == "\0retained":
        return (isinstance(value, dict) and set(value) == {"roots", "names"}
                and all(isinstance(value[k], list) and all(relative_name(n) for n in value[k])
                        for k in ("roots", "names")))
    if name == "\0salt":
        return text_field(value)
    if name.startswith(("\0head ", "\0onto ")):
        prefix = name[6:]
        return (not prefix or relative_name(prefix)) and text_field(value)
    return relative_name(name) and text_field(value)


def recorded_owner(path, ws, row):
    from arete import util, workspace

    try:
        run, repo, ticket, created = row.get("run", ""), row["repo"], row["ticket"], row.get("created", "")
        if (not all(text_field(s) for s in (run, repo, ticket, created))
                or (repo and not ws.name.startswith(f"{repo}-")) or (ticket and f"{repo}-{ticket}" != ws.name)
                or not isinstance(row["checkouts"], list)):
            raise ValueError("invalid owner")
        if created and util.parse_iso(created).tzinfo is None:
            raise ValueError("invalid owner timestamp")
        checkouts = [recorded_checkout(ws, c) for c in row["checkouts"]]
        if len({c.wt for c in checkouts}) != len(checkouts):
            raise ValueError("duplicate checkout")
    except (KeyError, TypeError, ValueError) as e:
        raise InvalidSnapshot(f"{path}: invalid workspace snapshot owner") from e
    return workspace.WorkspaceOwner(run, repo, ticket, checkouts)


def recorded_checkout(ws, row):
    from arete import workspace

    data = {k: row[k] for k in workspace.OWNER_CHECKOUT_FIELDS}
    if not all(text_field(value) for value in data.values()):
        raise ValueError("invalid checkout fields")
    c = workspace.Checkout.from_row({**data, "base": ""})
    if ((c.dir and not relative_name(c.dir)) or ".." in c.src.parts or not c.src.is_absolute()
            or c.wt != ws.absolute() / c.dir):
        raise ValueError("invalid checkout path")
    return c


def inferred_owner(ws, connect):
    from arete import config, workspace
    from arete.store import runs

    for repo in sorted(config.load_or_problem()[0].repos, key=lambda r: len(r.name), reverse=True):
        ticket = ws.name.removeprefix(f"{repo.name}-")
        if ticket != ws.name and (history := runs.ticket_runs(repo.name, ticket, limit=1, connect=connect, whole=True)):
            checkouts = [workspace.Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir, "")
                         for p in workspace.parts_of(repo)]
            return workspace.WorkspaceOwner(history[0]["id"], repo.name, ticket, checkouts)
    return workspace.WorkspaceOwner("", "", "", [])


def newer_owner(row, owner, run, connect):
    from arete.store import runs

    if not run or owner.run == row["owner_run"] or (run["repo"], str(run["issue"])) != (owner.repo, owner.ticket):
        return False
    previous = row["owner_run"]
    if previous and runs.run_get(previous, connect=connect):
        return bool(runs.later_run(owner.repo, owner.ticket, previous, connect=connect, candidate=owner.run))
    return run["started"] >= (row["removed_at"] or row["released_at"])


def snapshot_precedes_removal(row, modified):
    from arete import util

    return (modified <= row["legacy_mtime_ns"]
            or modified // 1_000_000_000 < util.parse_iso(row["removed_at"]).timestamp())


def quarantine(conn, ws, row, error):
    from arete import config, util, workspace
    from arete.store import workspaces

    reason = workspaces.SNAPSHOT_UNAVAILABLE
    detail = util.first_line(str(error), 300)
    print(f"workspace {ws.name} held: {detail}", flush=True)
    fields = dict(state="held", kept_reason=reason, last_sweep_error=detail, remove_asked_by="", released_at="")
    if row:
        update(conn, row, fields)
    else:
        repo, ticket = workspaces._repo_for(config.load_or_problem()[0], ws)
        workspaces._insert(conn, ws.name, workspaces._encode(
            dict(repo=repo, ticket=ticket, bytes=workspace.tree_bytes(ws), **fields)), {})


def handoff_workspace(conn, ws, folder, fd, names):
    row = current_workspace(conn, ws)
    name = f"{ws.name}.json"
    if name not in names:
        if row:
            fields = {}
            if row["legacy_mtime_ns"] > 0:
                from arete.store import workspaces

                fields.update(workspaces.snapshot_fields({}))
            if row["kept_reason"] != "operator" and row["state"] == "released" and row["legacy_mtime_ns"] == 0:
                fields.update(keep_fields())
            if fields:
                update(conn, row, fields)
        return
    try:
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        made = read_snapshot(folder, fd, name, before)
        import_snapshot(conn, ws, row, folder / name, before, made)
    except MalformedSnapshot:
        if row:
            from arete.store import workspaces

            fields = workspaces.snapshot_fields({})
            if row["state"] == "released" and row["kept_reason"] != "operator":
                fields.update(keep_fields())
            update(conn, row, fields)
        return signature(before)
    except (OSError, InvalidSnapshot) as e:
        quarantine(conn, ws, row, e)
        return None
    return signature(before)


def current_workspace(conn, ws):
    from arete.store import workspaces

    return workspaces._decode(conn.execute("SELECT * FROM workspaces WHERE name = ? AND state != 'removed'",
                                           (ws.name,)).fetchone())


def import_snapshot(conn, ws, row, path, before, made):
    from arete import util, workspace
    from arete.store import runs, workspaces

    modified = before.st_mtime_ns
    removed = conn.execute("SELECT * FROM workspaces WHERE name = ? AND state = 'removed' "
                           "ORDER BY id DESC LIMIT 1", (ws.name,)).fetchone()
    def connect():
        return nullcontext(conn)

    owner_data = made.get("\0owner")
    owner = (recorded_owner(path, ws, owner_data) if owner_data is not None
             else workspaces.owner_of(row) if row else inferred_owner(ws, connect))
    run = (runs.run_get(owner.run, connect=connect) or {}) if owner.run else {}
    if run and owner.ticket and (run["repo"], str(run["issue"])) != (owner.repo, owner.ticket):
        raise InvalidSnapshot(f"{path}: workspace snapshot owner disagrees with its run")
    if removed and snapshot_precedes_removal(removed, modified):
        return
    fields = workspaces.snapshot_fields(made)
    identity = dict(owner_run=owner.run, repo=owner.repo, ticket=owner.ticket, checkouts=owner.checkout_rows())
    if removed:
        if owner_data is None or not owner.run or owner.run == removed["owner_run"]:
            return
        if owner.run != (row or {}).get("owner_run") and not newer_owner(removed, owner, run, connect):
            return
    if not row:
        if not ws.is_dir() or ws.is_symlink():
            return
        created = owner_data.get("created") if isinstance(owner_data, dict) else ""
        made_at = created or run.get("started") or util.iso(datetime.fromtimestamp(modified / 1e9, UTC))
        fingerprints = fields.pop("files")
        workspaces._insert(conn, ws.name, workspaces._encode(
            dict(state="kept", kept_reason=workspaces.LEGACY_RESUME, made_at=made_at, bytes=workspace.tree_bytes(ws),
                 **identity, **fields)), fingerprints)
        return

    if (row["repo"] and row["repo"] != owner.repo) or (row["ticket"] and row["ticket"] != owner.ticket):
        raise InvalidSnapshot(f"{path}: workspace snapshot owner disagrees with its workspace row")
    takeover = newer_owner(row, owner, run, connect)
    changed = (any(fields[k] != row[k] for k in ("salt", "heads", "onto", "retained"))
               or fields["files"] != workspaces._files(conn, row["id"]))
    owner_changed = any(identity[k] != row[k] for k in ("owner_run", "repo", "ticket"))
    saved_owner = json.loads(row["legacy_owner"])
    if modified <= row["legacy_mtime_ns"]:
        snapshot_owner_changed = owner_data != saved_owner if saved_owner is not None else owner_changed
        if modified < row["legacy_mtime_ns"] and (changed or snapshot_owner_changed):
            raise InvalidSnapshot(f"{path}: workspace snapshot conflicts with its recorded version")
        return
    claimed = row["owner_run"] and saved_owner and saved_owner.get("run") != row["owner_run"]
    if claimed and owner_changed and not takeover and row["state"] != "released":
        return
    if takeover or row["release_excluded"] is None:
        fields.update(identity)
    elif changed and row["state"] == "released":
        fields.update({key: identity[key] for key in ("owner_run", "repo", "ticket")})
    if takeover or (row["state"] == "released" and changed):
        if takeover:
            fields["release_excluded"] = None
        fields.update(keep_fields())
    update(conn, row, fields)


def update(conn, row, fields):
    from arete.store import workspaces

    fingerprints = fields.pop("files", None)
    workspaces._update(conn, row, workspaces._encode(fields), fingerprints)


def keep_fields():
    from arete.store import workspaces

    return dict(state="kept", kept_reason=workspaces.LEGACY_RESUME, released_at="",
                remove_asked_by="", held_by=[], last_sweep_error="")


@contextmanager
def snapshot_directory(folder):
    from arete import paths

    folder = Path(folder).absolute()
    if not folder.name or ".." in folder.parts:
        raise ValueError("supply a workspace snapshot directory")
    if paths.ISOLATED and (paths.confined(folder) is None
                           or paths.scratch_root(folder) != paths.scratch_root(paths.STATE_DIR)):
        raise PermissionError("workspace snapshot directory must be inside the isolated state fixture")
    with paths.directory_fd(folder.parent) as parent:
        try:
            fd = os.open(folder.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            fd = None
        try:
            yield fd
            try:
                current = os.stat(folder.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if (not os.path.samestat(os.fstat(parent), folder.parent.stat())
                    or fd is None and current is not None
                    or fd is not None and (current is None or not os.path.samestat(os.fstat(fd), current))):
                raise OSError(f"{folder}: workspace snapshot directory changed during handoff")
        finally:
            if fd is not None:
                os.close(fd)


@contextmanager
def recovery_snapshot(folder, ws):
    from arete.store import workspaces

    filename = f"{ws.name}.json"
    with snapshot_directory(folder) as fd:
        if fd is None:
            raise ValueError(f"{ws.name} snapshot is missing")
        before = os.stat(filename, dir_fd=fd, follow_symlinks=False)
        made = read_snapshot(folder, fd, filename, before)
        owner = recorded_owner(folder / filename, ws, made.get("\0owner", {}))
        yield owner, workspaces.snapshot_fields(made), before.st_mtime_ns
        if signature(os.stat(filename, dir_fd=fd, follow_symlinks=False)) != signature(before):
            raise ValueError(f"{ws.name} snapshot changed during retry")


def snapshot_names(fd):
    return {name for name in os.listdir(fd) if name.endswith(".json")} if fd is not None else set()


def verify_snapshot(conn, ws, folder, fd, version):
    name = f"{ws.name}.json"
    try:
        if signature(os.stat(name, dir_fd=fd, follow_symlinks=False)) == version:
            return
    except OSError as e:
        quarantine(conn, ws, current_workspace(conn, ws), e)
    else:
        quarantine(conn, ws, current_workspace(conn, ws), f"{folder / name}: snapshot changed during handoff")


def apply(conn, folder=None):
    from arete import paths
    from arete.store import principals

    if folder is not None:
        with snapshot_directory(folder) as fd, principals.acting("hand"):
            files = snapshot_names(fd)
            names = {name.removesuffix(".json") for name in files}
            names.update(r[0] for r in conn.execute("SELECT name FROM workspaces WHERE state != 'removed' "
                                                  "AND legacy_mtime_ns >= 0"))
            versions = {}
            for name in sorted(names - {"", ".", ".."}):
                ws = paths.WORKTREES_DIR / name
                if version := handoff_workspace(conn, ws, folder, fd, files):
                    versions[ws] = version
            if snapshot_names(fd) - files:
                raise OSError(f"{folder}: workspace snapshots appeared during handoff")
            for ws, version in versions.items():
                verify_snapshot(conn, ws, folder, fd, version)
    conn.execute("ALTER TABLE workspaces DROP COLUMN legacy_mtime_ns")
    conn.execute("ALTER TABLE workspaces DROP COLUMN legacy_owner")
    conn.execute("UPDATE schema_meta SET value = '28' WHERE key = 'oldest_writer'")
    conn.execute("DELETE FROM schema_meta WHERE key = 'new_workspace_store'")
