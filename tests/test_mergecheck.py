from contextlib import ExitStack, nullcontext
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from arete import config, mergecheck, paths, pipeline, record, runner, sandbox, scratch, util, verify

from tests import support


def setUpModule():
    support.isolate_live_state()


class MergeCheckEvidenceTests(unittest.TestCase):
    def test_latest_outcome_without_saved_evidence_cannot_reuse_an_earlier_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "landed"
            folder.mkdir()
            (folder / "verify.md").write_text("- **Passed**: stale\n  - How: stale instructions\n")
            history = [{"event": "step_end", "result": "pass", "step": "verify", "run": "landed",
                        "data": {"verified": {"saved": ""}}}]
            with mock.patch.object(paths, "RUNS_DIR", root), \
                    mock.patch.object(mergecheck.factory, "chain", return_value=[history]):
                with self.assertRaisesRegex(mergecheck.Blocked, "latest verification has no saved report"):
                    mergecheck.report_steps({"run_id": "landed", "pr": 1})
                history[0]["data"]["verified"]["saved"] = str(folder / "verify2.md")
                with self.assertRaisesRegex(mergecheck.Blocked, "unreadable verification report"):
                    mergecheck.report_steps({"run_id": "landed", "pr": 1})

    def test_startup_regression_emits_failure_but_confinement_preserves_unchecked_work(self):
        for infrastructure in (False, True):
            with self.subTest(infrastructure=infrastructure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo = config.Repo(name="fixture", path=root / "repo", pipeline=True)
                cfg = config.Config(repos=[repo])
                layer = config.RepoLayer(dir=root, verify=config.Verify(start="fixture-start"))
                item = {"run_id": "landed", "key": "https://github.com/offline/fixture/pull/1",
                        "repo": "fixture", "pr": 1}
                steps = [mergecheck.Step("landed", 1, "New feature", "Use feature", 1),
                         mergecheck.Step("earlier", 2, "Neighbour", "Use neighbour", 2, neighbour=True)]
                planned = mergecheck.Plan(repo, layer, [item], steps, "main", root / "worktree")
                failure = (sandbox.ConfinementUnavailable("fixture confinement unavailable") if infrastructure
                           else verify.ServerError("fixture product crashed PRIVATE_START_LOG"))
                patches = [mock.patch.object(paths, "RUNS_DIR", root / "runs"),
                           mock.patch.object(mergecheck, "loop_dir", return_value=root / "loop"),
                           mock.patch.object(mergecheck, "merged", return_value=[item]),
                           mock.patch.object(mergecheck, "plan", return_value=planned),
                           mock.patch.object(mergecheck, "deadline", nullcontext),
                           mock.patch.object(record, "start"), mock.patch.object(record, "settle"),
                           mock.patch.object(util, "git", return_value="pinned-sha"),
                           mock.patch.object(mergecheck.verifier_suites, "held", return_value=False),
                           mock.patch.object(scratch, "make", side_effect=OSError("fixture has no scratch")),
                           mock.patch.object(verify, "preflight", return_value=None),
                           mock.patch.object(verify, "serving", side_effect=failure),
                           mock.patch.object(pipeline, "untracked_by_dir", return_value={}),
                           mock.patch.object(pipeline, "restore_worktrees")]
                stdout = io.StringIO()
                with ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    stack.enter_context(mock.patch.object(sys, "stdout", stdout))
                    agent = stack.enter_context(mock.patch.object(runner, "run",
                                                                 side_effect=AssertionError("verifier started")))
                    if infrastructure:
                        with self.assertRaises(mergecheck.Blocked):
                            mergecheck.consume(cfg, "fixture", [item])
                    else:
                        self.assertEqual(mergecheck.consume(cfg, "fixture", [item]), 0)
                    agent.assert_not_called()
                checked = json.loads((root / "loop/.state/checked.json").read_text())["fixture"]
                if infrastructure:
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(checked, [])
                else:
                    finding, metric = [json.loads(line) for line in stdout.getvalue().splitlines()]
                    self.assertEqual(metric, {"value": 0})
                    self.assertEqual(checked, [item["key"]])
                    self.assertIn("Product startup failed", finding["summary"])
                    self.assertIn("landed step 1: New feature", finding["summary"])
                    self.assertIn("earlier step 2: Neighbour", finding["summary"])
                    self.assertNotIn("PRIVATE_START_LOG", json.dumps(finding))
