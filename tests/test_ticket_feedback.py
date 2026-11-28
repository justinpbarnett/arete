import contextlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import isolate_live_state, seed_db
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

from arete import (
    config,
    enrollment,
    gh,
    jira,
    paths,
    pipeline,
    poll,
    record,
    release_promotion,
    release_proof,
    ticket_feedback as tf,
    tracker,
)
from arete.store import asks as ask_store
from arete.store import db as foundation
from arete.store import feedback as feedback_store
from arete.store import specs as spec_store

def setUpModule():
    isolate_live_state()


class _FeedbackDbCase(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="arete-tf-"))).resolve()
        self.root = root
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        state = root / "state"
        for name, value in {
            "STATE_DIR": state,
            "DB_FILE": state / "arete.db",
            "CONFIG_DIR": root / "config",
            "CONFIG_FILE": root / "config" / "config.toml",
            "RUNS_DIR": state / "runs",
            "WORKTREES_DIR": state / "worktrees",
            "LOCKS_DIR": state / "locks",
            "LOG_DIR": state / "logs",
        }.items():
            patches.enter_context(mock.patch.object(paths, name, value))
        patches.enter_context(mock.patch.object(foundation, "_CHECKED", set()))
        paths.ensure_dirs()
        seed_db(state)


class CommenterAuthorizationTests(_FeedbackDbCase):
    def test_github_allowlist_defaults_to_owner_and_ignores_outside_commenters(self):
        repo = config.Repo("personal", self.root, github="justinpbarnett/personal", pipeline=True, tracker="github")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        self.assertEqual(tk.commenter_allowlist(), ("justinpbarnett",))

        outside = tf.ThreadComment("101", "stranger", "stranger", "Please change the header to Foo.",
                                   "https://github.com/justinpbarnett/personal/issues/7#issuecomment-101",
                                   "2026-11-27T10:05:00Z")
        owner = tf.ThreadComment("102", "justinpbarnett", "justinpbarnett", "Please keep the selected filter after reload.",
                                 "https://github.com/justinpbarnett/personal/issues/7#issuecomment-102",
                                 "2026-11-27T10:06:00Z")
        ticket = tracker.Ticket("7", "#7", "Filter state", body="Preserve filter", open=True, labels=["agent"])

        outcomes = tf.consume_ticket(cfg, repo, "7", tk=tk, ticket=ticket, comments=[outside, owner], since="")
        self.assertEqual([(o.comment_id, o.authorized, o.route, o.status) for o in outcomes], [
            ("101", False, "none", "ignored"),
            ("102", True, "worker", "queued"),
        ])
        self.assertEqual(feedback_store.tickets_needing_worker("personal"), ["7"])
        pending = feedback_store.pending_worker_comments("personal", "7")
        self.assertEqual([row["comment_id"] for row in pending], ["102"])

    def test_github_configured_allowlist_and_enrollment_round_trip(self):
        checkout = self.root / "app"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True, timeout=CHILD_HANG_TIMEOUT)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "git@github.com:justinpbarnett/app.git"],
                       check=True, capture_output=True, timeout=CHILD_HANG_TIMEOUT)
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        paths.CONFIG_FILE.write_text("")
        enrolled = enrollment.enroll("app", checkout, target_branch="staging", pipeline=True,
                                     commenters=["justinpbarnett", "trusted-qa"])
        self.assertEqual(enrolled.commenters, ["justinpbarnett", "trusted-qa"])
        cfg = config.load_required()
        shown = enrollment.show(cfg, cfg.repo("app"))
        self.assertEqual(shown["commenters"], ["justinpbarnett", "trusted-qa"])

        tk = tracker.GitHubTracker(enrolled)
        self.assertTrue(tk.by_commenter({"author": {"login": "trusted-qa"}}))
        self.assertTrue(tk.by_commenter({"user": {"login": "JUSTINPBARNETT"}}))
        self.assertFalse(tk.by_commenter({"author": {"login": "outsider"}}))

        with self.assertRaises(ValueError):
            config.repo_table({"name": "bad", "path": str(checkout), "github": "a/b", "commenters": ["bad user"]})

    def test_jira_allows_team_and_relayed_comments_while_ignoring_arete_bot(self):
        repo = config.Repo("teamapp", self.root, tracker="jira", jira_project="TEAM", jira_label="repo-teamapp",
                           pipeline=True)
        cfg = config.Config(repos=[repo])
        client = jira.Client("https://jira.example.test", "u", "p")
        tk = tracker.JiraTracker(repo, cfg.jira)
        raw_comments = [
            {"id": "201", "author": {"accountId": "owner-me", "displayName": "Owner"},
             "body": f"{tracker.ARETE_TAG} Staging verified.\n\n{gh.ARETE_MARK}",
             "created": "2026-11-27T10:00:00.000+0000"},
            {"id": "202", "author": {"accountId": "qa-maya", "accountType": "atlassian", "displayName": "Maya QA"},
             "body": "Please preserve the dropdown value on reload.",
             "created": "2026-11-27T10:05:00.000+0000"},
            {"id": "203", "author": {"accountId": "slack-relay-app", "accountType": "app", "displayName": "Slack Relay"},
             "body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
                 {"type": "text", "text": "From @alex in #qa: Save button should stay disabled while submitting."}
             ]}]},
             "created": "2026-11-27T10:06:00.000+0000"},
        ]
        with mock.patch.object(tracker.JiraTracker, "client", new_callable=mock.PropertyMock, return_value=client), \
             mock.patch.object(client, "comments", return_value=raw_comments):
            parsed = tf.fetch_thread_comments(repo, tk, "TEAM-42")
            ticket = tracker.Ticket("TEAM-42", "TEAM-42", "Dropdown selection", body="Keep selection", open=True)
            outcomes = tf.consume_ticket(cfg, repo, "TEAM-42", tk=tk, ticket=ticket, comments=parsed, since="")

        self.assertEqual([(o.comment_id, o.authorized, o.route, o.status) for o in outcomes], [
            ("201", False, "none", "ignored"),
            ("202", True, "worker", "queued"),
            ("203", True, "worker", "queued"),
        ])
        stored = feedback_store.comments_for_ticket("teamapp", "TEAM-42", authorized=True)
        self.assertEqual(stored[1]["relayed_by"], "Slack Relay")
        self.assertIn("stay disabled while submitting", stored[1]["body"])


class IntentRoutingTests(_FeedbackDbCase):
    def setUp(self):
        super().setUp()
        self.repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github")
        self.ctx = tf.ThreadContext(repo="product", ticket="12", tracker="github", title="Keep selection",
                                    staged=True, staged_revision="a" * 40)

    def _comment(self, body: str, cid: str = "1") -> tf.ThreadComment:
        return tf.ThreadComment(cid, "owner", "owner", body,
                                f"https://github.com/owner/product/issues/12#issuecomment-{cid}",
                                "2026-11-27T11:00:00Z")

    def test_neutral_acknowledgments_do_not_trigger_work(self):
        for body in ("Thanks!", "Thank you", "Looks good", "LGTM", "Thanks, looks good on staging!", "Verified, thanks!"):
            result = tf.classify_intent(self._comment(body), self.ctx)
            self.assertEqual(result.intent, "neutral", body)

    def test_explicit_production_instructions_recognized_and_negatives_rejected(self):
        sha = "b" * 40
        for body, expected_rev in (
            ("Moving this to push to production on the sheet.", "a" * 40),
            (f"Promote {sha} to production", sha),
            ("Push this to production", "a" * 40),
            ("Approved for production", "a" * 40),
        ):
            res = tf.classify_intent(self._comment(body), self.ctx)
            self.assertEqual(res.intent, "production_instruction", body)
            self.assertEqual(res.production_revision, expected_rev, body)

        neg = tf.classify_intent(self._comment("Don't push to production yet, the filter still resets."), self.ctx)
        self.assertEqual(neg.intent, "correction")
        q = tf.classify_intent(self._comment("Should we push this to production now?"), self.ctx)
        self.assertEqual(q.intent, "product_question")

    def test_corrections_vs_product_questions_and_custom_classifier_hook(self):
        corr1 = tf.classify_intent(self._comment("Can you please keep the selected filter after reload?"), self.ctx)
        self.assertEqual(corr1.intent, "correction")
        corr2 = tf.classify_intent(self._comment("Thanks! One small bug: totals are missing tax."), self.ctx)
        self.assertEqual(corr2.intent, "correction")

        pq1 = tf.classify_intent(self._comment("Should archived items appear in the CSV export?"), self.ctx)
        self.assertEqual(pq1.intent, "product_question")
        pq2 = tf.classify_intent(
            self._comment("Not sure whether empty search should show all items or an empty state — whichever."),
            self.ctx,
        )
        self.assertEqual(pq2.intent, "product_question")
        self.assertTrue(pq2.ambiguous)

        override = tf.classify_intent(
            self._comment("Looks fine"),
            self.ctx,
            classifier=lambda c, ctx: tf.IntentResult("correction", "custom override"),
        )
        self.assertEqual(override.intent, "correction")


class ClarificationAndResumptionTests(_FeedbackDbCase):
    def test_ambiguous_feedback_asks_focused_question_reconciles_retry_and_resumes_only_on_authorized_answer(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github",
                           parts=[config.Part("", "owner/product")], commenters=["owner", "qa-lead"])
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        ticket = tracker.Ticket("15", "#15", "Empty search behavior", body="Add search", open=True, labels=["agent"])
        posted = []

        def fake_comment(slug, num, body, **kwargs):
            posted.append(body)
            if len(posted) == 1:
                # Simulate tracker accepting the post but response lost on first try.
                raise gh.GhUnsure("lost confirmation")
            return f"https://github.com/{slug}/issues/{num}#issuecomment-500"

        ambiguous = tf.ThreadComment(
            "301", "qa-lead", "qa-lead",
            "Not sure whether an empty search should show all rows or an empty state — either one, let me know.",
            "https://github.com/owner/product/issues/15#issuecomment-301",
            "2026-11-27T12:00:00Z",
        )

        with mock.patch.object(gh, "issue_comment", side_effect=fake_comment):
            with self.assertRaises(gh.GhUnsure):
                tf.consume_ticket(cfg, repo, "15", tk=tk, ticket=ticket, comments=[ambiguous], since="")
            self.assertEqual(len(posted), 1)
            self.assertTrue(feedback_store.has_open_clarification("product", "15"))

            # On retry, the marker is already visible in the thread -> reconciles without double posting.
            bot_comment = tf.ThreadComment("500", "owner", "owner", posted[0],
                                           "https://github.com/owner/product/issues/15#issuecomment-500",
                                           "2026-11-27T12:00:05Z", is_bot=True)
            reconciled = tf.consume_ticket(cfg, repo, "15", tk=tk, ticket=ticket,
                                           comments=[ambiguous, bot_comment], since="")
            self.assertEqual(len(posted), 1)
            self.assertEqual(reconciled[0].status, "waiting_answer")
            self.assertEqual(reconciled[0].clarification_url, "https://github.com/owner/product/issues/15#issuecomment-500")

        # While #15 waits for clarification, unrelated #16 can still be picked by poll.fresh_candidates.
        t15 = tracker.Ticket("15", "#15", "Empty search", labels=["agent"], created="2026-11-27T09:00:00Z")
        t16 = tracker.Ticket("16", "#16", "Unrelated work", labels=["agent"], created="2026-11-27T09:01:00Z")
        skips = pipeline.SkipLog({})
        with mock.patch.object(tk, "todo", return_value=[t15, t16]), \
             mock.patch.object(tk, "blocker", return_value=""), \
             mock.patch.object(tracker.LocalTracker, "todo", return_value=[]):
            picked = list(poll.fresh_candidates(repo, tk, skips, set(), set(), (), None))
        self.assertEqual([t.id for t in picked], ["16"])
        self.assertIn("product#15", skips.current)

        # Unauthorized reply does NOT resolve the clarification.
        outsider_reply = tf.ThreadComment("501", "stranger", "stranger", "Show all rows.",
                                          "https://github.com/owner/product/issues/15#issuecomment-501",
                                          "2026-11-27T12:02:00Z")
        res = tf.consume_ticket(cfg, repo, "15", tk=tk, ticket=ticket,
                                comments=[ambiguous, bot_comment, outsider_reply], since="")
        self.assertFalse(res[-1].authorized)
        self.assertTrue(feedback_store.has_open_clarification("product", "15"))
        self.assertEqual(feedback_store.tickets_needing_worker("product"), [])

        # Authorized answer resolves the clarification and queues the combined correction for the worker.
        auth_reply = tf.ThreadComment("502", "owner", "owner", "Show all rows when search is empty.",
                                      "https://github.com/owner/product/issues/15#issuecomment-502",
                                      "2026-11-27T12:03:00Z")
        res = tf.consume_ticket(cfg, repo, "15", tk=tk, ticket=ticket,
                                comments=[ambiguous, bot_comment, outsider_reply, auth_reply], since="")
        self.assertTrue(res[-1].acted)
        self.assertEqual((res[-1].intent, res[-1].route, res[-1].status), ("correction", "worker", "queued"))
        self.assertFalse(feedback_store.has_open_clarification("product", "15"))
        self.assertEqual(feedback_store.tickets_needing_worker("product"), ["15"])
        queued = feedback_store.pending_worker_comments("product", "15")
        md = tf.worker_instructions_markdown(queued)
        self.assertIn("Show all rows when search is empty.", md)
        self.assertIn("Original feedback:", md)

    def test_authorized_thread_answer_resumes_non_secret_worker_ask_but_never_secret_ask(self):
        repo = config.Repo("team", self.root, tracker="jira", jira_project="TEAM", pipeline=True)
        cfg = config.Config(repos=[repo])
        client = jira.Client("https://jira.example.test", "u", "p")
        tk = tracker.JiraTracker(repo, cfg.jira)

        # Non-secret needs-info ask -> resolved by authorized Jira team comment.
        rid1 = record.start("pipeline", repo="team", issue="TEAM-10", status="needs-info")
        ask_store.ask_add("ask-1", rid1, "team", "TEAM-10", "build", "build", "need choice", "",
                          [{"header": "Format", "kind": "text", "question": "Which date format?"}], {})
        c1 = tf.ThreadComment("901", "qa-1", "QA", "Use YYYY-MM-DD.",
                              "https://jira.example.test/browse/TEAM-10?focusedCommentId=901", "2026-11-27T13:00:00Z")
        with mock.patch.object(tracker.JiraTracker, "client", new_callable=mock.PropertyMock, return_value=client):
            tf.consume_ticket(cfg, repo, "TEAM-10", tk=tk, ticket=tracker.Ticket("TEAM-10", "TEAM-10", "Dates", open=True),
                              comments=[c1], since="")
        self.assertEqual(ask_store.ask_get("ask-1")["status"], "answered")

        # Secret ask -> NEVER answered by tracker comment.
        rid2 = record.start("pipeline", repo="team", issue="TEAM-11", status="needs-info")
        ask_store.ask_add("ask-2", rid2, "team", "TEAM-11", "build", "build", "need token", "",
                          [{"header": "Token", "kind": "secret", "name": "API_TOKEN", "question": "Supply token"}], {})
        c2 = tf.ThreadComment("902", "qa-1", "QA", "Here is the token",
                              "https://jira.example.test/browse/TEAM-11?focusedCommentId=902", "2026-11-27T13:05:00Z")
        with mock.patch.object(tracker.JiraTracker, "client", new_callable=mock.PropertyMock, return_value=client):
            tf.consume_ticket(cfg, repo, "TEAM-11", tk=tk, ticket=tracker.Ticket("TEAM-11", "TEAM-11", "Secret", open=True),
                              comments=[c2], since="")
        self.assertEqual(ask_store.ask_get("ask-2")["status"], "open")


class StagedChildAndWorkerSteeringTests(_FeedbackDbCase):
    def test_staged_child_correction_reaches_worker_holds_old_qa_and_thank_you_is_noop(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github",
                           target_branch="staging", landing="pr", unattended_environment="staging")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        rev = "a" * 40
        promo_file = release_proof.state_dir("product", "21") / "promotion.json"
        release_promotion.serve_guard.write(promo_file, {
            "phase": "awaiting_qa", "revision": rev, "graph_parent": "10",
            "handoff": "https://github.com/owner/product/issues/21#issuecomment-50",
            "qa_since": "2026-11-27T09:00:00Z", "scope": "scope-1", "policy": "policy-1",
        })
        ticket = tracker.Ticket("21", "#21", "Child feature", body="Save selection", open=True, labels=["agent"])

        # 1. Thank-you alone starts no work and leaves staging undisturbed.
        thanks = tf.ThreadComment("51", "owner", "owner", "Thanks!",
                                  "https://github.com/owner/product/issues/21#issuecomment-51", "2026-11-27T09:05:00Z")
        out_thanks = tf.consume_ticket(cfg, repo, "21", tk=tk, ticket=ticket, comments=[thanks], since="")
        self.assertEqual((out_thanks[0].intent, out_thanks[0].acted), ("neutral", False))
        self.assertEqual(feedback_store.tickets_needing_worker("product"), [])
        self.assertEqual(tf.pending_corrections("product", "21"), [])

        # 2. Actionable correction queues worker under same open spec and holds staging promotion.
        fix = tf.ThreadComment("52", "owner", "owner", "Save still loses the dropdown selection on reload. Please keep it.",
                               "https://github.com/owner/product/issues/21#issuecomment-52", "2026-11-27T09:10:00Z")
        out_fix = tf.consume_ticket(cfg, repo, "21", tk=tk, ticket=ticket, comments=[thanks, fix], since="")
        self.assertEqual((out_fix[-1].intent, out_fix[-1].route, out_fix[-1].status), ("correction", "worker", "queued"))
        self.assertEqual(len(tf.pending_corrections("product", "21")), 1)
        stored = feedback_store.comments_for_ticket("product", "21", intent="correction")[0]
        self.assertEqual(stored["parent_ticket"], "10")

        # 3. Poll priority_pick selects the child ticket for the single-ticket worker.
        with mock.patch.object(tk, "in_zte_queue", return_value=False), \
             mock.patch.object(tk, "replied", return_value=[]), \
             mock.patch.object(tracker.LocalTracker, "replied", return_value=[]):
            pick = poll.priority_pick(repo, tk, pipeline.SkipLog({}), set(), set())
        self.assertIsNotNone(pick)
        self.assertEqual((pick.tid, pick.from_ticket_feedback), ("21", True))

        # 4. Single-ticket worker claims feedback, injects into brief, and settles it applied on completion.
        rid = record.start("pipeline", repo="product", issue="21", status="running")
        run = pipeline.Run(rid, repo, self.root / "runs" / rid, tracker=tk, tid="21", writes_lines=False)
        run.claim_feedback()
        self.assertEqual(len(run.ticket_feedback), 1)
        work = pipeline.TicketWork(cfg=cfg, run=run, ticket=ticket, ws=self.root, checkouts=[],
                                   layer=config.RepoLayer(dir=self.root), timeout=10, workspace="product")
        self.assertIn("## Authorized Ticket Feedback", work.brief.comments)
        self.assertIn("Save still loses the dropdown selection on reload", work.brief.comments)

        # After worker finishes and returns PR/revision for QA, pending_corrections clears!
        feedback_store.settle_worker_comments(rid, "applied")
        self.assertEqual(tf.pending_corrections("product", "21"), [])
        self.assertEqual(feedback_store.tickets_needing_worker("product"), [])

        # 5. Explicit production direction after fresh QA is recorded and exposed to release workflow.
        prod = tf.ThreadComment("60", "owner", "owner", "Moving this to push to production on the sheet.",
                                "https://github.com/owner/product/issues/21#issuecomment-60", "2026-11-27T10:00:00Z")
        out_prod = tf.consume_ticket(cfg, repo, "21", tk=tk, ticket=ticket, comments=[thanks, fix, prod], since="")
        self.assertEqual((out_prod[-1].intent, out_prod[-1].route), ("production_instruction", "release"))
        dirs = tf.production_instructions("product", "21")
        self.assertEqual(len(dirs), 1)
        self.assertEqual(dirs[0]["data"]["revision"], rev)

    def test_active_running_worker_receives_steering_once_without_replay_duplication(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        rid = record.start("pipeline", repo="product", issue="30", status="running")
        ticket = tracker.Ticket("30", "#30", "Active feature", body="Build it", open=True, labels=["agent"])
        steer = tf.ThreadComment("701", "owner", "owner", "Please also validate empty email addresses.",
                                 "https://github.com/owner/product/issues/30#issuecomment-701", "2026-11-27T14:00:00Z")

        first = tf.consume_ticket(cfg, repo, "30", tk=tk, ticket=ticket, comments=[steer], since="")
        self.assertEqual((first[0].status, first[0].steered_run_id, first[0].acted), ("steered", rid, True))
        second = tf.consume_ticket(cfg, repo, "30", tk=tk, ticket=ticket, comments=[steer], since="")
        self.assertTrue(second[0].replayed)
        self.assertFalse(second[0].acted)

        messages = ask_store.take_messages(rid)
        self.assertEqual(len(messages), 1)
        self.assertIn("validate empty email addresses", messages[0])


class ClosedTicketFollowUpAndReplayTests(_FeedbackDbCase):
    def test_post_pickup_and_closed_ticket_follow_up_and_replay_idempotency(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)

        # Simulate ticket picked up at 10:00 and completed/closed.
        rid = record.start("pipeline", repo="product", issue="40", status="merged")
        with foundation.tx() as c:
            c.execute("UPDATE runs SET started = ? WHERE id = ?", ("2026-11-27T10:00:00Z", rid))
        spec_store.bind("github:owner/product#40", "product", "40", "", lambda: rid)
        spec_store.advance("github:owner/product#40", rid, "building", "complete", {"done": True})

        closed_ticket = tracker.Ticket("40", "#40", "Completed feature", body="Delivered", open=False, labels=["agent"])
        pre_pickup = tf.ThreadComment("800", "owner", "owner", "Change this before pickup.",
                                      "https://github.com/owner/product/issues/40#issuecomment-800", "2026-11-27T09:50:00Z")
        post_thanks = tf.ThreadComment("801", "owner", "owner", "Thanks for shipping!",
                                       "https://github.com/owner/product/issues/40#issuecomment-801", "2026-11-27T11:00:00Z")
        post_bug = tf.ThreadComment("802", "owner", "owner",
                                    "Save loses the previously selected timezone, and please also add CSV export.",
                                    "https://github.com/owner/product/issues/40#issuecomment-802", "2026-11-27T11:05:00Z")

        first = tf.consume_ticket(cfg, repo, "40", tk=tk, ticket=closed_ticket,
                                  comments=[pre_pickup, post_thanks, post_bug])
        # Pre-pickup comment (09:50 <= 10:00) is skipped; post-pickup comments are processed.
        self.assertEqual([(o.comment_id, o.intent, o.route, o.status, o.acted) for o in first], [
            ("801", "neutral", "none", "noop", False),
            ("802", "correction", "follow_up", "follow_up_pending", True),
        ])
        # Closed ticket does NOT queue a worker on the original ticket; it stages a follow-up instead.
        self.assertEqual(feedback_store.tickets_needing_worker("product"), [])
        follow_ups = tf.pending_follow_ups("product")
        self.assertEqual([row["comment_id"] for row in follow_ups], ["802"])
        self.assertEqual(follow_ups[0]["ticket_state"], "closed")
        self.assertEqual(spec_store.find("product", "40")["state"], "complete")

        # Replay exact same comments -> zero second actions!
        replay = tf.consume_ticket(cfg, repo, "40", tk=tk, ticket=closed_ticket,
                                   comments=[pre_pickup, post_thanks, post_bug])
        self.assertTrue(all(o.replayed and not o.acted for o in replay))
        self.assertEqual(len(tf.pending_follow_ups("product")), 1)
