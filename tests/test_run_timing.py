import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from arete import paths, record, run_lines, run_timing, util
from arete.store import asks, db, orchestration, runs, sources
from tests.support import workspace_state

ORIGIN = datetime(2030, 1, 2, tzinfo=UTC)


def clock(seconds):
    return ORIGIN + timedelta(seconds=seconds)


def stamp(seconds):
    return util.iso(clock(seconds))


class RunTimingTests(unittest.TestCase):
    def setUp(self):
        parent = paths.scratch_root(paths.STATE_DIR) if paths.ISOLATED else None
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=parent))).resolve()
        self.enterContext(workspace_state(root))
        self.enterContext(mock.patch.object(paths, "SERVE_BEAT_FILE", root / "serve-beat.json"))
        self.enterContext(mock.patch.object(db.dbstats.Timing, "finished"))
        self.enterContext(mock.patch.object(record.scrub, "secrets", return_value=()))
        self.now = self.enterContext(mock.patch.object(util, "now", return_value=ORIGIN))
        db.migrate()

    def at(self, seconds):
        self.now.return_value = clock(seconds)

    def run_row(self, rid, start, **extra):
        self.at(start)
        runs.insert_run(rid, {"kind": "pipeline", "repo": "arete", "issue": "964", "status": "running",
                              "started": stamp(start), **extra})

    def settle(self, rid, at, status):
        self.at(at)
        self.assertIsNotNone(runs.settle(rid, {"status": status, "finished": stamp(at)}))

    def log(self, rid, seconds, event, **fields):
        path = run_lines.path(rid)
        path.parent.mkdir(exist_ok=True, parents=True)
        with path.open("a") as stream:
            stream.write(json.dumps({"run": rid, "ts": stamp(seconds), "event": event, **fields}) + "\n")

    def accept(self, lease, at, *ops):
        self.at(at)
        turn = orchestration.begin(lease, {}, None)
        version = orchestration.snapshot(lease)["version"]
        return [row["id"] for row in orchestration.accept(
            lease, turn, version, {"calls": [{"op": op, "args": {}} for op in ops]}, None)]

    def transition(self, lease, aid, at, state, **result):
        self.at(at)
        return orchestration.transition(lease, aid, state, result)

    def project(self, rid, at):
        return run_timing.project(rid, as_of=clock(at))

    def test_parallel_builders_waits_repair_and_resume_share_one_wall_clock(self):
        self.run_row("first", 5)
        self.log("first", 5, "wait", cause="slot", phase="end", since=stamp(0), at=stamp(5))
        lease = orchestration.acquire("first", "", {})
        left, right, waiting, legacy, refused = self.accept(
            lease, 6, "worker.start", "worker.start", "run.wait", "worker.start", "worker.start")
        self.transition(lease, left, 8, "running", writing=True, role="build")
        self.transition(lease, left, 9, "running")
        self.transition(lease, waiting, 9, "running")
        with db.tx() as c:
            c.execute("UPDATE orchestration_actions SET state='running', at=?, result=? WHERE root=? AND id=?",
                      (stamp(10), '{"writing":true,"role":"build"}', lease.root, legacy))
        self.transition(lease, right, 12, "running", writing=True, role="build")
        live = self.project("first", 15)
        self.assertEqual(live["running_run_ids"], ["first"])
        self.assertEqual({a["source_id"] for a in live["active"]}, {left, right})
        self.assertEqual(live["blockers"], [])
        self.assertEqual(live["overlap"], [{"activity": {"writing": 2}, "seconds": 3}])
        self.transition(lease, refused, 16, "failed", writing=True, role="build")
        self.transition(lease, left, 18, "succeeded")
        self.transition(lease, legacy, 20, "succeeded")
        self.transition(lease, right, 23, "succeeded")
        self.transition(lease, waiting, 23, "succeeded", reason="timeout", active=True)
        actions = {a["id"]: a for a in orchestration.view("first")["actions"]}
        self.assertEqual(actions[left]["timing"], {"run_id": "first", "started_at": stamp(8), "finished_at": stamp(18)})
        self.assertIsNone(actions[legacy]["timing"])
        self.assertIsNone(actions[refused]["timing"])

        check, = self.accept(lease, 23, "checks.run")
        self.transition(lease, check, 24, "running")
        self.log("first", 25, "step_start", step="checks", **{"try": 1})
        record.mark("first", "check", at=stamp(26), name="unit", seconds=2.5, exit=0)
        self.log("first", 28, "step_end", step="checks", **{"try": 1})
        self.transition(lease, check, 29, "succeeded")
        self.log("first", 30, "step_start", step="review", **{"try": 1})
        self.log("first", 31, "step_end", step="review", carried="other", **{"try": 1})
        self.log("first", 32, "step_end", step="review", **{"try": 2})
        self.log("first", 34, "step_end", step="review", **{"try": 1})
        self.at(35)
        asks.ask_add("ask", "first", "arete", "964", "plan", "plan", "Need a decision", "", [], {})
        self.settle("first", 38, "needs-info")
        owner = self.project("first", 42)
        self.assertEqual(owner["elapsed_seconds"], 42)
        self.assertEqual(owner["blockers"], [{"run_id": "first", "category": "owner_wait", "cause": "person",
                                              "since": stamp(35), "source_id": "ask",
                                              "next_event": "Owner answers the outstanding question"}])
        self.at(47)
        self.assertTrue(asks.ask_close("ask", "answered"))
        self.at(50)
        sources.pass_over("arete", "964")
        queue = self.project("first", 54)
        self.assertEqual(queue["category_seconds"]["owner_wait"], 12)
        self.assertEqual(queue["category_seconds"]["queue_wait"], 9)
        self.assertEqual([b["cause"] for b in queue["blockers"]], ["slot"])

        self.run_row("repair", 55)
        self.log("repair", 55, "repair", source="first")
        self.log("repair", 55, "wait", cause="person", phase="end", since=stamp(35), at=stamp(50))
        passed_over = sources.take_passed_over("arete", "964")
        self.log("repair", 55, "wait", cause="slot", phase="end", since=passed_over, at=stamp(55))
        repair = orchestration.acquire("repair", "", {})
        fix, = self.accept(repair, 56, "worker.start")
        self.transition(repair, fix, 57, "running", writing=True, role="review_repair")
        self.settle("repair", 64, "interrupted")
        self.run_row("resume", 70)
        resume = orchestration.acquire("resume", "repair", {})
        self.transition(resume, fix, 72, "interrupted")
        recovered = orchestration.view("resume")["actions"][0]
        self.assertEqual(recovered["at"], stamp(72))
        self.assertEqual(recovered["timing"], {"run_id": "repair", "started_at": stamp(57), "finished_at": None})
        verify, review = self.accept(resume, 72, "verification.run", "review.run")
        self.transition(resume, verify, 72, "running")
        self.transition(resume, verify, 79, "succeeded")
        self.transition(resume, review, 80, "running")
        self.log("resume", 81, "step_start", step="review2", atom="review")
        self.log("resume", 84, "step_end", step="review2")
        self.transition(resume, review, 85, "succeeded")
        self.settle("resume", 87, "pr")
        self.at(120)
        self.assertTrue(runs.move("resume", "merged", ("pr",), finished=stamp(120)))
        self.run_row("specific-step", 121, only_step="verify")
        self.log("specific-step", 121, "route", continues="resume")
        self.run_row("unrelated", 122)
        self.assertIsNone(self.project("specific-step", 150))
        projected = self.project("resume", 150)
        self.assertEqual(projected, self.project("first", 150))
        self.assertEqual(projected, self.project("repair", 150))
        self.assertEqual(projected["run_ids"], ["first", "repair", "resume"])
        self.assertTrue(projected["elapsed_complete"])
        self.assertEqual((projected["started_at"], projected["through_at"]), (stamp(0), stamp(87)))
        self.assertEqual((projected["elapsed_seconds"], projected["unattributed_seconds"]), (87, 22))
        self.assertEqual(projected["category_seconds"], {"writing": 15, "checks": 12, "review": 9, "owner_wait": 12,
                                                         "queue_wait": 10, "retry": 7})
        self.assertEqual(projected["overlap_seconds"], 6)
        self.assertEqual(projected["overlap"], [{"activity": {"writing": 2}, "seconds": 6}])
        self.assertEqual(projected["active"], [])
        self.assertEqual(projected["blockers"], [])

    def test_prerequisite_hold_advances_until_retry_and_survives_release(self):
        url = "https://github.com/owner/arete/pull/52"
        self.run_row("draft", 0, pr_url=url)
        self.log("draft", 75, "step_start", step="verify")
        self.at(89)
        runs.save_environment_hold("draft", [("owner/arete", 52, url, "a" * 40)])
        self.settle("draft", 90, "pr")
        self.run_row("dry", 100, environment_retry_of="draft", dry_run=1)
        held = self.project("draft", 14490)
        self.assertEqual((held["elapsed_seconds"], held["through_at"]), (14490, stamp(14490)))
        self.assertEqual(held["category_seconds"]["queue_wait"], 14400)
        self.assertEqual(held["category_seconds"]["checks"], 0)
        self.assertEqual(held["unattributed_seconds"], 90)
        self.assertEqual(held["running_run_ids"], [])
        self.assertEqual(held["active"], [])
        self.assertEqual([(b["cause"], b["since"]) for b in held["blockers"]], [("prerequisite", stamp(90))])

        self.run_row("retry", 14600, environment_retry_of="draft")
        self.log("retry", 14600, "wait", cause="slot", phase="end", since=stamp(14590), at=stamp(14600))
        self.log("retry", 14605, "step_start", step="verify")
        verifying = self.project("retry", 14610)
        self.assertEqual(verifying["category_seconds"]["queue_wait"], 14510)
        self.assertEqual(verifying["category_seconds"]["checks"], 5)
        self.assertEqual(verifying["blockers"], [])
        self.log("retry", 14635, "step_end", step="verify")
        self.settle("retry", 14645, "pr")
        self.at(14700)
        runs.release_environment_hold("draft")
        self.assertTrue(runs.move("retry", "merged", ("pr",), finished=stamp(14700)))
        settled = self.project("retry", 14800)
        self.assertEqual(settled, self.project("draft", 14800))
        self.assertEqual(held, self.project("draft", 14490))
        self.assertEqual(settled["run_ids"], ["draft", "retry"])
        self.assertTrue(settled["elapsed_complete"])
        self.assertEqual((settled["elapsed_seconds"], settled["through_at"]), (14645, stamp(14645)))
        self.assertEqual(settled["category_seconds"]["queue_wait"], 14510)
        self.assertEqual(settled["category_seconds"]["checks"], 30)
        self.assertEqual(settled["unattributed_seconds"], 105)
        self.assertEqual(settled["overlap_seconds"], 0)
        self.assertEqual(settled["blockers"], [])

    def test_classic_checks_use_recorded_command_intervals_without_double_counting(self):
        self.run_row("classic", 0)
        for at, seconds, name, exit_code in ((2, 4.25, "lint", 0), (2, 4.25, "lint", 0), (6, 3, "unit", 1),
                                             (14, 8, "unit", 0), (21, 4, "late", 0)):
            record.mark("classic", "check", at=stamp(at), name=name, command=name, seconds=seconds, exit=exit_code)
        self.settle("classic", 20, "pr")
        timing = self.project("classic", 30)
        self.assertEqual(timing["elapsed_seconds"], 20)
        self.assertEqual(timing["category_seconds"]["checks"], 13)
        self.assertEqual(timing["unattributed_seconds"], 7)
        self.assertEqual(timing["overlap_seconds"], 0)

    def test_classic_repeated_review_follows_the_recorded_atom(self):
        self.run_row("classic", 0)
        self.log("classic", 4, "step_start", step="review", atom="review")
        self.log("classic", 9, "step_end", step="review")
        self.log("classic", 10, "step_start", step="implement", atom="review_repair")
        self.log("classic", 16, "step_end", step="implement")
        self.log("classic", 18, "step_start", step="review2", atom="review")
        self.log("classic", 19, "step_start", step="review3", atom="review", carried="other")
        self.log("classic", 21, "step_end", step="review3", carried="other")
        self.log("classic", 23, "step_end", step="review2")
        self.log("classic", 24, "step_start", step="review4")
        self.log("classic", 26, "step_end", step="review4")
        self.settle("classic", 27, "pr")
        timing = self.project("classic", 40)
        self.assertEqual(timing["elapsed_seconds"], 27)
        self.assertEqual(timing["category_seconds"]["review"], 10)
        self.assertEqual(timing["category_seconds"]["retry"], 6)
        self.assertEqual(timing["unattributed_seconds"], 11)
        self.assertEqual(timing["overlap_seconds"], 0)

    def test_missing_history_does_not_turn_pr_close_or_recovery_gap_into_work(self):
        self.run_row("pruned", 0, status="merged", finished=stamp(100))
        self.assertFalse(self.project("pruned", 150)["elapsed_complete"])
        self.assertEqual(self.project("pruned", 150)["elapsed_seconds"], 0)
        self.run_row("continuation", 10, status="failed", finished=stamp(20))
        self.log("continuation", 10, "route", continues="pruned")
        self.log("continuation", 12, "step_start", step="implement", atom="check_repair")
        self.log("continuation", 15, "step_end", step="implement")
        self.log("continuation", 16, "step_start", step="verify")
        self.run_row("environment", 28, status="failed", finished=stamp(30), environment_retry_of="continuation")
        progress = self.project("pruned", 150)
        self.assertEqual(progress, self.project("environment", 150))
        self.assertFalse(progress["elapsed_complete"])
        self.assertEqual((progress["elapsed_seconds"], progress["unattributed_seconds"]), (30, 27))
        self.assertEqual(progress["category_seconds"]["retry"], 3)
        self.assertEqual(progress["category_seconds"]["checks"], 0)

    def test_reopened_pr_without_settlement_keeps_its_close_time_unknown(self):
        self.run_row("reopened", 0, status="pr", finished=stamp(10))
        self.log("reopened", 2, "step_start", step="verify")
        self.log("reopened", 5, "step_end", step="verify")
        self.at(100)
        self.assertTrue(runs.move("reopened", "rejected", ("pr",), finished=stamp(100)))
        self.at(120)
        self.assertTrue(runs.move("reopened", "pr", ("rejected",)))
        progress = self.project("reopened", 150)
        self.assertFalse(progress["elapsed_complete"])
        self.assertEqual(progress["elapsed_seconds"], 5)
        self.assertEqual(progress["category_seconds"]["checks"], 3)
