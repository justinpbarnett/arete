import json
from datetime import timedelta
from functools import partial
from unittest.mock import patch

from arete import pipeline, record, run_lines, run_timing, util
from arete.store import asks, orchestration, runs
from tests.test_ticket_progress import GITHUB, REPO, ProgressFixture, admit

ORIGIN = util.parse_iso("2030-01-02T00:00:00Z")
DRAFT = f"https://github.com/{GITHUB}/pull/52"


def clock(seconds):
    return ORIGIN + timedelta(seconds=seconds)


def stamp(seconds):
    return util.iso(clock(seconds))


class TimingFixture(ProgressFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(pipeline, "SKIPPED_FILE", self.root / "skipped.json"))
        self.enterContext(patch.object(record.scrub, "secrets", return_value=()))
        self.now = self.enterContext(patch.object(util, "now", return_value=ORIGIN))

    def at(self, seconds, operation, *args, **kwargs):
        self.now.return_value = clock(seconds)
        return operation(*args, **kwargs)

    def run_row(self, rid, start=0, **fields):
        self.at(start, super().run_row, rid, started=stamp(start), **fields)

    def settle(self, rid, at, status, *, report=None, **fields):
        self.assertIsNotNone(self.at(at, record.settle, rid, status, **fields))
        if report is not None:
            self.log(rid, at, run_lines.END, status=status, **report)

    def move(self, rid, at, status, previous, **fields):
        self.assertTrue(self.at(at, runs.move, rid, status, (previous,), **fields))

    def log(self, rid, at, event, /, **fields):
        self.append(run_lines.path(rid), json.dumps(dict(run=rid, ts=stamp(at), event=event) | fields))

    def message(self, rid, at, text):
        self.append(run_lines.path(rid).with_name("run.log"), f"{stamp(at)} [arete#964] {text}")

    def append(self, path, line):
        path.parent.mkdir(exist_ok=True, parents=True)
        with path.open("a") as stream:
            stream.write(line + "\n")

    def step(self, rid, step, start, end=None, **fields):
        self.log(rid, start, "step_start", step=step, **fields)
        if end is not None:
            self.log(rid, end, "step_end", step=step, **fields)

    def wait(self, rid, at, *, since=None, cause="slot", **fields):
        self.log(rid, at, "wait", cause=cause, at=stamp(at), phase="start" if since is None else "end",
                 since=stamp(since) if since is not None else None, **fields)

    def check(self, rid, at, suite, seconds=None, **fields):
        kind, result = ("check_start", {}) if seconds is None else ("check", dict(seconds=seconds))
        record.mark(rid, kind, at=stamp(at), suite=suite, **result, **fields)

    def project(self, rid, at, *linked, categories=None, **expected):
        project = partial(run_timing.project, as_of=clock(at))
        timing = project(rid)
        for member in linked:
            self.assertEqual(timing, project(member), member)
        if categories is not None:
            expected["category_seconds"] = dict.fromkeys(run_timing.CATEGORIES, 0) | categories
        return self.fields(timing, **expected)


class RunTimingTests(TimingFixture):
    def accept(self, rid, at, *ops, resumes=""):
        lease = self.at(at, orchestration.acquire, rid, resumes, {})
        return partial(self.transition, lease), *(row["id"] for row in admit(lease, *(dict(op=op, args={}) for op in ops)))

    def transition(self, lease, aid, at, state, *, end=None, **result):
        self.at(at, orchestration.transition, lease, aid, state, result)
        if end is not None:
            self.transition(lease, aid, end, "succeeded")

    def test_parallel_builders_waits_repair_and_resume_share_one_wall_clock(self):
        self.run_row("first", 5)
        self.wait("first", 5, since=0)
        action, left, right, check = self.accept("first", 6, "worker.start", "worker.start", "checks.run")
        action(left, 8, "running", writing=True, role="build")
        action(left, 9, "running")
        action(right, 12, "running", writing=True, role="build")
        action(left, 18, "succeeded")
        action(right, 23, "succeeded")
        action(check, 24, "running", end=29)
        self.step("first", "checks", 25, 28)
        self.step("first", "review", 30, 34)
        self.at(35, asks.ask_add, "ask", "first", REPO, self.issue, "plan", "plan", "Need a decision", "", [], {})
        self.settle("first", 38, "needs-info")
        person, = self.project("first", 42, elapsed_seconds=42)["blockers"]
        self.fields(person, cause="person", since=stamp(35), source_id="ask")
        self.assertTrue(self.at(47, asks.ask_close, "ask", "answered"))
        self.run_row("repair", 55)
        self.log("repair", 55, "repair", source="first")
        self.wait("repair", 55, since=50)
        repair, fix = self.accept("repair", 56, "worker.start")
        repair(fix, 57, "running", writing=True, role="review_repair")
        record.mark("repair", "agent", at=stamp(58), step=f"worker-{fix}", seconds=4)
        self.settle("repair", 64, "interrupted")
        self.run_row("resume", 70)
        resume, verify, review = self.accept("resume", 72, "verification.run", "review.run", resumes="repair")
        resume(fix, 72, "interrupted")
        resume(verify, 72, "running", end=79)
        resume(review, 80, "running", end=85)
        self.settle("resume", 87, "pr")
        self.move("resume", 120, "merged", "pr", finished=stamp(120))
        self.run_row("specific-step", 121, only_step="verify")
        self.assertIsNone(run_timing.project("specific-step", as_of=clock(150)))
        self.project("resume", 150, "first", "repair", run_ids=["first", "repair", "resume"], elapsed_complete=True,
                     elapsed_seconds=87, unattributed_seconds=25, active=[], blockers=[],
                     categories=dict(writing=15, checks=12, review=9, owner_wait=12, queue_wait=10, retry=4),
                     overlap_seconds=6, overlap=[dict(activity=dict(writing=2), seconds=6)])

    def test_scoped_check_wait_does_not_pause_another_suite_or_builder(self):
        rid, scope = "adaptive", {"step": "checks", "try": 3}
        self.run_row(rid)
        action, gate, builder = self.accept(rid, 1, "checks.run", "worker.start")
        action(gate, 2, "running")
        self.step(rid, "checks", 6, 26, **{"try": 3})
        held = dict(suite="held", resource="full-suite-lock")
        self.wait(rid, 7, **held)
        self.wait(rid, 11, since=7, suite="other", resource="full-suite-lock")
        action(builder, 13, "running", writing=True, role="build", end=22)
        self.check(rid, 15, "other", 4, **scope)
        self.wait(rid, 21, since=7, **held)
        self.check(rid, 21, "held", 4, **scope)
        working = self.project(rid, 17, categories=dict(writing=4, checks=7, queue_wait=10), overlap_seconds=4)
        self.assertIn(dict(activity=dict(checks=1, queue_wait=1, writing=1), seconds=2), working["overlap"])
        self.fields(working["blockers"][0], cause="slot", since=stamp(7), **held, **scope)

    def test_check_start_pairs_with_its_receipt_and_does_not_extend_past_interruption(self):
        rid = "checks"
        self.run_row(rid)
        for suite in ("a", "b"):
            self.check(rid, 2, suite, name=suite, check_no=1)
        self.check(rid, 3, "a", 2, check_no=1)
        [active] = self.project(rid, 6, categories=dict(checks=4), overlap_seconds=3)["active"]
        self.fields(active, suite="b", name="b", check_no=1)
        self.settle(rid, 12, "interrupted")
        self.project(rid, 20, elapsed_seconds=12, unattributed_seconds=9, categories=dict(checks=3), overlap=[], active=[])

    def test_inline_repair_pauses_verification_but_not_independent_checks(self):
        rid, scope = "patch", {"step": "verify", "try": 2}
        self.run_row(rid)
        self.log(rid, 2, "step_start", **scope)
        self.log(rid, 4, "step_start", call="verify_fix2", atom="check_repair", **scope)
        record.mark(rid, "agent", at=stamp(4), step="verify_fix2", seconds=3)
        self.check(rid, 6, "independent", 3)
        self.check(rid, 7, "verify", 2, **scope)
        self.log(rid, 10, "step_end", **scope)
        self.settle(rid, 11, "done")
        self.project(rid, 20, elapsed_seconds=11, unattributed_seconds=3, categories=dict(checks=6, retry=3),
                     overlap=[dict(activity=dict(checks=1, retry=1), seconds=1), dict(activity=dict(checks=2), seconds=2)])

    def test_publication_catch_up_counts_its_nested_checks_and_review(self):
        rid, scope = "catch-up", {"step": "publish", "try": 2}
        self.run_row(rid)
        self.log(rid, 3, "step_start", **scope)
        self.check(rid, 4, "catch-up", 2, **scope)
        self.log(rid, 7, "step_start", call="verify2", atom="verify", **scope)
        record.mark(rid, "agent", at=stamp(8), step="verify2", seconds=3)
        self.log(rid, 12, "step_start", call="review3", atom="review", **scope)
        record.mark(rid, "agent", at=stamp(13), step="review3", seconds=3)
        self.log(rid, 17, "step_end", **scope)
        self.settle(rid, 19, "pr")
        self.project(rid, 25, elapsed_seconds=19, unattributed_seconds=11, categories=dict(checks=5, review=3), overlap=[])

    def test_interrupted_waits_cut_off_at_settlement_and_survive_resume(self):
        for cause in ("ci", "slot"):
            with self.subTest(cause=cause):
                rid, resumed = f"{cause}-wait", f"{cause}-resume"
                self.run_row(rid)
                self.wait(rid, 4, cause=cause)
                self.settle(rid, 10, "interrupted")
                self.run_row(resumed, 14)
                self.message(resumed, 14, f"restarting after run {rid} was interrupted")
                self.step(resumed, "implement", 15, 17)
                self.step(resumed, "verify", 17)
                self.settle(resumed, 18, "failed")
                self.project(resumed, 20, rid, elapsed_seconds=18, unattributed_seconds=10,
                             categories=dict(queue_wait=6, writing=2), blockers=[], overlap=[])
