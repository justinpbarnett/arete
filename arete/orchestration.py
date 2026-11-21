from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import asdict
from math import inf
from pathlib import Path

from . import (
    codebase,
    delegation,
    diffguard,
    factory,
    flows,
    gh,
    journeys,
    owner_asks,
    pilot,
    pipeline,
    prompt_loader,
    repairs,
    run_lines,
    runlimits,
    util,
    workspace,
    zte,
)
from .agent import NO_SESSION, Session, json_from_text
from .orchestration_api import BUNDLES, PROTOCOL, Catalog, help_text
from .store import asks as ask_store
from .store import orchestration as ledger
from .verify import Verified

STATE_FIELDS = ("plan_text", "goal", "estimate", "left_for_a_person", "build_summary", "reviews", "fix_reply",
                "document", "report_body", "draft_why", "summary", "opened", "found", "pushed", "drafted",
                "ci_stopped", "changed", "ci_result", "committed")


def policy_fingerprint(cfg, layer, closed_context):
    effective = asdict(layer)
    effective.pop("factory_path")
    if layer.verify:
        effective["verify"] = layer.verify.policy()
    return ledger.digest(json.loads(json.dumps({"layer": effective, "config": asdict(cfg),
                                                "closed_context": closed_context}, default=str)))


class Controller:
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
            self.data["prepared"]["checkouts"] = [checkout.to_row() for checkout in self.job.checkouts]
        if self.job.built:
            self.data["checked"] = self.job.b.checked
            self.data["report"] = self.job.b.report
        ledger.save(self.lease, self.data)

    def atomic(self, name, **kw):
        self.flow.stepped = True
        outcome = self.flow.run(name, (), kw)
        self.job.take(name, outcome, stored=False)
        return outcome

    def quiescent(self):
        for turn in ledger.pending(self.lease):
            launch = turn["response"] or {}
            if delegation.live(launch.get("pid"), launch.get("identity")):
                raise factory.Refused("the prior controller inference is still live; no replacement was launched")
        for action in ledger.actions(self.lease):
            result = action["result"]
            if action["state"] == "running" and delegation.live(result.get("pid"), result.get("identity")):
                raise factory.Refused(f"worker {action['id']} is still live; no replacement was launched")

    def prepare(self):
        if self.flow.refusal:
            raise factory.Refused(self.flow.refusal)
        self.quiescent()
        if self.run.feedback and (closed := [url for url in self.run.feedback_prs if gh.pr_view(url).get("state") != "OPEN"]):
            return "noop", f"the PR closed before arete could revise it: {', '.join(closed)}", ""
        self.flow.matching = False
        run_lines.write(self.run.id, "route", continues=self.flow.continues, mode="orchestrated", protocol=PROTOCOL)
        if self.data.get("prepared"):
            if not {"run_settings", "overrides", "step_overrides"} <= self.data.keys():
                raise factory.Refused("the saved controller lacks accepted invocation settings; reconcile before resuming")
            prepared = self.data["prepared"]
            layer_dir, source_file = Path(prepared["layer"]), None
            if not layer_dir.exists():
                for checkout in map(workspace.Checkout.from_row, prepared["checkouts"]):
                    if layer_dir.is_relative_to(checkout.wt):
                        source_file = checkout.src / layer_dir.relative_to(checkout.wt) / "config.toml"
                        break
            self.job.layer = pipeline.checked_layer(self.job.cfg, layer_dir, source_file=source_file)
            self.run.cfg, self.run.layer, self.run.kind = self.job.cfg, self.job.layer, prepared["ticket"]["kind"]
            self.lineage()
            if any(not (Path(c["wt"]) / ".git").exists() for c in prepared["checkouts"]):
                if any(Path(c["wt"]).exists() for c in prepared["checkouts"]):
                    raise factory.Refused("the retained workspace is incomplete; reconcile without discarding its edits")
                if ledger.publication_pending(self.run.id) or not any(
                        a["op"] == "publication.request" and a["state"] == "succeeded" for a in ledger.actions(self.lease)):
                    raise factory.Refused("the missing workspace has no settled publication; reconcile before resuming")
                if why := self.job.restore({"step": "prepare", "tree": str(self.job.ws), "data": prepared}, layer=self.job.layer):
                    raise factory.Refused(why)
            self.job.take("prepare", factory.Outcome("pass", data=self.data["prepared"]), stored=True)
            for name, value in self.data.get("job", {}).items():
                if name in STATE_FIELDS:
                    setattr(self.job, name, value)
            self.job.review = pipeline.Review.stored(self.data.get("review", {}))
            self.job.verified = Verified.stored(self.data.get("verified", {}))
            self.job.b.checked = self.data.get("checked", {})
            self.job.b.report = self.data.get("report", "")
            self.job.b.from_base = True
            self.job.b.plan_text, self.job.b.build_summary = self.job.plan_text, self.job.build_summary
        else:
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
            targets = [c for c in self.job.checkouts if c.pr in found.prs]
            if set(found.prs) != {c.pr for c in targets}:
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
        known = {(f.get("pr_url"), f.get("kind"), f.get("gh_id")) for f in self.data.get("feedback", [])}
        fresh = [f for f in self.run.feedback if (f.get("pr_url"), f.get("kind"), f.get("gh_id")) not in known]
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
        self.data.update(self.lineage())
        self.data["overrides"], self.data["step_overrides"] = self.run.overrides, self.run.step_overrides
        self.data.setdefault("code", codebase.STAMP)
        self.data["recovery_code"] = codebase.STAMP
        self.data["limits"] = asdict(self.policy)
        self.data["acceptance"] = ledger.digest([self.catalog.context["task"], self.job.goal,
                                                 self.job.left_for_a_person])
        self.recover()
        if self.flow.continues and (moved := flows.base_moved(self.flow.continues)):
            self.job.moved_base = moved
            self.job.merge_moved_base("orchestrate")
        self.persist()
        return None

    def lineage(self):
        self.catalog = Catalog(self.job)
        found = {"catalog_sha": self.catalog.fingerprint, "policy_sha": self.policy_sha(),
                 "controller_prompt": ledger.digest(prompt_loader.template("orchestrate").template),
                 "controller_settings": ledger.digest(asdict(self.run.settings("orchestrate"))),
                 "run_settings": ledger.digest([self.run.overrides, self.run.step_overrides])}
        if any(key in self.data and self.data[key] != value for key, value in found.items()):
            raise factory.Refused("the admitted catalog, controller or policy changed; reconcile before resuming")
        return found

    def policy_sha(self):
        return policy_fingerprint(self.job.cfg, self.job.layer, self.run.closed_context)

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
        found = self.data.get("evidence", {}).get(gate, {})
        return bool(found and found["tree"] == self.fingerprint() and found["policy"] == self.policy_sha()
                    and found.get("acceptance") == self.data["acceptance"])

    def recover(self):
        self.quiescent()
        for turn in ledger.pending(self.lease):
            ledger.reject(self.lease, turn["id"], "interrupted controller turn; admitted usage may be unknown")
        for action in ledger.actions(self.lease):
            result = action["result"]
            if action["op"] == "owner.ask" and result.get("awaiting_owner"):
                answers = [ask_store.ask_get(ask) for ask in result["ask_ids"]]
                if not answers or any(not answer or answer["status"] not in ("answered", "taken") for answer in answers):
                    raise factory.Refused("owner answers or interrupted question settlement need reconciliation before resuming")
            if action["state"] == "running":
                state = "reconciliation-required" if action["op"] == "publication.request" else "interrupted"
                ledger.transition(self.lease, action["id"], state,
                                  {**result, "error": "execution interrupted; usage may be unknown", "usage_unknown": True})
            if result.get("text"):
                self.catalog.add_context(action["id"], result["text"])
            gate = {"checks.run": "checks", "verification.run": "verify", "review.run": "review"}.get(action["op"])
            if gate and action["state"] != "accepted":
                self.data.setdefault("evidence", {}).pop(gate, None)
            if action["state"] == "succeeded":
                if result.get("writing"):
                    self.job.build_summary = self.job.b.build_summary = result.get("text", self.job.build_summary)
                elif result.get("effective", {}).get("prompt_id", "").removeprefix("prompt:") == "plan":
                    self.job.plan_text = self.job.b.plan_text = result.get("text", self.job.plan_text)
                gate = {"checks.run": "checks", "verification.run": "verify", "review.run": "review"}.get(action["op"])
                if gate and result.get("evidence"):
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

    def decide(self):
        observation = self.observe()
        turn = ledger.begin(self.lease, observation, self.policy.max_decisions)
        observation["decision_id"] = turn
        place = self.run.dir / "controller"
        place.mkdir(exist_ok=True)
        prompt = prompt_loader.prompt("orchestrate", api=help_text(), observation=json.dumps(observation))
        request = delegation.request(self.job, turn, prompt, self.run.settings("orchestrate"), (), [])
        request.update(step=f"orchestrate-{turn}", cwd=str(place))
        controller = delegation.Worker(self.run, turn, request)
        result = {"ok": False, "error": "controller exited without a decision; usage may be unknown"}
        complete = False
        try:
            ledger.turn_started(self.lease, turn, controller.pid, controller.identity)
            while not complete:
                self.collect()
                for message in controller.receive():
                    if message.get("event") == "launch":
                        if self.run.budget:
                            self.run.budget.charge(message["step"], message["role"])
                        grant = self.run.launch_gate(message["step"], message["role"], message["route"]) \
                            if self.run.launch_gate else None
                        ledger.launched(self.lease, self.policy.max_decisions)
                        controller.allow(grant)
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
        if not result.get("ok"):
            error = str(result.get("error") or "controller returned no decision")[:2000]
            ledger.reject(self.lease, turn, error)
            self.data["error"] = error
            self.persist()
            raise workspace.RunFailed(error, recovery=result.get("recovery"))
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
        if writing and (self.data.get("writes", 0) or self.run.repairs):
            role = ("ci_repair" if self.data.get("ci_failed") else "review_repair"
                    if self.job.review.verdict == "request_changes" else "check_repair")
            setting = pipeline.steps.resolve(self.job.cfg, self.job.layer, role, self.run.kind,
                                             {"runner": setting.runner, "model": setting.model,
                                              "provider": setting.provider, "effort": setting.effort})
        tools = tuple(tool for tool in BUNDLES[args["tool_bundle"]] if tool in setting.tools)
        if writing and not {"Write", "Edit"} & set(tools):
            raise ValueError("this role grants no implementation writing tools")
        contexts = "\n\n".join(self.catalog.read(ref, 0, 8000)["text"] for ref in args["context_refs"])
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
        session = NO_SESSION
        previous = (prior or {}).get("result", {})
        if previous.get("runner") == setting.runner and previous.get("model") == setting.model:
            session = Session(**previous.get("session", {}))
        request = delegation.request(self.job, action["id"], prompt, setting, tools,
                                     prompt_loader.bash_rules(self.job.builder_bash) if writing else [], session)
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
                    worker.allow(grant)
                elif message.get("event") in ("result", "error"):
                    worker.close()
                    del self.workers[key]
                    if self.run.launch_done:
                        self.run.launch_done(worker.step, bool(message.get("ok")))
                    self.run.cost += message.get("cost_usd", 0)
                    self.run.paid += message.get("paid_usd", 0)
                    result = {**message, "effective": worker.effective}
                    if worker.writing:
                        workspace.note_made(self.job.ws, self.job.checkouts,
                                            pipeline.workspace_owner(self.run, self.job.ws, self.job.checkouts))
                        if why := workspace.kept_work(self.job.ws, self.job.checkouts, self.run.repo.path,
                                                       self.job.layer.copy, commits=False,
                                                       held=workspace.scratch_left(self.job.ws, self.job.checkouts)):
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

    def publish(self):
        self.barrier()
        needed = [gate for gate in ("checks", "review", "verify") if not self.current(gate)]
        if needed:
            raise ValueError("publication needs current evidence: " + ", ".join(needed))
        if not self.data["evidence"]["checks"]["result"].get("full"):
            raise ValueError("publication requires the full owner check policy")
        if self.job.verified.verdict == "fail":
            raise ValueError("behavioral verification failed; repair or finish failed")
        self.job.b.require_valid_diff("the final orchestrated tree")
        if self.run.dry_run:
            self.ending = flows.end_dry_run(self.flow)
            return {"status": "dry-run"}
        if zte.reserved(str(self.run.tid)):
            grant = zte.prepare_candidate(self.run, self.job, self.data)
            self.ending = ("pr", "Prepared an exact README candidate for engine-owned delivery", "")
            return {"prepared_head": grant.data["head"], "tree": grant.data["tree"]}
        self.check_pilot_publication()
        self.data["publication_intent"] = {"tree": self.fingerprint()}
        self.persist()
        outcome = self.atomic("pr", tree=str(self.job.ws))
        if not outcome.passed:
            self.ending = flows.ended(self.flow, outcome)
            return outcome.data
        self.data.pop("publication_intent", None)
        self.data["publication"] = outcome.data
        self.persist()
        if self.run.repo.parts:
            self.job.slot.free()
            try:
                with self.run.waiting("ci"):
                    current = pipeline.ci.Round(1, [pipeline.ci.compare_with_base(
                        pipeline.ci.watch(pipeline.ci_target(c), self.run.log), self.run.log)
                        for c in self.job.picked(self.job.changed)])
            finally:
                self.job.slot.take(self.run.log, self.run.waiting)
            self.job.ci_result = current.summary()
            self.check_pilot_publication()
            pipeline.publish_ci(self.run, self.job.ticket, self.job.with_prs, pipeline.ci.section([current], 1))
            if current.reds or not current.all_read:
                self.data["ci_failed"] = True
                for checkout in self.job.with_prs:
                    self.check_pilot_publication()
                    pipeline.set_draft(self.run, checkout, True)
                return {"ci": self.job.ci_result, "error": "CI needs an explicit worker diagnosis or repair"}
            self.data.pop("ci_failed", None)
        self.check_pilot_publication()
        self.data["publication_intent"] = {"tree": self.fingerprint(), "phase": "publish"}
        self.persist()
        outcome = self.atomic("publish")
        self.data.pop("publication_intent", None)
        if outcome.passed:
            self.data["published"], self.data["published_tree"] = outcome.data, self.fingerprint()
        else:
            self.data.pop("published", None)
            self.data.pop("published_tree", None)
        self.persist()
        self.ending = flows.ended(self.flow, outcome)
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
            outcome = self.atomic("checks", tree=str(self.job.ws), quick=args["scope"] == "quick")
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
            if args["reason"] == "delivered":
                published = self.data.get("published", {})
                if (published.get("ending") not in ("done", "pr") or self.data.get("ci_failed")
                        or self.data.get("published_tree") != self.fingerprint()
                        or not all(self.current(gate) for gate in ("checks", "verify", "review"))):
                    raise ValueError("delivery has no successful current engine publication")
                self.ending = (published["ending"], published.get("summary", ""),
                               " ".join(c.pr for c in self.job.with_prs))
            elif args["reason"] == "noop":
                if not self.job.b.restage().empty:
                    raise ValueError("noop cannot discard an implementation diff")
                self.ending = flows.finish(self.flow, "nothing", summary=self.job.build_summary or "No change needed.")
            else:
                self.ending = flows.finish(self.flow, "failed", why="the orchestrator stopped without delivery",
                                           recovery=self.failure_recovery())
            return {"reason": args["reason"]}
        raise ValueError("unknown orchestration operation")

    def dispatch(self, action):
        if action["state"] != "accepted":
            return
        if action["op"] != "worker.start" and action["op"] != "worker.continue":
            ledger.transition(self.lease, action["id"], "running", {"input_stamp": {
                "tree": self.fingerprint(), "policy": self.policy_sha(), "acceptance": self.data["acceptance"]}})
        try:
            self.executing = action["id"]
            result = self.perform(action)
        except owner_asks.Asked as asked:
            ledger.transition(self.lease, action["id"], "succeeded",
                              {"ask_ids": asked.ask_ids, "awaiting_owner": True})
            self.persist()
            run_lines.write(self.run.id, "action", action=action["id"], op=action["op"], state="succeeded")
            raise
        except ValueError as error:
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

    def drive(self):
        try:
            if ended := self.prepare():
                return ended
            if self.run.action:
                prior = next((a for a in ledger.actions(self.lease) if a["id"] == self.run.action), None)
                allowed = ("worker.start", "worker.continue", "checks.run", "verification.run", "review.run")
                if prior is None or prior["op"] not in allowed or prior["state"] not in ledger.TERMINAL:
                    raise factory.Refused("rerun requires a finished worker/check/review/verification action ID")
                snapshot = ledger.snapshot(self.lease)
                turn = ledger.begin(self.lease, {"rerun": prior["id"]}, self.policy.max_decisions)
                response = {"version": PROTOCOL, "decision_id": turn, "state_version": snapshot["version"],
                            "rationale": f"Owner requested rerun of {prior['id']}",
                            "calls": [{"op": prior["op"], "args": prior["request"]}]}
                rerun = ledger.accept(self.lease, turn, snapshot["version"], response, self.policy.max_actions)[0]
                self.dispatch(rerun)
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
        except ledger.Conflict as error:
            raise runlimits.Parked(str(error)) from error
        finally:
            self.close()

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
                controller.dispatch(action)
                while controller.workers:
                    controller.collect()
                    time.sleep(0.05)
                if any(a["state"] in ("failed", "interrupted") for a in ledger.actions(controller.lease)):
                    return flows.finish(flow, "failed", why="single-worker execution did not pass its required gates",
                                        recovery=controller.failure_recovery())
                if controller.ending:
                    return controller.ending
        return flows.finish(flow, "failed", why="single-worker execution did not deliver")
    finally:
        controller.close()
