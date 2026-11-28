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
             "ARETE_WORKTREES_DIR": "worktrees", "TMPDIR": "tmp", "HOME": "home", "XDG_STATE_HOME": "xdg"}
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", _test_stacks.PREFIX_ENV,
                                      "ARETE_DRIVE_FLUSH", "ARETE_TEST_FAST_SYNC",
                                      "ARETE_TEST_DB_TEMPLATE") if k in os.environ}
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
            # Short resolved root keeps Unix socket paths within macOS 104-byte sun_path limit.
            root = Path(tempfile.mkdtemp(prefix="arete-api-check-", dir=Path("/tmp").resolve()))  # noqa: S108
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
        from . import environment

        where = cwd.resolve()
        require(where == sample or where.is_relative_to(sample), "replay Git working directory escaped the fixture")
        done = subprocess.run(["git", *args], cwd=where, text=True, capture_output=True, timeout=10, check=False,
                              env=environment.build(()))
        if check and done.returncode:
            raise RuntimeError(f"replay sample: git {args[0]}: {done.stderr.strip()}")
        return done.stdout

    with patch.object(util, "git", sample_git):
        config = replay.sample(sample, adw=True)
    return config, lambda repo: "origin" in sample_git(["remote"], repo.path).split()


def config_fixture_git(*args, layers=False):
    from . import environment, paths

    require(paths.CONFIG_DIR == Path.cwd() / "config" and not paths.CONFIG_DIR.is_symlink(),
            "config Git working directory escaped the fixture")
    root = paths.CONFIG_DIR / "layers" if layers else paths.CONFIG_DIR
    require(root.resolve() == root, "config Git working directory escaped the fixture")
    done = subprocess.run(["git", "-c", "user.name=Config Fixture", "-c", "user.email=config@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args], cwd=root,
                          capture_output=True, text=True, timeout=5, env=environment.build(()))
    require(done.returncode == 0, f"config fixture: git {args[0]}: {done.stderr.strip()}")
    return done.stdout.strip()


class Check:
    def __init__(self, port, root, spawn, token):
        self.port, self.root, self.spawn = port, root, spawn
        self.token = token

    @contextmanager
    def response(self, method, path, body=None, headers=None):
        conn = HTTPConnection(ADDRESS, self.port, timeout=5)
        merged = {"Accept": "application/json", **(headers or {})}
        if method != "GET":
            merged.setdefault("Authorization", f"Bearer {self.token}")
        if merged.get("Authorization") == "":
            del merged["Authorization"]
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

        if method == "GET":
            headers = {"Authorization": f"Bearer {self.token}", **(headers or {})}
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
            method = "OPTIONS" if contract.match("PUT", route) else "PUT"
            headers, _ = self.request(method, route, status=405)
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
        from . import environment, record

        path = f"/api/v1/runs/{self.run}"
        deadline = time.monotonic() + 5.0
        before = self.read(path)["run"]
        while before["status"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.05)
            before = self.read(path)["run"]
        require(before["status"] == "running", f"fixture child did not claim run {self.run}")
        with subprocess.Popen([sys.executable, "-IB", "-c", "import sys; sys.stdin.buffer.read(1)"],
                              stdin=subprocess.PIPE, start_new_session=True, env=environment.build(())) as replacement:
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
        self.request("POST", "/api/v1/tasks", {}, headers={"X-Arete": "1", "Authorization": ""}, status=401)
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
        from . import environment

        command = child_command(SOURCE, "from arete.api_check import cli_worker; sys.exit(cli_worker())", *argv)
        done = subprocess.run(command, env=environment.build(environment.INTERNAL_ARETE,
                                                             {"ARETE_SERVER": f"http://{ADDRESS}:{self.port}"}),
                              text=True, input=input, capture_output=True, timeout=15, check=False)
        require(done.returncode == 0, f"arete {' '.join(argv)} failed: {done.stdout.strip()} {done.stderr.strip()}")
        return done

    def cli(self):
        v1_body = {"repo": self.task_body["repo"], "body": self.task_body["prompt"], "kind": "feat", "start": True}
        cli_key = "check-cli-replay-key"
        with self.response("POST", "/api/v1/tasks", v1_body, headers={"Idempotency-Key": cli_key}) as resp:
            raw_first = resp.read()
            require(resp.status in (200, 201), f"POST /api/v1/tasks returned {resp.status}: {raw_first!r}")
        v1_created = json.loads(raw_first)
        spawns_after = len(self.spawn)
        done = self.cli_call("task", "start", "--repo", self.task_body["repo"], "--key", cli_key, self.task_body["prompt"])
        require(f"Idempotency-Key: {cli_key}" in done.stderr, "arete task start did not print its key")
        result = json.loads(done.stdout)
        require((result["task"], result["run"]) == (v1_created["task"]["id"], (v1_created.get("run") or {}).get("id", ""))
                and len(self.spawn) == spawns_after,
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
        from . import (
            agent, cohorts, config, contract, control, dashboard, devices, heartbeat, leases, oncall, outbox, paths,
            pipeline, principals, private_evidence, record, replay, research, secretstore, triage,
        )
        from . import tasks as tasks_mod
        from .sighting import Sighting
        from .store import blobs, chats, db, incidents, insights, push, runs, tokens
        from .store import evidence as evidence_store
        from .store import inbox as inbox_store
        from .store import tasks as task_store
        from .store import triage as triage_store

        record.step(self.run, "plan", agent.Result(ok=True, runner="replay"), 0)
        record.mark(self.run, "diff", patch=(self.root / "sample/replay/build.patch").read_text())
        fixture_blob = b"fixture-contract-blob"
        fixture_sha = blobs.put(fixture_blob, "upload")
        fixture_lease = leases.claim("local", devices.version_of("local"), run_id=self.run, start_renewer=False)
        fixture_lease.close("", fenced=False)
        chat = chats.chat_add("")
        chat_for_file = chats.chat_add("")
        chat_for_send = chats.chat_add("")
        chats.chat_say(chat_for_file, "arete", "Drafted", draft_title="Fixture draft", draft_body="Fixture draft body")
        incident, _ = incidents.record_incident(Sighting("check", "expected", "fixture", "", "fixture incident", 1, "", ""))
        incidents.alert_set("fixture-alert", None)
        fixture_item = inbox_store.get_open_by_subject("alert:fixture-alert")
        with db.tx() as c:
            ob_main = outbox.draft(c, target="github", kind="comment", object="github:demo/repo#7",
                                   body="Fixture outbox body", drafted_by="hand", slot="check-main")
            ob_undo = outbox.draft(c, target="github", kind="comment", object="github:demo/repo#7",
                                   body="Fixture undo", drafted_by="hand", slot="check-undo")
            ob_hold = outbox.draft(c, target="github", kind="comment", object="github:demo/repo#7",
                                   body="Fixture hold", drafted_by="hand", slot="check-hold")
            ob_reject = outbox.draft(c, target="github", kind="comment", object="github:demo/repo#7",
                                     body="Fixture reject", drafted_by="hand", slot="check-reject")
            ob_retry = outbox.draft(c, target="github", kind="comment", object="github:demo/repo#7",
                                    body="Fixture retry", drafted_by="hand", slot="check-retry")
            ob_discard = outbox.draft(c, target="github", kind="comment", object="github:demo/repo#7",
                                      body="Fixture discard", drafted_by="hand", slot="check-discard")
            c.execute("UPDATE outbox SET state = 'approved', send_at = '2099-01-01T00:00:00Z' WHERE id IN (?, ?)",
                      (ob_undo, ob_hold))
            c.execute("UPDATE outbox SET state = 'failed', error = 'fixture' WHERE id = ?", (ob_retry,))
        outbox_targets = {
            "outbox.undo": f"OB-{ob_undo}",
            "outbox.hold": f"OB-{ob_hold}",
            "outbox.release": f"OB-{ob_hold}",
            "outbox.reject": f"OB-{ob_reject}",
            "outbox.retry": f"OB-{ob_retry}",
            "outbox.discard": f"OB-{ob_discard}",
        }
        ev_id = private_evidence.request_id(replay.SAMPLE_REPO, self.task, "jira:attachment:fixture-1")
        evidence_store.upsert_missing(
            ev_id,
            repo=replay.SAMPLE_REPO,
            ticket=self.task,
            ticket_ref=self.task,
            tracker="jira",
            run_id=self.run,
            ref_key="jira:attachment:fixture-1",
            ref_kind="jira_attachment",
            source="body",
            name="fixture.png",
            attachment_id="fixture-1",
            media_kind="image",
            failure_status="inaccessible",
            failure_detail="HTTP 403 access denied",
        )
        fixture_evidence_item = inbox_store.get_open_by_subject(f"evidence:{ev_id}")
        fault_run = record.start(
            "pipeline", repo=replay.SAMPLE_REPO, issue=self.task,
            task_id=task_store.parse_task_num(self.task), issue_key=self.task, status="failed",
        )
        record.step(fault_run, "verify", agent.Result(ok=False, error="AssertionError: fixture", runner="replay"), 0.1)
        fixture_fault_item = inbox_store.get_open_by_kind_and_subject("fault", f"step:{fault_run}/verify")
        sub_id = push.push_add("http://127.0.0.1:9999/fixture-push", "fixturep256dh", "fixtureauth", "Fixture", LOGIN)
        ticket = triage.Candidate("demo#7", "7", "Fixture ticket", "https://example.invalid/7", "old")
        triage_store.record_verdict(
            triage.row(triage.GitHubSource("demo/repo", []), ticket, updated="old"),
            ensure_listed=True,
            key_override="demo/repo#7",
        )
        sample_cfg = config.load()
        sample_repo = sample_cfg.repo(replay.SAMPLE_REPO)
        send_src = triage.GitHubSource(sample_repo.github or f"owner/{sample_repo.name}", [sample_repo])
        send_ticket = triage.Candidate(f"{sample_repo.name}#9", "9", "Fixture send ticket", "https://example.invalid/9", "old")
        triage_store.record_verdict(
            {
                **triage.row(send_src, send_ticket, updated="old"),
                "repo": sample_repo.name,
                "repos": [sample_repo.name],
                "ready": True,
                "code": 1,
                "questions": [],
            },
            ensure_listed=True,
            key_override=f"{send_src.key}#9",
        )
        insights.research_set(ticket.ref, status="done", title="Fixture research")
        stopped = record.start("loop", pid=0)
        session = record.start("session")
        run_task = task_store.task_add(replay.SAMPLE_REPO, "Task to start via task.run", "Run me", "feat")
        sync_task = task_store.task_add(replay.SAMPLE_REPO, "Task to sync via task.sync", "Sync me", "feat")
        task_store.task_comment(self.task, principals.owner(), "Initial comment for check")
        fixture_comment = task_store.task_comments(self.task)[0]
        with db.tx() as conn:
            fixture_ref_link = task_store.TaskStore.link_ref(
                conn, self.task, "slack:C1/1700000000.000001", {"role": "related", "interpretation": "Fixture ref"},
                principals.owner(),
            )
            task_store.TaskStore.link_ref(
                conn, sync_task, "slack:C1/1700000000.000002", {"role": "primary", "interpretation": "Sync ref"},
                principals.owner(),
            )
            fixture_link = task_store.TaskStore.create_link(
                conn, self.task, "relates", other_key="github:sample/replay#42", origin="person",
            )
        fixture_token, _ = tokens.issue(principals.owner(), "Contract example", ["read"])
        secretstore.put("global", "DEMO_KEY", "fixture-contract-key", "api_key", "hand")
        paths.record_dir(session).mkdir(parents=True, exist_ok=True)
        turn_step = runs.steps(self.run)[0]
        if not runs.step_turns(turn_step["id"]):
            rec_files = record.record_files(self.run)
            if rec_files:
                rec_path = rec_files[0]
            else:
                with record.Recorder(self.run, "plan", {"runner": "replay", "model": "replay"}, step_id=turn_step["id"]) as rec:
                    rec.line('{"type":"replay","answer":"fixture"}')
                    rec_path = rec.path
            first_offset, first_len, _ = next(record.AppendedLines(rec_path).read_members(final=True))
            runs.insert_turn(
                turn_step["id"], 1, self.run, at="2026-01-01T00:00:00Z", status="ok",
                tool="", excerpt="fixture", seconds=0.0, tokens_in=1, cache_read=0, tokens_out=1,
                cost_nusd=0, file=record._relative_record_file(rec_path),
                member_offset=first_offset, member_len=first_len,
            )
        cohorts.declare("example", replay.SAMPLE_REPO, "fixture", "single known task", tickets=[self.task],
                        closes="2099-01-01T00:00:00Z", hours=24)
        from . import configrepo as configrepo_mod
        from . import drafts as drafts_mod
        from . import lines as lines_mod

        lines_mod.ensure(config.load())
        with principals.acting(principals.owner() or "hand"):
            if lines_mod.get_line(f"{replay.SAMPLE_REPO}-pipeline") is None:
                lines_mod.create_line({
                    "id": f"{replay.SAMPLE_REPO}-pipeline",
                    "project": replay.SAMPLE_REPO,
                    "name": replay.SAMPLE_REPO,
                    "kind": "pipeline",
                    "repo": replay.SAMPLE_REPO,
                })
            if lines_mod.get_view("fixture-view") is None:
                lines_mod.create_view({"id": "fixture-view", "name": "Fixture View", "filter": "state = ready"})
        cur_cfg_text = paths.CONFIG_FILE.read_text()
        alt_cfg_text = cur_cfg_text.replace("max_parallel = 3", "max_parallel = 4", 1)
        if alt_cfg_text == cur_cfg_text:
            alt_cfg_text = cur_cfg_text.replace("max_parallel = 2", "max_parallel = 4", 1)
        scratch_dev = paths.CONFIG_DIR / "devices" / "scratch.toml"
        scratch_dev.parent.mkdir(parents=True, exist_ok=True)
        scratch_dev.write_text('kind = "ssh"\nhost = "scratch.invalid"\n', encoding="utf-8")
        configrepo_mod.commit_hand_edits()
        draft_for_activate, _ = drafts_mod.create_and_check("step_settings", "defaults", alt_cfg_text, author="hand", why="fixture activate")
        draft_for_try, _ = drafts_mod.create_and_check("step_settings", "defaults", alt_cfg_text, author="hand", why="fixture try")
        draft_for_discard, _ = drafts_mod.create_and_check("step_settings", "defaults", alt_cfg_text, author="hand", why="fixture discard")
        identities = {"task": self.task, "run": self.run, "step": str(turn_step["id"]),
                      "node": str(turn_step.get("node") or turn_step["step"]), "try": str(turn_step.get("try") or 1),
                      "n": "1", "ticket": send_ticket.ref, "ref": fixture_ref_link["ref"],
                      "link": str(fixture_link["id"]), "comment": str(fixture_comment["id"]),
                      "chat": str(chat), "incident": str(incident), "triage": ticket.ref, "research": ticket.ref,
                      "project": replay.SAMPLE_REPO, "line": f"{replay.SAMPLE_REPO}-pipeline", "view": "fixture-view",
                      "loop": "poll",
                      "token": fixture_token["id"], "principal": f"client:{fixture_token['id']}",
                      "item": fixture_item["id"], "subscription": f"PS-{sub_id}", "sha256": fixture_sha,
                      "device": "local", "lease": fixture_lease.label, "draft": draft_for_discard["id"]}
        for op in self.published["operations"]:
            name, method, path = (op[key] for key in ("name", "method", "path"))
            values = op["example"]["input"].copy()
            resource = name.partition(".")[0]
            ob_item = outbox_targets.get(name, f"OB-{ob_main}")
            target = {**values, **identities, "id": identities.get(resource, ""),
                      "line": "demo-secondary" if name == "line.delete" else identities["line"],
                      "chat": (str(chat_for_file) if name == "chat.file"
                               else str(chat_for_send) if name in ("chat.send", "chat.session_start")
                               else identities["chat"]),
                      "item": ob_item if resource == "outbox" else identities["item"],
                      "task": (run_task if name == "task.run"
                               else sync_task if name == "task.sync"
                               else identities["task"]),
                      "run": stopped if name == "run.stop" else session if contract.keyed_write(op) else self.run}
            for field in re.findall(r"\{(\w+)\}", path):
                path = path.replace("{" + field + "}", quote(target[field], safe=""))
                values.pop(field)
            if "run" in values:
                values["run"] = self.run
            if name in ("task.update", "run.stop"):
                row = self.read(f"/api/v1/{resource}s/{self.task if resource == 'task' else stopped}")[resource]
                values["base_version"] = row["version"]
            if name in ("project.update", "line.update", "view.update"):
                row = self.read(f"/api/v1/{resource}s/{identities[resource]}")[resource]
                values["base_version"] = row["version"]
            if name in ("project.restore", "line.restore", "view.restore"):
                row = self.read(f"/api/v1/{resource}s/{identities[resource]}")[resource]
                hist = self.read(f"/api/v1/{resource}s/{identities[resource]}/history")["events"]
                values["base_version"] = row["version"]
                values["seq"] = hist[0]["seq"]
            if name == "line.delete":
                row = self.read("/api/v1/lines/demo-secondary")["line"]
                values["base_version"] = row["version"]
            if name == "comment.update":
                values["base_version"] = fixture_comment["version"]
            if name == "task.restore":
                # task.close just closed self.task right before task.restore in OPERATIONS order.
                pass
            if name in ("inbox.snooze", "inbox.shelve", "inbox.reopen", "inbox.resolve"):
                values["base_version"] = self.read(f"/api/v1/inbox/{identities['item']}")["item"]["version"]
            if resource == "outbox" and method != "GET":
                cur_ob = self.read(f"/api/v1/outbox/{ob_item}")["item"]
                values["base_version"] = cur_ob["version"]
                if name == "outbox.edit":
                    values["base_rev"] = cur_ob["rev"]
                elif name == "outbox.approve":
                    values["rev"] = cur_ob["rev"]
            if name == "config.diff":
                values["from"], values["to"] = "1", "2"
            if name in ("config.draft", "config.check"):
                values["body"] = alt_cfg_text
            if name == "config.activate":
                values["draft"] = draft_for_activate["id"]
                values["base_version"] = self.read("/api/v1/config/step_settings/defaults")["item"]["version"]
            if name == "config.rollback":
                values["to"] = 2
                values["base_version"] = self.read("/api/v1/config/step_settings/defaults")["item"]["version"]
            if name == "config.delete":
                values["base_version"] = self.read("/api/v1/config/device/scratch")["item"]["version"]
            if name == "config.try":
                values["draft"] = draft_for_try["id"]
                values["steps"] = ["plan"]
            if name == "settings.update":
                values["base_version"] = self.read("/api/v1/settings")["settings"]["version"]
            if name == "task.create":
                values["repo"] = replay.SAMPLE_REPO
            if name == "chat.file":
                values["repo"] = replay.SAMPLE_REPO
            if name == "triage.send":
                values["repo"] = sample_repo.name
                values["base_version"] = int((triage.board_row(send_ticket.ref) or {}).get("version") or 1)
                values["questions"] = []
                values["answers"] = []
            if method == "GET":
                data = self.read(path + ("?" + urlencode(values) if values else ""))
            else:
                key = {"Idempotency-Key": f"check-{name}"} if contract.keyed_write(op) else None
                denied = {"token.issue": 426, "token.rotate": 426, "pairing.claim": 426, "session.end": 401,
                          "secret.set": 426, "secret.remove": 202, "webhook.rotate": 426, "push.subscribe": 202}
                if name in denied:
                    self.request(method, path, values, headers=key, status=denied[name])
                    continue
                if name == "serve.install":
                    with patch.object(dashboard, "install_serve", return_value={"restarting": True}):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                elif name == "triage.send":
                    fake_text = triage.TicketText("", "", "", "old", (), send_ticket.title)
                    with patch.object(triage, "sources", return_value=[send_src]), \
                            patch.object(triage, "read_send_ticket", return_value=fake_text):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                elif name == "inbox.retry":
                    retry_path = f"/api/v1/inbox/{quote(fixture_fault_item['id'], safe='')}/retry"
                    q_run = pipeline.queue_run(replay.SAMPLE_REPO, self.task)
                    stub_ans = tasks_mod.answer(self.task, replay.SAMPLE_REPO, q_run, "queued", "started")
                    with patch.object(tasks_mod, "resume", return_value=stub_ans):
                        headers, raw = self.request(method, retry_path, values, headers=key, status=op["success_status"])
                elif name == "inbox.upload_evidence":
                    import struct
                    import zlib

                    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
                    idat = zlib.compress(b"\x00\xff\x00\x00")
                    png = (
                        b"\x89PNG\r\n\x1a\n"
                        + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
                        + struct.pack(">I", len(idat)) + b"IDAT" + idat + struct.pack(">I", zlib.crc32(b"IDAT" + idat) & 0xFFFFFFFF)
                        + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
                    )
                    ev_path = f"/api/v1/inbox/{quote(fixture_evidence_item['id'], safe='')}/evidence"
                    ev_headers = {
                        **(key or {}),
                        "Content-Type": "application/octet-stream",
                        "X-Evidence-Filename": "fixture.png",
                    }
                    with patch.object(private_evidence, "resume_ticket", return_value={"state": "queued", "why": "fixture"}):
                        headers, raw = self.request(method, ev_path, png, headers=ev_headers, status=op["success_status"])
                elif name == "blob.put":
                    headers, raw = self.request(method, path, fixture_blob,
                                                headers={**(key or {}), "Content-Type": "application/octet-stream"},
                                                status=op["success_status"])
                elif name in ("task.run", "session.start", "chat.session_start", "loop.start", "loop.try",
                              "loop.draft", "config.ask"):
                    dummy_proc = type("_P", (), {
                        "pid": 0, "poll": lambda self: None, "terminate": lambda self: None,
                        "kill": lambda self: None, "wait": lambda self, timeout=None: 0,
                    })()
                    with (patch.object(control, "launch", return_value=dummy_proc),
                          patch.object(control, "detach", return_value=dummy_proc)):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                elif name in ("run.resume", "run.repair", "run.step"):
                    q_run = pipeline.queue_run(replay.SAMPLE_REPO, self.task)
                    stub_ans = tasks_mod.answer(self.task, replay.SAMPLE_REPO, q_run, "queued", "started")
                    fn_name = {"run.resume": "resume", "run.repair": "repair", "run.step": "run_step"}[name]
                    with patch.object(tasks_mod, fn_name, return_value=stub_ans):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                elif name == "chat.send":
                    from . import chat as chat_mod

                    def _stub_chat_answer(chat_id, text, message_id, project, *, run_id=""):
                        if run_id:
                            record.settle(run_id, "done")
                        chats.chat_say(chat_id, "arete", "Fixture reply", busy=0)

                    with patch.object(chat_mod, "answer", side_effect=_stub_chat_answer):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                elif name == "triage.refresh":
                    with patch.object(triage, "run", return_value=0):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                elif name == "triage.research":
                    with (patch.object(research, "ticket", return_value={"ref": ticket.ref}),
                          patch.object(research, "run_now", return_value=0)):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                elif name == "health.check":
                    with patch.object(heartbeat, "run", return_value=0):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                elif name == "incident.scan":
                    with patch.object(oncall, "scan", return_value={"seen": 0}):
                        headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                else:
                    headers, raw = self.request(method, path, values, headers=key, status=op["success_status"])
                data = json.loads(raw)
                if name == "batch.run":
                    require(data["seq"] == headers.get("Arete-Seq") and all(row["seq"] == data["seq"] for row in data["results"]),
                            "batch.run mixed snapshots")
            contract.validate(data, op["result"], f"{name} ({method} {path}) result")
            if name == "config.versions":
                require(bool(data["versions"]), "config.versions returned no config Git history")
            if name == "config.diff":
                lines = data["diff"].splitlines()
                require("-max_parallel = 2" in lines and "+max_parallel = 3" in lines,
                        "config.diff did not show the fixture's config change")

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
            from . import (
                api_http,
                codebase,
                control,
                credential_files,
                dashboard,
                paths,
                pipeline,
                principals,
                tailnet,
                verify_faults,
            )
            from .store import db, tokens

            root = Path.cwd()
            require(not paths.DB_FILE.exists(), "state database already exists")
            (root / ".arete-owned").write_bytes(verify_faults.MARKER + b"\n")
            config, has_origin = prepare_replay(root)
            body = "\n".join(config) + '\ngithub = "sample/replay"\n[secrets]\nstore = "pve_store"\nstore_dir = "../store"\n'
            paths.CONFIG_FILE.write_text("max_parallel = 2\n" + body)
            config_fixture_git("init", "-q", "-b", "main")
            config_fixture_git("add", "--", "config.toml")
            config_fixture_git("commit", "-qm", "Initial config fixture")
            paths.CONFIG_FILE.write_text("max_parallel = 3\n" + body)
            db.migrate()
            from . import configrepo

            configrepo.rebuild_index()
            principals.seed_owner(LOGIN)
            tokens.init_owner()
            token = credential_files.read_client(db.schema_metadata()["instance_tag"])
            codebase.load()
            children = []

            def spawn(repo, tid, force=False, *, run_id="", line=""):
                entry = "from arete.api_check import fixture_child; fixture_child(*sys.argv[1:])"
                child = control.launch(child_command(SOURCE, entry, repo, str(tid), run_id), start_new_session=False)
                children.append(child)
                cleanup.callback(reap, child)
                return child

            cleanup.enter_context(patch.object(pipeline, "spawn_run", spawn))
            cleanup.enter_context(patch.object(pipeline, "has_origin", has_origin))
            cleanup.enter_context(patch.object(api_http, "log_request"))
            node = SimpleNamespace(names={ADDRESS})
            logins = tailnet.Logins(tailnet.ScratchHost())
            logins.of = lambda _: LOGIN
            stage = f"listen on {ADDRESS}:0"
            handler = type("FixtureHandler", (dashboard.handlers().open,), {"disable_nagle_algorithm": True})
            with patch("socket.getfqdn", return_value=ADDRESS):
                server = tailnet.Server(ADDRESS, 0, node, logins, False, handler, dashboard.Redirect)
            server.daemon_threads = False
            with server:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
                thread.start()
                check = Check(server.server_port, root, children, token)
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


def fixture_child(repo, ticket, run_id=""):
    with offline():
        from . import launch_lease, principals, record
        from .store import db as foundation
        from .store import tasks as task_store

        launch_lease.verify(repo, ticket)
        if run_id:
            record.claim(run_id)
        else:
            with foundation.tx() as c:
                task_num, issue_key = task_store.TaskStore.task_for_ticket(c, repo, ticket, principals.current())
                record.start("pipeline", repo=repo, issue=ticket, task_id=task_num, issue_key=issue_key)
        threading.Event().wait()


def reap(child):
    child.terminate()
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
