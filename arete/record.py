from __future__ import annotations

import contextlib
import fcntl
import gzip
import itertools
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from . import agent, codebase, launch_lease, paths, principals, run_lines, scrub, util
from .store import blobs as blob_store
from .store import db as foundation
from .store import insights as insights_store
from .store import loops as loop_store
from .store import runs as run_store
from .store import specgraphs

_UNSET = object()
GZIP_MAGIC = b"\x1f\x8b\x08\x00"

SETTLE_GRACE_SECONDS = 60
INTERRUPTED = "stopped before it finished (the Mac restarted, lost power or the process was killed)"
STOPPED = "stopped by request"
ID_PREFIX = {"pipeline": "pip", "miner": "min", "eval": "eva", "experiment": "exp"}
FAILS_WHEN_DEAD = ("miner", "eval")
TICKET_KINDS = ("pipeline",)
RECORD_SUFFIX = ".jsonl.gz"
TIMELINE = f"steps{RECORD_SUFFIX}"
CLOCK_SECONDS = 1
HEADER_WAIT_SECONDS = 10


def new_id(prefix: str, tag: str = "") -> str:
    stamp = util.now().strftime("%Y%m%d-%H%M%S")
    return "-".join(filter(None, [prefix.replace("/", "."), stamp, os.urandom(2).hex(), tag]))


def _resolve_started_by(actor: str) -> str:
    if not actor or actor == "hand":
        return "rule:serve"
    if actor.startswith("client:"):
        token_id = actor.removeprefix("client:")
        try:
            with foundation.reader() as c:
                row = c.execute("SELECT principal FROM tokens WHERE id = ?", (token_id,)).fetchone()
            if row and row[0]:
                return str(row[0])
        except (sqlite3.Error, foundation.NoDatabase, foundation.IncompatibleSchema):
            pass
    return actor


def _is_person_actor(actor: str | None) -> bool:
    if not actor:
        return False
    if actor.startswith("u:"):
        return True
    try:
        return principals.is_person(actor)
    except (sqlite3.Error, foundation.NoDatabase, foundation.IncompatibleSchema):
        return False


def _resolve_payer(started_by: str, *, restart: str, continues: str | None,
                   explicit_payer, resolution=None, loop_payer: str = "",
                   repo_payer: str = "", repo_name: str = "") -> str | None:
    if explicit_payer is not _UNSET:
        return explicit_payer or None
    if restart in ("interrupted", "lease") and continues:
        parent = run_store.run_get(continues)
        return parent.get("payer") if parent else None
    if _is_person_actor(started_by):
        return started_by
    if loop_payer:
        return loop_payer
    if repo_payer:
        return repo_payer
    if resolution is not None:
        if resolution.repo is not None and resolution.repo.payer:
            return resolution.repo.payer
        if resolution.cfg.automation_payer:
            return resolution.cfg.automation_payer
        return None
    try:
        from . import pins

        cfg = pins.active_config()
    except Exception:
        return None
    if repo_name:
        for r in cfg.repos:
            if r.name == repo_name and r.payer:
                return r.payer
    return cfg.automation_payer or None


def start(kind: str, *, run_id: str = "", tag: str = "", lock: str = "", resolution=None,
          continues: str | None = None, restart: str = "", started_by: str | None = None,
          payer=_UNSET, loop_payer: str = "", repo_payer: str = "", **fields) -> str:
    nested = foundation.in_write()
    actor = started_by if started_by is not None else _resolve_started_by(principals.current())
    continues_val = continues or None
    restart_val = restart or ""
    payer_val = _resolve_payer(
        actor, restart=restart_val, continues=continues_val, explicit_payer=payer,
        resolution=resolution, loop_payer=loop_payer, repo_payer=repo_payer,
        repo_name=str(fields.get("repo") or ""),
    )
    chain_val: str | None = None
    if continues_val:
        parent = run_store.run_get(continues_val)
        chain_val = (parent.get("chain") or continues_val) if parent else continues_val
    row = {
        "kind": kind,
        "status": "running",
        "started": util.iso(),
        "lock": lock or None,
        "pid": os.getpid(),
        "engine_sha": resolution.engine_sha if resolution is not None else codebase.STAMP,
        "schema_version": resolution.schema_version if resolution is not None else foundation.LATEST,
        "capabilities": json.dumps(list(foundation.PROCESS_CAPABILITIES)),
        "continues": continues_val,
        "restart": restart_val,
        "started_by": actor,
        "payer": payer_val,
        **fields,
    }
    blob_rows: tuple[tuple[str, str, int], ...] = ()
    if resolution is not None:
        resolution.payer = payer_val
        row["config_commit"] = resolution.config_commit or None
        row["resolved_sha"] = resolution.resolved_sha
        if "adw" not in fields and resolution.adw is not None:
            row["adw"] = resolution.adw
        if "adw_version" not in fields and resolution.adw_version is not None:
            row["adw_version"] = resolution.adw_version
        if "adw_rule" not in fields and resolution.adw_rule is not None:
            row["adw_rule"] = resolution.adw_rule
    if kind in TICKET_KINDS:
        row.update(active_seconds=0, verify_seconds=0, verify_open_seconds=0, repair_calls=0,
                   measured_at=row["started"])
    with (blob_store.lock() if resolution is not None else contextlib.nullcontext()):
        if resolution is not None:
            blob_rows = resolution.write_blobs()
        while True:
            minted = run_id or new_id(ID_PREFIX.get(kind, kind), tag)
            row_with_chain = {**row, "chain": chain_val or minted}
            try:
                with foundation.tx() as c:
                    if run_store.has_pins(c):
                        for sha, blob_kind, size in blob_rows:
                            blob_store.record_row(c, sha, blob_kind, size)
                        if resolution is not None and resolution.settings_snapshots:
                            run_store.save_settings_snapshots(c, resolution.settings_snapshots)
                    run_store.insert_run(minted, row_with_chain)
            except sqlite3.IntegrityError as e:
                if run_id or "runs.id" not in str(e):
                    raise
                continue
            break
    if resolution is not None:
        resolved_file = paths.RUNS_DIR / minted / "resolved.json"
        util.write_durably(resolved_file, resolution.resolved_bytes, 0o600)
    # A nested writer has not committed yet; retain its lease until exit.
    if kind in TICKET_KINDS and not nested:
        launch_lease.registered(row.get("repo", ""), str(row.get("issue_key") or row.get("issue") or ""))
    return minted


def attach_pin(run_id: str, resolution, *, pid: int | None = None) -> None:
    fields = {
        "config_commit": resolution.config_commit or None,
        "resolved_sha": resolution.resolved_sha,
        "engine_sha": resolution.engine_sha,
        "schema_version": resolution.schema_version,
    }
    if resolution.adw is not None:
        fields["adw"] = resolution.adw
    if resolution.adw_version is not None:
        fields["adw_version"] = resolution.adw_version
    if resolution.adw_rule is not None:
        fields["adw_rule"] = resolution.adw_rule
    if pid is not None:
        fields["pid"] = pid
    with blob_store.lock():
        blob_rows = resolution.write_blobs()
        with foundation.tx() as c:
            if run_store.has_pins(c):
                for sha, blob_kind, size in blob_rows:
                    blob_store.record_row(c, sha, blob_kind, size)
                if resolution.settings_snapshots:
                    run_store.save_settings_snapshots(c, resolution.settings_snapshots)
            run_store.note(run_id, **run_store.filter_run_fields(c, fields))
    existing = run_store.run_get(run_id)
    if existing is not None:
        resolution.payer = existing.get("payer")
    util.write_durably(paths.RUNS_DIR / run_id / "resolved.json", resolution.resolved_bytes, 0o600)


def claim(run_id: str, *, pid: int | None = None, resolution=None, **fields) -> bool:
    actual_pid = os.getpid() if pid is None else pid
    blob_rows: tuple[tuple[str, str, int], ...] = ()
    update_fields = {
        "capabilities": json.dumps(list(foundation.PROCESS_CAPABILITIES)),
        **fields,
        "pid": actual_pid,
    }
    continues_val = update_fields.get("continues")
    if continues_val and not update_fields.get("chain"):
        parent = run_store.run_get(str(continues_val))
        update_fields["chain"] = (parent.get("chain") or str(continues_val)) if parent else str(continues_val)
    if resolution is not None:
        update_fields.update(
            config_commit=resolution.config_commit or None,
            resolved_sha=resolution.resolved_sha,
            engine_sha=resolution.engine_sha,
            schema_version=resolution.schema_version,
        )
        if "adw" not in fields and resolution.adw is not None:
            update_fields["adw"] = resolution.adw
        if "adw_version" not in fields and resolution.adw_version is not None:
            update_fields["adw_version"] = resolution.adw_version
        if "adw_rule" not in fields and resolution.adw_rule is not None:
            update_fields["adw_rule"] = resolution.adw_rule
    with (blob_store.lock() if resolution is not None else contextlib.nullcontext()):
        if resolution is not None:
            blob_rows = resolution.write_blobs()
        with foundation.tx() as c:
            if run_store.has_pins(c):
                for sha, blob_kind, size in blob_rows:
                    blob_store.record_row(c, sha, blob_kind, size)
                if resolution is not None and resolution.settings_snapshots:
                    run_store.save_settings_snapshots(c, resolution.settings_snapshots)
            filtered = run_store.filter_run_fields(c, update_fields)
            moved = run_store.move(run_id, "running", ("queued", "running"), **filtered)
            if moved and actual_pid:
                from .store import factory

                factory.register(c, run_id, actual_pid, "run")
    if moved and resolution is not None:
        existing = run_store.run_get(run_id)
        if existing is not None:
            resolution.payer = existing.get("payer")
        util.write_durably(paths.RUNS_DIR / run_id / "resolved.json", resolution.resolved_bytes, 0o600)
    return moved


def launch_file(run_id: str) -> Path:
    return paths.RUNS_DIR / run_id / "launch.json"


def write_launch(run_id: str, command: Sequence[str], log: str = "") -> None:
    util.write_json(launch_file(run_id), {"command": list(command), "log": log})


def read_launch(run_id: str) -> dict:
    data = util.read_json(launch_file(run_id), {})
    return data if isinstance(data, dict) else {}


def step(run_id: str, name: str, res, seconds: float, effort: str = "", tier: str = "", kind: str = "",
         try_no: int = 0, auth_mode: str = "", *, inputs: Sequence[dict] = (), settings_sha: str = "",
         settings_snapshots: dict[str, str] | None = None,
         blob_rows: tuple[tuple[str, str, int], ...] = (),
         prompt_version: str = "", prompt_level: str = "", schema_version: str = "",
         step_id: int | None = None, chat_id: int | None = None, device: str = "",
         account_id: str | None = None, runner_version: str = "", unread: int = 0) -> None:
    steps(run_id, name, [(res, seconds)], effort, tier, kind, try_no, auth_mode,
          inputs=inputs, settings_sha=settings_sha, settings_snapshots=settings_snapshots,
          blob_rows=blob_rows, prompt_version=prompt_version, prompt_level=prompt_level,
          schema_version=schema_version, step_id=step_id, chat_id=chat_id, device=device,
          account_id=account_id, runner_version=runner_version, unread=unread)


def steps(run_id: str, name: str, calls, effort: str = "", tier: str = "", kind: str = "",
          try_no: int = 0, auth_mode: str = "", *, inputs: Sequence[dict] = (), settings_sha: str = "",
          settings_snapshots: dict[str, str] | None = None,
          blob_rows: tuple[tuple[str, str, int], ...] = (),
          prompt_version: str = "", prompt_level: str = "", schema_version: str = "",
          step_id: int | None = None, chat_id: int | None = None, device: str = "",
          account_id: str | None = None, runner_version: str = "", unread: int = 0) -> None:
    run_store.insert_steps(run_id, name, calls, effort, tier, kind, try_no, auth_mode,
                           inputs=inputs, settings_sha=settings_sha, settings_snapshots=settings_snapshots,
                           blob_rows=blob_rows, prompt_version=prompt_version, prompt_level=prompt_level,
                           schema_version=schema_version, step_id=step_id, chat_id=chat_id, device=device,
                           account_id=account_id, runner_version=runner_version, unread=unread)


def correct(run_id: str, usd: float, why: str, auth_mode: str = "subscription") -> None:
    if not math.isfinite(usd) or not why.strip():
        raise ValueError("correction needs a finite amount and a reason")
    if auth_mode not in ("subscription", "api_key", "local"):
        raise ValueError("invalid auth mode")
    if not run_store.run_get(run_id):
        raise ValueError(f"no run: {run_id}")
    cost_nusd = int(util.nusd(usd) or 0)
    paid_nusd = cost_nusd if auth_mode == "api_key" else 0
    step(
        run_id,
        "correction",
        agent.Result(
            ok=True,
            runner="correction",
            cost_usd=cost_nusd / 1_000_000_000,
            cost_nusd=cost_nusd,
            paid_nusd=paid_nusd,
            price_version="correction",
            error=why,
            auth_mode=auth_mode,
        ),
        0,
        kind="correction",
    )


def label_step(run_id: str, step: str, try_no: int, **labels) -> None:
    run_store.label_step(run_id, step, try_no, **labels)


def hold_ab(run_id: str, role: str, name: str, side: str, arm: dict) -> None:
    run_store.hold_ab(run_id, role, name, side, arm)


def metered_spent(run_id: str) -> float:
    return run_store.paid_by_run([run_id]).get(run_id, 0.0)


def note(run_id: str, *, base_version=None, **fields) -> None:
    if "status" in fields:
        raise ValueError("use move to change a run\'s status")
    run_store.note(run_id, base_version=base_version, **fields)


def move(run_id: str, to: str, expect: tuple[str, ...], *, base_version=None, **fields) -> bool:
    return run_store.move(run_id, to, expect, base_version=base_version, **fields)


def ask_stop(run_id: str, base_version=None) -> dict | None:
    return run_store.ask_stop(run_id, base_version)


class Stopped(Exception):
    pass


def stop_on_term(*_) -> None:
    raise Stopped


@contextmanager
def stoppable() -> Iterator[None]:
    signal.signal(signal.SIGTERM, stop_on_term)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


def stop_asked(run_id: str) -> bool:
    r = run_store.run_get(run_id)
    return bool(r and r["stop_asked"])


def settle(run_id: str, status: str, end_line: str = "", *, base_version=None, **fields) -> dict | None:
    if paths.DB_FILE.exists():
        with contextlib.suppress(sqlite3.Error, OSError, foundation.NoDatabase, foundation.IncompatibleSchema):
            if open_rows := run_store.open_steps(run_id):
                rebuild_turn_index(run_id, step_ids={r["id"] for r in open_rows}, missing_only=True)
    fields = {"status": status, "finished": util.iso(), **fields}
    settled = run_store.settle(run_id, fields, end_line, base_version)
    if settled is None:
        return None
    mark(run_id, "result", at=fields.pop("finished"), **fields)
    return settled


@contextmanager
def failed_on_crash(run_id: str):
    try:
        yield
    except foundation.LeaseFenced:
        raise
    except BaseException as e:
        why = util.first_line(f"crashed: {type(e).__name__}: {e}", 300)
        settle(run_id, "failed", end_line=f"failed: {why}", summary=why)
        raise


def dead_status(row: dict) -> str:
    if row.get("dry_run") or stop_asked(row["id"]):
        return "stopped"
    if row.get("only_step"):
        return "interrupted"
    if row["kind"] == "pipeline":
        target = int(row["task_id"]) if row.get("task_id") is not None else row["repo"]
        earlier = [r for r in (
            run_store.ticket_runs(target, limit=2, whole=True, live=True)
            if isinstance(target, int)
            else run_store.ticket_runs(row["repo"], row["issue"], limit=2, whole=True, live=True)
        ) if r["id"] != row["id"]]
        if earlier and earlier[0]["status"] == "interrupted" and not specgraphs.unfinished(row["repo"], row["issue"]):
            return "failed"
        return "interrupted"
    return "failed" if row["kind"] in FAILS_WHEN_DEAD else "interrupted"


@contextmanager
def dead(row: dict):
    if not row.get("lock"):
        yield not util.pid_alive(row.get("pid"))
    elif util.lock_is_held(row["lock"]):
        yield False
    else:
        with util.lock(row["lock"]) as free:
            yield free


def interrupt(row: dict, notes: Sequence[str] = (), *, base_version=None) -> dict | None:
    from .store import factory

    if factory.interrupted(row["id"]):
        return None
    status = dead_status(row)
    summary = "; ".join([STOPPED if status == "stopped" else INTERRUPTED, *notes])
    run_id = row["id"]
    with foundation.tx():
        settled = settle(run_id, status, end_line=f"{status}: {summary}", summary=summary, base_version=base_version)
        if settled and row["kind"] in TICKET_KINDS and (row["kind"] != "pipeline" or status == "failed"):
            insights_store.release_interrupted_feedback(run_id)
    if settled and not util.pid_alive(row.get("pid")):
        util.end_session(row.get("pid"))
    if settled and row["kind"] == "draft":
        shutil.rmtree(paths.draft_dir(row["id"]), ignore_errors=True)
    return settled


def reap(*, tickets: bool, then: Callable[[dict], None] = lambda row: None, release_claims: bool = True) -> list[dict]:
    settled = []
    for row in run_store.runs_marked_running():
        if ((row["kind"] in TICKET_KINDS) != tickets
                or (util.now() - util.parse_iso(row["started"])).total_seconds() < SETTLE_GRACE_SECONDS):
            continue
        with dead(row) as gone:
            if gone and (done := interrupt(row)):
                then(done)
                settled.append(done)
    if not tickets and release_claims:
        with foundation.tx() as c:
            loop_store.release_settled_claims(c)
    if not tickets:
        from . import pins

        pins.reap_config_worktrees()
    return settled


def slug(step: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", step).strip("-.") or "agent"


def fresh_record(folder: Path, name: str) -> tuple[Path, int]:
    folder.mkdir(parents=True, exist_ok=True)
    for n in itertools.count(1):
        path = folder / f"{name if n == 1 else f'{name}-{n}'}{RECORD_SUFFIX}"
        if path.name == TIMELINE:
            continue
        try:
            return path, os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


def runner_version_of(which: str) -> str:
    if which == "replay":
        from . import replay

        return replay.version()
    if which == "claude":
        from . import claude

        return claude.version()
    if which == "codex":
        from . import codex

        return codex.version()
    if which == "pi":
        from . import pi

        return pi.version()
    return ""


def _turn_charge_from_header(header: dict, ev: dict, *, billing_ctx: dict | None = None):
    from . import prices

    return prices.price_turn(header, ev, billing_ctx=billing_ctx)


def _turn_cost_from_header(header: dict, ev: dict) -> float | None:
    outcome = _turn_charge_from_header(header, ev)
    return None if outcome.cost_nusd is None else outcome.cost_nusd / 1_000_000_000


def _turn_at(started_iso: str, clock_seconds: float) -> str:
    if not started_iso:
        return ""
    if not clock_seconds:
        return started_iso
    try:
        return util.iso(util.parse_iso(started_iso) + timedelta(seconds=clock_seconds))
    except (TypeError, ValueError):
        return started_iso


def _relative_record_file(path: Path) -> str:
    try:
        return path.resolve().relative_to(paths.STATE_DIR.resolve()).as_posix()
    except ValueError:
        try:
            return path.relative_to(paths.STATE_DIR).as_posix()
        except ValueError:
            return path.name


def append(fd: int, text: str) -> tuple[int, int]:
    blob = gzip.compress(f"{text}\n".encode(errors="replace"))
    start = os.fstat(fd).st_size
    view = memoryview(blob)
    written = 0
    try:
        while written < len(blob):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("short write")
            written += n
    except OSError:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, start)
        raise
    return start, len(blob)


class Recorder:
    def __init__(self, run_id: str, step: str, header: dict, hidden: Sequence[str] | scrub.Redactions = (),
                 *, step_id: int | None = None):
        self.run_id, self.step, self.hidden = run_id, step, hidden
        self.step_id = step_id
        self.subject = f"{run_id}/{step}" if run_id else step
        self.runner, self.model = header.get("runner", ""), header.get("model", "")
        self.runner_version = str(header.get("runner_version") or runner_version_of(self.runner))
        self.lock = threading.RLock()
        self.at, self.started = util.iso(), time.monotonic()
        self.clocked, self.ended = self.started, False
        self._clock_seconds = 0.0
        self._prev_turn_seconds = 0.0
        self._turn_n = 0
        self._turn_tool = ""
        self._turn_excerpt = ""
        self._turn_status = "ok"
        self._billing_context: dict | None = (
            dict(header["billing_context"]) if isinstance(header.get("billing_context"), dict) else None
        )
        self._indexed_tin = 0
        self._indexed_tout = 0
        self._indexed_cr = 0
        self._indexed_cw = 0
        self._indexed_cost_nusd = 0
        self._indexed_paid_nusd = 0
        self.header = {
            "arete": "header",
            "v": 1,
            **header,
            "engine": codebase.STAMP,
            "runner_version": self.runner_version,
            "run_id": run_id,
            "step": step,
            "started": self.at,
            **({"step_id": step_id} if step_id is not None else {}),
        }
        self.reader = reader_for(self.header)
        self.held: list[tuple[str, str]] | None = []
        self.waiting = threading.Timer(HEADER_WAIT_SECONDS, self.release)
        self.waiting.daemon = True
        self.path: Path | None = None
        self.fd: int | None = None
        try:
            self.path, self.fd = fresh_record(paths.record_dir(run_id), slug(step))
        except OSError as e:
            self.fail(e)
        if self.model or self.runner == "replay" or self.fd is None:
            self.release()
        else:
            self.waiting.start()

    @property
    def unread_count(self) -> int:
        return int(getattr(self.reader, "unread", 0))

    @property
    def first_unknown(self) -> str:
        return str(getattr(self.reader, "first_unknown", ""))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind, error, trace) -> None:
        if error is not None:
            why = self.exception_text(error)
            self.end({"ok": False, "error": why, "seconds": self.elapsed()})
        self.close()

    def exception_text(self, error: BaseException) -> str:
        return STOPPED if self.stop_requested(error) else f"crashed: {failure(error)}"

    def stop_requested(self, error: BaseException) -> bool:
        if isinstance(error, KeyboardInterrupt):
            return True
        try:
            return stop_asked(self.run_id)
        except sqlite3.Error:
            return False

    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 1)

    def _observe_written(self, raw: str, span: tuple[int, int] | None) -> None:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(entry, dict):
            return
        if entry.get("arete"):
            if entry.get("arete") == "billing" and isinstance(entry.get("billing_context"), dict):
                self._billing_context = dict(entry["billing_context"])
            return
        if self.runner == "claude" and entry.get("type") == "system" and entry.get("subtype") == "init":
            init_ver = str(entry.get("claude_code_version") or "").strip()
            if init_ver and init_ver != self.runner_version:
                self.runner_version = init_ver
                self.header["runner_version"] = init_ver
        for ev in self.reader(entry):
            kind = ev.get("kind")
            if kind == "tool_call":
                self._turn_tool = str(ev.get("name") or self._turn_tool)
                if not self._turn_excerpt:
                    self._turn_excerpt = util.one_line(f"{ev.get('name', '')}: {ev.get('input', '')}", 200)
            elif kind == "message" and ev.get("role") == "assistant" and ev.get("text"):
                self._turn_excerpt = util.one_line(str(ev["text"]), 200)
            elif kind == "tool_result" and ev.get("error"):
                self._turn_status = "error"
            elif kind == "usage":
                is_total = bool(ev.get("total"))
                tin = int(ev.get("tokens_in") or 0)
                cr = int(ev.get("cache_read") or 0)
                cw = int(ev.get("cache_write") or 0)
                tout = int(ev.get("tokens_out") or 0)
                outcome = _turn_charge_from_header(self.header, ev, billing_ctx=self._billing_context)
                cost_n = outcome.cost_nusd
                paid_n = outcome.paid_nusd
                if is_total:
                    d_tin = max(0, tin - self._indexed_tin)
                    d_cr = max(0, cr - self._indexed_cr)
                    d_cw = max(0, cw - self._indexed_cw)
                    d_tout = max(0, tout - self._indexed_tout)
                    d_cost = None if cost_n is None else max(0, cost_n - self._indexed_cost_nusd)
                    d_paid = None if paid_n is None else max(0, paid_n - self._indexed_paid_nusd)
                    if not any((d_tin, d_tout, d_cr, d_cw)) and not (d_cost or 0):
                        continue
                    tin, cr, cw, tout, cost_n, paid_n = d_tin, d_cr, d_cw, d_tout, d_cost, d_paid
                self._turn_n += 1
                self._indexed_tin += tin
                self._indexed_cr += cr
                self._indexed_cw += cw
                self._indexed_tout += tout
                self._indexed_cost_nusd += cost_n or 0
                self._indexed_paid_nusd += paid_n or 0
                turn_seconds = round(max(0.0, self._clock_seconds - self._prev_turn_seconds), 2)
                self._prev_turn_seconds = self._clock_seconds
                ctx_json = (
                    json.dumps(self._billing_context, sort_keys=True, separators=(",", ":"))
                    if self._billing_context is not None
                    else None
                )
                if self.step_id is not None and self.path is not None and span is not None and paths.DB_FILE.exists():
                    with contextlib.suppress(sqlite3.Error, OSError, foundation.NoDatabase, foundation.IncompatibleSchema):
                        run_store.insert_turn(
                            self.step_id,
                            self._turn_n,
                            self.run_id,
                            at=_turn_at(self.at, self._clock_seconds),
                            status=self._turn_status,
                            tool=self._turn_tool,
                            excerpt=self._turn_excerpt,
                            seconds=turn_seconds,
                            tokens_in=tin,
                            cache_read=cr,
                            tokens_out=tout,
                            cost_nusd=cost_n,
                            paid_nusd=paid_n,
                            auth_mode=outcome.auth_mode,
                            price_version=outcome.price_version,
                            cache_write=cw,
                            billing_context=ctx_json,
                            file=_relative_record_file(self.path),
                            member_offset=span[0],
                            member_len=span[1],
                        )
                self._turn_tool = ""
                self._turn_excerpt = ""
                self._turn_status = "ok"

    def line(self, raw: str) -> None:
        raw = raw.rstrip("\r\n")
        if not raw.strip():
            return
        with self.lock:
            clock_line = ""
            if time.monotonic() - self.clocked >= CLOCK_SECONDS:
                self.clocked = time.monotonic()
                self._clock_seconds = self.elapsed()
                clock_line = json.dumps({"arete": "clock", "seconds": self._clock_seconds})
            scrubbed = scrub.text(raw, "record", self.subject, extra=self.hidden)
            if self.held is None:
                if clock_line:
                    self.put(clock_line)
                span = self.put(scrubbed)
                self._observe_written(scrubbed, span)
                return
            if clock_line:
                self.held.append((clock_line, ""))
            self.held.append((scrubbed, scrubbed))
            if self.runner == "claude":
                with contextlib.suppress(json.JSONDecodeError):
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and parsed.get("type") == "system" and parsed.get("subtype") == "init":
                        if init_ver := str(parsed.get("claude_code_version") or "").strip():
                            self.runner_version = self.header["runner_version"] = init_ver
            if model := named_model(raw):
                self.model = self.header["model"] = model
                self.release()

    def release(self) -> None:
        with self.lock:
            if self.held is None:
                return
            held, self.held = self.held, None
            self.waiting.cancel()
            self.reader = reader_for(self.header)
            self.write(self.header)
            for text, observe in held:
                span = self.put(text)
                if observe:
                    self._observe_written(observe, span)

    def finish(self, res: agent.Result, seconds: float) -> None:
        self.end({"ok": res.ok, "error": res.error, "text": res.text, "structured": res.structured,
                  "cost_usd": res.cost_usd, "cost_nusd": res.cost_nusd, "paid_nusd": res.paid_nusd,
                  "price_version": res.price_version, "reference_price_version": res.reference_price_version,
                  "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
                  "first_context_tokens": res.first_context_tokens, "turns": res.turns, "model": res.model,
                  "provider": res.provider, "runner": res.runner, "auth_mode": res.auth_mode,
                  "auth_modes": list(res.modes), "paid_usd": res.paid_usd, "credential": res.credential,
                  "effort": res.effort, "requested_effort": res.requested_effort, "skipped": res.skipped,
                  "cache_read": res.cache_read, "cache_write": res.cache_write,
                  "reference_usd": res.reference_usd, "quote": res.quote,
                  **({"calls": [{"runner": c.runner, "model": c.model, "provider": c.provider,
                                "auth_mode": c.auth_mode, "cost_usd": c.cost_usd, "paid_usd": c.paid_usd,
                                "cost_nusd": c.cost_nusd, "paid_nusd": c.paid_nusd,
                                "price_version": c.price_version, "reference_price_version": c.reference_price_version,
                                "tokens_in": c.tokens_in, "tokens_out": c.tokens_out,
                                "cache_read": c.cache_read, "cache_write": c.cache_write, "turns": c.turns,
                                "reference_usd": c.reference_usd, "quote": c.quote} for c in res.calls]}
                     if res.calls else {}),
                  "session": res.session.id, "seconds": round(seconds, 1)})

    def end(self, result: dict) -> None:
        if self.ended:
            return
        self.ended = True
        result = {"runner": self.runner, "model": self.model, "provider": self.header.get("provider", ""),
                  "auth_mode": self.header.get("auth_mode", ""), **result, "unread": self.unread_count}
        self.release()
        self.write({"arete": "result", **result})
        self.close()
        mark(self.run_id, "agent", at=self.at, step=self.step, record=self.path.name if self.path else "",
             runner=self.runner, model=result.get("model") or self.model, ok=result["ok"],
             auth_mode=result["auth_mode"], provider=result.get("provider", self.header.get("provider", "")),
             **({"atom": self.header["atom"]} if self.header.get("atom") else {}),
             **{k: result[k] for k in ("auth_modes", "paid_usd", "credential", "effort") if k in result},
             seconds=result["seconds"], **{k: result[k] for k in ("cost_usd", "turns") if k in result},
             **({"error": util.first_line(scrub.text(result["error"], "record", self.subject, extra=self.hidden), 300)}
                if result["error"] else {}))

    def write(self, entry: dict) -> tuple[int, int] | None:
        with self.lock:
            return self.put(scrub.text(json.dumps(entry, ensure_ascii=False, default=str), "record", self.subject,
                                       extra=self.hidden))

    def put(self, text: str) -> tuple[int, int] | None:
        if self.fd is None:
            return None
        try:
            return append(self.fd, text)
        except OSError as e:
            self.fail(e)
            return None

    def fail(self, e: OSError) -> None:
        print(f"{util.iso()} stopped recording {self.run_id} {self.step}: {e}", file=sys.stderr, flush=True)
        self.close()

    def close(self) -> None:
        with self.lock:
            self.release()
            fd, self.fd = self.fd, None
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)


def mark(run_id: str, kind: str, **fields) -> None:
    if not run_id:
        return
    text = scrub.text(json.dumps({"v": 1, "kind": kind, "at": util.iso(), **fields}, ensure_ascii=False, default=str),
                      "record", f"{run_id}/{kind}")
    folder = paths.record_dir(run_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / TIMELINE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                append(fd, text)
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except OSError as e:
        print(f"{util.iso()} could not mark {kind} for {run_id}: {e}", file=sys.stderr, flush=True)


@contextmanager
def timed(run_id: str, kind: str, **detail):
    at, started = util.iso(), time.monotonic()
    try:
        yield detail
    except BaseException as e:
        detail["error"] = failure(e)
        raise
    finally:
        mark(run_id, kind, at=at, **detail, seconds=round(time.monotonic() - started, 2))


def failure(e: BaseException) -> str:
    return ": ".join(filter(None, [type(e).__name__, util.first_line(str(e), 300)]))


def record_files(run_id: str) -> list[Path]:
    found = [f for f in paths.record_dir(run_id).glob(f"*{RECORD_SUFFIX}") if f.name != TIMELINE]
    return sorted(found, key=lambda f: f.stat().st_mtime)


def find_record(run_id: str, step: str) -> Path | None:
    folder = paths.record_dir(run_id)
    for name in (step, f"{step}{RECORD_SUFFIX}", f"{slug(step)}{RECORD_SUFFIX}"):
        if name != TIMELINE and (folder / name).is_file():
            return folder / name
    return None


def _prefix_keep(tail: bytes) -> int:
    for size in (3, 2, 1):
        if len(tail) >= size and GZIP_MAGIC.startswith(tail[-size:]):
            return size
    return 0


class AppendedLines:
    def __init__(self, path: Path, compressed: bool = True):
        self.path, self.compressed = path, compressed
        self.offset = 0
        self.pending = b""
        self._buf = b""
        self._buf_start = 0
        self.skipped = 0

    def _read_new(self) -> bytes:
        try:
            f = self.path.open("rb")
        except FileNotFoundError:
            return b""
        with f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                f.seek(self.offset)
                chunk = f.read()
                self.offset += len(chunk)
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return chunk

    def read_members(self, *, final: bool = False) -> Iterator[tuple[int, int, list[str]]]:
        if not self.compressed:
            raise ValueError("read_members requires compressed=True")
        chunk = self._read_new()
        if chunk:
            if not self._buf:
                self._buf_start = self.offset - len(chunk)
            self._buf += chunk
        buf = self._buf
        buf_start = self._buf_start
        pos = 0
        n = len(buf)
        while pos < n:
            if n - pos < 4:
                if final:
                    self.skipped += n - pos
                    pos = n
                    break
                if GZIP_MAGIC.startswith(buf[pos:]):
                    break
                keep = _prefix_keep(buf[pos:])
                self.skipped += (n - pos) - keep
                pos = n - keep
                break
            if buf[pos:pos + 4] != GZIP_MAGIC:
                nxt = buf.find(GZIP_MAGIC, pos + 1)
                if nxt != -1:
                    self.skipped += nxt - pos
                    pos = nxt
                    continue
                if final:
                    self.skipped += n - pos
                    pos = n
                    break
                keep = _prefix_keep(buf[pos:])
                self.skipped += (n - pos) - keep
                pos = n - keep
                break
            dec = zlib.decompressobj(wbits=31)
            try:
                data = dec.decompress(buf[pos:])
            except zlib.error:
                nxt = buf.find(GZIP_MAGIC, pos + 1)
                if nxt != -1:
                    self.skipped += nxt - pos
                    pos = nxt
                    continue
                if final:
                    self.skipped += n - pos
                    pos = n
                    break
                keep = _prefix_keep(buf[pos + 1:])
                self.skipped += (n - pos) - keep
                pos = n - keep
                break
            if dec.eof:
                member_len = (n - pos) - len(dec.unused_data)
                member_offset = buf_start + pos
                pos += member_len
                member_lines = [
                    line.decode(errors="replace")
                    for line in data.split(b"\n")
                    if line.strip()
                ]
                self._buf = buf[pos:]
                self._buf_start = buf_start + pos
                yield member_offset, member_len, member_lines
                continue
            found_whole = -1
            search_from = pos + 1
            while (nxt := buf.find(GZIP_MAGIC, search_from)) != -1:
                probe = zlib.decompressobj(wbits=31)
                try:
                    probe.decompress(buf[nxt:])
                    if probe.eof:
                        found_whole = nxt
                        break
                except zlib.error:
                    pass
                search_from = nxt + 1
            if found_whole != -1:
                self.skipped += found_whole - pos
                pos = found_whole
                continue
            if final:
                self.skipped += n - pos
                pos = n
            break
        self._buf = buf[pos:]
        self._buf_start = buf_start + pos

    def read(self, *, final: bool = False) -> Iterator[str]:
        if self.compressed:
            for _, _, member_lines in self.read_members(final=final):
                yield from member_lines
            return
        chunk = self._read_new()
        if not chunk:
            return
        self.pending += chunk
        complete = self.pending.split(b"\n")
        self.pending = complete.pop()
        for line in complete:
            if line.strip():
                yield line.decode(errors="replace")


def raw_lines(path: Path) -> Iterator[str]:
    source = AppendedLines(path)
    yield from source.read(final=True)
    if source.skipped:
        print(f"skipped {source.skipped} damaged bytes in {path}", file=sys.stderr, flush=True)


def run_status(run_id: str) -> str:
    try:
        row = run_store.run_get(run_id, connect=foundation.reader)
    except sqlite3.Error:
        return ""
    return (row.get("status") or "") if row else ""


def follow(run_id: str, step: str) -> Iterator[str]:
    path = find_record(run_id, step)
    if not path and (step in ("steps", TIMELINE) or run_status(run_id) != "running"):
        raise FileNotFoundError(step)
    source = AppendedLines(path) if path else None
    timeline = AppendedLines(paths.record_dir(run_id) / TIMELINE)
    report = AppendedLines(run_lines.path(run_id), compressed=False)
    ended = False
    while True:
        if source is None and (path := find_record(run_id, step)):
            source = AppendedLines(path)
        if source is not None:
            with contextlib.closing(source.read()) as available:
                for line in available:
                    result = next(entries([line]), {}).get("arete") == "result"
                    yield line
                    if result:
                        return
        if ended:
            if source is None:
                raise FileNotFoundError(step)
            return
        status = run_status(run_id)
        ended = bool(status and status != "running")
        for entry in entries(timeline.read()):
            ended |= entry.get("kind") == "result"
        for entry in entries(report.read()):
            ended |= entry.get("event") == run_lines.END
        if not ended:
            time.sleep(run_lines.FOLLOW_POLL_SECONDS)


def lines(path: Path) -> Iterator[dict]:
    yield from entries(raw_lines(path))


def entries(source: Iterable[str]) -> Iterator[dict]:
    for line in source:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def events(path: Path) -> Iterator[dict]:
    yield from normalized(lines(path))


def numbered_events(path: Path) -> Iterator[dict]:
    number = 0

    def numbered() -> Iterator[dict]:
        nonlocal number
        for idx, raw in enumerate(raw_lines(path), 1):
            number = idx
            yield from entries([raw])

    for event in normalized(numbered()):
        yield {**event, "line": number}


def runs_since(since: str, until: str = "9999") -> list[dict]:
    return run_store.finished_runs_since(since, until)


def read_run(run_id: str) -> dict | None:
    row = run_store.run_get(run_id)
    if not row:
        return None
    try:
        marks = list(lines(paths.record_dir(run_id) / TIMELINE))
    except OSError:
        marks = []
    try:
        parked = util.read_json(paths.RUNS_DIR / run_id / "parked.json", {})
    except OSError:
        parked = {}
    try:
        waited = sum(run_lines.wait_minutes(run_id).values())
    except OSError:
        waited = 0
    rows = run_store.steps(run_id)
    tries: dict[str, int] = {}
    for r in rows:
        tries[r["step"]] = max(tries.get(r["step"], 0), r["try"] or 0)
    try:
        seconds = (util.parse_iso(row["finished"]) - util.parse_iso(row["started"])).total_seconds()
    except (TypeError, ValueError):
        seconds = 0
    return {
        "id": run_id, "kind": row["kind"], "repo": row["repo"], "issue": row["issue"], "status": row["status"],
        "summary": row["summary"], "started": row["started"], "finished": row["finished"],
        "seconds": max(0, seconds), "wait_minutes": waited,
        "parked": str(parked.get("reason") or "") if isinstance(parked, dict) else "",
        "marks": marks,
        "calls": [{"step": m.get("step", ""), "record": m.get("record", ""), "runner": m.get("runner", ""),
                   "model": m.get("model", ""), "ok": bool(m.get("ok")), "error": m.get("error", ""),
                   "seconds": m.get("seconds") or 0} for m in marks if m.get("kind") == "agent"],
        "tries": tries,
        "failed_steps": [{"step": r["step"], "try": r["try"], "error": r["error"], "kind": r["kind"]}
                         for r in rows if not r["ok"]],
    }


def last_message(path: Path, notice: re.Pattern[str] | None = None) -> str:
    found = ""
    try:
        for event in events(path):
            text = str(event.get("text") or "").strip()
            if (event.get("kind") == "message" and event.get("role") == "assistant" and text
                    and not (notice and notice.search(text))):
                found = text
    except OSError:
        return ""
    return found


def _version_key(version: str) -> tuple[int, ...]:
    nums = [int(part) for part in re.findall(r"\d+", version or "")]
    return tuple(nums)


class _TrackedReader:
    def __init__(self, fn: Callable[[dict, Callable[[str], None]], list[dict]]):
        self._fn = fn
        self.unread = 0
        self.first_unknown = ""

    def _note_unknown(self, label: str) -> None:
        self.unread += 1
        if not self.first_unknown:
            self.first_unknown = label

    def __call__(self, event: dict) -> list[dict]:
        return self._fn(event, self._note_unknown)


def normalized(source: Iterable[dict]) -> Iterator[dict]:
    seconds, read = 0.0, read_nothing
    for line in source:
        own = line.pop("arete", None)
        if own == "clock":
            seconds = line.get("seconds", seconds)
        elif own in ("header", "result", "attach", "lost"):
            if own == "header":
                read = reader_for(line)
            yield {"kind": own, "seconds": seconds, **line}
        elif not own:
            for event in read(line):
                yield {**event, "seconds": seconds}


def read_nothing(event: dict) -> list[dict]:
    return []


def blocks(content) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def block_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in blocks(content) if b.get("type") == "text")


CLAUDE_SKIPPED_LINES = frozenset(("system", "tool_progress", "rate_limit_event"))
CODEX_SKIPPED_LINES = frozenset(("thread.started", "turn.started"))


def claude_reader(header: dict) -> _TrackedReader:
    tool_names: dict[str, str] = {}
    counted: set[str] = set()
    resumed = header.get("resume") if isinstance(header.get("resume"), dict) else {}
    spent_before = float(resumed.get("spent_usd") or 0)
    usage_rates = header.get("usage_rates")

    def read(e: dict, unknown: Callable[[str], None]) -> list[dict]:
        message = e.get("message") if isinstance(e.get("message"), dict) else {}
        out = []
        etype = e.get("type")
        if etype == "assistant":
            for b in blocks(message.get("content")):
                btype = b.get("type")
                if btype == "text":
                    out.append({"kind": "message", "role": "assistant", "text": b.get("text", "")})
                elif btype == "thinking":
                    out.append({"kind": "message", "role": "thinking", "text": b.get("thinking", "")})
                elif btype == "tool_use":
                    tool_names[b.get("id", "")] = b.get("name", "")
                    out.append({"kind": "tool_call", "id": b.get("id", ""), "name": b.get("name", ""),
                                "input": b.get("input")})
                else:
                    unknown(f"assistant:{btype}")
            if isinstance(usage := message.get("usage"), dict) and message.get("id") not in counted:
                counted.add(message.get("id"))
                out.append({"kind": "usage", "model": message.get("model", ""), **agent.claude_tokens(usage),
                            "cost_usd": None})
        elif etype == "user":
            if isinstance(message.get("content"), str):
                out.append({"kind": "message", "role": "user", "text": message["content"]})
            for b in blocks(message.get("content")):
                btype = b.get("type")
                if btype == "tool_result":
                    out.append({"kind": "tool_result", "id": b.get("tool_use_id", ""),
                                "name": tool_names.get(b.get("tool_use_id", ""), ""),
                                "text": block_text(b.get("content")), "error": bool(b.get("is_error"))})
                elif btype == "text":
                    out.append({"kind": "message", "role": "user", "text": b.get("text", "")})
                else:
                    unknown(f"user:{btype}")
        elif etype == "result":
            session_cost = e.get("total_cost_usd")
            tokens = agent.claude_tokens(e.get("usage") or {})
            cost = (agent.token_cost(usage_rates, **tokens) if usage_rates is not None else
                    None if session_cost is None else max(0.0, float(session_cost) - spent_before))
            out.append({"kind": "usage", "total": True, "model": next(iter(e.get("modelUsage") or {}), ""),
                        **tokens, "cost_usd": cost})
        elif etype not in CLAUDE_SKIPPED_LINES:
            unknown(f"line:{etype}")
        return out

    return _TrackedReader(read)


def pi_reader(header: dict) -> _TrackedReader:
    def read(e: dict, unknown: Callable[[str], None]) -> list[dict]:
        message = e.get("message") if isinstance(e.get("message"), dict) else {}
        if e.get("type") != "message_end":
            return []
        role = message.get("role")
        if role == "toolResult":
            for b in blocks(message.get("content")):
                if b.get("type") != "text":
                    unknown(f"toolResult:{b.get('type')}")
            return [{"kind": "tool_result", "id": message.get("toolCallId", ""), "name": message.get("toolName", ""),
                     "text": block_text(message.get("content")), "error": bool(message.get("isError"))}]
        if role == "user":
            for b in blocks(message.get("content")):
                if b.get("type") != "text":
                    unknown(f"user:{b.get('type')}")
            return [{"kind": "message", "role": "user", "text": block_text(message.get("content"))}]
        if role != "assistant":
            unknown(f"role:{role}")
            return []
        out = []
        for b in blocks(message.get("content")):
            btype = b.get("type")
            if btype == "text":
                out.append({"kind": "message", "role": "assistant", "text": b.get("text", "")})
            elif btype == "thinking":
                out.append({"kind": "message", "role": "thinking", "text": b.get("thinking", "")})
            elif btype == "toolCall":
                out.append({"kind": "tool_call", "id": b.get("id", ""), "name": b.get("name", ""),
                            "input": b.get("arguments")})
            else:
                unknown(f"assistant:{btype}")
        if isinstance(usage := message.get("usage"), dict):
            totals, provider = agent.usage_totals([usage]), message.get("provider", "")
            out.append({"kind": "usage",
                        "model": f"{provider}/{message.get('model')}" if provider else message.get("model", ""),
                        "tokens_in": totals["tokens_in"], "tokens_out": totals["tokens_out"],
                        "cache_read": totals["cache_read"], "cache_write": totals["cache_write"],
                        "cost_usd": totals["cost_usd"]})
        return out

    return _TrackedReader(read)


def read_pi(e: dict) -> list[dict]:
    return pi_reader({})(e)


def mcp_call(item: dict) -> dict:
    arguments = item.get("arguments")
    if isinstance(arguments, str):
        with contextlib.suppress(ValueError):
            arguments = json.loads(arguments)
    return {"kind": "tool_call", "id": item.get("id", ""), "name": f"mcp__{item.get('server', '')}__{item.get('tool', '')}",
            "input": arguments}


def mcp_result(item: dict, name: str) -> dict:
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    failure = item.get("error")
    text = block_text(result.get("content")) or (str(failure.get("message") if isinstance(failure, dict) else failure or ""))
    return {"kind": "tool_result", "id": item.get("id", ""), "name": name, "text": text,
            "error": bool(failure) or item.get("status") == "failed"}


def codex_reader(header: dict) -> _TrackedReader:
    before: dict = {}
    started: set[str] = set()

    def read(e: dict, unknown: Callable[[str], None]) -> list[dict]:
        nonlocal before
        if e.get("arete") == "command":
            before = e.get("usage_before") or {}
            return []
        etype = e.get("type")
        if etype in CODEX_SKIPPED_LINES:
            return []
        item = e.get("item") if isinstance(e.get("item"), dict) else {}
        kind, at = item.get("type"), item.get("id", "")
        if etype == "turn.completed":
            if isinstance(usage := e.get("usage"), dict):
                return [{"kind": "usage", "total": True, "model": header.get("model", ""),
                         "tokens_in": max(0, int(usage.get("input_tokens") or 0) - int(before.get("input_tokens") or 0)),
                         "tokens_out": max(0, int(usage.get("output_tokens") or 0) - int(before.get("output_tokens") or 0)),
                         "cost_usd": None}]
            return []
        if etype == "item.started":
            if kind == "command_execution":
                return [{"kind": "tool_call", "id": at, "name": "Bash", "input": {"command": item.get("command", "")}}]
            if kind == "mcp_tool_call":
                started.add(at)
                return [mcp_call(item)]
            if kind == "file_change":
                return []
            unknown(f"item.started:{kind}")
            return []
        if etype == "item.completed":
            if kind == "mcp_tool_call":
                call = mcp_call(item)
                return [*([] if at in started else [call]), mcp_result(item, call["name"])]
            if kind in ("agent_message", "reasoning"):
                role = "assistant" if kind == "agent_message" else "thinking"
                return [{"kind": "message", "role": role, "text": item.get("text", "")}]
            if kind == "command_execution":
                return [{"kind": "tool_result", "id": at, "name": "Bash", "text": item.get("aggregated_output", ""),
                         "error": item.get("exit_code") not in (0, None)}]
            if kind == "file_change":
                return [{"kind": "tool_call", "id": at, "name": "apply_patch", "input": {"changes": item.get("changes")}}]
            if kind == "error":
                return []
            unknown(f"item.completed:{kind}")
            return []
        unknown(f"line:{etype}")
        return []

    return _TrackedReader(read)


def replay_reader(header: dict) -> _TrackedReader:
    def read(e: dict, unknown: Callable[[str], None]) -> list[dict]:
        etype = e.get("type")
        if etype == "replay":
            return []
        if etype == "tool_call":
            return [{"kind": "tool_call", "id": e.get("id", ""), "name": e.get("name", ""), "input": e.get("input")}]
        if etype == "tool_result":
            return [{"kind": "tool_result", "id": e.get("id", ""), "name": e.get("name", ""), "text": e.get("text", ""),
                     "error": bool(e.get("error"))}]
        if etype == "message_end":
            message = e.get("message") if isinstance(e.get("message"), dict) else {}
            if isinstance(usage := message.get("usage"), dict):
                totals = agent.usage_totals([usage])
                return [{"kind": "usage", "model": header.get("model", ""),
                         "tokens_in": totals["tokens_in"], "tokens_out": totals["tokens_out"],
                         "cache_read": totals["cache_read"], "cache_write": totals["cache_write"],
                         "cost_usd": totals["cost_usd"]}]
            return []
        unknown(f"line:{etype}")
        return []

    return _TrackedReader(read)


def read_replay(e: dict) -> list[dict]:
    return replay_reader({})(e)


READERS: dict[str, tuple[tuple[str, Callable[[dict], Callable[[dict], list[dict]]]], ...]] = {
    "claude": (("0", claude_reader),),
    "pi": (("0", pi_reader),),
    "codex": (("0", codex_reader),),
    "replay": (("0", replay_reader),),
}


def reader_for(header: dict) -> Callable[[dict], list[dict]]:
    versions = READERS.get(header.get("runner", ""))
    if not versions:
        return read_nothing
    ordered = sorted(versions, key=lambda item: _version_key(item[0]))
    raw_version = str(header.get("runner_version") or "").strip()
    if not raw_version:
        return ordered[0][1](header)
    target = _version_key(raw_version)
    chosen = ordered[0][1]
    for min_ver, factory in ordered:
        if _version_key(min_ver) <= target:
            chosen = factory
    return chosen(header)


def _rebuild_file_turns(path: Path, *, step_ids: set[int] | None = None,
                        missing_only: bool = False) -> int:
    header: dict | None = None
    billing_ctx: dict | None = None
    read: Callable[[dict], list[dict]] = read_nothing
    sid: int | None = None
    run_id = ""
    started = ""
    clock_seconds = 0.0
    prev_turn_seconds = 0.0
    turn_n = 0
    indexed_tin = 0
    indexed_cr = 0
    indexed_cw = 0
    indexed_tout = 0
    indexed_cost_nusd = 0
    indexed_paid_nusd = 0
    turn_tool = ""
    turn_excerpt = ""
    turn_status = "ok"
    rel_file = _relative_record_file(path)
    existing_ns: set[int] = set()
    written = 0
    for member_offset, member_len, member_lines in AppendedLines(path).read_members(final=True):
        for raw in member_lines:
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(line, dict):
                continue
            own = line.get("arete")
            if own == "header":
                header = line
                billing_ctx = (
                    dict(line["billing_context"]) if isinstance(line.get("billing_context"), dict) else None
                )
                run_id = str(line.get("run_id") or path.parent.parent.name)
                started = str(line.get("started") or "")
                raw_sid = line.get("step_id")
                sid = int(raw_sid) if isinstance(raw_sid, int) else None
                if sid is None:
                    step_name = str(line.get("step") or "")
                    node, _ = run_store.step_node_and_chat(step_name)
                    for row in run_store.steps(run_id):
                        if row.get("step") == step_name or row.get("node") == node:
                            sid = row["id"]
                            if billing_ctx is None and row.get("billing_context"):
                                with contextlib.suppress(ValueError, TypeError):
                                    parsed_ctx = json.loads(row["billing_context"])
                                    if isinstance(parsed_ctx, dict):
                                        billing_ctx = parsed_ctx
                            break
                elif billing_ctx is None:
                    for row in run_store.steps(run_id):
                        if row.get("id") == sid and row.get("billing_context"):
                            with contextlib.suppress(ValueError, TypeError):
                                parsed_ctx = json.loads(row["billing_context"])
                                if isinstance(parsed_ctx, dict):
                                    billing_ctx = parsed_ctx
                            break
                if sid is None or (step_ids is not None and sid not in step_ids):
                    return 0
                if missing_only:
                    existing_ns = {t["n"] for t in run_store.step_turns(sid)}
                read = reader_for(header)
                continue
            if own == "billing":
                if isinstance(line.get("billing_context"), dict):
                    billing_ctx = dict(line["billing_context"])
                continue
            if own == "clock":
                clock_seconds = float(line.get("seconds", clock_seconds) or 0.0)
                continue
            if own:
                continue
            if header is None or sid is None:
                continue
            for ev in read(line):
                kind = ev.get("kind")
                if kind == "tool_call":
                    turn_tool = str(ev.get("name") or turn_tool)
                    if not turn_excerpt:
                        turn_excerpt = util.one_line(f"{ev.get('name', '')}: {ev.get('input', '')}", 200)
                elif kind == "message" and ev.get("role") == "assistant" and ev.get("text"):
                    turn_excerpt = util.one_line(str(ev["text"]), 200)
                elif kind == "tool_result" and ev.get("error"):
                    turn_status = "error"
                elif kind == "usage":
                    is_total = bool(ev.get("total"))
                    tin = int(ev.get("tokens_in") or 0)
                    cr = int(ev.get("cache_read") or 0)
                    cw = int(ev.get("cache_write") or 0)
                    tout = int(ev.get("tokens_out") or 0)
                    outcome = _turn_charge_from_header(header, ev, billing_ctx=billing_ctx)
                    cost_n = outcome.cost_nusd
                    paid_n = outcome.paid_nusd
                    if is_total:
                        d_tin = max(0, tin - indexed_tin)
                        d_cr = max(0, cr - indexed_cr)
                        d_cw = max(0, cw - indexed_cw)
                        d_tout = max(0, tout - indexed_tout)
                        d_cost = None if cost_n is None else max(0, cost_n - indexed_cost_nusd)
                        d_paid = None if paid_n is None else max(0, paid_n - indexed_paid_nusd)
                        if not any((d_tin, d_tout, d_cr, d_cw)) and not (d_cost or 0):
                            continue
                        tin, cr, cw, tout, cost_n, paid_n = d_tin, d_cr, d_cw, d_tout, d_cost, d_paid
                    turn_n += 1
                    indexed_tin += tin
                    indexed_cr += cr
                    indexed_cw += cw
                    indexed_tout += tout
                    indexed_cost_nusd += cost_n or 0
                    indexed_paid_nusd += paid_n or 0
                    turn_seconds = round(max(0.0, clock_seconds - prev_turn_seconds), 2)
                    prev_turn_seconds = clock_seconds
                    if not missing_only or turn_n not in existing_ns:
                        ctx_json = (
                            json.dumps(billing_ctx, sort_keys=True, separators=(",", ":"))
                            if billing_ctx is not None
                            else None
                        )
                        run_store.insert_turn(
                            sid,
                            turn_n,
                            run_id,
                            at=_turn_at(started, clock_seconds),
                            status=turn_status,
                            tool=turn_tool,
                            excerpt=turn_excerpt,
                            seconds=turn_seconds,
                            tokens_in=tin,
                            cache_read=cr,
                            tokens_out=tout,
                            cost_nusd=cost_n,
                            paid_nusd=paid_n,
                            auth_mode=outcome.auth_mode,
                            price_version=outcome.price_version,
                            cache_write=cw,
                            billing_context=ctx_json,
                            file=rel_file,
                            member_offset=member_offset,
                            member_len=member_len,
                        )
                        written += 1
                    turn_tool = ""
                    turn_excerpt = ""
                    turn_status = "ok"
    return written


def _purged_turn_runs() -> set[str]:
    if not paths.DB_FILE.exists():
        return set()
    try:
        with foundation.reader() as c:
            if foundation.migration_applied(foundation.RUN_RETENTION_SCHEMA, c):
                purged = {
                    str(r[0])
                    for r in c.execute("SELECT id FROM runs WHERE records_purged IS NOT NULL OR files_purged IS NOT NULL")
                }
            else:
                purged = {
                    str(r[0])
                    for r in c.execute("SELECT id FROM runs WHERE files_purged IS NOT NULL")
                }
            queued = {str(r[0]) for r in c.execute("SELECT run_id FROM run_purges")}
            return purged | queued
    except (foundation.NoDatabase, sqlite3.Error):
        return set()


def rebuild_turn_index(run_id: str | None = None, *, step_id: int | None = None,
                       step_ids: set[int] | None = None, missing_only: bool = False) -> int:
    if step_id is not None:
        step_ids = {step_id}
    if not missing_only:
        run_store.clear_turn_index(run_id=run_id, step_id=step_id)
    skip = _purged_turn_runs()
    run_ids = [run_id] if run_id is not None else [p.name for p in run_folders()]
    written = 0
    for rid in run_ids:
        if rid in skip:
            continue
        for path in record_files(rid):
            written += _rebuild_file_turns(path, step_ids=step_ids, missing_only=missing_only)
    return written


def decode_turn(row: dict) -> dict:
    rel = str(row.get("file") or "")
    if not rel or rel.startswith(("/", "~")) or ".." in Path(rel).parts:
        raise ValueError(f"invalid turn record file: {rel!r}")
    state_root = paths.STATE_DIR.resolve()
    target = (state_root / rel).resolve()
    if state_root not in target.parents:
        raise ValueError(f"turn record file escapes state dir: {rel!r}")
    offset = int(row.get("member_offset") or 0)
    length = int(row.get("member_len") or 0)
    with target.open("rb") as f:
        f.seek(offset)
        raw_member = f.read(length)
    decoded = gzip.decompress(raw_member).decode(errors="replace")
    parsed_lines = list(entries(decoded.splitlines()))
    header: dict = {}
    for _, _, hlines in AppendedLines(target).read_members(final=True):
        first = next(entries(hlines), None)
        if first and first.get("arete") == "header":
            header = first
            break
    read = reader_for(header) if header else read_nothing
    ev_list: list[dict] = []
    for item in parsed_lines:
        if not item.get("arete"):
            ev_list.extend(read(item))
    return {
        **row,
        "raw": decoded,
        "lines": parsed_lines,
        "events": ev_list,
    }


def named_model(raw: str) -> str:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "system" and event.get("subtype") == "init":
        return str(event.get("model") or "")
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if message.get("role") != "assistant" or not message.get("model"):
        return ""
    return f"{message['provider']}/{message['model']}" if message.get("provider") else str(message["model"])


GB = 10 ** 9
LIVE_SECONDS = 3600


@dataclass(frozen=True)
class Stored:
    run_id: str
    size: int
    written: float


def scanned(run_id: str, folder: Path) -> Stored:
    size, marks = 0, []

    def failed(error: OSError) -> None:
        raise error

    for root, _, names in os.walk(folder, onerror=failed):
        for name in names:
            info = os.lstat(os.path.join(root, name))
            size += info.st_size
            marks.append(info.st_mtime)
    return Stored(run_id, size, max(marks) if marks else folder.stat().st_mtime)


def stored() -> list[Stored]:
    try:
        runs = sorted(paths.RUNS_DIR.iterdir())
    except OSError:
        return []
    found = []
    for run in runs:
        folder = run / "record"
        if run.is_symlink() or folder.is_symlink() or not folder.is_dir():
            continue
        with contextlib.suppress(OSError):
            found.append(scanned(run.name, folder))
    return sorted(found, key=lambda s: (s.written, s.run_id))


def run_folders() -> list[Path]:
    if not paths.RUNS_DIR.exists():
        return []
    return sorted(p for p in paths.RUNS_DIR.iterdir() if not p.is_symlink() and p.is_dir())


def runs_bytes() -> int:
    return sum(scanned(p.name, p).size for p in run_folders())


def one_component(run_id: str) -> bool:
    return run_id not in ("", ".", "..") and "/" not in run_id and os.sep not in run_id


def pin(run_id: str) -> None:
    if not one_component(run_id):
        raise LookupError(f"no record for run {run_id}")
    run_store.pin_record(run_id, require_record_dir=lambda: paths.record_dir(run_id).is_dir())


def unpin(run_id: str) -> None:
    if not run_store.unpin_record(run_id):
        raise LookupError(f"run {run_id} is not pinned")


def store() -> dict:
    records = stored()
    oldest = records[0] if records else None
    return {"bytes": sum(r.size for r in records), "runs_bytes": runs_bytes(), "records": len(records),
            "oldest": {"run_id": oldest.run_id, "written": util.iso(datetime.fromtimestamp(oldest.written, UTC))}
            if oldest else None,
            "pins": len(run_store.record_pins())}


def size_text(n: int) -> str:
    if n >= GB:
        return f"{n / GB:.2f} GB"
    if n >= 10 ** 6:
        return f"{n / 10 ** 6:.1f} MB"
    if n >= 1000:
        return f"{n / 1000:.1f} KB"
    return util.count(n, "byte")
