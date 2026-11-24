from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from .. import dbstats, paths, util
from . import migrations
from .migrations import m0002_foundation_time_shape as time_shape
from .migrations import m0028_workspaces_contract as workspace_contract

TABLES = {"schema_meta": "kept", "schema_migrations": "kept"}
DB_LOCK = threading.Lock()
DB_THREAD = threading.local()
_TRANSACTION_DEADLINE: ContextVar[tuple[float, Callable[[], None]] | None] = ContextVar(
    "sqlite_transaction_deadline", default=None)


@contextmanager
def nonblocking_transactions(until: float, check: Callable[[], None]):
    if _TRANSACTION_DEADLINE.get() is not None or in_write():
        raise RuntimeError("bounded transactions require an outer startup scope")
    token = _TRANSACTION_DEADLINE.set((until, check))
    try:
        check()
        yield
        check()
    finally:
        _TRANSACTION_DEADLINE.reset(token)


class FixtureConnection(sqlite3.Connection):
    def close(self):
        try:
            super().close()
        finally:
            self.handles.close()


def connect_db(p: Path, readonly: bool = False, create: bool = False, *, streaming: bool = False,
               timeout: float | None = None) -> sqlite3.Connection:
    if streaming and not readonly:
        raise ValueError("a streaming database connection must be read-only")
    if not paths.ISOLATED:
        mode = "ro" if readonly else "rwc" if create else "rw"
        conn = sqlite3.connect(f"{p.absolute().as_uri()}?mode={mode}", uri=True, timeout=30 if timeout is None else timeout)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        except BaseException:
            conn.close()
            raise
    if not paths.ON_MAC or paths.confined(p) is None:
        raise PermissionError("isolated state requires a supported confined database path")
    deadline = None if timeout is None else time.monotonic() + timeout
    if getattr(DB_THREAD, "active", False) or not DB_LOCK.acquire(timeout=30 if timeout is None else timeout):
        raise PermissionError("nested or busy isolated state access refused")
    DB_THREAD.active = True
    handles, retained, conn = ExitStack(), ExitStack(), None
    handles.callback(DB_LOCK.release)
    handles.callback(setattr, DB_THREAD, "active", False)
    try:
        parent = handles.enter_context(paths.directory_fd(p.absolute().parent))
        lock_until = time.monotonic() + 30 if deadline is None else deadline
        while True:
            try:
                fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = lock_until - time.monotonic()
                if remaining <= 0:
                    raise PermissionError("busy isolated state directory refused") from None
                time.sleep(min(0.05, remaining))
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(p.name + suffix, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise PermissionError("existing isolated database sidecar refused")
        flags = os.O_RDONLY if readonly else os.O_RDWR | (os.O_CREAT if create else 0)
        fd = os.open(p.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        (retained if streaming else handles).callback(os.close, fd)
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid()
                or os.pread(fd, 20, 0)[18:20] == b"\x02\x02"):
            raise PermissionError("linked, non-plain or WAL isolated state database refused")
        conn = sqlite3.connect(f"file:/dev/fd/{fd}?mode={'ro' if readonly else 'rw'}", uri=True,
                               timeout=30 if deadline is None else max(0, deadline - time.monotonic()),
                               factory=FixtureConnection)
        conn.handles = retained if streaming else handles
        if not readonly and conn.execute("PRAGMA journal_mode=MEMORY").fetchone()[0] != "memory":
            raise PermissionError("isolated state requires disposable memory journaling")
        conn.execute("PRAGMA synchronous=NORMAL")
        if streaming:
            handles.close()
        return conn
    except BaseException:
        if conn is not None:
            conn.close()
        retained.close()
        handles.close()
        raise


TIME_COLUMNS = {**time_shape.TIME_COLUMNS, "runs": (*time_shape.TIME_COLUMNS["runs"], "measured_at"),
                "idempotency": ("at",), "orchestrations": ("at",), "orchestration_turns": ("at",),
                "orchestration_actions": ("at",)}
TIME_GLOB = time_shape.TIME_GLOB

_CHAIN = migrations.load()
LATEST = _CHAIN[-1].n
WORKSPACE_SNAPSHOT_SCHEMA = 25
WORKER_EXCHANGE_SCHEMA = 26
SECRET_REGISTRY_SCHEMA = 27
WORKSPACE_CONTRACT_SCHEMA = 28
INHERITED_SERVE_CHILDREN = "ARETE_SERVE_RESTARTED_FOR_NEW_CODE"
INHERITED_SERVE_IDENTITIES = "ARETE_SERVE_RESTARTED_CHILD_IDENTITIES"
WORKSPACE_CHILD_PIDS = "workspace_contract_children"
WORKSPACE_CHILD_IDENTITIES = "workspace_contract_child_identities"
type ProcessIdentity = tuple[str, int]
type ServeChildren = set[tuple[int, ProcessIdentity | None]]
WORKSPACE_HANDOFF = ("the installation's legacy snapshot directory in [serve] workspace_snapshots "
                     "or from arete migrate --workspace-snapshots DIR")
# Each capability migrates at its own entry point. Unrelated writers can use older databases.
NEEDED = max(m.n for m in _CHAIN if m.phase != "contract"
             and m.n not in (WORKSPACE_SNAPSHOT_SCHEMA, WORKER_EXCHANGE_SCHEMA, SECRET_REGISTRY_SCHEMA))
MIGRATE_WAIT_SECONDS = 60
_CHECKED = set()
_OWNER = threading.local()


class NoDatabase(RuntimeError):
    pass


class IncompatibleSchema(RuntimeError):
    pass


class WorkspaceHandoffPending(RuntimeError):
    pass


def refuse_empty_file() -> None:
    if paths.DB_FILE.exists() and paths.DB_FILE.stat().st_size > 0:
        return
    from .. import backup

    if newest := backup.newest_snapshot():
        raise NoDatabase(f"{paths.DB_FILE} is missing or empty but {newest} exists; stop arete serve and run "
                         f"`arete backup --restore {newest}` instead of starting over with an empty file")


def missing_database():
    refuse_empty_file()
    return NoDatabase(f"{paths.DB_FILE} is missing; run arete serve or arete migrate")


def contract_blockers(conn, n):
    from .. import control, launch_lease, record

    workspace_contract = n == WORKSPACE_CONTRACT_SCHEMA
    blockers = [f"launch lease {name}" for name in launch_lease.held_launches()] if workspace_contract else []
    statuses = "(status = 'running' OR pid IS NOT NULL)" if workspace_contract else "status = 'running'"
    for row in conn.execute(f"SELECT id, pid, lock, started, finished, status FROM runs WHERE {statuses} "
                            "AND (schema_version IS NULL OR schema_version < ?)", (n,)):
        row = dict(row)
        if row["status"] == "running":
            with record.dead(row) as gone:
                if not gone:
                    blockers.append(row["id"])
        elif workspace_contract and settled_run_process_alive(row):
            blockers.append(row["id"])
    if workspace_contract:
        beat, seen = util.read_json_snapshot(paths.DB_FILE.with_name(control.BEAT_FILE.name), {})
        if not isinstance(beat, dict):
            beat = {}
        children = live_serve_children(heartbeat_serve_children(beat, seen) | inherited_serve_children())
        blockers.extend(f"inherited child {pid}" for pid in sorted({pid for pid, _ in children}))
        blockers.extend(workspace_admission_blockers())
        if problem := workspace_serve_blocker(beat, n):
            blockers.append(problem)
    else:
        beat = control.database_beat()
        if util.pid_alive(beat.get("pid")) and (beat.get("schema") or 0) < n:
            blockers.append(f"serve {beat.get('pid')}")
    return blockers


def workspace_admission_blockers():
    for file in sorted(paths.LOCKS_DIR.iterdir()):
        name = file.stem
        if (file.suffix == ".lock" and (name == "file-rule" or name.startswith(("run-", "loop-")))
                and util.lock_is_held(name)):
            yield f"held lock {name}"


def process_identity(pid: int) -> ProcessIdentity | None:
    try:
        if paths.ON_MAC:
            from .. import scratch

            boot, serial = scratch.boot_session(), scratch.identity_of(pid)
        else:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            serial = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        if boot and serial is not None:
            return UUID(boot).hex, serial
    except (OSError, ValueError, IndexError):
        pass
    return None


def child_identity(value) -> ProcessIdentity | None:
    if (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
            and type(value[1]) is int and value[1] >= 0):
        with suppress(ValueError):
            return UUID(value[0]).hex, value[1]
    return None


def heartbeat_serve_children(beat, seen=None) -> ServeChildren:
    pids = beat.get(WORKSPACE_CHILD_PIDS)
    identities = beat.get(WORKSPACE_CHILD_IDENTITIES)
    if not isinstance(identities, dict):
        identities = {}
    children = set()
    for pid in pids if isinstance(pids, list) else ():
        if type(pid) is int and 0 < pid <= 2**31 - 1:
            identity = child_identity(identities.get(str(pid)))
            if identity is not None or seen is None or process_existed_by(pid, seen.st_mtime):
                children.add((pid, identity))
    return children


def inherited_serve_children() -> ServeChildren:
    pids = [int(pid) for pid in os.environ.get(INHERITED_SERVE_CHILDREN, "").split(",") if pid]
    try:
        identities = json.loads(os.environ.get(INHERITED_SERVE_IDENTITIES, "{}"))
    except ValueError:
        identities = {}
    inherited = heartbeat_serve_children({WORKSPACE_CHILD_PIDS: pids, WORKSPACE_CHILD_IDENTITIES: identities})
    children = set()
    for pid, identity in inherited:
        if identity is not None and process_identity(pid) not in (None, identity) and has_live_child(pid):
            identity = None
        children.add((pid, identity))
    return children


def has_live_child(pid: int) -> bool:
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None
    except ChildProcessError:
        return False
    except OSError:
        return True


def live_serve_children(children: ServeChildren) -> ServeChildren:
    return {(pid, identity) for pid, identity in children if util.pid_alive(pid)
            and (identity is None or process_identity(pid) in (None, identity))}


def workspace_child_fields(children: ServeChildren) -> dict:
    grouped = {}
    for pid, identity in children:
        grouped.setdefault(pid, set()).add(identity)
    identities = {str(pid): next(iter(found)) for pid, found in grouped.items() if len(found) == 1 and None not in found}
    return {WORKSPACE_CHILD_PIDS: sorted(grouped), WORKSPACE_CHILD_IDENTITIES: identities}


def process_existed_by(pid, at):
    if not util.pid_alive(pid):
        return False
    try:
        found = util.sh(["/bin/ps", "-p", str(pid), "-o", "stat=,lstart="], timeout=2, env={"LC_ALL": "C", "TZ": "UTC"})
        if not found.stdout.strip():
            return util.pid_alive(pid)
        state, started = found.stdout.strip().split(maxsplit=1)
        when = datetime.strptime(" ".join(started.split()), "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
        return not state.startswith("Z") and when.timestamp() <= at
    except (OSError, ValueError):
        return util.pid_alive(pid)


def settled_run_process_alive(row):
    if row["pid"] == os.getpid():
        return False
    try:
        at = util.parse_iso(row["finished"] or row["started"]).timestamp()
    except (ValueError, TypeError):
        return util.pid_alive(row["pid"])
    return process_existed_by(row["pid"], at)


def workspace_serve_blocker(beat, n):
    from .. import control

    if not util.lock_is_held("serve"):
        if (paths.DB_FILE.parent.resolve() != paths.STATE_DIR.resolve() and util.pid_alive(beat.get("pid"))
                and (type(beat.get("schema")) is not int or beat["schema"] < n)):
            return f"serve {beat.get('pid')}"
        return ""
    try:
        with (paths.LOCKS_DIR / "serve.lock").open() as lock:
            lock_pid = int(lock.read())
            lock_written = os.fstat(lock.fileno()).st_mtime_ns
    except (OSError, ValueError):
        return "serve lock holder has not reported its schema"
    unreported = f"serve {lock_pid} has not reported its schema"
    try:
        beat_written = paths.DB_FILE.with_name(control.BEAT_FILE.name).stat().st_mtime_ns
    except FileNotFoundError:
        return unreported
    status = control.database_beat()
    if (lock_pid <= 0 or not util.pid_alive(lock_pid) or not isinstance(status, dict)
            or status.get("pid") != lock_pid or type(status.get("schema")) is not int):
        return unreported
    if status["schema"] < n:
        return f"serve {lock_pid}"
    return unreported if beat_written < lock_written else ""


def workspace_handoff_blockers(conn, source, *, explicit=False):
    from .. import serve_guard

    blockers = contract_blockers(conn, WORKSPACE_CONTRACT_SCHEMA)
    try:
        receipt = serve_guard.pending(paths.DB_FILE.parent, paths.PACKAGE_DIR.parent)
        if receipt is not None and serve_guard.at_tip(paths.PACKAGE_DIR.parent, receipt):
            contract = next(m for m in _CHAIN if m.n == WORKSPACE_CONTRACT_SCHEMA)
            if reason := serve_guard.unsupported_contract(paths.PACKAGE_DIR.parent, receipt.previous,
                                                          [(contract.name, contract.sha256)]):
                blockers.append(f"deploy a compatible workspace rollback release first: {reason}")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        blockers.append(f"cannot verify the pending workspace deployment: {error}")
    if problem := workspace_source_blocker(conn, source, explicit=explicit):
        blockers.append(problem)
    return blockers


def workspace_source_blocker(conn, source, *, explicit):
    if source is None:
        return "" if new_workspace_store(conn) else f"supply {WORKSPACE_HANDOFF}"
    try:
        allow_missing = workspace_contract.may_be_missing(conn, explicit)
        with workspace_contract.snapshot_directory(source, allow_missing=allow_missing):
            return ""
    except (OSError, ValueError) as e:
        return workspace_source_problem(source, e)


def workspace_source_problem(source, error):
    return f"snapshot directory {source}: {util.first_line(str(error), 300)}; supply {WORKSPACE_HANDOFF}"


def new_workspace_store(conn):
    return (conn.execute("SELECT 1 FROM schema_meta WHERE key = 'new_workspace_store' AND value = '1'").fetchone() is not None
            and workspace_contract.unrestored(conn) and workspace_store_is_new())


def workspace_handoff_ready(snapshots: Path | None) -> bool:
    with reader() as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < SECRET_REGISTRY_SCHEMA:
            return False
        source = workspace_contract.snapshot_source(conn, snapshots)
        return not workspace_source_blocker(conn, source, explicit=snapshots is not None)


def execute_statements(conn, sql):
    pending = ""
    for char in sql:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip():
        conn.execute(pending)


def workspace_store_is_new():
    from .. import install, serve_guard

    scaffolding = {paths.DB_FILE, paths.RUNS_DIR, paths.WORKTREES_DIR, paths.LOCKS_DIR, paths.LOG_DIR,
                   paths.DB_FILE.with_name(paths.SERVE_BEAT_FILE.name), paths.SERVER_FILE, paths.backups_dir(),
                   paths.DB_FILE.parent / serve_guard.RECEIPT, install.serve_guard_file(),
                   *(paths.DB_FILE.with_name(paths.DB_FILE.name + suffix) for suffix in ("-wal", "-shm", "-journal"))}
    expected = {path.absolute() for path in scaffolding}
    return (all(entry.absolute() in expected for entry in paths.STATE_DIR.iterdir())
            and not any(paths.RUNS_DIR.iterdir()) and not any(paths.WORKTREES_DIR.iterdir()))


def migrate(contracts=True, *, workspace_snapshots: Path | None = None):
    from .. import backup, config

    chain = migrations.load()
    paths.ensure_dirs()
    with util.lock("migrate", wait=True):
        refuse_empty_file()
        conn = connect_db(paths.DB_FILE, create=True)
        conn.row_factory = sqlite3.Row
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            migrations.validate(conn, chain, version)
            conn.close()
            conn = None
            fresh_workspaces = (version == 0 and chain[-1].n >= WORKSPACE_CONTRACT_SCHEMA
                                and workspace_store_is_new())
            snapshot = None
            started_chain = False
            for migration in chain:
                if migration.n <= version:
                    continue
                if migration.phase == "contract":
                    if not contracts:
                        return True
                    if migration.n == WORKSPACE_CONTRACT_SCHEMA and workspace_snapshots is None:
                        cfg, problem = config.load_or_problem()
                        if problem:
                            print(f"migration {migration.name} waits for valid config "
                                  f"({util.first_line(problem, 300)}); supply {WORKSPACE_HANDOFF}", flush=True)
                            return True
                        workspace_snapshots = cfg.workspace_snapshots
                    conn = connect_db(paths.DB_FILE)
                    conn.row_factory = sqlite3.Row
                    try:
                        if migration.n == WORKSPACE_CONTRACT_SCHEMA:
                            source = workspace_contract.snapshot_source(conn, workspace_snapshots)
                            blockers = workspace_handoff_blockers(conn, source, explicit=workspace_snapshots is not None)
                        else:
                            blockers = contract_blockers(conn, migration.n)
                    finally:
                        conn.close()
                        conn = None
                    if blockers:
                        print(f"migration {migration.name} waits for {', '.join(blockers)}", flush=True)
                        return True
                if not started_chain:
                    if paths.DB_FILE.stat().st_size:
                        snapshot = backup.snapshot(f"m{migration.n}")
                    started_chain = True
                started = time.monotonic()
                try:
                    timing = dbstats.Timing(paths.DB_FILE, "write")
                    with owning_transaction(lambda: connect_db(paths.DB_FILE), "BEGIN EXCLUSIVE", timing) as writer:
                        if migration.n == WORKSPACE_CONTRACT_SCHEMA:
                            source = workspace_contract.snapshot_source(writer, workspace_snapshots)
                            explicit = workspace_snapshots is not None
                            if blockers := workspace_handoff_blockers(writer, source, explicit=explicit):
                                print(f"migration {migration.name} waits for {', '.join(blockers)}", flush=True)
                                return True
                            from .. import install

                            if paths.DB_FILE.parent == paths.STATE_DIR and install.serve_guard_file().exists():
                                install.write_serve_guard()
                            allow_missing = workspace_contract.may_be_missing(writer, explicit)
                            try:
                                migration.apply(writer, source, allow_missing=allow_missing)
                            except OSError as e:
                                raise WorkspaceHandoffPending(workspace_source_problem(source, e)) from e
                        elif migration.sql is not None:
                            execute_statements(writer, migration.sql)
                        else:
                            migration.apply(writer)
                        if migration.n == 1 and chain[-1].n >= WORKSPACE_CONTRACT_SCHEMA:
                            writer.execute("INSERT INTO schema_meta (key, value) VALUES ('new_workspace_store', ?)",
                                           ("1" if fresh_workspaces else "0",))
                        writer.execute("INSERT INTO schema_migrations "
                                       "(n, name, area, phase, sha256, applied, backup, seconds) VALUES (?,?,?,?,?,?,?,?)",
                                       (migration.n, migration.name, migration.area, migration.phase, migration.sha256,
                                        util.iso(), snapshot.name if snapshot else "", time.monotonic() - started))
                        writer.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                                       (str(migration.n),))
                        writer.execute(f"PRAGMA user_version = {migration.n}")
                        if migration.n == WORKSPACE_CONTRACT_SCHEMA and (blockers := contract_blockers(writer, migration.n)):
                            raise WorkspaceHandoffPending(", ".join(blockers))
                except WorkspaceHandoffPending as e:
                    print(f"migration {migration.name} waits for {e}", flush=True)
                    return True
                except BaseException as e:
                    raise migrations.Refused(f"{migration.name}: {e}") from e
                version = migration.n
                print(f"applied {migration.name}", flush=True)
            backup.finish_restore()
            return False
        finally:
            if conn is not None:
                conn.close()


def _schema_key(identity, needed):
    return (os.getpid(), str(paths.DB_FILE), identity.st_dev, identity.st_ino,
            getattr(identity, "st_birthtime", None), LATEST, needed)


def checked_connection(*, timeout: float | None = None, on_demand: bool = False, needed: int | None = None):
    from .. import backup

    needed = NEEDED if needed is None else needed
    if backup.pending_restore():
        raise IncompatibleSchema("pending restore requires arete serve or arete migrate before writing")
    try:
        identity = paths.DB_FILE.stat()
    except FileNotFoundError:
        if not on_demand and (not paths.ISOLATED or timeout is not None):
            raise missing_database() from None
        migrate()
        identity = paths.DB_FILE.stat()
    if identity.st_size == 0:
        refuse_empty_file()
    try:
        conn = connect_db(paths.DB_FILE, timeout=timeout)
    except (sqlite3.OperationalError, FileNotFoundError):
        if not paths.DB_FILE.exists():
            raise missing_database() from None
        raise
    try:
        key = _schema_key(identity, needed)
        if key not in _CHECKED:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if on_demand and version < needed:
                conn.close()
                migrate()
                return checked_connection(timeout=timeout, needed=needed)
            if paths.ISOLATED and version == 0 and timeout is None:
                conn.close()
                migrate()
                return checked_connection(needed=needed)
            deadline = time.monotonic() + (
                MIGRATE_WAIT_SECONDS if timeout is None and needed != WORKSPACE_CONTRACT_SCHEMA else 0)
            if version < needed:
                print(f"schema {version} waits for schema {needed}; run arete serve or arete migrate", flush=True)
            while version < needed:
                if time.monotonic() >= deadline:
                    action = (f"supply {WORKSPACE_HANDOFF}" if needed == WORKSPACE_CONTRACT_SCHEMA
                              else "run arete serve or arete migrate")
                    raise IncompatibleSchema(f"schema {version} needs {needed}; {action}")
                time.sleep(0.5)
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            oldest = conn.execute("SELECT value FROM schema_meta WHERE key = 'oldest_writer'").fetchone()[0]
            if oldest is not None and int(oldest) > LATEST:
                raise IncompatibleSchema(f"oldest_writer {oldest} is above this code's schema {LATEST}")
            _CHECKED.add(key)
        return conn
    except BaseException:
        with suppress(BaseException):
            conn.close()
        raise


@contextmanager
def owning_transaction(connect, begin="BEGIN IMMEDIATE", timing=None, *, commit=True, before_commit=None):
    conn = None
    failed = False
    try:
        conn = connect()
        conn.row_factory = sqlite3.Row
        conn.execute(begin)
        if timing:
            timing.begun()
        yield conn
        if commit:
            if timing:
                timing.committing()
            try:
                if before_commit is not None:
                    before_commit()
                conn.execute("COMMIT")
            finally:
                if timing:
                    timing.committed()
    except BaseException as e:
        failed = True
        if timing:
            timing.error = type(e).__name__
            timing.sqlite_error = isinstance(e, sqlite3.Error)
        if conn is not None:
            with suppress(BaseException):
                conn.execute("ROLLBACK")
        raise
    finally:
        try:
            if conn is not None:
                if failed:
                    with suppress(BaseException):
                        conn.close()
                else:
                    conn.close()
        finally:
            if timing:
                timing.finished()


def read_connection(*, timeout: float | None = None):
    try:
        return connect_db(paths.DB_FILE, readonly=True, timeout=timeout)
    except (sqlite3.OperationalError, FileNotFoundError):
        if not paths.DB_FILE.exists():
            raise missing_database() from None
        raise


@contextmanager
def owned(mode, timeout, before_commit=None):
    bounded = _TRANSACTION_DEADLINE.get()
    if bounded:
        until, check = bounded
        check()
        timeout = 0
    active = getattr(_OWNER, "active", None)
    if active and active[0] == os.getpid():
        if before_commit is not None:
            raise RuntimeError("a commit guard requires an outer transaction")
        _, held_mode, conn = active
        if held_mode == "read" and mode == "write":
            raise RuntimeError("cannot promote a read snapshot to a writer")
        yield conn
        return
    timing = dbstats.Timing(paths.DB_FILE, mode)
    connect = checked_connection if mode == "write" else read_connection
    def guarded_commit():
        if before_commit is not None:
            before_commit()
        check()

    callbacks = []
    try:
        with owning_transaction(lambda: connect(timeout=timeout), "BEGIN IMMEDIATE" if mode == "write" else "BEGIN", timing,
                                commit=mode == "write", before_commit=guarded_commit if bounded else before_commit) as conn:
            _OWNER.active = (os.getpid(), mode, conn)
            _OWNER.after_commit = callbacks
            if bounded:
                check()
                conn.set_progress_handler(lambda: int(time.monotonic() >= until), 1000)
            try:
                yield conn
                if bounded and mode == "read":
                    check()
            except sqlite3.OperationalError as error:
                if bounded and error.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
                    check()
                raise
            finally:
                if bounded:
                    conn.set_progress_handler(None, 0)
    finally:
        _OWNER.active = None
        _OWNER.after_commit = None
    for callback in callbacks:
        callback()


@contextmanager
def tx(*, timeout: float | None = None, before_commit=None):
    with owned("write", timeout, before_commit) as conn:
        yield conn


db = tx


@contextmanager
def one_read(*, timeout: float | None = None):
    with owned("read", timeout) as conn:
        yield conn


@contextmanager
def reader(*, streaming=False):
    if not streaming:
        with one_read() as conn:
            yield conn
        return
    conn = connect_db(paths.DB_FILE, readonly=True, streaming=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def read_snapshot(conn):
    timing = dbstats.Timing(paths.DB_FILE, "read")
    try:
        conn.execute("BEGIN")
        timing.begun()
        yield conn
    except BaseException as e:
        timing.error = type(e).__name__
        timing.sqlite_error = isinstance(e, sqlite3.Error)
        raise
    finally:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            timing.finished()


def write_connection():
    active = getattr(_OWNER, "active", None)
    if not active or active[:2] != (os.getpid(), "write") or not active[2].in_transaction:
        raise RuntimeError("event emission needs an open write transaction")
    return active[2]


def after_commit(callback: Callable[[], None]) -> None:
    write_connection()
    _OWNER.after_commit.append(callback)


def transaction_mode():
    active = getattr(_OWNER, "active", None)
    if active and active[0] == os.getpid() and active[2].in_transaction:
        return active[1]
    return None


def in_write():
    return transaction_mode() == "write"


def ensure_schema() -> None:
    with db():
        pass


def migrate_on_demand(needed: int = WORKSPACE_SNAPSHOT_SCHEMA) -> None:
    active = getattr(_OWNER, "active", None)
    if active and active[0] == os.getpid():
        return
    needed = max(NEEDED, needed)
    try:
        if _schema_key(paths.DB_FILE.stat(), needed) in _CHECKED:
            return
    except FileNotFoundError:
        pass
    checked_connection(on_demand=True, needed=needed).close()


def require_workspace_contract() -> None:
    active = getattr(_OWNER, "active", None)
    if active and active[0] == os.getpid():
        if active[2].execute("PRAGMA user_version").fetchone()[0] < WORKSPACE_CONTRACT_SCHEMA:
            raise IncompatibleSchema(f"workspace access waits for schema {WORKSPACE_CONTRACT_SCHEMA}")
    else:
        migrate_on_demand(WORKSPACE_CONTRACT_SCHEMA)


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def metadata(conn) -> dict:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_meta'").fetchone():
        return {}
    return dict(conn.execute("SELECT key, value FROM schema_meta"))


def sequences(conn: sqlite3.Connection) -> dict[str, int]:
    try:
        return {name: seq for name, seq in conn.execute("SELECT name, seq FROM sqlite_sequence")}
    except sqlite3.OperationalError:
        return {}


def raise_sequences(staged: Path, current: dict[str, int] | None, gap: int) -> None:
    timing = dbstats.Timing(staged, "write")
    with owning_transaction(lambda: connect_db(staged), timing=timing) as conn:
        held = sequences(conn)
        for name in set(held) | set(current or {}):
            wanted = max(held.get(name, 0), (current or {}).get(name, 0)) if current is not None else held[name] + gap
            if name in held:
                conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (wanted, name))
            elif held or conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
                conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (name, wanted))


def apply_restore_metadata(target, data):
    with owning_transaction(lambda: connect_db(target), timing=dbstats.Timing(target, "write")) as conn:
        held = sequences(conn)
        has_sequence = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
        if has_sequence:
            for name, floor in data["sequences"].items():
                if name in held:
                    conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (max(held[name], floor), name))
                else:
                    conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (name, floor))
        meta = metadata(conn)
        if "restore_epoch" not in meta:
            return False
        from . import tokens

        tokens.invalidate_restore(conn, meta, data["epoch"])
        for key, value in {**data["identity"], "restore_epoch": str(data["epoch"])}.items():
            conn.execute("INSERT INTO schema_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))
    return True


def copy_database(target: Path) -> None:
    conn = connect_db(paths.DB_FILE, readonly=True)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


def table_counts(conn):
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {name: conn.execute(f"SELECT count(*) FROM {quoted(name)}").fetchone()[0] for name in names}


def quick_check(conn):
    return [r[0] for r in conn.execute("PRAGMA quick_check")]


def restore_epoch(conn):
    return int(conn.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0])


def inspect_database(path, check=True):
    conn = connect_db(path, readonly=True)
    try:
        conn.execute("BEGIN")
        return {"quick_check": "; ".join(quick_check(conn)) or "no answer" if check else "",
                "counts": table_counts(conn)}
    finally:
        conn.close()


def latest_migration_backup():
    conn = connect_db(paths.DB_FILE, readonly=True)
    try:
        row = conn.execute("SELECT backup FROM schema_migrations ORDER BY n DESC LIMIT 1").fetchone()
        return row[0] if row else ""
    except sqlite3.OperationalError:
        return ""
    finally:
        conn.close()


def check_readable(conn):
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()


def vacuum_into(conn, target):
    conn.execute("VACUUM INTO ?", (str(target),))


def schema_metadata():
    with reader() as conn:
        return dict(conn.execute("SELECT key, value FROM schema_meta"))


def migration_applied(n: int, conn=None) -> bool:
    if conn is None:
        with reader() as connection:
            return migration_applied(n, connection)
    return conn.execute("SELECT 1 FROM schema_migrations WHERE n = ?", (n,)).fetchone() is not None


def enable_wal():
    conn = connect_db(paths.DB_FILE)
    try:
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise RuntimeError("arete.db refused WAL mode")
    finally:
        conn.close()
