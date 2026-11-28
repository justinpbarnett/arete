from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arete import api_check

ROOT = Path(__file__).resolve().parent.parent

_PREAMBLE = r"""
import hashlib, hmac, http.client, json, os, re, signal, socket, sqlite3, subprocess, sys, threading, time
from contextlib import contextmanager, redirect_stdout
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import urllib.request

from arete import (
    accounts, api, api_check, cli, client, config, configcheck, configrepo, control,
    credential_files, dashboard, drafts, events, gh, inbox, intake_rules, intake_service,
    jira, loops, paths, principals, record, runner, secretstore, serve, sources, tailnet,
    tasks, util, webhooks,
)
from arete.store import (
    blobs as blob_store, db, inbox as inbox_store, intake as intake_store,
    loops as loop_store, runs as run_store, sources as source_store,
    tasks as task_store, tokens as token_store,
)

def _forbid(*a, **kw):
    raise RuntimeError("external transport forbidden in intake test")

mock.patch.object(urllib.request, "urlopen", _forbid).start()
mock.patch.object(accounts, "read", _forbid).start()
mock.patch.object(gh, "gh", _forbid).start()

WORK = Path.cwd()

def emit(val):
    print("BEHAVIOR_RESULT=" + json.dumps(val), flush=True)

def write_loop(name, body):
    d = paths.CONFIG_DIR / "loops" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "loop.toml").write_text(body.strip() + "\n", encoding="utf-8")

def append_jsonl(path, rows):
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

@contextmanager
def live_dashboard(login="owner@example.test", hook_port=None):
    logins = tailnet.Logins(mock.Mock())
    logins.of = lambda _: login
    with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    srv.node = SimpleNamespace(names={"127.0.0.1"})
    srv.logins = logins
    srv.tls = object()
    srv.pending_secrets = {}
    if hook_port is not None:
        srv.hook_port = hook_port
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        yield srv, srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)

@contextmanager
def live_hook_listener():
    with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
        srv = dashboard.open_hook_server(0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        yield srv, srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)

def http_req(port, method, path, *, token=None, body=None, raw_body=None, key=None, extra_headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    if extra_headers:
        headers.update(extra_headers)
    payload = None
    if raw_body is not None:
        payload = raw_body
        headers.setdefault("Content-Type", "application/json")
        headers["Content-Length"] = str(len(payload))
    elif body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
        headers["Content-Length"] = str(len(payload))
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = list(resp.getheaders())
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", "replace")
        return resp.status, resp_headers, parsed, raw
    finally:
        conn.close()
"""


def _run_child(script: str, *, replay_dir: Path | None = None, env_extra: dict[str, str] | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-intake-") as tmp:
        root = Path(tmp).resolve()
        env = api_check.isolated_env(root)
        env["ARETE_TEST_FAST_SYNC"] = "1"
        env["ARETE_DRIVE_FLUSH"] = "0"
        rep = replay_dir or (root / "replay")
        rep.mkdir(parents=True, exist_ok=True)
        env["ARETE_REPLAY_DIR"] = str(rep)
        if env_extra:
            env.update(env_extra)
        code = _PREAMBLE + "\n" + script
        proc = subprocess.run(
            [sys.executable, "-IB", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\n{code}"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"child exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("BEHAVIOR_RESULT="):
                return json.loads(line.removeprefix("BEHAVIOR_RESULT="))
        raise AssertionError(f"no BEHAVIOR_RESULT in stdout:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


class IntakePollAndContractTests(unittest.TestCase):
    class_timeout = 30

    def test_sigkill_rollback_locked_retry_poll_crashed_and_refusal_batch(self):
        res = _run_child(r"""
paths.ensure_dirs()
paths.CONFIG_FILE.write_text("[replay]\nall = true\n")
db.migrate()
configrepo.init()
principals.seed_owner("owner@example.test")

# 1. SIGKILL inside enqueue rolls back cursor, cursor version, and intake_items;
#    post-commit SIGKILL leaves no duplicates on next poll.
log_file = WORK / "app.log"
log_file.write_text("INFO boot\n", encoding="utf-8")
write_loop("watchlog", f'''
kind = "command"
[run]
command = "true"
[[source]]
id = "app-log"
type = "log"
path = "{log_file}"
pattern = "ERROR"
every = "5m"
''')

cfg = config.load()
src = next(s for s in sources.load_all(cfg) if s.id == "app-log")
sources.poll(src, cfg)

before_cur = source_store.cursor("app-log")
with db.reader() as c:
    before_items = c.execute("SELECT COUNT(*) FROM intake_items WHERE source = 'app-log'").fetchone()[0]

with open(log_file, "a", encoding="utf-8") as fh:
    fh.write("ERROR alpha failed\nERROR beta failed\n")

kill_child = subprocess.run(
    [
        sys.executable, "-IB", "-c",
        "\n".join([
            f"import os, signal, sys",
            f"sys.path.insert(0, {str(Path(sources.__file__).resolve().parent.parent)!r})",
            "from unittest import mock",
            "from arete import config, sources",
            "from arete.store import loops as ls",
            "cfg = config.load()",
            "src = next(s for s in sources.load_all(cfg) if s.id == 'app-log')",
            "def boom(*a, **kw): os.kill(os.getpid(), signal.SIGKILL)",
            "with mock.patch.object(ls, 'enqueue', side_effect=boom): sources.poll(src, cfg)",
        ]),
    ],
    cwd=WORK,
    env=os.environ,
    capture_output=True,
    text=True,
    timeout=15,
)
assert kill_child.returncode == -signal.SIGKILL, (kill_child.returncode, kill_child.stderr)
# Recover SQLite WAL after writer SIGKILL (as arete serve does on restart) before read-only checks.
with db.tx() as _c:
    _c.execute("SELECT 1").fetchone()

after_kill_cur = source_store.cursor("app-log")
assert after_kill_cur["value"] == before_cur["value"]
assert after_kill_cur["version"] == before_cur["version"]
with db.reader() as c:
    assert c.execute("SELECT COUNT(*) FROM intake_items WHERE source = 'app-log'").fetchone()[0] == before_items
    assert loop_store.pending() == []

# Next child commits then kills itself right after commit.
commit_kill = subprocess.run(
    [
        sys.executable, "-IB", "-c",
        (
            f"import os, signal, sys; sys.path.insert(0, {str(Path(sources.__file__).resolve().parent.parent)!r}); "
            "from arete import config, sources; "
            "cfg = config.load(); src = next(s for s in sources.load_all(cfg) if s.id == 'app-log'); "
            "sources.poll(src, cfg); os.kill(os.getpid(), signal.SIGKILL)"
        ),
    ],
    cwd=WORK,
    env=os.environ,
    capture_output=True,
    text=True,
    timeout=15,
)
assert commit_kill.returncode == -signal.SIGKILL, commit_kill.returncode
with db.tx() as _c:
    _c.execute("SELECT 1").fetchone()
pending1 = loop_store.pending()
assert len(pending1) == 1
assert len(json.loads(pending1[0]["payload"])) == 2

# Poll again after post-commit kill adds no intake_items and no queue item.
with db.reader() as c:
    items_after_commit = c.execute("SELECT COUNT(*) FROM intake_items WHERE source = 'app-log'").fetchone()[0]
more_ev, more_q = sources.poll(src, cfg)
assert more_ev == [] and more_q == []
with db.reader() as c:
    assert c.execute("SELECT COUNT(*) FROM intake_items WHERE source = 'app-log'").fetchone()[0] == items_after_commit
assert len(loop_store.pending()) == 1

# 2. OperationalError('database is locked') once in enqueue leaves cursor/version/intake_items unchanged,
#    writes 1 source_polls(ok=0) + 1 serve.log line, and next poll queues once.
with open(log_file, "a", encoding="utf-8") as fh:
    fh.write("ERROR gamma locked\n")

cur_before_lock = source_store.cursor("app-log")
with db.reader() as c:
    items_before_lock = c.execute("SELECT COUNT(*) FROM intake_items WHERE source = 'app-log'").fetchone()[0]
    polls_before_lock = c.execute("SELECT COUNT(*) FROM source_polls WHERE source = 'app-log'").fetchone()[0]

srv = serve.Server()
serve_log = []
srv.say = serve_log.append
srv.load()
src_live = next(s for s in sources.load_all(srv.cfg, srv.live) if s.id == "app-log")
with mock.patch.object(loop_store, "enqueue", side_effect=sqlite3.OperationalError("database is locked")):
    srv.poll_source(src_live)

cur_after_lock = source_store.cursor("app-log")
assert cur_after_lock["value"] == cur_before_lock["value"]
assert cur_after_lock["version"] == cur_before_lock["version"]
with db.reader() as c:
    assert c.execute("SELECT COUNT(*) FROM intake_items WHERE source = 'app-log'").fetchone()[0] == items_before_lock
    polls_after = c.execute("SELECT ok, error FROM source_polls WHERE source = 'app-log' ORDER BY id").fetchall()
assert len(polls_after) == polls_before_lock + 1
assert polls_after[-1]["ok"] == 0 and "database is locked" in (polls_after[-1]["error"] or "")
lock_lines = [ln for ln in serve_log if "database is locked" in ln]
assert len(lock_lines) == 1, serve_log

# Clear existing waiting item (queue_one_waiting) and poll cleanly: queues once.
with db.tx() as c:
    c.execute("DELETE FROM queue")
srv.poll_source(src_live)
assert len(loop_store.pending()) == 1

# 3. Both intake tx AND error path raising -> exactly one "poll crashed" line, and next Server.poll starts a new poll.
with open(log_file, "a", encoding="utf-8") as fh:
    fh.write("ERROR delta crash\n")
srv.tried.clear()
srv.polls.clear()
crash_log = []
srv.say = crash_log.append
with (
    mock.patch.object(loop_store, "enqueue", side_effect=RuntimeError("tx boom")),
    mock.patch.object(source_store, "record_poll_error", side_effect=RuntimeError("err path boom")),
):
    srv.poll()
    fut1 = srv.polls["app-log"]
    try:
        fut1.result(timeout=5)
    except Exception:
        pass
    # Wait briefly for done callback to finish
    deadline = time.monotonic() + 2.0
    while not any("poll crashed" in ln for ln in crash_log) and time.monotonic() < deadline:
        time.sleep(0.01)

crashed_lines = [ln for ln in crash_log if "app-log: poll crashed:" in ln]
assert len(crashed_lines) == 1, crash_log

# Next Server.poll starts a new poll cleanly.
with db.tx() as c:
    c.execute("DELETE FROM queue")
srv.poll()
fut2 = srv.polls["app-log"]
assert fut2 is not fut1
fut2.result(timeout=5)
assert len(loop_store.pending()) == 1

# 4. Three new events: group_of raises for one key -> refused with error text, other two queued;
#    list data on trigger with group="repo" -> grp == "";
#    unaccepted event type -> stored refused with refusal text.
cmd_file = WORK / "events.jsonl"
append_jsonl(cmd_file, [{"key": "base", "type": "custom", "title": "Base"}])
write_loop("grouper", f'''
kind = "command"
[run]
command = "true"
[trigger]
event = ["custom"]
group = "repo"
[[source]]
id = "cmd-grp"
type = "command"
command = "cat {cmd_file}"
every = "5m"
''')
cfg = config.load()
all_loops = loops.load_all(cfg)
src_grp = next(s for s in sources.load_all(cfg, all_loops) if s.id == "cmd-grp")
sources.poll(src_grp, cfg)

with db.tx() as c:
    c.execute("DELETE FROM queue")

append_jsonl(cmd_file, [
    {"key": "e-ok-1", "type": "custom", "title": "OK 1", "data": {"repo": "r1"}},
    {"key": "e-boom", "type": "custom", "title": "Boom", "data": {"repo": "r2"}},
    {"key": "e-ok-2", "type": "custom", "title": "OK 2", "data": {"repo": "r3"}},
    {"key": "e-bad-type", "type": "other", "title": "Wrong type"},
])
orig_group_of = loops.Trigger.group_of
def flaky_group_of(self, ev):
    if ev.get("key") == "e-boom":
        raise ValueError("bad group key")
    return orig_group_of(self, ev)

with mock.patch.object(loops.Trigger, "group_of", flaky_group_of):
    sources.poll(src_grp, cfg)

with db.reader() as c:
    rows = {
        json.loads(r["raw"])["key"]: dict(r)
        for r in c.execute("SELECT * FROM intake_items WHERE source = 'cmd-grp'").fetchall()
        if r["raw"] and r["raw"].startswith("{")
    }
assert rows["e-boom"]["status"] == "refused" and "bad group key" in (rows["e-boom"]["refuse_reason"] or "")
assert rows["e-bad-type"]["status"] == "refused" and "custom" in (rows["e-bad-type"]["refuse_reason"] or "")
assert rows["e-ok-1"]["status"] == "new" and rows["e-ok-2"]["status"] == "new"
pending_grps = {r["grp"] for r in loop_store.pending()}
assert pending_grps == {"r1", "r3"}, pending_grps

# Event with list data queues under grp == "".
list_ev = sources.event("custom", "cmd-grp", "e-list", "List data", trust="internal")
list_ev["data"] = ["not", "a", "dict"]
with db.tx() as c:
    c.execute("DELETE FROM queue")
with mock.patch.object(sources.Command, "fetch", return_value=([list_ev], None)):
    sources.poll(src_grp, cfg)
pending_list = loop_store.pending()
assert len(pending_list) == 1 and pending_list[0]["grp"] == "", pending_list

# Verify large raw (> 256 KB) goes to blobs table and passes CHECK constraint.
big_ev = sources.event("custom", "cmd-grp", "e-big", "Big payload", trust="untrusted", data={"blob": "x" * 270000})
with mock.patch.object(sources.Command, "fetch", return_value=([big_ev], None)):
    sources.poll(src_grp, cfg)
with db.reader() as c:
    big_row = c.execute(
        "SELECT raw, raw_blob, raw_sha256, quarantined FROM intake_items WHERE source = 'cmd-grp' ORDER BY id DESC LIMIT 1"
    ).fetchone()
assert big_row["raw"] is None and big_row["raw_blob"] == big_row["raw_sha256"] and big_row["quarantined"] == 1
assert blob_store.read(big_row["raw_blob"]) is not None

emit({"ok": True})
""")
        self.assertTrue(res["ok"])

    def test_seen_contract_backfill_and_source_scan(self):
        res = _run_child(r"""
paths.ensure_dirs()
paths.CONFIG_FILE.write_text("[replay]\nall = true\n")
cmd_file = WORK / "legacy.jsonl"
append_jsonl(cmd_file, [{"key": "old-1", "title": "Old 1"}, {"key": "new-1", "title": "New 1"}])
write_loop("legacy", f'''
kind = "command"
[run]
command = "true"
[[source]]
id = "leg-src"
type = "command"
command = "cat {cmd_file}"
every = "5m"
''')

# Migrate only through m0071 (before intake expand/backfill/contract).
db.migrate(through=db.SOURCES_BACKFILL_SCHEMA)
old_ts = "2026-11-20T10:00:00Z"
with db.tx() as c:
    assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='seen'").fetchone() is not None
    c.execute("INSERT INTO cursors (source, value, polled, ok, detail, last_ok, scope_sha, version) VALUES ('leg-src', NULL, ?, 1, 'ok', ?, ?, 1)", (old_ts, old_ts, sources.load_all(config.load())[0].scope_sha))
    c.execute('INSERT INTO "seen" (source, key, ts) VALUES (?, ?, ?)', ("leg-src", "old-1", old_ts))
    c.execute('INSERT INTO "seen" (source, key, ts) VALUES (?, ?, ?)', ("unknown-src", "mystery-9", old_ts))

# Now migrate to LATEST (applies m0072 expand, m0073 backfill, m0074 contract).
db.migrate()
with db.reader() as c:
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.LATEST
    mig_rows = {r["n"]: r["phase"] for r in c.execute("SELECT n, phase FROM schema_migrations WHERE n >= ?", (db.INTAKE_EXPAND_SCHEMA,))}
    assert mig_rows[db.INTAKE_EXPAND_SCHEMA] == "expand"
    assert mig_rows[db.INTAKE_BACKFILL_SCHEMA] == "backfill"
    assert mig_rows[db.INTAKE_SEEN_CONTRACT_SCHEMA] == "contract"
    # seen table is gone!
    assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='seen'").fetchone() is None
    # Backfilled rows exist as dismissed with original received time
    leg_row = dict(c.execute("SELECT * FROM intake_items WHERE source = 'leg-src'").fetchone())
    assert leg_row["status"] == "dismissed" and leg_row["received"] == old_ts and leg_row["raw"] == "{}"
    unk_row = dict(c.execute("SELECT * FROM intake_items WHERE source = 'unknown-src'").fetchone())
    assert unk_row["status"] == "dismissed" and unk_row["external_key"].startswith("legacy-unresolved:")

# Polling leg-src must NOT refire old-1; only new-1 queues.
cfg = config.load()
src_leg = next(s for s in sources.load_all(cfg) if s.id == "leg-src")
ev, q = sources.poll(src_leg, cfg)
assert [e["key"] for e in ev] == ["new-1"] and len(q) == 1

# Source scan of arete/ finds no seen_keys, mark_seen, claim_seen, or FROM seen.
pkg_dir = Path(sources.__file__).resolve().parent
banned = ("seen_keys", "mark_seen", "claim_seen", "FROM seen")
hits = []
for py in sorted(pkg_dir.rglob("*.py")):
    text = py.read_text(encoding="utf-8")
    for token in banned:
        if token in text:
            hits.append((str(py.relative_to(pkg_dir)), token))
assert hits == [], f"forbidden legacy seen tokens found: {hits}"

emit({"ok": True})
""")
        self.assertTrue(res["ok"])


class IntakeWebhookAndListenerTests(unittest.TestCase):
    class_timeout = 30

    def test_webhook_transaction_concurrency_window_and_hook_listener(self):
        res = _run_child(r"""
paths.ensure_dirs()
pve_dir = WORK / "pve-secrets"
pve_dir.mkdir(mode=0o700, exist_ok=True)
pve_dir.chmod(0o700)
paths.CONFIG_FILE.write_text(
    f'[replay]\nall = true\n[secrets]\nstore = "pve_store"\nstore_dir = "{pve_dir}"\n[dashboard]\nhook_port = 0\n'
)
db.migrate()
configrepo.init()
principals.seed_owner("owner@example.test")
token_store.init_owner()

write_loop("whloop", '''
kind = "command"
[run]
command = "true"
[trigger]
webhook = true
event = ["github.issues", "webhook"]
''')
secret = webhooks.rotate("whloop", "hand")

# 1. Queue write raising answers 500 and rolls back; redelivery with same X-GitHub-Delivery answers 202 and queues once.
body1 = json.dumps({"action": "opened", "title": "First"}).encode("utf-8")
sig1 = sign(secret, body1)
with live_hook_listener() as (_, hook_port):
    with mock.patch.object(loop_store, "enqueue", side_effect=RuntimeError("queue failed")):
        st500, _, _, _ = http_req(
            hook_port, "POST", "/hook/whloop", raw_body=body1,
            extra_headers={"X-Hub-Signature-256": sig1, "X-GitHub-Delivery": "deliv-1", "X-GitHub-Event": "issues"},
        )
    assert st500 == 500, st500
    assert loop_store.pending() == []
    with db.reader() as c:
        assert c.execute("SELECT COUNT(*) FROM intake_items WHERE external_key = 'deliv-1'").fetchone()[0] == 0

    st202, _, parsed202, _ = http_req(
        hook_port, "POST", "/hook/whloop", raw_body=body1,
        extra_headers={"X-Hub-Signature-256": sig1, "X-GitHub-Delivery": "deliv-1", "X-GitHub-Event": "issues"},
    )
    assert st202 == 202 and parsed202["queued"] is True, (st202, parsed202)
    assert len(loop_store.pending()) == 1

    # Same delivery id with different body answers 200 and adds no row.
    body_diff = json.dumps({"action": "edited", "title": "Different"}).encode("utf-8")
    st200, _, parsed200, _ = http_req(
        hook_port, "POST", "/hook/whloop", raw_body=body_diff,
        extra_headers={"X-Hub-Signature-256": sign(secret, body_diff), "X-GitHub-Delivery": "deliv-1", "X-GitHub-Event": "issues"},
    )
    assert st200 == 200 and parsed200["queued"] is False, (st200, parsed200)
    with db.reader() as c:
        assert c.execute("SELECT COUNT(*) FROM intake_items WHERE external_key = 'deliv-1'").fetchone()[0] == 1

    # 2. Two threads posting one delivery id at once get one 202 and one 200, with one queue row.
    with db.tx() as c:
        c.execute("DELETE FROM queue")
    body_race = json.dumps({"action": "opened", "title": "Race"}).encode("utf-8")
    sig_race = sign(secret, body_race)
    barrier = threading.Barrier(2)
    statuses = []
    def _post_race():
        barrier.wait(timeout=5)
        st, _, _, _ = http_req(
            hook_port, "POST", "/hook/whloop", raw_body=body_race,
            extra_headers={"X-Hub-Signature-256": sig_race, "X-GitHub-Delivery": "deliv-race", "X-GitHub-Event": "issues"},
        )
        statuses.append(st)
    t1 = threading.Thread(target=_post_race)
    t2 = threading.Thread(target=_post_race)
    t1.start(); t2.start(); t1.join(timeout=5); t2.join(timeout=5)
    assert sorted(statuses) == [200, 202], statuses
    assert len(loop_store.pending()) == 1

    # 3. Body with no delivery id header queues once; moving first row's received back 11 minutes lets it queue again.
    with db.tx() as c:
        c.execute("DELETE FROM queue")
    body_noid = json.dumps({"title": "No delivery id"}).encode("utf-8")
    sig_noid = sign(secret, body_noid)
    st_a, _, p_a, _ = http_req(hook_port, "POST", "/hook/whloop", raw_body=body_noid, extra_headers={"X-Hub-Signature-256": sig_noid})
    st_b, _, p_b, _ = http_req(hook_port, "POST", "/hook/whloop", raw_body=body_noid, extra_headers={"X-Hub-Signature-256": sig_noid})
    assert st_a == 202 and p_a["queued"] is True and st_b == 200 and p_b["queued"] is False
    assert len(loop_store.pending()) == 1

    old_11m = util.iso(util.now() - timedelta(minutes=11))
    with db.tx() as c:
        c.execute("DELETE FROM queue")
        c.execute(
            "UPDATE intake_items SET received = ? WHERE source = 'whloop#webhook' AND external_key LIKE 'sha256:%'",
            (old_11m,),
        )
    st_c, _, p_c, _ = http_req(hook_port, "POST", "/hook/whloop", raw_body=body_noid, extra_headers={"X-Hub-Signature-256": sig_noid})
    assert st_c == 202 and p_c["queued"] is True
    assert len(loop_store.pending()) == 1

    # 4. Refused event stores refused row with reason and /hook/{name} answers 403.
    st_ref, _, _, _ = http_req(
        hook_port, "POST", "/hook/whloop", raw_body=body1,
        extra_headers={"X-Hub-Signature-256": sig1, "X-GitHub-Delivery": "deliv-refused", "X-GitHub-Event": "push"},
    )
    assert st_ref == 403, st_ref
    with db.reader() as c:
        ref_row = dict(c.execute("SELECT * FROM intake_items WHERE external_key = 'deliv-refused'").fetchone())
    assert ref_row["status"] == "refused" and "github.push" in (ref_row["refuse_reason"] or "")

    # 5. On hook listener: POST /api/v1/hooks/{source} answers 202 with empty body and NO Arete-* headers
    #    for new post and repeat alike!
    v1_body = json.dumps({"title": "Canonical hook"}).encode("utf-8")
    v1_headers = {"X-Hub-Signature-256": sign(secret, v1_body), "X-GitHub-Delivery": "deliv-v1-1"}
    st_v1_1, hdrs_v1_1, _, raw_v1_1 = http_req(hook_port, "POST", "/api/v1/hooks/whloop", raw_body=v1_body, extra_headers=v1_headers)
    st_v1_2, hdrs_v1_2, _, raw_v1_2 = http_req(hook_port, "POST", "/api/v1/hooks/whloop", raw_body=v1_body, extra_headers=v1_headers)
    assert st_v1_1 == 202 and raw_v1_1 == b"", (st_v1_1, raw_v1_1)
    assert st_v1_2 == 202 and raw_v1_2 == b"", (st_v1_2, raw_v1_2)
    for hdrs in (hdrs_v1_1, hdrs_v1_2):
        arete_hdrs = [k for k, _ in hdrs if "arete" in k.lower()]
        assert arete_hdrs == [], arete_hdrs

    # 6. With hook_port set, tailnet listener answers 404 at /hook/{name}, and `arete webhook` prints hook address.
    paths.CONFIG_FILE.write_text(
        f'[replay]\nall = true\n[secrets]\nstore = "pve_store"\nstore_dir = "{pve_dir}"\n[dashboard]\nhook_port = {hook_port}\n'
    )
    with live_dashboard(hook_port=hook_port) as (_, dash_port):
        st_404, _, _, _ = http_req(
            dash_port, "POST", "/hook/whloop", raw_body=v1_body, extra_headers=v1_headers,
        )
        assert st_404 == 404, st_404

    buf = StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["webhook", "whloop"])
    assert rc == 0 and f"http://127.0.0.1:{hook_port}/hook/whloop" in buf.getvalue(), buf.getvalue()

emit({"ok": True})
""")
        self.assertTrue(res["ok"])


class IntakeApiAndRulesTests(unittest.TestCase):
    class_timeout = 30

    def test_intake_api_accept_idempotency_span_append_rollback_and_task_sources(self):
        res = _run_child(r"""
paths.ensure_dirs()
repo_dir = WORK / "app-repo"
repo_dir.mkdir()
subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, env=os.environ, check=True)
(repo_dir / "README.md").write_text("app\n", encoding="utf-8")
subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@e", "add", "."], cwd=repo_dir, env=os.environ, check=True)
subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-qm", "init"], cwd=repo_dir, env=os.environ, check=True)

paths.CONFIG_FILE.write_text(f'''
[replay]
all = true
[[repo]]
name = "app"
path = "{repo_dir}"
tracker = "local"
''')
db.migrate()
configrepo.init()
principals.seed_owner("owner@example.test")
token_store.init_owner()
token = credential_files.read_client(db.schema_metadata()["instance_tag"])

transcript = (
    "Alice: First, fix the login timeout on mobile. "
    "Bob: Second, add CSV export to billing. "
    "Carol: Third, audit webhook retries. "
    "Dave: Also note that mobile login timeout happens on iOS 18."
)
q1 = (transcript.index("fix the login"), transcript.index("mobile.") + len("mobile."))
q2 = (transcript.index("add CSV"), transcript.index("billing.") + len("billing."))
q3 = (transcript.index("audit webhook"), transcript.index("retries.") + len("retries."))
q4 = (transcript.index("mobile login timeout"), transcript.index("iOS 18.") + len("iOS 18."))

with live_dashboard() as (_, port):
    # 1. intake.add
    st_add, _, body_add, _ = http_req(
        port, "POST", "/api/v1/intake",
        token=token, key="intake-add-1",
        body={"text": transcript, "kind": "transcript", "title": "Weekly sync"},
    )
    assert st_add == 201, (st_add, body_add)
    item = body_add["item"]
    item_id = item["id"]
    item_num = item_id.removeprefix("IN-")
    assert item["source"] == "intake"
    assert item["external_key"] == hashlib.sha256(transcript.encode()).hexdigest()
    assert item["trust"] == "untrusted" and item["quarantined"] is True

    # Write replay trace for intake-<id>-extract.json with 4 candidates (3 create + 1 link).
    replay_dir = Path(os.environ["ARETE_REPLAY_DIR"])
    (replay_dir / f"intake-{item_num}-extract.json").write_text(json.dumps({
        "ok": True,
        "cost_usd": 0,
        "tokens_in": 10,
        "tokens_out": 20,
        "turns": 1,
        "seconds": 0,
        "structured": {
            "candidates": [
                {"kind": "fix", "action": "create", "title": "Fix mobile login timeout",
                 "quote_start": q1[0], "quote_end": q1[1], "fields": {"summary": "Login times out on mobile", "repo": "app"},
                 "match_task": None, "match_confidence": 0.92},
                {"kind": "feat", "action": "create", "title": "Add CSV export to billing",
                 "quote_start": q2[0], "quote_end": q2[1], "fields": {"summary": "Billing CSV export", "repo": "app"},
                 "match_task": None, "match_confidence": 0.88},
                {"kind": "chore", "action": "create", "title": "Audit webhook retries",
                 "quote_start": q3[0], "quote_end": q3[1], "fields": {"summary": "Webhook retry audit", "repo": "app"},
                 "match_task": None, "match_confidence": 0.85},
                {"kind": "fix", "action": "link", "title": "iOS 18 login timeout detail",
                 "quote_start": q4[0], "quote_end": q4[1], "fields": {"summary": "Reproduces on iOS 18"},
                 "match_task": None, "match_confidence": 0.9},
            ],
        },
    }), encoding="utf-8")

    # 2. intake.extract: spy runner.run to verify tools == []
    seen_tools = []
    orig_run = runner.run
    def spy_run(*a, **kw):
        seen_tools.append(kw.get("tools"))
        return orig_run(*a, **kw)

    with mock.patch.object(runner, "run", side_effect=spy_run):
        st_ex, _, body_ex, _ = http_req(
            port, "POST", f"/api/v1/intake/{item_id}/extract",
            token=token, key="extract-1", body={}, extra_headers={"Prefer": "wait=5"},
        )
    assert st_ex == 200, (st_ex, body_ex)
    assert seen_tools == [[]], seen_tools
    assert body_ex["item"]["status"] == "extracted"
    assert len(body_ex["candidates"]) == 4
    assert all(c["state"] == "proposed" for c in body_ex["candidates"])
    ver_after_extract = body_ex["item"]["version"]

    # 3. Atomic rollback check: fault after first TaskStore.create rolls back everything!
    orig_create = task_store.TaskStore.create
    create_calls = [0]
    def fail_second_create(*a, **kw):
        create_calls[0] += 1
        if create_calls[0] == 2:
            raise RuntimeError("injected fault after first task")
        return orig_create(*a, **kw)

    with mock.patch.object(task_store.TaskStore, "create", side_effect=fail_second_create):
        st_fault, _, _, _ = http_req(
            port, "POST", f"/api/v1/intake/{item_id}/accept",
            token=token, key="accept-fault-1",
            body={
                "base_version": ver_after_extract,
                "candidates": [{"n": 1, "action": "create"}, {"n": 2, "action": "create"}],
            },
        )
    assert st_fault == 500, st_fault
    with db.reader() as c:
        assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM task_refs").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM idempotency WHERE key = 'accept-fault-1'").fetchone()[0] == 0

    # 4. intake.accept with candidates 1, 2, 3 as create -> 3 tasks, 1 refs row, 3 task_refs rows (role=origin)
    accept_body = {
        "base_version": ver_after_extract,
        "candidates": [
            {"n": 1, "action": "create"},
            {"n": 2, "action": "create"},
            {"n": 3, "action": "create"},
        ],
    }
    st_acc, _, body_acc, _ = http_req(
        port, "POST", f"/api/v1/intake/{item_id}/accept",
        token=token, key="accept-main-1", body=accept_body,
    )
    assert st_acc == 200, (st_acc, body_acc)
    assert len(body_acc["tasks"]) == 3 and len(body_acc["links"]) == 3
    assert body_acc["ref"]["system"] == "intake" and body_acc["ref"]["key"] == item_id
    assert body_acc["ref"]["intake_item"] == item_id
    assert all(lnk["role"] == "origin" and len(lnk["anchor"]) == 1 for lnk in body_acc["links"])
    task1_id = body_acc["tasks"][0]["id"]
    ver_after_accept = body_acc["item"]["version"]

    # task.get, screens/task, and tasks.view all show sources and Task.intake!
    for t in body_acc["tasks"]:
        st_tg, _, body_tg, _ = http_req(port, "GET", f"/api/v1/tasks/{t['id']}", token=token)
        assert st_tg == 200
        assert item_id in body_tg["task"]["intake"], body_tg["task"]
        assert len(body_tg["sources"]) == 1 and body_tg["sources"][0]["role"] == "origin"
        st_ts, _, body_ts, _ = http_req(port, "GET", f"/api/v1/screens/task?task={t['id']}", token=token)
        assert st_ts == 200 and item_id in body_ts["task"]["intake"] and len(body_ts["sources"]) == 1
        tv = tasks.view(t["id"])
        assert len(tv["sources"]) == 1 and item_id in tv["intake"]

    # 5. Same accept with same Idempotency-Key returns stored answer.
    st_rep, _, body_rep, _ = http_req(
        port, "POST", f"/api/v1/intake/{item_id}/accept",
        token=token, key="accept-main-1", body=accept_body,
    )
    assert st_rep == 200 and [t["id"] for t in body_rep["tasks"]] == [t["id"] for t in body_acc["tasks"]]

    # 6. New accept of candidate 1 returns existing link without bumping version.
    st_re1, _, body_re1, _ = http_req(
        port, "POST", f"/api/v1/intake/{item_id}/accept",
        token=token, key="accept-re-1",
        body={"base_version": ver_after_accept, "candidates": [{"n": 1, "action": "create"}]},
    )
    assert st_re1 == 200 and body_re1["links"][0]["version"] == 1

    # 7. Fourth candidate linked to first task appends a second span to anchor at version 2,
    #    with one task_ref.changed and no second task_refs row.
    st_acc4, _, body_acc4, _ = http_req(
        port, "POST", f"/api/v1/intake/{item_id}/accept",
        token=token, key="accept-4",
        body={
            "base_version": body_re1["item"]["version"],
            "candidates": [{"n": 4, "action": "link", "task": task1_id}],
        },
    )
    assert st_acc4 == 200, (st_acc4, body_acc4)
    lnk4 = body_acc4["links"][0]
    assert lnk4["version"] == 2 and len(lnk4["anchor"]) == 2
    assert lnk4["intake_item"] == item_id and lnk4["intake_n"] == 1
    with db.reader() as c:
        t1_num = task_store.parse_task_num(task1_id, c)
        assert c.execute("SELECT COUNT(*) FROM task_refs WHERE task_id = ?", (t1_num,)).fetchone()[0] == 1
        ev_rows = c.execute(
            "SELECT kind, ver FROM events_log WHERE subject = ? AND kind = 'task_ref.changed' ORDER BY seq",
            (f"task_ref:{lnk4['id']}",),
        ).fetchall()
    assert [r["ver"] for r in ev_rows] == [1, 2], [dict(r) for r in ev_rows]

    # 8. Stale base_version answers 409 version_conflict.
    st_stale, _, body_stale, _ = http_req(
        port, "POST", f"/api/v1/intake/{item_id}/accept",
        token=token, key="accept-stale",
        body={"base_version": 1, "candidates": [{"n": 1, "action": "create"}]},
    )
    assert st_stale == 409 and body_stale["code"] == "version_conflict", (st_stale, body_stale)

    # 9. intake.list, intake.get, intake.history, and intake.dismiss
    st_ls, _, body_ls, _ = http_req(port, "GET", "/api/v1/intake", token=token)
    assert st_ls == 200 and any(it["id"] == item_id for it in body_ls["items"])
    st_hist, _, body_hist, _ = http_req(port, "GET", f"/api/v1/intake/{item_id}/history", token=token)
    assert st_hist == 200
    kinds = [e["kind"] for e in body_hist["events"]]
    assert "intake.created" in kinds and "intake.extracted" in kinds and "intake.changed" in kinds, kinds

    st_d_add, _, body_d_add, _ = http_req(
        port, "POST", "/api/v1/intake", token=token, key="add-dismiss",
        body={"text": "Noise message", "kind": "paste"},
    )
    d_id = body_d_add["item"]["id"]
    st_dis, _, body_dis, _ = http_req(
        port, "POST", f"/api/v1/intake/{d_id}/dismiss", token=token, key="dismiss-1",
        body={"base_version": body_d_add["item"]["version"], "reason": "noise"},
    )
    assert st_dis == 200 and body_dis["item"]["status"] == "dismissed"

emit({"ok": True})
""")
        self.assertTrue(res["ok"])

    def test_source_specific_intake_rules_config_workflow_and_no_restate_on_edit(self):
        res = _run_child(r"""
paths.ensure_dirs()
repo_a = WORK / "repo-a"
repo_b = WORK / "repo-b"
for d in (repo_a, repo_b):
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, env=os.environ, check=True)
    (d / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@e", "add", "."], cwd=d, env=os.environ, check=True)
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-qm", "init"], cwd=d, env=os.environ, check=True)

paths.CONFIG_FILE.write_text(f'''
[replay]
all = true
[[repo]]
name = "alpha"
path = "{repo_a}"
tracker = "local"
[[repo]]
name = "beta"
path = "{repo_b}"
tracker = "local"
''')
(paths.CONFIG_DIR / "sources").mkdir(parents=True, exist_ok=True)
(paths.CONFIG_DIR / "sources" / "src-alpha.toml").write_text('type = "command"\ncommand = "true"\n')
(paths.CONFIG_DIR / "sources" / "src-beta.toml").write_text('type = "command"\ncommand = "true"\n')

db.migrate()
configrepo.init()
principals.seed_owner("owner@example.test")
token_store.init_owner()
token = credential_files.read_client(db.schema_metadata()["instance_tag"])

rule_alpha_v1 = '''
domain = "intake"
sources = ["src-alpha"]
kinds = ["transcript"]
candidate_kinds = ["fix", "feat"]
candidate_actions = ["create", "link"]
[defaults]
repo = "alpha"
kind = "fix"
priority = 1
labels = ["alpha-team"]
[task_permissions]
allow_create = true
allow_link = true
repos = ["alpha"]
'''

rule_beta_v1 = '''
domain = "intake"
sources = ["src-beta"]
kinds = ["transcript"]
candidate_kinds = ["feat", "research"]
candidate_actions = ["create", "link"]
[defaults]
repo = "beta"
kind = "research"
priority = 3
labels = ["beta-team"]
[task_permissions]
allow_create = true
allow_link = true
repos = ["beta"]
'''

with live_dashboard() as (_, port):
    # Reject unknown fields (e.g. tools / auto_accept) and unknown source refs through real config.check!
    st_bad, _, body_bad, _ = http_req(
        port, "POST", "/api/v1/config/rule/bad-rule/check",
        token=token, key="chk-bad-1",
        body={"body": 'domain = "intake"\nsources = ["src-alpha"]\ntools = ["Bash"]\n'},
    )
    assert st_bad == 200 and body_bad["report"]["ok"] is False, (st_bad, body_bad)

    st_bad_src, _, body_bad_src, _ = http_req(
        port, "POST", "/api/v1/config/rule/bad-src/check",
        token=token, key="chk-bad-2",
        body={"body": 'domain = "intake"\nsources = ["no-such-source"]\n'},
    )
    assert st_bad_src == 200 and body_bad_src["report"]["ok"] is False, (st_bad_src, body_bad_src)

    sess_cred = token_store.Credential(
        "sess1", "u:owner", "owner", "u:owner", "session", frozenset({"*"}), "", "", "", None,
    )

    # Draft, check, and activate rule-alpha and rule-beta through real #296 config routes!
    def draft_check_activate(rule_name, body_text, base_ver=None):
        st_r, _, body_r, _ = http_req(port, "GET", f"/api/v1/config/rule/{rule_name}", token=token)
        if base_ver is None:
            base_ver = body_r["item"]["version"] if st_r == 200 else 0
        st_d, _, body_d, _ = http_req(
            port, "PUT", f"/api/v1/config/rule/{rule_name}/draft",
            token=token, key=f"draft-{rule_name}-{hashlib.sha256(body_text.encode()).hexdigest()[:6]}",
            body={"body": body_text, "base_version": base_ver, "why": f"configure {rule_name}"},
        )
        assert st_d in (200, 201), (st_d, body_d)
        draft_id = body_d["draft"]["id"]
        st_c, _, body_c, _ = http_req(
            port, "POST", f"/api/v1/config/rule/{rule_name}/check",
            token=token, key=f"check-{draft_id}",
            body={"draft": draft_id},
        )
        assert st_c == 200 and body_c["report"]["ok"] is True, (st_c, body_c)
        st_a, _, body_a, _ = http_req(
            port, "POST", f"/api/v1/config/rule/{rule_name}/activate",
            token=token, key=f"act-{draft_id}",
            body={"draft": draft_id, "base_version": base_ver},
        )
        if st_a == 202 and body_a.get("state") == "awaiting_presence":
            st_w, _, body_w, _ = http_req(port, "GET", body_a["watch"], token=token)
            assert st_w == 200, (st_w, body_w)
            tr_item = body_w["item"]
            inbox.resolve(tr_item["id"], base_version=tr_item["version"], verb="approve", credential=sess_cred)
        else:
            assert st_a == 200, (st_a, body_a)
        st_after, _, body_after, _ = http_req(port, "GET", f"/api/v1/config/rule/{rule_name}", token=token)
        assert st_after == 200, (st_after, body_after)
        return body_after["item"]["version"], body_after["item"]["content_version"]

    num_alpha_1, ver_alpha_1 = draft_check_activate("rule-alpha", rule_alpha_v1)
    num_beta_1, ver_beta_1 = draft_check_activate("rule-beta", rule_beta_v1)

    # Add two intake items, one for src-alpha and one for src-beta.
    st_ia, _, b_ia, _ = http_req(
        port, "POST", "/api/v1/intake", token=token, key="add-alpha",
        body={"source": "src-alpha", "kind": "transcript", "text": "Alpha incident needs fix now."},
    )
    st_ib, _, b_ib, _ = http_req(
        port, "POST", "/api/v1/intake", token=token, key="add-beta",
        body={"source": "src-beta", "kind": "transcript", "text": "Beta roadmap question needs study."},
    )
    id_a, id_b = b_ia["item"]["id"], b_ib["item"]["id"]
    num_a, num_b = id_a.removeprefix("IN-"), id_b.removeprefix("IN-")

    replay_dir = Path(os.environ["ARETE_REPLAY_DIR"])
    # Same raw model output shape (leaving repo/priority/labels empty so pinned rule defaults differentiate them!).
    (replay_dir / f"intake-{num_a}-extract.json").write_text(json.dumps({
        "ok": True, "cost_usd": 0, "tokens_in": 5, "tokens_out": 5, "turns": 1, "seconds": 0,
        "structured": {"candidates": [{
            "kind": "bug", "action": "create", "title": "Fix alpha incident",
            "quote_start": 0, "quote_end": 28, "fields": {}, "match_task": None, "match_confidence": 0.8,
        }]},
    }), encoding="utf-8")
    (replay_dir / f"intake-{num_b}-extract.json").write_text(json.dumps({
        "ok": True, "cost_usd": 0, "tokens_in": 5, "tokens_out": 5, "turns": 1, "seconds": 0,
        "structured": {"candidates": [{
            "kind": "bug", "action": "create", "title": "Study beta roadmap",
            "quote_start": 0, "quote_end": 33, "fields": {}, "match_task": None, "match_confidence": 0.7,
        }]},
    }), encoding="utf-8")

    st_ea, _, b_ea, _ = http_req(
        port, "POST", f"/api/v1/intake/{id_a}/extract", token=token, key="ext-alpha",
        body={}, extra_headers={"Prefer": "wait=5"},
    )
    st_eb, _, b_eb, _ = http_req(
        port, "POST", f"/api/v1/intake/{id_b}/extract", token=token, key="ext-beta",
        body={}, extra_headers={"Prefer": "wait=5"},
    )
    assert st_ea == 200 and st_eb == 200, (st_ea, b_ea, st_eb, b_eb)
    assert b_ea["item"]["rule"] == "rule-alpha" and b_ea["item"]["rule_version"] == ver_alpha_1
    assert b_eb["item"]["rule"] == "rule-beta" and b_eb["item"]["rule_version"] == ver_beta_1
    assert b_ea["item"]["rule_version"] != b_eb["item"]["rule_version"]
    # Distinct rule-applied candidates:
    ca, cb = b_ea["candidates"][0], b_eb["candidates"][0]
    assert ca["kind"] == "fix" and ca["fields"]["repo"] == "alpha" and ca["fields"]["priority"] == 1
    assert cb["kind"] == "research" and cb["fields"]["repo"] == "beta" and cb["fields"]["priority"] == 3

    # Edit rule-alpha to a new version (priority = 5, labels = ["alpha-v2"]) and activate it.
    rule_alpha_v2 = rule_alpha_v1.replace("priority = 1", "priority = 5").replace("alpha-team", "alpha-v2")
    num_alpha_2, ver_alpha_2 = draft_check_activate("rule-alpha", rule_alpha_v2, base_ver=num_alpha_1)
    assert ver_alpha_2 != ver_alpha_1 and num_alpha_2 != num_alpha_1

    # Verify rule edit does NOT restate the already-extracted item (via get OR repeat extract)!
    st_ga, _, b_ga, _ = http_req(port, "GET", f"/api/v1/intake/{id_a}", token=token)
    assert st_ga == 200 and b_ga["item"]["rule_version"] == ver_alpha_1
    assert b_ga["candidates"][0]["fields"]["priority"] == 1
    st_ea2, _, b_ea2, _ = http_req(
        port, "POST", f"/api/v1/intake/{id_a}/extract", token=token, key="ext-alpha-2",
        body={}, extra_headers={"Prefer": "wait=5"},
    )
    assert st_ea2 == 200 and b_ea2["item"]["rule_version"] == ver_alpha_1
    assert b_ea2["candidates"][0]["fields"]["priority"] == 1

emit({"ok": True})
""")
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
