from __future__ import annotations

import os
import re
import secrets
import signal
import threading
from collections.abc import Callable
from contextlib import nullcontext, suppress
from contextvars import ContextVar
from datetime import datetime, timedelta

from . import codebase, contract, credential_files, engine_copy, events, paths, principals, record, util
from .store import db as foundation
from .store import devices as device_store
from .store import events as event_store
from .store import runs as run_store
from .store import tokens as token_store

TTL_SECONDS = 120
RENEW_SECONDS: float = 20.0
HEARTBEAT_HOLD_SECONDS: float = 20.0
LAPSE_GRACE_MINUTES = 10
CLAIM_TOKEN_USES = 3
CLAIM_TOKEN_MINUTES = 10
LINE_SHAPE = "v1"
SUPPORTED_LINE_SHAPES = frozenset(("v1", "1"))

LeaseFenced = foundation.LeaseFenced

_LEASE_ID_RE = re.compile(r"^(?:L-)?([1-9][0-9]{0,17})$")
_ACTIVE_LEASE: ContextVar[ActiveLease | None] = ContextVar("active_device_lease", default=None)
_BEFORE_LAPSE: Callable[[dict], None] | None = None
_WAKE_LOCK = threading.Lock()
_WAKE_EVENTS: dict[int, threading.Event] = {}


def current() -> ActiveLease | None:
    return _ACTIVE_LEASE.get()


def parse_id(text: str) -> int | None:
    if not isinstance(text, str):
        return None
    m = _LEASE_ID_RE.fullmatch(text.strip())
    return int(m.group(1)) if m else None


def _wake_event(lease_id: int) -> threading.Event:
    with _WAKE_LOCK:
        ev = _WAKE_EVENTS.get(lease_id)
        if ev is None:
            ev = threading.Event()
            _WAKE_EVENTS[lease_id] = ev
        return ev


def wake(lease_id: int) -> None:
    with _WAKE_LOCK:
        ev = _WAKE_EVENTS.get(lease_id)
        if ev is not None:
            ev.set()


def wake_all() -> None:
    with _WAKE_LOCK:
        for ev in _WAKE_EVENTS.values():
            ev.set()


def wait_for_control(lease_id: int, timeout: float) -> bool:
    if timeout <= 0:
        return False
    ev = _wake_event(lease_id)
    fired = ev.wait(timeout=min(HEARTBEAT_HOLD_SECONDS, max(0.0, float(timeout))))
    if fired:
        ev.clear()
    return fired


def worker_exited(pid: int | None) -> bool:
    if not pid:
        return False
    p = int(pid)
    if p <= 0 or not util.pid_alive(p):
        return True
    try:
        info = os.waitid(os.P_PID, p, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        if info is not None and info.si_code in (os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED):
            return True
    except (ChildProcessError, OSError):
        pass
    return False


def emit_changed(c, row: dict) -> int:
    return events.emit(
        "lease.changed",
        f"lease:{row['id']}",
        cls="durable",
        ver=int(row["version"]),
        data={
            "id": f"L-{row['id']}",
            "lease_id": int(row["id"]),
            "device": row["device"],
            "run_id": row["run_id"],
            "epoch": int(row["epoch"]),
            "released": row["released"],
            "outcome": row["outcome"],
            "vm_name": row["vm_name"],
            "vmid": row["vmid"],
            "address": row["address"],
            "version": int(row["version"]),
            "engine_sha": row.get("engine_sha") or "",
            "schema_version": int(row["schema_version"]) if row.get("schema_version") is not None else None,
            "line_shape": row.get("line_shape") or "",
        },
        connection=c,
    )


def view(row: dict) -> dict:
    return {
        "id": f"L-{row['id']}",
        "subject": f"lease:{row['id']}",
        "run_id": row["run_id"],
        "device": row["device"],
        "device_version": row["device_version"],
        "epoch": int(row["epoch"]),
        "created": row["created"],
        "renewed": row["renewed"],
        "expires": row["expires"],
        "released": row["released"],
        "outcome": row["outcome"],
        "kept_reason": row["kept_reason"],
        "vm_name": row["vm_name"],
        "vmid": row["vmid"],
        "ready": row["ready"],
        "address": row["address"],
        "host_key_fingerprint": row["host_key_fingerprint"],
        "sweep_attempts": int(row["sweep_attempts"]),
        "last_sweep": row["last_sweep"],
        "last_sweep_error": row["last_sweep_error"],
        "version": int(row["version"]),
        "engine_sha": row.get("engine_sha") or "",
        "schema_version": int(row["schema_version"]) if row.get("schema_version") is not None else None,
        "line_shape": row.get("line_shape") or "",
        "streams": [],
    }


def is_live(run_row: dict, conn=None) -> bool:
    if not run_row or run_row.get("status") != "running":
        return False
    now_iso = util.iso()
    lease_id = run_row.get("lease_id")
    connect = (lambda: nullcontext(conn)) if conn is not None else foundation.reader
    lease_row = None
    if lease_id is not None:
        lease_row = device_store.lease_get(int(lease_id), connect=connect)
    elif run_row.get("id"):
        lease_row = device_store.latest_lease_for_run(str(run_row["id"]), connect=connect)
    if lease_row is not None:
        if lease_row["released"] is not None:
            return False
        if lease_row["expires"] < now_iso:
            return False
        device_name = str(lease_row.get("device") or run_row.get("device") or "local")
        pid = run_row.get("pid")
        if device_name in ("", "local"):
            if pid is not None:
                return int(pid) > 0 and not worker_exited(pid)
            if not lease_row.get("token_hash"):
                return bool(run_row.get("lock") and util.lock_is_held(run_row["lock"]))
        return True
    # Legacy row with no lease: fall back to process/lock liveness.
    pid = run_row.get("pid")
    if pid and worker_exited(pid):
        return False
    if lock := run_row.get("lock"):
        return util.lock_is_held(lock)
    return bool(pid and util.pid_alive(pid))


def owns_lock(run_id: str, lock: str, conn=None) -> bool:
    if not run_id or not lock:
        return False
    connect = (lambda: nullcontext(conn)) if conn is not None else foundation.reader
    with connect() as c:
        row = c.execute(
            "SELECT * FROM runs WHERE id = ? AND lock = ? AND status IN ('running', 'queued')",
            (run_id, lock),
        ).fetchone()
        if row is None:
            return False
        row_dict = dict(row)
        if row_dict.get("lease_id") is None:
            return False
        lease_row = device_store.lease_get(int(row_dict["lease_id"]), connect=lambda: nullcontext(c))
        if lease_row is None:
            return False
        return lease_row["released"] is None and lease_row["expires"] >= util.iso()


def ensure_run_lease(
    c,
    run_id: str,
    *,
    device: str = "local",
    device_version: str = "",
    engine_sha: str = "",
    schema_version: int | None = None,
    line_shape: str = "",
) -> int:
    now = util.now()
    created = util.iso(now)
    expires = util.iso(now + timedelta(seconds=TTL_SECONDS))
    lease_id = device_store.insert_lease(
        c,
        run_id=run_id,
        device=device or "local",
        device_version=device_version or "",
        created=created,
        expires=expires,
        engine_sha=engine_sha or codebase.STAMP,
        schema_version=foundation.LATEST if schema_version is None else schema_version,
        line_shape=line_shape or "",
    )
    row = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone())
    emit_changed(c, row)
    return lease_id


def release_run_lease_in_tx(
    c,
    run_id: str,
    lease_id: int | None = None,
    *,
    outcome: str = "released",
    now_iso: str | None = None,
) -> dict | None:
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'leases'").fetchone():
        return None
    now = now_iso or util.iso()
    if lease_id is not None:
        row = c.execute(
            "SELECT * FROM leases WHERE id = ? AND released IS NULL AND vm_name = ''",
            (int(lease_id),),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT * FROM leases WHERE run_id = ? AND released IS NULL AND vm_name = '' ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    lid = int(row["id"])
    final_outcome = outcome if outcome in ("released", "expired", "revoked", "deleted", "") else "released"
    c.execute(
        "UPDATE leases SET epoch = epoch + 1, released = ?, outcome = ?, token_hash = '', "
        "version = version + 1 WHERE id = ? AND released IS NULL",
        (now, final_outcome, lid),
    )
    device_store.kill_lease_tokens(c, lid, now)
    updated = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lid,)).fetchone())
    emit_changed(c, updated)
    if foundation.in_write():
        foundation.after_commit(lambda lid=lid: wake(lid))
        foundation.after_commit(engine_copy.prune_unused)
    return updated


def settle_dead_on_lock(c, lock: str, *, now_iso: str | None = None) -> list[dict]:
    from . import control
    from .store import insights as insights_store

    if not lock:
        return []
    now = now_iso or util.iso()
    seq = run_store.seq_col(c)
    rows = [
        dict(r)
        for r in c.execute(
            f"SELECT * FROM runs WHERE lock = ? AND status = 'running' ORDER BY started ASC, {seq} ASC, id ASC",  # noqa: S608
            (lock,),
        ).fetchall()
    ]
    settled: list[dict] = []
    for row in rows:
        if is_live(row, conn=c):
            raise control.Busy(f"run {row['id']}", run_id=row["id"])
        lease_id = row.get("lease_id")
        if lease_id is not None:
            lease_row = c.execute("SELECT * FROM leases WHERE id = ? AND released IS NULL", (int(lease_id),)).fetchone()
            if lease_row is not None:
                lid = int(lease_row["id"])
                c.execute(
                    "UPDATE leases SET epoch = epoch + 1, released = ?, outcome = 'expired', "
                    "token_hash = '', version = version + 1 WHERE id = ?",
                    (now, lid),
                )
                device_store.kill_lease_tokens(c, lid, now)
                updated_lease = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lid,)).fetchone())
                emit_changed(c, updated_lease)
                if foundation.in_write():
                    foundation.after_commit(lambda lid=lid: wake(lid))
                    foundation.after_commit(engine_copy.prune_unused)
        status = record.dead_status_in_tx(c, row)
        summary = record.STOPPED if status == "stopped" else record.INTERRUPTED
        done = run_store.settle(
            row["id"],
            {"status": status, "finished": now, "summary": summary},
            end_line=f"{status}: {summary}",
        )
        if done is not None:
            if row["kind"] in record.TICKET_KINDS and (row["kind"] != "pipeline" or status == "failed"):
                insights_store.release_interrupted_feedback(row["id"])
            settled.append(done)
    return settled


def issue_claim_token(c, run_id: str, lease_id: int) -> str:
    token_store.ensure_key(c)
    expires = util.iso(util.now() + timedelta(minutes=CLAIM_TOKEN_MINUTES))
    _, text = token_store.insert(
        c,
        principals.owner(),
        f"claim:{run_id}",
        ["*"],
        kind="claim",
        expires=expires,
        lease_id=lease_id,
        run_id=run_id,
        uses_left=CLAIM_TOKEN_USES,
    )
    return text


def _check_lease_fence_in_tx(c, lease_id: int, expected_epoch: int, run_id: str | None = None) -> dict:
    row = c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
    if row is None or int(row["epoch"]) != int(expected_epoch) or row["released"] is not None:
        actual_epoch = int(row["epoch"]) if row is not None else int(expected_epoch)
        raise LeaseFenced("lease_fenced", lease_id=lease_id, epoch=actual_epoch, run_id=run_id or "")
    if row["expires"] < util.iso():
        raise LeaseFenced("lease_expired", lease_id=lease_id, epoch=int(row["epoch"]), run_id=run_id or "")
    return dict(row)


def http_claim(
    credential: token_store.Credential,
    *,
    engine_sha: str,
    line_shape: str,
    schema_version: int | None = None,
    pid: int | None = None,
    device: str = "",
    device_version: str = "",
    node: str = "worker",
) -> dict:
    if line_shape not in SUPPORTED_LINE_SHAPES:
        raise contract.Problem("invalid_input", f"Unsupported line shape {line_shape!r}.")
    if credential.kind != "claim" or not credential.run_id:
        raise contract.Problem("unauthenticated", "A claim token is required.")
    run_id = credential.run_id
    actor = f"run:{run_id}/{node or 'worker'}"
    now = util.now()
    now_iso = util.iso(now)
    expires = util.iso(now + timedelta(seconds=TTL_SECONDS))
    with principals.acting(actor), foundation.tx(fenced=False) as c:
        if not token_store.consume_claim_use(c, credential.id, now_iso):
            raise contract.Problem("unauthenticated", "This claim token is expired or exhausted.")
        run_row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run_row is None:
            raise contract.Problem("not_found", f"No run {run_id}.")
        lease_id = credential.lease_id or run_row["lease_id"]
        if lease_id is not None:
            existing = c.execute("SELECT * FROM leases WHERE id = ?", (int(lease_id),)).fetchone()
            if existing is None:
                lease_id = None
            elif existing["released"] is not None:
                raise LeaseFenced("lease_fenced", lease_id=int(existing["id"]), epoch=int(existing["epoch"]), run_id=run_id)
            elif existing["expires"] < now_iso:
                raise LeaseFenced("lease_expired", lease_id=int(existing["id"]), epoch=int(existing["epoch"]), run_id=run_id)
            else:
                lease_id = int(existing["id"])
        if lease_id is None:
            lease_id = device_store.insert_lease(
                c,
                run_id=run_id,
                device=device or run_row["device"] or "local",
                device_version=device_version or "",
                created=now_iso,
                expires=expires,
                engine_sha=engine_sha,
                schema_version=schema_version if schema_version is not None else run_row["schema_version"],
                line_shape=line_shape,
            )
        key_ref = token_store.ensure_key(c)
        key = credential_files.read_key(key_ref)
        tag = foundation.metadata(c)["instance_tag"]
        token_text = f"ar_{tag}_L{lease_id}_{secrets.token_urlsafe(32)}"
        token_hash = token_store.digest(key, token_text)
        device_store.kill_lease_tokens(c, lease_id, now_iso)
        device_store.set_lease_token(c, lease_id, token_hash, now_iso)
        update_fields: dict[str, object] = {
            "renewed": now_iso,
            "expires": expires,
            "engine_sha": engine_sha,
            "line_shape": line_shape,
        }
        if schema_version is not None:
            update_fields["schema_version"] = int(schema_version)
        if device_version:
            update_fields["device_version"] = device_version
        updated = device_store.update_lease_fields(c, lease_id, update_fields)
        run_updates: dict[str, object] = {"lease_id": lease_id}
        if pid is not None and pid > 0:
            run_updates["pid"] = int(pid)
        if engine_sha and not run_row["engine_sha"]:
            run_updates["engine_sha"] = engine_sha
        if schema_version is not None and run_row["schema_version"] is None:
            run_updates["schema_version"] = int(schema_version)
        from .store import writes

        writes.update("runs", run_id, run_updates)
        with event_store.request_scope(f"lease:{lease_id}"):
            emit_changed(c, updated)
        seq = event_store.metadata(c)["head"]
    return {
        "seq": seq,
        "lease": view(updated),
        "token": token_text,
        "header": f"L-{lease_id}.{int(updated['epoch'])}",
        "ttl_seconds": TTL_SECONDS,
        "heartbeat_seconds": int(RENEW_SECONDS),
    }


def http_heartbeat(
    lease_id: int,
    epoch: int,
    run_id: str | None,
    *,
    hold_seconds: float = 0.0,
    node: str = "worker",
) -> dict:
    actor = f"run:{run_id}/{node or 'worker'}" if run_id else f"lease:{lease_id}"
    now = util.now()
    now_iso = util.iso(now)
    new_expires = util.iso(now + timedelta(seconds=TTL_SECONDS))
    with principals.acting(actor), event_store.request_scope(f"lease:{lease_id}"), foundation.tx(fenced=False) as c:
        _check_lease_fence_in_tx(c, lease_id, epoch, run_id)
        if not device_store.renew_lease(c, lease_id, epoch, now_iso, new_expires):
            _check_lease_fence_in_tx(c, lease_id, epoch, run_id)
        updated = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone())
        effective_run_id = updated["run_id"] or run_id
        controls = device_store.pending_run_controls(c, effective_run_id, lease_id) if effective_run_id else []
        stop_asked = False
        if effective_run_id:
            rrow = c.execute("SELECT stop_asked FROM runs WHERE id = ?", (effective_run_id,)).fetchone()
            stop_asked = bool(rrow and rrow["stop_asked"])
        seq = event_store.metadata(c)["head"]

    hold = min(HEARTBEAT_HOLD_SECONDS, max(0.0, float(hold_seconds or 0.0)))
    if hold > 0 and not controls and not stop_asked:
        wait_for_control(lease_id, hold)
        with principals.acting(actor), event_store.request_scope(f"lease:{lease_id}"), foundation.tx(fenced=False) as c:
            _check_lease_fence_in_tx(c, lease_id, epoch, effective_run_id)
            updated = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone())
            controls = device_store.pending_run_controls(c, effective_run_id, lease_id) if effective_run_id else []
            if effective_run_id:
                rrow = c.execute("SELECT stop_asked FROM runs WHERE id = ?", (effective_run_id,)).fetchone()
                stop_asked = bool(rrow and rrow["stop_asked"])
            seq = event_store.metadata(c)["head"]

    if controls:
        with principals.acting(actor), event_store.request_scope(f"lease:{lease_id}"), foundation.tx(fenced=False) as c:
            _check_lease_fence_in_tx(c, lease_id, epoch, effective_run_id)
            device_store.ack_run_controls(c, [int(ctrl["id"]) for ctrl in controls], util.iso())
            seq = event_store.metadata(c)["head"]

    return {
        "seq": seq,
        "lease": view(updated),
        "controls": [
            {"id": int(ctrl["id"]), "kind": ctrl["kind"], "payload": ctrl["payload"], "created": ctrl["created"]}
            for ctrl in controls
        ],
        "stop_asked": stop_asked,
    }


def http_release(
    lease_id: int,
    epoch: int,
    run_id: str | None,
    *,
    outcome: str = "released",
    node: str = "worker",
) -> dict:
    actor = f"run:{run_id}/{node or 'worker'}" if run_id else f"lease:{lease_id}"
    final_outcome = outcome if outcome in ("released", "deleted", "kept", "") else "released"
    now_iso = util.iso()
    with principals.acting(actor), event_store.request_scope(f"lease:{lease_id}"), foundation.tx(fenced=False) as c:
        _check_lease_fence_in_tx(c, lease_id, epoch, run_id)
        c.execute(
            "UPDATE leases SET epoch = epoch + 1, released = ?, outcome = ?, token_hash = '', "
            "version = version + 1 WHERE id = ? AND epoch = ? AND released IS NULL",
            (now_iso, final_outcome, lease_id, epoch),
        )
        device_store.kill_lease_tokens(c, lease_id, now_iso)
        updated = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone())
        emit_changed(c, updated)
        seq = event_store.metadata(c)["head"]
    wake(lease_id)
    engine_copy.prune_unused()
    return {"seq": seq, "lease": view(updated)}


def http_revoke(lease_id: int, *, reason: str = "") -> dict:
    now_iso = util.iso()
    with foundation.tx(fenced=False) as c:
        row = c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        if row is None:
            raise contract.Problem("not_found", f"No lease L-{lease_id}.")
        if row["released"] is None:
            c.execute(
                "UPDATE leases SET epoch = epoch + 1, released = ?, outcome = 'revoked', "
                "kept_reason = CASE WHEN ? != '' THEN ? ELSE kept_reason END, "
                "token_hash = '', version = version + 1 WHERE id = ?",
                (now_iso, reason, reason, lease_id),
            )
            device_store.kill_lease_tokens(c, lease_id, now_iso)
            updated = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone())
            emit_changed(c, updated)
        else:
            updated = dict(row)
        seq = event_store.metadata(c)["head"]
    wake(lease_id)
    engine_copy.prune_unused()
    return {"seq": seq, "lease": view(updated)}


class ActiveLease:
    def __init__(
        self,
        lease_id: int,
        epoch: int,
        run_id: str | None,
        device: str,
        device_version: str,
        token: str,
    ) -> None:
        self.id = lease_id
        self.epoch = epoch
        self.run_id = run_id
        self.device = device
        self.device_version = device_version
        self.token = token
        self.lost = False
        self.closed = False
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def label(self) -> str:
        return f"L-{self.id}"

    @property
    def header_value(self) -> str:
        return f"L-{self.id}.{self.epoch}"

    def start_renewer(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._renew_loop,
            name=f"arete-lease-renew-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def pause_renewer(self) -> None:
        self._paused.set()

    def resume_renewer(self) -> None:
        self._paused.clear()

    def stop_renewer(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _renew_loop(self) -> None:
        while not self._stop.wait(timeout=max(0.01, float(RENEW_SECONDS))):
            if self._paused.is_set() or self.closed:
                continue
            try:
                ok = self.renew_once()
            except BaseException:
                ok = False
            if not ok and not self._stop.is_set() and not self.closed:
                self.lost = True
                with suppress(OSError):
                    os.kill(os.getpid(), signal.SIGTERM)
                return

    def renew_once(self) -> bool:
        now = util.now()
        now_iso = util.iso(now)
        new_expires = util.iso(now + timedelta(seconds=TTL_SECONDS))
        with foundation.tx(fenced=False) as c:
            return device_store.renew_lease(c, self.id, self.epoch, now_iso, new_expires)

    def update(self, **fields: object) -> dict | None:
        with foundation.tx() as c:
            row = device_store.update_lease_fields(c, self.id, fields)
            if row is not None:
                emit_changed(c, row)
            return row

    def close(
        self,
        outcome: str = "",
        *,
        kept_reason: str = "",
        sweep_attempts: int | None = None,
        failing_since: str | None = None,
        last_sweep: str | None = None,
        last_sweep_error: str | None = None,
        fenced: bool = True,
    ) -> dict | None:
        if self.closed:
            return None
        self.stop_renewer()
        now_iso = util.iso()
        fields: dict[str, object] = {
            "released": now_iso,
            "outcome": outcome,
            "token_hash": "",
        }
        if kept_reason:
            fields["kept_reason"] = kept_reason
        if sweep_attempts is not None:
            fields["sweep_attempts"] = int(sweep_attempts)
        if failing_since is not None:
            fields["failing_since"] = failing_since
        if last_sweep is not None:
            fields["last_sweep"] = last_sweep
        if last_sweep_error is not None:
            fields["last_sweep_error"] = last_sweep_error
        try:
            with foundation.tx(fenced=fenced) as c:
                row = device_store.update_lease_fields(c, self.id, fields)
                device_store.kill_lease_tokens(c, self.id, now_iso)
                if row is not None:
                    emit_changed(c, row)
            self.closed = True
            wake(self.id)
            engine_copy.prune_unused()
            return row
        finally:
            if self.run_id:
                foundation.clear_lease_fence(self.id)

    def abort_fenced(self) -> None:
        self.closed = True
        self.stop_renewer()
        if self.run_id:
            foundation.clear_lease_fence(self.id)


def claim(
    device_name: str,
    device_version: str,
    run_id: str | None = None,
    *,
    start_renewer: bool = True,
) -> ActiveLease:
    now = util.now()
    created = util.iso(now)
    expires = util.iso(now + timedelta(seconds=TTL_SECONDS))
    with foundation.tx(fenced=False) as c:
        key_ref = token_store.ensure_key(c)
        key = credential_files.read_key(key_ref)
        tag = foundation.metadata(c)["instance_tag"]
        adopted = (
            device_store.unclaimed_lease_for_run(c, run_id, device_name)
            if run_id
            else None
        )
        if adopted is not None:
            lease_id = int(adopted["id"])
            epoch = int(adopted["epoch"])
            device_store.update_lease_fields(
                c,
                lease_id,
                {
                    "device_version": device_version,
                    "renewed": created,
                    "expires": expires,
                    "engine_sha": adopted.get("engine_sha") or codebase.STAMP,
                    "schema_version": adopted.get("schema_version") or foundation.LATEST,
                },
            )
        else:
            lease_id = device_store.insert_lease(
                c,
                run_id=run_id,
                device=device_name,
                device_version=device_version,
                created=created,
                expires=expires,
                engine_sha=codebase.STAMP,
                schema_version=foundation.LATEST,
            )
            epoch = 1
        token_text = f"ar_{tag}_L{lease_id}_{secrets.token_urlsafe(32)}"
        token_hash = token_store.digest(key, token_text)
        device_store.set_lease_token(c, lease_id, token_hash, created)
        if run_id:
            c.execute("UPDATE runs SET lease_id = ? WHERE id = ?", (lease_id, run_id))
        row = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone())
        emit_changed(c, row)
    active = ActiveLease(lease_id, epoch, run_id, device_name, device_version, token_text)
    if run_id:
        foundation.register_lease_fence(lease_id, epoch, run_id)
    if start_renewer:
        active.start_renewer()
    return active


def _prev_tick_time(prev_tick: str | datetime | None) -> datetime | None:
    if isinstance(prev_tick, datetime):
        return prev_tick
    if isinstance(prev_tick, str) and prev_tick:
        try:
            return util.parse_iso(prev_tick)
        except (ValueError, TypeError):
            return None
    from . import control

    beat = control.serve_beat()
    if at := beat.get("at"):
        try:
            return util.parse_iso(at)
        except (ValueError, TypeError):
            return None
    return None


def lapse(
    prev_tick: str | datetime | None = None,
    *,
    before_lapse: Callable[[dict], None] | None = None,
) -> list[int]:
    if not paths.DB_FILE.is_file():
        return []
    now = util.now()
    previous = _prev_tick_time(prev_tick)
    if previous is not None and (now - previous).total_seconds() > TTL_SECONDS:
        new_expires = util.iso(now + timedelta(seconds=TTL_SECONDS))
        with foundation.tx(fenced=False) as c:
            device_store.extend_all_open_leases(c, new_expires)
        return []

    now_iso = util.iso(now)
    candidates = device_store.expired_open_leases(now_iso)
    hook = before_lapse if before_lapse is not None else _BEFORE_LAPSE
    lapsed: list[int] = []
    for candidate in candidates:
        if hook is not None:
            hook(candidate)
        with foundation.tx(fenced=False) as c:
            row = c.execute(
                "SELECT * FROM leases WHERE id = ? AND released IS NULL",
                (candidate["id"],),
            ).fetchone()
            recheck_now = util.iso()
            if row is None or row["expires"] >= recheck_now:
                continue
            c.execute(
                "UPDATE leases SET epoch = epoch + 1, released = ?, outcome = 'expired', "
                "token_hash = '', version = version + 1 WHERE id = ?",
                (recheck_now, row["id"]),
            )
            device_store.kill_lease_tokens(c, int(row["id"]), recheck_now)
            updated = dict(c.execute("SELECT * FROM leases WHERE id = ?", (row["id"],)).fetchone())
            emit_changed(c, updated)
            if updated["run_id"]:
                run_row = c.execute("SELECT * FROM runs WHERE id = ?", (updated["run_id"],)).fetchone()
                if run_row is not None and run_row["status"] == "running":
                    record.interrupt(dict(run_row))
            lapsed.append(int(updated["id"]))
    for lid in lapsed:
        wake(lid)
    if lapsed:
        engine_copy.prune_unused()
    return lapsed
