import contextlib
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arete import (
    accounts,
    agent,
    claude,
    codex,
    config,
    dbstats,
    devices,
    paths,
    pi,
    prices,
    record,
    runner,
    scrub,
    util,
)
from arete.store import db as foundation
from arete.store import runs as run_store


class DurableCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="arete-costs-")
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        database = cls.root / "costs.db"
        patches = contextlib.ExitStack()
        cls.addClassCleanup(patches.close)
        for name, value in {"DB_FILE": database, "RUNS_DIR": cls.root / "runs", "STATE_DIR": cls.root,
                            "WORKTREES_DIR": cls.root / "worktrees",
                            "LOCKS_DIR": cls.root / "locks", "LOG_DIR": cls.root / "logs"}.items():
            patches.enter_context(mock.patch.object(paths, name, value))
        patches.enter_context(mock.patch.object(foundation, "_CHECKED", set()))
        patches.enter_context(mock.patch.object(dbstats.Timing, "finished"))
        foundation.migrate()
        patches.enter_context(mock.patch.object(prices, "SNAPSHOT_FILE", cls.root / "absent-prices.json"))
        patches.enter_context(mock.patch.object(runner, "origin", return_value={"repo": "", "commit": ""}))
        patches.enter_context(mock.patch.object(runner, "secret_files", return_value=[]))
        patches.enter_context(mock.patch.object(scrub, "secrets", return_value=()))
        for module, name in ((claude, "run"), (codex, "run"), (pi, "run"), (accounts, "read"), (util, "sh")):
            patches.enter_context(mock.patch.object(module, name, side_effect=AssertionError("unexpected external call")))

    def start(self, rid):
        run_store.insert_run(rid, {"kind": "pipeline", "status": "running", "started": util.iso()})
        return rid

    def attempt(self, rid, which="claude", *, cfg=None, **kw):
        return runner.attempt(which, "test", "offline", self.root, agent.Session(),
                              cfg or config.Config(pricing={"test": (2, 4)}), (rid, "build"), rid,
                              devices.LOCAL, kw, auth=accounts.Auth(mode="api_key"))

    def marks(self, rid):
        timeline = paths.record_dir(rid) / record.TIMELINE
        return [entry for line in gzip.decompress(timeline.read_bytes()).decode().splitlines()
                if (entry := json.loads(line))["kind"] == "agent"]

    def charged(self, rid, *, turns, tokens_in, tokens_out, cost, error):
        row, = run_store.steps(rid)
        self.assertEqual((row["ok"], row["turns"], row["tokens_in"], row["tokens_out"]),
                         (0, turns, tokens_in, tokens_out))
        self.assertIn(error, row["error"])
        self.assertAlmostEqual(row["cost_usd"], cost, places=12)
        total = run_store.run_get(rid)
        self.assertEqual((total["cost_usd"], total["tokens_in"], total["tokens_out"]),
                         (row["cost_usd"], tokens_in, tokens_out))
        mark, = self.marks(rid)
        self.assertEqual((mark["cost_usd"], mark["error"]), (row["cost_usd"], row["error"]))

    @staticmethod
    def interrupted(*args, **kw):
        for ident in ("one", "two"):
            line = json.dumps({"type": "assistant", "message": {"id": ident,
                              "usage": {"input_tokens": 100, "output_tokens": 50}}})
            kw["on_line"](line)
            kw["on_line"](line)
        raise KeyboardInterrupt()

    def test_unlimited_interruptions_crash_park_and_token_cut_charge_once(self):
        with mock.patch.object(claude, "run", side_effect=self.interrupted):
            rid = self.start("interrupted")
            with self.assertRaises(KeyboardInterrupt):
                self.attempt(rid)
            self.charged(rid, turns=2, tokens_in=200, tokens_out=100, cost=.0008, error="stopped by request")

            rid = self.start("token-cut")
            result = self.attempt(rid, max_tokens=150)
            self.assertFalse(result.ok)
            self.charged(rid, turns=1, tokens_in=100, tokens_out=50, cost=.0004,
                         error="tokens left under the limit")

        def crashed(*args, **kw):
            kw["on_line"](json.dumps({"type": "message_end", "message": {"role": "assistant", "usage": {
                "input": 300, "output": 100, "cost": {"total": .25}}}}))
            raise RuntimeError("parked by chain budget")

        rid = self.start("partial-crash")
        with mock.patch.object(pi, "run", side_effect=crashed), self.assertRaisesRegex(RuntimeError, "parked"):
            self.attempt(rid, "pi")
        self.charged(rid, turns=1, tokens_in=300, tokens_out=100, cost=.001, error="parked by chain budget")

        rid = self.start("parked-before-usage")
        with mock.patch.object(claude, "run", side_effect=RuntimeError("parked before usage")), \
                self.assertRaisesRegex(RuntimeError, "parked"):
            self.attempt(rid)
        self.charged(rid, turns=0, tokens_in=0, tokens_out=0, cost=0,
                     error="crashed: RuntimeError: parked before usage")

    def test_failed_interrupted_write_and_post_commit_exception_never_charge_twice(self):
        rid = self.start("failed-write")
        with foundation.db() as conn:
            conn.execute("CREATE TRIGGER reject_call BEFORE INSERT ON steps "
                         "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        try:
            with mock.patch.object(claude, "run", side_effect=self.interrupted), \
                    self.assertRaisesRegex(sqlite3.IntegrityError, "refused"):
                self.attempt(rid)
            self.assertEqual(run_store.steps(rid), [])
            total = run_store.run_get(rid)
            self.assertEqual((total["cost_usd"], total["tokens_in"], total["tokens_out"]), (0, 0, 0))
            mark, = self.marks(rid)
            self.assertAlmostEqual(mark["cost_usd"], .0008, places=12)
            self.assertIn("cost is not in steps", mark["error"])
        finally:
            with foundation.db() as conn:
                conn.execute("DROP TRIGGER reject_call")

        # Retry after a rejected insert: only the successful write may change totals.
        with mock.patch.object(claude, "run", side_effect=self.interrupted), self.assertRaises(KeyboardInterrupt):
            self.attempt(rid)
        row, = run_store.steps(rid)
        self.assertAlmostEqual(row["cost_usd"], .0008, places=12)
        self.assertEqual(run_store.run_get(rid)["cost_usd"], row["cost_usd"])
        self.assertEqual((row["turns"], row["tokens_in"], row["tokens_out"]), (2, 200, 100))

        rid = self.start("post-commit")
        completion = []

        def broken_finish(recorder, result, seconds):
            completion.append((recorder, result, seconds))
            raise RuntimeError("timeline failure after commit")

        with mock.patch.object(claude, "run", return_value=agent.Result(ok=True, cost_usd=2.5)), \
                mock.patch.object(record.Recorder, "finish", broken_finish), \
                self.assertRaisesRegex(RuntimeError, "timeline failure after commit"):
            self.attempt(rid, cfg=config.Config())
        self.assertEqual(len(completion), 1)
        row, = run_store.steps(rid)
        self.assertEqual((row["cost_usd"], run_store.run_get(rid)["cost_usd"]), (2.5, 2.5))
        # Retry timeline completion of the same committed attempt. Calling attempt
        # again would launch another billable call; it has no replay/idempotency key.
        recorder, result, seconds = completion[0]
        recorder.finish(result, seconds)
        recorder.finish(result, seconds)
        self.assertEqual(run_store.steps(rid), [row])
        self.assertEqual(run_store.run_get(rid)["cost_usd"], 2.5)


if __name__ == "__main__":
    unittest.main()
