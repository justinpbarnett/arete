import json
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import patch

from arete import pipeline, record, run_lines, run_timing, util
from arete.store import asks, orchestration, runs, sources
from tests.test_ticket_progress import GITHUB, HEAD, REPO, ProgressFixture, admit

ORIGIN = datetime(2030, 1, 2, tzinfo=UTC)
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

    def at(self, seconds, operation=None, *args, **kwargs):
        self.now.return_value = clock(seconds)
        return operation(*args, **kwargs) if operation is not None else None

    def run_row(self, rid, start=0, *, parent=None, steps=(), **fields):
        self.at(start, super().run_row, rid, started=stamp(start), **fields)
        if parent:
            event, field, source = parent
            self.log(rid, start, event, **{field: source})
        for step in steps:
            self.step(rid, *step)

    def settle(self, rid, at, status, *, report=None, **fields):
        self.assertIsNotNone(self.at(at, record.settle, rid, status, **fields))
        if report is not None:
            self.log(rid, at, run_lines.END, status=status, **report)

    def move(self, rid, at, status, previous, **fields):
        self.assertTrue(self.at(at, runs.move, rid, status, (previous,), **fields))

    def log(self, rid, seconds, event, **fields):
        self.at(seconds)
        path = run_lines.path(rid)
        path.parent.mkdir(exist_ok=True, parents=True)
        with path.open("a") as stream:
            stream.write(json.dumps({"run": rid, "ts": stamp(seconds), "event": event, **fields}) + "\n")

    def step(self, rid, step, start, end=None, **fields):
        self.log(rid, start, "step_start", step=step, **fields)
        if end is not None:
            self.log(rid, end, "step_end", step=step, **fields)

    def wait(self, rid, at, suite=None, resource=None, *, since=None, cause="slot", **fields):
        self.log(rid, at, "wait", cause=cause, at=stamp(at), suite=suite, resource=resource, **fields,
                 phase="start" if since is None else "end", since=stamp(since) if isinstance(since, int) else since)

    def publish_draft(self, rid, published, *, settled=None, prs=DRAFT, **report):
        if settled is not None:
            self.at(published, runs.note, rid, pr_url=prs)
        self.settle(rid, published if settled is None else settled, "pr", pr_url=prs, report=report)

    def hold_environment(self, rid):
        runs.save_environment_hold(rid, [(GITHUB, 52, DRAFT, HEAD)])

    def project(self, rid, at, *linked, categories=None, **expected):
        timing = run_timing.project(rid, as_of=clock(at))
        for member in linked:
            self.assertEqual(timing, self.project(member, at), member)
        if categories is not None:
            expected["category_seconds"] = dict.fromkeys(run_timing.CATEGORIES, 0) | categories
        return self.fields(timing, **expected) if expected else timing


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
        self.project("first", 15, overlap=[dict(activity=dict(writing=2), seconds=3)])
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
        self.at(50, sources.pass_over, REPO, self.issue)
        self.run_row("repair", 55, parent=("repair", "source", "first"))
        self.wait("repair", 50, cause="person", since=35, ts=stamp(55))
        self.wait("repair", 55, since=sources.take_passed_over(REPO, self.issue))
        repair, fix = self.accept("repair", 56, "worker.start")
        repair(fix, 57, "running", writing=True, role="review_repair")
        self.settle("repair", 64, "interrupted")
        self.run_row("resume", 70)
        resume, verify, review = self.accept("resume", 72, "verification.run", "review.run", resumes="repair")
        resume(fix, 72, "interrupted")
        resume(verify, 72, "running", end=79)
        resume(review, 80, "running", end=85)
        self.step("resume", "review2", 81, 84, atom="review")
        self.settle("resume", 87, "pr")
        self.move("resume", 120, "merged", "pr", finished=stamp(120))
        self.run_row("specific-step", 121, only_step="verify", parent=("route", "continues", "resume"))
        self.assertIsNone(self.project("specific-step", 150))
        self.project("resume", 150, "first", "repair", run_ids=["first", "repair", "resume"], elapsed_complete=True,
                     started_at=stamp(0), through_at=stamp(87), elapsed_seconds=87, unattributed_seconds=22,
                     categories=dict(writing=15, checks=12, review=9, owner_wait=12, queue_wait=10, retry=7),
                     overlap_seconds=6, overlap=[{"activity": {"writing": 2}, "seconds": 6}], active=[], blockers=[])

    def test_draft_wait_cutoffs_without_readiness(self):
        for outcome in ("merged", "rejected", "manual-release"):
            with self.subTest(outcome=outcome):
                self.run_row(outcome)
                if outcome == "manual-release":
                    self.hold_environment(outcome)
                    self.settle(outcome, 20, "pr", pr_url=DRAFT)
                    self.at(30, runs.release_environment_hold, outcome)
                    categories, unattributed = dict(queue_wait=10), 20
                else:
                    self.publish_draft(outcome, 8, settled=10, held="Review requested changes")
                    self.move(outcome, 40, outcome, "pr", finished=stamp(30))
                    categories, unattributed = dict(owner_wait=22), 8
                self.project(outcome, 50, through_at=stamp(30), elapsed_seconds=30,
                             unattributed_seconds=unattributed, categories=categories, blockers=[], overlap=[])

    def test_check_waits_match_by_run_suite_resource_and_start_without_pausing_other_work(self):
        rid, scope = "adaptive", {"step": "checks", "try": 3}
        self.run_row(rid)
        action, gate, builder = self.accept(rid, 1, "checks.run", "worker.start")
        action(gate, 2, "running")
        self.step(rid, "checks", 6, 26, **{"try": 3})
        held = dict(suite="held", resource="full-suite-lock")
        self.wait(rid, 7, **held)
        for at, invalid in enumerate((dict(run="wrong"), dict(suite="other"), dict(resource="check-slot"), dict(since=8)), 11):
            self.wait(rid, at, **(held | dict(since=7) | invalid))
        action(builder, 13, "running", writing=True, role="build", end=22)
        record.mark(rid, "check", at=stamp(15), seconds=4, suite="other", **scope)
        self.wait(rid, 21, since=7, **held)
        for _ in range(2):
            record.mark(rid, "check", at=stamp(21), seconds=4, suite="held", **scope)
        working = self.project(rid, 17, categories=dict(writing=4, checks=7, queue_wait=10), overlap_seconds=4)
        self.assertIn(dict(activity=dict(checks=1, queue_wait=1, writing=1), seconds=2), working["overlap"])
        self.fields(working["blockers"][0], cause="slot", since=stamp(7), **held, **scope)
        action(gate, 27, "succeeded")
        self.settle(rid, 29, "pr")
        self.project(rid, 30, elapsed_seconds=29, unattributed_seconds=4, overlap_seconds=9,
                     blockers=[], categories=dict(writing=9, checks=15, queue_wait=14))

    def inline_verify_repair(self, rid, repair="verify_fix2"):
        self.run_row(rid)
        scope = {"step": "verify", "try": 2}
        for at, call in enumerate((None, "verify", repair), 2):
            self.log(rid, at, "step_start", call=call, atom="check_repair" if at == 4 else "verify", **scope)
        return scope

    def test_verify_recheck_and_check_repair_are_sequential_but_independent_checks_overlap(self):
        rid = "patch"
        scope = self.inline_verify_repair(rid)
        record.mark(rid, "agent", at=stamp(4), step="verify_fix2", seconds=2)
        record.mark(rid, "check", at=stamp(6), suite="one", seconds=3, exit=1, **scope)
        self.project(rid, 8, categories=dict(checks=4, retry=2), overlap=[])
        self.log(rid, 9, "step_start", call="check_repair", atom="check_repair", **scope)
        live = self.project(rid, 10, categories=dict(checks=5, retry=3), overlap=[])
        self.assertEqual([span["activity"] for span in live["active"]], ["check_repair"])
        record.mark(rid, "agent", at=stamp(9), step="check_repair", seconds=3)
        record.mark(rid, "check", at=stamp(12), suite="two", seconds=2, **scope)
        record.mark(rid, "check", at=stamp(11), suite="independent", seconds=3)
        self.wait(rid, 14, cause="limit")
        self.wait(rid, 16, cause="limit", since=14)
        self.project(rid, 15, active=[])
        self.log(rid, 16, "step_start", call="verify2", atom="verify", **scope)
        self.log(rid, 18, "step_end", result="pass", **scope)
        self.settle(rid, 19, "pr")
        self.project(rid, 20, elapsed_seconds=19, unattributed_seconds=3, categories=dict(checks=10, retry=5, queue_wait=2),
                     overlap=[dict(activity=dict(checks=1, retry=1), seconds=1), dict(activity=dict(checks=2), seconds=2)])

    def test_inline_verify_repair_survives_interruption_without_an_agent_result(self):
        for status, repair in (("interrupted", "verify_fix2"), ("failed", "check_repair2")):
            with self.subTest(status=status):
                self.inline_verify_repair(status, repair)
                self.settle(status, 8, status, summary=record.INTERRUPTED)
                self.project(status, 12, elapsed_seconds=8, through_at=stamp(8), active=[], blockers=[], overlap=[],
                             categories=dict(checks=2, retry=4), unattributed_seconds=2)

    def test_interrupted_ci_and_run_slot_waits_stop_at_settlement_and_survive_resume(self):
        self.run_row("budget-only", -10, status="merged", finished=stamp(-5))
        self.project("budget-only", 20, elapsed_complete=False, elapsed_seconds=0)
        for cause in ("ci", "slot"):
            with self.subTest(cause=cause):
                rid, resume = f"{cause}-wait", f"{cause}-resume"
                self.run_row(rid)
                self.wait(rid, 4, cause=cause)
                self.settle(rid, 10, "interrupted")
                self.run_row(resume, 14, steps=[("implement", 15, 17), ("verify", 17)])
                folder = run_lines.path(resume).parent
                (folder / "run.log").write_text(f"{stamp(14)} [arete#964] restarting after run {rid} was interrupted\n")
                (folder / "budget.json").write_text('{"parents": ["budget-only"]}')
                self.settle(resume, 18, "failed")
                self.project(resume, 20, rid, run_ids=[rid, resume], elapsed_seconds=18, unattributed_seconds=10,
                             categories=dict(queue_wait=6, writing=2), blockers=[], overlap=[])
