from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arete import api_check, stats, util
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_schema_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE runs ("
        "id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, "
        "started TEXT NOT NULL, finished TEXT, adw TEXT, adw_version TEXT, stop_version INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    conn.execute(
        "CREATE TABLE steps ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step TEXT, node TEXT, status TEXT, "
        "ok INTEGER, seconds REAL, ts TEXT, prompt_version TEXT, version INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    conn.commit()
    return conn


def _run_isolated(script: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="arete-stats-test-") as tmp:
        root = Path(tmp).resolve()
        env = api_check.isolated_env(root)
        cmd = api_check.child_command(REPO_ROOT, script)
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHILD_HANG_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"child failed ({proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("STATS_RESULT="):
                return json.loads(line.removeprefix("STATS_RESULT="))
        raise AssertionError(f"missing STATS_RESULT\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


class StatsQueryTests(unittest.TestCase):
    def test_window_bounds_snaps_to_utc_midnight_seven_whole_days(self) -> None:
        early = datetime(2026, 11, 28, 0, 0, 1, tzinfo=UTC)
        late = datetime(2026, 11, 28, 23, 59, 59, tzinfo=UTC)
        next_day = datetime(2026, 11, 29, 0, 0, 0, tzinfo=UTC)
        self.assertEqual(stats.window_bounds(early), ("2026-11-22T00:00:00Z", "2026-11-29T00:00:00Z"))
        self.assertEqual(stats.window_bounds(late), ("2026-11-22T00:00:00Z", "2026-11-29T00:00:00Z"))
        self.assertEqual(stats.window_bounds("2026-11-28T14:22:00Z"), ("2026-11-22T00:00:00Z", "2026-11-29T00:00:00Z"))
        self.assertEqual(stats.window_bounds(next_day), ("2026-11-23T00:00:00Z", "2026-11-30T00:00:00Z"))

    def test_prompt_and_adw_stats_queries_and_readonly(self) -> None:
        now = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
        in_day1 = "2026-11-22T00:00:00Z"
        just_before = "2026-11-21T23:59:59Z"
        eight_days_ago = util.iso(now - timedelta(days=8))
        today_start = "2026-11-28T10:00:00Z"

        with tempfile.TemporaryDirectory(prefix="arete-stats-unit-") as tmp:
            db_path = Path(tmp) / "stats.db"
            conn = _make_schema_db(db_path)
            try:
                conn.executemany(
                    "INSERT INTO runs (id, kind, status, started, finished, adw) VALUES (?, 'pipeline', ?, ?, ?, ?)",
                    [
                        ("r1", "done", today_start, "2026-11-28T10:01:00Z", "feature"),
                        ("r2", "pr", "2026-11-27T10:00:00Z", "2026-11-27T10:03:00Z", "feature"),
                        ("r3", "failed", in_day1, "2026-11-22T00:05:00Z", "feature"),
                        ("r4", "running", "2026-11-28T11:00:00Z", None, "feature"),
                        ("r_old_boundary", "done", just_before, "2026-11-22T00:00:30Z", "feature"),
                        ("r_8d", "done", eight_days_ago, util.iso(now - timedelta(days=8) + timedelta(seconds=50)), "feature"),
                        ("r_null", "done", today_start, "2026-11-28T10:02:00Z", None),
                        ("r_noop", "noop", today_start, "2026-11-28T10:00:40Z", "patch"),
                    ],
                )
                conn.executemany(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("r1", "plan", "plan", "done", 1, 10.0, today_start, "prompt/plan@111111111111"),
                        ("r1", "build", "build", "fault", 0, 20.0, today_start, "prompt/build@222222222222"),
                        ("r1", "build", "build", "done", 1, 40.0, today_start, "prompt/build@222222222222"),
                        ("r2", "plan", "plan", "done", 1, 30.0, "2026-11-27T10:01:00Z", "prompt/plan@333333333333"),
                        ("r2", "build", "build", "done", 1, 50.0, "2026-11-27T10:02:00Z", "prompt/build@222222222222"),
                        ("r3", "plan", "plan", "fault", 0, 25.0, in_day1, "prompt/plan@111111111111"),
                        ("r4", "plan", "plan", "running", 0, None, "2026-11-28T11:00:00Z", "prompt/plan@111111111111"),
                        ("r_old_boundary", "plan", "plan", "done", 1, 5.0, just_before, "prompt/plan@111111111111"),
                        ("r_8d", "plan", "plan", "done", 1, 5.0, eight_days_ago, "prompt/plan@111111111111"),
                        ("r_null", "plan", "plan", "done", 1, 12.0, today_start, "prompt/plan_v2@444444444444"),
                        ("r_null", "plan", "plan", "done", 1, 12.0, today_start, "prompt/plan.append@555555555555"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                plan = stats.prompt_stats(ro, "plan", now)
                self.assertEqual(
                    plan,
                    {"runs7d": 4, "passRate": 67, "costPerRunNusd": None, "costPerRun": None},
                )
                unused = stats.prompt_stats(ro, "unused", now)
                self.assertEqual(
                    unused,
                    {"runs7d": 0, "passRate": None, "costPerRunNusd": None, "costPerRun": None},
                )
                feat = stats.adw_stats(ro, "feature", now)
                self.assertEqual(
                    feat,
                    {
                        "runs": 4,
                        "passRate": 67,
                        "medianSeconds": 180,
                        "costPerRunNusd": None,
                        "costPerRun": None,
                        "nodes": {
                            "build": {"runs": 2, "passRate": 67, "medianSeconds": 40},
                            "plan": {"runs": 4, "passRate": 67, "medianSeconds": 25},
                        },
                    },
                )
                patch_stats = stats.adw_stats(ro, "patch", now)
                self.assertEqual(patch_stats["runs"], 1)
                self.assertEqual(patch_stats["passRate"], 100)
                self.assertEqual(patch_stats["medianSeconds"], 40)
                self.assertEqual(patch_stats["nodes"], {})
                self.assertEqual(stats.adw_stats(ro, "", now)["runs"], 0)
            finally:
                ro.close()

    def test_stored_money_integer_means_rounding_and_unknown_evidence(self) -> None:
        now = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
        today_start = "2026-11-28T10:00:00Z"
        eight_days_ago = util.iso(now - timedelta(days=8))

        vectors = [
            ("v_1_2", [1, 2], 2),
            ("v_0_1", [0, 1], 0),
            ("v_2_3", [2, 3], 2),
            ("v_neg_1_2", [-1, -2], -2),
            ("v_neg_2_3", [-2, -3], -2),
            ("v_0_neg1", [0, -1], 0),
            ("v_neg1_1", [-1, 1], 0),
            ("v_zero", [0, 0], 0),
            ("v_big_pos", [10**18 + 1, 10**18 + 2], 10**18 + 2),
            ("v_big_neg", [-(10**18 + 1), -(10**18 + 2)], -(10**18 + 2)),
        ]

        with tempfile.TemporaryDirectory(prefix="arete-stats-money-") as tmp:
            db_path = Path(tmp) / "stats_money.db"
            conn = _make_schema_db(db_path)
            try:
                conn.execute("ALTER TABLE steps ADD COLUMN cost_nusd INTEGER")
                for label, run_costs, _expected in vectors:
                    for idx, amount in enumerate(run_costs):
                        rid = f"{label}_r{idx}"
                        conn.execute(
                            "INSERT INTO runs (id, kind, status, started, finished, adw) "
                            "VALUES (?, 'pipeline', 'done', ?, ?, ?)",
                            (rid, today_start, "2026-11-28T10:01:00Z", label),
                        )
                        conn.execute(
                            "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version, cost_nusd) "
                            "VALUES (?, 'plan', 'plan', 'done', 1, 10.0, ?, ?, ?)",
                            (rid, today_start, f"prompt/{label}@111111111111", amount),
                        )

                # Run with NULL cost_nusd only -> unknown evidence (null/null)
                conn.execute(
                    "INSERT INTO runs (id, kind, status, started, finished, adw) "
                    "VALUES ('r_unknown', 'pipeline', 'done', ?, ?, 'unknown_only')",
                    (today_start, "2026-11-28T10:01:00Z"),
                )
                conn.execute(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version, cost_nusd) "
                    "VALUES ('r_unknown', 'plan', 'plan', 'done', 1, 10.0, ?, 'prompt/unknown_only@111111111111', NULL)",
                    (today_start,),
                )

                # Signed correction on an eligible run (correction has NULL prompt_version)
                conn.execute(
                    "INSERT INTO runs (id, kind, status, started, finished, adw) "
                    "VALUES ('r_corr1', 'pipeline', 'done', ?, ?, 'with_corr')",
                    (today_start, "2026-11-28T10:01:00Z"),
                )
                conn.execute(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version, cost_nusd) "
                    "VALUES ('r_corr1', 'plan', 'plan', 'done', 1, 10.0, ?, 'prompt/with_corr@111111111111', 10)",
                    (today_start,),
                )
                conn.execute(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version, cost_nusd) "
                    "VALUES ('r_corr1', 'correction', 'correction', 'done', 1, 0.0, ?, NULL, -7)",
                    (today_start,),
                )
                # Unknown run on same ADW/prompt must NOT coerce NULL to 0
                conn.execute(
                    "INSERT INTO runs (id, kind, status, started, finished, adw) "
                    "VALUES ('r_corr_null', 'pipeline', 'done', ?, ?, 'with_corr')",
                    (today_start, "2026-11-28T10:01:00Z"),
                )
                conn.execute(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version, cost_nusd) "
                    "VALUES ('r_corr_null', 'plan', 'plan', 'done', 1, 10.0, ?, 'prompt/with_corr@111111111111', NULL)",
                    (today_start,),
                )
                # 8-day-old run and NULL-ADW run must be excluded
                conn.execute(
                    "INSERT INTO runs (id, kind, status, started, finished, adw) "
                    "VALUES ('r_old', 'pipeline', 'done', ?, ?, 'v_1_2')",
                    (eight_days_ago, eight_days_ago),
                )
                conn.execute(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version, cost_nusd) "
                    "VALUES ('r_old', 'plan', 'plan', 'done', 1, 10.0, ?, 'prompt/v_1_2@111111111111', 999999)",
                    (eight_days_ago,),
                )
                conn.execute(
                    "INSERT INTO runs (id, kind, status, started, finished, adw) "
                    "VALUES ('r_null_adw', 'pipeline', 'done', ?, ?, NULL)",
                    (today_start, "2026-11-28T10:01:00Z"),
                )
                conn.execute(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version, cost_nusd) "
                    "VALUES ('r_null_adw', 'plan', 'plan', 'done', 1, 10.0, ?, NULL, 999999)",
                    (today_start,),
                )
                conn.commit()
                before_bytes = db_path.read_bytes()
            finally:
                conn.close()

            ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                for label, _run_costs, expected in vectors:
                    pstat = stats.prompt_stats(ro, label, now)
                    astat = stats.adw_stats(ro, label, now)
                    self.assertEqual(pstat["costPerRunNusd"], expected, label)
                    self.assertEqual(pstat["costPerRun"], expected / 1_000_000_000, label)
                    self.assertEqual(astat["costPerRunNusd"], expected, label)
                    self.assertEqual(astat["costPerRun"], expected / 1_000_000_000, label)

                self.assertEqual(stats.prompt_stats(ro, "unknown_only", now)["costPerRunNusd"], None)
                self.assertEqual(stats.prompt_stats(ro, "unknown_only", now)["costPerRun"], None)
                self.assertEqual(stats.adw_stats(ro, "unknown_only", now)["costPerRunNusd"], None)
                self.assertEqual(stats.adw_stats(ro, "unknown_only", now)["costPerRun"], None)
                self.assertEqual(stats.prompt_stats(ro, "never_ran", now)["costPerRunNusd"], None)
                self.assertEqual(stats.adw_stats(ro, "never_ran", now)["costPerRunNusd"], None)

                # Signed correction: 10 + (-7) = 3 across 1 monetary run (NULL run excluded from denominator)
                self.assertEqual(stats.prompt_stats(ro, "with_corr", now)["costPerRunNusd"], 3)
                self.assertEqual(stats.prompt_stats(ro, "with_corr", now)["costPerRun"], 3 / 1_000_000_000)
                self.assertEqual(stats.adw_stats(ro, "with_corr", now)["costPerRunNusd"], 3)
                self.assertEqual(stats.adw_stats(ro, "with_corr", now)["costPerRun"], 3 / 1_000_000_000)
            finally:
                ro.close()
            self.assertEqual(db_path.read_bytes(), before_bytes)


class StatsApiTests(unittest.TestCase):
    def test_routes_window_filtering_and_etag_invalidation(self) -> None:
        script = r"""
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import (
    api_check,
    api_http,
    codebase,
    configrepo,
    credential_files,
    dashboard,
    paths,
    stats,
    tailnet,
    util,
)
from arete.store import db, principals, tasks as task_store, tokens

root = paths.CONFIG_DIR.resolve()
lines, _ = api_check.prepare_replay(root)
paths.CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
db.migrate()
configrepo.init()
principals.seed_owner(api_check.LOGIN)
tokens.init_owner()
token = credential_files.read_client(db.schema_metadata()["instance_tag"])
codebase.load()

clock = {"now": datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)}

def fake_now():
    return clock["now"]

tid_label = task_store.task_add("replay-sample", "Seed task", title="Seed task")
tid_num = int(task_store.task_get(tid_label)["num"])

with db.tx() as conn:
    conn.executemany(
        "INSERT INTO runs (id, kind, status, started, finished, adw, task_id) VALUES (?, 'pipeline', ?, ?, ?, ?, ?)",
        [
            ("r1", "done", "2026-11-28T10:00:00Z", "2026-11-28T10:01:00Z", "feature", tid_num),
            ("r2", "done", "2026-11-27T10:00:00Z", "2026-11-27T10:02:00Z", "feature", tid_num),
            ("r3", "failed", "2026-11-22T08:00:00Z", "2026-11-22T08:03:00Z", "feature", tid_num),
        ],
    )
    conn.executemany(
        "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version, cost_usd, cost_nusd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
        [
            ("r1", "plan", "plan", "done", 1, 10.0, "2026-11-28T10:00:10Z", "prompt/plan@aaa111222333"),
            ("r1", "build", "build", "done", 1, 50.0, "2026-11-28T10:01:00Z", "prompt/build@bbb111222333"),
            ("r2", "plan", "plan", "done", 1, 20.0, "2026-11-27T10:00:20Z", "prompt/plan@aaa111222333"),
            ("r2", "build", "build", "done", 1, 100.0, "2026-11-27T10:02:00Z", "prompt/build@bbb111222333"),
            ("r3", "plan", "plan", "fault", 0, 30.0, "2026-11-22T08:00:30Z", "prompt/plan@aaa111222333"),
        ],
    )

node = SimpleNamespace(names={api_check.ADDRESS})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: api_check.LOGIN
handler = type("StatsHandler", (dashboard.handlers().open,), {"disable_nagle_algorithm": True})
with (
    mock.patch.object(api_http, "log_request"),
    mock.patch("socket.getfqdn", return_value=api_check.ADDRESS),
    mock.patch.object(util, "now", side_effect=fake_now),
):
    server = tailnet.Server(api_check.ADDRESS, 0, node, logins, False, handler, dashboard.Redirect)
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            api = api_check.Check(server.server_port, root, [], token)

            h_stats0, raw_stats0 = api.request("GET", "/api/v1/config/adw/feature/stats")
            etag_stats0 = h_stats0["ETag"]
            stats0 = json.loads(raw_stats0)
            assert stats0["runs"] == 3 and stats0["passRate"] == 67 and stats0["medianSeconds"] == 120
            assert stats0["costPerRunNusd"] is None and stats0["costPerRun"] is None
            assert stats0["nodes"] == {
                "build": {"runs": 2, "passRate": 100, "medianSeconds": 75},
                "plan": {"runs": 3, "passRate": 67, "medianSeconds": 20},
            }

            h_adw0, raw_adw0 = api.request("GET", "/api/v1/config/adw/feature")
            etag_adw0 = h_adw0["ETag"]
            adw0 = json.loads(raw_adw0)["item"]
            assert adw0["runs7d"] == 3 and adw0["passRate"] == 67
            assert adw0["costPerRunNusd"] is None and adw0["costPerRun"] is None

            h_plan0, raw_plan0 = api.request("GET", "/api/v1/config/prompt/plan")
            etag_plan0 = h_plan0["ETag"]
            plan0 = json.loads(raw_plan0)["item"]
            assert plan0["runs7d"] == 3 and plan0["passRate"] == 67
            assert plan0["costPerRunNusd"] is None and plan0["costPerRun"] is None

            review0 = api.read("/api/v1/config/prompt/review")["item"]
            assert review0["runs7d"] == 0 and review0["passRate"] is None
            assert review0["costPerRunNusd"] is None and review0["costPerRun"] is None

            api.request("GET", "/api/v1/config/adw/does-not-exist/stats", status=404)

            api.request("GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats0}, status=304)
            api.request("GET", "/api/v1/config/adw/feature", headers={"If-None-Match": etag_adw0}, status=304)
            api.request("GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan0}, status=304)

            with db.tx() as conn:
                conn.executemany(
                    "INSERT INTO runs (id, kind, status, started, finished, adw, task_id) VALUES (?, 'pipeline', ?, ?, ?, ?, ?)",
                    [
                        ("r_8d", "failed", "2026-11-20T10:00:00Z", "2026-11-20T10:05:00Z", "feature", tid_num),
                        ("r_null", "failed", "2026-11-28T09:00:00Z", "2026-11-28T09:05:00Z", None, tid_num),
                    ],
                )
                conn.execute(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version) "
                    "VALUES ('r_8d', 'plan', 'plan', 'fault', 0, 99.0, '2026-11-20T10:01:00Z', 'prompt/plan@aaa111222333')"
                )
            api.request("GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats0}, status=304)
            api.request("GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan0}, status=304)

            with db.tx() as conn:
                conn.execute(
                    "INSERT INTO runs (id, kind, status, started, finished, adw, task_id) "
                    "VALUES ('r4', 'pipeline', 'done', '2026-11-26T10:00:00Z', '2026-11-26T10:01:00Z', 'feature', ?)",
                    (tid_num,),
                )
                conn.execute(
                    "INSERT INTO steps (run_id, step, node, status, ok, seconds, ts, prompt_version) "
                    "VALUES ('r4', 'plan', 'plan', 'done', 1, 15.0, '2026-11-26T10:00:15Z', 'prompt/plan@aaa111222333')"
                )
            h_stats1, raw_stats1 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats0}, status=200
            )
            etag_stats1 = h_stats1["ETag"]
            stats1 = json.loads(raw_stats1)
            assert etag_stats1 != etag_stats0
            assert stats1["runs"] == 4 and stats1["passRate"] == 75
            h_plan1, raw_plan1 = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan0}, status=200
            )
            etag_plan1 = h_plan1["ETag"]
            plan1 = json.loads(raw_plan1)["item"]
            assert etag_plan1 != etag_plan0
            assert plan1["runs7d"] == 4 and plan1["passRate"] == 75

            with db.tx() as conn:
                conn.execute("UPDATE steps SET seconds = 11.0 WHERE run_id = 'r1' AND node = 'plan'")
            h_stats2, raw_stats2 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats1}, status=200
            )
            etag_stats2 = h_stats2["ETag"]
            assert etag_stats2 != etag_stats1
            assert json.loads(raw_stats2)["nodes"]["plan"]["medianSeconds"] == 17.5
            h_plan2, _ = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan1}, status=200
            )
            etag_plan2 = h_plan2["ETag"]
            assert etag_plan2 != etag_plan1

            with db.tx() as conn:
                conn.execute("UPDATE runs SET status = 'failed' WHERE id = 'r4'")
            h_stats3, raw_stats3 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats2}, status=200
            )
            etag_stats3 = h_stats3["ETag"]
            assert etag_stats3 != etag_stats2 and json.loads(raw_stats3)["passRate"] == 50
            h_plan3, _ = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan2}, status=200
            )
            etag_plan3 = h_plan3["ETag"]
            assert etag_plan3 != etag_plan2

            with db.tx() as conn:
                conn.execute("UPDATE runs SET finished = '2026-11-26T10:04:00Z' WHERE id = 'r4'")
            h_stats4, raw_stats4 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats3}, status=200
            )
            etag_stats4 = h_stats4["ETag"]
            assert etag_stats4 != etag_stats3 and json.loads(raw_stats4)["medianSeconds"] == 150

            with db.tx() as conn:
                conn.execute("UPDATE runs SET adw = NULL WHERE id = 'r4'")
                conn.execute("UPDATE steps SET prompt_version = 'prompt/other@ccc111222333' WHERE run_id = 'r4'")
            h_stats5, raw_stats5 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats4}, status=200
            )
            etag_stats5 = h_stats5["ETag"]
            assert etag_stats5 != etag_stats4 and json.loads(raw_stats5)["runs"] == 3
            h_plan5, raw_plan5 = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan3}, status=200
            )
            etag_plan5 = h_plan5["ETag"]
            assert etag_plan5 != etag_plan3 and json.loads(raw_plan5)["item"]["runs7d"] == 3

            with db.tx() as conn:
                conn.execute("DELETE FROM steps WHERE run_id = 'r2' AND node = 'build'")
            h_stats6, raw_stats6 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats5}, status=200
            )
            etag_stats6 = h_stats6["ETag"]
            assert etag_stats6 != etag_stats5 and json.loads(raw_stats6)["nodes"]["build"]["runs"] == 1

            adw_override = root / "adws" / "feature.toml"
            adw_override.parent.mkdir(parents=True, exist_ok=True)
            adw_override.write_text((paths.ENGINE_ADWS_DIR / "feature.toml").read_text(encoding="utf-8") + "\n", encoding="utf-8")
            h_stats7, _ = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats6}, status=200
            )
            etag_stats7 = h_stats7["ETag"]
            assert etag_stats7 != etag_stats6

            plan_override = root / "prompts" / "plan.md"
            plan_override.parent.mkdir(parents=True, exist_ok=True)
            plan_override.write_text((paths.PROMPTS_DIR / "plan.md").read_text(encoding="utf-8") + "\n", encoding="utf-8")
            h_plan7, _ = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan5}, status=200
            )
            etag_plan7 = h_plan7["ETag"]
            assert etag_plan7 != etag_plan5

            clock["now"] = datetime(2026, 11, 29, 0, 0, 1, tzinfo=UTC)
            h_stats8, raw_stats8 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats7}, status=200
            )
            etag_stats8 = h_stats8["ETag"]
            stats8 = json.loads(raw_stats8)
            assert etag_stats8 != etag_stats7 and stats8["runs"] == 2 and stats8["passRate"] == 100
            h_plan8, raw_plan8 = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan7}, status=200
            )
            etag_plan8 = h_plan8["ETag"]
            plan8 = json.loads(raw_plan8)["item"]
            assert etag_plan8 != etag_plan7 and plan8["runs7d"] == 2 and plan8["passRate"] == 100

            # Installing the stored-money producer invalidates an earlier null-cost representation
            # even when no step was added.
            with mock.patch.object(stats, "MONETARY_PRODUCER", None):
                h_stats_null, raw_stats_null = api.request("GET", "/api/v1/config/adw/feature/stats")
                etag_stats_null = h_stats_null["ETag"]
                assert json.loads(raw_stats_null)["costPerRunNusd"] is None
                h_plan_null, raw_plan_null = api.request("GET", "/api/v1/config/prompt/plan")
                etag_plan_null = h_plan_null["ETag"]
                assert json.loads(raw_plan_null)["item"]["costPerRunNusd"] is None

            h_stats9, raw_stats9 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats_null}, status=200
            )
            etag_stats9 = h_stats9["ETag"]
            assert etag_stats9 != etag_stats_null and json.loads(raw_stats9)["costPerRunNusd"] is None
            h_plan9, raw_plan9 = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan_null}, status=200
            )
            etag_plan9 = h_plan9["ETag"]
            assert etag_plan9 != etag_plan_null and json.loads(raw_plan9)["item"]["costPerRunNusd"] is None

            with db.tx() as conn:
                conn.execute("UPDATE steps SET cost_nusd = 1250000000 WHERE run_id = 'r1' AND node = 'plan'")
            h_stats10, raw_stats10 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats9}, status=200
            )
            etag_stats10 = h_stats10["ETag"]
            stats10 = json.loads(raw_stats10)
            assert etag_stats10 != etag_stats9
            assert stats10["costPerRunNusd"] == 1_250_000_000 and stats10["costPerRun"] == 1.25
            h_plan10, raw_plan10 = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan9}, status=200
            )
            etag_plan10 = h_plan10["ETag"]
            plan10 = json.loads(raw_plan10)["item"]
            assert etag_plan10 != etag_plan9
            assert plan10["costPerRunNusd"] == 1_250_000_000 and plan10["costPerRun"] == 1.25

            with db.tx() as conn:
                conn.execute("UPDATE steps SET cost_nusd = 2000000000 WHERE run_id = 'r1' AND node = 'plan'")
            h_stats11, raw_stats11 = api.request(
                "GET", "/api/v1/config/adw/feature/stats", headers={"If-None-Match": etag_stats10}, status=200
            )
            assert h_stats11["ETag"] != etag_stats10
            assert json.loads(raw_stats11)["costPerRunNusd"] == 2_000_000_000
            h_plan11, raw_plan11 = api.request(
                "GET", "/api/v1/config/prompt/plan", headers={"If-None-Match": etag_plan10}, status=200
            )
            assert h_plan11["ETag"] != etag_plan10
            assert json.loads(raw_plan11)["item"]["costPerRun"] == 2.0
        finally:
            server.shutdown()
            thread.join(timeout=5)

print("STATS_RESULT=" + json.dumps({"ok": True}))
"""
        result = _run_isolated(script)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
