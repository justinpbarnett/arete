import contextlib
import tempfile
import unittest
from tests import support
from pathlib import Path
from unittest import mock

from support import isolate_live_state

from arete import (
    adf,
    config,
    gh,
    jira,
    paths,
    ticket_feedback as tf,
    ticket_followups as tfu,
    tracker,
)
from arete.store import db as foundation
from arete.store import feedback as feedback_store
from arete.store import specs as spec_store

isolate_live_state()


class _FollowUpDbCase(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="arete-fup-"))).resolve()
        self.root = root
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        state = root / "state"
        for name, value in {
            "STATE_DIR": state,
            "DB_FILE": state / "arete.db",
            "CONFIG_DIR": root / "config",
            "CONFIG_FILE": root / "config" / "config.toml",
            "RUNS_DIR": state / "runs",
            "WORKTREES_DIR": state / "worktrees",
            "LOCKS_DIR": state / "locks",
            "LOG_DIR": state / "logs",
        }.items():
            patches.enter_context(mock.patch.object(paths, name, value))
        patches.enter_context(mock.patch.object(foundation, "_CHECKED", set()))
        paths.ensure_dirs()
        foundation.migrate()


class _FakeGitHub:
    """In-memory deterministic GitHub tracker double with injectable lost-ack faults."""

    def __init__(self, slug: str, start_number: int = 41):
        self.slug = slug
        self.next_number = start_number
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.next_comment_id = 500
        self.create_calls = 0
        self.comment_calls = 0
        self.fail_create_after_accept: set[int] = set()
        self.fail_create_before_accept: set[int] = set()
        self.fail_comment_after_accept: set[int] = set()

    def seed_issue(self, number: int, title: str, body: str = "", *, state: str = "CLOSED",
                   labels: list[str] | None = None) -> None:
        self.issues[number] = {
            "number": number,
            "id": 10000 + number,
            "title": title,
            "body": body,
            "state": state,
            "url": gh.issue_url(self.slug, number),
            "html_url": gh.issue_url(self.slug, number),
            "labels": [{"name": lb} for lb in (labels or [])],
            "author": {"login": "owner"},
        }
        self.comments.setdefault(number, [])

    def ensure_label(self, repo: str, name: str, **kwargs) -> str:
        return ""

    def issue_create(self, repo: str, title: str, body: str, labels=(), **kwargs) -> int:
        self.create_calls += 1
        call_no = self.create_calls
        if call_no in self.fail_create_before_accept:
            raise gh.GhError("temporary rejected create before accept")
        num = self.next_number
        self.next_number += 1
        self.seed_issue(num, title, body, state="OPEN", labels=list(labels))
        if call_no in self.fail_create_after_accept:
            raise gh.GhUnsure("lost confirmation after GitHub accepted issue creation")
        return num

    def find_issue_marker(self, repo: str, marker: str) -> str:
        for row in self.issues.values():
            if marker in (row.get("body") or "").splitlines():
                return row["html_url"]
        return ""

    def issue_comment(self, repo: str, number: int, body: str, *, return_url: bool = False, **kwargs) -> str | None:
        self.comment_calls += 1
        call_no = self.comment_calls
        cid = self.next_comment_id
        self.next_comment_id += 1
        url = f"{gh.issue_url(repo, number)}#issuecomment-{cid}"
        self.comments.setdefault(number, []).append({
            "id": cid,
            "user": {"login": "owner"},
            "body": body,
            "html_url": url,
            "created_at": f"2026-11-27T12:00:{cid % 60:02d}Z",
        })
        if call_no in self.fail_comment_after_accept:
            raise gh.GhUnsure("lost confirmation after GitHub accepted comment")
        return url if return_url else None

    def fetch_comments(self, repo: config.Repo, tk: tracker.Tracker, tid: str) -> list[tf.ThreadComment]:
        return [tf.normalize_github_comment(repo, str(tid), row) for row in self.comments.get(int(tid), [])]

    def issue_view(self, repo: str, number: int) -> dict:
        row = self.issues[int(number)]
        return {**row, "comments": self.comments.get(int(number), [])}

    def patches(self):
        return contextlib.ExitStack()

    def install(self, stack: contextlib.ExitStack) -> None:
        stack.enter_context(mock.patch.object(gh, "ensure_label", side_effect=self.ensure_label))
        stack.enter_context(mock.patch.object(gh, "issue_create", side_effect=self.issue_create))
        stack.enter_context(mock.patch.object(gh, "find_issue_marker", side_effect=self.find_issue_marker))
        stack.enter_context(mock.patch.object(gh, "issue_comment", side_effect=self.issue_comment))
        stack.enter_context(mock.patch.object(gh, "issue", side_effect=self.issue_view))
        stack.enter_context(mock.patch.object(tf, "fetch_thread_comments", side_effect=self.fetch_comments))


class _FakeJira(jira.Client):
    """In-memory deterministic Jira client double with native issueLink support and injectable faults."""

    def __init__(self, base_url: str = "https://jira.example.test", project: str = "TEAM", start_num: int = 41):
        super().__init__(base_url, "bot@example.test", "secret-token")
        self.project = project
        self.next_num = start_num
        self.issues: dict[str, dict] = {}
        self.comments_by_key: dict[str, list[dict]] = {}
        self.links: list[dict] = []
        self.next_comment_id = 900
        self.create_calls = 0
        self.link_calls = 0
        self.comment_calls = 0
        self.move_calls = 0
        self.reject_bug_type = False
        self.fail_create_after_accept: set[int] = set()
        self.fail_create_before_accept: set[int] = set()
        self.fail_link_after_accept: set[int] = set()
        self.fail_comment_after_accept: set[int] = set()

    def seed_issue(self, key: str, summary: str, description: str = "", *, done: bool = True,
                   issue_type: str = "Task", labels: list[str] | None = None) -> None:
        self.issues[key] = {
            "id": str(20000 + len(self.issues)),
            "key": key,
            "fields": {
                "project": {"key": self.project},
                "summary": summary,
                "description": description,
                "issuetype": {"name": issue_type, "subtask": False},
                "labels": list(labels or []),
                "status": {"name": "Done" if done else "To Do",
                           "statusCategory": {"key": "done" if done else "new"}},
                "assignee": {"accountId": "bot-acct"},
                "creator": {"accountId": "bot-acct"},
            },
        }
        self.comments_by_key.setdefault(key, [])

    def me(self) -> dict:
        return {"accountId": "bot-acct"}

    def issue(self, key: str, fields: list[str]) -> dict:
        row = self.issues[key]
        out_fields = dict(row["fields"])
        if "issuelinks" in fields:
            out_fields["issuelinks"] = [
                {
                    "type": lk["type"],
                    "inwardIssue": {"key": lk["inward"]},
                    "outwardIssue": {"key": lk["outward"]},
                }
                for lk in self.links
                if key in (lk["inward"], lk["outward"])
            ]
        return {"id": row["id"], "key": row["key"], "fields": out_fields}

    def comments(self, key: str) -> list[dict]:
        return list(self.comments_by_key.get(key, []))

    def find_task_marker(self, project: str, account: str, marker: str) -> str:
        for key, row in self.issues.items():
            desc = row["fields"].get("description") or ""
            text = adf.to_text(desc) if isinstance(desc, dict) else desc
            if marker in text.splitlines():
                return self.browse_url(key)
        return ""

    def get(self, path: str, **params):
        if path == "/rest/api/3/issueLinkType":
            return {
                "issueLinkTypes": [
                    {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"},
                ]
            }
        raise AssertionError(f"unexpected GET {path}")

    def request(self, method: str, path: str, params=None, body=None, *, single_attempt=False):
        if method == "POST" and path == "/rest/api/3/issue":
            self.create_calls += 1
            call_no = self.create_calls
            if call_no in self.fail_create_before_accept:
                raise jira.JiraError("rejected before create", status=503)
            fields = body["fields"]
            itype = (fields.get("issuetype") or {}).get("name", "Task")
            if self.reject_bug_type and itype.casefold() == "bug":
                raise jira.JiraError("issuetype 'Bug' is not valid for this project", status=400)
            key = f"{self.project}-{self.next_num}"
            self.next_num += 1
            desc_text = adf.to_text(fields.get("description"))
            self.seed_issue(
                key,
                fields["summary"],
                desc_text,
                done=False,
                issue_type=itype,
                labels=list(fields.get("labels") or []),
            )
            if call_no in self.fail_create_after_accept:
                raise jira.JiraUnsure("lost confirmation after Jira created issue")
            return {"id": self.issues[key]["id"], "key": key}
        if method == "POST" and path == "/rest/api/3/issueLink":
            self.link_calls += 1
            call_no = self.link_calls
            ltype = body["type"]
            kind = ({"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"}
                    if ltype.get("id") == "10000"
                    else {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"})
            self.links.append({
                "type": kind,
                "inward": body["inwardIssue"]["key"],
                "outward": body["outwardIssue"]["key"],
            })
            if call_no in self.fail_link_after_accept:
                raise jira.JiraUnsure("lost confirmation after Jira created issueLink")
            return None
        raise AssertionError(f"unexpected {method} {path}")

    def comment(self, key: str, markdown: str, *, return_url: bool = False, single_attempt: bool = False) -> str | None:
        self.comment_calls += 1
        call_no = self.comment_calls
        cid = str(self.next_comment_id)
        self.next_comment_id += 1
        self.comments_by_key.setdefault(key, []).append({
            "id": cid,
            "author": {"accountId": "bot-acct", "displayName": "Arete"},
            "body": markdown,
            "created": f"2026-11-27T12:10:{int(cid) % 60:02d}.000+0000",
        })
        if call_no in self.fail_comment_after_accept:
            raise jira.JiraUnsure("lost confirmation after Jira accepted comment")
        return self.comment_url(key, cid) if return_url else None

    def move(self, key: str, target: str, *, category: str = "") -> bool:
        self.move_calls += 1
        return True


class ClassificationAndSplittingTests(unittest.TestCase):
    def setUp(self):
        self.delivered = tfu.DeliveredOutcome(
            repo="product",
            ticket="40",
            tracker="github",
            title="Persist user settings on Save",
            body="Clicking Save preserves the selected timezone and profile preferences across reload.",
            url="https://github.com/owner/product/issues/40",
        )

    def test_distinguishes_bug_from_change_request_against_delivered_outcome(self):
        self.assertEqual(
            tfu.classify_request("Save loses the previously selected timezone on reload.", self.delivered),
            "bug",
        )
        self.assertEqual(
            tfu.classify_request("Please add a new CSV export for the settings table.", self.delivered),
            "change_request",
        )
        self.assertEqual(
            tfu.classify_request("Timezone preference is missing after clicking Save.", self.delivered),
            "bug",
        )
        self.assertEqual(
            tfu.classify_request("Instead of showing UTC by default, switch the default to local time.", self.delivered),
            "change_request",
        )

    def test_splits_mixed_requests_without_oversplitting_single_bug_evidence(self):
        mixed = tfu.analyze_requests(
            "Save loses a previously supported value and also request a new CSV export.",
            self.delivered,
        )
        self.assertEqual([(m.kind, m.depends_on) for m in mixed], [("bug", ()), ("change_request", ())])
        self.assertIn("CSV export", mixed[1].outcome_label)

        single_with_repro = tfu.analyze_requests(
            "Save is broken and loses the selected timezone. I tested in Chrome after reloading and it resets to UTC.",
            self.delivered,
        )
        self.assertEqual(len(single_with_repro), 1)
        self.assertEqual(single_with_repro[0].kind, "bug")

        explicit_dep = tfu.analyze_requests(
            "Save loses the timezone field. Also, after fixing that, add a CSV export of saved timezones.",
            self.delivered,
        )
        self.assertEqual(len(explicit_dep), 2)
        self.assertEqual(explicit_dep[1].depends_on, (0,))

    def test_requester_reply_uses_singular_and_plural_plain_language_with_clickable_links(self):
        single = tfu.format_requester_reply([{
            "kind": "bug", "action": "create", "outcome_label": "Save regression",
            "target_ref": "#41", "target_url": "https://github.com/o/r/issues/41",
        }])
        self.assertIn("implementing your request in a new ticket", single)
        self.assertIn("Save regression", single)
        self.assertIn("[#41](https://github.com/o/r/issues/41)", single)

        plural = tfu.format_requester_reply([
            {"kind": "bug", "action": "create", "outcome_label": "Save fix",
             "target_ref": "#41", "target_url": "https://github.com/o/r/issues/41"},
            {"kind": "change_request", "action": "create", "outcome_label": "CSV export",
             "target_ref": "#42", "target_url": "https://github.com/o/r/issues/42"},
        ])
        self.assertIn("implementing your requests in two new tickets", plural)
        self.assertIn("[#41](https://github.com/o/r/issues/41)", plural)
        self.assertIn("[#42](https://github.com/o/r/issues/42)", plural)


class GitHubFollowUpTests(_FollowUpDbCase):
    def _seed_completed_ticket(self, repo: config.Repo, tid: str) -> tracker.Ticket:
        rid = support.start_pipeline(repo=repo.name, issue=tid, status="merged")
        with foundation.tx() as c:
            c.execute("UPDATE runs SET started = ? WHERE id = ?", ("2026-11-27T09:00:00Z", rid))
        identity = tracker.github_issue_identity(repo.github, tid)
        spec_store.bind(identity, repo.name, tid, "", lambda: rid)
        spec_store.advance(identity, rid, "building", "complete", {"retro": "recorded"})
        return tracker.Ticket(
            tid, f"#{tid}", "Persist settings on Save",
            body="Save preserves selected timezone across reload.",
            open=False, url=gh.issue_url(repo.github, tid), labels=["agent"],
        )

    def test_single_bug_and_single_change_request_keep_original_done_and_link_back(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        original = self._seed_completed_ticket(repo, "40")
        fake = _FakeGitHub("owner/product", start_number=41)
        fake.seed_issue(40, original.title, original.body, state="CLOSED", labels=["agent"])

        bug_comment = tf.ThreadComment(
            "801", "owner", "owner", "Save loses the previously selected timezone after reload.",
            "https://github.com/owner/product/issues/40#issuecomment-801", "2026-11-27T11:00:00Z",
        )
        cr_comment = tf.ThreadComment(
            "802", "owner", "owner", "Please add a new CSV export for the settings table.",
            "https://github.com/owner/product/issues/40#issuecomment-802", "2026-11-27T11:05:00Z",
        )
        tf.consume_ticket(cfg, repo, "40", tk=tk, ticket=original, comments=[bug_comment, cr_comment])
        self.assertEqual(len(tf.pending_follow_ups("product")), 2)

        with contextlib.ExitStack() as stack:
            fake.install(stack)
            results = tfu.sync(cfg)

        self.assertEqual(len(results), 2)
        bug_res, cr_res = results
        self.assertEqual((bug_res.items[0].kind, bug_res.items[0].target_ref), ("bug", "#41"))
        self.assertEqual((cr_res.items[0].kind, cr_res.items[0].target_ref), ("change_request", "#42"))

        # Created tickets are correctly classified, discoverable (`agent`), and link directly back to #40.
        issue41, issue42 = fake.issues[41], fake.issues[42]
        self.assertEqual({lb["name"] for lb in issue41["labels"]}, {"bug", "agent"})
        self.assertEqual({lb["name"] for lb in issue42["labels"]}, {"change-request", "agent"})
        self.assertIn("[#40](https://github.com/owner/product/issues/40)", issue41["body"])
        self.assertIn("[#40](https://github.com/owner/product/issues/40)", issue42["body"])
        self.assertIn("### Definition of Done", issue41["body"])
        self.assertIn("### Definition of Done", issue42["body"])

        # Original stays Done with intact delivery record and clickable plain-language replies.
        self.assertEqual(fake.issues[40]["state"], "CLOSED")
        self.assertEqual(spec_store.find("product", "40")["state"], "complete")
        self.assertEqual(feedback_store.tickets_needing_worker("product"), [])
        self.assertEqual(tf.pending_follow_ups("product"), [])
        self.assertEqual(len(fake.comments[40]), 2)
        self.assertIn("[#41](https://github.com/owner/product/issues/41)", fake.comments[40][0]["body"])
        self.assertIn("implementing your request in a new ticket", fake.comments[40][0]["body"])
        self.assertIn("[#42](https://github.com/owner/product/issues/42)", fake.comments[40][1]["body"])

    def test_replay_after_lost_creation_ack_and_lost_reply_ack_converges_cleanly(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        original = self._seed_completed_ticket(repo, "40")
        fake = _FakeGitHub("owner/product", start_number=41)
        fake.seed_issue(40, original.title, original.body, state="CLOSED", labels=["agent"])
        fake.fail_create_after_accept.add(1)
        fake.fail_comment_after_accept.add(1)

        comment = tf.ThreadComment(
            "901", "owner", "owner", "Save drops the previously selected timezone.",
            "https://github.com/owner/product/issues/40#issuecomment-901", "2026-11-27T11:10:00Z",
        )
        tf.consume_ticket(cfg, repo, "40", tk=tk, ticket=original, comments=[comment])
        row = tf.pending_follow_ups("product")[0]

        with contextlib.ExitStack() as stack:
            fake.install(stack)
            # Attempt 1: GitHub accepts issue #41, then ack is lost -> raises GhUnsure.
            with self.assertRaises(gh.GhUnsure):
                tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertEqual(list(fake.issues.keys()), [40, 41])
            self.assertEqual(len(fake.comments[40]), 0)

            # Attempt 2: Reconciles #41 via marker (no 2nd issue!), posts reply, then reply ack is lost -> GhUnsure.
            with self.assertRaises(gh.GhUnsure):
                tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertEqual(list(fake.issues.keys()), [40, 41])
            self.assertEqual(fake.create_calls, 1)
            self.assertEqual(len(fake.comments[40]), 1)

            # Attempt 3: Reconciles reply via reply marker -> zero duplicate issues, zero duplicate replies!
            done = tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertEqual(done.status, "done")
            self.assertFalse(done.replayed)
            self.assertEqual(fake.create_calls, 1)
            self.assertEqual(fake.comment_calls, 1)
            self.assertEqual(len(fake.comments[40]), 1)
            self.assertIn("[#41](https://github.com/owner/product/issues/41)", fake.comments[40][0]["body"])

            # Attempt 4: Replay after completion is a pure no-op.
            again = tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertTrue(again.replayed)
            self.assertEqual((fake.create_calls, fake.comment_calls), (1, 1))


class JiraFollowUpTests(_FollowUpDbCase):
    def test_jira_bug_and_change_request_create_native_relates_backlinks_and_keep_original_done(self):
        repo = config.Repo(
            "teamapp", self.root, tracker="jira", jira_project="TEAM",
            jira_label="repo-teamapp", jira_site="https://jira.example.test", pipeline=True,
        )
        cfg = config.Config(repos=[repo])
        client = _FakeJira(project="TEAM", start_num=41)
        client.seed_issue("TEAM-40", "Persist settings on Save", "Save preserves timezone on reload.", done=True)
        tk = tracker.JiraTracker(repo, cfg.jira)
        original = tracker.Ticket(
            "TEAM-40", "TEAM-40", "Persist settings on Save",
            body="Save preserves timezone on reload.", open=False,
            url=client.browse_url("TEAM-40"),
        )

        bug_c = tf.ThreadComment(
            "301", "qa-maya", "Maya QA", "Save loses the previously selected timezone after reload.",
            client.comment_url("TEAM-40", "301"), "2026-11-27T11:00:00.000+0000",
        )
        cr_c = tf.ThreadComment(
            "302", "slack-app", "Slack Relay", "From @alex in #qa: Please add a new CSV export.",
            client.comment_url("TEAM-40", "302"), "2026-11-27T11:05:00.000+0000", relayed_by="Slack Relay",
        )
        with mock.patch.object(tracker.JiraTracker, "client", new_callable=mock.PropertyMock, return_value=client):
            tf.consume_ticket(cfg, repo, "TEAM-40", tk=tk, ticket=original, comments=[bug_c, cr_c], since="")
            results = tfu.sync(cfg)

        self.assertEqual(len(results), 2)
        self.assertEqual((results[0].items[0].kind, results[0].items[0].target_ticket), ("bug", "TEAM-41"))
        self.assertEqual((results[1].items[0].kind, results[1].items[0].target_ticket), ("change_request", "TEAM-42"))

        # Verify Jira issue types, route + classification + agent labels, and non-blocking Relates backlinks.
        f41 = client.issues["TEAM-41"]["fields"]
        f42 = client.issues["TEAM-42"]["fields"]
        self.assertEqual(f41["issuetype"]["name"], "Bug")
        self.assertEqual(f42["issuetype"]["name"], "Task")
        self.assertEqual(set(f41["labels"]), {"bug", "agent", "repo-teamapp"})
        self.assertEqual(set(f42["labels"]), {"change-request", "agent", "repo-teamapp"})
        self.assertEqual(
            [(lk["type"]["name"], lk["inward"], lk["outward"]) for lk in client.links],
            [("Relates", "TEAM-41", "TEAM-40"), ("Relates", "TEAM-42", "TEAM-40")],
        )
        # Original stays Done with zero transitions and clickable replies.
        self.assertEqual(client.move_calls, 0)
        self.assertEqual(client.issues["TEAM-40"]["fields"]["status"]["statusCategory"]["key"], "done")
        self.assertEqual(len(client.comments_by_key["TEAM-40"]), 2)
        self.assertIn("[TEAM-41](https://jira.example.test/browse/TEAM-41)", client.comments_by_key["TEAM-40"][0]["body"])
        self.assertIn("[TEAM-42](https://jira.example.test/browse/TEAM-42)", client.comments_by_key["TEAM-40"][1]["body"])

    def test_jira_bug_falls_back_to_task_with_bug_label_if_project_lacks_bug_issuetype(self):
        repo = config.Repo(
            "teamapp", self.root, tracker="jira", jira_project="TEAM",
            jira_label="repo-teamapp", jira_site="https://jira.example.test", pipeline=True,
        )
        cfg = config.Config(repos=[repo])
        client = _FakeJira(project="TEAM", start_num=60)
        client.reject_bug_type = True
        client.seed_issue("TEAM-59", "Persist settings on Save", "Save preserves timezone.", done=True)
        tk = tracker.JiraTracker(repo, cfg.jira)
        original = tracker.Ticket("TEAM-59", "TEAM-59", "Persist settings on Save", body="Save preserves timezone.",
                                  open=False, url=client.browse_url("TEAM-59"))
        c = tf.ThreadComment("310", "qa-1", "QA", "Save crashes with a 500 error.",
                             client.comment_url("TEAM-59", "310"), "2026-11-27T11:20:00.000+0000")
        with mock.patch.object(tracker.JiraTracker, "client", new_callable=mock.PropertyMock, return_value=client):
            tf.consume_ticket(cfg, repo, "TEAM-59", tk=tk, ticket=original, comments=[c], since="")
            res = tfu.sync(cfg)
        self.assertEqual(res[0].items[0].target_ticket, "TEAM-60")
        self.assertEqual(client.issues["TEAM-60"]["fields"]["issuetype"]["name"], "Task")
        self.assertIn("bug", client.issues["TEAM-60"]["fields"]["labels"])


class MixedRequestsAndDuplicateReportsTests(_FollowUpDbCase):
    def test_mixed_comment_splits_independently_and_duplicate_open_bug_reuses_ticket(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        original = tracker.Ticket(
            "50", "#50", "Persist settings on Save",
            body="Save preserves previously selected values across reloads.",
            open=False, url=gh.issue_url(repo.github, "50"), labels=["agent"],
        )
        fake = _FakeGitHub("owner/product", start_number=51)
        fake.seed_issue(50, original.title, original.body, state="CLOSED", labels=["agent"])

        # 1. Mixed comment: Save regression + new CSV export in ONE comment -> 2 independent tickets + 1 reply.
        c1 = tf.ThreadComment(
            "1001", "owner", "owner",
            "Save loses a previously supported value and also request a new CSV export.",
            "https://github.com/owner/product/issues/50#issuecomment-1001", "2026-11-27T11:00:00Z",
        )
        tf.consume_ticket(cfg, repo, "50", tk=tk, ticket=original, comments=[c1], since="")
        with contextlib.ExitStack() as stack:
            fake.install(stack)
            r1 = tfu.sync(cfg)

        self.assertEqual(len(r1), 1)
        self.assertEqual([(i.kind, i.action, i.target_ref) for i in r1[0].items], [
            ("bug", "create", "#51"),
            ("change_request", "create", "#52"),
        ])
        # Independent execution: neither new ticket waits on the other.
        self.assertNotIn("Waits on", fake.issues[51]["body"])
        self.assertNotIn("Waits on", fake.issues[52]["body"])
        self.assertIn("[#50](https://github.com/owner/product/issues/50)", fake.issues[51]["body"])
        self.assertIn("[#50](https://github.com/owner/product/issues/50)", fake.issues[52]["body"])
        # Exactly ONE reply on #50 pairing both outcomes with clickable links.
        self.assertEqual(len(fake.comments[50]), 1)
        reply1 = fake.comments[50][0]["body"]
        self.assertIn("implementing your requests in two new tickets", reply1)
        self.assertIn("[#51](https://github.com/owner/product/issues/51)", reply1)
        self.assertIn("[#52](https://github.com/owner/product/issues/52)", reply1)

        # 2. Repeat report of the SAME open Save bug -> reuses #51 and appends new evidence, zero new bug ticket!
        c2 = tf.ThreadComment(
            "1002", "owner", "owner",
            "Same Save bug in Safari: clicking Save still loses the previously supported value.",
            "https://github.com/owner/product/issues/50#issuecomment-1002", "2026-11-27T11:10:00Z",
        )
        tf.consume_ticket(cfg, repo, "50", tk=tk, ticket=original, comments=[c1, c2], since="")
        with contextlib.ExitStack() as stack:
            fake.install(stack)
            r2 = tfu.sync(cfg)

        self.assertEqual(len(r2), 1)
        self.assertEqual([(i.kind, i.action, i.target_ref) for i in r2[0].items], [("bug", "append", "#51")])
        self.assertEqual(sorted(fake.issues.keys()), [50, 51, 52])
        self.assertEqual(len(fake.comments[51]), 1)
        self.assertIn("Safari", fake.comments[51][0]["body"])
        self.assertEqual(len(fake.comments[50]), 2)
        self.assertIn("[#51](https://github.com/owner/product/issues/51)", fake.comments[50][1]["body"])

        # 3. Mixed repeat bug + NEW distinct outcome (PDF export) -> reuses #51 AND opens #53, ONE combined reply!
        c3 = tf.ThreadComment(
            "1003", "owner", "owner",
            "Same Save bug in Firefox too: Save loses the value, plus please add a new PDF export.",
            "https://github.com/owner/product/issues/50#issuecomment-1003", "2026-11-27T11:20:00Z",
        )
        tf.consume_ticket(cfg, repo, "50", tk=tk, ticket=original, comments=[c1, c2, c3], since="")
        with contextlib.ExitStack() as stack:
            fake.install(stack)
            r3 = tfu.sync(cfg)

        self.assertEqual([(i.kind, i.action, i.target_ref) for i in r3[0].items], [
            ("bug", "append", "#51"),
            ("change_request", "create", "#53"),
        ])
        self.assertEqual(len(fake.comments[51]), 2)
        self.assertIn("Firefox", fake.comments[51][1]["body"])
        self.assertEqual(len(fake.comments[50]), 3)
        self.assertIn("[#51](https://github.com/owner/product/issues/51)", fake.comments[50][2]["body"])
        self.assertIn("[#53](https://github.com/owner/product/issues/53)", fake.comments[50][2]["body"])

        # 4. Different bug on the same completed ticket gets its OWN bug ticket (#54).
        c4 = tf.ThreadComment(
            "1004", "owner", "owner",
            "Pagination next button crashes with a 500 error on page 2.",
            "https://github.com/owner/product/issues/50#issuecomment-1004", "2026-11-27T11:30:00Z",
        )
        tf.consume_ticket(cfg, repo, "50", tk=tk, ticket=original, comments=[c1, c2, c3, c4], since="")
        with contextlib.ExitStack() as stack:
            fake.install(stack)
            r4 = tfu.sync(cfg)

        self.assertEqual([(i.kind, i.action, i.target_ref) for i in r4[0].items], [("bug", "create", "#54")])
        self.assertEqual(sorted(fake.issues.keys()), [50, 51, 52, 53, 54])

        # 5. Once #51 is CLOSED, a later report of Save losing values opens a NEW bug (#55) instead of appending.
        fake.issues[51]["state"] = "CLOSED"
        c5 = tf.ThreadComment(
            "1005", "owner", "owner",
            "Save loses the previously supported value again after release.",
            "https://github.com/owner/product/issues/50#issuecomment-1005", "2026-11-27T12:00:00Z",
        )
        tf.consume_ticket(cfg, repo, "50", tk=tk, ticket=original, comments=[c1, c2, c3, c4, c5], since="")
        with contextlib.ExitStack() as stack:
            fake.install(stack)
            r5 = tfu.sync(cfg)

        self.assertEqual([(i.kind, i.action, i.target_ref) for i in r5[0].items], [("bug", "create", "#55")])
        self.assertEqual(len(fake.comments[51]), 2)


class PartialFailureReconciliationTests(_FollowUpDbCase):
    def test_interrupt_after_first_ticket_after_linking_and_after_reply_converges_on_jira(self):
        repo = config.Repo(
            "teamapp", self.root, tracker="jira", jira_project="TEAM",
            jira_label="repo-teamapp", jira_site="https://jira.example.test", pipeline=True,
        )
        cfg = config.Config(repos=[repo])
        client = _FakeJira(project="TEAM", start_num=71)
        client.seed_issue("TEAM-70", "Persist settings on Save", "Save preserves selected values.", done=True)
        rid = support.start_pipeline(repo="teamapp", issue="TEAM-70", status="merged")
        spec_store.bind("jira:https://jira.example.test:20000", "teamapp", "TEAM-70", "", lambda: rid)
        spec_store.advance("jira:https://jira.example.test:20000", rid, "building", "complete", {"retro": "done"})

        tk = tracker.JiraTracker(repo, cfg.jira)
        original = tracker.Ticket("TEAM-70", "TEAM-70", "Persist settings on Save",
                                  body="Save preserves selected values.", open=False,
                                  url=client.browse_url("TEAM-70"))
        mixed_c = tf.ThreadComment(
            "401", "qa-maya", "Maya QA",
            "Save loses a previously supported value and also request a new CSV export.",
            client.comment_url("TEAM-70", "401"), "2026-11-27T13:00:00.000+0000",
        )
        # Inject faults at EVERY crash boundary from #993 AC 4:
        # 1) Create #1 succeeds & links; Create #2 fails AFTER Jira accepted TEAM-72 (lost ack).
        # 2) Link #2 fails AFTER Jira created the Relates link for TEAM-72 (lost ack).
        # 3) Requester reply fails AFTER Jira accepted the combined comment on TEAM-70 (lost ack).
        client.fail_create_after_accept.add(2)
        client.fail_link_after_accept.add(2)
        client.fail_comment_after_accept.add(1)

        with mock.patch.object(tracker.JiraTracker, "client", new_callable=mock.PropertyMock, return_value=client):
            tf.consume_ticket(cfg, repo, "TEAM-70", tk=tk, ticket=original, comments=[mixed_c], since="")
            row = tf.pending_follow_ups("teamapp")[0]

            # Pass 1: interrupts after first ticket + lost ack on second ticket creation.
            with self.assertRaises(jira.JiraUnsure):
                tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertEqual(sorted(client.issues.keys()), ["TEAM-70", "TEAM-71", "TEAM-72"])
            self.assertEqual(len(client.links), 1)
            self.assertEqual(len(client.comments_by_key["TEAM-70"]), 0)

            # Pass 2: reconciles TEAM-72 via marker (no TEAM-73!), interrupts right after linking TEAM-72.
            with self.assertRaises(jira.JiraUnsure):
                tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertEqual(sorted(client.issues.keys()), ["TEAM-70", "TEAM-71", "TEAM-72"])
            self.assertEqual(client.create_calls, 2)
            self.assertEqual(len(client.links), 2)
            self.assertEqual(len(client.comments_by_key["TEAM-70"]), 0)

            # Pass 3: sees link already exists (no 3rd link!), posts combined reply, interrupts after tracker accepts it.
            with self.assertRaises(jira.JiraUnsure):
                tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertEqual(client.link_calls, 2)
            self.assertEqual(len(client.comments_by_key["TEAM-70"]), 1)

            # Pass 4: reconciles accepted reply via marker and completes cleanly!
            final = tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertEqual(final.status, "done")
            self.assertFalse(final.replayed)
            # Pass 5: replay after completion is a zero-write no-op.
            replay = tfu.reconcile_follow_up(cfg, repo, row, tk=tk, original_ticket=original)
            self.assertTrue(replay.replayed)

        # Verify every single invariant of #993 AC 4!
        self.assertEqual(sorted(client.issues.keys()), ["TEAM-70", "TEAM-71", "TEAM-72"])
        self.assertEqual(client.create_calls, 2)
        self.assertEqual(client.link_calls, 2)
        self.assertEqual(client.comment_calls, 1)
        self.assertEqual(client.move_calls, 0)
        self.assertEqual(
            [(lk["type"]["name"], lk["inward"], lk["outward"]) for lk in client.links],
            [("Relates", "TEAM-71", "TEAM-70"), ("Relates", "TEAM-72", "TEAM-70")],
        )
        self.assertEqual(len(client.comments_by_key["TEAM-70"]), 1)
        reply = client.comments_by_key["TEAM-70"][0]["body"]
        self.assertIn("implementing your requests in two new tickets", reply)
        self.assertIn("[TEAM-71](https://jira.example.test/browse/TEAM-71)", reply)
        self.assertIn("[TEAM-72](https://jira.example.test/browse/TEAM-72)", reply)
        self.assertEqual(spec_store.find("teamapp", "TEAM-70")["state"], "complete")
        self.assertEqual(spec_store.find("teamapp", "TEAM-70")["data"], {"retro": "done"})
        self.assertEqual(tf.pending_follow_ups("teamapp"), [])

    def test_interrupt_on_github_mixed_and_duplicate_append_lost_ack_produces_zero_duplicates(self):
        repo = config.Repo("product", self.root, github="owner/product", pipeline=True, tracker="github")
        cfg = config.Config(repos=[repo])
        tk = tracker.GitHubTracker(repo)
        original = tracker.Ticket(
            "80", "#80", "Persist settings on Save",
            body="Save preserves timezone across reload.",
            open=False, url=gh.issue_url(repo.github, "80"), labels=["agent"],
        )
        fake = _FakeGitHub("owner/product", start_number=81)
        fake.seed_issue(80, original.title, original.body, state="CLOSED", labels=["agent"])
        # Interrupt before 2nd ticket accepted, then after combined reply accepted.
        fake.fail_create_before_accept.add(2)
        fake.fail_comment_after_accept.add(1)

        c1 = tf.ThreadComment(
            "1101", "owner", "owner",
            "Save loses the previously selected timezone and also please add a new CSV export.",
            "https://github.com/owner/product/issues/80#issuecomment-1101", "2026-11-27T14:00:00Z",
        )
        tf.consume_ticket(cfg, repo, "80", tk=tk, ticket=original, comments=[c1], since="")
        row1 = tf.pending_follow_ups("product")[0]

        with contextlib.ExitStack() as stack:
            fake.install(stack)
            # First run: #81 created, #82 fails before GitHub accepts it.
            with self.assertRaises(gh.GhError):
                tfu.reconcile_follow_up(cfg, repo, row1, tk=tk, original_ticket=original)
            self.assertEqual(sorted(fake.issues.keys()), [80, 81])
            self.assertEqual(len(fake.comments[80]), 0)

            # Second run: skips #81, creates #82, posts combined reply, then loses ack.
            with self.assertRaises(gh.GhUnsure):
                tfu.reconcile_follow_up(cfg, repo, row1, tk=tk, original_ticket=original)
            self.assertEqual(sorted(fake.issues.keys()), [80, 81, 82])
            self.assertEqual(len(fake.comments[80]), 1)

            # Third run via sync(): reconciles accepted reply via marker cleanly.
            res1 = tfu.sync(cfg)
            self.assertEqual(len(res1), 1)
            self.assertEqual(res1[0].status, "done")
            self.assertEqual(len(fake.comments[80]), 1)

            # Duplicate bug report with lost ack on evidence append comment on #81.
            fake.fail_comment_after_accept.add(fake.comment_calls + 1)
            c2 = tf.ThreadComment(
                "1102", "owner", "owner",
                "Same Save bug in Safari: Save still loses the timezone.",
                "https://github.com/owner/product/issues/80#issuecomment-1102", "2026-11-27T14:05:00Z",
            )
            tf.consume_ticket(cfg, repo, "80", tk=tk, ticket=original, comments=[c1, c2], since="")
            row2 = tf.pending_follow_ups("product")[0]

            with self.assertRaises(gh.GhUnsure):
                tfu.reconcile_follow_up(cfg, repo, row2, tk=tk, original_ticket=original)
            self.assertEqual(len(fake.comments[81]), 1)
            self.assertEqual(len(fake.comments[80]), 1)

            res2 = tfu.reconcile_follow_up(cfg, repo, row2, tk=tk, original_ticket=original)
            self.assertEqual(res2.status, "done")
            self.assertEqual(len(fake.comments[81]), 1)
            self.assertEqual(len(fake.comments[80]), 2)
            self.assertEqual(sorted(fake.issues.keys()), [80, 81, 82])

