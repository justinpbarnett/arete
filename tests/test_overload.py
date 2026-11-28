import json
import subprocess
import unittest
from datetime import timedelta
from unittest import mock

from arete import accounts, agent, claude, codex, config, pi, routing, runner, util
from arete.store import devices as devices_store
from tests import support

def setUpModule():
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
        for provider, usage in ((codex.CHATGPT_PROVIDER, CODEX_LIMIT), (codex.KEY_PROVIDER, "Insufficient quota")):
            yield (f"codex {provider}", lambda text, provider=provider: codex.parse(
                self.failure({"type": "turn.failed", "error": {"message": text}}),
                agent.NO_SESSION, self.model, None, provider).usage_limit, usage)
        yield "claude", self.claude_limit, CODEX_LIMIT
        yield "claude api_key", lambda text: self.claude_api_key_result(claude_failure(text)).usage_limit, "Insufficient quota"
        for mode, family in (("subscription", "codex"), ("subscription", "claude"), ("api_key", "codex")):
            auth = accounts.Auth(mode=mode, family=family, provider="openai")
            yield (f"pi {mode} {family}", lambda text, auth=auth: self.pi_error_limit(text, self.model, auth),
                   "Insufficient quota" if mode == "api_key" else CODEX_LIMIT)

    def failure(self, *events, stderr=""):
        return subprocess.CompletedProcess([], 1, "\n".join(map(json.dumps, events)), stderr)

    def claude_limit(self, text):
        return claude.parse(claude_failure(text), agent.NO_SESSION, self.model, None).usage_limit

    def pi_error_limit(self, text, model, auth, *, api="", stderr=""):
        reply = {"role": "assistant", "stopReason": "error", "errorMessage": text, "api": api}
        return pi.parse(self.failure({"type": "agent_end", "messages": [reply]}, stderr=stderr),
                        agent.NO_SESSION, model, None, auth).usage_limit

    def assert_unreachable(self, limit, model=None):
        self.assertIsNotNone(limit)
        self.assertEqual((limit.kind, limit.scope), ("unreachable", agent.model_scope(model or self.model)))
        self.assertAlmostEqual(minutes_ahead(limit), 10, delta=0.5)

    def test_incident_timeout_holds_the_model_for_ten_minutes(self):
        with mock.patch.object(util, "now", return_value=util.now().replace(microsecond=0)):
            for name, read, _ in self.readers():
                with self.subTest(reader=name):
                    limit = read(self.timeout_error)
                    self.assert_unreachable(limit)
                    self.assertEqual(minutes_ahead(limit), 10)

    def test_each_connection_failure_marker_is_recognized(self):
        for text in ("UND_ERR_CONNECT_TIMEOUT", "Connect Timeout Error", "ECONNRESET", "ECONNREFUSED",
                     "ENOTFOUND", "EAI_AGAIN"):
            with self.subTest(text=text):
                self.assert_unreachable(agent.unreachable_limit(text, self.model))

    def claude_api_key_result(self, proc):
        with (mock.patch.object(claude, "command", return_value=[]),
              mock.patch.object(claude, "agent_env", return_value={}),
              mock.patch.object(claude.child, "stream", return_value=proc)):
            return claude.run("review", ".", model=self.model, auth=accounts.Auth(mode="api_key"))

    def test_claude_api_key_keeps_the_model_limit(self):
        result = self.claude_api_key_result(claude_failure(self.timeout_error))
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
            for text in ("tests failed", "the sandbox refused the command", "HTTP error 500: fetch failed",
                         "API Error: 500 Internal Server Error", "fetch failed", "Connection error.", "Request timed out."):
                with self.subTest(reader=name, text=text):
                    self.assertIsNone(read(text))

    def test_claude_structured_http_error_with_connection_code_does_not_hold_the_model(self):
        proc = claude_failure("Upstream connect ECONNREFUSED", 500)
        for mode, result in (("subscription", claude.parse(proc, agent.NO_SESSION, self.model, None)),
                             ("api_key", self.claude_api_key_result(proc))):
            with self.subTest(mode=mode):
                self.assertFalse(result.ok)
                self.assertIsNone(result.usage_limit)

    def test_http_response_messages_with_connection_codes_do_not_hold_the_model(self):
        for name, read, _ in self.readers():
            for text in ("HTTP error 500: upstream connect ECONNREFUSED",
                         'OpenAI API error (500): {"message":"Upstream connect ECONNREFUSED"}',
                         "unexpected status 500 Internal Server Error: upstream connect ECONNREFUSED",
                         '500: {"message":"upstream connect ECONNREFUSED"}',
                         '{"error":{"code":500,"message":"connect ECONNREFUSED"}}',
                         "Upstream request failed (HTTP 502): connect ECONNREFUSED",
                         "connect ECONNREFUSED after status:400",
                         "connect ECONNREFUSED (599)",
                         "connect ECONNREFUSED api.openai.com:443 (timeout: 500 ms)"):
                with self.subTest(reader=name, text=text):
                    self.assertIsNone(read(text))

    def test_pi_adapter_http_responses_with_connection_codes_do_not_hold_the_model(self):
        body = '{"message":"upstream connect ECONNREFUSED"}'
        google = '{"error":{"code":500,"message":"upstream connect ECONNREFUSED","status":"INTERNAL"}}'
        for mode in ("subscription", "api_key"):
            for provider, text in (("github-copilot", f"github-copilot API error (500): {body}"),
                                   ("openai", f"500: {body}"), ("anthropic", f'500 {{"error":{body}}}'),
                                   ("radius", "500 Internal Server Error: upstream connect ECONNREFUSED"),
                                   ("google", google), ("google-vertex", f"got status: INTERNAL. {google}"),
                                   ("azure", f"Azure OpenAI API error (500): {body}")):
                with self.subTest(mode=mode, provider=provider, text=text):
                    auth = accounts.Auth(mode=mode, provider=provider)
                    model = f"{provider}/model-a"
                    self.assertIsNone(self.pi_error_limit(text, model, auth, stderr="pi ended after an error"))
                    self.assert_unreachable(self.pi_error_limit(self.timeout_error, model, auth), model)

    def test_pi_sdk_plain_http_response_keeps_its_status(self):
        for provider, api in (("radius", "anthropic-messages"), ("openai", "openai-completions")):
            with self.subTest(provider=provider):
                model = f"{provider}/model-a"
                auth = accounts.Auth(mode="api_key", provider=provider)
                self.assertIsNone(self.pi_error_limit("500 upstream connect ECONNREFUSED", model, auth, api=api))
                transport = "500ms elapsed: connect ECONNREFUSED upstream.example:500"
                for text in (transport, f"Connection error.: {transport}"):
                    self.assert_unreachable(self.pi_error_limit(text, model, auth, api=api), model)

    def test_pi_bedrock_http_response_diagnostic_does_not_hold_the_model(self):
        model = "amazon-bedrock/model-a"
        auth = accounts.Auth(mode="api_key", provider="amazon-bedrock")
        reply = {"role": "assistant", "stopReason": "error", "errorMessage": "upstream connect ECONNREFUSED"}
        response = dict(reply, diagnostics=[{"type": "bedrock_response_failure", "details": {"status": 500}}])
        for kind in ("message_end", "agent_end"):
            with self.subTest(event=kind):
                proc = self.failure({"type": kind, "message": response, "messages": [response]})
                self.assertIsNone(pi.parse(proc, agent.NO_SESSION, model, None, auth).usage_limit)
        self.assert_unreachable(self.pi_error_limit(reply["errorMessage"], model, auth), model)

    def test_claude_structured_output_validation_error_does_not_hold_the_model(self):
        diagnostic = ("Failed to provide valid structured output after 5 attempts \u2014 last StructuredOutput error: "
                      "Invalid enum value: ECONNREFUSED")
        validation = {"type": "result", "subtype": "error_max_structured_output_retries", "is_error": True,
                      "errors": [diagnostic]}
        execution = dict(validation, subtype="error_during_execution", errors=[self.timeout_error])
        for mode, read in (("subscription", lambda proc: claude.parse(proc, agent.NO_SESSION, self.model, None)),
                           ("api_key", self.claude_api_key_result)):
            with self.subTest(mode=mode):
                result = read(self.failure(validation))
                self.assertFalse(result.ok)
                self.assertIn(diagnostic, result.error)
                self.assertIsNone(result.usage_limit)
                self.assert_unreachable(read(self.failure(validation, stderr=self.timeout_error)).usage_limit)
                self.assert_unreachable(read(self.failure(execution)).usage_limit)

    def test_claude_refusal_text_does_not_hold_the_model(self):
        refusal = {"type": "result", "subtype": "success", "is_error": True, "stop_reason": "refusal",
                   "result": "I can’t help trigger ECONNREFUSED.", "structured_output": {"outcome": "refuse"}}
        proc = self.failure(refusal)
        for mode, result in (("subscription", claude.parse(proc, agent.NO_SESSION, self.model, None)),
                             ("api_key", self.claude_api_key_result(proc))):
            with self.subTest(mode=mode):
                self.assertFalse(result.ok)
                self.assertEqual(result.error, refusal["result"])
                self.assertIsNone(result.usage_limit)

        genuine_failures = (self.failure(refusal, stderr=self.timeout_error),
                            self.failure(dict(refusal, api_error=self.timeout_error)),
                            self.failure(dict(refusal, errors=[self.timeout_error])))
        for proc in genuine_failures:
            with self.subTest(stdout=proc.stdout, stderr=proc.stderr):
                self.assert_unreachable(claude.parse(proc, agent.NO_SESSION, self.model, None).usage_limit)

    def test_ports_and_numbers_inside_words_do_not_prevent_a_connection_hold(self):
        for name, read, _ in self.readers():
            with self.subTest(reader=name):
                self.assert_unreachable(read("connect ECONNREFUSED api.openai.com:500 (timeout: 500ms) attempt 1500"))

    def test_status_veto_considers_all_provider_failures(self):
        codex_failure = {"type": "turn.failed", "error": {"message": self.timeout_error}}
        pi_reply = {"role": "assistant", "stopReason": "error", "errorMessage": self.timeout_error}
        pi_failure = {"type": "agent_end", "messages": [pi_reply]}
        claude_proc = claude_failure(self.timeout_error)
        claude_proc.stderr = "Earlier request returned 500"
        cases = (
            (claude.parse, claude_proc, ()),
            (codex.parse, self.failure({"type": "error", "message": "Retrying", "status": 500}, codex_failure),
             (codex.CHATGPT_PROVIDER,)),
            (pi.parse, self.failure({"type": "error", "error": {"message": "Retrying", "code": 500}}, pi_failure), ()),
            (pi.parse, self.failure(dict(pi_failure, messages=[dict(pi_reply, errorMessage="Retrying after 500"),
                                                               pi_reply])), ()),
        )
        for read, proc, args in cases:
            with self.subTest(reader=read.__module__, stdout=proc.stdout):
                self.assertIsNone(read(proc, agent.NO_SESSION, self.model, None, *args).usage_limit)

    def test_status_in_tool_and_assistant_output_does_not_veto_connection_hold(self):
        claude_output = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "HTTP 500"},
        ]}}
        codex_output = {"type": "item.completed", "item": {"type": "agent_message", "text": "HTTP 500"}}
        pi_output = {"role": "toolResult", "content": [{"type": "text", "text": "HTTP 500"}]}
        pi_reply = {"role": "assistant", "stopReason": "error", "errorMessage": self.timeout_error,
                    "content": [{"type": "text", "text": "HTTP 500"}]}
        cases = (
            (claude.parse, self.failure(claude_output, stderr=self.timeout_error), ()),
            (codex.parse, self.failure(codex_output, stderr=self.timeout_error), (codex.CHATGPT_PROVIDER,)),
            (pi.parse, self.failure({"type": "agent_end", "messages": [pi_output, pi_reply]}), ()),
        )
        for read, proc, args in cases:
            with self.subTest(reader=read.__module__):
                self.assert_unreachable(read(proc, agent.NO_SESSION, self.model, None, *args).usage_limit)

    def test_pi_claude_subscription_preserves_native_overload_precedence(self):
        model = "anthropic/claude-opus-4-7"
        auth = accounts.Auth(mode="subscription", family="claude", provider="anthropic")
        error = {"type": "error", "message": f"API Error: 529 Overloaded\n{self.timeout_error}"}
        reply = {"role": "assistant", "stopReason": "error", "errorMessage": "Weekly limit"}
        end = {"type": "agent_end", "messages": [reply]}
        limit = pi.parse(self.failure(error, end), agent.NO_SESSION, model, None, auth).usage_limit
        self.assertIsNotNone(limit)
        self.assertEqual((limit.kind, limit.scope), ("overloaded", agent.model_scope(model)))
        self.assertAlmostEqual(minutes_ahead(limit), 10, delta=0.5)

        limit = self.pi_error_limit(f"Weekly limit\n{self.timeout_error}", model, auth)
        self.assertIsNotNone(limit)
        self.assertEqual((limit.kind, limit.scope), ("subscription", claude.EVERY_MODEL))

    def test_stream_failures_and_stderr_can_hold_the_model(self):
        event = {"type": "error", "message": self.timeout_error}
        pi_error = {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                      "errorMessage": self.timeout_error}}
        claude_error = {"type": "result", "subtype": "error_during_execution", "errors": [self.timeout_error]}
        stderr = self.failure(stderr=self.timeout_error)
        for read, cases in ((claude.parse, (stderr, self.failure(claude_error))),
                            (pi.parse, (stderr, self.failure(event), self.failure(pi_error)))):
            for proc in cases:
                with self.subTest(reader=read.__module__, stdout=proc.stdout):
                    self.assert_unreachable(read(proc, agent.NO_SESSION, self.model, None).usage_limit)
        for proc in (stderr, self.failure(event)):
            with self.subTest(reader="codex", stdout=proc.stdout):
                self.assert_unreachable(codex.parse(proc, agent.NO_SESSION, self.model, None,
                                                    codex.CHATGPT_PROVIDER).usage_limit)

    def test_stdout_and_nonfailure_fields_do_not_hold_the_model(self):
        stdout = subprocess.CompletedProcess([], 1, self.timeout_error, "")
        claude_answer = self.failure({"type": "result", "subtype": "success", "result": self.timeout_error})
        codex_error = self.failure({"type": "error", "message": "request failed", "context": self.timeout_error})
        tool = {"type": "message_end", "error": self.timeout_error, "message": {"role": "toolResult"}}
        pi_error = {"role": "assistant", "stopReason": "error", "errorMessage": "request failed",
                    "content": [{"type": "text", "text": self.timeout_error}], "context": self.timeout_error}
        pi_end = {"type": "agent_end", "messages": [tool["message"], pi_error]}
        for read, cases in ((claude.parse, (stdout, claude_answer)),
                            (pi.parse, (stdout, self.failure(tool), self.failure(tool, pi_end)))):
            for proc in cases:
                with self.subTest(reader=read.__module__, stdout=proc.stdout):
                    self.assertIsNone(read(proc, agent.NO_SESSION, self.model, None).usage_limit)
        self.assertIsNone(codex.parse(codex_error, agent.NO_SESSION, self.model, None,
                                      codex.CHATGPT_PROVIDER).usage_limit)

    def test_claude_tool_result_followed_by_timeout_does_not_hold_the_model(self):
        tool_result = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "shell-1", "content": "Connection failed: ECONNREFUSED"},
        ]}}
        timeout = subprocess.TimeoutExpired([], 1, output=json.dumps(tool_result).encode())
        with (mock.patch.object(claude, "command", return_value=[]),
              mock.patch.object(claude, "agent_env", return_value={}),
              mock.patch.object(claude.child, "stream", side_effect=timeout)):
            result = claude.run("review", ".", model=self.model, timeout=1, auth=accounts.Auth(mode="subscription"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timed out after 1s")
        self.assertIsNone(result.usage_limit)


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
        from arete import record as record_mod
        run_id = record_mod.start("session")
        with (mock.patch.object(runner, "pick", self.route_chain(auth, which, models)),
              mock.patch.object(getattr(runner, which), "run", launch),
              mock.patch.object(runner, "wait_until", side_effect=AssertionError("unexpected limit wait"))):
            res = runner.run("review", ".", model=models[0], runner=which, cfg=self.cfg,
                             wait_out_limits=False, say=lambda text: None, trace=(run_id, "review"))
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
