"""Serve the classic dashboard with two disposable issue drafts and an offline issue transport."""

import argparse
import html
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arete import api_check

ADDRESS = "127.0.0.1"
LOGIN = "drafts@example.invalid"
REPO = "fixture/arete"


def serve(port):
    from arete import dashboard, gh, paths, principals, routing, tailnet
    from arete.store import chats, db, tokens

    paths.CONFIG_FILE.write_text(f'[[repo]]\nname = "arete"\npath = "."\ngithub = "{REPO}"\n')
    db.migrate()
    owner = principals.seed_owner(LOGIN)
    tokens.init_owner()
    _, credential = tokens.issue(owner, "Scratch draft actions", ["read", "chat:write"])
    chats_by_name = {name: chats.chat_add("") for name in "AB"}
    gates = {}
    commands = {}
    actions = set()
    for name, chat_id in chats_by_name.items():
        chats.chat_say(chat_id, "arete", f"Saved chat {name}", title=f"Chat {name}",
                       draft_title=f"Draft {name}", draft_body=f"Body {name}")
        path = f"/api/chats/{chat_id}"
        gates[path] = threading.Event()
        gates[path].set()
        commands[f"/fixture/hold/{name}"] = gates[path].clear
        commands[f"/fixture/release/{name}"] = gates[path].set
        actions.update({path + "/file", path + "/drop"})

    filed = []

    def create_issue(repo, title, body, **kwargs):
        if repo != REPO:
            raise ValueError("Unknown fixture repository")
        filed.append(title)
        return len(filed)

    class Handler(dashboard.Handler):
        def do_GET(self):
            if self.path == "/fixture":
                controls = []
                for name, chat_id in chats_by_name.items():
                    gate = gates[f"/api/chats/{chat_id}"]
                    action = "hold" if gate.is_set() else "release"
                    controls.append(f'<form action="/fixture/{action}/{name}" method="post">'
                                    f'<button>{action.title()} {name} reads</button></form>')
                controls.append("<p>Filed issues: " + html.escape(", ".join(filed) or "none") + "</p>")
                controls.append('<a href="/" target="_blank">Open the dashboard</a>')
                body = ("<!doctype html><title>Draft fixture</title>" + "".join(controls)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in gates:
                gates[self.path].wait()
            if self.path.startswith("/api/"):
                self.headers["Authorization"] = f"Bearer {credential}"
            super().do_GET()

        def do_POST(self):
            origin = f"http://{ADDRESS}:{self.server.server_port}"
            if self.headers.get("Origin") != origin:
                self.send_error(403)
                return
            if command := commands.get(self.path):
                command()
                self.send_response(303)
                self.send_header("Location", "/fixture")
                self.end_headers()
                return
            if self.path == "/api/usage":
                self.send_response(204)
                self.end_headers()
                return
            if self.path not in actions or self.headers.get("X-Arete") != "1":
                self.send_error(403)
                return
            self.headers["Authorization"] = f"Bearer {credential}"
            super().do_POST()

    logins = tailnet.Logins(tailnet.ScratchHost())
    logins.of = lambda peer: LOGIN
    node = tailnet.Node((ADDRESS,), frozenset({ADDRESS}), ADDRESS)
    with (
        patch.object(routing, "metered", return_value=False),
        patch("socket.getfqdn", return_value=ADDRESS),
        patch.object(gh, "find_issue_marker", return_value=""),
        patch.object(gh, "issue_create", create_issue),
        patch.object(gh, "issue_url", lambda repo, number: f"https://example.invalid/{repo}/issues/{number}"),
        tailnet.Server(ADDRESS, port, node, logins, False, Handler, dashboard.Redirect) as server,
    ):
        print(f"Open http://{ADDRESS}:{server.server_port}/fixture for draft controls.", flush=True)
        try:
            server.serve_forever()
        finally:
            for gate in gates.values():
                gate.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="arete-chat-drafts-") as directory:
        root = Path(directory).resolve()
        environment = api_check.isolated_env(root)
        os.environ.clear()
        os.environ.update(environment)
        os.chdir(root)
        with api_check.offline():
            serve(args.port)


def stop(signum, frame):
    # The process group and its launcher can both deliver SIGTERM during cleanup.
    signal.signal(signum, signal.SIG_IGN)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    main()
