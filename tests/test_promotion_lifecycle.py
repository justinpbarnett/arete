import contextlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support import isolate_live_state
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

from arete import (
    config,
    gh,
    jira,
    paths,
    release_profile,
    release_promotion as promotion,
    release_proof as proof,
    spec_delivery,
    ticket_feedback as tf,
    tracker,
)
from arete.store import db as foundation
from arete.store import feedback as feedback_store

isolate_live_state()


def _serve_env(test: unittest.TestCase, state: dict) -> str:
    class API(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/.well-known/"):
                payload = {"revision": state["revision"]}
            else:
                payload = state["response"]()
            self.send_response(state.get("status", 200))
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = test.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), API))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test.addCleanup(thread.join, 5)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}"


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True, env=env, timeout=CHILD_HANG_TIMEOUT).strip()


def _init_repo(root: Path, branches=("main", "staging")):
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, timeout=CHILD_HANG_TIMEOUT)
    checkout = root / "repo"
    subprocess.run(["git", "init", "-q", "-b", branches[0], str(checkout)], check=True, timeout=CHILD_HANG_TIMEOUT)
    _git(checkout, "config", "user.name", "Arete Test")
    _git(checkout, "config", "user.email", "test@example.test")
    _git(checkout, "remote", "add", "origin", str(origin))
    (checkout / "base.txt").write_text("base\n")
    _git(checkout, "add", "base.txt")
    _git(checkout, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "initial")
    base_sha = _git(checkout, "rev-parse", "HEAD")
    for branch in branches:
        _git(checkout, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return origin, checkout, base_sha


class _IsolatedLifecycleCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="arete-promo-"))).resolve()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        state = self.root / "state"
        for name, value in {
            "STATE_DIR": state,
            "DB_FILE": state / "arete.db",
            "CONFIG_DIR": self.root / "config",
            "CONFIG_FILE": self.root / "config" / "config.toml",
            "RUNS_DIR": state / "runs",
            "WORKTREES_DIR": state / "worktrees",
            "LOCKS_DIR": state / "locks",
            "LOG_DIR": state / "logs",
        }.items():
            stack.enter_context(mock.patch.object(paths, name, value))
        stack.enter_context(mock.patch.object(foundation, "_CHECKED", set()))
        stack.enter_context(mock.patch("arete.cohorts.record_release"))
        paths.ensure_dirs()
        foundation.migrate()


class ContextualAuthorizedPromotionTests(_IsolatedLifecycleCase):
    def test_natural_language_production_instructions_vs_neutral_negative_conditional_and_questions(self):
        sha = "b" * 40
        for positive in (
            "push to prod",
            "ship it to production",
            "approved for production",
            "moving this to push to production on the sheet",
            "Moving this to push to production on the sheet.",
            "Looks good on staging, push to prod!",
            f"Promote {sha} to production",
            "promote to prod",
            "@arete go ahead and ship to production",
        ):
            matched, _ = tf.match_production_instruction(positive)
            self.assertTrue(matched, f"expected match for: {positive!r}")

        for ignored in (
            "looks good",
            "thanks",
            "thank you",
            "don't push to prod",
            "do not push to production",
            "not approved for production",
            "hold off on pushing to prod",
            "push to prod if tests pass",
            "push to prod once we fix the crash",
            "push to prod after we update the banner",
            "should we push to prod?",
            "should we push to prod",
            "can we push to prod",
            "push to prod, but please keep the filter first",
        ):
            matched, _ = tf.match_production_instruction(ignored)
            self.assertFalse(matched, f"expected non-match for: {ignored!r}")

    def test_github_allowlisted_comment_and_unauthorized_rejection(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github",
                           target_branch="staging", landing="pr", unattended_environment="staging",
                           commenters=["owner", "qa-lead"])
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        old, new = "a" * 40, "b" * 40
        refs = {"staging": new, "main": old}
        stage_state = {"revision": new, "response": lambda: {"ok": True}}
        prod_state = {"revision": old, "response": lambda: {"ok": True}}
        stage_url = _serve_env(self, stage_state)
        prod_url = _serve_env(self, prod_state)
        notes, pushes = [], []
        ticket = tracker.Ticket("12", "#12", "Keep filter", body="Keep filter", labels=["agent"])

        policy = {
            "destination": "staging", "staging": stage_url, "production": prod_url,
            "path": "/ok", "identity_path": "/.well-known/arete-release.json",
            "production_branch": "main", "qa": ["owner"], "instructions": "Verify filter.",
            "assertions": [{"field": "/ok", "equals": True}], "recovery": [{"field": "/ok", "equals": True}],
        }
        pfile = self.root / "policy.json"
        pfile.write_text(json.dumps(policy))
        proof.install(repo, "12", pfile)

        def handoff(slug, num, body, **kwargs):
            notes.append(("arete-bot", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "20"))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-20"

        def do_patch(args, **kwargs):
            sha = json.loads(kwargs["input"])["sha"]
            pushes.append(sha)
            refs["main"] = sha
            return ""

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=ticket),
              mock.patch.object(proof, "merge_revision", return_value=new),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda *a: list(notes)),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "gh", side_effect=do_patch)):
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "awaiting_qa")

            # 1. "looks good" alone keeps ticket in QA.
            notes.append(("qa-lead", "looks good", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "21"))
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "awaiting_qa")
            self.assertEqual(pushes, [])

            # 2. Unauthorized outsider cannot promote.
            notes.append(("outsider", "moving this to push to production on the sheet",
                          "2026-01-02T00:01:00Z", "2026-01-02T00:01:00Z", "22"))
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "awaiting_qa")
            self.assertEqual(pushes, [])

            # 3. Allowlisted GitHub commenter promotes with contextual phrasing without consulting any sheet.
            notes.append(("qa-lead", "moving this to push to production on the sheet",
                          "2026-01-02T00:02:00Z", "2026-01-02T00:02:00Z", "23"))
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "promotion_intent")
            self.assertEqual(pushes, [new])
            prod_state["revision"] = new
            verified = promotion.advance(cfg, repo, "12")
            self.assertEqual(verified["phase"], "production_verified")
            self.assertEqual(verified["production_revision"], new)

    def test_jira_team_comment_including_child_ticket_promotes_via_ticket_feedback(self):
        repo = config.Repo("teamapp", self.root, github="org/teamapp", pipeline=True, tracker="jira",
                           jira_project="TEAM", jira_label="repo-teamapp",
                           target_branch="staging", landing="pr", unattended_environment="staging")
        cfg = config.Config(repos=[repo])
        client = jira.Client("https://jira.example.test", "u", "p")
        tk = tracker.JiraTracker(repo, cfg.jira)
        old, new = "a" * 40, "b" * 40
        refs = {"staging": new, "main": old}
        stage_state = {"revision": new, "response": lambda: {"ok": True}}
        prod_state = {"revision": old, "response": lambda: {"ok": True}}
        stage_url = _serve_env(self, stage_state)
        prod_url = _serve_env(self, prod_state)
        raw_jira_comments, pushes = [], []
        child_ticket = tracker.Ticket("TEAM-42", "TEAM-42", "Child story", body="Child change", open=True,
                                      labels=["agent", "repo-teamapp"])

        policy = {
            "destination": "staging", "staging": stage_url, "production": prod_url,
            "path": "/ok", "identity_path": "/.well-known/arete-release.json",
            "production_branch": "main", "qa": ["qa-lead"], "instructions": "Verify child on staging.",
            "assertions": [{"field": "/ok", "equals": True}], "recovery": [{"field": "/ok", "equals": True}],
        }
        pfile = self.root / "jira-policy.json"
        pfile.write_text(json.dumps(policy))
        proof.install(repo, "TEAM-42", pfile)

        def jira_handoff(tid, body, **kwargs):
            raw_jira_comments.append({
                "id": "500", "author": {"accountId": "arete-svc", "displayName": "Arete"},
                "body": body, "created": "2026-01-01T00:00:00.000+0000", "updated": "2026-01-01T00:00:00.000+0000",
            })
            return client.comment_url(tid, "500")

        def do_patch(args, **kwargs):
            sha = json.loads(kwargs["input"])["sha"]
            pushes.append(sha)
            refs["main"] = sha
            return ""

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker.JiraTracker, "client", new_callable=mock.PropertyMock, return_value=client),
              mock.patch.object(client, "comments", side_effect=lambda tid: list(raw_jira_comments)),
              mock.patch.object(client, "comment", side_effect=jira_handoff),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=child_ticket),
              mock.patch.object(proof, "merge_revision", return_value=new),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(gh, "gh", side_effect=do_patch)):
            self.assertEqual(promotion.advance(cfg, repo, "TEAM-42")["phase"], "awaiting_qa")

            # Team Jira member qa-maya (not in explicit policy["qa"]) posts "looks good" -> remains in QA.
            raw_jira_comments.append({
                "id": "501", "author": {"accountId": "qa-maya", "accountType": "atlassian", "displayName": "Maya QA"},
                "body": "looks good", "created": "2026-01-02T00:00:00.000+0000", "updated": "2026-01-02T00:00:00.000+0000",
            })
            tf.consume_ticket(cfg, repo, "TEAM-42", tk=tk, ticket=child_ticket, since="")
            self.assertEqual(promotion.advance(cfg, repo, "TEAM-42")["phase"], "awaiting_qa")
            self.assertEqual(pushes, [])

            # qa-maya posts explicit production direction -> authorized via #988 Jira team authority and promotes child!
            raw_jira_comments.append({
                "id": "502", "author": {"accountId": "qa-maya", "accountType": "atlassian", "displayName": "Maya QA"},
                "body": "Moving this to push to production on the sheet.",
                "created": "2026-01-02T00:05:00.000+0000", "updated": "2026-01-02T00:05:00.000+0000",
            })
            outcomes = tf.consume_ticket(cfg, repo, "TEAM-42", tk=tk, ticket=child_ticket, since="")
            self.assertEqual((outcomes[-1].intent, outcomes[-1].route, outcomes[-1].acted),
                             ("production_instruction", "release", True))
            self.assertEqual(len(tf.production_instructions("teamapp", "TEAM-42")), 1)
            self.assertEqual(promotion.advance(cfg, repo, "TEAM-42")["phase"], "promotion_intent")
            self.assertEqual(pushes, [new])
            prod_state["revision"] = new
            verified = promotion.advance(cfg, repo, "TEAM-42")
            self.assertEqual(verified["phase"], "production_verified")
            self.assertEqual(verified["approval_comment"]["actor"], "qa-maya")


class ReleaseOwnerAndActivationGateTests(_IsolatedLifecycleCase):
    def test_release_owner_gate_blocks_irreversible_promotion_until_owner_approves(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github",
                           target_branch="staging", landing="pr", unattended_environment="staging")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        old, new = "a" * 40, "b" * 40
        refs = {"staging": new, "main": old}
        stage_state = {"revision": new, "response": lambda: {"ok": True}}
        prod_state = {"revision": old, "response": lambda: {"ok": True}}
        stage_url = _serve_env(self, stage_state)
        prod_url = _serve_env(self, prod_state)
        notes, pushes = [], []
        ticket = tracker.Ticket("15", "#15", "Drop legacy column", body="Irreversible migration", labels=["agent"])

        # Install repo-wide profile with release_owners and a per-ticket irreversible promise.
        profile_file = self.root / "profile.json"
        profile_file.write_text(json.dumps({
            "destination": "staging", "staging": stage_url, "production": prod_url,
            "identity_path": "/.well-known/arete-release.json", "production_branch": "main",
            "qa": ["tester"], "release_owners": ["rel-owner"], "require_release_owner": False,
        }))
        with mock.patch.object(config, "load_required", return_value=cfg):
            release_profile.install(repo, profile_file)
            promise_file = self.root / "promise.json"
            promise_file.write_text(json.dumps({
                "path": "/ok", "instructions": "Verify migration.", "irreversible": True,
                "assertions": [{"field": "/ok", "equals": True}], "recovery": [{"field": "/ok", "equals": True}],
            }))
            proof.install(repo, "15", promise_file)

        def handoff(slug, num, body, **kwargs):
            notes.append(("arete-bot", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "10"))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-10"

        def do_patch(args, **kwargs):
            sha = json.loads(kwargs["input"])["sha"]
            pushes.append(sha)
            refs["main"] = sha
            return ""

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=ticket),
              mock.patch.object(proof, "merge_revision", return_value=new),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda *a: list(notes)),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "gh", side_effect=do_patch)):
            self.assertEqual(promotion.advance(cfg, repo, "15")["phase"], "awaiting_qa")

            # Tester approves production, but irreversible action still awaits release-owner approval!
            notes.append(("tester", "push to prod", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "11"))
            held = promotion.advance(cfg, repo, "15")
            self.assertEqual(held["phase"], "awaiting_qa")
            self.assertIn("release-owner approval", held["waiting"])
            self.assertEqual(pushes, [])

            # Non-release-owner cannot satisfy release-owner gate.
            notes.append(("outsider", "Release approved.", "2026-01-02T00:01:00Z", "2026-01-02T00:01:00Z", "12"))
            self.assertEqual(promotion.advance(cfg, repo, "15")["phase"], "awaiting_qa")
            self.assertEqual(pushes, [])

            # Release owner signs off -> promotes cleanly and records release_owner_approval!
            notes.append(("rel-owner", "Release approved.", "2026-01-02T00:02:00Z", "2026-01-02T00:02:00Z", "13"))
            state = promotion.advance(cfg, repo, "15")
            self.assertEqual(state["phase"], "promotion_intent")
            self.assertEqual(state["release_owner_approval"]["actor"], "rel-owner")
            self.assertEqual(pushes, [new])
            prod_state["revision"] = new
            self.assertEqual(promotion.advance(cfg, repo, "15")["phase"], "production_verified")

    def test_feature_flag_and_audience_activation_must_be_verified_before_closure(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github",
                           target_branch="staging", landing="pr", unattended_environment="staging")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        old, new = "a" * 40, "b" * 40
        refs = {"staging": new, "main": old}
        stage_state = {"revision": new, "response": lambda: {"ok": True, "feature_flag": "beta-search", "activation": "beta-users"}}
        prod_payload = {"ok": True}
        prod_state = {"revision": old, "response": lambda: dict(prod_payload)}
        stage_url = _serve_env(self, stage_state)
        prod_url = _serve_env(self, prod_state)
        notes, closed = [], []
        ticket = tracker.Ticket(
            "16", "#16", "Gated search", open=True, labels=["agent"],
            body="Roll out gated search.\nFeature flag: beta-search\nAudience activation: beta-users",
        )

        # Policy declares base assertion only, while ticket/policy require activation evidence.
        policy = {
            "destination": "staging", "staging": stage_url, "production": prod_url,
            "path": "/flags", "identity_path": "/.well-known/arete-release.json",
            "production_branch": "main", "qa": ["tester"], "instructions": "Check gated search.",
            "activations": [{"kind": "feature_flag", "value": "beta-search"},
                            {"kind": "activation", "value": "beta-users"}],
            "assertions": [{"field": "/ok", "equals": True}],
            "recovery": [{"field": "/ok", "equals": True}],
        }
        pfile = self.root / "flag-policy.json"
        pfile.write_text(json.dumps(policy))
        proof.install(repo, "16", pfile)

        def handoff(slug, num, body, **kwargs):
            notes.append(("arete-bot", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "10"))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-10"

        def do_patch(args, **kwargs):
            refs["main"] = json.loads(kwargs["input"])["sha"]
            return ""

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=ticket),
              mock.patch.object(tk, "require_ordinary"),
              mock.patch.object(proof, "merge_revision", return_value=new),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda *a: list(notes)),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "issue_close", side_effect=lambda *a: closed.append(a)),
              mock.patch.object(gh, "gh", side_effect=do_patch)):
            self.assertEqual(promotion.advance(cfg, repo, "16")["phase"], "awaiting_qa")
            notes.append(("tester", "ship it to production", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "11"))
            self.assertEqual(promotion.advance(cfg, repo, "16")["phase"], "promotion_intent")

            # Production deploys new revision, but activation fields are not verified -> stays open!
            prod_state["revision"] = new
            waiting = promotion.advance(cfg, repo, "16")
            self.assertEqual(waiting["phase"], "observing_production")
            self.assertIn("activation", waiting["waiting"])
            self.assertIsNone(proof.completion("product", "16", new))
            proof.finish_verified(cfg, repo, "16", new)
            self.assertEqual(closed, [])

            # Now enroll policy asserting the feature flag + audience activation and verify production activation!
            policy_with_flags = {
                **policy,
                "assertions": [
                    {"field": "/ok", "equals": True},
                    {"field": "/feature_flag", "equals": "beta-search"},
                    {"field": "/activation", "equals": "beta-users"},
                ],
            }
            pfile.write_text(json.dumps(policy_with_flags))
            proof.install(repo, "16", pfile)
            # Re-bind promotion state policy digest to observe updated assertions on production.
            promo_file = proof.state_dir("product", "16") / "promotion.json"
            saved = promotion.serve_guard.read(promo_file)
            saved["policy"] = proof.digest(proof.goal("product", "16"))
            promotion.serve_guard.write(promo_file, saved)

            # Still missing activation in live response -> failed observation, stays open!
            self.assertEqual(promotion.advance(cfg, repo, "16")["phase"], "observing_production")
            self.assertEqual(closed, [])

            # Activate flag for beta-users -> production_verified + ticket closes!
            prod_payload.update(feature_flag="beta-search", activation="beta-users")
            done = promotion.advance(cfg, repo, "16")
            self.assertEqual(done["phase"], "production_verified")
            self.assertEqual(len(done["activations_verified"]), 2)
            receipt = proof.completion("product", "16", new)
            self.assertIsNotNone(receipt)
            proof.finish_verified(cfg, repo, "16", new, receipt)
            self.assertEqual(len(closed), 1)

            # Single-spec at_delivery also enforces activation evidence.
            claim = {
                "issue": "16", "root": "spec-16", "run": "run-16",
                "data": {"definition": {"environment": "production"}, "targets": [{"repo": "owner/product", "branch": "staging", "pr": "https://github.com/owner/product/pull/1", "head": new, "tree": new}]},
            }
            with mock.patch.object(spec_delivery, "reached", return_value=[{"repo": "owner/product", "branch": "staging", "commit": new, "pr": "https://github.com/owner/product/pull/1", "exact_tree": True}]):
                evidence = spec_delivery.at_delivery(claim, tk, ticket=ticket)
                self.assertEqual(evidence["method"], "protected release observation")
                self.assertEqual(len(evidence["activations"]), 2)


class ReleaseWorkflowAndReconciliationTests(_IsolatedLifecycleCase):
    def _setup_single_repo(self, workflow_cfg):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github",
                           target_branch="staging", landing="pr", unattended_environment="staging")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        old, new = "a" * 40, "b" * 40
        stage_state = {"revision": new, "response": lambda: {"ok": True}}
        prod_state = {"revision": old, "response": lambda: {"ok": True}}
        stage_url = _serve_env(self, stage_state)
        prod_url = _serve_env(self, prod_state)
        policy = {
            "destination": "staging", "staging": stage_url, "production": prod_url,
            "path": "/ok", "identity_path": "/.well-known/arete-release.json",
            "production_branch": "main", "qa": ["tester"], "instructions": "Verify feature.",
            "release_workflow": workflow_cfg,
            "assertions": [{"field": "/ok", "equals": True}], "recovery": [{"field": "/ok", "equals": True}],
        }
        pfile = self.root / "wf-policy.json"
        pfile.write_text(json.dumps(policy))
        proof.install(repo, "12", pfile)
        return repo, cfg, tk, old, new, stage_state, prod_state, stage_url, prod_url

    def test_release_pr_workflow_reconciles_lost_confirmation_and_observes_merged_revision(self):
        repo, cfg, tk, old, new, _, prod_state, stage_url, _ = self._setup_single_repo(
            {"kind": "release_pr", "branch_prefix": "release/prod"}
        )
        merged_sha = "c" * 40
        refs = {"staging": new, "main": old}
        notes, created_refs, created_prs = [], [], []
        pr_url = "https://github.com/owner/product/pull/99"
        pr_state = {"state": "OPEN", "mergeCommit": {}}
        ticket = tracker.Ticket("12", "#12", "Release PR", body="Feature", labels=["agent"])

        def handoff(slug, num, body, **kwargs):
            notes.append(("arete-bot", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "20"))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-20"

        def create_ref(args, **kwargs):
            payload = json.loads(kwargs["input"])
            created_refs.append(payload)
            return ""

        def create_pr(slug, head, base, title, body, **kwargs):
            created_prs.append((head, base))
            raise gh.GhUnsure("lost PR creation response")

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=ticket),
              mock.patch.object(proof, "merge_revision", return_value=new),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda *a: list(notes)),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "gh", side_effect=create_ref),
              mock.patch.object(gh, "pr_create", side_effect=create_pr),
              mock.patch.object(gh, "open_pr_url", side_effect=lambda s, h: pr_url if created_prs else ""),
              mock.patch.object(gh, "find_pr_marker", return_value=""),
              mock.patch.object(gh, "pr_view", side_effect=lambda url: dict(pr_state))):
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "awaiting_qa")
            notes.append(("tester", "approved for production", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "21"))
            notes.append(("tester", "push to prod", "2026-01-02T00:01:00Z", "2026-01-02T00:01:00Z", "22"))

            # First attempt creates PR once, loses confirmation -> stays in promotion_intent.
            s1 = promotion.advance(cfg, repo, "12")
            self.assertEqual(s1["phase"], "promotion_intent")
            self.assertEqual(len(created_prs), 1)
            self.assertEqual(created_refs[0]["ref"], f"refs/heads/release/prod/12-{new[:12]}")

            # Restart/replay reconciles existing PR without creating a second PR, waiting for merge.
            s2 = promotion.advance(cfg, repo, "12")
            self.assertEqual(s2["phase"], "promotion_intent")
            self.assertEqual(s2["release_pr"], pr_url)
            self.assertIn("waiting for release PR", s2["waiting"])
            self.assertEqual(len(created_prs), 1)

            # PR merges -> observes merged revision on production -> production_verified!
            pr_state.update(state="MERGED", mergeCommit={"oid": merged_sha})
            refs["main"] = merged_sha
            s3 = promotion.advance(cfg, repo, "12")
            self.assertEqual(s3["phase"], "observing_production")
            self.assertEqual(s3["revision"], merged_sha)

            prod_state["revision"] = merged_sha
            s4 = promotion.advance(cfg, repo, "12")
            self.assertEqual(s4["phase"], "production_verified")
            self.assertEqual(s4["release_ref"], pr_url)
            self.assertEqual(len(created_prs), 1)
            self.assertIsNotNone(proof.completion("product", "12", new))
            self.assertIsNotNone(proof.completion("product", "12", merged_sha))

    def test_release_tag_workflow_reconciles_lost_confirmation_and_failed_deploy_stays_open(self):
        repo, cfg, tk, old, new, _, prod_state, stage_url, _ = self._setup_single_repo(
            {"kind": "tag", "prefix": "v-prod-"}
        )
        refs = {"staging": new, "main": old}
        tags, notes = {}, []
        tag_calls = []
        ticket = tracker.Ticket("12", "#12", "Release tag", body="Feature", labels=["agent"])

        def handoff(slug, num, body, **kwargs):
            notes.append(("arete-bot", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "20"))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-20"

        def create_tag(args, **kwargs):
            payload = json.loads(kwargs["input"])
            tag_calls.append(payload)
            tags[payload["ref"].removeprefix("refs/tags/")] = payload["sha"]
            raise gh.GhUnsure("lost tag write confirmation")

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=ticket),
              mock.patch.object(proof, "merge_revision", return_value=new),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda *a: list(notes)),
              mock.patch.object(promotion, "tag_sha", side_effect=lambda r, name: tags.get(name, "")),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "gh", side_effect=create_tag)):
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "awaiting_qa")
            notes.append(("tester", "push to prod", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "21"))

            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "promotion_intent")
            self.assertEqual(len(tag_calls), 1)

            # Reconcile lost tag confirmation -> moves to observing_production without a second tag write!
            obs = promotion.advance(cfg, repo, "12")
            self.assertEqual(obs["phase"], "observing_production")
            self.assertEqual(obs["release_ref"], f"refs/tags/v-prod-12-{new[:12]}")
            self.assertEqual(len(tag_calls), 1)
            self.assertIsNone(proof.completion("product", "12", new))

            # Deploy tag to production -> production_verified!
            prod_state["revision"] = new
            done = promotion.advance(cfg, repo, "12")
            self.assertEqual(done["phase"], "production_verified")
            self.assertEqual(len(tag_calls), 1)

    def test_workflow_dispatch_reconciles_running_failed_and_succeeded_runs_once(self):
        repo, cfg, tk, old, new, _, prod_state, stage_url, _ = self._setup_single_repo(
            {"kind": "workflow_dispatch", "workflow": "deploy-prod.yml", "ref": "main", "inputs": {"env": "prod"}}
        )
        refs = {"staging": new, "main": old}
        dispatches, notes = [], []
        wf_runs = []
        ticket = tracker.Ticket("12", "#12", "Dispatch release", body="Feature", labels=["agent"])

        def handoff(slug, num, body, **kwargs):
            notes.append(("arete-bot", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "20"))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-20"

        def dispatch(args, **kwargs):
            dispatches.append((args[1], json.loads(kwargs["input"])))
            wf_runs.append({"id": 777, "head_branch": "main", "status": "in_progress", "conclusion": None,
                            "html_url": "https://github.com/owner/product/actions/runs/777"})
            raise gh.GhUnsure("lost dispatch response")

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=ticket),
              mock.patch.object(proof, "merge_revision", return_value=new),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda *a: list(notes)),
              mock.patch.object(gh, "actions_rows", side_effect=lambda *a, **k: list(wf_runs)),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "gh", side_effect=dispatch)):
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "awaiting_qa")
            notes.append(("tester", "ship it to production", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "21"))

            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "promotion_intent")
            self.assertEqual(len(dispatches), 1)
            self.assertIn("deploy-prod.yml/dispatches", dispatches[0][0])
            self.assertEqual(dispatches[0][1]["inputs"]["revision"], new)
            self.assertEqual(dispatches[0][1]["inputs"]["env"], "prod")

            # While running -> waits without second dispatch.
            running = promotion.advance(cfg, repo, "12")
            self.assertEqual(running["phase"], "promotion_intent")
            self.assertIn("waiting for release workflow", running["waiting"])
            self.assertEqual(len(dispatches), 1)

            # Failed deploy leaves spec open and never re-dispatches blindly.
            wf_runs[0].update(status="completed", conclusion="failure")
            failed = promotion.advance(cfg, repo, "12")
            self.assertEqual(failed["phase"], "promotion_intent")
            self.assertIn("failed", failed["waiting"])
            self.assertEqual(len(dispatches), 1)

            # Successful run + live production revision -> production_verified!
            wf_runs[0].update(status="completed", conclusion="success")
            prod_state["revision"] = new
            done = promotion.advance(cfg, repo, "12")
            self.assertEqual(done["phase"], "production_verified")
            self.assertEqual(done["release_ref"], "https://github.com/owner/product/actions/runs/777")
            self.assertEqual(len(dispatches), 1)


class PauseAndFreshQALifecycleTests(_IsolatedLifecycleCase):
    def test_behavior_change_pauses_queues_correction_and_requires_fresh_qa_while_thanks_preserves_approval(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github",
                           target_branch="staging", landing="pr", unattended_environment="staging",
                           commenters=["tester"])
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        old, rev1, rev2 = "a" * 40, "b" * 40, "c" * 40
        current_rev = [rev1]
        refs = {"staging": rev1, "main": old}
        stage_state = {"revision": rev1, "response": lambda: {"ok": True}}
        prod_state = {"revision": old, "response": lambda: {"ok": True}}
        stage_url = _serve_env(self, stage_state)
        prod_url = _serve_env(self, prod_state)
        notes, pushes = [], []
        seq = [10]
        ticket = tracker.Ticket("20", "#20", "Save sort order", body="Persist settings", open=True, labels=["agent"])

        policy = {
            "destination": "staging", "staging": stage_url, "production": prod_url,
            "path": "/ok", "identity_path": "/.well-known/arete-release.json",
            "production_branch": "main", "qa": ["tester"], "instructions": "Check reload.",
            "assertions": [{"field": "/ok", "equals": True}], "recovery": [{"field": "/ok", "equals": True}],
        }
        pfile = self.root / "pause-policy.json"
        pfile.write_text(json.dumps(policy))
        proof.install(repo, "20", pfile)

        def handoff(slug, num, body, **kwargs):
            seq[0] += 10
            cid = str(seq[0])
            notes.append(("arete-bot", body, f"2026-01-01T00:{seq[0]:02d}:00Z", f"2026-01-01T00:{seq[0]:02d}:00Z", cid))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-{cid}"

        def do_patch(args, **kwargs):
            sha = json.loads(kwargs["input"])["sha"]
            pushes.append(sha)
            refs["main"] = sha
            return ""

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=ticket),
              mock.patch.object(proof, "merge_revision", side_effect=lambda *a: current_rev[0]),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda *a: list(notes)),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "gh", side_effect=do_patch)):
            # 1. Stage rev1 and post QA handoff #20.
            self.assertEqual(promotion.advance(cfg, repo, "20")["phase"], "awaiting_qa")

            # 2. QA approves rev1, then a behavior-changing correction arrives before promotion!
            notes.append(("tester", "push to prod", "2026-01-02T00:01:00Z", "2026-01-02T00:01:00Z", "21"))
            notes.append(("tester", "Actually, please also preserve the sort order on reload.",
                          "2026-01-02T00:02:00Z", "2026-01-02T00:02:00Z", "22"))

            paused = promotion.advance(cfg, repo, "20")
            self.assertEqual(paused["phase"], "paused")
            self.assertEqual(paused["paused_revision"], rev1)
            self.assertEqual(pushes, [])
            # Worker correction was automatically queued via ticket_feedback!
            self.assertEqual(feedback_store.tickets_needing_worker("product"), ["20"])
            self.assertEqual(len(tf.pending_corrections("product", "20")), 1)

            # Replaying old approval while still on rev1 stays paused.
            notes.append(("tester", "push to prod", "2026-01-02T00:03:00Z", "2026-01-02T00:03:00Z", "23"))
            self.assertEqual(promotion.advance(cfg, repo, "20")["phase"], "paused")
            self.assertEqual(pushes, [])

            # 3. Worker claims feedback, lands rev2 on staging, and settles comments.
            claimed = feedback_store.claim_worker_comments("product", "20", "run-fix-1")
            self.assertEqual(len(claimed), 1)
            feedback_store.settle_worker_comments("run-fix-1", "applied")
            current_rev[0] = rev2
            refs["staging"] = rev2
            stage_state["revision"] = rev2

            # Fresh QA handoff #30 posted for rev2; production approval is pending (old approval cannot promote rev2).
            fresh_qa = promotion.advance(cfg, repo, "20")
            self.assertEqual(fresh_qa["phase"], "awaiting_qa")
            self.assertEqual(fresh_qa["revision"], rev2)
            self.assertEqual(promotion.advance(cfg, repo, "20")["phase"], "awaiting_qa")
            self.assertEqual(pushes, [])

            # 4. Fresh instruction on rev2 + subsequent thank-you preserves approval and ships rev2!
            notes.append(("tester", "ship it to production", "2026-01-02T01:00:00Z", "2026-01-02T01:00:00Z", "31"))
            notes.append(("tester", "thank you!", "2026-01-02T01:01:00Z", "2026-01-02T01:01:00Z", "32"))
            self.assertEqual(promotion.advance(cfg, repo, "20")["phase"], "promotion_intent")
            self.assertEqual(pushes, [rev2])
            prod_state["revision"] = rev2
            self.assertEqual(promotion.advance(cfg, repo, "20")["phase"], "production_verified")

    def test_late_events_reconcile_in_progress_release_first_then_route_post_delivery_to_follow_up(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github",
                           target_branch="staging", landing="pr", unattended_environment="staging",
                           commenters=["tester"])
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        old, new = "a" * 40, "b" * 40
        refs = {"staging": new, "main": old}
        stage_state = {"revision": new, "response": lambda: {"ok": True}}
        prod_state = {"revision": old, "response": lambda: {"ok": True}}
        stage_url = _serve_env(self, stage_state)
        prod_url = _serve_env(self, prod_state)
        notes, pushes = [], []
        ticket = tracker.Ticket("25", "#25", "Export button", body="Add export", open=True, labels=["agent"])

        policy = {
            "destination": "staging", "staging": stage_url, "production": prod_url,
            "path": "/ok", "identity_path": "/.well-known/arete-release.json",
            "production_branch": "main", "qa": ["tester"], "instructions": "Check export.",
            "assertions": [{"field": "/ok", "equals": True}], "recovery": [{"field": "/ok", "equals": True}],
        }
        pfile = self.root / "late-policy.json"
        pfile.write_text(json.dumps(policy))
        proof.install(repo, "25", pfile)

        def handoff(slug, num, body, **kwargs):
            notes.append(("arete-bot", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "20"))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-20"

        def do_patch(args, **kwargs):
            sha = json.loads(kwargs["input"])["sha"]
            pushes.append(sha)
            refs["main"] = sha
            raise gh.GhUnsure("lost confirmation")

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", return_value=ticket),
              mock.patch.object(proof, "merge_revision", return_value=new),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: refs[b]),
              mock.patch.object(promotion, "parent", return_value=old),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda *a: list(notes)),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "gh", side_effect=do_patch)):
            self.assertEqual(promotion.advance(cfg, repo, "25")["phase"], "awaiting_qa")
            notes.append(("tester", "push to prod", "2026-01-02T00:01:00Z", "2026-01-02T00:01:00Z", "21"))
            self.assertEqual(promotion.advance(cfg, repo, "25")["phase"], "promotion_intent")
            self.assertEqual(pushes, [new])

            # Late feedback + repeated approval arrive while promotion is ALREADY in flight!
            late_change = tf.ThreadComment(
                "22", "tester", "tester", "Please also add CSV export format.",
                "https://github.com/owner/product/issues/25#issuecomment-22", "2026-01-02T00:02:00Z",
            )
            repeated_approve = tf.ThreadComment(
                "23", "tester", "tester", "push to prod",
                "https://github.com/owner/product/issues/25#issuecomment-23", "2026-01-02T00:03:00Z",
            )
            mid_outcomes = tf.consume_ticket(cfg, repo, "25", tk=tk, ticket=ticket,
                                             comments=[late_change, repeated_approve], since="")
            self.assertEqual((mid_outcomes[0].route, mid_outcomes[0].status, mid_outcomes[0].acted),
                             ("reconcile_release", "deferred", False))
            self.assertEqual(feedback_store.tickets_needing_worker("product"), [])

            # Reconcile actual deployment outcome first (observing_production -> production_verified) with single promotion!
            self.assertEqual(promotion.advance(cfg, repo, "25")["phase"], "observing_production")
            prod_state["revision"] = new
            self.assertEqual(promotion.advance(cfg, repo, "25")["phase"], "production_verified")
            self.assertEqual(pushes, [new])

            # After confirmed delivery (even before issue closes, or after close), late actionable feedback routes to follow-up!
            post_thanks = tf.ThreadComment(
                "24", "tester", "tester", "Thanks for shipping!",
                "https://github.com/owner/product/issues/25#issuecomment-24", "2026-01-02T00:04:00Z",
            )
            post_outcomes = tf.consume_ticket(cfg, repo, "25", tk=tk, ticket=ticket,
                                              comments=[late_change, post_thanks], since="")
            self.assertEqual([(o.comment_id, o.route, o.status, o.acted) for o in post_outcomes], [
                ("22", "follow_up", "follow_up_pending", True),
                ("24", "none", "noop", False),
            ])
            self.assertEqual(feedback_store.tickets_needing_worker("product"), [])
            self.assertEqual([r["comment_id"] for r in tf.pending_follow_ups("product")], ["22"])
            # Delivered promotion record remains untouched and replay is idempotent!
            self.assertEqual(promotion.advance(cfg, repo, "25")["phase"], "production_verified")
            replay = tf.consume_ticket(cfg, repo, "25", tk=tk, ticket=ticket, comments=[late_change], since="")
            self.assertTrue(replay[0].replayed and not replay[0].acted)
            self.assertEqual(len(tf.pending_follow_ups("product")), 1)


class IndependentContinuationOnPauseTests(_IsolatedLifecycleCase):
    def test_pausing_b_in_per_change_mode_allows_independent_approved_a_to_promote(self):
        origin, checkout, base_sha = _init_repo(self.root)
        repo = config.Repo("product", checkout, github="owner/product", pipeline=True, tracker="github",
                           target_branch="staging", landing="pr", unattended_environment="staging",
                           commenters=["tester"])
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        stage_state = {"revision": base_sha, "response": lambda: {"ok": True}}
        prod_state = {"revision": base_sha, "response": lambda: {"ok": True}}
        stage_url = _serve_env(self, stage_state)
        prod_url = _serve_env(self, prod_state)
        tickets, revisions, notes = {}, {}, {}
        seq = [10]

        def stage_commit(tid, filename, content, *, existing_ticket=False):
            _git(checkout, "fetch", "-q", "origin", "staging")
            _git(checkout, "checkout", "-q", "-B", "staging", "origin/staging")
            (checkout / filename).write_text(content)
            _git(checkout, "add", filename)
            _git(checkout, "-c", "core.hooksPath=/dev/null", "commit", "-qm", f"feat: {tid} {filename}")
            sha = _git(checkout, "rev-parse", "HEAD")
            _git(checkout, "push", "-q", "origin", "HEAD:refs/heads/staging")
            revisions[tid] = sha
            if not existing_ticket:
                tickets[tid] = tracker.Ticket(tid, f"#{tid}", f"Ticket {tid}", body="Change", open=True, labels=["agent"])
                pfile = self.root / f"policy-{tid}.json"
                pfile.write_text(json.dumps({
                    "destination": "staging", "staging": stage_url, "production": prod_url,
                    "path": "/ok", "identity_path": "/.well-known/arete-release.json",
                    "production_branch": "main", "promotion_mode": "per-change",
                    "qa": ["tester"], "instructions": f"Verify {tid}.",
                    "assertions": [{"field": "/ok", "equals": True}], "recovery": [{"field": "/ok", "equals": True}],
                }))
                proof.install(repo, tid, pfile)
            stage_state["revision"] = sha
            return sha

        def handoff(slug, num, body, **kwargs):
            seq[0] += 1
            cid = str(seq[0])
            notes.setdefault(str(num), []).append(("arete-bot", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", cid))
            return f"https://github.com/{slug}/issues/{num}#issuecomment-{cid}"

        def api_patch(args, **kwargs):
            sha = json.loads(kwargs["input"])["sha"]
            _git(origin, "update-ref", "refs/heads/main", sha)
            return ""

        def parent_of(r, sha):
            parents = _git(origin, "rev-list", "--parents", "-n", "1", sha).split()[1:]
            proof.need(len(parents) == 1, "single parent required")
            return parents[0]

        with (mock.patch.object(config, "load_required", return_value=cfg),
              mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", side_effect=lambda tid: tickets[str(tid)]),
              mock.patch.object(proof, "merge_revision", side_effect=lambda r, tid, b: revisions.get(str(tid), "")),
              mock.patch.object(gh, "merge_ci_passed", return_value=True),
              mock.patch.object(promotion, "branch", side_effect=lambda r, b: _git(origin, "rev-parse", f"refs/heads/{b}")),
              mock.patch.object(promotion, "parent", side_effect=parent_of),
              mock.patch.object(promotion, "preview", return_value=stage_url),
              mock.patch.object(promotion, "comments", side_effect=lambda r, tid, t: list(notes.get(str(tid), []))),
              mock.patch.object(gh, "issue_comment", side_effect=handoff),
              mock.patch.object(gh, "gh", side_effect=api_patch)):
            # Stage B then A: P0 -> sha_B -> sha_A.
            stage_commit("12", "b.txt", "change B v1\n")
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "awaiting_qa")
            sha_a = stage_commit("11", "a.txt", "change A\n")
            self.assertEqual(promotion.advance(cfg, repo, "11")["phase"], "awaiting_qa")

            # Both A and B get QA approval, then a behavior-changing correction arrives on B!
            seq[0] += 1
            notes["11"].append(("tester", "push to prod", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", str(seq[0])))
            seq[0] += 1
            notes["12"].append(("tester", "push to prod", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", str(seq[0])))
            seq[0] += 1
            fix_cid = str(seq[0])
            fix_comment = tf.ThreadComment(
                fix_cid, "tester", "tester", "Please fix B to include footer.",
                f"https://github.com/owner/product/issues/12#issuecomment-{fix_cid}", "2026-01-02T00:01:00Z",
            )
            notes["12"].append(("tester", fix_comment.body, fix_comment.created, fix_comment.created, fix_cid))
            tf.consume_ticket(cfg, repo, "12", tk=tk, ticket=tickets["12"], comments=[fix_comment], since="")

            # B is paused and queued for worker revision; independent approved A STILL promotes cleanly!
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "paused")
            self.assertEqual(feedback_store.tickets_needing_worker("product"), ["12"])

            state_a = promotion.advance(cfg, repo, "11")
            self.assertEqual(state_a["phase"], "promotion_intent")
            self.assertTrue(state_a["isolated"])
            iso_a = state_a["revision"]
            prod_files = set(_git(origin, "ls-tree", "--name-only", iso_a).splitlines())
            self.assertIn("a.txt", prod_files)
            self.assertNotIn("b.txt", prod_files)
            prod_state["revision"] = iso_a
            self.assertEqual(promotion.advance(cfg, repo, "11")["phase"], "production_verified")
            self.assertIsNotNone(proof.completion("product", "11", sha_a))

            # B's worker finishes fix, stages rev2 (clean isolated commit), gets fresh QA -> promotes onto A's commit!
            feedback_store.claim_worker_comments("product", "12", "run-b-fix")
            feedback_store.settle_worker_comments("run-b-fix", "applied")
            sha_b2 = stage_commit("12", "b_v2.txt", "change B v2 with footer\n", existing_ticket=True)
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "awaiting_qa")
            seq[0] += 1
            notes["12"].append(("tester", "ship it to production", "2026-01-02T02:00:00Z", "2026-01-02T02:00:00Z", str(seq[0])))
            state_b = promotion.advance(cfg, repo, "12")
            self.assertEqual(state_b["phase"], "promotion_intent")
            iso_b = state_b["revision"]
            self.assertEqual(parent_of(repo, iso_b), iso_a)
            prod_state["revision"] = iso_b
            self.assertEqual(promotion.advance(cfg, repo, "12")["phase"], "production_verified")
            self.assertIsNotNone(proof.completion("product", "12", sha_b2))
