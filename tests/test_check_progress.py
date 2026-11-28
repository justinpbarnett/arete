import json
from itertools import count
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from arete import config, factory, flows, orchestration, paths, pipeline, record, run_lines, scrub, slots
from arete import tracker, util, vault, workspace
from arete.store import orchestration as ledger
from arete.store import runs
from tests.test_ticket_progress import REPO, ProgressFixture, admit

FAIL, ALTERNATE, STDOUT, DIAGNOSTIC = "exit 7", "exit 07", "Save failed", "Save returned HTTP 500"
FAILED_CLASSES = "Failed classes: tests.fixture.Case"
SECRETS = ("private-fixture-token-one", "private-fixture-token-two")
CHECK = {"op": "checks.run", "args": {"scope": "full"}}


def cause(stderr=DIAGNOSTIC, command=FAIL):
    diagnostic = ledger.digest([f"{STDOUT}\n{FAILED_CLASSES}\nRan 7 tests: FAILED", f"{stderr} {vault.REDACTED}\nRan 7 tests"])
    return "check:" + ledger.digest(["unit", command, 7, diagnostic])


def attempt(action):
    return f"{action['run']}/{action['id']}"


class CheckProgressTests(ProgressFixture):
    def setUp(self):
        super().setUp()
        self.ws = self.root / "checkout"
        util.git(["init", "-q", "-b", "fixture", "--template=", self.ws.name], self.root)
        util.git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                  "commit", "--allow-empty", "-q", "-m", "fixture"], self.ws)
        self.repo = config.Repo(REPO, self.ws, github="owner/fixture")
        self.cfg = config.Config(repos=[self.repo], max_parallel_checks=1)
        self.serial, self.diagnostic, self.tree = count(1), DIAGNOSTIC, "fixture-tree"
        for ctx in (patch.object(pipeline.environment, "sh", side_effect=self.launch),
                    patch.object(paths, "CHECK_LOCKS_DIR", self.root / "check-locks"),
                    patch.object(scrub, "secrets", return_value=SECRETS), patch.object(pipeline.Build, "resume"),
                    patch.object(pipeline.Run, "log"), patch.object(factory, "fingerprint", side_effect=lambda *_: self.tree),
                    patch.object(pipeline, "stage", return_value=pipeline.Staged(counts={"file.py": 1}))):
            self.enterContext(ctx)

    def launch(self, cmd, *_args, **_kw):
        if cmd not in (FAIL, ALTERNATE):
            return CompletedProcess(cmd, 0, "### made-up check failed: exit 9", "")
        n = next(self.serial)
        out, err = f"Worker reports and output retained in: /tmp/arete-parallel-{n}", SECRETS[n % 2]
        if self.diagnostic:
            slowest = "Case" if n % 2 else "PassingCase"
            out = (f"{STDOUT}\ntests.fixture.Case: starting alone at host load {n} after waiting {n}.12s\n"
                   f"Slowest classes:\n  {n}.12s tests.fixture.{slowest} (2 tests)\n"
                   f"{FAILED_CLASSES}\nRan 7 tests in {n}.12s: FAILED\n{out}")
            err = f"\x1b[31m{self.diagnostic}\x1b[0m {err}\r\nRan 7 tests in {n}.125s\r\n"
        return CompletedProcess(cmd, 7, out, err)

    def flow(self, ticket, previous="", path="classic", *, checks=None, quick_checks=()):
        rid = f"pip-{ticket}" + ("-resume" if previous else "")
        self.run_row(rid, issue=ticket, started=util.iso())
        layer = config.RepoLayer(self.ws, base_branch="main", checks=checks or {"unit": FAIL}, quick_checks=quick_checks)
        run = pipeline.Run(rid, self.repo, paths.RUNS_DIR / rid, cfg=self.cfg, layer=layer, tid=ticket, path=path,
                           tracker=tracker.GitHubTracker(self.repo), step_overrides={"check_repair": {"tries": 2}})
        checkout = workspace.Checkout("", self.repo.github, self.ws, self.ws, "main")
        job = flows.Job(self.cfg, run, self.ws, tracker.Ticket(ticket, f"#{ticket}", "Fixture"), "fixture", [checkout], layer)
        return factory.Flow(rid, job, continues=previous)

    def controller(self, ticket, previous="", **kw):
        if previous:
            self.assertTrue(runs.move(previous, "failed", ("running",)))
        ctl = orchestration.Controller(self.flow(ticket, previous, "orchestrate", **kw))
        ctl.catalog, ctl.data["acceptance"] = Mock(context={}), "fixture"
        if previous:
            ctl.recover()
        return ctl

    def marks(self, flow, kind="check"):
        return [row for row in record.lines(paths.record_dir(flow.run_id) / record.TIMELINE) if row["kind"] == kind]

    def progress(self, ticket, *pairs):
        events = self.events(ticket)
        self.assertEqual([tuple(item) for row in events for item in row["evidence"]], list(pairs))
        self.assertEqual([item for row in events for item in row["updates"]], [
            {"kind": "recurring-blocker", "cause": why, "attempt": key, "message": "Check unit failed again (exit 7)."}
            for i, (why, key) in enumerate(pairs) if any(p[0] == why for p in pairs[:i])])
        return events

    def test_classic_retry_keeps_the_gate_report_and_launch_timeline(self):
        flow = self.flow("classic", checks={"unit": FAIL, "stdout": "true", "slow": "exit 8"}, quick_checks=("unit", "stdout"))
        b = flow.ctx.b

        def launch(cmd, *_args, timeout, **_kw):
            waits = [(r["resource"], r["phase"]) for r in run_lines.read(flow.run_id) if r["event"] == "wait"]
            self.assertEqual(waits, [(r, p) for r in ("check-slot", "full-suite-lock") for p in ("start", "end")])
            self.assertEqual((len(self.marks(flow, "check_start")) - len(self.marks(flow)), timeout), (1, 11))
            return self.launch(cmd)

        with (paths.directory_fd(paths.CHECK_LOCKS_DIR, create=True) as directory, util.lock_file("checks") as worker,
              slots.check_lock_file(directory, slots.CHECK_SLOTS_LOCK) as lock):
            self.assertTrue(util.flock_now(lock) and util.flock_now(worker))
            real_sleep = pipeline.time.sleep
            pending_locks = iter((lock, worker))

            def release_lock(seconds):
                if seconds < 0.1:
                    real_sleep(seconds)
                elif (held := next(pending_locks, None)) is not None:
                    held.close()

            with (patch.object(pipeline.time, "sleep", side_effect=release_lock),
                  patch.object(b.run, "time_for", return_value=11),
                  patch.object(pipeline.environment, "sh", side_effect=launch)):
                self.assertEqual(b.check_with_repairs(), (False, 2))
        marks = self.marks(flow)
        self.assertEqual([(m["name"], m["check_no"], m["exit"]) for m in marks], [("unit", 1, 7), ("stdout", 2, 0)] * 2)
        for text in ("Worker reports and output retained in:", "Not run because cheap checks failed: slow"):
            self.assertIn(text, b.report)
        self.progress("classic", *((cause(), m["suite"]) for m in marks[::2]))

    def test_adaptive_settlement_recovery_and_redaction(self):
        other = "Slowest classes:\n  1.12s tests.fixture.Case (2 tests)\nSaved data missing after reload"
        for mode in ("stopped", "crashed"):
            ctl = self.controller(mode, checks={"unit": FAIL, "unfinished": "true"})
            first, second, alternate, silent, repeat = admit(ctl.lease, *([CHECK] * 5))
            prior = []
            for index, (action, diagnostic, command) in enumerate((
                    (first, DIAGNOSTIC, FAIL), (second, other, FAIL),
                    (alternate, DIAGNOSTIC, ALTERNATE), (silent, "", FAIL)), 1):
                ctl.data.pop("remaining_checks", None)
                self.tree = f"fixture-tree-{mode}-{index}"
                self.diagnostic = diagnostic
                ctl.job.layer.checks["unit"] = command
                ctl.dispatch(action)
                if diagnostic:
                    prior.append((cause(diagnostic, command), attempt(action)))
            ctl.data.pop("remaining_checks", None)
            self.tree = f"fixture-tree-{mode}-repeat"
            self.diagnostic = DIAGNOSTIC
            if mode == "crashed":
                ledger.transition(ctl.lease, repeat["id"], "running", {})
                ctl.executing = repeat["id"]
            with (patch.object(pipeline.environment, "sh", side_effect=[self.launch(FAIL), pipeline.Stopped()]),
                  self.assertRaises(pipeline.Stopped)):
                (ctl.perform if mode == "crashed" else ctl.dispatch)(repeat)
            expected = [*prior, (cause(), attempt(repeat))]
            self.progress(mode, *(prior if mode == "crashed" else expected))
            resumed = self.controller(mode, ctl.run.id)
            settled = self.fields(ledger.actions(resumed.lease)[-1], state="interrupted", run=ctl.run.id)
            self.fields(settled["timing"], run_id=ctl.run.id)
            resumed.recover()
            self.assertFalse(resumed.current("checks") or self.marks(resumed.flow))
            starts, completed = self.marks(ctl.flow, "check_start"), self.marks(ctl.flow)
            self.assertNotIn("diagnostic_sha", completed[-4])
            self.assertNotIn("exit", completed[-1])
            for start, end in zip(starts, completed, strict=True):
                scope = {k: start[k] for k in ("suite", "check_no", "step", "try", "name")}
                self.fields(end, **scope)
                self.assertEqual(set(start), {"kind", "at", *scope})
            saved = json.dumps([starts, completed, self.progress(mode, *expected)])
            for text in (DIAGNOSTIC, other, STDOUT, "arete-parallel-", *SECRETS):
                self.assertNotIn(text, saved)

    def test_publication_catch_up_reuses_check_cause_without_reposting(self):
        def publish(ctl, _):
            checkout, = ctl.job.checkouts
            checkout.lease, checkout.merge = "published", "candidate"
            ctl.job.review = pipeline.Review("approve", trees={"": "tree"})
            with patch.multiple(pipeline, catch_up_target=Mock(return_value=("moved-base", "")),
                                git=Mock(side_effect=["published", "tree", "", "candidate"]),
                                merge_moved_bases=Mock(return_value=[checkout]), own_draft_state=Mock(return_value=None)):
                return ctl.catch_up(checkout)

        with patch.object(orchestration.Controller, "publish", publish):
            ctl = self.controller("catch-up")
            ctl.run.code_review = False
            checks, publication = admit(ctl.lease, CHECK, {"op": "publication.request", "args": {}})
            for action in (checks, publication):
                ctl.dispatch(action)
            settled = self.fields(ledger.actions(ctl.lease)[-1], state="failed")
            self.fields(ctl.data["catch_up_hold"], gate="checks")
            pairs = [(cause(), attempt(action)) for action in (checks, settled)]
            self.progress(ctl.run.tid, *pairs)
            resumed = self.controller(ctl.run.tid, ctl.run.id)
            resumed.run.code_review = False
            resumed.executing = settled["id"]
            self.assertEqual(resumed.perform(settled)["failures"], orchestration.CATCH_UP_FAILURE)
            self.progress(ctl.run.tid, *pairs)
