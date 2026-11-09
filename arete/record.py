from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager

from . import state, util

SETTLE_GRACE_SECONDS = 60
INTERRUPTED = "stopped before it finished (the Mac restarted, lost power or the process was killed)"
STOPPED = "stopped by request"
ID_PREFIX = {"pipeline": "pip", "miner": "min", "eval": "eva"}
FAILS_WHEN_DEAD = ("miner", "eval")
TICKET_KINDS = ("pipeline",)


def new_id(prefix: str, tag: str = "") -> str:
    stamp = util.now().strftime("%Y%m%d-%H%M%S")
    return "-".join(filter(None, [prefix.replace("/", "."), stamp, os.urandom(2).hex(), tag]))


def start(kind: str, *, run_id: str = "", tag: str = "", lock: str = "", **fields) -> str:
    row = {"kind": kind, "status": "running", "started": util.iso(), "lock": lock or None, "pid": os.getpid(),
           **fields}
    cols, marks = ", ".join(row), ", ".join("?" * len(row))
    while True:
        minted = run_id or new_id(ID_PREFIX.get(kind, kind), tag)
        try:
            with state.db() as c:
                c.execute(f"INSERT INTO runs (id, {cols}) VALUES (?, {marks})", (minted, *row.values()))
            return minted
        except sqlite3.IntegrityError:
            if run_id:
                raise


def step(run_id: str, name: str, res, seconds: float) -> None:
    with state.db() as c:
        c.execute("INSERT INTO steps (run_id, step, runner, model, provider, ok, cost_usd, tokens_in, tokens_out, turns,"
                  " seconds, error, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (run_id, name, res.runner, res.model, res.provider, int(res.ok), res.cost_usd, res.tokens_in,
                   res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], util.iso()))
        c.execute("UPDATE runs SET cost_usd = COALESCE(cost_usd, 0) + ?, tokens_in = COALESCE(tokens_in, 0) + ?, "
                  "tokens_out = COALESCE(tokens_out, 0) + ? WHERE id = ?",
                  (res.cost_usd, res.tokens_in, res.tokens_out, run_id))


def spent(run_id: str) -> float:
    r = state.run_get(run_id)
    return round(r["cost_usd"] or 0, 6) if r else 0.0


def metered_spent(run_id: str) -> float:
    with state.db() as c:
        total = c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM steps WHERE run_id = ? "
                          "AND COALESCE(runner, '') != 'claude'", (run_id,)).fetchone()[0]
    return round(float(total), 6)


def note(run_id: str, **fields) -> None:
    with state.db() as c:
        c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), run_id))


def ask_stop(run_id: str) -> dict | None:
    with state.db() as c:
        c.execute("UPDATE runs SET stop_asked = ? WHERE id = ? AND status = 'running'", (util.iso(), run_id))
        r = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(r) if r else None


def stop_asked(run_id: str) -> bool:
    r = state.run_get(run_id)
    return bool(r and r["stop_asked"])


def settle(run_id: str, status: str, end_line: str = "", **fields) -> dict | None:
    fields = {"status": status, "finished": util.iso(), **fields}
    with state.db() as c:
        if not c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ? AND status = 'running'",
                         (*fields.values(), run_id)).rowcount:
            return None
        if end_line:
            state.insert_feed(c, run_id, "end", end_line)
        return dict(c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


@contextmanager
def failed_on_crash(run_id: str):
    try:
        yield
    except BaseException as e:
        why = util.first_line(f"crashed: {type(e).__name__}: {e}", 300)
        settle(run_id, "failed", end_line=f"failed: {why}", summary=why)
        raise


def dead_status(row: dict) -> str:
    if row.get("stop_asked"):
        return "stopped"
    if row["kind"] == "pipeline":
        earlier = [r for r in state.ticket_runs(row["repo"], row["issue"], limit=3) if r["id"] != row["id"]]
        return "failed" if earlier and earlier[0]["status"] == "interrupted" else "interrupted"
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


def interrupt(row: dict) -> dict | None:
    if not util.pid_alive(row.get("pid")):
        util.end_session(row.get("pid"))
    status = dead_status(row)
    summary = STOPPED if status == "stopped" else INTERRUPTED
    return settle(row["id"], status, end_line=f"{status}: {summary}", summary=summary)


def reap(*, tickets: bool, then: Callable[[dict], None] = lambda row: None) -> list[dict]:
    settled = []
    for row in state.runs_marked_running():
        if (row["kind"] in TICKET_KINDS) != tickets or (util.now() - util.parse_iso(row["started"])).total_seconds() < SETTLE_GRACE_SECONDS:
            continue
        with dead(row) as gone:
            if gone and (done := interrupt(row)):
                then(done)
                settled.append(done)
    return settled
