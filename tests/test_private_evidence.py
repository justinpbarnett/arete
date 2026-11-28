import contextlib
import json
import os
import stat
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import isolate_live_state, seed_db

from arete import (
    board,
    config,
    dashboard,
    inbox,
    jira,
    outbound,
    paths,
    pipeline,
    poll,
    private_evidence,
    prompt_loader,
    record,
    tailnet,
    ticket_feedback,
    ticket_media,
    tracker,
)
from arete.store import db as foundation
from arete.store import evidence as evidence_store
from arete.store import inbox as inbox_store
from arete.store import principals as principals_store
from arete.store import tokens as token_store
from tests.test_ticket_media import make_mp4_stub, make_png, stub_keyframes


def setUpModule():
    isolate_live_state()


class _MutableJiraClient:
    """In-memory Jira client that records all outbound comments/labels and lets tests flip attachment access."""

    base = "https://example.atlassian.net"

    def __init__(self, issues: dict[str, dict], comments_by_key: dict[str, list[dict]], blobs: dict[str, bytes | Exception]):
        self.issues = issues
        self.comments_by_key = comments_by_key
        self.blobs = blobs
        self.posted_comments: list[tuple[str, str]] = []
        self.label_edits: list[tuple[str, list[str], list[str]]] = []

    def me(self) -> dict:
        return {"accountId": "owner-1", "displayName": "Owner"}

    def issue(self, key: str, fields: list[str]) -> dict:
        return self.issues[key]

    def get(self, path: str, **params) -> dict:
        key = path.rstrip("/").rsplit("/", 1)[-1]
        if key in self.issues:
            return self.issues[key]
        raise jira.JiraError(f"not found: {path}", 404)

    def changelog(self, key: str) -> list[dict]:
        return []

    def comments(self, key: str) -> list[dict]:
        return list(self.comments_by_key.get(key, ()))

    def browse_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    def comment_url(self, key: str, comment_id) -> str:
        return f"{self.browse_url(key)}?focusedCommentId={comment_id}"

    def attachment(self, attachment_id: str) -> bytes:
        val = self.blobs[attachment_id]
        if isinstance(val, Exception):
            raise val
        return val

    def comment(self, key: str, body: str, *, return_url: bool = False, single_attempt: bool = False) -> str | None:
        self.posted_comments.append((key, body))
        cid = str(len(self.posted_comments) + 100)
        self.comments_by_key.setdefault(key, []).append({
            "id": cid,
            "author": {"accountId": "owner-1", "displayName": "Owner"},
            "created": "2026-11-27T12:00:00.000+0000",
            "body": body,
        })
        return self.comment_url(key, cid) if return_url else None

    def edit_labels(self, key: str, add: list[str], remove: list[str]) -> None:
        self.label_edits.append((key, list(add), list(remove)))


def _jira_issue(
    key: str,
    summary: str,
    description: str,
    *,
    attachments: list[dict] | None = None,
    status_key: str = "new",
) -> dict:
    return {
        "id": key.replace("-", ""),
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "status": {"name": "To Do" if status_key != "done" else "Done", "statusCategory": {"key": status_key}},
            "labels": ["agent", "repo-app"],
            "issuetype": {"name": "Bug"},
            "created": "2026-11-27T10:00:00.000+0000",
            "updated": "2026-11-27T10:05:00.000+0000",
            "creator": {"accountId": "owner-1"},
            "assignee": {"accountId": "owner-1"},
            "attachment": attachments or [],
        },
    }


class _PrivateEvidenceDbCase(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="arete-pe-"))).resolve()
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
        seed_db(state)
        self.repo = config.Repo(name="app", path=root, github="owner/app", pipeline=True, tracker="jira", jira_project="APP")
        self.cfg = config.Config(repos=[self.repo])

    def _jira_tracker(self, client: _MutableJiraClient) -> tracker.JiraTracker:
        tk = tracker.JiraTracker(self.repo, config.JiraConfig(project="APP"))
        orig_prop = type(tk).client
        type(tk).client = property(lambda _self: client)
        self.addCleanup(setattr, type(tk), "client", orig_prop)
        return tk

    @contextlib.contextmanager
    def _http_server(self):
        principals_store.seed_owner("owner")
        token_store.init_owner()
        token_row, bearer = token_store.issue(principals_store.owner(), "triage-test", ["read", "inbox:resolve"])
        self.assertTrue(token_row["id"])
        node = SimpleNamespace(names={"127.0.0.1"})
        logins = tailnet.Logins(tailnet.ScratchHost())
        logins.of = lambda _: "owner"
        with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
            server = tailnet.Server("127.0.0.1", 0, node, logins, False, dashboard.Handler, dashboard.Redirect)
        with server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                yield server.server_port, bearer
            finally:
                server.shutdown()
                thread.join(timeout=5)


class JiraPrivateTriageUploadTests(_PrivateEvidenceDbCase):
    def test_denied_jira_attachment_pauses_privately_rejects_unreadable_and_resumes_from_triage_upload(self):
        att_secret = "ATATT3xFfGF0SyntheticJiraSecretToken12345"
        client = _MutableJiraClient(
            issues={
                "APP-12": _jira_issue(
                    "APP-12",
                    "Fix save flicker shown in recording",
                    "See attached recording !repro.mp4! for the exact failure.",
                    attachments=[{
                        "id": "att-403",
                        "filename": "repro.mp4",
                        "size": 400,
                        "mimeType": "video/mp4",
                        "author": {"accountId": "owner-1"},
                        "created": "2026-11-27T10:01:00.000+0000",
                    }],
                ),
            },
            comments_by_key={},
            blobs={
                "att-403": jira.JiraError(
                    f"jira GET /rest/api/2/attachment/content/att-403: HTTP 403 Forbidden (token {att_secret})",
                    403,
                ),
            },
        )
        tk = self._jira_tracker(client)
        ticket = tk.get("APP-12", include_environment=False)
        run_id = record.start("pipeline", repo="app", issue="APP-12")
        run_dir = paths.RUNS_DIR / run_id
        run = pipeline.Run(run_id, self.repo, run_dir, tracker=tk, tid="APP-12", cfg=self.cfg)

        # 1. Worker encounters denied attachment -> pauses quietly without posting any comment or label on Jira.
        with mock.patch.object(tk, "get", return_value=ticket), mock.patch.object(
            tk, "save_images", side_effect=lambda t, d: tracker.JiraTracker.save_images(tk, t, d, keyframe_extractor=stub_keyframes)
        ):
            status, summary, pr_url = pipeline._run(self.cfg, run, mock.MagicMock())

        self.assertEqual(status, "needs-info")
        self.assertEqual(pr_url, "")
        self.assertIn("repro.mp4", summary)
        self.assertEqual(client.posted_comments, [])
        self.assertEqual(client.label_edits, [])
        self.assertNotIn(att_secret, summary)

        # Verify private Triage inbox item identifies what is missing, without leaking credentials,
        # and does not create a duplicate run:<run_id> public-reply item.
        triage_view = inbox.triage_screen()
        ev_items = [item for item in triage_view["items"] if item["subject"].startswith("evidence:")]
        run_items = [item for item in triage_view["items"] if item["subject"].startswith("run:")]
        self.assertEqual(len(ev_items), 1)
        self.assertEqual(run_items, [])
        ev_item = ev_items[0]
        self.assertEqual(ev_item["kind"], "evidence")
        self.assertEqual(ev_item["priority"], 1)
        self.assertIn("APP-12", ev_item["title"])
        self.assertIn("repro.mp4", ev_item["title"])
        self.assertNotIn(att_secret, json.dumps(ev_item))
        self.assertNotIn("private_path", ev_item["detail"])
        # Board needs_you also exposes the evidence row for the web Triage UI.
        needs_you = board.inbox_needs_you([])
        self.assertEqual(len(needs_you["evidence"]["rows"]), 1)
        self.assertEqual(needs_you["evidence"]["rows"][0]["inbox_id"], ev_item["id"])

        # 2. Upload an unreadable file via the HTTP Triage API -> rejected (422), question stays open, no resume.
        resumed_calls: list[tuple[str, str, str]] = []
        with self._http_server() as (port, bearer):
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request(
                    "POST",
                    f"/api/v1/inbox/{ev_item['id']}/evidence",
                    body=b"this-is-corrupt-not-a-video",
                    headers={
                        "Authorization": f"Bearer {bearer}",
                        "Content-Type": "application/octet-stream",
                        "X-Evidence-Filename": "../../evil-corrupt.mp4",
                        "Idempotency-Key": "upload-corrupt-1",
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read())
                self.assertEqual(resp.status, 422)
                self.assertEqual(body["code"], "validation")
            finally:
                conn.close()

            still_open = inbox.get(ev_item["id"])["item"]
            self.assertEqual(still_open["state"], "open")
            self.assertTrue(still_open["detail"]["last_error"])
            self.assertTrue(evidence_store.has_open("app", "APP-12"))

            # 3. Upload a readable private copy via Triage HTTP API -> associates with original reference & resumes.
            def fake_resume(repo_name: str, tid: str, prev_run: str) -> dict:
                resumed_calls.append((repo_name, tid, prev_run))
                return {"task": tid, "repo": repo_name, "run": "run-resumed-1", "state": "running", "why": "started"}

            with (
                mock.patch.object(ticket_media, "extract_video_keyframes", side_effect=stub_keyframes),
                mock.patch.object(private_evidence, "resume_ticket", side_effect=fake_resume),
            ):
                conn = HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    conn.request(
                        "POST",
                        f"/api/v1/inbox/{ev_item['id']}/evidence",
                        body=make_mp4_stub(),
                        headers={
                            "Authorization": f"Bearer {bearer}",
                            "Content-Type": "application/octet-stream",
                            "X-Evidence-Filename": "../../repro.mp4",
                            "Idempotency-Key": "upload-valid-1",
                        },
                    )
                    resp = conn.getresponse()
                    uploaded = json.loads(resp.read())
                    self.assertEqual(resp.status, 200, uploaded)
                finally:
                    conn.close()

        self.assertEqual(uploaded["item"]["state"], "resolved")
        self.assertEqual(uploaded["item"]["resolution"], "uploaded")
        self.assertEqual(uploaded["result"]["run"], "run-resumed-1")
        self.assertEqual(resumed_calls, [("app", "APP-12", run_id)])
        # Still zero comments on Jira (no public copy, no ticket comment).
        self.assertEqual(client.posted_comments, [])

        # Private file is confined inside STATE_DIR/private-evidence with 0o600 permissions.
        ev_row = evidence_store.get(ev_item["detail"]["id"])
        self.assertIsNotNone(ev_row)
        stored_file = private_evidence.safe_private_file(ev_row["private_path"])
        self.assertIsNotNone(stored_file)
        self.assertTrue(stored_file.is_relative_to(private_evidence.private_dir().resolve()))
        self.assertEqual(stat.S_IMODE(os.stat(stored_file).st_mode), 0o600)

        # 4. Resumed worker runs collect_ticket_attachments while Jira STILL returns 403 ->
        #    associates private copy with original Jira attachment_id, extracts keyframes, and passes to worker prompt!
        resumed_dir = paths.RUNS_DIR / "run-resumed-1"
        with mock.patch.object(
            tk, "save_images", side_effect=lambda t, d: tracker.JiraTracker.save_images(tk, t, d, keyframe_extractor=stub_keyframes)
        ):
            attachments_md, add_dirs, manifest = pipeline.collect_ticket_attachments(
                tk,
                ticket,
                resumed_dir,
                repo_name="app",
                run_id="run-resumed-1",
                keyframe_extractor=stub_keyframes,
            )

        self.assertEqual(manifest["needs_private_help"], [])
        self.assertEqual(len(manifest["media"]), 1)
        self.assertEqual(manifest["media"][0]["status"], "resolved")
        self.assertEqual(manifest["media"][0]["ref"]["attachment_id"], "att-403")
        self.assertEqual(manifest["media"][0]["ref"]["name"], "repro.mp4")
        self.assertEqual(len(manifest["media"][0]["keyframes"]), 3)
        self.assertIn(resumed_dir / "images", add_dirs)
        self.assertIn("frame-02-1.50s.png", attachments_md)
        self.assertNotIn("private-evidence", attachments_md)

        brief = prompt_loader.Brief(
            "app", "ws", ticket.ref, ticket.title, ticket.body,
            attachments=attachments_md, expertise="", commands="", max_lines=100,
        )
        rendered = brief.prompt("build", plan="Fix flicker", checks="pytest", verify="check", estimate=10)
        self.assertIn("frame-02-1.50s.png", rendered)
        self.assertNotIn("private-evidence", rendered)


class RestoredAccessAndIsolationTests(_PrivateEvidenceDbCase):
    def test_restoring_access_resumes_work_while_still_unreadable_keeps_question_open(self):
        client = _MutableJiraClient(
            issues={
                "APP-20": _jira_issue(
                    "APP-20",
                    "Fix header alignment",
                    "Screenshot: !header.png!",
                    attachments=[{
                        "id": "att-20",
                        "filename": "header.png",
                        "size": 120,
                        "mimeType": "image/png",
                        "author": {"accountId": "owner-1"},
                        "created": "2026-11-27T10:01:00.000+0000",
                    }],
                ),
            },
            comments_by_key={},
            blobs={"att-20": jira.JiraError("HTTP 403 Forbidden", 403)},
        )
        tk = self._jira_tracker(client)
        ticket = tk.get("APP-20", include_environment=False)
        _, _, manifest = pipeline.collect_ticket_attachments(tk, ticket, self.root / "r1", repo_name="app", run_id="r1")
        self.assertEqual(len(manifest["needs_private_help"]), 1)
        ev_id = manifest["needs_private_help"][0]["id"]
        item = inbox_store.get_open_by_subject(f"evidence:{ev_id}")
        self.assertIsNotNone(item)

        # Access restored to a CORRUPT file -> still unreadable, keeps question open.
        client.blobs["att-20"] = b"corrupt-png-bytes"
        with self.assertRaises(private_evidence.StillInaccessible):
            private_evidence.confirm_access_restored(ev_id, tk=tk, auto_resume=False)
        self.assertTrue(evidence_store.has_open("app", "APP-20"))
        self.assertEqual(inbox.get(item["id"])["item"]["state"], "open")

        # Now restore access to a READABLE PNG -> closes question and triggers resumption.
        client.blobs["att-20"] = make_png()
        resumed: list[tuple[str, str, str]] = []
        result = private_evidence.confirm_access_restored(
            ev_id,
            tk=tk,
            auto_resume=True,
            resumer=lambda repo, tid, rid: (resumed.append((repo, tid, rid)) or {"run": "r2", "state": "running"}),
        )
        self.assertEqual(result["evidence"]["status"], "access_restored")
        self.assertEqual(resumed, [("app", "APP-20", "r1")])
        self.assertFalse(evidence_store.has_open("app", "APP-20"))
        self.assertEqual(inbox.get(item["id"])["item"]["state"], "resolved")
        self.assertEqual(client.posted_comments, [])

    def test_only_affected_work_and_dependents_wait_and_repeated_notifications_do_not_duplicate(self):
        client = _MutableJiraClient(
            issues={
                "APP-30": _jira_issue(
                    "APP-30",
                    "Blocked ticket",
                    "See !missing.png!",
                    attachments=[{
                        "id": "att-30",
                        "filename": "missing.png",
                        "size": 100,
                        "mimeType": "image/png",
                        "author": {"accountId": "owner-1"},
                        "created": "2026-11-27T10:01:00.000+0000",
                    }],
                ),
                "APP-31": _jira_issue("APP-31", "Dependent ticket", "Waits on APP-30\n\nBuild on top of APP-30."),
                "APP-32": _jira_issue("APP-32", "Independent ticket", "Unrelated fix with no missing evidence."),
            },
            comments_by_key={},
            blobs={"att-30": jira.JiraError("HTTP 403 Forbidden", 403)},
        )
        tk = self._jira_tracker(client)
        t30 = tk.get("APP-30", include_environment=False)
        t31 = tk.get("APP-31", include_environment=False)
        t32 = tk.get("APP-32", include_environment=False)

        # Repeatedly hitting missing evidence on APP-30 does not create duplicate records or inbox items.
        for n in range(3):
            pipeline.collect_ticket_attachments(tk, t30, self.root / f"run-{n}", repo_name="app", run_id=f"run-{n}")
        self.assertEqual(len(evidence_store.list_for_ticket("app", "APP-30")), 1)
        self.assertEqual(len(inbox.list(state="open", kind="evidence")), 1)

        # Dependent APP-31 is blocked on APP-30; independent APP-32 has no blocker.
        self.assertEqual(t31.blocker_kind, tracker.DEPENDENCY_WAIT)
        self.assertIn("APP-30", t31.blocker)
        self.assertEqual(t32.blocker, "")

        # Poll start_pick: APP-30 stays blocked on private evidence, APP-31 waits on APP-30, APP-32 starts!
        layer = config.RepoLayer(self.root / ".agents", base_branch="main")
        queue = poll.RepoQueue(self.repo, tk, set(), layer, mock.MagicMock())
        skips = pipeline.SkipLog({})
        spawned: list[str] = []

        with (
            mock.patch.object(tracker, "for_ticket", return_value=tk),
            mock.patch.object(poll, "step_without_route", return_value=""),
            mock.patch.object(poll, "spawn_unparked", side_effect=lambda _r, tid, *_a, **_kw: spawned.append(str(tid)) or True),
        ):
            r30_first = poll.start_pick(self.cfg, queue, poll.Pick("APP-30", "t30", from_queue=True), skips, False, poll.Counter(), 4)
            r30_again = poll.start_pick(self.cfg, queue, poll.Pick("APP-30", "t30", from_queue=True), skips, False, poll.Counter(), 4)
            r31 = poll.start_pick(self.cfg, queue, poll.Pick("APP-31", "t31", from_queue=True), skips, False, poll.Counter(), 4)
            r32 = poll.start_pick(self.cfg, queue, poll.Pick("APP-32", "t32", from_queue=True), skips, False, poll.Counter(), 4)

        self.assertEqual(r30_first, "blocked")
        self.assertEqual(r30_again, "blocked")
        self.assertEqual(r31, "blocked")
        self.assertEqual(r32, "started")
        self.assertEqual(spawned, ["APP-32"])
        self.assertEqual(len(inbox.list(state="open", kind="evidence")), 1)

        # Once access is restored on APP-30, the next poll probes, resolves the private ask, and starts APP-30 automatically.
        client.blobs["att-30"] = make_png()
        with (
            mock.patch.object(tracker, "for_ticket", return_value=tk),
            mock.patch.object(poll, "step_without_route", return_value=""),
            mock.patch.object(poll, "spawn_unparked", side_effect=lambda _r, tid, *_a, **_kw: spawned.append(str(tid)) or True),
        ):
            r30_resumed = poll.start_pick(self.cfg, queue, poll.Pick("APP-30", "t30", from_queue=True), skips, False, poll.Counter(), 4)

        self.assertEqual(r30_resumed, "started")
        self.assertEqual(spawned, ["APP-32", "APP-30"])
        self.assertFalse(evidence_store.has_open("app", "APP-30"))
        self.assertEqual(inbox.list(state="open", kind="evidence"), [])


class PrivacyBoundaryAndProductClarificationTests(_PrivateEvidenceDbCase):
    def test_synthetic_contents_credentials_and_private_links_stay_inside_arete(self):
        secret_token = "xoxb-synthetic-slack-secret-987654321"
        synthetic_body = f"Private requirement: sort by priority_rank desc. Ignore token {secret_token}."
        ticket = tracker.Ticket(
            "45",
            "#45",
            "Apply Slack thread ordering",
            body="Follow https://acme.slack.com/archives/C0123456789/p1710000000123456",
        )
        gh_repo = config.Repo(name="demo", path=self.root, github="owner/demo", pipeline=True, tracker="github")
        tk = tracker.GitHubTracker(gh_repo)

        # Missing Slack credential raises a private Triage item without touching GitHub.
        _, _, manifest = pipeline.collect_ticket_attachments(
            tk,
            ticket,
            self.root / "r-slack-1",
            repo_name="demo",
            run_id="r-slack-1",
            external_kwargs={"slack_token_value": ""},
        )
        self.assertEqual(len(manifest["needs_private_help"]), 1)
        ev_id = manifest["needs_private_help"][0]["id"]
        item = inbox_store.get_open_by_subject(f"evidence:{ev_id}")
        self.assertIsNotNone(item)

        # Upload a private text copy containing a synthetic credential -> credential is scrubbed before storage.
        uploaded = inbox.upload_evidence(
            item["id"],
            synthetic_body.encode("utf-8"),
            filename="thread.md",
            content_type="text/markdown",
            auto_resume=False,
        )
        self.assertEqual(uploaded["item"]["state"], "resolved")
        # Inbox item & events must never contain raw file contents, credentials, or private filesystem paths.
        history_json = json.dumps({"item": uploaded["item"], "history": inbox.history(item["id"])})
        self.assertNotIn("priority_rank", history_json)
        self.assertNotIn(secret_token, history_json)
        self.assertNotIn("private-evidence", history_json)

        # Worker resolves the original Slack reference from the private copy and gets the requirement without the secret.
        note, _, manifest2 = pipeline.collect_ticket_attachments(
            tk,
            ticket,
            self.root / "r-slack-2",
            repo_name="demo",
            run_id="r-slack-2",
            external_kwargs={"slack_token_value": ""},
        )
        self.assertEqual(manifest2["needs_private_help"], [])
        self.assertIn("sort by priority_rank desc", note)
        self.assertNotIn(secret_token, note)
        self.assertIn("[redacted]", note)

        # Defense-in-depth: outbound.screen strips private upload API paths and private-evidence paths.
        leaked = (
            f"Upload link http://127.0.0.1:8796/api/v1/inbox/{item['id']}/evidence "
            f"and /api/v1/inbox/{item['id']}/evidence and /tmp/state/private-evidence/{ev_id}/thread.md"
        )
        screened = outbound.screen(leaked)
        self.assertNotIn(f"/inbox/{item['id']}/evidence", screened)
        self.assertNotIn(f"private-evidence/{ev_id}", screened)
        self.assertNotIn("127.0.0.1", screened)

    def test_readable_ambiguous_feedback_still_uses_tracker_clarification_path(self):
        client = _MutableJiraClient(
            issues={
                "APP-50": _jira_issue(
                    "APP-50",
                    "Export button behavior",
                    "See readable screenshot !export.png!",
                    attachments=[{
                        "id": "att-50",
                        "filename": "export.png",
                        "size": 100,
                        "mimeType": "image/png",
                        "author": {"accountId": "owner-1"},
                        "created": "2026-11-27T10:01:00.000+0000",
                    }],
                ),
            },
            comments_by_key={},
            blobs={"att-50": make_png()},
        )
        tk = self._jira_tracker(client)
        ticket = tk.get("APP-50", include_environment=False)

        # Readable attachment -> zero private Triage items raised.
        _, _, manifest = pipeline.collect_ticket_attachments(tk, ticket, self.root / "r-50", repo_name="app", run_id="r-50")
        self.assertEqual(manifest["needs_private_help"], [])
        self.assertEqual(inbox.list(state="open", kind="evidence"), [])

        # Ambiguous product comment -> uses tracker clarification path and resumes once answered on the ticket!
        ambiguous = ticket_feedback.ThreadComment(
            "201",
            "qa-1",
            "QA Tester",
            "The export looks good, not sure whether it should include archived rows or active rows only.",
            client.comment_url("APP-50", "201"),
            "2026-11-27T10:10:00Z",
        )
        ctx = ticket_feedback.build_context(self.cfg, self.repo, tk, "APP-50", ticket=ticket, comments=[ambiguous])
        clar_outcome = ticket_feedback.process_comment(self.cfg, self.repo, tk, ambiguous, ctx)
        self.assertEqual(clar_outcome.intent, "product_question")
        self.assertEqual(clar_outcome.route, "clarify")
        self.assertEqual(len(client.posted_comments), 1)
        self.assertEqual(inbox.list(state="open", kind="evidence"), [])

        answer = ticket_feedback.ThreadComment(
            "202",
            "qa-1",
            "QA Tester",
            "Include active rows only.",
            client.comment_url("APP-50", "202"),
            "2026-11-27T10:15:00Z",
        )
        ctx2 = ticket_feedback.build_context(self.cfg, self.repo, tk, "APP-50", ticket=ticket, comments=[ambiguous, answer])
        ans_outcome = ticket_feedback.process_comment(self.cfg, self.repo, tk, answer, ctx2)
        self.assertEqual(ans_outcome.intent, "correction")
        self.assertEqual(ans_outcome.route, "worker")
        self.assertEqual(ans_outcome.status, "queued")
