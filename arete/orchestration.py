from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from math import inf
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from . import (
    codebase,
    config,
    delegation,
    diffguard,
    factory,
    flows,
    gh,
    journeys,
    outbound,
    owner_asks,
    pilot,
    pipeline,
    principals,
    prompt_loader,
    repairs,
    run_lines,
    runlimits,
    slots,
    util,
    workspace,
    zte,
)
from .agent import NO_SESSION, Session, json_from_text
from .orchestration_api import BUNDLES, PROTOCOL, Catalog, describe_operations, help_text
from .store import asks as ask_store
from .store import insights as insights_store
from .store import orchestration as ledger
from .verify import Verified

CI_FAILURES = "ci:failures"
MAX_CI_FAILURE_CHARS = 24000
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
    effective = asdict(layer)
    effective.pop("factory_path")
    if layer.required_check is None:
        effective.pop("required_check")
    if layer.verify:
        effective["verify"] = layer.verify.policy()
    policy_config = asdict(cfg)
    policy_config.pop("server_url")
    policy_config["max_parallel_checks"] = config.Config.max_parallel_checks
    if resume:
        config.remove_controller_only_settings(policy_config)
    return ledger.digest(json.loads(json.dumps({"layer": effective, "config": policy_config,
                                                "closed_context": closed_context}, default=str)))


class Controller:
    merge_lease: TextIO | None = None
    starts_new_lineage = False

    def __init__(self, flow):
        self.flow, self.job = flow, flow.ctx
        self.run = self.job.run
        self.workers, self.progress, self.reported = {}, {}, {}
        self.lease = ledger.acquire(self.run.id, flow.continues,
                                    {"protocol": PROTOCOL, "mode": self.run.path, "evidence": {}})
        self.data = ledger.snapshot(self.lease)["data"]
        if self.data.get("protocol") != PROTOCOL or self.data.get("mode") != self.run.path:
            raise factory.Refused("this controller protocol needs compatible code before it can resume")
        self.policy = pipeline.config.Orchestration(**self.data.get("limits", asdict(
            pipeline.config.load_layer(self.run.repo.layer_dir).orchestration)))
        self.run.overrides = {**self.data.get("overrides", {}), **self.run.overrides}
        inherited = self.data.get("step_overrides", {})
        self.run.step_overrides = {role: {**inherited.get(role, {}), **self.run.step_overrides.get(role, {})}
                                  for role in inherited.keys() | self.run.step_overrides.keys()}
        self.catalog = None
        self.last_progress, self.ending = -inf, None

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

    def continuation_blocker(self, *, decision_boundary=False):
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
                if action["state"] not in ledger.TERMINAL:
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
        prepared = self.data.get("prepared")
        restore_prs = bool(prepared) and not (pending_push or self.run.environment_release or self.run.action_only)
        if not restore_prs and self.run.feedback and (closed := self.run.feedback_closure()):
            self.quiescent()
            return "noop", closed, ""
        self.flow.matching = False
        run_lines.write(self.run.id, "route", continues=self.flow.continues, mode="orchestrated", protocol=PROTOCOL)
        if prepared:
            if not {"run_settings", "overrides", "step_overrides"} <= self.data.keys():
                raise factory.Refused("the saved controller lacks accepted invocation settings; reconcile before resuming")
            layer_dir, source_file = Path(prepared["layer"]), None
            if not layer_dir.exists():
                for checkout in map(workspace.Checkout.from_row, prepared["checkouts"]):
                    if layer_dir.is_relative_to(checkout.wt):
                        source_file = checkout.src / layer_dir.relative_to(checkout.wt) / "config.toml"
                        break
            self.job.layer = pipeline.checked_layer(self.job.cfg, layer_dir, source_file=source_file)
            self.run.cfg, self.run.layer, self.run.kind = self.job.cfg, self.job.layer, prepared["ticket"]["kind"]
            self.lineage()
            if self.starts_new_lineage:
                self.discard_gate_state()
            if restore_prs:
                if ending := self.restore_pr_targets(prepared):
                    return ending
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
            outcome = self.atomic("prepare", tree=str(self.job.ws))
            if not outcome.passed:
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
        if self.run.answers_due:
            answer = owner_asks.fresh_note(self.run.answers_due)
            self.run.story.append(answer)
            self.data.setdefault("owner_answers", []).append(answer)
        goal = self.data["goal_base"] + "\n" + "\n".join(self.data.get("owner_answers", []))
        self.job.goal = pipeline.triage_goal(goal, self.job.approval)
        self.job.triage = pipeline.triage_notes(self.job.approval)
        self.job.b.plan_text, self.job.b.left_for_a_person = self.job.plan_text, self.job.left_for_a_person
        lineage, old_lineage = self.lineage()
        self.data.update(lineage)
        self.data["overrides"], self.data["step_overrides"] = self.run.overrides, self.run.step_overrides
        self.data.setdefault("code", codebase.STAMP)
        self.data["recovery_code"] = codebase.STAMP
        self.data["limits"] = asdict(self.policy)
        self.data["acceptance"] = ledger.digest([self.catalog.context["task"], self.job.goal,
                                                 self.job.left_for_a_person])
        self.recover()
        if self.run.environment_release:
            self.data.pop("published", None)
            self.data.pop("published_tree", None)
        elif restore_prs:
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
        return None

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

    def check_push_targets(self):
        for checkout in self.job.with_prs:
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
            workspace.fetch(checkout.src, checkout.base)
        policy_dir = self.run.repo.layer or job.ws / ".agents"
        merge_order = sorted(prs, key=lambda c: -len(c.wt.parts) if policy_dir.is_relative_to(c.wt) else 0)
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
        for name in ("checked", "report"):
            self.data.pop(name, None)
        if "job" in self.data:
            self.data["job"] = {**self.data["job"], "reviews": 0, "ci_result": ""}

    def lineage(self):
        tid = str(getattr(self.run, "tid", ""))
        pinned = pilot.is_pilot_task(tid) or zte.reserved(tid)
        restarts = not pinned and any(c.get("pr") for c in self.data.get("prepared", {}).get("checkouts", ()))
        self.catalog = Catalog(self.job)
        policy = self.policy_sha()
        saved_policy = self.data.get("policy_sha")
        if saved_policy is not None and self.matches_policy(saved_policy, policy):
            policy = saved_policy
        found = {"catalog_sha": self.catalog.fingerprint, "operations_sha": ledger.digest(describe_operations()),
                 "policy_sha": policy,
                 "controller_prompt": ledger.digest(prompt_loader.template("orchestrate").template),
                 "controller_settings": ledger.digest(asdict(self.run.settings("orchestrate"))),
                 "run_settings": ledger.digest([self.run.overrides, self.run.step_overrides])}
        old = {key: self.data[key] for key, value in found.items() if key in self.data and self.data[key] != value}
        changes = ", ".join(f"{key} changed ({str(value)[:8]} → {found[key][:8]})" for key, value in old.items())
        if old.keys() - REFRESHABLE_LINEAGE and not restarts:
            raise factory.Refused(f"{changes}; reconcile before resuming")
        if why := self.continuation_blocker(decision_boundary=bool(old)):
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
        prints = {c.github: factory.fingerprint(c.wt) for c in self.job.checkouts}
        return ledger.digest(prints) if prints and all(prints.values()) else ""

    def stamp(self, gate, result):
        tree = self.fingerprint()
        if not tree:
            raise ValueError("current workspace fingerprint is unavailable; evidence cannot authorize publication")
        self.data.setdefault("evidence", {})[gate] = {"tree": tree, "policy": self.policy_sha(), "result": result,
            "acceptance": self.data["acceptance"], "producer": self.executing, "observed": util.iso()}
        self.catalog.add_context(f"evidence:{gate}", ledger.encoded(self.data["evidence"][gate]))

    def current(self, gate):
        if gate == "checks" and self.data.get("denied_feedback"):
            return False
        found = self.data.get("evidence", {}).get(gate, {})
        return bool(found and found["tree"] == self.fingerprint()
                    and self.matches_policy(found["policy"], self.policy_sha())
                    and found.get("acceptance") == self.data["acceptance"])

    def recover(self):
        self.quiescent()
        for turn in ledger.pending(self.lease):
            ledger.reject(self.lease, turn["id"], "interrupted controller turn; admitted usage may be unknown")
        catch_up = self.data.get("catch_up_hold", {})
        stale = set(self.data.get("stale_actions", ()))
        for action in ledger.actions(self.lease):
            result = action["result"]
            gate = {"checks.run": "checks", "verification.run": "verify", "review.run": "review"}.get(action["op"])
            old_gate = bool(gate) and action["id"] in stale
            if action["id"] == catch_up.get("action"):
                self.data["evidence"] = {}
            if action["op"] == "owner.ask" and result.get("awaiting_owner"):
                answers = [ask_store.ask_get(ask) for ask in result["ask_ids"]]
                if not answers or any(not answer or answer["status"] not in ("answered", "taken") for answer in answers):
                    raise factory.Refused("owner answers or interrupted question settlement need reconciliation before resuming")
            if action["state"] == "running":
                state = "reconciliation-required" if action["op"] == "publication.request" else "interrupted"
                ledger.transition(self.lease, action["id"], state,
                                  {**result, "error": "execution interrupted; usage may be unknown", "usage_unknown": True})
            if result.get("text") and not old_gate:
                self.catalog.add_context(action["id"], result["text"])
            if gate and not old_gate and action["state"] != "accepted":
                self.data.setdefault("evidence", {}).pop(gate, None)
            if action["state"] == "succeeded":
                if result.get("writing"):
                    self.job.build_summary = self.job.b.build_summary = result.get("text", self.job.build_summary)
                elif result.get("effective", {}).get("prompt_id", "").removeprefix("prompt:") == "plan":
                    self.job.plan_text = self.job.b.plan_text = result.get("text", self.job.plan_text)
                if gate and not old_gate and result.get("evidence"):
                    self.data.setdefault("evidence", {})[gate] = result["evidence"]
                    if gate == "checks":
                        self.job.b.checked, self.job.b.report = result.get("checked", {}), result.get("report", "")
                    elif gate == "review":
                        self.job.review = pipeline.Review.stored(result)
                    else:
                        self.job.verified = Verified.stored(result)
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
        for action in ledger.actions(self.lease):
            run_lines.write(self.run.id, "action", action=action["id"], op=action["op"], state=action["state"],
                            recovered=True, source=action["run"])

    def observe(self, limit=20, after=0):
        all_actions = ledger.actions(self.lease)
        actions = [a for a in all_actions if a["result"].get("revision", 0) > after]
        snapshot = ledger.snapshot(self.lease)
        for worker in self.workers.values():
            self.progress[worker.action] = worker.observations()
            self.reported[worker.action] = worker.report[0]
        return {"version": PROTOCOL, "state_version": snapshot["version"], "cursor": snapshot["version"],
                "root": self.lease.root,
                "task": self.catalog.read("task", 0, 8000), "owner_context": self.run.story[-8:],
                **({"triage": self.job.triage} if self.job.triage else {}),
                "catalog_sha": self.catalog.fingerprint,
                "catalog": {kind: self.catalog.list(kind, 0, 10) for kind in ("models", "prompts", "tools", "context")},
                "actions": [{**{k: a[k] for k in ("id", "op", "state")},
                             "result": self.excerpt(a)} for a in actions[-limit:]],
                "progress": self.progress, "evidence": {name: {**value, "current": self.current(name)}
                                                        for name, value in self.data.get("evidence", {}).items()},
                "limits": {**asdict(self.policy), "decisions_used": snapshot["turns"],
                           "launches_used": snapshot["launches"], "actions_used": len(all_actions),
                           "repair_cycles": self.run.budget.repair_calls if self.run.budget else 0},
                "error": self.data.get("error", "")}

    def excerpt(self, action):
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
                            ledger.launched(self.lease, self.policy.max_decisions)
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

    def decide(self):
        observation = self.observe()
        turn = ledger.begin(self.lease, observation, self.policy.max_decisions)
        observation["decision_id"] = turn
        place = self.run.dir / "controller"
        place.mkdir(exist_ok=True)
        prompt = prompt_loader.prompt("orchestrate", api=help_text(), observation=json.dumps(observation))
        result = {}
        for retry, setting in enumerate(stall_routes(self.run.settings("orchestrate"))):
            request = delegation.request(self.job, turn, prompt, setting, (), [])
            request.update(step=f"orchestrate-{turn}", cwd=str(place))
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
                                          {a["id"]: a for a in ledger.actions(self.lease)})
            actions = ledger.accept(self.lease, turn, observation["state_version"], calls, self.policy.max_actions)
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
        if any(known[dep]["state"] != "succeeded" for dep in args["depends_on"]):
            raise ValueError("a dependency has not succeeded")
        setting, name, writing = self.catalog.worker(args)
        writing = writing and args["tool_bundle"] == "write"
        if len(self.workers) >= self.policy.max_workers or (self.workers and (
                writing or any(worker.writing for worker in self.workers.values()))):
            raise ValueError("worker or workspace mutation lease is busy; wait before starting this delegation")
        role = setting.step
        catch_up = self.data.get("catch_up_hold", {})
        denied_feedback = self.data.get("denied_feedback", "")
        if writing and (self.data.get("writes", 0) or self.run.repairs or catch_up or denied_feedback):
            role = ("ci_repair" if self.data.get("ci_failed") else "review_repair"
                    if catch_up.get("gate") == "review" or self.job.review.verdict == "request_changes"
                    else "check_repair")
            setting = pipeline.steps.resolve(self.job.cfg, self.job.layer, role, self.run.kind,
                                             {"runner": setting.runner, "model": setting.model,
                                              "provider": setting.provider, "effort": setting.effort})
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
        queued = self.data.setdefault("steering", {}).pop((prior or action)["id"], [])
        goal += "\n" + "\n".join(queued) if queued else ""
        if name in ("delegate", "investigate") and self.job.triage:
            contexts = "\n\n".join(filter(None, (self.job.triage, contexts)))
        inputs = {"goal": goal, "context": contexts, "plan": self.job.plan_text, "checks": self.job.checks,
                  "denied": prompt_loader.code_list(diffguard.denied_paths(self.job.layer)),
                  "estimate": self.job.estimate, "verify": self.job.goal, "triage": self.job.triage, **args["inputs"]}
        if name == "plan" and self.job.layer.verify:
            inputs["journeys"] = journeys.guidance(self.job.layer.verify.feature_map, planning=True)
        if "verify" in args["inputs"] and pipeline.triage_checks(self.job.approval):
            inputs["verify"] = pipeline.triage_goal(str(inputs["verify"]), self.job.approval)
        prompt = self.job.brief.prompt("delegate" if name in ("delegate", "investigate") else name, **inputs)
        if writing and denied_feedback:
            prompt += f"\n\n## Check failure to repair\n\n{denied_feedback}"
        session = NO_SESSION
        previous = (prior or {}).get("result", {})
        if previous.get("runner") == setting.runner and previous.get("model") == setting.model:
            session = Session(**previous.get("session", {}))
        request = delegation.request(self.job, action["id"], prompt, setting, tools,
                                     prompt_loader.bash_rules(self.job.builder_bash) if writing else [], session)
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
            ledger.transition(self.lease, action["id"], "running", {"pid": worker.pid, "identity": worker.identity,
                                                                  "effective": args, "writing": writing, "role": role})
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
                    if self.run.budget:
                        self.run.budget.charge(message["step"], message["role"])
                    grant = self.run.launch_gate(message["step"], message["role"], message["route"]) \
                        if self.run.launch_gate else None
                    seconds = grant.get("timeout") if isinstance(grant, dict) else None
                    if type(seconds) in (int, float) and seconds > 0:
                        worker.deadline = min(worker.deadline, time.monotonic() + seconds)
                    if self.run.feedback and not self.run.step_started:
                        run_lines.write(self.run.id, "work_start")
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
                    result = {**message, "effective": worker.effective, "writing": worker.writing}
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
                c.github: (c.base_commit if c in staged.merged
                           else util.git(["merge-base", c.origin_base, "HEAD"], c.wt).strip())
                for c in checkouts
            }
            if not any(util.git(["diff", "--cached", "--name-only", bases[c.github]], c.wt) for c in checkouts):
                raise workspace.RunFailed(f"{after} undid every change")
            heads = {c.github: head for c in checkouts if (head := c.publication_baseline)}
            if not staged.merged and heads and not any(
                util.git(["diff", "--cached", "--name-only", heads.get(c.github, bases[c.github])], c.wt)
                for c in checkouts
            ):
                if (self.run.environment_release and not self.data.get("ci_failed")
                        and flows.retry_passed(self.job.verified) and any(c.pr and c.github in heads for c in checkouts)):
                    whole = staged if b.from_base else b.restage(from_base=True)
                    return b.validate_diff(whole, after, from_base=True)
                label = "head" if len(heads) == 1 else "heads"
                shas = ", ".join(f"`{head}`" for head in heads.values())
                raise UnchangedPushedHead(f"nothing changed since the pushed {label} {shas}")
        b.validate_diff(staged, after)

    def publish(self):
        self.barrier()
        if pending := ledger.catch_up_push(self.data):
            return self.reconcile_catch_up(pending)
        if self.data.get("publication_intent"):
            raise factory.Refused("publication was interrupted; reconcile its branch/PR effects before resuming")
        needed = [gate for gate in ("checks", "review", "verify") if not self.current(gate)]
        if needed:
            raise ValueError("publication needs current evidence: " + ", ".join(needed))
        if not self.data["evidence"]["checks"]["result"].get("full"):
            raise ValueError("publication requires the full owner check policy")
        if self.job.verified.verdict == "fail":
            raise ValueError("behavioral verification failed; repair or finish failed")
        catch_up = self.data.get("catch_up_hold")
        if catch_up and (not flows.retry_passed(self.job.verified) or not self.job.review.approved()):
            raise ValueError("publication after a catch-up hold requires passing verification and an approving review")
        if (not self.run.dry_run and not zte.reserved(str(self.run.tid)) and self.data.get("ci_failed")
                and (retry := self.data.get("ci_retry")) and (tree := self.fingerprint())
                and tree == self.data.get("published_tree")):
            return self.retry_publication_ci(retry, tree)
        self.validate_publication_diff()
        if self.run.dry_run:
            self.ending = flows.end_dry_run(self.flow)
            return {"status": "dry-run"}
        if zte.reserved(str(self.run.tid)):
            grant = zte.prepare_candidate(self.run, self.job, self.data)
            self.ending = ("pr", "Prepared an exact README candidate for engine-owned delivery", "")
            return {"prepared_head": grant.data["head"], "tree": grant.data["tree"]}
        self.check_pilot_publication()
        if catch_up:
            checkout, = self.job.checkouts
            self.take_merge_lease(checkout)
        self.data["publication_intent"] = {"tree": self.fingerprint()}
        self.persist()
        outcome = self.atomic("pr", tree=str(self.job.ws))
        if not outcome.passed:
            self.ending = flows.ended(self.flow, outcome)
            return outcome.data
        self.data.pop("publication_intent", None)
        self.data.pop("worker_trees", None)
        self.job.b.worker_trees = {}
        self.data.pop("ignored_denied_paths", None)
        self.job.b.ignored_denied_paths = {}
        self.data["publication"] = outcome.data
        self.persist()
        if catch_up:
            for checkout in self.job.picked(self.job.changed):
                pipeline.publish_gate_evidence(self.run, self.job.ticket, checkout, self.job.verified, self.job.review)
        if self.run.repo.parts and (failure := self.publication_ci(self.job.picked(self.job.changed))):
            return self.settle_publication_failure(failure)
        return self.finish_publication()

    def retry_publication_ci(self, retry, tree):
        job = self.job
        self.check_pilot_publication()
        flows.require_environment_owner_clear(job)
        flows.refuse_changed_prs(job)
        checkouts = {c.github: c for c in job.with_prs}
        heads = self.data["publication"]["heads"]
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
            return failure
        if retry["caught_up"]:
            checkout, = pushed
            self.take_merge_lease(checkout)
        for checkout in pushed:
            row = gh.pr_merge_details(checkout.pr)
            if (row.get("state") != "OPEN" or row.get("headRefOid") != checkout.lease
                    or row.get("baseRefName") != checkout.base):
                raise ValueError("the PR no longer matches this run's published head and base")
        saved = retry.get("reruns")
        if automatic_requested or (saved is None and not retry.get("legacy_rerun")):
            if self.refresh_publication_ci(pushed, retry, automatic_requested):
                return self.finish_ci_retry()
            if saved is None:
                return self.settle_publication_failure(self.publication_ci_failure())
        launches = {}
        if saved is not None:
            reruns = {repo: [gh.WorkflowRerun(**row) for row in requested] for repo, requested in saved.items()}
        else:
            self.data.setdefault("ci_retried_heads", retried)
            reruns = {c.github: gh.failed_job_reruns(c.github, c.pr, c.lease, targets[c.github]["jobs"])
                      for c in pushed if c.lease not in retried.get(c.github, [])}
            if not any(reruns.values()):
                raise ValueError("the failed CI has no GitHub Actions jobs to rerun")
            for checkout in pushed:
                if reruns.get(checkout.github):
                    retried.setdefault(checkout.github, []).append(checkout.lease)
            launches = reruns
        self.data["publication_intent"] = {"tree": tree, "phase": "ci-rerun"}
        self.persist()
        for repo, requested in launches.items():
            for rerun in requested:
                gh.rerun_failed_jobs(repo, rerun.run)
        failure = self.publication_ci(pushed, caught_up=retry["caught_up"], reruns=reruns)
        if failure:
            self.data.pop("publication_intent", None)
            if saved is not None and not automatic_requested:
                targets = self.data["ci_retry"]["targets"]
                if second_failure := self.second_ci_failure(targets, retried):
                    return second_failure
            return self.settle_publication_failure(failure)
        return self.finish_ci_retry()

    def finish_ci_retry(self):
        job = self.job
        flows.require_environment_owner_clear(job)
        fresh = [c for c in job.picked(job.opened) if c.github not in job.found]
        pipeline.ready_pushed(self.run, fresh, job.picked(job.ci_stopped), job.draft_why)
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

    def publication_ci_failure(self):
        return {"ci": self.job.ci_result, "failures": CI_FAILURES,
                "error": f"CI needs an explicit worker diagnosis or repair; its failure logs are context {CI_FAILURES}"}

    def clear_ci_failure(self):
        for key in ("ci_retry", "ci_failed", "ci_failures"):
            self.data.pop(key, None)
        self.catalog.context.pop(CI_FAILURES, None)

    def report_publication_ci(self, pushed, caught_up, section):
        self.check_pilot_publication()
        if caught_up:
            for checkout in pushed:
                pipeline.comment_catch_up_ci(self.run, checkout, section, self.job.verified, self.job.review)
        else:
            pipeline.publish_ci(self.run, self.job.ticket, self.job.with_prs, section)

    def publication_ci(self, pushed, *, caught_up=False, reruns=None):
        tree = self.fingerprint()
        saved = self.data.get("ci_retry", {}) if reruns is not None else {}
        max_rounds = saved.get("max_rounds", 2) if reruns is not None else self.run.settings("ci_repair").tries
        earlier = [pipeline.ci.Round(n, [], story) for n, story in enumerate(saved.get("round_stories", []), 1)]
        requested_heads = {(repo, head) for name in ("ci_rerun_requested_heads", "ci_retried_heads")
                           for repo, heads in self.data.get(name, {}).items() for head in heads}
        automatic = pipeline.AutomaticCIReruns(requested_heads, self.remember_ci_rerun)
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
        for checkout in self.job.with_prs:
            self.check_pilot_publication()
            if check_head and (watched_heads.get(checkout.pr) != checkout.lease
                               or pipeline.own_draft_state(self.run, checkout) is None):
                continue
            pipeline.set_draft(self.run, checkout, True)
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
        for checkout in candidates:
            if len(job.checkouts) == 1:
                self.take_merge_lease(checkout)
            while True:
                hold = pipeline.try_automerge(job.b, checkout, job.verified, job.review, about,
                                              track_moved_base=track_base)
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
                elif hold and self.data.get("catch_up_hold"):
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
        except pipeline.CatchUpGateHold as error:
            return self.hold_catch_up(checkout, base_sha, error)
        except pipeline.CatchUpPushUnconfirmed:
            raise
        except (workspace.RunFailed, RuntimeError, OSError) as e:
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
        for checkout in self.job.with_prs:
            if pipeline.own_draft_state(self.run, checkout) is False:
                pipeline.set_draft(self.run, checkout, True, "automerge")
        self.run.say(f"{held}. I parked the run because {reason}.", on_ticket=on_ticket)
        reason = f"{held}. {reason}"
        if close_failed:
            return pipeline.record_park(self.run, reason, {})
        return pipeline.park_run(self.run, reason)

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

        self.data["publication_intent"].pop("catch_up", None)
        self.persist()
        pipeline.publish_gate_evidence(self.run, job.ticket, checkout, job.verified, job.review)

    def finish_publication(self):
        self.check_pilot_publication()
        self.data["publication_intent"] = {"tree": self.fingerprint(), "phase": "publish"}
        self.persist()
        outcome = self.atomic("publish", merge=self.automerge)
        self.data.pop("publication_intent", None)
        if outcome.passed:
            self.data["published"], self.data["published_tree"] = outcome.data, self.fingerprint()
        else:
            self.data.pop("published", None)
            if not self.data.get("ci_retry"):
                self.data.pop("published_tree", None)
        self.persist()
        if self.data.get("catch_up_hold") or self.data.get("ci_failed"):
            return self.settle_publication_failure(outcome.data)
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
        if rationale is None:
            yield
            return
        comment_rationale = outbound.screen(rationale.strip()).encode("utf-8", "backslashreplace").decode("utf-8")
        with principals.acting(f"run:{self.run.id}/respond"):
            self.run.say(f"Run `{self.run.id}` failed without delivery.\n\n{comment_rationale}"
                         f"\n\n{self.run.retry_hint}.")
        say = self.run.say
        self.run.say = lambda _body: ""
        try:
            yield
        finally:
            self.run.say = say

    def perform(self, action):
        args, op = action["request"], action["op"]
        if op == "worker.start":
            return self.worker_start(action, args)
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
            if worker := self.workers.get(args["worker_id"]):
                worker.close()
                del self.workers[args["worker_id"]]
                if self.run.launch_done:
                    self.run.launch_done(worker.step, False)
                ledger.transition(self.lease, args["worker_id"], "cancelled",
                                  {"reason": args["reason"], "effective": worker.effective, "usage_unknown": True})
                return {"cancelled": args["worker_id"], "quiescent": True}
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
        if op == "checks.run":
            self.data.setdefault("evidence", {}).pop("checks", None)
            self.persist()
            b = self.job.b
            b.denied_feedback = self.data.get("denied_feedback", "")
            b.on_denied_edit = self.hold_for_denied_edit
            try:
                outcome = self.atomic("checks", tree=str(self.job.ws), quick=args["scope"] == "quick")
            finally:
                b.on_denied_edit = None
            if self.data.get("denied_feedback"):
                self.data["denied_feedback"] = b.report
            if outcome.passed:
                self.stamp("checks", outcome.data)
            return {"passed": outcome.passed, **outcome.data}
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
                                                   else {"attempt": self.job.reviews + 1}))
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
            return self.publish()
        if op == "run.finish":
            if args["reason"] == "noop" and self.data.get("ci_failed"):
                raise ValueError("CI is still failed; retry CI through publication.request, repair, or finish failed")
            if args["reason"] == "delivered":
                published = self.data.get("published", {})
                if (published.get("ending") not in ("done", "pr") or self.data.get("ci_failed")
                        or self.data.get("published_tree") != self.fingerprint()
                        or not all(self.current(gate) for gate in ("checks", "verify", "review"))):
                    raise ValueError("delivery has no successful current engine publication")
                self.ending = (published["ending"], published.get("summary", ""),
                               " ".join(c.pr for c in self.job.with_prs))
            elif self.data.get("catch_up_hold"):
                self.ending = self.park_catch_up("the orchestrator stopped without delivery")
            elif args["reason"] == "noop":
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

    def dispatch(self, action) -> ValueError | None:
        if action["state"] != "accepted":
            return
        if action["op"] != "worker.start" and action["op"] != "worker.continue":
            ledger.transition(self.lease, action["id"], "running", {"input_stamp": {
                "tree": self.fingerprint(), "policy": self.policy_sha(), "acceptance": self.data["acceptance"]}})
        refusal = None
        try:
            self.executing = action["id"]
            result = self.perform(action)
        except owner_asks.Asked as asked:
            ledger.transition(self.lease, action["id"], "succeeded",
                              {"ask_ids": asked.ask_ids, "awaiting_owner": True})
            self.persist()
            run_lines.write(self.run.id, "action", action=action["id"], op=action["op"], state="succeeded")
            raise
        except RetargetedPR as error:
            refusal = error
            self.ending = self.hold_revision(error.pr, str(error))
            self.data.pop("publication_intent", None)
            result = {"error": str(error)}
            ledger.transition(self.lease, action["id"], "failed", result)
        except ValueError as error:
            refusal = error
            result = {"error": str(error)[:2000]}
            ledger.transition(self.lease, action["id"], "failed", result)
        except BaseException as error:
            if action["id"] in self.workers:
                raise
            state = "reconciliation-required" if action["op"] == "publication.request" else "interrupted"
            ledger.transition(self.lease, action["id"], state,
                              {"error": f"stopped by {type(error).__name__}",
                               "usage_unknown": action["op"] in ("worker.start", "worker.continue", "verification.run", "review.run")})
            self.persist()
            run_lines.write(self.run.id, "action", action=action["id"], op=action["op"], state=state)
            raise
        else:
            if result is not None:
                gate = {"checks.run": "checks", "verification.run": "verify", "review.run": "review"}.get(action["op"])
                if gate and self.data.get("evidence", {}).get(gate, {}).get("producer") == action["id"]:
                    result["evidence"] = self.data["evidence"][gate]
                ledger.transition(self.lease, action["id"], "failed" if "error" in result else "succeeded", result)
        self.persist()
        run_lines.write(self.run.id, "action", action=action["id"], op=action["op"],
                        state="running" if result is None else "failed" if "error" in result else "succeeded")
        return refusal

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
                if prior is None or prior["op"] not in allowed or prior["state"] not in ledger.TERMINAL:
                    raise factory.Refused("rerun requires a finished worker/check/review/verification action ID")
                snapshot = ledger.snapshot(self.lease)
                turn = ledger.begin(self.lease, {"rerun": prior["id"]}, self.policy.max_decisions)
                requester = "Live release retry" if self.run.environment_release else "Owner requested rerun"
                response = {"version": PROTOCOL, "decision_id": turn, "state_version": snapshot["version"],
                            "rationale": f"{requester} of {prior['id']}",
                            "calls": [{"op": prior["op"], "args": prior["request"]}]}
                try:
                    response = self.catalog.validate(response, turn, snapshot["version"], known,
                                                     enforce_size_limit=False)
                except (ValueError, TypeError) as error:
                    ledger.reject(self.lease, turn, str(error)[:2000])
                    raise factory.Refused(f"cannot rerun {prior['id']} under the current catalog: {error}") from error
                rerun = ledger.accept(self.lease, turn, snapshot["version"], response, self.policy.max_actions)[0]
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
                    return ("step" if result["state"] == "succeeded" else "failed",
                            f"{rerun['id']} {result['state']}; dependent evidence must be refreshed", "")
            while not self.ending:
                self.collect()
                pending = [a for a in ledger.actions(self.lease) if a["state"] == "accepted"]
                for action in pending or self.decide():
                    self.dispatch(action)
                    if self.ending:
                        break
            return self.ending
        except runlimits.Parked as error:
            if self.data.get("catch_up_hold"):
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
                if park_reason is not None:
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
                del self.workers[key]
            except BaseException as error:
                errors.append(error)
        self.release_merge_lease()
        if errors:
            raise errors[0]


@factory.step("orchestrate_ask")
def orchestrate_ask(flow, request):
    flow.ctx.run.owner_ask("orchestrate", [request], NO_SESSION)
    return factory.Outcome("pass")


def orchestrate(flow):
    return Controller(flow).drive()


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
