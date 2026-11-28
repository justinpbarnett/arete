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
        self.issues, self.comments, self.replies, self.prs, self.calls, self.payloads = [], [], [], [], [], []
        self.labels = set()
        self.writes = 0

    def sh(self, command, **kw):
        args = command[1:]
        self.calls.append(args)

        def result(data):
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(data), stderr="")

        def failure():
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=self.error)

        if args[:2] == ["label", "create"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        is_api_post = args[0] == "api" and ("POST" in args)
        write = args[:2] in (["pr", "create"], ["issue", "close"], ["issue", "reopen"], ["issue", "edit"]) or is_api_post
        if write:
            self.writes += 1
            failing = self.writes == 1 or self.fail_twice
            payload = kw.get("input", "")
            self.payloads.append(payload)
            if failing and not self.apply:
                return failure()
            if args[:2] == ["pr", "create"]:
                url = "https://github.com/fixture/repo/pull/1"
                self.prs.append({"url": url})
                return failure() if failing else subprocess.CompletedProcess(command, 0, stdout=url, stderr="")
            if args[:2] == ["issue", "close"]:
                if self.issues:
                    self.issues[0]["state"] = "CLOSED"
                return failure() if failing else subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if args[:2] == ["issue", "reopen"]:
                if self.issues:
                    self.issues[0]["state"] = "OPEN"
                return failure() if failing else subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if args[:2] == ["issue", "edit"]:
                for idx, a in enumerate(args):
                    if a == "--add-label" and idx + 1 < len(args):
                        self.labels.add(args[idx + 1])
                    elif a == "--remove-label" and idx + 1 < len(args):
                        self.labels.discard(args[idx + 1])
                if "--body-file" in args and self.issues:
                    self.issues[0]["body"] = str(payload)
                return failure() if failing else subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            body_obj = json.loads(payload)
            endpoint = next(a for i, a in enumerate(args[1:], 1) if not a.startswith("-") and args[i - 1] not in ("--method", "-X", "--input"))
            if endpoint.endswith("/issues"):
                num = len(self.issues) + 1
                row = {"id": num, "number": num, "title": body_obj.get("title", ""), "body": body_obj.get("body", ""),
                       "state": "OPEN", "html_url": f"https://github.com/fixture/repo/issues/{num}"}
                self.issues.append(row)
                return failure() if failing else result(row)
            if endpoint.endswith("/replies"):
                rid = len(self.replies) + 1
                row = {"id": rid, "body": body_obj.get("body", ""), "html_url": f"https://github.com/fixture/repo/pull/1#discussion_r{rid}"}
                self.replies.append(row)
                return failure() if failing else result(row)
            cid = len(self.comments) + 1
            row = {"id": cid, "body": body_obj.get("body", ""), "html_url": f"https://github.com/fixture/repo/issues/1#issuecomment-{cid}"}
            self.comments.append(row)
            return failure() if failing else result(row)
        if self.lookup_fails:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="lookup unavailable")
        if args[:2] == ["pr", "list"]:
            return result(self.prs)
        if args[:2] == ["issue", "view"]:
            issue = self.issues[0] if self.issues else {"state": "OPEN", "body": ""}
            return result(issue)
        if args[0] == "api":
            endpoint = args[1]
            if "/labels" in endpoint:
                return result([[{"name": lb} for lb in sorted(self.labels)]])
            if "/pulls/" in endpoint and "/comments" in endpoint:
                return result([self.replies])
            if "/issues/" in endpoint and "/comments" in endpoint:
                return result([self.comments])
            if "/issues?" in endpoint:
                return result([self.issues])
        raise AssertionError(f"unknown GitHub request: {command}")


class GithubWriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from arete import outbox, send_github
        from arete.store import db
        from tests import support

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(support.workspace_state(root))
        db.migrate()
        send_github.clear_label_cache()
        self.enterContext(mock.patch.object(util, "wait_for_network", return_value=True))
        self.enterContext(mock.patch.object(outbox, "RETRY_SECONDS", 0))
        self.outbox = outbox

    def _draft_for(self, kind: str, slot: str):
        if kind == "issue":
            return self.outbox.draft(
                target="github", kind="issue", object="github:fixture/repo",
                body=json.dumps({"title": "fixture", "body": "body", "labels": []}),
                drafted_by="arete", slot=slot,
            )
        if kind == "comment":
            return self.outbox.draft(
                target="github", kind="comment", object="github:fixture/repo#1",
                body="body", drafted_by="arete", slot=slot,
            )
        if kind == "reply":
            return self.outbox.draft(
                target="github", kind="reply", object="github:fixture/repo#1/r2",
                body="body", drafted_by="arete", slot=slot,
            )
        if kind == "label":
            return self.outbox.draft(
                target="github", kind="label", object="github:fixture/repo#1",
                body=json.dumps({"add": ["agent:pr"], "remove": ["agent:running"], "family": "state"}),
                drafted_by="arete", slot=slot,
            )
        return self.outbox.draft(
            target="github", kind="status", object="github:fixture/repo#1",
            body=json.dumps({"state": "closed"}), drafted_by="arete", slot=slot,
        )

    def test_lost_replies_reconcile_all_sender_kinds(self):
        for kind in ("issue", "comment", "reply", "label", "status"):
            for idx, error in enumerate((GithubWriteLedger.RESET, "HTTP 502")):
                with self.subTest(kind=kind, error=error):
                    ledger = GithubWriteLedger(error)
                    if kind in ("label", "status"):
                        ledger.issues = [{"number": 1, "state": "OPEN", "body": ""}]
                        ledger.labels = {"agent:running"}
                    item_id = self._draft_for(kind, f"reconcile-{kind}-{idx}")
                    with mock.patch.object(util, "sh", side_effect=ledger.sh):
                        self.outbox.send_due()
                        self.outbox.send_due()
                    detail = self.outbox.get(item_id)
                    self.assertEqual(detail["item"]["state"], "sent")
                    self.assertEqual([a["outcome"] for a in detail["attempts"]], ["unknown", "found"])
                    self.assertEqual(ledger.writes, 1)

    def test_failed_lookup_never_resends_and_empty_lookup_resends_once(self):
        for kind in ("issue", "comment", "reply"):
            with self.subTest(kind=kind):
                ledger = GithubWriteLedger(lookup_fails=True)
                item_id = self._draft_for(kind, f"lookup-fail-{kind}")
                with mock.patch.object(util, "sh", side_effect=ledger.sh):
                    self.outbox.send_due()
                    self.outbox.send_due()
                detail = self.outbox.get(item_id)
                self.assertEqual(detail["item"]["state"], "approved")
                self.assertEqual([a["outcome"] for a in detail["attempts"]], ["unknown", "unknown"])
                self.assertEqual(ledger.writes, 1)
                self.outbox.reject(item_id, reason="cleanup")

                ledger2 = GithubWriteLedger(apply=False, fail_twice=False)
                item_id2 = self._draft_for(kind, f"empty-lookup-{kind}")
                with mock.patch.object(util, "sh", side_effect=ledger2.sh):
                    self.outbox.send_due()
                    self.outbox.send_due()
                detail2 = self.outbox.get(item_id2)
                self.assertEqual(detail2["item"]["state"], "sent")
                self.assertEqual([a["outcome"] for a in detail2["attempts"]], ["unknown", "sent"])
                self.assertEqual(ledger2.writes, 2)
                self.assertEqual(ledger2.payloads[0], ledger2.payloads[1])

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

    def test_comment_before_close_never_passes_per_object_order(self):
        ledger = GithubWriteLedger(apply=False)
        ledger.issues = [{"number": 1, "state": "OPEN", "body": ""}]
        c_id = self.outbox.draft(
            target="github", kind="comment", object="github:fixture/repo#1",
            body="Recovered.", drafted_by="arete", slot="order-comment",
        )
        s_id = self.outbox.draft(
            target="github", kind="status", object="github:fixture/repo#1",
            body=json.dumps({"state": "closed"}), drafted_by="arete", slot="order-close",
        )
        with mock.patch.object(util, "sh", side_effect=ledger.sh):
            self.outbox.send_due()
            # Comment failed transiently, so close MUST NOT have run yet!
            self.assertEqual(ledger.issues[0]["state"], "OPEN")
            self.assertEqual(self.outbox.get(s_id)["item"]["state"], "approved")
            self.outbox.send_due()
        self.assertEqual(self.outbox.get(c_id)["item"]["state"], "sent")
        self.assertEqual(self.outbox.get(s_id)["item"]["state"], "sent")
        self.assertEqual(ledger.issues[0]["state"], "CLOSED")
        self.assertEqual(len(ledger.comments), 1)


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
        self.enterContext(mock.patch.object(util, "wait_for_network", return_value=True))
        clock = self.enterContext(mock.patch.object(jira, "time", mock.Mock(wraps=jira.time)))
        clock.sleep.side_effect = AssertionError("unexpected retry sleep")

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
