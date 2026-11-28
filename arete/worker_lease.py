from __future__ import annotations

import http.client
import json
import os
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from urllib.parse import urlsplit

from . import codebase, control, leases
from .store import db as foundation

CLAIM_STDIN_ENV = "ARETE_CLAIM_STDIN"
LINE_SHAPE = leases.LINE_SHAPE


class WorkerError(control.Refused):
    def __init__(self, status: int, code: str, message: str, *, epoch: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.epoch = epoch


class WorkerClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        parsed = urlsplit(base_url)
        self.base_url = base_url.rstrip("/")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = int(parsed.port or 8797)
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        bearer: str,
        lease_header: str = "",
        timeout: float | None = None,
    ) -> tuple[int, dict]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer}",
        }
        if lease_header:
            headers["X-Arete-Lease"] = lease_header
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout if timeout is None else timeout)
        with closing(conn):
            conn.request(method, path, payload, headers)
            with conn.getresponse() as resp:
                raw = resp.read()
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    data = {"error": raw.decode("utf-8", errors="replace")}
                return resp.status, data if isinstance(data, dict) else {}

    def claim(
        self,
        claim_token: str,
        *,
        engine_sha: str,
        line_shape: str = LINE_SHAPE,
        schema_version: int | None = None,
        pid: int | None = None,
        node: str = "worker",
    ) -> dict:
        body: dict[str, object] = {
            "engine_sha": engine_sha,
            "line_shape": line_shape,
            "node": node,
        }
        if schema_version is not None:
            body["schema_version"] = int(schema_version)
        if pid is not None:
            body["pid"] = int(pid)
        status, data = self.request("POST", "/api/v1/leases", body, bearer=claim_token)
        if status == 410:
            raise foundation.LeaseFenced(
                str(data.get("code") or "lease_fenced"),
                epoch=data.get("epoch"),
            )
        if status not in (200, 201):
            raise WorkerError(status, str(data.get("code") or "invalid_input"), str(data.get("error") or data))
        return data

    def heartbeat(
        self,
        lease_label: str,
        token: str,
        lease_header: str,
        *,
        hold_seconds: float = 0.0,
        node: str = "worker",
    ) -> tuple[int, dict]:
        timeout = max(5.0, float(hold_seconds) + 10.0)
        return self.request(
            "POST",
            f"/api/v1/leases/{lease_label}/heartbeat",
            {"hold_seconds": float(hold_seconds), "node": node},
            bearer=token,
            lease_header=lease_header,
            timeout=timeout,
        )

    def release(
        self,
        lease_label: str,
        token: str,
        lease_header: str,
        *,
        outcome: str = "released",
        node: str = "worker",
    ) -> tuple[int, dict]:
        return self.request(
            "POST",
            f"/api/v1/leases/{lease_label}/release",
            {"outcome": outcome, "node": node},
            bearer=token,
            lease_header=lease_header,
        )


class AdoptedLease:
    def __init__(
        self,
        client: WorkerClient,
        *,
        lease_id: int,
        epoch: int,
        run_id: str,
        token: str,
        header: str,
        heartbeat_seconds: float,
    ) -> None:
        self.client = client
        self.lease_id = lease_id
        self.epoch = epoch
        self.run_id = run_id
        self.token = token
        self.header = header
        self.heartbeat_seconds = max(0.05, min(float(leases.RENEW_SECONDS), float(heartbeat_seconds)))
        self.lost = False
        self.closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def label(self) -> str:
        return f"L-{self.lease_id}"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"worker-lease-{self.lease_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _heartbeat_loop(self) -> None:
        hold = min(leases.HEARTBEAT_HOLD_SECONDS, self.heartbeat_seconds)
        while not self._stop.is_set() and not self.closed:
            try:
                status, data = self.client.heartbeat(
                    self.label,
                    self.token,
                    self.header,
                    hold_seconds=hold,
                )
            except (OSError, http.client.HTTPException):
                if self._stop.wait(timeout=min(1.0, self.heartbeat_seconds)):
                    return
                continue
            if self._stop.is_set() or self.closed:
                return
            if status == 410:
                self.lost = True
                with suppress(OSError):
                    os.kill(os.getpid(), signal.SIGTERM)
                # If SIGTERM is caught/delayed by cleanup, force termination so a fenced worker never lingers.
                if not self._stop.wait(timeout=2.0):
                    with suppress(OSError):
                        os.kill(os.getpid(), signal.SIGTERM)
                    if not self._stop.wait(timeout=1.0):
                        os._exit(143)
                return
            if status == 200 and (
                data.get("stop_asked")
                or any(ctrl.get("kind") == "stop" for ctrl in (data.get("controls") or ()))
            ):
                with suppress(OSError):
                    os.kill(os.getpid(), signal.SIGTERM)
                return
            if self._stop.wait(timeout=min(1.0, self.heartbeat_seconds)):
                return

    def release(self, outcome: str = "released") -> None:
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        if not self.lost:
            with suppress(OSError, http.client.HTTPException):
                self.client.release(self.label, self.token, self.header, outcome=outcome)
        self.stop()
        foundation.clear_lease_fence(self.lease_id)


def _read_claim_envelope() -> dict | None:
    if os.environ.pop(CLAIM_STDIN_ENV, None) != "1":
        return None
    try:
        raw = sys.stdin.readline()
    except (OSError, ValueError):
        return None
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@contextmanager
def adopted_if_present() -> Iterator[AdoptedLease | None]:
    envelope = _read_claim_envelope()
    if envelope is None:
        yield None
        return
    claim_token = str(envelope.get("claim_token") or "").strip()
    worker_url = str(envelope.get("worker_url") or "").strip()
    run_id = str(envelope.get("run_id") or "").strip()
    if not claim_token or not worker_url:
        yield None
        return
    client = WorkerClient(worker_url)
    claimed = client.claim(
        claim_token,
        engine_sha=codebase.STAMP or codebase.load(),
        line_shape=LINE_SHAPE,
        schema_version=foundation.LATEST,
        pid=os.getpid(),
    )
    lease_doc = claimed["lease"]
    lease_id = leases.parse_id(str(lease_doc["id"]))
    if lease_id is None:
        raise control.Refused(f"invalid lease id from worker listener: {lease_doc.get('id')!r}")
    epoch = int(lease_doc["epoch"])
    resolved_run_id = str(lease_doc.get("run_id") or run_id)
    adopted = AdoptedLease(
        client,
        lease_id=lease_id,
        epoch=epoch,
        run_id=resolved_run_id,
        token=str(claimed["token"]),
        header=str(claimed["header"]),
        heartbeat_seconds=float(claimed.get("heartbeat_seconds") or leases.RENEW_SECONDS),
    )
    if resolved_run_id:
        foundation.register_lease_fence(lease_id, epoch, resolved_run_id)
    adopted.start()
    try:
        yield adopted
    finally:
        adopted.release()
