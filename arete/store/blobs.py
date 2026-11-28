from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import re
import threading
from pathlib import Path

from .. import paths, util
from .db import (
    BLOBS_RETENTION_SCHEMA,
    RUNS_PINS_SCHEMA,
    STEPS_LIFECYCLE_SCHEMA,
    migration_applied,
    reader,
    tx,
)

TABLES = {"blobs": "kept"}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BLOB_BYTES = 16 * 1024 * 1024
_BLOBS_THREAD_LOCK = threading.RLock()
_BLOBS_LOCAL = threading.local()


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blobs_dir() -> Path:
    return paths.STATE_DIR / "blobs"


@contextlib.contextmanager
def lock():
    """Hold one exclusive flock on blobs/ across file writes/deletes and DB commits."""
    folder = blobs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    resolved = folder.resolve()
    with _BLOBS_THREAD_LOCK:
        depth = getattr(_BLOBS_LOCAL, "depth", 0)
        held_path = getattr(_BLOBS_LOCAL, "path", None)
        if depth > 0 and held_path == resolved:
            _BLOBS_LOCAL.depth = depth + 1
            try:
                yield
            finally:
                _BLOBS_LOCAL.depth = depth
            return
        with paths.directory_fd(resolved, create=True) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            prev_depth, prev_path = depth, held_path
            _BLOBS_LOCAL.depth = depth + 1
            _BLOBS_LOCAL.path = resolved
            try:
                yield
            finally:
                _BLOBS_LOCAL.depth = prev_depth
                _BLOBS_LOCAL.path = prev_path
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)


def blob_path(sha256: str) -> Path:
    validate_sha256(sha256)
    return blobs_dir() / sha256[:2] / f"{sha256}.gz"


def write_file(data: bytes, sha256: str | None = None) -> tuple[str, Path]:
    actual = digest(data)
    if sha256 is not None and actual != sha256:
        raise ValueError(f"blob sha256 mismatch: expected {sha256}, got {actual}")
    target = blob_path(actual)
    if not target.is_file():
        compressed = gzip.compress(data, mtime=0)
        util.write_durably(target, compressed, 0o600)
    return actual, target


def record_row(conn, sha256: str, kind: str, size: int) -> None:
    if migration_applied(BLOBS_RETENTION_SCHEMA, conn):
        conn.execute(
            "INSERT INTO blobs (sha256, kind, bytes, created, purged_at, purge_reason) "
            "VALUES (?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(sha256) DO UPDATE SET purged_at = NULL, purge_reason = NULL "
            "WHERE blobs.purged_at IS NOT NULL",
            (sha256, kind, size, util.iso()),
        )
        return
    conn.execute(
        "INSERT OR IGNORE INTO blobs (sha256, kind, bytes, created) VALUES (?, ?, ?, ?)",
        (sha256, kind, size, util.iso()),
    )


def put(data: bytes, kind: str, *, conn=None) -> str:
    if conn is not None:
        sha256, _ = write_file(data)
        record_row(conn, sha256, kind, len(data))
        return sha256
    with lock():
        sha256, _ = write_file(data)
        with tx() as c:
            record_row(c, sha256, kind, len(data))
        return sha256


def put_verified(expected_sha256: str, data: bytes, kind: str = "upload") -> dict:
    validate_sha256(expected_sha256)
    actual = digest(data)
    if actual != expected_sha256:
        raise ValueError(f"body sha256 {actual} does not match path {expected_sha256}")
    with lock():
        write_file(data, expected_sha256)
        with tx() as c:
            record_row(c, expected_sha256, kind, len(data))
            row = c.execute(
                "SELECT sha256, kind, bytes, created FROM blobs WHERE sha256 = ?",
                (expected_sha256,),
            ).fetchone()
    return dict(row)


def read(sha256: str) -> bytes | None:
    try:
        target = blob_path(sha256)
    except ValueError:
        return None
    if not target.is_file():
        return None
    raw = gzip.decompress(target.read_bytes())
    if digest(raw) != sha256:
        raise ValueError(f"blob {sha256} failed integrity check")
    return raw


def meta(sha256: str, connect=reader) -> dict | None:
    try:
        validate_sha256(sha256)
    except ValueError:
        return None
    with connect() as c:
        if migration_applied(BLOBS_RETENTION_SCHEMA, c):
            row = c.execute(
                "SELECT sha256, kind, bytes, created, purged_at, purge_reason FROM blobs WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT sha256, kind, bytes, created, NULL AS purged_at, NULL AS purge_reason "
                "FROM blobs WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
    return dict(row) if row is not None else None


def get(sha256: str, connect=reader) -> tuple[dict, bytes] | None:
    row = meta(sha256, connect=connect)
    if row is None or row.get("purged_at"):
        return None
    data = read(sha256)
    if data is None:
        return None
    return row, data


def purged_input_for_step(
    run_id: str,
    step_name: str = "",
    *,
    step_id: int | None = None,
    connect=reader,
) -> tuple[str, str] | None:
    """Return (sha256, purged_at) if any blob referenced by step_inputs for this step is purged."""
    with connect() as c:
        if not migration_applied(RUNS_PINS_SCHEMA, c) or not migration_applied(BLOBS_RETENTION_SCHEMA, c):
            return None
        if step_id is not None:
            row = c.execute(
                "SELECT b.sha256, b.purged_at "
                "FROM step_inputs si "
                "JOIN blobs b ON b.sha256 = si.sha256 "
                "WHERE si.step_id = ? AND b.purged_at IS NOT NULL "
                "ORDER BY b.purged_at ASC, b.sha256 ASC LIMIT 1",
                (step_id,),
            ).fetchone()
        elif not run_id:
            return None
        elif not step_name:
            row = c.execute(
                "SELECT b.sha256, b.purged_at "
                "FROM step_inputs si "
                "JOIN steps s ON s.id = si.step_id "
                "JOIN blobs b ON b.sha256 = si.sha256 "
                "WHERE s.run_id = ? AND b.purged_at IS NOT NULL "
                "ORDER BY b.purged_at ASC, b.sha256 ASC LIMIT 1",
                (run_id,),
            ).fetchone()
        elif migration_applied(STEPS_LIFECYCLE_SCHEMA, c):
            row = c.execute(
                "SELECT b.sha256, b.purged_at "
                "FROM step_inputs si "
                "JOIN steps s ON s.id = si.step_id "
                "JOIN blobs b ON b.sha256 = si.sha256 "
                "WHERE s.run_id = ? AND (s.step = ? OR s.node = ?) AND b.purged_at IS NOT NULL "
                "ORDER BY b.purged_at ASC, b.sha256 ASC LIMIT 1",
                (run_id, step_name, step_name),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT b.sha256, b.purged_at "
                "FROM step_inputs si "
                "JOIN steps s ON s.id = si.step_id "
                "JOIN blobs b ON b.sha256 = si.sha256 "
                "WHERE s.run_id = ? AND s.step = ? AND b.purged_at IS NOT NULL "
                "ORDER BY b.purged_at ASC, b.sha256 ASC LIMIT 1",
                (run_id, step_name),
            ).fetchone()
    if row is None:
        return None
    return str(row["sha256"]), str(row["purged_at"])
