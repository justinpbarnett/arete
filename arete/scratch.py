from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import guard, paths, util

PREFIX = ".arete-scratch-"
FOLDERS = ("fixtures", "config", "state", "out")
ENV_NAME = "ARETE_VERIFY_SCRATCH"
MAX_LISTED = 500
MTIME_SLACK = 2.0
FORBIDDEN_CHARS = ";|&()<$`\n\r"
GLOB_CHARS = "*?[]{}"
WRITERS = ("echo", "printf", "cat")
OPTIONS = {"mkdir": {"-p"}, "touch": set(), "cp": {"-r", "-R", "-f", "-p"}, "mv": {"-f"},
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


def grant_file(token: str) -> Path:
    return paths.VERIFY_SCRATCH_DIR / f"{token}.json"


def predates(st: os.stat_result, grant: Grant) -> bool:
    born = getattr(st, "st_birthtime", None)
    if born is not None:
        return born < grant.made
    return st.st_mtime < grant.made - MTIME_SLACK


def make(run_id: str, step: str, workspace: Path) -> Grant:
    sweep()
    ws = workspace.resolve()
    for other in grants():
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
        st = os.lstat(root)
        grant = Grant(token, run_id, step, str(ws), str(root), st.st_dev, st.st_ino, made, os.getpid())
        util.write_json(grant_file(token), asdict(grant))
        (root / ".gitignore").write_text("*\n")
        for name in FOLDERS:
            os.mkdir(root / name, 0o700)
    except BaseException:
        if grant is None:
            shutil.rmtree(root, ignore_errors=True)
        else:
            discard(grant)
        raise
    return grant


def intact(grant: Grant) -> bool:
    try:
        st = os.lstat(grant.root)
    except OSError:
        return False
    root = Path(grant.root)
    return (stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (grant.device, grant.inode)
            and st.st_uid == os.getuid() and root.parent == Path(grant.workspace)
            and root.name.startswith(PREFIX) and os.path.realpath(root) == grant.root
            and not predates(st, grant))


def valid(grant: Grant) -> bool:
    return util.pid_alive(grant.pid) and intact(grant)


def grants() -> list[Grant]:
    found = []
    try:
        files = sorted(paths.VERIFY_SCRATCH_DIR.glob("*.json"))
    except OSError:
        return found
    for f in files:
        try:
            found.append(Grant(**json.loads(f.read_text())))
        except (OSError, ValueError, TypeError):
            continue
    return found


def granted(cwd: str | Path) -> Grant | None:
    run_id = os.environ.get("ARETE_RUN_ID", "")
    if not run_id:
        return None
    here = Path(os.path.realpath(cwd))
    for grant in grants():
        ws = Path(grant.workspace)
        if grant.run_id == run_id and (here == ws or ws in here.parents) and valid(grant):
            return grant
    return None


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
    return not any(c in token for c in GLOB_CHARS) and owned(grant, token, cwd)


def clean_source(token: str, cwd: str) -> bool:
    if any(c in token for c in GLOB_CHARS) or token.startswith("~") or "\0" in token:
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


def lands_owned(grant: Grant, source: str, dest: str, cwd: str) -> bool:
    d = Path(dest) if os.path.isabs(dest) else Path(cwd) / dest
    if not d.is_dir():
        return True
    return owned(grant, str(d / Path(source).name), cwd)


def copy_confined(grant: Grant, source: str, dest: str, cwd: str, recursive: bool) -> bool:
    if not clean_source(source, cwd) or not lands_owned(grant, source, dest, cwd):
        return False
    s = Path(source) if os.path.isabs(source) else Path(cwd) / source
    if not os.path.isdir(s):
        return True
    if not recursive or os.path.islink(s):
        return False
    d = Path(dest) if os.path.isabs(dest) else Path(cwd) / dest
    top = d / s.name if d.is_dir() else d
    for dirpath, dirs, files in os.walk(s):
        for name in dirs + files:
            inner = Path(dirpath) / name
            if not clean_source(str(inner), cwd) or not owned(grant, str(top / inner.relative_to(s)), cwd):
                return False
    return True


def permits(command: str, cwd: str) -> bool:
    try:
        return plain_write(command, cwd)
    except Exception:
        return False


def plain_write(command: str, cwd: str) -> bool:
    grant = granted(cwd)
    if grant is None or any(c in command for c in FORBIDDEN_CHARS):
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
            if not clean_path(grant, args[i + 1], cwd):
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
    if len(operands) < 2:
        return False
    if program == "mv":
        return (all(clean_path(grant, o, cwd) for o in operands)
                and not any(old_files_below(grant, o, cwd) for o in operands[:-1])
                and all(lands_owned(grant, o, operands[-1], cwd) for o in operands[:-1]))
    recursive = bool({"-r", "-R"} & set(args))
    return (clean_path(grant, operands[-1], cwd)
            and all(copy_confined(grant, o, operands[-1], cwd, recursive) for o in operands[:-1]))


def note(grant: Grant) -> str:
    root = grant.root
    return (f"## Scratch folder\n\n`{root}` is yours alone. Arete made it empty for this attempt, nothing else uses "
            f"it, and Arete deletes it when the attempt ends. It holds `fixtures`, `config`, `state` and `out`, for "
            f"fixtures, scratch config, scratch state and temporary output.\n\n"
            f"- Write each path in full from the root, never through `..`, a link or a shell variable, one plain "
            f"command per file, such as `printf '...' > {root}/config/config.toml`, `cp`, `mkdir -p` or `rm`, so the "
            f"guard can confirm the command stays inside.\n"
            f"- Files elsewhere, secret-looking names (`.env`, keys) and files not made there get no special "
            f"treatment.\n"
            f"- Never copy a real credential, the live config or the live database in.\n"
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


def end(grant: Grant, manifest: Path | None = None) -> list[str]:
    if manifest is not None and intact(grant):
        try:
            util.write_json(manifest, {"root": grant.root, "run_id": grant.run_id, "step": grant.step,
                                       "made": grant.made, "ended": time.time(), "entries": listing(grant.root)})
        except OSError:
            pass
    return discard(grant)


def discard(grant: Grant) -> list[str]:
    try:
        grant_file(grant.token).unlink()
    except OSError:
        pass
    if not intact(grant):
        return []
    kept = []
    for dirpath, dirs, files in os.walk(grant.root, topdown=False):
        for name in files + dirs:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
                if stat.S_ISDIR(st.st_mode):
                    os.rmdir(full)
                elif stat.S_ISREG(st.st_mode) and predates(st, grant):
                    kept.append(full)
                else:
                    os.unlink(full)
            except OSError:
                if full not in kept and not os.path.isdir(full):
                    kept.append(full)
    try:
        os.rmdir(grant.root)
    except OSError:
        pass
    return kept


def sweep() -> list[str]:
    removed = []
    for grant in grants():
        if not util.pid_alive(grant.pid):
            discard(grant)
            removed.append(grant.root)
    return removed
