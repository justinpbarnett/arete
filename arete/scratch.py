from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shlex
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths, util

PREFIX = paths.SCRATCH_PREFIX
FOLDERS = ("fixtures", "config", "state", "out")
ENV_NAME = paths.SCRATCH_ENV
MAX_LISTED = 500
MAX_SAVED_BYTES = 16 * 1024 * 1024
IGNORE = b"*\n"
TOKEN = paths.SCRATCH_TOKEN
UNSAFE_CHARS = "$~*?[]{}"
HOOK = {"root": "", "id": "", "saves": ""}
valid_root = paths.valid_scratch


@dataclass(frozen=True)
class Attempt:
    token: str
    run_id: str
    step: str
    workspace: str
    root: str
    device: int
    inode: int
    folders: dict
    ignore: int


def open_marker(run_dir: Path, step: str) -> Path:
    return run_dir / f"{step}-scratch-open.json"


def make(run_id: str, step: str, workspace: Path, run_dir: Path) -> Attempt:
    ws = workspace.resolve()
    while True:
        token = secrets.token_hex(8)
        root = ws / f"{PREFIX}{token}"
        try:
            os.mkdir(root, 0o700)
            break
        except FileExistsError:
            continue
    attempt = None
    try:
        fd = os.open(root / ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(IGNORE)
            ignore = os.fstat(f.fileno()).st_ino
        st = os.lstat(root)
        attempt = Attempt(token, run_id, step, str(ws), str(root), st.st_dev, st.st_ino, {}, ignore)
        for name in FOLDERS:
            os.mkdir(root / name, 0o700)
            attempt.folders[name] = os.lstat(root / name).st_ino
        util.write_json(open_marker(run_dir, step), asdict(attempt))
    except BaseException:
        if attempt is not None:
            end(attempt, run_dir)
        else:
            Path(root, ".gitignore").unlink(missing_ok=True)
            os.rmdir(root)
        raise
    return attempt


def intact(a: Attempt) -> bool:
    st = valid_root(a.root)
    return (st is not None and (st.st_dev, st.st_ino) == (a.device, a.inode)
            and Path(a.root) == Path(a.workspace) / f"{PREFIX}{a.token}")


def listing(root: str) -> list[dict]:
    found = []
    for dirpath, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            if len(found) >= MAX_LISTED:
                return found
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            kind = ("link" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode)
                    else "file" if stat.S_ISREG(st.st_mode) else "other")
            found.append({"path": os.path.relpath(full, root), "kind": kind, "bytes": st.st_size})
    return found


def ours(name: str, device: int, ignore: int, dir_fd: int | None = None) -> bool:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and (st.st_dev, st.st_ino) == (device, ignore)
                and f.read(len(IGNORE) + 1) == IGNORE)


def open_dir(name: str | Path, inode: int | None = None, dir_fd: int | None = None) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    if inode is not None and os.fstat(fd).st_ino != inode:
        os.close(fd)
        raise OSError(errno.ESTALE, "not the folder Arete made", str(name))
    return fd


def archive(run_dir: Path, a: Attempt) -> tuple[Path, int]:
    run_dir.mkdir(parents=True, exist_ok=True)
    base = dest = run_dir / f"{a.step}-scratch-{a.token}"
    n = 0
    while True:
        try:
            os.mkdir(dest, 0o700)
            return dest, open_dir(dest)
        except FileExistsError:
            n += 1
            dest = base.with_name(f"{base.name}.{n}")


def carry(name: str, src_fd: int, dest_fd: int, shown: tuple[str, str], moved: list[str], kept: list[str]) -> None:
    try:
        os.rename(name, name, src_dir_fd=src_fd, dst_dir_fd=dest_fd)
        moved.append(shown[1])
    except OSError:
        kept.append(shown[0])


def carry_folder(name: str, inode: int, root_fd: int, dest_fd: int, shown: tuple[str, str], moved: list[str],
                 kept: list[str]) -> None:
    fds = []
    try:
        fds.append(open_dir(name, inode, root_fd))
        children = sorted(os.listdir(fds[0]))
        if not children:
            return
        os.mkdir(name, 0o700, dir_fd=dest_fd)
        fds.append(open_dir(name, dir_fd=dest_fd))
        for child in children:
            carry(child, fds[0], fds[1], (os.path.join(shown[0], child), os.path.join(shown[1], child)), moved, kept)
    except OSError:
        kept.append(shown[0])
    finally:
        for fd in fds:
            os.close(fd)


def end(a: Attempt, run_dir: Path) -> tuple[str, list[str], list[str]]:
    there = intact(a)
    try:
        util.write_json(run_dir / f"{a.step}-scratch.json",
                        {"root": a.root, "run_id": a.run_id, "step": a.step, "ended": util.iso(),
                         "entries": listing(a.root) if there else []})
    except OSError:
        pass
    if not there:
        return "", [], []
    moved, kept, fds = [], [], []
    try:
        fds.append(open_dir(a.root, a.inode))
        names = sorted(os.listdir(fds[0]))
        dest, dest_fd = archive(run_dir, a)
        fds.append(dest_fd)
    except OSError:
        for fd in fds:
            os.close(fd)
        return "", [], [a.root]
    root_fd = fds[0]
    try:
        for name in names:
            if name == ".gitignore" and ours(name, a.device, a.ignore, root_fd):
                continue
            shown = (os.path.join(a.root, name), str(dest / name))
            try:
                st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                kept.append(shown[0])
                continue
            if stat.S_ISDIR(st.st_mode) and a.folders.get(name) == st.st_ino:
                carry_folder(name, st.st_ino, root_fd, dest_fd, shown, moved, kept)
            else:
                carry(name, root_fd, dest_fd, shown, moved, kept)
        for name in FOLDERS:
            try:
                os.rmdir(name, dir_fd=root_fd)
            except OSError:
                pass
        if not kept:
            try:
                if ours(".gitignore", a.device, a.ignore, root_fd):
                    os.unlink(".gitignore", dir_fd=root_fd)
                os.rmdir(a.root)
            except OSError:
                kept.append(a.root)
    finally:
        for fd in fds:
            os.close(fd)
    if not moved:
        try:
            os.rmdir(dest)
        except OSError:
            pass
    if not os.path.lexists(a.root):
        open_marker(run_dir, a.step).unlink(missing_ok=True)
    return str(dest) if moved else "", moved, kept


def recovered(marker: Path, run_dir: Path) -> Attempt | None:
    try:
        a = Attempt(**json.loads(marker.read_text()))
    except (OSError, TypeError, ValueError):
        return None
    if not (isinstance(a.token, str) and TOKEN.fullmatch(a.token) and isinstance(a.step, str)
            and isinstance(a.workspace, str) and isinstance(a.root, str) and isinstance(a.folders, dict)
            and marker == open_marker(run_dir, a.step) and a.root == str(Path(a.workspace) / f"{PREFIX}{a.token}")
            and valid_root(a.root) is not None):
        return None
    if paths.WORKTREES_DIR.resolve() not in Path(os.path.realpath(a.workspace)).parents:
        return None
    return a


def recover(run_dir: Path) -> list[str]:
    emptied = []
    for marker in sorted(run_dir.glob("*-scratch-open.json")):
        if (a := recovered(marker, run_dir)) is None:
            continue
        end(a, run_dir)
        if not os.path.lexists(a.root):
            emptied.append(a.root)
    return emptied


def hook_args(a: Attempt, run_dir: Path) -> list[str]:
    return ["--scratch-id", f"{a.device}:{a.inode}:{a.ignore}", "--scratch-saves",
            os.path.join(os.path.realpath(run_dir), f"{a.step}-scratch-saved-{a.token}"), "--scratch", a.root]


def hook_command(command: str, args: list[str]) -> str:
    return command + " " + shlex.join(args)


def words(command: str) -> list[str]:
    try:
        split = shlex.split(command)
    except ValueError:
        split = command.split()
    return list(dict.fromkeys(p for w in split for p in re.split(r"[=<>;|&()`]", w) if p))


def under(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def unlinked_below(p: Path, root: Path) -> bool:
    here = root
    for part in p.relative_to(root).parts:
        here = here / part
        try:
            if stat.S_ISLNK(os.lstat(here).st_mode):
                return False
        except FileNotFoundError:
            return True
    return True


def refuse(e: OSError) -> None:
    raise e


def plain_files(p: Path) -> list[Path] | None:
    from . import guard

    found = []
    for dirpath, dirs, files in [(str(p.parent), [], [p.name])] if not p.is_dir() else os.walk(p, onerror=refuse):
        for name in dirs + files:
            full = Path(dirpath, name)
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode):
                continue
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid()
                    or guard.secret_path(full) or len(found) >= MAX_LISTED):
                return None
            found.append(full)
    return found


def inside(word: str, here: Path, root: Path) -> tuple[bool, list[Path]] | None:
    from . import guard

    if any(part in ("..", ".") for part in word.split("/")) or any(c in word for c in UNSAFE_CHARS):
        return None
    p = Path(os.path.abspath(os.path.join(here, word)))
    if not under(p, root) or not unlinked_below(p, root):
        return None
    if guard.secret_path(p) or guard.secret_path(os.path.realpath(p)):
        return None
    try:
        os.lstat(p)
    except FileNotFoundError:
        return False, []
    try:
        files = plain_files(p)
    except OSError:
        return None
    return None if files is None else (True, files)


def copy_plain(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(errno.EPERM, "not a plain file", str(src))
        with os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as out:
            shutil.copyfileobj(f, out)


def save(root: Path, files: list[Path]) -> Path | None:
    saves = HOOK["saves"]
    if not os.path.isabs(saves):
        return None
    try:
        if sum(os.lstat(f).st_size for f in files) > MAX_SAVED_BYTES:
            return None
        os.makedirs(saves, 0o700, exist_ok=True)
        if os.path.realpath(saves) != os.path.abspath(saves):
            return None
        n = len(os.listdir(saves))
        while True:
            n += 1
            dest = Path(saves, str(n))
            try:
                os.mkdir(dest, 0o700)
                break
            except FileExistsError:
                continue
        for f in files:
            copy_plain(f, dest / f.relative_to(root))
    except OSError:
        return None
    return dest


def bound_root() -> Path | None:
    root = HOOK["root"]
    st = valid_root(root) if root else None
    try:
        device, inode, ignore = (int(n) for n in HOOK["id"].split(":"))
    except ValueError:
        return None
    if (st is None or (st.st_dev, st.st_ino) != (device, inode)
            or not ours(os.path.join(root, ".gitignore"), device, ignore)):
        return None
    return Path(root)


def context(command: str, cwd: str) -> str:
    try:
        return described(command, cwd)
    except Exception:
        return ""


def described(command: str, cwd: str) -> str:
    root = bound_root()
    if root is None:
        return ""
    here = Path(os.path.realpath(cwd))
    if not under(here, root.parent):
        return ""
    new, there, outside, files = [], [], [], []
    for word in words(command):
        full = os.path.join(here, word)
        if root.name not in word and not (("/" in word or os.path.lexists(full))
                                          and under(Path(os.path.abspath(full)), root)):
            continue
        found = inside(word, here, root)
        if found is None:
            outside.append(word)
        elif found[0]:
            there.append(word)
            files += found[1]
        else:
            new.append(word)
    if not new and not there and not outside:
        return ""
    saved = save(root, list(dict.fromkeys(files))) if files else ""
    if saved is None:
        there, outside = [], there + outside
    lines = [f"Scratch folder: {root}",
             ("Arete made this folder empty, mode 700 and ignored by git, inside this worktree for the current "
              "verification attempt; when the attempt ends Arete moves everything in it to the run folder and deletes "
              "nothing.")]
    if new:
        lines.append("Inside it, not yet existing: " + ", ".join(new))
    if there:
        lines.append("Inside it, already there: " + ", ".join(there) + ". Arete cannot tell whether this attempt "
                     "made them or something moved them in from elsewhere, so " +
                     (f"before this command it saved a copy of every file in them, as it is now, to {saved}, and a "
                      f"change to them can be undone from that copy." if saved else "it notes they hold no files."))
    if outside:
        lines.append("These name the scratch folder but go through a link or `..`, are linked from elsewhere, look "
                     "like secret files, leave it or could not be copied first, so judge them as real files: "
                     + ", ".join(outside))
    lines.append("Everything else the command touches is judged as before.")
    return "\n".join(lines) + "\n\n"


def note(a: Attempt) -> str:
    root = a.root
    return (f"## Scratch folder\n\n`{root}` is this attempt's own folder. Arete made it empty for this attempt, and it "
            f"holds `fixtures`, `config`, `state` and `out`.\n\n"
            f"- Put fixtures in `{root}/fixtures`, scratch Arete config and state in `{root}/config` and "
            f"`{root}/state` (as `ARETE_CONFIG_DIR` and `ARETE_STATE_DIR`), and temporary output in `{root}/out`. "
            f"Write each path in full, such as `printf '...' > {root}/config/config.toml`.\n"
            f"- When the attempt ends Arete moves everything in the folder to the run folder and deletes nothing.\n"
            f"- A fixture there is not an edit to the change.\n"
            f"- Never copy a real credential, the live config or the live database in. Arete run on config or state "
            f"in this folder uses no login or key of this machine, but other tools still do: a check that would read "
            f"a credential store or call a provider needs a fake adapter, or it is `blocked`.\n"
            f"- When a needed fixture or service still cannot be made, do not work around the guard: return "
            f"`blocked` and name the command.")
