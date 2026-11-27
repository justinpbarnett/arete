import os
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from arete import board, config, control, gh, merge_review, orchestration, pipeline, poll, run_lines, slots
from arete.commands.common import print_ticket_progress
from arete.store import db, runs, sources, ticket_progress
from arete.store import orchestration as ledger
from tests.test_run_timing import DRAFT, TimingFixture, clock, stamp
from tests.test_ticket_progress import GITHUB, HEAD, REPO, TICKET, admit, poll_dependencies

RUN = "pip-held"
DEPLOYED = f"https://github.com/{GITHUB}/pull/49"
MERGED = f"https://github.com/{GITHUB}/pull/50"
OWNER_REVIEW = {"state": "owner-review", "prs": [DRAFT], "draft": True}


class ProgressSurfacesTests(TimingFixture):
    def setUp(self):
        super().setUp()
        self.cfg = config.Config(repos=[config.Repo(REPO, self.root, github=GITHUB)])
        self.enterContext(patch.object(config, "load_or_problem", return_value=(self.cfg, "")))
        self.enterContext(patch.object(board, "RECENT_RUNS", 0))
        self.enterContext(patch.object(control, "BEAT_FILE", self.root / "serve-beat.json"))
        self.enterContext(patch.object(slots, "holding", return_value=[]))

    def progress(self, rid, at, **expected):
        self.now.return_value = clock(at)
        row, = board.live_board(None, include_ticket_progress=True)["ticket_progress"]["rows"]
        self.assertEqual(board.run_board(rid, include_ticket_progress=True)["ticket_progress"], row)
        return self.fields(row, **expected)

    def environment_hold(self, **fields):
        self.run_row(RUN)
        self.settle(RUN, 22, "pr", pr_url=DRAFT)
        runs.save_environment_hold(RUN, [(GITHUB, 52, DRAFT, HEAD)])
        ticket_progress.record(REPO, TICKET, {"run": RUN, "attempt": RUN, "next_event": "Verification can retry",
            "blocker": {"causes": {"tool": "Tool is missing"}}, "follow_up": dict(OWNER_REVIEW, state="waiting")}, **fields)

    def test_progress_is_opt_in_and_status_agrees_with_run_timing(self):
        self.run_row(RUN)
        self.step(RUN, "build", 2, 8)
        self.assertNotIn("ticket_progress", board.run_board(RUN))
        self.assertNotIn("ticket_progress", board.live_board(None))
        row = self.progress(RUN, 10)
        self.assertEqual(board.snapshot()["ticket_progress"]["rows"], [row])
        self.fields(row["timing"], run_ids=[RUN], elapsed_seconds=10, unattributed_seconds=4)

    def test_status_labels_shipped_prerequisites_separately_from_the_draft(self):
        reads = {f"{GITHUB}#42": [{"key": f"{GITHUB}#{number}", "url": url, "state": "merged",
                                      "merge_commit": HEAD, "deployed_release": release}
                                  for number, url, release in ((49, DEPLOYED, HEAD), (50, MERGED, ""))]}
        self.environment_hold(prerequisite_reads=reads)
        row = self.progress(RUN, 30, blocker="Tool is missing", next_event="Verification can retry")
        with redirect_stdout(StringIO()) as output:
            print_ticket_progress([row])
        for line in (f"Deployed prerequisite: {DEPLOYED}", f"Merged prerequisite (deployment not confirmed): {MERGED}",
                     f"Draft follow-up: {DRAFT}"):
            self.assertIn(line, output.getvalue())

    def test_discovered_prerequisite_reaches_status_at_capacity_without_a_run(self):
        for _ in range(2):
            self.assertEqual(poll_dependencies(self.root, "open", at_capacity=True), {})
        row, = board.live_board(None, include_ticket_progress=True)["ticket_progress"]["rows"]
        self.fields(row, issue="965", run="", follow_up=None)
        self.assertEqual([u["message"] for u in row["updates"]], [f"Found prerequisite PR {DEPLOYED} (open)."])

    def test_retry_names_ci_wait_then_the_active_check(self):
        self.environment_hold()
        self.run_row("retry", 35, environment_retry_of=RUN)
        draft = dict(OWNER_REVIEW, state="verifying")
        event = "The saved verification reports its result"
        ticket_progress.record(REPO, TICKET, {"attempt": "retry", "follow_up": draft, "next_event": event, "blocker": None})
        self.progress("retry", 35, blocker="", next_event=event, follow_up=draft)
        self.wait("retry", 36, cause="ci")
        self.progress("retry", 37, blocker="CI is pending", next_event="CI reports its result", follow_up=draft)
        self.wait("retry", 38, since=36, cause="ci")
        self.check("retry", 39, "verify", check_no=1, name="unit")
        self.progress("retry", 40, activity="Running check: unit", blocker="",
                      next_event="An active check reports its result", follow_up=draft)

    def test_legacy_owner_review_uses_first_matching_receipt_and_ends_at_merge(self):
        self.environment_hold()
        self.run_row("retry", 35, environment_retry_of=RUN)
        self.settle("retry", 47, "pr", pr_url=DRAFT)
        for at, attempted in ((48, "other"), (50, "retry"), (52, "retry")):
            self.at(at, ticket_progress.record, REPO, TICKET,
                    {"attempt": attempted, "follow_up": OWNER_REVIEW, "next_event": f"Owner reviews the draft ({at})"})
        with db.tx() as c:
            c.execute("UPDATE environment_holds SET released = 1 WHERE run_id = ?", (RUN,))
        listed, = runs.environment_holds(REPO)
        for hold in (listed, runs.environment_hold(RUN)):
            self.fields(hold, release_state="owner-review", owner_review_started_at=stamp(50), released_at=None)
        self.progress("retry", 55, next_event="Owner reviews the draft (52)")
        self.move("retry", 63, "merged", "pr", finished=stamp(60))
        self.project("retry", 70, RUN, elapsed_seconds=60, blockers=[], categories={"queue_wait": 13, "owner_wait": 10})

    def test_saved_adaptive_catch_up_hold_names_the_repair(self):
        self.run_row(RUN)
        reason = "Catch-up to `main` at `new-base` held on checks: unit checks failed"
        ref = orchestration.CATCH_UP_FAILURE
        lease = self.at(2, ledger.acquire, RUN, "", {})
        action, = admit(lease, {"op": "publication.request", "args": {}})
        aid = action["id"]
        self.at(3, ledger.transition, lease, aid, "running", {})
        self.at(4, ledger.save, lease, {"catch_up_hold": {
            "action": aid, "gate": "checks", "reason": reason, "feedback": "Internal check output"}})
        self.progress(RUN, 4, blocker=reason, next_event=f"Delegate a repair using {ref}, then rerun the publication gates.")
        instruction = f"Delegate a repair using {ref}, then repeat the publication gates."
        self.at(5, ledger.transition, lease, aid, "failed", {"failures": ref, "error": f"{reason}. {instruction}"})
        row = self.progress(RUN, 6, activity="Publication catch-up held", blocker=reason, next_event=instruction)
        self.assertNotIn("Internal check output", str(row))
        self.at(7, ledger.save, lease, {})
        self.progress(RUN, 8, blocker="")

    def test_merge_hold_names_the_specific_next_event(self):
        cases = ((merge_review.PENDING, "The issue orchestrator records its merge decision"),
                 ("GitHub has no fully passing CI result for the verified head", "CI passes on the verified head"),
                 ("the PR base changed after verification", "The PR is verified against its current base"),
                 ("catch-up CI held: unit failed", "CI passes on the catch-up head"))
        for index, (reason, event) in enumerate(cases):
            with self.subTest(reason=reason):
                rid, start = f"{RUN}-{index}", index * 20
                self.run_row(rid, start)
                self.settle(rid, start + 10, "pr", pr_url=DRAFT, report={"merge_held": reason})
                self.progress(rid, start + 11, activity="Follow-up PR held at merge", blocker=reason, next_event=event)

    def test_tracker_answer_can_be_waiting_for_a_run_slot(self):
        self.run_row(RUN)
        self.log(RUN, 5, "question", summary="Which release?")
        self.settle(RUN, 6, "needs-info")
        self.progress(RUN, 6, activity="Waiting for the owner", blocker="Which release?")
        answer = run_lines.path(RUN).with_name(pipeline.ANSWER_FILE)
        answer.write_text("Next release")
        os.utime(answer, (clock(21).timestamp(),) * 2)
        self.at(21, sources.pass_over, REPO, TICKET)
        self.progress(RUN, 30, activity="Owner answered", blocker="No run slot was available",
                      next_event="A run slot becomes available")
        self.project(RUN, 30, elapsed_seconds=30, categories={"owner_wait": 16, "queue_wait": 9})

    def test_held_draft_stays_visible_until_confirmed_ready(self):
        self.run_row(RUN)
        self.message(RUN, 10, f"opened {DRAFT} as draft")
        self.settle(RUN, 12, "pr", pr_url=DRAFT, report={"held": "Review requested changes"})
        with patch.object(gh, "pr_merge_details", side_effect=[{"state": "OPEN"}, {"state": "OPEN", "isDraft": False}]):
            self.at(30, poll.observe_drafts, self.cfg)
            self.progress(RUN, 30, next_event="The owner addresses the blocker or takes over the draft", follow_up=OWNER_REVIEW)
            self.at(35, poll.observe_drafts, self.cfg)
        self.assertEqual(board.live_board(None, include_ticket_progress=True)["ticket_progress"]["rows"], [])
        self.project(RUN, 40, elapsed_seconds=35, blockers=[], categories={"owner_wait": 25})
