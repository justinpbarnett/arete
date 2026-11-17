import io
import json
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import config, gh, jev, outbound, pipeline, verify, workspace
from arete.review import Review

READ_ONLY_GRANT = {"repositories": ["widgets"], "permissions": {"contents": "read"}}


class CloneTokenTests(unittest.TestCase):
    VALUE = "fake-clone-value-0001"
    APP = config.GithubApp("12345", "67890", Path("/nowhere"))
    URL = "https://api.github.com/app/installations/67890/access_tokens"

    def good(self, **changes):
        return {"token": self.VALUE, "permissions": {"contents": "read", "metadata": "read"},
                "repository_selection": "selected", "repositories": [{"full_name": "Octo/Widgets"}], **changes}

    def mint(self, answer):
        seen = []

        class Reply:
            def __init__(self, text):
                self.text = text

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.text

        def opened(request, timeout=None):
            seen.append(request)
            if isinstance(answer, Exception):
                raise answer
            return Reply(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

        with mock.patch.object(gh.urllib.request, "urlopen", opened), \
                mock.patch.object(gh, "app_jwt", return_value="fixture-jwt"), \
                mock.patch.object(gh.util, "wait_for_network", return_value=False):
            try:
                return gh.clone_token(self.APP, "octo/widgets"), seen, None
            except gh.GhError as e:
                return None, seen, str(e)

    def test_the_mint_asks_for_read_only_on_the_one_repository(self):
        value, seen, error = self.mint(self.good())
        self.assertEqual((value, error), (self.VALUE, None))
        self.assertEqual(gh.CLONE_TOKEN_PERMISSIONS, {"contents": "read"})
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0].full_url, seen[0].get_method()), (self.URL, "POST"))
        self.assertEqual(json.loads(seen[0].data), READ_ONLY_GRANT)

    def test_the_mint_refuses_anything_but_read_on_this_one_repository(self):
        forbidden = urllib.error.HTTPError(self.URL, 403, "Forbidden", {},
                                           io.BytesIO(b'{"message": "Resource not accessible by integration"}'))
        refusals = {
            "write": (self.good(permissions={"contents": "write"}), ""),
            "extra permission": (self.good(permissions={"contents": "read", "pull_requests": "write"}), ""),
            "no permissions": ({"token": self.VALUE}, ""),
            "all repositories": (self.good(repository_selection="all", repositories=[]), ""),
            "another repository": (self.good(repositories=[{"full_name": "octo/other"}]), ""),
            "no token": (self.good(token=""), ""),
            "not json": (b"<html>", ""),
            "http 403": (forbidden, "HTTP 403"),
            "network down": (OSError("network is unreachable"), "network is unreachable"),
        }
        for name, (answer, said) in refusals.items():
            with self.subTest(name):
                value, seen, error = self.mint(answer)
                self.assertIsNone(value)
                self.assertIsNotNone(error)
                self.assertNotIn(self.VALUE, error)
                self.assertIn(said, error)
                self.assertTrue(seen)
                for request in seen:
                    self.assertEqual(json.loads(request.data), READ_ONLY_GRANT)


class PushedPrDraftTests(unittest.TestCase):
    URL = "https://github.com/fixture/repo/pull/7"

    def setUp(self):
        # Checkout contents and model replies are inputs; publication rules stay real.
        self.root = Path("/fixture")
        self.head = "a" * 40
        self.calls, self.logs = [], []
        self.draft, self.remote_head, self.score, self.fail = True, self.head, 0.1, False
        self.run = SimpleNamespace(id="x", repo=config.Repo(name="fixture", path=self.root), repairs="", feedback=[],
                                   facts={}, log=self.logs.append)
        self.layer = SimpleNamespace(trust_level="automerge")
        self.cfg = config.Config()
        self.c = workspace.Checkout("", "fixture/repo", self.root, self.root, "main", pr=self.URL, lease=self.head)
        for patch in (mock.patch.object(gh, "gh", self.fake_gh),
                      mock.patch.object(jev, "ready", return_value=True),
                      mock.patch.object(jev, "ask", self.fake_ask),
                      mock.patch.object(pipeline, "patch_from_base", return_value="+a change"),
                      mock.patch.object(pipeline.vault, "values", return_value=set()),
                      mock.patch.object(outbound, "keep_configured_private"),
                      mock.patch.object(outbound, "own_hosts", []),
                      mock.patch.object(outbound, "extra_hosts", set())):
            patch.start()
            self.addCleanup(patch.stop)

    def fake_gh(self, args, input=None, **kw):
        self.calls.append((list(args), input))
        if args[:2] == ["pr", "ready"]:
            self.draft = "--undo" in args
        elif args[:2] == ["pr", "view"]:
            if "isDraft,headRefOid" in args:
                if self.fail:
                    raise gh.GhError("gh pr view: boom")
                return json.dumps({"isDraft": self.draft, "headRefOid": self.remote_head})
            if "title" in args:
                return json.dumps({"title": "feat: Title"})
            return json.dumps({"mergeStateStatus": "DRAFT" if self.draft else "CLEAN", "mergeable": "MERGEABLE"})
        elif args[0] == "api":
            return json.dumps({"allow_auto_merge": False})
        elif args[:2] not in (["pr", "merge"], ["pr", "comment"]):
            raise AssertionError(f"unexpected GitHub request: {args}")
        return ""

    def fake_ask(self, *a, **k):
        answers = {n: jev.Answer(self.score, 1.0) for n in jev.RISK}
        answers["irreversible"] = jev.Answer(False, 1.0, {"yes": 0.0})
        return jev.Reply(answers=answers)

    def names(self, *prefix):
        return [a for a, _ in self.calls if a[:len(prefix)] == list(prefix)]

    def test_only_approved_review_can_make_current_work_ready_and_merge(self):
        cases = [
            ("missing", Review.stored({}), verify.Verified("pass"), False),
            ("changes requested", Review.stored({"verdict": "request_changes"}), verify.Verified("pass"), False),
            ("review failed", Review.failed_step([], [], {}), verify.Verified("pass"), False),
            ("approved", Review.stored({"verdict": "approve"}), verify.Verified("pass"), True),
            ("guard refused", Review.stored({"verdict": "approve"}),
             verify.Verified("blocked", blocked_by="the JEV guard refused it"), False),
        ]
        for name, review, verified, allowed in cases:
            with self.subTest(name=name):
                self.calls.clear()
                self.draft = True
                why = pipeline.draft_reason(review, verified)
                self.assertEqual(bool(why), not allowed)
                ready = pipeline.ready_pushed(self.run, [self.c], [], why)
                pipeline.automerge(self.cfg, self.run, self.layer, None, why, ready, "about")
                self.assertEqual(ready, [self.c] if allowed else [])
                self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL]] if allowed else [])
                self.assertEqual(len(self.names("pr", "merge")), int(allowed))
                self.assertEqual(self.draft, not allowed)

    def test_pushed_ready_pr_is_made_a_draft_when_held(self):
        self.draft = False
        why = "the check in use was blocked"
        self.assertEqual(pipeline.hold_pushed(self.run, [self.c], [], why), [self.c])
        self.assertEqual(self.names("pr", "ready"), [["pr", "ready", self.URL, "--undo"]])
        self.assertTrue(self.draft)
        self.calls.clear()
        self.assertEqual(pipeline.ready_pushed(self.run, [self.c], [], why), [])
        # Even callers passing a held checkout directly cannot enable merging.
        pipeline.automerge(self.cfg, self.run, self.layer, None, why, [self.c], "about")
        self.assertEqual(self.calls, [])

    def test_person_only_work_never_automerges(self):
        for reason in ("risk", "pr trust", "triage"):
            with self.subTest(reason=reason):
                self.calls.clear()
                self.draft = True
                self.score = 0.9 if reason == "risk" else 0.1
                self.layer.trust_level = "pr" if reason == "pr trust" else "automerge"
                approval = {"sent_by": "triage"} if reason == "triage" else None
                ready = pipeline.ready_pushed(self.run, [self.c], [], "")
                self.assertEqual(ready, [self.c])
                pipeline.automerge(self.cfg, self.run, self.layer, approval, "", ready, "about")
                self.assertEqual(self.names("pr", "merge"), [])
                if reason == "risk":
                    comments = [body for args, body in self.calls if args[:2] == ["pr", "comment"]]
                    self.assertEqual(len(comments), 1)
                    self.assertIn("Automerge stays off for this PR", comments[0])

    def test_unreviewed_heads_and_person_owned_prs_are_left_alone(self):
        for reason in ("moved head", "missing head", "missing lease", "unreadable", "ci stopped", "repair", "feedback"):
            with self.subTest(reason=reason):
                self.calls.clear()
                self.remote_head = "f" * 40 if reason == "moved head" else "" if reason == "missing head" else self.head
                self.c.lease = "" if reason == "missing lease" else self.head
                self.fail = reason == "unreadable"
                self.run.repairs = "earlier" if reason == "repair" else ""
                self.run.feedback = ["owner feedback"] if reason == "feedback" else []
                stopped = [self.c] if reason == "ci stopped" else []
                self.draft = True
                ready = pipeline.ready_pushed(self.run, [self.c], stopped, "")
                self.assertEqual(ready, [])
                pipeline.automerge(self.cfg, self.run, self.layer, None, "", ready, "about")
                self.draft = False
                self.assertEqual(pipeline.hold_pushed(self.run, [self.c], stopped, "held"), [])
                self.assertEqual(self.names("pr", "ready"), [])
                self.assertEqual(self.names("pr", "merge"), [])


if __name__ == "__main__":
    unittest.main()
