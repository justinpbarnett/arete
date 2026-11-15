import atexit
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CHECKOUT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CHECKOUT))
from arete import backup, cli, control, dashboard, engine, migrations, paths, record, serve, state, util

TRACE = []


def install_trace():
    connect = paths.connect_db

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(TRACE.append)
        return conn

    paths.connect_db = traced
    atexit.register(lambda: (paths.STATE_DIR.parent / f"trace-{os.getpid()}.json").write_text(json.dumps(TRACE)))


def child(code):
    prelude = (f"import sys; sys.path.insert(0, {str(Path(__file__).parent)!r}); "
               "import schema_ledger as s; s.install_trace(); "
               "from schema_ledger import *; ")
    return subprocess.Popen([sys.executable, "-I", "-c", prelude + code], cwd=CHECKOUT,
                            env=os.environ.copy(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finished(process, expected=0, timeout=30):
    out, err = process.communicate(timeout=timeout)
    assert process.returncode == expected, (process.returncode, out, err)
    assert "duplicate column name" not in out + err, out + err
    return out + err


def stop(process):
    if process.poll() is None:
        process.terminate()
    process.communicate(timeout=10)


def trace_of(process):
    return json.loads((paths.STATE_DIR.parent / f"trace-{process.pid}.json").read_text())


def no_ddl(trace):
    assert not any(s.lstrip().upper().startswith(("CREATE", "ALTER", "PRAGMA TABLE_INFO")) for s in trace), trace


def sql(query, args=()):
    with sqlite3.connect(paths.DB_FILE) as conn:
        return conn.execute(query, args).fetchall()


def version():
    return sql("PRAGMA user_version")[0][0]


def probe(phase="expand", statement="ALTER TABLE feed ADD COLUMN probe TEXT;"):
    folder = CHECKOUT / "arete/migrations"
    name = "m0003_foundation_probe.py"
    (folder / name).write_text(f"PHASE = {phase!r}\nSQL = {statement!r}\n")
    (folder / "latest.txt").write_text(name + "\n")
    return folder / name


def init():
    state.migrate()
    assert version() == 2
    assert dict(sql("SELECT key, value FROM schema_meta"))["schema_version"] == "2"
    assert {r[1]: r[5] for r in sql("PRAGMA table_list")}["schema_migrations"] == 1
    assert {r[1]: r[5] for r in sql("PRAGMA table_list")}["schema_meta"] == 1
    assert [(r[0], r[1]) for r in sql("SELECT n, sha256 FROM schema_migrations ORDER BY n")] == [
        (m.n, m.sha256) for m in migrations.load()]


def old_db():
    init()
    probe()


def legacy_db():
    paths.ensure_dirs()
    fixture = CHECKOUT / "tests/fixtures/schema-v0.sql"
    conn = sqlite3.connect(paths.DB_FILE)
    conn.executescript(fixture.read_text())
    conn.commit()
    conn.close()


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("timed out")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


DASHBOARD = """
class Host(dashboard.ScratchHost):
    def node(self, owner):
        return dashboard.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "me@example.com", "")
    def whois(self, address):
        return "me@example.com"
dashboard.serve(port=PORT, host=Host(), inside_serve=True)
"""


def meta_from_dashboard():
    port = free_port()
    process = child(DASHBOARD.replace("PORT", str(port)))
    try:
        def listening():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                return False
        wait_for(listening)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=5) as response:
            return json.load(response)
    finally:
        stop(process)


def test_an_old_db_waits_for_serve():
    old_db()
    process = child("state.ensure_schema(); print('OPENED')")
    time.sleep(2)
    assert process.poll() is None
    finished(child("state.migrate()"))
    output = finished(process)
    assert "OPENED" in output and output.count("waits for") == 1, output
    assert "2" in output and "3" in output
    no_ddl(trace_of(process))
    assert version() == 3


def test_eight_openers_and_serve_migrate_once():
    old_db()
    gate = paths.STATE_DIR.parent / "go"
    wait = f"\nwhile not Path({str(gate)!r}).exists(): time.sleep(.01)\n"
    writers = [child(wait + "state.ensure_schema()") for _ in range(8)]
    port = free_port()
    daemon = child(wait + "held = serve.take_serve_lock(); state.migrate()\n"
                   + DASHBOARD.replace("PORT", str(port)))
    migrator = child(wait + "raise SystemExit(cli.main(['migrate']))")
    try:
        gate.touch()
        for process in [*writers, migrator]:
            finished(process)
        def listening():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    return True
            except OSError:
                return False
        wait_for(listening)
        assert daemon.poll() is None
        assert sql("SELECT count(*) FROM schema_migrations WHERE n=3") == [(1,)]
        assert version() == 3
        for process in writers:
            no_ddl(trace_of(process))
    finally:
        stop(daemon)
        for process in [*writers, migrator]:
            if process.poll() is None:
                stop(process)


def test_add_feed_issues_no_ddl():
    init()
    state.ensure_schema()
    TRACE.clear()
    state.add_feed("run", "out", "x")
    statements = []
    for statement in TRACE:
        statement = statement.strip().upper()
        if not statements or statement != statements[-1]:
            statements.append(statement)
    assert len(statements) == 3, TRACE
    assert statements[0] == "BEGIN" and statements[1].startswith("INSERT INTO FEED") and statements[2] == "COMMIT", TRACE
    assert state.feed("run")[-1]["text"] == "x"


def test_no_serve_makes_no_file():
    process = child("""
try:
    state.ensure_schema()
except state.NoDatabase as e:
    assert 'arete serve' in str(e) and 'arete migrate' in str(e), str(e)
else:
    raise AssertionError('created a database')
assert not paths.DB_FILE.exists()
""")
    finished(process)
    no_ddl(trace_of(process))
    old_db()
    started = time.monotonic()
    process = child("""
state.MIGRATE_WAIT_SECONDS = 1
try:
    state.ensure_schema()
except state.IncompatibleSchema as e:
    assert all(s in str(e) for s in ('2', '3', 'arete serve', 'arete migrate')), str(e)
else:
    raise AssertionError('opened an old database')
""")
    finished(process)
    assert time.monotonic() - started < 3
    no_ddl(trace_of(process))
    finished(child("state.migrate()"))
    sql("UPDATE schema_meta SET value = '99' WHERE key = 'oldest_writer'")
    finished(child("""
try:
    state.ensure_schema()
except state.IncompatibleSchema as e:
    assert '99' in str(e) and str(state.LATEST) in str(e), str(e)
else:
    raise AssertionError('allowed an old writer')
with state.reader() as conn:
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
"""))


def test_a_deleted_db_stops_serve():
    init()
    snapshot = backup.snapshot("hand")
    marker = paths.STATE_DIR.parent / "tick"
    process = child(f"""
serve.TICK_SECONDS = 1
serve.start_notes = lambda cfg: []
def tick(self):
    state.pending()
    Path({str(marker)!r}).touch()
serve.Server.tick = tick
raise SystemExit(cli.main(['serve', '--no-dashboard']))
""")
    try:
        wait_for(marker.exists)
        paths.DB_FILE.unlink()
        output = finished(process, 1, timeout=4)
        assert snapshot.name in output and "arete backup --restore" in output, output
        assert not paths.DB_FILE.exists()
    finally:
        if process.poll() is None:
            stop(process)
    output = finished(child("""
serve.Server = lambda: (_ for _ in ()).throw(AssertionError('constructed Server'))
raise SystemExit(cli.main(['serve', '--no-dashboard']))
"""), 1)
    assert snapshot.name in output and "arete backup --restore" in output
    assert not paths.DB_FILE.exists()


def test_the_migration_snapshot_is_kept():
    legacy_db()
    start = util.now()
    count = iter(range(1000))
    with mock.patch.object(util, "now", side_effect=lambda: start + timedelta(minutes=next(count))):
        state.migrate()
        pinned = sql("SELECT backup FROM schema_migrations ORDER BY n DESC LIMIT 1")[0][0]
        assert pinned
        for _ in range(30):
            backup.snapshot("hand")
    target = paths.backups_dir() / pinned
    assert target.is_file()
    with sqlite3.connect(target) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert cli.main(["backup", "--check"]) == 0


def test_load_new_code_migrates_before_busy():
    old_db()
    server = serve.Server()
    server.code = frozenset({"old"})
    server.code_seen = frozenset({"new"})
    said, commands = [], []
    server.say = said.append
    server.busy_with = lambda: "a fixture child is running"
    real_sh = util.sh

    def sh(command, **kwargs):
        commands.append(command)
        return real_sh(command, **kwargs)

    with mock.patch.object(serve, "code_stamp", return_value=server.code_seen), \
            mock.patch.object(util, "sh", side_effect=sh), mock.patch.object(os, "execv") as execute:
        server.load_new_code()
        server.load_new_code()
    assert version() == 3
    assert sum("migrate" in command for command in commands) == 1, commands
    assert any("fixture child" in line for line in said), said
    assert not execute.called
    assert server.code == frozenset({"old"})
    server.migration_stamp = None
    said.clear()

    def failure(command, **kwargs):
        return subprocess.CompletedProcess(command, int("migrate" in command), "", "first\nlast migration error")

    with mock.patch.object(serve, "code_stamp", return_value=server.code_seen), \
            mock.patch.object(util, "sh", side_effect=failure), mock.patch.object(os, "execv") as execute:
        server.load_new_code()
        server.load_new_code()
    assert len(said) == 1 and said[0].endswith("last migration error"), said
    assert not execute.called
    server.pool.shutdown()
    for pool in server.job_pools.values():
        pool.shutdown()


def test_a_second_serve_waits_its_turn():
    init()
    root = paths.STATE_DIR.parent
    ready, reload, reexec = (root / name for name in ("ready", "reload", "reexec"))
    program = root / "reexec.py"
    program.write_text(f"""
import sys, time
sys.path.insert(0, {str(CHECKOUT)!r})
from arete import serve
held = serve.take_serve_lock()
from pathlib import Path
Path({str(reexec)!r}).touch()
time.sleep(60)
""")
    first = child(f"""
held = serve.take_serve_lock()
state.migrate()
server = serve.Server()
server.lock = held
Path({str(ready)!r}).touch()
while not Path({str(reload)!r}).exists(): time.sleep(.02)
serve.code_stamp = lambda: frozenset(['new'])
server.code_seen = serve.code_stamp()
os.execv = lambda exe, argv: os.execve(exe, [exe, {str(program)!r}], os.environ)
server.load_new_code()
""")
    try:
        wait_for(ready.exists)
        probe()
        output = finished(child("serve.run(with_dashboard=False)"), 1)
        assert str(first.pid) in output, output
        assert version() == 2
        reload.touch()
        wait_for(reexec.exists)
        assert version() == 3
        output = finished(child("serve.run(with_dashboard=False)"), 1)
        assert str(first.pid) in output
        assert sql("SELECT count(*) FROM schema_migrations WHERE n=3") == [(1,)]
    finally:
        stop(first)


def test_a_contract_waits_for_an_older_child():
    init()
    probe("contract")
    finished(child('''
older_run = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
older_serve = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
server = None
try:
    for stamp in (2, None):
        sql("INSERT OR REPLACE INTO runs (id, kind, status, started, pid, schema_version) "
            "VALUES ('older', 'loop', 'running', ?, ?, ?)", (util.iso(), older_run.pid, stamp))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert state.migrate()
            state.ensure_schema()
        assert "older" in output.getvalue() and "waits for" in output.getvalue(), output.getvalue()
        assert version() == 2
    sql("UPDATE runs SET schema_version=3")
    for at in (util.iso(), util.iso(util.now() - timedelta(days=1))):
        for stamp in (2, None):
            beat = dict(pid=older_serve.pid, at=at)
            if stamp is not None:
                beat["schema"] = stamp
            util.write_json(control.BEAT_FILE, beat)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert state.migrate()
            assert f"serve {older_serve.pid}" in output.getvalue(), output.getvalue()
            assert version() == 2
    assert state.migrate(contracts=False)
    assert version() == 2
    server = serve.Server()
    server.contracts_waiting = state.migrate()
    server.config_problem = "scheduler disabled for contract test"
    with mock.patch.object(server, "read_config"), mock.patch.object(state, "migrate", wraps=state.migrate) as migrate:
        server.tick()
        assert server.contracts_waiting and version() == 2
        assert util.read_json(control.BEAT_FILE, {})["schema"] == state.LATEST
        sql("UPDATE runs SET schema_version=2")
        sql("INSERT INTO runs (id, kind, status, started, pid, schema_version) "
            "VALUES ('unstamped', 'loop', 'running', ?, ?, NULL)", (util.iso(), older_run.pid))
        older_serve.terminate()
        older_serve.wait(timeout=10)
        util.write_json(control.BEAT_FILE, dict(pid=older_serve.pid, at=util.iso(), schema=2))
        server.tick()
        assert server.contracts_waiting and version() == 2
        older_run.terminate()
        older_run.wait(timeout=10)
        server.tick()
        assert not server.contracts_waiting and version() == 3
        assert migrate.call_count == 3
        server.tick()
        assert migrate.call_count == 3
    assert sql("SELECT id, status FROM runs ORDER BY id") == [("older", "running"), ("unstamped", "running")]
    assert sql("SELECT count(*) FROM schema_migrations WHERE n=3") == [(1,)]
finally:
    for process in (older_run, older_serve):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
    if server is not None:
        server.pool.shutdown(wait=True)
        for pool in server.job_pools.values():
            pool.shutdown(wait=True)
'''))
    assert version() == 3


def test_runs_name_their_schema():
    init()
    with mock.patch.object(state, "LATEST", 1):
        rid = record.start("loop", pid=None, loop="fixture")
    run = SimpleNamespace(events_file=paths.STATE_DIR / "missing.json")
    with mock.patch.object(state, "LATEST", 2), mock.patch.object(engine, "LoopRun", return_value=run), \
            mock.patch.object(engine.loops, "find"), mock.patch.object(engine, "carry", return_value="done"):
        engine.carry_opened(rid)
    row = state.run_get(rid)
    assert row["schema_version"] == 2 and row["engine_sha"] == engine.codebase.STAMP, row
    record.move(rid, "done", ("running",), finished=util.iso())
    (paths.STATE_DIR.parent / "authority-parent").mkdir()
    from tests import offline_workflow
    offline_workflow.main(paths.STATE_DIR.parent, "pass")
    rows = sql("SELECT schema_version FROM runs WHERE kind = 'pipeline'")
    assert rows and all(row[0] == state.LATEST for row in rows), rows


def test_the_instance_keeps_its_name():
    legacy_db()
    state.migrate()
    identity = dict(sql("SELECT key, value FROM schema_meta WHERE key LIKE 'instance_%'"))
    assert re.fullmatch("[0-9a-f]{32}", identity["instance_id"])
    assert identity["instance_tag"] == identity["instance_id"][:8]
    assert identity["instance_name"]
    assert sql("SELECT ? GLOB ?", (identity["instance_created"], state.TIME_GLOB)) == [(1,)]
    finished(child("state.migrate()"))
    assert identity == dict(sql("SELECT key, value FROM schema_meta WHERE key LIKE 'instance_%'"))
    snapshot = next(p for p in backup.snapshots() if p.name.endswith("-m1.db"))
    with mock.patch.object(control, "serve_beat", return_value={"alive": False}):
        assert cli.main(["backup", "--restore", str(snapshot)]) == 0
    state.migrate()
    assert identity == dict(sql("SELECT key, value FROM schema_meta WHERE key LIKE 'instance_%'"))
    meta = meta_from_dashboard()
    assert meta["instance"] == {k.removeprefix("instance_"): v for k, v in identity.items()}, meta
    assert meta["schema"] == {"version": version(), "oldest_writer": 0}, meta


def shape():
    with sqlite3.connect(paths.DB_FILE) as conn:
        objects = list(conn.execute("SELECT type, name, sql FROM sqlite_master "
                                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"))
        tables = {}
        for kind, name, statement in objects:
            if kind == "table":
                tables[name] = sorted((r[1], r[2], r[3], r[4])
                                      for r in conn.execute(f'PRAGMA table_info("{name}")'))
        other = [(kind, name, statement) for kind, name, statement in objects if kind != "table"]
        return tables, other


def test_the_chain_equals_a_fresh_db():
    init()
    expected = shape()
    original = paths.DB_FILE
    try:
        for end in (0, 1, 2, "legacy"):
            paths.DB_FILE = original.parent / f"at-{end}.db"
            if end == "legacy":
                legacy_db()
            elif end:
                conn = paths.connect_db(paths.DB_FILE, create=True)
                conn.execute("BEGIN EXCLUSIVE")
                for migration in migrations.load()[:end]:
                    historical = CHECKOUT / "migration-history" / migration.name
                    namespace = {"__file__": str(historical), "__name__": migration.name}
                    exec(compile(historical.read_bytes(), str(historical), "exec"), namespace)
                    namespace["apply"](conn)
                    conn.execute("INSERT INTO schema_migrations (n,name,area,phase,sha256,applied) VALUES (?,?,?,?,?,?)",
                                 (migration.n, migration.name, migration.area, migration.phase,
                                  hashlib.sha256(historical.read_bytes()).hexdigest(), util.iso()))
                    conn.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (str(migration.n),))
                    conn.execute(f"PRAGMA user_version = {migration.n}")
                conn.commit()
                conn.close()
            state.migrate()
            assert shape() == expected, end
    finally:
        paths.DB_FILE = original


def test_bad_folders_are_refused():
    init()
    folder = CHECKOUT / "arete/migrations"
    latest = (folder / "latest.txt").read_bytes()
    m1 = folder / "m0001_foundation_ledger.py"
    original = m1.read_bytes()
    cases = [
        ("m0002_work_duplicate.py", "PHASE='expand'\nSQL='SELECT 1;'\n", ["m0002_work_duplicate.py", "m0002_foundation_time_shape.py"]),
        ("m0004_foundation_gap.py", "PHASE='expand'\nSQL='SELECT 1;'\n", ["m0004_foundation_gap.py"]),
        ("m0003_unknown_area.py", "PHASE='expand'\nSQL='SELECT 1;'\n", ["m0003_unknown_area.py"]),
        ("latest.txt", "m0001_foundation_ledger.py\n", ["latest.txt", "m0002_foundation_time_shape.py"]),
        (m1.name, original.decode() + "\n", [m1.name]),
    ]
    for filename, content, names in cases:
        before = paths.DB_FILE.read_bytes()
        target = folder / filename
        target.write_text(content)
        try:
            output = finished(child("state.migrate()"), 1)
            assert all(name in output for name in names), output
            assert paths.DB_FILE.read_bytes() == before
        finally:
            if filename == m1.name:
                m1.write_bytes(original)
            elif filename == "latest.txt":
                target.write_bytes(latest)
            else:
                target.unlink()


def test_parallel_migrations_conflict():
    repo = paths.STATE_DIR.parent / "git"
    repo.mkdir()
    shutil.copytree(CHECKOUT / "arete/migrations", repo / "migrations")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com"}

    def git(*args, expected=0):
        result = subprocess.run(["git", *args], cwd=repo, env=env, text=True, capture_output=True)
        assert result.returncode == expected, result.stdout + result.stderr
        return result.stdout

    git("init", "-q", "-b", "main")
    git("add", "migrations")
    git("commit", "-qm", "base")
    for branch in ("left", "right"):
        git("checkout", "-qb", branch, "main")
        name = f"m0003_foundation_{branch}.py"
        (repo / "migrations" / name).write_text("PHASE='expand'\nSQL='SELECT 1;'\n")
        (repo / "migrations/latest.txt").write_text(name + "\n")
        git("add", "migrations")
        git("commit", "-qm", branch)
    output = git("merge-tree", "--write-tree", "left", "right", expected=1)
    assert "migrations/latest.txt" in output, output


def test_times_have_one_shape():
    legacy_db()
    for value in ("2026-11-08T10:04:51-05:00", "", None):
        sql("INSERT INTO verdicts (at, outcome_at) VALUES (?, ?)", (value, value))
    state.migrate()
    assert sql("SELECT at, outcome_at FROM verdicts") == [
        ("2026-11-08T15:04:51Z", "2026-11-08T15:04:51Z"), ("", ""), (None, None)]
    assert len(state.verdicts_since("2026-11-08T15:00:00Z")) == 1
    with sqlite3.connect(paths.DB_FILE) as conn:
        for table, columns in state.TIME_COLUMNS.items():
            if not conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                required = {r[1]: (1 if "INT" in r[2] else f"fixture-{table}")
                            for r in conn.execute(f'PRAGMA table_info("{table}")')
                            if (r[3] and r[4] is None) or (r[5] and r[2] != "INTEGER")}
                required.update({column: util.iso() for column in columns})
                names = ", ".join(f'"{column}"' for column in required)
                conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" * len(required))})',
                             tuple(required.values()))
            for column in columns:
                for statement in (f'INSERT INTO "{table}" ("{column}") VALUES (?)',
                                  f'UPDATE "{table}" SET "{column}" = ?'):
                    if statement.startswith("UPDATE"):
                        invalid = " OR ".join(f'(NEW."{c}" IS NOT NULL AND NEW."{c}" != \'\' '
                                              f'AND NEW."{c}" NOT GLOB \'{state.TIME_GLOB}\')' for c in columns)
                        assert invalid in conn.execute("SELECT sql FROM sqlite_master WHERE name=?",
                                                       (f"{table}_time_update",)).fetchone()[0]
                    try:
                        conn.execute(statement, ("2026-11-08T10:04:51-05:00",))
                    except sqlite3.IntegrityError as e:
                        assert "timestamp" in str(e), (table, column, str(e))
                    else:
                        raise AssertionError((table, column, statement))
    sql("UPDATE triage SET updated = 'tracker-etag'")
    sql("UPDATE research SET updated = 'tracker-etag', moved = 'running'")
    sql("UPDATE runs SET landed = 'main', moved = 'done', label_pending = 'triage', wait_minutes = '{}', ab_held = '{}'")
    probe(statement="ALTER TABLE feed ADD COLUMN rollback_probe TEXT; "
                    "INSERT INTO verdicts(at) VALUES ('2026-11-08T10:04:51-05:00');")
    output = finished(child("state.migrate()"), 1)
    assert "timestamp" in output and version() == 2, output
    assert sql("SELECT count(*) FROM schema_migrations WHERE n=3") == [(0,)]
    assert "rollback_probe" not in {r[1] for r in sql("PRAGMA table_info(feed)")}
    old = paths.DB_FILE
    try:
        paths.DB_FILE = old.parent / "invalid.db"
        legacy_db()
        sql("INSERT INTO verdicts(at) VALUES ('yesterday')")
        try:
            state.migrate()
        except migrations.Refused as e:
            assert "verdicts.at" in str(e), str(e)
        else:
            raise AssertionError("accepted yesterday")
        assert version() == 1
    finally:
        paths.DB_FILE = old


if __name__ == "__main__":
    install_trace()
    globals()[sys.argv[1]]()
    print("SCHEMA_LEDGER_OK")
