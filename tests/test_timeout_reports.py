import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
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


def summary_for(name, deadline=10):
    return (f"{name} (3 tests)\nclass exceeded {deadline}s\n2 completed, 0 never started\n"
            f"Slowest completed tests:\n  0.010s {name}.test_fast\n  0.001s {name}.test_faster\n{GUIDANCE}")


def details_for(name):
    return {"class_name": name, "count": 3, "deadline": 10, "completed": 2, "never_started": 0,
            "timings": [(0.010, f"{name}.test_fast"), (0.001, f"{name}.test_faster")]}


def timeout_output(name, stack=""):
    return "\n".join(filter(None, ("=== Test class timeout ===", name, "class exceeded 10s", stack,
                                  "=== End test class timeout ===")))


def summaries_in(path):
    with path.open("rb") as sidecar:
        return pipeline.read_timeout_summaries(sidecar, 3, str)


class ParallelTimeoutReportTests(unittest.TestCase):
    def test_timeout_preserves_stacks_and_keeps_generated_ids_out_of_retained_artifacts(self):
        marker = "synthetic-completed-test-id"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            sidecar = root / "summaries.jsonl"
            (root / "test_hang.py").write_text(
                "import time\nimport unittest\n\n"
                "class HangingTests(unittest.TestCase):\n"
                "    def id(self): return super().id() + getattr(self, 'suffix', '')\n"
                f"    def test_1_fast(self): self.suffix = '[{marker}]'\n"
                "    def test_2_slower(self): time.sleep(0.025)\n"
                "    def test_3_hang(self): time.sleep(20)\n"
            )
            result = subprocess.run(
                [sys.executable, "-m", "tests.parallel", "--start", directory, "--timeout", "0.5"],
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
        for dump, deadline in zip(dumps, ("0.5s", "1s")):
            self.assertEqual(dump.splitlines()[1:3], [name, f"class exceeded {deadline}"])
            self.assertRegex(dump.splitlines()[3], r"^Python class stack \(PID \d+\)$")
            self.assertIn("in test_3_hang", dump)
        self.assertNotIn(GUIDANCE, result.stdout)
        self.assertNotIn("Slowest completed tests:", result.stdout)
        self.assertEqual(len(summaries), 1)
        lines = summaries[0].splitlines()
        self.assertEqual(lines[:4], [f"{name} (3 tests)", "class exceeded 1s", "2 completed, 0 never started",
                                     "Slowest completed tests:"])
        self.assertRegex(lines[4], rf"^  \d+\.\d{{3}}s {name}\.test_2_slower$")
        self.assertRegex(lines[5], rf"^  \d+\.\d{{3}}s {name}\.test_1_fast\[{marker}\]$")
        self.assertEqual(lines[6:], [GUIDANCE])

    def test_deadline_excludes_grace_period_and_keeps_only_five_timings(self):
        batch = parallel.Batch("fixture.Sample", 8)
        durations = (2, 5, 1, 7, 4, 3, 20, 1)
        events = [timestamp for n, seconds in enumerate(durations) for timestamp in (10 * n, 10 * n + seconds)]
        events[-2:] = [81, 82]
        with io.StringIO() as progress:
            result = parallel.TimedResult(progress)
            clock = Mock(monotonic=Mock(side_effect=events))
            with patch.object(parallel, "time", clock):
                for index in range(batch.count):
                    test = Mock(id=Mock(return_value=f"fixture.Sample.test_{index}"))
                    result.startTest(test)
                    result.stopTest(test)
            details = parallel.class_timeout_details(batch, 75, 75, io.BytesIO(progress.getvalue().encode()))
        summary = pipeline.format_timeout_summary(details, str)

        self.assertEqual(summary.splitlines()[2:-1], ["6 completed, 1 never started", "Slowest completed tests:",
                                                   "  7.000s fixture.Sample.test_3", "  5.000s fixture.Sample.test_1",
                                                   "  4.000s fixture.Sample.test_4", "  3.000s fixture.Sample.test_5",
                                                   "  2.000s fixture.Sample.test_0"])
        long_name = "fixture." + "long_module." * 80 + "LongTests"
        summary = pipeline.format_timeout_summary({**details, "class_name": long_name}, str)
        self.assertLessEqual(len(summary), 800)
        self.assertIn("LongTests (8 tests)\nclass exceeded 75s", summary)
        self.assertTrue(summary.endswith("\n" + GUIDANCE))

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


class TimeoutSummaryRedactionTests(unittest.TestCase):
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

        with (patch.object(pipeline.environment, "stored", stored),
              patch.object(pipeline.environment, "passed", return_value=[]),
              patch.object(pipeline.vault, "hidden", side_effect=positional_redactor),
              patch.object(pipeline.util, "sh", side_effect=sh),
              patch.object(record, "timed", return_value=contextlib.nullcontext({}))):
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
        self.assertIn(f"{name} (3 tests)\nclass exceeded 10s\n2 completed, 0 never started", summary)
        self.assertIn("  0.010s test_", summary)
        self.assertIn("[redacted]", summary)
        self.assertNotIn(value, report)
        self.assertTrue(summary.endswith("\n" + GUIDANCE))


class LocalTimeoutReportTests(unittest.TestCase):
    def run_checks(self, outputs, *, slow=()):
        commands = {name: f"run {name}" for name in (*outputs, *slow)}
        layer = config.RepoLayer(Path.cwd(), checks=commands, quick_checks=list(outputs), parallel_checks=True)
        paths = []

        def sh(cmd, *, env, **_kw):
            path = Path(env[SUMMARY_ENV])
            paths.append(path)
            stdout, stderr, summaries = outputs[cmd.removeprefix("run ")]
            path.write_text("".join(json.dumps(summary) + "\n" for summary in summaries))
            return SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)

        with (patch.object(pipeline.util, "sh", side_effect=sh),
              patch.object(record, "timed", side_effect=lambda *_, **__: contextlib.nullcontext({}))):
            ok, report = pipeline.run_checks(layer, Path.cwd(), [], Mock(config=config.Config(), repo=None), 30)
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
