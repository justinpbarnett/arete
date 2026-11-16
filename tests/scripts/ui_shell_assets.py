
import json, re, threading, unittest, urllib.request
from html.parser import HTMLParser
from pathlib import Path
from arete import dashboard, state, tailnet

class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.styles, self.declared = set(), set(), set()
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.declared.update(value for name, value in attrs.items() if name in ("src", "href") and value.startswith("/"))
        if tag == "script" and attrs.get("src"):
            self.scripts.add(attrs["src"])
        if tag == "link" and attrs.get("rel") == "modulepreload":
            self.scripts.add(attrs["href"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.styles.add(attrs["href"])

check = unittest.TestCase()
state.migrate()
node = tailnet.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "ui@example.com", "")
owner = type("Owner", (), {"of": lambda self, address: "ui@example.com"})()
server = tailnet.Server("127.0.0.1", 0, node, owner, False, dashboard.Handler, dashboard.Redirect)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = f"http://127.0.0.1:{server.server_port}"
def get(path, mime):
    with opener.open(base + path, timeout=5) as response:
        check.assertEqual(response.status, 200)
        check.assertEqual(response.headers.get_content_type(), mime)
        check.assertEqual(response.headers["Content-Security-Policy"], dashboard.PAGE_POLICY)
        return response.read().decode()
try:
    page = get("/ui.html", "text/html")
    check.assertIsNone(re.search(r"\bstyle\s*=", page, re.I))
    assets = Assets()
    assets.feed(page)
    check.assertEqual(assets.scripts, {
        "/ui-boot.js", "/ui-shell.js", "/ui-router.js", "/ui-data.js", "/ui-keys.js", "/ui-keybar.js", "/ui-hooks.js", "/ui-list.js", "/ui-menu.js",
        "/ui-palette.js", "/ui-fuzzy.js", "/ui-feedback.js",
        "/ui-actions.js", "/ui-undo.js", "/ui-notice.js", "/ui-write.js", "/ui-live.js", "/ui-states.js", "/ui-run.js",
        "/vendor-preact.js", "/vendor-htm.js", "/vendor-signals.js"})
    check.assertEqual(assets.styles, {"/ui-fonts.css", "/ui-tokens.css", "/ui-layout.css", "/ui-keys.css", "/ui-list.css", "/ui-palette.css", "/ui-feedback.css", "/ui-run.css"})
    check.assertIn("/icon-192.png", assets.declared)
    for path in sorted(assets.declared):
        with opener.open(base + path, timeout=5) as response:
            check.assertEqual(response.status, 200)
    for path in sorted(assets.scripts):
        get(path, "text/javascript")
    for path in sorted(assets.styles):
        get(path, "text/css")
    router = get("/ui-router.js", "text/javascript")
    for screen in ("floor", "triage", "board", "task", "run", "intake", "outbox", "prompts", "adws", "insights", "settings"):
        check.assertIn(json.dumps(screen), router)
    check.assertIn('<a href="/ui.html">Try the new UI</a>', get("/", "text/html"))
    check.assertEqual(len(list((Path(dashboard.__file__).parent / "web").glob("vendor-*.js"))), 3)
finally:
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
