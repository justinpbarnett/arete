
import http.client
import http.server
import socket
import subprocess

from arete import dashboard, delivery

state.migrate()
OWNER = "owner@example.test"
CHANGED = frozenset({("changed", 1, 1)})
real_sh = util.sh
who = [OWNER]
dashboard.Logins.of = lambda self, address: who[0]
NODE = dashboard.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), OWNER, "127.0.0.1")
def listen(logins, handler):
    web = dashboard.Server("127.0.0.1", 0, NODE, logins, True, handler=handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    return web
web = listen(dashboard.Logins(None), dashboard.Handler)
PORT = web.server_address[1]
gate, sending = threading.Event(), threading.Event()
def fake_notify(item, batch, loop, run, cfg):
    sending.set()
    if not gate.wait(20):
        raise TimeoutError("fixture gate never opened")
    return "sent"
delivery.EMITS["ticket"].execute = fake_notify
def approval():
    return state.add_approval("demo", "", {"type": "ticket", "item": {"title": "t", "message": "m"}}, "summary")
def status_of(n):
    return state.approval(n)["status"]
def post(n, body=b'{"approve": true}', host="127.0.0.1", port=None, header=("X-Arete", "1"), extra=None):
    conn = http.client.HTTPConnection("127.0.0.1", port or PORT, timeout=20)
    conn.request("POST", f"/api/approvals/{n}", body, {"Host": host, **dict([header] if header else []), **(extra or {})})
    resp = conn.getresponse()
    answer = {"status": resp.status, "retry": resp.getheader("Retry-After"), "body": json.loads(resp.read())}
    conn.close()
    return answer
def page():
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    conn.request("GET", "/", headers={"Host": "127.0.0.1"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status
def raw(n, length):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    s.sendall(f"POST /api/approvals/{n} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Arete: 1\r\n{length}Connection: close\r\n\r\n".encode())
    data = b""
    while chunk := s.recv(4096):
        data += chunk
    s.close()
    head = data.split(b"\r\n\r\n")[0].decode()
    return {"status": int(head.split()[1]), "retry": "Retry-After: 3" in head}
def background(fn):
    box = {}
    def run():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = repr(e)
    box["thread"] = threading.Thread(target=run)
    box["thread"].start()
    return box
def finish(box):
    box["thread"].join(20)
    return box.get("value", box.get("error", "still running"))
def wait_for(ready):
    end = time.monotonic() + 10
    while not ready():
        if time.monotonic() > end:
            raise TimeoutError("fixture condition never held")
        time.sleep(0.01)
def reload_server():
    server, said = quiet_server()
    server.lock = util.lock_file(serve.SERVE_LOCK)
    server.code, server.code_seen = frozenset(), CHANGED
    return server, said
def passing(argv, **kw):
    if argv[-1] in (serve.IMPORT_CHECK, serve.CONFIG_CHECK):
        return subprocess.CompletedProcess(argv, 0, "", "")
    return real_sh(argv, **kw)
def attempt(server, sh, execv):
    try:
        with mock.patch.object(serve, "code_stamp", lambda: CHANGED), mock.patch.object(util, "sh", sh), \
                mock.patch.object(os, "execv", execv):
            server.load_new_code()
    except BaseException as e:
        return repr(e)
    finally:
        os.environ.pop(serve.SERVE_LOCK_FD, None)
        os.environ.pop(serve.RESTARTED_FOR_NEW_CODE, None)
    return ""
