import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path

RECEIPT = "deploy-receipt.json"
HOLDS = "deploy-holds"
LOCK_FD = "ARETE_SERVE_LOCK_FD"
START_LIMIT = 3
AGE_LIMIT = 5 * 60
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
IMPORT_FAILURE = "the candidate failed the import check"
MIGRATION_FAILURE = "the candidate failed the migration check"
AGE_FAILURE = "the deploy receipt is older than 5 minutes"
MIGRATION_FILE = re.compile(r"m([0-9]{4})_[a-z0-9_]+\.py")


@dataclass(frozen=True)
class Receipt:
    checkout: str
    branch: str
    previous: str
    tip: str
    github: str
    created_at: float
    starts: int = 0
    healthy: bool = False
    settled: str = ""

    def expired(self) -> bool:
        return time.time() - self.created_at >= AGE_LIMIT


def read(file: Path) -> dict | None:
    try:
        return json.loads(file.read_text())
    except FileNotFoundError:
        return None


def write(file: Path, data: dict) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=file.parent, prefix=f".{file.name}.", delete=False) as out:
        draft = Path(out.name)
        try:
            out.write(json.dumps(data).encode())
            out.flush()
            os.fsync(out.fileno())
            os.replace(draft, file)
        finally:
            draft.unlink(missing_ok=True)
    flush_directory(file.parent)


def flush_directory(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pending(state: Path, checkout: Path) -> Receipt | None:
    data = read(state / RECEIPT)
    if data is None:
        return None
    receipt = Receipt(**{field.name: data[field.name] for field in fields(Receipt) if field.name in data})
    if receipt.checkout != str(checkout) or receipt.healthy or receipt.settled:
        return None
    if not all(SHA.fullmatch(sha) for sha in (receipt.previous, receipt.tip)):
        raise ValueError("deploy receipt needs full commit IDs")
    return receipt


def hold_path(state: Path, tip: str) -> Path:
    return state / HOLDS / f"{tip}.json"


def git(checkout: Path, *args: str) -> str:
    ran = subprocess.run(["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
                          "-c", "core.fsmonitor=false", *args], cwd=checkout, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, errors="replace", timeout=30,
                         env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    if ran.returncode:
        raise RuntimeError(f"git {args[0]}: {ran.stderr[:300].strip()}")
    return ran.stdout.strip()


def log_tail(logs: Path) -> str:
    for name in ("serve.log", "serve.log.1"):
        try:
            with (logs / name).open("rb") as log:
                log.seek(max(0, os.fstat(log.fileno()).st_size - 8192))
                if tail := log.read(8192).decode(errors="replace").strip():
                    return "\n".join(tail.splitlines()[-40:])
        except OSError:
            continue
    return "(serve.log tail unavailable)"


def report_reason(held: dict) -> str:
    reason = held.get("reason", "")
    if isinstance(reason, str) and (
            reason in (IMPORT_FAILURE, MIGRATION_FAILURE, AGE_FAILURE)
            or re.fullmatch(r"[0-9]{1,6} unhealthy starts", reason)):
        return reason
    return "startup health was not confirmed"


def at_tip(checkout: Path, receipt: Receipt) -> bool:
    head = git(checkout, "rev-parse", "HEAD", "--symbolic-full-name", "HEAD").splitlines()
    return head == [receipt.tip, f"refs/heads/{receipt.branch}"]


def migration_manifest(checkout: Path) -> dict[str, str]:
    folder = "arete/store/migrations"
    manifest = {}
    for file in git(checkout, "ls-files", "--", folder).splitlines():
        path = checkout / file
        if path.parent != checkout / folder or not re.fullmatch(r"m[0-9]{4}_[a-z0-9_]+\.py", path.name):
            continue
        try:
            manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return manifest


def previous_schema(checkout: Path, receipt: Receipt) -> int:
    files = git(checkout, "ls-tree", "--name-only", receipt.previous, "arete/store/migrations/").splitlines()
    return max((int(found[1]) for name in files if (found := MIGRATION_FILE.fullmatch(Path(name).name))), default=0)


def contract_snapshot(state: Path, receipt: Receipt) -> Path:
    return state / HOLDS / f"{receipt.tip}.before-contract.db"


def applied_contract(conn: sqlite3.Connection, previous: int) -> str:
    if not conn.execute("PRAGMA user_version").fetchone()[0]:
        return ""
    row = conn.execute("SELECT name FROM schema_migrations WHERE n > ? AND phase = 'contract' ORDER BY n",
                       (previous,)).fetchone()
    return row[0] if row else ""


def lock_database(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA synchronous=FULL")
    if conn.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0] != "exclusive":
        raise RuntimeError("database recovery requires exclusive SQLite access")
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("COMMIT")


def snapshot_database(conn: sqlite3.Connection, file: Path) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=file.parent, prefix=f".{file.name}.", delete=False) as out:
        draft = Path(out.name)
        try:
            conn.execute("VACUUM INTO ?", (out.name,))
            os.fsync(out.fileno())
            os.replace(draft, file)
        finally:
            draft.unlink(missing_ok=True)
    flush_directory(file.parent)


def recover_database(state: Path, checkout: Path, receipt: Receipt) -> Path | None:
    database = state / "arete.db"
    if not database.is_file():
        return None
    latest = previous_schema(checkout, receipt)
    archive = state / HOLDS / f"{receipt.tip}.failed.db"
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)) as conn:
        contract = applied_contract(conn, latest)
    if not contract:
        return archive if archive.exists() else None
    snapshot = contract_snapshot(state, receipt)
    if not snapshot.is_file():
        raise RuntimeError(f"{contract} requires recovery but {snapshot} is missing; restore a pre-contract backup")
    with (closing(sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True, timeout=5)) as source,
          closing(sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True, timeout=5)) as live):
        lock_database(live)
        if (source.execute("PRAGMA quick_check").fetchall() != [("ok",)] or applied_contract(source, latest)
                or source.execute("PRAGMA user_version").fetchone()[0] >= live.execute("PRAGMA user_version").fetchone()[0]):
            raise RuntimeError(f"{snapshot} is not a usable pre-contract backup")
        snapshot_database(live, archive)
        source.backup(live)
    return archive


def rollback(state: Path, checkout: Path, logs: Path, receipt: Receipt, reason: str) -> bool:
    hold = hold_path(state, receipt.tip)
    migration_lock = state / "locks" / "migrate.lock"
    migration_lock.parent.mkdir(parents=True, exist_ok=True)
    with migration_lock.open("a") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        if not at_tip(checkout, receipt):
            return False
        if git(checkout, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=untracked"):
            raise RuntimeError("deploy rollback refused because the checkout has tracked changes")
        if Path(git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
            raise RuntimeError("deploy rollback refused outside the recorded checkout")
        if git(checkout, "merge-base", receipt.previous, receipt.tip) != receipt.previous:
            raise RuntimeError("deploy rollback refused because the previous commit is not an ancestor")
        if not hold.exists():
            write(hold, {**asdict(receipt), "reason": reason, "log_tail": log_tail(logs),
                         "migrations": migration_manifest(checkout)})
        archive = recover_database(state, checkout, receipt)
        if archive is not None and not at_tip(checkout, receipt):
            raise RuntimeError("deploy rollback refused because HEAD changed during database recovery")
        git(checkout, "reset", "--keep", receipt.previous)
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
          f"deploy: rolled back to {receipt.previous} because {reason}", flush=True)
    if archive is not None:
        print(f"deploy: restored the pre-contract database; candidate data is preserved in {archive}", flush=True)
    write(state / RECEIPT, asdict(replace(receipt, settled="rolled_back")))
    return True


def count_start(state: Path, checkout: Path, logs: Path) -> bool:
    receipt = pending(state, checkout)
    if receipt is None or not at_tip(checkout, receipt):
        return False
    held = read(hold_path(state, receipt.tip))
    reason = (report_reason(held) if held else AGE_FAILURE if receipt.expired()
              else f"{receipt.starts} unhealthy starts" if receipt.starts >= START_LIMIT else "")
    receipt = replace(receipt, starts=receipt.starts + 1)
    write(state / RECEIPT, asdict(receipt))
    return bool(reason) and rollback(state, checkout, logs, receipt, reason)


def lock_start(state: Path) -> None:
    path = state / "locks" / "serve.lock"
    if value := os.environ.get(LOCK_FD):
        fd = int(value)
        if not os.path.samestat(os.fstat(fd), path.stat()):
            raise ValueError("descriptor does not name the serve lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"arete serve {path.read_text().strip()} already holds state folder {state}") from error
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.set_inheritable(fd, True)
    except BaseException:
        os.close(fd)
        raise
    os.environ[LOCK_FD] = str(fd)


def startup(state: Path, checkout: Path, logs: Path) -> None:
    try:
        if pending(state, checkout) is None:
            return
        lock_start(state)
        if count_start(state, checkout, logs):
            os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        raise SystemExit(f"deploy: startup guard: {error}") from error
