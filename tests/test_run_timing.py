import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from arete import config, paths, pipeline, poll, record, run_lines, run_timing, util
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

    def check_wait(self, rid, at, suite, resource, *, since=None, **fields):
        phase = {"phase": "start"} if since is None else {"phase": "end", "since": stamp(since)}
        self.log(rid, at, "wait", cause="slot", at=stamp(at), suite=suite, resource=resource, **phase, **fields)

    def accept(self, lease, at, *ops):
        self.at(at)
        turn = orchestration.begin(lease, {}, None)
        version = orchestration.snapshot(lease)["version"]
        return [row["id"] for row in orchestration.accept(
            lease, turn, version, {"calls": [{"op": op, "args": {}} for op in ops]}, None)]

    def transition(self, lease, aid, at, state, **result):
        self.at(at)
        return orchestration.transition(lease, aid, state, result)

    def project(self, rid, at, *linked):
        timing = run_timing.project(rid, as_of=clock(at))
        for member in linked:
            self.assertEqual(timing, self.project(member, at), member)
        return timing

    def assert_timing(self, actual, *, categories=None, **expected):
        if categories is not None:
            empty = dict(writing=0, checks=0, review=0, owner_wait=0, queue_wait=0, retry=0)
            expected["category_seconds"] = empty | categories
        self.assertEqual({key: actual[key] for key in expected}, expected)

    def step(self, rid, step, start, end, *, atom=None, **fields):
        self.log(rid, start, "step_start", step=step, atom=atom, **fields)
        self.log(rid, end, "step_end", step=step, **fields)

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
        self.assertEqual({a["source_id"] for a in live["active"]}, {left, right})
        self.assert_timing(live, running_run_ids=["first"], blockers=[],
                           overlap=[{"activity": {"writing": 2}, "seconds": 3}])
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
        self.step("first", "checks", 25, 28, **{"try": 1})
        record.mark("first", "check", at=stamp(26), name="unit", seconds=2.5, exit=0)
        self.transition(lease, check, 29, "succeeded")
        self.log("first", 30, "step_start", step="review", **{"try": 1})
        self.log("first", 31, "step_end", step="review", carried="other", **{"try": 1})
        self.log("first", 32, "step_end", step="review", **{"try": 2})
        self.log("first", 34, "step_end", step="review", **{"try": 1})
        self.at(35)
        asks.ask_add("ask", "first", "arete", "964", "plan", "plan", "Need a decision", "", [], {})
        self.settle("first", 38, "needs-info")
        owner = self.project("first", 42)
        self.assert_timing(owner, elapsed_seconds=42, blockers=[{
            "run_id": "first", "category": "owner_wait", "cause": "person", "since": stamp(35), "source_id": "ask",
            "next_event": "Owner answers the outstanding question",
        }])
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
        self.step("resume", "review2", 81, 84, atom="review")
        self.transition(resume, review, 85, "succeeded")
        self.settle("resume", 87, "pr")
        self.at(120)
        self.assertTrue(runs.move("resume", "merged", ("pr",), finished=stamp(120)))
        self.run_row("specific-step", 121, only_step="verify")
        self.log("specific-step", 121, "route", continues="resume")
        self.run_row("unrelated", 122)
        self.assertIsNone(self.project("specific-step", 150))
        projected = self.project("resume", 150, "first", "repair")
        self.assert_timing(projected, run_ids=["first", "repair", "resume"], elapsed_complete=True,
                           started_at=stamp(0), through_at=stamp(87), elapsed_seconds=87, unattributed_seconds=22,
                           categories=dict(writing=15, checks=12, review=9, owner_wait=12, queue_wait=10, retry=7),
                           overlap_seconds=6, overlap=[{"activity": {"writing": 2}, "seconds": 6}], active=[], blockers=[])

    def hold_draft(self, start, end):
        url = "https://github.com/owner/arete/pull/52"
        self.run_row("draft", start, pr_url=url)
        self.at(end - 1)
        runs.save_environment_hold("draft", [("owner/arete", 52, url, "a" * 40)])
        self.settle("draft", end, "pr")
        return url

    def test_manual_release_without_retry_preserves_queue_time_from_either_run(self):
        self.run_row("first", 0)
        self.settle("first", 10, "interrupted")
        self.hold_draft(15, 30)
        self.log("draft", 15, "route", continues="first")

        held = self.project("draft", 50, "first")
        self.assertEqual((held["elapsed_seconds"], held["category_seconds"]["queue_wait"]), (50, 20))
        self.assertEqual([(b["cause"], b["since"]) for b in held["blockers"]], [("prerequisite", stamp(30))])

        self.at(60)
        runs.release_environment_hold("draft")
        released = self.project("draft", 120, "first")
        self.assertEqual(held, self.project("draft", 50))
        self.assert_timing(released, run_ids=["first", "draft"], through_at=stamp(60), elapsed_seconds=60,
                           unattributed_seconds=30, blockers=[])
        self.assertEqual(released["category_seconds"]["queue_wait"], 30)

        with db.tx() as c:
            c.execute("DELETE FROM events_log WHERE subject = 'run:draft' AND kind = ?", (runs.HOLD_RELEASED,))
        legacy = self.project("draft", 120, "first")
        self.assert_timing(legacy, through_at=stamp(30), elapsed_seconds=30, blockers=[], categories={})

    def test_prerequisite_queue_and_owner_review_are_separate_across_retry_lineage(self):
        url = self.hold_draft(0, 20)
        self.log("draft", 15, "step_start", step="verify")
        self.run_row("dry", 30, environment_retry_of="draft", dry_run=1)
        held = self.project("draft", 90)
        self.assert_timing(held, through_at=stamp(90), elapsed_seconds=90, unattributed_seconds=20,
                           running_run_ids=[], active=[], categories=dict(queue_wait=70))
        self.assertEqual([(b["cause"], b["since"]) for b in held["blockers"]], [("prerequisite", stamp(20))])

        self.run_row("retry", 100, environment_retry_of="draft", pr_url=url)
        self.log("retry", 100, "wait", cause="slot", phase="end", since=stamp(95), at=stamp(100))
        self.log("retry", 102, "step_start", step="verify")
        verifying = self.project("retry", 110, "draft")
        self.assert_timing(verifying, blockers=[], categories=dict(queue_wait=80, checks=8))
        self.log("retry", 112, "step_end", step="verify")
        self.step("retry", "review", 114, 118)
        self.settle("retry", 120, "pr")
        self.at(125)
        runs.release_environment_hold("draft", "owner-review")

        live = self.project("retry", 130, "draft")
        self.assert_timing(live, through_at=stamp(130), elapsed_seconds=130, blockers=[{
            "run_id": "retry", "cause": "owner-review", "category": "owner_wait", "since": stamp(120),
            "source_id": None, "next_event": "Owner reviews the draft and decides how to unblock verification",
        }], categories=dict(queue_wait=80, checks=10, review=4, owner_wait=10))

        self.at(150)
        self.assertTrue(runs.move("retry", "merged", ("pr",), finished=stamp(150)))
        self.at(160)
        runs.end_environment_owner_review("draft")
        resolved = self.project("retry", 200, "draft")
        self.assertEqual(held, self.project("draft", 90))
        self.assertEqual(live, self.project("retry", 130))
        self.assert_timing(resolved, run_ids=["draft", "retry"], elapsed_complete=True, through_at=stamp(160),
                           elapsed_seconds=160, unattributed_seconds=26, overlap_seconds=0, blockers=[],
                           categories=dict(checks=10, review=4, owner_wait=40, queue_wait=80))

    def test_check_step_waits_pair_by_run_suite_resource_and_since(self):
        rid, suite = "classic", "classic/checks/first"
        self.run_row(rid, 0)
        scope = {"step": "checks", "try": 2}
        self.log(rid, 2, "step_start", **scope)
        self.check_wait(rid, 5, suite, "check-slot", **scope)
        for at, invalid in enumerate(({"run": "another-run"}, {"suite": "other-suite"},
                                      {"resource": "full-suite-lock"}, {"since": 6}), 6):
            fields = {"suite": suite, "resource": "check-slot", "since": 5, **scope, **invalid}
            self.check_wait(rid, at, **fields)

        slot = self.project(rid, 10)
        self.assert_timing(slot, active=[], categories=dict(queue_wait=5, checks=3), blockers=[{
            "run_id": rid, "cause": "slot", "category": "queue_wait", "resource": "check-slot",
            "suite": suite, "source_id": suite, "since": stamp(5), "next_event": "A check slot becomes available",
            **scope,
        }])

        self.check_wait(rid, 11, suite, "check-slot", since=5, **scope)
        self.check_wait(rid, 11, suite, "full-suite-lock", **scope)
        lock = self.project(rid, 12)
        self.assert_timing(lock, categories=dict(queue_wait=7, checks=3))
        blocker, = lock["blockers"]
        self.assertEqual((blocker["cause"], blocker["resource"], blocker["since"], blocker["next_event"]),
                         ("slot", "full-suite-lock", stamp(11), "The full-suite lock becomes available"))

        self.check_wait(rid, 16, suite, "full-suite-lock", since=11, **scope)
        record.mark(rid, "check", at=stamp(16), name="unit", seconds=5, exit=0, suite=suite, **scope)
        self.log(rid, 24, "step_end", **scope)
        self.settle(rid, 25, "pr")
        self.assertEqual(self.project(rid, 12)["blockers"], lock["blockers"])
        settled = self.project(rid, 30)
        self.assert_timing(settled, elapsed_seconds=25, unattributed_seconds=3, overlap=[], blockers=[],
                           categories=dict(checks=11, queue_wait=11))

    def test_orchestrated_check_wait_scopes_the_pause_to_its_suite_and_gate(self):
        rid, scope = "adaptive", {"step": "checks", "try": 3}
        unknown, inferred, held, other = (f"{rid}/checks/{name}" for name in ("unknown", "inferred", "held", "other"))
        self.run_row(rid, 0)
        lease = orchestration.acquire(rid, "", {})
        gate, builder = self.accept(lease, 1, "checks.run", "worker.start")
        self.transition(lease, gate, 2, "running")
        self.check_wait(rid, 3, unknown, "check-slot")
        live = self.project(rid, 4)
        self.assertEqual(live["overlap"], [{"activity": {"checks": 1, "queue_wait": 1}, "seconds": 1}])
        self.assertEqual([span["source_id"] for span in live["active"]], [gate])
        self.assertNotIn("step", live["blockers"][0])
        self.check_wait(rid, 5, unknown, "check-slot", since=3)
        record.mark(rid, "check", at=stamp(5), name="unscoped", seconds=1, exit=0, suite=unknown)
        self.log(rid, 6, "step_start", **scope)
        self.check_wait(rid, 7, inferred, "full-suite-lock")
        self.check_wait(rid, 9, inferred, "full-suite-lock", since=7)
        record.mark(rid, "check", at=stamp(9), name="inferred", seconds=1, exit=0, suite=inferred, **scope)
        paused = self.project(rid, 8)
        self.assert_timing(paused, active=[], categories=dict(checks=5, queue_wait=3))
        blocker, = paused["blockers"]
        self.assert_timing(blocker, suite=inferred, **scope)

        self.check_wait(rid, 10, held, "full-suite-lock", **scope)
        self.transition(lease, builder, 13, "running", writing=True, role="build")
        for at, seconds, suite in ((15, 4, other), (16, 2, f"{other}/lint")):
            record.mark(rid, "check", at=stamp(at), name=suite, seconds=seconds, exit=0, suite=suite, **scope)
        self.assertEqual(self.project(rid, 14)["active"], [{"run_id": rid, "activity": "worker.start",
                                                           "category": "writing", "since": stamp(13),
                                                           "source_id": builder}])
        active = self.project(rid, 17)
        self.assert_timing(active, categories=dict(writing=4, checks=8, queue_wait=11), overlap=[
            {"activity": {"checks": 1, "queue_wait": 1}, "seconds": 2},
            {"activity": {"checks": 1, "queue_wait": 1, "writing": 1}, "seconds": 1},
            {"activity": {"checks": 2, "queue_wait": 1, "writing": 1}, "seconds": 1},
            {"activity": {"queue_wait": 1, "writing": 1}, "seconds": 2},
        ])
        self.assertEqual({(span["activity"], span["source_id"]) for span in active["active"]},
                         {("checks", None), ("worker.start", builder)})

        self.check_wait(rid, 21, held, "full-suite-lock", since=10, **scope)
        record.mark(rid, "check", at=stamp(21), name="unit", seconds=4, exit=0, suite=held, **scope)
        self.transition(lease, builder, 22, "succeeded")
        self.log(rid, 26, "step_end", **scope)
        self.transition(lease, gate, 27, "succeeded")
        self.settle(rid, 29, "pr")
        self.assertEqual(self.project(rid, 4), live)
        self.assert_timing(self.project(rid, 30), elapsed_seconds=29, unattributed_seconds=4, overlap_seconds=11,
                           blockers=[], categories=dict(writing=9, checks=16, queue_wait=15))

    def test_classic_tracker_wait_ends_when_reply_is_saved_not_when_provider_allows_resume(self):
        rid = "question"
        self.run_row(rid, 0)
        self.log(rid, 5, "question", summary="Which release should we target?")
        self.settle(rid, 6, "needs-info")
        self.assertEqual([blocker["cause"] for blocker in self.project(rid, 20)["blockers"]], ["person"])
        repo = config.Repo("arete", paths.RUNS_DIR)
        source = mock.Mock()
        source.reply_since.return_value = "Target the next release."
        self.at(30)
        self.assertEqual(poll.tracker_reply(repo, source, "964", False, from_queue=False, from_feedback=False), "")
        source.reply_since.assert_called_once_with("964", stamp(5))
        reply = run_lines.path(rid).with_name(pipeline.ANSWER_FILE)
        saved_at = clock(30).timestamp()
        os.utime(reply, (saved_at, saved_at))
        held = self.project(rid, 60)
        self.assert_timing(held, through_at=stamp(30), elapsed_seconds=30, blockers=[], categories=dict(owner_wait=25))
        self.assertEqual(self.project(rid, 120), {**held, "as_of": stamp(120)})
        self.assertEqual([blocker["cause"] for blocker in self.project(rid, 29)["blockers"]], ["person"])

        self.run_row("continuation", 150)
        self.log("continuation", 150, "answer", asked_in=rid)
        pipeline.opening_waits("continuation", runs.run_get(rid), "")
        self.settle("continuation", 160, "pr")
        self.assert_timing(self.project("continuation", 170), elapsed_seconds=160, unattributed_seconds=135,
                           blockers=[], categories=dict(owner_wait=25))

    def test_classic_timing_uses_review_atoms_and_command_intervals(self):
        rid = "classic"
        self.run_row(rid, 0)
        self.step(rid, "review", 4, 9, atom="review")
        self.step(rid, "implement", 10, 16, atom="review_repair")
        self.step(rid, "review2", 18, 23, atom="review")
        self.step(rid, "review3", 19, 21, atom="review", carried="other")
        self.step(rid, "review4", 24, 26)
        for at, seconds, name, exit_code in ((29, 4.25, "lint", 0), (29, 4.25, "lint", 0), (33, 3, "unit", 1),
                                             (41, 8, "unit", 0), (48, 4, "late", 0)):
            record.mark(rid, "check", at=stamp(at), name=name, command=name, seconds=seconds, exit=exit_code)
        self.settle(rid, 47, "pr")
        self.assert_timing(self.project(rid, 50), elapsed_seconds=47, unattributed_seconds=18, overlap_seconds=0,
                           categories=dict(checks=13, review=10, retry=6))

    def test_missing_history_does_not_turn_pr_close_or_recovery_gap_into_work(self):
        self.run_row("pruned", 0, status="merged", finished=stamp(100))
        self.assert_timing(self.project("pruned", 150), elapsed_complete=False, elapsed_seconds=0)
        self.run_row("continuation", 10, status="failed", finished=stamp(20))
        self.log("continuation", 10, "route", continues="pruned")
        self.step("continuation", "implement", 12, 15, atom="check_repair")
        self.log("continuation", 16, "step_start", step="verify")
        self.run_row("environment", 28, status="failed", finished=stamp(30), environment_retry_of="continuation")
        self.assert_timing(self.project("pruned", 150, "environment"), elapsed_complete=False, elapsed_seconds=30,
                           unattributed_seconds=27, categories=dict(retry=3))

    def test_reopened_pr_without_settlement_keeps_its_close_time_unknown(self):
        self.run_row("reopened", 0, status="pr", finished=stamp(10))
        self.step("reopened", "verify", 2, 5)
        self.at(100)
        self.assertTrue(runs.move("reopened", "rejected", ("pr",), finished=stamp(100)))
        self.at(120)
        self.assertTrue(runs.move("reopened", "pr", ("rejected",)))
        self.assert_timing(self.project("reopened", 150), elapsed_complete=False, elapsed_seconds=5,
                           categories=dict(checks=3))
