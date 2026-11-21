import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Template
from types import SimpleNamespace
from unittest.mock import patch

from arete import (
    codebase,
    config,
    control,
    dbstats,
    delegation,
    factory,
    flows,
    gh,
    jira,
    launch_lease,
    orchestration,
    outcomes,
    park_retries,
    paths,
    pipeline,
    poll,
    record,
    run_lines,
    runlimits,
    sandbox,
    scratch,
    steps,
    tasks,
    tracker,
    triage,
    util,
    verify,
    workspace,
)
from arete.agent import Result
from arete.commands.tasks import task_reply
from arete.orchestration_api import MAX_BYTES, OPERATIONS, PROTOCOL, Catalog, describe_operations
from arete.store import asks as ask_store
from arete.store import db as foundation
from arete.store import insights as insights_store
from arete.store import orchestration as ledger
from arete.store import runs as run_store
from arete.store import tasks as tasks_store
from arete.store import writes
from tests.harness import set_task_body


class Isolated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="arete-schema-")
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        cls.template = root / "state/arete.db"
        with contextlib.ExitStack() as patches:
            patches.enter_context(patch.object(foundation, "_CHECKED", set()))
            patches.enter_context(patch.object(dbstats.Timing, "finished"))
            for name, value in {"STATE_DIR": root / "state", "DB_FILE": cls.template,
                                "RUNS_DIR": root / "runs", "WORKTREES_DIR": root / "worktrees",
                                "LOCKS_DIR": root / "state/locks", "LOG_DIR": root / "logs"}.items():
                patches.enter_context(patch.object(paths, name, value))
            foundation.migrate()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="arete-limits-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(foundation, "_CHECKED", set()))
        self.patches.enter_context(patch.object(dbstats.Timing, "finished"))
        self.patches.enter_context(patch.object(tasks, "PENDING", {}))
        for name, value in {"STATE_DIR": self.root / "state", "RUNS_DIR": self.root / "runs",
                            "DB_FILE": self.root / "state/arete.db", "LOCKS_DIR": self.root / "state/locks",
                            "CONFIG_DIR": self.root / "config", "CONFIG_FILE": self.root / "config/config.toml",
                            "LOG_DIR": self.root / "logs", "EVENTS_FILE": self.root / "state/events.jsonl",
                            "SERVER_FILE": self.root / "state/server.json",
                            "WORKTREES_DIR": self.root / "worktrees"}.items():
            self.patches.enter_context(patch.object(paths, name, value))
        paths.CONFIG_DIR.mkdir()
        paths.ensure_dirs()
        shutil.copyfile(self.template, paths.DB_FILE)
        self.repo = config.Repo(name="fixture", path=self.root / "repo", tracker="local", pipeline=True)
        self.repo.path.mkdir()
        self.cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8)
        self.ticket = tasks_store.task_add(self.repo.name, "Fixture specification", "Fixture", "feat")
        self.local = tracker.LocalTracker(self.repo)

    def run(self, result=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return super().run(result)

    def made_run(self, previous="", forced=False):
        rid = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        directory = paths.RUNS_DIR / rid
        directory.mkdir()
        run = pipeline.Run(rid, self.repo, directory, tracker=self.local, tid=self.ticket, cfg=self.cfg)
        run.budget = runlimits.Budget(run, runlimits.Policy(), previous, self.local.get(self.ticket), forced)
        return run


class OrchestrationLineageTests(Isolated):
    def setUp(self):
        super().setUp()
        self.patches.enter_context(patch.object(pipeline, "workspace_note", return_value="Fixture workspace"))

    def controller(self, previous="", feedback=()):
        run = self.made_run(previous)
        run.path = "orchestrate"
        run.feedback = list(feedback)
        return orchestration.Controller(flows.start(self.cfg, run, {}, continues=previous))

    def admit(self):
        controller = self.controller()
        outcome = factory.Outcome("pass", data={
            "layer": str(self.repo.layer_dir), "checkouts": [], "ticket": asdict(self.local.get(self.ticket)),
            "branch": "fixture", "add_dirs": [], "attachments": "", "expertise": "", "triage": "",
        })

        def prepare_step(*args, **kwargs):
            controller.job.take("prepare", outcome, stored=False)
            return outcome

        with patch.object(controller, "atomic", side_effect=prepare_step):
            self.assertIsNone(controller.prepare())
        return controller

    def accept(self, controller, op, state, args=None, **result):
        lease = controller.lease
        version = ledger.snapshot(lease)["version"]
        turn = ledger.begin(lease, {}, 10)
        action, = ledger.accept(lease, turn, version, {"calls": [{"op": op, "args": args or {}}]}, 10)
        if state != "accepted":
            ledger.transition(lease, action["id"], state, result)
        return action["id"]

    def audits(self, controller):
        return [line for line in run_lines.read(controller.run.id) if line["event"] == "lineage"]

    def test_operations_refresh_reaches_the_next_decision_and_audits_once(self):
        wait = OPERATIONS["run.wait"]
        old_wait = replace(wait, description="Old wait", constraints={"timeout_seconds": {"minimum": 1}})
        with patch.dict(OPERATIONS, {"run.wait": old_wait}):
            prior = self.admit()
        record.move(prior.run.id, "failed", ("running",))
        resumed = self.controller(prior.run.id)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertIsNone(resumed.prepare())
        catalog_sha = prior.data["catalog_sha"]
        new = ledger.digest(describe_operations())
        audit, = self.audits(resumed)
        self.assertEqual(audit["old"], {"operations_sha": prior.data["operations_sha"]})
        self.assertEqual(audit["new"], {"operations_sha": new})
        self.assertNotEqual(audit["old"], audit["new"])
        self.assertEqual(audit["engine_commit"], "new-engine")
        data = ledger.snapshot(resumed.lease)["data"]
        self.assertEqual(data["operations_sha"], new)
        self.assertEqual(data["catalog_sha"], catalog_sha)

        def decide(job, turn, prompt, *args):
            self.assertIn(wait.description, prompt)
            self.assertIn(f'"catalog_sha": "{catalog_sha}"', prompt)
            response = {"version": PROTOCOL, "decision_id": turn, "rationale": "",
                        "state_version": ledger.snapshot(resumed.lease)["version"],
                        "calls": [{"op": "run.wait", "args": {"timeout_seconds": 0}}]}
            worker.receive.return_value = [{"ok": True, "structured": response}]
            return {}

        with (patch.object(delegation, "request", side_effect=decide),
              patch.object(delegation, "Worker") as launch):
            worker = launch.return_value
            worker.pid, worker.identity = 123, 456
            action, = resumed.decide()
        self.assertEqual(action["request"]["timeout_seconds"], 0)
        resumed.dispatch(action)
        record.move(resumed.run.id, "interrupted", ("running",))
        repeated = self.controller(resumed.run.id)
        self.assertIsNone(repeated.prepare())
        self.assertEqual(repeated.data["operations_sha"], new)
        self.assertEqual(self.audits(resumed), [audit])
        self.assertEqual(self.audits(repeated), [])

    def test_successive_refreshes_in_one_run_each_write_an_audit_line(self):
        wait = OPERATIONS["run.wait"]
        changed_wait = replace(wait, description="Deployed wait")
        prior = self.admit()
        record.move(prior.run.id, "failed", ("running",))
        resumed = self.controller(prior.run.id)
        original = {"operations_sha": prior.data["operations_sha"]}
        with patch.object(codebase, "STAMP", "deploy"):
            for deployed in (changed_wait, wait, changed_wait):
                with patch.dict(OPERATIONS, {"run.wait": deployed}):
                    self.assertIsNone(resumed.prepare())

        changed = {"operations_sha": ledger.digest({**describe_operations(), "run.wait": changed_wait.describe()})}
        self.assertNotEqual(changed, original)
        audits = self.audits(resumed)
        self.assertEqual([(line["old"], line["new"]) for line in audits],
                         [(original, changed), (changed, original), (original, changed)])
        self.assertEqual([line["engine_commit"] for line in audits], ["deploy"] * 3)
        self.assertEqual(ledger.snapshot(resumed.lease)["data"]["operations_sha"], changed["operations_sha"])

    def test_main_format_resumes_an_accepted_action_without_a_lineage_event(self):
        prior = self.admit()
        catalog_sha = ledger.digest(prior.catalog.entries)
        prior.data["catalog_sha"] = catalog_sha
        del prior.data["operations_sha"]
        ledger.save(prior.lease, prior.data)
        action_id = self.accept(prior, "run.finish", "accepted", {"reason": "noop", "evidence_refs": []})
        record.move(prior.run.id, "interrupted", ("running",))

        resumed = self.controller(prior.run.id)
        with patch.object(resumed, "decide", side_effect=AssertionError("the accepted action should finish the run")):
            self.assertEqual(resumed.drive()[0], "needs-info")
        action, = ledger.actions(resumed.lease)
        self.assertEqual((action["id"], action["state"]), (action_id, "succeeded"))
        data = ledger.snapshot(resumed.lease)["data"]
        self.assertEqual(data["catalog_sha"], catalog_sha)
        self.assertEqual(data["operations_sha"], ledger.digest(describe_operations()))
        self.assertEqual(self.audits(resumed), [])

    def test_catalog_audit_survives_interruption_after_append(self):
        prior = self.admit()
        old_entries = {key: entry for key, entry in prior.catalog.entries.items() if key != "prompt:delegate"}
        prior.data["catalog_sha"] = ledger.digest(old_entries)
        ledger.save(prior.lease, prior.data)
        record.move(prior.run.id, "interrupted", ("running",))
        resumed = self.controller(prior.run.id)
        write = run_lines.write

        def interrupt_after_write(run_id, event, **fields):
            line = write(run_id, event, **fields)
            if event == "lineage":
                raise OSError("interrupted after append")
            return line

        with patch.object(run_lines, "write", side_effect=interrupt_after_write):
            with self.assertRaisesRegex(OSError, "interrupted after append"):
                resumed.prepare()
        audit, = self.audits(resumed)
        pending = ledger.snapshot(resumed.lease)["data"]["lineage_event"]
        self.assertEqual(pending["run"], resumed.run.id)
        self.assertEqual(pending["event_id"], audit["event_id"])
        self.assertEqual(pending["old"], audit["old"])
        self.assertEqual(pending["new"], audit["new"])
        record.move(resumed.run.id, "interrupted", ("running",))
        repeated = self.controller(resumed.run.id)
        self.assertIsNone(repeated.prepare())
        self.assertEqual(self.audits(resumed), [audit])
        self.assertEqual(self.audits(repeated), [])
        self.assertNotIn("lineage_event", ledger.snapshot(repeated.lease)["data"])

    def test_lineage_row_missing_its_newline_recovers_in_either_append_order(self):
        fields = {"event_id": "refresh", "old": {"catalog_sha": "old"}, "new": {"catalog_sha": "new"},
                  "engine_commit": "deploy"}
        for error_first in (False, True):
            with self.subTest(error_first=error_first):
                run_id = f"torn-lineage-{error_first}"
                route = run_lines.write(run_id, "route")
                audit = {"ts": route["ts"], "run": run_id, "seq": route["seq"] + 1, "event": "lineage", **fields}
                with run_lines.path(run_id).open("ab") as log:
                    log.write(json.dumps(audit).encode())
                if error_first:
                    error = run_lines.write(run_id, "error", reason="interrupted append")
                self.assertEqual(run_lines.write(run_id, "lineage", **fields), audit)
                if not error_first:
                    self.assertEqual(run_lines.read(run_id), [route, audit])
                    error = run_lines.write(run_id, "error", reason="interrupted append")
                self.assertEqual(error["seq"], audit["seq"] + 1)
                self.assertEqual(run_lines.read(run_id), [route, audit, error])
                self.assertEqual(list(run_lines.follow(run_id, start=audit["seq"], ended=lambda: True)), [error])

    def test_explicit_rerun_uses_the_refreshed_catalog(self):
        checks = replace(OPERATIONS["checks.run"], constraints={"scope": {"enum": ["full"]}})
        for scope in ("quick", "full"):
            with self.subTest(scope=scope):
                prior = self.admit()
                action_id = self.accept(prior, "checks.run", "succeeded", {"scope": scope})
                record.move(prior.run.id, "failed", ("running",))
                resumed = self.controller(prior.run.id)
                resumed.run.action, resumed.run.action_only = action_id, True
                with (patch.dict(OPERATIONS, {"checks.run": checks}),
                      patch.object(resumed, "dispatch", wraps=resumed.dispatch) as dispatch,
                      patch.object(ledger, "accept", wraps=ledger.accept) as accept,
                      patch.object(resumed, "fingerprint", return_value="fixture-tree"),
                      patch.object(resumed, "atomic", return_value=factory.Outcome("pass")) as atomic):
                    if scope == "quick":
                        with self.assertRaisesRegex(factory.Refused, rf"{action_id}.*scope must be one of: full"):
                            resumed.drive()
                        accept.assert_not_called()
                        dispatch.assert_not_called()
                        atomic.assert_not_called()
                    else:
                        self.assertEqual(resumed.drive()[0], "step")
                        accept.assert_called_once()
                        dispatch.assert_called_once()
                        atomic.assert_called_once_with("checks", tree=str(resumed.job.ws), quick=False)
                self.assertEqual(ledger.pending(resumed.lease), [])
                self.assertEqual(len(self.audits(resumed)), 1)
                original, *reruns = ledger.actions(resumed.lease)
                self.assertEqual(original["id"], action_id)
                self.assertEqual(original["request"], {"scope": scope})
                self.assertEqual([(a["state"], a["request"]) for a in reruns],
                                 [("succeeded", {"scope": "full"})] if scope == "full" else [])

    def test_explicit_rerun_accepts_a_near_limit_worker_decision_after_normalization(self):
        prior = self.admit()
        route = "model:build:0"
        effort = prior.catalog.entries[route]["efforts"][0]
        sizes = iter((MAX_BYTES + 1, MAX_BYTES - 1))

        def decide(job, turn, prompt, *settings):
            request = {"goal": "", "route_id": route, "effort": effort}
            response = {"version": PROTOCOL, "decision_id": turn, "rationale": "",
                        "state_version": ledger.snapshot(prior.lease)["version"],
                        "calls": [{"op": "worker.start", "args": request}]}
            size = next(sizes)
            escaped, padding = divmod(size - len(json.dumps(response)), len(json.dumps("\u00e9")) - 2)
            request["goal"] = "\u00e9" * escaped + "x" * padding
            self.assertEqual(len(json.dumps(response).encode()), size)
            worker.receive.return_value = [{"ok": True, "structured": response}]
            return {}

        with (patch.object(delegation, "request", side_effect=decide),
              patch.object(delegation, "Worker") as launch):
            worker = launch.return_value
            worker.pid, worker.identity = 123, 456
            self.assertEqual(prior.decide(), [])
            self.assertEqual(ledger.actions(prior.lease), [])
            action, = prior.decide()
        self.assertEqual(action["request"]["prompt_id"], "delegate")
        ledger.transition(prior.lease, action["id"], "succeeded", {})
        record.move(prior.run.id, "failed", ("running",))

        resumed = self.controller(prior.run.id)
        resumed.run.action, resumed.run.action_only = action["id"], True
        with (patch.object(resumed, "worker_start", return_value={}) as start,
              patch.object(ledger, "accept", wraps=ledger.accept) as accept):
            self.assertEqual(resumed.drive()[0], "step")
        accept.assert_called_once()
        self.assertGreater(len(json.dumps(accept.call_args.args[3]).encode()), MAX_BYTES)
        original, rerun = ledger.actions(resumed.lease)
        self.assertEqual((rerun["state"], rerun["request"]), ("succeeded", original["request"]))
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0]["id"], rerun["id"])

    def test_refresh_refuses_with_live_workers_or_unfinished_actions(self):
        wait = OPERATIONS["run.wait"]
        cases = (
            ("pending", 123, "prior controller inference is still live"),
            ("running", 123, "worker .* is still live"),
            ("running", 321, "unfinished \\(running\\)"),
            ("accepted", None, "unfinished \\(accepted\\)"),
        )
        for state, pid, reason in cases:
            with self.subTest(state=state, pid=pid):
                prior = self.admit()
                if state == "pending":
                    turn = ledger.begin(prior.lease, {}, 10)
                    ledger.turn_started(prior.lease, turn, pid, 456)
                else:
                    self.accept(prior, "worker.start", state, pid=pid, identity=456)
                record.move(prior.run.id, "interrupted", ("running",))
                resumed = self.controller(prior.run.id)
                with (patch.dict(OPERATIONS, {"run.wait": replace(wait, description="Updated wait")}),
                      patch.object(delegation, "live", side_effect=lambda pid, identity: (pid, identity) == (123, 456))):
                    with self.assertRaisesRegex(factory.Refused, f"operations_sha changed .*{reason}"):
                        resumed.prepare()
                self.assertEqual(ledger.snapshot(resumed.lease)["data"]["operations_sha"], prior.data["operations_sha"])
                stored = ledger.pending(resumed.lease) if state == "pending" else ledger.actions(resumed.lease)
                self.assertEqual(stored[0]["state"], state)
                self.assertEqual(self.audits(resumed), [])

    def test_refresh_refuses_and_names_authority_changes(self):
        for key in ("policy_sha", "controller_settings", "run_settings"):
            with self.subTest(key=key):
                prior = self.admit()
                prior.data[key] = "old-authority"
                ledger.save(prior.lease, prior.data)
                record.move(prior.run.id, "failed", ("running",))
                resumed = self.controller(prior.run.id)
                with self.assertRaisesRegex(factory.Refused, f"{key} changed"):
                    resumed.prepare()
                self.assertEqual(ledger.snapshot(resumed.lease)["data"][key], "old-authority")
                self.assertEqual(self.audits(resumed), [])

    def test_prompt_refresh_on_owner_answer_and_pr_feedback(self):
        source = orchestration.prompt_loader.template

        def deployed_prompt(name):
            template = source(name)
            return Template(template.template + "\nFollow the deployed prompt.") if name == "orchestrate" else template

        for continuation in ("owner answer", "PR feedback"):
            with self.subTest(continuation=continuation):
                prior = self.admit()
                feedback = []
                if continuation == "owner answer":
                    question = {"header": "Scope", "question": "Which scope?", "kind": "text"}
                    ask_store.ask_add("ask-lineage", prior.run.id, self.repo.name, self.ticket, "orchestrate",
                                      "orchestrate", "", "", [question], {})
                    self.accept(prior, "owner.ask", "succeeded", ask_ids=["ask-lineage"], awaiting_owner=True)
                    record.move(prior.run.id, "needs-info", ("running",))
                    ask_store.ask_close("ask-lineage", "answered", [{"text": "Keep the existing scope"}])
                    context = "Keep the existing scope"
                else:
                    pr = "https://github.com/example/fixture/pull/1"
                    context = "Revise the status message"
                    feedback = [{"pr_url": pr, "kind": "comment", "gh_id": 1, "body": context}]
                    record.move(prior.run.id, "pr", ("running",), pr_url=pr)
                resumed = self.controller(prior.run.id, feedback)
                with (patch.object(orchestration.prompt_loader, "template", side_effect=deployed_prompt),
                      patch.object(gh, "pr_view", return_value={"state": "OPEN"}),
                      patch.object(gh, "pr_body", return_value="")):
                    self.assertIsNone(resumed.prepare())
                audit, = self.audits(resumed)
                self.assertEqual(audit["old"], {"controller_prompt": prior.data["controller_prompt"]})
                self.assertEqual(audit["new"], {"controller_prompt": resumed.data["controller_prompt"]})
                self.assertNotEqual(audit["old"], audit["new"])
                self.assertIn(context, resumed.job.goal)


class OrchestrationWaitTests(Isolated):
    def setUp(self):
        super().setUp()
        self.now = 0.0
        self.patches.enter_context(patch.object(orchestration, "time", SimpleNamespace(
            monotonic=lambda: self.now, sleep=self.advance)))
        controller = self.controller = orchestration.Controller.__new__(orchestration.Controller)
        controller.run = SimpleNamespace(id="wait-run", budget=None, launch_gate=None, launch_done=None,
                                         cost=0, paid=0)
        controller.lease = ledger.acquire(controller.run.id, "", {})
        controller.workers, controller.progress, controller.reported = {}, {}, {}
        controller.last_progress = float("-inf")
        controller.policy = config.Orchestration()
        controller.catalog = Catalog.__new__(Catalog)
        controller.catalog.context = {}
        controller.data = {"acceptance": "fixture"}
        controller.fingerprint = controller.policy_sha = lambda: "fixture"
        controller.persist = lambda: None

    def advance(self, seconds):
        self.now += seconds

    def accept(self, calls, maximum, *, validate=True):
        lease = self.controller.lease
        version = ledger.snapshot(lease)["version"]
        turn = ledger.begin(lease, {}, maximum)
        ledger.launched(lease, maximum)
        response = {"version": PROTOCOL, "decision_id": turn, "state_version": version,
                    "rationale": "", "calls": calls}
        if validate:
            response = self.controller.catalog.validate(
                response, turn, version, {row["id"]: row for row in ledger.actions(lease)})
        return ledger.accept(lease, turn, version, response, self.controller.policy.max_actions)

    def start_worker(self, deadline, *, finish=None, ok=True):
        controller = self.controller
        action, = self.accept([{"op": "worker.start", "args": {}}], 2, validate=False)
        worker = delegation.Worker.__new__(delegation.Worker)
        worker.action, worker.deadline, worker.writing = action["id"], deadline, False
        worker.run_id, worker.step = controller.run.id, f"worker-{worker.action}"
        worker.effective = {"prompt_id": "investigate"}
        worker.progress, worker.sources, worker.report = [], {}, (0, {})
        worker.proc = SimpleNamespace(poll=lambda: None)
        worker.close = lambda: None
        worker.receive = lambda: ([{"event": "result", "ok": ok}] if finish is not None and self.now >= finish else [])
        controller.workers[worker.action] = worker
        ledger.transition(controller.lease, worker.action, "running", {})
        return worker

    def wait(self, args, maximum=8, extra=()):
        actions = self.accept([{"op": "run.wait", "args": args}, *extra], maximum)
        for action in actions:
            self.controller.dispatch(action)
        return next(row for row in ledger.actions(self.controller.lease) if row["id"] == actions[0]["id"])

    def test_explicit_60_second_wait_keeps_cap_and_long_wait_reaches_worker_result(self):
        worker = self.start_worker(2700, finish=1800)
        capped = self.wait({"timeout_seconds": 60}, maximum=2)
        self.assertEqual((self.now, capped["result"]["active"]), (60, [worker.action]))
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 1)
        completed = self.wait({"timeout_seconds": 0}, maximum=2)
        self.assertLess(self.now - 1800, 0.1)
        self.assertEqual((completed["id"], completed["result"]["active"]), ("d0003-0", []))
        state = ledger.view(self.controller.run.id)
        self.assertEqual((state["decisions"], state["launches"]), (2, 3))
        self.assertEqual(state["actions"][0]["state"], "succeeded")

    def test_idle_waits_return_immediately_and_exhaust_decisions(self):
        for _ in range(2):
            self.assertEqual(self.wait({}, maximum=2)["result"]["active"], [])
        self.assertEqual(self.now, 0)
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_mixed_decision_keeps_charge_for_unchanged_wait(self):
        worker = self.start_worker(2700)
        steer = {"op": "worker.steer", "args": {"worker_id": worker.action, "message": "Check the log."}}
        settled = self.wait({}, maximum=2, extra=(steer,))
        self.assertEqual((self.now, settled["result"]["active"]), (60, [worker.action]))
        self.assertEqual(self.controller.data["steering"], {worker.action: ["Check the log."]})
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_wait_ends_at_worker_deadline_and_an_expired_wait_costs_a_decision(self):
        self.start_worker(10)
        self.now = 4
        self.wait({"timeout_seconds": 1800}, maximum=2)
        self.assertEqual(self.now, 10)
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 1)
        self.wait({"timeout_seconds": 0}, maximum=2)
        self.assertEqual(self.now, 10)
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_run_deadline_interrupts_long_wait(self):
        self.start_worker(2700)
        self.controller.run.budget = SimpleNamespace(run_deadline=lambda: 2, reason=lambda: "run deadline")
        with self.assertRaisesRegex(runlimits.Parked, "run deadline"):
            self.wait({"timeout_seconds": 0})
        self.assertEqual(self.now, 2)

    def test_new_progress_wakes_wait_and_worker_failure_wakes_next_wait(self):
        worker = self.start_worker(300, finish=5, ok=False)
        source = paths.record_dir(worker.run_id) / (worker.step + record.RECORD_SUFFIX)
        source.parent.mkdir(parents=True, exist_ok=True)
        with source.open("ab") as output:
            for event in ({"type": "replay"}, {"type": "heartbeat"},
                          {"type": "item.completed", "item": {"type": "command_execution"}},
                          {"type": "item.completed", "item": {"type": "agent_message", "text": "Checks are running."}}):
                record.append(output.fileno(), json.dumps(event))
        update = self.wait({"timeout_seconds": 0})
        self.assertIn("Checks are running.", update["result"]["progress"][worker.action]["excerpt"])
        self.assertEqual(self.now, 0)
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 2)
        failed = self.wait({"timeout_seconds": 60})
        self.assertEqual(failed["result"]["active"], [])
        self.assertLess(self.now - 5, 0.1)
        self.assertEqual(ledger.view(self.controller.run.id)["actions"][0]["state"], "failed")


class BudgetTests(Isolated):
    def test_measured_usage_rejects_corruption_and_zero_overrides_wall_time(self):
        run = self.made_run()
        record.note(run.id, started=util.iso(util.now() - timedelta(minutes=90)))
        record.settle(run.id, "interrupted")
        for field, read in (("active_seconds", runlimits.active_seconds),
                            ("verify_seconds", runlimits.verification_seconds),
                            ("verify_open_seconds", runlimits.verification_seconds),
                            ("repair_calls", lambda rid, row: runlimits.repair_count(rid))):
            for bad in (-1, "unknown"):
                with self.subTest(field=field, bad=bad), self.assertRaises(sqlite3.IntegrityError), \
                        foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (bad, run.id))
            for bad in ((None, float("nan")) if field == "repair_calls" else (None, float("nan"), float("inf"))):
                with foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (bad, run.id))
                with self.assertRaisesRegex(ValueError, run.id):
                    read(run.id, run_store.run_get(run.id))
            with foundation.tx() as conn:
                conn.execute(f"UPDATE runs SET {field}=0 WHERE id=?", (run.id,))
            self.assertEqual(read(run.id, run_store.run_get(run.id)), 0)

    def test_measured_recovery_and_nested_open_idle_waits(self):
        clock = [100.0]
        with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            run = self.made_run()
            clock[0] += 10
            for cause in ("limit", "slot"):
                with run.budget.idling(cause):
                    clock[0] += 20
                    with run.budget.idling("slot"):
                        clock[0] += 30
                        run.budget.checkpoint()
                        self.assertEqual(run.budget.run_seconds(), 10)
                    clock[0] += 10
                self.assertEqual(run.budget.run_seconds(), 10)
            self.assertEqual(run.budget.idled, 120)
            with run.budget.idling("ci"):
                clock[0] += 5
            self.assertEqual(run.budget.run_seconds(), 15)
            run.budget.persist()
            resumed = runlimits.Budget(run, run.budget.policy, "")
            self.assertEqual(resumed.run_seconds(), 15)
            clock[0] += 5
            self.assertEqual(resumed.run_seconds(), 20)

    def test_checkpoint_tracks_open_verification_without_charging_open_idle_time(self):
        clock = [100.0]
        with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            run = self.made_run()
            clock[0] += 10
            with patch.object(runlimits.signal, "setitimer"), run.budget.verifying(), run.budget.idling("limit"):
                clock[0] += 20
                run.budget.checkpoint()
                first = run_store.run_get(run.id)
                clock[0] += 30
                run.budget.checkpoint()
                second = run_store.run_get(run.id)
            self.assertEqual(first["active_seconds"], second["active_seconds"])
            self.assertEqual(second["verify_open_seconds"] - first["verify_open_seconds"], 30)
            saved = run_store.run_get(run.id)
            self.assertEqual(saved["verify_seconds"], 50)
            self.assertEqual(saved["verify_open_seconds"], 0)
            self.assertEqual(saved["active_seconds"], 10)

    def test_repair_budget_is_charged_before_launch_and_survives_continuation(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        invoked = []
        with patch.object(pipeline.runner, "run", side_effect=lambda *a, **k: invoked.append(True)), \
                self.assertRaises(runlimits.Parked):
            second.agent("repair", "repair again", self.repo.path, role="review_repair")
        self.assertEqual(invoked, [])
        self.assertEqual(runlimits.repair_count(second.id), 0)

    def test_repair_usage_is_durable_even_when_audit_event_cannot_be_written(self):
        first = self.made_run()
        with patch.object(run_lines, "write", return_value={}):
            first.budget.charge("review_fix1", "review_repair")
            first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
            second.budget.charge("repair", "review_repair")

    def test_cumulative_time_survives_continuation(self):
        first = self.made_run()
        with foundation.tx() as conn:
            conn.execute("UPDATE runs SET active_seconds=? WHERE id=?", (61 * 60, first.id))
        record.settle(first.id, "interrupted")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "cumulative 60"):
            second.budget.charge("plan", "plan")

    def test_interrupted_verification_phase_is_counted_on_restart(self):
        first = self.made_run()
        with foundation.tx() as conn:
            conn.execute("UPDATE runs SET verify_open_seconds=? WHERE id=?", (21 * 60, first.id))
        record.settle(first.id, "interrupted")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "verification"), second.budget.verifying():
            self.fail("unfinished verification phases must count")

    def test_unchanged_parked_input_refuses_retry_changed_spec_requires_explicit_retry(self):
        run = self.made_run()
        with patch.object(runlimits, "heads", return_value={"": "old"}):
            run.budget.park("repair limit")
            record.settle(run.id, "failed", branch="fixture")
            why, reset = runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True)
            self.assertIn("parked", why)
            self.assertFalse(reset)
            with foundation.db() as connection:
                set_task_body(self.ticket, "A narrower specification", connection=connection)
            self.assertTrue(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, False)[0])
            self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True), ("", True))

    def test_corrupt_budget_is_not_silently_reset(self):
        run = self.made_run()
        run.budget.charge("review_fix1", "review_repair")
        record.settle(run.id, "failed")
        for invalid in ("broken json", json.dumps({"version": 1, "parents": [], "left_out": "x"})):
            with self.subTest(invalid=invalid):
                (run.dir / "budget.json").write_text(invalid)
                with self.assertRaises(ValueError):
                    self.made_run(run.id)
                self.assertEqual(runlimits.repair_count(run.id), 1)

    def test_individual_step_reruns_cannot_reset_the_ticket_budget(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        second.budget.charge("review_fix2", "review_repair")
        record.note(second.id, only_step="implement")
        record.settle(second.id, "step")
        history = run_store.ticket_runs(self.repo.name, self.ticket)
        script = pipeline.Script("feature", lambda flow: None, continues=first.id)
        parent = pipeline.parent_run(script, run_store.run_get(first.id), history)
        self.assertEqual(parent, second.id)
        next_run = self.made_run(parent)
        self.assertEqual(set(next_run.budget.parents), {first.id, second.id})
        with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
            next_run.budget.charge("repair", "review_repair")

    def test_forced_run_leaves_out_repairs_spent_under_another_specification(self):
        for forced, edited, recorded in ((True, True, True), (True, False, True),
                                         (False, True, True), (True, True, False)):
            with self.subTest(forced=forced, edited=edited, recorded=recorded):
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Original", connection=connection)
                old = self.made_run()
                old.budget.charge("review_fix1", "review_repair")
                old.budget.charge("verify_fix1", "build")
                if not recorded:
                    old.budget.specification = None
                    old.budget.persist()
                record.settle(old.id, "pr")
                if edited:
                    with foundation.db() as connection:
                        set_task_body(self.ticket, "Edited", connection=connection)
                next_run = self.made_run(old.id, forced=forced)
                if forced and edited and recorded:
                    self.assertEqual((next_run.budget.parents, next_run.budget.left_out), ([], [old.id]))
                    next_run.budget.charge("review_fix", "review_repair")
                    self.assertEqual(runlimits.repair_count(next_run.id), 1)
                    record.settle(next_run.id, "failed")
                    retry = self.made_run(next_run.id, forced=True)
                    self.assertEqual(retry.budget.parents, [next_run.id])
                    self.assertNotIn(old.id, runlimits.family(retry.id))
                else:
                    self.assertEqual((next_run.budget.parents, next_run.budget.left_out), ([old.id], []))
                    with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
                        next_run.budget.charge("repair", "review_repair")

    def test_forced_run_leaves_out_run_and_verification_minutes_of_another_specification(self):
        for field, seconds in (("active_seconds", 61 * 60), ("verify_seconds", 21 * 60)):
            with self.subTest(field=field):
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Original", connection=connection)
                old = self.made_run()
                with foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (seconds, old.id))
                record.settle(old.id, "pr")
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Edited", connection=connection)
                fresh = self.made_run(old.id, forced=True).budget
                self.assertEqual((fresh.parents, fresh.left_out), ([], [old.id]))
                self.assertEqual((fresh.prior_seconds, fresh.verify_seconds), (0, 0))
                fresh.charge("plan", "plan")
                kept = self.made_run(old.id).budget
                with self.assertRaises(runlimits.Parked):
                    if field == "active_seconds":
                        kept.charge("plan", "plan")
                    else:
                        with kept.verifying():
                            self.fail("verification budget must not reset without an explicit retry")


class PollFixture(Isolated):
    def park_ticket(self, branch=""):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        run = self.made_run()
        run.budget.charge("review_fix1", "review_repair")
        record.note(run.id, branch=branch)
        run.budget.park("repair limit")
        record.settle(run.id, "failed")
        self.local.label(self.ticket, add=["failed"])
        self.cfg.replay = config.ReplayConfig(all=True)
        return run

    def run_polled_ticket(self, repo, tid, work, *, script=None):
        _, env = launch_lease.child_environment(())
        with patch.dict(os.environ, env), patch.object(launch_lease, "registered"), \
                patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                patch.object(pipeline, "_run", side_effect=work):
            return pipeline.run_issue(repo, tid, script=script)

    def poll_with(self, cfg, spawn):
        def launch_pending(repo, tid, launch):
            with launch_lease.acquiring(repo, tid):
                return launch()

        with patch.object(config, "load", return_value=cfg), patch.object(poll, "runnable", return_value=True), \
                patch.object(tasks, "spawn_pending", side_effect=launch_pending), \
                patch.object(poll, "spawn_run", side_effect=spawn), \
                patch.object(poll, "network_up_or_skip", return_value=True), \
                patch.object(poll, "settle_abandoned_runs"), patch.object(outcomes, "sync_outcomes"), \
                patch.object(outcomes, "sync_moves"), patch.object(outcomes, "sync_landings"), \
                patch.object(poll, "retry_scratch"), \
                patch.object(poll, "cleanup_worktrees"), patch.object(poll, "mark_loop"), \
                patch.object(pipeline, "SKIPPED_FILE", self.root / "skipped.json"):
            return poll.poll()


class ParkRetryFixture(PollFixture):
    def parked_findings(self, run, *blockers, extra_checks=()):
        run_lines.write(run.id, "step_end", step="review", ok=True)
        run.save("review.json", {"ok": True, "text": "Private reviewer transcript",
                                 "structured": {"problems": [{"severity": "blocker", **(
                                     {"problem": finding} if isinstance(finding, str) else finding)}
                                     for finding in blockers] + [
                                     {"problem": "Optional review note", "severity": "minor"}]}})
        pipeline.save_verification(run, "verify", verify.Verified("fail", "Private verify summary", [
            {"what": "The page displays Mara Doe owes 240 dollars",
             "passed": False, "how": "Private command", "saw": "Private raw page text"},
            {"what": "Environment startup failed", "passed": False, "cause": "environment", "how": "", "saw": ""},
            {"what": "Ordinary save succeeds", "passed": True, "how": "", "saw": ""},
            *({"how": "Synthetic check command", "saw": "Synthetic observation", **check}
              for check in extra_checks)]))

    def triage_parks(self, dry_run=False):
        with patch.object(triage, "META_FILE", self.root / "triage.json"), \
                patch.object(triage, "prune_records"), patch.object(triage, "mark_loop"), \
                patch.object(sandbox, "AUTHORITY", self.root / "verification-authority"), \
                patch.object(util, "github_token", return_value=""), patch.object(paths, "PI_KEYS_FILE", self.root / "keys"), \
                patch.object(park_retries.scrub.vault, "FOLDER", self.root / "vault"):
            return triage.refresh(self.cfg, dry_run)

    def age_park(self, run_id):
        park = runlimits.metadata(run_id, "parked.json")
        park["parked_at"] = util.iso(util.parse_iso(park["parked_at"]) - timedelta(seconds=1))
        util.write_json(runlimits.artifact(run_id, "parked.json"), park)

    def park_for_triage(self, run, reason="the repair chain used its 2 repair cycles", kept=None):
        run.budget.park(reason, kept)
        self.age_park(run.id)
        record.settle(run.id, "failed")

    def remote_run(self, source, issue):
        rid = record.start("pipeline", repo=self.repo.name, issue=issue)
        directory = paths.RUNS_DIR / rid
        directory.mkdir()
        run = pipeline.Run(rid, self.repo, directory, tracker=source, tid=issue, cfg=self.cfg)
        run.budget = runlimits.Budget(run, runlimits.Policy(), "", source.get(issue), False)
        return run

    def park_remote_issue(self, source, issue):
        run = self.remote_run(source, issue)
        self.park_for_triage(run)
        return run

    def purge_park(self, run_id):
        old = util.iso(util.now() - timedelta(days=120))
        writes.update("runs", run_id, {"started": old, "finished": old})
        with foundation.db() as connection:
            run_store.prune(connection)
        self.assertTrue(run_store.run_get(run_id)["files_purged"])
        self.assertIn(run_id, run_store.purge_inventory()[0])
        for artifact in (paths.RUNS_DIR / run_id).iterdir():
            if artifact.is_file():
                artifact.unlink()
        self.assertFalse(runlimits.artifact(run_id, "parked.json").exists())


class LocalParkRetryTests(ParkRetryFixture):
    def test_local_reply_readmits_after_run_retention(self):
        parked = self.park_ticket("fixture/parked")
        self.local.comment(self.ticket, f"[arete] Parked retry question for run {parked.id}.")
        self.local.label(self.ticket, add=["needs-info"])
        self.purge_park(parked.id)
        answer = "Keep drafts until save succeeds."
        self.assertEqual(task_reply(SimpleNamespace(id=self.ticket, text=[answer])), 0)
        started = []

        def work(cfg, run, slot):
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(pipeline.open_branch(run), "fixture/parked")
            self.assertIn(f"## Owner's answer to the parked retry question\n\n{answer}", run.task_ticket().comments)
            started.append(run.id)
            return "noop", "fixture", ""

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        self.assertFalse(runlimits.artifact(parked.id, "poll-readmissions.json").exists())

    def test_triage_rewrites_readmit_with_findings_in_the_next_plan_and_third_park_asks_once(self):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        workspace_dir = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
        workspace_dir.mkdir()
        checkout = workspace.Checkout("", "fixture/repo", self.repo.path, workspace_dir, "main")
        reasons = ("Saving the draft fails: preserve the draft after a declined save", "Restore the selected tab",
                   "The save button stays enabled while a save is in progress.")
        uncanned = "Keep attachments when editing a task title."
        original = ("Keep this body. cc @someone\n"
                    f"- [criterion:keep-attachments] {uncanned}\n"
                    f"- [criterion:save-button] {reasons[2]}\n")
        writes.update("tasks", self.ticket, {"body": original})
        park_reason = "the repair chain used its 2 repair cycles"
        first = self.made_run()
        self.parked_findings(first, reasons[0], "Show the failure beside the field",
                            {"problem": "Editing a task title drops its attachments.",
                             "criterion_ref": "criterion:keep-attachments"})
        branch = "retry/keep-this-branch"
        record.note(first.id, branch=branch)
        self.park_for_triage(first, park_reason)
        self.local.label(self.ticket, add=["failed"])
        self.assertEqual(self.triage_parks(dry_run=True), 0)
        self.assertEqual(self.local.get(self.ticket).body, original)
        prompts = []
        classic = pipeline._run

        def planner(run, step, prompt, *args, **kw):
            self.assertEqual(step, "plan")
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(pipeline.open_branch(run), branch)
            prompts.append((run.id, prompt))
            finding = reasons[len(prompts)]
            if len(prompts) == 2:
                finding = {"problem": "The form can send a second request mid-save.",
                           "criterion_ref": "criterion:save-button"}
            self.parked_findings(run, finding)
            raise runlimits.Parked(park_reason)

        append = park_retries.append

        def lost_append_reply(*args, **kw):
            self.assertTrue(append(*args, **kw))
            raise OSError("lost tracker reply")

        with patch.object(park_retries, "append", side_effect=lost_append_reply):
            self.assertEqual(self.triage_parks(), 1)
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        for attempt in range(2):
            self.assertEqual(self.triage_parks(), 0)
            ticket = self.local.get(self.ticket)
            self.assertEqual(ticket.body, original)
            self.assertEqual(len(ticket.retries), attempt + 1)
            self.assertEqual(ticket.retries[-1].number, attempt + 1)
            self.assertEqual(self.triage_parks(), 0)
            self.assertEqual(self.local.get(self.ticket).retries, ticket.retries)
            with patch.object(workspace, "make_workspace", return_value=(workspace_dir, [checkout])), \
                    patch.object(runlimits, "heads", return_value={}), \
                    patch.object(pipeline, "settle_starts"), patch.object(pipeline, "furnish", return_value=""), \
                    patch.object(pipeline.Run, "agent", autospec=True, side_effect=planner):
                self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, classic)), 0)
            self.assertEqual(len(prompts), attempt + 1)
            self.age_park(prompts[-1][0])
            self.assertIn("Current acceptance criteria that add to the ticket body", prompts[-1][1])
            self.assertIn(ticket.retries[-1].body, prompts[-1][1])
            self.assertIn(reasons[attempt], prompts[-1][1])
            if attempt == 0:
                self.assertIn("Show the failure beside the field", prompts[-1][1])
                self.assertIn(uncanned, ticket.retries[-1].body)
                self.assertIn("Editing a task title drops its attachments.", prompts[-1][1])
            self.assertIn("Private raw page text", prompts[-1][1])
            self.assertIn("Environment startup failed", prompts[-1][1])
        self.assertEqual(self.triage_parks(), 0)
        self.assertEqual(self.triage_parks(), 0)
        self.assertEqual(self.local.get(self.ticket).body, original)
        self.assertEqual(tasks_store.task_get(self.ticket)["state"], "needs-info")
        comments = tasks_store.task_comments(self.ticket)
        self.assertEqual(len(comments), 3)
        question = comments[-1]["body"]
        self.assertIn(reasons[2], question)
        self.assertIn(f'arete task reply {self.ticket} "your reply"', question)
        self.assertNotIn("Optional review note", question)
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("escalated ticket restarted")), 0)
        self.assertEqual(task_reply(SimpleNamespace(id=self.ticket, text=["Keep drafts until save succeeds."])), 0)
        started = []

        def answered(cfg, run, slot):
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(pipeline.open_branch(run), branch)
            self.assertIn("Keep drafts until save succeeds.", run.task_ticket().comments)
            self.assertIn("The form can send a second request mid-save.", run.task_ticket().comments)
            started.append(run.id)
            return "noop", "fixture", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, answered)), 0)
        self.assertEqual(len(started), 1)

    def test_triage_skips_missing_route_environment_and_held_or_kept_work(self):
        budget = "the repair chain used its 2 repair cycles"
        cases = [("missing route", {}, False, False), ("held scratch", {}, False, False),
                 (budget, {".": {"head": "kept"}}, False, False), (budget, {}, True, False),
                 (budget, {}, False, True),
                 ("controller used its 4 decision allowance", {".": {"head": "kept"}}, False, False),
                 ("run used its 3 action allowance", {}, True, False),
                 ("run used its 3 action allowance", {}, False, True),
                 ("controller used its 4 inference-launch allowance", {}, False, False)]
        for reason, kept, held, environment in cases:
            with self.subTest(reason=reason, kept=kept, held=held, environment=environment):
                self.ticket = tasks_store.task_add(self.repo.name, "Do the work", "Work")
                run = self.made_run()
                self.parked_findings(run, "Keep the draft on failed save")
                if environment:
                    record.note(run.id, failure=json.dumps({"class": "environment"}))
                ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
                if held:
                    run.workspace = (ws, [])
                self.park_for_triage(run, reason, kept)
                self.local.label(self.ticket, add=["failed"])
                with patch.object(workspace, "scratch_left", return_value=[ws / "scratch"] if held else []):
                    self.assertEqual(self.triage_parks(), 0)
                self.assertEqual(self.local.get(self.ticket).body, "Do the work")
                self.assertEqual(tasks_store.task_comments(self.ticket), [])

    def test_triage_retries_clean_controller_allowance_parks(self):
        self.local.label(self.ticket, add=["closed"])
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text(
            '[factory]\npath = "orchestrated"\n[orchestration]\nmax_decisions = 1\nmax_actions = 1\n')
        self.cfg.replay = config.ReplayConfig(all=True)

        for allowance in ("decision", "action"):
            with self.subTest(allowance=allowance):
                self.ticket = tasks_store.task_add(self.repo.name, "Fixture body", f"{allowance} limit")
                started = []

                def drive(flow):
                    controller = orchestration.Controller(flow)
                    started.append(controller)
                    if len(started) == 2:
                        self.assertIsNone(controller.prepare())
                        self.assertIn(retry.body, controller.catalog.context["task"])
                        self.assertEqual(controller.run.budget.parents, [])
                        self.assertEqual(controller.lease.root, flow.run_id)
                        self.assertEqual(ledger.snapshot(controller.lease)["turns"], 0)
                        return "noop", "fixture", ""

                    def exhaust():
                        lease = controller.lease
                        turn = ledger.begin(lease, {}, controller.policy.max_decisions)
                        if allowance == "decision":
                            ledger.reject(lease, turn, "Fixture decision consumed")
                            ledger.begin(lease, {}, controller.policy.max_decisions)
                        else:
                            call = {"op": "checks.run", "args": {}}
                            ledger.accept(lease, turn, ledger.snapshot(lease)["version"], {"calls": [call, call]},
                                          controller.policy.max_actions)
                        self.fail("the controller allowed more work")

                    with patch.object(controller, "decide", side_effect=exhaust):
                        return controller.drive()

                def spawn(repo, tid):
                    expected = 0 if started else 1
                    script = pipeline.Script("orchestrate", drive)
                    self.assertEqual(self.run_polled_ticket(repo, tid, None, script=script), expected)

                with patch.object(flows, "local_workspace", return_value=[]), \
                        patch.object(pipeline, "furnish", return_value=""), \
                        patch.object(runlimits, "heads", return_value={}), \
                        patch.object(pipeline, "workspace_note", return_value="Fixture workspace"):
                    self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                    first, = started
                    self.age_park(first.run.id)
                    row, park = runlimits.latest_park(self.repo.name, self.ticket)
                    self.assertEqual(row["id"], first.run.id)
                    self.assertTrue(park["reason"].endswith(f"1 {allowance} allowance"))
                    self.assertEqual(park["kept"], {})
                    self.assertEqual(self.triage_parks(), 0)
                    retry, = self.local.get(self.ticket).retries
                    self.assertEqual(retry.run_id, first.run.id)
                    self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                self.assertEqual(len(started), 2)

    def test_triage_readmits_a_clean_planning_budget_park_without_findings(self):
        self.repo.layer_dir.mkdir()
        layer = self.repo.layer_dir / "config.toml"
        layer.write_text('[factory]\npath = "classic"\n[git]\nbranch = "first/{id}-{slug}"\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
        ws.mkdir()
        checkout = workspace.Checkout("", "fixture/repo", self.repo.path, ws, "main")
        classic = pipeline._run
        branch = f"first/{self.ticket}-fixture"
        branches, runs = [], []

        def make(_run, _repo, _base, name, _ticket, _copy):
            branches.append(name)
            return ws, [checkout]

        def plan(run, step, *_args, **_kw):
            self.assertEqual(step, "plan")
            self.assertEqual(run.budget.parents, [])
            self.assertFalse(run_store.run_get(run.id)["branch"])
            runs.append(run)
            run.budget.deadline = 0
            run.budget.charge(step, step)

        def spawn(repo, tid):
            return self.run_polled_ticket(repo, tid, classic)

        with patch.object(workspace, "make_workspace", side_effect=make), \
                patch.object(pipeline, "settle_starts"), \
                patch.object(pipeline, "furnish", return_value=""), \
                patch.object(runlimits, "heads", return_value={"": ""}), \
                patch.object(pipeline.Run, "agent", autospec=True, side_effect=plan):
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            first, = runs
            row, park = runlimits.latest_park(self.repo.name, self.ticket)
            self.assertEqual((row["id"], park["branch"], park["kept"]), (first.id, branch, {}))
            self.age_park(first.id)
            self.assertEqual(park_retries.findings(first.id, {}), [])
            before = self.local.get(self.ticket)
            self.assertEqual(self.triage_parks(), 0)
            after = self.local.get(self.ticket)
            self.assertEqual(after.body, before.body)
            self.assertIn(f"## From parked run {first.id}", after.retries[-1].body)
            self.assertIn("The run reached its time budget. Replan the work", after.retries[-1].body)
            writes.update("tasks", self.ticket, {"title": "Renamed fixture"})
            layer.write_text(layer.read_text().replace("first/", "new/"))
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(branches, [branch, branch])
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(tasks_store.task_comments(self.ticket)), 1)

class GitHubParkRetryTests(ParkRetryFixture):
    def test_github_retries_and_escalation_use_approved_criteria_and_record_pointers(self):
        unsafe = ("Paste password hidden-literal", "Report ghp_synthetic12345678", "Read files from /tmp/private.txt",
                  r"Read C:\outside\private.txt", "Read ../private.txt", "<p>Private page contents</p>",
                  "First line\nPrivate log text", "Replace SensitiveFixtureValue", "Bearer hidden-literal",
                  "The page displays Mara Doe owes 240 dollars", "Mara Doe owes 240 dollars",
                  "Preserve the draft after a declined save for Mara Doe", "Renaming a task drops SensitiveFixtureValue",
                  "Editing a task title drops attachments from /var/run/private.txt",
                  "The button reads 'fixture account is overdue'.", "ERROR invoice status is unavailable",
                  "Your account will be suspended in ten days.")
        valid = "Keep editing a task title from dropping its attachments."
        cancel = "Keep closed dependencies closed when canceling a task."
        retain = "Preserve task attachments when saving a title."
        self.repo.tracker, self.repo.github = "github", "fixture/project"
        source = tracker.GitHubTracker(self.repo)
        issue = {"number": 17, "title": "Fixture", "state": "OPEN", "author": {"login": "fixture"}, "comments": [],
                 "labels": [{"name": "agent"}, {"name": "agent:failed"}],
                 "body": f"- [criterion:attachments] {valid}\n"
                         f"- [criterion:cancel] {cancel}\n"
                         "Check journey:save-title and journey:internal.\n"
                         "```markdown\n- [criterion:missing] Fixture example.\n```\n"
                         "<!--\n- [criterion:missing] Fixture hidden text.\n-->"}
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text(f'''
[verify.feature_map.save-title]
steps = ["Open the internal edit view"]
outcome = "Fixture internal field details"
tracker_criterion = "{retain}"
[verify.feature_map.internal]
steps = ["Open the internal edit view"]
outcome = "No approved tracker text"
''')
        with_evidence = {"problem": 'Editing a task title drops its attachments. The page read "Fixture account details" '
                                    "at /var/tmp/raw.html.", "criterion_ref": "criterion:attachments"}
        action = {"problem": "The page displays a private transaction", "category": "data_loss",
                  "retry_action": "avoid_partial_write", "file": "/tmp/private-review.py:8"}
        arbitrary = {"problem": "The reviewer supplied a private thought", "retry_action": "Private action text"}
        canceled = {"problem": "Canceling a task reopens closed dependencies.", "criterion_ref": "criterion:cancel"}
        checks = ({"what": "journey:save-title", "passed": False}, {"what": "journey:internal", "passed": False},
                  {"what": "Synthetic private check caption", "passed": False, "criterion_ref": "criterion:missing"})
        def post(_repo, _number, body, **_kw):
            issue["comments"].append({"body": body, "viewerDidAuthor": True, "createdAt": util.iso()})

        runs = []
        with patch.dict(os.environ, {"TEST_SECRET": "SensitiveFixtureValue"}), \
                patch.object(gh, "issue", return_value=issue), patch.object(gh, "issue_comment", side_effect=post), \
                patch.object(gh, "issue_labels") as labels:
            for _ in range(3):
                run = self.park_remote_issue(source, "17")
                runs.append(run.id)
                self.parked_findings(run, action, arbitrary, with_evidence, canceled, *unsafe, extra_checks=checks)
                self.assertIsNone(park_retries.retry_ticket(source, "17", ("SensitiveFixtureValue",)))
            labels.assert_called_once()
        comments = issue["comments"]
        self.assertEqual(len(comments), 3)
        for run_id, comment in zip(runs, comments):
            output = comment["body"]
            review = runlimits.artifact(run_id, "review.json").read_text().splitlines()
            at = next(n for n, line in enumerate(review, 1) if action["problem"] in line)
            self.assertIn(f"Review blocker (data_loss) — run `{run_id}`, `review.json:{at}`.", output)
            self.assertRegex(output, rf"Review blocker — run `{run_id}`, `review\.json:\d+`\.")
            self.assertIn(valid, output)
            self.assertIn(cancel, output)
            self.assertIn(retain, output)
            self.assertRegex(output, rf"Failed verification check — run `{run_id}`, `verify-outcome\.json:\d+`\.")
            for private in (*unsafe, "hidden-literal", "ghp_synthetic12345678", "private.txt", "SensitiveFixtureValue",
                            "Private page contents", "Private log text", "Private raw page text", "Private command",
                            "Private verify summary", "Private reviewer transcript", "Environment startup failed",
                            "Ordinary save succeeds", "Mara Doe", "Fixture account details", "raw.html",
                            "fixture account is overdue", "invoice status is unavailable",
                            with_evidence["problem"], "Editing a task title drops its attachments.",
                            canceled["problem"], "Fixture internal field details", "No approved tracker text",
                            "Synthetic private check caption", "criterion:missing", "Fixture example", "Fixture hidden text",
                            action["problem"], action["file"], *arbitrary.values()):
                self.assertNotIn(private, output)


    def test_github_retries_readmit_then_owner_reply_readmits_after_escalation(self):
        self.repo.tracker, self.repo.github = "github", "fixture/project"
        self.repo.parts = [config.Part("", self.repo.github)]
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        self.local.label(self.ticket, add=["closed"])
        source = tracker.GitHubTracker(self.repo)
        labels = [{"name": "agent"}, {"name": "agent:failed"}]
        original = "Owner specification. cc @someone\n"
        comments = []
        issue = {"number": 17, "title": "Fixture", "state": "OPEN", "labels": labels,
                 "author": {"login": "fixture"}, "body": original, "comments": comments}
        started = []
        label_attempts = []
        blocker = "Renaming a task drops the unread attachment labelled Private Fixture."
        error = "Synthetic verifier failed while navigating back to the editor."

        def command(args, *, input=None, write=False):
            if args[:2] == ["issue", "comment"]:
                self.assertTrue(write)
                self.assertIsInstance(input, gh.Outgoing)
                comments.append({"body": input, "author": {"login": "fixture-bot"}, "viewerDidAuthor": True,
                                 "createdAt": util.iso()})
            else:
                self.assertEqual(args, ["issue", "edit", "17", "--repo", self.repo.github,
                                        "--add-label", "agent:needs-info", "--remove-label", "agent:failed"])
                label_attempts.append(args)
                if len(label_attempts) == 1:
                    raise gh.GhError("temporary label failure")
                labels[:] = [{"name": "agent"}, {"name": "agent:needs-info"}]
            return "https://example.invalid/issue-comment\n"

        def work(cfg, run, slot):
            self.assertEqual(run.budget.parents, [])
            ticket = run.task_ticket()
            self.assertEqual(ticket.body, original)
            started.append(run.id)
            if len(started) == 3:
                self.assertIn("Retain the draft until the write commits.", ticket.comments)
                return "noop", "fixture", ""
            job = SimpleNamespace(run=run, cfg=cfg, layer=config.load_layer(self.repo.layer_dir),
                                  ticket=ticket, expertise="", attachments="")
            context = Catalog(job).context["task"]
            self.assertIn("Current acceptance criteria that add to the ticket body", context)
            self.assertIn(ticket.retries[-1].body, context)
            self.assertIn(blocker, context)
            output = ticket.retries[-1].body
            self.assertNotIn(blocker, output)
            self.assertNotIn(error, output)
            self.assertIn("Review blocker — run", output)
            if len(started) == 1:
                self.assertIn(error, context)
                self.assertNotIn("Private raw page text", context)
                self.assertRegex(output, r"`run\.jsonl:\d+`")
            else:
                self.assertIn("Private command", context)
                self.assertIn("Private raw page text", context)
                self.assertIn("Environment startup failed", context)
            self.parked_findings(run, blocker)
            if len(started) == 1:
                report = runlimits.metadata(run.id, "verify-outcome.json")
                run.save("verify.json", {"ok": True, "structured": report})
                run.save("verify-outcome.json", {"verdict": "fail", "checks": [
                    {k: v for k, v in check.items() if k not in ("how", "saw")} for check in report["checks"]]})
            raise runlimits.Parked("the repair chain used its 2 repair cycles")

        with patch.object(gh, "issue", return_value=issue), patch.object(gh, "gh", side_effect=command) as writes, \
                patch.object(gh, "issues", side_effect=lambda repo, label, **kw: [
                    issue] if {"name": label} in labels else []), \
                patch.object(pipeline.Run, "enter"), \
                patch.object(triage, "sources", return_value=[]):
            run = self.park_remote_issue(source, "17")
            self.parked_findings(run, blocker)
            run_lines.write(run.id, "step_end", step="verify2", ok=False, error=error)
            for attempt in (1, 2):
                if attempt == 1:
                    with patch.object(park_retries, "append", side_effect=OSError("stopped before post")):
                        self.assertEqual(self.triage_parks(), 1)
                self.assertEqual(self.triage_parks(), 0)
                self.assertEqual(self.triage_parks(), 0)
                self.assertTrue(comments[-1]["body"].startswith(f"<!-- arete:retry run={run.id} n={attempt} -->"))
                self.assertEqual(writes.call_count, attempt)
                comments.extend({"body": f"Owner context {n}", "author": issue["author"], "createdAt": util.iso()}
                                for n in range(tracker.MAX_COMMENTS + 1))
                self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
                self.assertEqual(len(started), attempt)
                run = SimpleNamespace(id=started[-1])
                self.age_park(run.id)
            self.assertEqual(self.triage_parks(), 1)
            self.assertEqual(self.triage_parks(), 0)
            self.assertEqual(self.triage_parks(), 0)
            self.assertEqual(writes.call_count, 5)
            self.assertEqual(sum("Parked retry question" in c["body"] for c in comments), 1)
            self.assertIn({"name": "agent:needs-info"}, labels)
            self.assertIn("Reply in a comment on this GitHub issue or remove the `agent:needs-info` label",
                          comments[-1]["body"])
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("started without a reply")), 0)
            comments.append({"body": "Retain the draft until the write commits.", "author": issue["author"],
                             "createdAt": util.iso(util.parse_iso(comments[-1]["createdAt"]) + timedelta(seconds=1))})
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
            self.assertEqual(len(started), 3)
            self.assertEqual(issue["body"], original)

    def test_github_escalation_requires_a_reply_or_label_removal(self):
        self.repo.tracker, self.repo.github = "github", "fixture/project"
        self.repo.parts = [config.Part("", self.repo.github)]
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        self.local.label(self.ticket, add=["closed"])
        source = tracker.GitHubTracker(self.repo)
        for number, field in enumerate(("title", "body", None), 17):
            with self.subTest(field=field):
                labels = [{"name": "agent"}, {"name": "agent:needs-info"}]
                issue = {"number": number, "title": "Original title", "body": "Original body", "state": "OPEN",
                         "labels": labels, "author": {"login": "fixture"}, "comments": []}
                started = []

                def work(cfg, run, slot):
                    current = run.task_ticket()
                    if field:
                        self.assertEqual(getattr(current, field), "Owner clarification")
                    self.assertEqual(run.budget.parents, [])
                    self.assertNotIn("agent:needs-info", current.labels)
                    started.append(run.id)
                    return "noop", "fixture", ""

                def spawn(repo, tid):
                    return self.run_polled_ticket(repo, tid, work)

                with patch.object(gh, "issue", return_value=issue), \
                        patch.object(gh, "issues", side_effect=lambda repo, label, **kw: [
                            issue] if {"name": label} in labels else []), \
                        patch.object(gh, "gh", side_effect=AssertionError("unexpected GitHub command")), \
                        patch.object(pipeline.Run, "enter"):
                    run = self.park_remote_issue(source, str(number))
                    issue["comments"].append({"body": f"[arete] Parked retry question for run {run.id}.",
                                              "viewerDidAuthor": True, "createdAt": util.iso()})
                    self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                    self.assertEqual(started, [])
                    if field:
                        issue[field] = "Owner clarification"
                    self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                    self.assertEqual(started, [])
                    labels.remove({"name": "agent:needs-info"})
                    self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                    self.assertEqual(len(started), 1)
                    self.assertEqual(len(issue["comments"]), 1)

    def github_batch(self):
        self.repo.tracker, self.repo.github = "github", "fixture/project"
        self.repo.parts = [config.Part("", self.repo.github)]
        self.cfg.replay = config.ReplayConfig(all=True)
        self.local.label(self.ticket, add=["closed"])
        return {"number": 17, "title": "Batch", "body": "Batch issues: #17, #18\nKeep tasks intact.",
                "state": "OPEN", "author": {"login": "fixture"}, "comments": [],
                "labels": [{"name": label} for label in ("agent", "agent:batch", "agent:failed")]}

    def test_batch_parks_without_a_retry(self):
        issue = self.github_batch()
        source = tracker.GitHubTracker(self.repo)
        with patch.object(gh, "issue", return_value=issue), patch.object(gh, "issue_comment") as post:
            self.park_remote_issue(source, "17")
            self.assertIsNone(park_retries.retry_ticket(source, "17", ()))
        post.assert_not_called()

    def test_holds_block_retry_and_reply_paths_even_after_selection(self):
        issue = self.github_batch()
        issue["labels"].remove({"name": "agent:batch"})
        source = tracker.GitHubTracker(self.repo)
        labels, comments, owner = (issue[key] for key in ("labels", "comments", "author"))

        def comment(_repo, _number, body, **_kw):
            comments.append({"body": body, "createdAt": util.iso(), "viewerDidAuthor": True})

        hold_labels = tracker.HOLD_LABELS | tracker.PARK_CONFLICTS
        def listed(_repo, label, **_kw):
            return [{**issue, "labels": [lb for lb in labels if lb["name"] not in hold_labels]}] if {
                "name": label} in labels else []

        with patch.object(gh, "issue", return_value=issue), patch.object(gh, "issues", side_effect=listed), \
                patch.object(gh, "issue_comment", side_effect=comment) as post:
            run = self.park_remote_issue(source, "17")
            self.assertIsNone(park_retries.retry_ticket(source, "17", ()))
            post.assert_called_once()
            _, proof = runlimits.poll_admission(self.repo, "17", source.get("17"), run.budget.policy)
            self.assertTrue(proof.feedback)
            for hold in sorted(hold_labels):
                with self.subTest(retry_hold=hold):
                    labels.append({"name": hold})
                    self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("held retry launched")), 0)
                    why, refused = runlimits.poll_admission(self.repo, "17", source.get("17"), run.budget.policy)
                    self.assertIn(hold, why)
                    self.assertIsNone(refused)
                    self.assertFalse(runlimits.admission(self.repo, "17", source, run.budget.policy, False,
                                                         readmission=proof)[1])
                    labels.pop()
            labels.append({"name": "agent:needs-info"})
            self.assertFalse(runlimits.admission(self.repo, "17", source, run.budget.policy, False,
                                                 readmission=proof)[1])
            labels.pop()

            labels.remove({"name": "agent:failed"})
            labels.append({"name": "agent:needs-info"})
            when = util.parse_iso(comments[-1]["createdAt"])
            comments.extend([{"body": f"[arete] Parked retry question for run {run.id}.", "viewerDidAuthor": True,
                              "createdAt": util.iso(when + timedelta(seconds=1))},
                             {"body": "Keep its dependencies closed.", "author": owner,
                              "createdAt": util.iso(when + timedelta(seconds=2))}])
            for hold in sorted(hold_labels):
                with self.subTest(reply_hold=hold):
                    labels.append({"name": hold})
                    self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("held reply launched")), 0)
                    self.assertEqual(source.reply_since("17", util.iso(when)), "")
                    labels.pop()
            started = []
            self.assertEqual(self.poll_with(self.cfg, lambda _repo, tid: started.append(tid)), 0)
            self.assertEqual(started, ["17"])

    def test_hold_at_child_admission_leaves_retry_and_reply_available_until_registration(self):
        self.repo.layer_dir.mkdir()
        layer = self.repo.layer_dir / "config.toml"
        classic = '[factory]\npath = "classic"\n'

        for number, trigger in enumerate(("retry", "reply", "policy and retry"), 17):
            with self.subTest(trigger=trigger):
                issue = self.github_batch()
                issue["number"] = number
                issue["labels"].remove({"name": "agent:batch"})
                labels, comments = issue["labels"], issue["comments"]
                tid = str(number)
                source = tracker.GitHubTracker(self.repo)
                layer.write_text(classic)
                started = []

                def listed(_repo, label, **_kw):
                    return [issue] if {"name": label} in labels else []

                def work(_cfg, run, _slot):
                    self.assertEqual(run.budget.parents, [])
                    self.assertTrue(run_store.run_get(run.id))
                    self.assertTrue(proof.attempted())
                    started.append(run.id)
                    return "noop", "fixture", ""

                hold = {"name": "no-agent"}

                def refuse(repo, ticket):
                    labels.append(hold)
                    self.assertEqual(self.run_polled_ticket(repo, ticket, lambda *_: self.fail("held child worked")), 2)

                def accept(repo, ticket):
                    self.assertEqual(self.run_polled_ticket(repo, ticket, work), 0)
                    self.assertEqual(self.run_polled_ticket(repo, ticket, lambda *_: self.fail("duplicate worked")), 2)

                with patch.object(gh, "issue", return_value=issue), patch.object(gh, "issues", side_effect=listed):
                    parked = self.park_remote_issue(source, tid)
                    if trigger == "reply":
                        labels[1] = {"name": "agent:needs-info"}
                        comments.extend([{"body": f"[arete] Parked retry question for run {parked.id}.",
                                          "viewerDidAuthor": True, "createdAt": util.iso()},
                                         {"body": "Keep the draft.", "author": issue["author"],
                                          "createdAt": util.iso(util.now() + timedelta(seconds=1))}])
                    else:
                        comments.append({"body": f"<!-- arete:retry run={parked.id} n=1 -->\n\nKeep the draft.",
                                         "viewerDidAuthor": True, "createdAt": util.iso()})
                    if trigger == "policy and retry":
                        layer.write_text(classic + "[limits]\nmax_repair_cycles = 3\n")
                    self.assertEqual(self.poll_with(self.cfg, refuse), 0)
                    self.assertEqual([row["id"] for row in run_store.ticket_runs(self.repo.name, tid)], [parked.id])
                    self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("hold was ignored")), 0)
                    labels.remove(hold)
                    policy = runlimits.Policy.of(config.load_layer(self.repo.layer_dir))
                    why, proof = runlimits.poll_admission(self.repo, tid, source.get(tid), policy)
                    self.assertEqual(why, "")
                    self.assertTrue(proof.feedback)
                    self.assertFalse(proof.attempted())
                    self.assertEqual(self.poll_with(self.cfg, accept), 0)
                    self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("trigger was used twice")), 0)
                    new, = started
                    self.assertEqual([row["id"] for row in run_store.ticket_runs(self.repo.name, tid)], [new, parked.id])

    def test_poll_revises_an_open_pr_for_a_closed_issue_and_still_blocks_a_closed_retry(self):
        revision = self.github_batch()
        revision["labels"] = [{"name": "agent"}, {"name": "agent:pr"}]
        retry = {**revision, "number": 18, "labels": [{"name": "agent"}, {"name": "agent:failed"}], "comments": []}
        source = tracker.GitHubTracker(self.repo)
        pr = "https://github.com/fixture/project/pull/42"
        prior = record.start("pipeline", repo=self.repo.name, issue="17")
        record.settle(prior, "pr", pr_url=pr, branch="fixture/revision")
        started = []
        classic = pipeline._run

        def review(gh_id):
            insights_store.add_feedback([{"pr_url": pr, "kind": "review", "gh_id": gh_id, "repo": self.repo.name,
                                         "issue": "17", "body": "Keep the draft on a failed save.", "at_once": 1,
                                         "created": util.iso(), "pr_state": "OPEN", "status": "new"}])

        def spawn(repo, tid):
            started.append(tid)
            self.run_polled_ticket(repo, tid, classic)

        with patch.object(gh, "issue", side_effect=lambda _repo, number: {17: revision, 18: retry}[number]), \
                patch.object(gh, "issues", side_effect=lambda _repo, label, **_kw: [
                    {**retry, "state": "OPEN"}] if {"name": label} in retry["labels"] else []), \
                patch.object(gh, "pr_view", return_value={"state": "OPEN"}) as pr_view, \
                patch.object(tracker.GitHubTracker, "prepare"), patch.object(pipeline.Run, "enter"), \
                patch.object(pipeline.Run, "say"), \
                patch.object(workspace, "make_workspace", side_effect=workspace.RunFailed("Fixture revision")) as make:
            parked = self.park_remote_issue(source, "18")
            retry["comments"].append({"body": f"<!-- arete:retry run={parked.id} n=1 -->\n\nKeep the draft.",
                                      "createdAt": util.iso(), "viewerDidAuthor": True})
            _, proof = runlimits.poll_admission(self.repo, "18", source.get("18"), parked.budget.policy)
            self.assertTrue(proof.feedback)
            retry["state"] = revision["state"] = "CLOSED"
            self.assertFalse(runlimits.admission(self.repo, "18", source, parked.budget.policy, False,
                                                 readmission=proof)[1])
            review(1)
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertEqual(started, ["17"])

            parked_revision = self.park_remote_issue(source, "17")
            record.note(parked_revision.id, pr_url=pr, branch="fixture/revision")
            revision["labels"][-1] = {"name": "agent:failed"}
            revision["title"] = "Owner updated the specification"
            review(2)
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(started, ["17", "17"])
        self.assertEqual([call.args for call in pr_view.call_args_list], [(pr,), (pr,)])
        self.assertEqual(make.call_count, 2)
        row = run_store.newest_whole_run(self.repo.name, "17")
        self.assertNotEqual(row["id"], parked_revision.id)
        self.assertEqual(row["summary"], "Fixture revision")
        self.assertEqual(run_store.newest_whole_run(self.repo.name, "18")["id"], parked.id)

    def test_continued_pr_revision_loads_the_retry_and_preserves_controller_state(self):
        self.check_pr_revision_retry("specification")

    def test_pr_revision_retry_starts_a_fresh_controller_when_shared_policy_changes(self):
        self.check_pr_revision_retry("policy")

    def test_pr_revision_retry_starts_a_fresh_controller_after_an_allowance_park(self):
        self.check_pr_revision_retry("allowance")

    def check_pr_revision_retry(self, trigger):
        fresh = trigger != "specification"
        repair_limit = 3 if trigger == "policy" else 2
        issue = self.github_batch()
        issue.update(title="Original title", body="Original body", labels=[{"name": "agent"}, {"name": "agent:failed"}])
        source = tracker.GitHubTracker(self.repo)
        self.repo.layer = self.root / "shared-layer"
        self.repo.layer.mkdir()
        config_file = self.repo.layer / "config.toml"
        configured = '[factory]\npath = "orchestrated"\n[limits]\nmax_repair_cycles = {}\n[orchestration]\nmax_decisions = 1\n'
        config_file.write_text(configured.format(2))
        pr = "https://github.com/fixture/project/pull/42"
        blocker = "A failed save clears the selected tab and removes the pending attachment."
        feedback = "Keep the checkbox selected across retries."
        insights_store.add_feedback([{"pr_url": pr, "kind": "review", "gh_id": 1, "repo": self.repo.name,
                                     "issue": "17", "body": feedback, "at_once": 1, "created": util.iso(),
                                     "pr_state": "OPEN", "status": "new"}])
        resumed = []

        def drive(flow):
            controller = orchestration.Controller(flow)
            self.assertIsNone(controller.prepare())
            context = controller.catalog.context["task"]
            self.assertIn("Current acceptance criteria that add to the ticket body", context)
            self.assertIn(retry, context)
            self.assertIn(blocker, context)
            self.assertIn("Private raw page text", context)
            self.assertIn(issue["body"], context)
            self.assertIn(issue["body"], controller.job.goal)
            self.assertIn(feedback, controller.job.goal)
            self.assertEqual(controller.job.branch, "fixture/revision")
            self.assertEqual(controller.run.budget.parents, [])
            self.assertEqual(controller.run.budget.prior_repairs + controller.run.budget.repair_calls, 0)
            self.assertEqual(controller.run.budget.policy.max_repair_cycles, repair_limit)
            self.assertNotEqual(controller.data["acceptance"], original_acceptance)
            snapshot = ledger.snapshot(controller.lease)
            saved = snapshot["data"]["prepared"]["ticket"]["comments"]
            self.assertIn(blocker, saved)
            if fresh:
                self.assertEqual(controller.lease.root, controller.run.id)
                self.assertEqual(snapshot["turns"], 0)
                self.assertEqual(controller.job.layer.max_repair_cycles, repair_limit)
                self.assertEqual(controller.data["policy_sha"], orchestration.policy_fingerprint(
                    self.cfg, config.load_layer(self.repo.layer), ()))
                self.assertEqual(ledger.find(prior.id)["data"]["policy_sha"], old_policy)
            else:
                self.assertNotIn("Original body", controller.job.goal)
                self.assertEqual(controller.job.plan_text, "Prior plan")
                self.assertEqual(controller.lease.root, prior.id)
                self.assertEqual(snapshot["turns"], 1)
            resumed.append(controller.run.id)
            if fresh:
                raise runlimits.Parked(controller.run.budget.reason())
            return "noop", "fixture", ""

        def spawn(repo, tid):
            command = pipeline.run_command(repo, tid)
            self.assertEqual(command[-2:], ["--continue", prior.id])
            script = pipeline.Script("orchestrate", drive, continues=command[-1])
            self.assertEqual(self.run_polled_ticket(repo, tid, None, script=script), 1 if fresh else 0)

        def make(_run, _repo, _base, branch, _tid, _copies):
            self.assertEqual(branch, "fixture/revision")
            return paths.WORKTREES_DIR / f"{self.repo.name}-17", []

        with patch.object(gh, "issue", return_value=issue), \
                patch.object(gh, "issues", side_effect=lambda _repo, label, **kw: [
                    issue] if {"name": label} in issue["labels"] else []), \
                patch.object(gh, "pr_view", return_value={"state": "OPEN"}), patch.object(pipeline.Run, "enter"), \
                patch.object(gh, "pr_body", return_value=""), \
                patch.object(gh, "gh", side_effect=AssertionError("unexpected GitHub command")), \
                patch.object(tracker.GitHubTracker, "prepare"), \
                patch.object(workspace, "make_workspace", side_effect=make), \
                patch.object(pipeline, "furnish", return_value=""), \
                patch.object(sandbox, "AUTHORITY", self.root / "verification-authority"), \
                patch.object(runlimits, "heads", return_value={}), \
                patch.object(pipeline, "workspace_note", return_value="Fixture workspace"):
            prior = self.remote_run(source, "17")
            record.note(prior.id, pr_url=pr, branch="fixture/revision")
            prior.path = "orchestrate"
            prior.feedback = insights_store.open_feedback([pr])
            controller = orchestration.Controller(flows.start(self.cfg, prior, {}))
            outcome = factory.Outcome("pass", data={
                "layer": str(self.repo.layer_dir), "checkouts": [], "ticket": asdict(source.get("17")),
                "branch": "fixture/revision", "add_dirs": [], "attachments": "", "expertise": "", "triage": "",
            })

            def prepare_step(*args, **kwargs):
                controller.job.take("prepare", outcome, stored=False)
                return outcome

            with patch.object(controller, "atomic", side_effect=prepare_step):
                self.assertIsNone(controller.prepare())
            controller.job.plan_text = "Prior plan"
            controller.persist()
            original_acceptance = controller.data["acceptance"]
            old_policy = controller.data["policy_sha"]
            turn = ledger.begin(controller.lease, {}, controller.policy.max_decisions)
            ledger.reject(controller.lease, turn, "Fixture spent decision")
            prior.budget.charge("review_fix1", "review_repair")
            insights_store.take_feedback(prior.feedback, prior.id)
            insights_store.settle_feedback(prior.id, "failed")
            self.parked_findings(prior, blocker)
            if trigger == "allowance":
                with self.assertRaises(ledger.Conflict) as exhausted:
                    ledger.begin(controller.lease, {}, controller.policy.max_decisions)
                self.park_for_triage(prior, str(exhausted.exception))
            else:
                self.park_for_triage(prior)
            retry = f"<!-- arete:retry run={prior.id} n=1 -->\n\nReview blocker — run `{prior.id}`, `review.json:1`."
            issue["comments"].append({"body": retry, "createdAt": util.iso(), "viewerDidAuthor": True})
            if trigger == "policy":
                config_file.write_text(configured.format(repair_limit))
            if trigger == "specification":
                issue["body"] = "Updated body"
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(resumed), 1)
        if fresh:
            row, park = runlimits.latest_park(self.repo.name, "17")
            self.assertEqual((row["id"], row["pr_url"], park["pr_url"]), (resumed[0], pr, pr))
            pending, = pipeline.pending_feedback(self.repo.name, "17")
            self.assertEqual((pending["body"], pending["status"]), (feedback, "failed"))

    def test_older_park_question_does_not_block_an_answered_pr_question(self):
        issue = self.github_batch()
        issue["labels"] = [{"name": "agent"}, {"name": "agent:needs-info"}]
        source = tracker.GitHubTracker(self.repo)
        pr = "https://github.com/fixture/project/pull/42"
        answer = "Keep the checkbox selected on save."
        started = []

        def drive(flow):
            self.assertEqual(flow.continues, revision.id)
            due, = flow.ctx.run.answers_due
            self.assertEqual(due["answers"], [{"header": "Save", "kind": "text", "text": answer}])
            started.append(flow.run_id)
            return "noop", "fixture", ""

        def spawn(repo, tid):
            self.assertEqual(pipeline.run_command(repo, tid)[-2:], ["--continue", revision.id])
            script = pipeline.Script("orchestrate", drive, continues=revision.id)
            self.assertEqual(self.run_polled_ticket(repo, tid, None, script=script), 0)

        with patch.object(gh, "issue", return_value=issue), \
                patch.object(gh, "issues", side_effect=lambda _repo, label, **kw: [
                    issue] if {"name": label} in issue["labels"] else []):
            parked = self.park_remote_issue(source, "17")
            past = util.now() - timedelta(minutes=1)
            issue["comments"].extend([
                {"body": f"[arete] Parked retry question for run {parked.id}.", "viewerDidAuthor": True,
                 "createdAt": util.iso(past)},
                {"body": "Keep failed drafts.", "author": issue["author"],
                 "createdAt": util.iso(past + timedelta(seconds=1))},
            ])
            revision = self.remote_run(source, "17")
            run_lines.write(revision.id, "execution", path="orchestrate")
            ask_store.ask_add("revision-question", revision.id, self.repo.name, "17", "orchestrate_ask", "orchestrate",
                              "", "", [{"header": "Save", "question": "What should remain selected?", "kind": "text"}], {})
            record.settle(revision.id, "needs-info", pr_url=pr)
            issue["comments"].append({"body": answer, "author": issue["author"], "createdAt": util.iso()})
            selected = []
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: selected.append(tid)), 0)
            self.assertEqual(ask_store.ask_get("revision-question")["status"], "answered")
            self.assertEqual(selected, ["17"])
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("stale park reply started a run")), 0)
        self.assertEqual(len(started), 1)

    def test_authenticated_github_reply_survives_retention_and_can_only_start_once(self):
        issue = self.github_batch()
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        owner, stranger = issue["author"], {"login": "someone-else"}
        issue.update(body="Original body", labels=[{"name": "agent"}, {"name": "agent:needs-info"}])
        source = tracker.GitHubTracker(self.repo)
        answer = "Keep the draft on failed save."
        started = []

        def work(cfg, run, slot):
            self.assertEqual(run.budget.parents, [])
            self.assertIn(f"## Owner's answer to the parked retry question\n\n{answer}", run.task_ticket().comments)
            started.append(run.id)
            return "noop", "fixture", ""

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

        with patch.object(gh, "issue", return_value=issue), \
                patch.object(gh, "issues", side_effect=lambda _repo, label, **kw: [
                    issue] if {"name": label} in issue["labels"] else []), patch.object(pipeline.Run, "enter"), \
                patch.object(gh, "gh", side_effect=AssertionError("unexpected GitHub write")):
            parked = self.park_remote_issue(source, "17")
            issue["comments"].append({"body": f"[arete] Parked retry question for run {parked.id}.",
                                      "viewerDidAuthor": True, "createdAt": util.iso()})
            self.purge_park(parked.id)
            issue["body"] = "Edited specification"
            issue["comments"].append({"body": "Try again", "author": stranger, "createdAt": util.iso()})
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("started without an owner reply")), 0)
            issue["comments"].append({"body": answer, "author": owner, "createdAt": util.iso()})
            hold = {"name": "no-agent"}

            def add_hold(repo, tid):
                issue["labels"].append(hold)
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 2)

            self.assertEqual(self.poll_with(self.cfg, add_hold), 0)
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("started while held")), 0)
            self.assertEqual(run_store.newest_whole_run(self.repo.name, "17")["id"], parked.id)
            issue["labels"].remove(hold)
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("replayed the reply")), 0)
        self.assertEqual(len(started), 1)
        self.assertFalse(runlimits.artifact(parked.id, "poll-readmissions.json").exists())

    def test_only_authenticated_arete_questions_invalidate_an_old_owner_reply(self):
        issue = self.github_batch()
        issue["labels"].remove({"name": "agent:batch"})
        issue["labels"].remove({"name": "agent:failed"})
        issue["labels"].append({"name": "agent:needs-info"})
        source = tracker.GitHubTracker(self.repo)

        def comment(body, login, offset, **fields):
            issue["comments"].append({"body": body, "author": {"login": login},
                                      "createdAt": util.iso(when + timedelta(seconds=offset)), **fields})

        with patch.object(gh, "issue", return_value=issue), patch.object(gh, "issues", side_effect=lambda _repo, label,
                          **_kw: [issue] if {"name": label} in issue["labels"] else []), \
                patch.object(gh, "me", return_value="fixture-app[bot]"):
            first = self.park_remote_issue(source, "17")
            second = self.park_remote_issue(source, "17")
            cutoff = runlimits.metadata(second.id, "parked.json")["parked_at"]
            when = util.parse_iso(cutoff)
            older = f"[arete] Parked retry question for run {first.id}."
            newest = f"[arete] Parked retry question for run {second.id}."
            comment(older, "fixture-app[bot]", 1, viewerDidAuthor=True)
            comment("Old answer.", "fixture", 2)
            comment(newest, "stranger[bot]", 3)
            comment(newest, "fixture", 4)
            self.assertEqual(source.get("17").park_question.run_id, first.id)
            self.assertEqual([t.id for t in source.replied()], ["17"])
            comment(newest, "Fixture-App[bot]", 5)
            self.assertEqual(source.get("17").park_question.run_id, second.id)
            self.assertEqual(source.replied(), [])
            self.assertEqual(source.reply_since("17", cutoff), "")
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("old answer launched")), 0)
            comment("New answer.", "fixture", 6)
            started = []
            self.assertEqual(source.reply_since("17", cutoff), "New answer.")
            self.assertEqual(self.poll_with(self.cfg, lambda _repo, tid: started.append(tid)), 0)
            self.assertEqual(started, ["17"])

    def test_parked_retry_ignores_jira(self):
        self.repo.tracker, self.repo.jira_project = "jira", "JRA"
        source = tracker.JiraTracker(self.repo, self.cfg.jira)
        client = jira.Client("https://example.invalid", "fixture", "fixture")
        body = "Owner specification. cc @someone\n"
        account = {"accountId": "owner"}
        fields = {"summary": "Fixture", "description": body, "creator": account, "assignee": account,
                  "labels": ["agent", source.route, "agent-failed"]}
        replies = {"myself": account, "changelog": {"values": []}, "comment": {"comments": []},
                   "JRA-17": {"fields": fields}}

        def request(method, path, params=None, body=None):
            self.assertEqual(method, "GET")
            self.assertIsNone(body)
            return replies[path.rsplit("/", 1)[-1]]

        with patch.object(tracker.JiraTracker, "client", client), \
                patch.object(client, "request", side_effect=request) as requests, \
                patch.object(triage, "sources", return_value=[]):
            self.park_remote_issue(source, "JRA-17")
            requests.reset_mock()
            self.assertEqual(self.triage_parks(), 0)
            requests.assert_not_called()
            self.assertEqual(fields["description"], body)


class JiraParkRetryTests(ParkRetryFixture):
    def test_done_jira_ticket_readmits_pr_feedback_after_spec_or_policy_change(self):
        self.repo.tracker, self.repo.jira_project, self.repo.github = "jira", "FIXTURE", "fixture/project"
        self.repo.parts = [config.Part("", self.repo.github)]
        self.local.label(self.ticket, add=["closed"])
        self.cfg.replay = config.ReplayConfig(all=True)
        self.repo.layer_dir.mkdir()
        layer_file = self.repo.layer_dir / "config.toml"
        classic = '[factory]\npath = "classic"\n'
        account = {"accountId": "owner"}
        client = SimpleNamespace(me=lambda: account, changelog=lambda _: [], comments=lambda _: [],
                                 search=lambda *args, **kw: [], browse_url=lambda tid: f"https://jira.invalid/{tid}",
                                 issue=lambda *_: issue)
        source = tracker.JiraTracker(self.repo, self.cfg.jira)

        def spawn(repo, tid):
            self.run_polled_ticket(repo, tid, pipeline._run)

        with patch.object(tracker.JiraTracker, "client", client), patch.object(pipeline.Run, "enter"), \
                patch.object(pipeline.Run, "say"), patch.object(runlimits, "heads", return_value={}), \
                patch.object(gh, "pr_view") as read_pr, \
                patch.object(workspace, "make_workspace", side_effect=workspace.RunFailed("Fixture revision")) as make:
            for n, (change, state) in enumerate((("spec", "OPEN"), ("policy", "OPEN"), ("spec", "CLOSED")), 1):
                with self.subTest(change=change, pr_state=state):
                    key, pr = f"FIXTURE-{n}", f"https://github.com/fixture/project/pull/{n}"
                    fields = {"summary": "Original specification", "description": "Keep the draft.",
                              "status": {"statusCategory": {"key": "done"}}, "assignee": account, "creator": account,
                              "labels": ["agent", source.route]}
                    issue = {"key": key, "fields": fields}
                    layer_file.write_text(classic)
                    parked = self.park_remote_issue(source, key)
                    record.note(parked.id, pr_url=pr, branch="fixture/revision")
                    if change == "spec":
                        fields["summary"] = "Updated specification"
                    else:
                        layer_file.write_text(classic + "[limits]\nmax_repair_cycles = 3\n")
                    insights_store.add_feedback([{"pr_url": pr, "kind": "review", "gh_id": n, "repo": self.repo.name,
                                                 "issue": key, "body": "Restore the pending draft.", "at_once": 1,
                                                 "created": util.iso(), "pr_state": "OPEN", "status": "new"}])
                    read_pr.return_value = {"state": state}
                    self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                    read_pr.assert_called_with(pr)
                    row = run_store.newest_whole_run(self.repo.name, key)
                    self.assertNotEqual(row["id"], parked.id)
                    self.assertEqual(row["summary"], "Fixture revision" if state == "OPEN" else
                                     f"the PR closed before arete could revise it: {pr}")
                    insights_store.close_feedback(pr, "CLOSED")
        self.assertEqual(read_pr.call_count, 3)
        self.assertEqual(make.call_count, 2)


class ParkRecoveryTests(ParkRetryFixture):
    def test_triage_conditional_append_preserves_concurrent_edit(self):
        run = self.made_run()
        self.park_for_triage(run, run.budget.reason())
        self.local.label(self.ticket, add=["failed"])
        append = tasks_store.task_append_comment
        edited = "Owner changed the specification. cc @someone\n"

        def concurrent_edit(*args, **kw):
            set_task_body(self.ticket, edited)
            return append(*args, **kw)

        with patch.object(tasks_store, "task_append_comment", side_effect=concurrent_edit) as attempted:
            self.assertEqual(self.triage_parks(), 0)
            self.assertEqual(self.triage_parks(), 0)
        attempted.assert_called_once()
        self.assertEqual(self.local.get(self.ticket).body, edited)
        self.assertEqual(tasks_store.task_comments(self.ticket), [])

    def test_triage_drops_uncommitted_append_after_owner_edit_and_next_run(self):
        run = self.made_run()
        self.park_for_triage(run, run.budget.reason())
        self.local.label(self.ticket, add=["failed"])
        journal = park_retries.journal_for(self.local, self.ticket)
        with patch.object(tasks_store, "task_append_comment", side_effect=OSError("lost reply")) as append:
            self.assertEqual(self.triage_parks(), 1)
            self.assertEqual(json.loads(journal.read_text())["rewrites"], {run.id: "pending"})
            set_task_body(self.ticket, "New owner specification")
            following = self.made_run()
            record.settle(following.id, "done")
            self.assertEqual(self.triage_parks(), 0)
        append.assert_called_once()
        self.assertEqual(json.loads(journal.read_text())["rewrites"], {})
        self.assertEqual(self.local.get(self.ticket).body, "New owner specification")
        self.assertEqual(tasks_store.task_comments(self.ticket), [])

    def test_github_rechecks_closure_and_owner_specification_before_posting(self):
        self.repo.tracker, self.repo.github = "github", "fixture/project"
        source = tracker.GitHubTracker(self.repo)
        for number, change in enumerate(({"state": "CLOSED"}, {"title": "Owner title"},
                                         {"body": "Owner description"}), 17):
            with self.subTest(change=change):
                tid = str(number)
                issue = {"number": number, "title": "Fixture", "state": "OPEN", "body": "Fixture body",
                         "author": {"login": "fixture"}, "labels": [{"name": "agent"}, {"name": "agent:failed"}]}
                with patch.object(gh, "issue", return_value=issue):
                    self.park_remote_issue(source, tid)
                with patch.object(gh, "issue", side_effect=[issue, {**issue, **change}]) as read, \
                        patch.object(gh, "gh") as command:
                    self.assertIsNone(park_retries.retry_ticket(source, tid, ()))
                self.assertEqual(read.call_count, 2)
                command.assert_not_called()
                journal = park_retries.journal_for(source, tid)
                self.assertEqual(json.loads(journal.read_text())["rewrites"], {})

    def test_retry_requires_the_newest_comment_to_be_newer_than_this_park(self):
        run = self.made_run()
        self.park_for_triage(run)
        self.local.label(self.ticket, add=["failed"])
        current = f"<!-- arete:retry run={run.id} n=1 -->\n\nFixture acceptance criteria"
        other = current.replace(run.id, "pip-20240102-030405-aaaa-older")

        def proof():
            _, admission = runlimits.poll_admission(self.repo, self.ticket, self.local.get(self.ticket),
                                                    run.budget.policy)
            return admission

        cutoff = runlimits.metadata(run.id, "parked.json")["parked_at"]
        with patch.object(util, "iso", return_value=cutoff):
            tasks_store.task_comment(self.ticket, "arete", current)
        self.assertIsNone(proof())
        tasks_store.task_comment(self.ticket, "arete", current)
        retry = proof()
        self.assertTrue(retry.feedback)
        tasks_store.task_comment(self.ticket, "arete", other)
        self.assertIsNone(proof())
        self.assertFalse(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, False,
                                             readmission=retry)[1])
        tasks_store.task_comment(self.ticket, "arete", current)
        writes.update("tasks", self.ticket, {"title": "Owner edited title"})
        self.assertEqual(proof().feedback, "")
        _, edited_with_policy = runlimits.poll_admission(
            self.repo, self.ticket, self.local.get(self.ticket), runlimits.Policy(max_repair_cycles=3))
        self.assertEqual(edited_with_policy.feedback, "")
        self.cfg.replay = config.ReplayConfig(all=True)
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        started = []

        def work(cfg, next_run, slot):
            self.assertIn(current, next_run.task_ticket().comments)
            started.append(next_run.id)
            return "noop", "fixture", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
        self.assertEqual(len(started), 1)

    def test_branch_readmission_includes_retry_context(self):
        with patch.object(runlimits, "heads", return_value={"": ""}):
            run = self.park_ticket(branch="fixture/retry")
        self.age_park(run.id)
        comment = f"<!-- arete:retry run={run.id} n=1 -->\n\nFixture acceptance criteria"
        tasks_store.task_comment(self.ticket, "arete", comment)
        _, retry = runlimits.poll_admission(self.repo, self.ticket, self.local.get(self.ticket), run.budget.policy)
        retry.attempted(record=True)
        observed = []

        def work(cfg, next_run, slot):
            self.assertIn(comment, next_run.task_ticket().comments)
            self.assertEqual(pipeline.open_branch(next_run), "fixture/retry")
            self.assertEqual(next_run.budget.parents, [])
            observed.append(next_run.id)
            return "noop", "fixture", ""

        with patch.object(runlimits, "effective_origins", return_value={"": "fixture-origin"}), \
                patch.object(runlimits, "heads", return_value={"": "a" * 40}):
            _, proof = runlimits.poll_admission(self.repo, self.ticket, self.local.get(self.ticket), run.budget.policy)
            self.assertEqual(proof.branch, "fixture/retry")
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
        self.assertEqual(len(observed), 1)

    def test_a_standalone_park_refuses_another_step_without_posting_retry_feedback(self):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        whole = self.made_run()
        run_lines.write(whole.id, "execution", path="feature")
        run_lines.write(whole.id, "step_start", step="review")
        record.settle(whole.id, "failed")
        step = self.made_run(whole.id)
        record.note(step.id, only_step="implement")
        self.park_for_triage(step)
        script = pipeline.Script("feature", lambda _: self.fail("started a parked step"),
                                 continues=whole.id, only="review")
        self.assertEqual(self.triage_parks(), 0)
        self.assertEqual(tasks_store.task_comments(self.ticket), [])
        why, proof = runlimits.poll_admission(self.repo, self.ticket, self.local.get(self.ticket), step.budget.policy)
        self.assertIn(f"parked after run {step.id}", why)
        self.assertIsNone(proof)
        self.assertIn(f"parked after run {step.id}", runlimits.admission(
            self.repo, self.ticket, self.local, step.budget.policy, False)[0])
        with patch.object(config, "load", return_value=self.cfg), \
                patch.object(tasks, "command_of", return_value=["fixture"]), \
                patch.object(control, "launch") as launch, \
                patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                patch.object(pipeline.Run, "agent") as agent:
            with self.assertRaisesRegex(control.Refused, f"parked after run {step.id}"):
                tasks.run_step(whole.id, "review", {})
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, force=True, script=script), 2)
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, force=True), 2)
        launch.assert_not_called()
        agent.assert_not_called()

    def test_newer_standalone_step_without_a_park_does_not_restore_an_older_park(self):
        whole = self.made_run()
        self.park_for_triage(whole)
        step = self.made_run()
        record.note(step.id, only_step="review")
        record.settle(step.id, "done")
        self.assertEqual(runlimits.latest_park(self.repo.name, self.ticket)[0]["id"], step.id)
        self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, step.budget.policy, False), ("", False))
        self.assertEqual(self.triage_parks(), 0)
        self.assertEqual(tasks_store.task_comments(self.ticket), [])

    def test_parked_retry_attributes_verification_to_the_inner_attempt_in_classic_and_adw(self):
        @contextlib.contextmanager
        def serving(_settings, _cwd, log, *args, **kw):
            if log.name == "verify2-server.log":
                raw = "Synthetic startup output from /var/tmp/private.log"
                log.write_text(raw)
                raise verify.ServerError(raw)
            yield ""

        for mode in ("classic", "adw"):
            for startup_failed in (False, True):
                with self.subTest(mode=mode, startup_failed=startup_failed):
                    self.ticket = tasks_store.task_add(self.repo.name, "Fixture body", "Fixture")
                    run = self.made_run()
                    prior_repairs = 1 if startup_failed else 2
                    for n in range(prior_repairs):
                        run.budget.charge(f"review_fix{n}", "review_repair")
                    layer = config.RepoLayer(self.repo.layer_dir, verify=config.Verify(url=""))
                    build = pipeline.Build(cfg=self.cfg, run=run, ticket=self.local.get(self.ticket),
                                           ws=self.repo.path, checkouts=[], layer=layer, timeout=60,
                                           workspace="fixture workspace", tools=[], deny=[])
                    job = SimpleNamespace(staged=lambda: build, with_left=lambda goal: goal,
                                          goal="Check save retry", build_summary="", run=run, labels=lambda *_: {})
                    result = Result(ok=True, structured={
                        "verdict": "fail", "summary": "Fixture verify summary",
                        "checks": [{"what": "Retry preserves the draft", "passed": False,
                                    "how": "Synthetic command", "saw": "Synthetic page contents"}],
                    })
                    with patch.object(run, "settings", return_value=SimpleNamespace(tries=3)), \
                            patch.object(run, "agent", return_value=result) as agent, \
                            patch.object(scratch, "make", return_value=None), \
                            patch.object(verify, "serving", side_effect=serving), \
                            patch.object(build, "resume", side_effect=lambda step, _, role: run.budget.charge(step, role)), \
                            patch.object(build, "recheck"), self.assertRaises(runlimits.Parked) as parked:
                        if mode == "classic":
                            pipeline.verify_in_use(build, job.goal, job.build_summary)
                        else:
                            factory.Flow(run.id, job).run("verify", (str(self.repo.path),), {})
                    agent.assert_called_once()
                    if mode == "adw":
                        self.assertTrue(any(line.get("event") == "step_end" and line.get("step") == "verify"
                                            and line.get("result") == "stop" for line in run_lines.read(run.id)))
                    self.assertTrue((run.dir / "verify-outcome.json").exists())
                    self.assertFalse((run.dir / "verify2-outcome.json").exists())
                    self.park_for_triage(run, str(parked.exception))
                    self.local.label(self.ticket, add=["failed"])
                    self.assertEqual(self.triage_parks(), 0)
                    body = self.local.get(self.ticket).retries[-1].body
                    context = run.task_ticket().comments
                    self.assertEqual("Verification startup failure" in body, startup_failed)
                    self.assertEqual("Failed verification check" in body, not startup_failed)
                    self.assertEqual("Synthetic page contents" in context, not startup_failed)
                    self.assertEqual("Synthetic startup output" in context, startup_failed)
                    for private in ("Fixture verify summary", "Synthetic command", "Synthetic page contents",
                                    "Synthetic startup output", "/var/tmp/private.log"):
                        self.assertNotIn(private, body)

    def test_retry_uses_latest_verify_step_end_even_without_saved_outcome(self):
        for newest in ({"step": "verify2", "ok": False},
                       {"step": "verify", "try": 2, "ok": False},
                       {"step": "verify", "try": 2, "result": "fail", "ok": False}, None):
            with self.subTest(newest=newest):
                self.ticket = tasks_store.task_add(self.repo.name, "Fixture body", "Fixture")
                run = self.made_run()
                self.parked_findings(run, "Preserve the draft when saving fails")
                if newest is None:
                    (run.dir / "verify2-server.log").write_text("Raw failure from /tmp/private.log")
                else:
                    run_lines.write(run.id, "step_end", **newest, error="Raw failure from /tmp/private.log")
                last_line = len(run_lines.path(run.id).read_text().splitlines())
                self.park_for_triage(run)
                self.local.label(self.ticket, add=["failed"])
                self.assertEqual(self.triage_parks(), 0)
                body = self.local.get(self.ticket).retries[-1].body
                context = run.task_ticket().comments
                self.assertIn("Verification failure", body)
                self.assertIn("Preserve the draft when saving fails", context)
                self.assertNotIn("Private raw page text", context)
                self.assertNotIn("/tmp/private.log", body)
                if newest:
                    self.assertIn("Raw failure from /tmp/private.log", context)
                    self.assertIn(f"`run.jsonl:{last_line}`", body)

    def test_verifier_worker_failure_keeps_its_evidence_when_the_app_log_is_empty(self):
        for mode in ("classic", "adw"):
            with self.subTest(mode=mode):
                self.ticket = tasks_store.task_add(self.repo.name, "Fixture body", "Fixture")
                self.check_worker_failure_evidence(mode)

    def check_worker_failure_evidence(self, mode):
        self.cfg.replay = config.ReplayConfig(all=True)
        run = self.made_run()
        self.parked_findings(run, "Keep the draft on a failed save")
        layer = config.RepoLayer(self.repo.layer_dir, verify=config.Verify(url=""))
        run.layer = layer
        build = pipeline.Build(cfg=self.cfg, run=run, ticket=self.local.get(self.ticket), ws=self.repo.path,
                               checkouts=[], layer=layer, timeout=60, workspace="Fixture", tools=[], deny=[])
        build.steps["verify"] = 1
        short_error = "Fixture verifier worker lost its browser session."
        full_error = (short_error + "\n" + "Synthetic worker diagnostics. " * 20
                      + "The worker was navigating back to the editor.")
        job = SimpleNamespace(staged=lambda: build, with_left=lambda goal: goal, layer=layer,
                              goal="Check the save flow", build_summary="Fixture", run=run, labels=lambda *_: {},
                              policy=lambda *_: {})

        @contextlib.contextmanager
        def serving(_settings, _cwd, log, *_args, **_kw):
            log.touch()
            yield "http://localhost:8123"

        with patch.object(verify, "serving", side_effect=serving), \
                patch.object(scratch, "make", return_value=None), \
                patch.object(pipeline.browser, "mcp_config", return_value=""), \
                patch.object(pipeline.runner, "run", return_value=Result(ok=False, error=full_error)):
            if mode == "adw":
                outcome = factory.Flow(run.id, job).run("verify", (str(self.repo.path),), {})
                result = verify.Verified.stored(outcome.data["verified"])
            else:
                result = pipeline.check_in_use(build, job.goal, job.build_summary)
        self.assertEqual((result.verdict, result.blocked_by), ("blocked", short_error))
        self.assertTrue((run.dir / "verify2-server.log").is_file())
        self.assertFalse((run.dir / "verify2-outcome.json").exists())
        verification_end, = [number for number, line in park_retries.numbered_log(run.id)
                             if line["event"] == "verification_end" and line["step"] == "verify2"]
        self.park_for_triage(run)
        self.local.label(self.ticket, add=["failed"])
        self.assertEqual(self.triage_parks(), 0)
        pointer = f"Verification blocked — run `{run.id}`, `run.jsonl:{verification_end}`."
        comment = self.local.get(self.ticket).retries[-1].body
        context = run.task_ticket().comments
        self.assertIn(pointer, comment)
        self.assertNotIn(short_error, comment)
        self.assertNotIn("navigating back to the editor", comment)
        self.assertIn(full_error, context)
        self.assertNotIn("Private raw page text", context)

class QueueReadmissionTests(PollFixture):
    def test_specification_edit_readmits_once_and_reparks_on_current_inputs(self):
        parked = self.park_ticket()
        with foundation.db() as connection:
            set_task_body(self.ticket, "A narrower specification", connection=connection)
        started = []

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            raise runlimits.Parked("repair limit again")

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 1)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        self.assertNotEqual(started[0], parked.id)
        self.assertEqual(runlimits.latest_park(self.repo.name, self.ticket)[0]["id"], started[0])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        self.assertIn(f"parked after run {started[0]}: repair limit again", output.getvalue())
        self.assertIn(runlimits.RETRY, output.getvalue())

    def test_raised_layer_budget_readmits_a_failed_ticket(self):
        self.park_ticket()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        started = []

        def work(cfg, run, slot):
            started.append(run.budget.policy.max_repair_cycles)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            return "noop", "fixture finished", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
        self.assertEqual(started, [3])

    def test_branch_readmission_checks_origin_once_per_poll_and_fails_closed(self):
        branch = "arete/fixture"
        command = ["git", "ls-remote", "origin", f"refs/heads/{branch}"]
        old, new = "a" * 40, "b" * 40
        remote = SimpleNamespace(returncode=0, stdout=f"{old}\trefs/heads/{branch}\n")
        calls, started = [], []
        sh = util.sh

        def read(cmd, *args, **kwargs):
            if cmd == ["git", "remote", "get-url", "origin"]:
                return SimpleNamespace(returncode=0, stdout="/tmp/fixture.git\n")
            if cmd == command:
                calls.append(cmd)
                return remote
            return sh(cmd, *args, **kwargs)

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            return "noop", "fixture finished", ""

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

        with patch.object(util, "sh", side_effect=read):
            parked = self.park_ticket(branch)
            unchanged = f"{old}\trefs/heads/{branch}\n{new}\trefs/heads/nested/refs/heads/{branch}\n"
            moved = f"{new}\trefs/heads/{branch}\n"
            cases = ((unchanged, 0, False), (moved, 1, False), ("truncated\n", 0, False), (moved, 0, True))
            for stdout, returncode, should_start in cases:
                with self.subTest(returncode=returncode, stdout=stdout):
                    calls.clear()
                    remote.returncode = returncode
                    remote.stdout = stdout
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                    self.assertEqual(calls, [command])
                    self.assertEqual(len(started), int(should_start))
                    if not should_start:
                        self.assertIn(f"parked after run {parked.id}: repair limit", output.getvalue())
                        self.assertIn(runlimits.RETRY, output.getvalue())
                    if returncode:
                        self.assertIn("could not confirm the parked branch head", output.getvalue())

    def test_child_refuses_a_specification_or_policy_reverted_after_poll(self):
        parked = self.park_ticket()
        original = self.local.get(self.ticket).body
        layer = self.repo.layer_dir / "config.toml"
        original_layer = layer.read_text()

        def set_body(body):
            with foundation.db() as connection:
                set_task_body(self.ticket, body, connection=connection)

        for change, revert in (
                (lambda: set_body("Changed specification"), lambda: set_body(original)),
                (lambda: layer.write_text(original_layer + "[limits]\nmax_repair_cycles = 3\n"),
                 lambda: layer.write_text(original_layer))):
            change()

            def spawn(repo, tid):
                revert()
                self.assertEqual(self.run_polled_ticket(repo, tid, lambda *_: self.fail("stale launch")), 2)

            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertIn(f"parked after run {parked.id}", output.getvalue())
            self.assertEqual([row["id"] for row in run_store.ticket_runs(self.repo.name, self.ticket)], [parked.id])

    def test_poll_does_not_repeat_a_launch_that_exits_before_registration(self):
        self.park_ticket()
        starts = []

        def spawn(repo, tid):
            starts.append(tid)

        for body in ("First edit", "Second edit"):
            with foundation.db() as connection:
                set_task_body(self.ticket, body, connection=connection)
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertIn("Poll already tried these inputs.", output.getvalue())
            self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, runlimits.Policy(), True),
                             ("", True))
        self.assertEqual(starts, [self.ticket, self.ticket])

    def test_multi_origin_readmission_checks_complete_exact_refs_and_revalidates_origins(self):
        branch = "arete/fixture"
        ref = f"refs/heads/{branch}"
        client, server = "a" * 40, "b" * 40
        self.repo.tracker = "jira"
        self.repo.parts = [config.Part("server", "Acme/server"), config.Part("client", "Acme/client")]
        urls = {"client": "https://github.com/acme/client.git", "server": "git@github.com:acme/server.git"}
        for name in urls:
            (self.repo.path / name).mkdir()
        lookups, calls, started = [], [], []
        sh = util.sh

        def repository(project, oid, name=branch):
            ref_row = {"prefix": "refs/heads/", "name": name, "target": {"oid": oid}} if oid else None
            return {"nameWithOwner": project, "ref": ref_row}

        old_client = repository("Acme/client", client)
        old_server = repository("Acme/server", server)
        deleted = repository("Acme/client", "")
        complete = {"part0": deleted, "part1": old_server}
        reply = {}

        def read(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "ls-remote"]:
                name = kwargs["cwd"].name
                lookups.append(name)
                oid = client if name == "client" else server
                return SimpleNamespace(returncode=0, stdout=f"{oid}\t{ref}\n")
            if cmd == ["git", "remote", "get-url", "origin"]:
                return SimpleNamespace(returncode=0, stdout=urls[kwargs["cwd"].name] + "\n")
            if cmd[:3] == ["gh", "api", "graphql"]:
                self.assertEqual(cmd[3:], ["--hostname", "github.com", "--input", "-"])
                calls.append(json.loads(kwargs["input"]))
                return SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr="")
            return sh(cmd, *args, **kwargs)

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertEqual(run.budget.parents, [])
            return "noop", "fixture finished", ""

        with patch.object(util, "sh", side_effect=read), patch.object(tracker, "for_repo", return_value=self.local):
            parked = self.park_ticket(branch)
            self.assertCountEqual(lookups, ["client", "server"])
            lookups.clear()
            for response in ({"data": {"part0": old_client, "part1": old_server}},
                             {"data": {"part0": deleted}},
                             {"data": {"part0": deleted, "part1": {"nameWithOwner": "Acme/server"}}},
                             {"data": complete, "errors": [{"message": "partial failure"}]},
                             {"data": {**complete, "part1": repository("Acme/another", server)}},
                             {"data": {**complete, "part1": repository("Acme/server", server, branch + "-other")}}):
                with self.subTest(response=response):
                    reply = response
                    calls.clear()
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("unexpected launch")), 0)
                    self.assertIn(f"parked after run {parked.id}", output.getvalue())
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0]["variables"], {"ref": ref})
                    self.assertIn('name: "client"', calls[0]["query"])
                    self.assertIn('name: "server"', calls[0]["query"])

            reply = {"data": complete}
            urls["server"] = "https://github.com.example/acme/server.git"
            calls.clear()
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("unverified origin")), 0)
            self.assertEqual(calls, [])

            urls["server"] = "git@github.com:acme/server.git"

            def stale_origin(repo, tid):
                urls["server"] = "https://github.com/acme/wrong.git"
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 2)

            self.assertEqual(self.poll_with(self.cfg, stale_origin), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(started, [])

            urls["server"] = "git@github.com:acme/server.git"
            reply = {"data": {**complete, "part1": repository("Acme/server", "c" * 40)}}
            calls.clear()

            def spawn(repo, tid):
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(lookups, [])
            self.assertEqual(len(started), 1)

    def test_jira_parked_candidate_can_follow_a_page_of_ordinary_failures(self):
        self.repo.jira_project = "FIXTURE"
        source = tracker.JiraTracker(self.repo, self.cfg.jira)
        client = jira.Client("https://example.invalid", "unused", "unused")
        ordinary = [{"key": f"FIXTURE-{i}"} for i in range(1, 21)]
        parked = {"key": "FIXTURE-21", "fields": {"summary": "Parked ticket"}}
        with patch.dict(tracker._clients, {"": client}), patch.object(client, "get", side_effect=[
            {"issues": [], "isLast": True},
            {"issues": ordinary, "isLast": False, "nextPageToken": "next"},
            {"issues": [parked], "isLast": True},
        ]) as read:
            self.assertEqual([ticket.id for ticket in source.todo(parked=lambda key: key == parked["key"])],
                             [parked["key"]])
        self.assertEqual(read.call_args.kwargs["nextPageToken"], "next")

class QueueTests(PollFixture):
    def test_interrupted_ticket_rechecks_dependencies_before_starting(self):
        interrupted = self.made_run()
        record.settle(interrupted.id, "interrupted")
        prerequisite = tasks_store.task_add(self.repo.name, "Prerequisite", "Prerequisite", "feat")
        with foundation.db() as connection:
            set_task_body(self.ticket, f"Waits on {prerequisite}", connection=connection)
        self.cfg.replay = config.ReplayConfig(all=True)
        started = []
        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertNotIn(self.ticket, started)
        self.assertIn(prerequisite, started)

    def test_one_poll_fills_capacity_round_robin_without_relying_on_child_rows(self):
        second = config.Repo(name="other", path=self.root / "other", tracker="local", pipeline=True)
        second.path.mkdir()
        for repo in (self.repo, second):
            for i in range(4):
                tasks_store.task_add(repo.name, f"Task {i}", f"Task {i}", "feat")
        cfg = config.Config(repos=[self.repo, second], max_parallel=3, max_parallel_per_repo=2,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append((repo, tid))), 0)
        self.assertEqual(len(started), 3)
        self.assertEqual([repo for repo, _ in started[:2]], [self.repo.name, second.name])
        self.assertEqual(len(set(started)), 3)
        self.assertLessEqual(sum(repo == self.repo.name for repo, _ in started), 2)

    def test_dependency_and_parked_ticket_do_not_block_other_ready_work(self):
        prerequisite = tasks_store.task_add(self.repo.name, "Prerequisite", "Prerequisite", "feat")
        blocked = tasks_store.task_add(self.repo.name, f"Waits on {prerequisite}", "Blocked", "feat")
        parked = self.made_run()
        parked.budget.park("repair limit")
        record.settle(parked.id, "failed")
        failed = tasks_store.task_add(self.repo.name, "Ordinary failure", "Ordinary failure", "feat")
        self.local.label(failed, add=["failed"])
        extra = tasks_store.task_add(self.repo.name, "Ready", "Ready", "feat")
        cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertIn(prerequisite, started)
        self.assertIn(extra, started)
        self.assertNotIn(blocked, started)
        self.assertNotIn(failed, started)
        self.assertNotIn(self.ticket, started)

    def test_route_readiness_is_rechecked_after_every_start(self):
        for i in range(3):
            tasks_store.task_add(self.repo.name, str(i), str(i), "feat")
        ready, started = [True], []
        def routes(*args, **kw):
            return [SimpleNamespace(state="ready" if ready[0] else "held", detail="fixture quota hold", until="")]
        def start(repo, tid):
            started.append(tid)
            ready[0] = False
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=routes)):
            self.assertEqual(self.poll_with(self.cfg, start), 0)
        self.assertEqual(len(started), 1)


class EnvironmentRequeueTests(Isolated):
    class_timeout = 20

    def setUp(self):
        super().setUp()
        tasks_store.task_set_state(self.ticket, "closed")
        self.repo = config.Repo(name="github-fixture", path=self.repo.path, github="owner/project",
                                parts=[config.Part("", "owner/project")], pipeline=True)
        self.cfg = config.Config(repos=[self.repo], replay=config.ReplayConfig(all=True))
        self.issue = "658"
        self.labels = {"agent"}
        self.comments = []
        self.started = []
        self.time = datetime(2026, 11, 21, 8, tzinfo=UTC)
        self.ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.issue}"
        self.authority = self.root / "authority"
        for module, name, func in (
            (config, "load", lambda: self.cfg),
            (codebase, "STAMP", "old-engine"),
            (util, "now", lambda: self.time),
            (gh, "issue", self.issue_view),
            (gh, "issues", self.issue_list),
            (gh, "issue_labels", self.edit_labels),
            (gh, "issue_comment", self.post_comment),
            (gh, "find_issue_comment_marker", self.find_comment),
            (gh, "ensure_label", lambda *args: ""),
            (gh, "default_branch", lambda repo: "main"),
            (poll, "network_up_or_skip", lambda *args: True),
            (poll, "mark_loop", lambda *args: None),
            (poll, "cleanup_worktrees", lambda **kwargs: None),
            (outcomes, "sync_outcomes", lambda cfg: None),
            (outcomes, "sync_moves", lambda cfg: None),
            (outcomes, "sync_landings", lambda cfg: None),
            (scratch, "process_ids", dict),
            (scratch, "service_labels", dict),
            (sandbox, "AUTHORITY", self.authority),
            (pipeline, "SKIPPED_FILE", self.root / "skipped.json"),
            (pipeline, "wanted", lambda repo: "classic"),
            (tasks, "spawn_pending", lambda repo, tid, launch: self.started.append(tid)),
        ):
            self.patches.enter_context(patch.object(module, name, func))
        with scratch.authority_fd(create=True) as fd:
            scratch.create_secret(fd, scratch.SECRET)
            self.key = scratch.read_secret(fd)

    def issue_view(self, repo, number):
        return {"number": number, "title": "Fixture issue", "body": "Fixture description", "state": "OPEN",
                "author": {"login": "owner"}, "labels": [{"name": label} for label in sorted(self.labels)]}

    def issue_list(self, repo, label, **kwargs):
        return [self.issue_view(repo, int(self.issue))] if label in self.labels else []

    def edit_labels(self, repo, number, add=(), remove=(), **kwargs):
        self.labels.difference_update(remove)
        self.labels.update(add)

    def post_comment(self, repo, number, body, **kwargs):
        self.comments.append(body)

    def find_comment(self, repo, number, marker):
        return next(("fixture" for body in self.comments if marker in body.splitlines()), "")

    def run_failure(self, failure=None, script=None):
        with patch.object(pipeline, "_run", side_effect=failure, wraps=pipeline._run):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, script=script), 1)
        self.assertIn("agent:failed", self.labels)
        return run_store.newest_whole_run(self.repo.name, self.issue)

    def test_authenticated_scratch_settlement_requeues_before_picking(self):
        selector = "a" * 64
        data = {"version": 2, "state": "retained", "source_mutations": False, "diagnostic": {},
                "attempt": {"selector": selector, "workspace": str(self.ws),
                            "root": str(self.ws / (paths.SCRATCH_PREFIX + "fixture"))}}
        with scratch.authority_fd(create=True) as fd:
            scratch.store(fd, self.key, data)
        with patch.object(scratch, "recover_pending", return_value=[]):
            row = self.run_failure()
            self.assertEqual(json.loads(row["failure"])["selectors"], [selector])
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)
        record_path = self.authority / (selector + ".json")
        record_path.unlink()
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)

        def settle():
            with scratch.authority_fd() as fd:
                scratch.store(fd, self.key, {**data, "state": "done"})
                scratch.move_settled(fd, selector + ".json")
            return [data["attempt"]["root"]]

        with patch.object(scratch, "recover_pending", side_effect=settle):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])
        self.assertNotIn("agent:failed", self.labels)
        self.assertIn("verification scratch settled", self.comments[-1])

    def test_pruned_scratch_requires_an_authenticated_receipt_when_tracker_returns(self):
        self.cfg.retention = config.Retention(days=0)
        selector = "b" * 64
        name = selector + ".json"
        data = {"version": 2, "state": "retained", "source_mutations": False, "diagnostic": {},
                "attempt": {"selector": selector, "workspace": str(self.ws),
                            "root": str(self.ws / (paths.SCRATCH_PREFIX + "fixture"))}}
        with scratch.authority_fd(create=True) as fd:
            scratch.store(fd, self.key, data)
        with patch.object(scratch, "recover_pending", return_value=[]):
            self.run_failure()
            with scratch.authority_fd() as fd:
                scratch.store(fd, self.key, {**data, "state": "done"})
                scratch.move_settled(fd, name)
            settled = self.authority / scratch.SETTLED / name
            os.utime(settled, (1, 1))
            with patch.object(sandbox, "can_confine", return_value=True), \
                    patch.object(poll, "network_up_or_skip", return_value=False):
                self.assertEqual(poll.poll(), 0)
                self.assertEqual(scratch.rotate(), (1, 0))
        self.assertFalse(settled.exists())
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])
        receipt = self.authority / scratch.RECEIPTS / name
        original = receipt.read_bytes()
        receipt.write_text(json.dumps({"data": {**data, "state": "done"}, "signature": "forged"}))
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])
        receipt.write_bytes(original)
        root = Path(data["attempt"]["root"])
        root.mkdir(parents=True)
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        root.rmdir()
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_disappearance_of_an_unidentifiable_scratch_record_does_not_requeue(self):
        unreadable = self.authority / "unidentifiable.json"
        unreadable.write_text("invalid scratch record")
        with patch.object(scratch, "recover_pending", return_value=[]):
            row = self.run_failure()
            unreadable.unlink()
            self.assertEqual(poll.poll(), 0)
        self.assertTrue(json.loads(row["failure"])["unidentifiable_record"])
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])

    def test_dry_run_rows_cannot_override_live_environment_or_work_failures(self):
        def preview(cfg, run, slot):
            return pipeline._fail(run, "preview work failed")

        self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        with patch.object(pipeline, "_run", side_effect=preview):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, dry_run=True), 1)
        dry = run_store.newest_whole_run(self.repo.name, self.issue)
        self.assertEqual((dry["dry_run"], dry["status"], dry["failure"]), (1, "failed", None))
        abandoned = record.start("pipeline", repo=self.repo.name, issue=self.issue, dry_run=1, pid=None)
        self.time += timedelta(minutes=5)
        comments = list(self.comments)
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(run_store.run_get(abandoned)["status"], "stopped")
        self.assertEqual(self.comments, comments)
        self.assertIn("agent:failed", self.labels)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

        self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.run_failure(workspace.RunFailed("checks failed"))
        with patch.object(pipeline, "_run", return_value=("dry-run", "preview passed", "")):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, dry_run=True), 0)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue])

    def test_worktree_rechecks_the_existing_hold_and_dry_run_preserves_retry_state(self):
        self.ws.mkdir()
        held = self.ws / "owner-file"
        held.write_text("keep me")
        def drive(flow):
            return flows.ended(flow, flows.step(flow, "prepare", tree=str(self.ws)))
        row = self.run_failure(script=pipeline.Script("adw", drive))
        self.assertEqual(json.loads(row["failure"])["reason"], "worktree")
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertEqual(held.read_text(), "keep me")
        held.unlink()
        comments = list(self.comments)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(poll.poll(dry_run=True), 0)
        self.assertIn("would requeue because the worktree is free", output.getvalue())
        self.assertEqual(self.comments, comments)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(run_store.environment_requeue(row, util.iso(self.time - timedelta(days=1))), ({}, 0))
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_crashes_require_a_changed_running_engine_and_work_or_legacy_stay_failed(self):
        row = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.assertEqual(json.loads(row["failure"])["reason"], "engine-crash")
        self.assertEqual(row["engine_sha"], "old-engine")
        self.assertEqual(poll.poll(), 0)
        with patch.object(codebase, "STAMP", ""):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
            self.assertEqual(self.started, [self.issue])
            for failure in ("checks failed", "verification failed", "review requested changes"):
                row = self.run_failure(workspace.RunFailed(failure))
                self.assertEqual(json.loads(row["failure"])["class"], "work")
                self.assertEqual(poll.poll(), 0)
                self.assertIn("agent:failed", self.labels)
            legacy = record.start("pipeline", repo=self.repo.name, issue=self.issue)
            record.settle(legacy, "failed", summary="crashed: Expecting value")
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue])

    def test_fourth_failure_is_capped_across_polls_and_ages_out(self):
        began = self.time
        for _ in range(3):
            self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
            with patch.object(codebase, "STAMP", "new-engine"):
                self.assertEqual(poll.poll(), 0)
            self.time += timedelta(hours=2)
        capped = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        def lost_reply(repo, number, body, **kwargs):
            self.post_comment(repo, number, body)
            raise gh.GhUnsure("reply lost after posting")
        with patch.object(gh, "issue_comment", side_effect=lost_reply):
            self.assertEqual(poll.poll(), 1)
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue] * 3)
        notices = [c for c in self.comments if "Leaving this issue failed" in c]
        self.assertEqual(len(notices), 1)
        self.assertIn("Arete engine crash", notices[0])
        entry, count = run_store.environment_requeue(capped, util.iso(self.time - timedelta(days=1)))
        self.assertEqual((entry["cap_notified"], count), (1, 3))
        self.time = began + timedelta(hours=24)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue] * 4)

    def test_rejected_label_edits_do_not_count_and_lost_replies_reconcile_once(self):
        label = "agent:failed"

        def reject(repo, number, add=(), remove=(), **kwargs):
            self.assertEqual(list(remove), [label])
            raise gh.GhError("label edit rejected")

        def lost_reply(repo, number, add=(), remove=(), **kwargs):
            self.assertTrue(kwargs["single_attempt"])
            self.edit_labels(repo, number, add, remove)
            raise gh.GhUnsure("reply lost after removing the label")

        since = util.iso(self.time - timedelta(days=1))
        for _ in range(3):
            rejected = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
            with patch.object(gh, "issue_labels", side_effect=reject), patch.object(codebase, "STAMP", "new-engine"):
                self.assertEqual(poll.poll(), 1)
            self.assertIn(label, self.labels)
            entry, count = run_store.environment_requeue(rejected, since)
            self.assertEqual((entry["edit_pending"], entry["requeued"], count), (None, 0, 0))
        self.assertFalse(any("environment-requeue" in body for body in self.comments))

        accepted = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        with patch.object(gh, "issue_labels", side_effect=gh.GhUnsure("reply lost before removal")), \
                patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 1)
        self.assertIn(label, self.labels)
        self.assertTrue(run_store.environment_requeue(accepted, since)[0]["edit_pending"])
        with patch.object(gh, "issue_labels", side_effect=lost_reply) as edit, \
                patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 1)
            pending, _ = run_store.environment_requeue(accepted, since)
            self.assertEqual(pending["requeued"], 0)
            self.assertTrue(pending["edit_pending"])
            self.assertNotIn(label, self.labels)
            self.assertEqual(self.started, [])
            self.assertEqual(poll.poll(dry_run=True), 0)
            self.assertEqual(run_store.environment_requeue(accepted, since), (pending, 0))
            self.assertEqual(poll.poll(), 0)
            self.assertEqual(poll.poll(), 0)
            edit.assert_called_once()

        next_run = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.assertEqual(run_store.environment_requeue(next_run, since), ({}, 1))
        self.assertEqual(sum("environment-requeue" in body for body in self.comments), 1)
        self.assertIn(self.issue, self.started)

    def test_adaptive_finish_uses_the_blocking_build_result(self):
        startup = Result(ok=False, runner="claude", error="claude not found: fixture")
        recovery = pipeline.runner.start_failure(startup, "build")
        build_start = ("worker.start", "failed", {"writing": True, "event": "result", "recovery": recovery})
        unrelated = ("worker.start", "failed", {"writing": False, "event": "result", "recovery": recovery})
        for actions, expected in (
            ([build_start], {"class": "environment", **recovery}),
            ([build_start, unrelated], {"class": "environment", **recovery}),
            ([unrelated], {"class": "work"}),
            ([build_start, ("worker.continue", "failed", {"writing": True, "event": "result"})], {"class": "work"}),
            ([build_start, ("checks.run", "succeeded", {"passed": False})], {"class": "work"}),
            ([build_start, ("verification.run", "succeeded", {"operation_passed": True, "verdict": "fail"})],
             {"class": "work"}),
        ):
            with self.subTest(actions=actions):
                def drive(flow):
                    controller = orchestration.Controller(flow)
                    lease = controller.lease
                    turn = ledger.begin(lease, {}, 1)
                    version = ledger.snapshot(lease)["version"]
                    accepted = ledger.accept(lease, turn, version,
                                             {"calls": [{"op": op, "args": {}} for op, _, _ in actions]}, 10)
                    for action, (_, state, result) in zip(accepted, actions):
                        ledger.transition(lease, action["id"], state, result)
                    controller.perform({"op": "run.finish", "request": {"reason": "failed"}})
                    return controller.ending

                row = self.run_failure(script=pipeline.Script("adw", drive))
                self.assertEqual(json.loads(row["failure"]), expected)

    def test_runner_start_failure_requires_a_successful_probe(self):
        failure = Result(ok=False, runner="claude", error="claude not found: fixture")
        def drive(cfg, run, slot):
            return pipeline._fail(run, "planning step failed",
                                  recovery=pipeline.runner.start_failure(failure, "plan"))
        row = self.run_failure(drive)
        self.assertEqual(json.loads(row["failure"])["reason"], "runner-start")
        with patch.object(pipeline.runner, "started_now", side_effect=[False, True]) as probe:
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual([call.args for call in probe.call_args_list], [("claude",), ("claude",)])
        self.assertEqual(self.started, [self.issue])

    def test_rejected_login_requires_a_ready_route_with_renewed_credentials(self):
        failure = Result(ok=False, runner="claude", model="sonnet", credential="cli_login", turns=1,
                         raw={"api_error": "authentication_failed"}, auth_generation="stale-login")
        def drive(cfg, run, slot):
            return pipeline._fail(run, "the conflict resolution step failed",
                                  recovery=pipeline.runner.start_failure(failure, "build"))
        row = self.run_failure(drive)
        self.assertEqual(json.loads(row["failure"])["reason"], "runner-login")
        route = SimpleNamespace(state="ready", runner="claude", model="sonnet", credential="cli_login")
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=lambda _: [route])), \
                patch.object(pipeline.runner, "authentication", side_effect=[
                    SimpleNamespace(generation="stale-login"), SimpleNamespace(generation="new-login")]):
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_sandbox_start_checks_confinement_before_requeueing(self):
        row = self.run_failure(RuntimeError(pipeline.runner.SANDBOX_START_ERROR))
        self.assertEqual(json.loads(row["failure"])["reason"], "sandbox-start")
        with patch.object(sandbox, "can_confine", return_value=True), \
                patch.object(sandbox, "write_confinement", return_value=["sandbox-exec"]), \
                patch.object(util, "run_whole_tree", side_effect=[
                    SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as probe:
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual([call.args[0] for call in probe.call_args_list], [["sandbox-exec", "/usr/bin/true"]] * 2)
        self.assertEqual(self.started, [self.issue])


class RouteABTests(unittest.TestCase):
    def configured(self, **test):
        return config.Config(steps={"plan": {"model": "opus"}}, ab={"cheap-plan": {
            "step": "plan", "kind": "", "share": 0.5, "arm": {"model": "haiku"}, **test}})

    def sides(self, cfg, kind="feat"):
        return [steps.resolve(cfg, None, "plan", kind, ticket=("fixture", str(n))) for n in range(1, 41)]

    def test_share_decides_a_stable_side_per_ticket(self):
        cfg = self.configured()
        first, second = self.sides(cfg), self.sides(cfg)
        self.assertTrue(12 <= sum(s.ab_side == "arm" for s in first) <= 28)
        self.assertEqual([s.ab_side for s in first], [s.ab_side for s in second])
        self.assertEqual({(s.ab_side, s.model) for s in first}, {("arm", "haiku"), ("baseline", "opus")})
        self.assertEqual({s.ab_name for s in self.sides(self.configured(kind="feat"), "fix")}, {""})


if __name__ == "__main__":
    unittest.main()
