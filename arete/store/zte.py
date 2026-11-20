from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime

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
        row = c.execute("SELECT phase, json_extract(data, '$.stopped_at') AS stopped_at, "
                        "json_extract(data, '$.completion_health') IS NOT NULL AS runtime_validated "
                        "FROM zte_deliveries ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            return False
        phase = row["stopped_at"] if row["phase"] == "uncertain" else row["phase"]
        return phase == "rollback_intent" or (phase == "deploy_intent" and not row["runtime_validated"])


def note_conforming(delivery: Delivery, observed_sha: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE zte_deliveries SET conformed = ?, version = version + 1 "
                  "WHERE task = ? AND phase = 'healthy' AND version = ? AND conformed = ''",
                  (observed_sha, delivery.task, delivery.version))


def ledger_time(value: object) -> datetime | None:
    if type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        try:
            return util.parse_iso(value)
        except ValueError:
            pass
    return None


def pre_pr_hold(delivery: Delivery | None) -> dict | None:
    if not delivery or delivery.phase != "held" or not isinstance(delivery.data, dict):
        return None
    data = delivery.data
    branch, head = data.get("branch"), data.get("head")
    issue = data.get("issue")
    created, deadline, pushed = map(ledger_time, (delivery.created, delivery.deadline, data.get("push_sent_after")))
    unexpected = {"pr", "pr_node", "pr_url", "pr_sent_after", "merge_sent_after", "release", "activation_after",
                  "issue_close_sent_after", "hold_reason"}
    if (not (created and deadline and pushed and created <= pushed < deadline)
            or data.get("stopped_at") != "pushed" or data.get("reason") != "the issue is not in the dedicated delivery queue"
            or data.get("production_issue_base") != delivery.base or data.get("close_after_health") is not True
            or unexpected & data.keys() or type(delivery.task) is not str
            or not re.fullmatch(r"t-[0-9a-f]{40}", delivery.task)
            or type(delivery.version) is not int or delivery.version < 1
            or type(delivery.base) is not str or not re.fullmatch(r"[0-9a-f]{40}", delivery.base)
            or type(branch) is not str or not re.fullmatch(rf"arete/{delivery.task}-[a-z0-9-]+", branch)
            or type(head) is not str or not re.fullmatch(r"[0-9a-f]{40}", head)
            or not isinstance(issue, dict) or issue.keys() != {"number", "node", "revision"}
            or type(issue["number"]) is not int or not 0 < issue["number"] < 2**31
            or type(issue["node"]) is not str or not 0 < len(issue["node"]) <= 128
            or type(issue["revision"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", issue["revision"])):
        return None
    return {"task": delivery.task, "version": delivery.version, "branch": branch, "head": head}


def recovery_ready(delivery: Delivery, now: datetime) -> bool:
    created, deadline = map(ledger_time, (delivery.created, delivery.deadline))
    return bool(created and deadline and now >= deadline and now.date() > created.date())


def same_installation(delivery: Delivery, repo: str, data: dict) -> bool:
    return delivery.repo == repo and all(data.get(key) and data[key] == delivery.data.get(key)
                                         for key in ("github", "approved_installation"))


def reserve(policy: str, repo: str, base: str, deadline: str, title: str, kind: str, body: str,
            data: dict) -> Delivery | None:
    with db.tx() as c:
        now = util.now()
        previous = latest()
        issue = data.get("issue")
        pinned = bool(issue and data.get("production_issue_base") == base and data.get("close_after_health") is True)
        predecessor = previous
        if recovery := data.get("reconciled_hold"):
            if not (pinned and recovery == pre_pr_hold(previous) and same_installation(previous, repo, data)
                    and recovery_ready(previous, now) and base != previous.base
                    and all(issue.get(key) and issue[key] != previous.data["issue"][key] for key in ("number", "node"))):
                return None
            predecessor = decode(c.execute("SELECT * FROM zte_deliveries WHERE phase = 'healthy' "
                                          "ORDER BY rowid DESC LIMIT 1").fetchone())
            if predecessor is None or not same_installation(predecessor, repo, data):
                return None
        if previous and (previous.created[:10] == now.date().isoformat() or predecessor.phase != "healthy"
                         or not (predecessor.policy == policy or (pinned and same_installation(predecessor, repo, data)))
                         or not predecessor.conformed or base in (predecessor.base, predecessor.conformed)):
            return None
        if issue:
            used = c.execute("SELECT 1 FROM zte_deliveries WHERE json_extract(data, '$.github.number') = ? "
                             "AND json_extract(data, '$.issue.node') = ? LIMIT 1",
                             (data["github"]["number"], issue["node"])).fetchone()
            if used:
                return None
        tid = "t-" + secrets.token_hex(20)
        tasks.task_add(repo, body, title=title, kind=kind, reserved_id=tid)
        c.execute("INSERT INTO zte_deliveries (task, policy, repo, base, created, deadline, phase, data) "
                  "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                  (tid, policy, repo, base, util.iso(now), deadline, json.dumps(data, sort_keys=True)))
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
    if delivery.data.get("close_after_health") and not (
            delivery.data.get("issue_close_sent_after") and data.get("issue_closure")):
        raise Conflict("the reserved issue has no observed completion")
    with db.tx():
        if not tasks.task_set_state(delivery.task, "done", from_states=("pr",), base_version=task_version):
            raise Conflict("ZTE task changed before its healthy release was recorded")
        return transition(delivery, "healthy", **data)
