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
ACTIONS = ("transactions", "concurrent-intake", "transaction-timing", "output-sinks", "durable-files",
           "wal-startup", "prune-restore", "restore-safety", "feed-cursors", "backup-health",
           "coherent-reads", "event-contract", "database-health", "legacy-cleanup")
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
    require(journal == "delete" and sync == 1, (journal, sync))
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
    required = {"timestamp", "database", "pid", "process_kind", "caller", "mode", "total_ms", "begin_ms", "commit_ms", "error"}
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


def sql_rows(query, args=(), file=None):
    from arete import paths

    conn = paths.connect_db(file or paths.DB_FILE)
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()


def db_values():
    from arete import backup, state

    with state.one_read() as conn:
        return {"sequences": backup.sequences(conn), "meta": backup.metadata(conn),
                "feed": [tuple(r) for r in conn.execute("SELECT * FROM feed ORDER BY id")]}


def stop_child(process):
    if process.poll() is None:
        process.terminate()
    process.communicate(timeout=10)


def start_fixture_serve():
    from arete import paths

    marker = paths.STATE_DIR.parent / ("serve-" + str(time.time_ns()))
    process = child("serve-live", marker)
    try:
        wait_file(marker, process)
        return process, json.loads(marker.read_text())
    except BaseException:
        stop_child(process)
        raise


def cli_output(*args):
    from arete import cli

    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = cli.main(list(args))
    return result, output.getvalue()


def wal_startup():
    from arete import paths, state, util

    paths.ensure_dirs()
    paths.CONFIG_FILE.write_text('[replay]\nall = true\n[jev]\nenabled = false\n')
    evidence = {"cases": []}
    for legacy in (False, True):
        if legacy:
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(paths.DB_FILE) + suffix).unlink(missing_ok=True)
            conn = sqlite3.connect(paths.DB_FILE)
            conn.executescript((REPO / "tests/fixtures/schema-v0.sql").read_text())
            conn.execute("INSERT INTO runs(id,kind,status,started) VALUES ('seed','loop','done',?)", (util.iso(),))
            conn.commit()
            conn.close()
        else:
            conn = paths.connect_db(paths.DB_FILE, create=True)
            conn.close()
        conn = paths.connect_db(paths.DB_FILE)
        require(conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete", "helper enabled WAL")
        conn.close()
        process, found = start_fixture_serve()
        try:
            require(found["journal_mode"] == "wal" and found["synchronous"] == 1, found)
            require(found["epoch"] == 0 and found["version"] == state.LATEST, found)
            if legacy:
                require(state.run_get("seed", connect=state.reader), "legacy row lost")
            evidence["cases"].append({"legacy": legacy, **found})
        finally:
            stop_child(process)
    reader = paths.connect_db(paths.DB_FILE, readonly=True)
    try:
        reader.execute("BEGIN")
        before = reader.execute("SELECT count(*) FROM feed").fetchone()[0]
        began = time.monotonic()
        with ThreadPoolExecutor(1) as pool:
            pool.submit(state.add_feed, "seed", "note", "outside-reader").result(timeout=1)
        elapsed = time.monotonic() - began
        require(reader.execute("SELECT count(*) FROM feed").fetchone()[0] == before, "reader snapshot changed")
    finally:
        reader.close()
    failed = child("wal-failure")
    failure_output = finish(failed)
    evidence.update(journal_mode="wal", synchronous=1, reader_write_seconds=elapsed, switch_failure=failure_output,
                    confined=confined_connections())
    return evidence


def confined_connections():
    from arete import paths

    if not paths.ON_MAC:
        with mock.patch.object(paths, "ISOLATED", True):
            expect(PermissionError, lambda: paths.connect_db(paths.DB_FILE))
        return {"unsupported_platform_refused": True}
    root = Path(tempfile.mkdtemp(prefix=".arete-scratch-", dir=ROOT))
    target = root.with_name(".arete-scratch-" + os.urandom(8).hex())
    root.rename(target)
    target.chmod(0o700)
    file = target / "arete.db"
    with mock.patch.object(paths, "ISOLATED", True):
        conn = paths.connect_db(file, create=True)
        try:
            require(conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory", "confined journal changed")
            require(conn.execute("PRAGMA synchronous").fetchone()[0] == 1, "confined sync changed")
            expect(PermissionError, lambda: paths.connect_db(file))
        finally:
            conn.close()
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(file) + suffix)
            sidecar.write_bytes(b"fixture")
            expect(PermissionError, lambda: paths.connect_db(file))
            sidecar.unlink()
        linked = target / "linked.db"
        linked.symlink_to(file)
        expect(PermissionError, lambda: paths.connect_db(linked))
        linked.unlink()
        os.link(file, linked)
        expect(PermissionError, lambda: paths.connect_db(file))
        linked.unlink()
        with mock.patch.object(paths, "ON_MAC", False):
            expect(PermissionError, lambda: paths.connect_db(file))
    conn = sqlite3.connect(file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE fixture(value)")
    conn.close()
    with mock.patch.object(paths, "ISOLATED", True):
        expect(PermissionError, lambda: paths.connect_db(file))
    return {"journal_mode": "memory", "synchronous": 1, "sidecars_links_nested_WAL_platform_refused": True}


def fixture_server():
    from arete import serve

    server = serve.Server()
    server.say = lambda text: print(text)
    server.copy_off_machine = lambda file: None
    return server


def close_server(server):
    server.pool.shutdown(wait=True)
    for pool in server.job_pools.values():
        pool.shutdown(wait=True)


def prune_restore():
    from arete import backup, paths, state, util

    evidence = setup()
    old = util.iso(util.now() - timedelta(days=60))
    with state.tx() as conn:
        for rid, kind, cost, at in (("old", "loop", 0, old), ("paid", "loop", 1, old),
                                    ("pipeline", "pipeline", 0, old), ("recent", "loop", 0, util.iso())):
            conn.execute("INSERT INTO runs(id,kind,loop_kind,status,started,cost_usd) VALUES (?,?,'command','done',?,?)",
                         (rid, kind, at, cost))
            state.add_feed(rid, "note", rid)
    baseline_snapshot = backup.snapshot("pre-prune")
    server = fixture_server()
    try:
        before = backup.inspect(paths.DB_FILE)["counts"]
        with mock.patch.object(backup, "snapshot", side_effect=OSError("fixture snapshot failure")):
            server.prune()
        require(backup.inspect(paths.DB_FILE)["counts"] == before, "failed snapshot pruned")
        server.prune()
        require(state.run_get("paid") and state.run_get("pipeline") and state.run_get("recent"), "eligibility changed")
        with mock.patch.object(state, "FIRINGS", "(1 = 1)"):
            server.prune()
        snapshot = baseline_snapshot
        require(backup.inspect(snapshot)["quick_check"] == "ok", "unchecked snapshot")
        expected = sql_rows("SELECT * FROM runs ORDER BY id", file=snapshot)
        rc, output = cli_output("backup", "--check")
        require(rc == 0 and "runs" in output and "feed" in output, output)
        for i in range(20):
            state.add_feed("later", "note", str(i))
        previous = db_values()
        snapshot_identity = "f" * 32
        with sqlite3.connect(snapshot) as conn:
            conn.execute("UPDATE schema_meta SET value=? WHERE key='instance_id'", (snapshot_identity,))
        rc, restored = cli_output("backup", "--restore", str(snapshot))
        require(rc == 0, restored)
        require(sql_rows("SELECT * FROM runs ORDER BY id") == expected, "restore lost runs")
        after = db_values()
        require(all(after["sequences"].get(k, 0) >= v for k, v in previous["sequences"].items()), "sequence regression")
        require(after["meta"]["instance_id"] == previous["meta"]["instance_id"], "identity replaced")
        require(int(after["meta"]["restore_epoch"]) > int(previous["meta"]["restore_epoch"]), "epoch did not increase")
        evidence.update(journal_mode="wal", check_output=output, restore_output=restored, before=previous, after=after,
                        retention=retention_policy(), consistent_counts=consistent_counts())
    finally:
        close_server(server)
    return evidence


def retention_policy():
    from arete import backup, paths, state, util

    folder = paths.STATE_DIR.parent / "retention"
    folder.mkdir()
    target = folder / "arete.db"
    conn = sqlite3.connect(target)
    conn.executescript((REPO / "tests/fixtures/schema-v0.sql").read_text())
    conn.close()
    with mock.patch.object(paths, "DB_FILE", target):
        state.migrate()
        pinned = sql_rows("SELECT backup FROM schema_migrations ORDER BY n DESC LIMIT 1")[0][0]
        require(pinned, "migration snapshot not pinned")
        snapshots = paths.backups_dir()
        for day in range(1, 21):
            for hour in (6, 18):
                stamp = util.now() - timedelta(days=day)
                (snapshots / f"arete-{stamp:%Y%m%d}T{hour:02d}0000Z-hand.db").write_bytes(b"fixture retention")
        every = backup.snapshots()
        by_day = {backup.stamp_of(p).date(): p for p in every}
        first = (util.now() - timedelta(days=13)).date()
        expected = set(every[-24:]) | {p for day, p in by_day.items() if day >= first} | {snapshots / pinned}
        backup.retain(snapshots)
        require(set(backup.snapshots()) == expected, "retention changed newest/day policy or lost pin")
        return {"pin": pinned, "kept": len(expected), "newest": 24, "days": 14}


def consistent_counts():
    from arete import backup, paths, state, util

    daemon, _ = start_fixture_serve()
    stop_child(daemon)
    before = backup.inspect(paths.DB_FILE)["counts"]
    connect = paths.connect_db
    errors, once = [], []

    def writer():
        try:
            with state.tx() as conn:
                conn.execute("INSERT INTO runs(id,kind,status,started) VALUES ('count-barrier','loop','done',?)", (util.iso(),))
                state.add_feed("count-barrier", "note", "committed together")
        except BaseException as e:
            errors.append(str(e))

    def traced(file, **kw):
        conn = connect(file, **kw)
        if file == paths.DB_FILE and kw.get("readonly"):
            def trace(statement):
                if 'SELECT count(*) FROM "feed"' in statement and not once:
                    once.append(True)
                    thread = threading.Thread(target=writer)
                    thread.start()
                    thread.join(5)
                    if thread.is_alive():
                        errors.append("concurrent count writer blocked")
            conn.set_trace_callback(trace)
        return conn

    with mock.patch.object(paths, "connect_db", side_effect=traced):
        found = backup.inspect(paths.DB_FILE)
    after = backup.inspect(paths.DB_FILE)["counts"]
    require(once and not errors and found["quick_check"] == "ok", errors)
    require(all(found["counts"][name] == before[name] and after[name] == before[name] + 1 for name in ("runs", "feed")),
            {"before": before, "found": found, "after": after})
    return {"before": before, "snapshot": found["counts"], "after": after}


def restore_safety():
    from arete import backup, control, paths, state, util

    evidence = setup()
    with state.tx() as conn:
        conn.execute("INSERT INTO runs(id,kind,status,started) VALUES ('safety','loop','done',?)", (util.iso(),))
        state.add_feed("safety", "note", "snapshot")
    snapshot = backup.snapshot("safety")
    hot = hot_journal_probe()
    refusals = {}

    def inventory():
        return {s: Path(str(paths.DB_FILE) + s).read_bytes() for s in backup.SIDECARS
                if Path(str(paths.DB_FILE) + s).exists()}

    def refused(name, expected_pid=None):
        before = inventory()
        rc, output = cli_output("backup", "--restore", str(snapshot))
        require(rc == 1 and before == inventory(), (name, rc, output))
        if expected_pid:
            require(str(expected_pid) in output, output)
        refusals[name] = output

    holder = child("hold-lock", "serve", paths.STATE_DIR.parent / "serve-held")
    try:
        wait_file(paths.STATE_DIR.parent / "serve-held", holder)
        refused("serve-lock", holder.pid)
    finally:
        stop_child(holder)
    util.write_json(control.BEAT_FILE, {"pid": os.getpid(), "at": util.iso(util.now() - timedelta(days=30))})
    refused("stale-live-heartbeat", os.getpid())
    control.BEAT_FILE.unlink()
    with state.tx() as conn:
        conn.execute("UPDATE runs SET status='running',pid=? WHERE id='safety'", (os.getpid(),))
    refused("run-pid", os.getpid())
    with state.tx() as conn:
        conn.execute("UPDATE runs SET pid=NULL,lock='fixture-detached' WHERE id='safety'")
    holder = child("hold-lock", "fixture-detached", paths.STATE_DIR.parent / "run-held")
    try:
        wait_file(paths.STATE_DIR.parent / "run-held", holder)
        refused("detached-lock", holder.pid)
    finally:
        stop_child(holder)
    util.write_json(control.BEAT_FILE, {"pid": holder.pid, "at": util.iso()})
    require(cli_output("backup", "--restore", str(snapshot))[0] == 0, "dead records blocked restore")
    invalid = paths.STATE_DIR.parent / "invalid.db"
    invalid.write_bytes(b"invalid")
    truncated = invalid.with_name("truncated.db")
    truncated.write_bytes(snapshot.read_bytes()[:snapshot.stat().st_size // 2])
    for source in (invalid, invalid.with_name("missing.db"), truncated):
        before = inventory()
        require(cli_output("backup", "--restore", str(source))[0] == 1 and before == inventory(), "invalid restore changed target")
    conn = paths.connect_db(paths.DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO feed(run_id,ts,kind,text) VALUES ('safety',?,'note','wal-only')", (util.iso(),))
    conn.execute("COMMIT")
    try:
        original = inventory()
        probe = backup.probe_current()
        require(probe and original == inventory(), "private WAL discovery changed originals")
        require(probe["sequences"]["feed"] > dict(sql_rows("SELECT name,seq FROM sqlite_sequence", file=snapshot))["feed"], "WAL data missed")
    finally:
        conn.close()
    faults = []
    for fail_at in range(1, 6):
        for suffix in backup.SIDECARS[1:]:
            Path(str(paths.DB_FILE) + suffix).write_bytes(b"\0" * 512)
        original = inventory()
        real_rename, calls = os.rename, []

        def rename(source, target):
            calls.append((str(source), str(target)))
            if len(calls) == fail_at:
                raise OSError("fixture rename failure")
            return real_rename(source, target)

        with mock.patch.object(os, "rename", side_effect=rename):
            expect(OSError, lambda: backup.restore(snapshot))
        require(inventory() == original, {"rename": fail_at, "calls": calls})
        require(not list(paths.STATE_DIR.glob("*.restoring-*")), "stage leaked")
        faults.append({"rename": fail_at, "original_restored": True})
        for suffix in backup.SIDECARS[1:]:
            Path(str(paths.DB_FILE) + suffix).unlink(missing_ok=True)
    original = inventory()
    flush_file = util.flush_file

    def fail_installed(file):
        if file == paths.DB_FILE:
            raise OSError("fixture installed sync failure")
        return flush_file(file)

    with mock.patch.object(util, "flush_file", side_effect=fail_installed):
        expect(OSError, lambda: backup.restore(snapshot))
    require(inventory() == original, "installed sync failure lost originals")
    cached = (paths.backups_dir() / "instance.json").read_bytes()
    snap_values = dict(sql_rows("SELECT name,seq FROM sqlite_sequence", file=snapshot))
    paths.DB_FILE.write_bytes(b"unreadable current database")
    backup.restore(snapshot)
    after = db_values()
    require(after["sequences"] == {k: v + 10000 for k, v in snap_values.items()}, "unreadable gap")
    require((paths.backups_dir() / "instance.json").read_bytes() == cached, "identity cache changed")
    legacy = paths.STATE_DIR.parent / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript((REPO / "tests/fixtures/schema-v0.sql").read_text())
    conn.execute("INSERT INTO runs(id,kind,status,started) VALUES ('legacy','loop','done',?)", (util.iso(),))
    conn.commit()
    conn.close()
    rc, output = cli_output("backup", "--restore", str(legacy))
    require(rc == 0 and "startup" in output and backup.pending_restore(), output)
    expect(state.IncompatibleSchema, lambda: state.add_feed("legacy", "note", "blocked"))
    process, first = start_fixture_serve()
    stop_child(process)
    process, second = start_fixture_serve()
    stop_child(process)
    require(first["epoch"] == second["epoch"] and state.run_get("legacy"), "pre-ledger completion repeated")
    interruptions = interrupted_restores(snapshot)
    receipt = backup.receipt_epoch()
    paths.DB_FILE.write_bytes(b"unreadable after preledger recovery")
    backup.restore(snapshot)
    require(int(db_values()["meta"]["restore_epoch"]) > receipt, "durable receipt generation regressed")
    evidence.update(refusals=refusals, rename_faults=faults, installed_sync_rollback=True, interruptions=interruptions, hot_journal=hot,
                    unreadable_sequences=after["sequences"], preledger_output=output, first=first, second=second)
    backup.restore_record().write_text("{broken")
    retained = inventory()
    output = finish(child("serve-contender"))
    require("corrupt restore recovery metadata" in output and retained == inventory(), output)
    expect(backup.Refused, backup.recover_restore)
    backup.restore_record().unlink()
    return evidence


def interrupted_restores(snapshot):
    from arete import backup, paths

    results = []
    for phase in ("rename-1", "rename-2", "rename-3", "rename-4", "rename-5", "metadata", "receipt"):
        for suffix in backup.SIDECARS[1:]:
            Path(str(paths.DB_FILE) + suffix).write_bytes(b"\0" * 512)
        original = {s: Path(str(paths.DB_FILE) + s).read_bytes() for s in backup.SIDECARS}
        process = child("interrupt-restore", phase, snapshot)
        out, err = process.communicate(timeout=30)
        require(process.returncode == 72, (phase, process.returncode, out, err))
        data = backup.pending_restore()
        epoch = data["epoch"]
        daemon, found = start_fixture_serve()
        stop_child(daemon)
        require(found["epoch"] == epoch and not backup.pending_restore(), (phase, found))
        archive = paths.DB_FILE.with_name(data["archive"])
        require(all(Path(str(archive) + s).read_bytes() == value for s, value in original.items()), phase)
        daemon, repeated = start_fixture_serve()
        stop_child(daemon)
        require(repeated["epoch"] == epoch, "recovery incremented twice")
        results.append({"phase": phase, "epoch": epoch, "archive": archive.name, "original_bytes_preserved": True})
    ready, release = paths.STATE_DIR.parent / "restore-ready", paths.STATE_DIR.parent / "restore-release"
    process = child("restore-barrier", snapshot, ready, release)
    try:
        wait_file(ready, process)
        contender = child("serve-contender")
        output = finish(contender)
        require("startup excluded" in output, output)
        release.touch()
        finish(process)
    finally:
        if process.poll() is None:
            stop_child(process)
    results.append({"startup_exclusion_barrier": True})
    return results


def hot_journal_probe():
    from arete import backup, paths, state, util

    with state.tx() as conn:
        conn.executemany("INSERT INTO feed(run_id,ts,kind,text) VALUES ('hot',?,'note',?)",
                         [(util.iso(), "original" * 100)] * 1000)
    process = child("hot-journal")
    finish(process)
    journal = Path(str(paths.DB_FILE) + "-journal")
    require(journal.exists(), "child left no hot journal")
    before = {s: Path(str(paths.DB_FILE) + s).read_bytes() for s in backup.SIDECARS
              if Path(str(paths.DB_FILE) + s).exists()}
    found = backup.probe_current()
    require(found and found["sequences"]["feed"] >= 1000, "hot journal could not be recovered privately")
    with backup.private_database(paths.DB_FILE) as conn:
        require(conn.execute("SELECT count(*) FROM feed WHERE text='uncommitted'").fetchone()[0] == 0, "uncommitted hot rows leaked")
    require(all(Path(str(paths.DB_FILE) + s).read_bytes() == value for s, value in before.items()), "hot discovery changed originals")
    return {"hot_journal": True, "private_recovery": True, "original_bytes_preserved": True}


def http_feed_snapshot(run_id):
    import socket
    import urllib.request

    from arete import paths

    daemon, _ = start_fixture_serve()
    stop_child(daemon)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = child("feed-http", port, run_id)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/feed/{run_id}?after=0", timeout=2) as response:
                    found = json.load(response)
                break
            except OSError:
                require(time.monotonic() < deadline and process.poll() is None, "HTTP feed did not start")
                time.sleep(.02)
        require(found["run"]["status"] == "running" and not any(r["kind"] == "end" for r in found["lines"]), found)
        require(isinstance(found["restore_epoch"], int), "HTTP generation absent")
        return {"status": found["run"]["status"], "generation": found["restore_epoch"], "lines": len(found["lines"]),
                "settlement_committed": sql_rows("SELECT status FROM runs WHERE id=?", (run_id,))[0][0] == "done"}
    finally:
        stop_child(process)


def feed_cursors():
    from arete import backup, control, paths, record, state, util
    from arete.commands import common

    evidence = setup()
    rid = record.start("loop", loop="fixture")
    snapshot = backup.snapshot("feed")
    got, errors = [], []
    done = threading.Event()

    def writer(n):
        for i in range(300):
            state.add_feed(rid, "note", f"{n}:{i}")

    def poller():
        after = 0
        try:
            while not done.is_set():
                lines = control.feed(rid, after)["lines"]
                got.extend(lines)
                after = lines[-1]["id"] if lines else after
                time.sleep(.001)
            got.extend(control.feed(rid, after)["lines"])
        except BaseException as e:
            errors.append(str(e))

    polling = threading.Thread(target=poller)
    polling.start()
    try:
        parallel(writer)
    finally:
        done.set()
        polling.join(10)
    require(not errors and len(got) == 1200 and len({r["text"] for r in got}) == 1200, (len(got), errors))
    require([r["id"] for r in got] == sorted({r["id"] for r in got}), "feed ordering")
    before = db_values()
    trace = []
    connect = paths.connect_db

    def traced(*a, **kw):
        conn = connect(*a, **kw)
        conn.set_trace_callback(trace.append)
        return conn

    try:
        with mock.patch.object(paths, "connect_db", side_effect=traced):
            with state.tx() as conn:
                state.insert_feed(rid, "note", "rollback-insert")
                state.add_feed(rid, "note", "rollback-add")
                written = conn.execute("SELECT text, count(*) FROM feed WHERE run_id=? "
                                       "AND text IN ('rollback-insert','rollback-add') GROUP BY text ORDER BY text",
                                       (rid,)).fetchall()
                require([tuple(row) for row in written] == [("rollback-add", 1), ("rollback-insert", 1)], written)
                raise ValueError("rollback")
    except ValueError:
        pass
    statements = []
    for statement in trace:
        if not statements or statement != statements[-1]:
            statements.append(statement)
    require(statements[0] == "BEGIN IMMEDIATE" and statements[-1] == "ROLLBACK"
            and sum(s.startswith("INSERT INTO feed") for s in statements) == 2, trace)
    require(db_values()["feed"] == before["feed"], "feed entry point escaped transaction")
    with mock.patch.object(state, "insert_feed", side_effect=ValueError("end line failure")):
        expect(ValueError, lambda: record.settle(rid, "done", end_line="done"))
    require(state.run_get(rid)["status"] == "running", "settlement escaped end line rollback")
    http = http_feed_snapshot(rid)
    require(http["settlement_committed"], "HTTP snapshot did not overlap a committed settlement")
    with state.tx() as conn:
        conn.executemany("INSERT INTO feed(run_id,ts,kind,text) VALUES (?,?,'note','later')", [(rid, util.iso())] * 15001)
        conn.execute("UPDATE runs SET status='done',pid=NULL WHERE id=?", (rid,))
    high = db_values()["sequences"]["feed"]
    emitted = []
    real_feed = control.feed
    calls = []

    def changing(run, after):
        calls.append(after)
        if len(calls) == 2:
            paths.DB_FILE.write_bytes(b"unreadable")
            backup.restore(snapshot)
            with state.tx() as conn:
                conn.execute("UPDATE runs SET status='done',pid=NULL WHERE id=?", (rid,))
            state.add_feed(rid, "note", "first restored generation")
        return real_feed(run, after)

    with mock.patch.object(control, "feed", side_effect=changing), \
            mock.patch.object(common, "print_feed_line", side_effect=lambda line: emitted.append(line["text"])):
        cursor = common.follow(rid, after=high)
    require("first restored generation" in emitted and 0 in calls, (emitted, calls))
    require(cursor < high and cursor == db_values()["sequences"]["feed"], "final drain cursor stale")

    def restore_during_poll():
        paths.DB_FILE.write_bytes(b"unreadable poll boundary")
        backup.restore(snapshot)
        with state.tx() as conn:
            conn.execute("UPDATE runs SET status='done',pid=NULL WHERE id=?", (rid,))
        state.add_feed(rid, "note", "first polling generation")

    alive = iter((True, False))
    polled = []
    with mock.patch.object(time, "sleep"), \
            mock.patch.object(common, "print_feed_line", side_effect=lambda line: polled.append(line["text"])):
        polling_cursor = common.follow(rid, alive=lambda: next(alive), after=cursor, send=restore_during_poll)
    require("first polling generation" in polled and polling_cursor == db_values()["sequences"]["feed"], polled)
    evidence.update(lines=len(got), unique=len({r["text"] for r in got}), old_cursor=high, returned_cursor=cursor,
                    poll_cursors=calls, emitted=emitted, polling_emitted=polled, polling_cursor=polling_cursor,
                    feed_trace=trace, settlement_rollback=True,
                    generation=real_feed(rid)["restore_epoch"], http=http, node=feed_node())
    return evidence


def feed_node():
    program = REPO / "docs/db-feed-fixture.mjs"
    result = subprocess.run(["node", str(program), str(REPO)], text=True, capture_output=True, timeout=20)
    require(result.returncode == 0, result.stdout + result.stderr)
    return json.loads(result.stdout)


def backup_health():
    import tarfile

    from arete import backup, config, heartbeat, install, paths, serve, util

    evidence = setup()
    cfg = config.load()
    checked = {}
    outputs = {}
    with mock.patch.object(install, "serve_installed", return_value=True), mock.patch.object(util, "last_wake", return_value=0):
        snapshot = backup.snapshot("health")
        checked["fresh"] = heartbeat.check_backup_db().ok
        outputs["fresh"] = backup_health_cli()
        stale = time.time() - 4 * 3600
        os.utime(snapshot, (stale, stale))
        checked["stale"] = heartbeat.check_backup_db().ok
        outputs["stale"] = backup_health_cli()
        with mock.patch.object(install, "serve_installed", return_value=False):
            require(not heartbeat.check_backup_db().ok, "stale snapshot passed with serve uninstalled")
            outputs["stale_uninstalled"] = backup_health_cli()
        snapshot = backup.snapshot("health")
        with snapshot.open("r+b") as file:
            file.truncate(snapshot.stat().st_size // 2)
        checked["truncated"] = heartbeat.check_backup_db().ok
        outputs["truncated"] = backup_health_cli()
        with mock.patch.object(install, "serve_installed", return_value=False):
            require(not heartbeat.check_backup_db().ok, "corrupt snapshot passed with serve uninstalled")
            outputs["corrupt_uninstalled"] = backup_health_cli()
        snapshot = backup.snapshot("health")
        backup.update_last(quick_check="fixture failed check")
        checked["recorded"] = heartbeat.check_backup_db().ok
        outputs["recorded"] = backup_health_cli()
    require(checked == {"fresh": True, "stale": False, "truncated": False, "recorded": False}, checked)
    loops = paths.CONFIG_DIR / "loops"
    loops.mkdir()
    (loops / "allowed.toml").write_text('name = "fixture"')
    (loops / "linked.toml").symlink_to(loops / "allowed.toml")
    authority = loops / "verification-authority"
    authority.mkdir()
    (authority / "receipt.json").write_text('{}')
    for name in (".env", ".env.test", "secret.env", "pi.env"):
        (loops / name).write_text("fixture only")
    day = util.now().strftime("%Y-%m-%d")
    layers_git("init", "-q", "-b", "main")
    (paths.CONFIG_DIR / "layers" / "allowed.txt").write_text("allowed history")
    (paths.CONFIG_DIR / "layers" / ":allowed.txt").write_text("allowed colon pathname")
    layers_git("add", ".")
    layers_git("commit", "-qm", "allowed")
    calls = []
    destination = paths.STATE_DIR.parent / "away"
    destination.mkdir()

    def transport(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "fixture failure")
        for name in argv[2:4]:
            shutil.copyfile(name, destination / Path(name).name)
        return subprocess.CompletedProcess(argv, 0, "", "")

    snapshot = backup.snapshot("health")
    with mock.patch.object(util, "sh", side_effect=transport):
        backup.daily_copy(snapshot, "")
        require(not calls and not heartbeat.check_backup_offsite(cfg).ok, "empty destination copied")
        server = serve.Server()
        server.cfg = replace(cfg, backup=config.BackupConfig(str(destination)))
        server.tick = server.prune
        server.load_new_code = lambda: None
        server.reap_inherited = lambda: None
        server.watch_alerts = lambda: None
        try:
            for expected_exit in (1, 0, 0):
                server.run_pass()
                deadline = time.monotonic() + 10
                while backup.COPYING:
                    require(time.monotonic() < deadline, "serve copy did not finish")
                    time.sleep(.01)
                require(backup.read_last().get("copy_exit") == expected_exit, "serve copy/retry result")
        finally:
            close_server(server)
        snapshot = backup.newest_snapshot()
    require(len(calls) == 2, "successful copy repeated")
    with tarfile.open(destination / f"config-{day}.tar.gz") as tar:
        members = tar.getnames()
        require("loops/allowed.toml" in members and "config.toml" in members, members)
        require(all(not m.issym() and not any(backup.forbidden_name(p) for p in Path(m.name).parts) for m in tar), members)
        bundle = destination / "layers.bundle"
        bundle.write_bytes(tar.extractfile("layers.bundle").read())
    layers_git("bundle", "verify", str(bundle))
    away = replace(cfg, backup=config.BackupConfig(str(destination)))
    backup.update_last(copied_at=util.iso(util.now() - timedelta(hours=49)))
    with mock.patch.object(util, "last_wake", return_value=0):
        require(not heartbeat.check_backup_offsite(away).ok, "old offsite receipt passed")
    history = []
    for name in (".env", ".env.test", "private.env", "pi.env", "verification-authority/receipt.json", "linked.txt",
                 ":private/.env.production", ":private/pi.env"):
        shutil.rmtree(paths.CONFIG_DIR / "layers")
        layers_git("init", "-q", "-b", "main")
        (paths.CONFIG_DIR / "layers" / "allowed.txt").write_text("allowed")
        layers_git("add", ".")
        layers_git("commit", "-qm", "allowed")
        forbidden = paths.CONFIG_DIR / "layers" / name
        forbidden.parent.mkdir(parents=True, exist_ok=True)
        if name == "linked.txt":
            forbidden.symlink_to("allowed.txt")
        else:
            forbidden.write_text("fixture only")
        layers_git("add", "-f", ".")
        layers_git("commit", "-qm", "forbidden history")
        layers_git("--literal-pathspecs", "rm", "-q", name)
        layers_git("commit", "-qm", "deleted history")
        expect(RuntimeError, backup.layers_bundle)
        backup.update_last(copied_at="")
        with mock.patch.object(util, "sh", side_effect=AssertionError("forbidden bundle reached transport")):
            failure = backup.run_copy(snapshot, str(destination), day)
        require(failure and not backup.read_last().get("copied_at"), (name, failure))
        history.append({"deleted_name": name, "refused": failure})
    require(outputs["fresh"][0] == 0 and all(outputs[n][0] == 1 for n in ("stale", "truncated", "recorded")), outputs)
    evidence.update(health=checked, heartbeat_output=outputs, copy_attempts=len(calls), archive_members=members, layers_history=history)
    return evidence


def layers_git(*args):
    from arete import paths

    folder = paths.CONFIG_DIR / "layers"
    folder.mkdir(exist_ok=True)
    result = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                             "-c", "commit.gpgsign=false", *args], cwd=folder, text=True, capture_output=True, timeout=20)
    require(result.returncode == 0, (args, result.stdout, result.stderr))
    return result.stdout


def backup_health_cli():
    from arete import heartbeat

    with contextlib.ExitStack() as stack:
        for name in ("check_jobs", "check_probes", "check_claude_auth", "check_codex_auth", "check_codex_cleanup",
                     "check_pi_auth", "check_hook", "check_disk", "check_serve", "check_costs", "check_loops",
                     "check_oncall", "check_workspaces", "check_scratch"):
            stack.enter_context(mock.patch.object(heartbeat, name, autospec=True, return_value=[]))
        stack.enter_context(mock.patch.object(heartbeat, "network_up_or_skip", return_value=True))
        stack.enter_context(mock.patch.object(heartbeat.alerts, "clear_alert", return_value=None))
        stack.enter_context(mock.patch.object(heartbeat.alerts, "raise_alert", return_value=None))
        rc, output = cli_output("heartbeat", "--dry-run")
    require("backup:db" in output and "backup:offsite" in output, output)
    return rc, output


def page_seed():
    from arete import paths, sessions, state, util

    now = util.iso()
    modes = {"local": ("pi", "ollama", "api_key"), "replay": ("replay", "", ""),
             "subscription": ("claude", "anthropic", "subscription"), "api-key": ("pi", "openai", "api_key")}
    with state.tx() as conn:
        for name in (*modes, "mixed", "session"):
            conn.execute("INSERT OR IGNORE INTO runs (id,kind,status,started,finished,summary) VALUES (?,?,?,?,?,?)",
                         (name, "session" if name == "session" else "pipeline", "done", now, now, name))
            for billing in modes if name == "mixed" else (("subscription",) if name == "session" else (name,)):
                runner, provider, auth = modes[billing]
                if name == "mixed" and billing == "subscription":
                    runner = "pi"
                conn.execute("INSERT INTO steps (run_id,step,runner,provider,model,auth_mode,cost_usd,ts,ok) "
                             "VALUES (?,'build',?,?,?,?,1.2345,?,1)",
                             (name, runner, provider, "shared-model" if name == "mixed" else provider + "/fixture",
                              auth, now))
        conn.execute("INSERT INTO runs (id,kind,status,started) VALUES ('settling','pipeline','running',?)", (now,))
    chat_id = state.chat_add("")
    state.chat_say(chat_id, "user", "Read snapshots and billing")
    state.chat_link_session(chat_id, "session", 10)
    state.save_report("session", {"summary": "Fixture report", "body": "A complete offline session report"})
    util.write_json(sessions.spec_file("session"), {"prompt": "Fixture session", "tools": [], "chat": chat_id})
    return {"runs": [*modes, "mixed", "session"], "chat": chat_id, "session": "session"}


def fixture_http(port=0):
    from http.server import ThreadingHTTPServer

    from arete import dashboard, tailnet

    server = ThreadingHTTPServer(("127.0.0.1", port), dashboard.Handler)
    server.node = tailnet.Node(("127.0.0.1",), frozenset({"127.0.0.1", "localhost"}), "fixture", "")
    server.logins = SimpleNamespace(of=lambda address: "fixture" if address == "127.0.0.1" else "")
    return server


def http_json(server, path, body=None, admitted=True):
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{server.server_port}"
    headers = {"Origin": url, "Content-Type": "application/json"}
    if admitted:
        headers["X-Arete"] = "1"
    request = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def coherent_reads():
    from arete import board, chat, control, paths, sessions, state, tasks, util
    from arete.commands import costs

    evidence = setup()
    seeded = page_seed()
    conn = paths.connect_db(paths.DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    connections, windows = [], []
    connect, steps_since = paths.connect_db, state.steps_since

    def tracked(*args, **kw):
        connections.append({"thread": threading.current_thread().name, "readonly": kw.get("readonly", False)})
        return connect(*args, **kw)

    def steps(since, connect=state.reader):
        windows.append(since)
        return steps_since(since, connect)

    def read_once(call):
        connections.clear()
        result = call()
        require(len(connections) == 1 and connections[0]["readonly"], connections)
        return result

    with mock.patch.object(paths, "connect_db", tracked), mock.patch.object(state, "steps_since", steps):
        evidence["snapshot"] = read_once(board.snapshot)
        evidence["run"] = read_once(lambda: board.run_board("mixed"))
        require(evidence["run"]["run"]["paid"] == sum(m["paid"] for m in evidence["run"]["models"]), "paid mismatch")
        for args in ([], ["--by", "route", "model", "--json"]):
            code, output = read_once(lambda: cli_output("costs", *args))
            require(code == 0, output)
        require(windows[-1] == windows[-2], windows)
        evidence["windows"] = windows[:]
        server = fixture_http()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            urls = ["/api/meta", "/api/runs/mixed", "/api/chats", f"/api/chats/{seeded['chat']}",
                    f"/api/chats/{seeded['chat']}/sessions",
                    f"/api/chats/{seeded['chat']}/sessions/session/report", "/api/sidebar", "/api/push",
                    "/api/sessions", "/api/feed/session", "/api/live", "/api/find"]
            evidence["gets"] = {}
            for url in urls:
                status, result = read_once(lambda: http_json(server, url))
                require(status == 200, (url, status, result))
                evidence["gets"][url] = {"status": status, "connections": connections[:]}
            for url, status in (("/api/runs/does-not-exist", 404), ("/api/not-a-route", 404),
                                ("/api/runs?limit=bad", 400)):
                got, result = http_json(server, url)
                require(got == status and "error" in result, (url, got, result))
            require(http_json(server, "/api/chats/new", {"project": ""})[0] == 200, "POST failed")
            require(http_json(server, "/api/chats/new", {"project": ""}, admitted=False)[0] == 403, "admission bypass")

            def remote(*args):
                active = getattr(state._OWNER, "active", None)
                require(active and active[1] == "read", active)
                return {"id": 42, "ref": "fake#42", "title": "remote"}, "fake"

            with mock.patch.object(tasks, "tracker_item", remote):
                require(read_once(lambda: http_json(server, "/api/tasks/42"))[0] == 200, "remote GET")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(10)
    require(not state.chat_sessions(seeded["chat"])[0]["told"], "GET delivered a report")
    require(sessions.tell_chats() >= 1 and state.chat_sessions(seeded["chat"])[0]["told"], "writer did not deliver")
    evidence["report_delivery"] = chat.session_report(seeded["chat"], "session")

    def concurrent_read(target, read_name, commit):
        original = getattr(state, read_name)
        reached, released = threading.Event(), threading.Event()
        once = False

        def paused(*a, **kw):
            nonlocal once
            answer = original(*a, **kw)
            if not once:
                once = True
                reached.set()
                require(released.wait(10), "read barrier timed out")
            return answer

        with ThreadPoolExecutor(1) as pool, mock.patch.object(state, read_name, paused):
            future = pool.submit(target)
            try:
                require(reached.wait(10), "read barrier not reached")
                commit()
            finally:
                released.set()
            return future.result(10)

    def settle():
        with state.tx() as conn:
            conn.execute("UPDATE runs SET status='done', finished=? WHERE id='settling'", (util.iso(),))

    snap = concurrent_read(board.snapshot, "runs_marked_running", settle)
    live = snap["live"]["rows"]["runs"]
    recent = snap["recent"]["rows"]
    require(any(r["id"] == "settling" for r in live)
            and next(r for r in recent if r["id"] == "settling")["status"] == "running", snap)

    def add_step():
        with state.tx() as conn:
            conn.execute("INSERT INTO steps (run_id,step,runner,provider,auth_mode,cost_usd,ts) "
                         "VALUES ('mixed','late','pi','openai','api_key',2,?)", (util.iso(),))

    run = concurrent_read(lambda: board.run_board("mixed"), "steps", add_step)
    require(run["run"]["paid"] == sum(m["paid"] for m in run["models"]), run)
    def cost_report():
        with state.one_read():
            return control.costs(connect=state.reader)

    with mock.patch.object(state, "steps_since", steps):
        report = concurrent_read(cost_report, "steps_since", add_step)
    require(windows[-1] == windows[-2], windows)
    require(round(report["total_usd"], 2) == round(sum(r["paid_usd"] for r in report["by_route"]), 2), report)
    evidence["barriers"] = {"snapshot": snap, "run": run, "costs": report}
    with mock.patch.object(costs, "print_step_costs", side_effect=AssertionError("writer entered report")):
        require(cli_output("costs", "--correct", "mixed", "-0.25", "--why", "fixture")[0] == 0, "correction failed")
        require(cli_output("costs", "--backfill")[0] == 0, "backfill failed")
    return evidence


def event_contract():
    import hashlib

    from arete import backup, config, control, loop_run, loops, paths, state, util

    paths.ensure_dirs()
    paths.CONFIG_FILE.write_text('[replay]\nall = true\n[jev]\nenabled = false\n')
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (REPO / "arete/migrations").glob("m000[1-3]*.py")}
    original = paths.DB_FILE
    evidence = {"journal_mode": "delete", "synchronous": 1, "cases": [], "historical_hashes": hashes}
    sleeper = child("sleep")
    try:
        for kind in ("fresh", "v0", "v3"):
            paths.DB_FILE = original.parent / kind / "arete.db"
            paths.DB_FILE.parent.mkdir()
            if kind == "v0":
                conn = paths.connect_db(paths.DB_FILE, create=True)
                conn.executescript((REPO / "tests/fixtures/schema-v0.sql").read_text())
                conn.execute("INSERT INTO events(loop,type,source,key,data,ts) VALUES "
                             "('history','command','old','kept','payload',?)", (util.iso(),))
                conn.commit()
                conn.close()
            state.migrate(contracts=False)
            with state.tx() as conn:
                conn.execute("INSERT INTO runs(id,kind,status,started,pid,schema_version) "
                             "VALUES ('older','loop','running',?,?,3)", (util.iso(), sleeper.pid))
            require(state.migrate(), "older run did not block contract")
            with state.tx() as conn:
                conn.execute("UPDATE runs SET schema_version=NULL WHERE id='older'")
            require(state.migrate(), "unstamped run did not block contract")
            state.add_event("pending", {"type": "finding", "source": "fixture", "key": kind})
            with state.tx() as conn:
                conn.execute("UPDATE runs SET status='done' WHERE id='older'")
            for stamp in (3, None):
                util.write_json(control.BEAT_FILE, {"pid": sleeper.pid, "schema": stamp})
                require(state.migrate(), "older serve did not block contract")
            stop_child(sleeper)
            require(not state.migrate(), "contract did not finish")
            control.BEAT_FILE.unlink()
            state.add_event("after", {"type": "finding", "source": "fixture", "key": kind})
            with state.one_read() as conn:
                columns = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
                meta = dict(conn.execute("SELECT key,value FROM schema_meta"))
                rows = [dict(r) for r in conn.execute("SELECT * FROM events")]
                require("data" not in columns and int(meta["oldest_writer"]) == state.LATEST, meta)
                require(len(rows) == (3 if kind == "v0" else 2), rows)
            require(not state.migrate(), "migration repeated")
            evidence["cases"].append({"kind": kind, "columns": columns, "events": rows, "meta": meta})
            sleeper = child("sleep")
    finally:
        stop_child(sleeper)
        paths.DB_FILE = original
    state.migrate()
    from arete import sources

    loop = loops.Loop("event-fixture", paths.CONFIG_DIR, "fixture", trigger=loops.Trigger(webhook=True))
    loop.prompt_file.write_text("Report the supplied event data.")
    events = [sources.event(kind, "fixture", kind, kind, trust="internal", data={"text": "x" * 30000})
              for kind in ("command", "finding")]
    item = sources.fire(loop, "fixture", events)[0]
    require(state.queue_item(item)["payload"] == events, "queue truncated events")
    from arete.agent import Result

    with mock.patch.object(control, "working_loop", return_value=loop), \
            mock.patch.object(loop_run.runner, "run", return_value=Result(
                ok=True, runner="replay", structured={"summary": "fixture", "findings": [], "state": {}})):
        run = loop_run.launch(loop.name, item=item, detach=False, cfg=config.load())
    require(run["status"] == "done" and run["runner"] == "replay", run)
    require(util.read_json(paths.RUNS_DIR / run["id"] / "events.json", []) == events, "run events truncated")
    require(hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in (REPO / "arete/migrations").glob("m000[1-3]*.py")}, "historical migration edited")
    evidence["payload_lengths"] = [len(e["data"]["text"]) for e in events]
    evidence["snapshot"] = backup.inspect(paths.DB_FILE)
    return evidence


def database_health():
    from arete import backup, config, dbstats, heartbeat, install, paths, state, util

    evidence = setup()
    anchor = paths.connect_db(paths.DB_FILE)
    anchor.execute("PRAGMA journal_mode=WAL")
    anchor.execute("BEGIN")
    anchor.execute("SELECT COUNT(*) FROM schema_meta").fetchone()
    evidence["journal_mode"] = "wal"
    with state.tx() as conn:
        conn.execute('CREATE TABLE "odd""table" (value TEXT)')
        conn.execute('INSERT INTO "odd""table" VALUES (\'kept\')')
    now = datetime.now(UTC)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    identity = dbstats.database_identity(paths.DB_FILE)
    publishers = [child("summary-publisher", start.isoformat(), i) for i in range(3)]
    evidence["publishers"] = [json.loads(finish(p)) for p in publishers]
    count = sum(p["count"] for p in evidence["publishers"])
    durations = [ms for p in evidence["publishers"] for ms in p["durations"]]
    expected = sorted(durations)[int(len(durations) * .95 + .999999) - 1]
    summaries = paths.LOG_DIR / "dbstats"
    (summaries / "corrupt.json").write_text('{"format":')
    sample = json.loads(next(summaries.glob(identity + "-*.json")).read_text())
    sample["database"] = "unrelated"
    (summaries / "unrelated.json").write_text(json.dumps(sample))
    sample["database"] = identity
    sample["hour"] = (start - timedelta(hours=1)).isoformat()
    (summaries / "adjacent.json").write_text(json.dumps(sample))
    sample["hour"] = start.isoformat()
    sample["incarnation"] = "invalid"
    sample["transactions"] = {"bad": {"count": 1000, "max_ms": 50, "histogram": {"6": 1}}}
    (summaries / "invalid.json").write_text(json.dumps(sample))
    rotated = paths.LOG_DIR / "db.log.1"
    row = {"timestamp": (start + timedelta(minutes=1)).isoformat(), "database": identity,
           "total_ms": 6000, "process_kind": "serve", "caller": "fixture.rotated", "error": "OperationalError"}
    rows = [row, {**row, "timestamp": end.isoformat()}, {**row, "database": "another"},
            {**row, "timestamp": (start - timedelta(seconds=1)).isoformat()},
            {k: v for k, v in {**row, "process_kind": "run", "error": "", "total_ms": 700}.items()
             if k != "database"}]
    rotated.write_text("bad\n[]\n" + "\n".join(json.dumps(r) for r in rows) + '\n{"truncated":')
    root = paths.RUNS_DIR / "size-fixture"
    (root / "record").mkdir(parents=True)
    (root / "record" / "data").write_bytes(b"r" * 101)
    (root / "run.log").write_bytes(b"l" * 203)
    external = paths.STATE_DIR.parent / "external"
    external.mkdir()
    (external / "large").write_bytes(b"x" * 10000)
    (root / "link-dir").symlink_to(external, target_is_directory=True)
    (root / "record" / "link-file").symlink_to(external / "large")
    measured = dbstats.collect(now)
    require(measured["transaction_count"] == count, measured)
    require(measured["transactions_by_kind"] == {"cli": 108, "serve": 108, "run": 108}, measured)
    require(expected <= measured["p95_ms"] <= expected * 2, (expected, measured))
    require(measured["max_ms"] == max(durations), measured)
    require(measured["slow_by_kind"] == {"serve": 1, "run": 1}
            and measured["errors_by_class"] == {"OperationalError": 1}, measured)
    require(measured["record_bytes"] == 101 and measured["other_bytes"] == 203
            and measured["total_bytes"] == 304, measured)
    require(measured["db_bytes"] == paths.DB_FILE.stat().st_size and measured["tables"]["events"] == 0, measured)
    require(measured["tables"]['odd"table'] == 1
            and measured["wal_bytes"] == Path(str(paths.DB_FILE) + "-wal").stat().st_size > 0, measured)
    require(dbstats.transaction_totals(start, end, "replaced-file")["transaction_count"] == 0, "identity leaked")
    evidence["hourly"] = measured
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(dbstats.runs_bytes)
        (root / "record" / "data").unlink()
        future.result(10)

    marker = paths.STATE_DIR.parent / "held"
    release = paths.STATE_DIR.parent / "release"
    holder = child("raw-writer", marker, release)
    wait_file(marker, holder)
    waiting_marker = paths.STATE_DIR.parent / "waiting"
    waiter = child("timed-writer", waiting_marker)
    wait_file(waiting_marker, waiter)
    try:
        time.sleep(6)
    finally:
        release.touch()
    finish(holder)
    finish(waiter)
    recent = list(dbstats.log_records(util.now() - timedelta(hours=1), util.now()))
    waiting = [r for r in recent if r.get("caller", "").endswith("timed_writer") and r["total_ms"] > 5000]
    require(len(waiting) == 1 and waiting[0]["begin_ms"] > 5900, recent)
    slow = heartbeat.check_db_slow()[0]
    require(not slow.ok and slow.severity == "warn" and "timed_writer" in slow.detail, slow)
    cfg = config.load()
    tiny = replace(cfg, retention=replace(cfg.retention, db_max_mb=.0001, max_gb=.00000001))
    checks = []
    for installed in (True, False):
        with mock.patch.object(install, "serve_installed", return_value=installed):
            found = heartbeat.check_db_slow() + heartbeat.check_db_size(tiny) + heartbeat.check_runs_size(tiny)
            require({c.key for c in found} == {"db:slow", "db:size", "runs:size"}
                    and all(not c.ok and c.severity == "warn" for c in found), found)
            checks.append([vars(c) for c in found])
    evidence["heartbeat_warnings"] = checks
    evidence["heartbeat_dry_run"] = backup_health_cli()
    require(all(key in evidence["heartbeat_dry_run"][1] for key in ("db:slow", "db:size", "runs:size")),
            evidence["heartbeat_dry_run"])
    for file in paths.LOG_DIR.glob("db.log*"):
        if file.is_file():
            file.unlink()
    old = {**row, "database": identity, "timestamp": (util.now() - timedelta(hours=2)).isoformat()}
    (paths.LOG_DIR / "db.log").write_text(json.dumps(old) + "\n")
    marker.unlink()
    release.unlink()
    holder = child("raw-writer", marker, release)
    wait_file(marker, holder)
    waiting_marker.unlink(missing_ok=True)
    waiter = child("timed-writer", waiting_marker)
    wait_file(waiting_marker, waiter)
    time.sleep(.2)
    release.touch()
    finish(holder)
    finish(waiter)
    healthy = heartbeat.check_db_slow() + heartbeat.check_db_size(cfg) + heartbeat.check_runs_size(cfg)
    require(all(c.ok for c in healthy), healthy)
    evidence["healthy"] = [vars(c) for c in healthy]
    require(config.retention_table({}).db_max_mb == 500, "default")
    require(config.retention_table({"db_max_mb": .5}).db_max_mb == .5, "fraction")
    for value in (0, -1, True, float("inf"), float("nan")):
        expect(ValueError, lambda: config.retention_table({"db_max_mb": value}))

    server = fixture_server()
    output = []
    server.say = output.append
    beats = []
    started, released = threading.Event(), threading.Event()
    calls = []
    collect = dbstats.collect

    def blocked():
        calls.append(1)
        started.set()
        require(released.wait(10), "collector barrier timed out")
        return collect()

    def beat():
        began = time.monotonic()
        server.beat()
        require(time.monotonic() - began < 1, "beat blocked on collection")
        data = util.read_json(paths.STATE_DIR / "serve.json", {})
        require("db" in data and set(dbstats.empty_collection()) <= data["db"].keys(), data)
        beats.append(data)
        return data["db"]

    try:
        require(beat()["status"] == "initial", beats)
        with mock.patch.object(dbstats, "collect", blocked), \
                mock.patch.object(backup, "snapshot", side_effect=OSError("no prune")):
            server.prune()
            require(started.wait(5), "collector not scheduled after skipped prune")
            parallel(lambda _: server.collect_db())
            require(beat()["status"] == "pending" and len(calls) == 1, calls)
            released.set()
            server.db_future.result(10)
        first = beat()
        require(first["status"] == "complete", first)
        started.clear()
        released.clear()
        with mock.patch.object(dbstats, "collect", blocked):
            server.collect_db()
            require(started.wait(5), "second collection")
            pending = beat()
            require(pending["collected_at"] == first["collected_at"]
                    and pending["total_bytes"] == first["total_bytes"], pending)
            released.set()
            server.db_future.result(10)
        require(beat()["status"] == "complete", beats)
        completed = beats[-1]["db"]
        with mock.patch.object(dbstats, "collect", side_effect=OSError("fixture collection failure")):
            server.collect_db()
            expect(OSError, lambda: server.db_future.result(10))
        failed = beat()
        require(failed["status"] == "failed" and failed["finished_at"]
                and failed["error"] == "OSError: fixture collection failure", failed)
        require(all(failed[key] == completed[key] for key in dbstats.empty_collection()), failed)
        for _ in range(3):
            require(beat() == failed, beats)
        with mock.patch.object(server.pool, "submit", side_effect=RuntimeError("fixture submission failure")) as submit:
            server.collect_db()
            failed = beat()
            require(failed["status"] == "failed" and failed["finished_at"]
                    and failed["error"] == "RuntimeError: fixture submission failure", failed)
            require(all(failed[key] == completed[key] for key in dbstats.empty_collection()), failed)
            for _ in range(3):
                require(beat() == failed, beats)
            require(submit.call_count == 1 and server.db_future is None, submit.call_count)
        summaries = [json.loads(s[3:]) for s in output if s.startswith("db {")]
        require(len(summaries) == 2, output)
        require(summaries == [{key: complete[key] for key in dbstats.empty_collection()}
                              for complete in (first, completed)], summaries)
        failures = [s for s in output if s.startswith("db collection failed:")]
        require(failures == ["db collection failed: OSError: fixture collection failure",
                             "db collection failed: RuntimeError: fixture submission failure"], output)
    finally:
        released.set()
        close_server(server)
    (paths.STATE_DIR.parent / "serve-beats.json").write_text(json.dumps(beats, indent=2))
    (paths.STATE_DIR.parent / "serve.log").write_text("\n".join(output) + "\n")
    evidence["collection"] = {"beats": beats, "log": output}
    anchor.close()
    return evidence


def legacy_cleanup():
    from arete import paths, state

    evidence = setup()
    legacy, marker = paths.STATE_DIR / "state.db", paths.STATE_DIR / "chat.json.moved"
    unrelated = paths.STATE_DIR / "unrelated"
    unrelated.write_text("keep")
    cases = []
    for kind in ("empty", "nonempty", "symlink", "missing"):
        legacy.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        if kind == "empty":
            legacy.touch()
            marker.write_text("obsolete")
        elif kind == "nonempty":
            legacy.write_text("legacy data")
            marker.write_text("obsolete")
        elif kind == "symlink":
            legacy.symlink_to(unrelated)
            marker.symlink_to(unrelated)
        server, found = start_fixture_serve()
        stop_child(server)
        require(unrelated.read_text() == "keep", "unrelated file removed")
        require(legacy.exists() == (kind in ("nonempty", "symlink")), kind)
        require(marker.exists() == (kind == "symlink"), kind)
        require(found["version"] == state.LATEST, found)
        cases.append({"kind": kind, "legacy_exists": legacy.exists(), "marker_exists": marker.exists(), **found})
    evidence["cases"] = cases
    return evidence


def browser_seed():
    evidence = setup()
    evidence.update(page_seed())
    print(json.dumps(evidence, indent=2))
    for run in evidence["runs"]:
        print(f"http://127.0.0.1:8765/#run/{run}")
    print(f"http://127.0.0.1:8765/api/chats/{evidence['chat']}")
    print("http://127.0.0.1:8765/#live (open the chat console and select the seeded chat)")
    print(f"http://127.0.0.1:8765/api/chats/{evidence['chat']}/sessions/session/report")
    print("http://127.0.0.1:8765/ui.html")
    return evidence


def browser_serve(port):
    from arete import paths

    conn = paths.connect_db(paths.DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    server = fixture_http(port)
    print(f"Offline fixture: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


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
                     "durable-files": durable_files, "wal-startup": wal_startup, "prune-restore": prune_restore,
                     "restore-safety": restore_safety, "feed-cursors": feed_cursors, "backup-health": backup_health}
        functions.update({"coherent-reads": coherent_reads, "event-contract": event_contract,
                          "database-health": database_health, "legacy-cleanup": legacy_cleanup})
        result = functions[role]()
        (paths.STATE_DIR.parent / "evidence.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(f"{role}: passed")
    elif role == "browser-seed":
        browser_seed()
    elif role == "browser-serve":
        browser_serve(int(args[0]))
    elif role == "sleep":
        time.sleep(120)
    elif role == "summary-publisher":
        from arete import dbstats

        hour, publisher = args
        durations = [1] * 90 + [3] * 6 + [512, 8192]
        kind = ("cli", "serve", "run")[int(publisher)]
        for ms in durations:
            dbstats.retain(dbstats.database_identity(paths.DB_FILE), hour,
                           {"mode": "read", "caller": "fixture.publisher", "process_kind": kind,
                            "total_ms": ms, "begin_ms": 0, "commit_ms": 0, "error": ""})
        retain = dbstats.retain

        def observed(database, at, row):
            durations.append(row["total_ms"])
            retain(database, at, row)

        with mock.patch.object(dbstats, "datetime") as clock, \
                mock.patch.object(dbstats, "process_kind", return_value=kind), \
                mock.patch.object(dbstats, "retain", observed):
            clock.now.return_value = datetime.fromisoformat(hour) + timedelta(minutes=10)
            for _ in range(10):
                with state.tx() as conn:
                    conn.execute("SELECT COUNT(*) FROM events").fetchone()
        print(json.dumps({"publisher": publisher, "pid": os.getpid(), "incarnation": dbstats.INCARNATION,
                          "count": len(durations), "durations": durations}))
    elif role == "raw-writer":
        conn = paths.connect_db(paths.DB_FILE)
        conn.execute("BEGIN IMMEDIATE")
        Path(args[0]).touch()
        wait_file(Path(args[1]))
        conn.execute("COMMIT")
        conn.close()
    elif role == "serve-live":
        from arete import backup, serve, util

        serve.start_notes = lambda cfg: []
        serve.Server.report_config = lambda self: None

        def tick(self):
            with state.one_read() as conn:
                found = {"journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                         "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
                         "epoch": int(backup.metadata(conn)["restore_epoch"]),
                         "version": conn.execute("PRAGMA user_version").fetchone()[0]}
            util.write_json(Path(args[0]), found)
            time.sleep(30)

        serve.Server.run_pass = tick
        serve.run(with_dashboard=False)
    elif role == "wal-failure":
        from arete import serve

        connect = paths.connect_db

        class FailingConnection:
            def __init__(self, conn):
                object.__setattr__(self, "conn", conn)

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def __setattr__(self, name, value):
                setattr(self.conn, name, value)

            def execute(self, query, *args):
                if query == "PRAGMA journal_mode=WAL":
                    raise sqlite3.OperationalError("fixture WAL failure")
                return self.conn.execute(query, *args)

        paths.connect_db = lambda *a, **kw: FailingConnection(connect(*a, **kw))
        serve.Server = lambda: (_ for _ in ()).throw(AssertionError("workers constructed after WAL failure"))
        expect(SystemExit, lambda: serve.run(with_dashboard=False))
        print("WAL failure stopped startup before workers/API")
    elif role == "hold-lock":
        from arete import util

        with util.lock(args[0]) as held:
            require(held, "fixture could not acquire lock")
            Path(args[1]).touch()
            time.sleep(60)
    elif role == "hot-journal":
        conn = sqlite3.connect(paths.DB_FILE)
        conn.execute("PRAGMA cache_size=10")
        conn.execute("UPDATE feed SET text='uncommitted'")
        os._exit(0)
    elif role == "feed-http":
        from arete import dashboard, record, tailnet

        original = state.feed
        once = False

        def paused_feed(*a, **kw):
            nonlocal once
            lines = original(*a, **kw)
            if not once:
                once = True
                errors = []

                def settle():
                    try:
                        record.settle(args[1], "done", end_line="done")
                    except BaseException as e:
                        errors.append(str(e))

                writer = threading.Thread(target=settle)
                writer.start()
                writer.join(5)
                require(not writer.is_alive() and not errors, ("concurrent HTTP settlement", errors))
            return lines

        class Host(tailnet.ScratchHost):
            def node(self, owner):
                return tailnet.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "fixture@example.com", "")

            def whois(self, address):
                return "fixture@example.com"

        state.feed = paused_feed
        dashboard.serve(port=int(args[0]), host=Host(), inside_serve=True)
    elif role == "interrupt-restore":
        from arete import backup, util

        phase, source = args
        rename, apply, write_json = os.rename, backup.apply_metadata, util.write_json
        count = 0

        def interrupted_rename(source, target):
            nonlocal count
            rename(source, target)
            count += 1
            if phase == f"rename-{count}":
                os._exit(72)

        def interrupted_metadata(target, data):
            result = apply(target, data)
            if phase == "metadata" and target == paths.DB_FILE:
                os._exit(72)
            return result

        def interrupted_receipt(file, data):
            write_json(file, data)
            if phase == "receipt" and file.name == "restore-receipt.json":
                os._exit(72)

        os.rename, backup.apply_metadata, util.write_json = interrupted_rename, interrupted_metadata, interrupted_receipt
        backup.restore(Path(source))
        raise AssertionError("restore interruption point was not reached")
    elif role == "restore-barrier":
        from arete import backup

        install = backup.install_staged

        def paused_install(staged):
            Path(args[1]).touch()
            wait_file(Path(args[2]))
            return install(staged)

        backup.install_staged = paused_install
        backup.restore(Path(args[0]))
    elif role == "serve-contender":
        from arete import serve

        with contextlib.redirect_stderr(sys.stdout):
            expect(SystemExit, lambda: serve.run(with_dashboard=False))
        print("startup excluded")
    elif role == "hold-writer":
        with state.tx():
            Path(args[0]).touch()
            wait_file(Path(args[1]))
    elif role == "timed-writer":
        if args:
            Path(args[0]).touch()
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
        require(data["journal_mode"] in ("delete", "wal") and data["synchronous"] == 1, file)
        found[action] = {"evidence": str(file), "journal_mode": data["journal_mode"], "synchronous": data["synchronous"]}
    readme = (REPO / "README.md").read_text()
    require("one full day" in readme and "rollback" in readme, "rollout documentation missing")
    (ROOT / "inspect.json").write_text(json.dumps(found, indent=2) + "\n")
    print(json.dumps(found, indent=2))
    print("inspect: passed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", choices=[*ACTIONS, "inspect", "browser-seed", "browser-serve"])
    parser.add_argument("--port", type=int, default=8765)
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
    if args.action == "browser-serve":
        folder = Path(json.loads((ROOT / "browser.json").read_text())["folder"])
        require(folder.resolve().is_relative_to(ROOT.resolve()) and not folder.is_symlink(), folder)
        process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--child",
                                    "browser-serve", str(args.port)], cwd=REPO, env=environment(folder))
        try:
            raise SystemExit(process.wait())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    if args.action == "inspect":
        inspect_evidence()
        return
    require(args.action in (*ACTIONS, "browser-seed"), "choose an exercise")
    folder = Path(tempfile.mkdtemp(prefix=args.action + "-", dir=ROOT))
    env = environment(folder)
    if args.action == "browser-seed":
        (ROOT / "browser.json").write_text(json.dumps({"folder": str(folder)}))
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
