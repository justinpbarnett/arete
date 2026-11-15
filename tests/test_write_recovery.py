import io
import json
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
import urllib.error
from unittest import mock
import arete

from arete import config, gh, outbound, paths, state, util

from tests import support
from tests.harness import make_folders, state_env


def setUpModule():
    support.isolate_live_state()


class GithubWriteLedger:
    RESET = 'Post "https://api.github.com/graphql": read tcp ...: read: connection reset by peer'

    def __init__(self, error=RESET, apply=True, lookup_fails=False, fail_twice=False):
        self.error, self.apply = error, apply
        self.lookup_fails, self.fail_twice = lookup_fails, fail_twice
        self.issues, self.comments, self.prs, self.calls, self.payloads = [], [], [], [], []
        self.writes = 0
        self.close_comment_only = False

    def sh(self, command, **kw):
        args = command[1:]
        self.calls.append(args)
        result = lambda data: subprocess.CompletedProcess(command, 0, stdout=json.dumps(data), stderr="")
        failure = lambda: subprocess.CompletedProcess(command, util.TIMED_OUT if self.error == "timeout" else 1,
                                                     stdout="", stderr=self.error)
        write = args[:2] in (["issue", "create"], ["issue", "comment"], ["pr", "comment"],
                                  ["pr", "create"], ["issue", "close"]) or "POST" in args
        if write:
            self.writes += 1
            failing = self.writes == 1 or self.fail_twice
            payload = kw.get("input", "")
            if args[0] == "api":
                payload = json.loads(payload)["body"]
            if "--comment" in args:
                payload = args[args.index("--comment") + 1]
            self.payloads.append(payload)
            if failing and not self.apply:
                return failure()
            url = "https://github.com/fixture/repo/issues/1"
            if args[:2] == ["issue", "create"]:
                self.issues.append({"number": 1, "body": payload, "state": "OPEN", "title": "fixture"})
            elif args[:2] == ["pr", "create"]:
                url = "https://github.com/fixture/repo/pull/1"
                self.prs.append({"url": url})
            elif args[:2] == ["issue", "close"]:
                if self.issues[0]["state"] == "OPEN":
                    if payload:
                        self.comments.append({"body": payload})
                    if not (failing and self.close_comment_only):
                        self.issues[0]["state"] = "CLOSED"
            else:
                self.comments.append({"body": payload})
            return failure() if failing else subprocess.CompletedProcess(command, 0, stdout=url, stderr="")
        if args[:2] == ["label", "create"]:
            return result(None)
        if self.lookup_fails:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="lookup unavailable")
        if args[:2] == ["issue", "list"]:
            return result(self.issues)
        if args[:2] == ["pr", "list"]:
            return result(self.prs)
        if args[:2] == ["issue", "view"]:
            return result({"state": self.issues[0]["state"]})
        if args[:2] == ["api", "user"]:
            return result({"login": "fixture"})
        if args[0] == "api" and "/comments?" in args[1]:
            return result([self.comments])
        if args[0] == "api" and "issues?labels=" in args[1]:
            return result([[i for i in self.issues if i["state"] == "OPEN"]])
        raise AssertionError(f"unknown GitHub request: {command}")


class GithubWriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.wait = mock.patch.object(util, "wait_for_network", return_value=True)
        self.wait.start()
        self.addCleanup(self.wait.stop)

    def invoke(self, name):
        if name == "issue_create":
            return gh.issue_create("fixture/repo", "fixture", "body")
        if name == "issue_comment":
            return gh.issue_comment("fixture/repo", 1, "body")
        if name == "pr_comment":
            return gh.pr_comment("https://github.com/fixture/repo/pull/1", "body")
        return gh.pr_reply("https://github.com/fixture/repo/pull/1", 2, "body")

    def test_lost_replies_reconcile_all_keyed_helpers(self):
        for name in ("issue_create", "issue_comment", "pr_comment", "pr_reply"):
            for error in (GithubWriteLedger.RESET, "HTTP 502: Bad Gateway (https://api.github.com/graphql)",
                          "timeout", "Something went wrong while executing your query"):
                with self.subTest(name=name, error=error):
                    ledger = GithubWriteLedger(error)
                    with mock.patch.object(util, "sh", side_effect=ledger.sh):
                        returned = self.invoke(name)
                    self.assertEqual(returned, 1 if name == "issue_create" else None)
                    self.assertEqual(ledger.writes, 1)
                    stored = ledger.issues if name == "issue_create" else ledger.comments
                    self.assertEqual(len(stored), 1)
                    self.assertRegex(stored[0]["body"], r"<!-- arete:key=[0-9a-f]{32} -->$")
                    self.assertEqual(gh.has_arete_mark(stored[0]["body"]), name.startswith("pr_"))

    def test_never_sent_retries_and_reads_keep_their_retry(self):
        ledger = GithubWriteLedger("error connecting to api.github.com", apply=False)
        with mock.patch.object(util, "sh", side_effect=ledger.sh):
            self.assertEqual(self.invoke("issue_create"), 1)
        self.assertEqual((ledger.writes, len(ledger.issues)), (2, 1))
        self.assertEqual(ledger.payloads[0], ledger.payloads[1])
        replies = [subprocess.CompletedProcess([], 1, stdout="", stderr=GithubWriteLedger.RESET),
                   subprocess.CompletedProcess([], 0, stdout="[]", stderr="")]
        with mock.patch.object(util, "sh", side_effect=replies) as shell:
            self.assertEqual(gh.gh_json(["issue", "list"]), [])
        self.assertEqual(shell.call_count, 2)

    def test_failed_lookup_never_resends_and_empty_lookup_resends_only_once(self):
        for name in ("issue_create", "issue_comment", "pr_comment", "pr_reply"):
            with self.subTest(name=name):
                ledger = GithubWriteLedger(lookup_fails=True)
                with mock.patch.object(util, "sh", side_effect=ledger.sh), self.assertRaises(gh.GhUnsure) as raised:
                    self.invoke(name)
                self.assertIsInstance(raised.exception, gh.GhError)
                self.assertEqual(ledger.writes, 1)
                self.assertEqual(len(ledger.issues) + len(ledger.comments), 1)
                for fail_twice in (False, True):
                    ledger = GithubWriteLedger(apply=False, fail_twice=fail_twice)
                    with mock.patch.object(util, "sh", side_effect=ledger.sh):
                        if fail_twice:
                            with self.assertRaises(gh.GhUnsure):
                                self.invoke(name)
                        else:
                            self.invoke(name)
                    self.assertEqual(ledger.writes, 2)
                    self.assertEqual(ledger.payloads[0], ledger.payloads[1])
                    self.assertTrue(ledger.calls[1][0] in ("api", "issue"))

    def test_rejected_write_and_failed_wait(self):
        ledger = GithubWriteLedger("HTTP 403: forbidden", apply=False)
        with mock.patch.object(util, "sh", side_effect=ledger.sh), self.assertRaises(gh.GhError) as raised:
            self.invoke("issue_create")
        self.assertNotIsInstance(raised.exception, gh.GhUnsure)
        self.assertEqual(len(ledger.calls), 1)
        ledger = GithubWriteLedger()
        with mock.patch.object(util, "sh", side_effect=ledger.sh), \
                mock.patch.object(util, "wait_for_network", return_value=False), self.assertRaises(gh.GhUnsure):
            self.invoke("issue_create")
        self.assertEqual(len(ledger.calls), 1)

    def test_pr_recovery_and_already_exists(self):
        for error in (GithubWriteLedger.RESET, "HTTP 502", "already exists"):
            ledger = GithubWriteLedger(error)
            with mock.patch.object(util, "sh", side_effect=ledger.sh):
                self.assertEqual(gh.pr_create("fixture/repo", "head", "main", "title", "body"),
                                 "https://github.com/fixture/repo/pull/1")
            self.assertEqual(len(ledger.prs), 1)
        for fails in (True, False):
            ledger = GithubWriteLedger(apply=False, lookup_fails=fails)
            with mock.patch.object(util, "sh", side_effect=ledger.sh), self.assertRaises(gh.GhUnsure):
                gh.pr_create("fixture/repo", "head", "main", "title", "body")
            self.assertEqual(ledger.writes, 1)

    def test_close_recovers_comment_only_and_full_close(self):
        for comment_only in (True, False):
            ledger = GithubWriteLedger()
            ledger.issues = [{"number": 1, "state": "OPEN"}]
            ledger.close_comment_only = comment_only
            with mock.patch.object(util, "sh", side_effect=ledger.sh):
                gh.issue_close("fixture/repo", 1, "recovered")
                gh.issue_close("fixture/repo", 1, "recovered again")
            self.assertEqual(ledger.issues[0]["state"], "CLOSED")
            self.assertEqual(len(ledger.comments), 1)
            if comment_only:
                self.assertNotIn("--comment", ledger.calls[3])

    def test_outgoing_key_is_after_link_screening_and_mark(self):
        with mock.patch.object(outbound, "screen", side_effect=lambda text: text.replace("private", "safe")):
            body = gh.outgoing("private\n  code", "Closes #5", mark=True, key="a" * 32)
        self.assertTrue(body.startswith("Closes #5\n\nsafe\n  code"))
        self.assertTrue(body.endswith(gh.ARETE_MARK + "\n\n" + gh.key_mark("a" * 32)))

    def test_invalid_lookup_reply_cannot_authorize_another_write(self):
        for name in ("issue_create", "issue_comment", "pr_comment", "pr_reply"):
            ledger = GithubWriteLedger()
            def transport(command, **kw):
                if ledger.writes:
                    return subprocess.CompletedProcess(command, 0, stdout="invalid json", stderr="")
                return ledger.sh(command, **kw)
            with self.subTest(name=name), mock.patch.object(util, "sh", side_effect=transport), \
                    self.assertRaises(gh.GhUnsure):
                self.invoke(name)
            self.assertEqual(ledger.writes, 1)

    def test_comment_recovery_finds_key_on_later_page(self):
        ledger = GithubWriteLedger()
        def transport(command, **kw):
            if command[1] == "api":
                self.assertIn("--paginate", command)
                self.assertIn("--slurp", command)
                self.assertIn("since=", command[2])
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps([
                    [{"body": "earlier comment"}] * 100, ledger.comments]), stderr="")
            return ledger.sh(command, **kw)
        with mock.patch.object(util, "sh", side_effect=transport):
            gh.issue_comment("fixture/repo", 1, "body")
        self.assertEqual(ledger.writes, 1)

    def test_comment_free_close_recovers_and_resends_only_if_open(self):
        for apply in (True, False):
            ledger = GithubWriteLedger(apply=apply)
            ledger.issues = [{"number": 1, "state": "OPEN"}]
            with mock.patch.object(util, "sh", side_effect=ledger.sh):
                gh.issue_close("fixture/repo", 1)
            self.assertEqual(ledger.writes, 1 if apply else 2)
            self.assertEqual(ledger.issues[0]["state"], "CLOSED")
            self.assertEqual(ledger.comments, [])


class JiraWriteLedger:
    def __init__(self, error="502", apply=True, lookup_fails=False, fail_twice=False):
        self.error, self.apply, self.lookup_fails, self.fail_twice = error, apply, lookup_fails, fail_twice
        self.comments, self.payloads, self.calls = [], [], []
        self.posts, self.transitions = 0, 0
        self.status = "Open"
        self.visible = True
        self.author, self.created = "fixture", util.iso()
        self.property_missing = False

    def open(self, request, **kw):
        from http.client import RemoteDisconnected
        import socket
        from urllib.parse import urlsplit
        method, path = request.get_method(), urlsplit(request.full_url).path
        self.calls.append((method, request.full_url))
        def http(code):
            return urllib.error.HTTPError(request.full_url, code, "fixture error", {"Retry-After": "0"}, io.BytesIO(b"{}"))
        if method == "POST":
            self.posts += 1
            payload = json.loads(request.data)
            self.payloads.append(payload)
            failing = self.posts == 1 or self.fail_twice
            if not failing or self.apply:
                if path.endswith("/transitions"):
                    self.transitions += 1
                    self.status = "Done"
                else:
                    self.comments.append({"id": str(len(self.comments) + 1), "author": {"accountId": self.author},
                                          "created": self.created, **payload})
            if failing:
                errors = {"502": http(502), "429": http(429), "400": http(400), "timeout": TimeoutError(),
                          "reset": ConnectionResetError(), "disconnected": RemoteDisconnected(),
                          "dns": urllib.error.URLError(socket.gaierror()),
                          "refused": urllib.error.URLError(ConnectionRefusedError())}
                raise errors[self.error]
            reply = None
        elif self.lookup_fails:
            raise http(400)
        elif path.endswith("/myself"):
            reply = {"accountId": "fixture"}
        elif path.endswith("/properties/arete"):
            if self.property_missing:
                raise http(404)
            reply = {"value": self.comments[int(path.split("/")[-3]) - 1]["properties"][0]["value"]}
        elif path.endswith("/comment"):
            reply = {"comments": self.comments if self.visible else []}
        elif path.endswith("/transitions"):
            reply = {"transitions": [{"id": "1", "name": "Finish", "to": {"name": "Done"}}]}
        elif path.endswith("/FIX-1"):
            reply = {"fields": {"status": {"name": self.status}}}
        else:
            raise AssertionError(f"unknown Jira request {method} {path}")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(reply).encode() if reply else b""
        return response


class JiraWriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        from arete import jira
        self.jira = jira
        self.client = jira.Client("https://jira.fixture.invalid", "fixture", "fixture")
        for patch in (mock.patch.object(util, "wait_for_network", return_value=True),
                      mock.patch.object(util, "online", return_value=True), mock.patch.object(jira.time, "sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_comments_reconcile_each_lost_reply(self):
        for error in ("502", "timeout", "reset", "disconnected"):
            with self.subTest(error=error):
                ledger = JiraWriteLedger(error)
                with mock.patch.object(self.jira, "_opener", ledger):
                    self.client.comment("FIX-1", "body")
                self.assertEqual((ledger.posts, len(ledger.comments)), (1, 1))
                self.assertRegex(ledger.comments[0]["properties"][0]["value"]["key"], r"^[0-9a-f]{32}$")
                self.assertIn("orderBy=-created&maxResults=10", ledger.calls[-2][1])
                self.assertTrue(ledger.calls[-1][1].endswith("/properties/arete"))

    def test_only_429_and_proven_unsent_posts_retry(self):
        for error in ("429", "dns", "refused"):
            with self.subTest(error=error):
                ledger = JiraWriteLedger(error, apply=False)
                with mock.patch.object(self.jira, "_opener", ledger):
                    self.client.comment("FIX-1", "body")
                self.assertEqual((ledger.posts, len(ledger.comments)), (2, 1))
                self.assertEqual(ledger.payloads[0], ledger.payloads[1])
                self.assertTrue(all(method == "POST" for method, _ in ledger.calls))

    def test_failed_lookup_preserves_one_comment_and_owner_guidance(self):
        ledger = JiraWriteLedger(lookup_fails=True)
        with mock.patch.object(self.jira, "_opener", ledger), self.assertRaises(self.jira.JiraUnsure) as raised:
            self.client.comment("FIX-1", "body")
        self.assertIsInstance(raised.exception, self.jira.JiraError)
        self.assertIn("reply was lost; check the ticket before sending again", str(raised.exception))
        self.assertEqual((ledger.posts, len(ledger.comments)), (1, 1))

    def test_no_match_allows_one_resend_with_the_same_property(self):
        for reason in ("invisible", "other-author", "old", "property-404"):
            for fail_twice in (False, True):
                with self.subTest(reason=reason, fail_twice=fail_twice):
                    ledger = JiraWriteLedger(fail_twice=fail_twice)
                    if reason == "invisible":
                        ledger.visible = False
                    if reason == "other-author":
                        ledger.author = "someone-else"
                    if reason == "old":
                        ledger.created = util.iso(util.now() - timedelta(minutes=5))
                    if reason == "property-404":
                        ledger.property_missing = True
                    with mock.patch.object(self.jira, "_opener", ledger):
                        if fail_twice:
                            with self.assertRaises(self.jira.JiraUnsure):
                                self.client.comment("FIX-1", "body")
                        else:
                            self.client.comment("FIX-1", "body")
                    self.assertEqual(ledger.posts, 2)
                    self.assertEqual(ledger.payloads[0], ledger.payloads[1])

    def test_transition_checks_destination_without_another_post(self):
        ledger = JiraWriteLedger()
        with mock.patch.object(self.jira, "_opener", ledger):
            self.assertTrue(self.client.transition("FIX-1", "Finish"))
        self.assertEqual((ledger.posts, ledger.transitions), (1, 1))
        ledger = JiraWriteLedger(apply=False)
        with mock.patch.object(self.jira, "_opener", ledger), self.assertRaises(self.jira.JiraUnsure):
            self.client.transition("FIX-1", "Done")
        self.assertEqual(ledger.posts, 1)

    def test_transition_lost_status_lookup_preserves_uncertainty(self):
        ledger = JiraWriteLedger()
        original = ledger.open
        def transport(request, **kw):
            if ledger.posts:
                ledger.lookup_fails = True
            return original(request, **kw)
        with mock.patch.object(self.jira, "_opener", SimpleNamespace(open=transport)), \
                self.assertRaises(self.jira.JiraUnsure):
            self.client.transition("FIX-1", "Done")
        self.assertEqual((ledger.posts, ledger.transitions), (1, 1))

    def test_missing_identity_and_failed_wait_do_not_resend(self):
        for account, online in (({}, True), ({"accountId": "fixture"}, False)):
            ledger = JiraWriteLedger()
            self.client._me = account
            with mock.patch.object(self.jira, "_opener", ledger), \
                    mock.patch.object(util, "wait_for_network", return_value=online), \
                    self.assertRaises(self.jira.JiraUnsure):
                self.client.comment("FIX-1", "body")
            self.assertEqual((ledger.posts, len(ledger.comments)), (1, 1))

    def test_rejected_post_is_ordinary_error_and_get_put_keep_retries(self):
        ledger = JiraWriteLedger("400", apply=False)
        with mock.patch.object(self.jira, "_opener", ledger), self.assertRaises(self.jira.JiraError) as raised:
            self.client.comment("FIX-1", "body")
        self.assertNotIsInstance(raised.exception, self.jira.JiraUnsure)
        self.assertEqual(ledger.posts, 1)
        for method in ("GET", "PUT"):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{}'
            for error in (TimeoutError(), urllib.error.HTTPError("fixture", 502, "", {}, io.BytesIO(b"{}"))):
                opener = mock.Mock()
                opener.open.side_effect = [error, response]
                with mock.patch.object(self.jira, "_opener", opener):
                    self.assertEqual(self.client.request(method, "/fixture"), {})
                self.assertEqual(opener.open.call_count, 2)


class AlertWriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        make_folders(root, "home", "state", "config", "logs", "worktrees")
        for patch in (mock.patch.dict(os.environ, state_env(root / "state", HOME=str(root / "home"))),
                      mock.patch.object(paths, "STATE_DIR", root / "state"),
                      mock.patch.object(paths, "RUNS_DIR", root / "state" / "runs"),
                      mock.patch.object(paths, "LOCKS_DIR", root / "state" / "locks"),
                      mock.patch.object(paths, "WORKTREES_DIR", root / "worktrees"),
                      mock.patch.object(paths, "LOG_DIR", root / "logs"),
                      mock.patch.object(paths, "DB_FILE", root / "state" / "arete.db"),
                      mock.patch.object(util, "wait_for_network", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)
        state.ensure_schema()

    def test_heartbeats_adopt_lost_create_and_clear_once(self):
        from arete import alerts
        ledger = GithubWriteLedger()
        original = ledger.sh
        def transport(command, **kw):
            if command[1:3] == ["issue", "list"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="lost lookup")
            return original(command, **kw)
        with mock.patch.object(util, "sh", side_effect=transport), mock.patch.object(alerts, "notify"):
            self.assertIn("gh failed", alerts.raise_alert("fixture/repo", "fixture", "Fixture", "broken"))
            for _ in range(2):
                self.assertIn("still open", alerts.raise_alert("fixture/repo", "fixture", "Fixture", "broken"))
            self.assertEqual(state.alert_get("fixture")["issue"], 1)
            alerts.clear_alert("fixture/repo", "fixture")
        self.assertEqual((len(ledger.issues), ledger.issues[0]["state"]), (1, "CLOSED"))
        self.assertIsNone(state.alert_get("fixture"))

    def test_clear_retries_after_lost_state_lookup_without_duplicate_comment(self):
        from arete import alerts
        ledger = GithubWriteLedger(lookup_fails=True)
        ledger.issues = [{"number": 1, "state": "OPEN"}]
        state.alert_set("fixture", 1, "fixture/repo")
        with mock.patch.object(util, "sh", side_effect=ledger.sh):
            self.assertIn("retrying next pass", alerts.clear_alert("fixture/repo", "fixture"))
            ledger.lookup_fails = False
            alerts.clear_alert("fixture/repo", "fixture")
        self.assertEqual(len(ledger.comments), 1)
        self.assertEqual(ledger.issues[0]["state"], "CLOSED")
        self.assertIsNone(state.alert_get("fixture"))

    def test_failed_adoption_cannot_create(self):
        from arete import alerts
        ledger = GithubWriteLedger(lookup_fails=True)
        with mock.patch.object(util, "sh", side_effect=ledger.sh), mock.patch.object(alerts, "notify"):
            self.assertIn("gh failed", alerts.raise_alert("fixture/repo", "fixture", "Fixture", "broken"))
        self.assertEqual(ledger.writes, 0)

    def test_clear_finishes_after_comment_lands_before_close(self):
        from arete import alerts
        ledger = GithubWriteLedger()
        ledger.issues = [{"number": 1, "state": "OPEN"}]
        ledger.close_comment_only = True
        state.alert_set("fixture", 1, "fixture/repo")
        with mock.patch.object(util, "sh", side_effect=ledger.sh):
            alerts.clear_alert("fixture/repo", "fixture")
            self.assertIsNone(alerts.clear_alert("fixture/repo", "fixture"))
        self.assertEqual((len(ledger.comments), ledger.issues[0]["state"]), (1, "CLOSED"))

    def test_capture_failed_lookup_keeps_press_submit_note_and_resume_adopts(self):
        from arete import capture
        state.capture_save(kind="idea", text="fixture")
        draft = state.capture_draft()
        draft_id, revision = draft["id"], draft["revision"]
        self.assertTrue(state.capture_move(draft_id, revision, "draft", "filing"))
        ledger = GithubWriteLedger(lookup_fails=True)
        with mock.patch.object(util, "sh", side_effect=ledger.sh), \
                mock.patch.object(capture.chat, "arete_repo", return_value=SimpleNamespace(github="fixture/repo")), \
                mock.patch.object(capture, "CAPTURES_DIR", Path(self.folder.name) / "captures"), \
                mock.patch.object(capture, "upload", return_value=""), \
                mock.patch.object(capture, "filing_text", return_value=("fixture", "body")):
            capture.deliver(draft_id, revision, config.Config(), False)
            self.assertIn("Press Submit", state.capture_draft()["delivery"])
            self.assertEqual(len(ledger.issues), 1)
            ledger.lookup_fails = False
            capture.deliver(draft_id, revision, config.Config(), True)
            self.assertEqual(state.capture_filed_row(draft_id)["issue"], 1)
        self.assertEqual(len(ledger.issues), 1)


if __name__ == "__main__":
    unittest.main()

