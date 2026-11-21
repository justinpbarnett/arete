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
        self.reported_ci_head = ""
        self.behind = False
        self.merged = False
        self.during_merge = lambda: None
        self.calls = []
        self.ci_reads = []
        self.prs = []
        self.stored = []
        self.root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.layer = config.RepoLayer(self.root, trust_level="automerge")
        self.enterContext(mock.patch.object(config, "load_layer", return_value=self.layer))
        self.enterContext(mock.patch.multiple(
            gh, gh_json=self.github_read, gh=self.github_write, me=lambda: "operator",
            pr_comment=lambda url, body: self.calls.append(("comment", url, body)),
            find_issue_comment_marker=lambda repo, number, marker: next(
                (call[1] for call in self.calls if call[0] == "comment" and marker in call[2].splitlines()), ""),
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
        if args == ["api", f"repos/{self.repo.github}/pulls/8"]:
            return {"state": "open", "draft": False, "user": {"login": "operator"}, "body": "Why: fixes the cited failure.",
                    "labels": [{"name": miner.MINER_LABEL}],
                    "base": {"sha": self.base, "ref": self.base_name},
                    "head": {"sha": self.head, "ref": self.BRANCH, "repo": {"full_name": self.repo.github}}}
        self.fail(f"Unexpected GitHub read {args}")

    def github_write(self, args, **kwargs):
        checks = f"repos/{self.repo.github}/commits/{self.head}/check-runs?check_name=arete-checks&filter=latest&per_page=100"
        if args == ["api", checks]:
            self.assertFalse(kwargs["retry"])
            self.ci_reads.append(self.head)
            runs = [{"head_sha": self.reported_ci_head or self.head, "name": miner.MINER_CHECK, "app": {"id": 15368},
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

    def run_miner(self, *, proposals=(), dry_run=False, apply=True):
        self.enterContext(mock.patch.multiple(
            miner, collect=mock.DEFAULT, collect_runs=mock.DEFAULT,
            render_events=mock.DEFAULT, proposal_status=mock.DEFAULT, apply_repo=mock.DEFAULT,
        ))
        miner.collect.return_value = [{"items": [], "cwd": "", "session": "fake"}] if proposals else []
        miner.collect_runs.return_value = [], 0
        miner.render_events.return_value = ""
        miner.proposal_status.return_value = "pending", ""
        miner.apply_repo.return_value = self.url, list(proposals)
        self.enterContext(mock.patch.object(miner.auditor, "latest_findings", return_value="(none)"))
        self.enterContext(mock.patch.object(miner.insights_store, "feedback_between", return_value=[]))
        self.enterContext(mock.patch.object(miner.jev, "ready", return_value=False))
        self.enterContext(mock.patch.object(miner.runner, "run", return_value=SimpleNamespace(
            ok=True, cost_usd=0, structured={"proposals": list(proposals)}, error="", paid_usd=0,
        )))
        self.enterContext(mock.patch.object(miner.record, "settle"))
        now = util.now()
        return miner._run(self.cfg, "min-test", now - timedelta(days=1), now, True, "", dry_run, apply)

    def test_fresh_pr_merges_at_the_checked_head_without_moving_checkout_or_blocking_admission(self):
        proposal = {"repo": "arete", "layer": "repo", "file": self.PROMPT, "change": "fix prompt"}
        self.prs = [{"html_url": self.URL, "pull_request": {}}]
        with mock.patch.object(pipeline.scratch, "process_ids", return_value=set()), mock.patch.object(
            pipeline.scratch, "service_labels", return_value=set(),
        ), mock.patch.object(pipeline.paths, "ensure_dirs"), mock.patch.object(
            config, "load", return_value=self.cfg,
        ), mock.patch.object(pipeline, "run_refusal", return_value=(False, "fixture reached admission")) as admission, \
                mock.patch.object(util, "write_bytes_atomic") as receipt:
            self.during_merge = lambda: self.assertEqual(pipeline.run_issue(self.repo.name, 7), 1)
            self.assertEqual(self.run_miner(proposals=[proposal]), 0)
            admission.assert_called_once_with(self.repo, False)
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

    def test_pending_missing_or_failing_ci_is_read_once_and_leaves_the_pr_open(self):
        for status in ("pending", "failure", "missing"):
            with self.subTest(status=status):
                self.status = status
                self.ci_reads.clear()
                self.assertIn("does not have a passing", self.decide())
                self.assertEqual(self.ci_reads, [self.HEAD])
                self.assertFalse(self.calls)
        self.proposals.assert_not_called()

    def test_oversized_or_incomplete_pr_stays_open(self):
        self.diff.return_value = diffguard.Diff({self.PROMPT: miner.MAX_MINER_DIFF + 1})
        self.assertIn("over the", self.decide())
        self.eval.assert_not_called()
        self.diff.return_value = diffguard.Diff({self.PROMPT: 2})
        self.eval.return_value = evals.Comparison("Eval table.", incomplete=["`unavailable-task`"])
        self.assertIn("incomplete", self.decide())
        self.assertIn("incomplete", self.decide())
        self.assertFalse(self.merged)
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertIn("unavailable-task", self.calls[0][2])

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
                self.assertEqual(miner.apply_repo(self.cfg, "min-test", self.repo, [proposal], self.BRANCH), ("", []))
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
        self.assertIn("| all | 2/3 | 1/2 (1 incomplete) |", table)
        self.assertEqual(self.calls[1], ("pr", "close", self.URL))
        self.assertFalse(self.merged)
        self.proposals.assert_called_once_with(self.URL, "rejected")

    def test_out_of_scope_prs_receive_no_actions(self):
        self.behind = True
        for extra in ("CLAUDE.md", "skills/example/SKILL.md", "arete/pipeline.py", "arete/prompts/fix.md"):
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

    def test_outage_after_recording_inputs_defers_comparison_and_skips_negative_history(self):
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
            evals.insights_store.add_eval.assert_called_once()
            self.assertEqual(evals.insights_store.add_eval.call_args.args[:4], ("eval-base", "arete", task.name, True))
        self.assertIn("1 incomplete", self.calls[0][2])
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertFalse(self.merged)
        self.proposals.assert_not_called()
        judge.assert_not_called()

    def test_grader_failure_and_blocked_verification_have_distinct_outcomes(self):
        task = evals.Task("fixture", "Fixture", "fixture", verify="Try the behavior")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root)
        for grade, outcome in ((verify.Verified("fail", "Behavior failed."), "fail"),
                               (verify.Verified("blocked", "Runner timed out.", finished=False), "incomplete")):
            with self.subTest(grade=grade.verdict), mock.patch.multiple(
                evals, git=mock.Mock(return_value=""), grade=mock.Mock(return_value=grade),
            ), mock.patch.object(evals.vault, "hidden", side_effect=str):
                result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                          "", "", True, 60)
                self.assertEqual(result.outcome, outcome)

    def test_combined_grading_preserves_a_failure_when_either_grader_is_unavailable(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="fixture-check", verify="Try the behavior")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root, verify=config.Verify())
        for verdict, check, outcome, failures in (
            ("fail", util.CommandIncomplete(task.check, 124, "", "timed out"), "fail", ["failed in use: fixture"]),
            ("blocked", subprocess.CompletedProcess(task.check, 1, "", ""), "fail", ["check exit 1"]),
            ("pass", util.CommandIncomplete(task.check, 124, "", "timed out"), "incomplete", []),
            ("blocked", subprocess.CompletedProcess(task.check, 0, "", ""), "incomplete", []),
            ("pass", subprocess.CompletedProcess(task.check, 0, "", ""), "pass", []),
        ):
            with self.subTest(verdict=verdict, check=check), mock.patch.multiple(
                evals, git=mock.Mock(return_value=""), grade=mock.Mock(return_value=verify.Verified(verdict, "fixture")),
                hidden_check=mock.Mock(return_value=check),
            ), mock.patch.object(evals.vault, "hidden", side_effect=str):
                result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                          "", "", True, 60)
                evals.hidden_check.assert_called_once()
                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.failures, failures)

    def test_held_out_failure_survives_a_blocked_in_use_verifier(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="fixture-check", verify="Try the behavior")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root, verify=config.Verify())
        with mock.patch.multiple(
            evals, git=mock.Mock(return_value=""),
            hidden_check=mock.Mock(return_value=subprocess.CompletedProcess(task.check, 1, "", "")),
        ), mock.patch.object(evals.vault, "hidden", side_effect=str), mock.patch.object(
            verify, "port_refusal", return_value="Fixture port is unavailable.",
        ):
            result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                      "", "", True, 60)
        self.assertEqual(result.outcome, "fail")
        self.assertEqual(result.verify, "blocked")
        self.assertEqual(result.failures, ["check exit 1"])

    def test_acceptance_distinguishes_invoked_missing_cli_from_infrastructure_failure(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="expected-command")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root)
        for process, outcome in (
            (subprocess.CompletedProcess(task.check, 127, "", "sh: expected-command: command not found"), "fail"),
            (FileNotFoundError("shell launcher unavailable"), "incomplete"),
            (subprocess.TimeoutExpired(task.check, 60), "incomplete"),
            (subprocess.CompletedProcess(task.check, 1, "", "sandbox-exec: sandbox_apply: Operation not permitted"),
             "incomplete"),
        ):
            with self.subTest(process=process), mock.patch.object(evals, "git", return_value=""), mock.patch.object(
                util, "run_whole_tree", side_effect=[process],
            ), mock.patch.object(evals.vault, "hidden", side_effect=lambda text, *args: text):
                result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                          "", "", True, 60)
                self.assertEqual(result.outcome, outcome)
                if outcome == "fail":
                    self.assertEqual(result.failures, ["check exit 127"])

    def test_unmeasured_requires_jev_yes_and_records_both_answers(self):
        self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)
        with mock.patch.object(jev, "ready", return_value=True), mock.patch.object(miner.vault, "hidden", side_effect=str):
            for value in (False, True):
                with self.subTest(value=value):
                    self.calls.clear()
                    answer = jev.Answer(value, 1.0, {"yes": float(value)})
                    with mock.patch.object(jev, "ask", return_value=jev.Reply({"fix": answer})) as ask:
                        self.decide()
                    self.assertIn("reported failure and rationale", ask.call_args.args[0])
                    self.assertIn(f"JEV answered {'yes' if value else 'no'}", self.calls[0][2])
                    self.assertEqual(self.merged, value)
                    if not value:
                        self.assertIn(("pr", "edit", self.URL, "--add-label", "agent:needs-review"), self.calls)
