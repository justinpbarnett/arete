import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from arete import codebase, config, control, gh, orchestration, paths, pilot, serve


class ServeDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.origin = self.root / "origin"
        self.checkout = self.root / "checkout"
        self.git(self.root, "init", "-q", "-b", "stable", str(self.origin))
        (self.origin / "arete").mkdir()
        self.original = self.commit(self.origin, "initial")
        self.git(self.root, "clone", "-q", str(self.origin), str(self.checkout))
        self.tip = self.commit(self.origin, "merged")
        self.package = self.checkout / "arete"
        self.cfg = config.Config(repos=[config.Repo("arete", self.checkout, github="example/arete")])
        locks = self.root / "locks"
        locks.mkdir()
        self.enterContext(mock.patch.object(paths, "LOCKS_DIR", locks))
        self.enterContext(mock.patch.object(paths, "ensure_dirs"))
        self.enterContext(mock.patch.object(paths, "PACKAGE_DIR", self.package))
        self.enterContext(mock.patch.object(codebase, "CHECKOUT", self.checkout))
        self.enterContext(mock.patch.object(codebase, "STAMP", self.original[:12]))
        self.enterContext(mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")))
        self.server = serve.Server()
        self.server.say = mock.Mock()
        self.addCleanup(self.server.pool.shutdown)
        self.set_passing_ci(self.tip)
        self.api = self.enterContext(mock.patch.object(gh, "gh_json", side_effect=self.github))

    def git(self, folder, *args):
        done = subprocess.run(["git", "-c", "user.name=Tester", "-c", "user.email=tester@example.test",
                               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                              cwd=folder, capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def commit(self, folder, subject):
        (folder / "arete" / "engine.py").write_text(f"revision = {subject!r}\n")
        self.git(folder, "add", "arete/engine.py")
        self.git(folder, "commit", "-qm", subject)
        return self.git(folder, "rev-parse", "HEAD")

    def head(self):
        return self.git(self.checkout, "rev-parse", "HEAD")

    def required_check(self, sha, **overrides):
        return {"id": 300, "head_sha": sha, "name": "arete-checks", "app": {"id": 15368},
                "status": "completed", "conclusion": "success", **overrides}

    def workflow(self, sha, **overrides):
        return {"id": 200, "run_number": 1, "run_attempt": 1, "head_sha": sha,
                "head_branch": "stable", "event": "push", "status": "completed", "conclusion": "success", **overrides}

    def set_passing_ci(self, sha):
        self.workflows = [self.workflow(sha)]
        self.jobs = [{"name": "arete-checks", "status": "completed", "conclusion": "success",
                      "check_run_url": "https://api.github.com/repos/example/arete/check-runs/300"}]
        self.check = self.required_check(sha)

    def github(self, args, **_options):
        if args[:2] == ["repo", "view"]:
            return {"defaultBranchRef": {"name": "stable"}}
        endpoint = args[1]
        if "/actions/workflows/ci.yml/runs?" in endpoint:
            return [{"workflow_runs": self.workflows}]
        if "/actions/runs/200/attempts/1/jobs?" in endpoint:
            return [{"jobs": self.jobs}]
        self.assertEqual(endpoint, "repos/example/arete/check-runs/300")
        return self.check

    def deploy(self):
        self.server.deploy(self.cfg)

    def notes(self):
        return [call.args[0] for call in self.server.say.call_args_list]

    def test_exact_required_check_must_pass_before_fast_forward(self):
        (self.checkout / "untracked").write_text("keep me")
        for result in (self.required_check(self.original), self.required_check(self.tip, name="other"),
                       self.required_check(self.tip, app={"id": 123}), self.required_check(self.tip, id=301),
                       self.required_check(self.tip, status="in_progress", conclusion=None),
                       self.required_check(self.tip, conclusion="failure"),
                       self.required_check(self.tip, status="queued", conclusion=None)):
            self.check = result
            self.deploy()
            self.assertEqual(self.head(), self.original)
        self.check = self.required_check(self.tip)
        self.deploy()
        self.deploy()
        self.assertEqual(self.head(), self.tip)
        self.assertEqual((self.checkout / "untracked").read_text(), "keep me")
        self.assertEqual(len(self.notes()), 3)
        self.assertTrue(self.notes()[0].endswith(f"{self.tip} (CI pending)"))
        self.assertTrue(self.notes()[1].endswith(f"{self.tip} (CI failed)"))
        self.assertTrue(self.notes()[2].endswith(f"fast forwarded to {self.tip} (merged)"))

    def test_only_latest_default_branch_push_workflow_can_qualify(self):
        green = self.workflow(self.tip)
        self.workflows = [self.workflow(self.tip, event="pull_request"),
                          self.workflow(self.tip, head_branch="topic"), self.workflow(self.original)]
        self.deploy()
        self.assertEqual(self.head(), self.original)
        calls = [call.args[0] for call in self.api.call_args_list if call.args[0][0] == "api"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(parse_qs(urlsplit(calls[0][1]).query),
                         {"event": ["push"], "branch": ["stable"], "head_sha": [self.tip], "per_page": ["100"]})
        for newer in (self.workflow(self.tip, run_attempt=2, status="queued", conclusion=None),
                      self.workflow(self.tip, id=201, run_number=2, conclusion="failure")):
            self.workflows = [green, newer]
            self.deploy()
            self.assertEqual(self.head(), self.original)
        self.workflows = [green]
        [job] = self.jobs
        self.jobs = [{**job, "name": "other"}]
        self.deploy()
        self.assertEqual(self.head(), self.original)
        self.jobs = [job]
        self.deploy()
        self.assertEqual(self.head(), self.tip)

    def test_ignored_file_collision_keeps_file_and_head(self):
        collision = self.checkout / "local-settings.txt"
        (self.checkout / ".git" / "info" / "exclude").write_text(f"/{collision.name}\n")
        collision.write_text("operator's local settings\n")
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        (self.origin / collision.name).write_text("tracked settings\n")
        self.git(self.origin, "add", collision.name)
        self.git(self.origin, "commit", "-qm", "track settings")
        tip = self.git(self.origin, "rev-parse", "HEAD")
        self.set_passing_ci(tip)

        self.deploy()
        self.deploy()

        self.assertEqual(self.head(), self.original)
        self.assertEqual(collision.read_text(), "operator's local settings\n")
        [note] = self.notes()
        self.assertIn(f"left alone for {tip}", note)
        self.assertIn("would be overwritten", note)

    def test_checkout_refusals_are_logged_once_per_tip_and_reason(self):
        changed = self.package / "engine.py"
        changed.write_text("local edit")
        self.deploy()
        self.git(self.checkout, "switch", "-qc", "topic")
        self.deploy()
        self.git(self.checkout, "switch", "-q", "stable")
        self.deploy()
        self.assertEqual(changed.read_text(), "local edit")
        self.git(self.checkout, "restore", "arete/engine.py")
        local = self.commit(self.checkout, "local commit")
        self.deploy()
        next_tip = self.commit(self.origin, "another merge")
        self.deploy()
        self.assertEqual(self.head(), local)
        self.assertFalse(any(call.args[0][0] == "api" for call in self.api.call_args_list))
        self.assertEqual([note.split("left alone for ")[1] for note in self.notes()],
                         [f"{self.tip} (dirty)", f"{self.tip} (on another branch)", f"{self.tip} (diverged)",
                          f"{next_tip} (diverged)"])

    def test_checkout_and_remote_tip_are_rechecked_after_ci(self):
        changed = self.package / "engine.py"

        def edit_during_ci(args, **options):
            if "/check-runs/" in args[1]:
                changed.write_text("edit while checking CI")
            return self.github(args, **options)

        self.api.side_effect = edit_during_ci
        self.deploy()
        self.assertEqual(self.head(), self.original)
        self.assertTrue(self.notes()[-1].endswith(f"{self.tip} (dirty)"))
        self.git(self.checkout, "restore", "arete/engine.py")
        next_tip = self.commit(self.origin, "new tip")

        def fetch_during_ci(args, **options):
            if "/check-runs/" in args[1]:
                self.git(self.checkout, "fetch", "-q", "origin")
            return self.github(args, **options)

        self.api.side_effect = fetch_during_ci
        self.deploy()
        self.assertEqual(self.head(), self.original)
        self.api.side_effect = self.github
        self.set_passing_ci(next_tip)
        self.deploy()
        self.assertEqual(self.head(), next_tip)

    def test_failed_import_leaves_running_commit_older_than_checkout_in_status(self):
        package = Path(serve.__file__).parent
        for source in (*package.rglob("*.py"), package / "store" / "migrations" / "latest.txt"):
            target = self.origin / "arete" / source.relative_to(package)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        (self.origin / "arete" / "pilot.py").write_text("def broken(\n")
        self.git(self.origin, "add", "arete")
        self.git(self.origin, "commit", "-qm", "import-broken candidate")
        tip = self.git(self.origin, "rev-parse", "HEAD")
        self.set_passing_ci(tip)
        self.server.deploy_when_due()
        self.server.deployment.result(timeout=5)

        env = {**os.environ, "PYTHONPATH": str(self.checkout), "ARETE_CONFIG_DIR": str(self.root),
               "ARETE_STATE_DIR": str(self.root)}
        env.pop(paths.SCRATCH_ENV, None)

        def child(args, timeout=15):
            return subprocess.run(args, cwd=self.checkout, env=env, capture_output=True, text=True,
                                  timeout=min(timeout, 15))

        with mock.patch.object(serve.util, "sh", side_effect=child):
            self.server.load_new_code()
            self.server.load_new_code()
        self.assertIn("new code does not import", self.notes()[-1])
        with (mock.patch.object(control, "BEAT_FILE", self.root / "serve.json"),
              mock.patch.object(serve.loop_store, "pending", return_value=[]),
              mock.patch.object(serve.run_store, "runs_marked_running", return_value=[])):
            self.server.beat()
        self.assertEqual(self.head(), tip)
        launcher = Path(sys.executable).with_name("arete")
        entrypoint = [str(launcher)] if launcher.is_file() else ["-m", "arete.cli"]
        status = child([sys.executable, *entrypoint, "status"])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("arete: cannot import", status.stderr)
        self.assertIn("SyntaxError", status.stderr)
        self.assertIn("pilot.py", status.stderr)
        self.assertIn(f"running code {self.original[:12]}", status.stdout)
        self.assertIn(f"checkout HEAD {tip[:12]}", status.stdout)

    def pilot_deployment(self, before_final_lock, wait_healthy):
        repo = self.cfg.repos[0]
        layer = config.RepoLayer(repo.layer_dir)
        signal = pilot.Signal(repo.github, self.original, "before", "after")
        tree = self.git(self.origin, "rev-parse", f"{self.tip}^{{tree}}")
        self.git(self.origin, "update-ref", "refs/pull/1/head", self.tip)
        url = f"https://github.com/{signal.github}/pull/1"
        run = {"id": "fixture", "pr_url": url}
        checkout = {"pr": url, "github": signal.github, "base": "stable", "dir": "", "lease": self.tip}
        data = {"policy_sha": orchestration.policy_fingerprint(self.cfg, layer, ()),
                "limits": asdict(layer.orchestration), "published_tree": pilot.orchestration.digest({repo.github: tree})}
        owner_merged = False

        def owner_merge(_repo, _number, _head, ready_at=None):
            nonlocal owner_merged
            if ready_at:
                owner_merged = True
                return self.tip
            return None

        def admitting():
            before_final_lock()
            return nullcontext()

        receipt = self.root / "pilot.json"
        with (mock.patch.multiple(pilot, BRANCH="stable", check_origin=mock.Mock(), protected=mock.Mock(),
                                  origin=lambda _: self.tip if owner_merged else self.original,
                                  owner_merge=owner_merge, scheduled_run=mock.Mock(return_value=run),
                                  published_proof=mock.Mock(return_value=(data, checkout)),
                                  task_matches=mock.Mock(return_value=True), exact_edit=mock.Mock(return_value=tree),
                                  require_evidence=mock.Mock(), healthy=mock.Mock(), wait_healthy=wait_healthy),
              mock.patch.object(pilot.slots, "admitting", side_effect=admitting),
              mock.patch.object(pilot.runs, "runs_marked_running", return_value=[{"kind": "pipeline"}]),
              mock.patch.object(pilot.jev, "ready", return_value=True),
              mock.patch.object(pilot.jev, "risk", return_value=pilot.jev.Risk(0, 0, {}, "")),
              mock.patch.object(pilot.delegation, "identity_of", return_value="fixture"),
              mock.patch.object(pilot.tasks, "task_add"), mock.patch.object(gh, "merge_readiness", return_value=("", "")),
              mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(config, "load_layer", return_value=layer),
              mock.patch.object(pilot, "git", wraps=pilot.git) as git):
            pilot.execute(self.cfg, repo, layer, signal, "after", receipt, 1)
        self.assertFalse(any(call.args[1] == "merge" for call in git.call_args_list))

    def test_pilot_checks_health_when_serve_deployed_exact_target_first(self):
        receipt = self.root / "pilot.json"

        def health_wait(_deadline, deployed_at):
            row = json.loads(receipt.read_text())
            self.assertEqual(row["phase"], "deployed_waiting_for_health")
            self.assertEqual(row["deployed_at"], deployed_at)
            self.assertEqual(self.head(), self.tip)

        wait_healthy = mock.Mock(side_effect=health_wait)
        self.pilot_deployment(self.deploy, wait_healthy)
        wait_healthy.assert_called_once()
        self.assertEqual(json.loads(receipt.read_text())["phase"], "healthy")

    def test_pilot_holds_when_checkout_moved_to_an_unrelated_head(self):
        wait_healthy = mock.Mock()
        with self.assertRaisesRegex(pilot.Held, "checkout changed or is dirty"):
            self.pilot_deployment(lambda: self.commit(self.checkout, "unrelated local commit"), wait_healthy)
        wait_healthy.assert_not_called()
        row = json.loads((self.root / "pilot.json").read_text())
        self.assertEqual(row["phase"], "held")
        self.assertEqual(row["stopped_at"], "merged")

    def test_non_git_package_does_not_contact_github(self):
        release = self.root / "release"
        release.mkdir()
        cfg = config.Config(repos=[config.Repo("arete", release, github="example/arete")])
        with mock.patch.object(paths, "PACKAGE_DIR", release):
            self.server.deploy(cfg)
        self.api.assert_not_called()
        self.assertEqual(self.notes(), [])
