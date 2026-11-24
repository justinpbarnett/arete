from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .. import util
from .db import WORKER_EXCHANGE_SCHEMA, migrate_on_demand, migration_applied, reader, tx

TABLES = {"orchestrations": "run lineage", "orchestration_turns": "run lineage",
          "orchestration_actions": "run lineage", "orchestration_workers": "run lineage",
          "orchestration_messages": "run lineage"}
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted", "reconciliation-required")
UNCHANGED_WAIT = "accepted-unchanged-wait"


class Conflict(ValueError):
    pass


class Limit(Conflict):
    pass


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def decoded(row) -> dict:
    if row is None:
        return {}
    found = dict(row)
    for key in ("data", "request", "result", "response"):
        if key in found:
            found[key] = json.loads(found[key])
    return found


@dataclass(frozen=True)
class Lease:
    root: str
    run: str
    generation: int


def find(run_id: str, connect=reader) -> dict:
    if not run_id:
        return {}
    with connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orchestrations'").fetchone():
            return {}
        return decoded(lookup(c, run_id))


def lookup(c, run_id):
    return c.execute("SELECT * FROM orchestrations WHERE root = ? OR current_run = ? OR root IN "
                     "(SELECT root FROM orchestration_turns WHERE run = ?) OR EXISTS "
                     "(SELECT 1 FROM json_each(orchestrations.data, '$.runs') WHERE value = ?)",
                     (run_id, run_id, run_id, run_id)).fetchone()


def acquire(run_id: str, previous: str, data: dict) -> Lease:
    if data.get("protocol") == 2:
        migrate_on_demand(WORKER_EXCHANGE_SCHEMA)
    with tx() as c:
        old = lookup(c, previous) if previous else None
        if previous and old is None:
            raise Conflict("the continued run has no durable orchestration state")
        if old and old["current_run"] == run_id:
            raise Conflict("this run already admitted a controller")
        if old:
            row = c.execute("SELECT status FROM runs WHERE id = ?", (old["current_run"],)).fetchone()
            if row is None or row["status"] == "running":
                raise Conflict("the previous controller has not been settled")
        root, generation = (old["root"], old["generation"] + 1) if old else (run_id, 1)
        data = decoded(old)["data"] if old else data
        data["runs"] = list(dict.fromkeys([*data.get("runs", [root]), run_id]))
        if old:
            c.execute("UPDATE orchestrations SET current_run = ?, generation = ?, owner = ?, data = ?, at = ? WHERE root = ?",
                      (run_id, generation, os.getpid(), encoded(data), util.iso(), root))
        else:
            c.execute("INSERT INTO orchestrations VALUES (?,?,?,?,0,0,0,?,?)",
                      (root, run_id, generation, os.getpid(), encoded(data), util.iso()))
        return Lease(root, run_id, generation)


def fenced(c, lease: Lease):
    row = c.execute("SELECT * FROM orchestrations WHERE root = ? AND current_run = ? AND generation = ?",
                    (lease.root, lease.run, lease.generation)).fetchone()
    if row is None:
        raise Conflict("stale controller generation")
    return row


def snapshot(lease: Lease) -> dict:
    with reader() as c:
        row = decoded(fenced(c, lease))
        row["turns"] -= unchanged_waits(c, lease.root)
        return row


def unchanged_waits(c, root: str) -> int:
    return c.execute("SELECT count(*) FROM orchestration_turns WHERE root = ? AND state = ?",
                     (root, UNCHANGED_WAIT)).fetchone()[0]


def save(lease: Lease, data: dict) -> None:
    with tx() as c:
        fenced(c, lease)
        c.execute("UPDATE orchestrations SET data = ?, version = version + 1, at = ? WHERE root = ?",
                  (encoded(data), util.iso(), lease.root))


def begin(lease: Lease, observation: dict, maximum: int | None) -> str:
    with tx() as c:
        row = fenced(c, lease)
        if maximum is not None and row["turns"] - unchanged_waits(c, lease.root) >= maximum:
            raise Limit(f"controller used its {maximum} decision allowance")
        turn = f"d{row['turns'] + 1:04d}"
        c.execute("INSERT INTO orchestration_turns VALUES (?,?,?,?,?,?,?)",
                  (lease.root, turn, lease.run, digest(observation), "null", "pending", util.iso()))
        c.execute("UPDATE orchestrations SET turns = turns + 1 WHERE root = ?", (lease.root,))
        return turn


def turn_started(lease: Lease, turn: str, pid: int, identity: int | None) -> None:
    with tx() as c:
        fenced(c, lease)
        c.execute("UPDATE orchestration_turns SET response = ? WHERE root = ? AND id = ? AND state = 'pending'",
                  (encoded({"pid": pid, "identity": identity}), lease.root, turn))


def pending(lease: Lease) -> list[dict]:
    with reader() as c:
        fenced(c, lease)
        return [decoded(row) for row in c.execute(
            "SELECT * FROM orchestration_turns WHERE root = ? AND state = 'pending'", (lease.root,))]


def launched(lease: Lease, maximum: int | None) -> None:
    with tx() as c:
        row = fenced(c, lease)
        if maximum is not None and row["launches"] - unchanged_waits(c, lease.root) >= maximum:
            raise Limit(f"controller used its {maximum} inference-launch allowance")
        c.execute("UPDATE orchestrations SET launches = launches + 1 WHERE root = ?", (lease.root,))


def reject(lease: Lease, turn: str, error: str) -> None:
    with tx() as c:
        fenced(c, lease)
        c.execute("UPDATE orchestration_turns SET state = 'rejected', response = ? WHERE root = ? AND id = ?",
                  (encoded({"error": error}), lease.root, turn))


def accept(lease: Lease, turn: str, version: int, response: dict, maximum: int | None) -> list[dict]:
    body = encoded(response)
    with tx() as c:
        current = fenced(c, lease)
        earlier = c.execute("SELECT * FROM orchestration_turns WHERE root = ? AND id = ?",
                            (lease.root, turn)).fetchone()
        if earlier is None:
            raise Conflict("unknown controller decision")
        if earlier["state"] in ("accepted", UNCHANGED_WAIT):
            if earlier["response"] != body:
                raise Conflict("decision identity reused with different content")
            return [decoded(r) for r in c.execute(
                "SELECT * FROM orchestration_actions WHERE root = ? AND turn = ? ORDER BY rowid", (lease.root, turn))]
        if earlier["state"] != "pending" or current["version"] != version:
            raise Conflict("stale decision state")
        if maximum is not None:
            count = c.execute("SELECT count(*) FROM orchestration_actions WHERE root = ?", (lease.root,)).fetchone()[0]
            if count + len(response["calls"]) > maximum:
                raise Limit(f"run used its {maximum} action allowance")
        for n, call in enumerate(response["calls"]):
            c.execute("INSERT INTO orchestration_actions VALUES (?,?,?,?,?,'accepted',?,?,?)",
                      (lease.root, f"{turn}-{n}", turn, lease.run, call["op"], encoded(call["args"]),
                       encoded({"revision": current["version"] + 1}), util.iso()))
        c.execute("UPDATE orchestration_turns SET response = ?, state = 'accepted' WHERE root = ? AND id = ?",
                  (body, lease.root, turn))
        c.execute("UPDATE orchestrations SET version = version + 1, at = ? WHERE root = ?", (util.iso(), lease.root))
        return [decoded(r) for r in c.execute(
            "SELECT * FROM orchestration_actions WHERE root = ? AND turn = ? ORDER BY rowid", (lease.root, turn))]


def actions(lease: Lease, connect=reader) -> list[dict]:
    with connect() as c:
        fenced(c, lease)
        return [decoded(r) for r in c.execute("SELECT * FROM orchestration_actions WHERE root = ? ORDER BY rowid",
                                             (lease.root,))]


def unlaunched_start(action):
    return action["op"] == "worker.start" and "depends_on" in action.get("result", {})


def queued_start(action):
    return action["state"] == "accepted" and unlaunched_start(action)


def transition(lease: Lease, action: str, state: str, result: dict, expected=("accepted", "running")) -> dict:
    with tx() as c:
        return transition_in(c, lease, action, state, result, expected)


def transition_in(c, lease, action, state, result, expected=("accepted", "running")):
    current = fenced(c, lease)
    result = {**result, "revision": current["version"] + 1}
    row = c.execute("SELECT * FROM orchestration_actions WHERE root = ? AND id = ?",
                    (lease.root, action)).fetchone()
    if row is None or row["state"] not in expected:
        raise Conflict(f"action {action} is not in an expected state")
    kept = decoded(row)["result"]
    retained = ("pid", "identity", "writing", "role", "worker_id", "active_builders", "input_stamp", "retry_of")
    result = {**{key: kept[key] for key in retained if key in kept},
              **result}
    c.execute("UPDATE orchestration_actions SET state = ?, result = ?, at = ? WHERE root = ? AND id = ?",
              (state, encoded(result), util.iso(), lease.root, action))
    if (row["op"] == "run.wait" and row["state"] == "running" and state == "succeeded"
            and result.get("reason") == "timeout" and result.get("active")):
        c.execute("UPDATE orchestration_turns SET state = ? WHERE root = ? AND id = ? AND state = 'accepted' "
                  "AND (SELECT count(*) FROM orchestration_actions WHERE root = ? AND turn = ?) = 1",
                  (UNCHANGED_WAIT, lease.root, row["turn"], lease.root, row["turn"]))
    c.execute("UPDATE orchestrations SET version = version + 1, at = ? WHERE root = ?", (util.iso(), lease.root))
    return {**decoded(row), "state": state, "result": result}


def view(run_id: str, connect=reader, *, messages_after=0) -> dict | None:
    with connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orchestrations'").fetchone():
            return None
        found = decoded(lookup(c, run_id))
        if not found:
            return None
        actions = [decoded(row) for row in c.execute(
            "SELECT * FROM orchestration_actions WHERE root = ? ORDER BY rowid", (found["root"],))]
        details = {}
        if found["data"].get("protocol") == 2:
            from . import worker_exchange

            lease = Lease(found["root"], found["current_run"], found["generation"])
            for action in actions:
                action["request"] = worker_exchange.public_text(action["request"])
                action["result"] = worker_exchange.public_text(action["result"])
            details = worker_exchange.projection(c, lease, found["data"].get("limits", {}), after=messages_after)
        return {**details, "root": found["root"], "current_run": found["current_run"], "generation": found["generation"],
                "mode": found["data"].get("mode", "orchestrate"),
                "version": found["version"], "decisions": found["turns"] - unchanged_waits(c, found["root"]),
                "launches": found["launches"],
                "actions": [{key: action[key] for key in ("id", "run", "op", "state", "at", "request", "result")}
                            for action in actions]}


def catch_up_push(data: dict) -> dict:
    intent = data.get("publication_intent") or {}
    saved = intent.get("catch_up") or {}
    if (intent.get("phase") == "publish" and data.get("mode") == "orchestrate"
            and {"action", "pr", "previous", "head", "base", "verified", "review"} <= saved.keys()):
        return saved
    return {}


def publication_pending(run_id: str) -> bool:
    found = find(run_id)
    if not found:
        return False
    if found["data"].get("publication_intent"):
        return True
    with reader() as c:
        return c.execute("SELECT 1 FROM orchestration_actions WHERE root = ? AND op = 'publication.request' "
                         "AND state IN ('running', 'reconciliation-required') LIMIT 1", (found["root"],)).fetchone() is not None


def purge(c, run_id: str) -> None:
    row = c.execute("SELECT root, current_run FROM orchestrations WHERE root = ? OR current_run = ?",
                    (run_id, run_id)).fetchone()
    if row and not c.execute("SELECT 1 FROM runs WHERE id = ? AND files_purged IS NULL",
                              (row["current_run"],)).fetchone():
        tables = ["orchestration_actions", "orchestration_turns", "orchestrations"]
        if migration_applied(WORKER_EXCHANGE_SCHEMA, c):
            from .. import builder_workspaces

            for worker in c.execute("SELECT data FROM orchestration_workers WHERE root=?", (row["root"],)):
                saved = json.loads(worker["data"])
                for candidate in [*saved.get("retained", []), saved.get("candidate")]:
                    if candidate:
                        builder_workspaces.release(candidate)
            tables[:0] = ["orchestration_messages", "orchestration_workers"]
        for table in tables:
            c.execute(f"DELETE FROM {table} WHERE root = ?", (row["root"],))
