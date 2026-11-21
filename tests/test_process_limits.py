import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Template
from types import SimpleNamespace
from unittest.mock import patch

from adws import feature
from arete import (
    codebase,
    config,
    dbstats,
    delegation,
    factory,
    flows,
    gh,
    jira,
    launch_lease,
    orchestration,
    outcomes,
    paths,
    pipeline,
    poll,
    principals,
    record,
    repairs,
    run_lines,
    runlimits,
    sandbox,
    scratch,
    steps,
    tasks,
    tracker,
    util,
    workspace,
)
from arete.agent import Result, Session
from arete.orchestration_api import MAX_BYTES, OPERATIONS, PROTOCOL, Catalog, describe_operations
from arete.store import asks as ask_store
from arete.store import db as foundation
from arete.store import insights as insights_store
from arete.store import orchestration as ledger
from arete.store import runs as run_store
from arete.store import tasks as tasks_store
from tests.harness import set_task_body


class Isolated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="arete-schema-")
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        cls.template = root / "state/arete.db"
        with contextlib.ExitStack() as patches:
            patches.enter_context(patch.object(foundation, "_CHECKED", set()))
            patches.enter_context(patch.object(dbstats.Timing, "finished"))
            for name, value in {"STATE_DIR": root / "state", "DB_FILE": cls.template,
                                "RUNS_DIR": root / "runs", "WORKTREES_DIR": root / "worktrees",
                                "LOCKS_DIR": root / "state/locks", "LOG_DIR": root / "logs"}.items():
                patches.enter_context(patch.object(paths, name, value))
            foundation.migrate()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="arete-limits-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(foundation, "_CHECKED", set()))
        self.patches.enter_context(patch.object(dbstats.Timing, "finished"))
        self.patches.enter_context(patch.object(tasks, "PENDING", {}))
        for name, value in {"STATE_DIR": self.root / "state", "RUNS_DIR": self.root / "runs",
                            "DB_FILE": self.root / "state/arete.db", "LOCKS_DIR": self.root / "state/locks",
                            "CONFIG_DIR": self.root / "config", "CONFIG_FILE": self.root / "config/config.toml",
                            "LOG_DIR": self.root / "logs", "EVENTS_FILE": self.root / "state/events.jsonl",
                            "SERVER_FILE": self.root / "state/server.json",
                            "WORKTREES_DIR": self.root / "worktrees"}.items():
            self.patches.enter_context(patch.object(paths, name, value))
        paths.CONFIG_DIR.mkdir()
        paths.ensure_dirs()
        shutil.copyfile(self.template, paths.DB_FILE)
        self.repo = config.Repo(name="fixture", path=self.root / "repo", tracker="local", pipeline=True)
        self.repo.path.mkdir()
        self.cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8)
        self.ticket = tasks_store.task_add(self.repo.name, "Fixture specification", "Fixture", "feat")
        self.local = tracker.LocalTracker(self.repo)

    def run(self, result=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return super().run(result)

    def made_run(self, previous="", forced=False):
        rid = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        directory = paths.RUNS_DIR / rid
        directory.mkdir()
        source = tracker.for_ticket(self.cfg, self.repo, self.ticket)
        run = pipeline.Run(rid, self.repo, directory, tracker=source, tid=self.ticket, cfg=self.cfg)
        run.budget = runlimits.Budget(run, runlimits.Policy(), previous, source.get(self.ticket), forced)
        return run


class CatchUpWorkspaceRecordTests(Isolated):
    branch = "arete/fixture"
    build_files = (".venv/pyvenv.cfg", "__pycache__/app.pyc")

    def setUp(self):
        super().setUp()
        self.ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
        self.layer = config.RepoLayer(self.repo.layer_dir, branch=self.branch, base_branch="main", verify=config.Verify())
        for patcher in (patch.object(config, "load", return_value=self.cfg),
                        patch.object(config, "load_layer", return_value=self.layer),
                        patch.object(sandbox, "AUTHORITY", self.root / "authority")):
            self.patches.enter_context(patcher)
        src = self.repo.path
        util.git(["init", "-q", "-b", "main"], src)
        util.git(["remote", "add", "origin", str(src)], src)
        (src / ".gitignore").write_text(".venv/\n__pycache__/\nlocal.txt\n")
        util.git(["add", ".gitignore"], src)
        util.git_as_owner(["commit", "-qm", "Initial base"], src)
        util.add_worktree(src, self.ws, self.branch, "main")
        (self.ws / "feature.txt").write_text("Branch change\n")
        util.git(["add", "feature.txt"], self.ws)
        util.git_as_owner(["commit", "-qm", "Branch change"], self.ws)
        self.head = util.git(["rev-parse", "HEAD"], self.ws).strip()
        self.checkout = workspace.Checkout("", self.repo.name, src, self.ws, "main", lease=self.head, onto=self.head)
        for name in self.build_files:
            path = self.ws / name
            path.parent.mkdir()
            path.write_text("Build output\n")

    def run_ticket(self, drive):
        status = pipeline.run_issue(self.repo.name, self.ticket, force=True, script=pipeline.Script("adw", drive))
        return status, run_store.newest_whole_run(self.repo.name, self.ticket)

    def fail_catch_up(self, incoming, why):
        src = self.repo.path
        (src / incoming).write_text("Base change\n")
        util.git(["add", "-f", incoming], src)
        util.git_as_owner(["commit", "-qm", "Move the base"], src)
        workspace.fetch(src, "main")
        base = util.git(["rev-parse", "origin/main"], self.ws).strip()

        def drive(flow):
            run = flow.ctx.run
            run.workspace = self.ws, [self.checkout]
            build = pipeline.Build(self.cfg, run, self.local.get(self.ticket), self.ws, [self.checkout], self.layer,
                                   5, "Fixture worktree", tools=[], deny=[])
            pipeline.catch_up_base(build, self.checkout, self.branch, base, "", "", before_push=self.unexpected_push)

        status, row = self.run_ticket(drive)
        self.assertEqual((status, row["summary"]), (1, why))
        self.assertEqual(util.git(["rev-parse", "HEAD"], self.ws).strip(), self.head)
        made = workspace.made_before(self.ws)
        for name in self.build_files:
            self.assertIn(f"{Path(name).parent}/", made)
            self.assertEqual((self.ws / name).read_text(), "Build output\n")
        return made

    def unexpected_push(self, *_):
        self.fail("A failed catch-up reached the push")

    def retry(self):
        def drive(flow):
            prepared = flows.step(flow, "prepare", tree=str(self.ws))
            return flows.ended(flow, flows.step(flow, "plan") if prepared.passed else prepared)

        with patch.object(pipeline.Run, "agent", return_value=Result(ok=False, error="Stop at the first worker")) as agent:
            status, row = self.run_ticket(drive)
        self.assertEqual(status, 1, row["summary"])
        return row["summary"], [call.args[0] for call in agent.call_args_list]

    def test_review_hold_records_build_files_and_retry_starts_planning(self):
        def verified(build, *_):
            tree = util.git(["rev-parse", "HEAD^{tree}"], build.ws).strip()
            return pipeline.verify.Verified("pass", checks=[{"passed": True}], native_trees={"": tree})

        with (patch.object(pipeline.Build, "check", return_value=True),
              patch.object(pipeline, "check_in_use", side_effect=verified),
              patch.object(pipeline, "_review", return_value=pipeline.Review("request_changes"))):
            self.fail_catch_up("incoming.txt", "the published tree lacks a current approving review")

        self.assertEqual(self.retry(), ("planning step failed", ["plan"]))

    def test_collision_leaves_the_path_off_record_and_retry_refuses_it(self):
        collision = self.ws / "local.txt"
        collision.write_text("Keep this file\n")

        made = self.fail_catch_up(collision.name, "the merge would overwrite ignored file `local.txt`")

        self.assertNotIn(collision.name, made)
        self.assertEqual(self.retry(), (
            f"the existing worktree {self.ws} of {self.repo.name} holds git-ignored files that arete has no record of "
            "leaving there (local.txt), so arete did not make it again and left them as they are; "
            "move or remove them, then run again", []))
        self.assertEqual(collision.read_text(), "Keep this file\n")


class ReviewRetryTests(Isolated):
    PR = "https://github.com/example/fixture/pull/1"
    BLOCKER = {"file": "notes.txt", "problem": "fixture blocker", "severity": "blocker", "category": "issue"}

    def review_build(self, *replies):
        run = self.made_run()
        run.step_overrides["review"] = {"tries": 2}
        flow = flows.start(self.cfg, run, {})
        job = flow.ctx
        job.ticket = self.local.get(self.ticket)
        with patch.object(pipeline, "workspace_note", return_value="Fixture workspace"):
            job.b.staged = pipeline.Staged(counts={"notes.txt": 1})

        def open_pr(flow, tree):
            return factory.Outcome("pass", data={
                "prs": {}, "opened": [], "changed": [], "pushed": [], "committed_lines": 1,
                "draft_why": flow.ctx.held_reason, "report_body": "", "summary": "",
            })

        with (patch.object(pipeline.runner, "run", side_effect=replies) as agent,
              patch.object(factory, "fingerprint", return_value="fixture-tree"),
              patch.object(flows, "documents", return_value=False),
              patch.dict(factory.STEPS, {
                  "checks": lambda *a, **kw: factory.Outcome("pass"),
                  "verify": lambda *a, **kw: factory.Outcome("pass", data={"verified": {"verdict": "pass"}, "names": []}),
                  "learn": lambda *a, **kw: factory.Outcome("pass"),
                  "pr": open_pr,
                  "publish": lambda *a: factory.Outcome("pass", data={"ending": "pr", "pr_url": self.PR}),
                  "respond": lambda _, ending, **kw: factory.Outcome("pass", data={"status": ending, "summary": "", **kw}),
              })):
            ending = feature.built(flow, [], prepared=True)
        self.assertEqual(ending, ("pr", "", self.PR))
        return flow, agent.call_args_list

    def test_review_retry_approves_and_reaches_publication_without_repair(self):
        for ok in (False, True):
            with self.subTest(ok=ok):
                first = Result(ok=ok, error="" if ok else "fixture call failed")
                flow, calls = self.review_build(first, Result(ok=True, structured={"summary": "Fine.", "problems": []}))
                self.assertEqual([call.kwargs["trace"][1] for call in calls], ["review", "review"])
                self.assertEqual(calls[0].args, calls[1].args)
                self.assertEqual(flow.tries["review"], 1)
                self.assertEqual(flow.ctx.run.budget.repair_calls, 0)
                self.assertTrue(flow.ctx.review.approved())
                self.assertEqual(flow.ctx.draft_why, "")

    def test_review_double_failure_keeps_earlier_blockers_and_the_draft(self):
        changes = Result(ok=True, structured={"summary": "Needs a fix.", "problems": [self.BLOCKER]})
        failed = Result(ok=False, error="fixture call failed")
        flow, calls = self.review_build(changes, Result(ok=True), failed, failed)
        self.assertEqual([call.kwargs["trace"][1] for call in calls], ["review", "review_fix", "review2", "review2"])
        self.assertEqual(flow.tries["review"], 2)
        self.assertEqual(flow.ctx.run.budget.repair_calls, 1)
        self.assertEqual(flow.ctx.review.verdict, "request_changes")
        self.assertTrue(flow.ctx.review.failed)
        self.assertEqual(flow.ctx.review.blocking()[0]["problem"], self.BLOCKER["problem"])
        self.assertIn("1 earlier blocker", flow.ctx.draft_why)

    def test_request_changes_starts_repair_without_retrying_review(self):
        changes = Result(ok=True, structured={"summary": "Needs a fix.", "problems": [self.BLOCKER]})
        approved = Result(ok=True, structured={
            "summary": "Fixed.", "problems": [], "earlier": [{"id": "B1", "status": "fixed", "why": "Rechecked."}],
        })
        flow, calls = self.review_build(changes, Result(ok=True), approved)
        self.assertEqual([call.kwargs["trace"][1] for call in calls], ["review", "review_fix", "review2"])
        self.assertIn(self.BLOCKER["problem"], calls[1].args[0])
        self.assertEqual(flow.ctx.run.budget.repair_calls, 1)
        self.assertTrue(flow.ctx.review.approved())


class OrchestrationLineageTests(Isolated):
    def setUp(self):
        super().setUp()
        self.patches.enter_context(patch.object(pipeline, "workspace_note", return_value="Fixture workspace"))

    def controller(self, previous="", feedback=()):
        run = self.made_run(previous)
        run.path = "orchestrate"
        run.feedback = list(feedback)
        return orchestration.Controller(flows.start(self.cfg, run, {}, continues=previous))

    def admit(self):
        controller = self.controller()
        outcome = factory.Outcome("pass", data={
            "layer": str(self.repo.layer_dir), "checkouts": [], "ticket": asdict(self.local.get(self.ticket)),
            "branch": "fixture", "add_dirs": [], "attachments": "", "expertise": "", "triage": "",
        })

        def prepare_step(*args, **kwargs):
            controller.job.take("prepare", outcome, stored=False)
            return outcome

        with patch.object(controller, "atomic", side_effect=prepare_step):
            self.assertIsNone(controller.prepare())
        return controller

    def accept(self, controller, op, state, args=None, **result):
        lease = controller.lease
        version = ledger.snapshot(lease)["version"]
        turn = ledger.begin(lease, {}, 10)
        action, = ledger.accept(lease, turn, version, {"calls": [{"op": op, "args": args or {}}]}, 10)
        if state != "accepted":
            ledger.transition(lease, action["id"], state, result)
        return action["id"]

    def audits(self, controller):
        return [line for line in run_lines.read(controller.run.id) if line["event"] == "lineage"]

    def test_operations_refresh_reaches_the_next_decision_and_audits_once(self):
        wait = OPERATIONS["run.wait"]
        old_wait = replace(wait, description="Old wait", constraints={"timeout_seconds": {"minimum": 1}})
        with patch.dict(OPERATIONS, {"run.wait": old_wait}):
            prior = self.admit()
        record.move(prior.run.id, "failed", ("running",))
        resumed = self.controller(prior.run.id)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertIsNone(resumed.prepare())
        catalog_sha = prior.data["catalog_sha"]
        new = ledger.digest(describe_operations())
        audit, = self.audits(resumed)
        self.assertEqual(audit["old"], {"operations_sha": prior.data["operations_sha"]})
        self.assertEqual(audit["new"], {"operations_sha": new})
        self.assertNotEqual(audit["old"], audit["new"])
        self.assertEqual(audit["engine_commit"], "new-engine")
        data = ledger.snapshot(resumed.lease)["data"]
        self.assertEqual(data["operations_sha"], new)
        self.assertEqual(data["catalog_sha"], catalog_sha)

        def decide(job, turn, prompt, *args):
            self.assertIn(wait.description, prompt)
            self.assertIn(f'"catalog_sha": "{catalog_sha}"', prompt)
            response = {"version": PROTOCOL, "decision_id": turn, "rationale": "",
                        "state_version": ledger.snapshot(resumed.lease)["version"],
                        "calls": [{"op": "run.wait", "args": {"timeout_seconds": 0}}]}
            worker.receive.return_value = [{"ok": True, "structured": response}]
            return {}

        with (patch.object(delegation, "request", side_effect=decide),
              patch.object(delegation, "Worker") as launch):
            worker = launch.return_value
            worker.pid, worker.identity = 123, 456
            action, = resumed.decide()
        self.assertEqual(action["request"]["timeout_seconds"], 0)
        resumed.dispatch(action)
        record.move(resumed.run.id, "interrupted", ("running",))
        repeated = self.controller(resumed.run.id)
        self.assertIsNone(repeated.prepare())
        self.assertEqual(repeated.data["operations_sha"], new)
        self.assertEqual(self.audits(resumed), [audit])
        self.assertEqual(self.audits(repeated), [])

    def test_successive_refreshes_in_one_run_each_write_an_audit_line(self):
        wait = OPERATIONS["run.wait"]
        changed_wait = replace(wait, description="Deployed wait")
        prior = self.admit()
        record.move(prior.run.id, "failed", ("running",))
        resumed = self.controller(prior.run.id)
        original = {"operations_sha": prior.data["operations_sha"]}
        with patch.object(codebase, "STAMP", "deploy"):
            for deployed in (changed_wait, wait, changed_wait):
                with patch.dict(OPERATIONS, {"run.wait": deployed}):
                    self.assertIsNone(resumed.prepare())

        changed = {"operations_sha": ledger.digest({**describe_operations(), "run.wait": changed_wait.describe()})}
        self.assertNotEqual(changed, original)
        audits = self.audits(resumed)
        self.assertEqual([(line["old"], line["new"]) for line in audits],
                         [(original, changed), (changed, original), (original, changed)])
        self.assertEqual([line["engine_commit"] for line in audits], ["deploy"] * 3)
        self.assertEqual(ledger.snapshot(resumed.lease)["data"]["operations_sha"], changed["operations_sha"])

    def test_main_format_resumes_an_accepted_action_without_a_lineage_event(self):
        prior = self.admit()
        catalog_sha = ledger.digest(prior.catalog.entries)
        prior.data["catalog_sha"] = catalog_sha
        del prior.data["operations_sha"]
        ledger.save(prior.lease, prior.data)
        action_id = self.accept(prior, "run.finish", "accepted", {"reason": "noop", "evidence_refs": []})
        record.move(prior.run.id, "interrupted", ("running",))

        resumed = self.controller(prior.run.id)
        with patch.object(resumed, "decide", side_effect=AssertionError("the accepted action should finish the run")):
            self.assertEqual(resumed.drive()[0], "needs-info")
        action, = ledger.actions(resumed.lease)
        self.assertEqual((action["id"], action["state"]), (action_id, "succeeded"))
        data = ledger.snapshot(resumed.lease)["data"]
        self.assertEqual(data["catalog_sha"], catalog_sha)
        self.assertEqual(data["operations_sha"], ledger.digest(describe_operations()))
        self.assertEqual(self.audits(resumed), [])

    def test_catalog_audit_survives_interruption_after_append(self):
        prior = self.admit()
        old_entries = {key: entry for key, entry in prior.catalog.entries.items() if key != "prompt:delegate"}
        prior.data["catalog_sha"] = ledger.digest(old_entries)
        ledger.save(prior.lease, prior.data)
        record.move(prior.run.id, "interrupted", ("running",))
        resumed = self.controller(prior.run.id)
        write = run_lines.write

        def interrupt_after_write(run_id, event, **fields):
            line = write(run_id, event, **fields)
            if event == "lineage":
                raise OSError("interrupted after append")
            return line

        with patch.object(run_lines, "write", side_effect=interrupt_after_write):
            with self.assertRaisesRegex(OSError, "interrupted after append"):
                resumed.prepare()
        audit, = self.audits(resumed)
        pending = ledger.snapshot(resumed.lease)["data"]["lineage_event"]
        self.assertEqual(pending["run"], resumed.run.id)
        self.assertEqual(pending["event_id"], audit["event_id"])
        self.assertEqual(pending["old"], audit["old"])
        self.assertEqual(pending["new"], audit["new"])
        record.move(resumed.run.id, "interrupted", ("running",))
        repeated = self.controller(resumed.run.id)
        self.assertIsNone(repeated.prepare())
        self.assertEqual(self.audits(resumed), [audit])
        self.assertEqual(self.audits(repeated), [])
        self.assertNotIn("lineage_event", ledger.snapshot(repeated.lease)["data"])

    def test_lineage_row_missing_its_newline_recovers_in_either_append_order(self):
        fields = {"event_id": "refresh", "old": {"catalog_sha": "old"}, "new": {"catalog_sha": "new"},
                  "engine_commit": "deploy"}
        for error_first in (False, True):
            with self.subTest(error_first=error_first):
                run_id = f"torn-lineage-{error_first}"
                route = run_lines.write(run_id, "route")
                audit = {"ts": route["ts"], "run": run_id, "seq": route["seq"] + 1, "event": "lineage", **fields}
                with run_lines.path(run_id).open("ab") as log:
                    log.write(json.dumps(audit).encode())
                if error_first:
                    error = run_lines.write(run_id, "error", reason="interrupted append")
                self.assertEqual(run_lines.write(run_id, "lineage", **fields), audit)
                if not error_first:
                    self.assertEqual(run_lines.read(run_id), [route, audit])
                    error = run_lines.write(run_id, "error", reason="interrupted append")
                self.assertEqual(error["seq"], audit["seq"] + 1)
                self.assertEqual(run_lines.read(run_id), [route, audit, error])
                self.assertEqual(list(run_lines.follow(run_id, start=audit["seq"], ended=lambda: True)), [error])

    def test_explicit_rerun_uses_the_refreshed_catalog(self):
        checks = replace(OPERATIONS["checks.run"], constraints={"scope": {"enum": ["full"]}})
        for scope in ("quick", "full"):
            with self.subTest(scope=scope):
                prior = self.admit()
                action_id = self.accept(prior, "checks.run", "succeeded", {"scope": scope})
                record.move(prior.run.id, "failed", ("running",))
                resumed = self.controller(prior.run.id)
                resumed.run.action, resumed.run.action_only = action_id, True
                with (patch.dict(OPERATIONS, {"checks.run": checks}),
                      patch.object(resumed, "dispatch", wraps=resumed.dispatch) as dispatch,
                      patch.object(ledger, "accept", wraps=ledger.accept) as accept,
                      patch.object(resumed, "fingerprint", return_value="fixture-tree"),
                      patch.object(resumed, "atomic", return_value=factory.Outcome("pass")) as atomic):
                    if scope == "quick":
                        with self.assertRaisesRegex(factory.Refused, rf"{action_id}.*scope must be one of: full"):
                            resumed.drive()
                        accept.assert_not_called()
                        dispatch.assert_not_called()
                        atomic.assert_not_called()
                    else:
                        self.assertEqual(resumed.drive()[0], "step")
                        accept.assert_called_once()
                        dispatch.assert_called_once()
                        atomic.assert_called_once_with("checks", tree=str(resumed.job.ws), quick=False)
                self.assertEqual(ledger.pending(resumed.lease), [])
                self.assertEqual(len(self.audits(resumed)), 1)
                original, *reruns = ledger.actions(resumed.lease)
                self.assertEqual(original["id"], action_id)
                self.assertEqual(original["request"], {"scope": scope})
                self.assertEqual([(a["state"], a["request"]) for a in reruns],
                                 [("succeeded", {"scope": "full"})] if scope == "full" else [])

    def test_explicit_rerun_accepts_a_near_limit_worker_decision_after_normalization(self):
        prior = self.admit()
        route = "model:build:0"
        effort = prior.catalog.entries[route]["efforts"][0]
        sizes = iter((MAX_BYTES + 1, MAX_BYTES - 1))

        def decide(job, turn, prompt, *settings):
            request = {"goal": "", "route_id": route, "effort": effort}
            response = {"version": PROTOCOL, "decision_id": turn, "rationale": "",
                        "state_version": ledger.snapshot(prior.lease)["version"],
                        "calls": [{"op": "worker.start", "args": request}]}
            size = next(sizes)
            escaped, padding = divmod(size - len(json.dumps(response)), len(json.dumps("\u00e9")) - 2)
            request["goal"] = "\u00e9" * escaped + "x" * padding
            self.assertEqual(len(json.dumps(response).encode()), size)
            worker.receive.return_value = [{"ok": True, "structured": response}]
            return {}

        with (patch.object(delegation, "request", side_effect=decide),
              patch.object(delegation, "Worker") as launch):
            worker = launch.return_value
            worker.pid, worker.identity = 123, 456
            self.assertEqual(prior.decide(), [])
            self.assertEqual(ledger.actions(prior.lease), [])
            action, = prior.decide()
        self.assertEqual(action["request"]["prompt_id"], "delegate")
        ledger.transition(prior.lease, action["id"], "succeeded", {})
        record.move(prior.run.id, "failed", ("running",))

        resumed = self.controller(prior.run.id)
        resumed.run.action, resumed.run.action_only = action["id"], True
        with (patch.object(resumed, "worker_start", return_value={}) as start,
              patch.object(ledger, "accept", wraps=ledger.accept) as accept):
            self.assertEqual(resumed.drive()[0], "step")
        accept.assert_called_once()
        self.assertGreater(len(json.dumps(accept.call_args.args[3]).encode()), MAX_BYTES)
        original, rerun = ledger.actions(resumed.lease)
        self.assertEqual((rerun["state"], rerun["request"]), ("succeeded", original["request"]))
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0]["id"], rerun["id"])

    def test_refresh_refuses_with_live_workers_or_unfinished_actions(self):
        wait = OPERATIONS["run.wait"]
        cases = (
            ("pending", 123, "prior controller inference is still live"),
            ("running", 123, "worker .* is still live"),
            ("running", 321, "unfinished \\(running\\)"),
            ("accepted", None, "unfinished \\(accepted\\)"),
        )
        for state, pid, reason in cases:
            with self.subTest(state=state, pid=pid):
                prior = self.admit()
                if state == "pending":
                    turn = ledger.begin(prior.lease, {}, 10)
                    ledger.turn_started(prior.lease, turn, pid, 456)
                else:
                    self.accept(prior, "worker.start", state, pid=pid, identity=456)
                record.move(prior.run.id, "interrupted", ("running",))
                resumed = self.controller(prior.run.id)
                with (patch.dict(OPERATIONS, {"run.wait": replace(wait, description="Updated wait")}),
                      patch.object(delegation, "live", side_effect=lambda pid, identity: (pid, identity) == (123, 456))):
                    with self.assertRaisesRegex(factory.Refused, f"operations_sha changed .*{reason}"):
                        resumed.prepare()
                self.assertEqual(ledger.snapshot(resumed.lease)["data"]["operations_sha"], prior.data["operations_sha"])
                stored = ledger.pending(resumed.lease) if state == "pending" else ledger.actions(resumed.lease)
                self.assertEqual(stored[0]["state"], state)
                self.assertEqual(self.audits(resumed), [])

    def test_refresh_refuses_and_names_authority_changes(self):
        for key in ("policy_sha", "controller_settings", "run_settings"):
            with self.subTest(key=key):
                prior = self.admit()
                prior.data[key] = "old-authority"
                ledger.save(prior.lease, prior.data)
                record.move(prior.run.id, "failed", ("running",))
                resumed = self.controller(prior.run.id)
                with self.assertRaisesRegex(factory.Refused, f"{key} changed"):
                    resumed.prepare()
                self.assertEqual(ledger.snapshot(resumed.lease)["data"][key], "old-authority")
                self.assertEqual(self.audits(resumed), [])

    def test_prompt_refresh_on_owner_answer_and_pr_feedback(self):
        source = orchestration.prompt_loader.template

        def deployed_prompt(name):
            template = source(name)
            return Template(template.template + "\nFollow the deployed prompt.") if name == "orchestrate" else template

        for continuation in ("owner answer", "PR feedback"):
            with self.subTest(continuation=continuation):
                prior = self.admit()
                feedback = []
                if continuation == "owner answer":
                    question = {"header": "Scope", "question": "Which scope?", "kind": "text"}
                    ask_store.ask_add("ask-lineage", prior.run.id, self.repo.name, self.ticket, "orchestrate",
                                      "orchestrate", "", "", [question], {})
                    self.accept(prior, "owner.ask", "succeeded", ask_ids=["ask-lineage"], awaiting_owner=True)
                    record.move(prior.run.id, "needs-info", ("running",))
                    ask_store.ask_close("ask-lineage", "answered", [{"text": "Keep the existing scope"}])
                    context = "Keep the existing scope"
                else:
                    pr = "https://github.com/example/fixture/pull/1"
                    context = "Revise the status message"
                    feedback = [{"pr_url": pr, "kind": "comment", "gh_id": 1, "body": context}]
                    record.move(prior.run.id, "pr", ("running",), pr_url=pr)
                resumed = self.controller(prior.run.id, feedback)
                with (patch.object(orchestration.prompt_loader, "template", side_effect=deployed_prompt),
                      patch.object(gh, "pr_view", return_value={"state": "OPEN"}),
                      patch.object(gh, "pr_body", return_value="")):
                    self.assertIsNone(resumed.prepare())
                audit, = self.audits(resumed)
                self.assertEqual(audit["old"], {"controller_prompt": prior.data["controller_prompt"]})
                self.assertEqual(audit["new"], {"controller_prompt": resumed.data["controller_prompt"]})
                self.assertNotEqual(audit["old"], audit["new"])
                self.assertIn(context, resumed.job.goal)


class OrchestrationWaitTests(Isolated):
    def setUp(self):
        super().setUp()
        self.now = 0.0
        self.patches.enter_context(patch.object(orchestration, "time", SimpleNamespace(
            monotonic=lambda: self.now, sleep=self.advance)))
        controller = self.controller = orchestration.Controller.__new__(orchestration.Controller)
        controller.run = SimpleNamespace(id="wait-run", budget=None, launch_gate=None, launch_done=None,
                                         cost=0, paid=0)
        controller.lease = ledger.acquire(controller.run.id, "", {})
        controller.workers, controller.progress, controller.reported = {}, {}, {}
        controller.last_progress = float("-inf")
        controller.policy = config.Orchestration()
        controller.catalog = Catalog.__new__(Catalog)
        controller.catalog.context = {}
        controller.data = {"acceptance": "fixture"}
        controller.fingerprint = controller.policy_sha = lambda: "fixture"
        controller.persist = lambda: None

    def advance(self, seconds):
        self.now += seconds

    def accept(self, calls, maximum, *, validate=True):
        lease = self.controller.lease
        version = ledger.snapshot(lease)["version"]
        turn = ledger.begin(lease, {}, maximum)
        ledger.launched(lease, maximum)
        response = {"version": PROTOCOL, "decision_id": turn, "state_version": version,
                    "rationale": "", "calls": calls}
        if validate:
            response = self.controller.catalog.validate(
                response, turn, version, {row["id"]: row for row in ledger.actions(lease)})
        return ledger.accept(lease, turn, version, response, self.controller.policy.max_actions)

    def start_worker(self, deadline, *, finish=None, ok=True):
        controller = self.controller
        action, = self.accept([{"op": "worker.start", "args": {}}], 2, validate=False)
        worker = delegation.Worker.__new__(delegation.Worker)
        worker.action, worker.deadline, worker.writing = action["id"], deadline, False
        worker.run_id, worker.step = controller.run.id, f"worker-{worker.action}"
        worker.effective = {"prompt_id": "investigate"}
        worker.progress, worker.sources, worker.report = [], {}, (0, {})
        worker.proc = SimpleNamespace(poll=lambda: None)
        worker.close = lambda: None
        worker.receive = lambda: ([{"event": "result", "ok": ok}] if finish is not None and self.now >= finish else [])
        controller.workers[worker.action] = worker
        ledger.transition(controller.lease, worker.action, "running", {})
        return worker

    def wait(self, args, maximum=8, extra=()):
        actions = self.accept([{"op": "run.wait", "args": args}, *extra], maximum)
        for action in actions:
            self.controller.dispatch(action)
        return next(row for row in ledger.actions(self.controller.lease) if row["id"] == actions[0]["id"])

    def test_explicit_60_second_wait_keeps_cap_and_long_wait_reaches_worker_result(self):
        worker = self.start_worker(2700, finish=1800)
        capped = self.wait({"timeout_seconds": 60}, maximum=2)
        self.assertEqual((self.now, capped["result"]["active"]), (60, [worker.action]))
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 1)
        completed = self.wait({"timeout_seconds": 0}, maximum=2)
        self.assertLess(self.now - 1800, 0.1)
        self.assertEqual((completed["id"], completed["result"]["active"]), ("d0003-0", []))
        state = ledger.view(self.controller.run.id)
        self.assertEqual((state["decisions"], state["launches"]), (2, 3))
        self.assertEqual(state["actions"][0]["state"], "succeeded")

    def test_idle_waits_return_immediately_and_exhaust_decisions(self):
        for _ in range(2):
            self.assertEqual(self.wait({}, maximum=2)["result"]["active"], [])
        self.assertEqual(self.now, 0)
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_mixed_decision_keeps_charge_for_unchanged_wait(self):
        worker = self.start_worker(2700)
        steer = {"op": "worker.steer", "args": {"worker_id": worker.action, "message": "Check the log."}}
        settled = self.wait({}, maximum=2, extra=(steer,))
        self.assertEqual((self.now, settled["result"]["active"]), (60, [worker.action]))
        self.assertEqual(self.controller.data["steering"], {worker.action: ["Check the log."]})
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_wait_ends_at_worker_deadline_and_an_expired_wait_costs_a_decision(self):
        self.start_worker(10)
        self.now = 4
        self.wait({"timeout_seconds": 1800}, maximum=2)
        self.assertEqual(self.now, 10)
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 1)
        self.wait({"timeout_seconds": 0}, maximum=2)
        self.assertEqual(self.now, 10)
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_run_deadline_interrupts_long_wait(self):
        self.start_worker(2700)
        self.controller.run.budget = SimpleNamespace(run_deadline=lambda: 2, reason=lambda: "run deadline")
        with self.assertRaisesRegex(runlimits.Parked, "run deadline"):
            self.wait({"timeout_seconds": 0})
        self.assertEqual(self.now, 2)

    def test_new_progress_wakes_wait_and_worker_failure_wakes_next_wait(self):
        worker = self.start_worker(300, finish=5, ok=False)
        source = paths.record_dir(worker.run_id) / (worker.step + record.RECORD_SUFFIX)
        source.parent.mkdir(parents=True, exist_ok=True)
        with source.open("ab") as output:
            for event in ({"type": "replay"}, {"type": "heartbeat"},
                          {"type": "item.completed", "item": {"type": "command_execution"}},
                          {"type": "item.completed", "item": {"type": "agent_message", "text": "Checks are running."}}):
                record.append(output.fileno(), json.dumps(event))
        update = self.wait({"timeout_seconds": 0})
        self.assertIn("Checks are running.", update["result"]["progress"][worker.action]["excerpt"])
        self.assertEqual(self.now, 0)
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 2)
        failed = self.wait({"timeout_seconds": 60})
        self.assertEqual(failed["result"]["active"], [])
        self.assertLess(self.now - 5, 0.1)
        self.assertEqual(ledger.view(self.controller.run.id)["actions"][0]["state"], "failed")


class BudgetTests(Isolated):
    def test_measured_usage_rejects_corruption_and_zero_overrides_wall_time(self):
        run = self.made_run()
        record.note(run.id, started=util.iso(util.now() - timedelta(minutes=90)))
        record.settle(run.id, "interrupted")
        for field, read in (("active_seconds", runlimits.active_seconds),
                            ("verify_seconds", runlimits.verification_seconds),
                            ("verify_open_seconds", runlimits.verification_seconds),
                            ("repair_calls", lambda rid, row: runlimits.repair_count(rid))):
            for bad in (-1, "unknown"):
                with self.subTest(field=field, bad=bad), self.assertRaises(sqlite3.IntegrityError), \
                        foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (bad, run.id))
            for bad in ((None, float("nan")) if field == "repair_calls" else (None, float("nan"), float("inf"))):
                with foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (bad, run.id))
                with self.assertRaisesRegex(ValueError, run.id):
                    read(run.id, run_store.run_get(run.id))
            with foundation.tx() as conn:
                conn.execute(f"UPDATE runs SET {field}=0 WHERE id=?", (run.id,))
            self.assertEqual(read(run.id, run_store.run_get(run.id)), 0)

    def test_measured_recovery_and_nested_open_idle_waits(self):
        clock = [100.0]
        with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            run = self.made_run()
            clock[0] += 10
            for cause in ("limit", "slot"):
                with run.budget.idling(cause):
                    clock[0] += 20
                    with run.budget.idling("slot"):
                        clock[0] += 30
                        run.budget.checkpoint()
                        self.assertEqual(run.budget.run_seconds(), 10)
                    clock[0] += 10
                self.assertEqual(run.budget.run_seconds(), 10)
            self.assertEqual(run.budget.idled, 120)
            with run.budget.idling("ci"):
                clock[0] += 5
            self.assertEqual(run.budget.run_seconds(), 15)
            run.budget.persist()
            resumed = runlimits.Budget(run, run.budget.policy, "")
            self.assertEqual(resumed.run_seconds(), 15)
            clock[0] += 5
            self.assertEqual(resumed.run_seconds(), 20)

    def test_checkpoint_tracks_open_verification_without_charging_open_idle_time(self):
        clock = [100.0]
        with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            run = self.made_run()
            clock[0] += 10
            with patch.object(runlimits.signal, "setitimer"), run.budget.verifying(), run.budget.idling("limit"):
                clock[0] += 20
                run.budget.checkpoint()
                first = run_store.run_get(run.id)
                clock[0] += 30
                run.budget.checkpoint()
                second = run_store.run_get(run.id)
            self.assertEqual(first["active_seconds"], second["active_seconds"])
            self.assertEqual(second["verify_open_seconds"] - first["verify_open_seconds"], 30)
            saved = run_store.run_get(run.id)
            self.assertEqual(saved["verify_seconds"], 50)
            self.assertEqual(saved["verify_open_seconds"], 0)
            self.assertEqual(saved["active_seconds"], 10)

    def test_repair_budget_is_charged_before_launch_and_survives_continuation(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        invoked = []
        with patch.object(pipeline.runner, "run", side_effect=lambda *a, **k: invoked.append(True)), \
                self.assertRaises(runlimits.Parked):
            second.agent("repair", "repair again", self.repo.path, role="review_repair")
        self.assertEqual(invoked, [])
        self.assertEqual(runlimits.repair_count(second.id), 0)

    def test_repair_usage_is_durable_even_when_audit_event_cannot_be_written(self):
        first = self.made_run()
        with patch.object(run_lines, "write", return_value={}):
            first.budget.charge("review_fix1", "review_repair")
            first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
            second.budget.charge("repair", "review_repair")

    def test_cumulative_time_survives_continuation(self):
        first = self.made_run()
        with foundation.tx() as conn:
            conn.execute("UPDATE runs SET active_seconds=? WHERE id=?", (61 * 60, first.id))
        record.settle(first.id, "interrupted")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "cumulative 60"):
            second.budget.charge("plan", "plan")

    def test_interrupted_verification_phase_is_counted_on_restart(self):
        first = self.made_run()
        with foundation.tx() as conn:
            conn.execute("UPDATE runs SET verify_open_seconds=? WHERE id=?", (21 * 60, first.id))
        record.settle(first.id, "interrupted")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "verification"), second.budget.verifying():
            self.fail("unfinished verification phases must count")

    def test_unchanged_parked_input_refuses_retry_changed_spec_requires_explicit_retry(self):
        run = self.made_run()
        with patch.object(runlimits, "heads", return_value={"": "old"}):
            run.budget.park("repair limit")
            record.settle(run.id, "failed", branch="fixture")
            admission = runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True)
            self.assertIn("parked", admission.reason)
            self.assertFalse(admission.reset_time)
            self.assertFalse(admission.reset_repairs)
            with foundation.db() as connection:
                set_task_body(self.ticket, "A narrower specification", connection=connection)
            self.assertTrue(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, False).reason)
            self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True),
                             runlimits.Admission(reset_time=True, reset_repairs=True))

    def test_corrupt_budget_is_not_silently_reset(self):
        run = self.made_run()
        run.budget.charge("review_fix1", "review_repair")
        record.settle(run.id, "failed")
        for invalid in ("broken json", json.dumps({"version": 1, "parents": [], "left_out": "x"})):
            with self.subTest(invalid=invalid):
                (run.dir / "budget.json").write_text(invalid)
                with self.assertRaises(ValueError):
                    self.made_run(run.id)
                self.assertEqual(runlimits.repair_count(run.id), 1)

    def test_individual_step_reruns_cannot_reset_the_ticket_budget(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        second.budget.charge("review_fix2", "review_repair")
        record.note(second.id, only_step="implement")
        record.settle(second.id, "step")
        history = run_store.ticket_runs(self.repo.name, self.ticket)
        script = pipeline.Script("feature", lambda flow: None, continues=first.id)
        parent = pipeline.parent_run(script, run_store.run_get(first.id), history)
        self.assertEqual(parent, second.id)
        next_run = self.made_run(parent)
        self.assertEqual(set(next_run.budget.parents), {first.id, second.id})
        with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
            next_run.budget.charge("repair", "review_repair")

    def test_forced_run_leaves_out_repairs_spent_under_another_specification(self):
        for forced, edited, recorded in ((True, True, True), (True, False, True),
                                         (False, True, True), (True, True, False)):
            with self.subTest(forced=forced, edited=edited, recorded=recorded):
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Original", connection=connection)
                old = self.made_run()
                old.budget.charge("review_fix1", "review_repair")
                old.budget.charge("verify_fix1", "build")
                if not recorded:
                    old.budget.specification = None
                    old.budget.persist()
                record.settle(old.id, "pr")
                if edited:
                    with foundation.db() as connection:
                        set_task_body(self.ticket, "Edited", connection=connection)
                next_run = self.made_run(old.id, forced=forced)
                if forced and edited and recorded:
                    self.assertEqual((next_run.budget.parents, next_run.budget.left_out), ([], [old.id]))
                    next_run.budget.charge("review_fix", "review_repair")
                    self.assertEqual(runlimits.repair_count(next_run.id), 1)
                    record.settle(next_run.id, "failed")
                    retry = self.made_run(next_run.id, forced=True)
                    self.assertEqual(retry.budget.parents, [next_run.id])
                    self.assertNotIn(old.id, runlimits.family(retry.id))
                else:
                    self.assertEqual((next_run.budget.parents, next_run.budget.left_out), ([old.id], []))
                    with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
                        next_run.budget.charge("repair", "review_repair")

    def test_forced_run_leaves_out_run_and_verification_minutes_of_another_specification(self):
        for field, seconds in (("active_seconds", 61 * 60), ("verify_seconds", 21 * 60)):
            with self.subTest(field=field):
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Original", connection=connection)
                old = self.made_run()
                with foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (seconds, old.id))
                record.settle(old.id, "pr")
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Edited", connection=connection)
                fresh = self.made_run(old.id, forced=True).budget
                self.assertEqual((fresh.parents, fresh.left_out), ([], [old.id]))
                self.assertEqual((fresh.prior_seconds, fresh.verify_seconds), (0, 0))
                fresh.charge("plan", "plan")
                kept = self.made_run(old.id).budget
                with self.assertRaises(runlimits.Parked):
                    if field == "active_seconds":
                        kept.charge("plan", "plan")
                    else:
                        with kept.verifying():
                            self.fail("verification budget must not reset without an explicit retry")


class PollFixture(Isolated):
    def run_polled_ticket(self, repo, tid, work, script=None):
        _, env = launch_lease.child_environment(())
        with patch.dict(os.environ, env), patch.object(launch_lease, "registered"), \
                patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                patch.object(pipeline, "_run", side_effect=work):
            return pipeline.run_issue(repo, tid, script=script)

    def poll_with(self, cfg, spawn, *, dry_run=False):
        def launch_pending(repo, tid, launch):
            with launch_lease.acquiring(repo, tid):
                return launch()

        with patch.object(config, "load", return_value=cfg), patch.object(poll, "runnable", return_value=True), \
                patch.object(tasks, "spawn_pending", side_effect=launch_pending), \
                patch.object(poll, "spawn_run", side_effect=spawn), \
                patch.object(poll, "network_up_or_skip", return_value=True), \
                patch.object(poll, "settle_abandoned_runs"), patch.object(outcomes, "sync_outcomes"), \
                patch.object(outcomes, "sync_moves"), patch.object(outcomes, "sync_landings"), \
                patch.object(poll, "retry_scratch"), \
                patch.object(poll, "cleanup_worktrees"), patch.object(poll, "mark_loop"), \
                patch.object(pipeline, "SKIPPED_FILE", self.root / "skipped.json"):
            return poll.poll(dry_run=dry_run)


class OwnerAnswerBudgetFixture(PollFixture):
    def spent_question(self, *, structured=False):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text(
            '[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        first = self.made_run()
        first.budget.charge("repair", "review_repair")
        record.settle(first.id, "failed")
        asked = self.made_run(first.id)
        asked.budget = runlimits.Budget(asked, runlimits.Policy(max_repair_cycles=3), first.id)
        for _ in range(2):
            asked.budget.charge("repair", "review_repair")
        question = "Where is the CI log?"
        asks = [{"ask_id": "ask-fixture", "step": "review", "call": "review", "questions": [
            {"kind": "text", "header": "CI log", "question": question}]}] if structured else None
        if structured:
            run_lines.write(asked.id, "execution", path="feature")
        with patch.object(pipeline, "release_workspace"):
            status, summary, _ = pipeline._ask(asked, self.repo.path, [], question, question, asks,
                                               {} if structured else None)
        self.assertEqual(status, "needs-info")
        record.settle(asked.id, status, summary=summary)
        with foundation.tx():
            run_store.checkpoint(first.id, 60, 20, 0, 1, util.iso())
            run_store.checkpoint(asked.id, 120, 40, 0, 2, util.iso())
        return first, asked

    def assert_answer_budget(self, run, first, asked, preview=None):
        parents = [asked.id, first.id]
        seconds = (180, 60)
        if preview:
            parents.insert(0, preview)
            seconds = (210, 70)
        self.assertEqual((run.budget.prior_seconds, run.budget.verify_seconds), seconds)
        self.assertEqual(run.budget.prior_repairs, 0)
        run.budget.charge("repair", "review_repair")
        self.assertEqual(runlimits.repair_count(run.id), 1)
        audit = next(line for line in (run.dir / "run.log").read_text().splitlines() if "budget chain:" in line)
        _, reason, omitted = audit.partition("because of an owner answer: ")
        self.assertTrue(reason, audit)
        self.assertCountEqual(omitted.split(", "), parents)
        self.assertEqual(runlimits.family(run.id, repairs=True), [run.id])
        self.assertEqual(set(runlimits.family(run.id)), {run.id, *parents})

    def consume_tracker_answers(self, run, expected):
        self.assertEqual({a["id"]: a["answers"][0]["text"] for a in run.answers_due}, expected)
        with patch.object(factory, "inside", return_value=factory.Calls(run.id, "review")), \
                patch.object(pipeline.AgentRun, "agent", return_value=Result(ok=True)) as agent:
            run.agent("review", "Review the change.", self.repo.path)
        for ask_id, text in expected.items():
            self.assertIn(text, agent.call_args.args[1])
            self.assertEqual(ask_store.ask_get(ask_id)["taken_by"], run.id)
        self.assertEqual(run.answers_due, [])

    def run_polled_answer(self, repo, tid, asked, work):
        self.assertEqual(pipeline.run_command(repo, tid)[-2:], ["--continue", asked.id])
        script = pipeline.Script("feature", lambda flow: work(flow.ctx.run), continues=asked.id)
        self.assertEqual(self.run_polled_ticket(repo, tid, lambda *_: self.fail("started an ordinary run"), script), 0)

    def expired_question(self):
        with patch.object(util, "now", return_value=datetime(2026, 1, 1, tzinfo=UTC)):
            first, asked = self.spent_question(structured=True)
        with patch.object(util, "now", return_value=datetime(2026, 1, 2, tzinfo=UTC)):
            with principals.acting(principals.seed_owner("fixture@example.test")):
                tasks_store.task_comment(self.ticket, principals.current(), "An older comment.")
        with patch.object(util, "now", return_value=datetime(2026, 1, 15, tzinfo=UTC)), \
                patch.object(config, "load", return_value=self.cfg), \
                patch.object(tasks, "kept_folder_removed", return_value=True):
            self.assertEqual(len(tasks.expire_asks(self.cfg)), 1)
        self.assertEqual(ask_store.ask_get("ask-fixture")["status"], "expired")
        self.assertIn("Reply here and remove", self.local.get(self.ticket).comments)
        return first, asked

    def check_tracker_reply_budget(self, *, structured, expired=False, dry_run=False):
        if expired:
            first, asked = self.expired_question()
        else:
            first, asked = self.spent_question(structured=structured)
        preview = None
        if dry_run:
            with patch.object(config, "load", return_value=self.cfg), \
                    patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                    patch.object(pipeline, "_run", return_value=("dry-run", "preview finished", "")):
                self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, dry_run=True), 0)
            preview = run_store.newest_whole_run(self.repo.name, self.ticket)["id"]
            self.assertNotEqual(preview, asked.id)
            with foundation.tx():
                run_store.checkpoint(preview, 30, 10, 0, 0, util.iso())

        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("launched without a reply")), 0)
        replied_at = datetime(2026, 1, 16, tzinfo=UTC) if expired else util.now()
        with patch.object(util, "now", return_value=replied_at), \
                principals.acting(principals.seed_owner("fixture@example.test")):
            tasks_store.task_comment(self.ticket, principals.current(), "See the attached log.")
        if expired:
            self.local.label(self.ticket, remove=["needs-info"])
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("dry poll launched"), dry_run=True), 0)
        self.assertFalse(pipeline.answer_text(asked.id))
        if structured:
            self.assertEqual(ask_store.ask_get("ask-fixture")["status"], "expired" if expired else "open")

        def work(run):
            self.assert_answer_budget(run, first, asked, preview)
            if structured and not expired:
                self.consume_tracker_answers(run, {"ask-fixture": "See the attached log."})
            else:
                self.assertEqual(run.answering["id"], asked.id)
                self.assertEqual(pipeline.answer_text(asked.id), "See the attached log.")
            return "noop", "fixture finished", ""

        started = []

        def spawn(repo, tid):
            started.append(tid)
            if structured and not expired:
                self.run_polled_answer(repo, tid, asked, work)
                return
            if expired:
                self.assertNotIn("--continue", pipeline.run_command(repo, tid))
            self.assertEqual(self.run_polled_ticket(repo, tid, lambda cfg, run, slot: work(run)), 0)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(started, [self.ticket])


class AnswerContinuationBudgetTests(OwnerAnswerBudgetFixture):
    def test_cli_answer_gives_the_first_repair_a_fresh_budget_and_resume_keeps_the_cutoff(self):
        first, asked = self.spent_question(structured=True)

        def work(flow):
            run = flow.ctx.run
            self.assertEqual([a["run"] for a in run.answers_due], [asked.id])
            self.assert_answer_budget(run, first, asked)
            flow.route()
            return "interrupted", "stopped before the answer was consumed", ""

        def launch(command, log):
            self.assertEqual(command[-3:], ["--force", "--continue", asked.id])
            script = pipeline.Script("feature", work, continues=asked.id)
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, force=True, script=script), 1)
            return run_store.newest_whole_run(self.repo.name, self.ticket)["id"]

        with patch.object(config, "load", return_value=self.cfg), \
                patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                patch.object(tasks, "spawned", side_effect=lambda repo, tid, spawn: (spawn(), "interrupted")), \
                patch.object(tasks.control, "launch", side_effect=launch):
            started = tasks.answer_ask("ask-fixture", {"answers": [{"text": "See the attached log."}]})
            answered = started["run"]

            def resume(flow):
                run = flow.ctx.run
                self.assertEqual([a["run"] for a in run.answers_due], [asked.id])
                self.assertEqual(run.budget.repair_parents, [answered])
                self.assertEqual(run.budget.prior_repairs, 1)
                for _ in range(2):
                    run.budget.charge("repair", "review_repair")
                with self.assertRaisesRegex(runlimits.Parked, "3 repair cycles"):
                    run.budget.charge("repair", "review_repair")
                return "noop", "fixture finished", ""

            script = pipeline.Script("feature", resume, continues=answered)
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, force=True, script=script), 0)

    def test_tracker_reply_to_an_unstructured_question_gives_a_fresh_budget_after_a_dry_run(self):
        self.check_tracker_reply_budget(structured=False, dry_run=True)

    def test_tracker_reply_to_a_structured_question_gives_a_fresh_budget_after_a_dry_run(self):
        self.check_tracker_reply_budget(structured=True, dry_run=True)


class TrackerReplyBudgetTests(OwnerAnswerBudgetFixture):
    def test_tracker_reply_to_a_structured_question_gives_a_fresh_budget(self):
        self.check_tracker_reply_budget(structured=True)

    def test_tracker_reply_to_an_unstructured_question_gives_a_fresh_budget(self):
        self.check_tracker_reply_budget(structured=False)

    def test_tracker_restart_after_an_expired_structured_question_gives_a_fresh_budget(self):
        self.check_tracker_reply_budget(structured=True, expired=True)
        self.assertEqual(ask_store.ask_get("ask-fixture")["status"], "expired")

    def test_tracker_reply_keeps_the_dashboard_answer_written_during_lookup(self):
        asked_at = datetime(2026, 1, 2, tzinfo=UTC)
        with patch.object(util, "now", return_value=asked_at):
            first, asked = self.spent_question(structured=True)
            ask_store.ask_add("ask-second", asked.id, self.repo.name, self.ticket, "review", "review", "", "", [
                {"kind": "text", "header": "Failed job", "question": "Which CI job failed?"}], {})
        owner = principals.seed_owner("fixture@example.test")
        with patch.object(util, "now", return_value=asked_at - timedelta(days=1)), principals.acting(owner):
            tasks_store.task_comment(self.ticket, principals.current(), "An older comment.")
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("launched with open asks")), 0)
        self.assertEqual(len(ask_store.asks_of(asked.id, "open")), 2)
        with patch.object(util, "now", return_value=asked_at + timedelta(days=1)), principals.acting(owner):
            tasks_store.task_comment(self.ticket, principals.current(), "The lint job failed.")

        reply_since = tracker.LocalTracker.reply_since

        def reply_after_dashboard_answer(source, tid, since):
            self.assertFalse(util.lock_is_held(tasks.STEER_LOCK))
            answer = tasks.answer_ask("ask-fixture", {"answers": [{"text": "The log is on the dashboard."}]})
            self.assertEqual(answer["state"], "waiting")
            self.assertEqual(ask_store.ask_get("ask-second")["status"], "open")
            return reply_since(source, tid, since)

        started = []

        def work(run):
            started.append(run.id)
            self.assert_answer_budget(run, first, asked)
            self.consume_tracker_answers(run, {"ask-fixture": "The log is on the dashboard.",
                                               "ask-second": "The lint job failed."})
            return "noop", "fixture finished", ""

        with patch.object(tracker.LocalTracker, "reply_since", autospec=True, side_effect=reply_after_dashboard_answer):
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_answer(repo, tid, asked, work)), 0)
        self.assertEqual(len(started), 1)


class UnansweredRepairBudgetTests(OwnerAnswerBudgetFixture):
    def test_github_label_removal_without_a_reply_admits_a_run_with_spent_repairs(self):
        self.repo = replace(self.repo, name="github-fixture", github="owner/project", tracker="github",
                            parts=[config.Part("", "owner/project")])
        self.cfg.repos = [self.repo]
        self.ticket = "697"
        labels = {tracker.LABEL}
        source = tracker.GitHubTracker(self.repo)

        def issue_view(repo, number):
            return {"number": number, "title": "Fixture issue", "body": "Fixture description", "state": "OPEN",
                    "author": {"login": "owner"}, "labels": [{"name": label} for label in sorted(labels)],
                    "comments": []}

        def issue_list(repo, label, **kwargs):
            return [issue_view(repo, int(self.ticket))] if label in labels else []

        def edit_labels(repo, number, add=(), remove=()):
            labels.difference_update(remove)
            labels.update(add)

        with (patch.object(gh, "issue", side_effect=issue_view), patch.object(gh, "issues", side_effect=issue_list),
              patch.object(gh, "issue_labels", side_effect=edit_labels), patch.object(gh, "issue_comment")):
            first, asked = self.spent_question(structured=True)
            self.assertIn(source.label_name("needs-info"), labels)
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("launched before label removal")), 0)
            self.assertEqual(source.label(self.ticket, remove=["needs-info"]), "")
            started = []

            def work(cfg, run, slot):
                started.append(run.id)
                self.assertEqual(ask_store.ask_get("ask-fixture")["status"], "open")
                self.assertEqual(run.answers_due, [])
                self.assertFalse(pipeline.answer_text(asked.id))
                self.assertEqual(run.budget.repair_left_out, [])
                self.assertCountEqual(run.budget.repair_parents, [first.id, asked.id])
                self.assertEqual(run.budget.prior_repairs, 3)
                with self.assertRaisesRegex(runlimits.Parked, "3 repair cycles"):
                    run.budget.charge("repair", "review_repair")
                return "noop", "fixture finished", ""

            def spawn(repo, tid):
                self.assertNotIn("--continue", pipeline.run_command(repo, tid))
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertEqual(len(started), 1)

    def test_tracker_restart_after_expiry_without_a_qualifying_reply_keeps_spent_repairs(self):
        _, asked = self.expired_question()
        self.local.comment(self.ticket, "An unrelated bot comment after expiry.")
        self.local.label(self.ticket, remove=["needs-info"])
        started = []

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertFalse(pipeline.answer_text(asked.id))
            self.assertEqual(run.budget.prior_repairs, 3)
            with self.assertRaisesRegex(runlimits.Parked, "3 repair cycles"):
                run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
class PrAnswerFixture(PollFixture):
    def prepare_pr_fixture(self, checkouts=()):
        return factory.Outcome("pass", data={
            "layer": str(self.repo.layer_dir), "checkouts": [c.to_row() for c in checkouts],
            "ticket": asdict(self.local.get(self.ticket)), "branch": "fixture", "add_dirs": [],
            "attachments": "", "expertise": "", "triage": "",
        })

    def open_pr_ask(self, asked, kind="text", *, step="orchestrate", session=None):
        asked_at = (util.now() - timedelta(minutes=pipeline.FEEDBACK_QUIET_MINUTES + 2)).replace(microsecond=0)
        question = {"kind": kind, "header": "CI log", "question": "Where is the CI log?"}
        if kind == "secret":
            question["name"] = "CI_LOG_TOKEN"
        with patch.object(util, "now", return_value=asked_at):
            ask_store.ask_add("ask-fixture", asked.id, self.repo.name, self.ticket, step, step, "", "",
                              [question], asdict(session) if session else {})
        record.settle(asked.id, "needs-info")
        self.local.label(self.ticket, add=["needs-info"])
        return asked_at

    def add_pr_comment(self, pr, comment, created, gh_id=1):
        insights_store.add_feedback([{"pr_url": pr, "kind": "comment", "gh_id": gh_id, "repo": self.repo.name,
                                      "issue": self.ticket, "body": comment, "created": util.iso(created), "at_once": 0,
                                      "pr_state": "OPEN", "status": "new"}])

    def add_issue_reply(self, text):
        with principals.acting(principals.seed_owner("fixture@example.test")):
            tasks_store.task_comment(self.ticket, principals.current(), text)

    def poll_continuation(self, asked, path):
        exit_codes = []

        def spawn(repo, tid):
            self.assertEqual(pipeline.run_command(repo, tid),
                             pipeline.script_command(self.cfg, path, repo, str(tid), "--continue", asked.id))
            script = pipeline.Script(path, pipeline.flow_function(self.cfg, path), continues=asked.id)
            exit_codes.append(self.run_polled_ticket(repo, tid, lambda *_: self.fail("started an ordinary run"), script))

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        revision = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertEqual(exit_codes, [0], revision["summary"])
        return revision

    def assert_feedback_done(self, pr, comments):
        rows = insights_store.feedback_between("", "9999")
        self.assertCountEqual([(row["body"], row["status"]) for row in rows if row["pr_url"] == pr],
                              [(body, "done") for body in comments])


class OrchestratedFeedbackAnswerTests(PrAnswerFixture):
    def waiting_pr_ask(self, kind="text", *, repair=False):
        self.cfg.replay = config.ReplayConfig(all=True)
        self.repo.layer_dir.mkdir()
        pr = "https://github.com/owner/project/pull/1"
        published = self.made_run()
        starting = published
        checkouts = []
        if repair:
            run_lines.write(published.id, run_lines.END, review={"verdict": "request_changes", "problems": [
                {"file": "notes.txt", "text": "Fix the saved finding.", "category": "issue"}]})
            (published.dir / "plan.md").write_text("Implement the issue.")
            record.settle(published.id, "pr", pr_url=pr, branch="fixture")
            starting = self.made_run(published.id)
            starting.repairs = published.id
            run_lines.write(starting.id, "repair", source=published.id)
            wt = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
            (wt / ".git").mkdir(parents=True)
            checkouts.append(workspace.Checkout("", "owner/project", self.repo.path, wt, "main", pr, "fixture-head"))
        starting.path = "orchestrate"
        controller = orchestration.Controller(flows.start(self.cfg, starting, {}))
        prepared = self.prepare_pr_fixture(checkouts)

        def prepare_workspace(*args, **kwargs):
            controller.job.take("prepare", prepared, stored=False)
            return prepared

        with (patch.object(controller, "atomic", side_effect=prepare_workspace),
              patch.object(pipeline, "workspace_note", return_value="Fixture workspace"),
              patch.object(repairs, "matched_head", return_value="fixture-head")):
            self.assertIsNone(controller.prepare())
        if repair:
            asked, lease = starting, controller.lease
        else:
            record.settle(published.id, "pr", pr_url=pr)
            asked = self.made_run(published.id)
            lease = ledger.acquire(asked.id, published.id, {})
        turn = ledger.begin(lease, {}, 10)
        action, = ledger.accept(lease, turn, ledger.snapshot(lease)["version"],
                                {"calls": [{"op": "owner.ask", "args": {}}]}, 10)
        ledger.transition(lease, action["id"], "succeeded", {"ask_ids": ["ask-fixture"], "awaiting_owner": True})
        return asked, pr, self.open_pr_ask(asked, kind)

    def check_polled_pr_answer(self, issue_reply="", *, stale_feedback=False, repair=False):
        asked, pr, asked_at = self.waiting_pr_ask(repair=repair)
        comments = []
        if stale_feedback:
            for gh_id, offset in enumerate((-1, 0), start=2):
                stale = f"Please rename button {gh_id}."
                self.add_pr_comment(pr, stale, asked_at + timedelta(seconds=offset), gh_id=gh_id)
                comments.append(stale)
        comment = "The lint job has the log. Please fix the failing check."
        self.add_pr_comment(pr, comment, asked_at + timedelta(seconds=1))
        comments.append(comment)
        if issue_reply:
            self.add_issue_reply(issue_reply)
        expected = issue_reply or comment

        def decide(run, turn, request):
            self.assertIn(f"Answer: {expected}", request["prompt"])
            for body in comments:
                self.assertIn(body, request["prompt"])
            if repair:
                self.assertTrue(run.repairs)
                self.assertEqual(run.repairs, pipeline.repaired_by(asked.id))
            found = ledger.find(run.id)
            response = {"version": PROTOCOL, "decision_id": turn,
                        "state_version": found["version"], "rationale": "Fixture reviewed owner input.",
                        "calls": [{"op": "run.finish", "args": {"reason": "noop"}}]}
            return SimpleNamespace(pid=0, identity=None, proc=SimpleNamespace(poll=lambda: 0),
                                   receive=lambda: [{"ok": True, "structured": response}], close=lambda: None)

        with (patch.object(delegation, "Worker", side_effect=decide) as worker,
              patch.object(pipeline, "workspace_note", return_value="Fixture workspace"),
              patch.object(flows.Job, "reclaim", return_value=""),
              patch.object(workspace, "kept_work", return_value=""),
              patch.object(repairs, "matched_head", return_value="fixture-head"),
              patch.object(pipeline, "stage", return_value=pipeline.Staged()),
              patch.object(pipeline, "nothing_to_build", return_value=("pr", "fixture finished", pr)),
              patch.object(gh, "pr_view", return_value={"state": "OPEN"}),
              patch.object(gh, "pr_body", return_value="")):
            revision = self.poll_continuation(asked, "orchestrate")
        worker.assert_called_once()
        ask = ask_store.ask_get("ask-fixture")
        self.assertEqual((ask["taken_by"], ask["answers"][0]["text"]), (revision["id"], expected))
        data = ledger.find(revision["id"])["data"]
        self.assertEqual([(row["pr_url"], row["body"]) for row in data["feedback"]], [(pr, body) for body in comments])
        self.assertIn(f"Answer: {expected}", data["job"]["goal"])
        for body in comments:
            self.assertIn(body, data["goal_base"])
        self.assertTrue(any(line.get("op") == "owner.ask" and line.get("recovered") and line.get("source") == asked.id
                            for line in run_lines.read(revision["id"])))
        self.assert_feedback_done(pr, comments)

    def test_poll_uses_pr_feedback_as_answer_and_recovers_orchestrated_revision(self):
        self.check_polled_pr_answer()

    def test_poll_leaves_an_ask_open_when_only_earlier_or_equal_second_feedback_is_due(self):
        asked, pr, asked_at = self.waiting_pr_ask()
        earlier, equal = "Please rename the button.", "Please move the icon."
        self.add_pr_comment(pr, earlier, asked_at - timedelta(seconds=1))
        self.add_pr_comment(pr, equal, asked_at, gh_id=2)
        started = []
        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertEqual(started, [])
        self.assertEqual(ask_store.asks_of(asked.id)[0]["status"], "open")
        pending = pipeline.pending_feedback(self.repo.name, self.ticket)
        self.assertEqual([(row["body"], row["status"]) for row in pending], [(earlier, "new"), (equal, "new")])
        name = tracker.ticket_name(self.repo.name, self.local.ref(self.ticket))
        self.assertEqual(util.read_json(self.root / "skipped.json", {})[name]["why"],
                         "its asks are still waiting for an owner answer")

    def test_poll_keeps_earlier_and_equal_second_comments_as_revision_feedback(self):
        self.check_polled_pr_answer(stale_feedback=True)

    def test_poll_prefers_issue_reply_to_due_pr_feedback_as_answer(self):
        self.check_polled_pr_answer("The integration job has the log.")

    def test_poll_delivers_pr_feedback_when_an_orchestrated_repair_recovers_an_issue_answer(self):
        self.check_polled_pr_answer("The integration job has the log.", repair=True)

    def test_poll_blocks_pr_feedback_when_an_open_ask_holds_a_secret(self):
        asked, pr, asked_at = self.waiting_pr_ask("secret")
        self.add_pr_comment(pr, "The lint job has the log.", asked_at + timedelta(minutes=1))
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("launched with an open secret ask")), 0)
        self.assertEqual(ask_store.asks_of(asked.id)[0]["status"], "open")
        name = tracker.ticket_name(self.repo.name, self.local.ref(self.ticket))
        self.assertEqual(util.read_json(self.root / "skipped.json", {})[name]["why"], poll.SECRET_ASK_REPLY)


class AdwPrFixture(PrAnswerFixture):
    def configure_adw(self):
        self.cfg.replay = config.ReplayConfig(all=True)
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "adw"\n')

    def adw_pr_steps(self, prepared):
        return {
            "prepare": lambda *a, **kw: prepared,
            "checks": lambda flow, **kw: factory.Outcome("pass", data={"policy": flow.ctx.policy("checks", kw)}),
            "verify": lambda flow, **kw: factory.Outcome("pass", data={
                "verified": {"verdict": "pass"}, "names": [], "policy": flow.ctx.policy("verify", kw)}),
            "learn": lambda *a, **kw: factory.Outcome("pass"),
            "pr": lambda *a, **kw: factory.Outcome("pass", data={
                "prs": {}, "opened": [], "changed": [], "pushed": [], "committed_lines": 1,
                "draft_why": "", "report_body": "", "summary": ""}),
            "publish": lambda *a, **kw: factory.Outcome("pass", data={"ending": "pr"}),
        }


class AdwRevisionFeedbackTests(AdwPrFixture):
    def check_adw_pr_answer(self, *, new_feedback, asking_step="review", legacy=False, only_review=False):
        self.configure_adw()
        pr = "https://github.com/owner/project/pull/1"
        published = self.made_run()
        run_lines.write(published.id, "execution", path="feature")
        record.settle(published.id, "pr", pr_url=pr)
        asked = self.made_run(published.id)
        asked.path = "revise"
        run_lines.write(asked.id, "execution", path="revise")
        original, fresh = "Please rename the button.", "Please also move the icon."
        self.add_pr_comment(pr, original, util.now() - timedelta(minutes=pipeline.FEEDBACK_QUIET_MINUTES + 3))
        asked.feedback = pipeline.pending_feedback(self.repo.name, self.ticket)
        insights_store.take_feedback(asked.feedback, asked.id)
        insights_store.settle_feedback(asked.id, "failed")
        prepared = self.prepare_pr_fixture()
        session = Session(id="kept-ask-session")
        expected = "The integration job has the log."

        def revise_step(flow, **kwargs):
            outcome = flows.revision(flow, **kwargs)
            if legacy and flow.run_id == asked.id:
                outcome.data.pop("feedback_keys", None)
            return outcome

        with (patch.object(factory, "fingerprint", return_value="fixture-tree"),
              patch.object(pipeline, "workspace_note", return_value="Fixture workspace"),
              patch.object(pipeline, "stage", return_value=pipeline.Staged(counts={"notes.txt": 1})),
              patch.object(gh, "pr_body", return_value=""),
              patch.object(pipeline, "record_prs", return_value=pr),
              patch.object(pipeline, "release_workspace"), patch.object(flows, "documents", return_value=False),
              patch.object(pipeline.AgentRun, "agent",
                           return_value=Result(ok=True, structured={"summary": "Fixture", "problems": []})) as agent,
              patch.dict(factory.STEPS, {**self.adw_pr_steps(prepared), "revise": revise_step})):
            prior = flows.start(self.cfg, asked, {})
            tree = str(prior.ctx.ws)
            prior.ctx.ws.mkdir()
            flows.step(prior, "prepare", tree=tree)
            if asking_step == "review":
                flows.step(prior, "revise", tree=tree)
                flows.step(prior, "checks", tree=tree, attempt=1, tries=asked.settings("check_repair").tries)
                flows.step(prior, "verify", tree=tree)
            run_lines.write(asked.id, "step_end", step=asking_step, result="ask")
            asked_at = self.open_pr_ask(asked, step=asking_step, session=session)
            if new_feedback:
                self.add_pr_comment(pr, fresh, asked_at + timedelta(seconds=1), gh_id=2)
            agent.reset_mock()
            if only_review:
                with (patch.object(config, "load", return_value=self.cfg),
                      patch.object(pipeline, "has_origin", return_value=True),
                      self.assertRaises(SystemExit) as result):
                    factory.main(pipeline.flow_function(self.cfg, "revise"),
                                 [self.repo.name, self.ticket, "--continue", asked.id, "--only", "review"])
                rerun = next(row for row in run_store.ticket_runs(self.repo.name, self.ticket) if row["only_step"])
                self.assertEqual(result.exception.code, 0, rerun["summary"])
            else:
                self.add_issue_reply(expected)
                revision = self.poll_continuation(asked, "revise")

        if only_review:
            self.assertEqual(rerun["summary"], "review pass")
            self.assertEqual([call.args[0] for call in agent.call_args_list], ["review"])
            self.assertNotIn(fresh, agent.call_args.args[1])
            pending = pipeline.pending_feedback(self.repo.name, self.ticket)
            self.assertEqual([(row["body"], row["status"], row["run_id"]) for row in pending],
                             [(original, "failed", asked.id), (fresh, "new", None)])
            return

        calls = {call.args[0]: call for call in agent.call_args_list}
        self.assertEqual([call.args[0] for call in agent.call_args_list],
                         ["revise", "review"] if new_feedback else ["review"])
        if new_feedback:
            self.assertIn(fresh, calls["revise"].args[1])
        self.assertEqual(calls[asking_step].kwargs["resume"], session)
        self.assertIn(expected, calls[asking_step].args[1])
        self.assertEqual(ask_store.ask_get("ask-fixture")["taken_by"], revision["id"])
        self.assert_feedback_done(pr, [original, fresh] if new_feedback else [original])

    def test_poll_reruns_saved_adw_revision_for_new_pr_feedback_before_resuming_review(self):
        self.check_adw_pr_answer(new_feedback=True)

    def test_poll_reuses_saved_adw_revision_when_only_the_issue_answer_is_new(self):
        self.check_adw_pr_answer(new_feedback=False, legacy=True)

    def test_poll_delivers_new_pr_feedback_when_resuming_the_adw_revision_agent(self):
        self.check_adw_pr_answer(new_feedback=True, asking_step="revise")

    def test_isolated_adw_review_reuses_saved_revision_and_leaves_new_feedback_pending(self):
        self.check_adw_pr_answer(new_feedback=True, only_review=True, legacy=True)


class AdwRepairContinuationTests(AdwPrFixture):
    def check_polled_adw_repair(self, session, asking_step="repair"):
        self.configure_adw()
        pr = "https://github.com/owner/project/pull/1"
        published = self.made_run()
        run_lines.write(published.id, run_lines.END, review={"verdict": "request_changes", "problems": [
            {"file": "notes.txt", "text": "Fix the saved finding.", "category": "issue"}]})
        (published.dir / "plan.md").write_text("Implement the issue.")
        record.settle(published.id, "pr", pr_url=pr, branch="fixture")
        asked = self.made_run(published.id)
        asked.path, asked.repairs = "repair", published.id
        run_lines.write(asked.id, "execution", path="repair")
        run_lines.write(asked.id, "repair", source=published.id)
        prior = flows.start(self.cfg, asked, {})
        checkout = workspace.Checkout("", "owner/project", self.repo.path, prior.ctx.ws, "main", pr, "fixture-head")
        (prior.ctx.ws / ".git").mkdir(parents=True)
        comment, reply = "Please move the icon.", "The integration job has the log."
        repairs_called = []

        def worker(prompt, cwd, *, trace, **kw):
            if trace[0] != asked.id:
                self.assertNotIn(comment, prompt)
                if trace[1] == asking_step:
                    self.assertIn(f"Answer: {reply}", prompt)
                    self.assertEqual(kw["resume"], session)
                    if session.id:
                        fresh_prompt = kw["fresh_prompt"]("codex")
                        self.assertIn(f"Answer: {reply}", fresh_prompt)
                        self.assertNotIn(comment, fresh_prompt)
                if trace[1] == "repair":
                    repairs_called.append(trace[0])
            return Result(ok=True, text="Fixture repaired.", structured={"summary": "Fixture", "problems": []})

        with (patch.object(factory, "fingerprint", return_value="fixture-tree"),
              patch.object(pipeline, "workspace_note", return_value="Fixture workspace"),
              patch.object(pipeline, "stage", return_value=pipeline.Staged(counts={"notes.txt": 1})),
              patch.object(pipeline, "index_trees", return_value={}),
              patch.object(pipeline, "record_prs", return_value=pr),
              patch.object(pipeline, "release_workspace"), patch.object(flows, "documents", return_value=False),
              patch.object(workspace, "kept_work", return_value=""), patch.object(flows.Job, "reclaim", return_value=""),
              patch.object(repairs, "matched_head", return_value="fixture-head"),
              patch.object(pipeline.runner, "run", side_effect=worker),
              patch.dict(factory.STEPS, self.adw_pr_steps(self.prepare_pr_fixture([checkout])))):
            tree = str(prior.ctx.ws)
            flows.step(prior, "prepare", tree=tree)
            if asking_step == "review":
                self.assertTrue(flows.step(prior, "repair", tree=tree).passed)
                flows.step(prior, "checks", tree=tree, attempt=1, tries=asked.settings("check_repair").tries)
                flows.step(prior, "verify", tree=tree)
            run_lines.write(asked.id, "step_end", step=asking_step, result="ask")
            asked_at = self.open_pr_ask(asked, step=asking_step, session=session)
            self.add_pr_comment(pr, comment, asked_at + timedelta(seconds=1))
            self.add_issue_reply(reply)
            with patch.object(flows, "start", wraps=flows.start) as start:
                repair = self.poll_continuation(asked, "repair")

        self.assertEqual(start.call_args.args[1].feedback, [])
        self.assertEqual(repairs_called, [repair["id"]] if asking_step == "repair" else [])
        self.assertEqual(ask_store.ask_get("ask-fixture")["taken_by"], repair["id"])
        pending = pipeline.pending_feedback(self.repo.name, self.ticket)
        self.assertEqual([(row["body"], row["status"], row["run_id"]) for row in pending], [(comment, "new", None)])

    def test_poll_delivers_only_the_issue_answer_to_resumed_adw_repair_agent(self):
        self.check_polled_adw_repair(Session(id="kept-repair-session", runner="codex"))

    def test_poll_delivers_only_the_issue_answer_to_fresh_adw_repair_agent(self):
        self.check_polled_adw_repair(Session())

    def test_poll_reuses_saved_adw_repair_when_pr_feedback_is_pending(self):
        self.check_polled_adw_repair(Session(id="kept-review-session", runner="codex"), asking_step="review")


class ReadmissionFixture(PollFixture):
    def park_ticket(self, branch=""):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        run = self.made_run()
        run.budget.charge("review_fix1", "review_repair")
        record.note(run.id, branch=branch)
        run.budget.park("repair limit")
        record.settle(run.id, "failed")
        self.local.label(self.ticket, add=["failed"])
        self.cfg.replay = config.ReplayConfig(all=True)
        return run


class ParkedRetryBudgetTests(Isolated):
    def setUp(self):
        super().setUp()
        self.layer = self.repo.layer_dir / "config.toml"
        self.layer.parent.mkdir()
        self.layer.write_text('[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        self.patches.enter_context(patch.object(config, "load", return_value=self.cfg))
        self.patches.enter_context(patch.object(pipeline, "run_refusal", return_value=(False, "")))

    def park_spent_ticket(self, branch=""):
        def spend(cfg, run, slot):
            record.note(run.id, branch=branch)
            for _ in range(run.budget.policy.max_repair_cycles + 1):
                run.budget.charge("repair", "review_repair")

        with patch.object(pipeline, "_run", side_effect=spend):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket), 1)
        parked, saved = runlimits.latest_park(self.repo.name, self.ticket)
        self.assertEqual(saved["reason"], "the repair chain used its 3 repair cycles")
        self.assertEqual(runlimits.repair_count(parked["id"]), 3)

    def assert_fresh_retry(self):
        def repair(cfg, run, slot):
            run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        with patch.object(pipeline, "_run", side_effect=repair):
            result = pipeline.run_issue(self.repo.name, self.ticket, force=True)
        retry = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertEqual(result, 0, retry["summary"])
        self.assertEqual(runlimits.repair_count(retry["id"]), 1)
        log = (paths.RUNS_DIR / retry["id"] / "run.log").read_text()
        self.assertIn("budget chain: no parents\n", log)

    def test_explicit_retry_after_published_head_changes_resets_spent_repairs(self):
        with patch.object(runlimits, "branch_head", return_value="a" * 40) as head:
            self.park_spent_ticket("arete/fixture")
            head.return_value = "b" * 40
            self.assert_fresh_retry()

    def test_explicit_retry_after_policy_changes_resets_spent_repairs(self):
        self.park_spent_ticket()
        self.layer.write_text(self.layer.read_text() + "max_run_minutes = 90\n")
        self.assert_fresh_retry()


class BudgetReadmissionTests(ReadmissionFixture):
    def test_specification_edit_readmits_once_and_reparks_on_current_inputs(self):
        parked = self.park_ticket()
        with foundation.db() as connection:
            set_task_body(self.ticket, "A narrower specification", connection=connection)
        started = []

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            raise runlimits.Parked("repair limit again")

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 1)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        self.assertNotEqual(started[0], parked.id)
        self.assertEqual(runlimits.latest_park(self.repo.name, self.ticket)[0]["id"], started[0])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        self.assertIn(f"parked after run {started[0]}: repair limit again", output.getvalue())
        self.assertIn(runlimits.RETRY, output.getvalue())

    def test_raised_layer_budget_readmits_a_failed_ticket_with_fresh_repairs(self):
        self.park_ticket()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        started = []

        def work(cfg, run, slot):
            started.append(run.budget.policy.max_repair_cycles)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(run.budget.repair_parents, [])
            self.assertEqual(run.budget.prior_repairs, 0)
            for _ in range(3):
                run.budget.charge("repair", "review_repair")
            with self.assertRaisesRegex(runlimits.Parked, "3 repair cycles"):
                run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
        self.assertEqual(started, [3])

    def test_child_refuses_a_specification_or_policy_reverted_after_poll(self):
        parked = self.park_ticket()
        original = self.local.get(self.ticket).body
        layer = self.repo.layer_dir / "config.toml"
        original_layer = layer.read_text()

        def set_body(body):
            with foundation.db() as connection:
                set_task_body(self.ticket, body, connection=connection)

        for change, revert in (
                (lambda: set_body("Changed specification"), lambda: set_body(original)),
                (lambda: layer.write_text(original_layer + "[limits]\nmax_repair_cycles = 3\n"),
                 lambda: layer.write_text(original_layer))):
            change()

            def spawn(repo, tid):
                revert()
                self.assertEqual(self.run_polled_ticket(repo, tid, lambda *_: self.fail("stale launch")), 2)

            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertIn(f"parked after run {parked.id}", output.getvalue())
            self.assertEqual([row["id"] for row in run_store.ticket_runs(self.repo.name, self.ticket)], [parked.id])

    def test_poll_does_not_repeat_a_launch_that_exits_before_registration(self):
        self.park_ticket()
        starts = []

        def spawn(repo, tid):
            starts.append(tid)

        for body in ("First edit", "Second edit"):
            with foundation.db() as connection:
                set_task_body(self.ticket, body, connection=connection)
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertIn("Poll already tried these inputs.", output.getvalue())
            self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, runlimits.Policy(), True),
                             runlimits.Admission(reset_time=True, reset_repairs=True))
        self.assertEqual(starts, [self.ticket, self.ticket])


class TrackerReadmissionTests(ReadmissionFixture):
    def test_branch_readmission_checks_origin_once_per_poll_and_fails_closed(self):
        branch = "arete/fixture"
        command = ["git", "ls-remote", "origin", f"refs/heads/{branch}"]
        old, new = "a" * 40, "b" * 40
        remote = SimpleNamespace(returncode=0, stdout=f"{old}\trefs/heads/{branch}\n")
        calls, started = [], []
        sh = util.sh

        def read(cmd, *args, **kwargs):
            if cmd == ["git", "remote", "get-url", "origin"]:
                return SimpleNamespace(returncode=0, stdout="/tmp/fixture.git\n")
            if cmd == command:
                calls.append(cmd)
                return remote
            return sh(cmd, *args, **kwargs)

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(run.budget.prior_repairs, 0)
            return "noop", "fixture finished", ""

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

        with patch.object(util, "sh", side_effect=read):
            parked = self.park_ticket(branch)
            unchanged = f"{old}\trefs/heads/{branch}\n{new}\trefs/heads/nested/refs/heads/{branch}\n"
            moved = f"{new}\trefs/heads/{branch}\n"
            cases = ((unchanged, 0, False), (moved, 1, False), ("truncated\n", 0, False), (moved, 0, True))
            for stdout, returncode, should_start in cases:
                with self.subTest(returncode=returncode, stdout=stdout):
                    calls.clear()
                    remote.returncode = returncode
                    remote.stdout = stdout
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                    self.assertEqual(calls, [command])
                    self.assertEqual(len(started), int(should_start))
                    if not should_start:
                        self.assertIn(f"parked after run {parked.id}: repair limit", output.getvalue())
                        self.assertIn(runlimits.RETRY, output.getvalue())
                    if returncode:
                        self.assertIn("could not confirm the parked branch head", output.getvalue())

    def test_multi_origin_readmission_checks_complete_exact_refs_and_revalidates_origins(self):
        branch = "arete/fixture"
        ref = f"refs/heads/{branch}"
        client, server = "a" * 40, "b" * 40
        self.repo.tracker = "jira"
        self.repo.parts = [config.Part("server", "Acme/server"), config.Part("client", "Acme/client")]
        urls = {"client": "https://github.com/acme/client.git", "server": "git@github.com:acme/server.git"}
        for name in urls:
            (self.repo.path / name).mkdir()
        lookups, calls, started = [], [], []
        sh = util.sh

        def repository(project, oid, name=branch):
            ref_row = {"prefix": "refs/heads/", "name": name, "target": {"oid": oid}} if oid else None
            return {"nameWithOwner": project, "ref": ref_row}

        old_client = repository("Acme/client", client)
        old_server = repository("Acme/server", server)
        deleted = repository("Acme/client", "")
        complete = {"part0": deleted, "part1": old_server}
        reply = {}

        def read(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "ls-remote"]:
                name = kwargs["cwd"].name
                lookups.append(name)
                oid = client if name == "client" else server
                return SimpleNamespace(returncode=0, stdout=f"{oid}\t{ref}\n")
            if cmd == ["git", "remote", "get-url", "origin"]:
                return SimpleNamespace(returncode=0, stdout=urls[kwargs["cwd"].name] + "\n")
            if cmd[:3] == ["gh", "api", "graphql"]:
                self.assertEqual(cmd[3:], ["--hostname", "github.com", "--input", "-"])
                calls.append(json.loads(kwargs["input"]))
                return SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr="")
            return sh(cmd, *args, **kwargs)

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertEqual(run.budget.parents, [])
            return "noop", "fixture finished", ""

        with patch.object(util, "sh", side_effect=read), patch.object(tracker, "for_repo", return_value=self.local):
            parked = self.park_ticket(branch)
            self.assertCountEqual(lookups, ["client", "server"])
            lookups.clear()
            for response in ({"data": {"part0": old_client, "part1": old_server}},
                             {"data": {"part0": deleted}},
                             {"data": {"part0": deleted, "part1": {"nameWithOwner": "Acme/server"}}},
                             {"data": complete, "errors": [{"message": "partial failure"}]},
                             {"data": {**complete, "part1": repository("Acme/another", server)}},
                             {"data": {**complete, "part1": repository("Acme/server", server, branch + "-other")}}):
                with self.subTest(response=response):
                    reply = response
                    calls.clear()
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("unexpected launch")), 0)
                    self.assertIn(f"parked after run {parked.id}", output.getvalue())
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0]["variables"], {"ref": ref})
                    self.assertIn('name: "client"', calls[0]["query"])
                    self.assertIn('name: "server"', calls[0]["query"])

            reply = {"data": complete}
            urls["server"] = "https://github.com.example/acme/server.git"
            calls.clear()
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("unverified origin")), 0)
            self.assertEqual(calls, [])

            urls["server"] = "git@github.com:acme/server.git"

            def stale_origin(repo, tid):
                urls["server"] = "https://github.com/acme/wrong.git"
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 2)

            self.assertEqual(self.poll_with(self.cfg, stale_origin), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(started, [])

            urls["server"] = "git@github.com:acme/server.git"
            reply = {"data": {**complete, "part1": repository("Acme/server", "c" * 40)}}
            calls.clear()

            def spawn(repo, tid):
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(lookups, [])
            self.assertEqual(len(started), 1)

    def test_jira_parked_candidate_can_follow_a_page_of_ordinary_failures(self):
        self.repo.jira_project = "FIXTURE"
        source = tracker.JiraTracker(self.repo, self.cfg.jira)
        client = jira.Client("https://example.invalid", "unused", "unused")
        ordinary = [{"key": f"FIXTURE-{i}"} for i in range(1, 21)]
        parked = {"key": "FIXTURE-21", "fields": {"summary": "Parked ticket"}}
        with patch.dict(tracker._clients, {"": client}), patch.object(client, "get", side_effect=[
            {"issues": [], "isLast": True},
            {"issues": ordinary, "isLast": False, "nextPageToken": "next"},
            {"issues": [parked], "isLast": True},
        ]) as read:
            self.assertEqual([ticket.id for ticket in source.todo(parked=lambda key: key == parked["key"])],
                             [parked["key"]])
        self.assertEqual(read.call_args.kwargs["nextPageToken"], "next")


class QueueTests(PollFixture):
    def test_interrupted_ticket_rechecks_dependencies_before_starting(self):
        interrupted = self.made_run()
        record.settle(interrupted.id, "interrupted")
        prerequisite = tasks_store.task_add(self.repo.name, "Prerequisite", "Prerequisite", "feat")
        with foundation.db() as connection:
            set_task_body(self.ticket, f"Waits on {prerequisite}", connection=connection)
        self.cfg.replay = config.ReplayConfig(all=True)
        started = []
        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertNotIn(self.ticket, started)
        self.assertIn(prerequisite, started)

    def test_one_poll_fills_capacity_round_robin_without_relying_on_child_rows(self):
        second = config.Repo(name="other", path=self.root / "other", tracker="local", pipeline=True)
        second.path.mkdir()
        for repo in (self.repo, second):
            for i in range(4):
                tasks_store.task_add(repo.name, f"Task {i}", f"Task {i}", "feat")
        cfg = config.Config(repos=[self.repo, second], max_parallel=3, max_parallel_per_repo=2,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append((repo, tid))), 0)
        self.assertEqual(len(started), 3)
        self.assertEqual([repo for repo, _ in started[:2]], [self.repo.name, second.name])
        self.assertEqual(len(set(started)), 3)
        self.assertLessEqual(sum(repo == self.repo.name for repo, _ in started), 2)

    def test_dependency_and_parked_ticket_do_not_block_other_ready_work(self):
        prerequisite = tasks_store.task_add(self.repo.name, "Prerequisite", "Prerequisite", "feat")
        blocked = tasks_store.task_add(self.repo.name, f"Waits on {prerequisite}", "Blocked", "feat")
        parked = self.made_run()
        parked.budget.park("repair limit")
        record.settle(parked.id, "failed")
        failed = tasks_store.task_add(self.repo.name, "Ordinary failure", "Ordinary failure", "feat")
        self.local.label(failed, add=["failed"])
        extra = tasks_store.task_add(self.repo.name, "Ready", "Ready", "feat")
        cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertIn(prerequisite, started)
        self.assertIn(extra, started)
        self.assertNotIn(blocked, started)
        self.assertNotIn(failed, started)
        self.assertNotIn(self.ticket, started)

    def test_route_readiness_is_rechecked_after_every_start(self):
        for i in range(3):
            tasks_store.task_add(self.repo.name, str(i), str(i), "feat")
        ready, started = [True], []
        def routes(*args, **kw):
            return [SimpleNamespace(state="ready" if ready[0] else "held", detail="fixture quota hold", until="")]
        def start(repo, tid):
            started.append(tid)
            ready[0] = False
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=routes)):
            self.assertEqual(self.poll_with(self.cfg, start), 0)
        self.assertEqual(len(started), 1)


class EnvironmentRequeueTests(Isolated):
    class_timeout = 20

    def setUp(self):
        super().setUp()
        tasks_store.task_set_state(self.ticket, "closed")
        self.repo = config.Repo(name="github-fixture", path=self.repo.path, github="owner/project",
                                parts=[config.Part("", "owner/project")], pipeline=True)
        self.cfg = config.Config(repos=[self.repo], replay=config.ReplayConfig(all=True))
        self.issue = "658"
        self.labels = {"agent"}
        self.comments = []
        self.started = []
        self.time = datetime(2026, 11, 21, 8, tzinfo=UTC)
        self.ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.issue}"
        self.authority = self.root / "authority"
        for module, name, func in (
            (config, "load", lambda: self.cfg),
            (codebase, "STAMP", "old-engine"),
            (util, "now", lambda: self.time),
            (gh, "issue", self.issue_view),
            (gh, "issues", self.issue_list),
            (gh, "issue_labels", self.edit_labels),
            (gh, "issue_comment", self.post_comment),
            (gh, "find_issue_comment_marker", self.find_comment),
            (gh, "ensure_label", lambda *args: ""),
            (gh, "default_branch", lambda repo: "main"),
            (poll, "network_up_or_skip", lambda *args: True),
            (poll, "mark_loop", lambda *args: None),
            (poll, "cleanup_worktrees", lambda **kwargs: None),
            (outcomes, "sync_outcomes", lambda cfg: None),
            (outcomes, "sync_moves", lambda cfg: None),
            (outcomes, "sync_landings", lambda cfg: None),
            (scratch, "process_ids", dict),
            (scratch, "service_labels", dict),
            (sandbox, "AUTHORITY", self.authority),
            (pipeline, "SKIPPED_FILE", self.root / "skipped.json"),
            (pipeline, "wanted", lambda repo: "classic"),
            (tasks, "spawn_pending", lambda repo, tid, launch: self.started.append(tid)),
        ):
            self.patches.enter_context(patch.object(module, name, func))
        with scratch.authority_fd(create=True) as fd:
            scratch.create_secret(fd, scratch.SECRET)
            self.key = scratch.read_secret(fd)

    def issue_view(self, repo, number):
        return {"number": number, "title": "Fixture issue", "body": "Fixture description", "state": "OPEN",
                "author": {"login": "owner"}, "labels": [{"name": label} for label in sorted(self.labels)]}

    def issue_list(self, repo, label, **kwargs):
        return [self.issue_view(repo, int(self.issue))] if label in self.labels else []

    def edit_labels(self, repo, number, add=(), remove=(), **kwargs):
        self.labels.difference_update(remove)
        self.labels.update(add)

    def post_comment(self, repo, number, body, **kwargs):
        self.comments.append(body)

    def find_comment(self, repo, number, marker):
        return next(("fixture" for body in self.comments if marker in body.splitlines()), "")

    def run_failure(self, failure=None, script=None):
        with patch.object(pipeline, "_run", side_effect=failure, wraps=pipeline._run):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, script=script), 1)
        self.assertIn("agent:failed", self.labels)
        return run_store.newest_whole_run(self.repo.name, self.issue)

    def test_authenticated_scratch_settlement_requeues_before_picking(self):
        selector = "a" * 64
        data = {"version": 2, "state": "retained", "source_mutations": False, "diagnostic": {},
                "attempt": {"selector": selector, "workspace": str(self.ws),
                            "root": str(self.ws / (paths.SCRATCH_PREFIX + "fixture"))}}
        with scratch.authority_fd(create=True) as fd:
            scratch.store(fd, self.key, data)
        with patch.object(scratch, "recover_pending", return_value=[]):
            row = self.run_failure()
            self.assertEqual(json.loads(row["failure"])["selectors"], [selector])
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)
        record_path = self.authority / (selector + ".json")
        record_path.unlink()
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)

        def settle():
            with scratch.authority_fd() as fd:
                scratch.store(fd, self.key, {**data, "state": "done"})
                scratch.move_settled(fd, selector + ".json")
            return [data["attempt"]["root"]]

        with patch.object(scratch, "recover_pending", side_effect=settle):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])
        self.assertNotIn("agent:failed", self.labels)
        self.assertIn("verification scratch settled", self.comments[-1])

    def test_pruned_scratch_requires_an_authenticated_receipt_when_tracker_returns(self):
        self.cfg.retention = config.Retention(days=0)
        selector = "b" * 64
        name = selector + ".json"
        data = {"version": 2, "state": "retained", "source_mutations": False, "diagnostic": {},
                "attempt": {"selector": selector, "workspace": str(self.ws),
                            "root": str(self.ws / (paths.SCRATCH_PREFIX + "fixture"))}}
        with scratch.authority_fd(create=True) as fd:
            scratch.store(fd, self.key, data)
        with patch.object(scratch, "recover_pending", return_value=[]):
            self.run_failure()
            with scratch.authority_fd() as fd:
                scratch.store(fd, self.key, {**data, "state": "done"})
                scratch.move_settled(fd, name)
            settled = self.authority / scratch.SETTLED / name
            os.utime(settled, (1, 1))
            with patch.object(sandbox, "can_confine", return_value=True), \
                    patch.object(poll, "network_up_or_skip", return_value=False):
                self.assertEqual(poll.poll(), 0)
                self.assertEqual(scratch.rotate(), (1, 0))
        self.assertFalse(settled.exists())
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])
        receipt = self.authority / scratch.RECEIPTS / name
        original = receipt.read_bytes()
        receipt.write_text(json.dumps({"data": {**data, "state": "done"}, "signature": "forged"}))
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])
        receipt.write_bytes(original)
        root = Path(data["attempt"]["root"])
        root.mkdir(parents=True)
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        root.rmdir()
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_disappearance_of_an_unidentifiable_scratch_record_does_not_requeue(self):
        unreadable = self.authority / "unidentifiable.json"
        unreadable.write_text("invalid scratch record")
        with patch.object(scratch, "recover_pending", return_value=[]):
            row = self.run_failure()
            unreadable.unlink()
            self.assertEqual(poll.poll(), 0)
        self.assertTrue(json.loads(row["failure"])["unidentifiable_record"])
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])

    def test_dry_run_rows_cannot_override_live_environment_or_work_failures(self):
        def preview(cfg, run, slot):
            return pipeline._fail(run, "preview work failed")

        self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        with patch.object(pipeline, "_run", side_effect=preview):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, dry_run=True), 1)
        dry = run_store.newest_whole_run(self.repo.name, self.issue)
        self.assertEqual((dry["dry_run"], dry["status"], dry["failure"]), (1, "failed", None))
        abandoned = record.start("pipeline", repo=self.repo.name, issue=self.issue, dry_run=1, pid=None)
        self.time += timedelta(minutes=5)
        comments = list(self.comments)
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(run_store.run_get(abandoned)["status"], "stopped")
        self.assertEqual(self.comments, comments)
        self.assertIn("agent:failed", self.labels)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

        self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.run_failure(workspace.RunFailed("checks failed"))
        with patch.object(pipeline, "_run", return_value=("dry-run", "preview passed", "")):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, dry_run=True), 0)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue])

    def test_worktree_rechecks_the_existing_hold_and_dry_run_preserves_retry_state(self):
        self.ws.mkdir()
        held = self.ws / "owner-file"
        held.write_text("keep me")
        def drive(flow):
            return flows.ended(flow, flows.step(flow, "prepare", tree=str(self.ws)))
        row = self.run_failure(script=pipeline.Script("adw", drive))
        self.assertEqual(json.loads(row["failure"])["reason"], "worktree")
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertEqual(held.read_text(), "keep me")
        held.unlink()
        comments = list(self.comments)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(poll.poll(dry_run=True), 0)
        self.assertIn("would requeue because the worktree is free", output.getvalue())
        self.assertEqual(self.comments, comments)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(run_store.environment_requeue(row, util.iso(self.time - timedelta(days=1))), ({}, 0))
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_crashes_require_a_changed_running_engine_and_work_or_legacy_stay_failed(self):
        row = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.assertEqual(json.loads(row["failure"])["reason"], "engine-crash")
        self.assertEqual(row["engine_sha"], "old-engine")
        self.assertEqual(poll.poll(), 0)
        with patch.object(codebase, "STAMP", ""):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
            self.assertEqual(self.started, [self.issue])
            for failure in ("checks failed", "verification failed", "review requested changes"):
                row = self.run_failure(workspace.RunFailed(failure))
                self.assertEqual(json.loads(row["failure"])["class"], "work")
                self.assertEqual(poll.poll(), 0)
                self.assertIn("agent:failed", self.labels)
            legacy = record.start("pipeline", repo=self.repo.name, issue=self.issue)
            record.settle(legacy, "failed", summary="crashed: Expecting value")
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue])

    def test_fourth_failure_is_capped_across_polls_and_ages_out(self):
        began = self.time
        for _ in range(3):
            self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
            with patch.object(codebase, "STAMP", "new-engine"):
                self.assertEqual(poll.poll(), 0)
            self.time += timedelta(hours=2)
        capped = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        def lost_reply(repo, number, body, **kwargs):
            self.post_comment(repo, number, body)
            raise gh.GhUnsure("reply lost after posting")
        with patch.object(gh, "issue_comment", side_effect=lost_reply):
            self.assertEqual(poll.poll(), 1)
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue] * 3)
        notices = [c for c in self.comments if "Leaving this issue failed" in c]
        self.assertEqual(len(notices), 1)
        self.assertIn("Arete engine crash", notices[0])
        entry, count = run_store.environment_requeue(capped, util.iso(self.time - timedelta(days=1)))
        self.assertEqual((entry["cap_notified"], count), (1, 3))
        self.time = began + timedelta(hours=24)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue] * 4)

    def test_rejected_label_edits_do_not_count_and_lost_replies_reconcile_once(self):
        label = "agent:failed"

        def reject(repo, number, add=(), remove=(), **kwargs):
            self.assertEqual(list(remove), [label])
            raise gh.GhError("label edit rejected")

        def lost_reply(repo, number, add=(), remove=(), **kwargs):
            self.assertTrue(kwargs["single_attempt"])
            self.edit_labels(repo, number, add, remove)
            raise gh.GhUnsure("reply lost after removing the label")

        since = util.iso(self.time - timedelta(days=1))
        for _ in range(3):
            rejected = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
            with patch.object(gh, "issue_labels", side_effect=reject), patch.object(codebase, "STAMP", "new-engine"):
                self.assertEqual(poll.poll(), 1)
            self.assertIn(label, self.labels)
            entry, count = run_store.environment_requeue(rejected, since)
            self.assertEqual((entry["edit_pending"], entry["requeued"], count), (None, 0, 0))
        self.assertFalse(any("environment-requeue" in body for body in self.comments))

        accepted = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        with patch.object(gh, "issue_labels", side_effect=gh.GhUnsure("reply lost before removal")), \
                patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 1)
        self.assertIn(label, self.labels)
        self.assertTrue(run_store.environment_requeue(accepted, since)[0]["edit_pending"])
        with patch.object(gh, "issue_labels", side_effect=lost_reply) as edit, \
                patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 1)
            pending, _ = run_store.environment_requeue(accepted, since)
            self.assertEqual(pending["requeued"], 0)
            self.assertTrue(pending["edit_pending"])
            self.assertNotIn(label, self.labels)
            self.assertEqual(self.started, [])
            self.assertEqual(poll.poll(dry_run=True), 0)
            self.assertEqual(run_store.environment_requeue(accepted, since), (pending, 0))
            self.assertEqual(poll.poll(), 0)
            self.assertEqual(poll.poll(), 0)
            edit.assert_called_once()

        next_run = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.assertEqual(run_store.environment_requeue(next_run, since), ({}, 1))
        self.assertEqual(sum("environment-requeue" in body for body in self.comments), 1)
        self.assertIn(self.issue, self.started)

    def test_adaptive_finish_uses_the_blocking_build_result(self):
        startup = Result(ok=False, runner="claude", error="claude not found: fixture")
        recovery = pipeline.runner.start_failure(startup, "build")
        build_start = ("worker.start", "failed", {"writing": True, "event": "result", "recovery": recovery})
        unrelated = ("worker.start", "failed", {"writing": False, "event": "result", "recovery": recovery})
        for actions, expected in (
            ([build_start], {"class": "environment", **recovery}),
            ([build_start, unrelated], {"class": "environment", **recovery}),
            ([unrelated], {"class": "work"}),
            ([build_start, ("worker.continue", "failed", {"writing": True, "event": "result"})], {"class": "work"}),
            ([build_start, ("checks.run", "succeeded", {"passed": False})], {"class": "work"}),
            ([build_start, ("verification.run", "succeeded", {"operation_passed": True, "verdict": "fail"})],
             {"class": "work"}),
        ):
            with self.subTest(actions=actions):
                def drive(flow):
                    controller = orchestration.Controller(flow)
                    lease = controller.lease
                    turn = ledger.begin(lease, {}, 1)
                    version = ledger.snapshot(lease)["version"]
                    accepted = ledger.accept(lease, turn, version,
                                             {"calls": [{"op": op, "args": {}} for op, _, _ in actions]}, 10)
                    for action, (_, state, result) in zip(accepted, actions):
                        ledger.transition(lease, action["id"], state, result)
                    controller.perform({"op": "run.finish", "request": {"reason": "failed"}})
                    return controller.ending

                row = self.run_failure(script=pipeline.Script("adw", drive))
                self.assertEqual(json.loads(row["failure"]), expected)

    def test_runner_start_failure_requires_a_successful_probe(self):
        failure = Result(ok=False, runner="claude", error="claude not found: fixture")
        def drive(cfg, run, slot):
            return pipeline._fail(run, "planning step failed",
                                  recovery=pipeline.runner.start_failure(failure, "plan"))
        row = self.run_failure(drive)
        self.assertEqual(json.loads(row["failure"])["reason"], "runner-start")
        with patch.object(pipeline.runner, "started_now", side_effect=[False, True]) as probe:
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual([call.args for call in probe.call_args_list], [("claude",), ("claude",)])
        self.assertEqual(self.started, [self.issue])

    def test_rejected_login_requires_a_ready_route_with_renewed_credentials(self):
        failure = Result(ok=False, runner="claude", model="sonnet", credential="cli_login", turns=1,
                         raw={"api_error": "authentication_failed"}, auth_generation="stale-login")
        def drive(cfg, run, slot):
            return pipeline._fail(run, "the conflict resolution step failed",
                                  recovery=pipeline.runner.start_failure(failure, "build"))
        row = self.run_failure(drive)
        self.assertEqual(json.loads(row["failure"])["reason"], "runner-login")
        route = SimpleNamespace(state="ready", runner="claude", model="sonnet", credential="cli_login")
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=lambda _: [route])), \
                patch.object(pipeline.runner, "authentication", side_effect=[
                    SimpleNamespace(generation="stale-login"), SimpleNamespace(generation="new-login")]):
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_sandbox_start_checks_confinement_before_requeueing(self):
        row = self.run_failure(RuntimeError(pipeline.runner.SANDBOX_START_ERROR))
        self.assertEqual(json.loads(row["failure"])["reason"], "sandbox-start")
        with patch.object(sandbox, "can_confine", return_value=True), \
                patch.object(sandbox, "write_confinement", return_value=["sandbox-exec"]), \
                patch.object(util, "run_whole_tree", side_effect=[
                    SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as probe:
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual([call.args[0] for call in probe.call_args_list], [["sandbox-exec", "/usr/bin/true"]] * 2)
        self.assertEqual(self.started, [self.issue])


class RouteABTests(unittest.TestCase):
    def configured(self, **test):
        return config.Config(steps={"plan": {"model": "opus"}}, ab={"cheap-plan": {
            "step": "plan", "kind": "", "share": 0.5, "arm": {"model": "haiku"}, **test}})

    def sides(self, cfg, kind="feat"):
        return [steps.resolve(cfg, None, "plan", kind, ticket=("fixture", str(n))) for n in range(1, 41)]

    def test_share_decides_a_stable_side_per_ticket(self):
        cfg = self.configured()
        first, second = self.sides(cfg), self.sides(cfg)
        self.assertTrue(12 <= sum(s.ab_side == "arm" for s in first) <= 28)
        self.assertEqual([s.ab_side for s in first], [s.ab_side for s in second])
        self.assertEqual({(s.ab_side, s.model) for s in first}, {("arm", "haiku"), ("baseline", "opus")})
        self.assertEqual({s.ab_name for s in self.sides(self.configured(kind="feat"), "fix")}, {""})


if __name__ == "__main__":
    unittest.main()
