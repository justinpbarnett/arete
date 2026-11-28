from unittest import mock

from arete import orchestration, owner_asks, pipeline, poll, record, tasks
from tests import support
from arete.store import asks, tasks as task_store
from tests.test_process_limits import Isolated


class SharedCheckRunnerTests(Isolated):
    blocked = {"runner_blocked": True, "passed": False,
               "failed_checks": [{"name": "tests", "capability": "macos-sandbox-profile"}]}

    def hold(self, ticket, *, call=owner_asks.CHECK_RUNNER_CALL, repo=None):
        request = owner_asks.check_runner_request(self.blocked)
        repo = repo or self.repo.name
        rid = support.start_pipeline(repo=repo, issue=ticket)
        record.move(rid, "needs-info", ("running",))
        first = asks.ask_add(rid, rid, repo, ticket, "checks", call, request["why"],
                             request["tried"], request["questions"], {})
        return rid, first

    def test_one_owner_confirmation_requeues_every_current_affected_ticket(self):
        leader, first = self.hold(self.ticket)
        second_ticket = task_store.task_add(self.repo.name, "Other ticket", "Issue", "feat")
        follower, again = self.hold(second_ticket)
        unrelated, _ = self.hold("12", repo="another", call="orchestrate")
        obsolete, _ = self.hold("99")
        support.start_pipeline(repo=self.repo.name, issue="99")
        self.assertTrue(first)
        self.assertFalse(again)
        rows = {row["id"]: row for row in tasks.asks_view()["asks"]}
        self.assertEqual(set(rows), {leader, unrelated})
        self.assertIn(f"{self.repo.name}#{second_ticket}", rows[leader]["why"])
        answers = owner_asks.answers_for(asks.ask_get(leader), {"answers": [{"done": True}]})
        self.assertTrue(asks.ask_close(leader, "answered", answers))
        self.assertEqual(asks.ask_get(follower)["answers"], answers)
        for rid in (unrelated, obsolete):
            self.assertEqual(asks.ask_get(rid)["status"], "open")
        self.assertEqual(set(asks.ready_runner_tickets(self.repo.name)), {self.ticket, second_ticket})
        pick = poll.next_pick(self.repo, self.local, pipeline.SkipLog({}), set(), excluded={self.ticket})
        self.assertEqual(pick.tid, second_ticket)
        asks.asks_taken([follower], "next")
        self.assertEqual(asks.ready_runner_tickets(self.repo.name), [self.ticket])

    def test_owner_text_and_mixed_check_failures_stay_ticket_specific(self):
        leader, _ = self.hold(self.ticket)
        follower, _ = self.hold("other")
        asks.ask_close(leader, "answered", owner_asks.text_answers(asks.ask_get(leader), "Still investigating"))
        self.assertEqual(asks.ask_get(follower)["status"], "open")
        self.assertEqual(asks.ready_runner_tickets(self.repo.name), [])
        self.assertIsNone(owner_asks.check_runner_request({**self.blocked,
                          "failed_checks": [*self.blocked["failed_checks"], {"name": "lint"}]}))
        self.assertIsNone(owner_asks.check_runner_request({"passed": False}))

    def test_only_first_held_ticket_sends_a_runner_notification(self):
        request = owner_asks.check_runner_request(self.blocked)

        def hold_and_notify(tid):
            run = mock.Mock(id=support.start_pipeline(repo=self.repo.name, issue=tid), tid=tid, repo=self.repo,
                            dry_run=False, alone=False, config=self.cfg)
            ask = {**request, "ask_id": run.id, "step": "checks", "call": owner_asks.CHECK_RUNNER_CALL}
            self.assertEqual(pipeline._ask(run, self.root, [], "Runner needed", "Blocked", [ask], {})[0], "needs-info")
            run.enter.assert_called_once_with("needs-info")
            return run.say.call_count

        from arete import webpush
        from arete.store import push as push_store

        push_store.push_add("https://127.0.0.1:9443/sub", "A" * 80, "B" * 20, "test", "")
        with mock.patch.object(pipeline.run_lines, "write"), mock.patch.object(pipeline.devices, "release"), \
                mock.patch.object(pipeline.alerts, "ask_notice") as alert, \
                mock.patch.object(webpush, "post", return_value=201) as push:
            self.assertEqual(hold_and_notify(self.ticket), 1)
            self.assertEqual(hold_and_notify("other"), 0)
            self.assertEqual(alert.call_count, 1)
            self.assertEqual(push.call_count, 1)

    def test_parked_check_retains_failed_tests_in_its_terminal_receipt(self):
        controller = mock.Mock()
        controller.run.dry_run = controller.run.alone = controller.run.action_only = False
        controller.run.owner_questions_blocked = controller.exchanged = False
        controller.data = {"acceptance": "ticket"}
        action = {"id": "checks", "op": "checks.run", "state": "accepted"}
        controller.perform.return_value = self.blocked
        request = owner_asks.check_runner_request(self.blocked)
        controller.atomic.side_effect = owner_asks.Asked([request], ["ask-id"], "checks",
                                                         owner_asks.CHECK_RUNNER_CALL, {})
        with mock.patch.object(orchestration.ledger, "transition") as transition, \
                mock.patch.object(orchestration.run_lines, "write"), self.assertRaises(owner_asks.Asked):
            orchestration.Controller.dispatch(controller, action)
        controller.atomic.assert_called_once_with("orchestrate_ask", request=request, call=owner_asks.CHECK_RUNNER_CALL)
        self.assertEqual(transition.call_args.args[3], {**self.blocked, "ask_ids": ["ask-id"], "awaiting_owner": True})
