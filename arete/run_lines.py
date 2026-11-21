from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import codebase, events, paths, principals, util
from .store import db as foundation
from .store import runs as run_store

FILE = "run.jsonl"
HEARTBEAT_SECONDS = 30
FOLLOW_POLL_SECONDS = 0.2
END = "report"
FIXED = ("ts", "run", "seq", "event")
WAIT_CAUSES = ("limit", "ci", "slot", "person")
TAIL_BYTES = 4096
GUARDED_TAIL_BYTES = 64 * 1024

_named: set[tuple[int, str]] = set()
_append_guard: ContextVar[Callable[[], None] | None] = ContextVar("run_line_append_guard", default=None)


class AppendRefused(RuntimeError):
    pass


@contextmanager
def nonblocking_appends(check: Callable[[], None]):
    token = _append_guard.set(check)
    try:
        check()
        yield
        check()
    finally:
        _append_guard.reset(token)


@contextmanager
def locked_log(target: Path, check: Callable[[], None] | None):
    if check:
        check()
    with open(target, "a+b", buffering=0 if check else -1) as stream:
        if check:
            check()
        # Each open has its own flock identity, even for threads in one process.
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if check else 0))
        except BlockingIOError as error:
            if check:
                raise AppendRefused("the run event log is locked") from error
            raise
        if check:
            check()
        yield stream


def path(run_id: str) -> Path:
    return paths.RUNS_DIR / run_id / FILE


def run_of_path(where: Path) -> str:
    return where.parent.name if where.name == FILE and where.parent.parent == paths.RUNS_DIR else ""


def whole(data: bytes) -> list[dict]:
    found = []
    for piece in data.split(b"\n")[:-1]:
        try:
            line = json.loads(piece)
        except (ValueError, RecursionError):
            continue
        if isinstance(line, dict):
            found.append(line)
    return found


def seq_of(line: dict) -> int:
    seq = line.get("seq")
    return seq if type(seq) is int else 0


def tail(f, check: Callable[[], None] | None = None) -> tuple[int, bool]:
    end = f.seek(0, 2)
    f.seek(max(0, end - 1))
    torn, size = bool(end) and f.read(1) != b"\n", TAIL_BYTES
    while True:
        if check:
            check()
            if min(end, size) > GUARDED_TAIL_BYTES:
                raise AppendRefused("cannot recover the run event sequence within the tail limit")
        start = max(0, end - size)
        f.seek(start)
        data = f.read(end - start)
        if start:
            data = data[data.find(b"\n") + 1:]
        if (seq := next((seq_of(line) for line in reversed(whole(data)) if seq_of(line)), 0)) or not start:
            return seq, torn
        size *= 2


def write(run_id: str, event: str, **fields) -> dict:
    line = {"ts": util.iso(), "run": run_id, "seq": 0, "event": event,
            **{k: v for k, v in fields.items() if k not in FIXED + ("engine",)}}
    target = path(run_id)
    check = _append_guard.get()
    try:
        if check:
            check()
        target.parent.mkdir(parents=True, exist_ok=True)
        with locked_log(target, check) as f:
            seq, torn = tail(f, check)
            line["ts"], line["seq"] = util.iso(), seq + 1
            first = (os.getpid(), run_id) not in _named
            if first:
                line["engine"] = codebase.STAMP
            text = json.dumps(line, ensure_ascii=False, default=str)
            payload = (b"\n" if torn else b"") + text.encode() + b"\n"
            if check:
                check()
            if f.write(payload) != len(payload):
                raise OSError("incomplete run event append")
            f.flush()
            if first:
                _named.add((os.getpid(), run_id))
    except OSError as e:
        if check:
            raise
        text = json.dumps(line, ensure_ascii=False, default=str)
        print(f"{util.iso()} could not write {event} to {target}: {e}", file=sys.stderr, flush=True)
    if event in ("step_start", "step_end"):
        step, attempt = str(fields.get("step", "")), fields.get("try", 1)
        with principals.acting(f"run:{run_id}/{step}"), foundation.tx():
            row = run_store.run_get(run_id)
            events.emit("step.started" if event == "step_start" else "step.ended",
                        f"step:{run_id}/{step}/{attempt}", cls="derived", task=row["issue"] if row else None,
                        data={**fields, "run": run_id, "step": step, "try": attempt})
    if check:
        check()
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    return line


def read(run_id: str) -> list[dict]:
    try:
        return whole(path(run_id).read_bytes())
    except FileNotFoundError:
        return []


def follow(run_id: str, start: int = 0, ended: Callable[[], bool] = lambda: False) -> Iterator[dict]:
    target, offset, rest = path(run_id), 0, b""
    while True:
        try:
            with open(target, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except FileNotFoundError:
            chunk = b""
        offset += len(chunk)
        rest += chunk
        cut = rest.rfind(b"\n") + 1
        done, rest = rest[:cut], rest[cut:]
        for line in whole(done):
            if seq_of(line) > start:
                yield line
            if line.get("event") == END:
                return
        if not chunk:
            if ended():
                return
            time.sleep(FOLLOW_POLL_SECONDS)


def last_step(run_id: str) -> str:
    return next((str(line.get("step") or "") for line in reversed(read(run_id)) if line.get("event") == "step_start"),
                "")


def ended_in_error(run_id: str, reply: str = "") -> bool:
    last = next((line for line in reversed(read(run_id)) if line.get("event") in ("error", "step_start")
                 and not (reply and line.get("event") == "step_start" and line.get("step") == reply)), {})
    return last.get("event") == "error"


def wait_start(run_id: str, cause: str, at: str) -> None:
    write(run_id, "wait", cause=cause, phase="start", at=at)


def wait_end(run_id: str, cause: str, since: str, until: str) -> None:
    seconds = (util.parse_iso(until) - util.parse_iso(since)).total_seconds()
    write(run_id, "wait", cause=cause, phase="end", at=until, since=since, minutes=round(max(0.0, seconds) / 60, 2))


def waited(run_id: str, cause: str, since: str, until: str) -> None:
    wait_start(run_id, cause, since)
    wait_end(run_id, cause, since, until)


@contextmanager
def waiting(run_id: str, cause: str):
    since = util.iso()
    wait_start(run_id, cause, since)
    try:
        yield
    finally:
        wait_end(run_id, cause, since, util.iso())


def wait_minutes(run_id: str) -> dict:
    found: dict[str, float] = {}
    for line in read(run_id):
        minutes = line.get("minutes")
        if (line.get("event") == "wait" and line.get("phase") == "end" and line.get("cause") in WAIT_CAUSES
                and type(minutes) in (int, float)):
            found[line["cause"]] = found.get(line["cause"], 0.0) + minutes
    return {cause: round(minutes, 2) for cause, minutes in found.items()}


@contextmanager
def beating(run_id: str, step: str):
    started = time.monotonic()
    append_guard = _append_guard.get()
    closed = threading.Event()

    def check():
        append_guard()
        if closed.is_set():
            raise AppendRefused("the step heartbeat ended")

    def beat() -> None:
        if append_guard is None:
            write(run_id, "heartbeat", step=step, seconds=round(time.monotonic() - started, 1))
            return
        try:
            with nonblocking_appends(check):
                write(run_id, "heartbeat", step=step, seconds=round(time.monotonic() - started, 1))
        except (OSError, RuntimeError):
            return  # A diagnostic heartbeat must not delay the step or its unwind.

    with util.repeating(HEARTBEAT_SECONDS, beat, f"heartbeat {run_id}", wait_on_exit=append_guard is None):
        try:
            yield
        finally:
            closed.set()
