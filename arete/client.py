from __future__ import annotations

import fcntl
import getpass
import hashlib
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
from contextvars import ContextVar
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlsplit

from . import config, paths, run_lines, util

ENV = "ARETE_SERVER"
MINOR = 3
CONNECT_SECONDS = 5
CALL_SECONDS = 120
RETRY_SECONDS = 2
RESTART_WAIT_SECONDS = 30
RECONNECT_SECONDS = 120
HOOK_EVENTS_PATH = "/api/v1/hook-events"
WAIT_EVENTS = ("step_end", "question", "error", run_lines.END)
BROKE = (OSError, http.client.HTTPException)
LEGACY_ROUTES = {"task.list": "/api/tasks", "run.stop": "/api/runs/{run}/stop"}
API_1_2_ONLY = frozenset({"status.get", "task.create", "comment.create", "task.close",
                          "run.pin", "run.unpin", "run.steer"})
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
        self.after_restart = False


class Broken(Failed):
    pass


class InFlight(Failed):
    seconds = 1


class Restarting(Exception):
    def __init__(self, answer: dict, seconds: float):
        super().__init__(str(answer.get("error", "")))
        self.answer, self.seconds = answer, seconds


@dataclass(frozen=True)
class Server:
    scheme: str
    host: str
    port: int
    name: str
    instance: str = ""


class Invocation:
    def __init__(self, *, upload_on_write=False):
        self.retry_prefix = os.environ.get("ARETE_IDEMPOTENCY_KEY")
        self.prefix = self.retry_prefix if self.retry_prefix is not None else secrets.token_hex(16)
        self.writes = 0
        self.server = None
        self.upload_on_write = upload_on_write

    def key(self, *, exact=None, scope=""):
        self.writes += 1
        if exact is not None:
            return exact
        parts = [self.prefix]
        if self.writes > 1:
            parts.append(str(self.writes))
        if scope:
            parts.append(scope)
        key = ":".join(parts)
        return hashlib.sha256(key.encode()).hexdigest() if len(parts) > 1 and len(key) > 128 else key


_invocation = ContextVar("client_invocation", default=None)


@contextmanager
def invocation(*, upload_on_write=False):
    token = _invocation.set(Invocation(upload_on_write=upload_on_write))
    try:
        yield
    finally:
        _invocation.reset(token)


def write_key(*, exact=None, scope=""):
    return (_invocation.get() or Invocation()).key(exact=exact, scope=scope)


def resolved_write(key, inputs, resolve):
    current = _invocation.get()
    if current is None or not current.retry_prefix:
        return resolve()
    where = server()
    identity = json.dumps([address(where), where.instance, key]).encode()
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    folder = paths.CONFIG_DIR / "write-retries"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / hashlib.sha256(identity).hexdigest()
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            saved = util.read_json(path, None)
            if not isinstance(saved, dict) or "resolved" not in saved:
                raise Failed({"error": "the saved request could not be read"})
            if saved.get("fingerprint") != fingerprint:
                raise Failed({"error": "This key was used with different content", "reason": "idempotency_key_reused"}, 422)
            return saved["resolved"]
        result = resolve()
        util.write_json(path, {"fingerprint": fingerprint, "resolved": result})
        return result


def named_server(wanted: str, setting=ENV) -> Server:
    try:
        parts = urlsplit(wanted if setting != ENV or "://" in wanted else f"http://{wanted}")
        host, port = parts.hostname, parts.port
        if (parts.scheme not in ("http", "https") or not host or parts.username is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment or port == 0
                or setting != ENV and port is None):
            raise ValueError
    except ValueError:
        raise Failed({"error": f"{setting} must be http://HOST:PORT or https://HOST:PORT"}) from None
    return Server(parts.scheme, host, port or config.discovery_port(), host)


def configured_server() -> Server | None:
    if wanted := os.environ.get(ENV, "").strip():
        return named_server(wanted)
    try:
        data = config.read_data()
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise Failed({"error": f"cannot read server config at {paths.CONFIG_FILE}: {e}"}) from e
    try:
        wanted = config.server_url(data)
    except TypeError as e:
        raise Failed({"error": f"cannot read server config at {paths.CONFIG_FILE}: {e}"}) from e
    if wanted:
        return named_server(wanted, "[server] url")


def discover() -> Server:
    if configured := configured_server():
        return configured
    found, mine = util.read_json(paths.SERVER_FILE, None), paths.state_and_config()
    default = f"http://127.0.0.1:{config.discovery_port()}"
    addresses = found.get("addresses") if isinstance(found, dict) else None
    if not isinstance(addresses, list) or not addresses or not all(isinstance(a, str) and a for a in addresses):
        raise Down(default, f"no server address in {paths.SERVER_FILE}")
    try:
        port, pid = int(found["port"]), int(found.get("pid") or 0)
        if not 0 < port <= 65535 or pid < 0:
            raise ValueError
    except (KeyError, ValueError, TypeError, OverflowError):
        raise Down(default, f"invalid port or pid in {paths.SERVER_FILE}") from None
    host = next((a for a in addresses if ":" not in a), addresses[0])
    where = Server(found.get("scheme") or "http", host, port, found.get("host") or host)
    if not util.pid_alive(pid):
        raise Down(address(where), "the server process has ended")
    if {k: found.get(k) for k in mine} != mine:
        raise Down(address(where), "server.json names a different state or config")
    return where


def check_instance(where, instance):
    url = address(where)
    folder = paths.CONFIG_DIR / "server-instances"
    folder.mkdir(parents=True, exist_ok=True)
    identity = folder / hashlib.sha256(url.encode()).hexdigest()
    with identity.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = util.read_json(identity, None)
        if saved is None and not identity.exists():
            util.write_json(identity, instance)
        elif saved != instance:
            raise changed_instance(where, saved, instance)


def instance_of(resp):
    answer = decoded(resp, "GET", "/api/meta")
    instance = field(field(answer, "instance"), "id", "instance.id")
    if not isinstance(instance, str) or not instance:
        raise Failed({"error": "the server answer has an invalid instance.id"})
    return instance


def changed_instance(where, saved, instance):
    return Failed({"error": f"server instance at {address(where)} changed (expected {saved}, got {instance})",
                   "reason": "server_identity_changed"})


def check_replay(where, deadline):
    if where.instance:
        resp = request_once("GET", "/api/meta", read_seconds=CONNECT_SECONDS, deadline=deadline, where=where,
                            buffered=True)
        instance = instance_of(resp)
        if where.instance != instance:
            raise changed_instance(where, where.instance, instance)


def server(deadline=None, *, where=None) -> Server:
    current = _invocation.get()
    if current is not None and current.server is not None:
        return current.server
    where = where or discover()
    resp = request("GET", "/api/meta", read_seconds=CONNECT_SECONDS, where=where, buffered=True, deadline=deadline)
    instance = instance_of(resp)
    check_instance(where, instance)
    where = replace(resp.arete_server, instance=instance)
    if current is not None:
        current.server = where
    return where


def upload_hook_events(where=None, *, wait=False):
    from . import tool_events

    deadline = None

    def send(body, key):
        nonlocal deadline
        if deadline is None:
            deadline = time.monotonic() + CONNECT_SECONDS
        send_hook_batch(body, key, deadline)

    try:
        if not tool_events.event_files() and not (paths.STATE_DIR / "hook-uploads").is_dir():
            return
        where = where or configured_server()
        if where is None:
            return
        remote = server(time.monotonic() + CONNECT_SECONDS, where=where)
        if remote.instance == tool_events.local_instance():
            return
        try:
            tool_events.upload_pending(remote.instance, send, wait=wait)
        except Failed as e:
            if wait or e.after_restart or not unknown_route(e, "POST", HOOK_EVENTS_PATH):
                raise
    except (Down, Failed, OSError) as e:
        if wait:
            if isinstance(e, OSError):
                raise Failed({"error": f"hook event upload: {e}"}) from e
            raise
        print(f"hook event upload: {e}", file=sys.stderr)


def send_hook_batch(body, key, deadline):
    path = HOOK_EVENTS_PATH
    answer = decoded(request("POST", path, body, key=key, buffered=True, deadline=deadline), "POST", path)
    if field(answer, "received", kind=int) != len(body["events"]):
        raise Failed({"error": "the server did not acknowledge the full hook event batch"})


def upload_hooks_before_write():
    current = _invocation.get()
    if current is not None and current.upload_on_write:
        current.upload_on_write = False
        upload_hook_events()


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
            *, where: Server | None = None, buffered: bool = False,
            deadline: float | None = None) -> http.client.HTTPResponse:
    where = where or server(deadline)
    restart = None
    while True:
        within = None if deadline is None else deadline - time.monotonic()
        if within is not None and within <= 0:
            raise Failed(restart.answer, 503) if restart else Broken({"error": "request retry budget expired"})
        try:
            if restart is not None and method != "GET":
                check_replay(where, deadline)
                within = budget_left(deadline)
            return request_once(method, path, body, read_seconds, within, key=key, deadline=deadline, where=where,
                                buffered=buffered)
        except Restarting as e:
            if restart is None:
                limit = time.monotonic() + RESTART_WAIT_SECONDS
                deadline = limit if deadline is None else min(deadline, limit)
            restart, pause = e, e.seconds
        except Down:
            if restart is None:
                raise
            pause = RETRY_SECONDS
        except Broken:
            if restart is None:
                raise
            pause = RETRY_SECONDS
        except Failed as e:
            e.after_restart = restart is not None
            raise
        left = deadline - time.monotonic()
        if left <= 0:
            raise Failed(restart.answer, 503)
        time.sleep(min(pause, left))


def request_once(method: str, path: str, body: dict | None = None,
                 read_seconds: float | None = CALL_SECONDS, within: float | None = None,
                 key: str | None = None, *, deadline: float | None = None,
                 where: Server | None = None, buffered: bool = False) -> http.client.HTTPResponse:
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
                        headers["X-Arete-Instance"] = where.instance
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
                    where, followed = replace(where, scheme="https", name=moved.hostname), True
                    continue
                if resp.status < 400:
                    if buffered and deadline is not None:
                        resp.arete_payload = read_response(resp, deadline)
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
    wait = retry_after(resp.getheader("Retry-After"))
    if key is not None and resp.status == 409 and failure.get("code") == "idempotency_in_flight":
        failure = InFlight(failure, resp.status)
        failure.seconds = 1 if wait is None else wait
        raise failure
    if resp.status == 503 and wait is not None:
        raise Restarting(failure, wait)
    raise Failed(failure, resp.status)


def retry_after(header):
    value = (header or "").strip()
    if value.isascii() and value.isdigit():
        return min(float(value), RESTART_WAIT_SECONDS)
    try:
        when = parsedate_to_datetime(value)
        return max(0, when.timestamp() - time.time()) if when.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


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
         *, deadline: float | None = None) -> dict:
    if method != "GET":
        upload_hooks_before_write()
        if key is None:
            key = write_key()
    if key is not None and method == "POST" and path == "/api/tasks":
        return keyed_call(body, key, deadline=deadline)
    where = server(deadline)
    try:
        response = request(method, path, body, key=key, where=where, buffered=True, deadline=deadline)
    except Failed as e:
        if not path.startswith("/api/v1/") or not unknown_route(e, method, path):
            raise
        from . import contract

        if matched := contract.match(method, urlsplit(path).path):
            operation, params = matched
            if legacy := LEGACY_ROUTES.get(operation["name"]):
                if e.after_restart:
                    raise
                return legacy_answer(method, operation, legacy.format(**params), body, key, where, deadline)
            if operation["name"] in API_1_2_ONLY:
                raise requires_api_1_2(method, path) from None
        raise
    return decoded(response, method, path)


def requires_api_1_2(method: str, path: str) -> Failed:
    return Failed({"error": f"{method} {path} requires Arete serve API version 1.2 or newer",
                   "code": "unknown_route", "required_version": "1.2"}, 404)


def unknown_route(error: Failed, method: str, path: str) -> bool:
    if error.code != 404:
        return False
    answer = error.answer
    return (
        reason(answer) == "unknown_route"
        or answer == {"error": f"Nothing at {method} {urlsplit(path).path}."}
        or (
            answer.get("type") == "urn:arete:problem:not_found"
            and answer.get("code") == "not_found"
            and answer.get("detail") == answer.get("error") == "No such route."
        )
    )


def legacy_request(method, path, body, key, where, deadline):
    try:
        response = request_once(method, path, body, key=key, where=where, deadline=deadline, buffered=True)
    except Restarting as e:
        raise Failed(e.answer, 503) from None
    seq = response.getheader("Arete-Seq") if method != "GET" else None
    answer = decoded(response, method, path)
    if seq:
        answer.setdefault("seq", seq)
    return answer


def legacy_task_rows(summaries, where, deadline):
    rows = []
    for summary in summaries:
        identity = field(summary, "id", "tasks[].id", kind=str)
        path = f"/api/tasks/{quote(identity, safe='')}"
        row = legacy_request("GET", path, None, None, where, deadline)
        if row.get("id") != identity:
            raise Failed({"error": f"GET {path} returned a different task"})
        rows.append(row)
    return sorted(rows, key=lambda row: field(row, "created", "task.created", kind=str))


def legacy_answer(method, operation, legacy, body, key, where, deadline):
    from . import api, contract

    answer = legacy_request(method, legacy, body, key, where, deadline)
    schema = operation["result"]
    if operation["name"] == "task.list":
        answer["tasks"] = legacy_task_rows(field(answer, "tasks", kind=list), where, deadline)
    if method == "GET":
        answer.pop("seq", None)
        schema = {**schema, "required": [name for name in schema["required"] if name != "seq"]}
    answer = api.serialize(answer, schema)
    try:
        contract.validate(answer, schema, "server answer")
    except contract.Problem as e:
        raise Failed({"error": str(e)}) from None
    return answer


def decoded(resp, method, path):
    try:
        raw = resp.arete_payload if hasattr(resp, "arete_payload") else resp.read()
        return answer_object(raw, method, path)
    except BROKE as e:
        raise Failed({"error": f"the server broke off: {e}"}) from None
    finally:
        close_response(resp)


def answer_object(raw, method, path):
    try:
        value = json.loads(raw)
    except ValueError:
        raise Failed({"error": f"the server answered {method} {path} with no JSON"}) from None
    if not isinstance(value, dict):
        raise Failed({"error": f"the server answered {method} {path} with an invalid JSON object"})
    return value


def keyed_call(body, key, *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + RESTART_WAIT_SECONDS
    where = server(deadline)
    retries, restart, failure = 0, None, None
    uncertain = False
    replaying = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise restart or failure or Failed({"error": "task start retry budget expired"})
        if uncertain and restart is None:
            if retries >= 2:
                raise failure
            retries += 1
        resp = None
        try:
            if replaying:
                check_replay(where, deadline)
            replaying = True
            resp = request_once("POST", "/api/tasks", body, within=budget_left(deadline), key=key, deadline=deadline,
                                where=where)
            answer = answer_object(read_response(resp, deadline), "POST", "/api/tasks")
            budget_left(deadline)
            return answer
        except BROKE as e:
            failure = Broken({"error": f"the server broke off: {e}"})
            uncertain, pause = True, RETRY_SECONDS if restart is not None else 0
        except (Broken, InFlight) as e:
            if restart is not None and time.monotonic() >= deadline:
                raise restart
            failure, pause = e, getattr(e, "seconds", RETRY_SECONDS if restart is not None else 0)
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
        finally:
            if resp is not None:
                close_response(resp)
        time.sleep(min(pause, max(0, deadline - time.monotonic())))


def start_task(body, key=None):
    upload_hooks_before_write()
    deadline = time.monotonic() + RESTART_WAIT_SECONDS
    chosen = write_key(exact=key)
    print(f"Idempotency-Key: {chosen}", file=sys.stderr, flush=True)
    return keyed_call(body, chosen, deadline=deadline)


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


def run_command(run_id: str, action: str, body: dict | None = None, *, key_scope: str = "") -> int:
    def selected():
        key = write_key(scope=key_scope)
        version = resolved_write(key, [run_id, action, body], lambda: field(run_info(run_id), "version", "run.version"))
        data = {**(body or {}), "base_version": version}
        if action == "stop":
            answer = call("POST", run_path(run_id, "stop", prefix="/api/v1"), data, key)
            emit({k: v for k, v in answer.items() if k != "seq"})
        else:
            emit(call("POST", run_path(run_id, action), data, key))
        return 0

    return steered(selected)


def run_info(run_id):
    return field(call("GET", f"/api/runs/{quote(run_id, safe='')}"), "run")


def require_steering(run_id: str) -> None:
    path = run_path(run_id, "steer", prefix="/api/v1")
    try:
        close_response(request("GET", path))
    except Failed as error:
        if error.code == 405 and reason(error.answer) == "method_not_allowed":
            return
        if unknown_route(error, "GET", path):
            raise requires_api_1_2("POST", path) from None
        raise
    raise Failed({"error": f"GET {path} did not report that steering requires POST"})


def steer(run_id, text):
    answer = call("POST", run_path(run_id, "steer", prefix="/api/v1"), {"text": text})
    return {k: v for k, v in answer.items() if k != "seq"}


def events(run_id: str, start: int = 0, follow: bool = False) -> Iterator[dict]:
    return stream(run_path(run_id, "events"), {"from": start, **({"follow": "1"} if follow else {})})


def run_status(run_id: str) -> str:
    try:
        return field(run_info(run_id), "status", "run.status")
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
    answer = e.answer
    if e.code == 400 and reason(answer) == "client_too_old" and (needed := answer.get("required_version")):
        answer = {**answer, "error": f"server requires Arete CLI version {needed}"}
    emit({**answer, **({"code": e.code} if e.code and "code" not in answer else {})})
    return 1


def reason(answer):
    return answer.get("reason") or answer.get("code")


def field(answer, name, label=None, *, kind=None):
    try:
        value = answer[name]
        if kind is None or type(value) is kind:
            return value
    except (KeyError, TypeError):
        pass
    raise Failed({"error": f"server answer is missing or has an invalid field {label or name}"})


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


def open_ask(run_id: str, ask_id: str) -> dict:
    asks = field(call("GET", "/api/asks"), "asks")
    if not isinstance(asks, list):
        raise Failed({"error": "server answer has an invalid field asks"})
    open_asks = [a for a in asks
                 if a["run"] == run_id and (not ask_id or a["id"] == ask_id)]
    if not open_asks:
        raise Failed({"error": f"run {run_id} has no open ask"})
    return open_asks[0]


def answered_ask(run_id: str, ask_id: str, raw: str) -> dict:
    key = write_key()
    body = None
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

    def target():
        found = open_ask(run_id, ask_id)
        return found["id"], found["questions"] if body is None else []

    identity, questions = resolved_write(key, ["answer ask", run_id, ask_id, body], target)
    if body is None:
        body = {"answers": [prompted(i, q) for i, q in enumerate(questions, 1)]}
    return call("POST", f"/api/asks/{quote(identity, safe='')}/answer", body, key)


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
