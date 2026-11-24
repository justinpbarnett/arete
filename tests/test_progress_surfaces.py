import os
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from arete import board, config, control, gh, outcomes, pipeline, poll, run_lines, slots
from arete.commands.common import print_ticket_progress
from arete.store import asks, runs, sources, ticket_progress
from tests.test_run_timing import DRAFT, TimingFixture, clock, stamp
from tests.test_ticket_progress import GITHUB, HEAD, REPO, TICKET, poll_dependencies

ISSUE, RUN = str(TICKET), "pip-held"
READY = dict(state="OPEN", isDraft=False)
DEPLOYED = "https://github.com/owner/arete/pull/49"
MERGED = "https://github.com/owner/arete/pull/50"
NEXT_RELEASE = "The live release is confirmed to contain the fixes"
DRAFT_REVIEW = dict(state="owner-review", prs=[DRAFT], draft=True)
REVIEW_REASON = "Review requested changes"


class ProgressSurfacesTests(TimingFixture):
    def setUp(self):
        super().setUp()
        repo = config.Repo(REPO, self.root, github=GITHUB)
        self.cfg = config.Config(repos=[repo])
        self.enterContext(patch.object(config, "load_or_problem", return_value=(self.cfg, "")))
        self.enterContext(patch.object(control, "BEAT_FILE", self.root / "serve-beat.json"))
        self.enterContext(patch.object(slots, "holding", return_value=[]))
        self.enterContext(redirect_stdout(StringIO()))
        self.ticket = Mock(id=ISSUE, url="", title="Time")
        self.source = Mock(ref=lambda _: f"#{ISSUE}", get=lambda _: self.ticket, blocker=lambda _: "",
                           reply_since=lambda *_: "Next release")
        self.queue = poll.RepoQueue(repo, self.source, set(), config.RepoLayer(self.root), Mock())

    def sync_ready(self, at, *pulls):
        with patch.object(outcomes, "sync_outcomes"), patch.object(gh, "pr_merge_details", side_effect=pulls) as read:
            self.at(at, poll.sync_pr_outcomes, self.cfg)
        return read

    def progress(self, run_id, at, **expected):
        row, = self.at(at, self.status)
        self.assertEqual(board.run_board(run_id)["ticket_progress"], row)
        return self.fields(row, **expected)

    def status(self):
        return board.live_board(None)["ticket_progress"]["rows"]

    def assert_cli(self, rows, *lines):
        with redirect_stdout(StringIO()) as output:
            print_ticket_progress(rows)
        self.assertFalse([line for line in lines if line not in output.getvalue()])

    def test_status_and_run_share_lineage_timing_and_separate_deployment_from_follow_up(self):
        self.run_row("root", steps=[("build", 2, 8), ("implement", 4, 10)])
        self.settle("root", 12, "interrupted")
        self.run_row(RUN, 15, parent=("route", "continues", "root"), steps=[("verify", 16, 20)])
        self.settle(RUN, 22, "pr", pr_url=DRAFT)
        self.hold_environment(RUN)
        reads = {f"{GITHUB}#{i}": [dict(key=f"{GITHUB}#{i + 7}", url=url, state="merged",
                                       merge_commit=HEAD, deployed_release=release)]
                 for i, url, release in ((42, DEPLOYED, HEAD), (43, MERGED, ""))}
        ticket_progress.record(REPO, ISSUE, dict(run=RUN, attempt=RUN, next_event=NEXT_RELEASE,
            blocker=dict(kind="verification-environment", causes={"tool": "Tool is missing"}),
            follow_up=dict(DRAFT_REVIEW, state="waiting")), prerequisite_reads=reads)
        self.run_row("dry", 25, environment_retry_of=RUN, status="dry-run", dry_run=1)
        held = self.progress(RUN, 30, activity="Draft waiting to retry verification", blocker="Tool is missing", next_event=NEXT_RELEASE)
        timing = self.fields(held["timing"], run_ids=["root", RUN], elapsed_seconds=30, unattributed_seconds=10, overlap_seconds=4)
        self.assertEqual(timing, board.run_board("root")["ticket_progress"]["timing"])
        self.assertEqual(board.snapshot()["ticket_progress"]["rows"], [held])
        self.assert_cli([held], "Blocker: Tool is missing", f"Next: {NEXT_RELEASE}", f"Deployed prerequisite: {DEPLOYED}",
                        f"Merged prerequisite (deployment not confirmed): {MERGED}", f"Draft follow-up: {DRAFT}", *timing["lines"])
        self.retry_environment_hold()

    def retry_environment_hold(self):
        retry, draft = "retry", dict(DRAFT_REVIEW, state="verifying")
        self.run_row(retry, 35, environment_retry_of=RUN)
        self.wait(retry, 35, since=34)
        self.step(retry, "verify", 36)
        scope = {"resource": "check-slot", "suite": "suite", "step": "verify", "try": 1}
        for start, end, blocker, event, wait in (
            (37, 39, "CI is pending", "CI reports its result", dict(cause="ci")),
            (40, 43, "A check slot is unavailable", "A check slot becomes available", scope),
        ):
            self.wait(retry, start, **wait)
            self.progress(retry, start, activity="Waiting", blocker=blocker, next_event=event, follow_up=draft)
            self.wait(retry, end, since=start, **wait)
        self.progress(retry, 43, activity="Running verify", blocker="")
        self.log(retry, 44, "step_end", step="verify")
        self.settle(retry, 47, "pr", pr_url=DRAFT)
        event = "Owner reviews the draft and decides how to unblock verification"
        ticket_progress.record(REPO, ISSUE, dict(attempt=retry, next_event=event, follow_up=DRAFT_REVIEW))
        self.at(50, runs.release_environment_hold, RUN, "owner-review")
        with patch.object(board, "RECENT_RUNS", 0):
            self.progress(retry, 55, activity="Draft awaiting owner review after automatic retry", next_event=event)
            self.move(retry, 63, "merged", "pr", finished=stamp(60))
            self.assertEqual(self.status(), [])
        self.project(retry, 70, RUN, elapsed_seconds=60, blockers=[],
                     categories=dict(queue_wait=18, owner_wait=13, writing=8, checks=9))

    def test_prerequisite_discovery_reaches_status_at_capacity_without_a_run_or_skip(self):
        for state, visible in (("open", True), ("partial", True), ("removed", False), ("open", True), ("closed", False)):
            self.assertEqual(poll_dependencies(self.root, state, at_capacity=True), {})
            rows = self.status()
            self.assertEqual(len(rows), int(visible), state)
            for row in rows:
                self.fields(row, issue="965", run="", blocker="", follow_up=None)
                self.assertEqual([u["message"] for u in row["updates"]], [f"Found prerequisite PR {DEPLOYED} (open)."])

    def pick_answer(self, at, *, slots_free, expected):
        skips = pipeline.SkipLog(pipeline.skipped_tickets())
        with patch.object(poll.tracker, "for_ticket", return_value=self.source):
            self.assertEqual(self.at(at, poll.start_pick, self.cfg, self.queue, poll.Pick(ISSUE, "Answer received"),
                                     skips, False, Counter(), slots_free), expected)
        poll.save_skips(skips)
        answer = run_lines.path(RUN).with_name(pipeline.ANSWER_FILE)
        if answer.exists():
            os.utime(answer, (clock(21).timestamp(),) * 2)

    def question(self, *, dashboard=False):
        self.run_row(RUN)
        question = "Which release should we target?"
        if dashboard:
            self.at(5, asks.ask_add, "ask", RUN, REPO, ISSUE, "plan", "plan", question, "", [], {})
        else:
            self.log(RUN, 5, "question", summary=question)
        self.settle(RUN, 6, "needs-info")
        with patch.object(board, "RECENT_RUNS", 0):
            self.progress(RUN, 6, activity="Waiting for the owner", blocker="An owner answer is needed" if dashboard else question)

    def continue_after_answer(self, at):
        self.run_row("continued", at, parent=("answer", "asked_in", RUN))
        pipeline.opening_waits("continued", runs.run_get(RUN), sources.take_passed_over(REPO, ISSUE))

    def test_classic_tracker_answer_names_the_recorded_run_slot_blocker(self):
        self.question()
        self.pick_answer(21, slots_free=0, expected="held")
        blocker, event = "No run slot was available", "A run slot becomes available"
        queued = self.progress(RUN, 30, activity="Owner answered", blocker=blocker, since=stamp(21), next_event=event)
        self.project(RUN, 30, elapsed_seconds=30, categories=dict(owner_wait=16, queue_wait=9))
        self.assert_cli([queued], f"Blocker: {blocker}", f"Next: {event}")
        self.continue_after_answer(32)
        self.progress("continued", 32, activity="Running", blocker="")
        self.project("continued", 32, elapsed_seconds=32, categories=dict(owner_wait=16, queue_wait=11))

    def test_classic_tracker_question_and_post_answer_provider_wait(self):
        self.question()
        self.assert_provider_wait(owner_seconds=16)
        self.continue_after_answer(92)
        categories = dict(owner_wait=16, queue_wait=70)
        self.project("continued", 93, elapsed_seconds=93, blockers=[], categories=categories)
        self.at(94, poll.save_skips, pipeline.SkipLog(pipeline.skipped_tickets()))
        self.settle("continued", 95, "done")
        self.project("continued", 100, elapsed_seconds=95, blockers=[], categories=categories)

    def test_answered_dashboard_ask_names_the_provider_route_blocker(self):
        self.question(dashboard=True)
        self.assertTrue(self.at(10, asks.ask_close, "ask", "answered"))
        self.assert_provider_wait(owner_seconds=5)

    def assert_provider_wait(self, *, owner_seconds):
        reason = "plan has no eligible route: subscription is unavailable"
        with (patch.object(poll.runlimits, "poll_admission", return_value=("", None)),
              patch.object(poll.intake, "route_ticket", return_value=self.ticket),
              patch.object(poll, "step_without_route", return_value=reason)):
            for at in (22, 30):
                self.pick_answer(at, slots_free=1, expected="blocked")
        for at in (40, 90):
            self.progress(RUN, at, activity="Owner answered", blocker=reason, since=stamp(22),
                          next_event="An eligible provider route becomes available")
            self.project(RUN, at, elapsed_seconds=at, categories=dict(owner_wait=owner_seconds, queue_wait=at - 22))

    def draft_case(self, repair_status=None):
        prs = [DRAFT] if repair_status else [DRAFT, "https://github.com/owner/other/pull/19"]
        self.run_row(RUN)
        self.publish_draft(RUN, 10, settled=12, prs=" ".join(prs), held=REVIEW_REASON)
        current = receipt = RUN
        activity, blocker, event = "Draft held at publication", REVIEW_REASON, "The owner addresses the blocker or takes over the draft"
        if repair_status:
            current = "repair"
            self.run_row(current, 15, parent=("repair", "source", RUN))
            self.step(current, "repair", 16, 19)
            if repair_status == "pr":
                self.move(RUN, 20, "superseded", "pr")
                self.publish_draft(current, 20, review={"verdict": "approve"})
                receipt = current
                activity, blocker, event = ("Draft awaiting readiness decision", "The repair review approved it, but the PR is still a draft",
                                            "The owner marks the draft ready for review")
            else:
                self.settle(current, 20, "failed", pr_url=DRAFT, report={"prs": prs})
        for i in range(board.RECENT_RUNS):
            self.run_row(f"newer-{i}", 29, status="done", issue="965", finished=stamp(29))
        held = self.progress(current, 30, activity=activity, blocker=blocker, next_event=event,
                             follow_up=dict(DRAFT_REVIEW, prs=prs))
        retry = 3 if repair_status else 0
        owner = 15 if repair_status else 20
        self.project(current, 30, RUN, elapsed_seconds=30, unattributed_seconds=30 - owner - retry,
                     overlap_seconds=0, categories=dict(owner_wait=owner, retry=retry))
        if not repair_status:
            for partial in ({}, {"state": "OPEN"}, dict(READY, isDraft=True), gh.GhError("unreadable")):
                self.sync_ready(30, READY, partial)
                self.assertEqual(self.status(), [held])
        read = self.sync_ready(35, *([READY] * len(prs)))
        if repair_status:
            read.assert_called_once_with(DRAFT, timeout=15, retry=False)
        self.sync_ready(40).assert_not_called()
        self.assertEqual(self.status(), [])
        self.assertEqual(runs.draft_review_ready_at(receipt), stamp(35))
        self.fields(runs.run_get(receipt), status="pr")
        cleared = board.run_board(current)["ticket_progress"]
        self.fields(cleared, activity="Last run failed" if repair_status == "failed" else "Follow-up PR recorded", blocker="")
        if not repair_status:
            self.fields(cleared, follow_up=dict(state="pr", prs=prs, draft=None), next_event="Review, merge, or new feedback")
            self.move(RUN, 51, "merged", "pr", finished=stamp(45))
        self.project(current, 60, elapsed_seconds=35, blockers=[], overlap_seconds=0,
                     categories=dict(owner_wait=owner + 5, retry=retry))

    def test_ordinary_review_held_draft_outlives_recent_history_until_ready_is_confirmed(self):
        self.draft_case()

    def test_successful_repair_waits_for_the_owner_to_make_the_draft_ready(self):
        self.draft_case("pr")

    def test_failed_repair_without_publication_does_not_hide_the_original_draft(self):
        self.draft_case("failed")
