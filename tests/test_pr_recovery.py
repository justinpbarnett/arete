import os
import signal
import tempfile
import unittest
from collections import Counter
from dataclasses import asdict
from datetime import timedelta
from itertools import count
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arete import (config, flows, gh, launch_lease, outcomes, paths, pipeline, poll, pr_recovery, tasks, tracker,
                   unblock, unblock_watch, util, workspace)
from arete.orchestration import PROTOCOL, Controller, orchestrate
from arete.store import db
from arete.store import insights as insights_store
from arete.store import orchestration
from arete.store import tasks as task_store
from arete.store import unblock as ledger
from arete.verify import Verified
from tests import support

PR_NUMBERS = count(961)


def setUpModule():
    support.isolate_live_state()


class RecoveryFixture(unittest.TestCase):
    part = ""

    def setUp(self):
        self.root = root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(support.workspace_state(root))
        git_env = self.enterContext(patch.dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                                               GIT_CONFIG_COUNT="0", GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0"))
        git_env.pop("GIT_CONFIG_PARAMETERS", None)
        self.enterContext(patch.object(util, "wait_for_network", side_effect=AssertionError("unexpected network wait")))
        db.migrate()
        src, layer = root / "sources", root / "policy"
        src.mkdir()
        layer.mkdir()
        self.repo = config.Repo(root.name, src, "example/sample", tracker="local", layer=layer,
                                parts=[config.Part(self.part, "example/sample")])
        self.cfg = config.Config(repos=[self.repo], unblock_repos=(self.repo.name,), replay=config.ReplayConfig(all=True))
        self.tid = task_store.task_add(self.repo.name, "Resume the PR", "Keep both edits")
        self.source = tracker.LocalTracker(self.repo)
        self.ticket = self.source.get(self.tid)
        self.previous = f"earlier-{self.tid}"
        self.ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.tid}"
        self.ws.parent.mkdir(parents=True, exist_ok=True)
        self.remotes = {}
        self.checkout, self.other = self.make_checkout(self.part, self.repo.github)
        self.src, self.wt, self.url = self.checkout.src, self.checkout.wt, self.checkout.pr
        self.remote = self.remotes[self.url]
        self.accepted, self.base = self.checkout.lease, self.checkout.base_commit
        run_dir = root / "resumed"
        run_dir.mkdir()
        self.run = pipeline.Run("resumed", self.repo, run_dir, tracker=self.source, tid=self.tid, path="orchestrate")
        self.job = flows.Job(self.cfg, self.run, self.ws, ticket=self.ticket, branch="topic", checkouts=[self.checkout],
                             layer=config.RepoLayer(dir=self.repo.layer_dir), before_pr_recovery=Mock())
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
        self.git(src, "switch", "-c", "topic")
        accepted = self.commit(src, "accepted.txt", "pushed by Arete\n", owner=self.previous)
        self.git(src, "push", "origin", "main", "topic")
        self.git(src, "switch", "main")
        self.git(self.root, "clone", "--branch", "topic", str(remote), str(other))
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git(src, "worktree", "add", str(wt), "topic")
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
        return self.git(path, "rev-parse", "HEAD")

    def remote_head(self, branch="topic"):
        return self.git(self.remote, "rev-parse", f"refs/heads/{branch}")

    def pr(self, url, **kw):
        remote = self.remotes[url]
        try:
            head, base = ((remote / "refs" / "heads" / name).read_text().strip() for name in ("topic", "main"))
        except FileNotFoundError:
            head, base = self.git(remote, "rev-parse", "refs/heads/topic", "refs/heads/main").splitlines()
        return {"url": url, "state": "OPEN", "headRefName": "topic", "isCrossRepository": False,
                "headRefOid": head, "baseRefName": "main", "baseRefOid": base}

    def advance(self, file="other.txt", value="second session\n"):
        head = self.commit(self.other, file, value)
        self.git(self.other, "push", "origin", "topic")
        return head

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

    def other_state(self):
        return (self.git(self.other, "show-ref"), self.git(self.other, "symbolic-ref", "HEAD"),
                self.git(self.other, "status", "--porcelain"), (self.other / "other.txt").read_bytes())


class OpenPRRecoveryTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(pipeline.record, "mark"))
        self.enterContext(patch.object(flows, "authors", return_value={self.previous}))

    def controller(self):
        controller = object.__new__(Controller)
        controller.job, controller.run, controller.workers = self.job, self.run, {}
        controller.lease = "lease"
        controller.data = {"acceptance": "owner answer", "evidence": {}, "prepared": {"ticket": {}}}
        controller.policy_sha = lambda: "policy"
        controller.catalog = SimpleNamespace(add_context=Mock())
        controller.executing = "action"
        controller.persist = Mock()
        controller.check_pilot_publication = Mock()
        controller.finish_publication = Mock(return_value={"status": "pr"})
        controller.publication_ci = Mock(return_value=None)

        def publish_pr(name, **kw):
            self.assertEqual(name, "pr")
            fresh, staged, _, _, unpushed = flows.settle_unpushed_checkouts(self.job)
            self.assertTrue(fresh.empty)
            self.assertEqual(unpushed, [self.checkout])
            return flows.publication_outcome(self.job, staged, "", True)

        controller.atomic = Mock(side_effect=publish_pr)
        return controller

    def stamp(self, controller):
        controller.stamp("checks", {"full": True})
        controller.stamp("verify", {"verdict": "pass"})
        controller.stamp("review", {"verdict": "approve"})

    def test_compatible_advance_compares_with_exact_pr_base_and_requires_fresh_gates(self):
        local = self.keep()
        external = self.advance()
        base = self.commit(self.src, "base-only.txt", "advanced base\n")
        self.git(self.src, "push", "origin", "main")
        before = self.other_state()

        self.reclaim()
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.job.install_pr_plans()
        candidate = self.git(self.wt, "rev-parse", "HEAD")
        for original in (local, external, base):
            self.assertTrue(pr_recovery.ancestor(self.wt, original, candidate))
        for file, value in (("local.txt", "retained work\n"), ("other.txt", "second session\n")):
            self.assertEqual((self.wt / file).read_text(), value)
        staged = self.job.b.restage()
        self.assertEqual(staged.bases, {self.repo.github: base})
        self.assertEqual(set(staged.files), {"accepted.txt", "local.txt", "other.txt"})

        controller = self.controller()
        self.stamp(controller)
        with patch.object(orchestration, "actions", return_value=[{"id": "old-review", "state": "succeeded"}]):
            controller.discard_pr_recovery_state()
        self.assertEqual(controller.data["stale_actions"], ["old-review"])
        with self.assertRaisesRegex(ValueError, "publication needs current evidence: checks, review, verify"):
            controller.publish()
        controller.atomic.assert_not_called()
        self.assertEqual(self.remote_head(), external)

        self.stamp(controller)
        self.assertEqual(controller.publish(), {"status": "pr"})
        self.assertEqual(self.remote_head(), candidate)
        self.assertEqual(self.other_state(), before)

    def test_conflict_retains_original_heads_and_names_resolution(self):
        local = self.keep("shared.txt")
        external = self.advance("shared.txt")
        registered = self.git(self.src, "worktree", "list", "--porcelain")
        with self.assertRaises(pr_recovery.Hold) as caught:
            self.reclaim()
        error = caught.exception
        for text in ("shared.txt", local, external, "Resolve the named files in a separate checkout"):
            self.assertIn(text, str(error))
        self.assertEqual(error.recovery["refs"]["head"], external)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), external)
        self.assertEqual(self.git(self.src, "worktree", "list", "--porcelain"), registered)
        self.assertEqual((self.wt / "shared.txt").read_text(), "retained work\n")
        self.assertEqual((self.other / "shared.txt").read_text(), "second session\n")

    def test_incoming_ignore_rule_holds_without_hiding_or_committing_retained_file(self):
        local = self.commit(self.wt, "local.txt", "retained commit\n", owner=self.previous)
        name = "private notes.tmp"
        secret = b"retained but not for publication\x00\n"
        (self.wt / name).write_bytes(secret)
        self.job.layer.deny = [name]
        self.record_retained()
        external = self.advance(".gitignore", f"/{name}\n")

        with self.assertRaises(pr_recovery.Hold) as caught:
            self.reclaim()
        why = str(caught.exception)
        for text in ("incoming ignore rules", repr(name), "Save the named files separately", local, external):
            self.assertIn(text, why)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), external)
        self.assertIn(name, workspace.untracked_files(self.wt))
        self.assertEqual((self.wt / name).read_bytes(), secret)
        self.assertEqual(self.git(self.wt, "diff", "--cached"), "")

    def test_removed_ignore_rule_keeps_retained_file_out_of_staging_and_patches(self):
        name = "local notes.txt"
        secret = "dummy private contents that should not appear"
        self.accepted = self.advance(".gitignore", f"/{name}\n")
        self.git(self.wt, "pull", "--ff-only", "origin", "topic")
        self.checkout.note_pushed_head(self.accepted)
        (self.wt / name).write_bytes(secret.encode())
        local = self.keep()
        external = self.advance(".gitignore", "# no ignores\n")

        with self.assertRaises(pr_recovery.Hold) as caught:
            self.reclaim()
        why = str(caught.exception)
        for text in ("expose retained ignored files", repr(name), local, external):
            self.assertIn(text, why)
        self.assertNotIn(secret, why)
        self.assertIn(name, workspace.ignored_file_paths(self.wt))
        staged = self.job.b.restage(from_base=True)
        self.assertNotIn(name, staged.files)
        self.assertNotIn(secret, staged.patch)
        self.assertEqual(self.git(self.wt, "ls-files", "--", name), "")
        self.assertEqual((self.wt / name).read_bytes(), secret.encode())
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), external)

    def test_owned_merge_does_not_authorize_a_foreign_local_parent(self):
        self.git(self.wt, "switch", "-c", "foreign")
        foreign = self.commit(self.wt, "foreign.txt", "not part of the run\n")
        self.git(self.wt, "switch", "topic")
        self.git(self.wt, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c",
                 "commit.gpgsign=false", "merge", "--no-ff", "-m", f"local merge\n\nArete-Run: {self.previous}",
                 "foreign")
        self.record_retained()
        local = self.git(self.wt, "rev-parse", "HEAD")
        external = self.advance()
        with self.assertRaisesRegex(pr_recovery.Hold, f"unowned commits: {foreign}"):
            self.reclaim()
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), external)

    def test_head_race_during_isolated_plan_does_not_install_candidate(self):
        local = self.keep()
        observed = self.advance()
        self.reclaim()
        moved = self.advance("late.txt")
        before = self.other_state()
        with self.assertRaises(pr_recovery.Hold) as caught:
            self.job.install_pr_plans()
        for text in ("during isolated reconciliation", observed, moved, "no push was made"):
            self.assertIn(text, str(caught.exception))
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), moved)
        self.assertEqual(self.other_state(), before)


class ResumeFixture(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        self.enterContext(patch.object(paths, "CHECK_LOCKS_DIR", self.root / "check-locks"))
        self.enterContext(patch.object(config, "load", return_value=self.cfg))
        self.enterContext(patch.multiple(pipeline.scratch, process_ids=set, service_labels=set,
                                         RUN_BASELINES={}, RUN_LABELS={}))
        self.pr_comments, self.launched = [], []
        self.enterContext(patch.object(gh, "me", return_value="keeper"))
        self.enterContext(patch.object(gh, "pr_discussion", side_effect=lambda _: gh.Discussion([], [], self.pr_comments)))
        self.spawn = self.enterContext(patch.object(tasks.control, "launch"))
        self.enterContext(patch.object(tasks, "spawned", side_effect=self.spawn_boundary))
        self.model = self.enterContext(patch.object(unblock, "diagnose", side_effect=AssertionError("unexpected diagnosis")))
        self.decide = self.enterContext(patch.object(Controller, "decide", autospec=True,
                                                     side_effect=AssertionError("unexpected controller inference")))

    def spawn_boundary(self, repo, tid, spawn):
        with launch_lease.acquiring(repo, tid) as lease:
            spawn()
            self.launched.append(lease.recovery)
        return "", "starting"

    def seed(self):
        pipeline.record.start("pipeline", run_id=self.previous, repo=self.repo.name, issue=self.tid)
        pipeline.run_lines.write(self.previous, "execution", path="orchestrate")
        prepared = {"branch": "topic", "ticket": asdict(self.source.get(self.tid)), "triage": "", "attachments": "",
                    "expertise": "", "layer": str(self.repo.layer_dir), "add_dirs": [],
                    "checkouts": [c.to_row() for c in self.job.checkouts]}
        orchestration.acquire(self.previous, "", {"protocol": PROTOCOL, "mode": "orchestrate", "evidence": {},
                                                   "run_settings": "fixture", "overrides": {}, "step_overrides": {},
                                                   "prepared": prepared})
        pipeline.record.settle(self.previous, "pr", branch="topic", pr_url=" ".join(c.pr for c in self.job.checkouts))

    def gate_publication(self, check):
        (self.repo.layer_dir / "config.toml").write_text(f'[checks]\nboth = "{check}"\n[verify]\nstart = "echo fixture"\n')
        controllers = []

        def gate_actions(ctl):
            self.assertFalse(controllers, "publication should leave the controller on a hold")
            controllers.append(ctl)
            self.assertEqual(ctl.job.b.restage().bases, {c.github: c.base_commit for c in self.job.checkouts})
            self.assertFalse(any(ctl.current(gate) for gate in ("checks", "verify", "review")))
            calls = [{"op": "checks.run", "args": {}}]
            for role, op in (("verify", "verification.run"), ("review", "review.run")):
                route = f"model:{role}:0"
                calls.append({"op": op, "args": {"route_id": route, "effort": ctl.catalog.entries[route]["efforts"][0]}})
            calls.append({"op": "publication.request", "args": {}})
            turn = orchestration.begin(ctl.lease, {}, ctl.decision_limit)
            version = orchestration.snapshot(ctl.lease)["version"]
            response = ctl.catalog.validate({"version": ctl.protocol, "decision_id": turn, "state_version": version,
                                            "rationale": "Run each gate on the reconciled tree", "calls": calls},
                                            turn, version, {})
            return orchestration.accept(ctl.lease, turn, version, response, ctl.action_limit)

        self.decide.side_effect = gate_actions
        self.enterContext(patch.object(pipeline, "check_in_use", return_value=Verified("pass", "Both edits checked")))
        self.enterContext(patch.object(pipeline, "_review", return_value=pipeline.Review("approve", "Reconciled tree approved")))
        return controllers

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

    def notices(self):
        return [c["body"] for c in task_store.task_comments(self.tid) if c["author"] == "arete"]

    def assert_held(self, row, *, pending=False):
        held = pr_recovery.blocked(row)
        self.assertTrue(held, row["summary"])
        self.assertTrue(held["decision"].startswith(pr_recovery.DECISION_VERSION))
        self.assertEqual(orchestration.publication_pending(row["id"]), pending)
        return held


class OpenPRRecoveryResumeTests(ResumeFixture):
    def test_conflict_stays_held_across_notice_eviction_and_allows_only_a_trusted_pr_reply(self):
        for number in range(tracker.MAX_COMMENTS):
            task_store.task_comment(self.tid, "owner", f"Owner note {number}.")
        local = self.keep("shared.txt")
        remote = self.advance("shared.txt")
        self.seed()

        held = self.resume(self.previous)
        self.assert_held(held)
        for text in ("shared.txt", local, remote, "Resolve the named files"):
            self.assertIn(text, held["summary"])
        self.assertNotIn("Owner note 0.", self.source.get(self.tid).comments)
        repeated = self.resume(held["id"])
        first, again = self.assert_held(held), self.assert_held(repeated)
        self.assertEqual({**first, "held_at": again["held_at"]}, again)
        baseline = self.watch()
        self.assertEqual(baseline["phase"], "needs_owner", baseline["detail"])
        self.pr_comments.extend([{"id": 1, "user": {"login": "example"}, "author_association": "OWNER",
                                  "body": "Try again"},
                                 {"id": 2, "user": {"login": "keeper"}, "body": gh.marked("Arete PR notice")}])
        self.assertEqual(self.watch()["generation"], baseline["generation"])
        self.assertEqual(len(self.notices()), 1)
        self.spawn.assert_not_called()

        self.pr_comments.append({"id": 3, "user": {"login": "keeper"}, "body": "Keep the second session's wording."})
        eligible = self.watch()
        attempt, = self.launched
        self.assertGreater(eligible["generation"], baseline["generation"])
        held_again = self.resume(repeated["id"], recovery=attempt)
        self.assert_held(held_again)
        self.assertEqual(ledger.attempt_of(attempt)["child"], held_again["id"])
        self.assertEqual(len(self.notices()), 2)
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.spawn.assert_called_once()
        self.decide.assert_not_called()
        self.model.assert_not_called()
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), remote)

    def test_first_successful_watch_keeps_trusted_reply_after_failed_baseline_read(self):
        self.keep("shared.txt")
        self.advance("shared.txt")
        self.seed()
        with patch.object(gh, "pr_discussion", side_effect=gh.GhError("discussion temporarily unavailable")):
            held = self.resume(self.previous)
        failure = pr_recovery.blocked(held)
        self.assertEqual(failure["decision"], "")
        self.spawn.assert_not_called()

        self.pr_comments.append({"id": 1, "user": {"login": "keeper"}, "body": "Keep the upstream wording.",
                                 "created_at": failure["held_at"]})
        self.watch()
        attempt, = self.launched
        child = self.resume(held["id"], recovery=attempt)
        self.assert_held(child)
        self.assertEqual(ledger.attempt_of(attempt)["child"], child["id"])
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.spawn.assert_called_once()
        self.decide.assert_not_called()
        self.model.assert_not_called()

    def test_feedback_poll_can_revise_held_pr_without_unblock_enrollment(self):
        self.cfg.unblock_repos = ()
        self.keep("shared.txt")
        self.advance("shared.txt")
        self.seed()
        self.pr_comments.append({"id": 1, "user": {"login": "keeper"}, "body": "An earlier reply",
                                 "created_at": util.iso(util.now() - timedelta(minutes=10))})
        held = self.resume(self.previous)
        cutoff = self.assert_held(held)["held_at"]
        self.pr_comments.extend([
            {"id": 2, "user": {"login": "stranger"}, "body": "Try again", "created_at": cutoff},
            {"id": 3, "user": {"login": "keeper"}, "body": gh.marked("Arete PR notice"), "created_at": cutoff},
            {"id": 4, "user": {"login": "keeper"}, "body": f"{tracker.ARETE_TAG}\nArete notice", "created_at": cutoff},
        ])
        layer = config.load_layer(self.repo.layer_dir)
        discovery = poll.routing.Discovery(self.cfg, self.repo, priced=False, layer=layer)
        queue = poll.RepoQueue(self.repo, self.source, set(), layer, discovery)
        skips = pipeline.SkipLog({})

        def collect_and_pick():
            insights_store.add_feedback(outcomes.owner_feedback(self.url, "OPEN", self.repo.name, self.tid))
            return poll.next_pick(self.repo, self.source, skips, set())

        def spawn_feedback(repo, tid, readmission):
            self.assertEqual(pipeline.run_command(repo, tid)[-2:], ["--continue", held["id"]])
            self.assertIsNone(readmission)
            self.assert_held(self.resume(held["id"]))
            return True

        with (patch.object(pipeline, "FEEDBACK_QUIET_MINUTES", 0),
              patch.object(poll, "spawn_unparked", side_effect=spawn_feedback) as start):
            pick = collect_and_pick()
            self.assertTrue(pick.from_feedback)
            self.assertEqual(poll.start_pick(self.cfg, queue, pick, skips, False, Counter(), 1), "skipped")
            start.assert_not_called()

            self.pr_comments.append({"id": 5, "user": {"login": "keeper"}, "body": "Please preserve both versions.",
                                     "created_at": cutoff})
            pick = collect_and_pick()
            self.assertTrue(pick.from_feedback)
            self.assertEqual(poll.start_pick(self.cfg, queue, pick, skips, False, Counter(), 1), "started")
            self.assertIsNone(collect_and_pick())
            self.assertEqual(poll.start_pick(self.cfg, queue, pick, skips, False, Counter(), 1), "skipped")
            start.assert_called_once()
        self.watch()
        self.spawn.assert_not_called()
        self.decide.assert_not_called()
        self.model.assert_not_called()

    def test_leased_push_race_is_held_after_real_publication_action_and_new_head_requeues(self):
        local = self.keep()
        observed = self.advance()
        self.seed()
        controllers = self.gate_publication("test -f local.txt && test -f other.txt")
        sh = util.sh
        pushed, moved = [], []

        def racing_push(argv, **kwargs):
            if argv[:3] == ["git", "push", "--porcelain"]:
                self.assertIn(f"--force-with-lease=refs/heads/topic:{observed}", argv)
                pushed.append(self.git(self.wt, "rev-parse", "HEAD"))
                self.assertIn(f"{pushed[-1]}:refs/heads/topic", argv)
                moved.append(self.advance("late.txt"))
            return sh(argv, **kwargs)

        with patch.object(util, "sh", side_effect=racing_push):
            row = self.resume(self.previous)
        held = self.assert_held(row)
        candidate, = pushed
        raced, = moved
        for text in ("during the leased push", observed, raced, candidate):
            self.assertIn(text, row["summary"])
        pipeline.check_in_use.assert_called_once()
        pipeline._review.assert_called_once()
        ctl, = controllers
        actions = orchestration.actions(ctl.lease)
        self.assertEqual([a["state"] for a in actions], ["succeeded", "succeeded", "succeeded", "failed"])
        self.assertIn("### both passed", (paths.RUNS_DIR / row["id"] / "checks.md").read_text())
        self.assertEqual(held["refs"]["head"], raced)
        self.assertEqual(self.remote_head(), raced)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), candidate)
        for original in (local, observed):
            self.assertTrue(pr_recovery.ancestor(self.wt, original, candidate))

        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.spawn.assert_not_called()
        self.assertEqual(len(self.notices()), 1)
        changed = self.advance("again.txt")
        self.watch()
        attempt, = self.launched
        self.assertEqual(ledger.attempt_of(attempt)["grant"]["binding"]["pr_recovery"]["refs"]["head"], changed)
        self.model.assert_not_called()
        self.decide.assert_called_once()

    def test_first_push_with_lost_confirmation_is_not_automatically_retried(self):
        local = self.keep()
        observed = self.advance()
        self.seed()
        controllers = self.gate_publication("test -f local.txt && test -f other.txt")
        sh = util.sh

        def lose_confirmation(argv, **kwargs):
            res = sh(argv, **kwargs)
            if argv[:3] == ["git", "push", "--porcelain"]:
                self.assertEqual(res.returncode, 0, res.stderr)
                return CompletedProcess(argv, util.TIMED_OUT, "", "fixture lost the result")
            return res

        with patch.object(util, "sh", side_effect=lose_confirmation):
            row = self.resume(self.previous)
        held = self.assert_held(row, pending=True)
        candidate = self.git(self.wt, "rev-parse", "HEAD")
        self.assertTrue(held["publication_unconfirmed"])
        self.assertEqual(held["pushed"], {})
        self.assertEqual(held["expected_refs"]["head"], observed)
        self.assertEqual(held["refs"]["head"], candidate)
        self.assertEqual(self.remote_head(), candidate)
        self.assertTrue(all(pr_recovery.ancestor(self.wt, oid, candidate) for oid in (local, observed)))
        self.assertNotIn("no push was made", row["summary"])
        ctl, = controllers
        self.assertEqual(orchestration.actions(ctl.lease)[-1]["state"], "reconciliation-required")
        self.git(self.other, "pull", "--ff-only", "origin", "topic")
        self.advance("again.txt")
        self.watch()
        self.spawn.assert_not_called()


class RecoveredPublicationBoundaryTests(ResumeFixture):
    def test_advance_after_confirmed_push_holds_before_refreshing_evidence(self):
        self.keep()
        observed = self.advance()
        self.seed()
        controllers = self.gate_publication("test -f local.txt && test -f other.txt")
        heads = []

        def read_body(_):
            pushed = self.remote_head()
            if not heads and pushed != observed:
                self.git(self.other, "pull", "--ff-only", "origin", "topic")
                heads.append((pushed, self.advance("after-push.txt")))
            return ""

        with (patch.object(gh, "pr_body", side_effect=read_body),
              patch.object(gh, "pr_edit_body") as edit):
            row = self.resume(self.previous)
        held = self.assert_held(row, pending=True)
        (pushed, advanced), = heads
        for text in ("before refreshing PR evidence", pushed, advanced, "Reconcile the partial publication"):
            self.assertIn(text, row["summary"])
        self.assertEqual(held["expected_refs"]["head"], pushed)
        self.assertEqual(held["expected_refs"]["origin"], pushed)
        self.assertEqual(held["refs"]["head"], advanced)
        self.assertEqual(held["refs"]["origin"], advanced)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), pushed)
        self.assertEqual(self.remote_head(), advanced)
        edit.assert_not_called()
        gh.pr_comment.assert_not_called()
        ctl, = controllers
        actions = orchestration.actions(ctl.lease)
        self.assertEqual([a["state"] for a in actions], ["succeeded"] * 3 + ["reconciliation-required"])
        data = orchestration.find(row["id"])["data"]
        self.assertNotIn("publication", data)
        self.assertEqual(data["publication_intent"]["pushed"], {self.repo.github: pushed})
        self.watch()
        self.spawn.assert_not_called()


class SettlementReplyRecoveryTests(ResumeFixture):
    def resume_with_settlement_reply(self, previous, reply_id, *, recovery=""):
        drive = flows.driven

        def reply_after_hold(*args):
            try:
                return drive(*args)
            except pr_recovery.Hold:
                self.pr_comments.append({"id": reply_id, "user": {"login": "keeper"},
                                         "body": "Keep both versions for my review.", "created_at": util.iso()})
                raise

        with patch.object(flows, "driven", side_effect=reply_after_hold):
            return self.resume(previous, recovery=recovery)

    def test_enrolled_hold_does_not_consume_a_reply_received_during_settlement(self):
        local = self.keep("shared.txt")
        remote = self.advance("shared.txt")
        self.seed()
        held = self.resume(self.previous)
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.pr_comments.append({"id": 1, "user": {"login": "keeper"}, "body": "Keep the upstream wording."})
        self.watch()
        attempt, = self.launched

        child = self.resume_with_settlement_reply(held["id"], 2, recovery=attempt)
        self.assert_held(child)
        self.assertEqual(ledger.attempt_of(attempt)["child"], child["id"])
        self.watch()
        first, next_attempt = self.launched
        self.assertEqual(first, attempt)
        self.assertNotEqual(next_attempt, first)

        retried = self.resume(child["id"], recovery=next_attempt)
        self.assert_held(retried)
        self.assertEqual(ledger.attempt_of(next_attempt)["child"], retried["id"])
        baseline = self.watch()
        self.assertEqual(baseline["phase"], "needs_owner")
        self.assertEqual(self.watch()["generation"], baseline["generation"])
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(len(self.notices()), 3)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), remote)
        self.decide.assert_not_called()
        self.model.assert_not_called()

    def test_feedback_poll_sees_a_trusted_reply_received_during_settlement(self):
        self.cfg.unblock_repos = ()
        self.keep("shared.txt")
        self.advance("shared.txt")
        self.seed()
        with patch.object(util, "now", return_value=util.now()):
            held = self.resume_with_settlement_reply(self.previous, 1)
        self.assert_held(held)
        insights_store.add_feedback(outcomes.owner_feedback(self.url, "OPEN", self.repo.name, self.tid))
        layer = config.load_layer(self.repo.layer_dir)
        discovery = poll.routing.Discovery(self.cfg, self.repo, priced=False, layer=layer)
        queue = poll.RepoQueue(self.repo, self.source, set(), layer, discovery)
        skips = pipeline.SkipLog({})

        def spawn_feedback(repo, tid, readmission):
            self.assertEqual(pipeline.run_command(repo, tid)[-2:], ["--continue", held["id"]])
            self.assert_held(self.resume(held["id"]))
            return True

        with (patch.object(pipeline, "FEEDBACK_QUIET_MINUTES", 0),
              patch.object(poll, "spawn_unparked", side_effect=spawn_feedback) as start):
            pick = poll.next_pick(self.repo, self.source, skips, set())
            self.assertTrue(pick.from_feedback)
            self.assertEqual(poll.start_pick(self.cfg, queue, pick, skips, False, Counter(), 1), "started")
            self.assertIsNone(poll.next_pick(self.repo, self.source, skips, set()))
            start.assert_called_once()
        self.decide.assert_not_called()
        self.model.assert_not_called()


class MultiCheckoutFixture(ResumeFixture):
    part = "first"

    def sibling(self, *, ignore=""):
        side, other = self.make_checkout("second", "example/second", ignore=ignore)
        self.repo.parts.append(config.Part(side.dir, side.github))
        self.job.checkouts.append(side)
        return side, other

    def advance_sibling(self, other, name="side-race.txt"):
        head = self.commit(other, name, "new sibling remote head\n")
        self.git(other, "push", "origin", "topic")
        return head


class MultiCheckoutRecoveryTests(MultiCheckoutFixture):
    def test_unchanged_sibling_head_with_a_conflicting_base_holds_before_installing(self):
        side, other = self.sibling()
        name = "shared.txt"
        retained = self.commit(side.wt, name, "retained sibling edit\n", owner=self.previous)
        local = self.keep()
        external = self.advance()
        self.seed()
        base = self.commit(side.src, name, "new base edit\n")
        self.git(side.src, "push", "origin", "main")

        row = self.resume(self.previous)
        held = self.assert_held(row)
        self.assertEqual((held["github"], held["local"]), (side.github, retained))
        self.assertEqual((held["refs"]["head"], held["refs"]["base"]), (side.lease, base))
        for text in (repr(name), retained, base, "Resolve the named files in a separate checkout"):
            self.assertIn(text, row["summary"])
        self.assertEqual(self.git(side.wt, "rev-parse", "HEAD"), retained)
        self.assertEqual(self.git(side.wt, "status", "--porcelain"), "")
        self.assertEqual((side.wt / name).read_text(), "retained sibling edit\n")
        self.assertEqual(self.git(side.src, "show", f"{base}:{name}"), "new base edit")
        remote = self.pr(side.pr)
        self.assertEqual((remote["headRefOid"], remote["baseRefOid"]), (side.lease, base))
        self.assertEqual(self.git(other, "rev-parse", "HEAD"), side.lease)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), external)
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.assertEqual(len(self.notices()), 1)
        self.spawn.assert_not_called()
        self.decide.assert_not_called()
        self.model.assert_not_called()

    def test_sibling_base_ignore_change_does_not_expose_retained_file(self):
        name = "side-private.txt"
        side, _ = self.sibling(ignore=f"/{name}\n")
        secret = b"dummy sibling contents that should not appear\n"
        (side.wt / name).write_bytes(secret)
        self.keep()
        self.advance()
        self.seed()
        base = self.commit(side.src, ".gitignore", "# no ignores\n")
        self.git(side.src, "push", "origin", "main")

        row = self.resume(self.previous)
        held = self.assert_held(row)
        self.assertEqual(held["github"], side.github)
        self.assertEqual(held["refs"]["base"], base)
        self.assertIn(repr(name), row["summary"])
        self.assertNotIn(secret.decode().strip(), row["summary"])
        self.assertEqual(self.git(side.wt, "rev-parse", "HEAD"), side.lease)
        self.assertIn(name, workspace.ignored_file_paths(side.wt))
        self.assertEqual(self.git(side.wt, "diff", "--cached"), "")
        self.assertEqual((side.wt / name).read_bytes(), secret)
        self.decide.assert_not_called()

    def test_unchanged_sibling_race_while_settling_is_a_no_effect_hold(self):
        side, other = self.sibling()
        retained = self.commit(side.wt, "side-local.txt", "retained sibling\n", owner=self.previous)
        local = self.keep()
        external = self.advance()
        self.seed()
        controllers = self.gate_publication("test -f first/local.txt && test -f first/other.txt && test -f second/side-local.txt")
        settle = flows.settle_unpushed_checkouts
        moved = []

        def race(job, *args):
            self.assertFalse(job.checkouts[1].recovery_head)
            moved.append(self.advance_sibling(other))
            return settle(job, *args)

        with patch.object(flows, "settle_unpushed_checkouts", side_effect=race):
            row = self.resume(self.previous)
        held = self.assert_held(row)
        raced, = moved
        for text in ("while settling the retained commits", side.lease, raced, retained, "no push was made"):
            self.assertIn(text, row["summary"])
        self.assertEqual(held["expected_refs"]["origin"], side.lease)
        self.assertEqual(held["refs"]["origin"], raced)
        self.assertNotIn("pushed", held)
        ctl, = controllers
        self.assertEqual(orchestration.actions(ctl.lease)[-1]["state"], "failed")
        self.assertEqual(self.remote_head(), external)
        self.assertEqual(self.git(side.wt, "rev-parse", "HEAD"), retained)
        self.assertEqual(self.pr(side.pr)["headRefOid"], raced)
        self.assertTrue(pr_recovery.ancestor(self.wt, local, self.git(self.wt, "rev-parse", "HEAD")))
        baseline = self.watch()
        self.assertEqual(baseline["phase"], "needs_owner")
        self.assertEqual(self.watch()["generation"], baseline["generation"])
        self.assertEqual(len(self.notices()), 1)
        self.spawn.assert_not_called()
        changed = self.advance_sibling(other, "changed-again.txt")
        self.watch()
        attempt, = self.launched
        self.assertEqual(ledger.attempt_of(attempt)["grant"]["binding"]["pr_recovery"]["refs"]["origin"], changed)
        self.model.assert_not_called()
        self.decide.assert_called_once()


class MultiCheckoutPublicationRaceTests(MultiCheckoutFixture):
    def test_second_pass_pre_push_race_reports_first_pass(self):
        self.check_second_pass_race("pre-push")

    def test_second_pass_rejected_lease_reports_first_pass(self):
        self.check_second_pass_race("lease")

    def test_second_pass_uncertain_push_requires_reconciliation(self):
        self.check_second_pass_race("uncertain")

    def check_second_pass_race(self, race):
        side, other = self.sibling()
        name = "side-local.txt"
        (side.wt / name).write_text("fresh sibling edits\n")
        local = self.keep()
        external = self.advance()
        self.seed()
        controllers = self.gate_publication(f"test -f first/local.txt && test -f first/other.txt && test -f second/{name}")
        publish_branch, sh = flows.publish_branch, util.sh
        heads, moved = {}, []

        def race_second_pass(job, *args, **kw):
            self.assertFalse(job.checkouts[1].recovery_head)
            self.assertEqual(heads.keys(), {self.wt})
            self.assertEqual(self.remote_head(), heads[self.wt])
            self.assertEqual(orchestration.find(self.previous)["data"]["publication_intent"]["pushed"],
                             {self.repo.github: heads[self.wt]})
            if race == "pre-push":
                moved.append(self.advance_sibling(other))
            return publish_branch(job, *args, **kw)

        def racing_push(argv, **kwargs):
            if argv[:3] != ["git", "push", "--porcelain"]:
                return sh(argv, **kwargs)
            wt = kwargs["cwd"]
            heads[wt] = self.git(wt, "rev-parse", "HEAD")
            if wt == side.wt:
                self.assertIn(f"--force-with-lease=refs/heads/topic:{side.lease}", argv)
                if race == "lease":
                    moved.append(self.advance_sibling(other))
                elif race == "uncertain":
                    res = sh(argv, **kwargs)
                    self.assertEqual(res.returncode, 0, res.stderr)
                    moved.append(heads[wt])
                    return CompletedProcess(argv, util.TIMED_OUT, "", "fixture lost the result")
            return sh(argv, **kwargs)

        with (patch.object(flows, "publish_branch", side_effect=race_second_pass),
              patch.object(util, "sh", side_effect=racing_push)):
            row = self.resume(self.previous)
        held = self.assert_held(row, pending=True)
        raced, = moved
        first = heads[self.wt]
        expected = {self.repo.github: first}
        retained = self.git(side.wt, "rev-parse", "HEAD")
        phase = {"pre-push": "before pushing the candidate", "lease": "during the leased push",
                 "uncertain": "did not confirm publication"}[race]
        for text in (phase, side.lease, raced, retained, f"Already pushed {self.repo.github}:topic@{first}"):
            self.assertIn(text, row["summary"])
        self.assertNotIn("no push was made", row["summary"])
        self.assertEqual(held["expected_refs"]["head"], side.lease)
        self.assertEqual(held["refs"]["origin"], raced)
        self.assertEqual(held["pushed"], expected)
        self.assertEqual(bool(held.get("publication_unconfirmed")), race == "uncertain")
        self.assertEqual(side.wt in heads, race != "pre-push")
        ctl, = controllers
        action = orchestration.actions(ctl.lease)[-1]
        self.assertEqual(action["state"], "reconciliation-required")
        self.assertEqual(action["result"]["pushed"], expected)
        intent = orchestration.find(row["id"])["data"]["publication_intent"]
        self.assertEqual(intent["pushed"], expected)
        self.assertEqual(self.remote_head(), first)
        self.assertTrue(all(pr_recovery.ancestor(self.wt, oid, first) for oid in (local, external)))
        self.assertEqual(self.pr(side.pr)["headRefOid"], raced)
        self.assertTrue(pr_recovery.ancestor(side.wt, side.lease, retained))
        self.assertEqual(self.git(side.wt, "show", f"{retained}:{name}"), "fresh sibling edits")
        if race != "uncertain":
            self.assertEqual(self.git(other, "show", "HEAD:side-race.txt"), "new sibling remote head")
        else:
            self.git(other, "pull", "--ff-only", "origin", "topic")
        self.advance_sibling(other, "head-after-hold.txt")
        self.watch()
        self.spawn.assert_not_called()
        self.model.assert_not_called()
        self.decide.assert_called_once()

    def test_second_leased_push_race_reports_first_push_and_requires_reconciliation(self):
        side, other = self.sibling()
        side_local = self.commit(side.wt, "side-local.txt", "retained sibling\n", owner=self.previous)
        local = self.keep()
        external = self.advance()
        side_external = self.commit(other, "side-other.txt", "second session\n")
        self.git(other, "push", "origin", "topic")
        self.seed()
        controllers = self.gate_publication("test -f first/local.txt && test -f first/other.txt && "
                                            "test -f second/side-local.txt && test -f second/side-other.txt")
        sh = util.sh
        pushes, moved = [], []

        def racing_push(argv, **kwargs):
            if argv[:3] == ["git", "push", "--porcelain"]:
                wt = kwargs["cwd"]
                pushes.append((wt, self.git(wt, "rev-parse", "HEAD")))
                if wt == side.wt:
                    self.assertIn(f"--force-with-lease=refs/heads/topic:{side_external}", argv)
                    self.assertEqual(self.remote_head(), pushes[0][1])
                    moved.append(self.commit(other, "late.txt", "new remote head\n"))
                    self.git(other, "push", "origin", "topic")
            return sh(argv, **kwargs)

        with patch.object(util, "sh", side_effect=racing_push):
            row = self.resume(self.previous)
        held = self.assert_held(row, pending=True)
        (first_wt, first_head), (second_wt, second_head) = pushes
        self.assertEqual((first_wt, second_wt), (self.wt, side.wt))
        raced, = moved
        for text in ("during the leased push", side_external, raced, second_head,
                     f"Already pushed {self.repo.github}:topic@{first_head}", "Reconcile the partial publication"):
            self.assertIn(text, row["summary"])
        self.assertEqual(held["expected_refs"]["head"], side_external)
        self.assertEqual(held["refs"]["origin"], raced)
        self.assertEqual(held["pushed"], {self.repo.github: first_head})
        ctl, = controllers
        action = orchestration.actions(ctl.lease)[-1]
        self.assertEqual(action["state"], "reconciliation-required")
        self.assertEqual(action["result"]["pushed"], held["pushed"])
        self.assertEqual(self.remote_head(), first_head)
        self.assertEqual(self.pr(side.pr)["headRefOid"], raced)
        self.assertEqual(self.git(side.wt, "rev-parse", "HEAD"), second_head)
        self.assertTrue(all(pr_recovery.ancestor(self.wt, oid, first_head) for oid in (local, external)))
        self.assertTrue(all(pr_recovery.ancestor(side.wt, oid, second_head) for oid in (side_local, side_external)))
        self.pr_comments.append({"id": 1, "user": {"login": "keeper"}, "body": "Try again without reconciliation."})
        self.watch()
        self.spawn.assert_not_called()


class SiblingIgnoreRecoveryTests(MultiCheckoutFixture):
    def test_sibling_comment_only_ignore_change_resumes_with_private_file_still_ignored(self):
        name = "side-private.txt"
        rule = f"/{name}\n"
        side, other = self.sibling(ignore=rule)
        private = side.wt / name
        private.write_text("retained private notes\n")
        self.keep()
        self.advance()
        self.seed()
        rules_with_comment = f"# Added a comment\n{rule}"
        base = self.commit(side.src, ".gitignore", rules_with_comment)
        self.git(side.src, "push", "origin", "main")

        def recovered(ctl):
            candidate = ctl.job.checkouts[1]
            self.assertEqual(candidate.recovery_base, base)
            self.assertTrue(pr_recovery.ancestor(side.wt, base, "HEAD"))
            self.assertEqual((side.wt / ".gitignore").read_text(), rules_with_comment)
            self.assertIn(name, workspace.ignored_file_paths(side.wt))
            self.assertNotIn(candidate.prefix + name, ctl.job.b.restage().files)
            ctl.ending = ("noop", "Resumed with the sibling's new base", "")
            return []

        self.decide.side_effect = recovered
        self.resume(self.previous, expected=0)
        self.decide.assert_called_once()
        self.assertEqual(private.read_text(), "retained private notes\n")
        self.assertEqual(self.git(side.wt, "ls-files", "--", name), "")
        self.assertEqual(self.git(other, "rev-parse", "HEAD"), side.lease)


class MultiCheckoutInstallTests(MultiCheckoutFixture):
    def test_later_install_failure_keeps_successful_checkout_provenance_for_next_resume(self):
        side, other = self.sibling()
        self.checkout.onto = self.accepted
        side_local = self.commit(side.wt, "side-local.txt", "retained sibling\n", owner=self.previous)
        local = self.keep()
        external = self.advance()
        side_external = self.commit(other, "side-other.txt", "second session\n")
        self.git(other, "push", "origin", "topic")
        self.seed()
        git = util.git

        def fail_second_install(args, cwd, **kw):
            if cwd == side.wt and "--ff-only" in args:
                raise RuntimeError("fixture refused the fast-forward")
            return git(args, cwd, **kw)

        with patch.object(util, "git", side_effect=fail_second_install):
            row = self.resume(self.previous)
        held = self.assert_held(row)
        self.assertEqual(held["github"], side.github)
        self.assertIn("refused candidate", row["summary"])
        self.decide.assert_not_called()
        actual = self.git(self.wt, "rev-parse", "HEAD")
        self.assertNotEqual(actual, local)
        self.assertTrue(pr_recovery.ancestor(self.wt, external, actual))
        self.assertEqual(self.git(side.wt, "rev-parse", "HEAD"), side_local)
        self.assertEqual(self.pr(side.pr)["headRefOid"], side_external)
        made = workspace.made_before(self.ws)
        self.assertEqual([workspace.head_recorded(made, c) for c in self.job.checkouts], [actual, side_local])
        self.assertEqual(workspace.onto_recorded(made, self.checkout), "")
        saved = flows.prepared_data(row["id"])["checkouts"]
        self.assertEqual([c["recovery_head"] for c in saved], [external, ""])
        self.assertEqual(self.watch()["phase"], "needs_owner")

        def recovered(ctl):
            self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), actual)
            self.assertTrue(pr_recovery.ancestor(side.wt, side_external, self.git(side.wt, "rev-parse", "HEAD")))
            ctl.ending = ("noop", "Reached the next decision after recovering both checkouts", "")
            return []

        self.decide.side_effect = recovered
        self.resume(row["id"], expected=0)
        self.decide.assert_called_once()
