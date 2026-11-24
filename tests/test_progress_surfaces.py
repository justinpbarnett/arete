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

REPO = "arete"
ISSUE = "964"
RUN = "pip-held"
AT = "2026-11-24T01:00:00Z"
DRAFT = "https://github.com/owner/arete/pull/52"
DEPLOYED = "https://github.com/owner/arete/pull/49"
MERGED = "https://github.com/owner/arete/pull/50"
ROOT_RUN = "pip-root"
REPAIR_RUN = "pip-repair"
AS_OF = datetime(2026, 11, 24, 3, tzinfo=UTC)


class ProgressSurfacesTests(unittest.TestCase):
    def setUp(self):
        parent = paths.scratch_root(paths.STATE_DIR) if paths.ISOLATED else None
        folder = self.enterContext(tempfile.TemporaryDirectory(prefix="arete-progress-surfaces-", dir=parent))
        root = Path(folder).resolve()
        self.cfg = config.Config(repos=[config.Repo(REPO, root, github="owner/arete")])
        self.enterContext(workspace_state(root))
        self.enterContext(patch.object(control, "BEAT_FILE", root / "serve-beat.json"))
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
        ticket_progress.record(REPO, ISSUE, {**state, "next_event": "Release verification finishes"})

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
        self.at(5)
        self.insert(ROOT_RUN, "running", started=stamp(5))
        self.log(ROOT_RUN, 5, "wait", cause="slot", phase="end", since=stamp(0), at=stamp(5))
        first = orchestration.acquire(ROOT_RUN, "", {})
        self.at(6)
        left, right, checks = self.accept(first, "worker.start", "worker.start", "checks.run")
        self.transition(first, left, 8, "running", writing=True, role="build")
        self.transition(first, right, 12, "running", writing=True, role="build")
        self.transition(first, left, 18, "succeeded")
        self.transition(first, right, 23, "succeeded")
        self.transition(first, checks, 24, "running")
        self.transition(first, checks, 29, "succeeded")
        self.log(ROOT_RUN, 30, "step_start", step="review")
        self.log(ROOT_RUN, 34, "step_end", step="review")
        self.at(35)
        asks.ask_add("ask", ROOT_RUN, REPO, ISSUE, "plan", "plan", "Need a decision", "", [], {})
        self.settle(ROOT_RUN, 38, "needs-info")
        self.at(47)
        self.assertTrue(asks.ask_close("ask", "answered"))
        self.at(50)
        sources.pass_over(REPO, ISSUE)

        self.at(55)
        self.insert(REPAIR_RUN, "running", started=stamp(55))
        self.log(REPAIR_RUN, 55, "repair", source=ROOT_RUN)
        self.log(REPAIR_RUN, 55, "wait", cause="slot", phase="end",
                 since=sources.take_passed_over(REPO, ISSUE), at=stamp(55))
        repair = orchestration.acquire(REPAIR_RUN, "", {})
        fix, = self.accept(repair, "worker.start")
        self.transition(repair, fix, 57, "running", writing=True, role="review_repair")
        self.settle(REPAIR_RUN, 64, "interrupted")

        self.at(70)
        self.insert(RUN, "running", started=stamp(70))
        resume = orchestration.acquire(RUN, REPAIR_RUN, {})
        self.transition(resume, fix, 72, "interrupted")
        verify, review = self.accept(resume, "verification.run", "review.run")
        self.transition(resume, verify, 72, "running")
        self.transition(resume, verify, 79, "succeeded")
        self.transition(resume, review, 80, "running")
        self.transition(resume, review, 85, "succeeded")
        self.settle(RUN, 87, "pr", pr_url=DRAFT)
        self.at(150)

    def test_status_and_run_share_stored_lineage_timing_and_preserve_durable_updates(self):
        self.store_timed_lineage()
        self.hold()
        self.queue()
        self.assertEqual(ticket_progress.get(REPO, ISSUE)["updates"], [])
        with db.reader() as c:
            before = events.metadata(c)["head"]
        detail = board.run_board(RUN)
        dashboard = board.live_board(None)
        cli = board.snapshot()
        ancestors = [board.run_board(run_id) for run_id in (ROOT_RUN, REPAIR_RUN)]
        with db.reader() as c:
            self.assertEqual(events.metadata(c)["head"], before)
        rows = {r["issue"]: r for r in dashboard["ticket_progress"]["rows"]}
        held = rows[ISSUE]
        self.assertEqual(held, detail["ticket_progress"])
        self.assertEqual(dashboard["ticket_progress"], cli["ticket_progress"])
        timing = held["timing"]
        for ancestor in ancestors:
            self.assertEqual(timing, ancestor["ticket_progress"]["timing"])
        self.assertEqual(timing["run_ids"], [ROOT_RUN, REPAIR_RUN, RUN])
        self.assertEqual(timing["as_of"], stamp(150))
        self.assertTrue(timing["elapsed_complete"])
        self.assertEqual(timing["elapsed_seconds"], 150)
        self.assertEqual(timing["category_seconds"]["queue_wait"], 73)
        self.assertEqual(timing["overlap_seconds"], 6)
        self.assertEqual(timing["unattributed_seconds"], 22)

        def timing_entries(text):
            return [entry.strip() for entry in text.replace("·", "\n").splitlines() if entry.strip()]

        expected_timing = ["Run lineage elapsed: 2m 30s", "Writing: 15s", "Checks / verification: 12s",
                           "Review: 9s", "Owner wait: 12s", "Queue wait: 1m 13s", "Retry: 7s", "Unattributed: 22s",
                           "Overlap: 6s (already counted in elapsed)", "Writing ×2: 6s"]
        self.assertEqual(timing_entries("\n".join(timing["lines"])), expected_timing)
        self.assertEqual(held["blocker"], "Tool is missing")
        self.assertEqual(held["next_event"], "Release verification finishes")
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
        for line in ("Blocker: Tool is missing", "Next: Release verification finishes", f"Deployed prerequisite: {DEPLOYED}",
                     f"Merged prerequisite (deployment not confirmed): {MERGED}", f"Draft follow-up: {DRAFT}",
                     "arete#965: Queued for a run slot", "arete#966: Blocked before start"):
            self.assertIn(line, text)
        cli_entries = timing_entries(text)
        for entry in expected_timing:
            self.assertIn(entry, cli_entries)
        self.assertEqual(text.count("Found prerequisite PR"), 2)

    def test_inactive_hold_uses_current_run_and_closed_wait_does_not_block(self):
        self.insert(RUN, "pr", pr_url=DRAFT, finished=AT)
        self.hold()
        self.insert("pip-next", "running", started="2026-11-24T02:00:00Z")
        sources.pass_over(REPO, ISSUE)
        lines = [{"event": "step_start", "step": "verify"}, {"event": "wait", "cause": "ci", "phase": "start", "at": AT}]
        with patch.object(board.run_lines, "read", return_value=lines):
            live = board.live_board(None)["ticket_progress"]["rows"]
            waiting, = live
            self.assertEqual(waiting["blocker"], "CI is pending")
            self.assertEqual(waiting["next_event"], "CI reports its result")
            lines.append({"event": "wait", "cause": "ci", "phase": "end", "since": AT})
            active = board.run_board("pip-next")["ticket_progress"]
            self.assertEqual(active["activity"], "Running verify")
            self.assertEqual(active["blocker"], "")
            self.assertEqual(active["next_event"], "verify finishes")
            self.assertIsNone(active["follow_up"])
            self.assertEqual(active["timing"]["run_ids"], ["pip-next"])
            self.assertEqual(active["prerequisites"], waiting["prerequisites"])
            self.assertEqual(active["updates"], waiting["updates"])
            self.assertEqual(active["since"], "")

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
