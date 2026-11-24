import os
import signal
import tempfile
import unittest
from dataclasses import asdict
from datetime import timedelta
from functools import cache, partialmethod
from itertools import count
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, call, patch

from arete import (config, flows, gh, launch_lease, paths, pipeline, pr_recovery, tasks, tracker, unblock,
                   unblock_watch, util, workspace)
from arete.orchestration import PROTOCOL, Controller, orchestrate
from arete.store import orchestration
from arete.store import tasks as task_store
from arete.store import unblock as ledger
from arete.verify import Verified
from tests.support import isolate_live_state as setUpModule
from tests.support import workspace_state

PR_NUMBERS = count(961)


@cache
def empty_state():
    return paths.DB_FILE.read_bytes()


class RecoveryFixture(unittest.TestCase):
    part = ""

    def setUp(self):
        snapshot = empty_state()
        self.root = root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(workspace_state(root))
        git_env = self.enterContext(patch.dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                                               GIT_CONFIG_COUNT="0", GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0"))
        git_env.pop("GIT_CONFIG_PARAMETERS", None)
        self.enterContext(patch.object(util, "wait_for_network", side_effect=AssertionError("unexpected network wait")))
        paths.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.DB_FILE.write_bytes(snapshot)
        src, layer = root / "sources", root / "policy"
        for directory in (src, layer):
            directory.mkdir()
        self.repo = config.Repo(root.name, src, "example/sample", tracker="local", layer=layer,
                                parts=[config.Part(self.part, "example/sample")])
        self.cfg = config.Config(repos=[self.repo], unblock_repos=(self.repo.name,), replay=config.ReplayConfig(all=True))
        self.tid = task_store.task_add(self.repo.name, "Resume the PR", "Keep both edits")
        self.source = tracker.LocalTracker(self.repo)
        self.previous = f"earlier-{self.tid}"
        self.ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.tid}"
        self.remotes = {}
        self.checkout, self.other = self.make_checkout(self.part, self.repo.github)
        self.src, self.wt, self.url = self.checkout.src, self.checkout.wt, self.checkout.pr
        self.remote = self.remotes[self.url]
        self.accepted = self.checkout.lease
        self.run = pipeline.Run("resumed", self.repo, root / "resumed", tracker=self.source, tid=self.tid, path="orchestrate")
        self.run.dir.mkdir()
        self.job = flows.Job(self.cfg, self.run, self.ws, ticket=self.source.get(self.tid), branch="topic",
                             checkouts=[self.checkout], layer=config.RepoLayer(dir=layer), before_pr_recovery=Mock())
        self.gh_command = gh.gh
        self.enterContext(patch.multiple(gh, pr_merge_details=Mock(side_effect=self.pr),
                                         pr_view=Mock(side_effect=self.pr), pr_body=Mock(return_value=""),
                                         pr_comment=Mock(), gh=Mock(side_effect=AssertionError("unexpected GitHub call"))))

    def make_checkout(self, part, github, *, ignore=""):
        slug = github.split("/")[-1]
        src, wt = self.repo.path / part, self.ws / part
        remote, other = self.root / f"{slug}.git", self.root / f"{slug}-other"
        self.git(self.root, "init", "--bare", str(remote))
        self.git(self.root, "init", "-b", "main", str(src))
        base = self.commit(src, "shared.txt", "base\n")
        if ignore:
            base = self.commit(src, ".gitignore", ignore)
        self.git(src, "remote", "add", "origin", str(remote))
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git(src, "worktree", "add", "-b", "topic", str(wt))
        accepted = self.commit(wt, "accepted.txt", "pushed by Arete\n", owner=self.previous)
        self.git(src, "push", "origin", "main", "topic")
        self.git(self.root, "clone", "--branch", "topic", str(remote), str(other))
        url = f"https://github.com/{github}/pull/{next(PR_NUMBERS)}"
        self.remotes[url] = remote
        return workspace.Checkout(part, github, src, wt, "main", url, accepted, True, base_commit=base), other

    def git(self, path, *args):
        return util.git(list(args), path).strip()

    def commit(self, path, file, value, owner=""):
        (path / file).write_text(value)
        self.git(path, "add", file)
        message = file + (f"\n\nArete-Run: {owner}" if owner else "")
        self.git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c",
                 "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", message)
        return self.head(path)

    def head(self, wt=None):
        return self.git(wt or self.wt, "rev-parse", "HEAD")

    def assert_contains(self, wt, head, *ancestors):
        for ancestor in ancestors:
            self.assertTrue(pr_recovery.ancestor(wt, ancestor, head), f"{head} dropped {ancestor}")

    def remote_refs(self, remote, *branches):
        refs = [f"refs/heads/{name}" for name in branches]
        try:
            return [(remote / ref).read_text().strip() for ref in refs]
        except FileNotFoundError:
            return self.git(remote, "rev-parse", *refs).splitlines()

    def remote_head(self, branch="topic"):
        return self.remote_refs(self.remote, branch)[0]

    def pr(self, url, **kw):
        head, base = self.remote_refs(self.remotes[url], "topic", "main")
        return {"url": url, "state": "OPEN", "headRefName": "topic", "isCrossRepository": False,
                "headRefOid": head, "baseRefName": "main", "baseRefOid": base}

    def push_edit(self, other, file, value="second session\n"):
        head = self.commit(other, file, value)
        self.git(other, "push", "origin", "topic")
        return head

    def advance(self, file="other.txt", value="second session\n"):
        return self.push_edit(self.other, file, value)

    def record_retained(self):
        pipeline.claim_workspace(self.run, self.ws, self.job.checkouts)
        workspace.note_made(self.ws, self.job.checkouts)
        self.run.workspace = None

    def keep(self, file="local.txt"):
        head = self.commit(self.wt, file, "retained work\n", owner=self.previous)
        self.record_retained()
        return head

    def reclaim(self):
        self.assertEqual(self.job.reclaim(self.checkout, self.accepted, True, {self.previous}), "")

    def assert_heads(self, local, remote):
        self.assertEqual((self.head(), self.remote_head()), (local, remote))

    def assert_loose(self, file, contents, *, private=False):
        files = workspace.ignored_file_paths if private else workspace.untracked_files
        self.assertIn(file.name, files(self.wt))
        self.assertEqual(file.read_bytes(), contents)

    def assert_staging_refused(self, why):
        with self.assertRaisesRegex(workspace.RunFailed, why):
            self.job.b.restage(from_base=True)

    def on_git(self, command, hook, *, wt=None, after=False):
        sh = util.sh

        def run(argv, **kw):
            if argv[0] == "git" and util.git_command(argv[1:]) == command and kw["cwd"] == (wt or self.wt):
                if after:
                    return hook(argv, sh(argv, **kw))
                hook(argv)
            return sh(argv, **kw)

        return patch.object(util, "sh", side_effect=run)

    def other_state(self):
        return (self.git(self.other, "show-ref"), self.git(self.other, "symbolic-ref", "HEAD"),
                self.git(self.other, "status", "--porcelain"), (self.other / "other.txt").read_bytes())


class OpenPRRecoveryTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(pipeline.record, "mark"))

    def check_cleanup(self, *, private):
        exclude, notes = self.src / ".git/info/exclude", self.wt / "notes.txt"
        rule, contents = "/notes.txt\n", b"retained contents\n"
        exclude.write_text(rule if private else "")
        notes.write_bytes(contents)
        self.keep()
        remote = self.advance()
        self.reclaim()
        self.job.install_pr_plans()
        head = self.head()
        pipeline.claim_workspace(self.run, self.ws, self.job.checkouts)
        before = pipeline.untracked_by_dir([self.wt])
        exclude.write_text("" if private else rule)
        with patch.object(workspace, "scratch_view", return_value=None):
            self.assertEqual(pipeline.restore_worktrees(before, self.root), 0)
        reason = "expose retained ignored files" if private else "hide retained untracked files"
        self.assert_staging_refused(f"{reason}: 'notes.txt'")
        self.assertFalse(pipeline.keep_edits(self.run))
        if not private:
            self.assertFalse(pipeline.release_workspace(self.run, self.ws, self.job.checkouts))
        self.assert_heads(head, remote)
        self.assert_loose(notes, contents, private=not private)
        if not private:
            exclude.write_text("")
            self.assertIn(notes.name, self.job.b.restage().files)
            self.assertEqual(self.git(self.wt, "show", ":notes.txt"), contents.decode().strip())

    test_cleanup_does_not_commit_retained_private_file_after_excludes_change = partialmethod(check_cleanup, private=True)
    test_cleanup_does_not_omit_retained_visible_file_after_excludes_change = partialmethod(check_cleanup, private=False)

    def test_owned_merge_does_not_authorize_a_foreign_local_parent(self):
        self.git(self.wt, "switch", "-c", "foreign")
        foreign = self.commit(self.wt, "foreign.txt", "not part of the run\n")
        self.git(self.wt, "switch", "topic")
        self.git(self.wt, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c",
                 "commit.gpgsign=false", "merge", "--no-ff", "-m", f"local merge\n\nArete-Run: {self.previous}",
                 "foreign")
        self.record_retained()
        local, external = self.head(), self.advance()
        with self.assertRaisesRegex(pr_recovery.Hold, f"unowned commits: {foreign}"):
            self.reclaim()
        self.assert_heads(local, external)


class ResumeFixture(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        self.enterContext(patch.object(paths, "CHECK_LOCKS_DIR", self.root / "check-locks"))
        self.enterContext(patch.object(config, "load", return_value=self.cfg))
        self.enterContext(patch.multiple(pipeline.scratch, process_ids=set, service_labels=set,
                                         RUN_BASELINES={}, RUN_LABELS={}))
        self.pr_comments, self.launched = [], []
        self.enterContext(patch.multiple(gh, me=Mock(return_value="keeper"),
                                         pr_discussion=Mock(side_effect=lambda _: gh.Discussion([], [], self.pr_comments))))
        self.spawn = self.enterContext(patch.object(tasks.control, "launch"))
        self.enterContext(patch.object(tasks, "spawned", side_effect=self.spawn_boundary))
        model = self.enterContext(patch.object(unblock, "diagnose", side_effect=AssertionError("unexpected diagnosis")))
        self.addCleanup(model.assert_not_called)
        self.decide = self.enterContext(patch.object(Controller, "decide", autospec=True,
                                                     side_effect=AssertionError("unexpected controller inference")))

    def spawn_boundary(self, repo, tid, spawn):
        with launch_lease.acquiring(repo, tid) as lease:
            spawn()
            self.launched.append(lease.recovery)
        return "", "starting"

    def seed_advance(self, name="other.txt", *, retained="local.txt", **state):
        local = self.keep(retained) if retained else self.head()
        if not retained:
            self.record_retained()
        observed = self.advance(name)
        pipeline.record.start("pipeline", run_id=self.previous, repo=self.repo.name, issue=self.tid)
        pipeline.run_lines.write(self.previous, "execution", path="orchestrate")
        prepared = {"branch": "topic", "ticket": asdict(self.source.get(self.tid)), "triage": "", "attachments": "",
                    "expertise": "", "layer": str(self.repo.layer_dir), "add_dirs": [],
                    "checkouts": [c.to_row() for c in self.job.checkouts]}
        orchestration.acquire(self.previous, "", {"protocol": PROTOCOL, "mode": "orchestrate", "evidence": {},
                                                   "run_settings": "fixture", "overrides": {}, "step_overrides": {},
                                                   "prepared": prepared, **state})
        pipeline.record.settle(self.previous, "pr", branch="topic", pr_url=" ".join(c.pr for c in self.job.checkouts if c.pr))
        return local, observed

    seed_conflict = partialmethod(seed_advance, "shared.txt", retained="shared.txt")

    def resume_without_discussion(self, previous=None):
        with patch.object(gh, "pr_discussion", side_effect=gh.GhError("discussion temporarily unavailable")):
            return self.resume(previous or self.previous)

    def gate_advance(self, check="test -f local.txt && test -f other.txt", *, retained="local.txt", state=None, **options):
        heads = self.seed_advance(retained=retained, **(state or {}))
        self.gate_publication(check, **options)
        return heads

    def gate_publication(self, check, *, finish_failed=False, requests=1, trust="pr"):
        (self.repo.layer_dir / "config.toml").write_text(
            f'[trust]\nlevel = "{trust}"\n[checks]\nboth = "{check}"\n[verify]\nstart = "echo fixture"\n')

        def gate_actions(ctl):
            self.decide.assert_called_once()
            self.assertEqual(ctl.job.b.restage().bases,
                             {c.github: self.pr(c.pr)["baseRefOid"] if c.pr else c.base_commit for c in self.job.checkouts})
            self.assertFalse(any(ctl.current(gate) for gate in ("checks", "verify", "review")))
            calls = [{"op": "checks.run", "args": {}}]
            for role, op in (("verify", "verification.run"), ("review", "review.run")):
                route = f"model:{role}:0"
                calls.append({"op": op, "args": {"route_id": route, "effort": ctl.catalog.entries[route]["efforts"][0]}})
            calls += [{"op": "publication.request", "args": {}}] * requests
            if finish_failed:
                calls.append({"op": "run.finish", "args": {"reason": "failed"}})
            turn = orchestration.begin(ctl.lease, {}, ctl.decision_limit)
            version = orchestration.snapshot(ctl.lease)["version"]
            response = ctl.catalog.validate({"version": ctl.protocol, "decision_id": turn, "state_version": version,
                                             "rationale": "Run fresh gates", "calls": calls}, turn, version, {})
            return orchestration.accept(ctl.lease, turn, version, response, ctl.action_limit)

        self.decide.side_effect = gate_actions
        checked = Verified("pass", checks=[{"what": "Required edits are present", "passed": True,
                                           "how": "Inspect the candidate", "saw": "Both edits"}])
        self.enterContext(patch.multiple(pipeline, check_in_use=Mock(return_value=checked),
                                         _review=Mock(return_value=pipeline.Review("approve")),
                                         watch_checkout_ci=Mock(side_effect=self.passing_ci)))
        drafts = {}
        self.enterContext(patch.multiple(
            gh, pr_set_draft=Mock(side_effect=lambda url, flag, **_: drafts.update({url: flag})),
            pr_draft_and_head=Mock(side_effect=lambda url: (drafts.get(url, False), self.pr(url)["headRefOid"])),
            pr_edit_body=Mock(side_effect=AssertionError("recovery rewrote a PR description"))))

    def passing_ci(self, run, checkout, *_):
        target = pipeline.ci_target(checkout)
        self.assertEqual(target.sha, self.pr(target.pr)["headRefOid"])
        return pipeline.ci.Watched(target, {})

    def unread_ci(self, run, checkout, *_):
        return pipeline.ci.Watched(pipeline.ci_target(checkout), {}, read=False)

    def controller(self):
        return self.decide.call_args.args[0]

    def action_states(self):
        return [a["state"] for a in orchestration.view(self.previous)["actions"]]

    def saved(self, run=None):
        return orchestration.find(run or self.previous)["data"]

    def publication_intent(self, run=None):
        return self.saved(run)["publication_intent"]

    def reports(self):
        return [(call.args[0], call.args[1].splitlines()[0]) for call in gh.pr_comment.call_args_list]

    def assert_published(self, row, heads, *, states=None):
        self.assertEqual(row["status"], "pr", row["summary"])
        self.assertEqual(set(row["pr_url"].split()), {c.pr for c in self.job.checkouts if c.pr})
        saved = self.saved(row["id"])
        self.assertFalse(orchestration.publication_pending(row["id"]))
        self.assertEqual(saved["publication"]["heads"], heads)
        self.assertEqual(self.action_states(), states or ["succeeded"] * 4)
        self.assertEqual({saved["evidence"][gate]["tree"] for gate in ("checks", "verify", "review")}, {saved["published_tree"]})
        self.assertIn("### both passed", (paths.RUNS_DIR / row["id"] / "checks.md").read_text())
        pipeline.check_in_use.assert_called_once()
        pipeline._review.assert_called_once()
        return saved

    def resume(self, previous, *, recovery="", expected=1):
        script = pipeline.Script("orchestrate", orchestrate, continues=previous)
        with patch.object(launch_lease, "recovery", return_value=recovery):
            code = pipeline.run_issue(self.repo.name, self.tid, script=script)
        row = pipeline.run_store.newest_live_whole_run(self.repo.name, self.tid)
        self.assertNotEqual(row["id"], previous, row)
        self.assertEqual(code, expected, row["summary"])
        return row

    def watch(self):
        if case := ledger.get(self.repo.name, self.tid):
            ledger.mark(case, case["phase"], case["detail"])
        unblock_watch.watch(self.cfg)
        return ledger.get(self.repo.name, self.tid)

    def resume_attempt(self, parent, attempt):
        child = self.resume(parent["id"], recovery=attempt)
        self.assert_held(child)
        self.assertEqual(ledger.attempt_of(attempt)["child"], child["id"])
        return child

    def notices(self):
        return [c["body"] for c in task_store.task_comments(self.tid) if c["author"] == "arete"]

    def assert_quiet_hold(self):
        baseline = self.watch()
        self.assertEqual(baseline["phase"], "needs_owner")
        self.assertEqual(self.watch()["generation"], baseline["generation"])
        self.assertEqual(len(self.notices()), 1)
        self.spawn.assert_not_called()
        return baseline

    def assert_held(self, row, *, pending=False):
        held = pr_recovery.blocked(row)
        self.assertTrue(held, row["summary"])
        self.assertTrue(held["decision"].startswith(pr_recovery.DECISION_VERSION))
        self.assertEqual(orchestration.publication_pending(row["id"]), pending)
        return held

    def assert_move(self, row, c, moved, phase, *, pending=False, expected=None):
        held = self.assert_held(row, pending=pending)
        if pending:
            self.assertEqual(self.action_states()[-1], "reconciliation-required")
        expected = c.lease if expected is None else expected
        self.assertNotEqual(expected, moved)
        self.assertEqual((held["pr"], held["expected_refs"]["head"], held["refs"]["head"]), (c.pr, expected, moved))
        for text in (phase, expected, moved):
            self.assertIn(text, row["summary"])
        return held


class OpenPRRecoveryResumeTests(ResumeFixture):
    def test_conflict_stays_held_across_notice_eviction_and_allows_only_a_trusted_pr_reply(self):
        for number in range(tracker.MAX_COMMENTS):
            task_store.task_comment(self.tid, "owner", f"Owner note {number}.")
        local, remote = self.seed_conflict()
        registered = self.git(self.src, "worktree", "list", "--porcelain")
        held = self.resume_without_discussion()
        for text in ("shared.txt", local, remote, "Resolve the named files in a separate checkout"):
            self.assertIn(text, held["summary"])
        self.assertEqual(self.git(self.src, "worktree", "list", "--porcelain"), registered)
        self.assertEqual((self.wt / "shared.txt").read_text(), "retained work\n")
        self.assertEqual((self.other / "shared.txt").read_text(), "second session\n")
        self.assertNotIn("Owner note 0.", self.source.get(self.tid).comments)
        repeated = self.resume_without_discussion(held["id"])
        first, again = pr_recovery.blocked(held), pr_recovery.blocked(repeated)
        self.assertEqual(first, {**again, "held_at": first["held_at"]})
        self.assertEqual((again["decision"], again["seen_replies"]), ("", None))
        baseline = self.watch()
        after = util.iso(util.parse_iso(again["held_at"]) + timedelta(seconds=1))
        self.pr_comments.extend({**reply, "created_at": after} for reply in (
            {"id": 1, "user": {"login": "example"}, "author_association": "OWNER", "body": "Try again"},
            {"id": 2, "user": {"login": "keeper"}, "body": gh.marked("Arete PR notice")},
            {"id": 4, "user": {"login": "keeper"}, "body": f"{tracker.ARETE_TAG}\nArete notice"}))
        self.assertEqual(self.assert_quiet_hold()["generation"], baseline["generation"])
        self.pr_comments.append({"id": 3, "user": {"login": "keeper"}, "body": "Keep both edits.", "created_at": after})
        eligible = self.watch()
        attempt, = self.launched
        self.assertGreater(eligible["generation"], baseline["generation"])
        self.resume_attempt(repeated, attempt)
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.assertEqual(len(self.notices()), 2)
        self.spawn.assert_called_once()
        self.assert_heads(local, remote)

    def test_leased_push_race_is_held_after_real_publication_action_and_new_head_requeues(self):
        local, observed = self.gate_advance()

        def racing_push(argv):
            pipeline.check_in_use.assert_called_once()
            pipeline._review.assert_called_once()
            self.assertIn(f"--force-with-lease=refs/heads/topic:{observed}", argv)
            self.assertIn(f"{self.head()}:refs/heads/topic", argv)
            self.advance("late.txt")

        with self.on_git("push --porcelain", racing_push):
            row = self.resume(self.previous)
        candidate, raced = self.head(), self.head(self.other)
        self.assert_move(row, self.checkout, raced, "during the leased push", expected=observed)
        self.assertIn(candidate, row["summary"])
        self.assertEqual(self.action_states(), ["succeeded"] * 3 + ["failed"])
        self.assert_heads(candidate, raced)
        self.assert_contains(self.wt, candidate, local, observed)
        self.assert_quiet_hold()
        changed = self.advance("again.txt")
        self.watch()
        attempt, = self.launched
        self.assertEqual(ledger.attempt_of(attempt)["grant"]["binding"]["pr_recovery"]["refs"]["head"], changed)
        self.decide.assert_called_once()

    def test_first_push_with_lost_confirmation_is_not_automatically_retried(self):
        local, observed = self.gate_advance()

        def lose_confirmation(argv, res):
            self.assertEqual(res.returncode, 0, res.stderr)
            return CompletedProcess(argv, util.TIMED_OUT, "", "fixture lost the result")

        with self.on_git("push --porcelain", lose_confirmation, after=True):
            row = self.resume(self.previous)
        candidate = self.head()
        held = self.assert_move(row, self.checkout, candidate, "did not confirm publication", pending=True, expected=observed)
        self.assertTrue(held["publication_unconfirmed"])
        self.assertEqual(held["pushed"], {})
        self.assertEqual(self.remote_head(), candidate)
        self.assert_contains(self.wt, candidate, local, observed)
        self.assertNotIn("no push was made", row["summary"])
        self.git(self.other, "pull", "--ff-only", "origin", "topic")
        self.advance("again.txt")
        self.watch()
        self.spawn.assert_not_called()
        (self.wt / "local.txt").write_text("edits outside the publication receipt")
        refused = self.resume(row["id"])
        self.assertIn("does not match the gated publication receipt", refused["summary"])
        self.assertTrue(orchestration.publication_pending(refused["id"]))
        self.decide.assert_called_once()

    def test_fresh_gates_retire_catch_up_hold_without_automerge_on_an_unpinned_base(self):
        prior = {"catch_up_hold": {"reason": "earlier review held"}, "catch_ups": 2}
        local, observed = self.gate_advance(trust="automerge", state=prior)
        base = self.commit(self.src, "base-only.txt", "advanced base\n")
        self.git(self.src, "push", "origin", "main")
        other_before = self.other_state()
        with (patch.object(gh, "merge_readiness", side_effect=AssertionError("unpinned base was mergeable")),
              patch.object(gh, "pr_merge_auto", side_effect=AssertionError("unpinned base was merged"))):
            row = self.resume(self.previous, expected=0)
        head = self.remote_head()
        saved = self.assert_published(row, {self.repo.github: head})
        self.assertNotIn("catch_up_hold", saved)
        self.assertEqual(saved["catch_ups"], 0)
        self.assert_contains(self.remote, head, local, observed, base)
        self.assertEqual(self.git(self.remote, "diff", "--name-only", base, head).splitlines(),
                         ["accepted.txt", "local.txt", "other.txt"])
        self.assertEqual(self.other_state(), other_before)
        self.assertEqual(saved["recovered_merge_heads"], {self.url: head})
        self.assertEqual(self.reports(), [(self.url, f"Recovered {kind} for head `{head}`")
                                          for kind in ("gate evidence", "CI evidence", "automerge decision")])
        self.assertIn(f"Compared with `main` at `{base}`.", gh.pr_comment.call_args_list[0].args[1])
        reason = (f"GitHub cannot require the checked base `main` at `{base}` when merging this recovered PR; "
                  "the merge API only pins the head")
        report, = [line for line in pipeline.run_lines.read(row["id"]) if line.get("event") == "report"]
        self.assertEqual(report["merge_held"], reason)
        self.assertIn(f"Automerge stays off for this PR: {reason}", gh.pr_comment.call_args.args[1])


class MultiCheckoutFixture(ResumeFixture):
    part = "first"

    def sibling(self, *, ignore="", without_pr=False):
        side, other = self.make_checkout("second", "example/second", ignore=ignore)
        self.repo.parts.append(config.Part(side.dir, side.github))
        self.job.checkouts.append(side)
        if without_pr:
            side.pr, side.resumes, side.revision = "", False, side.base_commit
            side.note_pushed_head("")
            self.git(side.wt, "reset", "--hard", side.base_commit)
            self.git(side.src, "push", "origin", ":topic")
        return side, other

    advance_sibling = partialmethod(RecoveryFixture.push_edit, file="side-race.txt")

    def move_sibling_base(self, side, name, text):
        base = self.commit(side.src, name, text)
        self.git(side.src, "push", "origin", "main")
        return base

    def gate_siblings(self, *, retained=False, **options):
        side, other = self.sibling()
        _, observed = self.gate_advance("test -f first/other.txt && test -f second/accepted.txt",
                                        retained="local.txt" if retained else "", **options)
        return side, other, observed

    def assert_drafts(self, side, *, ready=False):
        expected = [(url, draft) for draft in ((True, False) if ready else (True,)) for url in (self.url, side.pr)]
        self.assertEqual(gh.pr_set_draft.call_args_list, [call(*change, single_attempt=True) for change in expected])


class MultiCheckoutRecoveryTests(MultiCheckoutFixture):
    def check_sibling_move(self, *, installed):
        side, other = self.sibling()
        local, external = self.seed_advance()
        kind, method = (Controller, "merge_restored_bases") if installed else (flows.Job, "install_pr_plans")
        original = getattr(kind, method)

        def race(obj):
            if installed:
                original(obj)
            self.advance_sibling(other)
            if not installed:
                original(obj)

        with patch.object(kind, method, race):
            row = self.resume(self.previous)
        self.assert_move(row, side, self.head(other), "before resuming work" if installed else "during isolated reconciliation")
        self.assertIn("no push was made", row["summary"])
        if installed:
            self.assert_contains(self.wt, self.head(), local, external)
        else:
            self.assertEqual(self.head(), local)
        self.assertEqual(self.remote_head(), external)
        self.assertEqual(self.head(side.wt), side.lease)
        self.decide.assert_not_called()
        gh.pr_comment.assert_not_called()

    test_unchanged_sibling_move_after_observation_holds_before_installation = partialmethod(check_sibling_move, installed=False)
    test_unchanged_sibling_move_after_installation_holds_before_dispatch = partialmethod(check_sibling_move, installed=True)


class SiblingBaseRecoveryTests(MultiCheckoutFixture):
    def test_unchanged_sibling_head_with_a_conflicting_base_holds_before_installing(self):
        side, _ = self.sibling()
        name = "shared.txt"
        retained = self.commit(side.wt, name, "retained sibling edit\n", owner=self.previous)
        local, external = self.seed_advance()
        base = self.move_sibling_base(side, name, "new base edit\n")

        row = self.resume(self.previous)
        held = self.assert_held(row)
        self.assertEqual((held["github"], held["local"]), (side.github, retained))
        self.assertEqual((held["refs"]["head"], held["refs"]["base"]), (side.lease, base))
        for text in (repr(name), retained, base, "Resolve the named files in a separate checkout"):
            self.assertIn(text, row["summary"])
        self.assertEqual(self.head(side.wt), retained)
        self.assertEqual((side.wt / name).read_text(), "retained sibling edit\n")
        self.assertEqual(self.remote_refs(self.remotes[side.pr], "topic", "main"), [side.lease, base])
        self.assert_heads(local, external)
        self.assert_quiet_hold()
        self.decide.assert_not_called()


class InterruptedPublicationTests(MultiCheckoutFixture):
    def interrupted_gate_report(self, effect):
        gh.pr_comment.side_effect = effect
        row = self.resume(self.previous)
        intent, head = self.publication_intent(row["id"]), self.remote_head()
        self.assertEqual(intent["pushed"], {self.repo.github: head})
        self.assertEqual(self.reports(), [(self.url, f"Recovered gate evidence for head `{head}`")])
        gh.pr_comment.reset_mock(side_effect=True)
        self.decide.reset_mock()
        return row, intent, head

    def resume_without_replay(self, prior, heads, check):
        self.gate_publication(check)
        with patch.object(util, "sh", wraps=util.sh) as shell:
            resumed = self.resume(prior["id"], expected=0)
        self.assertFalse(any(c.args[0][:2] == ["git", "push"] for c in shell.call_args_list if c.args))
        self.assert_published(resumed, heads, states=["succeeded"] * 3 + ["failed"] + ["succeeded"] * 4)

    def test_resume_settles_only_the_open_pr_with_an_unchanged_no_pr_sibling(self):
        side, other = self.sibling(without_pr=True)
        check = "test -f first/local.txt && test -f second/shared.txt && test ! -e second/accepted.txt"
        self.gate_advance(check=check)
        self.enterContext(patch.multiple(gh, open_pr_url=Mock(return_value=""),
                                         pr_create=Mock(side_effect=AssertionError("unexpected PR creation"))))
        row, intent, published = self.interrupted_gate_report(gh.GhUnsure("fixture lost confirmation of a gate report"))
        receipt, heads = intent["recovery_receipt"], intent["pushed"]
        self.assertEqual(receipt["targets"].keys(), heads.keys())
        self.assertEqual(receipt["trees"].keys(), {self.repo.github, side.github})
        self.git(other, "push", "origin", "HEAD:topic")
        refused = self.resume(row["id"])
        self.assertIn(f"{side.github}: origin/topic moved while settling publication", refused["summary"])
        self.assertTrue(orchestration.publication_pending(refused["id"]))
        self.decide.assert_not_called()
        gh.pr_comment.assert_not_called()
        self.git(other, "push", "origin", ":topic")
        self.resume_without_replay(refused, heads, check)
        self.assertEqual((self.remote_head(), workspace.remote_head(side.src, "topic")), (published, ""))
        self.assertEqual(self.reports(), [(self.url, f"Recovered CI evidence for head `{published}`")])
        gh.pr_create.assert_not_called()

    def test_second_leased_push_race_reports_confirmed_first_push(self):
        side, other, _ = self.gate_siblings(retained=True)
        (side.wt / "side-local.txt").write_text("retained sibling edit\n")
        self.record_retained()
        with self.on_git("push --porcelain", lambda _: self.advance_sibling(other), wt=side.wt):
            row = self.resume(self.previous)
        raced = self.head(other)
        held = self.assert_move(row, side, raced, "during the leased push", pending=True)
        self.assertEqual(held["pushed"], {self.repo.github: self.remote_head()})
        self.assertIn("Already pushed", row["summary"])
        self.assertEqual((side.wt / "side-local.txt").read_text(), "retained sibling edit\n")

    def test_resume_after_a_push_and_pr_update_runs_fresh_gates_without_replay(self):
        side, other, _ = self.gate_siblings(retained=True)
        row, intent, published = self.interrupted_gate_report(lambda *_, **kw: self.advance_sibling(other))
        raced = self.head(other)
        held = self.assert_move(row, side, raced, "before refreshing PR evidence", pending=True)
        self.assertEqual(intent["pr_effects"], held["pr_effects"])
        self.watch()
        self.spawn.assert_not_called()
        self.resume_without_replay(row, {self.repo.github: published, side.github: raced},
                                   "test -f first/local.txt && test -f second/side-race.txt")
        self.assertEqual(self.reports(), [(side.pr, f"Recovered gate evidence for head `{raced}`"),
                                          (self.url, f"Recovered CI evidence for head `{published}`"),
                                          (side.pr, f"Recovered CI evidence for head `{raced}`")])


class RecoveredCIRetryTests(MultiCheckoutFixture):
    def test_stable_ci_retry_and_repair_use_the_pinned_base(self):
        side, _, observed = self.gate_siblings(requests=2)
        bases = {c.github: c.base_commit for c in self.job.checkouts}
        disclosure = pipeline.unverified_measurement_section({"requirement": "Required edits are present",
                                                             "measurement": "Earlier observation"})
        gh.pr_body.side_effect = lambda url: disclosure if url == side.pr and gh.pr_comment.called else ""
        red = gh.Check("fixture", "fail", job=123, workflow_run_id=456)
        rerun = gh.WorkflowRerun(456, 2, {red.name: red.job})

        def watch(run, checkout, reruns):
            target = pipeline.ci_target(checkout)
            self.assertEqual(target.base, bases[checkout.github])
            if reruns:
                b = self.controller().job.b
                with patch.object(workspace, "fetch", side_effect=AssertionError("CI repair fetched a mutable base")):
                    self.assertEqual(pipeline.merge_moved_bases_for_ci_fix(b, [checkout]), "")
                return pipeline.ci.Watched(target, {})
            return pipeline.ci.Watched(target, {red.key: red}, read=False,
                                       reds=[pipeline.ci.Red(target, red, None, target.base)])

        pipeline.watch_checkout_ci.side_effect = watch
        with (patch.object(gh, "failed_job_reruns", return_value=[rerun]), patch.object(gh, "rerun_failed_jobs") as launch,
              patch.object(gh, "job_log", return_value="fixture failure")):
            row = self.resume(self.previous, expected=0)
        self.assertEqual(launch.call_count, 2)
        gh.pr_edit_body.assert_not_called()
        self.assertIn((side.pr, f"Recovered measurement evidence for head `{side.lease}`"), self.reports())
        self.assert_drafts(side, ready=True)
        saved = self.assert_published(row, {self.repo.github: observed, side.github: side.lease},
                                      states=["succeeded"] * 3 + ["failed", "succeeded"])
        self.assertFalse({"ci_failed", "ci_retry", "recovered_publication"} & saved.keys())


class RecoveredFailedCIFinishTests(MultiCheckoutFixture):
    def test_failed_gate_before_first_publication_does_not_draft_advanced_sibling(self):
        side, other, _ = self.gate_siblings(requests=0, finish_failed=True)

        def reject(*_):
            self.advance_sibling(other)
            return pipeline.Review("request_changes", "The reconciled tree needs work")

        pipeline._review.side_effect = reject
        row = self.resume(self.previous)
        self.assert_move(row, side, self.head(other), "before reporting the failure")
        self.assertEqual(self.action_states()[-1], "failed")
        gh.pr_set_draft.assert_not_called()
        gh.pr_comment.assert_not_called()

    def test_failed_finish_does_not_draft_sibling_advanced_after_lookup(self):
        side, other, _ = self.gate_siblings(finish_failed=True)
        pipeline.watch_checkout_ci.side_effect = self.unread_ci

        def details(url, **options):
            row = self.pr(url) | {"isDraft": url == self.url}
            if (url == side.pr and not options.get("identity")
                    and self.saved().get("publication_intent", {}).get("phase") == "failed-ci"):
                self.advance_sibling(other)
            return row

        gh.pr_merge_details.side_effect = details
        row = self.resume(self.previous)
        self.assert_move(row, side, self.head(other), "before changing the PR draft state", pending=True)
        self.assert_drafts(side)


class RecoveredDraftMutationTests(MultiCheckoutFixture):
    def check_lost_mutation(self, *, draft):
        set_draft, sh = gh.pr_set_draft, util.sh
        side, other, _ = self.gate_siblings()
        if draft:
            pipeline.watch_checkout_ci.side_effect = self.unread_ci
        else:
            gh.pr_draft_and_head.side_effect = lambda url: (url == side.pr, self.pr(url)["headRefOid"])
        attempts = []

        def change(argv, **kw):
            if argv[0] != "gh":
                return sh(argv, **kw)
            self.assertEqual((argv[:3], argv[4:]), (["gh", "pr", "ready"], ["--undo"] if draft else []))
            attempts.append(argv[3])
            if argv[3] == self.url:
                return CompletedProcess(argv, 0, "", "")
            self.assertEqual(self.publication_intent()["pr_effects"][-1],
                             {"kind": "draft state", "github": side.github, "pr": side.pr, "head": side.lease})
            self.advance_sibling(other)
            return CompletedProcess(argv, 1, "", "error connecting to api.github.com: connection reset")

        with (patch.object(util, "sh", side_effect=change), patch.object(gh, "gh", self.gh_command),
              patch.object(gh, "pr_set_draft", set_draft)):
            row = self.resume(self.previous)
        self.assertEqual(attempts, [self.url, side.pr] if draft else [side.pr])
        state = "a draft" if draft else "ready for review"
        self.assert_move(row, side, self.head(other), f"after attempting to mark the PR {state}", pending=True)

    test_initial_ci_failure_does_not_retry_drafting_after_head_move = partialmethod(check_lost_mutation, draft=True)
    test_passing_ci_does_not_retry_readying_after_head_move = partialmethod(check_lost_mutation, draft=False)


class RecoveredAutomergeReportTests(ResumeFixture):
    def test_move_during_last_automerge_comment_leaves_intent_pending(self):
        _, observed = self.gate_advance(retained="", check="test -f other.txt", trust="automerge")

        def comment(url, body, *, single_attempt):
            if "Automerge stays off" in body:
                self.assertTrue(single_attempt)
                self.assertIn(f"for head `{observed}`", body)
                self.assertIn("the merge API only pins the head", body)
                self.advance("during-report.txt")

        gh.pr_comment.side_effect = comment
        row, raced = self.resume(self.previous), self.head(self.other)
        held = self.assert_move(row, self.checkout, raced, "before clearing publication intent", pending=True, expected=observed)
        self.assertEqual(held["pr_effects"][-1]["kind"], "Recovered automerge decision")
        self.assertTrue(held["publication_unconfirmed"])


class MultiCheckoutInstallTests(MultiCheckoutFixture):
    def test_later_install_failure_keeps_successful_checkout_provenance_for_next_resume(self):
        name, notes = "side-private.txt", "private notes\n"
        side, _ = self.sibling(ignore=f"/{name}\n")
        (side.wt / name).write_text(notes)
        self.checkout.onto = self.accepted
        side_local = self.commit(side.wt, "side-local.txt", "retained sibling\n", owner=self.previous)
        base = self.move_sibling_base(side, ".gitignore", f"# Comment\n/{name}\n")
        local, external = self.seed_advance()

        refuse_install = Mock(side_effect=RuntimeError("fixture refused the fast-forward"))
        with self.on_git("merge --no-autostash", refuse_install, wt=side.wt):
            row = self.resume(self.previous)
        self.assertEqual(self.assert_held(row)["github"], side.github)
        self.assertIn("refused candidate", row["summary"])
        self.decide.assert_not_called()
        actual = self.head()
        self.assert_contains(self.wt, actual, local, external)
        self.assertEqual((self.head(side.wt), self.pr(side.pr)["headRefOid"]), (side_local, side.lease))
        made = workspace.made_before(self.ws)
        self.assertEqual([workspace.head_recorded(made, c) for c in self.job.checkouts], [actual, side_local])
        self.assertEqual(workspace.onto_recorded(made, self.checkout), "")
        saved = flows.prepared_data(row["id"])["checkouts"]
        self.assertEqual([c["recovery_head"] for c in saved], [external, ""])
        self.assert_quiet_hold()

        def recovered(ctl):
            self.assertEqual(self.head(), actual)
            self.assert_contains(side.wt, self.head(side.wt), side_local, base)
            self.assertEqual(ctl.job.checkouts[1].recovery_base, base)
            self.assertNotIn(side.prefix + name, ctl.job.b.restage().files)
            ctl.ending = ("noop", "Reached the next decision after recovering both checkouts", "")
            return []

        self.decide.side_effect = recovered
        self.resume(row["id"], expected=0)
        self.decide.assert_called_once()
        self.assertIn(name, workspace.ignored_file_paths(side.wt))
        self.assertEqual((side.wt / name).read_text(), notes)
