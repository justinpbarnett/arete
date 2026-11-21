import json
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from arete import (
    config, diffguard, evals, factory, flows, gh, jev, mergecheck, miner, orchestration, pipeline, sandbox,
    tracker, util, verify, workspace,
)
from arete.agent import Result
from arete.review import Review


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


class MinerLifecycleTests(unittest.TestCase):
    BASE = "a" * 40
    HEAD = "b" * 40
    UPDATED = "c" * 40
    BRANCH = "arete/miner-20261120"
    URL = "https://github.com/example/arete/pull/8"
    PROMPT = "arete/prompts/build.md"

    def setUp(self):
        self.repo = config.Repo("arete", Path("."), github="example/arete")
        self.cfg = config.Config(repos=[self.repo])
        self.url = self.URL
        self.base_name = "main"
        self.base = self.BASE
        self.head = self.HEAD
        self.status = "success"
        self.labels = {miner.MINER_LABEL}
        self.reported_ci_head = ""
        self.ci_app = gh.GITHUB_ACTIONS_APP_ID
        self.behind = False
        self.merged = False
        self.during_merge = lambda: None
        self.calls = []
        self.comments = {}
        self.ci_reads = []
        self.prs = []
        self.stored = []
        self.root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.layer = config.RepoLayer(self.root, trust_level="automerge")
        self.load_layer = config.load_layer
        self.enterContext(mock.patch.object(config, "load_layer", return_value=self.layer))
        self.enterContext(mock.patch.multiple(
            gh, gh_json=self.github_read, gh=self.github_write, me=lambda: "operator",
        ))
        self.git_mock = self.enterContext(mock.patch.object(miner, "git", side_effect=self.git))
        self.enterContext(mock.patch.object(miner.paths, "RUNS_DIR", self.root / "runs"))
        self.enterContext(mock.patch.object(util, "repo_lock", return_value=nullcontext()))
        self.enterContext(mock.patch.object(util, "git_records", return_value=["M", self.PROMPT]))
        self.diff = self.enterContext(mock.patch.object(diffguard, "between", return_value=diffguard.Diff({self.PROMPT: 2})))
        self.compare_prompts = evals.compare_prompts
        self.eval = self.enterContext(mock.patch.object(evals, "compare_prompts",
                                                        return_value=evals.Comparison("eval table", complete=True)))
        self.proposals = self.enterContext(mock.patch.object(miner.approvals_store, "set_proposal_status_by_pr",
                                                             side_effect=self.forget_applied))
        self.enterContext(mock.patch.object(miner.approvals_store, "proposals", side_effect=lambda **kw: self.stored))
        self.enterContext(mock.patch.object(miner.approvals_store, "add_proposal", side_effect=self.store_applied))

    def store_applied(self, run_id, proposal, status, url=""):
        if status == "applied":
            self.stored.append({**proposal, "pr_url": url})

    def forget_applied(self, url, status):
        self.stored = [p for p in self.stored if p["pr_url"] != url]

    def github_read(self, args, **kwargs):
        if args[:2] == ["repo", "view"]:
            self.assertEqual(args[2], self.repo.github)
            return {"defaultBranchRef": {"name": self.base_name}}
        if args[:2] == ["api", f"repos/{self.repo.github}/issues?state=open&labels=arete-miner&per_page=100"]:
            self.assertEqual(args[2:], ["--paginate", "--slurp"])
            return [self.prs]
        if args[:3] == ["pr", "view", self.url]:
            return {"state": "MERGED" if self.merged else "OPEN"}
        if args == ["api", f"repos/{self.repo.github}/issues/8/comments?per_page=100", "--paginate", "--slurp"]:
            return [list(self.comments.values())]
        if args == ["api", f"repos/{self.repo.github}/pulls/8"]:
            return {"state": "open", "draft": False, "user": {"login": "operator"}, "body": "Why: fixes the cited failure.",
                    "labels": [{"name": label} for label in self.labels],
                    "base": {"sha": self.base, "ref": self.base_name},
                    "head": {"sha": self.head, "ref": self.BRANCH, "repo": {"full_name": self.repo.github}}}
        self.fail(f"Unexpected GitHub read {args}")

    def github_write(self, args, **kwargs):
        if args == ["pr", "comment", self.url, "--body-file", "-"]:
            self.assertTrue(kwargs["write"])
            body = kwargs["input"]
            self.assertIsInstance(body, gh.Outgoing)
            comment_id = len(self.comments) + 1
            self.comments[comment_id] = {"id": comment_id, "user": {"login": "operator"}, "body": body}
            self.calls.append(("comment", self.url, body))
            return f"{self.url}#issuecomment-{comment_id}"
        if args[:3] == ["api", "-X", "PATCH"]:
            self.assertTrue(kwargs["write"])
            self.assertIsInstance(kwargs["input"], gh.Outgoing)
            comment_id = int(args[3].rsplit("/", 1)[1])
            self.assertEqual(args[3:], [f"repos/{self.repo.github}/issues/comments/{comment_id}", "--input", "-"])
            body = json.loads(kwargs["input"])["body"]
            self.comments[comment_id]["body"] = body
            self.calls.append(("edit-comment", comment_id, body))
            return ""
        checks = f"repos/{self.repo.github}/commits/{self.head}/check-runs?check_name=arete-checks&filter=latest&per_page=100"
        if args == ["api", checks]:
            self.assertFalse(kwargs["retry"])
            self.ci_reads.append(self.head)
            runs = [{"head_sha": self.reported_ci_head or self.head, "name": miner.MINER_CHECK, "app": {"id": self.ci_app},
                     "status": "in_progress" if self.status == "pending" else "completed", "conclusion": self.status}]
            return json.dumps({"check_runs": [] if self.status == "missing" else runs})
        if args == ["api", "-X", "PUT", f"repos/{self.repo.github}/pulls/8/update-branch", "--input", "-"]:
            self.assertTrue(kwargs["write"])
            self.calls.append(("update", json.loads(kwargs["input"])))
            self.head = self.UPDATED
            self.behind = False
            return ""
        if args == ["pr", "merge", "8", "--repo", self.repo.github, "--squash", "--match-head-commit", self.head]:
            self.assertTrue(kwargs["write"])
            self.during_merge()
            self.calls.append(("merge", self.head))
            self.merged = True
            return ""
        if args[:4] == ["pr", "edit", self.url, "--add-label"]:
            self.labels.add(args[4])
        if args[:2] in (["pr", "close"], ["label", "create"], ["pr", "edit"]):
            self.calls.append(tuple(args))
            return ""
        self.fail(f"Unexpected GitHub write {args}")

    def git(self, args, cwd, **kw):
        self.assertEqual(cwd, self.repo.path)
        if args[0] == "fetch":
            return ""
        if args == ["rev-parse", f"origin/{self.base_name}", f"origin/{self.BRANCH}"]:
            return f"{self.base}\n{self.head}\n"
        if args == ["merge-base", self.base, self.head]:
            return ("e" * 40 if self.behind else self.base) + "\n"
        if args[0] == "show":
            return "Rules for ${goal}"
        self.fail(f"Unexpected Git command {args}")

    def decide(self):
        return miner.decide_pr(self.cfg, "min-test", self.repo, self.url)

    def run_miner(self, *, proposals=(), dry_run=False, apply=True, apply_repo=None, jev_ready=False):
        self.enterContext(mock.patch.multiple(
            miner, collect=mock.DEFAULT, collect_runs=mock.DEFAULT,
            render_events=mock.DEFAULT, proposal_status=mock.DEFAULT, apply_repo=mock.DEFAULT,
        ))
        miner.collect.return_value = [{"items": [], "cwd": "", "session": "fake"}] if proposals else []
        miner.collect_runs.return_value = [], 0
        miner.render_events.return_value = ""
        miner.proposal_status.return_value = "pending", ""
        miner.apply_repo.return_value = self.url, list(proposals), self.head
        miner.apply_repo.side_effect = apply_repo
        self.enterContext(mock.patch.object(miner.auditor, "latest_findings", return_value="(none)"))
        self.enterContext(mock.patch.object(miner.insights_store, "feedback_between", return_value=[]))
        self.enterContext(mock.patch.object(miner.jev, "ready", return_value=jev_ready))
        self.enterContext(mock.patch.object(miner.runner, "run", return_value=SimpleNamespace(
            ok=True, cost_usd=0, structured={"proposals": list(proposals)}, error="", paid_usd=0,
        )))
        self.enterContext(mock.patch.object(miner.record, "settle"))
        now = util.now()
        return miner._run(self.cfg, "min-test", now - timedelta(days=1), now, True, "", dry_run, apply)

    def test_fresh_pr_merges_at_the_checked_head_without_moving_checkout_or_blocking_admission(self):
        class ReachedPipelineStart(Exception):
            pass

        proposal = {"repo": "arete", "layer": "repo", "file": self.PROMPT, "change": "fix prompt"}
        self.prs = [{"html_url": self.URL, "pull_request": {}}]
        self.repo.parts = [config.Part("", self.repo.github)]
        self.layer.factory_path = "classic"
        locks = self.root / "locks"
        locks.mkdir()

        def start_pipeline():
            with self.assertRaises(ReachedPipelineStart):
                pipeline.run_issue(self.repo.name, "t-0657")

        with (
            mock.patch.multiple(pipeline.scratch, process_ids=set, service_labels=set),
            mock.patch.multiple(pipeline.paths, LOCKS_DIR=locks, ensure_dirs=mock.DEFAULT),
            mock.patch.object(config, "load", return_value=self.cfg),
            mock.patch.object(pipeline, "pending_feedback", return_value=[]),
            mock.patch.multiple(pipeline.run_store, newest_whole_run=mock.Mock(return_value=None),
                                ticket_runs=mock.Mock(return_value=[]), ticket_runs_marked_running=mock.Mock(return_value=[]),
                                newest_live_whole_run=mock.Mock(return_value=None)),
            mock.patch.object(pipeline.slots, "slots_free_to_start", return_value=1),
            mock.patch.object(pipeline.slots.RunSlot, "hold") as hold_slot,
            mock.patch.object(pipeline.record, "start", side_effect=ReachedPipelineStart) as start,
            mock.patch.object(util, "write_bytes_atomic") as receipt,
        ):
            self.during_merge = start_pipeline
            self.assertEqual(self.run_miner(proposals=[proposal]), 0)
            hold_slot.assert_called_once()
            self.assertEqual(start.call_args.args, ("pipeline",))
            self.assertEqual(start.call_args.kwargs["issue"], "t-0657")
            receipt.assert_not_called()
        self.eval.assert_called_once_with(self.cfg, self.repo, self.BASE, self.HEAD)
        self.assertEqual(self.ci_reads, [self.HEAD])
        self.assertEqual(self.calls[-1], ("merge", self.HEAD))
        self.assertEqual({call.args[0][0] for call in self.git_mock.call_args_list}, {"fetch", "rev-parse", "merge-base", "show"})
        self.proposals.assert_called_once_with(self.URL, "merged")

    def test_previous_pr_is_decided_on_noop_run(self):
        self.prs = [{"html_url": self.URL, "pull_request": {}}]
        self.assertEqual(self.run_miner(), 0)
        miner.runner.run.assert_not_called()
        self.assertEqual(self.calls[-1], ("merge", self.HEAD))

    def test_deferred_pr_is_retried_on_a_later_run(self):
        self.prs = [{"html_url": self.URL, "pull_request": {}}]
        self.status = "pending"
        self.assertEqual(self.run_miner(), 0)
        self.assertFalse(self.merged)
        self.status = "success"
        self.assertEqual(self.run_miner(), 0)
        self.assertTrue(self.merged)
        self.assertEqual(self.ci_reads, [self.HEAD, self.HEAD])

    def test_dry_run_leaves_stale_pr_untouched(self):
        self.prs = [{"createdAt": util.iso(util.now() - timedelta(days=3)), "url": self.URL}]
        self.assertEqual(self.run_miner(dry_run=True), 0)
        self.assertFalse(self.calls)
        self.eval.assert_not_called()

    def test_behind_pr_updates_and_only_reads_the_new_head_ci_next_run(self):
        self.behind = True
        self.assertEqual(self.decide(), "branch update requested")
        self.assertEqual(self.calls, [("update", {"expected_head_sha": self.HEAD})])
        self.eval.assert_not_called()
        self.assertEqual(self.ci_reads, [])
        self.reported_ci_head = self.HEAD
        self.assertIn("does not have a passing", self.decide())
        self.assertEqual(self.ci_reads, [self.UPDATED])
        self.assertFalse(self.merged)
        self.reported_ci_head = ""
        self.assertEqual(self.decide(), "merged")
        checked = mock.call(self.cfg, self.repo, self.BASE, self.UPDATED)
        self.assertEqual(self.eval.call_args_list, [checked, checked])
        self.assertEqual(self.calls[-1], ("merge", self.UPDATED))

    def test_pending_missing_or_failing_ci_posts_eval_feedback_without_a_decision(self):
        self.eval.return_value = evals.Comparison("eval table", regressions=["`regressed-task`"], complete=True)
        with mock.patch.object(miner, "judge_prompts") as judge:
            for status in ("pending", "failure", "missing"):
                with self.subTest(status=status):
                    self.status = status
                    self.calls.clear()
                    self.comments.clear()
                    self.ci_reads.clear()
                    self.assertIn("does not have a passing", self.decide())
                    self.assertEqual(self.ci_reads, [self.HEAD])
                    self.assertEqual([call[0] for call in self.calls], ["comment"])
                    self.assertIn("eval table", self.calls[0][2])
                    self.assertNotIn("Closing", self.calls[0][2])
                    self.assertIn("does not have a passing", self.decide())
                    self.assertEqual(self.ci_reads, [self.HEAD, self.HEAD])
                    self.assertEqual([call[0] for call in self.calls], ["comment"])
            judge.assert_not_called()
        self.proposals.assert_not_called()

    def test_a_check_with_the_same_name_from_a_different_app_does_not_count(self):
        self.ci_app += 1
        self.assertIn("does not have a passing", self.decide())
        self.assertEqual(self.ci_reads, [self.HEAD])
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.proposals.assert_not_called()

    def test_oversized_pr_stays_open(self):
        self.diff.return_value = diffguard.Diff({self.PROMPT: miner.MAX_MINER_DIFF + 1})
        self.assertIn("over the", self.decide())
        self.eval.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_global_rule_in_skill_diff_requires_approval_at_apply(self):
        self.repo = config.Repo("shared", Path("."), github="example/skills")
        self.cfg = config.Config(repos=[self.repo], global_repo=self.repo.name)
        self.url = "https://github.com/example/skills/pull/8"
        skill = "skills/example/SKILL.md"
        proposal = {"repo": self.repo.name, "layer": "global", "file": skill}
        mixed = self.diff.return_value = diffguard.Diff({skill: 2, miner.GLOBAL_RULES: 1})
        for resume in (False, True):
            with self.subTest(resume=resume), mock.patch.object(
                gh, "open_pr_url", return_value=self.url if resume else ""
            ), mock.patch.multiple(
                miner.workspace, make_worktree=mock.Mock(return_value=self.root), remove_worktree=mock.DEFAULT,
            ), mock.patch.object(miner, "apply_in", return_value=Result(ok=True)), mock.patch.object(
                diffguard, "stage", return_value=diffguard.Diff({skill: 2}) if resume else mixed,
            ), mock.patch.object(util, "git_as_owner") as commit, mock.patch.object(
                miner, "git", return_value=f"{self.BASE}\n",
            ) as git:
                self.diff.reset_mock()
                self.assertEqual(miner.apply_repo(self.cfg, "min-test", self.repo, [proposal], self.BRANCH), ("", [], ""))
                commit.assert_not_called()
                if resume:
                    git.assert_called_once_with(["merge-base", "origin/main", "HEAD"], self.root)
                    self.diff.assert_called_once_with(self.root, "--cached", self.BASE,
                                                      deny=diffguard.ALWAYS_DENY, moves=True)
                else:
                    git.assert_not_called()
        self.assertFalse(self.calls)


    def test_head_or_base_change_during_evals_holds(self):
        for field in ("head", "base"):
            with self.subTest(field=field):
                self.head, self.base = self.HEAD, self.BASE

                def change(*args):
                    setattr(self, field, self.UPDATED)
                    return evals.Comparison("eval table", complete=True)

                self.eval.side_effect = change
                self.assertIn("changed during evals", self.decide())
        self.assertEqual(self.calls, [])

    def test_head_change_during_ci_check_prevents_a_close_comment(self):
        self.eval.return_value = evals.Comparison("Eval table", regressions=["`regressed-task`"], complete=True)

        def passes(*args):
            self.head = self.UPDATED
            return True

        with mock.patch.object(gh, "actions_check_passed", side_effect=passes) as ci:
            self.assertIn("changed during the CI check", self.decide())
        ci.assert_called_once_with(self.repo.github, self.HEAD, miner.MINER_CHECK)
        self.assertEqual(self.calls, [])

    def test_confirmed_regression_closes_with_table_even_when_another_eval_is_incomplete(self):
        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            self.assertEqual(source.ref, self.BASE)
            return evals.Arm(tag, [evals.TaskResult(task, passed, base_commit=self.BASE, context_sha="harness",
                                                  graded=True, prompts_used={"build.md"})
                                   for task, passed in (("regressed-task", on_base), ("improved-task", not on_base))]
                             + [evals.TaskResult("unavailable-task", passed=on_base, graded=on_base,
                                                 base_commit=self.BASE, context_sha="harness")])

        self.eval.side_effect = self.compare_prompts
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"build.md": ref.encode()})
        with mock.patch.multiple(evals, export_prompts=export, suites=lambda cfg: [self.repo],
                                 paired_tasks=lambda *args: [evals.Task("fixture", "Fixture", "fixture")],
                                 run_suite=suite), mock.patch.object(util, "lock", return_value=nullcontext(True)):
            self.assertIn("closed", self.decide())
        self.assertEqual([call.args[1] for call in export.call_args_list], [self.BASE, self.HEAD])
        reason, table = self.calls[0][2].split("\n\n", 1)
        self.assertIn("regressed-task", reason)
        self.assertNotIn("improved-task", reason)
        self.assertIn("| all | 2/3 | 1/3 |", table)
        self.assertIn("Incomplete eval tasks: `unavailable-task`", table)
        self.assertEqual(self.calls[1], ("pr", "close", self.URL))
        self.assertFalse(self.merged)
        self.proposals.assert_called_once_with(self.URL, "rejected")

    def test_out_of_scope_prs_receive_no_actions(self):
        self.behind = True
        for extra in ("CLAUDE.md", "skills/example/SKILL.md", "arete/pipeline.py", "arete/prompts/fix.md", evals.LAYER_CONFIG):
            with self.subTest(extra=extra):
                self.diff.return_value = diffguard.Diff({self.PROMPT: 2, extra: 1})
                self.assertIn("outside", self.decide())
        self.diff.return_value = diffguard.Diff({self.PROMPT: 2})
        self.cfg.global_repo = self.cfg.arete_repo
        self.assertIn("outside", self.decide())
        self.cfg.global_repo = "shared"
        self.layer.trust_level = "pr"
        self.assertIn("outside", self.decide())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ci_reads, [])
        self.eval.assert_not_called()

    def test_published_out_of_scope_heads_get_feedback_and_swept_prs_do_not(self):
        self.repo.layer = self.root / "layer"
        (self.repo.layer / "evals").mkdir(parents=True)
        self.prs = [{"html_url": self.URL, "pull_request": {}}]
        for name, trust, mixed, expected in (
            ("arete", "automerge", True, ["eval table", "suite results"]),
            ("arete", "pr", False, ["eval table", "suite results"]),
            ("other", "pr", False, ["suite results"]),
            ("shared", "pr", False, []),
        ):
            with self.subTest(name=name, trust=trust, mixed=mixed), \
                    mock.patch.object(evals, "compare", return_value="suite results") as suite, \
                    mock.patch.object(miner, "judge_prompts") as judge:
                self.calls.clear()
                self.eval.reset_mock()
                self.head = self.HEAD
                self.cfg.global_repo = "shared"
                self.repo.name = name
                self.layer.trust_level = trust
                files = {self.PROMPT: 2, "CLAUDE.md": 1} if mixed else {self.PROMPT: 2}
                self.diff.return_value = diffguard.Diff(files)
                proposal = {"repo": name, "layer": "repo", "file": self.PROMPT, "change": "fix prompt"}
                self.assertEqual(self.run_miner(proposals=[proposal]), 0)
                self.head = self.UPDATED
                self.assertEqual(self.run_miner(proposals=[proposal]), 0)
                self.assertEqual(self.run_miner(), 0)
                self.assertEqual([call[0] for call in self.calls], ["comment"] * (2 * len(expected)))
                self.assertEqual([call[2].split("\n\n")[0] for call in self.calls], expected * 2)
                if name == "arete":
                    self.assertEqual(self.eval.call_args_list, [
                        mock.call(self.cfg, self.repo, self.BASE, head, fork=self.BASE)
                        for head in (self.HEAD, self.UPDATED)
                    ])
                else:
                    self.eval.assert_not_called()
                if expected:
                    self.assertEqual(suite.call_args_list, [mock.call(name, head) for head in (self.HEAD, self.UPDATED)])
                else:
                    suite.assert_not_called()
                judge.assert_not_called()
        self.assertEqual(self.ci_reads, [])
        self.proposals.assert_not_called()

    def test_apply_publishes_an_in_repo_eval_task_edit_for_human_review(self):
        self.repo.path = self.root
        task_file = self.repo.layer_dir / "evals" / "example.toml"
        task_file.parent.mkdir(parents=True)
        task_file.write_text('prompt = "Try the behavior"\n')
        relative = task_file.relative_to(self.repo.path).as_posix()
        staged = diffguard.Diff({relative: 2})
        proposal = {"repo": self.repo.name, "layer": "repo", "file": relative, "change": "fix eval task",
                    "why": "Exercise the required behavior."}
        wt = self.root / "miner-wt"
        existing = [{"headRefName": self.BRANCH, "url": self.URL}]
        self.diff.return_value = staged
        self.eval.return_value = evals.Comparison("")

        def apply_git(args, cwd, **kwargs):
            if cwd == wt:
                if args == ["rev-parse", "HEAD"]:
                    return self.UPDATED
                if args == ["merge-base", "origin/main", "HEAD"]:
                    return self.BASE
                self.assertEqual(args, ["push", "-q", "--force-with-lease", "-u", "origin", self.BRANCH])
                self.head = self.UPDATED
                return ""
            return self.git(args, cwd, **kwargs)

        with (
            mock.patch.multiple(gh, open_pr_url=mock.Mock(return_value=self.URL), prs=mock.Mock(return_value=existing),
                                ensure_label=mock.DEFAULT, pr_create=mock.DEFAULT),
            mock.patch.multiple(miner.workspace, make_worktree=mock.Mock(return_value=wt),
                                remove_worktree=mock.DEFAULT, remote_head=mock.Mock(return_value=self.HEAD)),
            mock.patch.object(miner, "miner_branch", return_value=self.BRANCH),
            mock.patch.object(miner, "apply_in", return_value=Result(ok=True)),
            mock.patch.object(miner, "git", side_effect=apply_git) as git,
            mock.patch.object(diffguard, "stage", return_value=staged),
            mock.patch.object(util, "git_records", return_value=["M", relative]),
            mock.patch.object(util, "git_as_owner") as commit,
            mock.patch.object(evals, "compare", return_value="suite results") as suite,
        ):
            self.assertEqual(self.run_miner(proposals=[proposal], apply_repo=miner.apply_repo), 0)
            commit.assert_called_once()
            self.assertEqual(commit.call_args.args[1], wt)
            self.assertIn(mock.call(["push", "-q", "--force-with-lease", "-u", "origin", self.BRANCH], wt, timeout=300),
                          git.call_args_list)
            miner.workspace.remove_worktree.assert_called_once_with(self.repo, wt)
            gh.pr_create.assert_not_called()
            suite.assert_called_once_with(self.repo.name, self.UPDATED)
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertIn("suite results", self.calls[0][2])
        self.assertEqual(self.ci_reads, [])
        self.proposals.assert_not_called()

    def test_prompt_comparison_shares_local_tasks_config_expertise_and_skills(self):
        self.repo.path = self.root
        config.load_layer.side_effect = self.load_layer
        task_file = ".agents/evals/local-task.toml"
        path = self.root / task_file
        path.parent.mkdir(parents=True)
        path.write_text('prompt = "Run the locally configured task"\nverify = "Try the behavior"\n')
        local_settings = b'[trust]\nlevel = "automerge"\n[verify]\nnotes = "operator verify settings"\n'
        (self.root / evals.LAYER_CONFIG).write_bytes(local_settings)
        local_harness = {
            ".agents/expertise.md": "Operator expertise",
            ".claude/skills/local/SKILL.md": "Operator Claude skill",
            "skills/local/SKILL.md": "Operator shared skill",
        }
        for rel, content in local_harness.items():
            f = self.root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        skill_link = ".claude/skills/alias/SKILL.md"
        (self.root / skill_link).parent.mkdir(parents=True)
        (self.root / skill_link).symlink_to("../../../skills/local/SKILL.md")
        self.eval.side_effect = self.compare_prompts
        arms = []

        def export(repo, ref, dest):
            dest.mkdir(parents=True)
            (dest / "plan.md").write_text("Plan with $expertise")
            build = f"Build from {ref} with $expertise"
            (dest / "build.md").write_text(build)
            return {"build.md": build.encode()}

        def records(args, cwd):
            if args == ["ls-tree", "-r", self.BASE]:
                return []
            if args == ["ls-files"]:
                return []
            if args[-2:] == ["--", evals.EVALS]:
                self.assertEqual(cwd, self.repo.path)
                return [task_file]
            if args[:1] == ["ls-files"]:
                self.assertEqual(cwd, self.repo.path)
                return [rel for rel in (*local_harness, skill_link) if rel.startswith(args[-1] + "/")]
            self.assertEqual(args, ["diff", "--name-status", "--no-renames", self.BASE, self.HEAD])
            return ["M", self.PROMPT]

        def suite(cfg, repo, source, tasks, tag, prompts):
            self.assertEqual(source.ref, self.BASE)
            self.assertEqual([(task.name, task.base) for task in tasks], [("local-task", self.BASE)])
            task, = tasks
            run_dir = self.root / tag
            run = evals.EvalRun(id=tag, repo=repo, dir=run_dir, task=task.name, trial=tag)
            run.call_kw = mock.Mock(return_value={})
            run.agent = mock.Mock(side_effect=[Result(ok=True, structured={}), Result(ok=True)])
            run.gate = mock.Mock(return_value=(True, "checks passed"))
            result = evals.run_task(cfg, source, task, prompts, run)
            self.assertTrue(result.graded, result.why)
            self.assertIn(f"Build from {prompts.commit} with Operator expertise", run.agent.call_args.args[1])
            arms.append(result)
            if prompts.commit == self.BASE:
                for rel in local_harness:
                    (self.root / rel).write_text("Edited after the first arm")
            return evals.Arm(tag, [result])

        def checkout(repo_path, base, wt):
            self.assertEqual(base, self.BASE)
            layer_dir = wt / ".agents"
            layer_dir.mkdir(parents=True)
            (layer_dir / "config.toml").write_text('[verify]\nnotes = "config from the checkout"\n')
            return self.BASE

        def grade(work, step, kind, *, role, goal):
            self.assertEqual(work.layer.verify.notes, "operator verify settings")
            self.assertEqual(work.layer.config_file.read_bytes(), local_settings)
            for rel, content in local_harness.items():
                self.assertEqual((work.ws / rel).read_text(), content)
            self.assertTrue((work.ws / skill_link).is_symlink())
            self.assertEqual((work.ws / skill_link).read_text(), local_harness["skills/local/SKILL.md"])
            self.assertEqual(goal, "Try the behavior")
            return verify.Verified("pass")

        with (
            mock.patch.multiple(
                evals, export_prompts=export, git=mock.Mock(return_value=self.BASE), run_suite=suite,
                fetch_origin=mock.DEFAULT, checkout_without_origin_refs=checkout, commit_overlay=mock.DEFAULT,
                use_product=mock.Mock(side_effect=grade),
            ),
            mock.patch.object(util, "git_records", side_effect=records),
            mock.patch.object(evals.vault, "hidden", side_effect=str),
            mock.patch.object(evals.paths, "WORKTREES_DIR", self.root / "eval-worktrees"),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
            mock.patch.object(miner, "judge_prompts") as judge,
        ):
            self.assertEqual(self.decide(), "merged")
            self.assertEqual(evals.use_product.call_count, 2)
            judge.assert_not_called()
        self.assertEqual(self.calls[-1], ("merge", self.HEAD))
        self.assertTrue(arms[0].context_sha)
        self.assertEqual(arms[0].context_sha, arms[1].context_sha)
        self.assertEqual((self.root / evals.LAYER_CONFIG).read_bytes(), local_settings)

    def test_changed_incomplete_tasks_update_one_comment_and_keep_private_tasks_hidden(self):
        private = config.Repo("private-project", self.root)
        tasks = {self.repo.name: [evals.Task("busy-task", "Public task", "fixture")],
                 private.name: [evals.Task("secret-task", "Private task", "fixture")]}
        self.eval.side_effect = self.compare_prompts
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"build.md": ref.encode()})
        with (
            mock.patch.multiple(evals, export_prompts=export, suites=lambda cfg: [self.repo, private],
                                paired_tasks=lambda repo, base: tasks[repo.name], run_suite=mock.DEFAULT),
            mock.patch.object(util, "lock", return_value=nullcontext(False)),
            mock.patch.object(miner, "judge_prompts") as judge,
        ):
            self.assertIn("incomplete", self.decide())
            original = self.comments[1]["body"]
            self.comments = {99: {"id": 99, "user": {"login": "reviewer"}, "body": original}, **self.comments}
            tasks[self.repo.name] = [evals.Task("next-task", "Another public task", "fixture")]
            tasks[private.name] = [evals.Task("other-secret", "Another private task", "fixture")]
            self.assertIn("incomplete", self.decide())
            self.assertIn("incomplete", self.decide())
            evals.run_suite.assert_not_called()
            judge.assert_not_called()
        self.assertEqual(self.ci_reads, [self.HEAD] * 3)
        self.assertEqual([call[0] for call in self.calls], ["comment", "edit-comment"])
        self.assertEqual(self.calls[1][1], 1)
        self.assertEqual(self.comments[99]["body"], original)
        for name, body in (("busy-task", original), ("next-task", self.comments[1]["body"])):
            self.assertIn(f"Incomplete eval tasks were `{name}` in arete, private repo 1 (1 task).", body)
            self.assertIn("its evals were already running", body)
            for hidden in ("secret-task", "other-secret", "private-project"):
                self.assertNotIn(hidden, body)
        self.assertNotIn("busy-task", self.comments[1]["body"])
        self.proposals.assert_not_called()

    def test_repair_only_on_base_leaves_candidate_fix_prompt_unmeasured(self):
        task = evals.Task("fixture", "Fixture", "fixture")

        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            return evals.Arm(tag, [evals.TaskResult(task.name, passed=True, graded=True, base_commit=self.BASE,
                                                  context_sha="harness", repairs=int(on_base),
                                                  prompts_used={"fix.md"} if on_base else {"build.md"})])

        self.eval.side_effect = self.compare_prompts
        self.diff.return_value = diffguard.Diff({"arete/prompts/fix.md": 2})
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"fix.md": ref.encode()})
        with mock.patch.multiple(evals, export_prompts=export, suites=lambda cfg: [self.repo],
                                 paired_tasks=lambda *args: [task], run_suite=suite), \
                mock.patch.object(util, "lock", return_value=nullcontext(True)), \
                mock.patch.object(miner, "judge_prompts", return_value=(False, "JEV answered no.")) as judge:
            self.assertEqual(self.decide(), "JEV answered no.")
        self.assertEqual(judge.call_args.args[-1], ["fix.md"])
        self.assertIn("Not measured", self.calls[0][2])
        self.assertIn("JEV answered no.", self.calls[0][2])
        self.assertFalse(self.merged)

    def test_outage_defers_the_pr_and_preserves_eval_history(self):
        task = evals.Task("fixture", "Fixture", "fixture", verify="Try the behavior", base=self.BASE)
        layer = config.RepoLayer(self.root, verify=config.Verify())

        def execute(cfg, source, task, prompts, run):
            on_base = prompts.commit == self.BASE
            run.call_kw = mock.Mock(return_value={})
            run.agent = mock.Mock(side_effect=[Result(ok=True, structured={}),
                                               Result(ok=on_base, error="" if on_base else "runner timed out")])
            run.gate = mock.Mock(return_value=(True, "checks passed"))
            return run_task(cfg, source, task, evals.Prompts(), run)

        run_task = evals.run_task
        self.eval.side_effect = self.compare_prompts
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"build.md": ref.encode()})
        with mock.patch.multiple(
            evals, export_prompts=export, suites=lambda cfg: [self.repo], paired_tasks=lambda *args: [task],
            run_task=execute, fetch_origin=mock.DEFAULT, checkout_without_origin_refs=mock.Mock(return_value=self.BASE),
            prepare=mock.Mock(return_value=layer), context_digest=mock.Mock(return_value="harness"),
            eval_settings=mock.Mock(return_value=""), git=mock.DEFAULT,
            grade=mock.Mock(return_value=verify.Verified("pass")),
        ), mock.patch.multiple(
            evals.record, start=mock.Mock(side_effect=["eval-base", "eval-head"]),
            failed_on_crash=mock.Mock(return_value=nullcontext()), metered_spent=mock.Mock(return_value=0),
            settle=mock.DEFAULT,
        ), mock.patch.multiple(
            evals.insights_store, task_history=mock.Mock(return_value=[]), add_eval=mock.DEFAULT,
        ), mock.patch.object(evals.codebase, "load", return_value="fixture"), \
                mock.patch.object(evals.vault, "hidden", side_effect=str), \
                mock.patch.object(evals, "prompts_digest", return_value="fixture"), \
                mock.patch.object(evals.run_store, "last_step_id", return_value=0), \
                mock.patch.object(evals.paths, "WORKTREES_DIR", self.root / "worktrees"), \
                mock.patch.object(util, "lock", return_value=nullcontext(True)), \
                mock.patch.object(miner, "judge_prompts") as judge:
            self.assertIn("incomplete", self.decide())
            history = [call.args[:4] for call in evals.insights_store.add_eval.call_args_list]
            self.assertEqual(history, [("eval-base", "arete", task.name, True), ("eval-head", "arete", task.name, False)])
        self.assertIn("Incomplete eval tasks were `fixture`", self.calls[0][2])
        self.assertIn("| all | 1/1 | 0/1 |", self.calls[0][2])
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertFalse(self.merged)
        self.proposals.assert_not_called()
        judge.assert_not_called()

    def test_blocked_verification_keeps_its_score_and_marks_the_task_incomplete(self):
        task = evals.Task("fixture", "Fixture", "fixture", verify="Try the behavior")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root, verify=config.Verify())
        with (
            mock.patch.object(evals, "git", return_value=""),
            mock.patch.object(evals.vault, "hidden", side_effect=str),
            mock.patch.object(verify, "with_free_ports", side_effect=lambda verify_config: verify_config),
            mock.patch.object(verify, "port_refusal", return_value="Fixture port is unavailable."),
        ):
            result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                      "", "", True, 60)
        self.assertEqual(result.verify, "blocked")
        self.assertFalse(result.passed)
        self.assertFalse(result.graded)

    def test_acceptance_distinguishes_invoked_missing_cli_from_infrastructure_failure(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="expected-command")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root)
        for process, exit_code, complete in (
            (subprocess.CompletedProcess(task.check, 127, "", "sh: expected-command: command not found"), 127, True),
            (FileNotFoundError("shell launcher unavailable"), 127, False),
            (subprocess.TimeoutExpired(task.check, 60), 124, False),
            (subprocess.CompletedProcess(task.check, 1, "", "sandbox-exec: sandbox_apply: Operation not permitted"),
             1, False),
        ):
            with self.subTest(process=process), mock.patch.object(evals, "git", return_value=""), mock.patch.object(
                util, "run_whole_tree", side_effect=[process],
            ), mock.patch.object(evals.vault, "hidden", side_effect=lambda text, *args: text):
                result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                          "", "", True, 60)
                self.assertFalse(result.passed)
                self.assertEqual(result.graded, complete)
                self.assertEqual(result.failures, [f"check exit {exit_code}"])

    def test_finished_verifier_failure_closes_even_when_the_same_tasks_shell_command_times_out(self):
        task = evals.Task("required-behavior", "Fixture", "fixture", check="held-out", verify="Try the behavior")
        layer = config.RepoLayer(self.root, verify=config.Verify())
        results = []

        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            run = evals.EvalRun(id=tag, repo=repo, dir=self.root, task=task.name)
            result = evals.TaskResult(task.name, base_commit=self.BASE, context_sha="harness", prompts_used={"build.md"})
            grading = verify.Verified("pass" if on_base else "fail", summary="Required behavior failed.")
            command = (subprocess.CompletedProcess(task.check, 0, "", "") if on_base
                       else subprocess.TimeoutExpired(task.check, 60))
            with mock.patch.object(evals, "grade", return_value=grading), mock.patch.object(
                util, "run_whole_tree", side_effect=[command],
            ):
                results.append(evals.acceptance(cfg, run, layer, self.root, task, None, result, "", "", True, 60))
            return evals.Arm(tag, [result])

        self.eval.side_effect = self.compare_prompts
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"build.md": ref.encode()})
        with (
            mock.patch.multiple(evals, export_prompts=export, paired_tasks=lambda *args: [task],
                                suites=lambda cfg: [self.repo], run_suite=suite, git=mock.Mock(return_value="")),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
            mock.patch.object(evals.vault, "hidden", side_effect=lambda text, *args: text),
            mock.patch.object(miner, "judge_prompts") as judge,
        ):
            self.assertEqual(self.decide(), "closed for an eval regression")
            judge.assert_not_called()
        baseline, candidate = results
        self.assertTrue(baseline.passed and baseline.graded)
        self.assertFalse(candidate.passed)
        self.assertFalse(candidate.graded)
        self.assertEqual(candidate.failures, ["failed in use: Required behavior failed.", "check exit 124"])
        self.assertIn("Closing because `required-behavior`", self.calls[0][2])
        self.assertIn("Incomplete eval tasks: `required-behavior`", self.calls[0][2])
        self.assertIn("| all | 1/1 | 0/1 |", self.calls[0][2])
        self.assertEqual(self.calls[1], ("pr", "close", self.URL))

    def test_held_out_exit_127_can_close_a_pr_even_when_its_verifier_is_blocked(self):
        task = evals.Task("held-out-behavior", "Fixture", "fixture", check="expected-command", verify="Try it")
        layer = config.RepoLayer(self.root, verify=config.Verify())
        results = {}

        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            run = evals.EvalRun(id=tag, repo=repo, dir=self.root, task=task.name)
            result = evals.TaskResult(task.name, base_commit=self.BASE, context_sha="harness", prompts_used={"build.md"})
            grading = (verify.Verified("pass") if on_base
                       else verify.Verified("blocked", blocked_by="Verifier unavailable", finished=False))
            command = subprocess.CompletedProcess(task.check, 0, "", "") if on_base else candidate_command
            with mock.patch.object(evals, "grade", return_value=grading), mock.patch.object(
                util, "run_whole_tree", side_effect=[command],
            ):
                results[prompts.commit] = evals.acceptance(cfg, run, layer, self.root, task, None, result,
                                                          "", "", True, 60)
            return evals.Arm(tag, [result])

        self.eval.side_effect = self.compare_prompts
        with (
            mock.patch.multiple(evals, export_prompts=lambda repo, ref, dest: {"build.md": ref.encode()},
                                paired_tasks=lambda *args: [task], suites=lambda cfg: [self.repo],
                                run_suite=suite, git=mock.Mock(return_value="")),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
            mock.patch.object(evals.vault, "hidden", side_effect=lambda text, *args: text),
            mock.patch.object(miner, "judge_prompts") as judge,
        ):
            for candidate_command, closes in (
                (FileNotFoundError("shell launcher unavailable"), False),
                (subprocess.CompletedProcess(task.check, 127, "", "expected-command: command not found"),
                 True),
            ):
                with self.subTest(command=candidate_command):
                    self.calls.clear()
                    decision = self.decide()
                    if closes:
                        self.assertEqual(decision, "closed for an eval regression")
                        self.assertEqual(self.calls[1:], [("pr", "close", self.URL)])
                        self.assertIn("Closing because `held-out-behavior`", self.calls[0][2])
                        self.proposals.assert_called_once_with(self.URL, "rejected")
                    else:
                        self.assertEqual(decision, "the eval comparison is incomplete")
                        self.assertEqual([call[0] for call in self.calls], ["comment"])
                        self.proposals.assert_not_called()
                    self.assertIn("Incomplete eval tasks: `held-out-behavior`", self.calls[0][2])
                    self.assertIn("| all | 1/1 | 0/1 |", self.calls[0][2])
                    baseline, candidate = results[self.BASE], results[self.HEAD]
                    self.assertTrue(baseline.passed and baseline.graded)
                    self.assertFalse(candidate.passed)
                    self.assertFalse(candidate.graded)
                    self.assertEqual(candidate.failures, ["grading blocked: Verifier unavailable", "check exit 127"])
            judge.assert_not_called()

    def test_unmeasured_requires_jev_yes_and_records_both_answers(self):
        self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)
        with mock.patch.object(jev, "ready", return_value=True), mock.patch.object(miner.vault, "hidden", side_effect=str):
            for value in (False, True):
                with self.subTest(value=value):
                    self.calls.clear()
                    self.labels.discard(miner.REVIEW_LABEL)
                    answer = jev.Answer(value, 1.0, {"yes": float(value)})
                    with mock.patch.object(jev, "ask", return_value=jev.Reply({"fix": answer})) as ask:
                        self.decide()
                    self.assertIn("reported failure and rationale", ask.call_args.args[0])
                    self.assertIn(f"JEV answered {'yes' if value else 'no'}", self.calls[0][2])
                    self.assertEqual(self.merged, value)
                    if not value:
                        self.assertIn(("pr", "edit", self.URL, "--add-label", "agent:needs-review"), self.calls)

    def test_jev_service_error_leaves_the_pr_retryable_on_the_next_miner_run(self):
        self.prs = [{"html_url": self.URL, "pull_request": {}}]
        self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)
        replies = [jev.Reply(error="HTTP 503: Service unavailable"), jev.Reply({"fix": jev.Answer(True, 1.0, {"yes": 1})})]
        with mock.patch.object(jev, "ask", side_effect=replies) as ask, \
                mock.patch.object(miner.vault, "hidden", side_effect=str):
            self.assertEqual(self.run_miner(jev_ready=True), 0)
            ask.assert_called_once()
            self.assertEqual(self.labels, {miner.MINER_LABEL})
            self.assertFalse(self.merged)
            self.proposals.assert_not_called()
            self.assertEqual([call[0] for call in self.calls], ["comment"])
            self.assertIn("JEV returned no decision.", self.calls[0][2])
            self.assertEqual(self.run_miner(jev_ready=True), 0)
            self.assertEqual(ask.call_count, 2)
        self.assertEqual(self.ci_reads, [self.HEAD, self.HEAD])
        self.assertEqual(self.eval.call_count, 2)
        self.assertEqual([call[0] for call in self.calls], ["comment", "comment", "merge"])
        self.assertIn("JEV answered yes", self.calls[1][2])
        self.assertEqual(self.calls[2], ("merge", self.HEAD))
        self.proposals.assert_called_once_with(self.URL, "merged")

    def test_unavailable_invalid_or_uncertain_jev_verdicts_do_not_request_human_review(self):
        self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)
        for ready, reply in (
            (False, jev.Reply()),
            (True, jev.Reply()),
            (True, jev.Reply({"fix": jev.Answer("no", 1.0, {"yes": 0})})),
            (True, jev.Reply({"fix": jev.Answer(True, 3.0, {"yes": 2.0})})),
            (True, jev.Reply({"fix": jev.Answer(False, 0.02, {"yes": 0.49})})),
        ):
            with self.subTest(ready=ready, reply=reply), mock.patch.object(jev, "ready", return_value=ready), \
                    mock.patch.object(jev, "ask", return_value=reply), mock.patch.object(miner.vault, "hidden", str):
                self.calls.clear()
                self.labels = {miner.MINER_LABEL}
                self.merged = False
                self.decide()
                self.assertEqual([call[0] for call in self.calls], ["comment"])
                self.assertEqual(self.labels, {miner.MINER_LABEL})
                self.assertFalse(self.merged)

    def test_jev_rejection_hands_the_pr_to_a_person_on_later_miner_runs(self):
        self.prs = [{"html_url": self.URL, "pull_request": {}}]
        self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)
        with mock.patch.object(miner, "judge_prompts", side_effect=[(False, "JEV answered no."),
                                                                   (True, "JEV answered yes.")]) as judge:
            self.assertEqual(self.run_miner(), 0)
            self.assertIn(miner.REVIEW_LABEL, self.labels)
            initial_writes = self.calls.copy()
            comments = [call[2] for call in initial_writes if call[0] == "comment"]
            self.assertEqual(len(comments), 1)
            self.assertIn("JEV answered no.", comments[0])
            self.assertEqual(self.run_miner(), 0)
            judge.assert_called_once()
        self.assertEqual(self.calls, initial_writes)
        self.assertEqual(self.ci_reads, [self.HEAD])
        self.eval.assert_called_once()
        self.proposals.assert_not_called()
