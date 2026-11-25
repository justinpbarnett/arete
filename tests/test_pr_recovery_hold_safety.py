from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import DEFAULT, patch

from arete import gh, pr_recovery, unblock, unblock_watch, workspace

PR = "https://github.com/example/first/pull/961"
SIBLING = "https://github.com/example/second/pull/962"
HELD_AT = "2026-11-24T12:00:00Z"


class RetainedPathSafetyTests(TestCase):
    def test_retained_private_paths_cannot_be_exposed_or_force_staged(self):
        c = workspace.Checkout("", "example/first", Path("src"), Path("wt"), "main")
        private = "private notes.txt"
        saved = pr_recovery.Retained(set(), {private})
        cases = [(set(), [private], saved), ({private}, (), saved),
                 (set(), (), pr_recovery.Retained({private}, set()))]
        for indexed, forced, found in cases:
            with (self.subTest(indexed=indexed, forced=forced, found=found),
                  patch.object(pr_recovery, "read_retained", return_value=saved),
                  patch.object(pr_recovery, "indexed_paths", return_value=indexed),
                  patch.object(pr_recovery, "existing_paths", side_effect=lambda _, names, __: names),
                  self.assertRaises(workspace.RunFailed) as refused):
                pr_recovery.check_retained(c, found=found, forced=forced)
            self.assertIn(f"expose retained ignored files: {private!r}", refused.exception.why)

    def test_retained_visible_work_cannot_be_silently_omitted(self):
        c = workspace.Checkout("", "example/first", Path("src"), Path("wt"), "main")
        name = "retained notes.txt"
        with (patch.object(pr_recovery, "read_retained", return_value=pr_recovery.Retained({name}, set())),
              patch.object(pr_recovery, "indexed_paths", return_value=set()),
              self.assertRaises(workspace.RunFailed) as refused):
            pr_recovery.check_retained(c, found=pr_recovery.Retained(set(), {name}))
        self.assertIn(f"hide retained untracked files: {name!r}", refused.exception.why)


class HeldRecoveryTests(TestCase):
    def setUp(self):
        accepted, local, head, base = (digit * 40 for digit in "abcd")
        refs = pr_recovery.Refs(PR, "OPEN", "topic", head, "main", base, False, head, base)
        self.held = {"pr": PR, "branch": "topic", "accepted": accepted, "local": local,
                     "refs": asdict(refs), "decision": "v2:held", "held_at": HELD_AT}
        self.observed = {"refs": self.held["refs"], "local": local, "decision": self.held["decision"],
                         "target": True, "sibling_replies": ""}

    def test_identical_hold_gets_the_same_notice_key(self):
        new_head = {**self.held["refs"], "head": "e" * 40, "origin": "e" * 40}
        with patch.object(pr_recovery.unblock, "notice") as notice:
            for held in (self.held, {**self.held, "held_at": "2026-11-24T12:01:00Z", "seen_replies": []},
                         {**self.held, "refs": new_head}, {**self.held, "decision": "v2:new"}):
                pr_recovery.notice("fixture", "961", held)
        first, repeated, moved, answered = (c.args[0] for c in notice.call_args_list)
        self.assertEqual(first, repeated)
        self.assertEqual(len({first, moved, answered}), 3)

    def test_unchanged_poll_neither_starts_a_recovery_attempt_nor_launches(self):
        repo = SimpleNamespace(name="fixture")
        ticket = SimpleNamespace(id="961", blocker="")
        row = {"id": "held-run", "summary": "Both versions need reconciliation"}
        with (patch.object(unblock_watch.orchestration, "publication_pending", return_value=False),
              patch.object(unblock, "pr_recovery_observation", return_value=self.observed),
              patch.multiple(unblock_watch.ledger, observe=lambda *a: {}, mark=DEFAULT, begin=DEFAULT)
              as ledger, patch.object(unblock_watch.tasks, "launch_recovery") as launch,
              patch.object(unblock, "diagnose") as diagnose):
            for _ in range(2):
                unblock_watch.inspect_pr_recovery(None, repo, None, ticket, row, self.held)
        self.assertEqual([c.args[1] for c in ledger["mark"].call_args_list], ["needs_owner"] * 2)
        ledger["begin"].assert_not_called()
        launch.assert_not_called()
        diagnose.assert_not_called()

    def test_new_head_or_owner_guidance_makes_a_hold_eligible_again(self):
        for change in ({"refs": {**self.held["refs"], "head": "e" * 40, "origin": "e" * 40}},
                       {"decision": "v2:new"}, {"sibling_replies": "new authorized reply"}):
            with self.subTest(change=change):
                self.assertTrue(unblock.recovery_changed(self.held, {**self.observed, **change}))


class HeldReplyTests(TestCase):
    def test_same_second_ticket_guidance_requires_a_snapshot_without_the_reply(self):
        for snapshot in ({}, {"seen_ticket_replies": None}, {"seen_ticket_replies": ["1"]}, {"seen_ticket_replies": []}):
            with self.subTest(snapshot=snapshot):
                held = {"held_at": HELD_AT, **snapshot}
                replies = [("1", "2026-11-24T11:59:59Z", "Earlier"), ("1", HELD_AT, "Same second"),
                           ("2", "2026-11-24T12:00:01Z", "Later")]
                self.assertEqual([unblock.ticket_answer_after_hold({}, held, [reply], []) for reply in replies],
                                 [False, snapshot.get("seen_ticket_replies") == [], True])

    def test_sibling_snapshot_distinguishes_review_from_comment_and_ignores_notices(self):
        held = {"held_at": HELD_AT, "pr": PR, "seen_replies_by_pr": {SIBLING: [["comment", 1]]}}
        seen = {"pr_url": SIBLING, "pr_state": "OPEN", "kind": "comment", "gh_id": 1,
                "created": HELD_AT, "body": "Keep both edits"}
        new_review = {**seen, "kind": "review"}
        machine = {**seen, "gh_id": 2, "body": gh.marked("Held for the owner")}
        self.assertEqual(unblock.replies_after_hold({}, held, [seen, new_review, machine], [PR, SIBLING]), [new_review])
        held["seen_replies_by_pr"][SIBLING] = None
        self.assertEqual(unblock.replies_after_hold({}, held, [new_review], [PR, SIBLING]), [])
