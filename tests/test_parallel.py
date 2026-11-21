import fcntl
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arete import ci, gh, orchestration
from arete.orchestration_api import Catalog


class ParallelTimeoutTests(unittest.TestCase):
    def run_fixture(self, source, *, runner=("-m", "tests.parallel")):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "test_fixture.py").write_text(textwrap.dedent(source))
        result = subprocess.run(
            [sys.executable, *runner, "--start", str(root), "--jobs", "3", "--timeout", "3"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "TMPDIR": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=9,
        )
        return root, result

    def test_timeout_reports_class_threads_and_python_children(self):
        root, result = self.run_fixture('''
            import subprocess
            import sys
            import threading
            import time
            import unittest
            from pathlib import Path

            from arete import api_check

            root = Path(__file__).parent

            def wait_in_thread():
                time.sleep(30)

            class WaitInClass(unittest.TestCase):
                def test_wait_in_class(self):
                    threading.Thread(target=wait_in_thread, daemon=True).start()
                    time.sleep(30)

            class WaitForChildren(unittest.TestCase):
                def test_wait_for_children(self):
                    child = (
                        "import threading, time\\n"
                        "def wait_in_child_thread():\\n time.sleep(30)\\n"
                        "threading.Thread(target=wait_in_child_thread, daemon=True).start()\\n"
                        "def wait_in_child():\\n time.sleep(30)\\n"
                        "wait_in_child()"
                    )
                    plain_isolated = "import time; time.sleep(30)"
                    helper = (
                        "import fcntl, signal, time\\nfrom pathlib import Path\\n"
                        "lock = open('helper-lock', 'w')\\nfcntl.flock(lock, fcntl.LOCK_EX)\\n"
                        "signal.signal(signal.SIGUSR1, lambda *_: Path('helper-signaled').touch())\\n"
                        "signal.signal(signal.SIGURG, lambda *_: Path('helper-signaled').touch())\\n"
                        "Path('helper-ready').touch()\\ntime.sleep(30)\\n"
                    )
                    api = child.replace("wait_in_child", "wait_in_api_child")
                    api_command = api_check.child_command(api_check.SOURCE, "exec(sys.argv[1])", api)
                    commands = (([sys.executable, "-c", child], None),
                                (api_command, api_check.isolated_env(root)),
                                ([sys.executable, "-I", "-c", plain_isolated], None),
                                ([sys.executable, "-I", "-c", helper], None))
                    children = [subprocess.Popen(cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                                for cmd, env in commands]
                    (root / "plain-isolated-pid").write_text(str(children[2].pid))
                    children[0].wait()

            class WaitForInstalledStatus(unittest.TestCase):
                def test_wait_for_installed_status(self):
                    from tests.test_workflow import ServeStatusTests

                    ServeStatusTests.setUp(self)
                    candidate = self.root / "arete"
                    candidate.mkdir()
                    source = api_check.SOURCE / "arete"
                    (candidate / "__init__.py").write_text(
                        (source / "__init__.py").read_text() + f"\\n__path__.append({str(source)!r})\\n"
                    )
                    (candidate / "paths.py").write_text(
                        "import threading, time\\n"
                        "def wait_in_cli_thread():\\n    time.sleep(30)\\n"
                        "for _ in range(32):\\n"
                        "    threading.Thread(target=wait_in_cli_thread, daemon=True).start()\\n"
                        "def wait_in_installed_cli():\\n    time.sleep(30)\\n"
                        "wait_in_installed_cli()\\n"
                    )
                    ServeStatusTests.check_revisions(self, self.env, Path(self.env["ARETE_STATE_DIR"]))
        ''')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertEqual(output.count("class exceeded 3s"), 3, output)
        self.assertEqual(output.count("Python class stack (PID"), 3, output)
        self.assertEqual(output.count("Python child stack (PID"), 3, output)
        for frame in ("test_wait_in_class", "wait_in_thread", "wait_in_child", "wait_in_child_thread",
                      "wait_in_api_child", "wait_in_installed_cli"):
            self.assertRegex(output, rf"class exceeded 3s[\s\S]* in {frame}\n")
        plain_isolated_pid = (root / "plain-isolated-pid").read_text()
        self.assertNotIn(f"Python child stack (PID {plain_isolated_pid})", output)
        self.assertTrue((root / "helper-ready").exists(), output)
        self.assertFalse((root / "helper-signaled").exists(), output)
        with (root / "helper-lock").open("rb") as lock:
            deadline = time.monotonic() + 1
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.assertLess(time.monotonic(), deadline, "unregistered child survived group cleanup")
                    time.sleep(.01)

        reports = ci.TEST_TIMEOUT.findall(output)
        short = next(report for report in reports if "WaitForChildren" in report)
        self.assertIn(" in wait_in_child_thread\n", short)
        error = "##[error]Process completed with exit code 1."
        short_log = self.actions_log(
            f"Started checks\n=== Test class timeout ===\n{short}\n=== End test class timeout ===\n"
            f"{error}\nCleanup finished\n"
        )
        self.assertLess(len(short_log), ci.LOG_EXCERPT_CHARS)
        self.assertEqual(self.catalog_log(root, short_log), short_log)

        installed = next(report for report in reports if "WaitForInstalledStatus" in report)
        self.assertGreater(len(installed), ci.LOG_EXCERPT_CHARS)
        self.assertEqual(installed.count(" in wait_in_cli_thread\n"), 32)
        excerpt = self.catalog_log(root, self.actions_log(f"{output}{error}\n"))
        self.assertLessEqual(len(excerpt), ci.LOG_EXCERPT_CHARS)
        self.assertRegex(excerpt, r"test_fixture.WaitForInstalledStatus\nclass exceeded 3s\n"
                                  r"Python class stack \(PID \d+\)[\s\S]*? in check_revisions\n"
                                  r"[\s\S]*?Python child stack \(PID \d+\)[\s\S]*? in wait_in_installed_cli\n")
        self.assertIn(error, excerpt)

    def actions_log(self, output):
        return "".join(f"2026-11-21T08:10:00.0000000Z {line}" for line in output.splitlines(keepends=True))

    def catalog_log(self, root, log):
        target = ci.Target("fixture/repo", "7", "#7", "head", "main")
        check = gh.Check("arete-checks", "fail", job=1)
        watch = ci.Watched(target, {check.key: check}, reds=[ci.Red(target, check, None, "")])
        controller = orchestration.Controller.__new__(orchestration.Controller)
        controller.run = SimpleNamespace(dir=root, add_dirs=[])
        catalog = Catalog.__new__(Catalog)
        catalog.context = {}
        with patch.object(gh, "job_log", return_value=log):
            catalog.add_context(orchestration.CI_FAILURES, controller.ci_failures(ci.Round(1, [watch])))
        self.assertEqual(next((root / "ci").glob("*.log")).read_text(), log)
        page = catalog.read(orchestration.CI_FAILURES, 0, 8000)
        text = page["text"]
        if page["next"] is not None:
            text += catalog.read(orchestration.CI_FAILURES, page["next"], 8000)["text"]
        return text.split("\n````", 2)[1].removeprefix("\n")

    def test_normal_completion_skips_diagnostic_collection(self):
        runner = ("-c", "from tests import parallel; from unittest.mock import Mock; "
                  "parallel.Runner.request_stacks = Mock(side_effect=AssertionError('unexpected collection')); "
                  "raise SystemExit(parallel.main())")
        _, result = self.run_fixture('''
            import unittest

            class Completes(unittest.TestCase):
                def test_completes(self):
                    pass
        ''', runner=runner)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"\ARan 1 tests in \d+\.\d+s: OK\n\Z")
        self.assertEqual(result.stderr, "")
