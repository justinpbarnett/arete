import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from arete import board, config, control, dbstats, paths, pipeline, run_lines, slots, util
from arete.commands.setup import print_status
from arete.store import asks, db, events, orchestration, runs, sources, ticket_progress
from tests.support import workspace_state
from tests.test_run_timing import clock, stamp
from tests.test_ticket_progress import poll_dependencies

REPO = "arete"
ISSUE = "964"
RUN = "pip-held"
AT = "2026-11-24T01:00:00Z"
DRAFT = "https://github.com/owner/arete/pull/52"
DEPLOYED = "https://github.com/owner/arete/pull/49"
MERGED = "https://github.com/owner/arete/pull/50"
ROOT_RUN = "pip-root"
RETRY_RUN = "pip-retry"
AS_OF = datetime(2026, 11, 24, 3, tzinfo=UTC)
DEPLOYMENT_NEXT = "The live release is confirmed to contain the fixes"
REVIEW_NEXT = "Owner reviews the draft and decides how to unblock verification"


class ProgressSurfacesTests(unittest.TestCase):
    def setUp(self):
        parent = paths.scratch_root(paths.STATE_DIR) if paths.ISOLATED else None
        folder = self.enterContext(tempfile.TemporaryDirectory(prefix="arete-progress-surfaces-", dir=parent))
        self.root = Path(folder).resolve()
        self.cfg = config.Config(repos=[config.Repo(REPO, self.root, github="owner/arete")])
        self.enterContext(workspace_state(self.root))
        self.enterContext(patch.object(control, "BEAT_FILE", self.root / "serve-beat.json"))
        self.enterContext(patch.object(dbstats.Timing, "finished"))
        self.now = self.enterContext(patch.object(util, "now", return_value=AS_OF))
        self.enterContext(patch.object(config, "load_or_problem", return_value=(self.cfg, "")))
        self.skips = {}
        self.enterContext(patch.object(pipeline, "skipped_tickets", return_value=self.skips))
        self.holding = self.enterContext(patch.object(slots, "holding", return_value=[]))
        db.migrate()

    def insert(self, run_id, status, **extra):
        runs.insert_run(run_id, {"kind": "pipeline", "repo": REPO, "issue": ISSUE, "status": status,
                                 "started": AT, **extra})

    def hold(self):
        runs.save_environment_hold(RUN, [("owner/arete", 52, DRAFT, "a" * 40)])
        state = {"run": RUN, "attempt": RUN, "blocker": {"kind": "verification-environment",
                                                        "causes": {"tool": "Tool is missing"}},
                 "next_event": "Prerequisites reach the live release",
                 "follow_up": {"state": "waiting", "prs": [DRAFT], "draft": True}}
        reads = {f"owner/arete#{i}": [{"key": f"owner/arete#{i + 7}", "url": url, "state": "merged",
                                      "merge_commit": "b" * 40, "deployed_release": release}]
                 for i, url, release in ((42, DEPLOYED, "c" * 40), (43, MERGED, ""))}
        ticket_progress.record(REPO, ISSUE, state, prerequisite_reads=reads)
        ticket_progress.record(REPO, ISSUE, {**state, "next_event": DEPLOYMENT_NEXT})

    def owner_review(self):
        self.at(5)
        self.insert(RUN, "pr", started=stamp(0), finished=stamp(5), pr_url=DRAFT)
        self.hold()
        self.at(75)
        self.insert(RETRY_RUN, "pr", started=stamp(60), finished=stamp(70), pr_url=DRAFT, environment_retry_of=RUN)
        with db.tx():
            ticket_progress.record(REPO, ISSUE, {
                "attempt": RETRY_RUN, "next_event": REVIEW_NEXT,
                "follow_up": {"state": "owner-review", "prs": [DRAFT], "draft": True},
            })
            runs.release_environment_hold(RUN, "owner-review")
        self.at(100)
        self.insert("pip-unrelated", "done", issue="967", started=stamp(80), finished=stamp(81))

    def status_progress(self):
        dashboard = board.live_board(None)["ticket_progress"]
        self.assertEqual(board.snapshot()["ticket_progress"], dashboard)
        return dashboard["rows"]

    def queue(self):
        sources.pass_over(REPO, "965")
        self.skips["arete#966"] = {"repo": REPO, "id": "966", "why": "Depends on #1000", "since": AT}

    def at(self, seconds):
        self.now.return_value = clock(seconds)

    def log(self, run_id, seconds, event, **fields):
        self.at(seconds)
        with redirect_stdout(StringIO()):
            run_lines.write(run_id, event, **fields)

    def accept(self, lease, *ops):
        turn = orchestration.begin(lease, {}, None)
        version = orchestration.snapshot(lease)["version"]
        response = {"calls": [{"op": op, "args": {}} for op in ops]}
        return [row["id"] for row in orchestration.accept(lease, turn, version, response, None)]

    def transition(self, lease, action, seconds, state, **result):
        self.at(seconds)
        orchestration.transition(lease, action, state, result)

    def settle(self, run_id, seconds, status, **fields):
        self.at(seconds)
        self.assertIsNotNone(runs.settle(run_id, {"status": status, "finished": stamp(seconds), **fields}))

    def store_timed_lineage(self):
        self.at(0)
        self.insert(ROOT_RUN, "running", started=stamp(0))
        first = orchestration.acquire(ROOT_RUN, "", {})
        left, right = self.accept(first, "worker.start", "worker.start")
        self.transition(first, left, 2, "running", writing=True, role="build")
        self.transition(first, right, 4, "running", writing=True, role="build")
        self.transition(first, left, 8, "succeeded")
        self.transition(first, right, 10, "succeeded")
        self.settle(ROOT_RUN, 12, "interrupted")
        self.at(15)
        self.insert(RUN, "running", started=stamp(15))
        self.log(RUN, 15, "route", continues=ROOT_RUN)
        self.log(RUN, 16, "step_start", step="verify")
        self.log(RUN, 20, "step_end", step="verify")
        self.settle(RUN, 22, "pr", pr_url=DRAFT)

    def test_status_and_run_share_stored_lineage_timing_and_preserve_durable_updates(self):
        self.store_timed_lineage()
        self.hold()
        self.at(30)
        self.queue()
        self.assertEqual(ticket_progress.get(REPO, ISSUE)["updates"], [])
        with db.reader() as c:
            before = events.metadata(c)["head"]
        detail = board.run_board(RUN)
        dashboard = board.live_board(None)
        cli = board.snapshot()
        ancestor = board.run_board(ROOT_RUN)["ticket_progress"]
        with db.reader() as c:
            self.assertEqual(events.metadata(c)["head"], before)
        rows = {r["issue"]: r for r in dashboard["ticket_progress"]["rows"]}
        held = rows[ISSUE]
        self.assertEqual(held, detail["ticket_progress"])
        self.assertEqual(dashboard["ticket_progress"], cli["ticket_progress"])
        timing = held["timing"]
        self.assertEqual(timing, ancestor["timing"])
        self.assertEqual(timing["run_ids"], [ROOT_RUN, RUN])
        self.assertEqual(timing["as_of"], stamp(30))
        self.assertTrue(timing["elapsed_complete"])

        def timing_entries(text):
            return [entry.strip() for entry in text.replace("·", "\n").splitlines() if entry.strip()]

        expected_timing = ["Run lineage elapsed: 30s", "Writing: 8s", "Checks / verification: 4s",
                           "Review: 0s", "Owner wait: 0s", "Queue wait: 8s", "Retry: 0s", "Unattributed: 10s",
                           "Overlap: 4s (already counted in elapsed)", "Writing ×2: 4s"]
        self.assertEqual(timing_entries("\n".join(timing["lines"])), expected_timing)
        self.assertEqual(held["activity"], "Draft waiting to retry verification")
        self.assertEqual(held["blocker"], "Tool is missing")
        self.assertEqual(held["next_event"], DEPLOYMENT_NEXT)
        self.assertEqual(held["follow_up"]["prs"], [DRAFT])
        self.assertEqual([(p["url"], bool(p["deployed_release"])) for p in held["prerequisites"]],
                         [(DEPLOYED, True), (MERGED, False)])
        self.assertEqual([u["kind"] for u in held["updates"]], ["prerequisite-pr", "prerequisite-pr"])
        self.assertEqual(dashboard["live"]["rows"]["runs"], [])
        self.assertEqual(rows["965"]["run"], "")
        self.assertEqual(rows["965"]["next_event"], "A run slot becomes available")
        self.assertEqual(rows["966"]["run"], "")
        self.assertEqual(rows["966"]["blocker"], "Depends on #1000")
        self.assertIsNone(rows["965"]["timing"])
        self.assertIsNone(rows["966"]["timing"])

        rendered = StringIO()
        with redirect_stdout(rendered):
            print_status(self.cfg, "", [], cli, {"rows": {"oldest": "", "pins": 0}}, "")
        text = rendered.getvalue()
        for line in ("Blocker: Tool is missing", f"Next: {DEPLOYMENT_NEXT}", f"Deployed prerequisite: {DEPLOYED}",
                     f"Merged prerequisite (deployment not confirmed): {MERGED}", f"Draft follow-up: {DRAFT}",
                     "arete#965: Queued for a run slot", "arete#966: Blocked before start"):
            self.assertIn(line, text)
        cli_entries = timing_entries(text)
        for entry in expected_timing:
            self.assertIn(entry, cli_entries)
        self.assertEqual(text.count("Found prerequisite PR"), 2)

    def test_current_waits_clear_independently_on_run_and_status_surfaces(self):
        self.at(1)
        self.insert(RUN, "pr", pr_url=DRAFT, started=stamp(0), finished=stamp(1))
        self.hold()
        current = "pip-next"
        self.insert(current, "running", started=stamp(2))
        sources.pass_over(REPO, ISSUE)
        self.log(current, 3, "step_start", step="verify")

        def activity(row):
            return row["activity"], row["blocker"], row["next_event"]

        def progress(expected):
            row, = self.status_progress()
            self.assertEqual(board.run_board(current)["ticket_progress"], row)
            self.assertEqual(activity(row), expected)
            return row

        self.log(current, 4, "wait", cause="ci", phase="start", at=stamp(4))
        waiting = progress(("Waiting", "CI is pending", "CI reports its result"))
        scope = {"step": "verify", "try": 1}
        lock = {**scope, "resource": "full-suite-lock", "suite": "first-suite"}
        slot = {**scope, "resource": "check-slot", "suite": "second-suite"}
        self.log(current, 5, "wait", cause="slot", phase="start", at=stamp(5), **lock)
        self.log(current, 6, "wait", cause="ci", phase="end", at=stamp(6), since=stamp(4))
        lock_wait = ("Waiting", "The full-suite lock is in use", "The full-suite lock becomes available")
        progress(lock_wait)
        historical = board.run_board(RUN)["ticket_progress"]
        self.assertEqual(historical["timing"]["run_ids"], [RUN])
        self.assertEqual(activity(historical), lock_wait)

        self.log(current, 7, "wait", cause="slot", phase="start", at=stamp(7), **slot)
        progress(("Waiting", "A check slot is unavailable", "A check slot becomes available"))
        self.log(current, 9, "wait", cause="slot", phase="end", at=stamp(9), since=stamp(7), **slot)
        progress(lock_wait)
        self.log(current, 12, "wait", cause="slot", phase="end", at=stamp(12), since=stamp(5), **lock)
        active = progress(("Running verify", "", "verify finishes"))
        self.assertEqual(active["timing"]["run_ids"], [current])
        self.assertEqual((active["follow_up"], active["since"]), (None, ""))
        for key in ("prerequisites", "updates"):
            self.assertEqual(active[key], waiting[key])

    def test_declared_pr_discovery_reaches_status_at_capacity_without_a_run_or_skip(self):
        self.assertEqual(poll_dependencies(self.root, "open", at_capacity=True), {})
        self.assertIsNone(runs.newest_live_whole_run(REPO, "965"))
        progress, = self.status_progress()
        self.assertEqual((progress["issue"], progress["run"], progress["blocker"]), ("965", "", ""))
        self.assertIsNone(progress["follow_up"])
        pr, = progress["prerequisites"]
        self.assertEqual((pr["url"], pr["state"], pr["deployed_release"]), (DEPLOYED, "open", ""))
        update, = progress["updates"]
        self.assertEqual(update["message"], f"Found prerequisite PR {DEPLOYED} (open).")
        self.assertEqual(poll_dependencies(self.root, "open", at_capacity=True), {})
        self.assertEqual(self.status_progress(), [progress])
        self.assertEqual(poll_dependencies(self.root, "removed", at_capacity=True), {})
        self.assertEqual(self.status_progress(), [])

    def test_owner_review_survives_recent_run_eviction_until_review_ends(self):
        self.owner_review()
        with patch.object(board, "RECENT_RUNS", 1):
            progress, = self.status_progress()
            self.assertEqual(progress, board.run_board(RETRY_RUN)["ticket_progress"])
            self.assertEqual((progress["run"], progress["activity"], progress["blocker"], progress["next_event"]),
                             (RETRY_RUN, "Draft awaiting owner review after automatic retry", "Tool is missing", REVIEW_NEXT))
            self.assertEqual(progress["follow_up"], {"state": "owner-review", "prs": [DRAFT], "draft": True})
            self.assertEqual([(p["url"], bool(p["deployed_release"])) for p in progress["prerequisites"]],
                             [(DEPLOYED, True), (MERGED, False)])
            runs.end_environment_owner_review(RUN)
            self.assertEqual(self.status_progress(), [])
        progress = board.run_board(RETRY_RUN)["ticket_progress"]
        self.assertEqual(progress["activity"], "Follow-up PR recorded")
        self.assertEqual(progress["blocker"], "")
        self.assertNotEqual(progress["next_event"], REVIEW_NEXT)
        self.assertEqual([p["url"] for p in progress["prerequisites"]], [DEPLOYED, MERGED])

    def test_merged_retry_clears_owner_review_before_the_poller_records_its_end(self):
        self.owner_review()
        self.assertTrue(runs.move(RETRY_RUN, "merged", expect=("pr",)))
        self.assertIsNone(runs.environment_hold(RUN)["owner_review_ended_at"])
        with patch.object(board, "RECENT_RUNS", 1):
            self.assertEqual(self.status_progress(), [])
        progress = board.run_board(RETRY_RUN)["ticket_progress"]
        self.assertEqual((progress["activity"], progress["blocker"], progress["next_event"]), ("Merged", "", ""))

    def test_classic_tracker_question_is_visible_without_an_ask_or_recent_run(self):
        self.insert(RUN, "running", started=stamp(0))
        self.log(RUN, 5, "question", question="Reply on the tracker.", summary="Which release should we target?")
        self.settle(RUN, 6, "needs-info")
        self.insert("pip-unrelated", "done", issue="965", started=stamp(7), finished=stamp(8))
        self.at(20)
        self.assertEqual(asks.asks_of(RUN), [])
        with patch.object(board, "RECENT_RUNS", 1):
            waiting = board.run_board(RUN)["ticket_progress"]
            self.assertEqual(waiting["activity"], "Waiting for the owner")
            self.assertEqual(waiting["blocker"], "Which release should we target?")
            self.assertEqual(waiting["next_event"], "The owner replies to the question on the tracker")
            self.assertEqual(board.live_board(None)["ticket_progress"]["rows"], [waiting])
            self.assertEqual(board.snapshot()["ticket_progress"]["rows"], [waiting])

            reply = run_lines.path(RUN).with_name(pipeline.ANSWER_FILE)
            reply.write_text("Target the next release.")
            answered = board.run_board(RUN)["ticket_progress"]
            self.assertEqual(answered["activity"], "Owner answered")
            self.assertEqual(answered["blocker"], "")
            self.assertEqual(board.live_board(None)["ticket_progress"]["rows"], [])

            reply.unlink()
            self.insert("pip-continuation", "done", started=stamp(10), finished=stamp(12))
            self.assertEqual(board.live_board(None)["ticket_progress"]["rows"], [])

    def test_publishing_wait_distinguishes_the_merge_lease_from_retaking_a_run_slot(self):
        self.insert(RUN, "running", started=stamp(0))
        self.log(RUN, 5, "wait", cause="slot", phase="start", at=stamp(5))
        markers = {"merge-wait"}
        self.holding.side_effect = lambda rows, kind, *_: rows if kind in markers else []
        detail = board.run_board(RUN)["ticket_progress"]
        self.assertEqual(detail["blocker"], "The repository merge lease is in use")
        self.assertEqual(detail["next_event"], "The repository merge lease becomes available")
        self.assertEqual(board.live_board(None)["ticket_progress"]["rows"], [detail])

        markers.add("slot-wait")
        detail = board.run_board(RUN)["ticket_progress"]
        self.assertEqual(detail["blocker"], "A run slot is unavailable")
        self.assertEqual(detail["next_event"], "A run slot becomes available")

    def test_ordinary_pr_completion_has_final_progress_and_separate_links_without_saved_progress(self):
        other = "https://github.com/owner/other/pull/19"
        self.insert(RUN, "running", started=stamp(0))
        self.log(RUN, 3, "step_start", step="verify")
        self.at(7)
        active, = board.live_board(None)["ticket_progress"]["rows"]
        self.assertEqual(active["activity"], "Running verify")
        self.assertEqual(active["timing"]["elapsed_seconds"], 7)

        self.log(RUN, 9, "step_end", step="verify")
        self.settle(RUN, 12, "pr", pr_url=f"{DRAFT} {other}")
        self.assertFalse(ticket_progress.get(REPO, ISSUE))
        self.assertEqual(board.live_board(None)["ticket_progress"]["rows"], [])
        completed = board.run_board(RUN)["ticket_progress"]
        self.assertEqual(completed["activity"], "Follow-up PR recorded")
        self.assertEqual(completed["timing"]["elapsed_seconds"], 12)
        self.assertEqual(completed["follow_up"]["prs"], [DRAFT, other])
