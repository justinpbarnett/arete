import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support import isolate_live_state

from arete import config, gh, paths, release_promotion as promotion, release_proof as proof, tracker

isolate_live_state()


class StagingRelease(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=paths.STATE_DIR)))
        self.enterContext(mock.patch.multiple(paths, CONFIG_DIR=root / "config", STATE_DIR=root / "state"))
        self.repo = config.Repo("product", root, github="owner/product", pipeline=True, target_branch="staging",
                                landing="pr", unattended_environment="staging")
        self.cfg = config.Config(repos=[self.repo])
        self.enrollment = self.enterContext(mock.patch.object(config, "load_required", return_value=self.cfg))
        self.old, self.new, self.other = "a" * 40, "b" * 40, "c" * 40
        self.refs = {"staging": self.new, "main": self.old}
        self.running = {"staging": self.new, "production": self.old}
        self.posts, self.notes = [], []
        self.issue = tracker.Ticket("12", "#12", "Keep selection through reload", body="Preserve the selection",
                                     labels=["agent"])
        self.tk = tracker.GitHubTracker(self.repo)
        self.enterContext(mock.patch.object(tracker, "for_ticket", return_value=self.tk))
        self.enterContext(mock.patch.object(self.tk, "get", return_value=self.issue))
        self.enterContext(mock.patch.object(proof, "merge_revision", return_value=self.new))
        self.enterContext(mock.patch.object(gh, "merge_ci_passed", return_value=True))
        self.enterContext(mock.patch.object(promotion, "branch", side_effect=lambda repo, branch: self.refs[branch]))
        self.enterContext(mock.patch.object(promotion, "parent", return_value=self.old))
        self.enterContext(mock.patch.object(promotion, "comments", side_effect=lambda *args: self.notes))
        self.enterContext(mock.patch.object(gh, "find_issue_comment_marker", return_value=""))
        self.enterContext(mock.patch.object(gh, "issue_comment", side_effect=self.handoff))
        self.enterContext(mock.patch.object(gh, "gh", side_effect=self.push))
        self.good = True
        self.stage_url = self.serve("staging")
        self.prod_url = self.serve("production")
        self.enterContext(mock.patch.object(promotion, "preview", return_value=self.stage_url))
        script = root / "observer.py"
        script.write_text('import json, sys, urllib.request\n'
                          'v = json.load(sys.stdin)\n'
                          'with urllib.request.urlopen(v["origin"] + v["path"], timeout=3) as r: print(r.read().decode())\n')
        self.policy = {"destination": "staging", "staging": self.stage_url, "production": self.prod_url,
                       "path": "/feature", "identity_path": "/.well-known/arete-release.json",
                       "observer_sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "production_branch": "main",
                       "qa": ["tester"], "qa_require_revision": True, "instructions": "Open Settings. Check the selection survives reload.",
                       "assertions": [{"field": "/selection", "equals": "Sources"}],
                       "recovery": [{"field": "/service", "equals": "ready"}]}
        policy_file = root / "policy.json"
        policy_file.write_text(json.dumps(self.policy))
        proof.install(self.repo, "12", policy_file, script)

    def serve(self, env):
        test = self
        class API(BaseHTTPRequestHandler):
            def do_GET(self):
                value = ({"revision": test.running[env]} if self.path.startswith("/.well-known/") else
                         {"selection": "Sources" if test.good else "Body", "service": "ready"})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def log_message(self, *args):
                pass
        http = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), API))
        t = threading.Thread(target=http.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(http.shutdown)
        return f"http://127.0.0.1:{http.server_port}"

    def handoff(self, repo, ticket, body, **kwargs):
        self.notes.append(("tester", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "20"))
        return "https://github.com/owner/product/issues/12#issuecomment-20"

    def push(self, args, **kwargs):
        self.assertIn("/git/refs/heads/main", args[1])
        self.assertEqual(json.loads(kwargs["input"]), {"sha": self.new, "force": False})
        self.posts.append(self.new)
        self.refs["main"] = self.new
        raise gh.GhUnsure("lost response")

    def comment(self, who, body, number):
        self.notes.append((who, body, "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", str(number)))

    def advance(self):
        return promotion.advance(self.cfg, self.repo, "12")

    def test_one_verified_promotion_reconciles_uncertain_write_without_advancing_unreviewed_work(self):
        self.assertEqual(self.advance()["phase"], "awaiting_qa")
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(proof.evidence("product", "12", self.new)["destination"], "staging")
        self.assertIsNotNone(proof.for_delivery("product", "12", self.new, "staging"))
        self.assertIsNone(proof.for_delivery("product", "12", self.new, "production"))
        self.assertIsNone(proof.for_delivery("product", "12", self.new, "other"))
        self.comment("untrusted", "Promote " + self.new + " to production", 24)
        self.comment("tester", "looks good", 24)
        self.comment("tester", "Promote this to production", 25)
        self.assertEqual(self.advance()["phase"], "awaiting_qa")
        self.comment("tester", "Promote " + self.new + " to production", 26)
        self.comment("tester", "Promote " + self.new + " to production", 27)
        self.assertEqual(self.advance()["phase"], "promotion_intent")
        self.assertEqual(self.advance()["phase"], "observing_production")
        self.assertEqual(self.posts, [self.new])
        self.running["production"] = self.new
        self.assertEqual(self.advance()["phase"], "production_verified")
        self.assertEqual(self.advance()["phase"], "production_verified")
        self.assertEqual(self.posts, [self.new])
        self.assertEqual(proof.for_delivery("product", "12", self.new, self.prod_url)["destination"], "production")

    def test_stable_address_moving_during_graph_observation_invalidates_the_receipt(self):
        with mock.patch.object(proof, "deployed_revision", side_effect=[self.new, self.new, self.new, self.other]):
            self.assertEqual(promotion.observe_staging(self.repo, "12", self.new)["status"], "blocked")
        self.assertIsNone(proof.for_delivery("product", "12", self.new, "staging"))

    def test_lost_handoff_confirmation_reconciles_only_the_service_publishers_comment(self):
        def uncertain(*args, **kwargs):
            self.handoff(*args, **kwargs)
            raise gh.GhUnsure("unconfirmed comment")

        with mock.patch.object(gh, "issue_comment", side_effect=uncertain), mock.patch.object(gh, "me", return_value="tester"):
            self.assertEqual(self.advance()["phase"], "observing_staging")
            original = self.notes.pop()
            self.notes.append(("other", *original[1:]))
            self.assertIn("reconciliation", self.advance()["waiting"])
            self.notes.append(original)
            self.assertEqual(self.advance()["phase"], "awaiting_qa")
            self.assertEqual(len(self.notes), 2)

    def test_changed_scope_or_unapproved_parent_holds_production(self):
        self.assertEqual(self.advance()["phase"], "awaiting_qa")
        self.comment("tester", "Promote " + self.new + " to production", 26)
        self.refs["main"] = self.other
        self.assertIn("unapproved", self.advance()["waiting"])
        self.refs["main"] = self.old
        self.enrollment.return_value = config.Config()
        self.assertIn("enrollment", self.advance()["waiting"])
        self.enrollment.return_value = self.cfg
        self.issue.body = "Change the action"
        self.assertIn("changed", self.advance()["waiting"])
        self.assertEqual(self.posts, [])

    def test_broken_staging_has_no_qa_handoff(self):
        self.good = False
        self.assertEqual(self.advance()["phase"], "observing_staging")
        self.assertIsNone(proof.evidence("product", "12", self.new))
        self.assertEqual(self.notes, [])
        self.assertEqual(self.posts, [])

class GitHubDeployment(unittest.TestCase):
    def test_preview_uses_successful_exact_staging_deployment_and_project_origin(self):
        repo = config.Repo("product", Path("/dev/null"), github="owner/product")
        head = "b" * 40
        rows = [{"sha": "a"*40, "environment": "staging", "id": 1},
                {"sha": head, "environment": "production", "id": 2},
                {"sha": head, "environment": "staging", "id": 3}]
        policy = {"staging": "https://staging.product.com", "immutable_host_suffix": "product.com"}
        for status, origin, expected in (("success", "https://078e.product.com", "https://078e.product.com"),
                                         ("inactive", "https://078e.product.com", ""),
                                         ("success", policy["staging"], ""), ("success", "https://otherproduct.com", "")):
            with mock.patch.object(promotion, "pages", side_effect=[rows, [{"state": status, "environment_url": origin}]]) as read:
                self.assertEqual(promotion.preview(repo, policy, head), expected)
                self.assertIn("/deployments/3/statuses?", read.call_args.args[0])


class PinnedCI(unittest.TestCase):
    def test_only_explicitly_named_skipped_jobs_from_the_pinned_app_and_head_are_optional(self):
        head = "a" * 40
        gate = {"required": ["Verify"], "allow_skipped": ["Deploy"], "app_id": 15368}
        check = {"head_sha": head, "name": "Verify", "app": {"id": 15368}, "status": "completed", "conclusion": "success"}

        def run(rows, configured=True):
            with mock.patch.object(gh, "gh_json", side_effect=[[{"check_runs": rows}], [[]]]):
                return gh.merge_ci_passed("owner/product", head, stage_checks=gate if configured else None)

        deploy = {**check, "name": "Deploy", "conclusion": "skipped"}
        self.assertFalse(run([check, deploy], configured=False))
        self.assertTrue(run([check, deploy]))
        self.assertFalse(run([deploy]))
        for changed in ({"conclusion": "failure"}, {"conclusion": "cancelled"}, {"head_sha": "b"*40},
                        {"name": "unrelated"}, {"app": {"id": 1}}, {"status": "in_progress"}):
            self.assertFalse(run([check, {**deploy, **changed}]))
