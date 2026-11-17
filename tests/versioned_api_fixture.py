from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.events_principals_fixture import Fixture as EventsFixture, isolated, live_replay
from tests.harness import SOURCE

EXERCISES = ("migration-rollback", "writes", "stale-stop", "concurrent-stop", "run-commands", "grace",
             "status-races", "ask-races", "carried-landings", "clients", "nearby", "keyed-replay",
             "keyed-concurrency", "keyed-clients", "keyed-recovery", "slow-start", "launch-boundaries", "idempotency-prune")


def forbidden(*args, **kwargs):
    raise AssertionError("external transport or credential lookup forbidden")


def registration_command(repo, tid, ready, *, gate=None, finish=False, fields=None, stop=None):
    code = ("import json,sys,time\n"
            f"sys.path.insert(0, {str(SOURCE)!r})\n"
            "from pathlib import Path\n"
            "from arete import record,util\n"
            "from arete.store import runs\n"
            "repo,tid,gate,ready,finish,fields,stop=json.loads(sys.argv[1])\n"
            "deadline=time.monotonic()+30\n"
            "def stopping():\n"
            " return time.monotonic()>deadline or (stop and Path(stop).exists())\n"
            "while gate and not Path(gate).exists():\n"
            " if stopping(): sys.exit(0)\n"
            " time.sleep(.01)\n"
            "with util.lock(runs.run_lock('pipeline',repo,tid),wait=True):\n"
            " run=record.start('pipeline',repo=repo,issue=tid,**fields)\n"
            " Path(ready).write_text(json.dumps({'run':run}))\n"
            " while not finish and not stopping(): time.sleep(.01)\n")
    return [sys.executable, "-I", "-c", code,
            json.dumps([repo, str(tid), str(gate) if gate else "", str(ready), finish, fields or {},
                        str(stop) if stop else ""])]


def launch(name, root=None, port=0):
    root = Path(root) if root else Path(tempfile.mkdtemp(prefix="arete-versioned-api-"))
    env = isolated(root)
    command = [sys.executable, "-I", str(SOURCE / "docs/versioned-api-fixture.py"), "--child", name,
               "--root", str(root), "--port", str(port)]
    print(f"Evidence: {root}", flush=True)
    return root, subprocess.Popen(command, cwd=SOURCE, env=env, text=True,
                                  stdout=(root / "stdout.txt").open("w"), stderr=(root / "stderr.txt").open("w"))


def exercise(name, root=None):
    root, proc = launch(name, root)
    try:
        code = proc.wait(timeout=240)
        assert code == 0, f"{root}\n{(root / 'stdout.txt').read_text()}\n{(root / 'stderr.txt').read_text()}"
        result = json.loads((root / "result.json").read_text())
        assert result["exercise"] == name and result["passed"]
        return root
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


class Fixture(EventsFixture):
    def __init__(self, root, port=0):
        from arete import control

        self.children = []
        self.evidence_lock = threading.Lock()
        self.launch_process = control.launch
        super().__init__(root, port)

    def open(self, port=0):
        from arete import dashboard, record
        from arete.store import runs

        fixture = self

        class EvidenceHandler(dashboard.Handler):
            def answer(self, method):
                self.evidence_headers, self.evidence_body = {}, None
                return super().answer(method)

            def raw_body(self, *args, **kwargs):
                raw = super().raw_body(*args, **kwargs)
                self.evidence_body = json.loads(raw or b"{}")
                return raw

            def send_header(self, name, value):
                if hasattr(self, "evidence_headers"):
                    self.evidence_headers[name] = value
                return super().send_header(name, value)

            def api(self, method, path, query):
                if method == "POST" and path.startswith("/api/fixture/stale/"):
                    run = path.removeprefix("/api/fixture/stale/")
                    record.note(run, summary="fixture advanced the displayed run")
                    return runs.run_get(run)
                return super().api(method, path, query)

            def send(self, status, payload, kind, cache, retry_after=0):
                super().send(status, payload, kind, cache, retry_after)
                if kind == "application/json":
                    evidence = {"path": self.path, "request": self.evidence_body, "status": status,
                                "headers": self.evidence_headers, "body": json.loads(payload), "raw": payload.hex(),
                                "key": self.headers.get("Idempotency-Key")}
                    with fixture.evidence_lock, (fixture.root / "http.jsonl").open("a") as out:
                        out.write(json.dumps(evidence) + "\n")

        with mock.patch.object(dashboard, "Handler", EvidenceHandler):
            super().open(port)

    def sample(self):
        from arete import replay
        from tests import offline_workflow

        offline_workflow.create_replay_config(SimpleNamespace(root=self.root, scenario="pass", replay=replay,
                                                             paths=self.paths))

    def post(self, path, body=None, key=None):
        headers = {"X-Arete": "1", "Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        conn, response = self.request(path, method="POST", headers=headers,
                                      body=body if isinstance(body, str) else json.dumps(body or {}))
        raw = response.read()
        record = {"path": path, "request": body, "status": response.status,
                  "headers": dict(response.getheaders()), "body": json.loads(raw), "raw": raw.hex()}
        with self.transcript.open("a") as out:
            out.write(json.dumps(record) + "\n")
        conn.close()
        return record

    def made(self, tag, status="failed", *, live=False, **fields):
        from arete import record, run_lines, util

        tid = self.tasks.task_add("replay-sample", "Add kumquat", tag)
        pid = 0
        if live:
            child = subprocess.Popen(["sleep", "30"], start_new_session=True)
            self.children.append(child)
            pid = child.pid
        run = record.start("pipeline", tag=tag, repo="replay-sample", issue=tid, pid=pid, **fields)
        run_lines.write(run, "task", path="feature", title=tag)
        run_lines.write(run, "step_start", step="implement", try_no=1)
        run_lines.write(run, "step_end", step="implement", result="fail", data={})
        if status != "running":
            record.settle(run, status, end_line=status)
            run_lines.write(run, "report", status=status, review={"verdict": "request_changes", "problems": [
                {"text": "Fix the fixture", "file": "notes.txt", "category": "issue"}]})
        folder = self.paths.RUNS_DIR / run
        (folder / "plan.md").write_text("Add kumquat to notes.txt")
        assert util.iso()
        return run

    def fake_launch(self, command, *args, **kw):
        old = next((r for r in self.rows("SELECT * FROM runs") if r["id"] in command), None)
        assert old, command
        return self.fake_spawn(old["repo"], old["issue"])

    def fake_spawn(self, repo, tid, force=False):
        child, ready = self.pipeline_child(repo, tid)
        self.wait_registration(child, ready)
        return child

    def pipeline_child(self, repo, tid, *, gate=None, finish=False, fields=None):
        ready = self.root / f"registration-{os.urandom(8).hex()}.json"
        child = self.launch_process(registration_command(repo, tid, ready, gate=gate, finish=finish, fields=fields),
                                    "fixture-children.log")
        self.children.append(child)
        return child, ready

    def wait_registration(self, child, ready):
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), (child.poll(), ready)
        return json.loads(ready.read_text())["run"]

    def close(self):
        super().close()
        for proc in self.children:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)

    def save(self):
        super().save()
        tables = ("runs", "tasks", "task_comments", "events_log", "schema_migrations", "schema_meta", "idempotency")
        (self.root / "canonical.json").write_text(json.dumps({t: self.rows(f"SELECT rowid, * FROM {t}")
                                                            for t in tables}, indent=2,
                                                           default=lambda v: {"bytes_hex": v.hex()}))


def refuses(kind, action):
    try:
        action()
    except kind as e:
        return e
    raise AssertionError(f"expected {kind.__name__}")


def migration(root):
    from dataclasses import replace

    from arete.store import db, migrations

    # Exercise migration 9 against its fixed predecessor even when newer
    # migrations are present in the checkout.
    chain = [m for m in migrations.load() if m.n <= 9]
    assert [m.n for m in chain] == list(range(1, 10))
    assert chain[-1].name == "m0009_foundation_versioned_api.py"
    with mock.patch.object(migrations, "load", return_value=chain[:-1]), \
            mock.patch.multiple(db, LATEST=8, NEEDED=8):
        db.migrate()
        with db.tx() as c:
            c.execute("INSERT INTO runs (id, kind, status, started) VALUES ('legacy', 'loop', 'done', ?)",
                      ("2026-01-01T00:00:00Z",))
            c.execute("INSERT INTO tasks (id, repo, body, created, updated) VALUES ('t-old', 'replay-sample', 'old', ?, ?)",
                      ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            c.execute("INSERT INTO task_comments (task, author, body, ts) VALUES ('t-old', 'owner', 'old', ?)",
                      ("2026-01-01T00:00:00Z",))
    with db.reader() as c:
        ledger = [tuple(r) for r in c.execute("SELECT n, sha256 FROM schema_migrations ORDER BY n")]
        old = {t: [dict(r) for r in c.execute(f"SELECT rowid, * FROM {t}")]
               for t in ("runs", "tasks", "task_comments")}

    def broken(c):
        chain[-1].apply(c)
        raise RuntimeError("injected migration failure")

    with mock.patch.object(migrations, "load", return_value=[*chain[:-1], replace(chain[-1], apply=broken)]):
        refuses(migrations.Refused, db.migrate)
    with db.reader() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 8
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'idempotency'").fetchone()
        assert all("version" not in {r[1] for r in c.execute(f"PRAGMA table_info({t})")} for t in old)
        assert [tuple(r) for r in c.execute("SELECT n, sha256 FROM schema_migrations ORDER BY n")] == ledger
    with mock.patch.object(migrations, "load", return_value=chain), mock.patch.multiple(db, LATEST=9, NEEDED=9):
        db.migrate()
        with db.reader() as c:
            applied = [tuple(r) for r in c.execute("SELECT * FROM schema_migrations ORDER BY n")]
            upgraded_schema = [tuple(r) for r in c.execute("SELECT * FROM sqlite_master ORDER BY name")]
        db.migrate()
        with db.reader() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 9
            assert [tuple(r) for r in c.execute("SELECT * FROM schema_migrations ORDER BY n")] == applied
            assert [tuple(r) for r in c.execute("SELECT * FROM sqlite_master ORDER BY name")] == upgraded_schema
    with db.reader() as c:
        for table, rows in old.items():
            upgraded = [dict(r) for r in c.execute(f"SELECT rowid, * FROM {table}")]
            assert upgraded == [{**r, "version": 1} for r in rows], upgraded
        assert [tuple(r) for r in c.execute("SELECT n, sha256 FROM schema_migrations WHERE n < 9 ORDER BY n")] == ledger
        assert c.execute("SELECT COUNT(*) FROM events_log").fetchone()[0] == 0
        schema = {t: [tuple(r) for r in c.execute(f"PRAGMA table_info({t})")]
                  for t in ("idempotency", "events_log")}
        assert schema["idempotency"][0][1] == "actor" and schema["idempotency"][0][5] == 1
        assert schema["idempotency"][1][1] == "key" and schema["idempotency"][1][5] == 2
        assert next(r for r in schema["events_log"] if r[1] == "via")[3] == 0
    with mock.patch.multiple(db, LATEST=9, NEEDED=9), db.tx() as c:
        refuses(sqlite3.IntegrityError, lambda: c.execute(
            "INSERT INTO idempotency (actor,key,route,request_sha,status,response,at) VALUES ('hand','key','r','s',0,'','bad')"))
        refuses(sqlite3.IntegrityError, lambda: c.execute(
            "UPDATE schema_meta SET value = 'bad' WHERE key = 'versioned_api_grace_deadline'"))
    assert migrations.owners()["idempotency"] == {"idempotency": "30 days"}
    (root / "migration.json").write_text(json.dumps({"old": old, "ledger": ledger, "schema": schema,
                                                    "rollback": True}, indent=2))


def invariant(f):
    rows = f.rows("SELECT subject, ver, COUNT(*) AS n FROM events_log WHERE ver IS NOT NULL GROUP BY subject, ver")
    assert all(r["n"] == 1 for r in rows), rows
    for table, prefix, identity in (("runs", "run", "id"), ("tasks", "task", "id"),
                                     ("task_comments", "task_comment", "rowid")):
        for row in f.rows(f"SELECT rowid, * FROM {table}"):
            versions = [r["ver"] for r in rows if r["subject"] == f"{prefix}:{row[identity]}"]
            if versions:
                assert sorted(versions) == list(range(1, row["version"] + 1)), (row, versions)


def writes_exercise(f):
    from arete import agent, control, events, principals, record, store, util
    from arete.store import runs, tasks, writes

    run = f.made("writes", "running")
    tid = runs.run_get(run)["issue"]
    assert tasks.task_comment(tid, f.person, "A comment")
    comment = f.rows("SELECT rowid, * FROM task_comments WHERE task = ?", (tid,))[0]
    writes.update("task_comments", comment["rowid"], {"body": "Revised"}, base_version=1)
    assert tasks.task_comments(tid) == [{"author": f.person, "body": "Revised", "ts": comment["ts"]}]
    assert tasks.task_set_state(tid, "needs-info", from_states=("todo",))
    assert not tasks.task_set_state(tid, "done", from_states=("todo",))
    record.steps(run, "build", [(agent.Result(ok=True, cost_usd=2, tokens_in=4, tokens_out=3), 1),
                               (agent.Result(ok=True, cost_usd=3, tokens_in=5), 2)])
    record.correct(run, -1, "repriced")
    assert runs.run_get(run)["cost_usd"] == 4
    record.hold_ab(run, "build", "trial", "a", {"model": "fixture"})
    runs.checkpoint(run, 2, 1, 0, 1, util.iso())
    record.note(run, summary="checkpoint")
    before = runs.run_get(run)
    assert not record.move(run, "merged", expect=("pr",))
    refuses(writes.VersionConflict, lambda: record.note(run, base_version=1, summary="stale"))
    refuses(ValueError, lambda: record.note(run, invalid="no"))
    assert runs.run_get(run) == before
    before_steps, before_comments, before_events = runs.steps(run), tasks.task_comments(tid), f.rows()
    with f.db.tx(), mock.patch.object(events, "emit", side_effect=RuntimeError("event fault")):
        refuses(RuntimeError, lambda: record.note(run, summary="lost"))
        refuses(RuntimeError, lambda: record.step(run, "fault", agent.Result(ok=True, cost_usd=99), 1))
        refuses(RuntimeError, lambda: tasks.task_comment(tid, f.person, "lost"))
        refuses(RuntimeError, lambda: record.settle(run, "done", "lost end"))
    emitted = events.emit

    def comment_fault(kind, *args, **kwargs):
        if kind == "task_comment.created":
            raise RuntimeError("comment event fault")
        return emitted(kind, *args, **kwargs)

    with f.db.tx(), mock.patch.object(events, "emit", side_effect=comment_fault):
        refuses(RuntimeError, lambda: tasks.task_comment(tid, f.person, "lost after parent update"))
    seen = []

    def second_fault(kind, *args, **kwargs):
        seen.append(kind)
        if len(seen) == 2:
            raise RuntimeError("second metering event fault")
        return emitted(kind, *args, **kwargs)

    with f.db.tx(), mock.patch.object(events, "emit", side_effect=second_fault):
        refuses(RuntimeError, lambda: record.steps(run, "fault", [(agent.Result(ok=True, cost_usd=99), 1)] * 2))
    assert runs.run_get(run) == before
    assert runs.steps(run) == before_steps and tasks.task_comments(tid) == before_comments and f.rows() == before_events
    with events.request_scope("fixture A"):
        with f.db.tx():
            record.note(run, summary="committed")
            assert events.committed_receipt() is None and writes.receipt(f"run:{run}") is None
        receipt = events.committed_receipt()
        assert receipt == writes.receipt(f"run:{run}")
    with events.request_scope("fixture B"):
        record.note(run, summary="other request")
        assert events.committed_receipt() != receipt
    record.settle(run, "done", "end")
    assert runs.feed(run)[-1]["text"] == "end"
    assert record.settle(run, "failed", "duplicate") is None
    old = record.start("loop", loop_kind="command", started="2000-01-01T00:00:00Z")
    record.settle(old, "done", finished="2000-01-01T00:00:00Z")
    store.prune()
    assert runs.run_get(old) is None
    assert f.rows("SELECT * FROM events_log WHERE subject = ? AND kind = 'run.deleted'", (f"run:{old}",))
    via = f.rows("SELECT actor, via FROM events_log WHERE subject = ? AND via IS NOT NULL", (f"run:{run}",))
    assert {r["via"] for r in via} == {"fixture A", "fixture B"}
    assert all(r["actor"] == f.person for r in via)
    for actor in (f.person, "rule:fixture", "step:fixture:build"):
        with principals.acting(actor), events.request_scope(actor):
            record.note(run, summary=actor)
        last = f.rows("SELECT * FROM events_log WHERE subject = ? ORDER BY seq DESC LIMIT 1", (f"run:{run}",))[0]
        assert last["actor"] == last["via"] == actor
    with events.request_scope("POST /fixture/child"), principals.acting("rule:fixture"):
        child = control.launch([sys.executable, "-c", "from arete import principals, record\n"
                                "with principals.acting(principals.cli_actor()):\n"
                                " record.start('fixture', run_id='provenance-child')\n"], "provenance.log")
        f.children.append(child)
        assert child.wait(timeout=15) == 0
    child_event = f.rows("SELECT * FROM events_log WHERE subject = 'run:provenance-child'")[0]
    assert child_event["actor"] == "rule:fixture" and child_event["via"] == "POST /fixture/child"
    assert writes.receipt("run:provenance-child", 1)["ver"] == 1
    assert events.committed_receipt() is None
    invariant(f)


def stop_path(run):
    return f"/api/runs/{run}/stop"


def stale_stop(f):
    from arete import control, devices, record, tasks
    from arete.store import runs

    run = f.made("stale", "running", live=True)
    version = runs.run_get(run)["version"]
    record.note(run, summary="changed")
    canonical = runs.run_get(run)
    with mock.patch.object(control.os, "killpg") as killpg, mock.patch.object(control.os, "kill") as kill, \
            mock.patch.object(devices, "release") as release, mock.patch.object(tasks, "kept_folder_removed") as cleanup:
        response = f.post(stop_path(run), {"base_version": version})
        assert response["status"] == 409 and response["body"]["code"] == "version_conflict", response
        assert response["body"]["current"] == canonical
        assert f.children[-1].poll() is None and not canonical["stop_asked"]
        for value in (True, False, 0, -1, "1", 1.5, [], {}):
            assert f.post(stop_path(run), {"base_version": value})["status"] == 400
        assert f.post(stop_path("missing"), {"base_version": 1})["status"] == 404
        ended = f.made("ended")
        assert f.post(stop_path(ended), {"base_version": runs.run_get(ended)["version"]})["status"] == 400
        for effect in (killpg, kill, release, cleanup):
            effect.assert_not_called()
    assert runs.run_get(run) == canonical


def concurrent_stop(f):
    from arete import control, record
    from arete.store import runs, writes

    run = f.made("concurrent", "running", live=True)
    record.note(run, summary="ready for competing stops")
    version = runs.run_get(run)["version"]
    current, signals = writes.current, []
    paused = threading.Event()

    def read(c, table, identity):
        row = current(c, table, identity)
        if table == "runs" and identity == run and not paused.is_set():
            paused.set()
            time.sleep(.2)
        return row

    def signal_after_commit(pid, sig):
        persisted = runs.run_get(run)
        assert persisted["version"] == version + 1 and persisted["stop_asked"]
        signals.append((pid, sig))
        record.start("fixture", tag="unrelated")

    with mock.patch.object(writes, "current", side_effect=read), \
            mock.patch.object(control.os, "killpg", side_effect=signal_after_commit):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(f.post, stop_path(run), {"base_version": version})
            assert paused.wait(5)
            second = pool.submit(f.post, stop_path(run), {"base_version": version})
            responses = [first.result(10), second.result(10)]
    assert sorted(r["status"] for r in responses) == [200, 409], responses
    assert len(signals) == 1
    successful = next(r for r in responses if r["status"] == 200)
    assert set(successful["body"]) == {"id", "stopped", "pid"}
    receipt = writes.receipt(f"run:{run}", version + 1)
    assert successful["headers"]["Arete-Seq"] == receipt["event_seq"]
    assert len(f.rows("SELECT * FROM events_log WHERE subject = ? AND ver = ?", (f"run:{run}", version + 1))) == 1
    competitor = f.made("settlement", "running", live=True)
    old = runs.run_get(competitor)["version"]
    ask = record.ask_stop

    def settle_first(run_id, base_version=None):
        record.settle(run_id, "done")
        return ask(run_id, base_version)

    with mock.patch.object(record, "ask_stop", side_effect=settle_first), mock.patch.object(control.os, "killpg") as kill:
        response = f.post(stop_path(competitor), {"base_version": old})
        assert response["status"] == 409, response
        kill.assert_not_called()


def run_commands(f):
    from arete import control, pipeline, record, repairs, tasks
    from arete.store import runs

    for action in ("resume", "repair", "steps/implement"):
        for status in ("failed", "running"):
            run = f.made(action.split("/")[0], status)
            old = runs.run_get(run)["version"]
            record.note(run, summary="updated")
            before = runs.run_get(run)
            with mock.patch.object(record, "interrupt") as recover, mock.patch.object(control, "launch") as spawn, \
                    mock.patch.object(tasks, "kept_folder_removed") as cleanup, mock.patch.object(repairs, "open_heads") as heads:
                response = f.post(f"/api/runs/{run}/{action}", {"base_version": old})
                assert response["status"] == 409 and response["body"]["current"] == before, response
                for effect in (recover, spawn, cleanup, heads):
                    effect.assert_not_called()
        invalid = f.made("live-invalid", "running", live=True)
        with mock.patch.object(control, "launch") as spawn:
            response = f.post(f"/api/runs/{invalid}/{action}", {"base_version": runs.run_get(invalid)["version"]})
            assert response["status"] == 400, response
            spawn.assert_not_called()
        run = f.made("preflight-" + action.split("/")[0], pr_url="https://example.test/fixture/pull/1", branch="fixture")
        version = runs.run_get(run)["version"]

        def changed(*args, **kwargs):
            record.note(run, summary="changed in preflight")

        with mock.patch.object(tasks, "free_to_start", side_effect=changed), \
                mock.patch.object(repairs, "open_heads"), mock.patch.object(pipeline, "script_refusal", return_value=""), \
                mock.patch.object(control, "launch") as spawn:
            response = f.post(f"/api/runs/{run}/{action}", {"base_version": version})
            assert response["status"] == 409, response
            spawn.assert_not_called()
        version = runs.run_get(run)["version"]
        with mock.patch.object(tasks, "free_to_start"), mock.patch.object(repairs, "open_heads"), \
                mock.patch.object(pipeline, "script_refusal", return_value=""), \
                mock.patch.object(control, "launch", side_effect=f.fake_launch):
            response = f.post(f"/api/runs/{run}/{action}", {"base_version": version})
            assert response["status"] == 200 and response["body"]["run"] != run, response
            assert "Arete-Seq" in response["headers"]
    dead = f.made("dead-recovery", "running")
    version = runs.run_get(dead)["version"]
    with mock.patch.object(tasks, "free_to_start"), mock.patch.object(control, "launch", side_effect=f.fake_launch), \
            mock.patch.object(pipeline, "tell_ticket_run_died", side_effect=lambda cfg, row: record.note(
                row["id"], label_pending="")):
        response = f.post(f"/api/runs/{dead}/resume", {"base_version": version})
        assert response["status"] == 200, response
        assert runs.run_get(dead)["version"] == version + 2
    original = f.made("already-repaired")
    version = runs.run_get(original)["version"]
    later = record.start("pipeline", repo="replay-sample", issue=runs.run_get(original)["issue"])
    record.note(original, summary="changed")
    with mock.patch.object(pipeline, "repaired_by", return_value=original):
        assert f.post(f"/api/runs/{original}/repair", {"base_version": version})["status"] == 409
        response = f.post(f"/api/runs/{original}/repair", {"base_version": version + 1})
        assert response["status"] == 200 and response["body"]["run"] == later, response


def grace(f):
    from arete import control, record, util
    from arete.store import runs, writes

    instant = datetime(2026, 1, 1, tzinfo=UTC)
    with mock.patch.object(control.os, "killpg"), mock.patch.object(util, "now", return_value=instant):
        run = f.made("inactive", "running", live=True)
        assert f.post(stop_path(run))["status"] == 200
        deadline = writes.activate_grace()
        assert deadline == util.iso(instant + timedelta(days=30))
    for delta, accepted in ((timedelta(days=30, seconds=-1), True), (timedelta(days=30), False),
                             (timedelta(days=31), False)):
        with mock.patch.object(util, "now", return_value=instant + delta), mock.patch.object(control.os, "killpg") as kill:
            run = f.made("boundary", "running", live=True)
            version = runs.run_get(run)["version"]
            response = f.post(stop_path(run))
            assert response["status"] == (200 if accepted else 409), response
            if not accepted:
                kill.assert_not_called()
                assert response["body"]["current"] == runs.run_get(run)
                assert f.post(stop_path(run), {"base_version": version})["status"] == 200
                assert f.post(stop_path(run), {"base_version": version})["status"] == 409
            assert writes.activate_grace() == deadline
            record.note(run, summary="internal versionless write")
            assert record.move(run, "stopped", expect=("running",))
    f.close()
    f.children = []
    f.open()
    assert f.rows("SELECT value FROM schema_meta WHERE key = 'versioned_api_grace_deadline'")[0]["value"] == deadline
    with mock.patch.object(util, "now", return_value=instant + timedelta(days=31)), \
            mock.patch.object(control.os, "killpg") as kill:
        run = f.made("reopened", "running", live=True)
        assert f.post(stop_path(run))["status"] == 409
        kill.assert_not_called()


def existing_case(f, filename, **extra):
    from arete import config, paths, record, util
    from arete.store import asks, runs

    def made(tag, status="done", minutes=2, repo="replay-sample", kind="pipeline", issue=None, **kwargs):
        run = record.start(kind, tag=tag, repo=repo, issue=tag if issue is None else issue,
                           started=util.iso(util.now() - timedelta(minutes=minutes)))
        record.settle(run, status)
        return run

    namespace = dict(root=f.root, config=config, paths=paths, record=record, util=util, mock=mock, json=json,
                     made=made, run_store=runs, asks_store=asks,
                     result=lambda **kw: (f.root / (filename + ".json")).write_text(json.dumps(kw)))
    namespace.update(extra)
    path = SOURCE / "tests/scripts" / filename
    exec(compile(path.read_text(), str(path), "exec"), namespace)


def status_races(f):
    from arete import config, outcomes, record
    from arete.store import runs

    existing_case(f, "miner_pr_poll_and_supersede.py")
    existing_case(f, "miner_stop_retirement.py")
    run = f.made("reopen", "rejected", pr_url="https://example.test/pull/1")
    with mock.patch.object(outcomes.gh, "pr_view", return_value={"state": "OPEN"}), \
            mock.patch.object(outcomes, "record_feedback"):
        outcomes.sync_outcomes(config.Config())
    assert runs.run_get(run)["status"] == "pr"
    before = runs.run_get(run)
    assert not record.move(run, "merged", expect=("running",))
    assert runs.run_get(run) == before
    invariant(f)


def ask_races(f):
    from arete import owner_asks, tasks
    from arete.store import asks, runs

    question = [{"kind": "text", "header": "Choice", "question": "Which?"}]
    for via in ("answer", "reply", "multi", "partial"):
        run = f.made(via, "needs-info")
        row = runs.run_get(run)
        ids = [via + "-one", via + "-two"] if via in ("multi", "partial") else [via]
        for ask in ids:
            asks.ask_add(ask, run, row["repo"], row["issue"], "build", "call", "why", "tried", question, {})

        def close_from_tracker(*args, **kwargs):
            if via != "partial":
                ask = asks.ask_get(ids[-1])
                asks.ask_close(ids[-1], "answered", owner_asks.text_answers(ask, "tracker text"))

        with mock.patch.object(tasks, "free_to_start", side_effect=close_from_tracker), \
                mock.patch.object(tasks, "continuing", return_value=["resume"]), \
                mock.patch.object(tasks, "live_run", return_value=None), mock.patch.object(tasks, "launch") as spawn:
            if via in ("answer", "partial"):
                response = f.post(f"/api/asks/{ids[0]}/answer", {"answers": [{"text": "dashboard text"}]})
                assert response["status"] == (200 if via == "partial" else 409), response
                if via == "partial":
                    assert response["body"]["state"] == "waiting" and asks.ask_get(ids[-1])["status"] == "open"
                else:
                    assert response["body"]["code"] == "already_resolved"
                    again = f.post(f"/api/asks/{ids[0]}/answer", {"answers": [{"text": "again"}]})
                    assert again["status"] == 409 and again["body"]["code"] == "already_resolved"
            else:
                refusal = refuses(tasks.AlreadyResolved, lambda: tasks.reply(run, "dashboard text"))
                assert refusal.status == 409
                if via == "multi":
                    assert asks.ask_get(ids[0])["status"] == "open"
            spawn.assert_not_called()
        if via != "partial":
            assert "tracker text" in json.dumps(asks.ask_get(ids[-1])["answers"])
    for in_flight in (False, True):
        existing_case(f, "miner_ask_answer_serialization.py", in_flight=in_flight)


def carried_landings(f):
    from arete.store import writes

    for wider in (False, True):
        for landed in ("", "staging"):
            for row in f.rows("SELECT * FROM runs"):
                writes.update("runs", row["id"], {}, delete=True, kind="run.deleted")
            existing_case(f, "miner_carried_landings.py", wider=wider, landed=landed)
    invariant(f)


def clients(f):
    from arete import client, control, record, tasks
    from arete.store import runs

    from tests.test_ui import NewUIShellTests

    run = f.made("client", "running", live=True)
    selected = runs.run_get(run)["version"]
    os.environ["ARETE_SERVER"] = f"http://127.0.0.1:{f.server.server_port}"
    with mock.patch.object(control.os, "killpg"):
        assert client.run_command(run, "stop") == 0
    deadline = time.monotonic() + 2
    observed = []
    while time.monotonic() < deadline:
        observed = [json.loads(line) for line in f.transcript.read_text().splitlines()]
        if any(r["path"] == stop_path(run) for r in observed):
            break
        time.sleep(.01)
    assert next(r for r in observed if r["path"] == stop_path(run))["request"]["base_version"] == selected
    for action, body in (("resume", {}), ("repair", {}), ("steps/implement", {"set": {}})):
        target = f.made("client-" + action.split("/")[0])
        read = client.call
        requests = []

        def changed(method, path, body=None):
            requests.append((method, path, body))
            result = read(method, path, body)
            if method == "GET":
                record.note(target, summary="changed after CLI read")
            return result

        with mock.patch.object(client, "call", side_effect=changed), mock.patch.object(tasks, "launch") as spawn:
            assert client.run_command(target, action, body) == 1
            spawn.assert_not_called()
        assert [r[0] for r in requests] == ["GET", "POST"]
        assert requests[-1][2]["base_version"] == runs.run_get(target)["version"] - 1
    attempts, restart_deadlines = [], []

    def restarted(method, path, body, *args, deadline=None):
        attempts.append((method, path, json.dumps(body, sort_keys=True)))
        restart_deadlines.append(deadline)
        if len(attempts) == 1:
            raise client.Restarting({"error": "restart"}, 0)
        return SimpleNamespace(read=lambda: b"{}")

    with mock.patch.object(client, "request_once", side_effect=restarted):
        client.call("POST", stop_path(run), {"base_version": selected})
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert restart_deadlines[0] is None and isinstance(restart_deadlines[1], float)
    ui = NewUIShellTests()
    ui.test_versioned_run_actions()


def keyed_replay(f):
    from arete import control, pipeline, record, tasks
    from arete.store import idempotency

    body = {"repo": "replay-sample", "prompt": "Add kumquat"}
    with mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn), mock.patch.object(tasks, "held", return_value=""):
        first = f.post("/api/tasks", body, "replay-572")
        assert first["status"] == 200 and first["body"]["state"] == "running", first
        second = f.post("/api/tasks", ' { "prompt": "Add kumquat", "repo": "replay-sample" } ', "replay-572")
        assert second["raw"] == first["raw"] and second["status"] == first["status"]
        assert second["headers"]["Idempotent-Replayed"] == "true"
        assert second["headers"]["Arete-Seq"] == first["headers"]["Arete-Seq"]
        assert f.post("/api/tasks", {**body, "prompt": "changed"}, "replay-572")["body"]["code"] == "idempotency_key_reused"
        assert len(f.rows("SELECT * FROM tasks")) == len(f.rows("SELECT * FROM runs")) == 1
        claim = idempotency.lookup(f.person, "replay-572")
        assert claim["via"] == "POST /api/tasks" and claim["response"].hex() == first["raw"]
        event = f.rows("SELECT * FROM events_log WHERE seq = ?", (claim["first_seq"],))[0]
        assert event["kind"] == "task.created" and event["actor"] == f.person
        with mock.patch.object(tasks, "begin", side_effect=control.Refused("handled start error")):
            failed = f.post("/api/tasks", body, "error-572")
        assert failed["status"] == 400
        count = len(f.rows())
        f.server.shutdown()
        f.server.server_close()
        f.thread.join(5)
        f.open()
        for key, original in (("replay-572", first), ("error-572", failed)):
            replayed = f.post("/api/tasks", body, key)
            assert replayed["raw"] == original["raw"] and replayed["status"] == original["status"]
        assert len(f.rows()) == count
        for key in ("_.:/-aZ0", "a" * 128):
            assert f.post("/api/tasks", body, key)["status"] == 200
        count = len(f.rows("SELECT * FROM tasks"))
        for key in ("", "short", "a" * 129, "bad key-572", "nonascii-é"):
            assert f.post("/api/tasks", body, key)["status"] == 400
        from arete import idempotency as coordinator

        refuses(control.Refused, lambda: coordinator.owning(f.person, "newline\n", "sha").__enter__())
        assert len(f.rows("SELECT * FROM tasks")) == count
        with mock.patch.object(tasks, "held", return_value="capacity"):
            queued = f.post("/api/tasks", body, "queued-572")
            assert f.post("/api/tasks", body)["body"]["state"] == "queued"
        assert queued["body"]["state"] == "queued"
        assert f.post("/api/tasks", body, "queued-572")["raw"] == queued["raw"]
        for _ in range(2):
            assert f.post("/api/tasks", body)["body"]["state"] == "running"
        blocked = f.post("/api/tasks", {**body, "prompt": f"Waits on {first['body']['task']}.\nAdd another word"})
        assert blocked["body"]["state"] == "queued"
        assert len(f.rows("SELECT * FROM tasks")) == count + 5
        record.note(first["body"]["run"], summary="later unrelated event")
        assert f.post("/api/tasks", body, "replay-572")["headers"]["Arete-Seq"] == first["headers"]["Arete-Seq"]


def keyed_concurrency(f):
    from arete import pipeline, tasks, util
    from arete.store import idempotency

    body = {"repo": "replay-sample", "prompt": "Add kumquat"}
    entered, release = threading.Event(), threading.Event()
    begin = tasks.begin

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return begin(*args, **kwargs)

    with mock.patch.object(tasks, "begin", side_effect=paused), mock.patch.object(tasks, "held", return_value=""), \
            mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn), ThreadPoolExecutor() as pool:
        first = pool.submit(f.post, "/api/tasks", body, "concurrent-572")
        assert entered.wait(5)
        try:
            started = time.monotonic()
            duplicate = f.post("/api/tasks", body, "concurrent-572")
            assert time.monotonic() - started < 2
            assert duplicate["status"] == 409 and duplicate["body"]["code"] == "idempotency_in_flight"
            assert duplicate["headers"]["Retry-After"] == "1"
            changed = f.post("/api/tasks", {**body, "prompt": "different"}, "concurrent-572")
            assert changed["status"] == 422
        finally:
            release.set()
        original = first.result(10)
    assert f.post("/api/tasks", body, "concurrent-572")["raw"] == original["raw"]
    with f.db.tx() as c:
        c.execute("INSERT INTO schema_meta VALUES ('multi_user', 'ready')")
        c.execute("INSERT INTO principals (id,kind,role,name,status,created) VALUES "
                  "('u:second','human','owner','Second','active',?)", (util.iso(),))
        for identity, person, login in (("second", "u:second", "second@example.test"),
                                        ("alias", f.person, "alias@example.test")):
            c.execute("INSERT INTO principal_logins (id,principal,method,subject,created) VALUES (?,?,'tailnet',?,?)",
                      (identity, person, login, util.iso()))
    with mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn), mock.patch.object(tasks, "held", return_value=""):
        f.who("second@example.test")
        second = f.post("/api/tasks", body, "concurrent-572")
        assert second["status"] == 200 and second["body"]["task"] != original["body"]["task"]
        f.who("alias@example.test")
        assert f.post("/api/tasks", body, "concurrent-572")["raw"] == original["raw"]
        for key, module, name in (("failure-execute", tasks, "begin"), ("failure-persist", idempotency, "complete")):
            with mock.patch.object(module, name, side_effect=RuntimeError("injected failure")):
                assert f.post("/api/tasks", body, key)["status"] == 500
            recovered = f.post("/api/tasks", body, key)
            assert recovered["status"] == (500 if name == "begin" else 200), recovered
            assert f.post("/api/tasks", body, key)["raw"] == recovered["raw"]


def slow_start(f):
    from collections import Counter
    from dataclasses import replace

    from arete import config, control, pipeline, poll, record, repairs, run_lines, slots, tasks, tracker
    from arete.store import runs

    registrations, spawned = [], []
    register_first = threading.Event()
    cfg = config.load()
    repo = replace(cfg.repo("replay-sample"), pipeline=True)
    cfg = replace(cfg, repos=[repo])

    def delayed(repo, tid, force=False):
        gate = f.root / f"register-{os.urandom(8).hex()}"
        child, ready = f.pipeline_child(repo, tid, gate=gate)
        spawned.append(tid)
        first = len(spawned) == 1
        register_at = time.monotonic() + 2

        def register():
            if first:
                assert register_first.wait(10)
            time.sleep(max(0, register_at - time.monotonic()))
            gate.touch()
            f.wait_registration(child, ready)

        thread = threading.Thread(target=register)
        registrations.append(thread)
        thread.start()
        return child

    with mock.patch.object(config, "load", return_value=cfg), \
            mock.patch.object(pipeline, "RUN_START_SECONDS", 1), mock.patch.object(tasks, "held", return_value=""), \
            mock.patch.object(pipeline, "spawn_run", side_effect=delayed):
        with ThreadPoolExecutor() as pool:
            started = pool.submit(f.post, "/api/tasks", {"repo": "replay-sample", "prompt": "slow"}, "slow-572")
            deadline = time.monotonic() + 5
            while not spawned and time.monotonic() < deadline:
                time.sleep(.01)
            writer = pool.submit(record.start, "fixture", tag="writer-during-wait")
            assert writer.result(.8)
            initial = started.result(5)
        assert initial["status"] == 200 and initial["body"]["state"] == "starting" and not initial["body"]["run"]
        tid = initial["body"]["task"]
        duplicate = f.post("/api/tasks", {"repo": "replay-sample", "ticket": tid}, "slow-other-572")
        assert duplicate["body"]["state"] == "starting" and len(spawned) == 1
        duplicate = f.post("/api/tasks", {"repo": "replay-sample", "ticket": tid})
        assert duplicate["body"]["state"] == "starting" and len(spawned) == 1
        with mock.patch.object(pipeline, "skipped_tickets", return_value={tid: {"repo": repo.name, "id": tid}}):
            skipped_after_http = f.post(f"/api/skipped/{tid}/run")
        assert skipped_after_http["status"] == 200 and skipped_after_http["body"]["id"] == tid
        assert len(spawned) == 1 and not runs.ticket_runs(repo.name, tid)
        queue = poll.RepoQueue(repo, tracker.LocalTracker(repo), set(), config.load_layer(repo.layer_dir), None)
        skips = pipeline.SkipLog({})
        pick = poll.next_pick(repo, queue.tracker, skips, set())
        assert pick and pick[0] == tid and not runs.ticket_runs(repo.name, tid)
        with mock.patch.object(poll, "spawn_run", side_effect=delayed) as poll_spawn, \
                mock.patch.object(poll, "step_without_route", return_value=""):
            result = poll.start_pick(cfg, queue, pick, skips, False, Counter(), 1)
            assert result == "skipped" and len(spawned) == 1
            poll_spawn.assert_not_called()
            register_first.set()
            registrations[-1].join(5)
            assert not poll.spawn_unparked(repo.name, tid)
            poll_spawn.assert_not_called()
            polled = f.tasks.task_add(repo.name, "poller starts before HTTP")
            assert poll.spawn_unparked(repo.name, polled)
            assert not runs.ticket_runs(repo.name, polled)
            duplicate = f.post("/api/tasks", {"repo": repo.name, "ticket": polled}, "polled-slow-572")
            assert duplicate["body"]["state"] == "starting" and poll_spawn.call_count == 1
            assert len(spawned) == 2
            registrations[-1].join(5)
            assert len(runs.ticket_runs(repo.name, polled)) == 1
        (f.root / "pending-poll.json").write_text(json.dumps({"http_task": tid, "poll_task": polled,
                                                            "overlap": result, "children": list(spawned)}))
        registrations[-1].join(5)
        replayed = f.post("/api/tasks", {"repo": "replay-sample", "prompt": "slow"}, "slow-572")
        assert replayed["raw"] == initial["raw"]
        assert len(runs.ticket_runs("replay-sample", tid)) == 1
        deadline = time.monotonic() + 5
        while (repo.name, tid) in tasks.PENDING and time.monotonic() < deadline:
            time.sleep(.01)
        assert (repo.name, tid) not in tasks.PENDING
        assert f.post("/api/tasks", {"repo": "replay-sample", "prompt": "slow"}, "slow-572")["raw"] == initial["raw"]
        skipped = f.tasks.task_add(repo.name, "Run anyway starts before keyed HTTP")
        with mock.patch.object(pipeline, "skipped_tickets", return_value={skipped: {"repo": repo.name, "id": skipped}}), \
                mock.patch.object(slots, "slots_free_to_start", return_value=1):
            skipped_first = f.post(f"/api/skipped/{skipped}/run")
            assert skipped_first["status"] == 200 and not runs.ticket_runs(repo.name, skipped)
            before = len(spawned)
            duplicate = f.post("/api/tasks", {"repo": repo.name, "ticket": skipped}, "skipped-slow-572")
            assert duplicate["body"]["state"] == "starting" and not duplicate["body"]["run"]
            assert len(spawned) == before
            assert f.post(f"/api/skipped/{skipped}/run")["status"] == 200
            assert len(spawned) == before
        registrations[-1].join(5)
        assert len(runs.ticket_runs(repo.name, skipped)) == 1
        (f.root / "pending-skipped.json").write_text(json.dumps({"http_first": skipped_after_http,
                                                               "skipped_first": skipped_first, "duplicate": duplicate,
                                                               "children": list(spawned)}))
        for action in ("resume", "repair", "steps/implement"):
            original = f.made(action.split("/")[0], pr_url="https://example.test/fixture/pull/1", branch="fixture")
            row = runs.run_get(original)
            target = row["issue"]
            with mock.patch.object(tasks, "free_to_start"), \
                    mock.patch.object(repairs, "open_heads"), mock.patch.object(pipeline, "script_refusal", return_value=""), \
                    mock.patch.object(control, "launch", side_effect=lambda *a: delayed(repo.name, target)):
                path = f"/api/runs/{original}/{action}"
                accepted = f.post(path, {"base_version": row["version"]})
                assert accepted["status"] == 200 and accepted["body"]["state"] == "starting", accepted
                assert f.post(path, {"base_version": row["version"]})["body"]["state"] == "starting"
            registrations[-1].join(5)
        def exited(*args):
            child = f.launch_process(["false"], "fixture-children.log")
            f.children.append(child)
            child.wait()
            return child

        def finished(repo_name, target, force=False):
            child, ready = f.pipeline_child(repo_name, target, finish=True, fields={
                "pr_url": "https://example.test/fixture/pull/1", "branch": "fixture"})
            run = f.wait_registration(child, ready)
            child.wait(5)
            run_lines.write(run, "task", path="feature", title="finished pending child")
            run_lines.write(run, "step_start", step="implement", try_no=1)
            run_lines.write(run, "step_end", step="implement", result="fail", data={})
            record.settle(run, "failed", end_line="failed")
            run_lines.write(run, "report", status="failed", review={"verdict": "request_changes", "problems": [
                {"text": "Fix the fixture", "file": "notes.txt", "category": "issue"}]})
            (f.paths.RUNS_DIR / run / "plan.md").write_text("Add kumquat to notes.txt")
            return child

        completed = []
        # Pin stale entries while the existing admission/status assertions exercise them.
        with mock.patch.object(tasks, "prune_pending"):
            for action in ("start", "skipped", "resume", "repair", "steps/implement"):
                target = f.tasks.task_add(repo.name, "Restart after the pending poller child finishes")
                with mock.patch.object(poll, "spawn_run", side_effect=finished):
                    assert poll.spawn_unparked(repo.name, target)
                old = runs.ticket_runs(repo.name, target)[0]
                assert old["status"] == "failed" and (repo.name, target) in tasks.PENDING
                with mock.patch.object(control, "launch", side_effect=f.fake_launch) as launch, \
                        mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn) as spawn, \
                        mock.patch.object(repairs, "open_heads"), \
                        mock.patch.object(pipeline, "script_refusal", return_value=""), \
                        mock.patch.object(pipeline, "skipped_tickets", return_value={target: {"repo": repo.name, "id": target}}), \
                        mock.patch.object(slots, "slots_free_to_start", return_value=1):
                    if action == "start":
                        result = f.post("/api/tasks", {"repo": repo.name, "ticket": target}, "completed-pending-572")
                    elif action == "skipped":
                        result = f.post(f"/api/skipped/{target}/run")
                    else:
                        path = f"/api/runs/{old['id']}/{action}"
                        stale = f.post(path, {"base_version": old["version"] - 1})
                        assert stale["status"] == 409 and stale["body"]["code"] == "version_conflict", stale
                        launch.assert_not_called()
                        assert (repo.name, target) in tasks.PENDING
                        result = f.post(path, {"base_version": old["version"]})
                    assert result["status"] == 200 and spawn.call_count + launch.call_count == 1, result
                    new = runs.ticket_runs(repo.name, target)[0]
                    assert new["id"] != old["id"] and new["status"] == "running"
                    assert runs.run_get(old["id"]) == old
                    if action != "skipped":
                        assert result["body"]["run"] == new["id"] and result["body"]["state"] == "running", result
                    assert (repo.name, target) not in tasks.PENDING
                    completed.append({"action": action, "old": old["id"], "new": new["id"], "response": result})
        (f.root / "pending-completed.json").write_text(json.dumps(completed, indent=2))
        completed_body = {"repo": repo.name, "prompt": "finishes during spawn"}
        with mock.patch.object(pipeline, "spawn_run", side_effect=finished) as spawn:
            completed = f.post("/api/tasks", completed_body, "finished-start-572")
            assert completed["status"] == 200 and completed["body"]["state"] == "failed", completed
            assert completed["body"]["run"] == runs.ticket_runs(repo.name, completed["body"]["task"])[0]["id"]
            replayed = f.post("/api/tasks", completed_body, "finished-start-572")
            assert replayed["raw"] == completed["raw"] and replayed["headers"]["Idempotent-Replayed"] == "true"
            assert spawn.call_count == 1

        target = f.tasks.task_add(repo.name, "exited")
        with mock.patch.object(pipeline, "spawn_run", side_effect=exited):
            result = f.post("/api/tasks", {"repo": repo.name, "ticket": target}, "exited-572")
            assert result["status"] == 400 and "did not show" in result["body"]["error"]
        with mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn):
            assert f.post("/api/tasks", {"repo": repo.name, "ticket": target}, "retry-exited-572")["body"]["state"] == "running"
        target = f.tasks.task_add(repo.name, "pending then exited")
        def exiting(*args):
            child = f.launch_process(["sleep", "1.3"], "fixture-children.log")
            f.children.append(child)
            return child

        with mock.patch.object(pipeline, "spawn_run", side_effect=exiting):
            accepted = f.post("/api/tasks", {"repo": repo.name, "ticket": target}, "pending-exit-572")
            assert accepted["body"]["state"] == "starting"
        child = f.children[-1]
        child.wait(5)
        deadline = time.monotonic() + 5
        while (repo.name, target) in tasks.PENDING and time.monotonic() < deadline:
            replayed = f.post("/api/tasks", {"repo": repo.name, "ticket": target}, "pending-exit-572")
            assert replayed["raw"] == accepted["raw"]
            time.sleep(.01)
        assert (repo.name, target) not in tasks.PENDING
        with mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn):
            assert f.post("/api/tasks", {"repo": repo.name, "ticket": target}, "after-pending-exit-572")["body"]["state"] == "running"
    for thread in registrations:
        thread.join(5)


def launch_boundaries(f):
    from arete import launch_lease, pipeline, util
    from arete.store import runs

    def eventually(predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.02)
        assert predicate()

    evidence = []
    repo = "replay-sample"
    for mode in ("live-parent", "after-fork", "before-fork"):
        children_before = len(f.children)
        tid = f.tasks.task_add(repo, f"Separate poller {mode}")
        gate, ready, stop, marker = (f.root / f"{mode}-{suffix}" for suffix in ("gate", "ready", "stop", "poller"))
        command = registration_command(repo, tid, ready, gate=gate, stop=stop)
        code = ("import json,os,sys\n"
                f"sys.path.insert(0, {str(SOURCE)!r})\n"
                "from pathlib import Path\n"
                "from unittest import mock\n"
                "from arete import control,poll,principals\n"
                "repo,tid,mode,command,marker=json.loads(sys.argv[1])\n"
                "def spawn(*args):\n"
                " if mode=='before-fork':\n"
                "  Path(marker).write_text('before fork')\n"
                "  os._exit(0)\n"
                " child=control.launch(command,'boundary-child.log')\n"
                " Path(marker).write_text('after fork')\n"
                " if mode=='after-fork': os._exit(0)\n"
                " return child\n"
                "with principals.acting(principals.owner()),mock.patch.object(poll,'spawn_run',side_effect=spawn):\n"
                " assert poll.spawn_unparked(repo,tid)\n"
                " sys.stdin.read(1)\n")
        parent = subprocess.Popen([sys.executable, "-I", "-c", code,
                                   json.dumps([repo, tid, mode, command, str(marker)])],
                                  stdin=subprocess.PIPE, start_new_session=True)
        f.children.append(parent)
        try:
            eventually(marker.exists)
            assert not runs.ticket_runs(repo, tid)
            if mode != "live-parent":
                assert parent.wait(5) == 0
            assert launch_lease.held(repo, tid) == (mode != "before-fork")
            body, key = {"repo": repo, "ticket": tid}, f"boundary-{mode}"
            with mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn) as spawn:
                initial = f.post("/api/tasks", body, key)
                assert initial["status"] == 200, initial
                if mode == "before-fork":
                    assert initial["body"]["state"] == "running" and spawn.call_count == 1
                    assert len(runs.ticket_runs(repo, tid)) == 1
                else:
                    assert initial["body"]["state"] == "starting" and not initial["body"]["run"]
                    assert spawn.call_count == 0 and not runs.ticket_runs(repo, tid)
                    if mode == "live-parent":
                        parent.stdin.close()
                        assert parent.wait(5) == 0
                        assert launch_lease.held(repo, tid)
                        duplicate = f.post("/api/tasks", body, key + "-after-parent")
                        assert duplicate["body"]["state"] == "starting" and spawn.call_count == 0
                        gate.touch()
                        eventually(ready.exists)
                        assert not launch_lease.held(repo, tid)
                        registered = f.post("/api/tasks", body, key + "-registered")
                        assert registered["body"]["state"] == "running" and spawn.call_count == 0
                        assert registered["body"]["run"] == json.loads(ready.read_text())["run"]
                        assert len(runs.ticket_runs(repo, tid)) == 1
                    else:
                        stop.touch()
                        eventually(lambda: not launch_lease.held(repo, tid))
                        assert not runs.ticket_runs(repo, tid)
                        retry = f.post("/api/tasks", body, key + "-new")
                        assert retry["body"]["state"] == "running" and spawn.call_count == 1
                        assert len(runs.ticket_runs(repo, tid)) == 1
                replay = f.post("/api/tasks", body, key)
                assert replay["raw"] == initial["raw"] and replay["headers"]["Idempotent-Replayed"] == "true"
                evidence.append({"mode": mode, "initial": initial, "replayed": replay, "http_spawns": spawn.call_count})
        finally:
            stop.touch()
            if parent.stdin and not parent.stdin.closed:
                parent.stdin.close()
            parent.wait(5)
            eventually(lambda: not launch_lease.held(repo, tid))
            if mode == "live-parent":
                eventually(lambda: not util.lock_is_held(runs.run_lock("pipeline", repo, tid)))
            for child in f.children[children_before:]:
                if child.poll() is None:
                    child.terminate()
                child.wait(5)
    (f.root / "launch-boundaries.json").write_text(json.dumps(evidence, indent=2))


def crash_server(root, mode):
    from arete import accounts, gh, record, tasks, tracker

    with mock.patch.object(urllib.request, "urlopen", forbidden), mock.patch.object(accounts, "read", forbidden), \
            mock.patch.object(gh, "gh", forbidden):
        f = Fixture(root)

        def die(cfg, repo, tid, *args, **kwargs):
            if mode in ("registered", "terminal"):
                run = record.start("pipeline", repo=repo.name, issue=tid)
                if mode == "terminal":
                    record.settle(run, "done")
            os._exit(9)

        external = (mock.patch.object(tracker, "for_ticket", return_value=SimpleNamespace(normalize=lambda ticket: ticket))
                    if mode == "external" else contextlib.nullcontext())
        with mock.patch.object(tasks, "begin", side_effect=die), external:
            (root / "crash-ready.json").write_text(json.dumps({"port": f.server.server_port}))
            while True:
                time.sleep(.1)


def keyed_recovery(f):
    from arete import pipeline, runlimits, tasks, tracker
    from arete.store import idempotency

    cases = (("empty", False), ("registered", False), ("terminal", False), ("empty", True), ("external", True))
    for i, (mode, ticket) in enumerate(cases):
        body = {"repo": "replay-sample"}
        if ticket:
            body["ticket"] = "42" if mode == "external" else f.tasks.task_add("replay-sample", "existing")
        else:
            body["prompt"] = "recover " + mode
        key = f"crash-572-{i}"
        ready = f.root / "crash-ready.json"
        ready.unlink(missing_ok=True)
        f.server.shutdown()
        f.server.server_close()
        f.thread.join(5)
        f.server = None
        proc = subprocess.Popen([sys.executable, "-I", str(SOURCE / "docs/versioned-api-fixture.py"),
                                 "--child", "crash-server", "--root", str(f.root), "--crash-mode", mode],
                                cwd=SOURCE, env=os.environ.copy(), stdout=(f.root / f"crash-{i}.out").open("w"),
                                stderr=(f.root / f"crash-{i}.err").open("w"))
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert ready.exists(), (f.root / f"crash-{i}.err").read_text()
            port = json.loads(ready.read_text())["port"]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/tasks", json.dumps(body),
                         {"X-Arete": "1", "Content-Type": "application/json", "Idempotency-Key": key})
            try:
                conn.getresponse()
                raise AssertionError("crash server returned a response")
            except (OSError, http.client.HTTPException):
                pass
            finally:
                conn.close()
            assert proc.wait(10) == 9
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        claim = idempotency.lookup(f.person, key)
        assert claim and claim["status"] == 0 and not claim["response"]
        repo, tid, prompt = idempotency.target(claim)
        assert prompt != ticket and repo == "replay-sample"
        before = len(f.rows("SELECT * FROM tasks"))
        runs_before = f.runs.ticket_runs(repo, tid)
        f.open()
        with mock.patch.object(tasks, "held", return_value=""), mock.patch.object(runlimits, "admission", return_value=("", None)), \
                mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn) as spawn:
            recovered = f.post("/api/tasks", body, key)
            assert recovered["status"] == 200 and recovered["body"]["task"] == tid, recovered
            if runs_before:
                spawn.assert_not_called()
                assert recovered["body"]["run"] == runs_before[0]["id"]
                assert recovered["body"]["state"] == runs_before[0]["status"]
            else:
                assert spawn.call_count == 1
            assert len(f.runs.ticket_runs(repo, tid)) == 1
        assert len(f.rows("SELECT * FROM tasks")) == before
        (f.root / f"crash-{i}.json").write_text(json.dumps({"exit": 9, "target": tid, "first_seq": claim["first_seq"],
                                                         "actor": claim["actor"], "via": claim["via"], "runs_before": len(runs_before)}))
    before = {table: f.rows(f"SELECT * FROM {table}") for table in ("tasks", "events_log", "idempotency")}
    body, key = {"repo": "replay-sample", "prompt": "rollback"}, "rollback-572"
    with mock.patch.object(idempotency, "claim", side_effect=RuntimeError("claim transaction failure")) as failed_claim:
        failed = f.post("/api/tasks", body, key)
        assert failed["status"] == 500
        assert failed["body"] == {"error": "RuntimeError: claim transaction failure."}
        failed_claim.assert_called_once()
    for table in ("tasks", "events_log"):
        assert f.rows(f"SELECT * FROM {table}") == before[table]
    claim = idempotency.lookup(f.person, key)
    assert claim and claim["status"] == 500 and claim["response"].hex() == failed["raw"]
    assert claim["first_seq"] is None and claim["last_seq"] is None
    assert claim["route"] == "/api/tasks" and claim["via"] == "POST /api/tasks"
    assert "Arete-Seq" not in failed["headers"]
    try:
        idempotency.target(claim)
    except RuntimeError as error:
        assert str(error) == "idempotency target event is missing"
    else:
        raise AssertionError("rolled back claim has a fabricated target")
    after = {table: f.rows(f"SELECT * FROM {table}") for table in before}
    assert len(after["idempotency"]) == len(before["idempotency"]) + 1
    existing = [row for row in after["idempotency"] if (row["actor"], row["key"]) != (f.person, key)]
    assert existing == before["idempotency"]
    with mock.patch.object(tasks, "start", side_effect=AssertionError("completed error must replay")) as start:
        replayed = f.post("/api/tasks", body, key)
        start.assert_not_called()
    assert replayed["status"] == 500 and replayed["raw"] == failed["raw"]
    assert replayed["headers"]["Idempotent-Replayed"] == "true" and "Arete-Seq" not in replayed["headers"]
    assert after == {table: f.rows(f"SELECT * FROM {table}") for table in after}
    assert tracker.is_task_id(tid) is False


class DropProxy:
    def __init__(self, f, mode, repeat=False):
        self.requests = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                proxy.requests.append({"key": self.headers.get("Idempotency-Key"), "body": raw.hex(), "path": self.path})
                if mode == "in-flight":
                    payload = b'{"error":"waiting","code":"idempotency_in_flight"}'
                    self.send_response(409)
                    self.send_header("Retry-After", "0")
                else:
                    conn = http.client.HTTPConnection("127.0.0.1", f.server.server_port, timeout=10)
                    conn.request("POST", self.path, raw, dict(self.headers))
                    response = conn.getresponse()
                    payload = response.read()
                    status, headers = response.status, response.getheaders()
                    conn.close()
                    if mode == "headers" and (repeat or len(proxy.requests) == 1):
                        self.close_connection = True
                        return
                    self.send_response(status)
                    for name, value in headers:
                        if name.lower() not in ("content-length", "server", "date"):
                            self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if mode == "body" and (repeat or len(proxy.requests) == 1):
                    self.wfile.write(payload[:len(payload) // 2])
                    self.close_connection = True
                    return
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def keyed_clients(f):
    from arete import client, pipeline, tasks

    evidence = []

    def cli(arguments, server, env_key=None):
        env = {k: v for k, v in os.environ.items() if k != "ARETE_IDEMPOTENCY_KEY"}
        env["ARETE_SERVER"] = f"http://127.0.0.1:{server.server_port}"
        if env_key is not None:
            env["ARETE_IDEMPOTENCY_KEY"] = env_key
        result = subprocess.run([sys.executable, "-m", "arete.cli", *arguments], cwd=SOURCE, env=env,
                                capture_output=True, text=True, timeout=35)
        evidence.append({"args": arguments, "stdout": result.stdout, "stderr": result.stderr, "exit": result.returncode})
        assert "Idempotency-Key: " in result.stderr, result
        return result, result.stderr.split("Idempotency-Key: ", 1)[1].splitlines()[0]

    with mock.patch.object(tasks, "held", return_value=""), mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn):
        keys = []
        for mode in ("headers", "body"):
            for command in ("start", "task-run", "run"):
                args = ["task", "start", "--repo", "replay-sample", "client " + mode] if command == "start" else []
                if command != "start":
                    tid = f.tasks.task_add("replay-sample", "client " + command)
                    args = (["task", "run"] if command == "task-run" else ["run"]) + ["replay-sample", tid]
                if command == "run":
                    def starting(*args, **kwargs):
                        return tasks.answer(args[2], args[1].name, "", "starting", "fixture accepted")
                    context = mock.patch.object(tasks, "begin", side_effect=starting)
                else:
                    context = contextlib.nullcontext()
                with context, DropProxy(f, mode) as proxy:
                    before = len(f.rows("SELECT * FROM tasks"))
                    result, key = cli(args, proxy.server)
                    assert result.returncode == 0, result
                    assert len(proxy.requests) == 2 and proxy.requests[0] == proxy.requests[1]
                    keys.append(key)
                    evidence.append({"proxy": mode, "requests": proxy.requests})
                    assert len(f.rows("SELECT * FROM tasks")) == before + (command == "start")
                if command == "start":
                    repeated, repeated_key = cli([*args, "--key", key], f.server)
                    assert repeated.returncode == 0 and repeated.stdout == result.stdout and repeated_key == key
        assert len(set(keys)) == len(keys)
        for explicit, environment, expected in (("explicit-572", "environment-572", "explicit-572"),
                                                 (None, "environment-572", "environment-572")):
            args = ["task", "start", "--repo", "replay-sample", "key selection"]
            result, key = cli(args + (["--key", explicit] if explicit else []), f.server, environment)
            assert result.returncode == 0 and key == expected
        for mode in ("headers", "body", "in-flight"):
            with DropProxy(f, mode, repeat=True) as proxy:
                started = time.monotonic()
                result, key = cli(["task", "start", "--repo", "replay-sample", "exhaust " + mode], proxy.server)
                assert result.returncode == 1 and time.monotonic() - started < 30
                assert len(proxy.requests) == 3 and len({r["key"] for r in proxy.requests}) == 1
                evidence.append({"exhaust": mode, "requests": proxy.requests})
        with DropProxy(f, "headers", repeat=True) as proxy, \
                mock.patch.dict(os.environ, ARETE_SERVER=f"http://127.0.0.1:{proxy.server.server_port}"):
            refuses(client.Broken, lambda: client.call("POST", "/api/fixture/stale/missing", {}))
            assert len(proxy.requests) == 1
    attempts, clock = [], [0.0]

    def broken(*args, **kwargs):
        attempts.append(kwargs)
        clock[0] += 16
        raise client.Broken({"error": "simulated broken connection"})

    with mock.patch.object(client, "request_once", side_effect=broken), \
            mock.patch.object(client.time, "monotonic", side_effect=lambda: clock[0]), \
            mock.patch.object(client.time, "sleep"):
        refuses(client.Broken, lambda: client.call("POST", "/api/tasks", {}, key="budget-572"))
    assert len(attempts) == 2 and attempts[1]["within"] == 14
    redirects = []

    def connect(where, seconds):
        transport = mock.Mock()
        raw = (b"HTTP/1.1 308 Permanent Redirect\r\nLocation: https://fixture.test:1234/api/tasks\r\nContent-Length: 0\r\n\r\n"
               if where.scheme == "http" else b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        transport.makefile.return_value = io.BytesIO(raw)
        response = http.client.HTTPResponse(transport)
        response.begin()
        return SimpleNamespace(sock=transport, getresponse=lambda: response, close=lambda: None,
                               request=lambda method, path, body, headers: redirects.append(
                                   {"method": method, "path": path, "body": body.hex(), "headers": headers}))

    with mock.patch.object(client, "server", return_value=client.Server("http", "127.0.0.1", 1234, "fixture.test")), \
            mock.patch.object(client, "connect", side_effect=connect):
        assert client.call("POST", "/api/tasks", {"prompt": "redirect"}, key="redirect-572") == {}
    assert len(redirects) == 2
    assert redirects[0]["body"] == redirects[1]["body"]
    assert all(r["headers"]["Idempotency-Key"] == "redirect-572" for r in redirects)
    evidence.append({"redirects": redirects})
    (f.root / "cli.json").write_text(json.dumps(evidence, indent=2))


def idempotency_prune(f):
    from arete import backup, config, serve, util
    from arete.store import idempotency, incidents

    instant = datetime(2026, 6, 1, tzinfo=UTC)
    ledger = f.rows("SELECT * FROM schema_migrations")
    with f.db.tx() as c:
        for status in (0, 200):
            for seconds in (-1, 0, 1):
                c.execute("INSERT INTO idempotency (actor,key,route,request_sha,status,response,at) VALUES (?,?,?,?,?,?,?)",
                          (f.person, f"boundary-{status}-{seconds}", "/api/tasks", "sha", status, b"{}",
                           util.iso(instant - timedelta(days=30) + timedelta(seconds=seconds))))
    before = f.rows("SELECT key,at,status FROM idempotency")
    server = SimpleNamespace(cfg=config.load(), say=lambda text: messages.append(text), copy_off_machine=lambda path: None)
    messages = []
    with mock.patch.object(util, "now", return_value=instant), \
            mock.patch.object(backup, "snapshot", side_effect=RuntimeError("backup failure")):
        serve.Server.prune_data(server)
    assert f.rows("SELECT key,at,status FROM idempotency") == before
    ordered = []
    original = idempotency.prune

    def snapshot(*args):
        assert f.rows("SELECT key,at,status FROM idempotency") == before
        ordered.append("backup")
        target = f.root / f"prune-backup-{len(ordered)}.db"
        destination = sqlite3.connect(target)
        try:
            with f.db.reader() as source:
                source.backup(destination)
            assert destination.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 6
        finally:
            destination.close()
        return target

    def prune():
        assert ordered[-1] == "backup"
        ordered.append("prune")
        original()

    with mock.patch.object(util, "now", return_value=instant), mock.patch.object(backup, "snapshot", side_effect=snapshot), \
            mock.patch.object(idempotency, "prune", side_effect=prune), \
            mock.patch.object(incidents, "prune", side_effect=RuntimeError("transaction failure")):
        serve.Server.prune_data(server)
    assert f.rows("SELECT key,at,status FROM idempotency") == before
    with mock.patch.object(util, "now", return_value=instant), mock.patch.object(backup, "snapshot", side_effect=snapshot), \
            mock.patch.object(idempotency, "prune", side_effect=prune):
        serve.Server.prune_data(server)
    after = f.rows("SELECT key,at,status FROM idempotency")
    assert len(after) == 4 and all(not r["key"].endswith("--1") for r in after)
    assert f.rows("SELECT * FROM schema_migrations") == ledger
    assert ordered == ["backup", "prune", "backup", "prune"]
    (f.root / "prune.json").write_text(json.dumps({"before": before, "after": after, "order": ordered, "messages": messages,
                                                 "backups": [p.name for p in f.root.glob("prune-backup-*.db")]}))


def seed_serve(f):
    ids = {"stop": f.made("live", "running", live=True), "retry": f.made("retry"),
           "repair": f.made("repair", pr_url="https://example.test/fixture/pull/1", branch="fixture"),
           "step": f.made("step")}
    (f.root / "seed.json").write_text(json.dumps(ids, indent=2))
    port = f.server.server_port
    for label, run in ids.items():
        print(f"{label}: {run} http://127.0.0.1:{port}/#run/{run} "
              f"http://127.0.0.1:{port}/ui.html#run/{run}", flush=True)
    print(f"Live: http://127.0.0.1:{port}/#live; evidence: {f.root}", flush=True)


def child(name, root, port):
    from arete import accounts, control, gh, pipeline, principals, repairs, tasks

    def stop(*args):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)

    with mock.patch.object(urllib.request, "urlopen", forbidden), mock.patch.object(accounts, "read", forbidden), \
            mock.patch.object(gh, "gh", forbidden):
        if name == "migration-rollback":
            migration(root)
        f = Fixture(root, port)
        try:
            with principals.acting(f.person):
                if name == "nearby":
                    live_replay(f, nearby=True)
                else:
                    f.sample()
                    actions = {"migration-rollback": lambda _: None, "writes": writes_exercise, "stale-stop": stale_stop,
                               "concurrent-stop": concurrent_stop, "run-commands": run_commands, "grace": grace,
                               "status-races": status_races, "ask-races": ask_races,
                               "carried-landings": carried_landings, "clients": clients, "keyed-replay": keyed_replay,
                               "keyed-concurrency": keyed_concurrency, "keyed-clients": keyed_clients,
                               "keyed-recovery": keyed_recovery, "slow-start": slow_start,
                               "launch-boundaries": launch_boundaries, "idempotency-prune": idempotency_prune}
                    if name == "serve":
                        seed_serve(f)
                        f.save()
                        with mock.patch.object(tasks, "free_to_start"), mock.patch.object(repairs, "open_heads"), \
                                mock.patch.object(tasks, "held", return_value=""), \
                                mock.patch.object(control, "launch", side_effect=f.fake_launch), \
                                mock.patch.object(pipeline, "spawn_run", side_effect=f.fake_spawn):
                            while True:
                                time.sleep(1)
                    else:
                        actions[name](f)
                f.save()
                result = {"exercise": name, "passed": True, "forbidden_calls": 0}
                (root / "result.json").write_text(json.dumps(result))
                print("BEHAVIOR_RESULT=" + json.dumps(result), flush=True)
        finally:
            if name == "serve":
                f.save()
            f.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("exercise", "serve", "inspect"))
    parser.add_argument("exercise", nargs="?", choices=EXERCISES)
    parser.add_argument("--child", choices=(*EXERCISES, "serve", "crash-server"))
    parser.add_argument("--crash-mode", default="empty")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.child == "crash-server":
        crash_server(args.root, args.crash_mode)
    elif args.child:
        child(args.child, args.root, args.port)
    elif args.mode == "exercise" and args.exercise:
        exercise(args.exercise, args.root)
    elif args.mode == "serve":
        root, proc = launch("serve", args.root, args.port)
        print(f"Serving http://127.0.0.1:{args.port}/#live; seed IDs in {root}/seed.json", flush=True)
        try:
            raise SystemExit(proc.wait())
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
    elif args.mode == "inspect":
        roots = [args.root] if args.root else sorted(Path(tempfile.gettempdir()).glob("arete-versioned-api-*"))
        for root in roots:
            print(root)
            for name in ("result.json", "seed.json", "migration.json", "stdout.txt", "stderr.txt", "http.jsonl",
                         "cli.json", "prune.json", "canonical.json", *(p.name for p in sorted(root.glob("crash-*.json")))):
                path = root / name
                if path.exists():
                    print(f"{name}: {path.read_text()[-5000:]}")
    else:
        parser.error("choose exercise NAME, serve, or inspect")
