from __future__ import annotations

import contextlib
import http.client
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from arete import (
    api_check,
    dashboard,
    principals,
    tailnet,
)
from arete.store import tokens as token_store
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_behavior_child(case: unittest.TestCase, code: str, *, root: Path | None = None,
                        expect_signal: int | None = None) -> dict:
    with contextlib.ExitStack() as stack:
        if root is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="arete-outbox-")))
        env = api_check.isolated_env(root)
        prelude = """
import json, sys, urllib.request
from unittest import mock
from arete import accounts, outbox, util
def _forbidden(*a, **kw):
    raise AssertionError("forbidden network/account call")
mock.patch("urllib.request.urlopen", side_effect=_forbidden).start()
mock.patch.object(accounts, "read", side_effect=_forbidden).start()
mock.patch.object(util, "wait_for_network", return_value=True).start()
outbox.RETRY_SECONDS = 0
"""
        script = prelude + "\n" + code
        cmd = api_check.child_command(REPO_ROOT, script)
        proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True,
                              timeout=CHILD_HANG_TIMEOUT, check=False)
        if expect_signal is not None:
            case.assertEqual(proc.returncode, -expect_signal, (proc.stdout, proc.stderr))
            return {}
        case.assertEqual(proc.returncode, 0, f"child exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("BEHAVIOR_RESULT="):
                return json.loads(line.removeprefix("BEHAVIOR_RESULT="))
        case.fail(f"missing BEHAVIOR_RESULT= in stdout:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


class FakeGitHubLedger:
    """Disk-backed fake for `util.sh` handling GitHub `gh` calls across SIGKILL/restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if not self.path.exists():
            self.save({"next_id": 100, "comments": [], "replies": [], "issues": [], "calls": []})

    def load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def handle(self, cmd, *, input=None, **_kw) -> subprocess.CompletedProcess:
        args = list(cmd) if isinstance(cmd, (list, tuple)) else cmd.split()
        if not args or args[0] != "gh":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        data = self.load()
        data["calls"].append({"args": args, "input": input})
        self.save(data)

        # Lagging search index: gh issue list --search / --label always returns empty list.
        if args[1:3] == ["issue", "list"] and ("--search" in args or "--label" in args):
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        if args[1:2] == ["api"]:
            return self._handle_api(cmd, args, input)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    def _handle_api(self, cmd, args: list[str], stdin: str | None) -> subprocess.CompletedProcess:
        method = "GET"
        if "--method" in args:
            method = args[args.index("--method") + 1].upper()
        elif "-X" in args:
            method = args[args.index("-X") + 1].upper()
        endpoint = next(
            a for i, a in enumerate(args[2:], start=2)
            if not a.startswith("-") and args[i - 1] not in ("--method", "-X", "--input")
        )
        parsed = urlsplit(endpoint)
        path = parsed.path
        data = self.load()

        if method == "POST":
            body_obj = json.loads(stdin or "{}")
            next_id = int(data["next_id"])
            data["next_id"] = next_id + 1
            if path.endswith("/comments") and "/issues/" in path:
                # repos/O/R/issues/N/comments
                entry = {
                    "id": next_id,
                    "path": path,
                    "body": body_obj.get("body", ""),
                    "html_url": f"https://github.com/comment/{next_id}",
                }
                data["comments"].append(entry)
                self.save(data)
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(entry), stderr="")
            if path.endswith("/replies") and "/pulls/" in path:
                # repos/O/R/pulls/N/comments/C/replies
                parts = path.split("/")
                list_path = "/".join(parts[:6])  # repos/O/R/pulls/N/comments
                entry = {
                    "id": next_id,
                    "path": list_path,
                    "body": body_obj.get("body", ""),
                    "html_url": f"https://github.com/reply/{next_id}",
                }
                data["replies"].append(entry)
                self.save(data)
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(entry), stderr="")
            if path.endswith("/issues"):
                # repos/O/R/issues
                repo = "/".join(path.split("/")[1:3])
                entry = {
                    "id": next_id,
                    "number": next_id,
                    "path": path,
                    "title": body_obj.get("title", ""),
                    "body": body_obj.get("body", ""),
                    "html_url": f"https://github.com/{repo}/issues/{next_id}",
                }
                data["issues"].append(entry)
                self.save(data)
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(entry), stderr="")

        if method == "GET":
            slurp = "--slurp" in args
            if path.endswith("/comments") and "/issues/" in path:
                rows = [r for r in data["comments"] if r["path"] == path]
            elif path.endswith("/comments") and "/pulls/" in path:
                rows = [r for r in data["replies"] if r["path"] == path]
            elif path.endswith("/issues"):
                rows = [r for r in data["issues"] if r["path"] == path]
            else:
                rows = []
            payload = [rows] if slurp else rows
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"unexpected api call: {args}")


class _LoopbackHttpsServer(tailnet.Server):
    def finish_request(self, request, client_address) -> None:
        self.RequestHandlerClass(request, client_address, self)


@contextmanager
def _start_api_server(owner_login: str = "owner@example.test", *, secure: bool = True):
    principals.seed_owner(owner_login)
    token_store.init_owner()
    address = "127.0.0.1"
    node = tailnet.Node((address,), frozenset({address}), address)
    logins = tailnet.Logins(mock.Mock())
    with (mock.patch.object(logins, "of", return_value=owner_login),
          mock.patch("socket.getfqdn", return_value=address)):
        server = _LoopbackHttpsServer(address, 0, node, logins, False, dashboard.Handler, dashboard.Redirect,
                                      tls=object() if secure else None)
        server.daemon_threads = False
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _http_call(server, method: str, path: str, *, body: dict | None = None, bearer: str | None = None,
               cookie: str | None = None, key: str | None = None) -> tuple[int, dict]:
    headers: dict[str, str] = {"Host": f"127.0.0.1:{server.server_port}"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if cookie:
        headers["Cookie"] = f"__Host-arete_session={cookie}"
        if method != "GET":
            headers["X-Arete"] = "1"
    payload = None
    if method != "GET":
        headers["Idempotency-Key"] = key or f"key-{time.monotonic_ns()}"
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        else:
            headers["Content-Length"] = "0"
    with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)) as conn:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})


class OutboxCoreTests(unittest.TestCase):
    def test_schema_draft_redactions_and_marks(self):
        result = _run_behavior_child(self, """
import sqlite3
from pathlib import Path
from arete import config, gh, outbox, paths, principals, secretstore
from arete.store import db, outbox as outbox_store

db.migrate()
paths.CONFIG_FILE.write_text(f'[secrets]\\nstore = "pve_store"\\nstore_dir = {json.dumps(str(Path.cwd() / "secret-store"))}\\n')
secretstore.put("global", "DEMO_SECRET", "super-secret-value-98765", "api_key", "hand")

# Unknown sender kind raises ValueError.
unknown_rejected = False
try:
    with db.tx() as c:
        outbox.draft(c, target="github", kind="email", object="github:o/r#1", body="hi", drafted_by="hand", slot="bad-kind")
except ValueError:
    unknown_rejected = True

# CHECK constraint rejects approved item with NULL marker and NULL legacy_approval.
check_rejected = False
with db.tx() as c:
    c.execute(
        "INSERT INTO outbox (marker, target, kind, object, audience, drafted_by, slot, rev, state, created, updated) "
        "VALUES (NULL, 'github', 'comment', 'github:o/r#1', 'self', 'hand', 'null-marker', 1, 'draft', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    bad_id = c.execute("SELECT id FROM outbox WHERE slot = 'null-marker'").fetchone()[0]
try:
    with db.tx() as c:
        c.execute("UPDATE outbox SET state = 'approved' WHERE id = ?", (bad_id,))
except sqlite3.IntegrityError:
    check_rejected = True

tag = db.schema_metadata()["instance_tag"]
with db.tx() as c:
    ob_id = outbox.draft(
        c,
        target="github",
        kind="comment",
        object="github:o/r#7",
        body="Token is super-secret-value-98765 here.",
        drafted_by="hand",
        slot="slot-1",
    )
    ob_id_2 = outbox.draft(
        c,
        target="github",
        kind="comment",
        object="github:o/r#7",
        body="Second draft body",
        drafted_by="hand",
        slot="slot-1",
    )

with db.reader() as c:
    row = dict(c.execute("SELECT * FROM outbox WHERE id = ?", (ob_id,)).fetchone())
    revs = [dict(r) for r in c.execute("SELECT * FROM outbox_revisions WHERE item = ? ORDER BY rev", (ob_id,))]
    reds = [dict(r) for r in c.execute("SELECT * FROM outbox_redactions WHERE item = ? ORDER BY rev, n", (ob_id,))]
    all_text = str(row) + str(revs) + str(reds)

# Marks on fresh DB (no runs rows before migration).
fresh_marked = gh.marked("x")
fresh_checks = {
    "ends_with_tag": fresh_marked.endswith(f"<!-- arete:{tag} -->"),
    "own_true": gh.has_arete_mark(fresh_marked),
    "other_tag_false": gh.has_arete_mark("<!-- arete:0000beef -->"),
    "bare_false": gh.has_arete_mark("<!-- arete -->"),
    "ci_false": gh.has_arete_mark("<!-- arete:ci -->"),
}

# Marks on pre-existing DB that held a runs row before m0038 (in a fresh state dir).
old_state = Path.cwd() / "old-state"
old_state.mkdir()
paths.STATE_DIR = old_state
paths.DB_FILE = old_state / "arete.db"
paths.RUNS_DIR = old_state / "runs"
paths.WORKTREES_DIR = old_state / "worktrees"
paths.LOCKS_DIR = old_state / "locks"
paths.LOG_DIR = old_state / "logs"
paths.SERVER_FILE = old_state / "server.json"
paths.SERVE_BEAT_FILE = old_state / "serve.json"
db._CHECKED.clear()
db.migrate(through=39)
with db.tx() as c:
    c.execute("INSERT INTO runs (id, kind, status, started) VALUES ('r-pre', 'pipeline', 'done', '2026-01-01T00:00:00Z')")
db.migrate()
old_tag = db.schema_metadata()["instance_tag"]
old_checks = {
    "bare_true": gh.has_arete_mark("<!-- arete -->"),
    "own_true": gh.has_arete_mark(gh.marked("y")),
    "other_tag_false": gh.has_arete_mark("<!-- arete:0000beef -->"),
    "ci_false": gh.has_arete_mark("<!-- arete:ci -->"),
    "tag_len": len(old_tag),
}

print("BEHAVIOR_RESULT=" + json.dumps({
    "unknown_rejected": unknown_rejected,
    "check_rejected": check_rejected,
    "same_id": ob_id == ob_id_2,
    "marker": row["marker"],
    "expected_marker": f"arete:{tag}:ob-{ob_id}",
    "rev": row["rev"],
    "rev1_body": revs[0]["body"],
    "redactions": reds,
    "secret_leaked": "super-secret-value-98765" in all_text,
    "fresh_checks": fresh_checks,
    "old_checks": old_checks,
}))
""")
        self.assertTrue(result["unknown_rejected"])
        self.assertTrue(result["check_rejected"])
        self.assertTrue(result["same_id"])
        self.assertEqual(result["marker"], result["expected_marker"])
        self.assertEqual(result["rev"], 2)
        self.assertIn("[secret:global/DEMO_SECRET]", result["rev1_body"])
        self.assertEqual(len(result["redactions"]), 1)
        self.assertEqual(result["redactions"][0]["kind"], "secret")
        self.assertFalse(result["secret_leaked"])
        self.assertEqual(result["fresh_checks"], {
            "ends_with_tag": True, "own_true": True, "other_tag_false": False, "bare_false": False, "ci_false": False,
        })
        self.assertEqual(result["old_checks"], {
            "bare_true": True, "own_true": True, "other_tag_false": False, "ci_false": False, "tag_len": 8,
        })

    def test_api_v1_outbox_routes_presence_conflicts_and_scopes(self):
        result = _run_behavior_child(self, """
from datetime import datetime
from unittest import mock
from arete import outbox, principals, util
from arete.store import db, tokens as token_store
from tests.test_outbox import _http_call, _start_api_server

db.migrate()
with _start_api_server() as server:
    owner = principals.owner()
    with principals.acting(owner):
        _, session_cookie = token_store.issue(owner, "Browser", ["*"], kind="session",
                                              expires=token_store.session_lifetime(util.now()))
        _, write_token = token_store.issue(owner, "Client", ["read", "outbox:write"])
        _, readonly_token = token_store.issue(owner, "Reader", ["read"])

    # Session cookie requires https scheme (set on server in _start_api_server).
    if True:
        st_meta, meta = _http_call(server, "GET", "/api/v1/meta", bearer=write_token)

        with db.tx() as c:
            ob_id = outbox.draft(
                c,
                target="github",
                kind="comment",
                object="github:o/r#7",
                body="Team message",
                drafted_by="hand",
                slot="api-team-1",
                audience="team",
            )
        oid = f"OB-{ob_id}"

        st_list, listed = _http_call(server, "GET", "/api/v1/outbox?state=draft", bearer=write_token)
        st_get, detail = _http_call(server, "GET", f"/api/v1/outbox/{oid}", bearer=write_token)
        st_prev, preview = _http_call(server, "GET", f"/api/v1/outbox/{oid}/preview", bearer=write_token)

        # Stale base_rev on PATCH -> 409 version_conflict
        st_bad_rev, bad_rev = _http_call(
            server, "PATCH", f"/api/v1/outbox/{oid}",
            body={"base_version": 1, "base_rev": 99, "body": "New"},
            bearer=write_token,
        )
        # Stale rev on approve -> 409 version_conflict
        st_bad_app, bad_app = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/approve",
            body={"base_version": 1, "rev": 99},
            cookie=session_cookie,
        )
        # Missing outbox:write scope -> 403
        st_forbid, forbid = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/open",
            body={"base_version": 1},
            bearer=readonly_token,
        )

        # Open item -> version becomes 2
        st_open, opened = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/open",
            body={"base_version": 1},
            bearer=write_token,
        )
        ver = opened["item"]["version"]

        # Approve team item with client token -> 202 awaiting_presence
        st_pres, pres = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/approve",
            body={"base_version": ver, "rev": 1},
            bearer=write_token,
        )

        # Approve team item from paired session -> sets send_at ~60s ahead
        before_app = util.now()
        st_app, approved = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/approve",
            body={"base_version": ver, "rev": 1},
            cookie=session_cookie,
        )
        send_at_delta = (util.parse_iso(approved["item"]["send_at"]) - before_app).total_seconds()
        ver = approved["item"]["version"]

        # Discard from approved -> 409 invalid_state
        st_disc_bad, disc_bad = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/discard",
            body={"base_version": ver},
            bearer=write_token,
        )

        # Undo before send_at -> returns to draft
        st_undo, undone = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/undo",
            body={"base_version": ver},
            bearer=write_token,
        )
        ver = undone["item"]["version"]

        # Edit (rev 1 -> 2), then approve via presence resolution, hold, release, reject.
        st_edit, edited = _http_call(
            server, "PATCH", f"/api/v1/outbox/{oid}",
            body={"base_version": ver, "base_rev": 1, "body": "Edited team message"},
            bearer=write_token,
        )
        ver = edited["item"]["version"]
        st_pres2, pres2 = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/approve",
            body={"base_version": ver, "rev": 2},
            bearer=write_token,
            key="presence-approve-key-1",
        )
        _, pres_item = _http_call(server, "GET", f"/api/v1/inbox/{pres2['item']}", cookie=session_cookie)
        st_pres_ok, _ = _http_call(
            server, "POST", f"/api/v1/inbox/{pres2['item']}/resolve",
            body={"base_version": pres_item["item"]["version"], "verb": "approve"},
            cookie=session_cookie,
        )
        st_replay, replayed = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/approve",
            body={"base_version": ver, "rev": 2},
            bearer=write_token,
            key="presence-approve-key-1",
        )
        ver = replayed["item"]["version"]
        st_hold, held = _http_call(server, "POST", f"/api/v1/outbox/{oid}/hold", body={"base_version": ver}, bearer=write_token)
        ver = held["item"]["version"]
        st_rel, released = _http_call(server, "POST", f"/api/v1/outbox/{oid}/release", body={"base_version": ver}, bearer=write_token)
        ver = released["item"]["version"]
        st_undo2, undone2 = _http_call(server, "POST", f"/api/v1/outbox/{oid}/undo", body={"base_version": ver}, bearer=write_token)
        ver = undone2["item"]["version"]
        st_rej, rejected = _http_call(
            server, "POST", f"/api/v1/outbox/{oid}/reject",
            body={"base_version": ver, "reason": "not ready"},
            bearer=write_token,
        )
        st_hist, hist = _http_call(server, "GET", f"/api/v1/outbox/{oid}/history", bearer=write_token)

print("BEHAVIOR_RESULT=" + json.dumps({
    "meta_status": st_meta,
    "feature_outbox": meta["features"]["outbox"],
    "list_ids": [i["id"] for i in listed["items"]],
    "detail_keys": sorted(detail.keys()),
    "preview_marker": preview["marker"],
    "preview_has_marker": f"<!-- {preview['marker']} -->" in preview["body"],
    "bad_rev": (st_bad_rev, bad_rev.get("code")),
    "bad_app": (st_bad_app, bad_app.get("code")),
    "forbid": (st_forbid, forbid.get("code")),
    "open_ok": (st_open, bool(opened["item"]["opened_at"])),
    "pres": (st_pres, pres.get("state")),
    "app": (st_app, approved["item"]["state"], 50 <= send_at_delta <= 70),
    "disc_bad": (st_disc_bad, disc_bad.get("code")),
    "undo": (st_undo, undone["item"]["state"]),
    "edit_pres_flow": (st_edit, edited["item"]["state"], st_pres2, st_pres_ok, st_replay, replayed["item"]["state"]),
    "hold_rel_undo2_rej": (
        st_hold, held["item"]["state"],
        st_rel, released["item"]["state"],
        st_undo2, undone2["item"]["state"],
        st_rej, rejected["item"]["state"], rejected["item"]["reject_reason"],
    ),
    "history_count": len(hist["events"]),
}))
""")
        self.assertEqual(result["meta_status"], 200)
        self.assertTrue(result["feature_outbox"])
        self.assertEqual(len(result["list_ids"]), 1)
        self.assertIn("revisions", result["detail_keys"])
        self.assertIn("redactions", result["detail_keys"])
        self.assertIn("attempts", result["detail_keys"])
        self.assertIn("sending_since", result["detail_keys"])
        self.assertTrue(result["preview_has_marker"])
        self.assertEqual(result["bad_rev"], [409, "version_conflict"])
        self.assertEqual(result["bad_app"], [409, "version_conflict"])
        self.assertEqual(result["forbid"][0], 403)
        self.assertEqual(result["open_ok"], [200, True])
        self.assertEqual(result["pres"], [202, "awaiting_presence"])
        self.assertEqual(result["app"], [200, "approved", True])
        self.assertEqual(result["disc_bad"], [409, "invalid_state"])
        self.assertEqual(result["undo"], [200, "draft"])
        self.assertEqual(result["edit_pres_flow"], [200, "edited", 202, 200, 200, "approved"])
        self.assertEqual(result["hold_rel_undo2_rej"], [200, "held", 200, "approved", 200, "edited", 200, "rejected", "not ready"])
        self.assertGreaterEqual(result["history_count"], 8)


class OutboxSenderTests(unittest.TestCase):
    def test_sigkill_after_and_before_store_recover_without_duplicates(self):
        with tempfile.TemporaryDirectory(prefix="arete-outbox-kill-") as tmp:
            root = Path(tmp)
            ledger_file = root / "gh-ledger.json"

            # 1) SIGKILL right AFTER fake stores comment.
            _run_behavior_child(self, f"""
import os, signal
from pathlib import Path
from unittest import mock
from arete import outbox, util
from arete.store import db
from tests.test_outbox import FakeGitHubLedger

db.migrate()
with db.tx() as c:
    ob_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#7",
                         body="Hello after kill", drafted_by="hand", slot="kill-after")
outbox.approve(ob_id)
ledger = FakeGitHubLedger(Path({str(ledger_file)!r}))
def kill_after(cmd, **kw):
    res = ledger.handle(cmd, **kw)
    if "--method" in cmd and "POST" in cmd:
        os.kill(os.getpid(), signal.SIGKILL)
    return res
with mock.patch.object(util, "sh", side_effect=kill_after):
    outbox.send_due()
""", root=root, expect_signal=signal.SIGKILL)

            # Fresh child sweeps with since=future (or after attempt start) and calls send_due().
            res_after = _run_behavior_child(self, f"""
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete import outbox, util
from arete.store import db, outbox as outbox_store
from tests.test_outbox import FakeGitHubLedger

ledger = FakeGitHubLedger(Path({str(ledger_file)!r}))
child_start = util.now() + timedelta(seconds=2)
with mock.patch.object(util, "sh", side_effect=ledger.handle):
    with db.tx() as c:
        swept = outbox.sweep(c, since=child_start)
    outbox.send_due()
tag = db.schema_metadata()["instance_tag"]
detail = outbox.get(1)
print("BEHAVIOR_RESULT=" + json.dumps({{
    "swept": swept,
    "comments": ledger.load()["comments"],
    "item": detail["item"],
    "attempts": detail["attempts"],
    "expected_tail": f"<!-- arete:{{tag}} --> <!-- arete:{{tag}}:ob-1 -->",
}}))
""", root=root)
            self.assertEqual(res_after["swept"], [1])
            self.assertEqual(len(res_after["comments"]), 1)
            self.assertTrue(res_after["comments"][0]["body"].endswith(res_after["expected_tail"]))
            self.assertEqual(res_after["item"]["state"], "sent")
            self.assertEqual(res_after["item"]["sent_ref"], str(res_after["comments"][0]["id"]))
            self.assertEqual(res_after["attempts"][0]["outcome"], "unknown")
            self.assertIn("cut off", res_after["attempts"][0]["error"])
            self.assertEqual(res_after["attempts"][1]["outcome"], "found")
            self.assertIn("repos/o/r/issues/7/comments", res_after["attempts"][1]["lookup"])
            self.assertTrue(all(a["ended"] is not None for a in res_after["attempts"]))

            # 2) SIGKILL BEFORE fake stores anything.
            ledger_file_2 = root / "gh-ledger-2.json"
            _run_behavior_child(self, f"""
import os, signal
from pathlib import Path
from unittest import mock
from arete import outbox, util
from arete.store import db
from tests.test_outbox import FakeGitHubLedger

with db.tx() as c:
    ob_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#8",
                         body="Hello before kill", drafted_by="hand", slot="kill-before")
outbox.approve(ob_id)
ledger = FakeGitHubLedger(Path({str(ledger_file_2)!r}))
def kill_before(cmd, **kw):
    if "--method" in cmd and "POST" in cmd:
        os.kill(os.getpid(), signal.SIGKILL)
    return ledger.handle(cmd, **kw)
with mock.patch.object(util, "sh", side_effect=kill_before):
    outbox.send_due()
""", root=root, expect_signal=signal.SIGKILL)

            res_before = _run_behavior_child(self, f"""
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete import outbox, util
from arete.store import db
from tests.test_outbox import FakeGitHubLedger

ledger = FakeGitHubLedger(Path({str(ledger_file_2)!r}))
child_start = util.now() + timedelta(seconds=2)
with mock.patch.object(util, "sh", side_effect=ledger.handle):
    with db.tx() as c:
        outbox.sweep(c, since=child_start)
    outbox.send_due()
detail = outbox.get(2)
sending = outbox.list_items(state="sending")
print("BEHAVIOR_RESULT=" + json.dumps({{
    "comments": ledger.load()["comments"],
    "item": detail["item"],
    "attempts": detail["attempts"],
    "sending": sending,
}}))
""", root=root)
            self.assertEqual(len(res_before["comments"]), 1)
            self.assertEqual(res_before["item"]["state"], "sent")
            self.assertEqual([a["outcome"] for a in res_before["attempts"]], ["unknown", "sent"])
            self.assertIn("not found", res_before["attempts"][1]["lookup"])
            self.assertEqual(res_before["sending"], [])

    def test_transport_faults_reply_issue_never_sent_exhaustion_and_inflight_sweep(self):
        result = _run_behavior_child(self, """
import subprocess, threading, time
from pathlib import Path
from unittest import mock
from arete import outbox, paths, secretstore, util
from arete.store import db
from tests.test_outbox import FakeGitHubLedger

db.migrate()
paths.CONFIG_FILE.write_text(f'[secrets]\\nstore = "pve_store"\\nstore_dir = {json.dumps(str(Path.cwd() / "secret-store"))}\\n')
ledger_path = paths.STATE_DIR / "ledger.json"
ledger = FakeGitHubLedger(ledger_path)

def run_unsure_case(slot, kind, obj, body, fault_rc, fault_err):
    with db.tx() as c:
        ob_id = outbox.draft(c, target="github", kind=kind, object=obj, body=body, drafted_by="hand", slot=slot)
    outbox.approve(ob_id)
    fired = [False]
    def sh_fault(cmd, **kw):
        res = ledger.handle(cmd, **kw)
        if not fired[0] and "--method" in cmd and "POST" in cmd:
            fired[0] = True
            return subprocess.CompletedProcess(cmd, fault_rc, stdout="", stderr=fault_err)
        return res
    with mock.patch.object(util, "sh", side_effect=sh_fault):
        outbox.send_due()
        outbox.send_due()
    d = outbox.get(ob_id)
    return d["item"]["state"], [a["outcome"] for a in d["attempts"]], d["item"]["sent_ref"]

c_reset = run_unsure_case("fault-reset", "comment", "github:o/r#10", "body reset", 1,
                          'Post "https://api.github.com/graphql": read tcp 1.2.3.4: read: connection reset by peer')
c_502 = run_unsure_case("fault-502", "comment", "github:o/r#11", "body 502", 1,
                        "HTTP 502: Bad Gateway (https://api.github.com/graphql)")
c_124 = run_unsure_case("fault-124", "comment", "github:o/r#12", "body 124", 124,
                        "[arete] timed out after 120s")
c_reply = run_unsure_case("fault-reply", "reply", "github:o/r#13/55", "reply text", 1,
                          "HTTP 502: Bad Gateway")
c_issue = run_unsure_case("fault-issue", "issue", "github:o/r", "Issue title\\n\\nIssue details", 1,
                          "HTTP 502: Bad Gateway")

# NEVER_SENT retried inside gh(): 1 attempt ends sent with 1 comment.
with db.tx() as c:
    ns_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#14",
                         body="never sent", drafted_by="hand", slot="never-sent")
outbox.approve(ns_id)
ns_once = [False]
def sh_never_sent(cmd, **kw):
    if not ns_once[0] and "--method" in cmd and "POST" in cmd:
        ns_once[0] = True
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error connecting to api.github.com")
    return ledger.handle(cmd, **kw)
with mock.patch.object(util, "sh", side_effect=sh_never_sent):
    outbox.send_due()
ns_detail = outbox.get(ns_id)

# Failing lookups up to MAX_ATTEMPTS -> failed with check-first error, only 1 POST; retry looks first.
with db.tx() as c:
    ex_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#15",
                         body="exhaust me", drafted_by="hand", slot="exhaust")
outbox.approve(ex_id)
ex_posts = [0]
def sh_all_fail(cmd, **kw):
    if "--method" in cmd and "POST" in cmd:
        ex_posts[0] += 1
        ledger.handle(cmd, **kw)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 502: Bad Gateway")
    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 503: Service Unavailable")
with mock.patch.object(util, "sh", side_effect=sh_all_fail):
    for _ in range(outbox.MAX_ATTEMPTS):
        outbox.send_due()
ex_failed = outbox.get(ex_id)
outbox.retry(ex_id)
with mock.patch.object(util, "sh", side_effect=ledger.handle):
    outbox.send_due()
ex_retried = outbox.get(ex_id)

# In-flight blocking send is NOT swept when since <= attempt start.
release_file = paths.STATE_DIR / "release"
with db.tx() as c:
    inf_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#16",
                          body="in flight", drafted_by="hand", slot="inflight")
outbox.approve(inf_id)
child_start = util.now()
entered = threading.Event()
def sh_block(cmd, **kw):
    if "--method" in cmd and "POST" in cmd:
        entered.set()
        while not release_file.exists():
            time.sleep(0.01)
    return ledger.handle(cmd, **kw)
with mock.patch.object(util, "sh", side_effect=sh_block):
    t = threading.Thread(target=outbox.send_due)
    t.start()
    entered.wait(timeout=5)
    swept_counts = []
    for _ in range(3):
        with db.tx() as c:
            swept_counts.append(len(outbox.sweep(c, since=child_start)))
    mid_state = outbox.get(inf_id)["item"]["state"]
    mid_open = outbox.get(inf_id)["attempts"][0]["ended"] is None
    release_file.write_text("go")
    t.join(timeout=5)
inf_after = outbox.get(inf_id)["item"]["state"]

# Scrub hit at send: value registered AFTER approval moves item to held, attempt refused, no POST.
with db.tx() as c:
    sc_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#17",
                         body="Contains late-registered-secret-424242!", drafted_by="hand", slot="late-secret")
outbox.approve(sc_id)
secretstore.put("global", "LATE_SECRET", "late-registered-secret-424242", "api_key", "hand")
before_calls = len(ledger.load()["calls"])
with mock.patch.object(util, "sh", side_effect=ledger.handle):
    outbox.send_due()
after_calls = len(ledger.load()["calls"])
sc_detail = outbox.get(sc_id)

# 429 rate limit -> refused with retry_after and rescheduled approved; 422 GhError -> refused & failed with http_status=422.
with db.tx() as c:
    rl_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#18",
                         body="rate limited", drafted_by="hand", slot="rate-limit")
    bad_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#19",
                          body="unprocessable", drafted_by="hand", slot="http-422")
outbox.approve(rl_id)
outbox.approve(bad_id)
def sh_refusals(cmd, **kw):
    if "issues/18/" in " ".join(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: HTTP 429 API rate limit exceeded")
    if "issues/19/" in " ".join(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: HTTP 422 Validation Failed")
    return ledger.handle(cmd, **kw)
with mock.patch.object(util, "sh", side_effect=sh_refusals):
    outbox.send_due()
rl_detail = outbox.get(rl_id)
bad_detail = outbox.get(bad_id)

print("BEHAVIOR_RESULT=" + json.dumps({
    "c_reset": c_reset,
    "c_502": c_502,
    "c_124": c_124,
    "c_reply": c_reply,
    "c_issue": c_issue,
    "ns": (ns_detail["item"]["state"], [a["outcome"] for a in ns_detail["attempts"]]),
    "ex_posts": ex_posts[0],
    "ex_failed_state": ex_failed["item"]["state"],
    "ex_failed_err": ex_failed["item"]["error"],
    "ex_lookups_failed": all((a["lookup"] or "").startswith("failed:") for a in ex_failed["attempts"][1:]),
    "ex_retried_state": ex_retried["item"]["state"],
    "ex_retried_last": ex_retried["attempts"][-1]["outcome"],
    "inflight": (swept_counts, mid_state, mid_open, inf_after),
    "scrub_send": (sc_detail["item"]["state"], sc_detail["attempts"][0]["outcome"],
                   sc_detail["attempts"][0]["error"], after_calls - before_calls),
    "rate_limit": (rl_detail["item"]["state"], rl_detail["attempts"][0]["outcome"], rl_detail["attempts"][0]["retry_after"]),
    "http_422": (bad_detail["item"]["state"], bad_detail["attempts"][0]["outcome"], bad_detail["attempts"][0]["http_status"]),
}))
""")
        for key in ("c_reset", "c_502", "c_124", "c_reply", "c_issue"):
            self.assertEqual(result[key][:2], ["sent", ["unknown", "found"]], key)
        self.assertEqual(result["ns"], ["sent", ["sent"]])
        self.assertEqual(result["ex_posts"], 1)
        self.assertEqual(result["ex_failed_state"], "failed")
        self.assertIn("reply was lost", result["ex_failed_err"])
        self.assertTrue(result["ex_lookups_failed"])
        self.assertEqual(result["ex_retried_state"], "sent")
        self.assertEqual(result["ex_retried_last"], "found")
        self.assertEqual(result["inflight"], [[0, 0, 0], "sending", True, "sent"])
        self.assertEqual(result["scrub_send"], ["held", "refused", "scrub hit", 0])
        self.assertEqual(result["rate_limit"], ["approved", "refused", 60])
        self.assertEqual(result["http_422"], ["failed", "refused", 422])

    def test_jira_transport_split_and_serve_tick_order(self):
        result = _run_behavior_child(self, """
import io, threading, urllib.error
from email.message import Message
from unittest import mock
from arete import jira, outbox, paths, serve
from arete.store import db

db.migrate()
paths.CONFIG_FILE.write_text("")

class _Resp(io.BytesIO):
    def __init__(self, data: bytes, url: str = "http://127.0.0.1/rest/api/3/issue/APP-1/comment"):
        super().__init__(data)
        self._url = url
    def geturl(self):
        return self._url
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def run_jira_post_fault(exc_factory):
    stored = []
    class Opener:
        def open(self, req, timeout=None):
            if req.get_method() == "POST":
                stored.append(req.data)
                raise exc_factory(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", Message(), io.BytesIO(b""))
    client = jira.Client("http://127.0.0.1", "a@b.test", "tok")
    raised = None
    with mock.patch.object(jira, "_opener", Opener()):
        try:
            client.comment("APP-1", "hello")
        except Exception as e:
            raised = type(e).__name__
    return raised, len(stored)

j_502 = run_jira_post_fault(lambda u: urllib.error.HTTPError(u, 502, "Bad Gateway", Message(), io.BytesIO(b"")))
j_timeout = run_jira_post_fault(lambda u: TimeoutError("timed out"))
j_reset = run_jira_post_fault(lambda u: ConnectionResetError("reset"))

# 429 with Retry-After: 0 and URLError(ConnectionRefusedError) retry POST once.
def run_jira_post_retry(exc_factory):
    stored = []
    calls = [0]
    class Opener:
        def open(self, req, timeout=None):
            calls[0] += 1
            if calls[0] == 1:
                raise exc_factory(req.full_url)
            stored.append(req.data)
            return _Resp(b'{"id": "501"}')
    client = jira.Client("http://127.0.0.1", "a@b.test", "tok")
    with mock.patch.object(jira, "_opener", Opener()), mock.patch("time.sleep"):
        url = client.comment("APP-1", "hello", return_url=True)
    return calls[0], len(stored), bool(url)

hdrs_429 = Message()
hdrs_429["Retry-After"] = "0"
j_429 = run_jira_post_retry(lambda u: urllib.error.HTTPError(u, 429, "Too Many Requests", hdrs_429, io.BytesIO(b"")))
j_refused = run_jira_post_retry(lambda u: urllib.error.URLError(ConnectionRefusedError("refused")))

# GET 502 then 200 is still retried.
get_calls = [0]
class GetOpener:
    def open(self, req, timeout=None):
        get_calls[0] += 1
        if get_calls[0] == 1:
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", Message(), io.BytesIO(b""))
        return _Resp(b'{"ok": true}')
client = jira.Client("http://127.0.0.1", "a@b.test", "tok")
with mock.patch.object(jira, "_opener", GetOpener()), mock.patch("time.sleep"):
    get_res = client.get("/rest/api/3/myself")

# Server.tick() runs outbox.sweep(..., server.started) before outbox.send_due(), and never starts a second sender while running.
order = []
sweep_sinces = []
send_started = threading.Event()
send_release = threading.Event()
def fake_sweep(c, since=None):
    order.append("sweep")
    sweep_sinces.append(since)
    return []
def fake_send_due():
    order.append("send_due")
    send_started.set()
    send_release.wait(timeout=5)
    return 0

srv = serve.Server()
with mock.patch.object(outbox, "sweep", side_effect=fake_sweep), \\
     mock.patch.object(outbox, "send_due", side_effect=fake_send_due), \\
     mock.patch.object(srv, "start_approval_jobs"):
    srv.approvals()
    send_started.wait(timeout=5)
    srv.approvals()
    send_release.set()
    srv.outbox_job.result(timeout=5)

print("BEHAVIOR_RESULT=" + json.dumps({
    "j_502": j_502,
    "j_timeout": j_timeout,
    "j_reset": j_reset,
    "j_429": j_429,
    "j_refused": j_refused,
    "get_retry": (get_calls[0], get_res),
    "order": order,
    "since_matches": len(sweep_sinces) == 1 and sweep_sinces[0] == srv.started,
}))
""")
        self.assertEqual(result["j_502"], ["JiraUnsure", 1])
        self.assertEqual(result["j_timeout"], ["JiraUnsure", 1])
        self.assertEqual(result["j_reset"], ["JiraUnsure", 1])
        self.assertEqual(result["j_429"], [2, 1, True])
        self.assertEqual(result["j_refused"], [2, 1, True])
        self.assertEqual(result["get_retry"], [2, {"ok": True}])
        self.assertEqual(result["order"], ["sweep", "send_due"])
        self.assertTrue(result["since_matches"])


class OutboxLegacyAndInboxTests(unittest.TestCase):
    def test_legacy_approvals_migration_triggers_skip_and_verbs(self):
        result = _run_behavior_child(self, """
import json
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete import contract, delivery, outbox, paths, util
from arete.store import approvals as approvals_store, db, outbox as outbox_store
from tests.test_outbox import FakeGitHubLedger

# Seed DB at m0037 with 5 approvals rows (one per status).
db.migrate(through=39)
decided_ts = "2026-11-01T12:00:00Z"
with db.tx() as c:
    c.execute("INSERT INTO runs (id, kind, repo, status, started) VALUES ('r1', 'pipeline', 'o/r', 'done', '2026-11-01T10:00:00Z')")
    rows = [
        ("loop1", "r1", json.dumps({"type": "ticket", "item": {}}), "Pending ticket", "pending", "", "2026-11-01T11:00:00Z", ""),
        ("loop1", "r1", json.dumps({"type": "pr", "item": {}}), "Sending PR", "approved", "", "2026-11-01T11:01:00Z", decided_ts),
        ("loop1", "r1", json.dumps({"type": "push", "item": {}}), "Done push", "done", "https://github.com/o/r/pull/1", "2026-11-01T11:02:00Z", decided_ts),
        ("loop1", "r1", json.dumps({"type": "loop", "item": {}}), "Rejected loop", "rejected", "no thanks", "2026-11-01T11:03:00Z", decided_ts),
        ("loop1", "r1", json.dumps({"type": "push", "item": {}}), "Failed push", "failed", "boom", "2026-11-01T11:04:00Z", decided_ts),
    ]
    for r in rows:
        c.execute(
            "INSERT INTO approvals (loop, run_id, emit, summary, status, result, created, decided) VALUES (?,?,?,?,?,?,?,?)",
            r,
        )

db.migrate()
migrated = [outbox.get(i["id"]) for i in outbox.list_items()]

# Live trigger tests: add_approval, set_approval_note, claim_approval -> settle_approval(done) & (failed).
aid1 = approvals_store.add_approval("loop2", "r1", {"type": "push", "item": {}}, "Live push 1")
approvals_store.set_approval_note(aid1, "plain_summary", "x")
with db.reader() as c:
    ob1_id = outbox_store.get_by_legacy_approval(c, aid1)["id"]
after_note = outbox.get(ob1_id)

tok1 = approvals_store.claim_approval(aid1, util.iso(util.now() + timedelta(minutes=2)))
after_claim = outbox.get(ob1_id)
approvals_store.settle_approval(aid1, "done", "approved", "https://github.com/o/r/pull/9", judged=False, token=tok1)
after_done = outbox.get(ob1_id)

aid2 = approvals_store.add_approval("loop2", "r1", {"type": "push", "item": {}}, "Live push 2")
with db.reader() as c:
    ob2_id = outbox_store.get_by_legacy_approval(c, aid2)["id"]
tok2 = approvals_store.claim_approval(aid2, util.iso(util.now() + timedelta(minutes=2)))
approvals_store.settle_approval(aid2, "failed", "failed", "err2", judged=False, token=tok2)
after_failed = outbox.get(ob2_id)

# send_due() and sweep(+1h) leave all legacy rows untouched and log no gh call.
ledger = FakeGitHubLedger(paths.STATE_DIR / "legacy-gh.json")
with mock.patch.object(util, "sh", side_effect=ledger.handle):
    outbox.send_due()
    with db.tx() as c:
        swept = outbox.sweep(c, since=util.now() + timedelta(hours=1))
gh_calls = len(ledger.load()["calls"])

# outbox.approve on legacy draft calls delivery.decide(id, True), and outbox.edit returns 409 invalid_state.
aid3 = approvals_store.add_approval("loop2", "r1", {"type": "push", "item": {}}, "Live push 3")
with db.reader() as c:
    ob3_id = outbox_store.get_by_legacy_approval(c, aid3)["id"]
decided_calls = []
with mock.patch.object(delivery, "decide", side_effect=lambda i, y: decided_calls.append((i, y)) or "ok"):
    outbox.approve(ob3_id)
edit_err = None
try:
    outbox.edit(ob3_id, body="nope", base_rev=1)
except contract.Problem as e:
    edit_err = (e.status, e.code)

print("BEHAVIOR_RESULT=" + json.dumps({
    "migrated_states": [m["item"]["state"] for m in migrated],
    "migrated_kinds": [m["item"]["kind"] for m in migrated],
    "migrated_sending_open": (len(migrated[1]["attempts"]), migrated[1]["attempts"][0]["started"], migrated[1]["attempts"][0]["ended"]),
    "migrated_sent_ref": migrated[2]["item"]["sent_ref"],
    "after_note_plain": after_note["item"]["plain_summary"],
    "after_claim": (after_claim["item"]["state"], len(after_claim["attempts"]), after_claim["attempts"][0]["ended"] is None),
    "after_done": (after_done["item"]["state"], after_done["attempts"][0]["outcome"], after_done["item"]["sent_ref"]),
    "after_failed": (after_failed["item"]["state"], after_failed["attempts"][0]["outcome"]),
    "swept": swept,
    "gh_calls": gh_calls,
    "decided_calls": decided_calls,
    "edit_err": edit_err,
}))
""")
        self.assertEqual(result["migrated_states"], ["draft", "sending", "sent", "rejected", "failed"])
        self.assertEqual(result["migrated_kinds"], ["issue", "pr", "push", "loop", "push"])
        self.assertEqual(result["migrated_sending_open"], [1, "2026-11-01T12:00:00Z", None])
        self.assertEqual(result["migrated_sent_ref"], "https://github.com/o/r/pull/1")
        self.assertEqual(result["after_note_plain"], "x")
        self.assertEqual(result["after_claim"], ["sending", 1, True])
        self.assertEqual(result["after_done"], ["sent", "sent", "https://github.com/o/r/pull/9"])
        self.assertEqual(result["after_failed"], ["failed", "unknown"])
        self.assertEqual(result["swept"], [])
        self.assertEqual(result["gh_calls"], 0)
        self.assertEqual(result["decided_calls"][0][1], True)
        self.assertEqual(result["edit_err"], [409, "invalid_state"])

    def test_inbox_adapter_stale_sending_and_cli_approve(self):
        result = _run_behavior_child(self, """
import io, os
from contextlib import redirect_stdout
from datetime import timedelta
from unittest import mock
from arete import cli, credential_files, inbox, outbox, principals, util
from arete.store import approvals as approvals_store, db, tokens as token_store
from tests.test_outbox import _http_call, _start_api_server

db.migrate()
with _start_api_server() as server:
    owner = principals.owner()
    with principals.acting(owner):
        _, session_cookie = token_store.issue(owner, "Browser", ["*"], kind="session",
                                              expires=token_store.session_lifetime(util.now()))
        _, client_token = token_store.issue(owner, "Client", ["read", "outbox:write", "inbox:resolve"])

    if True:
        # 1) Drafted outbox item + legacy approval draft.
        with db.tx() as c:
            ob_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#7",
                                 body="Inbox outbox draft", drafted_by="hand", slot="inbox-1", summary="Review comment")
        leg_id = approvals_store.add_approval("demo", "", {"type": "push", "item": {"title": "P", "about": "A", "files": []}}, "Legacy draft")

        inbox.sync_all()
        st1, listed = _http_call(server, "GET", "/api/v1/inbox", cookie=session_cookie)
        subjects = {it["subject"]: it for it in listed["items"]}
        ob_inbox = subjects.get(f"outbox:{ob_id}")
        leg_approval_inbox = subjects.get(f"approval:{leg_id}")
        with db.reader() as c:
            leg_ob_id = c.execute("SELECT id FROM outbox WHERE legacy_approval = ?", (leg_id,)).fetchone()[0]
        leg_has_outbox_inbox = f"outbox:{leg_ob_id}" in subjects

        # Approving the outbox item from paired session resolves its inbox item.
        st_res, resolved = _http_call(
            server, "POST", f"/api/v1/inbox/{ob_inbox['id']}/resolve",
            body={"base_version": ob_inbox["version"], "verb": "approve"},
            cookie=session_cookie,
        )
        st2, after_listed = _http_call(server, "GET", "/api/v1/inbox", cookie=session_cookie)
        still_open = any(it["subject"] == f"outbox:{ob_id}" for it in after_listed["items"])

        # 2) Item left in sending > 120s raises LOW outbox item with detail.sending_since and resolves on settle.
        with db.tx() as c:
            stale_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#9",
                                    body="Stale send", drafted_by="hand", slot="stale-send")
            old_started = util.iso(util.now() - timedelta(seconds=180))
            c.execute("UPDATE outbox SET state = 'sending', attempts = 1 WHERE id = ?", (stale_id,))
            c.execute("INSERT INTO delivery_attempts (item, n, started) VALUES (?, 1, ?)", (stale_id, old_started))
        inbox.sync_all()
        _, stale_listed = _http_call(server, "GET", "/api/v1/inbox", cookie=session_cookie)
        stale_item = next((it for it in stale_listed["items"] if it["subject"] == f"outbox:{stale_id}"), None)
        with db.tx() as c:
            c.execute("UPDATE outbox SET state = 'sent', sent_ref = '999', sent_at = ? WHERE id = ?", (util.iso(), stale_id))
            c.execute("UPDATE delivery_attempts SET ended = ?, outcome = 'sent' WHERE item = ? AND n = 1", (util.iso(), stale_id))
        inbox.sync_all()
        _, settled_listed = _http_call(server, "GET", "/api/v1/inbox", cookie=session_cookie)
        stale_still_open = any(it["subject"] == f"outbox:{stale_id}" for it in settled_listed["items"])

        # 3) CLI: cli.main(["approve"]) prints draft, legacy draft, then failed item by OB- id,
        # and cli.main(["approve", "<failed OB-id>"]) calls outbox.retry.
        with db.tx() as c:
            draft2_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#20",
                                     body="Second draft", drafted_by="hand", slot="cli-draft", summary="Draft summary")
            fail_id = outbox.draft(c, target="github", kind="comment", object="github:o/r#21",
                                   body="Failed draft", drafted_by="hand", slot="cli-fail", summary="Failed summary")
            c.execute("UPDATE outbox SET state = 'failed', error = 'HTTP 422' WHERE id = ?", (fail_id,))

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_list = cli.main(["approve"])
        list_out = buf.getvalue()

        retry_calls = []
        real_retry = outbox.retry
        def rec_retry(item_id, **kw):
            retry_calls.append(item_id)
            return real_retry(item_id, **kw)

        buf2 = io.StringIO()
        with mock.patch.dict(os.environ, {"ARETE_SERVER": f"http://127.0.0.1:{server.server_port}"}), \\
             mock.patch.object(outbox, "retry", side_effect=rec_retry), \\
             redirect_stdout(buf2):
            rc_retry = cli.main(["approve", f"OB-{fail_id}"])
        fail_after = outbox.get(fail_id)["item"]["state"]

print("BEHAVIOR_RESULT=" + json.dumps({
    "ob_inbox": (ob_inbox["kind"], ob_inbox["priority"], ob_inbox["detail"]["rev"], ob_inbox["verbs"], ob_inbox.get("outbox_id")),
    "leg_approval_present": leg_approval_inbox is not None,
    "leg_has_outbox_inbox": leg_has_outbox_inbox,
    "resolved_ok": (st_res, resolved["item"]["state"], still_open),
    "stale_item": (stale_item["priority"], stale_item["verbs"], stale_item["detail"]["sending_since"]) if stale_item else None,
    "stale_still_open": stale_still_open,
    "rc_list": rc_list,
    "list_order": (
        list_out.find(f"OB-{leg_ob_id}"),
        list_out.find(f"OB-{draft2_id}"),
        list_out.find(f"OB-{fail_id}"),
    ),
    "rc_retry": rc_retry,
    "retry_calls": retry_calls,
    "fail_after": fail_after,
}))
""")
        self.assertEqual(result["ob_inbox"][:4], ["outbox", 2, 1, ["approve", "reject"]])
        self.assertTrue(result["ob_inbox"][4].startswith("OB-"))
        self.assertTrue(result["leg_approval_present"])
        self.assertFalse(result["leg_has_outbox_inbox"])
        self.assertEqual(result["resolved_ok"], [200, "resolved", False])
        self.assertIsNotNone(result["stale_item"])
        self.assertEqual(result["stale_item"][0], 3)
        self.assertEqual(result["stale_item"][1], [])
        self.assertTrue(result["stale_item"][2])
        self.assertFalse(result["stale_still_open"])
        self.assertEqual(result["rc_list"], 0)
        leg_pos, draft_pos, fail_pos = result["list_order"]
        self.assertTrue(leg_pos >= 0 and draft_pos >= 0 and fail_pos > max(leg_pos, draft_pos))
        self.assertEqual(result["rc_retry"], 0)
        self.assertEqual(len(result["retry_calls"]), 1)
        self.assertEqual(result["fail_after"], "approved")


if __name__ == "__main__":
    unittest.main()
