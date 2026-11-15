
from arete import dashboard

write(BAD)
server, said = quiet_server()
server.run_pass()
class Host(dashboard.ScratchHost):
    def node(self, owner):
        return dashboard.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "me@example.com", "")
    def whois(self, address):
        return "me@example.com"
import socket

probe = socket.socket()
probe.bind(("127.0.0.1", 0))
port = probe.getsockname()[1]
probe.close()
threading.Thread(target=dashboard.serve, kwargs={"port": port, "inside_serve": True, "host": Host()}, daemon=True).start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
answers = {}
deadline = time.monotonic() + 40
while len(answers) < 2 and time.monotonic() < deadline:
    try:
        for route in ("/", "/api/health"):
            with opener.open(f"http://127.0.0.1:{port}{route}", timeout=5) as r:
                answers[route] = [r.status, r.read().decode()]
    except OSError:
        time.sleep(.5)
health = json.loads(answers["/api/health"][1]) if "/api/health" in answers else {}
result(status={k: v[0] for k, v in answers.items()}, state=health.get("state"),
       problem=(health.get("serve") or {}).get("config_problem", ""))
