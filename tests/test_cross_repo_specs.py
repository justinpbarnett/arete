import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support

from arete import (
    adf,
    cli,
    config,
    cross_repo,
    gh,
    idempotency,
    jira,
    orchestration,
    owner_asks,
    paths,
    poll,
    record,
    release_proof,
    serve_guard,
    spec_delivery,
    spec_tracker,
    ticket_publication,
    tracker,
    verify,
    workspace,
)
from arete.store import specs

setUpModule = support.isolate_live_state

SHA_PKG = "a" * 40
SHA_APP = "b" * 40
SHA_PKG_TREE = "1" * 40
SHA_APP_TREE = "2" * 40
SHA_INFRA = "c" * 40
SHA_INFRA_TREE = "3" * 40


def _make_repo(root: Path, name: str, github: str, *, tracker_kind: str = "github",
               jira_project: str = "", jira_label: str = "", target_branch: str = "main",
               landing: str = "pr") -> config.Repo:
    checkout = root / name
    checkout.mkdir(parents=True, exist_ok=True)
    return config.Repo(
        name=name,
        path=checkout,
        github=github,
        pipeline=True,
        tracker=tracker_kind,
        jira_project=jira_project,
        jira_label=jira_label,
        parts=[config.Part("", github)],
        target_branch=target_branch,
        landing=landing,
        unattended_environment="production",
    )


def _seed_release_proof(repo: config.Repo, ticket: str, revision: str, *,
                        path: str = "/release", observed: list[dict] | None = None,
                        receipt_id: str = "rcpt-1", destination: str = "production") -> dict:
    policy = {
        "destination": destination,
        "path": path,
        "identity_path": "/version.json",
        "production": "https://release.example.test",
        "instructions": "Verify the released contract.",
        "assertions": [{"field": "/ready", "equals": True}],
        "recovery": [{"field": "/ready", "equals": True}],
    }
    policy_file = release_proof.policy_file(repo.name, ticket)
    serve_guard.write(policy_file, policy)
    digest = release_proof.digest(policy)
    proof = {
        "repo": repo.name,
        "ticket": str(ticket),
        "revision": revision,
        "destination": destination,
        "policy": digest,
        "path": path,
        "recovery": False,
        "status": "verified",
        "receipt_id": receipt_id,
        "observed": observed if observed is not None else [
            {"field": "/ready", "value_sha256": release_proof.digest(True)}
        ],
    }
    serve_guard.write(
        release_proof.state_dir(repo.name, ticket) / revision / destination / "behavior" / "latest.json",
        proof,
    )
    return proof


class FakePairTrackers:
    def __init__(self):
        self.github: dict[str, dict[int, dict]] = {}
        self.comments: dict[tuple[str, int], list[dict]] = {}
        self.blockers: dict[tuple[str, int], set[tuple[str, int]]] = {}
        self.jira: dict[str, dict] = {}
        self.jira_comments: dict[str, list[dict]] = {}
        self.fail_create_for: set[str] = set()
        self.fail_comment_once: set[tuple[str, int]] = set()
        self.client = jira.Client("https://jira.example.test", "fixture", "fixture")

    def github_call(self, args, *, input=None, write=False, **options):
        if args[:2] == ["label", "create"]:
            return ""
        if args[:2] == ["issue", "create"]:
            slug = args[args.index("--repo") + 1]
            if slug in self.fail_create_for:
                self.fail_create_for.remove(slug)
                raise gh.GhError(f"transient create failure for {slug}")
            issues = self.github.setdefault(slug, {})
            number = len(issues) + 1
            issues[number] = {
                "number": number,
                "id": number + (1000 if "pkg" in slug else 2000),
                "state": "open",
                "repository_url": f"{gh.GITHUB_API}/repos/{slug}",
                "html_url": gh.issue_url(slug, number),
                "body": str(input or ""),
                "title": args[args.index("--title") + 1],
                "labels": [{"name": args[i + 1]} for i, a in enumerate(args) if a == "--label"],
            }
            return issues[number]["html_url"]
        if args[:2] == ["issue", "comment"]:
            slug = args[args.index("--repo") + 1]
            number = int(args[2])
            if (slug, number) in self.fail_comment_once:
                self.fail_comment_once.remove((slug, number))
                raise gh.GhError("transient comment failure")
            comments = self.comments.setdefault((slug, number), [])
            cid = len(comments) + 1
            url = f"{gh.issue_url(slug, number)}#issuecomment-{cid}"
            comments.append({"id": cid, "body": str(input or ""), "html_url": url})
            return url
        assert args[0] == "api", args
        path = args[3] if len(args) > 3 and args[1] == "-X" else args[1]
        if path == "user":
            return json.dumps({"id": 77})
        if m := re.fullmatch(r"repos/([^/]+/[^/]+)/issues(?:\?.*)?", path):
            slug = m[1]
            return json.dumps([list(self.github.get(slug, {}).values())])
        if m := re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)/comments(?:\?.*)?", path):
            slug, number = m[1], int(m[2])
            return json.dumps([self.comments.get((slug, number), [])])
        if m := re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)/dependencies/blocked_by(?:\?.*)?", path):
            slug, number = m[1], int(m[2])
            if write:
                payload = json.loads(str(input))
                prereq_id = payload["issue_id"]
                for other_slug, rows in self.github.items():
                    for other_num, row in rows.items():
                        if row["id"] == prereq_id:
                            self.blockers.setdefault((slug, number), set()).add((other_slug, other_num))
                return ""
            rows = [self.github[s][n] for s, n in sorted(self.blockers.get((slug, number), ()))]
            return json.dumps([rows])
        if m := re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)", path):
            slug, number = m[1], int(m[2])
            return json.dumps(self.github[slug][number])
        raise AssertionError(f"unexpected gh call: {args}")

    def jira_call(self, method, path, params=None, body=None, **options):
        if path == "/rest/api/3/myself":
            return b'{"accountId":"fixture-account"}'
        if path == "/rest/api/3/search/jql":
            project = re.search(r'project = "(\w+)"', params["jql"])[1]
            rows = [r for k, r in self.jira.items() if k.startswith(project + "-")]
            return json.dumps({"issues": rows, "isLast": True}).encode()
        if m := re.fullmatch(r"/rest/api/[23]/issue/([A-Z0-9_-]+)/comment", path):
            key = m[1]
            if method == "GET":
                return json.dumps({"comments": self.jira_comments.get(key, [])}).encode()
            comments = self.jira_comments.setdefault(key, [])
            cid = str(len(comments) + 1)
            raw_body = body["body"]
            text = adf.to_text(raw_body) if isinstance(raw_body, dict) else str(raw_body)
            comments.append({"id": cid, "body": text, "author": {"accountId": "fixture-account"}})
            return json.dumps({"id": cid}).encode()
        assert (method, path) == ("POST", "/rest/api/3/issue"), (method, path)
        fields = body["fields"]
        project = fields["project"]["key"]
        key = f"{project}-{len(self.jira) + 1}"
        self.jira[key] = {"key": key, "id": str(500 + len(self.jira)), "fields": fields}
        return json.dumps({"key": key}).encode()


class PairedSpecPublicationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=paths.STATE_DIR)))
        self.pkg = _make_repo(self.root, "pkg", "org/pkg")
        self.app = _make_repo(self.root, "app", "org/app")
        self.jira_app = _make_repo(
            self.root, "jira-app", "org/jira-app", tracker_kind="jira", jira_project="APP", jira_label="repo-app"
        )
        self.cfg = config.Config(
            repos=[self.pkg, self.app, self.jira_app],
            jira=config.JiraConfig(project="APP", base_url="https://jira.example.test"),
        )
        self.trackers = FakePairTrackers()
        self.enterContext(mock.patch.object(gh, "gh", side_effect=self.trackers.github_call))
        self.enterContext(mock.patch.object(jira.Client, "from_env", return_value=self.trackers.client))
        self.enterContext(mock.patch.object(self.trackers.client, "_send", side_effect=self.trackers.jira_call))

    def _pair_entries(self, *, app_repo=None, ready_app=True):
        return [
            {
                "role": "package",
                "repo": self.pkg,
                "title": "Publish mypkg 1.2.0 contract",
                "body": "Export formatBadge(v) returning deterministic badge JSON.",
            },
            {
                "role": "app",
                "repo": app_repo or self.app,
                "title": "Consume mypkg 1.2.0 in app dashboard",
                "body": "Render formatBadge output on the user dashboard.",
                "ready": ready_app,
                "artifact": "mypkg:1.2.0",
            },
        ]

    def test_publish_pair_links_both_specs_and_creates_cross_repo_blocker(self):
        result = ticket_publication.plan_pair(self.cfg, self._pair_entries(), "pair-sync-001")
        self.assertTrue(result["linked"])
        pkg_res = result["specs"]["package"]
        app_res = result["specs"]["app"]
        self.assertTrue(pkg_res["published"])
        self.assertTrue(app_res["published"])
        self.assertEqual(pkg_res["issue"], "1")
        self.assertEqual(app_res["issue"], "1")
        # Both issues have independent spec + agent labels on their respective trackers.
        self.assertEqual({label["name"] for label in self.trackers.github["org/pkg"][1]["labels"]}, {"spec", "agent"})
        self.assertEqual({label["name"] for label in self.trackers.github["org/app"][1]["labels"]}, {"spec", "agent"})
        # Each links to its peer via tracker comments.
        pkg_comments = "\n".join(c["body"] for c in self.trackers.comments[("org/pkg", 1)])
        app_comments = "\n".join(c["body"] for c in self.trackers.comments[("org/app", 1)])
        self.assertIn("Linked spec: app https://github.com/org/app/issues/1", pkg_comments)
        self.assertIn("Linked spec: pkg https://github.com/org/pkg/issues/1", app_comments)
        self.assertIn("Requires spec: pkg https://github.com/org/pkg/issues/1 `@artifact=mypkg:1.2.0`", app_comments)
        # Native cross-repo GitHub blocking link recorded on app pointing to pkg.
        self.assertIn(("org/pkg", 1), self.trackers.blockers[("org/app", 1)])
        # Durable local links record independent roles and prerequisite metadata.
        app_links = cross_repo.links_for("app", "1")
        self.assertEqual(len(app_links), 1)
        self.assertTrue(app_links[0]["requires"])
        self.assertEqual(app_links[0]["artifact"], {"name": "mypkg", "version": "1.2.0"})
        pkg_links = cross_repo.links_for("pkg", "1")
        self.assertEqual(len(pkg_links), 1)
        self.assertFalse(pkg_links[0]["requires"])

    def test_publish_pair_partial_failure_and_idempotent_retry_reuses_published_work(self):
        entries = self._pair_entries()
        # First attempt: package publishes, app creation fails.
        self.trackers.fail_create_for.add("org/app")
        with self.assertRaises(gh.GhError):
            ticket_publication.plan_pair(self.cfg, entries, "pair-retry-001")
        self.assertEqual(len(self.trackers.github.get("org/pkg", {})), 1)
        self.assertEqual(len(self.trackers.github.get("org/app", {})), 0)

        # Second attempt: app publishes, peer-link comment on app fails once.
        self.trackers.fail_comment_once.add(("org/app", 1))
        with self.assertRaises(gh.GhError):
            ticket_publication.plan_pair(self.cfg, entries, "pair-retry-001")
        # Package was NOT duplicated!
        self.assertEqual(len(self.trackers.github["org/pkg"]), 1)
        self.assertEqual(len(self.trackers.github["org/app"]), 1)

        # Third attempt: completes linking without duplicating issues or comments.
        final = ticket_publication.plan_pair(self.cfg, entries, "pair-retry-001")
        self.assertTrue(final["linked"])
        self.assertEqual(len(self.trackers.github["org/pkg"]), 1)
        self.assertEqual(len(self.trackers.github["org/app"]), 1)
        self.assertEqual(len(self.trackers.comments[("org/pkg", 1)]), 1)
        self.assertEqual(len(self.trackers.comments[("org/app", 1)]), 1)

        # Fourth call with the same key returns cached response with zero new writes.
        replay = ticket_publication.plan_pair(self.cfg, entries, "pair-retry-001")
        self.assertEqual(replay, final)
        self.assertEqual(len(self.trackers.comments[("org/app", 1)]), 1)

        # Reusing the same key with different content is refused.
        changed = self._pair_entries()
        changed[0]["title"] = "Different title"
        with self.assertRaises(idempotency.Reused):
            ticket_publication.plan_pair(self.cfg, changed, "pair-retry-001")

    def test_publish_only_ready_package_spec_and_mixed_tracker_pair(self):
        # Publish only the ready package spec via ready_only.
        ready_result = ticket_publication.plan_pair(
            self.cfg, self._pair_entries(), "pair-ready-only-001", ready_only="package"
        )
        self.assertFalse(ready_result["linked"])
        self.assertTrue(ready_result["specs"]["package"]["published"])
        self.assertFalse(ready_result["specs"]["app"]["published"])
        self.assertEqual(len(self.trackers.github.get("org/pkg", {})), 1)
        self.assertEqual(len(self.trackers.github.get("org/app", {})), 0)

        # Also support a cross-tracker GitHub (package) + Jira (consuming app) pair.
        mixed = ticket_publication.plan_pair(
            self.cfg, self._pair_entries(app_repo=self.jira_app), "pair-mixed-001"
        )
        self.assertTrue(mixed["linked"])
        self.assertEqual(mixed["specs"]["package"]["tracker"], "github")
        self.assertEqual(mixed["specs"]["app"]["tracker"], "jira")
        self.assertEqual(mixed["specs"]["app"]["issue"], "APP-1")
        jira_notes = "\n".join(c["body"] for c in self.trackers.jira_comments["APP-1"])
        self.assertIn("Requires spec: pkg https://github.com/org/pkg/issues/2 @artifact=mypkg:1.2.0", jira_notes)

    def test_cli_publish_pair_command(self):
        spec_file = self.root / "pair.json"
        spec_file.write_text(json.dumps({
            "specs": [
                {"role": "package", "repo": "pkg", "title": "Pkg CLI", "body": "Ship package."},
                {"role": "app", "repo": "app", "title": "App CLI", "body": "Use package.", "artifact": "mypkg:1.2.0"},
            ]
        }))
        parser = cli.parser()
        out, err = StringIO(), StringIO()
        args = parser.parse_args([
            "repo", "publish-pair", "--spec-file", str(spec_file), "--key", "cli-pair-key-01", "--json"
        ])
        with (mock.patch.object(config, "load_required", return_value=self.cfg),
              redirect_stdout(out), redirect_stderr(err)):
            rc = args.fn(args)
        self.assertEqual(rc, 0, err.getvalue())
        data = json.loads(out.getvalue())
        self.assertTrue(data["linked"])
        self.assertEqual(data["specs"]["package"]["repo"], "pkg")
        self.assertEqual(data["specs"]["app"]["repo"], "app")


class CrossRepoCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=paths.STATE_DIR)))
        self.pkg = _make_repo(self.root, "pkg", "org/pkg")
        self.app = _make_repo(self.root, "app", "org/app")
        self.cfg = config.Config(repos=[self.pkg, self.app])

    def test_spec_tracker_adapter_wires_allowed_repos_and_jira_projects(self):
        app_tk = tracker.for_repo(self.cfg, self.app)
        remote = spec_tracker.adapter(app_tk)
        self.assertEqual(remote.allowed_repos, {"org/app", "org/pkg"})
        self.assertEqual(remote.ref("org/pkg#10"), "org/pkg#10")
        with self.assertRaises(spec_tracker.Held):
            remote.ref("org/untracked#99")

        j_pkg = _make_repo(self.root, "jpkg", "org/jpkg", tracker_kind="jira", jira_project="PKG", jira_label="repo-pkg")
        j_app = _make_repo(self.root, "japp", "org/japp", tracker_kind="jira", jira_project="APP", jira_label="repo-app")
        jcfg = config.Config(
            repos=[j_pkg, j_app],
            jira=config.JiraConfig(project="APP", base_url="https://jira.example.test"),
        )
        jclient = jira.Client("https://jira.example.test", "fixture", "fixture")
        j_tk = tracker.for_repo(jcfg, j_app)
        j_tk._client = jclient
        j_remote = spec_tracker.adapter(j_tk)
        self.assertEqual(j_remote.allowed_projects, {"APP", "PKG"})
        self.assertEqual(j_remote.ref("PKG-12"), "PKG-12")
        with self.assertRaises(spec_tracker.Held):
            j_remote.ref("OTHER-1")

    def test_consuming_app_waits_for_released_package_version_and_verifies_independently(self):
        pkg_ticket = tracker.Ticket(
            id="10", ref="#10", title="Release mypkg 1.2.0", body="Ship mypkg 1.2.0 contract.",
            open=True, labels=["spec", "agent"],
            spec_identity=tracker.github_issue_identity("org/pkg", "10"), owner_comments="",
        )
        app_ticket = tracker.Ticket(
            id="20", ref="#20", title="Integrate mypkg 1.2.0 in app",
            body="Verify integrated user action with mypkg 1.2.0.\nRequires spec: pkg org/pkg#10@artifact=mypkg:1.2.0",
            open=True, labels=["spec", "agent"], github_blockers=["org/pkg#10"],
            spec_identity=tracker.github_issue_identity("org/app", "20"), owner_comments="",
        )
        cross_repo.record_link(
            "app", "20", "pkg", "10",
            url="https://github.com/org/app/issues/20",
            peer_url="https://github.com/org/pkg/issues/10",
            role="app", requires=True, need="artifact",
            artifact={"name": "mypkg", "version": "1.2.0"},
        )
        pkg_tk = tracker.for_repo(self.cfg, self.pkg)
        app_tk = tracker.for_repo(self.cfg, self.app)

        # Before package lands: consuming app spec is blocked waiting for exact artifact version.
        why = tracker.spec_blocker(self.cfg, app_tk, app_ticket)
        self.assertIn("waiting for linked spec pkg#10 to reach its required mypkg 1.2.0", why)

        # Package publishes and merges PR, but no release proof yet -> app STILL blocked.
        record.start("pipeline", run_id="run-pkg-10", repo="pkg", issue="10")
        record.settle("run-pkg-10", "done", summary="Published pkg PR")
        specs.bind(pkg_ticket.spec_identity, "pkg", "10", "", lambda: "run-pkg-10")
        pkg_done = {"criterion": "mypkg 1.2.0 exports formatBadge", "procedure": "check release", "environment": "production"}
        pkg_receipt = {
            "definition": pkg_done,
            "source_format": 2,
            "source": spec_delivery.source_version(pkg_ticket),
            "policy": "p1",
            "acceptance": "a1",
            "landing": [],
            "targets": [{"pr": "https://github.com/org/pkg/pull/101", "repo": "org/pkg",
                         "base": "main", "tree": SHA_PKG_TREE, "head": SHA_PKG}],
            "behavior": {"how": "ran contract check", "saw": "formatBadge v1.2.0 ok"},
        }
        specs.advance(pkg_ticket.spec_identity, "run-pkg-10", "building", "published", pkg_receipt)

        merged_pkg_pr = {
            "state": "MERGED", "mergedAt": "2026-11-27T10:00:00Z",
            "mergeCommit": {"oid": SHA_PKG}, "headRefOid": SHA_PKG, "baseRefName": "main",
        }
        with (mock.patch.object(gh, "pr_view", return_value=merged_pkg_pr),
              mock.patch.object(spec_delivery, "revision", return_value=(SHA_PKG, SHA_PKG_TREE))):
            # Wrong version released (1.1.0 instead of 1.2.0): app still waits!
            _seed_release_proof(
                self.pkg, "10", SHA_PKG, receipt_id="rcpt-pkg-wrong",
                observed=[
                    {"field": "/name", "value_sha256": release_proof.digest("mypkg")},
                    {"field": "/version", "value_sha256": release_proof.digest("1.1.0")},
                ],
            )
            self.assertIn("mypkg 1.2.0", tracker.spec_blocker(self.cfg, app_tk, app_ticket))

            # Correct version released (1.2.0): package settles and closes independently!
            _seed_release_proof(
                self.pkg, "10", SHA_PKG, receipt_id="rcpt-pkg-120",
                observed=[
                    {"field": "/name", "value_sha256": release_proof.digest("mypkg")},
                    {"field": "/version", "value_sha256": release_proof.digest("1.2.0")},
                ],
            )
            closed_issues = []
            posted_comments = []

            def fake_comment(slug, num, body, return_url=False, single_attempt=False):
                posted_comments.append((slug, num, body))
                return f"{gh.issue_url(slug, num)}#issuecomment-1"

            def fake_close(slug, num):
                closed_issues.append((slug, num))
                if slug == "org/pkg" and num == 10:
                    pkg_ticket.open = False
                if slug == "org/app" and num == 20:
                    app_ticket.open = False

            pkg_claim = specs.find("pkg", "10")
            with (mock.patch.object(tracker, "for_ticket", return_value=pkg_tk),
                  mock.patch.object(pkg_tk, "get", return_value=pkg_ticket),
                  mock.patch.object(gh, "issue_comment", side_effect=fake_comment),
                  mock.patch.object(gh, "issue_close", side_effect=fake_close)):
                spec_delivery.settle(self.cfg, self.pkg, pkg_claim)

            self.assertEqual(specs.find("pkg", "10")["state"], "complete")
            self.assertEqual(closed_issues, [("org/pkg", 10)])
            # Package release closed ONLY pkg#10; app#20 MUST remain open!
            self.assertTrue(app_ticket.open)

            # Now app#20 is unblocked (including its native GitHub blocker) and receives exact version provenance.
            self.assertEqual(tracker.spec_blocker(self.cfg, app_tk, app_ticket), "")
            self.assertEqual(app_tk.ticket_wait_blocker(app_ticket, include_environment=False), "")
            peers = cross_repo.consumed_peers_context(self.cfg, self.app, app_ticket)
            self.assertEqual(len(peers), 1)
            self.assertEqual(peers[0]["artifact"], {"name": "mypkg", "version": "1.2.0"})
            self.assertEqual(peers[0]["receipt_id"], "rcpt-pkg-120")

            # Consuming app's own orchestrator builds, verifies integrated user action, and lands its PR.
            record.start("pipeline", run_id="run-app-20", repo="app", issue="20")
            record.settle("run-app-20", "done", summary="Verified integrated app action")
            specs.bind(app_ticket.spec_identity, "app", "20", "", lambda: "run-app-20")
            app_done = {"criterion": "User sees formatted badge from mypkg 1.2.0",
                        "procedure": "Load dashboard", "environment": ""}
            app_receipt = {
                "definition": app_done,
                "source_format": 2,
                "source": spec_delivery.source_version(app_ticket),
                "policy": "p2",
                "acceptance": "a2",
                "landing": [],
                "targets": [{"pr": "https://github.com/org/app/pull/201", "repo": "org/app",
                             "base": "main", "tree": SHA_APP_TREE, "head": SHA_APP}],
                "behavior": {"how": "loaded dashboard", "saw": "badge rendered with mypkg 1.2.0"},
                "consumed_peers": peers,
            }
            specs.advance(app_ticket.spec_identity, "run-app-20", "building", "published", app_receipt)

            merged_app_pr = {
                "state": "MERGED", "mergedAt": "2026-11-27T11:00:00Z",
                "mergeCommit": {"oid": SHA_APP}, "headRefOid": SHA_APP, "baseRefName": "main",
            }
            app_claim = specs.find("app", "20")
            with (mock.patch.object(gh, "pr_view", return_value=merged_app_pr),
                  mock.patch.object(spec_delivery, "revision", return_value=(SHA_APP, SHA_APP_TREE)),
                  mock.patch.object(tracker, "for_ticket", return_value=app_tk),
                  mock.patch.object(app_tk, "get", return_value=app_ticket),
                  mock.patch.object(gh, "issue_comment", side_effect=fake_comment),
                  mock.patch.object(gh, "issue_close", side_effect=fake_close)):
                spec_delivery.settle(self.cfg, self.app, app_claim)

            self.assertEqual(specs.find("app", "20")["state"], "complete")
            self.assertFalse(app_ticket.open)
            self.assertIn(("org/app", 20), closed_issues)
            app_delivery_note = next(body for slug, num, body in posted_comments if slug == "org/app" and num == 20)
            self.assertIn("mypkg==1.2.0", app_delivery_note)
            self.assertIn("rcpt-pkg-120", app_delivery_note)


class ExplicitTwoRepoSpecTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=paths.STATE_DIR)))
        self.pkg = _make_repo(self.root, "pkg", "org/pkg")
        self.app = _make_repo(self.root, "app", "org/app")
        self.infra = _make_repo(self.root, "infra", "org/infra")
        self.cfg = config.Config(repos=[self.pkg, self.app, self.infra])

    def test_single_two_repo_spec_authorizes_both_and_collects_distinct_per_repo_evidence(self):
        ticket = tracker.Ticket(
            id="30", ref="#30", title="Ship shared schema in pkg and app",
            body="Authorized repos: pkg, app\n\nUpdate schema in pkg and consume it in app.",
            open=True, labels=["spec", "agent"],
            spec_identity=tracker.github_issue_identity("org/pkg", "30"), owner_comments="",
        )
        pkg_tk = tracker.for_repo(self.cfg, self.pkg)
        self.assertEqual(tracker.spec_blocker(self.cfg, pkg_tk, ticket), "")

        # Also verify Jira multi-repo spec removes the 1-repo rejection when explicitly authorized,
        # while still rejecting unauthorized multi-route Jira specs and preventing duplicate claims.
        j_pkg = _make_repo(self.root, "jpkg", "org/jpkg", tracker_kind="jira", jira_project="TEAM", jira_label="repo-pkg")
        j_app = _make_repo(self.root, "japp", "org/japp", tracker_kind="jira", jira_project="TEAM", jira_label="repo-app")
        jcfg = config.Config(repos=[j_pkg, j_app], jira=config.JiraConfig(project="TEAM"))
        j_pkg_tk = tracker.for_repo(jcfg, j_pkg)
        j_app_tk = tracker.for_repo(jcfg, j_app)
        j_ticket = tracker.Ticket(
            id="TEAM-30", ref="TEAM-30", title="Two-repo Jira spec",
            body="Authorized repos: jpkg, japp\n\nChange both.",
            open=True, labels=["spec", "agent", "repo-pkg", "repo-app"],
            spec_identity=tracker.jira_issue_identity("https://jira.example.test", "300"),
        )
        self.assertEqual(tracker.spec_blocker(jcfg, j_pkg_tk, j_ticket), "")
        self.assertFalse(poll._conflicts_other_routes(j_pkg, j_pkg_tk, j_ticket, {"repo-app"}))
        self.assertIn("orchestrated by jpkg", tracker.spec_blocker(jcfg, j_app_tk, j_ticket))
        self.assertTrue(poll._conflicts_other_routes(j_app, j_app_tk, j_ticket, {"repo-pkg"}))

        unauth_jira = tracker.Ticket(
            id="TEAM-31", ref="TEAM-31", title="Ambiguous Jira spec", body="No explicit repos.",
            open=True, labels=["spec", "agent", "repo-pkg", "repo-app"],
            spec_identity=tracker.jira_issue_identity("https://jira.example.test", "301"),
        )
        self.assertIn("exactly one", tracker.spec_blocker(jcfg, j_pkg_tk, unauth_jira))

        # Now exercise candidate() + at_delivery() + settle() with per-repo release evidence for pkg and app.
        done = {"criterion": "Both pkg and app serve v2 schema", "procedure": "Check both services", "environment": "production"}
        _seed_release_proof(self.pkg, "30", SHA_PKG, path="/pkg/schema", receipt_id="rcpt-pkg-30")
        # Initially only pkg has a verified release proof; app has an enrolled policy without a verified proof yet.
        app_policy = {
            "destination": "production",
            "path": "/app/schema",
            "identity_path": "/version.json",
            "production": "https://release.example.test",
            "instructions": "Verify the released contract.",
            "assertions": [{"field": "/ready", "equals": True}],
            "recovery": [{"field": "/ready", "equals": True}],
        }
        serve_guard.write(release_proof.policy_file("app", "30"), app_policy)

        c_pkg = workspace.Checkout("pkg", "org/pkg", self.pkg.path, self.root / "ws" / "pkg", "main",
                                   pr="https://github.com/org/pkg/pull/301", lease=SHA_PKG)
        c_app = workspace.Checkout("app", "org/app", self.app.path, self.root / "ws" / "app", "main",
                                   pr="https://github.com/org/app/pull/302", lease=SHA_APP)
        verified = verify.Verified(
            verdict="pass", finished=True, behavior_reached=True,
            checks=[{"what": done["criterion"], "how": "checked both", "saw": "v2 ok", "passed": True}],
        )
        review = SimpleNamespace(approved=lambda: True)
        run = SimpleNamespace(repo=self.pkg, tracker=pkg_tk, spec_identity=ticket.spec_identity)
        job = SimpleNamespace(
            cfg=self.cfg, run=run, ticket=ticket, checkouts=[c_pkg, c_app],
            with_prs=[c_pkg, c_app], verified=verified, review=review,
        )

        def fake_git(args, cwd):
            if cwd == c_pkg.wt:
                return SHA_PKG_TREE
            if cwd == c_app.wt:
                return SHA_APP_TREE
            raise AssertionError(cwd)

        with mock.patch.object(spec_delivery.util, "git", side_effect=fake_git):
            receipt = spec_delivery.candidate(
                job, done, "pol", "acc", {"pkg/": SHA_PKG_TREE, "app/": SHA_APP_TREE}
            )
        self.assertEqual(receipt["authorized_repos"], ["pkg", "app"])
        self.assertEqual(set(receipt["delivery_obligations"]), {"pkg", "app"})

        record.start("pipeline", run_id="run-two-30", repo="pkg", issue="30")
        record.settle("run-two-30", "done", summary="Published both PRs")
        specs.bind(ticket.spec_identity, "pkg", "30", "", lambda: "run-two-30")
        specs.advance(ticket.spec_identity, "run-two-30", "building", "published", receipt)

        def fake_pr_view(url):
            if "org/pkg" in url:
                return {"state": "MERGED", "mergedAt": "2026-11-27T10:00:00Z",
                        "mergeCommit": {"oid": SHA_PKG}, "headRefOid": SHA_PKG, "baseRefName": "main"}
            return {"state": "MERGED", "mergedAt": "2026-11-27T10:05:00Z",
                    "mergeCommit": {"oid": SHA_APP}, "headRefOid": SHA_APP, "baseRefName": "main"}

        def fake_revision(slug, ref):
            return (SHA_PKG, SHA_PKG_TREE) if slug == "org/pkg" else (SHA_APP, SHA_APP_TREE)

        claim = specs.find("pkg", "30")
        with (mock.patch.object(gh, "pr_view", side_effect=fake_pr_view),
              mock.patch.object(spec_delivery, "revision", side_effect=fake_revision)):
            # App lacks release evidence -> spec MUST stay open!
            self.assertEqual(spec_delivery.at_delivery(claim, pkg_tk, cfg=self.cfg), {})

            # Now record app's distinct release proof -> spec settles with per-repo evidence!
            _seed_release_proof(self.app, "30", SHA_APP, path="/app/schema", receipt_id="rcpt-app-30")
            posted = []
            closed = []

            def fake_comment(slug, num, body, return_url=False, single_attempt=False):
                posted.append(body)
                return f"{gh.issue_url(slug, num)}#issuecomment-30"

            def fake_close(slug, num):
                closed.append((slug, num))
                ticket.open = False

            with (mock.patch.object(tracker, "for_ticket", return_value=pkg_tk),
                  mock.patch.object(pkg_tk, "get", return_value=ticket),
                  mock.patch.object(gh, "issue_comment", side_effect=fake_comment),
                  mock.patch.object(gh, "issue_close", side_effect=fake_close)):
                spec_delivery.settle(self.cfg, self.pkg, claim)

        settled = specs.find("pkg", "30")
        self.assertEqual(settled["state"], "complete")
        self.assertEqual(closed, [("org/pkg", 30)])
        per_repo = settled["data"]["delivery"]["per_repo"]
        self.assertEqual([entry["repo"] for entry in per_repo], ["pkg", "app"])
        self.assertEqual([entry["receipt_id"] for entry in per_repo], ["rcpt-pkg-30", "rcpt-app-30"])
        self.assertIn("rcpt-pkg-30", posted[0])
        self.assertIn("rcpt-app-30", posted[0])
        self.assertIn("Per-repository release evidence", posted[0])

    def test_single_two_repo_spec_stops_and_asks_before_expanding_into_third_repo(self):
        ticket = tracker.Ticket(
            id="40", ref="#40", title="Two-repo change",
            body="Authorized repos: pkg, app\n\nImplement shared API.",
            open=True, labels=["spec", "agent"],
            spec_identity=tracker.github_issue_identity("org/pkg", "40"), owner_comments="",
        )
        # Authorized repos pass cleanly; 3rd repo ("infra") is caught.
        authorized, unauth_ok = cross_repo.check_repo_scope(self.cfg, self.pkg, ticket, ["pkg", "org/app"])
        self.assertEqual([r.name for r in authorized], ["pkg", "app"])
        self.assertEqual(unauth_ok, [])

        _, unauth_third = cross_repo.check_repo_scope(self.cfg, self.pkg, ticket, ["pkg", "app", "org/infra"])
        self.assertEqual(unauth_third, ["infra"])

        # Generated owner ask passes owner_asks validation.
        ask_req = cross_repo.expansion_ask_request(authorized, unauth_third)
        validated, errors = owner_asks.validate(ask_req)
        self.assertEqual(errors, [])
        self.assertEqual(validated["questions"][0]["header"], "Repo scope")
        self.assertIn("infra", validated["questions"][0]["question"])

        # Controller stops and asks the owner via orchestrate_ask before touching infra.
        orch = object.__new__(orchestration.Controller)
        orch.lease = SimpleNamespace(root="spec-root-40")
        orch.run = SimpleNamespace(
            id="run-40", repo=self.pkg, tid="40", spec_root="spec-root-40",
            dry_run=False, alone=False, owner_questions_blocked=False,
        )
        orch.job = SimpleNamespace(cfg=self.cfg, ticket=ticket)
        orch.data = {"superseded_actions": ()}
        orch.ending = None
        asked_calls = []

        def fake_atomic(step, **kwargs):
            asked_calls.append((step, kwargs))
            return SimpleNamespace(data={"asked": True})

        orch.atomic = fake_atomic
        worker_started = []
        orch.worker_start = lambda action, args: worker_started.append(args)

        with mock.patch.object(orchestration.ask_store, "carry_scope_ask", return_value=None):
            res = orch.perform({
                "id": "act-1",
                "op": "worker.start",
                "request": {"goal": "Update infra", "write_paths": ["infra/deploy.yml"]},
            })

        self.assertTrue(res["scope_expansion_requested"])
        self.assertEqual(worker_started, [])
        self.assertEqual(orch.ending[0], "needs-info")
        self.assertIn("infra", orch.ending[1])
        self.assertEqual(len(asked_calls), 1)
        self.assertEqual(asked_calls[0][0], "orchestrate_ask")
        _, ask_errors = owner_asks.validate(asked_calls[0][1]["request"])
        self.assertEqual(ask_errors, [])

        # spec_delivery.candidate also refuses any checkout outside authorized repos.
        done = {"criterion": "done", "procedure": "check", "environment": ""}
        c_infra = workspace.Checkout("infra", "org/infra", self.infra.path, self.root / "ws" / "infra", "main",
                                     pr="https://github.com/org/infra/pull/1", lease=SHA_INFRA)
        verified = verify.Verified(
            verdict="pass", finished=True, behavior_reached=True,
            checks=[{"what": "done", "how": "checked", "saw": "ok", "passed": True}],
        )
        bad_job = SimpleNamespace(
            cfg=self.cfg,
            run=SimpleNamespace(repo=self.pkg, tracker=tracker.for_repo(self.cfg, self.pkg),
                              spec_identity=ticket.spec_identity),
            ticket=ticket,
            checkouts=[c_infra],
            with_prs=[c_infra],
            verified=verified,
            review=SimpleNamespace(approved=lambda: True),
        )
        with self.assertRaisesRegex(ValueError, "outside spec authorization: infra"):
            spec_delivery.candidate(bad_job, done, "pol", "acc", {"infra/": SHA_INFRA_TREE})
