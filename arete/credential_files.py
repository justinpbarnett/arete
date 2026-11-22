from __future__ import annotations

import fcntl
import os
import re
import stat
from contextlib import contextmanager

from . import paths, util

KEY_REF = re.compile(r"file:(token-signing-[0-9a-f]{32})")
TAG = re.compile(r"[0-9a-f]{8}")
CLIENT_TEXT = re.compile(r"ar_([0-9a-f]{8})_([0-9a-f]{8})_[A-Za-z0-9_-]+")


def read_private(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError(f"{path}: expected a private regular file with mode 0600")
        value = file.read(limit + 1)
        if len(value) > limit:
            raise ValueError(f"{path}: credential file is too large")
        return value


def key_path(ref):
    match = KEY_REF.fullmatch(ref)
    if not match:
        raise ValueError("Invalid token signing key reference")
    return paths.STATE_DIR / match[1]


def read_key(ref):
    key = read_private(key_path(ref), 32)
    if key is None or len(key) != 32:
        raise OSError("The token signing key is missing or invalid")
    return key


def client_path(tag):
    if not TAG.fullmatch(tag):
        raise ValueError("Invalid Arete instance tag")
    return paths.TOKEN_CLIENT_DIR / f"{tag}.token"


def read_client(tag):
    path = client_path(tag)
    try:
        value = read_private(path, 256)
        return value.decode("ascii").strip() if value else None
    except ValueError:
        return None


def write_client(tag, text):
    path = client_path(tag)
    if path.parent.resolve().is_relative_to(paths.CONFIG_DIR.resolve()):
        raise OSError("The client credential directory must be outside the configuration directory")
    with client_lock(path):
        util.write_bytes_atomic(path, (text + "\n").encode("ascii"))


@contextmanager
def client_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def forget_client(tag, text):
    path = client_path(tag)
    with client_lock(path):
        if read_client(tag) == text:
            path.unlink(missing_ok=True)
