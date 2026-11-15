
import json, threading, urllib.request, urllib.error
from arete import codebase, control, dashboard, paths, serve, state, util
codebase.load()
state.ensure_schema()
serve.Server().beat([], [])
node = dashboard.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "me@example.com", "")
class Owner:
    def of(self, address):
        return "me@example.com"
server = dashboard.Server("127.0.0.1", 0, node, Owner(), False)
threading.Thread(target=server.serve_forever, daemon=True).start()
dashboard.record_server(node, server.server_port, dashboard.Front("http", [server]))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
heads = []
for path in ("/", "/nothing-here"):
    try:
        r = opener.open(f"http://127.0.0.1:{server.server_port}{path}")
    except urllib.error.HTTPError as e:
        r = e
    heads.append(r.headers.get_all("X-Arete-Engine"))
print(json.dumps({"stamp": codebase.STAMP, "beat": util.read_json(control.BEAT_FILE, {}).get("engine"),
                  "file": util.read_json(paths.SERVER_FILE, {}).get("engine"), "heads": heads}))
