from __future__ import annotations

import hashlib
import re
import secrets
import time

from . import api, api_http, contract, credential_http, mcp_protocol
from .store import db, tokens

REMOTE = frozenset("""
    screen.get inbox.list inbox.get inbox.resolve task.list task.get task.create task.run
    comment.create run.list run.get run.stop run.steer turn.list event.page intake.add outbox.list outbox.get
""".split())
FLOOR_PREFIXES = ("secret.", "token.", "principal.", "pairing.", "push.", "export.")
FLOOR_OPERATIONS = {"session.end", "webhook.rotate", "serve.install"}
TARGET_DEPENDENT_FLOORS = {"inbox.resolve", "outbox.approve", "config.activate"}
CALL_RATES = api_http.RateLimits(command_limit=(2, 10))
SIMPLE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
HOOK_RESULT = contract.envelope({"events": contract.array(contract.obj(dict.fromkeys(
    contract.HOOK_EVENT["properties"], {})))})
RESEARCH_ROW = contract.obj({"id": {}, "detail": contract.obj({**contract.RESEARCH["properties"], "ref": {}})})
SCREEN_LOOP = contract.obj(dict.fromkeys((
    *contract.LOOP["properties"], "origin", "description", "repo", "trigger", "sources", "runner", "device", "queue",
    "budget_usd", "daily_budget_usd", "emits", "dir", "error", "next_due", "last_fire", "spent_24h", "last",
    "findings", "waiting", "cursors", "webhook_path",
), {}))


def permitted(credential, op):
    name = op["name"]
    return (not name.startswith(FLOOR_PREFIXES) and name not in FLOOR_OPERATIONS
            and name not in TARGET_DEPENDENT_FLOORS
            and (credential.profile == "full" or credential.profile in (None, "remote") and name in REMOTE)
            and (credential.commands is None or name in credential.commands)
            and credential.allows(op["scope"]))


def tool_list(credential):
    allowed = {name: op for name, op in contract.BY_NAME.items() if permitted(credential, op)}
    if batch := allowed.pop("batch.run", None):
        reads = [name for name, op in allowed.items() if op["method"] == "GET"]
        if reads:
            item = contract.obj({"operation": {"enum": reads}, "input": contract.mapping(contract.JSON)}, ["operation"])
            allowed["batch.run"] = {**batch, "input": contract.obj({"requests": {
                **contract.array(item), "minItems": 1, "maxItems": 50}})}
    return {"tools": [{"name": name.replace(".", "__"), "description": name, "inputSchema": op["input"],
                       "_meta": {"arete/scope": op["scope"]}} for name, op in allowed.items()]}


def header(handler, name, pattern):
    values = handler.headers.get_all(name, [])
    if len(values) > 1 or values and not pattern.fullmatch(values[0]):
        raise contract.Problem("invalid_input", f"Send a valid {name}.")
    return values[0] if values else None


def session_id(handler):
    if not (session := header(handler, mcp_protocol.SESSION_HEADER, mcp_protocol.SESSION_ID)):
        raise contract.Problem("invalid_input", "Send the Mcp-Session-Id from initialize.")
    return session


def call_key(session, identity):
    component = str(identity)
    if isinstance(identity, str) and (not SIMPLE_ID.fullmatch(identity) or re.fullmatch(r"-?[0-9]+", identity)):
        component = "s/" + hashlib.sha256(identity.encode()).hexdigest()
    return f"mcp:{session}:{component}"


def protect_result(operation, arguments, data):
    schema = contract.BY_NAME[operation]["result"]
    if operation == "batch.run":
        envelope = mcp_protocol.untrusted({key: value for key, value in data.items() if key != "results"}, schema)
        results = [protect_result(read["operation"], read.get("input", {}), result)
                   for read, result in zip(arguments["requests"], data["results"], strict=True)]
        return {**envelope, "results": results}
    if operation == "hook_event.list":
        schema = HOOK_RESULT
    elif operation == "research.list":
        schema = contract.envelope({"research": contract.array(RESEARCH_ROW)})
    elif operation == "research.get":
        schema = contract.envelope({"research": RESEARCH_ROW})
    elif operation == "screen.get" and arguments.get("view") == "loop":
        schema = contract.obj({**schema["properties"], "view": SCREEN_LOOP})
    return mcp_protocol.untrusted(data, schema)


def call(handler, params, identity, actor):
    CALL_RATES.take(actor, command=True)
    name, arguments = params.get("name"), params.get("arguments", {})
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return mcp_protocol.error(identity, -32602, "A tool call needs a name and an arguments object.")
    operation = name.replace("__", ".")
    op = contract.BY_NAME.get(operation)
    if name != operation.replace(".", "__") or op is None or not permitted(handler.credential, op):
        raise credential_http.problem("scope_missing")
    key = call_key(session_id(handler), identity)
    api.prepare(operation, arguments)
    if operation == "batch.run":
        reads = arguments["requests"]
        if any(not permitted(handler.credential, contract.BY_NAME[read["operation"]]) for read in reads):
            raise credential_http.problem("scope_missing")
        api_http.RATES.take(handler.person, command=False, cost=len(reads) - 1)
    claim = actor, key, mcp_protocol.PATH, {"operation": operation, "arguments": arguments}
    data = api.command(handler, op, arguments, {}, claim=claim)
    result = protect_result(operation, arguments, data)
    return mcp_protocol.reply(identity, mcp_protocol.tool_result(result))


def rpc(handler, method, identity, params):
    previous = header(handler, mcp_protocol.ROTATION_HEADER, tokens.TOKEN_ID)
    if previous or method == "tools/call":
        root, lineage = tokens.rotation_lineage(handler.credential)
        if previous and previous not in lineage:
            raise contract.Problem("conflict", "The replacement token is outside the original token’s rotation chain.")
        if previous:
            handler.mcp_rotation_accepted = previous
    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in mcp_protocol.VERSIONS else mcp_protocol.VERSIONS[0]
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "arete", "version": contract.VERSION}}
        handler.mcp_session = secrets.token_urlsafe(24)
        return mcp_protocol.reply(identity, result)
    if method == "tools/list":
        return mcp_protocol.reply(identity, tool_list(handler.credential))
    if method == "tools/call":
        return call(handler, params, identity, f"client:{root}")
    if method == "ping":
        return mcp_protocol.reply(identity, {})
    return mcp_protocol.error(identity, -32601, "Method not found.")


def stream(handler):
    session_id(handler)
    handler.head(200, "text/event-stream", "no-store")
    handler.send_header("Connection", "close")
    handler.send_header("X-Accel-Buffering", "no")
    handler.connection.settimeout(.75)
    try:
        with handler.event_connection() as dropped:
            handler.stream_headers()
            until = time.monotonic() + 25
            while time.monotonic() < until and not handler.stream_problem() and not (dropped and dropped()):
                handler.stream_write(b": keepalive\n\n")
                time.sleep(.5)
    except OSError:
        return


def serve(handler, method):
    handler.instance_header = db.schema_metadata()["instance_id"]
    if method == "GET":
        return stream(handler)
    request = mcp_protocol.request(handler.body())
    if request is None or not isinstance(request[2], dict):
        raise contract.Problem("invalid_input", "Invalid JSON-RPC request.")
    if request[1] is None:
        return handler.send(202, b"", "application/json", "no-store")
    handler.send(200, api_http.json_bytes(rpc(handler, *request)), "application/json", "no-store")
