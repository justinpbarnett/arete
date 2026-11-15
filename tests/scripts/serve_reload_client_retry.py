
RESTART = {"error": dashboard.RESTARTING}
class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
def scripted(replies):
    seen = []
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.rfile.read(int(self.headers.get("Content-Length") or 0))))
            status, headers, body = replies[min(len(seen) - 1, len(replies) - 1)]
            data = json.dumps(body).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen
def dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
def live(srv, fn=None):
    calls = []
    def where():
        calls.append(1)
        port = srv.server_address[1] if fn is None or fn(len(calls)) else dead_port()
        return client.Server("http", "127.0.0.1", port, "127.0.0.1")
    return where
def go(replies, fn=None, clock=None, path="/probe"):
    srv, seen = scripted(replies)
    client.server = live(srv, fn)
    out = {}
    began = time.monotonic()
    try:
        if clock:
            with mock.patch.object(client, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)):
                out["answer"] = client.call("POST", path, {"value": 1})
        else:
            out["answer"] = client.call("POST", path, {"value": 1})
    except client.Failed as e:
        out["failed"] = {"code": e.code, "text": str(e)}
    except client.Down as e:
        out["down"] = str(e)
    srv.shutdown()
    return {**out, "requests": len(seen), "same": len(set(seen)) == 1, "elapsed": time.monotonic() - began}
OK = (200, {}, {"ok": True})
r503 = lambda after: (503, {"Retry-After": after}, RESTART)
clock = Clock()
repeated = go([r503("10")], clock=clock)
repeated["slept"] = clock.slept
clock = Clock()
expiry = go([r503("10"), r503("10"), r503("10"), OK], clock=clock)
expiry["slept"] = clock.slept
clock = Clock()
dropped = go([r503("1"), OK], fn=lambda n: n != 2, clock=clock)
dropped["slept"] = clock.slept
clock = Clock()
never = go([r503("1")], fn=lambda n: n == 1, clock=clock)
never["slept"] = clock.slept
clock = Clock()
client.server = lambda: client.Server("http", "127.0.0.1", dead_port(), "127.0.0.1")
try:
    with mock.patch.object(client, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)):
        client.call("POST", "/probe", {})
    initial = "no error"
except client.Down:
    initial = "down"
result(again=go([r503("0"), OK]), slow=go([r503("1"), OK]), none=go([(503, {}, RESTART)]),
       invalid=go([(503, {"Retry-After": "soon"}, RESTART)]), other=go([(409, {"Retry-After": "1"}, {"error": "Busy."})]),
       repeated=repeated, expiry=expiry, dropped=dropped, never=never, initial=initial, initial_sleeps=clock.slept)
