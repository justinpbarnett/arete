from __future__ import annotations

import getpass
import http.client
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from . import config, paths, run_lines, util

ENV = "ARETE_SERVER"
CONNECT_SECONDS = 5
PROBE_SECONDS = 1
CALL_SECONDS = 120
RETRY_SECONDS = 2
RESTART_WAIT_SECONDS = 30
RECONNECT_SECONDS = 120
WAIT_EVENTS = ("step_end", "question", "error", run_lines.END)
BROKE = (OSError, http.client.HTTPException)
_ACQUISITION_LOCK = threading.Lock()
_acquisition_thread: threading.Thread | None = None


class Down(Exception):
    pass


class Failed(Exception):
    def __init__(self, answer: dict, code: int = 0):
        super().__init__(str(answer.get("error", "")))
        self.answer, self.code = answer, code


class Broken(Failed):
    pass


class InFlight(Failed):
    seconds = 1


class Restarting(Exception):
    def __init__(self, answer: dict, seconds: int):
        super().__init__(str(answer.get("error", "")))
        self.answer, self.seconds = answer, seconds


@dataclass(frozen=True)
class Server:
    scheme: str
    host: str
    port: int
    name: str


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
    here = Server(scheme, host, port, found.get("host") or host)
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
    return headers


def address(where: Server) -> str:
    return f"{where.scheme}://{f'[{where.host}]' if ':' in where.host else where.host}:{where.port}"


def connect(where: Server, seconds: float = CONNECT_SECONDS) -> http.client.HTTPConnection:
    conn = util.NamedTlsConnection(where.host, where.port, where.name, seconds) if where.scheme == "https" \
        else http.client.HTTPConnection(where.host, where.port, timeout=seconds)
    try:
        conn.connect()
    except ssl.SSLError as e:
        conn.close()
        raise Down(f"the certificate of the arete server at {address(where)} was not accepted "
                   f"({getattr(e, 'verify_message', None) or e.reason or e})") from None
    except OSError as e:
        conn.close()
        raise Down(f"the arete server is not up at {address(where)} ({e.strerror or e}); start it with "
                   f"`arete serve`, or set {ENV}") from None
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def acquired_connection(where, deadline):
    if deadline is None:
        conn = connect(where)
        try:
            yield where, conn
        except BaseException:
            conn.close()
            raise
        return

    global _acquisition_thread
    lock, done = threading.Lock(), threading.Event()
    abandoned, delivered = False, False
    result, error = None, None

    def acquire():
        nonlocal result, error
        conn = None
        try:
            with lock:
                if abandoned or time.monotonic() >= deadline:
                    return
            found = server() if where is None else where
            with lock:
                if abandoned or time.monotonic() >= deadline:
                    return
            conn = connect(found, min(CONNECT_SECONDS, budget_left(deadline)))
            with lock:
                if not abandoned and time.monotonic() < deadline:
                    result, conn = (found, conn), None
        except BaseException as e:
            error = e
        finally:
            try:
                if conn is not None:
                    conn.close()
            finally:
                done.set()

    try:
        with _ACQUISITION_LOCK:
            budget_left(deadline)
            # One live acquisition thread, no queue, even if a resolver never returns.
            if _acquisition_thread is not None and _acquisition_thread.is_alive():
                raise Down("a previous arete server connection is still being acquired; try again later")
            worker = threading.Thread(target=acquire, name="arete-http-acquire", daemon=True)
            _acquisition_thread = worker
            worker.start()
        done.wait(budget_left(deadline))
        budget_left(deadline)
        worker.join(budget_left(deadline))
        budget_left(deadline)
        if error is not None:
            raise error
        if result is None:
            raise Broken({"error": "task start retry budget expired"})
        yield result
        delivered = True
    finally:
        with lock:
            abandoned = True
            unused, result = result, None
        if not delivered and unused is not None:
            unused[1].close()


def request(method: str, path: str, body: dict | None = None,
            read_seconds: float | None = CALL_SECONDS, key: str | None = None) -> http.client.HTTPResponse:
    deadline = None
    restart = None
    while True:
        within = None if deadline is None else deadline - time.monotonic()
        if within is not None and within <= 0:
            raise Failed(restart.answer, 503)
        try:
            return request_once(method, path, body, read_seconds, within,
                                **({"key": key} if key is not None else {}),
                                **({"deadline": deadline} if deadline is not None else {}))
        except Restarting as e:
            restart, pause = e, e.seconds
        except Down:
            if restart is None:
                raise
            pause = RETRY_SECONDS
        if deadline is None:
            deadline = time.monotonic() + RESTART_WAIT_SECONDS
        left = deadline - time.monotonic()
        if left <= 0:
            raise Failed(restart.answer, 503)
        time.sleep(min(pause, left))


def request_once(method: str, path: str, body: dict | None = None,
                 read_seconds: float | None = CALL_SECONDS, within: float | None = None,
                 key: str | None = None, *, deadline: float | None = None) -> http.client.HTTPResponse:
    if deadline is None and within is not None:
        deadline = time.monotonic() + within
    where = server() if deadline is None else None
    payload = json.dumps(body or {}).encode() if method == "POST" else None
    followed = False
    if within is not None:
        read_seconds = within if read_seconds is None else min(read_seconds, within)
    while True:
        if deadline is not None:
            within = budget_left(deadline)
            read_seconds = within if read_seconds is None else min(read_seconds, within)
        with acquired_connection(where, deadline) as (where, conn):
            transport = conn.sock
            resp, returned = None, False
            try:
                with transport_budget(transport, deadline):
                    transport.settimeout(read_seconds if deadline is None else budget_left(deadline))
                    headers = headers_for(where)
                    if key is not None:
                        headers["Idempotency-Key"] = key
                    if payload is not None:
                        headers["Content-Type"] = "application/json"
                    if deadline is not None:
                        budget_left(deadline)
                    conn.request(method, path, payload, headers)
                    if deadline is not None:
                        transport.settimeout(budget_left(deadline))
                    resp = conn.getresponse()
                    resp.arete_transport = transport
                if not followed and (moved := util.https_upgrade(resp, where.port)):
                    read_response(resp, deadline)
                    where, followed = Server("https", where.host, where.port, moved.hostname), True
                    continue
                if resp.status < 400:
                    returned = True
                    return resp
                text = read_response(resp, deadline)
            except BROKE as e:
                raise Broken({"error": f"the server at {address(where)} broke off: {e}"}) from None
            finally:
                if not returned:
                    try:
                        if resp is not None:
                            close_response(resp)
                    finally:
                        conn.close()
        break
    try:
        answer = json.loads(text)
    except ValueError:
        answer = None
    failure = (answer if isinstance(answer, dict) and "error" in answer
               else {"error": f"the server at {address(where)} answered {resp.status}"})
    wait = (resp.getheader("Retry-After") or "").strip()
    if key is not None and resp.status == 409 and failure.get("code") == "idempotency_in_flight":
        failure = InFlight(failure, resp.status)
        failure.seconds = int(wait) if wait.isascii() and wait.isdigit() else 1
        raise failure
    if resp.status == 503 and wait.isascii() and wait.isdigit():
        raise Restarting(failure, int(wait))
    raise Failed(failure, resp.status)


def budget_left(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise Broken({"error": "task start retry budget expired"})
    return left


@contextmanager
def transport_budget(transport, deadline):
    if deadline is None:
        yield
        return

    def expire():
        # Closing alone leaves makefile() reads alive; interrupt this socket object.
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(budget_left(deadline), expire)
    timer.name = "arete-http-deadline"
    timer.daemon = True
    try:
        timer.start()
        budget_left(deadline)
        try:
            yield
        except BROKE:
            budget_left(deadline)
            raise
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join()
    budget_left(deadline)


def close_response(resp):
    try:
        resp.close()
    finally:
        if transport := getattr(resp, "arete_transport", None):
            transport.close()


def read_response(resp, deadline):
    if deadline is None:
        return resp.read()
    chunks = []
    try:
        with transport_budget(resp.arete_transport, deadline):
            while not resp.isclosed():
                resp.arete_transport.settimeout(budget_left(deadline))
                chunk = resp.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            payload = b"".join(chunks)
            if resp.length:
                raise http.client.IncompleteRead(payload, resp.length)
        return payload
    finally:
        close_response(resp)


def call(method: str, path: str, body: dict | None = None, key: str | None = None) -> dict:
    if key is not None and method == "POST" and path == "/api/tasks":
        return keyed_call(body, key)
    resp = request(method, path, body)
    try:
        answer = json.loads(resp.read())
    except BROKE as e:
        raise Failed({"error": f"the server broke off: {e}"}) from None
    except ValueError:
        raise Failed({"error": f"the server answered {method} {path} with no JSON"}) from None
    return answer


def keyed_call(body, key):
    deadline = time.monotonic() + 30
    retries, restart, failure = 0, None, None
    uncertain = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise failure or Failed({"error": "task start retry budget expired"})
        if uncertain:
            if retries >= 2:
                raise failure
            retries += 1
        resp = None
        try:
            resp = request_once("POST", "/api/tasks", body, within=left, key=key, deadline=deadline)
            answer = json.loads(read_response(resp, deadline))
            budget_left(deadline)
            return answer
        except BROKE as e:
            failure = Broken({"error": f"the server broke off: {e}"})
            uncertain, pause = True, 0
        except (Broken, InFlight) as e:
            failure, pause = e, getattr(e, "seconds", 0)
            uncertain = True
        except Restarting as e:
            restart, failure, pause = e, Failed(e.answer, 503), e.seconds
            time.sleep(min(pause, max(0, deadline - time.monotonic())))
            continue
        except Down as e:
            if not uncertain and restart is None:
                raise
            failure, pause = Broken({"error": str(e)}), RETRY_SECONDS
        except ValueError:
            raise Failed({"error": "the server answered POST /api/tasks with no JSON"}) from None
        finally:
            if resp is not None:
                close_response(resp)
        time.sleep(min(pause, max(0, deadline - time.monotonic())))


def start_task(body, key=None):
    chosen = key if key is not None else os.environ.get("ARETE_IDEMPOTENCY_KEY")
    chosen = chosen if chosen is not None else secrets.token_hex(16)
    print(f"Idempotency-Key: {chosen}", file=sys.stderr, flush=True)
    return call("POST", "/api/tasks", body, key=chosen)


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


def run_command(run_id: str, action: str, body: dict | None = None) -> int:
    def selected():
        run = call("GET", f"/api/runs/{quote(run_id, safe='')}")["run"]
        emit(call("POST", run_path(run_id, action), {**(body or {}), "base_version": run["version"]}))
        return 0

    return steered(selected)


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
                seen, fresh, lost = max(seen, run_lines.seq_of(line)), True, None
                yield line
                if line.get("event") == run_lines.END:
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
    emit({**e.answer, **({"code": e.code} if e.code and "code" not in e.answer else {})})
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
        print(f"   the value of {q['name']} is not shown; it is stored for this repo. Enter declines", file=sys.stderr)
        value = (getpass.getpass("   ", stream=sys.stderr) if sys.stdin.isatty()
                 else sys.stdin.readline().rstrip("\r\n"))
        return {"secret": value} if value else {"declined": True}
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
        given = body.get("answers")
        if isinstance(given, list) and any(isinstance(a, dict) and "secret" in a for a in given):
            raise Failed({"error": "a secret value is never given as an argument; run `arete answer "
                                   f"{run_id}` and type or pipe it"})
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
        elif event == run_lines.END:
            report = line
    left = "unfinished" if report else "running"
    rows += [{"run": run_id, "event": "step", "step": step, "try": line.get("try"), "state": left}
             for step, line in opened.items()]
    return rows + ([report] if report else [])
