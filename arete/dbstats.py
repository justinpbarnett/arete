from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import stat
import sys
import threading
import time
import uuid
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import paths, util

LOCK = threading.Lock()
INCARNATION = uuid.uuid4().hex
SUMMARIES = {}


def after_fork():
    global LOCK, INCARNATION, SUMMARIES
    LOCK = threading.Lock()
    INCARNATION = uuid.uuid4().hex
    SUMMARIES = {}


os.register_at_fork(after_in_child=after_fork)


def caller():
    frame = inspect.currentframe()
    try:
        while frame:
            module = frame.f_globals.get("__name__", "")
            if module not in (__name__, "contextlib") and not module.startswith("arete.store."):
                return f"{module}.{frame.f_code.co_name}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


def database_identity(path):
    identity = [str(path.absolute())]
    try:
        st = path.stat()
        identity.extend((st.st_dev, st.st_ino, getattr(st, "st_birthtime", None)))
    except OSError:
        identity.append("missing")
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def process_kind():
    return "run" if os.environ.get("ARETE_RUN_ID") else next(
        (arg for arg in sys.argv[1:] if arg in ("serve", "migrate", "doctor", "loop", "backup")), "cli")


class Timing:
    def __init__(self, path, mode):
        self.started = time.monotonic()
        self.timestamp = datetime.now(UTC).isoformat()
        self.hour = self.timestamp[:13] + ":00:00+00:00"
        self.path = path
        self.database = database_identity(path)
        self.mode = mode
        self.caller = caller()
        self.begin_ms = None
        self.commit_start = None
        self.commit_ms = 0
        self.error = ""
        self.sqlite_error = False

    def begun(self):
        self.begin_ms = (time.monotonic() - self.started) * 1000
        self.database = database_identity(self.path)

    def committing(self):
        self.commit_start = time.monotonic()

    def committed(self):
        self.commit_ms = (time.monotonic() - self.commit_start) * 1000

    def finished(self):
        with suppress(BaseException):
            ended = time.monotonic()
            total = (ended - self.started) * 1000
            row = {"timestamp": self.timestamp, "database": self.database,
                   "pid": os.getpid(), "process_kind": process_kind(),
                   "caller": self.caller, "mode": self.mode, "total_ms": total,
                   "begin_ms": total if self.begin_ms is None else self.begin_ms,
                   "commit_ms": self.commit_ms,
                   "error": self.error}
            if total > 500 or self.sqlite_error:
                with suppress(BaseException):
                    append(row)
            retain(self.database, self.hour, row)


def append(row):
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(paths.LOG_DIR / "db.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def retain(database, hour, row):
    with LOCK:
        key = (str(paths.LOG_DIR), database, hour)
        summary = SUMMARIES.setdefault(key, {
            "format": 1, "database": database, "incarnation": INCARNATION, "hour": hour,
            "pid": os.getpid(), "process_kind": row["process_kind"], "transactions": {}})
        group = summary["transactions"].setdefault(row["mode"] + ":" + row["caller"], {
            "count": 0, "total_ms": 0, "begin_ms": 0, "commit_ms": 0, "max_ms": 0,
            "histogram": {}, "errors": {}})
        group["count"] += 1
        for field in ("total_ms", "begin_ms", "commit_ms"):
            group[field] += row[field]
        group["max_ms"] = max(group["max_ms"], row["total_ms"])
        bucket = str(max(0, math.ceil(math.log2(max(1, row["total_ms"])))))
        group["histogram"][bucket] = group["histogram"].get(bucket, 0) + 1
        if row["error"]:
            group["errors"][row["error"]] = group["errors"].get(row["error"], 0) + 1
        folder = paths.LOG_DIR / "dbstats"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{database}-{INCARNATION}-{hour[:13].replace(':', '')}.json"
        util.write_durably(folder / name, json.dumps(summary).encode(), 0o600)


def log_records(start, end, database=None):
    database = database or database_identity(paths.DB_FILE)
    for file in paths.LOG_DIR.glob("db.log*"):
        if file.name != "db.log" and not file.name.removeprefix("db.log.").isdigit():
            continue
        try:
            with file.open(errors="replace") as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        continue
                    try:
                        row = json.loads(line)
                        at = datetime.fromisoformat(row["timestamp"])
                        ms = row["total_ms"]
                        if (start <= at < end and row.get("database", database) == database
                                and type(ms) in (float, int) and math.isfinite(ms) and ms >= 0):
                            yield row
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue
        except (OSError, UnicodeError):
            continue


def file_bytes(file):
    try:
        info = file.lstat()
        return info.st_size if stat.S_ISREG(info.st_mode) else 0
    except OSError:
        return 0


def database_bytes():
    return {"db_bytes": file_bytes(paths.DB_FILE),
            "wal_bytes": file_bytes(paths.DB_FILE.with_name(paths.DB_FILE.name + "-wal"))}


def runs_bytes():
    record = other = 0
    if paths.RUNS_DIR.is_symlink():
        return {"record_bytes": 0, "other_bytes": 0, "total_bytes": 0}
    for folder, dirs, files in os.walk(paths.RUNS_DIR, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
        relative = Path(folder).relative_to(paths.RUNS_DIR)
        for name in files:
            size = file_bytes(Path(folder) / name)
            if len(relative.parts) >= 2 and relative.parts[1] == "record":
                record += size
            else:
                other += size
    return {"record_bytes": record, "other_bytes": other, "total_bytes": record + other}


def transaction_totals(start, end, database):
    count = 0
    maximum = 0.0
    histogram = Counter()
    kinds = Counter()
    seen = set()
    for file in (paths.LOG_DIR / "dbstats").glob("*.json"):
        try:
            summary = json.loads(file.read_text())
            hour = datetime.fromisoformat(summary["hour"])
            identity = (summary["incarnation"], hour)
            if (summary["format"] != 1 or summary["database"] != database
                    or not start <= hour < end or identity in seen
                    or hour.utcoffset() != timedelta(0) or hour.minute or hour.second or hour.microsecond
                    or not isinstance(summary["incarnation"], str) or not summary["incarnation"]):
                continue
            groups = list(summary["transactions"].values())
            for group in groups:
                if (type(group["count"]) is not int or group["count"] < 0
                        or not math.isfinite(group["max_ms"]) or group["max_ms"] < 0
                        or sum(group["histogram"].values()) != group["count"]
                        or any(type(n) is not int or n < 0 or not 0 <= int(b) <= 64
                               for b, n in group["histogram"].items())):
                    raise ValueError("invalid transaction summary")
            kind = summary["process_kind"]
            if not isinstance(kind, str):
                continue
            seen.add(identity)
            for group in groups:
                count += group["count"]
                kinds[kind] += group["count"]
                maximum = max(maximum, group["max_ms"])
                histogram.update({int(b): n for b, n in group["histogram"].items()})
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    rank, accumulated, p95 = math.ceil(count * .95), 0, None
    for bucket, n in sorted(histogram.items()):
        accumulated += n
        if rank and accumulated >= rank:
            p95 = min(maximum, 2 ** bucket)
            break
    return {"transaction_count": count, "p95_ms": p95, "max_ms": maximum if count else None,
            "transactions_by_kind": dict(kinds)}


def empty_collection():
    return dict.fromkeys(("window_start", "window_end", "collected_at", "transaction_count", "p95_ms", "max_ms",
                          "transactions_by_kind", "slow_by_kind", "errors_by_kind", "errors_by_class",
                          "db_bytes", "wal_bytes", "record_bytes", "other_bytes", "total_bytes", "tables"))


def collect(now=None):
    from .store import db as foundation

    now = (now or datetime.now(UTC)).astimezone(UTC)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    database = database_identity(paths.DB_FILE)
    totals = transaction_totals(start, end, database)
    slow, error_kinds, errors = Counter(), Counter(), Counter()
    for row in log_records(start, end, database):
        kind = str(row.get("process_kind") or "unknown")
        if row["total_ms"] > 500:
            slow[kind] += 1
        if error := row.get("error"):
            error_kinds[kind] += 1
            errors[str(error)] += 1
    with foundation.one_read() as conn:
        tables = foundation.table_counts(conn)
    return {**totals, "window_start": start.isoformat(), "window_end": end.isoformat(),
            "collected_at": now.isoformat(), "slow_by_kind": dict(slow), "errors_by_kind": dict(error_kinds),
            "errors_by_class": dict(errors), **database_bytes(), **runs_bytes(), "tables": tables}
