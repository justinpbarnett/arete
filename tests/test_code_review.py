import unittest

from arete import code_review


class FindingDecisionTests(unittest.TestCase):
    report = {"method": "code-review", "verdict": "pending", "summary": "Two findings.",
              "trees": {"": "reviewed"}, "findings": [
                  {"axis": "Standards", "id": "standards-1", "file": "writer.py", "finding": "Possible duplication."},
                  {"axis": "Spec", "id": "spec-1", "file": "writer.py", "finding": "The last row is omitted."}]}
    decisions = {"standards-1": {"status": "nonblocking", "reason": "The paths implement different policies."},
                 "spec-1": {"status": "fixed", "reason": "The last row now reaches the writer."}}

    def test_repair_and_nonblocking_reason_reach_the_delivery_report(self):
        final = code_review.disposition(self.report, self.decisions, {"": "repaired"})
        self.assertTrue(final.approved())
        self.assertEqual(final.trees, {"": "repaired"})
        self.assertEqual(final.asdict()["source_trees"], self.report["trees"])
        self.assertEqual(final.report()["decisions"], self.decisions)
        self.assertIn("The last row now reaches the writer.", final.markdown())
        self.assertEqual(self.report["verdict"], "pending")

    def test_every_finding_needs_a_reason_and_fixed_requires_an_edit(self):
        for decisions in ({}, {**self.decisions, "other": self.decisions["spec-1"]},
                          {**self.decisions, "spec-1": {"status": "nonblocking", "reason": ""}}, self.decisions):
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                code_review.disposition(self.report, decisions, self.report["trees"])

    def test_dismissing_every_finding_cannot_authorize_a_different_tree(self):
        notes = {key: {"status": "nonblocking", "reason": "Outside the ticket."} for key in self.decisions}
        self.assertTrue(code_review.disposition(self.report, notes, self.report["trees"]).approved())
        with self.assertRaises(ValueError):
            code_review.disposition(self.report, notes, {"": "other"})


class CodeReviewMergeTests(unittest.TestCase):
    def test_merge_requires_the_settled_tree_checks_and_any_required_behavior(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from arete import config, pipeline
        from arete.verify import Verified

        review = code_review.disposition(FindingDecisionTests.report, FindingDecisionTests.decisions, {"": "final"})
        layer = config.RepoLayer(dir=None, checks={"test": "run-tests"})
        build = SimpleNamespace(run=SimpleNamespace(code_review=True), layer=layer,
                                checked={"tree": "final", "commands": layer.checks})
        checkout = SimpleNamespace(prefix="", wt=None)
        with patch.object(pipeline, "git", return_value="final"):
            def gate(verified):
                return pipeline.merge_evidence_problem(build, checkout, verified, review, "commit")

            self.assertIsNone(gate(code_review.unverified()))
            self.assertEqual(gate(Verified("fail"))[0], "verify")
            build.checked["tree"] = "old"
            self.assertEqual(gate(code_review.unverified())[0], "checks")
            build.checked["tree"] = "final"
            layer.verify = config.Verify(probes={"required": "check"})
            self.assertEqual(gate(code_review.unverified())[0], "verify")
            layer.verify = None
            build.run.code_review = False
            self.assertEqual(gate(code_review.unverified())[0], "verify")
