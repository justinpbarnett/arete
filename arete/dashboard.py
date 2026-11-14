from __future__ import annotations

import atexit
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
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
    config,
    control,
    devices,
    engine,
    gh,
    hooks,
    jira,
    lines,
    loops,
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
)

WEB_DIR = paths.PACKAGE_DIR / "web"
WEB_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".js": "text/javascript; charset=utf-8"}
CODE_MARK = b'<meta name="arete-code" content="">'
TAILSCALE_APPS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  "/Applications/*/Tailscale.app/Contents/MacOS/Tailscale")
TAILSCALE_AS_CLI = {"TAILSCALE_BE_CLI": "1"}
RETRY_SECONDS = 10
WATCH_SECONDS = 30
WHOIS_SECONDS = 60
MAX_BODY = 64 * 1024
TLS_HANDSHAKE = b"\x16\x03"
LOG_PATH = "/api/log/"
LOOP_PATH = "/api/loops/"
FEED_PATH = "/api/feed/"
RUN_PATH = "/api/runs/"
APPROVAL_PATH = "/api/approvals/"
ASK_PATH = "/api/asks/"
SESSION_PATH = "/api/sessions/"
DEVICE_PATH = "/api/devices/"
TRIAGE_PATH = "/api/triage/"
SKIPPED_PATH = "/api/skipped/"
PROPOSAL_PATH = "/api/proposals/"
CHAT_PATH = "/api/chats/"
TASK_PATH = "/api/tasks/"
EVENTS_PATH = re.compile(r"/api/runs/([^/]+)/events")
EVENTS_TYPE = "application/x-ndjson"
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
    served = {}
    for f, body in bodies.items():
        if f.name == "index.html":
            if CODE_MARK not in body:
                raise RuntimeError(f"index.html lost {CODE_MARK.decode()}, so an open page could not tell when its "
                                   "code is old.")
            served["/"] = (body.replace(CODE_MARK, f'<meta name="arete-code" content="{code}">'.encode()),
                           WEB_TYPES[f.suffix])
        else:
            served[f"/{f.name}"] = (body, WEB_TYPES[f.suffix])
    return code, served


WEB_CODE, WEB_BY_PATH = web_snapshot()


def tailscale(*args: str) -> dict:
    exe = shutil.which("tailscale") or next((m for pattern in TAILSCALE_APPS for m in sorted(glob.glob(pattern))), "")
    if not exe:
        return {}
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, errors="replace", timeout=10, check=False,
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
class Gauge:
    count: int = 0
    guard: threading.Lock = field(default_factory=threading.Lock)

    def add(self, n: int) -> None:
        with self.guard:
            self.count += n


POSTS_IN_FLIGHT = Gauge()


@dataclass
class Logins:
    seen: dict[str, tuple[float, str]] = field(default_factory=dict)
    guard: threading.Lock = field(default_factory=threading.Lock)

    def of(self, address: str) -> str:
        with self.guard:
            seen = self.seen.get(address)
        if seen and time.monotonic() - seen[0] < WHOIS_SECONDS:
            return seen[1]
        login = (tailscale("whois", "--json", address).get("UserProfile") or {}).get("LoginName", "")
        if login:
            with self.guard:
                self.seen[address] = (time.monotonic(), login)
        return login


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


STATUS_OF_REFUSAL = {capture.Refused: 400, board.NotARunId: 400, board.UnknownFilter: 400, settings.Refused: 400, chat.Busy: 409,
                     chat.NoDraft: 409, chat.NoChat: 404, chat.BadProject: 400, gh.GhError: 502, jira.JiraError: 502,
                     control.NotFound: 404, control.Busy: 409, control.Refused: 400, loops.LoopError: 400,
                     devices.DeviceError: 400, triage.NotOnBoard: 404, triage.NotSendable: 409, triage.BadAnswers: 400,
                     triage.TrackerFailed: 502, pipeline.NoFreeSlot: 409, tasks.TrackerFailed: 502}


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
            if method == "GET" and (events := EVENTS_PATH.fullmatch(path)):
                return self.send_events(unquote(events[1]), query)
            self.send_json(200, self.api(method, path, query))
        except Exception as e:
            if status := refusal_status(e):
                return self.send_json(status, refusal_answer(e))
            self.log_error("%s", f"{type(e).__name__}: {e}")
            self.send_json(500, {"error": as_sentence(util.first_line(f"{type(e).__name__}: {e}", 300))})

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
            if path == "/api/capture/context/drop":
                return capture.drop_context()
            if path == "/api/capture/discard":
                return capture.discard()
            if path == "/api/settings":
                return settings.save(self.body())
            if path == "/api/chats/new":
                return chat.new(str(self.body().get("project") or ""))
            if path.startswith(CHAT_PATH):
                chat_id, action = last_part(path, CHAT_PATH)
                if action == "send":
                    return chat.send(whole_number(chat_id), self.chat_text())
                if action == "file":
                    return chat.file_draft(whole_number(chat_id))
                if action == "drop":
                    return chat.drop_draft(whole_number(chat_id))
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


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: str, port: int, node: Node, logins: Logins, inside_serve: bool):
        self.address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        self.node, self.logins, self.inside_serve = node, logins, inside_serve
        super().__init__((address, port), Handler)


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


def open_servers(node: Node, port: int, logins: Logins, inside_serve: bool) -> list[Server]:
    servers: list[Server] = []
    try:
        for address in node.addresses:
            servers.append(Server(address, port, node, logins, inside_serve))
    except OSError:
        for s in servers:
            s.server_close()
        raise
    return servers


def say(text: str) -> None:
    print(f"{util.iso()} {text}", flush=True)


def serve(port: int | None = None, inside_serve: bool = False) -> NoReturn:
    cfg = config.load()
    port = port or cfg.dashboard_port
    state.ensure_schema()
    chat.recover()
    logins = Logins()
    waiting_for = ""
    atexit.register(forget_server)
    try:
        while True:
            node = this_node(cfg.dashboard_owner)
            try:
                if node is None:
                    raise OSError("tailscale is not running or reports no address")
                servers = open_servers(node, port, logins, inside_serve)
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
            util.write_json(paths.SERVER_FILE, {"pid": os.getpid(), "addresses": list(node.addresses), "port": port,
                                                **paths.state_and_config(), "at": util.iso()})
            while this_node(cfg.dashboard_owner) == node:
                time.sleep(WATCH_SECONDS)
            say("the tailnet address or owner changed; listening again")
            for s in servers:
                s.shutdown()
                s.server_close()
    finally:
        forget_server()


def forget_server() -> None:
    found = util.read_json(paths.SERVER_FILE, None)
    if isinstance(found, dict) and found.get("pid") == os.getpid():
        paths.SERVER_FILE.unlink(missing_ok=True)
