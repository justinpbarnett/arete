import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arete import lanes

ROOT = Path(__file__).resolve().parent.parent
LANES = ["checks-0", "checks-1"]
WAITING = "waiting for a free check lane"
TINY_SUITE = "import unittest\n\n\nclass Tiny(unittest.TestCase):\n    def test_runs(self):\n        pass\n"


class WorkerCheckLaneTests(unittest.TestCase):
    class_timeout = 60

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.suite = root / "suite"
        self.suite.mkdir()
        (self.suite / "test_tiny.py").write_text(TINY_SUITE)
        (root / "state" / "locks").mkdir(parents=True)
        self.held = []
        for name in LANES:
            lane = open(root / "state" / "locks" / f"{name}.lock", "a")
            self.addCleanup(lane.close)
            fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held.append(lane)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("ARETE_VERIFY_SCRATCH", "ARETE_CONFIG_DIR")}
        self.env.update(ARETE_STATE_DIR=str(root / "state"), **{lanes.ENV: ",".join(LANES)})

    def launch(self, *names):
        proc = subprocess.Popen([sys.executable, "-m", "tests.parallel", "-s", str(self.suite), *names], cwd=ROOT,
                                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_full_suite_waits_for_a_free_lane_then_runs(self):
        proc = self.launch()
        self.assertIn(WAITING, proc.stdout.readline())
        self.assertIsNone(proc.poll())
        self.held[0].close()
        output, _ = proc.communicate(timeout=self.class_timeout)
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("Ran 1 tests", output)
        self.assertNotIn(WAITING, output)

    def test_named_class_runs_at_once_with_both_lanes_held(self):
        proc = self.launch("test_tiny.Tiny")
        output, _ = proc.communicate(timeout=self.class_timeout)
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("Ran 1 tests", output)
        self.assertNotIn(WAITING, output)

    def test_lane_names_outside_the_check_locks_are_dropped(self):
        env = {lanes.ENV: "checks,checks-1,../serve,checks-x,"}
        self.assertEqual(lanes.worker_names(env), ["checks", "checks-1"])
        self.assertEqual(lanes.worker_names({}), [])


if __name__ == "__main__":
    unittest.main()
