from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import socket
import sqlite3
import stat
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import local_paths

MARKER = b"arete-verification-scratch-v1"
DASHBOARD_PREFIX = ".arete-verify-dashboard-"
SPEC = "verify-faults.json"
REFUSAL = ("verify-fault requires ARETE_STATE_DIR under a user-owned verification scratch directory "
           "with a valid .arete-owned marker. Run it through the verification sandbox wrapper.")
EVENT_KIND = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
LOG = logging.getLogger(__name__)


def state_path() -> Path | None:
    value = os.environ.get("ARETE_STATE_DIR")
    return Path(value).absolute() if value else None


@contextmanager
def plain_file(directory: int, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise PermissionError(f"{name} must be a plain file owned by the current user")
        yield source


@contextmanager
def scratch_state(state: Path | None):
    if state is None or not state.name or ".." in state.parts:
        raise PermissionError(REFUSAL)
    root = os.open(state.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(root).st_uid != os.getuid():
            raise PermissionError(REFUSAL)
        with plain_file(root, ".arete-owned") as marker:
            if marker.read(len(MARKER) + 2) not in (MARKER, MARKER + b"\n"):
                raise PermissionError(REFUSAL)
        with local_paths.directory_fd(Path(state.name), root) as directory:
            if os.fstat(directory).st_uid != os.getuid():
                raise PermissionError(REFUSAL)
            yield directory
    finally:
        os.close(root)


def validate_arm(route, status, body, count) -> None:
    if (not isinstance(route, str) or not route.startswith("/") or route.startswith("//") or "#" in route
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in route)):
        raise ValueError("Use an absolute route path, optionally with a query string.")
    if type(status) is not int or not 200 <= status <= 599 or type(count) is not int or count < 1:
        raise ValueError("Use an HTTP status from 200 to 599 and a positive count.")
    if not isinstance(body, str):
        raise ValueError("Use text for the response body.")
    body.encode()


def validate_event(kind, run) -> None:
    if not isinstance(kind, str) or not EVENT_KIND.fullmatch(kind) or not isinstance(run, str) or not run:
        raise ValueError("Use a dotted event kind and an existing scratch run ID.")
    run.encode()


def read_spec(directory: int) -> dict:
    try:
        with plain_file(directory, SPEC) as source:
            spec = json.load(source)
    except FileNotFoundError:
        return {"faults": {}, "drop": "", "events": []}
    try:
        if not isinstance(spec["drop"], str) or not isinstance(spec["events"], list):
            raise TypeError
        for route, fault in spec["faults"].items():
            validate_arm(route, fault["status"], fault["body"], fault["count"])
        for event in spec["events"]:
            if not isinstance(event["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", event["id"]):
                raise TypeError
            validate_event(event["kind"], event["run"])
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError(f"Invalid {SPEC}. Run verify-fault clear to reset it.") from None
    return spec


def write_spec(directory: int, spec: dict) -> None:
    payload = json.dumps(spec).encode()
    temp = f".{SPEC}.{uuid4().hex}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, SPEC, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=directory)


def update(directory: int, args) -> None:
    fcntl.flock(directory, fcntl.LOCK_EX)
    try:
        spec = read_spec(directory)
    except (ValueError, PermissionError):
        if args.fault_command != "clear":
            raise
        spec = {"faults": {}, "drop": "", "events": []}
    if args.fault_command == "arm":
        validate_arm(args.route, args.status, args.body, args.count)
        path, _, query = args.route.partition("?")
        route = args.route if query else path
        spec["faults"][route] = {"status": args.status, "body": args.body, "count": args.count}
    elif args.fault_command == "drop-streams":
        spec["drop"] = uuid4().hex
    elif args.fault_command == "event":
        validate_event(args.kind, args.run)
        spec["events"].append({"id": uuid4().hex, "kind": args.kind, "run": args.run})
    else:
        spec["faults"].clear()
        spec["events"].clear()
    write_spec(directory, spec)


def emit(pending: list[dict]) -> None:
    from . import idempotency, principals
    from .store import db, events, writes
    from .store import idempotency as claims

    via = "verify-fault event"
    with db.tx() as conn, principals.acting(principals.owner()), events.request_scope(via):
        for event in pending:
            key = f"verify-fault:{event['id']}"
            run, kind = event["run"], event["kind"]
            context = ("hand", key, via, idempotency.fingerprint("event", via, {}, [kind, run]))
            if idempotency.matching_claim(context):
                continue
            row = writes.current(conn, "runs", run)
            seq = writes.emit(conn, "runs", run, row, kind) if row else None
            claims.complete(context, 200 if row else 404, b"", seq)
            if row is None:
                LOG.warning("verify-fault event: no such scratch run %r", run)


@dataclass(frozen=True)
class Fault:
    status: int
    body: bytes


class Controls:
    def __init__(self, state: Path):
        self.state = state
        self.lock = threading.Lock()
        self.drop = ""
        self.streams: dict[socket.socket, threading.Event] = {}

    def poll(self, route: str = "", query: str = "") -> Fault | None:
        with self.lock:
            return self.observe(route, query)

    def observe(self, route: str = "", query: str = "") -> Fault | None:
        try:
            with scratch_state(self.state) as directory:
                fcntl.flock(directory, fcntl.LOCK_EX)
                spec = read_spec(directory)
                if spec["drop"] and spec["drop"] != self.drop:
                    self.drop = spec["drop"]
                    for connection, dropped in self.streams.items():
                        if not dropped.is_set():
                            dropped.set()
                            with suppress(OSError):
                                connection.shutdown(socket.SHUT_RDWR)
                fault = self.consume(spec, route, query)
                emitted = False
                if pending := spec["events"]:
                    try:
                        emit(pending)
                    except sqlite3.Error as error:
                        LOG.warning("verify-fault event: %s", error)
                    else:
                        pending.clear()
                        emitted = True
                if fault or emitted:
                    write_spec(directory, spec)
                return fault
        except (OSError, ValueError):
            return None

    def consume(self, spec: dict, route: str, query: str) -> Fault | None:
        faults = spec["faults"]
        exact = f"{route}?{query}"
        key = exact if query and exact in faults else route
        if armed := faults.get(key):
            armed["count"] -= 1
            if not armed["count"]:
                del faults[key]
            return Fault(armed["status"], armed["body"].encode())
        return None

    @contextmanager
    def stream(self, connection):
        dropped = threading.Event()
        with self.lock:
            self.observe()
            self.streams[connection] = dropped
        try:
            def tick():
                if not dropped.is_set():
                    self.poll()
                return dropped.is_set()

            yield tick
        finally:
            with self.lock:
                self.streams.pop(connection, None)
