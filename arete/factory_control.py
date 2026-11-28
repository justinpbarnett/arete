"""Stop delivery and continuation for work managed by this Arete instance."""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

from . import util
from .store import db, factory
from .store import runs as run_store

starting = threading.Lock()
coordinators = {}
continuation = ContextVar("factory_continuation", default="")
log = logging.getLogger(__name__)


@contextmanager
def activity(name, version=None):
    if factory.generation.get() is not None:
        factory.admit(version=version)
        yield
        return
    if not factory.available():
        yield
        return
    key = f"{name}-{uuid4().hex}"
    with db.tx() as c:
        version = factory.admit(c, version=version)
        factory.register(c, key, os.getpid(), "activity", version=version)
    token = factory.generation.set(version)
    try:
        yield
    finally:
        factory.generation.reset(token)
        factory.forget(key)


def work(fn):
    @wraps(fn)
    def admitted(*args, **kwargs):
        with activity(fn.__name__):
            return fn(*args, **kwargs)
    return admitted


class WorkPool(ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        version = factory.admit()

        def run():
            try:
                with activity(fn.__name__, version):
                    return fn(*args, **kwargs)
            except factory.Off:
                return None

        return super().submit(run)


def spawn(launch, *args, **kwargs):
    """Register the actual child, including calls made on a shared serve thread."""
    if not factory.available():
        return None, launch(*args, **kwargs)
    key = f"child-{uuid4().hex}"
    with db.tx() as c:
        version = factory.admit(c)
        proc = launch(*args, **kwargs)
        try:
            factory.register(c, key, proc.pid, "child", version=version)
        except BaseException:
            proc.terminate()
            raise
    return key, proc


def update(key, phase, reason="", *, previous=None):
    with db.tx() as c:
        expected = factory.generation.get()
        c.execute("UPDATE factory_targets SET phase=?,reason=? WHERE id=? AND (? IS NULL OR phase=?) "
                  "AND (? IS NULL OR ?=(SELECT version FROM factory_state WHERE id=1))",
                  (phase, reason, key, previous, previous, expected, expected))


def request(enabled, base_version):
    result = factory.set_enabled(enabled, base_version)
    if result["version"] > 1:
        if db.in_write():
            db.after_commit(notify)
        else:
            notify()
    return result


def notify():
    with starting:
        for action in (stop_pending, resume_pending):
            pair = coordinators.get(action)
            if pair and pair[0].is_alive():
                pair[1].set()
                continue
            wake = threading.Event()
            thread = threading.Thread(target=monitor, args=(action, wake), name=f"factory-{action.__name__}", daemon=True)
            coordinators[action] = thread, wake
            thread.start()


def monitor(action, wake):
    while True:
        wake.clear()
        try:
            with util.lock(f"factory-{action.__name__}") as owner:
                if owner and factory.available():
                    action()
            pending = factory.available() and factory.view()["phase"] in ("stopping", "resuming")
        except Exception:
            log.exception("Factory control reconciliation failed")
            pending = True
        wake.wait(1 if pending else 20)


def process_matches(row):
    identity = db.child_identity(json.loads(row["identity"]))
    if not util.pid_alive(row["pid"]):
        return False
    if identity is None:
        return None
    current = db.process_identity(row["pid"])
    return current == identity if current is not None else None


def owns_run_lock(run, pid):
    from . import backup
    lock = run and run.get("lock")
    return bool(lock and "/" not in lock and util.lock_is_held(lock) and backup.lock_holder(lock) == str(pid))


def stop_process(key, process, run):
    from . import paths

    pid = process["pid"]
    same = process_matches(process)
    if same is None:
        return "Waiting to verify process identity"
    if same is False:
        return ""
    if process["kind"] == "activity":
        return "Waiting for the in-flight operation to return"
    if process["kind"] == "remote":
        return "Waiting for remote stop acknowledgement"
    services = [util.read_json(path, {}) or {} for path in (paths.SERVER_FILE, paths.SERVE_BEAT_FILE)]
    if pid == os.getpid() or any(pid == service.get("pid") for service in services):
        return "" if run and run["status"] != "running" else "Waiting for the service's worker to stop"
    if process["kind"] == "run" and os.getsid(pid) != pid:
        # Foreground hosts may share a session with a shell. Only signal an attested Arete child.
        with db.reader() as c:
            owned = c.execute("SELECT 1 FROM factory_processes WHERE pid=? AND kind='child' AND identity=?",
                              (pid, process["identity"])).fetchone()
        if not owned and not owns_run_lock(run, pid):
            return "Waiting for the foreground owner to finish or acknowledge"
    # Save independently grouped descendants before the parent can exit and orphan them.
    descendants = util.process_tree(pid)
    with db.tx() as c:
        for member, _ in descendants:
            if member == pid:
                continue
            identity = db.process_identity(member)
            if identity is None:
                continue
            member_key = f"pid-{member}-{identity[0]}-{identity[1]}"
            if c.execute("SELECT 1 FROM factory_processes WHERE id=?", (member_key,)).fetchone():
                continue
            factory.register(c, member_key, member, "descendant", identity=json.dumps(identity))
            c.execute("INSERT OR IGNORE INTO factory_targets(id,generation,phase) VALUES(?,?,'stopping')",
                      (member_key, process["generation"]))
    with db.tx() as c:
        last = c.execute("SELECT MAX(signalled) FROM factory_processes WHERE pid=? AND identity=?",
                         (pid, process["identity"])).fetchone()[0]
        if last and (util.now() - util.parse_iso(last)).total_seconds() < 30:
            return "Stop sent, waiting for exit"
        c.execute("UPDATE factory_processes SET signalled=? WHERE pid=? AND identity=?",
                  (util.iso(), pid, process["identity"]))
    if process_matches(process):
        try:
            stop = signal.SIGKILL if last else signal.SIGTERM
            if os.getpgid(pid) == pid:
                os.killpg(pid, stop)
            else:
                os.kill(pid, stop)
        except ProcessLookupError:
            return ""
    return "Stop sent, waiting for exit"


def stop_target(target):
    key = target["id"]
    with db.reader() as c:
        process = c.execute("SELECT * FROM factory_processes WHERE id=?", (key,)).fetchone()
        run = run_store.run_get(key)
    if process and process["kind"] == "remote":
        from . import device_turns
        if not device_turns.confirm_factory_stop(process):
            return update(key, "stopping", "Remote device has not confirmed its stop", previous="stopping")
    elif process:
        reason = stop_process(key, process, run)
        if reason:
            return update(key, "stopping", reason, previous="stopping")
    elif run and run["status"] == "running" and util.pid_alive(run.get("pid")):
        pid = run["pid"]
        identity = db.process_identity(pid)
        if identity is not None and owns_run_lock(run, pid):
            with db.tx() as c:
                if db.process_identity(pid) == identity and owns_run_lock(run, pid):
                    factory.register(c, key, pid, "run", identity=json.dumps(identity))
        return update(key, "stopping", "Waiting for verified worker registration", previous="stopping")
    elif run and run["status"] == "running" and not run.get("pid"):
        from . import record
        if (util.now() - util.parse_iso(run["started"])).total_seconds() < record.SETTLE_GRACE_SECONDS:
            return update(key, "stopping", "Waiting for launcher registration", previous="stopping")

    with db.tx() as c:
        row = run_store.run_get(key)
        phase, reason = "done", ""
        if row and row["status"] == "running":
            from . import record
            record.settle(key, "interrupted", summary="Paused by the factory switch")
            row = run_store.run_get(key)
        current = c.execute("SELECT cancelled FROM factory_targets WHERE id=?", (key,)).fetchone()
        if row and not current["cancelled"]:
            if row["status"] == "interrupted" or (row["status"] in ("stopped", "failed") and process and process["signalled"]):
                phase = "paused"
        if current["cancelled"] and row and row["status"] == "interrupted":
            from . import record
            record.move(key, "stopped", ("interrupted",), summary="Cancelled by operator", stop_asked=util.iso())
        c.execute("DELETE FROM factory_processes WHERE id=?", (key,))
        update(key, phase, reason, previous="stopping")


def resume_target(target):
    from . import config, control, loops, sessions, slots, tasks
    from .store import loops as loop_store

    key = target["id"]
    row = run_store.run_get(key)
    with db.tx() as c:
        version = factory.admit(c)
        current = c.execute("SELECT * FROM factory_targets WHERE id=?", (key,)).fetchone()
        if not current or current["cancelled"] or not row:
            return update(key, "done")
        if current["phase"] not in ("paused", "starting"):
            return
        update(key, "starting")
    token = continuation.set(key)
    fence = factory.generation.set(version)
    try:
        if row["kind"] == "session":
            sessions.continue_factory_session(key)
        elif row["kind"] == "pipeline":
            cfg = config.load()
            repo = cfg.repo(row["repo"])
            from . import oncall, tracker, zte

            tid = str(row["issue"])
            ticket = tracker.for_ticket(cfg, repo, tid).get(tid)
            if not ticket.open:
                return update(key, "done", "Ticket closed", previous="starting")
            if ticket.blocker or tid in oncall.parked(repo.name):
                return update(key, "held", ticket.blocker or "Ticket is parked by on-call", previous="starting")
            if zte.reserved(tid):
                return update(key, "held", "The ZTE lane owns this ticket", previous="starting")
            if run_store.later_run(repo.name, str(row["issue"]), key):
                return update(key, "held", "Ticket already has a later run")
            from . import factory as workflows, run_lines
            execution = workflows.first(run_lines.read(key), "execution")
            if not repo.pipeline and (row["trigger"] == "poll" or execution.get("from_poll")):
                return update(key, "held", "Ticket intake disabled")
            tasks.resume_factory_run(row)
        elif row["kind"] == "loop":
            loop = loops.find(row["loop"])
            if not loop.enabled or loop.error:
                return update(key, "held", "Loop is disabled or unavailable")
            with db.tx() as c:
                factory.admit(c)
                if loop.kind == "command" and loop.name != "poll":
                    return update(key, "held", "Inspect interrupted command effects before retrying")
                if not c.execute("SELECT 1 FROM queue WHERE run_id=?", (key,)).fetchone():
                    return update(key, "held", "No retained queue input for this loop")
                loop_store.end_claim(c, key, "release", "", count_attempt=False)
                update(key, "continued", "Original queue input released")
        else:
            update(key, "held", "Inspect interrupted work before retrying")
    except (factory.Off, control.Busy, slots.NoFreeSlot):
        update(key, "paused", previous="starting")
    except Exception as error:
        update(key, "held", util.first_line(str(error), 220), previous="starting")
    finally:
        factory.generation.reset(fence)
        continuation.reset(token)


def stop_pending():
    with db.reader() as c:
        state = factory.state(c)
        if state["version"] == 1:
            return
        rows = [dict(r) for r in c.execute("SELECT * FROM factory_targets WHERE phase='stopping' ORDER BY id")]
    def deliver(target):
        try:
            stop_target(target)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            update(target["id"], "stopping", util.first_line(str(error), 220), previous="stopping")
    local = [t for t in rows if not t["id"].startswith("remote-")]
    remote = [t for t in rows if t["id"].startswith("remote-")]
    if remote:
        with ThreadPoolExecutor(max_workers=min(16, len(remote)), thread_name_prefix="device-stop") as pool:
            pending = [pool.submit(deliver, target) for target in remote]
            for target in local:
                deliver(target)
            for done in pending:
                done.result()
    else:
        for target in local:
            deliver(target)


def resume_pending():
    with db.reader() as c:
        if not factory.state(c)["enabled"] or c.execute(
                "SELECT 1 FROM factory_targets WHERE phase='stopping' LIMIT 1").fetchone():
            return
        pending = [dict(r) for r in c.execute("SELECT * FROM factory_targets WHERE phase IN ('paused','starting') "
                                             "ORDER BY generation,id")]
    for target in pending:
        resume_target(target)
