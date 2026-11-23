import json
import subprocess
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


class UnreachableReaderTests(unittest.TestCase):
    timeout_error = ("Request timed out.: Connect Timeout Error "
                     "(attempted address: api.openai.com:443, timeout: 10000ms) (UND_ERR_CONNECT_TIMEOUT)")
    model = "openai/gpt-6.2-sol"

    def readers(self):
        yield "codex", lambda text: codex.usage_limit(text, self.model), CODEX_LIMIT
        yield "claude", self.claude_limit, CODEX_LIMIT
        for mode, family in (("subscription", "codex"), ("subscription", "claude"), ("api_key", "codex")):
            auth = accounts.Auth(mode=mode, family=family, provider="openai")
            yield (f"pi {mode} {family}", lambda text, auth=auth: pi.usage_limit(auth, "openai", text, self.model),
                   "Insufficient quota" if mode == "api_key" else CODEX_LIMIT)

    def claude_limit(self, text, status=None):
        proc = claude_failure(text, status)
        return claude.parse(proc, agent.NO_SESSION, self.model, None).usage_limit

    def assert_unreachable(self, limit):
        self.assertIsNotNone(limit)
        self.assertEqual((limit.kind, limit.scope), ("unreachable", agent.model_scope(self.model)))
        self.assertAlmostEqual(minutes_ahead(limit), 10, delta=0.5)

    def test_incident_timeout_holds_the_model_for_ten_minutes(self):
        for name, read, _ in self.readers():
            with self.subTest(reader=name):
                self.assert_unreachable(read(self.timeout_error))

    def test_each_connection_failure_marker_is_recognized(self):
        for text in ("UND_ERR_CONNECT_TIMEOUT", "Connect Timeout Error", "ECONNRESET", "ECONNREFUSED",
                     "ENOTFOUND", "EAI_AGAIN", "fetch failed", "Connection error.", "Request timed out."):
            with self.subTest(text=text):
                self.assert_unreachable(agent.unreachable_limit(text, self.model))

    def test_native_api_key_readers_keep_the_model_limit(self):
        proc = subprocess.CompletedProcess([], 1, "", self.timeout_error)
        self.assert_unreachable(codex.parse(proc, agent.NO_SESSION, self.model, None, codex.KEY_PROVIDER).usage_limit)
        with (mock.patch.object(claude, "command", return_value=[]),
              mock.patch.object(claude, "agent_env", return_value={}),
              mock.patch.object(claude.child, "stream", return_value=claude_failure(self.timeout_error))):
            result = claude.run("review", ".", model=self.model, auth=accounts.Auth(mode="api_key"))
        self.assert_unreachable(result.usage_limit)

    def test_usage_limit_wins_over_connection_failure(self):
        for name, read, usage in self.readers():
            with self.subTest(reader=name):
                expected = read(usage)
                self.assertIsNotNone(expected)
                actual = read(f"{usage}\n{self.timeout_error}")
                self.assertIsNotNone(actual)
                self.assertEqual((actual.kind, actual.scope), (expected.kind, expected.scope))

    def test_overload_wins_over_connection_failure(self):
        for name, read, _ in self.readers():
            with self.subTest(reader=name):
                limit = read(f"{CAPACITY}\n{self.timeout_error}")
                self.assertIsNotNone(limit)
                self.assertEqual((limit.kind, limit.scope), ("overloaded", agent.model_scope(self.model)))

    def test_ordinary_failures_and_http_errors_are_not_limits(self):
        for name, read, _ in self.readers():
            for text in ("tests failed", "the sandbox refused the command", "HTTP 500 Internal Server Error",
                         f"HTTP 400 Bad Request: {self.timeout_error}", f"API Error: 401 {self.timeout_error}",
                         f"unexpected status 403 Forbidden: {self.timeout_error}",
                         f"500 Internal Server Error: {self.timeout_error}"):
                with self.subTest(reader=name, text=text):
                    self.assertIsNone(read(text))

    def test_structured_http_status_excludes_connection_failure(self):
        self.assertIsNone(self.claude_limit(self.timeout_error, status=500))
        event = {"type": "turn.failed", "error": {"message": self.timeout_error, "statusCode": 500}}
        proc = subprocess.CompletedProcess([], 1, json.dumps(event), "")
        self.assertIsNone(codex.parse(proc, agent.NO_SESSION, self.model, None, codex.CHATGPT_PROVIDER).usage_limit)
        auth = accounts.Auth(mode="api_key", provider="openai")
        self.assertIsNone(pi.usage_limit(auth, "openai", event, self.model))

    def test_codex_http_error_in_event_or_stderr_is_not_a_limit(self):
        for provider in (codex.CHATGPT_PROVIDER, codex.KEY_PROVIDER):
            for prefix in ("Request failed\n", "Error: "):
                message = f"{prefix}500 Internal Server Error: fetch failed"
                event = {"type": "turn.failed", "error": {"message": message}}
                for stdout, stderr in ((json.dumps(event), ""), ("", message)):
                    with self.subTest(provider=provider, prefix=prefix, stream="stdout" if stdout else "stderr"):
                        proc = subprocess.CompletedProcess([], 1, stdout, stderr)
                        result = codex.parse(proc, agent.NO_SESSION, self.model, None, provider)
                        self.assertFalse(result.ok)
                        self.assertIsNone(result.usage_limit)

    def test_pi_http_error_in_reply_or_early_failure_is_not_a_limit(self):
        multiline = "Request failed\n500 Internal Server Error"
        prefixed = "Error: 500 Internal Server Error: fetch failed"
        cases = (
            ({"type": "error", "message": "fetch failed"}, multiline),
            ({"type": "error", "message": prefixed}, ""),
            ({"type": "agent_end", "messages": [
                {"role": "assistant", "stopReason": "error", "errorMessage": f"{multiline}: fetch failed"},
            ]}, ""),
            ({"type": "agent_end", "messages": [
                {"role": "assistant", "stopReason": "error", "errorMessage": prefixed},
            ]}, ""),
        )
        for mode, family in (("subscription", "codex"), ("subscription", "claude"), ("api_key", "codex")):
            auth = accounts.Auth(mode=mode, family=family, provider="openai")
            for event, stderr in cases:
                with self.subTest(mode=mode, family=family, event=event, stderr=stderr):
                    proc = subprocess.CompletedProcess([], 1, json.dumps(event), stderr)
                    result = pi.parse(proc, agent.NO_SESSION, self.model, None, auth)
                    self.assertFalse(result.ok)
                    self.assertIsNone(result.usage_limit)

    def test_pi_claude_subscription_overload_event_wins_over_weekly_limit_reply(self):
        model = "anthropic/claude-opus-4-7"
        auth = accounts.Auth(mode="subscription", family="claude", provider="anthropic")
        error = {"type": "error", "message": "API Error: 529 Overloaded"}
        reply = {"role": "assistant", "stopReason": "error", "errorMessage": "Weekly limit"}
        end = {"type": "agent_end", "messages": [reply]}
        proc = subprocess.CompletedProcess([], 1, "\n".join(map(json.dumps, (error, end))), "")
        result = pi.parse(proc, agent.NO_SESSION, model, None, auth)
        self.assertFalse(result.ok)
        limit = result.usage_limit
        self.assertIsNotNone(limit)
        self.assertEqual((limit.kind, limit.scope), ("overloaded", agent.model_scope(model)))
        self.assertAlmostEqual(minutes_ahead(limit), 10, delta=0.5)

    def test_claude_error_prefix_in_final_result_does_not_hide_http_status(self):
        result = claude.parse(claude_failure("Error: 500 Internal Server Error: fetch failed"),
                              agent.NO_SESSION, self.model, None)
        self.assertFalse(result.ok)
        self.assertIsNone(result.usage_limit)

    def test_claude_http_status_at_start_of_stderr_is_not_a_limit(self):
        proc = claude_failure("fetch failed")
        proc.stderr = "500 Internal Server Error"
        result = claude.parse(proc, agent.NO_SESSION, self.model, None)
        self.assertFalse(result.ok)
        self.assertIsNone(result.usage_limit)

    def test_claude_early_exit_checks_decoded_diagnostics_and_stream_boundaries(self):
        def assistant_event(message):
            return json.dumps({"type": "assistant", "error": "unknown",
                               "message": {"content": [{"type": "text", "text": message}]}})

        prelude = json.dumps({"type": "system", "subtype": "init"})
        for stdout, stderr in (
            (f"{prelude}\n{assistant_event('Request failed\n500 Internal Server Error: fetch failed')}", ""),
            ("500 Internal Server Error", "fetch failed."),
        ):
            with self.subTest(stdout=stdout, stderr=stderr):
                proc = subprocess.CompletedProcess([], 1, stdout, stderr)
                result = claude.parse(proc, agent.NO_SESSION, self.model, None)
                self.assertFalse(result.ok)
                self.assertIsNone(result.usage_limit)

        proc = subprocess.CompletedProcess([], 1, assistant_event(self.timeout_error), "")
        self.assert_unreachable(claude.parse(proc, agent.NO_SESSION, self.model, None).usage_limit)


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
            skipped = []
            for model in models:
                hold = routing.auth_hold(auth, model)
                if not hold:
                    return runner.Picked(self.candidate(which, model), auth, None, model, "", {auth}, None), skipped
                skipped.append({"runner": which, "model": model, "credential": "login", "state": "held",
                                "detail": hold["why"], "until": hold["until"]})
            return None, skipped
        return pick

    def run_chain(self, which, models, family, results, identity):
        auth = accounts.Auth(mode="subscription", family=family, identity=identity)
        results = iter(results)

        def launch(prompt, cwd, **kw):
            self.calls.append(kw["model"])
            return next(results)
        with (mock.patch.object(runner, "pick", self.route_chain(auth, which, models)),
              mock.patch.object(getattr(runner, which), "run", launch),
              mock.patch.object(runner, "wait_until", side_effect=AssertionError("unexpected limit wait"))):
            res = runner.run("review", ".", model=models[0], runner=which, cfg=self.cfg,
                             wait_out_limits=False, say=lambda text: None, trace=("overload-run", "review"))
        return auth, res

    def test_overloaded_route_returns_without_waiting_for_its_limit(self):
        failed = agent.Result(ok=False, error=CAPACITY, usage_limit=codex.usage_limit(CAPACITY, "gpt-6.2-sol"))
        _, res = self.run_chain("codex", ["gpt-6.2-sol"], "codex", [failed], "overload-no-wait")
        self.assertEqual(self.calls, ["gpt-6.2-sol"])
        self.assertFalse(res.ok)
        self.assertEqual(res.usage_limit.until, failed.usage_limit.until)

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
