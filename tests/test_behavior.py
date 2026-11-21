import base64
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing, contextmanager, redirect_stderr
from http.client import HTTPConnection, HTTPResponse
from io import BytesIO, StringIO
from pathlib import Path
from socket import SHUT_WR, create_connection
from types import SimpleNamespace
from unittest import mock

from arete import (
    accounts, api_check, api_http, chat, config, contract, control, dashboard, event_stream, events, gh, idempotency,
    jira, paths, pipeline, principals, screening, serve, tailnet, tasks, triage, util, verify,
)
from arete.store import approvals as approvals_store
from arete.store import chats as chats_store
from arete.store import db
from arete.store import idempotency as claims
from arete.store import runs as run_store
from arete.store import triage as triage_store
from tests import support, versioned_api_fixture


def setUpModule():
    support.isolate_live_state()


class DashboardSnapshotTests(unittest.TestCase):
    def test_event_client_surface(self):
        web = Path(__file__).resolve().parents[1] / "arete" / "web"
        source = (web / "ui-events.js").read_text() + (web / "ui-live.js").read_text()
        for name in ("EventSource", "navigator.locks", "BroadcastChannel", "Last-Event-ID", "reset",
                     "/api/v1/events", "timeout"):
            self.assertIn(name, source)
        for name in ("innerHTML", "insertAdjacentHTML", "eval"):
            self.assertNotIn(name, source)

    def test_snapshot_and_event_behavior(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("ui_events.mjs").resolve())],
            capture_output=True, text=True, timeout=8, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class VerifyFaultTests(unittest.TestCase):
    token = "arete-placeholder-fault-test"
    controls = (("POST", "/api/_verify/fault"), ("POST", "/api/_verify/streams/drop"),
                ("POST", "/api/_verify/event"), ("DELETE", "/api/_verify/faults"))

    def setUp(self):
        self.login = "fixture@example.test"
        self.actor = principals.seed_owner(self.login)

    @contextmanager
    def server(self, token, *, isolated=True):
        with mock.patch.dict(os.environ), mock.patch.object(paths, "ISOLATED", isolated):
            if token is None:
                os.environ.pop("ARETE_VERIFY_FAULTS", None)
            else:
                os.environ["ARETE_VERIFY_FAULTS"] = token
            handlers = dashboard.handlers()
        address = "127.0.0.1"
        node = SimpleNamespace(names={address})
        logins = SimpleNamespace(of=lambda _: self.login)
        with tailnet.Server(address, 0, node, logins, not isolated, handlers.open, handlers.redirect) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                yield server.server_port
            finally:
                server.shutdown()
                thread.join()

    def request(self, port, method, path, *, token=None, body=None, headers=()):
        headers = dict(headers)
        if token is not None:
            headers["X-Arete-Verify-Faults"] = token
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if method == "POST":
            headers["X-Arete"] = "1"
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as client:
            client.request(method, path, body, headers)
            with client.getresponse() as result:
                return result.status, json.load(result)

    def armed(self, port, route, count):
        return self.request(port, "POST", "/api/_verify/fault", token=self.token,
                            body={"route": route, "status": 404, "body": {"fault": "armed"}, "count": count})

    def initial_cursor(self, port):
        status, data = self.request(port, "GET", "/api/v1/events?timeout=0", headers={"Accept": "application/json"})
        self.assertEqual(status, 200)
        return data["next"]

    @contextmanager
    def stream(self, port, cursor):
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as client:
            client.request("GET", "/api/v1/events", headers={"Last-Event-ID": cursor, "Accept": "text/event-stream"})
            with client.getresponse() as result:
                self.assertEqual(result.status, 200)
                self.assertEqual(result.readline(), b"retry: 2000\n")
                self.assertEqual(result.readline(), b"\n")
                yield result

    def read_event(self, stream):
        position = stream.readline().removeprefix(b"id: ").strip().decode()
        data = json.loads(stream.readline().removeprefix(b"data: "))
        self.assertEqual(stream.readline(), b"\n")
        self.assertEqual(position, data["id"])
        return data

    def test_control_routes_are_off_without_opt_in_and_on_live_serve(self):
        for token, isolated in ((None, True), (self.token, False)):
            with self.subTest(isolated=isolated), self.server(token, isolated=isolated) as port:
                for method, path in self.controls:
                    with self.subTest(path=path):
                        self.assertEqual(self.request(port, method, path, token=self.token)[0], 404)
                self.assertEqual(verify.fault_note(f"http://127.0.0.1:{port}", self.token), "")

    def test_every_control_rejects_missing_or_wrong_token(self):
        with self.server(self.token) as port:
            for method, path in self.controls:
                for supplied in (None, "another-attempt"):
                    with self.subTest(path=path, token=supplied):
                        self.assertEqual(self.request(port, method, path, token=supplied)[0], 404)

    def test_relayed_fault_discovery_requires_this_attempts_proof(self):
        faults = verify.verify_faults
        with (self.server(self.token) as port, verify.relayed(f"http://127.0.0.1:{port}") as reach,
              mock.patch.object(faults.Controls, "prove", autospec=True, side_effect=faults.Controls.prove) as prove):
            note = verify.fault_note(reach, self.token)
            for _, path in self.controls:
                self.assertIn(path, note)
            self.assertEqual(verify.fault_note(reach, "another-attempt"), "")

        first, second = (call.args[1] for call in prove.call_args_list)
        self.assertNotIn(faults.HEADER, first)
        self.assertNotIn(faults.HEADER, second)
        self.assertNotEqual(first[faults.CHALLENGE_HEADER], second[faults.CHALLENGE_HEADER])

    def test_armed_response_is_consumed_twice_then_normal_dispatch_resumes(self):
        with self.server(self.token) as port:
            for route, normal_status in (("/api/v1/screen/board", 404), ("/api/v1/screen?view=live", 200)):
                with self.subTest(route=route):
                    before = self.request(port, "GET", route)
                    self.assertEqual(before[0], normal_status)
                    self.assertEqual(self.armed(port, route, count=2), (200, {"ok": True}))
                    for _ in range(2):
                        self.assertEqual(self.request(port, "GET", route), (404, {"fault": "armed"}))
                    self.assertEqual(self.request(port, "GET", route), before)
                    self.assertEqual(self.armed(port, route, count=2)[0], 200)
                    self.assertEqual(self.request(port, "DELETE", "/api/_verify/faults", token=self.token)[0], 200)
                    self.assertEqual(self.request(port, "GET", route), before)
            note = verify.fault_note(f"http://127.0.0.1:{port}", self.token)
            for _, path in self.controls:
                self.assertIn(path, note)

    def start_run(self, run):
        with principals.acting(self.actor):
            run_store.insert_run(run, {"kind": "pipeline", "started": util.iso(), "status": "running"})

    def test_drop_closes_open_streams_and_reconnect_resumes_after_last_event(self):
        with self.server(self.token) as port:
            cursor = self.initial_cursor(port)
            with self.stream(port, cursor) as first, self.stream(port, cursor) as second:
                self.start_run("fault-drop-first")
                last = self.read_event(first)["id"]
                self.assertEqual(self.read_event(second)["id"], last)
                self.assertEqual(self.request(port, "POST", "/api/_verify/streams/drop", token=self.token)[0], 200)
                self.assertEqual(first.read(), b"")
                self.assertEqual(second.read(), b"")
            status, polled = self.request(port, "GET", "/api/v1/events?timeout=0",
                                          headers={"Accept": "application/json", "Last-Event-ID": last})
            self.assertEqual((status, polled), (200, {"events": [], "next": last, "reset": None}))
            with self.stream(port, last) as reconnected:
                self.start_run("fault-drop-second")
                self.assertEqual(self.read_event(reconnected)["subject"], "run:fault-drop-second")

    def test_drop_after_heartbeat_reconnects_from_the_last_received_id(self):
        clock = mock.Mock()
        clock.monotonic.return_value = 0
        envelope = events.envelope

        def make_beat_due(conn, row, epoch):
            data = envelope(conn, row, epoch)
            if data["subject"] == "run:fault-before-beat":
                clock.monotonic.return_value = 15
            return data

        with (mock.patch.object(event_stream, "time", clock),
              mock.patch.object(events, "envelope", side_effect=make_beat_due), self.server(self.token) as port):
            with self.stream(port, self.initial_cursor(port)) as stream:
                self.start_run("fault-before-beat")
                before_beat = self.read_event(stream)
                last = stream.readline().removeprefix(b"id: ").strip().decode()
                self.assertEqual(stream.readline(), b"event: serve.beat\n")
                self.assertEqual(json.loads(stream.readline().removeprefix(b"data: "))["kind"], "serve.beat")
                self.assertEqual(stream.readline(), b"\n")
                self.assertEqual(self.request(port, "POST", "/api/_verify/streams/drop", token=self.token)[0], 200)
                self.assertEqual(stream.read(), b"")
            with self.stream(port, last) as reconnected:
                self.start_run("fault-after-beat")
                self.assertEqual(self.read_event(reconnected)["subject"], "run:fault-after-beat")
            self.assertEqual(last, before_beat["id"])

    def test_run_ended_is_delivered_through_the_real_event_log(self):
        run = "fault-event-run"
        self.start_run(run)
        with self.server(self.token) as port, self.stream(port, self.initial_cursor(port)) as stream:
            with principals.acting(self.actor):
                settled = run_store.settle(run, {"status": "failed", "finished": util.iso()})
            real = self.read_event(stream)
            self.assertEqual(real["kind"], "run.ended")
            self.assertEqual(self.request(port, "POST", "/api/_verify/event", token=self.token,
                                          body={"kind": "run.ended", "run": run})[0], 200)
            synthetic = self.read_event(stream)
            self.assertGreater(synthetic["seq"], real["seq"])
            for field in real.keys() - {"seq", "at", "id", "via"}:
                self.assertEqual(synthetic[field], real[field], field)
            self.assertEqual(run_store.run_get(run), settled)


class ApiCheckTests(unittest.TestCase):
    serial = True
    class_timeout = 20  # Hosted macOS reached streaming but exhausted the unit-test budget.

    def test_api_check(self):
        with tempfile.TemporaryDirectory(prefix="arete-api-cli-") as temp:
            root = Path(temp).resolve()
            command = api_check.child_command(versioned_api_fixture.SOURCE, "from arete.cli import main; sys.exit(main())",
                                              "api", "check")
            result = subprocess.run(command, cwd=root, env=api_check.isolated_env(root),
                                    timeout=api_check.CHECK_TIMEOUT + 5, check=False)
            self.assertEqual(result.returncode, 0, "the API smoke command failed; see its scenario output above")


class ApiCompatibilityTests(unittest.TestCase):
    serial = True

    def test_api_diff(self):
        versioned_api_fixture.evolution_temp()


class VersionedApiTests(unittest.TestCase):
    def setUp(self):
        self.actor = principals.seed_owner("fixture@example.test")
        self.repo = config.Repo("fixture", Path("."), github="fixture/repo", tracker="local")
        self.cfg = config.Config(repos=[self.repo])
        self.handler = SimpleNamespace(body=mock.Mock(), headers={"Idempotency-Key": self.id()},
                                       send=lambda *args: args, log_error=mock.Mock(), write_receipt=None,
                                       idempotent_replayed=False, keyed_delivery=False)

    def request(self, body):
        self.handler.body.return_value = body
        with principals.acting(self.actor), mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                mock.patch.object(tasks, "begin", side_effect=lambda cfg, repo, tid, **kw:
                                  tasks.answer(tid, repo.name, "", "queued", "capacity")):
            return dashboard.Handler.keyed_start(self.handler)

    def test_keyed_replay(self):
        body = {"repo": "fixture", "prompt": "Add kumquat"}
        first = self.request(body)
        self.assertEqual(first[0], 200)
        replayed = self.request({"prompt": "Add kumquat", "repo": "fixture"})
        self.assertEqual(replayed, first)
        self.assertTrue(self.handler.idempotent_replayed)
        with self.assertRaises(idempotency.Reused):
            self.request({**body, "prompt": "Changed"})
        with db.reader() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        saved = claims.lookup(self.actor, self.id())
        self.assertEqual(bytes(saved["response"]), first[1])
        self.assertEqual(claims.target(saved)[0], "fixture")
        self.handler.headers["Idempotency-Key"] = "handled-error"
        refused = self.request({"repo": "unknown", "prompt": "Add kumquat"})
        self.assertEqual(refused[0], 400)
        self.assertEqual(self.request({"repo": "unknown", "prompt": "Add kumquat"}), refused)
        with self.assertRaises(idempotency.Reused):
            self.request(body)

    def test_keyed_concurrency(self):
        sha = idempotency.fingerprint("POST", "/api/tasks", {}, {"repo": "fixture", "prompt": "Add kumquat"})
        with idempotency.owning(self.actor, "in-flight-key", sha):
            with self.assertRaises(idempotency.InFlight), idempotency.owning(self.actor, "in-flight-key", sha):
                self.fail("duplicate admitted")
            with self.assertRaises(idempotency.Reused), idempotency.owning(self.actor, "in-flight-key", "changed"):
                self.fail("changed request admitted")
            with idempotency.owning("another-actor", "in-flight-key", sha):
                pass
        with idempotency.owning(self.actor, "in-flight-key", sha):
            pass
        self.assertEqual(idempotency.ACTIVE, {})
        for key in ("", "short", "a" * 129, "bad key-572", "nonascii-é", "newline\n"):
            with self.subTest(key=key), self.assertRaises(control.Refused), idempotency.owning(self.actor, key, sha):
                self.fail("invalid key admitted")

    def test_versioned_read_surface(self):
        self.with_server(self.check_read_surface)

    def test_transport_and_publication_compatibility(self):
        self.with_server(self.check_transport)

    def test_restart_drains_without_discarding_an_admitted_publication(self):
        self.with_server(self.check_publication_drain)

    def with_server(self, exercise):
        address = "127.0.0.1"
        node = SimpleNamespace(names={address})
        logins = SimpleNamespace(of=lambda _: "fixture@example.test")
        with mock.patch("socket.getfqdn", return_value=address):
            server = tailnet.Server(address, 0, node, logins, False, dashboard.Handler, dashboard.Redirect)
        with server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                exercise(server.server_port)
            finally:
                server.shutdown()
                thread.join()

    def test_publication_recovers_remote_urls(self):
        self.cfg.arete_repo = self.repo.name
        source = triage.GitHubSource(self.repo.github, [self.repo])
        ticket = triage.Candidate("fixture#7", "7", "Question", gh.issue_url(self.repo.github, 7), "old")
        triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                         questions=["What happened?"]))
        chat_id = chats_store.chat_add(self.repo.name)
        chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Draft", draft_body="Chat issue body")
        approval_emit = {"type": "ticket", "item": {"repo": self.repo.name, "title": "Delivery",
                                                   "body": "Approval issue body", "labels": []}}
        approval_id = approvals_store.add_approval("fixture", "", approval_emit, "File the delivery")
        pr_item = {"repo": self.repo.github, "branch": "fixture/head", "base": "main", "workdir": ".",
                   "title": "Delivery PR", "body": "Approval PR body", "labels": [], "draft": True}
        pr_id = approvals_store.add_approval("fixture", "", {"type": "pr", "item": pr_item}, "File the PR")
        remote = {"comments": [], "issues": [], "pulls": []}
        searches = []
        labeled = []
        pushed = []
        comment_url = f"{ticket.url}#issuecomment-1"
        pr_url = f"https://github.com/{self.repo.github}/pull/42"

        def outside_tx():
            self.assertIsNone(getattr(db._OWNER, "active", None))

        def lookup(args):
            outside_tx()
            self.assertEqual(args[2:], ["--paginate", "--slurp"])
            endpoint, _, query = args[1].partition("?")
            kind = endpoint.split("/")[-1]
            prefix = f"repos/{self.repo.github}/issues/7" if kind == "comments" else f"repos/{self.repo.github}"
            self.assertEqual(endpoint, f"{prefix}/{kind}")
            if kind == "pulls":
                self.assertRegex(query, r"head=fixture%3Afixture%2F(?:head|retry)")
            searches.append(kind)
            return [remote[kind].copy()]

        def comment(repo, number, body, *, return_url, single_attempt):
            outside_tx()
            self.assertEqual((repo, number, return_url, single_attempt), (self.repo.github, 7, True, True))
            remote["comments"].append({"body": body, "html_url": comment_url})
            return comment_url

        def issue(repo, title, body, labels=(), *, single_attempt=False):
            outside_tx()
            self.assertEqual(repo, self.repo.github)
            self.assertEqual(single_attempt, title != "Legacy draft")
            number = len(remote["issues"]) + 20
            remote["issues"].append({"body": body, "html_url": gh.issue_url(repo, number)})
            return number

        def pull(repo, head, base, title, body, labels, *, draft, single_attempt):
            outside_tx()
            self.assertEqual((repo, base, draft, single_attempt), (self.repo.github, "main", True, True))
            self.assertIn(head, ("fixture/head", "fixture/retry"))
            url = pr_url if head == "fixture/head" else f"https://github.com/{repo}/pull/43"
            remote["pulls"].append({"body": body, "html_url": url})
            return url

        def label(row):
            outside_tx()
            labeled.append(row["ref"])

        def push(args, *unused, **kw):
            outside_tx()
            pushed.append(args)

        settle = approvals_store.settle_approval

        def settle_until_disconnect(ident, status, *args, **kwargs):
            if status != "failed":
                return settle(ident, status, *args, **kwargs)
            return False

        forbidden = AssertionError("credential lookup or external transport")
        with ExitStack() as patches:
            for patch in (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")),
              mock.patch.object(triage, "sources", return_value=[source]),
              mock.patch.object(source, "approve", side_effect=label),
              mock.patch.object(screening, "screen", return_value=None),
              mock.patch.object(screening, "held", return_value=False),
              mock.patch.object(chat.routing, "metered", return_value=False),
              mock.patch.object(dashboard, "start_poll_without_failing_the_send", return_value="Poll queued."),
              mock.patch.object(dashboard.api_http, "log_request"),
              mock.patch.object(accounts, "read", side_effect=forbidden),
              mock.patch.object(jira.Client, "from_env", side_effect=forbidden),
              mock.patch("urllib.request.urlopen", side_effect=forbidden),
              mock.patch.object(gh, "gh", side_effect=forbidden),
              mock.patch.object(gh, "gh_json", side_effect=lookup),
              mock.patch.object(gh, "issue", return_value={"labels": []}),
              mock.patch.object(gh, "issue_comment", side_effect=comment),
              mock.patch.object(gh, "issue_create", side_effect=issue),
              mock.patch.object(gh, "pr_create", side_effect=pull),
              mock.patch.object(util, "git", side_effect=push)):
                patches.enter_context(patch)
            read = patches.enter_context(mock.patch.object(source, "ticket",
                                                          return_value=triage.TicketText("", "Ticket", "", "old")))
            post = self.publication_post
            cases = [
                ("triage", "/api/triage/fixture%237/send", "/api/triage/{ref}/send", {"ref": "fixture%237"},
                 {"questions": ["What happened?"], "answers": ["It broke."], "repo": self.repo.name},
                 "comments", comment_url, "triage:fixture#7"),
                ("chat", f"/api/chats/{chat_id}/file", "/api/chats/{n}/file", {"n": str(chat_id)},
                 {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}, "issues",
                 gh.issue_url(self.repo.github, 20), f"chat:{chat_id}"),
                ("approval", f"/api/approvals/{approval_id}", "/api/approvals/{id}", {"id": str(approval_id)},
                 {"approve": True}, "issues", gh.issue_url(self.repo.github, 21), f"approval:{approval_id}"),
                ("approval-pr", f"/api/approvals/{pr_id}", "/api/approvals/{id}", {"id": str(pr_id)},
                 {"approve": True}, "pulls", pr_url, f"approval:{pr_id}"),
            ]
            for name, path, route, params, body, kind, url, subject in cases:
                with self.subTest(publication=name):
                    key = f"fixture-key-{name}"
                    before = len(remote[kind])
                    with (mock.patch.object(idempotency.Publication, "record", side_effect=RuntimeError("cut off")),
                          mock.patch.object(approvals_store, "settle_approval", side_effect=settle_until_disconnect)):
                        self.assertEqual(post(path, body, key)[0], 500)
                    self.assertEqual(len(remote[kind]), before + 1)
                    sha = idempotency.fingerprint("POST", route, params, body)
                    tag = db.schema_metadata()["instance_tag"]
                    self.assertIn(f"arete:{tag}:k-{sha[:8]}", remote[kind][-1]["body"].splitlines())
                    self.assertEqual(claims.lookup(self.actor, key)["status"], 0)
                    if name.startswith("approval"):
                        self.assertEqual(approvals_store.approval(int(params["id"]))["status"], "approved")
                    status, headers, answer = post(path, body, key)
                    self.assertEqual(status, 200, answer)
                    saved = claims.lookup(self.actor, key)
                    self.assertEqual(headers["Arete-Seq"], claims.receipt(saved["last_seq"])["event_seq"])
                    with db.reader() as conn:
                        event = conn.execute("SELECT subject, data FROM events_log WHERE seq=?",
                                             (saved["last_seq"],)).fetchone()
                    self.assertEqual((event["subject"], json.loads(event["data"])["url"]), (subject, url))
                    status, headers, again = post(path, body, key)
                    self.assertEqual((status, headers["Idempotent-Replayed"], again), (200, "true", answer))
                    self.assertEqual(len(remote[kind]), before + 1)

            self.assertEqual(post(f"/api/approvals/{approval_id}", {"approve": False}, "fixture-key-approval")[0], 422)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(labeled, [ticket.ref, ticket.ref])
            self.assertEqual(pushed, [["push", "-u", "origin", "fixture/head"]])
            self.assertEqual(searches, ["comments", "comments", "issues", "issues", "issues", "issues", "pulls", "pulls"])
            self.assertIsNone(triage.board_row(ticket.ref))
            self.assertEqual(triage_store.triage_approval(ticket.ref)["repo"], self.repo.name)
            self.assertEqual([m["text"] for m in chats_store.chat_messages(chat_id)],
                             ["Drafted", f"Filed {gh.issue_url(self.repo.github, 20)}"])
            self.assertEqual(approvals_store.approval(approval_id)["result"], gh.issue_url(self.repo.github, 21))
            self.assertEqual(approvals_store.approval(pr_id)["result"], pr_url)
            with self.subTest(publication="triage drift before first confirmed publication"):
                remote["comments"].clear()
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                                 questions=["What happened?"]))
                path = "/api/triage/fixture%237/send"
                payload = {"questions": ["What happened?"], "answers": ["A different answer."], "repo": self.repo.name}
                key = "fixture-triage-stale"
                with mock.patch.object(gh, "issue_comment", side_effect=gh.GhError("could not reach fixture")):
                    self.assertEqual(post(path, payload, key)[0], 502)
                self.assertEqual(claims.lookup(self.actor, key)["status"], 0)
                read.return_value = triage.TicketText("", "Edited", "", "new")
                labels_before = labeled.copy()
                self.assertEqual(post(path, payload, key)[0], 409)
                self.assertEqual(remote["comments"], [])
                self.assertEqual(labeled, labels_before)
                self.assertIsNotNone(triage.board_row(ticket.ref))
            with self.subTest(publication="label-only triage send recovers after its own timestamp change"):
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=1, code=1, repo=self.repo.name))
                path, key = "/api/triage/fixture%237/send", "fixture-triage-label-recovery"
                payload = {"questions": [], "answers": [], "repo": self.repo.name}
                read.return_value = triage.TicketText("", "Ticket", "", "old")
                with mock.patch.object(idempotency.Publication, "record", side_effect=RuntimeError("cut off")):
                    self.assertEqual(post(path, payload, key)[0], 500)
                wanted = triage.labels_to_add(triage.board_row(ticket.ref))
                read.return_value = triage.TicketText("", "Ticket", "", "after-labels", tuple(wanted))
                labels_before = labeled.copy()
                read.return_value.labels += ("agent:hold",)
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 409, answer)
                self.assertIsNotNone(triage.board_row(ticket.ref))
                read.return_value.labels = tuple(wanted)
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 200, answer)
                self.assertEqual(labeled, labels_before)
                self.assertIsNone(triage.board_row(ticket.ref))
            with self.subTest(publication="found comment cannot resume on a newer triage entry"):
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                                 questions=["What happened?"]))
                read.return_value = triage.TicketText("", "Ticket", "", "old")
                payload = {"questions": ["What happened?"], "answers": ["Preserve the new row."], "repo": self.repo.name}
                path, key = "/api/triage/fixture%237/send", "fixture-triage-revised-board"
                with mock.patch.object(source, "approve", side_effect=gh.GhError("cannot add labels")):
                    self.assertEqual(post(path, payload, key)[0], 502)
                triage_store.triage_put(triage.row(source, ticket, updated="new", ready=0, code=1, repo=self.repo.name,
                                                 questions=["New question"]))
                labels_before = labeled.copy()
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 409, answer)
                self.assertEqual(labeled, labels_before)
                self.assertEqual(triage.board_row(ticket.ref)["questions"], ["New question"])
            with self.subTest(publication="upstream tracker failure is kept and redacted"):
                ident = approvals_store.add_approval("fixture", "", approval_emit, "File again")
                marker = "synthetic provider diagnostic should stay private"
                with mock.patch.object(gh, "gh_json", side_effect=gh.GhError(marker)):
                    status, _, failure = post(f"/api/approvals/{ident}", {"approve": True}, "fixture-approval-upstream")
                self.assertNotIn(marker.encode(), failure)
                self.assertEqual(status, 502)
                self.assertIs(json.loads(failure)["kept"], True)
                saved = approvals_store.approval(ident)
                self.assertEqual(saved["status"], "failed")
                self.assertNotIn(marker, saved["result"])
            with self.subTest(publication="identical chat content under a new key"):
                chats_store.chat_say(chat_id, "arete", "Drafted again", draft_title="Draft", draft_body="Chat issue body")
                before = len(remote["issues"])
                path = f"/api/chats/{chat_id}/file"
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                status, _, answer = post(path, body, "fixture-distinct-chat-key")
                self.assertEqual(status, 200, answer)
                self.assertEqual(len(remote["issues"]), before + 1)
                self.assertNotEqual(remote["issues"][-1]["body"], remote["issues"][0]["body"])
            with self.subTest(publication="chat reply lost before the marked issue is visible"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Delayed draft", draft_body="Delayed body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-lost-chat-reply"
                before = len(remote["issues"])
                hidden = []

                def lost_reply(*args, **kwargs):
                    issue(*args, **kwargs)
                    hidden.append(remote["issues"].pop())
                    raise gh.GhUnsure("synthetic lost reply")

                with mock.patch.object(gh, "issue_create", side_effect=lost_reply) as outbound:
                    self.assertEqual(post(path, body, key)[0], 502)
                    retry_status, _, retry = post(path, body, key)
                    self.assertIn(retry_status, (409, 502), retry)
                    self.assertEqual(outbound.call_count, 1)
                self.assertEqual(len(hidden), 1)
                self.assertEqual(chat.chat_row(chat_id)["draft_title"], "Delayed draft")
                remote["issues"].extend(hidden)
                self.assertEqual(post(path, body, key)[0], 200)
                self.assertEqual(len(remote["issues"]), before + 1)
            with self.subTest(publication="approval prerequisites fail before the create attempt"):
                retry_pr = {"type": "pr", "item": {**pr_item, "branch": "fixture/retry"}}
                for item, kind, adapter, prerequisite in ((approval_emit, "issues", gh, "ensure_label"),
                                                          (retry_pr, "pulls", util, "git")):
                    item = {**item, "item": {**item["item"], "labels": ["fixture-label"]}}
                    ident = approvals_store.add_approval("fixture", "", item, "Recover the prerequisite failure")
                    path, key = f"/api/approvals/{ident}", f"fixture-prerequisite-{ident}"
                    before = len(remote[kind])
                    with mock.patch.object(gh, "ensure_label", return_value=""), \
                            mock.patch.object(adapter, prerequisite, side_effect=gh.GhError("prerequisite failed")):
                        status, _, failure = post(path, {"approve": True}, key)
                        self.assertEqual(status, 502, failure)
                    self.assertEqual(approvals_store.approval(ident)["status"], "failed")
                    self.assertEqual(len(remote[kind]), before)
                    with mock.patch.object(gh, "ensure_label", return_value=""):
                        status, _, answer = post(path, {"approve": True}, key)
                    self.assertEqual(status, 200, answer)
                    self.assertEqual(len(remote[kind]), before + 1)
                    self.assertEqual(approvals_store.approval(ident)["status"], "done")
            with self.subTest(publication="lost approval reply cannot authorize another send under a fresh key"):
                ident = approvals_store.add_approval("fixture", "", approval_emit, "Send once")
                path, key = f"/api/approvals/{ident}", "fixture-approval-lost-reply"
                hidden = []

                def lost_issue(*args, **kwargs):
                    issue(*args, **kwargs)
                    hidden.append(remote["issues"].pop())
                    raise gh.GhUnsure("reply not received")

                with mock.patch.object(gh, "issue_create", side_effect=lost_issue):
                    self.assertEqual(post(path, {"approve": True}, key)[0], 502)
                self.assertEqual(approvals_store.approval(ident)["status"], "failed")
                before = len(remote["issues"])
                self.assertEqual(post(path, {"approve": True}, "fixture-fresh-approval-key")[0], 409)
                self.assertEqual(self.publication_post(path, {"approve": True}, None)[0], 409)
                self.assertEqual(len(remote["issues"]), before)
                remote["issues"].extend(hidden)
                self.assertEqual(post(path, {"approve": True}, key)[0], 200)
                self.assertEqual(len(remote["issues"]), before + 1)
            with self.subTest(publication="discarded chat draft cannot be sent after an initial lookup failure"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Discarded", draft_body="Old body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-chat-discarded"
                with mock.patch.object(gh, "find_issue_marker", side_effect=gh.GhError("lookup not available")):
                    self.assertEqual(post(path, body, key)[0], 502)
                chat.drop_draft(chat_id)
                before = len(remote["issues"])
                self.assertEqual(post(path, body, key)[0], 409)
                self.assertEqual(len(remote["issues"]), before)
            with self.subTest(publication="chat discard wins while prior publication is being looked up"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Will discard", draft_body="Pending body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-chat-discard-at-lookup"
                before = len(remote["issues"])

                def discard(*unused):
                    chat.drop_draft(chat_id)
                    return ""

                with mock.patch.object(gh, "find_issue_marker", side_effect=discard):
                    self.assertEqual(post(path, body, key)[0], 409)
                saved = claims.lookup(self.actor, key)
                self.assertFalse(json.loads(saved["response"]).get("send_started"))
                self.assertEqual(len(remote["issues"]), before)
            with self.subTest(publication="chat without a bound draft"):
                chats_store.chat_say(chat_id, "arete", "Drafted again", draft_title="New draft", draft_body="Different body")
                path = f"/api/chats/{chat_id}/file"
                before = len(remote["issues"])
                status, _, _ = post(path, {}, "fixture-chat-without-draft")
                self.assertIn(status, (409, 422))
                self.assertEqual(len(remote["issues"]), before)
                self.assertEqual(chat.chat_row(chat_id)["draft_title"], "New draft")
                self.assertIsNone(claims.lookup(self.actor, "fixture-chat-without-draft"))
            with self.subTest(publication="legacy unkeyed filing without representation headers"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Legacy draft", draft_body="Legacy body")
                before = len(remote["issues"])
                status, _, answer = self.publication_post(f"/api/chats/{chat_id}/file", None, None)
                self.assertEqual(status, 200, answer)
                self.assertEqual(len(remote["issues"]), before + 1)
                self.assertEqual(remote["issues"][-1]["body"], "Legacy body")

    def test_keyed_tracker_adapters_send_at_most_once(self):
        marker = "arete:fixture:k-12345678.234567890abcdef1234567890abcdef1"
        with mock.patch.object(gh, "gh", side_effect=gh.GhUnsure("lost reply")) as send, \
                mock.patch.object(gh, "recovery_lookup", side_effect=AssertionError("internal retry")):
            writes = (
                lambda: gh.issue_create(self.repo.github, "Fixture", marker, single_attempt=True),
                lambda: gh.issue_comment(self.repo.github, 7, marker, return_url=True, single_attempt=True),
                lambda: gh.pr_create(self.repo.github, "fixture", "main", "Fixture", marker, single_attempt=True),
            )
            for write in writes:
                send.reset_mock()
                with self.assertRaises(gh.GhUnsure):
                    write()
                send.assert_called_once()
                self.assertIn(marker, send.call_args.kwargs["input"].splitlines())

        client = jira.Client("https://jira.example.test", "fixture@example.test", "not-a-credential")
        with mock.patch.object(jira._opener, "open", side_effect=TimeoutError("lost reply")) as send:
            with self.assertRaises(jira.JiraUnsure):
                client.comment("TEST-7", f"Approved answers\n\n{marker}", return_url=True, single_attempt=True)
            send.assert_called_once()
            request = send.call_args.args[0]
            self.assertEqual((request.method, request.selector), ("POST", "/rest/api/3/issue/TEST-7/comment"))
            self.assertIn(marker, json.dumps(json.loads(request.data)["body"]))
        pages = [BytesIO(b'{"total":0,"comments":[]}'), BytesIO(json.dumps({"total": 1, "comments": [
            {"id": "42", "body": f"Approved answers\n\n{marker}"}]}).encode())]
        with mock.patch.object(jira._opener, "open", side_effect=pages) as observe:
            self.assertEqual(client.find_comment_marker("TEST-7", marker), "")
            self.assertEqual(client.find_comment_marker("TEST-7", marker),
                             "https://jira.example.test/browse/TEST-7?focusedCommentId=42")
            self.assertEqual([call.args[0].method for call in observe.call_args_list], ["GET", "GET"])

    def publication_post(self, path, body, key):
        payload = json.dumps(body).encode() if body is not None else b""
        headers = f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Arete: 1\r\n"
        if payload:
            headers += f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        if key:
            headers += f"Idempotency-Key: {key}\r\n"
        incoming = BytesIO((headers + "\r\n").encode() + payload)
        outgoing = bytearray()
        connection = SimpleNamespace(makefile=lambda *args: incoming, sendall=outgoing.extend)
        server = SimpleNamespace(node=SimpleNamespace(names={"127.0.0.1"}),
                                 logins=SimpleNamespace(of=lambda _: "fixture@example.test"))
        dashboard.Handler(connection, ("127.0.0.1", 0), server)
        response = HTTPResponse(SimpleNamespace(makefile=lambda *args: BytesIO(outgoing)))
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def check_read_surface(self, port):
        def request(path, *, body=None, headers=None, status=200):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                request_headers = {"X-Arete": "1", "Accept": "application/json"}
                if body is not None:
                    request_headers["Content-Type"] = "application/json"
                connection.request("GET" if body is None else "POST", path,
                                   body=None if body is None else json.dumps(body),
                                   headers={**request_headers, **(headers or {})})
                response = connection.getresponse()
                data = json.loads(response.read())
                self.assertEqual(response.status, status, (path, data))
                self.assertEqual(response.getheader("Arete-API"), contract.VERSION)
                if body is None and status == 200 and "seq" in data:
                    self.assertEqual(data["seq"], response.getheader("Arete-Seq"))
                return data, response
            finally:
                connection.close()

        registry, _ = request("/api/v1/contract")
        operations = registry["operations"]
        self.assertEqual(len(operations), 33)
        self.assertEqual(len({op["name"] for op in operations}), 33)
        for op in operations:
            with self.subTest(op=op["name"]):
                contract.validate(op["example"]["input"], op["input"])
                contract.validate(op["example"]["result"], op["result"])
        for problem in registry["errors"]:
            contract.validate(problem["example"], problem["schema"])
        meta, alias = request("/api/meta")
        self.assertEqual(meta, request("/api/v1/meta")[0])
        self.assertEqual(meta["api"]["contract_sha"], registry["contract_sha"])
        self.assertEqual(alias.getheader("Deprecation"), "true")
        with db.tx():
            events.emit("fixture.changed", "fixture:573", cls="derived", data={})
        stream, response = request("/api/events?since=0&timeout=0")
        self.assertEqual(response.getheader("Deprecation"), "true")
        self.assertEqual(response.getheader("Arete-Seq"), stream["next"])
        self.assertEqual(stream["events"][-1]["id"], stream["next"])
        request("/api/v1/missing", status=404)
        request("/api/sidebar", headers={"X-Arete-Client": "mcp/0.9"}, status=400)
        with mock.patch.object(tasks, "start", side_effect=AssertionError("unknown field reached task start")), \
                mock.patch.object(dashboard.api_http, "record_alias", wraps=dashboard.api_http.record_alias) as record:
            refused, _ = request("/api/tasks", body={"colour": "not an input"}, status=422)
            record.assert_not_called()
        self.assertEqual(refused["fields"], ["colour"])
        with mock.patch.object(db, "tx", side_effect=AssertionError("GET opened a write transaction")), \
                mock.patch.object(chat, "recover", side_effect=AssertionError("GET recovered chat files")):
            sidebar, _ = request("/api/sidebar")
            live, _ = request("/api/live")
            request("/api/chats")
            self.assertEqual(sidebar["seq"], live["seq"])
            self.assertEqual(sidebar["running"], len(live["live"]["rows"]["runs"]))
            batch, response = request("/api/v1/batch", body={"requests": [
                {"operation": "screen.get"}, {"operation": "task.list"}, {"operation": "meta.get"}]})
            contract.validate(batch, contract.BY_NAME["batch.run"]["result"])
            self.assertEqual({row["seq"] for row in batch["results"]}, {response.getheader("Arete-Seq")})
        def grace():
            with db.reader() as c:
                return c.execute("SELECT value FROM schema_meta WHERE key = 'versioned_api_grace_deadline'").fetchone()[0]

        def page():
            with self.subTest("first versioned screen"):
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", "/ui.html")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"<!doctype html>", response.read().lower())
                finally:
                    connection.close()

        self.assertEqual(grace(), "")
        page()
        deadline = grace()
        self.assertTrue(deadline)
        with mock.patch.object(db, "tx", side_effect=AssertionError("repeat page opened a write transaction")):
            page()
        self.assertEqual(grace(), deadline)

    def check_transport(self, port):
        def exchange(method, path, headers=(), body=b""):
            raw = [f"{method} {path} HTTP/1.1\r\n", "Host: 127.0.0.1\r\n", "X-Arete: 1\r\n"]
            with create_connection(("127.0.0.1", port), timeout=3) as conn:
                conn.sendall(("".join(raw) + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode() + body)
                conn.shutdown(SHUT_WR)
                response = HTTPResponse(conn)
                response.begin()
                return response.status, response.headers, response.read()

        meta_route, batch_route = "/api/v1/meta", "/api/v1/batch"
        media = ("Content-Type", "application/json")

        def post(path, body, media_header=media):
            return exchange("POST", path, (media_header, ("Content-Length", len(body))), body)

        def problem(response, status, code):
            actual, headers, body = response
            self.assertEqual(actual, status, body)
            self.assertEqual(headers["Content-Type"], "application/problem+json")
            self.assertEqual(headers["Arete-API"], contract.VERSION)
            self.assertEqual(json.loads(body)["code"], code)

        now = [0.0]
        with mock.patch.object(api_http, "RATES", api_http.RateLimits(lambda: now[0])), \
                mock.patch.object(tasks, "start", side_effect=AssertionError("invalid request started a task")):
            problem(exchange("GET", meta_route + "?x=" + "a" * 65536), 414, "uri_too_long")
            for method, route, allow in (("GET", batch_route, "POST"), ("PUT", meta_route, "GET")):
                refused = exchange(method, route)
                problem(refused, 405, "method_not_allowed")
                self.assertEqual(refused[1]["Allow"], allow)
            for headers, body in (((), b""), ((media, ("Content-Length", "bad")), b""),
                                  ((media, ("Content-Length", "0"), ("Content-Length", "0")), b""),
                                  ((media, ("Transfer-Encoding", "chunked")), b""),
                                  ((media, ("Content-Length", "4")), b"{}")):
                with self.subTest(headers=headers):
                    problem(exchange("POST", batch_route, headers, body), 400, "invalid_framing")
            for raw in (b'{"requests":[],"requests":[]}', b'{"x":NaN}', b'{"x":Infinity}',
                        b'{"x":9007199254740992}', b'{"x":' + b"[" * 32 + b"0" + b"]" * 32 + b"}"):
                with self.subTest(raw=raw):
                    problem(post(batch_route, raw), 400, "invalid_json")
            problem(post(batch_route, b"{}", ("Content-Type", "text/plain")), 415, "unsupported_media_type")
            status, _, body = post("/api/tasks", b"{}", ("Content-Type", "text/plain"))
            self.assertEqual((status, json.loads(body)["code"]), (415, "unsupported_media_type"))
            problem(exchange("POST", batch_route, (media, ("Content-Length", api_http.MAX_JSON_BYTES + 1))),
                    413, "body_too_large")
            self.assertEqual(exchange("GET", meta_route, [("Fixture", "x")] * 98)[0], 200)
            problem(exchange("GET", meta_route, [("Fixture", "x")] * 99), 431, "headers_too_large")

            _, headers, _ = exchange("GET", meta_route)
            match = [("If-None-Match", headers["ETag"])]
            status, empty, body = exchange("GET", meta_route, match)
            self.assertEqual((status, body), (304, b""))
            for name in ("Arete-API", "Arete-Seq", "ETag"):
                self.assertEqual(empty[name], headers[name])
            self.assertIsNone(empty["Content-Length"])
            with db.tx() as conn:
                conn.execute("UPDATE schema_meta SET value = cast(value AS INTEGER) + 1 WHERE key = 'restore_epoch'")
            status, newer, _ = exchange("GET", meta_route, match)
            self.assertEqual(status, 200)
            self.assertNotEqual(headers["ETag"], newer["ETag"])
            self.assertNotEqual(headers["Arete-Seq"], newer["Arete-Seq"])

            dashboard.POSTS_IN_FLIGHT.close()
            try:
                for response in (exchange("GET", meta_route), post(batch_route, b"{}")):
                    problem(response, 503, "draining")
                    self.assertEqual(response[1]["Retry-After"], "2")
            finally:
                dashboard.POSTS_IN_FLIGHT.reopen()
            self.assertEqual(exchange("GET", meta_route)[0], 200)

            logged = StringIO()
            raw = b'{"colour":"private-body"}'
            with redirect_stderr(logged):
                exchange("POST", "/api/tasks?fixture=private-query",
                         (media, ("Content-Length", len(raw)), ("Idempotency-Key", "private-key")), raw)
            event = json.loads(logged.getvalue().splitlines()[-1])
            self.assertEqual((event["event"], event["route"], event["status"]), ("api.request", "/api/tasks", 422))
            self.assertNotIn("private-", logged.getvalue())

            with mock.patch.object(dashboard.webhooks, "receive", side_effect=AssertionError("GET webhook dispatched")):
                self.assertEqual(exchange("GET", dashboard.webhooks.HOOK_PATH + "fixture")[0], 405)

            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8WsAAAAASUVORK5CYII=")
            upload = "/api/capture/evidence?name=fixture.png"
            status, _, body = exchange("POST", upload, [("Content-Length", len(png))], png)
            self.assertEqual(status, 200, body)
            mime_status, _, _ = post(upload, png, ("Content-Type", "image/png"))
            self.assertEqual(mime_status, 200)
            evidence = json.loads(body)["evidence"]["id"]
            status, partial, body = exchange("GET", f"/api/capture/evidence/{evidence}", [("Range", "bytes=0-7")])
            self.assertEqual((status, body, partial["Content-Type"]), (206, png[:8], "image/png"))
            stale = headers["Arete-Seq"]
            status, stream_headers, body = exchange("GET", f"/api/v1/events?since={stale}",
                                                    [("Accept", "application/x-ndjson")])
            self.assertEqual((status, stream_headers["Content-Type"], json.loads(body)["kind"]),
                             (200, "application/x-ndjson", "reset"))

            now[0] += 10
            batch = json.dumps({"requests": [{"operation": "meta.get"}] * 50}).encode()
            self.assertEqual(post(batch_route, batch)[0], 200)
            for route in (meta_route, "/api/meta"):
                limit = exchange("GET", route)
                self.assertEqual((limit[0], limit[1]["Retry-After"], json.loads(limit[2])["code"]), (429, "1", "rate_limited"))
            now[0] += .021
            self.assertEqual(exchange("GET", meta_route)[0], 200)
            now[0] += .021
            problem(post(batch_route, b"{"), 400, "invalid_json")
            problem(post(batch_route, b"{"), 429, "rate_limited")

        now = [0.0]
        limits = api_http.RateLimits(lambda: now[0])
        limits.take("one", command=True, cost=60)
        with self.assertRaises(api_http.RateLimited):
            limits.take("one", command=True)
        limits.take("two", command=True)
        now[0] += .051
        limits.take("one", command=True)

    def check_publication_drain(self, port):
        def request(path, body=None, key=""):
            conn = HTTPConnection("127.0.0.1", port, timeout=3)
            headers = {"X-Arete": "1", "Accept": "application/json"}
            if body is not None:
                headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
            try:
                conn.request("GET" if body is None else "POST", path, headers=headers,
                             body=None if body is None else json.dumps(body))
                response = conn.getresponse()
                return response.status, response.headers, json.loads(response.read())
            finally:
                conn.close()

        self.cfg.arete_repo = self.repo.name
        chat_id = chats_store.chat_add(self.repo.name)
        chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Drain fixture", draft_body="Test the admitted write")
        body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
        path, key = f"/api/chats/{chat_id}/file", "drain-fixture-key"
        entered, release = threading.Event(), threading.Event()
        responses = []

        def publish(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("the fixture did not release the admitted request")
            return 42

        sender = threading.Thread(target=lambda: responses.append(request(path, body, key)))
        replacement = frozenset({("fixture.py", 2)})
        old = frozenset({("fixture.py", 1)})
        coordinator = object.__new__(serve.Server)
        coordinator.code, coordinator.code_seen = old, replacement
        coordinator.migration_stamp, coordinator.migration_error = replacement, ""
        coordinator.polls, coordinator.oncall = {}, None
        coordinator.job_running = mock.Mock(return_value=False)
        coordinator.sweeping = mock.Mock(return_value=False)
        coordinator.note_once = mock.Mock()

        def busy():
            prior = serve.Server.busy_with(coordinator)
            if not dashboard.POSTS_IN_FLIGHT.draining():
                self.assertEqual(prior, "")
                sender.start()
                self.assertTrue(entered.wait(3))
            else:
                self.assertEqual(prior, "the dashboard is still answering a request")
                refused = request(path, body, "new-request-during-drain")
                self.assertEqual((refused[0], refused[1]["Retry-After"], refused[2]["code"]), (503, "2", "draining"))
            return prior

        coordinator.busy_with = busy
        forbidden = AssertionError("unexpected external transport or restart")
        with (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")),
              mock.patch.object(serve, "code_stamp", return_value=replacement),
              mock.patch.object(util, "sh", return_value=SimpleNamespace(returncode=0)),
              mock.patch.object(serve.os, "execv", side_effect=forbidden),
              mock.patch.object(gh, "gh", side_effect=forbidden),
              mock.patch.object(gh, "find_issue_marker", return_value=""),
              mock.patch.object(gh, "issue_create", side_effect=publish) as sends,
              mock.patch.object(accounts, "read", side_effect=forbidden),
              mock.patch.object(chat.routing, "metered", return_value=False)):
            try:
                coordinator.load_new_code()
                coordinator.note_once.assert_called_once_with("new code waits to load: the dashboard is still answering a request")
            finally:
                release.set()
                if sender.ident is not None:
                    sender.join(4)
            self.assertEqual([response[0] for response in responses], [200])
            replay = request(path, body, key)
            self.assertEqual((replay[0], replay[1]["Idempotent-Replayed"]), (200, "true"))
            self.assertEqual(replay[2], responses[0][2])
            sends.assert_called_once()
            self.assertEqual(request("/api/v1/meta")[0], 200)

    def test_keyed_recovery(self):
        with db.reader() as c:
            before = c.execute("SELECT count(*) FROM tasks").fetchone()[0]
            events = c.execute("SELECT count(*) FROM events_log").fetchone()[0]
        with mock.patch.object(claims, "claim", side_effect=RuntimeError("claim failed")):
            answer = self.request({"repo": "fixture", "prompt": "Must roll back"})
        self.assertEqual(answer[0], 500)
        self.assertEqual(json.loads(answer[1])["error"], "The server could not complete the request.")
        with db.reader() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], before)
            self.assertEqual(c.execute("SELECT count(*) FROM events_log").fetchone()[0], events)
        self.assertEqual(claims.lookup(self.actor, self.id())["status"], 500)
        self.assertEqual(self.request({"repo": "fixture", "prompt": "Must roll back"}), answer)


class PrincipalTests(unittest.TestCase):
    def test_principal_gate(self):
        owner = principals.seed_owner("fixture@example.test")
        handler = SimpleNamespace(server=SimpleNamespace(node=SimpleNamespace(names={"fixture.test"}),
                                  logins=SimpleNamespace(of=mock.Mock(return_value="fixture@example.test"))),
                                  client_address=("127.0.0.1", 1), headers={"Host": "fixture.test", "X-Arete": "1"})
        with principals.acting("hand"):
            dashboard.Handler.admit(handler, "POST", "/api/tasks")
            self.assertEqual(principals.current(), owner)
            for host, login, header, status in (("foreign.test", "fixture@example.test", "1", 421),
                                               ("fixture.test", "stranger@example.test", "1", 403),
                                               ("fixture.test", "fixture@example.test", "", 403)):
                handler.headers = {"Host": host, "X-Arete": header}
                handler.server.logins.of.return_value = login
                with self.subTest(status=status), self.assertRaises(dashboard.Refusal) as got:
                    dashboard.Handler.admit(handler, "POST", "/api/tasks")
                self.assertEqual(got.exception.status, status)
        with self.assertRaises(sqlite3.IntegrityError), db.tx() as c:
            c.execute("INSERT INTO principals (id, kind, role, status, name, created) "
                      "VALUES ('u:other', 'human', 'owner', 'active', 'Other', ?)", (util.iso(),))
