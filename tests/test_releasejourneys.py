import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arete import paths, releasejourneys

BUILD = "0.1.0 code.0123456789ab"
SECRET = b"private fixture output"


class ReleaseJourneyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.output = self.root / "evidence"
        self.config = self.root / "selection.toml"

    def exercise(self, invoke, *journeys, build=BUILD):
        self.config.write_text(f'python = "/release/bin/python"\nbuild = "{build}"\njourneys = {json.dumps(journeys)}\n')
        stdout = io.StringIO()
        with patch.object(releasejourneys, "invoke", side_effect=invoke), contextlib.redirect_stdout(stdout):
            code = releasejourneys.run(self.config, self.output)
        path = stdout.getvalue().splitlines()[-1].split("local report ")[1]
        report = json.loads((self.output / path).read_text())
        findings = [json.loads(line) for line in stdout.getvalue().splitlines()[:-1]]
        for raw in (stdout.getvalue(), *(file.read_text() for file in self.output.rglob("*.json"))):
            self.assertNotIn(SECRET.decode(), raw)
        return code, report, findings

    def release(self, python, argv, scratch, deadline, *, overwrite=False, generated=b"max_parallel = 1\n"):
        config = scratch / "config/config.toml"
        if argv == ("--version",):
            return 0, f"arete {BUILD}\n".encode()
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
        return 0, b"" if argv[0] == "status" else SECRET

    def test_passing_journeys_record_build_and_actions_without_a_finding(self):
        code, report, findings = self.exercise(self.release, "first-run", "config-protection")
        self.assertEqual((code, report["outcome"], findings), (0, "pass", []))
        self.assertEqual((report["build"], report["actions"]), (BUILD, 10))
        self.assertEqual([row["outcome"] for row in report["journeys"]], ["pass", "pass"])
        attempt = report["journeys"][0]["attempts"][0]
        self.assertEqual(attempt["observed_build"], BUILD)
        self.assertEqual([action["argv"] for action in attempt["actions"]],
                         [["--version"], ["init"], ["validate-settings"], ["migrate"], ["status", "--json"]])
        self.assertFalse((self.output / "findings").exists())

    def test_first_run_migrates_and_reads_confined_linux_state(self):
        scratch = self.root / (paths.SCRATCH_PREFIX + "637428dbb12746ce")
        scratch.mkdir(mode=0o700)
        program = """\
from contextlib import redirect_stdout
from io import StringIO

from arete import cli, paths
from arete.store import db

paths.ON_MAC = False
db.LINUX = True
assert cli.main(["init"]) == 0
assert cli.main(["migrate"]) == 0
assert paths.DB_FILE.stat().st_size > 0
status = StringIO()
with redirect_stdout(status):
    assert cli.main(["status", "--json"]) == 0
assert not status.getvalue()
with db.reader() as conn:
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.LATEST

sidecar = paths.DB_FILE.with_suffix(".db-wal")
sidecar.touch()
for target in (paths.DB_FILE, paths.STATE_DIR.parent.parent / "outside.db"):
    try:
        db.connect_db(target, create=True).close()
    except PermissionError:
        pass
    else:
        raise AssertionError("isolated state accepted an unsafe database")
"""
        env = {paths.SCRATCH_ENV: str(scratch), "HOME": str(scratch), "PATH": os.defpath,
               "XDG_STATE_HOME": str(scratch / "client")}
        result = subprocess.run([sys.executable, "-B", "-c", program], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "outside.db").exists())

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
            if argv == ("--version",):
                scratch_paths.append(scratch)
                return 0, f"arete {builds[-1]}\n".encode()
            return self.release(python, argv, scratch, deadline, overwrite=True)

        code, report, findings = self.exercise(overwritten, "config-protection")
        self.assertEqual((code, report["outcome"]), (0, "product-failure"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["fingerprint"], "release-journeys:config-protection:existing-config-preserved")
        first_report = report
        self.assertEqual([attempt["reason"] for attempt in report["journeys"][0]["attempts"]],
                         ["existing-config-preserved", "existing-config-preserved"])
        builds.append("0.1.0 code.0123456789ac")
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

    def test_version_probe_distinguishes_startup_from_environment(self):
        cases = [
            ((1, SECRET), "startup", "version-probe-failed"),
            ((0, b"arete 0.1.0 123456789abc\n"), "environment", "release-build-mismatch"),
        ]
        for response, outcome, reason in cases:
            with self.subTest(outcome=outcome):
                code, report, findings = self.exercise(lambda *_: response, "first-run")
                self.assertEqual((code, report["actions"], findings), (1, 1, []))
                attempt, = report["journeys"][0]["attempts"]
                self.assertEqual((attempt["outcome"], attempt["reason"]), (outcome, reason))

    def test_action_cap_prevents_unconfirmed_assertion_from_publishing(self):
        with patch.object(releasejourneys, "MAX_ACTIONS", 3):
            def overwrite(python, argv, scratch, deadline):
                return self.release(python, argv, scratch, deadline, overwrite=True)

            code, report, findings = self.exercise(overwrite, "config-protection")
        self.assertEqual((code, report["actions"], findings), (1, 3, []))
        first, replay = report["journeys"][0]["attempts"]
        self.assertEqual(first["outcome"], "assertion")
        self.assertEqual((replay["outcome"], replay["reason"], replay["actions"]),
                         ("infrastructure", "run-limit", []))
