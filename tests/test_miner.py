import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import board, check_proposals, config, miner, util
from arete.store import approvals, insights
from tests import support


def setUpModule():
    support.isolate_live_state()


class EnforcementProposalTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = config.Repo(self._testMethodName, workspace, github=f"fixture/{self._testMethodName}")
        self.cfg = config.Config(repos=[self.repo])
        (workspace / "example.py").write_text('ready = status is "ready"\n')
        self.since = util.now().replace(microsecond=0)
        self.until = self.since + timedelta(hours=1)
        self.recent = self.feedback(22, self.since, "Compare readiness with equality again.")
        self.earlier = self.feedback(11, self.since - timedelta(days=2), "Use equality for string status values.")
        self.proposal = {
            "layer": "repo", "repo": self.repo.name, "file": "pyproject.toml",
            "change": "Treat Python's literal identity SyntaxWarning as an error in the compile check.",
            "why": "Python identifies an identity comparison against a string literal during compilation.",
            "evidence": [self.recent, self.earlier], "confidence": 0.95,
            "enforcement": {
                "kind": "check", "evaluator": check_proposals.PYTHON_SYNTAX_WARNINGS,
                "behavior": "Compare string status literals with object identity.",
                "cleanup": ["example.py"], "known_bad": 'ready = status is "ready"',
                "allowed": 'ready = status == "ready"', "diagnostic": 'Did you mean "=="?',
            },
        }

    def feedback(self, nid, when, body, repo=None):
        url = f"https://github.com/{self.repo.github}/pull/{nid}"
        stamp = util.iso(when)
        with mock.patch.object(insights.util, "iso", return_value=stamp):
            insights.add_feedback([{
                "pr_url": url, "kind": "line", "gh_id": nid, "repo": repo or self.repo.name,
                "issue": str(nid), "body": body, "created": stamp, "pr_state": "MERGED", "status": "done",
            }])
        return f"feedback {url} line {nid}"

    def mine(self, proposals, *, dry_run=False):
        result = SimpleNamespace(ok=True, structured={"proposals": proposals}, cost_usd=0, paid_usd=0, error="")
        with mock.patch.multiple(miner, collect=mock.Mock(return_value=[]), collect_runs=mock.Mock(return_value=([], 0)),
                                 sync_feedback=mock.Mock(), render_events=mock.Mock(return_value=""),
                                 apply_repo=mock.DEFAULT), \
                mock.patch.object(miner.auditor, "latest_findings", return_value="(none)"), \
                mock.patch.object(miner.record, "settle"), mock.patch.object(miner.runner, "run", return_value=result) as call:
            miner.apply_repo.return_value = "", []
            self.assertEqual(miner._run(self.cfg, self.id(), self.since, self.until, True, "", dry_run, True), 0)
            return call.call_args.args[0], miner.apply_repo.call_args_list

    def test_check_proposal_is_reviewable_and_never_enters_apply(self):
        rule = self.proposal["enforcement"]
        rule["allowed"] += '\nraise RuntimeError("fixture must never run")'
        prose = {"layer": "repo", "repo": self.repo.name, "file": "CLAUDE.md", "change": "Mention the owner review flow.",
                 "why": "Review depends on the ticket.", "evidence": [], "confidence": 0.9}
        prompt, apply = self.mine([self.proposal, prose])
        self.assertIn(self.recent, prompt)
        self.assertIn(self.earlier, prompt)
        self.assertEqual([call.args[3] for call in apply], [[prose]])

        report, = (self.repo.layer_dir / check_proposals.DIRECTORY).glob("*.md")
        text = report.read_text()
        for value in (rule["behavior"], self.proposal["why"], self.proposal["change"], "example.py", rule["known_bad"],
                      self.recent, self.earlier, "### Known-bad example rejected", "### Allowed example accepted"):
            self.assertIn(value, text)
        self.assertRegex(text, r"""### Observed diagnostic\n\n    "is" with (?:a|'str') literal\. Did you mean "=="\?""")
        for line in rule["allowed"].splitlines():
            self.assertIn(f"    {line}", text)

        with mock.patch.object(miner, "push_rule") as push:
            self.mine([self.proposal])
            rows = [row for row in board.proposals()["hand"] if row["repo"] == self.repo.name]
            self.assertEqual(len(rows), 1)
            self.assertEqual(self.repo.layer_dir / rows[0]["file"], report)
            with self.assertRaises(LookupError):
                miner.file_rule(rows[0]["id"])
            push.assert_not_called()
        self.assertFalse((self.repo.layer_dir / "config.toml").exists())
        self.assertFalse((self.repo.path / "pyproject.toml").exists())
        self.assertEqual((self.repo.path / "example.py").read_text().strip(), rule["known_bad"])
        self.assertTrue(approvals.forget_proposal(rows[0]["id"]))
        self.assertEqual(report.read_text(), text)

    def test_check_proposal_stays_out_of_review_when_both_examples_pass(self):
        rule = self.proposal["enforcement"]
        proposal = self.proposal | {"enforcement": rule | {"known_bad": 'ready = status != "ready"'}}
        _, apply = self.mine([proposal])
        self.assertEqual(apply, [])
        self.assertFalse(self.repo.layer_dir.exists())
        self.assertFalse(any(p["repo"] == self.repo.name for p in approvals.proposals()))

    def test_only_material_revisions_return_for_review(self):
        self.mine([self.proposal])
        first, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        self.assertTrue(approvals.forget_proposal(first["id"]))

        repeat = self.proposal | {"confidence": 0.99, "evidence": [self.earlier, self.recent]}
        self.mine([repeat])
        self.assertFalse(any(p["repo"] == self.repo.name for p in board.proposals()["hand"]))

        update = self.proposal | {"change": "Extend the existing compile gate to treat SyntaxWarning as an error."}
        self.mine([update])
        second, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        self.assertNotEqual(second["file"], first["file"])
        self.assertIn(update["change"], (self.repo.layer_dir / second["file"]).read_text())
        self.assertEqual(approvals.proposal(first["id"])["status"], "filed")

        self.mine([self.proposal])
        current, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        self.assertEqual(current["id"], second["id"])

        self.mine([update | {"why": "The compiler reports literal identity at compile time."}])
        third, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        self.assertNotEqual(third["id"], second["id"])
        self.assertFalse(approvals.forget_proposal(second["id"]))

    def test_incomplete_or_misscoped_enforcement_does_not_fall_back_to_apply(self):
        foreign = self.feedback(33, self.since, "Compare status by equality.", repo="other")
        rule = self.proposal["enforcement"]
        proposals = [self.proposal | {"evidence": [self.recent, self.recent]},
                     self.proposal | {"evidence": [self.recent, foreign]},
                     self.proposal | {"enforcement": rule | {"diagnostic": ""}},
                     self.proposal | {"enforcement": rule | {"evaluator": "unsupported"}},
                     self.proposal | {"enforcement": rule | {"known_bad": "def :", "diagnostic": "invalid syntax"}},
                     self.proposal | {"enforcement": rule | {"allowed": 'other = status is "other"'}},
                     self.proposal | {"enforcement": rule | {"cleanup": ["../example.py"]}},
                     self.proposal | {"layer": "global"}]
        _, apply = self.mine(proposals)
        self.assertEqual(apply, [])
        self.assertFalse(self.repo.layer_dir.exists())
        self.assertFalse(any(p["repo"] == self.repo.name for p in approvals.proposals()))

    def test_architecture_proposal_needs_no_executable_examples_and_dry_run_only_previews(self):
        proposal = self.proposal | {
            "change": "Expose readiness as a boolean property.",
            "enforcement": {"kind": "architecture", "behavior": "Compare status text outside its API.", "cleanup": []},
        }
        _, apply = self.mine([proposal], dry_run=True)
        self.assertEqual(apply, [])
        self.assertFalse(self.repo.layer_dir.exists())
        self.assertFalse(any(p["repo"] == self.repo.name for p in approvals.proposals()))
        _, apply = self.mine([proposal])
        self.assertEqual(apply, [])
        report, = (self.repo.layer_dir / check_proposals.DIRECTORY).glob("*.md")
        self.assertIn("API or architecture", report.read_text())
