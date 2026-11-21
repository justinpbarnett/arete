import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from arete.orchestration_api import PROTOCOL, Catalog
from arete.store import db as foundation
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

    def run_polled_ticket(self, repo, tid, work):
        _, env = launch_lease.child_environment(())
        with patch.dict(os.environ, env), patch.object(launch_lease, "registered"), \
                patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                patch.object(pipeline, "_run", side_effect=work):
            return pipeline.run_issue(repo, tid)

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
            {"what": "The page displays Mara Doe owes 240 dollars", "retry_action": "preserve_draft_on_retry",
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

    def park_remote_issue(self, source, issue):
        rid = record.start("pipeline", repo=self.repo.name, issue=issue)
        directory = paths.RUNS_DIR / rid
        directory.mkdir()
        run = pipeline.Run(rid, self.repo, directory, tracker=source, tid=issue, cfg=self.cfg)
        run.budget = runlimits.Budget(run, runlimits.Policy(), "", source.get(issue), False)
        self.park_for_triage(run)
        return run


class LocalParkRetryTests(ParkRetryFixture):
    def test_triage_rewrites_readmit_with_findings_in_the_next_plan_and_third_park_asks_once(self):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        workspace_dir = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
        workspace_dir.mkdir()
        checkout = workspace.Checkout("", "fixture/repo", self.repo.path, workspace_dir, "main")
        reasons = ("Saving the draft fails: preserve the draft after a declined save", "Restore the selected tab",
                   "The save button stays enabled while a save is in progress.")
        actions = ("Preserve the draft after a declined save.", "Restore the previously selected tab or item.",
                   reasons[2])
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
            self.assertIn(actions[attempt], prompts[-1][1])
            if attempt == 0:
                self.assertIn("Show the failure beside the field that needs correction.", prompts[-1][1])
                self.assertIn(f"Review blocker 3. {uncanned}", ticket.retries[-1].body)
            self.assertIn("Preserve the draft when retrying.", prompts[-1][1])
        self.assertEqual(self.triage_parks(), 0)
        self.assertEqual(self.triage_parks(), 0)
        self.assertEqual(self.local.get(self.ticket).body, original)
        self.assertEqual(tasks_store.task_get(self.ticket)["state"], "needs-info")
        comments = tasks_store.task_comments(self.ticket)
        self.assertEqual(len(comments), 3)
        question = comments[-1]["body"]
        self.assertIn(actions[2], question)
        self.assertIn(f'arete task reply {self.ticket} "your reply"', question)
        self.assertNotIn("Optional review note", question)
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("escalated ticket restarted")), 0)
        self.assertEqual(task_reply(SimpleNamespace(id=self.ticket, text=["Keep drafts until save succeeds."])), 0)
        started = []

        def answered(cfg, run, slot):
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(pipeline.open_branch(run), branch)
            self.assertIn("Keep drafts until save succeeds.", run.task_ticket().comments)
            started.append(run.id)
            return "noop", "fixture", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, answered)), 0)
        self.assertEqual(len(started), 1)

    def test_triage_skips_missing_route_environment_and_held_or_kept_work(self):
        budget = "the repair chain used its 2 repair cycles"
        cases = [("missing route", {}, False, False), ("held scratch", {}, False, False),
                 (budget, {".": {"head": "kept"}}, False, False), (budget, {}, True, False),
                 (budget, {}, False, True)]
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

    def test_triage_readmits_a_clean_planning_budget_park_without_findings(self):
        run = self.made_run()
        ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
        ws.mkdir()
        run.workspace = (ws, [])
        run.budget.deadline = 0
        with self.assertRaises(runlimits.Parked) as parked:
            run.budget.charge("plan", "plan")
        self.park_for_triage(run, str(parked.exception))
        self.local.label(self.ticket, add=["failed"])
        self.assertEqual(park_retries.findings(run.id, {}), [])
        before = self.local.get(self.ticket)
        reason, admission = runlimits.poll_admission(self.repo, self.ticket, before, run.budget.policy)
        self.assertIn("parked after run", reason)
        self.assertIsNone(admission)
        self.assertEqual(self.triage_parks(), 0)
        after = self.local.get(self.ticket)
        self.assertEqual(after.body, before.body)
        self.assertIn(f"## From parked run {run.id}", after.retries[-1].body)
        self.assertIn("The run reached its time budget. Replan the work", after.retries[-1].body)
        reason, admission = runlimits.poll_admission(self.repo, self.ticket, after, run.budget.policy)
        self.assertEqual(reason, "")
        self.assertEqual(admission.run_id, run.id)
        self.assertEqual(len(tasks_store.task_comments(self.ticket)), 1)

    def test_triage_filters_the_same_evidence_for_rewrites_and_escalation(self):
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
        writes.update("tasks", self.ticket, {"body": f"- [criterion:attachments] {valid}\n"
                                                     f"- [criterion:cancel] {cancel}\n"
                                                     "Check journey:save-title and journey:internal.\n"
                                                     "```markdown\n- [criterion:missing] Fixture example.\n```\n"
                                                     "<!--\n- [criterion:missing] Fixture hidden text.\n-->"})
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
                  "retry_action": "avoid_partial_write"}
        arbitrary = {"problem": "The reviewer supplied a private thought", "retry_action": "Private action text"}
        canceled = {"problem": "Canceling a task reopens closed dependencies.", "criterion_ref": "criterion:cancel"}
        checks = ({"what": "journey:save-title", "passed": False}, {"what": "journey:internal", "passed": False},
                  {"what": "Synthetic private check caption", "passed": False, "criterion_ref": "criterion:missing"})
        bodies = []
        with patch.dict(os.environ, {"TEST_SECRET": "SensitiveFixtureValue"}):
            for count in (1, 2, 2):
                run = self.made_run()
                self.parked_findings(run, action, arbitrary, with_evidence, canceled, *unsafe, extra_checks=checks)
                self.park_for_triage(run)
                self.local.label(self.ticket, add=["failed"])
                self.assertEqual(self.triage_parks(), 0)
                self.assertEqual(self.triage_parks(), 0)
                ticket = self.local.get(self.ticket)
                self.assertEqual(len(ticket.retries), count)
                bodies.append(ticket.retries[-1].body)
        comments = tasks_store.task_comments(self.ticket)
        self.assertEqual(len(comments), 3)
        self.assertEqual(tasks_store.task_get(self.ticket)["state"], "needs-info")
        for output in (*bodies, comments[-1]["body"]):
            self.assertIn("Review blocker 1 (data_loss). Keep existing data intact if any part of the write fails.",
                          output)
            self.assertIn("Review blocker 2. See this blocker in the run’s review.", output)
            self.assertIn(valid, output)
            self.assertIn(f"Review blocker 4. {cancel}", output)
            self.assertIn(f"Failed verification check 2. {retain}", output)
            self.assertIn("Failed verification check 3. See this check in the run’s verification.", output)
            self.assertIn("Preserve the draft when retrying.", output)
            for private in (*unsafe, "hidden-literal", "ghp_synthetic12345678", "private.txt", "SensitiveFixtureValue",
                            "Private page contents", "Private log text", "Private raw page text", "Private command",
                            "Private verify summary", "Private reviewer transcript", "Environment startup failed",
                            "Ordinary save succeeds", "Mara Doe", "Fixture account details", "raw.html",
                            "fixture account is overdue", "invoice status is unavailable",
                            with_evidence["problem"], "Editing a task title drops its attachments.",
                            canceled["problem"], "Fixture internal field details", "No approved tracker text",
                            "Synthetic private check caption", "criterion:missing", "Fixture example", "Fixture hidden text",
                            action["problem"], *arbitrary.values()):
                self.assertNotIn(private, output)


class GitHubParkRetryTests(ParkRetryFixture):
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
            raise runlimits.Parked("the repair chain used its 2 repair cycles")

        with patch.object(gh, "issue", return_value=issue), patch.object(gh, "gh", side_effect=command) as writes, \
                patch.object(gh, "issues", side_effect=lambda repo, label, **kw: [
                    issue] if {"name": label} in labels else []), \
                patch.object(pipeline.Run, "enter"), \
                patch.object(triage, "sources", return_value=[]):
            run = self.park_remote_issue(source, "17")
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
            self.assertIn("Reply in a comment on this GitHub issue.", comments[-1]["body"])
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("started without a reply")), 0)
            comments.append({"body": "Retain the draft until the write commits.", "author": issue["author"],
                             "createdAt": util.iso(util.parse_iso(comments[-1]["createdAt"]) + timedelta(seconds=1))})
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
            self.assertEqual(len(started), 3)
            self.assertEqual(issue["body"], original)

    def test_github_escalation_readmits_an_owner_title_or_body_edit_without_a_reply(self):
        self.repo.tracker, self.repo.github = "github", "fixture/project"
        self.repo.parts = [config.Part("", self.repo.github)]
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        self.local.label(self.ticket, add=["closed"])
        source = tracker.GitHubTracker(self.repo)
        labels = [{"name": "agent"}, {"name": "agent:needs-info"}]
        for number, field in enumerate(("title", "body"), 17):
            with self.subTest(field=field):
                issue = {"number": number, "title": "Original title", "body": "Original body", "state": "OPEN",
                         "labels": labels, "author": {"login": "fixture"}, "comments": []}
                started = []

                def work(cfg, run, slot):
                    current = run.task_ticket()
                    self.assertEqual(getattr(current, field), "Owner clarification")
                    self.assertEqual(run.budget.parents, [])
                    self.assertIsNone(run.parked_context)
                    self.assertIn("agent:needs-info", current.labels)
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
                    issue[field] = "Owner clarification"
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

    def test_batch_hold_blocks_retry_reply_and_a_stale_list_selection(self):
        issue = self.github_batch()
        source = tracker.GitHubTracker(self.repo)
        labels, comments, owner = (issue[key] for key in ("labels", "comments", "author"))

        def comment(_repo, _number, body, **_kw):
            comments.append({"body": body, "createdAt": util.iso(), "author": owner})

        def listed(_repo, label, **_kw):
            return [{**issue, "labels": [lb for lb in labels if lb["name"] != tracker.NO_AGENT]}] if {
                "name": label} in labels else []

        with patch.object(gh, "issue", return_value=issue), patch.object(gh, "issues", side_effect=listed), \
                patch.object(gh, "issue_comment", side_effect=comment) as post:
            run = self.park_remote_issue(source, "17")
            self.assertIsNone(park_retries.retry_ticket(source, "17", ()))
            post.assert_called_once()
            _, proof = runlimits.poll_admission(self.repo, "17", source.get("17"), run.budget.policy)
            self.assertTrue(proof.feedback)
            labels.append({"name": tracker.NO_AGENT})
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("held batch retry launched")), 0)
            self.assertEqual(runlimits.poll_admission(self.repo, "17", source.get("17"), run.budget.policy),
                             (tracker.NO_AGENT_HOLD, None))
            self.assertFalse(runlimits.admission(self.repo, "17", source, run.budget.policy, False,
                                                 readmission=proof)[1])

            labels.remove({"name": "agent:failed"})
            labels.append({"name": "agent:needs-info"})
            when = util.parse_iso(comments[-1]["createdAt"])
            comments.extend([{"body": f"[arete] Parked retry question for run {run.id}.", "author": owner,
                              "createdAt": util.iso(when + timedelta(seconds=1))},
                             {"body": "Keep its dependencies closed.", "author": owner,
                              "createdAt": util.iso(when + timedelta(seconds=2))}])
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("held batch reply launched")), 0)
            self.assertEqual(source.reply_since("17", util.iso(when)), "")
            labels.remove({"name": tracker.NO_AGENT})
            started = []
            self.assertEqual(self.poll_with(self.cfg, lambda _repo, tid: started.append(tid)), 0)
            self.assertEqual(started, ["17"])

    def test_new_trusted_automation_question_invalidates_an_old_owner_reply(self):
        issue = self.github_batch()
        issue["labels"].remove({"name": "agent:failed"})
        issue["labels"].append({"name": "agent:needs-info"})
        self.repo.github_automation_logins = ("fixture-app[bot]",)
        source = tracker.GitHubTracker(self.repo)

        def comment(body, login, offset):
            issue["comments"].append({"body": body, "author": {"login": login},
                                      "createdAt": util.iso(when + timedelta(seconds=offset))})

        with patch.object(gh, "issue", return_value=issue), patch.object(gh, "issues", side_effect=lambda _repo, label,
                          **_kw: [issue] if {"name": label} in issue["labels"] else []):
            first = self.park_remote_issue(source, "17")
            second = self.park_remote_issue(source, "17")
            cutoff = runlimits.metadata(second.id, "parked.json")["parked_at"]
            when = util.parse_iso(cutoff)
            older = f"[arete] Parked retry question for run {first.id}."
            newest = f"[arete] Parked retry question for run {second.id}."
            comment(older, "fixture", 1)
            comment("Old answer.", "fixture", 2)
            comment(newest, "stranger[bot]", 3)
            self.assertEqual(source.get("17").park_question.run_id, first.id)
            self.assertEqual([t.id for t in source.replied()], ["17"])
            comment(newest, "Fixture-App[bot]", 4)
            self.assertEqual(source.get("17").park_question.run_id, second.id)
            self.assertEqual(source.replied(), [])
            self.assertEqual(source.reply_since("17", cutoff), "")
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("old answer launched")), 0)
            comment("New answer.", "fixture", 5)
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

    def test_a_standalone_park_refuses_another_step_without_posting_retry_feedback(self):
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
        launch.assert_not_called()
        agent.assert_not_called()

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
                    self.assertEqual("The app did not start during the newest verification attempt." in body,
                                     startup_failed)
                    self.assertEqual("Preserve the draft when retrying." in body, not startup_failed)
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
                self.park_for_triage(run)
                self.local.label(self.ticket, add=["failed"])
                self.assertEqual(self.triage_parks(), 0)
                body = self.local.get(self.ticket).retries[-1].body
                self.assertIn("Verification failed before recording a check result.", body)
                self.assertIn("Preserve the draft when saving fails.", body)
                self.assertNotIn("Preserve the draft when retrying.", body)
                self.assertNotIn("/tmp/private.log", body)

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
