"""Instance-wide work admission and interruption ownership."""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from functools import lru_cache

from .. import paths, principals, refusals, util
from . import db, events
from ._rows import validate_identifier

TABLES = {"factory_state": "kept", "factory_targets": "kept", "factory_processes": "until settled",
          "session_checkpoints": "until session ends"}
generation = ContextVar("factory_generation", default=None)
CONTINUATION = "ARETE_FACTORY_CONTINUATION"
LAUNCH_VERSION = "ARETE_FACTORY_LAUNCH_VERSION"


class Off(refusals.Conflict, RuntimeError):
    code = "factory_off"

    def __init__(self):
        super().__init__("The factory has stopped admitting work.")


class Changed(refusals.Conflict):
    code = "factory_version_conflict"

    def __init__(self, current):
        super().__init__("The factory setting changed. Refresh before trying again.")
        self.current = current


def installed(c):
    return c.execute("PRAGMA user_version").fetchone()[0] >= db.FACTORY_SCHEMA


def uninitialized():
    if (paths.DB_FILE.exists() or paths.SERVE_BEAT_FILE.exists() or os.environ.get(LAUNCH_VERSION)
            or generation.get() is not None):
        return False
    db.refuse_empty_file()
    return True


def available():
    try:
        with db.reader() as c:
            return installed(c)
    except db.NoDatabase:
        if uninitialized():
            return False
        raise


def state(c):
    if not installed(c):
        raise db.IncompatibleSchema("Factory control requires schema 31. Run arete serve or arete migrate.")
    row = c.execute("SELECT * FROM factory_state WHERE id=1").fetchone()
    if row is None:
        raise RuntimeError("Factory control state is missing")
    return row


def check_worker():
    """Fence an already admitted operation before another external side effect."""
    if generation.get() is not None or os.environ.get(LAUNCH_VERSION):
        admit()


def admit(c=None, *, version=None):
    if c is None:
        try:
            with db.reader() as connection:
                return admit(connection, version=version)
        except db.NoDatabase:
            if uninitialized():
                return 1
            raise
    if not installed(c):
        if version not in (None, 1) or generation.get() not in (None, 1) or os.getenv(LAUNCH_VERSION, "1") != "1":
            raise Off()
        return 1
    row = state(c)
    expected = generation.get() if version is None else version
    if expected is None and os.getenv(LAUNCH_VERSION):
        expected = int(os.environ[LAUNCH_VERSION])
    if not row["enabled"] or (expected is not None and expected != row["version"]):
        raise Off()
    if row["version"] > 1 and c.execute("SELECT 1 FROM factory_targets WHERE phase='stopping' LIMIT 1").fetchone():
        raise Off()
    return row["version"]


def view(c=None):
    if c is None:
        with db.reader() as connection:
            return view(connection)
    row = state(c)
    targets = [dict(r) for r in c.execute(
        "SELECT id,phase,reason,successor FROM factory_targets WHERE phase IN ('stopping','paused','starting','held') "
        "ORDER BY id")]
    stopping = sum(t["phase"] == "stopping" for t in targets)
    paused = sum(t["phase"] in ("paused", "starting") for t in targets)
    enabled = bool(row["enabled"])
    phase = "stopping" if stopping else "resuming" if enabled and paused else "on" if enabled else "off"
    return {"enabled": enabled, "version": row["version"], "phase": phase, "actor": row["actor"],
            "changed": row["changed"], "unconfirmed": stopping, "paused": paused, "targets": targets}

def set_enabled(enabled, base_version):
    with db.tx() as c:
        row = state(c)
        if row["version"] != base_version:
            raise Changed(view(c))
        if bool(row["enabled"]) != enabled:
            version, now = base_version + 1, util.iso()
            c.execute("UPDATE factory_state SET enabled=?,version=?,actor=?,changed=?,"
                      "resumed=CASE WHEN ? THEN ? ELSE resumed END WHERE id=1",
                      (enabled, version, principals.current(), now, enabled, now))
            if not enabled:
                # A worker can finish, be cancelled, or receive a PID after this transaction.
                c.execute("INSERT INTO factory_targets(id,generation,phase,cancelled) "
                          "SELECT id,?, 'stopping', stop_asked IS NOT NULL AND stop_asked != '' "
                          "FROM runs WHERE status='running' "
                          "ON CONFLICT(id) DO UPDATE SET generation=excluded.generation, phase='stopping', "
                          "cancelled=MAX(factory_targets.cancelled,excluded.cancelled), reason=''", (version,))
                c.execute("INSERT INTO factory_targets(id,generation,phase) "
                          "SELECT id,?, 'stopping' FROM factory_processes WHERE true "
                          "ON CONFLICT(id) DO UPDATE SET generation=excluded.generation, phase='stopping', reason=''",
                          (version,))
            events.emit("factory.changed", "factory:state", cls="durable", ver=version,
                        data={"enabled": enabled})
        return view(c)


@lru_cache(maxsize=32)
def own_identity(pid):
    identity = db.process_identity(pid)
    if identity is None:
        raise RuntimeError("Cannot establish the factory worker's process identity")
    return json.dumps(identity)


def register(c, key, pid, kind, *, identity=None, detail="", version=None):
    if not pid or not installed(c):
        return
    identity = identity or (own_identity(pid) if pid == os.getpid() else json.dumps(db.process_identity(pid)))
    c.execute("INSERT INTO factory_processes(id,pid,identity,kind,generation,detail) VALUES(?,?,?,?,?,?) "
              "ON CONFLICT(id) DO UPDATE SET pid=excluded.pid,identity=excluded.identity,kind=excluded.kind,"
              "generation=excluded.generation,detail=excluded.detail, "
              "signalled=CASE WHEN identity=excluded.identity THEN signalled ELSE '' END",
              (key, pid, identity, kind, state(c)["version"] if version is None else version, detail))


def forget(key):
    if not key:
        return
    with db.tx() as c:
        if installed(c):
            c.execute("DELETE FROM factory_processes WHERE id=?", (key,))


def interrupted(run, c=None):
    if c is None:
        with db.reader() as conn:
            return interrupted(run, conn)
    if not installed(c):
        return None
    return c.execute("SELECT * FROM factory_targets WHERE id=? AND phase IN ('stopping','paused','starting')",
                     (run,)).fetchone()


def exclude_retained(c, run_column):
    validate_identifier(run_column, qualified=True)
    if not installed(c):
        return ""
    return (f" AND NOT EXISTS (SELECT 1 FROM factory_targets f WHERE f.id={run_column} "
            "AND f.phase IN ('stopping','paused','starting','held')) ")


def held_tickets(repo):
    if not available():
        return set()
    with db.reader() as c:
        rows = c.execute("SELECT r.issue FROM factory_targets f JOIN runs r ON r.id=f.id "
                         "WHERE r.repo=? AND r.kind='pipeline' AND (f.cancelled OR f.phase IN ('paused','starting','held')) "
                         "AND NOT EXISTS (SELECT 1 FROM runs newer WHERE newer.repo=r.repo AND newer.issue=r.issue "
                         "AND newer.kind='pipeline' AND newer.rowid>r.rowid)", (repo,))
        return {str(row[0]) for row in rows}


def retained(run):
    with db.reader() as c:
        return installed(c) and c.execute(
            "SELECT 1 FROM factory_targets WHERE id=? AND phase IN ('stopping','paused','starting','held')",
            (run,)).fetchone() is not None


def target(c, key, phase, reason="", successor=""):
    c.execute("UPDATE factory_targets SET phase=?,reason=?,successor=? WHERE id=?",
              (phase, reason, successor, key))


def registered_run(c, run, row):
    if not installed(c):
        return
    register(c, run, row.get("pid"), "run")
    parent = os.getenv(CONTINUATION)
    if parent and row["kind"] == "pipeline":
        linked = c.execute("UPDATE factory_targets SET phase='continued',successor=?,reason='' "
                  "WHERE id=? AND phase='starting' AND cancelled=0 AND EXISTS ("
                  "SELECT 1 FROM runs old JOIN runs new ON new.id=? AND old.kind=new.kind AND old.repo=new.repo "
                  "AND old.issue=new.issue WHERE old.id=factory_targets.id)", (run, parent, run))
        if not linked.rowcount:
            raise Off()


def previous_run(run_id):
    with db.reader() as c:
        row = c.execute("SELECT id FROM factory_targets WHERE successor=? AND phase='continued'", (run_id,)).fetchone()
        return row["id"] if row else None


def finish_run(c, run):
    if not installed(c):
        return
    c.execute("DELETE FROM factory_processes WHERE id=? AND NOT EXISTS "
              "(SELECT 1 FROM factory_targets WHERE id=? AND phase='stopping')", (run, run))
