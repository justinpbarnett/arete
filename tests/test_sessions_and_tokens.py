import base64
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest import mock

from arete import config, control, devices, gh, pipeline, sessions, verify

from tests import support
from tests.harness import behavior_result, make_folders, run_script, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class RemoteSessionTests(unittest.TestCase):
    SESSION = (SCRIPTS / "remote_session.py").read_text()

    def session(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), HOME=str(root / "home"),
                              GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            run = run_script(self.SESSION, root, str(root), scenario, env=env, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr[-2000:])
            return behavior_result(run, marker="")

    @staticmethod
    def subcommand(args):
        i = 0
        while i < len(args) and args[i] == "-c":
            i += 2
        return args[i] if i < len(args) else ""

    def test_work_returns_through_the_controller_and_the_device_never_pushes(self):
        out = self.session("normal")
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["hub"], out["tip"])
        self.assertEqual(out["pushed"], out["tip"])
        self.assertEqual(out["tree"], ["a.txt", "notes.txt"])
        self.assertEqual(out["asked"], [{"repositories": ["widgets"], "permissions": {"contents": "read"}}])
        self.assertEqual(len(out["pushes"]), 1)
        self.assertFalse([c for c in out["calls"] if " push" in c])
        self.assertEqual(out["leaks"], [])
        self.assertTrue(out["records"])
        self.assertNotIn("push", [self.subcommand(r["args"]) for r in out["records"]])

    def test_the_device_holds_the_token_only_for_the_clone(self):
        out = self.session("normal")
        value = "fake-clone-value-0001"
        self.assertEqual(out["asked"], [{"repositories": ["widgets"], "permissions": {"contents": "read"}}])
        self.assertEqual(out["token_calls"], [0])
        self.assertEqual(out["in_scripts"], [])
        holding = [r for r in out["records"] if r["token"]]
        self.assertTrue([r for r in holding if self.subcommand(r["args"]) == "clone"])
        self.assertTrue(all(r["call"] == "0" for r in holding))
        for r in out["records"]:
            if self.subcommand(r["args"]) in ("checkout", "config", "rev-parse", "reset", "add", "commit", "status", "bundle"):
                self.assertFalse(r["token"], r)
            self.assertFalse([a for a in r["args"] if value in a])
        self.assertEqual(out["device_leaks"], [])
        self.assertEqual(out["helpers"], "")
        self.assertNotIn("@", out["origin_url"])
        self.assertEqual(out["home_files"], [])

    def test_a_commit_that_adds_then_deletes_a_denied_path_holds_the_push(self):
        out = self.session("env-added-then-deleted")
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["tree"], ["a.txt", "notes.txt"])
        self.assertEqual((out["hub"], out["pushes"], out["pushed"]), ("", [], ""))
        self.assertTrue([h for h in out["held"].split(", ") if h.startswith(".env in ")], out["held"])
        self.assertEqual(out["controller_branch"], out["tip"])
        self.assertEqual(out["fetched"], out["tip"])
        self.assertIn("not pushed", out["summary"])
        self.assertNotIn("push", [self.subcommand(r["args"]) for r in out["records"]])

    def test_a_merged_orphan_side_and_the_merge_itself_are_checked(self):
        out = self.session("orphan-side-merged")
        self.assertEqual(out["status"], "done")
        self.assertIn(".github/workflows/ci.yml in ", out["held"])
        self.assertIn("config/.env.local in ", out["held"])
        self.assertEqual((out["hub"], out["pushes"]), ("", []))

    def test_a_task_session_pushes_asks_for_its_report_and_leaves_the_lease(self):
        out = self.session("task")
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["hub"], out["tip"])
        self.assertEqual(out["pushes_when_asked"], [1])
        self.assertEqual(out["lease_exits"], [True])
        report = out["report"]
        self.assertEqual((report["branch"], report["commit"]), ("session/ses-remote", out["tip"]))
        self.assertEqual([c["result"] for c in report["checks"]], ["pass"])
        self.assertEqual(report["problems"], [])

    def test_a_push_that_succeeds_on_the_final_retry_is_what_the_report_says(self):
        out = self.session("task-retry")
        self.assertEqual(out["status"], "done")
        self.assertEqual(len(out["pushes"]), 3)
        self.assertEqual(out["report"]["commit"], out["tip"])
        self.assertEqual(out["report"]["problems"], [])

    def test_a_remote_repo_is_refused_until_the_app_is_configured(self):
        box = devices.Device("box", "ssh", host="dev@box.test", dir="/work")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key.pem"
            key.write_text("x\n")
            repo = config.Repo("widgets", root, github="octo/widgets")

            def refusal(cfg):
                with self.assertRaises(control.Refused) as caught:
                    sessions.repo_named("widgets", box, cfg)
                return str(caught.exception)

            said = refusal(config.Config(repos=[repo], github_app=config.GithubApp()))
            for word in ("app_id", "installation_id", "private_key", "[github_app]"):
                self.assertIn(word, said)
            said = refusal(config.Config(repos=[repo], github_app=config.GithubApp("1", "2")))
            self.assertIn("private_key", said)
            self.assertNotIn("app_id", said)
            self.assertNotIn("installation_id", said)
            self.assertIn("is not a file", refusal(config.Config(
                repos=[repo], github_app=config.GithubApp("1", "2", root / "missing.pem"))))
            bare = config.Repo("widgets", root)
            self.assertIn("no github setting", refusal(config.Config(
                repos=[bare], github_app=config.GithubApp("1", "2", key))))

    def test_a_local_session_still_works_in_a_worktree(self):
        out = self.session("local")
        self.assertTrue(Path(out["cwd"]).resolve().is_relative_to(Path(out["scratch"]).resolve()))
        self.assertIn(Path(out["cwd"]).name, out["worktrees"])
        self.assertEqual((out["branch"], out["github"]), ("session/ses-remote", ""))
        self.assertEqual((out["asked"], out["calls"]), ([], []))

    def test_a_failed_bring_back_fails_the_session_and_keeps_the_device(self):
        out = self.session("bundle-fails")
        self.assertEqual(out["status"], "failed")
        self.assertEqual((out["hub"], out["pushes"]), ("", []))
        self.assertIn("device went away", out["unreturned"])
        self.assertIn("did not come back", out["summary"])
        kept, log, forgotten = SimpleNamespace(device=SimpleNamespace(keep=False), name="vm", delete_command=lambda: "rm"), [], []
        with mock.patch.object(devices.state, "forget_vm", forgotten.append), \
                mock.patch.object(devices, "delete_or_say_why", side_effect=AssertionError("deleted")), \
                devices.kept_or_deleted(kept, log.append, lambda: "the work is not back"):
            pass
        self.assertEqual((forgotten, len(log)), (["vm"], 1))
        with mock.patch.object(devices, "delete_or_say_why") as deleted, devices.kept_or_deleted(kept, log.append):
            pass
        deleted.assert_called_once()

    def test_any_interrupted_return_marks_the_work_unreturned(self):
        spec = sessions.Spec("ses-y", "p", repo="widgets", github="octo/widgets", branch="session/ses-y")
        cfg = SimpleNamespace(repo=lambda name: SimpleNamespace(path=Path("/nowhere")))
        for failure in (OSError("disk full"), KeyError("odd")):
            with self.subTest(failure=repr(failure)), mock.patch.object(sessions, "save_spec"), \
                    mock.patch.object(sessions, "log_line"), mock.patch.object(sessions, "bring_back", side_effect=failure):
                spec.unreturned = ""
                try:
                    sessions.return_work(spec, cfg, None, "m", last=True)
                except KeyError:
                    pass
                self.assertTrue(spec.unreturned)
                self.assertTrue(sessions.unreturned_reason(spec))

    def test_the_end_note_compares_the_push_with_the_latest_fetch(self):
        spec = sessions.Spec("ses-x", "p", repo="widgets", github="octo/widgets", branch="session/ses-x",
                             cloned="c" * 40, fetched="b" * 40, pushed="a" * 40)
        self.assertIn("not pushed", sessions.work_note(spec))
        self.assertIn("aaaaaaaaaa was pushed earlier", sessions.work_note(spec))
        spec.held = ".github/workflows/ci.yml"
        self.assertIn("denied paths .github/workflows/ci.yml", sessions.work_note(spec))
        spec.pushed = spec.fetched
        self.assertIn("is pushed as bbbbbbbbbb", sessions.work_note(spec))
        spec.unreturned = "bundle failed"
        self.assertIn("did not come back", sessions.work_note(spec))

    def test_a_device_error_is_redacted_before_it_is_cut_short(self):
        value = "fake-clone-value-0001"
        device = devices.Device("box", "ssh", host="dev@box.test")
        said = subprocess.CompletedProcess([], 1, "", "fatal: " + "x" * 280 + value)
        with mock.patch.object(sessions.util, "sh", return_value=said), \
                self.assertRaises(devices.DeviceError) as caught:
            sessions.run_script(device, "true", "cloning", value)
        self.assertNotIn("fake-c", str(caught.exception))
        self.assertIn("***", str(caught.exception))


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
                mock.patch.object(gh, "key_signature", return_value=b"signature"), \
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

    def test_app_jwt_verifies_with_the_public_key(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"HOME": directory}):
            root = Path(directory)
            key, pub = root / "key.pem", root / "pub.pem"
            subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
            subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)], check=True, capture_output=True)
            token = gh.app_jwt(config.GithubApp("12345", "67890", key))
            head, claims, signature = token.split(".")
            (root / "signed").write_text(f"{head}.{claims}")
            (root / "sig").write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
            checked = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(root / "sig"),
                                      str(root / "signed")], capture_output=True, text=True, check=False)
            self.assertIn("Verified OK", checked.stdout)


class TokenHelperTests(unittest.TestCase):
    VALUE = "fake-clone-value-0001"

    def fill(self, protocol, host, token=None):
        with tempfile.TemporaryDirectory() as home:
            env = {"PATH": os.environ["PATH"], "HOME": home, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0"}
            if token is not None:
                env["ARETE_GIT_TOKEN"] = token
            return subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=" + sessions.TOKEN_HELPER,
                                   "credential", "fill"], cwd=home, env=env, capture_output=True, text=True,
                                  input=f"protocol={protocol}\nhost={host}\n\n", check=False)

    def test_the_helper_answers_github_over_https_with_the_token(self):
        lines = self.fill("https", "github.com", self.VALUE).stdout.splitlines()
        self.assertIn("username=x-access-token", lines)
        self.assertIn(f"password={self.VALUE}", lines)

    def test_the_helper_answers_nothing_for_another_host_or_protocol(self):
        for protocol, host in (("https", "example.com"), ("http", "github.com"), ("https", "github.com.example.com")):
            with self.subTest(protocol=protocol, host=host):
                other = self.fill(protocol, host, self.VALUE)
                self.assertNotEqual(other.returncode, 0)
                self.assertNotIn(self.VALUE, other.stdout + other.stderr)

    def test_the_helper_answers_nothing_without_the_token(self):
        for token in (None, ""):
            with self.subTest(token=token):
                bare = self.fill("https", "github.com", token)
                self.assertNotEqual(bare.returncode, 0)
                self.assertNotIn("password=", bare.stdout)
                self.assertNotIn("x-access-token", bare.stdout)


class PushedPrDraftTests(unittest.TestCase):
    URL ="https://github.com/fixture/repo/pull/7"

    def setUp(self):
        from arete import gh, jev
        self.gh, self.jev = gh, jev
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]

        def sh(*args):
            return subprocess.run([*git, *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

        sh("init", "-b", "main")
        (self.root / "a.txt").write_text("a\n")
        sh("add", ".")
        sh("commit", "-m", "base")
        sh("update-ref", "refs/remotes/origin/main", "HEAD")
        sh("checkout", "-b", "work")
        (self.root / "a.txt").write_text("b\n")
        sh("commit", "-am", "change")
        self.head = sh("rev-parse", "HEAD")
        self.calls, self.logs, self.said = [], [], []
        self.draft, self.remote_head, self.score, self.fail = True, self.head, 0.1, False
        self.body, self.listed = "", True
        self.run = SimpleNamespace(id="x", repo=config.Repo(name="fixture", path=self.root), repairs="", feedback=[],
                                   facts={}, log=self.logs.append)
        self.layer = SimpleNamespace(trust_level="automerge")
        self.cfg = config.Config()
        self.c = pipeline.Checkout("", "fixture/repo", self.root, self.root, "main", pr=self.URL, lease=self.head)
        for target, name, fake in [(gh, "gh", self.fake_gh), (jev, "ready", lambda *a, **k: True),
                                   (jev, "ask", self.fake_ask)]:
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_gh(self, args, input=None, **kw):
        self.calls.append((list(args), input))
        if args[:2] == ["pr", "list"]:
            return json.dumps([{"url": self.URL}] if self.listed else [])
        if args[:2] == ["pr", "create"]:
            self.draft = "--draft" in args
            return self.URL
        if args[:2] == ["pr", "edit"]:
            self.body = input
        if args[:2] == ["pr", "ready"]:
            self.draft = "--undo" in args
        if args[:2] == ["pr", "view"] and "isDraft,headRefOid" in args:
            if self.fail:
                raise self.gh.GhError("gh pr view: boom")
            return json.dumps({"isDraft": self.draft, "headRefOid": self.remote_head})
        if args[:2] == ["pr", "view"] and "body" in args:
            return json.dumps({"body": self.body})
        if args[:2] == ["pr", "view"] and "title" in args:
            return json.dumps({"title": "feat: Title"})
        if args[:2] == ["pr", "view"]:
            return json.dumps({"mergeStateStatus": "DRAFT" if self.draft else "CLEAN", "mergeable": "MERGEABLE"})
        if args[0] == "api":
            return json.dumps({"allow_auto_merge": False})
        return ""

    def fake_ask(self, *a, **k):
        answers = {n: self.jev.Answer(self.score, 1.0) for n in self.jev.RISK}
        answers["irreversible"] = self.jev.Answer(False, 1.0, {"yes": 0.0})
        return self.jev.Reply(answers=answers)

    def names(self, *prefix):
        return [a for a, _ in self.calls if a[:len(prefix)] == list(prefix)]

    def reads(self):
        return [a for a, _ in self.calls if "isDraft,headRefOid" in a]

    def found_run(self, held=False, feedback=False, pushed=False, moved=False, preset=False, legacy=False, fault=False):
        from arete import flows, tracker
        root = Path(tempfile.mkdtemp(dir=self.root))
        origin, wt = root / "origin.git", root / "wt"
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]

        def sh(cwd, *args):
            return subprocess.run([*git, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

        sh(root, "init", "-q", "--bare", "-b", "main", str(origin))
        sh(root, "clone", "-q", str(origin), str(wt))
        (wt / "a.txt").write_text("a\n")
        sh(wt, "add", ".")
        sh(wt, "commit", "-qm", "base")
        sh(wt, "push", "-q", "origin", "main")
        sh(wt, "checkout", "-qb", "work")
        (wt / "a.txt").write_text("b\n")
        sh(wt, "commit", "-qam", "change by the earlier run")
        sh(wt, "push", "-q", "-u", "origin", "work")
        c = pipeline.Checkout("", "fixture/repo", wt, wt, "main", pr=self.URL if preset else "",
                              lease=sh(wt, "rev-parse", "HEAD"), resumes=True)
        self.remote_head = "f" * 40 if moved else c.lease
        self.calls.clear()
        self.logs.clear()
        self.said.clear()
        tk = SimpleNamespace(pr_link=lambda t, whole=True: "link", pr_title=lambda s, t: s, pr_labels=[])
        self.run = SimpleNamespace(id="x", repo=config.Repo(name="fixture", path=wt,
                                                            parts=[config.Part("", "fixture/repo")]),
                                   repairs="", feedback=["comment"] if feedback else [], feedback_prs=[],
                                   log=self.logs.append, tracker=tk, enter=lambda s: None, say=self.said.append)
        layer = config.RepoLayer(dir=Path())
        layer.trust_level = "automerge"
        verified = verify.Verified("blocked" if held or fault else "pass",
                                   blocked_by="the dev server did not start" if fault else "the JEV guard refused it")
        job = flows.Job(config.Config(), self.run, root / "ws", ticket=tracker.Ticket("123", "#123", "Title"),
                        branch="work", checkouts=[c], layer=layer, verified=verified, review={"verdict": "approve"})
        staged = pipeline.Staged(counts={"a.txt": 1}, changed=[c])
        job.built = SimpleNamespace(restage=lambda from_base=False: staged if from_base or pushed else pipeline.Staged(),
                                    from_base=False, committed_lines=0, staged=pipeline.Staged(),
                                    push=lambda branch, message: None)
        with mock.patch.multiple(pipeline, evidence_images=lambda *a, **k: {}, report_body=lambda *a, **k: "report",
                                 synced_pr_body=lambda run, url, body, t, left: ("", body),
                                 answer_feedback=lambda *a, **k: None):
            out = flows.pr(SimpleNamespace(ctx=job), str(wt))
            stored = {k: v for k, v in out.data.items() if k not in ("found", "drafted", "ci_stopped")}
            job.published(stored if legacy else out.data, stored=False)
            flows.publish(SimpleNamespace(ctx=job))
        return out.data

    def test_found_draft_pr_not_held_is_marked_ready_then_merged(self):
        data = self.found_run()
        self.assertEqual((data["found"], data["opened"]), (["fixture/repo"], ["fixture/repo"]))
        self.assertEqual(self.names("pr", "create"), [])
        self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL]])
        self.assertEqual(len(self.names("pr", "merge")), 1)
        self.assertEqual(self.names("pr", "merge")[0][-2:], ["--subject", f"feat: Title (#{gh.pr_number(self.URL)})"])
        self.assertFalse(self.draft)
        self.assertIn(f"pr: marked {self.URL} ready for review", self.logs)

    def test_found_draft_pr_with_an_approved_review_and_a_check_blocked_by_the_environment_is_marked_ready(self):
        self.found_run(fault=True)
        self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL]])
        self.assertFalse(self.draft)

    def test_found_ready_pr_held_is_made_a_draft(self):
        for pushed in (False, True):
            with self.subTest(pushed=pushed):
                self.draft = False
                data = self.found_run(held=True, pushed=pushed)
                self.assertEqual((data["committed"], data["drafted"]), (pushed, ["fixture/repo"]))
                self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL, "--undo"]])
                self.assertEqual(self.names("pr", "merge"), [])
                self.assertTrue(self.draft)
                self.assertTrue(self.said[-1].endswith("The PRs are drafts: the check in use was blocked. the JEV guard refused it"))

    def test_found_pr_in_a_revise_run_is_left_alone(self):
        for draft, held in ((True, False), (False, True), (False, False)):
            with self.subTest(draft=draft, held=held):
                self.draft = draft
                data = self.found_run(held=held, feedback=True)
                self.assertEqual(data["drafted"], [])
                self.assertEqual((self.reads(), self.names("pr", "ready"), self.names("pr", "merge")), ([], [], []))
                self.assertEqual(self.draft, draft)

    def test_found_pr_with_moved_head_is_left_alone(self):
        for draft, held in ((True, False), (False, True)):
            with self.subTest(draft=draft, held=held):
                self.draft = draft
                self.found_run(held=held, moved=True)
                self.assertEqual((self.names("pr", "ready"), self.names("pr", "merge")), ([], []))
                self.assertEqual(self.draft, draft)
                self.assertTrue(any(line.startswith(f"pr: left {self.URL} as it is: its head is ffffffffffff")
                                    for line in self.logs))

    def test_found_pr_an_earlier_ci_watch_stopped_is_left_alone_and_keeps_its_ci_section(self):
        self.body = "intro\n<!-- arete:ci -->\n## CI\n\nArete stopped: CI failed.\n<!-- /arete:ci -->\n"
        data = self.found_run()
        self.assertEqual(data["ci_stopped"], ["fixture/repo"])
        self.assertEqual((self.reads(), self.names("pr", "ready"), self.names("pr", "merge")), ([], [], []))
        self.assertTrue(self.draft)
        self.assertTrue(self.body.startswith("link\n\nreport"))
        self.assertTrue(pipeline.ci.stopped(self.body))

    def test_pr_opened_fresh_by_this_try_behaves_as_before(self):
        self.listed = False
        data = self.found_run()
        self.assertEqual((data["found"], data["opened"]), ([], ["fixture/repo"]))
        self.assertEqual(len(self.names("pr", "create")), 1)
        self.assertEqual((self.reads(), self.names("pr", "ready")), ([], []))
        self.assertEqual(len(self.names("pr", "merge")), 1)

    def test_pr_outcome_stored_before_found_was_kept_reads_as_none_found(self):
        from arete import flows
        job = flows.Job(config.Config(), self.run, self.root / "ws", checkouts=[self.c])
        job.built = SimpleNamespace(committed_lines=0)
        job.published({"prs": {}, "opened": ["fixture/repo"], "pushed": [], "changed": ["fixture/repo"],
                       "draft_why": "", "report_body": "", "summary": "", "committed_lines": 0}, stored=False)
        self.assertEqual(job.found, [])

    def test_pr_outcome_stored_before_ci_stopped_was_kept_leaves_a_standing_pr_alone(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.draft = True
                self.found_run(preset=True, legacy=legacy)
                self.assertEqual(len(self.names("pr", "ready")), 0 if legacy else 1)
                self.assertEqual(len(self.names("pr", "merge")), 0 if legacy else 1)
                self.assertEqual(self.draft, legacy)

    def test_pushed_draft_is_marked_ready_then_merged(self):
        ready = pipeline.ready_pushed(self.run, [self.c], [], "")
        pipeline.automerge(self.cfg, self.run, self.layer, None, "", ready, "about")
        self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL]])
        self.assertEqual(len(self.names("pr", "merge")), 1)
        self.assertIn(f"pr: marked {self.URL} ready for review", self.logs)

    def test_pushed_draft_over_risk_line_stays_for_a_person(self):
        self.score = 0.9
        ready = pipeline.ready_pushed(self.run, [self.c], [], "")
        pipeline.automerge(self.cfg, self.run, self.layer, None, "", ready, "about")
        self.assertEqual(len(self.names("pr", "ready")), 1)
        self.assertEqual(self.names("pr", "merge"), [])
        comments = [i for a, i in self.calls if a[:2] == ["pr", "comment"]]
        self.assertEqual(len(comments), 1)
        self.assertIn("Automerge stays off for this PR", comments[0])

    def test_pushed_ready_pr_is_made_a_draft_when_held(self):
        self.draft = False
        why, verified = "the check in use was blocked", verify.Verified("blocked", "", [])
        drafted = pipeline.hold_pushed(self.run, [self.c], [], why)
        self.assertEqual(drafted, [self.c])
        self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL, "--undo"]])
        self.assertIn(f"{why}.", pipeline.published_note(why, verified, [], drafted))
        self.calls.clear()
        self.assertEqual(pipeline.ready_pushed(self.run, [self.c], [], why), [])
        self.assertEqual(self.calls, [])

    def test_opened_pr_behaves_as_before(self):
        pipeline.automerge(self.cfg, self.run, self.layer, None, "", [self.c], "about")
        self.assertEqual([a for a in self.calls if "isDraft,headRefOid" in a[0]], [])
        self.assertEqual(self.names("pr", "ready"), [])
        verified = verify.Verified("blocked", "", [])
        self.assertEqual(pipeline.published_note("why", verified, [self.c], []), pipeline.draft_note("why", verified))
        self.assertEqual(pipeline.published_note("", verified, [self.c], []), "")

    def test_ci_stopped_repair_and_changed_head_are_left_alone(self):
        for why in ("", "held"):
            self.assertEqual(pipeline.hold_pushed(self.run, [self.c], [self.c], why), [])
            self.assertEqual(pipeline.ready_pushed(self.run, [self.c], [self.c], why), [])
        self.assertEqual(self.calls, [])
        self.run.repairs = "old"
        self.assertEqual(pipeline.hold_pushed(self.run, [self.c], [], "held"), [])
        self.assertEqual(pipeline.ready_pushed(self.run, [self.c], [], ""), [])
        self.assertEqual(self.calls, [])
        self.run.repairs = ""
        self.remote_head = "f" * 40
        self.assertEqual(pipeline.ready_pushed(self.run, [self.c], [], ""), [])
        self.draft = False
        self.assertEqual(pipeline.hold_pushed(self.run, [self.c], [], "held"), [])
        self.assertEqual(self.names("pr", "ready"), [])
        self.assertTrue(any(f"left {self.URL} as it is" in line for line in self.logs))

    def test_unreadable_state_is_left_alone(self):
        self.fail = True
        self.assertEqual(pipeline.ready_pushed(self.run, [self.c], [], ""), [])
        self.assertIn(f"pr: left {self.URL} as it is: arete could not read its state: gh pr view: boom", self.logs)

    def test_trust_level_pr_marks_ready_without_merging(self):
        self.layer.trust_level = "pr"
        ready = pipeline.ready_pushed(self.run, [self.c], [], "")
        pipeline.automerge(self.cfg, self.run, self.layer, None, "", ready, "about")
        self.assertEqual(len(self.names("pr", "ready")), 1)
        self.assertEqual(self.names("pr", "merge"), [])


if __name__ == "__main__":
    unittest.main()
