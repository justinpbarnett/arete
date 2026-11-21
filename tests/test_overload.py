import json
import subprocess
import time
import unittest
from datetime import timedelta
from unittest import mock

from arete import accounts, agent, claude, codex, config, pi, routing, runner, util
from arete.store import devices as devices_store
from tests import support

support.isolate_live_state()

CAPACITY = "Selected model is at capacity. Please try a different model."
CODEX_LIMIT = "You've hit your usage limit. Try again in 2 hours."


def minutes_ahead(limit: agent.UsageLimit) -> float:
    return (util.parse_iso(limit.until) - util.now()) / timedelta(minutes=1)


def claude_failure(text: str, status=None) -> subprocess.CompletedProcess:
    event = {"type": "result", "subtype": "success", "is_error": True, "result": text,
             "api_error_status": status, "session_id": "s1"}
    return subprocess.CompletedProcess([], 1, json.dumps(event), "")


class OverloadReaderTests(unittest.TestCase):
    def assert_overload(self, limit, model):
        self.assertIsNotNone(limit)
        self.assertEqual((limit.kind, limit.scope), ("overloaded", agent.model_scope(model)))
        self.assertAlmostEqual(minutes_ahead(limit), 10, delta=0.5)

    def test_codex_capacity_message_holds_only_that_model_for_ten_minutes(self):
        self.assert_overload(codex.usage_limit(CAPACITY, "gpt-6.2-sol"), "gpt-6.2-sol")

    def test_codex_usage_limit_wins_over_capacity_text(self):
        limit = codex.usage_limit(f"{CAPACITY}\n{CODEX_LIMIT}", "gpt-6.2-sol")
        self.assertEqual((limit.scope, limit.kind), (codex.LIMIT_SCOPE, ""))

    def test_codex_ordinary_failure_is_not_a_limit(self):
        self.assertIsNone(codex.usage_limit("the sandbox refused the command", "gpt-6.2-sol"))
    def test_claude_overloaded_529_holds_only_that_model(self):
        result = claude.parse(claude_failure("API Error: 529 overloaded_error", 529), agent.NO_SESSION,
                              "claude-opus-4-7", None)
        self.assertFalse(result.ok)
        self.assert_overload(result.usage_limit, "claude-opus-4-7")

    def test_claude_usage_limit_wins_over_overload_text(self):
        result = claude.parse(claude_failure("Overloaded. You've hit your limit"), agent.NO_SESSION,
                              "claude-opus-4-7", None)
        self.assertEqual(result.usage_limit.scope, claude.EVERY_MODEL)
        self.assertNotEqual(result.usage_limit.kind, "overloaded")

    def test_claude_ordinary_failure_is_not_a_limit(self):
        result = claude.parse(claude_failure("tests failed", 500), agent.NO_SESSION, "claude-opus-4-7", None)
        self.assertFalse(result.ok)
        self.assertIsNone(result.usage_limit)

    def test_pi_detects_overload_for_subscription_routes(self):
        for family in ("claude", "codex", "other"):
            with self.subTest(family):
                auth = accounts.Auth(mode="subscription", family=family, identity="acct", provider="prov")
                self.assert_overload(pi.usage_limit(auth, "prov", "529 Overloaded", "prov/model-a"),
                                     "prov/model-a")

    def test_pi_usage_limit_wins_over_overload_text(self):
        auth = accounts.Auth(mode="subscription", family="codex", identity="acct")
        limit = pi.usage_limit(auth, "openai", f"{CAPACITY} usage limit reached", "gpt-6.2-sol")
        self.assertNotEqual(limit.kind, "overloaded")

    def test_pi_ordinary_failure_is_not_a_limit(self):
        auth = accounts.Auth(mode="subscription", family="codex", identity="acct")
        self.assertIsNone(pi.usage_limit(auth, "openai", "bad request", "gpt-6.2-sol"))


class OverloadHoldTests(unittest.TestCase):
    def test_hold_covers_only_the_overloaded_model_on_a_login(self):
        auth = accounts.Auth(mode="subscription", family="codex", identity="overload-login")
        limit = codex.usage_limit(CAPACITY, "gpt-6.2-sol")
        self.assertIn(limit.scope, routing.limit_scopes(auth, "gpt-6.2-sol"))
        devices_store.hold(accounts.scope(auth, limit.scope), limit.until, "codex model overloaded")
        self.assertIsNotNone(routing.auth_hold(auth, "gpt-6.2-sol"))
        self.assertIsNone(routing.auth_hold(auth, "gpt-6.1-mini"))


class OverloadRunTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config()
        self.calls = []

    def candidate(self, which, model):
        return routing.Candidate(order=0, model=model, runner=which, provider="", credential="login",
                                 billing="subscription", tier="", position=0, requested=True, effort="",
                                 instructions=(), price=None, state="ready", detail="")

    def route_chain(self, auth, which, models):
        def pick(trip):
            for model in models:
                if not routing.auth_hold(auth, model):
                    return runner.Picked(self.candidate(which, model), auth, None, model, "", {auth}, None), []
            return None, [{"runner": which, "model": models[-1], "credential": "login", "state": "held",
                           "detail": "", "until": ""}]
        return pick

    def run_chain(self, which, models, family, results, identity):
        auth = accounts.Auth(mode="subscription", family=family, identity=identity)

        def launch(prompt, cwd, **kw):
            self.calls.append(kw["model"])
            return results[len(self.calls) - 1]
        with (mock.patch.object(runner, "pick", self.route_chain(auth, which, models)),
              mock.patch.object(getattr(runner, which), "run", launch),
              mock.patch.object(runner, "time", mock.Mock(wraps=runner.time)),
              mock.patch.object(runner.time, "sleep", side_effect=AssertionError("slept"))):
            res = runner.run("review", ".", model=models[0], runner=which, cfg=self.cfg,
                             wait_out_limits=False, say=lambda text: None, trace=("overload-run", "review"))
        return auth, res

    def test_run_chain_only_blocks_sleep_inside_runner(self):
        def check_sleep(*args, **kwargs):
            time.sleep(0)
            with self.assertRaisesRegex(AssertionError, "^slept$"):
                runner.wait_until(None, util.now() + timedelta(minutes=1))

        with (mock.patch.object(runner, "run", side_effect=check_sleep),
              mock.patch.object(runner, "HOLD_CHECK_SECONDS", 1)):
            self.run_chain("codex", ["gpt-6.2-sol"], "codex", [], "sleep-check")

    def test_codex_capacity_runs_the_next_route_and_holds_only_that_model(self):
        failed = agent.Result(ok=False, error=CAPACITY, usage_limit=codex.usage_limit(CAPACITY, "gpt-6.2-sol"))
        auth, res = self.run_chain("codex", ["gpt-6.2-sol", "gpt-6.1-mini"], "codex",
                                   [failed, agent.Result(ok=True, text="done")], "codex-capacity-login")
        self.assertEqual(self.calls, ["gpt-6.2-sol", "gpt-6.1-mini"])
        self.assertTrue(res.ok)
        hold = routing.auth_hold(auth, "gpt-6.2-sol")
        self.assertAlmostEqual((util.parse_iso(hold["until"]) - util.now()) / timedelta(minutes=1), 10, delta=0.5)
        self.assertIsNone(routing.auth_hold(auth, "gpt-6.1-mini"))

    def test_claude_529_runs_the_next_route_and_holds_only_that_model(self):
        failed = claude.parse(claude_failure("API Error: 529 overloaded_error", 529), agent.NO_SESSION,
                              "claude-opus-4-7", None)
        auth, res = self.run_chain("claude", ["claude-opus-4-7", "claude-sonnet-4-6"], "claude",
                                   [failed, agent.Result(ok=True, text="done")], "claude-529-login")
        self.assertEqual(self.calls, ["claude-opus-4-7", "claude-sonnet-4-6"])
        self.assertTrue(res.ok)
        self.assertIsNotNone(routing.auth_hold(auth, "claude-opus-4-7"))
        self.assertIsNone(routing.auth_hold(auth, "claude-sonnet-4-6"))

    def test_ordinary_failure_ends_the_call_without_trying_the_next_route(self):
        failed = agent.Result(ok=False, error="the sandbox refused the command")
        auth, res = self.run_chain("codex", ["gpt-6.2-sol", "gpt-6.1-mini"], "codex",
                                   [failed, agent.Result(ok=True, text="done")], "codex-ordinary-login")
        self.assertEqual(self.calls, ["gpt-6.2-sol"])
        self.assertFalse(res.ok)
        self.assertIsNone(routing.auth_hold(auth, "gpt-6.2-sol"))


if __name__ == "__main__":
    unittest.main()
