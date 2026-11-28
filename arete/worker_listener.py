from __future__ import annotations

import atexit
import contextlib
import os
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from . import api, api_http, contract, leases, paths, principals, util
from .store import db as foundation
from .store import events as event_store
from .store import tokens as token_store

WORKER_HOST = "127.0.0.1"
WORKER_OPERATIONS = frozenset(("lease.claim", "lease.heartbeat", "lease.release", "lease.get", "run.get"))
_STATE_LOCK = threading.Lock()
_ACTIVE_SERVER: WorkerServer | None = None
_ACTIVE_URL: str = ""


class WorkerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = WORKER_HOST
        self.server_port = int(self.server_address[1])


def current_url() -> str:
    with _STATE_LOCK:
        if _ACTIVE_URL:
            return _ACTIVE_URL
    data = util.read_json(paths.WORKER_SERVER_FILE, None)
    if not isinstance(data, dict):
        return ""
    pid = data.get("pid")
    url = str(data.get("url") or "").strip()
    if pid and util.pid_alive(pid) and url.startswith("http://127.0.0.1:"):
        if {k: data.get(k) for k in ("state", "config")} == paths.state_and_config():
            return url
    return ""


def _parse_bearer(headers) -> str:
    auth = headers.get_all("Authorization", [])
    if len(auth) != 1:
        raise token_store.InvalidCredential()
    parts = auth[0].split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise token_store.InvalidCredential()
    return parts[1]


def _parse_lease_epoch(header_val: str | None) -> tuple[int, int]:
    if not isinstance(header_val, str):
        raise token_store.InvalidCredential()
    m = token_store.LEASE_HEADER.fullmatch(header_val.strip())
    if not m:
        raise token_store.InvalidCredential()
    return int(m.group(1)), int(m.group(2))


class WorkerHandler(BaseHTTPRequestHandler):
    server_version = "arete-worker"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, body: dict, *, media: str = "application/json", seq: str | None = None) -> None:
        raw = api_http.json_bytes(body)
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Arete-API", contract.VERSION)
        if seq:
            self.send_header("Arete-Seq", seq)
        self.end_headers()
        self.wfile.write(raw)

    def _send_problem(self, status: int, code: str, message: str, **details: object) -> None:
        payload = api_http.problem(status, {"code": code, "error": message, **details})
        self._send_json(status, payload, media="application/problem+json")

    def _read_json_body(self) -> dict:
        size = api_http.content_length(self.headers, required=False)
        api_http.check_media(self.headers, ("application/json",), size)
        raw = api_http.read_body(self.rfile, size, api_http.MAX_JSON_BYTES) if size else b"{}"
        return api_http.json_object(raw)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        started = time.monotonic()
        path = urlsplit(self.path).path
        matched = contract.match(method, path)
        if matched is None:
            self._send_problem(404, "not_found", f"No route {method} {path}.")
            return
        op, path_params = matched
        name = op["name"]
        if name not in WORKER_OPERATIONS:
            self._send_problem(403, "forbidden", f"Operation {name} is not served on the worker listener.")
            return
        if self.headers.get_all("Cookie", []):
            self._send_problem(401, "unauthenticated", "Worker routes require bearer authentication only.")
            return
        try:
            bearer = _parse_bearer(self.headers)
            lease_hdr = self.headers.get("X-Arete-Lease")
            if name == "lease.claim":
                cred = token_store.resolve(bearer, kinds=("claim",))
                body = self._read_json_body()
                values = {**body, **{k: unquote(v) for k, v in path_params.items()}}
                contract.validate(values, op["input"])
                result = leases.http_claim(
                    cred,
                    engine_sha=str(values["engine_sha"]),
                    line_shape=str(values["line_shape"]),
                    schema_version=values.get("schema_version"),
                    pid=values.get("pid"),
                    device=str(values.get("device") or ""),
                    device_version=str(values.get("device_version") or ""),
                    node=str(values.get("node") or "worker"),
                )
                self._send_json(op["success_status"], result, seq=result.get("seq"))
                return

            cred = token_store.resolve(bearer, kinds=("lease",), lease_header=lease_hdr)
            hdr_lease, hdr_epoch = _parse_lease_epoch(lease_hdr)
            if cred.lease_id != hdr_lease:
                raise token_store.InvalidCredential()

            if name == "lease.get":
                target = unquote(path_params.get("lease", ""))
                if target not in (f"L-{cred.lease_id}", str(cred.lease_id)):
                    self._send_problem(403, "forbidden", "Lease token may access only its own lease.")
                    return
                with principals.acting(cred.actor), event_store.request_scope(f"lease:{cred.lease_id}"):
                    result = api.execute("lease.get", {"lease": target})
                self._send_json(200, result, seq=result.get("seq"))
                return

            if name == "run.get":
                target = unquote(path_params.get("id", ""))
                if not cred.run_id or target != cred.run_id:
                    self._send_problem(403, "forbidden", "Lease token may access only its own run.")
                    return
                with principals.acting(cred.actor), event_store.request_scope(f"lease:{cred.lease_id}"):
                    result = api.execute("run.get", {"id": target})
                self._send_json(200, result, seq=result.get("seq"))
                return

            body = self._read_json_body()
            values = {**body, **{k: unquote(v) for k, v in path_params.items()}}
            contract.validate(values, op["input"])
            target = values["lease"]
            if target not in (f"L-{cred.lease_id}", str(cred.lease_id)):
                self._send_problem(403, "forbidden", "Lease token may act only on its own lease.")
                return

            if name == "lease.heartbeat":
                wait_pref = api.prefer_wait(self, default=0.0, maximum=leases.HEARTBEAT_HOLD_SECONDS)
                hold_seconds = values.get("hold_seconds")
                effective_hold = float(hold_seconds) if hold_seconds is not None else wait_pref
                result = leases.http_heartbeat(
                    cred.lease_id,
                    hdr_epoch,
                    cred.run_id,
                    hold_seconds=effective_hold,
                    node=str(values.get("node") or "worker"),
                )
                self._send_json(200, result, seq=result.get("seq"))
                return

            if name == "lease.release":
                result = leases.http_release(
                    cred.lease_id,
                    hdr_epoch,
                    cred.run_id,
                    outcome=str(values.get("outcome") or "released"),
                    node=str(values.get("node") or "worker"),
                )
                self._send_json(200, result, seq=result.get("seq"))
                return

            self._send_problem(404, "not_found", f"No handler for {name}.")
        except token_store.InvalidCredential as error:
            status = 410 if error.code in ("lease_fenced", "lease_expired") else 401
            desc = next((e["description"] for e in contract.ERRORS if e["code"] == error.code), "Unauthenticated.")
            self._send_problem(status, error.code, desc, **error.details)
        except foundation.LeaseFenced as error:
            desc = next((e["description"] for e in contract.ERRORS if e["code"] == error.code), "Lease fenced.")
            self._send_problem(410, error.code, desc, epoch=error.epoch)
        except contract.Problem as error:
            self._send_problem(error.status, error.code, str(error), **error.details)
        except Exception as error:
            api_http.log_request(method, path, 500, "worker", False, started)
            self._send_problem(500, "internal_error", f"Worker request failed: {type(error).__name__}.")


def _record_file(port: int) -> None:
    payload = {
        "pid": os.getpid(),
        "host": WORKER_HOST,
        "port": port,
        "url": f"http://{WORKER_HOST}:{port}",
        **paths.state_and_config(),
        "at": util.iso(),
    }
    with contextlib.suppress(OSError):
        util.write_json(paths.WORKER_SERVER_FILE, payload)


def _forget_file() -> None:
    found = util.read_json(paths.WORKER_SERVER_FILE, None)
    if isinstance(found, dict) and found.get("pid") == os.getpid():
        paths.WORKER_SERVER_FILE.unlink(missing_ok=True)


def start(port: int | None = None, *, poll_interval: float = 0.05, record_file: bool = True) -> tuple[WorkerServer, str]:
    global _ACTIVE_SERVER, _ACTIVE_URL
    if port is None:
        env_port = os.environ.get("ARETE_WORKER_PORT", "").strip()
        if env_port:
            chosen_port = int(env_port)
        elif paths.ISOLATED:
            chosen_port = 0
        else:
            chosen_port = paths.WORKER_PORT
    else:
        chosen_port = int(port)
    server = WorkerServer((WORKER_HOST, chosen_port), WorkerHandler)
    actual_port = int(server.server_address[1])
    url = f"http://{WORKER_HOST}:{actual_port}"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": poll_interval},
        name=f"worker-listener-{actual_port}",
        daemon=True,
    )
    thread.start()
    with _STATE_LOCK:
        _ACTIVE_SERVER = server
        _ACTIVE_URL = url
    if record_file:
        _record_file(actual_port)
        atexit.register(_forget_file)
    return server, url


def stop(server: WorkerServer | None = None) -> None:
    global _ACTIVE_SERVER, _ACTIVE_URL
    leases.wake_all()
    with _STATE_LOCK:
        target = server or _ACTIVE_SERVER
        if target is _ACTIVE_SERVER:
            _ACTIVE_SERVER = None
            _ACTIVE_URL = ""
    if target is not None:
        with contextlib.suppress(OSError):
            target.shutdown()
        with contextlib.suppress(OSError):
            target.server_close()
    _forget_file()


def wait_ready(url: str, timeout: float = 2.0) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or WORKER_HOST
    port = parsed.port or paths.WORKER_PORT
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.01)
    return False
