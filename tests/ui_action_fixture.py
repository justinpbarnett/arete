import argparse
import os
from pathlib import Path
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


def server(port=0, live=False):
    from arete import dashboard, state

    state.ensure_schema()

    class Handler(dashboard.Handler):
        def body(self):
            if not hasattr(self, "payload"):
                self.payload = super().body()
            return self.payload

        def api(self, method, path, query):
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

    node = dashboard.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "fixture@example.com", "")
    owner = type("Owner", (), {"of": lambda self, address: node.owner})()
    instance = dashboard.Server("127.0.0.1", port, node, owner, False, handler=Handler)
    instance.requests, instance.next_response, instance.sequence = [], None, 0
    instance.fixture_lock = threading.Lock()
    instance.reads = []
    instance.control = {"scenario": "empty", "revision": 1, "age_seconds": 0, "delay": 0, "status": 503}
    return instance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="arete-ui-actions-", dir=os.environ.get("ARETE_VERIFY_SCRATCH")) as directory:
        root = Path(directory).resolve()
        for name in ("config", "state", "log", "worktrees"):
            folder = root / name
            folder.mkdir()
            os.environ[f"ARETE_{name.upper()}_DIR"] = str(folder)
        instance = server(args.port, args.live)
        print(f"http://127.0.0.1:{instance.server_port}/ui.html", flush=True)
        try:
            instance.serve_forever()
        finally:
            instance.server_close()


if __name__ == "__main__":
    main()
