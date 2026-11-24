from datetime import timedelta
from functools import partialmethod
from unittest import TestCase
from unittest.mock import Mock, patch

from arete import config, gh, jev, outcomes, pipeline, poll, pr_recovery, tasks, unblock, util, workspace
from arete.store import insights
from arete.store import tasks as task_store
from tests.support import isolate_live_state as setUpModule
from tests.test_pr_recovery import MultiCheckoutFixture, RecoveryFixture


class IgnoreSafetyTests(RecoveryFixture):
    def keep_with_excludes(self, initial, source_rules, retained_rules, replacement):
        self.accepted = self.advance(".gitignore", initial)
        self.git(self.wt, "pull", "--ff-only", "origin", "topic")
        self.checkout.note_pushed_head(self.accepted)
        source = self.root / "source-excludes"
        source.write_text(source_rules)
        (self.wt.parent / "retained-excludes").write_text(retained_rules)
        self.git(self.src, "config", "extensions.worktreeConfig", "true")
        self.git(self.src, "config", "core.excludesFile", str(source))
        self.git(self.wt, "config", "--worktree", "core.excludesFile", "../retained-excludes")
        notes = self.wt / "retained notes.txt"
        notes.write_bytes(b"retained bytes\x00")
        return notes, self.keep(), self.advance(".gitignore", replacement)

    def check_excludes(self, *, private):
        rule = "/retained notes.txt\n"
        rules = (rule, rule, "") if private else ("!" + rule, "", rule)
        notes, local, remote = self.keep_with_excludes(*rules, "")
        self.job.layer.deny = [notes.name]
        why = "expose retained ignored files" if private else "hide retained untracked files"
        with self.assertRaisesRegex(pr_recovery.Hold, f"{why}: {notes.name!r}") as caught:
            self.reclaim()
        self.assertIn("Save the named files separately", str(caught.exception))
        self.assert_heads(local, remote)
        self.assert_loose(notes, b"retained bytes\x00", private=private)
        self.assertEqual(self.git(self.wt, "diff", "--cached"), "")
        if private:
            self.assertNotIn("retained bytes", str(caught.exception))
            staged = self.job.b.restage(from_base=True)
            self.assertNotIn(notes.name, staged.files)
            self.assertNotIn("retained bytes", staged.patch)

    test_retained_excludes_override_does_not_expose_private_bytes = partialmethod(check_excludes, private=True)
    test_relative_retained_excludes_do_not_hide_untracked_file = partialmethod(check_excludes, private=False)

    def test_recovery_holds_if_the_retained_fallback_disappears_during_installation(self):
        rule = "/retained notes.txt\n"
        notes, local, remote = self.keep_with_excludes(rule, "# no ignores\n", rule, "")
        self.reclaim()
        self.assert_heads(local, remote)
        self.assert_loose(notes, b"retained bytes\x00", private=True)
        why = f"expose retained ignored files: {notes.name!r}"
        exclude = self.wt.parent / "retained-excludes"
        with self.on_git("merge --no-autostash", lambda _: exclude.write_text("")), self.assertRaisesRegex(pr_recovery.Hold, why):
            self.job.install_pr_plans()
        candidate = self.job.pr_plans[0].candidate
        self.assert_heads(candidate, remote)
        self.assert_contains(self.wt, candidate, local, remote)
        self.assertEqual(workspace.head_recorded(workspace.made_before(self.ws), self.checkout), candidate)
        self.assert_staging_refused(why)
        self.assert_loose(notes, b"retained bytes\x00")
        self.assertEqual(self.git(self.wt, "diff", "HEAD", "--name-only"), "")


class TicketReplyCutoffTests(TestCase):
    def test_same_second_guidance_requires_a_snapshot_without_the_reply(self):
        since = "2026-11-24T12:00:00Z"
        for snapshot in ({}, {"seen_ticket_replies": None}, {"seen_ticket_replies": ["1"]}, {"seen_ticket_replies": []}):
            with self.subTest(snapshot=snapshot):
                held = {"held_at": since, **snapshot}
                replies = [(str(i), f"2026-11-24T{stamp}Z", "Guidance")
                           for i, stamp in enumerate(("11:59:59", "12:00:00", "12:00:01"))]
                self.assertEqual([unblock.ticket_answer_after_hold({}, held, [reply], []) for reply in replies],
                                 [False, snapshot.get("seen_ticket_replies") == [], True])


class HeldReplyTests(MultiCheckoutFixture):
    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(patch.object(util, "now", return_value=util.now()))
        self.comments, self.reviews = {}, []
        self.enterContext(patch.object(gh, "pr_discussion", side_effect=lambda url: gh.Discussion(
            [], self.reviews if url == self.url else [], self.comments.get(url, []))))
        self.addCleanup(self.decide.assert_not_called)

    def reply(self, number, body, *, url=None, login="keeper", age=0):
        self.comments.setdefault(url or self.url, []).append({"id": number, "body": body, "user": {"login": login},
                                                             "created_at": util.iso(util.now() - timedelta(seconds=age))})

    def ordinary(self):
        self.cfg.unblock_repos, self.repo.pipeline = (), True
        self.cfg.jev = config.JevConfig(enabled=True, feedback_skip=0.5)
        self.enterContext(patch.multiple(pipeline, FEEDBACK_QUIET_MINUTES=0, SKIPPED_FILE=self.root / "skipped.json"))
        self.enterContext(patch.object(outcomes, "PR_SEEN_FILE", self.root / "pr_seen.json"))
        self.enterContext(patch.multiple(poll, network_up_or_skip=Mock(return_value=True), retry_scratch=Mock(),
                                         mark_loop=Mock()))
        self.enterContext(patch.object(tasks, "spawn_pending", side_effect=self.spawn_boundary))
        self.enterContext(patch.object(jev, "key", return_value="fixture-key"))
        self.judge = self.enterContext(patch.object(jev, "ask_many", side_effect=self.classify))
        self.spawn.side_effect = self.continue_held

    def continue_held(self, command, log):
        self.assertEqual(command[-4:], [self.repo.name, self.tid, "--continue", self.held["id"]])
        self.held = self.resume(self.held["id"])
        self.assert_held(self.held)

    def classify(self, items, cfg, *, trace):
        scores = (0.1 if prompt.endswith("\n\nComment:\nThanks for the example.") else 0.9 for prompt, _ in items)
        return [jev.Reply({"asks": jev.Answer(p > 0.5, 0.9, {"yes": p})}) for p in scores]

    def assert_stable(self, enrolled, launches=0, *, requeue=False):
        notices = self.notices()
        for _ in range(2):
            if enrolled:
                self.assertEqual(self.watch()["phase"], "needs_owner")
            else:
                if requeue:
                    task_store.task_set_state(self.tid, "todo")
                self.assertEqual(poll.poll(), 0)
        self.assertEqual((self.spawn.call_count, self.notices()), (launches, notices))

    def retry_held(self, enrolled):
        if enrolled:
            prior = len(self.launched)
            self.watch()
            attempt, = self.launched[prior:]
            self.held = self.resume_attempt(self.held, attempt)
        else:
            self.assertEqual(poll.poll(), 0)

    def hold_conflict(self, *, enrolled=True, snapshot=True):
        if not enrolled:
            self.ordinary()
        self.seed_conflict()
        self.held = self.resume(self.previous) if snapshot else self.resume_without_discussion()
        self.assertEqual(len(self.notices()), 1)
        return self.assert_held(self.held) if snapshot else pr_recovery.blocked(self.held)

    def test_enrolled_attempt_does_not_consume_new_guidance_during_settlement(self):
        self.assertEqual(self.hold_conflict()["seen_replies"], [])
        self.assert_stable(True)
        self.reply(1, "First guidance")
        hold_open_pr = pipeline.hold_open_pr

        def settle(run, error, kept):
            self.assertTrue(run.pr_recovery_discussion[error.recovery["pr"]].decision)
            self.reply(2, "New settlement guidance")
            return hold_open_pr(run, error, kept)

        with patch.object(pipeline, "hold_open_pr", side_effect=settle):
            self.retry_held(True)
        self.retry_held(True)
        self.assert_stable(True, 2)

    def check_pr_answer(self, *, enrolled, snapshot=True, primary=False):
        pr = self.url if primary else self.sibling()[0].pr
        for url in ((pr,) if snapshot else (self.url, pr)):
            self.reply(1, "Earlier reply", url=url, age=1)
            self.reply(2, "Same-second baseline reply", url=url)
        failure = self.hold_conflict(enrolled=enrolled, snapshot=snapshot)
        seen = {self.url: [], pr: [["comment", 1], ["comment", 2]]} if snapshot else dict.fromkeys((self.url, pr))
        self.assertEqual((failure["seen_replies_by_pr"], failure["seen_replies"], failure["held_at"]),
                         (seen, seen[self.url], util.iso()))
        keys = {("comment", n) for n in (1, 2, 4)}
        if not snapshot:
            self.assertEqual(failure["decision"], "")
            self.reply(5, "Ambiguous same-second guidance", url=pr)
            keys.add(("comment", 5))
            self.assert_stable(enrolled)
            self.clock.return_value += timedelta(seconds=1)
        self.reply(3, "Untrusted guidance", url=pr, login="stranger")
        self.assert_stable(enrolled)
        if not enrolled:
            self.judge.assert_not_called()
        if primary:
            keys.add(("review", 2))
            self.reviews.append({"id": 2, "user": {"login": "keeper"}, "body": "Thanks for the example.",
                                 "state": "COMMENTED", "submitted_at": util.iso()})
            self.assertEqual(poll.poll(), 0)
            self.spawn.assert_not_called()
        self.reply(4, "Keep both versions", url=pr)
        self.retry_held(enrolled)
        self.assert_stable(enrolled, 1)
        if not enrolled:
            judged = [p.split("\n\nComment:\n", 1)[1] for call in self.judge.call_args_list for p, _ in call.args[0]]
            self.assertEqual(judged, (["Thanks for the example."] if primary else []) + ["Keep both versions"])
            self.assertEqual(self.judge.call_count, 2 if primary else 1)
            self.assertEqual(insights.feedback_keys(pr), keys)
        if primary or not snapshot:
            self.assertEqual(len(self.notices()), 2)

    test_enrolled_watch_retries_for_later_sibling_reply = partialmethod(check_pr_answer, enrolled=True, snapshot=False)
    test_ordinary_poll_retries_for_later_sibling_reply = partialmethod(check_pr_answer, enrolled=False, snapshot=False)
    test_enrolled_watch_retries_for_same_second_sibling_reply = partialmethod(check_pr_answer, enrolled=True)
    test_ordinary_poll_retries_for_same_second_sibling_reply = partialmethod(check_pr_answer, enrolled=False)
    test_ordinary_poll_distinguishes_primary_comment_and_review = partialmethod(check_pr_answer, enrolled=False, primary=True)

    def assert_ticket_snapshot(self, failure, expected_count):
        replies = unblock.ticket_reply_rows(self.source, self.tid)
        self.assertEqual(len(replies), expected_count)
        self.assertEqual(failure["held_at"], util.iso())
        self.assertEqual(failure["seen_ticket_replies"], unblock.ticket_reply_keys(replies))
        self.assertEqual(failure["decision"], unblock.recovery_decision_for([], replies, []))

    def check_same_second_ticket_answer(self, *, enrolled):
        if enrolled:
            self.assertTrue(task_store.task_comment(self.tid, "owner", "Captured tracker guidance"))
        self.assert_ticket_snapshot(self.hold_conflict(enrolled=enrolled), int(enrolled))
        self.assertTrue(task_store.task_comment(self.tid, "service:untrusted", "Untrusted tracker guidance"))
        self.assert_stable(enrolled, requeue=not enrolled)

        self.assertTrue(task_store.task_comment(self.tid, "owner", "Keep both versions for review."))
        if not enrolled:
            task_store.task_set_state(self.tid, "failed")
            self.assert_stable(False)
            task_store.task_set_state(self.tid, "todo")
        self.retry_held(enrolled)
        self.assert_ticket_snapshot(self.assert_held(self.held), int(enrolled) + 1)
        self.assert_stable(enrolled, 1, requeue=not enrolled)
        self.assertEqual(len(self.notices()), 2)
        if not enrolled:
            remote = self.advance()
            self.retry_held(False)
            self.assertEqual(self.assert_held(self.held)["refs"]["head"], remote)
            self.assert_stable(False, 2, requeue=True)
            self.assertEqual(len(self.notices()), 3)
            self.judge.assert_not_called()

    test_enrolled_watch_readmits_same_second_ticket_answer = partialmethod(check_same_second_ticket_answer, enrolled=True)
    test_ordinary_requeue_readmits_same_second_ticket_answer = partialmethod(check_same_second_ticket_answer, enrolled=False)
