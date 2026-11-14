from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from . import config, lines, paths, util

ENV = "ARETE_SERVER"
IDENTITY_HEADER = "Tailscale-User-Login"
CONNECT_SECONDS = 5
PROBE_SECONDS = 1
CALL_SECONDS = 120
RETRY_SECONDS = 2
RECONNECT_SECONDS = 120
WAIT_EVENTS = ("step_end", "question", "error", lines.END)
BROKE = (OSError, http.client.HTTPException)


class Down(Exception):
    pass


class Failed(Exception):
    def __init__(self, answer: dict, code: int = 0):
        super().__init__(str(answer.get("error", "")))
        self.answer, self.code = answer, code


@dataclass(frozen=True)
class Server:
    scheme: str
    host: str
    port: int
    name: str
    login: str = ""


def named_server(wanted: str) -> Server:
    parts = urlsplit(wanted if "://" in wanted else f"http://{wanted}")
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        host, port = "", None
    if parts.scheme not in ("http", "https") or not host:
        raise Failed({"error": f"{ENV}={wanted} is not http://HOST:PORT or https://HOST:PORT"})
    return Server(parts.scheme, host, port or config.load().dashboard_port, host)


def server() -> Server:
    if wanted := os.environ.get(ENV, "").strip():
        return named_server(wanted)
    found, mine = util.read_json(paths.SERVER_FILE, None), paths.state_and_config()
    where = paths.short_path(paths.STATE_DIR)
    if not isinstance(found, dict) or not (found.get("addresses") and found.get("port")) \
            or not util.pid_alive(found.get("pid")):
        raise Down(f"no arete server is up on the state in {where}; start it with `arete serve`, or set {ENV}")
    if {k: found.get(k) for k in mine} != mine:
        raise Down(f"the arete server named in {where} works on {found.get('state')} with {found.get('config')}, "
                   f"not on {mine['state']} with {mine['config']}; set {ENV} to call it anyway")
    addresses = [str(a) for a in found["addresses"]]
    scheme = found.get("scheme") or "http"
    host, port = next((a for a in addresses if ":" not in a), addresses[0]), int(found["port"])
    here = Server(scheme, host, port, found.get("host") or host, str(found.get("login") or ""))
    try:
        socket.create_connection((host, port), timeout=PROBE_SECONDS).close()
    except OSError as e:
        raise Down(f"the arete server named in {where} does not answer on {address(here)} ({e}); start it "
                   f"with `arete serve`, or set {ENV}") from None
    return here


def headers_for(where: Server) -> dict[str, str]:
    headers = {"X-Arete": "1"}
    if where.scheme == "http" and where.name != where.host:
        headers["Host"] = f"{where.name}:{where.port}"
    if where.login:
        headers[IDENTITY_HEADER] = where.login
    return headers


def address(where: Server) -> str:
    return f"{where.scheme}://{f'[{where.host}]' if ':' in where.host else where.host}:{where.port}"


class SecureConnection(http.client.HTTPSConnection):
    def __init__(self, where: Server, timeout: float):
        self.tls = ssl.create_default_context()
        super().__init__(where.name, where.port, timeout=timeout, context=self.tls)
        self.address = where.host

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self.tls.wrap_socket(sock, server_hostname=self.host)
        except OSError:
            sock.close()
            raise


def connect(where: Server) -> http.client.HTTPConnection:
    conn = SecureConnection(where, CONNECT_SECONDS) if where.scheme == "https" \
        else http.client.HTTPConnection(where.host, where.port, timeout=CONNECT_SECONDS)
    try:
        conn.connect()
    except ssl.SSLError as e:
        raise Down(f"the certificate of the arete server at {address(where)} was not accepted "
                   f"({getattr(e, 'verify_message', None) or e.reason or e})") from None
    except OSError as e:
        raise Down(f"the arete server is not up at {address(where)} ({e.strerror or e}); start it with "
                   f"`arete serve`, or set {ENV}") from None
    return conn


def moved_to(resp: http.client.HTTPResponse, where: Server) -> Server | None:
    target = urlsplit(resp.getheader("Location") or "")
    if resp.status not in (301, 302, 307, 308) or target.scheme != "https" or not target.hostname:
        return None
    try:
        port = target.port or 443
    except ValueError:
        return None
    if port != where.port:
        return None
    return Server("https", where.host, where.port, target.hostname)


def request(method: str, path: str, body: dict | None = None,
            read_seconds: float | None = CALL_SECONDS) -> http.client.HTTPResponse:
    where = server()
    payload = json.dumps(body or {}).encode() if method == "POST" else None
    followed = False
    while True:
        conn = connect(where)
        conn.sock.settimeout(read_seconds)
        headers = headers_for(where)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, payload, headers)
            resp = conn.getresponse()
            if not followed and (moved := moved_to(resp, where)):
                resp.read()
                conn.close()
                where, followed = moved, True
                continue
            if resp.status < 400:
                return resp
            text = resp.read()
        except BROKE as e:
            raise Failed({"error": f"the server at {address(where)} broke off: {e}"}) from None
        break
    try:
        answer = json.loads(text)
    except ValueError:
        answer = None
    raise Failed(answer if isinstance(answer, dict) and "error" in answer
                 else {"error": f"the server at {address(where)} answered {resp.status}"}, resp.status)


def call(method: str, path: str, body: dict | None = None) -> dict:
    resp = request(method, path, body)
    try:
        answer = json.loads(resp.read())
    except BROKE as e:
        raise Failed({"error": f"the server broke off: {e}"}) from None
    except ValueError:
        raise Failed({"error": f"the server answered {method} {path} with no JSON"}) from None
    return answer


def stream(path: str, query: dict) -> Iterator[dict]:
    return parsed(request("GET", f"{path}?{urlencode(query)}", read_seconds=None))


def parsed(resp: http.client.HTTPResponse) -> Iterator[dict]:
    try:
        for raw in iter(resp.readline, b""):
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict):
                yield line
    except BROKE as e:
        raise Failed({"error": f"the server broke off the lines: {e}"}) from None
    finally:
        resp.close()


def run_path(run_id: str, action: str) -> str:
    return f"/api/runs/{quote(run_id, safe='')}/{action}"


def events(run_id: str, start: int = 0, follow: bool = False) -> Iterator[dict]:
    return stream(run_path(run_id, "events"), {"from": start, **({"follow": "1"} if follow else {})})


def run_status(run_id: str) -> str:
    try:
        return str((call("GET", f"/api/runs/{quote(run_id, safe='')}").get("run") or {}).get("status") or "")
    except (Down, Failed):
        return ""


def followed(run_id: str, start: int = 0) -> Iterator[dict]:
    return kept(run_id, start, events(run_id, start, follow=True))


def kept(run_id: str, seen: int, read: Iterator[dict] | None) -> Iterator[dict]:
    lost = None
    while True:
        fresh, whole = False, read is not None
        try:
            for line in read or ():
                seen, fresh, lost = max(seen, lines.seq_of(line)), True, None
                yield line
                if line.get("event") == lines.END:
                    return
        except Failed:
            whole = False
        lost = lost or time.monotonic()
        status = run_status(run_id)
        if whole and not fresh and status not in ("", "running"):
            raise Failed({"error": f"run {run_id} ended {status} with no report line"})
        if time.monotonic() - lost > RECONNECT_SECONDS:
            raise Failed({"error": f"the server stopped sending run {run_id}'s lines for {RECONNECT_SECONDS} s, "
                                   f"and the run may still be going; `arete wait {run_id}` follows it again"})
        time.sleep(RETRY_SECONDS)
        try:
            read = events(run_id, seen, follow=True)
        except (Down, Failed):
            read = None


def emit(line: dict) -> None:
    print(json.dumps(line, ensure_ascii=False, default=str), flush=True)


def failed(e: Failed) -> int:
    emit({**e.answer, **({"code": e.code} if e.code else {})})
    return 1


def steered(act: Callable[[], int]) -> int:
    try:
        return act()
    except Down as e:
        emit({"error": str(e)})
    except Failed as e:
        return failed(e)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    return 1


def say(method: str, path: str, body: dict | None = None) -> int:
    return steered(lambda: emit(call(method, path, body)) or 0)


def prompted(i: int, q: dict) -> dict:
    print(f"{i}. {q['header']}: {q['question']}", file=sys.stderr)
    kind = q["kind"]
    if kind == "secret":
        print(f"   arete stores no secret value yet, so {q['name']} is declined", file=sys.stderr)
        return {"declined": True}
    for n, o in enumerate(q.get("options") or [], 1):
        print(f"   {n}. {o['label']}: {o['description']}", file=sys.stderr)
    for n, s in enumerate(q.get("steps") or [], 1):
        print(f"   step {n}: {s}", file=sys.stderr)
    print("   Enter to confirm" if kind == "done" else
          "   option numbers, Enter for the first, or other words" if kind == "choice" else
          "   your answer, Enter to decline", file=sys.stderr)
    line = sys.stdin.readline().strip()
    if kind == "done":
        return {"done": True}
    if kind == "choice":
        numbers = line.replace(",", " ").split()
        if not line:
            return {"choice": 1}
        if all(x.isdigit() for x in numbers):
            return {"choice": [int(x) for x in numbers] if len(numbers) > 1 else int(numbers[0])}
    return {"text": line} if line else {"declined": True}


def answered_ask(run_id: str, ask_id: str, raw: str) -> dict:
    open_asks = [a for a in call("GET", "/api/asks")["asks"]
                 if a["run"] == run_id and (not ask_id or a["id"] == ask_id)]
    if not open_asks:
        raise Failed({"error": f"run {run_id} has no open ask"})
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            raise Failed({"error": "--json is not JSON"}) from None
        if not isinstance(body, dict):
            raise Failed({"error": "--json must be a JSON object"})
    else:
        body = {"answers": [prompted(i, q) for i, q in enumerate(open_asks[0]["questions"], 1)]}
    return call("POST", f"/api/asks/{quote(open_asks[0]['id'], safe='')}/answer", body)


def answer(run_id: str, text: str, raw: str, ask_id: str) -> int:
    if text and (raw or ask_id):
        return failed(Failed({"error": "give the answer as text, or with --json and --ask, not both"}))
    if text:
        return say("POST", run_path(run_id, "answer"), {"text": text})
    return steered(lambda: emit(answered_ask(run_id, ask_id, raw)) or 0)


def wait(run_id: str) -> int:
    asked = False
    try:
        for line in followed(run_id):
            event = line.get("event")
            if not asked and event in WAIT_EVENTS:
                emit(line)
                asked = event == "question"
    except Failed:
        if not asked:
            raise
    return 0


def steps_of(run_id: str, read: list[dict]) -> list[dict]:
    rows, opened, report = [], {}, None
    for line in read:
        event, step = line.get("event"), str(line.get("step") or "")
        if event == "step_start":
            opened[step] = line
        elif event == "step_end":
            opened.pop(step, None)
            rows.append({"run": run_id, "event": "step", "step": step, "try": line.get("try"),
                         "state": line.get("result") or ("pass" if line.get("ok") else "fail"),
                         **{k: line.get(k) for k in ("seconds", "cost_usd", "model", "runner", "error")}})
        elif event == lines.END:
            report = line
    left = "unfinished" if report else "running"
    rows += [{"run": run_id, "event": "step", "step": step, "try": line.get("try"), "state": left}
             for step, line in opened.items()]
    return rows + ([report] if report else [])
