from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import subprocess
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
def lock(name: str):
    paths.ensure_dirs()
    with open(paths.LOCKS_DIR / f"{name}.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
