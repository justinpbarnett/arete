from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from . import paths

ENV = "ARETE_LAUNCH_LEASE"
POLL = "ARETE_POLL_LAUNCH"
READMISSION = "ARETE_POLL_READMISSION"
RECOVERY = "ARETE_RECOVERY_GRANT"
CURRENT = ContextVar("task_launch_lease", default=None)


def filename(repo: str, tid: str) -> str:
    identity = json.dumps([repo, str(tid)], ensure_ascii=True).encode()
    return "task-launch-" + hashlib.sha256(identity).hexdigest() + ".lock"


def plain(st):
    return (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
            and st.st_nlink == 1 and not st.st_mode & 0o077)


def private_directory(fd: int) -> None:
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise PermissionError("unsafe task launch state directory")


def state_path() -> Path:
    p = paths.STATE_DIR.absolute()
    if not paths.ON_MAC or p.parts[1:2] not in (("var",), ("tmp",), ("etc",)):
        return p
    alias = p.parts[1]
    with paths.directory_fd(Path("/")) as root:
        st = os.fstat(root)
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise PermissionError("unsafe system root for task launch state")
        link = os.stat(alias, dir_fd=root, follow_symlinks=False)
        if not stat.S_ISLNK(link.st_mode):
            return p
        if (link.st_uid != 0
                or os.readlink(alias, dir_fd=root) not in (f"private/{alias}", f"/private/{alias}")):
            raise PermissionError("unsafe system alias for task launch state")
        with paths.directory_fd(Path("private"), root) as private:
            st = os.fstat(private)
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise PermissionError("unsafe private system root for task launch state")
    # macOS supplies these root aliases; every remaining component stays nofollow.
    return Path("/private", alias, *p.parts[2:])


@contextmanager
def lock_directory(*, create: bool):
    with paths.directory_fd(state_path(), create=create) as state:
        private_directory(state)
        with paths.directory_fd(paths.LOCKS_DIR.relative_to(paths.STATE_DIR), state, create=create) as parent:
            private_directory(parent)
            yield parent


@contextmanager
def opened(repo: str, tid: str, *, create: bool = True):
    # Never unlink these files: replacing an inode would split the launch lock.
    with lock_directory(create=create) as parent, opened_file(parent, filename(repo, tid), create=create) as fd:
        yield fd


@contextmanager
def opened_file(parent: int, name: str, *, create: bool):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
    fd = os.open(name, flags, 0o600, dir_fd=parent)
    try:
        if fd < 3:
            duplicate = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
            os.close(fd)
            fd = duplicate
        st = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not plain(st) or (st.st_dev, st.st_ino) != (named.st_dev, named.st_ino):
            raise PermissionError("unsafe task launch lock file")
        yield fd
    finally:
        os.close(fd)


def take(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def held(repo: str, tid: str) -> bool:
    try:
        with opened(repo, tid, create=False) as fd:
            return not take(fd)
    except FileNotFoundError:
        return False


def held_launches() -> list[str]:
    held = []
    with suppress(FileNotFoundError), lock_directory(create=False) as parent:
        for name in sorted(os.listdir(parent)):
            if re.fullmatch(r"task-launch-[0-9a-f]{64}\.lock", name):
                with suppress(FileNotFoundError), opened_file(parent, name, create=False) as fd:
                    if not take(fd):
                        held.append(name)
    return held


@dataclass
class Lease:
    fd: int
    message: str
    passed: bool = False
    readmission: str = ""
    poll: bool = False
    recovery: str = ""


@contextmanager
def acquiring(repo: str, tid: str):
    with opened(repo, tid) as fd:
        if not take(fd):
            raise RuntimeError(f"{repo}/{tid} already has a starting child")
        nonce = os.urandom(32).hex()
        os.ftruncate(fd, 0)
        os.pwrite(fd, nonce.encode(), 0)
        st = os.fstat(fd)
        lease = Lease(fd, json.dumps([repo, str(tid), fd, st.st_dev, st.st_ino, nonce]))
        token = CURRENT.set(lease)
        try:
            yield lease
        finally:
            CURRENT.reset(token)
            # Close only our copy. LOCK_UN here would also unlock the child's
            # inherited open file description across the fork/exec boundary.


def child_environment(keep: tuple[int, ...]) -> tuple[tuple[int, ...], dict]:
    from . import environment

    env = environment.build(environment.INTERNAL_ARETE)
    env.pop(ENV, None)
    env.pop(POLL, None)
    env.pop(READMISSION, None)
    env.pop(RECOVERY, None)
    if lease := CURRENT.get():
        keep = tuple(dict.fromkeys((*keep, lease.fd)))
        env[ENV] = lease.message
        if lease.poll:
            env[POLL] = "1"
        if lease.readmission:
            env[READMISSION] = lease.readmission
        if lease.recovery:
            env[RECOVERY] = lease.recovery
    return keep, env


def passed() -> None:
    if lease := CURRENT.get():
        lease.passed = True


def verify(repo: str, tid: str, *, release: bool = False) -> None:
    try:
        owner, ticket, fd, device, inode, nonce = json.loads(os.environ.get(ENV, ""))
        if (owner, ticket) != (repo, str(tid)):
            raise ValueError("incorrect task lease")
        if any(type(n) is not int for n in (fd, device, inode)) or fd < 3 or not isinstance(nonce, str):
            raise ValueError("invalid descriptor identity")
        st = os.fstat(fd)
        if not plain(st) or (st.st_dev, st.st_ino) != (device, inode):
            raise ValueError("descriptor was replaced")
        with opened(repo, tid, create=False) as probe:
            named = os.fstat(probe)
            if ((named.st_dev, named.st_ino) != (device, inode)
                    or os.pread(fd, 65, 0) != nonce.encode() or len(nonce) != 64):
                raise ValueError("descriptor does not name this launch lease")
            # The independent open must conflict; the inherited description
            # must already own the lock. An arbitrary open FD cannot release it.
            if take(probe) or not take(fd):
                raise ValueError("descriptor does not hold this launch lease")
            if release:
                fcntl.flock(fd, fcntl.LOCK_UN)
        if release:
            os.close(fd)
            os.environ.pop(ENV, None)
            os.environ.pop(POLL, None)
            os.environ.pop(READMISSION, None)
            os.environ.pop(RECOVERY, None)
    except (OSError, ValueError, TypeError) as e:
        raise PermissionError("invalid inherited task launch lease") from e


def from_poll(repo: str, tid: str) -> bool:
    if os.environ.get(POLL) != "1":
        return False
    verify(repo, tid)
    return True


def readmission(repo: str, tid: str) -> str:
    if evidence := os.environ.get(READMISSION, ""):
        verify(repo, tid)
    return evidence


def recovery(repo: str, tid: str) -> str:
    if grant := os.environ.get(RECOVERY, ""):
        verify(repo, tid)
    return grant


def registered(repo: str, tid: str) -> None:
    message = os.environ.get(ENV)
    if not message:
        return
    try:
        owner, ticket, _fd, _device, _inode, _nonce = json.loads(message)
    except (ValueError, TypeError) as e:
        raise PermissionError("invalid inherited task launch lease") from e
    if (owner, ticket) == (repo, str(tid)):
        verify(repo, tid, release=True)
