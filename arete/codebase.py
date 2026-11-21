from __future__ import annotations

import hashlib
import importlib
import os
import pkgutil
import re
import subprocess
import sys
import time
from pathlib import Path

import arete

CHECKOUT = Path(__file__).resolve().parent.parent
RELOADS = "ARETE_CODE_RELOADS"
MAX_RELOADS = 5
GIT_SECONDS = 30
STAMP = ""
RELEASE = ""


def code_files() -> list[Path]:
    found = [p for p in (CHECKOUT / "arete").rglob("*.py") if "__pycache__" not in p.parts]
    found += (CHECKOUT / "adws").glob("*.py")
    return sorted(found)


def inventory() -> tuple[str, int]:
    digest, newest = hashlib.sha256(), 0
    for file in code_files():
        try:
            data, stat = file.read_bytes(), file.stat()
        except OSError:
            continue
        name = file.relative_to(CHECKOUT).as_posix().encode()
        digest.update(len(name).to_bytes(8, "big") + name + len(data).to_bytes(8, "big") + data)
        newest = max(newest, stat.st_mtime_ns)
    return digest.hexdigest(), newest


def import_everything() -> None:
    failed: list[str] = []
    names = ["arete"] + [m.name for m in pkgutil.walk_packages(arete.__path__, "arete.", onerror=failed.append)]
    kept = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        for name in [*names, *failed]:
            try:
                importlib.import_module(name)
            except Exception as e:
                raise SystemExit(f"arete: cannot import {name}: {type(e).__name__}: {e}") from e
    finally:
        sys.dont_write_bytecode = kept


def git(*args: str) -> str | None:
    try:
        done = subprocess.run(["git", "--no-optional-locks", "-C", str(CHECKOUT), *args], check=False,
                              capture_output=True, text=True, timeout=GIT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def head_and_dirt() -> tuple[str, str | None]:
    head = (git("rev-parse", "HEAD") or "").strip()
    return head, git("status", "--porcelain", "--untracked-files=all") if head else None


def reload(count: int) -> None:
    os.environ[RELOADS] = str(count)
    time.sleep(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def load() -> str:
    global RELEASE, STAMP
    if STAMP:
        return STAMP
    before, _ = inventory()
    import_everything()
    head, status = head_and_dirt()
    changed = git("status", "--porcelain", "--", "arete", "adws") if status else ""
    dirty = any("__pycache__" not in line for line in (changed or "").splitlines())
    after, newest = inventory()
    moved = before != after or newest > arete.IMPORTED_NS
    reloads = int(os.environ.get(RELOADS) or 0)
    if moved and reloads < MAX_RELOADS:
        reload(reloads + 1)
    if (re.fullmatch(r"[0-9a-f]{40}", head) and status == "" and not moved
            and (git("rev-parse", "HEAD") or "").strip() == head):
        RELEASE = head
    stamp = (head[:12] + (f"+dirty.{after[:8]}" if dirty else "")) if head else f"code.{after[:12]}"
    STAMP = stamp + ("+moving" if moved else "")
    return STAMP
