import json
import sqlite3
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import chat, config, contract, control, dashboard, events, idempotency, pipeline, principals, tailnet, tasks, util
from arete.store import db
from arete.store import idempotency as claims
from tests import support


def setUpModule():
    support.isolate_live_state()


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
        node = SimpleNamespace(names={"127.0.0.1"})
        logins = SimpleNamespace(of=lambda _: "fixture@example.test")
        with tailnet.Server("127.0.0.1", 0, node, logins, False, dashboard.Handler, dashboard.Redirect) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                self.check_read_surface(server.server_port)
            finally:
                server.shutdown()
                thread.join()

    def check_read_surface(self, port):
        def request(path, *, body=None, headers=None, status=200):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET" if body is None else "POST", path,
                                   body=None if body is None else json.dumps(body),
                                   headers={"X-Arete": "1", "Accept": "application/json", **(headers or {})})
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
        with mock.patch.object(tasks, "start", side_effect=AssertionError("unknown field reached task start")):
            refused, _ = request("/api/tasks", body={"colour": "not an input"}, status=422)
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

    def test_keyed_recovery(self):
        with db.reader() as c:
            before = c.execute("SELECT count(*) FROM tasks").fetchone()[0]
            events = c.execute("SELECT count(*) FROM events_log").fetchone()[0]
        with mock.patch.object(claims, "claim", side_effect=RuntimeError("claim failed")):
            answer = self.request({"repo": "fixture", "prompt": "Must roll back"})
        self.assertEqual(answer[0], 500)
        self.assertIn("claim failed", json.loads(answer[1])["error"])
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
