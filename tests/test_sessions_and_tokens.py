import io
import json
import shlex
import subprocess
import tempfile
import unittest
import urllib.error
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import config, diffguard, gh, jev, outbound, pipeline, sessions, slots, tracker, util, verify, workspace
from arete.review import Review

READ_ONLY_GRANT = {"repositories": ["widgets"], "permissions": {"contents": "read"}}


class CloneTokenTests(unittest.TestCase):
    VALUE = "fake-clone-value-0001"
    APP = config.GithubApp("12345", "67890", Path("/nowhere"))
    URL = "https://api.github.com/app/installations/67890/access_tokens"

    def good(self, **changes):
        return {"token": self.VALUE, "permissions": {"contents": "read", "metadata": "read"},
                "repository_selection": "selected", "repositories": [{"full_name": "Octo/Widgets"}], **changes}

    def mint(self, answer):
        seen = []

        class Reply:
            def __init__(self, text):
                self.text = text

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.text

        def opened(request, timeout=None):
            seen.append(request)
            if isinstance(answer, Exception):
                raise answer
            return Reply(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

        with mock.patch.object(gh.urllib.request, "urlopen", opened), \
                mock.patch.object(gh, "app_jwt", return_value="fixture-jwt"), \
                mock.patch.object(gh.util, "wait_for_network", return_value=False):
            try:
                return gh.clone_token(self.APP, "octo/widgets"), seen, None
            except gh.GhError as e:
                return None, seen, str(e)

    def test_the_mint_asks_for_read_only_on_the_one_repository(self):
        value, seen, error = self.mint(self.good())
        self.assertEqual((value, error), (self.VALUE, None))
        self.assertEqual(gh.CLONE_TOKEN_PERMISSIONS, {"contents": "read"})
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0].full_url, seen[0].get_method()), (self.URL, "POST"))
        self.assertEqual(json.loads(seen[0].data), READ_ONLY_GRANT)

    def test_the_mint_refuses_anything_but_read_on_this_one_repository(self):
        forbidden = urllib.error.HTTPError(self.URL, 403, "Forbidden", {},
                                           io.BytesIO(b'{"message": "Resource not accessible by integration"}'))
        refusals = {
            "write": (self.good(permissions={"contents": "write"}), ""),
            "extra permission": (self.good(permissions={"contents": "read", "pull_requests": "write"}), ""),
            "no permissions": ({"token": self.VALUE}, ""),
            "all repositories": (self.good(repository_selection="all", repositories=[]), ""),
            "another repository": (self.good(repositories=[{"full_name": "octo/other"}]), ""),
            "no token": (self.good(token=""), ""),
            "not json": (b"<html>", ""),
            "http 403": (forbidden, "HTTP 403"),
            "network down": (OSError("network is unreachable"), "network is unreachable"),
        }
        for name, (answer, said) in refusals.items():
            with self.subTest(name):
                value, seen, error = self.mint(answer)
                self.assertIsNone(value)
                self.assertIsNotNone(error)
                self.assertNotIn(self.VALUE, error)
                self.assertIn(said, error)
                self.assertTrue(seen)
                for request in seen:
                    self.assertEqual(json.loads(request.data), READ_ONLY_GRANT)


class JevPromptTests(unittest.TestCase):
    def test_irreversible_question_requires_rollback_compatibility_for_additive_expands(self):
        question = jev.risk_questions()["irreversible"]["instructions"]
        self.assertEqual(jev.SCREEN["irreversible"][0], question)
        self.assertIn("dropping, rewriting or backfilling stored data", question)
        self.assertIn("running a contract migration", question)
        self.assertIn("A code rollback leaves added tables, columns and indexes in place", question)
        self.assertIn("Treat a purely additive expand migration as reversible only if it preserves existing reads "
                      "and writes on the rollback target and the rollback target accepts the newer expand ledger rows",
                      question)
        self.assertIn("adding even a nullable column can break an existing INSERT without a column list", question)


class PushedPrDraftTests(unittest.TestCase):
    URL = "https://github.com/fixture/repo/pull/7"

    def setUp(self):
        from arete.store import db
        from tests import support

        state_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(state_root))
        db.migrate()
        # Checkout contents and model replies are inputs; publication rules stay real.
        self.root = Path("/fixture")
        self.head = "a" * 40
        self.calls, self.logs = [], []
        self.draft, self.remote_head, self.score, self.fail = True, self.head, 0.1, False
        self.run = SimpleNamespace(id="x", repo=config.Repo(name="fixture", path=self.root), repairs="", feedback=[],
                                   tid="7", facts={}, log=self.logs.append, waiting=lambda _: nullcontext())
        self.slot = slots.RunSlot("fixture", "7", 1)
        self.layer = config.RepoLayer(dir=self.root, trust_level="automerge", checks={"behavior": "true"})
        self.c = workspace.Checkout("", "fixture/repo", self.root, self.root, "main", pr=self.URL, lease=self.head)
        self.tree, self.base = "b" * 40, "c" * 40
        self.build = pipeline.Build(run=self.run, layer=self.layer, cfg=config.Config(), checkouts=[self.c],
                                    ticket=tracker.Ticket("7", "#7", "Fixture"), ws=self.root, workspace="fixture",
                                    timeout=0, tools=[], deny=[],
                                    checked={"tree": self.tree, "commands": self.layer.checks})
        self.verified = verify.Verified("pass", checks=[{"passed": True}], native_trees={"": self.tree})
        self.review = Review(verdict="approve", trees={"": self.tree})
        for patch in (mock.patch.object(gh, "gh", self.fake_gh),
                      mock.patch.object(jev, "ready", return_value=True),
                      mock.patch.object(jev, "ask", self.fake_ask),
                      mock.patch.object(pipeline, "git", self.fake_git),
                      mock.patch.object(workspace, "fetch"),
                      mock.patch.object(pipeline, "checkout_patch", return_value="+a change"),
                      mock.patch.object(pipeline.vault, "values", return_value=set()),
                      mock.patch.object(outbound, "keep_configured_private"),
                      mock.patch.object(outbound, "own_hosts", []),
                      mock.patch.object(outbound, "extra_hosts", set())):
            patch.start()
            self.addCleanup(patch.stop)

    def fake_gh(self, args, input=None, **kw):
        self.calls.append((list(args), input))
        if args[:2] == ["pr", "ready"]:
            self.draft = "--undo" in args
        elif args[:2] == ["pr", "view"]:
            if "isDraft,headRefOid" in args:
                if self.fail:
                    raise gh.GhError("gh pr view: boom")
                return json.dumps({"isDraft": self.draft, "headRefOid": self.remote_head})
            return json.dumps({"title": "feat: Title", "state": "OPEN", "isDraft": self.draft,
                               "headRefOid": self.remote_head, "baseRefName": self.c.base, "baseRefOid": self.base,
                               "mergeStateStatus": "DRAFT" if self.draft else "CLEAN", "mergeable": "MERGEABLE"})
        elif args[0] == "api":
            if "/check-runs?" in args[1]:
                return json.dumps([{"check_runs": [{"status": "completed", "conclusion": "success"}]}])
            if "/statuses?" in args[1]:
                return "[[]]"
            if args[:3] == ["api", "-X", "PUT"]:
                self.assertEqual(json.loads(input)["sha"], self.head)
                self.assertTrue(kw.get("write"))
                return '{"merged": true}'
            raise AssertionError(f"unexpected GitHub request: {args}")
        elif args[:2] != ["pr", "comment"]:
            raise AssertionError(f"unexpected GitHub request: {args}")
        return ""

    def fake_git(self, args, cwd):
        return self.tree if args == ["rev-parse", f"{self.head}^{{tree}}"] else self.base

    def fake_ask(self, *a, **k):
        answers = {n: jev.Answer(self.score, 1.0) for n in jev.RISK}
        answers["irreversible"] = jev.Answer(False, 1.0, {"yes": 0.0})
        return jev.Reply(answers=answers)

    def names(self, *prefix):
        return [a for a, _ in self.calls if a[:len(prefix)] == list(prefix)]

    def automerge(self, approval, why, verified, review, ready):
        pipeline.automerge(self.build, approval, why, verified, review, ready, "about",
                           slot=self.slot, branch="run", goal="verify", plan="plan")

    def test_only_approved_review_can_make_current_work_ready_and_merge(self):
        cases = [
            ("missing", Review.stored({}), self.verified, False),
            ("changes requested", Review.stored({"verdict": "request_changes"}), self.verified, False),
            ("review failed", Review.failed_step([], [], {}), self.verified, False),
            ("approved", self.review, self.verified, True),
            ("guard refused", Review.stored({"verdict": "approve"}),
             verify.Verified("blocked", blocked_by="the JEV guard refused it"), False),
        ]
        for name, review, verified, allowed in cases:
            with self.subTest(name=name):
                self.calls.clear()
                self.draft = True
                why = pipeline.draft_reason(review, verified)
                self.assertEqual(bool(why), not allowed)
                ready = pipeline.ready_pushed(self.run, [self.c], [], why)
                self.automerge(None, why, verified, review, ready)
                self.assertEqual(ready, [self.c] if allowed else [])
                self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL]] if allowed else [])
                self.assertEqual(len(self.names("api", "-X", "PUT")), int(allowed))
                self.assertEqual(self.draft, not allowed)

    def test_pushed_ready_pr_is_made_a_draft_when_held(self):
        self.draft = False
        why = "the check in use was blocked"
        self.assertEqual(pipeline.hold_pushed(self.run, [self.c], [], why), [self.c])
        self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL, "--undo"]])
        self.assertTrue(self.draft)
        self.calls.clear()
        self.assertEqual(pipeline.ready_pushed(self.run, [self.c], [], why), [])
        # Even callers passing a held checkout directly cannot enable merging.
        self.automerge(None, why, self.verified, self.review, [self.c])
        self.assertEqual(self.calls, [])

    def test_person_only_work_never_automerges(self):
        for reason in ("risk", "pr trust", "triage"):
            with self.subTest(reason=reason):
                self.calls.clear()
                self.draft = True
                self.score = 0.9 if reason == "risk" else 0.1
                self.layer.trust_level = "pr" if reason == "pr trust" else "automerge"
                approval = {"sent_by": "triage"} if reason == "triage" else None
                ready = pipeline.ready_pushed(self.run, [self.c], [], "")
                self.assertEqual(ready, [self.c])
                self.automerge(approval, "", self.verified, self.review, ready)
                self.assertEqual(self.names("api", "-X", "PUT"), [])
                if reason == "risk":
                    from arete import outbox

                    items = [
                        outbox.get(it["id"])
                        for it in outbox.list_items(state="approved")
                        if it["kind"] == "comment" and it["object"] == "github:fixture/repo#7"
                    ]
                    self.assertEqual(len(items), 1)
                    self.assertIn("Automerge stays off for this PR", items[0]["revisions"][-1]["body"])

    def test_unreviewed_heads_and_person_owned_prs_are_left_alone(self):
        for reason in ("moved head", "missing head", "missing lease", "unreadable", "ci stopped", "repair", "feedback"):
            with self.subTest(reason=reason):
                self.calls.clear()
                self.remote_head = "f" * 40 if reason == "moved head" else "" if reason == "missing head" else self.head
                self.c.lease = "" if reason == "missing lease" else self.head
                self.fail = reason == "unreadable"
                self.run.repairs = "earlier" if reason == "repair" else ""
                self.run.feedback = ["owner feedback"] if reason == "feedback" else []
                stopped = [self.c] if reason == "ci stopped" else []
                self.draft = True
                ready = pipeline.ready_pushed(self.run, [self.c], stopped, "")
                self.assertEqual(ready, [])
                self.automerge(None, "", self.verified, self.review, ready)
                self.draft = False
                self.assertEqual(pipeline.hold_pushed(self.run, [self.c], stopped, "held"), [])
                self.assertEqual(self.names("pr", "ready"), [])
                self.assertEqual(self.names("api", "-X", "PUT"), [])


class StagingPathspecTests(unittest.TestCase):
    DENY = [".env", "secrets/**", "path with spaces/*.key"]

    def test_helpers_preserve_never_staged_order_and_glob_syntax(self):
        self.assertEqual(diffguard.staged_pathspec(()), list(diffguard.NEVER_STAGED))
        self.assertEqual(
            diffguard.staged_pathspec(self.DENY),
            [*diffguard.NEVER_STAGED, ":(exclude,glob).env", ":(exclude,glob)secrets/**",
             ":(exclude,glob)path with spaces/*.key"],
        )
        self.assertEqual(diffguard.denied_pathspec(()), [])
        self.assertEqual(
            diffguard.denied_pathspec(self.DENY),
            [":(glob).env", ":(glob)secrets/**", ":(glob)path with spaces/*.key"],
        )

    def test_diffguard_add_and_session_commits_generate_matching_args_and_scripts(self):
        wt = Path("/fixture/wt")
        with mock.patch.object(diffguard.util, "git") as git_mock:
            diffguard.add(wt)
        git_mock.assert_called_once_with(["add", "-A", "--", ".", *diffguard.NEVER_STAGED], wt)

        for deny in ([], self.DENY):
            with self.subTest(deny=bool(deny)):
                expected_add = ["add", "-A", "--", ".", *diffguard.NEVER_STAGED,
                                *(f":(exclude,glob){p}" for p in deny)]
                expected_status = ["status", "--porcelain", "--untracked-files=all", "--",
                                   *(f":(glob){p}" for p in deny)]

                seen_git: list[list[str]] = []
                logs: list[tuple[str, str]] = []

                def fake_git(args, cwd):
                    self.assertEqual(cwd, wt)
                    seen_git.append(list(args))
                    if args[:3] == ["diff", "--cached", "--name-only"]:
                        return "ok.txt\n"
                    if args[0] == "status":
                        return "?? .env\n"
                    if args == ["rev-parse", "HEAD"]:
                        return "1" * 40 + "\n"
                    return ""

                spec = sessions.Spec("ses-1", "prompt", cwd=str(wt), deny=list(deny))
                with mock.patch.object(sessions.util, "git", side_effect=fake_git), \
                        mock.patch.object(sessions.util, "git_as_owner") as commit_mock, \
                        mock.patch.object(sessions, "log_line", side_effect=lambda _s, k, t: logs.append((k, t))):
                    sessions.commit_local(spec, "msg")

                self.assertEqual(seen_git[0], ["reset", "-q"])
                self.assertEqual(seen_git[1], expected_add)
                self.assertEqual(seen_git[2], ["diff", "--cached", "--name-only"])
                commit_mock.assert_called_once_with(["-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "msg"], wt)
                if deny:
                    self.assertEqual(seen_git[3], expected_status)
                    self.assertEqual(seen_git[4], ["rev-parse", "HEAD"])
                    self.assertEqual(logs, [("device", "left .env out of the commit: a denied path")])
                else:
                    self.assertEqual(seen_git[3], ["rev-parse", "HEAD"])
                    self.assertEqual(logs, [])

                excluded = shlex.join([*diffguard.NEVER_STAGED, *(f":(exclude,glob){p}" for p in deny)])
                listed = shlex.join(f":(glob){p}" for p in deny)
                expected_steps = [
                    "cd ~/repo",
                    "git reset -q",
                    f"git add -A -- . {excluded}",
                    f"{{ git diff --cached --quiet || {sessions.GIT_NO_HOOKS} commit -q -m msg; }}",
                ]
                if deny:
                    expected_steps.append(f"git status --porcelain --untracked-files=all -- {listed}")
                expected_steps.append("git rev-parse HEAD")
                expected_script = " && ".join(expected_steps)
                remote_out = ("?? .env\n" if deny else "") + "2" * 40 + "\n"

                with mock.patch.object(sessions, "run_script", return_value=remote_out) as script_mock:
                    head, left_out = sessions.commit_all("box", "~/repo", "msg", list(deny))

                script_mock.assert_called_once_with("box", expected_script, "committing the session's work")
                self.assertEqual(head, "2" * 40)
                self.assertEqual(left_out, [".env"] if deny else [])

    def test_local_and_remote_staging_exclude_never_staged_and_denied_paths_in_git(self):
        for mode in ("local", "remote"):
            for deny in ([], [".env", "secrets/**"]):
                with self.subTest(mode=mode, deny=bool(deny)), tempfile.TemporaryDirectory() as tmp:
                    wt = Path(tmp) / "repo"
                    wt.mkdir()
                    util.git(["init", "-q", "-b", "main"], wt)
                    util.git(["config", "user.name", "Arete Test"], wt)
                    util.git(["config", "user.email", "test@localhost"], wt)
                    (wt / "base.txt").write_text("base\n")
                    util.git(["add", "base.txt"], wt)
                    util.git(["commit", "-q", "-m", "init"], wt)

                    (wt / "keep.txt").write_text("keep\n")
                    (wt / ".env").write_text("SECRET=1\n")
                    (wt / "secrets").mkdir()
                    (wt / "secrets" / "key.txt").write_text("k\n")
                    (wt / "__pycache__").mkdir()
                    (wt / "__pycache__" / "mod.pyc").write_bytes(b"pyc")

                    if mode == "local":
                        logs: list[str] = []
                        spec = sessions.Spec("ses-2", "prompt", cwd=str(wt), deny=list(deny))
                        with mock.patch.object(sessions, "log_line", side_effect=lambda _s, _k, t: logs.append(t)):
                            sessions.commit_local(spec, "session commit")
                        head = spec.fetched
                        left_out = logs
                    else:
                        def run_locally(_device, script, _what):
                            res = subprocess.run(["sh", "-c", script], check=True, capture_output=True, text=True)
                            return res.stdout

                        with mock.patch.object(sessions, "run_script", side_effect=run_locally):
                            head, left_out = sessions.commit_all("box", str(wt), "session commit", list(deny))

                    committed = set(util.git(["diff-tree", "--no-commit-id", "--name-only", "-r", head], wt).splitlines())
                    self.assertIn("keep.txt", committed)
                    self.assertNotIn("__pycache__/mod.pyc", committed)
                    if deny:
                        self.assertNotIn(".env", committed)
                        self.assertNotIn("secrets/key.txt", committed)
                        if mode == "local":
                            self.assertEqual(
                                sorted(left_out),
                                ["left .env out of the commit: a denied path",
                                 "left secrets/key.txt out of the commit: a denied path"],
                            )
                        else:
                            self.assertEqual(sorted(left_out), [".env", "secrets/key.txt"])
                    else:
                        self.assertIn(".env", committed)
                        self.assertIn("secrets/key.txt", committed)
                        self.assertEqual(left_out, [])


if __name__ == "__main__":
    unittest.main()
