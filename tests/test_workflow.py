import json
import shutil
import subprocess
import sysconfig
import tempfile
import threading
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from arete import (
    api_check, config, factory, flows, gh, mergecheck, orchestration, paths, pipeline, sandbox, serve, tracker, util,
    verify, workspace,
)
from arete.review import Review


class ServeDeploymentTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        origin = root / "origin.git"
        self.writer = root / "writer"
        self.checkout = root / "checkout"
        self.state = root / "state"
        self.git(root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(root, "init", "-q", "-b", "main", str(self.writer))
        self.git(self.writer, "remote", "add", "origin", str(origin))
        package = self.writer / "arete"
        package.mkdir()
        (package / "fixture.py").write_text("version = 1\n")
        self.start = self.commit(self.writer, "initial code")
        self.git(self.writer, "push", "-q", "origin", "main")
        self.git(root, "clone", "-q", str(origin), str(self.checkout))
        layer = root / "layer"
        layer.mkdir()
        (layer / "config.toml").write_text('[git.required_check]\ncontext = "deployment-check"\napp_id = 17\n')
        self.repo = config.Repo("arete", self.checkout, github="fixture/arete", layer=layer)
        self.cfg = config.Config(repos=[self.repo])
        self.enterContext(mock.patch.multiple(paths, PACKAGE_DIR=self.checkout / "arete", STATE_DIR=self.state,
                                             LOCKS_DIR=self.state / "locks", RUNS_DIR=self.state / "runs",
                                             LOG_DIR=root / "logs", WORKTREES_DIR=root / "worktrees"))
        self.server = object.__new__(serve.Server)
        self.server.checkout_update = threading.Lock()
        self.server.deploy_refusals = set()
        self.server.say = mock.Mock()
        self.ci_state = ("completed", "success")
        self.gh_json = gh.gh_json
        self.ci = self.enterContext(mock.patch.object(gh, "gh_json", side_effect=self.ci_response))
        self.gate = self.enterContext(mock.patch.object(gh, "merge_ci_passed", wraps=gh.merge_ci_passed))
        self.tip = self.publish("merged code")

    def git(self, cwd, *args):
        return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                                        "-c", "commit.gpgsign=false", *args], cwd=cwd, text=True, timeout=5).strip()

    def commit(self, cwd, subject):
        self.git(cwd, "add", ".")
        self.git(cwd, "commit", "-qm", subject, "--allow-empty")
        return self.git(cwd, "rev-parse", "HEAD")

    def publish(self, subject):
        head = self.commit(self.writer, subject)
        self.git(self.writer, "push", "-q", "origin", "main")
        return head

    def ci_response(self, args, **kwargs):
        if args[:2] == ["repo", "view"]:
            self.assertEqual(args[2], self.repo.github)
            return {"defaultBranchRef": {"name": "main"}}
        endpoint = args[1]
        self.assertTrue(endpoint.startswith(f"repos/{self.repo.github}/commits/"), endpoint)
        head, query = endpoint.split("/")[4:6]
        if query.startswith("statuses?"):
            return [[]]
        self.assertTrue(query.startswith("check-runs?"), endpoint)
        status, conclusion = self.ci_state
        return [{"check_runs": [{"head_sha": head, "name": "deployment-check", "app": {"id": 17},
                                 "status": status, "conclusion": conclusion}]}]

    def deploy(self):
        self.server.deploy(self.cfg)
        return self.git(self.checkout, "rev-parse", "HEAD")

    def test_ci_rerun_goes_pending_while_waiting_for_repo_lock(self):
        attempting = threading.Event()
        repo_lock = util.repo_lock

        @contextmanager
        def observe_lock(src):
            attempting.set()
            with repo_lock(src):
                yield

        with mock.patch.object(util, "repo_lock", observe_lock):
            self.check_ci_rerun_while_waiting(repo_lock(self.checkout), attempting)

    def test_ci_rerun_goes_pending_while_waiting_for_checkout_guard(self):
        attempting = threading.Event()
        guard = self.server.checkout_update

        @contextmanager
        def observe_guard():
            attempting.set()
            with guard:
                yield

        self.server.checkout_update = observe_guard()
        self.check_ci_rerun_while_waiting(guard, attempting)

    def check_ci_rerun_while_waiting(self, held, attempting):
        with ThreadPoolExecutor(1) as worker, mock.patch.object(util, "git", wraps=util.git) as git:
            with held:
                deployment = worker.submit(self.deploy)
                self.assertTrue(attempting.wait(3))
                self.gate.assert_not_called()
                self.assertFalse(any(call.args[0][0] == "api" for call in self.ci.call_args_list))
                self.assertFalse(any(call.args[0] == ["rev-parse", "--verify", "refs/remotes/origin/main"]
                                     for call in git.call_args_list))
                self.ci_state = ("in_progress", None)
            self.assertEqual(deployment.result(timeout=5), self.start)
        self.gate.assert_called_once_with(self.repo.github, self.tip,
                                          required={"context": "deployment-check", "app_id": 17})
        self.assertIn("CI pending", self.server.say.call_args.args[0])

    def test_network_loss_under_repo_lock_allows_cleanup_and_heartbeat(self):
        checking = threading.Event()
        cleaning = threading.Event()
        repo_lock, sh = util.repo_lock, util.sh
        wt = self.state.parent / "kept"
        checkout = workspace.Checkout("", self.repo.github, self.checkout, wt, "main")
        beat = self.state / "serve.json"

        @contextmanager
        def observe_lock(src):
            if checking.is_set():
                cleaning.set()
            with repo_lock(src):
                yield

        def serve_request(command, **kwargs):
            if command[0] != "gh":
                return sh(command, **kwargs)
            if command[1] == "repo":
                return subprocess.CompletedProcess(command, 0, json.dumps(self.ci_response(command[1:])), "")
            checking.set()
            self.assertTrue(cleaning.wait(5))
            self.assertLessEqual(kwargs["timeout"], serve.DEPLOY_API_TIMEOUT)
            return subprocess.CompletedProcess(command, 1, "", "connection reset")

        def clean():
            self.assertTrue(workspace.remove_workspace(wt, [checkout]))

        with mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")):
            server = serve.Server()
        server.say = self.server.say
        server.tick = clean
        server.deploy_when_due = mock.Mock()
        server.watch_alerts = mock.Mock()
        with (mock.patch.object(gh, "gh_json", self.gh_json), mock.patch.object(util, "sh", side_effect=serve_request),
              mock.patch.object(util, "repo_lock", observe_lock),
              mock.patch.object(util, "wait_for_network", return_value=False) as wait,
              mock.patch.object(serve.control, "BEAT_FILE", beat),
              mock.patch.object(serve.loop_store, "pending", return_value=[]),
              mock.patch.object(serve.run_store, "runs_marked_running", return_value=[]),
              ThreadPoolExecutor(2) as worker):
            deployment = worker.submit(server.deploy, self.cfg)
            self.assertTrue(checking.wait(5))
            tick = worker.submit(server.run_pass)
            deployment.result(timeout=5)
            tick.result(timeout=5)
            wait.assert_not_called()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.start)
        heartbeat = json.loads(beat.read_text())
        self.assertEqual(heartbeat["ticks"], 1)
        self.assertTrue(heartbeat["tick_ok"], heartbeat["failing"])
        self.assertIn("CI unavailable", server.say.call_args.args[0])

    def test_live_pilot_receipt_blocks_until_held_or_spent(self):
        receipt = self.state / "pilot-doc-fix" / "task" / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"phase": "waiting_for_owner_merge"}))
        self.assertEqual(self.deploy(), self.start)
        self.assertEqual(self.deploy(), self.start)
        self.gate.assert_not_called()
        self.server.say.assert_called_once()
        self.assertIn("live pilot receipt", self.server.say.call_args.args[0])
        for terminal in ("held", "spent", "healthy"):
            with self.subTest(terminal=terminal):
                tip = self.tip if terminal == "held" else self.publish(f"merge after {terminal}")
                receipt.write_text(json.dumps({"phase": terminal}))
                self.assertEqual(self.deploy(), tip)

    def test_refusal_logs_once_per_tip_and_reason(self):
        for status, conclusion in (("queued", None), ("completed", "failure"), ("queued", None)):
            self.ci_state = (status, conclusion)
            self.assertEqual(self.deploy(), self.start)
        other = self.publish("second merge")
        self.assertEqual(self.deploy(), self.start)
        messages = [call.args[0] for call in self.server.say.call_args_list]
        self.assertEqual([message.split(" left alone for ")[1] for message in messages],
                         [f"{self.tip} (CI pending)", f"{self.tip} (CI failed)", f"{other} (CI pending)"])
        self.ci_state = ("completed", "success")
        self.assertEqual(self.deploy(), other)
        self.assertIn(f"fast forwarded to {other} (second merge)", self.server.say.call_args.args[0])

    def test_origin_ref_moving_during_ci_is_rechecked(self):
        def pass_ci(*args, **kwargs):
            newer = self.publish("another merge")
            self.git(self.checkout, "fetch", "-q", "origin", "main")
            self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), newer)
            return True

        self.gate.side_effect = pass_ci
        self.assertEqual(self.deploy(), self.start)
        self.gate.assert_called_once_with(self.repo.github, self.tip,
                                          required={"context": "deployment-check", "app_id": 17})


class DeploymentCiTests(unittest.TestCase):
    def test_ci_lookup_and_classification_share_a_single_attempt_budget(self):
        now = 0
        timeouts = []
        repo, tip = "fixture/arete", "a" * 40
        required = {"context": "deployment-check", "app_id": 17}

        def sh(command, *, timeout, **kwargs):
            nonlocal now
            timeouts.append(timeout)
            now += 3
            endpoint = command[2]
            if "check_name=" in endpoint:
                return subprocess.CompletedProcess(command, 1, "", "connection reset")
            if "/statuses?" in endpoint:
                return subprocess.CompletedProcess(command, 0, "[[]]", "")
            self.assertIn("/check-runs?", endpoint)
            result = [{"check_runs": [{"head_sha": tip, "name": required["context"], "app": {"id": required["app_id"]},
                                       "status": "in_progress", "conclusion": None}]}]
            return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

        with (mock.patch.object(util, "sh", side_effect=sh),
              mock.patch.object(util, "wait_for_network", return_value=False) as wait,
              mock.patch.object(gh, "time", SimpleNamespace(monotonic=lambda: now))):
            self.assertEqual(gh.deployment_ci_refusal(repo, tip, required, timeout=9), "CI unavailable")
            self.assertEqual(timeouts, [9, 6, 3])
            wait.assert_not_called()
            self.assertEqual(gh.deployment_ci_refusal(repo, tip, required, timeout=6), "CI unavailable")
            self.assertEqual(timeouts, [9, 6, 3, 6, 3])
            wait.assert_not_called()
            with self.assertRaises(gh.GhError):
                gh.gh_json(["api", "unrelated?check_name=fixture"])
            wait.assert_called_once_with()
            self.assertEqual(timeouts[-1], 120)


class ServeStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.env = api_check.isolated_env(self.root)

    def break_candidate_import(self, module):
        candidate = self.root / "arete"
        shutil.copytree(Path(serve.__file__).parent, candidate, ignore=shutil.ignore_patterns("__pycache__"))
        (candidate / module).write_text('raise RuntimeError("broken deployment fixture")\n')

    def check_revisions(self, env, state_dir, *args):
        running, checkout = "a" * 12, "b" * 12
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "serve.json").write_text(json.dumps({
            "at": "2026-11-21T00:00:00Z", "engine": running, "checkout_head": checkout,
        }))
        installed = Path(sysconfig.get_path("scripts")) / "arete"
        result = subprocess.run([str(installed), "status", *args], cwd=self.root,
                                env={**env, "PYTHONPATH": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("broken deployment fixture", result.stderr)
        self.assertIn(f"running code {running}, checkout HEAD {checkout}", result.stdout)

    def test_installed_status_reads_revisions_when_candidate_common_raises(self):
        self.break_candidate_import("commands/common.py")
        self.check_revisions(self.env, Path(self.env["ARETE_STATE_DIR"]), "--limit", "1")

    def test_installed_status_reads_revisions_when_candidate_paths_raises(self):
        self.break_candidate_import("paths.py")
        self.check_revisions(self.env, Path(self.env["ARETE_STATE_DIR"]))

    def test_installed_status_finds_default_state_when_candidate_paths_raises(self):
        self.break_candidate_import("paths.py")
        del self.env["ARETE_CONFIG_DIR"], self.env["ARETE_STATE_DIR"]
        self.check_revisions(self.env, Path(self.env["HOME"]) / ".local/share/arete")

    def test_installed_status_finds_scratch_state_when_candidate_paths_raises(self):
        self.break_candidate_import("paths.py")
        scratch = self.root / ".arete-scratch-0123456789abcdef"
        scratch.mkdir(mode=0o700)
        del self.env["ARETE_CONFIG_DIR"], self.env["ARETE_STATE_DIR"]
        self.check_revisions({**self.env, "ARETE_VERIFY_SCRATCH": str(scratch)}, scratch / "state")


class SavedRunPolicyTests(unittest.TestCase):
    def test_pre_upgrade_lineage_accepts_unchanged_policy_and_refuses_changes(self):
        repo = config.Repo("fixture", Path("/fixture/repo"), github="fixture/repo")
        cfg = config.Config(repos=[repo], loop_dirs=[])
        run = pipeline.AgentRun("saved-run", repo, Path("/fixture/run"), cfg=cfg, kind="fix")
        controller = object.__new__(orchestration.Controller)
        controller.run = run
        controller.job = SimpleNamespace(cfg=cfg, run=run, ticket=tracker.Ticket("1", "#1", "Fixture"),
                                         expertise="", attachments="")
        # Generated by Controller.lineage() at 65efb2d3a8a8 with this fixture.
        saved_policy = "ab6163c172351fe95614fa84a09973fb8fa04bad4bbb9c0dffc4cce50369b70d"
        controller.data = {"policy_sha": saved_policy}
        controller.continuation_blocker = lambda **_: ""
        original = "[limits]\nmax_diff_lines = 200\n"
        with tempfile.NamedTemporaryFile() as file:
            source = Path(file.name)
            source.write_text(original)
            controller.job.layer = run.layer = config.load_layer(repo.layer_dir, source_file=source)
            self.assertEqual(controller.lineage()[0]["policy_sha"], saved_policy)

            for changed in ("[limits]\nmax_diff_lines = 201\n",
                            original + '[git.required_check]\ncontext = "deployment-check"\napp_id = 17\n'):
                with self.subTest(layer=changed):
                    source.write_text(changed)
                    controller.job.layer = run.layer = config.load_layer(repo.layer_dir, source_file=source)
                    with self.assertRaisesRegex(factory.Refused, "policy_sha changed"):
                        controller.lineage()
        self.assertEqual(controller.data["policy_sha"], saved_policy)


class AskConfigTests(unittest.TestCase):
    def test_expire_days_is_a_finite_number_of_at_most_ten_years(self):
        for entry in ("expire_days = inf", "expire_days = nan", "expire_days = 999999", "expire_days = 0",
                      "expire_days = true", 'expire_days = "7"'):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                config.ask_table(tomllib.loads(entry))
        self.assertEqual(config.ask_table({}).expire_days, 7)
        self.assertEqual(config.ask_table(tomllib.loads("expire_days = 0.5")).expire_days, 0.5)
        longest = config.ask_table({"expire_days": config.MAX_ASK_EXPIRE_DAYS}).expire_days
        self.assertTrue(util.iso(util.now() - timedelta(days=longest)))


class JourneyVerificationTests(unittest.TestCase):
    def setUp(self):
        self.example = Path(__file__).resolve().parents[1] / "docs/examples/product-verification"
        self.ticket = tracker.Ticket("1", "#1", "Check journey:verifier-prompt-inputs")
        self.run = mock.Mock(tid=1, repo=config.Repo("arete", self.example), dir=self.example,
                             add_dirs=[], owner_note="")
        self.run.time_for.side_effect = lambda seconds: seconds
        self.run.agent.return_value = SimpleNamespace(ok=True, structured={
            "verdict": "pass", "summary": "The configured journey passed.", "checks": [],
        })
        self.work = pipeline.TicketWork(config.Config(), self.run, self.ticket, self.example, [],
                                        config.load_layer(self.example), 60, "test workspace")
        self.scratch = self.enterContext(mock.patch.object(verify.scratch, "make"))
        self.scratch.return_value = SimpleNamespace(root=str(self.example / "scratch"))
        self.enterContext(mock.patch.object(verify.scratch, "end", return_value=("", [], [])))
        self.enterContext(mock.patch.object(pipeline, "run_folder", return_value=self.example / "evidence"))
        self.enterContext(mock.patch.object(pipeline, "restore_worktrees"))

    def verify(self):
        return pipeline.use_product(self.work, "verify", "verify", goal="Check the CLI output.", summary="",
                                    diff="")

    def passing_driver(self):
        process = mock.Mock(returncode=0, communicate=mock.Mock(return_value=("Driver observed valid prompt", "")))
        self.enterContext(mock.patch.object(verify.scratch, "service_command", return_value=["driver"]))
        self.enterContext(mock.patch.object(verify.scratch, "spawn", return_value=process))
        self.enterContext(mock.patch.object(util, "stop_group"))

    def test_passing_driver_cannot_supply_missing_verifier_checks(self):
        self.ticket.body = "Also inspect the catalog."
        self.work.layer.trust_level = "automerge"
        self.run.agent.return_value.runner = "codex"
        self.enterContext(mock.patch.object(pipeline, "verification_trees", return_value={"": "candidate"}))
        self.passing_driver()

        result = self.verify()

        self.assertEqual(result.verdict, "blocked")
        self.assertEqual(result.blocked_by, "the verifier said pass but listed no checks")
        self.assertEqual(result.native_trees, {})
        self.assertIn("Driver observed valid prompt", self.run.save.call_args.args[1])

    def test_merge_check_replays_numbered_verifier_step_without_the_driver(self):
        self.passing_driver()
        self.run.agent.return_value.structured["checks"] = [
            {"what": "1. Inspect the catalog", "how": "echo catalog", "saw": "catalog", "passed": True},
            {"what": "journey:verifier-prompt-inputs", "how": "echo prompt", "saw": "prompt", "passed": True},
        ]

        result = self.verify()
        self.assertEqual(result.verdict, "pass")
        report = self.run.save.call_args.args[1]
        self.assertIn(self.work.layer.verify.drivers["prompt-inputs"], report)
        self.assertIn("Driver observed valid prompt", report)
        with tempfile.NamedTemporaryFile(suffix=".md") as saved:
            path = Path(saved.name)
            path.write_text(report)
            with mock.patch.object(mergecheck, "reports", return_value=("build", path)):
                steps = mergecheck.report_steps({"run_id": "build", "pr": 646})

        self.assertEqual([step.goal(n) for n, step in enumerate(steps, 1)], [
            "1. [build step 1] Inspect the catalog: echo catalog",
            "2. [build step 2] journey:verifier-prompt-inputs: echo prompt",
        ])

    def test_linked_map_cannot_redirect_an_owner_configured_binding(self):
        with (tempfile.NamedTemporaryFile(suffix=".toml") as source,
              tempfile.NamedTemporaryFile(suffix=".toml") as linked):
            config_file, map_file = Path(source.name), Path(linked.name)
            configured = (self.example / "config.toml").read_text().replace("feature-map.toml", map_file.name)
            config_file.write_text(configured + '\nother = "echo other"\n')
            map_file.write_text((self.example / "feature-map.toml").read_text())
            journey = config.load_layer(self.example, source_file=config_file).verify.feature_map["verifier-prompt-inputs"]
            self.assertEqual(journey.driver, "prompt-inputs")

            with map_file.open("a") as stream:
                stream.write('driver = "other"\n')
            with self.assertRaisesRegex(ValueError, r"bind drivers in \[verify\.journey_drivers\]"):
                config.load_layer(self.example, source_file=config_file)

    def test_orchestrated_plan_worker_sees_the_configured_journey(self):
        checkout = workspace.Checkout("", "arete", self.example, self.example, "main")
        job = flows.Job(self.work.cfg, self.run, self.example, ticket=self.ticket, layer=self.work.layer,
                        checkouts=[checkout])
        controller = orchestration.Controller.__new__(orchestration.Controller)
        controller.job, controller.run, controller.lease = job, self.run, "fixture"
        controller.workers, controller.data, controller.policy = {}, {}, config.Orchestration()
        controller.catalog = mock.Mock()
        setting = SimpleNamespace(step="plan", tools=("Read",), runner="codex", model="fixture")
        controller.catalog.worker.return_value = setting, "plan", False
        args = {"depends_on": [], "tool_bundle": "read", "context_refs": [], "goal": "Plan", "inputs": {}}
        with (mock.patch.object(orchestration.ledger, "actions", return_value=[]),
              mock.patch.object(orchestration.ledger, "transition"),
              mock.patch.object(orchestration.delegation, "Worker"),
              mock.patch.object(orchestration.delegation, "request") as request):
            controller.worker_start({"id": "plan-1"}, args)

        prompt = request.call_args.args[2]
        self.assertIn("## Product feature map\n", prompt)
        self.assertIn("`journey:verifier-prompt-inputs` (configured driver available)", prompt)
        self.assertIn("Read the verification prompt's JSON record.", prompt)

    def test_stored_evidence_requires_the_same_ticket_selection_with_a_map(self):
        job = flows.Job(self.work.cfg, self.run, self.example, ticket=self.ticket, layer=self.work.layer,
                        goal="Check the CLI output.")
        passing = factory.Outcome("pass", data={"policy": job.policy("verify", {})})
        self.ticket.body = "Repeat journey:verifier-prompt-inputs."
        self.assertTrue(job.accepts("verify", passing, {}))
        self.ticket.title, self.ticket.body = "Check prompt inputs", ""
        self.assertFalse(job.accepts("verify", passing, {}))

        job.layer.verify = config.Verify(url="")
        passing.data["policy"] = job.policy("verify", {})
        self.ticket.body = "Check journey:verifier-prompt-inputs."
        self.assertTrue(job.accepts("verify", passing, {}))

    def test_requested_driver_runs_after_probes_in_journey_order_and_reports_observations(self):
        self.ticket.body = "Also check journey:another-journey and journey:verifier-prompt-inputs."
        self.run.agent.return_value.structured["checks"] = [
            {"what": "Inspect the catalog", "how": "echo catalog", "saw": "catalog", "passed": True},
        ]
        configured = self.work.layer.verify
        configured.feature_map["another-journey"] = replace(configured.feature_map["verifier-prompt-inputs"],
                                                           driver="another-driver")
        configured.drivers["another-driver"] = "echo {port} {scratch}"
        self.enterContext(mock.patch.object(verify.browser, "free_port", return_value=4242))
        configured.probes["ready"] = "echo ready"
        spawn = self.enterContext(mock.patch.object(verify.scratch, "spawn"))
        spawn.side_effect = [mock.Mock(returncode=0, communicate=mock.Mock(return_value=(out, "")))
                             for out in ("ready", "first output", "second output")]
        confined = self.enterContext(mock.patch.object(verify.scratch, "service_command", side_effect=lambda _, cmd: [cmd]))
        self.enterContext(mock.patch.object(util, "stop_group"))

        result = self.verify()

        self.assertEqual(result.verdict, "pass")
        self.assertEqual([c["what"] for c in result.checks], [
            "ready", "journey:another-journey", "journey:verifier-prompt-inputs", "Inspect the catalog",
        ])
        self.assertEqual(result.checks[2]["saw"], "exit 0: second output")
        self.assertEqual([call.args[1] for call in confined.call_args_list],
                         ["echo ready", f"echo 4242 {self.scratch.return_value.root}",
                          configured.drivers["prompt-inputs"]])
        self.assertTrue(all(call.kwargs["actor_confined"] for call in spawn.call_args_list))
        self.assertIn("Expected outcome", self.run.agent.call_args.args[1])
        self.assertIn("exit 0: second output", self.run.agent.call_args.args[1])
        self.assertIn("exit 0: second output", self.run.save.call_args.args[1])

    def test_assertion_failure_and_unavailable_command_have_distinct_results(self):
        self.enterContext(mock.patch.object(verify.scratch, "service_command", return_value=["driver"]))
        spawn = self.enterContext(mock.patch.object(verify.scratch, "spawn"))
        self.enterContext(mock.patch.object(util, "stop_group"))
        for code, stdout, stderr, verdict in ((1, "", "unexpected product response: EPERM", "fail"),
                                               (1, "", "sandbox-exec: confinement unavailable", "blocked"),
                                               (127, "", "driver: command not found", "blocked")):
            with self.subTest(code=code, stderr=stderr):
                spawn.return_value = mock.Mock(returncode=code, communicate=mock.Mock(return_value=(stdout, stderr)))

                result = self.verify()

                self.assertEqual(result.verdict, verdict)
                self.assertEqual(result.finished, verdict == "fail")
                self.assertIn("journey:verifier-prompt-inputs", result.summary)
                self.assertEqual(result.checks[0]["saw"], f"exit {code}: {stdout or stderr}")
        self.run.agent.assert_not_called()

    def test_driver_timeout_reports_the_journey_and_stops_the_process_group(self):
        self.enterContext(mock.patch.object(verify.scratch, "service_command", return_value=["driver"]))
        process = mock.Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired("driver", 1)
        self.enterContext(mock.patch.object(verify.scratch, "spawn", return_value=process))
        stop = self.enterContext(mock.patch.object(util, "stop_group"))

        result = self.verify()

        self.assertEqual(result.verdict, "blocked")
        self.assertIn("journey:verifier-prompt-inputs", result.blocked_by)
        self.assertIn("Timed out waiting for the driver", result.checks[0]["saw"])
        stop.assert_called_once_with(process)
        self.run.agent.assert_not_called()

    def test_driver_requires_trusted_scratch(self):
        self.scratch.side_effect = sandbox.ConfinementUnavailable("fixture authority unavailable")
        with mock.patch.object(verify, "run_probes") as commands:
            result = self.verify()
        self.assertEqual(result.verdict, "blocked")
        self.assertEqual(result.blocked_by, "fixture authority unavailable")
        self.assertIn("journey:verifier-prompt-inputs", result.summary)
        commands.assert_not_called()
        self.run.agent.assert_not_called()

    def test_map_guides_the_verifier_without_scheduling_an_unrequested_driver(self):
        self.ticket.title = "Check the verifier prompt inputs"
        self.run.agent.return_value.structured["checks"] = [
            {"what": "Verify prompt inputs", "how": "Open the prompt listing", "saw": "Valid prompt", "passed": True},
        ]
        with mock.patch.object(verify, "run_probes") as commands:
            result = self.verify()
        self.assertEqual(result.verdict, "pass")
        self.assertIn("journey:verifier-prompt-inputs", self.run.agent.call_args.args[1])
        commands.assert_not_called()

    def test_unknown_reference_blocks_only_when_map_is_configured(self):
        self.ticket.title = "Check journey:missing"
        result = self.verify()
        self.assertEqual(result.verdict, "blocked")
        self.assertEqual(result.blocked_by, "journey:missing")
        self.scratch.assert_not_called()
        self.run.agent.assert_not_called()

        self.work.layer.verify = config.Verify(url="")
        self.scratch.side_effect = OSError("fixture authority unavailable")
        self.run.agent.return_value.structured["checks"] = [
            {"what": "Ordinary CLI check", "how": "echo ready", "saw": "ready", "passed": True},
        ]
        with mock.patch.object(verify, "run_probes") as commands:
            result = self.verify()
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.checks[0]["what"], "Ordinary CLI check")
        self.assertNotIn("Product feature map", self.run.agent.call_args.args[1])
        commands.assert_not_called()


class ReviewTests(unittest.TestCase):
    BLOCKER: ClassVar[dict] = {"file": "notes.txt", "problem": "fixture blocker", "severity": "blocker", "category": "issue"}

    def test_security_blocker_cannot_be_waived(self):
        blocker = {**self.BLOCKER, "category": "security"}
        result = Review.judged({"summary": "Waived.", "problems": [],
                                "earlier": [{"id": "B1", "status": "waived", "why": "out of scope"}]},
                               [blocker], [], {})
        self.assertFalse(result.approved())
        self.assertEqual(result.blocking()[0]["status"], "open")

    def test_unanswered_earlier_blocker_stays_open(self):
        result = Review.judged({"summary": "Fine.", "problems": []}, [self.BLOCKER], [], {})
        self.assertFalse(result.approved())
        self.assertEqual(result.blocking()[0]["id"], "B1")

    def test_severity_outside_the_scale_still_blocks(self):
        result = Review.judged({"summary": "Fine.", "problems": [{**self.BLOCKER, "severity": "Critical"}]}, [], [], {})
        self.assertFalse(result.approved())
        self.assertEqual(len(result.blocking()), 1)

    def test_review_answer_without_a_problems_list_does_not_approve(self):
        build = SimpleNamespace(run=mock.Mock(), brief=mock.Mock(), report="", checkouts=[],
                                staged=SimpleNamespace(patch=""), ws=None, read_bash=[])
        build.run.agent.return_value = SimpleNamespace(ok=True, structured={"verdict": "approve", "summary": "Fine."})
        with mock.patch.object(pipeline, "compare_with_fork", return_value=""):
            result = pipeline._review(build, "", mock.Mock())
        self.assertFalse(result.approved())
        self.assertTrue(result.failed)
