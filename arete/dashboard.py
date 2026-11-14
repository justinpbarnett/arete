from __future__ import annotations

import atexit
import glob
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qs, unquote, urlsplit

from . import (
    board,
    capture,
    chat,
    client,
    config,
    control,
    devices,
    engine,
    gh,
    hooks,
    jira,
    lines,
    loops,
    oncall,
    paths,
    pipeline,
    record,
    research,
    scaffold,
    sessions,
    settings,
    state,
    tasks,
    triage,
    util,
    webapp,
    webpush,
)

WEB_DIR = paths.PACKAGE_DIR / "web"
WEB_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".js": "text/javascript; charset=utf-8", ".webmanifest": "application/manifest+json",
             ".png": "image/png"}
CODE_MARK = b'<meta name="arete-code" content="">'
THEME_MARK = b'<meta name="theme-color" content="">'
CERT_SECONDS = 120
RENEW_SECONDS = 7 * 24 * 3600
CERT_RETRY_SECONDS = 3600
HANDSHAKE_SECONDS = 10
LOOPBACK = "127.0.0.1"
TAILSCALE_APPS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  "/Applications/*/Tailscale.app/Contents/MacOS/Tailscale")
TAILSCALE_AS_CLI = {"TAILSCALE_BE_CLI": "1"}
RETRY_SECONDS = 10
WATCH_SECONDS = 30
WHOIS_SECONDS = 60
PROXY_SECONDS = 20
MAX_BODY = 64 * 1024
TLS_HANDSHAKE = b"\x16\x03"
LOG_PATH = "/api/log/"
LOOP_PATH = "/api/loops/"
INCIDENT_PATH = "/api/incidents/"
STEERING = ("dismiss", "retry", "cancel")
FEED_PATH = "/api/feed/"
RUN_PATH = "/api/runs/"
APPROVAL_PATH = "/api/approvals/"
ASK_PATH = "/api/asks/"
PUSH_PATH = "/api/push"
SESSION_PATH = "/api/sessions/"
DEVICE_PATH = "/api/devices/"
TRIAGE_PATH = "/api/triage/"
SKIPPED_PATH = "/api/skipped/"
PROPOSAL_PATH = "/api/proposals/"
CHAT_PATH = "/api/chats/"
TASK_PATH = "/api/tasks/"
EVENTS_PATH = re.compile(r"/api/runs/([^/]+)/events")
EVENTS_TYPE = "application/x-ndjson"
EVIDENCE_PATH = "/api/capture/evidence"
EVIDENCE_ITEM = re.compile(r"/api/capture/evidence/(\d+)")
EVIDENCE_DROP = re.compile(r"/api/capture/evidence/(\d+)/drop")
EVIDENCE_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
SKIP_CHUNK = 1024 * 1024
MOST_SKIPPED = 64 * 1024 * 1024
RUN_ID = re.compile(r"\w[\w.-]*")
END_GRACE_SECONDS = 5
INSTALL_SECONDS = 120
USAGE_KINDS = ("visit", "action")
USAGE_DEVICES = ("desktop", "phone")
USAGE_NEEDS = {"visit": ("view", "via"), "action": ("view", "name")}
USAGE_NAME = re.compile(r"[a-z]{1,20}(?:-[a-z]{1,20}){0,3}")
USAGE_TAB = re.compile(r"[0-9a-f]{16}")
USAGE_MOST_MS = 24 * 60 * 60 * 1000
PAGE_POLICY = ("default-src 'self'; img-src 'self' data:; font-src 'self' data:; style-src 'self'; "
               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def web_files() -> list[Path]:
    return sorted(f for f in WEB_DIR.iterdir() if f.suffix in WEB_TYPES)


def web_snapshot() -> tuple[str, dict[str, tuple[bytes, str]]]:
    bodies = {f: f.read_bytes() for f in web_files()}
    digest = hashlib.sha256()
    for f, body in bodies.items():
        digest.update(f"{f.name}\0{len(body)}\0".encode())
        digest.update(body)
    code = digest.hexdigest()[:12]
    colors = webapp.tokens(next(body for f, body in bodies.items() if f.name == "pattern.css"))
    served = {}
    for f, body in bodies.items():
        if f.name == "index.html":
            for mark in (CODE_MARK, THEME_MARK):
                if mark not in body:
                    raise RuntimeError(f"index.html lost {mark.decode()}, so an open page could not tell when its "
                                       "code is old or take its colours.")
            page = body.replace(CODE_MARK, f'<meta name="arete-code" content="{code}">'.encode())
            served["/"] = (page.replace(THEME_MARK, f'<meta name="theme-color" content="{colors["ground"]}">'.encode()),
                           WEB_TYPES[f.suffix])
        elif f.suffix == ".webmanifest":
            served[f"/{f.name}"] = (webapp.manifest(body, colors), WEB_TYPES[f.suffix])
        else:
            served[f"/{f.name}"] = (body, WEB_TYPES[f.suffix])
    for path, png in webapp.icons(colors).items():
        served[path] = (png, WEB_TYPES[".png"])
    return code, served


WEB_CODE, WEB_BY_PATH = web_snapshot()


def tailscale_exe() -> str:
    return shutil.which("tailscale") or next((m for pattern in TAILSCALE_APPS for m in sorted(glob.glob(pattern))), "")


def tailscale_run(*args: str, timeout: float) -> subprocess.CompletedProcess | None:
    exe = tailscale_exe()
    if not exe:
        return None
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, errors="replace", timeout=timeout,
                              check=False, env={**os.environ, **TAILSCALE_AS_CLI})
    except subprocess.TimeoutExpired:
        return None


def tailscale(*args: str) -> dict:
    proc = tailscale_run(*args, timeout=10)
    try:
        return json.loads(proc.stdout) if proc and proc.returncode == 0 else {}
    except json.JSONDecodeError:
        return {}


@dataclass(frozen=True)
class Node:
    addresses: tuple[str, ...]
    names: frozenset[str]
    owner: str
    name: str


class CertError(Exception):
    pass


class ProxyError(Exception):
    pass


PROXY_FAILURES = (ProxyError, OSError, subprocess.SubprocessError)


def serve_entries(status: dict) -> dict[int, dict[str, str]]:
    found: dict[int, dict[str, str]] = {}
    for scope in (status, *((status.get("Foreground") or {}).values())):
        if not isinstance(scope, dict):
            continue
        for hostport, web in (scope.get("Web") or {}).items():
            try:
                port = int(str(hostport).rsplit(":", 1)[1])
            except (IndexError, ValueError):
                continue
            for mount, handler in ((web or {}).get("Handlers") or {}).items():
                target = (handler or {}).get("Proxy") if isinstance(handler, dict) else None
                found.setdefault(port, {})[f"{hostport}{mount}"] = str(target or handler)
        for key, tcp in (scope.get("TCP") or {}).items():
            forward = (tcp or {}).get("TCPForward") if isinstance(tcp, dict) else None
            if forward and str(key).isdecimal():
                found.setdefault(int(key), {})["tcp"] = str(forward)
    return found


class Tailscale:
    def node(self, owner: str) -> Node | None:
        status = tailscale("status", "--json")
        me = status.get("Self") or {}
        addresses = tuple(me.get("TailscaleIPs") or ())
        dns_name = (me.get("DNSName") or "").rstrip(".").lower()
        owner = owner or ((status.get("User") or {}).get(str(me.get("UserID"))) or {}).get("LoginName", "")
        if status.get("BackendState") != "Running" or not addresses or not owner:
            return None
        return Node(addresses, frozenset({*addresses, dns_name, dns_name.split(".")[0]} - {""}), owner, dns_name)

    def whois(self, address: str) -> str:
        return (tailscale("whois", "--json", address).get("UserProfile") or {}).get("LoginName", "")

    def certificate(self, name: str) -> str:
        proc = tailscale_run("cert", "--cert-file", "-", "--key-file", "-", name, timeout=CERT_SECONDS)
        if proc is None:
            raise CertError("tailscale is missing or did not finish in time")
        if proc.returncode != 0:
            raise CertError(util.first_line(proc.stderr or "", 200) or f"tailscale cert exited {proc.returncode}")
        return proc.stdout

    def served(self) -> dict[int, dict[str, str]] | None:
        proc = tailscale_run("serve", "status", "--json", timeout=PROXY_SECONDS)
        if proc is None or proc.returncode != 0:
            return None
        try:
            status = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return serve_entries(status) if isinstance(status, dict) else None

    def proxy(self, port: int) -> None:
        proc = tailscale_run("serve", "--bg", f"--https={port}", f"http://{LOOPBACK}:{port}", timeout=PROXY_SECONDS)
        if proc is None:
            raise ProxyError("tailscale serve is missing or did not finish in time")
        if proc.returncode != 0:
            raise ProxyError(util.first_line(proc.stderr or "", 200) or f"tailscale serve exited {proc.returncode}")

    def unproxy(self, port: int) -> None:
        proc = tailscale_run("serve", f"--https={port}", "off", timeout=PROXY_SECONDS)
        if proc is None:
            raise ProxyError("tailscale serve is missing or did not finish in time")
        if proc.returncode != 0:
            raise ProxyError(util.first_line(proc.stderr or "", 200) or f"tailscale serve exited {proc.returncode}")


class ScratchHost(Tailscale):
    def certificate(self, name: str) -> str:
        raise CertError("scratch state asks for no certificate")

    def served(self) -> dict[int, dict[str, str]] | None:
        return None

    def proxy(self, port: int) -> None:
        raise ProxyError("scratch state changes no tailscale serve mapping")

    def unproxy(self, port: int) -> None:
        raise ProxyError("scratch state changes no tailscale serve mapping")


def host_services() -> Tailscale:
    return ScratchHost() if paths.ISOLATED else Tailscale()


def this_node(owner: str, host: Tailscale | None = None) -> Node | None:
    return (host or host_services()).node(owner)


@dataclass
class Gauge:
    count: int = 0
    guard: threading.Lock = field(default_factory=threading.Lock)

    def add(self, n: int) -> None:
        with self.guard:
            self.count += n


POSTS_IN_FLIGHT = Gauge()


@dataclass
class Logins:
    host: Tailscale
    seen: dict[str, tuple[float, str]] = field(default_factory=dict)
    guard: threading.Lock = field(default_factory=threading.Lock)

    def of(self, address: str) -> str:
        with self.guard:
            seen = self.seen.get(address)
        if seen and time.monotonic() - seen[0] < WHOIS_SECONDS:
            return seen[1]
        login = self.host.whois(address)
        if login:
            with self.guard:
                self.seen[address] = (time.monotonic(), login)
        return login


PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z ]+)-----.*?-----END \1-----\n?", re.DOTALL)


def pem_parts(text: str) -> tuple[bytes, bytes]:
    blocks = [(m[1], m[0]) for m in PEM_BLOCK.finditer(text)]
    key = "".join(b for label, b in blocks if "PRIVATE KEY" in label)
    chain = "".join(b for label, b in blocks if "CERTIFICATE" in label)
    return chain.encode(), key.encode()


def store_private(path: Path, content: bytes) -> bool:
    try:
        current = path.read_bytes()
    except OSError:
        current = None
    if current == content:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)
        return False
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return True


def fetch_certificate(host: Tailscale, name: str) -> bool:
    if not name:
        raise CertError("this machine has no tailnet name")
    chain, key = pem_parts(host.certificate(name))
    if not chain or not key:
        raise CertError("tailscale cert did not give a certificate and a key")
    try:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        changed = store_private(paths.TLS_KEY_FILE, key)
        return store_private(paths.TLS_CERT_FILE, chain) or changed
    except OSError as e:
        raise CertError(f"cannot store the certificate: {e.strerror or e}") from None


def tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(paths.TLS_CERT_FILE, paths.TLS_KEY_FILE)
    return context


def host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def whole_number(text: str) -> int:
    if not str(text).strip().isdecimal():
        raise Refusal(400, f"'{text}' is not a whole number.")
    return int(text)


class Refusal(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


STATUS_OF_REFUSAL = {capture.TooLarge: 413, capture.Refused: 400, capture.NotStaged: 404, capture.Conflict: 409,board.NotARunId: 400, board.UnknownFilter: 400, settings.Refused: 400, chat.Busy: 409,
                     chat.NoDraft: 409, chat.NoChat: 404, chat.BadProject: 400, gh.GhError: 502, jira.JiraError: 502,
                     control.NotFound: 404, control.Busy: 409, control.Refused: 400, loops.LoopError: 400,
                     devices.DeviceError: 400, triage.NotOnBoard: 404, triage.NotSendable: 409, triage.BadAnswers: 400,
                     triage.TrackerFailed: 502, pipeline.NoFreeSlot: 409, tasks.TrackerFailed: 502,
                     webpush.Refused: 400}


def refusal_status(e: Exception) -> int | None:
    return e.status if isinstance(e, Refusal | hooks.HookRefused) else next(
        (status for kind, status in STATUS_OF_REFUSAL.items() if isinstance(e, kind)), None)


def refusal_answer(e: Exception) -> dict:
    answer = {"error": as_sentence(str(e))}
    if isinstance(e, control.KeptFailure):
        answer["kept"] = True
    return answer


def as_sentence(text: str) -> str:
    text = text.strip()
    first_word = text.split(" ", 1)[0]
    if first_word.isalpha() and first_word.islower():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "?", "!")) else f"{text}."


def last_part(path: str, prefix: str) -> tuple[str, str]:
    name, _, action = unquote(path[len(prefix):]).rpartition("/")
    return name, action


class Handler(BaseHTTPRequestHandler):
    server_version = "arete"
    sys_version = ""

    def do_GET(self) -> None:
        self.answer("GET")

    def do_POST(self) -> None:
        POSTS_IN_FLIGHT.add(1)
        try:
            self.answer("POST")
        finally:
            POSTS_IN_FLIGHT.add(-1)

    def answer(self, method: str) -> None:
        try:
            parts = urlsplit(self.path)
            path = parts.path
            self.admit(method, path)
            if method == "GET" and path in WEB_BY_PATH:
                return self.send(200, *WEB_BY_PATH[path], "no-cache")
            if method == "POST" and path.startswith(hooks.HOOK_PATH):
                got = hooks.receive(unquote(path[len(hooks.HOOK_PATH):]), self.headers, self.raw_body())
                return self.send_json(202 if got["queued"] else 200, got)
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            if method == "POST" and path == EVIDENCE_PATH:
                return self.send_json(200, self.stage_evidence(query))
            if method == "GET" and (item := EVIDENCE_ITEM.fullmatch(path)):
                return self.send_evidence(int(item[1]))
            if method == "GET" and (events := EVENTS_PATH.fullmatch(path)):
                return self.send_events(unquote(events[1]), query)
            self.send_json(200, self.api(method, path, query))
        except Exception as e:
            if status := refusal_status(e):
                return self.send_json(status, refusal_answer(e))
            said = type(e).__name__ if self.path.startswith((ASK_PATH, PUSH_PATH)) else f"{type(e).__name__}: {e}"
            self.log_error("%s", said)
            self.send_json(500, {"error": as_sentence(util.first_line(said, 300))})

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
            if path == PUSH_PATH:
                return webpush.view()
            if path == "/api/sidebar":
                return board.sidebar(chat.pulse())
            if path == "/api/live":
                return board.live_board(chat.pulse()["draft"])
            if path == "/api/runs":
                return board.runs_board(query, limit=whole_number(query.get("limit", str(board.RECENT_RUNS))))
            if path.startswith(RUN_PATH):
                return board.run_board(unquote(path[len(RUN_PATH):]))
            if path.startswith(TASK_PATH):
                return tasks.view(unquote(path[len(TASK_PATH):]), query.get("repo", ""))
            if path == "/api/asks":
                return tasks.asks_view()
            if path == "/api/health":
                return board.health_board()
            if path == "/api/triage":
                return board.panel(board.triage_board)
            if path == "/api/outcomes":
                return board.panel(board.outcomes)
            if path == "/api/find":
                return board.find()
            if path == "/api/usage":
                return board.usage_board(whole_number(query.get("days", str(board.USAGE_DAYS))))
            if path == "/api/settings":
                return settings.current()
            if path == "/api/capture":
                return capture.view(connect=state.reader)
            if path == "/api/chats":
                return chat.listing()
            if path.startswith(CHAT_PATH) and "/sessions" in path:
                parts = unquote(path[len(CHAT_PATH):]).split("/")
                if len(parts) == 2 and parts[1] == "sessions":
                    return chat.sessions_of(whole_number(parts[0]))
                if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "report":
                    return chat.session_report(whole_number(parts[0]), parts[2])
            if path.startswith(CHAT_PATH):
                return chat.view(whole_number(unquote(path[len(CHAT_PATH):])))
            if path.startswith(LOG_PATH):
                run_id = unquote(path[len(LOG_PATH):])
                return {"id": run_id, "lines": board.run_log(run_id)}
            if path == "/api/loops":
                return {"loops": control.loop_views(connect=state.reader), "serve": control.serve_beat(),
                        "draft": draft_choices()}
            if path.startswith(LOOP_PATH):
                return control.loop_detail(unquote(path[len(LOOP_PATH):]), connect=state.reader)
            if path == "/api/incidents":
                return board.incidents_board()
            if path.startswith(INCIDENT_PATH):
                return board.incident_board(unquote(path[len(INCIDENT_PATH):]))
            if path.startswith(FEED_PATH):
                return control.feed(unquote(path[len(FEED_PATH):]), whole_number(query.get("after", "0")))
            if path == "/api/sessions":
                return {"sessions": sessions.views(connect=state.reader), "choices": sessions.choices()}
        if method == "POST":
            if path == "/api/usage":
                state.add_usage(self.usage_row(), board.KEEP_USAGE_DAYS)
                return {}
            if path == "/api/capture":
                return capture.save(self.body())
            if path == "/api/capture/context":
                return capture.add_context(self.body(), WEB_CODE)
            if path == "/api/capture/submit":
                return capture.submit(self.body())
            if path == "/api/capture/context/drop":
                return capture.drop_context()
            if path == "/api/capture/discard":
                return capture.discard()
            if drop := EVIDENCE_DROP.fullmatch(path):
                return capture.drop_evidence(int(drop[1]))
            if path == "/api/settings":
                return settings.save(self.body())
            if path == "/api/chats/new":
                return chat.new(str(self.body().get("project") or ""))
            if path.startswith(CHAT_PATH) and "/sessions" in path:
                parts = unquote(path[len(CHAT_PATH):]).split("/")
                if len(parts) == 2 and parts[1] == "sessions":
                    return self.start_chat_session(whole_number(parts[0]))
                if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "stop":
                    return chat.stop_session(whole_number(parts[0]), parts[2])
            if path.startswith(CHAT_PATH):
                chat_id, action = last_part(path, CHAT_PATH)
                if action == "send":
                    return chat.send(whole_number(chat_id), self.chat_text())
                if action == "file":
                    return chat.file_draft(whole_number(chat_id))
                if action == "drop":
                    return chat.drop_draft(whole_number(chat_id))
            if path == "/api/incidents/switch":
                on = self.body().get("on")
                if type(on) is not bool:
                    raise control.Refused('switch takes {"on": true} or {"on": false}')
                return oncall.switch(on)
            if path.startswith(INCIDENT_PATH):
                incident_id, action = last_part(path, INCIDENT_PATH)
                if action in STEERING:
                    return getattr(oncall, action)(board.incident_number(incident_id))
            if path == "/api/loops/new":
                return self.draft_loop()
            if path.startswith(LOOP_PATH):
                return self.loop_action(*last_part(path, LOOP_PATH))
            if path.startswith(RUN_PATH):
                run_id, _, action = unquote(path[len(RUN_PATH):]).partition("/")
                if action == "stop":
                    return control.stop_run(run_id)
                if action == "answer":
                    return tasks.reply(run_id, str(self.body().get("text") or ""))
                if action == "resume":
                    return tasks.resume(run_id, str(self.body().get("step") or ""))
                if action == "repair":
                    return tasks.repair(run_id)
                if action.startswith("steps/"):
                    return tasks.run_step(run_id, action[len("steps/"):], self.body().get("set") or {})
            if path == f"{PUSH_PATH}/subscribe":
                return webpush.subscribe(self.body(), self.server.node.owner)
            if path == f"{PUSH_PATH}/remove":
                return webpush.remove(str(self.body().get("id") or ""))
            if path == f"{PUSH_PATH}/test":
                return webpush.test(config.load())
            if path.startswith(ASK_PATH):
                ask_id, _, action = unquote(path[len(ASK_PATH):]).partition("/")
                if action == "answer":
                    return tasks.answer_ask(ask_id, self.body())
            if path.startswith(APPROVAL_PATH):
                return control.decide(whole_number(unquote(path[len(APPROVAL_PATH):])),
                                      self.body().get("approve") is True)
            if path == "/api/tasks":
                b = self.body()
                return tasks.start(*(str(b.get(k) or "") for k in ("repo", "prompt", "title", "kind", "ticket")),
                                   force=b.get("force") is not False)
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
            if path == "/api/triage/refresh":
                return {"run": engine.launch("triage")["id"]}
            if path.startswith(TRIAGE_PATH):
                ref, action = last_part(path, TRIAGE_PATH)
                if action == "send":
                    return self.send_ticket(ref)
                if action == "research":
                    return research_ticket(ref, self.body().get("force") is True)
            if path == "/api/checks/run":
                return {"run": engine.launch("heartbeat")["id"]}
            if path.startswith(SKIPPED_PATH):
                name, action = last_part(path, SKIPPED_PATH)
                if action == "run":
                    return pipeline.run_skipped(name)
            if path.startswith(PROPOSAL_PATH):
                proposal_id, action = last_part(path, PROPOSAL_PATH)
                if action == "done":
                    return forget_proposal(whole_number(proposal_id))
                if action == "approve":
                    return approve_rule(whole_number(proposal_id))
                if action == "reject":
                    return reject_rule(whole_number(proposal_id))
            if path == "/api/serve/install":
                return install_serve(self.server.inside_serve)
        raise Refusal(404, f"Nothing at {method} {path}.")

    def loop_action(self, name: str, action: str):
        if action in ("enable", "disable"):
            return control.switch(name, action == "enable")
        if action == "start":
            return engine.launch(name)
        if action == "try":
            return scaffold.start_try(name)
        raise Refusal(404, f"Loops take enable, disable, start or try, not {action}.")

    def draft_loop(self) -> dict:
        b = self.body()
        return scaffold.start_draft(scaffold.DraftRequest(
            *(str(b.get(field) or "") for field in ("sentence", "name", "repo", "model"))))

    def launch_session(self) -> dict:
        b = self.body()
        tools = b.get("tools")
        if tools is not None and not (isinstance(tools, list) and all(isinstance(x, str) for x in tools)):
            raise Refusal(400, "tools is a list of tool names.")
        budget = b.get("budget_usd")
        if budget not in (None, "") and (isinstance(budget, bool) or not isinstance(budget, int | float)):
            raise Refusal(400, "budget_usd is a number of dollars.")
        return sessions.launch(str(b.get("prompt", "")), device=str(b.get("device") or ""),
                               model=str(b.get("model") or ""), run_with=str(b.get("runner") or ""), tools=tools,
                               repo=str(b.get("repo") or ""),
                               budget_usd=None if budget in (None, "") else float(budget))

    def start_chat_session(self, chat_id: int) -> dict:
        b = self.body()
        budget = b.get("budget_usd")
        if budget not in (None, "") and (isinstance(budget, bool) or not isinstance(budget, int | float)):
            raise Refusal(400, "budget_usd is a number of dollars.")
        repo = b.get("repo")
        return chat.start_session(chat_id, str(b.get("prompt", "")), repo=None if repo is None else str(repo),
                                  device=str(b.get("device") or ""), model=str(b.get("model") or ""),
                                  runner=str(b.get("runner") or ""),
                                  budget_usd=None if budget in (None, "") else float(budget))

    def send_ticket(self, ref: str) -> dict:
        b = self.body()
        questions, answers = b.get("questions"), b.get("answers")
        if not all(isinstance(x, list) and all(isinstance(t, str) for t in x) for x in (questions, answers)):
            raise Refusal(400, "Questions and answers take lists of text.")
        said = triage.send(config.load(), ref, questions, answers, str(b.get("repo") or ""))
        return {"ref": ref, "said": f"{said} {start_poll_without_failing_the_send()}"}

    def usage_row(self) -> dict:
        b = self.body()
        kind = b.get("kind")
        if kind not in USAGE_KINDS:
            raise Refusal(400, f"The kind is one of {', '.join(USAGE_KINDS)}.")
        if b.get("device") not in USAGE_DEVICES:
            raise Refusal(400, f"The device is one of {', '.join(USAGE_DEVICES)}.")
        if not isinstance(b.get("tab"), str) or not USAGE_TAB.fullmatch(b["tab"]):
            raise Refusal(400, "The tab is the page's mark of 16 hex digits.")
        names = {k: b.get(k) or "" for k in ("view", "name", "via")}
        if any(not isinstance(v, str) or (v or k in USAGE_NEEDS[kind]) and not USAGE_NAME.fullmatch(v)
               for k, v in names.items()):
            raise Refusal(400, f"The {' and '.join(USAGE_NEEDS[kind])} of each {kind} are lowercase words joined by "
                               "hyphens, so usage keeps names and never ids or text.")
        ms = b.get("ms", 0)
        if isinstance(ms, bool) or not isinstance(ms, int) or not 0 <= ms <= USAGE_MOST_MS:
            raise Refusal(400, "The time is a whole number of milliseconds, at most a day.")
        return {"at": util.iso(), "kind": kind, "device": b["device"], "tab": b["tab"], "ms": ms, **names}

    def chat_text(self) -> str:
        text = str(self.body().get("text", "")).strip()
        if not text:
            raise Refusal(400, "Write a message first.")
        return text

    def body_size(self) -> int:
        length = (self.headers.get("Content-Length") or "0").strip()
        if not length.isdigit():
            raise Refusal(400, "Content-Length takes a whole number of bytes.")
        return int(length)

    def stage_evidence(self, query: dict) -> dict:
        size = self.body_size()
        if size > capture.MOST_VIDEO_BYTES:
            if size <= MOST_SKIPPED:
                left = size
                while left > 0 and (chunk := self.rfile.read(min(SKIP_CHUNK, left))):
                    left -= len(chunk)
            else:
                self.close_connection = True
            raise capture.TooLarge(f"An upload is limited to {capture.MOST_VIDEO_BYTES // capture.MB} MB for a video and "
                                   f"{capture.MOST_IMAGE_BYTES // capture.MB} MB for an image, and this one has "
                                   f"{capture.megabytes(size)}.")
        return capture.stage(self.raw_body(capture.MOST_VIDEO_BYTES), query.get("name", ""), query.get("source", ""),
                             query.get("seconds", ""))

    def send_evidence(self, evidence_id: int) -> None:
        path, row = capture.staged_file(evidence_id)
        total = path.stat().st_size
        first, last, status = 0, total - 1, 200
        wanted = EVIDENCE_RANGE.fullmatch((self.headers.get("Range") or "").strip())
        if wanted and (wanted[1] or wanted[2]):
            if wanted[1]:
                start, end = int(wanted[1]), int(wanted[2] or total - 1)
            else:
                start, end = max(total - int(wanted[2]), 0), total - 1
            end = min(end, total - 1)
            if 0 <= start <= end:
                first, last, status = start, end, 206
        self.head(status, row["type"], "no-store", last - first + 1)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {first}-{last}/{total}")
        try:
            self.end_headers()
            with path.open("rb") as f:
                f.seek(first)
                left = last - first + 1
                while left > 0 and (chunk := f.read(min(SKIP_CHUNK, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def raw_body(self, limit: int = hooks.MAX_HOOK_BODY) -> bytes:
        size = self.body_size()
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

    def send_json(self, status: int, data) -> None:
        self.send(status, json.dumps(data, default=str).encode(), "application/json", "no-store")

    def send_events(self, run_id: str, query: dict) -> None:
        if not RUN_ID.fullmatch(run_id):
            raise board.NotARunId(f"'{run_id}' is not a run id.")
        if not lines.path(run_id).exists() and not state.run_get(run_id, connect=state.reader):
            raise control.NotFound(f"nothing has the id {run_id}")
        start = whole_number(query.get("from", "0"))
        if query.get("follow") != "1":
            found = (line for line in lines.read(run_id) if lines.seq_of(line) > start)
            return self.send(200, b"".join(map(event_line, found)), EVENTS_TYPE, "no-store")
        self.head(200, EVENTS_TYPE, "no-store")
        self.close_connection = True
        try:
            self.end_headers()
            for line in lines.follow(run_id, start, run_ended(run_id)):
                self.wfile.write(event_line(line))
                self.wfile.flush()
        except OSError:
            pass

    def head(self, status: int, kind: str, cache: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Arete-Code", WEB_CODE)
        self.send_header("Content-Security-Policy", PAGE_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send(self, status: int, payload: bytes, kind: str, cache: str) -> None:
        self.head(status, kind, cache, len(payload))
        try:
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def log_request(self, code="-", size="-") -> None:
        if self.command == "GET" and isinstance(code, int) and 200 <= code < 300:
            return
        super().log_request(code, size)

    def log_message(self, format: str, *args) -> None:
        if not getattr(self, "raw_requestline", b"").startswith(TLS_HANDSHAKE):
            super().log_message(format, *args)


class Redirect(Handler):
    def answer(self, method: str) -> None:
        node: Node = self.server.node
        length = (self.headers.get("Content-Length") or "0").strip()
        if length.isdigit():
            self.rfile.read(min(int(length), hooks.MAX_HOOK_BODY))
        path = self.path if self.path.startswith("/") else "/"
        self.send_response(308)
        self.send_header("Location", f"https://{node.name}:{self.server.server_port}{path}")
        self.send_header("Content-Length", "0")
        self.end_headers()


@dataclass
class FrontedLogins:
    seen: threading.local = field(default_factory=threading.local)

    def note(self, login: str) -> None:
        self.seen.login = login

    def of(self, address: str) -> str:
        try:
            loopback = ipaddress.ip_address(address).is_loopback
        except ValueError:
            return ""
        return getattr(self.seen, "login", "") if loopback else ""


class FrontedHandler(Handler):
    def answer(self, method: str) -> None:
        self.server.logins.note(self.headers.get(client.IDENTITY_HEADER, ""))
        super().answer(method)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: str, port: int, node: Node, logins: Logins | FrontedLogins, inside_serve: bool,
                 tls: ssl.SSLContext | None = None, handler: type[Handler] = Handler):
        self.address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        self.node, self.logins, self.inside_serve, self.tls = node, logins, inside_serve, tls
        super().__init__((address, port), handler)

    def finish_request(self, request, client_address) -> None:
        if self.tls is None:
            return super().finish_request(request, client_address)
        request.settimeout(HANDSHAKE_SECONDS)
        try:
            first = request.recv(1, socket.MSG_PEEK)
            if first != b"\x16":
                request.settimeout(None)
                return Redirect(request, client_address, self)
            secure = self.tls.wrap_socket(request, server_side=True)
        except OSError:
            return
        try:
            secure.settimeout(None)
            self.RequestHandlerClass(secure, client_address, self)
        finally:
            try:
                secure.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            secure.close()


def event_line(line: dict) -> bytes:
    return json.dumps(line, ensure_ascii=False, default=str).encode() + b"\n"


def run_ended(run_id: str) -> Callable[[], bool]:
    quiet_since = None

    def ended() -> bool:
        nonlocal quiet_since
        row = state.run_get(run_id, connect=state.reader) or {}
        if row.get("status") == "running":
            with record.dead(row) as run_is_dead:
                if not run_is_dead:
                    quiet_since = None
                    return False
        quiet_since = quiet_since or time.monotonic()
        return time.monotonic() - quiet_since >= END_GRACE_SECONDS

    return ended


def draft_choices() -> dict:
    cfg = config.load()
    return {"repos": [r.name for r in cfg.repos], "models": sessions.known_models(cfg),
            "default_model": cfg.model("loop")}


def research_ticket(ref: str, force: bool) -> dict:
    r = research.ticket(ref)
    if not r:
        raise control.NotFound(f"{ref} is not a research ticket on the triage board")
    return {"ref": r["ref"], "run": engine.launch("research", events=[{"ref": r["ref"], "force": force}])["id"]}


def forget_proposal(proposal_id: int) -> dict:
    if not state.forget_proposal(proposal_id):
        raise control.NotFound(f"#{proposal_id} is not an open memory fact or harness fix")
    return {"id": proposal_id}


def approve_rule(proposal_id: int) -> dict:
    if (state.proposal(proposal_id, connect=state.reader) or {}).get("status") not in ("approve", "filing"):
        raise control.NotFound(f"#{proposal_id} is not a rule waiting for approval")
    control.detach(["file-rule", str(proposal_id)])
    return {"id": proposal_id}


def reject_rule(proposal_id: int) -> dict:
    if not state.move_proposal(proposal_id, ("approve",), "rejected"):
        raise control.NotFound(f"#{proposal_id} is not a rule waiting for approval")
    return {"id": proposal_id}


def install_serve(inside_serve: bool) -> dict:
    if control.serve_beat().get("alive"):
        raise Refusal(409, "Serve is already up, so it needs no install")
    if inside_serve:
        control.detach(["install"], "install.log")
        return {"restarting": True}
    res = util.sh([sys.executable, "-m", "arete.cli", "install"], timeout=INSTALL_SECONDS)
    said = "; ".join(line.strip() for line in (res.stdout + res.stderr).splitlines() if line.strip())
    if res.returncode != 0:
        raise Refusal(500, f"Serve did not install: {util.first_line(said or f'exit {res.returncode}', 400)}")
    return {"restarting": False, "said": util.first_line(said, 400)}


def start_poll_without_failing_the_send() -> str:
    try:
        if not control.find_loop("poll").enabled:
            return "The poll loop is off, so it builds once you turn poll on."
        engine.launch("poll")
    except control.Busy:
        return "A poll is running now, so the next one picks it up."
    except Exception as e:
        return f"The poll did not start: {util.first_line(str(e), 200)}."
    return "Started a poll to build it."


@dataclass(frozen=True)
class Front:
    scheme: str
    servers: list[Server]
    tls: ssl.SSLContext | None = None
    fronted: bool = False
    why: str = ""


@dataclass(frozen=True)
class Claim:
    behind: bool
    why: str = ""


def say(text: str) -> None:
    print(f"{util.iso()} {text}", flush=True)


def close_all(servers: list[Server]) -> None:
    for s in servers:
        s.server_close()


def stop_listener(server: Server | None) -> None:
    if server is not None:
        server.shutdown()
        server.server_close()


def open_servers(addresses: tuple[str, ...], port: int, node: Node, logins: Logins,
                 inside_serve: bool) -> list[Server]:
    servers: list[Server] = []
    try:
        for address in addresses:
            servers.append(Server(address, port, node, logins, inside_serve))
    except OSError:
        close_all(servers)
        raise
    return servers


def proxy_record(port: int) -> dict | None:
    try:
        found = json.loads(paths.proxy_file(port).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        found = None
    return found if isinstance(found, dict) else {"port": port, "pending": True}


def write_proxy_record(node: Node, port: int, entries: dict[str, str], pending: bool) -> None:
    util.write_json(paths.proxy_file(port), {"port": port, "name": node.name, "pid": os.getpid(),
                                             "pending": pending, "entries": entries, "at": util.iso()})


def drop_proxy_record(port: int) -> None:
    try:
        paths.proxy_file(port).unlink(missing_ok=True)
    except OSError as e:
        say(f"cannot remove the record of the tailscale serve mapping on port {port} ({e.strerror or e})")


def serve_status(host: Tailscale) -> dict[int, dict[str, str]] | None:
    try:
        status = host.served()
    except PROXY_FAILURES:
        return None
    return None if status is None else {int(p): dict(e) for p, e in status.items()}


def forwards_here(target: str, port: int) -> bool:
    parts = urlsplit(target if "://" in target else f"http://{target}")
    try:
        return parts.hostname in (LOOPBACK, "localhost") and parts.port == port
    except ValueError:
        return False


def owned(record: dict, on_port: dict[str, str], port: int) -> dict[str, str]:
    recorded = record.get("entries")
    recorded = recorded if isinstance(recorded, dict) else {}
    return {m: t for m, t in on_port.items() if forwards_here(t, port) or recorded.get(m) == t}


def release_proxy(host: Tailscale, port: int, made: dict[str, str] | None = None) -> bool:
    record = proxy_record(port)
    if record is None:
        return True
    if made:
        record = {**record, "entries": made}
    before = serve_status(host)
    if before is None:
        return False
    on_port = before.get(port, {})
    mine = owned(record, on_port, port)
    if mine and len(mine) == len(on_port):
        try:
            host.unproxy(port)
        except PROXY_FAILURES:
            pass
        after = serve_status(host)
        if after is None:
            return False
        mine = owned(record, after.get(port, {}), port)
    if mine:
        return False
    drop_proxy_record(port)
    return True


def claim_proxy(host: Tailscale, node: Node, port: int) -> Claim:
    record = proxy_record(port)
    before = serve_status(host)
    if before is None:
        return Claim(record is not None, "tailscale serve status cannot be read")
    on_port = before.get(port, {})
    if record:
        mine = owned(record, on_port, port)
        if mine:
            try:
                write_proxy_record(node, port, mine, False)
            except OSError as e:
                say(f"cannot update the record of the tailscale serve mapping on port {port} ({e.strerror or e})")
            return Claim(True)
        if not (on_port and record.get("pending")):
            drop_proxy_record(port)
    if on_port:
        return Claim(False, f"tailscale serve has a mapping on port {port} that arete did not make")
    try:
        write_proxy_record(node, port, {}, True)
    except OSError as e:
        return Claim(False, f"cannot record a tailscale serve mapping ({e.strerror or e})")
    failed = ""
    try:
        host.proxy(port)
    except PROXY_FAILURES as e:
        failed = f" ({util.first_line(str(e), 200)})"
    after = serve_status(host)
    if after is None:
        return Claim(True, "tailscale serve status cannot be read")
    made = after.get(port, {})
    if not made:
        drop_proxy_record(port)
        return Claim(False, f"tailscale serve made no mapping{failed}")
    try:
        write_proxy_record(node, port, made, False)
    except OSError as e:
        why = f"cannot record the tailscale serve mapping ({e.strerror or e})"
        return Claim(not release_proxy(host, port, made), why)
    return Claim(True)


def open_front(host: Tailscale, node: Node, port: int, logins: Logins, inside_serve: bool,
               kept: Server | None = None, secure: bool = False) -> Front:
    opened = open_servers(node.addresses, port, node, logins, inside_serve)
    try:
        tls, reason = None, ""
        if secure:
            try:
                fetch_certificate(host, node.name)
                tls = tls_context()
            except (CertError, ssl.SSLError, OSError) as e:
                reason = f"no certificate ({util.first_line(str(e), 200)})"
        if (tls or not secure) and release_proxy(host, port):
            stop_listener(kept)
            for s in opened:
                s.tls = tls
            return Front("https" if tls else "http", opened, tls=tls)
        close_all(opened)
        opened = []
        loopback = kept
        if loopback is None:
            loopback = Server(LOOPBACK, port, node, FrontedLogins(), inside_serve, handler=FrontedHandler)
            opened = [loopback]
        claim = Claim(True, f"arete's tailscale serve mapping on port {port} is still there") if tls or not secure \
            else claim_proxy(host, node, port)
        why = "; ".join(x for x in (reason, claim.why) if x)
        if claim.behind:
            return Front("http", [loopback], fronted=True, why=why)
        if kept is None:
            close_all(opened)
            opened = []
            opened = open_servers(node.addresses, port, node, logins, inside_serve)
        else:
            opened = open_servers(node.addresses, port, node, logins, inside_serve)
            stop_listener(kept)
        return Front("http", opened, why=why)
    except BaseException:
        close_all(opened)
        raise


def record_server(node: Node, port: int, front: Front) -> None:
    fields = {"pid": os.getpid(), "addresses": [LOOPBACK] if front.fronted else list(node.addresses), "port": port,
              "scheme": front.scheme, "host": node.name}
    if front.fronted:
        fields |= {"login": node.owner, "behind": "tailscale serve"}
    try:
        util.write_json(paths.SERVER_FILE, {**fields, **paths.state_and_config(), "at": util.iso()})
    except OSError as e:
        say(f"cannot write the server file ({e.strerror or e}); the CLI will not find this dashboard")


CERTIFICATE_RETRY = "trying for a certificate again"


def watch(host: Tailscale, owner: str, node: Node, front: Front, secure: bool = False) -> str:
    certificate_due = time.monotonic() + (RENEW_SECONDS if front.tls else CERT_RETRY_SECONDS) \
        if secure or front.fronted else math.inf
    renewal_failed = False
    reload_pending = False
    while True:
        now = host.node(owner)
        if now != node and not (front.fronted and now is None):
            return "the tailnet address or owner changed"
        time.sleep(WATCH_SECONDS)
        if now is None or time.monotonic() < certificate_due:
            continue
        if front.fronted:
            return CERTIFICATE_RETRY
        if not secure:
            continue
        try:
            reload_pending = fetch_certificate(host, node.name) or reload_pending
            if front.tls and reload_pending:
                fresh = tls_context()
                for s in front.servers:
                    s.tls = fresh
                reload_pending = False
        except (CertError, ssl.SSLError, OSError) as e:
            certificate_due = time.monotonic() + CERT_RETRY_SECONDS
            if front.tls and not renewal_failed:
                say(f"the certificate was not renewed ({util.first_line(str(e), 200)}); keeping the old one")
            renewal_failed = True
            continue
        renewal_failed = False
        if not front.tls:
            return "a certificate is ready"
        certificate_due = time.monotonic() + RENEW_SECONDS


def serve(port: int | None = None, inside_serve: bool = False, host: Tailscale | None = None) -> NoReturn:
    cfg = config.load()
    port = port or cfg.dashboard_port
    state.ensure_schema()
    chat.recover()
    capture.recover()
    host = host or host_services()
    logins = Logins(host)
    hold = util.lock_file(f"dashboard-{port}")
    held = False
    waiting_for = ""
    kept: Server | None = None
    kept_front: Front | None = None
    opened_once = False
    last_line = ""
    atexit.register(forget_server)
    try:
        while True:
            node = host.node(cfg.dashboard_owner)
            if kept is not None and kept_front is not None and node is not None and node != kept.node:
                kept.node = node
                record_server(node, port, kept_front)
            try:
                if node is None:
                    raise OSError("tailscale is not running or reports no address")
                held = held or util.flock_now(hold)
                if not held:
                    raise OSError(f"another dashboard on this state holds port {port}")
                if kept is None and not opened_once and (resumed := resume_fallback(node, port, inside_serve)):
                    kept_front = resumed
                    kept = resumed.servers[0]
                    last_line = announce(node, port, resumed, last_line)
                front = open_front(host, node, port, logins, inside_serve, kept, cfg.dashboard_https)
                opened_once = True
            except OSError as e:
                if str(e) != waiting_for:
                    say(f"cannot listen yet ({e}); trying again every {RETRY_SECONDS} s")
                    waiting_for = str(e)
                time.sleep(RETRY_SECONDS)
                continue
            waiting_for = ""
            for s in front.servers:
                if s is not kept:
                    threading.Thread(target=s.serve_forever, daemon=True).start()
            last_line = announce(node, port, front, last_line)
            reason = watch(host, cfg.dashboard_owner, node, front, cfg.dashboard_https)
            kept = front.servers[0] if front.fronted else None
            kept_front = front if front.fronted else None
            if reason != CERTIFICATE_RETRY:
                say(f"{reason}; listening again")
            for s in front.servers:
                if s is not kept:
                    stop_listener(s)
            if kept is None:
                forget_server()
    finally:
        forget_server()


RESUMING = "checking the tailscale serve mapping arete recorded"


def resume_fallback(node: Node, port: int, inside_serve: bool) -> Front | None:
    if proxy_record(port) is None:
        return None
    try:
        loopback = Server(LOOPBACK, port, node, FrontedLogins(), inside_serve, handler=FrontedHandler)
    except OSError:
        return None
    threading.Thread(target=loopback.serve_forever, daemon=True).start()
    return Front("http", [loopback], fronted=True, why=RESUMING)


def announce(node: Node, port: int, front: Front, last_line: str) -> str:
    line = listening_line(node, port, front)
    if line != last_line:
        say(line)
    record_server(node, port, front)
    return line


def listening_line(node: Node, port: int, front: Front) -> str:
    if front.tls:
        return f"dashboard listening on port {port} at {', '.join(node.addresses)} over HTTPS as {node.name}"
    if front.fronted:
        return (f"dashboard listening on port {port} at {LOOPBACK} behind tailscale serve as {node.name}; "
                f"{front.why}")
    if not front.why:
        return f"dashboard listening on port {port} at {', '.join(node.addresses)} over plain HTTP"
    return (f"dashboard listening on port {port} at {', '.join(node.addresses)} over plain HTTP; "
            f"{front.why}; trying again every hour")


def url(node: Node | None, port: int) -> str:
    found = util.read_json(paths.SERVER_FILE, None)
    mine = paths.state_and_config()
    recorded = isinstance(found, dict) and {k: found.get(k) for k in mine} == mine
    fallback = "https" if config.load().dashboard_https else "http"
    if recorded and found.get("behind"):
        scheme = "https"
    else:
        scheme = found["scheme"] if recorded and found.get("scheme") in ("http", "https") else fallback
    if node is None and scheme == "http":
        return f"http://<this machine's tailnet address>:{port}"
    if node is None:
        return f"{scheme}://<this machine's tailnet name>:{port}"
    return f"{scheme}://{node.name if scheme == 'https' else node.addresses[0]}:{port}"


def forget_server() -> None:
    found = util.read_json(paths.SERVER_FILE, None)
    if isinstance(found, dict) and found.get("pid") == os.getpid():
        paths.SERVER_FILE.unlink(missing_ok=True)
