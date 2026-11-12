from __future__ import annotations

import http.client
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from urllib.parse import quote, urlencode, urlsplit

from . import config, lines, paths, util

ENV = "ARETE_SERVER"
CONNECT_SECONDS = 5
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


def named_server(wanted: str) -> tuple[str, int]:
    parts = urlsplit(wanted if "://" in wanted else f"http://{wanted}")
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        host, port = "", None
    if parts.scheme != "http" or not host:
        raise Failed({"error": f"{ENV}={wanted} is not http://HOST:PORT"})
    return host, port or config.load().dashboard_port


def server() -> tuple[str, int]:
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
    return next((a for a in addresses if ":" not in a), addresses[0]), int(found["port"])


def address(host: str, port: int) -> str:
    return f"http://{f'[{host}]' if ':' in host else host}:{port}"


def request(method: str, path: str, body: dict | None = None,
            read_seconds: float | None = CALL_SECONDS) -> http.client.HTTPResponse:
    host, port = server()
    conn = http.client.HTTPConnection(host, port, timeout=CONNECT_SECONDS)
    try:
        conn.connect()
    except OSError as e:
        raise Down(f"the arete server is not up at {address(host, port)} ({e.strerror or e}); start it with "
                   f"`arete serve`, or set {ENV}") from None
    conn.sock.settimeout(read_seconds)
    headers, payload = {"X-Arete": "1"}, None
    if method == "POST":
        headers["Content-Type"], payload = "application/json", json.dumps(body or {}).encode()
    try:
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        if resp.status < 400:
            return resp
        text = resp.read()
    except BROKE as e:
        raise Failed({"error": f"the server at {address(host, port)} broke off: {e}"}) from None
    try:
        answer = json.loads(text)
    except ValueError:
        answer = None
    raise Failed(answer if isinstance(answer, dict) and "error" in answer
                 else {"error": f"the server at {address(host, port)} answered {resp.status}"}, resp.status)


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
