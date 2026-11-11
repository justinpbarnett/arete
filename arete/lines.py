from __future__ import annotations

import fcntl
import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import paths, util

FILE = "run.jsonl"
HEARTBEAT_SECONDS = 30
FOLLOW_POLL_SECONDS = 0.2
END = "report"
FIXED = ("ts", "run", "seq", "event")
WAIT_CAUSES = ("limit", "ci", "slot", "person")

_writing = threading.Lock()


def path(run_id: str) -> Path:
    return paths.RUNS_DIR / run_id / FILE


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


def write(run_id: str, event: str, **fields) -> dict:
    line = {"ts": util.iso(), "run": run_id, "seq": 0, "event": event,
            **{k: v for k, v in fields.items() if k not in FIXED}}
    target = path(run_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _writing, open(target, "a+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            data = f.read()
            line["ts"], line["seq"] = util.iso(), 1 + max(map(seq_of, whole(data)), default=0)
            text = json.dumps(line, ensure_ascii=False, default=str)
            f.write((b"\n" if data and not data.endswith(b"\n") else b"") + text.encode() + b"\n")
            f.flush()
    except OSError as e:
        text = json.dumps(line, ensure_ascii=False, default=str)
        print(f"{util.iso()} could not write {event} to {target}: {e}", file=sys.stderr, flush=True)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return line


def read(run_id: str) -> list[dict]:
    try:
        return whole(path(run_id).read_bytes())
    except FileNotFoundError:
        return []


def follow(run_id: str, start: int = 0) -> Iterator[dict]:
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
            time.sleep(FOLLOW_POLL_SECONDS)


def last_step(run_id: str) -> str:
    return next((str(line.get("step") or "") for line in reversed(read(run_id)) if line.get("event") == "step_start"),
                "")


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
    found = dict.fromkeys(WAIT_CAUSES, 0.0)
    for line in read(run_id):
        minutes = line.get("minutes")
        if (line.get("event") == "wait" and line.get("phase") == "end" and line.get("cause") in found
                and type(minutes) in (int, float)):
            found[line["cause"]] += minutes
    return {cause: round(minutes, 2) for cause, minutes in found.items()}


@contextmanager
def beating(run_id: str, step: str):
    started, stop = time.monotonic(), threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            write(run_id, "heartbeat", step=step, seconds=round(time.monotonic() - started, 1))

    beater = threading.Thread(target=beat, name=f"heartbeat {run_id}", daemon=True)
    beater.start()
    try:
        yield
    finally:
        stop.set()
        beater.join()
