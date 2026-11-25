import unittest
from pathlib import Path

from arete import check_repair, config


class FailedCheckDiagnosticsTests(unittest.TestCase):
    layer = config.RepoLayer(Path("."), checks={"behavior": check_repair.ARETE_SUITE, "lint": "lint"})
    first = "test_profile.ProfileTests.test_launch"
    second = "test_store.StoreTests.test_read"
    denied = "AssertionError: 71 != 0 : sandbox-exec: sandbox_apply: Operation not permitted"

    def failure(self, second_error):
        output = (f"test_launch ({self.first})\n{self.denied}\ntest_read ({self.second})\n{second_error}\n"
                  "Failed classes: test_profile.ProfileTests, test_store.StoreTests\nRan 2 tests in 0.20s: FAILED")
        return check_repair.failed_check(self.layer, "behavior", output, 1)

    def test_only_shared_confirmed_runner_failures_are_grouped(self):
        result = self.failure(self.denied)
        self.assertEqual(result, {"name": "behavior", "selectors": [self.first, self.second],
                                  "capability": check_repair.MACOS_PROFILE})
        self.assertNotIn("capability", self.failure("AssertionError: incorrect value"))

    def test_split_sandbox_stderr_in_the_child_traceback(self):
        assertion = "AssertionError: sandbox subprocess exited with return code 71; stderr:"
        stderr = "sandbox-exec: sandbox_apply: Operation not permitted"
        child = ("Traceback (most recent call last):\n  File \"child.py\", line 9, in <module>\n"
                 "    case.fail(message)\n")
        nested = "AssertionError: 1 != 0 : child output\n" + child + assertion + "\n" + stderr
        self.assertEqual(self.failure(nested)["capability"], check_repair.MACOS_PROFILE)
        for diagnostic in (assertion, assertion + "\nother failure\n" + stderr,
                           assertion.replace("71", "1") + "\n" + stderr,
                           assertion + "\n" + stderr + "\nadditional stderr",
                           nested + "\n\n" + child + "ValueError: application failed",
                           child + "    " + assertion + "\n    " + stderr + "\nAssertionError: wrong output"):
            with self.subTest(diagnostic=diagnostic):
                self.assertNotIn("capability", self.failure(diagnostic))

    def test_truncated_check_report_cannot_confirm_the_runner_was_the_only_failure(self):
        output = (f"...\ntest_launch ({self.first})\n{self.denied}\n"
                  "Failed classes: test_profile.ProfileTests\nRan 2 tests in 0.20s: FAILED")
        failure = check_repair.failed_check(self.layer, "behavior", output, 1)
        self.assertEqual(failure, {"name": "behavior", "selectors": [self.first]})

    def test_sandbox_stderr_immediately_precedes_the_failing_subprocess_traceback(self):
        stderr = "sandbox-exec: sandbox_apply: Operation not permitted"
        traceback = "Traceback (most recent call last):\n    subprocess.run(command, check=True)\n"
        error = ("subprocess.CalledProcessError: Command '['/usr/bin/sandbox-exec', '-p', 'profile', 'node']' "
                 "returned non-zero exit status 71.")
        result = self.failure(stderr + "\n" + traceback + error)
        self.assertEqual(result["capability"], check_repair.MACOS_PROFILE)
        for diagnostic in (stderr + "\n" + traceback + error.replace("/usr/bin/sandbox-exec", "/usr/bin/node"),
                           stderr + "\n" + traceback + error.replace("status 71", "status 1"),
                           "    " + stderr + "\n" + traceback + error,
                           stderr + "\nunrelated stderr\n" + traceback + error,
                           stderr + "\n" + traceback + error + "\n" + traceback + "AssertionError: wrong value"):
            with self.subTest(diagnostic=diagnostic):
                self.assertNotIn("capability", self.failure(diagnostic))

    def test_reuse_the_configured_runner_flags_without_deriving_a_command_from_shell_syntax(self):
        layer = config.RepoLayer(Path("."), checks={"tests": "uv run --locked python -m tests.parallel --timeout 30"})
        self.assertEqual(check_repair.classes_command(layer, "tests"),
                         ["uv", "run", "--locked", "python", "-m", "tests.parallel", "--timeout", "30"])
        layer.checks["tests"] = "prepare && python -m tests.parallel"
        self.assertEqual(check_repair.classes_command(layer, "tests"), [])

    def test_older_receipt_recovers_only_the_configured_failure_and_valid_runner_selectors(self):
        report = (f"### lint passed: `lint`\n\n### behavior failed: `{check_repair.ARETE_SUITE}` exit 1\n```\n"
                  f"test_launch ({self.first})\n{self.denied}\nFailed classes: test_profile.ProfileTests\n"
                  "Ran 1 tests in 1.00s: FAILED\n\n```")
        failures = check_repair.historical_failures(self.layer, report)
        self.assertEqual(check_repair.focus(self.layer, failures), {"behavior": [self.first]})
        self.assertEqual(check_repair.focus(self.layer, [{"name": "behavior", "selectors": ["--all"]}]), {})
        self.assertEqual(check_repair.focus(self.layer, [{"name": "other", "selectors": []}]), {})
        self.assertEqual(check_repair.focus(self.layer, [{"name": n} for n in self.layer.checks]), {})
