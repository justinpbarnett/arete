from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from arete import api_check
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

ROOT = Path(__file__).resolve().parent.parent
HELPER = r"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import api, api_check, cli, config, control, dashboard, inbox, loop_run, loops, paths, principals, record, serve, store, tailnet, util
from arete.store import db as foundation
from arete.store import inbox as inbox_store
from arete.store import loops as loop_store
from arete.store import runs as run_store
from arete.store import tokens as token_store


def setup_env(loop_specs: dict[str, dict]) -> tuple[Path, config.Config]:
    paths.ensure_dirs()
    foundation.migrate()
    loop_dir = Path(os.environ["ARETE_CONFIG_DIR"]) / "loops"
    loop_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = Path(os.environ["ARETE_CONFIG_DIR"]) / "config.toml"
    cfg_file.write_text(f"[serve]\nloop_dirs = [{json.dumps(str(loop_dir))}]\n", encoding="utf-8")
    for name, spec in loop_specs.items():
        folder = loop_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / "run.py"
        script.write_text(spec["script"], encoding="utf-8")
        extra_run = spec.get("extra_run", "")
        toml = (
            'kind = "command"\n'
            f'description = "fake {name}"\n'
            "enabled = true\n"
            "[run]\n"
            f'command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]\n'
            f"{extra_run}"
        )
        (folder / "loop.toml").write_text(toml, encoding="utf-8")
    return loop_dir, config.load()


@contextmanager
def live_api_server():
    owner = principals.seed_owner("owner@example.test")
    with foundation.tx() as c:
        token_store.ensure_key(c)
    _, bearer = token_store.issue(owner, "Queue test", ["read", "inbox:resolve"])
    node = tailnet.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "127.0.0.1")
    logins = tailnet.Logins(SimpleNamespace(whois=lambda _: "owner@example.test"))
    with mock.patch("socket.getfqdn", return_value="127.0.0.1"):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    httpd.node = node
    httpd.logins = logins
    httpd.inside_serve = True
    httpd.front = tailnet.Front("http", [])
    httpd.pending_secrets = {}
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield port, bearer
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def http_json(
    port: int,
    bearer: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    idem: str | None = None,
) -> tuple[int, dict]:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Authorization": f"Bearer {bearer}"}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(raw))
        if method != "GET" and idem:
            headers["Idempotency-Key"] = idem
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        return resp.status, data
    finally:
        conn.close()


def spawn_carry_process(loop_name: str, item_id: int) -> subprocess.Popen:
    code = (
        f"import sys; sys.path.insert(0, {str(Path(loop_run.__file__).resolve().parent.parent)!r}); "
        f"from arete import loop_run; loop_run.launch({loop_name!r}, item={item_id!r}, detach=False)"
    )
    env = {k: os.environ[k] for k in os.environ}
    return subprocess.Popen(
        [sys.executable, "-IB", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def wait_for(predicate, timeout: float = 8.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = predicate()
        if val:
            return val
        time.sleep(interval)
    raise TimeoutError("timed out waiting for condition")
"""


def _run_behavior(script: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-q289-") as tmp:
        root = Path(tmp)
        env = api_check.isolated_env(root)
        cmd = api_check.child_command(ROOT, HELPER + "\n" + script)
        proc = subprocess.run(
            cmd,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHILD_HANG_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"child exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("BEHAVIOR_RESULT="):
                return json.loads(line.removeprefix("BEHAVIOR_RESULT="))
        raise AssertionError(f"missing BEHAVIOR_RESULT= line\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


class LoopQueueDeadLetterInboxTests(unittest.TestCase):
    def test_failed_run_raises_inbox_item_requeue_dismiss_and_success_or_empty_drops(self):
        result = _run_behavior(r"""
root = Path.cwd()
marker = root / "marker.txt"
exit_file = root / "exit_code.txt"
exit_file.write_text("1", encoding="utf-8")

setup_env({
    "flaky": {
        "script": (
            "from pathlib import Path\n"
            "import sys\n"
            f"Path({str(marker)!r}).write_text('ran\\n', encoding='utf-8')\n"
            f"code = int(Path({str(exit_file)!r}).read_text(encoding='utf-8').strip())\n"
            "print('boom line' if code else 'ok line')\n"
            "sys.exit(code)\n"
        ),
    },
})

sample_event = [{"type": "test.ev", "source": "s", "key": "k1", "title": "first", "trust": "internal", "data": {"n": 1}}]
item_id = loop_store.enqueue("flaky", "flaky", "", "event", sample_event)

with live_api_server() as (port, bearer):
    run1 = loop_run.launch("flaky", item=item_id, detach=False)
    assert run1["status"] == "failed", run1

    dead_row = loop_store.queue_item(item_id)
    assert dead_row is not None and dead_row["dead_at"] and dead_row["run_id"] == run1["id"]

    status_list, listed = http_json(port, bearer, "GET", "/api/v1/inbox")
    assert status_list == 200, (status_list, listed)
    q_items = [it for it in listed["items"] if it["kind"] == "queue.dead"]
    assert len(q_items) == 1, q_items
    first_item = q_items[0]
    assert first_item["subject"] == f"queue:{item_id}"
    assert first_item["run_id"] == run1["id"]
    assert first_item["priority"] == 2
    assert "flaky" in first_item["title"]
    assert "exit 1" in first_item["detail"]["summary"]
    assert first_item["detail"]["events"] == 1
    assert first_item["detail"]["loop"] == "flaky"
    assert first_item["detail"]["run_id"] == run1["id"]
    assert first_item["verbs"] == ["requeue", "dismiss"]

    # loop_detail dead entries carry their inbox item id
    detail_view = control.loop_detail("flaky")
    assert len(detail_view["dead"]) == 1
    assert detail_view["dead"][0]["inbox_id"] == first_item["id"]

    # GET /api/v1/inbox/{item} returns subject_object
    status_get, got = http_json(port, bearer, "GET", f"/api/v1/inbox/{first_item['id']}")
    assert status_get == 200 and got["subject_object"]["id"] == item_id

    # events_log has one inbox.raised row
    with foundation.reader() as c:
        raised = c.execute(
            "SELECT * FROM events_log WHERE kind = 'inbox.raised' AND subject = ?",
            (f"inbox:{inbox_store.parse_id(first_item['id'])}",),
        ).fetchall()
    assert len(raised) == 1, raised

    # Invalid verb returns 422 validation
    bad_status, bad_body = http_json(
        port, bearer, "POST", f"/api/v1/inbox/{first_item['id']}/resolve",
        {"base_version": first_item["version"], "verb": "approve"},
        idem="bad-verb-test-key",
    )
    assert bad_status == 422 and bad_body["code"] == "validation", (bad_status, bad_body)

    # Resolve with requeue -> row returns to pending() with attempts 0 and writes inbox.resolved
    rq_status, rq_body = http_json(
        port, bearer, "POST", f"/api/v1/inbox/{first_item['id']}/resolve",
        {"base_version": first_item["version"], "verb": "requeue"},
        idem="requeue-test-key",
    )
    assert rq_status == 200, (rq_status, rq_body)
    assert rq_body["item"]["state"] == "resolved" and rq_body["item"]["resolution"] == "requeued"
    pend = loop_store.pending()
    assert len(pend) == 1 and pend[0]["id"] == item_id and pend[0]["attempts"] == 0 and pend[0]["dead_at"] is None
    with foundation.reader() as c:
        resolved_ev = c.execute(
            "SELECT * FROM events_log WHERE kind = 'inbox.resolved' AND subject = ?",
            (f"inbox:{inbox_store.parse_id(first_item['id'])}",),
        ).fetchall()
    assert len(resolved_ev) == 1

    # Second launch uses same events in events.json and failing again raises a new item
    run2 = loop_run.launch("flaky", item=item_id, detach=False)
    assert run2["status"] == "failed"
    replayed_events = json.loads((paths.RUNS_DIR / run2["id"] / "events.json").read_text(encoding="utf-8"))
    assert replayed_events == sample_event

    _, listed2 = http_json(port, bearer, "GET", "/api/v1/inbox")
    q_items2 = [it for it in listed2["items"] if it["kind"] == "queue.dead"]
    assert len(q_items2) == 1 and q_items2[0]["id"] != first_item["id"]
    second_item = q_items2[0]

    # Dismiss deletes the queue row and resolves the item
    dm_status, dm_body = http_json(
        port, bearer, "POST", f"/api/v1/inbox/{second_item['id']}/resolve",
        {"base_version": second_item["version"], "verb": "dismiss"},
        idem="dismiss-test-key",
    )
    assert dm_status == 200 and dm_body["item"]["resolution"] == "dismissed"
    assert loop_store.queue_item(item_id) is None

    # Same loop exiting 0 leaves no queue row and no item
    exit_file.write_text("0", encoding="utf-8")
    ok_id = loop_store.enqueue("flaky", "flaky", "", "event", sample_event)
    ok_run = loop_run.launch("flaky", item=ok_id, detach=False)
    assert ok_run["status"] == "done"
    assert loop_store.queue_item(ok_id) is None
    assert not inbox.list(state="open", kind="queue.dead")

    # Failed run fired by schedule (empty payload) leaves no queue row and no item
    exit_file.write_text("1", encoding="utf-8")
    sched_id = loop_store.enqueue("flaky", "flaky", "", "every", [])
    sched_run = loop_run.launch("flaky", item=sched_id, detach=False)
    assert sched_run["status"] == "failed"
    assert loop_store.queue_item(sched_id) is None
    assert not inbox.list(state="open", kind="queue.dead")

    # Inbox rebuild raises from a dead row without an item, and clears an open item whose row is no longer dead
    orphan_id = loop_store.enqueue("flaky", "flaky", "", "event", sample_event)
    with foundation.tx() as c:
        c.execute(
            "UPDATE queue SET run_id = 'r-orphan', dead_at = ?, dead_reason = 'manual dead' WHERE id = ?",
            (util.iso(), orphan_id),
        )
        inbox.sync(c, "queue")
    rebuilt = inbox.list(state="open", kind="queue.dead")
    assert len(rebuilt) == 1 and rebuilt[0]["subject"] == f"queue:{orphan_id}"
    with foundation.tx() as c:
        c.execute("UPDATE queue SET run_id = NULL, dead_at = NULL, dead_reason = '' WHERE id = ?", (orphan_id,))
        inbox.sync(c, "queue")
    assert not inbox.list(state="open", kind="queue.dead")
    cleared = inbox.get(rebuilt[0]["id"])["item"]
    assert cleared["state"] == "resolved" and cleared["resolution"] == "cleared"

    # CLI `arete loop requeue ID` calls requeue_item and resolves its inbox item
    cli_id = loop_store.enqueue("flaky", "flaky", "", "event", sample_event)
    cli_run = loop_run.launch("flaky", item=cli_id, detach=False)
    assert cli_run["status"] == "failed"
    cli_inbox = inbox_store.get_open_by_subject(f"queue:{cli_id}")
    assert cli_inbox is not None
    assert cli.main(["loop", "requeue", str(cli_id)]) == 0
    assert any(r["id"] == cli_id and r["attempts"] == 0 for r in loop_store.pending())
    assert inbox_store.get(cli_inbox["id"])["resolution"] == "requeued"

print("BEHAVIOR_RESULT=" + json.dumps({
    "first_item": first_item["id"],
    "second_item": second_item["id"],
    "replayed_match": replayed_events == sample_event,
    "bad_status": bad_status,
}))
""")
        self.assertEqual(result["bad_status"], 422)
        self.assertTrue(result["replayed_match"])
        self.assertNotEqual(result["first_item"], result["second_item"])

    def test_dead_letter_atomicity_on_inbox_insert_failure(self):
        result = _run_behavior(r"""
setup_env({
    "boom": {
        "script": "import sys; sys.exit(0)\n",
    },
})
sample_event = [{"type": "t", "source": "s", "key": "k", "title": "ev", "trust": "internal", "data": {}}]
item_id = loop_store.enqueue("boom", "boom", "", "event", sample_event)
run_id = record.start("loop", loop="boom", loop_kind="command", trigger="event", queue="boom")
with foundation.tx() as c:
    assert loop_store.claim(c, item_id, run_id)
    record.settle(run_id, "stopped", summary="stopped by request")

with mock.patch.object(inbox_store, "raise_item", side_effect=RuntimeError("inbox exploded")):
    try:
        loop_store.release_settled_claims()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "inbox exploded" in str(e)

after_fail = loop_store.queue_item(item_id)
assert after_fail["run_id"] == run_id and after_fail["dead_at"] is None
with foundation.reader() as c:
    inbox_rows = c.execute("SELECT * FROM inbox WHERE kind = 'queue.dead'").fetchall()
    ev_rows = c.execute("SELECT * FROM events_log WHERE kind = 'inbox.raised'").fetchall()
assert len(inbox_rows) == 0 and len(ev_rows) == 0

swept = loop_store.release_settled_claims()
assert swept == 1
after_ok = loop_store.queue_item(item_id)
assert after_ok["dead_at"] is not None and after_ok["run_id"] == run_id
with foundation.reader() as c:
    inbox_rows2 = c.execute("SELECT * FROM inbox WHERE kind = 'queue.dead'").fetchall()
    ev_rows2 = c.execute("SELECT * FROM events_log WHERE kind = 'inbox.raised'").fetchall()
assert len(inbox_rows2) == 1 and len(ev_rows2) == 1

print("BEHAVIOR_RESULT=" + json.dumps({"rolled_back": True, "finished_once": True}))
""")
        self.assertTrue(result["rolled_back"])
        self.assertTrue(result["finished_once"])

    def test_prune_keeps_running_claims_and_expires_old_dead_rows_and_inbox_items(self):
        result = _run_behavior(r"""
from datetime import timedelta

setup_env({
    "keeper": {
        "script": "import sys; sys.exit(0)\n",
    },
})
ev = [{"type": "t", "source": "s", "key": "k", "title": "ev", "trust": "internal", "data": {}}]

# 1. 15-day-old claim of a running run
running_item = loop_store.enqueue("keeper", "keeper", "", "event", ev)
running_run = record.start("loop", loop="keeper", loop_kind="command", trigger="event", queue="keeper")
with foundation.tx() as c:
    assert loop_store.claim(c, running_item, running_run)
    old_15 = util.iso(util.now() - timedelta(days=15))
    c.execute("UPDATE queue SET enqueued = ? WHERE id = ?", (old_15, running_item))

# 2. 57-day-old dead row
old_dead_item = loop_store.enqueue("keeper", "keeper", "", "event", ev)
old_dead_run = record.start("loop", loop="keeper", loop_kind="command", trigger="event", queue="keeper")
with foundation.tx() as c:
    assert loop_store.claim(c, old_dead_item, old_dead_run)
    loop_store.end_claim(c, old_dead_run, "dead", "old failure")
    old_57 = util.iso(util.now() - timedelta(days=57))
    c.execute("UPDATE queue SET dead_at = ?, enqueued = ? WHERE id = ?", (old_57, old_57, old_dead_item))
old_inbox = inbox_store.get_open_by_subject(f"queue:{old_dead_item}")
assert old_inbox is not None

# 3. 10-day-old dead row
recent_dead_item = loop_store.enqueue("keeper", "keeper", "", "event", ev)
recent_dead_run = record.start("loop", loop="keeper", loop_kind="command", trigger="event", queue="keeper")
with foundation.tx() as c:
    assert loop_store.claim(c, recent_dead_item, recent_dead_run)
    loop_store.end_claim(c, recent_dead_run, "dead", "recent failure")
    old_10 = util.iso(util.now() - timedelta(days=10))
    c.execute("UPDATE queue SET dead_at = ?, enqueued = ? WHERE id = ?", (old_10, old_10, recent_dead_item))
recent_inbox = inbox_store.get_open_by_subject(f"queue:{recent_dead_item}")
assert recent_inbox is not None

store.prune()

assert loop_store.queue_item(running_item) is not None
assert loop_store.queue_item(old_dead_item) is None
expired_inbox = inbox_store.get(old_inbox["id"])
assert expired_inbox["state"] == "resolved" and expired_inbox["resolution"] == "expired"
with foundation.reader() as c:
    ev_rows = c.execute(
        "SELECT * FROM events_log WHERE kind = 'inbox.resolved' AND subject = ?",
        (f"inbox:{inbox_store.parse_id(old_inbox['id'])}",),
    ).fetchall()
assert len(ev_rows) == 1

assert loop_store.queue_item(recent_dead_item) is not None
still_open = inbox_store.get(recent_inbox["id"])
assert still_open["state"] == "open"

print("BEHAVIOR_RESULT=" + json.dumps({
    "running_kept": True,
    "old_expired": expired_inbox["resolution"],
    "recent_state": still_open["state"],
}))
""")
        self.assertTrue(result["running_kept"])
        self.assertEqual(result["old_expired"], "expired")
        self.assertEqual(result["recent_state"], "open")


class LoopQueueRetryAndSignalsTests(unittest.TestCase):
    def test_sigkill_reap_retry_continues_log_and_max_attempts_config(self):
        result = _run_behavior(r"""
root = Path.cwd()
marker = root / "started.txt"

sleep_script = (
    "from pathlib import Path\n"
    "import os, time\n"
    f"Path({str(marker)!r}).write_text(os.environ.get('ARETE_RUN_ID', ''), encoding='utf-8')\n"
    "time.sleep(60)\n"
)

loop_dir, _ = setup_env({
    "sleeper": {"script": sleep_script},
    "triple": {"script": sleep_script, "extra_run": "max_attempts = 3\n"},
    "bad": {"script": "import sys; sys.exit(0)\n", "extra_run": "max_attempts = 0\n"},
})

# max_attempts = 0 gives a loop error naming the key
by_name = {x.name: x for x in loops.load_all()}
assert "max_attempts" in by_name["bad"].error, by_name["bad"].error

ev = [{"type": "t", "source": "s", "key": "k1", "title": "payload", "trust": "internal", "data": {"x": 42}}]
item_id = loop_store.enqueue("sleeper", "sleeper", "", "event", ev)

# Kill 1 -> reap -> pending with attempts=1, same payload, last_run=run1, no inbox item
marker.unlink(missing_ok=True)
p1 = spawn_carry_process("sleeper", item_id)
run1_id = wait_for(lambda: marker.read_text(encoding="utf-8").strip() if marker.exists() else "")
os.kill(p1.pid, signal.SIGKILL)
p1.wait(timeout=5)
with mock.patch.object(record, "SETTLE_GRACE_SECONDS", 0):
    record.reap(tickets=False)

pend1 = loop_store.pending()
assert len(pend1) == 1 and pend1[0]["id"] == item_id
assert pend1[0]["attempts"] == 1
assert json.loads(pend1[0]["payload"]) == ev
assert pend1[0]["last_run"] == run1_id
assert not inbox.list(state="open", kind="queue.dead")

# Kill 2 -> next launch's first run.log line names run1; after kill+reap -> dead with 1 inbox item, prune keeps it
marker.unlink(missing_ok=True)
p2 = spawn_carry_process("sleeper", item_id)
run2_id = wait_for(lambda: marker.read_text(encoding="utf-8").strip() if marker.exists() else "")
first_log_line = (paths.RUNS_DIR / run2_id / "run.log").read_text(encoding="utf-8").splitlines()[0]
assert f"continues {run1_id}" in first_log_line, first_log_line
os.kill(p2.pid, signal.SIGKILL)
p2.wait(timeout=5)
with mock.patch.object(record, "SETTLE_GRACE_SECONDS", 0):
    record.reap(tickets=False)

dead = loop_store.queue_item(item_id)
assert dead["dead_at"] is not None and dead["run_id"] == run2_id
q_items = inbox.list(state="open", kind="queue.dead")
assert len(q_items) == 1 and q_items[0]["subject"] == f"queue:{item_id}"
store.prune()
assert loop_store.queue_item(item_id) is not None

# With max_attempts = 3: 1st kill -> attempts 1, 2nd kill -> attempts 2, 3rd kill -> dead
item3 = loop_store.enqueue("triple", "triple", "", "event", ev)
seen_attempts = []
for _ in range(3):
    marker.unlink(missing_ok=True)
    proc = spawn_carry_process("triple", item3)
    wait_for(lambda: marker.read_text(encoding="utf-8").strip() if marker.exists() else "")
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    with mock.patch.object(record, "SETTLE_GRACE_SECONDS", 0):
        record.reap(tickets=False)
    row = loop_store.queue_item(item3)
    seen_attempts.append((row["attempts"], row["dead_at"] is not None))

assert seen_attempts == [(1, False), (2, False), (2, True)], seen_attempts
triple_items = [it for it in inbox.list(state="open", kind="queue.dead") if it["subject"] == f"queue:{item3}"]
assert len(triple_items) == 1

print("BEHAVIOR_RESULT=" + json.dumps({
    "first_log_line": first_log_line,
    "triple_progression": seen_attempts,
    "bad_error": by_name["bad"].error,
}))
""")
        self.assertIn("continues ", result["first_log_line"])
        self.assertEqual(result["triple_progression"], [[1, False], [2, False], [2, True]])
        self.assertIn("max_attempts", result["bad_error"])

    def test_stop_run_gone_stop_over_budget_and_unsolicited_sigterm(self):
        result = _run_behavior(r"""
root = Path.cwd()
marker = root / "started.txt"
sleep_script = (
    "from pathlib import Path\n"
    "import os, time\n"
    f"Path({str(marker)!r}).write_text(os.environ.get('ARETE_RUN_ID', ''), encoding='utf-8')\n"
    "time.sleep(60)\n"
)
setup_env({"worker": {"script": sleep_script}})
ev = [{"type": "t", "source": "s", "key": "k", "title": "ev", "trust": "internal", "data": {}}]

# 1. Run stopped through control.stop_run (live process) -> dead + 1 queue.dead item
id1 = loop_store.enqueue("worker", "worker", "", "event", ev)
marker.unlink(missing_ok=True)
p1 = spawn_carry_process("worker", id1)
r1 = wait_for(lambda: marker.read_text(encoding="utf-8").strip() if marker.exists() else "")
wait_for(lambda: (run_store.run_get(r1) or {}).get("pid"))
control.stop_run(r1)
p1.wait(timeout=5)
row1 = loop_store.queue_item(id1)
assert row1["dead_at"] is not None and row1["run_id"] == r1
assert inbox_store.get_open_by_subject(f"queue:{id1}") is not None

# 2. Run whose process is already gone when control.stop_run is called -> after next reap -> dead + 1 queue.dead item
id2 = loop_store.enqueue("worker", "worker", "", "event", ev)
marker.unlink(missing_ok=True)
p2 = spawn_carry_process("worker", id2)
r2 = wait_for(lambda: marker.read_text(encoding="utf-8").strip() if marker.exists() else "")
os.kill(p2.pid, signal.SIGKILL)
p2.wait(timeout=5)
stopped = control.stop_run(r2)
assert stopped.get("was") == "gone", stopped
with mock.patch.object(record, "SETTLE_GRACE_SECONDS", 0):
    record.reap(tickets=False)
row2 = loop_store.queue_item(id2)
assert row2["dead_at"] is not None and row2["run_id"] == r2
assert inbox_store.get_open_by_subject(f"queue:{id2}") is not None

# 3. Run whose kind raises OverBudget mid-run -> dead + 1 queue.dead item
id3 = loop_store.enqueue("worker", "worker", "", "event", ev)
orig_run = loops.KINDS["command"].run
try:
    loops.KINDS["command"].run = lambda _run: (_ for _ in ()).throw(loop_run.OverBudget("run budget is spent"))
    r3 = loop_run.launch("worker", item=id3, detach=False)
finally:
    loops.KINDS["command"].run = orig_run
assert r3["status"] == "skipped"
row3 = loop_store.queue_item(id3)
assert row3["dead_at"] is not None and row3["run_id"] == r3["id"]
assert inbox_store.get_open_by_subject(f"queue:{id3}") is not None

# 4. Run whose carry process gets SIGTERM with NO stop asked -> pending with attempts 1
id4 = loop_store.enqueue("worker", "worker", "", "event", ev)
marker.unlink(missing_ok=True)
p4 = spawn_carry_process("worker", id4)
r4 = wait_for(lambda: marker.read_text(encoding="utf-8").strip() if marker.exists() else "")
wait_for(lambda: (run_store.run_get(r4) or {}).get("pid"))
os.kill(p4.pid, signal.SIGTERM)
p4.wait(timeout=5)
row4 = loop_store.queue_item(id4)
assert row4["run_id"] is None and row4["dead_at"] is None and row4["attempts"] == 1 and row4["last_run"] == r4
assert inbox_store.get_open_by_subject(f"queue:{id4}") is None

# 5. Budget skip before start releases without counting an attempt
id5 = loop_store.enqueue("worker", "worker", "", "event", ev)
with mock.patch.object(loop_run, "spent_limit", return_value="daily loop budget"):
    r5 = loop_run.launch("worker", item=id5, detach=False)
assert r5["status"] == "skipped"
row5 = loop_store.queue_item(id5)
assert row5["run_id"] is None and row5["dead_at"] is None and row5["attempts"] == 0

print("BEHAVIOR_RESULT=" + json.dumps({
    "stop_live_dead": True,
    "stop_gone_dead": True,
    "over_budget_dead": True,
    "sigterm_attempts": row4["attempts"],
    "pre_budget_attempts": row5["attempts"],
}))
""")
        self.assertTrue(result["stop_live_dead"])
        self.assertTrue(result["stop_gone_dead"])
        self.assertTrue(result["over_budget_dead"])
        self.assertEqual(result["sigterm_attempts"], 1)
        self.assertEqual(result["pre_budget_attempts"], 0)


class LoopQueueAtomicityDedupeAndNeverBeganTests(unittest.TestCase):
    def test_claim_single_owner_and_atomic_launch_rollback(self):
        result = _run_behavior(r"""
root = Path.cwd()
marker = root / "marker.txt"
setup_env({
    "atomic": {
        "script": f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
    },
})
ev = [{"type": "t", "source": "s", "key": "k", "title": "ev", "trust": "internal", "data": {}}]

# 1. With item claimed by run A, claim(c, ID, "B") returns False and row keeps run A
id1 = loop_store.enqueue("atomic", "atomic", "", "event", ev)
with foundation.tx() as c:
    assert loop_store.claim(c, id1, "A") is True
    assert loop_store.claim(c, id1, "B") is False
assert loop_store.queue_item(id1)["run_id"] == "A"

# 2. With claim patched to return False, launch raises NotFound, leaves no runs row, no run.started, no marker
id2 = loop_store.enqueue("atomic", "atomic", "", "event", ev)
marker.unlink(missing_ok=True)
with mock.patch.object(loop_store, "claim", return_value=False):
    try:
        loop_run.launch("atomic", item=id2, detach=False, run_id="run-loser")
        raise AssertionError("expected NotFound")
    except control.NotFound:
        pass
assert run_store.run_get("run-loser") is None
with foundation.reader() as c:
    started_ev = c.execute(
        "SELECT * FROM events_log WHERE kind = 'run.started' AND subject = 'run:run-loser'"
    ).fetchall()
assert len(started_ev) == 0
assert not marker.exists()
assert loop_store.queue_item(id2)["run_id"] is None

# 3. With claim patched to raise after runs insert, item stays pending and no runs row is left
id3 = loop_store.enqueue("atomic", "atomic", "", "event", ev)
def boom_claim(c, item_id, run_id):
    assert c.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is not None
    raise RuntimeError("claim crashed")

with mock.patch.object(loop_store, "claim", side_effect=boom_claim):
    try:
        loop_run.launch("atomic", item=id3, detach=False, run_id="run-crash")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "claim crashed" in str(e)
assert run_store.run_get("run-crash") is None
assert loop_store.queue_item(id3)["run_id"] is None

print("BEHAVIOR_RESULT=" + json.dumps({"claim_guarded": True, "false_rolled_back": True, "error_rolled_back": True}))
""")
        self.assertTrue(result["claim_guarded"])
        self.assertTrue(result["false_rolled_back"])
        self.assertTrue(result["error_rolled_back"])

    def test_migration_dedupe_concurrent_enqueue_and_schedule_reap_coalesce(self):
        result = _run_behavior(r"""
# 1. Seed pre-38 DB with two waiting empty rows of one loop and trigger, then migrate()
paths.ensure_dirs()
foundation.migrate(through=37)
with foundation.tx() as c:
    low_id = c.execute(
        "INSERT INTO queue (loop, queue, grp, trigger, payload, enqueued) VALUES ('sched', 'sched', '', 'every', '[]', ?)",
        (util.iso(),),
    ).lastrowid
    high_id = c.execute(
        "INSERT INTO queue (loop, queue, grp, trigger, payload, enqueued) VALUES ('sched', 'sched', '', 'every', '[]', ?)",
        (util.iso(),),
    ).lastrowid
foundation.migrate()
with foundation.reader() as c:
    idx = c.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'queue_one_waiting'").fetchone()
    rows = [dict(r) for r in c.execute("SELECT id FROM queue WHERE loop = 'sched' AND trigger = 'every' ORDER BY id")]
assert idx is not None
assert [r["id"] for r in rows] == [low_id], (rows, low_id, high_id)
with foundation.tx() as c:
    c.execute("DELETE FROM queue WHERE loop = 'sched'")

# 2. Two processes calling enqueue(loop, queue, '', 'every') behind a barrier, 20 rounds
root = Path.cwd()
pkg_root = str(Path(loop_run.__file__).resolve().parent.parent)
worker_script = root / "enq_worker.py"
worker_script.write_text(
    f"import json, os, sys, time\n"
    f"sys.path.insert(0, {pkg_root!r})\n"
    "from pathlib import Path\n"
    "from arete.store import loops as loop_store\n"
    "role, rounds, gate_dir = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])\n"
    "results = []\n"
    "for r in range(rounds):\n"
    "    (gate_dir / f'ready-{r}-{role}').write_text('1')\n"
    "    while not (gate_dir / f'go-{r}').exists():\n"
    "        time.sleep(0.002)\n"
    "    results.append(loop_store.enqueue('race', 'race', '', 'every'))\n"
    "    (gate_dir / f'done-{r}-{role}').write_text('1')\n"
    "print(json.dumps(results))\n",
    encoding="utf-8",
)
gate_dir = root / "gates"
gate_dir.mkdir()
env = {k: os.environ[k] for k in os.environ}
p_a = subprocess.Popen(
    [sys.executable, "-IB", str(worker_script), "a", "20", str(gate_dir)],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
)
p_b = subprocess.Popen(
    [sys.executable, "-IB", str(worker_script), "b", "20", str(gate_dir)],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
)
counts_per_round = []
for r in range(20):
    wait_for(lambda r=r: (gate_dir / f"ready-{r}-a").exists() and (gate_dir / f"ready-{r}-b").exists())
    (gate_dir / f"go-{r}").write_text("1")
    wait_for(lambda r=r: (gate_dir / f"done-{r}-a").exists() and (gate_dir / f"done-{r}-b").exists())
    waiting = [row for row in loop_store.pending() if row["loop"] == "race"]
    counts_per_round.append(len(waiting))
    with foundation.tx() as c:
        c.execute("DELETE FROM queue WHERE loop = 'race'")

out_a, err_a = p_a.communicate(timeout=10)
out_b, err_b = p_b.communicate(timeout=10)
assert p_a.returncode == 0 and p_b.returncode == 0, (err_a, err_b)
res_a, res_b = json.loads(out_a), json.loads(out_b)
assert counts_per_round == [1] * 20, counts_per_round
assert all(sorted((a, b))[0] == 0 and sorted((a, b))[1] > 0 for a, b in zip(res_a, res_b, strict=True))

# 3. Schedule-fired run killed while a second fire of same trigger waits -> 1 waiting row after reap, no error
marker = root / "sched_marker.txt"
setup_env({
    "ticker": {
        "script": (
            "from pathlib import Path\n"
            "import os, time\n"
            f"Path({str(marker)!r}).write_text(os.environ.get('ARETE_RUN_ID', ''), encoding='utf-8')\n"
            "time.sleep(60)\n"
        ),
    },
})
first_sched = loop_store.enqueue("ticker", "ticker", "", "every", [])
proc = spawn_carry_process("ticker", first_sched)
wait_for(lambda: marker.read_text(encoding="utf-8").strip() if marker.exists() else "")
twin_sched = loop_store.enqueue("ticker", "ticker", "", "every", [])
assert twin_sched > 0 and twin_sched != first_sched
os.kill(proc.pid, signal.SIGKILL)
proc.wait(timeout=5)
with mock.patch.object(record, "SETTLE_GRACE_SECONDS", 0):
    record.reap(tickets=False)
waiting_ticker = [row for row in loop_store.pending() if row["loop"] == "ticker" and row["trigger"] == "every"]
assert len(waiting_ticker) == 1 and waiting_ticker[0]["id"] == twin_sched, waiting_ticker

print("BEHAVIOR_RESULT=" + json.dumps({
    "deduped_id": low_id,
    "rounds": len(counts_per_round),
    "twin_survived": waiting_ticker[0]["id"] == twin_sched,
}))
""")
        self.assertEqual(result["rounds"], 20)
        self.assertTrue(result["twin_survived"])

    def test_never_began_across_server_restart_and_stage_claims(self):
        result = _run_behavior(r"""
setup_env({
    "fastfail": {
        "script": "import sys; sys.exit(0)\n",
    },
})
ev = [{"type": "t", "source": "s", "key": "k", "title": "ev", "trust": "internal", "data": {}}]
item_id = loop_store.enqueue("fastfail", "fastfail", "", "event", ev)

def failing_detach(*_args, **_kwargs):
    return subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"], env={k: os.environ[k] for k in os.environ})

# Process 1: launch raises NeverBegan -> reap leaves attempts == 1
with mock.patch.object(control, "detach", side_effect=failing_detach):
    try:
        loop_run.launch("fastfail", item=item_id, detach=True)
        raise AssertionError("expected NeverBegan")
    except loop_run.NeverBegan:
        pass
with mock.patch.object(record, "SETTLE_GRACE_SECONDS", 0):
    record.reap(tickets=False)
row1 = loop_store.queue_item(item_id)
assert row1["run_id"] is None and row1["dead_at"] is None and row1["attempts"] == 1

# Process 2: builds a new Server(), has no restarted attr, meets same failure -> dead with 1 queue.dead item
pkg_root = str(Path(loop_run.__file__).resolve().parent.parent)
second_proc_code = f'''
import json, os, subprocess, sys
sys.path.insert(0, {pkg_root!r})
from unittest import mock
from arete import control, inbox, loop_run, loops, record, serve
from arete.store import loops as loop_store

srv = serve.Server()
assert not hasattr(srv, "restarted")
srv.load()
by_name = {{x.name: x for x in srv.every_loop}}

def failing_detach(*_args, **_kwargs):
    return subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"], env={{k: os.environ[k] for k in os.environ}})

with mock.patch.object(control, "detach", side_effect=failing_detach):
    srv.dispatch(by_name)
srv.guard("stage:claims", srv.release_claims)
assert "stage:claims" not in srv.failing

row2 = loop_store.queue_item({item_id})
assert row2["dead_at"] is not None
items = inbox.list(state="open", kind="queue.dead")
assert len(items) == 1 and items[0]["subject"] == "queue:{item_id}"
print(json.dumps({{"has_restarted": hasattr(srv, "restarted"), "dead": True, "inbox_count": len(items)}}))
'''
proc2 = subprocess.run(
    [sys.executable, "-IB", "-c", second_proc_code],
    env={k: os.environ[k] for k in os.environ},
    capture_output=True,
    text=True,
    timeout=15,
    check=False,
)
assert proc2.returncode == 0, (proc2.stdout, proc2.stderr)
p2_res = json.loads(proc2.stdout.strip().splitlines()[-1])

print("BEHAVIOR_RESULT=" + json.dumps({
    "first_attempts": row1["attempts"],
    **p2_res,
}))
""")
        self.assertEqual(result["first_attempts"], 1)
        self.assertFalse(result["has_restarted"])
        self.assertTrue(result["dead"])
        self.assertEqual(result["inbox_count"], 1)


if __name__ == "__main__":
    unittest.main()
