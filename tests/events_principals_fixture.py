from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.harness import SOURCE, scratch_env

LOGIN = "fixture@example.test"
SECOND = "second@example.test"
EXERCISES = ("live-replay", "reconnect-formats", "filters", "epoch-reset", "retention-reset", "backlog-reset",
             "atomicity", "admission-seed", "principal-gate", "row-actors", "completeness", "beats", "nearby")


def isolated(root):
    for name in ("config", "state", "logs", "worktrees", "tmp", "home", "bin"):
        (root / name).mkdir(parents=True, exist_ok=True)
    fake = root / "bin/tailscale"
    fake.write_text(f"#!{sys.executable}\nimport json, pathlib, sys\n"
                    f"root = pathlib.Path({str(root)!r})\n"
                    f"login = (root / 'who').read_text() if (root / 'who').exists() else {LOGIN!r}\n"
                    "if sys.argv[1] == 'status':\n"
                    " print(json.dumps({'BackendState':'Running', 'Self':{'TailscaleIPs':['127.0.0.1'], "
                    "'DNSName':'fixture.test.', 'UserID':1}, 'User':{'1':{'LoginName':" + repr(LOGIN) +
                    ", 'DisplayName':'Fixture Person'}}}))\n"
                    "elif sys.argv[1] == 'whois': print(json.dumps({'UserProfile':{'LoginName':login}}))\n"
                    "else: sys.exit(1)\n")
    fake.chmod(0o755)
    env = scratch_env(root, ARETE_WORKTREES_DIR=str(root / "worktrees"), TMPDIR=str(root / "tmp"),
                      HOME=str(root / "home"), PATH=str(root / "bin") + os.pathsep + os.environ.get("PATH", ""),
                      GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="1",
                      GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
    return env


def launch(scenario, root=None, port=0):
    root = Path(root) if root else Path(tempfile.mkdtemp(prefix="arete-events-"))
    env = isolated(root)
    command = [sys.executable, "-I", str(SOURCE / "docs/events-fixture.py"), "--child", scenario,
               "--root", str(root), "--port", str(port)]
    print(f"Evidence: {root}", flush=True)
    return root, subprocess.Popen(command, cwd=SOURCE, env=env, text=True,
                                  stdout=(root / f"stdout-{scenario}.txt").open("w"),
                                  stderr=(root / f"stderr-{scenario}.txt").open("w"))


def exercise(scenario, root=None):
    root, process = launch(scenario, root)
    try:
        code = process.wait(timeout=240 if scenario == "beats" else 180)
        if code:
            raise AssertionError(f"{scenario}: {root}\n{(root / ('stdout-' + scenario + '.txt')).read_text()}\n"
                                 f"{(root / ('stderr-' + scenario + '.txt')).read_text()}")
        return root
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


class Fixture:
    def __init__(self, root, port=0):
        from arete import codebase, config, dashboard, paths, principals, state, tailnet
        from arete.store import db as foundation

        self.root, self.server, self.thread = root, None, None
        self.state, self.paths, self.principals, self.tailnet = state, paths, principals, tailnet
        codebase.load()
        state.migrate()
        if not paths.ISOLATED:
            connection = foundation.connect_db(paths.DB_FILE)
            try:
                assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            finally:
                connection.close()
        self.config = config
        if not paths.CONFIG_FILE.exists():
            paths.CONFIG_FILE.write_text('[dashboard]\nowner="' + LOGIN + '"\n')
        self.host = tailnet.ScratchHost()
        self.node = self.host.node()
        assert self.node and self.node.addresses == ("127.0.0.1",)
        self.open(port)
        self.person = principals.owner()
        self.transcript = root / "http.jsonl"
        self.dashboard = dashboard

    def open(self, port=0):
        from arete import dashboard

        self.tailnet.seed_owner(self.host, self.config.load())
        self.server = self.tailnet.Server("127.0.0.1", port, self.node, self.tailnet.Logins(self.host), False,
                                          dashboard.Handler, dashboard.Redirect)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            self.server = None

    def request(self, path, accept="application/json", headers=None, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=40)
        conn.request(method, path, body=body, headers={"Accept": accept, **(headers or {})})
        response = conn.getresponse()
        return conn, response

    def get(self, path, status=200, headers=None):
        conn, response = self.request(path, headers=headers)
        raw = response.read()
        record = {"at": time.time(), "path": path, "status": response.status,
                  "headers": dict(response.getheaders()), "body": raw.decode()}
        with self.transcript.open("a") as out:
            out.write(json.dumps(record) + "\n")
        conn.close()
        assert response.status == status, record
        return json.loads(raw) if raw else None

    def poll(self, since="0", **filters):
        from urllib.parse import urlencode

        return self.get("/api/events?" + urlencode({"since": since, "timeout": 0, **filters}))

    def who(self, login):
        (self.root / "who").write_text(login)
        self.server.logins.seen.clear()

    def emit(self, kind="fixture.changed", cls="derived", subject="task:142", **kw):
        from arete import events

        with self.state.tx():
            return events.emit(kind, subject, cls=cls, data=kw.pop("data", {}), **kw)

    def rows(self, sql="SELECT * FROM events_log ORDER BY seq", args=()):
        with self.state.reader() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def save(self):
        result = {table: self.rows(f"SELECT * FROM {table}") for table in
                  ("schema_meta", "schema_migrations", "principals", "events_log", "runs", "task_comments", "triage_approvals")}
        (self.root / "database.json").write_text(json.dumps(result, indent=2))


class Stream:
    def __init__(self, fixture, path="/api/events", accept="text/event-stream", headers=None, step_limit=0):
        self.fixture, self.messages, self.raw = fixture, [], []
        self.conn, self.response = fixture.request(path, accept, headers)
        assert self.response.status == 200
        self.headers = dict(self.response.getheaders())
        self.error, self.done = None, threading.Event()
        self.step_limit = step_limit
        self.thread = threading.Thread(target=self.read, args=(accept,), daemon=True)
        self.thread.start()

    def read(self, accept):
        frame = {}
        try:
            while line := self.response.readline():
                text = line.decode()
                self.raw.append({"at": time.time(), "line": text})
                if accept == "application/x-ndjson":
                    self.messages.append({"at": time.time(), "data": json.loads(text)})
                elif not text.strip():
                    if "data" in frame:
                        self.messages.append({"at": time.time(), **frame})
                        if self.step_limit and sum(m["data"].get("kind", "").startswith("step.")
                                                   for m in self.messages) >= self.step_limit:
                            with contextlib.suppress(OSError):
                                self.response.fp.raw._sock.shutdown(2)
                            return
                    frame = {}
                else:
                    key, _, value = text.rstrip("\n").partition(":")
                    frame[key] = json.loads(value) if key == "data" else value.strip()
        except (OSError, ValueError) as e:
            self.error = repr(e)
        finally:
            self.done.set()

    def wait(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while not predicate(self.messages):
            assert time.monotonic() < end, (self.messages, self.error)
            time.sleep(.01)
        return self.messages

    def close(self):
        with contextlib.suppress(OSError):
            import socket

            sock = self.conn.sock or (self.response.fp.raw._sock if self.response.fp else None)
            if sock:
                sock.shutdown(socket.SHUT_RDWR)
        self.response.close()
        self.conn.close()
        self.thread.join(2)
        target = self.fixture.root / f"stream-{time.time_ns()}.json"
        target.write_text(json.dumps({"headers": self.headers, "raw": self.raw, "messages": self.messages}, indent=2))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def replay(f):
    import runpy

    from arete import accounts, config, factory, gh, paths, replay as runner_replay, sandbox, state
    from tests import offline_workflow

    fx = SimpleNamespace(root=f.root, scenario="pass", replay=runner_replay, paths=paths, sandbox=sandbox)
    (f.root / "authority-parent").mkdir(exist_ok=True)
    offline_workflow.redirect_authority(fx)
    offline_workflow.create_replay_config(fx)
    task = state.task_add("replay-sample", "Add kumquat to the fixture notes", "Add kumquat", "feat")
    feature = runpy.run_path(str(paths.ADWS_DIR / "feature.py"))["feature"]
    with mock.patch.object(accounts, "read", offline_workflow.forbidden), mock.patch.object(gh, "gh", offline_workflow.forbidden):
        with contextlib.suppress(SystemExit):
            factory.main(feature, ["replay-sample", task, "--force"])
    assert config.load().replay.all
    run = state.ticket_runs("replay-sample", task, whole=True)[0]
    assert run["status"] == "done", run
    return run


def live_replay(f, nearby=False):
    from arete import run_lines

    written, write = [], run_lines.write

    def timed(*args, **kw):
        at = time.time()
        line = write(*args, **kw)
        written.append((line, at))
        return line

    with Stream(f) as stream:
        time.sleep(.3)
        with mock.patch.object(run_lines, "write", timed):
            run = replay(f)
        stream.wait(lambda rows: any(m["data"].get("kind") == "run.ended" for m in rows))
        messages = [m for m in stream.messages if m["data"].get("data", {}).get("run") == run["id"]]
        rows = [m["data"] for m in messages]
        assert rows[0]["kind"] == "run.started" and rows[-1]["kind"] == "run.ended", rows
        assert rows[0]["actor"] == f.person
        seqs = [r["seq"] for r in rows]
        assert seqs == sorted(set(seqs))
        lines = run_lines.read(run["id"])
        (f.root / "run-lines.json").write_text(json.dumps(lines, indent=2))
        steps = [line for line in lines if line["event"] in ("step_start", "step_end")]
        emitted = [m for m in messages if m["data"]["kind"].startswith("step.")]
        assert len(steps) == len(emitted), (steps, emitted)
        for line, message in zip(steps, emitted):
            event = message["data"]
            assert event["subject"] == f"step:{run['id']}/{line['step']}/{line.get('try', 1)}"
            assert event["actor"] == f"run:{run['id']}/{line['step']}"
            assert event["ver"] is None
            at = next(at for raw, at in written if raw["run"] == run["id"] and raw["seq"] == line["seq"])
            assert message["at"] - at < 1
        for row in rows:
            assert row["id"] == f"{row['epoch']}.{row['seq']}" and row["actor"] != "owner" and "@" not in row["actor"]
        assert rows[0]["ver"] is None
        if nearby:
            conn, response = f.request(f"/api/runs/{run['id']}/events")
            old = [json.loads(line) for line in response.read().splitlines()]
            conn.close()
            assert [line["event"] for line in old] == [line["event"] for line in lines]
            assert f.get(f"/api/runs/{run['id']}")
            assert any(row["kind"] == "end" for row in f.state.feed(run["id"]))
            assert f.get("/api/live")
    return rows


def reconnect_formats(f):
    with Stream(f, step_limit=2) as stream:
        time.sleep(.3)
        replay(f)
        assert stream.done.wait(15)
        second = [m["data"] for m in stream.messages if m["data"]["kind"].startswith("step.")][1]
        since = second["id"]
    later = f.poll(since)["events"]
    answer = f.poll(since)
    assert [r["seq"] for r in answer["events"]] == [r["seq"] for r in later]
    with Stream(f, "/api/events?since=999999&named=1", headers={"Last-Event-ID": since}) as stream:
        stream.wait(lambda ms: len(ms) >= len(later))
        assert [m["data"]["seq"] for m in stream.messages] == [r["seq"] for r in later]
        assert all(m["event"] == m["data"]["kind"] for m in stream.messages)
        assert stream.raw[0]["line"] == "retry: 2000\n"
        for key, value in (("Cache-Control", "no-store"), ("X-Accel-Buffering", "no"), ("Connection", "close")):
            assert stream.headers[key] == value
        assert stream.headers["Arete-Instance"] and stream.headers["X-Arete-Code"]
    with Stream(f, "/api/v1/events?since=" + since, "application/x-ndjson") as stream:
        stream.wait(lambda ms: len(ms) >= len(later))
        assert [m["data"]["seq"] for m in stream.messages] == [r["seq"] for r in later]
        assert all(json.loads(line["line"]) for line in stream.raw)
    assert [r["seq"] for r in f.poll(str(second["seq"]))["events"]] == [r["seq"] for r in later]
    before = time.monotonic()
    answer = f.get("/api/events?timeout=.1")
    assert not answer["events"] and time.monotonic() - before < 2
    before = time.monotonic()
    answer = f.get("/api/events?timeout=999")
    assert not answer["events"] and 24 <= time.monotonic() - before < 28
    with Stream(f) as stream:
        time.sleep(.3)
        seq = f.emit()
        stream.wait(lambda ms: bool(ms))
        assert [m["data"]["seq"] for m in stream.messages] == [seq]
    for query in ("since=garbage", "class=wrong", "timeout=-1", "timeout=nan"):
        f.get("/api/events?" + query, status=400)


def filters(f):
    from arete import events

    start = f.poll()["next"]
    with f.state.tx():
        for n in range(1100):
            events.emit("task.changed" if n % 2 else "run.changed", "task:142" if n % 3 else "run:pip-1",
                        cls="derived" if n % 5 else "durable", task="142", data={"run": "pip-1", "n": n})
        for run in ("pip-1", "pip-2"):
            events.emit("turn.appended", f"run:{run}", cls="derived", data={"run": run})
    original_reader, launched, workers, pages = f.state.reader, threading.Event(), [], []

    @contextlib.contextmanager
    def traced(*args, **kw):
        with original_reader(*args, **kw) as c:
            if kw.get("streaming"):
                def trace(sql):
                    if "SELECT e.*" in sql:
                        pages.append(sql)
                        if not launched.is_set():
                            launched.set()
                            worker = threading.Thread(target=f.emit)
                            workers.append(worker)
                            worker.start()
                            if not f.paths.ISOLATED:
                                worker.join(5)

                c.set_trace_callback(trace)
            yield c

    with mock.patch.object(f.state, "reader", traced):
        first = f.poll(start)
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    all_rows = first["events"]
    assert len(pages) >= 3 and launched.is_set()
    later = f.poll(first["next"])["events"]
    assert len(later) == 1 and later[0]["kind"] == "fixture.changed"
    assert len(all_rows) == 1100
    assert len({r["seq"] for r in all_rows}) == 1100
    assert len(f.poll(start, kinds="task.*")["events"]) == 550
    assert len(f.poll(start, kinds="task.changed")["events"]) == 550
    assert len(f.poll(start, subject="task:142,run:pip-1")["events"]) == 1101
    assert len(f.poll(start, task="142", run="pip-1")["events"]) == 1100
    assert len(f.poll(start, turns="pip-1")["events"]) == 1102
    assert len(f.poll(start, kinds="turn.*", turns="pip-2")["events"]) == 1
    assert f.poll(start, kinds="turn.*")["events"] == []
    assert all(r["class"] == "durable" and r["kind"] == "task.changed" for r in
               f.poll(start, kinds="task.*", **{"class": "durable"})["events"])
    assert f.poll(start, subject="task:14_")["events"] == []
    assert f.poll(start, kinds="t%.*")["events"] == []
    seq = f.emit()
    with f.state.tx() as c:
        c.execute("UPDATE events_log SET idem = 'fixture-key' WHERE seq = ?", (seq,))
    assert [r["seq"] for r in f.poll(start, idem="fixture-key")["events"]] == [seq]
    empty = f.poll(start, kinds="absent")
    assert empty["next"] == f.poll()["next"] and empty["events"] == []
    with Stream(f, "/api/events?kinds=fixture.*") as stream:
        time.sleep(.3)
        for _ in range(4):
            f.emit()
            time.sleep(.05)
        stream.wait(lambda ms: len(ms) == 4)
        assert len({m["data"]["seq"] for m in stream.messages}) == 4
    for query in ("since=broken", "class=unknown", "timeout=nan", "timeout=-1"):
        assert "error" in f.get("/api/events?" + query, status=400)


def reset(f, since, reason, extra=""):
    answer = f.get(f"/api/events?since={since}&timeout=0{extra}")
    assert answer["events"] == [] and answer["reset"]["reason"] == reason, answer
    for accept in ("text/event-stream", "application/x-ndjson"):
        with Stream(f, f"/api/events?since={since}{extra}", accept) as stream:
            assert stream.done.wait(5)
            assert len(stream.messages) == 1
            message = stream.messages[0]
            assert message["data"]["reason"] == reason and "id" not in message and "id" not in message["data"]


def epoch_reset(f):
    from arete import backup, events

    before = f.poll()["next"]
    reset(f, "99999.0", "epoch")
    identity = f.rows("SELECT * FROM schema_meta WHERE key LIKE 'instance_%'")
    made = backup.snapshot("fixture")
    f.close()
    backup.restore(made)
    f.state.migrate()
    f.open()
    after = f.poll()["next"]
    assert events.cursor(after)[0] != events.cursor(before)[0]
    assert int(events.cursor(after)[0]) > int(events.cursor(before)[0])
    assert identity == f.rows("SELECT * FROM schema_meta WHERE key LIKE 'instance_%'")
    reset(f, before, "epoch")


def prune(f, fail=False):
    from arete import backup, serve

    server = serve.Server()
    try:
        with mock.patch.object(server, "copy_off_machine"), mock.patch.object(server, "collect_db"):
            if fail:
                with mock.patch.object(backup, "snapshot", side_effect=OSError("fixture backup failed")):
                    server.prune()
            else:
                server.prune()
    finally:
        server.pool.shutdown()
        for pool in server.job_pools.values():
            pool.shutdown()


def retention_reset(f):
    from arete import util

    before = f.poll()["next"]
    old = {cls: f.emit(cls=cls) for cls in ("derived", "notice", "durable")}
    with f.state.tx() as c:
        for cls, days in (("derived", 91), ("notice", 8), ("durable", 100)):
            c.execute("UPDATE events_log SET at = ? WHERE seq = ?", (util.iso(util.now() - timedelta(days=days)), old[cls]))
    saved = f.rows()
    prune(f, True)
    assert saved == f.rows()
    prune(f)
    reset(f, before, "too_old")
    answer = f.poll(before, **{"class": "durable"})
    assert [r["seq"] for r in answer["events"]] == [old["durable"]]
    marks = f.get("/api/meta")["stream"]["trim"]
    assert marks["derived"].endswith("." + str(old["derived"]))
    assert marks["notice"].endswith("." + str(old["notice"]))
    prune(f)
    assert marks == f.get("/api/meta")["stream"]["trim"]


def backlog_reset(f):
    from arete import events

    start = f.poll()["next"]
    with f.state.tx():
        for _ in range(10000):
            events.emit("fixture.changed", "task:142", cls="derived", data={})
    assert len(f.poll(start)["events"]) == 10000
    f.emit()
    reset(f, start, "too_far")
    assert f.poll(start, kinds="absent")["events"] == []
    assert f.rows("SELECT * FROM events_log WHERE kind IN ('reset', 'serve.beat')") == []


def raises(call, kind=RuntimeError, text=None):
    try:
        call()
    except kind as e:
        if text:
            assert text in str(e), e
    else:
        raise AssertionError("expected exception")


def atomicity(f):
    from arete import events, principals, record

    before = f.rows()
    with mock.patch.object(events, "emit", side_effect=RuntimeError("emit fault")):
        raises(lambda: record.start("fixture", run_id="rollback-start"))
    assert f.rows("SELECT * FROM runs WHERE id = 'rollback-start'") == [] and before == f.rows()
    run = record.start("fixture")
    before = f.rows()
    with mock.patch.object(events, "emit", side_effect=RuntimeError("emit fault")):
        raises(lambda: record.settle(run, "done", "fixture end"))
    assert f.state.run_get(run)["status"] == "running" and not f.state.feed(run)
    assert before == f.rows()
    raises(lambda: events.emit("test", "test", cls="durable", data={}))
    with f.state.reader():
        raises(lambda: events.emit("test", "test", cls="durable", data={}))
    for actor in ("owner", "x@example.com"):
        with principals.acting(actor), f.state.tx():
            raises(lambda: events.emit("test", "test", cls="durable", data={}), ValueError)
    record.ask_stop(run)
    record.ask_stop(run)
    assert len(f.rows("SELECT * FROM events_log WHERE kind = 'run.changed'")) == 1
    record.settle(run, "done", "fixture end")
    record.settle(run, "done", "duplicate")
    record.ask_stop(run)
    record.ask_stop("missing")
    assert len(f.state.feed(run)) == 1
    assert len(f.rows("SELECT * FROM events_log WHERE kind = 'run.ended'")) == 1


def admission_seed(f):
    original = f.person
    assert re.fullmatch(r"u:[0-9a-f]{8}", original)
    assert f.get("/api/sidebar")
    f.who(SECOND)
    f.get("/api/sidebar", status=403)
    for inside in (False, True):
        f.paths.CONFIG_FILE.write_text(f'[dashboard]\nowner="{SECOND}"\n')
        port = f.server.server_port
        f.close()
        real_dashboard_restart(f, port, inside)
        f.open()
        f.server.inside_serve = inside
        f.get("/api/sidebar", status=403)
        f.who(LOGIN)
        assert f.get("/api/sidebar")
        assert f.principals.owner() == original
        f.who(SECOND)
    f.who(LOGIN)
    person = f.get("/api/v1/principals/me")
    assert person["id"] == original and person["kind"] == "human" and person["role"] == "owner"
    assert person["name"] == "Fixture Person" and person["subject"] == f"principal:{original}"
    assert f.get("/api/v1/principals?kind=human&role=owner&status=active") == [person]
    assert f.get("/api/v1/principals?kind=rule") == []
    assert "@" not in json.dumps(person)
    f.get("/api/v1/principals/missing", status=404)
    f.get("/api/sidebar", status=421, headers={"Host": "invalid.test"})
    conn, response = f.request("/api/tasks", method="POST", body="{}")
    assert response.status == 403
    response.read()
    conn.close()
    f.principals.update(original, status="removed")
    f.get("/api/sidebar", status=403)
    f.principals.update(original, status="active")
    f.principals.update(original, kind="rule", role=None)
    f.get("/api/sidebar", status=403)
    f.principals.update(original, kind="human", role="owner")
    seeds = f.rows("SELECT * FROM events_log WHERE kind = 'principal.changed' AND ver = 1")
    assert len(seeds) == 1 and seeds[0]["actor"] == "hand"
    assert set(json.loads(seeds[0]["data"])) == {"id", "kind", "role", "status"}
    assert f.host.seed_identity("") == (LOGIN, "Fixture Person")
    assert f.host.seed_identity("unlisted@example.test") == ("unlisted@example.test", "unlisted")
    exercise("admission-empty", f.root / "fresh")


def real_dashboard_restart(f, port, inside):
    _, process = launch("dashboard-embedded" if inside else "dashboard-standalone", f.root, port)
    try:
        end = time.monotonic() + 15
        while True:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/api/sidebar")
                response = conn.getresponse()
                response.read()
                conn.close()
                assert response.status == 403
                break
            except OSError:
                assert process.poll() is None and time.monotonic() < end
                time.sleep(.05)
        (f.root / "who").write_text(LOGIN)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/v1/principals/me")
        response = conn.getresponse()
        person = json.loads(response.read())
        conn.close()
        assert response.status == 200 and person["id"] == f.person, person
    finally:
        process.terminate()
        process.wait(10)
        (f.root / "who").write_text(SECOND)


def admission_empty(f):
    person = f.get("/api/v1/principals/me")
    assert person["name"] == "Fixture Person" and f.principals.contact(person["id"]) == LOGIN
    first = person["id"]
    f.close()
    f.open()
    assert f.get("/api/v1/principals/me")["id"] == first
    assert len(f.rows("SELECT * FROM events_log WHERE kind = 'principal.changed'")) == 1


def principal_gate(f):
    from arete import events, principals, util

    def insert(id="u:12345678", kind="human", role="owner", status="active"):
        with f.state.tx() as c:
            c.execute("INSERT INTO principals (id, kind, role, status, name, created) VALUES (?, ?, ?, ?, 'Other', ?)",
                      (id, kind, role, status, util.iso()))

    raises(insert, sqlite3.IntegrityError, "a second person needs multi_user = ready")
    insert(status="removed")
    raises(lambda: principals.update("u:12345678", status="active"), sqlite3.IntegrityError,
           "a second person needs multi_user = ready")
    insert("rule:fixture", "rule", None)
    raises(lambda: principals.update("rule:fixture", kind="human", role="owner"), sqlite3.IntegrityError)
    principals.update(f.person, name="Updated Person")
    raises(lambda: insert("bad", "human", None, "removed"), sqlite3.IntegrityError)
    with f.state.tx() as c:
        c.execute("INSERT INTO schema_meta VALUES ('multi_user', 'ready')")
    principals.update("u:12345678", status="active")
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _: principals.seed_owner(SECOND, "Second"), range(8)))
    assert set(ids) == {f.person}
    assert len(f.rows("SELECT * FROM events_log WHERE kind = 'principal.changed' AND ver = 1")) == 1
    with f.state.tx() as c:
        raises(lambda: c.execute("INSERT INTO principal_logins SELECT 'duplicate', principal, method, subject, "
                                 "public_key, expires, created, last_used FROM principal_logins"), sqlite3.IntegrityError)
        c.execute("DELETE FROM principal_logins")
        c.execute("DELETE FROM principals")
    before = f.rows()
    with mock.patch.object(events, "emit", side_effect=RuntimeError("seed fault")):
        raises(lambda: principals.seed_owner(LOGIN))
    assert f.rows("SELECT * FROM principals") == [] and before == f.rows()
    f.person = principals.seed_owner(LOGIN, "Fixture Person")


def completeness(f):
    from arete import state

    identity = f.get("/api/meta")["instance"]
    for value, expected in (("4", False), ("5", True), (None, True)):
        with state.tx() as c:
            c.execute("UPDATE schema_meta SET value = ? WHERE key = 'oldest_writer'", (value,))
        answer = f.get("/api/meta")
        assert answer["stream"]["complete"] is expected and answer["instance"] == identity
        epoch = answer["stream"]["head"].split(".")[0]
        assert all(mark.startswith(epoch + ".") for mark in answer["stream"]["trim"].values())
        f.emit()
    ledger = f.rows("SELECT * FROM schema_migrations WHERE n = 5")
    assert len(ledger) == 1 and ledger[0]["phase"] == "expand"


def row_actors(f):
    from arete import board, cli, control, factory, pipeline, principals, record, screening, serve, tasks, tracker, triage

    task = f.state.task_add("replay-sample", "Question", "Question", "feat")
    with principals.acting(f.person):
        assert cli.main(["task", "reply", task, "An answer"]) == 0
        tasks.store_answer(None, None, task, "A stored answer")
    repo = SimpleNamespace(name="replay-sample")
    row = {"ref": "FIX-1", "source": "fixture", "tracker": "jira", "repo": repo.name, "route": "",
           "title": "Fixture ticket", "ready": True, "code": 1, "kind": "feat", "updated": "unchanged",
           "questions": [], "assumptions": ["A harmless assumption"], "plan": [], "checks": []}
    approved = []
    source = SimpleNamespace(name="fixture", by_name=lambda: {repo.name: repo}, route=lambda name: "",
                             candidate=lambda r: r, ticket=lambda r: SimpleNamespace(updated="unchanged", text="A task"),
                             approve=lambda r: approved.append(r))
    f.state.triage_put(row)
    with principals.acting(f.person), mock.patch.object(triage, "sources", return_value=[source]), \
            mock.patch.object(screening, "screen", return_value=screening.Screen(screening.PASSED)):
        assert "Sent FIX-1" in triage.send(None, "FIX-1", [], [], repo.name)
    assert len(approved) == 1
    approval = f.state.triage_approval("FIX-1")
    assert approval["sent_by"] == f.person
    legacy = (f.root / "legacy-task.txt").read_text()
    for actor in (f.person, "owner"):
        checked_task = legacy if actor == "owner" else task
        f.state.task_set_state(checked_task, "needs-info")
        assert checked_task in [r["id"] for r in f.state.tasks_replied(repo.name)]
        assert tracker.LocalTracker(repo).reply_since(checked_task, "2000-01-01T00:00:00Z")
        historical = {**approval, "sent_by": actor, "run": None}
        assert "by you" in triage.sent_note(historical)
        assert "Sent by its owner" in pipeline.triage_notes(historical)
        assert "Sent from the triage board" in pipeline.triage_block(historical)
    assert board.decisions("2000-01-01T00:00:00Z")["tickets"]["owner"] == 1
    authors = f.rows("SELECT * FROM task_comments")
    assert len(authors) == 3 and all(r["author"] == f.person for r in authors if r["task"] == task)
    assert principals.is_person("owner") and principals.is_person(f.person)
    with principals.acting("rule:fixture"):
        child = control.launch([sys.executable, "-c", "import os; print(os.environ['ARETE_ACTOR'])"], "actor.txt")
        assert child.wait(timeout=10) == 0
    assert (f.paths.LOG_DIR / "actor.txt").read_text().strip() == "rule:fixture"
    with mock.patch.dict(os.environ, {"ARETE_ACTOR": "rule:override"}):
        assert principals.cli_actor() == "rule:override"
    with principals.acting(f.person):
        run = record.start("fixture")
    event = f.rows("SELECT * FROM events_log WHERE subject = ?", (f"run:{run}",))[0]
    principals.update(f.person, name="New Name")
    sent = f.poll(str(event["seq"] - 1))["events"][0]
    assert sent["actor_name"] == "New Name" and "New Name" not in event["data"]
    record.settle(run, "done")
    server = serve.Server()
    try:
        observed = []
        server.guard("loop:aaa", lambda: observed.append(principals.current()))
        server.guard("stage:fixture", lambda: observed.append(principals.current()))
        with mock.patch.object(server, "dispatch_queued_as_rule", side_effect=lambda *a: observed.append(principals.current())):
            server.dispatch_queued({"loop": "bbb"}, {}, None)
        with mock.patch.object(server, "poll_source_as_rule", side_effect=lambda *a: observed.append(principals.current())):
            server.pool.submit(server.poll_source, SimpleNamespace(name="ccc"), 0).result(5)
        assert observed == ["rule:aaa", "hand", "rule:bbb", "rule:ccc"], observed
        with principals.acting(f.person), mock.patch.object(factory.Flow, "run_as_step", return_value=factory.Outcome("pass")) as step:
            flow = object.__new__(factory.Flow)
            flow.run_id = "fixture-run"
            flow.run("fixture", (), {})
            assert step.called and principals.current() == f.person
    finally:
        server.pool.shutdown()
        for pool in server.job_pools.values():
            pool.shutdown()
    assert all(r["actor"] != "owner" and "@" not in r["actor"] for r in f.rows())


def beats(f):
    from arete import control, events, loops, serve

    loops.SWITCHES_FILE.write_text(json.dumps({p.name: False for p in loops.BUNDLED_LOOPS_DIR.iterdir() if p.is_dir()}))
    folder = f.paths.CONFIG_DIR / "loops/aaa"
    folder.mkdir(parents=True)
    (folder / "loop.toml").write_text('kind="command"\n[run]\ncommand="true"\n')
    server = serve.Server()
    release, blocked, stop = threading.Event(), threading.Event(), threading.Event()
    tick_thread = traffic = None
    scanned = threading.local()
    expected_ids = []
    scan, serve_beat = events.scan, control.serve_beat

    def observed_scan(*args):
        result = scan(*args)
        scanned.cursor = f"{result[0]}.{result[1]}"
        return result

    def observed_beat():
        expected_ids.append(scanned.cursor)
        return serve_beat()

    try:
        def broken(loop):
            raise RuntimeError("fixture watch_metric")

        server.pruned = server.swept = server.rotated = server.checked_closed = time.monotonic()
        with mock.patch.object(server, "poll", broken), mock.patch.object(server, "load_new_code"):
            server.run_pass()
        with mock.patch.object(events, "scan", observed_scan), \
                mock.patch.object(control, "serve_beat", observed_beat), \
                Stream(f, "/api/events?kinds=absent") as stream:
            stream.wait(lambda ms: any(m["data"].get("kind") == "serve.beat" for m in ms), 40)
            first = stream.messages[-1]
            assert first["data"]["tick_ok"] is False
            assert "loop:aaa" in first["data"]["failing"]
            assert first["id"] == expected_ids[0]

            def block():
                blocked.set()
                assert release.wait(60)

            def writes():
                while not stop.wait(.1):
                    f.emit()

            with mock.patch.object(server, "tick", block), mock.patch.object(server, "load_new_code"):
                tick_thread = threading.Thread(target=server.run_pass)
                tick_thread.start()
                assert blocked.wait(5)
                traffic = threading.Thread(target=writes)
                traffic.start()
                stream.wait(lambda ms: len(ms) >= 3, 40)
                second, third = stream.messages[1:3]
                assert first["data"]["at"] == second["data"]["at"] == third["data"]["at"]
                assert 14 <= third["at"] - second["at"] <= 17
                assert [m["id"] for m in stream.messages[:3]] == expected_ids[:3]
                assert int(third["id"].split(".")[1]) > int(first["id"].split(".")[1])
                assert int(third["id"].split(".")[1]) <= int(third["data"]["head"].split(".")[1])
                stop.set()
                traffic.join(5)
                release.set()
                tick_thread.join(5)
        assert f.get("/api/sidebar")
        assert f.rows("SELECT * FROM events_log WHERE kind = 'serve.beat'") == []
        blocked_socket(f, events)
    finally:
        release.set()
        stop.set()
        for thread in (tick_thread, traffic):
            if thread:
                thread.join(5)
        server.pool.shutdown()
        for pool in server.job_pools.values():
            pool.shutdown()


def blocked_socket(f, events):
    import socket

    opened, closed = threading.Event(), threading.Event()
    times = {}
    original = f.state.reader

    @contextlib.contextmanager
    def tracked(*args, **kw):
        try:
            with original(*args, **kw) as c:
                if kw.get("streaming"):
                    times["opened"] = time.monotonic()
                    opened.set()
                yield c
        finally:
            if kw.get("streaming"):
                times["closed"] = time.monotonic()
                closed.set()

    since = f.poll()["next"]
    with f.state.tx():
        for _ in range(128):
            events.emit("socket.fixture", "socket:fixture", cls="derived", data={"blob": "x" * 65536})
    conn = http.client.HTTPConnection("127.0.0.1", f.server.server_port, timeout=40)
    conn.connect()
    conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    try:
        with mock.patch.object(f.state, "reader", tracked):
            conn.request("GET", "/api/events?since=" + since, headers={"Accept": "text/event-stream"})
            response = conn.getresponse()
            assert response.status == 200 and opened.wait(5)
            assert f.get("/api/sidebar")
            run = replay(f)
            assert run["status"] == "done"
            assert closed.wait(40), "blocked stream did not release its reader"
            assert 29 <= times["closed"] - times["opened"] < 40, times
            (f.root / "blocked-socket.json").write_text(json.dumps(times))
    finally:
        conn.close()


def child(scenario, root, port):
    if scenario.startswith("dashboard-"):
        from arete import dashboard, tailnet

        tailnet.WHOIS_SECONDS = 0
        return dashboard.serve(port=port, inside_serve=scenario == "dashboard-embedded")
    if scenario == "admission-empty":
        (root / "config/config.toml").write_text('[dashboard]\nowner=""\n')
    if scenario == "row-actors":
        from arete import state
        from arete.store import db as foundation, migrations

        chain = migrations.load()
        with mock.patch.object(migrations, "load", return_value=chain[:4]), \
                mock.patch.multiple(foundation, LATEST=4, NEEDED=4):
            state.migrate()
            legacy = state.task_add("replay-sample", "A legacy question", "Legacy", "feat")
            state.task_set_state(legacy, "needs-info")
            state.task_comment(legacy, "owner", "A legacy answer")
            (root / "legacy-task.txt").write_text(legacy)
    fixture = Fixture(root, port)
    try:
        if scenario == "serve":
            from arete import replay as runner_replay
            from tests import offline_workflow

            offline_workflow.create_replay_config(SimpleNamespace(root=root, scenario="pass", replay=runner_replay,
                                                                 paths=fixture.paths))
            print(f"Serving http://127.0.0.1:{fixture.server.server_port}; evidence: {root}", flush=True)
            fixture.close()
            return fixture.dashboard.serve(port=port, host=fixture.host)
        actions = {"live-replay": live_replay, "reconnect-formats": reconnect_formats, "filters": filters,
                   "epoch-reset": epoch_reset, "retention-reset": retention_reset, "backlog-reset": backlog_reset,
                   "atomicity": atomicity, "admission-seed": admission_seed, "principal-gate": principal_gate,
                   "row-actors": row_actors, "completeness": completeness, "beats": beats,
                   "admission-empty": lambda f: admission_empty(f),
                   "nearby": lambda f: live_replay(f, nearby=True)}
        actions[scenario](fixture)
        fixture.save()
        print(f"Passed {scenario}; evidence: {root}", flush=True)
    finally:
        fixture.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("serve", "exercise", "inspect"))
    parser.add_argument("exercise", nargs="?", choices=EXERCISES)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--child", choices=(*EXERCISES, "serve", "admission-empty", "dashboard-embedded", "dashboard-standalone"))
    args = parser.parse_args()
    if args.child:
        child(args.child, args.root, args.port)
    elif args.mode == "exercise":
        if not args.exercise:
            parser.error("exercise needs a scenario")
        exercise(args.exercise, args.root)
    elif args.mode == "serve":
        root, process = launch("serve", args.root, args.port)
        print(f"http://127.0.0.1:{args.port}; evidence: {root}", flush=True)
        try:
            raise SystemExit(process.wait())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
    elif args.mode == "inspect":
        roots = [args.root] if args.root else sorted(Path(tempfile.gettempdir()).glob("arete-events-*"))
        for root in roots:
            print(root)
            for file in sorted(root.glob("*.json*")):
                print(f"  {file.name}: {file.stat().st_size} bytes")
            for file in sorted(root.glob("stdout-*.txt")):
                print(file.read_text()[-1000:])
    else:
        parser.error("choose serve, exercise or inspect")
