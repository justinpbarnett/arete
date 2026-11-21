import shlex
import subprocess
import sys
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
            "change": check_proposals.ADOPTION_CHANGE,
            "why": "Python identifies an identity comparison against a string literal during compilation.",
            "evidence": [self.recent, self.earlier], "confidence": 0.95,
            "enforcement": {
                "kind": "check", "evaluator": check_proposals.PYTHON_SYNTAX_WARNINGS,
                "behavior": "Compare string status literals with object identity.",
                "cleanup": ["example.py"], "known_bad": 'ready = status is "ready"',
                "allowed": 'ready = status == "ready"', "diagnostic": 'Did you mean "=="?',
            },
        }

    def feedback(self, nid, when, body, *, github=None, url=None):
        url = url or f"https://github.com/{github or self.repo.github}/pull/{nid}"
        stamp = util.iso(when)
        with mock.patch.object(insights.util, "iso", return_value=stamp):
            insights.add_feedback([{
                "pr_url": url, "kind": "line", "gh_id": nid, "repo": self.repo.name,
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
        for example in ("known_bad", "allowed"):
            rule[example] += '\nraise RuntimeError("fixture must never run")'
        (self.repo.path / "example.py").write_text(rule["known_bad"])
        prose = {"layer": "repo", "repo": self.repo.name, "file": "CLAUDE.md", "change": "Mention the owner review flow.",
                 "why": "Review depends on the ticket.", "evidence": [], "confidence": 0.9}
        prompt, apply = self.mine([self.proposal, prose])
        self.assertIn(self.recent, prompt)
        self.assertIn(self.earlier, prompt)
        self.assertIn(check_proposals.ADOPTION_CHANGE, prompt)
        self.assertEqual([call.args[3] for call in apply], [[prose]])

        report, = (self.repo.layer_dir / check_proposals.DIRECTORY).glob("*.md")
        text = report.read_text()
        for value in (rule["behavior"], self.proposal["why"], self.proposal["change"], "example.py",
                      self.recent, self.earlier, "### Known-bad example rejected", "### Allowed example accepted",
                      "python -I -S -B -Werror::SyntaxWarning -c", "path/to/source.py < path/to/source.py"):
            self.assertIn(value, text)
        self.assertRegex(text, r"""### Observed diagnostic\n\n    "is" with (?:a|'str') literal\. Did you mean "=="\?""")
        for example in ("known_bad", "allowed"):
            for line in rule[example].splitlines():
                self.assertIn(f"    {line}", text)

        command, redirect, source = text.split("```sh\n", 1)[1].splitlines()[0].rpartition(" < ")
        self.assertEqual(redirect, " < ")
        self.assertEqual(shlex.split(command)[-1], source)
        args = [sys.executable, *shlex.split(command)[1:]]
        rejected = subprocess.run(args, input=rule["known_bad"], capture_output=True, text=True, timeout=5)
        accepted = subprocess.run(args, input=rule["allowed"], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(rule["diagnostic"], rejected.stderr)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

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

    def test_check_proposal_requires_the_verified_implementation_and_comparable_examples(self):
        rule = self.proposal["enforcement"]
        proposals = [self.proposal | {"change": "Set a runtime warnings filter at application startup."}]
        proposals.extend(self.proposal | {"enforcement": rule | fields} for fields in (
            {"allowed": "pass"},
            {"allowed": 'ready = status == "busy"'},
            {"allowed": 'ready = status != "ready"'},
            {"diagnostic": "literal"},
        ))
        _, apply = self.mine(proposals)
        self.assertEqual(apply, [])
        self.assertFalse(self.repo.layer_dir.exists())
        self.assertFalse(any(p["repo"] == self.repo.name for p in approvals.proposals()))

    def test_multipart_feedback_counts_only_distinct_comments_on_the_target_parts_prs(self):
        self.repo.parts = [config.Part("app/client", "Fixture/Client"), config.Part("app/server", "fixture/server")]
        self.repo.github = ""
        self.repo.tracker = "jira"
        target = self.repo.path / "app/client"
        target.mkdir(parents=True)
        (target / "example.py").write_text('ready = status is "ready"\n')
        client = self.feedback(31, self.since, "Use equality here.", github="fixture/client")
        duplicate = self.feedback(31, self.since, "Use equality here.", github="Fixture/Client")
        server = self.feedback(32, self.since, "Use equality here too.", github="fixture/server")
        other_client = self.feedback(33, self.since - timedelta(days=2), "Use equality.", github="fixture/client")
        lookalike = self.feedback(34, self.since, "Use equality.", url="https://example.com/fixture/client/pull/34")
        proposal = self.proposal | {
            "file": "app/client/pyproject.toml",
            "enforcement": self.proposal["enforcement"] | {"cleanup": ["app/client/example.py"]},
        }
        valid = proposal | {"evidence": [client, other_client]}
        pairs = ([client, server], [client, duplicate], [client, lookalike])
        invalid = [proposal | {"evidence": pair} for pair in pairs]
        invalid.append(valid | {"file": "app/client-extra/pyproject.toml"})
        self.mine(invalid)
        self.assertFalse(self.repo.layer_dir.exists())
        self.assertFalse(any(p["repo"] == self.repo.name for p in approvals.proposals()))

        self.repo.parts.append(config.Part("app", "fixture/parent"))
        self.mine([valid])
        self.assertFalse(self.repo.layer_dir.exists())
        self.repo.parts.pop()

        _, apply = self.mine([valid])
        self.assertEqual(apply, [])
        row, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        report = (self.repo.layer_dir / row["file"]).read_text()
        for value in (client, other_client, valid["file"]):
            self.assertIn(value, report)

    def test_rediscovery_preserves_owner_bytes_before_and_after_mark_done(self):
        self.mine([self.proposal])
        row, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        report = self.repo.layer_dir / row["file"]
        pending_edit = report.read_bytes() + b"\nOwner notes before review.\r\n"
        report.write_bytes(pending_edit)

        self.mine([self.proposal])
        self.assertEqual(report.read_bytes(), pending_edit)
        self.assertEqual(approvals.proposal(row["id"])["status"], "hand")
        self.assertTrue(approvals.forget_proposal(row["id"]))

        filed_edit = pending_edit + b"Owner notes after review.\r\n"
        report.write_bytes(filed_edit)
        self.mine([self.proposal])
        self.assertEqual(report.read_bytes(), filed_edit)
        self.assertEqual([p for p in approvals.proposals() if p["repo"] == self.repo.name],
                         [approvals.proposal(row["id"])])
        self.assertEqual(approvals.proposal(row["id"])["status"], "filed")
        self.assertFalse(any(p["repo"] == self.repo.name for p in board.proposals()["hand"]))

    def test_only_material_revisions_return_for_review(self):
        self.mine([self.proposal])
        first, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        self.assertTrue(approvals.forget_proposal(first["id"]))

        repeat = self.proposal | {"confidence": 0.99, "evidence": [self.earlier, self.recent]}
        self.mine([repeat])
        self.assertFalse(any(p["repo"] == self.repo.name for p in board.proposals()["hand"]))

        update = self.proposal | {"file": "checks/python.sh"}
        self.mine([update])
        second, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        self.assertNotEqual(second["file"], first["file"])
        self.assertIn(update["file"], (self.repo.layer_dir / second["file"]).read_text())
        self.assertEqual(approvals.proposal(first["id"])["status"], "filed")

        self.mine([self.proposal])
        current, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        self.assertEqual(current["id"], second["id"])

        self.mine([update | {"why": "The compiler reports literal identity at compile time."}])
        third, = [p for p in board.proposals()["hand"] if p["repo"] == self.repo.name]
        self.assertNotEqual(third["id"], second["id"])
        self.assertFalse(approvals.forget_proposal(second["id"]))

    def test_incomplete_or_misscoped_enforcement_does_not_fall_back_to_apply(self):
        foreign = self.feedback(33, self.since, "Compare status by equality.", github="fixture/other")
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
