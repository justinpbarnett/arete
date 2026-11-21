import argparse
import ast
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from http.client import HTTPConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from socket import SHUT_WR, create_connection
from types import SimpleNamespace
from unittest import mock

from arete import (
    accounts, api_check, api_http, chat, config, contract, control, dashboard, events, gh, idempotency, jira, pipeline, principals,
    screening, serve, tailnet, tasks, triage, util,
)
from arete.store import approvals as approvals_store
from arete.store import chats as chats_store
from arete.store import db
from arete.store import idempotency as claims
from arete.store import triage as triage_store
from tests import support, versioned_api_fixture


def setUpModule():
    support.isolate_live_state()


class CliThroughServeTests(unittest.TestCase):
    class_timeout = 30
    OWNER = "cli-owner@example.test"
    ONLINE = (
        "run replay-sample 7", "task add --repo replay-sample change", "task list", "task show t-1234",
        "task reply t-1234 answer", "task close t-1234", "task start --repo replay-sample change",
        "task run replay-sample 7", "answer r1", "resume r1", "repair r1", "step r1 build", "wait r1",
        "lines r1", "status", "stop r1", "events", "approve 1", "forget 1", "file-rule 1", "reject-rule 1",
        "loop start example", "loop enable example", "loop disable example", "loop stop example",
        "session start work", "session attach r1", "session send r1 hello", "session stop r1",
        "oncall off", "oncall on", "oncall cancel 1", "oncall dismiss 1", "oncall retry 1",
    )
    ONLINE_VARIANTS = (
        "status --json r1", "status --json", "record pin r1", "record unpin r1", "approve 1 --reject",
        "loop start example -f", "session --chat 1 start work", "session --chat 1 list",
        "session --chat 1 report r1", "session --chat 1 stop r1",
    )
    READS = (
        "ab", "api diff --check-artifact", "backup --check", "benchmark report missing", "costs", "devices", "doctor",
        "experiment summary missing", "jev report", "jira-check", "loop feed r1", "loop list", "loop status poll",
        "merged", "oncall list", "oncall show 1", "prices diff", "prices recompute r1 build", "prompts",
        "hold", "record r1", "research", "secrets replay-sample", "session list", "session report r1",
        "steps --repo replay-sample", "triage", "verifiers report missing",
        "webhook secret missing", "writers report r1", "zte status",
    )
    READ_VARIANTS = ("approve", "mcp-ask --check",
                     "status --offline", "status --json --offline r1", "lines --offline r1",
                     "task show --offline --json {task}")
    LOCAL = {
        "api check": "api_check", "ask": "jev_ask", "audit": "audit", "background": "background",
        "benchmark fixture": "lab.benchmark", "benchmark run": "lab.benchmark", "benchmark sample": "lab.benchmark",
        "codex-guard": "codex", "dashboard": "dashboard", "devices probe": "devices",
        "document": "document", "eval": "evals", "experiment optimize": "lab.optimize",
        "experiment promote": "lab.optimize", "experiment run": "lab.experiment", "experiment sample": "lab.experiment",
        "guard": "guard", "heartbeat": "heartbeat", "hook": "tool_events", "init": "commands.setup",
        "install": "install", "jev compare": "lab.calibrate", "jev sample": "lab.calibrate",
        "loop exec": "loop_run", "loop new": "scaffold", "loop requeue": "store.loops", "loop try": "scaffold",
        "mcp-ask": "owner_asks", "merge-check": "mergecheck", "migrate": "store.db", "mine": "miner",
        "oncall scan": "oncall", "pilot-doc-fix": "pilot", "poll": "poll", "prices import": "prices",
        "replay sample": "replay", "retrieval run": "lab.retrieval", "retrieval sample": "lab.retrieval",
        "rotate-authority": "scratch", "serve": "serve", "session exec": "sessions", "uninstall": "install",
        "upload-guard": "uploads",
        "verifiers run": "lab.verifier_suites", "verifiers sample": "lab.verifier_suites",
        "writers export": "lab.writer_eval", "writers rate": "lab.writer_eval", "writers run": "lab.writer_eval",
        "writers sample": "lab.writer_eval", "zte pause": "zte",
    }
    LOCAL_VARIANTS = {
        "run replay-sample 7 --dry-run": "pipeline.run_issue / record.start",
        "run replay-sample 7 --local": "pipeline.run_issue", "file-rule 1 --local": "miner.file_rule",
        "loop new --run-id r1 work": "scaffold.draft", "loop start example --events sample.json": "loop_run.launch",
        "session start --task work": "sessions.launch", "session start --task --draft-pr work": "sessions.launch",
        "research --next": "research.next_due", "research replay-sample#7 --run": "research.run_now",
        "triage --refresh": "triage.run", "hold claude 1h": "store.devices",
        "hold claude --release": "store.devices", "costs --correct r1 1 --why correction": "record.correct",
        "costs --backfill": "store.runs", "backup": "backup.snapshot", "backup --restore backup.db": "backup.restore",
        "secrets rm replay-sample secret": "vault", "api diff": "contract_evolution",
    }

    def in_subprocess(self, case):
        with tempfile.TemporaryDirectory(prefix="arete-cli-") as temp:
            root = Path(temp).resolve()
            entry = ("import faulthandler; faulthandler.dump_traceback_later(15); "
                     "from tests.test_behavior import CliThroughServeTests; "
                     "from unittest import TextTestRunner; "
                     "sys.exit(not TextTestRunner().run(CliThroughServeTests(sys.argv[1])).wasSuccessful())")
            command = api_check.child_command(Path(__file__).resolve().parents[1], entry, case)
            try:
                result = subprocess.run(command, cwd=root, env=api_check.isolated_env(root), capture_output=True,
                                        text=True, timeout=18, check=False)
            except subprocess.TimeoutExpired as error:
                output = (error.stderr or b"").decode(errors="replace")
                self.fail(f"{case} timed out after {error.timeout:g}s\n{output}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def setUp(self):
        if not self._testMethodName.startswith("check_"):
            return
        from arete import paths

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("urllib.request.urlopen", "arete.gh.gh"):
            self.stack.enter_context(mock.patch(name, side_effect=AssertionError(name)))
        lines, _ = api_check.prepare_replay(Path.cwd())
        self.fixture_config = "\n".join(lines) + "\n"
        paths.CONFIG_FILE.write_text(self.fixture_config)

    def cli(self, command, stdin=""):
        from arete import cli

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", StringIO(stdin)):
            code = cli.main(command.split())
        return code, out.getvalue().splitlines(), err.getvalue()

    def problem(self, command, reason):
        code, lines, err = self.cli(command)
        self.assertEqual(code, 1, (command, lines, err))
        self.assertEqual(len(lines), 1, (command, lines, err))
        self.assertNotIn("Traceback", err)
        answer = json.loads(lines[0])
        self.assertEqual(answer.get("reason") or answer.get("code"), reason, (command, answer))
        return answer

    @contextmanager
    def listening(self, server):
        with server, mock.patch.dict(os.environ, {"ARETE_SERVER": f"http://127.0.0.1:{server.server_port}"}):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                yield
            finally:
                server.shutdown()
                thread.join()

    @contextmanager
    def serve_at(self, requests, names=()):
        db.migrate()
        principals.seed_owner(self.OWNER)

        class Handler(dashboard.Handler):
            def send_response(handler, code, message=None):
                requests.append((handler.command, handler.path, code, handler.headers.get("Idempotency-Key")))
                super().send_response(code, message)

            def log_request(self, *_):
                pass

        address = "127.0.0.1"
        node = tailnet.Node((address,), frozenset({address, *names}), address)
        logins = tailnet.Logins(mock.Mock())
        with mock.patch.object(logins, "of", return_value=self.OWNER), mock.patch("socket.getfqdn", return_value=address):
            server = tailnet.Server(address, 0, node, logins, False, Handler, dashboard.Redirect)
            with self.listening(server):
                yield server

    @contextmanager
    def stub(self, response):
        class Handler(BaseHTTPRequestHandler):
            def reply(handler):
                status, answer, headers = response(handler)
                raw = json.dumps(answer).encode()
                handler.send_response(status)
                for name, value in headers.items():
                    handler.send_header(name, value)
                handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw)

            do_GET = do_POST = reply

            def log_message(self, *_):
                pass

        with self.listening(ThreadingHTTPServer(("127.0.0.1", 0), Handler)):
            yield

    def test_parser_groups_and_cli_calls(self):
        self.in_subprocess("check_parser_groups_and_cli_calls")

    def check_parser_groups_and_cli_calls(self):
        from arete import cli
        from arete.commands import common

        def leaves(parser, prefix=()):
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    if prefix and not action.required:
                        yield prefix
                    for name, sub in action.choices.items():
                        yield from leaves(sub, (*prefix, name))
                    return
                if action.dest == "action" and action.choices:
                    yield from ((*prefix, name) for name in action.choices)
                    return
            yield prefix

        parser = cli.parser()
        actual = set(leaves(parser))
        groups = {}
        for group, case in ((1, self.ONLINE), (2, self.READS), (3, self.LOCAL)):
            for command in case:
                words = tuple(command.split())
                match = next((words[:n] for n in (2, 1) if words[:n] in actual), words)
                self.assertNotIn(match, groups)
                groups[match] = group
        self.assertSetEqual(actual, set(groups))
        for command in (*self.ONLINE, *self.ONLINE_VARIANTS, *self.READS, *self.READ_VARIANTS, *self.LOCAL_VARIANTS):
            parser.parse_args(command.split())
        forbidden = {"record": {"pin", "unpin"}, "control": {"decide", "switch", "stop_run"},
                     "sessions": {"send"}, "chat": {"start_session", "stop_session"}, "oncall": {"switch"}}
        files = [Path(cli.__file__), *Path(common.__file__).parent.glob("*.py")]
        for path in files:
            for call in (node for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Call)):
                function = call.func
                if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
                    module, name = function.value.id, function.attr
                    self.assertNotIn(name, forbidden.get(module, ()), f"{path.name}:{call.lineno}")
                    if module == "state":
                        self.assertIn(("connect", "state.reader"),
                                      [(kw.arg, ast.unparse(kw.value)) for kw in call.keywords])
                elif isinstance(function, ast.Name) and function.id == "getattr" and call.args:
                    self.assertNotEqual(ast.unparse(call.args[0]), "oncall")

    def test_commands_fail_when_serve_is_down(self):
        self.in_subprocess("check_commands_fail_when_serve_is_down")

    def check_commands_fail_when_serve_is_down(self):
        from socket import AF_INET, SOCK_STREAM, socket

        from arete import paths

        for name in ("pipeline.run_issue", "delivery.send", "miner.file_rule", "loop_run.launch", "sessions.launch",
                     "control.stop_run", "record.pin", "oncall.dismiss", "chat.start_session", "chat.stop_session"):
            self.stack.enter_context(mock.patch("arete." + name, side_effect=AssertionError(name)))
        with socket(AF_INET, SOCK_STREAM) as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        with self.assertRaises(ConnectionRefusedError), create_connection(("127.0.0.1", port), timeout=1):
            pass
        url = f"http://127.0.0.1:{port}"
        paths.CONFIG_FILE.write_text(self.fixture_config + f'\n[server]\nurl = "{url}"\n')
        for command in (*self.ONLINE, *self.ONLINE_VARIANTS):
            with self.subTest(command=command):
                self.assertIn(url, self.problem(command, "server_down")["error"])
                self.assertFalse(paths.DB_FILE.exists())
        paths.CONFIG_FILE.write_text(self.fixture_config)
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=3)
        util.write_json(paths.SERVER_FILE, {**paths.state_and_config(), "pid": exited.pid,
                                           "addresses": ["127.0.0.1"], "port": port})
        self.assertIn("process has ended", self.problem("run replay-sample 7", "server_down")["error"])
        with mock.patch.dict(os.environ, {"ARETE_SERVER": url}):
            self.problem("run replay-sample 7", "server_down")
            self.problem("loop start example", "server_down")
        self.assertFalse(paths.DB_FILE.exists())

    def test_readers_and_worker_guards(self):
        self.in_subprocess("check_readers_and_worker_guards")

    def check_readers_and_worker_guards(self):
        from arete import record, run_lines
        from arete.store import tasks as task_store

        db.migrate()
        task = task_store.task_add("replay-sample", "work")
        run = record.start("session", run_id="r1", pid=None)
        record.settle(run, "done", end_line="done")
        run_lines.write(run, run_lines.END, status="done")
        calls = []
        real_connect = db.connect_db

        def readonly(*args, **kwargs):
            if not kwargs.get("readonly"):
                calls.append((args, kwargs))
                raise AssertionError("CLI opened the database for writing")
            return real_connect(*args, **kwargs)

        with mock.patch.object(db, "connect_db", readonly):
            for command in (*self.READS, *self.READ_VARIANTS):
                with self.subTest(command=command):
                    self.cli(command.format(task=task))
                    self.assertFalse(calls)
            for command in ("run replay-sample 7 --local", "file-rule 1 --local", "loop new --run-id r1 work",
                            "loop exec --run-id r1", "session exec r1"):
                code, _, error = self.cli(command)
                self.assertEqual(code, 1)
                self.assertIn("started by Arete", error)
            for command in ("lines --offline r1", "status --json --offline r1", f"task show --offline --json {task}"):
                code, lines, _ = self.cli(command)
                self.assertEqual(code, 0)
                self.assertTrue(lines)
                self.assertTrue(all(json.loads(line)["source"] == "local" for line in lines))

    def test_identity_change_blocks_writes(self):
        self.in_subprocess("check_identity_change_blocks_writes")

    def check_identity_change_blocks_writes(self):
        from arete import paths

        paths.CONFIG_FILE.write_text(self.fixture_config + '\n[server]\nurl = "http://127.0.0.1:1"\n')
        sent = []
        instances = iter(("first", "second"))

        def response(request):
            sent.append(request.command)
            body = ({"instance": {"id": next(instances)}} if request.command == "GET" else
                    {"task": {"id": "t-1234"}})
            return 200, body, {}

        with self.stub(response):
            self.assertEqual(self.cli("task add --repo replay-sample first")[0], 0)
            self.problem("task add --repo replay-sample second", "server_identity_changed")
        self.assertEqual(sent, ["GET", "POST", "GET"])

    def test_approval_waits_out_drain(self):
        self.in_subprocess("check_approval_waits_out_drain")

    def check_approval_waits_out_drain(self):
        from arete import client, delivery

        seen, refused = [], []
        with self.serve_at(seen), mock.patch.object(delivery.EMITS["push"], "execute", return_value="sent") as send:
            number = approvals_store.add_approval("example", "", {"type": "push", "item": {}}, "deliver")
            connect = client.connect

            def reconnect(where, *args):
                if not refused and any(status == 503 for _, _, status, _ in seen):
                    refused.append(True)
                    raise client.Down(client.address(where), "connection refused")
                return connect(where, *args)

            dashboard.POSTS_IN_FLIGHT.close()
            timer = threading.Timer(1, dashboard.POSTS_IN_FLIGHT.reopen)
            timer.start()
            try:
                with mock.patch.object(client, "connect", reconnect), mock.patch.object(client, "RETRY_SECONDS", .01):
                    code, lines, err = self.cli(f"approve {number}")
                self.assertEqual(code, 0, (lines, err))
            finally:
                timer.cancel()
                timer.join()
                dashboard.POSTS_IN_FLIGHT.reopen()
            send.assert_called_once()
            self.assertEqual(approvals_store.approval(number)["status"], "done")
        self.assertEqual(refused, [True])
        writes = [(code, key) for method, _, code, key in seen if method == "POST"]
        self.assertEqual([code for code, _ in writes], [503, 200])
        self.assertIsNotNone(writes[0][1])
        self.assertEqual(writes[0][1], writes[1][1])

    def test_file_rule_detaches_a_marked_worker_once(self):
        self.in_subprocess("check_file_rule_detaches_a_marked_worker_once")

    def check_file_rule_detaches_a_marked_worker_once(self):
        from arete import miner

        seen, workers = [], []
        real_detach = control.detach

        def detach(args):
            workers.append(args)
            return real_detach(args)

        def spawn(argv, *, env, **_):
            self.assertEqual(env.get(control.WORKER_ENV), "file-rule")
            with mock.patch.dict(os.environ, env):
                thread = threading.Thread(target=lambda: self.cli(" ".join(argv[-3:])))
                thread.start()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            return SimpleNamespace(pid=1, wait=lambda: 0)

        child = SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT)
        with self.serve_at(seen), mock.patch.object(control, "detach", side_effect=detach), \
                mock.patch.object(control, "subprocess", child), \
                mock.patch.object(miner, "file_rule", return_value="filed") as file_rule:
            approvals_store.add_proposal("", {"repo": "replay-sample"}, "approve")
            number = approvals_store.proposals()[0]["id"]
            code, lines, err = self.cli(f"file-rule {number}")
            self.assertEqual(code, 0, (lines, err))
            file_rule.assert_called_once_with(number)
        self.assertEqual(workers, [["file-rule", str(number), "--local"]])
        self.assertEqual([path for method, path, _, _ in seen if method == "POST"], [f"/api/proposals/{number}/approve"])

    def test_version_and_missing_field_errors_are_flat(self):
        self.in_subprocess("check_version_and_missing_field_errors_are_flat")

    def check_version_and_missing_field_errors_are_flat(self):
        versions = []

        def old(request):
            versions.append(request.headers.get("X-Arete-Client"))
            return 400, {"reason": "client_too_old", "required_version": "1.99"}, {}

        with self.stub(old):
            for command in ("wait r1", "answer r1", "status --json r1"):
                self.assertIn("1.99", self.problem(command, "client_too_old")["error"])
        self.assertEqual(len(versions), 3)
        for version in versions:
            self.assertRegex(version, r"^cli/1\.\d+$")

        with self.stub(lambda request: (200, {"instance": {"id": "first"}} if request.path == "/api/meta" else {}, {})):
            self.assertIn("asks", self.problem("answer r1", None)["error"])

    def test_task_updates_and_stop_replay(self):
        self.in_subprocess("check_task_updates_and_stop_replay")

    def check_task_updates_and_stop_replay(self):
        from arete import client, record
        from arete.store import runs, tasks as task_store

        seen = []
        with self.serve_at(seen):
            code, lines, err = self.cli("task add --repo replay-sample change")
            self.assertEqual(code, 0, (lines, err))
            task = lines[0]
            self.assertEqual(task_store.task_get(task)["state"], "todo")
            self.assertFalse(runs.runs())
            self.assertEqual(json.loads(self.cli("task list")[1][0])["id"], task)
            before = task_store.task_get(task)["version"]
            with client.invocation():
                path = f"/api/v1/tasks/{task}"
                changed = client.call("PATCH", path, {"base_version": before, "title": "Edited"})["task"]
                self.assertEqual(changed["title"], "Edited")
                with self.assertRaises(client.Failed) as stale:
                    client.call("PATCH", path, {"base_version": before, "title": "Stale"})
                self.assertEqual(stale.exception.code, 409)
            self.assertEqual(self.cli(f"task reply {task} reply")[0], 0)
            self.assertEqual([(c["author"], c["body"]) for c in task_store.task_comments(task)], [("owner", "reply")])
            self.assertEqual(self.cli(f"task close {task}")[0], 0)
            self.problem(f"task close {task}", "invalid_state")
            run = record.start("session", run_id="r1", pid=None)
            with mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "stop-replay"}):
                first = self.cli(f"stop {run}")
                self.assertEqual(first[0], 0, first)
                self.assertEqual(self.cli(f"stop {run}")[:2], first[:2])
            self.assertEqual(runs.run_get(run)["status"], "stopped")

    def test_run_retries_keep_the_original_target_and_version(self):
        self.in_subprocess("check_run_retries_keep_the_original_target_and_version")

    def check_run_retries_keep_the_original_target_and_version(self):
        from arete import record
        from arete.store import runs

        seen = []
        with self.serve_at(seen):
            record.start("loop", run_id="r1", loop="example", pid=None)
            record.start("loop", run_id="r2", loop="example", pid=None)
            with mock.patch.object(control, "loop_detail", side_effect=[{"running": "r1"}, {"running": "r2"}]) as detail:
                with mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "loop-stop"}):
                    first = self.cli("loop stop example")
                    self.assertEqual(first[0], 0, first)
                    self.assertEqual(self.cli("loop stop example")[:2], first[:2])
                self.assertEqual(detail.call_count, 1)
            self.assertEqual(runs.run_get("r1")["status"], "stopped")
            self.assertEqual(runs.run_get("r2")["status"], "running")
            for action, command, args in (("resume", "resume r2", ("r2", "")),
                                          ("repair", "repair r2", ("r2",)),
                                          ("run_step", "step r2 build", ("r2", "build", {}))):
                version = runs.run_get("r2")["version"]
                with (mock.patch.object(tasks, action, return_value={"run": "r2"}) as handler,
                      mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": f"run-{action}"})):
                    first = self.cli(command)
                    self.assertEqual(first[0], 0, first)
                    runs.note("r2", summary=action)
                    self.assertGreater(runs.run_get("r2")["version"], version)
                    self.assertEqual(self.cli(command)[:2], first[:2])
                    handler.assert_called_once_with(*args, version)

        reads, writes = [], []

        def respond(request):
            if request.path == "/api/meta":
                return 200, {"instance": {"id": "ask-replay"}}, {}
            if request.command == "GET":
                reads.append(request.path)
                asks = [{"id": "ask1", "run": "r1"}] if not writes else []
                return 200, {"asks": asks}, {}
            writes.append((request.path, request.headers["Idempotency-Key"]))
            return 200, {"answered": True}, {}

        with self.stub(respond), mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "ask-replay"}):
            command = 'answer r1 --json {"answers":[{"text":"yes"}]}'
            first = self.cli(command)
            self.assertEqual(first[0], 0, first)
            self.assertEqual(self.cli(command)[:2], first[:2])
        self.assertEqual(reads, ["/api/asks"])
        self.assertEqual(writes, [("/api/asks/ask1/answer", "ask-replay:1")] * 2)

    def test_interrupted_launch_does_not_repeat(self):
        self.in_subprocess("check_interrupted_launch_does_not_repeat")

    def check_interrupted_launch_does_not_repeat(self):
        from arete import record, sessions
        from arete.store import runs

        def launched(*args, **kwargs):
            record.start("session", pid=None)
            raise RuntimeError("interrupted after launch")

        with (self.serve_at([]), mock.patch.object(sessions, "launch", side_effect=launched) as launch,
              mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "session-start"})):
            self.problem("session start work", 500)
            reply = self.problem("session start work", 409)
            self.assertIn("earlier request", reply["error"])
            launch.assert_called_once()
            self.assertEqual(len(runs.runs(kind="session")), 1)

    def test_deferred_session_attach_can_send_input(self):
        self.in_subprocess("check_deferred_session_attach_can_send_input")

    def check_deferred_session_attach_can_send_input(self):
        from arete import client, sessions
        from arete.commands import sessions as commands

        def follow(run, *, send, remote):
            self.assertFalse(remote)
            send()

        launched = {"id": "r1", "runner": "pi", "model": "sample", "device": "local", "cwd": str(Path.cwd()),
                    "branch": "", "tools": [], "task": True}
        with (mock.patch.object(client, "server", side_effect=AssertionError("contacted serve")),
              mock.patch.object(sessions, "launch", return_value=launched) as launch,
              mock.patch.object(sessions, "send") as send, mock.patch.object(commands, "follow", side_effect=follow),
              mock.patch("select.select", return_value=([True], [], []))):
            for options in ("", "--draft-pr --repo replay-sample"):
                code, lines, err = self.cli(f"session start --task --attach {options} work", "hello\n")
                self.assertEqual(code, 0, (lines, err))
                self.assertTrue(launch.call_args.kwargs["task"])
                self.assertEqual(launch.call_args.kwargs["draft_pr"], bool(options))
                send.assert_called_once_with("r1", "hello\n")
                send.reset_mock()

    def test_session_attach_uses_serve_for_feed_and_input(self):
        self.in_subprocess("check_session_attach_uses_serve_for_feed_and_input")

    def check_session_attach_uses_serve_for_feed_and_input(self):
        from arete import record, sessions
        from arete.store import runs

        send = sessions.send

        def finish_after_message(session, text):
            queued = send(session, text)
            record.settle(session, "done", end_line="done")
            return queued

        seen = []
        with (self.serve_at(seen), mock.patch.object(sessions, "send", side_effect=finish_after_message) as steer,
              mock.patch("select.select", return_value=([True], [], []))):
            record.start("session", run_id="r1", pid=None)
            runs.add_feed("r1", "session", "ready for input")
            code, lines, error = self.cli("session attach r1", "hello\n")
            self.assertEqual(code, 0, (lines, error))
            self.assertTrue(any("ready for input" in line for line in lines), lines)
            steer.assert_called_once_with("r1", "hello\n")
            self.assertEqual(runs.run_get("r1")["status"], "done")
        routes = [(method, path) for method, path, status, _ in seen if status == 200]
        self.assertIn(("GET", "/api/feed/r1?after=0"), routes)
        self.assertIn(("GET", "/api/runs/r1"), routes)
        self.assertIn(("POST", "/api/v1/runs/r1/steer"), routes)

    def check_pin_commands(self):
        from arete import paths

        with self.serve_at([]):
            paths.record_dir("r1").mkdir(parents=True)
            for command, code in (("pin r1", 0), ("unpin r1", 0), ("unpin r1", 1), ("pin missing", 1), ("pin", 2)):
                self.assertEqual(self.cli(f"record {command}")[0], code, command)

    def test_legacy_fallback_keeps_the_key(self):
        self.in_subprocess("check_legacy_fallback_keeps_the_key")

    def check_legacy_fallback_keeps_the_key(self):
        writes = []
        result = (404, "unknown_route")

        def response(request):
            if request.path == "/api/meta":
                return 200, {"instance": {"id": "legacy"}}, {}
            writes.append((request.path, request.headers.get("Idempotency-Key")))
            if request.path.startswith("/api/v1/"):
                status, reason = result
                return status, {"error": reason, "reason": reason}, {}
            return 200, {"id": "r1", "queued": True}, {}

        with self.stub(response), mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "fallback-test"}):
            self.assertEqual(self.cli("session send r1 message")[0], 0)
            self.assertEqual(writes, [("/api/v1/runs/r1/steer", "fallback-test:1"),
                                      ("/api/sessions/r1/send", "fallback-test:1")])
            for result in ((404, "not_found"), (409, "conflict")):
                writes.clear()
                self.problem("session send r1 message", result[1])
                self.assertEqual(writes, [("/api/v1/runs/r1/steer", "fallback-test:1")])
            result = (404, "unknown_route")
            writes.clear()
            self.problem("task list", "unknown_route")
            self.assertEqual(writes, [("/api/v1/tasks", None)])

    def test_hook_batches_replay_from_the_saved_offset(self):
        self.in_subprocess("check_hook_batches_replay_from_the_saved_offset")

    def check_hook_batches_replay_from_the_saved_offset(self):
        from arete import paths, tool_events

        host = "receiver.example.test"
        root = Path.cwd() / "sender"
        root.mkdir()
        env = api_check.isolated_env(root)
        local = root / "state/events.jsonl"
        corrupt_db = local.with_name("arete.db")
        corrupt_db.write_bytes(b"corrupt local database")
        rows = [{"ts": "2020-01-01T00:00:00Z", "index": index, "detail": "🍊" * 400} for index in range(20)]
        local.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        other = {"ts": rows[0]["ts"], "detail": "already archived"}
        archive = paths.EVENTS_FILE.with_name("events-202001.jsonl")
        archive.write_text(json.dumps(other) + "\n")
        seen = []
        sent = []
        interrupted = []
        lost = []
        finish = tool_events.finish_ingest
        respond = dashboard.Handler.send_json

        def partial_append():
            _, checkpoint = tool_events.ingest_checkpoint()
            if not interrupted and (pending := checkpoint.get("pending")):
                data = pending["data"].encode()
                paths.EVENTS_FILE.write_bytes(data[:data.index(b"\n") + 11])
                interrupted.append(True)
                raise OSError("interrupted during append")
            return finish()

        def respond_or_disconnect(handler, code, data, *args, **kwargs):
            if handler.command == "POST" and handler.path == "/api/v1/hook-events":
                sent.append((handler.headers["Idempotency-Key"], handler.body()))
                if code == 200 and not lost:
                    lost.append(True)
                    handler.connection.shutdown(SHUT_WR)
                    return
            return respond(handler, code, data, *args, **kwargs)

        def cli_child(command, payload=None):
            entry = ("import socket; from unittest.mock import patch; from arete.cli import main; "
                     "resolve = socket.getaddrinfo; "
                     "local = lambda host, *a, **kw: resolve('127.0.0.1', *a, **kw) "
                     "if host == 'receiver.example.test' else resolve(host, *a, **kw); "
                     "blocked = AssertionError('unexpected external call'); "
                     "patch('socket.getaddrinfo', local).start(); "
                     "patch('urllib.request.urlopen', side_effect=blocked).start(); "
                     "patch('arete.gh.gh', side_effect=blocked).start(); sys.exit(main())")
            args = api_check.child_command(versioned_api_fixture.SOURCE, entry, *command.split())
            return subprocess.run(args, cwd=root, env=env, input=payload, capture_output=True, text=True, timeout=5)

        with self.serve_at(seen, names=(host,)) as server:
            url = f"http://{host}:{server.server_port}"
            (root / "config/config.toml").write_text(f'[server]\nurl = "{url}"\n')
            with mock.patch.object(tool_events, "finish_ingest", side_effect=partial_append), \
                    mock.patch.object(dashboard.Handler, "send_json", respond_or_disconnect):
                first = cli_child("events --raw --hours 1000000")
                self.assertEqual(first.returncode, 1, first)
                self.assertEqual(json.loads(first.stdout)["code"], "internal_error")
                self.assertEqual(interrupted, [True])
                tool_events.rotate_events(365)
                self.assertTrue(archive.with_name("events-202001-0001.jsonl").is_file())
                payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                                      "tool_input": {"skill": "after-restart"}})
                hooked = cli_child("hook", payload)
                self.assertEqual(hooked.returncode, 0, hooked)
                self.assertEqual(len(sent), 1)
                added = json.loads(local.read_text().splitlines()[-1])
                self.assertEqual(added["detail"], "after-restart")
                dropped = cli_child("events --raw --hours 1000000")
                self.assertEqual(dropped.returncode, 1, dropped)
                self.assertEqual(lost, [True])
                replay = cli_child("events --raw --hours 1000000")
                self.assertEqual(replay.returncode, 0, replay)
                expected = [other, *rows, added]
                self.assertEqual([json.loads(line) for line in replay.stdout.splitlines()], expected)
                self.assertEqual(sent[0], sent[1])
                self.assertEqual(sent[1], sent[2])
                self.assertGreater(len(sent), 3)
                count = len(sent)
                created = cli_child("task add --repo replay-sample remote")
                self.assertEqual(created.returncode, 0, created)
                status = cli_child("status")
                self.assertEqual(status.returncode, 0, status)
                self.assertIn(f"serve at {url}", status.stdout)
                self.assertEqual(len(sent), count)
        raw = [json.loads(line) for path in tool_events.event_files() for line in path.read_text().splitlines()]
        self.assertEqual(raw, expected)
        self.assertEqual(corrupt_db.read_bytes(), b"corrupt local database")


class ApiCheckTests(unittest.TestCase):
    serial = True
    class_timeout = 20  # Hosted macOS reached streaming but exhausted the unit-test budget.

    def test_api_check(self):
        with tempfile.TemporaryDirectory(prefix="arete-api-cli-") as temp:
            root = Path(temp).resolve()
            command = api_check.child_command(versioned_api_fixture.SOURCE, "from arete.cli import main; sys.exit(main())",
                                              "api", "check")
            result = subprocess.run(command, cwd=root, env=api_check.isolated_env(root),
                                    timeout=api_check.CHECK_TIMEOUT + 5, check=False)
            self.assertEqual(result.returncode, 0, "the API smoke command failed; see its scenario output above")


class ApiCompatibilityTests(unittest.TestCase):
    serial = True

    def test_api_diff(self):
        versioned_api_fixture.evolution_temp()


class VersionedApiTests(unittest.TestCase):
    def setUp(self):
        self.actor = principals.seed_owner("fixture@example.test")
        self.repo = config.Repo("fixture", Path("."), github="fixture/repo", tracker="local")
        self.cfg = config.Config(repos=[self.repo])
        self.handler = SimpleNamespace(body=mock.Mock(), headers={"Idempotency-Key": self.id()},
                                       send=lambda *args: args, log_error=mock.Mock(), write_receipt=None,
                                       idempotent_replayed=False, keyed_delivery=False)

    def request(self, body):
        self.handler.body.return_value = body
        with principals.acting(self.actor), mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                mock.patch.object(tasks, "begin", side_effect=lambda cfg, repo, tid, **kw:
                                  tasks.answer(tid, repo.name, "", "queued", "capacity")):
            return dashboard.Handler.keyed_start(self.handler)

    def test_keyed_replay(self):
        body = {"repo": "fixture", "prompt": "Add kumquat"}
        first = self.request(body)
        self.assertEqual(first[0], 200)
        replayed = self.request({"prompt": "Add kumquat", "repo": "fixture"})
        self.assertEqual(replayed, first)
        self.assertTrue(self.handler.idempotent_replayed)
        with self.assertRaises(idempotency.Reused):
            self.request({**body, "prompt": "Changed"})
        with db.reader() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        saved = claims.lookup(self.actor, self.id())
        self.assertEqual(bytes(saved["response"]), first[1])
        self.assertEqual(claims.target(saved)[0], "fixture")
        self.handler.headers["Idempotency-Key"] = "handled-error"
        refused = self.request({"repo": "unknown", "prompt": "Add kumquat"})
        self.assertEqual(refused[0], 400)
        self.assertEqual(self.request({"repo": "unknown", "prompt": "Add kumquat"}), refused)
        with self.assertRaises(idempotency.Reused):
            self.request(body)

    def test_keyed_concurrency(self):
        sha = idempotency.fingerprint("POST", "/api/tasks", {}, {"repo": "fixture", "prompt": "Add kumquat"})
        with idempotency.owning(self.actor, "in-flight-key", sha):
            with self.assertRaises(idempotency.InFlight), idempotency.owning(self.actor, "in-flight-key", sha):
                self.fail("duplicate admitted")
            with self.assertRaises(idempotency.Reused), idempotency.owning(self.actor, "in-flight-key", "changed"):
                self.fail("changed request admitted")
            with idempotency.owning("another-actor", "in-flight-key", sha):
                pass
        with idempotency.owning(self.actor, "in-flight-key", sha):
            pass
        self.assertEqual(idempotency.ACTIVE, {})
        for key in ("", "short", "a" * 129, "bad key-572", "nonascii-é", "newline\n"):
            with self.subTest(key=key), self.assertRaises(control.Refused), idempotency.owning(self.actor, key, sha):
                self.fail("invalid key admitted")

    def test_versioned_read_surface(self):
        self.with_server(self.check_read_surface)

    def test_transport_and_publication_compatibility(self):
        self.with_server(self.check_transport)

    def test_restart_drains_without_discarding_an_admitted_publication(self):
        self.with_server(self.check_publication_drain)

    def with_server(self, exercise):
        address = "127.0.0.1"
        node = SimpleNamespace(names={address})
        logins = SimpleNamespace(of=lambda _: "fixture@example.test")
        with mock.patch("socket.getfqdn", return_value=address):
            server = tailnet.Server(address, 0, node, logins, False, dashboard.Handler, dashboard.Redirect)
        with server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                exercise(server.server_port)
            finally:
                server.shutdown()
                thread.join()

    def test_publication_recovers_remote_urls(self):
        self.cfg.arete_repo = self.repo.name
        source = triage.GitHubSource(self.repo.github, [self.repo])
        ticket = triage.Candidate("fixture#7", "7", "Question", gh.issue_url(self.repo.github, 7), "old")
        triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                         questions=["What happened?"]))
        chat_id = chats_store.chat_add(self.repo.name)
        chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Draft", draft_body="Chat issue body")
        approval_emit = {"type": "ticket", "item": {"repo": self.repo.name, "title": "Delivery",
                                                   "body": "Approval issue body", "labels": []}}
        approval_id = approvals_store.add_approval("fixture", "", approval_emit, "File the delivery")
        pr_item = {"repo": self.repo.github, "branch": "fixture/head", "base": "main", "workdir": ".",
                   "title": "Delivery PR", "body": "Approval PR body", "labels": [], "draft": True}
        pr_id = approvals_store.add_approval("fixture", "", {"type": "pr", "item": pr_item}, "File the PR")
        remote = {"comments": [], "issues": [], "pulls": []}
        searches = []
        labeled = []
        pushed = []
        comment_url = f"{ticket.url}#issuecomment-1"
        pr_url = f"https://github.com/{self.repo.github}/pull/42"

        def outside_tx():
            self.assertIsNone(getattr(db._OWNER, "active", None))

        def lookup(args):
            outside_tx()
            self.assertEqual(args[2:], ["--paginate", "--slurp"])
            endpoint, _, query = args[1].partition("?")
            kind = endpoint.split("/")[-1]
            prefix = f"repos/{self.repo.github}/issues/7" if kind == "comments" else f"repos/{self.repo.github}"
            self.assertEqual(endpoint, f"{prefix}/{kind}")
            if kind == "pulls":
                self.assertRegex(query, r"head=fixture%3Afixture%2F(?:head|retry)")
            searches.append(kind)
            return [remote[kind].copy()]

        def comment(repo, number, body, *, return_url, single_attempt):
            outside_tx()
            self.assertEqual((repo, number, return_url, single_attempt), (self.repo.github, 7, True, True))
            remote["comments"].append({"body": body, "html_url": comment_url})
            return comment_url

        def issue(repo, title, body, labels=(), *, single_attempt=False):
            outside_tx()
            self.assertEqual(repo, self.repo.github)
            self.assertEqual(single_attempt, title != "Legacy draft")
            number = len(remote["issues"]) + 20
            remote["issues"].append({"body": body, "html_url": gh.issue_url(repo, number)})
            return number

        def pull(repo, head, base, title, body, labels, *, draft, single_attempt):
            outside_tx()
            self.assertEqual((repo, base, draft, single_attempt), (self.repo.github, "main", True, True))
            self.assertIn(head, ("fixture/head", "fixture/retry"))
            url = pr_url if head == "fixture/head" else f"https://github.com/{repo}/pull/43"
            remote["pulls"].append({"body": body, "html_url": url})
            return url

        def label(row):
            outside_tx()
            labeled.append(row["ref"])

        def push(args, *unused, **kw):
            outside_tx()
            pushed.append(args)

        settle = approvals_store.settle_approval

        def settle_until_disconnect(ident, status, *args, **kwargs):
            if status != "failed":
                return settle(ident, status, *args, **kwargs)
            return False

        forbidden = AssertionError("credential lookup or external transport")
        with ExitStack() as patches:
            for patch in (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")),
              mock.patch.object(triage, "sources", return_value=[source]),
              mock.patch.object(source, "approve", side_effect=label),
              mock.patch.object(screening, "screen", return_value=None),
              mock.patch.object(screening, "held", return_value=False),
              mock.patch.object(chat.routing, "metered", return_value=False),
              mock.patch.object(dashboard, "start_poll_without_failing_the_send", return_value="Poll queued."),
              mock.patch.object(dashboard.api_http, "log_request"),
              mock.patch.object(accounts, "read", side_effect=forbidden),
              mock.patch.object(jira.Client, "from_env", side_effect=forbidden),
              mock.patch("urllib.request.urlopen", side_effect=forbidden),
              mock.patch.object(gh, "gh", side_effect=forbidden),
              mock.patch.object(gh, "gh_json", side_effect=lookup),
              mock.patch.object(gh, "issue", return_value={"labels": []}),
              mock.patch.object(gh, "issue_comment", side_effect=comment),
              mock.patch.object(gh, "issue_create", side_effect=issue),
              mock.patch.object(gh, "pr_create", side_effect=pull),
              mock.patch.object(util, "git", side_effect=push)):
                patches.enter_context(patch)
            read = patches.enter_context(mock.patch.object(source, "ticket",
                                                          return_value=triage.TicketText("", "Ticket", "", "old")))
            post = self.publication_post
            cases = [
                ("triage", "/api/triage/fixture%237/send", "/api/triage/{ref}/send", {"ref": "fixture%237"},
                 {"questions": ["What happened?"], "answers": ["It broke."], "repo": self.repo.name},
                 "comments", comment_url, "triage:fixture#7"),
                ("chat", f"/api/chats/{chat_id}/file", "/api/chats/{n}/file", {"n": str(chat_id)},
                 {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}, "issues",
                 gh.issue_url(self.repo.github, 20), f"chat:{chat_id}"),
                ("approval", f"/api/approvals/{approval_id}", "/api/approvals/{id}", {"id": str(approval_id)},
                 {"approve": True}, "issues", gh.issue_url(self.repo.github, 21), f"approval:{approval_id}"),
                ("approval-pr", f"/api/approvals/{pr_id}", "/api/approvals/{id}", {"id": str(pr_id)},
                 {"approve": True}, "pulls", pr_url, f"approval:{pr_id}"),
            ]
            for name, path, route, params, body, kind, url, subject in cases:
                with self.subTest(publication=name):
                    key = f"fixture-key-{name}"
                    before = len(remote[kind])
                    with (mock.patch.object(idempotency.Publication, "record", side_effect=RuntimeError("cut off")),
                          mock.patch.object(approvals_store, "settle_approval", side_effect=settle_until_disconnect)):
                        self.assertEqual(post(path, body, key)[0], 500)
                    self.assertEqual(len(remote[kind]), before + 1)
                    sha = idempotency.fingerprint("POST", route, params, body)
                    tag = db.schema_metadata()["instance_tag"]
                    self.assertIn(f"arete:{tag}:k-{sha[:8]}", remote[kind][-1]["body"].splitlines())
                    self.assertEqual(claims.lookup(self.actor, key)["status"], 0)
                    if name.startswith("approval"):
                        self.assertEqual(approvals_store.approval(int(params["id"]))["status"], "approved")
                    status, headers, answer = post(path, body, key)
                    self.assertEqual(status, 200, answer)
                    saved = claims.lookup(self.actor, key)
                    self.assertEqual(headers["Arete-Seq"], claims.receipt(saved["last_seq"])["event_seq"])
                    with db.reader() as conn:
                        event = conn.execute("SELECT subject, data FROM events_log WHERE seq=?",
                                             (saved["last_seq"],)).fetchone()
                    self.assertEqual((event["subject"], json.loads(event["data"])["url"]), (subject, url))
                    status, headers, again = post(path, body, key)
                    self.assertEqual((status, headers["Idempotent-Replayed"], again), (200, "true", answer))
                    self.assertEqual(len(remote[kind]), before + 1)

            self.assertEqual(post(f"/api/approvals/{approval_id}", {"approve": False}, "fixture-key-approval")[0], 422)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(labeled, [ticket.ref, ticket.ref])
            self.assertEqual(pushed, [["push", "-u", "origin", "fixture/head"]])
            self.assertEqual(searches, ["comments", "comments", "issues", "issues", "issues", "issues", "pulls", "pulls"])
            self.assertIsNone(triage.board_row(ticket.ref))
            self.assertEqual(triage_store.triage_approval(ticket.ref)["repo"], self.repo.name)
            self.assertEqual([m["text"] for m in chats_store.chat_messages(chat_id)],
                             ["Drafted", f"Filed {gh.issue_url(self.repo.github, 20)}"])
            self.assertEqual(approvals_store.approval(approval_id)["result"], gh.issue_url(self.repo.github, 21))
            self.assertEqual(approvals_store.approval(pr_id)["result"], pr_url)
            with self.subTest(publication="triage drift before first confirmed publication"):
                remote["comments"].clear()
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                                 questions=["What happened?"]))
                path = "/api/triage/fixture%237/send"
                payload = {"questions": ["What happened?"], "answers": ["A different answer."], "repo": self.repo.name}
                key = "fixture-triage-stale"
                with mock.patch.object(gh, "issue_comment", side_effect=gh.GhError("could not reach fixture")):
                    self.assertEqual(post(path, payload, key)[0], 502)
                self.assertEqual(claims.lookup(self.actor, key)["status"], 0)
                read.return_value = triage.TicketText("", "Edited", "", "new")
                labels_before = labeled.copy()
                self.assertEqual(post(path, payload, key)[0], 409)
                self.assertEqual(remote["comments"], [])
                self.assertEqual(labeled, labels_before)
                self.assertIsNotNone(triage.board_row(ticket.ref))
            with self.subTest(publication="label-only triage send recovers after its own timestamp change"):
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=1, code=1, repo=self.repo.name))
                path, key = "/api/triage/fixture%237/send", "fixture-triage-label-recovery"
                payload = {"questions": [], "answers": [], "repo": self.repo.name}
                read.return_value = triage.TicketText("", "Ticket", "", "old")
                with mock.patch.object(idempotency.Publication, "record", side_effect=RuntimeError("cut off")):
                    self.assertEqual(post(path, payload, key)[0], 500)
                wanted = triage.labels_to_add(triage.board_row(ticket.ref))
                read.return_value = triage.TicketText("", "Ticket", "", "after-labels", tuple(wanted))
                labels_before = labeled.copy()
                read.return_value.labels += ("agent:hold",)
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 409, answer)
                self.assertIsNotNone(triage.board_row(ticket.ref))
                read.return_value.labels = tuple(wanted)
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 200, answer)
                self.assertEqual(labeled, labels_before)
                self.assertIsNone(triage.board_row(ticket.ref))
            with self.subTest(publication="found comment cannot resume on a newer triage entry"):
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                                 questions=["What happened?"]))
                read.return_value = triage.TicketText("", "Ticket", "", "old")
                payload = {"questions": ["What happened?"], "answers": ["Preserve the new row."], "repo": self.repo.name}
                path, key = "/api/triage/fixture%237/send", "fixture-triage-revised-board"
                with mock.patch.object(source, "approve", side_effect=gh.GhError("cannot add labels")):
                    self.assertEqual(post(path, payload, key)[0], 502)
                triage_store.triage_put(triage.row(source, ticket, updated="new", ready=0, code=1, repo=self.repo.name,
                                                 questions=["New question"]))
                labels_before = labeled.copy()
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 409, answer)
                self.assertEqual(labeled, labels_before)
                self.assertEqual(triage.board_row(ticket.ref)["questions"], ["New question"])
            with self.subTest(publication="upstream tracker failure is kept and redacted"):
                ident = approvals_store.add_approval("fixture", "", approval_emit, "File again")
                marker = "synthetic provider diagnostic should stay private"
                with mock.patch.object(gh, "gh_json", side_effect=gh.GhError(marker)):
                    status, _, failure = post(f"/api/approvals/{ident}", {"approve": True}, "fixture-approval-upstream")
                self.assertNotIn(marker.encode(), failure)
                self.assertEqual(status, 502)
                self.assertIs(json.loads(failure)["kept"], True)
                saved = approvals_store.approval(ident)
                self.assertEqual(saved["status"], "failed")
                self.assertNotIn(marker, saved["result"])
            with self.subTest(publication="identical chat content under a new key"):
                chats_store.chat_say(chat_id, "arete", "Drafted again", draft_title="Draft", draft_body="Chat issue body")
                before = len(remote["issues"])
                path = f"/api/chats/{chat_id}/file"
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                status, _, answer = post(path, body, "fixture-distinct-chat-key")
                self.assertEqual(status, 200, answer)
                self.assertEqual(len(remote["issues"]), before + 1)
                self.assertNotEqual(remote["issues"][-1]["body"], remote["issues"][0]["body"])
            with self.subTest(publication="chat reply lost before the marked issue is visible"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Delayed draft", draft_body="Delayed body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-lost-chat-reply"
                before = len(remote["issues"])
                hidden = []

                def lost_reply(*args, **kwargs):
                    issue(*args, **kwargs)
                    hidden.append(remote["issues"].pop())
                    raise gh.GhUnsure("synthetic lost reply")

                with mock.patch.object(gh, "issue_create", side_effect=lost_reply) as outbound:
                    self.assertEqual(post(path, body, key)[0], 502)
                    retry_status, _, retry = post(path, body, key)
                    self.assertIn(retry_status, (409, 502), retry)
                    self.assertEqual(outbound.call_count, 1)
                self.assertEqual(len(hidden), 1)
                self.assertEqual(chat.chat_row(chat_id)["draft_title"], "Delayed draft")
                remote["issues"].extend(hidden)
                self.assertEqual(post(path, body, key)[0], 200)
                self.assertEqual(len(remote["issues"]), before + 1)
            with self.subTest(publication="approval prerequisites fail before the create attempt"):
                retry_pr = {"type": "pr", "item": {**pr_item, "branch": "fixture/retry"}}
                for item, kind, adapter, prerequisite in ((approval_emit, "issues", gh, "ensure_label"),
                                                          (retry_pr, "pulls", util, "git")):
                    item = {**item, "item": {**item["item"], "labels": ["fixture-label"]}}
                    ident = approvals_store.add_approval("fixture", "", item, "Recover the prerequisite failure")
                    path, key = f"/api/approvals/{ident}", f"fixture-prerequisite-{ident}"
                    before = len(remote[kind])
                    with mock.patch.object(gh, "ensure_label", return_value=""), \
                            mock.patch.object(adapter, prerequisite, side_effect=gh.GhError("prerequisite failed")):
                        status, _, failure = post(path, {"approve": True}, key)
                        self.assertEqual(status, 502, failure)
                    self.assertEqual(approvals_store.approval(ident)["status"], "failed")
                    self.assertEqual(len(remote[kind]), before)
                    with mock.patch.object(gh, "ensure_label", return_value=""):
                        status, _, answer = post(path, {"approve": True}, key)
                    self.assertEqual(status, 200, answer)
                    self.assertEqual(len(remote[kind]), before + 1)
                    self.assertEqual(approvals_store.approval(ident)["status"], "done")
            with self.subTest(publication="lost approval reply cannot authorize another send under a fresh key"):
                ident = approvals_store.add_approval("fixture", "", approval_emit, "Send once")
                path, key = f"/api/approvals/{ident}", "fixture-approval-lost-reply"
                hidden = []

                def lost_issue(*args, **kwargs):
                    issue(*args, **kwargs)
                    hidden.append(remote["issues"].pop())
                    raise gh.GhUnsure("reply not received")

                with mock.patch.object(gh, "issue_create", side_effect=lost_issue):
                    self.assertEqual(post(path, {"approve": True}, key)[0], 502)
                self.assertEqual(approvals_store.approval(ident)["status"], "failed")
                before = len(remote["issues"])
                self.assertEqual(post(path, {"approve": True}, "fixture-fresh-approval-key")[0], 409)
                self.assertEqual(self.publication_post(path, {"approve": True}, None)[0], 409)
                self.assertEqual(len(remote["issues"]), before)
                remote["issues"].extend(hidden)
                self.assertEqual(post(path, {"approve": True}, key)[0], 200)
                self.assertEqual(len(remote["issues"]), before + 1)
            with self.subTest(publication="discarded chat draft cannot be sent after an initial lookup failure"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Discarded", draft_body="Old body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-chat-discarded"
                with mock.patch.object(gh, "find_issue_marker", side_effect=gh.GhError("lookup not available")):
                    self.assertEqual(post(path, body, key)[0], 502)
                chat.drop_draft(chat_id)
                before = len(remote["issues"])
                self.assertEqual(post(path, body, key)[0], 409)
                self.assertEqual(len(remote["issues"]), before)
            with self.subTest(publication="chat discard wins while prior publication is being looked up"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Will discard", draft_body="Pending body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-chat-discard-at-lookup"
                before = len(remote["issues"])

                def discard(*unused):
                    chat.drop_draft(chat_id)
                    return ""

                with mock.patch.object(gh, "find_issue_marker", side_effect=discard):
                    self.assertEqual(post(path, body, key)[0], 409)
                saved = claims.lookup(self.actor, key)
                self.assertFalse(json.loads(saved["response"]).get("send_started"))
                self.assertEqual(len(remote["issues"]), before)
            with self.subTest(publication="chat without a bound draft"):
                chats_store.chat_say(chat_id, "arete", "Drafted again", draft_title="New draft", draft_body="Different body")
                path = f"/api/chats/{chat_id}/file"
                before = len(remote["issues"])
                status, _, _ = post(path, {}, "fixture-chat-without-draft")
                self.assertIn(status, (409, 422))
                self.assertEqual(len(remote["issues"]), before)
                self.assertEqual(chat.chat_row(chat_id)["draft_title"], "New draft")
                self.assertIsNone(claims.lookup(self.actor, "fixture-chat-without-draft"))
            with self.subTest(publication="legacy unkeyed filing without representation headers"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Legacy draft", draft_body="Legacy body")
                before = len(remote["issues"])
                status, _, answer = self.publication_post(f"/api/chats/{chat_id}/file", None, None)
                self.assertEqual(status, 200, answer)
                self.assertEqual(len(remote["issues"]), before + 1)
                self.assertEqual(remote["issues"][-1]["body"], "Legacy body")

    def test_keyed_tracker_adapters_send_at_most_once(self):
        marker = "arete:fixture:k-12345678.234567890abcdef1234567890abcdef1"
        with mock.patch.object(gh, "gh", side_effect=gh.GhUnsure("lost reply")) as send, \
                mock.patch.object(gh, "recovery_lookup", side_effect=AssertionError("internal retry")):
            writes = (
                lambda: gh.issue_create(self.repo.github, "Fixture", marker, single_attempt=True),
                lambda: gh.issue_comment(self.repo.github, 7, marker, return_url=True, single_attempt=True),
                lambda: gh.pr_create(self.repo.github, "fixture", "main", "Fixture", marker, single_attempt=True),
            )
            for write in writes:
                send.reset_mock()
                with self.assertRaises(gh.GhUnsure):
                    write()
                send.assert_called_once()
                self.assertIn(marker, send.call_args.kwargs["input"].splitlines())

        client = jira.Client("https://jira.example.test", "fixture@example.test", "not-a-credential")
        with mock.patch.object(jira._opener, "open", side_effect=TimeoutError("lost reply")) as send:
            with self.assertRaises(jira.JiraUnsure):
                client.comment("TEST-7", f"Approved answers\n\n{marker}", return_url=True, single_attempt=True)
            send.assert_called_once()
            request = send.call_args.args[0]
            self.assertEqual((request.method, request.selector), ("POST", "/rest/api/3/issue/TEST-7/comment"))
            self.assertIn(marker, json.dumps(json.loads(request.data)["body"]))
        pages = [BytesIO(b'{"total":0,"comments":[]}'), BytesIO(json.dumps({"total": 1, "comments": [
            {"id": "42", "body": f"Approved answers\n\n{marker}"}]}).encode())]
        with mock.patch.object(jira._opener, "open", side_effect=pages) as observe:
            self.assertEqual(client.find_comment_marker("TEST-7", marker), "")
            self.assertEqual(client.find_comment_marker("TEST-7", marker),
                             "https://jira.example.test/browse/TEST-7?focusedCommentId=42")
            self.assertEqual([call.args[0].method for call in observe.call_args_list], ["GET", "GET"])

    def publication_post(self, path, body, key):
        payload = json.dumps(body).encode() if body is not None else b""
        headers = f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Arete: 1\r\n"
        if payload:
            headers += f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        if key:
            headers += f"Idempotency-Key: {key}\r\n"
        incoming = BytesIO((headers + "\r\n").encode() + payload)
        outgoing = bytearray()
        connection = SimpleNamespace(makefile=lambda *args: incoming, sendall=outgoing.extend)
        server = SimpleNamespace(node=SimpleNamespace(names={"127.0.0.1"}),
                                 logins=SimpleNamespace(of=lambda _: "fixture@example.test"))
        dashboard.Handler(connection, ("127.0.0.1", 0), server)
        response = HTTPResponse(SimpleNamespace(makefile=lambda *args: BytesIO(outgoing)))
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def check_read_surface(self, port):
        def request(path, *, body=None, headers=None, status=200):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                request_headers = {"X-Arete": "1", "Accept": "application/json"}
                if body is not None:
                    request_headers["Content-Type"] = "application/json"
                connection.request("GET" if body is None else "POST", path,
                                   body=None if body is None else json.dumps(body),
                                   headers={**request_headers, **(headers or {})})
                response = connection.getresponse()
                data = json.loads(response.read())
                self.assertEqual(response.status, status, (path, data))
                self.assertEqual(response.getheader("Arete-API"), contract.VERSION)
                if body is None and status == 200 and "seq" in data:
                    self.assertEqual(data["seq"], response.getheader("Arete-Seq"))
                return data, response
            finally:
                connection.close()

        registry, _ = request("/api/v1/contract")
        operations = registry["operations"]
        self.assertEqual(len(operations), 46)
        self.assertEqual(len({op["name"] for op in operations}), 46)
        for op in operations:
            with self.subTest(op=op["name"]):
                contract.validate(op["example"]["input"], op["input"])
                contract.validate(op["example"]["result"], op["result"])
        for problem in registry["errors"]:
            contract.validate(problem["example"], problem["schema"])
        meta, alias = request("/api/meta")
        self.assertEqual(meta, request("/api/v1/meta")[0])
        self.assertEqual(meta["api"]["contract_sha"], registry["contract_sha"])
        self.assertEqual(alias.getheader("Deprecation"), "true")
        with db.tx():
            events.emit("fixture.changed", "fixture:573", cls="derived", data={})
        stream, response = request("/api/events?since=0&timeout=0")
        self.assertEqual(response.getheader("Deprecation"), "true")
        self.assertEqual(response.getheader("Arete-Seq"), stream["next"])
        self.assertEqual(stream["events"][-1]["id"], stream["next"])
        request("/api/v1/missing", status=404)
        request("/api/sidebar", headers={"X-Arete-Client": "mcp/0.9"}, status=400)
        with mock.patch.object(tasks, "start", side_effect=AssertionError("unknown field reached task start")), \
                mock.patch.object(dashboard.api_http, "record_alias", wraps=dashboard.api_http.record_alias) as record:
            refused, _ = request("/api/tasks", body={"colour": "not an input"}, status=422)
            record.assert_not_called()
        self.assertEqual(refused["fields"], ["colour"])
        with mock.patch.object(db, "tx", side_effect=AssertionError("GET opened a write transaction")), \
                mock.patch.object(chat, "recover", side_effect=AssertionError("GET recovered chat files")):
            sidebar, _ = request("/api/sidebar")
            live, _ = request("/api/live")
            request("/api/chats")
            self.assertEqual(sidebar["seq"], live["seq"])
            self.assertEqual(sidebar["running"], len(live["live"]["rows"]["runs"]))
            batch, response = request("/api/v1/batch", body={"requests": [
                {"operation": "screen.get"}, {"operation": "task.list"}, {"operation": "meta.get"}]})
            contract.validate(batch, contract.BY_NAME["batch.run"]["result"])
            self.assertEqual({row["seq"] for row in batch["results"]}, {response.getheader("Arete-Seq")})
        def grace():
            with db.reader() as c:
                return c.execute("SELECT value FROM schema_meta WHERE key = 'versioned_api_grace_deadline'").fetchone()[0]

        def page():
            with self.subTest("first versioned screen"):
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", "/ui.html")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"<!doctype html>", response.read().lower())
                finally:
                    connection.close()

        self.assertEqual(grace(), "")
        page()
        deadline = grace()
        self.assertTrue(deadline)
        with mock.patch.object(db, "tx", side_effect=AssertionError("repeat page opened a write transaction")):
            page()
        self.assertEqual(grace(), deadline)

    def check_transport(self, port):
        def exchange(method, path, headers=(), body=b""):
            raw = [f"{method} {path} HTTP/1.1\r\n", "Host: 127.0.0.1\r\n", "X-Arete: 1\r\n"]
            with create_connection(("127.0.0.1", port), timeout=3) as conn:
                conn.sendall(("".join(raw) + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode() + body)
                conn.shutdown(SHUT_WR)
                response = HTTPResponse(conn)
                response.begin()
                return response.status, response.headers, response.read()

        meta_route, batch_route = "/api/v1/meta", "/api/v1/batch"
        media = ("Content-Type", "application/json")

        def post(path, body, media_header=media):
            return exchange("POST", path, (media_header, ("Content-Length", len(body))), body)

        def problem(response, status, code):
            actual, headers, body = response
            self.assertEqual(actual, status, body)
            self.assertEqual(headers["Content-Type"], "application/problem+json")
            self.assertEqual(headers["Arete-API"], contract.VERSION)
            self.assertEqual(json.loads(body)["code"], code)

        now = [0.0]
        with mock.patch.object(api_http, "RATES", api_http.RateLimits(lambda: now[0])), \
                mock.patch.object(tasks, "start", side_effect=AssertionError("invalid request started a task")):
            problem(exchange("GET", meta_route + "?x=" + "a" * 65536), 414, "uri_too_long")
            for method, route, allow in (("GET", batch_route, "POST"), ("PUT", meta_route, "GET")):
                refused = exchange(method, route)
                problem(refused, 405, "method_not_allowed")
                self.assertEqual(refused[1]["Allow"], allow)
            for headers, body in (((), b""), ((media, ("Content-Length", "bad")), b""),
                                  ((media, ("Content-Length", "0"), ("Content-Length", "0")), b""),
                                  ((media, ("Transfer-Encoding", "chunked")), b""),
                                  ((media, ("Content-Length", "4")), b"{}")):
                with self.subTest(headers=headers):
                    problem(exchange("POST", batch_route, headers, body), 400, "invalid_framing")
            for raw in (b'{"requests":[],"requests":[]}', b'{"x":NaN}', b'{"x":Infinity}',
                        b'{"x":9007199254740992}', b'{"x":' + b"[" * 32 + b"0" + b"]" * 32 + b"}"):
                with self.subTest(raw=raw):
                    problem(post(batch_route, raw), 400, "invalid_json")
            problem(post(batch_route, b"{}", ("Content-Type", "text/plain")), 415, "unsupported_media_type")
            status, _, body = post("/api/tasks", b"{}", ("Content-Type", "text/plain"))
            self.assertEqual((status, json.loads(body)["code"]), (415, "unsupported_media_type"))
            problem(exchange("POST", batch_route, (media, ("Content-Length", api_http.MAX_JSON_BYTES + 1))),
                    413, "body_too_large")
            self.assertEqual(exchange("GET", meta_route, [("Fixture", "x")] * 98)[0], 200)
            problem(exchange("GET", meta_route, [("Fixture", "x")] * 99), 431, "headers_too_large")

            _, headers, _ = exchange("GET", meta_route)
            match = [("If-None-Match", headers["ETag"])]
            status, empty, body = exchange("GET", meta_route, match)
            self.assertEqual((status, body), (304, b""))
            for name in ("Arete-API", "Arete-Seq", "ETag"):
                self.assertEqual(empty[name], headers[name])
            self.assertIsNone(empty["Content-Length"])
            with db.tx() as conn:
                conn.execute("UPDATE schema_meta SET value = cast(value AS INTEGER) + 1 WHERE key = 'restore_epoch'")
            status, newer, _ = exchange("GET", meta_route, match)
            self.assertEqual(status, 200)
            self.assertNotEqual(headers["ETag"], newer["ETag"])
            self.assertNotEqual(headers["Arete-Seq"], newer["Arete-Seq"])

            dashboard.POSTS_IN_FLIGHT.close()
            try:
                for response in (exchange("GET", meta_route), post(batch_route, b"{}")):
                    problem(response, 503, "draining")
                    self.assertEqual(response[1]["Retry-After"], "2")
            finally:
                dashboard.POSTS_IN_FLIGHT.reopen()
            self.assertEqual(exchange("GET", meta_route)[0], 200)

            logged = StringIO()
            raw = b'{"colour":"private-body"}'
            with redirect_stderr(logged):
                exchange("POST", "/api/tasks?fixture=private-query",
                         (media, ("Content-Length", len(raw)), ("Idempotency-Key", "private-key")), raw)
            event = json.loads(logged.getvalue().splitlines()[-1])
            self.assertEqual((event["event"], event["route"], event["status"]), ("api.request", "/api/tasks", 422))
            self.assertNotIn("private-", logged.getvalue())

            with mock.patch.object(dashboard.webhooks, "receive", side_effect=AssertionError("GET webhook dispatched")):
                self.assertEqual(exchange("GET", dashboard.webhooks.HOOK_PATH + "fixture")[0], 405)

            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8WsAAAAASUVORK5CYII=")
            upload = "/api/capture/evidence?name=fixture.png"
            status, _, body = exchange("POST", upload, [("Content-Length", len(png))], png)
            self.assertEqual(status, 200, body)
            mime_status, _, _ = post(upload, png, ("Content-Type", "image/png"))
            self.assertEqual(mime_status, 200)
            evidence = json.loads(body)["evidence"]["id"]
            status, partial, body = exchange("GET", f"/api/capture/evidence/{evidence}", [("Range", "bytes=0-7")])
            self.assertEqual((status, body, partial["Content-Type"]), (206, png[:8], "image/png"))
            stale = headers["Arete-Seq"]
            status, stream_headers, body = exchange("GET", f"/api/v1/events?since={stale}",
                                                    [("Accept", "application/x-ndjson")])
            self.assertEqual((status, stream_headers["Content-Type"], json.loads(body)["kind"]),
                             (200, "application/x-ndjson", "reset"))

            now[0] += 10
            batch = json.dumps({"requests": [{"operation": "meta.get"}] * 50}).encode()
            self.assertEqual(post(batch_route, batch)[0], 200)
            for route in (meta_route, "/api/meta"):
                limit = exchange("GET", route)
                self.assertEqual((limit[0], limit[1]["Retry-After"], json.loads(limit[2])["code"]), (429, "1", "rate_limited"))
            now[0] += .021
            self.assertEqual(exchange("GET", meta_route)[0], 200)
            now[0] += .021
            problem(post(batch_route, b"{"), 400, "invalid_json")
            problem(post(batch_route, b"{"), 429, "rate_limited")

        now = [0.0]
        limits = api_http.RateLimits(lambda: now[0])
        limits.take("one", command=True, cost=60)
        with self.assertRaises(api_http.RateLimited):
            limits.take("one", command=True)
        limits.take("two", command=True)
        now[0] += .051
        limits.take("one", command=True)

    def check_publication_drain(self, port):
        def request(path, body=None, key=""):
            conn = HTTPConnection("127.0.0.1", port, timeout=3)
            headers = {"X-Arete": "1", "Accept": "application/json"}
            if body is not None:
                headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
            try:
                conn.request("GET" if body is None else "POST", path, headers=headers,
                             body=None if body is None else json.dumps(body))
                response = conn.getresponse()
                return response.status, response.headers, json.loads(response.read())
            finally:
                conn.close()

        self.cfg.arete_repo = self.repo.name
        chat_id = chats_store.chat_add(self.repo.name)
        chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Drain fixture", draft_body="Test the admitted write")
        body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
        path, key = f"/api/chats/{chat_id}/file", "drain-fixture-key"
        entered, release = threading.Event(), threading.Event()
        responses = []

        def publish(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("the fixture did not release the admitted request")
            return 42

        sender = threading.Thread(target=lambda: responses.append(request(path, body, key)))
        replacement = frozenset({("fixture.py", 2)})
        old = frozenset({("fixture.py", 1)})
        coordinator = object.__new__(serve.Server)
        coordinator.code, coordinator.code_seen = old, replacement
        coordinator.migration_stamp, coordinator.migration_error = replacement, ""
        coordinator.polls, coordinator.oncall = {}, None
        coordinator.job_running = mock.Mock(return_value=False)
        coordinator.sweeping = mock.Mock(return_value=False)
        coordinator.note_once = mock.Mock()

        def busy():
            prior = serve.Server.busy_with(coordinator)
            if not dashboard.POSTS_IN_FLIGHT.draining():
                self.assertEqual(prior, "")
                sender.start()
                self.assertTrue(entered.wait(3))
            else:
                self.assertEqual(prior, "the dashboard is still answering a request")
                refused = request(path, body, "new-request-during-drain")
                self.assertEqual((refused[0], refused[1]["Retry-After"], refused[2]["code"]), (503, "2", "draining"))
            return prior

        coordinator.busy_with = busy
        forbidden = AssertionError("unexpected external transport or restart")
        with (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")),
              mock.patch.object(serve, "code_stamp", return_value=replacement),
              mock.patch.object(util, "sh", return_value=SimpleNamespace(returncode=0)),
              mock.patch.object(serve.os, "execv", side_effect=forbidden),
              mock.patch.object(gh, "gh", side_effect=forbidden),
              mock.patch.object(gh, "find_issue_marker", return_value=""),
              mock.patch.object(gh, "issue_create", side_effect=publish) as sends,
              mock.patch.object(accounts, "read", side_effect=forbidden),
              mock.patch.object(chat.routing, "metered", return_value=False)):
            try:
                coordinator.load_new_code()
                coordinator.note_once.assert_called_once_with("new code waits to load: the dashboard is still answering a request")
            finally:
                release.set()
                if sender.ident is not None:
                    sender.join(4)
            self.assertEqual([response[0] for response in responses], [200])
            replay = request(path, body, key)
            self.assertEqual((replay[0], replay[1]["Idempotent-Replayed"]), (200, "true"))
            self.assertEqual(replay[2], responses[0][2])
            sends.assert_called_once()
            self.assertEqual(request("/api/v1/meta")[0], 200)

    def test_keyed_recovery(self):
        with db.reader() as c:
            before = c.execute("SELECT count(*) FROM tasks").fetchone()[0]
            events = c.execute("SELECT count(*) FROM events_log").fetchone()[0]
        with mock.patch.object(claims, "claim", side_effect=RuntimeError("claim failed")):
            answer = self.request({"repo": "fixture", "prompt": "Must roll back"})
        self.assertEqual(answer[0], 500)
        self.assertEqual(json.loads(answer[1])["error"], "The server could not complete the request.")
        with db.reader() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], before)
            self.assertEqual(c.execute("SELECT count(*) FROM events_log").fetchone()[0], events)
        self.assertEqual(claims.lookup(self.actor, self.id())["status"], 500)
        self.assertEqual(self.request({"repo": "fixture", "prompt": "Must roll back"}), answer)


class PrincipalTests(unittest.TestCase):
    def test_principal_gate(self):
        owner = principals.seed_owner("fixture@example.test")
        handler = SimpleNamespace(server=SimpleNamespace(node=SimpleNamespace(names={"fixture.test"}),
                                  logins=SimpleNamespace(of=mock.Mock(return_value="fixture@example.test"))),
                                  client_address=("127.0.0.1", 1), headers={"Host": "fixture.test", "X-Arete": "1"})
        with principals.acting("hand"):
            dashboard.Handler.admit(handler, "POST", "/api/tasks")
            self.assertEqual(principals.current(), owner)
            for host, login, header, status in (("foreign.test", "fixture@example.test", "1", 421),
                                               ("fixture.test", "stranger@example.test", "1", 403),
                                               ("fixture.test", "fixture@example.test", "", 403)):
                handler.headers = {"Host": host, "X-Arete": header}
                handler.server.logins.of.return_value = login
                with self.subTest(status=status), self.assertRaises(dashboard.Refusal) as got:
                    dashboard.Handler.admit(handler, "POST", "/api/tasks")
                self.assertEqual(got.exception.status, status)
        with self.assertRaises(sqlite3.IntegrityError), db.tx() as c:
            c.execute("INSERT INTO principals (id, kind, role, status, name, created) "
                      "VALUES ('u:other', 'human', 'owner', 'active', 'Other', ?)", (util.iso(),))
