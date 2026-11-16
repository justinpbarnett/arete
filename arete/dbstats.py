from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import sys
import threading
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime

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
            if module not in (__name__, "arete.state", "contextlib"):
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
            row = {"timestamp": self.timestamp, "pid": os.getpid(), "process_kind": process_kind(),
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
