from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from . import config, gh, paths, record, sandbox, scratch, tracker, util
from .store import db, runs, workspaces
from .util import git

SALT_KEY = "\0salt"
RETAINED_KEY = "\0retained"
ENDED = ("merged", "rejected", "done")
WAITING = ("running", "pr", "needs-info")
termination_pending = False


@dataclass
class Checkout:
    dir: str
    github: str
    src: Path
    wt: Path
    base: str
    pr: str = ""
    lease: str = ""
    resumes: bool = False
    merged_base: bool = False
    revision: str = ""
    base_commit: str = ""
    resolved: list[str] = field(default_factory=list)
    merge: str = ""
    resolved_lines: int = 0
    onto: str = ""
    kept: str = ""
    pushed_head: str = ""

    @classmethod
    def from_row(cls, row: dict) -> Checkout:
        return cls(**{**row, "src": Path(row["src"]), "wt": Path(row["wt"])})

    def to_row(self) -> dict:
        return {**asdict(self), "src": str(self.src), "wt": str(self.wt)}

    def note_pushed_head(self, head: str) -> None:
        self.lease = self.pushed_head = head

    @property
    def publication_baseline(self) -> str:
        return self.lease if self.resumes or self.lease == self.pushed_head else ""

    @property
    def prefix(self) -> str:
        return f"{self.dir}/" if self.dir else ""

    @property
    def starting_point(self) -> str:
        if not self.onto:
            return self.start_note
        diff = f"git {f'-C {self.dir} ' if self.dir else ''}diff {self.onto[:12]} HEAD"
        return (f"{self.start_note}; the worktree also holds edits that an earlier run of this ticket left unpushed, "
                f"committed locally since `{self.onto[:12]}` (`{diff}` shows them), and they are part of this change")

    @property
    def start_note(self) -> str:
        if not self.resumes:
            return f"fresh from `{self.base}`"
        start = (f"continuing the open PR {self.pr}" if self.pr
                 else "continuing commits already pushed to the branch, with no PR yet")
        if not self.merged_base:
            return start
        at = f" at `{self.base_commit[:12]}`" if self.base_commit else ""
        fixed = (f"; arete resolved its merge conflicts in {', '.join(f'`{p}`' for p in self.resolved)}"
                 if self.resolved else "")
        return start + f", with `{self.origin_base}`{at} merged in since the base moved after the last push" + fixed

    @property
    def pr_name(self) -> str:
        return f"{self.github.split('/')[-1]}#{gh.pr_number(self.pr)}"

    @property
    def origin_base(self) -> str:
        return f"origin/{self.base}"

    @property
    def diff_from_base(self) -> str:
        return f"git {f'-C {self.dir} ' if self.dir else ''}diff {self.origin_base}...HEAD"

    @property
    def diff_from_fork(self) -> str:
        try:
            fork = git(["merge-base", self.origin_base, "HEAD"], self.wt).strip()[:12]
        except (RuntimeError, OSError):
            fork = ""
        return f"git {f'-C {self.dir} ' if self.dir else ''}diff {fork or self.origin_base}"


OWNER_CHECKOUT_FIELDS = ("dir", "github", "src", "wt")


@dataclass
class WorkspaceOwner:
    run: str
    repo: str
    ticket: str
    checkouts: list[Checkout]

    def checkout_rows(self) -> list[dict]:
        return [{k: row[k] for k in OWNER_CHECKOUT_FIELDS} for row in (c.to_row() for c in self.checkouts)]


@dataclass
class Release:
    state: Literal["removed", "held"]
    held_by: list[str] = field(default_factory=list)


def remote_head(src: Path, branch: str) -> str:
    out = git(["ls-remote", "origin", f"refs/heads/{branch}"], src).split()
    return out[0] if out else ""


def fetch(src: Path, ref: str) -> None:
    with util.repo_lock(src):
        git(["fetch", "origin", ref], src)


def add_worktree(c: Checkout, branch: str, start: str) -> None:
    c.wt.parent.mkdir(parents=True, exist_ok=True)
    util.add_worktree(c.src, c.wt, branch, start, reset=True)


def start_of(c: Checkout, branch: str) -> str:
    return f"origin/{branch}" if c.resumes else c.origin_base


def continue_from(c: Checkout, branch: str, onto: str) -> None:
    c.onto = onto
    behind = [start_of(c, branch), *([c.origin_base] if c.resumes else [])]
    if any(git(["rev-list", "--count", f"HEAD..{ref}"], c.wt).strip() != "0" for ref in behind):
        c.kept = git(["rev-parse", "HEAD"], c.wt).strip()


def scratch_named(names: set[str] | None, name: str) -> bool:
    return name.startswith(paths.SCRATCH_PREFIX) and (names is None or name in names)


def scratch_view() -> scratch.View | None:
    try:
        return scratch.survey()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def record_holds(view: scratch.View | None, d: Path) -> list[Path]:
    if view is None:
        return [sandbox.AUTHORITY] if os.path.lexists(sandbox.AUTHORITY) else []
    return [Path(scratch.path_of(h)) for h in scratch.holds(view, d)]


def held_words(left: list[Path]) -> str:
    return ", ".join(list(dict.fromkeys(str(p) for p in left))[:5])


def scratch_left(ws: Path, checkouts: list[Checkout], *, strict: bool = False) -> list[Path]:
    found, unproven = [], []
    view = scratch.survey() if strict else scratch_view()
    names = scratch.root_names(view) if view is not None else None
    scanned = {ws, *(c.wt for c in checkouts)}
    for d in scanned:
        if records := record_holds(view, d):
            found += [d, *records]

        def unreadable(e: OSError, d: Path = d, found: list[Path] = found) -> None:
            found.append(Path(e.filename) if e.filename else d)

        try:
            try:
                d.lstat()
            except FileNotFoundError:
                continue
            if d.is_symlink():
                found.append(d)
                continue
            for root, dirs, files in os.walk(d, onerror=unreadable):
                held = [n for n in dirs + files if scratch_named(names, n)]
                found += [Path(root, n) for n in held]
                unproven += [Path(root, n) for n in dirs + files
                             if n.startswith(paths.SCRATCH_PREFIX) and n not in held]
                dirs[:] = [n for n in dirs if n != ".git" and n not in held]
        except OSError:
            found.append(d)
    late_view = scratch.survey() if strict else scratch_view()
    late = scratch.root_names(late_view) if late_view is not None else None
    for d in scanned:
        if records := record_holds(late_view, d):
            found += [d, *records]
    return list(dict.fromkeys(found + [p for p in unproven if scratch_named(late, p.name)]))


def scratch_held(ws: Path, checkouts: list[Checkout]) -> list[Path]:
    if held := scratch_left(ws, checkouts):
        finalize_scratch()
        held = scratch_left(ws, checkouts)
    return held


def finalize_scratch(*, pending_refresh: set[str] | None = None) -> list[str]:
    done = scratch.recover_pending()
    roots = dict.fromkeys(done)
    if pending_refresh is not None:
        roots.update(dict.fromkeys(data["attempt"]["root"] for data in scratch.records() if data["state"] == "done"))
    for root in roots:
        refreshed = refresh_released(Path(root).parent, Path(root).name)
        if not refreshed and pending_refresh is not None:
            pending_refresh.add(root)
    return done


def refresh_released(ws: Path, root: str) -> bool:
    try:
        return workspaces.refresh_released(ws, root)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as e:
        print(f"{util.iso()} refreshing workspace {ws} after scratch: {util.first_line(str(e), 300)}")
        return False


def retained(ws: Path, checkouts: list[Checkout], names: list[str], held: list[Path]) -> dict:
    examined = {c.wt for c in checkouts if (c.wt / ".git").exists()}
    loose = {c.prefix + n for c in checkouts if c.wt in examined for n in ignored_files(c.wt)}
    loose |= set(unexamined(ws, examined))
    return {"roots": sorted({p.name for p in held if p.name.startswith(paths.SCRATCH_PREFIX)}),
            "names": [n for n in names if n in loose]}


def remove_workspace(ws: Path, checkouts: list[Checkout], *, by: str = "",
                     excluded: Collection[Path] = (), edits: bool = False) -> Release:
    if termination_pending:
        return Release("held", ["SIGTERM unwinding"])
    with workspaces.folder_lock(ws):
        try:
            row = workspaces.release(ws, checkouts, excluded=excluded)
        except workspaces.SnapshotUnavailable as e:
            return Release("held", [str(e)])
        if row:
            excluded = {Path(wt) for wt in row["release_excluded"]}
            if by and by != "sweep" and by == row["owner_run"]:
                note_made(ws, checkouts, workspaces.owner_of(row), excluded=excluded)
        made = made_before(ws) if row else {}
        force = by == "sweep" and bool(row and row["remove_asked_by"])
        try:
            release = remove_checkouts(ws, checkouts, made, excluded=excluded, force=force, edits=edits)
        except OSError as e:
            if row:
                workspaces.record_release_error(ws, row, e)
            raise
        workspaces.record_release(ws, checkouts, row, release, excluded=excluded)
        return release


def remove_checkouts(ws: Path, checkouts: list[Checkout], made: dict, *, excluded: Collection[Path],
                     force: bool, edits: bool = False) -> Release:
    if names := workspace_blockers(ws, checkouts, edits, excluded=excluded, made=made, force=force, strict=True):
        return Release("held", names)
    for index, c in enumerate(checkouts):
        target_last = checkouts[:index] + checkouts[index + 1:] + [c]
        with util.repo_lock(c.src):
            if names := workspace_blockers(ws, target_last, edits, excluded=excluded, made=made, target=c, force=force,
                                           strict=True):
                return Release("held", names)
            attached, unsafe = checkout_attachment(ws, c.wt)
            if unsafe is not None:
                return Release("held", [str(unsafe)])
            if attached:
                try:
                    git(["worktree", "remove", "--force", str(c.wt)], c.src)
                except RuntimeError as e:
                    raise OSError(f"{c.wt}: {e}") from e
    if names := workspace_blockers(ws, checkouts, excluded=excluded, made=made, after_removal=True, force=force):
        return Release("held", names)
    if excluded:
        return Release("removed")
    if os.path.lexists(ws):
        shutil.rmtree(ws)
    if os.path.lexists(ws):
        return Release("held", workspace_blockers(ws, checkouts, made=made, after_removal=True, force=force))
    for c in checkouts:
        with util.repo_lock(c.src):
            git(["worktree", "prune"], c.src, check=False)
    return Release("removed")


def recorded(made: dict, base: Path, name: str, prefix: str = "") -> bool:
    salt = salt_of(made)
    return bool(salt) and made.get(prefix + name) == fingerprint(base / name, salt)


def unverified(made: dict, base: Path, name: str, prefix: str = "") -> bool:
    try:
        return prefix + name not in made or not recorded(made, base, name, prefix)
    except OSError:
        return True


def outside_blockers(ws: Path, made: dict, examined: set[Path]) -> list[str]:
    try:
        return [n for n in unexamined(ws, examined) if ws / n not in examined and unverified(made, ws, n)]
    except OSError:
        return ["."]


def checkout_attachment(ws: Path, wt: Path) -> tuple[bool, Path | None]:
    if not wt.is_relative_to(ws):
        return False, wt
    parts = wt.relative_to(ws).parts
    if ".." in parts:
        return False, wt
    path = ws
    try:
        for path in reversed((wt, *wt.parents[:len(parts)])):
            if not stat.S_ISDIR(path.lstat().st_mode):
                return False, path
        path = wt / ".git"
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError):
        return False, path
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        return False, path
    return True, None


def registered_root_checkout(ws: Path) -> list[Checkout]:
    attached, _ = checkout_attachment(ws, ws)
    marker = ws / ".git"
    if not attached or not stat.S_ISREG(marker.lstat().st_mode):
        return []
    resolved = git(["rev-parse", "--resolve-git-dir", str(marker)], ws, check=False).removesuffix("\n")
    if not resolved:
        return []
    gitdir = Path(resolved)
    try:
        backlink = os.fsdecode((gitdir / "gitdir").read_bytes().removesuffix(b"\n"))
    except FileNotFoundError:
        return []
    if not backlink or (gitdir / backlink).resolve() != marker.resolve():
        return []
    try:
        listing = git(["worktree", "list", "--porcelain", "-z"], ws)
    except RuntimeError as e:
        raise OSError(f"{ws}: {e}") from e
    entries = [entry.split("\0") for entry in listing.split("\0\0") if entry.startswith("worktree ")]
    if not entries:
        return []
    main, *linked = entries
    root = ws.resolve()
    for path, *attrs in linked:
        if Path(path.removeprefix("worktree ")).resolve() == root and not any(a.startswith("prunable ") for a in attrs):
            return [Checkout("", "", Path(main[0].removeprefix("worktree ")), ws, "")]
    return []


def checkout_blockers(c: Checkout, made: dict, edits: bool, *, strict: bool) -> list[str]:
    try:
        found = (uncommitted(c.wt) + ignored_files(c.wt) if edits
                 else ignored_files(c.wt) + sorted(untracked_files(c.wt)))
        return [c.prefix + n for n in dict.fromkeys(found) if unverified(made, c.wt, n, c.prefix)]
    except (OSError, RuntimeError) as e:
        if strict:
            raise OSError(f"{c.wt}: {e}") from e
        return [c.dir or "."]


def workspace_blockers(ws: Path, checkouts: list[Checkout], edits: bool = False, *,
                       excluded: Collection[Path] = (), made: dict | None = None,
                       after_removal: bool = False, target: Checkout | None = None, force: bool = False,
                       strict: bool = False) -> list[str]:
    held = scratch_left(ws, checkouts)
    names = [str(p) for p in held]
    try:
        unsafe_root = not stat.S_ISDIR(ws.lstat().st_mode)
    except FileNotFoundError:
        unsafe_root = False
    except OSError:
        unsafe_root = True
    if unsafe_root:
        return list(dict.fromkeys([*names, str(ws)]))
    for wt in sorted(excluded):
        try:
            overlaps = any(wt.resolve().is_relative_to(c.wt.resolve()) for c in checkouts)
        except (OSError, RuntimeError, ValueError):
            overlaps = True
        if overlaps:
            names.append(str(wt))
    made = made_before(ws) if made is None else made
    examined = set()
    if not after_removal:
        for c in checkouts:
            attached, unsafe = checkout_attachment(ws, c.wt)
            if unsafe is not None:
                names.append(str(unsafe))
            if attached:
                examined.add(c.wt)
    containers = {ws, *(c.wt for c in checkouts)}
    if not force:
        names += outside_blockers(ws, made, examined.union(excluded, set(held) - containers))
    if not after_removal:
        for c in checkouts:
            attached, unsafe = checkout_attachment(ws, c.wt)
            if unsafe is not None:
                names.append(str(unsafe))
                continue
            if attached and not force:
                names += checkout_blockers(c, made, edits, strict=strict)
            if c is target:
                still_attached, unsafe = checkout_attachment(ws, c.wt)
                if unsafe is not None or still_attached != attached:
                    names.append(str(unsafe or c.wt))
    return list(dict.fromkeys(names))


def made_copy(ws: Path, source: Path, copies: list[str], rel: str) -> bool:
    return (rel in copies and (source / rel).is_file() and (ws / rel).is_file()
            and (ws / rel).read_bytes() == (source / rel).read_bytes())


def fingerprint(p: Path, salt: str) -> str:
    if not p.exists() and not p.is_symlink():
        return "missing"
    walked = os.walk(p) if p.is_dir() and not p.is_symlink() else [(str(p.parent), [], [p.name])]
    h = hashlib.sha256(salt.encode())
    for root, dirs, files in walked:
        dirs.sort()
        for name in sorted(dirs + files):
            q = Path(root, name)
            st = q.lstat()
            entry = f"{q.relative_to(p.parent).as_posix()}\0{st.st_mode}\0{st.st_size}\0{st.st_mtime_ns}\n"
            h.update(entry.encode(errors="surrogateescape"))
            if stat.S_ISLNK(st.st_mode):
                h.update(os.readlink(q).encode(errors="surrogateescape"))
            elif stat.S_ISREG(st.st_mode):
                with open(q, "rb") as f:
                    while chunk := f.read(1 << 20):
                        h.update(chunk)
    return h.hexdigest()


def made_before(ws: Path) -> dict:
    return workspaces.made_before(ws)


def salt_of(made: dict) -> str:
    return str(made.get(SALT_KEY) or "")


def retained_in(made: dict) -> dict | None:
    retained = made.get(RETAINED_KEY)
    return retained if isinstance(retained, dict) else None


def head_recorded(made: dict, c: Checkout) -> str:
    return str(made.get(head_key(c)) or "")


def onto_recorded(made: dict, c: Checkout) -> str:
    return str(made.get(onto_key(c)) or "")


def ignored_files(wt: Path) -> list[str]:
    return [r[3:] for r in util.git_records(["status", "--porcelain", "--ignored=matching",
                                             "--untracked-files=normal"], wt) if r.startswith("!! ")]


def unexamined(ws: Path, examined: set[Path]) -> list[str]:
    if ws in examined or not ws.is_dir():
        return []
    found = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if Path(root, d) not in examined]
        found += [Path(root, f).relative_to(ws).as_posix() for f in files]
        found += [Path(root, d).relative_to(ws).as_posix() for d in dirs if Path(root, d).is_symlink()]
    return sorted(found)


def leftovers(ws: Path, checkouts: list[Checkout], *, excluded: Collection[Path] = ()) -> list[str]:
    examined = {c.wt for c in checkouts if (c.wt / ".git").exists()}
    found = [c.prefix + n for c in checkouts if c.wt in examined for n in uncommitted(c.wt) + ignored_files(c.wt)]
    return found + unexamined(ws, examined.union(excluded))


def head_key(c: Checkout) -> str:
    return f"\0head {c.prefix}"


def onto_key(c: Checkout) -> str:
    return f"\0onto {c.prefix}"


def outside_scratch(ws: Path, names: list[str], held: list[Path]) -> list[str]:
    roots = scratch.root_names()
    return [n for n in names
            if not any(scratch_named(roots, part) for part in Path(n).parts)
            and not any(h.is_relative_to(ws / n) for h in held)]


def note_made(ws: Path, checkouts: list[Checkout], owner: WorkspaceOwner | None = None,
              *, preserve: Collection[str] = (), excluded: Collection[Path] = (),
              during_termination: bool = False) -> None:
    if termination_pending and not during_termination:
        return
    with workspaces.folder_lock(ws):
        if row := workspaces.open_for(ws.name):
            _note_made(ws, checkouts, row, owner, preserve, excluded)


def _note_made(ws: Path, checkouts: list[Checkout], row: dict, owner: WorkspaceOwner | None,
               preserve: Collection[str], excluded: Collection[Path]) -> None:
    excluded = set(excluded) | {Path(wt) for wt in row["release_excluded"] or ()}
    if excluded:
        checkouts = [c for c in checkouts if c.wt not in excluded]
    owner = owner or workspaces.owner_of(row)
    if not owner.checkouts:
        owner.checkouts = checkouts
    try:
        before = made_before(ws) if excluded else {}
        salt = salt_of(before) if excluded else ""
        excluded_records = {n: digest for n, digest in before.items() if salt and not n.startswith("\0")
                            and any((ws / n).is_relative_to(wt) for wt in excluded)}
        names, held = [], []
        if ws.is_dir():
            names, held = leftovers(ws, checkouts, excluded=excluded), scratch_left(ws, checkouts)
            names = outside_scratch(ws, names, held)
            if preserve:
                protected = {Path(n) for n in preserve}
                protected |= {parent for path in protected for parent in path.parents}
                names = [n for n in names if Path(n) not in protected]
        heads = {head_key(c): git(["rev-parse", "HEAD"], c.wt).strip() for c in checkouts if (c.wt / ".git").exists()}
        heads |= {onto_key(c): c.onto for c in checkouts if c.onto and (c.wt / ".git").exists()}
        if names or heads or excluded_records:
            salt = salt if excluded_records else os.urandom(16).hex()
            made = excluded_records | {n: fingerprint(ws / n, salt) for n in names} | heads | {SALT_KEY: salt}
            if held:
                made[RETAINED_KEY] = retained(ws, checkouts, names, held)
            workspaces.save_snapshot(row, made, owner)
            if held and not scratch_left(ws, checkouts):
                for root in made[RETAINED_KEY]["roots"]:
                    refresh_released(ws, root)
            return
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as e:
        log_snapshot_error(ws, owner.run, e)
    workspaces.save_snapshot(row, {})


def log_snapshot_error(ws: Path, run_id: str, error: Exception) -> None:
    line = f"{util.iso()} could not record files in {ws}: {util.first_line(str(error), 300)}"
    print(line, flush=True)
    if run_id and Path(run_id).name == run_id and run_id not in (".", ".."):
        with suppress(OSError):
            with (paths.RUNS_DIR / run_id / "run.log").open("a") as log:
                log.write(line + "\n")


def unrecorded_files(ws: Path, names: list[str], source: Path, copies: list[str], made: dict,
                     held: list[Path] | None) -> list[str]:
    if held:
        names = outside_scratch(ws, names, held)
    return [n for n in names if not made_copy(ws, source, copies, n) and not recorded(made, ws, n)]


def kept_checkout(ws: Path, c: Checkout, source: Path, copies: list[str], *,
                  made: dict | None = None, commits: bool = True, held: list[Path] | None = None,
                  closed: bool = False, force: bool = False, strict: bool = False) -> str:
    try:
        if closed and not (c.wt / ".git").exists():
            if c.wt.is_symlink() or (c.wt.exists() and any(c.wt.iterdir())):
                return f"The closed PR checkout `{c.github}` at `{c.wt}` is incomplete. Arete left its files in place."
            return ""
        made = made_before(ws) if made is None else made
        edits = [] if force else uncommitted(c.wt)
        if edits and closed:
            return f"The closed PR checkout `{c.github}` at `{c.wt}` has uncommitted edits. Arete left them in place."
        edits = unrecorded_files(ws, [c.prefix + n for n in edits], source, copies, made, held)
        ignored = [] if force else unrecorded_files(
            ws, [c.prefix + n for n in ignored_files(c.wt)], source, copies, made, held)
        ahead = "0"
        if not closed:
            ahead = git(["rev-list", "--count", "HEAD", "--not", "--remotes=origin"], c.wt).strip() if commits else "0"
            if head_recorded(made, c) == git(["rev-parse", "HEAD"], c.wt).strip():
                ahead = "0"
    except (RuntimeError, OSError) as e:
        why = f"arete could not read the existing worktree {c.wt} of {c.github}, so it left it as it is: {e}"
        if strict:
            raise OSError(why) from e
        return why
    if edits or ignored or ahead != "0":
        what = (f"uncommitted edits that arete has no record of leaving there ({', '.join(edits[:5])})" if edits
                else (f"git-ignored files that arete has no record of leaving there ({', '.join(ignored[:5])})"
                      if ignored else f"{util.count(int(ahead), 'commit')} that are not on origin"))
        return (f"the existing worktree {c.wt} of {c.github} holds {what}, so arete did not make it again and "
                "left them as they are; move or remove them, then run again")
    return ""


def kept_work(ws: Path, checkouts: list[Checkout], source: Path, copies: list[str], commits: bool = True,
              held: list[Path] | None = None, *, excluded: Collection[Path] = (), force: bool = False,
              strict: bool = False) -> str:
    made = made_before(ws)
    examined = {c.wt for c in checkouts if (c.wt / ".git").exists()}
    for c in checkouts:
        if c.wt in examined and (why := kept_checkout(ws, c, source, copies, made=made, commits=commits, held=held,
                                                     force=force, strict=strict)):
            return why
    if force:
        return ""
    try:
        others = unrecorded_files(ws, unexamined(ws, examined.union(excluded)), source, copies, made, held)
    except OSError as e:
        if strict:
            raise
        return f"arete could not read the existing workspace {ws}, so it left it as it is: {e}"
    if others:
        return (f"the existing workspace {ws} holds files outside this repo's worktrees ({', '.join(others[:5])}), "
                "so arete did not make it again and left them as they are; move or remove them, then run again")
    return ""


def scratch_failure(why: str, ws: Path, held: list[Path], *, held_by: list[str] | None = None) -> RunFailed:
    records = [p for p in held if p.parent == sandbox.AUTHORITY]
    selectors = sorted({p.stem for p in records if scratch.SELECTOR_FILE.fullmatch(p.name)})
    unidentifiable = sandbox.AUTHORITY in held or any(not scratch.SELECTOR_FILE.fullmatch(p.name) for p in records)
    return RunFailed(why, recovery={"reason": "scratch", "workspace": str(ws), "selectors": selectors,
                                    "unidentifiable_record": unidentifiable}, held_by=held_by)


def worktree_failure(why: str, ws: Path, branch: str) -> RunFailed:
    return RunFailed(why, recovery={"reason": "worktree", "workspace": str(ws), "branch": branch})


def clear_workspace(ws: Path, checkouts: list[Checkout], source: Path, copies: list[str],
                    branch: str = "", *, excluded: Collection[Path] = (), by: str = "") -> None:
    if left := scratch_held(ws, checkouts):
        raise scratch_failure(
            f"unresolved or unknown scratch resources keep the existing workspace retained ({held_words(left)})",
            ws, left)
    row = workspaces.open_for(ws.name) if by == "sweep" else None
    options = {"force": True} if row and row["remove_asked_by"] else {}
    if why := kept_work(ws, checkouts, source, copies, excluded=excluded, strict=by == "sweep", **options):
        raise worktree_failure(why, ws, branch)
    removal = {"excluded": excluded} if excluded else {}
    if by:
        removal["by"] = by
    release = remove_workspace(ws, checkouts, **removal)
    if release.state == "held":
        raise removal_failure(ws, checkouts, release)


def removal_failure(ws: Path, checkouts: list[Checkout], release: Release) -> RunFailed:
    why = "workspace removal withheld to preserve scratch resources"
    if release.held_by:
        why += f" ({', '.join(release.held_by[:5])})"
    if left := scratch_left(ws, checkouts):
        return scratch_failure(why, ws, left, held_by=release.held_by)
    return RunFailed(why, recovery={"reason": "worktree-removal", "workspace": str(ws)}, held_by=release.held_by)


def kept_onto(ws: Path, checkouts: list[Checkout], branch: str) -> dict[str, str]:
    made = made_before(ws)
    kept = {}
    for c in checkouts:
        onto = onto_recorded(made, c)
        if not onto or not (c.wt / ".git").exists():
            continue
        try:
            head = git(["rev-parse", "HEAD"], c.wt).strip()
            on = git(["symbolic-ref", "-q", "--short", "HEAD"], c.wt, check=False).strip()
        except (RuntimeError, OSError) as e:
            raise worktree_failure(f"arete could not read the existing worktree {c.wt} of {c.github}, so it left it as "
                                   f"it is: {e}", ws, branch) from e
        recorded = head_recorded(made, c)
        if head != recorded:
            what = f"is at `{head[:12]}`, not at `{recorded[:12]}` where arete kept the last run's edits"
        elif on != branch:
            what = f"has `{on or 'a detached HEAD'}` checked out, not `{branch}`"
        else:
            kept[str(c.wt)] = onto
            continue
        raise worktree_failure(
            f"the existing worktree {c.wt} of {c.github} {what}, so arete did not continue from it or make it again; "
            f"put it back with `git -C {c.wt} checkout -B {branch} {recorded}`, or remove "
            f"the worktree and the local branch `{branch}` to start from origin, then run again", ws, branch)
    return kept


def kept_or_cleared(ws: Path, checkouts: list[Checkout], source: Path, copies: list[str],
                    branch: str, *, excluded: Collection[Path] = ()) -> dict[str, str]:
    if left := scratch_held(ws, checkouts):
        raise scratch_failure(
            f"unresolved or unknown scratch resources keep the existing workspace retained ({held_words(left)})",
            ws, left)
    kept = kept_onto(ws, checkouts, branch)
    if not kept:
        clear_workspace(ws, checkouts, source, copies, branch, excluded=excluded)
        return {}
    if why := kept_work(ws, checkouts, source, copies, excluded=excluded):
        raise worktree_failure(why, ws, branch)
    return kept


def make_workspace(run_id: str, repo: config.Repo, base: str, branch: str, key: str,
                   copies: list[str], *, preview_of: str = "",
                   excluded: Collection[Path] = ()) -> tuple[Path, list[Checkout]]:
    ws = paths.WORKTREES_DIR / f"{repo.name}-{key}"
    checkouts = [Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir, base or gh.default_branch(p.github))
                 for p in repo.parts if ws / p.dir not in excluded]
    if any(wt.resolve().is_relative_to(c.wt.resolve()) or c.wt.resolve().is_relative_to(wt.resolve())
           for c in checkouts for wt in excluded):
        raise worktree_failure("an active worktree overlaps a closed PR worktree", ws, branch)
    kept = kept_or_cleared(ws, checkouts, repo.path, copies, branch, excluded=excluded)

    def resumes_for(c: Checkout) -> bool:
        if preview_of:
            return bool(remote_head(c.src, preview_of))
        c.pr = gh.open_pr_url(c.github, branch)
        c.lease = remote_head(c.src, branch)
        return bool(c.pr) or (bool(c.lease) and c.lease not in gh.closed_pr_heads(c.github, branch))

    place_checkouts(run_id, checkouts, kept, branch, resumes_for, starting_branch=preview_of or branch)
    return ws, checkouts


def make_worktree(repo: config.Repo, base: str, branch: str, key: str, resume: bool = False, owner: str = "") -> Path:
    c = Checkout("", repo.github, repo.path, paths.WORKTREES_DIR / f"{repo.name}-{key}", base)
    with workspaces.folder_lock(c.wt):
        release = remove_workspace(c.wt, [c])
        if release.state == "held":
            names = ", ".join(release.held_by[:5])
            raise RunFailed("worktree removal withheld to preserve existing resources"
                            + (f" ({names})" if names else ""))
        fetch(c.src, base)
        if resume:
            fetch(c.src, branch)
        c.resumes = resume
        add_worktree(c, branch, start_of(c, branch))
        workspaces.claim(c.wt, repo.name, "", owner or os.environ.get("ARETE_RUN_ID", ""), checkouts=[c])
    return c.wt


def remove_worktree(repo: config.Repo, wt: Path, owner: str = "") -> None:
    checkouts = [Checkout("", repo.github, repo.path, wt, "")]
    try:
        release = remove_workspace(wt, checkouts, by=owner or os.environ.get("ARETE_RUN_ID", ""))
    except (OSError, sqlite3.Error) as e:
        print(f"{util.iso()} worktree kept at {wt}: removal failed: {util.first_line(str(e), 300)}")
        return
    if release.state == "held":
        print(f"{util.iso()} worktree kept at {wt}: removal blocked by {', '.join(release.held_by[:10])}")


def parts_of(repo: config.Repo) -> list[config.Part]:
    return repo.parts or [config.Part("", repo.name)]


def tree_bytes(d: Path) -> int:
    if d.is_symlink():
        return 0
    total = 0
    for root, dirs, files in os.walk(d):
        for n in dirs + files:
            try:
                total += Path(root, n).lstat().st_size
            except OSError:
                pass
    return total


def ticket_closed(tid: str) -> bool:
    return tracker.is_task_id(tid) and tracker.task_state(tid) == "closed"


def prune_repos(cfg: config.Config) -> None:
    for repo in cfg.repos:
        for src in [repo.path / p.dir for p in repo.parts] or [repo.path]:
            try:
                if (src / ".git").exists():
                    with util.repo_lock(src):
                        git(["worktree", "prune"], src, check=False)
            except (OSError, RuntimeError):
                pass


def held_summary() -> dict:
    with db.one_read() as c:
        rows = [held_summary_row(row) for row in workspaces.retained_rows(c)]
    rows.sort(key=lambda row: row["since"])
    oldest = rows[0] if rows else {}
    return {"count": len(rows), "bytes": sum(row["bytes"] for row in rows), "oldest": oldest.get("since", ""),
            "oldest_name": oldest.get("name", ""), "blocked": sum(row["blocked"] for row in rows), "rows": rows}


def held_summary_row(row: dict) -> dict:
    run_id = row["owner_run"]
    run = (runs.run_get(run_id) or {}) if run_id else {}
    blocked, names = row["state"] == "held", row["held_by"]
    why = row["last_sweep_error"] or row["kept_reason"]
    if blocked and not why:
        why = "workspace removal blocked" + (f" ({', '.join(names[:5])})" if names else "")
    return dict(name=row["name"], path=str(paths.WORKTREES_DIR / row["name"]), run=run_id,
                status=run.get("status") or row["kept_reason"],
                since=run.get("finished") or run.get("started") or row["made_at"],
                bytes=row["bytes"], blocked=blocked, why=why, names=names)


class RunFailed(Exception):
    def __init__(self, why: str, detail: str = "", *, recovery: dict | None = None,
                 held_by: list[str] | None = None):
        super().__init__(why)
        self.why, self.detail = why, detail
        self.recovery = recovery
        self.held_by = held_by


def uncommitted(wt: Path, *, tracked_only: bool = False) -> list[str]:
    untracked = "no" if tracked_only else "all"
    return [r[3:] for r in util.git_records(["status", "--porcelain", "--no-renames", f"--untracked-files={untracked}"], wt)]


def untracked_files(wt: Path) -> set[str]:
    return set(util.git_records(["ls-files", "--others", "--exclude-standard"], wt))


def ignored_file_paths(wt: Path) -> set[str]:
    return set(util.git_records(["ls-files", "--others", "--ignored", "--exclude-standard"], wt))


def remove_untracked(wt: Path, name: str) -> None:
    f = wt / name
    f.unlink(missing_ok=True)
    for d in f.parents:
        if d == wt or not d.is_dir() or any(d.iterdir()):
            break
        d.rmdir()


def drop_untracked_bytecode(wt: Path) -> None:
    for name in untracked_files(wt):
        if Path(name).parent.name == "__pycache__":
            remove_untracked(wt, name)


def place_checkouts(run_id: str, checkouts: list[Checkout], kept: dict[str, str], branch: str,
                    resumes_for: Callable[[Checkout], bool], *, starting_branch: str = "") -> None:
    starting_branch = starting_branch or branch
    for c in checkouts:
        with record.timed(run_id, "clone", repo=c.github, base=c.base, branch=branch, path=str(c.wt)) as clone:
            fetch(c.src, c.base)
            c.resumes = resumes_for(c)
            if c.resumes:
                fetch(c.src, starting_branch)
            if str(c.wt) in kept:
                continue_from(c, branch, kept[str(c.wt)])
                clone["continued"] = True
            else:
                if kept and (c.wt / ".git").exists():
                    with util.repo_lock(c.src):
                        git(["worktree", "remove", "--force", str(c.wt)], c.src)
                add_worktree(c, branch, start_of(c, starting_branch))
            clone["resumed"] = c.resumes
