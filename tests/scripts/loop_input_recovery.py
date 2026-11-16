
import contextlib, io, json, os, signal, subprocess, sys, time
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete import cli, config, control, loop_run, loops, paths, record, sandbox, scratch, serve, state, util
root, mode = Path(sys.argv[1]), sys.argv[2]
sandbox.AUTHORITY = root / "authority-parent/verification-authority"
paths.ensure_dirs()
state.migrate()
paths.CONFIG_FILE.write_text('[jev]\nenabled = false\n')
event = {"type": "test", "title": "keep me", "data": {"value": 17}, "trust": "internal"}
def folder(name, code):
    p = paths.CONFIG_DIR / "loops" / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "loop.toml").write_text('kind = "command"\nenabled = true\n[run]\ncommand = ' +
                               json.dumps([sys.executable, "-c", code]) + '\n')
folder("fail", "raise SystemExit(1)")
folder("ok", "import sys; from pathlib import Path; Path(" + repr(str(root / "received")) +
       ").write_text(sys.stdin.read()); print('received input')")
folder("silent", "pass")
folder("slow", "import os,time; from pathlib import Path; Path(" + repr(str(root / "ready")) +
       ").write_text(str(os.getpid())); time.sleep(60)")
def enqueue(name="fail", payload=None, trigger="event"):
    return state.enqueue(name, name, "", trigger, [event] if payload is None else payload)
def call(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main(["loop", *args])
    return code, out.getvalue()
def wait(test):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        if test():
            return
        time.sleep(.03)
    raise AssertionError("timed out waiting for command recovery")
def reap():
    with mock.patch.object(record, "SETTLE_GRACE_SECONDS", 0):
        record.reap(tickets=False)
def start(item):
    (root / "ready").unlink(missing_ok=True)
    r = loop_run.launch("slow", item=item)
    wait(lambda: (root / "ready").exists() and (root / "ready").read_text().strip())
    return r, int((root / "ready").read_text())
def kill(r, child):
    os.kill(r["pid"], signal.SIGKILL)
    wait(lambda: not util.lock_is_held(r["lock"]))
    with contextlib.suppress(ProcessLookupError):
        os.kill(child, signal.SIGKILL)
if mode == "outcomes":
    import sqlite3
    with sqlite3.connect(paths.DB_FILE) as c:
        c.execute("INSERT INTO queue (loop, queue, trigger, payload, enqueued) VALUES ('fail', 'fail', 'event', ?, ?)",
                  (json.dumps([event]), util.iso()))
        legacy = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    for _ in range(2):
        row = state.queue_item(legacy)
        assert row["payload"] == [event] and row["attempts"] == 0
        assert row["dead_at"] is None and row["dead_reason"] == ""
    state.drop_queue_item(legacy)
    item = enqueue()
    for _ in range(2):
        with state.db() as c:
            row = dict(c.execute("SELECT * FROM queue WHERE id = ?", (item,)).fetchone())
        assert (row["attempts"], row["dead_at"], row["dead_reason"]) == (0, None, "")
    r = loop_run.launch("fail", item=item, detach=False)
    assert r["status"] == "failed" and not state.pending()
    for args in (("list",), ("status", "fail")):
        rc, shown = call(*args)
        assert rc == 0 and "1 dead" in shown, shown
    assert str(item) in shown and r["id"] in shown and "1 event" in shown and "exit 1" in shown, shown
    assert call("requeue", str(item))[0] == 0
    row = state.queue_item(item)
    assert row["payload"] == [event] and row["run_id"] is None and row["attempts"] == 0
    assert row["dead_at"] is None and row["dead_reason"] == ""
    for invalid in (str(item), "9999999", "bad"):
        assert call("requeue", invalid)[0] != 0
    folder("fail", "import sys; from pathlib import Path; Path(" + repr(str(root / "requeued")) +
           ").write_text(sys.stdin.read()); print('received requeued input')")
    assert loop_run.launch("fail", item=item, detach=False)["status"] == "done"
    assert json.loads((root / "requeued").read_text()) == event
    folder("fail", "raise SystemExit(1)")
    item = enqueue("ok")
    assert loop_run.launch("ok", item=item, detach=False)["status"] == "done"
    assert state.queue_item(item) is None
    assert json.loads((root / "received").read_text()) == event
    item = enqueue(payload=[], trigger="every")
    loop_run.launch("fail", item=item, detach=False)
    assert state.queue_item(item) is None
    assert loop_run.launch("ok", detach=False)["status"] == "done"
    item = enqueue("silent")
    with mock.patch.object(loop_run.delivery, "deliver", side_effect=AssertionError("silent command delivered")), \
            mock.patch.object(loop_run.runner, "run", side_effect=AssertionError("silent command called a model")):
        silent = loop_run.launch("silent", item=item, detach=False)
    assert silent["status"] == "skipped" and "no output" in silent["summary"], silent
    assert state.queue_item(item) is None
    item = enqueue("ok")
    assert state.take_queue_item(item, "first") is True
    assert state.take_queue_item(item, "second") is False
    assert state.queue_item(item)["run_id"] == "first"
    assert call("requeue", str(item))[0] != 0
    assert state.queue_item(item)["run_id"] == "first"
    (root / "received").unlink()
    for value in (item, 999999):
        try:
            loop_run.launch("ok", item=value, detach=False)
        except control.NotFound:
            pass
        else:
            raise AssertionError("claimed or missing item launched")
    item = enqueue("ok")
    take = state.take_queue_item
    def compete(item, run_id):
        assert take(item, "competitor")
        return take(item, run_id)
    with mock.patch.object(state, "take_queue_item", side_effect=compete):
        try:
            loop_run.launch("ok", item=item, detach=False)
        except control.NotFound as e:
            assert "was taken by another run" in str(e)
        else:
            raise AssertionError("lost claim launched")
    assert not (root / "received").exists()
    assert state.runs(kind="loop", loop="ok", firings=True)[0]["status"] == "skipped"
    item = enqueue("ok")
    with mock.patch.object(loop_run, "spent_limit", return_value="daily budget"):
        loop_run.launch("ok", item=item, detach=False)
    assert state.queue_item(item)["run_id"] is None and state.queue_item(item)["attempts"] == 0
    with mock.patch.object(loop_run, "run_command", side_effect=loop_run.OverBudget("during execution")):
        loop_run.launch("ok", item=item, detach=False)
    assert "during execution" in state.queue_item(item)["dead_reason"]
    item = enqueue("ok")
    with mock.patch.object(loop_run, "screened", return_value=("skipped", "kind skipped")):
        loop_run.launch("ok", item=item, detach=False)
    assert state.queue_item(item) is None
elif mode == "signals":
    item = enqueue("slow")
    for attempt in (1, 2):
        r, child = start(item)
        try:
            kill(r, child)
            reap()
            row = state.queue_item(item)
            assert row["payload"] == [event]
            if attempt == 1:
                assert row["run_id"] is None and row["attempts"] == 1
            else:
                assert row["run_id"] == r["id"] and row["dead_at"] and row["dead_reason"]
                reap()
                state.prune()
                assert state.queue_item(item) == row
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
    for action in ("stop", "gone", "term", "empty"):
        item = enqueue("slow", payload=[] if action == "empty" else None, trigger="every")
        r, child = start(item)
        try:
            if action == "empty":
                newer = enqueue("slow", payload=[], trigger="every")
                other = enqueue("slow", payload=[], trigger="cron")
            if action in ("gone", "empty"):
                kill(r, child)
            if action in ("stop", "gone"):
                control.stop_run(r["id"])
            elif action == "term":
                os.kill(r["pid"], signal.SIGTERM)
            wait(lambda: not util.lock_is_held(r["lock"]))
            reap()
            row = state.queue_item(item)
            if action in ("stop", "gone"):
                assert row["dead_at"] and row["run_id"] == r["id"], row
            elif action == "term":
                assert row["run_id"] is None and row["attempts"] == 1, row
            else:
                assert row is None
                assert {i["id"] for i in state.pending() if i["loop"] == "slow"} >= {newer, other}
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
elif mode == "retention":
    old = util.iso(util.now() - timedelta(days=60))
    ids = {}
    for status in ("running", "failed", "missing", "stopped"):
        item = ids[status] = enqueue()
        if status != "missing":
            record.start("loop", run_id=status, loop="fail")
            if status != "running":
                record.settle(status, status, summary="settled " + status)
        state.take_queue_item(item, status)
    reap()
    for status in ("failed", "missing"):
        row = state.queue_item(ids[status])
        assert row["run_id"] is None and row["attempts"] == 1
    assert state.queue_item(ids["stopped"])["dead_at"]
    before = [state.queue_item(i) for i in ids.values()]
    reap()
    assert before == [state.queue_item(i) for i in ids.values()]
    waiting = enqueue()
    with state.db() as c:
        c.execute("UPDATE queue SET enqueued = ?", (old,))
        c.execute("INSERT INTO events (loop, type, ts) VALUES ('fail', 'old', ?)", (old,))
    state.prune()
    assert all(state.queue_item(i) for i in [*ids.values(), waiting])
    with state.db() as c:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        c.execute("UPDATE queue SET dead_at = ? WHERE id = ?", (old, ids["stopped"]))
    state.prune()
    assert state.queue_item(ids["stopped"]) is None
elif mode == "never":
    pending = state.pending()
    item = pending[0]["id"] if pending else enqueue()
    attempt = state.queue_item(item)["attempts"]
    def fails(args, keep=()):
        return control.launch([sys.executable, "-c", "raise SystemExit(7)"], keep=keep)
    server = serve.Server()
    try:
        with mock.patch.object(control, "detach", side_effect=fails):
            server.dispatch({"fail": loops.find("fail", config.load())})
        reap()
        row = state.queue_item(item)
        assert (row["dead_at"] is not None) == (attempt == 1), row
        assert row["payload"] == [event]
        reap()
        assert state.queue_item(item) == row
    finally:
        server.pool.shutdown()
        for pool in server.job_pools.values():
            pool.shutdown()
elif mode == "race":
    state.ensure_schema()
    code = "import sys,time; from pathlib import Path; from arete import state; "
    code += "p=Path(sys.argv[1]); Path(sys.argv[2]).touch(); "
    code += "\nwhile not p.exists(): time.sleep(.001)\n"
    code += "print(state.enqueue('race','race','','every'))"
    prefix = "import sys; sys.path.insert(0," + repr(str(paths.PACKAGE_DIR.parent)) + ");"
    for n in range(20):
        gate = root / str(n)
        ready = [root / f"{n}-{i}.ready" for i in range(2)]
        procs = [subprocess.Popen([sys.executable, "-I", "-c", prefix + code, str(gate), str(marker)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for marker in ready]
        try:
            wait(lambda: all(marker.exists() for marker in ready))
            gate.touch()
            results = [p.communicate(timeout=15) for p in procs]
            assert all(p.returncode == 0 for p in procs), results
            assert sum(int(out.strip()) == 0 for out, err in results) == 1, results
            rows = [i for i in state.pending() if i["loop"] == "race"]
            assert len(rows) == 1
            state.drop_queue_item(rows[0]["id"])
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()
    assert enqueue() != enqueue()
print("RECOVERY_OK")
