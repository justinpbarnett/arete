import os
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from arete import config, environment, orchestration, paths, pipeline, scratch, tracker, util, workspace
from arete.agent import Result


class VerificationCleanupTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.wt = root / "worktree"
        self.wt.mkdir()
        util.git(["init", "-q", "--template="], self.wt)
        (self.wt / "tracked.txt").write_text("tracked content\n")
        util.git(["add", "tracked.txt"], self.wt)

        self.run_dir = root / "run"
        self.run_dir.mkdir()
        config_file = root / "config.toml"
        config_file.write_text('[environment]\npass = ["VERIFIER_FIXTURE_VALUE"]\n')
        self.cfg = config.load(config_file, required=True)
        repo = config.Repo("fixture", self.wt)
        self.run = pipeline.Run("verify-cleanup", repo, self.run_dir, tracker=mock.Mock(), tid="1")
        checkout = workspace.Checkout("", "owner/fixture", self.wt, self.wt, "main")
        ticket = tracker.Ticket("1", "#1", "Check the fixture")
        layer = config.RepoLayer(root, verify=config.Verify(url=""))
        self.work = pipeline.TicketWork(self.cfg, self.run, ticket, self.wt, [checkout], layer, 60, "test workspace")
        self.enterContext(mock.patch.object(paths, "dashboard_ports", return_value=frozenset()))
        self.enterContext(mock.patch.object(scratch, "make", side_effect=ValueError("no scratch authority")))
        self.enterContext(mock.patch.object(workspace, "scratch_view", return_value=scratch.View([], [], True)))
        self.enterContext(mock.patch.object(self.run, "log"))

    def evidence(self, run_agent):
        with mock.patch.object(self.run, "agent", side_effect=run_agent):
            verified = pipeline.use_product(self.work, "verify", "verify", goal="Check the fixture.", summary="", diff="")
        self.assertEqual(verified.verdict, "pass")
        controller = SimpleNamespace(data={"acceptance": []}, executing="d0001", catalog=mock.Mock(),
                                     fingerprint=lambda: "fixture-tree", policy_sha=lambda: "fixture-policy")
        orchestration.Controller.stamp(controller, "verify", asdict(verified))
        name, evidence = controller.catalog.add_context.call_args.args
        self.assertEqual(name, "evidence:verify")
        return (self.run_dir / "verify.md").read_text(), evidence

    def agent_result(self, *_args, **_kwargs):
        return Result(ok=True, structured={"verdict": "pass", "summary": "The fixture passed.", "checks": [
            {"what": "Inspect the fixture", "how": "Open it", "saw": "It opened", "passed": True},
        ]})

    def test_removed_file_is_reported_without_its_forwarded_value(self):
        synthetic = "synthetic-forwarded-fixture-value"
        added = self.wt / f"{synthetic}.txt"
        screenshot = self.wt / "fixture.png"

        def agent(*args, **kwargs):
            forwarded = environment.build(environment.passed(self.cfg, self.run.repo))
            (self.wt / f"{forwarded['VERIFIER_FIXTURE_VALUE']}.txt").write_text("fixture\n")
            screenshot.write_bytes(b"image fixture")
            return self.agent_result(*args, **kwargs)

        with mock.patch.dict(os.environ, {"VERIFIER_FIXTURE_VALUE": synthetic}):
            saved, evidence = self.evidence(agent)
        self.assertFalse(added.exists())
        self.assertFalse(screenshot.exists())
        self.assertEqual((self.run_dir / "evidence" / screenshot.name).read_bytes(), b"image fixture")
        line = ("Arete removed 1 untracked file that verification added to the worktree, "
                "after verification ended. None is left behind.")
        self.assertIn(line, saved.splitlines())
        for surface in (saved, evidence):
            self.assertEqual(surface.count(line), 1)
            self.assertNotIn(synthetic, surface)

    def test_no_added_file_has_no_cleanup_line(self):
        for surface in self.evidence(self.agent_result):
            self.assertNotIn("Arete removed", surface)
