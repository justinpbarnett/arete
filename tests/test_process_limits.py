import contextlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arete import (
    config,
    dbstats,
    outcomes,
    paths,
    pipeline,
    poll,
    record,
    run_lines,
    runlimits,
    steps,
    tasks,
    tracker,
    util,
)
from arete.store import db as foundation
from arete.store import runs as run_store
from arete.store import tasks as tasks_store
from tests.harness import set_task_body


class PinCommandTests(unittest.TestCase):
    def test_pin_commands(self):
        from tests.test_behavior import CliThroughServeTests

        CliThroughServeTests().in_subprocess("check_pin_commands")


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
        self.root = Path(self.folder.name)
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


class QueueTests(Isolated):
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

    def poll_with(self, cfg, spawn):
        with patch.object(config, "load", return_value=cfg), patch.object(poll, "runnable", return_value=True), \
                patch.object(tasks, "spawn_pending", side_effect=lambda repo, tid, launch: spawn(repo, tid)), \
                patch.object(poll, "settle_abandoned_runs"), patch.object(outcomes, "sync_outcomes"), \
                patch.object(outcomes, "sync_moves"), patch.object(outcomes, "sync_landings"), \
                patch.object(poll, "retry_scratch"), \
                patch.object(poll, "cleanup_worktrees"), patch.object(poll, "mark_loop"), \
                patch.object(pipeline, "SKIPPED_FILE", self.root / "skipped.json"):
            return poll.poll()

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
        extra = tasks_store.task_add(self.repo.name, "Ready", "Ready", "feat")
        cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertIn(prerequisite, started)
        self.assertIn(extra, started)
        self.assertNotIn(blocked, started)
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
