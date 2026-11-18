import io
import json
import subprocess
import unittest
import urllib.error
from unittest import mock

from arete import gh, jira, outbound, util


def setUpModule():
    # Outgoing screening stays real, without reading machine configuration or leaking caches.
    for patch in (mock.patch.object(outbound, "keep_configured_private"),
                  mock.patch.object(outbound, "own_hosts", []),
                  mock.patch.object(outbound, "extra_hosts", set())):
        patch.start()
        unittest.addModuleCleanup(patch.stop)


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

        def result(data):
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(data), stderr="")

        def failure():
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=self.error)

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
        if self.lookup_fails:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="lookup unavailable")
        if args[:2] == ["issue", "list"]:
            return result(self.issues)
        if args[:2] == ["pr", "list"]:
            return result(self.prs)
        if args[:2] == ["issue", "view"]:
            return result({"state": self.issues[0]["state"]})
        if args[0] == "api" and "/comments?" in args[1]:
            return result([self.comments])
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
            for error in (GithubWriteLedger.RESET, "HTTP 502"):
                with self.subTest(name=name, error=error):
                    ledger = GithubWriteLedger(error)
                    with mock.patch.object(util, "sh", side_effect=ledger.sh):
                        returned = self.invoke(name)
                    self.assertEqual(returned, 1 if name == "issue_create" else None)
                    self.assertEqual(ledger.writes, 1)
                    stored = ledger.issues if name == "issue_create" else ledger.comments
                    self.assertEqual(len(stored), 1)
                    self.assertRegex(stored[0]["body"], r"<!-- arete:key=[0-9a-f]{32} -->$")

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


class JiraWriteLedger:
    def __init__(self, error="502", apply=True, lookup_fails=False, fail_twice=False):
        self.error, self.apply, self.lookup_fails, self.fail_twice = error, apply, lookup_fails, fail_twice
        self.comments, self.payloads = [], []
        self.posts, self.transitions = 0, 0
        self.status = "Open"
        self.author, self.created = "fixture", util.iso()

    def open(self, request, **kw):
        from urllib.parse import urlsplit
        method, path = request.get_method(), urlsplit(request.full_url).path
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
                errors = {"502": http(502), "timeout": TimeoutError()}
                raise errors[self.error]
            reply = None
        elif self.lookup_fails:
            raise http(400)
        elif path == "/rest/api/3/project/FIX/statuses":
            reply = [{"statuses": [{"name": "Open"}, {"name": "Done"}]}]
        elif path.endswith("/myself"):
            reply = {"accountId": "fixture"}
        elif path.endswith("/properties/arete"):
            reply = {"value": self.comments[int(path.split("/")[-3]) - 1]["properties"][0]["value"]}
        elif path.endswith("/comment"):
            reply = {"comments": self.comments}
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
        self.client = jira.Client("https://jira.fixture.invalid", "fixture", "fixture")
        for patch in (mock.patch.object(util, "wait_for_network", return_value=True),
                      mock.patch.object(jira.time, "sleep", side_effect=AssertionError("unexpected retry sleep"))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_comments_reconcile_each_lost_reply(self):
        for error, lookup_fails in (("502", False), ("timeout", False), ("502", True)):
            with self.subTest(error=error, lookup_fails=lookup_fails):
                ledger = JiraWriteLedger(error, lookup_fails=lookup_fails)
                with mock.patch.object(jira, "_opener", ledger):
                    if lookup_fails:
                        with self.assertRaisesRegex(jira.JiraUnsure, "check the ticket before sending again"):
                            self.client.comment("FIX-1", "body")
                    else:
                        self.client.comment("FIX-1", "body")
                self.assertEqual((ledger.posts, len(ledger.comments)), (1, 1))
                self.assertRegex(ledger.comments[0]["properties"][0]["value"]["key"], r"^[0-9a-f]{32}$")

    def test_no_match_allows_one_resend_with_the_same_property(self):
        for fail_twice in (False, True):
            with self.subTest(fail_twice=fail_twice):
                ledger = JiraWriteLedger(apply=False, fail_twice=fail_twice)
                with mock.patch.object(jira, "_opener", ledger):
                    if fail_twice:
                        with self.assertRaises(jira.JiraUnsure):
                            self.client.comment("FIX-1", "body")
                    else:
                        self.client.comment("FIX-1", "body")
                self.assertEqual(ledger.posts, 2)
                self.assertEqual(len(ledger.comments), 0 if fail_twice else 1)
                self.assertEqual(ledger.payloads[0], ledger.payloads[1])
                self.assertRegex(ledger.payloads[0]["properties"][0]["value"]["key"], r"^[0-9a-f]{32}$")

    def test_transition_checks_destination_without_another_post(self):
        for apply in (True, False):
            with self.subTest(apply=apply):
                ledger = JiraWriteLedger(apply=apply)
                with mock.patch.object(jira, "_opener", ledger):
                    if apply:
                        self.assertTrue(self.client.move("FIX-1", "Finish"))
                    else:
                        with self.assertRaises(jira.JiraUnsure):
                            self.client.move("FIX-1", "Done")
                self.assertEqual((ledger.posts, ledger.transitions), (1, int(apply)))


if __name__ == "__main__":
    unittest.main()
