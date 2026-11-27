from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from functools import cache

from . import configkinds, refusals

MINOR = 10
VERSION = f"1.{MINOR}"
OLDEST_CLIENT = dict.fromkeys(("cli", "mcp", "web"), "1.0")
STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
BOOLEAN = {"type": "boolean"}
JSON = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def obj(properties, required=None, additional=False):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": additional}


def array(items):
    return {"type": "array", "items": items}


def mapping(values):
    return obj({}, [], values)


TASK = obj({**dict.fromkeys(("id", "repo", "title", "body", "kind", "state", "parent", "created", "updated"), STRING),
            "version": INTEGER})
RUN_TEXT = ("id", "kind", "repo", "issue", "loop", "loop_kind", "started", "finished", "status", "summary", "pr_url",
            "branch", "only_step", "files_purged", "mined_until", "stop_asked", "ab_held", "measured_at", "landed",
            "device", "model", "runner", "log", "trigger", "queue", "grp", "lock", "moved", "engine_sha",
            "label_pending", "wait_minutes")
RUN_NUMBERS = ("pid", "cost_usd", "tokens_in", "tokens_out", "active_seconds", "verify_seconds", "verify_open_seconds",
               "repair_calls", "schema_version", "attempts", "version")
RUN = obj({**{k: {"type": ["string", "null"]} for k in RUN_TEXT},
           **{k: {"type": ["number", "null"]} for k in RUN_NUMBERS}}, ["id", "status", "version"])
RUN["properties"]["issue"] = {"type": ["integer", "string", "null"]}
RUN["properties"]["version"] = INTEGER
FACTORY_STATE = obj({"enabled": BOOLEAN, "phase": STRING, "version": INTEGER, "actor": STRING, "changed": STRING,
                     "unconfirmed": INTEGER, "paused": INTEGER, "targets": array(obj(dict.fromkeys(
                         ("id", "phase", "reason", "successor"), STRING)))})
NULL_TEXT = {"type": ["string", "null"]}
LOOP = obj({"name": STRING, "enabled": BOOLEAN, "kind": STRING, "running": STRING, "pending": INTEGER,
            "dead": {"type": ["integer", "array"], "items": mapping(JSON)},
            "prompt": STRING, "model": STRING, "state": mapping(JSON), "runs": array(RUN),
            "webhook_path": STRING, "secret_present": BOOLEAN},
           ["name", "enabled"])
INCIDENT = obj({**dict.fromkeys(("state", "phase", "waiting", "class", "source", "repo", "signature", "first_seen",
                               "last_seen", "decision", "reason", "decided", "retry_at", "recovery", "closed",
                               "decision_line", "repair_line"), STRING),
                "id": INTEGER, "count": INTEGER, "confidence": NUMBER, "injection": INTEGER, "alerted": INTEGER,
                "evidence": STRING, "answers": mapping(JSON), "notes_total": INTEGER,
                "notes": array(obj({"ts": STRING, "note": STRING}))},
               ["id", "state"])
TRIAGE = obj({**dict.fromkeys(("ref", "source", "tracker", "repo", "route", "title", "url", "updated", "size", "why",
                             "ts", "kind", "parent"), NULL_TEXT), "ready": JSON, "code": JSON, "closed": JSON,
              "in_sprint": JSON, "cost": NUMBER,
              **{k: array(STRING) for k in ("questions", "plan", "repos", "assumptions", "checks")}}, ["ref", "title"])
RESEARCH = obj({**dict.fromkeys(("ref", "source", "title", "url", "updated", "status", "summary", "report", "error",
                               "run_id", "ts", "ticket", "basis", "answered_by", "filed_path", "filed_url", "moved",
                               "delivered"), NULL_TEXT), "cost": NUMBER}, ["ref", "status"])
CHAT = obj({"id": INTEGER, **dict.fromkeys(("title", "project", "created", "updated", "summary", "folder",
                                          "runner", "draft_title", "draft_body"), STRING),
            "busy": {"type": ["boolean", "integer"]}, "paid_usd": {"type": ["number", "null"]},
            "spent": {"type": ["number", "null"]}, "messages": array(obj({"id": INTEGER, "sender": STRING, "text": NULL_TEXT,
              "cost": NUMBER, "paid_usd": {"type": ["number", "null"]}, "ts": STRING}))}, ["id", "title"])
HOOK_EVENT = obj({**dict.fromkeys(("ts", "hook", "kind", "cls", "detail", "error"), STRING),
                  **dict.fromkeys(("tool", "cwd", "session"), JSON), "agent": BOOLEAN}, ["ts", "kind", "cls"])
RESOURCE = {
    "task": TASK, "run": RUN,
    "project": obj(dict.fromkeys(("id", "name", "github", "path"), STRING)),
    "line": obj({"id": STRING, "kind": STRING, "repo": STRING, "enabled": BOOLEAN}),
    "view": obj({"id": STRING, "label": STRING, "data": mapping(JSON)}, ["id", "label"]),
    "step": obj({"id": INTEGER, "run_id": STRING, "name": STRING, "model": STRING, "runner": STRING,
                 "cost_usd": NUMBER, "seconds": NUMBER}),
    "loop": obj({"id": STRING, "enabled": BOOLEAN, "detail": LOOP}),
    "incident": obj({"id": STRING, "detail": INCIDENT}),
    "triage": obj({"id": STRING, "detail": TRIAGE}),
    "research": obj({"id": STRING, "detail": RESEARCH}),
    "chat": obj({"id": STRING, "detail": CHAT}),
}
ERRORS = [{"code": code, "status": status, "description": description} for code, status, description in (
    ("not_found", 404, "The selected resource does not exist."),
    ("unavailable", 404, "No saved diff is available."),
    ("unknown_field", 422, "Undeclared request fields; fields lists rejected paths."),
    ("invalid_input", 400, "The input does not match the operation schema."),
    ("client_too_old", 400, "Upgrade the client to required_version."),
    ("version_conflict", 409, "Refresh current before trying again."),
    ("invalid_framing", 400, "Use a single decimal Content-Length for a body and omit Transfer-Encoding."),
    ("invalid_json", 400, "Send a UTF-8 JSON object with unique names, at most 32 levels, finite numbers, and "
                          "integers between -9007199254740991 and 9007199254740991."),
    ("forbidden", 403, "The request was not authorized."),
    ("unauthenticated", 401, "A valid credential is required."),
    ("ambiguous_credential", 400, "Send either a bearer token or a session cookie."),
    ("token_expired", 401, "The credential expired."),
    ("token_rotated", 401, "The credential was rotated."),
    ("scope_missing", 403, "The credential lacks the required scope."),
    ("csrf", 403, "A browser write needs X-Arete: 1."),
    ("bad_origin", 403, "The write must come from this dashboard."),
    ("tls_required", 426, "This route requires HTTPS."),
    ("auth_blocked", 429, "Too many failed authentication attempts."),
    ("method_not_allowed", 405, "The route does not accept this method."),
    ("conflict", 409, "The request conflicts with current state."),
    ("invalid_state", 409, "The task cannot be closed from its current state."),
    ("factory_off", 409, "The factory has stopped admitting work."),
    ("factory_version_conflict", 409, "Read the current factory setting before retrying."),
    ("task_version_conflict", 409, "The task was updated since the requested base version."),
    ("idempotency_in_flight", 409, "Try again with the same key after Retry-After."),
    ("body_too_large", 413, "The request body exceeds the route's size limit."),
    ("uri_too_long", 414, "The request target is too long."),
    ("unsupported_media_type", 415, "The route does not accept this Content-Type."),
    ("misdirected_request", 421, "Use the server's tailnet name or address."),
    ("invalid_fields", 422, "The request fields could not be processed."),
    ("idempotency_key_reused", 422, "Use a new key for different content."),
    ("rate_limited", 429, "Wait Retry-After seconds before sending more requests."),
    ("headers_too_large", 431, "A request takes at most 100 headers."),
    ("internal_error", 500, "The server could not complete the request."),
    ("not_implemented", 501, "The server does not implement this method."),
    ("secret_store_missing", 501, "This instance has no secret store configured."),
    ("secret_internal", 409, "This secret is managed by its owning service."),
    ("upstream_failed", 502, "The target could not complete the request."),
    ("draining", 503, "Serve is loading new code. Try again after Retry-After."),
    ("unsupported_http_version", 505, "Use HTTP/1.0 or HTTP/1.1."),
)]


def envelope(properties):
    return obj({"seq": {"type": "string", "pattern": r"^\d+\.\d+$"}, **properties})


AUTH_ERRORS = {"unauthenticated", "ambiguous_credential", "token_expired", "token_rotated", "scope_missing",
               "csrf", "bad_origin", "tls_required", "auth_blocked"}


def operation(name, path, result, inputs=None, example=None, method="GET", *, errors=(), success_status=200):
    shown = sample(result)
    if "seq" in result.get("properties", {}):
        shown["seq"] = "0.1"
    noun = name.split(".", 1)[0]
    scope = "read" if method == "GET" or noun == "batch" else f"{'task' if noun == 'comment' else noun}:write"
    optional_errors = {"factory_version_conflict", "factory_off", "invalid_state", "task_version_conflict", "secret_store_missing", "secret_internal"}
    if noun not in ("token", "pairing", "principal", "secret") and name != "session.end":
        optional_errors |= AUTH_ERRORS
    return {"name": name, "method": method, "path": "/api/v1/" + path,
            "input": inputs or obj({}), "result": result, "scope": scope, "success_status": success_status,
            "errors": [r["code"] for r in ERRORS if r["code"] not in optional_errors or r["code"] in errors],
            "example": {"input": example or {}, "result": shown}}


def sample(schema):
    if "anyOf" in schema:
        return sample(schema["anyOf"][0])
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema["type"]
    if isinstance(kind, list):
        return None if "null" in kind else sample({"type": kind[0]})
    if kind == "object":
        return {k: sample(v) for k, v in schema.get("properties", {}).items() if k in schema.get("required", [])}
    return {"string": "example", "integer": 1, "number": 1, "boolean": True, "array": []}[kind]


for _error in ERRORS:
    _code, _status, _description = (_error[k] for k in ("code", "status", "description"))
    _details = {"unknown_field": {"fields": array(STRING)}, "client_too_old": {"required_version": STRING},
                "factory_version_conflict": {"current": FACTORY_STATE}, "version_conflict": {"current": RUN}, "task_version_conflict": {"current": TASK},
                "rate_limited": {"retry_after": INTEGER}, "auth_blocked": {"retry_after": INTEGER},
                "forbidden": {"reason": STRING}, "conflict": {"reason": STRING}}.get(_code, {})
    _error["schema"] = obj({"type": {"type": "string", "enum": [f"urn:arete:problem:{_code}"]}, "title": STRING,
                            "status": {"type": "integer", "enum": [_status]}, "detail": STRING, "error": STRING,
                            "code": {"type": "string", "enum": [_code]}, **_details})
    if _code in ("forbidden", "conflict"):
        _error["schema"]["required"].remove("reason")
    _error["example"] = {**sample(_error["schema"]), "type": f"urn:arete:problem:{_code}", "status": _status,
                         "detail": _description, "error": _description}


META = envelope({"api": obj({"version": STRING, "minor": INTEGER, "contract_sha": STRING}),
                 "server": obj({"engine_sha": STRING}), "oldest_client": obj(dict.fromkeys(OLDEST_CLIENT, STRING)),
                 "instance": obj(dict.fromkeys(("id", "tag", "name", "created"), STRING)),
                 "schema": obj({"version": INTEGER, "oldest_writer": {"type": ["integer", "null"]}}),
                 "stream": obj({"complete": BOOLEAN, "head": STRING, "trim": mapping(STRING)})})
SETTINGS = obj({"max_parallel": INTEGER, "max_parallel_per_repo": INTEGER, "parallel_max": INTEGER,
                "aliases": array(STRING), "models": mapping(STRING), "defaults": mapping(STRING)})
PANEL = obj({"rows": JSON, "error": STRING}, [])
HEALTH = obj({"checks": PANEL, "alerts": PANEL, "incidents": PANEL, "now": STRING, "serve": mapping(JSON), "state": STRING,
              "devices": PANEL, "workspaces": PANEL})
SIDEBAR = obj({"now": STRING, "needs_you": INTEGER, "running": INTEGER, "questions": INTEGER, "health": STRING,
               "serve": mapping(JSON), "chat": obj({"busy": BOOLEAN, "draft": BOOLEAN})})
ASK = obj({**dict.fromkeys(("id", "repo", "ticket", "run", "step", "asked", "why", "tried", "reminded"), STRING),
           "questions": array(mapping(JSON))})
FEED_LINE = obj({"id": INTEGER, "run_id": STRING, "ts": STRING, "kind": STRING, "text": STRING})
ERROR = obj({"code": STRING, "status": INTEGER, "description": STRING, "schema": mapping(JSON), "example": mapping(JSON)})
OPERATION = obj({"name": STRING, "method": STRING, "path": STRING, "input": mapping(JSON),
                 "result": mapping(JSON), "errors": array(STRING),
                 "example": obj({"input": mapping(JSON), "result": mapping(JSON)}),
                 "deprecated": BOOLEAN, "sunset": STRING, "scope": STRING, "success_status": INTEGER,
                 "body_media": STRING},
                ["name", "method", "path", "input", "result", "errors", "example"])
ALIAS = obj({"method": STRING, "path": STRING, "provider": STRING, "adapter": STRING, "input": mapping(JSON),
             "actions": mapping(mapping(JSON)), "transport": BOOLEAN, "deprecated": BOOLEAN, "sunset": STRING},
            ["method", "path", "provider", "adapter", "input"])
TOKEN = obj({**dict.fromkeys(("id", "principal", "audience", "label", "created", "fingerprint", "kind"), STRING),
             **dict.fromkeys(("expires", "revoked_at", "rotated_from", "last_used_at", "last_used_node", "profile",
                              "grace_until", "created_by", "revoke_reason"), NULL_TEXT),
             "scopes": array(STRING), "commands": {**array(STRING), "type": ["array", "null"]},
             "version": INTEGER, "spend_cap_nusd": {"type": ["integer", "null"]}})
TOKEN_ISSUE = obj({"label": STRING, "scopes": array(STRING), "profile": {**STRING, "enum": ["remote", "full"]},
                   "expires": STRING, "commands": array(STRING), "spend_cap_usd": {**NUMBER, "minimum": 0}},
                  ["label", "scopes"])
TOKEN_ISSUED = envelope({"token": TOKEN, "text": STRING})
COHORT_REPORT = obj({"cohort": mapping(JSON), "observed": STRING, "arrivals": INTEGER, "goals": INTEGER,
                     "outcomes": mapping(mapping(INTEGER)), "members": array(mapping(JSON)),
                     "intake_complete": BOOLEAN,
                     "pending_overdue": {**INTEGER, "minimum": 0,
                                         "description": "Number of pending members with a deadline before the report observation time."}},
                    ["cohort", "observed", "arrivals", "goals", "outcomes", "members", "intake_complete"],
                    additional=JSON)
OPERATIONS = [operation("meta.get", "meta", META),
              operation("factory.get", "factory/state", envelope({"factory": FACTORY_STATE})),
              operation("factory.set", "factory/state", envelope({"factory": FACTORY_STATE}),
                        obj({"enabled": BOOLEAN, "base_version": {**INTEGER, "minimum": 1}}),
                        {"enabled": True, "base_version": 1}, "POST", errors=("factory_version_conflict",)),
              operation("contract.get", "contract", envelope({"operations": array(OPERATION),
                        "errors": array(ERROR), "aliases": array(ALIAS), "contract_sha": STRING})),
              operation("cohort.get", "cohorts/{name}", envelope({"report": COHORT_REPORT}),
                        obj({"name": STRING}), {"name": "example"}),
              operation("error.list", "errors", envelope({"errors": array(ERROR)})),
              operation("screen.get", "screen", envelope({"sidebar": SIDEBAR, "asks": obj({"asks": array(ASK)}),
                        "view": mapping(JSON)}), obj({"view": STRING, "id": STRING}, []))]
PLURALS = {name: name + "s" for name in RESOURCE}
PLURALS.update(research="research", triage="triage")
for _name, _shape in RESOURCE.items():
    _plural = PLURALS[_name]
    _selectors = {"run": STRING} if _name == "step" else {}
    OPERATIONS += [operation(f"{_name}.list", _plural, envelope({_plural: array(_shape)}),
                             obj(_selectors, list(_selectors)), dict.fromkeys(_selectors, "example")),
                   operation(f"{_name}.get", _plural + "/{id}", envelope({_name: _shape}),
                             obj({"id": STRING, **_selectors}), {"id": "example", **dict.fromkeys(_selectors, "example")})]
CONFIG_KIND = {"type": "string", "enum": list(configkinds.REGISTRY)}
CONFIG_NAME = {**STRING, "pattern": f"^(?:{configkinds.NAME_PATTERN}|{configkinds.DEFAULTS_LAYER_ADDRESS})$",
               "description": "For step_settings, defaults selects config.toml and defaults@layer selects "
                              "layers/defaults/config.toml."}
CONFIG_ID = {"kind": CONFIG_KIND, "name": CONFIG_NAME}
CONFIG_ITEM = {**CONFIG_ID, "path": STRING}
CONFIG_EXAMPLE = {"kind": "step_settings", "name": "defaults"}
SECRET = obj({**dict.fromkeys(("subject", "name", "scope", "reference_kind"), STRING),
              **dict.fromkeys(("service", "fingerprint", "last_checked", "rotated"), NULL_TEXT),
              "present": BOOLEAN, "expose": BOOLEAN, "uses": array(STRING),
              "check_ok": {"type": ["boolean", "null"]}})
SECRET_HISTORY = obj({"id": INTEGER, "event": STRING, "actor": STRING, "ok": BOOLEAN,
                      "fingerprint_after": NULL_TEXT, "at": STRING})
SECRET_TARGET = obj({"scope": STRING, "name": STRING})
SECRET_EXAMPLE = {"scope": "global", "name": "DEMO_KEY"}
ACCOUNT = obj({**dict.fromkeys(("id", "service", "mode", "usage", "holder"), STRING), "secret_name": NULL_TEXT})


def secret_operation(action, suffix, result, method="GET", internal=False):
    errors = ("secret_store_missing", "secret_internal") if internal else ("secret_store_missing",)
    return operation(f"secret.{action}", "secrets/{scope}/{name}" + suffix, envelope({"secret": SECRET, **result}),
                     SECRET_TARGET, SECRET_EXAMPLE, method, errors=errors)


OPERATIONS += [
    operation("secret.list", "secrets", envelope({"secrets": array(SECRET)}), errors=("secret_store_missing",)),
    secret_operation("get", "", {}),
    secret_operation("history", "/history", {"history": array(SECRET_HISTORY)}),
    {**secret_operation("set", "/value", {}, "PUT", internal=True), "body_media": "text/plain"},
    secret_operation("remove", "", {}, "DELETE", internal=True),
    {**secret_operation("check", "/check", {}, "POST", internal=True), "scope": "config:activate"},
    operation("account.list", "accounts", envelope({"accounts": array(ACCOUNT)})),
    operation("webhook.rotate", "loops/{loop}/webhook/rotate", envelope({"text": STRING}),
              obj({"loop": STRING}), {"loop": "example"}, "POST",
              errors=(*AUTH_ERRORS, "secret_store_missing", "secret_internal")),
    operation("config.list", "config/{kind}", envelope({"items": array(obj(CONFIG_ITEM))}),
              obj({"kind": CONFIG_KIND}), {"kind": "step_settings"}),
    operation("config.read", "config/{kind}/{name}",
              envelope({"item": obj({**CONFIG_ITEM, "content": {"type": ["string", "object"]}})}),
              obj(CONFIG_ID), CONFIG_EXAMPLE),
    operation("config.versions", "config/{kind}/{name}/versions", envelope({"versions": array(STRING)}),
              obj(CONFIG_ID), CONFIG_EXAMPLE),
    operation("config.diff", "config/{kind}/{name}/diff", envelope({"diff": STRING}),
              obj({**CONFIG_ID, "from": STRING, "to": STRING}, list(CONFIG_ID)), CONFIG_EXAMPLE),
    operation("token.list", "tokens", envelope({"tokens": array(TOKEN)})),
    operation("token.issue", "tokens", TOKEN_ISSUED, TOKEN_ISSUE,
              {"label": "Remote CLI", "scopes": ["read", "task:write"]}, "POST", success_status=201),
    operation("token.get", "tokens/{token}", envelope({"token": TOKEN}), obj({"token": STRING}), {"token": "abcdef01"}),
    operation("token.rotate", "tokens/{token}/rotate", TOKEN_ISSUED,
              obj({"token": STRING, "grace_seconds": {**INTEGER, "minimum": 0}}, ["token"]),
              {"token": "abcdef01", "grace_seconds": 300}, "POST", success_status=201),
    operation("token.revoke", "tokens/{token}/revoke", envelope({"token": TOKEN}), obj({"token": STRING}),
              {"token": "abcdef01"}, "POST"),
    operation("principal.revoke", "principals/{principal}/revoke", envelope({"principal": STRING, "revoked": INTEGER}),
              obj({"principal": STRING}), {"principal": "client:abcdef01"}, "POST"),
    operation("pairing.start", "auth/pairings", envelope({"code": STRING, "link": STRING, "expires": STRING}),
              method="POST", success_status=201),
    operation("pairing.claim", "auth/pairings/claim", envelope({"principal": STRING}),
              obj({"code": STRING}), {"code": "123456"}, "POST", success_status=201),
    operation("session.end", "auth/logout", envelope({"ended": BOOLEAN}), method="POST"),
    operation("run.diff", "runs/{id}/diff", envelope({"diff": STRING}), obj({"id": STRING}), {"id": "example"}),
    operation("task.history", "tasks/{id}/history", envelope({"comments": array(obj({"version": INTEGER,
              "author": STRING, "body": STRING, "ts": STRING})), "runs": array(RUN)}),
              obj({"id": STRING}), {"id": "example"}),
    operation("line_stream.read", "line-stream", envelope({"lines": array(FEED_LINE), "after": INTEGER}),
              obj({"run": STRING, "after": STRING}, ["run"]), {"run": "example"}),
    operation("settings.get", "settings", envelope({"settings": SETTINGS})),
    operation("health.get", "health", envelope({"health": HEALTH})),
    operation("status.get", "status", envelope({"snapshot": mapping(JSON), "loops": array(mapping(JSON)),
              "records": PANEL}), obj({"limit": STRING}, [])),
    operation("task.create", "tasks", envelope({"task": TASK}),
              obj({"repo": STRING, "body": STRING, "title": STRING, "kind": STRING}, ["repo", "body"]),
              {"repo": "example", "body": "Work to do"}, "POST"),
    operation("task.update", "tasks/{id}", envelope({"task": TASK}),
              obj({"id": STRING, "base_version": {**INTEGER, "minimum": 1}, "title": STRING, "body": STRING,
                   "kind": STRING, "state": STRING}, ["id", "base_version"]),
              {"id": "t-a1b2", "title": "Updated title", "base_version": 1}, "PATCH", errors=("task_version_conflict",)),
    operation("task.close", "tasks/{task}/close", envelope({"task": TASK}), obj({"task": STRING}),
              {"task": "t-a1b2"}, "POST", errors=("invalid_state",)),
    operation("comment.create", "tasks/{task}/comments", envelope({"task": TASK}),
              obj({"task": STRING, "body": STRING}), {"task": "t-a1b2", "body": "Reply"}, "POST"),
    operation("run.stop", "runs/{run}/stop", envelope({"id": STRING, "stopped": BOOLEAN, "pid": INTEGER,
              "was": STRING, "ticket": STRING}), obj({"run": STRING, "base_version": {**INTEGER, "minimum": 1}}),
              {"run": "pip-demo-1", "base_version": 1}, "POST"),
    operation("run.steer", "runs/{run}/steer", envelope({"id": STRING, "queued": BOOLEAN}),
              obj({"run": STRING, "text": STRING}), {"run": "session-demo-1", "text": "Continue"}, "POST"),
    *(operation(f"run.{action}", f"runs/{{run}}/{action}", envelope({"id": STRING}), obj({"run": STRING}),
                {"run": "pip-demo-1"}, "POST") for action in ("pin", "unpin")),
    operation("chat.drop", "chats/{chat}", envelope({"chat": mapping(JSON)}), obj({"chat": STRING}),
              {"chat": "1"}, "DELETE"),
    operation("serve.install", "serve/install", envelope({"restarting": BOOLEAN, "said": STRING}),
              method="POST"),
    operation("hook_event.list", "hook-events", envelope({"events": array(mapping(JSON))}), obj({"since": STRING}, [])),
    operation("hook.ingest", "hook-events", envelope({"received": INTEGER}),
              obj({"origin": {**STRING, "pattern": "^[0-9a-f]{32}$"}, "stream": {**STRING, "pattern": "^[0-9a-f]{64}$"},
                   "offset": {**INTEGER, "minimum": 0}, "events": array(HOOK_EVENT)}, ["origin", "offset", "events"]),
              {"origin": "0123456789abcdef0123456789abcdef", "offset": 0,
               "events": [{"ts": "2026-01-01T00:00:00Z", "kind": "skill", "cls": "invoked", "session": None}]}, "POST"),
    operation("batch.run", "batch", envelope({"results": array(mapping(JSON))}),
              obj({"requests": array(obj({"operation": STRING, "input": mapping(JSON)}, ["operation"]))}),
              {"requests": [{"operation": "meta.get"}]}, "POST"),
    operation("usage.record", "usage", obj({}), obj({"kind": STRING, "device": STRING, "tab": STRING,
              "view": STRING, "via": STRING, "name": STRING, "ms": INTEGER}, ["kind", "device", "tab", "view"]),
              {"kind": "visit", "device": "desktop", "tab": "0123456789abcdef", "view": "live", "via": "open"}, "POST"),
]
for _op in OPERATIONS:
    if _op["name"] in ("run.stop", "serve.install"):
        optional = {"pid", "was", "ticket", "said"}
        shape = _op["result"]
        shape["required"] = [key for key in shape["required"] if key not in optional]
        _op["example"]["result"] = {key: _op["example"]["result"][key] for key in shape["required"]}
BY_NAME = {op["name"]: op for op in OPERATIONS}
BY_NAME["batch.run"]["result"]["properties"]["results"]["items"] = {
    "anyOf": [op["result"] for op in OPERATIONS if op["method"] == "GET"]}
BY_NAME["batch.run"]["input"]["properties"]["requests"].update(minItems=1, maxItems=50)
BY_NAME["batch.run"]["input"]["properties"]["requests"]["items"]["properties"]["operation"] = {
    "type": "string", "enum": [op["name"] for op in OPERATIONS if op["method"] == "GET"]}
BY_NAME["usage.record"]["input"]["properties"].update(
    kind={"type": "string", "enum": ["visit", "action"]},
    device={"type": "string", "enum": ["desktop", "phone"]},
    tab={"type": "string", "pattern": "^[0-9a-f]{16}$"},
    ms={"type": "integer", "minimum": 0, "maximum": 86400000})
BY_NAME["meta.get"]["example"]["result"].update(
    api={"version": VERSION, "minor": MINOR, "contract_sha": "0" * 64},
    server={"engine_sha": "code.7b1170924da4"}, oldest_client=OLDEST_CLIENT,
    instance={"id": "a1b2c3d4", "tag": "demo", "name": "Demo", "created": "2026-01-01T00:00:00Z"},
    stream={"complete": True, "head": "0.1", "trim": {}})
ALIASES = []
USES = Counter()
USE_LOCK = threading.Lock()


def publish():
    return {"operations": OPERATIONS, "errors": ERRORS, "aliases": ALIASES}


@cache
def digest():
    return hashlib.sha256(json.dumps(publish(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Problem(refusals.Stated):
    def __init__(self, code, message, **details):
        super().__init__(next(r["status"] for r in ERRORS if r["code"] == code), message)
        self.code, self.details = code, details
        self.retry_after = details.get("retry_after", 0)


def unknown(value, schema, path=""):
    found = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            at = f"{path}.{key}" if path else key
            if key in properties:
                found.extend(unknown(item, properties[key], at))
            elif extra is False:
                found.append(at)
            elif isinstance(extra, dict):
                found.extend(unknown(item, extra, at))
    elif isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            found.extend(unknown(item, schema["items"], f"{path}[{i}]"))
    return sorted(found)


def reject_unknown(value, schema, path=""):
    if fields := unknown(value, schema, path):
        raise Problem("unknown_field", "Undeclared fields: " + ", ".join(fields), fields=fields)


def validate(value, schema, path="input"):
    if "anyOf" in schema:
        for candidate in schema["anyOf"]:
            try:
                validate(value, candidate, path)
                return
            except Problem:
                pass
        raise Problem("invalid_input", f"{path} matches no declared result shape")
    reject_unknown(value, schema)
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    kind = ("null" if value is None else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int)
            else "number" if isinstance(value, float) else "string" if isinstance(value, str)
            else "array" if isinstance(value, (list, tuple)) else "object" if isinstance(value, dict) else "unknown")
    if kinds and kind not in kinds and not (kind == "integer" and "number" in kinds):
        raise Problem("invalid_input", f"{path} must be {' or '.join(kinds)}")
    if "enum" in schema and value not in schema["enum"]:
        raise Problem("invalid_input", f"{path} is not a declared value")
    if isinstance(value, str) and "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        raise Problem("invalid_input", f"{path} has an invalid format")
    if kind in ("integer", "number") and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
        raise Problem("invalid_input", f"{path} is outside its allowed range")
    if isinstance(value, (list, tuple)) and not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
        raise Problem("invalid_input", f"{path} has an invalid number of items")
    if isinstance(value, dict):
        if missing := set(schema.get("required", [])) - value.keys():
            raise Problem("invalid_input", f"{path} needs {', '.join(sorted(missing))}")
        for key, item in value.items():
            child = schema.get("properties", {}).get(key, schema.get("additionalProperties"))
            if isinstance(child, dict):
                validate(item, child, f"{path}.{key}")
    if isinstance(value, (list, tuple)) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")


def match(method, path):
    for op in OPERATIONS:
        if method == op["method"] and (found := re.fullmatch(re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", op["path"]), path)):
            return op, found.groupdict()
    return None


def keyed_write(op):
    return op["method"] != "GET" and op["name"] not in ("batch.run", "usage.record")


def fields(names):
    return obj(dict.fromkeys(names.split(), JSON), [])


SESSION_INPUT = fields("prompt device model runner tools repo budget_usd")
ALIAS_INPUTS = {
    "post_task": fields("repo prompt title kind ticket force"),
    "post_usage": BY_NAME["usage.record"]["input"],
    "post_settings": obj({"max_parallel": JSON, "max_parallel_per_repo": JSON,
                          "models": {**mapping(STRING), "type": ["object", "null"]}}, []),
    "post_capture": fields("kind text expected actual steps"),
    "post_capture_context": obj({"view": JSON, "screen": JSON,
                                 "detail": {**fields("kind id"), "type": ["object", "null"]}}, []),
    "post_capture_submit": fields("id revision"),
    "post_chat_new": fields("project"),
    "post_loop_new": fields("sentence name repo model"),
    "post_incident_switch": fields("on"),
    "post_push_subscribe": obj({"endpoint": JSON, "expirationTime": JSON, "label": JSON,
                                "keys": fields("auth p256dh")}, []),
    "post_push_remove": fields("id"),
    "post_sessions": SESSION_INPUT,
    "post_approval": fields("approve"),
}


def alias_input(provider, rest):
    if provider == "post_run":
        action = rest.partition("/")[2]
        if action.startswith("steps/"):
            return obj({"base_version": JSON, "set": mapping(JSON)}, [])
        return fields("text" if action == "answer" else "base_version step" if action == "resume" else "base_version")
    if provider == "post_chat":
        if rest.endswith("/sessions"):
            return SESSION_INPUT
        if rest.endswith("/file"):
            return obj({"draft": STRING}, [])
        return fields("text") if rest.endswith("/send") else obj({})
    if provider == "post_session":
        return fields("text") if rest.endswith("/send") else obj({})
    if provider == "post_triage":
        return fields("questions answers repo") if rest.endswith("/send") else fields("force")
    if provider == "post_ask":
        answer = {"type": ["object", "null"], "properties": {
                  "text": STRING, "secret": STRING, "choice": {"type": ["string", "integer", "array"],
                  "items": {"type": ["string", "integer"]}}, "done": BOOLEAN, "declined": BOOLEAN},
                  "additionalProperties": False}
        return obj({"answers": {**array(answer), "type": ["array", "null"]}, "recommended": BOOLEAN, "stop": BOOLEAN}, [])
    return ALIAS_INPUTS.get(provider, obj({}))


ALIAS_ROUTES = (
    ("GET", "/api/meta", "get_meta"),
    ("GET", "/api/push", "get_push"),
    ("GET", "/api/sidebar", "get_sidebar"),
    ("GET", "/api/live", "get_live"),
    ("GET", "/api/runs", "get_runs"),
    ("GET", "/api/runs/*", "get_run"),
    ("GET", "/api/tasks", "get_tasks"),
    ("GET", "/api/tasks/*", "get_task"),
    ("GET", "/api/asks", "get_asks"),
    ("GET", "/api/health", "get_health"),
    ("GET", "/api/triage", "get_triage"),
    ("GET", "/api/outcomes", "get_outcomes"),
    ("GET", "/api/find", "get_find"),
    ("GET", "/api/usage", "get_usage"),
    ("GET", "/api/settings", "get_settings"),
    ("GET", "/api/capture", "get_capture"),
    ("GET", "/api/chats", "get_chats"),
    ("GET", "/api/chats/*", "get_chat"),
    ("GET", "/api/log/*", "get_log"),
    ("GET", "/api/loops", "get_loops"),
    ("GET", "/api/loops/*", "get_loop"),
    ("GET", "/api/incidents", "get_incidents"),
    ("GET", "/api/incidents/*", "get_incident"),
    ("GET", "/api/feed/*", "get_feed"),
    ("GET", "/api/sessions", "get_sessions"),
    ("POST", "/api/usage", "post_usage"),
    ("POST", "/api/capture", "post_capture"),
    ("POST", "/api/capture/context", "post_capture_context"),
    ("POST", "/api/capture/submit", "post_capture_submit"),
    ("POST", "/api/capture/context/drop", "post_capture_context_drop"),
    ("POST", "/api/capture/discard", "post_capture_discard"),
    ("POST", "/api/capture/evidence/*", "post_capture_evidence"),
    ("POST", "/api/settings", "post_settings"),
    ("POST", "/api/chats/new", "post_chat_new"),
    ("POST", "/api/chats/*", "post_chat"),
    ("POST", "/api/incidents/switch", "post_incident_switch"),
    ("POST", "/api/incidents/*", "post_incident"),
    ("POST", "/api/loops/new", "post_loop_new"),
    ("POST", "/api/loops/*", "post_loop"),
    ("POST", "/api/runs/*", "post_run"),
    ("POST", "/api/push/subscribe", "post_push_subscribe"),
    ("POST", "/api/push/remove", "post_push_remove"),
    ("POST", "/api/push/test", "post_push_test"),
    ("POST", "/api/asks/*", "post_ask"),
    ("POST", "/api/approvals/*", "post_approval"),
    ("POST", "/api/tasks", "post_task"),
    ("POST", "/api/sessions", "post_sessions"),
    ("POST", "/api/sessions/*", "post_session"),
    ("POST", "/api/devices/*", "post_device"),
    ("POST", "/api/triage/refresh", "post_triage_refresh"),
    ("POST", "/api/triage/*", "post_triage"),
    ("POST", "/api/checks/run", "post_checks_run"),
    ("POST", "/api/skipped/*", "post_skipped"),
    ("POST", "/api/proposals/*", "post_proposal"),
    ("POST", "/api/serve/install", "post_serve_install"),
)
ALIAS_VARIANTS = {"post_run": ("stop", "answer", "resume", "repair", "steps/{step}"),
                  "post_chat": ("send", "file", "drop", "sessions", "sessions/{session}/stop"),
                  "post_session": ("send", "stop"), "post_triage": ("send", "research"),
                  "post_incident": ("dismiss", "retry", "cancel"),
                  "post_loop": ("enable", "disable", "start", "try"), "post_device": ("probe",),
                  "post_proposal": ("done", "approve", "reject"), "post_skipped": ("run",),
                  "post_ask": ("answer",), "post_capture_evidence": ("drop",),
                  "get_chat": ("sessions", "sessions/{session}/report")}
ALIASES.extend({"method": method, "path": path, "provider": provider, "adapter": "frozen-v0",
                "input": alias_input(provider, "") if method == "POST" else obj({}),
                "actions": {action: alias_input(provider, "id/" + action) for action in ALIAS_VARIANTS.get(provider, ())}}
               for method, path, provider in ALIAS_ROUTES)
ALIASES[:0] = [{"method": method, "path": path, "provider": provider, "adapter": "identity", "transport": True,
               "input": mapping(STRING) if method == "POST" else obj({})} for method, path, provider in (
    ("POST", "/api/capture/evidence", "stage_evidence"),
    ("GET", "/api/capture/evidence/*", "send_evidence"),
    ("GET", "/api/runs/*/events", "send_events"),
    ("GET", "/api/events", "event_stream.serve"),
)]


def complete_examples():
    instances = {"task": "t-a1b2", "project": "replay-sample", "line": "replay-sample", "view": "live",
                 "run": "pip-demo-1", "step": "1", "loop": "poll", "incident": "1",
                 "triage": "demo#7", "research": "demo#7", "chat": "1"}
    for name, identity in instances.items():
        get = BY_NAME[f"{name}.get"]["example"]
        get["input"]["id"] = identity
        row = get["result"][name]
        row["id"] = int(identity) if name == "step" else identity
        BY_NAME[f"{name}.list"]["example"]["result"][PLURALS[name]] = [row.copy()]
    for name in ("step.get", "step.list", "line_stream.read"):
        BY_NAME[name]["example"]["input"]["run"] = instances["run"]
    BY_NAME["step.get"]["example"]["result"]["step"].update(run_id=instances["run"], name="plan")
    BY_NAME["run.diff"]["example"]["input"]["id"] = instances["run"]
    BY_NAME["run.diff"]["example"]["result"]["diff"] = "--- a/notes.txt\n+++ b/notes.txt\n@@ -1 +1,2 @@\n Notes\n+kumquat\n"
    BY_NAME["task.history"]["example"]["input"]["id"] = instances["task"]
    BY_NAME["line_stream.read"]["example"]["result"].update(after=1, lines=[{
        "id": 1, "run_id": instances["run"], "ts": "2026-01-01T00:00:00Z", "kind": "plan", "text": "Add kumquat"}])
    BY_NAME["batch.run"]["example"]["result"]["results"] = [BY_NAME["meta.get"]["example"]["result"]]
    BY_NAME["error.list"]["example"]["result"]["errors"] = ERRORS[:1]
    BY_NAME["contract.get"]["example"]["result"].update(
        operations=[BY_NAME["meta.get"]], errors=ERRORS[:1], aliases=ALIASES[:1], contract_sha="0" * 64)


complete_examples()


def alias_match(method, path):
    for entry in ALIASES:
        if entry["method"] != method:
            continue
        template = entry["path"]
        if (template == path or template.endswith("/*") and path.startswith(template[:-1])
                or "*" in template and re.fullmatch(re.escape(template).replace(r"\*", "[^/]+"), path)):
            return entry
    return None
