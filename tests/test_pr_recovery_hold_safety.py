from collections import Counter
from unittest.mock import patch

from arete import config, outcomes, pipeline, poll, pr_recovery, util, workspace
from arete.store import insights
from arete.store import unblock as ledger
from tests import support
from tests.test_pr_recovery import RecoveryFixture, ResumeFixture


def setUpModule():
    support.isolate_live_state()


class IgnoreSafetyTests(RecoveryFixture):
    def excludes(self, source_rules, retained_rules):
        source = self.root / "source-excludes"
        retained = self.wt.parent / "retained-excludes"
        source.write_text(source_rules)
        retained.write_text(retained_rules)
        self.git(self.src, "config", "extensions.worktreeConfig", "true")
        self.git(self.src, "config", "core.excludesFile", str(source))
        self.git(self.wt, "config", "--worktree", "core.excludesFile", "../retained-excludes")

    def accept_rule(self, rule):
        self.accepted = self.advance(".gitignore", rule)
        self.git(self.wt, "pull", "--ff-only", "origin", "topic")
        self.checkout.note_pushed_head(self.accepted)

    def test_retained_excludes_override_does_not_expose_private_bytes(self):
        self.accept_rule("/notes.txt\n")
        self.excludes("/notes.txt\n", "# no ignores\n")
        private = self.wt / "notes.txt"
        secret = b"dummy private bytes"
        private.write_bytes(secret)
        local = self.keep()
        remote = self.advance(".gitignore", "# no ignores\n")

        with self.assertRaisesRegex(pr_recovery.Hold, "expose retained ignored files: 'notes.txt'") as caught:
            self.reclaim()
        self.assertNotIn(secret.decode(), str(caught.exception))
        self.assertEqual(private.read_bytes(), secret)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertEqual(self.remote_head(), remote)
        staged = self.job.b.restage(from_base=True)
        self.assertNotIn(private.name, staged.files)
        self.assertNotIn(secret.decode(), staged.patch)

    def test_relative_retained_excludes_do_not_hide_untracked_file(self):
        self.accept_rule("!/notes.txt\n")
        self.excludes("# no ignores\n", "/notes.txt\n")
        notes = self.wt / "notes.txt"
        notes.write_bytes(b"retained untracked bytes")
        local = self.keep()
        self.advance(".gitignore", "# removed the negation\n")

        with self.assertRaisesRegex(pr_recovery.Hold, "hide retained untracked files: 'notes.txt'"):
            self.reclaim()
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), local)
        self.assertIn(notes.name, workspace.untracked_files(self.wt))
        self.assertEqual(notes.read_bytes(), b"retained untracked bytes")

    def test_recovery_can_install_when_retained_excludes_keep_the_file_ignored(self):
        self.accept_rule("/notes.txt\n")
        self.excludes("# no ignores\n", "/notes.txt\n")
        notes = self.wt / "notes.txt"
        notes.write_bytes(b"dummy private bytes")
        local = self.keep()
        remote = self.advance(".gitignore", "# no tracked ignores\n")

        self.reclaim()
        self.job.install_pr_plans()
        candidate = self.git(self.wt, "rev-parse", "HEAD")
        self.assertTrue(pr_recovery.ancestor(self.wt, local, candidate))
        self.assertTrue(pr_recovery.ancestor(self.wt, remote, candidate))
        self.assertIn(notes.name, workspace.ignored_file_paths(self.wt))
        self.assertEqual(notes.read_bytes(), b"dummy private bytes")
        self.assertNotIn(notes.name, self.job.b.restage(from_base=True).files)


class SettlementReplyTests(ResumeFixture):
    def conflict(self):
        self.keep("shared.txt")
        self.advance("shared.txt")
        self.seed()

    def reply(self, comment_id, body, *, login="keeper"):
        self.pr_comments.append({"id": comment_id, "user": {"login": login}, "body": body, "created_at": util.iso()})

    def resume_with_settlement_reply(self, previous, *, attempt=""):
        hold_open_pr = pipeline.hold_open_pr

        def settle(run, error, kept):
            self.assertTrue(run.pr_recovery_cutoff)
            self.assertTrue(run.pr_recovery_discussion[error.recovery["pr"]].decision)
            self.reply(2, "New settlement guidance")
            self.reply(3, "Ignore the conflict", login="stranger")
            return hold_open_pr(run, error, kept)

        # Keep the replies and baseline in the same timestamp second; their order comes from the snapshot.
        with (patch.object(util, "now", return_value=util.now()),
              patch.object(pipeline, "hold_open_pr", side_effect=settle)):
            return self.resume(previous, recovery=attempt)

    def test_enrolled_attempt_does_not_consume_new_guidance_during_settlement(self):
        self.conflict()
        held = self.resume(self.previous)
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.spawn.assert_not_called()
        self.reply(1, "First guidance")
        self.watch()
        first, = self.launched
        child = self.resume_with_settlement_reply(held["id"], attempt=first)
        self.assert_held(child)
        self.assertEqual(ledger.attempt_of(first)["child"], child["id"])

        self.watch()
        _, second = self.launched
        retried = self.resume(child["id"], recovery=second)
        self.assert_held(retried)
        notices = self.notices()
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.reply(4, "Untrusted follow-up", login="stranger")
        self.assertEqual(self.watch()["phase"], "needs_owner")
        self.assertEqual(self.notices(), notices)
        self.assertEqual(self.launched, [first, second])
        self.decide.assert_not_called()
        self.model.assert_not_called()

    def test_unenrolled_feedback_poll_picks_up_only_new_settlement_reply_once(self):
        self.cfg.unblock_repos = ()
        self.conflict()
        with patch.object(util, "now", return_value=util.now()):
            self.reply(1, "Baseline guidance")
            baseline = outcomes.owner_feedback(self.url, "OPEN", self.repo.name, self.tid)
            held = self.resume_with_settlement_reply(self.previous)
        failure = self.assert_held(held)
        self.assertEqual(failure["seen_replies"], [["comment", 1]])
        self.assertEqual({reply["created_at"] for reply in self.pr_comments}, {failure["held_at"]})
        layer = config.load_layer(self.repo.layer_dir)
        queue = poll.RepoQueue(self.repo, self.source, set(), layer,
                               poll.routing.Discovery(self.cfg, self.repo, priced=False, layer=layer))
        skips = pipeline.SkipLog({})

        def collect_and_pick():
            insights.add_feedback(outcomes.owner_feedback(self.url, "OPEN", self.repo.name, self.tid))
            return poll.next_pick(self.repo, self.source, skips, set())

        def resume_feedback(repo, tid, readmission):
            self.assertIsNone(readmission)
            self.assert_held(self.resume(held["id"]))
            return True

        with (patch.object(pipeline, "FEEDBACK_QUIET_MINUTES", 0),
              patch.object(poll, "spawn_unparked", side_effect=resume_feedback) as start):
            self.assertEqual(insights.add_feedback(baseline), 1)
            pick = poll.next_pick(self.repo, self.source, skips, set())
            self.assertTrue(pick.from_feedback)
            self.assertEqual(poll.start_pick(self.cfg, queue, pick, skips, False, Counter(), 1), "skipped")
            start.assert_not_called()

            pick = collect_and_pick()
            self.assertTrue(pick.from_feedback)
            self.assertEqual(poll.start_pick(self.cfg, queue, pick, skips, False, Counter(), 1), "started")
            self.reply(4, "Untrusted follow-up", login="stranger")
            self.assertIsNone(collect_and_pick())
            self.assertEqual(poll.start_pick(self.cfg, queue, pick, skips, False, Counter(), 1), "skipped")
            start.assert_called_once()
        self.spawn.assert_not_called()
        self.decide.assert_not_called()
        self.model.assert_not_called()
