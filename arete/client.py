from __future__ import annotations

import fcntl
import hashlib
import getpass
import http.client
import ipaddress
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
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from . import config, paths, run_lines, util

ENV = "ARETE_SERVER"
MINOR = 2
CONNECT_SECONDS = 5
CALL_SECONDS = 120
RETRY_SECONDS = 2
RESTART_WAIT_SECONDS = 30
RECONNECT_SECONDS = 120
WAIT_EVENTS = ("step_end", "question", "error", run_lines.END)
BROKE = (OSError, http.client.HTTPException)
_ACQUISITION_LOCK = threading.Lock()
_acquisition_thread: threading.Thread | None = None


class Down(Exception):
    def __init__(self, url, cause):
        super().__init__(f"server unreachable at {url} ({cause})")
        self.answer = {"error": str(self), "reason": "server_down"}


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


class Invocation:
    def __init__(self):
        self.prefix = os.environ.get("ARETE_IDEMPOTENCY_KEY") or secrets.token_hex(16)
        self.writes = 0
        self.server = None

    def key(self):
        self.writes += 1
        return f"{self.prefix}:{self.writes}"


_invocation = ContextVar("client_invocation", default=None)


@contextmanager
def invocation():
    token = _invocation.set(Invocation())
    try:
        yield
    finally:
        _invocation.reset(token)


def write_key():
    return (_invocation.get() or Invocation()).key()


def named_server(wanted: str, setting=ENV) -> Server:
    try:
        parts = urlsplit(wanted if setting != ENV or "://" in wanted else f"http://{wanted}")
        host, port = parts.hostname, parts.port
        if (parts.scheme not in ("http", "https") or not host or parts.username is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment or port == 0):
            raise ValueError
    except ValueError:
        raise Failed({"error": f"{setting} must be http://HOST:PORT or https://HOST:PORT"}) from None
    return Server(parts.scheme, host, port or config.load_or_problem()[0].dashboard_port, host)


def discover() -> Server:
    if wanted := os.environ.get(ENV, "").strip():
        return named_server(wanted)
    cfg = config.load()
    if cfg.server_url:
        return named_server(cfg.server_url, "[server] url")
    found, mine = util.read_json(paths.SERVER_FILE, None), paths.state_and_config()
    if not isinstance(found, dict) or not (found.get("addresses") and found.get("port")):
        raise Down(f"http://127.0.0.1:{cfg.dashboard_port}", f"no server address in {paths.SERVER_FILE}")
    addresses = [str(a) for a in found["addresses"]]
    host, port = next((a for a in addresses if ":" not in a), addresses[0]), int(found["port"])
    where = Server(found.get("scheme") or "http", host, port, found.get("host") or host)
    if not util.pid_alive(found.get("pid")):
        raise Down(address(where), "the server process has ended")
    if {k: found.get(k) for k in mine} != mine:
        raise Down(address(where), "server.json names a different state or config")
    return where


def check_instance(where, instance):
    url = address(where)
    if not isinstance(instance, str) or not instance:
        raise Failed({"error": "the server answer has an invalid instance.id"})
    folder = paths.CONFIG_DIR / "server-instances"
    folder.mkdir(parents=True, exist_ok=True)
    identity = folder / hashlib.sha256(url.encode()).hexdigest()
    with identity.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = util.read_json(identity, None)
        if saved is None and not identity.exists():
            util.write_json(identity, instance)
        elif saved != instance:
            raise Failed({"error": f"server instance at {url} changed (expected {saved}, got {instance})",
                          "reason": "server_identity_changed"})


def server() -> Server:
    current = _invocation.get()
    if current is not None and current.server is not None:
        return current.server
    where = discover()
    resp = request("GET", "/api/meta", read_seconds=CONNECT_SECONDS, where=where)
    answer = decoded(resp, "GET", "/api/meta")
    check_instance(where, field(field(answer, "instance"), "id", "instance.id"))
    where = getattr(resp, "arete_server", where)
    if current is not None:
        current.server = where
        if remote(where):
            from . import tool_events

            tool_events.send_pending(address(where), lambda body, key: call("POST", "/api/v1/hook-events", body, key))
    return where


def remote(where):
    try:
        if ipaddress.ip_address(where.host).is_loopback:
            return False
    except ValueError:
        pass
    return where.host not in {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}


def headers_for(where: Server) -> dict[str, str]:
    headers = {"X-Arete": "1", "X-Arete-Client": f"cli/1.{MINOR}"}
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
        raise Down(address(where), getattr(e, "verify_message", None) or e.reason or e) from None
    except OSError as e:
        conn.close()
        raise Down(address(where), e.strerror or e) from None
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
            found = where
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
                raise Down(address(where), "a previous server connection is still being acquired")
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
            read_seconds: float | None = CALL_SECONDS, key: str | None = None,
            *, where: Server | None = None) -> http.client.HTTPResponse:
    where = where or server()
    deadline = None
    restart = None
    while True:
        within = None if deadline is None else deadline - time.monotonic()
        if within is not None and within <= 0:
            raise Failed(restart.answer, 503)
        try:
            return request_once(method, path, body, read_seconds, within,
                                key=key, deadline=deadline, where=where)
        except Restarting as e:
            restart, pause = e, e.seconds
        except Down:
            if restart is None:
                raise
            pause = RETRY_SECONDS
        except Broken:
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
                 key: str | None = None, *, deadline: float | None = None,
                 where: Server | None = None) -> http.client.HTTPResponse:
    if deadline is None and within is not None:
        deadline = time.monotonic() + within
    where = where or server()
    payload = json.dumps(body or {}).encode() if method != "GET" else None
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
                    resp.arete_server = where
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
    failure = {"error": f"the server at {address(where)} answered {resp.status}"}
    if isinstance(answer, dict):
        failure.update(answer)
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


def call(method: str, path: str, body: dict | None = None, key: str | None = None,
         *, legacy: str = "", legacy_body: dict | None = None) -> dict:
    if method != "GET" and key is None:
        key = write_key()
    if key is not None and method == "POST" and path == "/api/tasks":
        return keyed_call(body, key)
    try:
        return decoded(request(method, path, body, key=key), method, path)
    except Failed as e:
        if not legacy or e.code != 404 or not unknown_route(e.answer):
            raise
    return decoded(request(method, legacy, body if legacy_body is None else legacy_body, key=key), method, legacy)


def decoded(resp, method, path):
    try:
        return json.loads(resp.read())
    except BROKE as e:
        raise Failed({"error": f"the server broke off: {e}"}) from None
    except ValueError:
        raise Failed({"error": f"the server answered {method} {path} with no JSON"}) from None
    finally:
        close_response(resp)


def keyed_call(body, key):
    where = server()
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
            resp = request_once("POST", "/api/tasks", body, within=left, key=key, deadline=deadline, where=where)
            answer = json.loads(read_response(resp, deadline))
            budget_left(deadline)
            return answer
        except BROKE as e:
            failure = Broken({"error": f"the server broke off: {e}"})
            uncertain, pause = True, 0
        except (Broken, InFlight) as e:
            if restart is not None and time.monotonic() >= deadline:
                raise restart
            failure, pause = e, getattr(e, "seconds", 0)
            uncertain = True
        except Restarting as e:
            restart = failure = Failed(e.answer, 503)
            pause = e.seconds
            time.sleep(min(pause, max(0, deadline - time.monotonic())))
            continue
        except Down as e:
            if not uncertain and restart is None:
                raise
            failure, pause = restart or Broken(e.answer), RETRY_SECONDS
        except ValueError:
            raise Failed({"error": "the server answered POST /api/tasks with no JSON"}) from None
        finally:
            if resp is not None:
                close_response(resp)
        time.sleep(min(pause, max(0, deadline - time.monotonic())))


def start_task(body, key=None):
    chosen = key if key is not None else write_key()
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
        raise Broken({"error": f"the server broke off the lines: {e}"}) from None
    finally:
        resp.close()


def run_path(run_id: str, action: str, *, prefix="/api") -> str:
    return f"{prefix}/runs/{quote(run_id, safe='')}/{action}"


def run_command(run_id: str, action: str, body: dict | None = None, *, legacy: str = "") -> int:
    def selected():
        run = run_info(run_id)
        data = {**(body or {}), "base_version": field(run, "version", "run.version")}
        path = run_path(run_id, action)
        if action == "stop":
            answer = call("POST", run_path(run_id, "stop", prefix="/api/v1"), data,
                          legacy=legacy or path, legacy_body={} if legacy else None)
            emit({k: v for k, v in answer.items() if k != "seq"})
        else:
            emit(call("POST", path, data))
        return 0

    return steered(selected)


def run_info(run_id):
    return field(call("GET", f"/api/runs/{quote(run_id, safe='')}"), "run")


def steer(run_id, text):
    answer = call("POST", run_path(run_id, "steer", prefix="/api/v1"), {"text": text},
                  legacy=f"/api/sessions/{quote(run_id, safe='')}/send")
    return {k: v for k, v in answer.items() if k != "seq"}


def events(run_id: str, start: int = 0, follow: bool = False) -> Iterator[dict]:
    return stream(run_path(run_id, "events"), {"from": start, **({"follow": "1"} if follow else {})})


def run_status(run_id: str) -> str:
    try:
        return str((call("GET", f"/api/runs/{quote(run_id, safe='')}").get("run") or {}).get("status") or "")
    except (Down, Broken):
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
        except Broken:
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
        except (Down, Broken):
            read = None


def emit(line: dict) -> None:
    print(json.dumps(line, ensure_ascii=False, default=str), flush=True)


def failed(e: Failed) -> int:
    answer = e.answer
    if e.code == 400 and reason(answer) == "client_too_old" and (needed := answer.get("required_version")):
        answer = {**answer, "error": f"server requires Arete CLI version {needed}"}
    emit({**answer, **({"code": e.code} if e.code and "code" not in answer else {})})
    return 1


def reason(answer):
    return answer.get("reason") or answer.get("code")


def unknown_route(answer):
    code = reason(answer)
    return code == "unknown_route" or (code == "not_found" and answer.get("detail") == "No such route.")


def field(answer, name, label=None):
    try:
        return answer[name]
    except (KeyError, TypeError) as e:
        raise Failed({"error": f"server answer is missing or has an invalid field {label or name}"}) from e


def answer_error(e):
    detail = e.args[0] if e.args else "unknown"
    return failed(Failed({"error": f"server answer is missing or has an invalid field ({detail})"}))


def steered(act: Callable[[], int]) -> int:
    try:
        return act()
    except Down as e:
        emit(e.answer)
    except Failed as e:
        return failed(e)
    except (KeyError, TypeError) as e:
        return answer_error(e)
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
    asks = field(call("GET", "/api/asks"), "asks")
    if not isinstance(asks, list):
        raise Failed({"error": "server answer has an invalid field asks"})
    open_asks = [a for a in asks
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
