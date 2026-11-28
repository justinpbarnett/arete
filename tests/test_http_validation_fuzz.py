from __future__ import annotations

import hashlib
import hmac
import io
import json
import random
import string
import unittest
from collections import deque
from email.message import Message
from http.client import HTTPMessage
from types import SimpleNamespace
from unittest import mock

from arete import (
    api,
    api_http,
    contract,
    credential_http,
    dashboard,
    event_stream,
    inbox,
    tailnet,
    webhooks,
    webpush,
)
from arete.refusals import Stated
from arete.store import events as event_store
from arete.store import inbox as inbox_store
from arete.store import tokens


def _headers(*pairs: tuple[str, str]) -> HTTPMessage:
    msg = HTTPMessage()
    for key, value in pairs:
        msg.add_header(key, value)
    return msg


class HttpFramingAndJsonFuzzTests(unittest.TestCase):
    def test_read_headers_rejects_malformed_and_oversized(self):
        with self.assertRaises(contract.Problem) as ctx:
            api_http.read_headers(io.BytesIO(b"BadHeaderNoColon\r\n\r\n"))
        self.assertEqual(ctx.exception.code, "invalid_framing")

        with self.assertRaises(contract.Problem) as ctx:
            api_http.read_headers(io.BytesIO(b"X-Test: val\n\r\n"))
        self.assertEqual(ctx.exception.code, "invalid_framing")

        too_many = b"".join(f"X-H{i}: v\r\n".encode() for i in range(api_http.MAX_HEADERS + 1)) + b"\r\n"
        with self.assertRaises(contract.Problem) as ctx:
            api_http.read_headers(io.BytesIO(too_many))
        self.assertEqual(ctx.exception.code, "headers_too_large")

        too_long = b"X-Long: " + (b"a" * 65536) + b"\r\n\r\n"
        with self.assertRaises(contract.Problem) as ctx:
            api_http.read_headers(io.BytesIO(too_long))
        self.assertEqual(ctx.exception.code, "headers_too_large")

        rng = random.Random(20261128)
        alphabet = b"abcXYZ0123:-_ \t\r\n\x00\x0b\x7f\xff"
        for _ in range(80):
            raw = bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 64)))
            try:
                parsed = api_http.read_headers(io.BytesIO(raw))
            except contract.Problem as err:
                self.assertIn(err.code, ("invalid_framing", "headers_too_large"))
            else:
                self.assertIsInstance(parsed, HTTPMessage)

    def test_content_length_and_read_body_boundaries(self):
        self.assertEqual(api_http.content_length(_headers(), required=False), 0)
        with self.assertRaises(contract.Problem) as ctx:
            api_http.content_length(_headers(), required=True)
        self.assertEqual(ctx.exception.code, "invalid_framing")

        bad_header_sets = [
            _headers(("Content-Length", "5"), ("Content-Length", "5")),
            _headers(("Content-Length", "5"), ("Transfer-Encoding", "chunked")),
            _headers(("Content-Length", "-1")),
            _headers(("Content-Length", "+5")),
            _headers(("Content-Length", "5.0")),
            _headers(("Content-Length", "²")),
            _headers(("Content-Length", "١٢")),
            _headers(("Content-Length", "12abc")),
        ]
        for hdrs in bad_header_sets:
            with self.assertRaises(contract.Problem) as ctx:
                api_http.content_length(hdrs, required=False)
            self.assertEqual(ctx.exception.code, "invalid_framing")

        huge = api_http.content_length(_headers(("Content-Length", "9" * 40)), required=True)
        self.assertEqual(huge, 2**63)

        with self.assertRaises(contract.Problem) as ctx:
            api_http.read_body(io.BytesIO(b"hello"), 1025, 1024)
        self.assertEqual(ctx.exception.code, "body_too_large")

        with self.assertRaises(contract.Problem) as ctx:
            api_http.read_body(io.BytesIO(b"abc"), 5, 1024)
        self.assertEqual(ctx.exception.code, "invalid_framing")

        self.assertEqual(api_http.read_body(io.BytesIO(b"abc"), 3, 1024), b"abc")

    def test_json_object_fuzz_and_boundaries(self):
        self.assertEqual(api_http.json_object(b""), {})
        bad_payloads = [
            b"\xff\xfe",
            b"[]",
            b"123",
            b'"text"',
            b"null",
            b"true",
            b'{"a": 1, "a": 2}',
            b'{"n": 9007199254740992}',
            b'{"n": -9007199254740992}',
            b'{"f": NaN}',
            b'{"f": Infinity}',
            b'{"f": 1e500}',
            b'{"s": "\\uD800"}',
            (b'{"a":' * 33) + b"1" + (b"}" * 33),
            b"   ",
        ]
        for raw in bad_payloads:
            with self.assertRaises(contract.Problem) as ctx:
                api_http.json_object(raw)
            self.assertEqual(ctx.exception.code, "invalid_json")

        rng = random.Random(9001)
        tokens_pool = [b"{", b"}", b"[", b"]", b'"a"', b":", b",", b"1", b"null", b"NaN", b"\\", b"\xff", b"0" * 20]
        for _ in range(120):
            sample = b"".join(rng.choices(tokens_pool, k=rng.randint(1, 25)))
            try:
                parsed = api_http.json_object(sample)
            except contract.Problem as err:
                self.assertEqual(err.code, "invalid_json")
            else:
                self.assertIsInstance(parsed, dict)

    def test_problem_fallback_and_dashboard_number_and_run_id_validation(self):
        p416 = api_http.problem(416, {"error": "Range not satisfiable."})
        self.assertEqual(p416["code"], "invalid_input")
        self.assertEqual(p416["status"], 416)
        p504 = api_http.problem(504, {"error": "Timed out."})
        self.assertEqual(p504["code"], "internal_error")
        self.assertEqual(p504["status"], 504)

        self.assertEqual(dashboard.whole_number("42"), 42)
        for bad in ("-1", "1.5", "", "abc", "١٢٣", "²", "9" * 19):
            with self.assertRaises(Stated) as ctx:
                dashboard.whole_number(bad)
            self.assertEqual(ctx.exception.status, 400)

        self.assertIsNotNone(dashboard.RUN_ID.fullmatch("run_2026-11-28.1"))
        self.assertIsNone(dashboard.RUN_ID.fullmatch("a" * 201))
        self.assertIsNone(dashboard.RUN_ID.fullmatch("../etc"))
        self.assertIsNone(dashboard.RUN_ID.fullmatch("run_\u03b1"))

        self.assertIsNone(dashboard.EVIDENCE_ITEM.fullmatch("/api/capture/evidence/١٢"))
        self.assertIsNone(dashboard.EVIDENCE_DROP.fullmatch("/api/capture/evidence/١٢/drop"))
        self.assertIsNone(dashboard.EVIDENCE_RANGE.fullmatch("bytes=0-" + "9" * 19))
        self.assertIsNotNone(dashboard.EVIDENCE_RANGE.fullmatch("bytes=0-100"))


class AuthOriginAndTokenFuzzTests(unittest.TestCase):
    def test_origin_of_and_tailnet_host_name_fuzz(self):
        self.assertEqual(credential_http.origin_of("HTTPS", "node.tailnet.ts.net"), ("https", "node.tailnet.ts.net", 443))
        self.assertEqual(credential_http.origin_of("http", "127.0.0.1:8080"), ("http", "127.0.0.1", 8080))
        self.assertEqual(credential_http.origin_of("https", "[::1]:8443"), ("https", "::1", 8443))
        self.assertEqual(tailnet.host_name("node.tailnet.ts.net:8443"), "node.tailnet.ts.net")
        self.assertEqual(tailnet.host_name("[fd7a:115c:a1e0::1]:8443"), "fd7a:115c:a1e0::1")

        bad_hosts = [
            "", " ", "@127.0.0.1", ":@127.0.0.1", "user:pass@127.0.0.1", "127.0.0.1:0",
            "127.0.0.1:70000", "127.0.0.1:-1", "127.0.0.1/path", "127.0.0.1?", "127.0.0.1#",
            "127.0.0.1\\@evil", "127.0.0.1\r\nX-Inj: 1", "127.0.0.1\x00", "bad host", "[::1",
        ]
        for host in bad_hosts:
            self.assertIsNone(credential_http.origin_of("https", host), host)
            self.assertEqual(tailnet.host_name(host), "", host)

        for scheme in ("javascript", "ftp", "file", "", None):
            self.assertIsNone(credential_http.origin_of(scheme, "127.0.0.1"))
        self.assertIsNone(credential_http.origin_of("https", None))
        self.assertEqual(tailnet.host_name(None), "")

        rng = random.Random(4242)
        chars = string.ascii_letters + string.digits + ".:[]@/?#\\\r\n\t\x00\x7f-"
        for _ in range(150):
            host = "".join(rng.choices(chars, k=rng.randint(0, 30)))
            result = credential_http.origin_of(rng.choice(["http", "https", "ws", ""]), host)
            if result is not None:
                sch, parsed_host, port = result
                self.assertIn(sch, ("http", "https"))
                self.assertTrue(parsed_host)
                self.assertTrue(1 <= port <= 65535)

    def test_check_origin_and_traffic_sweep_empty_deques(self):
        handler = SimpleNamespace(
            origin=("https", "node.ts.net", 443),
            headers=_headers(("Sec-Fetch-Site", "  CROSS-SITE  ")),
        )
        with self.assertRaises(contract.Problem) as ctx:
            credential_http.check_origin(handler)
        self.assertEqual(ctx.exception.code, "bad_origin")

        handler.headers = _headers(("Origin", "https://@node.ts.net"))
        with self.assertRaises(contract.Problem) as ctx:
            credential_http.check_origin(handler)
        self.assertEqual(ctx.exception.code, "bad_origin")

        handler.headers = _headers(("Origin", "https://node.ts.net"), ("Origin", "https://node.ts.net"))
        with self.assertRaises(contract.Problem) as ctx:
            credential_http.check_origin(handler)
        self.assertEqual(ctx.exception.code, "bad_origin")

        handler.headers = _headers(("Origin", "https://node.ts.net"), ("Sec-Fetch-Site", "same-origin"))
        credential_http.check_origin(handler)

        traffic = credential_http.Traffic()
        traffic.failed["peer-a"] = deque()
        traffic.issues["peer-b"] = deque()
        traffic.swept = -1000.0
        traffic.check("peer-c")
        self.assertNotIn("peer-a", traffic.failed)
        self.assertNotIn("peer-b", traffic.issues)

    def test_issue_grants_rejects_malformed_spend_cap_and_expires(self):
        for bad_cap in ("not-a-number", "NaN", "Infinity", "-1", 1e-10, 10**12):
            with self.assertRaises(contract.Problem) as ctx:
                credential_http.issue_grants({"label": "CLI", "scopes": ["read"], "spend_cap_usd": bad_cap})
            self.assertEqual(ctx.exception.code, "invalid_input")

        label, grants = credential_http.issue_grants({"label": "CLI", "scopes": ["read"], "spend_cap_usd": "12.5"})
        self.assertEqual(label, "CLI")
        self.assertEqual(grants["spend_cap_nusd"], 12_500_000_000)

    def test_pairing_start_and_batch_run_enforce_scopes_and_csrf(self):
        readonly_owner = tokens.Credential(
            "abcd1234", "human:owner", "owner", "client:abcd1234", "token",
            frozenset({"read"}), "file:k", "0", "1", "2099-01-01T00:00:00Z",
        )
        pairing_op = contract.BY_NAME["pairing.start"]
        handler = SimpleNamespace(credential=readonly_owner, operation=(pairing_op, {}), alias=None)
        with mock.patch("arete.credential_http.db.reader") as reader_ctx, \
             mock.patch("arete.credential_http.tokens.live_session", return_value=False):
            reader_ctx.return_value.__enter__.return_value = object()
            with self.assertRaises(contract.Problem) as ctx:
                credential_http.authorize(handler, pairing_op, "pairing.start", True, True, "127.0.0.1",
                                          requires_credential=True)
        self.assertEqual(ctx.exception.code, "scope_missing")

        # batch.run must enforce per-operation scopes for each sub-request.
        no_scope_cred = tokens.Credential(
            "abcd1234", "human:owner", "owner", "client:abcd1234", "token",
            frozenset({"task:write"}), "file:k", "0", "1", "2099-01-01T00:00:00Z",
        )
        batch_handler = SimpleNamespace(credential=no_scope_cred)
        with self.assertRaises(contract.Problem) as ctx:
            api.execute("batch.run", {"requests": [{"operation": "meta.get", "input": {}}]}, handler=batch_handler)
        self.assertEqual(ctx.exception.code, "scope_missing")

        # line_stream.read rejects non-ASCII decimals and oversized cursors.
        for bad_after in ("١", "²", "-1", "9" * 19):
            with self.assertRaises(contract.Problem) as ctx:
                api.read("line_stream.read", {"run": "r1", "after": bad_after}, handler=None)
            self.assertEqual(ctx.exception.code, "invalid_input")

        # POST /api/v1/batch with session cookie enforces Origin and X-Arete: 1 CSRF check.
        session_cred = tokens.Credential(
            "sess1234", "human:owner", "owner", "human:owner", "session",
            frozenset({"*"}), "file:k", "0", "1", "2099-01-01T00:00:00Z",
        )
        logins = SimpleNamespace(
            credentials=credential_http.Traffic(),
            of=lambda _peer: "owner@example.com",
            node_key=lambda peer: peer,
        )
        batch_op = contract.BY_NAME["batch.run"]
        csrf_handler = SimpleNamespace(
            operation=(batch_op, {}),
            alias=None,
            origin=("https", "node.ts.net", 443),
            headers=_headers(("Cookie", f"{credential_http.COOKIE}=ar_tag_sess1234_secret")),
            client_address=("127.0.0.1", 12345),
            server=SimpleNamespace(logins=logins),
        )
        with mock.patch("arete.credential_http.tokens.resolve", return_value=session_cred), \
             mock.patch("arete.credential_http.principals.admit"):
            with self.assertRaises(contract.Problem) as ctx:
                credential_http.authenticate(csrf_handler, "POST", "/api/v1/batch")
        self.assertEqual(ctx.exception.code, "csrf")

    def test_token_resolve_and_note_use_boundaries(self):
        for bad in (None, 123, "", "ar_x_y_" + "a" * 600, "not-a-token", "ar_tag_BAD!_secret"):
            with self.assertRaises(tokens.InvalidCredential):
                tokens.resolve(bad)

        bad_session = tokens.Credential(
            "sess1234", "human:owner", "owner", "human:owner", "session",
            frozenset({"*"}), "file:k", "0", "1", None,
        )
        with self.assertRaises(tokens.InvalidCredential):
            tokens.note_use(bad_session, "node-1")


class WebhookSseAndWebpushFuzzTests(unittest.TestCase):
    def test_webhook_check_rejects_duplicate_auth_headers_and_verifies_hmac(self):
        secret = "test-webhook-secret-123"
        body = b'{"action":"edited"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        with mock.patch("arete.webhooks.secret", return_value=secret):
            webhooks.check("demo", _headers(("X-Hub-Signature-256", sig)), body)
            webhooks.check("demo", _headers(("X-Arete-Token", secret)), body)
            webhooks.check("demo", _headers(("Authorization", f"Bearer {secret}")), body)

            for dup_headers in (
                _headers(("X-Hub-Signature-256", sig), ("X-Hub-Signature-256", sig)),
                _headers(("X-Arete-Token", secret), ("X-Arete-Token", "other")),
                _headers(("Authorization", f"Bearer {secret}"), ("Authorization", "Bearer other")),
                _headers(("X-Arete-Token", secret), ("Authorization", "Bearer a"), ("Authorization", "Bearer b")),
            ):
                with self.assertRaises(Stated) as ctx:
                    webhooks.check("demo", dup_headers, body)
                self.assertEqual(ctx.exception.status, 400)

            with self.assertRaises(Stated) as ctx:
                webhooks.check("demo", _headers(), body)
            self.assertEqual(ctx.exception.status, 401)

            with self.assertRaises(Stated) as ctx:
                webhooks.check("demo", _headers(("X-Hub-Signature-256", "sha256=deadbeef")), body)
            self.assertEqual(ctx.exception.status, 403)

    def test_webhook_event_of_fuzz_and_reconcile_missing_claim(self):
        deep_json = (b"[" * 2500) + b"1" + (b"]" * 2500)
        weird_cases = [
            (_headers(), b""),
            (_headers(), b"\xff\xfe\x00not-json"),
            (_headers(), deep_json),
            (_headers(), b"[1, 2, 3]"),
            (_headers(), b'{"url": null, "repository": "oops", "action": 99}'),
            (_headers(("X-GitHub-Event", "issues" + "x" * 200), ("X-GitHub-Delivery", "d" * 300)),
             b'{"action": "' + b"a" * 200 + b'", "repository": {"full_name": "' + b"r" * 300 + b'"}}'),
            (_headers(("Content-Type", "application/x-www-form-urlencoded")), b"payload=%7B%22title%22%3A%22hi%22%7D"),
        ]
        for hdrs, body in weird_cases:
            ev = webhooks.event_of("demo", hdrs, body)
            self.assertIsInstance(ev, dict)
            self.assertLessEqual(len(ev["key"]), 200)
            self.assertLessEqual(len(ev["repo"]), 200)
            self.assertLessEqual(len(ev["url"]), 500)
            self.assertNotEqual(ev["url"], "None")

        rng = random.Random(1337)
        for _ in range(80):
            body = bytes(rng.randrange(256) for _ in range(rng.randint(0, 256)))
            hdrs = _headers(("X-GitHub-Event", rng.choice(["", "issues", "push"])),
                            ("X-Request-Id", "req-" * rng.randint(0, 70)))
            ev = webhooks.event_of("fuzz", hdrs, body)
            self.assertEqual(ev["trust"], "untrusted")
            self.assertLessEqual(len(ev["key"]), 200)

        # reconcile_spec_event skips gracefully if claim disappears under lock.
        event = {
            "type": "github.issues",
            "data": {"action": "edited", "repository": {"full_name": "org/repo"}, "issue": {"number": 42}},
        }
        fake_repo = SimpleNamespace(tracker="github", github="org/repo")
        fake_cfg = SimpleNamespace(repo=lambda _name: fake_repo)
        lock_cm = mock.MagicMock()
        lock_cm.__enter__.return_value = True
        with mock.patch("arete.store.specs.pending", return_value=[{"repo": "r", "issue": "42", "state": "published", "data": {"graph": {"nodes": []}}}]), \
             mock.patch("arete.store.workspaces.ticket_lock", return_value=lock_cm), \
             mock.patch("arete.store.specs.find", return_value=None):
            self.assertEqual(webhooks.reconcile_spec_event(event, cfg=fake_cfg), [])

    def test_sse_sanitization_and_event_store_cursor_filter_limits(self):
        self.assertEqual(event_stream._sse_line("a\rb\nc\r\nd"), "a b c  d")
        self.assertEqual(event_store.cursor(None), (None, None))
        self.assertEqual(event_store.cursor("12.345"), ("12", 345))
        for bad_cursor in ("", 123, "1.2.3", "-1", "١.٢", "9" * 19, "1." + "9" * 19):
            with self.assertRaises(ValueError):
                event_store.cursor(bad_cursor)

        over = ",".join(f"x{i}" for i in range(event_store.MAX_FILTER_ITEMS + 1))
        for key in ("subject", "task", "idem", "kinds", "turns"):
            with self.assertRaises(ValueError):
                event_store.filters({key: over})
        with self.assertRaises(ValueError):
            event_store.filters({"class": ",".join(["durable"] * (event_store.MAX_FILTER_ITEMS + 1))})
        with self.assertRaises(ValueError):
            event_store.filters({"class": "bogus"})

        where, args, classes = event_store.filters({"kinds": "run_100%.*"})
        self.assertIn("ESCAPE '\\'", where)
        self.assertEqual(args, [r"run\_100\%.%",])
        self.assertEqual(classes, list(event_store.CLASSES))

    def test_webpush_checked_endpoint_fuzz_and_security_headers_and_redirect(self):
        with mock.patch("arete.paths.ISOLATED", False):
            self.assertEqual(webpush.checked_endpoint("https://push.example.com/sub/1"), "https://push.example.com/sub/1")
            self.assertEqual(webpush.checked_endpoint("http://127.0.0.1:8080/push"), "http://127.0.0.1:8080/push")
            bad_endpoints = [
                None, 123, "", "http://push.example.com/sub", "javascript:alert(1)",
                "https://push.example.com/sub\r\nX: 1", "https://push.example.com/a b",
                "https://user@push.example.com/sub", "https://@push.example.com/sub",
                "https://push.example.com:0/sub", "https://push.example.com/" + "a" * 2048,
            ]
            for ep in bad_endpoints:
                with self.assertRaises(webpush.Refused):
                    webpush.checked_endpoint(ep)

            rng = random.Random(777)
            alphabet = "abcdefghijklmnopqrstuvwxyz0123456789:/.-_@? \r\n\t\x00"
            for _ in range(100):
                candidate = "https://" + "".join(rng.choices(alphabet, k=rng.randint(0, 40)))
                try:
                    checked = webpush.checked_endpoint(candidate)
                except webpush.Refused:
                    pass
                else:
                    self.assertEqual(checked, candidate)
                    self.assertFalse(any(ord(c) <= 0x20 or ord(c) == 0x7f for c in checked))

        # Verify Handler.send_security_headers and Redirect Location sanitization.
        recorded = []
        fake_redirect = SimpleNamespace(
            path="//evil.example/phish",
            headers=Message(),
            server=SimpleNamespace(node=tailnet.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "node.ts.net"), server_port=8443),
            send_response=lambda code: recorded.append(("status", code)),
            send_header=lambda k, v: recorded.append((k, v)),
            end_headers=lambda: recorded.append(("end", True)),
            send_security_headers=lambda: dashboard.Handler.send_security_headers(fake_redirect),
        )
        dashboard.Redirect.answer(fake_redirect, "GET")
        headers_map = {k: v for k, v in recorded if k not in ("status", "end")}
        self.assertEqual(headers_map["Location"], "https://node.ts.net:8443/evil.example/phish")
        self.assertEqual(headers_map["X-Frame-Options"], "DENY")
        self.assertEqual(headers_map["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers_map["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers_map["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers_map["Content-Security-Policy"])

        recorded.clear()
        fake_redirect.path = "/ok\r\nSet-Cookie: evil=1"
        dashboard.Redirect.answer(fake_redirect, "GET")
        headers_map = {k: v for k, v in recorded if k not in ("status", "end")}
        self.assertEqual(headers_map["Location"], "https://node.ts.net:8443/")

    def test_inbox_and_webpush_id_overflow_and_reserved_manual_subjects(self):
        self.assertEqual(inbox_store.parse_id("TR-42"), 42)
        self.assertEqual(inbox_store.parse_id(42), 42)
        for bad_id in ("TR-0", "TR-" + "9" * 19, "TR-١٢", 0, -1, 2**63, True, None):
            self.assertIsNone(inbox_store.parse_id(bad_id), bad_id)

        self.assertEqual(webpush.parse_subscription_id("PS-7"), 7)
        for bad_sub in ("PS-0", "PS-" + "9" * 19, "PS-١", "7", None):
            self.assertIsNone(webpush.parse_subscription_id(bad_sub), bad_sub)

        for prefix in ("presence:owner/k", "approval:1", "APPROVAL:1", "ask:a1", "workspace:1", "proposal:5", "chat:1/draft"):
            with self.assertRaises(contract.Problem) as ctx:
                inbox.raise_manual({"subject": prefix, "kind": "freeform", "verbs": ["approve"]})
            self.assertEqual(ctx.exception.code, "validation")

        for bad_prio in (True, "not-int", 0, 4, None):
            with self.assertRaises(contract.Problem) as ctx:
                inbox.raise_manual({"subject": "alert:test", "kind": "alert", "priority": bad_prio})
            self.assertEqual(ctx.exception.code, "validation")


if __name__ == "__main__":
    unittest.main()
