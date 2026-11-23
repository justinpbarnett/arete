from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager, suppress
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote, urlencode

from . import _test_stacks

ADDRESS = "127.0.0.1"
LOGIN = "api-check@example.invalid"
SOURCE = Path(__file__).resolve().parent.parent
SCENARIOS = ("registry/router", "examples", "replay", "stale write", "error catalog", "CLI",
             "MCP", "line/event streaming", "shapes", "aliases", "reserved names")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def isolated_env(root):
    names = {"ARETE_CONFIG_DIR": "config", "ARETE_STATE_DIR": "state", "ARETE_LOG_DIR": "logs",
             "ARETE_WORKTREES_DIR": "worktrees", "TMPDIR": "tmp", "HOME": "home"}
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", _test_stacks.PREFIX_ENV) if k in os.environ}
    for variable, name in names.items():
        folder = root / name
        folder.mkdir(exist_ok=True)
        env[variable] = str(folder)
    return {**env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_KEY_1": "init.templateDir", "GIT_CONFIG_VALUE_1": str(root / "tmp"),
            "GIT_ALLOW_PROTOCOL": "file"}


def child_command(source, entry, *argv):
    entrypoint = f"{_test_stacks.bootstrap()}import sys; sys.path.insert(0, {str(source)!r}); {entry}"
    return [sys.executable, "-IB", "-c", entrypoint, *argv]


CHECK_TIMEOUT = 60


def kill_group(group):
    with suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)


def follow_launcher(lifeline):
    os.set_inheritable(lifeline, False)

    def watch():
        with os.fdopen(lifeline, "rb") as parent:
            parent.read(1)
        kill_group(os.getpgrp())

    require(os.getpgrp() == os.getpid(), "API check worker needs its own process group")
    threading.Thread(target=watch, daemon=True).start()


def wait_for_worker_exit(process):
    deadline = time.monotonic() + CHECK_TIMEOUT
    while not os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG):
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, CHECK_TIMEOUT)
        time.sleep(.025)


def drain_group(process):
    # Darwin reports EPERM if the only group member is the unreaped worker.
    with suppress(PermissionError):
        kill_group(process.pid)
    process.wait(timeout=2)
    deadline = time.monotonic() + 2
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        require(time.monotonic() < deadline, "fixture process group did not exit")
        time.sleep(.025)


def launch():
    root = None
    process = None
    drained = False
    try:
        with ExitStack() as cleanup:
            root = Path(tempfile.mkdtemp(prefix="arete-api-check-")).resolve()
            read, write = os.pipe()
            cleanup.callback(os.close, read)
            cleanup.callback(os.close, write)
            entry = "from arete.api_check import worker; sys.exit(worker(int(sys.argv[1])))"
            command = child_command(SOURCE, entry, str(read))
            process = subprocess.Popen(command, cwd=root, env=isolated_env(root), start_new_session=True, pass_fds=(read,))
            try:
                wait_for_worker_exit(process)
            finally:
                drain_group(process)
                drained = True
            return process.returncode
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"api check: could not finish the isolated check: {error}", file=sys.stderr)
        return 1
    finally:
        if root is not None:
            if process is None or drained:
                shutil.rmtree(root)
            else:
                print(f"api check: retained scratch state at {root} because the process group did not drain", file=sys.stderr)


def forbidden(*args, **kwargs):
    raise RuntimeError("api check forbids external transports and account lookup")


def loopback_only(event, args):
    if event in ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"):
        address = args[1]
        if address is None and event == "socket.sendmsg":
            return
        if not isinstance(address, tuple) or not address or address[0] != ADDRESS:
            forbidden()
    if event in ("socket.getnameinfo", "socket.gethostbyaddr"):
        forbidden()
    if event == "socket.getaddrinfo" and (type(args[0]) is not str or args[0] != ADDRESS or type(args[1]) is not int):
        forbidden()
    if event == "socket.gethostbyname" and (type(args[0]) is not str or args[0] != ADDRESS):
        forbidden()


def address_call(method):
    def checked(sock, *args, **kwargs):
        if args:
            address = args[-1]
        else:
            address = kwargs.get("address")
        if (type(address) is not tuple or len(address) != 2 or type(address[0]) is not str
                or address[0] != ADDRESS or type(address[1]) is not int):
            forbidden()
        return method(sock, *args, **kwargs)

    return checked


@contextmanager
def offline():
    sys.addaudithook(loopback_only)
    with ExitStack() as guards:
        for name in ("bind", "connect", "connect_ex", "sendto"):
            guards.enter_context(patch.object(socket.socket, name, address_call(getattr(socket.socket, name))))
        if hasattr(socket.socket, "sendmsg"):
            guards.enter_context(patch.object(socket.socket, "sendmsg", forbidden))
        from . import accounts, gh, jira, paths, tailnet, util

        root = Path.cwd()
        require(all(path == root / name and not path.is_symlink() for path, name in (
            (paths.CONFIG_DIR, "config"), (paths.STATE_DIR, "state"), (paths.LOG_DIR, "logs"),
            (paths.WORKTREES_DIR, "worktrees"), (paths.HOME, "home"), (Path(os.environ["TMPDIR"]), "tmp"))),
            "expected an isolated scratch root")
        for primitive in (patch("urllib.request.urlopen", forbidden), patch.object(accounts, "read", forbidden),
                          patch.object(gh, "gh", forbidden), patch.object(jira.Client, "from_env", forbidden),
                          patch.object(tailnet, "tailscale_run", forbidden),
                          patch.object(util, "github_token", return_value="")):
            guards.enter_context(primitive)
        yield


def cli_worker():
    with offline():
        from .cli import main

        return main()


def prepare_replay(root):
    from . import replay, util

    sample = root / "sample"

    def sample_git(args, cwd, *, check=True):
        where = cwd.resolve()
        require(where == sample or where.is_relative_to(sample), "replay Git working directory escaped the fixture")
        done = subprocess.run(["git", *args], cwd=where, text=True, capture_output=True, timeout=10, check=False)
        if check and done.returncode:
            raise RuntimeError(f"replay sample: git {args[0]}: {done.stderr.strip()}")
        return done.stdout

    with patch.object(util, "git", sample_git):
        config = replay.sample(sample, adw=True)
    return config, lambda repo: "origin" in sample_git(["remote"], repo.path).split()


class Check:
    def __init__(self, port, root, spawn):
        self.port, self.root, self.spawn = port, root, spawn

    @contextmanager
    def response(self, method, path, body=None, headers=None):
        conn = HTTPConnection(ADDRESS, self.port, timeout=5)
        merged = {"X-Arete": "1", "Accept": "application/json", **(headers or {})}
        if body is not None:
            merged.setdefault("Content-Type", "application/json")
        try:
            conn.connect()
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.request(method, path, json.dumps(body) if isinstance(body, dict) else body, merged)
            with conn.getresponse() as response:
                yield response
        finally:
            conn.close()

    def request(self, method, path, body=None, *, status=200, headers=None):
        from . import contract

        with self.response(method, path, body, headers) as response:
            raw = response.read()
            at = f"{method} {path}"
            require(response.status == status, f"{at} returned {response.status}, wanted {status}: {raw[:300]!r}")
            require(response.headers.get("Arete-API") == contract.VERSION, f"{at} has incorrect Arete-API")
            return response.headers, raw

    def read(self, path):
        headers, raw = self.request("GET", path)
        data = json.loads(raw)
        if "seq" in data:
            require(data["seq"] == headers.get("Arete-Seq"), f"GET {path}: seq differs from Arete-Seq")
        return data

    def registry(self):
        from . import contract
        from .contract_evolution import registry_problems

        self.published = self.read("/api/v1/contract")
        require(self.published["contract_sha"] == self.read("/api/v1/meta")["api"]["contract_sha"] == contract.digest(),
                "contract.get and meta.get disagree on the registry hash")
        require(all(self.published[key] == value for key, value in contract.publish().items()),
                "contract.get differs from the in-process registry")
        require(len(self.published["operations"]) >= 33, "v1 registry lost operations")
        problems = list(registry_problems(self.published))
        require(not problems, ", ".join(problems))
        for op in self.published["operations"]:
            route = re.sub(r"\{\w+\}", "route-check", op["path"])
            headers, _ = self.request("PUT", route, status=405)
            require(op["method"] in headers.get("Allow", "").split(", "),
                    f"{op['name']} {route}: registered method missing from Allow")

    def examples(self):
        from . import contract

        for op in self.published["operations"]:
            for kind in ("input", "result"):
                contract.validate(op["example"][kind], op[kind], f"{op['name']} example.{kind}")
        for error in self.published["errors"]:
            contract.validate(error["example"], error["schema"], f"{error['code']} example")

    def replay(self):
        from . import replay

        self.task_body = {"repo": replay.SAMPLE_REPO, "prompt": "Add kumquat", "title": "", "kind": "feat"}
        self.key = "check-replay-key"
        headers = {"Idempotency-Key": self.key}
        initial, first = self.request("POST", "/api/tasks", self.task_body, headers=headers)
        again, second = self.request("POST", "/api/tasks", dict(reversed(list(self.task_body.items()))), headers=headers)
        require(first == second and again.get("Idempotent-Replayed") == "true" and
                initial.get("Arete-Seq") == again.get("Arete-Seq"), "POST /api/tasks did not replay bytes and receipt")
        _, raw = self.request("POST", "/api/tasks", {**self.task_body, "prompt": "Different"}, status=422, headers=headers)
        require(json.loads(raw)["code"] == "idempotency_key_reused", "POST /api/tasks accepted changed keyed content")
        created = json.loads(first)
        self.task, self.run = created["task"], created["run"]
        require(created["state"] == "running" and self.run and len(self.spawn) == 1,
                "POST /api/tasks did not return one running task")
        require(len(self.read("/api/v1/tasks")["tasks"]) == len(self.read("/api/v1/runs")["runs"]) == 1,
                "POST /api/tasks replay created extra rows")

    def stale_write(self):
        from . import record

        path = f"/api/v1/runs/{self.run}"
        before = self.read(path)["run"]
        with subprocess.Popen([sys.executable, "-IB", "-c", "import sys; sys.stdin.buffer.read(1)"],
                              stdin=subprocess.PIPE, start_new_session=True) as replacement:
            record.note(self.run, pid=replacement.pid)
            current = self.read(path)["run"]
            _, raw = self.request("POST", f"/api/runs/{self.run}/stop", {"base_version": before["version"]}, status=409)
            problem = json.loads(raw)
            require(problem.get("code") == "version_conflict" and problem.get("current") == current and
                    current == self.read(path)["run"] and current["version"] == before["version"] + 1 and
                    replacement.poll() is None and not current.get("stop_asked"),
                    "stale run stop changed the run, signalled its child, or returned the wrong current object")

    def errors(self):
        from . import contract

        catalog = self.read("/api/v1/errors")["errors"]
        require(catalog == self.published["errors"], "error.list disagrees with contract.get")
        errors = {error["code"]: error for error in catalog}
        for client, minimum in contract.OLDEST_CLIENT.items():
            self.request("GET", "/api/v1/meta", headers={"X-Arete-Client": f"{client}/{minimum}"})
        self.request("POST", "/api/v1/batch", {"requests": []}, headers={"X-Arete": ""}, status=403)
        for method, path, body, custom, code in (
            ("GET", "/api/v1/no-such-operation", None, {}, "not_found"),
            ("PUT", "/api/v1/meta", None, {}, "method_not_allowed"),
            ("GET", "/api/v1/meta", None, {"X-Arete-Client": "cli/0.9"}, "client_too_old"),
            ("POST", "/api/v1/batch", b'{"requests":[],"requests":[]}', {}, "invalid_json"),
            ("POST", "/api/v1/batch", {}, {"Content-Type": "text/plain"}, "unsupported_media_type"),
        ):
            error = errors[code]
            headers, payload = self.request(method, path, body, status=error["status"], headers=custom)
            require(headers.get("Content-Type") == "application/problem+json", f"{method} {path}: missing problem media type")
            contract.validate(json.loads(payload), error["schema"], f"{method} {path} {code}")

    def cli_call(self, *argv, input=None):
        command = child_command(SOURCE, "from arete.api_check import cli_worker; sys.exit(cli_worker())", *argv)
        done = subprocess.run(command, env={**os.environ, "ARETE_SERVER": f"http://{ADDRESS}:{self.port}"},
                              text=True, input=input, capture_output=True, timeout=15, check=False)
        require(done.returncode == 0, f"arete {' '.join(argv)} failed: {done.stdout.strip()} {done.stderr.strip()}")
        return done

    def cli(self):
        done = self.cli_call("task", "start", "--repo", self.task_body["repo"], "--key", self.key, self.task_body["prompt"])
        require(f"Idempotency-Key: {self.key}" in done.stderr, "arete task start did not print its key")
        result = json.loads(done.stdout)
        require((result["task"], result["run"]) == (self.task, self.run) and len(self.spawn) == 1,
                "arete task start did not replay the HTTP-started task and run")
        shown = self.cli_call("task", "show", self.task, "--json")
        require(json.loads(shown.stdout)["id"] == self.task, "arete task show did not read the loopback task")

    def mcp(self):
        from . import owner_asks

        messages = [("initialize", {"protocolVersion": "2024-11-05"}), ("tools/list", {}),
                    ("tools/call", {"name": "ask_owner", "arguments": {
                        "questions": [{"header": "Branch", "question": "Which branch should we use?", "kind": "text"}],
                        "why": "Need a branch"}}),
                    ("tools/call", {"name": "ask_owner", "arguments": {}})]
        data = "".join(json.dumps({"jsonrpc": "2.0", "id": index, "method": method, "params": params}) + "\n"
                       for index, (method, params) in enumerate(messages))
        reply = [json.loads(line) for line in self.cli_call("mcp-ask", input=data).stdout.splitlines()]
        require([r.get("id") for r in reply] == list(range(len(messages))) and all(r["jsonrpc"] == "2.0" for r in reply),
                "mcp-ask did not correlate the JSON-RPC responses")
        init, tools, sent, rejected = [r["result"] for r in reply]
        require(init["protocolVersion"] == "2024-11-05" and tools["tools"][0]["name"] == owner_asks.TOOL and
                not sent["isError"] and rejected["isError"], f"ask_owner JSON-RPC results: {sent!r}, {rejected!r}")

    def streams(self):
        from . import contract, run_lines
        from .store import db, events, runs

        runs.add_feed(self.run, "plan", "Add kumquat")
        run_lines.write(self.run, "step_end", step="plan", result="pass")
        path = "/api/v1/line-stream?run=" + quote(self.run)
        first = self.read(path)
        require(first["lines"][-1]["text"] == "Add kumquat" and not self.read(path + f"&after={first['after']}")["lines"],
                "line_stream.read did not advance its cursor")
        _, lines = self.request("GET", f"/api/runs/{self.run}/events")
        require(any(json.loads(line).get("event") == "step_end" for line in lines.splitlines()),
                "run event stream did not contain the recorded step")
        for path in ("/api/v1/events", "/api/events"):
            with db.one_read() as connection:
                epoch, _ = events.cursor(events.metadata(connection)["head"])
            query = urlencode({"since": f"{epoch}.0", "subject": f"run:{self.run}"})
            with self.response("GET", f"{path}?{query}", headers={"Accept": "application/x-ndjson"}) as response:
                try:
                    require(response.status == 200 and response.headers.get("Content-Type") == "application/x-ndjson"
                            and response.headers.get("Arete-API") == contract.VERSION, f"{path} did not open an NDJSON stream")
                    for _ in range(16):
                        row = json.loads(response.readline(65536))
                        if row.get("kind") == "run.started" and row.get("id"):
                            break
                    else:
                        raise RuntimeError(f"{path} did not send the committed run event with a cursor")
                finally:
                    with db.tx() as connection:
                        connection.execute("UPDATE schema_meta SET value = ? WHERE key = 'restore_epoch'",
                                           (str(int(epoch) + 1),))
                for _ in range(16):
                    row = json.loads(response.readline(65536))
                    if row == {"kind": "reset", "reason": "epoch"}:
                        break
                else:
                    raise RuntimeError(f"{path} did not reset after the fixture epoch changed")
                require(not response.read(1), f"{path} kept streaming after reset")

    def shapes(self):
        from . import agent, contract, dashboard, paths, record, replay, triage
        from .sighting import Sighting
        from .store import chats, incidents, insights, runs
        from .store import triage as triage_store

        record.step(self.run, "plan", agent.Result(ok=True, runner="replay"), 0)
        record.mark(self.run, "diff", patch=(self.root / "sample/replay/build.patch").read_text())
        chat = chats.chat_add("")
        incident, _ = incidents.record_incident(Sighting("check", "expected", "fixture", "", "fixture incident", 1, "", ""))
        ticket = triage.Candidate("demo#7", "7", "Fixture ticket", "https://example.invalid/7", "old")
        triage_store.triage_put(triage.row(triage.GitHubSource("demo/repo", []), ticket, updated="old"))
        insights.research_set(ticket.ref, status="done", title="Fixture research")
        stopped = record.start("loop", pid=0)
        session = record.start("session")
        paths.record_dir(session).mkdir(parents=True, exist_ok=True)
        identities = {"task": self.task, "run": self.run, "step": str(runs.steps(self.run)[0]["id"]),
                      "chat": str(chat), "incident": str(incident), "triage": ticket.ref, "research": ticket.ref,
                      "project": replay.SAMPLE_REPO, "line": replay.SAMPLE_REPO, "view": "live", "loop": "poll"}
        for op in self.published["operations"]:
            name, method, path = (op[key] for key in ("name", "method", "path"))
            values = op["example"]["input"].copy()
            resource = name.partition(".")[0]
            target = {**identities, "id": identities.get(resource, ""),
                      "run": stopped if name == "run.stop" else session if contract.keyed_write(op) else self.run}
            for field in re.findall(r"\{(\w+)\}", path):
                path = path.replace("{" + field + "}", quote(target[field], safe=""))
                values.pop(field)
            if "run" in values:
                values["run"] = self.run
            if name in ("task.update", "run.stop"):
                row = self.read(f"/api/v1/{resource}s/{self.task if resource == 'task' else stopped}")[resource]
                values["base_version"] = row["version"]
            if name == "task.create":
                values["repo"] = replay.SAMPLE_REPO
            if method == "GET":
                data = self.read(path + ("?" + urlencode(values) if values else ""))
            else:
                key = {"Idempotency-Key": f"check-{name}"} if contract.keyed_write(op) else None
                if name == "serve.install":
                    with patch.object(dashboard, "install_serve", return_value={"restarting": True}):
                        headers, raw = self.request(method, path, values, headers=key)
                else:
                    headers, raw = self.request(method, path, values, headers=key)
                data = json.loads(raw)
                if name == "batch.run":
                    require(data["seq"] == headers.get("Arete-Seq") and all(row["seq"] == data["seq"] for row in data["results"]),
                            "batch.run mixed snapshots")
            contract.validate(data, op["result"], f"{name} ({method} {path}) result")

    def aliases(self):
        from . import paths

        primary = self.read("/api/v1/meta")
        headers, raw = self.request("GET", "/api/meta")
        require(headers.get("Deprecation") == "true" and json.loads(raw) == primary,
                "GET /api/meta lost its legacy metadata or deprecation header")
        sidebar, live = self.read("/api/sidebar"), self.read("/api/live")
        require(sidebar["seq"] == live["seq"] and sidebar["running"] == len(live["live"]["rows"]["runs"]),
                "GET /api/sidebar and GET /api/live disagree without a write")
        self.read("/api/chats")
        days = json.loads((paths.STATE_DIR / "api-aliases.json").read_bytes())
        require(all(key in days for key in ("GET /api/meta", "GET /api/sidebar", "POST /api/tasks", "GET /api/events")),
                "legacy alias requests were not recorded under their frozen routes")

    def reserved(self):
        from .contract_evolution import RESERVED_FIELDS, RESERVED_PATHS

        for path in sorted(RESERVED_PATHS - {"/api/v1/events"}):
            self.read(path.replace("{id}", "me"))
        before = self.read("/api/v1/tasks")["tasks"]
        fields = dict.fromkeys(RESERVED_FIELDS, "reserved")
        for path, body in (("/api/v1/usage", {**fields, "kind": "visit"}), ("/api/tasks", {**self.task_body, **fields})):
            _, raw = self.request("POST", path, body, status=422)
            require(sorted(json.loads(raw)["fields"]) == sorted(fields), f"{path} did not reject reserve fields")
        require(self.read("/api/v1/tasks")["tasks"] == before, "reserved input created a task")


def worker(lifeline):
    follow_launcher(lifeline)
    stage = "scratch startup"
    try:
        with offline(), ExitStack() as cleanup:
            from . import api_http, codebase, control, dashboard, paths, pipeline, principals, tailnet
            from .store import db

            root = Path.cwd()
            require(not paths.DB_FILE.exists(), "state database already exists")
            config, has_origin = prepare_replay(root)
            paths.CONFIG_FILE.write_text("max_parallel = 3\n" + "\n".join(config) + "\n")
            db.migrate()
            principals.seed_owner(LOGIN)
            codebase.load()
            children = []

            def spawn(repo, tid, force=False):
                entry = "from arete.api_check import fixture_child; fixture_child(*sys.argv[1:])"
                child = control.launch(child_command(SOURCE, entry, repo, str(tid)), start_new_session=False)
                children.append(child)
                cleanup.callback(reap, child)
                return child

            cleanup.enter_context(patch.object(pipeline, "spawn_run", spawn))
            cleanup.enter_context(patch.object(pipeline, "has_origin", has_origin))
            cleanup.enter_context(patch.object(api_http, "log_request"))
            node = SimpleNamespace(names={ADDRESS})
            logins = SimpleNamespace(of=lambda _: LOGIN)
            stage = f"listen on {ADDRESS}:0"
            handler = type("FixtureHandler", (dashboard.Handler,), {"disable_nagle_algorithm": True})
            with patch("socket.getfqdn", return_value=ADDRESS):
                server = tailnet.Server(ADDRESS, 0, node, logins, False, handler, dashboard.Redirect)
            server.daemon_threads = False
            with server:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
                thread.start()
                check = Check(server.server_port, root, children)
                actions = (check.registry, check.examples, check.replay, check.stale_write, check.errors, check.cli,
                           check.mcp, check.streams, check.shapes, check.aliases, check.reserved)
                try:
                    for number, (stage, exercise) in enumerate(zip(SCENARIOS, actions, strict=True), 1):
                        exercise()
                        print(f"{number}. {stage} passed", flush=True)
                finally:
                    server.shutdown()
                    thread.join()
    except Exception as error:
        print(f"api check: {stage}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


def fixture_child(repo, ticket):
    with offline():
        from . import launch_lease, record

        launch_lease.verify(repo, ticket)
        record.start("pipeline", repo=repo, issue=ticket)
        threading.Event().wait()


def reap(child):
    child.terminate()
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
