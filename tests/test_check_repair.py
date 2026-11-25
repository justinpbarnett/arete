import shlex
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arete import check_repair, config, environment, flows, hosted_check, orchestration, pipeline, prompt_loader, vault
from arete.store import orchestration as ledger


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

    def local_checks(self, *, secret="", **cases):
        root = Path(self.enterContext(TemporaryDirectory()))
        commands = {}
        for name, (code, output) in cases.items():
            fixture = root / name
            fixture.write_text(f"printf '%s\\n' {shlex.quote(output)} >&2\nexit {code}\n")
            commands[name] = shlex.join(["/bin/sh", str(fixture)])
        layer = config.RepoLayer(root, checks=commands, quick_checks=tuple(commands),
                                 targeted_commands={"classes": check_repair.PARALLEL})
        run = SimpleNamespace(id="fixture", config=config.Config(), repo=None, time_for=lambda n: n, log=Mock(),
                              story=[], save=Mock(), settings=lambda _: SimpleNamespace(tries=2))
        results = []
        with (patch.object(pipeline, "check_lane", return_value=nullcontext(())),
              patch.object(pipeline.record, "timed", side_effect=lambda *a, **kw: nullcontext(kw)),
              patch.object(environment, "stored", return_value={"TOKEN": secret} if secret else {})):
            passed, report = pipeline.run_checks(layer, root, [], run, 5, results=results)
        self.assertFalse(passed)
        self.assertEqual([item["exit"] for item in results], [code for code, _ in cases.values()])
        return root, layer, run, results, report

    def test_no_footer_diagnostic_survives_redaction_and_report_and_story_tails(self):
        secret = "fixture-secret-value"
        reason = "ImportError: could not load " + secret + " " + "x" * 400
        output = ("ValueError: earlier exception\nTraceback (most recent call last):\n"
                  '  File "test_profile.py", line 1, in <module>\n    import missing\n' + reason + "\n"
                  + "log noise " * 800 + "\n````\n    AssertionError: quoted source\nStatus: failed")
        root, layer, run, results, report = self.local_checks(
            behavior=(1, output), compile=(1, "more log noise " * 500), secret=secret)
        redacted = reason.replace(secret, vault.REDACTED)[:300]
        self.assertEqual(results[0], {"name": "behavior", "selectors": [], "reason": redacted, "exit": 1, "passed": False})
        self.assertNotIn(secret, report)
        self.assertNotIn(redacted, report.split("`````", 2)[1])
        self.assertIn(f"behavior (exit 1):\n\n    {redacted}", report)
        self.assertNotIn(redacted, report[-2500:])
        b = SimpleNamespace(check=Mock(return_value=False), check_results=results, report=report, checked={}, run=run,
                            check_commands="`checks behavior`", deny=[], resume=Mock())
        job = SimpleNamespace(run=run, b=b, layer=layer, policy=Mock())
        with (patch.object(flows.paths, "CONFIG_DIR", root), patch.object(flows.record, "note")):
            outcome = flows.checks(SimpleNamespace(ctx=job), tree=str(root), tries=2)
        self.assertIn(redacted, outcome.data["story"][-1])
        self.assertIn(redacted, outcome.feedback)
        b.check.side_effect = [False, True]
        self.assertEqual(pipeline.Build.check_with_repairs(b, lambda n: f"fix-{n}"), (True, 2))
        self.assertIn(redacted, run.story[-1])
        self.assertIn(redacted, b.resume.call_args.args[1])
        self.assertIn("first rerun its named configured command", b.resume.call_args.args[1])

    def test_summary_still_reports_and_recovers_class_and_case_selectors(self):
        output = (f"test_launch ({self.first})\nAssertionError: incorrect value\n````\n"
                  "Failed classes: test_profile.ProfileTests, test_store.StoreTests\nRan 2 tests in 1.00s: FAILED")
        _, layer, _, results, report = self.local_checks(behavior=(1, output))
        expected = [self.first, "test_store.StoreTests"]
        self.assertEqual(results[0]["selectors"], expected)
        self.assertNotIn("reason", results[0])
        self.assertIn(", ".join(expected), report)
        self.assertEqual(check_repair.focus(layer, check_repair.historical_failures(layer, report)), {"behavior": expected})

    def test_exact_sandbox_denial_remains_hosted_eligible_and_hosted_failure_has_no_guessed_reason(self):
        root, _, run, results, report = self.local_checks(behavior=(71, check_repair.SANDBOX_DENIAL))
        b = SimpleNamespace(check=Mock(return_value=False), check_results=results, report=report, checked={})
        run.path, run.dry_run, run.tid = "orchestrate", False, "1028"
        run.repo = SimpleNamespace(name="arete", github=hosted_check.REPO)
        job = SimpleNamespace(run=run, b=b, layer=self.layer, ws=root, policy=Mock(),
                              checkouts=[SimpleNamespace(github=hosted_check.REPO, base="main")])
        self.assertEqual(results[0]["capability"], check_repair.MACOS_PROFILE)
        self.assertNotIn("reason", results[0])
        with patch.object(hosted_check.paths, "ISOLATED", False):
            self.assertTrue(hosted_check.eligible(job, results))
            for code, output in [(1, check_repair.SANDBOX_DENIAL), (71, check_repair.SANDBOX_DENIAL + "\nextra")]:
                self.assertFalse(hosted_check.eligible(job, [check_repair.failed_check(self.layer, "behavior", output, code)]))
        receipt = {"status": "failure", "exit": 1, "url": "https://example.invalid/hosted"}
        (root / "hosted-macos.toml").touch()
        with (patch.object(flows.paths, "CONFIG_DIR", root), patch.object(flows.factory, "fingerprint", return_value="tree"),
              patch.object(flows.hosted_check, "check", return_value=receipt), patch.object(flows.record, "note")):
            outcome = flows.checks(SimpleNamespace(ctx=job), tree=str(root), tries=2)
        self.assertEqual(outcome.data["hosted_check"], receipt)
        self.assertEqual(outcome.data["failed_checks"], [{"name": "behavior", "selectors": [], "exit": 1}])
        hosted_report = outcome.feedback.split("### GitHub-hosted macOS check:", 1)[1]
        self.assertIn("behavior (exit 1): no failing case recorded", hosted_report)
        self.assertNotIn(check_repair.SANDBOX_DENIAL, hosted_report)

    def test_orchestrated_diagnostics_require_the_current_check_and_candidate(self):
        tree = ledger.digest({"repo": "tree"})
        stamp = {"tree": tree, "policy": "policy", "acceptance": "issue"}
        failure = {"name": "behavior", "exit": 1, "reason": "AssertionError: wrong value"}
        action = {"id": "a1", "op": "checks.run", "state": "succeeded",
                  "result": {"input_stamp": stamp, "passed": False, "failed_checks": [failure]}}
        ctl = SimpleNamespace(data={"acceptance": "issue"}, fingerprint=Mock(return_value=tree),
                              policy_sha=lambda: "policy", job=SimpleNamespace(checkouts=[SimpleNamespace(github="repo")]))
        diagnostics = orchestration.Controller.current_check_diagnostics
        candidate = {"candidate": {"parts": [{"tree": "tree"}]}}
        self.assertIn(failure["reason"], diagnostics(ctl, [action], candidate))
        candidate["candidate"]["parts"][0]["tree"] = "older private tree"
        self.assertEqual(diagnostics(ctl, [action], candidate), "")
        self.assertEqual(diagnostics(ctl, [action, {**action, "id": "a2", "result": {"passed": True}}]), "")
        ctl.fingerprint.return_value = "new tree"
        self.assertEqual(diagnostics(ctl, [action]), "")
        ctl.fingerprint.return_value = tree
        ctl.data["acceptance"] = "revised issue"
        self.assertEqual(diagnostics(ctl, [action]), "")

    def test_rendered_repair_prompt_uses_the_configured_verifier_first(self):
        brief = prompt_loader.Brief("arete", "/repo", "#1028", "Fixture", "")
        text = check_repair.builder_prompt(brief.prompt(
            "repair", plan="plan", source="run", findings="findings", checks="`checks behavior`"))
        self.assertIn("first rerun its named configured command", text)
        self.assertIn("Do not build a separate verifier environment", text)
        self.assertIn("`checks behavior`", text)
