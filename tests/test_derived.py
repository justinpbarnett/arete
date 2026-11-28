from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import (
    auditor,
    cli,
    config,
    dashboard,
    dbstats,
    derived,
    doctor,
    gh,
    paths,
    principals,
    serve,
    tailnet,
    util,
)
from arete.store import db as foundation
from arete.store import derived as derived_store
from arete.store import tokens as token_store
from tests import support


def setUpModule() -> None:
    support.isolate_live_state()


def _scratch_totals_provider(*, code_version: int = 1, multiplier: int = 1, fail_rebuild: bool = False) -> derived.Provider:
    def source_mark(conn: sqlite3.Connection, _name: str) -> str:
        rows = conn.execute("SELECT id, grp, amount FROM scratch_items ORDER BY id").fetchall()
        return util.digest(json.dumps([tuple(r) for r in rows]))

    def diff(conn: sqlite3.Connection, name: str) -> derived.DiffReport:
        conn.execute("CREATE TEMP TABLE temp_scratch_totals (grp TEXT PRIMARY KEY, total INTEGER NOT NULL) STRICT")
        conn.execute(
            "INSERT INTO temp.temp_scratch_totals (grp, total) "
            "SELECT grp, SUM(amount) * ? FROM main.scratch_items GROUP BY grp",
            (multiplier,),
        )
        return derived.exact_table_diff(
            conn,
            "scratch_totals",
            "temp_scratch_totals",
            ("grp", "total"),
            key_columns=("grp",),
            source_mark=source_mark(conn, name),
        )

    def rebuild(conn: sqlite3.Connection, name: str) -> str:
        conn.execute("DELETE FROM scratch_totals")
        conn.execute(
            "INSERT INTO scratch_totals (grp, total) "
            "SELECT grp, SUM(amount) * ? FROM scratch_items GROUP BY grp",
            (multiplier,),
        )
        if fail_rebuild:
            raise RuntimeError("boom during rebuild")
        return source_mark(conn, name)

    return derived.Provider(
        name="scratch.totals",
        code_version=code_version,
        required_tables=("scratch_items", "scratch_totals"),
        required_columns={"scratch_items": ("id", "grp", "amount"), "scratch_totals": ("grp", "total")},
        source_mark=source_mark,
        diff=diff,
        rebuild=rebuild,
    )


def _scratch_band_provider(*, code_version: int = 1) -> derived.Provider:
    def enumerate_names(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute("SELECT name FROM scratch_lines ORDER BY name").fetchall()
        return [f"band:{r[0]}" for r in rows]

    def _line(store_name: str) -> str:
        return store_name.removeprefix("band:")

    def source_mark(conn: sqlite3.Connection, store_name: str) -> str:
        line = _line(store_name)
        rows = conn.execute(
            "SELECT id, seconds FROM scratch_line_tries WHERE line = ? ORDER BY id",
            (line,),
        ).fetchall()
        return util.digest(json.dumps([tuple(r) for r in rows]))

    def _compute_band(conn: sqlite3.Connection, line: str) -> str:
        vals = sorted(
            r[0]
            for r in conn.execute("SELECT seconds FROM scratch_line_tries WHERE line = ? ORDER BY id", (line,))
        )
        if len(vals) < 2:
            return "[]"
        return json.dumps([vals[0], vals[-1]])

    def diff(conn: sqlite3.Connection, store_name: str) -> derived.DiffReport:
        line = _line(store_name)
        expected = _compute_band(conn, line)
        conn.execute("CREATE TEMP TABLE temp_line_band (name TEXT PRIMARY KEY, band TEXT NOT NULL) STRICT")
        conn.execute("INSERT INTO temp.temp_line_band (name, band) VALUES (?, ?)", (line, expected))
        return derived.exact_table_diff(
            conn,
            "scratch_lines",
            "temp_line_band",
            ("name", "band"),
            key_columns=("name",),
            where="name = ?",
            params=(line,),
            source_mark=source_mark(conn, store_name),
        )

    def rebuild(conn: sqlite3.Connection, store_name: str) -> str:
        line = _line(store_name)
        expected = _compute_band(conn, line)
        conn.execute("UPDATE scratch_lines SET band = ? WHERE name = ?", (expected, line))
        return source_mark(conn, store_name)

    return derived.Provider(
        name="band:",
        code_version=code_version,
        required_tables=("scratch_lines", "scratch_line_tries"),
        required_columns={"scratch_lines": ("name", "band"), "scratch_line_tries": ("id", "line", "seconds")},
        source_mark=source_mark,
        diff=diff,
        rebuild=rebuild,
        enumerate_names=enumerate_names,
    )


class DerivedStoreFrameworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        tmp = tempfile.TemporaryDirectory(prefix="arete-derived-", dir=Path("/tmp").resolve())
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / "state"
        self.cfg_dir = self.root / "config"
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg_dir / "config.toml").write_text('alert_repo = ""\n')
        for name, value in {
            "STATE_DIR": self.state,
            "DB_FILE": self.state / "arete.db",
            "CONFIG_DIR": self.cfg_dir,
            "CONFIG_FILE": self.cfg_dir / "config.toml",
            "LOCKS_DIR": self.state / "locks",
            "RUNS_DIR": self.state / "runs",
            "LOG_DIR": self.root / "logs",
            "WORKTREES_DIR": self.root / "worktrees",
            "SERVER_FILE": self.state / "server.json",
            "HEARTBEAT_FILE": self.state / "heartbeat.json",
            "SERVE_BEAT_FILE": self.state / "serve.json",
            "HOME": self.root / "home",
            "CLAUDE_SETTINGS": self.root / "home" / ".claude" / "settings.json",
        }.items():
            self._stack.enter_context(mock.patch.object(paths, name, value))
        self._stack.enter_context(mock.patch.object(auditor, "AUDIT_DIR", self.state / "audit"))
        self._stack.enter_context(mock.patch.object(foundation, "_CHECKED", set()))
        self._stack.enter_context(mock.patch.object(dbstats.Timing, "finished"))
        self._stack.enter_context(mock.patch.dict(os.environ, {"ARETE_DESKTOP_NOTIFY": "0"}))
        # Fail closed on external adapters.
        self._stack.enter_context(mock.patch.object(gh, "issue_state", side_effect=AssertionError("unexpected gh call")))
        self._stack.enter_context(mock.patch.object(util, "sh", side_effect=AssertionError("unexpected shell call")))
        # Keep the global registry isolated per test.
        self._stack.enter_context(mock.patch.object(derived, "REGISTRY", derived.Registry()))

    def _seed_full_db(self) -> None:
        support.seed_db(self.state)

    def _create_scratch_tables(self) -> None:
        with foundation.tx() as c:
            c.execute(
                "CREATE TABLE scratch_items (id INTEGER PRIMARY KEY, grp TEXT NOT NULL, amount INTEGER NOT NULL) STRICT"
            )
            c.execute("CREATE TABLE scratch_totals (grp TEXT PRIMARY KEY, total INTEGER NOT NULL) STRICT")
            c.executemany(
                "INSERT INTO scratch_items (id, grp, amount) VALUES (?, ?, ?)",
                [(1, "alpha", 10), (2, "alpha", 15), (3, "beta", 7), (4, "gamma", 4)],
            )
            # Drift: alpha wrong value (20 != 25), gamma missing, delta extra -> 3 exact diff rows.
            c.executemany(
                "INSERT INTO scratch_totals (grp, total) VALUES (?, ?)",
                [("alpha", 20), ("beta", 7), ("delta", 99)],
            )

    @contextmanager
    def _live_api(self):
        principals.seed_owner("Owner", "owner@example.test")
        with foundation.tx() as c:
            token_store.ensure_key(c)
            owner = principals.owner()
            _, token = token_store.issue(owner, "Derived API test", ["*"])
        node = tailnet.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "127.0.0.1")
        logins = tailnet.Logins(SimpleNamespace(whois=lambda _: "owner@example.test"))
        with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        httpd.node = node
        httpd.logins = logins
        httpd.inside_serve = True
        httpd.front = tailnet.Front("http", [])
        httpd.pending_secrets = {}
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield port, token
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    @staticmethod
    def _api_get(port: int, path: str, token: str) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def test_migration_creates_strict_derived_table(self) -> None:
        self._seed_full_db()
        with foundation.reader() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], foundation.LATEST)
            self.assertTrue(derived_store.available(c))
            cols = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
                    for r in c.execute("PRAGMA table_info(derived)")]
            self.assertEqual(
                cols,
                [
                    ("name", "TEXT", 1, None, 1),
                    ("code_version", "INTEGER", 1, None, 0),
                    ("source_mark", "TEXT", 1, None, 0),
                    ("rebuilt", "TEXT", 0, None, 0),
                    ("checked", "TEXT", 0, None, 0),
                    ("diff_rows", "INTEGER", 1, "0", 0),
                ],
            )
            ddl = c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'derived'").fetchone()[0]
            self.assertIn("STRICT", ddl.upper())

        with foundation.tx() as c, self.assertRaises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO derived (name, code_version, source_mark) VALUES ('bad', 'not-an-int', 'm1')"
            )

    def test_scratch_provider_drift_readonly_doctor_rebuild_startup_and_reset_event(self) -> None:
        self._seed_full_db()
        self._create_scratch_tables()
        provider_v1 = _scratch_totals_provider(code_version=1, multiplier=1)

        with foundation.reader() as c:
            self.assertTrue(derived.register_if_available(c, provider_v1))

        # Doctor reads must detect exact drift (3 rows) without mutating live DB or events.
        before_bytes = paths.DB_FILE.read_bytes()
        out = StringIO()
        with redirect_stdout(out):
            rc = cli.main(["doctor", "derived:scratch.totals"])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL derived:scratch.totals    3", out.getvalue())
        self.assertIn("alpha", out.getvalue())
        self.assertIn("delta", out.getvalue())
        self.assertIn("gamma", out.getvalue())
        self.assertEqual(paths.DB_FILE.read_bytes(), before_bytes)
        with foundation.reader() as c:
            self.assertIsNone(derived_store.get("scratch.totals", c))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_temp_master").fetchone()[0], 0)

        # Explicit rebuild repairs live store and writes derived metadata (with checked still NULL).
        rebuilt_v1 = derived.rebuild_store("scratch.totals")
        self.assertEqual(rebuilt_v1["code_version"], 1)
        self.assertIsNotNone(rebuilt_v1["rebuilt"])
        self.assertIsNone(rebuilt_v1["checked"])
        self.assertEqual(rebuilt_v1["diff_rows"], 0)

        out = StringIO()
        with redirect_stdout(out):
            rc = cli.main(["doctor", "derived:scratch.totals"])
        self.assertEqual(rc, 0)
        self.assertIn("OK   derived:scratch.totals    0  matches source", out.getvalue())

        # Seed a row for a future store whose provider is absent: startup must leave it alone.
        with foundation.tx() as c:
            derived_store.record_rebuilt(c, "future.unregistered", code_version=1, source_mark="m0")

        # Advance code_version to 2 (multiplier=2) and trigger serve startup rebuild.
        provider_v2 = _scratch_totals_provider(code_version=2, multiplier=2)
        derived.register(provider_v2, replace=True)
        logs: list[str] = []
        server = serve.Server()
        server.say = logs.append
        with principals.acting("hand"):
            rebuilt_list = server.start_derived()
        self.assertEqual([r["name"] for r in rebuilt_list], ["scratch.totals"])
        self.assertEqual(rebuilt_list[0]["code_version"], 2)
        self.assertIn("reset_seq", rebuilt_list[0])
        self.assertTrue(any("derived:scratch.totals: rebuilt from v1 to v2" in line for line in logs))

        with foundation.reader() as c:
            totals = dict(c.execute("SELECT grp, total FROM scratch_totals ORDER BY grp").fetchall())
            self.assertEqual(totals, {"alpha": 50, "beta": 14, "gamma": 8})
            row = derived_store.get("scratch.totals", c)
            self.assertEqual(row["code_version"], 2)
            self.assertIsNotNone(row["rebuilt"])
            self.assertIsNone(row["checked"])
            # Future producer untouched.
            future_row = derived_store.get("future.unregistered", c)
            self.assertEqual(future_row["code_version"], 1)
            self.assertEqual(future_row["source_mark"], "m0")
            reset_rows = c.execute(
                "SELECT kind, subject, ver, class, data FROM events_log WHERE kind = 'reset' ORDER BY seq"
            ).fetchall()
            self.assertEqual(len(reset_rows), 1)
            self.assertEqual(reset_rows[0]["subject"], "derived:scratch.totals")
            self.assertEqual(reset_rows[0]["ver"], 2)
            self.assertEqual(reset_rows[0]["class"], "durable")
            payload = json.loads(reset_rows[0]["data"])
            self.assertEqual(payload["reason"], "derived.rebuilt")
            self.assertEqual(payload["from_version"], 1)
            self.assertEqual(payload["code_version"], 2)

        # Verify the reset event is surfaced over the real /api/v1/events endpoint.
        with self._live_api() as (port, token):
            status, body = self._api_get(port, "/api/v1/events?since=0&timeout=0&kinds=reset", token)
            self.assertEqual(status, 200)
            self.assertEqual(len(body["events"]), 1)
            self.assertEqual(body["events"][0]["kind"], "reset")
            self.assertEqual(body["events"][0]["subject"], "derived:scratch.totals")

        # Atomicity check: a failing v3 rebuild rolls back table, derived row, and reset event.
        provider_v3_fail = _scratch_totals_provider(code_version=3, multiplier=3, fail_rebuild=True)
        derived.register(provider_v3_fail, replace=True)
        with self.assertRaisesRegex(RuntimeError, "boom during rebuild"):
            derived.rebuild_stale_on_startup()
        with foundation.reader() as c:
            totals = dict(c.execute("SELECT grp, total FROM scratch_totals ORDER BY grp").fetchall())
            self.assertEqual(totals, {"alpha": 50, "beta": 14, "gamma": 8})
            self.assertEqual(derived_store.get("scratch.totals", c)["code_version"], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events_log WHERE kind = 'reset'").fetchone()[0], 1)

    def test_audit_records_check_raises_one_normal_inbox_alert_and_clears_on_clean_recheck(self) -> None:
        self._seed_full_db()
        self._create_scratch_tables()
        provider = _scratch_totals_provider(code_version=1, multiplier=1)
        with foundation.reader() as c:
            self.assertTrue(derived.register_if_available(c, provider))

        # Seed a clean backup so only our derived store check fails during audit.
        with mock.patch.object(
            doctor,
            "check_backup",
            return_value=doctor.Result("backup", "fail", 0, [], "ok"),
        ):
            # Dry-run must not write derived metadata or raise alerts.
            out = StringIO()
            with redirect_stdout(out):
                rc = cli.main(["audit", "--dry-run"])
            self.assertEqual(rc, 0)
            with foundation.reader() as c:
                self.assertIsNone(derived_store.get("scratch.totals", c))
                self.assertEqual(c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

            # Real audit pass #1: records checked/source_mark/diff_rows=3 and raises ONE normal inbox alert.
            out = StringIO()
            with redirect_stdout(out):
                rc = auditor.run(dry_run=False)
            self.assertEqual(rc, 0)
            with foundation.reader() as c:
                row = derived_store.get("scratch.totals", c)
                self.assertIsNotNone(row)
                self.assertIsNotNone(row["checked"])
                self.assertEqual(row["diff_rows"], 3)
                self.assertEqual(row["source_mark"], provider.source_mark(c, "scratch.totals"))

            # Real audit pass #2 while still drifted: still exactly ONE open normal inbox alert.
            with redirect_stdout(StringIO()):
                rc = auditor.run(dry_run=False)
            self.assertEqual(rc, 0)
            with self._live_api() as (port, token):
                status, body = self._api_get(port, "/api/v1/inbox?state=open", token)
                self.assertEqual(status, 200)
                alert_items = [
                    item for item in body["items"]
                    if item["subject"] == "alert:audit:doctor:derived:scratch.totals"
                ]
                self.assertEqual(len(alert_items), 1)
                self.assertEqual(alert_items[0]["kind"], "alert")
                self.assertEqual(alert_items[0]["priority"], 2)

            # Rebuild cleanly and re-run audit: diff_rows=0 and alert clears.
            derived.rebuild_store("scratch.totals")
            with redirect_stdout(StringIO()):
                rc = auditor.run(dry_run=False)
            self.assertEqual(rc, 0)
            with foundation.reader() as c:
                row = derived_store.get("scratch.totals", c)
                self.assertEqual(row["diff_rows"], 0)
                self.assertIsNotNone(row["checked"])
                self.assertEqual(row["source_mark"], provider.source_mark(c, "scratch.totals"))
                self.assertEqual(
                    c.execute(
                        "SELECT COUNT(*) FROM alerts WHERE key = 'audit:doctor:derived:scratch.totals'"
                    ).fetchone()[0],
                    0,
                )
            with self._live_api() as (port, token):
                status, body = self._api_get(port, "/api/v1/inbox?state=open", token)
                self.assertEqual(status, 200)
                self.assertEqual(
                    [i for i in body["items"] if i["subject"] == "alert:audit:doctor:derived:scratch.totals"],
                    [],
                )

    def test_consumer_expected_coverage_and_staleness_never_trust_empty_or_unchecked(self) -> None:
        self._seed_full_db()

        # Empty/undeclared consumer is never clean.
        with foundation.reader() as c:
            empty = derived.coverage(c, "charts")
            self.assertFalse(empty.complete)
            self.assertTrue(empty.stale)
            self.assertTrue(derived.charts_stale(c))

        # Expected consumer coverage with empty provider registry -> stale + failing doctor check.
        derived.expect_coverage("charts", ("scratch.totals",))
        with foundation.reader() as c:
            cov = derived.coverage(c, "charts")
            self.assertFalse(cov.complete)
            self.assertTrue(cov.stale)
            self.assertEqual(cov.missing, ("scratch.totals",))
        results = doctor.run()
        missing_res = next(r for r in results if r.name == "derived:scratch.totals")
        self.assertTrue(missing_res.failing)
        self.assertIn("incomplete coverage for charts", missing_res.detail)
        self.assertIn("provider not registered", missing_res.detail)

        # Provider whose source tables do not exist yet: register_if_available refuses; direct register still unavailable.
        provider = _scratch_totals_provider(code_version=1, multiplier=1)
        with foundation.reader() as c:
            self.assertFalse(derived.register_if_available(c, provider))
            self.assertEqual(derived.providers(), ())
        derived.register(provider)
        with foundation.reader() as c:
            self.assertEqual(derived.active_providers(c), ())
            cov = derived.coverage(c, "charts")
            self.assertTrue(cov.stale)
            self.assertEqual(cov.missing, ("scratch.totals",))
        results = doctor.run()
        unavail_res = next(r for r in results if r.name == "derived:scratch.totals")
        self.assertTrue(unavail_res.failing)
        self.assertIn("source tables or producer unavailable", unavail_res.detail)

        # Create tables and rebuild: checked is NULL and diff_rows=0 (SQL default) -> STILL stale!
        self._create_scratch_tables()
        derived.rebuild_store("scratch.totals")
        with foundation.reader() as c:
            row = derived_store.get("scratch.totals", c)
            self.assertIsNone(row["checked"])
            self.assertEqual(row["diff_rows"], 0)
            cov = derived.coverage(c, "charts")
            self.assertFalse(cov.complete)
            self.assertTrue(cov.stale)
            self.assertEqual(cov.unchecked, ("scratch.totals",))

        # Audit records checked + current source_mark + diff_rows=0 -> NOW fresh!
        with mock.patch.object(
            doctor,
            "check_backup",
            return_value=doctor.Result("backup", "fail", 0, [], "ok"),
        ):
            auditor.audit(config.load(), record_derived=True)
        with foundation.reader() as c:
            cov = derived.coverage(c, "charts")
            self.assertTrue(cov.complete)
            self.assertFalse(cov.stale)
            self.assertFalse(derived.charts_stale(c))

        # Mutate source so source_mark changes while derived.diff_rows is still 0 -> stale again!
        with foundation.tx() as c:
            c.execute("INSERT INTO scratch_items (id, grp, amount) VALUES (5, 'alpha', 3)")
        with foundation.reader() as c:
            cov = derived.coverage(c, "charts")
            self.assertFalse(cov.complete)
            self.assertTrue(cov.stale)
            self.assertEqual(cov.stale_stores, ("scratch.totals",))

    def test_band_family_provider_supported_without_money_or_refs_dependencies(self) -> None:
        self._seed_full_db()
        with foundation.tx() as c:
            c.execute("CREATE TABLE scratch_lines (name TEXT PRIMARY KEY, band TEXT NOT NULL) STRICT")
            c.execute(
                "CREATE TABLE scratch_line_tries (id INTEGER PRIMARY KEY, line TEXT NOT NULL, seconds REAL NOT NULL) STRICT"
            )
            c.executemany("INSERT INTO scratch_lines (name, band) VALUES (?, ?)", [("web", "[]"), ("api", "[]")])
            c.executemany(
                "INSERT INTO scratch_line_tries (id, line, seconds) VALUES (?, ?, ?)",
                [(1, "web", 10.0), (2, "web", 30.0), (3, "api", 5.0), (4, "api", 15.0)],
            )

        band_provider = _scratch_band_provider(code_version=1)
        derived.expect_coverage("charts", ("band:",))
        with foundation.reader() as c:
            self.assertTrue(derived.register_if_available(c, band_provider))
            self.assertTrue(derived.charts_stale(c))

        # Doctor discovers both derived:band:api and derived:band:web and reports drift without mutating.
        results = doctor.run()
        by_name = {r.name: r for r in results}
        self.assertIn("derived:band:api", by_name)
        self.assertIn("derived:band:web", by_name)
        self.assertEqual(by_name["derived:band:web"].count, 1)
        self.assertEqual(by_name["derived:band:api"].count, 1)

        # Rebuild only web -> charts STILL stale because api is drifted/unchecked.
        derived.rebuild_store("band:web")
        with mock.patch.object(
            doctor,
            "check_backup",
            return_value=doctor.Result("backup", "fail", 0, [], "ok"),
        ):
            auditor.audit(config.load(), record_derived=True)
        with foundation.reader() as c:
            cov = derived.coverage(c, "charts")
            self.assertFalse(cov.complete)
            self.assertTrue(cov.stale)
            self.assertEqual(cov.drifted, ("band:api",))

        # Rebuild api and re-audit -> all band:<line> stores are fresh and charts are no longer stale.
        derived.rebuild_store("band:api")
        with mock.patch.object(
            doctor,
            "check_backup",
            return_value=doctor.Result("backup", "fail", 0, [], "ok"),
        ):
            auditor.audit(config.load(), record_derived=True)
        with foundation.reader() as c:
            cov = derived.coverage(c, "charts")
            self.assertTrue(cov.complete)
            self.assertFalse(cov.stale)
            self.assertEqual(
                dict(c.execute("SELECT name, band FROM scratch_lines ORDER BY name").fetchall()),
                {"api": "[5.0, 15.0]", "web": "[10.0, 30.0]"},
            )

    def test_older_schema_waiting_on_legitimate_writer_neither_registers_nor_forces_contract(self) -> None:
        # Migrate only through schema 51 (right before contract m0052_outbox_approvals_contract.py).
        waiting = foundation.migrate(through=51)
        self.assertFalse(waiting)
        with foundation.tx() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 51)
            self.assertFalse(derived_store.available(c))
            # Insert a live legitimate older writer running at schema 51.
            c.execute(
                "INSERT INTO runs (id, kind, status, started, pid, schema_version) VALUES (?, 'pipeline', 'running', ?, ?, 51)",
                ("legacy-writer-51", util.iso(), os.getpid()),
            )

        # Full migrate waits on the live older writer and must NOT force contract m0052.
        contracts_waiting = foundation.migrate()
        self.assertTrue(contracts_waiting)
        with foundation.reader() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 51)
            self.assertFalse(derived_store.available(c))

        # Framework refuses to register unavailable sources on the older schema.
        provider = _scratch_totals_provider(code_version=2)
        with foundation.reader() as c:
            self.assertFalse(derived.register_if_available(c, provider))
            self.assertEqual(derived.providers(), ())

        # Even if registered in code, active_providers is empty and startup neither rebuilds nor forces contract.
        derived.register(provider)
        derived.expect_coverage("charts", ("scratch.totals",))
        with foundation.reader() as c:
            self.assertEqual(derived.active_providers(c), ())
            self.assertTrue(derived.charts_stale(c))

        self.assertEqual(derived.rebuild_stale_on_startup(contracts_waiting=True), [])
        self.assertEqual(derived.rebuild_stale_on_startup(contracts_waiting=False), [])

        # Serve tick with contracts_waiting=True keeps waiting without forcing m0052 or marking derived_started.
        server = serve.Server()
        server.contracts_waiting = True
        with principals.acting("hand"):
            self.assertFalse(server.tick())
        self.assertTrue(server.contracts_waiting)
        self.assertFalse(server.derived_started)
        with foundation.reader() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 51)

        # Doctor runs read-only without crashing or forcing a contract, and reports required coverage as incomplete.
        results = doctor.run()
        missing = next(r for r in results if r.name == "derived:scratch.totals")
        self.assertTrue(missing.failing)
        self.assertIn("derived table unavailable on this schema", missing.detail)
        with foundation.reader() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 51)

        # Once the older writer finishes, migrate/tick finishes m0052..m0055 and starts derived cleanly.
        with foundation.tx() as c:
            c.execute(
                "UPDATE runs SET status = 'done', finished = ? WHERE id = ?",
                (util.iso(), "legacy-writer-51"),
            )
        with principals.acting("hand"):
            server.tick()
        self.assertFalse(server.contracts_waiting)
        self.assertTrue(server.derived_started)
        with foundation.reader() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], foundation.LATEST)
            self.assertTrue(derived_store.available(c))


if __name__ == "__main__":
    unittest.main()
