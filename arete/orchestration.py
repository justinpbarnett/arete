from __future__ import annotations

import json
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from math import inf
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from . import (
    builder_workspaces,
    check_repair,
    code_review,
    codebase,
    config,
    delegation,
    diffguard,
    factory,
    flows,
    gh,
    hosted_check,
    journeys,
    merge_review,
    outbound,
    owner_asks,
    pilot,
    pipeline,
    pr_recovery,
    principals,
    prompt_loader,
    repairs,
    requirement_revision,
    run_lines,
    runlimits,
    slots,
    spec_amendments,
    spec_delivery,
    spec_graph,
    spec_graph_delivery,
    tracker,
    util,
    vault,
    verification_attempts,
    worker_turns,
    workspace,
    zte,
)
from .agent import NO_SESSION, Session, json_from_text
from .orchestration_api import BUNDLES, PROTOCOL, WORKER_PROTOCOL, Catalog, describe_operations, help_text
from .store import asks as ask_store
from .store import db
from .store import insights as insights_store
from .store import orchestration as ledger
from .store import specs as spec_store
from .store import triage as triage_store
from .store import worker_exchange as exchange
from .verify import Verified

CI_FAILURES = "ci:failures"
MAX_CI_FAILURE_CHARS = 24000
ACTION_GATES = {"checks.run": "checks", "verification.run": "verify", "verification.decide": "verify",
                "review.run": "review", "review.decide": "review"}
CATCH_UP_FAILURE = "catch-up:failure"
MAX_CATCH_UP_FEEDBACK_CHARS = 12000
STATE_FIELDS = ("plan_text", "goal", "estimate", "left_for_a_person", "build_summary", "reviews", "fix_reply",
                "document", "report_body", "draft_why", "summary", "opened", "found", "pushed", "drafted",
                "ci_stopped", "changed", "ci_result", "committed")
REFRESHABLE_LINEAGE = {"catalog_sha", "controller_prompt", "operations_sha"}
CONTROLLER_STALL = "controller-stall"


def stall_routes(setting):
    routes = [setting, setting]
    if setting.chain[1:]:
        later = setting.chain[1]
        routes.append(replace(setting, runner=later.runner, provider=later.provider, model=later.model,
                              effort=later.effort))
    return routes


def stalled_before_reply(result):
    return (not result.get("ok") and result.get("turns") == 0 and not result.get("paid_usd")
            and not result.get("cost_usd") and not result.get("recovery"))


class UnchangedPushedHead(ValueError):
    pass


class RetargetedPR(ValueError):
    def __init__(self, pr: str, previous: str, current: str):
        self.pr = pr
        super().__init__(f"The PR base changed from `{previous}` to `{current}`. A person needs to revise it.")


def policy_fingerprint(cfg, layer, closed_context, *, resume=False):
    effective = config.policy_dict(layer)
    effective.pop("factory_path")
    effective["orchestration"].pop("max_builders", None)
    effective["orchestration"].pop("private_worker_model", None)
    if layer.required_check is None:
        effective.pop("required_check")
    if layer.verify:
        effective["verify"] = layer.verify.policy()
    policy_config = config.policy_dict(cfg)
    for field in ("server_url", "unblock_repos", "unblock_step"):
        policy_config.pop(field)
    policy_config["max_parallel_checks"] = config.Config.max_parallel_checks
    if resume:
        config.remove_controller_only_settings(policy_config)
    return ledger.digest(json.loads(json.dumps({"layer": effective, "config": policy_config,
                                                "closed_context": closed_context}, default=str)))


class Controller(worker_turns.WorkerTurns):
    issue_goal = False
    merge_lease: TextIO | None = None
    starts_new_lineage = False
    route_noted = False

    def __init__(self, flow, *, max_builders=None):
        self.flow, self.job = flow, flow.ctx
        self.run = self.job.run
        self.workers, self.progress, self.reported = {}, {}, {}
        configured = pipeline.config.load_layer(self.run.repo.layer_dir).orchestration
        if max_builders is not None:
            configured = replace(configured, max_builders=config.orchestration_table({"max_builders": max_builders}).max_builders)
        private = configured.max_builders and self.default_code_review and not self.is_spec
        protocol = WORKER_PROTOCOL if private else PROTOCOL
        self.lease = ledger.acquire(self.run.id, flow.continues,
                                    {"protocol": protocol, "mode": self.run.path, "evidence": {},
                                     "review_method": code_review.METHOD if self.default_code_review else "legacy"},
                                    **({"spec_root": self.run.spec_root} if self.is_spec else {}))
        self.data = ledger.snapshot(self.lease)["data"]
        self.run.code_review = self.uses_code_review
        self.protocol = self.data.get("protocol")
        if self.protocol not in (PROTOCOL, WORKER_PROTOCOL) or self.data.get("mode") != self.run.path:
            raise factory.Refused("this controller protocol needs compatible code before it can resume")
        self.policy = pipeline.config.Orchestration(**self.data.get("limits", asdict(configured)))
        if not self.exchanged:
            self.policy = replace(self.policy, max_builders=None)
        if self.run.recovery.get("allowance"):
            base = pipeline.config.load_layer(self.run.repo.layer_dir).orchestration
            extra = self.run.recovery["allowance"]
            self.policy = replace(self.policy, max_decisions=max(self.policy.max_decisions,
                                                                  base.max_decisions + extra.get("max_decisions", 0)),
                                  max_actions=max(self.policy.max_actions, base.max_actions + extra.get("max_actions", 0)))
        self.run.overrides = {**self.data.get("overrides", {}), **self.run.overrides}
        inherited = self.data.get("step_overrides", {})
        self.run.step_overrides = {role: {**inherited.get(role, {}), **self.run.step_overrides.get(role, {})}
                                  for role in inherited.keys() | self.run.step_overrides.keys()}
        self.issue_goal = getattr(self.run.budget, "issue_goal", False) is True
        self.catalog = None
        self.last_progress, self.ending = -inf, None

    @property
    def is_spec(self):
        return bool(spec_store.run_root(self.run))

    @property
    def decision_limit(self):
        return None if self.issue_goal else self.policy.max_decisions

    @property
    def action_limit(self):
        return None if self.issue_goal else self.policy.max_actions

    def configured_limits(self):
        limits = asdict(self.policy)
        if not self.exchanged:
            limits.pop("max_builders", None)
        if not self.exchanged or not self.policy.private_worker_model:
            limits.pop("private_worker_model", None)
        return limits

    def persist(self):
        self.data["job"] = {name: getattr(self.job, name) for name in STATE_FIELDS}
        self.data["review"] = self.job.review.asdict()
        self.data["verified"] = asdict(self.job.verified)
        if self.job.checkouts and self.data.get("prepared"):
            self.data["prepared"]["checkouts"] = [checkout.to_row() for checkout in self.job.workspace_checkouts]
            self.data["prepared"]["closed_prs"] = sorted(self.job.closed_prs)
        if self.job.built:
            self.data["checked"] = self.job.b.checked
            self.data["report"] = self.job.b.report
            self.data["catch_ups"] = self.job.b.catch_ups
        ledger.save(self.lease, self.data)

    def atomic(self, name, **kw):
        self.flow.stepped = True
        outcome = self.flow.run(name, (), kw)
        self.job.take(name, outcome, stored=False)
        return outcome

    def continuation_blocker(self, *, decision_boundary=False, settling=""):
        for turn in ledger.pending(self.lease):
            launch = turn["response"] or {}
            if delegation.live(launch.get("pid"), launch.get("identity")):
                return "the prior controller inference is still live; no replacement was launched"
        actions = ledger.actions(self.lease)
        for action in actions:
            result = action["result"]
            if action["state"] == "running" and delegation.live(result.get("pid"), result.get("identity")):
                return f"worker {action['id']} is still live; no replacement was launched"
        if decision_boundary:
            for action in actions:
                if action["state"] not in ledger.TERMINAL and not (
                    settling and (action["id"] == settling or action["state"] == "accepted")
                ):
                    return f"accepted action {action['id']} is unfinished ({action['state']})"
        return ""

    def quiescent(self):
        if why := self.continuation_blocker():
            raise factory.Refused(why)

    def write_lineage_event(self):
        if event := self.data.get("lineage_event"):
            started = {"new_lineage": True} if event.get("new_lineage") else {}
            run_lines.write(event["run"], "lineage", event_id=event["event_id"], old=event["old"], new=event["new"],
                            engine_commit=event["engine_commit"], **started)
            if started:
                self.run.log("lineage: started a new controller lineage; " + ", ".join(
                    f"{key} {event['old'][key]} → {event['new'][key]}" for key in event["old"]))
            del self.data["lineage_event"]
            ledger.save(self.lease, self.data)

    def prepare(self):
        self.write_lineage_event()
        if self.flow.refusal:
            raise factory.Refused(self.flow.refusal)
        pending_push = ledger.catch_up_push(self.data)
        merge_verification = self.run.recovery.get("merge_verification") is True
        if self.is_spec and self.data.get("preparing") and not self.data.get("prepared"):
            if recovered := flows.prepared_data(self.flow.continues):
                self.data["prepared"] = recovered
            elif not factory.started(self.flow.continues, "prepare"):
                self.data.pop("preparing")
        prepared = self.data.get("prepared")
        restore_prs = bool(prepared) and not (pending_push or self.run.environment_release or self.run.action_only)
        if not restore_prs and self.run.feedback and (closed := self.run.feedback_closure()):
            self.quiescent()
            return "noop", closed, ""
        self.flow.matching = False
        if prepared:
            if not {"run_settings", "overrides", "step_overrides"} <= self.data.keys():
                raise factory.Refused("the saved controller lacks accepted invocation settings; reconcile before resuming")
            if restore_prs:
                published = flows.publication(self.flow.continues).get("prs") or {}
                for c in prepared["checkouts"]:
                    if not c.get("pr") and c["github"] in published:
                        c["pr"] = published[c["github"]]
            if ending := self.refresh_requirements(prepared, pending_push):
                return ending
            if merge_verification and not pending_push:
                prepared["approval"] = triage_store.triage_approval(self.run.triage_key)
            layer_dir, source_file = Path(prepared["layer"]), None
            if not layer_dir.exists():
                for checkout in map(workspace.Checkout.from_row, prepared["checkouts"]):
                    if layer_dir.is_relative_to(checkout.wt):
                        source_file = checkout.src / layer_dir.relative_to(checkout.wt) / "config.toml"
                        break
            self.job.layer = pipeline.checked_layer(self.job.cfg, layer_dir, source_file=source_file)
            self.run.cfg, self.run.layer, self.run.kind = self.job.cfg, self.job.layer, prepared["ticket"]["kind"]
            receipt = pr_recovery.pending_receipt(self.data)
            self.lineage(settling=receipt.get("action", ""))
            if receipt:
                self.note_route()
                self.settle_recovered_publication(prepared, receipt["action"])
            elif self.starts_new_lineage:
                self.discard_gate_state()
            if restore_prs:
                if ending := self.restore_pr_targets(prepared):
                    return ending
                tid = str(self.run.tid)
                if not (self.run.dry_run or self.run.action or pilot.is_pilot_task(tid) or zte.reserved(tid)):
                    self.job.before_pr_recovery = self.before_pr_recovery
                    self.job.on_pr_plan_installed = self.record_pr_recovery_checkout
            else:
                self.job.closed_prs = set(prepared.get("closed_prs", ()))
                if self.run.action_only and (ending := self.hold_dirty_closed_checkout(prepared["checkouts"])):
                    return ending
            active = [c for c in prepared["checkouts"] if c.get("pr") not in self.job.closed_prs]
            missing = any(not (Path(c["wt"]) / ".git").exists() for c in active)
            if missing:
                if any(Path(c["wt"]).exists() for c in active):
                    raise factory.Refused("the retained workspace is incomplete; reconcile without discarding its edits")
                if ledger.publication_pending(self.run.id) or not any(
                        a["op"] == "publication.request" and a["state"] == "succeeded" for a in ledger.actions(self.lease)):
                    raise factory.Refused("the missing workspace has no settled publication; reconcile before resuming")
            if pending_push:
                self.job.prepared(prepared, stored=True, reconciling_catch_up=True)
            elif self.run.environment_release:
                flows.restore_environment_draft(self.job, prepared)
            else:
                if missing:
                    self.run.closed_checkouts = [workspace.Checkout.from_row(c) for c in prepared["checkouts"]
                                                 if c.get("pr") in self.job.closed_prs]
                    line = {"step": "prepare", "tree": str(self.job.ws), "data": {**prepared, "checkouts": active}}
                    if why := self.job.restore(line, layer=self.job.layer):
                        raise factory.Refused(why)
                self.job.take("prepare", factory.Outcome("pass", data=prepared), stored=True)
            if self.job.pr_plans:
                self.discard_pr_recovery_state()
            for name, value in self.data.get("job", {}).items():
                if name in STATE_FIELDS:
                    setattr(self.job, name, value)
            self.job.review = pipeline.Review.stored(self.data.get("review", {}))
            self.job.verified = Verified.stored(self.data.get("verified", {}))
            self.job.b.checked = self.data.get("checked", {})
            self.job.b.report = self.data.get("report", "")
            self.job.b.catch_ups = self.data.get("catch_ups", 0)
            self.job.b.worker_trees = self.data.get("worker_trees", {})
            self.job.b.ignored_denied_paths = self.data.get("ignored_denied_paths", {})
            self.job.b.from_base = True
            self.job.b.plan_text, self.job.b.build_summary = self.job.plan_text, self.job.build_summary
            if self.job.pr_plans:
                workspace.note_made(self.job.ws, self.job.checkouts, excluded=self.run.closed_checkout_paths)
                self.persist()
            if pending_push:
                self.catalog.refresh_context()
                self.quiescent()
                self.publish()
                return self.ending
        else:
            self.quiescent()
            if self.run.environment_release:
                raise factory.Refused("the held controller has no saved workspace for verification")
            if self.data.get("preparing"):
                raise factory.Refused("preparation was interrupted before durable workspace authority; reconcile before restarting")
            self.data["preparing"] = True
            ledger.save(self.lease, self.data)
            try:
                outcome = self.atomic("prepare", tree=str(self.job.ws))
            except owner_asks.Asked:
                del self.data["preparing"]
                ledger.save(self.lease, self.data)
                raise
            if not outcome.passed:
                del self.data["preparing"]
                ledger.save(self.lease, self.data)
                return flows.ended(self.flow, outcome)
            self.data["prepared"] = outcome.data
            self.job.goal = f"Check the issue and owner's answers in use:\n{self.job.ticket.title}\n{self.job.ticket.body}"
            self.job.plan_text = ""
        if self.run.repairs:
            found = repairs.source(pipeline.run_store.run_get(self.run.repairs) or {})
            expected = set(found.prs) - self.job.closed_prs
            targets = [c for c in self.job.with_prs if c.pr in expected]
            if not expected or expected != {c.pr for c in targets}:
                raise factory.Refused("the repair findings do not name this workspace's open PRs")
            for checkout in targets:
                repairs.matched_head(checkout.wt, checkout.lease, found)
            if not self.data.get("repair_source"):
                self.data["repair_source"] = found.run_id
                self.run.story.append(f"Review findings to repair:\n{found.review.findings()}")
                self.job.plan_text, self.job.review = found.plan, found.review
                self.job.reviews = 1
                self.job.goal += f"\n\nCheck the saved review findings are fixed:\n{found.review.findings()}"
                self.job.left_for_a_person = found.left_for_a_person
        if zte.reserved(str(self.run.tid)):
            grant, policy = zte.for_run(self.run.repo, str(self.run.tid), self.run.id, "running")
            zte.check_checkout(grant, policy, self.run.repo, self.job.checkouts)
            if self.job.left_for_a_person or self.run.feedback or self.run.repairs or self.flow.continues:
                raise factory.Refused("ZTE does not inherit owner work or previous attempts")
        self.data.setdefault("goal_base", self.job.goal)
        known = {pipeline.feedback_key(f) for f in self.data.get("feedback", [])}
        fresh = [f for f in self.run.feedback if pipeline.feedback_key(f) not in known]
        if fresh:
            self.data.setdefault("feedback", []).extend(fresh)
            feedback = pipeline.feedback_markdown(fresh)
            self.run.story.append(feedback)
            self.data["goal_base"] += f"\n\nCheck each change the owner's PR feedback asks for:\n{feedback}"
            self.job.left_for_a_person = pipeline.left_on_prs(self.run.feedback_prs)
        answers = [a for a in self.run.answers_due or [] if not a.get("call", "").startswith("requirements.")]
        if answers:
            answer = owner_asks.fresh_note(answers)
            self.run.story.append(answer)
            notes = self.data.setdefault("owner_answers", [])
            for note in owner_asks.decision_notes(answers):
                if note not in notes:
                    notes.append(note)
        self.acceptance_goal()
        self.job.triage = pipeline.triage_notes(self.job.approval)
        self.job.b.plan_text, self.job.b.left_for_a_person = self.job.plan_text, self.job.left_for_a_person
        if self.exchanged and not prepared and (reason := self.serial_reason()):
            self.protocol = self.data["protocol"] = PROTOCOL
            self.policy = replace(self.policy, max_builders=None)
            self.data["serial_reason"] = reason
            self.run.log(f"using the serial writer before dispatch: {reason}")
        if not self.job.pr_plans:
            self.note_route()
        lineage, old_lineage = self.lineage()
        self.data.update(lineage)
        self.data["overrides"], self.data["step_overrides"] = self.run.overrides, self.run.step_overrides
        self.data.setdefault("code", codebase.STAMP)
        self.data["limits"] = self.configured_limits()
        self.acceptance_stamp()
        if (self.flow.continues and self.default_code_review and not self.run.environment_release
                and not self.uses_code_review):
            self.adopt_code_review()
        self.job.recheck_pr_plans("before restoring controller actions", installed=True)
        self.recover()
        if self.run.environment_release or merge_verification:
            self.data.pop("published", None)
            self.data.pop("published_tree", None)
        if restore_prs:
            try:
                self.merge_restored_bases()
            except delegation.PolicyChanged as error:
                recovery = {"reason": "policy-changed"}
                if self.run.closed_checkouts:
                    recovery.update(branch=self.job.branch, closed_checkouts=[c.to_row() for c in self.run.closed_checkouts])
                raise workspace.RunFailed(pipeline.record.failure(error), recovery=recovery) from error
        elif not prepared and self.flow.continues and (moved := flows.base_moved(self.flow.continues)):
            self.job.moved_base = moved
            self.job.merge_moved_base("orchestrate")
        if old_lineage:
            self.data["lineage_event"] = {"run": self.run.id, "event_id": uuid4().hex, "old": old_lineage,
                                          "new": {key: lineage[key] for key in old_lineage},
                                          "engine_commit": util.engine_commit(),
                                          **({"new_lineage": True} if old_lineage.keys() - REFRESHABLE_LINEAGE else {})}
        self.persist()
        self.write_lineage_event()
        self.job.recheck_pr_plans("before resuming work", installed=True)
        return None

    def current_ticket(self):
        ticket = spec_amendments.child_ticket(self.run.tracker, self.run.tracker.get(self.run.tid))
        if self.is_spec:
            why = tracker.spec_blocker(self.job.cfg, self.run.tracker, ticket)
            if not why and ticket.spec_identity != self.run.spec_identity:
                why = "the spec tracker identity changed"
        elif graph_hold := spec_graph_delivery.active_child_blocker(self.run):
            why = graph_hold
        elif not ticket.open:
            why = "the source ticket is closed"
        elif {"no-agent", "agent:hold"} & set(ticket.labels):
            why = "the owner placed the ticket on hold"
        elif ticket.is_spec:
            why = "the ordinary ticket now requires the spec delivery route"
        else:
            why = ticket.blocker
        return ticket, why

    def scope_question(self, ticket, reason):
        key = ledger.digest([self.lease.root, requirement_revision.version(ticket), reason])
        call = f"requirements.{key}"
        # The ask and ticket notice are persisted through the normal owner-question seam.
        if (not self.run.dry_run and not self.run.alone and not self.run.owner_questions_blocked
                and not ask_store.carry_scope_ask(self.run.repo.name, self.run.tid, call, self.run.id)):
            request = {"questions": [{"header": "Ticket scope", "kind": "done",
                                      "question": "Can you confirm the current requirements on the tracker?",
                                      "steps": [reason, "Reconcile the identified change, then resume this ticket."]}],
                       "why": "The current tracker text must authorize the next action.",
                       "tried": "Compared the saved requirements with the current tracker and execution receipts."}
            self.atomic("orchestrate_ask", request=request, call=call)
        return "needs-info", reason, ""

    def reconcile_answers(self, old, ticket):
        notes = ask_store.individual_scope_notes(self.data.get("runs", []), self.data.get("owner_answers", []))
        retained, retired, uncertain = requirement_revision.prior_answers(old, ticket, notes)
        if not uncertain:
            return retained, retired, None
        key = ledger.digest([self.lease.root, requirement_revision.version(ticket), uncertain])
        call = f"requirements.decisions.{key}"
        keep, revised = "Keep the listed decisions", "Use the revised ticket"
        asked = ask_store.carry_scope_ask(self.run.repo.name, self.run.tid, call, self.run.id)
        if asked and asked["status"] in ("answered", "taken") and len(asked["answers"]) == 1:
            answer = asked["answers"][0]
            if not answer.get("by", "").startswith("rule:"):
                if answer.get("picked") == [keep]:
                    return [*retained, *uncertain], retired, None
                if answer.get("picked") == [revised]:
                    return retained, [*retired, *uncertain], None
        reason = "The revised ticket leaves the applicability of these earlier owner decisions unresolved."
        if not asked and not (self.run.dry_run or self.run.alone or self.run.owner_questions_blocked):
            request = {"questions": [{"header": "Prior scope", "kind": "choice",
                       "question": reason + "\n\n" + "\n\n".join(uncertain),
                       "options": [{"label": revised, "description": "Recommended. The current ticket replaces these "
                                    "decisions. Add any detail you want to retain to its requirements."},
                                   {"label": keep, "description": "I confirm all the listed decisions still apply to this revision."}]}],
                       "why": "Previously accepted decisions must be reconciled with the owner's current scope.",
                       "tried": "Kept decisions tied to unchanged checks and retired decisions the ticket explicitly supersedes."}
            self.atomic("orchestrate_ask", request=request, call=call)
        return retained, retired, ("needs-info", reason, "")

    def refresh_requirements(self, prepared, pending_push):
        if not self.default_code_review:  # Reserved pilot and ZTE tasks have their own immutable grant.
            return None
        ticket, why = self.current_ticket()
        self.data.pop("scope_needs_refresh", None)
        if why:
            if self.data.get("scope_revoked") != why:
                self.data["scope_revoked"] = why
                ledger.save(self.lease, self.data)
                run_lines.write(self.run.id, "requirements", held=why)
            self.run.scope_revoked = True
            return "noop", f"Work held because {why}.", ""
        old = tracker.Ticket(**prepared["ticket"])
        previous, current = requirement_revision.version(old), requirement_revision.version(ticket)
        if not self.data.get("scope_revoked") and previous == current and (not self.is_spec or not self.data.get("definition")
                                    or self.data["spec_source"] == current):
            return None
        if isinstance(self.run.tracker, tracker.GitHubTracker):
            if why := requirement_revision.github_conflict(self.run.tracker, ticket):
                return self.scope_question(ticket, why)
        self.quiescent()
        actions = ledger.actions(self.lease)
        workers = exchange.workers(self.lease) if self.exchanged else {}
        keep = set() if self.data.get("scope_revoked") else requirement_revision.independent_workers(old, ticket, workers)
        if (pending_push or self.data.get("publication_intent")
                or any(a["op"] == "publication.request" and a["state"] in ("running", "reconciliation-required")
                       for a in actions)
                or any(w["phase"] in ("integrating", "reconciliation-required") for wid, w in workers.items() if wid not in keep)):
            return self.scope_question(ticket, "An earlier publication or worker integration has unsettled effects.")
        retained_notes, retired_notes, waiting = self.reconcile_answers(old, ticket)
        if waiting:
            return waiting
        reason = "the owner revised the ticket requirements"
        for worker, w in workers.items():
            if worker not in keep and w["phase"] not in exchange.CLOSED:
                exchange.cancel(self.lease, worker, f"scope-{current}", reason)
        retained = keep | {w["attempt"] for wid, w in workers.items() if wid in keep}
        superseded = {a["id"] for a in actions if a["id"] not in retained
                      and a["result"].get("worker_id", a["request"].get("worker_id")) not in keep}
        for action in ledger.actions(self.lease):
            if action["id"] in superseded and action["state"] in ("accepted", "running"):
                ledger.transition(self.lease, action["id"], "cancelled", {"reason": reason,
                                  "usage_unknown": action["state"] == "running"})
        self.discard_gate_state()
        self.data["superseded_actions"] = sorted({*self.data.get("superseded_actions", ()),
                                                  *superseded})
        self.data["owner_answers"] = retained_notes
        if retired_notes:
            history = self.data.setdefault("retired_owner_answers", {}).setdefault(previous, [])
            history.extend(note for note in retired_notes if note not in history)
        for key in ("scope_revoked", "definition", "spec_source", "published", "published_tree", "ci_retry", "ci_failed",
                    "ci_failures", "verification_required_for", "denied_feedback"):
            self.data.pop(key, None)
        job = self.data.get("job", {})
        for key in ("plan_text", "build_summary", "left_for_a_person", "fix_reply", "document", "report_body",
                    "draft_why", "summary"):
            job.pop(key, None)
        prepared["ticket"] = {key: getattr(ticket, key) for key in flows.TICKET_FIELDS}
        prepared["ticket"]["comments"] = ticket.owner_comments or ""
        prepared["approval"] = triage_store.triage_approval(self.run.triage_key)
        self.data["goal_base"] = f"Check the current authorized ticket in use:\n{ticket.title}\n{ticket.body}"
        if feedback := self.data.get("feedback"):
            self.data["goal_base"] += f"\n\nOwner PR feedback predating this ticket revision:\n{pipeline.feedback_markdown(feedback)}"
        ledger.save(self.lease, self.data)
        run_lines.write(self.run.id, "requirements", previous=previous, current=current,
                        superseded=self.data["superseded_actions"])
        return None

    def candidate_scope(self, worker):
        if not self.data.get("prepared"):
            return True
        current, why = self.current_ticket()
        saved = self.job.ticket
        if not why and (requirement_revision.version(saved) == requirement_revision.version(current)
                        or worker["id"] in requirement_revision.independent_workers(saved, current, {worker["id"]: worker})):
            return True
        reason = why or "the owner revised this worker's acceptance check"
        exchange.cancel(self.lease, worker["id"], "scope-" + requirement_revision.version(current), reason)
        self.data["scope_needs_refresh"] = reason
        if why:
            self.run.scope_revoked = True
            self.data["scope_revoked"] = why
        ledger.save(self.lease, self.data)
        return False

    def publication_scope(self):
        if not self.data.get("prepared") or not self.default_code_review:
            return
        current, why = self.current_ticket()
        if why:
            self.run.scope_revoked = True
            self.data["scope_revoked"] = why
            ledger.save(self.lease, self.data)
        if not why and requirement_revision.version(self.job.ticket) != requirement_revision.version(current):
            why = "the source requirements changed; resume to verify the revised ticket"
        if why:
            self.ending = ("failed", why, "")
            raise factory.Refused(why)

    def acceptance_goal(self):
        goal = self.data["goal_base"] + "\n" + "\n".join(self.data.get("owner_answers", []))
        if self.is_spec and (done := self.data.get("definition")):
            goal += (f"\n\n{spec_delivery.describe(done)}\n\n"
                     f"Required in-use evidence: check the criterion and return its exact wording in 'what':\n"
                     f"{done['criterion']}")
            if done.get("environment"):
                goal += ("\nMeasure the behavior on this candidate now. The engine separately requires evidence "
                         "in the named runtime before closure; do not claim to have checked that runtime here.")
        if self.job.pr_plans:
            if discussion := pr_recovery.owner_discussion(self.job.ticket.comments):
                goal += f"\n\n## Current ticket discussion\n\n{discussion}"
            known_replies = {(pipeline.feedback_key(f), f["body"]) for f in self.data.get("feedback", [])}
            for plan in self.job.pr_plans:
                pr = plan.inputs.checkout.pr
                replies = self.run.pr_recovery_discussion[pr].replies
                if replies is None:
                    replies = pr_recovery.pr_replies(pr, self.run.repo.name, str(self.run.tid))
                unseen = [row for row in replies if (pipeline.feedback_key(row), row["body"]) not in known_replies]
                if unseen:
                    latest = sorted(unseen, key=lambda row: row["created"] or "")[-tracker.MAX_COMMENTS:]
                    discussion = f"## Owner discussion on the reconciled PR\n\n{pipeline.feedback_markdown(latest)}"
                    self.run.story.append(discussion)
                    goal += f"\n\n{discussion}"
        self.job.goal = pipeline.triage_goal(goal, self.job.approval)

    def acceptance_stamp(self):
        self.data["acceptance"] = ledger.digest([self.catalog.context["task"], self.job.goal, self.job.left_for_a_person])
        if self.is_spec and (done := self.data.get("definition")):
            self.catalog.add_context("definition of done", spec_delivery.describe(done))

    def spec_ticket(self):
        source = self.run.tracker.get(self.run.tid)
        if (why := tracker.spec_blocker(self.job.cfg, self.run.tracker, source)) or source.spec_identity != self.run.spec_identity:
            raise factory.Refused(why or "the spec tracker identity changed")
        return source

    def define_spec(self, args):
        found = spec_delivery.definition(args)
        found["environment"] = spec_delivery.protected_environment(self.run.repo.name, str(self.run.tid), found["environment"])
        if prior := self.data.get("definition"):
            if found != prior:
                raise ValueError("the spec already has a durable definition of done; do not silently replace it")
            return prior
        source = spec_delivery.source_version(self.spec_ticket())
        if source != spec_delivery.source_version(self.job.ticket):
            raise factory.Refused("the spec changed during preparation; resume to read the updated scope")
        self.data.update(definition=found, spec_source=source)
        self.acceptance_goal()
        self.acceptance_stamp()
        self.persist()
        self.run.say(spec_delivery.describe(found), on_ticket=True)
        return found

    def restore_pr_targets(self, prepared):
        targets = {c["pr"]: c for c in prepared["checkouts"] if c.get("pr")}
        try:
            open_targets = {pr for pr, c in targets.items() if self.pr_target_open(pr, c["base"])}
        except RetargetedPR as error:
            return self.hold_revision(error.pr, str(error))
        self.job.closed_prs = targets.keys() - open_targets
        if ending := self.hold_dirty_closed_checkout(prepared["checkouts"]):
            return ending
        unlisted = [pr for pr in self.run.feedback_prs if pr not in targets]
        self.run.feedback_closure(unlisted)
        self.run.closed_feedback_prs += tuple(pr for pr in self.run.feedback_prs if pr in self.job.closed_prs)
        closed = self.run.closed_feedback_prs
        if closed and not open_targets and len(closed) == len(self.run.feedback_prs):
            return "noop", f"the PR closed before arete could revise it: {', '.join(closed)}", ""
        if targets and not open_targets and all(c.get("pr") in self.job.closed_prs for c in prepared["checkouts"]):
            return "noop", "the saved PRs closed before arete could revise them", ""
        if closed:
            if not (self.run.dry_run or self.run.alone):
                insights_store.settle_feedback(self.run.id, "recorded", prs=closed)
            self.run.feedback = [f for f in self.run.feedback if f["pr_url"] not in closed]

        if open_targets:
            self.job.check_publication_targets = self.check_push_targets

    def hold_dirty_closed_checkout(self, checkouts):
        job = self.job
        closed = job.closed_prs
        revision_pr = next((c["pr"] for c in checkouts if c.get("pr") and c["pr"] not in closed), "")
        for c in checkouts:
            if c.get("pr") not in closed:
                continue
            checkout = workspace.Checkout.from_row(c)
            if why := workspace.kept_checkout(job.ws, checkout, self.run.repo.path, job.layer.copy, closed=True):
                return self.hold_revision(revision_pr or checkout.pr, why)

    def before_pr_recovery(self):
        if why := self.continuation_blocker():
            raise factory.Refused(f"cannot reconcile the PR while {why}; settle the pending work first")
        if self.job.pr_plans:
            return
        if self.exchanged:
            try:
                self.worker_barrier()
            except ValueError as error:
                raise factory.Refused(f"cannot reconcile the PR: {error}; settle the private assignments first") from error
        ticket = self.run.tracker.get(self.run.tid)
        if not ticket.open:
            raise factory.Refused("the ticket closed before the PR could be reconciled")
        self.job.ticket.comments = ticket.comments
        self.note_route()
        self.run.capture_pr_recovery_discussion([c.pr for c in self.job.with_prs])

    def note_route(self):
        if not self.route_noted:
            run_lines.write(self.run.id, "route", continues=self.flow.continues, mode="orchestrated", protocol=self.protocol)
            self.route_noted = True

    def record_pr_recovery_checkout(self, checkout):
        rows = self.data["prepared"]["checkouts"]
        self.data["prepared"]["checkouts"] = [checkout.to_row() if row["github"] == checkout.github else row for row in rows]
        ledger.save(self.lease, self.data)

    def discard_pr_recovery_state(self):
        self.invalidate_reconciled_inputs()
        for plan in self.job.pr_plans:
            self.data.get("worker_trees", {}).pop(plan.inputs.checkout.github, None)
        self.data["prepared"]["ticket"]["comments"] = self.job.ticket.comments

    def invalidate_reconciled_inputs(self):
        self.discard_gate_state()
        self.data["pr_recovery_stale_actions"] = self.data["stale_actions"]
        for action in ledger.actions(self.lease):
            if action["state"] == "accepted":
                ledger.transition(self.lease, action["id"], "cancelled", {"reason": "PR reconciliation invalidated the input"})
        for name in ("publication", "published", "published_tree", "ci_failed", "ci_retry", "ci_failures",
                     "steering", "catch_up_hold", "catch_ups"):
            self.data.pop(name, None)

    def check_push_targets(self):
        self.publication_scope()
        recovery = self.job.recovery_publication
        for checkout in self.job.with_prs:
            if recovery and recovery.bound(checkout):
                continue
            if not self.pr_target_open(checkout.pr, checkout.base):
                raise workspace.RunFailed(f"{checkout.pr} closed before publication")

    def pr_target_open(self, pr, base):
        details = gh.pr_merge_details(pr)
        if details.get("state") in ("CLOSED", "MERGED"):
            return False
        if details.get("state") != "OPEN" or not (current := details.get("baseRefName")):
            raise workspace.RunFailed(f"GitHub did not identify the open PR and its target branch for {pr}")
        if base != current:
            raise RetargetedPR(pr, base, current)
        return True

    def hold_revision(self, pr, reason):
        self.run.log(f"{pr}: {reason}")
        if not self.run.dry_run:
            key = ledger.digest([self.run.id, pr, reason])
            gh.pr_comment(pr, pipeline.public_text(self.run, reason), key=key)
            if not self.run.alone:
                self.run.enter("pr")
                if self.run.feedback:
                    insights_store.settle_feedback(self.run.id, "failed")
        return "failed", reason, pr

    def merge_restored_bases(self):
        job = self.job
        prs = job.with_prs
        if not prs:
            if self.run.answers_due:
                kept = {c.github for c in job.checkouts if c.onto}
                job.moved_base = flows.base_moved(self.flow.continues, repos=kept) if kept else ""
                if job.merge_moved_base("orchestrate", before_resolve=self.check_merged_policy):
                    self.check_merged_policy()
            return
        if job.pr_plans:
            self.check_merged_policy()
            if self.run.answers_due:
                self.run.kept_note = pipeline.kept_edits_note([c for c in job.checkouts if c.onto])
            job.b.workspace = job.workspace
            return
        deny = diffguard.denied_paths(job.layer)
        b = job.b

        def denied_paths(checkout, paths):
            return {path for path in paths
                    if util.matches_any(path, deny) or util.matches_any(checkout.prefix + path, deny)}

        def visible_denied(checkout):
            return denied_paths(checkout, pipeline.stageable_untracked(checkout.wt))

        denied_before = {}
        saved_baseline = {}
        saved_tracked = {}

        def remember_denied(checkout, head, tracked):
            repo = checkout.github
            denied_before[repo] = visible_denied(checkout)
            if tracked:
                saved_tracked[repo] = set(tracked)
            if previous := b.worker_trees.get(repo):
                paths = pipeline.changed_tree_paths(checkout.wt, head, previous)
                saved_baseline[repo] = head, denied_paths(checkout, paths)

        for checkout in prs:
            checkout.resumes = True
            if not checkout.recovery_base:
                workspace.fetch(checkout.src, checkout.base)
        policy_dir = self.run.repo.layer or job.ws / ".agents"
        merge_order = sorted((c for c in prs if not c.recovery_base),
                             key=lambda c: -len(c.wt.parts) if policy_dir.is_relative_to(c.wt) else 0)
        merged = pipeline.merge_moved_bases(self.run, merge_order, job.layer, job.ticket, allow_untracked=True,
                                           retained_workspace=job.ws,
                                           before_merge=remember_denied,
                                           before_resolve=self.check_merged_policy)
        if merged:
            self.check_merged_policy()
            for checkout in merged:
                repo = checkout.github
                visible = visible_denied(checkout)
                if hidden := denied_before[repo] - visible:
                    b.ignored_denied_paths[repo] = sorted(hidden.union(b.ignored_denied_paths.get(repo, ())))
                denied = visible.union(b.ignored_denied_paths.get(repo, ()))
                preserved = saved_tracked.get(repo, set())
                if repo in saved_baseline:
                    head, prior_paths = saved_baseline[repo]
                    if prior_paths:
                        changed = {Path(path) for path in pipeline.changed_tree_paths(checkout.wt, head, checkout.merge)}
                        parents = {parent for path in changed for parent in path.parents}
                        targets = changed | parents
                        preserved.update(path for path in prior_paths if Path(path) not in targets
                                         and changed.isdisjoint(Path(path).parents))
                previous = b.worker_trees.pop(repo, "")
                if denied or preserved:
                    b.worker_trees[repo] = (
                        factory.tree_with_paths(checkout.wt, checkout.merge, previous, sorted(denied | preserved),
                                                remove_missing=preserved)
                        if previous else checkout.merge)
            self.data["worker_trees"] = b.worker_trees
            if b.ignored_denied_paths:
                self.data["ignored_denied_paths"] = b.ignored_denied_paths
            workspace.note_made(job.ws, job.checkouts, excluded=self.run.closed_checkout_paths)
        if self.run.answers_due:
            self.run.kept_note = pipeline.kept_edits_note([c for c in job.checkouts if c.onto])
            self.run.base_note = pipeline.base_merge_note([c for c in merged if c.onto])
        job.b.workspace = job.workspace

    def check_merged_policy(self):
        job = self.job
        layer = pipeline.resolve_layer(job.cfg, self.run, job.ws)
        launch = {"config_sha": delegation.settings_sha(job.cfg), "layer_sha": delegation.settings_sha(job.layer)}
        delegation.check_policy(launch, job.cfg, layer)

    def discard_gate_state(self):
        self.data["stale_actions"] = sorted({*self.data.get("stale_actions", ()),
                                             *(action["id"] for action in ledger.actions(self.lease))})
        self.data["evidence"] = {}
        self.data["review"] = pipeline.Review.stored({}).asdict()
        self.data["verified"] = asdict(Verified("unverified"))
        for name in ("checked", "report", "remaining_checks"):
            self.data.pop(name, None)
        if "job" in self.data:
            self.data["job"] = {**self.data["job"], "reviews": 0, "ci_result": ""}

    def lineage(self, *, settling=""):
        tid = str(getattr(self.run, "tid", ""))
        pinned = pilot.is_pilot_task(tid) or zte.reserved(tid)
        restarts = not pinned and any(c.get("pr") for c in self.data.get("prepared", {}).get("checkouts", ()))
        self.catalog = Catalog(self.job, self.protocol, self.policy.private_worker_model) if self.exchanged else Catalog(self.job)
        policy = self.policy_sha()
        saved_policy = self.data.get("policy_sha")
        if saved_policy is not None and self.matches_policy(saved_policy, policy):
            policy = saved_policy
        found = {"catalog_sha": self.catalog.fingerprint,
                 "operations_sha": ledger.digest(describe_operations(self.protocol, spec=self.is_spec)),
                 "policy_sha": policy,
                 "controller_prompt": ledger.digest(prompt_loader.template(self.controller_prompt).template),
                 "controller_settings": ledger.digest(asdict(self.run.settings("orchestrate"))),
                 "run_settings": ledger.digest([self.run.overrides, self.run.step_overrides])}
        old = {key: self.data[key] for key, value in found.items() if key in self.data and self.data[key] != value}
        changes = ", ".join(f"{key} changed ({str(value)[:8]} → {found[key][:8]})" for key, value in old.items())
        if self.exchanged and old.keys() - REFRESHABLE_LINEAGE:
            if any(w["phase"] not in exchange.CLOSED for w in exchange.workers(self.lease).values()):
                raise factory.Refused(f"{changes}; outstanding private assignments need reconciliation before resuming")
        if old.keys() - REFRESHABLE_LINEAGE and not restarts:
            raise factory.Refused(f"{changes}; reconcile before resuming")
        if why := self.continuation_blocker(decision_boundary=bool(old), settling=settling):
            raise factory.Refused(f"{changes}; {why}; reconcile before resuming" if old else why)
        self.starts_new_lineage = bool(old.keys() - REFRESHABLE_LINEAGE)
        return found, old

    def ci_failures(self, current):
        logs = pipeline.run_folder(self.run, "ci")
        prefix = "publish" if current.n == 1 else "publish-rerun"
        notes = [pipeline.ci.failure_note(r, logs / f"{prefix}-{i}-{prompt_loader.slug(r.label)}.log")
                 for i, r in enumerate(current.reds, 1)]
        return "\n\n".join(["## CI failures", "", current.summary(), *notes])

    def policy_sha(self):
        tid = str(getattr(self.run, "tid", ""))
        authorized = pilot.is_pilot_task(tid) or zte.reserved(tid)
        return policy_fingerprint(self.job.cfg, self.job.layer, self.run.closed_context, resume=not authorized)

    def matches_policy(self, saved, current):
        return saved == current or saved == policy_fingerprint(
            self.job.cfg, self.job.layer, self.run.closed_context)

    def fingerprint(self):
        pr_recovery.check_staging(self.job.checkouts)
        trees = pipeline.checkout_trees(self.job.checkouts)
        pr_recovery.check_staging(self.job.checkouts)
        return ledger.digest(trees) if trees else ""

    def stamp(self, gate, result):
        run = getattr(self, "run", None)
        ticket = getattr(getattr(self, "job", None), "ticket", None)
        spec_graph_delivery.require_active_child(run, ticket)
        checked_trees = result.get("checked", {}).get("trees") if gate == "checks" else None
        tree = ledger.digest(checked_trees) if checked_trees else self.fingerprint()
        if not tree:
            raise ValueError("current workspace fingerprint is unavailable; evidence cannot authorize publication")
        self.data.setdefault("evidence", {})[gate] = {"tree": tree, "policy": self.policy_sha(), "result": result,
            "acceptance": self.data["acceptance"], "producer": self.executing, "observed": util.iso()}
        self.catalog.add_context(f"evidence:{gate}", ledger.encoded(self.data["evidence"][gate]))

    @property
    def default_code_review(self):
        tid = str(getattr(self.run, "tid", ""))
        return self.run.path == "orchestrate" and not (pilot.is_pilot_task(tid) or zte.reserved(tid))

    @property
    def uses_code_review(self):
        return self.data.get("review_method") == code_review.METHOD and not self.run.environment_release

    def required_gates(self):
        if self.is_spec:
            return ("checks", "review", "verify")
        owner_required = any(verification_attempts.REQUIRED_IN_USE.search(note) for note in self.data.get("owner_answers", []))
        if (self.uses_code_review and not code_review.requires_behavior(self.job.b) and not owner_required
                and not any(c.recovery_head for c in self.job.checkouts)
                and not self.data.get("verification_required_for")):
            return ("checks", "review")
        return ("checks", "review", "verify")

    def reuse_checks(self, evidence, focused):
        if (not self.uses_code_review or not self.default_code_review or self.run.action or self.run.alone
                or self.run.answers_due or self.run.recovery.get("merge_verification") or self.data.get("ci_failed")
                or self.data.get("catch_up_hold") or self.data.get("denied_feedback")):
            return None
        stale = self.data.get("stale_actions", ())
        if focused:
            source = next((a for a in reversed(ledger.actions(self.lease)) if a["op"] == "checks.run"
                           and a["id"] != self.executing and a["state"] != "accepted"
                           and not a["result"].get("reused_checks")), {})
            result = source.get("result", {})
            stamp = result.get("input_stamp", {})
            failures = result.get("failed_checks", [])
            if (source.get("state") == "succeeded" and source["id"] not in stale and result.get("passed") is False
                    and result.get("scope") == "failed" and failures
                    and all(f.get("selectors") and not f.get("capability") for f in failures)
                    and result.get("policy") == self.job.policy("checks", {"focused": focused})
                    and stamp.get("acceptance") == self.data["acceptance"] and stamp.get("policy") == self.policy_sha()
                    and stamp.get("tree") and stamp["tree"] == self.fingerprint()):
                return {"reused_checks": source["id"], "passed": False, "scope": "failed", "full": False,
                        "checks_executed": [], "failed_checks": failures, "remaining_checks": self.data["remaining_checks"],
                        "next": "Repair the recorded failure before retrying the focused check."}
            return None
        result = evidence.get("result", {})
        if (not result.get("full") or self.data.get("remaining_checks") or evidence.get("producer") in stale
                or not self.current("checks")):
            return None
        checked = result.get("checked", {})
        trees = pipeline.checkout_trees(self.job.checkouts)
        recorded_trees = checked.get("trees")
        if (result.get("policy") not in {self.job.policy("checks", {"quick": q}) for q in (False, True)}
                or checked.get("commands") != self.job.layer.checks or not checked.get("tree")
                or checked["tree"] != pipeline.check_tree(trees) or evidence.get("tree") != ledger.digest(trees)
                or checked.get("report") != result.get("report")):
            return None
        if (recorded_trees is not None or any(c.recovery_head for c in self.job.checkouts)) and recorded_trees != trees:
            return None
        b = self.job.b
        staged = b.restage()
        if staged.denied:
            return None
        b.validate_diff(staged, "the build and its fixes")
        b.checked, b.report = checked, checked["report"]
        return {"reused_checks": evidence["producer"], "passed": True, "full": True, "scope": "full",
                "checks_executed": [], "failed_checks": [], "remaining_checks": []}

    def latest_code_review(self):
        stale = set(self.data.get("stale_actions", ()))
        return next((a for a in reversed(ledger.actions(self.lease))
                     if a["op"] == "review.run" and a["id"] not in stale
                     and a["state"] != "accepted" and a["id"] != self.executing
                     and not (a["result"].get("reused_review") or a["result"].get("review_deferred"))), {})

    def applicable_report(self, source):
        stamp = source.get("result", {}).get("evidence", {})
        report = stamp.get("result", {})
        if (source.get("state") == "succeeded" and report.get("method") == code_review.METHOD
                and stamp.get("acceptance") == self.data["acceptance"]
                and self.matches_policy(stamp.get("policy"), self.policy_sha())
                and report.get("bases") == code_review.bases(self.job.checkouts)):
            return report
        return {}

    def decide_findings(self, request):
        if not self.uses_code_review:
            raise ValueError("this workflow uses the original independent review policy")
        source = self.latest_code_review()
        report = self.applicable_report(source)
        if source.get("id") != request["review_id"] or report.get("verdict") != "pending":
            raise ValueError("decisions require the latest completed review for this issue, base and policy")
        if not self.full_checks_passed():
            raise ValueError("run the full configured checks on the candidate before deciding findings")
        judged = code_review.disposition(report, request["decisions"], code_review.candidate_trees(self.job.checkouts))
        self.stamp("review", judged.asdict())
        self.job.review = judged
        pipeline.note_review(self.run, judged, self.job.verified)
        return judged.asdict()

    def full_checks_passed(self):
        return self.current("checks") and self.data["evidence"]["checks"].get("result", {}).get("full") is True

    def current(self, gate, *, tree=None):
        if gate == "checks" and self.data.get("denied_feedback"):
            return False
        found = self.data.get("evidence", {}).get(gate, {})
        if not (found and found["tree"] == (self.fingerprint() if tree is None else tree)
                and self.matches_policy(found["policy"], self.policy_sha())
                and found.get("acceptance") == self.data["acceptance"]):
            return False
        result = found.get("result", {})
        if gate == "verify" and (promise := result.get("delivery_obligation")):
            if (not self.is_spec or not (done := self.data.get("definition"))
                    or promise != spec_delivery.obligation(self.run.repo.name, self.job.ticket, done, self.run.spec_identity)):
                return False
            if any(not spec_delivery.confirmed_observation(self.run.tracker, self.run.tid, r["text"], r["comment"])
                   for r in result.get("delivery_directions", {}).values()):
                return False
        if gate == "checks" and not hosted_check.current(result):
            return False
        if gate == "verify" and result.get("verification_environment"):
            setting = self.catalog.settings.get(result["verification_route"])
            return bool(setting and result["verification_environment"] == verification_attempts.environment(
                verification_attempts.host(), verification_attempts.capabilities(self.job.b, setting)))
        return True

    def verification_guidance(self, report, tools, unattempted, history):
        remaining = verification_attempts.outstanding(report)
        guidance = {"remaining_requirements": remaining, "observed_requirements": sorted(verification_attempts.observed(report))}
        if report.verdict != "blocked":
            return guidance
        required = ("verify" in self.required_gates() or self.job.requires_behavior_on_block
                    or not verification_attempts.advisory(report, self.job.goal + "\n" + self.job.ticket.title + "\n" + self.job.ticket.body))
        routes = []
        for key, setting in self.catalog.settings.items():
            if setting.step != "verify":
                continue
            candidate = verification_attempts.capabilities(self.job.b, setting)
            if verification_attempts.supplies_missing(tools, candidate, report.missing_tools) and not (
                    verification_attempts.already_blocked(history, verification_attempts.environment(
                        verification_attempts.host(), candidate), remaining)):
                routes.append(key)
        if report.nonblocking_reason:
            next_action = "publication.request"
        elif routes or unattempted:
            next_action = "verification.run"
        else:
            next_action = "owner.ask" if required else "verification.decide"
        return {**guidance, "unattempted_requirements": unattempted, "missing_capability": report.missing_capability or report.blocked_by,
                "verification_required": required, "capable_routes": routes,
                "next_action": next_action}

    def verification_attempt(self, args):
        setting = self.catalog.route(args["route_id"], args["effort"], ("verify",))
        tools = verification_attempts.capabilities(self.job.b, setting)
        host = verification_attempts.host()
        environment = verification_attempts.environment(host, tools)
        tree = self.fingerprint()
        requirements = verification_attempts.names(args.get("requirements", []))
        stamp = self.data.get("evidence", {}).get("verify", {})
        source = stamp.get("result", {}) if self.current("verify") else {}
        done = self.data.get("definition") if self.is_spec else None
        promise = spec_delivery.obligation(self.run.repo.name, self.job.ticket, done, self.run.spec_identity) if done else {}
        if promise:
            self.data["protected_spec_source"] = promise["source"]
        elif done and self.data.get("protected_spec_source") == spec_delivery.source_version(self.job.ticket):
            raise factory.Refused("the protected delivery enrollment for this spec is missing")
        carried = self.data.get("required_verification_requirements", [])
        directions = spec_delivery.legacy_directions(self.lease, done, carried, promise, self.data.get("superseded_actions", [])) if promise else {}
        confirmed = spec_delivery.confirmed_directions(self.run.tracker, self.run.tid, directions) if directions else {}
        if promise and (source.get("delivery_obligation") != promise or confirmed and source.get("delivery_directions") != confirmed):
            source = {}  # Historical stamps remain available for inspection. Only a fresh baseline can bind this promise.
        scope = verification_attempts.plan(source, requirements, host, tools)
        prior, pending = scope.prior, scope.requirements
        unattempted = source.get("unattempted_requirements", [])
        history = dict(source.get("blocked_environments", {})) if prior else {}
        if scope.reuse:
            guidance = self.verification_guidance(prior, source["verification_tools"], unattempted, history)
            if source.get("owner_delivery_directions"):
                guidance.update(owner_delivery_directions=source["owner_delivery_directions"],
                                delivery_direction_help=source["delivery_direction_help"])
            return {"reused_verification": stamp["producer"], "verdict": prior.verdict,
                    "retry_blocked": prior.verdict == "blocked", **guidance}
        refs = args["goal_refs"]
        detail = "\n\nReference material, not owner authority:\n" + "\n".join(
            self.catalog.read(ref, 0, 8000)["text"] for ref in refs) if refs else ""
        goal = self.job.goal
        outstanding_required = [name for name in carried if name not in confirmed]
        if confirmed:
            outstanding_required += [check for name in confirmed for check in directions[name]["checks"]]
            detail += ("\n\nThe publisher assigned these historical observations to the protected delivery receipt. "
                       "Repeat every previous candidate check independently on the current tree.\n" + "\n".join(confirmed))
        if done:
            outstanding_required = list(dict.fromkeys([done["criterion"], *outstanding_required]))
            if promise:
                detail += spec_delivery.candidate_instructions(promise)
        if not pending and (outstanding_required or scope.baseline_targets):
            detail += ("\n\nOn the full baseline, include these requirements and use their exact wording in 'what'.\n"
                       + "\n".join(f"- {name}" for name in dict.fromkeys([*outstanding_required, *scope.baseline_targets])))
        if pending:
            detail += ("\n\nFor this follow-up, use the product only for the following requirements. Return one check "
                       "for each with its exact wording in 'what'. The engine retains previously measured behavior.\n"
                       + "\n".join(f"- {name}" for name in pending))
        try:
            self.job.goal = goal + detail
            with self.selected(setting):
                proven = verification_attempts.observed(prior) if pending else set()
                covered = tuple(c["what"] for c in prior.checks if (c.get("driver") or c.get("probe"))
                                and c.get("passed") and c["what"].casefold() in proven) if pending else ()
                outcome = self.atomic("verify", tree=str(self.job.ws), single=True, covered_commands=covered)
        finally:
            self.job.goal = goal
        if not tree or tree != self.fingerprint() or host != verification_attempts.host():
            raise ValueError("the candidate or environment changed during verification; run a new baseline")
        raw = Verified.stored(outcome.data["verified"])
        if promise or raw.pending_delivery:
            if promise and promise != spec_delivery.obligation(self.run.repo.name, self.job.ticket, done, self.run.spec_identity):
                raise factory.Refused("the protected delivery obligation changed during candidate verification")
            raw = spec_delivery.candidate_report(raw, promise)
        fresh = verification_attempts.unattempted(raw)
        measured = verification_attempts.partial(raw)
        unattempted = [name for name in dict.fromkeys([*(unattempted if pending else []), *fresh])
                       if name.casefold() not in {target.casefold() for target in pending}]
        if pending:
            measured = verification_attempts.combine(prior, measured, pending)
        observed = verification_attempts.observed(measured)
        owed = [name for name in dict.fromkeys([*outstanding_required, *scope.baseline_targets]) if name.casefold() not in observed]
        if owed:
            absent = [name for name in owed if name not in verification_attempts.outstanding(measured)]
            measured = verification_attempts.partial(replace(measured, unmeasured_requirements=list(dict.fromkeys(
                [*measured.unmeasured_requirements, *absent]))))
            unattempted = list(dict.fromkeys([*unattempted, *absent]))
        elif outstanding_required:
            self.data.pop("required_verification_requirements", None)
            self.data.pop("verification_required_for", None)
        if measured.saved and (pending or measured.verdict != outcome.data["verified"]["verdict"]):
            self.run.save(Path(measured.saved).name, measured.markdown())
        if measured.verdict == "blocked":
            attempted = {name.casefold() for name in verification_attempts.outstanding(measured)} - {
                name.casefold() for name in unattempted}
            history[environment] = sorted(set(history.get(environment, [])) | attempted)
        result = {**asdict(measured), "blocked_environments": history, "verification_environment": environment, "verification_host": host,
                  "verification_tools": tools, "verification_requirements": pending, "verification_route": args["route_id"],
                  "unattempted_requirements": unattempted}
        self.job.verified = measured
        guidance = self.verification_guidance(measured, tools, unattempted, history)
        if measured.verdict == "fail" or guidance.get("verification_required"):
            self.remember_required_verification(measured, self.data["acceptance"])
        elif flows.retry_passed(measured):
            self.data.pop("verification_required_for", None)
        result.update(guidance)
        if confirmed:
            result["delivery_directions"] = confirmed
        unresolved = {name: rule["text"] for name, rule in directions.items() if name not in confirmed}
        if unresolved and measured.verdict == "blocked":
            result["owner_delivery_directions"] = unresolved
            result["delivery_direction_help"] = ("If a historical gap refers solely to the enrolled delivery receipt, "
                "ask its publisher to post the exact directive on the spec ticket. Otherwise it still needs a candidate check.")
        self.stamp("verify", result)
        return {"operation_passed": outcome.passed, **result}

    def decide_verification(self, args):
        stamp = self.data.get("evidence", {}).get("verify", {})
        if (not self.uses_code_review or not self.current("verify") or stamp.get("producer") != args["verification_id"]
                or "verify" in self.required_gates()):
            raise ValueError("a nonblocking decision requires a current advisory verification gap")
        report = self.job.verified
        authority = self.job.goal + "\n" + self.job.ticket.title + "\n" + self.job.ticket.body
        if not verification_attempts.advisory(report, authority) or self.job.requires_behavior_on_block:
            raise ValueError("required or unclassified behavior cannot be marked nonblocking")
        self.job.verified = replace(report, nonblocking_reason=args["reason"].strip(),
                                    nonblocking_trees=code_review.candidate_trees(self.job.checkouts))
        result = {**stamp["result"], **asdict(self.job.verified)}
        self.stamp("verify", result)
        return result

    def adopt_code_review(self):
        stale = set(self.data.get("stale_actions", ()))
        stale.update(a["id"] for a in ledger.actions(self.lease)
                     if ACTION_GATES.get(a["op"]) == "review" and a["state"] != "accepted")
        self.data["stale_actions"] = sorted(stale)
        self.data.setdefault("evidence", {}).pop("review", None)
        self.job.review, self.job.reviews = pipeline.Review(), 0
        self.data["review_method"] = code_review.METHOD
        self.run.code_review = True
        self.run.log("Adopted Standards and Spec code review for this continuation.")

    def remember_required_verification(self, report, acceptance):
        self.data["verification_required_for"] = acceptance
        authority = self.job.goal + "\n" + self.job.ticket.title + "\n" + self.job.ticket.body
        supplemental = {name.casefold() for name in report.advisory_gaps
                        if name.casefold() not in pipeline.verify.one_line(authority).casefold()}
        failures = {c.get("what", "").casefold() for c in report.failed_checks if c.get("cause") != "environment"}
        required = [name for name in verification_attempts.outstanding(report)
                    if name.casefold() not in supplemental or name.casefold() in failures]
        observed = verification_attempts.observed(report)
        self.data["required_verification_requirements"] = [name for name in dict.fromkeys(
            [*self.data.get("required_verification_requirements", []), *required]) if name.casefold() not in observed]

    def restore_verification(self, result):
        stamp = result["evidence"]
        raw = Verified.stored(stamp["result"] if "verdict" in stamp["result"] else result)
        self.job.verified = raw
        if not self.uses_code_review:
            return
        report = self.job.verified = verification_attempts.partial(raw)
        if raw is not report:
            # A historic pass can explicitly list an unmeasured criterion. Preserve the immutable ledger entry.
            self.data["evidence"]["verify"] = {**stamp, "result": {
                **stamp["result"], **asdict(report), "unattempted_requirements": verification_attempts.unattempted(raw)}}
        if report.verdict == "fail" or (report.verdict == "blocked" and not report.nonblocking_reason and
                (result.get("verification_required") or not verification_attempts.advisory(report, self.job.goal))):
            self.remember_required_verification(report, stamp["acceptance"])
        elif flows.retry_passed(report) or report.nonblocking_reason or result.get("verification_required") is False:
            self.data.pop("verification_required_for", None)
            self.data.pop("required_verification_requirements", None)

    def recover(self):
        self.quiescent()
        for turn in ledger.pending(self.lease):
            ledger.reject(self.lease, turn["id"], "interrupted controller turn; admitted usage may be unknown")
        catch_up = self.data.get("catch_up_hold", {})
        stale = set(self.data.get("stale_actions", ()))
        for action in ledger.actions(self.lease):
            if action["id"] in self.data.get("superseded_actions", ()):
                continue
            result = action["result"]
            gate = ACTION_GATES.get(action["op"])
            old_gate = bool(gate) and action["id"] in stale
            if action["id"] == catch_up.get("action"):
                self.data["evidence"] = {}
            if result.get("awaiting_owner"):
                answers = [ask_store.ask_get(ask) for ask in result["ask_ids"]]
                if not answers or any(not answer or answer["status"] not in ("answered", "taken") for answer in answers):
                    raise factory.Refused("owner answers or interrupted question settlement need reconciliation before resuming")
            if action["state"] == "running":
                state = "reconciliation-required" if action["op"] == "publication.request" else "interrupted"
                ledger.transition(self.lease, action["id"], state,
                                  {**result, "error": "execution interrupted; usage may be unknown", "usage_unknown": True})
            if result.get("text") and not old_gate:
                self.catalog.add_context(action["id"], result["text"])
            if gate and not old_gate and action["state"] != "accepted" and not (
                    result.get("reused_checks") or result.get("reused_review") or result.get("reused_verification")
                    or result.get("review_deferred") or result.get("preserved_verification")
                    or action["op"] in ("review.decide", "verification.decide") and action["state"] != "succeeded"):
                self.data.setdefault("evidence", {}).pop(gate, None)
            if action["state"] == "succeeded":
                if gate == "checks" and not old_gate and self.uses_code_review and not result.get("reused_checks"):
                    if "remaining_checks" in result:
                        self.data["remaining_checks"] = result["remaining_checks"]
                    elif result.get("passed") is False:
                        self.data["remaining_checks"] = check_repair.historical_failures(
                            self.job.layer, result.get("report", ""))
                    elif result.get("full"):
                        self.data.pop("remaining_checks", None)
                if result.get("writing"):
                    self.job.build_summary = self.job.b.build_summary = result.get("text", self.job.build_summary)
                elif result.get("effective", {}).get("prompt_id", "").removeprefix("prompt:") == "plan":
                    self.job.plan_text = self.job.b.plan_text = result.get("text", self.job.plan_text)
                if gate and not old_gate and result.get("evidence"):
                    self.data.setdefault("evidence", {})[gate] = result["evidence"]
                    if gate == "checks":
                        self.job.b.checked, self.job.b.report = result.get("checked", {}), result.get("report", "")
                    elif gate == "review":
                        self.job.review = pipeline.Review.stored(result["evidence"]["result"])
                        if code_review.completed(self.job.review):
                            self.run.facts["review"] = self.job.review.report()
                    else:
                        self.restore_verification(result)
        self.data["writes"] = max(self.data.get("writes", 0), sum(bool(a["result"].get("writing"))
                                                                 for a in ledger.actions(self.lease)))
        if self.data.get("publication_intent") or any(a["state"] == "reconciliation-required"
                                                       for a in ledger.actions(self.lease)):
            raise factory.Refused("publication was interrupted; reconcile its branch/PR effects before resuming")
        for gate, value in self.data.get("evidence", {}).items():
            self.catalog.add_context(f"evidence:{gate}", ledger.encoded(value))
        if self.data.get("ci_failures"):
            self.catalog.add_context(CI_FAILURES, self.data["ci_failures"])
        if catch_up:
            self.catalog.add_context(CATCH_UP_FAILURE, catch_up["feedback"])
        if self.exchanged:
            self.recover_workers()
        for action in ledger.actions(self.lease):
            run_lines.write(self.run.id, "action", action=action["id"], op=action["op"], state=action["state"],
                            recovered=True, source=action["run"])

    def resolved_pr_conflicts(self, actions):
        # A recovery grant names the remote revisions that conflicted. Verify their local merge
        # from Git itself, including when a restart happened before the controller saved its state.
        conflicts = {url: pr for url, pr in self.run.recovery.get("binding", {}).get("prs", {}).items()
                     if pr.get("mergeable") == "CONFLICTING"}
        if not conflicts or not self.default_code_review:
            return {}
        trees = {}
        merged = {}
        for checkout in self.job.checkouts:
            trees[checkout.github] = factory.fingerprint(checkout.wt)
            pr = conflicts.get(checkout.pr)
            if not pr:
                continue
            if checkout.lease != pr.get("headRefOid") or checkout.base_commit != pr.get("baseRefOid"):
                return {}
            # Both tracking refs were fetched during preparation. A newer fetch or local edit
            # invalidates this handoff; publication independently checks the remote again.
            refs = util.git(["rev-parse", "--revs-only", checkout.origin_base, f"origin/{self.job.branch}"], checkout.wt)
            if refs.splitlines() != [checkout.base_commit, checkout.lease]:
                return {}
            head, tree, *parents = util.git(["show", "-s", "--format=%H %T %P", "HEAD"], checkout.wt).split()
            if parents != [checkout.lease, checkout.base_commit] or tree != trees[checkout.github]:
                return {}
            merged[checkout.pr] = {"pr_head": checkout.lease, "base": checkout.base_commit, "merge": head, "tree": tree}
        if merged.keys() != conflicts.keys() or not all(trees.values()):
            return {}
        stamp = {"tree": ledger.digest(trees), "policy": self.policy_sha(), "acceptance": self.data["acceptance"]}
        stale = set(self.data.get("stale_actions", ()))
        last = next((a for a in reversed(actions) if a["op"] == "checks.run" and a["id"] not in stale
                     and a["state"] in ("succeeded", "failed") and a["result"].get("input_stamp") == stamp), {})
        result = last.get("result", {})
        partial_pass = result.get("passed") is True and result.get("scope") in ("quick", "failed")
        return {"merges": merged, "checks_action": last.get("id", ""), "run_full_checks": not last or partial_pass,
                "status": "Arete resolved the conflicts locally. Use the current checks to continue recovery."}

    def observe(self, limit=20, after=0):
        all_actions = ledger.actions(self.lease)
        actions = [a for a in all_actions if a["result"].get("revision", 0) > after]
        snapshot = ledger.snapshot(self.lease)
        for worker in self.workers.values():
            self.progress[worker.action] = worker.observations()
            self.reported[worker.action] = worker.report[0]
        return {**(self.worker_observation(outstanding=True) if self.exchanged else {}),
                "version": self.protocol, "state_version": snapshot["version"], "cursor": snapshot["version"],
                "root": self.lease.root,
                **({"serial_reason": self.data["serial_reason"]} if self.data.get("serial_reason") else {}),
                "review_method": self.data.get("review_method", "legacy") if self.uses_code_review else "legacy",
                "required_gates": self.required_gates(),
                "task": self.catalog.read("task", 0, 8000), "owner_context": self.run.story[-8:],
                **({"resolved_pr_conflicts": receipt} if (receipt := self.resolved_pr_conflicts(all_actions)) else {}),
                **({"spec": {"guide": spec_delivery.GUIDANCE, "definition": self.data.get("definition")}}
                   if self.is_spec else {}),
                **({"recovery_guidance": direction} if (direction := self.run.recovery.get("direction")) else {}),
                **({"triage": self.job.triage} if self.job.triage else {}),
                "catalog_sha": self.catalog.fingerprint,
                "catalog": {kind: self.catalog.list(kind, 0, 10) for kind in ("models", "prompts", "tools", "context")},
                "actions": [{**{k: a[k] for k in ("id", "op", "state")},
                             "result": self.excerpt(a)} for a in actions[-limit:]],
                "progress": self.progress, "evidence": {name: {**value, "current": self.current(name)}
                                                        for name, value in self.data.get("evidence", {}).items()},
                "limits": {**self.configured_limits(), "max_decisions": self.decision_limit,
                           "max_actions": self.action_limit, "decisions_used": snapshot["turns"],
                           "launches_used": snapshot["launches"], "actions_used": len(all_actions),
                           "repair_cycles": self.run.budget.repair_calls if self.run.budget else 0},
                "error": self.data.get("error", "")}

    def excerpt(self, action):
        if action["id"] in self.data.get("superseded_actions", ()):
            return {"superseded": "the owner revised the ticket requirements"}
        text = ledger.encoded(action["result"])
        if len(text) <= 4000:
            return action["result"]
        self.catalog.add_context(action["id"], text)
        return {"ref": action["id"], "excerpt": text[:2000], "characters": len(text)}

    def controller_turn(self, turn, request, retried):
        controller = delegation.Worker(self.run, turn, request)
        result = {"ok": False, "error": "controller exited without a decision; usage may be unknown"}
        complete = False
        try:
            ledger.turn_started(self.lease, turn, controller.pid, controller.identity)
            while not complete:
                self.collect()
                for message in controller.receive():
                    if message.get("event") == "launch":
                        if self.run.budget and not retried:
                            self.run.budget.charge(message["step"], message["role"])
                        grant = self.run.launch_gate(message["step"], message["role"], message["route"]) \
                            if self.run.launch_gate else None
                        if not retried:
                            ledger.launched(self.lease, self.decision_limit)
                        if self.run.feedback and not self.run.step_started:
                            run_lines.write(self.run.id, "work_start")
                        controller.allow(grant)
                        self.run.step_started = True
                    else:
                        result, complete = message, True
                if controller.proc.poll() is not None:
                    complete = True
                if not complete:
                    time.sleep(0.05)
            self.run.cost += result.get("cost_usd", 0)
            self.run.paid += result.get("paid_usd", 0)
        finally:
            controller.close()
            if not complete:
                ledger.reject(self.lease, turn, "controller stopped; admitted usage may be unknown")
            if self.run.launch_done:
                self.run.launch_done(controller.step, bool(result.get("ok")))
        return result

    @property
    def controller_prompt(self):
        return "orchestrate_workers" if self.exchanged else "orchestrate"

    def decide(self):
        observation = self.observe()
        turn = ledger.begin(self.lease, observation, self.decision_limit)
        observation["decision_id"] = turn
        receipt = observation.get("resolved_pr_conflicts")
        if (receipt and receipt["run_full_checks"] and not (self.run.answers_due or self.run.feedback or self.workers)
                and all(w["phase"] in exchange.CLOSED for w in observation.get("workers", []))):
            response = {"version": self.protocol, "decision_id": turn, "state_version": observation["state_version"],
                        "rationale": "Check the locally resolved PR before continuing recovery.",
                        "calls": [{"op": "checks.run", "args": {"scope": "full"}}]}
            return ledger.accept(self.lease, turn, observation["state_version"], response, self.action_limit)
        place = self.run.dir / "controller"
        place.mkdir(exist_ok=True)
        prompt = prompt_loader.prompt(self.controller_prompt, api=help_text(self.protocol, spec=self.is_spec),
                                      observation=json.dumps(observation))
        result = {}
        for retry, setting in enumerate(stall_routes(self.run.settings("orchestrate"))):
            request = delegation.request(self.job, turn, prompt, setting, (), [])
            request.update(step=f"orchestrate-{turn}", cwd=str(place))
            if self.issue_goal:
                request.update(goal_ticket=str(self.run.tid), goal_event=f"{self.run.id}/{turn}")
            result = self.controller_turn(turn, request, retried=retry > 0)
            if not stalled_before_reply(result):
                break
            self.run.log(f"{request['step']}: no reply from {setting.runner} {setting.model}: "
                         f"{util.first_line(str(result.get('error') or ''), 200)}")
        stalled = stalled_before_reply(result)
        if not result.get("ok"):
            error = str(result.get("error") or "controller returned no decision")[:2000]
            ledger.reject(self.lease, turn, error)
            self.data["error"] = error
            self.persist()
            raise workspace.RunFailed(error, recovery={"reason": CONTROLLER_STALL} if stalled else result.get("recovery"))
        self.run.step_started = True
        try:
            response = result.get("structured")
            if response is None:
                response = json_from_text(result.get("text", ""))
            calls = self.catalog.validate(response, turn, observation["state_version"],
                                          {a["id"]: a for a in ledger.actions(self.lease)
                                           if a["id"] not in self.data.get("superseded_actions", ())})
            if self.exchanged:
                self.preflight(calls["calls"])
            actions = ledger.accept(self.lease, turn, observation["state_version"], calls, self.action_limit)
            self.data.pop("error", None)
            if self.run.answers_due:
                ask_store.asks_taken([a["id"] for a in self.run.answers_due], self.run.id)
                self.run.answers_due = []
            return actions
        except (ValueError, TypeError) as error:
            ledger.reject(self.lease, turn, str(error)[:2000])
            self.data["error"] = str(error)[:2000]
            self.persist()
            return []

    def barrier(self):
        if self.workers:
            raise ValueError("workers are still active; wait for confirmed completion or cancellation first")

    def worker_start(self, action, args, prior=None):
        known = {item["id"]: item for item in ledger.actions(self.lease)}
        if not self.exchanged and any(known[dep]["state"] != "succeeded" for dep in args["depends_on"]):
            raise ValueError("a dependency has not succeeded")
        setting, name, writing = self.catalog.worker(args)
        writing = writing and args["tool_bundle"] == "write"
        if self.is_spec and writing and not self.data.get("definition"):
            raise ValueError("record the spec's definition of done or ask the owner before implementation")
        if len(self.workers) >= self.policy.max_workers or (not self.exchanged and self.workers and (
                writing or any(worker.writing for worker in self.workers.values()))):
            raise ValueError("worker or workspace mutation lease is busy; wait before starting this delegation")
        role = setting.step
        catch_up = self.data.get("catch_up_hold", {})
        denied_feedback = self.data.get("denied_feedback", "")
        assignment = None
        if self.exchanged:
            ws = exchange.workers(self.lease)
            for dep in args["depends_on"]:
                owner = known[dep]["result"].get("worker_id")
                ready = ws[owner]["phase"] in {"integrated", "completed"} if owner else known[dep]["state"] == "succeeded"
                if not ready:
                    raise ValueError(f"dependency {dep} is unfinished")
            if writing and sum(w.writing for w in self.workers.values()) >= self.policy.builder_limit:
                raise ValueError("builder capacity is full")
            logical_id, assignment = self.private_assignment(action, args, prior, writing, role)
            role = assignment["role"]
        if not self.exchanged and writing and (self.data.get("writes", 0) or self.run.repairs or catch_up or denied_feedback):
            role = ("ci_repair" if self.data.get("ci_failed") else "review_repair"
                    if catch_up.get("gate") == "review" or self.job.review.verdict == "request_changes"
                    else "check_repair")
        if role != setting.step:
            setting = pipeline.steps.resolve(self.job.cfg, self.job.layer, role, self.run.kind,
                                             {"runner": setting.runner, "model": setting.model,
                                              "provider": setting.provider, "effort": setting.effort,
                                              **(self.catalog.private_route if assignment else {})})
        tools = tuple(tool for tool in BUNDLES[args["tool_bundle"]] if tool in setting.tools)
        if writing and not {"Write", "Edit"} & set(tools):
            raise ValueError("this role grants no implementation writing tools")
        contexts = "\n\n".join(self.catalog.read(ref, 0, 8000)["text"] for ref in args["context_refs"]
                               if ref != CI_FAILURES and not (ref == CATCH_UP_FAILURE and writing and catch_up))
        if self.data.get("ci_failures") and (self.data.get("ci_failed") or CI_FAILURES in args["context_refs"]):
            contexts = "\n\n".join(filter(None, (contexts, self.data["ci_failures"][:MAX_CI_FAILURE_CHARS])))
        if writing and catch_up:
            contexts = "\n\n".join(filter(None, (contexts, catch_up["feedback"])))
        if prior:
            contexts += "\n\nPrevious worker result:\n" + prior["result"].get("text", "")[:8000]
        goal = args["goal"]
        if not self.exchanged:
            queued = self.data.setdefault("steering", {}).pop((prior or action)["id"], [])
            goal += "\n" + "\n".join(queued) if queued else ""
        if name in ("delegate", "investigate") and self.job.triage:
            contexts = "\n\n".join(filter(None, (self.job.triage, contexts)))
        direction = self.run.recovery.get("direction")
        if (writing or name == "plan") and isinstance(direction, str) and direction:
            contexts += "\n\nIssue orchestrator guidance (does not change acceptance criteria):\n" + direction
        inputs = {"goal": goal, "context": contexts, "plan": self.job.plan_text, "checks": self.job.checks,
                  "denied": prompt_loader.code_list(diffguard.denied_paths(self.job.layer)),
                  "estimate": self.job.estimate, "verify": self.job.goal, "triage": self.job.triage, **args["inputs"]}
        if name == "plan" and self.job.layer.verify:
            inputs["journeys"] = journeys.guidance(self.job.layer.verify.feature_map, planning=True)
        if "verify" in args["inputs"] and pipeline.triage_checks(self.job.approval):
            inputs["verify"] = pipeline.triage_goal(str(inputs["verify"]), self.job.approval)
        if assignment:
            private = builder_workspaces.folder(assignment["candidate"]["token"])
            inputs.update(workspace=str(private), class_check="")
        if not writing:
            inputs["class_check"] = ""
        if self.is_spec and (done := self.data.get("definition")):
            inputs["goal"] += "\n\n" + spec_delivery.describe(done)
            inputs["verify"] = self.job.goal
        prompt = self.job.brief.prompt("delegate" if name in ("delegate", "investigate") else name, **inputs)
        if assignment:
            messages = exchange.inbox(self.lease, logical_id)
            prompt += worker_turns.instructions(assignment, messages)
        if writing and denied_feedback:
            prompt += f"\n\n## Check failure to repair\n\n{denied_feedback}"
        session = NO_SESSION
        previous = (prior or {}).get("result", {})
        if not assignment and previous.get("runner") == setting.runner and previous.get("model") == setting.model:
            session = Session(**previous.get("session", {}))
        request = delegation.request(self.job, action["id"], prompt, setting, tools,
                                     prompt_loader.bash_rules(self.job.builder_bash) if writing else [], session)
        if assignment:
            if self.catalog.private_route:
                request["overrides"].update(self.catalog.private_route)
            request.update(cwd=str(private), private_worker=True, offered_ids=[m["id"] for m in messages],
                           evidence_refs=args["context_refs"], closed_context=[
                *self.run.closed_context, str(self.job.ws), *(str(c.src) for c in self.job.checkouts)])
        if writing:
            b = self.job.b
            if not b.worker_trees and not denied_feedback:
                b.worker_trees = {c.github: factory.fingerprint(c.wt) for c in b.checkouts}
                if not all(b.worker_trees.values()):
                    b.worker_trees.clear()
                    raise ValueError("the workspace could not be recorded before the writing worker")
                self.data["worker_trees"] = b.worker_trees
                self.persist()
        worker = delegation.Worker(self.run, action["id"], request)
        worker.writing, worker.effective = writing, args
        try:
            receipt = {"pid": worker.pid, "identity": worker.identity, "effective": args, "writing": writing, "role": role}
            if assignment:
                worker.logical_id, worker.role, worker.admitted = logical_id, role, False
                worker.offered = [m["id"] for m in messages]
                exchange.started(self.lease, logical_id, action["id"], receipt)
            else:
                ledger.transition(self.lease, action["id"], "running", receipt)
        except BaseException:
            worker.close()
            raise
        self.workers[action["id"]] = worker
        if writing:
            self.data["writes"] = self.data.get("writes", 0) + 1

    def collect(self):
        changed = False
        for key, worker in list(self.workers.items()):
            for message in worker.receive():
                if message.get("event") == "launch":
                    budget = self.run.budget
                    if self.exchanged and budget and worker.role in runlimits.REPAIRS and not budget.issue_goal:
                        reserved = sum(other is not worker and other.role in runlimits.REPAIRS and not other.admitted
                                       for other in self.workers.values())
                        if budget.repair_calls + budget.prior_repairs + reserved >= budget.policy.max_repair_cycles:
                            worker.refuse("the remaining repair allowance is exhausted or reserved for admitted siblings")
                            continue
                    if self.run.budget:
                        self.run.budget.charge(message["step"], message["role"])
                    grant = self.run.launch_gate(message["step"], message["role"], message["route"]) \
                        if self.run.launch_gate else None
                    seconds = grant.get("timeout") if isinstance(grant, dict) else None
                    if type(seconds) in (int, float) and seconds > 0:
                        worker.deadline = min(worker.deadline, time.monotonic() + seconds)
                    if self.run.feedback and not self.run.step_started:
                        run_lines.write(self.run.id, "work_start")
                    if self.exchanged and not worker.admitted:
                        exchange.dispatched(self.lease, worker.logical_id, key, worker.offered)
                        worker.admitted = True
                    worker.allow(grant)
                    self.run.step_started = True
                    if (self.run.budget and worker.writing and message["role"] in runlimits.REPAIRS
                            and self.data.get("denied_feedback")):
                        self.data.pop("denied_feedback")
                        self.job.b.denied_feedback = ""
                        self.persist()
                elif message.get("event") in ("result", "error"):
                    worker.close()
                    del self.workers[key]
                    if self.run.launch_done:
                        self.run.launch_done(worker.step, bool(message.get("ok")))
                    self.run.cost += message.get("cost_usd", 0)
                    self.run.paid += message.get("paid_usd", 0)
                    ticket = getattr(getattr(self, "job", None), "ticket", None)
                    if reason := spec_graph_delivery.active_child_blocker(self.run, ticket):
                        if self.exchanged:
                            exchange.cancel(self.lease, worker.logical_id, key, reason)
                        else:
                            ledger.transition(self.lease, key, "cancelled", {"reason": reason, "usage_unknown": False})
                        raise workspace.RunFailed(reason)
                    result = {**message, "effective": worker.effective, "writing": worker.writing}
                    if self.exchanged:
                        phase = self.settle_turn(worker, message)
                        run_lines.write(self.run.id, "action", action=key, worker=worker.logical_id, phase=phase)
                        changed = True
                        continue
                    if worker.writing:
                        checkouts = self.job.checkouts
                        excluded = self.run.closed_checkout_paths
                        workspace.note_made(self.job.ws, checkouts,
                                            pipeline.workspace_owner(self.run, self.job.ws, checkouts), excluded=excluded)
                        if why := workspace.kept_work(self.job.ws, checkouts, self.run.repo.path,
                                                       self.job.layer.copy, commits=False,
                                                       held=workspace.scratch_left(self.job.ws, checkouts),
                                                       excluded=excluded):
                            raise workspace.worktree_failure(why, self.job.ws, self.job.branch)
                    ledger.transition(self.lease, key, "succeeded" if message.get("ok") else "failed", result)
                    self.catalog.add_context(key, message.get("text", message.get("error", "")))
                    if worker.writing:
                        self.job.build_summary = message.get("text", self.job.build_summary)
                        self.job.b.build_summary = self.job.build_summary
                    elif worker.effective["prompt_id"].removeprefix("prompt:") == "plan":
                        self.job.plan_text = message.get("text", self.job.plan_text)
                        self.job.b.plan_text = self.job.plan_text
                    self.persist()
                    run_lines.write(self.run.id, "action", action=key,
                                    state="succeeded" if message.get("ok") else "failed")
                    changed = True
            if key in self.workers and worker.proc.poll() is not None:
                worker.close()
                del self.workers[key]
                if self.run.launch_done:
                    self.run.launch_done(worker.step, False)
                if self.exchanged:
                    self.settle_turn(worker, {"event": "error", "error": "worker ended without a durable result"})
                    changed = True
                    continue
                ledger.transition(self.lease, key, "interrupted", {"effective": worker.effective,
                                  "error": "worker ended without a durable result", "usage_unknown": True})
                changed = True
        return changed

    def wait_workers(self, timeout_seconds):
        started = time.monotonic()
        run_deadline = self.run.budget.run_deadline() if self.run.budget else inf
        progress_at = self.last_progress + self.policy.progress_seconds
        slept = False
        while self.workers:
            if time.monotonic() >= run_deadline:
                raise runlimits.Parked(self.run.budget.reason())
            if self.collect():
                break
            deadline = min(run_deadline, min(worker.deadline for worker in self.workers.values()))
            if timeout_seconds and timeout_seconds < deadline - started:
                deadline = started + timeout_seconds
            now = time.monotonic()
            if now >= run_deadline:
                raise runlimits.Parked(self.run.budget.reason())
            if now >= progress_at or now >= deadline:
                reports = {}
                for key, worker in self.workers.items():
                    self.progress[key] = worker.observations()
                    count, report = worker.report
                    if count > self.reported.get(key, 0):
                        reports[key] = report
                        self.reported[key] = count
                if reports:
                    self.last_progress = now
                    return {"active": list(self.workers), "progress": reports}
                progress_at = now + 1
            if now >= deadline:
                return {"active": list(self.workers), "reason": "timeout" if slept else "deadline"}
            delay = min(0.05, deadline - time.monotonic())
            if delay > 0:
                time.sleep(delay)
                slept = True
        return {"active": list(self.workers)}

    @contextmanager
    def selected(self, setting):
        previous = self.run.resolved_settings
        self.run.resolved_settings = {**previous, setting.step: setting}
        try:
            yield
        finally:
            self.run.resolved_settings = previous

    def validate_publication_diff(self):
        b = self.job.b
        staged = b.restage()
        checkouts = self.job.checkouts
        after = "the final orchestrated tree"
        if any(c.lease for c in checkouts):
            bases = {
                c.github: (c.base_commit if c in staged.merged else pipeline.base_of_head(c))
                for c in checkouts
            }
            if not any(util.git(["diff", "--cached", "--name-only", bases[c.github]], c.wt) for c in checkouts):
                raise workspace.RunFailed(f"{after} undid every change")
            heads = {c.github: head for c in checkouts if (head := c.publication_baseline)}
            if not staged.merged and heads and not any(
                util.git(["diff", "--cached", "--name-only", heads.get(c.github, bases[c.github])], c.wt)
                for c in checkouts
            ):
                merge_verification = self.run.recovery.get("merge_verification") is True
                recovered = [c for c in checkouts if c.recovery_head]
                if (any(pr_recovery.current_head(c) != c.lease for c in recovered)
                        or ((self.run.environment_release or merge_verification or recovered) and not self.data.get("ci_failed")
                            and flows.retry_passed(self.job.verified) and any(c.pr and c.github in heads for c in checkouts))):
                    return b.validate_diff(staged if b.from_base else b.restage(from_base=True), after, from_base=True)
                label = "head" if len(heads) == 1 else "heads"
                shas = ", ".join(f"`{head}`" for head in heads.values())
                raise UnchangedPushedHead(f"nothing changed since the pushed {label} {shas}")
        b.validate_diff(staged, after)

    def publish(self, unverified_measurement: dict | None = None):
        if self.exchanged:
            self.worker_barrier()
        self.barrier()
        if pending := ledger.catch_up_push(self.data):
            self.publication_scope()
            return self.reconcile_catch_up(pending)
        if self.data.get("publication_intent"):
            raise factory.Refused("publication was interrupted; reconcile its branch/PR effects before resuming")
        if self.is_spec:
            if not (done := self.data.get("definition")) or self.job.left_for_a_person or unverified_measurement:
                raise ValueError("a spec needs one complete definition of done and cannot ship a partial or unverified outcome")
            if spec_delivery.source_version(self.spec_ticket()) != self.data["spec_source"]:
                raise factory.Refused("the published spec changed after the definition of done was recorded")
            promise = spec_delivery.obligation(self.run.repo.name, self.job.ticket, done, self.run.spec_identity)
            if self.job.verified.delivery_obligation != promise or (not promise and
                    self.data.get("protected_spec_source") == self.data["spec_source"]):
                raise factory.Refused("the protected delivery obligation changed before publication")
            if not spec_delivery.behavior(self.job.verified, done):
                raise ValueError("verify the exact spec criterion in use and record how it was checked and what was observed")
        recovering = any(c.recovery_head for c in self.job.checkouts)
        tree = self.fingerprint() if recovering else None
        gates = self.required_gates()
        needed = [gate for gate in gates if not self.current(gate, **({"tree": tree} if recovering else {}))]
        if needed:
            raise ValueError("publication needs current evidence: " + ", ".join(needed))
        checks = self.data["evidence"]["checks"]["result"]
        if not checks.get("full"):
            raise ValueError("publication requires the full owner check policy")
        if recovering and ledger.digest(checks.get("checked", {}).get("trees", {})) != tree:
            raise ValueError("recovered publication requires fresh full checks fingerprinted for every participating checkout")
        if self.job.verified.verdict == "fail":
            raise ValueError("behavioral verification failed; repair or finish failed")
        if self.uses_code_review:
            recorded = self.data["evidence"]["review"]["result"]
            if (recorded.get("verdict") != "approve" or not self.job.review.approved()
                    or not code_review.completed(self.job.review)):
                raise ValueError("review findings need an explicit orchestrator decision before publication")
            if (self.job.review.trees != code_review.candidate_trees(self.job.checkouts)
                    or self.job.review.asdict().get("bases") != code_review.bases(self.job.checkouts)):
                raise ValueError("the candidate or comparison base changed since the review decision")
            if (self.current("verify") and self.job.verified.verdict == "blocked"
                    and not self.job.verified.nonblocking_reason):
                raise ValueError("resolve the missing verification capability or explicitly settle an advisory gap")
            if "verify" in self.required_gates() and not flows.retry_passed(self.job.verified):
                raise ValueError("the configured behavioral check must pass")
            if "verify" not in self.required_gates() and not self.current("verify"):
                self.job.verified = code_review.unverified()
            pipeline.note_review(self.run, self.job.review, self.job.verified)
        catch_up = self.data.get("catch_up_hold")
        if catch_up and (("verify" in self.required_gates() and not flows.retry_passed(self.job.verified))
                         or not self.job.review.approved()):
            raise ValueError("publication after a catch-up hold requires passing verification and an approving review")
        measurement_section = (pipeline.unverified_measurement_section(unverified_measurement)
                               if unverified_measurement else "")
        if (not self.run.dry_run and not zte.reserved(str(self.run.tid)) and self.data.get("ci_failed")
                and (retry := self.data.get("ci_retry")) and (tree := tree or self.fingerprint())
                and tree == self.data.get("published_tree")):
            return self.retry_publication_ci(retry, tree, measurement_section=measurement_section)
        self.publication_scope()
        self.validate_publication_diff()
        if self.run.dry_run:
            self.ending = flows.end_dry_run(self.flow, measurement_section=measurement_section)
            return {"status": "dry-run"}
        if zte.reserved(str(self.run.tid)):
            grant = zte.prepare_candidate(self.run, self.job, self.data)
            self.ending = ("pr", "Prepared an exact README candidate for engine-owned delivery", "")
            return {"prepared_head": grant.data["head"], "tree": grant.data["tree"]}
        self.check_pilot_publication()
        publication = None
        if recovering:
            publication = self.recovered_publication()
            if tree != self.fingerprint() or tree != ledger.digest(publication.trees):
                raise ValueError("the staged candidate does not match the reconciled tree's gates; refresh the gates")
            self.job.recovery_publication = publication
        if catch_up:
            checkout, = self.job.checkouts
            self.take_merge_lease(checkout)
        self.begin_publication(tree if recovering else self.fingerprint())
        outcome = self.atomic("pr", tree=str(self.job.ws), measurement_section=measurement_section)
        if not outcome.passed:
            self.ending = flows.ended(self.flow, outcome)
            return outcome.data
        self.data.pop("worker_trees", None)
        self.job.b.worker_trees = {}
        self.data.pop("ignored_denied_paths", None)
        self.job.b.ignored_denied_paths = {}
        changed = self.job.picked(self.job.changed)
        if not publication:
            self.data.pop("publication_intent", None)
            self.data["publication"] = outcome.data
            self.persist()
            if catch_up:
                for c in changed:
                    pipeline.publish_gate_evidence(self.run, self.job.ticket, c, self.job.verified, self.job.review)
        failure = self.publication_ci(changed) if self.run.repo.parts else None
        if publication:
            publication.recheck(during="before recording publication")
            self.data["publication"] = outcome.data
            if self.job.with_prs:
                pipeline.note_prs(self.run, self.job.branch, self.job.with_prs)
        if failure:
            self.keep_failed_publication()
            return self.settle_publication_failure(failure)
        return self.finish_publication()

    def recovered_publication(self):
        authors = flows.authors(self.job.continues) | {self.run.id}
        publication = pr_recovery.Publication(self.job.checkouts, self.job.branch, authors,
                                              record=self.record_recovery_effects,
                                              reported_gates=self.data.get("recovered_gate_reports", ()))
        self.run.capture_pr_recovery_discussion([target.refs.url for target in publication.targets.values()])
        return publication

    def begin_publication(self, tree, *, phase=""):
        intent = {"tree": tree}
        if phase:
            intent["phase"] = phase
        if recovery := self.job.recovery_publication:
            intent["recovery_receipt"] = recovery.receipt(self.executing)
        self.data["publication_intent"] = intent
        self.persist()

    def end_publication_intent(self, *, keep_recovery=False):
        if recovery := self.job.recovery_publication:
            recovery.recheck(during="before clearing publication intent")
            if keep_recovery:
                for target in recovery.targets.values():
                    c = target.checkout
                    c.recovery_head, c.recovery_base = recovery.expected_head(c), target.refs.base
            else:
                for checkout in self.job.checkouts:
                    checkout.recovery_head = checkout.recovery_base = ""
            self.job.recovery_publication = None
        self.data.pop("publication_intent", None)

    def keep_failed_publication(self):
        if self.job.recovery_publication:
            self.end_publication_intent(keep_recovery=True)
            self.persist()

    def record_recovery_effects(self, **effects):
        if effects.get("attempt"):
            workspace.note_made(self.job.ws, self.job.checkouts, excluded=self.run.closed_checkout_paths)
        self.data["publication_intent"].update(effects)
        self.persist()

    def settle_recovered_publication(self, prepared, action_id):
        if (self.run.dry_run or self.run.action or self.run.environment_release or self.run.recovery
                or self.run.repairs or self.run.alone or self.run.from_poll):
            raise factory.Refused("use a whole-run resume to settle the interrupted recovered publication")
        intent = self.data["publication_intent"]
        actions = ledger.actions(self.lease)
        prior = next((a for a in actions if a["id"] == action_id), None)
        if (not prior or prior["op"] != "publication.request"
                or prior["result"].get("input_stamp", {}).get("tree") != intent["tree"]
                or prior["state"] not in ("running", "reconciliation-required")):
            raise factory.Refused("the interrupted publication has no matching durable action; inspect its effects manually")
        if any(a["id"] != action_id and (a["state"] == "reconciliation-required"
               or a["state"] == "running" and a["op"] == "publication.request") for a in actions):
            raise factory.Refused("another action still has unconfirmed effects; inspect it before resuming")
        checkouts = [workspace.Checkout.from_row(row) for row in prepared["checkouts"]
                     if row.get("pr") not in prepared.get("closed_prs", ())]
        if any(not (c.wt / ".git").exists() for c in checkouts):
            raise factory.Refused("settling publication requires the original retained worktrees")
        try:
            reports = pr_recovery.settle(checkouts, prepared["branch"], workspace.made_before(self.job.ws),
                                         intent, flows.authors(self.flow.continues))
        except factory.Refused:
            raise
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            raise factory.Refused(f"could not verify the interrupted publication: "
                                  f"{util.first_line(vault.hidden(str(error)), 300)}") from error
        settled = {c.github: c.to_row() for c in checkouts}
        prepared["checkouts"] = [settled.get(row["github"], row) for row in prepared["checkouts"]]
        posted = self.data.setdefault("recovered_gate_reports", [])
        posted.extend(report for report in reports if report not in posted)
        with ledger.tx():
            ledger.transition(self.lease, action_id, "failed", {**prior["result"], "publication_settled": True},
                              expected=(prior["state"],))
            self.invalidate_reconciled_inputs()
            self.data.pop("publication_intent")
            ledger.save(self.lease, self.data)
        self.run.log("Verified the interrupted open PR publication. Fresh gates are required; confirmed gate reports "
                     "will not be reposted for the same head and base.")

    def retry_publication_ci(self, retry, tree, *, measurement_section=""):
        job = self.job
        self.check_pilot_publication()
        flows.require_environment_owner_clear(job)
        recovery = self.job.recovery_publication = (self.recovered_publication()
                                                    if any(c.recovery_head for c in job.checkouts) else None)
        if recovery:
            self.begin_publication(tree, phase="ci-rerun")
            recovery.recheck(during="before retrying publication CI")
        flows.refuse_changed_prs(job)
        checkouts = {c.github: c for c in job.with_prs}
        publication = self.data["publication"]
        heads = publication["heads"]
        retried = self.data.get("ci_retried_heads", {})
        pushed = []
        targets = retry["targets"]
        for repo, target in targets.items():
            checkout = checkouts.get(repo)
            head = target["head"]
            if not checkout or checkout.pr != target["pr"] or checkout.lease != head or heads.get(repo) != head:
                raise ValueError("the failed CI no longer matches this run's published head")
            pushed.append(checkout)
        automatic_requested = self.rerun_requested(pushed)
        if not automatic_requested and (failure := self.second_ci_failure(targets, retried)):
            self.keep_failed_publication()
            return failure
        if retry["caught_up"]:
            checkout, = pushed
            self.take_merge_lease(checkout)
        if not recovery:
            for checkout in pushed:
                row = gh.pr_merge_details(checkout.pr)
                if (row.get("state") != "OPEN" or row.get("headRefOid") != checkout.lease
                        or row.get("baseRefName") != checkout.base):
                    raise ValueError("the PR no longer matches this run's published head and base")
        observed = [] if measurement_section else pipeline.observed_measurements(self.run, job.verified)
        if measurement_section or observed:
            for checkout in pushed:
                try:
                    if recovery:
                        report = publication.get("report_body", "")
                        pipeline.report_recovered_measurement(self.run, recovery, checkout, report,
                                                              measurement_section, observed, job.verified)
                    else:
                        pipeline.publish_unverified_measurement(self.run, job.ticket, checkout.pr, measurement_section,
                                                                observed_requirements=observed)
                except gh.GhError as error:
                    raise ValueError(f"could not update the unverified requirement in {checkout.pr}") from error
        saved = retry.get("reruns")
        if automatic_requested or (saved is None and not retry.get("legacy_rerun")):
            if self.refresh_publication_ci(pushed, retry, automatic_requested):
                return self.finish_ci_retry()
            if saved is None:
                self.keep_failed_publication()
                return self.settle_publication_failure(self.publication_ci_failure())
        launches = {}
        if saved is not None:
            reruns = {repo: [gh.WorkflowRerun(**row) for row in requested] for repo, requested in saved.items()}
        else:
            self.data.setdefault("ci_retried_heads", retried)
            reruns = {c.github: gh.failed_job_reruns(c.github, c.pr, c.lease, targets[c.github]["jobs"])
                      for c in pushed if c.lease not in retried.get(c.github, [])}
            if not any(reruns.values()):
                self.keep_failed_publication()
                raise ValueError("the failed CI has no GitHub Actions jobs to rerun")
            for checkout in pushed:
                if reruns.get(checkout.github):
                    retried.setdefault(checkout.github, []).append(checkout.lease)
            launches = reruns
        if not recovery:
            self.begin_publication(tree, phase="ci-rerun")
        for repo, requested in launches.items():
            for rerun in requested:
                self.request_ci_rerun(repo, rerun.run)
        failure = self.publication_ci(pushed, caught_up=retry["caught_up"], reruns=reruns)
        if failure:
            second_failure = (self.second_ci_failure(self.data["ci_retry"]["targets"], retried)
                              if saved is not None and not automatic_requested else None)
            self.end_publication_intent(keep_recovery=True)
            return second_failure or self.settle_publication_failure(failure)
        return self.finish_ci_retry()

    def finish_ci_retry(self):
        job = self.job
        flows.require_environment_owner_clear(job)
        fresh = [c for c in job.picked(job.opened) if c.github not in job.found]
        options = {"recovery": recovery} if (recovery := job.recovery_publication) else {}
        pipeline.ready_pushed(self.run, fresh, job.picked(job.ci_stopped), job.draft_why,
                              **options, ready_drafts=flows.can_ready_drafts(job))
        return self.finish_publication()

    def refresh_publication_ci(self, pushed, retry, automatic_requested):
        try:
            for checkout in pushed:
                target = retry["targets"][checkout.github]
                actions_failures = target.get("actions_failures")
                if actions_failures is None:
                    actions_failures = [(job, "") for job in target.get("jobs", ())] if automatic_requested else []
                if not gh.publication_ci_passed(checkout.github, checkout.lease, actions_failures=actions_failures):
                    return False
        except pipeline.ci.READ_ERRORS as error:
            self.run.log(f"ci: could not refresh published checks: {util.first_line(str(error), 300)}")
            return False
        self.job.ci_result = "passed on the published head"
        stories = retry.get("round_stories", [])
        if automatic_requested and not stories:
            stories = ["Arete previously requested a CI rerun."]
        ending = "\n\n".join([*stories, "GitHub now reports passing checks on the published head."])
        self.report_publication_ci(pushed, retry["caught_up"], pipeline.ci.section([], 0, ending))
        self.clear_ci_failure()
        return True

    def second_ci_failure(self, targets, retried):
        for repo, target in targets.items():
            head = target["head"]
            if target["red"] and head in retried.get(repo, []):
                reason = f"CI failed twice on the unchanged head {head}"
                self.ending = flows.finish(self.flow, "failed", why=reason, prs=True)
                return {"error": reason}
        return None

    def rerun_requested(self, pushed):
        requested = self.data.get("ci_rerun_requested_heads", {})
        return any(c.lease in requested.get(c.github, []) for c in pushed)

    def remember_ci_rerun(self, repo, head):
        self.data.setdefault("ci_rerun_requested_heads", {}).setdefault(repo, []).append(head)
        self.persist()

    def request_ci_rerun(self, repo, workflow):
        recovery = self.job.recovery_publication
        write = nullcontext()
        if recovery:
            checkout = next(c for c in self.job.with_prs if c.github == repo)
            write = recovery.pr_write(checkout, "CI rerun", "before rerunning CI", "after rerunning CI", workflow=workflow)
        with write:
            gh.rerun_failed_jobs(repo, workflow)
        return True

    def publication_ci_failure(self):
        return {"ci": self.job.ci_result, "failures": CI_FAILURES,
                "error": f"CI needs an explicit worker diagnosis or repair; its failure logs are context {CI_FAILURES}"}

    def clear_ci_failure(self):
        for key in ("ci_retry", "ci_failed", "ci_failures"):
            self.data.pop(key, None)
        self.catalog.context.pop(CI_FAILURES, None)

    def report_publication_ci(self, pushed, caught_up, section):
        self.check_pilot_publication()
        if recovery := self.job.recovery_publication:
            body = pipeline.public_text(self.run, section)
            for checkout in pushed:
                recovery.comment(checkout, body, kind="Recovered CI evidence",
                                 during="before refreshing PR CI evidence")
        elif caught_up:
            for checkout in pushed:
                pipeline.comment_catch_up_ci(self.run, checkout, section, self.job.verified, self.job.review)
        else:
            pipeline.publish_ci(self.run, self.job.ticket, self.job.with_prs, section)

    def publication_ci(self, pushed, *, caught_up=False, reruns=None):
        recovery = self.job.recovery_publication
        tree = self.data["publication_intent"]["tree"] if recovery else self.fingerprint()
        saved = self.data.get("ci_retry", {}) if reruns is not None else {}
        max_rounds = saved.get("max_rounds", 2) if reruns is not None else self.run.settings("ci_repair").tries
        earlier = [pipeline.ci.Round(n, [], story) for n, story in enumerate(saved.get("round_stories", []), 1)]
        requested_heads = {(repo, head) for name in ("ci_rerun_requested_heads", "ci_retried_heads")
                           for repo, heads in self.data.get(name, {}).items() for head in heads}
        automatic = pipeline.AutomaticCIReruns(requested_heads, self.remember_ci_rerun,
                                               self.request_ci_rerun if recovery else None)
        if recovery:
            recovery.recheck(during="before watching publication CI")
        rounds = pipeline.watch_publication_ci(self.job.b, self.job.slot, pushed, max_rounds,
                                               automatic=automatic, reruns=reruns)
        current = rounds[-1]
        if earlier:
            current.n = len(earlier) + 1
            rounds = earlier + rounds
        self.job.ci_result = current.summary()
        self.report_publication_ci(pushed, caught_up, pipeline.ci.section(rounds, max_rounds))
        automatic_reruns = {c.github: attempts for c in pushed if (attempts := automatic.for_checkout(c))}
        if current.passed:
            self.clear_ci_failure()
            return None
        self.data.pop("ci_retry", None)
        self.data["ci_failed"] = True
        targets = {}
        for w in current.seen:
            reds = [r for r in w.reds if reruns is None or w.read or r.check.key in w.rerun_reds or r.check.actions_only]
            actions_failures = []
            for red in reds:
                check = red.check
                run = check.workflow_run_id or check.actions_workflow(w.target.github)
                if check.job or (run and check.source == f"workflow:{run}"):
                    actions_failures.append((check.job, check.name, run))
            targets[w.target.github] = {"head": w.target.sha, "pr": w.target.pr, "red": bool(reds),
                                        "jobs": [r.check.job for r in reds if r.check.job],
                                        "actions_failures": actions_failures}
        requested = automatic_reruns if reruns is None else reruns
        interrupted_watch = bool(requested) and not current.all_read
        if interrupted_watch or any(t["red"] for t in targets.values()):
            self.data["published_tree"] = tree
            retry = self.data["ci_retry"] = {"caught_up": caught_up, "targets": targets}
            if any((c.github, c.lease) in requested_heads for c in pushed):
                retry["round_stories"] = [r.story for r in rounds if r.story]
            elif reruns is None and not self.rerun_requested(pushed) and not any(r.caused for r in current.reds):
                retry["legacy_rerun"] = True
            if interrupted_watch:
                retry["reruns"] = {repo: [asdict(rerun) for rerun in attempts] for repo, attempts in requested.items()}
                if earlier or automatic_reruns:
                    retry.update(max_rounds=max_rounds, round_stories=[r.story for r in rounds[:-1]])
        self.data["ci_failures"] = self.ci_failures(current)
        self.catalog.add_context(CI_FAILURES, self.data["ci_failures"])
        check_head = caught_up or self.data.get("catch_up_hold")
        watched_heads = {w.target.pr: w.target.sha for w in current.seen}
        draft_options = {"recovery": recovery} if recovery else {}
        for checkout in self.job.with_prs:
            self.check_pilot_publication()
            if check_head and (watched_heads.get(checkout.pr) != checkout.lease
                               or pipeline.own_draft_state(self.run, checkout) is None):
                continue
            pipeline.set_draft(self.run, checkout, True, **draft_options)
        return self.publication_ci_failure()

    def take_merge_lease(self, checkout):
        if self.merge_lease is None:
            self.merge_lease = slots.take_merge_lease(self.run, checkout.github, slot=self.job.slot)

    def release_merge_lease(self):
        if self.merge_lease is not None:
            self.merge_lease.close()
            self.merge_lease = None

    def automerge(self, candidates):
        job = self.job
        if not candidates or not pipeline.automerge_allowed(job.b, job.approval, job.draft_why):
            if self.data.get("catch_up_hold"):
                return {"park": True, "error": "the repaired PR is no longer eligible for automerge"}
            return None
        about = pipeline.automerge_about(job.ticket, job.build_summary)
        track_base = not pilot.is_pilot_task(str(self.run.tid))
        recovery = job.recovery_publication
        for checkout in candidates:
            if recovery:
                recovery.recheck(during="before considering automerge")
                hold = (merge_review.MULTIPLE if len(job.checkouts) != 1 else
                        pipeline.unpinned_merge_hold(checkout, recovery.checked_base(checkout)))
                pipeline.report_automerge(self.run, checkout, hold, recovery=recovery)
                continue
            if len(job.checkouts) == 1:
                self.take_merge_lease(checkout)
            options = ({"require_pinned_base": True}
                       if self.data.get("recovered_merge_heads", {}).get(checkout.pr) == checkout.lease else {})
            while True:
                self.publication_scope()
                hold = pipeline.try_automerge(job.b, checkout, job.verified, job.review, about,
                                              track_moved_base=track_base, **options)
                if hold is None:
                    self.clear_catch_up_hold()
                    return None
                if isinstance(hold, gh.MovedBaseHold):
                    hold = self.catch_up(checkout)
                    if isinstance(hold, dict):
                        return hold
                    if not hold:
                        if failure := self.publication_ci([checkout], caught_up=True):
                            return failure
                        self.run.retry_gate_evidence()
                        continue
                elif hold and hold != merge_review.PENDING and self.data.get("catch_up_hold"):
                    return {"park": True, "error": hold}
                pipeline.report_automerge(self.run, checkout, hold)
                self.clear_catch_up_hold()
                break
        return None

    def catch_up(self, checkout):
        job, b = self.job, self.job.b
        base_sha = ""

        def remember_push(head, verified, review):
            self.data["publication_intent"]["catch_up"] = {
                "action": self.executing,
                "pr": checkout.pr, "previous": checkout.lease, "head": head, "base": base_sha,
                "verified": asdict(verified), "review": review.asdict(),
            }
            try:
                self.persist()
            except BaseException:
                self.data["publication_intent"].pop("catch_up", None)
                raise

        try:
            base_sha, hold = pipeline.catch_up_target(b, checkout, job.slot)
            if hold:
                return self.catch_up_failure(hold)
            b.catch_ups += 1
            target_check = {"check_targets": job.check_publication_targets} if job.check_publication_targets else {}
            job.verified, job.review = pipeline.catch_up_base(
                b, checkout, job.branch, base_sha, job.with_left(job.goal), job.with_left(job.plan_text),
                job.review, before_push=remember_push, **target_check)
        except pipeline.CatchUpPushUnconfirmed:
            raise
        except (workspace.RunFailed, RuntimeError, OSError) as e:
            if pipeline._termination is not None:
                raise
            if isinstance(e, pipeline.CatchUpGateHold) and e.candidate_restored:
                return self.hold_catch_up(checkout, base_sha, e)
            reason = e.why if isinstance(e, workspace.RunFailed) else util.first_line(str(e), 300)
            base = f"`{checkout.base}` at `{base_sha}`" if base_sha else f"`{checkout.base}`"
            failure = f"could not catch up to {base}: {pipeline.public_text(self.run, reason)}"
            if isinstance(e, (pipeline.ConflictUnresolved, pipeline.CatchUpPathCollision, pipeline.CatchUpNeedsInput)):
                return failure
            return self.catch_up_failure(failure)
        self.note_caught_up(checkout)
        return ""

    def catch_up_failure(self, reason):
        return {"park": True, "error": reason} if self.data.get("catch_up_hold") else reason

    def hold_catch_up(self, checkout, base_sha, hold):
        reason = pipeline.public_text(
            self.run, f"Catch-up to `{checkout.base}` at `{base_sha}` held on {hold.gate}: {hold.why}")
        attempted = f"{reason}. Checked candidate `{hold.head}` and kept it in the worktree for repair."
        evidence = [attempted]
        if hold.gate == "checks":
            evidence.append(util.tail(hold.report, MAX_CATCH_UP_FEEDBACK_CHARS))
        if hold.verified is not None:
            evidence.append(hold.verified.markdown())
        if hold.review is not None:
            evidence.append(hold.review.markdown())
        feedback = "\n\n".join(evidence)
        self.data["catch_up_hold"] = {"action": self.executing, "gate": hold.gate, "reason": reason,
                                      "feedback": feedback}
        for gate in self.data.get("evidence", {}):
            self.catalog.context.pop(f"evidence:{gate}", None)
        self.data["evidence"] = {}
        self.catalog.add_context(CATCH_UP_FAILURE, feedback)
        self.persist()
        if pipeline.own_draft_state(self.run, checkout) is not None:
            pipeline.publish_gate_evidence(self.run, self.job.ticket, checkout, hold.verified, hold.review, held=attempted)
        return {"failures": CATCH_UP_FAILURE,
                "error": f"{reason}. Delegate a repair using {CATCH_UP_FAILURE}, then rerun the publication gates."}

    def clear_catch_up_hold(self):
        self.data.pop("catch_up_hold", None)
        self.catalog.context.pop(CATCH_UP_FAILURE, None)

    def park_catch_up(self, reason, *, on_ticket=False, close_failed=False):
        held = self.data["catch_up_hold"]["reason"]
        recovering = any(c.recovery_head for c in self.job.checkouts)
        if not recovering:
            for checkout in self.job.with_prs:
                if pipeline.own_draft_state(self.run, checkout) is False:
                    pipeline.set_draft(self.run, checkout, True, "automerge")
        self.run.say(f"{held}. I parked the run because {reason}.", on_ticket=on_ticket or bool(recovering))
        reason = f"{held}. {reason}"
        if close_failed:
            return pipeline.record_park(self.run, reason, {}, comment=False)
        return pipeline.park_run(self.run, reason, comment=False)

    def settle_publication_failure(self, failure):
        if self.data.get("catch_up_hold"):
            if failure.get("park"):
                self.ending = self.park_catch_up(failure["error"])
            elif self.run.budget and (limit := self.run.budget.repair_limit_reason()):
                reason = f"Publication CI held: {failure['ci']}. {limit}" if failure.get("ci") else limit
                self.ending = self.park_catch_up(reason)
        if self.ending:
            self.release_merge_lease()
        return failure

    def reconcile_catch_up(self, pending):
        job = self.job
        self.executing = pending["action"]
        actions = ledger.actions(self.lease)
        action = next((a for a in actions if a["id"] == self.executing), None)
        if (not action or action["op"] != "publication.request"
                or action["state"] not in ("running", "reconciliation-required")):
            raise factory.Refused("the catch-up push has no interrupted publication action")
        if len(job.checkouts) != 1:
            raise factory.Refused("the catch-up push does not match the retained checkout")
        checkout = job.checkouts[0]
        head = pending["head"]
        if (checkout.pr != pending["pr"] or checkout.lease != pending["previous"] or checkout.merge != head
                or checkout.base_commit != pending["base"]
                or self.data["publication"]["heads"].get(checkout.github) != checkout.lease
                or util.git(["rev-parse", "HEAD"], checkout.wt).strip() != head or workspace.uncommitted(checkout.wt)):
            raise factory.Refused("the catch-up push does not match the retained checkout")
        verified, review = Verified.stored(pending["verified"]), pipeline.Review.stored(pending["review"])
        if hold := pipeline.merge_evidence_hold(job.b, checkout, verified, review, head):
            raise factory.Refused(f"cannot reconcile the catch-up push: {hold}")
        self.check_pilot_publication()
        self.take_merge_lease(checkout)
        pipeline.note_prs(self.run, job.branch, job.with_prs)
        try:
            pipeline.push_caught_up_head(job.b, checkout, job.branch, head, check_targets=self.check_push_targets)
        except RetargetedPR as error:
            self.ending = self.hold_revision(error.pr, str(error))
            self.data.pop("publication_intent", None)
            ledger.transition(self.lease, self.executing, "failed", {"error": str(error)}, expected=(action["state"],))
            self.persist()
            return {"error": str(error)}
        job.verified, job.review = verified, review
        self.note_caught_up(checkout)
        if failure := self.publication_ci([checkout], caught_up=True):
            for prior in actions:
                if prior["state"] == "accepted" and prior["op"] == "owner.ask":
                    ledger.transition(self.lease, prior["id"], "cancelled",
                                      {"reason": "the run resumed an interrupted catch-up push"}, expected=("accepted",))
            self.data.pop("publication_intent", None)
            self.persist()
            result = self.settle_publication_failure(failure)
        else:
            result = self.finish_publication()
        ledger.transition(self.lease, self.executing, "failed" if "error" in result else "succeeded", result,
                          expected=(action["state"],))
        return result

    def note_caught_up(self, checkout):
        job, b = self.job, self.job.b
        self.data["publication"]["heads"][checkout.github] = checkout.lease
        pipeline.note_review(self.run, job.review, job.verified)
        self.run.log(f"automerge: pushed catch-up {b.catch_ups} to {checkout.pr} at {checkout.lease}")
        self.stamp("checks", {"full": True, "checked": b.checked, "report": b.report})
        self.stamp("verify", asdict(job.verified))
        self.stamp("review", job.review.asdict())
        pipeline.publish_gate_evidence(self.run, job.ticket, checkout, job.verified, job.review)

        self.data["publication_intent"].pop("catch_up", None)
        self.persist()

    def finish_publication(self):
        self.check_pilot_publication()
        recovery = self.job.recovery_publication
        if recovery:
            self.data["publication_intent"]["phase"] = "publish"
            self.persist()
        else:
            self.begin_publication(self.fingerprint(), phase="publish")
        outcome = self.atomic("publish", merge=self.automerge)
        tree = self.fingerprint() if outcome.passed else ""
        self.end_publication_intent()
        if outcome.passed:
            if recovery:
                self.data.setdefault("recovered_merge_heads", {}).update(
                    (c.pr, c.lease) for c in self.job.picked(self.job.changed) if c.pr)
            self.data["published"], self.data["published_tree"] = outcome.data, tree
        else:
            self.data.pop("published", None)
            if not self.data.get("ci_retry"):
                self.data.pop("published_tree", None)
        self.persist()
        if self.data.get("catch_up_hold") or self.data.get("ci_failed"):
            return self.settle_publication_failure(outcome.data)
        if self.is_spec and outcome.passed:
            if not all(self.current(gate) for gate in self.required_gates()):
                raise factory.Refused("the published tree no longer has current spec gates")
            receipt = spec_delivery.candidate(self.job, self.data["definition"], self.policy_sha(), self.data["acceptance"],
                                              code_review.candidate_trees(self.job.checkouts))
            if not spec_store.advance(self.run.spec_identity, self.run.id, "building", "published", receipt):
                raise factory.Refused("the spec delivery claim changed during publication")
        self.ending = flows.ended(self.flow, outcome)
        self.release_merge_lease()
        return outcome.data

    def check_pilot_publication(self):
        if pilot.is_pilot_task(str(self.run.tid)):
            try:
                repo, tid = self.run.repo, str(self.run.tid)
                receipt = pilot.live_receipt(tid, repo.name)
                layer = pipeline.config.load_layer(repo.layer_dir)
                run = pilot.scheduled_run(repo.name, tid)
                pilot.require(run and run["id"] == self.run.id, "the approved one-shot run changed")
                pilot.require(layer.factory_path == "orchestrated"
                              and self.policy_sha() == receipt["policy"] == policy_fingerprint(
                                  pipeline.config.load(), layer, ()), "the one-shot run changed its admitted policy")
            except pilot.Held as error:
                raise runlimits.Parked(str(error)) from error

    def failure_recovery(self):
        actions = (action for action in ledger.actions(self.lease) if action["run"] == self.run.id)
        for action in sorted(actions, key=lambda action: action["result"]["revision"], reverse=True):
            op, result = action["op"], action["result"]
            if op in ("worker.start", "worker.continue") and result.get("writing") is True:
                return result.get("recovery") if action["state"] == "failed" and result.get("event") == "result" else None
            if (op == "checks.run" and result.get("passed") is False
                    or op == "verification.run" and result.get("verdict") == "fail"
                    or op == "review.run" and result.get("verdict") == "request_changes"):
                return None
        return None

    @contextmanager
    def report_failed_finish(self, turn: str | None):
        if not turn or self.flow.only or self.run.action_only or self.run.dry_run:
            yield
            return
        with ledger.reader() as connection:
            ledger.fenced(connection, self.lease)
            row = connection.execute(
                "SELECT response FROM orchestration_turns WHERE root = ? AND id = ? AND state = 'accepted'",
                (self.lease.root, turn)).fetchone()
        rationale = ledger.decoded(row).get("response", {}).get("rationale")
        recovering = any(c.recovery_head for c in self.job.checkouts)
        if recovering:
            self.recovered_publication().recheck(during="before reporting the failure")
        else:
            for checkout in self.job.with_prs:
                try:
                    pr = gh.pr_merge_details(checkout.pr)
                    if not isinstance(pr, dict):
                        raise ValueError("PR lookup did not return a JSON object")
                except (gh.GhError, ValueError) as error:
                    self.run.log(f"pr: could not inspect {checkout.pr}: {util.first_line(str(error), 300)}")
                    continue
                if pr.get("state") == "OPEN" and pr.get("isDraft") is False:
                    pipeline.set_draft(self.run, checkout, True, "failed")
        if rationale is None and not recovering:
            yield
            return
        say = self.run.say
        if rationale is not None:
            comment_rationale = outbound.screen(rationale.strip()).encode("utf-8", "backslashreplace").decode("utf-8")
            with principals.acting(f"run:{self.run.id}/respond"):
                options = {"on_ticket": True} if recovering else {}
                say(f"Run `{self.run.id}` failed without delivery.\n\n{comment_rationale}\n\n{self.run.retry_hint}.", **options)
        self.run.say = (lambda _body: "") if rationale is not None else (lambda body: say(body, on_ticket=True))
        try:
            yield
        finally:
            self.run.say = say

    def perform(self, action):
        args, op = action["request"], action["op"]
        stale = self.data.get("superseded_actions", ())
        if (args.get("worker_id") in stale or any(dep in stale for dep in args.get("depends_on", []))):
            raise ValueError("the worker or dependency belongs to superseded ticket requirements")
        if op == "worker.start":
            return self.worker_start(action, args)
        if self.exchanged and op == "worker.messages":
            page = self.worker_observation(**args)
            return {k: page[k] for k in ("messages", "message_cursor", "more_messages")}
        if self.exchanged and op in ("worker.reply", "worker.steer"):
            return exchange.queue(self.lease, action, self.validate_scope)
        if op == "worker.continue" and args["worker_id"] in self.data.get("pr_recovery_stale_actions", ()):
            raise ValueError("the worker predates PR reconciliation; start a new worker against the reconciled tree")
        if op == "worker.continue" and self.exchanged:
            w = exchange.workers(self.lease)[args["worker_id"]]
            prior = next(a for a in ledger.actions(self.lease) if a["id"] == w["id"])
            effective = {**w["data"]["effective"], **args["settings"], "goal": args["message"]}
            prior = {**prior, "result": {**prior["result"], "text": w["data"].get("summary", "") +
                                         "\n" + w["data"].get("reason", "")}}
            return self.worker_start(action, effective, prior)
        if op == "worker.continue":
            prior = next(a for a in ledger.actions(self.lease) if a["id"] == args["worker_id"])
            if prior["state"] not in ledger.TERMINAL or "effective" not in prior["result"]:
                raise ValueError("continue requires a completed worker")
            settings = args["settings"]
            if set(settings) - {"route_id", "effort"}:
                raise ValueError("continuation settings may only select an approved route and effort")
            effective = {**prior["result"]["effective"], **settings, "goal": args["message"]}
            return self.worker_start(action, effective, prior)
        if op == "worker.steer":
            self.data.setdefault("steering", {}).setdefault(args["worker_id"], []).append(args["message"])
            return {"queued": True, "applies": "next worker turn", "worker_id": args["worker_id"]}
        if op == "worker.cancel":
            attempt = args["worker_id"]
            if self.exchanged:
                attempt = exchange.workers(self.lease)[args["worker_id"]]["attempt"]
            if worker := self.workers.get(attempt):
                worker.close()
                del self.workers[attempt]
                if self.run.launch_done:
                    self.run.launch_done(worker.step, False)
                if not self.exchanged:
                    ledger.transition(self.lease, attempt, "cancelled", {"reason": args["reason"],
                                      "effective": worker.effective, "usage_unknown": True})
                    return {"cancelled": args["worker_id"], "quiescent": True}
            if self.exchanged:
                return exchange.cancel(self.lease, args["worker_id"], action["id"], args["reason"], receipt=True)
            return {"cancelled": False, "worker_id": args["worker_id"], "quiescent": True}
        if op == "catalog.list":
            return self.catalog.list(**args)
        if op == "catalog.describe":
            return {"entries": self.catalog.describe(**args)}
        if op == "context.read":
            return self.catalog.read(**args)
        if op == "run.observe":
            if args["after"] < 0 or not 1 <= args["limit"] <= 20:
                raise ValueError("invalid observation bounds")
            return self.observe(args["limit"], args["after"])
        if op == "run.wait":
            return self.wait_workers(args["timeout_seconds"])
        self.barrier()
        if self.exchanged and op != "owner.ask" and not (op == "run.finish" and args["reason"] == "failed"):
            self.worker_barrier()
        if op == "spec.define" and self.is_spec:
            return self.define_spec(args)
        if op == "checks.run":
            remaining = self.data.get("remaining_checks", [])
            focused = check_repair.focus(self.job.layer, remaining) if remaining and self.uses_code_review else {}
            quick = args["scope"] == "quick" and not (remaining and self.uses_code_review)
            evidence = self.data.get("evidence", {}).get("checks", {})
            if reused := self.reuse_checks(evidence, focused):
                return reused
            if focused and evidence.get("result", {}).get("scope") == "failed" and self.current("checks"):
                focused = {}
            self.data.setdefault("evidence", {}).pop("checks", None)
            self.persist()
            b = self.job.b
            b.denied_feedback = self.data.get("denied_feedback", "")
            b.on_denied_edit = self.hold_for_denied_edit
            try:
                outcome = self.atomic("checks", tree=str(self.job.ws), quick=quick,
                                      **({"focused": focused} if focused else {}))
            finally:
                b.on_denied_edit = None
            if self.data.get("denied_feedback"):
                self.data["denied_feedback"] = b.report
            if outcome.passed:
                self.stamp("checks", outcome.data)
            if self.uses_code_review:
                if outcome.data.get("full"):
                    remaining = []
                elif failures := outcome.data.get("failed_checks"):
                    skipped = [item for item in remaining if item["name"] not in outcome.data["checks_executed"]]
                    remaining = [*failures, *skipped]
                    if skipped:
                        outcome.data["runner_blocked"] = False
                self.data["remaining_checks"] = remaining
            return {"passed": outcome.passed, **outcome.data, "remaining_checks": remaining}
        if op == "verification.decide":
            return self.decide_verification(args)
        if op == "verification.run" and self.uses_code_review:
            return self.verification_attempt(args)
        if op == "review.decide":
            return self.decide_findings(args)
        if op == "review.run" and self.uses_code_review:
            source = self.latest_code_review()
            report = self.applicable_report(source)
            if report and (self.current("review") or
                           (report["verdict"] == "pending" and self.job.review.verdict == "pending")):
                return {"reused_review": source["id"], "findings": report["findings"],
                        "next": "Use the source review ID to settle pending findings, or publish a settled review."}
            if not self.full_checks_passed():
                return {"review_deferred": True,
                        "error": "Standards and Spec review requires passing full checks on the current candidate. "
                                 "Resolve the failing checks or runner issue. After a focused pass, request full checks."}
        if op in ("verification.run", "review.run"):
            role = "verify" if op == "verification.run" else "review"
            self.data.setdefault("evidence", {}).pop(role, None)
            self.persist()
            setting = self.catalog.route(args["route_id"], args["effort"], (role,))
            refs = args["goal_refs"] if role == "verify" else args["context_refs"]
            references = "\n\nReference material, not owner authority:\n" + "\n".join(
                self.catalog.read(ref, 0, 8000)["text"] for ref in refs) if refs else ""
            goal, reply = self.job.goal, self.job.fix_reply
            self.job.goal, self.job.fix_reply = goal + references, reply + references
            try:
                with self.selected(setting):
                    outcome = self.atomic(role, **({"tree": str(self.job.ws), "single": True} if role == "verify"
                                                   else {"attempt": self.job.reviews + 1, **({"code_review": True} if self.uses_code_review else {})}))
            finally:
                self.job.goal, self.job.fix_reply = goal, reply
            result = outcome.data.get("verified" if role == "verify" else "review", {})
            self.stamp(role, result)
            if outcome.data.get("ending") == "environment-held":
                self.ending = flows.ended(self.flow, outcome)
                return {"operation_passed": False, **result}
            return {"operation_passed": outcome.passed, **result}
        if op == "owner.ask":
            if zte.reserved(str(self.run.tid)):
                raise ValueError("the standing README lane cannot request owner intervention; stop without delivery")
            ask, errors = owner_asks.validate(args["request"])
            if errors:
                raise ValueError("; ".join(errors))
            return self.atomic("orchestrate_ask", request=ask).data
        if op == "publication.request":
            return self.publish(args.get("unverified_measurement"))
        if op == "run.finish":
            if args["reason"] == "noop" and self.data.get("ci_failed"):
                raise ValueError("CI is still failed; retry CI through publication.request, repair, or finish failed")
            if args["reason"] == "delivered":
                published = self.data.get("published", {})
                if (published.get("ending") not in ("done", "pr") or self.data.get("ci_failed")
                        or self.data.get("published_tree") != self.fingerprint()
                        or not all(self.current(gate) for gate in self.required_gates())):
                    raise ValueError("delivery has no successful current engine publication")
                self.ending = (published["ending"], published.get("summary", ""),
                               " ".join(c.pr for c in self.job.with_prs))
            elif self.data.get("catch_up_hold"):
                self.ending = self.park_catch_up("the orchestrator stopped without delivery")
            elif args["reason"] == "noop":
                if self.is_spec:
                    raise ValueError("a spec cannot finish without delivery-point evidence")
                if feedback := self.data.get("denied_feedback"):
                    raise ValueError(feedback)
                if not self.job.b.restage().empty:
                    raise ValueError("noop cannot discard an implementation diff")
                self.ending = flows.finish(self.flow, "nothing", summary=self.job.build_summary or "No change needed.")
            elif (self.run.budget and (reason := self.run.budget.repair_limit_reason())
                  and not zte.reserved(str(self.run.tid))):
                if feedback := self.data.get("denied_feedback"):
                    reason = f"{reason}. {feedback}"
                self.ending = pipeline.park_run(self.run, reason)
            else:
                with self.report_failed_finish(action.get("turn")):
                    self.ending = flows.finish(self.flow, "failed", why="the orchestrator stopped without delivery",
                                               recovery=self.failure_recovery())
            return {"reason": args["reason"]}
        raise ValueError("unknown orchestration operation")

    def hold_for_denied_edit(self, feedback):
        self.data["denied_feedback"] = feedback
        self.persist()

    def dispatch_publication(self, action):
        publication = getattr(getattr(self, "job", None), "recovery_publication", None)
        return publication, action["op"] == "publication.request" and bool(getattr(self, "data", {}).get("publication_intent"))

    def dispatch(self, action) -> ValueError | None:
        if action["state"] != "accepted":
            return
        if action["op"] not in ("worker.start", "worker.continue") and not (
                self.exchanged and action["op"] in ("worker.steer", "worker.reply", "worker.cancel")):
            ledger.transition(self.lease, action["id"], "running", {"input_stamp": {
                "tree": self.fingerprint(), "policy": self.policy_sha(), "acceptance": self.data["acceptance"]}})
        refusal, result = None, None
        try:
            self.executing = action["id"]
            result = self.perform(action)
            if (action["op"] == "checks.run" and result and result.get("runner_blocked") and self.uses_code_review
                    and not self.run.dry_run and not self.run.alone and not self.run.action_only
                    and not self.run.owner_questions_blocked and (request := owner_asks.check_runner_request(result))):
                self.atomic("orchestrate_ask", request=request, call=owner_asks.CHECK_RUNNER_CALL)
        except owner_asks.Asked as asked:
            ledger.transition(self.lease, action["id"], "succeeded",
                              {**(result or {}), "ask_ids": asked.ask_ids, "awaiting_owner": True})
            self.persist()
            run_lines.write(self.run.id, "action", action=action["id"], op=action["op"], state="succeeded")
            raise
        except pr_recovery.Hold as error:
            publication, pending = self.dispatch_publication(action)
            reconcile = pending and (publication is None or publication.effected or error.recovery.get("publication_unconfirmed"))
            state = "reconciliation-required" if reconcile else "failed"
            if pending and not reconcile:
                self.data.pop("publication_intent", None)
            ledger.transition(self.lease, action["id"], state, {"error": error.why, **error.recovery})
            self.persist()
            run_lines.write(self.run.id, "action", action=action["id"], op=action["op"], state=state)
            if reconcile and publication is None:
                raise workspace.RunFailed(f"Unconfirmed publication must be reconciled. {error.why}") from error
            raise
        except ValueError as error:
            publication, reconcile = self.dispatch_publication(action)
            if publication and (reconcile or publication.effected):
                why = f"{error.pr}: {error}" if isinstance(error, RetargetedPR) else str(error)
                failure = publication.failure(util.first_line(vault.hidden(why), 300))
                ledger.transition(self.lease, action["id"], "reconciliation-required",
                                  {"error": failure.why, "pushed": dict(publication.pushed)})
                self.persist()
                raise failure from error
            refusal = error
            result = {"error": str(error)[:2000]}
            if isinstance(error, RetargetedPR):
                self.ending = self.hold_revision(error.pr, str(error))
                self.data.pop("publication_intent", None)
            if isinstance(error, verification_attempts.Refused):
                result["preserved_verification"] = True
            if self.exchanged and action["op"] in ("worker.start", "worker.continue"):
                wid = action["request"].get("worker_id", action["id"])
                found = exchange.workers(self.lease).get(wid)
                if found and found["phase"] == "assigned" and found["attempt"] == action["id"]:
                    exchange.change(self.lease, wid, "parked", reason=result["error"])
                    result.update(worker_id=wid, effective=found["data"]["effective"])
            ledger.transition(self.lease, action["id"], "failed", result)
        except BaseException as error:
            if action["id"] in getattr(self, "workers", {}):
                raise
            publication, reconcile = self.dispatch_publication(action)
            state = "reconciliation-required" if reconcile else "interrupted"
            result = {"error": f"stopped by {type(error).__name__}",
                      "usage_unknown": action["op"] in ("worker.start", "worker.continue", "verification.run", "review.run")}
            if reconcile and publication:
                result["pushed"] = dict(publication.pushed)
                if isinstance(error, workspace.RunFailed):
                    result["error"] = error.why
            ledger.transition(self.lease, action["id"], state, result)
            self.persist()
            run_lines.write(self.run.id, "action", action=action["id"], op=action["op"], state=state)
            raise
        else:
            if result is not None and not (self.exchanged and action["op"] in ("worker.steer", "worker.reply", "worker.cancel")):
                gate = ACTION_GATES.get(action["op"])
                if gate and self.data.get("evidence", {}).get(gate, {}).get("producer") == action["id"]:
                    result["evidence"] = self.data["evidence"][gate]
                state = "failed" if action["op"] != "run.observe" and "error" in result else "succeeded"
                ledger.transition(self.lease, action["id"], state, result)
        self.persist()
        run_lines.write(self.run.id, "action", action=action["id"], op=action["op"],
                        state="running" if result is None else "failed" if "error" in result else "succeeded")
        if isinstance(refusal, prompt_loader.MissingInputs):
            raise refusal
        return refusal

    def spec_intake(self):
        if not self.is_spec:
            return None
        try:
            found = spec_graph.intake(self.job, self.data.get("spec_direct"))
        except spec_graph.Pending as error:
            summary = f"Spec intake awaiting tracker reconciliation: {error}"
            self.run.log(summary)
            return "interrupted", summary, ""
        except (db.IncompatibleSchema, *spec_graph.READ_ERRORS) as error:
            raise workspace.RunFailed(f"Spec intake held: {error}") from error
        if not found:
            return None
        if found["mode"] == "direct":
            self.data["spec_direct"] = found["input"]
            self.persist()
            return None
        if found["preview"]:
            return "dry-run", "Reviewed the spec breakdown without publishing it", ""
        refs = ", ".join(tid if "#" in tid else self.run.tracker.ref(tid) for tid in found["children"])
        summary = f"Published or adopted the native spec graph: {refs}"
        self.run.leave("running")
        return "done", summary, ""

    def drive(self):
        verification_passed = False
        park_reason = None
        try:
            if ended := self.prepare():
                return ended
            if self.run.action:
                known = {action["id"]: action for action in ledger.actions(self.lease)}
                prior = known.get(self.run.action)
                allowed = (("verification.run",) if self.run.environment_release else
                           ("worker.start", "worker.continue", "checks.run", "verification.run", "review.run"))
                if prior and prior["id"] in self.data.get("superseded_actions", ()):
                    raise factory.Refused("the requested action belongs to superseded requirements")
                if prior is None or prior["op"] not in allowed or prior["state"] not in ledger.TERMINAL:
                    raise factory.Refused("rerun requires a finished worker/check/review/verification action ID")
                if prior["op"].startswith("worker.") and prior["id"] in self.data.get("pr_recovery_stale_actions", ()):
                    raise factory.Refused("the worker predates PR reconciliation; resume and start a fresh worker")
                snapshot = ledger.snapshot(self.lease)
                turn = ledger.begin(self.lease, {"rerun": prior["id"]}, self.decision_limit)
                requester = "Live release retry" if self.run.environment_release else "Owner requested rerun"
                response = {"version": self.protocol, "decision_id": turn, "state_version": snapshot["version"],
                            "rationale": f"{requester} of {prior['id']}",
                            "calls": [{"op": prior["op"], "args": prior["request"]}]}
                if self.exchanged and prior["op"] in ("worker.start", "worker.continue"):
                    self.worker_barrier()
                    response["calls"] = [{"op": "worker.start", "args": prior["result"]["effective"]}]
                try:
                    response = self.catalog.validate(response, turn, snapshot["version"], known,
                                                     enforce_size_limit=False)
                except (ValueError, TypeError) as error:
                    ledger.reject(self.lease, turn, str(error)[:2000])
                    raise factory.Refused(f"cannot rerun {prior['id']} under the current catalog: {error}") from error
                rerun = ledger.accept(self.lease, turn, snapshot["version"], response, self.action_limit)[0]
                self.dispatch(rerun)
                if self.run.environment_release:
                    if self.ending:
                        return self.ending
                    if (not flows.retry_passed(self.job.verified)
                            or self.data.get("evidence", {}).get("verify", {}).get("producer") != rerun["id"]):
                        raise factory.Refused("the saved verification action did not complete successfully")
                    verification_passed = True
                if self.run.action_only:
                    while self.workers:
                        self.collect()
                        time.sleep(0.05)
                    result = next(a for a in ledger.actions(self.lease) if a["id"] == rerun["id"])
                    if self.exchanged and (wid := result["result"].get("worker_id")):
                        phase = exchange.workers(self.lease)[wid]["phase"]
                        if phase not in ("integrated", "completed"):
                            return "failed", f"{wid} ended its standalone attempt {phase}; resume the controller", ""
                    return ("step" if result["state"] == "succeeded" else "failed",
                            f"{rerun['id']} {result['state']}; dependent evidence must be refreshed", "")
            if not (self.run.alone or self.run.action or self.run.repairs or self.run.environment_release
                    or self.run.feedback or self.run.recovery or ledger.actions(self.lease)):
                if ended := self.spec_intake():
                    return ended
            while not self.ending:
                self.collect()
                if reason := self.data.get("scope_needs_refresh"):
                    if self.workers:
                        time.sleep(0.05)
                        continue
                    return "failed", f"{reason}; resume to verify the current ticket", ""
                pending = [a for a in ledger.actions(self.lease) if a["state"] == "accepted"]
                if self.exchanged and not pending:
                    self.resume_ready()
                for action in pending or self.decide():
                    self.dispatch(action)
                    if self.ending:
                        break
            return self.ending
        except runlimits.Parked as error:
            if pipeline._termination is None and self.data.get("catch_up_hold"):
                return self.park_catch_up(str(error))
            raise
        except ledger.Conflict as error:
            if not (isinstance(error, ledger.Limit) and self.data.get("catch_up_hold")):
                raise runlimits.Parked(str(error)) from error
            park_reason = str(error)
        finally:
            try:
                self.close()
            except BaseException as cleanup_error:
                if park_reason is not None and pipeline._termination is None:
                    try:
                        self.run.catch_up_cleanup_park = self.park_catch_up(
                            park_reason, on_ticket=True, close_failed=True)
                    except BaseException as park_error:
                        cleanup_error.add_note(f"Could not park the catch-up: {park_error}")
                raise
            if self.run.environment_release and not verification_passed and flows.reset_environment_base(self.job.checkouts):
                self.persist()
        return self.park_catch_up(park_reason, on_ticket=True)

    def close(self):
        errors = []
        for key, worker in list(self.workers.items()):
            try:
                worker.close()
                if self.run.launch_done:
                    self.run.launch_done(worker.step, False)
                ledger.transition(self.lease, key, "interrupted", {"effective": worker.effective,
                                  "error": "controller stopped", "usage_unknown": True})
                if self.exchanged:
                    exchange.change(self.lease, worker.logical_id, "parked", resume_interrupted=True,
                                    reason="controller stopped; candidate retained")
                del self.workers[key]
            except BaseException as error:
                errors.append(error)
        if self.exchanged:
            for w in exchange.workers(self.lease).values():
                if w["phase"] not in exchange.CLOSED and not w["data"].get("reason"):
                    exchange.change(self.lease, w["id"], w["phase"], reason="controller ended; retained for continuation")
        self.release_merge_lease()
        if errors:
            raise errors[0]


@factory.step("orchestrate_ask")
def orchestrate_ask(flow, request, call="orchestrate"):
    flow.ctx.run.owner_ask(call, [request], NO_SESSION)
    return factory.Outcome("pass")


def orchestrate(flow, *, max_builders=None):
    return Controller(flow, max_builders=max_builders).drive()


def single_worker(flow):
    controller = Controller(flow)
    try:
        if ended := controller.prepare():
            return ended
        routes = controller.catalog.settings
        calls = [[{"op": "worker.start", "args": {"goal": controller.catalog.context["task"],
                  "route_id": "model:build:0", "effort": routes["model:build:0"].effort or "low"}}],
                 [{"op": "checks.run", "args": {}},
                  {"op": "verification.run", "args": {"route_id": "model:verify:0",
                     "effort": routes["model:verify:0"].effort or "low"}},
                  {"op": "review.run", "args": {"route_id": "model:review:0",
                     "effort": routes["model:review:0"].effort or "low"}}],
                 [{"op": "publication.request", "args": {}}]]
        for batch in calls:
            snapshot = ledger.snapshot(controller.lease)
            turn = ledger.begin(controller.lease, {"executor": "single-worker"}, controller.policy.max_decisions)
            response = controller.catalog.validate({"version": PROTOCOL, "decision_id": turn,
                "state_version": snapshot["version"], "rationale": "Single-worker executor without controller inference.",
                "calls": batch}, turn, snapshot["version"], {a["id"]: a for a in ledger.actions(controller.lease)})
            actions = ledger.accept(controller.lease, turn, snapshot["version"], response, controller.policy.max_actions)
            for action in actions:
                if isinstance(controller.dispatch(action), RetargetedPR):
                    return controller.ending
                while controller.workers:
                    controller.collect()
                    time.sleep(0.05)
                failed = [a["id"] for a in ledger.actions(controller.lease) if a["state"] in ("failed", "interrupted")]
                if failed:
                    return flows.finish(flow, "failed", why="single-worker execution did not pass its required gates",
                                        recovery=controller.failure_recovery())
                if controller.ending:
                    return controller.ending
        return flows.finish(flow, "failed", why="single-worker execution did not deliver")
    finally:
        controller.close()
