import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from arete import (
    ci, config, diffguard, factory, flows, gh, mergecheck, orchestration, owner_asks, paths, pipeline, sandbox, tracker,
    util, verify, workspace,
)
from arete.agent import NO_SESSION, Session
from arete.review import Review
from arete.store import asks as ask_store
from arete.store import db as foundation


class MovedBaseAutomergeTests(unittest.TestCase):
    class_timeout = 60

    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}))
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.base, self.origin, self.wt = root / "base", root / "origin", root / "work"
        self.git(root, "init", "-q", "-b", "main", str(self.base))
        self.commit(self.base, "shared.txt", "original\n")
        self.git(root, "clone", "-q", "--bare", str(self.base), str(self.origin))
        self.git(root, "clone", "-q", str(self.origin), str(self.wt))
        self.git(self.wt, "checkout", "-qb", "ticket")
        head = self.commit(self.wt, "shared.txt", "ticket\n")
        self.git(self.wt, "push", "-q", "origin", "ticket")
        self.c = workspace.Checkout("", "fixture/repo", self.wt, self.wt, "main",
                                    pr="https://github.com/fixture/repo/pull/7", lease=head)
        self.events, self.logs = [], []
        self.conflict, self.retargeted = False, False
        self.move_on_read = None
        self.run = SimpleNamespace(id="fixture-run", tid="7", repo=config.Repo("fixture", self.wt), repairs="",
                                   feedback=[], facts={}, log=self.logs.append, waiting=lambda _: nullcontext(),
                                   settings=lambda _: SimpleNamespace(tries=1), agent=mock.Mock(side_effect=self.agent),
                                   save=mock.Mock(), owner_note="", sessions={})
        layer = config.RepoLayer(dir=self.wt, trust_level="automerge", checks={"behavior": "check fixture"},
                                 verify=config.Verify())
        self.b = pipeline.Build(config.Config(), self.run, tracker.Ticket("7", "#7", "Edit shared text"),
                                self.wt, [self.c], layer, 60, "fixture", tools=[], deny=diffguard.denied_paths(layer))
        tree = self.tree()
        self.b.checked = {"tree": tree, "commands": layer.checks}
        self.verified = self.verification(tree)
        self.review = Review(verdict="approve", trees={"": tree})
        self.slot = mock.Mock()
        for patch in (mock.patch.object(pipeline.record, "mark"),
                      mock.patch.object(pipeline.record, "timed", side_effect=lambda *a, **kw: nullcontext({})),
                      mock.patch.object(util, "repo_lock", side_effect=lambda _: nullcontext()),
                      mock.patch.object(pipeline, "run_checks", side_effect=self.checks),
                      mock.patch.object(pipeline, "check_in_use", side_effect=self.verify_candidate),
                      mock.patch.object(pipeline.jev, "ready", return_value=True),
                      mock.patch.object(pipeline.jev, "risk", return_value=SimpleNamespace(hold="")),
                      mock.patch.object(gh, "pr_merge_details", side_effect=self.pr_details),
                      mock.patch.object(gh, "commit_checks", side_effect=self.ci_checks),
                      mock.patch.object(gh, "merge_ci_passed", return_value=True),
                      mock.patch.object(gh, "gh", side_effect=self.merge_pr),
                      mock.patch.object(gh, "pr_comment"),
                      mock.patch.object(ci, "SETTLE_SECONDS", 0)):
            self.enterContext(patch)
        self.write_ci = pipeline.publish_ci
        self.publish_ci = self.enterContext(mock.patch.object(pipeline, "publish_ci"))

    @staticmethod
    def git(root, *args):
        return subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.com",
                               "-c", "commit.gpgsign=false", *args], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, root, path, value):
        (root / path).write_text(value)
        self.git(root, "add", path)
        self.git(root, "commit", "-qm", path)
        return self.git(root, "rev-parse", "HEAD")

    def move_base(self, path="base.txt", value="base\n"):
        base = self.commit(self.base, path, value)
        self.git(self.base, "push", "-q", str(self.origin), "main")
        return base

    def remote_head(self):
        return self.git(self.origin, "rev-parse", "refs/heads/ticket")

    def tree(self):
        return self.git(self.wt, "rev-parse", "HEAD^{tree}")

    def pr_details(self, *a):
        if self.move_on_read:
            move, self.move_on_read = self.move_on_read, None
            self.move_base(*move)
        head, base = self.remote_head(), self.git(self.origin, "rev-parse", "refs/heads/main")
        behind = base != self.git(self.origin, "merge-base", head, base)
        state = ("DIRTY" if self.conflict else "BEHIND") if behind else "CLEAN"
        return {"state": "OPEN", "isDraft": False, "headRefOid": head, "baseRefOid": base,
                "baseRefName": "release" if self.retargeted else "main", "mergeStateStatus": state,
                "mergeable": "CONFLICTING" if state == "DIRTY" else "MERGEABLE"}

    def checks(self, layer, cwd, git_dirs, run, timeout, *, selected):
        self.assertIsNone(selected)
        self.assertEqual(self.remote_head(), self.c.lease)
        self.events.append(("checks", self.tree()))
        return True, "full check report"

    def verification(self, tree):
        return verify.Verified("pass", checks=[{"what": "changed text", "how": "read the file", "saw": "expected text",
                                                "passed": True}], native_trees={"": tree})

    def verify_candidate(self, b, goal, summary):
        self.assertIn("original behavioral goal", goal)
        self.assertEqual(self.remote_head(), self.c.lease)
        self.events.append(("verify", self.tree()))
        return self.verification(self.tree())

    def agent(self, step, prompt, cwd, **kw):
        if kw["role"] == "build":
            self.events.append(("resolve", self.c.base_commit))
            (cwd / "shared.txt").write_text("ticket and base\n")
            answer = {"resolved": True, "summary": "kept both"}
        else:
            self.assertEqual(kw["role"], "review")
            self.assertIn("original plan", prompt)
            self.assertEqual(self.remote_head(), self.c.lease)
            self.events.append(("review", self.tree()))
            answer = {"problems": [], "summary": "approved"}
        return SimpleNamespace(ok=True, structured=answer)

    def ci_checks(self, repo, sha):
        self.assertEqual(sha, self.remote_head())
        self.events.append(("ci", sha))
        return {"checks": gh.Check("checks", "pass")}

    def merge_pr(self, args, **kw):
        self.assertEqual(args[:4], ["api", "-X", "PUT", "repos/fixture/repo/pulls/7/merge"])
        self.assertTrue(kw["write"])
        self.assertEqual(json.loads(kw["input"])["sha"], self.remote_head())
        self.events.append(("merge", self.remote_head()))
        return '{"merged": true}'

    def automerge(self):
        return pipeline.automerge(self.b, None, "", self.verified, self.review, [self.c], "about", branch="ticket",
                                  slot=self.slot, goal="original behavioral goal", plan="original plan")

    def assert_caught_up(self, initial_head):
        head, tree = self.remote_head(), self.tree()
        self.assertNotEqual(head, initial_head, "\n".join(self.logs))
        self.assertEqual(self.c.lease, head)
        self.assertEqual(self.events[-5:], [("checks", tree), ("verify", tree), ("review", tree),
                                           ("ci", head), ("merge", head)])
        self.assertNotIn("merge_held", self.run.facts)

    def test_clean_base_move_reported_by_github_after_the_fetch_is_merged(self):
        initial_head = self.c.lease
        self.move_on_read = ("base.txt", "base\n")
        self.assertEqual(self.automerge().summary(), "1 of 1 passed")
        self.assert_caught_up(initial_head)
        self.assertEqual((self.wt / "base.txt").read_text(), "base\n")

    def test_resolvable_conflict_uses_the_resolver_and_publishes_its_tree(self):
        initial_head = self.c.lease
        self.move_on_read = ("shared.txt", "base edit\n")
        self.conflict = True
        self.automerge()
        self.assert_caught_up(initial_head)
        base = self.git(self.origin, "rev-parse", "refs/heads/main")
        self.assertEqual(self.events[0], ("resolve", base))
        self.assertEqual((self.wt / "shared.txt").read_text(), "ticket and base\n")

    def test_squash_rejection_for_a_new_conflict_enters_catch_up(self):
        initial_head = self.c.lease

        def merge(args, **kwargs):
            if self.remote_head() == initial_head:
                self.move_base("shared.txt", "base edit\n")
                self.conflict = True
                raise gh.GhError("gh api: gh: Pull Request is not mergeable (HTTP 405)")
            return self.merge_pr(args, **kwargs)

        gh.gh.side_effect = merge
        self.automerge()
        self.assert_caught_up(initial_head)
        self.assertEqual(self.events[0][0], "resolve")
        self.assertEqual(gh.gh.call_count, 2)

    def test_squash_rejection_holds_if_the_pr_was_retargeted(self):
        initial_head = self.c.lease

        def merge(*args, **kwargs):
            self.move_base()
            self.retargeted = True
            raise gh.GhError("gh api: gh: Pull Request is not mergeable (HTTP 405)")

        gh.gh.side_effect = merge
        self.automerge()
        self.assertEqual(self.remote_head(), initial_head)
        self.assertEqual(self.events, [])
        self.assertEqual(self.run.facts["merge_held"], "the PR base changed after verification")
        gh.gh.assert_called_once()

    def test_unrelated_and_ambiguous_squash_errors_do_not_retry_after_a_move(self):
        head = self.c.lease
        for error in (gh.GhError("gh api: gh: Required status check is expected (HTTP 405)"),
                      gh.GhUnsure("reply lost")):
            with self.subTest(error=error):
                def merge(*args, **kwargs):
                    self.move_base(value=f"{error}\n")
                    raise error

                gh.gh.reset_mock(side_effect=True)
                gh.gh.side_effect = merge
                gh.pr_merge_details.reset_mock()
                with mock.patch.object(gh, "merge_readiness", return_value=("", "")):
                    with self.assertRaises(type(error)):
                        gh.pr_merge_auto(self.c.github, 7, head, self.c.base, self.c.base_commit)
                gh.gh.assert_called_once()
                gh.pr_merge_details.assert_not_called()

    def test_squash_response_requires_a_definite_refusal_to_recheck_the_base(self):
        base = self.pr_details()["baseRefOid"]
        self.move_base()
        kwargs = (self.c.github, 7, self.c.lease, self.c.base, base)
        with mock.patch.object(gh, "merge_readiness", return_value=("", "")):
            gh.gh.side_effect = None
            gh.gh.return_value = '{"message": "Pull Request is not mergeable"}'
            with self.assertRaises(gh.GhUnsure):
                gh.pr_merge_auto(*kwargs)
            gh.pr_merge_details.assert_not_called()
            gh.gh.return_value = '{"merged": false, "message": "Pull Request is not mergeable"}'
            self.assertIsInstance(gh.pr_merge_auto(*kwargs), gh.BaseTipChanged)
            gh.pr_merge_details.assert_called_once_with("7", self.c.github)

    def test_caught_up_pr_body_pairs_verification_and_review_with_the_same_head_ci(self):
        initial_head = self.c.lease
        old_ci = ci.section([ci.Round(1, [ci.Watched(pipeline.ci_target(self.c),
                                                   {"old CI": gh.Check("old CI", "pass")})])], 1)
        body = ci.with_section("Closes #7\n\n## Summary\n\nOriginal summary\n\n## Checked in use\n\n"
                               "Old verification\n\n## Review\n\nOld review\n\n## Checks\n\nLocal checks", old_ci)
        self.run.tracker = mock.Mock()
        self.run.tracker.pr_link.side_effect = lambda _, whole: "Closes #7" if whole else "Refs #7"
        self.publish_ci.side_effect = self.write_ci
        self.move_base()
        with (mock.patch.object(gh, "pr_body", return_value=body),
              mock.patch.object(gh, "pr_edit_body") as edit):
            self.automerge()
        self.assert_caught_up(initial_head)
        edit.assert_called_once()
        url, updated = edit.call_args.args
        self.assertEqual((url, edit.call_args.kwargs["link"]), (self.c.pr, "Closes #7"))
        self.assertEqual(updated.count(self.c.lease[:7]), 2)
        self.assertEqual(updated.count(ci.SECTION_START), 1)
        for text in ("Original summary", "expected text", "approved", "Local checks", "Passed: `checks`"):
            self.assertIn(text, updated)
        for text in ("Old verification", "Old review", "old CI", initial_head[:7]):
            self.assertNotIn(text, updated)

    def publication_state(self):
        root = self.wt.parent
        state = root / "state"
        self.enterContext(mock.patch.multiple(paths, STATE_DIR=state, DB_FILE=state / "arete.db",
                                              LOCKS_DIR=state / "locks", EVENTS_FILE=state / "events.jsonl",
                                              WORKTREES_DIR=root / "worktrees", LOG_DIR=root / "logs",
                                              RUNS_DIR=root / "runs"))
        self.enterContext(mock.patch.object(pipeline, "checked_layer", return_value=self.b.layer))
        ended = self.enterContext(mock.patch.object(flows, "ended", return_value=("pr", "published", self.c.pr)))
        foundation.migrate()
        return ended

    def publication_flow(self, path, previous=""):
        repo = replace(self.run.repo, parts=[config.Part("", self.c.github)])
        run_id = pipeline.record.start("pipeline", repo=repo.name, issue="7")
        run = pipeline.Run(run_id, repo, paths.RUNS_DIR / run_id, cfg=self.b.cfg, layer=self.b.layer,
                           tracker=mock.Mock(), tid="7", path=path)
        run.log, run.enter, run.say = self.logs.append, mock.Mock(), mock.Mock()
        run.settings = lambda _: SimpleNamespace(tries=1)
        run.dir.mkdir()
        self.b.run = run
        job = flows.Job(self.b.cfg, run, self.wt, ticket=self.b.ticket, branch="ticket", checkouts=[self.c],
                        layer=self.b.layer, goal="original behavioral goal", plan_text="original plan",
                        built=None if previous else self.b, verified=self.verified, review=self.review, slot=self.slot,
                        opened=[self.c.github], changed=[self.c.github])
        return factory.Flow(run_id, job, continues=previous)

    def publication_controller(self, previous=""):
        ledger = orchestration.ledger
        found = orchestration.Controller.__new__(orchestration.Controller)
        found.flow = self.publication_flow("orchestrate", previous)
        found.job, found.run = found.flow.ctx, found.flow.ctx.run
        found.workers, found.catalog, found.ending = {}, mock.Mock(), None
        found.policy = config.Orchestration()
        found.catalog.context = {"task": "Edit shared text"}
        found.lineage = lambda: {}
        found.lease = ledger.acquire(found.run.id, previous, {"acceptance": "fixture"})
        found.data = ledger.snapshot(found.lease)["data"]
        return found

    def publication_action(self, first):
        ledger = orchestration.ledger
        first.data.update(run_settings="fixture", overrides={}, step_overrides={})
        first.data["prepared"] = {"layer": str(self.wt), "branch": "ticket", "checkouts": [self.c.to_row()],
                                  "ticket": asdict(self.b.ticket), "attachments": "", "triage": "", "expertise": "",
                                  "add_dirs": []}
        first.data["publication"] = {"heads": {self.c.github: self.c.lease}, "changed": [self.c.github]}
        turn = ledger.begin(first.lease, {}, 1)
        version = ledger.snapshot(first.lease)["version"]
        action = ledger.accept(first.lease, turn, version,
                               {"calls": [{"op": "publication.request", "args": {}}]}, 1)[0]
        first.perform = lambda _: first.finish_publication()
        return action

    def test_answer_resumes_the_same_publication_and_keeps_the_two_catch_up_limit(self):
        ledger = orchestration.ledger
        self.publication_state()
        first = self.publication_controller()
        action = self.publication_action(first)
        question = owner_asks.text_ask("How should we combine the two edits?")
        bases = [self.move_base()]

        def after_ci(*args):
            if len(bases) == 1:
                self.conflict = True
                bases.append(self.move_base("shared.txt", "base edit\n"))
            else:
                bases.append(self.move_base(value="third base\n"))

        def resolve(run, step, prompt, cwd, role, **kwargs):
            if step == "resolve":
                if run is first.run:
                    run.owner_ask(step, [question], Session(id="aborted-session"))
                self.assertIn("Keep both edits", prompt)
                self.assertEqual(kwargs["resume"], NO_SESSION)
            result = self.agent(step, prompt, cwd, role=role, **kwargs)
            result.record = ""
            return result

        self.publish_ci.side_effect = after_ci
        with mock.patch.object(pipeline.AgentRun, "agent", resolve):
            with self.assertRaises(pipeline.CatchUpAsked) as raised:
                first.dispatch(action)
            asked = raised.exception
            caught_up_head = self.remote_head()
            self.assertEqual(asked.pause.attempt, 2)
            self.assertEqual(workspace.uncommitted(self.wt), [])
            self.assertFalse(ledger.publication_pending(first.run.id))
            self.assertEqual(flows.authority(first.run.id, self.c, ""), (caught_up_head, True))
            ledger.save(first.lease, {**first.data, "publication_intent": {"phase": "publish"}})
            self.assertIn("publication was interrupted", flows.guard(first.job, first.run.id))
            ledger.save(first.lease, first.data)
            ask_id = asked.ask_ids[0]
            ask_store.ask_add(ask_id, first.run.id, first.run.repo.name, "7", asked.step, asked.call,
                              question["why"], question["tried"], question["questions"], {})
            with self.assertRaisesRegex(factory.Refused, "owner answers"):
                first.recover()
            self.assertTrue(ask_store.ask_close(ask_id, "answered", [{"text": "Keep both edits"}]))
            pipeline.record.settle(first.run.id, "needs-info")
            second = self.publication_controller(first.run.id)
            second.job.continues = first.run.id
            second.run.answers_due = ask_store.asks_due([first.run.id])
            second.flow.refusal = flows.guard(second.job, first.run.id)
            self.assertEqual(second.prepare()[0], "pr")
        self.assertEqual(ask_store.ask_get(ask_id)["taken_by"], second.run.id)
        self.assertIn("Keep both edits", second.job.goal)
        self.assertEqual(second.run.facts["merge_held"], f"the base `main` moved to `{bases[-1]}` after 2 catch-ups")
        self.assertEqual([event for event, _ in self.events], ["checks", "verify", "review", "ci", "resolve",
                                                            "checks", "verify", "review", "ci"])
        self.assertEqual(ledger.snapshot(second.lease)["turns"], 1)
        self.assertEqual([a["id"] for a in ledger.actions(second.lease)], [action["id"]])
        self.assertFalse(ledger.publication_pending(second.run.id))

    def test_orchestrated_catch_up_routes_red_ci_to_diagnosis_and_drafts_the_pr(self):
        ended = self.publication_state()
        controller = self.publication_controller()
        action = self.publication_action(controller)
        initial_head = self.c.lease
        base = self.move_base()
        failure = {"checks": gh.Check("checks", "fail")}
        gh.commit_checks.side_effect = [failure, {}]
        with (mock.patch.object(gh, "branch_sha", return_value=base),
              mock.patch.object(gh, "pr_draft_and_head", side_effect=lambda _: (False, self.remote_head())),
              mock.patch.object(gh, "pr_set_draft") as draft,
              mock.patch.object(pipeline.AgentRun, "agent", autospec=True, side_effect=self.agent_result)):
            controller.dispatch(action)

        head = self.remote_head()
        self.assertNotEqual(head, initial_head)
        self.assertEqual(gh.commit_checks.call_args_list, [mock.call(self.c.github, head), mock.call(self.c.github, base)])
        self.assertIn("Failed: `checks`", self.publish_ci.call_args.args[3])
        draft.assert_called_once_with(self.c.pr, True)
        gh.gh.assert_not_called()
        ended.assert_not_called()
        controller.run.say.assert_not_called()
        self.slot.take.assert_called_with(controller.run.log, controller.run.waiting)
        saved = orchestration.ledger.snapshot(controller.lease)["data"]
        self.assertTrue(saved["ci_failed"])
        self.assertEqual(saved["catch_ups"], 1)
        self.assertEqual(saved["publication"]["heads"][self.c.github], head)
        self.assertNotIn("publication_intent", saved)
        self.assertNotIn("published", saved)
        row, = orchestration.ledger.actions(controller.lease)
        self.assertEqual(row["state"], "failed")
        self.assertIn("CI needs an explicit worker diagnosis or repair", row["result"]["error"])

    def agent_result(self, run, step, prompt, cwd, role, **kwargs):
        result = self.agent(step, prompt, cwd, role=role, **kwargs)
        result.record = ""
        return result

    def ask_event(self, question):
        return [{"kind": "tool_call", "name": owner_asks.TOOL, "input": owner_asks.text_ask(question)}]

    def test_orchestrated_catch_up_reviewer_question_becomes_a_moved_base_hold(self):
        self.publication_state()
        controller = self.publication_controller()
        action = self.publication_action(controller)
        initial_head = self.c.lease
        base = self.move_base()
        ask = SimpleNamespace(record="review.jsonl", session=NO_SESSION)
        with (mock.patch.object(pipeline.AgentRun, "agent", autospec=True, return_value=ask) as agent,
              mock.patch.object(pipeline.record, "events", return_value=self.ask_event("What should pass review?"))):
            controller.dispatch(action)

        self.assertEqual(agent.call_args.args[1], "automerge-review1")
        self.assertEqual(self.remote_head(), initial_head)
        hold = f"could not catch up to `main` at `{base}`: review needs owner input"
        self.assertEqual(controller.run.facts["merge_held"], hold)
        self.assertIn(hold, gh.pr_comment.call_args.args[1])
        self.assertNotIn("What should pass review?", controller.run.say.call_args.args[0])
        row, = orchestration.ledger.actions(controller.lease)
        self.assertEqual(row["state"], "succeeded")
        self.assertNotIn("awaiting_owner", row["result"])
        self.assertFalse(orchestration.ledger.publication_pending(controller.run.id))

    def test_classic_adw_resolver_question_on_catch_up_two_holds_after_catch_up_one_pushed(self):
        self.publication_state()
        flow = self.publication_flow("feature")
        initial_head = self.c.lease
        first_base = self.move_base()
        caught_up = []

        def after_ci(*args):
            caught_up.append((self.remote_head(), self.move_base("shared.txt", "base edit\n")))

        def agent(run, step, prompt, cwd, role, **kwargs):
            if step == "resolve":
                return SimpleNamespace(record="resolve.jsonl", session=NO_SESSION)
            return self.agent_result(run, step, prompt, cwd, role, **kwargs)

        self.publish_ci.side_effect = after_ci
        with (mock.patch.object(pipeline.AgentRun, "agent", autospec=True, side_effect=agent) as calls,
              mock.patch.object(pipeline.record, "events", return_value=self.ask_event("How should both edits fit?"))):
            outcome = flow.step("publish")

        head, moved = caught_up[0]
        self.assertTrue(outcome.passed)
        self.assertNotEqual(head, initial_head)
        self.assertEqual(self.remote_head(), head)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD^2"), first_base)
        self.assertEqual([call.args[1] for call in calls.call_args_list], ["automerge-review1", "resolve"])
        self.assertEqual(workspace.uncommitted(self.wt), [])
        hold = f"could not catch up to `main` at `{moved}`: the conflict resolver needs owner input"
        self.assertEqual(flow.ctx.run.facts["merge_held"], hold)
        self.assertIn(hold, gh.pr_comment.call_args.args[1])
        self.assertNotIn("How should both edits fit?", flow.ctx.run.say.call_args.args[0])
        gh.gh.assert_not_called()

    def test_protected_conflict_stops_before_resolution_and_names_the_base(self):
        initial_head = self.c.lease
        base = self.move_base("shared.txt", "base edit\n")
        self.b.layer.deny.append("shared.txt")
        self.automerge()
        self.assertEqual(self.remote_head(), initial_head)
        self.assertEqual(self.events, [])
        self.assertEqual(workspace.uncommitted(self.wt), [])
        hold = self.run.facts["merge_held"]
        self.assertIn(base, hold)
        self.assertIn("protected paths", hold)
        self.assertIn(hold, gh.pr_comment.call_args.args[1])

    def test_two_catch_ups_then_a_hold_names_the_latest_base(self):
        bases = [self.move_base(value="one\n")]

        def advance_after_ci(*args):
            bases.append(self.move_base(value=f"move {len(bases)}\n"))

        self.publish_ci.side_effect = advance_after_ci
        self.automerge()
        self.assertEqual([event for event, _ in self.events], ["checks", "verify", "review", "ci"] * 2)
        self.assertEqual((self.wt / "base.txt").read_text(), "move 1\n")
        self.assertEqual(self.run.facts["merge_held"], f"the base `main` moved to `{bases[-1]}` after 2 catch-ups")

    def test_stale_native_pass_prevents_the_catch_up_push(self):
        initial_head = self.c.lease
        self.move_base()
        with mock.patch.object(pipeline, "check_in_use", return_value=self.verified):
            self.automerge()
        self.assertEqual(self.remote_head(), initial_head)
        self.assertIn("no native behavioral pass", self.run.facts["merge_held"])

    def test_verifier_owner_question_holds_for_the_moved_base(self):
        initial_head = self.c.lease
        base = self.move_base()
        question = owner_asks.text_ask("What should the expected behavior be?")
        asked = owner_asks.Asked([question], ["fixture-a1"], "publish", "verify2", NO_SESSION)
        with mock.patch.object(pipeline, "check_in_use", side_effect=asked) as verifier:
            self.automerge()
        verifier.assert_called_once()
        self.assertEqual(self.remote_head(), initial_head)
        self.assertEqual([event for event, _ in self.events], ["checks"])
        hold = f"could not catch up to `main` at `{base}`: behavioral verification needs owner input"
        self.assertEqual(self.run.facts["merge_held"], hold)
        self.assertIn(hold, gh.pr_comment.call_args.args[1])

    def test_stale_approval_prevents_the_catch_up_push(self):
        initial_head = self.c.lease
        self.move_base()
        with mock.patch.object(pipeline, "_review", return_value=self.review):
            self.automerge()
        self.assertEqual(self.remote_head(), initial_head)
        self.assertIn("lacks a current approving review", self.run.facts["merge_held"])

    def test_stale_local_checks_prevent_the_catch_up_push(self):
        initial_head = self.c.lease
        self.move_base()
        with mock.patch.object(self.b, "check", return_value=True):
            self.automerge()
        self.assertEqual(self.remote_head(), initial_head)
        self.assertIn("lacks the full local check result", self.run.facts["merge_held"])

    def test_retargeted_pr_keeps_its_unrelated_hold_even_when_main_moved(self):
        initial_head = self.c.lease
        self.move_base()
        self.retargeted = True
        self.automerge()
        self.assertEqual(self.remote_head(), initial_head)
        self.assertEqual(self.events, [])
        self.assertEqual(self.run.facts["merge_held"], "the PR base changed after verification")


class ConflictCiWatchTests(unittest.TestCase):
    def test_first_poll_conflict_reads_checks_once_without_sleeping(self):
        target = ci.Target("fixture/repo", "https://github.com/fixture/repo/pull/7", "fixture#7", "head", "main")
        for mergeability in ({"mergeable": "CONFLICTING"}, {"mergeStateStatus": "DIRTY"}):
            logs = []
            with (self.subTest(mergeability=mergeability),
                  mock.patch.object(gh, "pr_merge_details", return_value={"state": "OPEN", "headRefOid": "head",
                                                                         **mergeability}) as details,
                  mock.patch.object(gh, "commit_checks", return_value={}) as checks,
                  mock.patch.object(ci.time, "sleep") as sleep):
                watch = ci.watch(target, logs.append)
                self.assertTrue(watch.conflicting)
                self.assertTrue(watch.read)
                self.assertEqual(watch.checks, {})
                self.assertIn("merge conflict", "\n".join(logs))
                self.assertIn("merge conflict", ci.Round(1, [watch]).summary())
                details.assert_called_once_with(target.pr)
                checks.assert_called_once_with(target.github, target.sha)
                sleep.assert_not_called()

    def test_orchestrated_publication_drafts_when_a_later_conflict_poll_finds_a_failure(self):
        checkout = SimpleNamespace(github="fixture/repo", pr="https://github.com/fixture/repo/pull/7",
                                   pr_name="fixture#7", lease="published-head", base="main")
        controller = orchestration.Controller.__new__(orchestration.Controller)
        controller.run = mock.Mock(dry_run=False, tid="7", repo=SimpleNamespace(parts=[checkout]),
                                   waiting=mock.Mock(return_value=nullcontext()))
        controller.job = mock.Mock(with_prs=[checkout], picked=mock.Mock(return_value=[checkout]),
                                   verified=verify.Verified("pass"))
        controller.data = {"evidence": {"checks": {"result": {"full": True}}}}
        controller.workers = {}
        controller.current = mock.Mock(return_value=True)
        controller.fingerprint = mock.Mock(return_value="tree")
        controller.persist = mock.Mock()
        controller.atomic = mock.Mock(return_value=factory.Outcome("pass"))
        controller.finish_publication = mock.Mock()
        conflict = {"state": "OPEN", "headRefOid": checkout.lease, "mergeable": "CONFLICTING"}
        clean = {**conflict, "mergeable": "MERGEABLE"}
        failed = {"failing": gh.Check("failing", "fail")}
        with (mock.patch.object(gh, "pr_merge_details", side_effect=[clean, conflict]) as details,
              mock.patch.object(gh, "commit_checks", side_effect=[{}, failed, {}]) as checks,
              mock.patch.object(gh, "branch_sha", return_value="base-sha"),
              mock.patch.object(gh, "pr_draft_and_head", return_value=(False, checkout.lease)),
              mock.patch.object(pipeline, "publish_ci") as publish_ci,
              mock.patch.object(gh, "pr_set_draft") as draft,
              mock.patch.object(ci.time, "sleep") as sleep,
              mock.patch.object(ci.time, "monotonic", return_value=0)):
            result = controller.publish()
        self.assertEqual(details.call_args_list, [mock.call(checkout.pr)] * 2)
        self.assertEqual(checks.call_args_list, [mock.call(checkout.github, checkout.lease)] * 2 + [
                                                 mock.call(checkout.github, "base-sha")])
        sleep.assert_called_once_with(ci.POLL_SECONDS)
        self.assertTrue(controller.data["ci_failed"])
        self.assertIn("CI needs an explicit worker diagnosis", result["error"])
        self.assertIn("Failed: `failing`", publish_ci.call_args.args[3])
        draft.assert_called_once_with(checkout.pr, True)
        controller.finish_publication.assert_not_called()

    def test_first_poll_conflict_reports_an_unread_check_result(self):
        target = ci.Target("fixture/repo", "https://github.com/fixture/repo/pull/7", "fixture#7", "head", "main")
        conflict = {"state": "OPEN", "headRefOid": target.sha, "mergeable": "CONFLICTING"}
        logs = []
        with (mock.patch.object(gh, "pr_merge_details", return_value=conflict) as details,
              mock.patch.object(gh, "commit_checks", side_effect=gh.GhError("unavailable")) as checks,
              mock.patch.object(ci.time, "sleep") as sleep):
            watch = ci.watch(target, logs.append)
        details.assert_called_once_with(target.pr)
        checks.assert_called_once_with(target.github, target.sha)
        sleep.assert_not_called()
        self.assertTrue(watch.conflicting)
        self.assertFalse(ci.Round(1, [watch]).all_read)
        self.assertIn("could not read the checks", "\n".join(watch.markdown()))
        self.assertIn("merge conflict", "\n".join(watch.markdown()))
        self.assertNotIn("trying again", "\n".join(logs))

    def test_conflict_on_another_head_does_not_stop_the_wait(self):
        target = ci.Target("fixture/repo", "https://github.com/fixture/repo/pull/7", "fixture#7", "head", "main")
        passed = {"check": gh.Check("check", "pass")}
        with (mock.patch.object(gh, "pr_merge_details", return_value={"state": "OPEN", "headRefOid": "new-head",
                                                                     "mergeable": "CONFLICTING"}),
              mock.patch.object(gh, "commit_checks", return_value=passed),
              mock.patch.object(ci, "SETTLE_SECONDS", 0)):
            result = ci.watch(target, mock.Mock())
        self.assertFalse(result.conflicting)
        self.assertEqual(result.checks, passed)

    def test_same_head_conflict_preserves_a_failure_through_a_missing_or_unread_refresh(self):
        target = ci.Target("fixture/repo", "https://github.com/fixture/repo/pull/7", "fixture#7", "head", "main")
        clean = {"state": "OPEN", "headRefOid": target.sha, "mergeable": "MERGEABLE"}
        conflict = {**clean, "mergeable": "CONFLICTING"}
        failed = gh.Check("failing", "fail")
        retry = {"failing": gh.Check("failing", "pending")}
        for refresh in ({}, retry, gh.GhError("unavailable")):
            with (self.subTest(refresh=refresh),
                  mock.patch.object(gh, "pr_merge_details", side_effect=[clean, clean, conflict]) as details,
                  mock.patch.object(gh, "commit_checks", side_effect=[{"failing": failed}, retry, refresh, {}]) as checks,
                  mock.patch.object(gh, "branch_sha", return_value="base"),
                  mock.patch.object(ci.time, "sleep") as sleep,
                  mock.patch.object(ci.time, "monotonic", return_value=target.pushed_at)):
                log = mock.Mock()
                result = ci.compare_with_base(ci.watch(target, log), log)
            self.assertTrue(result.conflicting)
            self.assertEqual(result.read, not isinstance(refresh, gh.GhError))
            self.assertEqual([r.check for r in ci.Round(1, [result]).reds], [failed])
            self.assertIn("merge conflict", "\n".join(result.markdown()))
            self.assertIn("Failed: `failing`", "\n".join(result.markdown()))
            self.assertEqual(checks.call_args_list, [mock.call(target.github, "head")] * 3 + [mock.call(target.github, "base")])
            self.assertEqual(details.call_count, 3)
            self.assertEqual(sleep.call_count, 2)


class AskConfigTests(unittest.TestCase):
    def test_expire_days_is_a_finite_number_of_at_most_ten_years(self):
        for entry in ("expire_days = inf", "expire_days = nan", "expire_days = 999999", "expire_days = 0",
                      "expire_days = true", 'expire_days = "7"'):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                config.ask_table(tomllib.loads(entry))
        self.assertEqual(config.ask_table({}).expire_days, 7)
        self.assertEqual(config.ask_table(tomllib.loads("expire_days = 0.5")).expire_days, 0.5)
        longest = config.ask_table({"expire_days": config.MAX_ASK_EXPIRE_DAYS}).expire_days
        self.assertTrue(util.iso(util.now() - timedelta(days=longest)))


class JourneyVerificationTests(unittest.TestCase):
    def setUp(self):
        self.example = Path(__file__).resolve().parents[1] / "docs/examples/product-verification"
        self.ticket = tracker.Ticket("1", "#1", "Check journey:verifier-prompt-inputs")
        self.run = mock.Mock(tid=1, repo=config.Repo("arete", self.example), dir=self.example,
                             add_dirs=[], owner_note="")
        self.run.time_for.side_effect = lambda seconds: seconds
        self.run.agent.return_value = SimpleNamespace(ok=True, structured={
            "verdict": "pass", "summary": "The configured journey passed.", "checks": [],
        })
        self.work = pipeline.TicketWork(config.Config(), self.run, self.ticket, self.example, [],
                                        config.load_layer(self.example), 60, "test workspace")
        self.scratch = self.enterContext(mock.patch.object(verify.scratch, "make"))
        self.scratch.return_value = SimpleNamespace(root=str(self.example / "scratch"))
        self.enterContext(mock.patch.object(verify.scratch, "end", return_value=("", [], [])))
        self.enterContext(mock.patch.object(pipeline, "run_folder", return_value=self.example / "evidence"))
        self.enterContext(mock.patch.object(pipeline, "restore_worktrees"))

    def verify(self):
        return pipeline.use_product(self.work, "verify", "verify", goal="Check the CLI output.", summary="",
                                    diff="")

    def passing_driver(self):
        process = mock.Mock(returncode=0, communicate=mock.Mock(return_value=("Driver observed valid prompt", "")))
        self.enterContext(mock.patch.object(verify.scratch, "service_command", return_value=["driver"]))
        self.enterContext(mock.patch.object(verify.scratch, "spawn", return_value=process))
        self.enterContext(mock.patch.object(util, "stop_group"))

    def test_passing_driver_cannot_supply_missing_verifier_checks(self):
        self.ticket.body = "Also inspect the catalog."
        self.work.layer.trust_level = "automerge"
        self.run.agent.return_value.runner = "codex"
        self.enterContext(mock.patch.object(pipeline, "verification_trees", return_value={"": "candidate"}))
        self.passing_driver()

        result = self.verify()

        self.assertEqual(result.verdict, "blocked")
        self.assertEqual(result.blocked_by, "the verifier said pass but listed no checks")
        self.assertEqual(result.native_trees, {})
        self.assertIn("Driver observed valid prompt", self.run.save.call_args.args[1])

    def test_merge_check_replays_numbered_verifier_step_without_the_driver(self):
        self.passing_driver()
        self.run.agent.return_value.structured["checks"] = [
            {"what": "1. Inspect the catalog", "how": "echo catalog", "saw": "catalog", "passed": True},
            {"what": "journey:verifier-prompt-inputs", "how": "echo prompt", "saw": "prompt", "passed": True},
        ]

        result = self.verify()
        self.assertEqual(result.verdict, "pass")
        report = self.run.save.call_args.args[1]
        self.assertIn(self.work.layer.verify.drivers["prompt-inputs"], report)
        self.assertIn("Driver observed valid prompt", report)
        with tempfile.NamedTemporaryFile(suffix=".md") as saved:
            path = Path(saved.name)
            path.write_text(report)
            with mock.patch.object(mergecheck, "reports", return_value=("build", path)):
                steps = mergecheck.report_steps({"run_id": "build", "pr": 646})

        self.assertEqual([step.goal(n) for n, step in enumerate(steps, 1)], [
            "1. [build step 1] Inspect the catalog: echo catalog",
            "2. [build step 2] journey:verifier-prompt-inputs: echo prompt",
        ])

    def test_linked_map_cannot_redirect_an_owner_configured_binding(self):
        with (tempfile.NamedTemporaryFile(suffix=".toml") as source,
              tempfile.NamedTemporaryFile(suffix=".toml") as linked):
            config_file, map_file = Path(source.name), Path(linked.name)
            configured = (self.example / "config.toml").read_text().replace("feature-map.toml", map_file.name)
            config_file.write_text(configured + '\nother = "echo other"\n')
            map_file.write_text((self.example / "feature-map.toml").read_text())
            journey = config.load_layer(self.example, source_file=config_file).verify.feature_map["verifier-prompt-inputs"]
            self.assertEqual(journey.driver, "prompt-inputs")

            with map_file.open("a") as stream:
                stream.write('driver = "other"\n')
            with self.assertRaisesRegex(ValueError, r"bind drivers in \[verify\.journey_drivers\]"):
                config.load_layer(self.example, source_file=config_file)

    def test_orchestrated_plan_worker_sees_the_configured_journey(self):
        checkout = workspace.Checkout("", "arete", self.example, self.example, "main")
        job = flows.Job(self.work.cfg, self.run, self.example, ticket=self.ticket, layer=self.work.layer,
                        checkouts=[checkout])
        controller = orchestration.Controller.__new__(orchestration.Controller)
        controller.job, controller.run, controller.lease = job, self.run, "fixture"
        controller.workers, controller.data, controller.policy = {}, {}, config.Orchestration()
        controller.catalog = mock.Mock()
        setting = SimpleNamespace(step="plan", tools=("Read",), runner="codex", model="fixture")
        controller.catalog.worker.return_value = setting, "plan", False
        args = {"depends_on": [], "tool_bundle": "read", "context_refs": [], "goal": "Plan", "inputs": {}}
        with (mock.patch.object(orchestration.ledger, "actions", return_value=[]),
              mock.patch.object(orchestration.ledger, "transition"),
              mock.patch.object(orchestration.delegation, "Worker"),
              mock.patch.object(orchestration.delegation, "request") as request):
            controller.worker_start({"id": "plan-1"}, args)

        prompt = request.call_args.args[2]
        self.assertIn("## Product feature map\n", prompt)
        self.assertIn("`journey:verifier-prompt-inputs` (configured driver available)", prompt)
        self.assertIn("Read the verification prompt's JSON record.", prompt)

    def test_stored_evidence_requires_the_same_ticket_selection_with_a_map(self):
        job = flows.Job(self.work.cfg, self.run, self.example, ticket=self.ticket, layer=self.work.layer,
                        goal="Check the CLI output.")
        passing = factory.Outcome("pass", data={"policy": job.policy("verify", {})})
        self.ticket.body = "Repeat journey:verifier-prompt-inputs."
        self.assertTrue(job.accepts("verify", passing, {}))
        self.ticket.title, self.ticket.body = "Check prompt inputs", ""
        self.assertFalse(job.accepts("verify", passing, {}))

        job.layer.verify = config.Verify(url="")
        passing.data["policy"] = job.policy("verify", {})
        self.ticket.body = "Check journey:verifier-prompt-inputs."
        self.assertTrue(job.accepts("verify", passing, {}))

    def test_requested_driver_runs_after_probes_in_journey_order_and_reports_observations(self):
        self.ticket.body = "Also check journey:another-journey and journey:verifier-prompt-inputs."
        self.run.agent.return_value.structured["checks"] = [
            {"what": "Inspect the catalog", "how": "echo catalog", "saw": "catalog", "passed": True},
        ]
        configured = self.work.layer.verify
        configured.feature_map["another-journey"] = replace(configured.feature_map["verifier-prompt-inputs"],
                                                           driver="another-driver")
        configured.drivers["another-driver"] = "echo {port} {scratch}"
        self.enterContext(mock.patch.object(verify.browser, "free_port", return_value=4242))
        configured.probes["ready"] = "echo ready"
        spawn = self.enterContext(mock.patch.object(verify.scratch, "spawn"))
        spawn.side_effect = [mock.Mock(returncode=0, communicate=mock.Mock(return_value=(out, "")))
                             for out in ("ready", "first output", "second output")]
        confined = self.enterContext(mock.patch.object(verify.scratch, "service_command", side_effect=lambda _, cmd: [cmd]))
        self.enterContext(mock.patch.object(util, "stop_group"))

        result = self.verify()

        self.assertEqual(result.verdict, "pass")
        self.assertEqual([c["what"] for c in result.checks], [
            "ready", "journey:another-journey", "journey:verifier-prompt-inputs", "Inspect the catalog",
        ])
        self.assertEqual(result.checks[2]["saw"], "exit 0: second output")
        self.assertEqual([call.args[1] for call in confined.call_args_list],
                         ["echo ready", f"echo 4242 {self.scratch.return_value.root}",
                          configured.drivers["prompt-inputs"]])
        self.assertTrue(all(call.kwargs["actor_confined"] for call in spawn.call_args_list))
        self.assertIn("Expected outcome", self.run.agent.call_args.args[1])
        self.assertIn("exit 0: second output", self.run.agent.call_args.args[1])
        self.assertIn("exit 0: second output", self.run.save.call_args.args[1])

    def test_assertion_failure_and_unavailable_command_have_distinct_results(self):
        self.enterContext(mock.patch.object(verify.scratch, "service_command", return_value=["driver"]))
        spawn = self.enterContext(mock.patch.object(verify.scratch, "spawn"))
        self.enterContext(mock.patch.object(util, "stop_group"))
        for code, stdout, stderr, verdict in ((1, "", "unexpected product response: EPERM", "fail"),
                                               (1, "", "sandbox-exec: confinement unavailable", "blocked"),
                                               (127, "", "driver: command not found", "blocked")):
            with self.subTest(code=code, stderr=stderr):
                spawn.return_value = mock.Mock(returncode=code, communicate=mock.Mock(return_value=(stdout, stderr)))

                result = self.verify()

                self.assertEqual(result.verdict, verdict)
                self.assertEqual(result.finished, verdict == "fail")
                self.assertIn("journey:verifier-prompt-inputs", result.summary)
                self.assertEqual(result.checks[0]["saw"], f"exit {code}: {stdout or stderr}")
        self.run.agent.assert_not_called()

    def test_driver_timeout_reports_the_journey_and_stops_the_process_group(self):
        self.enterContext(mock.patch.object(verify.scratch, "service_command", return_value=["driver"]))
        process = mock.Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired("driver", 1)
        self.enterContext(mock.patch.object(verify.scratch, "spawn", return_value=process))
        stop = self.enterContext(mock.patch.object(util, "stop_group"))

        result = self.verify()

        self.assertEqual(result.verdict, "blocked")
        self.assertIn("journey:verifier-prompt-inputs", result.blocked_by)
        self.assertIn("Timed out waiting for the driver", result.checks[0]["saw"])
        stop.assert_called_once_with(process)
        self.run.agent.assert_not_called()

    def test_driver_requires_trusted_scratch(self):
        self.scratch.side_effect = sandbox.ConfinementUnavailable("fixture authority unavailable")
        with mock.patch.object(verify, "run_probes") as commands:
            result = self.verify()
        self.assertEqual(result.verdict, "blocked")
        self.assertEqual(result.blocked_by, "fixture authority unavailable")
        self.assertIn("journey:verifier-prompt-inputs", result.summary)
        commands.assert_not_called()
        self.run.agent.assert_not_called()

    def test_map_guides_the_verifier_without_scheduling_an_unrequested_driver(self):
        self.ticket.title = "Check the verifier prompt inputs"
        self.run.agent.return_value.structured["checks"] = [
            {"what": "Verify prompt inputs", "how": "Open the prompt listing", "saw": "Valid prompt", "passed": True},
        ]
        with mock.patch.object(verify, "run_probes") as commands:
            result = self.verify()
        self.assertEqual(result.verdict, "pass")
        self.assertIn("journey:verifier-prompt-inputs", self.run.agent.call_args.args[1])
        commands.assert_not_called()

    def test_unknown_reference_blocks_only_when_map_is_configured(self):
        self.ticket.title = "Check journey:missing"
        result = self.verify()
        self.assertEqual(result.verdict, "blocked")
        self.assertEqual(result.blocked_by, "journey:missing")
        self.scratch.assert_not_called()
        self.run.agent.assert_not_called()

        self.work.layer.verify = config.Verify(url="")
        self.scratch.side_effect = OSError("fixture authority unavailable")
        self.run.agent.return_value.structured["checks"] = [
            {"what": "Ordinary CLI check", "how": "echo ready", "saw": "ready", "passed": True},
        ]
        with mock.patch.object(verify, "run_probes") as commands:
            result = self.verify()
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.checks[0]["what"], "Ordinary CLI check")
        self.assertNotIn("Product feature map", self.run.agent.call_args.args[1])
        commands.assert_not_called()


class ReviewTests(unittest.TestCase):
    BLOCKER: ClassVar[dict] = {"file": "notes.txt", "problem": "fixture blocker", "severity": "blocker", "category": "issue"}

    def test_security_blocker_cannot_be_waived(self):
        blocker = {**self.BLOCKER, "category": "security"}
        result = Review.judged({"summary": "Waived.", "problems": [],
                                "earlier": [{"id": "B1", "status": "waived", "why": "out of scope"}]},
                               [blocker], [], {})
        self.assertFalse(result.approved())
        self.assertEqual(result.blocking()[0]["status"], "open")

    def test_unanswered_earlier_blocker_stays_open(self):
        result = Review.judged({"summary": "Fine.", "problems": []}, [self.BLOCKER], [], {})
        self.assertFalse(result.approved())
        self.assertEqual(result.blocking()[0]["id"], "B1")

    def test_severity_outside_the_scale_still_blocks(self):
        result = Review.judged({"summary": "Fine.", "problems": [{**self.BLOCKER, "severity": "Critical"}]}, [], [], {})
        self.assertFalse(result.approved())
        self.assertEqual(len(result.blocking()), 1)

    def test_review_answer_without_a_problems_list_does_not_approve(self):
        build = SimpleNamespace(run=mock.Mock(), brief=mock.Mock(), report="", checkouts=[],
                                staged=SimpleNamespace(patch=""), ws=None, read_bash=[])
        build.run.agent.return_value = SimpleNamespace(ok=True, structured={"verdict": "approve", "summary": "Fine."})
        with mock.patch.object(pipeline, "compare_with_fork", return_value=""):
            result = pipeline._review(build, "", mock.Mock())
        self.assertFalse(result.approved())
        self.assertTrue(result.failed)
