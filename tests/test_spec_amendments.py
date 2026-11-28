import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support

from arete import (
    config,
    flows,
    gh,
    loops,
    orchestration,
    orchestration_api,
    requirement_revision,
    spec_amendments,
    spec_delivery,
    spec_graph,
    spec_graph_delivery,
    spec_tracker,
    tracker,
    util,
    verify,
    webhooks,
)
from arete.refusals import Stated
from arete.store import db, specgraphs, specs
from arete.store import orchestration as ledger
from tests.test_spec_graph_delivery import (
    GITHUB_SLUG,
    REPO_NAME,
    SHA_A,
    SHA_B,
    FakeGitHubSource,
    FakeRemote,
    add_run,
    make_item,
    make_parent_ticket,
    make_verified_pub,
    seed_graph,
)

setUpModule = support.isolate_live_state


class RequirementRevisionTests(unittest.TestCase):
    def test_criteria_unchanged_prior_answers_and_independent_workers(self):
        before = make_parent_ticket(
            "50",
            body="Overview\n- AC-1: API returns 200\n- AC-2: Banner is blue\n- AC-3: Audit logs events",
        )
        after = make_parent_ticket(
            "50",
            body=(
                "Overview\n"
                "- AC-1: API returns 200\n"
                "- AC-2: Banner is green (supersedes the earlier owner answer)\n"
                "- AC-3: Audit logs structured JSON events"
            ),
        )
        self.assertNotEqual(requirement_revision.version(before), requirement_revision.version(after))
        shared, named = requirement_revision.criteria(before.body)
        self.assertEqual(shared, ["Overview"])
        self.assertEqual(set(named), {"AC-1", "AC-2", "AC-3"})
        self.assertEqual(set(requirement_revision.unchanged_criteria(before, after)), {"AC-1"})

        notes = [
            "AC-1: Use GET /status",
            "AC-2: Use hex #0000ff",
            "AC-3: Emit plain text",
            "General note without criterion id",
        ]
        retained, retired, unresolved = requirement_revision.prior_answers(before, after, notes)
        self.assertEqual(retained, ["AC-1: Use GET /status"])
        self.assertEqual(retired, ["AC-2: Use hex #0000ff"])
        self.assertEqual(unresolved, ["AC-3: Emit plain text", "General note without criterion id"])

        workers = {
            "w1": {"data": {"effective": {"acceptance_check": "AC-1", "depends_on": []}}},
            "w2": {"data": {"effective": {"acceptance_check": "AC-2", "depends_on": []}}},
            "w3": {"data": {"effective": {"acceptance_check": "AC-1", "depends_on": ["w2"]}}},
        }
        self.assertEqual(requirement_revision.independent_workers(before, after, workers), {"w1"})


class SpecGraphProposalTests(unittest.TestCase):
    def test_proposal_validates_prerequisites_artifacts_cycles_and_declared_refs(self):
        direct = spec_graph.proposal({"mode": "direct", "reason": "Small change", "children": []}, {})
        self.assertEqual(direct["mode"], "direct")

        split = spec_graph.proposal(
            {
                "mode": "split",
                "reason": "Two slices",
                "environment": "staging",
                "children": [
                    {
                        "key": "pkg",
                        "title": "Publish package",
                        "outcome": "Package published",
                        "checks": ["AC-1: Package builds"],
                        "requires": [],
                        "existing": "",
                    },
                    {
                        "key": "app",
                        "title": "Consume package",
                        "outcome": "App works",
                        "checks": ["AC-2: App renders"],
                        "requires": ["pkg@artifact=@org/pkg:1.2.3"],
                        "existing": "",
                    },
                    {
                        "key": "ops",
                        "title": "Verify rollout",
                        "outcome": "Rollout verified",
                        "checks": ["AC-3: Health OK"],
                        "requires": ["app@release"],
                        "existing": "",
                    },
                ],
            },
            {},
        )
        by_key = {n["key"]: n for n in split["children"]}
        self.assertEqual(by_key["app"]["requires"], ["pkg"])
        self.assertEqual(by_key["app"]["artifacts"], {"pkg": {"name": "@org/pkg", "version": "1.2.3"}})
        self.assertEqual(by_key["ops"]["release_requires"], ["app"])

        # Cycle detection raises Held.
        with self.assertRaises(spec_tracker.Held):
            spec_graph.ordered({"a": {"b"}, "b": {"a"}})

        # declared_refs ignores fenced code blocks and HTML comments.
        remote = spec_tracker.GitHub(GITHUB_SLUG)
        parent = make_item(
            "60",
            "600",
            body=(
                "Child issues: #61, #62\n"
                "<!-- [ ] #99 -->\n"
                "```markdown\n"
                "- [ ] #98\n"
                "```\n"
                "- [ ] #63\n"
            ),
        )
        self.assertEqual(spec_graph.declared_refs(remote, parent), ("61", "62", "63"))

        adopted = {"61": make_item("61", "601", body="")}
        plan = spec_graph.proposal(
            {
                "mode": "split",
                "reason": "Adopt 61",
                "environment": "",
                "children": [
                    {
                        "key": "first",
                        "title": "First",
                        "outcome": "Outcome",
                        "checks": ["AC-1: Check"],
                        "requires": [],
                        "existing": "61",
                    }
                ],
            },
            adopted,
        )
        # Empty adopted issue body requires being named in add_contract.
        with self.assertRaises(spec_tracker.Held):
            spec_graph.reviewed({"approved": True, "reason": "ok", "add_contract": []}, plan, adopted)
        reviewed = spec_graph.reviewed({"approved": True, "reason": "ok", "add_contract": ["first"]}, plan, adopted)
        self.assertEqual(reviewed["add_contract"], ["first"])


class SpecDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(self.root))
        db.migrate()
        self.repo = config.Repo(REPO_NAME, self.root, github=GITHUB_SLUG, pipeline=True, landing="pr")

    def test_definition_behavior_exact_tree_and_confirmed_observation(self):
        done = spec_delivery.definition({"criterion": "Health returns 200", "procedure": "GET /health", "environment": ""})
        report = verify.Verified.stored({
            "verdict": "pass",
            "finished": True,
            "behavior_reached": True,
            "checks": [{"what": "Health returns 200", "how": "curl /health", "saw": "200 OK", "passed": True}],
            "failed_checks": [],
        })
        self.assertEqual(spec_delivery.behavior(report, done), {"how": "curl /health", "saw": "200 OK"})

        parent = make_parent_ticket("70")
        add_run("r-70", REPO_NAME, "70", "merged")
        specs.bind(parent.spec_identity, REPO_NAME, "70", "", lambda: "r-70")
        claim_data = {
            "definition": done,
            "source_format": 2,
            "source": spec_delivery.source_version(parent),
            "landing": [],
            "targets": [{"pr": f"https://github.com/{GITHUB_SLUG}/pull/70", "repo": GITHUB_SLUG, "base": "main",
                         "tree": SHA_A, "head": SHA_A}],
            "behavior": {"how": "curl /health", "saw": "200 OK"},
        }
        specs.advance(parent.spec_identity, "r-70", "building", "published", claim_data)
        claim = specs.find(REPO_NAME, "70")
        tk = FakeGitHubSource(self.repo, {"70": parent})
        pr_view = {"state": "MERGED", "mergedAt": "2026-11-27T00:00:00Z", "headRefOid": SHA_A,
                   "baseRefName": "main", "mergeCommit": {"oid": SHA_A}}

        with (mock.patch.object(gh, "pr_view", return_value=pr_view),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_A, SHA_A))):
            # Exact tree match without runtime requirement succeeds immediately.
            evidence = spec_delivery.at_delivery(claim, tk)
            self.assertEqual(evidence["method"], "verified candidate tree matches the landed tree")

        # Requiring an environment switches to publisher observation request + confirmation.
        claim["data"]["definition"]["environment"] = "staging"
        spec_delivery.save(claim)
        posted = []

        def fake_comment(slug, num, body, return_url=False, single_attempt=False):
            posted.append(body)
            return f"https://github.com/{slug}/issues/{num}#issuecomment-{len(posted)}"

        with (mock.patch.object(gh, "pr_view", return_value=pr_view),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_A, SHA_B)),
              mock.patch.object(gh, "issue_comment", side_effect=fake_comment),
              mock.patch.object(spec_delivery, "comments", return_value=[])):
            self.assertEqual(spec_delivery.at_delivery(claim, tk), {})
            self.assertEqual(len(posted), 1)
            # Second poll does not duplicate the observation request comment.
            self.assertEqual(spec_delivery.at_delivery(claim, tk), {})
            self.assertEqual(len(posted), 1)

        token = claim["data"]["observation_request"]["token"]
        reply = f"Spec evidence {token}\nHow: Checked staging\nSaw: 200 OK on staging"
        reply_url = f"https://github.com/{GITHUB_SLUG}/issues/70#issuecomment-55"
        reply_at = util.iso(util.parse_iso(claim["data"]["observation_request"]["since"]) + timedelta(seconds=10))
        with (mock.patch.object(gh, "pr_view", return_value=pr_view),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_A, SHA_B)),
              mock.patch.object(gh, "issue_comment", side_effect=fake_comment),
              mock.patch.object(spec_delivery, "comments", return_value=[(reply, reply_url, reply_at)]),
              mock.patch.object(spec_delivery, "confirmed_observation", return_value=True)):
            evidence = spec_delivery.at_delivery(claim, tk)
            self.assertEqual(evidence["method"], "publisher's delivery-point observation")
            self.assertEqual(evidence["source"], reply_url)


class SpecAmendmentsReconcileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(self.root))
        db.migrate()
        self.repo = config.Repo(REPO_NAME, self.root, github=GITHUB_SLUG, pipeline=True, landing="pr")

    def _seed_two_branches(self):
        body = "- AC-1: API returns v1 JSON\n- AC-2: UI renders card\n- AC-3: Docs list endpoint"
        parent_ticket = make_parent_ticket("80", body=body)
        parent_item = make_item("80", "800", title=parent_ticket.title, body=body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        plan_children = [
            {"key": "api", "title": "API", "outcome": "API ready", "checks": ["AC-1: API returns v1 JSON"],
             "requires": [], "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "ui", "title": "UI", "outcome": "UI ready", "checks": ["AC-2: UI renders card"],
             "requires": ["api"], "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "docs", "title": "Docs", "outcome": "Docs ready", "checks": ["AC-3: Docs list endpoint"],
             "requires": [], "release_requires": [], "artifacts": {}, "existing": ""},
        ]
        items = [
            make_item("81", "801", title="API"),
            make_item("82", "802", title="UI"),
            make_item("83", "803", title="Docs"),
        ]
        key = seed_graph(
            REPO_NAME, parent_ticket, parent_item, plan_children,
            [("api", "81", "801"), ("ui", "82", "802"), ("docs", "83", "803")],
            run_id="run-parent-80",
        )
        remote = FakeRemote(parent_item, items, blockers={"82": {"81"}})
        source = FakeGitHubSource(self.repo, {"80": parent_ticket})
        return key, remote, source

    def test_edit_open_child_keeps_unrelated_branch_moving_and_posts_idempotent_parent_summary(self):
        key, remote, source = self._seed_two_branches()
        edited_body = "- AC-1: API returns v2 JSON with cursor\n- AC-2: UI renders card\n- AC-3: Docs list endpoint"
        edited_ticket = make_parent_ticket("80", body=edited_body)
        edited_item = make_item("80", "800", title=edited_ticket.title, body=edited_body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        source._tickets["80"] = edited_ticket
        remote.items["80"] = edited_item

        saved = specgraphs.get(key, REPO_NAME)
        with (mock.patch.object(requirement_revision, "github_conflict", return_value=""),
              mock.patch.object(spec_tracker, "adapter", return_value=remote)):
            # During the gap before reconcile(): unrelated branch (docs=83) is unaffected,
            # while affected child (api=81) and its dependent (ui=82) are held!
            self.assertTrue(spec_amendments.unaffected(source, remote, key, saved, edited_item, edited_ticket, "83"))
            self.assertFalse(spec_amendments.unaffected(source, remote, key, saved, edited_item, edited_ticket, "81"))
            self.assertFalse(spec_amendments.unaffected(source, remote, key, saved, edited_item, edited_ticket, "82"))
            self.assertEqual(spec_graph_delivery.ready(source, remote, key, "83"), "")
            self.assertIn(spec_graph_delivery.PARENT_AMENDMENT_HOLD,
                          spec_graph_delivery.ready(source, remote, key, "81"))

            # Reconcile updates api's contract, posts steering note on 81, and posts summary on parent 80.
            claim = specs.find(REPO_NAME, "80")
            self.assertFalse(spec_amendments.reconcile(source, claim, edited_ticket))
            self.assertEqual(len(remote.comments["81"]), 1)
            self.assertIn("AC-1: API returns v2 JSON with cursor", remote.comments["81"][0][1])
            self.assertEqual(len(remote.comments["80"]), 1)
            parent_note = remote.comments["80"][0][1]
            self.assertIn("AC-1: API returns v1 JSON", parent_note)
            self.assertIn("AC-1: API returns v2 JSON with cursor", parent_note)
            self.assertIn("81 (`api`)", parent_note)
            self.assertIn("83 (`docs`)", parent_note)

            # Replaying reconcile() after restart is a no-op with zero duplicate comments!
            claim_after = specs.find(REPO_NAME, "80")
            self.assertTrue(spec_amendments.reconcile(source, claim_after, edited_ticket))
            self.assertEqual(len(remote.comments["81"]), 1)
            self.assertEqual(len(remote.comments["80"]), 1)

            # child_ticket() appends amended contract to 81, leaving 83 untouched.
            t81 = tracker.Ticket("81", "#81", "API", body="Original API body")
            t83 = tracker.Ticket("83", "#83", "Docs", body="Original Docs body")
            self.assertIn("AC-1: API returns v2 JSON with cursor", spec_amendments.child_ticket(source, t81).body)
            self.assertEqual(spec_amendments.child_ticket(source, t83).body, "Original Docs body")

    def test_edit_merged_child_keeps_closed_ticket_creates_fix_child_and_relinks_dependent_once(self):
        key, remote, source = self._seed_two_branches()
        # Mark api (81) as closed & landed.
        remote.items["81"] = make_item("81", "801", title="API", opened=False)
        add_run("r-81", REPO_NAME, "81", "merged")
        pub_81 = make_verified_pub(f"https://github.com/{GITHUB_SLUG}/pull/81", SHA_A)
        pr_81 = {"state": "MERGED", "mergedAt": "2026-11-27T00:00:00Z", "headRefOid": SHA_A,
                 "baseRefName": "main", "mergeCommit": {"oid": SHA_A}}

        edited_body = "- AC-1: API returns v2 JSON with cursor\n- AC-2: UI renders card\n- AC-3: Docs list endpoint"
        edited_ticket = make_parent_ticket("80", body=edited_body)
        edited_item = make_item("80", "800", title=edited_ticket.title, body=edited_body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        source._tickets["80"] = edited_ticket
        remote.items["80"] = edited_item

        claim = specs.find(REPO_NAME, "80")
        with (mock.patch.object(requirement_revision, "github_conflict", return_value=""),
              mock.patch.object(spec_tracker, "adapter", return_value=remote),
              mock.patch.object(flows, "publication", return_value=pub_81),
              mock.patch.object(gh, "pr_view", return_value=pr_81),
              mock.patch.object(gh, "branch_contains", return_value=True)):
            self.assertFalse(spec_amendments.reconcile(source, claim, edited_ticket))

            # Completed ticket 81 stayed closed; a linked fix child (100) was created and enabled.
            self.assertFalse(remote.items["81"].open)
            self.assertEqual(len(remote.created_items), 1)
            fix_item = next(iter(remote.created_items.values()))
            self.assertIn(fix_item.ref, remote.enabled)
            # Open dependent 82 gained a real native blocking prerequisite on the fix child.
            self.assertIn((fix_item.ref, "82"), remote.blocked_links)
            # Parent summary names the changed check, correction child, relinked dependent, and unaffected child.
            parent_note = remote.comments["80"][0][1]
            self.assertIn(fix_item.ref, parent_note)
            self.assertIn("82 (`ui`)", parent_note)
            self.assertIn("83 (`docs`)", parent_note)

            # Replaying reconcile() creates nothing new and posts no duplicate comments.
            self.assertTrue(spec_amendments.reconcile(source, specs.find(REPO_NAME, "80"), edited_ticket))
            self.assertEqual(len(remote.created_items), 1)
            self.assertEqual(len(remote.comments["80"]), 1)


class SpecAmendmentWebhookAndRedirectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(self.root))
        db.migrate()
        self.repo = config.Repo(REPO_NAME, self.root, github=GITHUB_SLUG, pipeline=True, landing="pr")
        self.cfg = config.Config(repos=[self.repo])

    def test_webhook_spec_amendments_auth_dispatch_and_replay_deduplication(self):
        with mock.patch.object(loops, "load_all", return_value=[]):
            secret_val = webhooks.rotate(webhooks.SPEC_HOOK, "cli:test")
            gh_body = json.dumps({
                "action": "edited",
                "repository": {"full_name": GITHUB_SLUG},
                "issue": {"number": 80},
            }).encode()
            sig = "sha256=" + hmac.new(secret_val.encode(), gh_body, hashlib.sha256).hexdigest()
            headers = {
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "deliv-1",
                "X-Hub-Signature-256": sig,
            }

            # Rejects invalid signature.
            with self.assertRaises(Stated) as bad:
                webhooks.receive(webhooks.SPEC_HOOK, {**headers, "X-Hub-Signature-256": "sha256=dead"}, gh_body)
            self.assertEqual(bad.exception.status, 403)

            # Valid GitHub issue edit triggers spec amendment reconciliation and deduplicates replay.
            scheduled = []
            with mock.patch.object(webhooks, "schedule_spec_reconcile", side_effect=lambda e: scheduled.append(e) or True):
                first = webhooks.receive(webhooks.SPEC_HOOK, headers, gh_body)
                replay = webhooks.receive(webhooks.SPEC_HOOK, headers, gh_body)
            self.assertTrue(first["queued"])
            self.assertTrue(first["reconcile"])
            self.assertFalse(replay["queued"])
            self.assertEqual(replay["why"], "already received")
            self.assertEqual(len(scheduled), 1)
            self.assertEqual(webhooks.spec_amendment_target(scheduled[0]), ("github", GITHUB_SLUG, "80"))

            # Jira issue_updated payload is also recognized.
            jira_body = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "PROJ-42"}}).encode()
            jira_event = webhooks.event_of(webhooks.SPEC_HOOK, {"X-Request-Id": "j-1"}, jira_body)
            self.assertEqual(webhooks.spec_amendment_target(jira_event), ("jira", "", "PROJ-42"))

    def test_reconcile_spec_event_invokes_spec_amendments_for_matching_published_graph(self):
        parent_ticket = make_parent_ticket("90", body="- AC-1: old")
        parent_item = make_item("90", "900", title=parent_ticket.title, body=parent_ticket.body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        children = [{"key": "api", "title": "API", "outcome": "API", "checks": ["AC-1: old"],
                     "requires": [], "release_requires": [], "artifacts": {}, "existing": ""}]
        seed_graph(REPO_NAME, parent_ticket, parent_item, children, [("api", "91", "901")], run_id="r-90")
        source = FakeGitHubSource(self.repo, {"90": parent_ticket})

        event = webhooks.event_of(
            webhooks.SPEC_HOOK,
            {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-90"},
            json.dumps({"action": "edited", "repository": {"full_name": GITHUB_SLUG}, "issue": {"number": 90}}).encode(),
        )
        with (mock.patch.object(tracker, "for_ticket", return_value=source),
              mock.patch.object(spec_amendments, "reconcile", return_value=True) as rec):
            matched = webhooks.reconcile_spec_event(event, cfg=self.cfg)
        self.assertEqual(matched, [(REPO_NAME, "90")])
        rec.assert_called_once()

    def test_inflight_affected_child_cancels_stale_worker_and_refreshes_scope_while_sibling_continues(self):
        parent_ticket = make_parent_ticket("95", body="- AC-1: old\n- AC-2: untouched")
        parent_item = make_item("95", "950", title=parent_ticket.title, body=parent_ticket.body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        children = [
            {"key": "api", "title": "API", "outcome": "API", "checks": ["AC-1: old"],
             "requires": [], "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "docs", "title": "Docs", "outcome": "Docs", "checks": ["AC-2: untouched"],
             "requires": [], "release_requires": [], "artifacts": {}, "existing": ""},
        ]
        items = [make_item("96", "951", title="API"), make_item("97", "952", title="Docs")]
        key = seed_graph(REPO_NAME, parent_ticket, parent_item, children,
                         [("api", "96", "951"), ("docs", "97", "952")], run_id="r-95")
        remote = FakeRemote(parent_item, items)

        # Simulate an amended contract already reconciled for child 96 (leaving sibling 97 untouched).
        saved = specgraphs.get(key, REPO_NAME)
        new_contract = "Current acceptance from parent 95 (v2).\n\n### Acceptance checks\n\n- AC-1: new"
        updated = {**saved["data"], "amended_contracts": {"api": new_contract}}
        specgraphs.amend(key, saved["data"], updated)

        t96_old = tracker.Ticket("96", "#96", "API", body="Original API body", open=True, labels=[tracker.LABEL], owner_comments="")
        t97_old = tracker.Ticket("97", "#97", "Docs", body="Original Docs body", open=True, labels=[tracker.LABEL], owner_comments="")
        source = FakeGitHubSource(self.repo, {"95": parent_ticket, "96": t96_old, "97": t97_old})

        # Unaffected sibling 97 has no blocker at all.
        run97 = SimpleNamespace(tid="97", tracker=source, repo=self.repo)
        with mock.patch.object(spec_tracker, "adapter", return_value=remote):
            self.assertEqual(spec_graph_delivery.active_child_blocker(run97, t97_old), "")

        # Affected child 96 with an in-flight worker cancels stale result and refreshes scope in-place!
        add_run("r-96", REPO_NAME, "96", "running")
        lease = ledger.acquire("r-96", "", {})
        action_id = "act-1"
        turn = ledger.begin(lease, {"test": True}, 10)
        ledger.accept(lease, turn, 0, {"version": 1, "decision_id": turn, "state_version": 0,
                                       "rationale": "work", "calls": [{"op": "worker.start", "args": {}}]}, 10)
        act = ledger.actions(lease)[0]
        action_id = act["id"]
        ledger.transition(lease, action_id, "running", {})

        ctrl = object.__new__(orchestration.Controller)
        ctrl.lease = lease
        ctrl.protocol = 1
        ctrl.run = SimpleNamespace(
            id="r-96", tid="96", tracker=source, repo=self.repo, path="orchestrate",
            launch_done=None, cost=0.0, paid=0.0, dry_run=False, alone=False,
            owner_questions_blocked=True, scope_revoked=False, spec_identity="",
            triage_key=f"{REPO_NAME}:96", story=[],
        )
        ctrl.job = SimpleNamespace(
            ticket=t96_old, b=SimpleNamespace(ticket=t96_old), approval=None,
            expertise="", attachments="", pr_plans=[], goal="", left_for_a_person="", run=ctrl.run,
        )
        ctrl.catalog = object.__new__(orchestration_api.Catalog)
        ctrl.catalog.job = ctrl.job
        ctrl.catalog.context = {}
        ctrl.catalog.refresh_context()
        prepared = {
            "ticket": {field: getattr(t96_old, field) for field in flows.TICKET_FIELDS},
            "approval": None,
        }
        ctrl.data = {"prepared": prepared, "goal_base": "old goal", "evidence": {"checks": {"tree": "old"}}}
        ledger.save(lease, ctrl.data)
        ctrl.ending = None
        fake_worker = SimpleNamespace(
            step="build", logical_id=action_id, effective={}, writing=True,
            receive=lambda: [{"event": "result", "ok": True, "text": "stale result"}],
            close=lambda: None,
        )
        ctrl.workers = {action_id: fake_worker}
        ctrl.quiescent = lambda: None
        ctrl.discard_gate_state = lambda: ctrl.data.pop("evidence", None)
        ctrl.persist = lambda: ledger.save(ctrl.lease, ctrl.data)

        with (mock.patch.object(spec_tracker, "adapter", return_value=remote),
              mock.patch.object(requirement_revision, "github_conflict", return_value="")):
            changed = ctrl.collect()
            self.assertTrue(changed)
            self.assertEqual(ctrl.workers, {})
            self.assertEqual(ledger.actions(lease)[0]["state"], "cancelled")
            self.assertIn("amended", ctrl.data["scope_needs_refresh"])

            # Now drive() refreshes the amended scope cleanly in-place!
            refreshed = ctrl.refresh_amended_scope()
            self.assertTrue(refreshed)
            self.assertIsNone(ctrl.ending)
            self.assertNotIn("scope_needs_refresh", ctrl.data)
            self.assertIn("AC-1: new", ctrl.job.ticket.body)
            self.assertIn("AC-1: new", ctrl.catalog.context["task"])
            self.assertNotIn("evidence", ctrl.data)


if __name__ == "__main__":
    unittest.main()
