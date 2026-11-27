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
        self.scored = self.enterContext(mock.patch("arete.cohorts.record_release"))
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
        self.assertEqual({call.args[4] for call in self.scored.call_args_list}, {"staging", "production"})

    def standing(self):
        grant = {"id": "approved-policy-digest", "production_checks": {"app_id": 15368, "required": ["Verify"], "allow_skipped": []}}
        return self.enterContext(mock.patch.object(promotion.release_profile, "standing", return_value=grant))

    def test_standing_authority_promotes_after_proof_and_waits_for_the_production_build(self):
        standing = self.standing()
        checks = self.enterContext(mock.patch.object(promotion, "production_checks_passed", return_value=False))
        self.good = False
        self.assertEqual(self.advance()["phase"], "observing_staging")
        self.assertEqual(self.posts, [])
        self.good = True
        state = self.advance()
        self.assertEqual(state["phase"], "promotion_intent")
        self.assertEqual(state["standing_authority"], standing.return_value["id"])
        self.assertNotIn("approval", state)
        self.assertEqual(self.notes, [])
        self.running["production"] = self.new
        self.assertEqual(self.advance()["phase"], "observing_production")
        self.assertIsNone(proof.completion("product", "12", self.new))
        checks.return_value = True
        self.assertEqual(self.advance()["phase"], "production_verified")
        self.assertEqual(self.advance()["phase"], "production_verified")
        self.assertEqual(self.posts, [self.new])
        self.assertEqual(proof.completion("product", "12", self.new)["destination"], "production")

    def test_revocation_during_recheck_stops_standing_push_even_after_recording_intent(self):
        standing = self.standing()
        grant = standing.return_value
        standing.side_effect = (grant, grant, grant, None)
        state = self.advance()
        self.assertEqual(state["phase"], "promotion_intent")
        self.assertIn("authorization changed", state["waiting"])
        standing.side_effect = None
        self.assertEqual(self.posts, [])
        grant = {"id": "replacement", "production_checks": {}}
        standing.return_value = grant
        self.assertIn("authorization changed", self.advance()["waiting"])
        self.assertEqual(self.posts, [])

    def test_staging_only_spec_stops_at_its_verified_destination(self):
        self.standing()
        with mock.patch.object(promotion.specs, "find", return_value={"state": "published", "data": {
                "definition": {"environment": "staging"}}}):
            self.assertEqual(self.advance()["phase"], "staging_only")
            self.assertEqual(self.advance()["phase"], "staging_only")
        self.assertIsNotNone(proof.evidence("product", "12", self.new))
        self.assertEqual(self.posts, [])

    def test_standing_parent_waits_for_the_last_childs_own_staging_result(self):
        self.standing()
        children = {"first": ("21", "101"), "last": ("22", "102")}
        graph = {"commit": self.new, "children": [self.other, self.new]}
        self.enterContext(mock.patch("arete.spec_graph_delivery.promotion_target", return_value=graph))
        self.enterContext(mock.patch.object(promotion.specgraphs, "children", return_value=children))
        parents = {self.new: self.other, self.other: self.old}
        self.enterContext(mock.patch.object(promotion, "parent", side_effect=lambda repo, revision: parents[revision]))

        def stage_child(ticket, revision, good):
            proof.install(self.repo, ticket, proof.policy_file("product", "12"))
            self.running["staging"] = revision
            self.good = good
            receipt = promotion.observe_staging(self.repo, ticket, revision)
            self.assertEqual(receipt["status"], "verified" if good else "failed")
            if good:
                promotion.serve_guard.write(proof.state_dir("product", ticket) / "promotion.json", {
                    "phase": "staged_for_parent", "graph_parent": "12", "revision": revision,
                    "staging_receipt": receipt["receipt_id"]})

        stage_child("21", self.other, True)
        self.running["staging"] = self.new
        self.assertIn("each graph child", self.advance()["waiting"])
        stage_child("22", self.new, False)
        self.good = True  # The parent's integrated scenario passes independently of child 22's own check.
        self.assertIn("each graph child", self.advance()["waiting"])
        self.assertEqual(self.posts, [])
        stage_child("22", self.new, True)
        state = self.advance()
        self.assertEqual(state["phase"], "promotion_intent")
        self.assertCountEqual(state["graph_revisions"], [self.other, self.new])
        self.assertEqual(self.posts, [self.new])

    def test_graph_child_keeps_staging_receipt_and_only_verifies_the_parent_in_production(self):
        self.standing()
        self.enterContext(mock.patch.object(promotion, "graph_parent", return_value="10"))
        self.assertEqual(self.advance()["phase"], "staged_for_parent")
        self.assertIsNotNone(proof.evidence("product", "12", self.new))
        self.assertIn("waiting for the parent", self.advance()["waiting"])
        parent_file = proof.state_dir("product", "10") / "promotion.json"
        promotion.serve_guard.write(parent_file, {"revision": self.other, "graph_revisions": [self.new]})
        real_completion = proof.completion
        def parent_completion(repo, ticket, revision):
            if ticket == "10" and revision == self.other:
                return {"status": "verified"}
            return real_completion(repo, ticket, revision)

        self.refs["main"] = self.running["production"] = self.other
        with (mock.patch.object(proof, "completion", side_effect=parent_completion),
              mock.patch.object(promotion, "production_checks_passed", return_value=True)):
            state = self.advance()
        self.assertEqual((state["phase"], state["staged_revision"], state["revision"]),
                         ("production_verified", self.new, self.other))
        self.assertEqual(self.posts, [])
        self.assertEqual(real_completion("product", "12", self.other)["ticket"], "12")
        self.assertEqual(real_completion("product", "12", self.new)["revision"], self.other)

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


    def test_production_checks_require_the_matching_push_and_complete_pinned_jobs(self):
        repo = config.Repo("product", Path("/dev/null"), github="owner/product")
        revision, suite = "a" * 40, 1234
        state = {"revision": revision, "production_branch": "main",
                 "production_checks": {"app_id": 15368, "required": ["Verify", "Pages"], "allow_skipped": ["Worker"]}}
        run = {"head_sha": revision, "head_branch": "main", "event": "push", "status": "completed",
               "conclusion": "success", "check_suite_id": suite}
        check = {"head_sha": revision, "check_suite": {"id": suite}, "name": "Verify", "status": "completed",
                 "conclusion": "success", "app": {"id": 15368}}
        pages = {**check, "name": "Pages"}
        worker = {**check, "name": "Worker", "conclusion": "skipped"}
        def passes(runs, jobs):
            with mock.patch.object(gh, "actions_rows", side_effect=[runs, jobs]):
                return promotion.production_checks_passed(repo, state)
        self.assertTrue(passes([run], [check, pages, worker]))
        self.assertTrue(passes([run], [check, pages, {**worker, "conclusion": "success"}]))
        for delta in ({"head_branch": "staging"}, {"event": "pull_request"}, {"head_sha": "b" * 40},
                      {"status": "in_progress"}, {"conclusion": "failure"}):
            self.assertFalse(passes([{**run, **delta}], [check, pages, worker]))
        for jobs in ([check, worker], [check, {**pages, "app": {"id": 1}}, worker],
                     [check, pages, {**worker, "conclusion": "failure"}],
                     [check, pages, {**worker, "check_suite": {"id": 5678}}]):
            self.assertFalse(passes([run], jobs))


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
