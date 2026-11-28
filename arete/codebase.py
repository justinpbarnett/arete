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
    found += (CHECKOUT / "arete" / "prompts").glob("*.md")
    found += (CHECKOUT / "arete" / "schemas").glob("*.json")
    found += (CHECKOUT / "arete" / "durable").glob("*.mjs")
    found += (CHECKOUT / "arete" / "durable").glob("*.json")
    found += config_data_files()
    return sorted(found)


def config_data_files() -> list[Path]:
    package = CHECKOUT / "arete"
    return [
        *(package / "defaults").glob("*.toml"),
        *(package / "adws").glob("*.toml"),
        *(package / "routes").glob("*.toml"),
        *(package / "config_schemas").glob("*.json"),
    ]


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
    env = {k: v for k in ("PATH", "HOME", "TMPDIR", "USER", "LANG", "LC_ALL", "TZ") if (v := os.environ.get(k)) is not None}
    try:
        done = subprocess.run(["git", "--no-optional-locks", "-C", str(CHECKOUT), *args], check=False,
                              capture_output=True, text=True, timeout=GIT_SECONDS, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def checkout_head() -> str:
    return (git("rev-parse", "--verify", "--short=12", "HEAD") or "").strip()


def serve_revisions(beat: dict) -> str:
    revisions = []
    if beat.get("engine"):
        revisions.append(f"running code {beat['engine']}")
    if "checkout_head" in beat:
        revisions.append(f"checkout HEAD {beat['checkout_head'] or 'unavailable'}")
    return ", ".join(revisions)


def head_and_dirt() -> tuple[str, bool]:
    head = checkout_head()
    status = git("status", "--porcelain", "--", "arete") if head else None
    return head, any("__pycache__" not in line for line in (status or "").splitlines())


def clean_release(short_head: str) -> str:
    head = (git("rev-parse", "--verify", "HEAD") or "").strip()
    if (re.fullmatch(r"[0-9a-f]{40}", head) and head.startswith(short_head)
            and git("status", "--porcelain", "--untracked-files=no") == ""
            and (git("rev-parse", "--verify", "HEAD") or "").strip() == head):
        return head
    return ""


def reload(count: int) -> None:
    os.environ[RELOADS] = str(count)
    time.sleep(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def load(*, templates: bool = False) -> str:
    global RELEASE, STAMP
    if STAMP:
        return STAMP
    before, _ = inventory()
    import_everything()
    if templates:
        from . import paths, prompt_loader

        prompt_loader.load_templates(paths.PROMPTS_DIR)
    head, dirty = head_and_dirt()
    release = clean_release(head) if head and not dirty else ""
    after, newest = inventory()
    moved = before != after or newest > arete.IMPORTED_NS
    reloads = int(os.environ.get(RELOADS) or 0)
    if moved and reloads < MAX_RELOADS:
        reload(reloads + 1)
    RELEASE = release if not moved else ""
    stamp = (head + (f"+dirty.{after[:8]}" if dirty else "")) if head else f"code.{after[:12]}"
    STAMP = stamp + ("+moving" if moved else "")
    return STAMP
