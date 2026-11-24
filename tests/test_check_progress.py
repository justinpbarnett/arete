import json
import subprocess
from unittest.mock import DEFAULT, Mock, patch, sentinel

from arete import config, factory, flows, orchestration, paths, pipeline, record, run_lines, scrub
from arete import slots, tracker, util, workspace
from arete.store import db, runs, ticket_progress
from arete.store import orchestration as ledger
from arete.verify import Verified
from tests import test_ticket_progress as fixtures
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

FAIL = "printf '%s' \"$OUT\"; printf '%s' \"$ERR\" >&2; exit 7"
STDOUT, DIAGNOSTIC = "Save failed", "Save returned HTTP 500"
WHAT, OBSERVED = "The list shows saved items", "No saved items"
UNIT = ("unit", 7)
CHECK_AGAIN = "Check unit failed again (exit 7)."
VERIFY_AGAIN = f"In-use verification failed again: {WHAT}."


def check_cause(stderr=DIAGNOSTIC):
    return "check:" + ledger.digest(["unit", FAIL, 7, ledger.digest([STDOUT, stderr])])


def attempt(action):
    return f"{action['run']}/{action['id']}"


class CheckProgressTests(fixtures.ProgressFixture):
    def setUp(self):
        super().setUp()
        self.ws = self.root / "checkout"
        util.git(["init", "-q", "-b", "fixture", "--template=", self.ws.name], self.root)
        self.repo = config.Repo(fixtures.REPO, self.ws, github="owner/fixture")
        self.cfg = config.Config(repos=[self.repo], max_parallel_checks=1)
        self.output = {"OUT": STDOUT, "ERR": DIAGNOSTIC}
        self.shell = self.enterContext(patch.object(pipeline.environment, "sh", side_effect=lambda cmd, *_a, cwd, **_kw:
            subprocess.run(cmd, shell=True, cwd=cwd, env=self.output, timeout=CHILD_HANG_TIMEOUT, capture_output=True, text=True)))
        self.verify = self.enterContext(patch.object(pipeline, "use_product"))
        self.repair = self.enterContext(patch.multiple(pipeline.Build, recheck=DEFAULT, resume=DEFAULT))["resume"]
        for ctx in (patch.object(paths, "CHECK_LOCKS_DIR", self.root / "check-locks"),
                    patch.object(scrub, "secrets", return_value=()), patch.object(pipeline.Run, "log"),
                    patch.object(factory, "fingerprint", return_value="fixture-tree"),
                    patch.object(pipeline, "stage", return_value=pipeline.Staged(counts={"file.py": 1}))):
            self.enterContext(ctx)

    def flow(self, ticket, path="classic", *, previous="", dry=False, only="", **layer_kw):
        rid = f"pip-{ticket}" + ("-resume" if previous else "")
        self.run_row(rid, issue=ticket, started=util.iso(), dry_run=int(dry), only_step=only)
        layer = config.RepoLayer(self.ws, **(dict(base_branch="main", verify=config.Verify(url=""), checks={"unit": FAIL}) | layer_kw))
        run = pipeline.Run(rid, self.repo, paths.RUNS_DIR / rid, cfg=self.cfg, layer=layer, tid=ticket, path=path,
                           tracker=tracker.GitHubTracker(self.repo), dry_run=dry, alone=bool(only),
                           step_overrides={"check_repair": {"tries": 2}, "verify": {"tries": 3}})
        run.dir.mkdir(parents=True)
        checkout = workspace.Checkout("", self.repo.github, self.ws, self.ws, "main")
        job = flows.Job(self.cfg, run, self.ws, tracker.Ticket(ticket, f"#{ticket}", "Fixture"), "fixture", [checkout], layer)
        return factory.Flow(rid, job, continues=previous)

    def controller(self, ticket, previous="", **layer_kw):
        if previous:
            self.assertTrue(runs.move(previous, "failed", ("running",)))
        ctl = orchestration.Controller(self.flow(ticket, "orchestrate", previous=previous, **layer_kw))
        ctl.catalog = Mock(route=lambda *_: ctl.run.settings("verify"), context={})
        ctl.data["acceptance"] = "fixture"
        if previous:
            ctl.recover()
        return ctl

    def admit(self, ctl, op="checks.run", *, turn=None, **args):
        lease = ctl.lease
        call = {"op": op, "args": ({"scope": "full", **args} if op == "checks.run" else args)}
        if turn is None:
            return fixtures.admit(lease, call)[0]
        return ledger.accept(lease, turn, ledger.snapshot(lease)["version"], {"calls": [call]}, None)[0]

    def checks(self, flow):
        return [row for row in record.lines(paths.record_dir(flow.run_id) / record.TIMELINE) if row["kind"] == "check"]

    def exits(self, flow):
        return [(row["name"], row.get("exit")) for row in self.checks(flow)]

    def progress(self, ticket):
        with db.reader() as c:
            data = [json.loads(row["data"]) for row in ticket_progress.rows(c, self.repo.name, ticket)]
        return tuple([v for row in reversed(data) for v in row[key]] for key in ("evidence", "updates"))

    def assert_progress(self, ticket, pairs=(), message=CHECK_AGAIN):
        pairs = [list(pair) for pair in pairs]
        updates = [dict(kind="recurring-blocker", cause=cause, attempt=key, message=message)
                   for i, (cause, key) in enumerate(pairs) if any(p[0] == cause for p in pairs[:i])]
        self.assertEqual(self.progress(ticket), (pairs, updates))

    def test_classic_repair_counts_real_check_exits(self):
        flow = self.flow("classic", checks={"unit": FAIL, "stdout": "echo '### made-up check failed: exit 9'", "slow": "exit 8"},
                         quick_checks=("unit", "stdout"))
        self.assertEqual(flow.ctx.b.check_with_repairs(), (False, 2))
        self.assertEqual(self.exits(flow), [UNIT, ("stdout", 0)] * 2)
        self.assert_progress("classic", [(check_cause(), row["suite"]) for row in self.checks(flow)[::2]])

    def test_only_measured_verification_causes_recur_across_repair_and_resume(self):
        measured = dict(what=WHAT, how="Opened the list", saw=OBSERVED, passed=False)
        mixed = Verified("fail", checks=[measured | extra for extra in (
            {}, {"how": "Refreshed"}, {"what": "Timing requirement"}, {"passed": None}, {"how": ""}, {"saw": " "},
            {"cause": "environment"})], unmeasured_requirements=[" timing\n requirement "])
        other = "The list never loaded"
        changed = Verified("fail", checks=[measured | {"saw": other}])
        matching = Verified("fail", checks=[measured | {"how": "Refreshed", "saw": " no SAVED\n items "}])
        passed = Verified("pass", checks=[measured | {"passed": True}])
        causes = ["verification:" + ledger.digest([WHAT.lower(), saw.lower()]) for saw in (OBSERVED, other, OBSERVED)]
        for path in ("classic", "feature", "patch", "adaptive"):
            with self.subTest(path=path):
                self.verify.reset_mock()
                self.repair.reset_mock()
                if path == "adaptive":
                    self.verify.side_effect = [mixed, changed, matching]
                    ctl = self.controller(path)
                    first, second, repeat = [self.admit(ctl, "verification.run", route_id="fixture", effort="", goal_refs=[])
                                             for _ in causes]
                    ctl.dispatch(first)
                    ctl.dispatch(second)
                    self.assert_progress(path, zip(causes[:2], map(attempt, (first, second))), VERIFY_AGAIN)
                    self.controller(path, ctl.run.id).dispatch(repeat)
                    keys = map(attempt, (first, second, repeat))
                else:
                    self.verify.side_effect = [mixed, changed, passed, matching, passed]
                    flow = self.flow(path, path)
                    for calls in ((3, 2), (3, 2), (5, 3)):
                        outcome = flows.step(flow, "verify", tree=str(self.ws))
                        self.assertEqual((outcome.passed, Verified.stored(outcome.data["verified"])), (True, passed))
                        self.assertEqual((self.verify.call_count, self.repair.call_count), calls)
                        if not flow.continues:
                            flow = self.flow(path, path, previous=flow.run_id)
                    keys = (key for _, key in self.progress(path)[0])
                self.assert_progress(path, zip(causes, keys, strict=True), VERIFY_AGAIN)

    def test_same_exit_only_recurs_for_the_same_cause_across_resume_and_interruption(self):
        other = "Saved data missing after reload"
        for mode in ("accepted", "stopped", "crashed"):
            with self.subTest(mode=mode):
                ctl = self.controller(mode, checks={"unit": FAIL, "unfinished": "true"})
                first, second, silent, repeat = [self.admit(ctl) for _ in range(4)]
                for action, diagnostic in ((first, DIAGNOSTIC), (second, other), (silent, "\x1b[31m  \n\x1b[0m")):
                    with patch.dict(self.output, OUT="" if action == silent else STDOUT, ERR=diagnostic):
                        ctl.dispatch(action)
                prior = [(check_cause(), attempt(first)), (check_cause(other), attempt(second))]
                expected = [*prior, (check_cause(), attempt(repeat))]
                self.assertEqual(self.exits(ctl.flow), [UNIT, ("unfinished", 0)] * 3)
                self.assertNotIn("diagnostic_sha", self.checks(ctl.flow)[-2])
                self.assertIs(ledger.actions(ctl.lease)[2]["result"]["passed"], False)
                self.assert_progress(mode, prior)
                self.output["ERR"] = f"\n \x1b[31m{DIAGNOSTIC}\x1b[0m\n"
                interrupted = mode != "accepted"
                if interrupted:
                    if mode == "crashed":
                        ledger.transition(ctl.lease, repeat["id"], "running", {})
                        ctl.executing = repeat["id"]
                    output = self.shell.side_effect(FAIL, cwd=self.ws)
                    with patch.object(self.shell, "side_effect", [output, pipeline.Stopped()]), self.assertRaises(pipeline.Stopped):
                        (ctl.perform if mode == "crashed" else ctl.dispatch)(repeat)
                    self.assertEqual(self.exits(ctl.flow)[6:], [UNIT, ("unfinished", None)])
                    self.assert_progress(mode, expected if mode == "stopped" else prior)
                resumed = self.controller(mode, ctl.run.id)
                with self.assertRaisesRegex(ledger.Conflict, "stale controller generation"):
                    ctl.dispatch(repeat)
                if not interrupted:
                    resumed.dispatch(repeat)
                settled = ledger.actions(resumed.lease)[-1]
                self.fields(settled, run=ctl.run.id, state="interrupted" if interrupted else "succeeded")
                self.assertEqual(settled["timing"]["run_id"], ctl.run.id if interrupted else resumed.run.id)
                self.assertIs(settled["result"].get("passed", sentinel.unsettled), sentinel.unsettled if interrupted else False)
                self.assertFalse(resumed.current("checks"))
                self.assert_progress(mode, expected)
                resumed.recover()
                replayed = self.admit(resumed, turn=repeat["turn"])
                self.assertEqual(replayed, settled)
                resumed.dispatch(replayed)
                self.assertEqual(self.exits(resumed.flow), [] if interrupted else [UNIT])
                self.assert_progress(mode, expected)
                recorded = json.dumps([self.checks(ctl.flow), self.checks(resumed.flow), self.progress(mode)])
                for text in (DIAGNOSTIC, other, STDOUT):
                    self.assertNotIn(text, recorded)

    def test_publication_catch_up_reports_recurring_check_once_across_replay(self):
        def publish(ctl, _):
            checkout, = ctl.job.checkouts
            checkout.lease, checkout.merge = "published", "candidate"
            ctl.job.review = pipeline.Review("approve", trees={"": "tree"})
            with patch.multiple(pipeline, catch_up_target=Mock(return_value=("moved-base", "")),
                                git=Mock(side_effect=["published", "tree", "", "candidate"]),
                                merge_moved_bases=Mock(return_value=[checkout]), own_draft_state=Mock(return_value=None)):
                return ctl.catch_up(checkout)

        self.enterContext(patch.object(orchestration.Controller, "publish", publish))
        ctl = self.controller("catch-up")
        checks = self.admit(ctl)
        ctl.dispatch(checks)
        ctl.dispatch(self.admit(ctl, "publication.request"))
        settled = ledger.actions(ctl.lease)[-1]
        self.assertEqual((settled["state"], settled["result"]["failures"], ctl.data["catch_up_hold"]["gate"]),
                         ("failed", orchestration.CATCH_UP_FAILURE, "checks"))
        expected = [(check_cause(), attempt(action)) for action in (checks, settled)]
        self.assert_progress(ctl.run.tid, expected)
        resumed = self.controller(ctl.run.tid, ctl.run.id)
        resumed.executing = settled["id"]
        self.assertEqual(resumed.perform(settled)["failures"], orchestration.CATCH_UP_FAILURE)
        self.assertEqual(self.exits(resumed.flow), [UNIT])
        self.assert_progress(ctl.run.tid, expected)

    def test_missing_database_or_unfinished_checks_do_not_create_evidence(self):
        for ticket, flags in (("dry", {"dry": True}), ("single", {"only": "checks"}), ("no-db", {})):
            check = self.flow(ticket, **flags).ctx.b.check
            with patch.object(runs, "run_get", side_effect=runs.run_get if flags else db.NoDatabase):
                self.assertFalse(check())
            self.assert_progress(ticket)
        self.shell.side_effect = OSError("not launched")
        self.assertRaises(OSError, self.flow("no-exit").ctx.b.check)
        self.assert_progress("no-exit")

    def test_slot_and_full_suite_waits_keep_the_check_scope(self):
        flow = self.flow("contention", "patch", checks={"unit": "true"})
        with paths.directory_fd(paths.CHECK_LOCKS_DIR, create=True) as directory, util.lock_file("checks") as worker:
            admission = self.enterContext(slots.check_lock_file(directory, slots.CHECK_SLOTS_LOCK))
            self.assertTrue(util.flock_now(admission) and util.flock_now(worker))
            with patch.object(pipeline.time, "sleep", side_effect=(lock.close() for lock in (admission, worker))):
                self.assertTrue(flows.step(flow, "checks", tree=str(self.ws)).passed)
        mark, = self.checks(flow)
        waits = [row for row in run_lines.read(flow.run_id) if row["event"] == "wait"]
        self.assertEqual([(row["resource"], row["phase"]) for row in waits],
                         [(r, phase) for r in ("check-slot", "full-suite-lock") for phase in ("start", "end")])
        self.assertEqual([row["since"] for row in waits[1::2]], [row["at"] for row in waits[::2]])
        self.assertEqual([{k: row[k] for k in ("suite", "step", "try")} for row in waits],
                         [{k: mark[k] for k in ("suite", "step", "try")}] * 4)
