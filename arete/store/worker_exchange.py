"""Fenced turn, inbox and integration receipts for protocol 2."""
from __future__ import annotations

import json

from .. import outbound, util
from . import orchestration as ledger
from .db import reader, tx

CLOSED = {"completed", "integrated", "cancelled"}
MAX_INBOX = 20


def workers(lease, c=None):
    if c is None:
        with reader() as conn:
            return workers(lease, conn)
    ledger.fenced(c, lease)
    return {row["id"]: ledger.decoded(row) for row in c.execute(
        "SELECT * FROM orchestration_workers WHERE root=? ORDER BY id", (lease.root,))}


def get(c, lease, worker):
    ledger.fenced(c, lease)
    row = c.execute("SELECT * FROM orchestration_workers WHERE root=? AND id=?", (lease.root, worker)).fetchone()
    if row is None:
        raise ValueError(f"unknown logical worker {worker}")
    return ledger.decoded(row)


def save(c, lease, w, phase):
    ledger.fenced(c, lease)
    c.execute("UPDATE orchestration_workers SET attempt=?, generation=?, phase=?, data=? WHERE root=? AND id=?",
              (w["attempt"], lease.generation, phase, ledger.encoded(w["data"]), lease.root, w["id"]))


def assign(lease, action, data):
    with tx() as c:
        ledger.fenced(c, lease)
        c.execute("INSERT INTO orchestration_workers VALUES (?,?,?,?,?,?)",
                  (lease.root, action, action, lease.generation, "assigned", ledger.encoded(data)))


def message_id(lease, action, kind):
    return f"{kind}-{ledger.digest([lease.root, action, kind])[:20]}"


def add(c, lease, w, action, kind, body, reply_to="", state="queued"):
    mid = message_id(lease, action, kind)
    event = {"state": state, "action": action, "at": util.iso()}
    c.execute("INSERT INTO orchestration_messages(root,id,worker,kind,body,reply_to,action,state,attempt,history)"
              " VALUES(?,?,?,?,?,?,?,?,?,?)", (lease.root, mid, w["id"], kind, body, reply_to, action, state,
                                              "", ledger.encoded([event])))
    return mid


def mark(c, lease, mid, state, attempt, reason=""):
    event = {"state": state, "action": attempt, "at": util.iso(), **({"reason": reason} if reason else {})}
    c.execute("UPDATE orchestration_messages SET state=?, attempt=?, history=json_insert(history,'$[#]',json(?)) "
              "WHERE root=? AND id=?", (state, attempt, ledger.encoded(event), lease.root, mid))


def page(c, root, after=0, limit=20, worker="", pending=False, outstanding=False):
    rows = c.execute("SELECT * FROM orchestration_messages WHERE root=? AND seq>? AND (?='' OR worker=?) "
                     "AND (NOT ? OR state IN ('queued','dispatched')) AND (NOT ? OR state IN ('open','queued','dispatched')) ORDER BY seq LIMIT ?",
                     (root, after, worker, worker, pending, outstanding, limit))
    return [{**dict(row), "history": json.loads(row["history"]),
             "sender": "controller" if row["kind"] in ("reply", "steer") else row["worker"],
             "recipient": row["worker"] if row["kind"] in ("reply", "steer") else "controller"} for row in rows]


def inbox(lease, worker):
    with reader() as c:
        ledger.fenced(c, lease)
        return page(c, lease.root, worker=worker, pending=True, limit=MAX_INBOX)


def queue(lease, action, validate_scope=None):
    args = action["request"]
    kind = "reply" if action["op"] == "worker.reply" else "steer"
    question = args.get("question_id", "")
    with tx() as c:
        w = get(c, lease, args["worker_id"])
        existing = c.execute("SELECT * FROM orchestration_messages WHERE root=? AND "
                             "(id=? OR (kind='reply' AND reply_to=? AND reply_to<>''))",
                             (lease.root, message_id(lease, action["id"], kind), question)).fetchone()
        if existing:
            prior = c.execute("SELECT request FROM orchestration_actions WHERE root=? AND id=?",
                              (lease.root, existing["action"])).fetchone()
            if (not prior or json.loads(prior["request"]) != args
                    or (existing["worker"], existing["body"], existing["reply_to"]) != (w["id"], args["message"], question)):
                raise ValueError("message identity or answered question reused with different content")
            mid = existing["id"]
            if existing["action"] == action["id"]:
                saved = c.execute("SELECT result,state FROM orchestration_actions WHERE root=? AND id=?",
                                  (lease.root, action["id"])).fetchone()
                if saved["state"] == "succeeded":
                    return json.loads(saved["result"])
        else:
            if w["phase"] in CLOSED | {"integrating", "candidate", "reconciliation-required"}:
                raise ValueError(f"worker is {w['phase']} at {w['attempt']}")
            if kind == "reply" and (w["phase"] != "waiting" or w["data"].get("question") != question):
                raise ValueError("reply must name this worker's outstanding question")
            if len(page(c, lease.root, worker=w["id"], pending=True)) >= MAX_INBOX:
                raise ValueError("worker inbox is full")
            if scope := args.get("write_paths"):
                validate_scope(w, scope, workers(lease, c))
                w["data"]["effective"]["write_paths"] = w["data"]["write_paths"] = scope
            mid = add(c, lease, w, action["id"], kind, args["message"], question)
            if question:
                mark(c, lease, question, "answered", action["id"])
                w["data"].pop("question")
            if question or w["phase"] == "handoff":
                save(c, lease, w, "ready")
        return ledger.transition_in(c, lease, action["id"], "succeeded",
                                    {"worker_id": w["id"], "message_id": mid, "state": existing["state"] if existing
                                     else "queued"})["result"]


def started(lease, worker, attempt, result):
    with tx() as c:
        w = get(c, lease, worker)
        if w["phase"] not in {"assigned", "ready", "handoff", "parked", "conflict"}:
            raise ValueError(f"worker {worker} is {w['phase']}")
        w["attempt"] = attempt
        save(c, lease, w, "running")
        ledger.transition_in(c, lease, attempt, "running", {**result, "worker_id": worker})


def dispatched(lease, worker, attempt, ids):
    with tx() as c:
        w = get(c, lease, worker)
        if w["attempt"] != attempt or w["phase"] != "running":
            raise ledger.Conflict("worker attempt superseded")
        w["data"]["admitted_attempt"] = attempt
        save(c, lease, w, "running")
        active = sum(x["phase"] == "running" and x["data"]["writing"] and x["data"].get("admitted_attempt") == x["attempt"]
                     for x in workers(lease, c).values())
        c.execute("UPDATE orchestration_actions SET result=json_set(result,'$.active_builders',?) WHERE root=? AND id=?",
                  (active, lease.root, attempt))
        for mid in ids:
            mark(c, lease, mid, "dispatched", attempt)


def finish(lease, worker, attempt, result, outcome):
    with tx() as c:
        w = get(c, lease, worker)
        if w["generation"] != lease.generation or w["attempt"] != attempt:
            raise ledger.Conflict("worker result belongs to a superseded attempt")
        terminal_sha = ledger.digest([result, outcome])
        if w["phase"] != "running":
            if w["data"].get("terminal_sha") == terminal_sha:
                return w["phase"]
            raise ledger.Conflict("attempt identity reused with different terminal content")
        w["data"]["terminal_sha"] = terminal_sha
        data = w["data"]
        if outcome:
            for mid in outcome["ack_ids"]:
                row = c.execute("SELECT state,attempt FROM orchestration_messages WHERE root=? AND id=? AND worker=?",
                                (lease.root, mid, worker)).fetchone()
                if not row or tuple(row) != ("dispatched", attempt):
                    raise ValueError("acknowledgment has no matching dispatched attempt")
                mark(c, lease, mid, "acknowledged", attempt)
            kind = outcome["outcome"]
            result = {**result, "text": outcome["text"], "outcome": kind, "evidence_refs": outcome.get("evidence_refs", [])}
            data.pop("retries", None)
            data.pop("reason", None)
            phase = "candidate" if data["writing"] else "completed"
            if kind in ("question", "handoff"):
                mid = add(c, lease, w, attempt, kind, outcome["text"], state="open" if kind == "question" else "reported")
                result["message_id"] = mid
                if kind == "question":
                    data["question"] = mid
                phase = "waiting" if kind == "question" else "handoff"
            if phase != "waiting" and page(c, lease.root, worker=worker, pending=True):
                phase = "ready"
            data["summary"] = outcome["text"]
        else:
            data["retries"] = data.get("retries", 0) + 1
            data["reason"] = result.get("error", "worker produced no valid outcome")
            phase = "ready" if data["retries"] == 1 else "parked"
        save(c, lease, w, phase)
        ledger.transition_in(c, lease, attempt, "succeeded" if outcome else "failed",
                             {**result, "worker_id": worker, "phase": phase})
        return phase


def schedule(lease, worker, maximum):
    with tx() as c:
        w = get(c, lease, worker)
        if w["phase"] != "ready":
            raise ledger.Conflict("continuation is not ready")
        rows = c.execute("SELECT count(*) FROM orchestration_actions WHERE root=?", (lease.root,)).fetchone()[0]
        if maximum is not None and rows >= maximum:
            raise ledger.Limit(f"run used its {maximum} action allowance")
        attempt = f"{worker}.t{rows + 1}"
        args = {"worker_id": worker, "message": w["data"]["effective"]["goal"], "settings": {}}
        c.execute("INSERT INTO orchestration_actions VALUES (?,?,?,?,?,'accepted',?,?,?)",
                  (lease.root, attempt, worker.split("-")[0], lease.run, "worker.continue", ledger.encoded(args),
                   ledger.encoded({"scheduled": True}), util.iso()))
        w["attempt"] = attempt
        save(c, lease, w, "assigned")
        return ledger.decoded(c.execute("SELECT * FROM orchestration_actions WHERE root=? AND id=?",
                                        (lease.root, attempt)).fetchone())


def change(lease, worker, phase, **updates):
    with tx() as c:
        w = get(c, lease, worker)
        w["data"].update(updates)
        save(c, lease, w, phase)


def cancel(lease, worker, action, reason, *, receipt=False):
    with tx() as c:
        w = get(c, lease, worker)
        if w["phase"] in CLOSED | {"integrating", "reconciliation-required"}:
            raise ValueError(f"worker is {w['phase']} at {w['attempt']}")
        producer = c.execute("SELECT state FROM orchestration_actions WHERE root=? AND id=?",
                             (lease.root, w["attempt"])).fetchone()
        if producer and producer["state"] in ("accepted", "running"):
            ledger.transition_in(c, lease, w["attempt"], "cancelled",
                                 {"effective": w["data"]["effective"], "worker_id": worker,
                                  "reason": reason, "usage_unknown": producer["state"] == "running"})
        for row in c.execute("SELECT id FROM orchestration_messages WHERE root=? AND worker=? "
                             "AND state IN ('open','queued','dispatched')", (lease.root, worker)).fetchall():
            mark(c, lease, row["id"], "cancelled", action, reason)
        w["data"].pop("question", None)
        w["data"]["reason"] = reason
        save(c, lease, w, "cancelled")
        if receipt:
            return ledger.transition_in(c, lease, action, "succeeded", {"cancelled": worker, "quiescent": True})["result"]


def public_text(value, key=""):
    if isinstance(value, dict):
        return {k: public_text(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [public_text(v, key) for v in value]
    if isinstance(value, str) and key in {"text", "message", "goal", "question", "reason", "error", "body"}:
        return outbound.redact(value)[:4000]
    return value


def projection(c, lease, limits, *, after=0, limit=20, outstanding=False):
    ws = workers(lease, c)
    visible = [{k: w[k] for k in ("id", "attempt", "generation", "phase")} |
               {k: w["data"][k] for k in ("writing", "question", "role", "reason", "write_paths", "conflicts") if k in w["data"]}
               for w in ws.values()]
    messages = page(c, lease.root, after, limit + 1, outstanding=outstanding)
    more = len(messages) > limit
    messages = messages[:limit]
    for m in messages:
        m["body"] = outbound.redact(m["body"])[:4000]
        m["history"] = public_text(m["history"][-20:])
        m.pop("root")
    for item in visible:
        if "reason" in item:
            item["reason"] = outbound.redact(item["reason"])[:2000]
    builders = sum(w["phase"] == "running" and w["data"]["writing"] for w in ws.values())
    total = sum(w["phase"] == "running" for w in ws.values())
    ceiling = min(limits.get("max_builders") or 1, limits.get("max_workers", 1))
    return {"workers": visible, "messages": messages, "more_messages": more, "message_cursor": messages[-1]["seq"] if messages else after,
            "capacity": {"configured_builders": limits.get("max_builders"), "effective_builders": ceiling,
                         "active_builders": builders, "active_workers": total, "max_workers": limits.get("max_workers"),
                         "available_builders": max(0, min(ceiling - builders, limits.get("max_workers", 1) - total))}}
