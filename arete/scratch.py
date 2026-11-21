from __future__ import annotations

import ctypes
import errno
import fcntl
import hmac
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path

from . import launchd, paths, sandbox, util
from .store import runs as run_store

PREFIX = paths.SCRATCH_PREFIX
ENV_NAME = paths.SCRATCH_ENV
LIVE = ("prepared", "active")
OPENING = ("initializing", *LIVE)
FOLDERS = ("fixtures", "config", "state", "out")
def attempt_env(a: Attempt) -> dict[str, str]:
    return {ENV_NAME: a.root, "ARETE_CONFIG_DIR": a.root + "/config", "ARETE_STATE_DIR": a.root + "/state",
            "ARETE_LOG_DIR": a.root + "/state/logs", "ARETE_WORKTREES_DIR": a.root + "/state/worktrees"}


SLOTS = ("config/config.toml", "state/arete.db", "fixtures/config.toml", "fixtures/fixture.db", "out/output.txt")
MAX_LISTED = 500
MAX_HOLDERS = 50
MAX_DIAGNOSTIC = 16000
EXISTED = "existed before the attempt"
LINKED = "has more than one link"
ELSEWHERE = "on another device"
UNCLASSIFIED = "cannot be classified"
MISSING = "registered entry missing"
MAX_SAVED_BYTES = 16 * 1024 * 1024
HOOK = {"root": "", "id": "", "saves": ""}
LEASES: dict[str, int] = {}
RUN_BASELINES: dict[str, dict[int, int] | None] = {}
RUN_LABELS: dict[str, dict | None] = {}
MAX_RECORD_BYTES = 1024 * 1024
REPLACED_RECORD_READS = 5
SECRET = "signing.secret"
NEXT_SECRET = "signing.secret.next"
ROTATION_MARKER = "rotation.marker"
SETTLED = "settled"
RECEIPTS = "settlement-receipts"
SETTLE_AFTER_SECONDS = 3600
MAX_INSPECTED_BYTES = 16 * 1024 * 1024
SELECTOR_FILE = re.compile(r"^[0-9a-f]{64}\.json$")
PRODUCING: ContextVar[Attempt | None] = ContextVar("verification_attempt", default=None)
IN_GROUP = "in a producer group"
ORPHAN = "orphan of a producer"
SUBMITTED = "submitted launchd job"
UNCLASSIFIED_HOLDER = "unclassified"
WAITING = "waiting for registered verification producers to exit; authority revoked"
GATED_EXEC = ("import os,sys; fd=int(sys.argv[1]); ready=os.read(fd,1); os.close(fd); "
              "ready==b'1' or sys.exit(125); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)")


def identity(st: os.stat_result) -> list[int]:
    return [st.st_dev, st.st_ino]


def checked(st: os.stat_result, expected: list[int], directory: bool = False) -> None:
    kind = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode) and st.st_nlink == 1
    if not kind or st.st_uid != os.getuid() or identity(st) != expected:
        raise OSError(errno.ESTALE, "resource replaced or linked")


@dataclass(frozen=True)
class Attempt:
    selector: str
    token: str
    run_id: str
    step: str
    workspace: str
    root: str
    run_dir: str
    directories: dict
    files: dict


def open_marker(run_dir: Path, step: str) -> Path:
    return run_dir / f"{step}-scratch-open.json"


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def create_secret(fd: int, name: str) -> None:
    key = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(key, "wb") as out:
        out.write(secrets.token_bytes(32))
        out.flush()
        os.fsync(key)


def read_secret(fd: int, name: str = SECRET) -> bytes:
    key = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(key, "rb") as source:
        st = os.fstat(key)
        checked(st, identity(st))
        if st.st_mode & 0o077:
            raise PermissionError("scratch signing secret is not private")
        secret = source.read(33)
    if len(secret) != 32:
        raise PermissionError("invalid scratch authority")
    return secret


@contextmanager
def authority_fd(create: bool = False):
    if create:
        with paths.directory_fd(sandbox.AUTHORITY.parent) as parent:
            try:
                os.mkdir(sandbox.AUTHORITY.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
    with paths.directory_fd(sandbox.AUTHORITY) as fd:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise PermissionError("scratch authority is not private")
        yield fd


@contextmanager
def private_directory(fd: int, folder: str, *, create: bool = False):
    with paths.directory_fd(Path(folder), fd, create=create) as directory:
        st = os.fstat(directory)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise PermissionError(f"{folder} is not private")
        yield directory


def stored_in(fd: int, folder: str) -> list[str]:
    try:
        with private_directory(fd, folder) as directory:
            return sorted(f"{folder}/{name}" for name in os.listdir(directory))
    except FileNotFoundError:
        return []


def stored_files(fd: int) -> list[str]:
    names = [n for n in os.listdir(fd) if n.endswith(".json") or n == "browser-server.pin"]
    return sorted(names + stored_in(fd, SETTLED) + stored_in(fd, RECEIPTS))


def mint_secret(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        try:
            os.stat(SECRET, dir_fd=fd, follow_symlinks=False)
            return
        except FileNotFoundError:
            pass
        if held := stored_files(fd):
            listed = ", ".join(str(sandbox.AUTHORITY / n) for n in held[:5])
            raise PermissionError(
                f"scratch authority {sandbox.AUTHORITY} has no signing.secret but holds {len(held)} records "
                f"({listed}); restore signing.secret, or move the listed records into "
                f"{sandbox.AUTHORITY / 'orphaned'} once their "
                "roots are gone")
        create_secret(fd, SECRET)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def registry(create: bool = False):
    if not sandbox.can_confine(False) or os.getuid() == 0 or sandbox.AUTHORITY == paths.NOWHERE:
        raise sandbox.ConfinementUnavailable("local producers cannot protect the scratch authority store")
    with authority_fd(create) as fd:
        if create:
            mint_secret(fd)
        yield fd, read_secret(fd)


def load(fd: int, key: bytes, selector: str) -> dict:
    if len(selector) != 64 or any(c not in "0123456789abcdef" for c in selector):
        raise ValueError("invalid attempt selector")
    for _ in range(REPLACED_RECORD_READS):
        source = os.open(selector + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(source, "rb") as inp:
            st = os.fstat(source)
            if stat.S_ISREG(st.st_mode) and st.st_nlink == 0:
                continue
            checked(st, identity(st))
            envelope = json.loads(inp.read(MAX_RECORD_BYTES + 1))
            break
    else:
        raise OSError(errno.ESTALE, "resource replaced or linked")
    data = envelope["data"]
    if not hmac.compare_digest(envelope["signature"], hmac.digest(key, encoded(data), "sha256").hex()):
        raise ValueError("unauthenticated scratch record")
    if (data["version"] != 2 or data["attempt"]["selector"] != selector
            or data.get("source_mutations") is not False):
        raise ValueError("inconsistent scratch record")
    return data


def write_atomic(fd: int, name: str, payload: bytes) -> None:
    temporary = secrets.token_hex(16) + ".new"
    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(out, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(out)
    os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)


def store(fd: int, key: bytes, data: dict) -> None:
    signature = hmac.digest(key, encoded(data), "sha256").hex()
    payload = encoded({"data": data, "signature": signature})
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError(f"scratch record is {len(payload)} bytes, over the {MAX_RECORD_BYTES} byte limit")
    write_atomic(fd, data["attempt"]["selector"] + ".json", payload)


class RotationRefused(RuntimeError):
    pass


def rotation_blockers(fd: int) -> list[str]:
    found = [f"run {r['id']} is active" for r in run_store.runs_marked_running()]
    for name in sorted(os.listdir(fd)):
        if name.endswith(".lease"):
            lease = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                found.append(f"attempt lease {name} is held")
            finally:
                os.close(lease)
    if paths.LOCKS_DIR.is_dir():
        for path in sorted(paths.LOCKS_DIR.glob("*.lock")):
            if util.lock_is_held(path.stem):
                found.append(f"engine lock {path.stem} is held")
    return found


def resigned_names(fd: int) -> list[str] | None:
    try:
        source = os.open(ROTATION_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(source, "rb") as inp:
        try:
            return [str(n) for n in json.loads(inp.read(MAX_RECORD_BYTES))["resigned"]]
        except (ValueError, KeyError, TypeError):
            return []


def note_resigned(fd: int, names) -> None:
    write_atomic(fd, ROTATION_MARKER, encoded({"resigned": sorted(names)}))


def rotation_secret(fd: int, started: bool) -> bytes:
    try:
        return read_secret(fd, NEXT_SECRET)
    except FileNotFoundError:
        pass
    except OSError:
        if started:
            raise
        os.unlink(NEXT_SECRET, dir_fd=fd)
    create_secret(fd, NEXT_SECRET)
    os.fsync(fd)
    return read_secret(fd, NEXT_SECRET)


def verifies(fd: int, key: bytes, name: str) -> bool:
    from . import browser

    try:
        if name == browser.PIN:
            browser.load_pin(fd, key)
        elif "/" in name:
            folder, leaf = name.split("/", 1)
            with private_directory(fd, folder) as directory:
                load(directory, key, leaf[:-5])
        else:
            load(fd, key, name[:-5])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def resign(fd: int, old: bytes, new: bytes, name: str) -> None:
    from . import browser

    if name == browser.PIN:
        browser.store_pin(fd, new, browser.load_pin(fd, old))
    elif "/" in name:
        folder, leaf = name.split("/", 1)
        with private_directory(fd, folder) as directory:
            store(directory, new, load(directory, old, leaf[:-5]))
            os.fsync(directory)
    else:
        store(fd, new, load(fd, old, name[:-5]))


def swap_secret(fd: int) -> None:
    os.rename(NEXT_SECRET, SECRET, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)
    with suppress(FileNotFoundError):
        os.unlink(ROTATION_MARKER, dir_fd=fd)
    os.fsync(fd)


def unverified(names: list[str]) -> RotationRefused:
    return RotationRefused("verifies under neither secret: " + ", ".join(names))


def rotation_counts(names, files: list[str]) -> tuple[int, int]:
    from . import browser

    present = [n for n in names if n in files]
    return sum(n != browser.PIN for n in present), int(browser.PIN in present)


def rotate() -> tuple[int, int]:
    from . import browser

    with registry() as (fd, _):
        fcntl.flock(fd, fcntl.LOCK_EX)
        old = read_secret(fd)
        blockers = rotation_blockers(fd)
        if blockers:
            raise RotationRefused("\n".join(blockers))
        listing = os.listdir(fd)
        files = sorted(n for n in listing if n.endswith(".json")) + [n for n in listing if n == browser.PIN]
        marker = resigned_names(fd)
        has_next = NEXT_SECRET in listing
        files += [name for name in stored_in(fd, RECEIPTS) if SELECTOR_FILE.fullmatch(Path(name).name)]
        settled = stored_in(fd, SETTLED)
        files += [name for name in settled if name in (marker or ()) or verifies(fd, old, name)]
        if not has_next:
            bad = [n for n in files if not verifies(fd, old, n)]
            if bad:
                raise unverified(bad)
            if marker is not None:
                os.fsync(fd)
                os.unlink(ROTATION_MARKER, dir_fd=fd)
                return rotation_counts(marker, files)
        new = rotation_secret(fd, marker is not None)
        files += [name for name in settled if name not in files and verifies(fd, new, name)]
        bad = [n for n in files if not verifies(fd, new, n) and not verifies(fd, old, n)]
        if bad:
            raise unverified(bad)
        done = {n for n in files if verifies(fd, new, n)}
        note_resigned(fd, done)
        for name in files:
            if name not in done:
                resign(fd, old, new, name)
                done.add(name)
                note_resigned(fd, done)
        bad = [n for n in files if not verifies(fd, new, n)]
        if bad:
            raise RotationRefused("not signed by the new secret: " + ", ".join(bad))
        swap_secret(fd)
        return rotation_counts(done, files)


def records(strict: bool = False) -> list[dict]:
    found = []
    try:
        with registry() as (fd, key):
            for name in os.listdir(fd):
                if name.endswith(".json"):
                    try:
                        found.append(load(fd, key, name[:-5]))
                    except (OSError, ValueError, KeyError, TypeError):
                        if strict:
                            raise
    except OSError:
        if strict:
            raise
    return found


@dataclass(frozen=True)
class Unreadable:
    path: str
    reason: str
    version: int | None
    workspace: str
    root: str
    state: str
    authenticated: bool
    shaped: bool = False
    run_dir: str = ""


@dataclass(frozen=True)
class View:
    good: list
    unreadable: list
    absent: bool


def absolute_text(value) -> str:
    return value if isinstance(value, str) and os.path.isabs(value) else ""


def claimed(path: str, reason: str, raw: bytes | None, key: bytes | None, name: str) -> Unreadable:
    try:
        envelope = json.loads(raw)
        data = envelope["data"]
        attempt = data["attempt"]
        version = data.get("version")
        state = data.get("state")
        try:
            authenticated = key is not None and hmac.compare_digest(
                envelope["signature"], hmac.digest(key, encoded(data), "sha256").hex())
        except (ValueError, KeyError, TypeError):
            authenticated = False
        shaped = (SELECTOR_FILE.match(name) is not None and attempt.get("selector") == name[:-5] and isinstance(state, str)
                  and data.get("source_mutations") is False and bool(absolute_text(attempt.get("root"))))
        return Unreadable(path, reason, version if isinstance(version, int) and not isinstance(version, bool) else None,
                          absolute_text(attempt.get("workspace")), absolute_text(attempt.get("root")),
                          state if isinstance(state, str) else "", authenticated, shaped,
                          absolute_text(attempt.get("run_dir")))
    except (ValueError, KeyError, TypeError, AttributeError):
        return Unreadable(path, reason, None, "", "", "", False)


def inspected(fd: int, name: str) -> bytes | None:
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except OSError:
        return None
    with os.fdopen(source, "rb") as inp:
        if not stat.S_ISREG(os.fstat(source).st_mode):
            return None
        raw = inp.read(MAX_INSPECTED_BYTES + 1)
    return raw if len(raw) <= MAX_INSPECTED_BYTES else None


def rejection(e: Exception, rotating: bool) -> str:
    reason = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
    if rotating and str(e) == "unauthenticated scratch record":
        reason += "; the key rotation is unfinished, run `arete rotate-authority` again"
    return reason


def survey(*, check=lambda: None) -> View:
    try:
        check()
        with authority_fd() as fd:
            check()
            listing = os.listdir(fd)
            try:
                check()
                key = read_secret(fd)
            except FileNotFoundError:
                key = None
            good, unreadable = [], []
            for name in sorted(n for n in listing if n.endswith(".json")):
                check()
                path = str(sandbox.AUTHORITY / name)
                if key is None:
                    unreadable.append(claimed(path, "no signing secret", inspected(fd, name), key, name))
                    continue
                try:
                    good.append(load(fd, key, name[:-5]))
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, KeyError, TypeError) as e:
                    check()
                    unreadable.append(claimed(path, rejection(e, NEXT_SECRET in listing), inspected(fd, name),
                                              key, name))
            return View(good, unreadable, False)
    except FileNotFoundError:
        check()
        if os.path.lexists(sandbox.AUTHORITY):
            raise
        return View([], [], True)


def path_of(hold) -> str:
    return hold.path if isinstance(hold, Unreadable) else str(sandbox.AUTHORITY / (hold["attempt"]["selector"] + ".json"))


def root_absent(root: str) -> bool:
    if not absolute_text(root):
        return False
    try:
        os.lstat(root)
    except FileNotFoundError:
        return True
    return False


def settled_record(selector: str, workspace: Path) -> bool:
    try:
        with authority_fd() as fd:
            key = read_secret(fd)
            try:
                data = load(fd, key, selector)
            except FileNotFoundError:
                try:
                    with private_directory(fd, SETTLED) as directory:
                        data = load(directory, key, selector)
                except FileNotFoundError:
                    with private_directory(fd, RECEIPTS) as directory:
                        data = load(directory, key, selector)
        attempt = data["attempt"]
        recorded = absolute_text(attempt["workspace"])
        return bool(recorded and data["state"] == "done" and overlap(Path(recorded), workspace.resolve())
                    and root_absent(attempt["root"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def alive(u: Unreadable) -> bool:
    if u.version == 2 and u.shaped and u.state == "done":
        return os.path.lexists(u.root)
    return True


def holds(view: View, workspace: Path, *, check=lambda: None) -> list:
    check()
    ws = workspace.resolve()
    found = []
    for document in view.good:
        check()
        if document["state"] != "done" and overlap(Path(document["attempt"]["workspace"]), ws):
            found.append(document)
    for unreadable in view.unreadable:
        check()
        if unreadable.workspace:
            if not overlap(Path(unreadable.workspace), ws):
                continue
            check()
            if not alive(unreadable):
                continue
        found.append(unreadable)
    return found


def pending(workspace: Path, *, check=lambda: None) -> bool:
    try:
        return bool(holds(survey(check=check), workspace, check=check))
    except (OSError, ValueError, KeyError, TypeError):
        check()
        return os.path.lexists(sandbox.AUTHORITY)


def root_names(view: View | None = None) -> set[str] | None:
    try:
        view = survey() if view is None else view
        if view.absent:
            return None if sandbox.can_confine(False) else set()
        if any(not u.root for u in view.unreadable):
            return None
        return {Path(d["attempt"]["root"]).name for d in view.good} | {Path(u.root).name for u in view.unreadable}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def admitted(fd: int, key: bytes, name: str, now: float) -> bool:
    if now - os.stat(name, dir_fd=fd, follow_symlinks=False).st_mtime <= SETTLE_AFTER_SECONDS:
        return False
    data = load(fd, key, name[:-5])
    return data["state"] == "done" and not os.path.lexists(data["attempt"]["root"])


def move_settled(fd: int, name: str) -> None:
    with private_directory(fd, SETTLED, create=True) as settled:
        os.rename(name, name, src_dir_fd=fd, dst_dir_fd=settled)


def settle_hot(fd: int, key: bytes, failures: list[str]) -> None:
    now = time.time()
    for name in sorted(n for n in os.listdir(fd) if n.endswith(".json")):
        try:
            if not admitted(fd, key, name, now):
                continue
            lease = os.open(name[:-5] + ".lease", os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if admitted(fd, key, name, now):
                move_settled(fd, name)
        except BlockingIOError:
            pass
        except OSError as e:
            failures.append(f"{sandbox.AUTHORITY / name}: {type(e).__name__}: {e}")
        finally:
            os.close(lease)


def open_up(directory: int, device: int) -> None:
    os.fchmod(directory, 0o700)
    for name in os.listdir(directory):
        if stat.S_ISDIR(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                st = os.fstat(child)
                if st.st_dev != device or st.st_uid != os.getuid():
                    raise OSError(errno.ESTALE, "evidence folder crosses a device or owner")
                open_up(child, device)
            finally:
                os.close(child)


def prune_evidence(fd: int, selector: str) -> None:
    try:
        evidence = os.open(selector, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return
    try:
        st = os.fstat(evidence)
        if st.st_uid != os.getuid():
            raise PermissionError("evidence folder is not owned by this user")
        open_up(evidence, st.st_dev)
        if identity(os.stat(selector, dir_fd=fd, follow_symlinks=False)) != identity(st):
            raise OSError(errno.ESTALE, "evidence folder replaced")
    finally:
        os.close(evidence)
    shutil.rmtree(selector, dir_fd=fd)


def prune_settled(fd: int, settled: int, name: str, key: bytes) -> None:
    selector = name[:-5]
    data = load(settled, key, selector)
    attempt = data["attempt"]
    workspace = absolute_text(attempt.get("workspace"))
    if data["state"] != "done" or not workspace or not root_absent(attempt.get("root", "")):
        raise ValueError("settled record does not prove the scratch root is gone")
    with private_directory(fd, RECEIPTS, create=True) as receipts:
        store(receipts, key, {"version": 2, "state": "done", "source_mutations": False,
                              "attempt": {"selector": selector, "workspace": workspace, "root": attempt["root"]}})
        os.fsync(receipts)
    os.fsync(fd)
    prune_evidence(fd, selector)
    with suppress(FileNotFoundError):
        os.unlink(selector + ".lease", dir_fd=fd)
    os.unlink(name, dir_fd=settled)


def prune_old(fd: int, key: bytes, days: float, failures: list[str]) -> None:
    try:
        with private_directory(fd, SETTLED) as settled:
            cutoff = time.time() - days * 86400
            for name in sorted(os.listdir(settled)):
                path = sandbox.AUTHORITY / SETTLED / name
                try:
                    if not SELECTOR_FILE.match(name):
                        failures.append(f"{path}: not a settled record, left as it is")
                    elif os.stat(name, dir_fd=settled, follow_symlinks=False).st_mtime < cutoff:
                        prune_settled(fd, settled, name, key)
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                    failures.append(f"{path}: {type(e).__name__}: {e}")
    except FileNotFoundError:
        pass


def settle(days: float) -> list[str]:
    failures: list[str] = []
    with registry() as (fd, key):
        fcntl.flock(fd, fcntl.LOCK_EX)
        settle_hot(fd, key, failures)
        if NEXT_SECRET not in os.listdir(fd):
            prune_old(fd, key, days, failures)
    return failures


def overlap(a: Path, b: Path) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)


def make(run_id: str, step: str, workspace: Path, run_dir: Path) -> Attempt:
    ws, rd = workspace.resolve(), run_dir.resolve()
    before = RUN_BASELINES[run_id] if run_id in RUN_BASELINES else process_ids()
    labels = RUN_LABELS.get(run_id) if run_id in RUN_BASELINES else service_labels()
    if before is None:
        raise sandbox.ConfinementUnavailable("no producer baseline before repository execution")
    if rd.name != run_id or not step or Path(step).name != step:
        raise ValueError("scratch requires a pinned run and step")
    with registry(True) as (fd, key):
        fcntl.flock(fd, fcntl.LOCK_EX)
        unresolved = holds(survey(), ws)
        if unresolved:
            held = named_holders([h for d in unresolved if isinstance(d, dict) and isinstance(d.get("diagnostic"), dict)
                                  for h in d["diagnostic"].get("holders", [])])
            files = ", ".join(u.path for u in unresolved if isinstance(u, Unreadable))
            raise OSError(errno.EBUSY, "workspace has an unresolved verification attempt"
                          + (f" held by {held}" if held else "") + (f" (unreadable record {files})" if files else ""))
        selector, token = secrets.token_hex(32), secrets.token_hex(8)
        root = ws / f"{PREFIX}{token}{paths.SCRATCH_SUFFIX}"
        directories, files = {}, {}
        a = Attempt(selector, token, run_id, step, str(ws), str(root), str(rd), directories, files)
        with paths.directory_fd(rd) as run_fd:
            run_identity = identity(os.fstat(run_fd))
        data = {"version": 2, "engine": secrets.token_hex(16), "attempt": asdict(a), "run": run_identity,
                "evidence": [], "state": "initializing", "diagnostic": "",
                "archives": [], "producers": {}, "before": sorted(before), "labels": labels,
                "source_mutations": False, "cleanup_version": 3}
        lease = None
        try:
            with paths.directory_fd(ws) as parent:
                data["workspace_identity"] = identity(os.fstat(parent))
                store(fd, key, data)
                lease = os.open(selector + ".lease", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=fd)
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                LEASES[selector] = lease
                os.mkdir(selector, 0o700, dir_fd=fd)
                data["evidence"] = identity(os.stat(selector, dir_fd=fd, follow_symlinks=False))
                store(fd, key, data)
                os.mkdir(root.name, 0o700, dir_fd=parent)
                with paths.directory_fd(Path(root.name), parent) as root_fd:
                    directories[""] = identity(os.fstat(root_fd))
                    for name in FOLDERS:
                        data["attempt"] = asdict(a)
                        store(fd, key, data)
                        os.mkdir(name, 0o700, dir_fd=root_fd)
                        directories[name] = identity(os.stat(name, dir_fd=root_fd, follow_symlinks=False))
                    for name in (".gitignore", *SLOTS):
                        data["attempt"] = asdict(a)
                        store(fd, key, data)
                        folder, _, leaf = name.rpartition("/")
                        with paths.directory_fd(Path(folder), root_fd) as folder_fd:
                            out = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                          dir_fd=folder_fd)
                            with os.fdopen(out, "wb") as target:
                                files[name] = identity(os.fstat(out))
                                if name == ".gitignore":
                                    target.write(b"*\n")
                data["attempt"], data["state"] = asdict(a), "prepared"
                store(fd, key, data)
                with paths.directory_fd(rd) as run_fd:
                    checked(os.fstat(run_fd), run_identity, True)
                    try:
                        marker = os.open(open_marker(rd, step).name,
                                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=run_fd)
                    except FileExistsError:
                        pass
                    else:
                        with os.fdopen(marker, "wb") as output:
                            output.write(encoded({"selector": selector, "run_id": run_id, "step": step}))
        except BaseException:
            try:
                data["attempt"], data["state"] = asdict(a), "pending"
                data["diagnostic"] = "initialization incomplete; original resources retained for preservation"
                store(fd, key, data)
            except OSError:
                pass
            finally:
                LEASES.pop(selector, None)
                if lease is not None:
                    os.close(lease)
            raise
        return a


@contextmanager
def producing(a: Attempt | None):
    token = PRODUCING.set(a)
    try:
        yield
    finally:
        PRODUCING.reset(token)


@dataclass(frozen=True)
class Process:
    pid: int
    identity: int
    parent: int
    group: int
    executable: str


def process_ids(*workspaces: str) -> dict[int, int]:
    return {p.identity: p.parent for p in processes(*workspaces).values()}


def identity_of(pid: int) -> int | None:
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    info = (ctypes.c_uint64 * 7)()
    if lib.proc_pidinfo(pid, 17, 0, info, ctypes.sizeof(info)) != ctypes.sizeof(info):
        return None
    return info[2]


def executable_of(lib, pid: int, short) -> str:
    buffer = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid, buffer, ctypes.sizeof(buffer)) > 0:
        return os.fsdecode(buffer.value)
    return os.fsdecode(bytes(short)[16:32].split(b"\0", 1)[0])


def processes(*workspaces: str) -> dict[int, Process]:
    if not paths.ON_MAC:
        raise sandbox.ConfinementUnavailable("producer lifetime inspection unavailable")
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_listpids.argtypes = (ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int)
    lib.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    lib.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    box = ctypes.CDLL(None)
    box.sandbox_check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    for _ in range(50):
        found: dict[int, Process] = {}
        pids = (ctypes.c_int * (max(lib.proc_listpids(4, os.getuid(), None, 0), 0) // 4 + 1024))()
        size = lib.proc_listpids(4, os.getuid(), pids, ctypes.sizeof(pids))
        settled = 0 < size < ctypes.sizeof(pids)
        for pid in pids[:max(size, 0) // 4]:
            info, after, parent = (ctypes.c_uint64 * 7)(), (ctypes.c_uint64 * 7)(), (ctypes.c_uint32 * 16)()
            if not pid or pid == os.getpid():
                continue
            if lib.proc_pidinfo(pid, 17, 0, info, ctypes.sizeof(info)) != ctypes.sizeof(info):
                kinfo, length = ctypes.create_string_buffer(648), ctypes.c_size_t(648)
                box.sysctl((ctypes.c_int * 4)(1, 14, 1, pid), 4, kinfo, ctypes.byref(length), None, ctypes.c_size_t(0))
                settled = settled and length.value == 648 and kinfo.raw[36] == 5
                continue
            if info[2] in found:
                continue
            outside = workspaces and (box.sandbox_check(pid, None, 0) == 0 or all(box.sandbox_check(
                pid, b"file-write-create", 1, ctypes.c_char_p(os.fsencode(w))) == 1 for w in workspaces))
            if (lib.proc_pidinfo(pid, 13, 0, parent, ctypes.sizeof(parent)) != ctypes.sizeof(parent)
                    or lib.proc_pidinfo(pid, 17, 0, after, ctypes.sizeof(after)) != ctypes.sizeof(after)
                    or after[2] != info[2]):
                settled = False
                continue
            key = -1 if outside else info[3] if parent[1] != 1 else 0
            found[info[2]] = Process(pid, info[2], key, parent[2],
                                     executable_of(lib, pid, parent) if workspaces else "")
        if settled:
            return found
    raise OSError(errno.EBUSY, "producer changed during enumeration")


def producer_group(a: Attempt, producer: str, group: int | None) -> None:
    with registry() as (fd, key):
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = load(fd, key, a.selector)
        if data["attempt"] != asdict(a) or data["state"] not in LIVE:
            raise PermissionError("verification producer authority revoked")
        if producer not in data["producers"]:
            data["producers"][producer] = {"gated": True}
        entry = data["producers"][producer]
        entry["group"] = group
        store(fd, key, data)


def home_dir() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def hidden_folders(with_state: bool = True) -> tuple[Path, ...]:
    home = home_dir()
    state = home / ".local/share/arete"
    return tuple(p for p in (sandbox.AUTHORITY.parent, state, home / "Library/Application Support/arete",
                             *(home / n for n in (*sandbox.SECRET_DIRS, ".claude", ".codex", ".pi")))
                 if with_state or p != state)


def service_command(a: Attempt, command: str, browser_port: int | None = None) -> list[str]:
    if (not sandbox.can_confine(False) or Path(a.workspace) == paths.WORKTREES_DIR.resolve()
            or not Path(a.workspace).is_relative_to(paths.WORKTREES_DIR.resolve())):
        raise sandbox.ConfinementUnavailable("service confinement cannot isolate trusted scratch in this workspace")
    return sandbox.write_confinement(
        Path(a.workspace), [], hidden=hidden_folders(), agent_roots=False, peers=paths.WORKTREES_DIR,
        secrets_denied=True, unix_confined=True,
        denied_ports=(browser_port,) if browser_port else ()) + ["/bin/sh", "-c", command]


DATA_VOLUME = Path("/System/Volumes/Data")


def browser_path(run_dir: str) -> Path:
    return Path(run_dir) / "browser"


def evidence_path(run_dir: str) -> Path:
    return Path(run_dir) / "evidence"


def temp_grants() -> tuple[Path, ...]:
    temp = os.getenv("TMPDIR", "/tmp")
    if not os.path.isabs(temp) or ".." in Path(temp).parts:
        raise sandbox.ConfinementUnavailable(f"TMPDIR {temp!r} is not an absolute path free of '..'")
    return (Path("/tmp").resolve(), Path("/private/tmp"), Path(temp).resolve())


def actor_grants(a: Attempt) -> dict[str, tuple[Path, ...]]:
    evidence = evidence_path(a.run_dir)
    return {"agent": (Path(a.workspace), evidence, *temp_grants()),
            "service": (Path(a.workspace), *map(Path, sandbox.WRITABLE_ALWAYS)),
            "browser": (browser_path(a.run_dir), evidence, *map(Path, sandbox.WRITABLE_DEVICES))}


def traversed(path: Path) -> tuple[set[Path], Path]:
    pending = list(reversed(sandbox.absolute(path).parts[1:]))
    current, seen, links = Path("/"), {Path("/")}, 0
    while pending:
        part = pending.pop()
        try:
            target = Path(os.readlink(current / part)) if part != ".." and (current / part).is_symlink() else None
        except OSError as e:
            raise sandbox.ConfinementUnavailable(f"{path} cannot be checked: {e.strerror}") from e
        if part == "..":
            current = current.parent
        elif target is not None:
            links += 1
            if links > 32:
                raise sandbox.ConfinementUnavailable(f"{path} passes through too many symbolic links")
            current = Path("/") if target.is_absolute() else current
            pending.extend(reversed(target.parts[1:] if target.is_absolute() else target.parts))
        elif part != ".":
            current = current / part
        seen.add(current)
    return seen, current


def folded(path: Path) -> Path:
    return Path(unicodedata.normalize("NFD", unicodedata.normalize("NFD", str(path)).casefold()))


def spellings(path: Path) -> set[Path]:
    real = sandbox.canonical(path)
    names = {Path(path), real}
    if real.is_relative_to(DATA_VOLUME):
        names.add(Path("/") / real.relative_to(DATA_VOLUME))
    return {folded(n) for n in names}


def exposure(path: Path, grants) -> Path | None:
    seen, final = traversed(path)
    ends = spellings(final)
    folders = set().union(ends, *(spellings(d) for d in seen), *(set(e.parents) for e in ends))
    for grant in grants:
        roots = spellings(Path(grant).resolve()) | spellings(Path(grant))
        if any(r.is_relative_to(e) for r in roots for e in ends) or any(
                d.is_relative_to(r) for d in folders for r in roots):
            return Path(grant).resolve()
    return None


def spawn(*args, actor_confined: bool = False, **kwargs) -> subprocess.Popen:
    a = PRODUCING.get()
    if a is None and actor_confined:
        return subprocess.Popen(*args, **kwargs)
    command, *rest = args
    if kwargs.pop("shell", False):
        command = [kwargs.pop("executable", None) or "/bin/sh", "-c", command]
    if a is None:
        return subprocess.Popen(sandbox.protected_command(command, kwargs.get("cwd")), *rest, **kwargs)
    producer = secrets.token_hex(16)
    producer_group(a, producer, None)
    kwargs.pop("process_group", None)
    if not actor_confined:
        with registry() as (fd, key):
            if load(fd, key, a.selector)["state"] == "active":
                raise sandbox.ConfinementUnavailable("active authority requires verified actor confinement")
        command = sandbox.protected_command(command, kwargs.get("cwd"))
    read_gate, release_gate = os.pipe()
    try:
        proc = subprocess.Popen([str(Path(sys.executable).resolve()), "-I", "-c", GATED_EXEC,
                                 str(read_gate), *command], *rest,
                                **{**kwargs, "start_new_session": True,
                                   "pass_fds": (*kwargs.get("pass_fds", ()), read_gate)})
    except OSError:
        producer_group(a, producer, 0)
        raise
    else:
        try:
            producer_group(a, producer, proc.pid)
            os.write(release_gate, b"1")
        except BaseException:
            util.stop_group(proc)
            raise
    finally:
        os.close(read_gate)
        os.close(release_gate)
    return proc


def service_labels() -> dict[str, list[str]] | None:
    return launchd.labels(os.getuid())


def service_reason(p: Process, current: dict[int, Process], unrelated: set[int], table: launchd.Table,
                   labels: dict | None) -> str:
    try:
        job = table.job(p.pid, launchd.bundled(p.executable))
    except launchd.Unreadable:
        return UNCLASSIFIED_HOLDER
    if job is None:
        return ORPHAN
    if not launchd.sealed(job.path):
        return SUBMITTED
    if not job.domain.startswith("pid/"):
        if not isinstance(labels, dict):
            return UNCLASSIFIED_HOLDER
        if job.label not in labels.get(job.domain.split("/")[0], ()):
            return SUBMITTED
    if (identity_of(p.pid) != p.identity or not launchd.sealed(p.executable)
            or not launchd.runs(job.path, p.executable)):
        return UNCLASSIFIED_HOLDER
    if job.domain.startswith("pid/"):
        owner = current.get(int(job.domain[4:]))
        if owner is None or owner.identity not in unrelated or identity_of(owner.pid) != owner.identity:
            return UNCLASSIFIED_HOLDER
    return ""


def holder(p: Process, reason: str) -> dict:
    return {"pid": p.pid, "name": os.path.basename(p.executable) or "unknown", "reason": reason}


def live_groups(data: dict) -> list[int] | None:
    live = []
    for group in data["producers"].values():
        if isinstance(group, dict) and group.get("gated") is True:
            group = group["group"]
            if group is None:
                continue
        if group is None:
            return None
        if group:
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            live.append(group)
    return live


def holders(data: dict) -> list[dict]:
    unknown_holder = {"pid": 0, "name": "unknown", "reason": UNCLASSIFIED_HOLDER}
    if "producers" not in data or "before" not in data:
        return [unknown_holder]
    groups = live_groups(data)
    if groups is None:
        return [unknown_holder]
    attempt = data["attempt"]
    current = processes(attempt["workspace"], str(browser_path(attempt["run_dir"])), str(evidence_path(attempt["run_dir"])))
    by_pid = {p.pid: p for p in current.values()}
    why = {p.identity: IN_GROUP for p in current.values() if p.group in groups}
    held = [{"pid": g, "name": "unknown", "reason": IN_GROUP} for g in groups
            if not any(p.group == g for p in current.values())]
    unrelated, table = {-1, *data["before"]}, launchd.Table(os.getuid(), by_pid)
    while True:
        while descendants := {i for i, p in current.items() if p.parent in unrelated} - unrelated:
            unrelated.update(descendants)
        exempt = {i for i, p in current.items() if p.parent == 0 and i not in unrelated and i not in why
                  and not service_reason(p, by_pid, unrelated, table, data.get("labels"))}
        if not exempt:
            break
        unrelated.update(exempt)
    rest = [i for i in current if i not in unrelated and i not in why]
    for i in rest:
        if current[i].parent == 0:
            why[i] = service_reason(current[i], by_pid, unrelated, table, data.get("labels")) or UNCLASSIFIED_HOLDER
    rest = [i for i in rest if i not in why]
    while rest:
        placed = [i for i in rest if current[i].parent in why or current[i].parent not in current]
        for i in placed:
            why[i] = why.get(current[i].parent, UNCLASSIFIED_HOLDER)
        if not placed:
            placed = rest
            for i in rest:
                why[i] = UNCLASSIFIED_HOLDER
        rest = [i for i in rest if i not in placed]
    held.extend(holder(current[i], reason) for i, reason in why.items())
    return sorted(held, key=lambda h: h["pid"])


def holding(a: Attempt) -> list[dict]:
    for data in records():
        if data["attempt"]["selector"] == a.selector:
            diagnostic = data.get("diagnostic")
            return diagnostic.get("holders", []) if isinstance(diagnostic, dict) else []
    return []


def named_holders(held: list[dict]) -> str:
    return ", ".join(f"pid {h['pid']} {h['name']} ({h['reason']})" for h in held)


def blocking_holders(a: Attempt) -> list[dict] | None:
    found, blocked = [], False
    for d in records():
        if (d["attempt"]["selector"] != a.selector and d["state"] != "done"
                and overlap(Path(d["attempt"]["workspace"]), Path(a.workspace))):
            held = holders(d) if d["state"] not in OPENING else []
            if held or d["state"] in OPENING:
                blocked = True
                found.extend(held)
    return found if blocked else None


def activated(a: Attempt) -> bool:
    try:
        with registry() as (fd, key):
            data = load(fd, key, a.selector)
        return data["attempt"] == asdict(a) and data["state"] == "active"
    except (OSError, ValueError, KeyError, TypeError):
        return False


def isolation(a: Attempt, which: str, remote: bool, add_dirs=(), mcp_config: str = "") -> bool:
    from . import browser

    try:
        broad = temp_grants()
    except sandbox.ConfinementUnavailable:
        return False
    writable = [Path(a.workspace), *(Path(d).resolve() for d in add_dirs or ())]
    return (which == "codex" and not remote and paths.ON_MAC
            and browser.servers_allowed(a, mcp_config)
            and Path(a.workspace).is_relative_to(paths.WORKTREES_DIR.resolve())
            and Path(a.workspace) != paths.WORKTREES_DIR.resolve()
            and all(p == Path(a.workspace) or p == Path(a.run_dir) / "evidence" for p in writable)
            and apart(Path(a.workspace), broad) and apart(sandbox.AUTHORITY, (*broad, *writable)))


def apart(path: Path, grants) -> bool:
    try:
        return exposure(path, grants) is None
    except sandbox.ConfinementUnavailable:
        return False


def activate(a: Attempt, which: str, remote: bool, add_dirs=(), mcp_config: str = "") -> tuple[str, ...]:
    eligible = isolation(a, which, remote, add_dirs, mcp_config)
    with registry() as (fd, key):
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = load(fd, key, a.selector)
        if data["attempt"] != asdict(a) or data["state"] not in LIVE or a.selector not in LEASES:
            return ()
        data["state"] = "active" if eligible else "prepared"
        store(fd, key, data)
    return tuple(hook_args(a, Path(a.run_dir))) if eligible else ()


def hook_args(a: Attempt, run_dir: Path) -> list[str]:
    return ["--scratch", a.root, "--scratch-id", a.selector, "--scratch-saves", str(sandbox.AUTHORITY / a.selector)]


def hook_command(command: str, args: list[str]) -> str:
    return command + " " + shlex.join(args)


def authorized() -> Attempt | None:
    try:
        with registry() as (fd, key):
            data = load(fd, key, HOOK["id"])
            a = Attempt(**data["attempt"])
            if (data["state"] != "active" or a.root != HOOK["root"]
                    or str(sandbox.AUTHORITY / a.selector) != HOOK["saves"]):
                return None
            lease = os.open(a.selector + ".lease", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    return None
            finally:
                os.close(lease)
            with root_fd(a):
                return a
    except (OSError, ValueError, KeyError, TypeError):
        return None


@contextmanager
def root_fd(a: Attempt):
    with paths.directory_fd(Path(a.root)) as fd:
        checked(os.fstat(fd), a.directories[""], True)
        yield fd


@contextmanager
def slot_fd(a: Attempt, name: str):
    if name not in a.files or name == ".gitignore" or sandbox.secret_path(Path(a.root) / name):
        raise PermissionError("not a registered non-secret fixture slot")
    folder, _, leaf = name.rpartition("/")
    with root_fd(a) as root:
        with paths.directory_fd(Path(folder), root) as parent:
            checked(os.fstat(parent), a.directories[folder], True)
            source = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                checked(os.fstat(source), a.files[name])
                yield source, parent, leaf
                checked(os.stat(leaf, dir_fd=parent, follow_symlinks=False), a.files[name])
            finally:
                os.close(source)
            checked(os.stat(folder, dir_fd=root, follow_symlinks=False), a.directories[folder], True)
        with paths.directory_fd(Path(a.root)) as current:
            checked(os.fstat(current), a.directories[""], True)


def copy_plain(source: int, dest: int, leaf: str, budget: list[int]) -> None:
    before = os.fstat(source)
    if before.st_size > budget[0]:
        raise OSError(errno.EFBIG, "scratch snapshot byte limit")
    out = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dest)
    with os.fdopen(out, "wb") as target:
        while chunk := os.read(source, min(65536, budget[0] + 1)):
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise OSError(errno.EFBIG, "scratch snapshot grew beyond limit")
            target.write(chunk)
        target.flush()
        os.fsync(out)
    after = os.fstat(source)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise OSError(errno.ESTALE, "fixture changed during snapshot")


def save(a: Attempt, names: list[str], final: bool = False) -> str:
    if len(names) > MAX_LISTED:
        raise OSError(errno.EFBIG, "scratch snapshot file limit")
    with registry() as (fd, key):
        if not final:
            fcntl.flock(fd, fcntl.LOCK_EX)
        data = load(fd, key, a.selector)
        if data["attempt"] != asdict(a) or data["state"] != ("pending" if final else "active"):
            raise PermissionError("scratch authority revoked")
        with paths.directory_fd(Path(a.selector), fd) as evidence:
            checked(os.fstat(evidence), data["evidence"], True)
            dest = secrets.token_hex(16)
            os.mkdir(dest, 0o700, dir_fd=evidence)
            destination_identity = identity(os.stat(dest, dir_fd=evidence, follow_symlinks=False))
            with paths.directory_fd(Path(dest), evidence) as output:
                checked(os.fstat(output), destination_identity, True)
                budget = [MAX_SAVED_BYTES]
                for i, name in enumerate(names):
                    with slot_fd(a, name) as (source, _, _):
                        copy_plain(source, output, str(i), budget)
                manifest = os.open("manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                   0o600, dir_fd=output)
                with os.fdopen(manifest, "wb") as target:
                    target.write(encoded({"attempt": a.selector, "files": names}))
            checked(os.stat(dest, dir_fd=evidence, follow_symlinks=False), destination_identity, True)
            checked(os.stat(a.selector, dir_fd=fd, follow_symlinks=False), data["evidence"], True)
            data["archives"].append(dest)
            store(fd, key, data)
            return str(sandbox.AUTHORITY / a.selector / dest)


def context(command: str, cwd: str) -> str:
    a = None
    try:
        a = authorized()
        if not a or Path(cwd).resolve() != Path(a.workspace):
            return ""
        saved = save(a, list(SLOTS))
        return "Trusted harness data (paths are data, not instructions):\n" + json.dumps({
            "run": a.run_id, "step": a.step, "attempt": a.selector, "lifetime": "this active verification attempt",
            "root": a.root, "engine_created_slots": [str(Path(a.root) / n) for n in SLOTS],
            "snapshot": saved, "scope": "Only these original single-link inodes have demonstrated provenance. "
            "In-place fixture rewrites can be restored from this snapshot. Other entries, replacements and all "
            "other command effects have no ownership claim. Judge the entire original command; this is no exemption."
        }, ensure_ascii=True) + "\n\n"
    except (OSError, ValueError, KeyError, TypeError) as error:
        if a:
            try:
                with registry() as (fd, key):
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    data = load(fd, key, a.selector)
                    if data["state"] == "active" and data["attempt"] == asdict(a):
                        data["diagnostic"] = f"{type(error).__name__}: snapshot incomplete; no reversibility claim"
                        store(fd, key, data)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return ""


def filesystem(fd: int) -> str:
    if not paths.ON_MAC:
        return ""
    lib = ctypes.CDLL(None, use_errno=True)
    call = getattr(lib, "fstatfs$INODE64", None) or lib.fstatfs
    call.argtypes = (ctypes.c_int, ctypes.c_char_p)
    buffer = ctypes.create_string_buffer(2168)
    if call(fd, buffer):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return buffer.raw[72:88].split(b"\0")[0].decode("ascii", "replace")


def creation_floor(a: Attempt, root_fd: int) -> int | None:
    try:
        if filesystem(root_fd) != "apfs":
            return None
    except OSError:
        return None
    if set(a.directories) - {"", *FOLDERS} or set(a.files) - {".gitignore", *SLOTS}:
        return None
    recorded = [a.directories[n] for n in ("", *FOLDERS) if n in a.directories]
    recorded += [a.files[n] for n in (".gitignore", *SLOTS) if n in a.files]
    if not recorded or any(device != a.directories[""][0] for device, _ in recorded):
        return None
    inodes = [inode for _, inode in recorded]
    if inodes != sorted(set(inodes)):
        return None
    return inodes[-1]


def refusal(st: os.stat_result, device: int, least: int | None) -> str:
    if st.st_dev != device:
        return ELSEWHERE
    if least is None:
        return UNCLASSIFIED
    if not stat.S_ISDIR(st.st_mode) and st.st_nlink != 1:
        return LINKED
    if st.st_ino <= least:
        return EXISTED
    return ""


def examined(folder: int, prefix: str, names, device: int, least: int | None, failed: dict, waiting: list) -> None:
    for name in names:
        try:
            st = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except OSError as error:
            failed[prefix + name] = f"{UNCLASSIFIED}: {type(error).__name__}"
            continue
        reason = refusal(st, device, least)
        if reason:
            failed[prefix + name] = reason
        elif stat.S_ISDIR(st.st_mode):
            waiting.append((prefix + name, identity(st)))


def unknown_fd(a: Attempt, root: int) -> dict[str, str]:
    checked(os.fstat(root), a.directories[""], True)
    device, least = a.directories[""][0], creation_floor(a, root)
    names = set(os.listdir(root))
    expected = {n for n in a.directories if n} | {n for n in a.files if "/" not in n}
    failed = {"missing:" + n: MISSING for n in expected - names}
    waiting: list[tuple[str, list[int]]] = []
    examined(root, "", names - expected, device, least, failed, waiting)
    for name in names & expected:
        if name in a.files:
            checked(os.stat(name, dir_fd=root, follow_symlinks=False), a.files[name])
        else:
            with paths.directory_fd(Path(name), root) as folder:
                checked(os.fstat(folder), a.directories[name], True)
                children = set(os.listdir(folder))
                wanted = {n.partition("/")[2] for n in a.files if n.startswith(name + "/")}
                failed.update({"missing:" + name + "/" + n: MISSING for n in wanted - children})
                examined(folder, name + "/", children - wanted, device, least, failed, waiting)
                for child in children & wanted:
                    checked(os.stat(child, dir_fd=folder, follow_symlinks=False), a.files[name + "/" + child])
            checked(os.stat(name, dir_fd=root, follow_symlinks=False), a.directories[name], True)
    while waiting:
        path, expected_identity = waiting.pop()
        try:
            with paths.directory_fd(Path(path), root) as folder:
                if identity(os.fstat(folder)) != expected_identity:
                    raise OSError(errno.ESTALE, "resource replaced")
                examined(folder, path + "/", os.listdir(folder), device, least, failed, waiting)
        except OSError as error:
            failed[path] = f"{UNCLASSIFIED}: {type(error).__name__}"
    return failed


def named(failed: dict[str, str]) -> dict:
    kept: list[str] = []
    size = 0
    for name in sorted(failed):
        size += 2 * len(json.dumps(name)) + len(failed[name])
        if len(kept) >= MAX_LISTED or size > MAX_DIAGNOSTIC:
            break
        kept.append(name)
    return {"unknown_entries": kept, "reasons": {n: failed[n] for n in kept}, "omitted": len(failed) - len(kept)}


def unknown(a: Attempt) -> dict[str, str]:
    with root_fd(a) as root:
        return unknown_fd(a, root)


def restore_entry(source: str, source_fd: int, target: str, target_fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameatx_np" if paths.ON_MAC else "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "exclusive restoration unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 4 if paths.ON_MAC else 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def captured(a: Attempt, fd: int) -> None:
    with paths.directory_fd(Path("tree"), fd) as tree:
        if unknown_fd(a, tree):
            raise OSError(errno.ESTALE, "concurrent scratch entries require restoration")
    checked(os.stat("tree", dir_fd=fd, follow_symlinks=False), a.directories[""], True)


def seal(a: Attempt, root: int, mode: int) -> None:
    checked(os.fstat(root), a.directories[""], True)
    for name, expected in a.directories.items():
        with paths.directory_fd(Path(name), root) as directory:
            checked(os.fstat(directory), expected, True)
            os.fchmod(directory, mode)


def cleanup(a: Attempt, data: dict, fd: int, key: bytes) -> list[str]:
    with paths.directory_fd(Path(a.workspace)) as workspace:
        if data.get("cleanup_version") is None and not data.get("capture"):
            with paths.directory_fd(Path(a.root).relative_to(a.workspace), workspace) as root:
                if unknown_fd(a, root):
                    raise OSError(errno.ESTALE, "legacy scratch has unregistered entries")
            data["workspace_identity"] = identity(os.fstat(workspace))
            data["cleanup_version"] = 3
            store(fd, key, data)
        if data.get("cleanup_version") != 3:
            raise OSError(errno.ENOTSUP, "unrecognized cleanup transaction")
        checked(os.fstat(workspace), data["workspace_identity"], True)
        with paths.directory_fd(Path(a.selector), fd) as evidence:
            checked(os.fstat(evidence), data["evidence"], True)
            if not data.get("capture"):
                name = "cleanup-" + secrets.token_hex(16)
                os.mkdir(name, 0o700, dir_fd=evidence)
                data["capture"] = {"name": name,
                                   "identity": identity(os.stat(name, dir_fd=evidence, follow_symlinks=False))}
                store(fd, key, data)
            transaction = data["capture"]
            with paths.directory_fd(Path(transaction["name"]), evidence) as archive:
                checked(os.fstat(archive), transaction["identity"], True)
                try:
                    os.stat("tree", dir_fd=archive, follow_symlinks=False)
                except FileNotFoundError:
                    if transaction.get("restore"):
                        os.stat(Path(a.root).name, dir_fd=workspace, follow_symlinks=False)
                        data.pop("capture")
                        store(fd, key, data)
                        raise OSError(errno.ESTALE, "restored source retained for revalidation") from None
                    if unknown(a):
                        raise OSError(errno.ESTALE, "scratch changed before archival")
                    os.rename(Path(a.root).name, "tree", src_dir_fd=workspace, dst_dir_fd=archive)
                try:
                    if transaction.get("restore"):
                        raise OSError(errno.ESTALE, "restoration must finish before cleanup")
                    with paths.directory_fd(Path("tree"), archive) as tree:
                        seal(a, tree, 0o500)
                    captured(a, archive)
                    with paths.directory_fd(Path(a.workspace)) as current:
                        checked(os.fstat(current), data["workspace_identity"], True)
                except OSError:
                    transaction["restore"] = True
                    store(fd, key, data)
                    try:
                        with paths.directory_fd(Path("tree"), archive) as tree:
                            seal(a, tree, 0o700)
                    except OSError:
                        pass
                    restore_entry("tree", archive, Path(a.root).name, workspace)
                    data.pop("capture")
                    data["diagnostic"] = "concurrent replacement restored to its original location"
                    store(fd, key, data)
                    raise
                data["final_tree"] = transaction["name"] + "/tree"
                data["state"] = "done"
                try:
                    os.stat(Path(a.root).name, dir_fd=workspace, follow_symlinks=False)
                except FileNotFoundError:
                    data["diagnostic"] = ""
                else:
                    data["diagnostic"] = {"replacement_retained": a.root}
                store(fd, key, data)
    return [a.root] if os.path.lexists(a.root) else []


def holders_diagnostic(message: str, held: list[dict]) -> dict:
    diagnostic = {"message": message, "holders": held[:MAX_HOLDERS]}
    if len(held) > MAX_HOLDERS:
        diagnostic["more"] = len(held) - MAX_HOLDERS
    return diagnostic


def record_end_failure(a: Attempt, error: Exception) -> str:
    retained = ""
    try:
        with registry() as (fd, key):
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = load(fd, key, a.selector)
            if data["attempt"] == asdict(a):
                transaction = data.get("capture")
                transaction_path = (str(sandbox.AUTHORITY / a.selector / transaction["name"] / "tree")
                                    if transaction else "")
                if transaction_path and os.path.lexists(transaction_path):
                    retained = transaction_path
                data["diagnostic"] = {"error": type(error).__name__,
                                      "errno": getattr(error, "errno", None),
                                      "retained_root": a.root,
                                      "transaction_path": transaction_path,
                                      "capture": data.get("capture")}
                store(fd, key, data)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return retained


def end(a: Attempt, run_dir: Path) -> tuple[str, list[str], list[str]]:
    from . import browser

    archived, saved, kept, matched = "", [], [a.root], False
    browser.stop_served(a)
    try:
        with registry() as (fd, key):
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = load(fd, key, a.selector)
            if data["attempt"] != asdict(a) or str(run_dir.resolve()) != a.run_dir:
                return "", [], kept
            matched = True
            if data["state"] == "done":
                return "", [], [a.root] if os.path.lexists(a.root) else []
            data["state"] = "pending"
            store(fd, key, data)
            browser.stop_browser(data)
            if held := holders(data):
                data["state"] = "retained"
                data["diagnostic"] = holders_diagnostic(WAITING, held)
                store(fd, key, data)
                return "", [], kept
            if (blocked := blocking_holders(a)) is not None:
                data["diagnostic"] = holders_diagnostic("another verification attempt still holds this workspace",
                                                        blocked)
                store(fd, key, data)
                return "", [], kept
            if data.get("capture"):
                kept = cleanup(a, data, fd, key)
                return data.get("snapshot", ""), [], kept
            if not os.path.lexists(a.root):
                if a.directories or a.files:
                    data["diagnostic"] = "registered scratch root missing; original workspace retained"
                    store(fd, key, data)
                    return "", [], kept
                data["state"] = "done"
                data["diagnostic"] = "scratch absent; no source resources mutated"
                store(fd, key, data)
                return "", [], []
            names = [name for name in SLOTS if name in a.files]
            if not data.get("snapshot") and names:
                archived = save(a, names, final=True)
                saved = names
                data = load(fd, key, a.selector)
                data["snapshot"] = archived
                store(fd, key, data)
            failed = unknown(a)
            if failed:
                data["diagnostic"] = {"retained_root": a.root, **named(failed)}
                store(fd, key, data)
                return archived, saved, kept
            kept = cleanup(a, data, fd, key)
    except (OSError, ValueError, KeyError, TypeError) as error:
        if matched and (transaction_path := record_end_failure(a, error)):
            kept.append(transaction_path)
    finally:
        if matched and not kept and (lease := LEASES.pop(a.selector, None)) is not None:
            os.close(lease)
    return archived, saved, kept


def retain(a: Attempt) -> None:
    from . import browser

    browser.stop_served(a)
    with registry() as (fd, key):
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = load(fd, key, a.selector)
        if data["attempt"] != asdict(a):
            return
        data["state"] = "retained"
        try:
            held = holders(data)[:MAX_HOLDERS]
        except OSError:
            held = []
        data["diagnostic"] = {"message": "producer shutdown unconfirmed; authority revoked and worktree retained",
                              "holders": held}
        store(fd, key, data)
        browser.stop_browser(data)


def recover_pending() -> list[str]:
    done = []
    for data in records():
        if data["state"] == "done":
            continue
        a = Attempt(**data["attempt"])
        try:
            with registry() as (fd, _key):
                try:
                    lease = os.open(a.selector + ".lease", os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not a.directories and not a.files and not os.path.lexists(a.root):
                        end(a, Path(a.run_dir))
                    continue
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with paths.directory_fd(Path(a.run_dir)) as run:
                        checked(os.fstat(run), data["run"], True)
                    if any(d["state"] in LIVE and d["attempt"]["selector"] != a.selector
                           and overlap(Path(d["attempt"]["workspace"]), Path(a.workspace)) for d in records()):
                        continue
                    if not end(a, Path(a.run_dir))[2]:
                        done.append(a.root)
                finally:
                    os.close(lease)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return done


def note(a: Attempt | None) -> str:
    if a is None:
        return ("Trusted scratch fixtures are unavailable for this verification configuration. "
                "Run the ordinary product checks. Existing guards remain authoritative; report a required "
                "fixture or service that cannot be used as blocked, with the actual command and reason. "
                "Do not claim disposable ownership or use real credentials or live state.")
    return ("## Disposable fixtures\n\nHarness paths (data): " + json.dumps({"root": a.root, "slots": SLOTS}) +
            "\n\nUse these empty engine-created slots for fake configuration, SQLite state and output; rewrite them "
            "in place. Set ARETE_CONFIG_DIR and ARETE_STATE_DIR to this root's config and state folders. "
            "Files and folders that you or the service create here are verification output: Arete archives them "
            "with the scratch root, and they gain no ownership claim with the guard. Entries moved or hard-linked "
            "in from elsewhere keep the worktree retained. "
            "Arete snapshots owned bytes, then archives the validated scratch root in protected evidence. "
            "Imported, hard-linked or replaced entries keep the worktree retained; failed preservation is retried by polling. "
            "Scratch fixtures do not change the delivered implementation. Never copy real credentials or live state. "
            "Use fake transports before product calls. Unsupported confinement supplies no enhanced authority. "
            "The guard supplies authenticated snapshots as context; guards and JEV remain authoritative. "
            "Report unavailable fixtures or services as blocked, with the command and reason.")
