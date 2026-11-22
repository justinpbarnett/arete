import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAIL = 3000

SUITE = '''import os
import time
import unittest


def crowd(test):
    marker = os.path.join(os.environ["PARALLEL_MARKERS"], type(test).__name__)
    if not os.path.exists(marker):
        open(marker, "w").close()
        test.fail("crowded on the first run")


class CrowdedOne(unittest.TestCase):
    def test_it(self):
        crowd(self)


class CrowdedTwo(unittest.TestCase):
    def test_it(self):
        crowd(self)


class CrowdedThree(unittest.TestCase):
    def test_it(self):
        crowd(self)


class Noisy(unittest.TestCase):
    def test_assertion(self):
        for number in range(2000):
            print(f"line {number:05d}")
        self.assertEqual(1, 2, "noisy class assertion")


class Sleeper(unittest.TestCase):
    class_timeout = 0.5

    def test_hangs(self):
        time.sleep(60)
'''


class RunnerOutputTests(unittest.TestCase):
    class_timeout = 60

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.suite = root / "suite"
        self.suite.mkdir()
        (self.suite / "test_sample.py").write_text(SUITE)
        (root / "markers").mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("ARETE_VERIFY_SCRATCH", "ARETE_CONFIG_DIR")}
        self.env.update(PARALLEL_MARKERS=str(root / "markers"), ARETE_STATE_DIR=str(root / "state"))

    def run_suite(self, *names):
        done = subprocess.run([sys.executable, "-m", "tests.parallel", "-s", str(self.suite), *names],
                              check=False, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=self.class_timeout)
        return done.returncode, done.stdout + done.stderr

    def test_a_noisy_failing_class_keeps_its_traceback_in_the_tail(self):
        code, output = self.run_suite("test_sample.CrowdedOne", "test_sample.CrowdedTwo",
                                      "test_sample.CrowdedThree", "test_sample.Noisy")
        self.assertEqual(code, 1, output)
        tail = output[-TAIL:]
        self.assertIn("test_sample.Noisy: failures", tail)
        self.assertIn("Traceback (most recent call last):", tail)
        self.assertIn("AssertionError: 1 != 2 : noisy class assertion", tail)
        self.assertIn("Ran ", tail)
        self.assertIn("Worker reports and output retained in:", tail)
        self.assertEqual(output.count("on its first run, then passed alone"), 3)
        self.assertLess(output.rindex("on its first run"), output.index("line 01999"))
        self.assertLess(output.index("line 01999"), output.index("test_sample.Noisy: failures"))
        self.assertLess(output.index("test_sample.Noisy: failures"), output.index("Ran "))
        self.assertNotIn("line 00000", output)
        self.assertIn("1960 earlier output lines dropped", output)
        self.assertIn(".out", output.partition("1960 earlier output lines dropped")[2].partition("\n")[0])
        self.assertEqual(output.count("line 0"), 40)

    def test_a_class_that_times_out_alone_keeps_its_timeout_report_by_its_traceback(self):
        code, output = self.run_suite("test_sample.Sleeper")
        self.assertEqual(code, 1, output)
        self.assertIn("class exceeded 1s", output)
        self.assertIn("test_sample.Sleeper", output)
        self.assertIn("Python class stack", output)
        self.assertIn("in test_hangs", output)
        self.assertLess(output.rindex("class exceeded"), output.index("Ran "))
        self.assertNotIn("on its first run", output)


if __name__ == "__main__":
    unittest.main()
