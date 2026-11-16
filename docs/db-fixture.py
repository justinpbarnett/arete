from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / ".arete-sandbox" / "db-fixture"
ACTIONS = ("transactions", "concurrent-intake", "transaction-timing", "output-sinks", "durable-files")
sys.path.insert(0, str(REPO))


def require(value, detail):
    if not value:
        raise AssertionError(detail)


def environment(folder):
    env = dict(os.environ)
    env.pop("ARETE_VERIFY_SCRATCH", None)
    env.pop("ARETE_RUN_ID", None)
    for name, part in (("ARETE_CONFIG_DIR", "config"), ("ARETE_STATE_DIR", "state"),
                       ("ARETE_LOG_DIR", "logs"), ("ARETE_WORKTREES_DIR", "worktrees"),
                       ("TMPDIR", "tmp"), ("HOME", "home")):
        target = folder / part
        target.mkdir(parents=True, exist_ok=True)
        env[name] = str(target)
    return env


def child(role, *args):
    return subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--child", role, *map(str, args)],
                            cwd=REPO, env=dict(os.environ), text=True, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finish(process, timeout=30):
    try:
        out, err = process.communicate(timeout=timeout)
        require(process.returncode == 0, {"exit": process.returncode, "out": out, "err": err})
        return out
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def wait_file(path, process=None):
    deadline = time.monotonic() + 15
    while not path.exists():
        require(time.monotonic() < deadline, f"barrier timed out: {path}")
        require(process is None or process.poll() is None, f"child exited before {path}")
        time.sleep(.01)


def setup():
    from arete import paths, state

    require(not paths.ISOLATED, "rollback evidence must use ordinary disposable SQLite files")
    require(ROOT in paths.DB_FILE.resolve().parents, "refusing non-fixture database")
    paths.ensure_dirs()
    paths.CONFIG_FILE.write_text('[replay]\nall = true\n[jev]\nenabled = false\n')
    state.migrate()
    with state.one_read() as c:
        journal = c.execute("PRAGMA journal_mode").fetchone()[0]
        sync = c.execute("PRAGMA synchronous").fetchone()[0]
    require(journal == "delete" and sync == 2, (journal, sync))
    return {"journal_mode": journal, "synchronous": sync}


def expect(error, fn):
    try:
        fn()
    except error as e:
        return type(e).__name__
    raise AssertionError(f"expected {error}")


def parallel(fn, count=4):
    gate = threading.Barrier(count)

    def run(i):
        gate.wait(10)
        return fn(i)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def transactions():
    from arete import backup, doctor, paths, state

    evidence = setup()
    trace, connections = [], []
    original = paths.connect_db

    def connect(*args, **kw):
        c = original(*args, **kw)
        c.set_trace_callback(trace.append)
        connections.append({"readonly": kw.get("readonly", False), "id": id(c)})
        return c

    with mock.patch.object(paths, "connect_db", connect):
        with state.tx() as c:
            item = state.enqueue("nested", "default", "", "fixture")
            with state.db() as joined, state.reader() as read:
                require(c is joined is read, "writer ownership was not shared")
                require(state.queue_item(item)["loop"] == "nested", "joined helper lost uncommitted write")
                require(c.in_transaction, "nested exit committed")
            require(c.in_transaction, "nested scope ended ownership")
    require(len(connections) == 1, connections)
    require(trace.count("BEGIN IMMEDIATE") == trace.count("COMMIT") == 1, trace)
    require(not any(s.lstrip().upper().startswith(("CREATE", "ALTER", "DROP")) for s in trace), trace)
    evidence["nested_writer"] = {"connections": connections[:], "trace": trace[:]}
    event = {"key": "rollback", "type": "fixture", "title": "rollback", "data": {}}
    for error in (ValueError, KeyboardInterrupt):
        try:
            with state.tx():
                state.enqueue("rollback", "default", "", "fixture")
                state.add_event("rollback", event)
                raise error("rollback")
        except error:
            pass
        with state.one_read() as c:
            require(c.execute("SELECT count(*) FROM queue WHERE loop='rollback'").fetchone()[0] == 0, "queue committed")
            require(c.execute("SELECT count(*) FROM events WHERE loop='rollback'").fetchone()[0] == 0, "event committed")
        state.set_cursor("after-error", "ok", True)

    class CommitFailure(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("fixture commit")
            return super().execute(sql, *args)

    def failing():
        return sqlite3.connect(paths.DB_FILE, factory=CommitFailure)

    def failed_write():
        with state.tx():
            state.enqueue("commit-failure", "default", "", "fixture")

    with mock.patch.object(state, "checked_connection", failing):
        expect(sqlite3.OperationalError, failed_write)
    require(not any(r["loop"] == "commit-failure" for r in state.pending()), "failed commit persisted")
    parallel(lambda i: state.set_cursor(f"thread-{i}", "ok", True))
    trace.clear()
    connections.clear()
    ready, done = threading.Event(), threading.Event()
    faults = []

    def writer():
        try:
            ready.set()
            state.set_cursor("snapshot", "new", True)
            done.set()
        except BaseException as e:
            faults.append(str(e))

    state.set_cursor("snapshot", "old", True)
    with mock.patch.object(paths, "connect_db", connect):
        with state.one_read() as c, state.reader() as nested:
            require(c is nested, "read snapshots did not join")
            require(state.cursor("snapshot", state.reader)["value"] == "old", "initial snapshot")
            before = len(connections)
            for scope in (state.tx, state.db):
                expect(RuntimeError, lambda: scope().__enter__())
            require(len(connections) == before, "promotion opened a connection")
            worker = threading.Thread(target=writer)
            worker.start()
            require(ready.wait(5), "writer did not start")
            time.sleep(.15)
            require(not done.is_set(), "writer committed through a read snapshot")
            require(state.cursor("snapshot", state.reader)["value"] == "old", "snapshot changed")
        worker.join(10)
        require(not worker.is_alive() and done.is_set() and not faults, faults)
    evidence["snapshot"] = {"connections": connections[:], "trace": trace[:]}
    require(connections[0]["readonly"], "reader was not mode=ro")
    trace.clear()
    with mock.patch.object(paths, "connect_db", connect):
        report = doctor.run()
    require(not any("within a transaction" in r.detail for r in report), [r.detail for r in report])
    require(not any(s.upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in trace), trace)
    evidence["doctor"] = [r.__dict__ for r in report]
    state.record_refresh("old-generation", "new-generation", "old-auth", "new-auth")
    state.hold("old-auth", "", "fixture")
    require({r["scope"] for r in state.holds()} >= {"old-auth", "new-auth"}, "refresh did not propagate")
    state.clear_holds(["new-auth"], "9999")
    require(not state.holds(), "linked holds were not cleared")
    evidence["capture"] = captures()
    retry = state.enqueue("retry", "default", "", "fixture", [event])
    require(state.take_queue_item(retry, "missing-run"), "claim failed")
    state.release_settled_claims()
    require(state.queue_item(retry)["run_id"] is None, "settled claim not released")
    state.take_queue_item(retry, "dead-run")
    state.end_claim("dead-run", "dead", "fixture")
    require(any(r["id"] == retry for r in state.dead_items()), "dead item lost")
    state.requeue_item(retry)
    require(state.queue_item(retry)["attempts"] == 0, "requeue did not reset attempts")
    staged = paths.STATE_DIR / "staged.db"
    shutil.copyfile(paths.DB_FILE, staged)
    with state.tx() as live:
        live_before = backup.sequences(live)
        backup.raise_sequences(staged, {"queue": 100000})
        require(backup.sequences(live) == live_before, "restore touched live database")
        require(state.enqueue("after-restore", "default", "", "fixture") < 100000, "live sequence changed")
        with sqlite3.connect(staged) as c:
            require(backup.sequences(c)["queue"] >= 100000, "staged sequence did not rise")
    backup.raise_sequences(staged, None)
    with sqlite3.connect(staged) as c:
        require(backup.sequences(c)["queue"] >= 100000 + backup.RESTORE_GAP, "restore gap lost")
    evidence["migration"] = migration_exercise()
    evidence["oncall"] = oncall_exercise()
    evidence["confined_scratch"] = confined_exercise()
    evidence["cleanup"] = "ordinary exceptions, BaseException, commit errors and another thread recovered"
    return evidence


def captures():
    from arete import state

    require(state.capture_discard() is None, "empty discard")
    require(state.capture_save(text="fixture"), "save")
    with state.reader() as c:
        draft = dict(c.execute("SELECT * FROM captures WHERE status='draft'").fetchone())
    key, revision = draft["id"], draft["revision"]
    require(not state.capture_move(key, revision + 1, "draft", "checking"), "stale move")
    require(state.capture_move(key, revision, "draft", "checking"), "move")
    require(state.capture_begin_checks(key, {"fixture": "digest"}), "begin checks")
    require(state.capture_check_done(key, "fixture", "pass", "", {}, .1), "check result")
    require(not state.capture_check_done(key, "fixture", "pass", "", {}, .1), "repeated result")
    state.capture_end_checks(key, "fixture")
    row = {"sha256": "a" * 64, "kind": "text", "name": "fixture.txt", "type": "text/plain", "bytes": 5}
    calls = []
    held, same = state.capture_stage(row, lambda *a: calls.append("admit"), lambda *a: calls.append("keep"))
    again, duplicate = state.capture_stage(row, lambda *a: calls.append("admit"), lambda *a: calls.append("keep"))
    require(not same and duplicate and held["id"] == again["id"] and calls == ["admit", "keep", "admit"], calls)
    require(state.capture_inspected(held["id"], row["sha256"], {"ok": True}, .1), "inspect")
    require(not state.capture_inspected(held["id"], "stale", {}, .1), "stale inspect")
    require(state.capture_drop_evidence(held["id"])["id"] == held["id"], "drop")
    require(state.capture_drop_evidence(held["id"]) is None, "repeated drop")
    with state.reader() as c:
        revision = c.execute("SELECT revision FROM captures WHERE id=?", (key,)).fetchone()[0]
    require(state.capture_move(key, revision, "draft", "filing"), "filing")
    require(not state.capture_save(text="blocked"), "filing save")
    require(state.capture_filed(key, 1, "offline/1", "filed"), "filed")
    require(not state.capture_filed(key, 1, "offline/1", "filed"), "repeat filed")
    state.capture_save(text="next")
    require(state.capture_discard() is not None and state.capture_discard() is None, "discard")
    return {"writers": 10, "stale_and_noop": True, "callbacks": calls}


def migration_exercise():
    from arete import backup, migrations, paths, state, util

    original = paths.DB_FILE
    chain = migrations.load()
    folder = paths.STATE_DIR / "migration-chain"
    folder.mkdir()
    for file in migrations.FOLDER.glob("*.py"):
        if file.name.startswith("m"):
            shutil.copyfile(file, folder / file.name)
    sql = ("CREATE TABLE fixture_migration (value TEXT);"
           "CREATE TRIGGER fixture_trigger AFTER INSERT ON fixture_migration "
           "WHEN new.value = 'quoted;value' BEGIN UPDATE fixture_migration SET value='trigger;ok'; END;"
           "-- trailing comment\nINSERT INTO fixture_migration VALUES ('quoted;value')")
    good = f"m{len(chain) + 1:04}_work_fixture.py"
    bad = f"m{len(chain) + 2:04}_work_failure.py"
    (folder / good).write_text(f"PHASE = 'expand'\nSQL = {sql!r}\n")
    (folder / bad).write_text("PHASE = 'expand'\nSQL = \"CREATE TABLE must_rollback (id); "
                              "INSERT INTO fixture_migration VALUES ('rollback'); SELECT * FROM absent;\"\n")
    (folder / "latest.txt").write_text(bad + "\n")
    temporary = migrations.load(folder)
    ready, done = paths.STATE_DIR / "migration-ready", paths.STATE_DIR / "migration-done"
    with util.lock("migrate", wait=True):
        migrator = child("migrate-marker", ready, done)
        try:
            wait_file(ready, migrator)
            time.sleep(.15)
            require(not done.exists() and migrator.poll() is None, "migration lock did not exclude another migrator")
        except BaseException:
            migrator.kill()
            migrator.communicate(timeout=10)
            raise
    finish(migrator)
    require(done.exists(), "migrator did not recover after lock release")
    try:
        paths.DB_FILE = paths.STATE_DIR / "migration.db"
        with mock.patch.object(migrations, "load", lambda: temporary):
            expect(migrations.Refused, state.migrate)
        with sqlite3.connect(paths.DB_FILE) as c:
            require(c.execute("PRAGMA user_version").fetchone()[0] == len(chain) + 1, "version not atomic")
            require(c.execute("SELECT value FROM fixture_migration").fetchall() == [("trigger;ok",)], "data not atomic")
            require(not c.execute("SELECT 1 FROM sqlite_master WHERE name='must_rollback'").fetchone(), "DDL survived")
            require(c.execute("SELECT max(n) FROM schema_migrations").fetchone()[0] == len(chain) + 1, "ledger not atomic")
            require(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
                    == str(len(chain) + 1), "schema_meta not atomic")
        failed = replace(temporary[-1], sql=None, apply=lambda c: (
            c.execute("CREATE TABLE apply_rollback (id)"), c.execute("SELECT * FROM absent")))
        with mock.patch.object(migrations, "load", lambda: [*temporary[:-1], failed]):
            expect(migrations.Refused, state.migrate)
        with sqlite3.connect(paths.DB_FILE) as c:
            require(not c.execute("SELECT 1 FROM sqlite_master WHERE name='apply_rollback'").fetchone(), "apply survived")
        snapshots = backup.snapshots(paths.backups_dir())
        require(snapshots and backup.inspect(snapshots[-1])["quick_check"] == "ok", "migration backup lost")
        contract = replace(temporary[-1], phase="contract", sql="CREATE TABLE gated_contract (id)")
        with mock.patch.object(migrations, "load", lambda: [*temporary[:-1], contract]):
            require(state.migrate(contracts=False), "no-contracts gate ignored")
            with mock.patch.object(state, "contract_blockers", return_value=["fixture-live-run"]):
                require(state.migrate(), "active-writer contract gate ignored")
        with sqlite3.connect(paths.DB_FILE) as c:
            require(not c.execute("SELECT 1 FROM sqlite_master WHERE name='gated_contract'").fetchone(), "gate applied SQL")
        return {"trigger_and_quoted_semicolon": True, "sql_and_apply_atomicity": True,
                "version": len(chain) + 1, "temporary_chain": str(folder),
                "contract_gates": True, "migration_lock": True, "backups": [str(p) for p in snapshots]}
    finally:
        paths.DB_FILE = original


def confined_exercise():
    from arete import paths

    root = paths.STATE_DIR / (paths.SCRATCH_PREFIX + "1234567890abcdef")
    root.mkdir(mode=0o700)
    file = root / "scratch.db"
    if not paths.ON_MAC:
        with mock.patch.object(paths, "ISOLATED", True):
            expect(PermissionError, lambda: paths.connect_db(file, create=True))
        return {"unsupported_platform_refused": True}
    with mock.patch.object(paths, "ISOLATED", True):
        c = paths.connect_db(file, create=True)
        try:
            require(c.execute("PRAGMA journal_mode").fetchone()[0] == "memory", "scratch journal changed")
        finally:
            c.close()
    wal = root / "wal.db"
    with sqlite3.connect(wal) as c:
        require(c.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal", "fixture WAL setup failed")
        c.execute("CREATE TABLE fixture (id)")
    c.close()
    with mock.patch.object(paths, "ISOLATED", True):
        expect(PermissionError, lambda: paths.connect_db(wal))
    return {"journal_mode": "memory", "wal_refused": True}


def oncall_exercise():
    from arete import config, oncall, serve, state, util

    cfg = config.load()
    cfg.oncall = replace(cfg.oncall, enabled=True)
    order = []
    failure = ValueError("scan fixture")

    @contextlib.contextmanager
    def lock(*args, **kw):
        order.append("acquired")
        try:
            yield True
        finally:
            order.append("released")

    real = oncall.crashed

    def crashed(text):
        require(order[-1] == "acquired", order)
        order.append("crashed")
        real(text)

    with mock.patch.object(util, "lock", lock), mock.patch.object(oncall, "crashed", crashed):
        with mock.patch.object(oncall, "scan_once", side_effect=failure):
            try:
                oncall.scan(cfg)
            except ValueError as e:
                require(e is failure, "original failure replaced")
        require(order == ["acquired", "crashed", "released"], order)
    require(not state.cursor(oncall.SCAN)["ok"], "crash unrecorded")
    def successful(cfg):
        state.set_cursor(oncall.SCAN, "success", True)
        return {"ok": True}

    with mock.patch.object(oncall, "scan_once", successful):
        require(oncall.scan(cfg) == {"ok": True}, "subsequent scan failed")
    require(state.cursor(oncall.SCAN)["ok"], "successful scan remained crashed")
    with mock.patch.object(oncall, "scan_once", side_effect=failure), \
            mock.patch.object(oncall, "crashed", side_effect=OSError("crash record unavailable")):
        try:
            oncall.scan(cfg)
        except ValueError as e:
            require(e is failure, "crash-record failure replaced scan exception")
    with mock.patch.object(util, "lock", return_value=contextlib.nullcontext(False)):
        require("skipped" in oncall.scan(cfg), "busy scan changed")
    cfg.oncall = replace(cfg.oncall, enabled=False)
    require(oncall.scan(cfg) is None, "disabled scan changed")
    said = []
    server = SimpleNamespace(oncall_error="", say=said.append)
    with mock.patch.object(oncall, "scan", side_effect=failure), mock.patch.object(oncall, "crashed") as crash:
        serve.Server.scan_oncall(server, cfg)
        serve.Server.scan_oncall(server, cfg)
        require(len(said) == 1 and not crash.called, "serve duplicated crash reporting")
    return {"order": order, "serve_messages": said}


def concurrent_intake():
    from arete import config, loops, paths, sources, state, util, webhooks

    evidence = setup()
    loop = loops.Loop("intake", paths.CONFIG_DIR, "fixture", trigger=loops.Trigger(webhook=True),
                      sources=[{"type": "command", "command": "fake", "name": "feed"}])
    cfg = config.load()
    source = sources.SOURCES["command"]
    sid = sources.source_id(loop, 0)
    event = sources.event("command", sid, "baseline", "baseline")
    with mock.patch.object(source, "fetch", return_value=([event], "baseline")):
        require(sources.poll(loop, 0, cfg) == ([], []), "first poll did not establish baseline")
    event = sources.event("command", sid, "new", "new")
    with mock.patch.object(source, "fetch", return_value=([event], "next")):
        results = parallel(lambda _: sources.poll(loop, 0, cfg))
    require(sum(len(r[1]) for r in results) == 1, results)
    empty = parallel(lambda _: sources.fire(loop, "empty"))
    require(sum(len(r) for r in empty) == 1, empty)
    with mock.patch.object(loops, "load_all", return_value=[loop]), mock.patch.object(webhooks, "check"):
        hooks = parallel(lambda _: webhooks.receive(loop.name, {"X-Request-Id": "delivery"}, b'{"title":"hook"}'))
        require(sum(r["queued"] for r in hooks) == 1, hooks)
        first = webhooks.receive(loop.name, {}, b'{"title":"body"}')
        duplicate = webhooks.receive(loop.name, {}, b'{"title":"body"}')
        require(first["queued"] and not duplicate["queued"], "body key did not deduplicate")
        with state.tx() as c:
            c.execute("UPDATE seen SET ts=? WHERE source=? AND key=?",
                      (util.iso(util.now() - timedelta(minutes=11)), loop.name + "#webhook", first["key"]))
        require(webhooks.receive(loop.name, {}, b'{"title":"body"}')["queued"], "body key did not expire")
    before = state.cursor(sid)
    owner = state.source_owner(sid)
    event = sources.event("command", sid, "retry", "retry")
    with mock.patch.object(source, "fetch", return_value=([event], "retry")):
        with mock.patch.object(state, "enqueue", side_effect=ValueError("after seen")):
            expect(ValueError, lambda: sources.poll(loop, 0, cfg))
        require(state.cursor(sid) == before and state.source_owner(sid) == owner, "failed poll partially committed")
        with state.reader() as c:
            require(not c.execute("SELECT 1 FROM seen WHERE source=? AND key='retry'", (sid,)).fetchone(), "seen leaked")
            require(not c.execute("SELECT 1 FROM events WHERE key='retry'").fetchone(), "events leaked")
        require(len(sources.poll(loop, 0, cfg)[1]) == 1, "retry not enqueued")
        require(not sources.poll(loop, 0, cfg)[1], "retry duplicated")
    fetching, release = threading.Event(), threading.Event()

    def fetch(*a, **kw):
        fetching.set()
        require(release.wait(10), "fetch barrier timed out")
        return [], "after-barrier"

    with ThreadPoolExecutor(max_workers=1) as pool, mock.patch.object(source, "fetch", fetch):
        future = pool.submit(sources.poll, loop, 0, cfg)
        try:
            require(fetching.wait(5), "fetch did not start")
            state.set_cursor("network-independent", "ok", True)
        finally:
            release.set()
        future.result(timeout=10)
    state.set_cursor("serialized", "old", True, started="2026-01-01T00:00:00Z")
    ready = threading.Event()

    def failed_poll():
        ready.set()
        state.set_cursor("serialized", None, False)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with state.tx():
            future = pool.submit(failed_poll)
            require(ready.wait(5), "cursor waiter not started")
            state.set_cursor("serialized", "new", True, started="2026-01-01T01:00:00Z")
        future.result(timeout=10)
    row = state.cursor("serialized")
    require(row["value"] == "new" and row["last_ok"] == "2026-01-01T01:00:00Z" and not row["ok"], row)
    old = replace(loop, name="legacy", sources=[{"type": "command", "command": "fake"}])
    state.set_cursor("legacy#0", "history", True)
    state.set_source_owner("legacy#0", sources.legacy_fingerprint(old.sources[0]))
    named = replace(old, sources=[{**old.sources[0], "name": "named"}])
    sources.adopt_history(named, 0, named.sources[0], "legacy#named")
    require(state.cursor("legacy#named")["value"] == "history", "named adoption lost history")
    changed = replace(named, sources=[{**named.sources[0], "name": "edited", "timeout": 3}])
    sources.adopt_history(changed, 0, changed.sources[0], "legacy#edited")
    require(state.cursor("legacy#edited") is None, "name plus settings edit adopted legacy history")
    evidence.update({"source_polls": results, "empty_fire": empty, "webhooks": hooks,
                     "cursor_serialization": row, "rollback_and_retry": True, "fetch_outside_tx": True,
                     "identity_adoption": True, "pending": state.pending()})
    return evidence


def log_rows():
    from arete import paths

    file = paths.LOG_DIR / "db.log"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []


def transaction_timing():
    from arete import dbstats, paths, state

    evidence = setup()
    initial_rows = log_rows()
    with state.tx():
        with state.db():
            pass
    require(log_rows() == initial_rows, "fast success wrote diagnostics")
    before = sum(g["count"] for s in dbstats.SUMMARIES.values() for g in s["transactions"].values())
    with state.tx():
        with state.db(), state.reader():
            pass
    after = sum(g["count"] for s in dbstats.SUMMARIES.values() for g in s["transactions"].values())
    require(after == before + 1, "nested scopes were counted")
    ready = paths.STATE_DIR / "writer-ready"
    release = paths.STATE_DIR / "writer-release"
    holder = child("hold-writer", ready, release)
    try:
        wait_file(ready, holder)
        waiter = child("timed-writer")
        try:
            time.sleep(6)
            release.touch()
            finish(holder)
            finish(waiter)
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.communicate(timeout=10)
    finally:
        release.touch()
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=10)
    rows = log_rows()
    waited = [r for r in rows if r["pid"] == waiter.pid]
    require(len(waited) == 1 and waited[0]["begin_ms"] >= 5000, rows)
    require(waited[0]["caller"].endswith(".timed_writer"), waited)
    marker = paths.STATE_DIR / "commit-ready"
    with state.one_read() as c:
        c.execute("SELECT count(*) FROM cursors").fetchone()
        committer = child("commit-writer", marker)
        try:
            wait_file(marker, committer)
            time.sleep(.7)
            require(committer.poll() is None, "COMMIT did not wait for reader")
        except BaseException:
            committer.kill()
            committer.communicate(timeout=10)
            raise
    finish(committer)
    committed = [r for r in log_rows() if r["pid"] == committer.pid]
    require(len(committed) == 1 and committed[0]["commit_ms"] >= 600, committed)
    previous = len(log_rows())
    with state.tx():
        with state.tx(), state.reader():
            time.sleep(.6)
    require(len(log_rows()) == previous + 1, "slow nesting duplicated diagnostics")

    class Broken(sqlite3.Connection):
        fail = ""

        def execute(self, sql, *a):
            if sql == self.fail:
                raise sqlite3.OperationalError("PRIVATE_SQL_PAYLOAD")
            return super().execute(sql, *a)

    def write():
        with state.tx() as c:
            c.execute("SELECT * FROM absent_PRIVATE_SQL_PAYLOAD")

    failures = []
    for phase in ("acquisition", "begin", "body", "commit", "slow-body"):
        previous = len(log_rows())

        def connect():
            if phase == "acquisition":
                raise sqlite3.OperationalError("PRIVATE_SQL_PAYLOAD")
            c = sqlite3.connect(paths.DB_FILE, factory=Broken)
            c.fail = {"begin": "BEGIN IMMEDIATE", "commit": "COMMIT"}.get(phase, "")
            return c

        def operation():
            with state.tx() as c:
                if phase == "slow-body":
                    time.sleep(.6)
                if phase in ("body", "slow-body"):
                    c.execute("SELECT * FROM absent_PRIVATE_SQL_PAYLOAD")

        with mock.patch.object(state, "checked_connection", connect):
            expect(sqlite3.OperationalError, operation)
        require(len(log_rows()) == previous + 1, f"{phase}: missing or duplicate record")
        failures.append({"phase": phase, "record": log_rows()[-1]})
    for function in ("open", "write"):
        with mock.patch.object(dbstats.os, function, side_effect=OSError("diagnostic unavailable")):
            with state.tx():
                time.sleep(.51)
            expect(sqlite3.OperationalError, write)
    children = [child("timed-error") for _ in range(4)]
    for process in children:
        finish(process)
    rows = log_rows()
    required = {"timestamp", "pid", "process_kind", "caller", "mode", "total_ms", "begin_ms", "commit_ms", "error"}
    for row in rows:
        require(set(row) == required, row)
        require(row["mode"] in ("read", "write") and row["begin_ms"] <= row["total_ms"], row)
        require("PRIVATE_SQL_PAYLOAD" not in json.dumps(row), "payload leaked")
    evidence.update({"begin_wait": waited, "commit_wait": committed, "errors": failures,
                     "diagnostics": rows, "summaries": summary_exercise()})
    return evidence


def summary_exercise():
    from arete import dbstats, paths, state

    def count():
        return sum(g["count"] for s in dbstats.SUMMARIES.values() for g in s["transactions"].values())

    before = count()
    parallel(lambda i: state.set_cursor(f"summary-{i}", "fast", True), count=8)
    require(count() == before + 8, "thread summary updates lost")
    identity = dbstats.database_identity(paths.DB_FILE)
    other = paths.STATE_DIR / "other.db"
    shutil.copyfile(paths.DB_FILE, other)
    require(dbstats.database_identity(other) != identity, "database paths were mixed")
    original = paths.DB_FILE
    try:
        paths.DB_FILE = other
        with state.tx():
            pass
    finally:
        paths.DB_FILE = original
    replacement = paths.STATE_DIR / "replacement.db"
    shutil.copyfile(original, replacement)
    os.replace(replacement, original)
    require(dbstats.database_identity(original) != identity, "replacement identity was reused")
    with state.tx():
        pass
    clock = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    with mock.patch.object(dbstats, "datetime") as fake:
        fake.now.return_value = clock
        with state.tx():
            pass
    incarnation = dbstats.INCARNATION
    forked = os.fork()
    if forked == 0:
        try:
            require(dbstats.INCARNATION != incarnation and not dbstats.SUMMARIES, "fork inherited summary identity")
            with state.tx():
                pass
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(forked, 0)
    require(status == 0, "fork summary failed")
    processes = [child("timed-writer") for _ in range(2)]
    for process in processes:
        finish(process)
    files = list((paths.LOG_DIR / "dbstats").glob("*.json"))
    summaries = [json.loads(p.read_text()) for p in files]
    tokens = {s["incarnation"] for s in summaries}
    require(len(tokens) >= 4, "process restarts or fork mixed identities")
    require(len({s["hour"] for s in summaries}) >= 2, "hours were mixed")
    for process in processes:
        require(any(s["pid"] == process.pid for s in summaries), "short-lived process failed to publish")
    for summary in summaries:
        for group in summary["transactions"].values():
            require(sum(group["histogram"].values()) == group["count"], "histogram count mismatch")
    return {"files": [str(p) for p in files], "records": summaries}


def output_sinks():
    from arete import devices, loop_run, paths, util

    evidence = setup()
    code = "import sys; [print(str(i) + ':' + 'x'*100) for i in range(3000)]; print('FINAL_SENTINEL'); sys.exit(7)"
    expected = "".join(f"{i}:" + "x" * 100 + "\n" for i in range(3000)) + "FINAL_SENTINEL\n"
    cases = {}
    real_open = open
    for failure in ("healthy", "callback", "open", "write", "flush", "both"):
        logfile = paths.LOG_DIR / f"output-{failure}.log"
        attempts = {"callback": 0, "write": 0, "flush": 0}

        def on_line(line):
            attempts["callback"] += 1
            if failure in ("callback", "both") and attempts["callback"] >= 10:
                raise ValueError("callback fixture")

        class Log:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def write(self, text):
                attempts["write"] += 1
                if failure in ("write", "both") and attempts["write"] >= 10:
                    raise OSError("write fixture")
                return self.handle.write(text)

            def flush(self):
                attempts["flush"] += 1
                if failure == "flush" and attempts["flush"] >= 10:
                    raise OSError("flush fixture")
                return self.handle.flush()

            def close(self):
                self.handle.close()

        def opening(path, *a, **kw):
            if path == logfile:
                if failure == "open":
                    raise OSError("opening fixture")
                return Log(real_open(path, *a, **kw))
            return real_open(path, *a, **kw)

        stderr = io.StringIO()
        started = time.monotonic()
        with mock.patch.object(loop_run, "open", opening, create=True), contextlib.redirect_stderr(stderr):
            status, text = loop_run.run_command([sys.executable, "-c", code], REPO, timeout=60, env={},
                                                 device=devices.LOCAL, output=logfile, on_line=on_line)
        require(status == 7 and text == expected, {"case": failure, "status": status, "tail": text[-200:]})
        require(time.monotonic() - started < 30, "sink failure stopped pipe draining")
        require(attempts["callback"] == (10 if failure in ("callback", "both") else 3001), attempts)
        if failure in ("write", "both"):
            require(attempts["write"] == 10, "disabled log was retried")
        if failure == "flush":
            require(attempts["flush"] == 10, "disabled flush was retried")
        log = logfile.read_text() if logfile.exists() else ""
        diagnostics = (log + stderr.getvalue()).count("[arete] feed stopped:")
        require(diagnostics == (0 if failure == "healthy" else 2 if failure == "both" else 1), diagnostics)
        if failure in ("healthy", "callback"):
            require("FINAL_SENTINEL" in log and len(log.splitlines()) == 3001 + diagnostics, "healthy log lost output")
        cases[failure] = {"status": status, "lines": len(text.splitlines()), "attempts": attempts,
                          "diagnostics": diagnostics, "stderr": stderr.getvalue(), "log": str(logfile)}
    timeout_log = paths.LOG_DIR / "timeout.log"
    status, text = loop_run.run_command(
        [sys.executable, "-u", "-c", "import time; print('before timeout', flush=True); time.sleep(60)"],
        REPO, timeout=2, env={}, device=devices.LOCAL, output=timeout_log)
    require(status == util.TIMED_OUT and text == "before timeout\n\n[arete] stopped after 2s\n", text)
    require(timeout_log.read_text() == text, "timeout note lost")
    with mock.patch.object(loop_run, "open", side_effect=OSError("disabled timeout sink"), create=True) as opening, \
            contextlib.redirect_stderr(io.StringIO()):
        disabled_status, disabled_text = loop_run.run_command(
            [sys.executable, "-u", "-c", "import time; print('before timeout', flush=True); time.sleep(60)"],
            REPO, timeout=2, env={}, device=devices.LOCAL, output=timeout_log)
        require(opening.call_count == 1, "timeout reopened disabled log")
    require(disabled_status == util.TIMED_OUT and disabled_text == text, "failed sink changed timeout result")
    evidence.update({"cases": cases, "timeout": {"status": status, "text": text}})
    return evidence


def durable_files():
    from arete import paths, pipeline, util

    evidence = setup()
    folder = paths.STATE_DIR / "durable"
    folder.mkdir()
    file = folder / "value.json"
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        require(util.read_json(file, "missing") == "missing", "missing default lost")
        require(not stderr.getvalue(), "missing file warned")
        file.write_bytes(b"bad!")
        stamp = file.stat().st_mtime_ns
        for _ in range(3):
            require(util.read_json(file, None) is None, "corrupt default lost")
        require(stderr.getvalue().count("not valid JSON") == 1, stderr.getvalue())
        replacement = folder / "replacement"
        replacement.write_bytes(b"bad?")
        os.utime(replacement, ns=(stamp, stamp))
        os.replace(replacement, file)
        util.read_json(file, None)
        file.write_bytes(b"bad#")
        os.utime(file, ns=(stamp, stamp))
        util.read_json(file, None)
        require(stderr.getvalue().count("not valid JSON") == 3, stderr.getvalue())
    util.write_json(file, {"valid": True})
    require(util.read_json(file, None) == {"valid": True}, "valid replacement unreadable")
    require(file.stat().st_mode & 0o777 == 0o600, "JSON mode")
    run = SimpleNamespace(dir=folder)
    pipeline.Run.save(run, "text.txt", "saved text")
    pipeline.Run.save(run, "saved.json", {"saved": True})
    require((folder / "text.txt").read_text() == "saved text", "Run.save text")
    require(json.loads((folder / "saved.json").read_text()) == {"saved": True}, "Run.save JSON")
    require((folder / "text.txt").stat().st_mode & 0o777 == 0o600, "Run.save mode")
    order = []
    flush, replace_file, flush_folder = util.flush_fd, os.replace, util.flush_folder

    def flushed(fd):
        order.append("file-sync")
        flush(fd)

    def replaced(src, dst):
        order.append("replace")
        replace_file(src, dst)

    def directory(path):
        order.append("directory-sync")
        flush_folder(path)

    with mock.patch.object(util, "flush_fd", flushed), mock.patch.object(util.os, "replace", replaced), \
            mock.patch.object(util, "flush_folder", directory):
        util.write_json(file, {"ordered": True})
    require(order[:3] == ["file-sync", "replace", "directory-sync"], order)
    import fcntl

    with mock.patch.object(fcntl, "F_FULLFSYNC", 51, create=True), \
            mock.patch.object(fcntl, "fcntl", side_effect=OSError("full sync unavailable")), \
            mock.patch.object(os, "fsync") as fallback:
        util.flush_fd(999)
        require(fallback.call_args == mock.call(999), "full-sync failure did not fall back")
    old_sync = getattr(fcntl, "F_FULLFSYNC", None)
    if old_sync is not None:
        del fcntl.F_FULLFSYNC
    try:
        with mock.patch.object(os, "fsync") as fallback:
            util.flush_fd(999)
            require(fallback.call_args == mock.call(999), "full-sync absence did not fall back")
    finally:
        if old_sync is not None:
            fcntl.F_FULLFSYNC = old_sync
    old = file.read_bytes()
    failures = []
    for phase in ("write", "validation", "sync", "replace"):
        failure = OSError(phase)
        with contextlib.ExitStack() as stack:
            check = None
            if phase == "write":
                real_fdopen = os.fdopen

                def fdopen(fd, mode):
                    handle = real_fdopen(fd, mode)
                    proxy = mock.MagicMock(wraps=handle)
                    proxy.__enter__.return_value = proxy
                    proxy.__exit__.side_effect = lambda *a: handle.close()
                    proxy.write.side_effect = failure
                    return proxy

                stack.enter_context(mock.patch.object(os, "fdopen", fdopen))
            elif phase == "validation":
                check = mock.Mock(side_effect=failure)
            elif phase == "sync":
                stack.enter_context(mock.patch.object(util, "flush_fd", side_effect=failure))
            else:
                stack.enter_context(mock.patch.object(os, "replace", side_effect=failure))
            expect(OSError, lambda: util.write_durably(file, b"new", 0o600, check))
        require(file.read_bytes() == old, f"{phase} replaced destination")
        require(not list(folder.glob(".*.tmp")), f"{phase} left temporary files")
        failures.append(phase)
    with mock.patch.object(util, "flush_folder", side_effect=OSError("directory sync")):
        expect(OSError, lambda: util.write_durably(file, b"replaced", 0o600))
    require(file.read_bytes() == b"replaced" and not list(folder.glob(".*.tmp")), "post-replace sync failure cleanup")
    failures.append("directory-sync-after-replace")
    evidence.update({"corrupt_warnings": stderr.getvalue(), "sync_order": order,
                     "failure_cleanup": failures, "shared_Run_save": True, "mode": "0600"})
    return evidence


def timed_writer():
    from arete import state

    with state.tx():
        state.set_cursor("timed-writer", "ok", True)


def child_main(role, args):
    from arete import paths, state

    require(ROOT in paths.DB_FILE.resolve().parents, "child refuses production database")
    if role in ACTIONS:
        functions = {"transactions": transactions, "concurrent-intake": concurrent_intake,
                     "transaction-timing": transaction_timing, "output-sinks": output_sinks,
                     "durable-files": durable_files}
        result = functions[role]()
        (paths.STATE_DIR.parent / "evidence.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(f"{role}: passed")
    elif role == "hold-writer":
        with state.tx():
            Path(args[0]).touch()
            wait_file(Path(args[1]))
    elif role == "timed-writer":
        timed_writer()
    elif role == "migrate-marker":
        Path(args[0]).touch()
        state.migrate()
        Path(args[1]).touch()
    elif role == "commit-writer":
        with state.tx():
            state.set_cursor("commit-wait", "ok", True)
            Path(args[0]).touch()
    elif role == "timed-error":
        def operation():
            with state.tx() as c:
                c.execute("SELECT * FROM absent_PRIVATE_SQL_PAYLOAD")
        expect(sqlite3.OperationalError, operation)
    else:
        raise ValueError(role)


def inspect_evidence():
    found = {}
    for action in ACTIONS:
        folders = sorted(ROOT.glob(action + "-*"), key=lambda p: p.stat().st_mtime_ns)
        require(folders, f"missing exercise: {action}")
        latest = folders[-1]
        file = latest / "evidence.json"
        require(file.exists(), f"exercise did not pass: {latest}")
        data = json.loads(file.read_text())
        require(data["journal_mode"] == "delete" and data["synchronous"] == 2, file)
        found[action] = {"evidence": str(file), "journal_mode": data["journal_mode"], "synchronous": data["synchronous"]}
    readme = (REPO / "README.md").read_text()
    require("one full day" in readme and "rollback" in readme, "rollout documentation missing")
    (ROOT / "inspect.json").write_text(json.dumps(found, indent=2) + "\n")
    print(json.dumps(found, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", choices=[*ACTIONS, "inspect"])
    parser.add_argument("--child", nargs="+")
    args = parser.parse_args()
    sandbox = REPO / ".arete-sandbox"
    require(Path(os.environ.get("ARETE_STATE_DIR", "")).resolve().is_relative_to(sandbox.resolve()),
            "invoke through the supplied verification sandbox")
    require(not sandbox.is_symlink() and not ROOT.is_symlink(), "linked evidence directory refused")
    if args.child:
        child_main(args.child[0], args.child[1:])
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    if args.action == "inspect":
        inspect_evidence()
        return
    require(args.action in ACTIONS, "choose an exercise")
    folder = Path(tempfile.mkdtemp(prefix=args.action + "-", dir=ROOT))
    env = environment(folder)
    process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--child", args.action],
                               cwd=REPO, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=180)
    except BaseException:
        process.kill()
        process.communicate(timeout=10)
        raise
    (folder / "stdout.log").write_text(out)
    (folder / "stderr.log").write_text(err)
    print(folder)
    print(out, end="")
    if process.returncode:
        print(err, file=sys.stderr)
        raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
