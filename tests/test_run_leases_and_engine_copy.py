from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from arete import api_check
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

RESULT_PREFIX = "BEHAVIOR_RESULT="


def _run_behavior_child(case: unittest.TestCase, body: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-lease-test-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp).resolve()
        env = api_check.isolated_env(root)
        env["ARETE_TEST_FAST_SYNC"] = "1"
        env["ARETE_DRIVE_FLUSH"] = "0"
        preamble = f"""
import json, os, shutil, signal, sqlite3, subprocess, sys, threading, time
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path({str(root)!r})
SOURCE = Path({str(api_check.SOURCE)!r})

def emit(payload):
    print({RESULT_PREFIX!r} + json.dumps(payload, sort_keys=True), flush=True)

def local_git(args, cwd, check=True):
    done = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, timeout=10, check=False,
        env={{**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}},
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"git {{args}}: {{done.stderr.strip()}}")
    return done.stdout
"""
        cmd = api_check.child_command(api_check.SOURCE, preamble + body)
        done = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=CHILD_HANG_TIMEOUT, check=False)
        case.assertEqual(done.returncode, 0, f"child exited {done.returncode}\nSTDOUT:\n{done.stdout}\nSTDERR:\n{done.stderr}")
        for line in reversed(done.stdout.splitlines()):
            if line.startswith(RESULT_PREFIX):
                return json.loads(line[len(RESULT_PREFIX):])
        case.fail(f"missing {RESULT_PREFIX} line\nSTDOUT:\n{done.stdout}\nSTDERR:\n{done.stderr}")


class RunForeignKeysAndPruneTests(unittest.TestCase):
    def test_foreign_keys_prune_doctor_and_one_live_migration(self):
        res = _run_behavior_child(self, r"""
from arete import (
    agent, api_check, auditor, config, control, credential_files, dashboard, leases,
    paths, principals, record, replay, retention as retention_mod, tailnet, util,
)
from arete.store import chats, db, inbox as inbox_store, insights as insights_store
from arete.store import loops as loop_store, retention as retention_store, runs as run_store, tasks as task_store, tokens

# 1. Seed a pre-m0076 DB (through=75) with two running rows on one lock, then migrate to LATEST (76).
sample = ROOT / "sample"
with mock.patch.object(util, "git", local_git):
    cfg_lines = replay.sample(sample, adw=True)
paths.CONFIG_FILE.write_text("max_parallel = 4\n" + "\n".join(cfg_lines) + "\n")
db.migrate(through=75)
owner = principals.seed_owner("owner@example.test")
tokens.init_owner()
with db.tx() as c:
    c.execute(
        "INSERT INTO runs (id, kind, status, lock, started, pid) VALUES "
        "('dup-old', 'loop', 'running', 'loop-dup', '2026-01-01T00:00:00Z', 999991), "
        "('dup-new', 'loop', 'running', 'loop-dup', '2026-01-01T00:01:00Z', 999992)"
    )
for dup_id in ("dup-old", "dup-new"):
    (paths.RUNS_DIR / dup_id).mkdir(parents=True, exist_ok=True)
waiting = db.migrate()
assert not waiting, waiting
with db.reader() as c:
    fk_on = c.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk_on == 1, fk_on
    old_status = c.execute("SELECT status FROM runs WHERE id = 'dup-old'").fetchone()[0]
    new_status = c.execute("SELECT status FROM runs WHERE id = 'dup-new'").fetchone()[0]
    assert old_status == "interrupted" and new_status == "running", (old_status, new_status)
    oldest = c.execute("SELECT value FROM schema_meta WHERE key = 'oldest_writer'").fetchone()[0]
    assert int(oldest) >= 76, oldest
    idx = c.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'runs_one_live'").fetchone()
    assert idx is not None and "WHERE status = 'running' AND lock IS NOT NULL" in idx[0], idx
record.settle("dup-new", "done")

# 2. Verify PRAGMA foreign_key_list on steps, feed, verdicts, evals.
fk_targets = {}
with db.reader() as c:
    for tbl in ("steps", "feed", "verdicts", "evals"):
        fk_targets[tbl] = sorted({row[2] for row in c.execute(f"PRAGMA foreign_key_list({tbl})")})
assert fk_targets == {
    "steps": ["chats", "runs"],
    "feed": ["runs"],
    "verdicts": ["runs"],
    "evals": ["runs"],
}, fk_targets

# 3. Missing run insert into steps fails on serve connection with FOREIGN KEY constraint failed.
missing_run_err = ""
try:
    with db.tx() as c:
        c.execute(
            "INSERT INTO steps (run_id, step, node, status, ok, ts) VALUES ('no-such-run', 'plan', 'plan', 'done', 1, ?)",
            (util.iso(),),
        )
except sqlite3.IntegrityError as exc:
    missing_run_err = str(exc)
assert "FOREIGN KEY constraint failed" in missing_run_err, missing_run_err

# 4. Chat turn still writes its steps row.
chat_id = chats.chat_add("hello")
chat_run = record.start("chat", tag=f"chat-{chat_id}")
record.step(chat_run, "chat", agent.Result(ok=True, runner="replay"), 0.01, chat_id=chat_id)
with db.reader() as c:
    chat_steps = c.execute("SELECT count(*) FROM steps WHERE run_id = ? AND chat_id = ?", (chat_run, chat_id)).fetchone()[0]
assert chat_steps == 1, chat_steps
record.settle(chat_run, "done")

# 5. Step against another payer's personal account is still refused by steps_payer_floor trigger.
payer_err = ""
with db.tx() as c:
    c.execute(
        "INSERT INTO accounts (id, provider, identity, mode, holder, usage, fingerprint, version, created) "
        "VALUES ('acct-other', 'anthropic', 'other@example.test', 'subscription', 'u:other123', 'personal', 'fp-other', 1, ?)",
        (util.iso(),),
    )
payer_run = record.start("loop", loop="payer-test", loop_payer="u:owner999")
try:
    with db.tx() as c:
        c.execute(
            "INSERT INTO steps (run_id, step, node, status, account_id, ok, ts) "
            "VALUES (?, 'step', 'step', 'done', 'acct-other', 1, ?)",
            (payer_run, util.iso()),
        )
except sqlite3.IntegrityError as exc:
    payer_err = str(exc)
assert "personal account" in payer_err, payer_err
record.settle(payer_run, "done")

# 6. Firing older than 14 days with steps, verdicts, and evals rows is pruned with no foreign key error.
old_ts = util.iso(util.now() - timedelta(days=20))
old_firing = record.start("loop", loop="old-loop", loop_kind="command", trigger="schedule")
record.step(old_firing, "work", agent.Result(ok=True, runner="replay"), 0.01)
run_store.add_feed(old_firing, "info", "old line")
insights_store.add_verdict(old_firing, "screen", "old-subject", "old about", {}, "")
insights_store.add_eval(old_firing, replay.SAMPLE_REPO, "task-1", True, 0.1, 0.0, "replay")
record.settle(old_firing, "done")
with db.tx() as c:
    c.execute("UPDATE runs SET started = ?, finished = ? WHERE id = ?", (old_ts, old_ts, old_firing))
pruned = retention_store.prune_rows(retention_mod.default_policy(), retention_store.measure())
with db.reader() as c:
    for tbl, col in (("runs", "id"), ("steps", "run_id"), ("feed", "run_id"), ("verdicts", "run_id"), ("evals", "run_id")):
        left = c.execute(f"SELECT count(*) FROM {tbl} WHERE {col} = ?", (old_firing,)).fetchone()[0]
        assert left == 0, (tbl, left)

# 7. arete doctor reports foreign_keys ok; planted orphan makes doctor exit 1 naming table and row, and auditor raises alert.
doc_ok = subprocess.run(
    api_check.child_command(SOURCE, "import sys; from arete.cli import main; sys.argv = ['arete', 'doctor']; raise SystemExit(main())"),
    cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=15, check=False,
)
assert doc_ok.returncode == 0 and "foreign_keys" in doc_ok.stdout and "ok" in doc_ok.stdout, (doc_ok.returncode, doc_ok.stdout, doc_ok.stderr)

raw_conn = sqlite3.connect(paths.DB_FILE)
try:
    raw_conn.execute("PRAGMA foreign_keys = OFF")
    cur = raw_conn.execute(
        "INSERT INTO steps (run_id, step, node, status, ok, ts) VALUES ('ghost-run', 'orphan', 'orphan', 'done', 1, ?)",
        (util.iso(),),
    )
    orphan_rowid = cur.lastrowid
    raw_conn.commit()
finally:
    raw_conn.close()

doc_bad = subprocess.run(
    api_check.child_command(SOURCE, "import sys; from arete.cli import main; sys.argv = ['arete', 'doctor']; raise SystemExit(main())"),
    cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=15, check=False,
)
assert doc_bad.returncode == 1, (doc_bad.returncode, doc_bad.stdout, doc_bad.stderr)
assert "steps" in doc_bad.stdout and str(orphan_rowid) in doc_bad.stdout, doc_bad.stdout

auditor.run(dry_run=False)
fk_alert = inbox_store.get_open_by_subject("alert:audit:doctor:foreign_keys")
assert fk_alert is not None, inbox_store.list_items()

raw_conn = sqlite3.connect(paths.DB_FILE)
try:
    raw_conn.execute("PRAGMA foreign_keys = OFF")
    raw_conn.execute("DELETE FROM steps WHERE rowid = ?", (orphan_rowid,))
    raw_conn.commit()
finally:
    raw_conn.close()

# 8. Live pipeline run + live loop run -> second start gets 409 naming live run with "already running"; no duplicate locks.
with principals.acting(owner):
    tid = task_store.task_add(replay.SAMPLE_REPO, "Live ticket", title="Live ticket")
task_num = task_store.parse_task_num(tid)
pip_lock = run_store.run_lock("pipeline", task=task_num)
live_pip = record.start("pipeline", repo=replay.SAMPLE_REPO, task_id=task_num, issue_key=tid,
                        lock=pip_lock, pid=os.getpid())

loop_dir = paths.CONFIG_DIR / "loops" / "alpha"
loop_dir.mkdir(parents=True, exist_ok=True)
(loop_dir / "loop.toml").write_text('kind = "command"\ndescription = "alpha"\nenabled = true\n[run]\ncommand = ["true"]\n')
loop_lock = loop_store.loop_lock("alpha")
live_loop = record.start("loop", loop="alpha", lock=loop_lock, pid=os.getpid())

token = credential_files.read_client(db.schema_metadata()["instance_tag"])
node = SimpleNamespace(names={"127.0.0.1"})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: "owner@example.test"
handler = type("H", (dashboard.handlers().open,), {"disable_nagle_algorithm": True})
with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
    server = tailnet.Server("127.0.0.1", 0, node, logins, False, handler, dashboard.Redirect)
with server:
    th = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    th.start()
    try:
        def post(path, body, key):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request("POST", path, json.dumps(body), {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Idempotency-Key": key,
                })
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())
            finally:
                conn.close()

        st_pip, body_pip = post(f"/api/v1/tasks/{tid}/run", {"repo": replay.SAMPLE_REPO}, "dup-pip-1")
        st_loop, body_loop = post("/api/v1/loops/alpha/start", {}, "dup-loop-1")
    finally:
        server.shutdown()
        th.join(timeout=3)

assert st_pip == 409 and live_pip in body_pip["detail"] and "already running" in body_pip["detail"], (st_pip, body_pip)
assert st_loop == 409 and live_loop in body_loop["detail"] and "already running" in body_loop["detail"], (st_loop, body_loop)

busy_err = ""
try:
    record.start("loop", loop="alpha", lock=loop_lock, pid=os.getpid())
except control.Busy as exc:
    busy_err = str(exc)
assert live_loop in busy_err and "already running" in busy_err, busy_err

with db.reader() as c:
    dups = c.execute(
        "SELECT lock, COUNT(*) FROM runs WHERE status = 'running' AND lock IS NOT NULL GROUP BY lock HAVING COUNT(*) > 1"
    ).fetchall()
assert dups == [], dups
record.settle(live_pip, "done")
record.settle(live_loop, "done")

emit({
    "ok": True,
    "fk_targets": fk_targets,
    "pruned": pruned.rows,
    "pip_status": st_pip,
    "loop_status": st_loop,
    "dups": len(dups),
})
""")
        self.assertTrue(res["ok"])
        self.assertEqual(res["pip_status"], 409)
        self.assertEqual(res["loop_status"], 409)
        self.assertEqual(res["dups"], 0)


class RunLeasesAndRoutesTests(unittest.TestCase):
    def test_all_run_kinds_have_leases_or_import_and_lease_routes_fence(self):
        res = _run_behavior_child(self, r"""
from arete import (
    codebase, credential_files, dashboard, leases, loop_run,
    paths, principals, record, replay, tailnet, util,
    worker_lease, worker_listener,
)
from arete.store import db, runs as run_store, tokens

now_box = [datetime.now(timezone.utc)]
util.set_clock(lambda: now_box[0])

sample = ROOT / "sample"
with mock.patch.object(util, "git", local_git):
    cfg_lines = replay.sample(sample, adw=True)
paths.CONFIG_FILE.write_text("max_parallel = 4\n" + "\n".join(cfg_lines) + "\n")
db.migrate()
owner = principals.seed_owner("owner@example.test")
tokens.init_owner()
codebase.load()

w_server, w_url = worker_listener.start(port=0, poll_interval=0.01)
token = credential_files.read_client(db.schema_metadata()["instance_tag"])
node = SimpleNamespace(names={"127.0.0.1"})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: "owner@example.test"
handler = type("H", (dashboard.handlers().open,), {"disable_nagle_algorithm": True})
with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
    t_server = tailnet.Server("127.0.0.1", 0, node, logins, False, handler, dashboard.Redirect)

def http_req(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Accept": "application/json", **(headers or {})}
    payload = None
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
        payload = json.dumps(body)
    try:
        conn.request(method, path, payload, hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()

try:
    with t_server:
        t_thread = threading.Thread(target=t_server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        t_thread.start()
        try:
            # 1. Replay ticket run via POST /api/tasks -> real child worker through replay runner.
            st, created = http_req(
                t_server.server_port, "POST", "/api/tasks",
                {"repo": replay.SAMPLE_REPO, "prompt": "Add kumquat", "title": "Kumquat", "kind": "feat"},
                {"Authorization": f"Bearer {token}", "Idempotency-Key": "lease-all-kinds-1"},
            )
            assert st == 200 and created.get("run"), (st, created)
            ticket_run = created["run"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                row = run_store.run_get(ticket_run)
                if row and row["status"] not in ("queued", "running"):
                    break
                time.sleep(0.05)
            row = run_store.run_get(ticket_run)
            assert row and row["status"] not in ("queued", "running"), row

            # 2. Loop run (detached real child worker).
            loop_dir = paths.CONFIG_DIR / "loops" / "echo-loop"
            loop_dir.mkdir(parents=True, exist_ok=True)
            (loop_dir / "loop.toml").write_text(
                'kind = "command"\ndescription = "echo"\nenabled = true\n[run]\ncommand = ["true"]\n'
            )
            with principals.acting(owner):
                l_row = loop_run.launch("echo-loop", detach=True, wait=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                cur = run_store.run_get(l_row["id"])
                if cur and cur["status"] not in ("queued", "running"):
                    break
                time.sleep(0.05)

            # 3. Scaffold draft run (lockless).
            draft_run = record.start("draft", trigger="manual", summary="lockless draft")
            record.settle(draft_run, "done")

            # 4. Offline benchmark dry run / run.import.
            imported_run = record.import_run(
                "benchmark", repo=replay.SAMPLE_REPO, status="done", summary="benchmark dry run",
            )

            with db.reader() as c:
                all_runs = [dict(r) for r in c.execute("SELECT id, kind, lease_id FROM runs").fetchall()]
                imported_ids = {
                    str(r["subject"]).removeprefix("run:")
                    for r in c.execute("SELECT subject FROM events_log WHERE kind = 'run.import'").fetchall()
                }
            assert len(all_runs) >= 4, all_runs
            for r in all_runs:
                assert r["lease_id"] is not None or r["id"] in imported_ids, r
            assert imported_run in imported_ids and run_store.run_get(imported_run)["lease_id"] is None

            # 5. Worker listener vs tailnet listener routes, unknown line_shape 400, hold/wake, 410 fencing, events_log actor/via.
            route_run = record.start("loop", loop="route-test", pid=os.getpid())
            route_row = run_store.run_get(route_run)
            lease_id = int(route_row["lease_id"])
            with db.tx() as c:
                claim_tok = leases.issue_claim_token(c, route_run, lease_id)

            st_unauth, _ = http_req(t_server.server_port, "POST", "/api/v1/leases", {"run_id": route_run})
            assert st_unauth == 401, st_unauth
            st_forbid, _ = http_req(
                t_server.server_port, "POST", "/api/v1/leases", {"run_id": route_run},
                {"Authorization": f"Bearer {token}"},
            )
            assert st_forbid == 403, st_forbid

            client = worker_lease.WorkerClient(w_url)
            try:
                client.claim(claim_tok, engine_sha=codebase.STAMP, line_shape="unknown-v99")
            except worker_lease.WorkerError as exc:
                assert exc.status == 400, exc
            else:
                raise AssertionError("expected 400 on unknown line_shape")

            claimed = client.claim(claim_tok, engine_sha=codebase.STAMP, line_shape="v1", node="build")
            assert claimed.get("token"), claimed
            lease_tok = claimed["token"]
            lease_hdr = claimed["header"]
            lease_label = claimed["lease"]["id"]

            st_w_rev, _ = http_req(
                w_server.server_port, "POST", f"/api/v1/leases/{lease_label}/revoke", {},
                {"Authorization": f"Bearer {lease_tok}", "X-Arete-Lease": lease_hdr},
            )
            assert st_w_rev == 403, st_w_rev

            hb_result = {}
            def do_hb():
                hb_result["res"] = client.heartbeat(lease_label, lease_tok, lease_hdr, hold_seconds=5.0, node="build")
            hb_t = threading.Thread(target=do_hb, daemon=True)
            hb_t.start()
            time.sleep(0.1)
            t0 = time.monotonic()
            st_rev, rev_body = http_req(
                t_server.server_port, "POST", f"/api/v1/leases/{lease_label}/revoke", {"reason": "operator"},
                {"Authorization": f"Bearer {token}", "Idempotency-Key": "revoke-lease-1"},
            )
            assert st_rev == 200 and rev_body["lease"]["outcome"] == "revoked", (st_rev, rev_body)
            hb_t.join(timeout=3)
            elapsed = time.monotonic() - t0
            assert not hb_t.is_alive() and elapsed < 2.0, elapsed
            hb_st, hb_body = hb_result["res"]
            assert hb_st == 410 and hb_body.get("code") == "lease_fenced", (hb_st, hb_body)

            st_rel_fenced, rel_fenced = client.release(lease_label, lease_tok, lease_hdr, node="build")
            assert st_rel_fenced == 410 and rel_fenced.get("code") == "lease_fenced", (st_rel_fenced, rel_fenced)

            with db.reader() as c:
                ev = c.execute(
                    "SELECT actor, via FROM events_log WHERE subject = ? AND via = ? ORDER BY seq DESC LIMIT 1",
                    (f"lease:{lease_id}", f"lease:{lease_id}"),
                ).fetchone()
            assert ev is not None and ev["actor"] == f"run:{route_run}/build" and ev["via"] == f"lease:{lease_id}", dict(ev) if ev else None

            exp_run = record.start("loop", loop="exp-test", pid=os.getpid())
            exp_lid = int(run_store.run_get(exp_run)["lease_id"])
            with db.tx() as c:
                exp_ctok = leases.issue_claim_token(c, exp_run, exp_lid)
            c2 = client.claim(exp_ctok, engine_sha=codebase.STAMP, line_shape="v1", node="plan")
            assert c2.get("token"), c2
            now_box[0] = now_box[0] + timedelta(seconds=leases.TTL_SECONDS + 5)
            st_exp, exp_body = client.heartbeat(c2["lease"]["id"], c2["token"], c2["header"], node="plan")
            assert st_exp == 410 and exp_body.get("code") == "lease_expired", (st_exp, exp_body)

            record.settle(route_run, "done")
            record.settle(exp_run, "done")
        finally:
            t_server.shutdown()
            t_thread.join(timeout=3)
finally:
    worker_listener.stop(w_server)

emit({"ok": True, "run_count": len(all_runs), "imported": imported_run})
""")
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["run_count"], 4)


class RunLeaseLaunchRecoveryTests(unittest.TestCase):
    def test_sigstop_lapse_continues_and_fences_old_worker(self):
        res = _run_behavior_child(self, r"""
from arete import (
    codebase, credential_files, dashboard, leases, paths, principals,
    replay, tailnet, util, worker_listener,
)
from arete.store import db, runs as run_store, tokens

now_box = [datetime.now(timezone.utc)]
util.set_clock(lambda: now_box[0])

sample = ROOT / "sample"
with mock.patch.object(util, "git", local_git):
    cfg_lines = replay.sample(sample, adw=True)
paths.CONFIG_FILE.write_text("max_parallel = 1\n" + "\n".join(cfg_lines) + "\n")
build_json = sample / "replay" / "build.json"
orig_build = json.loads(build_json.read_text())
build_json.write_text(json.dumps({**orig_build, "seconds": 60}))

db.migrate()
principals.seed_owner("owner@example.test")
tokens.init_owner()
codebase.load()

w_server, _ = worker_listener.start(port=0, poll_interval=0.01)
token = credential_files.read_client(db.schema_metadata()["instance_tag"])
node = SimpleNamespace(names={"127.0.0.1"})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: "owner@example.test"
handler = type("H", (dashboard.handlers().open,), {"disable_nagle_algorithm": True})
with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
    t_server = tailnet.Server("127.0.0.1", 0, node, logins, False, handler, dashboard.Redirect)

def post(path, body, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", t_server.server_port, timeout=5)
    hdrs = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        **(extra_headers or {}),
    }
    try:
        conn.request("POST", path, json.dumps(body), hdrs)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()

r1_pid = None
try:
    with t_server:
        th = threading.Thread(target=t_server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        th.start()
        try:
            st1, first = post("/api/tasks", {"repo": replay.SAMPLE_REPO, "prompt": "Add kumquat", "title": "Stop test", "kind": "feat"},
                              {"Idempotency-Key": "sigstop-1"})
            assert st1 == 200 and first.get("run"), (st1, first)
            tid, r1 = first["task"], first["run"]

            # Wait until r1 has entered build's replay sleep (build transcript header flushed, no DB tx held).
            r1_log = paths.RUNS_DIR / r1 / "run.log"
            r1_rec_dir = paths.record_dir(r1)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                row1 = run_store.run_get(r1)
                if (
                    row1
                    and row1.get("pid")
                    and r1_log.exists()
                    and "build: starting" in r1_log.read_text()
                    and r1_rec_dir.exists()
                    and any(p.stat().st_size > 0 for p in r1_rec_dir.glob("build*.jsonl*"))
                ):
                    r1_pid = int(row1["pid"])
                    break
                time.sleep(0.03)
            assert r1_pid, (run_store.run_get(r1), r1_log.read_text() if r1_log.exists() else "")
            with db.reader() as c:
                r1_lease = dict(c.execute("SELECT id, epoch, token_hash FROM leases WHERE id = ?", (row1["lease_id"],)).fetchone())
            assert r1_lease["token_hash"], f"worker {r1} did not claim its lease before SIGSTOP: {r1_lease}"

            # SIGSTOP worker r1 at build: starting.
            os.kill(r1_pid, signal.SIGSTOP)

            # Second POST while lease is live gets 409 naming r1 with "already running".
            st_busy, busy = post("/api/tasks", {"repo": replay.SAMPLE_REPO, "ticket": tid})
            assert st_busy == 409 and r1 in str(busy) and "already running" in str(busy), (st_busy, busy)

            # Speed up build for the continuation run r2.
            build_json.write_text(json.dumps({**orig_build, "seconds": 0}))

            # Advance serve's clock past r1's lease expires.
            now_box[0] = now_box[0] + timedelta(seconds=leases.TTL_SECONDS + 10)

            # Next POST continues r1: r1 -> interrupted, r2 -> continues=r1, restart='lease'.
            st2, second = post("/api/tasks", {"repo": replay.SAMPLE_REPO, "ticket": tid})
            assert st2 == 200 and second.get("run") and second["run"] != r1, (st2, second)
            r2 = second["run"]
            r1_after = run_store.run_get(r1)
            assert r1_after["status"] == "interrupted", r1_after

            # Wait for r2 to start (and finish) and verify run.log + lineage + never two running rows.
            r2_log = paths.RUNS_DIR / r2 / "run.log"
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                row2 = run_store.run_get(r2)
                with db.reader() as c:
                    dups = c.execute(
                        "SELECT count(*) FROM runs WHERE status = 'running' AND lock = ?",
                        (r1_after["lock"],),
                    ).fetchone()[0]
                assert dups <= 1, dups
                if row2 and row2["status"] not in ("queued", "running"):
                    break
                time.sleep(0.05)

            row2 = run_store.run_get(r2)
            assert row2["continues"] == r1 and row2["restart"] == "lease", row2
            r2_log_text = r2_log.read_text() if r2_log.exists() else ""
            expected_line = f"restarting after run {r1} was interrupted"
            assert expected_line in r2_log_text, r2_log_text

            # Resume old worker r1 (SIGCONT) -> receives 410 on heartbeat and exits cleanly.
            os.kill(r1_pid, signal.SIGCONT)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if leases.worker_exited(r1_pid):
                    break
                time.sleep(0.05)
            assert leases.worker_exited(r1_pid), f"resumed fenced worker {r1_pid} did not exit"
            assert run_store.run_get(r1)["status"] == "interrupted"
            r1_pid = None
        finally:
            if r1_pid is not None:
                try:
                    os.kill(r1_pid, signal.SIGCONT)
                    os.kill(r1_pid, signal.SIGKILL)
                except OSError:
                    pass
            t_server.shutdown()
            th.join(timeout=3)
finally:
    worker_listener.stop(w_server)

emit({"ok": True, "r1": r1, "r2": r2, "restart": row2["restart"], "continues": row2["continues"]})
""")
        self.assertTrue(res["ok"])
        self.assertEqual(res["restart"], "lease")
        self.assertEqual(res["continues"], res["r1"])


class RunLeaseSigkillAndOneStepTests(unittest.TestCase):
    def test_sigkill_settles_at_once_and_stop_then_one_step_fails_ticket(self):
        res = _run_behavior_child(self, r"""
from arete import (
    codebase, control, credential_files, dashboard, leases, paths, principals,
    record, replay, tailnet, util, worker_listener,
)
from arete.store import db, runs as run_store, tasks as task_store, tokens

now_box = [datetime.now(timezone.utc)]
util.set_clock(lambda: now_box[0])

sample = ROOT / "sample"
with mock.patch.object(util, "git", local_git):
    cfg_lines = replay.sample(sample, adw=True)
paths.CONFIG_FILE.write_text("max_parallel = 2\n" + "\n".join(cfg_lines) + "\n")
build_json = sample / "replay" / "build.json"
orig_build = json.loads(build_json.read_text())
build_json.write_text(json.dumps({**orig_build, "seconds": 60}))

db.migrate()
principals.seed_owner("owner@example.test")
tokens.init_owner()
codebase.load()

w_server, _ = worker_listener.start(port=0, poll_interval=0.01)
token = credential_files.read_client(db.schema_metadata()["instance_tag"])
node = SimpleNamespace(names={"127.0.0.1"})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: "owner@example.test"
handler = type("H", (dashboard.handlers().open,), {"disable_nagle_algorithm": True})
with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
    t_server = tailnet.Server("127.0.0.1", 0, node, logins, False, handler, dashboard.Redirect)

def post(path, body, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", t_server.server_port, timeout=5)
    hdrs = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        **(extra_headers or {}),
    }
    try:
        conn.request("POST", path, json.dumps(body), hdrs)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()

try:
    with t_server:
        th = threading.Thread(target=t_server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        th.start()
        try:
            # Part A: SIGKILL at build: starting -> next POST settles at once and continues;
            # tracker gets no comment/move for old run; stop on dead run signals nothing.
            st1, first = post("/api/tasks", {"repo": replay.SAMPLE_REPO, "prompt": "Add kumquat", "title": "Kill test", "kind": "feat"},
                              {"Idempotency-Key": "sigkill-1"})
            assert st1 == 200 and first.get("run"), (st1, first)
            tid1, r1 = first["task"], first["run"]
            r1_log = paths.RUNS_DIR / r1 / "run.log"
            r1_pid = None
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                row1 = run_store.run_get(r1)
                if row1 and row1.get("pid") and r1_log.exists() and "build: starting" in r1_log.read_text():
                    r1_pid = int(row1["pid"])
                    break
                time.sleep(0.03)
            assert r1_pid, run_store.run_get(r1)
            os.kill(r1_pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not leases.worker_exited(r1_pid):
                time.sleep(0.02)
            assert leases.worker_exited(r1_pid)

            comments_before = task_store.task_comments(tid1)
            build_json.write_text(json.dumps({**orig_build, "seconds": 0}))

            # Without advancing clock, next POST settles r1 at once and continues!
            st2, second = post("/api/tasks", {"repo": replay.SAMPLE_REPO, "ticket": tid1})
            assert st2 == 200 and second.get("run") and second["run"] != r1, (st2, second)
            r2 = second["run"]
            assert run_store.run_get(r1)["status"] == "interrupted"
            # Whole-run launch did NOT comment on the ticket about r1 dying.
            assert [c["body"] for c in task_store.task_comments(tid1) if r1 in c["body"]] == [], task_store.task_comments(tid1)
            # Wait for r2 to finish.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                row2 = run_store.run_get(r2)
                if row2 and row2["status"] not in ("queued", "running"):
                    break
                time.sleep(0.05)
            row2 = run_store.run_get(r2)
            assert row2["continues"] == r1 and row2["restart"] == "lease", row2

            # Stop on the old settled run r1 signals nothing (refused as already ended).
            with mock.patch.object(control.os, "kill", wraps=os.kill) as m_kill, mock.patch.object(control.os, "killpg", wraps=os.killpg) as m_killpg:
                st_old_stop, old_stop = post(
                    f"/api/v1/runs/{r1}/stop",
                    {"base_version": run_store.run_get(r1)["version"]},
                    {"Idempotency-Key": "stop-old-r1"},
                )
            assert st_old_stop == 400, (st_old_stop, old_stop)
            assert all(call.args[1] == 0 for call in m_kill.call_args_list), m_kill.call_args_list
            m_killpg.assert_not_called()

            # Part B: Stop followed by one-step post settles old run 'stopped' and fails ticket with 'was stopped by request'.
            build_json.write_text(json.dumps({**orig_build, "seconds": 15}))
            st3, third = post("/api/tasks", {"repo": replay.SAMPLE_REPO, "prompt": "Add kumquat", "title": "Stop one-step", "kind": "feat"},
                              {"Idempotency-Key": "stop-onestep-1"})
            assert st3 == 200 and third.get("run"), (st3, third)
            tid2, r3 = third["task"], third["run"]
            r3_log = paths.RUNS_DIR / r3 / "run.log"
            r3_pid = None
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                row3 = run_store.run_get(r3)
                if row3 and row3.get("pid") and r3_log.exists() and "build: starting" in r3_log.read_text():
                    r3_pid = int(row3["pid"])
                    break
                time.sleep(0.03)
            assert r3_pid, run_store.run_get(r3)
            # Kill worker immediately so r3 stays status='running' while dead, then call stop_run (which records stop_asked without settling ticket run).
            os.kill(r3_pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not leases.worker_exited(r3_pid):
                time.sleep(0.02)
            with mock.patch.object(control.os, "kill", wraps=os.kill) as m_kill2, mock.patch.object(control.os, "killpg", wraps=os.killpg) as m_killpg2:
                st_stop, stop_ans = post(
                    f"/api/v1/runs/{r3}/stop",
                    {"base_version": run_store.run_get(r3)["version"]},
                    {"Idempotency-Key": "stop-dead-r3"},
                )
            assert st_stop == 200 and stop_ans.get("was") == "gone", (st_stop, stop_ans)
            assert all(call.args[1] == 0 for call in m_kill2.call_args_list), m_kill2.call_args_list
            m_killpg2.assert_not_called()
            assert run_store.run_get(r3)["status"] == "running" and run_store.run_get(r3)["stop_asked"]

            # One-step post on r3 settles r3 as 'stopped' and fails ticket tid2 with 'was stopped by request'.
            build_json.write_text(json.dumps({**orig_build, "seconds": 0}))
            st_step, step_ans = post(f"/api/v1/runs/{r3}/step", {"node": "plan"}, {"Idempotency-Key": "step-after-stop-r3"})
            assert st_step == 201 and step_ans.get("run"), (st_step, step_ans)
            r3_final = run_store.run_get(r3)
            assert r3_final["status"] == "stopped", r3_final
            t2_comments = [c["body"] for c in task_store.task_comments(tid2)]
            assert any("was stopped by request" in body for body in t2_comments), t2_comments
            t2_row = task_store.task_get(tid2)
            assert t2_row["fault"] == 1 or t2_row["state"] in ("needs-you", "failed"), t2_row

            # Wait for the one-step child to settle cleanly before exiting.
            r4_obj = step_ans["run"]
            r4 = r4_obj["id"] if isinstance(r4_obj, dict) else r4_obj
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                row4 = run_store.run_get(r4)
                if row4 and row4["status"] not in ("queued", "running"):
                    break
                time.sleep(0.05)
        finally:
            t_server.shutdown()
            th.join(timeout=3)
finally:
    worker_listener.stop(w_server)

emit({"ok": True, "r1": r1, "r2": r2, "r3_status": r3_final["status"]})
""")
        self.assertTrue(res["ok"])
        self.assertEqual(res["r3_status"], "stopped")


class PinnedEngineCopyTests(unittest.TestCase):
    def test_pinned_engine_copy_isolation_cleanup_and_migration_gate(self):
        res = _run_behavior_child(self, r"""
from arete import (
    codebase, control, engine_copy, leases, paths, principals,
    record, replay, util, worker_lease, worker_listener,
)
from arete.store import db, devices as device_store, runs as run_store, tokens

now_box = [datetime.now(timezone.utc)]
util.set_clock(lambda: now_box[0])

sample = ROOT / "sample"
with mock.patch.object(util, "git", local_git):
    cfg_lines = replay.sample(sample, adw=True)
paths.CONFIG_FILE.write_text("max_parallel = 2\n" + "\n".join(cfg_lines) + "\n")
db.migrate()
principals.seed_owner("owner@example.test")
tokens.init_owner()

# Build a small local git checkout of the engine package to test commits A -> B -> C cleanly!
src_checkout = ROOT / "engine-src"
shutil.copytree(SOURCE / "arete", src_checkout / "arete", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
local_git(["init", "-q", "-b", "main"], src_checkout)
local_git(["add", "arete"], src_checkout)
local_git(
    ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
     "commit", "-qm", "engine commit A"],
    src_checkout,
)
sha_a = local_git(["rev-parse", "--short=12", "HEAD"], src_checkout).strip()

# Commit B in source checkout (serve runs at engine B, newer than A).
marker_file = src_checkout / "arete" / "_pin_marker.py"
marker_file.write_text('VERSION = "B"\n')
local_git(["add", "arete/_pin_marker.py"], src_checkout)
local_git(
    ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
     "commit", "-qm", "engine commit B"],
    src_checkout,
)
sha_b = local_git(["rev-parse", "--short=12", "HEAD"], src_checkout).strip()
assert sha_a != sha_b

w_server, w_url = worker_listener.start(port=0, poll_interval=0.01)
try:
    # Ensure engine copy at A from src_checkout, read-only, once per sha.
    copy_a = engine_copy.ensure(sha_a, source_root=src_checkout)
    assert copy_a == paths.ENGINE_DIR / sha_a and (copy_a / "arete" / "cli.py").is_file(), copy_a
    assert (copy_a / ".engine_sha").read_text().strip() == sha_a
    # Read-only check: creating a file inside copy_a or copy_a/arete must raise PermissionError.
    for probe_path in (copy_a / "mutate.txt", copy_a / "arete" / "mutate.txt"):
        ro_blocked = False
        try:
            probe_path.write_text("nope")
        except PermissionError:
            ro_blocked = True
        assert ro_blocked, f"engine copy path {probe_path} was not read-only"

    # Second ensure for same sha_a reuses existing copy without rebuilding.
    assert engine_copy.ensure(sha_a, source_root=src_checkout) == copy_a

    # Start a run pinned at engine A while serve's checkout is at engine B.
    run_id = record.start("loop", loop="pin-worker", engine_sha=sha_a, schema_version=75, pid=os.getpid())
    lease_id = int(run_store.run_get(run_id)["lease_id"])
    with db.tx() as c:
        claim_tok = leases.issue_claim_token(c, run_id, lease_id)

    client = worker_lease.WorkerClient(w_url)
    # Unknown line_shape gets 400 at claim.
    try:
        client.claim(claim_tok, engine_sha=sha_a, line_shape="bogus-v99")
    except worker_lease.WorkerError as exc:
        assert exc.status == 400, exc
    else:
        raise AssertionError("expected 400 on unknown line_shape")

    # Valid claim at engine A while serve runs engine B.
    claimed = client.claim(claim_tok, engine_sha=sha_a, line_shape="v1", node="worker")
    assert claimed["lease"]["engine_sha"] == sha_a and claimed["lease"]["line_shape"] == "v1"

    # Worker posts lines that serve reads.
    run_store.add_feed(run_id, "info", f"line from engine {sha_a}")
    feed_lines = run_store.feed(run_id, 0)
    assert any(f"line from engine {sha_a}" in ln["text"] for ln in feed_lines), feed_lines

    # Make commit C in source checkout WHILE worker is running!
    marker_file.write_text('VERSION = "C"\n')
    local_git(["add", "arete/_pin_marker.py"], src_checkout)
    local_git(
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
         "commit", "-qm", "engine commit C"],
        src_checkout,
    )

    # Launch a worker subprocess using pinned engine_root=copy_a and verify:
    # 1. Every loaded arete.* module's __file__ is under STATE_DIR/engine/<A>/
    # 2. _pin_marker (added in commit B and mutated in C) is NOT present in the worker
    # 3. An `arete` command started by the worker via paths.cli_command() ALSO runs from STATE_DIR/engine/<A>/!
    probe_code = (
        "import json, os, subprocess, sys; "
        f"sys.path.insert(0, {str(copy_a)!r}); "
        "import arete, arete.codebase, arete.paths, arete.control; "
        "loaded = {name: getattr(mod, '__file__', '') for name, mod in sys.modules.items() "
        "if (name == 'arete' or name.startswith('arete.')) and getattr(mod, '__file__', None)}; "
        "has_marker = False\n"
        "try:\n"
        "    import arete._pin_marker\n"
        "    has_marker = True\n"
        "except ImportError:\n"
        "    pass\n"
        "sub_cmd = arete.paths.cli_command('--version'); "
        "print(json.dumps({'loaded': loaded, 'has_marker': has_marker, 'stamp': arete.codebase.load(), "
        "'sub_cmd': sub_cmd, 'sub_uses_copy': str(arete.paths.ENGINE_DIR) in ' '.join(sub_cmd)}))"
    )
    worker_env = {
        **os.environ,
        "ARETE_ENGINE_COPY": str(copy_a),
        "ARETE_ENGINE_SHA": sha_a,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    probe = subprocess.run(
        [sys.executable, "-IB", "-c", probe_code],
        cwd=ROOT, env=worker_env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert probe.returncode == 0, (probe.stdout, probe.stderr)
    probe_data = json.loads(probe.stdout)
    assert probe_data["loaded"], probe_data
    for mod_name, mod_file in probe_data["loaded"].items():
        assert Path(mod_file).resolve().is_relative_to(copy_a.resolve()), (mod_name, mod_file, copy_a)
    assert not probe_data["has_marker"], probe_data
    assert probe_data["stamp"] == sha_a, probe_data
    assert probe_data["sub_uses_copy"], probe_data

    # Migration gate counts live leases by schema_version and blocks contract 76 while schema_version=75 lease is open!
    with db.reader() as c:
        counts = db.live_lease_schema_counts(c)
        blockers_76 = db.contract_blockers(c, 76)
    assert counts.get(75, 0) >= 1, counts
    assert f"lease L-{lease_id}" in blockers_76, blockers_76

    # Release lease -> folder STATE_DIR/engine/<A>/ is removed automatically, and contract blocker clears!
    st_rel, _ = client.release(claimed["lease"]["id"], claimed["token"], claimed["header"], node="worker")
    assert st_rel == 200, st_rel
    record.settle(run_id, "done")
    assert not copy_a.exists(), f"engine copy {copy_a} still exists after lease release"
    with db.reader() as c:
        counts_after = db.live_lease_schema_counts(c)
        blockers_after = db.contract_blockers(c, 76)
    assert counts_after.get(75, 0) == 0, counts_after
    assert f"lease L-{lease_id}" not in blockers_after, blockers_after
finally:
    worker_listener.stop(w_server)

emit({"ok": True, "sha_a": sha_a, "sha_b": sha_b, "loaded_count": len(probe_data["loaded"])})
""")
        self.assertTrue(res["ok"])
        self.assertGreater(res["loaded_count"], 0)


if __name__ == "__main__":
    unittest.main()
