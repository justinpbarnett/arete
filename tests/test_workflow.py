import ast
import http.client
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from datetime import timedelta
from io import StringIO
from pathlib import Path
from socket import socketpair
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from adws import single_worker
from arete import (
    api_check, ci, codex, config, dashboard, diffguard, evals, factory, flows, gh, guard, install, jev, journeys,
    loop_run, mergecheck, miner, orchestration, outbound, outcomes, owner_asks, paths, pipeline, poll, principals,
    prompt_loader, queueing, runlimits, sandbox, serve, serve_guard, slots, tailnet, tracker, util, verify, workspace,
)
from arete.agent import NO_SESSION, Result
from arete.orchestration_api import OPERATIONS, help_text
from arete.review import Review, catch_up_problems
from arete.store import db, orchestration as ledger
from tests.parallel import stack_env


class QueueScoringTests(unittest.TestCase):
    def setUp(self):
        remote = "fixture/repo"
        self.repo = config.Repo("fixture", Path("."), github=remote, parts=[config.Part("", remote)])
        self.source = tracker.for_repo(config.Config(), self.repo)

    def issue(self, number, *labels, body="", created="2026-11-21T00:00:00Z", author="fixture"):
        return {"number": number, "title": f"Issue {number}", "body": body, "createdAt": created,
                "author": {"login": author}, "labels": [{"name": name} for name in ("agent", *labels)]}

    def next_ticket(self):
        self.enterContext(mock.patch.object(poll.runlimits, "latest_park", return_value=(None, None)))
        self.enterContext(mock.patch.multiple(poll.run_store, issue_running=mock.Mock(return_value=False),
                                             pending_label=mock.Mock(return_value=""),
                                             environment_waits=mock.Mock(return_value=[])))
        self.enterContext(mock.patch.object(poll.pilot, "pickable", return_value=True))
        self.enterContext(mock.patch.object(poll.zte, "reserved", return_value=False))
        self.enterContext(mock.patch.object(tracker.LocalTracker, "todo", return_value=[]))
        return poll.next_ticket(self.repo, self.source, pipeline.SkipLog({}))

    def test_waits_on_ignores_oversized_reference(self):
        oversized = "9" * 4500
        self.assertEqual(tracker.waits_on(f"Waits on #12, #{oversized}, #34 and #12."), [12, 34])

    def test_poll_uses_valid_reference_alongside_oversized_reference(self):
        oversized = "9" * 4500
        body = (f"Waits on #{oversized}, fixture/repo#{oversized}, "
                f"https://github.com/fixture/repo/issues/{oversized} and #1.")
        issues = [self.issue(1), self.issue(2, "agent:pr", body=body), self.issue(3, created="2026-11-20T00:00:00Z")]
        self.enterContext(mock.patch.object(gh, "issues", return_value=issues))

        self.assertEqual(self.next_ticket().id, "1")

    def test_poll_ranks_only_same_repository_references(self):
        cases = [
            ("Fixture/Repo#708", "708"),
            ("https://github.com/Fixture/Repo/issues/708", "708"),
            ("fixture/other#708", "692"),
            ("https://github.com/fixture/other/issues/708", "692"),
        ]
        for reference, expected in cases:
            with self.subTest(reference=reference):
                issues = [self.issue(708), self.issue(283, "agent:pr", body=f"Waits on {reference}."),
                          self.issue(692, created="2026-11-20T00:00:00Z")]
                with mock.patch.object(gh, "issues", return_value=issues):
                    self.assertEqual(self.next_ticket().id, expected)

    def test_poll_prioritizes_prerequisite_of_in_flight_chain(self):
        issues = [self.issue(1), self.issue(2, "agent:pr", body="Waits on #1."),
                  self.issue(3, "agent:running", body="Waits on #2."),
                  self.issue(4, created="2026-11-20T00:00:00Z")]
        self.enterContext(mock.patch.object(gh, "issues", return_value=issues))

        todo = self.source.todo()
        self.assertEqual([ticket.id for ticket in todo], ["4", "1"])
        self.assertEqual(self.next_ticket().id, "1")
        self.assertEqual(queueing.scores(list(self.source.open_agent_tickets))["1"], (3, 2))

    def test_poll_counts_local_waits_on_inside_underscores(self):
        older = [self.issue(number, created="2026-11-20T00:00:00Z") for number in (692, 693, 697, 698)]
        issues = [*older, self.issue(708), self.issue(283, "agent:pr", body="__Waits on #708__")]
        self.enterContext(mock.patch.object(gh, "issues", return_value=issues))

        todo = self.source.todo()
        ranked = queueing.prioritized(todo, open_tickets=self.source.open_agent_tickets)
        self.assertEqual([ticket.id for ticket in ranked], ["708", "692", "693", "697", "698"])
        self.assertEqual(self.next_ticket().id, "708")

    def test_poll_uses_one_limited_listing_and_main_prerequisite_cache(self):
        candidate = self.issue(1, body="Waits on #1001.")
        issues = [candidate, *(self.issue(n, "agent:pr") for n in range(2, 1001))]
        github = self.enterContext(mock.patch.object(gh, "gh_json", side_effect=[issues, {"state": "CLOSED"}]))

        self.assertEqual(self.next_ticket().id, "1")
        self.assertEqual(len(self.source.open_agent_tickets), 1000)
        self.assertIs(self.source.is_open(2), True)
        self.assertIs(self.source.is_open(1001), False)
        self.assertEqual(github.call_args_list, [
            mock.call(["issue", "list", "--repo", "fixture/repo", "--state", "open", "--limit", "1000", "--json",
                       "number,title,body,labels,url,createdAt,updatedAt,author", "--label", "agent"]),
            mock.call(["issue", "view", "1001", "--repo", "fixture/repo", "--json",
                       "number,title,body,labels,url,comments,state,author,updatedAt"]),
        ])

    def test_poll_preserves_candidate_cycle_ranking_with_unrelated_in_flight_issue(self):
        issues = [self.issue(1), self.issue(2, body="Waits on #1 and #3."), self.issue(3, body="Waits on #2."),
                  self.issue(5), self.issue(6, body="Waits on #5.")]
        with mock.patch.object(gh, "issues", return_value=issues):
            self.assertEqual(self.next_ticket().id, "5")

        with mock.patch.object(gh, "issues", return_value=[*issues, self.issue(7, "agent:pr")]):
            self.assertEqual(self.next_ticket().id, "5")

    def test_rank_counts_failed_and_other_ineligible_open_agent_tickets(self):
        issues = [self.issue(1), self.issue(2, created="2026-11-19T00:00:00Z"),
                  self.issue(3, "agent:failed", body="Waits on #1, #1, #2 and #6."),
                  self.issue(4, "agent:zte", body="Waits on #1."), self.issue(5, body="Waits on #1.", author="other"),
                  self.issue(6, created="2026-11-20T00:00:00Z")]
        self.enterContext(mock.patch.object(gh, "issues", return_value=issues))

        todo = self.source.todo()
        self.assertEqual([ticket.id for ticket in todo], ["2", "6", "1"])
        open_tickets = self.source.open_agent_tickets
        scores = queueing.scores(list(open_tickets))
        self.assertEqual((scores["1"], scores["2"], scores["6"]), ((2, 3), (2, 1), (2, 1)))
        ranked = queueing.prioritized(todo[::-1], open_tickets=open_tickets)
        self.assertEqual([ticket.id for ticket in ranked], ["1", "2", "6"])

        parked = self.source.todo(parked=lambda tid: tid == "3")
        self.assertEqual({ticket.id for ticket in parked}, {"1", "2", "3", "6"})
        self.assertEqual(queueing.scores(list(self.source.open_agent_tickets)), scores)

    def test_todo_refresh_drops_dependents_no_longer_in_open_agent_list(self):
        first = [self.issue(1), self.issue(2, "agent:pr", body="Waits on #1 and #99.")]
        self.enterContext(mock.patch.object(gh, "issues", side_effect=[first, first[:1]]))

        self.source.todo()
        self.assertEqual(queueing.scores(list(self.source.open_agent_tickets)), {"1": (2, 1), "2": (1, 0)})

        self.source.todo()
        self.assertEqual(queueing.scores(list(self.source.open_agent_tickets)), {"1": (1, 0)})


class PublicationFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.file = self.root / "file.txt"
        self.set_up_repository()
        self.checkout = workspace.Checkout("", "fixture/repo", self.root, self.root, "main")
        self.controller = orchestration.Controller.__new__(orchestration.Controller)
        run = mock.Mock(id="fixture-run", environment_release="", dry_run=False)
        self.build = pipeline.Build(cfg=config.Config(), run=run, ticket=mock.Mock(), ws=self.root,
                                    checkouts=[self.checkout], layer=config.RepoLayer(dir=self.root), timeout=0,
                                    workspace="fixture", tools=[], deny=[])
        self.controller.run = run
        self.controller.job = SimpleNamespace(b=self.build, checkouts=[self.checkout], verified=verify.Verified("pass"))
        self.controller.lease, self.controller.ending, self.controller.workers = "fixture-lease", None, {}
        self.controller.data = {"acceptance": "task", "evidence": {"checks": {"result": {"full": True}}}}
        self.controller.current = mock.Mock(return_value=True)
        self.controller.policy_sha = mock.Mock(return_value="policy")
        self.controller.persist = mock.Mock()
        self.controller.atomic = mock.Mock()
        self.action = {"id": "d0001-0", "op": "publication.request", "state": "accepted", "request": {}}
        self.transition = self.enterContext(mock.patch.object(ledger, "transition", wraps=ledger.transition,
                                                              return_value=None))
        self.enterContext(mock.patch.object(pipeline.record, "mark"))
        self.enterContext(mock.patch.object(orchestration.run_lines, "write"))

    def set_up_repository(self):
        self.create_repository(self.root)

    @staticmethod
    def create_repository(root):
        def git(*args):
            util.git(list(args), root)

        git("init", "-q", "--template=", "-b", "main")
        (root / "file.txt").write_text("base\n")
        git("add", "--", "file.txt")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
            "-c", "commit.gpgSign=false", "commit", "-qm", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args):
        return util.git(list(args), self.root).strip()

    def commit(self, subject, *files):
        self.git("add", "--", *(files or ("file.txt",)))
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                 "-c", "commit.gpgSign=false", "commit", "-qm", subject)

    def record_pushed_head(self):
        self.file.write_text("implementation\n")
        self.commit("implementation")
        self.checkout.note_pushed_head(self.git("rev-parse", "HEAD"))
        self.checkout.pr = "https://github.com/fixture/repo/pull/7"
        self.controller.data["publication"] = {"heads": {self.checkout.github: self.checkout.lease}}

    def publication_job(self, repo, branch, *, verified, carried=False, continues="", stamp=True):
        c, controller, b = self.checkout, self.controller, self.build
        run = controller.run
        run.configure_mock(dir=self.root, tid="7", repairs="", feedback=[], feedback_prs=[], facts={}, story=[],
                           resolved_settings={}, repo=repo, waiting=mock.Mock(return_value=nullcontext()))
        run.tracker.pr_link.return_value = "Closes #7"
        run.tracker.pr_title.return_value = "Edit fixture"
        b.ticket = tracker.Ticket("7", "#7", "Edit fixture")
        run.layer = b.layer
        job = flows.Job(b.cfg, run, c.wt, ticket=b.ticket, branch=branch, checkouts=[c], layer=b.layer, built=b,
                        slot=mock.Mock(), carried=carried, continues=continues, verified=verified,
                        review=Review.stored({"verdict": "approve"}))
        flow = SimpleNamespace(ctx=job, skipped=0)
        flow.run = lambda name, args, kw: getattr(flows, name)(flow, *args, **kw)
        flow.step = lambda name, **kw: flow.run(name, (), kw)
        controller.job, controller.flow = job, flow
        controller.catalog = mock.Mock(context={})
        del controller.atomic, controller.current
        controller.executing = "prior-action"
        if stamp:
            controller.stamp("checks", {"full": True})
            controller.stamp("review", job.review.asdict())
        return job


class LiveOutsideEventPublicationTests(PublicationFixture):
    def test_rendered_prompt_requests_publication_for_one_live_outside_event(self):
        rendered = prompt_loader.prompt("orchestrate", api=help_text(), observation="{}")

        self.assertIn(
            "Ask the owner only for information or permission you cannot obtain safely through workers.\n"
            "When verification can check everything in use except one requirement that depends on a live outside "
            "event the run cannot cause", rendered)
        self.assertIn("do not ask the owner for that event or a decision about publishing without it.", rendered)
        self.assertIn("explicitly list that requirement as unverified in its summary and record the stubbed evidence "
                      "that covers it, then request publication so both appear in the PR body.", rendered)
        self.assertIn("Keep the existing automerge rules and continue to ask for credentials, permissions, "
                      "and decisions the issue leaves open.", rendered)

    def test_publication_request_includes_the_unverified_requirement_and_stubbed_evidence(self):
        controller, checkout = self.controller, self.checkout
        self.file.write_text("implementation\n")
        self.build.layer.verify = config.Verify(publish_evidence=False)
        repo = config.Repo("fixture", self.root, parts=[config.Part("", checkout.github)])
        self.publication_job(repo, "arete/7-edit-fixture", verified=verify.Verified("unverified"))
        controller.run.path = "orchestrate"
        controller.catalog.route.return_value = SimpleNamespace(step="verify")
        verification = {"id": "d0001-0", "op": "verification.run", "state": "accepted",
                        "request": {"route_id": "fixture", "effort": "", "goal_refs": []}}
        publication = {**self.action, "id": "d0002-0"}
        requirement = "A real PR with an unrelated hosted CI timeout gets one rerun and automerges on green."
        stub_observation = "The stub recorded one rerun request and a merge after green CI."
        verified = verify.Verified.from_agent({
            "verdict": "blocked",
            "summary": f"Unverified in use: {requirement}",
            "blocked_by": "The run cannot cause an unrelated timeout on a hosted runner.",
            "checks": [
                {"what": "A PR with green CI completes publication", "how": "Publish with green CI",
                 "saw": "The PR was published", "passed": True},
                {"what": "Rerun an unrelated timeout with a stubbed GitHub response",
                 "how": "Replay a timeout followed by a green rerun", "saw": stub_observation, "passed": True},
            ],
        })
        pr = "https://github.com/fixture/repo/pull/7"
        with (mock.patch.object(pipeline, "check_in_use", return_value=verified),
              mock.patch.object(pipeline, "commit_and_push"),
              mock.patch.object(pipeline, "release_workspace"),
              mock.patch.object(pipeline, "supersede_older_runs"),
              mock.patch.object(pipeline.record, "note"),
              mock.patch.object(pipeline.record, "metered_spent", return_value=0),
              mock.patch.object(ci, "watch", side_effect=lambda target, log: ci.Watched(target, {})),
              mock.patch.object(gh, "open_pr_url", return_value=""),
              mock.patch.object(gh, "pr_create", return_value=pr) as create,
              mock.patch.object(gh, "pr_body", side_effect=lambda _: create.call_args.args[4]),
              mock.patch.object(gh, "pr_edit_body") as edit):
            self.assertIsNone(controller.dispatch(verification))
            self.assertIsNone(controller.dispatch(publication))

        self.assertEqual(self.transition.call_args.args[:3], (controller.lease, publication["id"], "succeeded"))
        self.assertEqual((controller.ending[0], controller.ending[2]), ("pr", pr))
        create.assert_called_once()
        edit.assert_called_once()
        body = edit.call_args.args[1]
        self.assertIn(f"Unverified in use: {requirement}", body)
        self.assertIn("**Passed**: A PR with green CI completes publication", body)
        self.assertIn(stub_observation, body)


class UnchangedHeadCIRetryTests(PublicationFixture):
    def setUp(self):
        super().setUp()
        self.record_pushed_head()
        c, controller = self.checkout, self.controller
        pushed_tree = factory.fingerprint(c.wt)
        self.assertTrue(pushed_tree)
        self.enterContext(mock.patch.object(factory, "fingerprint", return_value=pushed_tree))
        repo = config.Repo("fixture", self.root, parts=[config.Part("", c.github)])
        job = self.publication_job(repo, "arete/7-edit-fixture", verified=verify.Verified("pass"))
        job.changed = job.opened = [c.github]
        job.b.layer.trust_level = "automerge"
        controller.stamp("verify", {"verdict": "pass"})

        api = f"repos/{c.github}"
        self.run_path = f"{api}/actions/runs/456"
        self.workflow_runs_path = f"{api}/actions/runs?event=pull_request&head_sha={c.lease}&per_page=100"
        self.jobs_path = f"{self.run_path}/attempts/3/jobs?per_page=100"
        self.failed_job_path = f"{api}/actions/jobs/123"
        self.failed = self.actions_job(123, 456, 2, "failure")
        self.head_checks = [self.failed]
        self.other_pr_check = self.actions_job(200, 789, 1, "success")
        self.original_run = self.actions_run(456, 7, 2, "completed", "failure")
        self.other_pr_run = self.actions_run(789, 8, 1, "completed", "failure")
        self.workflow_runs = [self.original_run]

        def transport(args, **options):
            endpoint = args[1]
            if endpoint == self.failed_job_path:
                return self.failed
            if endpoint == f"{api}/actions/jobs/200":
                return self.other_pr_check
            if endpoint == f"{api}/actions/runs/789":
                return self.other_pr_run
            if endpoint.startswith(f"{api}/actions/jobs/"):
                return next(row for row in self.head_checks if row["id"] == int(endpoint.rsplit("/", 1)[1]))
            if endpoint == self.workflow_runs_path:
                return [{"workflow_runs": self.workflow_runs}]
            if endpoint == self.run_path:
                return self.actions_run(456, 7, *next(self.run_snapshots))
            if endpoint == f"{self.run_path}/attempts/3":
                return self.exact_attempt
            if endpoint == f"{self.run_path}/attempts/2/jobs?per_page=100":
                return [{"jobs": [self.failed]}]
            if endpoint == self.jobs_path:
                return [{"jobs": next(self.job_snapshots)}]
            if endpoint.startswith(f"{api}/commits/{c.lease}/check-runs?"):
                return [{"check_runs": self.head_checks}]
            if endpoint.startswith(f"{api}/commits/base/check-runs?"):
                return [{"check_runs": []}]
            if "/statuses?" in endpoint:
                return [[]]
            self.fail(f"unexpected GitHub endpoint {endpoint}")

        self.enterContext(mock.patch.object(gh, "branch_sha", return_value="base"))
        self.enterContext(mock.patch.object(gh, "pr_merge_details", return_value={
            "state": "OPEN", "headRefOid": c.lease, "baseRefName": c.base, "mergeable": "MERGEABLE",
        }))
        self.enterContext(mock.patch.object(ci, "SETTLE_SECONDS", 0))
        self.sleep = self.enterContext(mock.patch.object(ci.time, "sleep"))
        self.enterContext(mock.patch.object(controller, "ci_failures", return_value="checks failed"))
        self.enterContext(mock.patch.object(pipeline, "publish_ci"))
        self.enterContext(mock.patch.object(pipeline, "release_workspace"))
        self.enterContext(mock.patch.object(pipeline, "record_prs", return_value=c.pr))
        self.github = self.enterContext(mock.patch.object(gh, "gh_json", side_effect=transport))
        self.command = self.enterContext(mock.patch.object(gh, "gh"))
        self.draft = True

        def set_draft(pr, draft):
            self.assertEqual(pr, c.pr)
            self.draft = draft

        self.set_draft = self.enterContext(mock.patch.object(gh, "pr_set_draft", side_effect=set_draft))
        self.enterContext(mock.patch.object(gh, "pr_draft_and_head", side_effect=lambda _: (self.draft, c.lease)))
        self.merge = self.enterContext(mock.patch.object(pipeline, "try_automerge", return_value=None))
        self.record_ci_failure()

        for obj, name in ((job.b, "restage"), (job.b, "require_valid_diff"),
                          (controller, "validate_publication_diff"), (pipeline, "commit_and_push")):
            call = self.enterContext(mock.patch.object(obj, name, side_effect=AssertionError(f"unexpected {name}")))
            self.addCleanup(call.assert_not_called)

    def record_ci_failure(self):
        controller, c = self.controller, self.checkout
        self.assertIn("error", controller.publication_ci([c]))
        self.assertEqual(controller.data["published_tree"], controller.fingerprint())
        self.assertTrue(controller.data["ci_failed"])

    def actions_run(self, run, pr, attempt, status, conclusion, *, workflow_id=99):
        return {"id": run, "head_sha": self.checkout.lease, "workflow_id": workflow_id, "run_number": run,
                "event": "pull_request", "pull_requests": [{"number": pr}],
                "run_attempt": attempt, "status": status, "conclusion": conclusion}

    def actions_job(self, job, run, attempt, conclusion, name="arete-checks"):
        return {"id": job, "run_id": run, "run_attempt": attempt, "head_sha": self.checkout.lease,
                "name": name, "status": "completed", "conclusion": conclusion,
                "app": {"id": gh.GITHUB_ACTIONS_APP_ID, "slug": "github-actions"}}

    def request_retry(self, action=None):
        waits = self.controller.run.waiting.call_args_list[:]
        self.assertIsNone(self.controller.dispatch(action or self.action))
        self.command.assert_called_once_with(["run", "rerun", "456", "--failed", "--repo", self.checkout.github],
                                             write=True)
        self.assertEqual(self.controller.run.waiting.call_args_list, [*waits, mock.call("ci")])

    def complete_retry(self, conclusion):
        self.head_checks.append(self.other_pr_check)
        previous, finished = (2, "completed", "failure"), (3, "completed", conclusion)
        self.run_snapshots = iter([previous, (3, "in_progress", None), finished, finished, finished])
        lagging_jobs = [[], [self.actions_job(124, 456, 2, "success")]] if conclusion == "success" else []
        self.job_snapshots = iter([*lagging_jobs, [self.actions_job(125, 456, 3, conclusion)]])

        def still_waiting(_):
            self.assertTrue(self.draft)
            self.merge.assert_not_called()

        self.sleep.side_effect = still_waiting
        self.request_retry()
        self.assertEqual(self.sleep.call_count, 2 + len(lagging_jobs))

    def test_publication_request_reruns_failed_jobs_and_resumes_publication_after_green_ci(self):
        def merge(*args, **kwargs):
            self.assertFalse(self.draft)

        self.merge.side_effect = merge
        self.complete_retry("success")
        self.merge.assert_called_once()
        self.assertEqual(self.controller.ending[0], "pr")
        self.assertNotIn("ci_failed", self.controller.data)
        self.assertEqual(self.transition.call_args.args[2], "succeeded")

    def test_second_red_on_the_same_head_fails_publication_with_the_exact_reason(self):
        self.complete_retry("failure")
        self.assertEqual(self.controller.data["ci_retry"]["targets"][self.checkout.github]["jobs"], [125])
        self.assert_second_red()

    def test_unchanged_request_resumes_timed_out_rerun_and_reports_its_failure(self):
        self.run_snapshots = iter([(3, "in_progress", None), (3, "completed", "failure")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "failure")]])
        with mock.patch.object(ci, "WAIT_SECONDS", 0):
            self.request_retry()
        self.assert_retry_held()

        self.controller.data = json.loads(json.dumps(self.controller.data))
        verify_evidence = self.controller.data["evidence"]["verify"]
        with mock.patch.dict(verify_evidence, tree="stale"):
            stale_action = {**self.action, "id": "d0002-1"}
            self.assertEqual(str(self.controller.dispatch(stale_action)), "publication needs current evidence: verify")
        self.assert_second_red()
        self.assertEqual(self.controller.run.waiting.call_args_list, [mock.call("ci")] * 3)

    def test_unchanged_request_resumes_timed_out_rerun_and_publishes_on_green(self):
        self.run_snapshots = iter([(3, "in_progress", None), (3, "completed", "success")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success")]])
        with mock.patch.object(ci, "WAIT_SECONDS", 0):
            self.request_retry()
        self.assert_retry_held()

        self.assertIsNone(self.controller.dispatch({**self.action, "id": "d0002-0"}))
        self.command.assert_called_once()
        self.merge.assert_called_once()
        self.assertFalse(self.draft)
        self.assertNotIn("ci_retry", self.controller.data)

    def assert_second_red(self):
        self.assertIsNone(self.controller.ending)
        self.assertTrue(self.draft)
        next_action = {**self.action, "id": "d0002-0"}
        self.assertIsNone(self.controller.dispatch(next_action))
        reason = f"CI failed twice on the unchanged head {self.checkout.lease}"
        self.assertEqual(self.controller.ending, ("failed", reason, self.checkout.pr))
        self.transition.assert_called_with(self.controller.lease, next_action["id"], "failed", {"error": reason})
        self.command.assert_called_once()
        self.merge.assert_not_called()

    def test_completed_attempt_without_the_replacement_job_keeps_publication_blocked(self):
        self.head_checks.append(self.other_pr_check)
        self.run_snapshots = iter([(3, "completed", "success")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success", name="other-job")]])
        with mock.patch.object(ci, "WAIT_SECONDS", 0):
            self.request_retry()
        self.assertTrue(self.controller.data["ci_failed"])
        self.assertIsNone(self.controller.ending)
        self.assertTrue(self.draft)
        self.merge.assert_not_called()

    def test_failed_attempt_without_the_replacement_job_counts_as_second_red(self):
        self.run_snapshots = iter([(3, "completed", "failure")])
        self.job_snapshots = iter([[]])
        with mock.patch.object(ci, "WAIT_SECONDS", 0):
            self.request_retry()
        reds = self.controller.ci_failures.call_args.args[0].reds
        self.assertEqual([r.check.description for r in reds], ["Workflow attempt 3 concluded failure"])
        self.assert_second_red()

    def test_newer_pending_attempt_keeps_publication_draft_after_the_requested_attempt_passes(self):
        self.run_snapshots = iter([(4, "in_progress", None)])
        self.exact_attempt = self.actions_run(456, 7, 3, "completed", "success")
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success")]])
        pending = self.actions_job(126, 456, 4, None)
        self.head_checks.append({**pending, "status": "in_progress"})
        with mock.patch.object(ci, "WAIT_SECONDS", 0):
            self.request_retry()
        self.assert_retry_held()
        watched, = self.controller.ci_failures.call_args.args[0].seen
        self.assertFalse(watched.read)
        self.assertEqual([check.source for check in watched.in_state("pending")], ["workflow:456"])

    def test_newer_failed_attempt_keeps_publication_draft_after_another_check_finishes(self):
        independent = self.actions_job(300, 789, 1, None, name="independent")
        self.head_checks.append({**independent, "status": "in_progress"})
        self.run_snapshots = iter([(3, "completed", "success"), (4, "completed", "failure")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success")]])

        def finish_checks(_):
            self.head_checks = [self.actions_job(126, 456, 4, "failure"),
                                {**independent, "conclusion": "success"}]

        self.sleep.side_effect = finish_checks
        self.request_retry()
        self.sleep.assert_called_once()
        self.assert_retry_held()
        reds = self.controller.ci_failures.call_args.args[0].reds
        self.assertEqual([r.check.description for r in reds], ["Workflow attempt 4 concluded failure"])

    def test_newer_run_of_the_same_workflow_keeps_publication_draft(self):
        self.run_snapshots = iter([(3, "completed", "success")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success")]])

        def start_newer_run(*args, **kwargs):
            self.workflow_runs.append(self.actions_run(457, 7, 1, "completed", "failure"))

        self.command.side_effect = start_newer_run
        self.request_retry()
        self.assert_retry_held()
        reds = self.controller.ci_failures.call_args.args[0].reds
        self.assertEqual([r.check.source for r in reds], ["workflow:457"])

    def assert_newer_check_from_separate_workflow_holds_publication(self, status, conclusion, state):
        separate = self.actions_run(458, 7, 2, status, conclusion, workflow_id=100)
        self.run_snapshots = iter([(3, "completed", "success")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success")]])

        def rerun_requested(*args, **kwargs):
            self.workflow_runs.append(separate)
            self.head_checks.append({**self.actions_job(127, 458, 2, conclusion), "status": status})

        self.command.side_effect = rerun_requested
        with mock.patch.object(ci, "WAIT_SECONDS", 0):
            self.request_retry()
        self.assert_retry_held()
        watched, = self.controller.ci_failures.call_args.args[0].seen
        self.assertEqual([check.job for check in watched.in_state(state)], [127])
        self.assertEqual([check.job for check in watched.in_state("pass")], [125])

    def test_newer_red_check_from_separate_same_name_workflow_keeps_publication_draft(self):
        self.assert_newer_check_from_separate_workflow_holds_publication("completed", "failure", "fail")

    def test_newer_pending_check_from_separate_same_name_workflow_keeps_publication_draft(self):
        self.assert_newer_check_from_separate_workflow_holds_publication("in_progress", None, "pending")

    def assert_retry_held(self):
        self.assertTrue(self.draft)
        self.assertNotIn(mock.call(self.checkout.pr, False), self.set_draft.call_args_list)
        self.merge.assert_not_called()
        self.assertIsNone(self.controller.ending)
        self.assertTrue(self.controller.data.get("ci_failed"))

    def test_newer_same_name_failure_on_another_pr_never_spends_this_prs_retry(self):
        self.other_pr_check["conclusion"] = "failure"
        self.head_checks.append(self.other_pr_check)
        self.record_ci_failure()
        self.assertEqual(self.controller.data["ci_retry"]["targets"][self.checkout.github]["jobs"], [200])
        self.workflow_runs = [self.other_pr_run]
        refusal = self.controller.dispatch(self.action)
        self.assertEqual(str(refusal), "the failed CI has no GitHub Actions jobs to rerun")
        self.command.assert_not_called()
        self.assertFalse(self.controller.data["ci_retried_heads"])

        self.workflow_runs.append(self.original_run)
        self.run_snapshots = iter([(3, "completed", "success")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success")]])
        self.merge.side_effect = lambda *args, **kwargs: setattr(self, "draft", False)
        self.request_retry({**self.action, "id": "d0002-0"})
        self.github.assert_any_call(["api", f"repos/{self.checkout.github}/actions/jobs/200"])
        self.merge.assert_called_once()

    def test_failed_rerun_replaces_a_stale_skipped_dependent_check(self):
        self.head_checks.append(self.actions_job(124, 456, 2, "skipped", name="dependent"))
        self.run_snapshots = iter([(3, "completed", "failure")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success"),
                                    self.actions_job(126, 456, 3, "failure", name="dependent")]])
        self.request_retry()
        reds = self.controller.ci_failures.call_args.args[0].reds
        self.assertEqual([(r.check.name, r.check.job) for r in reds], [("dependent", 126)])
        self.assert_second_red()

    def test_failed_workflow_conclusion_blocks_publication_when_the_rerun_job_passed(self):
        self.run_snapshots = iter([(4, "completed", "success")])
        self.exact_attempt = self.actions_run(456, 7, 3, "completed", "failure")
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "success")]])
        self.request_retry()
        reds = self.controller.ci_failures.call_args.args[0].reds
        self.assertEqual([r.check.description for r in reds], ["Workflow attempt 3 concluded failure"])
        self.assert_second_red()

    def test_confirmed_second_red_is_kept_when_an_independent_check_times_out(self):
        pending = self.actions_job(200, 789, 1, None, name="independent")
        self.head_checks.append({**pending, "status": "in_progress"})
        self.run_snapshots = iter([(3, "completed", "failure")])
        self.job_snapshots = iter([[self.actions_job(125, 456, 3, "failure")]])
        with mock.patch.object(ci, "WAIT_SECONDS", 0):
            self.request_retry()
        watched, = self.controller.ci_failures.call_args.args[0].seen
        self.assertFalse(watched.read)
        self.assertEqual([check.name for check in watched.in_state("pending")], ["independent"])
        self.assertEqual(self.controller.data["ci_retry"]["targets"][self.checkout.github]["jobs"], [125])
        self.assert_second_red()

    def test_publication_retry_requires_current_verification(self):
        self.controller.data["evidence"]["verify"]["tree"] = "stale"
        refusal = self.controller.dispatch(self.action)
        self.assertEqual(str(refusal), "publication needs current evidence: verify")
        self.command.assert_not_called()


class RedPublicationLifecycleTests(PublicationFixture):
    def test_noop_refuses_red_ci_with_an_empty_restaged_diff(self):
        self.record_pushed_head()
        controller = self.controller
        repo = config.Repo("fixture", self.root, parts=[config.Part("", self.checkout.github)])
        self.publication_job(repo, "arete/7-edit-fixture", verified=verify.Verified("pass"), stamp=False)
        controller.data["ci_failed"] = True
        self.assertTrue(self.build.restage().empty)

        action = {**self.action, "op": "run.finish", "request": {"reason": "noop"}}
        with mock.patch.object(flows, "finish", return_value=("pr", "No change needed.", self.checkout.pr)) as finish:
            with self.assertRaisesRegex(ValueError, r"retry CI through publication\.request, repair, or finish failed"):
                controller.perform(action)

        finish.assert_not_called()
        self.assertIsNone(controller.ending)

    def test_publication_retries_a_reported_red_after_a_polling_error(self):
        controller, c = self.controller, self.checkout
        self.file.write_text("implementation\n")
        self.enterContext(mock.patch.object(controller, "fingerprint", return_value="fixture-tree"))
        repo = config.Repo("fixture", self.root, parts=[config.Part("", c.github)])
        self.publication_job(repo, "arete/7-edit-fixture", verified=verify.Verified("pass"))
        controller.merge_lease = None
        controller.stamp("verify", {"verdict": "pass"})
        original_run = controller.flow.run

        def run_step(name, args, kw):
            if name == "pr":
                self.record_pushed_head()
                return factory.Outcome("pass", data={
                    **controller.data["publication"], "prs": {c.github: c.pr}, "opened": [c.github],
                    "pushed": [c.github], "changed": [c.github], "ci_stopped": [], "draft_why": "",
                    "report_body": "", "summary": "Edit fixture", "committed_lines": 2,
                })
            return original_run(name, args, kw)

        red = gh.Check("arete-checks", "fail", job=123, source=f"app:{gh.GITHUB_ACTIONS_APP_ID}")
        base = replace(red, state="pass", job=122, updated="2026-11-21")
        green = replace(red, state="pass", job=125)
        rerun = gh.WorkflowRerun(456, 2, {red.name: red.job})

        def watch(target, log, *, reruns=()):
            if reruns:
                self.assertEqual(reruns, [rerun])
                launch.assert_called_once_with(c.github, rerun.run)
                return ci.Watched(target, {green.key: green})
            return ci.Watched(target, {red.key: red}, read=False)

        controller.flow.run = run_step
        self.enterContext(mock.patch.object(ci, "watch", side_effect=watch))
        self.enterContext(mock.patch.object(gh, "branch_sha", return_value="base"))
        self.enterContext(mock.patch.object(gh, "commit_checks", return_value={base.key: base}))
        self.enterContext(mock.patch.object(gh, "pr_merge_details", side_effect=lambda _: {
            "state": "OPEN", "headRefOid": c.lease, "baseRefName": c.base,
        }))
        self.enterContext(mock.patch.object(gh, "pr_draft_and_head", side_effect=lambda _: (True, c.lease)))
        draft = self.enterContext(mock.patch.object(gh, "pr_set_draft"))
        find_jobs = self.enterContext(mock.patch.object(gh, "failed_job_reruns", return_value=[rerun]))
        launch = self.enterContext(mock.patch.object(gh, "rerun_failed_jobs"))
        report = self.enterContext(mock.patch.object(pipeline, "publish_ci"))
        self.enterContext(mock.patch.object(controller, "ci_failures", return_value="failed check logs"))
        self.enterContext(mock.patch.object(pipeline, "release_workspace"))
        self.enterContext(mock.patch.object(pipeline, "record_prs", side_effect=lambda *_: c.pr))

        controller.dispatch(self.action)
        first_result = self.transition.call_args.args[-1]
        self.assertIn("CI needs an explicit worker diagnosis or repair", first_result["error"])
        self.assertIn("passed on `main`", report.call_args.args[-1])
        self.assertIsNone(controller.ending)
        draft.assert_called_once_with(c.pr, True)
        launch.assert_not_called()
        head = c.lease

        retry = {**self.action, "id": "d0002-0"}
        self.assertIsNone(controller.dispatch(retry))
        self.assertEqual(self.transition.call_args.args[:3], (controller.lease, retry["id"], "succeeded"))
        find_jobs.assert_called_once_with(c.github, c.pr, head, [red.job])
        self.assertEqual(draft.call_args_list, [mock.call(c.pr, True), mock.call(c.pr, False)])
        self.assertEqual((controller.ending[0], controller.ending[2]), ("pr", c.pr))
        self.assertNotIn("ci_failed", controller.data)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)


class SingleWorkerPublicationTests(PublicationFixture):
    def test_single_worker_repair_fails_required_gates_when_publication_is_refused(self):
        branch = "arete/7-edit-fixture"
        self.git("switch", "-qc", branch)
        self.record_pushed_head()
        c, controller, b = self.checkout, self.controller, self.build
        c.resumes = True
        repo = config.Repo("fixture", self.root, parts=[config.Part("", c.github)])
        self.publication_job(repo, branch, verified=verify.Verified("pass"))
        controller.run.repairs = "saved-review"
        b.from_base, b.layer.verify = True, config.Verify()
        controller.policy = config.Orchestration()
        catalog = controller.catalog
        catalog.context["task"] = "Check the saved review findings"
        catalog.settings = {f"model:{role}:0": SimpleNamespace(step=role, effort="low")
                            for role in ("build", "verify", "review")}
        catalog.route.side_effect = lambda key, *_: catalog.settings[key]
        catalog.validate.side_effect = lambda *args: orchestration.Catalog.validate(catalog, *args)

        state = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.enterContext(mock.patch.multiple(paths, STATE_DIR=state, DB_FILE=state / "arete.db",
                                             RUNS_DIR=state / "runs", LOCKS_DIR=state / "locks",
                                             WORKTREES_DIR=state / "worktrees", LOG_DIR=state / "logs"))
        db.migrate()
        self.transition.return_value = mock.DEFAULT
        del controller.persist

        unchanged = f"nothing changed since the pushed head `{c.lease}`"
        for verified, review, error in (
            ("pass", "approve", unchanged),
            ("fail", "approve", "behavioral verification failed; repair or finish failed"),
            ("pass", "request_changes", unchanged),
        ):
            with self.subTest(verified=verified, review=review):
                controller.run.id = f"fixture-{verified}-{review}"
                controller.lease = ledger.acquire(controller.run.id, "", controller.data)
                with (mock.patch.object(orchestration, "Controller", return_value=controller),
                      mock.patch.object(controller, "prepare", return_value=None),
                      mock.patch.object(controller, "worker_start", return_value={"text": "Head already fixes the findings"}),
                      mock.patch.object(pipeline, "check_in_use", return_value=verify.Verified(verified)),
                      mock.patch.object(pipeline, "_review", return_value=Review.stored({"verdict": review})),
                      mock.patch.object(pipeline.record, "note"),
                      mock.patch.object(pipeline, "release_workspace"),
                      mock.patch.object(pipeline, "commit_and_push", side_effect=AssertionError("unexpected push")) as push,
                      mock.patch.object(gh, "pr_set_draft", side_effect=AssertionError("unexpected draft change")) as draft):
                    ending = single_worker.single_worker(controller.flow)
                publication = ledger.actions(controller.lease)[-1]
                self.assertEqual((publication["op"], publication["state"], publication["result"]["error"]),
                                 ("publication.request", "failed", error))
                self.assertEqual(ending, ("failed", "single-worker execution did not pass its required gates", ""))
                self.assertEqual(self.git("rev-parse", "HEAD"), c.lease)
                push.assert_not_called()
                draft.assert_not_called()


class ClosedPRPublicationTests(PublicationFixture):
    def test_closed_pr_rerun_publishes_with_the_remote_only_lease(self):
        other = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.git("clone", "-q", "--shared", str(self.root), str(other))
        (other / "file.txt").write_text("old closed PR\n")
        util.git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                  "-c", "commit.gpgSign=false", "commit", "-qam", "closed PR"], other)
        lease = util.git(["rev-parse", "HEAD"], other).strip()
        self.assertNotEqual(util.sh(["git", "cat-file", "-e", f"{lease}^{{commit}}"], cwd=self.root).returncode, 0)
        self.git("config", "commit.gpgSign", "false")
        repo = config.Repo("fixture", self.root, parts=[config.Part("", self.checkout.github)])
        branch, controller, b = "arete/7-edit-fixture", self.controller, self.build
        remote = lease
        pushes = []

        def git(args, cwd, **kw):
            nonlocal remote
            if args[0] != "push":
                return util.git(args, cwd, **kw)
            pushes.append(args)
            self.assertEqual(remote, lease)
            self.assertEqual(args, ["push", "-q", f"--force-with-lease={branch}:{lease}", "-u", "origin", branch])
            remote = util.git(["rev-parse", "HEAD"], cwd).strip()
            return ""

        pr = "https://github.com/fixture/repo/pull/8"
        with (mock.patch.object(paths, "WORKTREES_DIR", other / "worktrees"),
              mock.patch.object(workspace, "fetch") as fetch,
              mock.patch.object(workspace, "remote_head", side_effect=lambda src, branch: remote),
              mock.patch.object(gh, "open_pr_url", return_value=""),
              mock.patch.object(gh, "closed_pr_heads", return_value={lease}),
              mock.patch.object(workspace.record, "timed", return_value=nullcontext({})),
              mock.patch.object(pipeline, "git", side_effect=git),
              mock.patch.object(pipeline, "release_workspace"),
              mock.patch.object(pipeline, "supersede_older_runs"),
              mock.patch.object(pipeline.record, "note"),
              mock.patch.object(pipeline.record, "metered_spent", return_value=0),
              mock.patch.object(ci, "watch", side_effect=lambda target, log: ci.Watched(target, {})) as watch,
              mock.patch.object(gh, "pr_create", return_value=pr) as create,
              mock.patch.object(gh, "pr_body", return_value="Fixture PR"),
              mock.patch.object(gh, "pr_edit_body")):
            ws, checkouts = workspace.make_workspace(controller.run.id, repo, "main", branch, "7", [])
            self.checkout = c = checkouts[0]
            b.checkouts, b.ws = checkouts, ws
            self.assertEqual(c.lease, lease)
            self.assertFalse(c.resumes)
            self.assertEqual(util.git(["rev-parse", "HEAD"], ws).strip(), self.git("rev-parse", "origin/main"))
            (ws / "file.txt").write_text("new implementation\n")
            job = self.publication_job(repo, branch, verified=verify.Verified("pass"))
            controller.stamp("verify", {"verdict": "pass"})
            controller.dispatch(self.action)

        state = self.transition.call_args.args
        self.assertEqual(state[:3], (controller.lease, self.action["id"], "succeeded"), state[-1])
        self.assertEqual(controller.ending[0], "pr")
        self.assertEqual(controller.ending[2], pr)
        self.assertEqual(util.git(["show", f"{remote}:file.txt"], ws), "new implementation\n")
        self.assertEqual(controller.data["publication"]["heads"], {c.github: remote})
        self.assertEqual(c.lease, remote)
        self.assertEqual(c.publication_baseline, remote)
        self.assertTrue(job.committed)
        self.assertEqual(len(pushes), 1)
        watch.assert_called_once()
        self.assertEqual(watch.call_args.args[0].sha, remote)
        create.assert_called_once()
        fetch.assert_called_once_with(self.root, "main")


class PublicationDiffTests(PublicationFixture):
    def test_unchanged_pushed_tree_refuses_publication_and_keeps_the_run_open(self):
        self.record_pushed_head()
        self.controller.data["ci_failed"] = True
        self.controller.job.verified.checks = [{"passed": True}]
        for from_base, release in ((False, ""), (True, ""), (True, "live-release")):
            with self.subTest(from_base=from_base, release=release):
                self.build.from_base = from_base
                self.controller.run.environment_release = release
                self.controller.dispatch(self.action)
                self.transition.assert_called_with(
                    self.controller.lease, self.action["id"], "failed",
                    {"error": f"nothing changed since the pushed head `{self.checkout.lease}`"})
                self.assertIsNone(self.controller.ending)
                self.assertTrue(self.controller.data["ci_failed"])
                self.assertNotIn("publication_intent", self.controller.data)
        self.controller.atomic.assert_not_called()

    def test_verified_environment_resume_readies_the_existing_draft_without_a_push(self):
        self.record_pushed_head()
        c, controller, b = self.checkout, self.controller, self.build
        c.resumes, b.from_base = True, True
        controller.run.environment_release = "live-release"
        b.layer.verify = config.Verify(publish_evidence=False)
        repo = config.Repo("fixture", self.root, parts=[config.Part("", c.github)])
        self.publication_job(repo, "arete/7-edit-fixture", verified=verify.Verified("blocked"),
                             carried=True, continues="held-run")
        controller.catalog.route.return_value = SimpleNamespace(step="verify")
        controller.dispatch(self.action)
        self.transition.assert_called_with(controller.lease, self.action["id"], "failed",
                                           {"error": "publication needs current evidence: verify"})

        verified = verify.Verified("pass", "Implementation works", checks=[
            {"what": "Use the fixture", "how": "Open the fixture", "saw": "The fixture opened", "passed": True}])
        rerun = {"id": "d0002-0", "op": "verification.run", "state": "accepted",
                 "request": {"route_id": "fixture", "effort": "", "goal_refs": []}}
        publish = {**self.action, "id": "d0003-0"}
        head = c.lease
        with (mock.patch.object(pipeline, "check_in_use", return_value=verified),
              mock.patch.object(flows.run_store, "run_get", return_value={"started": "2026-01-01"}),
              mock.patch.object(pipeline, "pending_feedback", return_value=[]),
              mock.patch.object(workspace, "remote_head", return_value=head),
              mock.patch.object(pipeline, "commit_and_push") as push,
              mock.patch.object(pipeline, "release_workspace"),
              mock.patch.object(pipeline, "supersede_older_runs"),
              mock.patch.object(pipeline.record, "note"),
              mock.patch.object(pipeline.record, "metered_spent", return_value=0),
              mock.patch.object(ci, "watch", side_effect=lambda target, log: ci.Watched(target, {})) as watch,
              mock.patch.object(gh, "me", return_value="fixture"),
              mock.patch.object(gh, "pr_discussion", return_value=gh.Discussion([], [], [])),
              mock.patch.object(gh, "pr_body", return_value="Saved draft"),
              mock.patch.object(gh, "pr_edit_body"),
              mock.patch.object(gh, "pr_comment") as comment,
              mock.patch.object(gh, "pr_draft_and_head", return_value=(True, head)),
              mock.patch.object(gh, "pr_set_draft") as draft):
            controller.dispatch(rerun)
            controller.dispatch(publish)
        action_state = self.transition.call_args.args
        self.assertEqual(action_state[:3], (controller.lease, publish["id"], "succeeded"), action_state[-1])
        self.assertEqual(controller.ending[0], "pr")
        self.assertEqual(controller.ending[2], c.pr)
        self.assertEqual(controller.data["publication"]["heads"], {c.github: head})
        self.assertEqual(watch.call_args.args[0].sha, head)
        draft.assert_called_once_with(c.pr, False)
        comment.assert_called_once()
        self.assertIn("Verified and reviewed #7 again; nothing new was pushed.", comment.call_args.args[1])
        push.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_edits_that_undo_the_implementation_still_fail_the_run(self):
        for pushed in (False, True):
            with self.subTest(pushed=pushed):
                if pushed:
                    self.record_pushed_head()
                else:
                    self.file.write_text("implementation\n")
                    self.git("add", "--", "file.txt")
                self.file.write_text("base\n")
                with self.assertRaisesRegex(workspace.RunFailed, "the final orchestrated tree undid every change"):
                    self.controller.dispatch(self.action)
        self.controller.atomic.assert_not_called()


class PendingBaseMergePublicationTests(PublicationFixture):
    def resume_with_pending_base_merge(self):
        c, controller, b = self.checkout, self.controller, self.build
        branch = "arete/7-edit-fixture"
        fork = self.git("rev-parse", "origin/main")
        self.git("switch", "-qc", branch)
        self.record_pushed_head()
        c.resumes = True

        self.git("switch", "-qc", "moved-base", fork)
        (self.root / "upstream.txt").write_text("upstream change\n")
        self.git("add", "--", "upstream.txt")
        self.commit("moved base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("switch", "-q", branch)
        self.git("config", "commit.gpgSign", "false")
        with mock.patch.object(pipeline.record, "timed", return_value=nullcontext({})):
            pipeline.settle_starts(controller.run, [c], b.layer, b.ticket, branch)
        # A later fetch can update the tracking ref while the merge is still unpushed.
        self.git("update-ref", "refs/remotes/origin/main", fork)
        self.assertEqual(pipeline.merge_moved_bases(controller.run, [c], b.layer, b.ticket), [])
        self.assertTrue(pipeline.pending_merge(c))
        self.assertEqual(c.base_commit, self.git("rev-parse", "HEAD^2"))
        return branch

    def test_pending_base_merge_fails_before_push_when_the_implementation_is_undone(self):
        branch = self.resume_with_pending_base_merge()
        c, controller = self.checkout, self.controller
        self.file.write_text("base\n")
        repo = config.Repo("fixture", self.root, parts=[config.Part("", c.github)])
        self.publication_job(repo, branch, verified=verify.Verified("pass"))
        controller.stamp("verify", {"verdict": "pass"})

        with mock.patch.object(pipeline, "commit_and_push", side_effect=AssertionError("unexpected push")) as push:
            with self.assertRaisesRegex(workspace.RunFailed, "the final orchestrated tree undid every change"):
                controller.dispatch(self.action)
        push.assert_not_called()
        self.assertEqual(self.git("diff", "--cached", "--name-only", c.base_commit), "")
        self.assertNotIn("publication_intent", controller.data)

    def test_pending_base_merge_reaches_push_with_the_implementation_intact(self):
        branch = self.resume_with_pending_base_merge()
        c, controller = self.checkout, self.controller
        repo = config.Repo("fixture", self.root, parts=[config.Part("", c.github)])
        self.publication_job(repo, branch, verified=verify.Verified("pass"))
        controller.stamp("verify", {"verdict": "pass"})
        push = mock.Mock(side_effect=RuntimeError("stubbed push"))

        def git(args, cwd, **kw):
            return push(args, cwd) if args[0] == "push" else util.git(args, cwd, **kw)

        with (mock.patch.object(workspace, "remote_head", return_value=c.lease),
              mock.patch.object(pipeline, "git", side_effect=git)):
            with self.assertRaisesRegex(workspace.RunFailed, "the push to fixture/repo failed"):
                controller.dispatch(self.action)
        push.assert_called_once_with(["push", "-q", f"--force-with-lease={branch}:{c.lease}", "-u", "origin", branch],
                                     self.root)
        self.assertEqual(self.git("show", "HEAD:file.txt"), "implementation")


class ResolverScratchFixture(unittest.TestCase):
    MARKER = "arete-verification-scratch-v1\n"
    LOG = ".arete-sandbox/logs/db.log"
    TRACKED = ".arete-sandbox/logs/tracked.txt"
    merge_fixture = None

    def setUp(self):
        self.enterContext(mock.patch.object(util, "repo_lock", return_value=nullcontext()))

    @classmethod
    def create_merge(cls, root, tracked_changed_by="", tracked_added_by_base=False):
        def git(*args, check=True):
            subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True)

        git("init", "-q", "-b", "main", "--template=")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.test")
        git("config", "commit.gpgsign", "false")
        (root / ".gitignore").write_text(".arete-sandbox/\nignored.local\nproject/.arete-owned\nproject/private.local\n")
        (root / "project").mkdir()
        (root / "project/tracked.txt").write_text("untouched\n")
        (root / "conflict.txt").write_text("original\n")
        tracked = root / cls.TRACKED
        if tracked_changed_by or tracked_added_by_base:
            tracked.parent.mkdir(parents=True)
            (root / ".arete-sandbox/.arete-owned").write_text(cls.MARKER)
        if tracked_changed_by:
            tracked.write_text("original tracked content\n")
            git("add", "-f", "--", cls.TRACKED)
        git("add", ".")
        git("commit", "-qm", "original")
        git("checkout", "-qb", "branch")
        (root / "conflict.txt").write_text("branch\n")
        (root / "supplemental.txt").write_text("branch addition\n")
        if tracked_changed_by in ("branch", "both"):
            tracked.write_text("branch content\n")
        git("add", ".")
        git("commit", "-qm", "branch change")
        git("checkout", "-q", "main")
        (root / "conflict.txt").write_text("base\n")
        if tracked_changed_by in ("base", "both") or tracked_added_by_base:
            tracked.write_text("base content\n")
        if tracked_changed_by == "removed by base":
            tracked.unlink()
        if tracked_added_by_base:
            (root / "incoming.txt").write_text("incoming addition\n")
            git("add", "-f", "--", cls.TRACKED)
            git("add", "--", "incoming.txt")
        git("commit", "-qam", "base change")
        git("checkout", "-q", "branch")
        git("merge", "--no-commit", "main", check=False)

    def merge(self, tracked_changed_by="", tracked_added_by_base=False):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        if self.merge_fixture is not None:
            shutil.copytree(self.merge_fixture, self.root, dirs_exist_ok=True)
        else:
            self.create_merge(self.root, tracked_changed_by, tracked_added_by_base)
        self.conflicts = [self.TRACKED, "conflict.txt"] if tracked_changed_by == "both" else ["conflict.txt"]
        self.assertEqual(pipeline.unmerged_paths(self.root), self.conflicts)
        self.checkout = workspace.Checkout("", "fixture/repo", self.root, self.root, "main",
                                           base_commit=self.git("rev-parse", "main"))
        self.layer = config.RepoLayer(self.root)
        self.run = mock.Mock(repo=SimpleNamespace(name="repo"), sessions={}, owner_note="",
                             settings=mock.Mock(return_value=SimpleNamespace(tries=1)))

    def git(self, *args, check=True):
        return util.git(list(args), self.root, check=check).strip()

    def resolve(self, edit):
        def agent(*args, **kwargs):
            (self.root / "conflict.txt").write_text("branch and base\n")
            edit()
            return Result(ok=True, structured={"resolved": True})

        self.run.agent.side_effect = agent
        pipeline.resolve_conflicts(self.run, self.checkout, self.conflicts, self.layer,
                                   tracker.Ticket("719", "#719", "Scratch exemption", "", ""))

    def write_log(self):
        log = self.root / self.LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as out:
            out.write("verification ran\n")

    def assert_outside_conflict(self, edit, paths):
        with self.assertRaises(workspace.RunFailed) as failed:
            self.resolve(edit)
        self.assertEqual(str(failed.exception), f"the resolver edited files outside the conflict in fixture/repo: {paths}")


class SharedResolverScratchFixture(ResolverScratchFixture):
    @classmethod
    def setUpClass(cls):
        cls.merge_fixture = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, cls.merge_fixture, ignore_errors=True)
        cls.create_merge(cls.merge_fixture)


class ResolverScratchTests(SharedResolverScratchFixture):
    def test_wrapper_scratch_stays_out_of_merge(self):
        self.merge()
        scratch = self.root / ".arete-sandbox"
        scratch.mkdir()
        (scratch / ".gitignore").write_text("*\n")
        (scratch / ".arete-owned").write_text(self.MARKER)
        (self.root / "project/.arete-owned").write_text(self.MARKER.removesuffix("\n"))

        def edit():
            self.write_log()
            (self.root / "project/untracked.txt").write_text("untracked scratch\n")
            (self.root / "supplemental.txt").write_text("updated branch addition\n")

        with mock.patch.object(pipeline, "git", wraps=pipeline.git) as git:
            self.resolve(edit)
        staged = [call.args[0] for call in git.call_args_list if call.args[0][0] == "add"]
        self.assertEqual(staged, [["add", "-u", "--", "conflict.txt", "supplemental.txt"]])
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        self.assertEqual(self.git("show", "HEAD:conflict.txt"), "branch and base")
        self.assertEqual(self.git("show", "HEAD:supplemental.txt"), "updated branch addition")
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD", "--", ".arete-sandbox"), "")
        self.assertEqual((self.root / self.LOG).read_text(), "verification ran\n")
        self.assertEqual(workspace.untracked_files(self.root), {"project/untracked.txt"})

    def test_invalid_scratch_markers_do_not_exempt_ignored_log_changes(self):
        self.merge()
        scratch = self.root / ".arete-sandbox"
        marker = scratch / ".arete-owned"
        (self.root / ".arete-owned").write_text(self.MARKER)
        for kind in ("missing", "symlink", "wrong text", "blank line"):
            with self.subTest(marker=kind):
                marker.unlink(missing_ok=True)
                self.write_log()
                if kind == "symlink":
                    marker.symlink_to("../.arete-owned")
                elif kind == "wrong text":
                    marker.write_text("someone else's scratch\n")
                elif kind == "blank line":
                    marker.write_text(self.MARKER + "\n")
                self.assert_outside_conflict(self.write_log, self.LOG)

    def test_scratch_exemption_preserves_other_path_checks(self):
        self.merge()
        folder = self.root / "project"
        (folder / ".arete-owned").write_text(self.MARKER.removesuffix("\n"))
        self.write_log()
        (self.root / ".arete-sandbox/.arete-owned").write_text(self.MARKER)
        (self.root / "ignored.local").write_text("ignored original\n")

        def edit():
            self.write_log()
            (self.root / "ignored.local").write_text("ignored edit\n")
            (folder / "private.local").write_text("scratch edit\n")
            (folder / "tracked.txt").write_text("tracked edit\n")
            (folder / "untracked.txt").write_text("untracked addition\n")
            (self.root / "outside.txt").write_text("outside addition\n")

        self.assert_outside_conflict(edit, "ignored.local, outside.txt, project/tracked.txt")

    def test_resolving_without_the_wrapper_leaves_scratch_absent(self):
        self.merge()
        scratch = self.root / ".arete-sandbox"
        self.assertFalse(scratch.exists())
        self.resolve(lambda: None)
        self.assertFalse(scratch.exists())

    def test_first_use_keeps_existing_scratch_entry(self):
        self.merge()
        self.layer.allow_bash.append("sandbox *")
        scratch = self.root / ".arete-sandbox"
        scratch.symlink_to("missing", target_is_directory=True)
        self.resolve(lambda: self.assertEqual(scratch.readlink(), Path("missing")))
        self.assertFalse((self.root / "missing").exists())

    def test_resolver_cannot_exempt_a_new_scratch_directory(self):
        self.merge()
        scratch = self.root / ".arete-sandbox"
        self.assertFalse(scratch.exists())
        for forged_marker in (False, True):
            with self.subTest(forged_marker=forged_marker):
                def edit():
                    scratch.mkdir(exist_ok=True)
                    (scratch / "unrelated.txt").write_text(f"outside edit {forged_marker}\n")
                    if forged_marker:
                        (scratch / ".arete-owned").write_text(self.MARKER)

                expected = ".arete-sandbox/unrelated.txt"
                if forged_marker:
                    expected = ".arete-sandbox/.arete-owned, " + expected
                self.assert_outside_conflict(edit, expected)

    def test_unreadable_ignored_scratch_requires_a_still_valid_marker(self):
        self.merge()
        self.write_log()
        marker = self.root / ".arete-sandbox/.arete-owned"
        log = self.root / self.LOG
        fingerprint = workspace.fingerprint

        def fingerprint_file(path, salt):
            if path == log:
                raise PermissionError(path)
            return fingerprint(path, salt)

        for invalidate in (True, False):
            with self.subTest(invalidate=invalidate):
                marker.write_text(self.MARKER)

                def edit():
                    self.write_log()
                    if invalidate:
                        marker.unlink()

                with mock.patch.object(workspace, "fingerprint", side_effect=fingerprint_file):
                    if invalidate:
                        self.assert_outside_conflict(edit, f".arete-sandbox/.arete-owned, {self.LOG}")
                    else:
                        self.resolve(edit)
                        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD", "--", self.LOG), "")

    def test_invalidated_scratch_markers_do_not_exempt_ignored_log_changes(self):
        self.merge()
        self.write_log()
        marker = self.root / ".arete-sandbox/.arete-owned"
        for kind in ("missing", "symlink", "wrong text", "blank line", "replacement"):
            with self.subTest(marker=kind):
                marker.unlink(missing_ok=True)
                marker.write_text(self.MARKER)
                (marker.parent / "marker-source").write_text(self.MARKER)

                def edit():
                    self.write_log()
                    if kind in ("missing", "symlink"):
                        marker.unlink()
                        if kind == "symlink":
                            marker.symlink_to("marker-source")
                    elif kind == "replacement":
                        marker.with_name("marker-source").replace(marker)
                    else:
                        marker.write_text("wrong text\n" if kind == "wrong text" else self.MARKER + "\n")

                expected = f".arete-sandbox/.arete-owned, {self.LOG}"
                if kind == "replacement":
                    expected += ", .arete-sandbox/marker-source"
                self.assert_outside_conflict(edit, expected)

    def test_resolver_cannot_make_an_ignored_path_scratch(self):
        self.merge()
        marker = self.root / "project/.arete-owned"
        ignored = self.root / "project/private.local"
        for initial in (None, "wrong text\n"):
            with self.subTest(initial=initial):
                if initial is not None:
                    marker.write_text(initial)
                ignored.write_text("original\n")

                def edit():
                    marker.write_text(self.MARKER)
                    ignored.write_text("ignored edit\n")

                self.assert_outside_conflict(edit, "project/.arete-owned, project/private.local")


class TrackedResolverScratchTests(ResolverScratchFixture):
    def test_incoming_tracked_addition_is_preserved(self):
        self.merge(tracked_added_by_base=True)
        staged = self.git("diff", "--cached", "--name-only", "--diff-filter=A").splitlines()
        self.assertEqual(staged, [self.TRACKED, "incoming.txt"])
        self.resolve(self.write_log)
        self.assertEqual(self.git("show", f"HEAD:{self.TRACKED}"), "base content")
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD", "--", self.LOG), "")
        self.assertEqual(self.git("show", "HEAD:incoming.txt"), "incoming addition")
        self.assertEqual(workspace.uncommitted(self.root), [])

    def test_unrelated_tracked_scratch_edit_fails(self):
        self.merge(tracked_changed_by="neither")
        head = self.git("rev-parse", "HEAD")
        tracked = self.root / self.TRACKED

        def edit():
            self.write_log()
            tracked.write_text("unrelated edit\n")

        self.assert_outside_conflict(edit, self.TRACKED)
        self.assertEqual(tracked.read_text(), "unrelated edit\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_tracked_supplemental_scratch_edit_reaches_the_merge_commit(self):
        for parent in ("branch", "base"):
            with self.subTest(parent=parent):
                self.merge(tracked_changed_by=parent)

                def edit():
                    self.write_log()
                    (self.root / self.TRACKED).write_text("adapted content\n")

                with mock.patch.object(pipeline, "git", wraps=pipeline.git) as git:
                    self.resolve(edit)
                staged = [call.args[0] for call in git.call_args_list if call.args[0][0] == "add"]
                self.assertEqual(staged, [["add", "-u", "--", "conflict.txt", self.TRACKED]])
                self.assertEqual(self.git("show", f"HEAD:{self.TRACKED}"), "adapted content")
                self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD", "--", self.LOG), "")
                self.assertEqual((self.root / self.LOG).read_text(), "verification ran\n")
                self.assertEqual(workspace.uncommitted(self.root), [])

    def test_parent_tracked_path_under_logs_can_be_restored(self):
        self.merge(tracked_changed_by="removed by base")
        tracked = self.root / self.TRACKED
        self.assertFalse(tracked.exists())

        def edit():
            self.write_log()
            tracked.write_text("restored parent content\n")

        self.resolve(edit)
        self.assertEqual(self.git("show", f"HEAD:{self.TRACKED}"), "restored parent content")
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD", "--", self.LOG), "")

    def test_tracked_scratch_conflict_is_resolved(self):
        self.merge(tracked_changed_by="both")

        def edit():
            self.write_log()
            (self.root / self.TRACKED).write_text("resolved content\n")

        self.resolve(edit)
        self.assertEqual(self.git("show", f"HEAD:{self.TRACKED}"), "resolved content")
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD", "--", self.LOG), "")
        self.assertEqual(pipeline.unmerged_paths(self.root), [])

    def test_tracked_scratch_conflict_respects_repository_deny(self):
        self.merge(tracked_changed_by="both")
        self.layer.deny.append(self.TRACKED)
        with self.assertRaisesRegex(workspace.RunFailed, r"conflicts in protected paths"):
            self.resolve(self.write_log)
        self.run.agent.assert_not_called()


class FirstUseResolverScratchTests(SharedResolverScratchFixture):
    def test_first_use_initializes_scratch_for_trusted_wrapper(self):
        self.merge()
        scratch = self.root / ".arete-sandbox"
        self.assertFalse(scratch.exists())
        self.layer.allow_bash.append("sandbox *")

        def edit():
            self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o700)
            self.assertEqual((scratch / ".arete-owned").read_text(), self.MARKER)
            self.assertEqual((scratch / ".gitignore").read_text(), "*\n")
            self.write_log()

        self.resolve(edit)
        self.assertEqual(self.git("show", "HEAD:conflict.txt"), "branch and base")
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "HEAD", "--", ".arete-sandbox"), "")

    def test_first_use_without_trusted_wrapper_rejects_scratch_write(self):
        self.merge()
        self.layer.allow_bash.extend(["echo sandbox *", "./sandbox *"])
        scratch = self.root / ".arete-sandbox"
        self.assertFalse(scratch.exists())

        def edit():
            self.assertFalse(scratch.exists())
            self.write_log()

        self.assert_outside_conflict(edit, self.LOG)


class CatchUpRepairFixture(PublicationFixture):
    def prepare_publication(self, gate, *, spent=False, move_base_again=False, conflict=False,
                            implementation="implementation\n", resolution="resolved implementation\n", edit_base=None):
        c, b, controller = self.checkout, self.build, self.controller
        branch = "arete/7-edit-fixture"
        self.git("branch", branch)
        incoming = self.root / "incoming.txt"
        incoming.write_text("new base\n" * 100)
        if conflict:
            self.file.write_text(self.file.read_text().replace("base\n", "base implementation\n", 1))
        extra = edit_base() if edit_base else ()
        self.commit("base moved", "incoming.txt", "file.txt", *extra)
        self.base = self.git("rev-parse", "HEAD")
        next_base = ""
        if move_base_again:
            incoming.write_text("newer base\n" * 100)
            self.commit("base moved again", "incoming.txt")
            next_base = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", branch)
        self.file.write_text(implementation)
        repo = config.Repo("fixture", self.root, github=c.github, parts=[config.Part("", c.github)])
        b.layer.trust_level, b.layer.verify, b.layer.checks = "automerge", config.Verify(), {"local": "check"}
        b.layer.max_diff_lines = 20
        job = self.publication_job(repo, branch, verified=verify.Verified("pass"), stamp=False)
        state = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.enterContext(mock.patch.multiple(paths, STATE_DIR=state, DB_FILE=state / "arete.db",
                                             RUNS_DIR=state / "runs", LOCKS_DIR=state / "locks",
                                             WORKTREES_DIR=state / "worktrees", LOG_DIR=state / "logs"))
        db.migrate()
        rid = pipeline.record.start("pipeline", repo=repo.name, issue="7")
        folder = paths.RUNS_DIR / rid
        folder.mkdir(parents=True)
        run = pipeline.Run(rid, repo, folder, tracker=tracker.GitHubTracker(repo), tid="7",
                           path="orchestrate", layer=b.layer, workspace=(self.root, [c]))
        controller.run = job.run = b.run = run
        self.enterContext(mock.patch.object(run, "log"))
        run.budget = runlimits.Budget(run, runlimits.Policy(max_repair_cycles=1), "")
        if spent:
            run.budget.charge("earlier_worker", "review_repair")
        self.transition.return_value = mock.DEFAULT
        del controller.persist
        controller.policy = config.Orchestration()
        controller.lease = ledger.acquire(run.id, "", controller.data)
        controller.catalog.route.side_effect = lambda key, *_: SimpleNamespace(step=key, runner="codex")
        controller.catalog.add_context.side_effect = controller.catalog.context.__setitem__
        controller.selected = mock.Mock(return_value=nullcontext())
        self.head, self.body, self.draft, self.merged, self.ticket_open = "", "", False, False, True
        self.pushes, self.ci_heads, self.labels, self.comments = [], [], set(), []
        self.message = {"checks": "local check output from the merged base",
                        "verify": "verification reproduced the bug on the merged base",
                        "review": "review found a blocker specific to the merged base",
                        "unrelated": "the approved implementation has an unrelated problem"}[gate]
        fingerprints = {}
        git_fingerprint = factory.fingerprint

        def fingerprint(folder):
            files = tuple((p.name, p.read_bytes()) for p in (self.file, incoming) if p.exists())
            if files not in fingerprints:
                fingerprints[files] = git_fingerprint(folder)
            return fingerprints[files]

        self.enterContext(mock.patch.object(factory, "fingerprint", side_effect=fingerprint))

        def holds():
            return incoming.exists() and self.file.read_text() == implementation

        def check(*_, **__):
            return (False, self.message) if gate == "checks" and holds() else (True, "local checks passed")

        def verification(*_, trees=None):
            failed = gate == "verify" and holds()
            summary = self.message if failed else "verified with the new base" if incoming.exists() else "pre-catch-up verification"
            return verify.Verified("fail" if failed else "pass", summary, checks=[
                {"what": "fixture behavior", "how": "read fixture", "saw": summary, "passed": not failed}],
                native_trees=pipeline.verification_trees(b) if trees is None else trees)

        real_review = pipeline._review

        def review(*args, catch_up=None, **kwargs):
            if catch_up is not None:
                return real_review(*args, catch_up=catch_up, **kwargs)
            return Review.judged({"summary": "approved on the new base", "problems": []}, [], [],
                                 pipeline.verification_trees(b))

        def review_answer(*_, **__):
            problems = []
            if gate in ("review", "unrelated") and holds():
                finding = {"file": "file.txt", "severity": "blocker", "category": "issue", "problem": self.message}
                if gate == "review":
                    finding["attribution"] = {"part": "base", "file": "incoming.txt", "line": 1}
                problems.append(finding)
            return Result(ok=True, structured={"summary": "Reviewed the moved base.", "problems": problems})

        self.review_agent = self.enterContext(mock.patch.object(run, "agent", side_effect=review_answer))

        if conflict:
            def resolve(*_):
                self.file.write_text(resolution)
                self.commit("Resolve fixture conflict")

            self.enterContext(mock.patch.object(pipeline, "resolve_conflicts", side_effect=resolve))

        def git(args, cwd, **kw):
            if args[0] != "push":
                return util.git(args, cwd, **kw)
            self.head = self.git("rev-parse", "HEAD")
            self.pushes.append(self.head)
            return ""

        def ci_watch(target, _log):
            if not self.ci_heads:
                self.git("update-ref", f"refs/remotes/{c.origin_base}", self.base)
            elif next_base and len(self.ci_heads) == 1:
                self.base = next_base
                self.git("update-ref", f"refs/remotes/{c.origin_base}", self.base)
            self.ci_heads.append(target.sha)
            return ci.Watched(target, {})

        def edit_body(_url, body, *, link):
            self.body = gh.outgoing(body, link)

        def create_pr(*args, link, **_):
            url = "https://github.com/fixture/repo/pull/7"
            edit_body(url, args[4] + "\n\n## Owner note\n\nKeep this.", link=link)
            self.assertIn("**approve**: pre-catch-up review", self.body)
            self.assertIn("**pass**: pre-catch-up verification", self.body)
            return url

        def merge(_repo, _number, head, _base, base_sha, **_):
            self.assertEqual(head, self.head)
            self.assertEqual(base_sha, self.git("merge-base", self.base, head))
            self.assertTrue(self.body.startswith("Closes #7\n"))
            self.merged = True
            self.ticket_open = False
            return ""

        def label(_repo, _number, add, remove):
            self.labels.difference_update(remove)
            self.labels.update(add)

        patches = {
            pipeline: dict(git=mock.Mock(side_effect=git), run_checks=mock.Mock(side_effect=check),
                           check_in_use=mock.Mock(side_effect=verification), _review=mock.Mock(side_effect=review),
                           release_workspace=mock.DEFAULT),
            workspace: dict(fetch=mock.DEFAULT, remote_head=mock.Mock(side_effect=lambda *_: self.head)),
            runlimits: dict(branch_head=mock.Mock(side_effect=lambda *_: self.head)),
            ci: dict(watch=mock.Mock(side_effect=ci_watch)),
            jev: dict(ready=mock.Mock(return_value=True), risk=mock.Mock(return_value=SimpleNamespace(hold=""))),
            gh: dict(open_pr_url=mock.Mock(return_value=""), pr_create=mock.Mock(side_effect=create_pr),
                     pr_merge_details=mock.Mock(side_effect=lambda _: {"state": "OPEN", "isDraft": self.draft,
                         "headRefOid": self.head, "baseRefName": "main", "baseRefOid": self.base}),
                     pr_body=mock.Mock(side_effect=lambda _: self.body), pr_edit_body=mock.Mock(side_effect=edit_body),
                     pr_comment=mock.DEFAULT, find_own_pr_comment=mock.Mock(return_value=None),
                     pr_set_draft=mock.Mock(side_effect=lambda _, draft: setattr(self, "draft", draft)),
                     pr_draft_and_head=mock.Mock(side_effect=lambda _: (self.draft, self.head)),
                     merge_readiness=mock.Mock(return_value=("", "")), pr_merge_auto=mock.Mock(side_effect=merge),
                     issue_labels=mock.Mock(side_effect=label), issue_comment=mock.Mock(
                         side_effect=lambda _repo, _number, body: self.comments.append(body)),
                     issue=mock.Mock(return_value={"labels": []})),
        }
        for module, replacements in patches.items():
            self.enterContext(mock.patch.multiple(module, **replacements))
        trees = pipeline.verification_trees(b)
        job.verified = verification(trees=trees)
        job.review = Review.judged({"summary": "pre-catch-up review", "problems": [
            {"file": "file.txt", "severity": "minor", "problem": "Earlier approved note"}]}, [], [], trees)
        b.report = "local checks passed"
        b.checked = {"tree": trees[c.prefix], "commands": b.layer.checks, "report": b.report}
        with mock.patch.object(controller, "fingerprint", return_value=ledger.digest({c.github: trees[c.prefix]})):
            controller.stamp("checks", {"full": True, "checked": b.checked, "report": b.report})
            controller.stamp("verify", asdict(job.verified))
            controller.stamp("review", job.review.asdict())
        controller.persist()
        self.addCleanup(controller.release_merge_lease)

    def action_result(self, op, **args):
        controller = self.controller
        turn = ledger.begin(controller.lease, {}, 100)
        version = ledger.snapshot(controller.lease)["version"]
        action = ledger.accept(controller.lease, turn, version,
                               {"calls": [{"op": op, "args": OPERATIONS[op].arguments(args)}]}, 100)[0]
        controller.dispatch(action)
        return ledger.actions(controller.lease)[-1]

    def gates(self):
        self.action_result("checks.run")
        self.action_result("verification.run", route_id="verify", effort="low")
        self.action_result("review.run", route_id="review", effort="low")

    def publish_held(self, gate):
        controller = self.controller
        action = self.action_result("publication.request")
        self.assertEqual(action["state"], "failed")
        self.assertEqual(action["result"]["failures"], orchestration.CATCH_UP_FAILURE)
        self.assertIn(self.message, controller.data["catch_up_hold"]["feedback"])
        self.assertEqual(controller.data["evidence"], {})
        candidate = self.git("rev-parse", "HEAD")
        self.assertEqual(candidate, self.checkout.merge)
        self.assertNotEqual(candidate, self.head)
        self.assertEqual(self.git("merge-base", candidate, self.base), self.base)
        self.assertEqual(self.git("merge-base", candidate, self.head), self.head)
        self.assertIn(f"held on {gate}", self.body)
        if gate == "review":
            self.assertIn("base change at incoming.txt:1 (after)", self.body)
        self.assertIn("Closes #7", self.body)
        self.assertIn("Keep this.", self.body)
        self.assertNotIn("pre-catch-up review", self.body)
        self.assertNotIn("pre-catch-up verification", self.body)
        self.assertEqual(self.build.report, self.message if gate == "checks" else "local checks passed")

    def repair_and_publish(self, gate, role, *, last_catch_up=False, readiness_hold=""):
        self.prepare_publication(gate, move_base_again=last_catch_up)
        if last_catch_up:
            self.build.catch_ups = pipeline.AUTOMERGE_CATCH_UPS - 1
        self.publish_held(gate)
        self.assertIn("**request_changes**" if gate == "review" else "**fail**", self.body)
        candidate = self.checkout.merge
        controller = self.controller
        self.assertIsNone(controller.ending)
        controller.recover()
        self.assertEqual(controller.data["evidence"], {})
        setting = pipeline.steps.resolve(self.build.cfg, self.build.layer, "build", controller.run.kind,
                                         {"runner": "codex", "model": "test", "effort": "low"})
        controller.catalog.worker.return_value = setting, "delegate", True

        def repair(_):
            self.assertEqual(self.git("rev-parse", "HEAD"), candidate)
            self.assertTrue((self.root / "incoming.txt").is_file())
            self.file.write_text("repaired implementation\n")

        messages = [{"event": "launch", "step": "worker", "role": role, "route": {}},
                    {"event": "result", "ok": True, "text": "Repaired"}]
        worker = SimpleNamespace(pid=123, identity=456, step="worker", receive=lambda: messages,
                                 allow=repair, close=lambda: None)
        request = mock.Mock(wraps=orchestration.delegation.request)
        repair_setting = replace(setting, step=role)
        with (mock.patch.object(pipeline.steps, "resolve", return_value=repair_setting),
              mock.patch.object(orchestration.delegation, "Worker", return_value=worker),
              mock.patch.object(orchestration.delegation, "request", request),
              mock.patch.object(workspace, "note_made"), mock.patch.object(workspace, "kept_work", return_value="")):
            launched = self.action_result("worker.start", goal="Repair the catch-up", route_id="build", effort="low")
            self.assertEqual(launched["result"]["role"], role)
            self.assertIs(request.call_args.args[3], repair_setting)
            self.assertIn(self.message, request.call_args.args[2])
            controller.collect()
        self.assertEqual(controller.run.budget.repair_calls, 1)
        self.gates()
        if readiness_hold:
            gh.merge_readiness.return_value = "", readiness_hold
        delivered = self.action_result("publication.request")
        self.assertEqual(len(self.pushes), 2)
        self.assertEqual(self.git("merge-base", candidate, self.pushes[-1]), candidate)
        if last_catch_up or readiness_hold:
            self.assertEqual(delivered["state"], "failed")
            self.assertEqual(controller.ending[0], "failed")
            self.assertEqual(self.labels, {"agent:failed"})
            self.assertTrue(self.draft)
            self.assertFalse(self.merged)
            if last_catch_up:
                self.assertEqual(self.build.catch_ups, pipeline.AUTOMERGE_CATCH_UPS)
                self.assertIn(f"after {pipeline.AUTOMERGE_CATCH_UPS} catch-ups", self.comments[-1])
            else:
                self.assertIn(readiness_hold, self.comments[-1])
            self.assertIn("held on review", self.comments[-1])
            return
        self.assertEqual(delivered["state"], "succeeded")
        self.assertEqual(controller.ending[0], "pr")
        self.assertTrue(self.merged)
        self.assertFalse(self.ticket_open)
        self.assertEqual(self.build.catch_ups, 1)
        self.assertEqual(self.ci_heads, self.pushes)
        self.assertEqual(self.git("show", "HEAD:file.txt"), "repaired implementation")
        self.assertIn("**approve**: approved on the new base", self.body)
        self.assertIn("**pass**: verified with the new base", self.body)
        self.assertNotIn(self.message, self.body)
        self.assertNotIn("merge_held", controller.run.facts)
        status, summary, url = controller.ending
        pipeline.record.settle(controller.run.id, status, summary=summary, pr_url=url)
        with (mock.patch.object(outcomes, "PR_SEEN_FILE", paths.STATE_DIR / "pr_seen.json"),
              mock.patch.object(outcomes, "record_feedback"),
              mock.patch.object(gh, "pr_view", return_value={"state": "MERGED", "mergedAt": util.iso()})):
            outcomes.sync_outcomes(config.Config())
        self.assertEqual(pipeline.run_store.run_get(controller.run.id)["status"], "merged")


class CatchUpReviewRepairTests(CatchUpRepairFixture):
    def test_review_hold_repairs_and_automerge_closes_ticket(self):
        self.repair_and_publish("review", "review_repair")


class CatchUpReviewTests(CatchUpRepairFixture):
    @classmethod
    def setUpClass(cls):
        cls.repository_fixture = Path(cls.enterClassContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        cls.create_repository(cls.repository_fixture)

    def set_up_repository(self):
        shutil.copytree(self.repository_fixture, self.root, dirs_exist_ok=True)

    def test_catch_up_requires_approval_for_the_pushed_tree(self):
        self.record_pushed_head()
        c = self.checkout
        prior = Review("approve", trees={"": self.git("rev-parse", "origin/main^{tree}")})
        push = mock.Mock()
        with (mock.patch.object(pipeline, "merge_moved_bases") as merge,
              self.assertRaisesRegex(workspace.RunFailed, "lacks a current approving review")):
            pipeline.catch_up_base(self.build, c, "fixture", self.git("rev-parse", "origin/main"), "", "", prior,
                                   before_push=push)
        merge.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), c.lease)

    def test_prompt_includes_approval_and_separates_base_from_resolution(self):
        self.prepare_publication("review", conflict=True)
        prior_trees = self.controller.job.review.trees
        self.review_agent.side_effect = None
        self.review_agent.return_value = Result(ok=True, structured={
            "summary": "The resolution lost behavior.",
            "problems": [{"severity": "blocker", "category": "regression", "file": "file.txt",
                          "problem": "The conflict resolution changed the output.",
                          "attribution": {"part": "resolution", "file": "file.txt", "line": 1}}],
        })
        action = self.action_result("publication.request")
        self.assertEqual(action["result"]["failures"], orchestration.CATCH_UP_FAILURE)
        self.assertIn("resolution change at file.txt:1 (after)", self.body)
        self.review_agent.assert_called_once()
        prompt = self.review_agent.call_args.args[1]
        approval, _, deltas = prompt.partition("## Approved diff")
        self.assertIn(self.head, approval)
        self.assertIn('"verdict": "approve"', approval)
        self.assertIn("pre-catch-up review", approval)
        self.assertIn("Earlier approved note", approval)
        self.assertIn(prior_trees[""], approval)
        approved, _, deltas = deltas.partition("## Base changes")
        base, _, resolution = deltas.partition("## Conflict-resolution changes")
        self.assertIn("\n+implementation\n", approved)
        self.assertIn("\n+base implementation\n", base)
        self.assertIn("\n+new base\n", base)
        self.assertNotIn("resolved implementation", base)
        self.assertIn("\n+resolved implementation\n", resolution)
        self.assertNotIn("\n+new base\n", resolution)

    def test_base_executable_bit_regression_holds_for_direct_and_dependent_citations(self):
        self.git("config", "core.fileMode", "true")
        helper = self.root / "helper tool"
        helper.write_text("#!/bin/sh\nprintf 'worked\\n'\n")
        helper.chmod(0o755)
        self.commit("add executable helper", helper.name)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

        def remove_executable_bit():
            helper.chmod(0o644)
            return (helper.name,)

        def invoke_caller():
            return subprocess.run(["/bin/sh", self.file.name], cwd=self.root, capture_output=True, text=True)

        self.prepare_publication("review", implementation="exec './helper tool'\n", edit_base=remove_executable_bit)
        self.assertEqual(invoke_caller().stdout, "worked\n")
        mode = {"file": helper.name}
        finding = {"file": "file.txt", "problem": "The helper gets permission denied.",
                   "severity": "blocker", "category": "regression"}

        def review(*_args, **_kwargs):
            self.assertEqual(invoke_caller().returncode, 126)
            return Result(ok=True, structured={"summary": "The caller stopped working.", "problems": [
                {**finding, "attribution": {"part": "base", **mode}},
                {**finding, "attribution": {"part": "approved", "file": "file.txt", "line": 1,
                                            "base_change": mode, "dependency": "The caller executes the helper directly."}},
            ]})

        self.review_agent.side_effect = review
        action = self.action_result("publication.request")
        self.assertEqual(action["result"]["failures"], orchestration.CATCH_UP_FAILURE)
        change = "base change at helper tool"
        self.assertIn(f"permission denied. ({change})", self.body)
        self.assertIn(f"approved change at file.txt:1 (after) depends on {change}", self.body)
        self.assertEqual(len(self.pushes), 1)
        self.assertFalse(self.merged)

    def test_read_only_reviewer_can_cite_resolution_hunk_omitted_from_prompt(self):
        context = "unchanged\n" * 8
        remainder = f"{context}old middle\n{context}old last\n"
        self.file.write_text("base\n" + remainder)
        self.commit("add context")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        padding = "x" * (pipeline.MAX_FIX_DIFF_IN_PROMPT // 2 + 100)
        omitted = "resolver broke the middle output"
        resolution = f"head {padding}\n{context}{omitted}\n{context}tail {padding}\n"
        self.prepare_publication("review", conflict=True, implementation="implementation\n" + remainder,
                                 resolution=resolution)
        patch_path = None

        def review(_step, prompt, cwd, *, allowed_tools, **_):
            nonlocal patch_path
            self.assertNotIn(omitted, prompt)
            self.assertNotIn("--remerge-diff", prompt)
            self.assertFalse(codex.writes(None, allowed_tools))
            notice = "The resolution excerpt is truncated. Read the full patch at `"
            self.assertIn(notice, prompt)
            name = prompt.split(notice, 1)[1].split("`", 1)[0]
            patch_path = cwd / name
            command = ["cat", name]
            self.assertEqual(codex.guard_reason({"tool_name": "Bash", "tool_input": {"command": command},
                                                 "cwd": str(cwd)}, False, []), "")
            patch = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout
            self.assertGreater(len(patch), pipeline.MAX_FIX_DIFF_IN_PROMPT)
            self.assertEqual(patch.count("\n@@ "), 3)
            self.assertIn(f"\n+{omitted}\n", patch)
            return Result(ok=True, structured={"summary": "The resolution changed the middle output.", "problems": [
                {"severity": "blocker", "category": "regression", "file": "file.txt", "problem": omitted,
                 "attribution": {"part": "resolution", "file": "file.txt", "line": 10}},
            ]})

        self.review_agent.side_effect = review
        action = self.action_result("publication.request")
        self.assertEqual(action["result"].get("failures"), orchestration.CATCH_UP_FAILURE,
                         self.build.run.log.call_args_list)
        self.assertIn(f"{omitted} (resolution change at file.txt:10 (after))", self.body)
        self.review_agent.assert_called_once()
        self.assertFalse(patch_path.exists())
        self.assertEqual(len(self.pushes), 1)
        self.assertFalse(self.merged)

    def test_unrelated_blocker_becomes_pr_note_and_merged_tree_is_approved(self):
        self.prepare_publication("unrelated")
        self.build.steps["review"] = 1
        action = self.action_result("publication.request")
        self.assertEqual(action["state"], "succeeded")
        self.assertTrue(self.merged)
        self.assertEqual(self.ci_heads, self.pushes)
        self.assertEqual(len(self.pushes), 2)
        self.assertNotEqual(self.git("rev-parse", f"{self.pushes[0]}^{{tree}}"), self.git("rev-parse", "HEAD^{tree}"))
        review = self.controller.job.review
        self.assertTrue(review.approved())
        self.assertEqual(review.trees, {"": self.git("rev-parse", "HEAD^{tree}")})
        self.assertIn(f"{self.message} (found on catch-up)", self.body)
        self.assertIn("### Review notes", self.body)
        self.assertIn("Keep this.", self.body)
        self.review_agent.assert_called_once()
        self.assertEqual(self.review_agent.call_args.args[0], "review2")
        self.assertEqual(self.review_agent.call_args.kwargs["role"], "review")
        self.assertEqual(self.build.steps["review"], 2)


class CatchUpReviewSafetyTests(CatchUpRepairFixture):
    def prepare_renamed_helpers(self):
        renames = {"base": ("base helper.txt", "moved helper.txt"),
                   "resolution": ("resolution helper.txt", "resolved\thelper.txt")}
        for part, (old, _) in renames.items():
            (self.root / old).write_text(f"{part} helper behavior\n")
        self.commit("add helpers", *(old for old, _ in renames.values()))
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

        def rename(part):
            old, new = renames[part]
            (self.root / old).rename(self.root / new)
            return old, new

        implementation = 'load("base helper.txt", "resolution helper.txt")\n'
        self.prepare_publication("review", conflict=True, implementation=implementation, resolution=implementation,
                                 edit_base=lambda: rename("base"))
        resolve = pipeline.resolve_conflicts

        def resolve_with_rename(*args):
            self.git("add", "--", *rename("resolution"))
            resolve(*args)

        self.enterContext(mock.patch.object(pipeline, "resolve_conflicts", side_effect=resolve_with_rename))
        return renames

    def test_rename_only_changes_hold_for_direct_and_dependent_citations(self):
        renames = self.prepare_renamed_helpers()
        caller = {"part": "approved", "file": "file.txt", "line": 1,
                  "dependency": "The approved caller still loads the old path."}

        def review(_step, prompt, _cwd, **_):
            self.assertIn("similarity index 100%\nrename from base helper.txt\nrename to moved helper.txt", prompt)
            self.assertIn('similarity index 100%\nrename from resolution helper.txt\nrename to "resolved\\thelper.txt"',
                          prompt)
            finding = {"file": "file.txt", "severity": "blocker", "category": "regression"}
            problems = [{**finding, "problem": f"Direct {part} rename finding.",
                         "attribution": {"part": part, "file": new}} for part, (_, new) in renames.items()]
            problems.append({**finding, "problem": "Dependent base rename finding.",
                             "attribution": {**caller, "base_change": {"file": renames["base"][0]}}})
            return Result(ok=True, structured={"summary": "The old paths no longer resolve.", "problems": problems})

        self.review_agent.side_effect = review
        action = self.action_result("publication.request")
        self.assertEqual(action["result"].get("failures"), orchestration.CATCH_UP_FAILURE)
        for part, (_, new) in renames.items():
            self.assertIn(f"Direct {part} rename finding. ({part} change at {new})", self.body)
        self.assertIn("Dependent base rename finding. (approved change at file.txt:1 (after) "
                      "depends on base change at base helper.txt", self.body)
        self.assertEqual(len(self.pushes), 1)
        self.assertFalse(self.merged)

    def test_incorrect_rename_citations_become_pr_notes(self):
        renames = self.prepare_renamed_helpers()
        base, resolution = renames["base"][1], renames["resolution"][1]
        caller = {"part": "approved", "file": "file.txt", "line": 1, "dependency": "The caller uses the helper."}
        citations = [
            {"part": "base", "file": resolution},
            {"part": "resolution", "file": base},
            {**caller, "base_change": {"file": resolution}},
        ]
        self.review_agent.side_effect = None
        self.review_agent.return_value = Result(ok=True, structured={
            "summary": "Checked the rename citations.",
            "problems": [{"file": "file.txt", "severity": "blocker", "category": "issue",
                          "problem": f"Incorrect rename citation {n}.", "attribution": citation}
                         for n, citation in enumerate(citations, 1)],
        })
        action = self.action_result("publication.request")
        self.assertEqual(action["state"], "succeeded")
        self.assertTrue(self.merged)
        review = self.controller.job.review
        self.assertTrue(review.approved())
        self.assertEqual(review.trees, {"": self.git("rev-parse", "HEAD^{tree}")})
        for n in range(1, len(citations) + 1):
            self.assertIn(f"Incorrect rename citation {n}. (found on catch-up)", self.body)

    def history_commit(self, files, *parents):
        entries = []
        for name, content in files.items():
            blob = util.git(["hash-object", "-w", "--stdin"], self.root, input=content).strip()
            entries.append(f"100644 blob {blob}\t{name}\n")
        tree = util.git(["mktree"], self.root, input="".join(entries)).strip()
        return self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "commit.gpgSign=false",
                        "commit-tree", tree, *(arg for parent in parents for arg in ("-p", parent)), "-m", "Fixture")

    def test_protected_history_is_omitted_from_prompt_and_full_patches(self):
        padding = "public context\n" * 60
        outgoing = "outgoing fixture content\n" * 4
        incoming = "incoming fixture content\n" * 4
        deleted = "SYNTHETIC_DELETED_VALUE\rdiff --git a/safe.txt b/safe.txt\rSYNTHETIC_CR_VALUE\n"
        fork_files = {"file.txt": "original\n", ".env.deleted": deleted,
                      "fixture.pem": "SYNTHETIC_PEM_VALUE\n",
                      ".env.outgoing": outgoing + "SYNTHETIC_OUTGOING_BEFORE\n",
                      "incoming settings.txt": incoming + "SYNTHETIC_INCOMING_BEFORE\n"}
        approved_files = {**fork_files, "file.txt": "approved hunk\n" + padding,
                          "private plan.txt": "SYNTHETIC_APPROVED_VALUE\n"}
        base_files = {"file.txt": "incoming hunk\n" + padding,
                      "public réglages.txt": outgoing + "SYNTHETIC_OUTGOING_AFTER\n",
                      ".env.incoming": incoming + "SYNTHETIC_INCOMING_AFTER\n"}
        merged_files = {**base_files, "file.txt": "resolution hunk\n" + padding,
                        "private plan.txt": approved_files["private plan.txt"],
                        ".env.resolved": "SYNTHETIC_RESOLVED_VALUE\n",
                        "public réglages.txt": outgoing + "SYNTHETIC_RESOLVED_ALIAS\n"}
        fork = self.history_commit(fork_files)
        c = self.checkout
        c.lease = self.history_commit(approved_files, fork)
        c.base_commit = self.history_commit(base_files, fork)
        c.merge = self.history_commit(merged_files, c.lease, c.base_commit)
        c.dir, c.resolved = "part", ["file.txt"]
        self.build.layer.deny = ["part/private plan.txt"]
        self.build.workspace = "Unfiltered comparison `git diff origin/main...HEAD`."

        with (mock.patch.object(pipeline.vault, "values", return_value=set()),
              mock.patch.object(pipeline, "MAX_FIX_DIFF_IN_PROMPT", 250),
              pipeline.catch_up_review_prompt(self.build, c, "", verify.Verified("pass"), Review("approve"))
              as (prompt, patches)):
            self.assertNotIn("git diff", prompt)
            self.assertNotIn("git show", prompt)
            artifacts = []
            for part, changed in (("approved", "approved hunk"), ("base", "incoming hunk"),
                                  ("resolution", "resolution hunk")):
                notice = f"The {part} excerpt is truncated. Read the full patch at `"
                self.assertIn(notice, prompt)
                artifact = self.root / prompt.split(notice, 1)[1].split("`", 1)[0]
                artifacts.append(artifact)
                self.assertIn(f"\n+{changed}\n", patches[part])
                self.assertEqual(artifact.read_text(), patches[part])
            for exposed in (prompt, *patches.values()):
                for hidden in ("SYNTHETIC_", ".env.", "fixture.pem", "incoming settings.txt", "public r",
                               "private plan.txt"):
                    self.assertNotIn(hidden, exposed)
        self.assertTrue(all(not artifact.exists() for artifact in artifacts))

    def test_binary_changes_hold_in_both_deltas_by_path(self):
        base_asset = self.root / "base.bin"
        resolution_asset = self.root / "resolution schema.bin"
        unchanged_asset = self.root / "unchanged.bin"
        for asset in (base_asset, resolution_asset, unchanged_asset):
            asset.write_bytes(b"\x00fixture schema v1\n")
        (self.root / ".gitattributes").write_text("base.bin diff\n")
        self.commit("add binary schema fixtures", ".gitattributes", base_asset.name,
                    resolution_asset.name, unchanged_asset.name)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

        def replace_base_asset():
            base_asset.write_bytes(b"\x00fixture schema BASE v2\n")
            return (base_asset.name,)

        implementation = 'read("base.bin", "resolution schema.bin", "unchanged.bin")\n'
        self.prepare_publication("review", conflict=True, implementation=implementation, resolution=implementation,
                                 edit_base=replace_base_asset)
        resolve = pipeline.resolve_conflicts

        def resolve_with_binary(*args):
            resolution_asset.write_bytes(b"\x00fixture schema RESOLUTION v2\n")
            self.git("add", "--", resolution_asset.name)
            resolve(*args)

        def review(_step, prompt, _cwd, **_):
            self.assertIn("Binary files differ", prompt)
            self.assertIn(f"Binary files a/{resolution_asset.name} and b/{resolution_asset.name} differ", prompt)
            self.assertNotIn("fixture schema", prompt)
            self.assertNotIn("GIT binary patch", prompt)
            finding = {"file": "file.txt", "severity": "blocker", "category": "regression"}
            caller = {"part": "approved", "file": "file.txt", "line": 1,
                      "dependency": "The caller requires schema v1."}
            unchanged = {"file": unchanged_asset.name}
            problems = []
            for part, asset in (("base", base_asset), ("resolution", resolution_asset)):
                problems.extend((
                    {**finding, "problem": f"Direct {part} binary finding.",
                     "attribution": {"part": part, "file": asset.name}},
                    {**finding, "problem": f"Unchanged binary in {part}.",
                     "attribution": {"part": part, **unchanged}},
                ))
            problems.extend((
                {**finding, "problem": "Dependent base binary finding.",
                 "attribution": {**caller, "base_change": {"file": base_asset.name}}},
                {**finding, "problem": "Unchanged dependency.", "attribution": {**caller, "base_change": unchanged}},
            ))
            return Result(ok=True, structured={"summary": "Two schemas changed.", "problems": problems})

        self.review_agent.side_effect = review
        with mock.patch.object(pipeline, "resolve_conflicts", side_effect=resolve_with_binary):
            action = self.action_result("publication.request")
        self.assertEqual(action["result"]["failures"], orchestration.CATCH_UP_FAILURE)
        for part, asset in (("base", base_asset), ("resolution", resolution_asset)):
            change = f"{part} change at {asset.name}"
            self.assertIn(f"Direct {part} binary finding. ({change})", self.body)
            self.assertIn(f"Unchanged binary in {part}. (found on catch-up)", self.body)
        self.assertIn("Dependent base binary finding. (approved change at file.txt:1 (after) "
                      "depends on base change at base.bin", self.body)
        self.assertIn("Unchanged dependency. (found on catch-up)", self.body)
        self.assertEqual(len(self.pushes), 1)
        self.assertFalse(self.merged)


class CatchUpAttributionTests(PublicationFixture):
    BLOCKER: ClassVar[dict] = {"file": "file.txt", "problem": "The caller no longer works.",
                               "severity": "blocker", "category": "regression"}

    def test_file_only_citation_requires_a_delta_without_text_hunks(self):
        patches = {"base": (
            "diff --git a/empty b/empty\ndeleted file mode 100644\nindex e69de29..0000000\n"
            "diff --git a/helper b/helper\nold mode 100755\nnew mode 100644\n"
            "--- a/helper\n+++ b/helper\n@@ -1 +1 @@\n-old\n+new\n"
        )}
        for location, severity in (({"file": "empty"}, "blocker"), ({"file": "helper"}, "major"),
                                   ({"file": "helper", "line": 1}, "blocker"), ({"file": "unchanged"}, "major")):
            with self.subTest(location=location):
                attribution = {"part": "base", **location}
                problems = catch_up_problems([{**self.BLOCKER, "attribution": attribution}], patches)
                self.assertEqual(problems[0]["severity"], severity)

    def test_repository_blank_context_setting_cannot_shift_changed_line_citations(self):
        c = self.checkout
        self.file.write_text("kept\n\nold\n")
        self.commit("add blank context")
        c.lease = self.git("rev-parse", "HEAD")
        self.file.write_text("kept\n\nnew\n")
        self.commit("change line after blank context")
        c.base_commit = c.merge = self.git("rev-parse", "HEAD")
        self.git("config", "diff.suppressBlankEmpty", "true")
        raw = self.git("diff", "--unified=3", "--no-color", "--no-ext-diff", "--no-textconv", c.lease, c.base_commit)
        self.assertIn("\n kept\n\n-old\n+new", raw)
        self.build.run.repo = config.Repo("fixture", self.root)
        self.build.ticket = tracker.Ticket("7", "#7", "Preserve attribution after blank context")

        with (mock.patch.object(pipeline.vault, "values", return_value=set()),
              pipeline.catch_up_review_prompt(self.build, c, "", verify.Verified("pass"), Review("approve"))
              as (prompt, patches)):
            self.assertIn("\n kept\n \n-old\n+new\n", prompt)
            for line, severity in ((2, "major"), (3, "blocker")):
                with self.subTest(line=line):
                    attribution = {"part": "base", "file": self.file.name, "line": line}
                    problems = catch_up_problems([{**self.BLOCKER, "attribution": attribution}], patches)
                    self.assertEqual(problems[0]["severity"], severity)


class CatchUpVerifyRepairTests(CatchUpRepairFixture):
    def test_verify_hold_repairs_and_automerge_closes_ticket(self):
        self.repair_and_publish("verify", "check_repair")


class CatchUpLimitTests(CatchUpRepairFixture):
    def test_repair_keeps_the_per_run_catch_up_limit(self):
        self.repair_and_publish("review", "review_repair", last_catch_up=True)


class CatchUpReadinessParkingTests(CatchUpRepairFixture):
    def test_readiness_failure_after_last_repair_parks_and_drafts(self):
        self.repair_and_publish("review", "review_repair", readiness_hold="a required check is missing")


class CatchUpParkingTests(CatchUpRepairFixture):
    def test_checks_hold_with_no_repair_cycles_parks_and_drafts(self):
        self.prepare_publication("checks", spent=True)
        self.publish_held("checks")
        self.assertEqual(self.controller.ending[0], "failed")
        self.assertEqual(self.labels, {"agent:failed"})
        self.assertTrue(self.draft)
        self.assertFalse(self.merged)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.controller.run.budget.repair_calls, 1)
        self.assertIn("held on checks", self.comments[-1])
        self.assertIn("local checks failed", self.comments[-1])
        self.assertIn("repair cycles", self.comments[-1])
        self.assertIn("Verification did not run.", self.body)
        self.assertIn("Review did not run.", self.body)


class BaseMergeDiffLimitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.enterContext(mock.patch.object(pipeline.record, "mark"))

    def git(self, c, *args):
        return util.git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                         "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", *args], c.wt).strip()

    def write_lines(self, c, name, count):
        (c.wt / name).write_text(f"{name}\n" * count)

    def commit(self, c, subject):
        self.git(c, "add", ".")
        self.git(c, "commit", "-qm", subject)

    def checkout(self, name, lines):
        root = self.root / name
        root.mkdir()
        c = workspace.Checkout(name, f"fixture/{name}", root, root, "main", resumes=True)
        self.git(c, "init", "-q", "--template=", "-b", c.base)
        self.git(c, "commit", "-qm", "base", "--allow-empty")
        self.git(c, "switch", "-qc", "work")
        self.write_lines(c, "work.txt", lines)
        self.commit(c, "published work")
        c.onto = self.git(c, "rev-parse", "HEAD")
        c.note_pushed_head(c.onto)
        return c

    def advance_origin(self, c, lines):
        self.git(c, "switch", "-q", "--detach", c.base)
        self.write_lines(c, "incoming.txt", lines)
        self.commit(c, "advance origin base")
        self.git(c, "update-ref", f"refs/remotes/{c.origin_base}", "HEAD")
        self.git(c, "switch", "-q", "work")

    def build(self, limit, *checkouts, committed_lines=0):
        return pipeline.Build(cfg=config.Config(), run=mock.Mock(id="diff-limit"), ticket=mock.Mock(),
                              ws=self.root, checkouts=list(checkouts),
                              layer=config.RepoLayer(dir=self.root, max_diff_lines=limit), timeout=0,
                              workspace="diff-limit", tools=[], deny=[], committed_lines=committed_lines)

    def test_committed_base_merge_passes_with_100_lines(self):
        c = self.checkout("first", 100)
        self.advance_origin(c, 500)
        self.git(c, "merge", "-q", "--no-edit", c.origin_base)
        self.build(300, c).require_valid_diff("the build")

    def test_committed_base_merge_refusal_names_origin_base(self):
        c = self.checkout("first", 400)
        self.advance_origin(c, 500)
        self.git(c, "merge", "-q", "--no-edit", c.origin_base)
        base = self.git(c, "rev-parse", "--short=7", c.origin_base)
        with self.assertRaisesRegex(workspace.RunFailed, f"the diff is 400 lines against main at {base}"):
            self.build(300, c).require_valid_diff("the build")

    def test_no_base_merge_counts_staged_pushed_and_resolved_lines(self):
        c = self.checkout("first", 80)
        self.advance_origin(c, 500)
        self.write_lines(c, "work.txt", 110)
        c.resolved_lines = 20
        b = self.build(130, c, committed_lines=80)
        b.require_valid_diff("the build")
        b.layer.max_diff_lines = 129
        with self.assertRaisesRegex(workspace.RunFailed, "the diff is 130 lines") as refused:
            b.validate_diff(b.staged, "the build")
        self.assertIn("pushed earlier in this run: 100", refused.exception.detail)

    def test_one_base_merge_counts_both_repositories_against_their_bases(self):
        first = self.checkout("first", 155)
        self.advance_origin(first, 500)
        self.git(first, "merge", "-q", "--no-edit", first.origin_base)
        first.onto = self.git(first, "rev-parse", "HEAD")
        second = self.checkout("second", 100)
        self.advance_origin(second, 500)
        self.write_lines(second, "work.txt", 120)
        b = self.build(400, first, second, committed_lines=255)
        b.require_valid_diff("the build")
        b.layer.max_diff_lines = 274
        with self.assertRaisesRegex(workspace.RunFailed, "the diff is 275 lines") as refused:
            b.validate_diff(b.staged, "the build")
        self.assertIn("`first/work.txt`: 155", refused.exception.detail)
        self.assertIn("`second/work.txt`: 120", refused.exception.detail)
        self.assertNotIn("pushed earlier", refused.exception.detail)


class MovedBasePublicationFixture:
    def controller(self):
        root = Path(".")
        checkout = workspace.Checkout("", "fixture/repo", root, root, "main",
                                      pr="https://github.com/fixture/repo/pull/7", lease="head")
        controller = orchestration.Controller.__new__(orchestration.Controller)
        run = mock.Mock(id="fixture-run", path="orchestrate", dry_run=False, alone=False, tid="7",
                        repo=SimpleNamespace(name="repo", parts=[checkout], public=False), catch_up_preserved=set(),
                        waiting=mock.Mock(return_value=nullcontext()), feedback=[], repairs="", facts={},
                        action="", environment_release="", owner_questions_blocked=False)
        job = mock.Mock(run=run, only="", checkouts=[checkout], with_prs=[checkout],
                        picked=mock.Mock(side_effect=lambda names: [checkout] if names else []),
                        verified=verify.Verified("pass"), review=Review("approve", trees={"": "head-tree"}),
                        approval=None, draft_why="", found=[], drafted=[],
                        changed=[checkout.github], opened=[checkout.github], pushed=[], ci_stopped=[], build_summary="updated")
        job.ticket = SimpleNamespace(labels=[], title="Edit fixture")
        job.b = mock.Mock(run=run, layer=SimpleNamespace(trust_level="automerge"), ticket=job.ticket,
                          checked={}, report="", staged=None, catch_ups=0)
        controller.run, controller.job = run, job
        controller.flow = SimpleNamespace(ctx=job, refusal="", continues="")
        controller.data = {"mode": "orchestrate", "evidence": {"checks": {"result": {"full": True}}}}
        controller.workers = {}
        controller.executing, controller.lease, controller.ending = "d0001-0", "fixture-lease", None
        controller.current = mock.Mock(return_value=True)
        controller.fingerprint = mock.Mock(return_value="tree")
        controller.persist = mock.Mock()
        controller.validate_publication_diff = mock.Mock()
        controller.atomic = mock.Mock(side_effect=lambda name, **kw: (
            flows.publish(controller.flow, **kw) if name == "publish"
            else factory.Outcome("pass", data={"heads": {checkout.github: checkout.lease}})))
        controller.catalog = mock.Mock(context={})
        self.addCleanup(controller.release_merge_lease)
        return controller, checkout


class AutomergeLeaseTests(MovedBasePublicationFixture, unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        locks = self.state / "locks"
        locks.mkdir()
        self.enterContext(mock.patch.multiple(paths, LOCKS_DIR=locks, ensure_dirs=mock.DEFAULT))
        self.running = []
        self.enterContext(mock.patch.object(slots.run_store, "runs_marked_running", return_value=self.running))
        self.enterContext(mock.patch.object(slots.chats_store, "chat_sessions", return_value=[]))
        self.enterContext(mock.patch.object(slots.zte_store, "activation_pending", return_value=False))
        self.enterContext(mock.patch.object(slots, "live_max_parallel", return_value=2))
        self.sleep = mock.Mock(side_effect=AssertionError("unexpected wait for a run slot"))
        self.enterContext(mock.patch.object(slots, "time", SimpleNamespace(sleep=self.sleep)))

    def running_slot(self, tid):
        run_id = f"run-{tid}"
        self.running.append({"id": run_id, "kind": "pipeline", "repo": "repo", "issue": tid})
        slot = slots.RunSlot("repo", tid, 2)
        slot.hold()
        self.addCleanup(slot.free)
        return run_id, slot

    def ready_run(self, tid):
        controller, checkout = self.controller()
        run, job = controller.run, controller.job
        run.tid = tid
        run.id, job.slot = self.running_slot(tid)
        checkout.pr = f"https://github.com/{checkout.github}/pull/{tid}"
        checkout.lease = f"head-{tid}"
        checkout.src = checkout.wt = self.state / tid
        job.b.checkouts = [checkout]
        controller.data["publication"] = {"heads": {checkout.github: checkout.lease}}
        controller.stamp = mock.Mock()
        return controller, checkout

    def test_waiter_catches_up_once_against_the_first_runs_merge(self):
        self.waiter_catches_up(fixed=False)

    def test_fixed_waiter_catches_up_after_refreshing_a_stale_base(self):
        self.waiter_catches_up(fixed=True)

    def test_timed_out_ci_waits_for_capacity_before_reading_the_moved_base(self):
        self.waiter_catches_up(fixed=False, ci_after_base_move="pending")

    def test_green_ci_waits_for_capacity_before_reading_the_moved_base(self):
        self.waiter_catches_up(fixed=False, ci_after_base_move="pass")

    def test_green_waiter_refreshes_an_unchanged_base_after_a_slot_frees(self):
        first, a = self.ready_run("1")
        second, b = self.ready_run("2")
        _, other_slot = self.running_slot("3")
        other_slot.free()
        first.take_merge_lease(a)
        waiting = threading.Event()
        capacity_wait = threading.Event()
        allow_resume = threading.Event()
        second.run.log.side_effect = lambda line: waiting.set() if "waiting for the merge lease" in line else None
        local_base = "stale-base"

        def wait_for_capacity(_seconds):
            self.assertTrue(util.lock_is_held("automerge-fixture%2Frepo"))
            self.assertIsNone(second.job.slot.held)
            capacity_wait.set()
            self.assertTrue(allow_resume.wait(1.5))

        self.sleep.side_effect = wait_for_capacity

        def fetch(src, branch):
            nonlocal local_base
            self.assertEqual((src, branch), (b.src, "main"))
            local_base = "current-base"

        def merge(_build, checkout, _number, head, base, _about, **_):
            self.assertEqual((checkout, head, base), (b, b.lease, "current-base"))
            self.assertTrue(util.lock_is_held("automerge-fixture%2Frepo"))
            self.assertTrue(first.job.slot.held and second.job.slot.held)
            return ""

        def watch(target, _log):
            check = gh.Check("checks", "pass")
            return ci.Watched(target, {check.key: check})

        with (mock.patch.object(ci, "watch", side_effect=watch),
              mock.patch.object(orchestration.pilot, "is_pilot_task", return_value=True),
              mock.patch.object(second, "check_pilot_publication"),
              mock.patch.object(pipeline, "publish_ci"),
              mock.patch.object(pipeline, "merge_evidence_hold", return_value=""),
              mock.patch.object(pipeline, "git", side_effect=lambda *_: local_base),
              mock.patch.object(pipeline, "has_merge_base", side_effect=lambda _, head, base: base == "current-base"),
              mock.patch.object(pipeline, "screen_and_merge", side_effect=merge) as merge_head,
              mock.patch.object(pipeline, "catch_up_base") as catch_up,
              mock.patch.object(workspace, "fetch", side_effect=fetch) as fetch_base,
              mock.patch.object(gh, "merge_readiness", return_value=("title", "")),
              mock.patch.object(jev, "ready", return_value=True),
              mock.patch.object(flows, "ended", return_value=("pr", "published", b.pr)),
              ThreadPoolExecutor(max_workers=1) as pool):
            result = pool.submit(second.publish)
            try:
                self.assertTrue(waiting.wait(1.5))
                self.assertIsNone(second.job.slot.held)
                other_slot.take(mock.Mock())
                self.assertEqual(len(slots.runs_in_slots()), 2)
                fetch_base.assert_not_called()
                first.release_merge_lease()
                self.assertTrue(capacity_wait.wait(1.5))
                fetch_base.assert_not_called()
                merge_head.assert_not_called()
                other_slot.free()
            finally:
                first.release_merge_lease()
                allow_resume.set()
            self.assertEqual(result.result(timeout=1.5)["ending"], "pr")
            fetch_base.assert_called_once()
            merge_head.assert_called_once()
            catch_up.assert_not_called()
        self.assertIsNone(second.merge_lease)
        self.sleep.assert_called_once()

    def test_classic_drafts_failed_catch_up_only_for_the_watched_head(self):
        controller, checkout = self.controller()
        job, run = controller.job, controller.run
        job.b.checkouts = [checkout]
        job.slot = mock.Mock(spec=slots.RunSlot)
        for result, live_head in (
            ("fail", "caught-up"),
            ("unread", "caught-up"),
            ("fail", "maintainer-head"),
            ("unread", "maintainer-head"),
            ("fail", ""),
            ("unread", gh.GhError("PR unavailable")),
            ("unread", ValueError("malformed PR state")),
        ):
            with self.subTest(ci=result, head=live_head):
                checkout.note_pushed_head("previous-head")
                job.b.catch_ups = 0
                refs = {"state": "OPEN", "isDraft": False, "headRefOid": checkout.lease, "baseRefName": "main"}

                def catch_up(*_):
                    checkout.note_pushed_head("caught-up")
                    return job.verified, job.review

                def watch(target, _log):
                    self.assertEqual(target.sha, "caught-up")
                    check = gh.Check("checks", result)
                    return ci.Watched(target, {check.key: check} if result == "fail" else {}, read=result == "fail")

                with (mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")) as hold,
                      mock.patch.object(gh, "pr_merge_details", return_value=refs),
                      mock.patch.object(pipeline, "fetch_merge_base", return_value="new-base"),
                      mock.patch.object(pipeline, "catch_up_base", side_effect=catch_up),
                      mock.patch.object(pipeline, "note_review"),
                      mock.patch.object(pipeline, "publish_gate_evidence"),
                      mock.patch.object(pipeline, "comment_catch_up_ci"),
                      mock.patch.object(ci, "watch", side_effect=watch) as watch_ci,
                      mock.patch.object(gh, "branch_sha", return_value="new-base"),
                      mock.patch.object(gh, "commit_checks", return_value={}),
                      mock.patch.object(gh, "pr_draft_and_head", return_value=(False, live_head),
                                        side_effect=live_head if isinstance(live_head, Exception) else None) as state,
                      mock.patch.object(gh, "pr_set_draft") as draft,
                      mock.patch.object(gh, "pr_comment") as comment):
                    summary = pipeline.automerge(job.b, None, "", job.verified, job.review, [checkout], "",
                                                 slot=job.slot, branch="run", goal="", plan="")
                watch_ci.assert_called_once()
                state.assert_called_once_with(checkout.pr)
                hold.assert_called_once()
                self.assertEqual(run.facts["merge_held"], f"catch-up CI held: {summary}")
                self.assertIn("A person merges it.", comment.call_args.args[1])
                if live_head == "caught-up":
                    draft.assert_called_once_with(checkout.pr, True)
                else:
                    draft.assert_not_called()

    def test_orchestrated_catch_up_ci_leaves_a_replacement_head_ready(self):
        for result in ("fail", "unread"):
            with self.subTest(ci=result):
                controller, checkout = self.ready_run("7")
                self.addCleanup(controller.close)
                pr = {"head": checkout.lease, "draft": False}

                def catch_up(*_, before_push):
                    evidence = verify.Verified("pass"), Review("approve")
                    before_push("caught-up", *evidence)
                    checkout.note_pushed_head("caught-up")
                    pr["head"] = checkout.lease
                    return evidence

                def watch(target, _log):
                    self.assertEqual(target.sha, pr["head"])
                    self.assertEqual(target.sha, "caught-up")
                    pr["head"] = "maintainer-head"
                    check = gh.Check("checks", "fail")
                    return ci.Watched(target, {check.key: check} if result == "fail" else {}, read=result == "fail")

                def draft_pr(_, draft):
                    pr["draft"] = draft

                refs = {"state": "OPEN", "isDraft": False, "headRefOid": checkout.lease, "baseRefName": "main"}
                controller.ci_failures = mock.Mock(return_value="CI needs diagnosis")
                with (mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")) as merge,
                      mock.patch.object(gh, "pr_merge_details", return_value=refs),
                      mock.patch.object(pipeline, "fetch_merge_base", return_value="new-base"),
                      mock.patch.object(pipeline, "catch_up_base", side_effect=catch_up),
                      mock.patch.object(pipeline, "note_review"),
                      mock.patch.object(pipeline, "publish_gate_evidence"),
                      mock.patch.object(pipeline, "comment_catch_up_ci"),
                      mock.patch.object(ci, "watch", side_effect=watch),
                      mock.patch.object(gh, "branch_sha", return_value="new-base"),
                      mock.patch.object(gh, "commit_checks", return_value={}),
                      mock.patch.object(gh, "pr_draft_and_head", side_effect=lambda _: (pr["draft"], pr["head"])) as state,
                      mock.patch.object(gh, "pr_set_draft", side_effect=draft_pr) as draft):
                    failure = controller.finish_publication()
                self.assertEqual(failure["failures"], orchestration.CI_FAILURES)
                self.assertEqual(pr, {"head": "maintainer-head", "draft": False})
                state.assert_called_once_with(checkout.pr)
                draft.assert_not_called()
                merge.assert_called_once()
                controller.close()
                controller.job.slot.free()

    def waiter_catches_up(self, *, fixed, ci_after_base_move=""):
        first, a = self.ready_run("1")
        second, b = self.ready_run("2")
        if fixed:
            second.run.path = "feature"
            second.job.branch = "run-2"
            second.job.goal, second.job.plan_text = "verify changes", "make a change"
            second.job.with_left.side_effect = lambda text: text
        watches = threading.Event()
        finish_watch = threading.Event()
        capacity_wait = threading.Event()
        allow_resume = threading.Event()
        waiting = threading.Event()
        second.run.log.side_effect = lambda line: waiting.set() if "waiting for the merge lease" in line else None
        base, merged, reads = "main-0", [], []
        heads = {a.pr: a.lease, b.pr: b.lease}
        checkouts = {c.src: c for c in (a, b)}
        local_bases = dict.fromkeys(checkouts, base)
        original_bases = {a.lease: "before-main-0", b.lease: "main-0"}
        caught_up = {}

        def fetch(src, branch):
            self.assertEqual(branch, "main")
            reads.append((checkouts[src].pr, base))
            local_bases[src] = base

        def git(args, wt):
            self.assertEqual(args, ["rev-parse", "origin/main"])
            return local_bases[wt]

        def catch_up(build, checkout, branch, base_sha, *args, before_push=None):
            self.assertTrue(util.lock_is_held("automerge-fixture%2Frepo"))
            controller = first if build.run is first.run else second
            self.assertIsNotNone(controller.job.slot.held)
            self.assertLessEqual(len(slots.runs_in_slots()), 2)
            if build.run is first.run and base_sha == "main-external" and ci_after_base_move == "pending":
                self.assertIn("still running", first.job.ci_result)
            if build.run is second.run and fixed:
                self.assertEqual((branch, *args[:2]), ("run-2", "verify changes", "make a change"))
            head = f"head-{build.run.tid}-caught-up-{build.catch_ups}"
            evidence = verify.Verified("pass"), Review("approve")
            caught_up[head] = base_sha
            if before_push is not None:
                before_push(head, *evidence)
            heads[checkout.pr] = head
            checkout.note_pushed_head(head)
            return evidence

        watch = ci.watch

        def watch_ci(target, log):
            if target.pr == a.pr and caught_up[target.sha] == "main-0":
                self.assertIsNone(first.job.slot.held)
                watches.set()
                self.assertTrue(finish_watch.wait(1.5))
                if ci_after_base_move:
                    target.pushed_at -= ci.WAIT_SECONDS
                    check = gh.Check("checks", ci_after_base_move)
                    with mock.patch.object(gh, "commit_checks", return_value={check.key: check}):
                        return watch(target, log)
            passed = gh.Check("checks", "pass")
            return ci.Watched(target, {passed.key: passed})

        def wait_for_capacity(_seconds):
            self.assertIsNone(first.job.slot.held)
            self.assertEqual([r["id"] for r in slots.runs_waiting_for_a_slot()], [first.run.id])
            self.assertTrue(util.lock_is_held("automerge-fixture%2Frepo"))
            self.assertEqual(len(slots.runs_in_slots()), 2)
            capacity_wait.set()
            self.assertTrue(allow_resume.wait(1.5))

        def merge(_repo, number, head, _branch, base_sha, **_):
            nonlocal base
            self.assertEqual(base_sha, base)
            self.assertEqual(caught_up[head], base)
            if number == 1:
                self.assertEqual(len(slots.runs_in_slots()), 2)
                third_slot.free()
            merged.append(number)
            base = f"main-after-{number}"
            return ""

        def publish(controller, *, fixed=False):
            try:
                if fixed:
                    return flows.publish(controller.flow).data
                result = controller.finish_publication()
                self.assertIsNone(controller.merge_lease)
                return result
            finally:
                controller.job.slot.free()
                controller.close()

        patches = {
            pipeline: dict(merge_evidence_hold=mock.Mock(return_value=""), git=mock.Mock(side_effect=git),
                           has_merge_base=mock.Mock(side_effect=lambda _, head, sha: caught_up.get(head, original_bases.get(head)) == sha),
                           catch_up_base=mock.Mock(side_effect=catch_up), checkout_patch=mock.Mock(return_value="diff"),
                           publish_gate_evidence=mock.DEFAULT, comment_catch_up_ci=mock.DEFAULT, note_review=mock.DEFAULT),
            workspace: dict(fetch=mock.Mock(side_effect=fetch)),
            gh: dict(pr_merge_details=mock.Mock(side_effect=lambda pr, repo="": {
                "state": "OPEN", "isDraft": False, "headRefOid": heads[f"https://github.com/{repo}/pull/{pr}" if repo else pr],
                "baseRefName": "main", "baseRefOid": base, "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE"}),
                merge_ci_passed=mock.Mock(return_value=True), pr_merge_auto=mock.Mock(side_effect=merge),
                pr_comment=mock.DEFAULT),
            jev: dict(ready=mock.Mock(return_value=True), risk=mock.Mock(return_value=SimpleNamespace(hold=""))),
            ci: dict(watch=mock.Mock(side_effect=watch_ci)),
            flows: dict(ended=mock.Mock(return_value=("pr", "published", ""))),
        }
        for module, replacements in patches.items():
            self.enterContext(mock.patch.multiple(module, **replacements))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_result = pool.submit(publish, first)
            try:
                self.assertTrue(watches.wait(1.5))
                with slots.admitting():
                    self.assertEqual(slots.slots_free_to_start(2), 1)
                    _, third_slot = self.running_slot("3")
                if ci_after_base_move:
                    self.sleep.side_effect = wait_for_capacity
                    base = "main-external"
                    finish_watch.set()
                    self.assertTrue(capacity_wait.wait(1.5))
                if fixed:
                    second.job.slot.free()
                second_result = pool.submit(publish, second, fixed=fixed)
                self.assertTrue(waiting.wait(1.5))
                self.assertIsNone(second.job.slot.held)
                self.assertEqual([r["id"] for r in slots.runs_waiting_for_merge_lease()], [second.run.id])
                self.assertEqual(slots.slots_free_to_start(3), 0)
                self.assertEqual(reads, [(a.pr, "main-0"), (a.pr, "main-0")])
                pipeline.comment_catch_up_ci.assert_not_called()
                self.assertEqual(merged, [])
                self.assertEqual((first.job.b.catch_ups, second.job.b.catch_ups), (1, 0))
            finally:
                finish_watch.set()
                allow_resume.set()
            self.assertEqual(first_result.result(timeout=1.5)["ending"], "pr")
            self.assertEqual(second_result.result(timeout=1.5)["ending"], "pr")
        self.assertEqual(merged, [1, 2])
        self.assertEqual((first.job.b.catch_ups, second.job.b.catch_ups), (2 if ci_after_base_move else 1, 1))
        self.assertEqual(set(sha for pr, sha in reads if pr == b.pr), {"main-after-1"})
        self.assertEqual(slots.runs_waiting_for_merge_lease(), [])
        gh.pr_comment.assert_not_called()

    def test_each_catch_up_ci_result_waits_for_a_free_slot(self):
        for n, result in enumerate(("pass", "fail", "unread"), 1):
            with self.subTest(ci=result):
                controller, checkout = self.ready_run(f"{n}1")
                controller.take_merge_lease(checkout)
                occupied = []

                def watch(target, _log):
                    self.assertIsNone(controller.job.slot.held)
                    occupied.extend(self.running_slot(f"{n}{i}")[1] for i in (2, 3))
                    check = gh.Check("checks", result)
                    return ci.Watched(target, {check.key: check} if result != "unread" else {}, read=result != "unread")

                def free_capacity(_seconds):
                    self.assertIsNone(controller.job.slot.held)
                    self.assertEqual([r["id"] for r in slots.runs_waiting_for_a_slot()], [controller.run.id])
                    self.assertTrue(util.lock_is_held("automerge-fixture%2Frepo"))
                    self.assertEqual(len(slots.runs_in_slots()), 2)
                    occupied[-1].free()

                def failure_logs(_round):
                    self.assertIsNotNone(controller.job.slot.held)
                    self.assertEqual(len(slots.runs_in_slots()), 2)
                    return "CI failed"

                self.sleep.reset_mock()
                self.sleep.side_effect = free_capacity
                controller.ci_failures = mock.Mock(side_effect=failure_logs)
                try:
                    with (mock.patch.object(ci, "watch", side_effect=watch),
                          mock.patch.object(gh, "branch_sha", return_value="main"),
                          mock.patch.object(gh, "commit_checks", return_value={}),
                          mock.patch.object(gh, "pr_draft_and_head", return_value=(False, checkout.lease)),
                          mock.patch.object(pipeline, "comment_catch_up_ci"),
                          mock.patch.object(pipeline, "set_draft")):
                        failure = controller.publication_ci([checkout], caught_up=True)
                    self.assertEqual(len(slots.runs_in_slots()), 2)
                    self.sleep.assert_called_once()
                    if result == "pass":
                        self.assertIsNone(failure)
                        controller.ci_failures.assert_not_called()
                    else:
                        self.assertEqual(failure["failures"], orchestration.CI_FAILURES)
                        controller.ci_failures.assert_called_once()
                finally:
                    controller.close()
                    controller.job.slot.free()
                    for slot in occupied:
                        slot.free()

    def test_dead_process_releases_the_lease(self):
        child = """
import os
import signal
from types import SimpleNamespace
from arete import slots, util
run = SimpleNamespace(repo=SimpleNamespace(name='repo'), tid='1')
lease = slots.take_merge_lease(run, 'fixture/repo')
print(util.lock_is_held('automerge-fixture%2Frepo'), flush=True)
os.kill(os.getpid(), signal.SIGKILL)
"""
        env = {**os.environ, "ARETE_STATE_DIR": str(self.state), "ARETE_LOG_DIR": str(self.state / "logs"),
               "ARETE_WORKTREES_DIR": str(self.state / "worktrees")}
        done = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=2, env=env)
        self.assertEqual((done.returncode, done.stdout.strip()), (-signal.SIGKILL, "True"), done.stderr)
        self.assertFalse(util.lock_is_held("automerge-fixture%2Frepo"))
        run = SimpleNamespace(repo=SimpleNamespace(name="repo"), tid="2")
        with slots.take_merge_lease(run, "fixture/repo"):
            self.assertTrue(util.lock_is_held("automerge-fixture%2Frepo"))


class CatchUpPublicationFailureTests(MovedBasePublicationFixture, unittest.TestCase):
    def test_publication_ci_after_a_catch_up_repair_parks_only_when_spent(self):
        spent = "the repair chain used its 1 repair cycles"
        for limit in ("", spent):
            with self.subTest(limit=limit):
                controller, checkout = self.controller()
                controller.data["catch_up_hold"] = {"reason": "Catch-up held on review"}
                controller.job.verified = verify.Verified("pass", checks=[{"passed": True}])
                controller.job.review = Review.stored({"verdict": "approve"})
                controller.run.budget.repair_limit_reason.return_value = limit
                controller.ci_failures = mock.Mock(return_value="CI check failed on the repaired head")
                with (mock.patch.object(ci, "watch", side_effect=lambda target, _: ci.Watched(target, {}, read=False)),
                      mock.patch.object(pipeline, "publish_gate_evidence"),
                      mock.patch.object(pipeline, "publish_ci"),
                      mock.patch.object(pipeline, "own_draft_state", return_value=True),
                      mock.patch.object(pipeline, "set_draft") as draft,
                      mock.patch.object(pipeline, "park_run", return_value=("failed", "parked", checkout.pr)) as park):
                    failure = controller.publish()
                self.assertEqual(failure["failures"], orchestration.CI_FAILURES)
                self.assertTrue(controller.data["ci_failed"])
                self.assertTrue(controller.data["catch_up_hold"])
                controller.catalog.add_context.assert_called_once_with(
                    orchestration.CI_FAILURES, "CI check failed on the repaired head")
                draft.assert_called_once_with(controller.run, checkout, True)
                if limit:
                    self.assertEqual(controller.ending, park.return_value)
                    self.assertIn(spent, park.call_args.args[1])
                    self.assertIn(failure["ci"], park.call_args.args[1])
                    self.assertIn("Catch-up held on review", controller.run.say.call_args.args[0])
                else:
                    self.assertIsNone(controller.ending)
                    park.assert_not_called()
                controller.release_merge_lease()

    def test_final_readiness_failure_parks_except_preserved_catch_up_handoffs(self):
        for error, parked in (
            (workspace.RunFailed("the published head already contains the fetched base"), True),
            (pipeline.ConflictUnresolved(workspace.RunFailed("a protected file conflicts"), ["file.txt"]), False),
            (pipeline.CatchUpNeedsInput("a catch-up worker needs owner input"), False),
        ):
            with self.subTest(error=error):
                controller, checkout = self.controller()
                controller.data["catch_up_hold"] = {"reason": "Catch-up held on verify"}
                controller.run.budget.repair_limit_reason.return_value = "the repair chain used its 1 repair cycles"
                refs = {"state": "OPEN", "isDraft": False, "headRefOid": checkout.lease, "baseRefName": "main"}
                with (mock.patch.object(gh, "pr_merge_details", return_value=refs),
                      mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")),
                      mock.patch.object(pipeline, "fetch_merge_base", return_value="moved-base"),
                      mock.patch.object(pipeline, "catch_up_base", side_effect=error),
                      mock.patch.object(pipeline, "report_automerge") as report,
                      mock.patch.object(controller, "park_catch_up", return_value=("failed", "parked", checkout.pr)) as park,
                      mock.patch.object(flows, "ended", return_value=("pr", "held", checkout.pr))):
                    controller.finish_publication()
                self.assertEqual(controller.job.b.catch_ups, 1)
                if parked:
                    self.assertEqual(controller.ending, park.return_value)
                    self.assertIn(error.why, park.call_args.args[0])
                    report.assert_not_called()
                else:
                    self.assertEqual(controller.ending[0], "pr")
                    self.assertIn(error.why, report.call_args.args[2])
                    park.assert_not_called()


class CatchUpRecoveryTests(MovedBasePublicationFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = Path(cls.enterClassContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        cls.enterClassContext(mock.patch.multiple(paths, STATE_DIR=cls.state, DB_FILE=cls.state / "arete.db",
                                                  RUNS_DIR=cls.state / "runs", LOCKS_DIR=cls.state / "locks",
                                                  LOG_DIR=cls.state / "logs", WORKTREES_DIR=cls.state / "worktrees"))
        cls.enterClassContext(mock.patch.object(db.dbstats.Timing, "finished"))
        db.migrate()

    def setUp(self):
        self.ctrl, self.checkout = self.controller()
        job, c = self.ctrl.job, self.checkout
        self.repo = config.Repo(self._testMethodName, self.state, github=c.github, parts=[config.Part("", c.github)])
        self.run = self.new_run()
        self.ctrl.run = job.run = job.b.run = self.run
        job.ticket = job.b.ticket = tracker.Ticket("7", "#7", "Edit fixture")
        self.ctrl.data.update(publication_intent={}, publication={"heads": {c.github: c.lease}})
        self.ctrl.lease = ledger.acquire(self.run.id, "", self.ctrl.data)
        self.ctrl.prepare = mock.Mock(return_value=None)
        self.ctrl.observe = mock.Mock(side_effect=lambda: {"state_version": ledger.snapshot(self.ctrl.lease)["version"]})
        self.ctrl.take_merge_lease = mock.Mock()
        self.ctrl.publication_ci = mock.Mock(return_value=None)
        self.ctrl.stamp = mock.Mock()
        self.body = ("Closes #7\n\n## Checked in use\n\nEarlier verification.\n\n## Review\n\nEarlier review.\n\n"
                     "## Owner note\n\nKeep this.")
        self.head, self.draft = c.lease, False
        self.enterContext(mock.patch.object(gh, "gh", side_effect=AssertionError("unexpected GitHub call")))
        self.enterContext(mock.patch.object(gh, "pr_body", side_effect=lambda _: self.body))
        self.enterContext(mock.patch.object(gh, "pr_draft_and_head", side_effect=lambda _: (self.draft, self.head)))
        self.enterContext(mock.patch.object(gh, "pr_set_draft", side_effect=lambda _, draft: setattr(self, "draft", draft)))
        self.edit = self.enterContext(mock.patch.object(gh, "pr_edit_body", side_effect=self.edit_body))
        issue = {"number": 7, "title": job.ticket.title, "author": {"login": "fixture"},
                 "labels": [{"name": tracker.LABEL}], "state": "OPEN"}
        self.enterContext(mock.patch.object(gh, "issue", return_value=issue))
        self.enterContext(mock.patch.object(gh, "issue_labels", return_value=""))
        self.comment = self.enterContext(mock.patch.object(gh, "issue_comment", return_value=""))
        self.push = self.enterContext(mock.patch.object(pipeline, "commit_and_push"))
        self.enterContext(mock.patch.object(pipeline, "git", side_effect=AssertionError("unexpected git call")))
        self.enterContext(mock.patch.object(pipeline, "catch_up_target", return_value=("new-base", "")))

    def new_run(self):
        rid = pipeline.record.start("pipeline", repo=self.repo.name, issue="7")
        folder = paths.RUNS_DIR / rid
        folder.mkdir(parents=True)
        run = pipeline.Run(rid, self.repo, folder, tracker=tracker.GitHubTracker(self.repo), tid="7")
        self.enterContext(mock.patch.object(run, "log"))
        self.enterContext(mock.patch.object(run, "enter"))
        return run

    def edit_body(self, url, text, *, link):
        self.assertEqual(url, self.checkout.pr)
        self.body = gh.outgoing(text, link)

    def catch_up_hold(self):
        verified = verify.Verified("fail", "Verification failed on the new base.")
        error = pipeline.CatchUpGateHold("verify", "verification found a blocker", "candidate", "", verified=verified)
        with mock.patch.object(pipeline, "catch_up_base", side_effect=error):
            result = self.ctrl.catch_up(self.checkout)
        self.assertEqual(result["failures"], orchestration.CATCH_UP_FAILURE)

    def use_last_repair(self):
        self.catch_up_hold()
        self.run.budget = runlimits.Budget(self.run, runlimits.Policy(max_repair_cycles=1), "")
        self.run.budget.charge("repair", "review_repair")
        self.assertTrue(self.run.budget.repair_limit_reason())

    def assert_parked_for(self, limit):
        status, summary, _ = self.ctrl.drive()
        self.assertEqual(status, "failed")
        self.assertIn(limit, summary)
        self.assertTrue(self.draft)
        self.comment.assert_called_once()
        ticket_comment = self.comment.call_args.args[2]
        self.assertIn("Catch-up to `main` at `new-base` held on verify", ticket_comment)
        self.assertIn(limit, ticket_comment)

    def test_last_repair_parks_at_decision_limit(self):
        self.use_last_repair()
        self.ctrl.policy = config.Orchestration(max_decisions=1)
        ledger.begin(self.ctrl.lease, {}, self.ctrl.policy.max_decisions)
        self.assert_parked_for("1 decision allowance")

    def test_decision_limit_stops_repair_before_retaining_workspace(self):
        self.use_last_repair()
        self.ctrl.policy = config.Orchestration(max_decisions=1)
        worker = self.start_writing_worker()

        def close():
            self.assertFalse(self.draft)
            self.comment.assert_not_called()

        def keep_edits(run):
            self.assertIs(run, self.run)
            worker.close.assert_called_once_with()
            self.assertFalse(self.ctrl.workers)
            self.assertEqual(ledger.actions(self.ctrl.lease)[0]["state"], "interrupted")

        held = {".": {"head": "retained-head", "onto": "candidate"}}
        worker.close.side_effect = close
        with (mock.patch.object(pipeline, "keep_edits", side_effect=keep_edits) as keep,
              mock.patch.object(pipeline, "kept_heads", return_value=held)):
            self.assert_parked_for("1 decision allowance")
        keep.assert_called_once_with(self.run)
        parked = runlimits.metadata(self.run.id, "parked.json")
        self.assertEqual(parked["kept"], held)
        self.assertIn("held on verify", parked["reason"])
        self.assertIn("1 decision allowance", parked["reason"])

    def test_decision_limit_parks_even_when_worker_cleanup_fails(self):
        self.use_last_repair()
        self.ctrl.policy = config.Orchestration(max_decisions=1)
        self.run.workspace = self.state, [self.checkout]
        self.run.feedback = [{"body": "Please revise."}]
        worker = self.start_writing_worker()
        error = RuntimeError("worker producer group did not become quiescent")

        def close():
            self.assertFalse(self.draft)
            self.comment.assert_not_called()
            raise error

        worker.close.side_effect = close
        with (mock.patch.object(pipeline, "keep_edits") as keep,
              mock.patch.object(util, "group_alive", return_value=True)):
            with self.assertRaises(RuntimeError) as raised:
                self.ctrl.drive()
        self.assertIs(raised.exception, error)
        worker.close.assert_called_once_with()
        self.assertEqual(ledger.actions(self.ctrl.lease)[0]["state"], "running")
        self.assertTrue(self.draft)
        self.comment.assert_called_once()
        ticket_comment = self.comment.call_args.args[2]
        reason = runlimits.metadata(self.run.id, "parked.json")["reason"]
        for text in ("Catch-up to `main` at `new-base` held on verify", "1 decision allowance"):
            self.assertIn(text, ticket_comment)
            self.assertIn(text, reason)
        keep.assert_not_called()

    def test_decision_limit_retains_workspace_when_stopped_workers_ledger_update_fails(self):
        self.catch_up_hold()
        checkout = self.checkout
        checkout.wt = self.state / "retained-repair"
        checkout.wt.mkdir()
        (checkout.wt / ".git").touch()
        checkout.onto = "candidate"
        error = RuntimeError("ledger update failed")
        errors = []

        def drive(flow):
            run = flow.ctx.run
            self.ctrl.run = self.ctrl.job.run = self.ctrl.job.b.run = run
            run.workspace = checkout.wt, [checkout]
            self.ctrl.lease = ledger.acquire(run.id, "", self.ctrl.data)
            self.ctrl.policy = config.Orchestration(max_decisions=1)
            worker = self.start_writing_worker()

            def interrupt(*_):
                worker.close.assert_called_once_with()
                self.assertFalse(self.draft)
                self.comment.assert_not_called()
                raise error

            with (mock.patch.object(ledger, "transition", side_effect=interrupt),
                  mock.patch.object(util, "group_alive", return_value=False)):
                try:
                    return self.ctrl.drive()
                except RuntimeError as raised:
                    errors.append(raised)
                    raise

        with (mock.patch.object(pipeline, "commit_kept") as commit,
              mock.patch.object(pipeline, "git", return_value="retained-head\n"),
              mock.patch.object(workspace, "note_made") as note_made):
            self.assertEqual(self.run_ticket_again(drive), 1)
        run = self.ctrl.run
        self.assertEqual(errors, [error])
        commit.assert_any_call(run.id, checkout, run.layer)
        note_made.assert_called_once_with(checkout.wt, [checkout], mock.ANY, preserve=set())
        self.assertTrue(self.draft)
        hold_comments = [call.args[2] for call in self.comment.call_args_list if "held on verify" in call.args[2]]
        self.assertEqual(len(hold_comments), 1)
        self.assertIn("1 decision allowance", hold_comments[0])
        parked = runlimits.metadata(run.id, "parked.json")
        self.assertIn("1 decision allowance", parked["reason"])
        self.assertEqual(parked["kept"], {".": {"head": "retained-head", "onto": "candidate"}})

    def test_outer_cleanup_retains_workspace_when_writing_worker_survives(self):
        self.catch_up_hold()
        checkout = self.checkout
        checkout.wt = self.state / "surviving-repair"
        checkout.wt.mkdir()
        error = RuntimeError("worker producer group did not become quiescent")

        def drive(flow):
            run = flow.ctx.run
            self.ctrl.run = self.ctrl.job.run = self.ctrl.job.b.run = run
            run.workspace = checkout.wt, [checkout]
            self.ctrl.lease = ledger.acquire(run.id, "", self.ctrl.data)
            self.ctrl.policy = config.Orchestration(max_decisions=1)
            worker = self.start_writing_worker()

            def close():
                self.assertFalse(self.draft)
                self.comment.assert_not_called()
                raise error

            worker.close.side_effect = close
            with self.assertRaises(RuntimeError) as raised:
                self.ctrl.drive()
            self.assertIs(raised.exception, error)
            worker.close.assert_called_once_with()
            raise raised.exception

        with (mock.patch.object(util, "group_alive", return_value=True),
              mock.patch.object(pipeline, "keep_edits") as keep,
              mock.patch.object(workspace, "note_made") as note_made):
            self.assertEqual(self.run_ticket_again(drive), 1)

        run = self.ctrl.run
        self.assertTrue(self.draft)
        parked = runlimits.metadata(run.id, "parked.json")
        hold_comments = [call.args[2] for call in self.comment.call_args_list if "held on verify" in call.args[2]]
        self.assertEqual(len(hold_comments), 1)
        for text in ("Catch-up to `main` at `new-base` held on verify", "1 decision allowance"):
            self.assertIn(text, hold_comments[0])
            self.assertIn(text, parked["reason"])
        self.assertIn(f"crashed: RuntimeError: {error}", (run.dir / "run.log").read_text())
        keep.assert_called_once_with(run)
        note_made.assert_called_once_with(checkout.wt, [checkout], mock.ANY, preserve=set())

    def test_budget_expiry_during_limit_cleanup_preserves_catch_up_park(self):
        self.use_last_repair()

        def drive(flow):
            run = flow.ctx.run
            self.ctrl.run = self.ctrl.job.run = self.ctrl.job.b.run = run
            self.ctrl.lease = ledger.acquire(run.id, "", self.ctrl.data)
            self.ctrl.policy = config.Orchestration(max_decisions=1)
            worker = self.start_writing_worker()
            error = runlimits.Parked(run.budget.reason())
            worker.close.side_effect = error
            with self.assertRaises(runlimits.Parked) as raised:
                self.ctrl.drive()
            self.assertIs(raised.exception, error)
            worker.close.assert_called_once_with()
            raise raised.exception

        with mock.patch.object(util, "group_alive", return_value=True):
            self.assertEqual(self.run_ticket_again(drive), 1)

        self.assertTrue(self.draft)
        self.comment.assert_called_once()
        ticket_comment = self.comment.call_args.args[2]
        reason = runlimits.metadata(self.ctrl.run.id, "parked.json")["reason"]
        for text in ("Catch-up to `main` at `new-base` held on verify", "1 decision allowance"):
            self.assertIn(text, ticket_comment)
            self.assertIn(text, reason)

    def start_writing_worker(self):
        lease = self.ctrl.lease
        turn = ledger.begin(lease, {}, self.ctrl.policy.max_decisions)
        action, = ledger.accept(lease, turn, ledger.snapshot(lease)["version"],
                                {"calls": [{"op": "worker.start", "args": {}}]}, self.ctrl.policy.max_actions)
        ledger.transition(lease, action["id"], "running", {"writing": True, "role": "check_repair"})
        worker = mock.Mock(writing=True, effective={})
        worker.receive.return_value = []
        worker.proc.poll.return_value = None
        self.ctrl.workers[action["id"]] = worker
        return worker

    def test_time_limit_reports_hold_when_writer_shutdown_fails(self):
        self.catch_up_hold()
        error = RuntimeError("worker producer group did not become quiescent")

        def drive(flow):
            run = flow.ctx.run
            self.ctrl.run = self.ctrl.job.run = self.ctrl.job.b.run = run
            self.ctrl.lease = ledger.acquire(run.id, "", self.ctrl.data)
            self.ctrl.policy = config.Orchestration()
            self.ctrl.last_progress = 0
            worker = self.start_writing_worker()
            def close():
                self.assertTrue(self.draft)
                self.comment.assert_called_once()
                raise error

            worker.close.side_effect = close
            self.ctrl.decide = mock.Mock(side_effect=lambda: self.ctrl.wait_workers(0))
            with mock.patch.object(run.budget, "run_deadline", return_value=0):
                try:
                    return self.ctrl.drive()
                finally:
                    worker.close.assert_called_once_with()

        with (mock.patch.object(pipeline, "commit_kept") as keep,
              mock.patch.object(pipeline, "git", side_effect=AssertionError("unexpected workspace read")) as kept,
              mock.patch.object(workspace, "note_made") as note_made):
            self.assertEqual(self.run_ticket_again(drive), 1)
        run = self.ctrl.run
        self.assertTrue(self.draft)
        hold_comments = [call.args[2] for call in self.comment.call_args_list if "held on verify" in call.args[2]]
        self.assertEqual(len(hold_comments), 1)
        self.assertIn("Catch-up to `main` at `new-base` held on verify", hold_comments[0])
        self.assertIn("minute budget", hold_comments[0])
        self.assertIn(f"crashed: RuntimeError: {error}", (run.dir / "run.log").read_text())
        keep.assert_not_called()
        kept.assert_not_called()
        note_made.assert_not_called()

    def test_last_repair_parks_at_action_limit(self):
        self.use_last_repair()
        self.ctrl.policy = config.Orchestration(max_actions=1)
        lease = self.ctrl.lease
        turn = ledger.begin(lease, {}, self.ctrl.policy.max_decisions)
        calls = [{"op": "checks.run", "args": {}}]
        version = ledger.snapshot(lease)["version"]
        action, = ledger.accept(lease, turn, version, {"calls": calls}, self.ctrl.policy.max_actions)
        ledger.transition(lease, action["id"], "succeeded", {})
        self.run.action = action["id"]
        self.ctrl.catalog.validate.side_effect = lambda response, *args, **kw: response
        self.assert_parked_for("1 action allowance")

    def controller_batches(self, *batches):
        self.ctrl.catalog.validate.side_effect = lambda response, *_: response
        replies = [[{"ok": True, "structured": {"calls": calls}}] for calls in batches]
        worker = mock.Mock(pid=1, identity=None, receive=mock.Mock(side_effect=replies))
        self.enterContext(mock.patch.object(orchestration.delegation, "Worker", return_value=worker))
        self.enterContext(mock.patch.object(orchestration.delegation, "request", return_value={}))
        self.enterContext(mock.patch.object(self.run, "settings"))

    def test_oversized_gate_batch_can_retry_with_four_actions(self):
        self.use_last_repair()
        self.ctrl.policy = config.Orchestration(max_actions=4, max_decisions=2)
        self.ctrl.policy_sha = mock.Mock(return_value="policy")
        self.ctrl.data["acceptance"] = "current"
        calls = [{"op": "checks.run", "args": {}},
                 {"op": "verification.run", "args": {"route_id": "model:verify:0", "effort": "low"}},
                 {"op": "review.run", "args": {"route_id": "model:review:0", "effort": "low"}},
                 {"op": "publication.request", "args": {}}]
        observation = {"op": "run.observe", "args": {"after": 0, "limit": 20}}
        self.controller_batches([observation, *calls], calls)

        def perform(action):
            if action["op"] == "publication.request":
                self.ctrl.ending = ("pr", "gates passed", self.checkout.pr)
            return {}

        with mock.patch.object(self.ctrl, "perform", side_effect=perform):
            self.assertEqual(self.ctrl.drive(), ("pr", "gates passed", self.checkout.pr))
        actions = ledger.actions(self.ctrl.lease)
        self.assertEqual([(a["turn"], a["op"], a["state"]) for a in actions],
                         [("d0002", call["op"], "succeeded") for call in calls])
        self.assertFalse(self.draft)
        self.comment.assert_not_called()

    def fail_body_edit(self):
        self.edit.side_effect = gh.GhError("edit rejected")
        self.catch_up_hold()
        pending, = pipeline.run_store.pr_gate_refreshes(self.repo.name, "7")
        self.assertEqual((pending["run_id"], pending["head"]), (self.run.id, self.head))
        self.assertIn("Earlier verification.", self.body)
        failures = [call.args[0] for call in self.run.log.call_args_list if "could not update" in call.args[0]]
        self.assertEqual(len(failures), 1)
        self.assertIn("edit rejected", failures[0])
        self.edit.side_effect = self.edit_body

    def assert_body_refreshed(self, log_lines):
        self.assertEqual(pipeline.run_store.pr_gate_refreshes(self.repo.name, "7"), [])
        self.assertIn("Verification failed on the new base.", self.body)
        self.assertIn("Review did not run.", self.body)
        self.assertIn("Catch-up to `main` at `new-base` held on verify", self.body)
        self.assertIn("## Owner note\n\nKeep this.", self.body)
        self.assertTrue(self.body.startswith("Closes #7\n"))
        self.assertNotIn("Earlier review.", self.body)
        retries = [line for line in log_lines if "retrying gate evidence" in line]
        self.assertEqual(len(retries), 1)
        self.push.assert_not_called()

    def test_later_publish_retries_body_without_pushing(self):
        self.fail_body_edit()
        job = self.ctrl.job
        job.changed = job.opened = job.pushed = []
        self.assertTrue(flows.publish(self.ctrl.flow).passed)
        self.assert_body_refreshed(call.args[0] for call in self.run.log.call_args_list)

    def test_later_publish_retries_body_before_merge_callback(self):
        self.fail_body_edit()
        job = self.ctrl.job
        job.changed = job.opened = job.pushed = []

        def merge(candidates):
            self.assertEqual(candidates, [])
            self.assert_body_refreshed(call.args[0] for call in self.run.log.call_args_list)

        callback = mock.Mock(side_effect=merge)
        self.assertTrue(flows.publish(self.ctrl.flow, merge=callback).passed)
        callback.assert_called_once()

    def test_classic_publication_retries_body_after_startup_retry_fails(self):
        self.fail_body_edit()
        self.edit.reset_mock()
        self.edit.side_effect = gh.GhError("startup edit rejected")
        checkout = self.checkout
        checkout.wt = self.state
        layer = config.RepoLayer(self.state, factory_path="classic", trust_level="automerge")
        staged = pipeline.Staged(counts={"file.txt": 1}, changed=[checkout])
        verified, review = verify.Verified("pass"), Review("approve")

        def watch_ci(*args):
            self.edit.assert_called_once()
            self.assertIn("Earlier verification.", self.body)
            self.edit.side_effect = self.edit_body
            return "passed"

        def merge(b, candidate, *_args, **_kwargs):
            self.assertIs(candidate, checkout)
            self.assertEqual(self.edit.call_count, 2)
            self.assertEqual(pipeline.run_store.pr_gate_refreshes(self.repo.name, "7"), [])
            self.assertIn("Verification failed on the new base.", self.body)
            self.assertIn("## Owner note\n\nKeep this.", self.body)
            self.push.assert_called_once()

        self.enterContext(mock.patch.object(config, "load_layer", return_value=layer))
        self.enterContext(mock.patch.object(tracker.GitHubTracker, "prepare"))
        self.enterContext(mock.patch.object(gh, "pr_comment"))
        self.enterContext(mock.patch.object(workspace, "make_workspace", return_value=(self.state, [checkout])))
        self.enterContext(mock.patch.object(workspace, "note_made"))
        self.enterContext(mock.patch.multiple(pipeline, settle_starts=mock.DEFAULT, _learn=mock.DEFAULT,
                                              release_workspace=mock.DEFAULT))
        self.enterContext(mock.patch.object(pipeline, "git", return_value=self.head))
        self.enterContext(mock.patch.object(pipeline, "stage", return_value=staged))
        self.enterContext(mock.patch.object(pipeline.Run, "agent",
                                            side_effect=[Result(True, structured={"plan": "Edit the file."}),
                                                         Result(True, text="Edited the file.")]))
        self.enterContext(mock.patch.object(pipeline.Build, "check_with_repairs", return_value=(True, 1)))
        self.enterContext(mock.patch.object(pipeline.Build, "require_valid_diff"))
        self.enterContext(mock.patch.object(pipeline, "verify_in_use", return_value=verified))
        self.enterContext(mock.patch.object(pipeline, "_review", return_value=review))
        self.enterContext(mock.patch.object(pipeline, "watch_ci", side_effect=watch_ci))
        self.enterContext(mock.patch.object(slots, "take_merge_lease", return_value=nullcontext()))
        merge_attempt = self.enterContext(mock.patch.object(pipeline, "try_automerge", side_effect=merge))

        result = self.run_ticket_again()
        row = pipeline.run_store.newest_whole_run(self.repo.name, "7")
        log = (paths.RUNS_DIR / row["id"] / "run.log").read_text()
        self.assertEqual(result, 0, log)
        merge_attempt.assert_called_once()
        self.assertEqual(self.edit.call_count, 2)
        self.push.assert_called_once()

    def test_controller_retries_new_evidence_before_merge(self):
        self.publish_repaired_catch_up(retry_fails=False)

    def test_controller_keeps_new_evidence_pending_when_retry_fails(self):
        self.publish_repaired_catch_up(retry_fails=True)

    def publish_repaired_catch_up(self, *, retry_fails):
        self.catch_up_hold()
        job = self.ctrl.job
        self.ctrl.data["evidence"]["checks"] = {"result": {"full": True}}
        job.verified = verify.Verified("pass", "Repaired head verified.", checks=[
            {"passed": True, "what": "repair", "how": "run the check", "saw": "passed"}])
        job.review = Review("approve", "Repaired head approved.")
        self.edit.reset_mock()

        def edit_body(*args, **kwargs):
            if self.edit.call_count == 1 or retry_fails:
                raise gh.GhError("edit rejected")
            self.edit_body(*args, **kwargs)

        def merge(*_args, **_kwargs):
            self.assertEqual(self.edit.call_count, 2)
            pending = pipeline.run_store.pr_gate_refreshes(self.repo.name, "7")
            if retry_fails:
                row, = pending
                self.assertEqual(row["head"], self.head)
                self.assertIn("Repaired head verified.", row["evidence"]["checked"])
                self.assertIn("Verification failed on the new base.", self.body)
            else:
                self.assertEqual(pending, [])
                self.assertIn("Repaired head verified.", self.body)
                self.assertIn("Repaired head approved.", self.body)

        self.edit.side_effect = edit_body
        with (mock.patch.object(pipeline, "try_automerge", side_effect=merge) as merge_attempt,
              mock.patch.object(flows, "ended", return_value=("pr", "published", self.checkout.pr))):
            self.assertEqual(self.ctrl.publish()["ending"], "pr")
        merge_attempt.assert_called_once()
        self.assertEqual(bool(pipeline.run_store.pr_gate_refreshes(self.repo.name, "7")), retry_fails)
        self.assertEqual([call.args[0] for call in self.ctrl.atomic.call_args_list], ["pr", "publish"])
        self.push.assert_not_called()
        log_lines = [call.args[0] for call in self.run.log.call_args_list]
        self.assertEqual(sum("retrying gate evidence" in line for line in log_lines), 1)
        self.assertEqual(sum("could not update gate evidence" in line for line in log_lines), 2 if retry_fails else 1)

    def test_new_run_of_ticket_retries_saved_body_refresh(self):
        self.fail_body_edit()
        drive = mock.Mock(return_value=("noop", "fixture done", ""))
        self.assertEqual(self.run_ticket_again(drive), 0)
        drive.assert_called_once()
        run = drive.call_args.args[0].ctx.run
        self.assertNotEqual(run.id, self.run.id)
        self.assert_body_refreshed((run.dir / "run.log").read_text().splitlines())

    def test_new_run_retries_screened_evidence_after_verify_url_changes(self):
        old_host = "old-verifier.example.com"
        old_url = f"https://{old_host}/health"
        attempts = []

        def reject_edit(url, text, *, link):
            self.assertEqual(url, self.checkout.pr)
            attempts.append(gh.outgoing(text, link))
            raise gh.GhError("edit rejected")

        with (mock.patch.object(outbound, "extra_hosts", set()),
              mock.patch.object(outbound, "keep_configured_private")):
            outbound.keep_verify_private(old_url)
            self.edit.side_effect = reject_edit
            pipeline.publish_gate_evidence(self.run, self.ctrl.job.ticket, self.checkout,
                                           verify.Verified("fail", f"Checked {old_url}."),
                                           Review("request_changes", f"Could not verify {old_host}."))
            pending, = pipeline.run_store.pr_gate_refreshes(self.repo.name, "7")
            first, = attempts
            self.assertNotIn(old_host, first)

            outbound.extra_hosts.clear()
            outbound.keep_verify_private("https://new-verifier.example.com/health")
            self.assertEqual(outbound.screen(old_url), old_url)
            self.edit.side_effect = self.edit_body
            self.assertEqual(self.run_ticket_again(lambda _: ("noop", "fixture done", "")), 0)

        self.assertNotIn(old_host, self.body)
        self.assertNotIn(old_host, json.dumps(pending["evidence"]))
        self.assertIn("Checked [private address]/health.", self.body)
        self.assertIn("Could not verify [private address].", self.body)
        self.assertEqual(pipeline.run_store.pr_gate_refreshes(self.repo.name, "7"), [])
        self.push.assert_not_called()

    def run_ticket_again(self, drive=None):
        pipeline.record.settle(self.run.id, "failed")
        script = pipeline.Script("orchestrate", drive) if drive is not None else None
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        with (mock.patch.object(config, "load", return_value=config.Config(repos=[self.repo])),
              mock.patch.multiple(pipeline.scratch, process_ids=dict, service_labels=dict, RUN_BASELINES={}, RUN_LABELS={}),
              redirect_stdout(StringIO())):
            return pipeline.run_issue(self.repo.name, "7", script=script)

    def test_newer_gate_results_replace_the_pending_hold(self):
        self.fail_body_edit()
        c = self.checkout
        self.head = "repaired-head"
        c.note_pushed_head(self.head)
        self.body = self.body.replace("Keep this.", "Updated while refresh was pending.")
        pipeline.publish_gate_evidence(self.run, self.ctrl.job.ticket, c,
                                       verify.Verified("pass", "Repaired head verified."),
                                       Review("approve", "Repaired head approved."))
        self.assertIn("Repaired head verified.", self.body)
        self.assertIn("Repaired head approved.", self.body)
        self.assertIn("Updated while refresh was pending.", self.body)
        self.assertNotIn("Verification failed on the new base.", self.body)
        self.assertEqual(pipeline.run_store.pr_gate_refreshes(self.repo.name, "7"), [])

    def test_passing_catch_up_refreshes_evidence_and_retries_merge_as_before(self):
        c = self.checkout
        current = verify.Verified("pass", "Verified on the new base."), Review("approve", "Reviewed on the new base.")

        def catch_up(*_, before_push):
            before_push("caught-up", *current)
            self.head = "caught-up"
            c.note_pushed_head(self.head)
            return current

        with (mock.patch.object(pipeline, "try_automerge", side_effect=[gh.MovedBaseHold("base moved"), None]) as merge,
              mock.patch.object(pipeline, "catch_up_base", side_effect=catch_up)):
            self.assertIsNone(self.ctrl.automerge([c]))
        self.assertEqual(merge.call_count, 2)
        self.assertEqual(merge.call_args.args[2:4], current)
        self.ctrl.publication_ci.assert_called_once_with([c], caught_up=True)
        self.assertFalse(self.draft)
        self.assertEqual(self.ctrl.data["publication"]["heads"][c.github], "caught-up")
        self.assertIn("Verified on the new base.", self.body)
        self.assertIn("Reviewed on the new base.", self.body)
        self.assertEqual(pipeline.run_store.pr_gate_refreshes(self.repo.name, "7"), [])


class MovedBasePublicationTests(MovedBasePublicationFixture, unittest.TestCase):
    def test_conflict_after_a_rerun_passes_reaches_catch_up(self):
        controller, checkout = self.controller()
        clean = {"state": "OPEN", "headRefOid": checkout.lease, "mergeable": "MERGEABLE"}
        conflict = {**clean, "mergeable": "CONFLICTING"}
        failed = {"id": 1, "app": {"id": 1}, "name": "checks", "status": "completed", "conclusion": "failure"}
        passed = {**failed, "id": 2, "conclusion": "success"}
        with (mock.patch.object(gh, "pr_merge_details", side_effect=[clean, conflict]),
              mock.patch.object(gh, "gh_json", side_effect=[[{"check_runs": [failed]}], [[]],
                                                           [{"check_runs": [passed, failed]}], [[]]]),
              mock.patch.object(ci.time, "monotonic", return_value=0),
              mock.patch.object(ci.time, "sleep") as sleep,
              mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")),
              mock.patch.object(controller, "catch_up", return_value="could not catch up") as catch_up,
              mock.patch.object(pipeline, "report_automerge"),
              mock.patch.object(pipeline, "publish_ci") as report,
              mock.patch.object(pipeline, "set_draft") as draft,
              mock.patch.object(flows, "ended")):
            result = controller.publish()
        self.assertEqual(result["ending"], "pr")
        sleep.assert_called_once()
        catch_up.assert_called_once_with(checkout)
        self.assertIn("Passed: `checks`", report.call_args.args[3])
        self.assertIn("merge conflict", report.call_args.args[3])
        self.assertTrue(any("stopped waiting" in call.args[0] and "merge conflict" in call.args[0]
                            for call in controller.run.log.call_args_list))
        draft.assert_not_called()

    def test_catch_up_ci_failure_uses_publication_diagnosis_even_if_another_app_passed(self):
        controller, checkout = self.controller()
        refs = {"state": "OPEN", "isDraft": False, "headRefOid": "head", "baseRefName": "main",
                "baseRefOid": "moved-base"}

        def catch_up(*args, before_push):
            evidence = verify.Verified("pass"), Review.stored({"verdict": "approve"})
            before_push("new-head", *evidence)
            checkout.lease = "new-head"
            return evidence

        def transport(args, **kwargs):
            if "/statuses?" in args[1]:
                return [[]]
            checks = [{"id": n, "app": {"id": n}, "name": "build", "status": "completed", "conclusion": conclusion}
                      for n, conclusion in enumerate(("failure", "success"), 1)] if "/new-head/" in args[1] else []
            return [{"check_runs": checks}]

        def details(pr, **kwargs):
            return {**refs, "headRefOid": checkout.lease, "mergeable": "CONFLICTING"}

        with (mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")) as gate,
              mock.patch.object(gh, "pr_merge_details", side_effect=details),
              mock.patch.object(pipeline, "fetch_merge_base", return_value="moved-base"),
              mock.patch.object(pipeline, "catch_up_base", side_effect=catch_up),
              mock.patch.object(controller, "stamp"),
              mock.patch.object(ci, "watch", wraps=ci.watch) as watched,
              mock.patch.object(gh, "branch_sha", return_value="moved-base"),
              mock.patch.object(gh, "gh_json", side_effect=transport),
              mock.patch.object(pipeline, "publish_ci") as report,
              mock.patch.object(pipeline, "publish_gate_evidence"),
              mock.patch.object(controller, "ci_failures", return_value="failed check logs"),
              mock.patch.object(gh, "pr_draft_and_head", side_effect=lambda _: (False, checkout.lease)),
              mock.patch.object(pipeline, "set_draft") as draft,
              mock.patch.object(gh, "find_own_pr_comment", return_value=None),
              mock.patch.object(gh, "pr_comment") as comment):
            result = controller.publish()
        self.assertEqual(result["failures"], orchestration.CI_FAILURES)
        self.assertTrue(controller.data["ci_failed"])
        self.assertEqual(controller.data["publication"]["heads"][checkout.github], "new-head")
        self.assertNotIn("publication_intent", controller.data)
        self.assertEqual([call.args[0].sha for call in watched.call_args_list], ["head", "new-head"])
        controller.catalog.add_context.assert_called_once_with(orchestration.CI_FAILURES, "failed check logs")
        draft.assert_called_once_with(controller.run, checkout, True)
        gate.assert_called_once()
        report.assert_called_once()
        comment.assert_called_once()
        self.assertIn("head `new-head`", comment.call_args.args[1])
        self.assertIn("Failed", comment.call_args.args[1])

    def test_next_poll_reconciles_only_the_recorded_catch_up_push(self):
        first, checkout = self.controller()
        root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        checkout.src = checkout.wt = root
        (root / ".git").touch()
        refs = {"state": "OPEN", "isDraft": False, "baseRefName": "main", "baseRefOid": "moved-base"}
        verified = verify.Verified("pass", checks=[{"passed": True}], native_trees={"": "new-tree"})
        review = Review.stored({"verdict": "approve", "trees": {"": "new-tree"}})
        first.job.b.checked = {"tree": "new-tree", "commands": {"local": "check"}}
        first.job.b.catch_ups = 1

        def catch_up(b, c, branch, *args, before_push):
            c.merge, c.base_commit = "new-head", "moved-base"
            before_push(c.merge, verified, review)
            pipeline.push_caught_up_head(b, c, branch, c.merge)

        with (mock.patch.object(gh, "pr_merge_details", return_value={**refs, "headRefOid": "head"}),
              mock.patch.object(pipeline, "fetch_merge_base", return_value="moved-base"),
              mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")),
              mock.patch.object(pipeline, "catch_up_base", side_effect=catch_up),
              mock.patch.object(first, "publication_ci", return_value=None),
              mock.patch.object(pipeline, "commit_and_push", side_effect=OSError("lost push reply")) as push,
              mock.patch.object(pipeline.time, "sleep")):
            with self.assertRaises(pipeline.CatchUpPushUnconfirmed) as unconfirmed:
                first.publish()
        first.release_merge_lease()
        self.assertTrue(unconfirmed.exception.retryable)
        self.assertEqual(push.call_count, 3)
        ticket = tracker.Ticket("7", "#7", "Restore context", "Issue body", "Owner clarification", kind="fix")
        prepared = {"layer": str(root), "ticket": vars(ticket), "checkouts": [checkout.to_row()],
                    "branch": "fix", "attachments": "Saved attachment", "expertise": "Saved expertise",
                    "triage": "", "add_dirs": []}
        saved = json.dumps({**first.data, "prepared": prepared, "catch_ups": first.job.b.catch_ups,
                            "verified": {"verdict": "pass"}, "checked": first.job.b.checked,
                            "run_settings": "settings", "overrides": {}, "step_overrides": {}})
        action = {"id": first.executing, "op": "publication.request", "state": "reconciliation-required"}
        question = {"id": "d0001-1", "op": "owner.ask", "state": "accepted"}

        for initial, tree in (("new-head", "new-tree"), ("head", "new-tree"), ("unrelated-head", "new-tree"),
                              ("head", "different-tree")):
            with self.subTest(pr_head=initial, tree=tree):
                resumed, _ = self.controller()
                resumed.flow.continues = first.run.id
                resumed.data = json.loads(saved)
                resumed.job = resumed.flow.ctx = flows.Job(config.Config(), resumed.run, root, built=resumed.job.b)
                resumed.run.add_dirs, resumed.run.story, resumed.run.workspace = [], [], None
                resumed.run.settings.return_value = SimpleNamespace(chain=[])
                remote = initial

                def retry(all_checkouts, changed, branch, message, *, before_push):
                    nonlocal remote
                    self.assertEqual(remote, "head")
                    self.assertEqual(changed[0].lease, "head")
                    self.assertIn("catch_up", resumed.data["publication_intent"])
                    remote = before_push(changed[0])

                def ci_gate(pushed, *, caught_up):
                    self.assertTrue(caught_up)
                    candidate = resumed.job.checkouts[0]
                    self.assertEqual(pushed, [candidate])
                    self.assertEqual(candidate.lease, remote)
                    self.assertNotIn("catch_up", resumed.data["publication_intent"])
                    self.assertEqual(resumed.job.verified, verified)
                    self.assertEqual(resumed.job.review, review)
                    self.assertEqual(resumed.catalog.read("task", 0, 8000)["text"],
                                     "Restore context\n\nIssue body\n\nOwner clarification")
                    self.assertEqual(resumed.catalog.read("expertise", 0, 8000)["text"], "Saved expertise")
                    self.assertEqual(resumed.catalog.read("attachments", 0, 8000)["text"], "Saved attachment")
                    return {"error": "CI failed"}

                def lineage():
                    resumed.catalog = orchestration.Catalog(resumed.job)

                with (mock.patch.object(orchestration.ledger, "find", return_value={"data": resumed.data}),
                      mock.patch.object(orchestration.ledger, "actions", return_value=[action, question]),
                      mock.patch.object(orchestration.ledger, "transition") as transition,
                      mock.patch.multiple(resumed, lineage=mock.Mock(side_effect=lineage),
                                          write_lineage_event=mock.DEFAULT, quiescent=mock.DEFAULT, stamp=mock.DEFAULT),
                      mock.patch.object(orchestration.run_lines, "write"),
                      mock.patch.multiple(pipeline, checked_layer=mock.Mock(return_value=SimpleNamespace(
                          checks={"local": "check"})), check_script=mock.Mock(return_value=([], [])), note_prs=mock.DEFAULT,
                                          publish_gate_evidence=mock.DEFAULT),
                      mock.patch.object(resumed, "publication_ci", side_effect=ci_gate) as watched,
                      mock.patch.object(pipeline, "catch_up_base") as merge,
                      mock.patch.multiple(workspace, uncommitted=mock.Mock(return_value=[]),
                                          scratch_left=mock.Mock(return_value=[])),
                      mock.patch.object(util, "git", return_value="new-head"),
                      mock.patch.object(pipeline, "git", return_value=tree),
                      mock.patch.object(gh, "pr_merge_details", side_effect=lambda *_: {**refs, "headRefOid": remote}),
                      mock.patch.object(pipeline, "commit_and_push", side_effect=retry) as retried):
                    resumed.job.b.layer = pipeline.checked_layer(None, root)
                    self.assertEqual(flows.guard(resumed.job, first.run.id), "")
                    if tree != "new-tree":
                        with self.assertRaisesRegex(factory.Refused, "native behavioral pass"):
                            resumed.prepare()
                    elif initial == "unrelated-head":
                        with self.assertRaises(pipeline.CatchUpPushUnconfirmed) as refused:
                            resumed.prepare()
                        self.assertFalse(refused.exception.retryable)
                    else:
                        self.assertIsNone(resumed.prepare())
                        watched.assert_called_once()
                        self.assertNotIn("publication_intent", resumed.data)
                        self.assertEqual([call.args[1:3] for call in transition.call_args_list],
                                         [(question["id"], "cancelled"), (first.executing, "failed")])
                        self.assertEqual(transition.call_args.kwargs["expected"], ("reconciliation-required",))
                    if initial == "unrelated-head" or tree != "new-tree":
                        self.assertIn("catch_up", resumed.data["publication_intent"])
                        watched.assert_not_called()
                        transition.assert_not_called()
                    self.assertEqual(resumed.job.b.catch_ups, 2)
                    self.assertEqual(retried.call_count, int(initial == "head" and tree == "new-tree"))
                    merge.assert_not_called()
                resumed.release_merge_lease()

    def test_collision_file_survives_the_final_pr_hold(self):
        for kind in ("ignored", "untracked"):
            with self.subTest(kind=kind):
                self.check_collision_hold(kind)

    def check_collision_hold(self, kind):
        controller, checkout = self.controller()
        root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        checkout.wt = controller.job.ws = root / "checkout"
        path = checkout.wt / "ignored" / "data.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"owner's file")
        (checkout.wt / ".git").touch()
        for file in (path, checkout.wt / ".git", root / "workspaces" / "checkout.json"):
            self.addCleanup(file.unlink, missing_ok=True)
        refs = {"state": "OPEN", "isDraft": False, "headRefOid": "head", "baseRefName": "main",
                "baseRefOid": "moved-base"}

        def git(args, *_, **kw):
            if args == ["rev-parse", "head^{tree}"]:
                return "head-tree"
            return {"rev-parse": "head", "rev-list": "1", "merge-tree": "merged-tree"}.get(args[0], "")

        def git_records(args, wt):
            if args[0] == "diff" or (args[0] == "ls-files" and ("--ignored" in args) == (kind == "ignored")):
                return ["ignored/data.txt"]
            if args[0] == "status":
                if kind == "ignored" and "--ignored=matching" in args:
                    return ["!! ignored/"]
                if kind == "untracked" and "--untracked-files=all" in args:
                    return ["?? ignored/data.txt"]
            return []

        def remove_checkout(*_):
            path.unlink()
            return True

        with (mock.patch.object(gh, "pr_merge_details", return_value=refs), mock.patch.object(gh, "pr_comment"),
              mock.patch.object(pipeline, "fetch_merge_base", return_value="moved-base"),
              mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")),
              mock.patch.object(controller, "publication_ci", return_value=None),
              mock.patch.object(pipeline, "git", side_effect=git), mock.patch.object(workspace, "git", side_effect=git),
              mock.patch.object(util, "git_records", side_effect=git_records),
              mock.patch.object(util, "repo_lock", return_value=nullcontext()),
              mock.patch.object(pipeline.record, "timed", return_value=nullcontext({})),
              mock.patch.object(pipeline, "abort_merge"), mock.patch.object(util, "git_as_owner") as merge,
              mock.patch.object(workspace, "scratch_left", return_value=[]), mock.patch.object(paths, "STATE_DIR", root),
              mock.patch.object(workspace, "remove_workspace", side_effect=remove_checkout) as remove,
              mock.patch.object(pipeline, "record_prs", return_value=checkout.pr),
              mock.patch.object(flows, "ended", side_effect=lambda flow, outcome: flows.respond(flow, **outcome.data))):
            self.assertEqual(controller.publish()["ending"], "pr")
            self.assertEqual(path.read_bytes(), b"owner's file")
            self.assertEqual(workspace.blocked_by(checkout.wt, [checkout]),
                             ["ignored/" if kind == "ignored" else "ignored/data.txt"])
        self.assertIn("`main` at `moved-base`", controller.run.facts["merge_held"])
        self.assertIn(f"{kind} file `ignored/data.txt`", controller.run.facts["merge_held"])
        merge.assert_not_called()
        remove.assert_not_called()

    def test_resolver_question_cleans_its_ignored_file_and_names_moved_base(self):
        controller, checkout = self.controller()
        root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        checkout.wt = controller.job.ws = root / "checkout"
        ignored = checkout.wt / "ignored"
        ignored.mkdir(parents=True)
        (checkout.wt / ".git").touch()
        existing, created = ignored / "kept", ignored / "resolver"
        existing_untracked, untracked = checkout.wt / "kept", checkout.wt / "resolver"
        for path in (existing, created, existing_untracked, untracked, checkout.wt / ".git",
                     root / "workspaces" / "checkout.json"):
            self.addCleanup(path.unlink, missing_ok=True)
        existing.write_bytes(b"keep me")
        existing_untracked.write_bytes(b"keep me too")
        refs = {"state": "OPEN", "isDraft": False, "headRefOid": "head", "baseRefName": "main",
                "baseRefOid": "moved-base"}

        def resolve(*args, **kwargs):
            self.assertTrue(controller.run.owner_questions_blocked)
            created.write_text("remove me")
            untracked.write_text("remove me too")
            raise owner_asks.Asked([], [], "publish", "resolve", NO_SESSION)

        def git_records(args, wt):
            files = (existing, created) if "--ignored" in args else (existing_untracked, untracked)
            names = [str(path.relative_to(wt)) for path in files if path.exists()]
            if args[0] == "ls-files":
                return names
            if "--ignored=matching" in args:
                return ["!! ignored/"]
            return [f"?? {name}" for name in names] if "--untracked-files=all" in args else []

        with (mock.patch.object(gh, "pr_merge_details", return_value=refs), mock.patch.object(gh, "pr_comment"),
              mock.patch.object(pipeline, "fetch_merge_base", return_value="moved-base"),
              mock.patch.object(pipeline, "merge_hold", return_value=gh.MovedBaseHold("base moved")),
              mock.patch.object(controller, "publication_ci", return_value=None),
              mock.patch.object(pipeline, "git", side_effect=lambda args, *_: (
                  "head-tree" if args == ["rev-parse", "head^{tree}"] else "head")),
              mock.patch.object(workspace, "git", return_value="head"),
              mock.patch.object(util, "git_records", side_effect=git_records),
              mock.patch.object(util, "repo_lock", return_value=nullcontext()),
              mock.patch.object(pipeline, "merge_moved_bases", side_effect=resolve),
              mock.patch.object(pipeline, "abort_merge") as abort,
              mock.patch.object(pipeline, "commit_and_push") as push,
              mock.patch.object(workspace, "scratch_left", return_value=[]), mock.patch.object(paths, "STATE_DIR", root),
              mock.patch.object(workspace, "remove_workspace", return_value=True) as remove,
              mock.patch.object(pipeline, "record_prs", return_value=checkout.pr),
              mock.patch.object(flows, "ended", side_effect=lambda flow, outcome: flows.respond(flow, **outcome.data))):
            self.assertEqual(controller.publish()["ending"], "pr")
            self.assertEqual(workspace.blocked_by(checkout.wt, [checkout]), [])
        hold = controller.run.facts["merge_held"]
        self.assertIn("`main` at `moved-base`", hold)
        self.assertIn("needs owner input", hold)
        self.assertNotIn("publication_intent", controller.data)
        self.assertFalse(created.exists())
        self.assertFalse(untracked.exists())
        self.assertEqual(existing.read_bytes(), b"keep me")
        self.assertEqual(existing_untracked.read_bytes(), b"keep me too")
        self.assertFalse(controller.run.owner_questions_blocked)
        remove.assert_called_once_with(checkout.wt, [checkout])
        abort.assert_called_once_with(checkout.wt)
        push.assert_not_called()


class ServeDeploymentFixture(unittest.TestCase):
    class_timeout = 30

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
        (package / "other.py").write_text("other = 1\n")
        (package / "shared.py").write_text("shared = 1\n")
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


class ServeDeploymentTests(ServeDeploymentFixture):
    def setUp(self):
        super().setUp()
        self.tip = self.publish("merged code")

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


class ServeDeploymentPilotTests(ServeDeploymentFixture):
    def test_live_pilot_receipt_blocks_until_held_or_spent(self):
        first_tip = self.publish("merged code")
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
                tip = first_tip if terminal == "held" else self.publish(f"merge after {terminal}")
                receipt.write_text(json.dumps({"phase": terminal}))
                self.assertEqual(self.deploy(), tip)


class ServeDeploymentRefusalTests(ServeDeploymentFixture):
    def test_refusal_logs_once_per_tip_and_reason(self):
        tip = self.publish("merged code")
        for status, conclusion in (("queued", None), ("completed", "failure"), ("queued", None)):
            self.ci_state = (status, conclusion)
            self.assertEqual(self.deploy(), self.start)
        other = self.publish("second merge")
        self.assertEqual(self.deploy(), self.start)
        messages = [call.args[0] for call in self.server.say.call_args_list]
        self.assertEqual([message.split(" left alone for ")[1] for message in messages],
                         [f"{tip} (CI pending)", f"{tip} (CI failed)", f"{other} (CI pending)"])
        self.ci_state = ("completed", "success")
        self.assertEqual(self.deploy(), other)
        self.assertIn(f"fast forwarded to {other} (second merge)", self.server.say.call_args.args[0])


class SelfDeployRollbackTests(unittest.TestCase):
    class_timeout = 5

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                                       cwd=self.checkout,
                                       text=True, timeout=2).strip()

    def prepare_state(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.checkout = root / "checkout"
        self.state = root / "state"
        self.github = "fixture/arete"
        self.ref = "refs/remotes/origin/main"
        self.previous, self.tip = "1" * 40, "2" * 40
        self.enterContext(mock.patch.multiple(paths, HOME=root, PACKAGE_DIR=self.checkout / "arete",
                                             STATE_DIR=self.state, CONFIG_DIR=root / "config",
                                             CONFIG_FILE=root / "config" / "config.toml",
                                             LOCKS_DIR=self.state / "locks", DB_FILE=self.state / "arete.db",
                                             RUNS_DIR=self.state / "runs", LOG_DIR=root / "logs",
                                             WORKTREES_DIR=root / "worktrees", SERVER_FILE=self.state / "server.json",
                                             ISOLATED=False))
        self.server = object.__new__(serve.Server)
        self.server.deploy_refusals = set()
        self.server.say = mock.Mock()

    def prepare_checkout(self, candidate):
        self.prepare_state()
        self.checkout.mkdir()
        self.git("init", "-q", "-b", "main")
        package = self.checkout / "arete"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "cli.py").write_text("def main():\n    print('last good started')\n    return 0\n")
        self.git("add", "arete")
        self.git("commit", "-qm", "working entry point")
        for name, body in candidate.items():
            file = self.checkout / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(body)
        self.git("add", ".")
        self.git("commit", "-qm", "new code", "--allow-empty")
        self.tip, self.previous = self.git("log", "-2", "--format=%H").splitlines()

    def receipt(self):
        return serve_guard.read(self.state / serve_guard.RECEIPT)

    def write_receipt(self, **updates):
        created_at = updates.pop("created_at", time.time())
        receipt = serve_guard.Receipt(str(self.checkout), "main", self.previous, self.tip, self.github, created_at,
                                      **updates)
        serve_guard.write(self.state / serve_guard.RECEIPT, asdict(receipt))

    def start(self):
        plist = install.serve_plist()
        return subprocess.run(plist["ProgramArguments"], cwd=plist["WorkingDirectory"],
                              env=plist["EnvironmentVariables"], capture_output=True, text=True, timeout=2)

    def test_import_rejection_counts_as_a_failed_start(self):
        self.prepare_state()
        self.write_receipt()
        self.server.code = frozenset({("HEAD", self.previous[:12])})
        stamp = frozenset({("HEAD", self.tip[:12])})
        self.server.code_seen = stamp
        self.server.code_note = ""
        failure = subprocess.CompletedProcess([], 1, "", "ImportError: broken candidate initializer")
        attempted_at = time.time()

        def failed_probe(*args, **kwargs):
            clock.return_value = attempted_at + 60
            return failure

        with (mock.patch.object(serve, "code_stamp", return_value=stamp),
              mock.patch.object(serve_guard.time, "time", return_value=attempted_at) as clock,
              mock.patch.object(util, "sh", side_effect=failed_probe),
              mock.patch.object(serve_guard, "at_tip", return_value=True),
              mock.patch.object(self.server, "busy_with", return_value=""),
              mock.patch.object(self.server, "restart_for_new_code") as restart):
            self.server.load_new_code()
        restart.assert_not_called()
        self.assertIn("broken candidate initializer", self.server.code_note)
        self.assertEqual((self.receipt()["starts"], self.receipt()["started_at"]), (1, attempted_at))
        self.assertFalse(serve_guard.hold_path(self.state, self.tip).exists())

    def test_launchd_rolls_back_after_three_failed_starts_and_holds_the_tip(self):
        token = "ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ"
        password = "synthetic-password-that-must-stay-local"
        self.prepare_checkout({"arete/__init__.py": f"raise ImportError('cannot import module token={token} "
                                                  f"password={password}')\n"})
        untracked = self.checkout / "arete" / "local.py"
        untracked.write_text("local = True\n")
        install.write_serve_guard()
        self.write_receipt(starts=2)
        observed = self.receipt()
        self.assertEqual((observed["previous"], observed["tip"], observed["starts"]),
                         (self.previous, self.tip, 2))

        self.write_receipt(healthy=True, starts=3, created_at=0)
        serve_guard.startup(self.state, self.checkout, paths.LOG_DIR)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual(self.receipt()["starts"], 3)
        self.assertFalse(serve_guard.hold_path(self.state, self.tip).exists())
        serve_guard.write(self.state / serve_guard.RECEIPT, observed)

        log = Path(install.serve_plist()["StandardErrorPath"])
        log.parent.mkdir(parents=True, exist_ok=True)
        failed = self.start()
        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertIn("ImportError: cannot import module", failed.stderr)
        log.write_text(failed.stderr)
        self.assertEqual(self.receipt()["starts"], 3)
        restored = self.start()
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn(f"deploy: rolled back to {self.previous} because 3 unhealthy starts", restored.stdout)
        self.assertIn("last good started", restored.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.previous)
        self.assertEqual(untracked.read_text(), "local = True\n")
        self.assertEqual(self.receipt()["starts"], 3)
        self.assertEqual(self.receipt()["rollback"], "code_only")
        self.assertIsNone(serve_guard.pending(self.state, self.checkout))
        self.assertEqual(serve.deploy_refusal(self.checkout, "main", self.tip), "held after startup rollback")

        held = serve_guard.read(serve_guard.hold_path(self.state, self.tip))
        self.assertIn(token, held["log_tail"])
        with (mock.patch.object(serve.codebase, "checkout_head", return_value=self.previous[:12]),
              mock.patch.object(gh, "gh", return_value=f"https://github.com/{self.github}/issues/52") as github):
            serve.report_rollbacks(self.checkout, self.server.say)
            serve.report_rollbacks(self.checkout, self.server.say)
        self.assertEqual(serve_guard.read(serve_guard.hold_path(self.state, self.tip))["issue"], 52)
        github.assert_called_once()
        self.assertEqual(github.call_args.args[0][:4], ["issue", "create", "--repo", self.github])
        self.assertEqual(github.call_args.args[0][8:], ["--label", "agent"])
        body = github.call_args.kwargs["input"]
        self.assertIn("Redacted serve.log tail", body)
        self.assertIn("ImportError: cannot import module", body)
        for private in (token, password, str(self.checkout)):
            self.assertNotIn(private, body)

    def test_terminal_wrapper_checks_receipt_before_loading_cli_commands(self):
        source = Path(serve_guard.__file__).parent
        self.prepare_checkout({f"arete/{name}": (source / name).read_text()
                               for name in ("cli.py", "local_paths.py", "serve_guard.py")})
        wrapper = paths.HOME / "arete-cli"
        wrapper.write_text(f"import sys\nsys.path.insert(0, {str(self.checkout)!r})\n"
                           "from arete.cli import main\nraise SystemExit(main())\n")
        self.write_receipt(starts=3, started_at=time.time())
        restored = subprocess.run([sys.executable, "-I", "-S", str(wrapper), "serve"],
                                  cwd=self.checkout, env=install.serve_plist()["EnvironmentVariables"],
                                  capture_output=True, text=True, timeout=2)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("last good started", restored.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.previous)
        with (mock.patch.object(serve.codebase, "checkout_head", return_value=self.previous[:12]),
              mock.patch.object(gh, "issue_create", return_value=52) as ticket):
            serve.report_rollbacks(self.checkout, self.server.say)
        self.assertIn(serve_guard.MISSING_TAIL, ticket.call_args.args[2])

    def test_unstarted_deploy_waits_for_chat_before_opening_health_window(self):
        self.prepare_state()
        self.write_receipt(created_at=time.time() - 6 * 60)
        self.server.code = frozenset({("HEAD", self.previous[:12])})
        stamp = frozenset({("HEAD", self.tip[:12])})
        self.server.code_seen = stamp
        self.server.code_note = ""
        self.server.migration_stamp = None
        self.server.polls, self.server.jobs = {}, {}
        self.server.oncall = self.server.sweep = None
        self.server.lock = self.enterContext(tempfile.TemporaryFile())
        with (mock.patch.object(serve, "code_stamp", return_value=stamp),
              mock.patch.object(util, "sh", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
              mock.patch.object(serve.chat, "any_busy", return_value=True) as busy,
              mock.patch.object(serve.capture, "checking", return_value=False),
              mock.patch.object(serve, "reap_children"),
              mock.patch.object(serve.os, "environ", serve.os.environ.copy()),
              mock.patch.object(serve.os, "execv", side_effect=OSError("restart candidate")) as restart):
            self.server.load_new_code()
            run.assert_not_called()
            restart.assert_not_called()
            self.server.say.assert_called_once_with("new code waits to load: a chat message is still running")
            self.assertEqual(self.receipt()["starts"], 0)
            busy.return_value = False
            with self.assertRaisesRegex(OSError, "restart candidate"):
                self.server.load_new_code()
            restart.assert_called_once()

        with mock.patch.object(serve_guard, "at_tip", return_value=True):
            self.assertFalse(serve_guard.count_start(self.state, self.checkout, paths.LOG_DIR))
        receipt = serve_guard.pending(self.state, self.checkout)
        self.assertEqual(receipt.starts, 1)
        self.assertGreater(receipt.started_at, receipt.created_at + serve_guard.AGE_LIMIT)
        self.assertFalse(receipt.expired())
        self.assertFalse(serve_guard.hold_path(self.state, self.tip).exists())
        with (mock.patch.object(serve_guard.time, "time", return_value=receipt.started_at + serve_guard.AGE_LIMIT),
              mock.patch.object(serve_guard, "at_tip", return_value=True),
              mock.patch.object(serve_guard, "rollback", return_value=True) as rollback):
            self.assertTrue(serve_guard.count_start(self.state, self.checkout, paths.LOG_DIR))
        rollback.assert_called_once_with(self.state, self.checkout, paths.LOG_DIR, receipt, serve_guard.AGE_FAILURE)

    def test_new_green_commit_deploys_after_config_rejection(self):
        self.prepare_state()
        self.write_receipt()
        self.server.checkout_update = threading.Lock()
        self.server.code = frozenset({("HEAD", self.previous[:12])})
        stamp = frozenset({("HEAD", self.tip[:12])})
        self.server.code_seen = stamp
        self.server.code_note = ""
        self.server.started_head = self.previous[:12]
        good = subprocess.CompletedProcess([], 0, "", "")
        bad_config = subprocess.CompletedProcess([], 1, "", "invalid candidate config")
        with (mock.patch.object(serve, "code_stamp", return_value=stamp),
              mock.patch.object(util, "sh", side_effect=[good, bad_config]),
              mock.patch.object(self.server, "busy_with", return_value=""),
              mock.patch.object(serve_guard, "at_tip", return_value=True),
              mock.patch.object(config, "load_or_problem", return_value=(config.Config(), ""))):
            self.server.load_new_code()
        self.assertIn("invalid candidate config", self.server.code_problem)
        self.server.acknowledge_deploy(True)
        self.assertEqual(self.receipt()["starts"], 1)
        self.assertFalse(self.receipt()["healthy"])

        newer = "3" * 40
        repo = config.Repo("arete", self.checkout, github=self.github)
        cfg = config.Config(arete_repo=repo.name, repos=[repo])
        self.checkout.mkdir()

        def git(args, *_args, **_kwargs):
            return {("rev-parse", "--show-toplevel"): str(self.checkout),
                    ("rev-parse", "--verify", self.ref): newer,
                    ("rev-parse", "--verify", "HEAD"): self.tip,
                    ("branch", "--show-current"): "main",
                    ("--no-replace-objects", "merge-base", self.tip, newer): self.tip,
                    ("--no-replace-objects", "show", "-s", "--format=%s", newer): "new candidate"}.get(tuple(args), "")

        with (mock.patch.object(util, "git", side_effect=git),
              mock.patch.object(serve, "pilot_deploy_refusal", return_value=""),
              mock.patch.object(util, "deploy_recovery", return_value=SimpleNamespace(head=self.tip)),
              mock.patch.object(util, "deploy_fast_forward") as fast_forward,
              mock.patch.object(gh, "default_branch", return_value="main"),
              mock.patch.object(gh, "deployment_ci_refusal", return_value="") as ci_gate):
            self.server.deploy_if_ready(cfg)
        self.assertEqual(ci_gate.call_args.args[1], newer)
        fast_forward.assert_called_once_with(self.checkout, newer)
        self.assertEqual((self.receipt()["previous"], self.receipt()["tip"], self.receipt()["starts"]),
                         (self.previous, newer, 0))

    def test_deploy_requires_a_full_tick_and_dashboard_readiness(self):
        self.prepare_state()
        self.server.with_dashboard = False
        cfg = config.Config()
        with mock.patch.object(self.server, "deploy") as deploy:
            self.server.deploy_if_ready(cfg)
        deploy.assert_called_once_with(cfg)
        self.write_receipt(starts=1)
        stamp = frozenset({("HEAD", self.tip[:12])})
        with (mock.patch.object(serve, "code_stamp", return_value=stamp),
              mock.patch.object(serve.codebase, "RELEASE", self.previous)):
            self.server = serve.Server()
        self.addCleanup(self.server.pool.shutdown)
        self.assertEqual(self.server.code, stamp)
        self.write_receipt()
        self.server.with_dashboard = True

        self.server.contracts_waiting = False
        self.server.code_problem = ""
        self.server.every_loop = self.server.live = []
        self.server.failing, self.server.logged = {}, {}
        actions = ("reap", "forget_unpolled_errors", "watch_oncall", "zte_when_due", "dispatch", "approvals",
                   "rotate_when_due", "prune_when_due", "sweep_when_due", "expire_asks", "release_closed_when_due",
                   "remind")
        with (mock.patch.multiple(self.server, load=mock.Mock(return_value=True),
                                  **{action: mock.DEFAULT for action in actions}) as stages,
              mock.patch.object(db, "ensure_schema"),
              mock.patch.object(serve.loop_store, "loop_states", return_value={}),
              mock.patch.object(serve.codebase, "RELEASE", ""),
              mock.patch.object(serve_guard, "at_tip", return_value=True),
              mock.patch.object(serve, "dashboard_answers", side_effect=[False, True]) as probe):
            self.server.acknowledge_deploy(self.server.tick())
            probe.assert_not_called()
            self.write_receipt(starts=1)
            stages["dispatch"].side_effect = RuntimeError("queue failure")
            self.server.acknowledge_deploy(self.server.tick())
            self.assertIn("stage:dispatch", self.server.failing)
            self.assertFalse(self.receipt()["healthy"])
            probe.assert_not_called()
            stages["dispatch"].side_effect = None
            self.server.acknowledge_deploy(self.server.tick())
            self.assertFalse(self.receipt()["healthy"])
            healthy_at_deploy = mock.Mock()
            with mock.patch.multiple(self.server, load_new_code=mock.DEFAULT, reap_inherited=mock.DEFAULT,
                                     watch_alerts=mock.DEFAULT, beat=mock.DEFAULT,
                                     deploy_when_due=lambda: healthy_at_deploy(self.receipt()["healthy"])):
                self.server.run_pass()
            healthy_at_deploy.assert_called_once_with(True)
        self.assertTrue(self.receipt()["healthy"])

    def test_pending_deploy_delivers_tick_alert_at_normal_threshold(self):
        self.prepare_state()
        self.write_receipt(starts=1, created_at=time.time() - serve_guard.AGE_LIMIT)
        self.server = serve.Server()
        self.addCleanup(self.server.pool.shutdown)
        self.server.cfg = config.Config()
        self.server.say = mock.Mock()
        dispatch = mock.Mock(return_value=True, side_effect=RuntimeError("queue failure"))
        self.server.tick = lambda: self.server.guard("stage:dispatch", dispatch) is True
        with (mock.patch.object(serve_guard, "at_tip", return_value=True),
              mock.patch.multiple(self.server, load_new_code=mock.DEFAULT, deploy_when_due=mock.DEFAULT,
                                  reap_inherited=mock.DEFAULT, beat=mock.DEFAULT,
                                  busy_with=mock.Mock(return_value="a chat message is still running")),
              mock.patch.multiple(serve.incident_store, alert_get=mock.DEFAULT, alert_set=mock.DEFAULT,
                                  alert_clear=mock.DEFAULT) as incidents,
              mock.patch.object(serve.alerts, "notify") as notify):
            incidents["alert_get"].return_value = None
            for _ in range(serve.ALERT_TICKS - 1):
                self.server.run_pass()
            self.assertIsNone(self.server.alert_last)
            self.server.run_pass()
            self.server.alert_last.result(timeout=1)
            incidents["alert_set"].assert_called_once_with(serve.ALERT_KEY, None)
            self.assertIn("stage:dispatch: RuntimeError: queue failure", notify.call_args.args[1])
            self.server.run_pass()
            self.server.alert_last.result(timeout=1)
            notify.assert_called_once()
            self.assertFalse(self.receipt()["healthy"])
            incidents["alert_get"].return_value = {"issue": None}
            dispatch.side_effect = None
            self.server.run_pass()
            self.server.alert_last.result(timeout=1)
            incidents["alert_clear"].assert_called_once_with(serve.ALERT_KEY)

    def test_rollback_leaves_contract_and_expansion_in_the_database(self):
        self.prepare_checkout({})
        self.write_receipt(starts=3)
        with redirect_stdout(StringIO()):
            with mock.patch.object(db.migrations, "load", return_value=db._CHAIN[:9]):
                self.assertFalse(db.migrate())
            with mock.patch.object(db.migrations, "load", return_value=db._CHAIN[:11]):
                self.assertFalse(db.migrate())
            with closing(db.connect_db(paths.DB_FILE)) as conn:
                applied = conn.execute("SELECT phase FROM schema_migrations WHERE n >= 10 ORDER BY n").fetchall()
                self.assertEqual(applied, [("contract",), ("expand",)])
            contents = paths.DB_FILE.read_bytes()
            self.assertTrue(serve_guard.count_start(self.state, self.checkout, paths.LOG_DIR))
        self.assertEqual(paths.DB_FILE.read_bytes(), contents)
        self.assertEqual(self.receipt()["rollback"], "code_only")
        self.assertEqual(list((self.state / serve_guard.HOLDS).iterdir()),
                         [serve_guard.hold_path(self.state, self.tip)])

    def test_dashboard_readiness_preserves_normal_access_checks(self):
        self.prepare_state()
        address = "127.0.0.1"
        node = tailnet.Node((address,), frozenset({address}), address)
        logins = tailnet.Logins(SimpleNamespace(whois=lambda _: "visitor@example.test"))
        server = SimpleNamespace(node=node, logins=logins, inside_serve=True)

        def connect(conn):
            browser, app = socketpair()
            browser.settimeout(conn.timeout)

            def handle():
                with app:
                    dashboard.Handler(app, (address, 0), server)

            worker = threading.Thread(target=handle, daemon=True)
            worker.start()
            conn.sock = browser
            self.addCleanup(worker.join, 1)

        def get(path, headers):
            with closing(http.client.HTTPConnection(address, 1234, timeout=1)) as conn:
                conn.request("GET", path, headers=headers)
                with conn.getresponse() as response:
                    response.read()
                    return response.status

        tailnet.record_server(node, 1234, tailnet.Front("http", []))
        with (mock.patch.object(http.client.HTTPConnection, "connect", connect),
              mock.patch.object(principals, "for_login", return_value=None) as for_login,
              mock.patch.object(dashboard.api, "meta", return_value={"schema": {"version": db.LATEST}}) as meta):
            self.assertTrue(serve.dashboard_answers())
            for_login.assert_not_called()
            self.assertEqual(get(dashboard.READY_PATH, {}), 403)
            self.assertEqual(get("/api/meta", {dashboard.READY_HEADER: dashboard.READY_TOKEN}), 403)
            meta.assert_called_once()
            meta.return_value = {"schema": {"version": db.NEEDED - 1}}
            self.assertFalse(serve.dashboard_answers())

    def test_guard_imports_only_standard_library(self):
        tree = ast.parse(Path(serve_guard.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)


class ServeDeploymentMergeTests(ServeDeploymentFixture):
    @unittest.skipUnless(sandbox.can_confine(False), "requires macOS sandbox-exec")
    @unittest.skipIf(sandbox.can_confine(False) and sandbox.sandboxed(),
                     "sandbox-exec cannot install a profile inside the test runner's sandbox")
    def test_deploy_fast_forwards_engine_file_in_real_sandbox(self):
        (self.writer / "arete" / "fixture.py").write_text("version = 2\n")
        tip = self.publish("engine update")
        self.git(self.checkout, "fetch", "-q", "origin", "main")
        with self.assertRaisesRegex(RuntimeError, "Operation not permitted"):
            util.git(["merge", "--ff-only", tip], self.checkout, retry=False)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.start)
        self.assertEqual(self.deploy(), tip)
        self.assertEqual((self.checkout / "arete" / "fixture.py").read_text(), "version = 2\n")
        self.assertIn(f"fast forwarded to {tip} (engine update)", self.server.say.call_args.args[0])

    def test_approved_deploy_skips_hooks_and_logs_once(self):
        (self.writer / "arete" / "fixture.py").write_text("version = 2\n")
        tip = self.publish("engine update")
        hooks = self.state.parent / "hooks"
        hooks.mkdir()
        hook = hooks / "post-merge"
        hook.write_text("#!/bin/sh\necho called > hook-ran\n")
        hook.chmod(0o755)
        self.git(self.checkout, "config", "core.hooksPath", str(hooks))

        self.assertEqual(self.deploy(), tip)
        self.assertEqual(self.deploy(), tip)
        self.assertFalse((self.checkout / "hook-ran").exists())
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        checkout = paths.short_path(self.checkout)
        self.assertEqual([call.args[0] for call in self.server.say.call_args_list], [
            f"deploy: {checkout} Git hooks disabled for fast forward to {tip}",
            f"deploy: {checkout} fast forwarded to {tip} (engine update)",
        ])

    def test_approved_deploy_skips_default_hooks_without_logging(self):
        (self.writer / "arete" / "fixture.py").write_text("version = 2\n")
        tip = self.publish("engine update")
        hooks = self.checkout / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "post-merge"
        hook.write_text("#!/bin/sh\necho called > hook-ran\n")
        hook.chmod(0o755)

        with mock.patch.dict("os.environ", {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}):
            self.assertEqual(self.deploy(), tip)

        self.assertFalse((self.checkout / "hook-ran").exists())
        self.server.say.assert_called_once_with(
            f"deploy: {paths.short_path(self.checkout)} fast forwarded to {tip} (engine update)")

    def test_failed_fast_forward_restores_tracked_paths_and_retries(self):
        package = self.writer / "arete"
        (package / "fixture.py").write_text("version = 2\n")
        (package / "new.py").write_text("added = True\n")
        (package / "link").symlink_to("new.py")
        (package / "filtered.txt").write_bytes(b"line one\nline two\n")
        (self.writer / ".gitattributes").write_text("arete/filtered.txt text eol=crlf\n")
        tip = self.publish("engine update")
        untracked = self.checkout / "untracked.txt"
        untracked.write_text("owner's file\n")
        lock = self.checkout / ".git" / "refs" / "heads" / "main.lock"
        lock.touch()
        restore_paths = util.restore_deploy_paths

        def restore(src, base, recovery):
            self.assertEqual((src / "arete" / "fixture.py").read_text(), "version = 2\n")
            self.assertTrue((src / "arete" / "new.py").is_file())
            self.assertTrue((src / "arete" / "link").is_symlink())
            self.assertEqual((src / "arete" / "filtered.txt").read_bytes(), b"line one\r\nline two\r\n")
            return restore_paths(src, base, recovery)

        try:
            with mock.patch.object(util, "restore_deploy_paths", side_effect=restore) as restored:
                self.assertEqual(self.deploy(), self.start)
                restored.assert_called_once()
        finally:
            lock.unlink()
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "?? untracked.txt")
        self.assertEqual((self.checkout / "arete" / "fixture.py").read_text(), "version = 1\n")
        self.assertEqual(untracked.read_text(), "owner's file\n")
        self.assertEqual(self.deploy(), tip)
        messages = [call.args[0] for call in self.server.say.call_args_list]
        restores = [message for message in messages if " recovery for " in message]
        self.assertEqual(len(restores), 1, messages)
        self.assertIn(f"recovery for {tip}", restores[0])
        self.assertIn("main.lock", restores[0])
        self.assertIn("remaining paths: ?? untracked.txt", restores[0])


class ServeDeploymentRecoveryTests(ServeDeploymentFixture):
    def test_interrupted_merge_removes_only_tip_additions_absent_before_the_merge(self):
        additions = ("fresh [1].py", "filtered.txt", "pre-existing.py", "pre-existing-dir.py", "pre-existing-link")
        content = "added = True\n"
        for name in additions:
            (self.writer / "arete" / name).write_text(content)
        (self.writer / "arete" / "fresh-link").symlink_to("missing-target")
        tip = self.publish("tip additions")
        package = self.checkout / "arete"
        (package / "pre-existing.py").write_text(content)
        (package / "pre-existing-link").symlink_to("owner-target")
        occupied = package / "pre-existing-dir.py"
        occupied.mkdir()
        (occupied / "keep").write_text("existing directory content")
        unrelated = package / "unrelated.py"
        unrelated.write_text(content)

        def interrupt(src, checked_tip):
            self.assertEqual((src, checked_tip), (self.checkout, tip))
            (package / additions[0]).write_text(content)
            (package / additions[1]).write_bytes(b"added = True\r\n")
            (package / "fresh-link").symlink_to("missing-target")
            self.assertEqual(self.git(src, "diff", "--cached", "--name-only"), "")
            raise RuntimeError("interrupted before writing the index")

        with mock.patch.object(util, "deploy_fast_forward", side_effect=interrupt):
            self.assertEqual(self.deploy(), self.start)

        self.assertFalse((package / additions[0]).exists())
        self.assertFalse((package / additions[1]).exists())
        self.assertFalse((package / "fresh-link").is_symlink())
        self.assertEqual((package / "pre-existing.py").read_text(), content)
        self.assertEqual((package / "pre-existing-link").readlink(), Path("owner-target"))
        self.assertEqual((occupied / "keep").read_text(), "existing directory content")
        self.assertEqual(unrelated.read_text(), content)
        messages = [call.args[0] for call in self.server.say.call_args_list]
        restores = [message for message in messages if " recovery for " in message]
        self.assertEqual(len(restores), 1, messages)
        self.assertIn(tip, restores[0])
        self.assertIn("interrupted before writing the index", restores[0])
        self.assertIn("?? arete/pre-existing-dir.py/keep", restores[0])
        self.assertIn("?? arete/pre-existing-link", restores[0])

    def test_partial_merge_restores_missing_and_truncated_paths_and_leaves_unrelated_paths(self):
        (self.writer / "arete" / "fixture.py").write_text("version = 2\n")
        (self.writer / "arete" / "shared.py").write_text("shared = 2\n")
        tip = self.publish("engine update")

        def interrupt(src, checked_tip):
            self.assertEqual(checked_tip, tip)
            package = src / "arete"
            (package / "fixture.py").unlink()
            (package / "shared.py").write_bytes(b"sha")
            (package / "other.py").write_text("other = 'staged by owner'\n")
            self.git(src, "add", "arete/other.py")
            (package / "other.py").write_text("other = 'also edited by owner'\n")
            raise RuntimeError("write interrupted")

        with mock.patch.object(util, "deploy_fast_forward", side_effect=interrupt):
            self.assertEqual(self.deploy(), self.start)

        package = self.checkout / "arete"
        self.assertEqual((package / "fixture.py").read_text(), "version = 1\n")
        self.assertEqual((package / "shared.py").read_text(), "shared = 1\n")
        self.assertEqual((package / "other.py").read_text(), "other = 'also edited by owner'\n")
        self.assertEqual(self.git(self.checkout, "show", ":arete/other.py"), "other = 'staged by owner'")
        [recovery] = [call.args[0] for call in self.server.say.call_args_list if " recovery for " in call.args[0]]
        self.assertIn("remaining paths: MM arete/other.py", recovery)
        with mock.patch.object(util, "deploy_fast_forward") as merge:
            self.assertEqual(self.deploy(), self.start)
            merge.assert_not_called()
        self.assertIn(f"left alone for {tip} (dirty)", self.server.say.call_args.args[0])


class DeployProcessTimeoutTests(unittest.TestCase):
    def test_deploy_escalates_and_bounds_reaping_when_child_stays_running(self):
        process = mock.MagicMock(pid=123, stdin=None)
        shutdown = mock.Mock()
        expired = subprocess.TimeoutExpired(["git"], 30)
        process.communicate.side_effect = expired
        process.wait = shutdown.wait
        shutdown.wait.side_effect = subprocess.TimeoutExpired(["git"], 1)

        with (mock.patch.object(subprocess, "Popen", return_value=process),
              mock.patch.object(util.os, "killpg", shutdown.killpg),
              self.assertRaisesRegex(util.DeployStillRunning, "could not confirm Git stopped")):
            util.run_whole_tree(["git"], 30, None, graceful_timeout=True)

        self.assertEqual(shutdown.mock_calls, [
            mock.call.killpg(123, signal.SIGTERM),
            mock.call.wait(timeout=util.DEPLOY_TERMINATION_GRACE),
            mock.call.killpg(123, signal.SIGKILL),
            mock.call.wait(timeout=util.DEPLOY_TERMINATION_GRACE),
        ])
        process.__exit__.assert_not_called()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()


class ServeDeploymentTimeoutTests(ServeDeploymentFixture):
    def test_killed_merge_recovers_partial_writes_and_its_leftover_lock(self):
        self.check_killed_merge_lock(held=True)

    def test_killed_merge_leaves_an_unproven_lock_in_place(self):
        self.check_killed_merge_lock(held=False)

    def check_killed_merge_lock(self, *, held):
        (self.writer / "arete" / "fixture.py").write_text("version = 2\n")
        (self.writer / "arete" / "fresh.py").write_text("added = True\n")
        tip = self.publish("interrupted checkout")
        script = self.state.parent / "interrupted-merge.py"
        ready = self.state.parent / "merge-started"
        script.write_text("""\
import os
import signal
import sys
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
fd = os.open('.git/index.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, b'partial index')
if sys.argv[1] == 'False':
    os.close(fd)
Path('arete/fixture.py').write_text('version = ')
Path('arete/fresh.py').write_text('added = True\\n')
Path(sys.argv[2]).touch()
signal.pause()
""")
        package = self.checkout / "arete"
        lock = self.checkout / ".git" / "index.lock"
        protected_command = sandbox.protected_command
        communicate = subprocess.Popen.communicate
        merges = []

        def substitute_merge(cmd, *args, **kwargs):
            if "--ff-only" in cmd:
                cmd = [sys.executable, str(script), str(held), str(ready)]
            return protected_command(cmd, *args, **kwargs)

        def expire_after_writes(proc, input=None, timeout=None):
            if str(script) in proc.args:
                merges.append(proc)
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                self.assertEqual(lock.read_bytes(), b"partial index")
                self.assertEqual((package / "fixture.py").read_text(), "version = ")
                timeout = 0
            return communicate(proc, input=input, timeout=timeout)

        with (mock.patch.object(sandbox, "protected_command", substitute_merge),
              mock.patch.object(subprocess.Popen, "communicate", expire_after_writes)):
            self.assertEqual(self.deploy(), self.start)

        [merge] = merges
        self.assertEqual(merge.returncode, -signal.SIGKILL)
        [recovery] = [call.args[0] for call in self.server.say.call_args_list if " recovery for " in call.args[0]]
        self.assertIn(f"recovery for {tip}", recovery)
        self.assertIn("timed out", recovery)
        if held:
            self.assertFalse(lock.exists())
            self.assertEqual((package / "fixture.py").read_text(), "version = 1\n")
            self.assertFalse((package / "fresh.py").exists())
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assertNotIn("remaining paths", recovery)
        else:
            self.assertEqual(lock.read_bytes(), b"partial index")
            self.assertEqual((package / "fixture.py").read_text(), "version = ")
            self.assertIn("Git index is locked", recovery)

    def test_unconfirmed_merge_shutdown_skips_recovery(self):
        (self.writer / "arete" / "fixture.py").write_text("version = 2\n")
        tip = self.publish("engine update")
        error = util.DeployStillRunning("could not confirm Git stopped after deploy timeout")
        with (mock.patch.object(util, "deploy_fast_forward", side_effect=error),
              mock.patch.object(self.server, "restore_deploy") as restore):
            self.assertEqual(self.deploy(), self.start)

        restore.assert_not_called()
        self.assertIn(f"left alone for {tip} ({error})", self.server.say.call_args.args[0])

    def test_timed_out_merge_releases_its_lock_and_retries(self):
        (self.writer / "arete" / "fixture.py").write_text("version = 2\n")
        (self.writer / "arete" / "fresh.py").write_text("added = True\n")
        (self.writer / ".gitattributes").write_text("z-delayed.txt filter=delay\n")
        (self.writer / "z-delayed.txt").write_text("tip content\n")
        tip = self.publish("delayed checkout")
        lock = self.checkout / ".git" / "index.lock"
        ready = self.state.parent / "filter-started"
        filter_code = "import pathlib, signal, sys; pathlib.Path(sys.argv[1]).touch(); signal.pause()"
        self.git(self.checkout, "config", "filter.delay.smudge", shlex.join([sys.executable, "-c", filter_code,
                                                                             str(ready)]))
        self.git(self.checkout, "config", "filter.delay.required", "true")
        self.git(self.checkout, "config", "checkout.workers", "1")
        communicate, wait = subprocess.Popen.communicate, subprocess.Popen.wait
        interrupted = False
        cleanup_waits = []

        def expire_while_git_holds_lock(proc, input=None, timeout=None):
            nonlocal interrupted
            if "--ff-only" in proc.args:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                self.assertTrue(lock.exists())
                self.assertTrue((self.checkout / "arete" / "fresh.py").is_file())
                interrupted = True
                timeout = 0
            return communicate(proc, input=input, timeout=timeout)

        def observe_cleanup(proc, timeout=None):
            if interrupted and "--ff-only" in proc.args:
                cleanup_waits.append(timeout)
            return wait(proc, timeout=timeout)

        with (mock.patch.object(subprocess.Popen, "communicate", expire_while_git_holds_lock),
              mock.patch.object(subprocess.Popen, "wait", observe_cleanup)):
            self.assertEqual(self.deploy(), self.start)
        self.assertEqual(cleanup_waits, [util.DEPLOY_TERMINATION_GRACE] * 2)
        self.assertFalse(lock.exists())
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        [recovery] = [call.args[0] for call in self.server.say.call_args_list if " recovery for " in call.args[0]]
        self.assertIn("timed out", recovery)

        lock.write_text("another Git process")
        self.assertEqual(self.deploy(), self.start)
        self.assertEqual(lock.read_text(), "another Git process")
        self.assertIn("Git index is locked", self.server.say.call_args_list[-2].args[0])
        lock.unlink()
        self.git(self.checkout, "config", "filter.delay.smudge", "cat")
        self.assertEqual(self.deploy(), tip)


class CommandConfinementTests(unittest.TestCase):
    def test_guard_denies_reads_from_eval_skill_folder(self):
        root = paths.PACKAGE_DIR.parent / "fixture"
        state = root / "state"
        cwd = root / "eval-checkout"
        skill = state / "eval-skills" / "other-arm" / "skills" / "example" / "helper.md"
        payload = {"cwd": str(cwd), "tool_name": "Read", "tool_input": {"file_path": str(skill)}}
        output = StringIO()
        with (mock.patch.object(paths, "STATE_DIR", state),
              mock.patch.object(sys, "stdin", StringIO(json.dumps(payload))), redirect_stdout(output)):
            self.assertEqual(guard.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_only_deploy_merge_drops_the_engine_write_deny(self):
        src = paths.PACKAGE_DIR.parent
        tip = "a" * 40
        recovery = util.DeployRecovery("b" * 40, (b"arete/util.py",), (b"arete/added.py",), (b"arete/added.py",))
        with (mock.patch.object(sandbox, "can_confine", return_value=True),
              mock.patch.object(sandbox, "sandboxed", return_value=False),
              mock.patch.object(util, "_deploy_index_lock", return_value=src / "index.lock"),
              mock.patch.object(util, "_deploy_path_mode", return_value=stat.S_IFREG),
              mock.patch.object(util, "run_whole_tree", return_value=subprocess.CompletedProcess([], 0, "", ""))
              as run):
            util.sh(["true"], cwd=src)
            util.git(["status"], src)
            util.deploy_fast_forward(src, tip)
            util._restore_deploy_tracked(src, recovery)
            util._remove_deploy_additions(src, recovery)
            worker = sandbox.write_confinement(src, [])

        shell, git, deploy, restore, remove = (call.args[0] for call in run.call_args_list)
        engine = f"ENGINE={sandbox.real_path(paths.PACKAGE_DIR)}"
        deny_engine = '(deny file-write* (subpath (param "ENGINE")))'
        for command in (shell, git, worker):
            self.assertEqual(command[0], "/usr/bin/sandbox-exec")
            self.assertIn(engine, command)
            self.assertIn(f"RUNTIME={sandbox.real_path(sys.prefix)}", command)
            profile = command[command.index("-p") + 1]
            self.assertIn(deny_engine, profile)
            self.assertIn('(deny file-write* (subpath (param "RUNTIME")))', profile)
        profile_index = git.index("-p") + 1
        engine_index = git.index(engine)
        self.assertEqual(deploy[:deploy.index("-p") + 2], [
            *git[:engine_index - 1], *git[engine_index + 1:profile_index], git[profile_index].replace(deny_engine, ""),
        ])
        self.assertEqual(deploy[-6:], ["merge", "-q", "--ff-only", "--no-autostash", "--no-overwrite-ignore", tip])
        self.assertIn("core.hooksPath=/dev/null", deploy)
        recovery_profile = git[profile_index].replace(
            deny_engine, '(deny file-write* (require-all (subpath (param "ENGINE")) '
                         '(require-not (literal (param "RESTORE0")))))',
        )
        for command, filename in ((restore, "util.py"), (remove, "added.py")):
            self.assertIn(engine, command)
            exception = f"RESTORE0={sandbox.real_path(paths.PACKAGE_DIR) / filename}"
            self.assertEqual([arg for arg in command if arg.startswith("RESTORE")], [exception])
            self.assertEqual(command[command.index("-p") + 1], recovery_profile)


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
                                env=stack_env({**env, "PYTHONPATH": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}),
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("broken deployment fixture", result.stderr)
        self.assertIn(
            f"last recorded serve heartbeat at 2026-11-21T00:00:00Z, running code {running}, checkout HEAD {checkout}",
            result.stderr.splitlines(),
        )
        self.assertFalse((state_dir / "arete.db").exists())

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

    def test_fault_cli_guidance_only_for_the_wrapped_dashboard(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.run.dir = root
        self.run.agent.return_value.structured["checks"] = [
            {"what": "Inspect the fixture", "how": "Open the app", "saw": "The fixture opened", "passed": True},
        ]
        wrapper = root / "verification-runtime/harness/sandbox"
        wrapped = "sandbox uv run arete dashboard --port {port}"
        self.enterContext(mock.patch.object(verify, "serving", return_value=nullcontext("http://127.0.0.1:4242")))
        self.enterContext(mock.patch.object(verify.browser, "free_port", return_value=4242))

        for start in ("uv run arete dashboard --port {port}", "./sandbox uv run arete dashboard", wrapped,
                      f"{wrapper} uv run arete dashboard --port {{port}}"):
            with self.subTest(start=start):
                self.work.layer.verify = config.Verify(start=start, allow_bash=[f"{wrapper} *"])
                self.assertEqual(self.verify().verdict, "pass")
                prompt = self.run.agent.call_args.args[1]
                if start in (wrapped, f"{wrapper} uv run arete dashboard --port {{port}}"):
                    for command in ("arm", "drop-streams", "event", "clear"):
                        self.assertIn(f"{wrapper} uv run arete verify-fault {command}", prompt)
                    self.assertIn("Do not use page scripts or write directly to the store", prompt)
                else:
                    self.assertNotIn("verify-fault", prompt)

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
        item = {"run_id": "build", "pr": 646}
        with tempfile.NamedTemporaryFile(suffix=".md") as saved:
            path = Path(saved.name)
            path.write_text(report)
            with mock.patch.object(mergecheck, "reports", return_value=("build", path, None)):
                classic = mergecheck.report_steps(item)
            recorded = [check for check in result.checks if check.get("driver") is True]
            with mock.patch.object(mergecheck, "reports", return_value=("build", path, recorded)):
                steps = mergecheck.report_steps(item)

        self.assertEqual([step.goal(n) for n, step in enumerate(classic, 1)], [
            "1. [build step 1] Inspect the catalog: echo catalog",
            "2. [build step 2] journey:verifier-prompt-inputs: echo prompt",
        ])
        self.assertEqual(steps, [
            *classic, mergecheck.Step("build", 3, "journey:verifier-prompt-inputs", "", 646, driver=True),
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


class MergeCheckJourneyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.runs = self.root / "runs"
        self.report = self.runs / "verified" / "verify.md"
        self.report.parent.mkdir(parents=True)
        self.item = {"run_id": "verified", "pr": 678, "key": "fixture/pull/678", "repo": "fixture", "merged_at": "now"}
        self.provenance = {}
        self.enterContext(mock.patch.object(paths, "RUNS_DIR", self.runs))
        self.enterContext(mock.patch.object(factory, "chain", side_effect=self.history))

    def history(self, run_id):
        if run_id not in self.provenance:
            return [[{"run": run_id}]]
        return [[{"run": run_id, "event": "step_end", "step": "verify", "result": "pass",
                  "data": {"verified": self.provenance[run_id]}}]]

    def save_report(self, path, verified):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(verified.markdown())
        self.provenance[path.parent.name] = asdict(replace(verified, saved=str(path)))
        recorded = [check for check in verified.checks if check.get("driver") is True]
        path.with_name(f"{path.stem}-probes.json").write_text(json.dumps({"checks": recorded}))

    def prepare_replay(self, exit_code, *, output="merged checkout output", later_drivers=None, grade=None,
                       cleanup_error=False, probe_exit=None):
        exit_codes = {} if exit_code is None else {"checkout": exit_code, **(later_drivers or {})}
        ordinary = {"what": "Inspect the catalog", "how": "echo catalog", "saw": "catalog", "passed": True}
        self.save_report(self.report, verify.Verified("pass", checks=[
            {"what": f"journey:{name}", "how": "exit 99", "saw": "old output", "passed": True, "driver": True}
            for name in exit_codes
        ] or [ordinary]))
        self.items = [self.item]
        if grade is not None and exit_codes:
            self.items.append({**self.item, "run_id": "ordinary", "pr": 679, "key": "fixture/pull/679"})
            self.save_report(self.runs / "ordinary" / "verify.md", verify.Verified("pass", checks=[ordinary]))
        repo = config.Repo("fixture", self.root, pipeline=True)
        layer = config.RepoLayer(self.root, base_branch="main", verify=config.Verify(
            url="", drivers={name: f"cat product.txt; exit {code} # {name}" for name, code in exit_codes.items()},
            feature_map={name: journeys.Journey(("Read product output",), "Product is ready", name)
                         for name in exit_codes},
        ))
        self.cfg = config.Config(repos=[repo])
        self.layer = layer
        self.worktree = None
        self.enterContext(mock.patch.object(config, "load_layer", return_value=layer))
        self.enterContext(mock.patch.object(mergecheck, "changed", return_value={"product.txt"}))
        self.enterContext(mock.patch.object(paths, "WORKTREES_DIR", self.root))
        self.enterContext(mock.patch.object(mergecheck, "merged", return_value=self.items))
        self.enterContext(mock.patch.object(mergecheck, "loop_dir", return_value=self.root / "loop"))
        self.enterContext(mock.patch.object(mergecheck.record, "new_id", side_effect=["replay", "retry", "last"]))
        self.enterContext(mock.patch.object(mergecheck.record, "start"))
        self.settle = self.enterContext(mock.patch.object(mergecheck.record, "settle"))

        def git(args, cwd):
            if args[:2] == ["rev-parse", "--verify"]:
                self.assertEqual(args[2], "origin/main^{commit}")
                return "merged-sha"
            if args[:2] == ["worktree", "add"]:
                self.assertEqual((args[2], args[4]), ("--detach", "merged-sha"))
                self.worktree = Path(args[3])
                self.worktree.mkdir()
            if args[:2] == ["worktree", "remove"] and cleanup_error:
                raise RuntimeError("worktree removal failed")
            return ""

        def spawn(command, *, actor_confined, **kwargs):
            self.assertTrue(actor_confined)
            self.assertEqual(kwargs["cwd"], self.worktree)
            code = int(command[-1].split(" # ", 1)[0].rsplit("exit ", 1)[1])
            return mock.Mock(returncode=code, communicate=mock.Mock(return_value=(output + "\n", "")))

        def agent(run, *_args, **_kwargs):
            run.invoked = True
            return SimpleNamespace(ok=True, structured=grade)

        self.run_dir = self.runs / "replay-fixture"
        self.enterContext(mock.patch.object(mergecheck.util, "git", side_effect=git))
        self.enterContext(mock.patch.object(mergecheck.verifier_suites, "held", return_value=False))
        self.enterContext(mock.patch.object(pipeline, "untracked_by_dir", return_value={}))
        self.enterContext(mock.patch.object(pipeline, "restore_worktrees"))
        self.enterContext(mock.patch.object(verify.scratch, "make",
                                           return_value=SimpleNamespace(root=str(self.root / "scratch"))))
        self.enterContext(mock.patch.object(verify.scratch, "end", return_value=("", [], [])))
        self.enterContext(mock.patch.object(util, "stop_group"))
        self.enterContext(mock.patch.object(verify.scratch, "service_command",
                                           side_effect=lambda _, command: ["/bin/sh", "-c", command]))
        self.spawn = self.enterContext(mock.patch.object(verify.scratch, "spawn", side_effect=spawn))
        self.agent = self.enterContext(mock.patch.object(mergecheck.CheckRun, "agent", autospec=True, side_effect=agent))
        if probe_exit is not None:
            layer.verify.probes["ready"] = f"exit {probe_exit}"

    def replay_driver(self, exit_code, **kwargs):
        self.prepare_replay(exit_code, **kwargs)
        repo = self.cfg.repo("fixture")
        replay = mergecheck.plan(self.cfg, repo, self.items, self.items, self.run_dir.name)
        return replay, mergecheck.execute(self.cfg, replay, self.run_dir.name)

    def consume(self, events):
        with (redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr,
              mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(mergecheck.sys, "stdin", StringIO(json.dumps(events)))):
            code = mergecheck.command(SimpleNamespace(cmd="merge-check", repo="fixture", dry_run=False))
        return code, stdout.getvalue(), stderr.getvalue()

    def checked(self):
        return json.loads((self.root / "loop/.state/checked.json").read_text())["fixture"]

    def test_classic_driver_only_report_holds_as_on_main_even_with_a_probe_artifact(self):
        self.prepare_replay(1)
        self.provenance.pop("verified")

        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("no usable, uniquely numbered checks", stderr)
        self.assertEqual(self.checked(), [])
        self.spawn.assert_not_called()
        self.agent.assert_not_called()

    def test_classic_report_with_a_driver_row_replays_the_ordinary_step(self):
        self.prepare_replay(1, grade={
            "verdict": "pass", "summary": "Catalog checked.",
            "checks": [{"what": "Step 1: Inspect the catalog", "how": "echo catalog", "saw": "catalog", "passed": True}],
        })
        self.provenance.clear()
        self.report.write_text(self.report.read_text() + "\n" +
                               (self.runs / "ordinary" / "verify.md").read_text())
        self.items.pop()

        code, stdout, _ = self.consume(self.items)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"value": 1})
        self.assertEqual(self.checked(), [self.item["key"]])
        self.spawn.assert_not_called()
        self.agent.assert_called_once()

    def test_driver_only_failure_names_the_journey_and_its_observation(self):
        self.prepare_replay(1)

        code, stdout, _ = self.consume(self.items)

        self.assertEqual(code, 0)
        finding, metric = map(json.loads, stdout.splitlines())
        self.assertIn("journey:checkout", finding["summary"])
        self.assertIn("local merge-check record for replay-fixture", finding["summary"])
        self.assertEqual(metric, {"value": 0})
        self.assertEqual(self.checked(), [self.item["key"]])
        self.assertEqual(json.loads((self.run_dir / "driver-failures.json").read_text()), {
            "1": {"what": "journey:checkout", "saw": "exit 1: merged checkout output\n"},
        })
        self.spawn.assert_called_once()
        self.agent.assert_not_called()

    def test_failure_block_and_pending_driver_preserve_the_regression_and_retry_the_batch(self):
        self.prepare_replay(1, later_drivers={"inventory": 127, "shipping": 0})
        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, mergecheck.INCOMPLETE_EXIT)
        self.assertEqual(self.checked(), [])
        result = json.loads((self.run_dir / "result.json").read_text())
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual([(c["what"], c.get("cause")) for c in result["checks"]], [
            ("journey:checkout", "change"), ("journey:inventory", "environment"),
            ("journey:shipping", None),
        ])
        finding = json.loads(stdout)
        self.assertIn("journey:checkout", finding["summary"])
        self.assertIn("journey:inventory", stderr)
        self.assertNotIn("merged checkout output", stdout + stderr)
        self.assertEqual(json.loads((self.run_dir / "driver-failures.json").read_text()), {
            "1": {"what": "journey:checkout", "saw": "exit 1: merged checkout output\n"},
        })
        self.assertEqual(self.spawn.call_count, 3)
        self.assertIn("journey:checkout: exit 1: merged checkout output", self.settle.call_args.kwargs["summary"])
        self.agent.assert_not_called()

        loop = SimpleNamespace(name="merge-check", command=["arete", "merge-check"], repo="", dir=self.root,
                               timeout_minutes=30, emits=[{}])
        run = SimpleNamespace(loop=loop, id="loop", trial=False, cfg=self.cfg, events=self.items,
                              device=SimpleNamespace(remote=False), log=mock.Mock(), log_file=self.root / "loop.log")
        with (mock.patch.object(loop_run, "run_command", return_value=(code, stdout + stderr)),
              mock.patch.object(loop_run, "new_findings", side_effect=lambda _, findings: findings) as results,
              mock.patch.object(loop_run.delivery, "deliver") as deliver,
              mock.patch.object(loop_run, "hand_on")):
            status, summary = loop_run.command_loop(run)
        self.assertEqual(status, "failed")
        self.assertIn("journey:inventory", summary)
        self.assertEqual(results.call_args.args[1], [finding])
        self.assertEqual(deliver.call_args.args[2], [finding])
        self.assertEqual(deliver.call_args.args[3]["summary"], "")

        self.layer.verify.drivers["inventory"] = "cat product.txt; exit 0 # inventory"
        code, stdout, _ = self.consume([])
        self.assertEqual(code, 0)
        self.assertEqual(self.checked(), [self.item["key"]])
        self.assertEqual(json.loads(stdout.splitlines()[0])["fingerprint"], finding["fingerprint"])
        self.assertEqual(json.loads(stdout.splitlines()[1]), {"value": 0})
        self.assertEqual(self.spawn.call_count, 6)
        self.assertTrue(self.spawn.call_args.args[0][-1].endswith("# shipping"))

    def test_environment_block_without_a_driver_failure_holds_replay(self):
        marker = "synthetic private application output"
        _, replay = self.replay_driver(127, output=marker)

        self.assertIn("journey:checkout", replay.blocked_by)
        self.assertNotIn(marker, replay.blocked_by)
        self.assertEqual(replay.failed, [])
        result = json.loads((self.run_dir / "result.json").read_text())
        self.assertEqual(result["verdict"], "blocked")
        self.assertIn(marker, result["blocked_by"])
        self.assertFalse((self.run_dir / "driver-failures.json").exists())
        self.agent.assert_not_called()

    def test_replay_driver_without_trusted_scratch_holds_without_spawning(self):
        self.prepare_replay(0)
        with mock.patch.object(verify.scratch, "make", side_effect=sandbox.ConfinementUnavailable("unavailable")):
            code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("journey:checkout", stderr)
        self.assertEqual(self.checked(), [])
        self.spawn.assert_not_called()
        self.agent.assert_not_called()

    def test_later_driver_failure_wins_over_the_first_driver_exit_127(self):
        replay, result = self.replay_driver(127, later_drivers={"inventory": 1})

        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(result.failed, [2])
        self.assertIn("journey:inventory: exit 1:", result.driver_reason)
        self.assertIn("journey:inventory", mergecheck.finding(replay, result.failed)["summary"])
        self.assertEqual(json.loads((self.run_dir / "result.json").read_text())["verdict"], "fail")

    def test_later_driver_failure_is_reported_after_a_communicate_timeout(self):
        self.prepare_replay(0, later_drivers={"inventory": 1})
        elapsed = 0.0

        def time_out(*, timeout):
            nonlocal elapsed
            elapsed += timeout
            raise subprocess.TimeoutExpired("checkout", timeout)

        checkout = mock.Mock(communicate=mock.Mock(side_effect=time_out))
        inventory = mock.Mock(returncode=1, communicate=mock.Mock(return_value=("inventory missing\n", "")))
        self.spawn.side_effect = [checkout, inventory]
        with mock.patch.object(verify, "time", SimpleNamespace(monotonic=lambda: elapsed)):
            code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, mergecheck.INCOMPLETE_EXIT)
        finding = json.loads(stdout)
        self.assertIn("journey:inventory", finding["summary"])
        self.assertNotIn("journey:checkout", finding["summary"])
        self.assertIn("journey:checkout", stderr)
        self.assertNotIn("inventory missing", stdout + stderr)
        self.assertEqual(self.checked(), [])
        for proc in (checkout, inventory):
            proc.communicate.assert_called_once_with(timeout=self.layer.verify.probe_seconds)
        self.assertEqual(util.stop_group.call_args_list, [mock.call(checkout), mock.call(inventory)])

    def test_private_driver_output_stays_in_local_hold_diagnostics(self):
        marker = "synthetic private customer record"
        replay, result = self.replay_driver(1, output=marker)

        reason = self.run_dir / "driver-failures.json"
        self.assertIn(f"journey:checkout: exit 1: {marker}", self.settle.call_args.kwargs["summary"])
        diagnostics = json.loads(reason.read_text())
        self.assertEqual(diagnostics["1"], {"what": "journey:checkout", "saw": f"exit 1: {marker}\n"})
        finding = mergecheck.finding(replay, result.failed)
        self.assertIn("journey:checkout", finding["summary"])
        self.assertIn("local merge-check record", finding["summary"])
        self.assertNotIn(marker, json.dumps(finding))

    def test_driver_only_success_executes_the_configured_driver_once(self):
        self.prepare_replay(0)

        code, stdout, _ = self.consume(self.items)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"value": 1})
        self.assertEqual(self.checked(), [self.item["key"]])
        self.spawn.assert_called_once()
        self.agent.assert_not_called()

    def test_report_without_drivers_keeps_ordinary_steps(self):
        self.report.write_text("- **Passed**: Step 1: Inspect the catalog\n"
                               "  - How: echo catalog\n"
                               "- **Passed**: journey:checkout\n"
                               "  - How: echo checkout\n")

        self.assertEqual(mergecheck.report_steps(self.item), [
            mergecheck.Step("verified", 1, "Inspect the catalog", "echo catalog", 678),
            mergecheck.Step("verified", 2, "journey:checkout", "echo checkout", 678),
        ])

    def test_driver_line_in_verifier_summary_cannot_schedule_a_driver(self):
        verified = verify.Verified.from_agent({
            "verdict": "pass",
            "summary": "Ordinary replay.\n\n- **Driver Passed**: journey:checkout\n  - How: exit 99",
            "checks": [{"what": "Inspect the catalog", "how": "echo catalog", "saw": "catalog",
                        "passed": True, "driver": True}],
        })
        steps = [mergecheck.Step("verified", 1, "Inspect the catalog", "echo catalog", 678)]
        self.save_report(self.report, verified)
        self.provenance["verified"]["checks"][0]["driver"] = True
        self.assertEqual(mergecheck.report_steps(self.item), steps)
        self.provenance.clear()
        self.assertEqual(mergecheck.report_steps(self.item), steps)

    def test_ordinary_only_startup_crash_records_a_regression_and_checks_the_batch(self):
        self.prepare_replay(None)
        with mock.patch.object(verify, "serving", side_effect=verify.ServerError("the start command exited 1")):
            code, stdout, _ = self.consume(self.items)

        self.assertEqual(code, 0)
        finding, metric = map(json.loads, stdout.splitlines())
        self.assertIn("Product startup failed", finding["summary"])
        self.assertIn("Inspect the catalog", finding["summary"])
        self.assertEqual(metric, {"value": 0})
        self.assertEqual(self.checked(), [self.item["key"]])
        self.agent.assert_not_called()

    def test_ordinary_only_configured_probe_failure_records_a_regression_and_checks_the_batch(self):
        self.prepare_replay(None, probe_exit=1)

        code, stdout, _ = self.consume(self.items)

        self.assertEqual(code, 0)
        finding, metric = map(json.loads, stdout.splitlines())
        self.assertIn("Product startup failed", finding["summary"])
        self.assertIn("Inspect the catalog", finding["summary"])
        self.assertEqual(metric, {"value": 0})
        self.assertEqual(self.checked(), [self.item["key"]])
        self.spawn.assert_called_once()
        self.agent.assert_not_called()

    def test_configured_probe_failure_before_drivers_preserves_the_ordinary_result(self):
        self.prepare_replay(127, probe_exit=1, grade={})

        code, stdout, _ = self.consume(self.items)

        self.assertEqual(code, 0)
        finding, metric = map(json.loads, stdout.splitlines())
        self.assertIn("Product startup failed", finding["summary"])
        self.assertIn("Inspect the catalog", finding["summary"])
        self.assertNotIn("journey:checkout", finding["summary"])
        self.assertEqual(metric, {"value": 0})
        self.assertEqual(self.checked(), [item["key"] for item in self.items])
        self.assertEqual([call.args[0][-1] for call in self.spawn.call_args_list], ["exit 1", "exit 1"])
        self.agent.assert_not_called()

    def test_worktree_removal_failure_stops_publication_and_leaves_the_batch_unchecked(self):
        self.prepare_replay(None, cleanup_error=True, grade={
            "verdict": "fail", "summary": "Catalog regressed.",
            "checks": [{"what": "Step 1: Inspect the catalog", "how": "echo catalog", "saw": "empty", "passed": False}],
        })

        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("worktree removal failed", stderr)
        self.assertEqual(self.checked(), [])
        self.agent.assert_called_once()

    def test_malformed_ordinary_manifest_holds_without_a_finding_and_can_retry(self):
        check = {"what": "Step 1: Inspect the catalog", "how": "echo catalog", "saw": "empty", "passed": False}
        grade = {"verdict": "fail", "summary": "Catalog regressed.", "checks": []}
        self.prepare_replay(None, grade=grade)

        grade["checks"] = [check, {**check, "what": "Malformed"}]
        code, stdout, stderr = self.consume(self.items)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("unknown or invalid step", stderr)
        self.assertEqual(self.checked(), [])

        grade.update(verdict="pass", checks=[{**check, "passed": True}])
        code, stdout, _ = self.consume([])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"value": 1})
        self.assertEqual(self.checked(), [self.item["key"]])

    def test_driver_failure_and_independent_ordinary_failure_are_both_reported(self):
        marker = "synthetic private order details"
        self.prepare_replay(1, output=marker, grade={
            "verdict": "fail", "summary": "Catalog regressed.",
            "checks": [{"what": "Step 1: Inspect the catalog", "how": "echo catalog", "saw": "empty",
                        "passed": False, "cause": "change"}],
        })

        code, stdout, _ = self.consume(self.items)

        self.assertEqual(code, 0)
        self.assertEqual(self.checked(), [item["key"] for item in self.items])
        self.agent.assert_called_once()
        prompt = self.agent.call_args.args[2]
        self.assertIn("1. [ordinary step 1] Inspect the catalog: echo catalog", prompt)
        self.assertNotIn(marker, prompt + stdout)
        finding, metric = [json.loads(line) for line in stdout.splitlines()]
        self.assertIn("journey:checkout", finding["summary"])
        self.assertIn("Inspect the catalog", finding["summary"])
        self.assertEqual(metric, {"value": 0})

    def test_driver_block_still_reports_the_ordinary_failure_and_leaves_the_batch_unchecked(self):
        self.prepare_replay(127, grade={
            "verdict": "fail", "summary": "Catalog regressed.",
            "checks": [{"what": "Step 1: Inspect the catalog", "how": "echo catalog", "saw": "empty", "passed": False}],
        })
        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, mergecheck.INCOMPLETE_EXIT)
        finding, metric = map(json.loads, stdout.splitlines())
        self.assertIn("Inspect the catalog", finding["summary"])
        self.assertNotIn("journey:checkout", finding["summary"])
        self.assertIn("journey:checkout", stderr)
        self.assertEqual(metric, {"value": 0})
        self.assertEqual(self.checked(), [])
        self.spawn.assert_called_once()
        self.agent.assert_called_once()

    def test_driver_and_ordinary_grader_each_see_their_own_seeded_state(self):
        self.prepare_replay(0, grade={
            "verdict": "pass", "summary": "Catalog checked.",
            "checks": [{"what": "Step 1: Inspect the catalog", "how": "update catalog", "saw": "updated",
                        "passed": True}],
        })
        self.layer.setup.append("seed catalog")
        self.layer.verify.start = "serve catalog {scratch}"
        self.layer.verify.url = "http://localhost:{port}"
        phases, services = [], []
        grade, spawn = self.agent.side_effect, self.spawn.side_effect

        def setup(command, *_args, cwd, **_kwargs):
            self.assertEqual(command, "seed catalog")
            (cwd / "product.txt").write_text("seeded product")
            return SimpleNamespace(returncode=0)

        @contextmanager
        def serving(configured, cwd, *_args, attempt, **_kwargs):
            scratch_root = Path(attempt.root)
            self.assertEqual(configured.start, f"serve catalog {scratch_root}")
            (scratch_root / "catalog.txt").write_text("seeded scratch")
            services.append((cwd, scratch_root, configured.url))
            phases.append("start")
            yield configured.url
            phases.append("stop")

        def mutate_catalog(cwd, scratch_root, phase):
            phases.append(phase)
            state = [(cwd / "product.txt", "seeded product"), (scratch_root / "catalog.txt", "seeded scratch")]
            intact = all(path.read_text() == seed for path, seed in state)
            for path, _ in state:
                path.write_text(f"mutated by {phase}")
            return intact

        def mutate_as_grader(run, step, prompt, cwd, *, scratch, **kwargs):
            graded = grade(run, step, prompt, cwd, scratch=scratch, **kwargs)
            graded.structured["checks"][0]["passed"] = mutate_catalog(cwd, Path(scratch.root), "grade")
            return graded

        def mutate_as_driver(command, **kwargs):
            proc = spawn(command, **kwargs)
            scratch_root = Path(kwargs["env"][verify.scratch.ENV_NAME])
            proc.returncode = 0 if mutate_catalog(kwargs["cwd"], scratch_root, "driver") else 1
            return proc

        def scratch_attempt(_run, step, cwd, _folder):
            root = cwd / f"scratch-{step}"
            root.mkdir()
            return SimpleNamespace(root=str(root))

        self.agent.side_effect = mutate_as_grader
        self.spawn.side_effect = mutate_as_driver
        with (mock.patch.object(pipeline.environment, "sh", side_effect=setup),
              mock.patch.object(verify.scratch, "make", side_effect=scratch_attempt),
              mock.patch.object(verify.browser, "free_port", side_effect=[12000, 12001]),
              mock.patch.object(verify, "serving", side_effect=serving)):
            code, stdout, _ = self.consume(self.items)

        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line) for line in stdout.splitlines()], [{"value": 1}])
        self.assertEqual(self.checked(), [item["key"] for item in self.items])
        self.assertEqual(phases, ["start", "driver", "stop", "start", "grade", "stop"])
        for driver_value, grader_value in zip(*services, strict=True):
            self.assertNotEqual(driver_value, grader_value)

    def test_driver_failure_and_hold_with_ordinary_pass_emit_a_failing_metric(self):
        marker = "private driver diagnostic"
        self.prepare_replay(1, later_drivers={"inventory": 127}, output=marker, grade={
            "verdict": "pass", "summary": "Catalog checked.",
            "checks": [{"what": "Step 1: Inspect the catalog", "how": "echo catalog", "saw": "catalog",
                        "passed": True}],
        })

        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, mergecheck.INCOMPLETE_EXIT)
        finding, metric = map(json.loads, stdout.splitlines())
        self.assertIn("journey:checkout", finding["summary"])
        self.assertNotIn("Inspect the catalog", finding["summary"])
        self.assertIn("journey:inventory", stderr)
        self.assertNotIn(marker, stdout + stderr)
        self.assertEqual(metric, {"value": 0})
        self.assertEqual(self.checked(), [])

    def test_driver_failure_is_reported_while_ordinary_grader_holds_for_environment(self):
        marker = "synthetic private checkout failure"
        hold = "The catalog service is unavailable."
        self.prepare_replay(1, output=marker, grade={
            "verdict": "blocked", "summary": "Catalog could not be checked.", "blocked_by": hold, "checks": [],
        })

        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, mergecheck.INCOMPLETE_EXIT)
        finding = json.loads(stdout)
        self.assertIn("journey:checkout", finding["summary"])
        self.assertIn("local merge-check record for replay-fixture", finding["summary"])
        self.assertIn(hold, stderr)
        self.assertNotIn(marker, stdout + stderr)
        self.assertEqual(self.checked(), [])
        self.assertEqual(json.loads((self.run_dir / "driver-failures.json").read_text()), {
            "2": {"what": "journey:checkout", "saw": f"exit 1: {marker}\n"},
        })
        local_summary = self.settle.call_args.kwargs["summary"]
        self.assertIn(hold, local_summary)
        self.assertIn(marker, local_summary)

    def test_ordinary_and_driver_holds_are_both_recorded_locally_without_a_metric(self):
        marker = "private driver startup details"
        hold = "The catalog service is unavailable."
        self.prepare_replay(127, output=marker, grade={
            "verdict": "blocked", "summary": "Catalog could not be checked.", "blocked_by": hold, "checks": [],
        })

        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(hold, stderr)
        self.assertNotIn(marker, stderr)
        self.assertEqual(self.checked(), [])
        local_summary = self.settle.call_args.kwargs["summary"]
        self.assertIn(hold, local_summary)
        self.assertIn(f"journey:checkout: exit 127: {marker}", local_summary)

    def test_malformed_manifest_row_prevents_publication_even_when_both_kinds_of_check_fail(self):
        check = {"what": "Step 1: Inspect the catalog", "how": "echo catalog", "saw": "empty", "passed": False}
        self.prepare_replay(1, grade={"verdict": "fail", "summary": "Catalog regressed.",
                                      "checks": [check, {**check, "what": "Malformed"}]})

        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("unknown or invalid step", stderr)
        self.assertEqual(self.checked(), [])
        self.spawn.assert_called_once()

    def test_driver_failure_with_an_unaccounted_ordinary_step_holds_without_a_finding(self):
        check = {"what": "Inspect the catalog", "how": "echo catalog", "saw": "catalog", "passed": True}
        self.prepare_replay(1, grade={"verdict": "fail", "summary": "Catalog failed.",
                                      "checks": [{**check, "what": "Step 1: Inspect the catalog", "passed": False}]})
        self.save_report(self.runs / "ordinary" / "verify.md", verify.Verified("pass", checks=[
            check, {**check, "what": "Inspect inventory", "how": "echo inventory", "saw": "inventory"},
        ]))

        code, stdout, stderr = self.consume(self.items)

        self.assertEqual(code, 1)
        self.assertEqual(self.checked(), [])
        self.assertIn("did not account for every manifest step", stderr)
        self.assertEqual(stdout, "")
        self.assertIn("2. [ordinary step 2] Inspect inventory", self.agent.call_args.args[2])


class ReviewTests(unittest.TestCase):
    BLOCKER: ClassVar[dict] = {"file": "notes.txt", "problem": "fixture blocker", "severity": "blocker", "category": "issue"}

    def test_approved_diff_blocker_requires_a_changed_base_dependency(self):
        patches = {
            "approved": "diff --git a/notes.txt b/notes.txt\n--- a/notes.txt\n+++ b/notes.txt\n@@ -1 +1 @@\n-old\n+new\n",
            "base": "diff --git a/api.txt b/api.txt\n--- a/api.txt\n+++ b/api.txt\n@@ -1,2 +1 @@\n-removed\n same\n",
        }
        attribution = {"part": "approved", "file": "notes.txt", "line": 1,
                       "dependency": "The PR uses the removed API."}
        for side, verdict in (("before", "request_changes"), ("after", "approve")):
            with self.subTest(side=side):
                cause = {**attribution, "base_change": {"file": "api.txt", "line": 1, "side": side}}
                problems = catch_up_problems([{**self.BLOCKER, "attribution": cause}], patches)
                review = Review.judged({"problems": problems}, [], [], {})
                self.assertEqual(review.verdict, verdict)

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


class MinerGitHubTransportTests(unittest.TestCase):
    def test_miner_and_pipeline_merges_use_the_arete_gh_login(self):
        repo, head, base = "example/arete", "b" * 40, "a" * 40
        commands = [
            ["gh", "api", "user"],
            ["gh", "pr", "merge", "8", "--repo", repo, "--squash", "--match-head-commit", head],
            ["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/9/merge", "--input", "-"],
        ]
        replies = [subprocess.CompletedProcess(cmd, 0, body, "") for cmd, body in
                   zip(commands, ['{"login":"operator"}', "", '{"merged":true}'])]
        with mock.patch.object(util, "sh_with_network", side_effect=replies) as transport, \
                mock.patch.object(gh, "merge_readiness", return_value=("", "")):
            self.assertEqual(gh.me.__wrapped__(), "operator")
            gh.pr_squash(repo, 8, head)
            self.assertEqual(gh.pr_merge_auto(repo, 9, head, "main", base), "")
        self.assertEqual([call.args[0] for call in transport.call_args_list], commands)
        for call in transport.call_args_list:
            self.assertNotIn("env", call.kwargs)
            self.assertIsNone(call.kwargs["cwd"])


class EvalOverlayCopyTests(unittest.TestCase):
    def test_mixed_comparison_copies_local_agents_file_into_both_arms(self):
        root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        repo = config.Repo("arete", root / "repo")
        cfg = config.Config(repos=[repo])
        repo.layer_dir.mkdir(parents=True)
        (repo.path / evals.LAYER_CONFIG).write_text('[setup]\ncopy = ["AGENTS.md"]\n')
        local_rules = "Local agent instructions\n"
        (repo.path / "AGENTS.md").write_text(local_rules)
        base, head = "a" * 40, "b" * 40
        rules = {base: b"Base Claude instructions", head: b"Candidate Claude instructions"}
        prepared = {}
        task = evals.Task("fixture", "Fixture", "Build it")

        def suite(cfg, repo, source, tasks, tag, prompts):
            checkout = root / tag
            checkout.mkdir()
            evals.prepare(repo, source, checkout, mock.Mock())
            self.assertEqual(source.ref, prompts.commit)
            self.assertEqual((checkout / "CLAUDE.md").read_bytes(), rules[source.ref])
            prepared[source.ref] = checkout
            result = evals.TaskResult(task.name, passed=True, graded=True, base_commit=base,
                                      context_sha="fixture", prompts_used={"build.md"})
            return evals.Arm(tag, [result])

        def records(args, cwd):
            if args[:2] == ["ls-tree", "-r"]:
                return [f"100644 blob {args[2]}\tCLAUDE.md"]
            return []

        with (
            mock.patch.multiple(evals, commit_overlay=mock.DEFAULT, run_suite=suite,
                                paired_tasks=mock.Mock(return_value=[task]),
                                export_prompts=lambda repo, ref, dest: {"build.md": ref.encode()}),
            mock.patch.object(evals.Source, "read", lambda source, name: rules[source.ref]),
            mock.patch.object(util, "git_records", side_effect=records),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
        ):
            comparison = evals.compare_prompts(cfg, repo, base, head, harness=["CLAUDE.md"])

        self.assertTrue(comparison.complete, comparison.markdown)
        self.assertEqual(set(prepared), {base, head})
        for ref, checkout in prepared.items():
            with self.subTest(ref=ref):
                copied = checkout / "AGENTS.md"
                self.assertTrue(copied.is_file())
                self.assertEqual(copied.read_text(), local_rules)


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
        self.eval.return_value = evals.Comparison("| pending eval table |", complete=True)
        self.assertEqual(self.run_miner(), 0)
        self.assertFalse(self.merged)
        self.status = "success"
        self.eval.return_value = evals.Comparison("| latest eval table |", complete=True)
        self.assertEqual(self.run_miner(), 0)
        self.assertTrue(self.merged)
        self.assertEqual(self.ci_reads, [self.HEAD, self.HEAD])
        self.assertEqual([call[0] for call in self.calls], ["comment", "edit-comment", "merge"])
        self.assertEqual(list(self.comments), [1])
        self.assertIn("| latest eval table |", self.comments[1]["body"])
        self.assertNotIn("pending eval table", self.comments[1]["body"])

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

    def test_lowered_trust_while_inspecting_a_behind_pr_prevents_branch_update(self):
        self.behind = True

        def inspect_diff(*args, **kwargs):
            self.layer.trust_level = "pr"
            return self.diff.return_value

        self.diff.side_effect = inspect_diff
        self.assertIn("trust", self.decide())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ci_reads, [])
        self.eval.assert_not_called()

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

    def use_global_repo(self):
        self.repo = config.Repo("shared", Path("."), github="example/skills")
        self.cfg = config.Config(repos=[self.repo], global_repo=self.repo.name)
        self.url = "https://github.com/example/skills/pull/8"

    def test_global_rule_in_skill_diff_requires_approval_at_apply(self):
        self.use_global_repo()
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

    def test_global_rules_prs_stay_open_for_owner_review(self):
        self.use_global_repo()
        self.prs = [{"html_url": self.url, "pull_request": {}}]
        self.behind = True
        with mock.patch.object(evals, "compare_harness") as harness:
            for trust, files in (("pr", {}), ("automerge", {"skills/example/SKILL.md": 2})):
                with self.subTest(trust=trust):
                    self.layer.trust_level = trust
                    self.labels = {miner.MINER_LABEL}
                    self.calls.clear()
                    self.diff.return_value = diffguard.Diff({miner.GLOBAL_RULES: 1, **files})
                    self.assertEqual(self.run_miner(), 0)
                    self.assertEqual(self.labels, {miner.MINER_LABEL, miner.REVIEW_LABEL})
                    self.assertEqual([call[:2] for call in self.calls], [("label", "create"), ("pr", "edit")])
                    self.assertIn("awaiting human review", self.decide())
            harness.assert_not_called()
        self.assertFalse(self.merged)
        self.assertEqual(self.ci_reads, [])
        self.proposals.assert_not_called()

    def test_global_skill_uses_its_own_paired_evals_and_ci_before_jev(self):
        self.use_global_repo()
        self.repo.layer = self.root / "shared-layer"
        (self.repo.layer / "evals").mkdir(parents=True)
        (self.repo.layer / "evals" / "global-task.toml").write_text('prompt = "Build it"\ncheck = "check behavior"\n')
        self.cfg.repos.append(config.Repo("arete", self.root, github="example/arete", mine=False))
        self.layer.required_check = {"context": "skills-ci", "app_id": 123}
        skill = "skills/example/SKILL.md"
        helper = "skills/example/helper.md"
        self.repo.path = self.root / "shared"
        stale = self.repo.path / helper
        stale.parent.mkdir(parents=True)
        stale.write_text("old helper")
        self.layer.copy = [f"./{helper}"]
        trees = {self.BASE: {skill: "base skill", helper: "current helper"},
                 self.HEAD: {skill: "candidate skill", helper: "current helper"}}
        self.diff.return_value = diffguard.Diff({skill: 2})
        self.prs = [{"html_url": self.url, "pull_request": {}}]
        graded = []

        def read(args, **kwargs):
            commit = f"repos/{self.repo.github}/commits/{self.HEAD}"
            if args == ["api", f"{commit}/check-runs?filter=latest&per_page=100", "--paginate", "--slurp"]:
                self.ci_reads.append(self.HEAD)
                return [{"check_runs": [{"head_sha": self.HEAD, "name": "skills-ci", "app": {"id": 123},
                                         "status": "completed" if self.status == "success" else "in_progress",
                                         "conclusion": self.status}]}]
            if args == ["api", f"{commit}/statuses?per_page=100", "--paginate", "--slurp"]:
                return [[]]
            return self.github_read(args, **kwargs)

        def records(args, cwd):
            if args[:2] == ["ls-tree", "-r"]:
                self.assertEqual(cwd, self.repo.path)
                return [f"100644 blob {args[2]}\t{name}" for name in trees[args[2]]]
            self.assertEqual(args, ["ls-files"])
            return []

        def git(args, cwd, **kwargs):
            if args[0] == "diff":
                self.assertEqual(args[3:6], [self.BASE, self.HEAD, "--"])
                self.assertEqual(args[-1], f":(literal){skill}")
                return "-base skill\n+candidate skill\n"
            return self.git(args, cwd, **kwargs)

        def suite(cfg, repo, source, tasks, tag, prompts):
            self.assertIs(repo, self.repo)
            task, = tasks
            self.assertEqual((task.name, task.base), ("global-task", self.BASE))
            trial = f"{tag}-{len(graded)}"
            run = evals.EvalRun(id=trial, repo=repo, dir=self.root / trial, task=task.name, trial=trial,
                                writes_lines=False)
            run.call_kw = mock.Mock(return_value={})
            run.gate = mock.Mock(return_value=(True, "checks passed"))
            run.agent = mock.Mock(return_value=Result(ok=True, structured={}))
            result = evals.run_task(cfg, source, task, prompts, run)
            self.assertTrue(result.graded and result.passed, result.why)
            self.assertEqual([call.args[0] for call in run.agent.call_args_list], ["plan", "build"])
            return evals.Arm(tag, [result])

        def checkout(path, base, wt):
            wt.mkdir(parents=True)
            return base

        def check(cmd, cfg, repo, *, cwd, timeout):
            self.assertIs(repo, self.repo)
            self.assertEqual(cmd, "check behavior")
            graded.append(((cwd / skill).read_text(), (cwd / helper).read_text()))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        compare_harness = evals.compare_harness

        def compare(*args):
            report = compare_harness(*args)
            self.assertTrue(report.complete)
            self.assertEqual(report.unmeasured, [skill])
            return report

        with (
            mock.patch.object(gh, "gh_json", side_effect=read),
            mock.patch.object(config, "load", return_value=self.cfg),
            mock.patch.multiple(evals, compare_harness=compare, run_suite=suite, git=mock.Mock(return_value=self.BASE),
                                suites=mock.DEFAULT, fetch_origin=mock.DEFAULT, checkout_without_origin_refs=checkout,
                                commit_overlay=mock.DEFAULT),
            mock.patch.object(paths, "WORKTREES_DIR", self.root / "eval-worktrees"),
            mock.patch.object(evals.Source, "read", lambda source, name: trees[source.ref][name].encode()),
            mock.patch.object(util, "git_records", side_effect=records),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
            mock.patch.object(miner, "git", side_effect=git),
            mock.patch.object(miner.vault, "hidden", side_effect=str),
            mock.patch.object(evals.environment, "sh", side_effect=check),
            mock.patch.object(jev, "ask_many", return_value=[jev.Reply()]),
            mock.patch.object(jev, "ask") as ask,
        ):
            self.status = "pending"
            proposal = {"repo": self.repo.name, "layer": "global", "file": skill, "change": "fix skill"}
            self.assertEqual(self.run_miner(proposals=[proposal], jev_ready=True), 0)
            self.assertFalse(self.merged)
            ask.assert_not_called()

            self.status = "success"
            for verdict in (False, True):
                with self.subTest(verdict=verdict):
                    ask.return_value = jev.Reply({"fix": jev.Answer(verdict, 1.0, {"yes": float(verdict)})})
                    self.assertEqual(self.run_miner(jev_ready=True), 0)
                    evidence = json.loads(ask.call_args.args[0])
                    self.assertEqual(evidence[skill], "-base skill\n+candidate skill\n")
                    self.assertIn(f"JEV answered {'yes' if verdict else 'no'}", self.comments[1]["body"])
                    self.assertEqual(self.merged, verdict)
                    self.assertEqual(miner.REVIEW_LABEL in self.labels, not verdict)
                    if not verdict:
                        self.assertIn("awaiting human review", self.decide())
                        self.labels.remove(miner.REVIEW_LABEL)
            self.assertEqual(ask.call_count, 2)
            evals.suites.assert_not_called()
        self.eval.assert_not_called()
        self.assertEqual(self.ci_reads, [self.HEAD] * 3)
        self.assertEqual(graded, [("base skill", "current helper"), ("candidate skill", "current helper")] * 3)
        self.assertEqual(stale.read_text(), "old helper")
        self.assertEqual(self.calls[-1], ("merge", self.HEAD))
        self.proposals.assert_called_once_with(self.url, "merged")

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

    def test_lowered_trust_during_evals_or_jev_prevents_pr_writes(self):
        self.repo.layer = self.root
        trust_file = self.root / "config.toml"

        def lower_trust(result):
            def finish(*args):
                trust_file.write_text('[trust]\nlevel = "pr"\n')
                return result

            return finish

        reports = (
            evals.Comparison("eval table", complete=True),
            evals.Comparison("eval table", regressions=["`regressed-task`"]),
            evals.Comparison("eval table", incomplete=["`unfinished-task`"]),
        )
        with mock.patch.object(config, "load_layer", wraps=self.load_layer):
            for report in reports:
                with self.subTest(report=report):
                    trust_file.write_text('[trust]\nlevel = "automerge"\n')
                    self.eval.side_effect = lower_trust(report)
                    self.assertIn("trust", self.decide())
                    self.assertEqual(self.calls, [])
            self.eval.side_effect = None
            self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)
            for approved in (True, False):
                with self.subTest(approved=approved), mock.patch.object(
                    miner, "judge_prompts", side_effect=lower_trust((approved, "JEV verdict")),
                ) as judge:
                    trust_file.write_text('[trust]\nlevel = "automerge"\n')
                    self.assertIn("trust", self.decide())
                    judge.assert_called_once()
                    self.assertEqual(self.calls, [])
        self.proposals.assert_not_called()

    def test_lowered_trust_after_comment_prevents_the_next_pr_write(self):
        self.repo.layer = self.root
        trust_file = self.root / "config.toml"
        post = gh.pr_comment

        def comment(*args):
            post(*args)
            trust_file.write_text('[trust]\nlevel = "pr"\n')

        for report, verdict in (
            (evals.Comparison("eval table", complete=True), None),
            (evals.Comparison("eval table", regressions=["`regressed-task`"]), None),
            (evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True), False),
        ):
            with self.subTest(report=report), mock.patch.object(gh, "pr_comment", side_effect=comment) as posted, \
                    mock.patch.object(miner, "judge_prompts", return_value=(verdict, "JEV verdict")), \
                    mock.patch.object(config, "load_layer", wraps=self.load_layer):
                self.comments.clear()
                self.calls.clear()
                trust_file.write_text('[trust]\nlevel = "automerge"\n')
                self.eval.return_value = report
                self.assertIn("trust", self.decide())
                posted.assert_called_once()
                self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.proposals.assert_not_called()

    def test_lowered_trust_during_label_creation_prevents_labeling_the_pr(self):
        self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)

        def ensure_label(*args, **kwargs):
            self.layer.trust_level = "pr"
            return ""

        with mock.patch.object(gh, "ensure_label", side_effect=ensure_label) as ensured, \
                mock.patch.object(miner, "judge_prompts", return_value=(False, "JEV answered no.")):
            self.assertIn("trust", self.decide())
        ensured.assert_called_once()
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertEqual(self.labels, {miner.MINER_LABEL})
        self.proposals.assert_not_called()

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
            self.assertEqual(source.ref, "")
            return evals.Arm(tag, [evals.TaskResult(task, passed, base_commit=self.BASE, context_sha="harness",
                                                  graded=True, prompts_used={"build.md"},
                                                  required_behavior_failed=not passed)
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

    def test_mixed_pr_closes_for_external_regression_with_both_edits_in_place(self):
        self.repo.path = self.root / "arete"
        config.load_layer.side_effect = self.load_layer
        external = config.Repo("external", self.root / "external", public=True)
        self.cfg.repos.append(external)
        external_base = "d" * 40
        harness = "CLAUDE.md"
        instructions = {self.BASE: "Base rules allow the default setting.",
                        self.HEAD: "Candidate rules allow the default setting."}
        external_rules = "Keep the custom setting."
        build_prompts = {self.BASE: "Preserve the custom setting.", self.HEAD: "Use the default setting."}
        self.diff.return_value = diffguard.Diff({self.PROMPT: 2, harness: 2})
        self.eval.side_effect = self.compare_prompts
        for repo in self.cfg.repos:
            eval_dir = repo.layer_dir / "evals"
            eval_dir.mkdir(parents=True)
            verify_step = 'verify = "Exercise the behavior"\n' if repo == self.repo else ""
            (eval_dir / f"{repo.name}-task.toml").write_text('prompt = "Build it"\ncheck = "assert behavior"\n'
                                                           + verify_step)
        (self.repo.path / evals.LAYER_CONFIG).write_text(
            '[trust]\nlevel = "automerge"\n[verify]\nnotes = "Operator grading instructions"\n')
        (external.path / harness).write_text(external_rules)

        def records(args, cwd):
            if args[:2] == ["ls-tree", "-r"]:
                self.assertEqual(cwd, self.repo.path)
                return [f"100644 blob {self.BASE}\t{harness}"]
            if args == ["ls-files"]:
                return []
            if args[:1] == ["ls-files"]:
                return [f".agents/evals/{cwd.name}-task.toml"] if args[-1] == evals.EVALS else [harness]
            return ["M", self.PROMPT, "M", harness]

        def export(repo, ref, dest):
            dest.mkdir(parents=True)
            (dest / "plan.md").write_text("Plan it")
            build = build_prompts[ref].encode()
            (dest / "build.md").write_bytes(build)
            return {"build.md": build}

        read_file = evals.Source.read

        def read(source, name):
            if source.ref:
                self.assertEqual(name, harness)
                return instructions[source.ref].encode()
            return read_file(source, name)

        def checkout(path, base, wt):
            self.assertEqual(base, external_base if path == external.path else self.BASE)
            wt.mkdir(parents=True)
            return base

        def git(args, cwd, **kwargs):
            if args[:2] == ["rev-parse", "--verify"]:
                self.assertEqual(args[2], "origin/main^{commit}" if cwd == external.path else f"{self.BASE}^{{commit}}")
                return external_base if cwd == external.path else self.BASE
            return ""

        ran = []
        graded = []

        def grade(cfg, run, layer, wt, title, ask, steps, timeout):
            self.assertEqual(layer.verify.notes, "Operator grading instructions")
            self.assertEqual(steps, "Exercise the behavior")
            graded.append((wt / harness).read_text())
            return verify.Verified("pass")

        def suite(cfg, repo, source, tasks, tag, prompts):
            task, = tasks
            run = evals.EvalRun(id=tag, repo=repo, dir=self.root / tag, task=task.name, trial=tag)
            run.call_kw = mock.Mock(return_value={})
            run.gate = mock.Mock(return_value=(True, "checks passed"))

            def agent(step, text, wt, **kwargs):
                if step == "build":
                    self.assertIn(build_prompts[prompts.commit], text)
                    setting = "default" if build_prompts[self.HEAD] in text else "custom"
                    (wt / "setting.txt").write_text(setting)
                return Result(ok=True, structured={})

            def check(cmd, cfg, repo, *, cwd, timeout):
                self.assertEqual(cmd, "assert behavior")
                rules = (cwd / harness).read_text()
                setting = (cwd / "setting.txt").read_text()
                failed = rules == external_rules and setting != "custom"
                ran.append((repo.name, rules, setting, not failed))
                return SimpleNamespace(returncode=int(failed), stdout="", stderr="")

            run.agent = agent
            with mock.patch.object(evals.environment, "sh", side_effect=check):
                result = evals.run_task(cfg, source, task, prompts, run)
            self.assertTrue(result.graded, result.why)
            return evals.Arm(tag, [result])

        with (
            mock.patch.multiple(evals, export_prompts=export, run_suite=suite, fetch_origin=mock.DEFAULT,
                                git=git, checkout_without_origin_refs=checkout, commit_overlay=mock.DEFAULT,
                                grade=grade),
            mock.patch.object(evals.Source, "read", read),
            mock.patch.object(util, "git_records", side_effect=records),
            mock.patch.object(evals.paths, "WORKTREES_DIR", self.root / "eval-worktrees"),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
        ):
            self.assertEqual(self.decide(), "closed for an eval regression")
        self.assertEqual(graded, [instructions[self.BASE], instructions[self.HEAD]])
        self.assertEqual(ran, [
            ("arete", instructions[self.BASE], "custom", True),
            ("arete", instructions[self.HEAD], "default", True),
            ("external", external_rules, "custom", True),
            ("external", external_rules, "default", False),
        ])
        self.assertIn("`external-task` in external", self.calls[0][2].split("\n\n", 1)[0])
        self.assertEqual(self.calls[1], ("pr", "close", self.URL))

    def test_out_of_scope_prs_receive_no_actions(self):
        self.behind = True
        for extra in ("arete/pipeline.py", "arete/prompts/fix.md", evals.LAYER_CONFIG, ".agents/evals/task.toml"):
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
                files = {self.PROMPT: 2, ".agents/evals/task.toml": 1} if mixed else {self.PROMPT: 2}
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
                    "why": "Exercise the required behavior.", "confidence": 0.9}
        wt = self.root / "miner-wt"
        self.diff.return_value = staged
        self.eval.return_value = evals.Comparison("")

        def apply_git(args, cwd, **kwargs):
            if cwd == wt:
                if args == ["rev-parse", "HEAD"]:
                    return self.UPDATED
                self.assertEqual(args, ["push", "-q", "--force-with-lease", "-u", "origin", self.BRANCH])
                self.head = self.UPDATED
                return ""
            return self.git(args, cwd, **kwargs)

        with (
            mock.patch.multiple(gh, open_pr_url=mock.Mock(return_value=""), prs=mock.Mock(return_value=[]),
                                ensure_label=mock.DEFAULT, pr_create=mock.Mock(return_value=self.URL)),
            mock.patch.multiple(miner.workspace, make_worktree=mock.Mock(return_value=wt),
                                remove_worktree=mock.DEFAULT, remote_head=mock.Mock(return_value=self.HEAD)),
            mock.patch.object(miner, "miner_branch", return_value=self.BRANCH),
            mock.patch.object(miner, "apply_in", return_value=Result(ok=True)),
            mock.patch.object(miner, "git", side_effect=apply_git) as git,
            mock.patch.object(diffguard, "stage", return_value=staged),
            mock.patch.object(util, "git_records", return_value=["M", relative]),
            mock.patch.object(util, "git_as_owner") as commit,
            mock.patch.object(evals, "compare", return_value="suite results") as suite,
            mock.patch.object(miner, "public_wording_refusal", return_value=""),
        ):
            self.assertEqual(miner.proposal_status(self.cfg, proposal, []), ("pending", ""))
            self.assertEqual(self.run_miner(proposals=[proposal], apply_repo=miner.apply_repo), 0)
            commit.assert_called_once()
            self.assertEqual(commit.call_args.args[1], wt)
            self.assertIn(mock.call(["push", "-q", "--force-with-lease", "-u", "origin", self.BRANCH], wt, timeout=300),
                          git.call_args_list)
            miner.workspace.remove_worktree.assert_called_once_with(self.repo, wt)
            gh.pr_create.assert_called_once()
            self.assertEqual(gh.pr_create.call_args.args[:3], (self.repo.github, self.BRANCH, "main"))
            self.assertEqual(gh.pr_create.call_args.kwargs["labels"], [miner.MINER_LABEL])
            suite.assert_called_once_with(self.repo.name, self.UPDATED)
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertIn("suite results", self.calls[0][2])
        self.assertEqual(self.ci_reads, [])
        self.proposals.assert_not_called()

    def test_prompt_comparison_uses_working_copy_harness_for_both_arms(self):
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
            "CLAUDE.md": "Operator project instructions",
            ".claude/commands/check.md": "Operator Claude command",
            ".claude/skills/local/SKILL.md": "Operator Claude skill",
            "skills/local/SKILL.md": "Operator shared skill",
        }
        deleted_skill = "skills/deleted/SKILL.md"
        base_harness = {
            ".agents/expertise.md": "Committed expertise",
            "CLAUDE.md": "Committed project instructions",
            deleted_skill: "Committed skill the operator deleted",
            evals.LAYER_CONFIG: '[verify]\nnotes = "config from the checkout"\n',
            ".agents/evals/base-only.toml": 'prompt = "Committed eval task"\n',
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
            if args == ["ls-files"]:
                return list(base_harness)
            if args[-2:] == ["--", evals.EVALS]:
                self.assertEqual(cwd, self.repo.path)
                return [task_file, ".agents/evals/base-only.toml"]
            if args[:1] == ["ls-files"]:
                self.assertEqual(cwd, self.repo.path)
                self.assertEqual(args[-2:], ["--", "."])
                return list(dict.fromkeys((*base_harness, *local_harness, skill_link, task_file)))
            self.assertEqual(args, ["diff", "--name-status", "--no-renames", self.BASE, self.HEAD])
            return ["M", self.PROMPT]

        def suite(cfg, repo, source, tasks, tag, prompts):
            self.assertEqual(source.ref, "")
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
            return evals.Arm(tag, [result])

        def checkout(repo_path, base, wt):
            self.assertEqual(base, self.BASE)
            for rel, content in base_harness.items():
                dest = wt / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
            (wt / deleted_skill).with_name(".gitkeep").touch()
            return self.BASE

        def grade(work, step, kind, *, role, goal):
            self.assertEqual(work.layer.verify.notes, "operator verify settings")
            self.assertEqual(work.layer.config_file.read_bytes(), local_settings)
            for rel, content in local_harness.items():
                self.assertEqual((work.ws / rel).read_text(), content)
            self.assertTrue((work.ws / skill_link).is_symlink())
            self.assertEqual((work.ws / skill_link).read_text(), local_harness["skills/local/SKILL.md"])
            self.assertFalse((work.ws / deleted_skill).exists())
            self.assertFalse((work.ws / ".agents/evals/base-only.toml").exists())
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
            mock.patch.object(miner, "judge_prompts", return_value=(True, "JEV answered yes.")) as judge,
        ):
            self.assertIn("incomplete", self.decide())
            original = self.comments[1]["body"]
            self.comments = {99: {"id": 99, "user": {"login": "reviewer"}, "body": original}, **self.comments}
            tasks[self.repo.name] = [evals.Task("next-task", "Another public task", "fixture")]
            tasks[private.name] = [evals.Task("other-secret", "Another private task", "fixture")]
            self.assertIn("incomplete", self.decide())
            self.assertIn("incomplete", self.decide())
            self.base = self.UPDATED
            self.assertIn("incomplete", self.decide())
            evals.run_suite.assert_not_called()
            self.assertEqual(judge.call_count, 4)
        self.assertEqual(self.ci_reads, [self.HEAD] * 4)
        self.assertEqual([call[0] for call in self.calls], ["comment", "edit-comment", "edit-comment"])
        self.assertEqual([call[1] for call in self.calls[1:]], [1, 1])
        self.assertIn("`next-task`", self.calls[1][2])
        self.assertIn(f"Base `{self.UPDATED}`", self.comments[1]["body"])
        self.assertEqual(self.comments[99]["body"], original)
        for name, body in (("busy-task", original), ("next-task", self.comments[1]["body"])):
            self.assertIn(f"Incomplete eval tasks were `{name}` in arete, private repo 1 (1 task).", body)
            self.assertIn("its evals were already running", body)
            for hidden in ("secret-task", "other-secret", "private-project"):
                self.assertNotIn(hidden, body)
        self.assertNotIn("busy-task", self.comments[1]["body"])
        self.proposals.assert_not_called()

    def test_completed_comparison_replaces_incomplete_feedback_on_the_same_head(self):
        for status, base in (("failure", self.BASE), ("failure", self.UPDATED), ("success", self.BASE)):
            with self.subTest(status=status, base=base):
                self.calls.clear()
                self.comments.clear()
                self.ci_reads.clear()
                self.status = status
                self.base = self.BASE
                self.eval.return_value = evals.Comparison("partial table", incomplete=["`first-task`"])
                self.decide()
                self.assertIn("Incomplete eval tasks were `first-task`", self.comments[1]["body"])

                self.base = base
                self.eval.return_value = evals.Comparison("complete table", complete=True)
                decision = self.decide()
                self.assertEqual(self.ci_reads, [self.HEAD, self.HEAD])
                self.assertEqual(list(self.comments), [1])
                body = self.comments[1]["body"]
                self.assertIn("complete table", body)
                self.assertIn(f"Base `{base}`, head `{self.HEAD}`.", body)
                self.assertNotIn("first-task", body)
                self.assertIn("Incomplete eval tasks: none.", body)
                self.assertEqual(self.calls[1], ("edit-comment", 1, body))
                if status == "success":
                    self.assertEqual(decision, "merged")
                    self.assertEqual(self.calls[2:], [("merge", self.HEAD)])
                else:
                    self.assertIn("does not have a passing", decision)
                    self.assertIn("does not have a passing", self.decide())
                    self.assertEqual([call[0] for call in self.calls], ["comment", "edit-comment"])
                    self.proposals.assert_not_called()

    def test_completed_comparison_refreshes_the_original_comment_after_a_branch_update(self):
        self.eval.return_value = evals.Comparison("partial table", incomplete=["`first-task`"])
        self.assertEqual(self.decide(), "the eval comparison is incomplete")
        original = self.comments[1]["body"]
        self.assertIn("Incomplete eval tasks were `first-task`", original)

        self.base = "d" * 40
        self.behind = True
        self.assertEqual(self.decide(), "branch update requested")
        self.assertEqual(self.calls[1:], [("update", {"expected_head_sha": self.HEAD})])
        self.assertEqual(self.comments[1]["body"], original)
        self.assertEqual(self.ci_reads, [self.HEAD])

        self.eval.return_value = evals.Comparison("complete table", complete=True)
        self.assertEqual(self.decide(), "merged")
        self.assertEqual(self.eval.call_args_list, [mock.call(self.cfg, self.repo, self.BASE, self.HEAD),
                                                    mock.call(self.cfg, self.repo, self.base, self.UPDATED)])
        self.assertEqual(self.ci_reads, [self.HEAD, self.UPDATED])
        self.assertEqual(list(self.comments), [1])
        body = self.comments[1]["body"]
        self.assertIn(f"Base `{self.base}`, head `{self.UPDATED}`.", body)
        self.assertIn("complete table", body)
        self.assertNotIn("first-task", body)
        self.assertIn("Incomplete eval tasks: none.", body)
        self.assertEqual(self.calls[2:], [("edit-comment", 1, body), ("merge", self.UPDATED)])

    def test_lowered_trust_during_comment_lookup_prevents_editing_eval_feedback(self):
        self.eval.return_value = evals.Comparison("eval table", incomplete=["`first-task`"])
        self.assertIn("incomplete", self.decide())
        original = self.comments[1]["body"]
        self.eval.return_value = evals.Comparison("eval table", incomplete=["`second-task`"])
        find_comment = gh.find_own_pr_comment

        def find_and_lower_trust(*args):
            self.layer.trust_level = "pr"
            return find_comment(*args)

        with mock.patch.object(gh, "find_own_pr_comment", side_effect=find_and_lower_trust):
            self.assertIn("trust", self.decide())
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertEqual(self.comments[1]["body"], original)

    def test_multipart_suite_defers_only_when_it_has_eval_tasks(self):
        self.repo.layer = self.root / "arete-layer"
        multipart = config.Repo("multipart", self.root, layer=self.root / "multipart-layer", public=True,
                                parts=[config.Part("frontend", "example/frontend")])
        self.cfg.repos.append(multipart)
        for repo in self.cfg.repos:
            (repo.layer_dir / "evals").mkdir(parents=True)
        (self.repo.layer_dir / "evals" / "measured.toml").write_text('prompt = "Try the behavior"\n')

        def suite(cfg, repo, source, tasks, tag, prompts):
            self.assertEqual(repo, self.repo)
            return evals.Arm(tag, [evals.TaskResult(task.name, passed=True, graded=True, base_commit=task.base,
                                                  context_sha="harness", prompts_used={"build.md"}) for task in tasks])

        self.eval.side_effect = self.compare_prompts
        with (
            mock.patch.multiple(evals, export_prompts=lambda repo, ref, dest: {"build.md": ref.encode()},
                                run_suite=suite, git=mock.Mock(return_value=self.BASE), fetch_origin=mock.DEFAULT),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
            mock.patch.object(miner, "judge_prompts") as judge,
        ):
            with self.subTest("empty multipart suite"):
                self.assertEqual(self.decide(), "merged")
                self.assertEqual(self.calls[-1], ("merge", self.HEAD))
            self.calls.clear()
            self.comments.clear()
            self.proposals.reset_mock()
            (multipart.layer_dir / "evals" / "unavailable.toml").write_text('prompt = "Try the behavior"\n')
            with self.subTest("multipart suite with a task"):
                self.assertEqual(self.decide(), "the eval comparison is incomplete")
                self.assertEqual([call[0] for call in self.calls], ["comment"])
                self.assertIn("Incomplete eval tasks were `unavailable` in multipart", self.calls[0][2])
                self.proposals.assert_not_called()
            evals.fetch_origin.assert_not_called()
            judge.assert_not_called()

    def test_pr_requires_jev_yes_for_prompts_no_candidate_graded(self):
        task = evals.Task("fixture", "Fixture", "fixture")
        blocked_task = evals.Task("blocked", "Fixture", "fixture", verify="Exercise the repaired behavior")
        ran = []

        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            self.assertEqual(source.ref, prompts.commit if mixed else "")
            ran.append(prompts.commit)
            result = evals.TaskResult(task.name, passed=True, graded=True, base_commit=self.BASE,
                                      context_sha=prompts.commit, shared_context_sha="unchanged harness",
                                      repairs=int(on_base), prompts_used={"build.md", "fix.md"} if on_base else
                                      {"build.md"})
            results = [result]
            if blocked_task in tasks:
                blocked = replace(result, task=blocked_task.name, passed=False, graded=False,
                                  prompts_used={"build.md", "fix.md"})
                run = evals.EvalRun(id=tag, repo=repo, dir=self.root, task=blocked_task.name)
                grade = (verify.Verified("pass") if on_base else
                         verify.Verified("blocked", blocked_by="Verifier unavailable", finished=False))
                with (mock.patch.object(evals, "grade", return_value=grade),
                      mock.patch.object(evals, "git", return_value="")):
                    results.append(evals.acceptance(cfg, run, self.layer, self.root, blocked_task, None, blocked,
                                                    "", "", True, 60))
                self.assertEqual(blocked.graded, on_base)
            return evals.Arm(tag, results)

        def compare(*args, **kwargs):
            report = self.compare_prompts(*args, **kwargs)
            self.assertEqual(report.unmeasured, [prompt])
            if suite_state == "grading blocked":
                self.assertFalse(report.complete)
                self.assertEqual(report.incomplete, ["`blocked` in arete"])
            return report

        self.eval.side_effect = compare
        with (
            mock.patch.multiple(evals, suites=lambda cfg: [self.repo], run_suite=suite),
            mock.patch.object(jev, "ready", return_value=True),
            mock.patch.object(miner.vault, "hidden", side_effect=str),
        ):
            cases = [("fix.md", "ready", False), ("fix.md", "ready", True), ("fix.md", "grading blocked", True),
                     ("review.md", "ready", True), ("review.md", "busy", True), ("review.md", "empty", True)]
            for prompt, suite_state, mixed in cases:
                files = {f"arete/prompts/{prompt}": 2}
                if mixed:
                    files["CLAUDE.md"] = 2
                self.diff.return_value = diffguard.Diff(files)
                tasks = [task, blocked_task] if suite_state == "grading blocked" else [task]
                if suite_state == "empty":
                    tasks = []
                for verdict in (None, False, True):
                    with self.subTest(prompt=prompt, suite_state=suite_state, mixed=mixed, verdict=verdict):
                        self.calls.clear()
                        self.comments.clear()
                        self.labels.discard(miner.REVIEW_LABEL)
                        self.merged = False
                        ran.clear()
                        reply = (jev.Reply(error="Unavailable") if verdict is None else
                                 jev.Reply({"fix": jev.Answer(verdict, 1.0, {"yes": float(verdict)})}))
                        with (
                            mock.patch.object(evals, "export_prompts", side_effect=lambda r, ref, d: {prompt: ref.encode()}),
                            mock.patch.object(evals, "paired_tasks", return_value=tasks),
                            mock.patch.object(util, "lock", return_value=nullcontext(suite_state != "busy")),
                            mock.patch.object(jev, "ask", return_value=reply) as ask,
                        ):
                            decision = self.decide()
                        self.assertIn(prompt, ask.call_args.args[0])
                        self.assertEqual(ran, [self.BASE, self.HEAD] if suite_state in ("ready", "grading blocked") else [])
                        self.assertEqual(self.merged, verdict is True and suite_state == "ready")
                        self.assertEqual(miner.REVIEW_LABEL in self.labels, verdict is False)
                        if verdict is None and suite_state == "ready":
                            self.assertEqual(self.calls, [])
                        else:
                            self.assertIn("Not measured", self.calls[0][2])
                            if verdict is None:
                                self.assertEqual(decision, "the eval comparison is incomplete")
                            if verdict is True and suite_state != "ready":
                                self.assertIn("JEV answered yes", self.calls[0][2])
                                self.assertEqual(decision, "the eval comparison is incomplete")

    def test_hidden_fixtures_cannot_write_outside_the_eval_checkout(self):
        wt = self.root / "eval"
        wt.mkdir()
        outside = self.root / "live"
        outside.mkdir()
        protected = outside / "fixture.txt"
        protected.write_text("live data")
        (wt / "file-link").symlink_to(protected)
        (wt / "parent-link").symlink_to(outside, target_is_directory=True)
        task = evals.Task("fixture", "Fixture", "Use the skill", check="check fixture")

        with mock.patch.object(evals.environment, "sh", return_value=subprocess.CompletedProcess([], 0, "", "")) as sh:
            for rel in ("file-link", "parent-link/fixture.txt", "../live/fixture.txt", str(protected)):
                with self.subTest(rel=rel):
                    task.hidden_files = {rel: b"must stay in the eval"}
                    with self.assertRaisesRegex(workspace.RunFailed, "outside the eval checkout"):
                        evals.hidden_check(self.cfg, self.repo, task, wt, 5)
                    sh.assert_not_called()
                    self.assertEqual(protected.read_text(), "live data")
            task.hidden_files = {"fixtures/input.txt": b"held out input"}
            self.assertEqual(evals.hidden_check(self.cfg, self.repo, task, wt, 5).returncode, 0)
            self.assertEqual((wt / "fixtures/input.txt").read_bytes(), b"held out input")
            sh.assert_called_once()

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
                mock.patch.object(miner, "judge_prompts", return_value=(True, "JEV answered yes.")) as judge:
            self.assertIn("incomplete", self.decide())
            history = [call.args[:4] for call in evals.insights_store.add_eval.call_args_list]
            self.assertEqual(history, [("eval-base", "arete", task.name, True), ("eval-head", "arete", task.name, False)])
        self.assertIn("Incomplete eval tasks were `fixture`", self.calls[0][2])
        self.assertIn("| all | 1/1 | 0/1 |", self.calls[0][2])
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertFalse(self.merged)
        self.proposals.assert_not_called()
        self.assertEqual(judge.call_args.args[-1], ["build.md"])

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

    def test_preflight_timeout_keeps_a_green_pr_open_as_incomplete(self):
        task = evals.Task("fixture", "Fixture", "fixture", verify="Try the behavior")
        command = "prepare-fixture"
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root, verify=config.Verify(url="", preflight={"fixture setup": command}))
        baseline = evals.TaskResult(task.name, passed=True, graded=True, base_commit=self.BASE, context_sha="harness")
        candidate = replace(baseline, passed=False, graded=False, prompts_used={"build.md"})
        with (
            mock.patch.object(evals, "git", return_value=""),
            mock.patch.object(evals.vault, "hidden", side_effect=lambda text, *args: text),
            mock.patch.object(util, "run_whole_tree", side_effect=subprocess.TimeoutExpired(command, 60)),
            mock.patch.object(run, "agent") as agent,
        ):
            evals.acceptance(self.cfg, run, layer, self.root, task, None, candidate, "", "", True, 60)
            agent.assert_not_called()
        self.assertEqual(candidate.verify, "blocked")
        self.assertFalse(candidate.passed)
        self.assertFalse(candidate.graded)
        self.assertFalse(candidate.required_behavior_failed)
        self.assertIn("fixture setup: exit 124", candidate.why)
        arms = {"main": evals.Arm("main", [baseline]), "pr": evals.Arm("pr", [candidate])}
        self.eval.return_value = evals.comparison(self.repo, self.BASE, "evals", [(self.repo, arms)], ["build.md"], [])
        with mock.patch.object(miner, "judge_prompts", return_value=(True, "JEV answered yes.")) as judge:
            self.assertEqual(self.decide(), "the eval comparison is incomplete")
        self.assertEqual(judge.call_args.args[-1], ["build.md"])
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertIn("Incomplete eval tasks were `fixture`", self.calls[0][2])
        self.assertIn("| all | 1/1 | 0/1 |", self.calls[0][2])
        self.proposals.assert_not_called()

    def test_verifier_startup_failure_needs_an_independent_behavior_failure_to_close_a_pr(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="held-out", verify="Try the behavior")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root, verify=config.Verify(start="start-fixture", url="http://localhost:54321",
                                                                 ready_seconds=1))
        baseline = evals.TaskResult(task.name, passed=True, graded=True, base_commit=self.BASE, context_sha="harness")
        with (
            mock.patch.object(evals, "git", return_value=""),
            mock.patch.object(evals.vault, "hidden", side_effect=lambda text, *args: text),
            mock.patch.object(pipeline, "untracked_by_dir", return_value={}),
            mock.patch.object(pipeline, "restore_worktrees"),
            mock.patch.object(verify, "port_refusal", return_value=""),
            mock.patch.object(verify.scratch, "make", return_value=None),
            mock.patch.object(verify.scratch, "spawn") as spawn,
            mock.patch.object(verify.environment, "started", return_value=({}, {})),
            mock.patch.object(verify.urllib.request, "urlopen", side_effect=OSError("service not ready")),
            mock.patch.object(verify, "time") as clock,
            mock.patch.object(verify.os, "killpg", side_effect=ProcessLookupError),
            mock.patch.object(util, "stop_group"),
            mock.patch.object(util, "run_whole_tree") as held_out,
            mock.patch.object(run, "agent") as agent,
            mock.patch.object(miner, "judge_prompts", return_value=(True, "JEV answered yes.")),
        ):
            for start_exit, held_out_exit, diagnostic in ((None, 0, "did not answer within 1 seconds"),
                                                          (127, 0, "start command exited 127"),
                                                          (127, 1, "start command exited 127")):
                with self.subTest(start_exit=start_exit, held_out_exit=held_out_exit):
                    self.comments.clear()
                    self.calls.clear()
                    self.proposals.reset_mock()
                    clock.monotonic.side_effect = [0, 0, 2]
                    spawn.return_value = mock.Mock(returncode=start_exit, stdout=(),
                                                   poll=mock.Mock(return_value=start_exit))
                    held_out.return_value = subprocess.CompletedProcess(task.check, held_out_exit, "", "")
                    candidate = replace(baseline, passed=False, graded=False, prompts_used={"build.md"})
                    evals.acceptance(self.cfg, run, layer, self.root, task, None, candidate, "", "", True, 60)
                    arms = {"main": evals.Arm("main", [baseline]), "pr": evals.Arm("pr", [candidate])}
                    self.eval.return_value = evals.comparison(self.repo, self.BASE, "evals", [(self.repo, arms)],
                                                              ["build.md"], [])
                    self.assertEqual(candidate.verify, "fail")
                    self.assertFalse(candidate.passed)
                    self.assertIn(diagnostic, candidate.why)
                    if held_out_exit:
                        self.assertEqual(self.decide(), "closed for an eval regression")
                        self.assertIn("Closing because `fixture`", self.comments[1]["body"])
                        self.assertEqual(self.calls[1:], [("pr", "close", self.URL)])
                    else:
                        self.assertEqual(self.decide(), "the eval comparison is incomplete")
                        self.assertIn("Incomplete eval tasks were `fixture`", self.comments[1]["body"])
                        self.assertEqual([call[0] for call in self.calls], ["comment"])
                        self.proposals.assert_not_called()
                    self.assertIn("| all | 1/1 | 0/1 |", self.comments[1]["body"])
            agent.assert_not_called()

    def test_unavailable_held_out_commands_keep_their_failing_eval_score(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="expected-command")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root)
        for process, exit_code, complete in (
            (subprocess.CompletedProcess(task.check, 127, "", "sh: expected-command: command not found"), 127, False),
            (subprocess.CompletedProcess(task.check, 126, "", "sh: expected-command: permission denied"), 126, False),
            (subprocess.CompletedProcess(task.check, 1, "", "assertion failed"), 1, True),
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

    def test_required_check_outcome_requires_a_product_failure_and_a_command_exit(self):
        for verdict, cause, code, expected in (
            ("fail", None, 1, "fail"),
            ("fail", "product", 0, "fail"),
            ("fail", None, 124, ""),
            ("fail", None, 126, ""),
            ("fail", None, 127, ""),
            ("fail", None, -15, ""),
            ("fail", None, 143, ""),
            ("fail", None, None, ""),
            ("fail", "environment", 1, ""),
            ("fail", "prerequisite", 1, ""),
            ("blocked", None, 1, ""),
            ("pass", None, 0, "pass"),
        ):
            with self.subTest(verdict=verdict, cause=cause, code=code):
                self.assertEqual(evals.required_check_outcome(verdict, cause, code), expected)

    def test_optional_repo_check_and_failed_repair_do_not_hide_a_regression(self):
        task = evals.Task("required-behavior", "Fixture", "fixture", base=self.BASE, check="held-out")
        layer = config.RepoLayer(self.root, max_attempts=2, checks={"optional": "optional-check"})
        results = {}

        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            run = evals.EvalRun(id=tag, repo=repo, dir=self.root / tag, task=task.name)
            run.call_kw = mock.Mock(return_value={})
            run.agent = mock.Mock(side_effect=[Result(ok=True, structured={}), Result(ok=True),
                                               Result(ok=False, turns=0 if on_base else 1)])
            run.gate = mock.Mock(return_value=(False, "optional check failed"))
            check = subprocess.CompletedProcess(task.check, 0 if on_base else 1, "", "")
            with mock.patch.object(evals, "hidden_check", return_value=check):
                result = evals.run_task(cfg, source, task, evals.Prompts(commit=prompts.commit), run)
            self.assertEqual(run.agent.call_args.args[0], "fix2")
            self.assertEqual(result.checks, "fail")
            results[prompts.commit] = result
            return evals.Arm(tag, [result])

        self.eval.side_effect = self.compare_prompts
        with (
            mock.patch.multiple(evals, export_prompts=lambda repo, ref, dest: {"build.md": ref.encode()},
                                paired_tasks=lambda *args: [task], suites=lambda cfg: [self.repo], run_suite=suite,
                                git=mock.Mock(return_value=""), fetch_origin=mock.DEFAULT,
                                checkout_without_origin_refs=mock.Mock(return_value=self.BASE),
                                prepare=mock.Mock(return_value=layer), check_script=mock.Mock(return_value=([], []))),
            mock.patch.object(evals.paths, "WORKTREES_DIR", self.root / "eval-worktrees"),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
            mock.patch.object(evals.vault, "hidden", side_effect=str),
        ):
            self.assertEqual(self.decide(), "closed for an eval regression")
        self.assertTrue(results[self.BASE].passed)
        self.assertTrue(results[self.BASE].graded)
        self.assertNotIn("fix.md", results[self.BASE].prompts_used)
        self.assertIn("fix.md", results[self.HEAD].prompts_used)
        self.assertIn("Closing because `required-behavior`", self.comments[1]["body"])
        self.assertEqual(self.calls[-1], ("pr", "close", self.URL))

    def test_confirmed_verifier_failure_closes_even_when_other_commands_are_incomplete(self):
        task = evals.Task("required-behavior", "Fixture", "fixture", check="held-out", verify="Try the behavior")
        layer = config.RepoLayer(self.root, verify=config.Verify())
        results = []

        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            run = evals.EvalRun(id=tag, repo=repo, dir=self.root, task=task.name)
            result = evals.TaskResult(task.name, base_commit=self.BASE, context_sha="harness", prompts_used={"build.md"})
            grading = baseline_grade if on_base else candidate_grade
            command = subprocess.CompletedProcess(task.check, 0, "", "") if on_base else candidate_command
            with mock.patch.object(evals, "grade", return_value=grading), mock.patch.object(
                util, "run_whole_tree", side_effect=[command],
            ):
                results.append(evals.acceptance(cfg, run, layer, self.root, task, None, result, "", "", True, 60))
            return evals.Arm(tag, [result])

        self.eval.side_effect = self.compare_prompts
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"build.md": ref.encode()})
        observed_failure = {"what": "Required behavior", "how": "Run the feature", "saw": "exit 1: Assertion failed",
                            "passed": False}
        failed = verify.Verified("fail", checks=[observed_failure])
        blocked_check = {"what": "Another step", "how": "Run the command", "saw": "Service unavailable",
                         "passed": False, "cause": "environment"}
        blocked_with_failure = verify.Verified("blocked", finished=False, blocked_by="Another command could not run",
                                               checks=[{**observed_failure, "cause": "change"}, blocked_check])
        blocked_with_untagged_failure = replace(blocked_with_failure, checks=[observed_failure, blocked_check])
        with (
            mock.patch.multiple(evals, export_prompts=export, paired_tasks=lambda *args: [task],
                                suites=lambda cfg: [self.repo], run_suite=suite, git=mock.Mock(return_value="")),
            mock.patch.object(util, "lock", return_value=nullcontext(True)),
            mock.patch.object(evals.vault, "hidden", side_effect=lambda text, *args: text),
            mock.patch.object(miner, "judge_prompts", return_value=(True, "JEV answered yes.")) as judge,
        ):
            passed = verify.Verified("pass")
            timeout_grade = verify.Verified("fail", checks=[{**observed_failure, "saw": "exit 124: timeout"}])
            unobserved_grade = verify.Verified("fail", checks=[{**observed_failure, "saw": "Service unavailable"}])
            unavailable_grade = verify.Verified("fail", checks=[{**observed_failure, "saw": "exit 126: unavailable"}])
            timeout_command = subprocess.TimeoutExpired(task.check, 60)
            for baseline_grade, candidate_grade, candidate_command, exit_code, closes in (
                (passed, failed, timeout_command, 124, True),
                (passed, failed, subprocess.CompletedProcess(task.check, 127, "", "command not found"), 127, True),
                (passed, blocked_with_failure, timeout_command, 124, True),
                (passed, blocked_with_untagged_failure, timeout_command, 124, True),
                (passed, timeout_grade, timeout_command, 124, False),
                (passed, unobserved_grade, timeout_command, 124, False),
                (passed, unavailable_grade, timeout_command, 124, False),
                (timeout_grade, failed, timeout_command, 124, False),
            ):
                with self.subTest(main=baseline_grade, candidate=candidate_grade, exit_code=exit_code):
                    self.calls.clear()
                    self.comments.clear()
                    results.clear()
                    judge.reset_mock()
                    expected = "closed for an eval regression" if closes else "the eval comparison is incomplete"
                    self.assertEqual(self.decide(), expected)
                    baseline, candidate = results
                    self.assertEqual(baseline.graded, baseline_grade == passed)
                    self.assertFalse(baseline.required_behavior_failed)
                    self.assertFalse(candidate.passed)
                    self.assertFalse(candidate.graded)
                    self.assertEqual(candidate.failures[-1], f"check exit {exit_code}")
                    baseline_score = "1/1" if baseline_grade == passed else "0/1"
                    self.assertIn(f"| all | {baseline_score} | 0/1 |", self.calls[0][2])
                    if closes:
                        judge.assert_not_called()
                        self.assertIn("Closing because `required-behavior`", self.calls[0][2])
                        self.assertEqual(self.calls[1:], [("pr", "close", self.URL)])
                    else:
                        self.assertEqual(judge.call_args.args[-1], ["build.md"])
                        self.assertIn("Incomplete eval tasks were `required-behavior`", self.calls[0][2])
                        self.assertEqual([call[0] for call in self.calls], ["comment"])

    def test_confirmed_held_out_failure_closes_even_when_the_verifier_is_blocked(self):
        task = evals.Task("held-out-behavior", "Fixture", "fixture", check="expected-command", verify="Try it")
        layer = config.RepoLayer(self.root, verify=config.Verify())
        results = {}

        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            run = evals.EvalRun(id=tag, repo=repo, dir=self.root, task=task.name)
            result = evals.TaskResult(task.name, base_commit=self.BASE, context_sha="harness", prompts_used={"build.md"})
            grading = (verify.Verified("pass") if on_base
                       else verify.Verified("blocked", blocked_by="Verifier unavailable", finished=False,
                                            checks=[{"what": "Feature", "how": "Run it", "saw": "Service unavailable",
                                                     "passed": False, "cause": "environment"}]))
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
            mock.patch.object(miner, "judge_prompts", return_value=(True, "JEV answered yes.")) as judge,
        ):
            for candidate_command, exit_code, closes in (
                (FileNotFoundError("shell launcher unavailable"), 127, False),
                (subprocess.CompletedProcess(task.check, 127, "", "expected-command: command not found"),
                 127, False),
                (subprocess.CompletedProcess(task.check, 126, "", "expected-command: permission denied"), 126, False),
                (subprocess.CompletedProcess(f"timeout 10 {task.check}", 124, "", ""), 124, False),
                (subprocess.CompletedProcess(task.check, 1, "", "assertion failed"), 1, True),
            ):
                with self.subTest(command=candidate_command):
                    self.calls.clear()
                    self.comments.clear()
                    self.proposals.reset_mock()
                    judge.reset_mock()
                    decision = self.decide()
                    if closes:
                        judge.assert_not_called()
                        self.assertEqual(decision, "closed for an eval regression")
                        self.assertEqual(self.calls[1:], [("pr", "close", self.URL)])
                        self.assertIn("Closing because `held-out-behavior`", self.calls[0][2])
                        self.proposals.assert_called_once_with(self.URL, "rejected")
                    else:
                        self.assertEqual(judge.call_args.args[-1], ["build.md"])
                        self.assertEqual(decision, "the eval comparison is incomplete")
                        self.assertEqual([call[0] for call in self.calls], ["comment"])
                        self.proposals.assert_not_called()
                    self.assertEqual("Incomplete eval tasks: `held-out-behavior`" in self.calls[0][2], not closes)
                    self.assertIn("| all | 1/1 | 0/1 |", self.calls[0][2])
                    baseline, candidate = results[self.BASE], results[self.HEAD]
                    self.assertTrue(baseline.passed and baseline.graded)
                    self.assertFalse(candidate.passed)
                    self.assertFalse(candidate.graded)
                    self.assertEqual(candidate.failures,
                                     ["grading blocked: Verifier unavailable", f"check exit {exit_code}"])

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
            self.assertEqual(self.calls, [])
            self.assertEqual(self.run_miner(jev_ready=True), 0)
            self.assertEqual(ask.call_count, 2)
        self.assertEqual(self.ci_reads, [self.HEAD, self.HEAD])
        self.assertEqual(self.eval.call_count, 2)
        self.assertEqual([call[0] for call in self.calls], ["comment", "merge"])
        self.assertIn("JEV answered yes", self.calls[0][2])
        self.assertEqual(self.calls[1], ("merge", self.HEAD))
        self.proposals.assert_called_once_with(self.URL, "merged")

    def test_jev_errors_preserve_existing_incomplete_feedback_until_a_verdict(self):
        self.eval.return_value = evals.Comparison("partial table", incomplete=["`first-task`"])
        self.assertIn("incomplete", self.decide())
        original = self.comments[1]["body"]
        self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)
        replies = [jev.Reply(error="HTTP 503"), jev.Reply(error="JEV request timed out"), jev.Reply(),
                   jev.Reply({"fix": jev.Answer(True, 1.0, {"yes": 1})})]
        with mock.patch.object(jev, "ready", return_value=True), \
                mock.patch.object(jev, "ask", side_effect=replies) as ask, \
                mock.patch.object(miner.vault, "hidden", side_effect=str):
            for _ in range(3):
                self.assertEqual(self.decide(), "JEV returned no decision.")
                self.assertEqual([call[0] for call in self.calls], ["comment"])
                self.assertEqual(self.comments[1]["body"], original)
                self.assertEqual(self.labels, {miner.MINER_LABEL})
                self.proposals.assert_not_called()
            self.assertEqual(self.decide(), "merged")
        self.assertEqual(ask.call_count, 4)
        self.assertEqual([call[0] for call in self.calls], ["comment", "edit-comment", "merge"])
        self.assertEqual(list(self.comments), [1])
        self.assertIn("Not measured.", self.comments[1]["body"])
        self.assertIn("Incomplete eval tasks: none.", self.comments[1]["body"])
        self.assertNotIn("first-task", self.comments[1]["body"])
        self.assertIn("JEV answered yes", self.comments[1]["body"])
        self.assertEqual(self.labels, {miner.MINER_LABEL})
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
                self.assertEqual(self.calls, [])
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
