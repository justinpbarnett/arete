import json
import os
import shlex
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict
from http.client import HTTPResponse
from io import BytesIO, StringIO
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest import mock

from arete import (
    accounts,
    api_http,
    claude,
    cli,
    client,
    codebase,
    config,
    devices,
    factory,
    oncall,
    paths,
    pi,
    prices,
    record,
    routing,
    runner,
    secret_move,
    steps,
    util,
)
from tests.test_behavior import (
    SECRET_CLI_HTTP_FIXTURE,
    SECRET_HTTP_FIXTURE,
    _CliThroughServeCase,
    _SecretFileWorkflowCase,
)


REPO_ASK_FIXTURE = r"""
from arete import tasks, vault
from arete.store import asks


def answer_ask(values):
    questions = [{'kind': 'secret', 'header': 'Credential', 'name': name} for name in values]
    asks.ask_add('demo-ask', 'run1', 'demo', '1', 'build', '', '', '', questions, {})
    cfg = config.load()
    row = {'id': 'run1', 'status': 'needs-info', 'issue': '1'}
    body = {'answers': [{'secret': value} for value in values.values()]}
    with mock.patch.multiple(tasks, run_of=mock.Mock(return_value=(cfg, cfg.repo('demo'), row)),
                             no_later_run=mock.Mock(), live_run=mock.Mock(return_value=None),
                             free_to_start=mock.Mock(), continuing=mock.Mock(),
                             continue_asked=mock.Mock(return_value={'state': 'running'})):
        assert tasks.answer_ask('demo-ask', body) == {'state': 'running'}
    answered = asks.ask_get('demo-ask')
    assert answered['status'] == 'answered'
    assert all(entry['set'] for entry in answered['answers'])
    assert not any(value in json.dumps(answered) for value in values.values())
"""


class SecretRepoCompatibilityTests(_SecretFileWorkflowCase):
    def test_no_store_secret_answer_and_local_removal_use_the_legacy_file(self):
        self.run_file_workflow(REPO_ASK_FIXTURE + r"""
from arete import client

paths.CONFIG_FILE.write_text('')
add_repo()
assert config.secret_settings().store is None
write_fixture(repo_file, json.dumps({'OTHER_TOKEN': 'fixture-keep-existing'}))
answer_ask({'_answerKey': repo_value})
assert vault.read('demo') == {'_answerKey': repo_value, 'OTHER_TOKEN': 'fixture-keep-existing'}
assert secretstore.get('repo:demo', '_answerKey') is None
with mock.patch.object(client, 'call', side_effect=AssertionError('local removal contacted serve')):
    status, out, err = run_cli('secrets', 'rm', 'demo', '_answerKey')
    assert (status, out, err) == (0, 'removed _answerKey from demo\n', '')
    assert run_cli('secrets', 'rm', 'demo', '_answerKey')[0] == 1
assert json.loads(repo_file.read_text()) == {'OTHER_TOKEN': 'fixture-keep-existing'}
""")

    def test_configured_answers_and_adoption_preserve_repo_environment_names_only(self):
        self.run_file_workflow(REPO_ASK_FIXTURE + r"""
add_repo()
answers = {'_answerKey': repo_value, 'camelCase': 'fixture-mixed-case-key'}
answer_ask(answers)
assert not repo_file.exists()
assert all(secretstore.get('repo:demo', name)['kind'] == 'repo_answer' for name in answers)
legacy = {'_legacyKey': 'fixture-private-legacy-key', 'legacyKey': 'fixture-mixed-legacy-key'}
write_fixture(repo_file, json.dumps(legacy))
assert not secretstore.move_files()['problems']
assert not repo_file.exists() and vault.read('demo') == {**answers, **legacy}
assert all([e['event'] for e in secretstore.history('repo:demo', name)] == ['moved'] for name in legacy)
for scope in ('global', 'connector:demo', 'line:demo', 'device:demo'):
    for name in answers:
        with case.assertRaisesRegex(ValueError, 'invalid secret name'):
            secretstore.validate(scope, name)
""")

    def test_mixed_case_repo_recovery_record_is_not_skipped_or_overwritten(self):
        self.run_file_workflow(r"""
add_repo()
secretstore.fingerprint_key()
scope, name = 'repo:demo', '_pendingKey'
with mock.patch.object(secretstore, 'save_change', side_effect=OSError('fixture interrupted registry write')):
    with case.assertRaisesRegex(OSError, 'fixture interrupted'):
        secretstore.put(scope, name, repo_value, 'repo_answer', 'hand')
journal, _ = secretstore.pending_path(f'{scope}/{name}')
assert secretstore.get(scope, name) is None
receipt = journal.read_bytes()
write_fixture(repo_file, json.dumps({name: 'fixture-unpublished-legacy-value'}))
assert secretstore.move_files()['problems'] and repo_file.exists()
assert journal.read_bytes() == receipt
assert secretstore.check(scope, name)['status'] == 'set'
assert secretstore.read(scope, name) == repo_value.encode()
""")


class AuthorityRepoRemovalTests(_SecretFileWorkflowCase):
    def test_cli_presence_refusal_leaves_both_copies_until_owner_uses_the_route(self):
        self.run_file_workflow(SECRET_HTTP_FIXTURE + SECRET_CLI_HTTP_FIXTURE + r"""
add_repo()
name, scope = '_removeKey', 'repo:demo'
write_fixture(repo_file, json.dumps({name: repo_value, 'KEEP': 'fixture-retained-key'}))
row = secretstore.put(scope, name, repo_value, 'repo_answer', actor)
ref = secretstore.Reference(row['store'], db.schema_metadata()['instance_tag'], f'{scope}/{name}')
settings = config.secret_settings()
route = root + '/repo%3Ademo/' + name
before = repo_file.read_bytes()
with cli_over_http() as trace, mock.patch.object(client, 'configured_server', return_value=None):
    status, out, err = run_cli('secrets', 'rm', 'demo', name)
    assert status == 1 and json.loads(out)['reason'] == 'presence_required', (status, out, err)
    assert 'paired owner browser' in json.loads(out)['hint'] and repo_value not in out + err
assert ('DELETE', route, 403) in trace, trace
assert repo_file.read_bytes() == before and secretstore.read_item(ref, settings) == repo_value.encode()

_, result = request('DELETE', route, cookie=browser_session(), key='remove-repo-answer')
assert result['secret']['present'] is False and repo_value not in json.dumps(result)
assert secretstore.read_item(ref, settings) is None
assert json.loads(repo_file.read_text()) == {'KEEP': 'fixture-retained-key'}
""")

    def test_remote_missing_store_does_not_trigger_local_legacy_removal(self):
        self.run_file_workflow(SECRET_HTTP_FIXTURE + SECRET_CLI_HTTP_FIXTURE + r"""
paths.CONFIG_FILE.write_text('')
add_repo()
write_fixture(repo_file, json.dumps({'_legacyKey': repo_value}))
before = repo_file.read_bytes()
route = root + '/repo%3Ademo/_legacyKey'
with cli_over_http() as trace, mock.patch.dict(os.environ, {client.ENV: 'https://127.0.0.1:8796'}):
    status, out, err = run_cli('secrets', 'rm', 'demo', '_legacyKey')
    assert status == 1 and json.loads(out)['code'] == 'secret_store_missing', (status, out, err)
assert ('DELETE', route, 501) in trace, trace
assert repo_file.read_bytes() == before
""")


class CliRouteFallbackTests(unittest.TestCase):
    routes = _CliThroughServeCase.ONLINE | _CliThroughServeCase.ONLINE_VARIANTS
    key = "compat-key"

    def setUp(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.multiple(paths, STATE_DIR=state, EVENTS_FILE=state / "events.jsonl",
                                              DB_FILE=state / "arete.db"))
        self.enterContext(mock.patch.object(config, "read_data", return_value={}))
        self.enterContext(mock.patch.object(codebase, "load"))
        self.enterContext(mock.patch.object(client, "check_instance"))
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("ARETE_IDEMPOTENCY_KEY", None)
        self.enterContext(mock.patch.object(client, "secrets", SimpleNamespace(token_hex=lambda _: self.key)))

    def pending_event(self):
        event = {"ts": "2026-01-01T00:00:00Z", "kind": "skill", "cls": "invoked", "detail": "pending"}
        paths.EVENTS_FILE.write_text(json.dumps(event) + "\n")
        return event

    def cli(self, command, stdin=""):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", StringIO(stdin)):
            code = cli.main(command.split())
        return code, out.getvalue().splitlines(), err.getvalue()

    problem = _CliThroughServeCase.problem

    @contextmanager
    def stub(self, response, url="http://serve.example.test"):
        def connect(where, seconds=client.CONNECT_SECONDS):
            transport = mock.Mock()

            def request(method, path, body, headers):
                raw = body or b""
                incoming = SimpleNamespace(command=method, path=path, rfile=BytesIO(raw),
                                           headers={**headers, "Content-Length": str(len(raw))})
                status, answer, extra_headers = response(incoming)
                payload = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                headers = {**extra_headers, "Content-Length": str(len(payload))}
                prefix = f"HTTP/1.0 {status} stub\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                transport.makefile.return_value = BytesIO(prefix.encode() + b"\r\n" + payload)

            def getresponse():
                result = HTTPResponse(transport)
                result.begin()
                return result

            return SimpleNamespace(sock=transport, request=request, getresponse=getresponse, close=transport.close)

        with (mock.patch.dict(os.environ, {client.ENV: url}),
              mock.patch.object(client, "connect", side_effect=connect)):
            yield

    def test_missing_v1_routes_fall_back_once(self):
        seq = "1.9"
        summary = {"id": "t-1234", "repo": "replay-sample", "title": "Change", "kind": "feat", "state": "todo",
                   "updated": "2026-01-03T00:00:00Z"}
        task = {**summary, "body": "Full task description", "parent": "t-5678", "created": "2026-01-02T00:00:00Z",
                "version": 8, "title": "Edited after the list was read"}
        parent = {**task, "id": "t-5678", "title": "Parent", "body": "Parent description", "parent": "",
                  "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z", "version": 2}
        parent_summary = {key: parent[key] for key in summary}
        stop = {"id": "r1", "stopped": True, "pid": 123}
        meta = "GET /api/v1/meta"
        info = "GET /api/runs/r1"
        feed = "GET /api/feed/r1?after=0"
        loop = "GET /api/loops/example"
        webhook = "GET /api/v1/loops/example"
        rotate = "POST /api/v1/loops/example/webhook/rotate"
        base = "https://serve.example.test:8796"
        hook_path = "/hook/example"
        ask = "GET /api/asks"
        events_route = "GET /api/runs/r1/events?from=0"
        follow_events = events_route + "&follow=1"
        session_start = "POST /api/sessions"
        task_details = ["GET /api/tasks/t-1234", "GET /api/tasks/t-5678"]
        report = b'{"event":"report","seq":1,"status":"done"}\n'
        config_list = "GET /api/v1/config/step_settings"
        config_read = f"{config_list}/defaults"
        config_item = {"kind": "step_settings", "name": "defaults", "path": "config.toml"}
        config_replies = {config_list: {"items": [config_item]}, config_read: {"item": {**config_item, "content": ""}},
                          f"{config_read}/versions": {"versions": []}, f"{config_read}/diff": {"diff": ""}}
        replies = {
            **config_replies,
            meta: {"instance": {"id": "fallback"}}, loop: {"running": "r1"},
            webhook: {"loop": {"detail": {"webhook_path": hook_path, "secret_present": True}}},
            ask: {"asks": [{"run": "r1", "id": "1", "questions": []}]},
            feed: {"restore_epoch": 0, "lines": []},
            events_route: report, follow_events: report,
            "POST /api/tasks": {"run": "r1", "task": "t-1234", "state": "running"},
            "GET /api/tasks": {"tasks": [summary, parent_summary]},
            **{route: {**row, "comments": [], "runs": [], "children": []}
               for route, row in zip(task_details, (task, parent), strict=True)},
            "GET /api/v1/tasks": {"seq": seq, "tasks": [parent, task]},
            "POST /api/runs/r1/stop": stop, "POST /api/v1/runs/r1/stop": {"seq": seq, **stop},
            "POST /api/asks/1/answer": {}, "POST /api/runs/r1/resume": {}, "POST /api/runs/r1/repair": {},
            "POST /api/runs/r1/steps/build": {}, "POST /api/approvals/1": {},
            "POST /api/proposals/1/done": {}, "POST /api/proposals/1/approve": {}, "POST /api/proposals/1/reject": {},
            "POST /api/loops/example/start": {"id": "r1"},
            "POST /api/loops/example/enable": {"name": "example", "enabled": True, "serve_up": True},
            "POST /api/loops/example/disable": {"name": "example", "enabled": False},
            session_start: {"id": "r1", "task": False, "runner": "pi", "model": "test", "device": "local",
                            "cwd": "/tmp", "branch": "", "tools": []},
            "POST /api/incidents/switch": {}, "POST /api/incidents/1/cancel": {"stopped": "", "cooldown_until": ""},
            "POST /api/incidents/1/dismiss": {}, "POST /api/incidents/1/retry": {},
            "GET /api/live": {"live": {"rows": {"runs": []}}},
            "GET /api/chats/1/sessions": {}, "POST /api/chats/1/sessions": {},
            "GET /api/chats/1/sessions/r1/report": {}, "POST /api/chats/1/sessions/r1/stop": {},
        }
        canonical = {"GET /api/tasks": "GET /api/v1/tasks", "POST /api/runs/r1/stop": "POST /api/v1/runs/r1/stop"}
        before = {
            "answer r1": [ask], "resume r1": [info], "repair r1": [info], "step r1 build": [info],
            "stop r1": [info], "session stop r1": [info], "loop stop example": [loop, info],
        }
        after = {"run replay-sample 7": [follow_events], "loop start example -f": [feed, info] * 2,
                 "session start --attach work": [info, feed, info, feed, info]}
        values = {"task": "t-1234", "run": "r1", "session": "r1", "id": 1, "step": "build", "name": "example", "chat": 1}
        command_values = {
            "webhook example": dict(values, id="example"),
            "webhook example --rotate": dict(values, loop="example"),
        }
        config_values = dict(values, kind="step_settings", name="defaults")
        seen = []
        old = historical = False

        def response(request):
            path = request.path
            route = f"{request.command} {path}"
            body = request.rfile.read(int(request.headers.get("Content-Length", "0")))
            seen.append((route, request.headers.get("Idempotency-Key"), body))
            if old and path.startswith("/api/v1/") and route != meta:
                if historical:
                    return 404, {"error": "Nothing at GET /api/v1/tasks."}, {}
                return 404, {"code": "unknown_route", "error": "No such route."}, {}
            if route == rotate:
                return 403, {"code": "forbidden", "reason": "presence_required", "error": "Owner presence required."}, {}
            if route == info:
                return 200, {"run": {"kind": "session", "status": "done", "version": 7}}, {}
            if route in replies:
                return 200, replies[route], {"Arete-Seq": seq} if route == "POST /api/runs/r1/stop" else {}
            return 500, {"error": f"unexpected request {route}"}, {}

        with self.stub(response, base):
            for command, template in self.routes.items():
                if template is None:
                    continue
                with self.subTest(command=command):
                    params = config_values if "{kind}" in template else command_values.get(command, values)
                    legacy = template.format(**params)
                    if legacy == "GET /api/runs/r1/events":
                        legacy = follow_events if command == "wait r1" else events_route
                    primary = canonical.get(legacy, legacy)
                    prefix = [meta, *before.get(command, [])]
                    suffix = after.get(command, [])
                    primary_key = f"{self.key}:r1" if command == "loop stop example" else self.key

                    def expected(routes):
                        return [(route, None if route.startswith("GET ") else primary_key) for route in routes]

                    old = False
                    seen.clear()
                    current = self.cli(command)
                    if primary == rotate:
                        code, lines, err = current
                        self.assertEqual((code, len(lines), err), (1, 1, ""), current)
                        problem = json.loads(lines[0])
                        self.assertEqual((problem["code"], problem["reason"]), ("forbidden", "presence_required"))
                        self.assertNotIn("text", problem)
                    else:
                        self.assertEqual(current[0], 0, current)
                        if primary in config_replies:
                            self.assertEqual(list(map(json.loads, current[1])), [config_replies[primary]])
                        if primary == webhook:
                            self.assertEqual(current, (0, [f"POST {base}{hook_path}", "secret_present: true"], ""))
                    self.assertEqual([(route, key) for route, key, _ in seen], expected([*prefix, primary, *suffix]))
                    current_requests = seen.copy()

                    old = True
                    retry = [] if primary == legacy else [legacy]
                    if legacy == "GET /api/tasks":
                        retry += task_details
                    for historical in ((False, True) if legacy == "GET /api/tasks" else (False,)):
                        with self.subTest(historical=historical):
                            seen.clear()
                            result = self.cli(command)
                            if primary in config_replies or primary in (webhook, rotate):
                                code, lines, err = result
                                self.assertEqual((code, len(lines), err), (1, 1, ""), result)
                                self.assertEqual(json.loads(lines[0])["code"], "unknown_route")
                            else:
                                self.assertEqual(result, current)
                            self.assertEqual([(route, key) for route, key, _ in seen],
                                             expected([*prefix, primary, *retry, *suffix]))
                            self.assertEqual(seen[:len(prefix) + 1], current_requests[:len(prefix) + 1])
                            if retry:
                                self.assertEqual(seen[len(prefix) + 1][2], current_requests[len(prefix)][2])
                            if legacy == "GET /api/tasks":
                                self.assertEqual([json.loads(line) for line in result[1]], [parent, task])
                            if legacy == "POST /api/runs/r1/stop":
                                self.assertEqual(json.loads(seen[len(prefix) + 1][2]), {"base_version": 7})

    def test_task_list_matches_v1_with_60_rate_limited_legacy_tasks(self):
        rows = [
            {"id": f"t-{i:04x}", "repo": "replay-sample", "title": f"Task {i}", "body": f"Task {i}\nDétails",
             "kind": "feat" if i % 2 else "fix", "state": "todo" if i % 3 else "done",
             "parent": "t-0000" if i % 2 else "", "created": f"2026-01-01T00:00:{i:02d}Z",
             "updated": f"2026-01-03T00:00:{i * 17 % 60:02d}Z", "version": i + 1}
            for i in range(60)
        ]
        summary_fields = ("id", "repo", "title", "kind", "state", "updated")
        newest_updates = sorted(rows, key=lambda row: row["updated"], reverse=True)
        summaries = [{k: row[k] for k in summary_fields} for row in newest_updates]
        details = {f"/api/tasks/{row['id']}": {**row, "comments": [], "runs": [], "children": []}
                   for row in newest_updates}
        meta, primary, legacy = "/api/v1/meta", "/api/v1/tasks", "/api/tasks"
        now = 0.0
        pauses = []
        seen = []
        old = False

        def sleep(seconds):
            nonlocal now
            pauses.append(seconds)
            now += seconds

        clock = SimpleNamespace(monotonic=lambda: now, sleep=sleep)
        limits = api_http.RateLimits(clock.monotonic)

        def response(request):
            nonlocal now
            self.assertEqual(request.command, "GET")
            self.assertNotIn("Idempotency-Key", request.headers)
            path = request.path
            seen.append(path)
            headers = {"Arete-API": "1.1" if old else "1.2"}
            now += 0.001
            try:
                limits.take("legacy" if old else "v1", command=False)
            except api_http.RateLimited as error:
                return (429, {"error": str(error), "code": error.code, **error.details},
                        {**headers, "Retry-After": str(error.retry_after)})
            if path == meta:
                return 200, {"instance": {"id": "api-1-1"}}, headers
            if path == primary:
                if old:
                    return 404, {"error": "Nothing at GET /api/v1/tasks."}, headers
                return 200, {"seq": "1.9", "tasks": rows}, headers
            if path == legacy:
                return 200, {"tasks": summaries}, headers
            return 200, details[path], headers

        with self.stub(response), mock.patch.object(client, "time", clock):
            current = self.cli("task list")
            self.assertEqual((current[0], current[2]), (0, ""), current)
            self.assertEqual([json.loads(line) for line in current[1]], rows)
            self.assertEqual(seen, [meta, primary])

            old = True
            seen.clear()
            fallback = self.cli("task list")

        self.assertEqual(fallback, current)
        self.assertEqual(pauses, [1])
        self.assertEqual(seen[:3], [meta, primary, legacy])
        self.assertEqual(list(dict.fromkeys(seen[3:])), list(details))
        self.assertEqual(len(seen[3:]), 61)

    def test_api_1_0_missing_v1_routes(self):
        seen = []
        stop = "/api/v1/runs/r1/stop"
        legacy_stop = "/api/runs/r1/stop"
        stopped = {"id": "r1", "stopped": True, "pid": 123}
        status, failure = 404, None

        def response(request):
            path = request.path
            if request.command == "GET" and path in ("/api/v1/meta", "/api/meta"):
                if path == "/api/v1/meta":
                    return 404, {"error": f"Nothing at GET {path}."}, {"Arete-API": "1.0"}
                return 200, {"instance": {"id": "api-1-0"}}, {}
            if request.command == "GET" and path == "/api/runs/r1":
                return 200, {"run": {"version": 7}}, {}
            seen.append((request.command, path, request.headers.get("Idempotency-Key")))
            if path.startswith("/api/v1/"):
                return status, failure or {"error": f"Nothing at {request.command} {path}."}, {"Arete-API": "1.0"}
            if path == legacy_stop:
                return 200, stopped, {"Arete-Seq": "1.9"}
            return 500, {"error": f"Unexpected request to {path}."}, {}

        with self.stub(response):
            code, lines, err = self.cli("stop r1")
            self.assertEqual((code, len(lines), err), (0, 1, ""), lines)
            self.assertEqual(json.loads(lines[0]), stopped)
            self.assertEqual(seen, [("POST", stop, self.key), ("POST", legacy_stop, self.key)])

            for status, message in ((404, "No such run."), (404, f"Nothing at GET {stop}."),
                                    (404, "Nothing at POST /api/v1/runs/other/stop."), (409, f"Nothing at POST {stop}."),
                                    (422, f"Nothing at POST {stop}."), (503, f"Nothing at POST {stop}.")):
                with self.subTest(status=status, message=message):
                    failure = {"error": message}
                    seen.clear()
                    self.assertEqual(self.problem("stop r1", status)["error"], message)
                    self.assertEqual(seen, [("POST", stop, self.key)])

    def test_v1_failures_without_a_legacy_retry(self):
        seen = []
        status, reason, headers = 404, "unknown_route", {}
        legacy_status = 503

        def response(request):
            if request.path == "/api/v1/meta":
                return 200, {"instance": {"id": "compat-errors"}}, {}
            if request.path == "/api/runs/r1":
                return 200, {"run": {"version": 7}}, {}
            seen.append((request.path, request.headers.get("Idempotency-Key")))
            if request.path.startswith("/api/v1/"):
                if reason == "legacy_route_missing":
                    return 404, {"code": "not_found", "type": "urn:arete:problem:not_found",
                                 "error": "No such route.", "detail": "No such route."}, {}
                return status, {"error": "The request failed.", "code": reason}, headers
            return legacy_status, {"error": "The legacy request failed.", "code": "legacy_error"}, {"Retry-After": "0"}

        with self.stub(response), mock.patch.object(client, "RESTART_WAIT_SECONDS", 0):
            for status, reason, case_headers in ((404, "not_found", {}), (405, "method_not_allowed", {}),
                                                 (409, "unknown_route", {}),
                                                 (422, "unknown_route", {}), (503, "unknown_route", {}),
                                                 (503, "draining", {"Retry-After": "0"})):
                headers = case_headers
                with self.subTest(status=status, reason=reason):
                    seen.clear()
                    self.problem("stop r1", reason)
                    self.assertEqual(seen, [("/api/v1/runs/r1/stop", self.key)])

            status, headers = 404, {}
            for reason, legacy_status in (("unknown_route", 404), ("unknown_route", 503), ("legacy_route_missing", 404)):
                with self.subTest(reason=reason, legacy_status=legacy_status):
                    seen.clear()
                    self.problem("stop r1", "legacy_error")
                    self.assertEqual(seen, [("/api/v1/runs/r1/stop", self.key), ("/api/runs/r1/stop", self.key)])

    def test_stop_does_not_fall_back_after_a_v1_restart(self):
        seen = []
        stop = "/api/v1/runs/r1/stop"
        replies = iter(((503, {"code": "draining", "error": "Server is draining."}, {"Retry-After": "0"}),
                        (404, {"code": "unknown_route", "error": "No such route."}, {})))

        def response(request):
            seen.append((request.command, request.path, request.headers.get("Idempotency-Key")))
            if request.path == "/api/v1/meta":
                return 200, {"instance": {"id": "rollback"}}, {}
            if request.path == "/api/runs/r1":
                return 200, {"run": {"version": 7}}, {}
            if request.path == stop:
                return next(replies)
            if request.path == "/api/runs/r1/stop":
                return 200, {"id": "r1", "stopped": True, "pid": 123}, {"Arete-Seq": "1.9"}
            self.fail(f"unexpected request {request.command} {request.path}")

        with self.stub(response):
            code, lines, err = self.cli("stop r1")
        self.assertEqual(seen, [("GET", "/api/v1/meta", None), ("GET", "/api/runs/r1", None),
                                ("POST", stop, self.key), ("GET", "/api/v1/meta", None), ("POST", stop, self.key)])
        self.assertEqual((code, len(lines), err), (1, 1, ""), lines)
        self.assertEqual(json.loads(lines[0])["code"], "unknown_route")

    def test_commands_without_legacy_routes_require_1_2(self):
        steer = "GET /api/v1/runs/r1/steer"
        commands = {
            "task add --repo replay-sample change": "POST /api/v1/tasks",
            "task reply t-1234 answer": "POST /api/v1/tasks/t-1234/comments",
            "task close t-1234": "POST /api/v1/tasks/t-1234/close",
            "status": "GET /api/v1/status?limit=10",
            "record pin r1": "POST /api/v1/runs/r1/pin",
            "record unpin r1": "POST /api/v1/runs/r1/unpin",
            "session send r1 hello": steer,
            "session attach r1": steer,
        }
        self.assertEqual(set(commands), {cmd for cmd, route in self.routes.items()
                                        if route is None})
        event = self.pending_event()
        uploads = []
        requests = []
        missing = {
            "unknown_route": {"error": "No such route.", "code": "unknown_route"},
            "not_found": {"error": "No such route.", "detail": "No such route.", "code": "not_found",
                          "type": "urn:arete:problem:not_found"},
        }

        def response(request):
            headers = {"Arete-API": "1.1"}
            if request.path == "/api/v1/meta":
                return 200, {"instance": {"id": "no-legacy"}, "api": {"version": "1.1"}}, headers
            route = f"{request.command} {request.path}"
            key = request.headers.get("Idempotency-Key")
            if route == "POST /api/v1/hook-events":
                uploads.append(json.load(request.rfile))
            else:
                requests.append((route, key))
            if not request.path.startswith("/api/v1/"):
                self.fail(f"unexpected request {route}")
            return 404, missing.get(shape, {"error": f"Nothing at {route.partition('?')[0]}."}), headers

        with self.stub(response):
            for command, route in commands.items():
                for shape in (*missing, "historical"):
                    with self.subTest(command=command, shape=shape):
                        requests.clear()
                        uploads.clear()
                        code, lines, err = self.cli(command, "hello\n")
                        self.assertEqual((code, len(lines), err), (1, 1, ""), (lines, err))
                        problem = json.loads(lines[0])
                        self.assertIn("requires Arete serve API version 1.2", problem["error"])
                        self.assertEqual((problem["code"], problem["required_version"]), ("unknown_route", "1.2"))
                        key = self.key if route.startswith("POST ") else None
                        self.assertEqual(requests, [(route, key)])
                        self.assertEqual([body["events"] for body in uploads], [[event]] if key else [])
        checkpoint, = (paths.STATE_DIR / "hook-uploads").glob("*.json")
        saved = json.loads(checkpoint.read_text())
        self.assertEqual(saved["offsets"], {})
        self.assertEqual(saved["pending"]["body"]["events"], [event])
        self.assertEqual(json.loads(paths.EVENTS_FILE.read_text()), event)

    def test_supported_server_reports_upload_failure_and_retries_pending_events(self):
        event = self.pending_event()
        uploads = []
        writes = []
        reject_upload = True

        def response(request):
            route = f"{request.command} {request.path}"
            headers = {"Arete-API": "1.3"}
            if route == "GET /api/v1/meta":
                return 200, {"instance": {"id": "upload-supported"}, "api": {"version": "1.3"}}, headers
            writes.append(route)
            if route == "POST /api/v1/hook-events":
                batch = json.load(request.rfile)
                uploads.append((request.headers["Idempotency-Key"], batch))
                if reject_upload:
                    return 404, {"error": "Upload source missing.", "code": "not_found"}, headers
                return 200, {"received": len(batch["events"])}, headers
            if route == "POST /api/v1/tasks/t-1234/close":
                return 200, {}, headers
            self.fail(f"unexpected request {route}")

        with self.stub(response):
            command = "task close t-1234"
            self.assertEqual(self.cli(command), (0, ["t-1234: closed"], "hook event upload: Upload source missing.\n"))
            reject_upload = False
            self.assertEqual(self.cli(command), (0, ["t-1234: closed"], ""))
            self.assertEqual(self.cli(command), (0, ["t-1234: closed"], ""))
        first, retry = uploads
        self.assertEqual(first, retry)
        self.assertEqual(retry[1]["events"], [event])
        hook, close = "POST /api/v1/hook-events", "POST /api/v1/tasks/t-1234/close"
        self.assertEqual(writes, [hook, close, hook, close, close])

    def test_session_start_attach_watches_on_api_1_1(self):
        requests = []
        statuses = iter(("running", "running", "done", "done"))
        launch = {"id": "r1", "task": False, "runner": "pi", "model": "test", "device": "local",
                  "cwd": "/tmp", "branch": "", "tools": []}
        feed_line = {"id": 1, "text": "session output", "ts": "", "kind": "session"}

        def response(request):
            route = f"{request.command} {request.path}"
            requests.append(route)
            headers = {"Arete-API": "1.1"}
            if request.path.startswith("/api/v1/"):
                return 404, {"error": f"Nothing at {route}."}, headers
            if route == "GET /api/meta":
                return 200, {"instance": {"id": "api-1-1"}, "api": {"version": "1.1"}}, headers
            if route == "POST /api/sessions":
                self.assertEqual(json.load(request.rfile)["prompt"], "work")
                self.assertEqual(request.headers.get("Idempotency-Key"), self.key)
                return 200, launch, headers
            if route == "GET /api/runs/r1":
                return 200, {"run": {"kind": "session", "status": next(statuses)}}, headers
            if route in ("GET /api/feed/r1?after=0", "GET /api/feed/r1?after=1"):
                return 200, {"restore_epoch": 0, "lines": [feed_line] if route.endswith("=0") else []}, headers
            self.fail(f"unexpected request {route}")

        with (self.stub(response), mock.patch("select.select", return_value=([], [], [])),
              mock.patch("time.sleep")):
            code, lines, err = self.cli("session start --attach work")
        self.assertEqual((code, err), (0, ""), lines)
        self.assertTrue(any(line.startswith("session r1:") for line in lines), lines)
        self.assertTrue(any(line.endswith("[session] session output") for line in lines), lines)
        info = "GET /api/runs/r1"
        feed = "GET /api/feed/r1"
        self.assertEqual(requests, ["GET /api/v1/meta", "GET /api/meta", "POST /api/sessions", info,
                                    f"{feed}?after=0", info, f"{feed}?after=1", info, f"{feed}?after=1", info])

    def test_session_send_retry_after_serve_rollback(self):
        path = "/api/v1/runs/r1/steer"
        key = "steer-replay"
        sent = []
        current = True

        def response(request):
            if request.path == "/api/v1/meta":
                return 200, {"instance": {"id": "rollback"}}, {}
            if request.path == path:
                if not current:
                    return 404, {"type": "urn:arete:problem:not_found", "code": "not_found",
                                 "error": "No such route.", "detail": "No such route."}, {}
                if request.command == "GET":
                    return 405, {"code": "method_not_allowed"}, {"Allow": "POST"}
                sent.append((request.headers.get("Idempotency-Key"), json.load(request.rfile)))
                return 200, {"seq": "1.9", "id": "r1", "queued": True}, {}
            self.fail(f"unexpected request {request.command} {request.path}")

        with self.stub(response), mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": key}):
            self.assertEqual(self.cli("session send r1 hello"), (0, ['{"id": "r1", "queued": true}'], ""))
            current = False
            self.assertEqual(self.problem("session send r1 hello", "unknown_route")["required_version"], "1.2")
            self.assertEqual(sent, [(key, {"text": "hello"})])


class ClaudeOpenRouterTests(unittest.TestCase):
    model = "deepseek/deepseek-v4.1-flash-1017"
    key = "fixture-openrouter-key"
    anthropic_key = "fixture-anthropic-key"
    anthropic_model = "claude-sonnet-5-5"
    plan_token = "sk-ant-oat-fixture"

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.keys = self.root / "pi.env"
        self.keys.write_text(f"OPENROUTER_API_KEY={self.key}\nANTHROPIC_API_KEY={self.anthropic_key}\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": "ambient-anthropic-key",
            "ANTHROPIC_AUTH_TOKEN": "ambient-anthropic-auth", "CLAUDE_CODE_OAUTH_TOKEN": "ambient-oauth",
            "CLAUDE_CONFIG_DIR": str(self.root / "owner-config"),
        }))
        self.enterContext(mock.patch.multiple(paths, STATE_DIR=self.state, RUNS_DIR=self.state / "runs",
                                              DB_FILE=self.state / "arete.db", PI_KEYS_FILE=self.keys))
        self.enterContext(mock.patch.object(secret_move, "STATE", self.state / "secret-move.json"))
        self.enterContext(mock.patch.object(pi, "owner_dir", return_value=self.root / "pi"))
        self.snapshot = self.enterContext(mock.patch.object(prices, "load", return_value=None))
        self.enterContext(mock.patch.object(routing, "auth_hold", return_value=None))
        self.status = self.enterContext(mock.patch.object(claude, "auth_status", return_value={"loggedIn": True}))
        self.enterContext(mock.patch.object(claude, "authentication", return_value=accounts.Auth("subscription", "claude")))
        self.enterContext(mock.patch.object(record, "mark"))
        self.ledger = self.enterContext(mock.patch.object(record, "steps"))
        self.enterContext(mock.patch.object(runner, "origin", return_value={"repo": "", "commit": ""}))
        self.enterContext(mock.patch.object(runner, "secret_files", return_value=[]))
        self.enterContext(mock.patch.object(runner.run_store, "step_tries", return_value=0))
        self.enterContext(mock.patch.object(runner.devices_store, "clear_holds"))
        self.listing()

    def listing(self, cache_write=None):
        util.write_json(self.state / routing.PRICE_FILE, {
            "fetched": util.iso(),
            "models": {self.model: {"input": 0.15, "output": 0.6, "cache_read": 0.003, "cache_write": cache_write}},
        })

    def settings(self, link=None, instructions=("repo",), routing_toml="", step="plan"):
        link = link or f'{{ runner = "claude", provider = "openrouter", model = "{self.model}" }}'
        path = self.root / "config.toml"
        path.write_text(f'[tiers.test]\nfast = {link}\n[steps.{step}]\ntier = "fast"\n'
                        f'instructions = {json.dumps(instructions)}\n{routing_toml}')
        cfg = config.load(path, strict=True)
        return cfg, steps.resolve(cfg, None, step)

    def call(self, cfg, settings, *, tools=("Read",), **kw):
        return runner.run("read the file", self.repo, **settings.call, cfg=cfg, settings=settings,
                          ask_jev=False, wait_out_limits=False, tools=list(tools), **kw)

    def child(self, *, stderr="", cache_write=0, stream_usage=False):
        self.launches = []

        def stream(cmd, cwd, prompt, env, timeout, on_line):
            self.launches.append((cmd, env))
            session = cmd[cmd.index("--resume") + 1] if "--resume" in cmd else cmd[cmd.index("--session-id") + 1]
            event = {"type": "result", "subtype": "success", "result": "file read", "session_id": session,
                     "num_turns": 2, "total_cost_usd": 5, "usage": {
                         "input_tokens": 100_000, "cache_read_input_tokens": 1_000_000,
                         "cache_creation_input_tokens": cache_write, "output_tokens": 10_000}}
            out = json.dumps(event) + "\n"
            if stream_usage:
                usage = json.dumps({"type": "assistant", "message": {"id": "fixture", "usage": event["usage"]}}) + "\n"
                on_line(usage)
                out = usage + out
            on_line(out)
            return CompletedProcess(cmd, 0, out, stderr)

        self.enterContext(mock.patch.object(claude.child, "stream", side_effect=stream))

    def test_discovery_uses_the_openrouter_key_and_pi_quote(self):
        cfg, settings = self.settings()
        found = routing.Discovery(cfg)
        route, = found.routes(settings)
        self.assertEqual((route.runner, route.model, route.credential, route.billing, route.mode, route.provider, route.state),
                         ("claude", self.model, "cli_key", "paid", "api_key", "openrouter", "ready"))
        self.assertEqual(route.price, routing.Quote(0.15, 0.6, 0.003, source="openrouter"))
        pi_link = f'{{ runner = "pi", model = "openrouter/{self.model}" }}'
        pi_cfg, pi_settings = self.settings(pi_link, instructions=())
        self.assertEqual(route.price, routing.Discovery(pi_cfg).routes(pi_settings)[0].price)
        self.status.assert_not_called()
        self.keys.unlink()
        missing, = routing.Discovery(cfg).routes(settings)
        self.assertEqual(missing.state, "missing_key")

    def test_ambient_claude_plan_token_allows_openrouter_cli_key_lookup(self):
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": self.plan_token}))
        cfg, settings = self.settings()
        _, pi_settings = self.settings(f'{{ runner = "pi", model = "openrouter/{self.model}" }}', instructions=())
        _, anthropic = self.settings(f'{{ runner = "claude", model = "{self.anthropic_model}" }}', instructions=())
        found = routing.Discovery(cfg)
        self.assertEqual(found.routes(pi_settings)[0].state, "refused")
        self.assertEqual(found.routes(settings)[0].state, "ready")
        self.assertEqual(found.routes(pi_settings)[0].state, "refused")
        self.assertEqual([(r.credential, r.state) for r in found.routes(anthropic)], [
            ("cli_login", "ready"), ("cli_key", "refused"), ("pi_key", "refused"),
        ])

        self.keys.unlink()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": self.key}):
            self.assertEqual(routing.Discovery(cfg).routes(settings)[0].state, "ready")
        self.keys.write_text(f"OPENROUTER_API_KEY={self.plan_token}\n")
        self.assertEqual(routing.Discovery(cfg).routes(settings)[0].state, "refused")

    def test_paid_provider_policy_uses_openrouter(self):
        cfg, settings = self.settings(routing_toml='[routing.paid]\nproviders = ["anthropic"]\n')
        route, = routing.Discovery(cfg).routes(settings)
        self.assertEqual(route.state, "policy")
        self.assertIn("[routing.paid]", route.detail)
        self.assertIn("openrouter", route.detail)

    def test_quote_uses_pricing_then_snapshot_then_pi_catalog(self):
        catalog = self.root / "pi" / "models.json"
        util.write_json(catalog, {"providers": {"openrouter": {"models": [{
            "id": self.model, "cost": {"input": 2, "output": 3, "cacheRead": 0.02},
        }]}}})
        cfg, settings = self.settings()
        self.assertEqual(routing.Discovery(cfg).routes(settings)[0].price,
                         routing.Quote(2, 3, 0.02, source="models.json"))
        self.snapshot.return_value = prices.parse({
            "schema": 1, "source": "fixture", "observed": "2025-01-01T00:00:00Z", "routes": [{
                "route": f"pi:openrouter/{self.model}", "kind": "marketplace", "provider": "openrouter",
                "model": self.model, "currency": "USD", "unit": "per_million_tokens",
                "rates": [{"input": 4, "output": 5, "cache_read": 0.04}],
            }],
        })
        price = routing.Discovery(cfg).routes(settings)[0].price
        self.assertEqual((price.input, price.output, price.cache_read, price.source), (4, 5, 0.04, "snapshot"))
        cfg, settings = self.settings(routing_toml=f'[pricing.models."openrouter/{self.model}"]\ninput=6\noutput=7\n')
        self.assertEqual(routing.Discovery(cfg).routes(settings)[0].price, routing.Quote(6, 7, source="pricing"))

    def test_instruction_scopes_allow_repo_only(self):
        cfg, settings = self.settings(instructions=("user", "repo"))
        route, = routing.Discovery(cfg).routes(settings)
        self.assertEqual(route.state, "incompatible")
        self.assertIn("runs without your own claude setup", route.detail)
        cfg, settings = self.settings(routing_toml="[routing]\nfallback = false\n")
        route, = routing.Discovery(cfg).routes(settings)
        self.assertEqual(route.state, "ready")
        remote = devices.Device("remote", "ssh")
        self.assertEqual(routing.Discovery(cfg, device=remote).routes(settings), [])
        request = routing.request_for(cfg, None, settings.runner, settings.model, settings, True, {})
        self.assertIn("runs locally", routing.fit_refusal(request, remote))
        self.assertIn("payment_modes", routing.fit_refusal(request, devices.LOCAL, ("subscription",)))

    def test_oncall_repair_admission_uses_the_resolved_step_routes(self):
        cfg, _ = self.settings()
        self.keys.write_text(f"OPENROUTER_API_KEY={self.key}\n")
        os.environ["ANTHROPIC_API_KEY"] = ""
        self.enterContext(mock.patch.object(routing, "auth_hold", side_effect=lambda auth, *args, **kw:
                                         {"why": "Claude subscription held", "until": ""}
                                         if auth.mode == "subscription" else None))
        repo = config.Repo("fixture", self.repo, layer=self.root / "layer")
        repo.layer_dir.mkdir()
        layer_file = repo.layer_dir / "config.toml"
        repo_only = ('[steps.build]\ntier = "fast"\ninstructions = ["repo"]\n'
                     '[steps.review]\ntier = "fast"\ninstructions = ["repo"]\n')
        layer_file.write_text(repo_only)
        layer = config.load_layer(repo.layer_dir, strict=True)
        setting = steps.resolve(cfg, layer, "review_repair", "fix")
        route, = routing.Discovery(cfg, repo, layer=layer).routes(setting)
        self.assertEqual((route.provider, route.state), ("openrouter", "ready"))
        self.assertEqual(oncall.route_refusal(cfg, repo), ("", False))

        layer_file.write_text(repo_only + '[steps.review_repair]\ninstructions = ["user", "repo"]\n')
        why, lasting = oncall.route_refusal(cfg, repo)
        self.assertIn("review_repair: instructions user cannot load", why)
        self.assertTrue(lasting)

    def test_turn_environment_uses_only_the_openrouter_auth(self):
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": self.plan_token}))
        cfg, settings = self.settings()
        self.child()
        res = self.call(cfg, settings, trace=("fixture-run", "plan"), extra_env={
            "CLAUDE_CODE_OAUTH_TOKEN": "injected-oauth", "ANTHROPIC_CUSTOM_HEADERS": "Authorization: fixture",
            "CLAUDE_CODE_USE_BEDROCK": "1", "OPENROUTER_API_KEY": "injected-key",
        })
        self.assertTrue(res.ok, res.error)
        (cmd, env), = self.launches
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://openrouter.ai/api")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], self.key)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        self.assertEqual(env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"], "1")
        for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            self.assertEqual(env[name], self.model)
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_USE_BEDROCK",
                     "OPENROUTER_API_KEY"):
            self.assertNotIn(name, env)
        self.assertNotIn(self.plan_token, env.values())
        folder = Path(env["CLAUDE_CONFIG_DIR"])
        self.assertEqual(folder.parent, paths.RUNS_DIR / "fixture-run")
        self.assertTrue(folder.is_dir())
        self.assertNotEqual(folder, Path.home() / ".claude")
        self.assertFalse((folder / ".credentials.json").exists())
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project,local")

    def test_openrouter_pins_provider_routing_at_launch(self):
        cfg, settings = self.settings()
        self.child()
        other_providers = dict.fromkeys((
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
            "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_GATEWAY",
        ), "1")
        res = self.call(cfg, settings, extra_env={
            **other_providers, "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "0",
            "ANTHROPIC_BASE_URL": "https://redirect.invalid/api",
        })
        self.assertTrue(res.ok, res.error)
        (cmd, env), = self.launches
        self.assertEqual(env.get("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"), "1")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://openrouter.ai/api")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], self.key)
        self.assertFalse(other_providers.keys() & env.keys())
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project,local")

    def test_custom_claude_profile_stays_protected_on_openrouter(self):
        profile = self.root / "owner's Claude profile"
        profile.mkdir()
        login = profile / ".credentials.json"
        login.write_text('{"claudeAiOauth": {"accessToken": "fictional-login"}}')
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(profile)}))
        self.child()
        native = f'{{ runner = "claude", model = "{self.anthropic_model}" }}'
        isolated = paths.RUNS_DIR / "fixture-run" / "claude-openrouter"
        for link, config_dir in ((native, profile), (None, isolated)):
            with self.subTest(config_dir=config_dir.name):
                cfg, settings = self.settings(link, step="build", routing_toml="[jev]\nguard = false\n")
                res = self.call(cfg, settings, trace=("fixture-run", "build"), tools=("Bash",),
                                allowed_tools=["Bash(cat *)"])
                self.assertTrue(res.ok, res.error)
                cmd, env = self.launches[-1]
                self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(config_dir))
                hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"]
                hook, = (hook for group in hooks if "Bash" in group["matcher"].split("|") for hook in group["hooks"])
                payload = {"cwd": str(self.repo), "tool_name": "Bash",
                           "tool_input": {"command": shlex.join(["cat", str(login)])}}
                decision = util.sh(shlex.split(hook["command"]), cwd=self.repo, env=env, inherit=False,
                                   input=json.dumps(payload))
                self.assertEqual(decision.returncode, 0, decision.stderr)
                reply = json.loads(decision.stdout or "{}")
                self.assertEqual(reply.get("hookSpecificOutput", {}).get("permissionDecision"), "deny", decision.stdout)

    def test_success_with_model_warning_is_recorded_at_the_quote(self):
        cfg, settings = self.settings()
        self.child(stderr=f'[claude-code:unrecognized_model] {{"model":"{self.model}","query_source":"sdk"}}\n')
        res = self.call(cfg, settings, trace=("fixture-run", "plan"))
        self.assertTrue(res.ok, res.error)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(res.cost_usd, 0.024)
        self.assertEqual(res.paid_usd, 0.024)
        self.assertEqual(res.session.spent_usd, 0.024)
        (charged, _), = self.ledger.call_args.args[2]
        self.assertEqual(charged.cost_usd, 0.024)
        path = paths.record_dir("fixture-run") / res.record
        rows = list(record.lines(path))
        self.assertEqual(rows[0]["provider"], "openrouter")
        self.assertEqual(rows[-1]["provider"], "openrouter")
        self.assertTrue(rows[-1]["ok"])
        self.assertEqual(rows[-1]["cost_usd"], 0.024)
        usage, = (event for event in record.events(path) if event["kind"] == "usage")
        self.assertEqual(usage["cost_usd"], 0.024)
        self.assertNotIn(self.key, json.dumps(rows))

    def test_dollar_limit_uses_the_quote_for_streamed_usage(self):
        cfg, settings = self.settings()
        self.child(stream_usage=True)
        res = self.call(cfg, settings, max_budget_usd=0.020, hard_cap=True)
        self.assertFalse(res.ok)
        self.assertIn("stopped at the $0.0200 left", res.error)
        self.assertEqual(res.cost_usd, 0.024)
        (cmd, _), = self.launches
        self.assertNotIn("--max-budget-usd", cmd)

    def test_resume_keeps_the_config_and_rejects_other_routes(self):
        self.listing(cache_write=0.3)
        cfg, settings = self.settings()
        self.child(cache_write=10_000)
        first = self.call(cfg, settings, trace=("first-run", "plan"))
        self.assertTrue(first.ok, first.error)
        self.assertEqual(first.cost_usd, 0.027)
        saved = factory.outcome_of({"session": asdict(first.session)}).session
        again = self.call(cfg, settings, trace=("next-run", "plan"), resume=saved, resume_only=True)
        self.assertTrue(again.ok, again.error)
        self.assertEqual(again.session.id, saved.id)
        self.assertEqual(again.session.spent_usd, 0.054)
        (first_cmd, first_env), (again_cmd, again_env) = self.launches
        self.assertNotIn("--resume", first_cmd)
        self.assertEqual(again_cmd[again_cmd.index("--resume") + 1], saved.id)
        self.assertEqual(again_env["CLAUDE_CONFIG_DIR"], first_env["CLAUDE_CONFIG_DIR"])
        for link in ('{ runner = "claude", provider = "openrouter", model = "other/model" }',
                     f'{{ runner = "claude", model = "{self.anthropic_model}" }}'):
            with self.subTest(link=link):
                different_cfg, different = self.settings(link)
                res = self.call(different_cfg, different, resume=saved, resume_only=True)
                self.assertFalse(res.ok)
                self.assertIn("kept session cannot continue", res.error)
        self.assertEqual(len(self.launches), 2)

    def test_anthropic_routes_and_api_key_are_preserved(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        cfg, settings = self.settings(f'{{ runner = "claude", model = "{self.anthropic_model}" }}',
                                      instructions=("user", "repo"))
        routes = routing.Discovery(cfg).routes(settings)
        self.assertEqual([(r.runner, r.credential, r.provider, r.state) for r in routes], [
            ("claude", "cli_login", "anthropic", "ready"), ("claude", "cli_key", "anthropic", "ready"),
            ("pi", "pi_key", "anthropic", "incompatible"),
        ])
        self.status.return_value = {}
        self.child()
        res = self.call(cfg, settings)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.credential, "cli_key")
        (_, env), = self.launches
        self.assertEqual(env["ANTHROPIC_API_KEY"], self.anthropic_key)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", env)

    def test_openrouter_runs_as_a_fallback_when_native_routes_are_unavailable(self):
        link = (f'[{{ runner = "claude", model = "{self.anthropic_model}" }}, '
                f'{{ runner = "pi", model = "openrouter/{self.model}" }}, '
                f'{{ runner = "claude", provider = "openrouter", model = "{self.model}" }}]')
        cfg, settings = self.settings(link, step="verify")
        self.keys.write_text(f"OPENROUTER_API_KEY={self.key}\n")
        os.environ["ANTHROPIC_API_KEY"] = ""
        self.status.return_value = {}
        self.child()
        res = self.call(cfg, settings)
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.runner, res.model, res.provider), ("claude", self.model, "openrouter"))
        self.assertEqual(len(self.launches), 1)
