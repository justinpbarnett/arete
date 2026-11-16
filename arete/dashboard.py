from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qs, unquote, urlsplit

from . import (
    board,
    capture,
    chat,
    codebase,
    config,
    control,
    devices,
    event_stream,
    events,
    gh,
    idempotency,
    install_files,
    jira,
    loop_run,
    oncall,
    paths,
    pipeline,
    principals,
    record,
    refusals,
    research,
    run_lines,
    scaffold,
    sessions,
    settings,
    tailnet,
    tasks,
    triage,
    util,
    webhooks,
    webpush,
)
from .refusals import Stated as Refusal
from .store import approvals as approvals_store
from .store import db as foundation
from .store import events as event_store
from .store import idempotency as claims
from .store import insights as insights_store
from .store import runs as run_store
from .store import writes

WEB_DIR = paths.PACKAGE_DIR / "web"
WEB_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".js": "text/javascript; charset=utf-8", ".webmanifest": "application/manifest+json",
             ".png": "image/png"}
CODE_MARK = b'<meta name="arete-code" content="">'
THEME_MARK = b'<meta name="theme-color" content="">'
MAX_BODY = 64 * 1024
STEERING = ("dismiss", "retry", "cancel")
ASK_PATH = "/api/asks/"
PUSH_PATH = "/api/push"
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
    colors = install_files.colors_from_css(next(body for f, body in bodies.items() if f.name == "pattern.css"))
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
            served[f"/{f.name}"] = (install_files.manifest(body, colors), WEB_TYPES[f.suffix])
        else:
            served[f"/{f.name}"] = (body, WEB_TYPES[f.suffix])
    for path, png in install_files.icons(colors).items():
        served[path] = (png, WEB_TYPES[".png"])
    return code, served


WEB_CODE, WEB_BY_PATH = web_snapshot()


@dataclass
class Gauge:
    count: int = 0
    guard: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def add(self, n: int) -> None:
        with self.guard:
            self.count += n

    def enter(self) -> bool:
        with self.guard:
            if self.closed:
                return False
            self.count += 1
            return True

    def close(self) -> None:
        with self.guard:
            self.closed = True

    def reopen(self) -> None:
        with self.guard:
            self.closed = False


POSTS_IN_FLIGHT = Gauge()


def whole_number(text: str) -> int:
    if not str(text).strip().isdecimal():
        raise Refusal(400, f"'{text}' is not a whole number.")
    return int(text)


RESTART_RETRY_SECONDS = 3
RESTARTING = "Arete is loading new code. Send this again in a few seconds."


class Restarting(Refusal):
    pass


class NoRoute(Exception):
    pass


def refusal_status(e: Exception) -> int | None:
    if isinstance(e, refusals.Refusal) and e.status is not None:
        return e.status
    return 502 if isinstance(e, (gh.GhError, jira.JiraError)) else None


def refusal_answer(e: Exception) -> dict:
    answer = {"error": as_sentence(str(e))}
    if getattr(e, "code", None):
        answer["code"] = e.code
    if hasattr(e, "current"):
        answer["current"] = e.current
    if isinstance(e, control.KeptFailure):
        answer["kept"] = True
    return answer


def as_sentence(text: str) -> str:
    text = text.strip()
    first_word = text.split(" ", 1)[0]
    if first_word.isalpha() and first_word.islower():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "?", "!")) else f"{text}."


def route_rest(template: str, path: str) -> str | None:
    if not template.endswith("/*"):
        return "" if path == template else None
    prefix = template[:-1]
    return path[len(prefix):] if path.startswith(prefix) else None


class Handler(BaseHTTPRequestHandler):
    server_version = "arete"
    sys_version = ""
    restarting = False

    def do_GET(self) -> None:
        self.answer("GET")

    def do_POST(self) -> None:
        entered = POSTS_IN_FLIGHT.enter()
        self.restarting = not entered
        try:
            self.answer("POST")
        finally:
            if entered:
                POSTS_IN_FLIGHT.add(-1)

    def drop_body(self) -> None:
        length = (self.headers.get("Content-Length") or "0").strip()
        if length.isdigit():
            self.rfile.read(min(int(length), webhooks.MAX_HOOK_BODY))

    def answer(self, method: str) -> None:
        with principals.acting("hand"), event_store.request_scope(f"{method} {urlsplit(self.path).path}"):
            self.write_receipt = None
            self.keyed_delivery = False
            self.idempotent_replayed = False
            return self.answer_as_person(method)

    def answer_as_person(self, method: str) -> None:
        try:
            parts = urlsplit(self.path)
            path = parts.path
            self.admit(method, path)
            if self.restarting:
                self.drop_body()
                raise Restarting(503, RESTARTING)
            if method == "GET" and path in WEB_BY_PATH:
                return self.send(200, *WEB_BY_PATH[path], "no-cache")
            if method == "POST" and path.startswith(webhooks.HOOK_PATH):
                got = webhooks.receive(unquote(path[len(webhooks.HOOK_PATH):]), self.headers, self.raw_body())
                return self.send_json(202 if got["queued"] else 200, got)
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            if method == "POST" and path == "/api/tasks" and "Idempotency-Key" in self.headers:
                return self.keyed_start()
            if method == "GET" and path in ("/api/events", "/api/v1/events"):
                return event_stream.serve(self, query, WEB_CODE)
            if method == "POST" and path == EVIDENCE_PATH:
                return self.send_json(200, self.stage_evidence(query))
            if method == "GET" and (item := EVIDENCE_ITEM.fullmatch(path)):
                return self.send_evidence(int(item[1]))
            if method == "GET" and (events := EVENTS_PATH.fullmatch(path)):
                return self.send_events(unquote(events[1]), query)
            result = self.api(method, path, query)
            if method == "POST" and self.write_receipt is None:
                self.write_receipt = event_store.committed_receipt()
                if isinstance(result, dict) and isinstance(result.get("run"), str) and result["run"]:
                    self.write_receipt = writes.receipt(f"run:{result['run']}", kind="run.started") or self.write_receipt
            self.send_json(200, result)
        except Exception as e:
            if self.keyed_delivery:
                raise
            if status := refusal_status(e):
                if isinstance(e, Restarting):
                    return self.send_json(status, refusal_answer(e), retry_after=RESTART_RETRY_SECONDS)
                if isinstance(e, idempotency.InFlight):
                    return self.send_json(status, refusal_answer(e), retry_after=1)
                return self.send_json(status, refusal_answer(e))
            said = type(e).__name__ if self.path.startswith((ASK_PATH, PUSH_PATH)) else f"{type(e).__name__}: {e}"
            self.log_error("%s", said)
            self.send_json(500, {"error": as_sentence(util.first_line(said, 300))})

    def keyed_start(self):
        body = self.body()
        sha = idempotency.fingerprint("POST", "/api/tasks", {}, body)
        with idempotency.owning(principals.current(), self.headers["Idempotency-Key"], sha) as context:
            row = claims.lookup(*context[:2])
            if row and row["request_sha"] != sha:
                raise idempotency.Reused("This key was used with different content")
            if row and row["status"]:
                self.write_receipt = claims.receipt(row["last_seq"])
                self.idempotent_replayed = True
                self.keyed_delivery = True
                return self.send(row["status"], bytes(row["response"]), "application/json", "no-store")
            try:
                result = (tasks.recover_start(row, body.get("force") is not False) if row else
                          tasks.start(*(str(body.get(k) or "") for k in ("repo", "prompt", "title", "kind", "ticket")),
                                      force=body.get("force") is not False, claim=context))
                status = 200
            except Exception as e:
                status = refusal_status(e)
                if status is None:
                    status = 500
                    said = f"{type(e).__name__}: {e}"
                    self.log_error("%s", said)
                    result = {"error": as_sentence(util.first_line(said, 300))}
                else:
                    result = refusal_answer(e)
            self.write_receipt = event_store.committed_receipt()
            if not row and status == 200 and result.get("run"):
                self.write_receipt = writes.receipt(f"run:{result['run']}", kind="run.started") or self.write_receipt
            if not self.write_receipt and (saved := claims.lookup(*context[:2])):
                self.write_receipt = claims.receipt(saved["first_seq"])
            payload = json.dumps(result, default=str).encode()
            seq = int(self.write_receipt["event_seq"].rsplit(".", 1)[1]) if self.write_receipt else None
            claims.complete(context, status, payload, seq)
            self.keyed_delivery = True
            return self.send(status, payload, "application/json", "no-store")

    def admit(self, method: str, path: str) -> None:
        node: tailnet.Node = self.server.node
        if tailnet.host_name(self.headers.get("Host", "")) not in node.names:
            raise Refusal(421, "This server answers only to its tailnet name or address.")
        if path.startswith(webhooks.HOOK_PATH):
            if method != "POST":
                raise Refusal(405, "A webhook takes POST.")
            return
        person = principals.for_login(self.server.logins.of(self.client_address[0]))
        if not person:
            raise Refusal(403, "The dashboard opens only for its owner's tailnet login.")
        principals.admit(person)
        if method == "POST" and self.headers.get("X-Arete") != "1":
            raise Refusal(403, "A change needs the X-Arete header, which the dashboard page sends.")

    def api(self, method: str, path: str, query: dict):
        if method == "GET" and path == "/api/v1/principals":
            return principals.listing(**{k: query[k] for k in ("kind", "role", "status") if k in query})
        if method == "GET" and path.startswith("/api/v1/principals/"):
            principal = unquote(path.removeprefix("/api/v1/principals/"))
            rows = principals.listing(id=principals.current() if principal == "me" else principal)
            if not rows:
                raise Refusal(404, "No such principal.")
            return rows[0]
        for verb, template, route in ROUTES:
            rest = route_rest(template, path) if verb == method else None
            if rest is not None:
                try:
                    if method == "GET":
                        with foundation.one_read():
                            return route(self, rest, query)
                    return route(self, rest, query)
                except NoRoute:
                    break
        raise Refusal(404, f"Nothing at {method} {path}.")

    def loop_action(self, name: str, action: str):
        if action in ("enable", "disable"):
            return control.switch(name, action == "enable")
        if action == "start":
            return loop_run.launch(name)
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
        said = triage.send(config.load_or_problem()[0], ref, questions, answers, str(b.get("repo") or ""))
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

    def raw_body(self, limit: int = webhooks.MAX_HOOK_BODY) -> bytes:
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

    def send_json(self, status: int, data, retry_after: int = 0) -> None:
        self.send(status, json.dumps(data, default=str).encode(), "application/json", "no-store", retry_after)

    def send_events(self, run_id: str, query: dict) -> None:
        if not RUN_ID.fullmatch(run_id):
            raise board.NotARunId(f"'{run_id}' is not a run id.")
        if not run_lines.path(run_id).exists() and not run_store.run_get(run_id, connect=foundation.reader):
            raise control.NotFound(f"nothing has the id {run_id}")
        start = whole_number(query.get("from", "0"))
        if query.get("follow") != "1":
            found = (line for line in run_lines.read(run_id) if run_lines.seq_of(line) > start)
            return self.send(200, b"".join(map(event_line, found)), EVENTS_TYPE, "no-store")
        self.head(200, EVENTS_TYPE, "no-store")
        self.close_connection = True
        try:
            self.end_headers()
            for line in run_lines.follow(run_id, start, run_ended(run_id)):
                self.wfile.write(event_line(line))
                self.wfile.flush()
        except OSError:
            pass

    def end_headers(self) -> None:
        self.send_header("X-Arete-Engine", codebase.STAMP)
        super().end_headers()

    def head(self, status: int, kind: str, cache: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Arete-Code", WEB_CODE)
        if getattr(self, "idempotent_replayed", False):
            self.send_header("Idempotent-Replayed", "true")
        if 200 <= status < 300 and getattr(self, "write_receipt", None):
            self.send_header("Arete-Seq", self.write_receipt["event_seq"])
        self.send_header("Content-Security-Policy", PAGE_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send(self, status: int, payload: bytes, kind: str, cache: str, retry_after: int = 0) -> None:
        self.head(status, kind, cache, len(payload))
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
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
        if not getattr(self, "raw_requestline", b"").startswith(tailnet.TLS_HANDSHAKE):
            super().log_message(format, *args)


class Redirect(Handler):
    def answer(self, method: str) -> None:
        node: tailnet.Node = self.server.node
        length = (self.headers.get("Content-Length") or "0").strip()
        if length.isdigit():
            self.rfile.read(min(int(length), webhooks.MAX_HOOK_BODY))
        path = self.path if self.path.startswith("/") else "/"
        self.send_response(308)
        self.send_header("Location", f"https://{node.name}:{self.server.server_port}{path}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def event_line(line: dict) -> bytes:
    return json.dumps(line, ensure_ascii=False, default=str).encode() + b"\n"


def run_ended(run_id: str) -> Callable[[], bool]:
    quiet_since = None

    def ended() -> bool:
        nonlocal quiet_since
        row = run_store.run_get(run_id, connect=foundation.reader) or {}
        if row.get("status") == "running":
            with record.dead(row) as run_is_dead:
                if not run_is_dead:
                    quiet_since = None
                    return False
        quiet_since = quiet_since or time.monotonic()
        return time.monotonic() - quiet_since >= END_GRACE_SECONDS

    return ended


def draft_choices() -> dict:
    cfg = config.load_or_problem()[0]
    return {"repos": [r.name for r in cfg.repos], "models": sessions.known_models(cfg),
            "default_model": cfg.model("loop")}


def research_ticket(ref: str, force: bool) -> dict:
    r = research.ticket(ref)
    if not r:
        raise control.NotFound(f"{ref} is not a research ticket on the triage board")
    return {"ref": r["ref"], "run": loop_run.launch("research", events=[{"ref": r["ref"], "force": force}])["id"]}


def forget_proposal(proposal_id: int) -> dict:
    if not approvals_store.forget_proposal(proposal_id):
        raise control.NotFound(f"#{proposal_id} is not an open memory fact or harness fix")
    return {"id": proposal_id}


def approve_rule(proposal_id: int) -> dict:
    if (approvals_store.proposal(proposal_id, connect=foundation.reader) or {}).get("status") not in ("approve", "filing"):
        raise control.NotFound(f"#{proposal_id} is not a rule waiting for approval")
    control.detach(["file-rule", str(proposal_id)])
    return {"id": proposal_id}


def reject_rule(proposal_id: int) -> dict:
    if not approvals_store.move_proposal(proposal_id, ("approve",), "rejected"):
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
        loop_run.launch("poll")
    except control.Busy:
        return "A poll is running now, so the next one picks it up."
    except Exception as e:
        return f"The poll did not start: {util.first_line(str(e), 200)}."
    return "Started a poll to build it."


def chat_session_parts(rest: str) -> list[str] | None:
    return unquote(rest).split("/") if "/sessions" in f"/{rest}" else None


def get_meta(handler, rest, query):
    with foundation.reader() as conn:
        meta = foundation.metadata(conn)
        stream = events.metadata(conn)
    return {"instance": {key: meta[f"instance_{key}"] for key in ("id", "tag", "name", "created")},
            "schema": {"version": int(meta["schema_version"]),
                       "oldest_writer": int(meta["oldest_writer"]) if meta["oldest_writer"] is not None else None},
            "stream": stream}


def get_push(handler, rest, query):
    return webpush.subscription_state()


def get_sidebar(handler, rest, query):
    return board.sidebar(chat.pulse())


def get_live(handler, rest, query):
    return board.live_board(chat.pulse()["draft"])


def get_runs(handler, rest, query):
    return board.runs_board(query, limit=whole_number(query.get("limit", str(board.RECENT_RUNS))))


def get_run(handler, rest, query):
    return board.run_board(unquote(rest))


def get_task(handler, rest, query):
    return tasks.view(unquote(rest), query.get("repo", ""))


def get_asks(handler, rest, query):
    return tasks.asks_view()


def get_health(handler, rest, query):
    return board.health_board()


def get_triage(handler, rest, query):
    return board.panel(board.triage_board)


def get_outcomes(handler, rest, query):
    return board.panel(board.outcomes)


def get_find(handler, rest, query):
    return board.find()


def get_usage(handler, rest, query):
    return board.usage_board(whole_number(query.get("days", str(board.USAGE_DAYS))))


def get_settings(handler, rest, query):
    return settings.current()


def get_capture(handler, rest, query):
    return capture.view(connect=foundation.reader)


def get_chats(handler, rest, query):
    return chat.listing()


def get_chat(handler, rest, query):
    parts = chat_session_parts(rest)
    if parts and len(parts) == 2 and parts[1] == "sessions":
        return chat.sessions_of(whole_number(parts[0]))
    if parts and len(parts) == 4 and parts[1] == "sessions" and parts[3] == "report":
        return chat.session_report(whole_number(parts[0]), parts[2])
    return chat.view(whole_number(unquote(rest)))


def get_log(handler, rest, query):
    run_id = unquote(rest)
    return {"id": run_id, "lines": board.run_log(run_id)}


def get_loops(handler, rest, query):
    return {"loops": control.loop_views(connect=foundation.reader), "serve": control.serve_beat(),
            "draft": draft_choices()}


def get_loop(handler, rest, query):
    return control.loop_detail(unquote(rest), connect=foundation.reader)


def get_incidents(handler, rest, query):
    return board.incidents_board()


def get_incident(handler, rest, query):
    return board.incident_board(unquote(rest))


def get_feed(handler, rest, query):
    return control.feed(unquote(rest), whole_number(query.get("after", "0")))


def get_sessions(handler, rest, query):
    return {"sessions": sessions.views(connect=foundation.reader), "choices": sessions.choices()}


def post_usage(handler, rest, query):
    insights_store.add_usage(handler.usage_row(), board.KEEP_USAGE_DAYS)
    return {}


def post_capture(handler, rest, query):
    return capture.save(handler.body())


def post_capture_context(handler, rest, query):
    return capture.add_context(handler.body(), WEB_CODE)


def post_capture_submit(handler, rest, query):
    return capture.submit(handler.body())


def post_capture_context_drop(handler, rest, query):
    return capture.drop_context()


def post_capture_discard(handler, rest, query):
    return capture.discard()


def post_capture_evidence(handler, rest, query):
    if drop := EVIDENCE_DROP.fullmatch(f"{EVIDENCE_PATH}/{rest}"):
        return capture.drop_evidence(int(drop[1]))
    raise NoRoute


def post_settings(handler, rest, query):
    return settings.save(handler.body())


def post_chat_new(handler, rest, query):
    return chat.new(str(handler.body().get("project") or ""))


def post_chat(handler, rest, query):
    parts = chat_session_parts(rest)
    if parts and len(parts) == 2 and parts[1] == "sessions":
        return handler.start_chat_session(whole_number(parts[0]))
    if parts and len(parts) == 4 and parts[1] == "sessions" and parts[3] == "stop":
        return chat.stop_session(whole_number(parts[0]), parts[2])
    chat_id, _, action = unquote(rest).rpartition("/")
    if action == "send":
        return chat.send(whole_number(chat_id), handler.chat_text())
    if action == "file":
        return chat.file_draft(whole_number(chat_id))
    if action == "drop":
        return chat.drop_draft(whole_number(chat_id))
    raise NoRoute


def post_incident_switch(handler, rest, query):
    on = handler.body().get("on")
    if type(on) is not bool:
        raise control.Refused('switch takes {"on": true} or {"on": false}')
    return oncall.switch(on)


def post_incident(handler, rest, query):
    incident_id, _, action = unquote(rest).rpartition("/")
    if action in STEERING:
        return getattr(oncall, action)(board.incident_number(incident_id))
    raise NoRoute


def post_loop_new(handler, rest, query):
    return handler.draft_loop()


def post_loop(handler, rest, query):
    name, _, action = unquote(rest).rpartition("/")
    return handler.loop_action(name, action)


def post_run(handler, rest, query):
    run_id, _, action = unquote(rest).partition("/")
    body = handler.body()
    base_version = body.get("base_version")
    result = None
    if action == "stop":
        result = control.stop_run(run_id, base_version, public=True)
    elif action == "answer":
        result = tasks.reply(run_id, str(body.get("text") or ""))
    elif action == "resume":
        result = tasks.resume(run_id, str(body.get("step") or ""), base_version)
    elif action == "repair":
        result = tasks.repair(run_id, base_version)
    elif action.startswith("steps/"):
        result = tasks.run_step(run_id, action[len("steps/"):], body.get("set") or {}, base_version)
    else:
        raise NoRoute
    handler.write_receipt = event_store.committed_receipt(f"run:{run_id}", first=action == "stop")
    if result.get("run") and result["run"] != run_id:
        handler.write_receipt = writes.receipt(f"run:{result['run']}", kind="run.started")
    return result


def post_push_subscribe(handler, rest, query):
    return webpush.subscribe(handler.body(), principals.contact(principals.current()))


def post_push_remove(handler, rest, query):
    return webpush.remove(str(handler.body().get("id") or ""))


def post_push_test(handler, rest, query):
    return webpush.send_test(config.load_or_problem()[0])


def post_ask(handler, rest, query):
    ask_id, _, action = unquote(rest).partition("/")
    if action == "answer":
        return tasks.answer_ask(ask_id, handler.body())
    raise NoRoute


def post_approval(handler, rest, query):
    return control.decide(whole_number(unquote(rest)), handler.body().get("approve") is True)


def post_task(handler, rest, query):
    b = handler.body()
    return tasks.start(*(str(b.get(k) or "") for k in ("repo", "prompt", "title", "kind", "ticket")),
                       force=b.get("force") is not False)


def post_sessions(handler, rest, query):
    return handler.launch_session()


def post_session(handler, rest, query):
    session_id, _, action = unquote(rest).rpartition("/")
    if action == "send":
        return sessions.send(session_id, str(handler.body().get("text", "")))
    if action == "stop":
        return control.stop_run(session_id)
    raise NoRoute


def post_device(handler, rest, query):
    name, _, action = unquote(rest).rpartition("/")
    if action == "probe":
        return devices.probe(name)
    raise NoRoute


def post_triage_refresh(handler, rest, query):
    return {"run": loop_run.launch("triage")["id"]}


def post_triage(handler, rest, query):
    ref, _, action = unquote(rest).rpartition("/")
    if action == "send":
        return handler.send_ticket(ref)
    if action == "research":
        return research_ticket(ref, handler.body().get("force") is True)
    raise NoRoute


def post_checks_run(handler, rest, query):
    return {"run": loop_run.launch("heartbeat")["id"]}


def post_skipped(handler, rest, query):
    name, _, action = unquote(rest).rpartition("/")
    if action == "run":
        return pipeline.run_skipped(name)
    raise NoRoute


def post_proposal(handler, rest, query):
    proposal_id, _, action = unquote(rest).rpartition("/")
    if action == "done":
        return forget_proposal(whole_number(proposal_id))
    if action == "approve":
        return approve_rule(whole_number(proposal_id))
    if action == "reject":
        return reject_rule(whole_number(proposal_id))
    raise NoRoute


def post_serve_install(handler, rest, query):
    return install_serve(handler.server.inside_serve)


ROUTES = (
    ("GET", "/api/meta", get_meta),
    ("GET", PUSH_PATH, get_push),
    ("GET", "/api/sidebar", get_sidebar),
    ("GET", "/api/live", get_live),
    ("GET", "/api/runs", get_runs),
    ("GET", "/api/runs/*", get_run),
    ("GET", "/api/tasks/*", get_task),
    ("GET", "/api/asks", get_asks),
    ("GET", "/api/health", get_health),
    ("GET", "/api/triage", get_triage),
    ("GET", "/api/outcomes", get_outcomes),
    ("GET", "/api/find", get_find),
    ("GET", "/api/usage", get_usage),
    ("GET", "/api/settings", get_settings),
    ("GET", "/api/capture", get_capture),
    ("GET", "/api/chats", get_chats),
    ("GET", "/api/chats/*", get_chat),
    ("GET", "/api/log/*", get_log),
    ("GET", "/api/loops", get_loops),
    ("GET", "/api/loops/*", get_loop),
    ("GET", "/api/incidents", get_incidents),
    ("GET", "/api/incidents/*", get_incident),
    ("GET", "/api/feed/*", get_feed),
    ("GET", "/api/sessions", get_sessions),
    ("POST", "/api/usage", post_usage),
    ("POST", "/api/capture", post_capture),
    ("POST", "/api/capture/context", post_capture_context),
    ("POST", "/api/capture/submit", post_capture_submit),
    ("POST", "/api/capture/context/drop", post_capture_context_drop),
    ("POST", "/api/capture/discard", post_capture_discard),
    ("POST", "/api/capture/evidence/*", post_capture_evidence),
    ("POST", "/api/settings", post_settings),
    ("POST", "/api/chats/new", post_chat_new),
    ("POST", "/api/chats/*", post_chat),
    ("POST", "/api/incidents/switch", post_incident_switch),
    ("POST", "/api/incidents/*", post_incident),
    ("POST", "/api/loops/new", post_loop_new),
    ("POST", "/api/loops/*", post_loop),
    ("POST", "/api/runs/*", post_run),
    ("POST", f"{PUSH_PATH}/subscribe", post_push_subscribe),
    ("POST", f"{PUSH_PATH}/remove", post_push_remove),
    ("POST", f"{PUSH_PATH}/test", post_push_test),
    ("POST", "/api/asks/*", post_ask),
    ("POST", "/api/approvals/*", post_approval),
    ("POST", "/api/tasks", post_task),
    ("POST", "/api/sessions", post_sessions),
    ("POST", "/api/sessions/*", post_session),
    ("POST", "/api/devices/*", post_device),
    ("POST", "/api/triage/refresh", post_triage_refresh),
    ("POST", "/api/triage/*", post_triage),
    ("POST", "/api/checks/run", post_checks_run),
    ("POST", "/api/skipped/*", post_skipped),
    ("POST", "/api/proposals/*", post_proposal),
    ("POST", "/api/serve/install", post_serve_install),
)


def serve(port: int | None = None, inside_serve: bool = False, host: tailnet.Tailscale | None = None) -> NoReturn:
    cfg = config.load_or_problem()[0]
    port = port or cfg.dashboard_port
    while True:
        try:
            foundation.ensure_schema()
            break
        except foundation.IncompatibleSchema:
            if not inside_serve:
                raise
            time.sleep(RESTART_RETRY_SECONDS)
    chat.recover()
    if inside_serve:
        capture.recover()
    else:
        from . import serve

        with util.lock(serve.SERVE_LOCK) as free:
            if free:
                capture.recover()
    tailnet.serve(tailnet.Handlers(Handler, Redirect), cfg, port, inside_serve, host)
