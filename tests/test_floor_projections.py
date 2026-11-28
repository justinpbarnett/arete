from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import (
    adw,
    attention,
    auditor,
    bands,
    cli,
    config,
    configcheck,
    credential_files,
    dashboard,
    dbstats,
    doctor,
    floor,
    gh,
    lines,
    paths,
    principals,
    settings,
    tailnet,
    util,
)
from arete.store import db as foundation
from arete.store import derived as derived_store
from arete.store import inbox as inbox_store
from arete.store import projects as projects_store
from arete.store import tasks as task_store
from arete.store import tokens as token_store
from tests import support


def setUpModule() -> None:
    support.isolate_live_state()


class _FloorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        tmp = tempfile.TemporaryDirectory(prefix="arete-floor-", dir=Path("/tmp").resolve())
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / "state"
        self.cfg_dir = self.root / "config"
        self.repo_dir = self.root / "repos" / "demo"
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg_dir / "config.toml").write_text(
            f'alert_repo = ""\n\n[[repo]]\nname = "demo"\npath = "{self.repo_dir}"\npipeline = true\n',
            encoding="utf-8",
        )
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
            "EVENTS_FILE": self.state / "events.jsonl",
            "PUSH_KEY_FILE": self.state / "webpush.key",
            "FINGERPRINT_KEY_FILE": self.state / "fingerprint.key",
            "CLIENT_DIR": self.root / "client",
            "TOKEN_CLIENT_DIR": self.root / "tokens",
            "DEFAULT_TOKEN_CLIENT_DIR": self.root / "tokens",
            "HOME": self.root / "home",
            "CLAUDE_SETTINGS": self.root / "home" / ".claude" / "settings.json",
        }.items():
            self._stack.enter_context(mock.patch.object(paths, name, value))
        self._stack.enter_context(mock.patch.object(auditor, "AUDIT_DIR", self.state / "audit"))
        self._stack.enter_context(mock.patch.object(foundation, "_CHECKED", set()))
        self._stack.enter_context(mock.patch.object(dbstats.Timing, "finished"))
        self._stack.enter_context(mock.patch.dict(os.environ, {"ARETE_DESKTOP_NOTIFY": "0"}))
        self._stack.enter_context(mock.patch.object(gh, "issue_state", side_effect=AssertionError("unexpected gh call")))
        self._stack.enter_context(mock.patch.object(util, "sh", side_effect=AssertionError("unexpected shell call")))
        support.seed_db(self.state)
        principals.seed_owner("Owner", "owner@example.test")
        with principals.acting(principals.owner() or "hand"):
            lines.ensure(config.load())
            self.default_task = task_store.task_add("demo", "Seed task", title="Seed task")
            self.default_task_num = int(task_store.task_get(self.default_task)["num"])

    def _insert_run(
        self,
        conn,
        run_id: str,
        *,
        status: str,
        started: str,
        finished: str | None = None,
        line: str = "demo-pipeline",
        task_num: int | None = None,
        issue: str | None = None,
    ) -> None:
        tnum = self.default_task_num if task_num is None else task_num
        conn.execute(
            "INSERT INTO runs (id, kind, repo, issue_key, task_id, status, started, finished, line) "
            "VALUES (?, 'pipeline', 'demo', ?, ?, ?, ?, ?, ?)",
            (run_id, issue or self.default_task, tnum, status, started, finished, line),
        )

    @contextmanager
    def _live_api(self):
        with foundation.tx() as c:
            token_store.ensure_key(c)
            owner = principals.owner()
            _, full_token = token_store.issue(owner, "Floor full test", ["*"])
            _, read_token = token_store.issue(owner, "Floor read test", ["read"])
            tag = foundation.metadata(c)["instance_tag"]
        credential_files.write_client(tag, full_token)
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
            yield port, full_token, read_token
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    @staticmethod
    def _http(
        port: int,
        method: str,
        path: str,
        token: str,
        body: dict[str, object] | None = None,
        *,
        idem: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        if idem is not None:
            headers["Idempotency-Key"] = idem
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=payload if body is not None else None, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, hdrs, json.loads(raw) if raw else {}
        finally:
            conn.close()


class AttentionAndScreensTests(_FloorTestBase):
    def test_attention_schema_defaults_override_and_round_trip(self) -> None:
        self.assertEqual(attention.load_thresholds(), attention.DEFAULTS)
        saved_ask = settings.save_attention({"max_per_run": 4, "settle": False})
        self.assertEqual(saved_ask["max_per_run"], 4)
        self.assertFalse(saved_ask["settle"])
        self.assertEqual(settings.attention(), saved_ask)

        bad_zero = configcheck.check("attention", "defaults", "attention.toml", "standingLimit = 0\n", author="owner")
        self.assertFalse(bad_zero["ok"])
        bad_unknown = configcheck.check("attention", "defaults", "attention.toml", "mystery = 3\n", author="owner")
        self.assertFalse(bad_unknown["ok"])
        good_check = configcheck.check("attention", "defaults", "attention.toml", "standingLimit = 6\n", author="owner")
        self.assertTrue(good_check["ok"])

        old_ts = util.iso(util.now() - timedelta(hours=2))
        with foundation.tx() as c:
            for i in range(6):
                c.execute(
                    "INSERT INTO inbox (kind, subject, priority, title, state, raised, version) "
                    "VALUES ('alert', ?, 2, 'msg', 'open', ?, 1)",
                    (f"alert:a{i}", old_ts),
                )

        with self._live_api() as (port, token, _):
            st, _, before = self._http(port, "GET", "/api/v1/screens/triage", token)
            self.assertEqual(st, 200)
            self.assertEqual(before["andon"]["limit"], 10)
            self.assertEqual(before["andon"]["zone"], "STABLE")

            (self.cfg_dir / "attention.toml").write_text("standingLimit = 6\n", encoding="utf-8")
            st2, _, after = self._http(port, "GET", "/api/v1/screens/triage", token)
            self.assertEqual(st2, 200)
            self.assertEqual(after["andon"]["limit"], 6)
            self.assertEqual(after["andon"]["zone"], "REACTIVE")

    def test_andon_split_snooze_shelve_resolve_and_all_zones(self) -> None:
        old_ts = util.iso(util.now() - timedelta(hours=2))
        med_ids: list[str] = []
        with foundation.tx() as c:
            for i in range(3):
                c.execute(
                    "INSERT INTO inbox (kind, subject, priority, title, verbs, state, raised, version) "
                    "VALUES ('alert', ?, 1, 'hi', '[\"dismiss\"]', 'open', ?, 1)",
                    (f"alert:h{i}", old_ts),
                )
            for i in range(7):
                cur = c.execute(
                    "INSERT INTO inbox (kind, subject, priority, title, verbs, state, raised, version) "
                    "VALUES ('alert', ?, 2, 'med', '[\"dismiss\"]', 'open', ?, 1)",
                    (f"alert:m{i}", old_ts),
                )
                med_ids.append(inbox_store.format_id(cur.lastrowid))
            for i in range(2):
                c.execute(
                    "INSERT INTO inbox (kind, subject, priority, title, verbs, state, raised, version) "
                    "VALUES ('alert', ?, 3, 'low', '[\"dismiss\"]', 'open', ?, 1)",
                    (f"alert:l{i}", old_ts),
                )

        with self._live_api() as (port, token, _):
            st, _, tri = self._http(port, "GET", "/api/v1/screens/triage", token)
            self.assertEqual(st, 200)
            andon = tri["andon"]
            self.assertEqual(andon["count"], 10)
            self.assertEqual(andon["high"], 3)
            self.assertEqual(andon["med"], 7)
            self.assertEqual(andon["low"], 2)
            self.assertEqual(andon["standing"], 12)
            self.assertEqual(andon["limit"], 10)
            self.assertEqual(andon["aim"], 5)
            self.assertEqual(andon["zone"], "REACTIVE")
            self.assertIn("12", andon["why"])

            st_sn, _, _ = self._http(
                port, "POST", f"/api/v1/inbox/{med_ids[0]}/snooze", token,
                {"base_version": 1, "until": util.iso(util.now() + timedelta(hours=1))},
                idem="andon-snooze-1",
            )
            self.assertEqual(st_sn, 200)
            _, _, tri_sn = self._http(port, "GET", "/api/v1/screens/triage", token)
            self.assertEqual(tri_sn["andon"]["med"], 6)
            self.assertEqual(tri_sn["andon"]["standing"], 11)

            st_sh, _, _ = self._http(
                port, "POST", f"/api/v1/inbox/{med_ids[1]}/shelve", token,
                {"base_version": 1, "reason": "later"},
                idem="andon-shelve-1",
            )
            self.assertEqual(st_sh, 200)
            _, _, tri_sh = self._http(port, "GET", "/api/v1/screens/triage", token)
            self.assertEqual(tri_sh["andon"]["med"], 5)
            self.assertEqual(tri_sh["andon"]["standing"], 10)

            with foundation.tx() as c:
                inbox_store.resolve_item(c, inbox_store.parse_id(med_ids[2]), resolution="dismissed")
            _, _, tri_res = self._http(port, "GET", "/api/v1/screens/triage", token)
            self.assertEqual(tri_res["andon"]["med"], 4)
            self.assertEqual(tri_res["andon"]["standing"], 9)
            self.assertEqual(tri_res["andon"]["zone"], "STABLE")

        now = util.now()
        with foundation.tx() as c:
            c.execute("DELETE FROM inbox")
            for i in range(4):
                c.execute(
                    "INSERT INTO inbox (kind, subject, priority, title, state, raised, version) "
                    "VALUES ('alert', ?, 2, 'r', 'open', ?, 1)",
                    (f"alert:rob{i}", util.iso(now - timedelta(hours=2))),
                )
            self.assertEqual(attention.andon(c, now=now)["zone"], "ROBUST")

            for i in range(3):
                c.execute(
                    "INSERT INTO inbox (kind, subject, priority, title, state, raised, version) "
                    "VALUES ('alert', ?, 2, 's', 'open', ?, 1)",
                    (f"alert:stb{i}", util.iso(now - timedelta(hours=2))),
                )
            self.assertEqual(attention.andon(c, now=now)["zone"], "STABLE")

            for i in range(13):
                c.execute(
                    "INSERT INTO inbox (kind, subject, priority, title, state, raised, version) "
                    "VALUES ('alert', ?, 2, 'o', 'open', ?, 1)",
                    (f"alert:ov{i}", util.iso(now - timedelta(hours=2))),
                )
            self.assertEqual(attention.andon(c, now=now)["zone"], "OVERLOADED")

            c.execute("DELETE FROM inbox")
            for i in range(10):
                c.execute(
                    "INSERT INTO inbox (kind, subject, priority, title, state, raised, version) "
                    "VALUES ('alert', ?, 2, 'f', 'resolved', ?, 1)",
                    (f"alert:flood{i}", util.iso(now - timedelta(minutes=2))),
                )
            self.assertEqual(attention.andon(c, now=now)["standing"], 0)
            self.assertEqual(attention.andon(c, now=now)["zone"], "OVERLOADED")

            c.execute("DELETE FROM inbox")
            for bucket in range(6):
                for j in range(2):
                    c.execute(
                        "INSERT INTO inbox (kind, subject, priority, title, state, raised, version) "
                        "VALUES ('alert', ?, 2, 'sus', 'resolved', ?, 1)",
                        (f"alert:sus{bucket}-{j}", util.iso(now - timedelta(minutes=bucket * 10 + 3))),
                    )
            self.assertEqual(attention.andon(c, now=now)["zone"], "REACTIVE")

    def test_board_needs_you_step_marks_and_handled_without_you_pct(self) -> None:
        now = util.now()
        with principals.acting(principals.owner() or "hand"):
            t1 = task_store.task_add("demo", "Fix auth", title="Fix auth")
            t2 = task_store.task_add("demo", "Clean docs", title="Clean docs")
            t1_num = int(task_store.task_get(t1)["num"])

        with self._live_api() as (port, token, _):
            st_sh, _, shell0 = self._http(port, "GET", "/api/v1/screens/shell", token)
            self.assertEqual(st_sh, 200)
            self.assertIsNone(shell0["handledWithoutYouPct"])

            st_fl, _, floor0 = self._http(port, "GET", "/api/v1/screens/floor", token)
            self.assertEqual(st_fl, 200)
            self.assertIsNone(floor0["handledWithoutYouPct"])

            with foundation.tx() as c:
                for i in range(4):
                    rid = f"run-today-{i}"
                    self._insert_run(
                        c, rid, status="done",
                        started=util.iso(now - timedelta(minutes=30)),
                        finished=util.iso(now - timedelta(minutes=5)),
                    )
                c.execute(
                    "INSERT INTO inbox (kind, subject, run_id, priority, title, state, answered_by, answered_at, raised, version) "
                    "VALUES ('choice', 'run:run-today-0', 'run-today-0', 2, 'pick', 'resolved', 'owner', ?, ?, 2)",
                    (util.iso(now - timedelta(minutes=10)), util.iso(now - timedelta(minutes=20))),
                )
                c.execute(
                    "INSERT INTO inbox (kind, subject, run_id, priority, title, state, answered_by, resolution, raised, version) "
                    "VALUES ('fault', 'step:run-today-1/checks', 'run-today-1', 2, 'auto', 'resolved', '', 'passed', ?, 2)",
                    (util.iso(now - timedelta(minutes=20)),),
                )
                self._insert_run(
                    c, "run-live-1", status="running",
                    started=util.iso(now - timedelta(minutes=5)),
                    task_num=t1_num, issue=t1,
                )
                c.execute(
                    "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                    "VALUES ('run-live-1', 'plan', 'plan', 1, 'done', 1, ?, ?, 1)",
                    (util.iso(now - timedelta(minutes=5)), util.iso(now - timedelta(minutes=4))),
                )
                c.execute(
                    "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                    "VALUES ('run-live-1', 'verify', 'verify', 1, 'fault', 0, ?, ?, 1)",
                    (util.iso(now - timedelta(minutes=3)), util.iso(now - timedelta(minutes=2))),
                )
                item, _, _ = inbox_store.raise_item(
                    c,
                    kind="stop",
                    subject="run:run-live-1",
                    priority=1,
                    title="halted at verify",
                    detail={"run_id": "run-live-1", "node": "verify"},
                    task_id=t1,
                    run_id="run-live-1",
                    line="demo-pipeline",
                )

            _, _, shell1 = self._http(port, "GET", "/api/v1/screens/shell", token)
            self.assertEqual(shell1["handledWithoutYouPct"], 75)
            self.assertEqual(shell1["zone"], shell1["andon"]["zone"])

            _, _, floor1 = self._http(port, "GET", "/api/v1/screens/floor", token)
            self.assertEqual(floor1["handledWithoutYouPct"], 75)

            st_b, _, brd = self._http(port, "GET", "/api/v1/screens/board", token)
            self.assertEqual(st_b, 200)
            by_id = {t["id"]: t for t in brd["tasks"]}
            self.assertEqual(by_id[t1]["needs_you"], 1)
            self.assertEqual(by_id[t1]["needs_you_ids"], [item["id"]])
            self.assertEqual(by_id[t2]["needs_you"], 0)
            self.assertEqual(by_id[t2]["needs_you_ids"], [])
            self.assertEqual(brd["andon"]["high"], 1)

            st_steps, _, steps_resp = self._http(port, "GET", "/api/v1/runs/run-live-1/steps", token)
            self.assertEqual(st_steps, 200)
            by_node = {s["node"]: s for s in steps_resp["steps"]}
            self.assertEqual(by_node["verify"]["needs_you"], 1)
            self.assertEqual(by_node["plan"]["needs_you"], 0)

            st_run, _, run_resp = self._http(port, "GET", "/api/v1/runs/run-live-1", token)
            self.assertEqual(st_run, 200)
            sum_by_node = {s["node"]: s for s in run_resp["steps"]}
            self.assertEqual(sum_by_node["verify"]["needs_you"], 1)
            self.assertEqual(sum_by_node["plan"]["needs_you"], 0)


class NormalTimeBandAndEtaTests(_FloorTestBase):
    def _seed_done_tries(self, line_id: str, node: str, durations: list[float], *, prefix: str = "seed") -> str:
        base_time = util.now() - timedelta(hours=3)
        last_rid = ""
        with foundation.tx() as c:
            for idx, sec in enumerate(durations):
                rid = f"{prefix}-{node}-{idx}"
                last_rid = rid
                stamp = util.iso(base_time + timedelta(seconds=idx * 10))
                self._insert_run(c, rid, status="done", started=stamp, finished=stamp, line=line_id)
                c.execute(
                    "INSERT INTO steps (run_id, step, node, try, status, ok, seconds, queued_at, ts, version) "
                    "VALUES (?, ?, ?, 1, 'done', 1, ?, ?, ?, 1)",
                    (rid, node, node, sec, stamp, stamp),
                )
        return last_rid

    def test_band_percentiles_doctor_audit_recompute_idempotency_and_cli(self) -> None:
        line_id = "demo-pipeline"
        # 2 very old outliers (9999s) followed by 30 tries [10, 20, ..., 300]
        durations = [9999.0, 9999.0, *[float((i + 1) * 10) for i in range(30)]]
        self._seed_done_tries(line_id, "implement", durations)
        # 3 historical done tries + 1 done try on run-check-band = 4 done tries total (< 5)
        self._seed_done_tries(line_id, "plan", [20.0, 30.0, 40.0])

        with foundation.tx() as c:
            now_s = util.iso()
            self._insert_run(c, "run-check-band", status="running", started=now_s, line=line_id)
            c.execute(
                "INSERT INTO steps (run_id, step, node, try, status, ok, seconds, queued_at, ts, version) "
                "VALUES ('run-check-band', 'plan', 'plan', 1, 'done', 1, 25, ?, ?, 1)",
                (now_s, now_s),
            )
            c.execute(
                "INSERT INTO steps (run_id, step, node, try, status, ok, seconds, queued_at, ts, version) "
                "VALUES ('run-check-band', 'implement', 'implement', 1, 'running', 0, 0, ?, ?, 1)",
                (now_s, now_s),
            )

        # Doctor detects drift read-only without mutating live bytes, versions, or events.
        before_bytes = paths.DB_FILE.read_bytes()
        with foundation.reader() as c:
            events_before = c.execute("SELECT COUNT(*) FROM events_log").fetchone()[0]
            ver_before = projects_store.get_line(c, line_id)["version"]
        doc_before = doctor.run([f"derived:band:{line_id}"])
        self.assertEqual(len(doc_before), 1)
        self.assertTrue(doc_before[0].failing)
        self.assertEqual(doc_before[0].count, 1)
        self.assertEqual(paths.DB_FILE.read_bytes(), before_bytes)
        with foundation.reader() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events_log").fetchone()[0], events_before)
            self.assertEqual(projects_store.get_line(c, line_id)["version"], ver_before)

        expected_lo = bands.percentile([float((i + 1) * 10) for i in range(30)], 0.10)
        expected_hi = bands.percentile([float((i + 1) * 10) for i in range(30)], 0.90)

        with self._live_api() as (port, token, _):
            st1, hdr1, res1 = self._http(
                port, "POST", f"/api/v1/derived/band/{line_id}/recompute", token,
                {"base_version": ver_before},
                idem="band-recomp-1",
            )
            self.assertEqual(st1, 200)
            self.assertTrue(res1["changed"])
            self.assertEqual(res1["ver"], ver_before + 1)
            self.assertIsNotNone(res1["event_seq"])
            self.assertEqual(hdr1.get("arete-seq"), res1["event_seq"])
            self.assertEqual(res1["data"]["band"], {"implement": [expected_lo, expected_hi]})

            # Idempotent replay returns committed receipt without error or extra event.
            st_rep, hdr_rep, res_rep = self._http(
                port, "POST", f"/api/v1/derived/band/{line_id}/recompute", token,
                {"base_version": ver_before},
                idem="band-recomp-1",
            )
            self.assertEqual(st_rep, 200)
            self.assertEqual(hdr_rep.get("idempotent-replayed"), "true")
            self.assertEqual(res_rep["event_seq"], res1["event_seq"])

            # Second recompute with new key & current version is a no-op (no version bump, no line.changed).
            st2, _, res2 = self._http(
                port, "POST", f"/api/v1/derived/band/{line_id}/recompute", token,
                {"base_version": res1["ver"]},
                idem="band-recomp-2",
            )
            self.assertEqual(st2, 200)
            self.assertFalse(res2["changed"])
            self.assertEqual(res2["ver"], res1["ver"])
            self.assertIsNone(res2["event_seq"])

            with foundation.reader() as c:
                changed_events = c.execute(
                    "SELECT COUNT(*) FROM events_log WHERE kind = 'line.changed' AND subject = ?",
                    (f"line:{line_id}",),
                ).fetchone()[0]
            # 1 initial create event + 1 band update event = 2 total
            self.assertEqual(changed_events, 2)

            # Steps list (both /runs/{id}/steps and /steps?run={id}) includes band for implement and omits plan (4 tries).
            st_rs, _, steps_resp = self._http(port, "GET", "/api/v1/runs/run-check-band/steps", token)
            self.assertEqual(st_rs, 200)
            by_node = {s["node"]: s for s in steps_resp["steps"]}
            self.assertEqual(by_node["implement"]["band"], [expected_lo, expected_hi])
            self.assertNotIn("band", by_node["plan"])
            st_sl, _, sl_resp = self._http(port, "GET", "/api/v1/steps?run=run-check-band", token)
            self.assertEqual(st_sl, 200)
            self.assertEqual(sl_resp["steps"], steps_resp["steps"])

            # Floor carries same band as lines.band.
            _, _, fl = self._http(port, "GET", "/api/v1/screens/floor", token)
            line_entry = next(entry for entry in fl["lines"] if entry["id"] == line_id)
            self.assertEqual(line_entry["band"], {"implement": [expected_lo, expected_hi]})

            # Doctor passes now, and nightly audit includes band:<line>.
            doc_ok = doctor.run([f"derived:band:{line_id}"])
            self.assertFalse(doc_ok[0].failing)
            self.assertEqual(doc_ok[0].count, 0)

            with (
                mock.patch.object(auditor, "check_skills", return_value=([], set())),
                mock.patch.object(auditor, "check_hooks", return_value=[]),
                mock.patch.object(auditor, "check_repos", return_value=[]),
                mock.patch.object(auditor, "check_events", return_value=[]),
                mock.patch.object(auditor, "heartbeat_failing", return_value=set()),
            ):
                _, _, audit_results = auditor.audit(config.load(), record_derived=True)
            band_audit = next(r for r in audit_results if r.name == f"derived:band:{line_id}")
            self.assertFalse(band_audit.failing)
            with foundation.reader() as c:
                drow = derived_store.get(f"band:{line_id}", c)
                self.assertIsNotNone(drow)
                self.assertEqual(drow["diff_rows"], 0)
                self.assertIsNotNone(drow["checked"])

            # Add 5th try for plan and slow try for implement -> doctor drifts without mutating DB.
            with foundation.tx() as c:
                later = util.iso(util.now() - timedelta(minutes=1))
                for rid, node, sec in (("extra-plan-5", "plan", 60.0), ("extra-impl-slow", "implement", 1200.0)):
                    self._insert_run(c, rid, status="done", started=later, finished=later, line=line_id)
                    c.execute(
                        "INSERT INTO steps (run_id, step, node, try, status, ok, seconds, queued_at, ts, version) "
                        "VALUES (?, ?, ?, 1, 'done', 1, ?, ?, ?, 1)",
                        (rid, node, node, sec, later, later),
                    )

            drift_bytes = paths.DB_FILE.read_bytes()
            doc_drift = doctor.run([f"derived:band:{line_id}"])
            self.assertTrue(doc_drift[0].failing)
            self.assertEqual(paths.DB_FILE.read_bytes(), drift_bytes)

            # Use CLI `arete derived recompute band:<line>` against the live loopback server.
            out, err = StringIO(), StringIO()
            with (
                mock.patch.dict(os.environ, {"ARETE_SERVER": f"http://127.0.0.1:{port}"}),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                rc = cli.main([
                    "derived", "recompute", f"band:{line_id}",
                    "--base-version", str(res1["ver"]),
                    "--idempotency-key", "cli-band-recomp-3",
                    "--json",
                ])
            self.assertEqual(rc, 0, msg=out.getvalue() + err.getvalue())
            cli_payload = json.loads(out.getvalue())
            self.assertTrue(cli_payload["changed"])
            self.assertIn("plan", cli_payload["data"]["band"])
            self.assertGreater(cli_payload["data"]["band"]["implement"][1], expected_hi)

            doc_fixed = doctor.run([f"derived:band:{line_id}"])
            self.assertFalse(doc_fixed[0].failing)

    def test_recompute_refusals_and_coherent_source_revalidation(self) -> None:
        line_id = "demo-pipeline"
        self._seed_done_tries(line_id, "plan", [10.0, 20.0, 30.0, 40.0, 50.0])

        # Unsupported provider refused by CLI before any HTTP call.
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc_bad = cli.main(["derived", "recompute", "cost", "--base-version", "1"])
        self.assertEqual(rc_bad, 1)
        self.assertIn("unsupported derived provider", err.getvalue())

        with self._live_api() as (port, full_token, read_token):
            # Unauthorized (read-only token lacking task:write) refused before mutation.
            st_unauth, _, body_unauth = self._http(
                port, "POST", f"/api/v1/derived/band/{line_id}/recompute", read_token,
                {"base_version": 1},
                idem="unauth-key-1",
            )
            self.assertEqual(st_unauth, 403)
            self.assertEqual(body_unauth["code"], "scope_missing")

            # Unknown field refused strictly.
            st_unk, _, body_unk = self._http(
                port, "POST", f"/api/v1/derived/band/{line_id}/recompute", full_token,
                {"base_version": 1, "extra": True},
                idem="unknown-key-1",
            )
            self.assertEqual(st_unk, 422)
            self.assertEqual(body_unk["code"], "unknown_field")

            # Stale version refused before mutation.
            st_stale, _, body_stale = self._http(
                port, "POST", f"/api/v1/derived/band/{line_id}/recompute", full_token,
                {"base_version": 99},
                idem="stale-key-1",
            )
            self.assertEqual(st_stale, 409)
            self.assertEqual(body_stale["code"], "version_conflict")

            # Config gate refusal when config.toml is invalid.
            good_cfg = paths.CONFIG_FILE.read_bytes()
            paths.CONFIG_FILE.write_text("not valid = [toml\n", encoding="utf-8")
            try:
                st_gate, _, body_gate = self._http(
                    port, "POST", f"/api/v1/derived/band/{line_id}/recompute", full_token,
                    {"base_version": 1},
                    idem="gate-key-1",
                )
                self.assertEqual(st_gate, 403)
                self.assertEqual(body_gate["code"], "gate_refused")
            finally:
                paths.CONFIG_FILE.write_bytes(good_cfg)

        # Moved source between read and fenced write refuses if max_retries=0 and retries cleanly otherwise.
        inserted = [0]

        def inject_try() -> None:
            inserted[0] += 1
            if inserted[0] == 1:
                with foundation.tx() as c:
                    ts = util.iso(util.now())
                    self._insert_run(c, "race-run-1", status="done", started=ts, finished=ts, line=line_id)
                    c.execute(
                        "INSERT INTO steps (run_id, step, node, try, status, ok, seconds, queued_at, ts, version) "
                        "VALUES ('race-run-1', 'plan', 'plan', 1, 'done', 1, 500.0, ?, ?, 1)",
                        (ts, ts),
                    )

        with principals.acting(principals.owner() or "hand"):
            with self.assertRaises(bands.SourceMoved):
                bands.recompute(line_id, 1, max_retries=0, before_write=inject_try)
            # Band was NOT updated with stale data.
            self.assertEqual(lines.get_line(line_id)["band"], {})

            # With retry enabled, recompute catches the moved source and publishes the fresh band.
            inserted[0] = 0

            def inject_once() -> None:
                inserted[0] += 1
                if inserted[0] == 1:
                    with foundation.tx() as c:
                        ts = util.iso(util.now())
                        self._insert_run(c, "race-run-2", status="done", started=ts, finished=ts, line=line_id)
                        c.execute(
                            "INSERT INTO steps (run_id, step, node, try, status, ok, seconds, queued_at, ts, version) "
                            "VALUES ('race-run-2', 'plan', 'plan', 1, 'done', 1, 600.0, ?, ?, 1)",
                            (ts, ts),
                        )

            result = bands.recompute(line_id, 1, max_retries=2, before_write=inject_once)
            self.assertTrue(result["changed"])
            self.assertGreaterEqual(result["data"]["band"]["plan"][1], 500)
            self.assertFalse(doctor.run([f"derived:band:{line_id}"])[0].failing)

    def test_run_eta_and_floor_live_eta(self) -> None:
        line_id = "demo-pipeline"
        with principals.acting(principals.owner() or "hand"):
            lines.update_line(line_id, 1, {"band": {"implement": [60, 180], "checks": [40, 80]}})

        now = util.now().replace(microsecond=0)
        with foundation.tx() as c:
            self._insert_run(
                c, "run-eta-partial", status="running",
                started=util.iso(now - timedelta(seconds=60)), line=line_id,
            )
            c.execute(
                "INSERT INTO steps (run_id, step, node, try, status, ok, seconds, queued_at, ts, version) "
                "VALUES ('run-eta-partial', 'implement', 'implement', 1, 'running', 0, 0, ?, ?, 1)",
                (util.iso(now - timedelta(seconds=30)), util.iso(now - timedelta(seconds=30))),
            )
            c.execute(
                "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                "VALUES ('run-eta-partial', 'checks', 'checks', 1, 'queued', 0, ?, ?, 1)",
                (util.iso(now), util.iso(now)),
            )
            c.execute(
                "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                "VALUES ('run-eta-partial', 'verify', 'verify', 1, 'queued', 0, ?, ?, 1)",
                (util.iso(now), util.iso(now)),
            )
            self._insert_run(
                c, "run-eta-done", status="done",
                started=util.iso(now - timedelta(minutes=10)),
                finished=util.iso(now - timedelta(minutes=5)),
                line=line_id,
            )

        # Pure deterministic ETA check: 150s remaining on implement + 60s mean on checks = 210s, partial True.
        with foundation.reader() as c:
            steps_rows = [dict(r) for r in c.execute("SELECT * FROM steps WHERE run_id = 'run-eta-partial' ORDER BY id")]
            eta, partial = bands.run_eta(steps_rows, {"implement": [60, 180], "checks": [40, 80]}, now=now)
            self.assertEqual(eta, util.iso(now + timedelta(seconds=210)))
            self.assertTrue(partial)

            # Capped at band end: elapsed 250s > 180s -> 0s remaining + 60s mean on checks = 60s.
            capped_steps = [
                {"node": "implement", "status": "running", "seconds": 250},
                {"node": "checks", "status": "queued"},
            ]
            eta_cap, partial_cap = bands.run_eta(capped_steps, {"implement": [60, 180], "checks": [40, 80]}, now=now)
            self.assertEqual(eta_cap, util.iso(now + timedelta(seconds=60)))
            self.assertFalse(partial_cap)

        with self._live_api() as (port, token, _):
            st_r, _, run_resp = self._http(port, "GET", "/api/v1/runs/run-eta-partial", token)
            self.assertEqual(st_r, 200)
            self.assertIsNotNone(run_resp["eta"])
            self.assertTrue(run_resp["eta_partial"])

            st_d, _, done_resp = self._http(port, "GET", "/api/v1/runs/run-eta-done", token)
            self.assertEqual(st_d, 200)
            self.assertIsNone(done_resp["eta"])
            self.assertFalse(done_resp["eta_partial"])

            st_f, _, fl = self._http(port, "GET", "/api/v1/screens/floor", token)
            self.assertEqual(st_f, 200)
            live_entry = next(row for row in fl["live"] if row["id"] == "run-eta-partial")
            self.assertIsNotNone(live_entry["eta"])
            self.assertTrue(live_entry["eta_partial"])


class FloorStationsTests(_FloorTestBase):
    def test_stations_order_states_precedence_wip_paused_custom_adw_and_loops(self) -> None:
        now = util.now()
        with principals.acting(principals.owner() or "hand"):
            lines.create_line({
                "id": "demo-idle",
                "project": "demo",
                "name": "Demo Idle",
                "kind": "pipeline",
                "repo": "demo",
            })
            lines.create_line({
                "id": "demo-patch",
                "project": "demo",
                "name": "Demo Patch",
                "kind": "pipeline",
                "repo": "demo",
                "adw_route": "patch",
            })
            lines.create_line({
                "id": "demo-paused",
                "project": "demo",
                "name": "Demo Paused",
                "kind": "pipeline",
                "repo": "demo",
                "paused": True,
                "paused_why": "maintenance",
            })
            lines.create_line({
                "id": "demo-loops",
                "project": "demo",
                "name": "Demo Loops",
                "kind": "loops",
                "repo": "demo",
            })

        feature_names = floor.adw_station_names("feature", config.load())
        patch_names = floor.adw_station_names("patch", config.load())
        self.assertEqual(feature_names[:5], ["prepare", "plan", "implement", "checks", "verify"])
        self.assertEqual(patch_names[:4], ["prepare", "patch", "checks", "verify"])
        self.assertNotIn("plan", patch_names)

        with foundation.tx() as c:
            ts = util.iso(now - timedelta(minutes=2))
            # Running run at 4th step (checks) on demo-pipeline, plus an unknown legacy node.
            self._insert_run(c, "run-st-1", status="running", started=ts, line="demo-pipeline")
            for node, status in (("prepare", "done"), ("plan", "done"), ("implement", "done"), ("checks", "running"), ("old_legacy_node", "running")):
                c.execute(
                    "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                    "VALUES ('run-st-1', ?, ?, 1, ?, ?, ?, ?, 1)",
                    (node, node, status, 1 if status == "done" else 0, ts, ts),
                )
            # Even with a running run on demo-paused, paused line stays idle.
            self._insert_run(c, "run-paused-1", status="running", started=ts, line="demo-paused")
            c.execute(
                "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                "VALUES ('run-paused-1', 'plan', 'plan', 1, 'running', 0, ?, ?, 1)",
                (ts, ts),
            )

        with self._live_api() as (port, token, _):
            st, _, fl = self._http(port, "GET", "/api/v1/screens/floor", token)
            self.assertEqual(st, 200)
            by_line = {entry["id"]: entry for entry in fl["lines"]}

            # 1. Single running run at 4th step: done, done, done, running (wip 1), queued...
            main_stations = by_line["demo-pipeline"]["stations"]
            self.assertEqual([s["name"] for s in main_stations], feature_names)
            self.assertNotIn("old_legacy_node", [s["name"] for s in main_stations])
            self.assertEqual([s["state"] for s in main_stations[:4]], ["done", "done", "done", "running"])
            self.assertEqual(main_stations[3]["wip"], 1)
            self.assertTrue(all(s["state"] == "queued" and s["wip"] == 0 for s in main_stations[4:]))

            # 2. Idle line: every station idle with wip 0.
            idle_stations = by_line["demo-idle"]["stations"]
            self.assertEqual([s["name"] for s in idle_stations], feature_names)
            self.assertTrue(all(s["state"] == "idle" and s["wip"] == 0 for s in idle_stations))

            # 3. Paused line: lists ADW stations, all idle with wip 0.
            paused_stations = by_line["demo-paused"]["stations"]
            self.assertEqual([s["name"] for s in paused_stations], feature_names)
            self.assertTrue(all(s["state"] == "idle" and s["wip"] == 0 for s in paused_stations))

            # 4. Custom adw_route = patch lists patch ADW steps, not feature's.
            patch_stations = by_line["demo-patch"]["stations"]
            self.assertEqual([s["name"] for s in patch_stations], patch_names)

            # 5. Loops line returns [].
            self.assertEqual(by_line["demo-loops"]["stations"], [])

            # 6. Second run at same node gives wip 2; fault overrides running; waiting secret shows waiting.
            with foundation.tx() as c:
                self._insert_run(
                    c, "run-st-2", status="running",
                    started=util.iso(now - timedelta(minutes=1)), line="demo-pipeline",
                )
                c.execute(
                    "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                    "VALUES ('run-st-2', 'checks', 'checks', 1, 'running', 0, ?, ?, 1)",
                    (ts, ts),
                )
                self._insert_run(c, "run-wait-secret", status="running", started=ts, line="demo-idle")
                c.execute(
                    "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                    "VALUES ('run-wait-secret', 'prepare', 'prepare', 1, 'done', 1, ?, ?, 1)",
                    (ts, ts),
                )
                inbox_store.raise_item(
                    c,
                    kind="continue",
                    subject="secret:global/ANTHROPIC_API_KEY",
                    priority=1,
                    title="Missing key",
                    detail={"node": "plan", "waiting": ["run-wait-secret"]},
                    run_id="run-wait-secret",
                    line="demo-idle",
                )

            _, _, fl2 = self._http(port, "GET", "/api/v1/screens/floor", token)
            by_line2 = {entry["id"]: entry for entry in fl2["lines"]}
            checks_st = next(s for s in by_line2["demo-pipeline"]["stations"] if s["name"] == "checks")
            self.assertEqual(checks_st["state"], "running")
            self.assertEqual(checks_st["wip"], 2)

            plan_wait = next(s for s in by_line2["demo-idle"]["stations"] if s["name"] == "plan")
            self.assertEqual(plan_wait["state"], "waiting")

            # Add fault step at checks on a third run -> fault wins over running.
            with foundation.tx() as c:
                self._insert_run(c, "run-st-fault", status="running", started=util.iso(now), line="demo-pipeline")
                c.execute(
                    "INSERT INTO steps (run_id, step, node, try, status, ok, queued_at, ts, version) "
                    "VALUES ('run-st-fault', 'checks', 'checks', 1, 'fault', 0, ?, ?, 1)",
                    (ts, ts),
                )
            _, _, fl3 = self._http(port, "GET", "/api/v1/screens/floor", token)
            by_line3 = {entry["id"]: entry for entry in fl3["lines"]}
            checks_fault = next(s for s in by_line3["demo-pipeline"]["stations"] if s["name"] == "checks")
            self.assertEqual(checks_fault["state"], "fault")
            self.assertEqual(checks_fault["wip"], 2)

        # Compare outline directly with adw.load('feature').
        loaded_feature = adw.load(config.load(), "feature")
        self.assertTrue(len(adw.outline(loaded_feature)) >= len(feature_names))


if __name__ == "__main__":
    unittest.main()
