import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import isolate_live_state
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

from arete import (
    config,
    flows,
    gh,
    jev,
    paths,
    pipeline,
    push,
    release_promotion as promotion,
    release_proof as proof,
    review as review_mod,
    spec_delivery,
    tracker,
    verify as verify_mod,
    workspace,
)

def setUpModule():
    isolate_live_state()


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True, env=env, timeout=CHILD_HANG_TIMEOUT).strip()


def _init_repo_with_origin(root: Path, *, branches=("main",)):
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


def _serve_env(test: unittest.TestCase, state: dict):
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

    with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
        server = test.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), API))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    test.addCleanup(thread.join, 5)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}"


class DirectPushDelivery(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=paths.STATE_DIR))).resolve()
        self.enterContext(mock.patch.multiple(paths, CONFIG_DIR=self.root / "config", STATE_DIR=self.root / "state",
                                              WORKTREES_DIR=self.root / "worktrees", RUNS_DIR=self.root / "runs"))
        self.origin, self.checkout, self.base_sha = _init_repo_with_origin(self.root, branches=("main",))
        self.repo = config.Repo("personal", self.checkout, github="me/personal", pipeline=True,
                                target_branch="main", landing="direct-push", unattended_environment="production",
                                parts=[config.Part("", "me/personal")])
        self.cfg = config.Config(repos=[self.repo])
        self.enterContext(mock.patch.object(config, "load_required", return_value=self.cfg))
        self.enterContext(mock.patch.object(config, "load", return_value=self.cfg))
        self.enterContext(mock.patch("arete.cohorts.record_release"))
        self.prod_state = {"revision": self.base_sha, "response": lambda: {"status": "ok" if self.prod_good else "broken"}}
        self.prod_good = False
        self.prod_url = _serve_env(self, self.prod_state)
        self.policy = {
            "destination": "production",
            "production": self.prod_url,
            "path": "/health",
            "identity_path": "/.well-known/arete-release.json",
            "instructions": "Confirm /health returns ok after deploy.",
            "assertions": [{"field": "/status", "equals": "ok"}],
            "recovery": [{"field": "/status", "equals": "ok"}],
        }
        policy_file = self.root / "policy.json"
        policy_file.write_text(json.dumps(self.policy))
        proof.install(self.repo, "7", policy_file)

    def _make_build(self, repo, ticket_id="7", base="main", *, branch="arete/7-fix",
                    verified_ok=True, review_ok=True, checks_ok=True, is_spec=False):
        wt = self.root / f"wt-{ticket_id}-{branch.replace('/', '_')}"
        _git(self.checkout, "worktree", "add", "-q", "-b", branch, str(wt), "origin/main")
        self.addCleanup(lambda: subprocess.run(["git", "-C", str(self.checkout), "worktree", "remove", "--force", str(wt)],
                                               capture_output=True, timeout=CHILD_HANG_TIMEOUT))
        (wt / "feature.txt").write_text("shipped via direct push\n")
        _git(wt, "add", "feature.txt")
        _git(wt, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "feat: ship change (#7)")
        head = _git(wt, "rev-parse", "HEAD")
        tree = _git(wt, "rev-parse", f"{head}^{{tree}}")
        c = workspace.Checkout("", repo.github, self.checkout, wt, base)
        layer = config.RepoLayer(dir=self.checkout / ".agents", checks={"test": "true"})
        tk = tracker.GitHubTracker(repo)
        labels = ["agent", "spec"] if is_spec else ["agent"]
        ticket = tracker.Ticket(ticket_id, f"#{ticket_id}", "Ship direct change", body="Deliver directly",
                                labels=labels, spec_identity=f"github:{repo.github}#{ticket_id}" if is_spec else "")
        run_dir = self.root / "runs" / f"run-{ticket_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run = pipeline.Run(f"run-{ticket_id}", repo, run_dir, cfg=self.cfg, layer=layer, tracker=tk, tid=ticket_id,
                           writes_lines=False, spec_root=f"spec-{ticket_id}" if is_spec else "")
        b = pipeline.Build(cfg=self.cfg, run=run, ticket=ticket, ws=wt, checkouts=[c], layer=layer,
                           timeout=60, workspace="", attachments="", tools=[], deny=[], left_for_a_person="",
                           check_commands="")
        if checks_ok:
            b.checked = {"tree": tree, "commands": dict(layer.checks), "report": "ok"}
        verified = verify_mod.Verified(
            verdict="pass" if verified_ok else "fail",
            summary="verified",
            checks=[{"what": "returns ok", "how": "curl /health", "saw": "ok", "passed": verified_ok}],
            finished=verified_ok,
            behavior_reached=verified_ok,
            native_trees={"": tree} if verified_ok else {},
        )
        review = review_mod.Review(
            verdict="approve" if review_ok else "request_changes",
            summary="looks good",
            trees={"": tree} if review_ok else {},
        )
        return run, b, c, ticket, verified, review, head, tree

    def test_opted_in_direct_push_lands_without_pr_and_verifies_production_across_partial_and_failed_deploys(self):
        run, b, c, ticket, verified, review, head, tree = self._make_build(self.repo)
        job = flows.Job(cfg=self.cfg, run=run, ticket=ticket, ws=c.wt, branch="arete/7-fix",
                        checkouts=[c], layer=b.layer, built=b, verified=verified, review=review,
                        build_summary="Ship change")
        flow = SimpleNamespace(ctx=job)
        low_risk = jev.Risk(0.1, 0.05, {"blast": 0.1}, "")
        with (mock.patch.object(jev, "ready", return_value=True),
              mock.patch.object(jev, "risk", return_value=low_risk),
              mock.patch.object(gh, "pr_create", side_effect=AssertionError("PR must not be opened")),
              mock.patch.object(gh, "open_pr_url", return_value=""),
              mock.patch.object(flows.record, "note"),
              mock.patch.object(run, "enter")):
            pr_out = flows.pr(flow, str(c.wt))
            job.take("pr", pr_out, stored=False)
            pub_out = flows.publish(flow)

        self.assertTrue(pr_out.passed)
        self.assertEqual(pr_out.data["prs"], {})
        self.assertEqual(job.direct_push["me/personal"]["revision"], head)
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), head)
        self.assertEqual(pub_out.data["ending"], "done")
        self.assertIn("awaiting release verification", pub_out.data["summary"])
        self.assertEqual(proof.direct_push_revision(self.repo, "7", "main"), head)

        # Spec candidate + reached support direct-pushed targets without requiring a PR.
        done = {"criterion": "returns ok", "procedure": "GET /health", "environment": "production"}
        receipt = spec_delivery.candidate(job, done, "pol", "acc", {"": tree})
        self.assertTrue(receipt["targets"][0]["direct_push"])
        self.assertEqual(receipt["targets"][0]["pr"], "")
        with mock.patch.object(spec_delivery, "revision", return_value=(head, tree)):
            landed = spec_delivery.reached(receipt)
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0]["commit"], head)

        # Production release observation via release_proof.advance():
        tk = tracker.GitHubTracker(self.repo)
        open_ticket = tracker.Ticket("7", "#7", "Ship direct change", body="Deliver directly", labels=["agent"], open=True)
        closed = []

        def close_issue(slug, number, comment=""):
            closed.append((slug, number, comment))
            open_ticket.open = False

        with (mock.patch.object(tracker, "for_ticket", return_value=tk),
              mock.patch.object(tk, "get", side_effect=lambda tid: open_ticket),
              mock.patch.object(tk, "require_ordinary"),
              mock.patch.object(gh, "issue_close", side_effect=close_issue)):
            # 1) Partial deploy: production still serves old revision -> ticket stays open.
            proof.advance(self.cfg)
            self.assertEqual(closed, [])
            prog = proof.progress(self.repo, "7")
            self.assertEqual(prog["revision"], head)
            self.assertIsNone(prog["completion"])
            self.assertEqual(prog["last_observation"]["status"], "blocked")

            # 2) Failed deploy: production serves new revision but assertion fails -> ticket stays open even after restart.
            self.prod_state["revision"] = head
            self.prod_good = False
            proof.advance(self.cfg)
            proof.advance(self.cfg)
            self.assertEqual(closed, [])
            prog = proof.progress(self.repo, "7")
            self.assertIsNone(prog["completion"])
            self.assertEqual(prog["last_observation"]["status"], "failed")

            # 3) Verified deploy: production serves new revision and assertion passes -> ticket closes once.
            self.prod_good = True
            proof.advance(self.cfg)
            proof.advance(self.cfg)
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0][1], 7)
            self.assertEqual(proof.completion("personal", "7", head)["destination"], "production")

    def test_direct_push_strictly_refuses_pr_policy_branch_mismatch_unattended_ceiling_irreversible_risk_and_protection(self):
        pr_repo = config.Repo("team", self.checkout, github="org/team", pipeline=True,
                              target_branch="staging", landing="pr", unattended_environment="staging")
        self.assertIn("requires a PR", pipeline.direct_push_refusal(pr_repo, "staging"))

        ceiling_repo = config.Repo("guarded", self.checkout, github="me/guarded", pipeline=True,
                                   target_branch="main", landing="direct-push", unattended_environment="none")
        self.assertIn("unattended deployment", pipeline.direct_push_refusal(ceiling_repo, "main"))
        self.assertIn("unattended deployment", push.policy_refusal(ceiling_repo, "main"))

        self.assertIn("targets main, not release", pipeline.direct_push_refusal(self.repo, "release"))

        # Irreversible action hold from JEV prevents direct push.
        _, b, c, _, verified, review, _, _ = self._make_build(self.repo, ticket_id="8", branch="arete/8-irrev")
        irrev = jev.Risk(0.2, 0.9, {"blast": 0.2}, "JEV reads the change as one a revert will not undo (p 0.90)")
        with (mock.patch.object(jev, "ready", return_value=True), mock.patch.object(jev, "risk", return_value=irrev),
              self.assertRaisesRegex(workspace.RunFailed, "revert will not undo")):
            pipeline.direct_push_checkout(b, c, verified, review, "drop table")
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), self.base_sha)

        # Missing local checks prevents direct push.
        _, b2, c2, _, verified2, review2, _, _ = self._make_build(self.repo, ticket_id="9", branch="arete/9-nocheck",
                                                                  checks_ok=False)
        with self.assertRaisesRegex(workspace.RunFailed, "local check"):
            pipeline.direct_push_checkout(b2, c2, verified2, review2, "no checks")
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), self.base_sha)

        # Remote branch protection rejecting direct pushes leaves origin unchanged and raises cleanly.
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'protected branch: PR required' >&2\nexit 1\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        _, b3, c3, _, verified3, review3, _, _ = self._make_build(self.repo, ticket_id="10", branch="arete/10-prot")
        low_risk = jev.Risk(0.1, 0.05, {"blast": 0.1}, "")
        with (mock.patch.object(jev, "ready", return_value=True), mock.patch.object(jev, "risk", return_value=low_risk),
              self.assertRaisesRegex(workspace.RunFailed, "refused by origin")):
            pipeline.direct_push_checkout(b3, c3, verified3, review3, "blocked by hook")
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), self.base_sha)


class _MultiTicketPromotionBase(unittest.TestCase):
    promotion_mode = "per-change"

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=paths.STATE_DIR))).resolve()
        self.enterContext(mock.patch.multiple(paths, CONFIG_DIR=self.root / "config", STATE_DIR=self.root / "state",
                                              WORKTREES_DIR=self.root / "worktrees"))
        self.origin, self.checkout, self.base_sha = _init_repo_with_origin(self.root, branches=("main", "staging"))
        self.repo = config.Repo("product", self.checkout, github="owner/product", pipeline=True,
                                target_branch="staging", landing="pr", unattended_environment="staging")
        self.cfg = config.Config(repos=[self.repo])
        self.enterContext(mock.patch.object(config, "load_required", return_value=self.cfg))
        self.enterContext(mock.patch("arete.cohorts.record_release"))
        self.tickets: dict[str, tracker.Ticket] = {}
        self.revisions: dict[str, str] = {}
        self.notes: dict[str, list[tuple]] = {}
        self.comment_seq = 10
        self.posts: list[str] = []
        self.tk = tracker.GitHubTracker(self.repo)
        self.enterContext(mock.patch.object(tracker, "for_ticket", return_value=self.tk))
        self.enterContext(mock.patch.object(self.tk, "get", side_effect=lambda tid: self.tickets[str(tid)]))
        self.enterContext(mock.patch.object(proof, "merge_revision",
                                            side_effect=lambda repo, tid, base: self.revisions.get(str(tid), "")))
        self.enterContext(mock.patch.object(gh, "merge_ci_passed", return_value=True))
        self.enterContext(mock.patch.object(promotion, "branch", side_effect=self._branch))
        self.enterContext(mock.patch.object(promotion, "parent", side_effect=self._parent))
        self.enterContext(mock.patch.object(promotion, "comments",
                                            side_effect=lambda repo, tid, tk: list(self.notes.get(str(tid), []))))
        self.enterContext(mock.patch.object(gh, "issue_comment", side_effect=self._handoff))
        self.enterContext(mock.patch.object(gh, "gh", side_effect=self._api_push))
        self.stage_state = {"revision": self.base_sha, "response": lambda: {"ok": True}}
        self.prod_state = {"revision": self.base_sha, "response": lambda: {"ok": True}}
        self.stage_url = _serve_env(self, self.stage_state)
        self.prod_url = _serve_env(self, self.prod_state)
        self.enterContext(mock.patch.object(promotion, "preview", return_value=self.stage_url))

    def _branch(self, repo, name):
        return _git(self.origin, "rev-parse", f"refs/heads/{name}")

    def _parent(self, repo, revision):
        parents = _git(self.origin, "rev-list", "--parents", "-n", "1", revision).split()[1:]
        proof.need(len(parents) == 1, "production requires an isolated single-parent change")
        return parents[0]

    def _handoff(self, slug, number, body, **kwargs):
        self.comment_seq += 1
        cid = str(self.comment_seq)
        self.notes.setdefault(str(number), []).append(
            ("tester", body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", cid)
        )
        return f"https://github.com/{slug}/issues/{number}#issuecomment-{cid}"

    def _api_push(self, args, **kwargs):
        self.assertIn("/git/refs/heads/main", args[1])
        sha = json.loads(kwargs["input"])["sha"]
        self.posts.append(sha)
        _git(self.origin, "update-ref", "refs/heads/main", sha)
        return ""

    def stage_ticket(self, tid: str, filename: str, content: str, *, body: str = "Change",
                     github_blockers=(), blocker: str = "") -> str:
        _git(self.checkout, "fetch", "-q", "origin", "staging")
        _git(self.checkout, "checkout", "-q", "-B", "staging", "origin/staging")
        (self.checkout / filename).write_text(content)
        _git(self.checkout, "add", filename)
        _git(self.checkout, "-c", "core.hooksPath=/dev/null", "commit", "-qm", f"feat: {tid} {filename}")
        sha = _git(self.checkout, "rev-parse", "HEAD")
        _git(self.checkout, "push", "-q", "origin", "HEAD:refs/heads/staging")
        self.revisions[tid] = sha
        self.tickets[tid] = tracker.Ticket(tid, f"#{tid}", f"Ticket {tid}", body=body, labels=["agent"],
                                           github_blockers=list(github_blockers), blocker=blocker)
        policy = {
            "destination": "staging",
            "staging": self.stage_url,
            "production": self.prod_url,
            "path": "/ok",
            "identity_path": "/.well-known/arete-release.json",
            "production_branch": "main",
            "promotion_mode": self.promotion_mode,
            "qa": ["tester"],
            "qa_require_revision": True,
            "instructions": f"Verify ticket {tid}.",
            "assertions": [{"field": "/ok", "equals": True}],
            "recovery": [{"field": "/ok", "equals": True}],
        }
        pfile = self.root / f"policy-{tid}.json"
        pfile.write_text(json.dumps(policy))
        proof.install(self.repo, tid, pfile)
        self.stage_state["revision"] = sha
        state = promotion.advance(self.cfg, self.repo, tid)
        self.assertEqual(state["phase"], "awaiting_qa", state)
        return sha

    def approve(self, tid: str):
        self.comment_seq += 1
        sha = self.revisions[tid]
        self.notes.setdefault(tid, []).append(
            ("tester", f"Promote {sha} to production", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", str(self.comment_seq))
        )


class PerChangeIsolatedPromotion(_MultiTicketPromotionBase):
    promotion_mode = "per-change"

    def test_independent_a_promotes_via_isolated_commit_while_unapproved_b_below_it_stays_in_qa(self):
        # Stage B first, then A on top of B: P0 -> sha_B -> sha_A.
        sha_b = self.stage_ticket("12", "b.txt", "change B\n")
        sha_a = self.stage_ticket("11", "a.txt", "change A\n")
        self.approve("11")

        # Promote A alone: must cherry-pick A onto P0 without carrying unapproved B.
        state_a = promotion.advance(self.cfg, self.repo, "11")
        self.assertEqual(state_a["phase"], "promotion_intent")
        self.assertTrue(state_a["isolated"])
        self.assertEqual(state_a["staged_revision"], sha_a)
        self.assertEqual(state_a["promoted_revisions"], [sha_a])
        isolated_a = state_a["revision"]
        self.assertNotEqual(isolated_a, sha_a)
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), isolated_a)
        self.assertEqual(self._parent(self.repo, isolated_a), self.base_sha)

        # Verify production tree contains A's file and NOT B's file!
        prod_files = set(_git(self.origin, "ls-tree", "--name-only", isolated_a).splitlines())
        self.assertIn("a.txt", prod_files)
        self.assertNotIn("b.txt", prod_files)

        # Deploy isolated A to production and verify completion binds to both staged and isolated revisions.
        self.prod_state["revision"] = isolated_a
        self.assertEqual(promotion.advance(self.cfg, self.repo, "11")["phase"], "production_verified")
        self.assertIsNotNone(proof.completion("product", "11", sha_a))
        self.assertIsNotNone(proof.completion("product", "11", isolated_a))

        # B stays staged in QA (unapproved).
        state_b = promotion.advance(self.cfg, self.repo, "12")
        self.assertEqual(state_b["phase"], "awaiting_qa")
        self.assertIsNone(proof.completion("product", "12", sha_b))

        # Later, approve B -> isolates B onto isolated_a -> production now has both A and B.
        self.approve("12")
        state_b = promotion.advance(self.cfg, self.repo, "12")
        self.assertEqual(state_b["phase"], "promotion_intent")
        isolated_b = state_b["revision"]
        self.assertEqual(self._parent(self.repo, isolated_b), isolated_a)
        prod_files_after = set(_git(self.origin, "ls-tree", "--name-only", isolated_b).splitlines())
        self.assertTrue({"a.txt", "b.txt"} <= prod_files_after)
        self.prod_state["revision"] = isolated_b
        self.assertEqual(promotion.advance(self.cfg, self.repo, "12")["phase"], "production_verified")

    def test_independent_a_below_b_fast_forwards_alone_while_b_stays_in_qa(self):
        # Stage A first, then B on top of A: P0 -> sha_A -> sha_B.
        sha_a = self.stage_ticket("11", "a.txt", "change A\n")
        sha_b = self.stage_ticket("12", "b.txt", "change B\n")
        self.approve("11")

        state_a = promotion.advance(self.cfg, self.repo, "11")
        self.assertEqual(state_a["phase"], "promotion_intent")
        self.assertEqual(state_a["revision"], sha_a)
        self.assertEqual(self.posts, [sha_a])
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), sha_a)
        self.prod_state["revision"] = sha_a
        self.assertEqual(promotion.advance(self.cfg, self.repo, "11")["phase"], "production_verified")
        self.assertEqual(promotion.advance(self.cfg, self.repo, "12")["phase"], "awaiting_qa")
        self.assertIsNone(proof.completion("product", "12", sha_b))


class WholeBranchApprovalAggregation(_MultiTicketPromotionBase):
    promotion_mode = "whole-branch"

    def test_whole_branch_waits_for_every_included_ticket_then_promotes_once_without_widening(self):
        sha_a = self.stage_ticket("11", "a.txt", "change A\n")
        sha_b = self.stage_ticket("12", "b.txt", "change B\n")
        self.approve("11")

        # Only A is approved: whole-branch promotion refuses and explicitly reports unapproved ticket 12.
        held = promotion.advance(self.cfg, self.repo, "11")
        self.assertEqual(held["phase"], "awaiting_qa")
        self.assertIn("unapproved tickets: 12", held["waiting"])
        self.assertEqual(self.posts, [])
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), self.base_sha)

        # Now approve B too -> single whole-branch promotion ships [sha_A, sha_B].
        self.approve("12")
        state_a = promotion.advance(self.cfg, self.repo, "11")
        self.assertEqual(state_a["phase"], "promotion_intent")
        self.assertEqual(state_a["revision"], sha_b)
        self.assertEqual(state_a["promoted_revisions"], [sha_a, sha_b])
        self.assertEqual(state_a["promoted_tickets"], ["11", "12"])
        self.assertEqual(self.posts, [sha_b])

        # Stage a NEW unapproved change C while A & B are reconciling production: must NEVER widen to C!
        sha_c = self.stage_ticket("13", "c.txt", "change C\n")
        self.prod_state["revision"] = sha_b
        self.assertEqual(promotion.advance(self.cfg, self.repo, "11")["phase"], "production_verified")
        # Ticket 12 reconciles against ticket 11's promotion with ZERO duplicate push!
        self.assertEqual(promotion.advance(self.cfg, self.repo, "12")["phase"], "production_verified")
        self.assertEqual(self.posts, [sha_b])
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), sha_b)
        self.assertIsNotNone(proof.completion("product", "11", sha_a))
        self.assertIsNotNone(proof.completion("product", "12", sha_b))
        self.assertEqual(promotion.advance(self.cfg, self.repo, "13")["phase"], "awaiting_qa")
        self.assertIsNone(proof.completion("product", "13", sha_c))

    def test_retry_after_unlanded_intent_refuses_to_silently_widen_when_staging_moves(self):
        sha_a = self.stage_ticket("11", "a.txt", "change A\n")
        sha_b = self.stage_ticket("12", "b.txt", "change B\n")
        self.approve("11")
        self.approve("12")

        # Simulate push failing before updating main after promotion_intent was written.
        with mock.patch.object(gh, "gh", side_effect=gh.GhUnsure("dropped before landing")):
            state_a = promotion.advance(self.cfg, self.repo, "11")
        self.assertEqual(state_a["phase"], "promotion_intent")
        self.assertEqual(_git(self.origin, "rev-parse", "refs/heads/main"), self.base_sha)

        # Unapproved C lands on staging before restart/retry.
        self.stage_ticket("13", "c.txt", "change C\n")
        retry = promotion.advance(self.cfg, self.repo, "11")
        self.assertEqual(retry["phase"], "promotion_intent")
        self.assertIn("silently widen", retry["waiting"])
        self.assertEqual(self.posts, [])
        self.assertEqual(retry["promoted_revisions"], [sha_a, sha_b])


class DependencyOrderedPromotion(_MultiTicketPromotionBase):
    promotion_mode = "per-change"

    def test_dependent_b_approved_first_waits_for_a_approval_and_deployment_while_independent_c_ships(self):
        sha_a = self.stage_ticket("11", "a.txt", "prereq A\n")
        sha_b = self.stage_ticket("12", "b.txt", "dependent B\n",
                                  body="Depends on prereq.\nWaits on #11",
                                  github_blockers=[11],
                                  blocker="it waits on #11, which is still open")
        sha_c = self.stage_ticket("13", "c.txt", "independent C\n")

        # Approve B and C first while A is still unapproved.
        self.approve("12")
        self.approve("13")

        # B MUST wait for A's approval.
        held_b = promotion.advance(self.cfg, self.repo, "12")
        self.assertEqual(held_b["phase"], "awaiting_qa")
        self.assertIn("prerequisite 11", held_b["waiting"])

        # Independent C remains eligible and ships to production!
        state_c = promotion.advance(self.cfg, self.repo, "13")
        self.assertEqual(state_c["phase"], "promotion_intent")
        iso_c = state_c["revision"]
        self.prod_state["revision"] = iso_c
        self.assertEqual(promotion.advance(self.cfg, self.repo, "13")["phase"], "production_verified")
        self.assertIsNotNone(proof.completion("product", "13", sha_c))

        # Now approve A -> A promotes first.
        self.approve("11")
        state_a = promotion.advance(self.cfg, self.repo, "11")
        self.assertEqual(state_a["phase"], "promotion_intent")
        iso_a = state_a["revision"]

        # While A is not yet verified live in production, B STILL waits!
        still_waiting_b = promotion.advance(self.cfg, self.repo, "12")
        self.assertEqual(still_waiting_b["phase"], "awaiting_qa")
        self.assertIn("prerequisite 11", still_waiting_b["waiting"])
        self.assertIn("deployed and verified", still_waiting_b["waiting"])

        # Deploy and verify A in production -> now B promotes in order A -> B!
        self.prod_state["revision"] = iso_a
        self.assertEqual(promotion.advance(self.cfg, self.repo, "11")["phase"], "production_verified")
        self.assertIsNotNone(proof.completion("product", "11", sha_a))

        state_b = promotion.advance(self.cfg, self.repo, "12")
        self.assertEqual(state_b["phase"], "promotion_intent")
        iso_b = state_b["revision"]
        self.assertEqual(self._parent(self.repo, iso_b), iso_a)
        self.prod_state["revision"] = iso_b
        self.assertEqual(promotion.advance(self.cfg, self.repo, "12")["phase"], "production_verified")
        self.assertIsNotNone(proof.completion("product", "12", sha_b))

    def test_whole_branch_live_dependency_releases_a_live_first_then_b(self):
        self.promotion_mode = "whole-branch"
        sha_a = self.stage_ticket("11", "a.txt", "migration A\n")
        sha_b = self.stage_ticket("12", "b.txt", "consumer B\n",
                                  body="Waits on #11 (live in production)",
                                  blocker="it waits on #11, which is still open")

        # Approve B first -> waits for A.
        self.approve("12")
        held_b = promotion.advance(self.cfg, self.repo, "12")
        self.assertEqual(held_b["phase"], "awaiting_qa")
        self.assertIn("unapproved tickets: 11", held_b["waiting"])

        # Approve A -> because B requires A live first, A ships alone first while B waits!
        self.approve("11")
        still_b = promotion.advance(self.cfg, self.repo, "12")
        self.assertEqual(still_b["phase"], "awaiting_qa")
        self.assertIn("prerequisite 11", still_b["waiting"])

        state_a = promotion.advance(self.cfg, self.repo, "11")
        self.assertEqual(state_a["phase"], "promotion_intent")
        self.assertEqual(state_a["revision"], sha_a)
        self.assertEqual(self.posts, [sha_a])

        # Verify A live in production -> now B promotes next!
        self.prod_state["revision"] = sha_a
        self.assertEqual(promotion.advance(self.cfg, self.repo, "11")["phase"], "production_verified")

        state_b = promotion.advance(self.cfg, self.repo, "12")
        self.assertEqual(state_b["phase"], "promotion_intent")
        self.assertEqual(state_b["revision"], sha_b)
        self.assertEqual(self.posts, [sha_a, sha_b])
        self.prod_state["revision"] = sha_b
        self.assertEqual(promotion.advance(self.cfg, self.repo, "12")["phase"], "production_verified")
