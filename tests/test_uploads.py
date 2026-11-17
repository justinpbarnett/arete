import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arete import gh, guard, outbound, paths, uploads, util


class InertModelTextTests(unittest.TestCase):
    def test_inert_table(self):
        table = [
            ("cc @someone and @org/team.", "cc <code>@someone</code> and <code>@org/team</code>."),
            ("Fixes #12", "Fixes <code>#12</code>"),
            ("resolved: fixture/repo#12", "resolved: <code>fixture/repo#12</code>"),
            ("Resolve https://github.com/o/r/issues/12", "Resolve <code>https://github.com/o/r/issues/12</code>"),
            ("**Fixes** #12", "**Fixes** <code>#12</code>"),
            ("<pre>Fixes #12</pre>", "<pre>Fixes <code>#12</code></pre>"),
            ("see #12", "see #12"),
            ("a@b.co", "a@b.co"),
            ("https://example.com/@someone/post", "https://example.com/@someone/post"),
            ("`@someone Fixes #12`", "`@someone Fixes #12`"),
            ("```\n@someone\nFixes #12\n```", "```\n@someone\nFixes #12\n```"),
            ("<code>@someone</code>", "<code>@someone</code>"),
        ]
        for text, expected in table:
            with self.subTest(text=text):
                self.assertEqual(outbound.inert(text), expected)
                self.assertEqual(outbound.inert(expected), expected)

    def test_gh_refuses_text_that_skipped_outgoing(self):
        calls = [
            (["issue", "comment", "5", "--body-file", "-"], "@someone"),
            (["issue", "close", "5", "--comment", "@someone"], None),
            (["issue", "comment", "5", "-b", "@someone"], None),
            (["pr", "create", "--body=@someone"], None),
            (["api", "x", "-fbody=@someone"], None),
            (["api", "x", "--input", "/tmp/body.json"], None),
        ]
        with mock.patch.object(util, "sh_with_network", side_effect=AssertionError("transport reached")) as transport:
            for args, body in calls:
                with self.subTest(args=args), self.assertRaisesRegex(TypeError, "gh.outgoing"):
                    gh.gh(args, input=body)
            transport.assert_not_called()
        with mock.patch.object(outbound, "keep_configured_private"), \
                mock.patch.object(outbound, "own_hosts", []), mock.patch.object(outbound, "extra_hosts", set()):
            self.assertEqual(gh.outgoing("Fixes #12 and @someone", "Closes #5"),
                             "Closes #5\n\nFixes <code>#12</code> and <code>@someone</code>")


class UploadGuardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.evidence = self.root / "runs" / "run" / "evidence"
        self.browser = self.root / "runs" / "run" / "browser"
        self.outside = self.root / "outside.txt"
        for directory in (self.evidence, self.browser):
            directory.mkdir(parents=True)
        self.outside.write_text("x")
        for patch in (mock.patch.object(paths, "RUNS_DIR", self.root / "runs"),
                      mock.patch.dict(os.environ, {"ARETE_RUN_ID": "run"})):
            patch.start()
            self.addCleanup(patch.stop)
        noted = mock.patch.object(guard, "note")
        self.note = noted.start()
        self.addCleanup(noted.stop)

    def call(self, payload, raw=None):
        text = raw if raw is not None else json.dumps(payload)
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(text)), mock.patch.object(sys, "stdout", out):
            code = uploads.main()
        return code, out.getvalue()

    def upload(self, *names):
        return {"tool_name": uploads.TOOL, "tool_input": {"paths": [str(name) for name in names]}}

    def test_only_existing_nonsecret_files_in_this_run_can_be_uploaded(self):
        good = self.evidence / "fixture.txt"
        browser = self.browser / "fixture.txt"
        secret = self.evidence / ".env"
        for file in (good, browser, secret):
            file.write_text("fixture")
        escape = self.evidence / "escape.txt"
        escape.symlink_to(self.outside)
        hidden_secret = self.evidence / "secret-alias.txt"
        hidden_secret.symlink_to(secret)
        cases = [
            ((good, browser), ""),
            ((self.outside,), uploads.OUTSIDE),
            ((good, self.outside), uploads.OUTSIDE),
            ((escape,), uploads.OUTSIDE),
            ((secret,), uploads.SECRET),
            ((hidden_secret,), uploads.SECRET),
            ((self.evidence / "missing.txt",), uploads.MISSING),
            ((self.evidence,), uploads.MISSING),
            (("relative.txt",), uploads.OUTSIDE),
        ]
        for files, rule in cases:
            with self.subTest(files=files):
                reason = uploads.decide(self.upload(*files))
                if rule:
                    self.assertIn(rule, reason)
                else:
                    self.assertEqual(reason, "")
        self.assertEqual(uploads.decide(self.upload()), "")

    def test_errors_deny(self):
        bad = ["not json", {"tool_input": {"paths": "not a list"}},
               {"tool_input": {"paths": [1]}}, {"tool_input": "x"}]
        for payload in bad:
            with self.subTest(payload=payload):
                code, out = self.call(payload, payload if isinstance(payload, str) else None)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        good = self.evidence / "fixture.txt"
        good.write_text("fixture")
        for run_id in ("", "..", "../run"):
            with self.subTest(run_id=run_id), mock.patch.dict(os.environ, {"ARETE_RUN_ID": run_id}):
                self.assertEqual(json.loads(self.call(self.upload(good))[1])
                                 ["hookSpecificOutput"]["permissionDecision"], "deny")
        with mock.patch.object(uploads, "decide", side_effect=RuntimeError):
            self.assertEqual(json.loads(self.call(self.upload(good))[1])
                             ["hookSpecificOutput"]["permissionDecision"], "deny")
        self.note.side_effect = OSError
        self.assertEqual(json.loads(self.call(self.upload(self.outside))[1])
                         ["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
