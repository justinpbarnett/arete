from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import paths


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


def git(args: list[str], cwd: Path, timeout: float = 300, check: bool = True, input: str | None = None) -> str:
    res = sh_with_network(["git", *args], cwd=cwd, timeout=timeout, input=input)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {git_error(res.stderr or res.stdout)}")
    return res.stdout


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


def count(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def tail(text: str, n: int = 4000) -> str:
    return text if len(text) <= n else "...\n" + text[-n:]


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


@contextmanager
def lock(name: str, wait: bool = False):
    paths.ensure_dirs()
    with open(paths.LOCKS_DIR / f"{name}.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        f.truncate(0)
        f.write(str(os.getpid()))
        f.flush()
        yield True


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)
