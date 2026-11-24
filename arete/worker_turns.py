"""Protocol 2 scheduling and reconciliation. The controller owns all dispatch."""
from __future__ import annotations

import json

from . import builder_workspaces as candidates
from . import delegation, factory, runlimits, sandbox, util
from .orchestration_api import WORKER_PROTOCOL
from .store import orchestration as ledger
from .store import worker_exchange as exchange
from .store.db import reader

WORKER_INSTRUCTIONS = """
Use only your private working directory. Read the repository instructions that apply to your paths before editing.
Keep edits within the listed write paths.
Run the focused acceptance check when feasible. The engine runs the final gates.
For a decision or a larger scope, stop this invocation with a question. Report a finding for routing with handoff.
Return exactly one JSON object as your final response. version is 2, outcome is done, question or handoff,
text is your summary, finding or question (1 to 16000 characters), ack_ids lists every supplied message ID.
Optional evidence_refs may only name supplied context references. Transcript messages have no control effect.
"""


class WorkerTurns:
    protocol = 1

    @property
    def exchanged(self):
        return self.protocol == WORKER_PROTOCOL

    def serial_reason(self):
        if self.job.layer.copy or self.job.closed_prs:
            return "this workspace needs shared copy inputs or contains a closed checkout"
        if not self.job.cfg.replay.all:
            routes = self.run.settings("build").chain
            if not sandbox.can_confine(False) or not (self.policy.private_worker_model or any(link.runner == "pi" for link in routes)):
                return "private workers require local Pi with macOS confinement"
        for c in self.job.checkouts:
            if c.wt.resolve() != self.job.ws.resolve() / c.dir:
                return "the checkout layout cannot be reproduced privately"
            tree = factory.fingerprint(c.wt)
            if not tree or candidates.unsupported_tree(c.wt, tree):
                return "this checkout needs a submodule, credential, or unavailable base snapshot"
        return ""

    def worker_observation(self, after=0, limit=20, outstanding=False):
        with reader() as c:
            return exchange.projection(c, self.lease, vars(self.policy), after=after, limit=limit, outstanding=outstanding)

    def assignment_role(self, args, writing, role, workers):
        if not writing:
            return role
        if self.data.get("ci_failed"):
            return "ci_repair"
        catch_up = self.data.get("catch_up_hold", {})
        if self.job.review.verdict == "request_changes" or catch_up.get("gate") == "review":
            return "review_repair"
        if self.run.repairs or catch_up or self.data.get("denied_feedback"):
            return "check_repair"
        for action in ledger.actions(self.lease):
            result = action["result"]
            if ((action["op"] == "checks.run" and result.get("passed") is False)
                    or (action["op"] == "verification.run" and result.get("verdict") == "fail")
                    or (action["op"] == "review.run" and result.get("verdict") == "request_changes")):
                return "check_repair"
        for w in workers.values():
            if w["data"]["writing"] and (w["data"]["role"] != "build"
                    or candidates.overlaps(args["write_paths"], w["data"]["write_paths"])):
                return "check_repair"
        return "build"

    def preflight(self, calls):
        ws = exchange.workers(self.lease)
        for call in calls:
            wid = call["args"].get("worker_id")
            if wid is not None and wid not in ws:
                raise ValueError("address the logical worker by its original action ID")
        if any(w["phase"] in {"integrating", "reconciliation-required"} for w in ws.values()) and any(
                call["op"] in ("worker.start", "worker.continue") for call in calls):
            raise ValueError("an incomplete integration needs reconciliation before another launch")
        active = len(self.workers)
        builders = sum(w.writing for w in self.workers.values())
        claims = [w["data"]["write_paths"] for w in ws.values() if w["phase"] not in exchange.CLOSED]
        repairs = sum(w.role in runlimits.REPAIRS and not w.admitted for w in self.workers.values())
        starts = [call for call in calls if call["op"] in ("worker.start", "worker.continue")]
        if sum(w["phase"] not in exchange.CLOSED for w in ws.values()) + sum(
                call["op"] == "worker.start" for call in starts) > self.policy.max_actions:
            raise ValueError("outstanding workers exceed the pinned action limit")
        if len(starts) > 1 and len(starts) != len(calls):
            raise ValueError("a fan-out decision must contain only worker launches")
        continuing = set()
        for call in starts:
            args = call["args"]
            if call["op"] == "worker.continue":
                w = ws.get(args["worker_id"])
                if not w or w["phase"] not in {"conflict", "handoff", "parked"} or w["id"] in continuing:
                    raise ValueError("continue requires a parked worker, conflict or handoff; answer questions with worker.reply")
                continuing.add(w["id"])
                writing, role = w["data"]["writing"], w["data"]["role"]
                args = {**w["data"]["effective"], **args.get("settings", {})}
                setting, _, _ = self.catalog.worker(args)
            else:
                setting, _, writing = self.catalog.worker(args)
                writing = writing and args["tool_bundle"] == "write"
                if writing:
                    candidates.validate_claims(args["write_paths"], self.job.layer, self.job.checkouts)
                    if args["acceptance_check"] == "none":
                        raise ValueError("a builder requires a focused acceptance_check")
                    if any(candidates.overlaps(args["write_paths"], other) for other in claims):
                        raise ValueError("write_paths overlap an outstanding builder claim")
                    claims.append(args["write_paths"])
                elif args["write_paths"]:
                    raise ValueError("readers cannot claim write paths")
                role = self.assignment_role(args, writing, setting.step, ws)
            if not self.job.cfg.replay.all and setting.runner != "pi":
                raise ValueError("this private worker needs a local Pi route; select the approved Pi route or a serial layer")
            active += 1
            builders += int(writing)
            repairs += int(writing and role in runlimits.REPAIRS)
        if starts and (active > self.policy.max_workers or builders > self.policy.builder_limit):
            raise ValueError("requested workers exceed available worker or builder capacity")
        budget = self.run.budget
        if starts and budget and repairs and not getattr(budget, "issue_goal", False):
            if budget.prior_repairs + budget.repair_calls + repairs > budget.policy.max_repair_cycles:
                raise ValueError("worker batch exceeds remaining repair calls")

    def validate_scope(self, w, names, workers):
        if not w["data"]["writing"]:
            raise ValueError("only a builder can request an expanded write scope")
        candidates.validate_claims(names, self.job.layer, self.job.checkouts)
        if any(other["id"] != w["id"] and other["phase"] not in exchange.CLOSED and
               candidates.overlaps(names, other["data"]["write_paths"]) for other in workers.values()):
            raise ValueError("revised write_paths overlap another worker's claim")

    def private_assignment(self, action, args, prior, writing, role):
        ws = exchange.workers(self.lease)
        wid = action["request"]["worker_id"] if prior else action["id"]
        w = ws.get(wid)
        if w:
            if w["phase"] not in {"assigned", "conflict", "handoff", "parked"}:
                raise ValueError(f"worker {wid} is {w['phase']}")
            data = w["data"]
            if w["phase"] == "conflict" and data.get("merge_conflict"):
                retained = [*data.get("retained", []), data["candidate"]]
                candidate = candidates.rebase(data["candidate"], self.job.checkouts, self.job.ws)
                data.update(candidate=candidate, retained=retained, merge_conflict=False)
                exchange.change(self.lease, wid, "conflict", **data)
            list(candidates.parts(data["candidate"], self.job.checkouts))
            return wid, data
        self.preflight([{"op": "worker.start", "args": args}])
        role = self.assignment_role(args, writing, role, ws)
        sibling = next((w["data"]["candidate"] for w in ws.values() if w["data"]["turn"] == action["turn"]), None)
        candidate = candidates.create(self.job.checkouts, self.job.ws, pinned=sibling)
        data = {"effective": args, "role": role, "writing": writing, "write_paths": args["write_paths"],
                "turn": action["turn"], "candidate": candidate}
        try:
            exchange.assign(self.lease, wid, data)
        except Exception:
            candidates.release(candidate)
            raise
        return wid, data

    def settle_candidate(self, w):
        wid, data = w["id"], w["data"]
        with util.lock("builder-integration-" + ledger.digest(str(self.job.ws.resolve()))[:24], wait=True) as held:
            if not held:
                raise ValueError("cannot acquire delivery integration lease")
            # A generation cannot change while the fenced write transaction applies this intent.
            if w["phase"] == "candidate":
                if any(other["id"] != wid and other["phase"] in {"integrating", "reconciliation-required"}
                       for other in exchange.workers(self.lease).values()):
                    return
                try:
                    intent = candidates.stage(data["candidate"], self.job.checkouts, self.job.layer, data["write_paths"])
                except (RuntimeError, OSError, ValueError) as error:
                    exchange.change(self.lease, wid, "conflict", reason=str(error)[:2000],
                                    merge_conflict=isinstance(error, candidates.MergeConflict), conflicts=data.get("conflicts", 0) + 1)
                    return
                exchange.change(self.lease, wid, "integrating", intent=intent,
                                candidate_digest=ledger.digest([item["candidate"] for item in intent]))
            with exchange.tx() as c:
                current = exchange.get(c, self.lease, wid)
                if current["phase"] != "integrating":
                    raise ledger.Conflict("integration no longer pending")
                try:
                    candidates.apply(data["candidate"], self.job.checkouts, current["data"]["intent"])
                except (RuntimeError, OSError, ValueError) as error:
                    current["data"]["reason"] = str(error)[:2000]
                    exchange.save(c, self.lease, current, "reconciliation-required")
                    return
                current["data"].pop("reason", None)
                exchange.save(c, self.lease, current, "integrated")
            self.data["evidence"] = {}
            self.job.b.checked = {}
            self.job.b.report = ""
        self.worker_summaries()

    def worker_summaries(self):
        ws = exchange.workers(self.lease)
        self.job.build_summary = self.job.b.build_summary = "\n\n".join(
            f"{wid}: {w['data']['summary']}" for wid, w in ws.items() if w["phase"] == "integrated")

    def settle_turn(self, worker, message):
        result = {**message, "effective": worker.effective, "writing": worker.writing}
        for key in ("text", "structured"):
            result.pop(key, None)
        outcome = None
        if message.get("ok"):
            try:
                outcome = delegation.turn_outcome(message, worker.offered, worker.effective["context_refs"])
            except (ValueError, TypeError) as error:
                result.update(ok=False, error=str(error)[:2000])
        phase = exchange.finish(self.lease, worker.logical_id, worker.action, result, outcome)
        if outcome:
            self.catalog.add_context(worker.action, outcome["text"])
            if phase == "completed" and worker.effective["prompt_id"].removeprefix("prompt:") == "plan":
                self.job.plan_text = self.job.b.plan_text = outcome["text"]
        if phase == "candidate":
            self.settle_candidate(exchange.workers(self.lease)[worker.logical_id])
        self.persist()
        return exchange.workers(self.lease)[worker.logical_id]["phase"]

    def recover_workers(self):
        actions = {a["id"]: a for a in ledger.actions(self.lease)}
        ws = exchange.workers(self.lease).values()
        for w in sorted(ws, key=lambda item: item["phase"] == "candidate"):
            if w["phase"] in ("running", "assigned") and actions[w["attempt"]]["state"] != "accepted":
                exchange.change(self.lease, w["id"], "ready", reason="interrupted turn; redelivering outstanding messages")
            if w["phase"] == "parked" and w["data"].get("resume_interrupted"):
                exchange.change(self.lease, w["id"], "ready", resume_interrupted=False)
            if w["phase"] in ("candidate", "integrating", "reconciliation-required"):
                if w["phase"] == "reconciliation-required":
                    exchange.change(self.lease, w["id"], "integrating")
                self.settle_candidate(w)
        self.worker_summaries()

    def resume_ready(self):
        workers = exchange.workers(self.lease).values()
        if any(w["phase"] in {"integrating", "reconciliation-required"} for w in workers):
            return
        for w in workers:
            if w["phase"] != "ready":
                continue
            if len(self.workers) >= self.policy.max_workers:
                return
            if w["data"]["writing"] and sum(x.writing for x in self.workers.values()) >= self.policy.builder_limit:
                continue
            action = exchange.schedule(self.lease, w["id"], self.action_limit)
            self.dispatch(action)

    def worker_barrier(self):
        pending = [f"{w['id']} ({w['phase']})" for w in exchange.workers(self.lease).values()
                   if w["phase"] not in exchange.CLOSED]
        if pending:
            raise ValueError("outstanding worker work: " + ", ".join(pending[:20]))


def instructions(data, messages):
    return (WORKER_INSTRUCTIONS + "\nWrite paths: " + json.dumps(data["write_paths"]) +
            "\nFocused acceptance check: " + data["effective"]["acceptance_check"] +
            "\nMessages for this turn: " + json.dumps([{"id": m["id"], "kind": m["kind"], "body": m["body"],
                                                   "reply_to": m["reply_to"]} for m in messages]))
