import base64
import contextlib
import errno
import fcntl
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.metadata import Distribution
from importlib.util import spec_from_file_location
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

from arete import devices, loop_run, paths, record, releasebuild, releasejourneys, scratch

BUILD = "0.1.0 sha256." + "a" * 64
OTHER_BUILD = "0.1.0 sha256." + "b" * 64
SECRET = b"private fixture output"
STOP_DRIVER = """\
import sys
from pathlib import Path
from unittest.mock import patch

from arete import control, devices, loop_run, record

command = [sys.executable, "-c", sys.argv[1], *sys.argv[2:]]
with patch.object(control, "command", return_value=command):
    try:
        with record.stoppable():
            loop_run.run_command(["arete", "release-journeys"], Path.cwd(), timeout=None, env={}, device=devices.LOCAL)
    except record.Stopped:
        sys.exit(0)
sys.exit("loop command completed before stop")
"""
STOP_CONTROLLER = """\
import sys
import time
from pathlib import Path

from arete import releasejourneys

scratch = Path(sys.argv[1])
selected = releasejourneys.Selection(Path(sys.executable), scratch / "unused.whl", "", ())
releasejourneys.SETTINGS_PROBE = f"import runpy; runpy.run_path({sys.argv[2]!r}, run_name='__main__')"
try:
    releasejourneys.invoke(selected, releasejourneys.SETTINGS_ACTION, scratch, time.monotonic() + 30)
finally:
    (scratch / "controller-finally").touch()
"""
STOP_ACTION = """\
import fcntl
import os
import subprocess
import sys
import time

name = "descendant" if sys.argv[1:] else "action"
with open(name, "w") as held:
    fcntl.flock(held, fcntl.LOCK_EX)
    held.write(str(os.getpid()))
    held.flush()
    if name == "action":
        subprocess.Popen([sys.executable, "-I", "-B", __file__, "descendant"])
    time.sleep(30)
"""


def holds_lock(path):
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


class WheelIdentityTests(unittest.TestCase):
    def install_inventory(self, site: Path, asset: bytes) -> Path:
        wheel_info = "arete-0.1.0.dist-info"
        files = {
            "arete/__init__.py": b'__version__ = "0.1.0"\n',
            "arete/web/index.html": asset,
            f"{wheel_info}/METADATA": b"Name: arete\nVersion: 0.1.0.post1\n",
            f"{wheel_info}/WHEEL": b"Wheel-Version: 1.0\n",
            f"{wheel_info}/entry_points.txt": b"[console_scripts]\narete = arete.cli:main\n",
        }
        rows = []
        for name, content in files.items():
            file = site / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            rows.append(f"{name},sha256={digest},{len(content)}\n")
        (site / wheel_info / "RECORD").write_text("".join(rows))
        wheel = site.with_suffix(".whl")
        with ZipFile(wheel, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        (site / wheel_info / "direct_url.json").write_text(json.dumps({"archive_info": {"hashes": {
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}}}))
        return wheel

    def identity(self, site: Path, wheel: Path) -> str:
        spec = spec_from_file_location("arete", site / "arete" / "__init__.py")
        with (patch.object(releasebuild, "distribution", return_value=Distribution.at(site / "arete-0.1.0.dist-info")),
              patch.object(releasebuild.importlib.util, "find_spec", return_value=spec)):
            return releasebuild.wheel_identity(wheel)

    def test_installed_identities_use_package_contents_under_a_shared_parent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            one, two = (Path(directory) / name / "site-packages" for name in ("first", "second"))
            first_wheel = self.install_inventory(one, b"first wheel")
            second_wheel = self.install_inventory(two, b"second wheel")
            first = self.identity(one, first_wheel)
            second = self.identity(two, second_wheel)
            self.assertEqual(first, f"0.1.0.post1 sha256.{hashlib.sha256(first_wheel.read_bytes()).hexdigest()}")
            self.assertNotEqual(first, second)
            with self.assertRaisesRegex(ValueError, "archive digest differs"):
                self.identity(one, second_wheel)
            cache = one / "arete" / "__pycache__"
            cache.mkdir()
            (cache / "__init__.cpython-313.pyc").write_bytes(b"generated during install")
            with (one / "arete-0.1.0.dist-info" / "RECORD").open("a") as record:
                record.write("arete/__pycache__/__init__.cpython-313.pyc,,\n")
            self.assertEqual(self.identity(one, first_wheel), first)
            self.assertEqual(self.identity(two, second_wheel), second)
            with ZipFile(first_wheel, "a") as archive:
                archive.comment = b"same payload, different wheel bytes"
            with self.assertRaisesRegex(ValueError, "archive digest differs"):
                self.identity(one, first_wheel)
            (two / "arete" / "web" / "index.html").write_bytes(b"locally modified")
            with self.assertRaisesRegex(ValueError, "installed file differs from the wheel"):
                self.identity(two, second_wheel)


class ReleaseJourneyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.output = self.root / "evidence"
        self.config = self.root / "selection.toml"
        self.enterContext(patch.object(releasejourneys, "ACTION_SECONDS", 1))

    def exercise(self, invoke, *journeys, build=BUILD, confined_state=True, real_cleanup=False):
        self.config.write_text(f'python = "/release/bin/python"\nwheel = "/release/arete.whl"\n'
                               f'build = "{build}"\njourneys = {json.dumps(journeys)}\n')
        cleanup = (contextlib.nullcontext() if real_cleanup else
                   patch.object(releasejourneys, "cleanup_scratches", return_value=None))

        def action_result(*args):
            result = invoke(*args)
            return result if isinstance(result, releasejourneys.ActionResult) else releasejourneys.ActionResult(*result)

        action = patch.object(releasejourneys, "invoke", side_effect=action_result) if invoke else contextlib.nullcontext()
        with action, cleanup, patch.object(paths, "ON_MAC", confined_state):
            return self.run_config()

    def run_config(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = releasejourneys.run(self.config, self.output)
        path = stdout.getvalue().splitlines()[-1].split("local report ")[1]
        report = json.loads((self.output / path).read_text())
        findings = [json.loads(line) for line in stdout.getvalue().splitlines()[:-1]]
        for raw in (stdout.getvalue(), *(file.read_text() for file in self.output.rglob("*.json"))):
            self.assertNotIn(SECRET.decode(), raw)
        return code, report, findings

    def release(self, python, argv, scratch, deadline, *, overwrite=False, generated=b"max_parallel = 1\n"):
        config = scratch / "config/config.toml"
        if argv == releasejourneys.BUILD_ACTION:
            return 0, f"{BUILD}\n".encode()
        if argv == releasejourneys.SETTINGS_ACTION:
            out = io.StringIO()
            with patch.multiple(paths, CONFIG_FILE=config, ISOLATED=True), contextlib.redirect_stdout(out):
                exec(releasejourneys.SETTINGS_PROBE, {})
            return 0, out.getvalue().encode()
        if argv[0] == "init":
            if config.exists() and "--force" not in argv and not overwrite:
                return 1, f"{config} exists; pass --force to overwrite\n".encode()
            config.parent.mkdir(exist_ok=True)
            config.write_bytes(generated)
        if argv == ("migrate",):
            (scratch / "state").mkdir()
            (scratch / "state/arete.db").write_bytes(b"fixture state")
        if argv[0] == "status":
            if "--offline" not in argv:
                return 1, b'{"error": "no server", "reason": "server_down"}\n'
            return 0, b""
        return 0, SECRET

    def test_passing_journeys_record_build_and_actions_without_a_finding(self):
        code, report, findings = self.exercise(self.release, "first-run", "config-protection")
        self.assertEqual((code, report["outcome"], findings), (0, "pass", []))
        self.assertEqual((report["build"], report["actions"]), (BUILD, 9))
        self.assertEqual([row["outcome"] for row in report["journeys"]], ["pass", "pass"])
        attempt = report["journeys"][0]["attempts"][0]
        self.assertEqual(attempt["observed_build"], BUILD)
        self.assertEqual([action["argv"] for action in attempt["actions"]],
                         [["build-identity"], ["init"], ["validate-settings"], ["migrate"], ["status", "--json", "--offline"]])
        self.assertFalse((self.output / "findings").exists())

    def test_normal_cleanup_removes_attempt_scratch(self):
        probe = self.root / "teardown-probe"
        (probe / "nested").mkdir(parents=True)
        try:
            shutil.rmtree(probe)
        except PermissionError:
            self.skipTest("the test environment denies recursive directory removal")

        code, report, findings = self.exercise(self.release, "config-protection", real_cleanup=True)
        self.assertEqual((code, report["outcome"], findings), (0, "pass", []))
        self.assertEqual(list(self.output.glob(f"runs/*/{paths.SCRATCH_PREFIX}*")), [])
        self.assertTrue(self.config.is_file())

    def test_force_refusal_replays_but_an_unknown_exit_blocks(self):
        for known in (True, False):
            with self.subTest(known=known):
                def refuse(python, argv, scratch, deadline):
                    if argv == ("init", "--force"):
                        config = scratch / "config/config.toml"
                        return 1, f"{config} exists; pass --force to overwrite\n".encode() if known else SECRET
                    return self.release(python, argv, scratch, deadline)

                code, report, findings = self.exercise(refuse, "config-protection")
                attempts = report["journeys"][0]["attempts"]
                if known:
                    self.assertEqual((code, report["outcome"]), (0, "product-failure"))
                    self.assertEqual([row["reason"] for row in attempts], ["forced-init-replaces-config"] * 2)
                    self.assertEqual(len(findings), 1)
                else:
                    self.assertEqual((code, report["outcome"], findings), (1, "blocked", []))
                    attempt, = attempts
                    self.assertEqual((attempt["outcome"], attempt["reason"]),
                                     ("infrastructure", "unexpected-command-exit"))

    def test_unsafe_config_blocks_but_missing_config_replays(self):
        def damaged(python, argv, scratch, deadline):
            code, out = self.release(python, argv, scratch, deadline)
            if argv == ("init", "--force"):
                config = scratch / "config/config.toml"
                config.unlink()
                if unsafe:
                    config.symlink_to(self.root / "no-such-file")
            return code, out

        for unsafe in (True, False):
            with self.subTest(unsafe=unsafe):
                code, report, findings = self.exercise(damaged, "config-protection")
                attempts = report["journeys"][0]["attempts"]
                if unsafe:
                    self.assertEqual((code, report["outcome"], findings), (1, "blocked", []))
                    attempt, = attempts
                    self.assertEqual((attempt["outcome"], attempt["reason"]), ("environment", "scratch-io-failed"))
                    self.assertFalse((self.output / "findings").exists())
                else:
                    self.assertEqual((code, report["outcome"], len(findings)), (0, "product-failure", 1))
                    self.assertEqual([row["reason"] for row in attempts], ["forced-init-replaces-config"] * 2)

    def test_unreadable_state_evidence_blocks(self):
        open_file = os.open

        def unreadable(path, *args, **kwargs):
            if path == "arete.db":
                raise PermissionError(errno.EACCES, SECRET.decode())
            return open_file(path, *args, **kwargs)

        with patch.object(paths.os, "open", side_effect=unreadable):
            code, report, findings = self.exercise(self.release, "first-run")
        self.assertEqual((code, report["outcome"], findings), (1, "blocked", []))
        attempt, = report["journeys"][0]["attempts"]
        self.assertEqual((attempt["outcome"], attempt["reason"]), ("environment", "scratch-io-failed"))
        self.assertEqual(attempt["actions"][-1], {"argv": ["migrate"], "exit": 0})
        self.assertFalse((self.output / "findings").exists())

    def test_scratch_removal_follows_report_and_failure_only_warns(self):
        def overwrite(python, argv, scratch, deadline):
            return self.release(python, argv, scratch, deadline, overwrite=True)

        def refuse_removal(scratch):
            saved = json.loads((scratch.parent / "report.json").read_text())
            self.assertEqual(saved["outcome"], "product-failure")
            raise PermissionError(SECRET.decode())

        with (patch.object(releasejourneys.shutil, "rmtree", side_effect=refuse_removal) as remove,
              self.assertLogs(releasejourneys.LOG, "WARNING") as warnings):
            code, report, findings = self.exercise(overwrite, "config-protection", real_cleanup=True)
        self.assertEqual((code, report["outcome"], len(findings), remove.call_count), (0, "product-failure", 1, 2))
        self.assertIn("scratch-cleanup-failed (PermissionError)", warnings.output[0])
        self.assertNotIn(SECRET.decode(), "\n".join(warnings.output))

    def test_action_at_deadline_is_killed_and_reported(self):
        fixture = self.root / "release_action.py"
        fixture.write_text(STOP_ACTION)
        invoke = releasejourneys.invoke
        scratch_paths = []

        def run_stub(selected, argv, scratch, deadline):
            if argv != ("init",):
                return self.release(selected, argv, scratch, deadline)
            scratch_paths.append(scratch)
            selected = releasejourneys.Selection(Path(sys.executable), selected.wheel, selected.build, ())
            return invoke(selected, releasejourneys.SETTINGS_ACTION, scratch, deadline)

        with patch.object(releasejourneys, "SETTINGS_PROBE", f"import runpy; runpy.run_path({str(fixture)!r})"):
            code, report, findings = self.exercise(run_stub, "first-run")
        self.assertEqual((code, report["outcome"], findings), (1, "blocked", []))
        attempt, = report["journeys"][0]["attempts"]
        self.assertEqual((attempt["outcome"], attempt["reason"]), ("timeout", "action-timeout"))
        scratch, = scratch_paths
        self.assertTrue((scratch / "descendant").is_file())
        deadline = time.monotonic() + 1
        while any(holds_lock(scratch / name) for name in ("action", "descendant")):
            self.assertLess(time.monotonic(), deadline, "release action or descendant survived its deadline")
            time.sleep(0.01)

    def test_unreaped_action_timeout_still_writes_a_report(self):
        proc = Mock(spec=subprocess.Popen, pid=1234, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())

        def unreapable(*, timeout):
            self.assertTrue(proc.stdin.closed)
            self.assertGreaterEqual(timeout, 0)
            self.assertLessEqual(timeout, releasejourneys.REAP_SECONDS)
            raise subprocess.TimeoutExpired("release action", timeout)

        proc.wait.side_effect = unreapable
        timeout = releasejourneys.Blocked("timeout", "action-timeout")
        with (patch.object(releasejourneys.subprocess, "Popen", return_value=proc),
              patch.object(releasejourneys, "read_output", side_effect=timeout),
              patch.object(releasejourneys.os, "killpg") as kill):
            code, report, findings = self.exercise(None, "config-protection")
        self.assertEqual((code, report["outcome"], findings), (1, "blocked", []))
        attempt, = report["journeys"][0]["attempts"]
        self.assertEqual((attempt["outcome"], attempt["reason"]), ("timeout", "action-timeout"))
        self.assertEqual(attempt["actions"], [{"argv": ["build-identity"], "error": "action-timeout",
                                               "cleanup": "action-cleanup-timeout"}])
        self.assertEqual(proc.wait.call_count, 1)
        kill.assert_called_once_with(proc.pid, signal.SIGKILL)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)

    def test_loop_stop_kills_in_flight_action_and_descendant(self):
        fixture = self.root / "release_action.py"
        fixture.write_text(STOP_ACTION)
        self.check_controller_stop(fixture, wrapped=True)

    def test_stop_during_controller_startup_cleans_up_before_propagating(self):
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        self.enterContext(record.stoppable())
        for boundary in ("spawn", "first-thread-start"):
            with self.subTest(boundary=boundary):
                proc = Mock(spec=subprocess.Popen, returncode=None)

                def spawn(*args, **kwargs):
                    if boundary == "spawn":
                        signal.raise_signal(signal.SIGTERM)
                    return proc

                with (patch.object(scratch, "spawn", side_effect=spawn),
                      patch.object(loop_run.threading.Thread, "start", side_effect=record.Stopped) as start,
                      patch.object(loop_run.util, "kill_group") as kill):
                    proc.wait.side_effect = lambda _: kill.assert_called_once_with(proc)
                    with self.assertRaises(record.Stopped):
                        loop_run.run_command(["arete", "release-journeys"], self.root,
                                             timeout=None, env={}, device=devices.LOCAL)
                    kill.assert_called_once_with(proc)
                    proc.wait.assert_called_once()
                    self.assertEqual(start.call_count, int(boundary == "first-thread-start"))

    def test_stop_after_command_leader_is_reaped_kills_its_group(self):
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        self.enterContext(record.stoppable())
        read_stdin = [sys.executable, "-c", "import sys; sys.stdin.read()"]
        with (subprocess.Popen(read_stdin, process_group=0,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as leader,
              subprocess.Popen(read_stdin, process_group=leader.pid, stdin=subprocess.PIPE) as worker):
            wait = leader.wait

            def stop_after_reap(timeout):
                reaped = leader.returncode is not None
                code = wait(timeout)
                if not reaped:
                    self.assertEqual(leader.returncode, 0)
                    signal.raise_signal(signal.SIGTERM)
                return code

            with (patch.object(scratch, "spawn", return_value=leader),
                  patch.object(leader, "wait", side_effect=stop_after_reap),
                  patch.object(loop_run.util, "kill_group", wraps=loop_run.util.kill_group) as kill):
                with self.assertRaises(record.Stopped):
                    loop_run.run_command(["command"], self.root, timeout=1, env={}, device=devices.LOCAL)
                kill.assert_called_once_with(leader)
            self.assertEqual(worker.wait(timeout=1), -signal.SIGKILL)

    def test_sigkill_of_controller_kills_action_group(self):
        fixture = self.root / "release_action.py"
        fixture.write_text(STOP_ACTION)
        self.check_controller_stop(fixture, wrapped=False)

    def check_controller_stop(self, fixture, *, wrapped):
        locks = [self.root / "action", self.root / "descendant"]
        command = [sys.executable, "-c", STOP_CONTROLLER, str(self.root), str(fixture)]
        if wrapped:
            command.insert(2, STOP_DRIVER)
        driver = subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not all(holds_lock(path) and path.read_text() for path in locks):
                self.assertIsNone(driver.poll())
                self.assertLess(time.monotonic(), deadline, "release action and descendant did not start")
                time.sleep(0.01)

            os.killpg(driver.pid, signal.SIGTERM if wrapped else signal.SIGKILL)
            _, stderr = driver.communicate(timeout=10)
            self.assertEqual(driver.returncode, 0 if wrapped else -signal.SIGKILL, stderr.decode())
            deadline = time.monotonic() + 10
            while any(holds_lock(path) for path in locks):
                self.assertLess(time.monotonic(), deadline, "release action or descendant survived loop stop")
                time.sleep(0.01)
            self.assertFalse((self.root / "controller-finally").exists())
        finally:
            if driver.poll() is None:
                os.killpg(driver.pid, signal.SIGKILL)
            driver.communicate(timeout=1)
            for path in locks:
                if holds_lock(path) and (pid := path.read_text()):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(int(pid), signal.SIGKILL)

    def test_first_run_without_confined_state_support_stops_after_build_probe(self):
        code, report, findings = self.exercise(self.release, "first-run", confined_state=False)
        self.assertEqual((code, report["actions"], findings), (1, 1, []))
        attempt, = report["journeys"][0]["attempts"]
        self.assertEqual((attempt["outcome"], attempt["reason"]), ("environment", "confined-state-unsupported"))

    def test_parseable_settings_rejected_by_product_loader_are_replayed(self):
        def invalid(python, argv, scratch, deadline):
            return self.release(python, argv, scratch, deadline, generated=b'max_parallel = "invalid"\n')

        for journey, invariant in (("first-run", "initial-config-loads"), ("config-protection", "forced-config-loads")):
            with self.subTest(journey=journey):
                code, report, findings = self.exercise(invalid, journey)
                self.assertEqual((code, report["outcome"]), (0, "product-failure"))
                attempts = report["journeys"][0]["attempts"]
                self.assertEqual([row["reason"] for row in attempts], [invariant] * 2)
                self.assertEqual(len(findings), 1)

    def test_seeded_overwrite_requires_replay_and_coalesces_across_builds(self):
        scratch_paths = []
        builds = [BUILD]

        def overwritten(python, argv, scratch, deadline):
            if argv == releasejourneys.BUILD_ACTION:
                scratch_paths.append(scratch)
                return 0, f"{builds[-1]}\n".encode()
            return self.release(python, argv, scratch, deadline, overwrite=True)

        code, report, findings = self.exercise(overwritten, "config-protection")
        self.assertEqual((code, report["outcome"]), (0, "product-failure"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["fingerprint"], "release-journeys:config-protection:existing-config-preserved")
        first_report = report
        self.assertEqual([(attempt["reason"], attempt["observed_build"])
                          for attempt in report["journeys"][0]["attempts"]],
                         [("existing-config-preserved", BUILD)] * 2)
        builds.append(OTHER_BUILD)
        code, report, duplicates = self.exercise(overwritten, "config-protection", build=builds[-1])
        self.assertEqual((code, report["outcome"], duplicates), (0, "product-failure", []))
        self.assertEqual(report["findings"][0]["fingerprint"], findings[0]["fingerprint"])
        canonical, = (self.output / "findings").glob("*.json")
        self.assertEqual(json.loads(canonical.read_text()), findings[0])
        self.assertEqual(json.loads((self.output / findings[0]["evidence"]).read_text()), first_report)
        self.assertEqual(len(set(scratch_paths)), 4)

    def test_unexpected_product_exit_is_infrastructure(self):
        def unavailable(python, argv, scratch, deadline):
            if argv == ("migrate",):
                return 1, SECRET
            return self.release(python, argv, scratch, deadline)

        code, report, findings = self.exercise(unavailable, "first-run")
        self.assertEqual((code, report["outcome"], findings), (1, "blocked", []))
        attempt, = report["journeys"][0]["attempts"]
        self.assertEqual((attempt["outcome"], attempt["reason"]), ("infrastructure", "unexpected-command-exit"))
        self.assertEqual(attempt["actions"][-1], {"argv": ["migrate"], "exit": 1})

    def test_build_probe_distinguishes_startup_from_environment(self):
        cases = [
            ((1, SECRET), "startup", "build-probe-failed"),
            ((0, OTHER_BUILD.encode() + b"\n"), "environment", "release-build-mismatch"),
        ]
        for response, outcome, reason in cases:
            with self.subTest(outcome=outcome):
                code, report, findings = self.exercise(lambda *_: response, "first-run")
                self.assertEqual((code, report["actions"], findings), (1, 1, []))
                attempt, = report["journeys"][0]["attempts"]
                self.assertEqual((attempt["outcome"], attempt["reason"]), (outcome, reason))

    def test_replay_must_observe_the_pinned_artifact(self):
        builds = iter((BUILD, OTHER_BUILD))

        def replaced(python, argv, scratch, deadline):
            if argv == releasejourneys.BUILD_ACTION:
                return 0, next(builds).encode()
            return self.release(python, argv, scratch, deadline, overwrite=True)

        code, report, findings = self.exercise(replaced, "config-protection")
        self.assertEqual((code, report["outcome"], findings, report["findings"]), (1, "blocked", [], []))
        first, replay = report["journeys"][0]["attempts"]
        self.assertEqual((first["outcome"], first["observed_build"]), ("assertion", BUILD))
        self.assertEqual((replay["reason"], replay["observed_build"]), ("release-build-mismatch", OTHER_BUILD))
        self.assertEqual(replay["actions"], [{"argv": ["build-identity"], "exit": 0}])

    def test_deeply_nested_selection_is_an_environment_outcome(self):
        self.config.write_text("nested = " + "[" * 5000 + "0" + "]" * 5000)
        code, report, findings = self.run_config()
        self.assertEqual((code, report["outcome"], report["reason"], report["actions"], findings),
                         (1, "environment", "config-unreadable", 0, []))

    def test_selection_read_and_parse_errors_are_environment_outcomes(self):
        for text in ('syntactically = [', None):
            with self.subTest(text=text):
                if text is not None:
                    self.config.write_text(text)
                else:
                    self.config.unlink()
                code, report, findings = self.run_config()
                self.assertEqual((code, report["outcome"], report["reason"], report["actions"], findings),
                                 (1, "environment", "config-unreadable", 0, []))

    def test_failed_report_or_finding_writes_keep_available_evidence(self):
        write = releasejourneys.util.write_durably

        def overwrite(python, argv, scratch, deadline):
            return self.release(python, argv, scratch, deadline, overwrite=True)

        for phase in ("report", "finding"):
            with self.subTest(phase=phase):
                def unavailable(path, *args, **kwargs):
                    if path.name != "infrastructure.json" and (path.name == "report.json") == (phase == "report"):
                        raise PermissionError(SECRET.decode())
                    write(path, *args, **kwargs)

                with patch.object(releasejourneys.util, "write_durably", side_effect=unavailable):
                    code, report, findings = self.exercise(overwrite, "config-protection")
                self.assertEqual((code, report["outcome"], report["reason"], report["findings"], findings),
                                 (1, "infrastructure", f"{phase}-io-failed", [], []))
                self.assertEqual(report["journeys"][0]["outcome"], "product-failure")
                self.assertEqual(list((self.output / "findings").glob("*.json")), [])
                if phase == "report":
                    self.assertNotIn("completed_report", report)
                else:
                    completed = json.loads((self.output / report["completed_report"]).read_text())
                    self.assertEqual(completed["journeys"], report["journeys"])

    def test_probe_and_action_teardown_failures_are_both_reported(self):
        cases = [
            (1, "startup", "build-probe-failed"),
            (2, "environment", "release-wheel-unverified"),
        ]
        for exit_code, outcome, reason in cases:
            with self.subTest(outcome=outcome):
                def probe(*_):
                    return releasejourneys.ActionResult(exit_code, SECRET, "action-cleanup-failed")

                code, report, findings = self.exercise(probe, "config-protection")
                self.assertEqual((code, report["outcome"], findings), (1, "blocked", []))
                journey, = report["journeys"]
                attempt, = journey["attempts"]
                self.assertEqual(journey["outcome"], outcome)
                self.assertEqual((attempt["outcome"], attempt["reason"]), (outcome, reason))
                self.assertEqual(attempt["actions"][0]["cleanup"], "action-cleanup-failed")

    def test_action_cap_prevents_unconfirmed_assertion_from_publishing(self):
        with patch.object(releasejourneys, "MAX_ACTIONS", 2):
            def overwrite(python, argv, scratch, deadline):
                return self.release(python, argv, scratch, deadline, overwrite=True)

            code, report, findings = self.exercise(overwrite, "config-protection")
        self.assertEqual((code, report["actions"], findings), (1, 2, []))
        first, replay = report["journeys"][0]["attempts"]
        self.assertEqual(first["outcome"], "assertion")
        self.assertEqual((replay["outcome"], replay["reason"], replay["actions"]),
                         ("infrastructure", "run-limit", []))
