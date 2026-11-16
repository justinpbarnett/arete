
import hashlib, hmac, json, os, signal, sqlite3, sys, threading, urllib.error, urllib.request
from datetime import timedelta
from pathlib import Path
from arete.store import loops as loop_store, sources as source_store
from arete import store, config, dashboard, webhooks, loops, paths, serve, sources, state, tailnet, util
state.migrate()
root = Path(os.environ["INTAKE_ROOT"])
rows = root / "rows.jsonl"
def make_loop(name="intake", trigger="", webhook=False):
    folder = paths.CONFIG_DIR / "loops" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "loop.toml").write_text(
        'kind = "command"\n[run]\ncommand = "true"\n[trigger]\n' + ("webhook = true\n" if webhook else "") + trigger +
        f'[[source]]\ntype = "command"\ncommand = ["cat", "{rows}"]\nevery = "1m"\n')
    return next(x for x in loops.load_all() if x.name == name)
def write_rows(*items):
    rows.write_text("".join(json.dumps(i) + "\n" for i in items))
def issue(key, data=None):
    return {"key": key, "title": f"issue {key}", "data": data or {"number": key}}
def snap(sid="intake#0"):
    with state.db() as c:
        out = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("seen", "events", "queue")}
        out["queue_rows"] = [{"grp": r["grp"], "keys": [e["key"] for e in json.loads(r["payload"])]}
                             for r in c.execute("SELECT grp, payload FROM queue ORDER BY id")]
        cur = c.execute("SELECT value, ok, detail FROM cursors WHERE source = ?", (sid,)).fetchone()
        out["cursor"] = dict(cur) if cur else None
        owner = c.execute("SELECT fingerprint FROM source_owners WHERE source = ?", (sid,)).fetchone()
        out["owner"] = owner[0] if owner else None
    return out
def make_server():
    server = serve.Server()
    server.lines, server.ended = [], threading.Event()
    server.say = server.lines.append
    original = server.poll_ended
    def ended(sid, done):
        original(sid, done)
        server.ended.set()
    server.poll_ended = ended
    return server
def poll(server, loop, sid="intake#0"):
    server.tried.clear()
    server.ended.clear()
    server.poll(loop)
    future = server.polls[sid]
    error = future.exception(timeout=30)
    assert server.ended.wait(30)
    return error
def failing_once(real):
    left = [1]
    def flaky(*a, **k):
        if left:
            left.pop()
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    return flaky
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def web():
    node = tailnet.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "me@example.com", "")
    owner = type("Owner", (), {"of": lambda self, address: "me@example.com"})()
    server = tailnet.Server("127.0.0.1", 0, node, owner, False, dashboard.Handler, dashboard.Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
def post(server, name, body, headers):
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/hook/{name}", data=body,
                                     headers=headers, method="POST")
    try:
        got = OPENER.open(request, timeout=30)
    except urllib.error.HTTPError as e:
        got = e
    with got:
        return got.code, json.loads(got.read() or b"{}")
def signed(name, body, delivery):
    key = webhooks.secret(name)
    return {"X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()}
def token(name, **more):
    return {"X-Arete-Token": webhooks.secret(name), **more}
def finish(result):
    print("BEHAVIOR_RESULT=" + json.dumps(result))
