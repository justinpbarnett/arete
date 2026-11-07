from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NoReturn
from urllib.parse import parse_qs, unquote, urlsplit

from . import (
    board,
    chat,
    config,
    control,
    devices,
    gh,
    hooks,
    loops,
    paths,
    sessions,
    settings,
    util,
)

WEB_DIR = paths.PACKAGE_DIR / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/pattern.css": ("pattern.css", "text/css; charset=utf-8"),
}
TAILSCALE_APPS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  "/Applications/*/Tailscale.app/Contents/MacOS/Tailscale")
TAILSCALE_AS_CLI = {"TAILSCALE_BE_CLI": "1"}
RETRY_SECONDS = 10
WATCH_SECONDS = 30
WHOIS_SECONDS = 60
MAX_BODY = 64 * 1024
LOG_PATH = "/api/log/"
LOOP_PATH = "/api/loops/"
FEED_PATH = "/api/feed/"
RUN_PATH = "/api/runs/"
APPROVAL_PATH = "/api/approvals/"
SESSION_PATH = "/api/sessions/"
DEVICE_PATH = "/api/devices/"
PAGE_POLICY = ("default-src 'self'; img-src 'self' data:; font-src 'self' data:; style-src 'self'; "
               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def tailscale(*args: str) -> dict:
    exe = shutil.which("tailscale") or next((m for pattern in TAILSCALE_APPS for m in sorted(glob.glob(pattern))), "")
    if not exe:
        return {}
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10, check=False,
                              env={**os.environ, **TAILSCALE_AS_CLI})
        return json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


@dataclass(frozen=True)
class Node:
    addresses: tuple[str, ...]
    names: frozenset[str]
    owner: str


def this_node(owner: str) -> Node | None:
    status = tailscale("status", "--json")
    me = status.get("Self") or {}
    addresses = tuple(me.get("TailscaleIPs") or ())
    dns_name = (me.get("DNSName") or "").rstrip(".").lower()
    owner = owner or ((status.get("User") or {}).get(str(me.get("UserID"))) or {}).get("LoginName", "")
    if status.get("BackendState") != "Running" or not addresses or not owner:
        return None
    return Node(addresses, frozenset({*addresses, dns_name, dns_name.split(".")[0]} - {""}), owner)


@dataclass
class Logins:
    seen: dict[str, tuple[float, str]] = field(default_factory=dict)
    guard: threading.Lock = field(default_factory=threading.Lock)

    def of(self, address: str) -> str:
        with self.guard:
            at, login = self.seen.get(address, (0.0, ""))
        if time.monotonic() - at < WHOIS_SECONDS:
            return login
        login = (tailscale("whois", "--json", address).get("UserProfile") or {}).get("LoginName", "")
        with self.guard:
            self.seen[address] = (time.monotonic(), login)
        return login


def host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def whole_number(text: str) -> int:
    if not str(text).strip().isdigit():
        raise Refusal(400, f"'{text}' is not a whole number.")
    return int(text)


class Refusal(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


STATUS_OF_REFUSAL = {board.NotARunId: 400, settings.Refused: 400, chat.Busy: 409, chat.NoDraft: 409, gh.GhError: 502,
                     control.NotFound: 404, control.Busy: 409, control.Refused: 400, loops.LoopError: 400,
                     devices.DeviceError: 400}


def refusal_status(e: Exception) -> int | None:
    return e.status if isinstance(e, Refusal | hooks.HookRefused) else next(
        (status for kind, status in STATUS_OF_REFUSAL.items() if isinstance(e, kind)), None)


def last_part(path: str, prefix: str) -> tuple[str, str]:
    name, _, action = unquote(path[len(prefix):]).rpartition("/")
    return name, action


class Handler(BaseHTTPRequestHandler):
    server_version = "arete"
    sys_version = ""

    def do_GET(self) -> None:
        self.answer("GET")

    def do_POST(self) -> None:
        self.answer("POST")

    def answer(self, method: str) -> None:
        try:
            parts = urlsplit(self.path)
            path = parts.path
            self.admit(method, path)
            if method == "GET" and path in STATIC:
                return self.send_static(*STATIC[path])
            if method == "POST" and path.startswith(hooks.HOOK_PATH):
                got = hooks.receive(unquote(path[len(hooks.HOOK_PATH):]), self.headers, self.raw_body())
                return self.send_json(202 if got["queued"] else 200, got)
            self.send_json(200, self.api(method, path, {k: v[-1] for k, v in parse_qs(parts.query).items()}))
        except Exception as e:
            if status := refusal_status(e):
                return self.send_json(status, {"error": str(e)})
            self.log_error("%s", f"{type(e).__name__}: {e}")
            self.send_json(500, {"error": util.first_line(f"{type(e).__name__}: {e}", 300)})

    def admit(self, method: str, path: str) -> None:
        node: Node = self.server.node
        if host_name(self.headers.get("Host", "")) not in node.names:
            raise Refusal(421, "This server answers only to its tailnet name or address.")
        if path.startswith(hooks.HOOK_PATH):
            if method != "POST":
                raise Refusal(405, "A webhook takes POST.")
            return
        if self.server.logins.of(self.client_address[0]) != node.owner:
            raise Refusal(403, "The dashboard opens only for its owner's tailnet login.")
        if method == "POST" and self.headers.get("X-Arete") != "1":
            raise Refusal(403, "A change needs the X-Arete header, which the dashboard page sends.")

    def api(self, method: str, path: str, query: dict):
        if method == "GET":
            if path == "/api/state":
                return board.snapshot()
            if path == "/api/trends":
                return board.trends()
            if path == "/api/settings":
                return settings.current()
            if path == "/api/chat":
                return chat.view()
            if path.startswith(LOG_PATH):
                run_id = unquote(path[len(LOG_PATH):])
                return {"id": run_id, "lines": board.run_log(run_id)}
            if path == "/api/loops":
                return {"loops": control.loop_views(), "serve": control.serve_beat()}
            if path.startswith(LOOP_PATH):
                return control.loop_detail(unquote(path[len(LOOP_PATH):]))
            if path.startswith(FEED_PATH):
                return control.feed(unquote(path[len(FEED_PATH):]), whole_number(query.get("after", "0")))
            if path == "/api/approvals":
                return {"approvals": control.approvals()}
            if path == "/api/devices":
                return {"devices": control.fleet()}
            if path == "/api/sessions":
                return {"sessions": sessions.views(), "choices": sessions.choices()}
            if path == "/api/costs":
                return control.costs(float(whole_number(query.get("days", str(control.COST_DAYS))) or 1))
        if method == "POST":
            if path == "/api/settings":
                return settings.save(self.body())
            if path == "/api/chat":
                return chat.send(self.chat_text())
            if path == "/api/chat/new":
                return chat.new()
            if path == "/api/chat/file":
                return chat.file_draft()
            if path == "/api/chat/drop":
                return chat.drop_draft()
            if path.startswith(LOOP_PATH):
                return self.loop_action(*last_part(path, LOOP_PATH))
            if path.startswith(RUN_PATH):
                run_id, action = last_part(path, RUN_PATH)
                if action == "stop":
                    return control.stop_run(run_id)
            if path.startswith(APPROVAL_PATH):
                return control.decide(whole_number(unquote(path[len(APPROVAL_PATH):])),
                                      self.body().get("approve") is True)
            if path == "/api/sessions":
                return self.launch_session()
            if path.startswith(SESSION_PATH):
                session_id, action = last_part(path, SESSION_PATH)
                if action == "send":
                    return sessions.send(session_id, str(self.body().get("text", "")))
                if action == "stop":
                    return control.stop_run(session_id)
            if path.startswith(DEVICE_PATH):
                name, action = last_part(path, DEVICE_PATH)
                if action == "probe":
                    return devices.probe(name)
        raise Refusal(404, f"Nothing at {method} {path}.")

    def loop_action(self, name: str, action: str):
        if action in ("enable", "disable"):
            return control.switch(name, action == "enable")
        if action == "start":
            return control.start_loop(name)
        raise Refusal(404, f"Loops take enable, disable or start, not {action}.")

    def launch_session(self) -> dict:
        b = self.body()
        tools = b.get("tools")
        if tools is not None and not (isinstance(tools, list) and all(isinstance(x, str) for x in tools)):
            raise Refusal(400, "tools is a list of tool names.")
        budget = b.get("budget_usd")
        if budget not in (None, "") and not isinstance(budget, int | float):
            raise Refusal(400, "budget_usd is a number of dollars.")
        device, repo = str(b.get("device") or "local"), str(b.get("repo") or "")
        if repo and repo not in {r.name for r in config.load().repos}:
            raise Refusal(400, "Pick a repo from the config.")
        if repo and device != "local":
            raise Refusal(400, "A session on another device starts in that device's folder; leave the repo empty.")
        return sessions.launch(str(b.get("prompt", "")), device=device, model=str(b.get("model") or ""),
                               run_with=str(b.get("runner") or ""), tools=tools, repo=repo,
                               budget_usd=float(budget) if budget else None)

    def chat_text(self) -> str:
        text = str(self.body().get("text", "")).strip()
        if not text:
            raise Refusal(400, "Write a message first.")
        return text

    def raw_body(self, limit: int = hooks.MAX_HOOK_BODY) -> bytes:
        length = (self.headers.get("Content-Length") or "0").strip()
        if not length.isdigit():
            raise Refusal(400, "Content-Length takes a whole number of bytes.")
        size = int(length)
        if size > limit:
            raise Refusal(413, f"A request body holds at most {limit // 1024} KB.")
        return self.rfile.read(size)

    def body(self) -> dict:
        try:
            data = json.loads(self.raw_body(MAX_BODY) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise Refusal(400, "The request body is not JSON.") from e
        if not isinstance(data, dict):
            raise Refusal(400, "The request body is not a JSON object.")
        return data

    def send_static(self, name: str, kind: str) -> None:
        self.send(200, (WEB_DIR / name).read_bytes(), kind, "no-cache")

    def send_json(self, status: int, data) -> None:
        self.send(status, json.dumps(data, default=str).encode(), "application/json", "no-store")

    def send(self, status: int, payload: bytes, kind: str, cache: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", PAGE_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def log_request(self, code="-", size="-") -> None:
        if self.command == "GET" and isinstance(code, int) and 200 <= code < 300:
            return
        super().log_request(code, size)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: str, port: int, node: Node, logins: Logins):
        self.address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        self.node, self.logins = node, logins
        super().__init__((address, port), Handler)


def open_servers(node: Node, port: int, logins: Logins) -> list[Server]:
    servers: list[Server] = []
    try:
        for address in node.addresses:
            servers.append(Server(address, port, node, logins))
    except OSError:
        for s in servers:
            s.server_close()
        raise
    return servers


def say(text: str) -> None:
    print(f"{util.iso()} {text}", flush=True)


def serve(port: int | None = None) -> NoReturn:
    cfg = config.load()
    port = port or cfg.dashboard_port
    chat.settle_after_restart()
    logins = Logins()
    waiting_for = ""
    while True:
        node = this_node(cfg.dashboard_owner)
        try:
            if node is None:
                raise OSError("tailscale is not running or reports no address")
            servers = open_servers(node, port, logins)
        except OSError as e:
            if str(e) != waiting_for:
                say(f"cannot listen yet ({e}); trying again every {RETRY_SECONDS} s")
                waiting_for = str(e)
            time.sleep(RETRY_SECONDS)
            continue
        waiting_for = ""
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        say(f"dashboard listening on port {port} at {', '.join(node.addresses)}")
        while this_node(cfg.dashboard_owner) == node:
            time.sleep(WATCH_SECONDS)
        say("the tailnet address or owner changed; listening again")
        for s in servers:
            s.shutdown()
            s.server_close()
