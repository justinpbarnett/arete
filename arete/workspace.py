from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config, gh, paths, record, sandbox, scratch, tracker, util
from .store import runs as run_store
from .util import git

SALT_KEY = "\0salt"
OWNER_KEY = "\0owner"
RETAINED_KEY = "\0retained"
ENDED = ("merged", "rejected", "done")
WAITING = ("running", "pr", "needs-info")
HELD_FILE = paths.STATE_DIR / "held-workspaces.json"


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


def finalize_scratch() -> list[str]:
    done = scratch.recover_pending()
    for root in done:
        refresh_released(Path(root).parent, Path(root).name)
    return done


def refresh_released(ws: Path, root: str) -> None:
    made = made_before(ws)
    held, salt = retained_in(made), salt_of(made)
    if held is None or not isinstance(held.get("roots"), list) or root not in held["roots"]:
        return
    del made[RETAINED_KEY]
    names = [n for n in held.get("names") or [] if isinstance(n, str) and n in made]
    try:
        made |= {n: fingerprint(ws / n, salt) for n in names}
    except OSError:
        pass
    try:
        made_record(ws).write_text(json.dumps(made))
    except OSError:
        pass


def retained(ws: Path, checkouts: list[Checkout], names: list[str], held: list[Path]) -> dict:
    examined = {c.wt for c in checkouts if (c.wt / ".git").exists()}
    loose = {c.prefix + n for c in checkouts if c.wt in examined for n in ignored_files(c.wt)}
    loose |= set(unexamined(ws, examined))
    return {"roots": sorted({p.name for p in held if p.name.startswith(paths.SCRATCH_PREFIX)}),
            "names": [n for n in names if n in loose]}


def remove_workspace(ws: Path, checkouts: list[Checkout], *, excluded: Collection[Path] = ()) -> bool:
    if scratch_left(ws, checkouts):
        return False
    made = made_before(ws)

    def unknown_outside(examined: set[Path]) -> bool:
        try:
            return any(not recorded(made, ws, n) for n in unexamined(ws, examined.union(excluded)))
        except OSError:
            return True

    if any(wt.resolve().is_relative_to(c.wt.resolve()) for c in checkouts for wt in excluded):
        return False
    if unknown_outside({c.wt for c in checkouts if (c.wt / ".git").exists()}):
        return False
    for c in checkouts:
        with util.repo_lock(c.src):
            if scratch_left(ws, checkouts):
                return False
            if (c.wt / ".git").exists():
                try:
                    if any(not recorded(made, c.wt, n, c.prefix)
                           for n in [*ignored_files(c.wt), *untracked_files(c.wt)]):
                        return False
                except (OSError, RuntimeError):
                    return False
            git(["worktree", "remove", "--force", str(c.wt)], c.src, check=False)
    if scratch_left(ws, checkouts) or unknown_outside(set()):
        return False
    if excluded:
        return True
    shutil.rmtree(ws, ignore_errors=True)
    if os.path.lexists(ws):
        return False
    made_record(ws).unlink(missing_ok=True)
    for c in checkouts:
        with util.repo_lock(c.src):
            git(["worktree", "prune"], c.src, check=False)
    return True


def recorded(made: dict, base: Path, name: str, prefix: str = "") -> bool:
    salt = salt_of(made)
    return bool(salt) and made.get(prefix + name) == fingerprint(base / name, salt)


def blocked_by(ws: Path, checkouts: list[Checkout], edits: bool = False) -> list[str]:
    try:
        if left := scratch_left(ws, checkouts):
            return list(dict.fromkeys(str(p) for p in left))
        made = made_before(ws)
        examined = {c.wt for c in checkouts if (c.wt / ".git").exists()}
        names = [n for n in unexamined(ws, examined) if not recorded(made, ws, n)]
        for c in checkouts:
            if (c.wt / ".git").exists():
                found = ([*uncommitted(c.wt), *ignored_files(c.wt)] if edits
                         else [*ignored_files(c.wt), *untracked_files(c.wt)])
                names += [c.prefix + n for n in dict.fromkeys(found)
                          if not recorded(made, c.wt, n, c.prefix)]
        return names
    except (OSError, RuntimeError) as e:
        return [util.first_line(str(e), 200)]


def made_copy(ws: Path, source: Path, copies: list[str], rel: str) -> bool:
    return (rel in copies and (source / rel).is_file() and (ws / rel).is_file()
            and (ws / rel).read_bytes() == (source / rel).read_bytes())


def made_record(ws: Path) -> Path:
    return paths.STATE_DIR / "workspaces" / f"{ws.name}.json"


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
    try:
        made = json.loads(made_record(ws).read_text())
    except (OSError, ValueError):
        return {}
    return made if isinstance(made, dict) else {}


def salt_of(made: dict) -> str:
    return str(made.get(SALT_KEY) or "")


def owner_in(made: dict) -> dict | None:
    owner = made.get(OWNER_KEY)
    return owner if isinstance(owner, dict) else None


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


def note_made(ws: Path, checkouts: list[Checkout], owner: dict | None = None,
              *, preserve: Collection[str] = (), excluded: Collection[Path] = ()) -> None:
    try:
        before = made_before(ws) if excluded or not owner else {}
        owner = owner or owner_in(before)
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
            if owner:
                made[OWNER_KEY] = owner
            if held:
                made[RETAINED_KEY] = retained(ws, checkouts, names, held)
            made_record(ws).parent.mkdir(parents=True, exist_ok=True)
            made_record(ws).write_text(json.dumps(made))
            if held and not scratch_left(ws, checkouts):
                for root in made[RETAINED_KEY]["roots"]:
                    refresh_released(ws, root)
            return
    except (OSError, RuntimeError):
        pass
    made_record(ws).unlink(missing_ok=True)


def unrecorded_files(ws: Path, names: list[str], source: Path, copies: list[str], made: dict,
                     held: list[Path] | None) -> list[str]:
    if held:
        names = outside_scratch(ws, names, held)
    return [n for n in names if not made_copy(ws, source, copies, n) and not recorded(made, ws, n)]


def kept_checkout(ws: Path, c: Checkout, source: Path, copies: list[str], *,
                  made: dict | None = None, commits: bool = True, held: list[Path] | None = None,
                  closed: bool = False) -> str:
    try:
        if closed and not (c.wt / ".git").exists():
            if c.wt.is_symlink() or (c.wt.exists() and any(c.wt.iterdir())):
                return f"The closed PR checkout `{c.github}` at `{c.wt}` is incomplete. Arete left its files in place."
            return ""
        made = made_before(ws) if made is None else made
        edits = uncommitted(c.wt)
        if edits and closed:
            return f"The closed PR checkout `{c.github}` at `{c.wt}` has uncommitted edits. Arete left them in place."
        edits = unrecorded_files(ws, [c.prefix + n for n in edits], source, copies, made, held)
        ignored = unrecorded_files(ws, [c.prefix + n for n in ignored_files(c.wt)], source, copies, made, held)
        ahead = "0"
        if not closed:
            ahead = git(["rev-list", "--count", "HEAD", "--not", "--remotes=origin"], c.wt).strip() if commits else "0"
            if head_recorded(made, c) == git(["rev-parse", "HEAD"], c.wt).strip():
                ahead = "0"
    except (RuntimeError, OSError) as e:
        return f"arete could not read the existing worktree {c.wt} of {c.github}, so it left it as it is: {e}"
    if edits or ignored or ahead != "0":
        what = (f"uncommitted edits that arete has no record of leaving there ({', '.join(edits[:5])})" if edits
                else (f"git-ignored files that arete has no record of leaving there ({', '.join(ignored[:5])})"
                      if ignored else f"{util.count(int(ahead), 'commit')} that are not on origin"))
        return (f"the existing worktree {c.wt} of {c.github} holds {what}, so arete did not make it again and "
                "left them as they are; move or remove them, then run again")
    return ""


def kept_work(ws: Path, checkouts: list[Checkout], source: Path, copies: list[str], commits: bool = True,
              held: list[Path] | None = None, *, excluded: Collection[Path] = ()) -> str:
    made = made_before(ws)
    examined = {c.wt for c in checkouts if (c.wt / ".git").exists()}
    for c in checkouts:
        if c.wt in examined and (why := kept_checkout(ws, c, source, copies, made=made, commits=commits, held=held)):
            return why
    try:
        others = unrecorded_files(ws, unexamined(ws, examined.union(excluded)), source, copies, made, held)
    except OSError as e:
        return f"arete could not read the existing workspace {ws}, so it left it as it is: {e}"
    if others:
        return (f"the existing workspace {ws} holds files outside this repo's worktrees ({', '.join(others[:5])}), "
                "so arete did not make it again and left them as they are; move or remove them, then run again")
    return ""


def scratch_failure(why: str, ws: Path, held: list[Path]) -> RunFailed:
    records = [p for p in held if p.parent == sandbox.AUTHORITY]
    selectors = sorted({p.stem for p in records if scratch.SELECTOR_FILE.fullmatch(p.name)})
    unidentifiable = sandbox.AUTHORITY in held or any(not scratch.SELECTOR_FILE.fullmatch(p.name) for p in records)
    return RunFailed(why, recovery={"reason": "scratch", "workspace": str(ws), "selectors": selectors,
                                    "unidentifiable_record": unidentifiable})


def worktree_failure(why: str, ws: Path, branch: str) -> RunFailed:
    return RunFailed(why, recovery={"reason": "worktree", "workspace": str(ws), "branch": branch})


def clear_workspace(ws: Path, checkouts: list[Checkout], source: Path, copies: list[str],
                    branch: str = "", *, excluded: Collection[Path] = ()) -> None:
    if left := scratch_held(ws, checkouts):
        raise scratch_failure(
            f"unresolved or unknown scratch resources keep the existing workspace retained ({held_words(left)})",
            ws, left)
    if why := kept_work(ws, checkouts, source, copies, excluded=excluded):
        raise worktree_failure(why, ws, branch)
    removal = {"excluded": excluded} if excluded else {}
    if not remove_workspace(ws, checkouts, **removal):
        why = "workspace removal withheld to preserve scratch resources"
        if left := scratch_left(ws, checkouts):
            raise scratch_failure(why, ws, left)
        raise RunFailed(why, recovery={"reason": "worktree-removal", "workspace": str(ws)})


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


def make_worktree(repo: config.Repo, base: str, branch: str, key: str, resume: bool = False) -> Path:
    c = Checkout("", repo.github, repo.path, paths.WORKTREES_DIR / f"{repo.name}-{key}", base)
    if not remove_workspace(c.wt, [c]):
        left = scratch_left(c.wt, [c])
        raise RunFailed("worktree removal withheld to preserve existing resources"
                        + (f" ({held_words(left)})" if left else ""))
    fetch(c.src, base)
    if resume:
        fetch(c.src, branch)
    c.resumes = resume
    add_worktree(c, branch, start_of(c, branch))
    return c.wt


def remove_worktree(repo: config.Repo, wt: Path) -> None:
    checkouts = [Checkout("", repo.github, repo.path, wt, "")]
    if not remove_workspace(wt, checkouts):
        print(f"{util.iso()} worktree kept at {wt}: removal blocked by {', '.join(blocked_by(wt, checkouts)[:10])}")


def parts_of(repo: config.Repo) -> list[config.Part]:
    return repo.parts or [config.Part("", repo.name)]


def owner_of(d: Path, cfg: config.Config) -> dict | None:
    owner = owner_in(made_before(d))
    if owner and owner.get("repo") and owner.get("checkouts"):
        return owner
    for repo in sorted(cfg.repos, key=lambda r: len(r.name), reverse=True) if made_record(d).is_file() else []:
        tid = d.name.removeprefix(f"{repo.name}-")
        if tid != d.name and run_store.ticket_runs(repo.name, tid, limit=1):
            return {"run": "", "repo": repo.name, "ticket": tid, "created": "",
                    "checkouts": [{"dir": p.dir, "github": p.github, "src": str(repo.path / p.dir),
                                   "wt": str(d / p.dir)} for p in parts_of(repo)]}
    return None


def tree_bytes(d: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(d):
        for n in dirs + files:
            try:
                total += Path(root, n).lstat().st_size
            except OSError:
                pass
    return total


def held_entry(d: Path, owner: dict, row: dict | None, blocked: bool, why: str, names: list[str] | None = None) -> dict:
    row = row or {}
    try:
        modified = util.iso(datetime.fromtimestamp(d.stat().st_mtime, UTC))
    except OSError:
        modified = ""
    return {"name": d.name, "path": str(d), "repo": owner["repo"], "ticket": owner["ticket"],
            "run": row.get("id") or owner.get("run") or "", "status": row.get("status") or "",
            "since": row.get("finished") or row.get("started") or owner.get("created") or modified,
            "bytes": tree_bytes(d), "blocked": blocked, "why": why, "names": (names or [])[:10]}


def ticket_closed(cfg: config.Config, repo: str, tid: str, status: str) -> bool:
    if tracker.is_task_id(tid):
        return tracker.task_state(tid) == "closed"
    configured = next((r for r in cfg.repos if r.name == repo), None)
    if configured is None or status == "pr":
        return False
    try:
        return not tracker.for_repo(cfg, configured).get(tid).open
    except Exception:
        return False


def sweep_owned(d: Path, owner: dict, cfg: config.Config) -> dict | None:
    repo, tid = owner["repo"], str(owner["ticket"])
    lock_name = run_store.run_lock("pipeline", repo, tid)
    if util.lock_is_held(lock_name) or run_store.issue_running(repo, tid):
        return None
    checkouts = [Checkout(c["dir"], c["github"], Path(c["src"]), Path(c["wt"]), "") for c in owner["checkouts"]]
    row = ((run_store.run_get(owner["run"]) if owner.get("run") else None)
           or run_store.newest_whole_run(repo, tid))
    status = (row or {}).get("status") or ""
    run_id = (row or {}).get("id") or owner.get("run") or ""
    closed = (status not in ENDED and status not in WAITING and tracker.is_task_id(tid)
              and ticket_closed(cfg, repo, tid, status))
    if status not in ENDED and not closed:
        return held_entry(d, owner, row, False,
                          f"run {run_id} ended {status or 'unknown'}, so the workspace waits for the ticket")
    made = made_before(d)
    if any(onto_recorded(made, c) and (c.wt / ".git").exists() for c in checkouts):
        return held_entry(d, owner, row, False, f"run {run_id} kept edits in the workspace, so it waits for a resume")
    with util.lock(lock_name) as got:
        if not got:
            return None
        try:
            clear_workspace(d, checkouts, checkouts[0].src, [])
        except RunFailed as e:
            return held_entry(d, owner, row, True, e.why, blocked_by(d, checkouts, edits=True))
        except (OSError, RuntimeError) as e:
            return held_entry(d, owner, row, True, util.first_line(str(e), 300), blocked_by(d, checkouts, edits=True))
    print(f"{util.iso()} removed the workspace {d}: "
          + (f"run {run_id} is {status}" if status in ENDED else f"task {tid} is closed"))
    return None


def sweep_unowned(d: Path, cutoff: float) -> None:
    if d.stat().st_mtime < cutoff and not scratch_left(d, []) and not unexamined(d, set()):
        shutil.rmtree(d, ignore_errors=True)


def prune_repos(cfg: config.Config) -> None:
    for repo in cfg.repos:
        for src in [repo.path / p.dir for p in repo.parts] or [repo.path]:
            try:
                if (src / ".git").exists():
                    with util.repo_lock(src):
                        git(["worktree", "prune"], src, check=False)
            except (OSError, RuntimeError):
                pass


def cleanup_worktrees(days: float = 7, cfg: config.Config | None = None) -> None:
    cfg = cfg or config.load()
    cutoff = util.now().timestamp() - days * 86400
    held = []
    for d in paths.WORKTREES_DIR.glob("*"):
        try:
            if not d.is_dir() or d.is_symlink():
                continue
            owner = owner_of(d, cfg)
            if owner is None:
                sweep_unowned(d, cutoff)
            elif entry := sweep_owned(d, owner, cfg):
                held.append(entry)
        except Exception as e:
            print(f"{util.iso()} sweeping {d}: {type(e).__name__}: {e}")
    prune_repos(cfg)
    util.write_json(HELD_FILE, sorted(held, key=lambda h: h["since"]))


def held_summary() -> dict:
    rows = util.read_json(HELD_FILE, [])
    rows = rows if isinstance(rows, list) else []
    oldest = rows[0] if rows else {}
    return {"count": len(rows), "bytes": sum(r.get("bytes") or 0 for r in rows), "oldest": oldest.get("since", ""),
            "oldest_name": oldest.get("name", ""), "blocked": sum(1 for r in rows if r.get("blocked")), "rows": rows}


class RunFailed(Exception):
    def __init__(self, why: str, detail: str = "", *, recovery: dict | None = None):
        super().__init__(why)
        self.why, self.detail = why, detail
        self.recovery = recovery


def uncommitted(wt: Path, *, tracked_only: bool = False) -> list[str]:
    untracked = "no" if tracked_only else "all"
    return [r[3:] for r in util.git_records(["status", "--porcelain", "--no-renames", f"--untracked-files={untracked}"], wt)]


def untracked_files(wt: Path, *, preserve_path_bytes: bool = False) -> set[str]:
    records = util.git_path_records if preserve_path_bytes else util.git_records
    return set(records(["ls-files", "--others", "--exclude-standard"], wt))


def ignored_file_paths(wt: Path, *, preserve_path_bytes: bool = False) -> set[str]:
    records = util.git_path_records if preserve_path_bytes else util.git_records
    return set(records(["ls-files", "--others", "--ignored", "--exclude-standard"], wt))


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
