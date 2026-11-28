from __future__ import annotations

import os
import re
import secrets
import signal
import threading
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime, timedelta

from . import credential_files, events, paths, record, util
from .store import db as foundation
from .store import devices as device_store
from .store import tokens as token_store

TTL_SECONDS = 120
RENEW_SECONDS: float = 20.0
LAPSE_GRACE_MINUTES = 10

LeaseFenced = foundation.LeaseFenced

_LEASE_ID_RE = re.compile(r"^(?:L-)?([1-9][0-9]{0,17})$")
_ACTIVE_LEASE: ContextVar[ActiveLease | None] = ContextVar("active_device_lease", default=None)
_BEFORE_LAPSE: Callable[[dict], None] | None = None


def current() -> ActiveLease | None:
    return _ACTIVE_LEASE.get()


def parse_id(text: str) -> int | None:
    if not isinstance(text, str):
        return None
    m = _LEASE_ID_RE.fullmatch(text.strip())
    return int(m.group(1)) if m else None


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
        "streams": [],
    }


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
        lease_id = device_store.insert_lease(
            c,
            run_id=run_id,
            device=device_name,
            device_version=device_version,
            created=created,
            expires=expires,
        )
        token_text = f"ar_{tag}_L{lease_id}_{secrets.token_urlsafe(32)}"
        token_hash = token_store.digest(key, token_text)
        device_store.set_lease_token(c, lease_id, token_hash, created)
        if run_id:
            c.execute("UPDATE runs SET lease_id = ? WHERE id = ?", (lease_id, run_id))
        row = dict(c.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone())
        emit_changed(c, row)
    active = ActiveLease(lease_id, 1, run_id, device_name, device_version, token_text)
    if run_id:
        foundation.register_lease_fence(lease_id, 1, run_id)
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
                from .store import runs as run_store

                run_row = c.execute("SELECT * FROM runs WHERE id = ?", (updated["run_id"],)).fetchone()
                if run_row is not None and run_row["status"] == "running":
                    record.interrupt(run_store.run_row(run_row))
            lapsed.append(int(updated["id"]))
    return lapsed
