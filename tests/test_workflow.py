import subprocess
import tempfile
import tomllib
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from arete import (
    ci, config, factory, flows, gh, mergecheck, orchestration, owner_asks, pipeline, sandbox, tracker, util, verify, workspace,
)
from arete.agent import NO_SESSION
from arete.review import Review


class MovedBasePublicationTests(unittest.TestCase):
    def test_conflict_after_a_rerun_passes_reaches_automerge(self):
        checkout = workspace.Checkout("", "fixture/repo", Path("."), Path("."), "main",
                                      pr="https://github.com/fixture/repo/pull/7", lease="head")
        controller = orchestration.Controller.__new__(orchestration.Controller)
        controller.run = mock.Mock(dry_run=False, tid="7", repo=SimpleNamespace(parts=[checkout]),
                                   waiting=mock.Mock(return_value=nullcontext()))
        controller.job = mock.Mock(with_prs=[checkout], picked=mock.Mock(return_value=[checkout]),
                                   verified=verify.Verified("pass"))
        controller.data = {"evidence": {"checks": {"result": {"full": True}}}}
        controller.workers = {}
        controller.current = mock.Mock(return_value=True)
        controller.fingerprint = mock.Mock(return_value="tree")
        controller.persist = mock.Mock()
        controller.atomic = mock.Mock(return_value=factory.Outcome("pass"))
        controller.catalog = mock.Mock(context={})
        controller.finish_publication = mock.Mock(return_value={"reached_automerge": True})
        clean = {"state": "OPEN", "headRefOid": checkout.lease, "mergeable": "MERGEABLE"}
        conflict = {**clean, "mergeable": "CONFLICTING"}
        failed, passed = gh.Check("checks", "fail"), gh.Check("checks", "pass")
        with (mock.patch.object(gh, "pr_merge_details", side_effect=[clean, clean, conflict]),
              mock.patch.object(gh, "commit_checks", side_effect=[{"checks": failed}, {"checks": passed},
                                                                 {"checks": passed}]),
              mock.patch.object(ci.time, "monotonic", return_value=0),
              mock.patch.object(ci.time, "sleep") as sleep,
              mock.patch.object(pipeline, "publish_ci") as report,
              mock.patch.object(pipeline, "set_draft") as draft):
            result = controller.publish()
        self.assertEqual(result, {"reached_automerge": True})
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("Passed: `checks`", report.call_args.args[3])
        self.assertIn("merge conflict", report.call_args.args[3])
        draft.assert_not_called()

    def test_catch_up_ci_failure_uses_publication_diagnosis(self):
        checkout = workspace.Checkout("", "fixture/repo", Path("."), Path("."), "main",
                                      pr="https://github.com/fixture/repo/pull/7", lease="new-head")
        controller = orchestration.Controller.__new__(orchestration.Controller)
        run = mock.Mock(tid="7", repo=SimpleNamespace(parts=[checkout], public=False), feedback=[], repairs="")
        job = mock.Mock(run=run, with_prs=[checkout], picked=mock.Mock(return_value=[checkout]),
                        approval=None, draft_why="", found=[], build_summary="updated")
        controller.run, controller.job, controller.data, controller.catalog = run, job, {}, mock.Mock()
        job.ticket.labels = []
        job.ticket.title = "Edit fixture"
        job.b = SimpleNamespace(run=run, layer=SimpleNamespace(trust_level="automerge"), ticket=job.ticket)
        target = pipeline.ci_target(checkout)
        failure = gh.Check("checks", "fail")
        current = ci.Round(1, [ci.Watched(target, {"checks": failure}, reds=[ci.Red(target, failure, None, "")])])
        with (mock.patch.object(pipeline, "merge_with_catch_ups", return_value=("CI needs repair", current)) as catch_up,
              mock.patch.object(controller, "ci_failures", return_value="failed check logs"),
              mock.patch.object(pipeline, "set_draft") as draft,
              mock.patch.object(gh, "pr_comment") as comment):
            result = flows.publish(SimpleNamespace(ctx=job), ci_guard=controller.publication_ci_error)
        self.assertFalse(result.passed)
        self.assertEqual(result.data["failures"], orchestration.CI_FAILURES)
        self.assertTrue(controller.data["ci_failed"])
        controller.catalog.add_context.assert_called_once_with(orchestration.CI_FAILURES, "failed check logs")
        draft.assert_called_once_with(run, checkout, True)
        catch_up.assert_called_once()
        comment.assert_not_called()
        run.say.assert_not_called()

    def test_resolver_question_cleans_its_untracked_file_and_names_moved_base(self):
        root = Path(".")
        checkout = workspace.Checkout("", "fixture/repo", root, root, "main", pr="pr", lease="head")
        run = SimpleNamespace(tid="7", repo=SimpleNamespace(public=False), owner_questions_blocked=False)
        build = SimpleNamespace(run=run, layer=object(), ticket=object(), checked={}, report="", staged=None,
                                catch_ups=0)
        refs = {"state": "OPEN", "isDraft": False, "headRefOid": "head", "baseRefName": "main",
                "baseRefOid": "moved-base"}
        untracked = set()

        def resolve(*args):
            self.assertTrue(run.owner_questions_blocked)
            untracked.add("resolver-output")
            raise owner_asks.Asked([], [], "publish", "resolve", NO_SESSION)

        with (mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")),
              mock.patch.object(gh, "pr_merge_details", return_value=refs),
              mock.patch.object(pipeline, "fetch_merge_base", return_value="moved-base"),
              mock.patch.object(pipeline, "git", return_value="head"),
              mock.patch.object(workspace, "uncommitted", side_effect=lambda _: sorted(untracked)),
              mock.patch.object(workspace, "untracked_files", side_effect=lambda _: untracked.copy()),
              mock.patch.object(workspace, "remove_untracked", side_effect=lambda _, name: untracked.remove(name)),
              mock.patch.object(pipeline, "merge_moved_bases", side_effect=resolve),
              mock.patch.object(pipeline, "abort_merge") as abort,
              mock.patch.object(pipeline, "commit_and_push") as push):
            hold, _ = pipeline.merge_with_catch_ups(build, checkout, None, None, "", "ticket", None, "", "")
        self.assertIn("`main` at `moved-base`", hold)
        self.assertIn("needs owner input", hold)
        self.assertFalse(untracked)
        self.assertFalse(run.owner_questions_blocked)
        abort.assert_called_once_with(root)
        push.assert_not_called()


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
