from __future__ import annotations

import contextlib
import io
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path

from . import codebase, paths, util
from .store import db as foundation
from .store import devices as device_store

LOCK_NAME = "engine-copy"
STAGE_PREFIX = ".stage-"
STAGE_GRACE_SECONDS = 60
_SAFE_SHA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_HEX_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


def safe_sha(sha: str) -> str:
    text = str(sha or "").strip()
    if not text or not _SAFE_SHA.fullmatch(text) or text in (".", ".."):
        raise ValueError(f"unsafe engine sha: {sha!r}")
    return text


def engine_dir() -> Path:
    return paths.STATE_DIR / "engine"


def copy_path(sha: str) -> Path:
    return engine_dir() / safe_sha(sha)


def _make_readonly(root: Path) -> None:
    dirs: list[Path] = []
    for current, _sub, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs.append(current_path)
        for name in files:
            entry = current_path / name
            if entry.is_symlink():
                continue
            mode = entry.lstat().st_mode
            os.chmod(entry, stat.S_IMODE(mode) & ~0o222)
    for d in reversed(dirs):
        mode = d.lstat().st_mode
        os.chmod(d, stat.S_IMODE(mode) & ~0o222)


def _remove_tree(root: Path) -> None:
    if not root.exists() and not root.is_symlink():
        return
    if root.is_symlink():
        root.unlink(missing_ok=True)
        return
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    for current, subdirs, files in os.walk(root, topdown=True, followlinks=False):
        cur = Path(current)
        with contextlib.suppress(OSError):
            os.chmod(cur, 0o700)
        for d in subdirs:
            with contextlib.suppress(OSError):
                os.chmod(cur / d, 0o700)
        for name in files:
            entry = cur / name
            if not entry.is_symlink():
                with contextlib.suppress(OSError):
                    os.chmod(entry, 0o600)
    shutil.rmtree(root, ignore_errors=True)


def _extract_git_tree(source_root: Path, sha: str, stage: Path) -> bool:
    if not _HEX_COMMIT.fullmatch(sha):
        return False
    env = {k: v for k in ("PATH", "HOME", "TMPDIR", "USER", "LANG", "LC_ALL", "TZ") if (v := os.environ.get(k)) is not None}
    try:
        verify = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(source_root), "cat-file", "-t", sha],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        if verify.returncode != 0 or verify.stdout.strip() != "commit":
            return False
        archive = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(source_root), "archive", "--format=tar", sha, "arete"],
            check=False,
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        if archive.returncode != 0 or not archive.stdout:
            return False
    except (OSError, subprocess.SubprocessError):
        return False
    stage.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tf:
        tf.extractall(stage, filter="data")
    return (stage / "arete" / "__init__.py").is_file()


def _copy_working_tree(source_root: Path, stage: Path) -> None:
    src_pkg = source_root / "arete"
    if not src_pkg.is_dir():
        raise FileNotFoundError(f"engine package not found at {src_pkg}")
    shutil.copytree(
        src_pkg,
        stage / "arete",
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def ensure(sha: str, source_root: Path | None = None) -> Path:
    sha = safe_sha(sha)
    base = engine_dir()
    base.mkdir(parents=True, exist_ok=True)
    target = base / sha
    if (target / "arete" / "__init__.py").is_file():
        return target
    src = (source_root or codebase.CHECKOUT).resolve()
    paths.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    with util.lock(LOCK_NAME, wait=True):
        if (target / "arete" / "__init__.py").is_file():
            return target
        stage = base / f"{STAGE_PREFIX}{sha}-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            stage.mkdir(parents=True, exist_ok=True)
            if not _extract_git_tree(src, sha, stage):
                _copy_working_tree(src, stage)
            (stage / ".engine_sha").write_text(f"{sha}\n", encoding="utf-8")
            _make_readonly(stage)
            try:
                os.rename(stage, target)
            except OSError:
                if (target / "arete" / "__init__.py").is_file():
                    _remove_tree(stage)
                    return target
                raise
            return target
        except BaseException:
            _remove_tree(stage)
            raise


def prune_unused(*, conn=None) -> list[str]:
    base = engine_dir()
    if not base.is_dir():
        return []
    if conn is not None:
        active = device_store.active_engine_shas(conn)
    else:
        if not paths.DB_FILE.is_file():
            return []
        try:
            with foundation.reader() as c:
                active = device_store.active_engine_shas(c)
        except (OSError, foundation.NoDatabase, foundation.IncompatibleSchema):
            return []
    removed: list[str] = []
    now_ts = time.time()
    paths.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    with util.lock(LOCK_NAME, wait=True):
        if not base.is_dir():
            return []
        for entry in sorted(base.iterdir()):
            name = entry.name
            if name.startswith(STAGE_PREFIX):
                with contextlib.suppress(OSError):
                    if now_ts - entry.stat().st_mtime > STAGE_GRACE_SECONDS:
                        _remove_tree(entry)
                continue
            if name in active:
                continue
            _remove_tree(entry)
            removed.append(name)
    return removed


def rewrite_cli_command(command: list[str], engine_sha: str, source_root: Path | None = None) -> tuple[list[str], Path | None]:
    cli_prefix = paths.cli_command()
    if len(command) < len(cli_prefix) or command[: len(cli_prefix)] != cli_prefix:
        return command, None
    args = command[len(cli_prefix) :]
    copy_root = ensure(engine_sha, source_root=source_root)
    return paths.cli_command(*args, engine_root=copy_root), copy_root
