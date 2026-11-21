from __future__ import annotations

import hmac
import re
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import api_http
from .refusals import Stated
from .store import db, writes

ENV = "ARETE_VERIFY_FAULTS"
HEADER = "X-Arete-Verify-Faults"
CHALLENGE_HEADER = "X-Arete-Verify-Fault-Challenge"
PROOF_HEADER = "X-Arete-Verify-Fault-Proof"
PREFIX = "/api/_verify"
FAULT = PREFIX + "/fault"
DROP = PREFIX + "/streams/drop"
EVENT = PREFIX + "/event"
CLEAR = PREFIX + "/faults"
ROUTES = {("POST", FAULT), ("POST", DROP), ("POST", EVENT), ("DELETE", CLEAR)}
FAULT_INPUT = "Send a route, HTTP status, JSON body and positive count."
EVENT_KIND = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
CHALLENGE = re.compile(r"[0-9a-f]{48}")


def is_control(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def discovery_proof(token: bytes, challenge: str) -> str:
    return hmac.digest(token, b"arete.verify.fault-discovery\0" + challenge.encode(), "sha256").hex()


def route_of(route) -> tuple[str, str]:
    if isinstance(route, str) and route.startswith("/") and not route.startswith("//"):
        parts = urlsplit(route)
        if not is_control(parts.path) and "#" not in route and not any(ch.isspace() or ord(ch) < 32 for ch in route):
            return parts.path, parts.query
    raise Stated(400, FAULT_INPUT)


@dataclass
class Fault:
    status: int
    body: bytes
    count: int


class Controls:
    def __init__(self, token: str):
        self.token = token.encode()
        self.lock = threading.Lock()
        self.faults: dict[tuple[str, str], Fault] = {}
        self.streams: dict[socket.socket, threading.Event] = {}

    def allows(self, headers) -> bool:
        values = headers.get_all(HEADER, [])
        return len(values) == 1 and hmac.compare_digest(self.token, values[0].encode())

    def prove(self, headers) -> str:
        values = headers.get_all(CHALLENGE_HEADER, [])
        return discovery_proof(self.token, values[0]) if len(values) == 1 and CHALLENGE.fullmatch(values[0]) else ""

    def arm(self, data: dict) -> None:
        status, count = data.get("status"), data.get("count")
        if type(status) is not int or not 200 <= status <= 599 or type(count) is not int or count < 1 or "body" not in data:
            raise Stated(400, FAULT_INPUT)
        route = route_of(data.get("route"))
        fault = Fault(status, api_http.json_bytes(data["body"]), count)
        with self.lock:
            self.faults[route] = fault

    def clear(self) -> None:
        with self.lock:
            self.faults.clear()

    def take(self, path: str, query: str) -> Fault | None:
        with self.lock:
            route = (path, query) if (path, query) in self.faults else (path, "")
            fault = self.faults.get(route)
            if fault is not None:
                fault.count -= 1
                if not fault.count:
                    del self.faults[route]
            return fault

    @contextmanager
    def stream(self, connection):
        dropped = threading.Event()
        with self.lock:
            self.streams[connection] = dropped
        try:
            yield dropped
        finally:
            with self.lock:
                self.streams.pop(connection, None)

    def drop(self) -> None:
        with self.lock:
            for connection, dropped in self.streams.items():
                dropped.set()
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def emit(data: dict) -> None:
    kind, run = data.get("kind"), data.get("run")
    if not isinstance(kind, str) or not EVENT_KIND.fullmatch(kind) or not isinstance(run, str) or not run:
        raise Stated(400, "Send an event kind and a scratch run ID.")
    with db.tx() as conn:
        row = writes.current(conn, "runs", run)
        if row is None:
            raise Stated(404, "No such run.")
        writes.emit("runs", run, row, kind)
