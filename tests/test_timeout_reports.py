import contextlib
import errno
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arete import ci, config, pipeline, record
from tests import parallel

GUIDANCE = (
    "Move the tests this change adds into their own TestCase class, or split the class, "
    "so each class finishes alone well within its deadline. Raise class_timeout only for a class "
    "whose tests are slow on purpose."
)
SUMMARY_ENV = "ARETE_TEST_TIMEOUT_SUMMARIES"
HANG_READY = "timeout fixture reached its hang"
POPEN = subprocess.Popen


def summary_for(name, deadline=10):
    return (f"{name} (3 tests)\nclass exceeded {deadline}s\n2 timing rows, 0 never started\n"
            f"Slowest recorded tests:\n  0.010s {name}.test_fast\n  0.001s {name}.test_faster\n{GUIDANCE}")


def details_for(name):
    return {"class_name": name, "count": 3, "deadline": 10, "never_started": 0,
            "timings": [(0.010, f"{name}.test_fast"), (0.001, f"{name}.test_faster")]}


def timeout_output(name, stack=""):
    return "\n".join(filter(None, ("=== Test class timeout ===", name, "class exceeded 10s", stack,
                                  "=== End test class timeout ===")))


def summaries_in(path):
    with path.open("rb") as sidecar:
        return pipeline.read_timeout_summaries(sidecar, 3, str)


def worker_timing(reader, writer, lifeline):
    return (["--progress-fd", str(writer.fileno()), "--progress-peer-fd", str(reader.fileno())],
            (lifeline, writer.fileno(), reader.fileno()))


def start_hanging_worker(*args, stdout, **kwargs):
    process = POPEN(*args, stdout=stdout, **kwargs)
    deadline = time.monotonic() + 2
    with open(stdout.name) as output:
        while process.poll() is None and time.monotonic() < deadline:
            output.seek(0)
            if HANG_READY in output.read():
                break
            time.sleep(0.005)
    return process


class TimeoutStackRetentionTests(unittest.TestCase):
    def test_timeout_preserves_stacks_and_keeps_generated_ids_out_of_retained_artifacts(self):
        marker = "synthetic-completed-test-id"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            sidecar = root / "summaries.jsonl"
            sidecar.touch()
            (root / "test_hang.py").write_text(
                "import sys\nimport time\nimport unittest\n\n"
                "class HangingTests(unittest.TestCase):\n"
                "    def id(self):\n"
                "        name = super().id()\n"
                "        if '--one' in sys.argv and self._testMethodName == 'test_1_fast':\n"
                f"            return name + '[{marker}]'\n"
                "        return name\n"
                "    def test_1_fast(self): pass\n"
                "    def test_2_slower(self): time.sleep(0.025)\n"
                "    def test_3_hang(self):\n"
                f"        print({HANG_READY!r}, flush=True)\n"
                "        time.sleep(20)\n"
            )
            runner = (
                "from tests import parallel; from tests.test_timeout_reports import start_hanging_worker; "
                "parallel.STACK_GRACE = 0.3; parallel.subprocess.Popen = start_hanging_worker; "
                "raise SystemExit(parallel.main())"
            )
            result = subprocess.run(
                [sys.executable, "-c", runner, "--start", directory, "--timeout", "0.25"],
                env={**os.environ, "TMPDIR": directory, SUMMARY_ENV: str(sidecar)},
                capture_output=True, text=True, timeout=8,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertTrue(sidecar.exists(), result.stdout)
            summaries = summaries_in(sidecar)
            retained = Path(result.stdout.rsplit("Worker reports and output retained in: ", 1)[1].strip())
            self.assertEqual(retained.parent, root)
            artifacts = list(retained.rglob("*"))
            self.assertTrue(any(path.suffix == ".out" for path in artifacts))
            self.assertFalse(any(path.suffix == ".progress" for path in artifacts))
            for path in artifacts:
                self.assertNotIn(marker, path.name)
                if path.is_file():
                    self.assertNotIn(marker.encode(), path.read_bytes(), path.name)
            self.assertNotIn(marker, result.stdout)

        name = "test_hang.HangingTests"
        dumps = ci.TEST_TIMEOUT.findall(result.stdout)
        self.assertEqual(len(dumps), 2, result.stdout)
        for dump, deadline in zip(dumps, ("0.25s", "0.5s")):
            self.assertEqual(dump.splitlines()[1:3], [name, f"class exceeded {deadline}"])
            self.assertRegex(dump.splitlines()[3], r"^Python class stack \(PID \d+\)$")
            self.assertIn("in test_3_hang", dump)
        self.assertNotIn(GUIDANCE, result.stdout)
        self.assertNotIn("Slowest recorded tests:", result.stdout)
        self.assertEqual(len(summaries), 1)
        lines = summaries[0].splitlines()
        self.assertEqual(lines[:4], [f"{name} (3 tests)", "class exceeded 0.5s", "2 timing rows, 0 never started",
                                     "Slowest recorded tests:"])
        self.assertCountEqual([row.split()[-1] for row in lines[4:6]],
                              [f"{name}.test_1_fast", f"{name}.test_2_slower"])
        for row in lines[4:6]:
            self.assertRegex(row, r"^  \d+\.\d{3}s ")
        self.assertEqual(lines[6:], [GUIDANCE])


class ParallelTimeoutReportTests(unittest.TestCase):
    def test_worker_writes_the_same_result_when_id_raises_after_teardown(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "test_teardown.py").write_text(
                "import unittest\n\n"
                "class TeardownTests(unittest.TestCase):\n"
                "    def id(self):\n"
                "        if getattr(self, 'done', False):\n"
                "            raise RuntimeError('id called after teardown')\n"
                "        return super().id()\n"
                "    def tearDown(self): self.done = True\n"
                "    def test_pass(self): pass\n"
                "    def test_failure(self): self.fail('original assertion')\n"
            )
            lifeline, held = os.pipe()
            reports = []
            try:
                with parallel.timing_channel() as (reader, writer):
                    command = [sys.executable, "-m", "tests.parallel", "--one", "test_teardown.TeardownTests",
                               "--start", directory, "--lifeline", str(lifeline)]
                    for enabled in (False, True):
                        path = root / f"result-{enabled}.json"
                        timing, fds = worker_timing(reader, writer, lifeline) if enabled else ([], (lifeline,))
                        result = subprocess.run([*command, "--result", str(path), *timing], pass_fds=fds,
                                                capture_output=True, text=True, timeout=2)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        reports.append(path.read_bytes())
                    events = sorted(parallel.timing_rows(reader))
            finally:
                os.close(lifeline)
                os.close(held)
            self.assertEqual(reports[1], reports[0])
            report = json.loads(reports[1])
            self.assertEqual(report["ran"], 2)
            self.assertEqual(report["errors"], [])
            self.assertIn("AssertionError: original assertion", report["failures"][0][1])
            self.assertEqual([name for _, name, _ in events if name],
                             ["test_teardown.TeardownTests.test_failure", "test_teardown.TeardownTests.test_pass"])

    def test_passing_class_keeps_the_runner_output(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            sidecar = Path(directory) / "summaries.jsonl"
            sidecar.touch()
            with (patch.dict(os.environ, {SUMMARY_ENV: str(sidecar)}),
                  patch.object(parallel, "HISTORY", str(Path(directory) / "history.json")),
                  patch.object(parallel.shutil, "rmtree") as cleanup,
                  contextlib.redirect_stdout(io.StringIO()) as output):
                runner = parallel.Runner(SimpleNamespace(slowest=0))
                runner.folder = directory
                runner.results["fixture.Passing"] = {"ran": 2, "skipped": [], "seconds": 0.25}
                self.assertEqual(runner.finish(1), 0)
                cleanup.assert_called_once_with(directory)
            self.assertEqual(output.getvalue(), "Ran 2 tests in 1.00s: OK\n")
            self.assertEqual(sidecar.read_bytes(), b"")

    def test_deadline_excludes_grace_period_and_keeps_only_five_timings(self):
        name = "test_timeout_reports.Sample"
        batch = parallel.Batch(name, 8)
        durations = (2, 5, 1, 7, 4, 3, 20, 1)
        events = [timestamp for n, seconds in enumerate(durations) for timestamp in (10 * n, 10 * n + seconds)]
        events[-2:] = [81, 82]
        rows = []
        result = parallel.TimedResult(lambda *row: rows.append(row))
        clock = Mock(monotonic=Mock(side_effect=events))
        with patch.object(parallel, "time", clock):
            for index in range(batch.count):
                test = unittest.TestCase()
                test._testMethodName = f"test_{index}"
                result.startTest(test)
                result.stopTest(test)
        details = parallel.class_timeout_details(batch, 75, 75, rows, started=7)
        summary = pipeline.format_timeout_summary(details, str)

        self.assertEqual(summary.splitlines()[2:-1], ["5 timing rows, 1 never started", "Slowest recorded tests:",
                                                   "  7.000s unittest.case.TestCase.test_3",
                                                   "  5.000s unittest.case.TestCase.test_1",
                                                   "  4.000s unittest.case.TestCase.test_4",
                                                   "  3.000s unittest.case.TestCase.test_5",
                                                   "  2.000s unittest.case.TestCase.test_0"])
        long_name = "fixture." + "long_module." * 80 + "LongTests"
        summary = pipeline.format_timeout_summary({**details, "class_name": long_name}, str)
        self.assertLessEqual(len(summary), 800)
        self.assertIn("LongTests (8 tests)\nclass exceeded 75s", summary)
        self.assertTrue(summary.endswith("\n" + GUIDANCE))

    def test_unavailable_clock_leaves_the_test_result_in_place(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                self.fail("original result")

        start = Mock(side_effect=OSError("unavailable start time"))
        stop = Mock(side_effect=[0, OSError("unavailable finish time")])
        for clock in (start, stop):
            with self.subTest(clock=clock):
                events = []
                result = parallel.TimedResult(lambda *row: events.append(row))
                with patch.object(parallel, "time", Mock(monotonic=clock)):
                    Sample().run(result)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(len(result.failures), 1)
                self.assertIn("AssertionError: original result", result.failures[0][1])
                details = parallel.class_timeout_details(parallel.Batch("Sample", 1), 1, 1, events, result.testsRun)
                self.assertEqual(details["timings"], [])

    def test_crowded_timeout_only_prints_the_note_beside_a_failed_retry(self):
        class Recovered(unittest.TestCase):
            def test_case(self):
                pass

        class Failed(unittest.TestCase):
            def test_case(self):
                pass

        recovered, failed = (case().id().rsplit(".", 1)[0] for case in (Recovered, Failed))
        stacks = {name: timeout_output(name) for name in (recovered, failed)}
        assertion = "AssertionError: retry failed"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            sidecar = root / "summaries.jsonl"
            for path in (None, sidecar):
                with (patch.dict(os.environ), patch.object(parallel, "HISTORY", str(root / "history.json")),
                      patch.object(parallel.tempfile, "mkdtemp", return_value=directory),
                      contextlib.redirect_stdout(io.StringIO()) as output):
                    os.environ.pop(SUMMARY_ENV, None)
                    if path:
                        path.touch()
                        os.environ[SUMMARY_ENV] = str(path)
                    runner = parallel.Runner(SimpleNamespace(jobs=1, slowest=0))

                    def child(item, stretch=1):
                        _, batch = item
                        name = batch.identifier
                        first = stretch == 1
                        runner.results[name] = dict(
                            ran=0 if first else 1, errors=[], skipped=[], unexpected=[], output="",
                            failures=[(f"{name}.test_case", assertion)] if not first and name == failed else [],
                            seconds=0.25, timeout=stacks[name] if first else "",
                            timeout_details=details_for(name) if first else None,
                        )

                    finish = runner.finish
                    with (patch.object(runner, "child", side_effect=child) as run_child,
                          patch.object(runner, "finish", side_effect=lambda _: finish(1))):
                        self.assertEqual(runner.run([Recovered("test_case"), Failed("test_case")]), 1)
                    self.assertEqual([call.kwargs.get("stretch", 1) for call in run_child.call_args_list], [1, 1, 2, 2])
                baseline = (
                    f"\n{stacks[failed]}\n\n{failed}: failures\n{failed}.test_case\n{assertion}\n"
                    f"{recovered}: ran past its deadline on its first run, then passed alone within twice its deadline in 0.25s\n"
                    f"Ran 2 tests in 1.00s: FAILED\nWorker reports and output retained in: {directory}\n"
                )
                self.assertEqual(output.getvalue().encode(), baseline.encode())
                if path:
                    self.assertEqual(summaries_in(path), [summary_for(failed)])
                else:
                    self.assertFalse(sidecar.exists())
                    self.assertEqual(set(root.iterdir()), {root / "history.json"})

    def run_redacted_check(self, details, value):
        layer = config.RepoLayer(Path.cwd(), checks={"suite": "run suite"}, quick_checks=["suite"],
                                 parallel_checks=True)
        run = Mock(config=config.Config())
        stored = Mock(return_value={"FIXTURE_VALUE": value})
        redact = pipeline.vault.hidden

        def positional_redactor(text, given, /):
            return redact(text, given)

        def sh(cmd, *, env, **_kw):
            self.assertEqual(env["FIXTURE_VALUE"], value)
            Path(env[SUMMARY_ENV]).write_text(json.dumps(details) + "\n")
            stored.return_value = {}
            return subprocess.CompletedProcess(cmd, 1, f"failure in {value}\n", f"stderr in {value}")

        with (tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
              patch.object(pipeline.environment, "stored", stored),
              patch.object(pipeline.environment, "passed", return_value=[]),
              patch.object(pipeline.vault, "hidden", side_effect=positional_redactor),
              patch.object(pipeline.util, "sh", side_effect=sh),
              patch.object(record, "timed", return_value=contextlib.nullcontext({}))):
            run.dir = Path(directory)
            ok, report = pipeline.run_checks(layer, Path.cwd(), [], run, 30)

        self.assertFalse(ok)
        baseline = "### suite failed: `run suite` exit 1\n```\nfailure in [redacted]\nstderr in [redacted]\n```"
        self.assertEqual(report.baseline, baseline)
        self.assertEqual(len(report.summaries), 1)
        return report

    def test_timeout_report_redacts_values_removed_during_the_check(self):
        value = "synthetic-timeout-value"
        name = "fixture.Hanging"
        details = details_for(name)
        details["timings"][0] = (0.010, f"{name}.test_{value}")
        report = self.run_redacted_check(details, value)
        summary = summary_for(name).replace(".test_fast\n", ".test_[redacted]\n")
        expected = f"{report.baseline}\n\n{summary}"
        self.assertEqual(report, expected)
        self.assertEqual(pipeline.check_report_tail(report, 2500), expected)

    def test_redacts_an_intact_value_that_would_be_split_by_clipping(self):
        value = "synthetic-start-" + "v" * 900 + "-synthetic-end"
        name = "fixture.Hanging"
        details = details_for(name)
        details["timings"][0] = (0.010, f"{name}.test_before_{value}_after")
        report = self.run_redacted_check(details, value)
        expected = summary_for(name).replace(".test_fast\n", ".test_before_[redacted]_after\n")
        self.assertEqual(report.summaries, (expected,))
        self.assertNotIn("synthetic-start-", report)
        self.assertNotIn("-synthetic-end", report)

    def test_redaction_growth_shortens_timings_and_keeps_the_summary(self):
        value = "MASKTHIS"
        name = "fixture.Hanging"
        details = details_for(name)
        repeated = value * 30
        prefix = "p" * (800 - len(summary_for(name)) + len("fast") - len(repeated))
        test_name = f"{name}.test_{prefix}{repeated}"
        self.assertEqual(len(summary_for(name).replace(f"{name}.test_fast\n", test_name + "\n")), 800)
        details["timings"][0] = (0.010, test_name)
        report = self.run_redacted_check(details, value)
        summary, = report.summaries
        self.assertEqual(len(summary), 800)
        self.assertIn(f"{name} (3 tests)\nclass exceeded 10s\n2 timing rows, 0 never started", summary)
        self.assertIn("  0.010s test_", summary)
        self.assertIn("[redacted]", summary)
        self.assertNotIn(value, report)
        self.assertTrue(summary.endswith("\n" + GUIDANCE))

    def run_checks(self, outputs, *, slow=()):
        commands = {name: f"run {name}" for name in (*outputs, *slow)}
        layer = config.RepoLayer(Path.cwd(), checks=commands, quick_checks=list(outputs), parallel_checks=True)
        paths = []

        def sh(cmd, *, env, **_kw):
            path = Path(env[SUMMARY_ENV])
            self.assertEqual(path.parent, run_dir)
            self.assertTrue(path.is_file())
            paths.append(path)
            stdout, stderr, summaries = outputs[cmd.removeprefix("run ")]
            path.write_text("".join(json.dumps(summary) + "\n" for summary in summaries))
            return SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)

        with (tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
              patch.object(pipeline.util, "sh", side_effect=sh),
              patch.object(record, "timed", side_effect=lambda *_, **__: contextlib.nullcontext({}))):
            run_dir = Path(directory)
            run = Mock(config=config.Config(), repo=None, dir=run_dir)
            ok, report = pipeline.run_checks(layer, Path.cwd(), [], run, 30)
            self.assertFalse(ok)
            self.assertEqual(len(set(paths)), len(outputs))
            self.assertFalse(any(path.exists() for path in paths))
        return report

    def test_two_checks_append_at_most_three_summaries_after_the_full_report(self):
        first = [summary_for(f"fixture.First{n}") for n in (1, 2)]
        second = [summary_for(f"fixture.Second{n}") for n in (1, 2)]
        outputs = {"first": ("first failure\n", "", [details_for(f"fixture.First{n}") for n in (1, 2)]),
                   "second": ("", "second failure", [details_for(f"fixture.Second{n}") for n in (1, 2)])}
        report = self.run_checks(outputs, slow=("later",))
        baseline = ("### first failed: `run first` exit 1\n```\nfirst failure\n\n```\n\n"
                    "### second failed: `run second` exit 1\n```\nsecond failure\n```\n\n"
                    "Not run because cheap checks failed: later")
        self.assertEqual(report, "\n\n".join((baseline, *first, second[0])))
        limit = len(baseline) + 2 + len(first[0])
        self.assertEqual(pipeline.check_report_tail(report, limit), f"{baseline}\n\n{first[0]}")
        self.assertEqual(pipeline.check_report_tail(report, len(baseline)), baseline)

    def test_assertion_quoting_a_runner_report_and_timings_is_preserved_verbatim(self):
        quoted = "\n".join((
            "\nfixture.Quoted: failures", "fixture.Quoted.test_case", "AssertionError: original error",
            "Slowest classes:", "  1.00s fixture.Quoted (2 tests)", "Ran 2 tests in 1.00s: FAILED",
            "Worker reports and output retained in: /tmp/quoted", "=== Test class timeout summary ===",
            summary_for("fixture.Quoted"), "",
        ))
        stdout = "\nfixture.Real: failures\nfixture.Real.test_retry\nAssertionError: captured output differs\n" + quoted
        stderr = "stderr\r\n"
        actual = summary_for("fixture.Real")
        baseline = f"### suite failed: `run suite` exit 1\n```\n{stdout}{stderr}\n```"
        for sidecar in ([], [details_for("fixture.Real")]):
            with self.subTest(sidecar=bool(sidecar)):
                report = self.run_checks({"suite": (stdout, stderr, sidecar)})
                expected = f"{baseline}\n\n{actual}" if sidecar else baseline
                self.assertEqual(report.encode(), expected.encode())

    def test_report_budgets_preserve_main_output_including_the_retry_traceback(self):
        name = "fixture.Hanging"
        frame = '  File "fixture.py", line 42 in test_hang\n'
        stack = ("Python class stack (PID 42)\n" + frame * 200)[:7244]
        traceback = ('Traceback (most recent call last):\n  File "fixture.py", line 8 in test_retry\n'
                     f"    self.fail(value)\nAssertionError: {'é' * 150}")
        output = (f"\n{timeout_output(name, stack)}\n\n{name}: failures\n{name}.test_retry\n{traceback}\n"
                  "Ran 1 tests in 1.00s: FAILED\nWorker reports and output retained in: /tmp/reports\n")
        summary = summary_for(name)
        report = self.run_checks({"suite": (output, "", [details_for(name)])})
        baseline = f"### suite failed: `run suite` exit 1\n```\n...\n{output[-6000:]}\n```"
        self.assertEqual(report.encode(), f"{baseline}\n\n{summary}".encode())
        tail = "...\n" + baseline[-2500:]
        self.assertIn(traceback, tail)
        self.assertEqual(pipeline.check_report_tail(report, 2500).encode(), tail.encode())

        plain = output + "##[error]Process completed with exit code 1.\n"
        self.assertLessEqual(len(plain), 8000)
        self.assertGreater(len(plain) + len(summary) + 2, 8000)
        actions_log = "".join(f"2026-11-21T12:00:00Z {line}" for line in plain.splitlines(keepends=True))
        self.assertEqual(ci.excerpt(actions_log).encode(), plain.encode())


class TimeoutDiagnosticsIsolationTests(unittest.TestCase):
    def test_worker_writes_normal_result_when_optional_id_raises_system_exit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "test_unavailable_id.py").write_text(
                "import unittest\n\n"
                "class UnavailableIdTests(unittest.TestCase):\n"
                "    ready = False\n"
                "    @classmethod\n"
                "    def setUpClass(cls): cls.ready = True\n"
                "    def id(self):\n"
                "        if self.ready and self._testMethodName == 'test_1_without_id':\n"
                "            raise SystemExit('fixture metadata unavailable')\n"
                "        return super().id()\n"
                "    def test_1_without_id(self): pass\n"
                "    def test_2_with_id(self): self.fail('original assertion')\n"
            )
            lifeline, held = os.pipe()
            reports = []
            try:
                with parallel.timing_channel() as (reader, writer):
                    command = [sys.executable, "-B", "-m", "tests.parallel", "--one",
                               "test_unavailable_id.UnavailableIdTests", "--start", directory,
                               "--lifeline", str(lifeline)]
                    for timing, fds in (([], (lifeline,)), worker_timing(reader, writer, lifeline)):
                        path = root / f"result-{len(reports)}.json"
                        result = subprocess.run([*command, "--result", str(path), *timing], pass_fds=fds,
                                                capture_output=True, text=True, timeout=2)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        reports.append(path.read_bytes())
                    events = parallel.timing_rows(reader)
            finally:
                os.close(lifeline)
                os.close(held)

        self.assertEqual(reports[1], reports[0])
        report = json.loads(reports[1])
        self.assertEqual(report["ran"], 2)
        self.assertEqual(report["errors"], [])
        self.assertIn("AssertionError: original assertion", report["failures"][0][1])
        self.assertEqual(sorted(name for _, name, _ in events), [
            "test_unavailable_id.UnavailableIdTests.test_1_without_id",
            "test_unavailable_id.UnavailableIdTests.test_2_with_id",
        ])

    def test_unittest_result_hook_exceptions_still_propagate(self):
        result = parallel.TimedResult(Mock())
        case = unittest.TestCase()
        for hook in ("startTest", "stopTest"):
            with self.subTest(hook=hook), patch.object(unittest.TestResult, hook, side_effect=SystemExit(7)):
                with self.assertRaises(SystemExit):
                    getattr(result, hook)(case)

    def test_rendered_summary_redacts_numeric_values(self):
        secret = "83927164"
        deadline_secret = "8.39272e+07"
        name = "fixture.Hanging"
        details = details_for(name)
        details.update(count=int(secret), deadline=int(secret), never_started=int(secret))
        details["timings"][0] = (float(secret), f"{name}.test_fast")
        sidecar = io.BytesIO((json.dumps(details) + "\n").encode())
        summaries = pipeline.read_timeout_summaries(
            sidecar, 3, lambda text: pipeline.vault.hidden(text, {secret, deadline_secret}),
        )
        expected = (f"{name} ([redacted] tests)\nclass exceeded [redacted]s\n"
                    "2 timing rows, [redacted] never started\nSlowest recorded tests:\n"
                    f"  [redacted].000s {name}.test_fast\n  0.001s {name}.test_faster\n{GUIDANCE}")
        self.assertEqual(summaries, [expected])

    def test_redaction_matches_across_rendered_summary_fields(self):
        name = "fixture.Hanging"
        secret = "Hanging (3 tests)"
        summary = pipeline.format_timeout_summary(details_for(name), lambda text: pipeline.vault.hidden(text, {secret}))
        self.assertEqual(summary, summary_for(name).replace(secret, "[redacted]"))


class TimeoutProgressRegressionTests(unittest.TestCase):
    def test_worker_publishes_the_same_result_when_optional_id_is_cancelled(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "test_cancelled_id.py").write_text(
                "from asyncio import CancelledError\nimport unittest\n\n"
                "class CancelledIdTests(unittest.TestCase):\n"
                "    @classmethod\n"
                "    def setUpClass(cls): cls.ready = True\n"
                "    def id(self):\n"
                "        if self.ready and self._testMethodName == 'test_cancelled_id':\n"
                "            raise CancelledError('fixture metadata unavailable')\n"
                "        return super().id()\n"
                "    def test_cancelled_id(self): pass\n"
                "    def test_normal_id(self): self.fail('original assertion')\n"
            )
            lifeline, held = os.pipe()
            reports = []
            try:
                with parallel.timing_channel() as (reader, writer):
                    command = [sys.executable, "-B", "-m", "tests.parallel", "--one",
                               "test_cancelled_id.CancelledIdTests", "--start", directory, "--lifeline", str(lifeline)]
                    for timing, fds in (([], (lifeline,)), worker_timing(reader, writer, lifeline)):
                        path = root / f"result-{len(reports)}.json"
                        child = subprocess.run([*command, "--result", str(path), *timing], pass_fds=fds,
                                               capture_output=True, text=True, timeout=2)
                        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                        reports.append(path.read_bytes())
                    events = parallel.timing_rows(reader)
            finally:
                os.close(lifeline)
                os.close(held)

        self.assertEqual(reports[1], reports[0])
        report = json.loads(reports[1])
        self.assertEqual(report["ran"], 2)
        self.assertIn("AssertionError: original assertion", report["failures"][0][1])
        self.assertEqual(sorted(name for _, name, _ in events), [
            "test_cancelled_id.CancelledIdTests.test_cancelled_id", "test_cancelled_id.CancelledIdTests.test_normal_id",
        ])

    def test_missing_start_timestamp_still_counts_the_test_as_started(self):
        batch = parallel.Batch("fixture.Sample", 3)
        clock = Mock(monotonic=Mock(side_effect=[OSError("start time unavailable"), 2, 2.5]))
        events = []
        with tempfile.TemporaryFile() as counter, patch.object(parallel, "time", clock):
            result = parallel.TimedResult(lambda *row: events.append(row), counter.fileno())
            for name in ("test_missing", "test_timed"):
                test = unittest.TestCase()
                test._testMethodName = name
                result.startTest(test)
                result.stopTest(test)
            started = os.lseek(counter.fileno(), 0, os.SEEK_CUR)
        details = parallel.class_timeout_details(batch, 3, 3, events, started)
        self.assertEqual(details["never_started"], 1)
        self.assertEqual(details["timings"], [(0.5, "unittest.case.TestCase.test_timed")])

    def test_timeout_counts_starts_with_a_full_progress_socket(self):
        reader, writer = socket.socketpair()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, reader, writer:
            writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            reader.setblocking(False)
            writer.setblocking(False)
            with self.assertRaises(BlockingIOError):
                while True:
                    writer.send(b"x" * 4096)
            with self.assertRaises(BlockingIOError):
                writer.send(b"x")

            root = Path(directory)
            (root / "test_full_socket.py").write_text(
                "import time\nimport unittest\n\n"
                "class FullSocketTests(unittest.TestCase):\n"
                "    def id(self): return None\n"
                "    def test_1_fast(self): print('fast', flush=True)\n"
                "    def test_2_hang(self):\n"
                f"        print({HANG_READY!r}, flush=True)\n"
                "        time.sleep(20)\n"
                "    def test_3_unreached(self): print('unreached', flush=True)\n"
            )
            batch = parallel.Batch("test_full_socket.FullSocketTests", 3)
            with patch.dict(os.environ, {SUMMARY_ENV: str(root / "summaries.jsonl")}):
                runner = parallel.Runner(SimpleNamespace(start=directory, timeout=1, verbose=False))
            runner.folder = directory
            runner.lifeline, held = os.pipe()
            try:
                channel = contextlib.nullcontext((reader, writer))
                with (patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}),
                      patch.object(parallel, "timing_channel", return_value=channel),
                      patch.object(parallel.subprocess, "Popen", start_hanging_worker),
                      patch.object(runner, "request_stacks", return_value=[])):
                    runner.child((0, batch))
            finally:
                os.close(runner.lifeline)
                os.close(held)

        report = runner.results[batch.identifier]
        self.assertIn("class exceeded 1s", report["timeout"])
        self.assertEqual(report["output"], f"fast\n{HANG_READY}\n")
        self.assertEqual(report["timeout_details"]["never_started"], 1)
        self.assertEqual(report["timeout_details"]["timings"], [])


class TimeoutTimingIsolationTests(unittest.TestCase):
    def test_worker_report_matches_baseline_when_optional_id_raises_keyboard_interrupt(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "test_interrupted_id.py").write_text(
                "import unittest\n\n"
                "class InterruptedIdTests(unittest.TestCase):\n"
                "    @classmethod\n"
                "    def setUpClass(cls): cls.ready = True\n"
                "    def id(self):\n"
                "        if self.ready and self._testMethodName == 'test_missing_id':\n"
                "            self.id_called = True\n"
                "            raise KeyboardInterrupt('fixture metadata unavailable')\n"
                "        return super().id()\n"
                "    def test_missing_id(self): self.assertFalse(getattr(self, 'id_called', False))\n"
                "    def test_normal_id(self): self.fail('original assertion')\n"
            )
            lifeline, held = os.pipe()
            reports = []
            try:
                with parallel.timing_channel() as (reader, writer):
                    command = [sys.executable, "-B", "-m", "tests.parallel", "--one",
                               "test_interrupted_id.InterruptedIdTests", "--start", directory,
                               "--lifeline", str(lifeline)]
                    for timing, fds in (([], (lifeline,)), worker_timing(reader, writer, lifeline)):
                        path = root / f"result-{len(reports)}.json"
                        child = subprocess.run([*command, "--result", str(path), *timing], pass_fds=fds,
                                               capture_output=True, text=True, timeout=2)
                        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                        reports.append(path.read_bytes())
                    events = parallel.timing_rows(reader)
            finally:
                os.close(lifeline)
                os.close(held)

        self.assertEqual(reports[1], reports[0])
        report = json.loads(reports[1])
        self.assertEqual(report["ran"], 2)
        self.assertIn("AssertionError: original assertion", report["failures"][0][1])
        self.assertEqual(sorted(name for _, name, _ in events), [
            "test_interrupted_id.InterruptedIdTests.test_missing_id",
            "test_interrupted_id.InterruptedIdTests.test_normal_id",
        ])

    def test_full_timing_channel_reports_zero_rows_and_counts_never_started(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "test_backpressure.py").write_text(
                "import time\nimport unittest\n\n"
                "class BackpressureTests(unittest.TestCase):\n"
                "    def id(self):\n"
                "        if self._testMethodName == 'test_2_without_id': raise RuntimeError()\n"
                "        return super().id()\n"
                "    def test_1_fast(self): print('fast', flush=True)\n"
                "    def test_2_without_id(self): print('without ID', flush=True)\n"
                "    def test_3_hang(self):\n"
                f"        print({HANG_READY!r}, flush=True)\n"
                "        time.sleep(20)\n"
                "    def test_4_unreached(self): self.fail('unreached')\n"
            )
            batch = parallel.Batch("test_backpressure.BackpressureTests", 4)
            with patch.dict(os.environ, {SUMMARY_ENV: str(root / "summaries.jsonl")}):
                runner = parallel.Runner(SimpleNamespace(start=directory, timeout=1, verbose=False))
            runner.folder = directory
            runner.lifeline, held = os.pipe()
            try:
                with parallel.timing_channel() as (reader, writer):
                    # The fast test's timing snapshot cannot fit in this channel.
                    with self.assertRaises(OSError) as full:
                        while True:
                            writer.send(b"x" * 32)
                    self.assertIn(full.exception.errno, (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS))
                    channel = contextlib.nullcontext((reader, writer))
                    with (patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}),
                          patch.object(parallel, "timing_channel", return_value=channel),
                          patch.object(parallel.subprocess, "Popen", start_hanging_worker),
                          patch.object(runner, "request_stacks", return_value=[])):
                        runner.child((0, batch))
            finally:
                os.close(runner.lifeline)
                os.close(held)

        report = runner.results[batch.identifier]
        self.assertIn("class exceeded 1s", report["timeout"])
        self.assertEqual(report["output"], f"fast\nwithout ID\n{HANG_READY}\n")
        details = report["timeout_details"]
        self.assertEqual(details["never_started"], 1)
        self.assertEqual(details["timings"], [])
        summary = pipeline.format_timeout_summary(details, str)
        self.assertEqual(summary.splitlines()[2:], ["0 timing rows, 1 never started", GUIDANCE])

    def test_timing_uses_names_without_invoking_attribute_hooks(self):
        class TestType(type):
            def __getattribute__(cls, name):
                if name in {"__module__", "__qualname__"}:
                    raise AssertionError(name)
                return super().__getattribute__(name)

        class Sample(unittest.TestCase, metaclass=TestType):
            @property
            def __dict__(self):
                raise AssertionError("test dictionary hook")

            def __getattribute__(self, name):
                if name == "_testMethodName":
                    raise AssertionError(name)
                return super().__getattribute__(name)

        rows = []
        case = Sample()
        result = parallel.TimedResult(lambda *row: rows.append(row))
        result.startTest(case)
        result.stopTest(case)
        self.assertEqual([name for _, name, _ in rows], [
            f"{__name__}.TimeoutTimingIsolationTests.test_timing_uses_names_without_invoking_attribute_hooks"
            ".<locals>.Sample.runTest",
        ])

    def test_worker_keeps_only_the_slowest_five_before_the_deadline(self):
        with parallel.timing_channel() as (reader, writer):
            reader.send(b"10")
            publisher = parallel.TimingPublisher(writer, reader)
            for finished, duration in enumerate((1, 2, 3, 4, 5, 6, 7), 1):
                publisher.record(finished, f"fixture.Sample.test_{finished}", duration)
            publisher.record(11, "fixture.Sample.test_grace", 11)
            rows = parallel.timing_rows(reader)

        self.assertEqual([duration for _, _, duration in rows], [7, 6, 5, 4, 3])

    def test_keyboard_interrupt_from_test_body_or_result_hook_still_propagates(self):
        class Interrupted(unittest.TestCase):
            def runTest(self):
                raise KeyboardInterrupt("test body")

        with self.assertRaisesRegex(KeyboardInterrupt, "test body"):
            Interrupted().run(parallel.TimedResult(Mock()))
        for hook in ("startTest", "stopTest"):
            result = parallel.TimedResult(Mock())
            with self.subTest(hook=hook), patch.object(unittest.TestResult, hook, side_effect=KeyboardInterrupt(hook)):
                with self.assertRaisesRegex(KeyboardInterrupt, hook):
                    getattr(result, hook)(Interrupted())
