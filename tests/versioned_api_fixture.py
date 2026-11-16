from __future__ import annotations

import argparse
import contextlib
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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.events_principals_fixture import Fixture as EventsFixture, isolated, live_replay
from tests.harness import SOURCE

EXERCISES = ("migration-rollback", "writes", "stale-stop", "concurrent-stop", "run-commands", "grace",
             "status-races", "ask-races", "carried-landings", "clients", "nearby")


def forbidden(*args, **kwargs):
    raise AssertionError("external transport or credential lookup forbidden")


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
        self.children = []
        self.evidence_lock = threading.Lock()
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
                                "headers": self.evidence_headers, "body": json.loads(payload)}
                    with fixture.evidence_lock, (fixture.root / "http.jsonl").open("a") as out:
                        out.write(json.dumps(evidence) + "\n")

        with mock.patch.object(dashboard, "Handler", EvidenceHandler):
            super().open(port)

    def sample(self):
        from arete import replay
        from tests import offline_workflow

        offline_workflow.create_replay_config(SimpleNamespace(root=self.root, scenario="pass", replay=replay,
                                                             paths=self.paths))

    def post(self, path, body=None):
        conn, response = self.request(path, method="POST", headers={"X-Arete": "1", "Content-Type": "application/json"},
                                      body=json.dumps(body or {}))
        raw = response.read()
        record = {"path": path, "request": body, "status": response.status,
                  "headers": dict(response.getheaders()), "body": json.loads(raw)}
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
        from arete import record

        old = next((r for r in self.rows("SELECT * FROM runs") if r["id"] in command), None)
        assert old, command
        child = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.children.append(child)
        record.start("pipeline", repo=old["repo"], issue=old["issue"], pid=child.pid)
        return child

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
                                                            for t in tables}, indent=2))


def refuses(kind, action):
    try:
        action()
    except kind as e:
        return e
    raise AssertionError(f"expected {kind.__name__}")


def migration(root):
    from dataclasses import replace

    from arete.store import db, migrations

    chain = migrations.load()
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
    db.migrate()
    db.migrate()
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
    with db.tx() as c:
        refuses(sqlite3.IntegrityError, lambda: c.execute(
            "INSERT INTO idempotency (actor,key,route,request_sha,status,response,at) VALUES ('hand','key','r','s',0,'','bad')"))
        refuses(sqlite3.IntegrityError, lambda: c.execute(
            "UPDATE schema_meta SET value = 'bad' WHERE key = 'versioned_api_grace_deadline'"))
    assert migrations.owners()["writes"] == {"idempotency": "30 days"}
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
    attempts = []

    def restarted(method, path, body, *args):
        attempts.append((method, path, json.dumps(body, sort_keys=True)))
        if len(attempts) == 1:
            raise client.Restarting({"error": "restart"}, 0)
        return SimpleNamespace(read=lambda: b"{}")

    with mock.patch.object(client, "request_once", side_effect=restarted):
        client.call("POST", stop_path(run), {"base_version": selected})
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    ui = NewUIShellTests()
    ui.test_versioned_run_actions()


def seed_serve(f):
    ids = {"stop": f.made("live", "running", live=True), "retry": f.made("retry"),
           "repair": f.made("repair", pr_url="https://example.test/fixture/pull/1", branch="fixture"),
           "step": f.made("step")}
    (f.root / "seed.json").write_text(json.dumps(ids, indent=2))
    port = f.server.server_port
    for label, run in ids.items():
        print(f"{label}: {run} http://127.0.0.1:{port}/#live/run/{run} "
              f"http://127.0.0.1:{port}/ui.html#run/{run}", flush=True)
    print(f"Live: http://127.0.0.1:{port}/#live; evidence: {f.root}", flush=True)


def child(name, root, port):
    from arete import accounts, control, gh, principals, repairs, tasks

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
                               "carried-landings": carried_landings, "clients": clients}
                    if name == "serve":
                        seed_serve(f)
                        f.save()
                        with mock.patch.object(tasks, "free_to_start"), mock.patch.object(repairs, "open_heads"), \
                                mock.patch.object(control, "launch", side_effect=f.fake_launch):
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
    parser.add_argument("--child", choices=(*EXERCISES, "serve"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.child:
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
            for name in ("result.json", "seed.json", "migration.json", "stdout.txt", "stderr.txt"):
                path = root / name
                if path.exists():
                    print(f"{name}: {path.read_text()[-5000:]}")
    else:
        parser.error("choose exercise NAME, serve, or inspect")
