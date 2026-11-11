from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from . import paths

PROBE_GRACE_SECONDS = 0.2

def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def sh(
    cmd: list[str] | str,
    cwd: Path | str | None = None,
    timeout: float | None = 600,
    check: bool = False,
    env: dict | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    try:
        res = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=full_env,
            input=input,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        res = subprocess.CompletedProcess(
            cmd, 124, stdout=_s(e.stdout), stderr=_s(e.stderr) + f"\n[arete] timed out after {timeout}s"
        )
    except FileNotFoundError as e:
        res = subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(e))
    if check and res.returncode != 0:
        raise RuntimeError(f"{cmd!r} failed ({res.returncode}): {res.stderr.strip() or res.stdout.strip()}")
    return res


NETWORK_ERROR = re.compile(
    r"could not resolve host|no such host|network is unreachable|connection (?:timed out|reset|refused)"
    r"|operation timed out|i/o timeout|tls handshake timeout|failed to connect|error connecting to|dial tcp"
    r"|temporary failure in name resolution|ssh: connect to host|unable to access",
    re.IGNORECASE,
)
NETWORK_PROBE_HOSTS = (("api.github.com", 443), ("1.1.1.1", 53))
WAKE_NETWORK_WAIT_MINUTES = 3


def is_network_error(text: str) -> bool:
    return bool(NETWORK_ERROR.search(text or ""))


def online() -> bool:
    for host, port in NETWORK_PROBE_HOSTS:
        try:
            socket.create_connection((host, port), timeout=5).close()
            return True
        except OSError:
            continue
    return False


def wait_for_network(max_minutes: float = 30) -> bool:
    deadline = time.time() + max_minutes * 60
    pause = 5.0
    while not online():
        if time.time() >= deadline:
            return False
        time.sleep(min(pause, max(1.0, deadline - time.time())))
        pause = min(pause * 2, 60)
    return True


def last_wake() -> float:
    times = []
    for key in ("kern.waketime", "kern.boottime"):
        if found := re.search(r"sec = (\d+)", sh(["sysctl", "-n", key], timeout=10).stdout):
            times.append(float(found.group(1)))
    return max(times, default=0.0)


def hours_awake_since(timestamp: float) -> float:
    return (now().timestamp() - max(timestamp, last_wake())) / 3600


def sh_with_network(cmd: list[str] | str, **kw) -> subprocess.CompletedProcess:
    res = sh(cmd, **kw)
    if res.returncode == 0 or not is_network_error(res.stdout + res.stderr):
        return res
    print(f"{iso()} network error, waiting for the network: {first_line(res.stderr or res.stdout)}", flush=True)
    return sh(cmd, **kw) if wait_for_network() else res


def git(args: list[str], cwd: Path, timeout: float = 300, check: bool = True, input: str | None = None,
        env: dict | None = None) -> str:
    res = sh_with_network(["git", *args], cwd=cwd, timeout=timeout, input=input, env=env)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {git_command(args)}: {git_error(res.stderr or res.stdout)}")
    return res.stdout


def git_command(args: list[str]) -> str:
    while args[:1] == ["-c"]:
        args = args[2:]
    return " ".join(args[:2])


ARETE_IDENTITY = ["-c", "user.name=arete", "-c", "user.email=arete@localhost"]


def git_as_owner(args: list[str], cwd: Path, **kw) -> str:
    owner_known = git(["config", "user.email"], cwd, check=False).strip()
    return git([*([] if owner_known else ARETE_IDENTITY), *args], cwd, **kw)


def git_records(args: list[str], cwd: Path, **kw) -> list[str]:
    return [r for r in git([args[0], "-z", *args[1:]], cwd, **kw).split("\0") if r]


def origin_default_branch(src: Path) -> str:
    head = git(["ls-remote", "--symref", "origin", "HEAD"], src, check=False)
    found = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", head, re.MULTILINE)
    return found.group(1) if found else "main"


def git_error(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith(("fatal:", "error:"))]
    return "; ".join(errors or lines[:1])[:300]


@contextmanager
def repo_lock(src: Path):
    paths.ensure_dirs()
    name = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    with open(paths.LOCKS_DIR / f"git-{name}.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _s(b) -> str:
    if b is None:
        return ""
    return b.decode(errors="replace") if isinstance(b, bytes) else b


def money(usd: float, places: int = 2) -> str:
    least = 10 ** -places
    return f"<${least:.{places}f}" if 0 < usd < least / 2 else f"${usd:.{places}f}"


def paid_note(usd: float, places: int = 2) -> str:
    return f"{money(usd, places)} paid" if usd else "nothing paid"


def count(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def tail(text: str, n: int = 4000) -> str:
    return text if len(text) <= n else "...\n" + text[-n:]


def both_ends(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    half = (n - 5) // 2
    return f"{text[:half]}\n...\n{text[-half:]}"


def first_line(text: str, n: int = 160) -> str:
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line[:n]
    return ""


def last_line(text: str, n: int = 160) -> str:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line:
            return line[:n]
    return ""


def matches_any(path: str, globs: list[str]) -> bool:
    for g in globs:
        if g.endswith("/") and (path.startswith(g) or path == g.rstrip("/")):
            return True
        if fnmatch.fnmatch(path, g):
            return True
        if g.endswith("/**") and path.startswith(g[:-3] + "/"):
            return True
    return False


def flock_now(f: TextIO) -> bool:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def take_lock(f: TextIO, wait: bool) -> bool:
    if wait:
        fcntl.flock(f, fcntl.LOCK_EX)
    elif not flock_now(f):
        time.sleep(PROBE_GRACE_SECONDS)
        if not flock_now(f):
            return False
    mark_holder(f, os.getpid())
    return True


def mark_holder(f: TextIO, pid: int) -> None:
    f.truncate(0)
    f.write(str(pid))
    f.flush()


def lock_file(name: str) -> TextIO:
    paths.ensure_dirs()
    return open(paths.LOCKS_DIR / f"{name}.lock", "a")


@contextmanager
def lock(name: str, wait: bool = False):
    with lock_file(name) as f:
        yield take_lock(f, wait)


def hold_lock(name: str) -> TextIO:
    f = lock_file(name)
    take_lock(f, wait=True)
    return f


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def own_group_args() -> dict:
    return {"process_group": 0} if os.getsid(0) == os.getpid() else {"start_new_session": True}


def end_session(pid) -> None:
    if not pid:
        return
    groups = {int(pid)}
    listed = sh(["ps", "-A", "-o", "pid=,pgid="], timeout=10).stdout
    for member, group in (map(int, line.split()) for line in listed.splitlines()):
        try:
            if os.getsid(member) == int(pid):
                groups.add(group)
        except OSError:
            pass
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def process_tree(root: int) -> list[tuple[int, int]]:
    listed = sh(["ps", "-A", "-o", "pid=,ppid=,pgid="], timeout=10).stdout
    group_of: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    for pid, parent, group in (map(int, line.split()) for line in listed.splitlines()):
        group_of[pid] = group
        children.setdefault(parent, []).append(pid)
    tree, todo = [], [root]
    while todo:
        pid = todo.pop()
        if pid in group_of:
            tree.append((pid, group_of[pid]))
        todo += children.get(pid, [])
    return tree


def kill_tree(root: int) -> None:
    own_group = os.getpgid(0)
    for pid, group in process_tree(root):
        try:
            if group == own_group:
                os.kill(pid, signal.SIGKILL)
            else:
                os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def lock_is_held(name: str) -> bool:
    with lock_file(name) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.removeprefix("export ").partition("=")
        if sep:
            out[k.strip()] = env_value(v)
    return out


def env_value(raw: str) -> str:
    stripped = raw.strip()
    quote = stripped[:1]
    if quote in ("'", '"') and quote in stripped[1:]:
        return stripped[1:stripped.index(quote, 1)]
    return re.split(r"\s#", raw, maxsplit=1)[0].strip().strip('"').strip("'")


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2, default=str))
    os.replace(tmp, p)
