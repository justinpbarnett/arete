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
AGE_FAILURE = "the deploy receipt is older than 5 minutes"
MISSING_TAIL = "(serve.log tail unavailable)"


def is_serve_start(argv: list[str]) -> bool:
    return argv[:1] == ["serve"] and all(arg.startswith("--n") and "--no-dashboard".startswith(arg) for arg in argv[1:])


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
    rollback: str = ""
    started_at: float | None = None

    def expired(self) -> bool:
        return bool(self.starts and time.time() - (
            self.created_at if self.started_at is None else self.started_at) >= AGE_LIMIT)


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
    if data is None or data.get("checkout") != str(checkout):
        return None
    receipt = Receipt(**{field.name: data[field.name] for field in fields(Receipt) if field.name in data})
    if receipt.healthy or receipt.settled:
        return None
    if not all(SHA.fullmatch(sha) for sha in (receipt.previous, receipt.tip)):
        raise ValueError("deploy receipt needs full commit IDs")
    return receipt


def hold_path(state: Path, tip: str) -> Path:
    return state / HOLDS / f"{tip}.json"


def git_bytes(checkout: Path, *args: str) -> bytes:
    ran = subprocess.run(["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
                          "-c", "core.fsmonitor=false", *args], cwd=checkout, stdin=subprocess.DEVNULL,
                         capture_output=True, timeout=30,
                         env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    if ran.returncode:
        raise RuntimeError(f"git {args[0]}: {ran.stderr[:300].decode(errors='replace').strip()}")
    return ran.stdout


def git(checkout: Path, *args: str) -> str:
    return git_bytes(checkout, *args).decode(errors="replace").strip()


def log_tail(logs: Path) -> str:
    for name in ("serve.log", "serve.log.1"):
        try:
            with (logs / name).open("rb") as log:
                log.seek(max(0, os.fstat(log.fileno()).st_size - 8192))
                if tail := log.read(8192).decode(errors="replace").strip():
                    return "\n".join(tail.splitlines()[-40:])
        except OSError:
            continue
    return MISSING_TAIL


def report_reason(held: dict) -> str:
    reason = held.get("reason", "")
    if isinstance(reason, str) and (
            reason in ("the candidate failed the import check", "the candidate failed the migration check", AGE_FAILURE)
            or re.fullmatch(r"[0-9]{1,6} unhealthy starts", reason)):
        return reason
    return "startup health was not confirmed"


def at_tip(checkout: Path, receipt: Receipt) -> bool:
    head = git(checkout, "rev-parse", "HEAD", "--symbolic-full-name", "HEAD").splitlines()
    return head == [receipt.tip, f"refs/heads/{receipt.branch}"]


def rollback_schema_refusal(state: Path, checkout: Path, previous: str) -> str:
    db = state / "arete.db"
    try:
        if not db.stat().st_size:
            return ""
        with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 27:
                return ""
            contracts = list(conn.execute("SELECT name, sha256 FROM schema_migrations WHERE n >= 27 "
                                          "AND phase = 'contract'"))
    except FileNotFoundError:
        return ""
    except (OSError, sqlite3.Error, ValueError) as e:
        return f"could not verify the database schema ({e})"
    if not contracts:
        return "the database contract ledger is missing"
    for name, digest in contracts:
        try:
            body = git_bytes(checkout, "show", f"{previous}:arete/store/migrations/{name}")
        except (RuntimeError, subprocess.SubprocessError):
            body = b""
        if hashlib.sha256(body).hexdigest() != digest:
            return f"the previous code cannot open a database with {name} applied"
    return ""


def rollback(state: Path, checkout: Path, logs: Path, receipt: Receipt, reason: str) -> bool:
    if not at_tip(checkout, receipt):
        return False
    if refused := rollback_schema_refusal(state, checkout, receipt.previous):
        print(f"deploy: code-only rollback refused because {refused}", flush=True)
        return False
    if git(checkout, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=untracked"):
        raise RuntimeError("deploy rollback refused because the checkout has tracked changes")
    if Path(git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
        raise RuntimeError("deploy rollback refused outside the recorded checkout")
    if git(checkout, "merge-base", receipt.previous, receipt.tip) != receipt.previous:
        raise RuntimeError("deploy rollback refused because the previous commit is not an ancestor")
    hold = hold_path(state, receipt.tip)
    if not hold.exists():
        write(hold, {**asdict(receipt), "reason": reason, "log_tail": log_tail(logs)})
    git(checkout, "reset", "--keep", receipt.previous)
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
          f"deploy: rolled back to {receipt.previous} because {reason}", flush=True)
    write(state / RECEIPT, asdict(replace(receipt, settled="rolled_back", rollback="code_only")))
    return True


def rollback_if_unhealthy(state: Path, checkout: Path, logs: Path, receipt: Receipt) -> bool:
    if held := read(hold_path(state, receipt.tip)):
        reason = report_reason(held)
    elif receipt.expired():
        reason = AGE_FAILURE
    elif receipt.starts >= START_LIMIT:
        reason = f"{receipt.starts} unhealthy starts"
    else:
        return False
    return rollback(state, checkout, logs, receipt, reason)


def record_start(state: Path, receipt: Receipt, at: float) -> None:
    started_at = receipt.started_at
    if started_at is None:
        started_at = receipt.created_at if receipt.starts else at
    receipt = replace(receipt, starts=receipt.starts + 1, started_at=started_at)
    write(state / RECEIPT, asdict(receipt))


def count_start(state: Path, checkout: Path, logs: Path) -> bool:
    receipt = pending(state, checkout)
    if receipt is None or not at_tip(checkout, receipt):
        return False
    if rollback_if_unhealthy(state, checkout, logs, receipt):
        return True
    record_start(state, receipt, time.time())
    return False


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
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"deploy: startup guard: {error}") from error


def main() -> None:
    state, checkout, logs = map(Path, sys.argv[1:4])
    startup(state, checkout, logs)
    os.execv(sys.executable, [sys.executable, "-I", "-S", "-c", sys.argv[4], *sys.argv])


if __name__ == "__main__":
    main()
