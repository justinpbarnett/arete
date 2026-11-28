from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from arete import api_check, configschema, connectors, paths

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_isolated(script: str, timeout: int = 30) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-conn-") as td:
        root = Path(td)
        env = {
            **api_check.isolated_env(root),
            "ARETE_TEST_FAST_SYNC": "1",
            "PYTHONPATH": str(REPO_ROOT),
        }
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(root)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT=")), "")
        if proc.returncode != 0 or not line:
            raise AssertionError(
                f"isolated child failed (rc={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        return json.loads(line.removeprefix("RESULT="))


class ConnectorSchemaAndOpenApiTests(unittest.TestCase):
    def test_protected_defaults_and_openapi_projection(self):
        for name in ("github", "jira"):
            default_file = paths.DEFAULTS_DIR / "connectors" / f"{name}.toml"
            self.assertTrue(default_file.is_file(), f"missing engine default {default_file}")
            raw = default_file.read_text(encoding="utf-8")
            data = tomllib.loads(raw)
            conn = connectors.validate_structure(name, data, where=str(default_file), allow_protected=True, raw_text=raw)
            self.assertEqual(conn.id, name)
            self.assertEqual(conn.service, name)
            self.assertTrue(conn.protected)
            self.assertTrue(conn.enabled)

        openapi_doc = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(
            openapi_doc["components"]["schemas"]["config.connector"],
            configschema.schema("connector"),
        )
        self.assertIn("arete/config_schemas/connector.json", openapi_doc["x-arete-contract-sources-sha256"])
        for route in ("/api/v1/accounts/status", "/api/v1/connectors", "/api/v1/connectors/{id}"):
            self.assertIn(route, openapi_doc["paths"])

    def test_structure_rejects_invalid_services_fields_and_credentials(self):
        base_gh = {
            "schema": 1,
            "id": "gh-work",
            "service": "github",
            "account": "github:token",
            "endpoint": "https://api.github.com",
            "secrets": {"api_token": "secret:connector:gh-work/GITHUB_TOKEN"},
        }
        conn = connectors.validate_structure("gh-work", base_gh)
        self.assertEqual(conn.service, "github")
        self.assertEqual(conn.endpoint, "https://api.github.com")

        bad_cases = [
            ("github", {**base_gh, "id": "github"}, "protected engine default"),
            ("jira", {"schema": 1, "id": "jira", "service": "jira", "account": "jira:api_key", "endpoint": "https://x.atlassian.net"}, "protected engine default"),
            ("slack-1", {"schema": 1, "id": "slack-1", "service": "slack", "account": "slack:token"}, "must be one of"),
            ("gh-work", {**base_gh, "driver": "jira"}, "driver"),
            ("gh-work", {**base_gh, "id": "other"}, "must match connector name"),
            ("gh-work", {**base_gh, "extra": True}, "unknown key extra"),
            ("gh-work", {**base_gh, "token": "ghp_1234567890abcdef123456"}, "literal credential"),
            ("gh-work", {**base_gh, "secrets": {"api_token": "ghp_1234567890abcdef123456"}}, "literal credential"),
            ("gh-work", {**base_gh, "secrets": {"api_token": "secret:connector:other/GITHUB_TOKEN"}}, "not allowed for connector"),
            ("gh-work", {**base_gh, "secrets": {"api_token": "secret:global/FINGERPRINT_KEY"}}, "internal Arete secrets"),
            ("gh-work", {**base_gh, "endpoint": "https://user:pass@api.github.com"}, "literal credential"),
            ("gh-work", {**base_gh, "endpoint": "http://api.github.com"}, "https://"),
            ("gh-work", {**base_gh, "options": {"email": "dev@example.com"}}, "not supported for github"),
            ("jira-1", {"schema": 1, "id": "jira-1", "service": "jira", "account": "jira:api_key", "endpoint": "https://x.atlassian.net", "options": {"app_id": "123"}}, "not supported for jira"),
            ("jira-1", {"schema": 1, "id": "jira-1", "service": "jira", "account": "jira:api_key", "endpoint": "https://x.atlassian.net", "secrets": {"app_private_key": "secret:global/KEY"}}, "not supported for jira"),
            ("gh-work", {**base_gh, "name": "ATATT3xFfGF0secretvalue12345"}, "literal credential"),
            ("gh-work", {**base_gh, "name": "-----BEGIN RSA PRIVATE KEY-----"}, "literal credential"),
        ]
        for item_name, payload, expected in bad_cases:
            with self.subTest(item_name=item_name, expected=expected), self.assertRaisesRegex(ValueError, expected):
                connectors.validate_structure(item_name, payload)


class ConnectorLifecycleAndTransportTests(unittest.TestCase):
    class_timeout = 30

    def test_create_read_draft_check_activate_edit_and_real_drivers(self):
        result = _run_isolated(r"""
import base64
import http.client
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from arete import (
    accounts,
    config,
    configrepo,
    connectors,
    dashboard,
    drafts,
    gh,
    jira,
    paths,
    principals,
    secretstore,
    tailnet,
    util,
)
from arete.store import credentials as cred_store
from arete.store import db
from arete.store import tokens as token_store

root = Path(sys.argv[1])
store_dir = root / "pve-secrets"
store_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(store_dir, 0o700)

db.migrate()
owner = principals.seed_owner("owner@example.com")

# Generate a real RSA key for GitHub App JWT signing.
pem_proc = subprocess.run(
    ["openssl", "genrsa", "2048"],
    capture_output=True,
    check=True,
    stdin=subprocess.DEVNULL,
)
app_pem = pem_proc.stdout.decode("ascii")

gh_hits = []
gh_app_hits = []
jira_a_hits = []
jira_b_hits = []
jira_default_hits = []


def start_fake_server(handler_cls):
    with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = False
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01})
    th.start()
    return srv, th, f"http://127.0.0.1:{srv.server_port}"


class FakeGitHubTokenHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        gh_hits.append((self.path, self.headers.get("Authorization")))
        if self.path == "/user":
            body = json.dumps({"login": "gh-work-bot", "id": 101}).encode()
        elif self.path.startswith("/repos/acme/app/issues"):
            body = json.dumps([{"number": 7, "title": "from-work-connector"}]).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class FakeGitHubAppHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        gh_app_hits.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/app":
            body = json.dumps({"id": 4242, "slug": "arete-app"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        gh_app_hits.append(("POST", self.path, self.headers.get("Authorization"), json.loads(raw)))
        if self.path == "/app/installations/9901/access_tokens":
            body = json.dumps({
                "token": "minted-app-clone-token-9901",
                "permissions": {"contents": "read"},
                "repository_selection": "selected",
                "repositories": [{"full_name": "acme/app"}],
            }).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def make_jira_handler(hit_log, account_id, site_tag):
    class FakeJiraHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            hit_log.append((self.path, self.headers.get("Authorization")))
            if self.path == "/rest/api/3/myself":
                body = json.dumps({"accountId": account_id, "site": site_tag}).encode()
            elif self.path.startswith("/rest/api/3/search/jql"):
                body = json.dumps({"issues": [{"key": f"{site_tag}-1", "fields": {"summary": f"from-{site_tag}"}}], "isLast": True}).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    return FakeJiraHandler


gh_srv, gh_th, gh_url = start_fake_server(FakeGitHubTokenHandler)
gh_app_srv, gh_app_th, gh_app_url = start_fake_server(FakeGitHubAppHandler)
jira_a_srv, jira_a_th, jira_a_url = start_fake_server(make_jira_handler(jira_a_hits, "jira-acct-a", "A"))
jira_b_srv, jira_b_th, jira_b_url = start_fake_server(make_jira_handler(jira_b_hits, "jira-acct-b", "B"))
jira_def_srv, jira_def_th, jira_def_url = start_fake_server(make_jira_handler(jira_default_hits, "jira-acct-def", "DEF"))

# Write config.toml with pve_store + compatibility default [jira] config.
cfg_toml = (
    'alert_repo = "acme/alerts"\n'
    "[secrets]\n"
    'store = "pve_store"\n'
    f'store_dir = {json.dumps(str(store_dir))}\n'
    "[jira]\n"
    f'base_url = {json.dumps(jira_def_url)}\n'
    'email = "default-jira@example.com"\n'
)
paths.CONFIG_FILE.write_text(cfg_toml, encoding="utf-8")
configrepo.init()

# Populate secretstore and existing registry accounts (no login/OAuth started).
gh_tok_val = "synthetic-github-work-secret-1111"
jira_a_val = "synthetic-jira-prod-secret-2222"
jira_b_val = "synthetic-jira-stage-secret-3333"
jira_def_val = "synthetic-jira-default-secret-4444"

gh_tok_row = secretstore.put("connector:github-work", "GITHUB_TOKEN", gh_tok_val, "api_key", owner, provider="github")
gh_app_row = secretstore.put("connector:github-app", "GITHUB_APP_PRIVATE_KEY", app_pem, "app_private_key", owner, provider="github")
jira_a_row = secretstore.put("connector:jira-prod", "JIRA_API_TOKEN", jira_a_val, "api_key", owner, provider="jira")
jira_b_row = secretstore.put("global", "JIRA_STAGE_TOKEN", jira_b_val, "api_key", owner, provider="jira")
jira_def_row = secretstore.put("global", "JIRA_API_TOKEN", jira_def_val, "api_key", owner, provider="jira")

acct_gh_tok = accounts.record_workspace(gh_tok_row, "github", "token")
acct_gh_app = accounts.record_workspace(gh_app_row, "github", "app")
acct_jira_a = accounts.record_workspace(jira_a_row, "jira", "api_key")
acct_jira_b = accounts.record_workspace(jira_b_row, "jira", "api_key")

with db.tx() as c:
    c.execute("UPDATE accounts SET usage_scope = ? WHERE id = ?", (gh_url, acct_gh_tok["id"]))
    c.execute("UPDATE accounts SET usage_scope = ? WHERE id = ?", (gh_app_url, acct_gh_app["id"]))
    c.execute("UPDATE accounts SET usage_scope = ? WHERE id = ?", (jira_a_url, acct_jira_a["id"]))
    c.execute("UPDATE accounts SET usage_scope = ? WHERE id = ?", (jira_b_url, acct_jira_b["id"]))
    token_store.ensure_key(c)
    _, owner_session = token_store.issue(
        owner, "owner-browser", ["*"], kind="session", expires=token_store.session_lifetime(util.now())
    )

class QuietHandler(dashboard.Handler):
    def log_request(self, *_):
        pass

    def admit(self, method, path):
        self.server.tls = object()
        try:
            return super().admit(method, path)
        finally:
            self.server.tls = None

address = "127.0.0.1"
node = tailnet.Node((address,), frozenset({address}), address)
class _TestLogins(tailnet.Logins):
    def of(self, _peer):
        return "owner@example.com"


logins = _TestLogins(mock.Mock())

with mock.patch("socket.getfqdn", return_value=address):
    server = tailnet.Server(address, 0, node, logins, False, QuietHandler, dashboard.Redirect)
    server.daemon_threads = False
    srv_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    srv_thread.start()

try:
    sess_headers = {"Cookie": f"__Host-arete_session={owner_session}", "X-Arete": "1"}

    def req(method, path, body=None, *, headers=None, expected=200):
        conn = http.client.HTTPConnection(address, server.server_port, timeout=10)
        try:
            hdrs = {"Content-Type": "application/json", **sess_headers, **(headers or {})}
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            data = json.loads(raw) if raw else {}
            assert resp.status == expected, (method, path, resp.status, data)
            return data, resp
        finally:
            conn.close()

    # 1. Read-only discovery/status performs ZERO writes or logins.
    with db.reader() as c:
        before_seq = c.execute("SELECT COALESCE(MAX(seq), 0) FROM events_log").fetchone()[0]
        before_accts = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        before_sec_ev = c.execute("SELECT COUNT(*) FROM secret_events").fetchone()[0]

    acct_status, _ = req("GET", "/api/v1/accounts/status")
    conn_list_0, _ = req("GET", "/api/v1/connectors")
    cfg_list_0, _ = req("GET", "/api/v1/config/connector")
    gh_def_cfg, _ = req("GET", "/api/v1/config/connector/github")
    jira_def_cfg, _ = req("GET", "/api/v1/config/connector/jira")
    gh_def_view, _ = req("GET", "/api/v1/connectors/github")
    jira_def_view, _ = req("GET", "/api/v1/connectors/jira")

    with db.reader() as c:
        assert c.execute("SELECT COALESCE(MAX(seq), 0) FROM events_log").fetchone()[0] == before_seq
        assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before_accts
        assert c.execute("SELECT COUNT(*) FROM secret_events").fetchone()[0] == before_sec_ev

    assert {item["name"] for item in cfg_list_0["items"]} == {"github", "jira"}
    assert gh_def_cfg["item"]["level"] == "engine" and jira_def_cfg["item"]["level"] == "engine"
    assert gh_def_view["connector"]["protected"] is True and jira_def_view["connector"]["protected"] is True
    assert len(acct_status["accounts"]) == 4 and all(a["usable"] for a in acct_status["accounts"])

    # 2. Draft, check, activate github-work (token mode), github-app (app mode), jira-prod (api_key mode).
    gh_work_body = (
        'schema = 1\n'
        'id = "github-work"\n'
        'name = "Work GitHub"\n'
        'service = "github"\n'
        'driver = "github"\n'
        f'account = {json.dumps(acct_gh_tok["id"])}\n'
        'enabled = true\n'
        f'endpoint = {json.dumps(gh_url)}\n'
        '[secrets]\n'
        'api_token = "secret:connector:github-work/GITHUB_TOKEN"\n'
    )
    gh_app_body = (
        'schema = 1\n'
        'id = "github-app"\n'
        'name = "Work GitHub App"\n'
        'service = "github"\n'
        'driver = "github"\n'
        f'account = {json.dumps(acct_gh_app["id"])}\n'
        'enabled = true\n'
        f'endpoint = {json.dumps(gh_app_url)}\n'
        '[options]\n'
        'app_id = "4242"\n'
        'installation_id = "9901"\n'
        '[secrets]\n'
        'app_private_key = "secret:connector:github-app/GITHUB_APP_PRIVATE_KEY"\n'
    )
    jira_a_body = (
        'schema = 1\n'
        'id = "jira-prod"\n'
        'name = "Prod Jira"\n'
        'service = "jira"\n'
        'driver = "jira"\n'
        f'account = {json.dumps(acct_jira_a["id"])}\n'
        'enabled = true\n'
        f'endpoint = {json.dumps(jira_a_url)}\n'
        '[options]\n'
        'email = "prod-bot@example.com"\n'
        'project = "PROD"\n'
        '[secrets]\n'
        'api_token = "secret:connector:jira-prod/JIRA_API_TOKEN"\n'
    )

    for cid, body in (("github-work", gh_work_body), ("github-app", gh_app_body), ("jira-prod", jira_a_body)):
        chk, _ = req("POST", f"/api/v1/config/connector/{cid}/check", {"body": body}, headers={"Idempotency-Key": f"chk-{cid}"})
        assert chk["report"]["ok"] is True and chk["report"]["gate"] == "C", chk
        dr, _ = req("PUT", f"/api/v1/config/connector/{cid}/draft", {"body": body, "why": f"create {cid}"}, headers={"Idempotency-Key": f"dr-{cid}"}, expected=201)
        assert dr["draft"]["state"] == "valid" and dr["draft"]["gate"] == "C", dr
        act, _ = req("POST", f"/api/v1/config/connector/{cid}/activate", {"draft": dr["draft"]["id"], "base_version": 0}, headers={"Idempotency-Key": f"act-{cid}"})
        assert act["version"] == 1 and act["draft"]["state"] == "active", act
        rd, _ = req("GET", f"/api/v1/config/connector/{cid}")
        assert rd["item"]["version"] == 1 and rd["item"]["level"] == "config", rd
        vw, _ = req("GET", f"/api/v1/connectors/{cid}")
        assert vw["connector"]["status"] == "ready" and vw["connector"]["usable"] is True, vw

    # 3. Execute real GitHub and Jira drivers against fake transports and prove exact account & endpoint!
    gh_c = gh.Client.from_connector("github-work")
    assert gh_c.viewer()["login"] == "gh-work-bot"
    assert gh_c.issues("acme/app")[0]["title"] == "from-work-connector"
    assert gh_hits[0] == ("/user", f"Bearer {gh_tok_val}"), gh_hits

    gh_app_c = gh.Client.from_connector("github-app")
    assert gh_app_c.viewer()["slug"] == "arete-app"
    minted = gh_app_c.clone_token("acme/app")
    assert minted == "minted-app-clone-token-9901"
    assert gh_app_hits[0][0] == "GET" and gh_app_hits[0][2].startswith("Bearer ") and gh_app_hits[0][2].count(".") == 2
    assert gh_app_hits[1][0] == "POST" and gh_app_hits[1][1] == "/app/installations/9901/access_tokens"

    jira_c = jira.Client.from_connector("jira-prod")
    assert jira_c.me()["accountId"] == "jira-acct-a"
    assert jira_c.search("project = PROD", ["summary"])[0]["key"] == "A-1"
    expected_auth_a = "Basic " + base64.b64encode(f"prod-bot@example.com:{jira_a_val}".encode()).decode()
    assert jira_a_hits[0] == ("/rest/api/3/myself", expected_auth_a), jira_a_hits

    # 4. Edit jira-prod to switch to second account & endpoint B (version 1 -> 2), verify versions & diff!
    jira_b_body = (
        'schema = 1\n'
        'id = "jira-prod"\n'
        'name = "Prod Jira B"\n'
        'service = "jira"\n'
        'driver = "jira"\n'
        f'account = {json.dumps(acct_jira_b["id"])}\n'
        'enabled = true\n'
        f'endpoint = {json.dumps(jira_b_url)}\n'
        '[options]\n'
        'email = "stage-bot@example.com"\n'
        'project = "STG"\n'
        '[secrets]\n'
        'api_token = "secret:global/JIRA_STAGE_TOKEN"\n'
    )
    dr_edit, _ = req("PUT", "/api/v1/config/connector/jira-prod/draft", {"body": jira_b_body, "why": "switch to site B"}, headers={"Idempotency-Key": "dr-jira-b"}, expected=201)
    act_edit, _ = req("POST", "/api/v1/config/connector/jira-prod/activate", {"draft": dr_edit["draft"]["id"], "base_version": 1}, headers={"Idempotency-Key": "act-jira-b"})
    assert act_edit["version"] == 2, act_edit
    vers, _ = req("GET", "/api/v1/config/connector/jira-prod/versions")
    assert len(vers["versions"]) == 2 and [e["version"] for e in vers["entries"]] == [2, 1], vers
    diff_res, _ = req("GET", "/api/v1/config/connector/jira-prod/diff?from=1&to=2")
    assert jira_b_url in diff_res["diff"] and jira_a_url in diff_res["diff"], diff_res

    jira_c2 = jira.Client.from_connector("jira-prod")
    assert jira_c2.me()["accountId"] == "jira-acct-b"
    expected_auth_b = "Basic " + base64.b64encode(f"stage-bot@example.com:{jira_b_val}".encode()).decode()
    assert jira_b_hits[0] == ("/rest/api/3/myself", expected_auth_b), jira_b_hits

    # 5. Disable jira-prod (enabled = false -> Gate B, non-reach edit) and verify fail-closed (no default fallback!).
    jira_disabled_body = jira_b_body.replace("enabled = true", "enabled = false")
    dr_dis, _ = req("PUT", "/api/v1/config/connector/jira-prod/draft", {"body": jira_disabled_body, "why": "disable"}, headers={"Idempotency-Key": "dr-jira-dis"}, expected=201)
    assert dr_dis["draft"]["gate"] == "B", dr_dis
    act_dis, _ = req("POST", "/api/v1/config/connector/jira-prod/activate", {"draft": dr_dis["draft"]["id"], "base_version": 2}, headers={"Idempotency-Key": "act-jira-dis"})
    assert act_dis["version"] == 3, act_dis

    disabled_failed = False
    try:
        jira.Client.from_connector("jira-prod")
    except connectors.ConnectorError as err:
        disabled_failed = "disabled" in str(err)
    assert disabled_failed, "disabled connector must fail closed"

    # Explicit unknown or wrong-service connectors also fail closed (never take default).
    for fn in (
        lambda: jira.Client.from_connector("jira-missing"),
        lambda: jira.Client.from_connector("github-work"),
        lambda: gh.Client.from_connector("gh-missing"),
        lambda: gh.Client.from_connector("jira-prod"),
    ):
        try:
            fn()
            raise AssertionError("expected ConnectorError")
        except connectors.ConnectorError:
            pass

    # 6. Compatibility implicit/default Jira execution still uses [jira] + global/JIRA_API_TOKEN without credential copy.
    def_jira_1 = jira.Client.from_connector(None)
    def_jira_2 = jira.Client.from_connector("jira")
    assert def_jira_1.me()["accountId"] == "jira-acct-def"
    assert def_jira_2.me()["accountId"] == "jira-acct-def"
    expected_def_auth = "Basic " + base64.b64encode(f"default-jira@example.com:{jira_def_val}".encode()).decode()
    assert jira_default_hits[0] == ("/rest/api/3/myself", expected_def_auth), jira_default_hits

    # 7. Ensure NO secret value appears in any API response or SQLite artifact.
    all_views = json.dumps([
        req("GET", "/api/v1/connectors")[0],
        req("GET", "/api/v1/accounts/status")[0],
        req("GET", "/api/v1/accounts")[0],
        req("GET", "/api/v1/config/connector/github-work")[0],
        req("GET", "/api/v1/config/connector/github-app")[0],
        req("GET", "/api/v1/config/connector/jira-prod")[0],
        req("GET", "/api/v1/drafts")[0],
    ])
    for secret_val in (gh_tok_val, jira_a_val, jira_b_val, jira_def_val, "BEGIN RSA PRIVATE KEY", "BEGIN PRIVATE KEY"):
        assert secret_val not in all_views, f"leaked secret {secret_val!r} in API responses"

    print("RESULT=" + json.dumps({
        "gh_hits": len(gh_hits),
        "gh_app_hits": len(gh_app_hits),
        "jira_a_hits": len(jira_a_hits),
        "jira_b_hits": len(jira_b_hits),
        "jira_default_hits": len(jira_default_hits),
    }))
finally:
    server.shutdown()
    srv_thread.join()
    server.server_close()
    for srv, th in ((gh_srv, gh_th), (gh_app_srv, gh_app_th), (jira_a_srv, jira_a_th), (jira_b_srv, jira_b_th), (jira_def_srv, jira_def_th)):
        srv.shutdown()
        th.join()
        srv.server_close()
""")
        self.assertGreaterEqual(result["gh_hits"], 2)
        self.assertGreaterEqual(result["gh_app_hits"], 2)
        self.assertGreaterEqual(result["jira_a_hits"], 2)
        self.assertGreaterEqual(result["jira_b_hits"], 1)
        self.assertGreaterEqual(result["jira_default_hits"], 2)


class ConnectorSecurityGatesAndRefusalsTests(unittest.TestCase):
    class_timeout = 30

    def test_refusals_stale_versions_idempotency_gates_and_activation_recheck(self):
        result = _run_isolated(r"""
import http.client
import json
import os
import sys
import threading
from pathlib import Path
from unittest import mock

from arete import (
    accounts,
    configrepo,
    dashboard,
    drafts,
    paths,
    principals,
    secretstore,
    tailnet,
    util,
)
from arete.store import credentials as cred_store
from arete.store import db
from arete.store import tokens as token_store

root = Path(sys.argv[1])
store_dir = root / "pve-secrets"
store_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(store_dir, 0o700)

db.migrate()
owner = principals.seed_owner("owner@example.com")
with db.tx() as c:
    c.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('multi_user', 'ready')")
    c.execute(
        "INSERT INTO principals (id, kind, role, name, status, created) VALUES (?, 'human', 'member', 'Member', 'active', ?)",
        ("u:22222222", util.iso()),
    )
    c.execute(
        "INSERT INTO principals (id, kind, role, name, status, created) VALUES (?, 'human', 'member', 'Other', 'active', ?)",
        ("u:33333333", util.iso()),
    )

paths.CONFIG_FILE.write_text(
    'alert_repo = "acme/alerts"\n'
    "[secrets]\n"
    'store = "pve_store"\n'
    f'store_dir = {json.dumps(str(store_dir))}\n',
    encoding="utf-8",
)
configrepo.init()

gh_secret_val = "synthetic-gh-secret-value-98765"
jira_secret_val = "synthetic-jira-secret-value-54321"
other_secret_val = "synthetic-other-personal-secret-777"

gh_row = secretstore.put("global", "GH_WORK_TOKEN", gh_secret_val, "api_key", owner, provider="github")
jira_row = secretstore.put("global", "JIRA_WORK_TOKEN", jira_secret_val, "api_key", owner, provider="jira")
other_row = secretstore.put("global", "OTHER_GH_TOKEN", other_secret_val, "api_key", owner, provider="github")

acct_gh = accounts.record_workspace(gh_row, "github", "token")
acct_jira = accounts.record_workspace(jira_row, "jira", "api_key")

with db.tx() as c:
    c.execute("UPDATE accounts SET usage_scope = ? WHERE id = ?", ("https://github.acme.example/api/v3", acct_gh["id"]))
    c.execute("UPDATE accounts SET usage_scope = ? WHERE id = ?", ("https://acme.atlassian.net", acct_jira["id"]))
    cred_store.save_account(
        c, "github:other-personal", "github", "token", "u:33333333", "personal",
        "id-other-personal", "fp-other-personal", other_row["id"],
    )
    cred_store.save_account(
        c, "anthropic:sub", "anthropic", "subscription", owner, "personal",
        "id-sub", "fp-sub", None,
    )
    token_store.ensure_key(c)
    _, owner_session = token_store.issue(
        owner, "owner-session", ["*"], kind="session", expires=token_store.session_lifetime(util.now())
    )
    _, owner_bearer = token_store.issue(owner, "owner-bearer", ["read", "config:draft", "config:activate"])
    _, draft_only_bearer = token_store.issue(owner, "draft-only", ["read", "config:draft"])
    _, read_only_bearer = token_store.issue(owner, "read-only", ["read"])
    _, member_bearer = token_store.issue("u:22222222", "member-bearer", ["read", "config:draft", "config:activate"])

class QuietHandler(dashboard.Handler):
    def log_request(self, *_):
        pass

    def admit(self, method, path):
        self.server.tls = object()
        try:
            return super().admit(method, path)
        finally:
            self.server.tls = None

address = "127.0.0.1"
node = tailnet.Node((address,), frozenset({address}), address)
class _TestLogins(tailnet.Logins):
    def of(self, _peer):
        return "owner@example.com"


logins = _TestLogins(mock.Mock())

with mock.patch("socket.getfqdn", return_value=address):
    server = tailnet.Server(address, 0, node, logins, False, QuietHandler, dashboard.Redirect)
    server.daemon_threads = False
    srv_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    srv_thread.start()

try:
    sess_hdr = {"Cookie": f"__Host-arete_session={owner_session}", "X-Arete": "1"}
    tok_hdr = {"Authorization": f"Bearer {owner_bearer}"}
    draft_only_hdr = {"Authorization": f"Bearer {draft_only_bearer}"}
    read_only_hdr = {"Authorization": f"Bearer {read_only_bearer}"}
    member_hdr = {"Authorization": f"Bearer {member_bearer}"}

    def req(method, path, body=None, *, headers=None, expected=200):
        conn = http.client.HTTPConnection(address, server.server_port, timeout=10)
        try:
            hdrs = {"Content-Type": "application/json", **(headers or {})}
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            data = json.loads(raw) if raw else {}
            assert resp.status == expected, (method, path, resp.status, data)
            return data, resp
        finally:
            conn.close()

    def state_snapshot():
        conn_dir = paths.CONFIG_DIR / "connectors"
        files = sorted(p.name for p in conn_dir.glob("*")) if conn_dir.is_dir() else []
        with db.reader() as c:
            idx = [dict(r) for r in c.execute("SELECT kind, name, ver, version FROM config_index WHERE kind = 'connector' ORDER BY ver")]
            drs = [dict(r) for r in c.execute("SELECT id, name, state, version FROM config_drafts WHERE kind = 'connector' ORDER BY id")]
            idem = c.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) FROM events_log").fetchone()[0]
        return {"files": files, "idx": idx, "drs": drs, "idem": idem, "seq": seq}

    # 1. Refuse unsupported service, wrong tenant/account, unknown fields, literal credentials,
    #    protected default overwrite, wrong caller authority with ZERO state changes.
    snap0 = state_snapshot()
    refusal_bodies = [
        ("slack-1", 'schema = 1\nid = "slack-1"\nservice = "slack"\naccount = "github:token"\n', sess_hdr),
        ("gh-bad-tenant", f'schema = 1\nid = "gh-bad-tenant"\nservice = "github"\naccount = {json.dumps(acct_gh["id"])}\nendpoint = "https://github.other.example/api/v3"\n', sess_hdr),
        ("jira-wrong-acct", f'schema = 1\nid = "jira-wrong-acct"\nservice = "jira"\naccount = {json.dumps(acct_gh["id"])}\nendpoint = "https://acme.atlassian.net"\n[options]\nemail = "a@b.com"\n', sess_hdr),
        ("gh-sub-acct", 'schema = 1\nid = "gh-sub-acct"\nservice = "github"\naccount = "anthropic:sub"\n', sess_hdr),
        ("gh-unknown", f'schema = 1\nid = "gh-unknown"\nservice = "github"\naccount = {json.dumps(acct_gh["id"])}\nextra = 1\n', sess_hdr),
        ("gh-lit-pat", f'schema = 1\nid = "gh-lit-pat"\nservice = "github"\naccount = {json.dumps(acct_gh["id"])}\n[secrets]\napi_token = "ghp_1234567890abcdef1234567890"\n', sess_hdr),
        ("gh-lit-stored", f'schema = 1\nid = "gh-lit-stored"\nname = {json.dumps(gh_secret_val)}\nservice = "github"\naccount = {json.dumps(acct_gh["id"])}\nendpoint = "https://github.acme.example/api/v3"\n', sess_hdr),
        ("github", f'schema = 1\nid = "github"\nservice = "github"\naccount = {json.dumps(acct_gh["id"])}\nendpoint = "https://github.acme.example/api/v3"\n', sess_hdr),
        ("gh-other-pers", 'schema = 1\nid = "gh-other-pers"\nservice = "github"\naccount = "github:other-personal"\n', sess_hdr),
    ]
    for idx_i, (cid, body, hdr) in enumerate(refusal_bodies):
        err, _ = req("PUT", f"/api/v1/config/connector/{cid}/draft", {"body": body}, headers={**hdr, "Idempotency-Key": f"refuse-{idx_i}"}, expected=422)
        assert err["code"] == "validation", (cid, err)
        assert gh_secret_val not in json.dumps(err), "error message must not echo secret"
    # Non-owner member is refused both by HTTP scope gate (403) and connector caller-authority validation!
    member_body = f'schema = 1\nid = "gh-member-ws"\nservice = "github"\naccount = {json.dumps(acct_gh["id"])}\nendpoint = "https://github.acme.example/api/v3"\n'
    mem_err, _ = req("PUT", "/api/v1/config/connector/gh-member-ws/draft", {"body": member_body}, headers={**member_hdr, "Idempotency-Key": "refuse-member"}, expected=403)
    assert mem_err["code"] == "scope_missing", mem_err
    from arete import connectors
    try:
        connectors.validate_connector("gh-member-ws", {"schema": 1, "id": "gh-member-ws", "service": "github", "account": acct_gh["id"], "endpoint": "https://github.acme.example/api/v3"}, caller="u:22222222")
        raise AssertionError("expected caller authority refusal for non-owner member")
    except ValueError as err:
        assert "lacks owner authority" in str(err), err
    assert state_snapshot() == snap0, (state_snapshot(), snap0)

    # Verify literal credential values never entered SQLite artifacts.
    with db.reader() as c:
        dump = json.dumps([
            [dict(r) for r in c.execute("SELECT * FROM config_drafts")],
            [dict(r) for r in c.execute("SELECT * FROM events_log")],
            [dict(r) for r in c.execute("SELECT * FROM idempotency")],
        ], default=str)
    for leaked in ("ghp_1234567890abcdef1234567890", gh_secret_val, jira_secret_val, other_secret_val):
        assert leaked not in dump, f"credential leaked into SQLite: {leaked!r}"

    # 2. Scope refusals: read-only token cannot draft; draft-only token cannot activate.
    valid_gh_body = (
        'schema = 1\n'
        'id = "gh-acme"\n'
        'name = "Acme GitHub"\n'
        'service = "github"\n'
        f'account = {json.dumps(acct_gh["id"])}\n'
        'enabled = true\n'
        'endpoint = "https://github.acme.example/api/v3"\n'
        '[secrets]\n'
        'api_token = "secret:global/GH_WORK_TOKEN"\n'
    )
    req("PUT", "/api/v1/config/connector/gh-acme/draft", {"body": valid_gh_body}, headers={**read_only_hdr, "Idempotency-Key": "ro-draft"}, expected=403)
    dr_res, _ = req("PUT", "/api/v1/config/connector/gh-acme/draft", {"body": valid_gh_body, "why": "create gh-acme"}, headers={**tok_hdr, "Idempotency-Key": "dr-gh-acme"}, expected=201)
    draft_id = dr_res["draft"]["id"]
    assert dr_res["draft"]["gate"] == "C" and dr_res["draft"]["state"] == "valid"

    req("POST", "/api/v1/config/connector/gh-acme/activate", {"draft": draft_id, "base_version": 0}, headers={**draft_only_hdr, "Idempotency-Key": "do-act"}, expected=403)

    # 3. Stale base_version refusal -> 409 version_conflict with zero live/event/receipt changes.
    snap_before_stale = state_snapshot()
    stale_err, _ = req("POST", "/api/v1/config/connector/gh-acme/activate", {"draft": draft_id, "base_version": 99}, headers={**sess_hdr, "Idempotency-Key": "act-stale-1"}, expected=409)
    assert stale_err["code"] == "version_conflict", stale_err
    assert state_snapshot() == snap_before_stale

    # 4. Gate C presence refusals & approvals:
    #    - Missing Idempotency-Key returns 400 invalid_input with zero state changes.
    #    - Direct activation without presence_passed raises drafts.PresenceRequired with zero state changes.
    #    - Bearer token with Idempotency-Key returns 202 awaiting_presence; owner decline keeps it unactivated;
    #      owner approval activates it!
    missing_key_err, _ = req("POST", "/api/v1/config/connector/gh-acme/activate", {"draft": draft_id, "base_version": 0}, headers=tok_hdr, expected=400)
    assert missing_key_err["code"] == "invalid_input", missing_key_err
    assert state_snapshot() == snap_before_stale

    try:
        drafts.activate(draft_id, owner, 0, presence_passed=False)
        raise AssertionError("expected PresenceRequired")
    except drafts.PresenceRequired:
        pass
    assert state_snapshot() == snap_before_stale

    pend_rej, _ = req("POST", "/api/v1/config/connector/gh-acme/activate", {"draft": draft_id, "base_version": 0}, headers={**tok_hdr, "Idempotency-Key": "act-gh-acme-rej"}, expected=202)
    assert pend_rej["state"] == "awaiting_presence", pend_rej
    rej_item = req("GET", pend_rej["watch"], headers=sess_hdr)[0]["item"]
    req("POST", f"/api/v1/inbox/{rej_item['id']}/resolve", {"verb": "reject", "base_version": rej_item["version"]}, headers={**sess_hdr, "Idempotency-Key": "reject-gh-acme"})
    assert not (paths.CONFIG_DIR / "connectors" / "gh-acme.toml").exists()

    pend, _ = req("POST", "/api/v1/config/connector/gh-acme/activate", {"draft": draft_id, "base_version": 0}, headers={**tok_hdr, "Idempotency-Key": "act-gh-acme-pres"}, expected=202)
    assert pend["state"] == "awaiting_presence", pend
    pres_item = req("GET", pend["watch"], headers=sess_hdr)[0]["item"]
    req("POST", f"/api/v1/inbox/{pres_item['id']}/resolve", {"verb": "approve", "base_version": pres_item["version"]}, headers={**sess_hdr, "Idempotency-Key": "approve-gh-acme"})
    assert (paths.CONFIG_DIR / "connectors" / "gh-acme.toml").is_file()
    assert req("GET", "/api/v1/config/connector/gh-acme", headers=sess_hdr)[0]["item"]["version"] == 1

    # 5. Idempotent retry of activation returns exact same commit/version; reused key with different body returns 422.
    valid_jira_body = (
        'schema = 1\n'
        'id = "jira-acme"\n'
        'name = "Acme Jira"\n'
        'service = "jira"\n'
        f'account = {json.dumps(acct_jira["id"])}\n'
        'enabled = true\n'
        'endpoint = "https://acme.atlassian.net"\n'
        '[options]\n'
        'email = "dev@acme.example"\n'
        '[secrets]\n'
        'api_token = "secret:global/JIRA_WORK_TOKEN"\n'
    )
    dr_j, _ = req("PUT", "/api/v1/config/connector/jira-acme/draft", {"body": valid_jira_body}, headers={**sess_hdr, "Idempotency-Key": "dr-jira-acme"}, expected=201)
    act_j1, _ = req("POST", "/api/v1/config/connector/jira-acme/activate", {"draft": dr_j["draft"]["id"], "base_version": 0}, headers={**sess_hdr, "Idempotency-Key": "act-jira-idem"})
    act_j2, _ = req("POST", "/api/v1/config/connector/jira-acme/activate", {"draft": dr_j["draft"]["id"], "base_version": 0}, headers={**sess_hdr, "Idempotency-Key": "act-jira-idem"})
    assert act_j1["commit"] == act_j2["commit"] and act_j1["version"] == act_j2["version"] == 1
    reused_err, _ = req("POST", "/api/v1/config/connector/jira-acme/activate", {"draft": dr_j["draft"]["id"], "base_version": 1}, headers={**sess_hdr, "Idempotency-Key": "act-jira-idem"}, expected=422)
    assert reused_err["code"] == "idempotency_key_reused", reused_err

    # 6. Recheck account compatibility at activation under instance mutation lock BEFORE SQLite writes:
    #    Create valid draft, then remove secret from store -> activate refuses with ZERO live/version/receipt/event changes!
    valid_jira_2 = (
        'schema = 1\n'
        'id = "jira-recheck"\n'
        'service = "jira"\n'
        f'account = {json.dumps(acct_jira["id"])}\n'
        'enabled = true\n'
        'endpoint = "https://acme.atlassian.net"\n'
        '[options]\n'
        'email = "dev@acme.example"\n'
        '[secrets]\n'
        'api_token = "secret:global/JIRA_WORK_TOKEN"\n'
    )
    dr_rc, _ = req("PUT", "/api/v1/config/connector/jira-recheck/draft", {"body": valid_jira_2}, headers={**sess_hdr, "Idempotency-Key": "dr-jira-rc"}, expected=201)
    secretstore.remove("global", "JIRA_WORK_TOKEN", owner)
    snap_before_rc = state_snapshot()
    rc_err, _ = req("POST", "/api/v1/config/connector/jira-recheck/activate", {"draft": dr_rc["draft"]["id"], "base_version": 0}, headers={**sess_hdr, "Idempotency-Key": "act-jira-rc"}, expected=422)
    assert rc_err["code"] == "validation", rc_err
    assert state_snapshot() == snap_before_rc, (state_snapshot(), snap_before_rc)

    # 7. Run actor cannot activate.
    try:
        drafts.activate(dr_rc["draft"]["id"], "run:pip-1", 0, presence_passed=True)
        raise AssertionError("expected forbidden")
    except Exception as err:
        assert getattr(err, "code", "") == "forbidden"

    print("RESULT=" + json.dumps({"ok": True}))
finally:
    server.shutdown()
    srv_thread.join()
    server.server_close()
""")
        self.assertTrue(result["ok"])
