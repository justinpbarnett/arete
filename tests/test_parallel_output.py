import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import parallel
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

ROOT = Path(__file__).resolve().parent.parent
TAIL = 3000

SUITE = '''import os
import time
import unittest
from unittest.mock import patch


def crowd(test):
    marker = os.path.join(os.environ["PARALLEL_MARKERS"], type(test).__name__)
    if not os.path.exists(marker):
        open(marker, "w").close()
        test.fail("crowded on the first run")


class CrowdedOne(unittest.TestCase):
    def test_it(self):
        crowd(self)


class CrowdedTwo(unittest.TestCase):
    def test_it(self):
        crowd(self)


class CrowdedThree(unittest.TestCase):
    def test_it(self):
        crowd(self)


class DriveFlush(unittest.TestCase):
    def test_it(self):
        marker = os.path.join(os.environ["PARALLEL_MARKERS"], "drive-flush")
        with open(marker, "a") as values:
            print(os.environ.get("ARETE_DRIVE_FLUSH"), file=values)
        self.fail("force an alone rerun")


class Noisy(unittest.TestCase):
    def test_assertion(self):
        for number in range(2000):
            print(f"line {number:05d}")
        self.assertEqual(1, 2, "noisy class assertion")


class Sleeper(unittest.TestCase):
    class_timeout = 0.25

    def test_hangs(self):
        time.sleep(60)


class BusySolo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        marker = os.path.join(os.environ["PARALLEL_MARKERS"], cls.__name__)
        with open(marker, "a+") as attempts:
            attempts.seek(0)
            cls.attempt = len(attempts.readlines()) + 1
            print(cls.attempt, file=attempts)


def skip_cleanup():
    raise unittest.SkipTest("module cleanup skipped")


def broken_cleanup():
    raise RuntimeError("module cleanup failed")


class ModuleCleanupFailure(BusySolo):
    def test_cleanup(self):
        # unittest reports the first cleanup exception in LIFO execution order.
        unittest.addModuleCleanup(skip_cleanup)
        unittest.addModuleCleanup(broken_cleanup)


class AssertionWithReplacePatched(BusySolo):
    def test_assertion(self):
        self.enterContext(patch.object(os, "replace", side_effect=PermissionError("replacement denied")))
        self.fail("assertion while os.replace is patched")


class BusyTimeout(BusySolo):
    class_timeout = 0.3

    def test_hangs(self):
        if self.attempt <= 2:
            time.sleep(60)


class StateWriterOne(unittest.TestCase):
    def test_clean_state(self):
        state = os.environ["ARETE_STATE_DIR"]
        marker = os.path.join(state, "dirty.txt")
        self.assertFalse(os.path.exists(marker))
        with open(marker, "w") as handle:
            handle.write("from-one")


class StateWriterTwo(unittest.TestCase):
    def test_clean_state(self):
        state = os.environ["ARETE_STATE_DIR"]
        marker = os.path.join(state, "dirty.txt")
        self.assertFalse(os.path.exists(marker))
        with open(marker, "w") as handle:
            handle.write("from-two")


class BusyTimeoutThenFailure(BusySolo):
    class_timeout = 0.3

    def test_hangs_on_first_solo_attempt(self):
        if self.attempt == 1:
            self.fail("force an alone rerun")
        if self.attempt == 2:
            time.sleep(60)
        self.fail("assertion on the extra solo attempt")
'''


class RunnerOutputTests(unittest.TestCase):
    class_timeout = 60

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.suite = root / "suite"
        self.suite.mkdir()
        (self.suite / "test_sample.py").write_text(SUITE)
        (root / "markers").mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("ARETE_VERIFY_SCRATCH", "ARETE_CONFIG_DIR")}
        self.env.update(PARALLEL_MARKERS=str(root / "markers"), ARETE_STATE_DIR=str(root / "state"), TMPDIR=str(root))

    def run_suite(self, *names, loads=(0,)):
        runner = ("from types import SimpleNamespace; from tests import parallel; "
                  f"loads = iter({loads!r}); "
                  "parallel.os.cpu_count = lambda: 14; "
                  "parallel.os.getloadavg = lambda: (next(loads, 0), 0, 0); "
                  "parallel.LOAD_WAIT_BUDGET = 0; "
                  # The managed sandbox can deny removal of temporary directories.
                  "remove_reports = parallel.shutil.rmtree; "
                  "parallel.shutil = SimpleNamespace(rmtree=lambda path: remove_reports(path, ignore_errors=True)); "
                  "raise SystemExit(parallel.main())")
        done = subprocess.run([sys.executable, "-c", runner, "-s", str(self.suite), *names],
                              check=False, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=max(CHILD_HANG_TIMEOUT, self.class_timeout))
        return done.returncode, done.stdout + done.stderr

    def test_drive_flush_is_disabled_in_initial_and_alone_class_processes(self):
        self.env["ARETE_DRIVE_FLUSH"] = "1"
        code, output = self.run_suite("test_sample.DriveFlush")
        self.assertEqual(code, 1, output)
        marker = Path(self.env["PARALLEL_MARKERS"]) / "drive-flush"
        self.assertTrue(marker.is_file(), output)
        self.assertEqual(marker.read_text().splitlines(), ["0", "0"], output)

    def test_a_noisy_failing_class_keeps_its_traceback_in_the_tail(self):
        code, output = self.run_suite("test_sample.CrowdedOne", "test_sample.CrowdedTwo",
                                      "test_sample.CrowdedThree", "test_sample.Noisy")
        self.assertEqual(code, 1, output)
        tail = output[-TAIL:]
        self.assertIn("test_sample.Noisy: failures", tail)
        self.assertIn("Traceback (most recent call last):", tail)
        self.assertIn("AssertionError: 1 != 2 : noisy class assertion", tail)
        self.assertIn("Ran ", tail)
        self.assertIn("Worker reports and output retained in:", tail)
        self.assertEqual(output.count("on its first run, then passed alone"), 3)
        self.assertLess(output.rindex("on its first run"), output.index("line 01999"))
        self.assertLess(output.index("line 01999"), output.index("test_sample.Noisy: failures"))
        self.assertLess(output.index("test_sample.Noisy: failures"), output.index("Ran "))
        self.assertNotIn("line 00000", output)
        self.assertIn("1960 earlier output lines dropped", output)
        self.assertIn(".out", output.partition("1960 earlier output lines dropped")[2].partition("\n")[0])
        self.assertEqual(output.count("line 0"), 40)

    def test_a_class_that_times_out_alone_keeps_its_timeout_report_by_its_traceback(self):
        code, output = self.run_suite("test_sample.Sleeper")
        self.assertEqual(code, 1, output)
        self.assertRegex(output, r"(?m)^class exceeded (?:0\.75s|[\d.]+s \(deadline 0\.75s x load [\d.]+\))$")
        self.assertIn("test_sample.Sleeper", output)
        self.assertIn("Python class stack", output)
        self.assertIn("in test_hangs", output)
        self.assertLess(output.rindex("class exceeded"), output.index("Ran "))
        self.assertNotIn("on its first run", output)

    def test_module_cleanup_error_fails_the_suite(self):
        name = "ModuleCleanupFailure"
        identifier = f"test_sample.{name}"
        code, output = self.run_suite(identifier, loads=(0, 20, 20))
        self.assertEqual(code, 1, output)
        self.assertIn(f"{identifier}: errors\ntearDownModule (test_sample)", output)
        self.assertIn("RuntimeError: module cleanup failed", output)
        self.assertIn(f"Failed classes: {identifier}", output)
        marker = Path(self.env["PARALLEL_MARKERS"]) / name
        self.assertEqual(marker.read_text().splitlines(), ["1", "2"], output)

    def test_busy_alone_assertion_remains_visible_with_replace_patched(self):
        name = "AssertionWithReplacePatched"
        identifier = f"test_sample.{name}"
        code, output = self.run_suite(identifier, loads=(0, 20, 20))
        self.assertEqual(code, 1, output)
        self.assertIn(f"{identifier}: starting alone at host load 20", output)
        self.assertIn(f"{identifier}: failures", output)
        self.assertIn("AssertionError: assertion while os.replace is patched", output)
        self.assertNotIn("PermissionError: replacement denied", output)
        self.assertIn(f"Failed classes: {identifier}", output)
        marker = Path(self.env["PARALLEL_MARKERS"]) / name
        self.assertEqual(marker.read_text().splitlines(), ["1", "2"], output)

    def test_busy_alone_timeout_gets_one_extra_rerun_and_reports_both_loads(self):
        identifier = "test_sample.BusyTimeout"
        code, output = self.run_suite(identifier, loads=(0, 0, 20, 20, 20, 21, 21))
        self.assertEqual(code, 0, output)
        self.assertIn(f"{identifier}: ran past its deadline on its first run, "
                      "ran past its deadline alone at host load 20, then passed alone at host load 21", output)
        self.assertTrue(output.rstrip().endswith(": OK"), output)
        marker = Path(self.env["PARALLEL_MARKERS"]) / "BusyTimeout"
        self.assertEqual(marker.read_text().splitlines(), ["1", "2", "3"], output)

    def test_failed_extra_solo_attempt_keeps_the_first_solo_timeout_stack(self):
        name = "BusyTimeoutThenFailure"
        identifier = f"test_sample.{name}"
        code, output = self.run_suite(identifier, loads=(0, 20, 20, 20, 21, 21))
        self.assertEqual(code, 1, output)
        self.assertEqual(output.count(f"{identifier}: starting alone at host load"), 2, output)
        self.assertIn(f"{identifier}: starting alone at host load 20", output)
        self.assertIn(f"{identifier}: starting alone at host load 21", output)
        self.assertIn(f"{identifier}: failures", output)
        before_failure = output.partition(f"{identifier}: failures")[0]
        self.assertRegex(before_failure, r"class exceeded [\d.]+s \(deadline 0\.9s x load [\d.]+\)\nPython class stack")
        self.assertIn("in test_hangs_on_first_solo_attempt\n", before_failure)
        self.assertIn("AssertionError: assertion on the extra solo attempt", output)
        self.assertIn(f"Failed classes: {identifier}", output)
        marker = Path(self.env["PARALLEL_MARKERS"]) / name
        self.assertEqual(marker.read_text().splitlines(), ["1", "2", "3"], output)

    def test_fork_pool_isolates_state_across_reused_slots_and_captures_timeout_stacks(self):
        code, output = self.run_suite("-j", "1", "test_sample.StateWriterOne", "test_sample.StateWriterTwo")
        self.assertEqual(code, 0, output)
        self.assertIn("Ran 2 tests", output)
        code, output = self.run_suite("-j", "1", "test_sample.Sleeper", "test_sample.StateWriterOne")
        self.assertEqual(code, 1, output)
        self.assertIn("Python class stack", output)
        self.assertIn("in test_hangs", output)
        self.assertIn("Failed classes: test_sample.Sleeper", output)
        self.assertIn("Ran 1 tests", output)


def report(**details):
    return {"ran": 1, "errors": [], "failures": [], "skipped": [], "unexpected": [],
            "seconds": 0.10, "output": "", "timeout": "", **details}


class RunnerLoadTests(unittest.TestCase):
    def run_suite(self, loads, outcomes):
        events = []
        clock = 0
        loads = iter(loads)
        outcomes = {name: iter(attempts) for name, attempts in outcomes.items()}

        def getloadavg():
            load = next(loads)
            events.append(("load", load))
            return load, 0, 0

        def sleep(seconds):
            nonlocal clock
            events.append(("sleep", seconds))
            clock += seconds

        def child(item, stretch=1):
            index, batch = item
            events.append((batch.identifier, index, stretch))
            runner.results[batch.identifier] = next(outcomes[batch.identifier])

        tests = [SimpleNamespace(id=lambda name=name: f"{name}.test_it") for name in outcomes]
        with (patch.object(parallel, "open", side_effect=OSError, create=True),
              patch.object(parallel.tempfile, "mkdtemp", return_value="worker-reports"),
              patch.object(parallel.shutil, "rmtree"),
              patch.object(parallel.os, "cpu_count", return_value=14),
              patch.object(parallel.os, "getloadavg", side_effect=getloadavg),
              patch.object(parallel, "time", SimpleNamespace(monotonic=lambda: clock, sleep=sleep)),
              redirect_stdout(StringIO()) as output):
            runner = parallel.Runner(SimpleNamespace(jobs=1, slowest=0))
            with patch.object(runner, "child", side_effect=child):
                code = runner.run(tests)
        return code, output.getvalue(), events

    def test_rerun_starts_after_the_third_load_check_and_logs_the_wait(self):
        code, output, events = self.run_suite([18, 16, 13], {
            "First": [report(timeout="initial timeout"), report()],
        })
        self.assertEqual(code, 0, output)
        self.assertEqual(events, [("First", 0, 1), ("load", 18), ("sleep", 5), ("load", 16),
                                  ("sleep", 5), ("load", 13), ("First", "0.alone", 3)])
        self.assertEqual(output.splitlines()[:2], [
            "First: waiting for host load to fall from 18 to 14 or less",
            "First: starting alone at host load 13 after waiting 10.00s",
        ])

    def test_wait_budget_is_shared_by_all_classes_and_both_alone_attempts(self):
        timeout = report(ran=0, timeout="class exceeded its deadline")
        code, output, events = self.run_suite([18, 14, *([20] * 60), 21, 22], {
            "First": [timeout, report()],
            "Second": [timeout, timeout, report()],
            "Third": [timeout, report()],
        })
        self.assertEqual(code, 0, output)
        self.assertEqual(events.count(("sleep", 5)), 60)
        self.assertEqual(output.count("waiting for host load to fall"), 2)
        self.assertIn("First: starting alone at host load 14 after waiting 5.00s", output)
        self.assertIn("Second: starting alone at host load 20 after waiting 295.00s", output)
        self.assertEqual(events[-6:], [("load", 20), ("Second", "1.alone", 3),
                                       ("load", 21), ("Second", "1.alone-retry", 3),
                                       ("load", 22), ("Third", "2.alone", 3)])
        self.assertIn("Second: ran past its deadline on its first run, ran past its deadline alone at host load 20, "
                      "then passed alone at host load 21 within three times its deadline", output)
        self.assertTrue(output.rstrip().endswith(": OK"), output)

    def test_quiet_timeout_stays_failed_after_one_alone_run(self):
        timeout = report(ran=0, timeout="class exceeded its deadline")
        code, output, events = self.run_suite([14], {"First": [timeout, timeout]})
        self.assertEqual(code, 1, output)
        self.assertEqual(events, [("First", 0, 1), ("load", 14), ("First", "0.alone", 3)])
        self.assertIn("Failed classes: First", output)

    def test_only_over_capacity_timeouts_get_one_extra_rerun(self):
        timeout = report(ran=0, timeout="class exceeded its deadline")
        assertion = report(failures=[("test_it", "AssertionError: failed")])
        error = report(errors=[("test_it", "RuntimeError: failed")])
        code, output, events = self.run_suite([20] * 64, {
            "TimedOut": [timeout, timeout, timeout],
            "Assertion": [timeout, assertion],
            "Error": [timeout, error],
        })
        self.assertEqual(code, 1, output)
        self.assertEqual([event for event in events if len(event) == 3 and event[2] == 3], [
            ("TimedOut", "0.alone", 3), ("TimedOut", "0.alone-retry", 3),
            ("Assertion", "1.alone", 3), ("Error", "2.alone", 3),
        ])
        self.assertIn("Failed classes: Assertion, Error, TimedOut", output)


class RunnerFailureSummaryTests(unittest.TestCase):
    crowded = "test_sample.Crowded"
    crowded_stack = "in test_crowded_timeout"
    crowded_note = (f"{crowded}: ran past its deadline on its first run, "
                    "then passed alone within three times its deadline in 0.10s")

    def finish_suite(self, failures, slowest=0):
        with (patch.object(parallel, "open", side_effect=OSError, create=True),
              patch.object(parallel.shutil, "rmtree"), redirect_stdout(StringIO()) as output):
            runner = parallel.Runner(SimpleNamespace(slowest=slowest))
            runner.folder = "worker-reports"
            runner.results = {self.crowded: report(), **failures}
            runner.crowded = [self.crowded]
            runner.first_failures[self.crowded] = report(timeout=f"Python class stack\n{self.crowded_stack}")
            code = runner.finish(0.25)
        return code, output.getvalue()

    def test_tail_names_every_failed_class_after_the_reports(self):
        code, output = self.finish_suite({
            "test_sample.TimedOut": report(ran=0, timeout="class exceeded its retry deadline"),
            "test_sample.Noisy": report(failures=[("test_assertion", "AssertionError: " + "x" * 2000)]),
        }, slowest=1)
        self.assertEqual(code, 1, output)
        tail = output[-1500:]
        self.assertIn("Failed classes: test_sample.Noisy, test_sample.TimedOut\nRan 2 tests", tail)
        self.assertEqual(output.count("Failed classes:"), 1)
        self.assertNotIn(self.crowded_stack, output)

    def test_crowded_only_suite_ends_ok_with_a_one_line_note(self):
        code, output = self.finish_suite({})
        self.assertEqual(code, 0, output)
        self.assertEqual(output.splitlines(), [self.crowded_note, "Ran 1 tests in 0.25s: OK"])


if __name__ == "__main__":
    unittest.main()
