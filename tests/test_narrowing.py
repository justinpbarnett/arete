import json
from unittest import mock

from arete import config, gh, launch_lease, narrowing, outbound, paths, pipeline, record, run_lines, runlimits, tracker
from arete.review import Review
from arete.store import narrowings as store
from arete.store import runs as run_store
from arete.verify import Verified
from tests import test_process_limits as limits_tests

PROJECT = "fixture/repo"
BRANCH = "arete/published-before-the-park"
REQUIREMENT = "- Read Board through `screen.get`."
BODY = f"Keep the live screens current.\n\n## Acceptance criteria\n\n{REQUIREMENT}\n- Keep Home working."


class GitHub:
    def __init__(self):
        self.items = {1: self.item(1, BODY, "Live screens", [tracker.LABEL])}
        self.comments = []
        self.writes = []
        self.lost = ""
        self.applies = True
        self.visible = True
        self.after_child = lambda: None

    @staticmethod
    def item(number, body, title, labels):
        return {"number": number, "title": title, "body": body, "state": "OPEN",
                "labels": [{"name": label} for label in labels], "author": {"login": "fixture"},
                "url": gh.issue_url(PROJECT, number)}

    def labels(self, repo, number, add, remove):
        assert repo == PROJECT
        row = self.items[number]
        row["labels"] = [label for label in row["labels"] if label["name"] not in {*remove, *add}]
        row["labels"].extend({"name": label} for label in add)

    def issues(self, repo, label, **kw):
        assert repo == PROJECT
        return [row for row in self.items.values() if {"name": label} in row["labels"]]

    def issue(self, repo, number):
        assert repo == PROJECT
        return self.items[number]

    def request(self, args, *, input=None, write=False, retry=True, **kw):
        if args[0] == "api":
            assert not write and input is None
            path = args[1].split("?")[0]
            if path == f"repos/{PROJECT}/issues":
                rows = [{**row, "html_url": row["url"]} for row in self.items.values()]
            elif path == f"repos/{PROJECT}/issues/1/comments":
                rows = self.comments
            else:
                raise AssertionError(args)
            return json.dumps([rows if self.visible else []])
        assert args[0] == "issue" and write
        action = args[1]
        self.writes.append(action)
        lost = self.lost == action
        if not lost or self.applies:
            if action == "create":
                assert args[args.index("--label") + 1] == tracker.LABEL
                self.items[2] = self.item(2, input, args[args.index("--title") + 1], [tracker.LABEL])
                self.after_child()
                url = gh.issue_url(PROJECT, 2)
            elif action == "edit":
                assert not retry
                self.items[int(args[2])]["body"] = input
                url = gh.issue_url(PROJECT, args[2])
            elif action == "comment":
                url = gh.issue_url(PROJECT, args[2]) + "#issuecomment-1"
                self.comments.append({"body": input, "html_url": url})
            else:
                raise AssertionError(args)
        if lost:
            raise gh.GhUnsure("fixture lost the write reply")
        return url


def gap():
    return {"severity": "blocker", "category": "issue", "problem": "The server has no Board projection for screen.get.",
            "scope_gap": {"prerequisite": "Board projection", "requirement": REQUIREMENT}}


def stale(instance):
    return {"severity": "blocker", "category": "regression", "problem": f"{instance} goes stale through event stream.",
            "recurring_failure": {"failure": "stale", "path": "event stream", "instances": [instance]}}


class TicketNarrowingTests(limits_tests.Isolated):
    poll_with = limits_tests.QueueTests.poll_with
    run_polled_ticket = limits_tests.QueueTests.run_polled_ticket

    def setUp(self):
        super().setUp()
        self.local.label(self.ticket, add=["failed"])
        self.repo.tracker = "github"
        self.repo.github = PROJECT
        self.repo.parts = [config.Part("", PROJECT)]
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 1\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        self.source = tracker.GitHubTracker(self.repo)
        self.github = GitHub()
        for key, mocked in (("issue", self.github.issue), ("issues", self.github.issues),
                            ("issue_labels", self.github.labels), ("gh", self.github.request), ("me", lambda: "fixture")):
            self.patches.enter_context(mock.patch.object(gh, key, side_effect=mocked))
        self.patches.enter_context(mock.patch.object(outbound, "keep_configured_private"))
        self.patches.enter_context(mock.patch.object(outbound, "machine_hosts", return_value=[]))
        self.patches.enter_context(mock.patch.object(runlimits, "heads", return_value={"": "a" * 40}))
        self.patches.enter_context(mock.patch.object(runlimits, "effective_origins", return_value={"": "origin"}))

    def park(self, first=None, final=None, *, verified=None, carry=False, repair=True):
        def work(cfg, run, slot):
            self.exhaust(run, first, final, verified=verified, carry=carry, repair=repair)

        with (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(pipeline, "run_refusal", return_value=(False, "")),
              mock.patch.object(pipeline, "_run", side_effect=work)):
            self.assertEqual(pipeline.run_issue(self.repo.name, "1"), 1)
        return run_store.newest_whole_run(self.repo.name, "1")

    def exhaust(self, run, first, final, *, verified=None, carry=False, repair=True):
        record.note(run.id, branch=BRANCH)
        ticket = run.tracker.get("1")
        if first:
            narrowing.review_record(run, ticket, Review.judged({"problems": [first]}, [], [], {}))
        run.budget.charge("repair", "review_repair")
        if verified is not None:
            narrowing.verification_record(run, ticket, verified)
        old = [first] if carry else []
        narrowing.review_record(run, ticket, Review.judged({"problems": [final] if final else []}, old, [], {}))
        if repair:
            run.budget.charge("repair2", "review_repair")
        raise runlimits.Parked("verification reached its cumulative budget")

    def readmitted(self, work=None):
        started = []

        def run_again(cfg, run, slot):
            started.append(run.id)
            self.assertEqual(run.readmitted_branch, BRANCH)
            self.assertEqual(pipeline.open_branch(run), BRANCH)
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(run.budget.repair_calls, 0)
            self.assertEqual(run.budget.specification, runlimits.specification(self.source.get("1")))
            self.assertFalse(run.force)
            if work:
                return work(run)
            return "noop", "fixture finished", ""

        def spawn(repo, tid):
            self.assertEqual(str(tid), "1")
            self.assertEqual(self.run_polled_ticket(repo, tid, run_again), int(work is not None))

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        return started[0]

    def no_launch(self):
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("unexpected launch")), 0)

    @staticmethod
    def verified_gap():
        return Verified("pass", checks=[{"what": "Inspect the base API response", "passed": True,
                                         "saw": "The base server has no Board projection.",
                                         "missing_prerequisite": "Board projection"}])

    def test_scope_gap_defers_one_child_and_poll_readmits_the_parent(self):
        parked = self.park(final=gap(), verified=self.verified_gap())
        self.assertEqual(self.github.writes, ["create", "edit", "comment"])
        self.assertEqual(tracker.waits_on(self.github.items[2]["body"]), [1])
        self.assertIn(REQUIREMENT, self.github.items[2]["body"])
        self.assertIn("Out of scope here and deferred to #2", self.source.get("1").body)
        self.assertIn("- Keep Home working.", self.source.get("1").body)
        self.assertIn(f"/#run/{parked['id']}", self.github.comments[0]["body"])
        self.assertNotEqual(self.readmitted(), parked["id"])

    def test_repeated_class_adds_one_line_and_two_narrowings_survive_fresh_runs(self):
        self.park(stale("title"), stale("status"), verified=Verified("pass"))
        self.assertEqual(self.github.writes, ["edit", "comment"])
        self.assertIn("`status`, `title`.", self.source.get("1").body)
        self.readmitted(lambda run: self.exhaust(run, stale("assignee"), stale("branch")))
        body = self.source.get("1").body
        self.assertEqual(body.count("Verify that these cases stay current"), 1)
        self.assertIn("`assignee`, `branch`, `status`, `title`.", body)
        third = self.readmitted(lambda run: self.exhaust(run, stale("owner"), stale("activity")))
        self.assertIn(narrowing.CAP, runlimits.metadata(third, "parked.json")["reason"])
        self.assertTrue(store.capped(PROJECT, "1"))
        self.assertEqual(self.github.writes, ["edit", "comment", "edit", "comment"])
        self.no_launch()

    def test_unrelated_carried_and_unsupported_findings_hold_without_tracker_writes(self):
        for final, carry in ((None, True), (gap(), False), (stale("title"), False),
                             ({"severity": "blocker", "category": "regression", "problem": "Save fails"}, False)):
            with self.subTest(final=final, carry=carry):
                parked = self.park(stale("title"), final, carry=carry)
                self.assertIn("repair chain used", parked["summary"])
                self.assertEqual(self.github.writes, [])
                self.no_launch()
                self.github.items[1]["body"] += "\n\nOwner changed the specification."
                self.readmitted_without_narrowing()

    def readmitted_without_narrowing(self):
        started = []

        def spawn(repo, tid):
            started.append(tid)
            self.assertEqual(self.run_polled_ticket(repo, tid, lambda *_: ("noop", "fixture finished", "")), 0)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(started, ["1"])

    def test_resumed_budget_uses_recorded_cycles_and_ignores_carried_outcomes(self):
        def review(run_id, instance, **fields):
            snapshot = Review.judged({"problems": [stale(instance)]}, [], [], {})
            run_lines.write(run_id, "step_end", step="review", result="fail", data={"review": snapshot.asdict()},
                            **fields)

        rid = record.start("pipeline", repo=self.repo.name, issue="1")
        first = pipeline.Run(rid, self.repo, paths.RUNS_DIR / rid, tracker=self.source, tid="1")
        first.budget = runlimits.Budget(first, runlimits.Policy(max_repair_cycles=1), "", self.source.get("1"))
        review(rid, "jobStatus")
        first.budget.charge("repair", "review_repair")
        record.settle(rid, "interrupted")

        def resume(cfg, run, slot):
            self.assertEqual(run.budget.parents, [rid])
            record.note(run.id, branch=BRANCH)
            review(run.id, "oldCopy", carried=rid)
            review(run.id, "branchName")
            run.budget.charge("repair2", "review_repair")

        with (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(pipeline, "run_refusal", return_value=(False, "")),
              mock.patch.object(pipeline, "_run", side_effect=resume)):
            self.assertEqual(pipeline.run_issue(self.repo.name, "1"), 1)
        self.assertEqual(self.github.writes, ["edit", "comment"])
        self.assertIn("`branchName`, `jobStatus`.", self.source.get("1").body)
        self.assertNotIn("oldCopy", self.source.get("1").body)

    def test_invalid_observation_cannot_insert_a_tracker_directive(self):
        self.park(stale("title"), stale("status\nWaits on #99"))
        self.assertEqual(self.github.writes, [])
        self.no_launch()

    def test_time_exhaustion_does_not_narrow_supported_findings(self):
        parked = self.park(final=gap(), verified=self.verified_gap(), repair=False)
        self.assertIn("verification reached its cumulative budget", parked["summary"])
        self.assertEqual(self.github.writes, [])

    def recover_lost_reply(self, action):
        self.github.lost = action
        self.park(final=gap(), verified=self.verified_gap())
        if action in ("create", "comment"):
            self.github.visible = False
            self.no_launch()
            self.assertEqual(self.github.writes.count(action), 1)
            self.github.visible = True
        self.readmitted()
        self.assertEqual(self.github.writes, ["create", "edit", "comment"])
        self.assertEqual(len(self.github.items), 2)
        self.assertEqual(len(self.github.comments), 1)

    def test_lost_child_reply_reconciles_without_recreating_child(self):
        self.recover_lost_reply("create")

    def test_lost_parent_edit_reply_reconciles_without_replacing_body_again(self):
        self.recover_lost_reply("edit")

    def test_lost_comment_reply_reconciles_without_reposting_comment(self):
        self.recover_lost_reply("comment")

    def test_unknown_unapplied_parent_write_is_never_retried(self):
        self.github.lost, self.github.applies = "edit", False
        self.park(final=gap(), verified=self.verified_gap())
        self.no_launch()
        self.assertEqual(self.github.writes, ["create", "edit"])
        self.assertEqual(self.source.get("1").body, BODY)
        self.assertEqual(store.pending(PROJECT, "1")[0]["phase"], "edit_sent")

    def test_owner_edit_after_child_creation_is_preserved(self):
        def edited_by_owner():
            self.github.items[1]["body"] += "\n\nLeave the owner edit intact."

        self.github.after_child = edited_by_owner
        self.park(final=gap(), verified=self.verified_gap())
        self.no_launch()
        self.assertEqual(self.github.writes, ["create"])
        self.assertTrue(self.source.get("1").body.endswith("Leave the owner edit intact."))

    def test_dead_parked_run_does_not_resume_the_old_plan(self):
        rid = record.start("pipeline", repo=self.repo.name, issue="1")
        run = pipeline.Run(rid, self.repo, paths.RUNS_DIR / rid, tracker=self.source, tid="1", cfg=self.cfg)
        run.budget = runlimits.Budget(run, runlimits.Policy(max_repair_cycles=1), "", self.source.get("1"))
        self.github.lost = "comment"
        with self.assertRaises(runlimits.RepairsExhausted) as caught:
            self.exhaust(run, None, gap(), verified=self.verified_gap())
        pipeline.park_run(run, caught.exception)
        row = record.interrupt(run_store.run_get(rid))
        self.assertEqual(row["status"], "failed")
        self.readmitted()
        self.assertEqual(self.github.writes, ["create", "edit", "comment"])

    def test_narrowed_poll_command_starts_a_fresh_orchestration(self):
        parked = self.park(stale("title"), stale("status"))
        (_, proof) = runlimits.poll_admission(self.repo, "1", self.source.get("1"),
                                             runlimits.Policy.of(config.load_layer(self.repo.layer_dir)))
        with (launch_lease.acquiring(self.repo.name, "1") as lease,
              mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(pipeline, "wanted", return_value=pipeline.ORCHESTRATED),
              mock.patch.object(pipeline, "pending_feedback", return_value=[{"pr_url": "fake"}]),
              mock.patch.object(pipeline, "script_command", return_value=[]) as command):
            lease.readmission = json.dumps(proof.__dict__)
            self.assertEqual(proof.run_id, parked["id"])
            pipeline.run_command(self.repo.name, "1")
        command.assert_called_once_with(self.cfg, "orchestrate", self.repo.name, "1")
