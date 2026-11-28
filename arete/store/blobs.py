from __future__ import annotations

import gzip
import hashlib
import re
from pathlib import Path

from .. import paths, util
from .db import reader, tx

TABLES = {"blobs": "kept"}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BLOB_BYTES = 16 * 1024 * 1024


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blobs_dir() -> Path:
    return paths.STATE_DIR / "blobs"


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
    conn.execute(
        "INSERT OR IGNORE INTO blobs (sha256, kind, bytes, created) VALUES (?, ?, ?, ?)",
        (sha256, kind, size, util.iso()),
    )


def put(data: bytes, kind: str, *, conn=None) -> str:
    sha256, _ = write_file(data)
    if conn is not None:
        record_row(conn, sha256, kind, len(data))
        return sha256
    with tx() as c:
        record_row(c, sha256, kind, len(data))
    return sha256


def put_verified(expected_sha256: str, data: bytes, kind: str = "upload") -> dict:
    validate_sha256(expected_sha256)
    actual = digest(data)
    if actual != expected_sha256:
        raise ValueError(f"body sha256 {actual} does not match path {expected_sha256}")
    write_file(data, expected_sha256)
    with tx() as c:
        record_row(c, expected_sha256, kind, len(data))
        row = c.execute("SELECT sha256, kind, bytes, created FROM blobs WHERE sha256 = ?", (expected_sha256,)).fetchone()
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


def get(sha256: str, connect=reader) -> tuple[dict, bytes] | None:
    try:
        validate_sha256(sha256)
    except ValueError:
        return None
    with connect() as c:
        row = c.execute("SELECT sha256, kind, bytes, created FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
    if row is None:
        return None
    data = read(sha256)
    if data is None:
        return None
    return dict(row), data
