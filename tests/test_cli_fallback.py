import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.client import HTTPResponse
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import cli, client, codebase, config, paths
from tests.test_behavior import _CliThroughServeCase


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
    def stub(self, response):
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

        with (mock.patch.dict(os.environ, {client.ENV: "http://serve.example.test"}),
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
        meta = "GET /api/meta"
        info = "GET /api/runs/r1"
        feed = "GET /api/feed/r1?after=0"
        loop = "GET /api/loops/example"
        ask = "GET /api/asks"
        events_route = "GET /api/runs/r1/events?from=0"
        follow_events = events_route + "&follow=1"
        session_start = "POST /api/sessions"
        task_details = ["GET /api/tasks/t-1234", "GET /api/tasks/t-5678"]
        report = b'{"event":"report","seq":1,"status":"done"}\n'
        replies = {
            meta: {"instance": {"id": "fallback"}}, loop: {"running": "r1"},
            ask: {"asks": [{"run": "r1", "id": "1", "questions": []}]},
            feed: {"restore_epoch": 0, "lines": []},
            events_route: report, follow_events: report,
            "POST /api/tasks": {"run": "r1", "task": "t-1234", "state": "running"},
            "GET /api/tasks": {"tasks": [summary, parent_summary]},
            **{route: {**row, "comments": [], "runs": [], "children": []}
               for route, row in zip(task_details, (task, parent))},
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
        values = dict(task="t-1234", run="r1", session="r1", id=1, step="build", name="example", chat=1)
        seen = []
        old = historical = False

        def response(request):
            path = request.path
            route = f"{request.command} {path}"
            body = request.rfile.read(int(request.headers.get("Content-Length", "0")))
            seen.append((route, request.headers.get("Idempotency-Key"), body))
            if old and path.startswith("/api/v1/"):
                if historical:
                    return 404, {"error": "Nothing at GET /api/v1/tasks."}, {}
                return 404, {"code": "unknown_route", "error": "No such route."}, {}
            if route == info:
                return 200, {"run": {"kind": "session", "status": "done", "version": 7}}, {}
            if route in replies:
                return 200, replies[route], {"Arete-Seq": seq} if route == "POST /api/runs/r1/stop" else {}
            return 500, {"error": f"unexpected request {route}"}, {}

        with self.stub(response):
            for command, template in self.routes.items():
                if template is None:
                    continue
                with self.subTest(command=command):
                    legacy = template.format(**values)
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
                    self.assertEqual(current[0], 0, current)
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

    def test_api_1_0_missing_v1_routes(self):
        seen = []
        stop = "/api/v1/runs/r1/stop"
        legacy_stop = "/api/runs/r1/stop"
        stopped = {"id": "r1", "stopped": True, "pid": 123}
        status, failure = 404, None

        def response(request):
            path = request.path
            if request.command == "GET" and path == "/api/meta":
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
            if request.path == "/api/meta":
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
            for status, reason, headers in ((404, "not_found", {}), (405, "method_not_allowed", {}),
                                            (409, "unknown_route", {}),
                                            (422, "unknown_route", {}), (503, "unknown_route", {}),
                                            (503, "draining", {"Retry-After": "0"})):
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
            if request.path == "/api/meta":
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
        self.assertEqual(seen, [("GET", "/api/meta", None), ("GET", "/api/runs/r1", None),
                                ("POST", stop, self.key), ("GET", "/api/meta", None), ("POST", stop, self.key)])
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
            if request.path == "/api/meta":
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
            if route == "GET /api/meta":
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
        self.assertEqual(requests, ["GET /api/meta", "POST /api/sessions", info,
                                    f"{feed}?after=0", info, f"{feed}?after=1", info, f"{feed}?after=1", info])

    def test_session_send_retry_after_serve_rollback(self):
        path = "/api/v1/runs/r1/steer"
        key = "steer-replay"
        sent = []
        current = True

        def response(request):
            if request.path == "/api/meta":
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
