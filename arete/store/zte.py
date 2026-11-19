from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from .. import util
from . import db, tasks

TABLES = {"zte_deliveries": "kept"}
TERMINAL = frozenset({"healthy", "held", "uncertain", "rolled_back"})
NEXT = {
    "queued": {"dispatching"},
    "dispatching": {"running"},
    "running": {"prepared"},
    "prepared": {"push_intent"},
    "push_intent": {"pushed"},
    "pushed": {"pr_intent"},
    "pr_intent": {"pr_ready"},
    "pr_ready": {"merge_intent"},
    "merge_intent": {"merged"},
    "merged": {"release_ready"},
    "release_ready": {"deploy_intent"},
    "deploy_intent": {"healthy", "rollback_intent"},
    "rollback_intent": {"rolled_back"},
}


class Conflict(RuntimeError):
    pass


@dataclass(frozen=True)
class Delivery:
    task: str
    policy: str
    repo: str
    base: str
    created: str
    deadline: str
    phase: str
    version: int
    data: dict
    conformed: str


def decode(row) -> Delivery | None:
    return Delivery(**{**dict(row), "data": json.loads(row["data"])}) if row else None


def latest() -> Delivery | None:
    with db.one_read() as c:
        return decode(c.execute("SELECT * FROM zte_deliveries ORDER BY rowid DESC LIMIT 1").fetchone())


def find(tid: str) -> Delivery | None:
    with db.one_read() as c:
        return decode(c.execute("SELECT * FROM zte_deliveries WHERE task = ?", (tid,)).fetchone())


def activation_pending() -> bool:
    with db.one_read() as c:
        if c.execute("PRAGMA user_version").fetchone()[0] < 12:
            return False
        row = c.execute("SELECT phase, json_extract(data, '$.stopped_at') AS stopped_at "
                        "FROM zte_deliveries ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            return False
        activating = {"deploy_intent", "rollback_intent"}
        return row["phase"] in activating or (row["phase"] == "uncertain" and row["stopped_at"] in activating)


def note_conforming(delivery: Delivery, observed_sha: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE zte_deliveries SET conformed = ?, version = version + 1 "
                  "WHERE task = ? AND phase = 'healthy' AND version = ? AND conformed = ''",
                  (observed_sha, delivery.task, delivery.version))


def reserve(policy: str, repo: str, base: str, deadline: str, title: str, kind: str, body: str,
            data: dict) -> Delivery | None:
    now = util.iso()
    with db.tx() as c:
        previous = latest()
        if previous and not (previous.phase == "healthy" and previous.policy == policy and previous.conformed and previous.base != base
                             and previous.conformed != base and previous.created[:10] != now[:10]):
            return None
        tid = "t-" + secrets.token_hex(20)
        tasks.task_add(repo, body, title=title, kind=kind, reserved_id=tid)
        c.execute("INSERT INTO zte_deliveries (task, policy, repo, base, created, deadline, phase, data) "
                  "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                  (tid, policy, repo, base, now, deadline, json.dumps(data, sort_keys=True)))
        return find(tid)


def transition(delivery: Delivery, phase: str, **data) -> Delivery:
    if phase not in NEXT.get(delivery.phase, set()) and not (
            delivery.phase not in TERMINAL and phase in {"held", "uncertain"}):
        raise Conflict(f"ZTE delivery cannot transition from {delivery.phase} to {phase}")
    return amend(delivery, phase, **data)


def annotate(delivery: Delivery, **data) -> Delivery:
    if delivery.phase in TERMINAL:
        raise Conflict("terminal ZTE evidence is closed")
    return amend(delivery, delivery.phase, **data)


def amend(delivery: Delivery, phase: str, **data) -> Delivery:
    if data.keys() & delivery.data.keys():
        raise Conflict("ZTE evidence is append-only")
    with db.tx() as c:
        changed = c.execute("UPDATE zte_deliveries SET phase = ?, version = version + 1, data = ? "
                            "WHERE task = ? AND version = ? AND phase = ?",
                            (phase, json.dumps({**delivery.data, **data}, sort_keys=True), delivery.task,
                             delivery.version, delivery.phase))
        if changed.rowcount != 1:
            raise Conflict("another controller already moved the ZTE delivery")
        return find(delivery.task)


def complete(delivery: Delivery, task_version: int, **data) -> Delivery:
    with db.tx():
        if not tasks.task_set_state(delivery.task, "done", from_states=("pr",), base_version=task_version):
            raise Conflict("ZTE task changed before its healthy release was recorded")
        return transition(delivery, "healthy", **data)
