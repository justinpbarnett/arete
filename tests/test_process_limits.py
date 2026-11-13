import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from arete import claude, config, lines, paths, pipeline, record, runlimits, state, tracker, util


class Isolated(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="arete-limits-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in {"STATE_DIR": self.root / "state", "RUNS_DIR": self.root / "runs",
                            "DB_FILE": self.root / "state/arete.db", "LOCKS_DIR": self.root / "locks",
                            "CONFIG_DIR": self.root / "config", "CONFIG_FILE": self.root / "config/config.toml",
                            "LOG_DIR": self.root / "logs", "EVENTS_FILE": self.root / "state/events.jsonl",
                            "WORKTREES_DIR": self.root / "worktrees"}.items():
            self.patches.enter_context(patch.object(paths, name, value))
        paths.CONFIG_DIR.mkdir()
        paths.ensure_dirs()
        self.repo = config.Repo(name="fixture", path=self.root / "repo", tracker="local", pipeline=True)
        self.repo.path.mkdir()
        self.cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8)
        self.ticket = state.task_add(self.repo.name, "Fixture specification", "Fixture", "feat")
        self.local = tracker.LocalTracker(self.repo)

    def run(self, result=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return super().run(result)

    def made_run(self, previous="", policy=None):
        rid = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        directory = paths.RUNS_DIR / rid
        directory.mkdir()
        run = pipeline.Run(rid, self.repo, directory, tracker=self.local, tid=self.ticket, cfg=self.cfg)
        run.budget = runlimits.Budget(run, policy or runlimits.Policy(), previous, self.local.get(self.ticket))
        return run


class BudgetTests(Isolated):
    def delayed_start(self, concurrent, drive):
        before_lock, finished = threading.Event(), threading.Event()
        original_lock, failures = util.lock, []
        ticket_lock = state.run_lock("pipeline", self.repo.name, self.ticket)
        def worker():
            try:
                if not before_lock.wait(5):
                    raise AssertionError("launcher never reached its ticket lock")
                with original_lock(ticket_lock) as got:
                    if not got:
                        raise AssertionError("concurrent attempt could not acquire its ticket lock")
                    concurrent()
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()
        @contextlib.contextmanager
        def delayed_lock(name, *args, **kw):
            if name == ticket_lock:
                before_lock.set()
                if not finished.wait(5):
                    raise AssertionError("concurrent attempt never finished")
            with original_lock(name, *args, **kw) as got:
                yield got
        thread = threading.Thread(target=worker)
        thread.start()
        called = []
        try:
            with patch.object(config, "load", return_value=self.cfg), \
                    patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                    patch.object(util, "lock", side_effect=delayed_lock), \
                    patch.object(pipeline.runner, "run", side_effect=lambda *a, **k: called.append(True)):
                code = pipeline.run_issue(self.repo.name, self.ticket, force=True,
                                          script=pipeline.Script("feature", drive))
        finally:
            before_lock.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(called, [])
        return code

    def test_delayed_launcher_counts_attempt_completed_before_ticket_lock(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        record.settle(first.id, "failed")
        def concurrent():
            second = self.made_run(first.id)
            second.budget.charge("review_fix2", "review_repair")
            record.settle(second.id, "failed")
        def drive(flow):
            flow.ctx.run.agent("repair", "one repair too many", self.repo.path, role="review_repair")
        self.assertEqual(self.delayed_start(concurrent, drive), 1)
        latest = state.ticket_runs(self.repo.name, self.ticket)[0]
        self.assertIn("repair cycles", latest["summary"])
        self.assertTrue((paths.RUNS_DIR / latest["id"] / "parked.json").exists())

    def test_delayed_launcher_does_not_reuse_another_epoch_reset(self):
        first = self.made_run()
        first.budget.park("first epoch exhausted")
        record.settle(first.id, "failed")
        with state.db() as connection:
            connection.execute("UPDATE tasks SET body = ? WHERE id = ?", ("A narrower specification", self.ticket))
        def concurrent():
            second = self.made_run()
            second.budget.charge("review_fix1", "review_repair")
            second.budget.charge("review_fix2", "review_repair")
            second.budget.park("second epoch exhausted")
            record.settle(second.id, "failed")
        def drive(flow):
            flow.ctx.run.agent("repair", "stale reset must not run", self.repo.path, role="review_repair")
        self.assertEqual(self.delayed_start(concurrent, drive), 2)
        self.assertEqual(len(state.ticket_runs(self.repo.name, self.ticket)), 2)

    def test_interrupted_atomic_publish_keeps_previous_authoritative_files(self):
        run = self.made_run()
        run.budget.park("original park")
        for name, action in (("budget.json", run.budget.persist),
                             ("parked.json", lambda: run.budget.park("replacement park"))):
            path = run.dir / name
            before = path.read_bytes()
            with patch.object(util.os, "replace", side_effect=runlimits.Parked("interrupted before replace")):
                with self.assertRaises(runlimits.Parked):
                    action()
            self.assertEqual(path.read_bytes(), before)
            self.assertIsInstance(json.loads(path.read_text()), dict)

    def test_limits_validate_positive_integers_and_explicit_parallel_opt_in(self):
        (self.root / "config.toml").write_text("[limits]\nmax_repair_cycles=4\nmax_run_minutes=90\n"
                                             "max_verify_minutes=25\nparallel_checks=true\n")
        layer = config.load_layer(self.root)
        self.assertEqual(runlimits.Policy.of(layer), runlimits.Policy(4, 90, 25))
        self.assertTrue(layer.parallel_checks)
        for bad in ("max_repair_cycles=0", "max_run_minutes=true", "max_verify_minutes=1.5", "parallel_checks=1"):
            (self.root / "config.toml").write_text("[limits]\n" + bad)
            with self.assertRaises(ValueError):
                config.load_layer(self.root)

    def test_repair_budget_is_charged_before_launch_and_survives_continuation(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        invoked = []
        with patch.object(pipeline.runner, "run", side_effect=lambda *a, **k: invoked.append(True)):
            with self.assertRaises(runlimits.Parked):
                second.agent("repair", "repair again", self.repo.path, role="review_repair")
        self.assertEqual(invoked, [])
        self.assertEqual(runlimits.repair_count(second.id), 0)

    def test_fresh_process_inherits_consumed_repair_budget(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        overrides = {name: str(getattr(paths, name)) for name in (
            "STATE_DIR", "RUNS_DIR", "DB_FILE", "LOCKS_DIR", "CONFIG_DIR", "CONFIG_FILE", "LOG_DIR",
            "EVENTS_FILE", "WORKTREES_DIR")}
        code = (f"import sys;sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r});"
                "from pathlib import Path;from arete import config,paths,pipeline,record,runlimits,tracker;"
                f"overrides={overrides!r};"
                "\nfor name,value in overrides.items():setattr(paths,name,Path(value))\n"
                f"repo=config.Repo(name='fixture',path=Path({str(self.repo.path)!r}),tracker='local');"
                f"tid={self.ticket!r};rid=record.start('pipeline',repo=repo.name,issue=tid);"
                "folder=paths.RUNS_DIR/rid;folder.mkdir();local=tracker.LocalTracker(repo);"
                "run=pipeline.Run(rid,repo,folder,tracker=local,tid=tid);"
                f"budget=runlimits.Budget(run,runlimits.Policy(),{first.id!r},local.get(tid));"
                "\ntry:budget.charge('repair','review_repair')"
                "\nexcept runlimits.Parked:print('budget remained exhausted')"
                "\nelse:raise AssertionError('restart reset the budget')")
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=5,
                              env={k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("budget remained exhausted", done.stdout)

    def test_repair_usage_is_durable_even_when_audit_event_cannot_be_written(self):
        first = self.made_run()
        with patch.object(lines, "write", return_value={}):
            first.budget.charge("review_fix1", "review_repair")
            first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
            second.budget.charge("repair", "review_repair")

    def test_abrupt_process_exit_retains_active_verification_checkpoint_without_audit(self):
        overrides = {name: str(getattr(paths, name)) for name in (
            "STATE_DIR", "RUNS_DIR", "DB_FILE", "LOCKS_DIR", "CONFIG_DIR", "CONFIG_FILE", "LOG_DIR",
            "EVENTS_FILE", "WORKTREES_DIR")}
        before = util.iso(util.now() - timedelta(minutes=21))
        code = (f"import os,sys;sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r});"
                "from pathlib import Path;from arete import config,paths,pipeline,record,runlimits,tracker,util,lines;"
                f"overrides={overrides!r};"
                "\nfor name,value in overrides.items():setattr(paths,name,Path(value))\n"
                f"util.now=lambda:util.parse_iso({before!r});"
                f"repo=config.Repo(name='fixture',path=Path({str(self.repo.path)!r}),tracker='local');"
                f"tid={self.ticket!r};rid=record.start('pipeline',repo=repo.name,issue=tid);"
                "folder=paths.RUNS_DIR/rid;folder.mkdir();local=tracker.LocalTracker(repo);"
                "run=pipeline.Run(rid,repo,folder,tracker=local,tid=tid);"
                "budget=runlimits.Budget(run,runlimits.Policy(),'',local.get(tid));"
                "lines.write=lambda *a,**k:{};"
                "\nwith budget.watching(),budget.verifying():os._exit(0)")
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=5,
                              env={k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")})
        self.assertEqual(done.returncode, 0, done.stderr)
        previous = state.ticket_runs(self.repo.name, self.ticket)[0]
        record.settle(previous["id"], "interrupted")
        next_run = self.made_run(previous["id"])
        with self.assertRaisesRegex(runlimits.Parked, "verification"):
            with next_run.budget.watching(), next_run.budget.verifying():
                self.fail("abrupt exit reset verification usage")

    def test_legacy_nested_announcements_count_one_cycle(self):
        rid = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        lines.write(rid, "step_start", step="implement", atom="review_repair", **{"try": 2})
        lines.write(rid, "step_start", step="implement", call="review_fix1", atom="review_repair", **{"try": 2})
        lines.write(rid, "step_start", step="verify", call="verify2", **{"try": 2})
        self.assertEqual(runlimits.repair_count(rid), 1)

    def test_cumulative_time_survives_continuation(self):
        first = self.made_run()
        record.note(first.id, started=util.iso(util.now() - timedelta(minutes=61)))
        record.settle(first.id, "interrupted")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "cumulative 60"):
            with second.budget.watching():
                self.fail("an exhausted chain must not run")

    def test_verification_deadline_interrupts_and_is_accounted(self):
        run = self.made_run(policy=runlimits.Policy(max_verify_minutes=.001))
        started = time.monotonic()
        with self.assertRaisesRegex(runlimits.Parked, "verification"):
            with run.budget.watching(), run.budget.verifying():
                time.sleep(2)
        self.assertLess(time.monotonic() - started, 1)
        record.settle(run.id, "failed")
        next_run = self.made_run(run.id, run.budget.policy)
        with self.assertRaisesRegex(runlimits.Parked, "verification"):
            with next_run.budget.watching(), next_run.budget.verifying():
                self.fail("verification time must not reset")

    def test_interrupted_verification_phase_is_counted_on_restart(self):
        before = util.now() - timedelta(minutes=21)
        with patch.object(util, "now", return_value=before):
            first = self.made_run()
            lines.write(first.id, "budget_verify_start")
        record.settle(first.id, "interrupted")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "verification"):
            with second.budget.watching(), second.budget.verifying():
                self.fail("unfinished verification phases must count")

    def test_unchanged_parked_input_refuses_retry_changed_spec_requires_explicit_retry(self):
        run = self.made_run()
        with patch.object(runlimits, "heads", return_value={"": "old"}):
            run.budget.park("repair limit")
            record.settle(run.id, "failed", branch="fixture")
            why, reset = runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True)
            self.assertIn("parked", why)
            self.assertFalse(reset)
            with state.db() as connection:
                connection.execute("UPDATE tasks SET body = ? WHERE id = ?", ("A narrower specification", self.ticket))
            self.assertTrue(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, False)[0])
            self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True), ("", True))

    def test_changed_published_head_allows_explicit_new_epoch(self):
        run = self.made_run()
        record.note(run.id, branch="fixture")
        with patch.object(runlimits, "heads", return_value={"": "old"}):
            run.budget.park("repair limit")
        record.settle(run.id, "failed")
        with patch.object(runlimits, "heads", return_value={"": "new"}):
            self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True), ("", True))
        next_run = self.made_run()
        next_run.budget.charge("repair", "review_repair")
        self.assertEqual(next_run.budget.parents, [])

    def test_unknown_published_head_does_not_claim_material_change(self):
        run = self.made_run()
        record.note(run.id, branch="fixture")
        with patch.object(runlimits, "heads", side_effect=ValueError("offline")):
            run.budget.park("repair limit")
        record.settle(run.id, "failed")
        with patch.object(runlimits, "heads", return_value={"": "any"}):
            self.assertTrue(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True)[0])

    def test_corrupt_budget_is_not_silently_reset(self):
        run = self.made_run()
        (run.dir / "budget.json").write_text("broken json")
        with self.assertRaises(ValueError):
            runlimits.family(run.id)

    def test_corrupt_verification_usage_does_not_reset_spent_time(self):
        run = self.made_run()
        path = run.dir / "budget.json"
        original = json.loads(path.read_text())
        for invalid in (-1, float("nan"), "unknown"):
            util.write_json(path, {**original, "verification_seconds": invalid})
            with self.assertRaises(ValueError):
                runlimits.verification_seconds(run.id, state.run_get(run.id))

    def test_individual_step_reruns_cannot_reset_the_ticket_budget(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        second.budget.charge("review_fix2", "review_repair")
        record.note(second.id, only_step="implement")
        record.settle(second.id, "step")
        called = []
        def drive(flow):
            flow.ctx.run.agent("repair", "repair again", self.repo.path, role="review_repair")
        with patch.object(config, "load", return_value=self.cfg), \
                patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                patch.object(pipeline.runner, "run", side_effect=lambda *a, **k: called.append(True)):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, force=True,
                             script=pipeline.Script("feature", drive, continues=first.id)), 1)
        latest = state.ticket_runs(self.repo.name, self.ticket)[0]
        self.assertEqual(called, [])
        self.assertIn("repair cycles", latest["summary"])
        self.assertTrue((paths.RUNS_DIR / latest["id"] / "parked.json").exists())

    def test_whole_pipeline_deadline_stops_child_keeps_workspace_and_frees_slot(self):
        pid_file = self.root / "child-pid"
        workspace = paths.WORKTREES_DIR / "fixture-held"
        workspace.mkdir()
        owner = workspace / "owner.txt"
        owner.write_text("keep this work")

        def transport(*args, **kw):
            code = (f"import os,time;from pathlib import Path;Path({str(pid_file)!r}).write_text(str(os.getpid()));"
                    "time.sleep(20)")
            return claude.run_streaming([sys.executable, "-I", "-c", code], str(workspace), "", {}, 30, lambda _: None)

        def drive(flow):
            run = flow.ctx.run
            run.workspace = workspace, []
            run.save("proof.json", {"before_timeout": True})
            run.agent("build", "offline child", workspace)
            self.fail("deadline must interrupt model transport")

        script = pipeline.Script("feature", drive)
        with patch.object(config, "load", return_value=self.cfg), patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                patch.object(runlimits.Policy, "of", return_value=runlimits.Policy(max_run_minutes=.005)), \
                patch.object(pipeline.runner, "run", side_effect=transport):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, force=True, script=script), 1)
        row = state.ticket_runs(self.repo.name, self.ticket, whole=True)[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("parked", row["summary"])
        self.assertEqual(owner.read_text(), "keep this work")
        self.assertTrue((paths.RUNS_DIR / row["id"] / "parked.json").is_file())
        self.assertTrue((paths.RUNS_DIR / row["id"] / "proof.json").is_file())
        self.assertFalse(util.lock_is_held(state.run_lock("slot", self.repo.name, self.ticket)))
        self.assertEqual(state.task_get(self.ticket)["state"], "failed")
        self.assertTrue(pid_file.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


class QueueTests(Isolated):
    def interrupted_repair(self):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath="adw"\n[verify]\nnotes="offline"\n')
        source = self.made_run()
        record.settle(source.id, "pr")
        current = self.made_run(source.id)
        lines.write(current.id, "repair", source=source.id)
        record.settle(current.id, "interrupted")
        return current

    def test_interrupted_repair_ignores_held_planner_and_uses_repair_atom(self):
        self.interrupted_repair()
        seen, started = [], []
        def routes(setting, *args, **kw):
            seen.append(setting.step)
            return [SimpleNamespace(state="held" if setting.step in ("plan", "build") else "ready",
                                    detail="fixture quota hold", until="")]
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=routes)):
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertEqual(started, [self.ticket])
        self.assertEqual(seen, ["review_repair", "verify", "review"])

    def test_interrupted_repair_waits_for_its_actual_held_repair_route(self):
        self.interrupted_repair()
        seen, started = [], []
        def routes(setting, *args, **kw):
            seen.append(setting.step)
            return [SimpleNamespace(state="held" if setting.step == "review_repair" else "ready",
                                    detail="repair quota hold", until="")]
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=routes)):
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertEqual(started, [])
        self.assertEqual(seen, ["review_repair"])

    def test_revision_ignores_held_planner(self):
        seen, started = [], []
        def routes(setting, *args, **kw):
            seen.append(setting.step)
            return [SimpleNamespace(state="held" if setting.step == "plan" else "ready",
                                    detail="planner quota hold", until="")]
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=routes)), \
                patch.object(pipeline, "pending_feedback", return_value=[{"id": 1}]):
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertEqual(started, [self.ticket])
        self.assertEqual(seen, ["build", "review"])

    def test_interrupted_ticket_rechecks_dependencies_before_starting(self):
        interrupted = self.made_run()
        record.settle(interrupted.id, "interrupted")
        prerequisite = state.task_add(self.repo.name, "Prerequisite", "Prerequisite", "feat")
        with state.db() as connection:
            connection.execute("UPDATE tasks SET body = ? WHERE id = ?", (f"Waits on {prerequisite}", self.ticket))
        self.cfg.replay = config.ReplayConfig(all=True)
        started = []
        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertNotIn(self.ticket, started)
        self.assertIn(prerequisite, started)

    def poll_with(self, cfg, spawn):
        with patch.object(config, "load", return_value=cfg), patch.object(pipeline, "runnable", return_value=True), \
                patch.object(pipeline, "spawn_run", side_effect=spawn), \
                patch.object(pipeline, "settle_abandoned_runs"), patch.object(pipeline, "sync_outcomes"), \
                patch.object(pipeline, "sync_moves"), patch.object(pipeline, "sync_landings"), \
                patch.object(pipeline, "cleanup_worktrees"), patch.object(pipeline, "mark_loop"), \
                patch.object(pipeline, "SKIPPED_FILE", self.root / "skipped.json"):
            return pipeline.poll()

    def test_one_poll_fills_capacity_round_robin_without_relying_on_child_rows(self):
        second = config.Repo(name="other", path=self.root / "other", tracker="local", pipeline=True)
        second.path.mkdir()
        for repo in (self.repo, second):
            for i in range(4):
                state.task_add(repo.name, f"Task {i}", f"Task {i}", "feat")
        cfg = config.Config(repos=[self.repo, second], max_parallel=3, max_parallel_per_repo=2,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append((repo, tid))), 0)
        self.assertEqual(len(started), 3)
        self.assertEqual([repo for repo, _ in started[:2]], [self.repo.name, second.name])
        self.assertEqual(len(set(started)), 3)
        self.assertLessEqual(sum(repo == self.repo.name for repo, _ in started), 2)

    def test_dependency_and_parked_ticket_do_not_block_other_ready_work(self):
        prerequisite = state.task_add(self.repo.name, "Prerequisite", "Prerequisite", "feat")
        blocked = state.task_add(self.repo.name, f"Waits on {prerequisite}", "Blocked", "feat")
        parked = self.made_run()
        parked.budget.park("repair limit")
        record.settle(parked.id, "failed")
        extra = state.task_add(self.repo.name, "Ready", "Ready", "feat")
        cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertIn(prerequisite, started)
        self.assertIn(extra, started)
        self.assertNotIn(blocked, started)
        self.assertNotIn(self.ticket, started)

    def test_unavailable_routes_do_not_consume_slot_and_scan_is_bounded(self):
        for i in range(5):
            state.task_add(self.repo.name, str(i), str(i), "feat")
        started = []
        with patch.object(pipeline, "route_blocker", return_value="verify is held"), \
                patch.object(pipeline, "MAX_STARTS_PER_POLL", 3):
            self.assertEqual(self.poll_with(self.cfg, lambda *args: started.append(args)), 0)
        self.assertEqual(started, [])
        self.assertEqual(len(json.loads((self.root / "skipped.json").read_text())), 3)

    def test_spawn_failure_never_loops_on_same_ticket(self):
        self.cfg.replay = config.ReplayConfig(all=True)
        attempts = []
        def fail(repo, tid):
            attempts.append(tid)
            raise OSError("fixture spawn failure")
        self.assertEqual(self.poll_with(self.cfg, fail), 1)
        self.assertEqual(attempts, [self.ticket])

    def test_route_readiness_is_rechecked_after_every_start(self):
        for i in range(3):
            state.task_add(self.repo.name, str(i), str(i), "feat")
        ready, started = [True], []
        def routes(*args, **kw):
            return [SimpleNamespace(state="ready" if ready[0] else "held", detail="fixture quota hold", until="")]
        def start(repo, tid):
            started.append(tid)
            ready[0] = False
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=routes)):
            self.assertEqual(self.poll_with(self.cfg, start), 0)
        self.assertEqual(len(started), 1)


class CheckConcurrencyTests(Isolated):
    def checks(self, parallel):
        starts = []
        layer = config.RepoLayer(dir=self.root, parallel_checks=parallel)
        workers = [self.root / "one", self.root / "two"]
        for folder in workers:
            folder.mkdir()
        code = ("import json,time;from pathlib import Path;"
                "s=time.monotonic();time.sleep(.15);"
                "Path('interval.json').write_text(json.dumps([s,time.monotonic()]))")
        if parallel:
            code = ("import json,time;from pathlib import Path;"
                    "p=Path.cwd();(p.parent/(p.name+'.ready')).touch();s=time.monotonic();"
                    "\nwhile len(list(p.parent.glob('*.ready')))<2 and time.monotonic()-s<2:time.sleep(.01)"
                    "\nassert len(list(p.parent.glob('*.ready')))==2;time.sleep(.1);"
                    "Path('interval.json').write_text(json.dumps([s,time.monotonic()]))")
        layer.checks = {"behavior": shlex.join([sys.executable, "-I", "-c", code])}
        def checked(folder):
            run = pipeline.AgentRun(folder.name, self.repo, folder, cfg=self.cfg)
            return pipeline.run_checks(layer, folder, [], run, 5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(checked, workers))
        self.assertTrue(all(ok for ok, _ in results), results)
        for folder in workers:
            starts.append(json.loads((folder / "interval.json").read_text()))
        return starts

    def test_default_checks_remain_serialized(self):
        first, second = self.checks(False)
        self.assertTrue(first[1] <= second[0] or second[1] <= first[0])

    def test_explicit_isolated_checks_overlap(self):
        first, second = self.checks(True)
        self.assertLess(max(first[0], second[0]), min(first[1], second[1]))


if __name__ == "__main__":
    unittest.main()
