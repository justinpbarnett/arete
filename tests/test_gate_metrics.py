import json
import sqlite3
import unittest
from contextlib import nullcontext

from arete.store import gate_metrics
from arete.store.migrations import m0011_orchestration_state


class GateMetricsTests(unittest.TestCase):
    def test_counts_executed_gates_across_continuations_and_distinct_prs(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(m0011_orchestration_state.SQL)
        db.execute("CREATE TABLE runs(id TEXT, kind TEXT, dry_run INTEGER, pr_url TEXT, status TEXT)")
        prs = "https://github.com/example/a/pull/1 https://github.com/example/b/pull/1"
        for run, dry in (("first", 0), ("resumed", 0), ("other", 0), ("preview", 1)):
            db.execute("INSERT INTO runs VALUES (?, 'pipeline', ?, ?, 'pr')", (run, dry, prs))

        def action(second, op, result, *, run="resumed", request=None, root="first"):
            at = f"2026-01-01T00:00:{second:02d}Z"
            db.execute("INSERT INTO orchestration_actions VALUES (?,?,?,?,?,'succeeded',?,?,?)",
                       (root, str(second), str(second), run, op, json.dumps(request or {}), json.dumps(result), at))

        action(1, "checks.run", {"passed": False, "report": "### tests failed: test command"}, run="first")
        blocked = {"verdict": "blocked", "missing_capability": "browser.drag", "verification_environment": "browser",
                   "verification_requirements": ["Pan", "Zoom"], "missing_tools": ["drag", "wheel"],
                   "input_stamp": {"tree": "candidate", "policy": "policy", "acceptance": "ticket"}}
        action(2, "verification.run", blocked, run="first")
        action(5, "checks.run", {"passed": False, "scope": "failed", "checks_executed": ["tests"]})
        action(6, "checks.run", {"passed": True, "scope": "quick", "checks_executed": ["lint"]})
        action(9, "checks.run", {"passed": True, "scope": "failed", "checks_executed": ["tests"]})
        action(10, "verification.run", {**blocked, "verification_requirements": ["zoom", "pan"],
                                         "missing_tools": ["wheel", "drag"], "missing_capability": "wheel and drag"})
        action(11, "verification.run", {**blocked, "retry_blocked": True, "reused_verification": "10"})
        action(12, "verification.run", {**blocked, "verification_environment": "browser-with-drag"})
        action(13, "verification.run", {"verdict": "blocked", "blocked_by": "historical prose"})
        action(14, "checks.run", {"passed": True, "scope": "full", "checks_executed": ["lint", "tests"]})
        action(15, "checks.run", {"passed": True, "full": True, "report": "### tests passed: test command"})
        action(16, "checks.run", {"passed": True, "report": "### cached result", "reused_checks": "14"})
        action(17, "checks.run", {"passed": False, "scope": "full", "checks_executed": []})
        action(18, "checks.run", {"passed": False, "report": "### tests failed: test command"}, root="other", run="other")
        action(19, "checks.run", {"passed": True, "report": "### preview only"}, run="preview")
        action(20, "publication.request", {"ending": "pr"})
        action(21, "publication.request", {"ending": "pr"})
        metrics = gate_metrics.check_verification("2026-01-01T00:00:03Z", lambda: nullcontext(db))
        self.assertEqual(metrics, {
            "repeated_blocked_verifications": 1, "suppressed_blocked_retries": 1,
            "unclassified_blocked_verifications": 1, "full_suite_runs": 3, "published_prs": 2,
            "full_suite_runs_per_pr": 1.5, "median_repair_seconds": 8, "completed_repairs": 1,
            "open_check_failures": 1,
        })

    def test_empty_history_has_no_ratio_or_median(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        metrics = gate_metrics.check_verification("2026-01-01T00:00:00Z", lambda: nullcontext(db))
        self.assertEqual(metrics["published_prs"], 0)
        self.assertIsNone(metrics["full_suite_runs_per_pr"])
        self.assertIsNone(metrics["median_repair_seconds"])
