import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

import support

from arete import (
    board,
    config,
    control,
    flows,
    gh,
    pipeline,
    record,
    release_proof,
    slots,
    spec_delivery,
    spec_graph,
    spec_graph_delivery,
    spec_tracker,
    tracker,
    util,
)
from arete.commands.common import print_ticket_progress
from arete.store import db, orchestration, specgraphs, specs

setUpModule = support.isolate_live_state

REPO_NAME = "product"
GITHUB_SLUG = "org/product"
SCOPE = f"github:{GITHUB_SLUG}"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
SHA_MAIN_1 = "1" * 40
SHA_MAIN_2 = "2" * 40


def make_parent_ticket(issue="10", body="AC-1: API works\nAC-2: UI works", title="Ship feature", labels=None):
    return tracker.Ticket(
        id=issue,
        ref=f"#{issue}",
        title=title,
        body=body,
        open=True,
        labels=list(labels if labels is not None else (spec_tracker.SPEC, tracker.LABEL)),
        spec_identity=tracker.github_issue_identity(GITHUB_SLUG, issue),
        owner_comments="",
    )


def make_item(ref, native_id, title="Item", body="Body", labels=None, opened=True):
    return spec_tracker.Item(
        ref=str(ref),
        native_id=str(native_id),
        title=title,
        body=body,
        labels=tuple(labels if labels is not None else (tracker.LABEL,)),
        open=opened,
    )


def make_verified_pub(pr_url, head_sha, *, contract=None, summary="Verified change"):
    pub = {
        "summary": summary,
        "prs": {GITHUB_SLUG: pr_url},
        "heads": {GITHUB_SLUG: head_sha},
        "verified": {
            "verdict": "pass",
            "finished": True,
            "behavior_reached": True,
            "checks": [{"what": "AC-1", "how": "ran check", "saw": "ok", "passed": True}],
            "failed_checks": [],
        },
    }
    if contract:
        pub["graph_contract"] = orchestration.digest(contract)
    return pub


class FakeRemote:
    def __init__(self, parent_item, items, *, parents=None, blockers=None, account="42"):
        self.scope = SCOPE
        self._account = account
        self.parent_item = parent_item
        self.items = {parent_item.ref: parent_item, **{i.ref: i for i in items}}
        self.parents = parents or {i.ref: parent_item for i in items}
        self._blockers = blockers or {}
        self.comments = {}
        self.created_items = {}
        self.attached = []
        self.blocked_links = []
        self.enabled = []
        self.next_ref = 100

    def account(self):
        return self._account

    def read(self, ref):
        return self.items[str(ref)]

    def parent(self, ref):
        return self.parents.get(str(ref))

    def children(self, ref):
        return [item for r, item in self.items.items() if r != ref and self.parents.get(r) == self.items[ref]]

    def blockers(self, ref, policy):
        return set(self._blockers.get(str(ref), set()))

    def find_creation(self, token, account):
        return self.created_items.get(token)

    def create(self, node, parent, policy, token, account):
        ref = str(self.next_ref)
        self.next_ref += 1
        item = make_item(ref, str(1000 + int(ref)), title=node["title"], body=spec_tracker.contract(node), labels=())
        self.items[ref] = item
        self.created_items[token] = item
        return item

    def attach(self, parent, child):
        self.parents[child.ref] = parent
        self.attached.append((parent.ref, child.ref))

    def block(self, prerequisite, dependent, policy):
        self._blockers.setdefault(dependent.ref, set()).add(prerequisite.ref)
        self.blocked_links.append((prerequisite.ref, dependent.ref))

    def comment(self, ref, body, token):
        self.comments.setdefault(str(ref), []).append((token, body))

    def commented(self, ref, token, account):
        return any(t == token for t, _ in self.comments.get(str(ref), ()))

    def enable(self, ref):
        item = self.items[str(ref)]
        self.items[str(ref)] = spec_tracker.Item(
            item.ref, item.native_id, item.title, item.body, tuple({*item.labels, tracker.LABEL}), item.open
        )
        self.enabled.append(str(ref))


class FakeGitHubSource(tracker.GitHubTracker):
    def __init__(self, repo, tickets):
        self.repo = repo
        self.owner = "owner"
        self._tickets = dict(tickets)
        self._open = {}

    def get(self, tid: str):
        return self._tickets[str(tid)]


def add_run(run_id, repo_name, issue, status="merged", *, summary=""):
    support.start_pipeline(run_id=run_id, repo=repo_name, issue=str(issue))
    if status != "running":
        record.settle(run_id, status, summary=summary)


def seed_graph(repo_name, parent_ticket, parent_item, plan_children, existing_pairs, *,
               complete=True, run_id="run-parent", state="published", delivery_extra=None):
    key = specgraphs.GraphKey(SCOPE, parent_ticket.id)
    data = {
        "version": 1,
        "accepted_parent": {"title": parent_ticket.title, "body": parent_ticket.body, "owner_comments": ""},
        "account": "42",
        "destination": SCOPE,
        "project": "",
        "route": "",
        "targets": [{"repo": GITHUB_SLUG, "branch": "main"}],
        "parent": spec_graph.body_identity(parent_item),
        "native_id": parent_item.native_id,
        "source": "src-digest",
        "ticket_version": spec_delivery.source_version(parent_ticket),
        "input": "in-digest",
        "plan": {"mode": "split", "reason": "separately testable", "environment": "", "children": plan_children},
        "review": {"reason": "ok", "add_contract": []},
        "policy": {},
        "adopted": {},
    }
    add_run(run_id, repo_name, parent_ticket.id, "done", summary="Published spec graph")
    specs.bind(parent_ticket.spec_identity, repo_name, parent_ticket.id, "", lambda: run_id)
    specgraphs.accept(key, repo_name, data, existing_pairs)
    if complete:
        delivery = {
            "graph": True,
            "source": data["ticket_version"],
            "definition": {
                "criterion": parent_ticket.title,
                "procedure": "Verify assembled feature",
                "environment": "",
            },
            "landing": [],
            **(delivery_extra or {}),
        }
        specgraphs.finish(key, parent_ticket.spec_identity, run_id, delivery)
    return key


class SpecGraphStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(self.root))
        db.migrate()

    def test_accept_write_receipts_finish_and_amend_lifecycle(self):
        parent_ticket = make_parent_ticket("10")
        parent_item = make_item("10", "100", title=parent_ticket.title, body=parent_ticket.body, labels=(spec_tracker.SPEC, tracker.LABEL))
        children = [{"key": "api", "title": "API", "outcome": "API ready", "checks": ["AC-1: API works"],
                     "requires": [], "release_requires": [], "artifacts": {}, "existing": ""}]
        key = seed_graph(REPO_NAME, parent_ticket, parent_item, children, [("api", "11", "101")], complete=False)

        self.assertTrue(specgraphs.unfinished(REPO_NAME, "10"))
        self.assertEqual(specgraphs.active_parents(REPO_NAME), ["10"])
        self.assertEqual(specgraphs.membership(SCOPE, "11"), key)
        self.assertTrue(specgraphs.tracks_ticket(REPO_NAME, "11"))
        self.assertIn("owned by the spec graph", specgraphs.launch_blocker(SCOPE, "11"))

        self.assertIs(specgraphs.receipt(key, "create:api"), specgraphs.Receipt.MISSING)
        specgraphs.prepare(key, "create:api")
        self.assertIs(specgraphs.receipt(key, "create:api"), specgraphs.Receipt.PREPARED)
        self.assertTrue(specgraphs.dispatch(key, "create:api"))
        self.assertFalse(specgraphs.dispatch(key, "create:api"))
        self.assertIs(specgraphs.receipt(key, "create:api"), specgraphs.Receipt.DISPATCHED)
        self.assertTrue(specgraphs.unconfirmed(key))

        with self.assertRaises(specgraphs.Conflict):
            specgraphs.finish(key, parent_ticket.spec_identity, "run-parent", {"graph": True})

        specgraphs.rejected(key, "create:api")
        self.assertIs(specgraphs.receipt(key, "create:api"), specgraphs.Receipt.MISSING)
        specgraphs.prepare(key, "create:api")
        specgraphs.dispatch(key, "create:api")
        specgraphs.receive(key, "create:api", ("api", "11", "101"))
        self.assertIs(specgraphs.receipt(key, "create:api"), specgraphs.Receipt.RECEIVED)
        self.assertFalse(specgraphs.unconfirmed(key))

        delivery = {"graph": True, "source": "v1", "definition": {"criterion": "c", "procedure": "p", "environment": ""},
                    "landing": [], "notes": {"x": "u"}, "integrated_verification": {"status": "failed"}}
        specgraphs.finish(key, parent_ticket.spec_identity, "run-parent", delivery)
        self.assertFalse(specgraphs.unfinished(REPO_NAME, "10"))
        found_key, saved, mapped = specgraphs.find_parent(REPO_NAME, "10")
        self.assertEqual(found_key, key)
        self.assertEqual(mapped, {"api": ("11", "101")})

        claim = specs.find(REPO_NAME, "10")
        updated = {**saved["data"], "ticket_version": "v2"}
        specgraphs.amend(key, saved["data"], updated, claim=claim)
        refreshed_claim = specs.find(REPO_NAME, "10")
        self.assertEqual(refreshed_claim["data"]["source"], "v2")
        self.assertNotIn("notes", refreshed_claim["data"])
        self.assertNotIn("integrated_verification", refreshed_claim["data"])

        with self.assertRaises(specgraphs.Conflict):
            specgraphs.amend(key, saved["data"], updated)


class SpecGraphReadinessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(self.root))
        db.migrate()
        self.repo = config.Repo(REPO_NAME, self.root, github=GITHUB_SLUG, pipeline=True, landing="pr")

    def _setup_diamond(self):
        parent_ticket = make_parent_ticket("10", body="AC-1: A\nAC-2: B\nAC-3: C\nAC-4: D")
        parent_item = make_item("10", "100", title=parent_ticket.title, body=parent_ticket.body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        plan_children = [
            {"key": "api", "title": "API", "outcome": "API", "checks": ["AC-1: A"], "requires": [],
             "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "pkg", "title": "Package", "outcome": "Pkg", "checks": ["AC-2: B"], "requires": [],
             "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "docs", "title": "Docs", "outcome": "Docs", "checks": ["AC-3: C"], "requires": [],
             "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "app", "title": "App", "outcome": "App", "checks": ["AC-4: D"],
             "requires": ["api", "pkg"], "release_requires": ["api"],
             "artifacts": {"pkg": {"name": "@org/sdk", "version": "1.2.3"}}, "existing": ""},
        ]
        items = [
            make_item("11", "101", title="API"),
            make_item("12", "102", title="Package"),
            make_item("13", "103", title="Docs"),
            make_item("14", "104", title="App"),
        ]
        key = seed_graph(
            REPO_NAME, parent_ticket, parent_item, plan_children,
            [("api", "11", "101"), ("pkg", "12", "102"), ("docs", "13", "103"), ("app", "14", "104")],
        )
        remote = FakeRemote(parent_item, items, blockers={"14": {"11", "12"}})
        source = FakeGitHubSource(self.repo, {"10": parent_ticket})
        return key, remote, source

    def test_parallel_independent_children_and_merge_release_artifact_prerequisites(self):
        key, remote, source = self._setup_diamond()

        # Independent children 11, 12, 13 are all immediately ready in parallel; dependent 14 waits.
        self.assertEqual(spec_graph_delivery.ready(source, remote, key, "11"), "")
        self.assertEqual(spec_graph_delivery.ready(source, remote, key, "12"), "")
        self.assertEqual(spec_graph_delivery.ready(source, remote, key, "13"), "")
        self.assertIn("waiting for 11 to reach its required release", spec_graph_delivery.ready(source, remote, key, "14"))

        # Record merged PRs for 11 (requires @release) and 12 (requires @artifact=@org/sdk:1.2.3).
        add_run("r-11", REPO_NAME, "11", "merged")
        add_run("r-12", REPO_NAME, "12", "merged")
        pubs = {
            "r-11": make_verified_pub(f"https://github.com/{GITHUB_SLUG}/pull/1", SHA_A),
            "r-12": make_verified_pub(f"https://github.com/{GITHUB_SLUG}/pull/2", SHA_B),
        }
        prs = {
            f"https://github.com/{GITHUB_SLUG}/pull/1": {
                "state": "MERGED", "mergedAt": "2026-11-27T00:00:00Z",
                "headRefOid": SHA_A, "baseRefName": "main", "mergeCommit": {"oid": SHA_A},
            },
            f"https://github.com/{GITHUB_SLUG}/pull/2": {
                "state": "MERGED", "mergedAt": "2026-11-27T00:00:00Z",
                "headRefOid": SHA_B, "baseRefName": "main", "mergeCommit": {"oid": SHA_B},
            },
        }
        receipts = {}

        def fake_evidence(repo_name, ticket, commit):
            return receipts.get((ticket, commit))

        with (mock.patch.object(flows, "publication", side_effect=lambda rid: pubs.get(rid, {})),
              mock.patch.object(gh, "pr_view", side_effect=lambda url: prs[url]),
              mock.patch.object(gh, "branch_contains", return_value=True),
              mock.patch.object(release_proof, "evidence", side_effect=fake_evidence),
              mock.patch.object(spec_tracker, "adapter", return_value=remote)):
            # Merge alone does NOT satisfy @release or @artifact!
            self.assertIn("waiting for 11 to reach its required release", spec_graph_delivery.ready(source, remote, key, "14"))
            self.assertEqual(spec_graph_delivery.satisfied_native(source, "14", {"11", "12"}), set())

            # Release proof for 11 unblocks 11, while 12 with wrong package version still blocks 14.
            receipts[("11", SHA_A)] = {"status": "verified", "revision": SHA_A, "observed": []}
            receipts[("12", SHA_B)] = {
                "status": "verified", "revision": SHA_B,
                "observed": [
                    {"field": "/name", "value_sha256": release_proof.digest("@org/sdk")},
                    {"field": "/version", "value_sha256": release_proof.digest("1.2.2")},
                ],
            }
            self.assertEqual(spec_graph_delivery.satisfied_native(source, "14", {"11", "12"}), {"11"})
            self.assertIn("waiting for 12 to reach its required @org/sdk 1.2.3",
                          spec_graph_delivery.ready(source, remote, key, "14"))

            # Matching package version unblocks 14 even while 11 and 12 tracker tickets remain open!
            receipts[("12", SHA_B)]["observed"][1]["value_sha256"] = release_proof.digest("1.2.3")
            self.assertEqual(spec_graph_delivery.satisfied_native(source, "14", {"11", "12"}), {"11", "12"})
            self.assertEqual(spec_graph_delivery.ready(source, remote, key, "14"), "")

    def test_merge_dependency_unblocks_before_production_and_restart_preserves_identities(self):
        parent_ticket = make_parent_ticket("20", body="AC-1: API\nAC-2: Consumer")
        parent_item = make_item("20", "200", title=parent_ticket.title, body=parent_ticket.body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        plan_children = [
            {"key": "api", "title": "API", "outcome": "API ready", "checks": ["AC-1: API"], "requires": [],
             "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "consumer", "title": "Consumer", "outcome": "Consumer", "checks": ["AC-2: Consumer"],
             "requires": ["api"], "release_requires": [], "artifacts": {}, "existing": ""},
        ]
        key = seed_graph(REPO_NAME, parent_ticket, parent_item, plan_children, [], complete=False, run_id="r-pub-20")
        remote = FakeRemote(parent_item, [])
        saved = specgraphs.get(key, REPO_NAME)

        # Publish once, then replay Publication after a restart: exact same child IDs, no duplicate creation.
        pub1 = spec_graph.Publication(remote, parent_item, saved, lambda: True)
        items1 = pub1.publish()
        created_count = len(remote.created_items)
        pub2 = spec_graph.Publication(remote, parent_item, specgraphs.get(key, REPO_NAME), lambda: True)
        items2 = pub2.publish()
        self.assertEqual({k: v.ref for k, v in items1.items()}, {k: v.ref for k, v in items2.items()})
        self.assertEqual(len(remote.created_items), created_count)

        specgraphs.finish(key, parent_ticket.spec_identity, "r-pub-20", {
            "graph": True, "source": saved["data"]["ticket_version"],
            "definition": {"criterion": "c", "procedure": "p", "environment": ""}, "landing": [],
        })
        api_ref, consumer_ref = items1["api"].ref, items1["consumer"].ref
        source = FakeGitHubSource(self.repo, {"20": parent_ticket})

        add_run("r-api", REPO_NAME, api_ref, "merged")
        pub_data = make_verified_pub(f"https://github.com/{GITHUB_SLUG}/pull/10", SHA_A)
        pr_data = {"state": "MERGED", "mergedAt": "2026-11-27T00:00:00Z", "headRefOid": SHA_A,
                   "baseRefName": "main", "mergeCommit": {"oid": SHA_A}}
        with (mock.patch.object(flows, "publication", return_value=pub_data),
              mock.patch.object(gh, "pr_view", return_value=pr_data),
              mock.patch.object(gh, "branch_contains", return_value=True)):
            # @merge prerequisite unblocks consumer immediately on merge without any release_proof!
            self.assertEqual(spec_graph_delivery.ready(source, remote, key, consumer_ref), "")


class SpecGraphParentStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(self.root))
        db.migrate()
        self.repo = config.Repo(REPO_NAME, self.root, github=GITHUB_SLUG, pipeline=True, landing="pr")
        self.cfg = config.Config(repos=[self.repo])
        self.enterContext(mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")))
        self.enterContext(mock.patch.object(board, "RECENT_RUNS", 0))
        self.enterContext(mock.patch.object(control, "BEAT_FILE", self.root / "serve-beat.json"))
        self.enterContext(mock.patch.object(slots, "holding", return_value=[]))
        self.enterContext(mock.patch.object(pipeline, "SKIPPED_FILE", self.root / "skipped.json"))
        self.enterContext(mock.patch.object(util, "now", return_value=util.parse_iso("2026-11-27T12:00:00Z")))

    def test_parent_status_surfaces_per_child_wait_reasons_without_changing_legacy_live_shape(self):
        parent_ticket = make_parent_ticket("30", body="AC-1: API\nAC-2: App")
        parent_item = make_item("30", "300", title=parent_ticket.title, body=parent_ticket.body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        plan_children = [
            {"key": "api", "title": "API", "outcome": "API", "checks": ["AC-1: API"], "requires": [],
             "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "app", "title": "App", "outcome": "App", "checks": ["AC-2: App"], "requires": ["api"],
             "release_requires": ["api"], "artifacts": {}, "existing": ""},
        ]
        seed_graph(REPO_NAME, parent_ticket, parent_item, plan_children, [("api", "31", "301"), ("app", "32", "302")],
                   run_id="run-parent-30")
        add_run("run-31", REPO_NAME, "31", "failed")

        # Legacy /api/live and /api/runs/{id} shapes remain unchanged (no ticket_progress).
        self.assertNotIn("ticket_progress", board.live_board(None))
        self.assertNotIn("ticket_progress", board.run_board("run-parent-30"))

        # Opt-in ticket_progress surfaces the parent spec with per-child wait reasons!
        rows = board.live_board(None, include_ticket_progress=True)["ticket_progress"]["rows"]
        parent_row = next(r for r in rows if r["issue"] == "30")
        self.assertIn("spec_graph", parent_row)
        self.assertEqual(board.run_board("run-parent-30", include_ticket_progress=True)["ticket_progress"], parent_row)
        self.assertEqual(parent_row["spec_graph"]["waiting"], [{
            "key": "app", "ticket": "32",
            "reason": "waiting for 31 to reach its required release (31 failed)",
        }])
        self.assertIn("32: waiting for 31 to reach its required release", parent_row["blocker"])

        # CLI rendering prints the child states and wait reasons.
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_ticket_progress([parent_row])
        self.assertIn("Child 32 (app): waiting — waiting for 31 to reach its required release", buf.getvalue())


class SpecGraphIntegratedVerificationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(self.root))
        db.migrate()
        self.repo = config.Repo(REPO_NAME, self.root, github=GITHUB_SLUG, pipeline=True, landing="pr")
        self.cfg = config.Config(repos=[self.repo])

    def _seed_landed_graph(self, parent_body="AC-1: Backend\nAC-2: Frontend", *, same_pr=False):
        parent_ticket = make_parent_ticket("40", body=parent_body)
        parent_item = make_item("40", "400", title=parent_ticket.title, body=parent_ticket.body,
                                labels=(spec_tracker.SPEC, tracker.LABEL))
        plan_children = [
            {"key": "backend", "title": "Backend", "outcome": "Backend", "checks": ["AC-1: Backend"],
             "requires": [], "release_requires": [], "artifacts": {}, "existing": ""},
            {"key": "frontend", "title": "Frontend", "outcome": "Frontend", "checks": ["AC-2: Frontend"],
             "requires": ["backend"], "release_requires": [], "artifacts": {}, "existing": ""},
        ]
        items = [make_item("41", "401", title="Backend"), make_item("42", "402", title="Frontend")]
        seed_graph(REPO_NAME, parent_ticket, parent_item, plan_children,
                   [("backend", "41", "401"), ("frontend", "42", "402")], run_id="run-parent-40")
        add_run("r-41", REPO_NAME, "41", "merged")
        add_run("r-42", REPO_NAME, "42", "merged")
        pr1 = f"https://github.com/{GITHUB_SLUG}/pull/41"
        pr2 = pr1 if same_pr else f"https://github.com/{GITHUB_SLUG}/pull/42"
        pubs = {
            "r-41": make_verified_pub(pr1, SHA_A, summary="Backend shipped"),
            "r-42": make_verified_pub(pr2, SHA_A if same_pr else SHA_B, summary="Frontend shipped"),
        }
        prs = {
            pr1: {"state": "MERGED", "mergedAt": "2026-11-27T00:00:00Z", "headRefOid": SHA_A,
                  "baseRefName": "main", "mergeCommit": {"oid": SHA_A}},
            pr2: {"state": "MERGED", "mergedAt": "2026-11-27T00:00:00Z", "headRefOid": SHA_A if same_pr else SHA_B,
                  "baseRefName": "main", "mergeCommit": {"oid": SHA_A if same_pr else SHA_B}},
        }
        remote = FakeRemote(parent_item, items)
        source = FakeGitHubSource(self.repo, {"40": parent_ticket})
        return source, remote, pubs, prs

    def test_fallback_unmet_check_posts_idempotent_note_then_closes_after_correction_and_activation(self):
        source, remote, pubs, prs = self._seed_landed_graph(
            parent_body="AC-1: Backend\nAC-2: Frontend\nFeature flag: checkout_v2\nAudience activation: staff-beta"
        )
        posted = []

        def fake_comment(slug, num, body, return_url=False, single_attempt=False):
            posted.append(body)
            return f"https://github.com/{slug}/issues/{num}#issuecomment-{len(posted)}"

        claim = specs.find(REPO_NAME, "40")
        assembled_1 = orchestration.digest([(GITHUB_SLUG, "main", SHA_MAIN_1)])[:24]
        claim["data"]["integrated_verification"] = {
            "assembled": assembled_1, "status": "failed",
            "unmet": "Integrated checkout flow returned 500",
        }
        spec_delivery.save(claim)

        with (mock.patch.object(flows, "publication", side_effect=lambda rid: pubs[rid]),
              mock.patch.object(gh, "pr_view", side_effect=lambda url: prs[url]),
              mock.patch.object(gh, "branch_contains", return_value=True),
              mock.patch.object(spec_tracker, "adapter", return_value=remote),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_MAIN_1, SHA_MAIN_1)),
              mock.patch.object(gh, "issue_comment", side_effect=fake_comment)):
            # Unmet integrated check keeps parent open and posts visibility note strictly once.
            self.assertEqual(spec_graph_delivery.integrated(source, claim), {})
            self.assertEqual(len(posted), 1)
            self.assertIn("Integrated checkout flow returned 500", posted[0])
            self.assertEqual(spec_graph_delivery.integrated(source, claim), {})
            self.assertEqual(len(posted), 1)

            # Passing check without required feature-flag / activation confirmation STILL stays open.
            claim["data"]["integrated_verification"] = {
                "assembled": assembled_1, "status": "pass", "how": "ran e2e", "saw": "checkout succeeded",
            }
            spec_delivery.save(claim)
            self.assertEqual(spec_graph_delivery.integrated(source, claim), {})
            self.assertEqual(len(posted), 2)
            self.assertIn("Required activation not yet verified", posted[1])
            self.assertIn("checkout_v2", posted[1])

        # After correction lands at SHA_MAIN_2 with activation confirmed, integrated check passes and settles!
        assembled_2 = orchestration.digest([(GITHUB_SLUG, "main", SHA_MAIN_2)])[:24]
        claim["data"]["integrated_verification"] = {
            "assembled": assembled_2, "status": "pass",
            "how": "ran e2e with checkout_v2 enabled for staff-beta",
            "saw": "integrated checkout succeeded",
            "activations": ["checkout_v2", "staff-beta"],
        }
        spec_delivery.save(claim)
        closed = []
        with (mock.patch.object(flows, "publication", side_effect=lambda rid: pubs[rid]),
              mock.patch.object(gh, "pr_view", side_effect=lambda url: prs[url]),
              mock.patch.object(gh, "branch_contains", return_value=True),
              mock.patch.object(spec_tracker, "adapter", return_value=remote),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_MAIN_2, SHA_MAIN_2)),
              mock.patch.object(gh, "issue_comment", side_effect=fake_comment),
              mock.patch.object(gh, "issue_close", side_effect=lambda slug, n: closed.append((slug, n))),
              mock.patch.object(tracker, "for_ticket", return_value=source),
              mock.patch.object(tracker, "spec_blocker", return_value="")):
            spec_delivery.settle(self.cfg, self.repo, claim)
        self.assertEqual(closed, [(GITHUB_SLUG, 40)])
        self.assertEqual(specs.find(REPO_NAME, "40")["state"], "complete")
        self.assertIn("Combined child verification", posted[-1])
        self.assertIn("checkout_v2", posted[-1])

    def test_non_reversible_shared_child_pr_and_failed_release_proof_keep_parent_open(self):
        source, remote, pubs, prs = self._seed_landed_graph(same_pr=True)
        posted = []

        def fake_comment(slug, num, body, return_url=False, single_attempt=False):
            posted.append(body)
            return f"https://github.com/{slug}/issues/{num}#issuecomment-{len(posted)}"

        claim = specs.find(REPO_NAME, "40")
        with (mock.patch.object(flows, "publication", side_effect=lambda rid: pubs[rid]),
              mock.patch.object(gh, "pr_view", side_effect=lambda url: prs[url]),
              mock.patch.object(gh, "branch_contains", return_value=True),
              mock.patch.object(spec_tracker, "adapter", return_value=remote),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_MAIN_1, SHA_MAIN_1)),
              mock.patch.object(gh, "issue_comment", side_effect=fake_comment)):
            self.assertEqual(spec_graph_delivery.integrated(source, claim), {})
            self.assertEqual(len(posted), 1)
            self.assertIn("independently reversible", posted[0])

    def test_protected_release_proof_failure_posts_unmet_note_and_passing_receipt_verifies(self):
        source, remote, pubs, prs = self._seed_landed_graph()
        posted = []

        def fake_comment(slug, num, body, return_url=False, single_attempt=False):
            posted.append(body)
            return f"https://github.com/{slug}/issues/{num}#issuecomment-{len(posted)}"

        claim = specs.find(REPO_NAME, "40")
        policy = {"destination": "arete-serve", "path": "/api/health"}
        with (mock.patch.object(flows, "publication", side_effect=lambda rid: pubs[rid]),
              mock.patch.object(gh, "pr_view", side_effect=lambda url: prs[url]),
              mock.patch.object(gh, "branch_contains", return_value=True),
              mock.patch.object(spec_tracker, "adapter", return_value=remote),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_MAIN_1, SHA_MAIN_1)),
              mock.patch.object(release_proof, "required", return_value=True),
              mock.patch.object(release_proof, "goal", return_value=policy),
              mock.patch.object(gh, "issue_comment", side_effect=fake_comment)):
            with mock.patch.object(release_proof, "observe", return_value={
                "status": "failed", "revision": SHA_MAIN_1, "reason": "wrong value at /ok",
            }):
                self.assertEqual(spec_graph_delivery.integrated(source, claim), {})
                self.assertEqual(len(posted), 1)
                self.assertIn("wrong value at /ok", posted[0])
                # Re-running with the same failure is idempotent.
                self.assertEqual(spec_graph_delivery.integrated(source, claim), {})
                self.assertEqual(len(posted), 1)

            with mock.patch.object(release_proof, "observe", return_value={
                "status": "verified", "revision": SHA_MAIN_1, "path": "/api/health",
                "destination": "arete-serve", "receipt_id": "rcpt-123", "observed": [],
            }):
                result = spec_graph_delivery.integrated(source, claim)
                self.assertEqual(result["method"], "protected integrated release observation")
                self.assertIn("rcpt-123", result["saw"])
                self.assertEqual(len(result["children"]), 2)

    def test_fallback_publisher_observation_closes_when_confirmed(self):
        source, remote, pubs, prs = self._seed_landed_graph()
        posted = []

        def fake_comment(slug, num, body, return_url=False, single_attempt=False):
            posted.append(body)
            return f"https://github.com/{slug}/issues/{num}#issuecomment-{len(posted)}"

        claim = specs.find(REPO_NAME, "40")
        with (mock.patch.object(flows, "publication", side_effect=lambda rid: pubs[rid]),
              mock.patch.object(gh, "pr_view", side_effect=lambda url: prs[url]),
              mock.patch.object(gh, "branch_contains", return_value=True),
              mock.patch.object(spec_tracker, "adapter", return_value=remote),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_MAIN_1, SHA_MAIN_1)),
              mock.patch.object(gh, "issue_comment", side_effect=fake_comment),
              mock.patch.object(spec_delivery, "comments", return_value=[])):
            # Without observation reply, child merges alone do NOT close the parent.
            self.assertEqual(spec_graph_delivery.integrated(source, claim), {})
            self.assertEqual(len(posted), 1)
            token = claim["data"]["observation_request"]["token"]
            self.assertIn(f"Spec evidence {token}", posted[0])

        reply_body = f"Spec evidence {token}\nHow: Clicked through checkout\nSaw: Order confirmed"
        reply_url = f"https://github.com/{GITHUB_SLUG}/issues/40#issuecomment-99"
        reply_time = util.iso(util.parse_iso(claim["data"]["observation_request"]["since"]) + timedelta(seconds=30))
        with (mock.patch.object(flows, "publication", side_effect=lambda rid: pubs[rid]),
              mock.patch.object(gh, "pr_view", side_effect=lambda url: prs[url]),
              mock.patch.object(gh, "branch_contains", return_value=True),
              mock.patch.object(spec_tracker, "adapter", return_value=remote),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_MAIN_1, SHA_MAIN_1)),
              mock.patch.object(gh, "issue_comment", side_effect=fake_comment),
              mock.patch.object(spec_delivery, "comments", return_value=[(reply_body, reply_url, reply_time)]),
              mock.patch.object(spec_delivery, "confirmed_observation", return_value=True)):
            result = spec_graph_delivery.integrated(source, claim)
            self.assertEqual(result["method"], "assembled target observation")
            self.assertEqual(result["source"], reply_url)
            self.assertIn("Order confirmed", result["saw"])


if __name__ == "__main__":
    unittest.main()
