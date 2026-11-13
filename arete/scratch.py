from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import struct
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import guard, paths, state, util

PREFIX = paths.SCRATCH_PREFIX
FOLDERS = ("fixtures", "config", "state", "out")
ENV_NAME = paths.SCRATCH_ENV
SOURCE = "arete:verify-scratch"
MAX_LISTED = 500
IGNORE = b"*\n"
KEY_BITS = 60
FLOCK = {"darwin": "@qqihh", "linux": "@hhqqi4x"}
FORBIDDEN_CHARS = ";|&()<$`\n\r"
GLOB_CHARS = "*?[]{}"
WRITERS = ("echo", "printf", "cat")
OPTIONS = {"mkdir": {"-p"}, "touch": set(), "cp": {"-f", "-p"}, "mv": {"-f"},
           "rm": {"-r", "-R", "-f", "-rf", "-fr", "-Rf", "-fR"}, "echo": None, "printf": None, "cat": set()}


@dataclass(frozen=True)
class Grant:
    token: str
    run_id: str
    step: str
    workspace: str
    root: str
    device: int
    inode: int
    made: float
    pid: int
    ignore: int


HELD: dict[str, tuple[int, Grant]] = {}


def grant_file(token: str) -> Path:
    return paths.VERIFY_SCRATCH_DIR / f"{token}.lock"


def record_of(grant: Grant) -> bytes:
    return json.dumps(asdict(grant), sort_keys=True).encode()


def key_of(data: bytes) -> int:
    return int(hashlib.sha256(data).hexdigest(), 16) >> (256 - KEY_BITS)


def born_before(st: os.stat_result, moment: float) -> bool | None:
    born = getattr(st, "st_birthtime", None)
    return None if born is None else born < moment


def predates(st: os.stat_result, grant: Grant) -> bool:
    return born_before(st, grant.made) is not False


def make(run_id: str, step: str, workspace: Path) -> Grant:
    sweep()
    ws = workspace.resolve()
    for token, (_, other) in list(HELD.items()):
        if other.workspace == str(ws):
            discard(other)
    made = time.time()
    while True:
        token = secrets.token_hex(8)
        root = ws / f"{PREFIX}{token}"
        try:
            os.mkdir(root, 0o700)
            break
        except FileExistsError:
            continue
    grant = None
    try:
        fd = os.open(root / ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(IGNORE)
            ignore = os.fstat(f.fileno()).st_ino
        st = os.lstat(root)
        grant = hold(Grant(token, run_id, step, str(ws), str(root), st.st_dev, st.st_ino, made, os.getpid(), ignore))
        for name in FOLDERS:
            os.mkdir(root / name, 0o700)
    except BaseException:
        if grant is None:
            shutil.rmtree(root, ignore_errors=True)
        else:
            discard(grant)
        raise
    return grant


def hold(grant: Grant) -> Grant:
    data = record_of(grant)
    paths.VERIFY_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(grant_file(grant.token), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, key_of(data), os.SEEK_SET)
        state.mark_seen(SOURCE, [data.decode()])
    except BaseException:
        os.close(fd)
        grant_file(grant.token).unlink(missing_ok=True)
        raise
    HELD[grant.token] = (fd, grant)
    return grant


def holder(f: Path, key: int) -> int:
    form = FLOCK.get(sys.platform)
    if form is None:
        return 0
    darwin = sys.platform == "darwin"
    query = ((key, 1, 0, fcntl.F_RDLCK, os.SEEK_SET) if darwin else (fcntl.F_RDLCK, os.SEEK_SET, key, 1, 0))
    fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        answer = struct.unpack(form, fcntl.fcntl(fd, fcntl.F_GETLK, struct.pack(form, *query)))
    finally:
        os.close(fd)
    kind, pid = (answer[3], answer[2]) if darwin else (answer[0], answer[4])
    return 0 if kind == fcntl.F_UNLCK else pid


def started(pid: int) -> float | None:
    said = util.sh(["ps", "-o", "lstart=", "-p", str(pid)], timeout=10, env={"LC_ALL": "C"}).stdout
    try:
        return time.mktime(time.strptime(" ".join(said.split()), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


def made_after_start(grant: Grant) -> float | None:
    try:
        born = getattr(os.lstat(grant.root), "st_birthtime", None)
    except OSError:
        return None
    start = started(grant.pid)
    return born if born is not None and start is not None and born >= start else None


def ancestors() -> set[int]:
    parents = {}
    for line in util.sh(["ps", "-A", "-o", "pid=,ppid="], timeout=10).stdout.splitlines():
        pid, _, parent = line.strip().partition(" ")
        if pid.isdigit() and parent.strip().isdigit():
            parents[int(pid)] = int(parent.strip())
    found, pid = set(), os.getppid()
    while pid > 1 and pid not in found:
        found.add(pid)
        pid = parents.get(pid, 0)
    return found


def intact(grant: Grant) -> bool:
    try:
        st = os.lstat(grant.root)
    except OSError:
        return False
    root = Path(grant.root)
    return (stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (grant.device, grant.inode)
            and st.st_uid == os.getuid() and root.parent == Path(grant.workspace)
            and root.name == PREFIX + grant.token and os.path.realpath(root) == grant.root
            and born_before(st, grant.made) is not True)


def records() -> list[Grant]:
    found = []
    for key in sorted(state.seen_keys(SOURCE)):
        try:
            grant = Grant(**json.loads(key))
        except (ValueError, TypeError):
            continue
        if record_of(grant) == key.encode():
            found.append(grant)
    return found


def grants() -> list[Grant]:
    return [HELD[g.token][1] if g.token in HELD else g for g in records()]


def granted(cwd: str | Path) -> Grant | None:
    run_id = os.environ.get("ARETE_RUN_ID", "")
    if not run_id:
        return None
    here = Path(os.path.realpath(cwd))
    above = None
    for grant in records():
        ws = Path(grant.workspace)
        if grant.run_id != run_id or not (here == ws or ws in here.parents) or not util.pid_alive(grant.pid):
            continue
        run = state.run_get(grant.run_id)
        if not run or run.get("status") != "running":
            continue
        if holder(grant_file(grant.token), key_of(record_of(grant))) != grant.pid:
            continue
        above = ancestors() if above is None else above
        if grant.pid not in above or not intact(grant):
            continue
        born = made_after_start(grant)
        if born is not None:
            return replace(grant, made=max(grant.made, born))
    return None


def canonical(word: str) -> bool:
    return (bool(word) and "\0" not in word and not word.startswith("~") and os.path.normpath(word) == word
            and not any(c in word for c in GLOB_CHARS))


def owned(grant: Grant, word: str, cwd: str | Path) -> bool:
    if not word or "\0" in word or word.startswith("~"):
        return False
    p = Path(word) if os.path.isabs(word) else Path(cwd) / word
    real = Path(os.path.realpath(p))
    if Path(grant.root) not in real.parents or guard.secret_path(real) or guard.secret_path(p):
        return False
    try:
        st = os.lstat(real)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if st.st_uid != os.getuid():
        return False
    if stat.S_ISREG(st.st_mode):
        return st.st_nlink == 1 and not predates(st, grant)
    return stat.S_ISDIR(st.st_mode)


def words(command: str) -> list[str]:
    try:
        split = shlex.split(command)
    except ValueError:
        split = command.split()
    return [p for w in split for p in re.split(r"[=<>;|&()`]", w) if p]


def told(command: str, cwd: str) -> str:
    try:
        grant = granted(cwd)
        if grant is None:
            return ""
        parts = list(dict.fromkeys(words(command)))
        inside = [p for p in parts if owned(grant, p, cwd)]
        near = [p for p in parts if p not in inside and Path(grant.root).name in p]
        if not inside and not near:
            return ""
        lines = [f"Scratch folder: {grant.root}",
                 ("Arete made this folder empty for this verification attempt and deletes it when the attempt ends. "
                  "Files in it are throwaway fixtures the attempt made itself, so creating, rewriting or deleting "
                  "them, a config or database file among them, changes temporary files and nobody's work or data.")]
        if inside:
            lines.append("Arete confirmed these paths in the command are such files: " + ", ".join(inside))
        if near:
            lines.append("These paths name the scratch folder but are not such files (they resolve outside it, look "
                         "like secret files, or were not made by this attempt): " + ", ".join(near)
                         + ". Judge them as real files.")
        lines.append("Everything else the command touches is judged as before.")
        return "\n".join(lines) + "\n\n"
    except Exception:
        return ""


def clean_path(grant: Grant, token: str, cwd: str) -> bool:
    return canonical(token) and owned(grant, token, cwd)


def clean_source(token: str, cwd: str) -> bool:
    if not canonical(token):
        return False
    p = Path(token) if os.path.isabs(token) else Path(cwd) / token
    return not guard.secret_path(p) and not guard.secret_path(os.path.realpath(p))


def old_files_below(grant: Grant, token: str, cwd: str) -> bool:
    p = Path(token) if os.path.isabs(token) else Path(cwd) / token
    if os.path.islink(p) or not os.path.isdir(p):
        return False
    for dirpath, _, files in os.walk(p):
        for f in files:
            try:
                if predates(os.lstat(os.path.join(dirpath, f)), grant):
                    return True
            except OSError:
                return True
    return False


def exact_destination(token: str, cwd: str) -> bool:
    p = Path(token) if os.path.isabs(token) else Path(cwd) / token
    return not token.endswith("/") and not os.path.isdir(p)


def permits(command: str, cwd: str) -> bool:
    try:
        return plain_write(command, cwd)
    except Exception:
        return False


def plain_write(command: str, cwd: str) -> bool:
    if any(c in command for c in FORBIDDEN_CHARS):
        return False
    grant = granted(cwd)
    if grant is None:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or tokens[0] not in OPTIONS:
        return False
    program, args = tokens[0], tokens[1:]
    operands, targets, i = [], [], 0
    while i < len(args):
        a = args[i]
        if ">" in a:
            if a not in (">", ">>") or program not in WRITERS or i + 1 >= len(args):
                return False
            if not clean_path(grant, args[i + 1], cwd) or not exact_destination(args[i + 1], cwd):
                return False
            targets.append(args[i + 1])
            i += 2
            continue
        if a.startswith("-") and program not in ("echo", "printf"):
            if a not in OPTIONS[program]:
                return False
        else:
            operands.append(a)
        i += 1
    if program in WRITERS:
        if program == "cat" and not all(clean_source(o, cwd) for o in operands):
            return False
        return bool(targets)
    if program in ("mkdir", "touch", "rm"):
        if not operands or not all(clean_path(grant, o, cwd) for o in operands):
            return False
        return not (program == "rm" and any(old_files_below(grant, o, cwd) for o in operands))
    if len(operands) != 2:
        return False
    source, dest = operands
    if not clean_path(grant, dest, cwd) or not exact_destination(dest, cwd):
        return False
    if program == "mv":
        return clean_path(grant, source, cwd) and not old_files_below(grant, source, cwd)
    s = Path(source) if os.path.isabs(source) else Path(cwd) / source
    return clean_source(source, cwd) and os.path.isfile(s)


def note(grant: Grant) -> str:
    root = grant.root
    return (f"## Scratch folder\n\n`{root}` is yours alone. Arete made it empty for this attempt, nothing else uses "
            f"it, and Arete deletes it when the attempt ends. It holds `fixtures`, `config`, `state` and `out`, for "
            f"fixtures, scratch config, scratch state and temporary output.\n\n"
            f"- Write each path in full from the root, never through `..`, a link or a shell variable, one plain "
            f"command per file, such as `printf '...' > {root}/config/config.toml`, `cp` of one file to its full "
            f"destination path, `mkdir -p` or `rm`, so the guard can confirm the command stays inside.\n"
            f"- Files elsewhere, secret-looking names (`.env`, keys) and files not made there get no special "
            f"treatment.\n"
            f"- Never copy a real credential, the live config or the live database in. Arete run on config or state "
            f"in this folder finds no login, key or price lookup of this machine, but other tools still see the real "
            f"logins and network: a check that would discover logins, read a credential store or call a provider "
            f"needs a fake adapter, or it is `blocked`.\n"
            f"- A fixture there is not an edit to the change.\n"
            f"- When a needed fixture or service still cannot be made, do not work around the guard: return "
            f"`blocked` and name the command.")


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


def end(grant: Grant, manifest: Path | None = None, keep: Path | None = None) -> tuple[list[str], list[str]]:
    if manifest is not None and intact(grant):
        try:
            util.write_json(manifest, {"root": grant.root, "run_id": grant.run_id, "step": grant.step,
                                       "made": grant.made, "ended": time.time(), "entries": listing(grant.root)})
        except OSError:
            pass
    moved = shelter(grant, keep) if keep is not None else []
    return moved, discard(grant)


def unused(dest: Path) -> Path:
    found, n = dest, 1
    while os.path.lexists(found):
        found, n = dest.with_name(f"{dest.name}.{n}"), n + 1
    return found


def shelter(grant: Grant, keep: Path, everything: bool = False) -> list[str]:
    if not intact(grant):
        return []
    moved, ignore = [], os.path.join(grant.root, ".gitignore")
    for dirpath, dirs, files in os.walk(grant.root):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
            full = os.path.join(dirpath, name)
            if not everything and full == ignore and ours(full, grant):
                continue
            dest = keep / os.path.relpath(full, grant.root)
            try:
                st = os.lstat(full)
                if not everything and (not stat.S_ISREG(st.st_mode) or not predates(st, grant)):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest = unused(dest)
                shutil.move(full, dest)
                moved.append(str(dest))
            except OSError:
                continue
    return moved


def ours(path: str, grant: Grant) -> bool:
    try:
        st = os.lstat(path)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size != len(IGNORE)
                or (st.st_dev, st.st_ino) != (grant.device, grant.ignore) or born_before(st, grant.made)):
            return False
        with open(path, "rb") as f:
            return f.read() == IGNORE
    except OSError:
        return False


def release(grant: Grant) -> None:
    held = HELD.pop(grant.token, None)
    state.forget_seen(SOURCE, [record_of(grant).decode()])
    try:
        grant_file(grant.token).unlink()
    except OSError:
        pass
    if held:
        os.close(held[0])


def discard(grant: Grant, remove: bool = True) -> list[str]:
    release(grant)
    if not intact(grant):
        return []
    ignore = os.path.join(grant.root, ".gitignore")
    kept = []
    for dirpath, dirs, files in os.walk(grant.root, topdown=False):
        for name in files + dirs:
            full = os.path.join(dirpath, name)
            if full == ignore:
                continue
            try:
                st = os.lstat(full)
                if stat.S_ISDIR(st.st_mode):
                    os.rmdir(full)
                elif not remove or (stat.S_ISREG(st.st_mode) and predates(st, grant)):
                    kept.append(full)
                else:
                    os.unlink(full)
            except OSError:
                if full not in kept and not os.path.isdir(full):
                    kept.append(full)
    if kept:
        if not os.path.lexists(ignore):
            try:
                fd = os.open(ignore, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(IGNORE)
            except OSError:
                pass
        return kept
    if remove and ours(ignore, grant):
        os.unlink(ignore)
    try:
        os.rmdir(grant.root)
    except OSError:
        if os.path.lexists(ignore):
            kept.append(ignore)
    return kept


def sweep() -> list[str]:
    found = grants()
    live = {g.root for g in found if g.token in HELD or util.pid_alive(g.pid)}
    removed = []
    for grant in found:
        if grant.token in HELD or util.pid_alive(grant.pid):
            continue
        if grant.root in live:
            release(grant)
            continue
        there = intact(grant)
        shelter(grant, paths.STATE_DIR / "verify-scratch-kept" / grant.token, everything=True)
        discard(grant, remove=False)
        if there and not os.path.lexists(grant.root):
            removed.append(grant.root)
    return removed
