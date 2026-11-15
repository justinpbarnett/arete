from pathlib import Path
import tempfile
import unittest

from tests import support
from tests.harness import behavior_result, make_folders, run_script, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class ServeBulkheadTests(unittest.TestCase):
    SCRIPT = (SCRIPTS / "serve_bulkhead.py").read_text()

    def case(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
            env = scratch_env(root, ARETE_WORKTREES_DIR=str(root / "worktrees"), TMPDIR=str(root / "tmp"),
                              HOME=str(root / "home"))
            done = run_script(self.SCRIPT, root, str(root), mode, env=env, timeout=300)
            return behavior_result(done, marker="RESULT")

    def test_one_failing_loop_costs_only_itself_and_the_readers_name_it(self):
        out = self.case("fault")
        self.assertTrue(out["clean"] and out["clean_last_ok"])
        self.assertEqual([p["bbb"] for p in out["passes"]], [1, 1, 1])
        self.assertEqual([p["ticks"] for p in out["passes"]], [1, 2, 3])
        self.assertTrue(all(not p["tick_ok"] and "bulkhead probe" in p["error"] for p in out["passes"]))
        self.assertEqual([(p["severity"], p["ok"]) for p in out["passes"]], [("warn", False), ("warn", False), ("fail", False)])
        self.assertEqual(out["failed_logs"], 1)
        self.assertTrue(out["alive"] and out["stuck"])
        self.assertIn("loop:aaa", out["details"])
        self.assertNotIn("serve is down", out["details"])
        self.assertIn("loop:aaa", out["line"])
        self.assertNotIn("DOWN", out["line"])
        self.assertEqual(out["word"], "fault")
        self.assertEqual(out["recovered"], 1)
        self.assertEqual(out["failing"], {})
        self.assertTrue(out["tick_ok"] and out["last_ok"] and out["after_ok"])

    def test_on_call_opens_one_incident_for_a_stuck_loop(self):
        out = self.case("oncall")
        self.assertEqual(len(out["second"]), len(out["first"]))
        self.assertEqual(len([t for t in out["texts"] if "loop:aaa" in t]), 1)

    def test_an_approval_the_code_cannot_read_does_not_stop_the_next_one_or_housekeeping(self):
        out = self.case("approvals")
        self.assertEqual(out["ticks"], [1, 2, 3])
        self.assertEqual(out["failing"], {"approval:2:plain_summary": "KeyError: 'renamed_emit'"})
        self.assertEqual(out["summary"], "fixed text")
        self.assertEqual(out["asks"], 3)
        self.assertEqual(out["severity"], "warn")
        self.assertEqual(out["after"], {})

    def test_a_queue_item_that_raises_stays_queued_and_the_rest_of_the_queue_goes_on(self):
        out = self.case("queue")
        self.assertEqual(out["launched"], ["bbb"])
        self.assertEqual(out["pending"], [out["one"]])
        self.assertEqual(out["first"], [f"queue:{out['one']}"])
        self.assertEqual(out["kept"], {f"queue:{out['one']}": 2})
        self.assertEqual(out["unread"][f"queue:{out['one']}"], 2)
        self.assertIn("stage:dispatch", out["unread"])
        self.assertIsNone(out["unread_pending"])
        self.assertEqual(out["cleared"], [])

    def test_dispatch_keeps_its_wait_rules(self):
        out = self.case("waits")
        self.assertEqual(out["group"], [1, 1])
        self.assertEqual(out["NeverBegan"], [1, None, out["NeverBegan"][2]])
        self.assertTrue(out["NeverBegan"][2])
        self.assertEqual(out["Refused"][:2], [1, None])
        self.assertEqual(out["busy"], 1)
        self.assertEqual(out["spent"], [1, []])
        self.assertEqual(out["dropped"], 1)

    def test_serve_raises_its_own_alert_once_after_nine_failing_passes_and_clears_it(self):
        out = self.case("alerts")
        self.assertEqual(out["seen"], [0] * 8 + [1, 1])
        self.assertEqual(out["ticks"], list(range(1, 11)))
        self.assertTrue(out["tick_ok"])
        self.assertEqual((out["closed"], out["fresh"]), (1, 1))
        self.assertEqual(out["asks"], 11)
        self.assertEqual(out["summary"], "fixed text")

    def test_a_slow_alert_raise_cannot_reopen_after_the_clear(self):
        self.assertEqual(self.case("alert_order")["order"], ["raise", "clear"])

    def test_an_unreadable_config_still_beats_and_changes_nothing(self):
        out = self.case("bad_config")
        self.assertEqual(out["failing"], ["tick:load"])
        self.assertEqual(out["loops"][0], out["loops"][1])
        self.assertEqual(out["off"][0], out["off"][1])
        self.assertEqual(out["ticks"], [1, 2])
        self.assertTrue(out["same"] and out["problem"])
        self.assertIn("tick:load", out["line"])
        self.assertEqual(out["recovered"], [])
        self.assertEqual(out["fires"], [2, 4])

    def test_an_unreadable_loop_state_fires_nothing_and_is_only_a_warning(self):
        out = self.case("states")
        self.assertEqual(out["failing"], ["stage:states"])
        self.assertEqual((out["severity"], out["ok"]), ("warn", False))
        self.assertEqual(out["loaded"], [False, True])
        self.assertTrue(out["unchanged"])
        self.assertEqual(out["queued"], 0)
        self.assertEqual(out["launched"], ["aaa", "bbb"])
        self.assertEqual((out["beat_back"], out["ccc"]), ([], 1))

    def test_the_beat_survives_an_unreadable_count_and_a_failed_write(self):
        out = self.case("beat")
        self.assertEqual(out["first"][0], None)
        self.assertIsNotNone(out["first"][1])
        self.assertEqual(out["second"][1], None)
        self.assertIsNotNone(out["second"][0])
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["ticks"], [2, 4])

    def test_code_tick_and_inherited_failures_each_leave_the_later_parts_running(self):
        out = self.case("guard")
        self.assertEqual(out["order"], ["code", "tick", "inherited"])
        self.assertEqual(out["keys"], ["stage:code", "stage:inherited", "tick"])
        self.assertEqual(out["ticks"], [1, 2, 3])
        self.assertEqual(out["tick_logs"], 2)
        self.assertEqual((out["code_ticks"], out["tick"]["ticks"]), (3, 3))
        self.assertEqual(out["clean"], {})
        self.assertEqual(out["recovered"], 1)


if __name__ == "__main__":
    unittest.main()
