import io
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from arete import install


class ServeEnvironmentTests(unittest.TestCase):
    def test_services_use_the_cli_state_home(self):
        state_home = Path("custom state 50%").absolute()
        with patch.object(install.paths, "CLIENT_DIR", state_home / "arete-client"):
            plist = install.serve_plist()
            unit = install.serve_unit()
        self.assertEqual(plist["EnvironmentVariables"]["XDG_STATE_HOME"], str(state_home))
        escaped = str(state_home).replace("%", "%%")
        self.assertIn(f'Environment="XDG_STATE_HOME={escaped}"', unit.splitlines())


class LaunchdReinstallTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(install, "AGENTS_DIR", Path(__file__).parent))
        self.enterContext(patch.object(Path, "write_bytes"))
        self.enterContext(patch.object(install, "write_serve_guard"))
        self.enterContext(patch.object(install, "retire"))
        self.enterContext(patch.object(install, "this_platform", return_value="mac"))
        self.enterContext(patch.object(install.paths, "ensure_dirs"))
        self.prepare_trackers = self.enterContext(patch.object(install, "prepare_trackers"))
        self.sleep = self.enterContext(patch.object(install.time, "sleep"))
        self.enterContext(patch.object(install.util, "sh", side_effect=self.launchctl))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.commands = []
        self.old_job_prints = 0
        self.bootstrap_errors = []
        self.loaded = False

    def launchctl(self, argv, **kwargs):
        program, command, *args = argv
        self.assertEqual(program, "launchctl")
        self.commands.append(command)
        domain = install._domain()
        job = f"{domain}/com.arete.serve"
        if command == "bootout":
            self.assertEqual(args, [job])
        elif command == "print":
            self.assertEqual(args, [job])
            if self.old_job_prints:
                self.old_job_prints -= 1
            elif not self.loaded:
                return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
        elif command == "bootstrap":
            self.assertEqual(args, [domain, str(install.serve_file("mac"))])
            if self.old_job_prints:
                return subprocess.CompletedProcess(argv, 5, "", "Old job is still loaded")
            if self.bootstrap_errors:
                error = self.bootstrap_errors[0]
                if len(self.bootstrap_errors) > 1:
                    self.bootstrap_errors.pop(0)
                return subprocess.CompletedProcess(argv, 5, "", error)
            self.loaded = True
        else:
            self.fail(f"unexpected launchctl command: {command}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_waits_for_old_job_and_confirms_new_job(self):
        self.old_job_prints = 2

        self.assertEqual(install.install_launchd(), 0)

        self.assertEqual(self.commands, ["bootout", "print", "print", "print", "bootstrap", "print"])
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(self.output.getvalue(), "com.arete.serve: loaded\n")

    def test_returns_final_bootstrap_error_before_tracker_work(self):
        self.bootstrap_errors = ["Initial error", "Retry error", "Input/output error"]

        self.assertNotEqual(install.install(), 0)

        self.assertEqual(self.commands, ["bootout", "print", "bootstrap", "bootstrap", "bootstrap"])
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(self.output.getvalue(), "com.arete.serve: bootstrap failed: Input/output error\n")
        self.prepare_trackers.assert_not_called()
