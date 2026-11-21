import unittest

from arete.environment_gaps import Signature, decide_hold, signatures


class EnvironmentSignatureTests(unittest.TestCase):
    def test_sandbox_denial_has_one_identity_across_reports(self):
        expected = {Signature("sandbox_apply: Operation not permitted", "sandbox_apply")}
        self.assertEqual(signatures("sandbox-exec: sandbox_apply: Operation not permitted"), expected)
        self.assertEqual(signatures("Could not run replay. Output: sandbox_apply: Operation not permitted (1)"), expected)

    def test_exit_127_names_the_missing_tool(self):
        expected = {Signature("exit 127", "uv")}
        self.assertEqual(signatures("exit 127: /bin/sh: line 1: uv: command not found"), expected)
        self.assertEqual(signatures("exit 127: zsh:1: command not found: uv"), expected)
        self.assertEqual(signatures("The probe reported exit status 127 for 'uv'."), expected)
        self.assertEqual(signatures("`uv run` exited with code 127"), expected)
        self.assertEqual(signatures("exit 127 for `arete`"), {Signature("exit 127", "arete")})

    def test_git_names_the_missing_subcommand(self):
        expected = {Signature("is not a git command", "replay")}
        self.assertEqual(signatures("exit 1: git: 'replay' is not a git command. See 'git --help'."), expected)
        self.assertEqual(signatures("git: 'stageit' is not a git command"),
                         {Signature("is not a git command", "stageit")})

    def test_unknown_or_unnamed_errors_have_no_signature(self):
        for text in ("connect ENOTFOUND", "The command exited 127", "exit 127", "uv: command not found",
                     "Unexpected exit 127", "The shell returned exit 127 for the requested command.",
                     "exit 127 for uv", "exit 127 for /private/scratch/uv", "exit 1270 for 'uv'"):
            with self.subTest(text=text):
                self.assertEqual(signatures(text), set())


class EnvironmentHoldTests(unittest.TestCase):
    def setUp(self):
        self.url = "https://github.com/example/arete/pull/42"
        self.check = {"passed": False, "cause": "environment", "what": "Run replay", "how": "arete replay",
                      "saw": "sandbox_apply: Operation not permitted"}
        self.verified = {"verdict": "blocked", "finished": False, "checks": [{"passed": True}, self.check]}
        self.publication = {"draft_why": "verification was blocked", "heads": {"example/arete": "head"},
                            "prs": {"example/arete": self.url}, "verified": self.verified}
        self.review = {"verdict": "approve"}

    def test_environment_only_hold_is_independent_of_finished(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                self.verified["finished"] = finished
                hold = decide_hold(self.publication, self.review)
                self.assertIsNotNone(hold)
                self.assertEqual(hold.heads, {self.url: "head"})
                self.assertEqual(len(hold.gaps), 1)

    def test_any_unrecognized_or_non_environment_failure_keeps_the_manual_hold(self):
        for check in ({**self.check, "saw": "connect ENOTFOUND"},
                      {**self.check, "cause": "product"}, {**self.check, "cause": ""}):
            with self.subTest(check=check):
                self.verified["checks"] = [self.check, check]
                self.assertIsNone(decide_hold(self.publication, self.review))

    def test_another_reason_for_the_draft_prevents_automatic_resume(self):
        self.assertIsNone(decide_hold(self.publication, {"verdict": "request_changes"}))
        self.assertIsNone(decide_hold({**self.publication, "ci_stopped": ["example/arete"]}, self.review))
        self.assertIsNone(decide_hold({**self.publication, "draft_why": ""}, self.review))
