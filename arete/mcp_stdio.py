from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
import time
from contextlib import ExitStack

from . import api_http, client, credential_files, mcp_protocol, paths
from .contract import VERSION
from .refusals import Refusal

RETRIES = 3
BUDGET = 10
ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PROBLEM_CODE = re.compile(r"[a-z_]+")


def ulid():
    value = (time.time_ns() // 1_000_000 << 80) | secrets.randbits(80)
    return "".join(ENCODING[(value >> shift) & 31] for shift in range(125, -1, -5))


def token_parts(text, where):
    found = credential_files.CLIENT_TEXT.fullmatch(text or "")
    if found is None or found[1] != where.tag:
        raise client.Failed({"code": "unauthenticated", "error": "Provide a bearer token for this Arete instance."})
    return found


def failed(identity, key, answer, status=0):
    code = answer.get("code") or client.reason(answer)
    if not isinstance(code, str) or not PROBLEM_CODE.fullmatch(code):
        code = "transport_error"
    if status and answer.get("status") == status and answer.get("type") == f"urn:arete:problem:{code}":
        data = answer
    else:
        data = {"code": code, "error": mcp_protocol.untrusted(answer.get("error", "The MCP request failed."))}
        if status:
            data["status"] = status
    if key is not None:
        return mcp_protocol.reply(identity, mcp_protocol.tool_result(data, failed=True))
    return mcp_protocol.error(identity, -32000, "Arete MCP request failed.", data)


def outcome_unknown(identity, key, error=None):
    data = {"code": "outcome_unknown", "idempotency_key": key}
    if error is not None:
        data["error"] = mcp_protocol.untrusted(error)
    return mcp_protocol.reply(identity, mcp_protocol.tool_result(data, failed=True))


class Stdio:
    def __init__(self):
        self.where = None
        self.token = os.environ.get("ARETE_TOKEN", "").strip() or None
        self.retired_ids = set()
        self.headers = {"X-Arete-Client": f"mcp/{VERSION}", "Accept": "application/json"}

    def replacement(self):
        try:
            token = client.stored_token(self.where)
        except client.Failed:
            token = None
        if token and token_parts(token, self.where)[2] not in self.retired_ids:
            return token
        raise client.Failed({"code": "token_rotated", "error": "The bearer token was rotated. "
                             "Store a replacement token for this Arete instance."})

    def exchange(self, message, key, deadline, previous):
        if self.where is None:
            self.where = client.server(deadline)
        else:
            if self.token is None and self.retired_ids:
                self.token = self.replacement()
            client.check_replay(self.where, deadline)
        where = self.where
        if where.scheme != "https":
            raise client.Failed({"code": "tls_required", "error": "Arete MCP requires HTTPS."}, 426)
        self.token = self.token or client.stored_token(where)
        token_parts(self.token, where)
        headers = self.headers if previous is None else {**self.headers, mcp_protocol.ROTATION_HEADER: previous}
        response = client.request_once("POST", mcp_protocol.PATH, message, key=key, where=where, deadline=deadline,
                                       token=self.token, buffered=True, extra_headers=headers)
        try:
            client.confirm_instance(where, response.getheader("Arete-Instance", ""))
            try:
                answer = client.answer_object(response.arete_payload, "POST", mcp_protocol.PATH)
            except client.Failed as failure:
                raise client.Broken(failure.answer) from None
            if answer.get("jsonrpc") != "2.0" or answer.get("id") != message["id"]:
                raise client.Broken({"error": "The server did not return the expected JSON-RPC response."})
            if message["method"] == "initialize" and "result" in answer:
                session = response.getheader(mcp_protocol.SESSION_HEADER, "")
                result = answer["result"]
                version = result.get("protocolVersion") if isinstance(result, dict) else None
                if not mcp_protocol.SESSION_ID.fullmatch(session) or version not in mcp_protocol.VERSIONS:
                    raise client.Broken({"error": "The server did not negotiate an MCP session."})
                self.headers.update({mcp_protocol.SESSION_HEADER: session, mcp_protocol.VERSION_HEADER: version})
            return answer
        finally:
            client.close_response(response)

    def handle(self, message):
        request = mcp_protocol.request(message)
        if request is None:
            return mcp_protocol.error(None, -32600, "Invalid JSON-RPC request.")
        method, identity, _ = request
        if identity is None:
            return None
        key = ulid() if method == "tools/call" else None
        if key and mcp_protocol.SESSION_HEADER not in self.headers:
            return mcp_protocol.error(identity, -32600, "Initialize the MCP session before calling tools.")
        if key:
            message = {**message, "id": key}
        deadline = time.monotonic() + BUDGET
        previous, terminal, uncertain = None, None, False
        for attempt in range(RETRIES + 1):
            now = time.monotonic()
            if now >= deadline:
                break
            try:
                answer = self.exchange(message, key, min(deadline, now + BUDGET / (RETRIES + 1)), previous)
                return {**answer, "id": identity}
            except (client.Down, client.Restarting, client.Failed) as failure:
                status = getattr(failure, "code", 0)
                if status == 401 and client.reason(failure.answer) == "token_rotated" and self.token:
                    retired = token_parts(self.token, self.where)[2]
                    self.retired_ids.add(retired)
                    self.token = None
                    if previous is None:
                        try:
                            self.token = self.replacement()
                        except client.Failed as missing:
                            if uncertain:
                                return outcome_unknown(identity, key, missing.answer["error"])
                            return failed(identity, key, missing.answer)
                        previous = retired
                        continue
                if status == 401 and previous is not None:
                    return failed(identity, key, failure.answer, status)
                retryable = status >= 500 or isinstance(
                    failure, (client.Down, client.Broken, client.InFlight, client.Restarting))
                rate_limited = status == 429 and failure.retry_after is not None
                if not uncertain:
                    terminal = failure, status
                if not key or not (retryable or rate_limited):
                    break
                if retryable:
                    uncertain, terminal = True, None
                pause = failure.retry_after if rate_limited else getattr(failure, "seconds", 0)
                if attempt == RETRIES or pause >= deadline - time.monotonic():
                    break
                if pause:
                    time.sleep(pause)
        if terminal:
            failure, status = terminal
            return failed(identity, key, failure.answer, status)
        if key:
            return outcome_unknown(identity, key)
        return mcp_protocol.error(identity, -32000, "Arete MCP request timed out.")


def serve(stdin, stdout):
    bridge = Stdio()
    while raw := stdin.readline(api_http.MAX_JSON_BYTES + 1):
        try:
            if len(raw) > api_http.MAX_JSON_BYTES:
                while raw and not raw.endswith(b"\n"):
                    raw = stdin.readline(api_http.MAX_JSON_BYTES + 1)
                raise Refusal("MCP message exceeds the size limit.")
            if not raw.strip():
                continue
            message = api_http.json_object(raw)
        except Refusal:
            answer = mcp_protocol.error(None, -32700, "Invalid JSON-RPC message.")
        else:
            answer = bridge.handle(message)
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=True) + "\n")
            stdout.flush()
    return 0


def register(sub):
    sub.add_parser("mcp", help="serve Arete tools over MCP stdio").set_defaults(fn=start)


def start(_):
    with ExitStack() as stack:
        try:
            paths.STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(paths.STATE_DIR / "mcp-stdio.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            lock = stack.enter_context(os.fdopen(fd, "rb"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("An arete mcp instance is already running for this state directory.", file=sys.stderr)
            return 1
        except OSError as error:
            print(f"Could not acquire the arete mcp lock: {error}", file=sys.stderr)
            return 1
        return serve(sys.stdin.buffer, sys.stdout)
