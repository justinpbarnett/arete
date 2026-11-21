import json
import subprocess
import tomllib
import unittest
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from arete import config, diffguard, evals, gh, jev, miner, pipeline, util, verify
from arete.agent import Result
from arete.review import Review


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


class MinerLifecycleTests(unittest.TestCase):
    BASE = "a" * 40
    HEAD = "b" * 40
    UPDATED = "c" * 40
    MERGED = "d" * 40
    BRANCH = "arete/miner-20261120"
    URL = "https://github.com/example/arete/pull/8"
    PROMPT = "arete/prompts/build.md"

    def setUp(self):
        self.repo = config.Repo("arete", Path("."), github="example/arete")
        self.cfg = config.Config(repos=[self.repo])
        self.url = self.URL
        self.base_name = "main"
        self.base = self.BASE
        self.head = self.HEAD
        self.checkout = self.BASE
        self.status = "success"
        self.reported_ci_head = ""
        self.behind = False
        self.merged = False
        self.merge_visible = True
        self.merge_visible_after = 0
        self.merge_fetches = 0
        self.calls = []
        self.prs = []
        self.stored = []
        self.readiness = []
        self.elapsed = 0.0
        self.green_at = 0.0
        self.root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.enterContext(mock.patch.multiple(
            gh, gh_json=self.github_read, gh=self.github_write, me=lambda: "operator",
            pr_comment=lambda url, body: self.calls.append(("comment", url, body)),
        ))
        self.enterContext(mock.patch.object(miner.time, "monotonic", side_effect=lambda: self.elapsed))
        self.sleep = self.enterContext(mock.patch.object(miner.time, "sleep", side_effect=self.advance))
        self.git_mock = self.enterContext(mock.patch.object(miner, "git", side_effect=self.git))
        self.enterContext(mock.patch.object(miner.paths, "RUNS_DIR", self.root / "runs"))
        self.enterContext(mock.patch.object(miner.paths, "prompt_merges_dir", return_value=self.root / "pending"))
        self.enterContext(mock.patch.object(util, "repo_lock", return_value=nullcontext()))
        self.enterContext(mock.patch.object(util, "git_records", return_value=["M", self.PROMPT]))
        self.diff = self.enterContext(mock.patch.object(diffguard, "between", return_value=diffguard.Diff({self.PROMPT: 2})))
        self.compare_prompts = evals.compare_prompts
        self.eval = self.enterContext(mock.patch.object(evals, "compare_prompts",
                                                        return_value=evals.Comparison("eval table", complete=True)))
        self.proposals = self.enterContext(mock.patch.object(miner.approvals_store, "set_proposal_status_by_pr",
                                                             side_effect=self.forget_applied))
        self.enterContext(mock.patch.object(miner.approvals_store, "proposals", side_effect=lambda **kw: self.stored))
        self.enterContext(mock.patch.object(miner.approvals_store, "add_proposal", side_effect=self.store_applied))

    def store_applied(self, run_id, proposal, status, url=""):
        if status == "applied":
            self.stored.append({**proposal, "pr_url": url})

    def forget_applied(self, url, status):
        self.stored = [p for p in self.stored if p["pr_url"] != url]

    def advance(self, seconds):
        self.elapsed += seconds

    def github_read(self, args, **kwargs):
        if args[:2] == ["repo", "view"]:
            self.assertEqual(args[2], self.repo.github)
            return {"defaultBranchRef": {"name": self.base_name}}
        if args[:2] == ["pr", "list"]:
            self.assertEqual(args[args.index("--repo") + 1], self.repo.github)
            return self.prs
        if args[:3] == ["pr", "view", self.url]:
            return {"baseRefName": self.base_name, "mergeCommit": {"oid": self.MERGED},
                    "state": "MERGED" if self.merged else "OPEN", "mergedAt": util.iso() if self.merged else None}
        if args[:3] == ["pr", "view", "8"]:
            self.assertEqual(args[args.index("--repo") + 1], self.repo.github)
            state = "UNSTABLE" if self.elapsed < self.green_at else "CLEAN"
            self.readiness.append(state)
            return {"state": "OPEN", "isDraft": False, "baseRefName": self.base_name, "baseRefOid": self.base,
                    "headRefOid": self.head, "mergeStateStatus": state, "mergeable": "MERGEABLE", "title": "Prompt fix"}
        if args == ["api", f"repos/{self.repo.github}/pulls/8"]:
            return {"state": "open", "draft": False, "user": {"login": "operator"}, "body": "Why: fixes the cited failure.",
                    "labels": [{"name": miner.MINER_LABEL}],
                    "base": {"sha": self.base, "ref": self.base_name},
                    "head": {"sha": self.head, "ref": self.BRANCH, "repo": {"full_name": self.repo.github}}}
        if args[0] == "api" and "/check-runs?" in args[1]:
            self.assertTrue(args[1].startswith(f"repos/{self.repo.github}/commits/"))
            sha = args[1].split("/")[4]
            self.calls.append(("ci", sha))
            return [{"check_runs": [{"head_sha": self.reported_ci_head or sha,
                                     "status": "in_progress" if self.status == "pending" else "completed",
                                     "conclusion": self.status}]}]
        if args[0] == "api" and "/statuses?" in args[1]:
            return [[]]
        self.fail(f"Unexpected GitHub read {args}")

    def github_write(self, args, **kwargs):
        if args[:3] == ["api", "-X", "PUT"]:
            self.assertTrue(kwargs["write"])
            payload = json.loads(kwargs["input"])
            if args[3].endswith("/update-branch"):
                self.calls.append(("update", payload))
                self.head = self.UPDATED
                self.behind = False
                return ""
            self.assertEqual(args[3], f"repos/{self.repo.github}/pulls/8/merge")
            if self.repo.name == self.cfg.arete_repo:
                self.assertEqual(len(miner.paths.pending_prompt_merges()), 1)
            if kwargs["deadline"] is not None:
                self.assertGreater(kwargs["deadline"], self.elapsed)
            self.calls.append(("merge", payload))
            self.merged = True
            return json.dumps({"merged": True, "sha": self.MERGED})
        if args[:2] in (["pr", "close"], ["label", "create"], ["pr", "edit"]):
            self.calls.append(tuple(args))
            return ""
        self.fail(f"Unexpected GitHub write {args}")

    def git(self, args, cwd, **kw):
        self.assertEqual(cwd, self.repo.path)
        if args[0] == "fetch":
            if self.merged:
                self.merge_fetches += 1
            return ""
        if args == ["rev-parse", f"origin/{self.base_name}", f"origin/{self.BRANCH}"]:
            return f"{self.base}\n{self.head}\n"
        if args == ["merge-base", self.base, self.head]:
            return ("e" * 40 if self.behind else self.base) + "\n"
        if args[0] == "show":
            return "Rules for ${goal}"
        if args == ["branch", "--show-current"]:
            return "main\n"
        if args == ["merge-base", self.MERGED, "origin/main"] and self.merged:
            return self.MERGED + "\n" if self.merge_visible and self.merge_fetches >= self.merge_visible_after else ""
        if args == ["merge-base", self.MERGED, "HEAD"]:
            return self.MERGED + "\n" if self.checkout == self.MERGED else ""
        if args == ["merge", "-q", "--ff-only", "origin/main"] and self.merged:
            self.calls.append(("catch-up",))
            self.checkout = self.MERGED
            return ""
        if args == ["rev-parse", "HEAD", "origin/main"] and self.merged:
            return f"{self.checkout}\n{self.MERGED}\n"
        self.fail(f"Unexpected Git command {args}")

    def decide(self, wait=10):
        return miner.decide_pr(self.cfg, "min-test", self.repo, self.url, fresh=True, deadline=self.elapsed + wait)

    def run_miner(self, *, proposals=(), dry_run=False, apply=True, deadline=None, apply_repo=None):
        self.enterContext(mock.patch.multiple(
            miner, collect=mock.DEFAULT, collect_runs=mock.DEFAULT,
            render_events=mock.DEFAULT, proposal_status=mock.DEFAULT, apply_repo=mock.DEFAULT,
        ))
        miner.collect.return_value = [{"items": [], "cwd": "", "session": "fake"}] if proposals else []
        miner.collect_runs.return_value = [], 0
        miner.render_events.return_value = ""
        miner.proposal_status.return_value = "pending", ""
        miner.apply_repo.return_value = self.url, list(proposals)
        miner.apply_repo.side_effect = apply_repo
        self.enterContext(mock.patch.object(miner.auditor, "latest_findings", return_value="(none)"))
        self.enterContext(mock.patch.object(miner.insights_store, "feedback_between", return_value=[]))
        self.enterContext(mock.patch.object(miner.jev, "ready", return_value=False))
        self.enterContext(mock.patch.object(miner.runner, "run", return_value=SimpleNamespace(
            ok=True, cost_usd=0, structured={"proposals": list(proposals)}, error="", paid_usd=0,
        )))
        self.enterContext(mock.patch.object(miner.record, "settle"))
        now = util.now()
        return miner._run(self.cfg, "min-test", now - timedelta(days=1), now, True, "", dry_run, apply,
                          deadline=self.elapsed + 10 if deadline is None else deadline)

    def test_fresh_pr_merges_through_pipeline_path_and_fast_forwards_checkout(self):
        proposal = {"repo": "arete", "layer": "repo", "file": self.PROMPT, "change": "fix prompt"}
        self.assertEqual(self.run_miner(proposals=[proposal]), 0)
        self.eval.assert_called_once_with(self.cfg, self.repo, self.BASE, self.HEAD)
        self.assertEqual(self.calls[-3:], [("ci", self.HEAD),
                                           ("merge", {"sha": self.HEAD, "merge_method": "squash",
                                                      "commit_title": "Prompt fix (#8)"}), ("catch-up",)])
        self.assertEqual(self.checkout, self.MERGED)
        self.proposals.assert_called_once_with(self.URL, "merged")

    def test_stale_pr_is_decided_on_noop_run(self):
        self.prs = [{"createdAt": util.iso(util.now() - age), "url": url}
                    for age, url in ((timedelta(hours=1), "not due"), (timedelta(hours=48), self.URL))]
        self.assertEqual(self.run_miner(), 0)
        miner.runner.run.assert_not_called()
        self.assertTrue(self.merged)

    def test_fresh_pr_merges_in_same_run_when_ci_turns_green_after_two_minutes(self):
        self.green_at = 125
        proposal = {"repo": "arete", "layer": "repo", "file": self.PROMPT, "change": "fix prompt"}
        self.assertEqual(self.run_miner(proposals=[proposal], deadline=miner.miner_ci_deadline(self.elapsed)), 0)
        self.assertEqual(self.elapsed, 125)
        self.assertEqual(set(self.readiness[:-1]), {"UNSTABLE"})
        self.assertEqual(self.readiness[-1], "CLEAN")
        self.assertEqual(self.checkout, self.MERGED)

    def test_second_fresh_pr_checks_ci_after_first_repo_uses_polling_budget(self):
        second = config.Repo("other", Path("."), github="example/other")
        second_url = "https://github.com/example/other/pull/8"
        self.cfg.repos.append(second)
        self.status = "pending"
        first = {"repo": self.repo.name, "layer": "repo", "file": self.PROMPT, "change": "fix prompt"}
        other = {"repo": second.name, "layer": "repo", "file": "CLAUDE.md", "change": "clarify rules"}

        def apply_repo(cfg, run_id, repo, proposals, branch):
            if repo is second:
                self.assertEqual(self.elapsed, 10)
                self.assertFalse(self.merged)
                self.repo, self.url = second, second_url
                self.head, self.status = self.UPDATED, "success"
                self.diff.return_value = diffguard.Diff({"CLAUDE.md": 1})
            return self.url, proposals

        with mock.patch.object(miner, "settle_open_prs"), mock.patch.object(
            evals, "compare_harness", return_value=evals.Comparison("eval table", complete=True),
        ):
            self.assertEqual(self.run_miner(proposals=[first, other], apply_repo=apply_repo), 0)
        self.assertEqual(self.elapsed, 10)
        self.assertIn(("ci", self.HEAD), self.calls)
        self.assertEqual([call for call in self.calls if call[:1] == ("merge",)], [
            ("merge", {"sha": self.UPDATED, "merge_method": "squash", "commit_title": "Prompt fix (#8)"}),
        ])
        self.assertEqual(self.calls[-2], ("ci", self.UPDATED))
        self.proposals.assert_called_once_with(second_url, "merged")

    def test_deferred_pr_is_retried_without_new_proposals_before_48_hours(self):
        proposal = {"repo": "arete", "layer": "repo", "file": self.PROMPT, "change": "fix prompt"}
        self.status = "pending"
        self.assertEqual(self.run_miner(proposals=[proposal]), 0)
        self.assertFalse(self.merged)
        self.assertEqual(self.elapsed, 10)
        self.prs = [{"createdAt": util.iso(), "url": self.URL}]
        self.status = "success"
        self.assertEqual(self.run_miner(), 0)
        miner.runner.run.assert_not_called()
        self.assertTrue(self.merged)
        self.assertEqual(self.eval.call_count, 2)

    def test_configured_global_skills_use_their_own_repo_for_fresh_and_retry(self):
        self.repo = config.Repo("shared", Path("."), github="example/skills")
        self.cfg = config.Config(repos=[self.repo], global_repo=self.repo.name)
        self.url = "https://github.com/example/skills/pull/8"
        self.base_name = "trunk"
        skill = "skills/example/SKILL.md"
        self.diff.return_value = diffguard.Diff({skill: 2})
        proposal = {"repo": self.repo.name, "layer": "global", "file": skill, "change": "clarify skill"}
        self.status = "pending"
        with mock.patch.object(evals, "compare_harness", return_value=evals.Comparison("eval table", complete=True)) as suite:
            self.assertEqual(self.run_miner(proposals=[proposal]), 0)
            self.assertFalse(self.merged)
            self.assertEqual(self.run_miner(apply=False), 0)
            suite.assert_called_once_with(self.cfg, self.repo, self.BASE, self.HEAD)
            self.status = "success"
            self.assertEqual(self.run_miner(), 0)
        self.assertEqual(suite.call_args_list, [mock.call(self.cfg, self.repo, self.BASE, self.HEAD)] * 2)
        self.assertTrue(self.merged)
        self.eval.assert_not_called()
        self.assertNotIn(("catch-up",), self.calls)
        self.proposals.assert_called_once_with(self.url, "merged")

    def test_dry_run_leaves_stale_pr_untouched(self):
        self.prs = [{"createdAt": util.iso(util.now() - timedelta(days=3)), "url": self.URL}]
        self.assertEqual(self.run_miner(dry_run=True), 0)
        self.assertFalse(self.calls)
        self.eval.assert_not_called()

    def test_behind_pr_uses_new_head_for_diff_evals_and_ci(self):
        self.behind = True
        self.assertEqual(self.decide(), "merged")
        self.assertEqual(self.calls[0], ("update", {"expected_head_sha": self.HEAD}))
        self.diff.assert_called_once_with(self.repo.path, self.BASE, self.UPDATED,
                                          deny=diffguard.ALWAYS_DENY, moves=True)
        self.eval.assert_called_once_with(self.cfg, self.repo, self.BASE, self.UPDATED)
        self.assertIn(("ci", self.UPDATED), self.calls)
        self.assertNotIn(("ci", self.HEAD), self.calls)
        self.assertEqual(self.calls[-2][1]["sha"], self.UPDATED)

    def test_pending_or_failing_ci_holds_even_with_green_evals(self):
        for wait in (10, -1):
            for status in ("pending", "failure"):
                with self.subTest(status=status, wait=wait):
                    self.status = status
                    self.assertIn("no fully passing CI", self.decide(wait=wait))
                    self.assertFalse(self.merged)
        self.proposals.assert_not_called()

    def test_updated_pr_cannot_use_passing_ci_from_old_head(self):
        self.behind = True
        self.reported_ci_head = self.HEAD
        self.assertIn("no fully passing CI", self.decide())
        self.eval.assert_called_once_with(self.cfg, self.repo, self.BASE, self.UPDATED)
        self.assertFalse(self.merged)

    def test_oversized_or_incomplete_pr_stays_open(self):
        self.diff.return_value = diffguard.Diff({self.PROMPT: miner.MAX_MINER_DIFF + 1})
        self.assertIn("over the", self.decide())
        self.eval.assert_not_called()
        self.diff.return_value = diffguard.Diff({self.PROMPT: 2})
        self.eval.return_value = evals.Comparison("Eval suite could not run.")
        self.assertIn("incomplete", self.decide())
        self.assertFalse(self.merged)
        self.assertEqual([call[0] for call in self.calls], ["comment"])

    def test_global_rule_in_skill_diff_requires_approval_at_apply_and_merge(self):
        self.repo = config.Repo("shared", Path("."), github="example/skills")
        self.cfg = config.Config(repos=[self.repo], global_repo=self.repo.name)
        self.url = "https://github.com/example/skills/pull/8"
        skill = "skills/example/SKILL.md"
        proposal = {"repo": self.repo.name, "layer": "global", "file": skill}
        mixed = self.diff.return_value = diffguard.Diff({skill: 2, miner.GLOBAL_RULES: 1})
        for resume in (False, True):
            with self.subTest(resume=resume), mock.patch.object(
                gh, "open_pr_url", return_value=self.url if resume else ""
            ), mock.patch.multiple(
                miner.workspace, make_worktree=mock.Mock(return_value=self.root), remove_worktree=mock.DEFAULT,
            ), mock.patch.object(miner, "apply_in", return_value=Result(ok=True)), mock.patch.object(
                diffguard, "stage", return_value=diffguard.Diff({skill: 2}) if resume else mixed,
            ), mock.patch.object(util, "git_as_owner") as commit, mock.patch.object(
                miner, "git", return_value=f"{self.BASE}\n",
            ) as git:
                self.diff.reset_mock()
                self.assertEqual(miner.apply_repo(self.cfg, "min-test", self.repo, [proposal], self.BRANCH), ("", []))
                commit.assert_not_called()
                if resume:
                    git.assert_called_once_with(["merge-base", "origin/main", "HEAD"], self.root)
                    self.diff.assert_called_once_with(self.root, "--cached", self.BASE,
                                                      deny=diffguard.ALWAYS_DENY, moves=True)
                else:
                    git.assert_not_called()
        with mock.patch.object(evals, "compare_harness") as suite:
            self.assertIn(f"{miner.GLOBAL_RULES} requires owner approval", self.decide())
        suite.assert_not_called()
        self.assertFalse(self.calls)

    def test_merge_catches_up_when_commit_appears_on_later_fetch(self):
        self.merge_visible_after = 2
        self.assertEqual(self.decide(), "merged")
        self.assertEqual(self.merge_fetches, 2)
        self.sleep.assert_called_once_with(gh.MERGE_POLL_SECONDS)
        self.assertEqual(self.checkout, self.MERGED)
        self.assertFalse(miner.paths.pending_prompt_merges())
        self.proposals.assert_called_once_with(self.URL, "merged")

    def test_catch_up_exhaustion_keeps_receipt_for_next_run_and_blocks_stale_pipeline(self):
        self.merge_visible = False
        with self.assertRaisesRegex(RuntimeError, "does not yet contain merge commit"):
            self.decide()
        self.assertEqual(self.elapsed, miner.PROMPT_CATCH_UP_SECONDS)
        self.assertTrue(self.merged)
        self.assertEqual(self.checkout, self.BASE)
        self.proposals.assert_not_called()
        pending, = miner.paths.pending_prompt_merges()
        self.assertEqual(json.loads(pending.read_text())["commit"], self.MERGED)
        with mock.patch.object(config, "load", side_effect=AssertionError("pipeline started on stale prompts")):
            self.assertEqual(pipeline.run_issue("fixture", 7), 2)
        self.stored = [{"repo": self.repo.name, "pr_url": self.URL, "file": self.PROMPT, "change": "fix prompt"}]
        self.assertEqual(self.run_miner(dry_run=True), 0)
        self.assertEqual(self.run_miner(apply=False), 0)
        self.assertEqual(miner.paths.pending_prompt_merges(), [pending])
        self.assertEqual(self.checkout, self.BASE)
        self.proposals.assert_not_called()
        self.merge_visible = True
        self.assertEqual(self.run_miner(), 0)
        self.assertEqual(self.checkout, self.MERGED)
        self.assertEqual(miner.paths.pending_prompt_merges(), [])
        self.proposals.assert_called_once_with(self.URL, "merged")

    def test_prompt_merge_receipt_blocks_new_run_but_allows_next_call_in_active_run(self):
        run = pipeline.Run(id="pipeline-fixture", repo=self.repo, dir=self.root, tracker=mock.Mock(), tid="7")
        with mock.patch.object(pipeline.AgentRun, "agent", return_value=Result(ok=True)) as agent:
            self.assertTrue(run.agent("build", "Build the fix", self.root).ok)
            miner.PromptMerge(self.URL, self.base_name).save()
            with mock.patch.object(config, "load", side_effect=AssertionError("pipeline started on stale prompts")):
                self.assertEqual(pipeline.run_issue("fixture", 8), 2)
            self.assertTrue(run.agent("verify", "Check the fix", self.root).ok)
        self.assertEqual([call.args[0] for call in agent.call_args_list], ["build", "verify"])

    def test_no_apply_keeps_an_observed_merge_pending_until_catch_up(self):
        self.merged = True
        self.stored = [{"repo": self.repo.name, "pr_url": self.URL, "file": self.PROMPT, "change": "fix prompt"}]
        self.assertEqual(self.run_miner(apply=False), 0)
        self.assertEqual(self.checkout, self.BASE)
        self.proposals.assert_not_called()
        self.assertTrue(miner.paths.pending_prompt_merges())
        self.assertEqual(self.run_miner(), 0)
        self.assertEqual(self.checkout, self.MERGED)
        self.proposals.assert_called_once_with(self.URL, "merged")

    def test_no_apply_reconciles_contained_manual_merge_without_fetch_or_checkout_write(self):
        self.merged = True
        for has_receipt in (False, True):
            with self.subTest(has_receipt=has_receipt):
                self.checkout = self.MERGED if not has_receipt else self.BASE
                self.stored = [{"repo": self.repo.name, "pr_url": self.URL, "file": self.PROMPT, "change": "fix prompt"}]
                if has_receipt:
                    self.assertEqual(self.run_miner(apply=False), 0)
                    self.assertTrue(miner.paths.pending_prompt_merges())
                    self.checkout = self.MERGED
                self.proposals.reset_mock()
                self.git_mock.reset_mock()
                self.assertEqual(self.run_miner(apply=False), 0)
                self.git_mock.assert_called_once_with(["merge-base", self.MERGED, "HEAD"], self.repo.path,
                                                      check=False, retry=False, env={"GIT_NO_LAZY_FETCH": "1"})
                self.assertFalse(miner.paths.pending_prompt_merges())
                self.proposals.assert_called_once_with(self.URL, "merged")

    def test_head_or_base_change_during_evals_holds(self):
        for field in ("head", "base"):
            with self.subTest(field=field):
                self.head, self.base = self.HEAD, self.BASE

                def change(*args):
                    setattr(self, field, self.UPDATED)
                    return evals.Comparison("eval table", complete=True)

                self.eval.side_effect = change
                self.assertIn("changed during evals", self.decide())
        self.assertEqual(self.calls, [])

    def test_confirmed_regression_closes_with_table_even_when_another_eval_is_incomplete(self):
        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            self.assertEqual(source.ref, self.BASE)
            return evals.Arm(tag, [evals.TaskResult(task, passed, base_commit=self.BASE, context_sha="harness",
                                                  graded=True, prompts_used={"build.md"})
                                   for task, passed in (("regressed-task", on_base), ("improved-task", not on_base))]
                             + [evals.TaskResult("unavailable-task", passed=on_base, graded=on_base,
                                                 base_commit=self.BASE, context_sha="harness")])

        self.eval.side_effect = self.compare_prompts
        self.status = "failure"
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"build.md": ref.encode()})
        with mock.patch.multiple(evals, export_prompts=export, suites=lambda cfg: [self.repo],
                                 paired_tasks=lambda *args: [evals.Task("fixture", "Fixture", "fixture")],
                                 run_suite=suite), mock.patch.object(util, "lock", return_value=nullcontext(True)):
            self.assertIn("closed", self.decide())
        self.assertEqual([call.args[1] for call in export.call_args_list], [self.BASE, self.HEAD])
        reason, table = self.calls[0][2].split("\n\n", 1)
        self.assertIn("regressed-task", reason)
        self.assertNotIn("improved-task", reason)
        self.assertIn("| all | 2/3 | 1/2 (1 incomplete) |", table)
        self.assertEqual(self.calls[1], ("pr", "close", self.URL))
        self.assertFalse(self.merged)
        self.proposals.assert_called_once_with(self.URL, "rejected")

    def test_mixed_pr_compares_matching_prompt_and_harness_arms_on_the_base_tasks(self):
        self.diff.return_value = diffguard.Diff({self.PROMPT: 2, "CLAUDE.md": 1})
        task = evals.Task("interaction", "Interaction", "Check both edits", base=self.BASE)
        arms = []

        def suite(cfg, repo, source, tasks, tag, prompts):
            self.assertEqual(tasks, [task])
            arms.append((source.ref, prompts.commit))
            fails = source.ref == prompts.commit == self.HEAD
            return evals.Arm(tag, [evals.TaskResult(task.name, passed=not fails, graded=True,
                                                  base_commit=task.base, context_sha=source.ref,
                                                  prompts_used={"build.md"})])

        with mock.patch.multiple(
            evals, export_prompts=lambda repo, ref, dest: {"build.md": ref.encode()},
            paired_tasks=mock.Mock(return_value=[task]), run_suite=suite,
        ), mock.patch.object(util, "lock", return_value=nullcontext(True)):
            self.assertEqual(self.decide(), "closed for an eval regression")
            evals.paired_tasks.assert_called_once()
            self.assertEqual(evals.paired_tasks.call_args.args[1].ref, self.BASE)
            self.assertEqual(evals.paired_tasks.call_args.args[2], self.BASE)
        self.eval.assert_not_called()
        self.assertEqual(arms, [(self.BASE, self.BASE), (self.HEAD, self.HEAD)])
        self.assertIn("`interaction`", self.calls[0][2])
        self.assertEqual(self.calls[1], ("pr", "close", self.URL))
        self.assertFalse(self.merged)

    def test_mixed_pr_stays_open_when_no_task_exercises_the_prompt_and_harness_together(self):
        self.diff.return_value = diffguard.Diff({"arete/prompts/fix.md": 2, "CLAUDE.md": 1})
        task = evals.Task("fixture", "Fixture", "fixture", base=self.BASE)

        def suite(cfg, repo, source, tasks, tag, prompts):
            self.assertEqual(source.ref, prompts.commit)
            return evals.Arm(tag, [evals.TaskResult(task.name, passed=source.ref == self.BASE, graded=True,
                                                  base_commit=task.base, prompts_used={"build.md"})])

        with mock.patch.multiple(
            evals, export_prompts=lambda repo, ref, dest: {"fix.md": ref.encode()},
            paired_tasks=lambda *args: [task], run_suite=suite,
        ), mock.patch.object(util, "lock", return_value=nullcontext(True)), mock.patch.object(miner, "judge_prompts") as judge:
            self.assertIn("incomplete", self.decide())
        judge.assert_not_called()
        self.eval.assert_not_called()
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertIn("Not measured", self.calls[0][2])

    def test_repair_only_on_base_leaves_candidate_fix_prompt_unmeasured(self):
        task = evals.Task("fixture", "Fixture", "fixture")

        def suite(cfg, repo, source, tasks, tag, prompts):
            on_base = prompts.commit == self.BASE
            return evals.Arm(tag, [evals.TaskResult(task.name, passed=True, graded=True, base_commit=self.BASE,
                                                  context_sha="harness", repairs=int(on_base),
                                                  prompts_used={"fix.md"} if on_base else {"build.md"})])

        self.eval.side_effect = self.compare_prompts
        self.diff.return_value = diffguard.Diff({"arete/prompts/fix.md": 2})
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"fix.md": ref.encode()})
        with mock.patch.multiple(evals, export_prompts=export, suites=lambda cfg: [self.repo],
                                 paired_tasks=lambda *args: [task], run_suite=suite), \
                mock.patch.object(util, "lock", return_value=nullcontext(True)), \
                mock.patch.object(miner, "judge_prompts", return_value=(False, "JEV answered no.")) as judge:
            self.assertEqual(self.decide(), "JEV answered no.")
        self.assertEqual(judge.call_args.args[-1], ["fix.md"])
        self.assertIn("Not measured", self.calls[0][2])
        self.assertIn("JEV answered no.", self.calls[0][2])
        self.assertFalse(self.merged)

    def test_outage_after_recording_inputs_defers_comparison_and_skips_negative_history(self):
        task = evals.Task("fixture", "Fixture", "fixture", verify="Try the behavior", base=self.BASE)
        layer = config.RepoLayer(self.root, verify=config.Verify())

        def execute(cfg, source, task, prompts, run):
            on_base = prompts.commit == self.BASE
            run.call_kw = mock.Mock(return_value={})
            run.agent = mock.Mock(side_effect=[Result(ok=True, structured={}),
                                               Result(ok=on_base, error="" if on_base else "runner timed out")])
            run.gate = mock.Mock(return_value=(True, "checks passed"))
            return run_task(cfg, source, task, evals.Prompts(), run)

        run_task = evals.run_task
        self.eval.side_effect = self.compare_prompts
        export = mock.Mock(side_effect=lambda repo, ref, dest: {"build.md": ref.encode()})
        with mock.patch.multiple(
            evals, export_prompts=export, suites=lambda cfg: [self.repo], paired_tasks=lambda *args: [task],
            run_task=execute, fetch_origin=mock.DEFAULT, checkout_without_origin_refs=mock.Mock(return_value=self.BASE),
            prepare=mock.Mock(return_value=layer), context_digest=mock.Mock(return_value="harness"),
            eval_settings=mock.Mock(return_value=""), git=mock.DEFAULT,
            grade=mock.Mock(return_value=verify.Verified("pass")),
        ), mock.patch.multiple(
            evals.record, start=mock.Mock(side_effect=["eval-base", "eval-head"]),
            failed_on_crash=mock.Mock(return_value=nullcontext()), metered_spent=mock.Mock(return_value=0),
            settle=mock.DEFAULT,
        ), mock.patch.multiple(
            evals.insights_store, task_history=mock.Mock(return_value=[]), add_eval=mock.DEFAULT,
        ), mock.patch.object(evals.codebase, "load", return_value="fixture"), \
                mock.patch.object(evals.vault, "hidden", side_effect=str), \
                mock.patch.object(evals, "prompts_digest", return_value="fixture"), \
                mock.patch.object(evals.run_store, "last_step_id", return_value=0), \
                mock.patch.object(evals.paths, "WORKTREES_DIR", self.root / "worktrees"), \
                mock.patch.object(util, "lock", return_value=nullcontext(True)), \
                mock.patch.object(miner, "judge_prompts") as judge:
            self.assertIn("incomplete", self.decide())
            evals.insights_store.add_eval.assert_called_once()
            self.assertEqual(evals.insights_store.add_eval.call_args.args[:4], ("eval-base", "arete", task.name, True))
        self.assertIn("1 incomplete", self.calls[0][2])
        self.assertEqual([call[0] for call in self.calls], ["comment"])
        self.assertFalse(self.merged)
        self.proposals.assert_not_called()
        judge.assert_not_called()

    def test_grader_failure_and_blocked_verification_have_distinct_outcomes(self):
        task = evals.Task("fixture", "Fixture", "fixture", verify="Try the behavior")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root)
        for grade, outcome in ((verify.Verified("fail", "Behavior failed."), "fail"),
                               (verify.Verified("blocked", "Runner timed out.", finished=False), "incomplete")):
            with self.subTest(grade=grade.verdict), mock.patch.multiple(
                evals, git=mock.Mock(return_value=""), grade=mock.Mock(return_value=grade),
            ), mock.patch.object(evals.vault, "hidden", side_effect=str):
                result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                          "", "", True, 60)
                self.assertEqual(result.outcome, outcome)

    def test_combined_grading_preserves_a_failure_when_either_grader_is_unavailable(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="fixture-check", verify="Try the behavior")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root, verify=config.Verify())
        for verdict, check, outcome, failures in (
            ("fail", util.CommandIncomplete(task.check, 124, "", "timed out"), "fail", ["failed in use: fixture"]),
            ("blocked", subprocess.CompletedProcess(task.check, 1, "", ""), "fail", ["check exit 1"]),
            ("pass", util.CommandIncomplete(task.check, 124, "", "timed out"), "incomplete", []),
            ("blocked", subprocess.CompletedProcess(task.check, 0, "", ""), "incomplete", []),
            ("pass", subprocess.CompletedProcess(task.check, 0, "", ""), "pass", []),
        ):
            with self.subTest(verdict=verdict, check=check), mock.patch.multiple(
                evals, git=mock.Mock(return_value=""), grade=mock.Mock(return_value=verify.Verified(verdict, "fixture")),
                hidden_check=mock.Mock(return_value=check),
            ), mock.patch.object(evals.vault, "hidden", side_effect=str):
                result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                          "", "", True, 60)
                evals.hidden_check.assert_called_once()
                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.failures, failures)

    def test_held_out_failure_survives_a_blocked_in_use_verifier(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="fixture-check", verify="Try the behavior")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root, verify=config.Verify())
        with mock.patch.multiple(
            evals, git=mock.Mock(return_value=""),
            hidden_check=mock.Mock(return_value=subprocess.CompletedProcess(task.check, 1, "", "")),
        ), mock.patch.object(evals.vault, "hidden", side_effect=str), mock.patch.object(
            verify, "port_refusal", return_value="Fixture port is unavailable.",
        ):
            result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                      "", "", True, 60)
        self.assertEqual(result.outcome, "fail")
        self.assertEqual(result.verify, "blocked")
        self.assertEqual(result.failures, ["check exit 1"])

    def test_acceptance_distinguishes_invoked_missing_cli_from_infrastructure_failure(self):
        task = evals.Task("fixture", "Fixture", "fixture", check="expected-command")
        run = evals.EvalRun(id="eval-fixture", repo=self.repo, dir=self.root, task=task.name)
        layer = config.RepoLayer(self.root)
        for process, outcome in (
            (subprocess.CompletedProcess(task.check, 127, "", "sh: expected-command: command not found"), "fail"),
            (FileNotFoundError("shell launcher unavailable"), "incomplete"),
            (subprocess.TimeoutExpired(task.check, 60), "incomplete"),
            (subprocess.CompletedProcess(task.check, 1, "", "sandbox-exec: sandbox_apply: Operation not permitted"),
             "incomplete"),
        ):
            with self.subTest(process=process), mock.patch.object(evals, "git", return_value=""), mock.patch.object(
                util, "run_whole_tree", side_effect=[process],
            ), mock.patch.object(evals.vault, "hidden", side_effect=lambda text, *args: text):
                result = evals.acceptance(self.cfg, run, layer, self.root, task, None, evals.TaskResult(task.name),
                                          "", "", True, 60)
                self.assertEqual(result.outcome, outcome)
                if outcome == "fail":
                    self.assertEqual(result.failures, ["check exit 127"])

    def test_unmeasured_requires_jev_yes_and_records_both_answers(self):
        self.eval.return_value = evals.Comparison("Not measured.", unmeasured=["build.md"], complete=True)
        with mock.patch.object(jev, "ready", return_value=True), mock.patch.object(miner.vault, "hidden", side_effect=str):
            for value in (False, True):
                with self.subTest(value=value):
                    self.calls.clear()
                    answer = jev.Answer(value, 1.0, {"yes": float(value)})
                    with mock.patch.object(jev, "ask", return_value=jev.Reply({"fix": answer})) as ask:
                        self.decide()
                    self.assertIn("reported failure and rationale", ask.call_args.args[0])
                    self.assertIn(f"JEV answered {'yes' if value else 'no'}", self.calls[0][2])
                    self.assertEqual(self.merged, value)
                    if not value:
                        self.assertIn(("pr", "edit", self.URL, "--add-label", "agent:needs-review"), self.calls)
