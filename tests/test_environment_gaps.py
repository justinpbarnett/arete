import contextlib
import io
import json
import unittest
from collections import Counter
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from arete import ci, codebase, config, control, environment_gaps, factory, flows, gh, jev, paths, pipeline, poll, record, run_lines, tasks, tracker, util, workspace
from arete.agent import Result
from arete.store import orchestration as ledger
from arete.store import runs as run_store
from arete.verify import Verified
from tests import test_process_limits


class EnvironmentGapTests(test_process_limits.Isolated):
    sha = "a" * 40
    fix = "b" * 40
    head = "c" * 40
    github = "fixture/repo"
    error = "sandbox_apply: Operation not permitted"
    branch = "arete/1-fixture"

    def setUp(self):
        super().setUp()
        self.repo.github, self.repo.tracker = self.github, "github"
        self.cfg.arete_repo = self.repo.name
        self.repo.parts = [config.Part("", self.github)]
        self.queue = SimpleNamespace(repo=self.repo, tracker=tracker.GitHubTracker(self.repo), selected=set())
        self.layer = config.RepoLayer(dir=self.repo.path, base_branch="main")
        self.issues, self.comments, self.launched = {}, [], []
        self.other_issues = {}
        self.open_drafts = set()
        self.created, self.edited = 0, 0
        self.merge_sha, self.contains = "", False
        self.beat = {"alive": False, "tick_ok": True, "engine": self.sha[:12], "release": self.sha}
        self.journal = paths.STATE_DIR / "environment-gaps.json"
        for mod, name, fake in (
            (config, "load", lambda: self.cfg), (config, "load_layer", lambda _, **kwargs: self.layer),
            (control, "serve_beat", lambda: self.beat), (gh, "gh_json", self.pr_view),
            (gh, "issues", self.issue_list), (gh, "issue", lambda repo, number: self.issues_of(repo)[number]),
            (gh, "issue_create", self.create), (gh, "issue_add_waits", self.edit), (gh, "pr_comment", self.comment),
            (gh, "issue_merge_commit", lambda repo, n, base: self.merge_sha
             if self.issues_of(repo)[n]["state"] == "CLOSED" else ""),
            (gh, "branch_contains", lambda _, ref, commit: ref == "main" or self.contains and commit == self.fix),
            (tasks, "launch", self.launch),
        ):
            self.patches.enter_context(patch.object(mod, name, fake))
        self.patches.enter_context(patch.object(codebase, "RELEASE", self.sha))
        self.patches.enter_context(patch.object(codebase, "STAMP", self.sha[:12]))

    def url(self, number):
        return f"https://github.com/{self.github}/pull/{number + 100}"

    def issues_of(self, repo):
        return self.issues if repo == self.github else self.other_issues.setdefault(repo, {})

    def issue(self, number, title, body="", label="agent", repo=None):
        self.issues_of(repo or self.github)[number] = {"number": number, "title": title, "body": body, "state": "OPEN",
                                                     "labels": [{"name": label}], "author": {"login": "fixture"}}

    def issue_list(self, repo, label="", state="open", **kwargs):
        return [i for i in self.issues_of(repo).values() if (not label or {"name": label} in i["labels"])
                and (state == "all" or i["state"] == "OPEN")]

    def create(self, repo, title, body, labels, *, key, single_attempt):
        self.assertTrue(single_attempt)
        self.created += 1
        number = 90 + self.created
        self.issue(number, title, f"{body}\n\n{gh.key_mark(key)}", labels[0], repo)
        return number

    def edit(self, repo, number, body, waits):
        self.edited += 1
        self.issues_of(repo)[number]["body"] = f"{body}\n\nWaits on " + ", ".join(waits)
        return True

    def comment(self, url, body, *, key, single_attempt):
        self.assertTrue(single_attempt)
        self.comments.append((url, f"{body}\n\n{gh.key_mark(key)}"))

    def pr_view(self, args):
        self.assertEqual(args[:2], ["pr", "view"])
        return {"state": "OPEN", "isDraft": args[2] in self.open_drafts, "headRefOid": self.head}

    def verification(self, *causes, saw=None):
        return Verified("blocked", checks=[{"what": f"Run replay check {i}", "how": "arete replay",
                                           "saw": saw or self.error, "cause": cause, "passed": False}
                                          for i, cause in enumerate(causes)])

    def held(self, number, *causes, path="feature", saw=None):
        self.issue(number, f"Held work for {number}", "Original specification @owner")
        self.open_drafts.add(self.url(number))
        run = record.start("pipeline", repo=self.repo.name, issue=str(number))
        run_lines.write(run, "execution", path=path)
        run_lines.write(run, "step_start", step="verify")
        run_lines.write(run, "step_end", step="verify", result="pass",
                        data={"verified": asdict(self.verification(*causes, saw=saw))})
        run_lines.write(run, "step_end", step="review", result="pass", data={"review": {"verdict": "approve"}})
        run_lines.write(run, "step_end", step="pr", result="pass",
                        data={"prs": {self.github: self.url(number)}, "heads": {self.github: self.head},
                              "draft_why": "the check in use was blocked; this change requires behavioral evidence"})
        record.note(run, branch=self.branch)
        record.settle(run, "pr", pr_url=self.url(number))
        return run

    def classic_draft(self, prepared=None):
        self.issue(1, "Held classic change", "Original specification")
        self.open_drafts.add(self.url(1))
        run = record.start("pipeline", repo=self.repo.name, issue="1")
        run_lines.write(run, "execution", path="classic")
        run_lines.write(run, "classic_hold", prepared=prepared or {},
                        plan=dict(plan="Keep the feature file", goal="Check the feature file", estimate=1,
                                  left_for_a_person=""),
                        build={"summary": "Added feature.txt"}, review={"verdict": "approve"},
                        publication={"draft_why": "the check in use was blocked", "prs": {self.github: self.url(1)},
                                     "heads": {self.github: self.head},
                                     "verified": asdict(self.verification("environment"))})
        record.note(run, branch=self.branch)
        record.settle(run, "pr", pr_url=self.url(1))
        return run

    def launch(self, cfg, repo, tid, command, *, admission):
        self.launched.append(command)
        child = record.start("pipeline", repo=repo.name, issue=tid)
        run_lines.write(child, "execution", path="feature", environment_release=self.sha)
        run_lines.write(child, "route", continues=admission[0], again="verify")
        return {"run": child, "state": "running"}

    def sweep(self, dry_run=False, errors=None):
        output = io.StringIO()
        problems = [] if errors is None else errors
        with contextlib.redirect_stdout(output):
            started = poll.resume_environment_holds(self.cfg, [self.queue], dry_run, problems, Counter(), 8)
        if errors is None:
            self.assertEqual(problems, [])
        return started, output.getvalue()

    def test_reuses_open_gap_and_ignores_mixed_causes(self):
        self.held(1, "environment")
        self.held(2, "environment", saw=f"The replay command exited 1 with `sandbox-exec: {self.error}`.")
        self.held(3, "environment", "change")
        self.issue(50, "Replay fails inside verification", self.error)
        for _ in range(3):
            self.assertEqual(self.sweep()[0], 0)
        self.assertEqual(self.created, 0)
        self.assertEqual(self.edited, 2)
        self.assertEqual(tracker.waits_on(self.issues[1]["body"]), [50])
        self.assertEqual(tracker.waits_on(self.issues[2]["body"]), [50])
        self.assertEqual(self.issues[3]["body"], "Original specification @owner")
        self.assertCountEqual([url for url, _ in self.comments], [self.url(1), self.url(2)])

    def test_release_repository_owns_cross_repository_gaps(self):
        release = config.Repo(name="release", path=self.root / "release", github="fixture/arete")
        release_layer = config.RepoLayer(dir=release.layer_dir, base_branch="stable")
        self.cfg.repos.append(release)
        self.cfg.arete_repo = release.name
        run = self.held(1, "environment")
        self.issue(50, self.error)
        self.issue(91, "Unrelated local issue with the same number")
        self.issue(77, "Owner dependency", repo="other/project")
        with patch.object(config, "load_layer", side_effect=lambda p: release_layer if p == release.layer_dir else self.layer), \
                patch.object(gh, "issue_merge_commit", return_value=self.fix) as merged, \
                patch.object(gh, "branch_contains", return_value=True) as contains:
            self.sweep()
            self.assertNotIn("Waits on #91", self.issues[1]["body"])
            self.assertIn("Waits on fixture/arete#91", self.issues[1]["body"])
            self.assertIn("fixture/arete#91", self.comments[0][1])
            self.assertEqual(self.created, 1)
            self.assertIn(run, self.issues_of(release.github)[91]["body"])
            self.issues[1]["body"] += "\nWaits on other/project#77"
            self.assertIn("other/project#77", self.queue.tracker.get("1").blocker)
            self.issues_of(release.github)[91]["state"] = "CLOSED"
            self.beat["alive"] = True
            self.assertEqual(self.sweep(dry_run=True)[0], 0)
            merged.assert_not_called()
            self.issues_of("other/project")[77]["state"] = "CLOSED"
            self.assertEqual(self.sweep(dry_run=True)[0], 1)
            merged.assert_called_once_with(release.github, 91, "stable")
            self.assertEqual([call.args for call in contains.call_args_list],
                             [(release.github, "stable", self.sha), (release.github, self.sha, self.fix)])
        self.assertEqual(self.issues[91]["state"], "OPEN")

    def test_owner_edit_during_gap_lookup_is_kept(self):
        self.held(1, "environment")
        self.issue(77, "Owner dependency")
        new_body = "Owner's new requirements\n\nWaits on #77"

        def create_after_owner_edit(*args, **kwargs):
            self.issues[1]["body"] = new_body
            return self.create(*args, **kwargs)

        with patch.object(gh, "issue_create", create_after_owner_edit):
            self.sweep()
        self.assertEqual(self.issues[1]["body"], f"{new_body}\n\nWaits on #91")
        self.assertEqual((self.created, self.edited, len(self.comments)), (1, 1, 1))

    def test_errno_matches_across_reports_and_does_not_conflate_errors(self):
        self.held(1, "environment", saw="The command failed with ECONNREFUSED at 127.0.0.1:9000.")
        self.held(2, "environment", saw="Waiting on the server, then saw connect econnrefused.")
        self.held(3, "environment", saw="Node returned ECONNRESET while fetching a page.")
        self.sweep()
        self.assertEqual(self.created, 2)
        first, second, third = (tracker.waits_on(self.issues[n]["body"]) for n in (1, 2, 3))
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(third), 1)
        self.assertIn("ECONNREFUSED", self.issues[first[0]]["title"])
        self.assertIn("ECONNRESET", self.issues[third[0]]["title"])

    def test_classic_hold_is_linked_and_uses_the_classic_verify_continuation(self):
        source = self.classic_draft()
        self.sweep()
        self.assertEqual(tracker.waits_on(self.issues[1]["body"]), [91])
        self.issues[91]["state"] = "CLOSED"
        self.beat["alive"], self.merge_sha, self.contains = True, self.fix, True
        self.assertEqual(self.sweep()[0], 1)
        self.assertEqual(self.launched[0][-9:], ["run", self.repo.name, "1", "--local", "--force",
                                                "--continue", source, "--environment-release", self.sha])

    def test_retry_with_missing_plan_refuses_before_rebuilding_or_running_an_agent(self):
        source = self.held(1, "environment")
        run_lines.write(source, "step_end", step="prepare", result="pass", data={"branch": self.branch})
        child = record.start("pipeline", repo=self.repo.name, issue="1")
        run = pipeline.Run(child, self.repo, paths.RUNS_DIR / child, tracker=self.queue.tracker, tid="1",
                           environment_release=self.sha)
        flow = factory.Flow(child, flows.Job(self.cfg, run, self.root / "removed", continues=source),
                            continues=source, again="verify")
        with patch.object(flows, "restore_environment_workspace") as restore, patch.object(pipeline.Run, "agent") as agent:
            with self.assertRaisesRegex(workspace.RunFailed, "no complete saved plan and build"):
                pipeline.flow_function(self.cfg, "feature")(flow)
        restore.assert_not_called()
        agent.assert_not_called()

    def git(self, *args, cwd=None):
        return util.git([*util.ARETE_IDENTITY, *args], cwd or self.repo.path).strip()

    def saved_workspace(self, *, classic=False):
        self.layer.verify = config.Verify(publish_evidence=False)
        self.layer.checks = {"files": "test -f feature.txt && test -f fix.txt"}
        self.layer.trust_level = "automerge"
        origin = self.root / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin))
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", str(origin))
        self.git("commit", "-q", "--allow-empty", "-m", "Base")
        old_base = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "main")
        self.git("switch", "-q", "-c", self.branch)
        (self.repo.path / "feature.txt").write_text("Feature\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "Feature for the held draft")
        self.head = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", self.branch)
        self.git("switch", "-q", "main")
        (self.repo.path / "fix.txt").write_text("Fix\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", "Advance main")
        self.git("push", "-q", "origin", "main")
        base = self.git("rev-parse", "HEAD")
        return self.saved_hold(old_base, base, classic=classic)

    def saved_hold(self, old_base, base, *, classic):
        wt = paths.WORKTREES_DIR / f"{self.repo.name}-1"
        checkout = workspace.Checkout("", self.github, self.repo.path, wt, "main", base_commit=old_base,
                                      revision=old_base, lease=self.head)
        ticket = tracker.Ticket("1", "#1", "Keep the feature file", body="Original specification")
        prepare = flows.prepared_fields(ticket, self.branch, [checkout], self.layer, [], "", None)
        if classic:
            return self.classic_draft(prepare), wt, base
        self.issue(1, ticket.title, ticket.body)
        self.open_drafts.add(self.url(1))
        source = record.start("pipeline", repo=self.repo.name, issue="1")
        run_lines.write(source, "execution", path="feature")
        for step, data in (
            ("prepare", prepare),
            ("plan", {"plan": "Keep the feature file", "goal": "Check the feature file", "estimate": 1,
                      "left_for_a_person": ""}),
            ("implement", {"round": "build", "summary": "Added feature.txt"}),
            ("verify", {"verified": asdict(self.verification("environment"))}),
            ("review", {"review": {"verdict": "approve"}}),
            ("pr", {"prs": {self.github: self.url(1)}, "heads": {self.github: self.head},
                    "changed": [self.github], "draft_why": "the check in use was blocked"}),
        ):
            run_lines.write(source, "step_start", step=step)
            run_lines.write(source, "step_end", step=step, result="pass", data=data)
        record.note(source, branch=self.branch)
        record.settle(source, "pr", pr_url=self.url(1))
        return source, wt, base

    def assert_delivered(self, *, classic):
        source, wt, base = self.saved_workspace(classic=classic)
        self.assertFalse(wt.exists())
        self.sweep()
        self.issues[91]["state"] = "CLOSED"
        self.beat["alive"], self.merge_sha, self.contains = True, self.fix, True
        order = []
        pr = {"state": "OPEN", "isDraft": True, "body": "Closes #1", "baseRefOid": base, "baseRefName": "main",
              "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE", "title": "Fixture title"}

        def live_head():
            return workspace.remote_head(self.repo.path, self.branch)

        def pr_view(args):
            self.assertEqual(args[:2], ["pr", "view"])
            return {**pr, "headRefOid": live_head()}

        def verify(b, goal, summary):
            order.append("verify")
            self.assertEqual(goal, "Check the feature file")
            self.assertEqual(summary, "Added feature.txt")
            self.assertEqual(self.git("merge-base", "HEAD", base, cwd=b.ws), base)
            self.assertEqual((b.ws / "feature.txt").read_text(), "Feature\n")
            self.assertEqual((b.ws / "fix.txt").read_text(), "Fix\n")
            return Verified("pass", checks=[{"what": "Open the feature", "how": "Read it", "saw": "Feature",
                                              "passed": True}], native_trees=pipeline.verification_trees(b))

        def agent(run, name, prompt, cwd, **kwargs):
            self.assertEqual(name, "review")
            self.assertIn("Keep the feature file", prompt)
            self.assertIn("Open the feature", prompt)
            order.append("review")
            return Result(True, "", structured={"summary": "Approved", "problems": []})

        def checks(repo, head):
            self.assertEqual(head, live_head())
            order.append("ci")
            return {"build": gh.Check("build", "pass")}

        def ready(url, draft):
            self.assertEqual(url, self.url(1))
            self.assertEqual(order[-1], "ci")
            self.assertFalse(draft)
            order.append("ready")
            pr["isDraft"] = draft

        def merge_ci(repo, head, **kwargs):
            self.assertEqual((repo, head), (self.github, live_head()))
            self.assertFalse(pr["isDraft"])
            order.append("merge CI gate")
            return True

        def merge(args, **kwargs):
            self.assertEqual(args[:4], ["api", "-X", "PUT", f"repos/{self.github}/pulls/101/merge"])
            self.assertEqual(json.loads(kwargs["input"])["sha"], live_head())
            self.assertFalse(pr["isDraft"])
            self.assertEqual(order[-2:], ["risk", "merge CI gate"])
            order.append("merge")
            pr["state"] = "MERGED"
            return '{"merged": true}'

        def launch(cfg, repo, tid, command, *, admission):
            self.assertEqual(admission[0], source)
            if classic:
                drive = flows.retry_environment
            else:
                drive = pipeline.flow_function(cfg, "feature")
            script = pipeline.Script("classic" if classic else "feature", drive, continues=source, again="verify",
                                     environment_release=self.sha)
            self.assertEqual(pipeline.run_issue(repo.name, tid, force=True, script=script), 0)
            return {"run": run_store.newest_whole_run(repo.name, tid)["id"], "state": "pr"}

        with contextlib.ExitStack() as patches:
            for mod, name, options in (
                (tasks, "launch", {"side_effect": launch}),
                (pipeline, "check_in_use", {"side_effect": verify}),
                (pipeline.Run, "agent", {"autospec": True, "side_effect": agent}),
                (gh, "gh_json", {"side_effect": pr_view}),
                (gh, "gh", {"side_effect": merge}),
                (gh, "pr_body", {"side_effect": lambda url: pr["body"]}),
                (gh, "pr_edit_body", {"side_effect": lambda url, text, link: pr.update(body=gh.outgoing(text, link))}),
                (gh, "pr_comment", {}),
                (gh, "pr_set_draft", {"side_effect": ready}),
                (gh, "issue_labels", {}),
                (gh, "issue_comment", {}),
                (gh, "commit_checks", {"side_effect": checks}),
                (gh, "merge_ci_passed", {"side_effect": merge_ci}),
                (ci, "SETTLE_SECONDS", {"new": 0}),
                (jev, "ready", {"return_value": True}),
                (jev, "risk", {"side_effect": lambda *a, **k: order.append("risk") or SimpleNamespace(hold="")}),
            ):
                patches.enter_context(patch.object(mod, name, **options))
            self.assertEqual(self.sweep()[0], 1)
        self.assertEqual(order, ["verify", "review", "ci", "ready", "merge CI gate", "risk", "merge CI gate", "merge"],
                         run_store.newest_whole_run(self.repo.name, "1")["summary"])
        self.assertEqual(pr["state"], "MERGED")
        child = run_store.newest_whole_run(self.repo.name, "1")["id"]
        self.assertEqual([line["step"] for line in factory.ends(run_lines.read(child))],
                         ["verify", "checks", "review", "learn", "pr", "ci", "publish", "respond"])

    def test_feature_retry_restores_removed_workspace_and_delivers_against_advanced_base(self):
        self.assert_delivered(classic=False)

    def test_classic_retry_restores_removed_workspace_and_delivers_against_advanced_base(self):
        self.assert_delivered(classic=True)

    def test_daily_creation_and_lost_reply_recovery(self):
        run = self.held(1, "environment")

        def create_then_lose(*args, **kwargs):
            self.create(*args, **kwargs)
            raise gh.GhUnsure("reply lost after creation")

        errors = []
        with patch.object(gh, "issue_create", create_then_lose):
            self.sweep(errors=errors)
        self.assertEqual(len(errors), 1)
        self.issues[91]["state"] = "CLOSED"
        with patch.object(gh, "find_issue_marker", return_value=f"https://github.com/{self.github}/issues/91"):
            self.sweep()
            other = self.held(2, "environment")
            self.sweep()
        self.assertEqual(self.created, 1)
        self.assertEqual(self.edited, 2)
        self.assertIn(run, self.issues[91]["body"])
        self.assertIn(self.error, self.issues[91]["body"])
        self.assertIn("arete replay", self.issues[91]["body"])
        self.assertEqual(tracker.waits_on(self.issues[2]["body"]), [91])
        self.assertEqual(run_store.run_get(other)["status"], "pr")

    def test_resume_requires_merged_fix_and_healthy_release_containing_it(self):
        run = self.held(1, "environment")
        self.sweep()
        self.issues[91]["state"] = "CLOSED"
        self.beat["alive"] = True
        self.assertEqual(self.sweep()[0], 0)
        self.merge_sha = self.fix
        self.assertEqual(self.sweep()[0], 0)
        self.contains = True
        self.beat["alive"] = False
        self.assertEqual(self.sweep()[0], 0)
        self.beat["alive"] = True
        before = self.journal.read_bytes()
        with patch.object(poll, "network_up_or_skip", return_value=True), \
                patch.object(poll, "runnable", return_value=True), patch.object(poll, "fill_slots", return_value=False):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(poll.poll(dry_run=True), 0)
        text = out.getvalue()
        self.assertIn(f"would resume {run} from verify because #91 merged", text)
        self.assertIn(f"live release {self.sha[:12]} contains the fix", text)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(self.launched, [])
        row = run_store.run_get(run)
        self.assertTrue(environment_gaps.confirm_retry(self.cfg, self.repo, row, self.sha))
        self.issues[91]["state"] = "OPEN"
        self.assertFalse(environment_gaps.confirm_retry(self.cfg, self.repo, row, self.sha))
        self.issues[91]["state"] = "CLOSED"
        self.assertEqual(self.sweep()[0], 1)
        self.assertEqual(self.sweep()[0], 0)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0][-6:], ["--continue", run, "--from", "verify", "--environment-release", self.sha])

    def test_same_gap_stops_before_publication_and_polls_comment_once(self):
        run = self.held(1, "environment")
        self.sweep()
        self.issues[91]["state"] = "CLOSED"
        self.beat["alive"], self.merge_sha, self.contains = True, self.fix, True
        self.assertEqual(self.sweep()[0], 1)
        child = run_store.later_run(self.repo.name, "1", run)["id"]
        job = SimpleNamespace(run=SimpleNamespace(environment_release=self.sha), staged=lambda: None, layer=self.layer,
                              with_left=lambda text: text, goal="goal", build_summary="summary")
        job.layer.verify = config.Verify()
        with patch.object(pipeline, "check_in_use", return_value=self.verification("environment", saw="same check timed out")):
            result = flows.verify(SimpleNamespace(ctx=job), "fixture")
        self.assertFalse(result.passed)
        self.assertEqual(result.data["ending"], "environment-held")
        run_lines.write(child, "step_end", step="verify", result=result.result, data=result.data)
        record.settle(child, "pr", pr_url=self.url(1))
        for _ in range(3):
            self.assertEqual(self.sweep()[0], 0)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.created, 1)
        self.assertEqual(len(self.comments), 2)
        self.assertIn("same environment gap again", self.comments[-1][1])
        self.assertIn(self.url(1), self.open_drafts)

    def test_orchestrated_retry_uses_the_stored_verification_action(self):
        run = self.held(1, "environment", path="orchestrate")
        lease = ledger.acquire(run, "", {"mode": "orchestrate"})
        turn = ledger.begin(lease, {}, 10)
        action = ledger.accept(lease, turn, 0, {"calls": [{"op": "verification.run", "args": {}}]}, 10)[0]["id"]
        ledger.transition(lease, action, "succeeded", {})
        ledger.save(lease, {"mode": "orchestrate", "evidence": {"verify": {"producer": action}}})
        self.sweep()
        self.issues[91]["state"] = "CLOSED"
        self.beat["alive"], self.merge_sha, self.contains = True, self.fix, True
        self.assertEqual(self.sweep()[0], 1)
        self.assertEqual(self.launched[0][-6:], ["--continue", run, "--action", action, "--environment-release", self.sha])

    def test_interrupted_retry_does_not_start_as_ordinary_work_without_release_proof(self):
        run = self.held(1, "environment")
        self.sweep()
        self.issues[91]["state"] = "CLOSED"
        self.beat["alive"], self.merge_sha, self.contains = True, self.fix, True
        self.assertEqual(self.sweep()[0], 1)
        child = run_store.later_run(self.repo.name, "1", run)["id"]
        record.settle(child, "interrupted")
        with patch.object(poll, "next_ticket", return_value=None):
            self.assertIsNone(poll.next_pick(self.repo, self.queue.tracker, pipeline.SkipLog({}), set()))
        self.assertEqual(self.sweep()[0], 0)

    def test_poll_recovers_posted_comment_after_losing_its_response(self):
        self.held(1, "environment")

        def comment_then_lose(*args, **kwargs):
            self.comment(*args, **kwargs)
            raise gh.GhUnsure("reply lost after comment")

        errors = []
        with patch.object(gh, "pr_comment", comment_then_lose):
            self.sweep(errors=errors)
        self.assertEqual(len(errors), 1)
        with patch.object(gh, "find_issue_comment_marker", return_value=self.url(1)) as found:
            self.sweep()
            self.sweep()
        found.assert_called_once()
        self.assertEqual((self.edited, self.created, len(self.comments)), (1, 1, 1))


class GapMergeEvidenceTests(unittest.TestCase):
    def test_conditional_waits_edit_preserves_body_and_defers_on_conflict(self):
        path = "repos/fixture/work/issues/1"
        old = "Owner's original text @owner\r\n"
        edited = "Owner's update"

        def reply(body):
            return 'HTTP/2 200 OK\r\nETag: "observed"\r\n\r\n' + json.dumps({"body": body, "state": "open"})

        with patch.object(gh, "gh", return_value=reply(edited)) as call:
            self.assertFalse(gh.issue_add_waits("fixture/work", 1, old, ["fixture/arete#9"]))
            call.assert_called_once_with(["api", "--include", path])
        with patch.object(gh, "gh", side_effect=[reply(old), gh.GhError("HTTP 412 Precondition Failed")]) as call:
            self.assertFalse(gh.issue_add_waits("fixture/work", 1, old, ["fixture/arete#9"]))
            self.assertEqual(call.call_count, 2)
            request = call.call_args
            self.assertEqual(request.args[0], ["api", "--method", "PATCH", path, "-H", 'If-Match: "observed"',
                                               "--input", "-"])
            self.assertTrue(request.kwargs["write"])
            self.assertEqual(json.loads(request.kwargs["input"])["body"], old + "\n\nWaits on fixture/arete#9")

    def test_only_latest_completed_close_by_merged_fix_on_target_branch_counts(self):
        sha = "a" * 40
        fix = {"__typename": "PullRequest", "merged": True, "baseRefName": "main",
               "repository": {"nameWithOwner": "fixture/repo"}, "mergeCommit": {"oid": sha}}
        issue = {"state": "CLOSED", "stateReason": "COMPLETED", "timelineItems": {"nodes": [{"closer": fix}]}}
        with patch.object(gh, "gh_json", return_value={"data": {"repository": {"issue": issue}}}):
            self.assertEqual(gh.issue_merge_commit("fixture/repo", 9, "main"), sha)
            self.assertEqual(gh.issue_merge_commit("fixture/repo", 9, "other"), "")
            self.assertEqual(gh.issue_merge_commit("another/repo", 9, "main"), "")
            fix["merged"] = False
            self.assertEqual(gh.issue_merge_commit("fixture/repo", 9, "main"), "")
            fix["merged"] = True
            issue["timelineItems"]["nodes"][-1]["closer"] = None
            self.assertEqual(gh.issue_merge_commit("fixture/repo", 9, "main"), "")
