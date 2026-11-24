import json
import subprocess
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arete import config, dbstats, factory, flows, paths, pipeline, record, run_lines, scrub, slots, tracker, util
from arete.store import db
from arete.store import orchestration as ledger
from arete.store import runs, ticket_progress
from tests.support import workspace_state

REPO = "fixture"
TICKET = "964"
FAIL = "exit 7"


def shell(command, _cfg, _repo, *, cwd, timeout, **_options):
    return subprocess.run(["/bin/sh", "-c", command], cwd=cwd, timeout=timeout, capture_output=True, text=True)


class CheckProgressTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory(prefix="arete-check-progress-"))
        self.root = Path(folder).resolve()
        self.enterContext(workspace_state(self.root))
        self.enterContext(patch.object(paths, "CHECK_LOCKS_DIR", self.root / "check-locks"))
        self.enterContext(patch.object(dbstats.Timing, "finished"))
        self.enterContext(patch.object(scrub, "secrets", return_value=()))
        self.enterContext(patch.object(factory, "fingerprint", return_value="fixture-tree"))
        self.enterContext(patch.object(pipeline, "stage", return_value=pipeline.Staged(counts={"file.py": 1})))
        self.enterContext(patch.object(pipeline.environment, "sh", side_effect=shell))
        self.enterContext(redirect_stdout(StringIO()))
        db.migrate()
        self.repo = config.Repo(REPO, self.root)
        self.cfg = config.Config(repos=[self.repo], max_parallel_checks=1)

    def job(self, rid, *, path="", layer=None, ticket=TICKET, dry_run=False, only=""):
        runs.insert_run(rid, {"kind": "pipeline", "repo": REPO, "issue": ticket, "status": "running",
                              "started": util.iso(), "dry_run": int(dry_run), "only_step": only})
        folder = paths.RUNS_DIR / rid
        folder.mkdir(parents=True)
        run = pipeline.Run(rid, self.repo, folder, cfg=self.cfg, tracker=tracker.GitHubTracker(self.repo),
                           tid=ticket, path=path, dry_run=dry_run, alone=bool(only))
        layer = layer or config.RepoLayer(self.root, checks={"unit": FAIL})
        issue = tracker.Ticket(ticket, f"#{ticket}", "Fixture")
        build = pipeline.Build(self.cfg, run, issue, self.root, [], layer, 2, "fixture workspace", tools=[], deny=[])
        return flows.Job(self.cfg, run, self.root, ticket=issue, layer=layer, built=build)

    def progress(self, ticket=TICKET):
        with db.reader() as c:
            rows = [json.loads(row["data"]) for row in ticket_progress.rows(c, REPO, ticket)]
        return ([entry for row in reversed(rows) for entry in row["evidence"]],
                [update for row in reversed(rows) for update in row["updates"]])

    def checks(self, run):
        return [row for row in record.lines(paths.record_dir(run.id) / record.TIMELINE) if row["kind"] == "check"]

    def test_classic_repair_reports_only_real_command_failures_and_deduplicates_evidence(self):
        layer = config.RepoLayer(self.root, checks={"unit": FAIL,
                                                   "stdout": "echo '### made-up check failed: exit 9'",
                                                   "slow": "exit 8"}, quick_checks=("unit", "stdout"))
        job = self.job("pip-classic", layer=layer)
        with (patch.object(pipeline, "check_lane", return_value=nullcontext(())),
              patch.object(job.run, "settings", return_value=SimpleNamespace(tries=2)),
              patch.object(job.b, "resume") as repair):
            self.assertEqual(job.b.check_with_repairs(), (False, 2))
            repair.assert_called_once()
        marks = self.checks(job.run)
        self.assertEqual([(row["name"], row["exit"]) for row in marks],
                         [("unit", 7), ("stdout", 0), ("unit", 7), ("stdout", 0)])
        first, second = marks[0]["suite"], marks[2]["suite"]
        self.assertNotEqual(first, second)
        self.assertEqual([row["suite"] for row in marks], [first, first, second, second])
        cause = "check:" + ledger.digest(["unit", FAIL, 7])
        expected = ([[cause, first], [cause, second]], [{
            "kind": "recurring-blocker", "cause": cause, "attempt": second,
            "message": "Check unit failed again (exit 7).",
        }])
        self.assertEqual(self.progress(), expected)
        pipeline.report_check_failures(job.run, second, [("unit", FAIL, 7)])
        self.assertEqual(self.progress(), expected)

    def test_feature_and_patch_resume_reuses_stored_failure_not_a_new_attempt(self):
        for path, quick, ticket in (("feature", False, "feature-964"), ("patch", True, "patch-964")):
            with self.subTest(path=path), patch.object(pipeline, "check_lane", return_value=nullcontext(())):
                ready = self.root / "ready"
                layer = config.RepoLayer(self.root, checks={"unit": "test -f ready"}, quick_checks=("unit",))
                job = self.job(f"pip-{path}", path=path, ticket=ticket, layer=layer)
                flow = factory.Flow(job.run.id, job)
                args = {"tree": str(self.root), "quick": quick}
                self.assertFalse(flows.step(flow, "checks", **args).passed)
                ready.touch()
                self.assertTrue(flows.step(flow, "checks", **args).passed)
                ready.unlink()
                self.assertEqual(self.progress(ticket)[1], [])

                resumed = self.job(f"pip-{path}-resume", path=path, ticket=ticket, layer=layer)
                flow = factory.Flow(resumed.run.id, resumed, continues=job.run.id, again="checks")
                with patch.object(pipeline.environment, "sh", wraps=shell) as command:
                    self.assertFalse(flows.step(flow, "checks", **args).passed)
                    command.assert_not_called()
                    self.assertFalse(flows.step(flow, "checks", **args).passed)
                    command.assert_called_once()
                mark, = self.checks(resumed.run)
                self.assertEqual((mark["step"], mark["try"]), ("checks", 3))
                evidence, updates = self.progress(ticket)
                self.assertEqual(len(evidence), 2)
                update, = updates
                self.assertEqual(update["attempt"], mark["suite"])

    def test_action_ledger_and_pipeline_do_not_both_report_the_same_failure(self):
        legacy = self.job("pip-legacy")
        adaptive = self.job("pip-adaptive", path="orchestrate")
        lease = ledger.acquire(adaptive.run.id, "", {})
        version = ledger.snapshot(lease)["version"]
        turn = ledger.begin(lease, {}, None)
        action, = ledger.accept(lease, turn, version,
                               {"calls": [{"op": "checks.run", "args": {"scope": "full"}}]}, None)
        with patch.object(pipeline, "check_lane", return_value=nullcontext(())):
            self.assertFalse(legacy.b.check())
            ledger.transition(lease, action["id"], "running", {})
            outcome = factory.Flow(adaptive.run.id, adaptive).run("checks", (), {"tree": str(self.root)})
            self.assertFalse(outcome.passed)
            self.assertEqual(self.progress()[1], [])
            ledger.transition(lease, action["id"], "succeeded", {"passed": outcome.passed})
        evidence, updates = self.progress()
        update, = updates
        self.assertEqual(len(evidence), 2)
        self.assertEqual(update["attempt"], f"{lease.root}/{action['id']}")

    def test_dry_and_single_step_runs_and_checks_without_an_exit_do_not_report(self):
        with patch.object(pipeline, "check_lane", return_value=nullcontext(())):
            for job in (self.job("pip-dry", dry_run=True), self.job("pip-single", only="checks")):
                self.assertFalse(job.b.check())
            job = self.job("pip-no-exit")
            with patch.object(pipeline.environment, "sh", side_effect=OSError("not launched")):
                with self.assertRaisesRegex(OSError, "not launched"):
                    job.b.check()
        mark, = self.checks(job.run)
        self.assertNotIn("exit", mark)
        self.assertEqual(self.progress(), ([], []))

    def test_contended_slot_and_full_suite_lock_pair_to_the_same_suite(self):
        job = self.job("pip-contention", path="patch")
        job.layer.checks["unit"] = "true"
        with paths.directory_fd(paths.CHECK_LOCKS_DIR, create=True) as directory:
            admission = slots.check_lock_file(directory, slots.CHECK_SLOTS_LOCK)

        def release(lock):
            self.assertFalse(lock.closed, "check still blocked after its holder released the lock")
            lock.close()

        with admission, util.lock_file("checks") as worker:
            self.assertTrue(util.flock_now(admission))
            self.assertTrue(util.flock_now(worker))
            with (patch.object(slots, "time", SimpleNamespace(sleep=lambda _: release(admission))),
                  patch.object(pipeline, "time", SimpleNamespace(sleep=lambda _: release(worker)))):
                outcome = flows.step(factory.Flow(job.run.id, job), "checks", tree=str(self.root))
        self.assertTrue(outcome.passed)
        self.assertTrue(job.b.check())
        waited, clear = self.checks(job.run)
        self.assertNotEqual(waited["suite"], clear["suite"])
        waits = [line for line in run_lines.read(job.run.id) if line["event"] == "wait"]
        self.assertEqual([(row["resource"], row["phase"]) for row in waits],
                         [("check-slot", "start"), ("check-slot", "end"),
                          ("full-suite-lock", "start"), ("full-suite-lock", "end")])
        for start, end in (waits[:2], waits[2:]):
            self.assertEqual((start["cause"], end["cause"], end["since"]), ("slot", "slot", start["at"]))
            for row in (start, end):
                self.assertEqual([row[k] for k in ("suite", "step", "try")],
                                 [waited[k] for k in ("suite", "step", "try")])

    def test_stopping_during_full_suite_lock_wait_closes_wait_without_check_evidence(self):
        job = self.job("pip-stop")
        with (util.lock_file("checks") as worker,
              patch.object(pipeline.time, "sleep", side_effect=pipeline.Stopped)):
            self.assertTrue(util.flock_now(worker))
            with self.assertRaises(pipeline.Stopped):
                job.b.check()
        start, end = [line for line in run_lines.read(job.run.id) if line["event"] == "wait"]
        self.assertEqual((start["phase"], end["phase"], end["since"]), ("start", "end", start["at"]))
        self.assertEqual((start["resource"], start["suite"]), ("full-suite-lock", end["suite"]))
        self.assertEqual(end["resource"], start["resource"])
        self.assertEqual(self.checks(job.run), [])
        self.assertEqual(self.progress(), ([], []))


if __name__ == "__main__":
    unittest.main()
