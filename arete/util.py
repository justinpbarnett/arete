from __future__ import annotations

import fcntl
import fnmatch
import functools
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import SplitResult, urlsplit

from . import paths, sandbox

PROBE_GRACE_SECONDS = 0.2
DEPLOY_TERMINATION_GRACE = 1
TIMED_OUT = 124


def digest(*parts: bytes | str) -> str:
    h = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def engine_commit() -> str:
    from . import codebase

    return codebase.STAMP


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


FOREGROUND_MAINTENANCE = ("maintenance.autoDetach", "gc.autoDetach")


def foreground_maintenance(env: dict) -> dict:
    try:
        held = max(int(env.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        held = 0
    set_false = {env.get(f"GIT_CONFIG_KEY_{i}") for i in range(held) if env.get(f"GIT_CONFIG_VALUE_{i}") == "false"}
    missing = [key for key in FOREGROUND_MAINTENANCE if key not in set_false]
    if not missing:
        return env
    added = {}
    for i, key in enumerate(missing, held):
        added[f"GIT_CONFIG_KEY_{i}"], added[f"GIT_CONFIG_VALUE_{i}"] = key, "false"
    return {**env, **added, "GIT_CONFIG_COUNT": str(held + len(missing))}


def sh(
    cmd: list[str] | str,
    cwd: Path | str | None = None,
    timeout: float | None = 600,
    check: bool = False,
    env: dict | None = None,
    input: str | None = None,
    inherit: bool = True,
    keeps: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    return _sh(cmd, cwd, timeout, check, env, input, inherit, keeps)


def _sh(cmd, cwd=None, timeout=600, check=False, env=None, input=None, inherit=True, keeps=(), *,
        deploy=False, restore_paths=(), binary=False, graceful_timeout=False,
        index_lock: _DeployIndexLock | None = None) -> subprocess.CompletedProcess:
    full_env = foreground_maintenance({**(os.environ if inherit else {}), **(env or {})})
    command = sandbox.protected_command(cmd, cwd, keeps, _deploy=deploy, _restore_paths=restore_paths)
    try:
        res = run_whole_tree(
            command,
            timeout,
            input,
            cwd=str(cwd) if cwd else None,
            shell=isinstance(command, str),
            text=not binary,
            errors=None if binary else "replace",
            env=full_env,
            graceful_timeout=graceful_timeout,
            index_lock=index_lock,
        )
    except subprocess.TimeoutExpired as e:
        res = subprocess.CompletedProcess(
            cmd, TIMED_OUT, stdout=_s(e.stdout), stderr=_s(e.stderr) + f"\n[arete] timed out after {timeout}s"
        )
    except FileNotFoundError as e:
        res = subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(e))
    if check and res.returncode != 0:
        raise RuntimeError(f"{cmd!r} failed ({res.returncode}): {res.stderr.strip() or res.stdout.strip()}")
    return res


def run_whole_tree(command, timeout: float | None, input: str | bytes | None, *,
                   graceful_timeout: bool = False, index_lock: _DeployIndexLock | None = None,
                   **kw) -> subprocess.CompletedProcess:
    stdin = subprocess.PIPE if input is not None else None
    if graceful_timeout:
        kw.update(own_group_args())
    with ExitStack() as cleanup:
        proc = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        if graceful_timeout:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    cleanup.enter_context(closing(stream))
        else:
            cleanup.enter_context(proc)
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except BaseException:
            if graceful_timeout:
                _stop_deploy_process(proc, index_lock)
            else:
                with suppress(Exception):
                    kill_tree(proc.pid)
                proc.kill()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


class DeployStillRunning(RuntimeError):
    pass


def _stop_deploy_process(proc: subprocess.Popen, index_lock: _DeployIndexLock | None) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=DEPLOY_TERMINATION_GRACE)
    except subprocess.TimeoutExpired:
        if index_lock is not None:
            with suppress(OSError, RuntimeError):
                index_lock.observe(proc.pid)
    try:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=DEPLOY_TERMINATION_GRACE)
    except (PermissionError, subprocess.TimeoutExpired) as error:
        raise DeployStillRunning("could not confirm Git stopped after deploy timeout") from error


NETWORK_ERROR = re.compile(
    r"could not resolve host|no such host|network is unreachable|connection (?:timed out|reset|refused)"
    r"|operation timed out|i/o timeout|tls handshake timeout|failed to connect|error connecting to|dial tcp"
    r"|temporary failure in name resolution|ssh: connect to host|unable to access",
    re.IGNORECASE,
)
PROVIDER_BUSY = re.compile(
    r"overloaded|try again later|service unavailable|internal server error|bad gateway|gateway time-?out"
    r"|(?:api error|status|http|error|code)\W{0,3}(?:500|502|503|504|529)\b",
    re.IGNORECASE,
)
RATE_LIMITED = re.compile(r"rate.?limit|\b429\b|usage limit|quota", re.IGNORECASE)
NEVER_SENT = re.compile(
    r"could not resolve host|no such host|temporary failure in name resolution|error connecting to"
    r"|dial tcp|connection refused|tls handshake timeout", re.IGNORECASE,
)
NETWORK_PROBE_HOSTS = (("api.github.com", 443), ("1.1.1.1", 53))
WAKE_NETWORK_WAIT_MINUTES = 3


def is_network_error(text: str) -> bool:
    return bool(NETWORK_ERROR.search(text or ""))


def is_provider_busy(text: str) -> bool:
    return bool(PROVIDER_BUSY.search(text or "")) and not RATE_LIMITED.search(text or "")


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


def sh_with_network(cmd: list[str] | str, rerun: re.Pattern = NETWORK_ERROR, *,
                    before_attempt: Callable[[], None] | None = None, **kw) -> subprocess.CompletedProcess:
    def attempt():
        if before_attempt:
            before_attempt()
        return sh(cmd, **kw)

    res = attempt()
    if res.returncode == 0 or not rerun.search(res.stdout + res.stderr):
        return res
    print(f"{iso()} network error, waiting for the network: {first_line(res.stderr or res.stdout)}", flush=True)
    return attempt() if wait_for_network() else res


def git(args: list[str], cwd: Path, timeout: float = 300, check: bool = True, input: str | None = None,
        env: dict | None = None, *, retry: bool = True, before_attempt: Callable[[], None] | None = None) -> str:
    keeps = ("ssh-agent",) if git_command(args).partition(" ")[0] in NETWORK_GIT else ()
    call = sh_with_network if retry else sh
    if before_attempt and not retry:
        before_attempt()
    attempt = {"before_attempt": before_attempt} if retry and before_attempt else {}
    res = call(["git", *args], cwd=cwd, timeout=timeout, input=input, env=env, keeps=keeps, **attempt)
    if check:
        _check_git(args, res)
    return res.stdout


def git_bytes(args: list[str], cwd: Path, timeout: float = 300) -> bytes:
    res = _sh(["git", *args], cwd=cwd, timeout=timeout, binary=True)
    _check_git(args, res)
    return res.stdout


def _check_git(args: list[str], res: subprocess.CompletedProcess) -> None:
    if res.returncode != 0:
        raise RuntimeError(f"git {git_command(args)}: {git_error(_s(res.stderr or res.stdout))}")


def deploy_fast_forward(src: Path, tip: str) -> None:
    args = ["--no-replace-objects", "merge", "-q", "--ff-only", "--no-autostash", "--no-overwrite-ignore", tip]
    path = _deploy_index_lock(src)
    with ExitStack() as cleanup:
        index_lock = None if path.exists(follow_symlinks=False) else _DeployIndexLock(path, cleanup)
        result = _deploy_git(src, args, deploy=True, index_lock=index_lock)
        if result.returncode == TIMED_OUT and index_lock is not None:
            index_lock.clear(src)
    _check_git(args, result)


@dataclass
class _DeployIndexLock:
    path: Path
    cleanup: ExitStack
    observed: os.stat_result | None = None

    def observe(self, pid: int) -> None:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        self.cleanup.callback(os.close, fd)
        candidate = os.fstat(fd)
        if stat.S_ISREG(candidate.st_mode) and _process_holds_lock(pid, self.path, candidate):
            self.observed = candidate

    def clear(self, src: Path) -> None:
        if self.observed is None:
            return
        with suppress(FileNotFoundError):
            if os.path.samestat(self.path.lstat(), self.observed):
                _sh(["/bin/rm", "-f", str(self.path)], cwd=src, timeout=10, graceful_timeout=True)


def _process_holds_lock(pid: int, path: Path, candidate: os.stat_result) -> bool:
    if sys.platform == "darwin":
        result = _sh(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-FaDi", "--", str(path)],
                     cwd=path.parent, timeout=1, graceful_timeout=True)
        if result.returncode != 0:
            return False
        identity = {f"D{candidate.st_dev:#x}", f"i{candidate.st_ino}"}
        for descriptor in result.stdout.split("\nf")[1:]:
            fields = set(descriptor.splitlines()[1:])
            if identity <= fields and fields.intersection({"aw", "au"}):
                return True
    elif sys.platform == "linux":
        proc = Path(f"/proc/{pid}")
        for fd in (proc / "fd").iterdir():
            with suppress(FileNotFoundError):
                if not os.path.samestat(fd.stat(), candidate):
                    continue
                flags = (proc / "fdinfo" / fd.name).read_text()
                match = re.search(r"^flags:\s*([0-7]+)$", flags, re.MULTILINE)
                if match and int(match[1], 8) & os.O_ACCMODE:
                    return True
    return False


@dataclass(frozen=True)
class DeployRecovery:
    head: str
    tracked: tuple[bytes, ...]
    additions: tuple[bytes, ...]
    absent_additions: tuple[bytes, ...]


def _deploy_read(src: Path, *args: str) -> bytes:
    result = _deploy_git(src, ["--no-replace-objects", "--no-optional-locks", *args],
                         timeout=10, binary=True, graceful_timeout=False)
    _check_git(list(args), result)
    return result.stdout


def _deploy_path_mode(src: Path, name: bytes) -> int | None:
    parts = os.fsdecode(name).split("/")
    try:
        for part in parts[:-1]:
            src /= part
            if not stat.S_ISDIR(src.lstat().st_mode):
                return None
        return (src / parts[-1]).lstat().st_mode
    except FileNotFoundError:
        return None


def _deploy_head(src: Path) -> tuple[str, str]:
    head, *refs = _deploy_read(src, "rev-parse", "HEAD", "--symbolic-full-name", "HEAD").splitlines()
    return head.decode("ascii"), os.fsdecode(refs[0]) if refs else ""


def _deploy_index_lock(src: Path) -> Path:
    raw = _deploy_read(src, "rev-parse", "--path-format=absolute", "--git-path", "index")
    index = Path(os.fsdecode(raw.removesuffix(b"\n"))).resolve()
    return Path(f"{index}.lock")


def deploy_recovery(src: Path, base: str, tip: str) -> DeployRecovery:
    head, ref = _deploy_head(src)
    if ref != f"refs/heads/{base}":
        raise RuntimeError("on another branch")
    raw = _deploy_read(src, "diff", "--name-status", "-z", "--no-renames", "--no-ext-diff",
                       "--ignore-submodules=none", head, tip, "--")
    records = iter(raw.split(b"\0")[:-1])
    tracked, additions = [], []
    for status, name in zip(records, records, strict=True):
        (additions if status == b"A" else tracked).append(name)
    absent = tuple(name for name in additions if _deploy_path_mode(src, name) is None)
    return DeployRecovery(head, tuple(tracked), tuple(additions), absent)


def restore_deploy_paths(src: Path, base: str, recovery: DeployRecovery) -> list[str]:
    if _deploy_head(src) != (recovery.head, f"refs/heads/{base}"):
        raise RuntimeError("HEAD or branch changed during deploy")
    if _deploy_index_lock(src).exists(follow_symlinks=False):
        raise RuntimeError("Git index is locked")

    errors = []
    for restore in (_restore_deploy_index_additions, _remove_deploy_additions, _restore_deploy_tracked):
        try:
            restore(src, recovery)
        except (OSError, RuntimeError) as error:
            errors.append(str(error))
    return errors


def deploy_porcelain(src: Path) -> str:
    status = _deploy_read(src, "-c", "core.quotePath=true", "status", "--porcelain=v1", "--untracked-files=all",
                          "--ignore-submodules=none")
    return ", ".join(_s(status).splitlines())


def _restore_deploy_index_additions(src: Path, recovery: DeployRecovery) -> None:
    if not recovery.additions:
        return
    staged = set(_deploy_read(src, "diff", "--cached", "--name-only", "-z", "--no-renames", "--no-ext-diff",
                              "--diff-filter=A", "--ignore-submodules=none", recovery.head, "--").split(b"\0"))
    names = [name for name in recovery.additions if name in staged]
    if names:
        args = ["update-index", "--force-remove", "-z", "--stdin"]
        _check_git(args, _deploy_git(src, args, input=b"\0".join(names) + b"\0", binary=True))


def _deploy_engine_paths(src: Path, names: Iterable[bytes], additions: Iterable[bytes] = ()) -> tuple[Path, ...]:
    root, engine = sandbox.real_path(src), sandbox.real_path(paths.PACKAGE_DIR)
    files = {root / os.fsdecode(name) for name in names}
    parents = {parent for path in files for parent in path.parents if parent.is_relative_to(engine)}
    exceptions = files | {path for path in parents if not path.exists()}
    for name in additions:
        path = root / os.fsdecode(name)
        if path in parents:
            exceptions.add(path)
        if files.intersection(path.parents):
            exceptions.update(parent for parent in path.parents if files.intersection(parent.parents))
    return tuple(sorted(path for path in exceptions if path.is_relative_to(engine)))


def _restore_deploy_tracked(src: Path, recovery: DeployRecovery) -> None:
    if not recovery.tracked:
        return
    args = ["--no-replace-objects", "--literal-pathspecs", "restore", f"--source={recovery.head}", "--staged", "--worktree",
            "--no-recurse-submodules", "--pathspec-from-file=-", "--pathspec-file-nul"]
    restore_paths = _deploy_engine_paths(src, recovery.tracked, recovery.additions)
    _check_git(args, _deploy_git(src, args, input=b"\0".join(recovery.tracked) + b"\0", binary=True,
                                restore_paths=restore_paths))


def _remove_deploy_additions(src: Path, recovery: DeployRecovery) -> None:
    additions = [(name, _deploy_path_mode(src, name)) for name in recovery.absent_additions]
    files = [name for name, mode in additions if mode is not None and not stat.S_ISDIR(mode)]
    empty_dirs = [name for name, mode in additions if mode is not None and stat.S_ISDIR(mode)
                  and not any((src / os.fsdecode(name)).iterdir())]
    for command, names in ((["/bin/rm", "-f"], files), (["/bin/rmdir"], empty_dirs)):
        if not names:
            continue
        result = _sh([*command, *("./" + os.fsdecode(name) for name in names)], cwd=src, timeout=10,
                     restore_paths=_deploy_engine_paths(src, names))
        if result.returncode != 0:
            raise RuntimeError(f"removing tip additions: {git_error(result.stderr or result.stdout)}")


def _deploy_git(src: Path, args: list[str], *, timeout: float = 30, graceful_timeout: bool = True,
                **kwargs) -> subprocess.CompletedProcess:
    return _sh(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
               cwd=src, timeout=timeout, graceful_timeout=graceful_timeout, **kwargs)


NETWORK_GIT = frozenset({"fetch", "push", "pull", "clone", "ls-remote"})


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


def add_worktree(src: Path, wt: Path, branch: str, start: str, reset: bool = False) -> None:
    with repo_lock(src):
        branch_existed = bool(git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], src, check=False).strip())
        path_existed = wt.exists()
        try:
            git(["worktree", "add", "-q", "--no-track", "-B" if reset else "-b", branch, str(wt), start], src)
        except BaseException:
            if not path_existed:
                git(["worktree", "remove", "--force", str(wt)], src, check=False)
                shutil.rmtree(wt, ignore_errors=True)
            if not branch_existed:
                _drop_branch(src, branch)
            raise


def delete_branch(src: Path, branch: str) -> None:
    with repo_lock(src):
        _drop_branch(src, branch)


def _drop_branch(src: Path, branch: str) -> None:
    git(["branch", "-D", branch], src, check=False)
    if git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], src, check=False).strip():
        return
    for attempt in range(5):
        if not any(git(["config", "--get", f"branch.{branch}.{key}"], src, check=False).strip()
                   for key in ("merge", "remote")):
            break
        removed = sh(["git", "config", "--remove-section", f"branch.{branch}"], cwd=src)
        if removed.returncode == 0:
            break
        if attempt < 4:
            time.sleep(0.1)


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


def one_line(text: str, n: int = 300) -> str:
    return " ".join(text.split())[:n]


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
    for member, group in process_rows("pid=,pgid="):
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


def process_rows(columns: str) -> list[list[int]]:
    try:
        listed = subprocess.run(["/bin/ps", "-A", "-o", columns], capture_output=True, text=True, timeout=10,
                                check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [list(map(int, line.split())) for line in listed.splitlines()]


def process_tree(root: int) -> list[tuple[int, int]]:
    group_of: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    for pid, parent, group in process_rows("pid=,ppid=,pgid="):
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
    try:
        with (paths.LOCKS_DIR / f"{name}.lock").open() as f:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except FileNotFoundError:
        pass
    return False


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = paths.read_confined(path).decode()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
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


UNREADABLE_WARNED: dict[str, tuple] = {}


def read_json(p: Path, default):
    try:
        with open(p, "rb") as f:
            seen = os.fstat(f.fileno())
            raw = f.read()
    except FileNotFoundError:
        return default
    try:
        return json.loads(raw.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        version = (seen.st_dev, seen.st_ino, seen.st_ctime_ns, seen.st_mtime_ns, seen.st_size,
                   hashlib.sha256(raw).digest())
        key = str(Path(p).absolute())
        if UNREADABLE_WARNED.get(key) != version:
            UNREADABLE_WARNED[key] = version
            print(f"{paths.short_path(p)} is not valid JSON, using the default: {e}", file=sys.stderr)
        return default


def flush_fd(fd: int) -> None:
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def flush_file(p: Path) -> None:
    fd = os.open(p, os.O_RDONLY)
    try:
        flush_fd(fd)
    finally:
        os.close(fd)


def flush_folder(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_durably(p: Path, data: bytes, mode: int | None = None, check=None, *, before_replace=None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    draft = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode if mode is not None else 0o644)
            f.write(data)
            f.flush()
            if check:
                check(draft)
            flush_fd(f.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(draft, p)
    except BaseException:
        draft.unlink(missing_ok=True)
        raise
    flush_folder(p.parent)


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(p, json.dumps(data, indent=2, default=str).encode())


def log_ref(ref: str, line: str) -> None:
    print(f"{iso()} {ref}: {line}", flush=True)


GROUP_STOP_SECONDS = 10


def group_alive(proc: subprocess.Popen) -> bool:
    proc.poll()
    try:
        os.killpg(proc.pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def stop_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            break
        deadline = time.monotonic() + GROUP_STOP_SECONDS
        while group_alive(proc) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not group_alive(proc):
            break
    proc.wait()


def nonnegative_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        found = float(value)
    except ValueError:
        return None
    return found if math.isfinite(found) and found >= 0 else None


@functools.cache
def github_token() -> str:
    if paths.ISOLATED:
        return ""
    res = sh(["gh", "auth", "token"], timeout=10)
    return res.stdout.strip() if res.returncode == 0 else ""


class NamedTlsConnection(http.client.HTTPSConnection):
    def __init__(self, address: str, port: int, hostname: str, timeout: float | None = None):
        options = {} if timeout is None else {"timeout": timeout}
        super().__init__(hostname, port, context=ssl.create_default_context(), **options)
        self.address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def https_upgrade(resp: http.client.HTTPResponse, port: int | None) -> SplitResult | None:
    try:
        moved = urlsplit(resp.getheader("Location") or "")
        if resp.status not in (301, 302, 307, 308) or moved.scheme != "https" or not moved.hostname:
            return None
        return moved if (moved.port or 443) == port else None
    except ValueError:
        return None


def write_bytes_atomic(path: Path, data: bytes) -> None:
    write_durably(path, data, 0o600)


@contextmanager
def repeating(seconds: float, action, name: str, *, wait_on_exit: bool = True):
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(seconds):
            action()

    beater = threading.Thread(target=beat, name=name, daemon=True)
    beater.start()
    try:
        yield
    finally:
        stop.set()
        if wait_on_exit:
            beater.join()
