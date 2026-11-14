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

from arete import (
    claude,
    cli,
    config,
    control,
    diffguard,
    factory,
    flows,
    gh,
    lines,
    paths,
    pi,
    pipeline,
    record,
    runlimits,
    scratch,
    serve,
    state,
    steps,
    tracker,
    util,
)
from arete.agent import Result


def setUpModule():
    folder = tempfile.TemporaryDirectory(prefix="arete-no-live-")
    unittest.addModuleCleanup(folder.cleanup)
    root = Path(folder.name).resolve()
    for pointer in (patch.object(scratch, "AUTHORITY", root / "authority" / "verification-authority"),
                    patch.object(paths, "SERVER_FILE", root / "state" / "server.json"),
                    patch.object(paths, "PI_KEYS_FILE", root / "config" / "pi.env"),
                    *(patch.object(pi, name, root / "state" / getattr(pi, name).name)
                      for name in ("AGENT_DIR", "LOGIN_DIR", "KEY_DIR", "SESSIONS_DIR"))):
        pointer.start()
        unittest.addModuleCleanup(pointer.stop)


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
                              cwd=str(self.root),
                              env={**{k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")},
                                   "HOME": str(self.root)})
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
                              cwd=str(self.root),
                              env={**{k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")},
                                   "HOME": str(self.root)})
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

    def test_idle_waits_move_the_deadline_and_other_waits_do_not(self):
        for cause in ("limit", "slot"):
            with self.subTest(cause=cause):
                run = self.made_run(policy=runlimits.Policy(max_run_minutes=.05))
                started = time.monotonic()
                with self.assertRaisesRegex(runlimits.Parked, "run chain"):
                    with run.budget.watching():
                        with run.waiting(cause):
                            time.sleep(.9)
                        self.assertGreater(run.budget.deadline - time.monotonic(), 0)
                        time.sleep(6)
                self.assertLess(time.monotonic() - started, 3 + .9 + 1)
                self.assertGreater(runlimits.metadata(run.id, "budget.json")["wait_minutes_left_out"][cause], 0)
        run = self.made_run(policy=runlimits.Policy(max_run_minutes=.05))
        with self.assertRaisesRegex(runlimits.Parked, "run chain"):
            with run.budget.watching(), run.waiting("ci"):
                time.sleep(6)

    def chained_after(self, *waits):
        first = self.made_run()
        record.note(first.id, started=util.iso(util.now() - timedelta(minutes=61)))
        for cause, since, until in waits:
            if until is None:
                lines.wait_start(first.id, cause, util.iso(util.now() - timedelta(minutes=since)))
            else:
                lines.waited(first.id, cause, util.iso(util.now() - timedelta(minutes=since)),
                             util.iso(util.now() - timedelta(minutes=until)))
        record.settle(first.id, "interrupted")
        return first, self.made_run(first.id)

    def test_limit_wait_of_an_earlier_run_is_left_out_of_prior_seconds(self):
        for waits in ([("limit", 50, 10)], [("limit", 50, None)]):
            with self.subTest(waits=waits):
                first, second = self.chained_after(*waits)
                self.assertLess(second.budget.prior_seconds, 25 * 60)
                self.assertGreater(second.budget.earlier_idle["limit"], 35 * 60)
                with second.budget.watching():
                    pass
                self.assertGreater(
                    runlimits.metadata(second.id, "budget.json")["earlier_wait_minutes_left_out"]["limit"], 35)

    def test_ci_waits_and_waits_before_the_run_started_still_count(self):
        for waits in ([("ci", 50, 10)], [("slot", 90, 70)]):
            with self.subTest(waits=waits):
                first, second = self.chained_after(*waits)
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

    def legacy_held_run(self, recorded=True, status="pr"):
        rid = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        if recorded:
            ticket = self.local.get(self.ticket)
            lines.write(rid, "step_end", step="prepare", result="pass",
                        data={"ticket": {"title": ticket.title, "body": ticket.body}})
        for n in (1, 2, 3):
            lines.write(rid, "step_start", step="implement", call=f"review_fix{n}", atom="review_repair", **{"try": n})
        record.settle(rid, status)
        return rid

    def started_after(self, force):
        def drive(flow):
            flow.ctx.run.budget.charge("review_fix", "review_repair")
            return "failed", "fixture ended", ""
        with patch.object(config, "load", return_value=self.cfg), \
                patch.object(pipeline, "run_refusal", return_value=(True, "")):
            code = pipeline.run_issue(self.repo.name, self.ticket, force=force,
                                      script=pipeline.Script("feature", drive))
        latest = state.ticket_runs(self.repo.name, self.ticket)[0]
        directory = paths.RUNS_DIR / latest["id"]
        return code, latest, (directory / "run.log").read_text(), runlimits.metadata(latest["id"], "budget.json")

    def edit_ticket(self):
        with state.db() as connection:
            connection.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Edited with a follow up", self.ticket))

    def test_forced_run_leaves_out_repairs_spent_under_another_specification(self):
        legacy = self.legacy_held_run()
        self.edit_ticket()
        code, latest, log, budget = self.started_after(True)
        self.assertEqual(code, 1)
        self.assertIn("left out 3 earlier repair cycles", log)
        self.assertIn("specification changed", log)
        self.assertEqual((budget["parents"], budget["left_out"]), ([], [legacy]))
        self.assertFalse((paths.RUNS_DIR / latest["id"] / "parked.json").exists())
        self.assertEqual(runlimits.repair_count(latest["id"]), 1)
        second = self.started_after(True)[3]
        self.assertEqual(second["parents"], [latest["id"]])
        self.assertNotIn(legacy, runlimits.family(latest["id"]))

    def test_forcing_without_an_edit_or_polling_after_one_still_counts_everything(self):
        self.legacy_held_run()
        _, latest, log, _ = self.started_after(True)
        self.assertIn("repair cycles", latest["summary"])
        self.assertNotIn("left out", log)
        self.assertTrue((paths.RUNS_DIR / latest["id"] / "parked.json").exists())

    def test_polled_run_after_an_edit_counts_earlier_repairs(self):
        legacy = self.legacy_held_run()
        self.edit_ticket()
        _, latest, log, budget = self.started_after(False)
        self.assertIn("repair cycles", latest["summary"])
        self.assertNotIn("left out", log)
        self.assertEqual(budget["parents"], [legacy])

    def test_unreadable_specification_counts_in_full(self):
        self.legacy_held_run(recorded=False)
        self.edit_ticket()
        _, latest, log, _ = self.started_after(True)
        self.assertIn("repair cycles", latest["summary"])
        self.assertNotIn("left out", log)

    def exhausted_then_edited(self, **budget):
        old = self.made_run()
        with state.db() as connection:
            connection.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Edited with a follow up", self.ticket))
        if "verification_seconds" in budget:
            saved = runlimits.metadata(old.id, "budget.json")
            util.write_json(paths.RUNS_DIR / old.id / "budget.json", {**saved, **budget})
        else:
            record.note(old.id, started=util.iso(util.now() - timedelta(minutes=61)))
        record.settle(old.id, "pr")
        return old

    def forced_budget(self, previous, forced):
        rid = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        run = pipeline.Run(rid, self.repo, paths.RUNS_DIR / rid, tracker=self.local, tid=self.ticket, cfg=self.cfg)
        return runlimits.Budget(run, runlimits.Policy(), previous, self.local.get(self.ticket), forced)

    def test_forced_run_leaves_out_run_and_verification_minutes_of_another_specification(self):
        for budget in ({}, {"verification_seconds": 21 * 60}):
            old = self.exhausted_then_edited(**budget)
            fresh = self.forced_budget(old.id, True)
            with fresh.watching(), fresh.verifying():
                pass
            self.assertEqual((fresh.parents, fresh.left_out), ([], [old.id]))
            kept = self.forced_budget(old.id, False)
            with self.assertRaises(runlimits.Parked):
                with kept.watching(), kept.verifying():
                    pass
            record.settle(fresh.run.id, "failed")
            record.settle(kept.run.id, "failed")
            with state.db() as connection:
                connection.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Original", self.ticket))

    def test_restarting_an_interrupted_run_keeps_inherited_usage_after_an_edit(self):
        legacy = self.legacy_held_run(status="interrupted")
        self.edit_ticket()
        _, latest, log, budget = self.started_after(True)
        self.assertEqual(budget["parents"], [legacy])
        self.assertNotIn("left out", log)

    def test_invalid_left_out_lineage_is_refused(self):
        run = self.made_run()
        util.write_json(paths.RUNS_DIR / run.id / "budget.json", {"version": 1, "parents": [], "left_out": "x"})
        with self.assertRaises(ValueError):
            runlimits.family(run.id)

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
                patch.object(runlimits.Policy, "of", return_value=runlimits.Policy(max_run_minutes=.05)), \
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


class KeptEditsTests(Isolated):
    BRANCH = "arete/t1-fixture"
    URL = "https://github.com/o/fixture/pull/1"

    def setUp(self):
        super().setUp()
        self.origin = self.root / "origin.git"
        util.git(["init", "-q", "--bare", "-b", "main", str(self.origin)], self.root)
        util.git(["init", "-q", "-b", "main"], self.repo.path)
        self.commit(self.repo.path, "shared.txt", "one\n", "first")
        util.git(["remote", "add", "origin", str(self.origin)], self.repo.path)
        util.git(["push", "-q", "origin", "main:main", f"main:refs/heads/{self.BRANCH}"], self.repo.path)
        self.repo.parts = [config.Part("", "o/fixture")]
        self.patches.enter_context(patch.object(gh, "open_pr_url", return_value=self.URL))
        self.patches.enter_context(patch.object(gh, "closed_pr_heads", return_value=[]))
        self.layer, self.ticket_data = config.RepoLayer(dir=Path()), tracker.Ticket("", "", "")

    def commit(self, where, name, text, message):
        (where / name).write_text(text)
        util.git(["add", name], where)
        util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", message], where)

    def head(self, where):
        return util.git(["rev-parse", "HEAD"], where).strip()

    def start(self, run):
        made = pipeline.make_workspace(run.id, self.repo, "main", self.BRANCH, "t1", [])
        run.workspace = made
        return made

    def parked_edit(self, name="edit.txt", text="edit\n"):
        run = self.made_run()
        ws, checkouts = self.start(run)
        (checkouts[0].wt / name).write_text(text)
        pipeline.keep_edits(run)
        pipeline.note_made(ws, checkouts)
        return ws, checkouts[0]

    def push_from_elsewhere(self, name, text, ref=BRANCH):
        src = self.repo.path
        util.git(["fetch", "-q", "origin"], src)
        util.git(["checkout", "-q", "--detach", f"origin/{ref}"], src)
        self.commit(src, name, text, "moved")
        util.git(["push", "-q", "origin", f"HEAD:refs/heads/{ref}"], src)
        util.git(["checkout", "-q", "main"], src)

    def run_of(self, where, rev="HEAD"):
        return util.git(["log", "-1", "--format=%(trailers:key=Arete-Run,valueonly)", rev], where).strip()

    def test_next_run_continues_on_the_kept_edits_over_the_pr_head(self):
        _, first = self.parked_edit()
        kept, pr_head = self.head(first.wt), util.git(["rev-parse", f"origin/{self.BRANCH}"], self.repo.path).strip()
        run = self.made_run()
        _, checkouts = self.start(run)
        pipeline.settle_starts(run, checkouts, self.layer, self.ticket_data, self.BRANCH)
        self.assertEqual(self.head(checkouts[0].wt), kept)
        self.assertEqual(checkouts[0].onto, pr_head)
        self.assertIn("left unpushed", checkouts[0].starting_point)
        self.assertEqual(pipeline.stage(checkouts, []).files, ["edit.txt"])

    def test_reset_worktree_is_refused_naming_both_commits(self):
        _, first = self.parked_edit()
        kept = self.head(first.wt)
        util.git(["reset", "-q", "--hard", "HEAD~1"], first.wt)
        with self.assertRaises(pipeline.RunFailed) as refused:
            self.start(self.made_run())
        self.assertIn(kept[:12], refused.exception.why)
        self.assertIn(self.head(first.wt)[:12], refused.exception.why)
        self.assertTrue((first.wt / ".git").exists())

    def test_moved_branch_is_merged_into_the_kept_edits(self):
        _, first = self.parked_edit()
        kept = self.head(first.wt)
        self.push_from_elsewhere("moved.txt", "moved\n")
        new_head = util.git(["rev-parse", f"origin/{self.BRANCH}"], self.repo.path).strip()
        run = self.made_run()
        _, checkouts = self.start(run)
        pipeline.settle_starts(run, checkouts, self.layer, self.ticket_data, self.BRANCH)
        wt = checkouts[0].wt
        self.assertEqual(util.git(["rev-list", "--parents", "-n1", "HEAD"], wt).split()[1:], [kept, new_head])
        self.assertEqual(checkouts[0].onto, new_head)
        self.assertTrue((wt / "edit.txt").exists() and (wt / "moved.txt").exists())
        self.assertEqual(self.run_of(wt), run.id)
        self.assertEqual(flows.foreign(wt, "HEAD", [kept, new_head], {run.id}), [])

    def test_base_merged_into_a_published_branch_through_a_resolved_conflict_stays_in_the_runs_chain(self):
        self.push_from_elsewhere("shared.txt", "mine\n")
        self.push_from_elsewhere("shared.txt", "theirs\n", "main")
        run = self.made_run()
        _, checkouts = self.start(run)
        wt = checkouts[0].wt

        def resolver(step, text, cwd, **kw):
            (cwd / "shared.txt").write_text("mine and theirs\n")
            return Result(True, structured={"resolved": True})

        with patch.object(run, "agent", side_effect=resolver):
            pipeline.settle_starts(run, checkouts, self.layer, self.ticket_data, self.BRANCH)
        self.assertEqual(checkouts[0].resolved, ["shared.txt"])
        self.assertEqual((wt / "shared.txt").read_text(), "mine and theirs\n")
        self.assertEqual(self.run_of(wt), run.id)
        self.assertEqual(flows.foreign(wt, "HEAD", [f"origin/{self.BRANCH}"], {run.id}), [])

    def test_denied_paths_stay_out_of_the_kept_commit(self):
        run = self.made_run()
        ws, checkouts = self.start(run)
        wt = checkouts[0].wt
        (wt / "edit.txt").write_text("edit\n")
        (wt / ".env").write_text("SECRET=1\n")
        pipeline.keep_edits(run)
        self.assertEqual(util.git(["show", "--name-only", "--format=", "HEAD"], wt).split(), ["edit.txt"])
        self.assertTrue((wt / ".env").exists())
        self.assertEqual(pipeline.stage(checkouts, diffguard.denied_paths(self.layer)).denied, [".env"])
        pipeline.note_made(ws, checkouts)
        self.push_from_elsewhere("moved.txt", "moved\n")
        again = self.made_run()
        _, second = self.start(again)
        with self.assertRaises(pipeline.RunFailed) as refused:
            pipeline.settle_starts(again, second, self.layer, self.ticket_data, self.BRANCH)
        self.assertIn(".env", refused.exception.why)
        self.assertTrue((wt / ".env").exists())

    def test_commit_left_unpushed_by_a_failed_publication_is_kept_for_the_retry(self):
        run = self.made_run()
        ws, checkouts = self.start(run)
        wt, pr_head = checkouts[0].wt, self.head(checkouts[0].wt)
        (wt / "edit.txt").write_text("edit\n")
        staged = pipeline.stage(checkouts, [])
        with patch.object(pipeline, "remote_head", return_value="someone-else"), \
                self.assertRaises(pipeline.RunFailed):
            pipeline.commit_and_push(checkouts, staged.changed, self.BRANCH, "work")
        committed = self.head(wt)
        self.assertNotEqual(committed, pr_head)
        pipeline.keep_edits(run)
        pipeline.note_made(ws, checkouts)
        again = self.made_run()
        _, second = self.start(again)
        pipeline.settle_starts(again, second, self.layer, self.ticket_data, self.BRANCH)
        self.assertEqual(self.head(second[0].wt), committed)
        self.assertEqual(second[0].onto, pr_head)
        self.assertEqual(pipeline.stage(second, []).files, ["edit.txt"])

    def test_failed_retention_commit_does_not_lose_the_edits_on_retry(self):
        run = self.made_run()
        ws, checkouts = self.start(run)
        wt, head = checkouts[0].wt, self.head(checkouts[0].wt)
        (wt / "edit.txt").write_text("edit\n")
        with patch.object(pipeline, "commit_kept", side_effect=RuntimeError("hook failed")):
            pipeline.keep_edits(run)
        pipeline.note_made(ws, checkouts)
        again = self.made_run()
        _, second = self.start(again)
        pipeline.settle_starts(again, second, self.layer, self.ticket_data, self.BRANCH)
        self.assertEqual((second[0].wt / "edit.txt").read_text(), "edit\n")
        self.assertEqual(second[0].onto, head)
        self.assertEqual(pipeline.stage(second, []).files, ["edit.txt"])

    def test_renewed_flow_carries_stored_steps_when_the_workspace_holds_several_worktrees(self):
        container = self.root / "container"
        for part in ("a", "b"):
            (container / part).mkdir(parents=True)
            util.git(["init", "-q", "-b", "main"], container / part)
        earlier = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        listed = [{"wt": str(container / part)} for part in ("a", "b")]
        lines.write(earlier, "step_end", step="prepare", result="pass", tree=str(container), fingerprint="",
                    data={"checkouts": listed})
        lines.write(earlier, "step_end", step="plan", result="pass", data={"plan": "p"})
        made = []
        fakes = {"prepare": lambda flow, tree: factory.Outcome("pass"),
                 "plan": lambda flow: made.append("plan") or factory.Outcome("pass")}
        with patch.dict(factory.STEPS, fakes):
            flow = factory.Flow("later", continues=earlier, renew="prepare", keeps=lambda name, kw: name == "plan")
            flow.step("prepare", tree=str(container))
            carried = flow.step("plan")
        self.assertTrue(carried.passed)
        self.assertEqual(made, [])
        self.assertTrue(any(line.get("carried") == earlier for line in lines.read("later")))

    def test_conflicting_move_with_a_failed_resolver_leaves_the_kept_commit(self):
        _, first = self.parked_edit("shared.txt", "mine\n")
        kept = self.head(first.wt)
        self.push_from_elsewhere("shared.txt", "theirs\n")
        run = self.made_run()
        _, checkouts = self.start(run)
        with patch.object(pipeline, "resolve_conflicts", side_effect=pipeline.RunFailed("resolver failed")), \
                self.assertRaises(pipeline.RunFailed):
            pipeline.settle_starts(run, checkouts, self.layer, self.ticket_data, self.BRANCH)
        self.assertEqual(self.head(checkouts[0].wt), kept)
        self.assertEqual(util.git(["status", "--short"], checkouts[0].wt).strip(), "")


class RecordRetentionTests(Isolated):
    def written(self, rid, days, size=1000):
        folder = paths.record_dir(rid)
        folder.mkdir(parents=True)
        (folder / "call.jsonl.gz").write_bytes(b"x" * size)
        (folder.parent / "run.log").write_text("log\n")
        at = time.time() - days * 86400
        os.utime(folder / "call.jsonl.gz", (at, at))
        os.utime(folder, (at, at))
        return folder

    def proposal(self, run_id, evidence):
        state.add_proposal(run_id, {"layer": "repo", "repo": "r", "file": "f", "change": "c", "why": "w",
                                    "evidence": evidence, "confidence": 0.9}, "hand")

    def test_age_removes_only_the_old_record(self):
        old, new = self.written("pip-old", 40), self.written("pip-new", 2)
        found = record.prune(30, 20)
        self.assertEqual([r.run_id for r in found.removed], ["pip-old"])
        self.assertFalse(old.exists())
        self.assertTrue((old.parent / "run.log").is_file())
        self.assertTrue(new.exists())

    def test_size_removes_oldest_first(self):
        folders = [self.written(f"pip-{n}", d) for n, d in (("a", 3), ("b", 2), ("c", 1))]
        found = record.prune(30, 1500 / record.GB)
        self.assertEqual([r.run_id for r in found.removed], ["pip-a", "pip-b"])
        self.assertEqual([f.exists() for f in folders], [False, False, True])
        self.assertFalse(found.over)

    def test_pinned_and_cited_records_stay_until_released(self):
        pinned, by_run, by_text = (self.written(n, 40) for n in ("pip-pinned", "pip-run", "pip-text"))
        record.pin("pip-pinned")
        self.proposal("pip-run", [])
        self.proposal("min-x", ["run pip-text at 10:00"])
        self.assertEqual(record.prune(30, 20).removed, ())
        record.unpin("pip-pinned")
        state.move_proposal(1, ("hand",), "rejected")
        self.assertEqual([r.run_id for r in record.prune(30, 20).removed], ["pip-pinned", "pip-run"])
        self.assertTrue(by_text.exists())
        state.move_proposal(2, ("hand",), "skipped")
        record.prune(30, 20)
        self.assertFalse(by_text.exists())
        self.assertFalse(pinned.exists() or by_run.exists())

    def test_running_and_fresh_records_stay_over_the_limit(self):
        rid = record.start("pipeline", repo="r", issue=1)
        running = self.written(rid, 40, 5000)
        fresh = self.written("pip-fresh", 0, 5000)
        found = record.prune(30, 1 / record.GB)
        self.assertEqual(found.removed, ())
        self.assertTrue(found.over)
        self.assertTrue(running.exists() and fresh.exists())

    def test_symlinked_run_folder_is_never_scanned_or_removed(self):
        target = self.written("pip-real", 40)
        (paths.RUNS_DIR / "alias").symlink_to(paths.RUNS_DIR / "pip-real", target_is_directory=True)
        self.assertEqual([r.run_id for r in record.stored()], ["pip-real"])
        record.pin("pip-real")
        self.assertEqual(record.prune(30, 20).removed, ())
        self.assertTrue(target.exists())

    def test_unreadable_record_is_left_out_of_the_scan(self):
        self.written("pip-a", 40)
        self.written("pip-b", 40)
        real = os.walk

        def walk(top, *args, onerror=None, **kwargs):
            if "pip-a" in str(top):
                onerror(PermissionError("denied"))
                return iter(())
            return real(top, *args, onerror=onerror, **kwargs)

        with patch.object(os, "walk", walk):
            self.assertEqual([r.run_id for r in record.stored()], ["pip-b"])

    def test_retention_table(self):
        self.assertEqual(config.retention_table({}), config.Retention(30, 20))
        self.assertEqual(config.retention_table({"days": 0.5}), config.Retention(0.5, 20))
        for bad in ({"days": 0}, {"max_gb": "x"}, {"days": True}, {"weeks": 1}):
            with self.assertRaises(ValueError):
                config.retention_table(bad)

    def test_pin_commands(self):
        self.written("pip-a", 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["record", "pin", "pip-a"]), 0)
            self.assertEqual(state.record_pins(), {"pip-a"})
            self.assertEqual(cli.main(["record", "pin", "nope"]), 1)
            self.assertEqual(cli.main(["record", "pin"]), 2)
            self.assertEqual(cli.main(["record", "unpin", "pip-a"]), 0)
            self.assertEqual(cli.main(["record", "unpin", "pip-a"]), 1)
            self.assertEqual(cli.main(["record", "pip-a"]), 0)

    def test_serve_prune_removes_records_and_prunes_state_once(self):
        old = self.written("pip-old", 40)
        server = serve.Server()
        with patch.object(state, "prune") as pruned:
            server.prune()
        pruned.assert_called_once_with()
        self.assertFalse(old.exists())

    def test_store_reports_size_oldest_and_pins(self):
        self.written("pip-a", 5, 1000)
        self.written("pip-b", 1, 500)
        record.pin("pip-b")
        found = record.store()
        self.assertEqual((found["bytes"], found["records"], found["pins"]), (1500, 2, 1))
        self.assertEqual(found["oldest"]["run_id"], "pip-a")
        self.assertEqual(record.size_text(1500), "1.5 KB")


class RouteABTests(Isolated):
    def configured(self, **test):
        return config.Config(repos=[self.repo], steps={"plan": {"model": "opus"}}, ab={"cheap-plan": {
            "step": "plan", "kind": "", "share": 0.5, "arm": {"model": "haiku"}, **test}})

    def sides(self, cfg, kind="feat"):
        return [steps.resolve(cfg, None, "plan", kind, ticket=("fixture", str(n))) for n in range(1, 41)]

    def refused_text(self, body):
        path = self.root / "ab.toml"
        path.write_text(body)
        with self.assertRaises(ValueError) as caught:
            config.load(path)
        return str(caught.exception)

    def test_share_decides_a_stable_side_per_ticket(self):
        cfg = self.configured()
        first, second = self.sides(cfg), self.sides(cfg)
        self.assertTrue(12 <= sum(s.ab_side == "arm" for s in first) <= 28)
        self.assertEqual([s.ab_side for s in first], [s.ab_side for s in second])
        self.assertEqual({(s.ab_side, s.model) for s in first}, {("arm", "haiku"), ("baseline", "opus")})
        self.assertEqual({s.ab_name for s in self.sides(self.configured(kind="feat"), "fix")}, {""})

    def test_config_refuses_bad_tables_with_the_reason(self):
        head = '[ab.cheap-plan]\nstep = "plan"\n'
        self.assertIn("[ab.cheap-plan]: share must be a number above 0 and at most 0.5, not 0.8",
                      self.refused_text(head + 'share = 0.8\narm = { model = "haiku" }\n'))
        self.assertIn("share must be a number", self.refused_text(head + 'share = 0\narm = { model = "haiku" }\n'))
        self.assertIn("unknown step 'nope'", self.refused_text(
            '[ab.cheap-plan]\nstep = "nope"\nshare = 0.2\narm = { model = "haiku" }\n'))
        self.assertIn("unknown key tools", self.refused_text(head + 'share = 0.2\narm = { tools = ["Read"] }\n'))
        text = self.refused_text(head + 'share = 0.2\narm = { model = "haiku", effort = "turbo" }\n')
        self.assertTrue(text.startswith("[ab.cheap-plan]:") and "turbo" in text, text)
        other = '[ab.other]\nstep = "plan"\nshare = 0.2\narm = { model = "sonnet" }\n'
        for kinds in ("", 'kind = "feat"\n'):
            self.assertIn("both change step plan", self.refused_text(
                head + 'share = 0.2\narm = { model = "haiku" }\n' + other.replace("share", kinds + "share")))
        self.assertIn("both change step plan for feat tickets", self.refused_text(
            head + 'kind = "feat"\nshare = 0.2\narm = { model = "haiku" }\n'
            + other.replace("share", 'kind = "feat"\nshare')))
        path = self.root / "ok.toml"
        path.write_text(head + 'share = 0.5\narm = { model = "haiku" }\n')
        self.assertEqual(config.load(path).ab["cheap-plan"]["share"], 0.5)

    def test_runs_record_the_side_and_a_retry_keeps_it(self):
        answers = self.root / "answers"
        answers.mkdir()
        (answers / "plan.json").write_text(json.dumps({"text": "ok", "cost_usd": 0.5}))
        cfg = self.configured()
        cfg.replay = config.ReplayConfig(all=True, dir=answers)
        tids = {side: next(str(n) for n in range(1, 99) if steps.in_arm("cheap-plan", 0.5, ("fixture", str(n)))
                           == (side == "arm")) for side in ("arm", "baseline")}

        def rows(tid):
            rid = record.start("pipeline", repo=self.repo.name, issue=tid)
            directory = paths.RUNS_DIR / rid
            directory.mkdir()
            pipeline.Run(rid, self.repo, directory, tracker=self.local, tid=tid, cfg=cfg).agent(
                "plan", "x", self.repo.path, role="plan")
            with state.db() as c:
                return [dict(r) for r in c.execute("SELECT * FROM steps WHERE run_id = ?", (rid,))]

        arm, baseline, retry = rows(tids["arm"]), rows(tids["baseline"]), rows(tids["arm"])
        self.assertEqual([(r["model"], r["ab"], r["ab_side"], r["atom"]) for r in arm],
                         [("haiku", "cheap-plan", "arm", "plan")])
        self.assertEqual([(r["model"], r["ab_side"]) for r in baseline], [("opus", "baseline")])
        self.assertEqual([(r["model"], r["ab_side"]) for r in retry], [("haiku", "arm")])

    def test_a_continuation_keeps_its_route_after_the_table_is_removed(self):
        tid = next(t for t in (state.task_add(self.repo.name, "Fixture", "Fixture", "feat") for _ in range(60))
                   if steps.in_arm("cheap-plan", 0.5, (self.repo.name, str(t))))
        models = []

        def interrupted_call(text, cwd, **kw):
            models.append(kw["model"])
            record.settle(kw["trace"][0], "interrupted")
            return Result(ok=False, error="interrupted")

        def drive(flow):
            flow.ctx.run.agent("plan", "x", self.repo.path, role="plan")
            return "failed", "fixture ended", ""

        def run_with(cfg, call):
            with patch.object(config, "load", return_value=cfg), \
                    patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                    patch.object(pipeline.runner, "run", side_effect=call):
                pipeline.run_issue(self.repo.name, tid, force=True, script=pipeline.Script("feature", drive))
            return state.ticket_runs(self.repo.name, tid, limit=1, whole=True)[0]["id"]

        first = run_with(self.configured(), interrupted_call)
        removed = self.configured()
        removed.ab = {}
        second = run_with(removed, interrupted_call)
        self.assertEqual(models, ["haiku", "haiku"])
        self.assertNotEqual(first, second)
        self.assertEqual(state.ab_held(second), state.ab_held(first))
        self.assertEqual(state.ab_held(first)["plan"][:2], ("cheap-plan", "arm"))

    def test_the_seam_envelope_carries_the_labels(self):
        found = factory.envelope([{"ab": "cheap-plan", "ab_side": "arm", "atom": "plan"}])
        self.assertEqual((found["ab"], found["ab_side"]), ("cheap-plan", "arm"))

    def seeded(self, per_side):
        for side, mode, cost, merged in (("arm", "api_key", 1.0, 15), ("baseline", "subscription", 2.0, 10)):
            for n in range(per_side):
                rid = record.start("pipeline", repo=self.repo.name, issue=f"{side}{n}")
                made = Result(ok=True, runner="claude", model="opus", cost_usd=cost, auth_mode=mode)
                record.step(rid, "plan", made, 1, try_no=1)
                record.label_step(rid, "plan", 1, atom="plan", ab="cheap-plan", ab_side=side)
                record.step(rid, "review", made, 1, try_no=1)
                record.label_step(rid, "review", 1, atom="review")
                if side == "arm" and n < 3:
                    record.step(rid, "review_fix", made, 1, try_no=1)
                    record.label_step(rid, "review_fix", 1, atom="review_repair")
                won = n < merged
                record.settle(rid, "merged" if won else "failed", pr_url="https://pr/1" if won else "")

    def test_report_counts_costs_and_interval(self):
        self.seeded(25)
        report = control.ab_report("cheap-plan", cfg=self.cfg)
        self.assertEqual(len(report), 1)
        arm, baseline = report[0]["sides"]["arm"], report[0]["sides"]["baseline"]
        self.assertEqual((arm["tickets"], arm["merged"], arm["no_pr"], arm["repairs"], arm["review_rounds"]),
                         (25, 15, 10, 3, 25))
        self.assertEqual((baseline["tickets"], baseline["merged"], baseline["repairs"]), (25, 10, 0))
        self.assertEqual((arm["paid_per_merged"], arm["subscription_per_merged"]), (round(53 / 15, 4), 0.0))
        self.assertEqual((baseline["paid_per_merged"], baseline["subscription_per_merged"]), (0.0, 10.0))
        low, high = report[0]["interval"]
        self.assertTrue(low < report[0]["merge_rate_difference"] < high)
        self.assertEqual(report[0]["merge_rate_difference"], 0.2)
        self.assertFalse(report[0]["too_few"])
        out = io.StringIO()
        with patch.object(config, "load", return_value=self.cfg), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["ab", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["sides"], report[0]["sides"])

    def test_report_flags_too_few_tickets(self):
        self.seeded(5)
        self.assertTrue(control.ab_report(cfg=self.cfg)[0]["too_few"])
        self.assertEqual(control.ab_report("nothing", cfg=self.cfg), [])


class NoLiveStateTests(unittest.TestCase):
    def test_no_test_here_points_at_the_live_authority_or_state(self):
        live = (Path.home() / ".config" / "arete", Path.home() / ".local" / "share" / "arete")
        for path in (scratch.AUTHORITY, paths.SERVER_FILE, paths.PI_KEYS_FILE, pi.AGENT_DIR, pi.LOGIN_DIR, pi.KEY_DIR,
                     pi.SESSIONS_DIR):
            self.assertFalse(any(path.resolve().is_relative_to(r.resolve()) for r in live), path)


if __name__ == "__main__":
    unittest.main()
