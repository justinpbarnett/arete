import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time


LIVE_PATHS = ("/api/sidebar", "/api/live")


def live_rows(revision, count):
    return [{"id": f"run-{n}", "kind": "pipeline", "ticket": f"ARE-{n}", "repo": "arete", "started": "2026-01-01T00:00:00Z",
             "seconds": n * 10 + revision, "step": f"step {revision} of {n}", "slot": "", "paid": 0, "stopping": False}
            for n in range(1, count + 1)]


def live_answer(path, control):
    from arete import util

    populated = control["scenario"] == "populated"
    runs = live_rows(control["revision"], 40) if populated else []
    ready = [{"ticket": f"ARE-{n}"} for n in (1, 2)] if populated else []
    if path == "/api/sidebar":
        serve = {"at": util.iso(), "age_seconds": control["age_seconds"], "alive": True, "started": util.iso()}
        return {"now": util.iso(), "needs_you": len(ready), "running": len(runs), "questions": 0, "health": "ok",
                "serve": serve, "chat": {"busy": False, "draft": False}}
    asks = {"approvals": {"rows": []}, "skipped": {"rows": []}, "proposals": {"rows": {"memory": [], "hand": [], "approve": []}},
            "ready": {"rows": ready}, "draft": None}
    return {"now": util.iso(), "live": {"rows": {"runs": runs, "slots": len(runs), "waiting": 0, "max_parallel": 4}},
            "waiting": {"rows": {"tickets": [], "quiet_minutes": 5}}, "needs_you": asks}


RUN_PATHS = ("/api/runs", "/api/feed/", "/api/log/")
RUN_ACTION = re.compile(r"/api/runs/([^/]+)/(resume|repair)")
NOTHING = object()
SLEEPER = "import time; time.sleep(3600)"


def sleeper():
    return subprocess.Popen([sys.executable, "-c", SLEEPER], start_new_session=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def ago(minutes):
    from datetime import timedelta
    from arete import util

    return util.iso(util.now() - timedelta(minutes=minutes))


def add_run(run_id, kind="pipeline", status="running", minutes=10, issue=None, pid=None, **fields):
    from arete.store import db as foundation

    row = {"id": run_id, "kind": kind, "repo": "arete", "issue": issue, "status": status, "started": ago(minutes),
           "finished": None if status == "running" else ago(max(minutes - 5, 0)), "pid": pid, **fields}
    with foundation.db() as c:
        c.execute(f"INSERT INTO runs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def add_usage(run_id, step, tokens_in, tokens_out, cost):
    from arete import util
    from arete.store import db as foundation

    with foundation.db() as c:
        c.execute("INSERT INTO steps (run_id, step, runner, model, provider, auth_mode, ok, cost_usd, tokens_in, tokens_out, turns, "
                  "seconds, ts, try) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (run_id, step, "claude", "sonnet", "anthropic", "api_key", 1, cost, tokens_in, tokens_out, 3, 12.0, util.iso(), 1))


def step_events(run_id, steps, report=None):
    from arete import run_lines

    for name, attempt, result, error in steps:
        run_lines.write(run_id, "step_start", step=name, model="sonnet", runner="claude", **{"try": attempt})
        if result:
            run_lines.write(run_id, "step_end", step=name, model="sonnet", runner="claude", result=result, ok=result == "pass", cost_usd=0.12,
                        tokens_in=1200, tokens_out=300, turns=3, seconds=12.5, error=error, **{"try": attempt})
        if error and result == "fail":
            run_lines.write(run_id, "error", step=name, message=error)
    if report:
        run_lines.write(run_id, "report", status=report)


def seed_runs(instance):
    from arete.store import runs as run_store
    from arete import paths

    ids = {}
    producers = instance.producers

    def alive():
        process = sleeper()
        producers.append(process)
        return process

    def named(label, run_id):
        ids[label] = run_id
        return run_id

    finished = named("finished", "fix-finished")
    add_run(finished, status="pr", minutes=60, issue=11, summary="Add the shared list header", pr_url="https://example.com/pull/11")
    step_events(finished, [("prepare", 1, "pass", ""), ("build", 1, "fail", "tests failed <b>twice</b>"), ("build", 2, "pass", "")], "pr")
    add_usage(finished, "build", 2400, 600, 0.24)
    running = alive()
    add_run(named("running", "fix-running"), status="running", minutes=5, issue=12, pid=running.pid, summary="Run the live build")
    step_events("fix-running", [("prepare", 1, "pass", ""), ("build", 1, None, "")])
    add_usage("fix-running", "prepare", 800, 200, 0.08)
    feeding = alive()
    add_run(named("feed", "fix-feed"), kind="loop", status="running", minutes=4, pid=feeding.pid, loop="fixture-loop", summary="Watch the feed")
    for text in ("started the loop", "line one\nline two\nline three", "<script>alert(1)</script> is literal"):
        run_store.add_feed("fix-feed", "note", text)
    add_run(named("failed", "fix-failed"), status="failed", minutes=30, issue=13, summary="Break the build")
    step_events("fix-failed", [("prepare", 1, "pass", ""), ("build", 1, "fail", "AssertionError: tests failed")])
    run_store.add_feed("fix-failed", "end", "failed: tests failed")
    add_run(named("waiting", "fix-waiting"), status="needs-info", minutes=20, issue=14, summary="Needs an answer")
    step_events("fix-waiting", [("prepare", 1, "pass", ""), ("plan", 1, "ask", "")])
    add_run(named("interrupted", "fix-interrupted"), status="interrupted", minutes=25, issue=15, summary="Cut short")
    step_events("fix-interrupted", [("prepare", 1, "pass", "")])
    add_run(named("stopped", "fix-stopped"), status="stopped", minutes=26, issue=16, summary="Stopped by hand")
    step_events("fix-stopped", [("prepare", 1, "pass", "")])
    add_run(named("plain", "fix-plain"), kind="miner", status="failed", minutes=40, summary="Miner failed")
    log = paths.RUNS_DIR / "fix-plain" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"2026-11-15T09:00:{n % 60:02d}Z mined item {n}\n" for n in range(60)))
    gone = alive()
    add_run(named("gone", "fix-gone"), status="running", minutes=15, issue=17, pid=gone.pid, summary="Process already gone")
    step_events("fix-gone", [("prepare", 1, None, "")])
    gone.kill()
    gone.wait()
    for label in ("running", "feed"):
        watch(ids[label], running if label == "running" else feeding)
    return ids


def watch(run_id, process):
    from arete.store import runs as run_store
    from arete import record

    def settle():
        process.wait()
        if row := run_store.run_get(run_id):
            record.interrupt(row)

    threading.Thread(target=settle, daemon=True).start()


def continuation(instance, row):
    from arete import record

    with instance.fixture_lock:
        instance.sequence += 1
        new = f"{row['id']}-next-{instance.sequence}"
    process = sleeper()
    instance.producers.append(process)
    add_run(new, status="running", minutes=0, issue=row["issue"], pid=process.pid, summary=f"Continues {row['id']}")
    step_events(new, [("build", 1, None, "")])
    watch(new, process)
    return new


def fault_for(control, target):
    fault = control.get("faults", {}).get(target)
    return fault if fault and fault.get("mode") != "none" else None


def run_answer(handler, method, path, query):
    from arete.store import runs as run_store
    from arete import control, dashboard, record, tasks

    server = handler.server
    if path == "/__ui_runs/requests" and method == "GET":
        with server.fixture_lock:
            return list(server.run_requests)
    if path == "/__ui_runs/control" and method == "POST":
        data = handler.body()
        action = data.get("action")
        if action == "append-feed":
            run_store.add_feed(server.ids["feed"], "note", str(data.get("text", "")))
        elif action == "fault":
            with server.fixture_lock:
                faults = {**server.run_control.get("faults", {}), str(data.get("target")): data}
                server.run_control = {**server.run_control, "faults": faults}
        elif action == "settle-gone":
            record.interrupt(run_store.run_get(data.get("id", server.ids["gone"])))
        elif action == "reset":
            with server.fixture_lock:
                server.run_control = {}
                server.run_requests.clear()
        else:
            raise dashboard.Refusal(400, "Invalid fixture action")
        return {"configured": True}
    if not path.startswith(RUN_PATHS):
        return NOTHING
    entry = {"method": method, "url": path, "query": query, "at": time.time(), "guard": handler.headers.get("X-Arete"),
             "idempotency_key": handler.headers.get("Idempotency-Key"), "body": handler.body() if method == "POST" else None}
    with server.fixture_lock:
        server.run_requests.append(entry)
        control_state = dict(server.run_control)
    target = "queue" if path == "/api/runs" else "detail" if method == "GET" and path.count("/") == 3 and path.startswith("/api/runs/") else None
    if fault := fault_for(control_state, target):
        time.sleep(float(fault.get("delay", 0)))
        if fault["mode"] == "error":
            raise dashboard.Refusal(int(fault.get("status", 503)), "fixture response")
        if fault["mode"] == "malformed":
            handler.malformed = True
            return {}
        if fault["mode"] == "empty":
            return {"now": "", "runs": [], "more": False, "facets": {}}
    if (action := RUN_ACTION.fullmatch(path)) and method == "POST":
        row = run_store.run_get(action[1])
        if row and row["kind"] == "pipeline":
            if row["status"] == "running":
                raise control.Refused(f"run {row['id']} is running; stop it first")
            if action[2] == "resume" and row["status"] not in tasks.RESUMABLE:
                raise control.Refused(f"run {row['id']} ended {row['status']}; a run resumes only when it ended interrupted, stopped or failed")
            return {"run": continuation(handler.server, row), "state": "running", "why": "started"}
    return NOTHING


def server(port=0, live=False, runs=False):
    from arete import dashboard, tailnet
    from arete.store import db as foundation

    foundation.migrate()

    class Handler(dashboard.Handler):
        def body(self):
            if not hasattr(self, "payload"):
                self.payload = super().body()
            return self.payload

        def send_events(self, run_id, query):
            if runs:
                with self.server.fixture_lock:
                    self.server.run_requests.append({"method": "GET", "url": f"/api/runs/{run_id}/events", "query": query, "at": time.time(),
                                                     "guard": None, "idempotency_key": None, "body": None})
                    fault = fault_for(self.server.run_control, "events")
                if fault and fault["mode"] == "error":
                    raise dashboard.Refusal(int(fault.get("status", 503)), "fixture response")
                if fault and fault["mode"] == "malformed":
                    return self.send(200, b"{not json\n", "application/x-ndjson", "no-store")
            return super().send_events(run_id, query)

        def api(self, method, path, query):
            if runs and (found := run_answer(self, method, path, query)) is not NOTHING:
                return found
            if runs and method == "POST" and path.startswith("/api/runs/"):
                return super().api(method, path, query)
            if live and method == "GET" and path == "/__ui_live/requests":
                with self.server.fixture_lock:
                    return list(self.server.reads)
            if live and method == "POST" and path == "/__ui_live/control":
                data = self.body()
                scenario = data.get("scenario", "empty")
                if scenario not in ("empty", "populated", "error", "malformed", "delay"):
                    raise dashboard.Refusal(400, "Invalid fixture scenario")
                with self.server.fixture_lock:
                    self.server.control = {**self.server.control, **data, "scenario": scenario}
                return {"configured": True}
            if live and method == "GET" and path in LIVE_PATHS:
                with self.server.fixture_lock:
                    self.server.reads.append({"path": path, "at": time.time()})
                    control = dict(self.server.control)
                if control["scenario"] == "delay":
                    time.sleep(float(control["delay"]))
                if control["scenario"] == "error":
                    raise dashboard.Refusal(int(control["status"]), "fixture response")
                if control["scenario"] == "malformed":
                    self.malformed = True
                    return {}
                return live_answer(path, control)
            if method == "GET" and path == "/__ui_actions/requests":
                with self.server.fixture_lock:
                    return list(self.server.requests)
            if method == "POST" and path == "/__ui_actions/control":
                data = self.body()
                status = int(data.get("status", 200))
                delay = float(data.get("delay", 0))
                if not 200 <= status <= 599 or not 0 <= delay <= 10:
                    raise dashboard.Refusal(400, "Invalid fixture response")
                with self.server.fixture_lock:
                    self.server.next_response = {**data, "status": status, "delay": delay}
                return {"configured": True}
            if method != "POST":
                return super().api(method, path, query)
            data = self.body()
            entry = {"method": method, "url": path, "guard": self.headers.get("X-Arete"),
                     "idempotency_key": self.headers.get("Idempotency-Key"), "body": data,
                     "serialized_body": self.serialized_body}
            with self.server.fixture_lock:
                self.server.requests.append(entry)
                synthetic = path.startswith("/__ui_actions/items/")
                response = self.server.next_response if synthetic or path == "/api/checks/run" else None
                if response:
                    self.server.next_response = None
            if response:
                time.sleep(response["delay"])
                if response["status"] != 200 or "body" in response:
                    entry.update(status=response["status"], response=response.get("body", {}))
                    self.fixture_response = response["status"], response.get("body", {})
                    raise dashboard.Refusal(response["status"], "fixture response")
            if synthetic:
                with self.server.fixture_lock:
                    self.server.sequence += 1
                    result = {"id": data.get("id") or f"new-{self.server.sequence}",
                              "version": data.get("base_version", 0) + 1}
            else:
                result = super().api(method, path, query)
            entry.update(status=200, response=result)
            return result

        def raw_body(self, limit=dashboard.MAX_BODY):
            raw = super().raw_body(limit)
            self.serialized_body = raw.decode()
            return raw

        def send_json(self, status, data):
            if hasattr(self, "malformed"):
                return self.send(200, b"{not json", "application/json", "no-store")
            if hasattr(self, "fixture_response"):
                status, data = self.fixture_response
            super().send_json(status, data)

    node = tailnet.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "fixture@example.com", "")
    owner = type("Owner", (), {"of": lambda self, address: node.owner})()
    instance = tailnet.Server("127.0.0.1", port, node, owner, False, handler=Handler, redirect=dashboard.Redirect)
    instance.requests, instance.next_response, instance.sequence = [], None, 0
    instance.fixture_lock = threading.Lock()
    instance.reads = []
    instance.control = {"scenario": "empty", "revision": 1, "age_seconds": 0, "delay": 0, "status": 503}
    instance.producers, instance.run_requests, instance.run_control, instance.ids = [], [], {}, {}
    if runs:
        instance.ids = seed_runs(instance)
    return instance


def close_producers(instance):
    for process in instance.producers:
        if process.poll() is None:
            process.kill()
        process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--runs", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="arete-ui-actions-", dir=os.environ.get("ARETE_VERIFY_SCRATCH")) as directory:
        root = Path(directory).resolve()
        for name in ("config", "state", "log", "worktrees"):
            folder = root / name
            folder.mkdir()
            os.environ[f"ARETE_{name.upper()}_DIR"] = str(folder)
        instance = server(args.port, args.live, args.runs)
        base = f"http://127.0.0.1:{instance.server_port}/ui.html"
        print(base, flush=True)
        for label, run_id in instance.ids.items():
            print(f"{label}: {run_id} {base}#run/{run_id}", flush=True)
        try:
            instance.serve_forever()
        finally:
            instance.server_close()
            close_producers(instance)


if __name__ == "__main__":
    main()
