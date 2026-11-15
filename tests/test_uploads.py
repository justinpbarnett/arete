import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from arete import gh, outbound, paths, pipeline, util, verify, cli, codex, guard, runner, uploads

from tests import support
from tests.harness import make_folders, run_script, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


FAKE_GH ='''#!{python}
import json, os, sys
argv = sys.argv[1:]
stdin = ""
if any(a in ("--body-file", "--input") and i + 1 < len(argv) and argv[i + 1] == "-" for i, a in enumerate(argv)):
    stdin = sys.stdin.read()
with open(os.environ["GH_LOG"], "a") as log:
    log.write(json.dumps({{"args": argv, "input": stdin}}) + "\\n")
last = os.environ["GH_LOG"] + ".body"
head = argv[:2]
if head in (["pr", "create"], ["pr", "edit"]):
    open(last, "w").write(stdin)
if head == ["issue", "view"]:
    print(json.dumps({{"number": 5, "title": "Add kumquat", "body": "Add kumquat to the fixture notes",
                      "labels": [{{"name": "agent"}}], "state": "OPEN", "author": {{"login": "fixture"}},
                      "comments": [], "url": "https://github.com/fixture/repo/issues/5",
                      "updatedAt": "2026-01-01T00:00:00Z"}}))
elif head == ["pr", "create"]:
    print("https://github.com/fixture/repo/pull/7")
elif head == ["issue", "create"]:
    print("https://github.com/fixture/repo/issues/9")
elif head == ["pr", "view"]:
    body = open(last).read() if os.path.exists(last) else ""
    print(json.dumps({{"body": body, "isDraft": False, "headRefOid": "0" * 40, "mergeStateStatus": "BLOCKED",
                      "mergeable": "MERGEABLE", "number": 7, "url": "https://github.com/fixture/repo/pull/7",
                      "state": "OPEN", "headRefName": "x", "statusCheckRollup": []}}))
elif head == ["pr", "list"] or argv[:1] == ["api"]:
    print("[]")
'''


class InertModelTextTests(unittest.TestCase):
    MODEL = "cc @someone and @org/team. Fixes #12"
    PLANTED = ("Closes #5\n\nresolved: fixture/repo#12\n\n`@someone Fixes #12`\n\n"
               "```\n@someone\nFixes #12\n```\n")
    PROBE = f"PROBE {MODEL}\n\n{PLANTED}"
    SCRIPT = (SCRIPTS / "inert_model_text.py").read_text()

    def run_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "authority-parent", "home", "bin")
            fake = root / "bin/gh"
            fake.write_text(FAKE_GH.format(python=sys.executable))
            fake.chmod(0o755)
            log = root / "gh.log"
            env = scratch_env(root, keep=("LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              PATH=f"{root / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}", GH_LOG=str(log),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), HOME=str(root / "home"),
                              GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            done = run_script(self.SCRIPT, root, str(root), self.MODEL, self.PLANTED, self.PROBE, env=env, timeout=300)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            self.assertTrue(log.exists(), done.stderr + done.stdout)
            return [json.loads(line) for line in log.read_text().splitlines()]

    def assert_inert(self, body):
        outside = re.sub(r"```.*?```|`[^`\n]*`|<code>.*?</code>", "", body, flags=re.DOTALL)
        self.assertIn("<code>@someone</code>", body)
        self.assertIn("<code>@org/team</code>", body)
        self.assertIn("Fixes <code>#12</code>", body)
        self.assertNotIn("@someone", outside, body)
        self.assertNotIn("@org/team", outside, body)
        self.assertIsNone(re.search(r"(?i)\b(fixes|closes|resolved):?\s*(#\d|fixture/repo#)", outside), body)
        self.assertIn("Closes <code>#5</code>", body)
        self.assertIn("resolved: <code>fixture/repo#12</code>", body)
        self.assertIn("`@someone Fixes #12`", body)
        self.assertIn("```\n@someone\nFixes #12\n```", body)

    def test_model_text_is_inert_in_every_github_write(self):
        calls = self.run_workflow()

        def kind(*head):
            return [c["input"] for c in calls if c["args"][:len(head)] == list(head)]

        created = kind("pr", "create")
        self.assertEqual(len(created), 1, calls)
        self.assertEqual(created[0].split("\n")[0], "Closes #5")
        self.assertEqual(created[0].count("Closes #5"), 1)
        self.assert_inert(created[0].split("\n", 1)[1])
        comment = next(b for b in kind("pr", "comment") if "PROBE" in b)
        self.assert_inert(comment)
        self.assertRegex(comment, r"<!-- arete -->\n\n<!-- arete:key=[0-9a-f]{32} -->\Z")
        self.assert_inert(next(b for b in kind("issue", "comment") if "PROBE" in b))
        self.assert_inert(next(b for b in kind("issue", "create") if "PROBE" in b))
        self.assertTrue(any("PRs for this ticket" in b and "@" not in b for b in kind("issue", "comment")), calls)
        for edited in kind("pr", "edit"):
            self.assertEqual(edited.split("\n")[0], "Closes #5")
            self.assertNotIn("<code><code>", edited)

    def test_inert_table(self):
        table = [
            ("cc @someone and @org/team.", "cc <code>@someone</code> and <code>@org/team</code>."),
            ("Fixes #12", "Fixes <code>#12</code>"),
            ("fixes: #12", "fixes: <code>#12</code>"),
            ("CLOSED fixture/repo#12", "CLOSED <code>fixture/repo#12</code>"),
            ("Resolve https://github.com/o/r/issues/12", "Resolve <code>https://github.com/o/r/issues/12</code>"),
            ("**Fixes** #12", "**Fixes** <code>#12</code>"),
            ("resolved GH-12", "resolved <code>GH-12</code>"),
            ("example.com/@someone", "example.com/<code>@someone</code>"),
            ("\\@someone", "\\ <code>@someone</code>"),
            ("a ` stray\n\n@someone", "a ` stray\n\n<code>@someone</code>"),
            ("- one ` tick\n- @someone ` end", "- one ` tick\n- <code>@someone</code> ` end"),
            ("`a\\` @someone `b`", "`a\\` <code>@someone</code> `b`"),
            ("<summary>ping `@someone`</summary>", "<summary>ping `<code>@someone</code>`</summary>"),
            ("<details>\n```\n@someone\n```\n</details>", "<details>\n```\n<code>@someone</code>\n```\n</details>"),
            ("<pre>Fixes #12</pre>", "<pre>Fixes <code>#12</code></pre>"),
            ("请@someone 看", "请<code>@someone</code> 看"),
            ("bug-fixes #12", "bug-fixes <code>#12</code>"),
            ("Fixes \\#12", "Fixes \\ <code>#12</code>"),
            ("xhttps://a.b/@someone", "xhttps://a.b/<code>@someone</code>"),
            ("http://a_b/@someone", "http://a_b/<code>@someone</code>"),
            ("www.example.com/@someone", "www.example.com/<code>@someone</code>"),
            ("https://x.y/fixes #12", "https://x.y/fixes <code>#12</code>"),
            ("<code>see @someone</code>", "<code>see <code>@someone</code></code>"),
            ("<div>https://a.b/@someone</div>", "<div>https://a.b/<code>@someone</code></div>"),
            ("- <div>`@someone`</div>", "- <div>`<code>@someone</code>`</div>"),
            ("x <b class=\"`\">@someone</b><i class=\"`\">",
             "x <b class=\"`\"><code>@someone</code></b><i class=\"`\">"),
            ("| `a | @someone` |", "| `a | <code>@someone</code>` |"),
            ("`a\nb` @someone `c\nd`", "`a\nb` <code>@someone</code> `c\nd`"),
            ("a `x\n2. y` @someone `z\nw`", "a `x\n2. y` <code>@someone</code> `z\nw`"),
            ("- item\n  ```\n  code\n@someone\n\n```\nmore\n```",
             "- item\n  ```\n  code\n<code>@someone</code>\n\n```\nmore\n```"),
            (" ```\n@someone", " ```\n<code>@someone</code>"),
            ("<pre>\n\n```\n@someone\n</pre>\n@other", "<pre>\n\n```\n<code>@someone</code>\n</pre>\n<code>@other</code>"),
            ("[link](https://example.com/`a) @someone Fixes #12 `b",
             "[link](https://example.com/`a) <code>@someone</code> Fixes <code>#12</code> `b"),
            ("[ref]: https://example.com/`a\n\n@someone `b", "[ref]: https://example.com/`a\n\n<code>@someone</code> `b"),
            ("[ref]: https://example.com/`a @someone `b", "[ref]: https://example.com/`a <code>@someone</code> `b"),
            ("_@someone_", "_<code>@someone</code>_"),
            ("__@org/team__", "__<code>@org/team</code>__"),
            ("@some_one_ ok", "<code>@some_one</code>_ ok"),
            ("| URL | Person |\n| --- | --- |\n| https://example.com/|@someone |",
             "| URL | Person |\n| --- | --- |\n| https://example.com/|<code>@someone</code> |"),
            ("@someone see `@other`","<code>@someone</code> see `@other`"),
        ]
        unchanged = ["see #12", "prefixes #12", "a@b.co", "https://example.com/@someone/post",
                     "`@someone Fixes #12`", "```\n@someone\nFixes #12\n```", "~~~\n@someone\nFixes #12\n~~~",
                     "<code>@someone</code>",
                     "<details><summary>Plan</summary>\n\n```\n@someone\n```\n\n</details>",
                     "Use `<details>` and `@decorator`", "1. step\n   ```\n   @someone\n   ```", "```\n@someone"]
        table += [(text, text) for text in unchanged]
        for text, expected in table:
            with self.subTest(text=text):
                self.assertEqual(outbound.inert(text), expected)
                self.assertEqual(outbound.inert(expected), expected)

    def test_gh_refuses_text_that_skipped_outgoing(self):
        with mock.patch.object(util, "sh_with_network", side_effect=AssertionError("reached")):
            with self.assertRaisesRegex(TypeError, "gh.outgoing"):
                gh.gh(["issue", "comment", "5", "--body-file", "-"], input="@someone")
            with self.assertRaisesRegex(TypeError, "gh.outgoing"):
                gh.gh(["issue", "close", "5", "--comment", "@someone"])
            for args in (["issue", "comment", "5", "-b", "@someone"], ["api", "x", "-f", "body=@someone"],
                         ["pr", "create", "--body=@someone"], ["pr", "create", "--body-file", "/tmp/x"],
                         ["issue", "comment", "5", "-b@someone"], ["api", "x", "-fbody=@someone"],
                         ["api", "repos/o/r/issues/5/comments", "--input", "/tmp/body.json"],
                         ["api", "x", "--input=/tmp/body.json"]):
                with self.subTest(args=args), self.assertRaisesRegex(TypeError, "gh.outgoing"):
                    gh.gh(args)
        self.assertTrue(gh.outgoing("x", "Closes #5").startswith("Closes #5\n\n"))


class UploadGuardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.evidence = self.root / "runs" / "run" / "evidence"
        self.browser = self.root / "runs" / "run" / "browser"
        self.outside = self.root / "outside.txt"
        for d in (self.evidence, self.browser):
            d.mkdir(parents=True)
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
        return {"tool_name": uploads.TOOL, "tool_input": {"paths": [str(n) for n in names]}}

    def assertDenied(self, payload, rule, shown=None):
        reason = uploads.decide(payload)
        self.assertIn(rule, reason)
        self.assertIn(str(shown or payload["tool_input"]["paths"][-1]), reason)
        self.assertIn(str(self.evidence), reason)
        _, out = self.call(payload)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.note.call_args.args[0].error, reason)

    def test_files_in_the_run_folders_are_allowed(self):
        good = self.evidence / "a.png"
        also = self.browser / "b.txt"
        good.write_text("x")
        also.write_text("x")
        self.assertEqual(uploads.decide(self.upload(good, also)), "")
        self.assertEqual(self.call(self.upload(good, also)), (0, ""))
        self.note.assert_not_called()

    def test_other_files_are_denied(self):
        (self.evidence / "leak.txt").symlink_to(self.outside)
        for name in (".env", "id_rsa", "x.pem"):
            (self.evidence / name).write_text("x")
        (self.evidence / "plain.txt").symlink_to(self.evidence / ".env")
        cases = [(self.outside, uploads.OUTSIDE), ("rel.png", uploads.OUTSIDE),
                 (self.evidence / "leak.txt", uploads.OUTSIDE), (self.evidence / ".env", uploads.SECRET),
                 (self.evidence / "id_rsa", uploads.SECRET), (self.evidence / "x.pem", uploads.SECRET),
                 (self.evidence / "plain.txt", uploads.SECRET), (self.evidence / "none.png", uploads.MISSING),
                 (self.evidence, uploads.MISSING)]
        for path, rule in cases:
            with self.subTest(path=str(path)):
                self.assertDenied(self.upload(path), rule)

    def test_one_bad_path_denies_the_whole_call(self):
        good = self.evidence / "a.png"
        good.write_text("x")
        self.assertDenied(self.upload(good, self.outside), uploads.OUTSIDE, self.outside)

    def test_closing_the_chooser_is_allowed(self):
        for args in ({"paths": []}, {}):
            self.assertEqual(self.call({"tool_name": uploads.TOOL, "tool_input": args}), (0, ""))

    def test_errors_deny(self):
        good = self.evidence / "a.png"
        good.write_text("x")
        bad = [("not json", None), ({"tool_input": {"paths": str(good)}}, None),
               ({"tool_input": {"paths": [1]}}, None), ({"tool_input": "x"}, None)]
        for payload, _ in bad:
            with self.subTest(payload=payload):
                _, out = self.call(payload, payload if isinstance(payload, str) else None)
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        with mock.patch.dict(os.environ, {"ARETE_RUN_ID": ""}):
            _, out = self.call(self.upload(good))
            self.assertIn("deny", out)
        with mock.patch.object(uploads, "decide", side_effect=RuntimeError):
            self.assertIn("deny", self.call(self.upload(good))[1])
        self.note.side_effect = OSError
        self.assertIn("deny", self.call(self.upload(self.outside))[1])

    def test_cli_denies_and_fails_closed(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.upload(self.outside)))), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(cli.main(["upload-guard"]), 0)
        self.assertIn("deny", out.getvalue())
        with mock.patch.object(uploads, "main", side_effect=RuntimeError), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(cli.main(["upload-guard"]), 2)

    def launch(self, which="claude", guarded=False, remote=None):
        picked = SimpleNamespace(candidate=SimpleNamespace(runner=which, effort="", instructions=()), selected=None, budget=None)
        trip = SimpleNamespace(cfg=SimpleNamespace(jev=SimpleNamespace(guard=guarded),
                                                   environment=SimpleNamespace(providers=())), repo=None, scratch=None,
                               target=SimpleNamespace(remote=remote), tokens=None, hard_cap=False)
        tools = [uploads.TOOL, "mcp__playwright__browser_click"]
        with mock.patch.object(runner.environment, "passed", return_value=[]):
            return runner.launch_kwargs({"allowed_tools": tools}, picked, trip, ("r", "s"), False,
                                        [].append, cwd=Path.cwd())

    def test_only_local_claude_gets_the_tool_and_its_hook(self):
        for guarded in (False, True):
            kw = self.launch(guarded=guarded)
            groups = kw["settings"]["hooks"]["PreToolUse"]
            self.assertIn(uploads.TOOL, kw["allowed_tools"])
            self.assertEqual([g["matcher"] for g in groups if "playwright" in g["matcher"]], [uploads.TOOL])
            self.assertIn(guard.MATCHER, [g["matcher"] for g in groups])
        for which, remote in (("codex", None), ("pi", None), ("claude", SimpleNamespace())):
            with self.subTest(which=which, remote=bool(remote)):
                kw = self.launch(which, remote=remote)
                self.assertEqual(kw["allowed_tools"], ["mcp__playwright__browser_click"])
                self.assertIn(uploads.WITHHELD, kw["system"])

    def test_codex_refuses_the_tool_and_keeps_other_tools(self):
        payload = {"tool_name": uploads.TOOL, "tool_input": {"paths": []}}
        self.assertEqual(codex.guard_reason(payload, False, []), uploads.CODEX_REFUSAL)
        click = {"tool_name": "mcp__playwright__browser_click", "tool_input": {}}
        self.assertEqual(codex.guard_reason(click, False, []), "")

    def test_the_tool_list_prompt_and_server_limit(self):
        self.assertIn("browser_file_upload", verify.PLAYWRIGHT_TOOLS)
        for name in ("browser_evaluate", "browser_run_code_unsafe"):
            self.assertNotIn(name, verify.PLAYWRIGHT_TOOLS)
        self.assertNotIn("--allow-unrestricted-file-access", verify.mcp_config(self.evidence))
        note = pipeline.surface_note(SimpleNamespace(start="x"), "http://127.0.0.1:1", self.evidence)
        self.assertIn("`browser_file_upload`", note)
        self.assertIn(f"copy a worktree file into `{self.evidence}`", note)


if __name__ == "__main__":
    unittest.main()
