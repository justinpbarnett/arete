from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter
from functools import cache

from . import configkinds, refusals

MINOR = 28
VERSION = f"1.{MINOR}"
OLDEST_CLIENT = dict.fromkeys(("cli", "mcp", "web"), "1.0")
STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
BOOLEAN = {"type": "boolean"}
JSON = {"type": ["object", "array", "string", "number", "boolean", "null"]}
NULL_TEXT = {"type": ["string", "null"]}
NULL_INT = {"type": ["integer", "null"]}
NULL_NUM = {"type": ["number", "null"]}


def obj(properties, required=None, additional=False):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": additional}


def array(items):
    return {"type": "array", "items": items}


def mapping(values):
    return obj({}, [], values)


REF = obj({
    "id": STRING,
    "system": {**STRING, "enum": ["github", "jira", "slack", "email", "meeting", "intake"]},
    "kind": {**STRING, "enum": ["issue", "ticket", "pr", "thread", "email", "transcript", "paste"]},
    "key": STRING,
    "connector": STRING,
    "title": STRING,
    "url": STRING,
    "ext_state": STRING,
    "facts": mapping(JSON),
    "run_id": NULL_TEXT,
    "remote_updated": NULL_TEXT,
    "fetched_at": NULL_TEXT,
    "sync": {**STRING, "enum": ["local", "synced", "ahead", "behind", "conflict", "off"]},
    "version": INTEGER,
    "created": STRING,
    "updated": STRING,
})
TASK_REF = obj({
    "id": INTEGER,
    "task": STRING,
    "task_id": INTEGER,
    "ref": STRING,
    "ref_id": INTEGER,
    "role": {**STRING, "enum": ["primary", "origin", "related", "parent"]},
    "interpretation": STRING,
    "claims": mapping(JSON),
    "anchor": array(JSON),
    "source_rev": STRING,
    "linked_by": STRING,
    "confidence": NULL_NUM,
    "version": INTEGER,
    "created": STRING,
    "updated": STRING,
    "system": STRING,
    "kind": STRING,
    "key": STRING,
    "title": STRING,
    "url": STRING,
    "ref_detail": REF,
}, ["id", "task", "task_id", "ref", "ref_id", "role", "interpretation", "claims", "anchor",
    "source_rev", "linked_by", "confidence", "version", "created", "updated"])
REF_TASK_LINK = obj({
    **TASK_REF["properties"],
    "task_title": STRING,
    "task_state": STRING,
    "task_repo": STRING,
}, [*TASK_REF["required"], "task_title", "task_state", "task_repo"])
TASK_LINK = obj({
    "id": INTEGER,
    "task": STRING,
    "task_id": INTEGER,
    "kind": {**STRING, "enum": ["waits_on", "blocks", "relates", "duplicate"]},
    "other_task": NULL_TEXT,
    "other_key": NULL_TEXT,
    "origin": {**STRING, "enum": ["person", "agent", "body", "jira_link"]},
    "created": STRING,
})
TASK_COMMENT = obj({
    "id": INTEGER,
    "version": INTEGER,
    "author": STRING,
    "body": STRING,
    "ts": STRING,
    "created": STRING,
    "updated": STRING,
    "audience": NULL_TEXT,
}, ["version", "author", "body", "ts"])
_TASK_REQUIRED = ["id", "repo", "title", "body", "kind", "state", "parent", "created", "updated", "version"]
TASK = obj({
    **dict.fromkeys(("id", "repo", "title", "body", "kind", "state", "parent", "created", "updated"), STRING),
    "version": INTEGER,
    "project": NULL_TEXT,
    "line": NULL_TEXT,
    "fault": INTEGER,
    "fault_detail": NULL_TEXT,
    "priority": NULL_INT,
    "rank": NULL_NUM,
    "owner": NULL_TEXT,
    "origin": NULL_TEXT,
    "delegate": NULL_TEXT,
    "estimate": NULL_TEXT,
    "answer": NULL_TEXT,
    "refs": array(TASK_REF),
    "primary_ref": {"anyOf": [REF, {"type": "null"}]},
    "links": array(TASK_LINK),
}, _TASK_REQUIRED)
RUN_TEXT = ("id", "kind", "repo", "issue", "loop", "loop_kind", "started", "finished", "status", "summary", "pr_url",
            "branch", "only_step", "files_purged", "records_purged", "mined_until", "stop_asked", "ab_held",
            "measured_at", "landed", "device", "model", "runner", "log", "trigger", "queue", "grp", "lock", "moved",
            "engine_sha", "label_pending", "wait_minutes", "config_commit", "resolved_sha", "continues", "chain",
            "restart", "started_by", "payer", "adw", "adw_version", "adw_rule", "task", "issue_key", "line")
RUN_NUMBERS = ("pid", "cost_usd", "tokens_in", "tokens_out", "active_seconds", "verify_seconds", "verify_open_seconds",
               "repair_calls", "schema_version", "attempts", "version", "seq", "lease_id", "task_id")
RUN_STEP_SUMMARY = obj({
    "id": INTEGER,
    "node": STRING,
    "try": INTEGER,
    "status": STRING,
    "queued_at": {"type": ["string", "null"]},
    "model": STRING,
    "account": {"type": ["string", "null"]},
    "cost_usd": NUMBER,
}, ["node", "try", "status"])
RUN = obj({**{k: {"type": ["string", "null"]} for k in RUN_TEXT},
           **{k: {"type": ["number", "null"]} for k in RUN_NUMBERS}}, ["id", "status", "version"])
RUN["properties"]["issue"] = {"type": ["integer", "string", "null"]}
RUN["properties"]["version"] = INTEGER
FACTORY_STATE = obj({"enabled": BOOLEAN, "phase": STRING, "version": INTEGER, "actor": STRING, "changed": STRING,
                     "unconfirmed": INTEGER, "paused": INTEGER, "targets": array(obj(dict.fromkeys(
                         ("id", "phase", "reason", "successor"), STRING)))})
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
              "in_sprint": JSON, "cost": NUMBER, "version": INTEGER,
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
PROJECT = obj({
    "id": STRING,
    "name": STRING,
    "github": STRING,
    "path": STRING,
    "repo": STRING,
    "priority": NULL_INT,
    "rank": NULL_NUM,
    "archived": BOOLEAN,
    "version": INTEGER,
    "created": STRING,
    "updated": STRING,
}, ["id", "name", "github", "path"])
LINE = obj({
    "id": STRING,
    "kind": STRING,
    "repo": STRING,
    "enabled": BOOLEAN,
    "project": STRING,
    "name": STRING,
    "adw_route": STRING,
    "device_pool": array(STRING),
    "rank": NULL_NUM,
    "concurrency": INTEGER,
    "wip_limit": NULL_INT,
    "priority": NULL_INT,
    "paused": BOOLEAN,
    "paused_why": STRING,
    "band": mapping(array(NUMBER)),
    "archived": BOOLEAN,
    "version": INTEGER,
    "created": STRING,
    "updated": STRING,
}, ["id", "kind", "repo", "enabled"])
VIEW = obj({
    "id": STRING,
    "label": STRING,
    "data": mapping(JSON),
    "name": STRING,
    "project": NULL_TEXT,
    "filter": STRING,
    "filter_sql": STRING,
    "group_by": STRING,
    "sort": STRING,
    "layout": STRING,
    "columns": array(STRING),
    "rank": NULL_NUM,
    "version": INTEGER,
    "created": STRING,
    "updated": STRING,
}, ["id", "label"])
RESOURCE = {
    "task": TASK, "run": RUN,
    "project": PROJECT,
    "line": LINE,
    "view": VIEW,
    "step": obj({"id": INTEGER, "run_id": STRING, "name": STRING, "model": STRING, "runner": STRING,
                 "cost_usd": NUMBER, "seconds": NUMBER,
                 "settings_sha": NULL_TEXT,
                 "settings": {"type": ["object", "null"], "additionalProperties": JSON},
                 "inputs": {"type": ["array", "null"], "items": mapping(JSON)},
                 "prompt_version": NULL_TEXT,
                 "prompt_level": NULL_TEXT,
                 "schema_version": NULL_TEXT,
                 "node": STRING, "try": INTEGER, "status": STRING, "queued_at": NULL_TEXT,
                 "waiting_since": NULL_TEXT, "device": NULL_TEXT, "account": NULL_TEXT,
                 "chat_id": {"type": ["integer", "null"]}, "version": INTEGER,
                 "runner_version": NULL_TEXT, "unread": INTEGER},
                ["id", "run_id", "name", "model", "runner", "cost_usd", "seconds"]),
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
    ("lease_fenced", 410, "This lease has been fenced by a newer epoch or release."),
    ("lease_expired", 410, "This lease has expired."),
    ("files_purged", 410, "The retained files for this resource were purged."),
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
    ("already_resolved", 409, "This inbox item is already resolved."),
    ("still_missing", 409, "The required secret is still missing."),
    ("line_in_use", 409, "The line is still referenced or is the last active pipeline line."),
    ("incompatible_writers", 409, "Incompatible writers are still active; wait for them to drain or upgrade before retrying."),
    ("validation", 422, "The request failed domain validation."),
    ("gate_refused", 403, "The owner refused the pending presence request."),
    ("presence_expired", 409, "The presence approval expired before it was approved."),
    ("upstream_failed", 502, "The target could not complete the request."),
    ("draining", 503, "Serve is loading new code. Try again after Retry-After."),
    ("unsupported_http_version", 505, "Use HTTP/1.0 or HTTP/1.1."),
)]


def envelope(properties, required=None):
    all_props = {"seq": {"type": "string", "pattern": r"^\d+\.\d+$"}, **properties}
    return obj(all_props, list(all_props) if required is None else required)


AUTH_ERRORS = {"unauthenticated", "ambiguous_credential", "token_expired", "token_rotated", "scope_missing",
               "csrf", "bad_origin", "tls_required", "auth_blocked"}


def operation(name, path, result, inputs=None, example=None, method="GET", *, errors=(), success_status=200):
    shown = sample(result)
    if isinstance(shown, dict) and "seq" in shown:
        shown["seq"] = "0.1"
    noun = name.split(".", 1)[0]
    scope = "read" if method == "GET" or noun == "batch" else (
        "task:write" if noun in ("comment", "task_ref", "task_link", "project", "line", "view") else f"{noun}:write"
    )
    optional_errors = {"factory_version_conflict", "factory_off", "invalid_state", "task_version_conflict",
                       "secret_store_missing", "secret_internal", "already_resolved", "still_missing", "validation",
                       "gate_refused", "presence_expired", "lease_fenced", "lease_expired", "files_purged",
                       "line_in_use", "incompatible_writers"}
    if noun not in ("token", "pairing", "principal", "secret", "inbox", "push", "outbox", "device", "lease") and name != "session.end":
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


INBOX_CONTINUE_DETAIL = obj({
    "name": STRING,
    "scope": STRING,
    "required": BOOLEAN,
    "status": STRING,
    "missing_since": NULL_TEXT,
    "last_check": obj({"checked_at": NULL_TEXT, "ok": {"type": ["boolean", "null"]}}, ["checked_at", "ok"]),
    "waiting": array(STRING),
    "group_key": STRING,
    "group_members": array(STRING),
    "stamp": STRING,
}, ["name", "scope", "last_check", "waiting", "group_key", "group_members"], additional=JSON)
INBOX_JIDOKA = obj({
    "detect": STRING,
    "stop": STRING,
    "fix": STRING,
    "prevent": STRING,
}, ["detect", "stop", "fix", "prevent"])
INBOX_STOP_DETAIL = obj({
    "run_id": STRING,
    "node": STRING,
    "error": STRING,
    "error_class": STRING,
    "attempts": INTEGER,
    "jidoka": INBOX_JIDOKA,
    "last_lines": array(STRING),
    "group_key": STRING,
    "stamp": STRING,
}, ["node", "jidoka", "last_lines", "group_key"], additional=JSON)
INBOX_FAULT_DETAIL = obj({
    "run_id": STRING,
    "node": STRING,
    "error": STRING,
    "error_class": STRING,
    "attempts": INTEGER,
    "last_lines": array(STRING),
    "shelved_reason": NULL_TEXT,
    "shelve_until": NULL_TEXT,
    "group_key": STRING,
    "stamp": STRING,
}, ["node", "error", "attempts", "last_lines", "group_key"], additional=JSON)
INBOX_DETAIL_SHAPES = {
    "continue": INBOX_CONTINUE_DETAIL,
    "stop": INBOX_STOP_DETAIL,
    "fault": INBOX_FAULT_DETAIL,
}
INBOX_ITEM = obj({
    **dict.fromkeys(("id", "kind", "subject", "task_id", "run_id", "line", "title", "state", "snooze_until",
                     "snooze_watch", "shelve_reason", "shelve_until", "answer", "answered_by", "answered_at",
                     "resolution", "reminded", "raised", "resolved"), STRING),
    **dict.fromkeys(("priority", "snooze_watch_ver", "shelves", "version"), INTEGER),
    "detail": mapping(JSON),
    "verbs": array(STRING),
    "options": array(JSON),
    "outbox_id": NULL_TEXT,
}, [
    "id", "kind", "subject", "task_id", "run_id", "line", "title", "state", "snooze_until",
    "snooze_watch", "shelve_reason", "shelve_until", "answer", "answered_by", "answered_at",
    "resolution", "reminded", "raised", "resolved", "priority", "snooze_watch_ver", "shelves",
    "version", "detail", "verbs", "options",
])
INBOX_EVENT = obj({
    "id": STRING, "seq": INTEGER, "epoch": STRING, "at": STRING, "kind": STRING, "subject": STRING,
    "ver": {"type": ["integer", "null"]}, "actor": STRING, "actor_name": NULL_TEXT, "data": mapping(JSON),
}, ["id", "seq", "at", "kind", "subject", "actor", "data"], additional=JSON)
OUTBOX_ITEM = obj({
    **dict.fromkeys(("id", "target", "kind", "object", "audience", "drafted_by", "slot", "state",
                     "summary", "plain_summary", "review", "created", "updated"), STRING),
    **dict.fromkeys(("marker", "posts_as", "task_id", "ref_id", "run_id", "step", "loop", "opened_at",
                     "approved_by", "approved_rule", "approved_rule_version", "send_at", "base_remote_updated",
                     "sent_at", "sent_ref", "sent_url", "error", "reject_reason", "sending_since"), NULL_TEXT),
    **dict.fromkeys(("rev", "attempts", "shape", "version"), INTEGER),
    "legacy_approval": NULL_INT,
})
OUTBOX_REVISION = obj({
    "item": INTEGER, "rev": INTEGER, "base_rev": NULL_INT, "by": STRING, "body": STRING, "at": STRING,
})
OUTBOX_REDACTION = obj({
    "item": INTEGER, "rev": INTEGER, "n": INTEGER, "kind": STRING, "start": INTEGER, "end": INTEGER, "why": STRING,
})
DELIVERY_ATTEMPT = obj({
    "item": INTEGER, "n": INTEGER, "started": STRING, "ended": NULL_TEXT, "outcome": NULL_TEXT,
    "http_status": NULL_INT, "retry_after": NULL_INT, "external_id": NULL_TEXT, "external_url": NULL_TEXT,
    "lookup": NULL_TEXT, "error": NULL_TEXT,
})
PUSH_SUBSCRIPTION = obj({
    **dict.fromkeys(("id", "endpoint_host", "label", "created"), STRING),
    "last_success": NULL_TEXT,
})
PUSH_SUBSCRIBE = obj({
    "endpoint": STRING,
    "keys": obj({"p256dh": STRING, "auth": STRING}),
    "label": STRING,
}, ["endpoint", "keys"])

for _error in ERRORS:
    _code, _status, _description = (_error[k] for k in ("code", "status", "description"))
    _details = {"unknown_field": {"fields": array(STRING)}, "client_too_old": {"required_version": STRING},
                "factory_version_conflict": {"current": FACTORY_STATE}, "version_conflict": {"current": RUN},
                "task_version_conflict": {"current": TASK}, "already_resolved": {"current": INBOX_ITEM},
                "invalid_state": {"current": JSON},
                "rate_limited": {"retry_after": INTEGER}, "auth_blocked": {"retry_after": INTEGER},
                "lease_fenced": {"epoch": INTEGER}, "lease_expired": {"epoch": INTEGER},
                "files_purged": {"purged_at": STRING},
                "line_in_use": {"blockers": array(JSON)},
                "incompatible_writers": {"blockers": array(STRING), "retry_guidance": STRING},
                "forbidden": {"reason": STRING}, "conflict": {"reason": STRING}}.get(_code, {})
    _error["schema"] = obj({"type": {"type": "string", "enum": [f"urn:arete:problem:{_code}"]}, "title": STRING,
                            "status": {"type": "integer", "enum": [_status]}, "detail": STRING, "error": STRING,
                            "code": {"type": "string", "enum": [_code]}, **_details})
    if _code in ("forbidden", "conflict"):
        _error["schema"]["required"].remove("reason")
    if _code == "line_in_use":
        _error["schema"]["required"].remove("blockers")
    if _code == "incompatible_writers":
        _error["schema"]["required"].remove("blockers")
        _error["schema"]["required"].remove("retry_guidance")
    if _code == "invalid_state":
        _error["schema"]["required"].remove("current")
    _error["example"] = {**sample(_error["schema"]), "type": f"urn:arete:problem:{_code}", "status": _status,
                         "detail": _description, "error": _description}


META = envelope({"api": obj({"version": STRING, "minor": INTEGER, "contract_sha": STRING}),
                 "server": obj({"engine_sha": STRING}), "oldest_client": obj(dict.fromkeys(OLDEST_CLIENT, STRING)),
                 "instance": obj(dict.fromkeys(("id", "tag", "name", "created"), STRING)),
                 "schema": obj({"version": INTEGER, "oldest_writer": {"type": ["integer", "null"]}}),
                 "stream": obj({"complete": BOOLEAN, "head": STRING, "trim": mapping(STRING)}),
                 "features": obj({"inbox": BOOLEAN, "outbox": BOOLEAN, "run_start_tx": BOOLEAN, "refs": BOOLEAN}, ["inbox"]),
                 "deprecated": array(STRING),
                 "push": obj({"vapid_public_key": STRING})})
META["required"] = [k for k in META["required"] if k not in ("features", "deprecated", "push")]
ROLE_ENUM = {**STRING, "enum": list(configkinds.ROLE_NAMES)}
ROLE_LEVEL = {"type": ["string", "null"],
              "enum": ["inherit", "engine", "config", "config kind", "layer", "layer kind", "arm", "run", None]}
ROLE_STEP_OVERRIDES = obj({"runner": STRING, "model": STRING, "effort": STRING}, [], additional=False)
ROLE_OVERRIDES = obj({
    "models": {**mapping(STRING), "maxProperties": 1},
    "steps": {**mapping(ROLE_STEP_OVERRIDES), "maxProperties": 1},
}, ["models", "steps"], additional=False)
ROLE_VIEW = obj({
    "role": ROLE_ENUM,
    "runner": NULL_TEXT,
    "model": NULL_TEXT,
    "effort": NULL_TEXT,
    "level": ROLE_LEVEL,
    "overrides": ROLE_OVERRIDES,
})
BLOB_SHA = {**STRING, "pattern": "^[0-9a-f]{64}$"}
BLOB_META = obj({"sha256": STRING, "kind": STRING, "bytes": INTEGER})
BLOB_DATA = obj({"sha256": STRING, "kind": STRING, "bytes": INTEGER, "data": STRING,
                 "encoding": {**STRING, "enum": ["utf-8", "base64"]}})
RESOLVED_LEVEL_ENTRY = obj({"level": ROLE_LEVEL, "from": NULL_TEXT})
RESOLVED_SETTINGS = envelope({
    "values": mapping(mapping(JSON)),
    "levels": mapping(mapping(RESOLVED_LEVEL_ENTRY)),
    "roles": array(ROLE_VIEW),
    "config_commit": NULL_TEXT,
}, ["seq", "values", "levels", "config_commit"])
SETTINGS_VALUES = obj({
    "max_parallel": INTEGER,
    "parallel_max": INTEGER,
    "aliases": array(STRING),
    "models": mapping(STRING),
    "defaults": mapping(STRING),
    "roles": array(ROLE_VIEW),
})
SETTINGS = obj({
    "max_parallel": INTEGER,
    "parallel_max": INTEGER,
    "aliases": array(STRING),
    "models": mapping(STRING),
    "defaults": mapping(STRING),
    "version": INTEGER,
    "values": SETTINGS_VALUES,
    "roles": array(ROLE_VIEW),
}, ["max_parallel", "parallel_max", "aliases", "models", "defaults"])
NORMAL_ROLE_PATCH = obj({
    "role": ROLE_ENUM,
    "runner": NULL_TEXT,
    "model": NULL_TEXT,
    "effort": NULL_TEXT,
}, ["role"], additional=False)
RESTORE_ROLE_PATCH = obj({
    "role": ROLE_ENUM,
    "restore": ROLE_OVERRIDES,
}, ["role", "restore"], additional=False)
ROLE_PATCH = {"anyOf": [NORMAL_ROLE_PATCH, RESTORE_ROLE_PATCH]}
SETTINGS_PATCH_VALUES = obj({
    "max_parallel": {"type": ["integer", "string"]},
    "models": mapping(STRING),
    "roles": array(ROLE_PATCH),
}, [], additional=False)
SETTINGS_PATCH = obj({
    "base_version": {**INTEGER, "minimum": 1},
    "values": SETTINGS_PATCH_VALUES,
}, ["base_version", "values"], additional=False)
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
                        "view": mapping(JSON)}),
                        obj({"view": STRING, "id": STRING, "project": STRING,
                             "include_archived": {**BOOLEAN, "default": False}}, []))]
PLURALS = {name: name + "s" for name in RESOURCE}
PLURALS.update(research="research", triage="triage")
for _name, _shape in RESOURCE.items():
    _plural = PLURALS[_name]
    _selectors = {"run": STRING} if _name == "step" else {}
    _list_inputs = (
        obj({"view": STRING}, []) if _name == "task"
        else obj({"project": STRING, "include_archived": {**BOOLEAN, "default": False}}, []) if _name == "line"
        else obj({"project": STRING}, []) if _name == "view"
        else obj(_selectors, list(_selectors))
    )
    _get_envelope = (
        envelope({_name: _shape, "steps": array(RUN_STEP_SUMMARY)}, ["seq", _name])
        if _name == "run" else envelope({_name: _shape})
    )
    OPERATIONS += [operation(f"{_name}.list", _plural, envelope({_plural: array(_shape)}),
                             _list_inputs, dict.fromkeys(_selectors, "example")),
                   operation(f"{_name}.get", _plural + "/{id}", _get_envelope,
                             obj({"id": STRING, **_selectors}), {"id": "example", **dict.fromkeys(_selectors, "example")})]
CONFIG_KIND = {"type": "string", "enum": list(configkinds.REGISTRY)}
CONFIG_NAME = {**STRING, "pattern": f"^(?:{configkinds.NAME_PATTERN}|{configkinds.DEFAULTS_LAYER_ADDRESS})$",
               "description": "For step_settings, defaults selects config.toml and defaults@layer selects "
                              "layers/defaults/config.toml."}
CONFIG_ID = {"kind": CONFIG_KIND, "name": CONFIG_NAME}
CONFIG_ITEM = {**CONFIG_ID, "path": STRING}
CONFIG_ITEM_RICH = {**CONFIG_ITEM, "subject": STRING, "version": INTEGER, "content_version": STRING, "level": STRING}
CONFIG_ITEM_SCHEMA = obj(CONFIG_ITEM_RICH, ["kind", "name", "path"])
CONFIG_READ_ITEM_SCHEMA = obj({**CONFIG_ITEM_RICH, "content": {"type": ["string", "object"]}},
                              ["kind", "name", "path", "content"])
CONFIG_KIND_INFO = obj({"kind": CONFIG_KIND, "home": array(STRING), "schema": STRING, "gate": STRING,
                        "verbs": array(STRING)})
CONFIG_VERSION = obj({"version": INTEGER, "content_version": STRING, "commit_sha": STRING, "sha256": STRING,
                      "origin": STRING, "path": STRING, "n": INTEGER, "author": STRING, "source": STRING,
                      "note": STRING, "at": STRING, "live": BOOLEAN})
CONFIG_EXAMPLE = {"kind": "step_settings", "name": "defaults"}
CHECK_STAGE = obj({
    "name": STRING,
    "ok": BOOLEAN,
    "problems": array(STRING),
    "notes": array(STRING),
})
CHECK_REPORT = obj({
    "validator": STRING,
    "ok": BOOLEAN,
    "stages": array(CHECK_STAGE),
    "gate": {**STRING, "enum": ["A", "B", "C"]},
    "exposes": array(STRING),
})
CONFIG_DRAFT = obj({
    "id": STRING,
    "kind": STRING,
    "name": STRING,
    "origin": STRING,
    "path": STRING,
    "base_version": STRING,
    "body": STRING,
    "diff": STRING,
    "author": STRING,
    "model": STRING,
    "instruction": STRING,
    "why": STRING,
    "gate": {**STRING, "enum": ["A", "B", "C"]},
    "check_report": {"anyOf": [CHECK_REPORT, {"type": "null"}]},
    "validator": STRING,
    "state": {**STRING, "enum": ["draft", "valid", "invalid", "active", "declined", "discarded", "superseded"]},
    "decline_reason": STRING,
    "evidence": JSON,
    "activated_commit": STRING,
    "legacy_proposal": NULL_INT,
    "pr_url": NULL_TEXT,
    "version": INTEGER,
    "created": STRING,
    "updated": STRING,
})
CONFIG_BASE_VER = {"type": ["integer", "string"]}
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
    operation("config.kinds", "config", envelope({"kinds": array(CONFIG_KIND_INFO)})),
    operation("config.list", "config/{kind}", envelope({"items": array(CONFIG_ITEM_SCHEMA)}),
              obj({"kind": CONFIG_KIND}), {"kind": "step_settings"}),
    operation("config.read", "config/{kind}/{name}",
              envelope({"item": CONFIG_READ_ITEM_SCHEMA}),
              obj({**CONFIG_ID, "version": STRING, "level": STRING, "from": STRING, "file": STRING}, list(CONFIG_ID)),
              CONFIG_EXAMPLE),
    operation("config.versions", "config/{kind}/{name}/versions",
              envelope({"versions": array(STRING), "entries": array(CONFIG_VERSION)}, required=["seq", "versions"]),
              obj(CONFIG_ID), CONFIG_EXAMPLE),
    operation("config.diff", "config/{kind}/{name}/diff", envelope({"diff": STRING}),
              obj({**CONFIG_ID, "from": STRING, "to": STRING}, list(CONFIG_ID)), CONFIG_EXAMPLE),
    {**operation("config.draft", "config/{kind}/{name}/draft",
                 envelope({"draft": CONFIG_DRAFT, "report": {"anyOf": [CHECK_REPORT, {"type": "null"}]}}, ["seq", "draft"]),
                 obj({**CONFIG_ID, "body": STRING, "base_version": CONFIG_BASE_VER, "why": STRING,
                      "instruction": STRING, "model": STRING, "path": STRING, "check": BOOLEAN},
                     [*CONFIG_ID, "body"]),
                 {**CONFIG_EXAMPLE, "body": "max_parallel = 2\n"}, "PUT",
                 errors=("validation", "invalid_state"), success_status=201),
     "scope": "config:draft"},
    {**operation("config.check", "config/{kind}/{name}/check",
                 envelope({"report": CHECK_REPORT, "draft": {"anyOf": [CONFIG_DRAFT, {"type": "null"}]}}, ["seq", "report"]),
                 obj({**CONFIG_ID, "draft": STRING, "body": STRING}, list(CONFIG_ID)),
                 {**CONFIG_EXAMPLE, "body": "max_parallel = 2\n"}, "POST",
                 errors=("validation", "invalid_state")),
     "scope": "config:draft"},
    {**operation("config.activate", "config/{kind}/{name}/activate",
                 envelope({"draft": CONFIG_DRAFT, "commit": STRING, "version": INTEGER}),
                 obj({**CONFIG_ID, "draft": STRING, "base_version": CONFIG_BASE_VER},
                     [*CONFIG_ID, "base_version"]),
                 {**CONFIG_EXAMPLE, "draft": "CD-1", "base_version": 1}, "POST",
                 errors=("validation", "invalid_state", "gate_refused", "presence_expired")),
     "scope": "config:activate"},
    {**operation("config.rollback", "config/{kind}/{name}/rollback",
                 envelope({"draft": CONFIG_DRAFT, "commit": STRING, "version": INTEGER}),
                 obj({**CONFIG_ID, "to": CONFIG_BASE_VER, "base_version": CONFIG_BASE_VER},
                     [*CONFIG_ID, "to", "base_version"]),
                 {**CONFIG_EXAMPLE, "to": 1, "base_version": 2}, "POST",
                 errors=("validation", "invalid_state", "gate_refused", "presence_expired")),
     "scope": "config:activate"},
    {**operation("config.try", "config/{kind}/{name}/try",
                 envelope({"draft": CONFIG_DRAFT, "ref": STRING, "commit": STRING, "runs": array(STRING)}),
                 obj({**CONFIG_ID, "draft": STRING, "steps": array(JSON)},
                     [*CONFIG_ID, "draft", "steps"]),
                 {**CONFIG_EXAMPLE, "draft": "CD-1", "steps": ["plan"]}, "POST",
                 errors=("validation", "invalid_state", "files_purged"), success_status=201),
     "scope": "config:draft"},
    operation("draft.list", "drafts", envelope({"drafts": array(CONFIG_DRAFT)}),
              obj({"state": STRING, "kind": STRING, "name": STRING, "author": STRING}, [])),
    operation("draft.get", "drafts/{draft}", envelope({"draft": CONFIG_DRAFT}),
              obj({"draft": STRING}), {"draft": "CD-1"}),
    {**operation("draft.discard", "drafts/{draft}/discard", envelope({"draft": CONFIG_DRAFT}),
                 obj({"draft": STRING, "reason": STRING}, ["draft"]),
                 {"draft": "CD-1"}, "POST", errors=("invalid_state",)),
     "scope": "config:draft"},
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
    operation("task.history", "tasks/{id}/history",
              envelope({"comments": array(TASK_COMMENT), "runs": array(RUN), "events": array(INBOX_EVENT)},
                       ["seq", "comments", "runs"]),
              obj({"id": STRING}), {"id": "T-1"}),
    operation("line_stream.read", "line-stream", envelope({"lines": array(FEED_LINE), "after": INTEGER}),
              obj({"run": STRING, "after": STRING}, ["run"]), {"run": "example"}, errors=("files_purged",)),
    operation("settings.get", "settings", envelope({"settings": SETTINGS})),
    operation("settings.resolved", "settings/resolved", RESOLVED_SETTINGS,
              obj({"repo": STRING, "task_kind": STRING, "node": STRING}, [])),
    {**operation("settings.update", "settings", envelope({"settings": SETTINGS}),
                 SETTINGS_PATCH, {"base_version": 1, "values": {"max_parallel": 2}}, "PATCH"),
     "scope": "config:activate"},
    operation("blob.get", "blobs/{sha256}", envelope({"blob": BLOB_DATA}),
              obj({"sha256": BLOB_SHA}), {"sha256": "0" * 64}, errors=("files_purged",)),
    {**operation("blob.put", "blobs/{sha256}", envelope({"blob": BLOB_META}),
                 obj({"sha256": BLOB_SHA}), {"sha256": "0" * 64}, "PUT",
                 errors=("validation",), success_status=201),
     "scope": "intake:write", "body_media": "application/octet-stream"},
    {**operation("loop.enable", "loops/{loop}/enable", envelope({"loop": LOOP}),
                 obj({"loop": STRING}), {"loop": "poll"}, "POST"),
     "scope": "config:activate"},
    {**operation("loop.disable", "loops/{loop}/disable", envelope({"loop": LOOP}),
                 obj({"loop": STRING}), {"loop": "poll"}, "POST"),
     "scope": "config:activate"},
    operation("health.get", "health", envelope({"health": HEALTH})),
    operation("status.get", "status", envelope({"snapshot": mapping(JSON), "loops": array(mapping(JSON)),
              "records": PANEL}), obj({"limit": STRING}, [])),
    operation("task.create", "tasks", envelope({"task": TASK, "run": {"anyOf": [RUN, {"type": "null"}]},
                                                "state": STRING, "why": STRING}, ["seq", "task"]),
              obj({"repo": STRING, "body": STRING, "title": STRING, "kind": STRING, "ticket": STRING,
                   "start": BOOLEAN, "force": BOOLEAN,
                   "project": NULL_TEXT, "line": NULL_TEXT, "priority": NULL_INT, "rank": NULL_NUM,
                   "owner": NULL_TEXT, "delegate": NULL_TEXT, "estimate": NULL_TEXT, "answer": NULL_TEXT}, ["repo"]),
              {"repo": "example", "body": "Work to do"}, "POST"),
    operation("task.update", "tasks/{id}", envelope({"task": TASK}),
              obj({"id": STRING, "base_version": {**INTEGER, "minimum": 1}, "title": STRING, "body": STRING,
                   "kind": STRING, "state": STRING,
                   "project": NULL_TEXT, "line": NULL_TEXT, "priority": NULL_INT, "rank": NULL_NUM,
                   "owner": NULL_TEXT, "delegate": NULL_TEXT, "estimate": NULL_TEXT, "answer": NULL_TEXT},
                  ["id", "base_version"]),
              {"id": "T-1", "title": "Updated title", "base_version": 1}, "PATCH", errors=("task_version_conflict",)),
    operation("task.close", "tasks/{task}/close", envelope({"task": TASK}), obj({"task": STRING}),
              {"task": "T-1"}, "POST", errors=("invalid_state",)),
    operation("task.restore", "tasks/{task}/restore", envelope({"task": TASK}),
              obj({"task": STRING, "base_version": {**INTEGER, "minimum": 1}}, ["task"]),
              {"task": "T-1"}, "POST", errors=("invalid_state", "task_version_conflict")),
    operation("comment.create", "tasks/{task}/comments", envelope({"task": TASK}),
              obj({"task": STRING, "body": STRING}), {"task": "T-1", "body": "Reply"}, "POST"),
    operation("comment.update", "tasks/{task}/comments/{comment}", envelope({"comment": TASK_COMMENT}),
              obj({"task": STRING, "comment": STRING, "base_version": {**INTEGER, "minimum": 1},
                   "body": STRING, "audience": STRING}, ["task", "comment", "base_version"]),
              {"task": "T-1", "comment": "1", "base_version": 1, "body": "Edited reply"}, "PATCH",
              errors=("validation",)),
    operation("task_ref.list", "tasks/{task}/refs", envelope({"refs": array(TASK_REF)}),
              obj({"task": STRING}), {"task": "T-1"}),
    operation("task_ref.link", "tasks/{task}/refs", envelope({"task_ref": TASK_REF, "ref": REF}),
              obj({"task": STRING, "ref": STRING, "system": STRING, "kind": STRING, "key": STRING,
                   "connector": STRING, "title": STRING, "url": STRING,
                   "role": {**STRING, "enum": ["primary", "origin", "related", "parent"]},
                   "interpretation": STRING, "claims": mapping(JSON), "anchor": array(JSON),
                   "source_rev": STRING, "confidence": NULL_NUM}, ["task"]),
              {"task": "T-1", "ref": "slack:C1/1700000000.000100", "role": "related",
               "interpretation": "Source thread"}, "POST", errors=("invalid_state", "validation"),
              success_status=201),
    operation("task_ref.update", "tasks/{task}/refs/{ref}", envelope({"task_ref": TASK_REF, "ref": REF}),
              obj({"task": STRING, "ref": STRING, "base_version": {**INTEGER, "minimum": 1},
                   "role": {**STRING, "enum": ["primary", "origin", "related", "parent"]},
                   "interpretation": STRING, "claims": mapping(JSON), "anchor": array(JSON),
                   "source_rev": STRING, "confidence": NULL_NUM}, ["task", "ref"]),
              {"task": "T-1", "ref": "R-1", "role": "primary"}, "PATCH",
              errors=("invalid_state", "validation")),
    operation("task_ref.unlink", "tasks/{task}/refs/{ref}",
              envelope({"unlinked": BOOLEAN, "task": STRING, "ref": STRING}),
              obj({"task": STRING, "ref": STRING}), {"task": "T-1", "ref": "R-1"}, "DELETE"),
    operation("ref.list", "refs", envelope({"refs": array(REF)}),
              obj({"system": STRING, "kind": STRING, "key": STRING, "sync": STRING}, [])),
    operation("ref.get", "refs/{ref}", envelope({"ref": REF, "tasks": array(REF_TASK_LINK)}),
              obj({"ref": STRING}), {"ref": "R-1"}),
    operation("ref.history", "refs/{ref}/history", envelope({"events": array(INBOX_EVENT)}),
              obj({"ref": STRING}), {"ref": "R-1"}),
    operation("task_link.list", "tasks/{task}/links", envelope({"links": array(TASK_LINK)}),
              obj({"task": STRING}), {"task": "T-1"}),
    operation("task_link.create", "tasks/{task}/links", envelope({"link": TASK_LINK}),
              obj({"task": STRING, "kind": {**STRING, "enum": ["waits_on", "blocks", "relates", "duplicate"]},
                   "other_task": STRING, "other_key": STRING,
                   "origin": {**STRING, "enum": ["person", "agent", "body", "jira_link"]}}, ["task", "kind"]),
              {"task": "T-1", "kind": "relates", "other_key": "github:o/r#7"}, "POST",
              errors=("validation",), success_status=201),
    operation("task_link.delete", "tasks/{task}/links/{link}", envelope({"deleted": BOOLEAN, "id": INTEGER}),
              obj({"task": STRING, "link": STRING}), {"task": "T-1", "link": "1"}, "DELETE"),
    operation("run.stop", "runs/{run}/stop", envelope({"id": STRING, "stopped": BOOLEAN, "pid": INTEGER,
              "was": STRING, "ticket": STRING}), obj({"run": STRING, "base_version": {**INTEGER, "minimum": 1}}),
              {"run": "pip-demo-1", "base_version": 1}, "POST"),
    operation("run.steer", "runs/{run}/steer", envelope({"id": STRING, "queued": BOOLEAN}),
              obj({"run": STRING, "text": STRING}), {"run": "session-demo-1", "text": "Continue"}, "POST"),
    operation("run.pin", "runs/{run}/pin", envelope({"id": STRING}), obj({"run": STRING}),
              {"run": "pip-demo-1"}, "POST", errors=("files_purged",)),
    operation("run.unpin", "runs/{run}/unpin", envelope({"id": STRING}), obj({"run": STRING}),
              {"run": "pip-demo-1"}, "POST"),
    operation("chat.drop", "chats/{chat}", envelope({"chat": mapping(JSON)}), obj({"chat": STRING}),
              {"chat": "1"}, "DELETE"),
    {**operation("chat.file", "chats/{chat}/file", envelope({"item": OUTBOX_ITEM}),
                 obj({"chat": STRING, "repo": STRING, "title": STRING, "draft": STRING}, ["chat"]),
                 {"chat": "1"}, "POST", errors=("invalid_state", "validation"), success_status=201),
     "scope": "task:write"},
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
    operation("inbox.list", "inbox", envelope({"items": array(INBOX_ITEM)}),
              obj({"state": STRING, "kind": STRING, "priority": STRING, "task": STRING, "run": STRING, "line": STRING}, [])),
    {**operation("inbox.raise", "inbox", envelope({"item": INBOX_ITEM}),
                 obj({"kind": STRING, "subject": STRING, "title": STRING, "priority": {**INTEGER, "minimum": 1, "maximum": 3},
                      "detail": mapping(JSON), "verbs": array(STRING), "options": array(JSON),
                      "task_id": STRING, "run_id": STRING, "line": STRING, "ask": mapping(JSON)}, []),
                 {"kind": "alert", "subject": "alert:example", "title": "Example alert", "priority": 2, "verbs": ["continue"]},
                 "POST", errors=("validation",), success_status=201), "scope": "inbox:raise"},
    operation("inbox.get", "inbox/{item}", envelope({"item": INBOX_ITEM, "subject_object": JSON}),
              obj({"item": STRING}), {"item": "TR-1"}),
    {**operation("inbox.snooze", "inbox/{item}/snooze", envelope({"item": INBOX_ITEM}),
                 obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}, "until": STRING, "until_changed": BOOLEAN},
                     ["item", "base_version"]),
                 {"item": "TR-1", "base_version": 1, "until": "2099-01-01T00:00:00Z"},
                 "POST", errors=("already_resolved", "invalid_state", "validation")), "scope": "inbox:resolve"},
    {**operation("inbox.shelve", "inbox/{item}/shelve", envelope({"item": INBOX_ITEM}),
                 obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}, "reason": STRING, "until": STRING},
                     ["item", "base_version", "reason"]),
                 {"item": "TR-1", "base_version": 1, "reason": "Later"},
                 "POST", errors=("already_resolved", "invalid_state", "validation")), "scope": "inbox:resolve"},
    {**operation("inbox.resolve", "inbox/{item}/resolve", envelope({"item": INBOX_ITEM, "result": JSON}),
                 obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}, "verb": STRING,
                      "option": JSON, "answer": STRING, "answers": array(JSON), "reason": STRING,
                      "fix": STRING, "prevent": STRING},
                     ["item", "base_version", "verb"]),
                 {"item": "TR-1", "base_version": 1, "verb": "continue"},
                 "POST", errors=("already_resolved", "invalid_state", "validation", "gate_refused", "presence_expired")),
     "scope": "inbox:resolve"},
    {**operation("inbox.retry", "inbox/{item}/retry", envelope({"item": INBOX_ITEM, "result": JSON}),
                 obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}}, ["item"]),
                 {"item": "TR-1"},
                 "POST", errors=("already_resolved", "invalid_state", "validation", "still_missing")),
     "scope": "inbox:resolve"},
    {**operation("inbox.upload_evidence", "inbox/{item}/evidence",
                 envelope({"item": INBOX_ITEM, "evidence": mapping(JSON), "result": JSON}),
                 obj({"item": STRING}, ["item"]), {"item": "TR-1"}, "POST",
                 errors=("already_resolved", "invalid_state", "validation", "body_too_large")),
     "scope": "inbox:resolve", "body_media": "application/octet-stream"},
    operation("inbox.history", "inbox/{item}/history", envelope({"events": array(INBOX_EVENT)}),
              obj({"item": STRING}), {"item": "TR-1"}),
    operation("triage_screen.get", "screens/triage",
              envelope({"items": array(INBOX_ITEM),
                        "by_priority": obj({"high": array(INBOX_ITEM), "med": array(INBOX_ITEM), "low": array(INBOX_ITEM)}),
                        "counts": mapping(INTEGER)})),
]
DEVICE_PROBE = obj({
    "at": STRING,
    "ok": BOOLEAN,
    "seconds": NUMBER,
    "device_version": STRING,
    "system": STRING,
    "home": STRING,
    "tools": array(STRING),
    "gpus": array(STRING),
    "detail": STRING,
}, ["at", "ok", "seconds", "device_version", "detail"])
DEVICE = obj({
    "id": STRING,
    "subject": STRING,
    "version": STRING,
    "kind": {**STRING, "enum": ["local", "ssh", "exe", "proxmox"]},
    "pool": NULL_TEXT,
    "status": {**STRING, "enum": ["ok", "fault", "unprobed"]},
    "last_probe": {**DEVICE_PROBE, "type": ["object", "null"]},
    "leases": array(STRING),
    "runners": array(STRING),
    "spec": NULL_TEXT,
    "tools": {**array(STRING), "type": ["array", "null"]},
    "host": STRING,
    "note": STRING,
})
DEVICE_PROBE_RESULT = {"anyOf": [
    envelope({"device": DEVICE, "probe": DEVICE_PROBE}),
    envelope({"accepted": BOOLEAN, "watch": STRING, "device": STRING}),
]}
LEASE = obj({
    "id": STRING,
    "subject": STRING,
    "run_id": NULL_TEXT,
    "device": STRING,
    "device_version": STRING,
    "epoch": INTEGER,
    "created": STRING,
    "renewed": STRING,
    "expires": STRING,
    "released": NULL_TEXT,
    "outcome": STRING,
    "kept_reason": STRING,
    "vm_name": STRING,
    "vmid": {"type": ["integer", "null"]},
    "ready": STRING,
    "address": STRING,
    "host_key_fingerprint": STRING,
    "sweep_attempts": INTEGER,
    "last_sweep": STRING,
    "last_sweep_error": STRING,
    "version": INTEGER,
    "streams": array(mapping(JSON)),
})
RUN_SANDBOX = envelope({
    "device": STRING,
    "lease": NULL_TEXT,
    "address": STRING,
    "facts": {**mapping(JSON), "type": ["object", "null"]},
})

OPERATIONS += [
    operation("device.list", "devices", envelope({"devices": array(DEVICE)})),
    operation("device.get", "devices/{device}", envelope({"device": DEVICE}),
              obj({"device": STRING}), {"device": "local"}),
    {**operation("device.probe", "devices/{device}/probe", DEVICE_PROBE_RESULT,
                 obj({"device": STRING}), {"device": "local"}, "POST"),
     "scope": "run:start"},
    operation("lease.list", "leases", envelope({"leases": array(LEASE)}),
              obj({"run": STRING, "device": STRING, "open": STRING}, [])),
    operation("lease.get", "leases/{lease}", envelope({"lease": LEASE}),
              obj({"lease": STRING}), {"lease": "L-1"}, errors=("lease_fenced", "lease_expired")),
    operation("run.sandbox", "runs/{run}/sandbox", RUN_SANDBOX,
              obj({"run": STRING}), {"run": "pip-demo-1"}),
    operation("push.list", "push/subscriptions", envelope({"subscriptions": array(PUSH_SUBSCRIPTION)})),
    operation("push.subscribe", "push/subscriptions", envelope({"subscription": PUSH_SUBSCRIPTION}), PUSH_SUBSCRIBE,
              {"endpoint": "http://127.0.0.1:9999/push", "keys": {"p256dh": "examplekey", "auth": "exampleauth"}, "label": "Phone"},
              "POST", errors=("validation",), success_status=201),
    operation("push.remove", "push/subscriptions/{subscription}", envelope({"id": STRING, "removed": BOOLEAN}),
              obj({"subscription": STRING}), {"subscription": "PS-1"}, "DELETE"),
    operation("outbox.list", "outbox", envelope({"items": array(OUTBOX_ITEM)}),
              obj({"state": STRING, "task": STRING, "run": STRING}, [])),
    operation("outbox.get", "outbox/{item}",
              envelope({"item": OUTBOX_ITEM, "revisions": array(OUTBOX_REVISION),
                        "redactions": array(OUTBOX_REDACTION), "attempts": array(DELIVERY_ATTEMPT),
                        "sending_since": NULL_TEXT}),
              obj({"item": STRING}), {"item": "OB-1"}),
    operation("outbox.edit", "outbox/{item}", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1},
                   "base_rev": {**INTEGER, "minimum": 1}, "body": STRING}),
              {"item": "OB-1", "base_version": 1, "base_rev": 1, "body": "Edited body"}, "PATCH",
              errors=("invalid_state", "validation")),
    operation("outbox.preview", "outbox/{item}/preview",
              envelope({"item": STRING, "rev": INTEGER, "marker": NULL_TEXT, "body": STRING}),
              obj({"item": STRING}), {"item": "OB-1"}),
    operation("outbox.open", "outbox/{item}/open", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}}),
              {"item": "OB-1", "base_version": 1}, "POST", errors=("invalid_state",)),
    operation("outbox.approve", "outbox/{item}/approve", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1},
                   "rev": {**INTEGER, "minimum": 1}}),
              {"item": "OB-1", "base_version": 1, "rev": 1}, "POST",
              errors=("invalid_state", "validation", "gate_refused", "presence_expired")),
    operation("outbox.undo", "outbox/{item}/undo", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}}),
              {"item": "OB-1", "base_version": 1}, "POST", errors=("invalid_state",)),
    operation("outbox.hold", "outbox/{item}/hold", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}}),
              {"item": "OB-1", "base_version": 1}, "POST", errors=("invalid_state",)),
    operation("outbox.release", "outbox/{item}/release", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}}),
              {"item": "OB-1", "base_version": 1}, "POST", errors=("invalid_state",)),
    operation("outbox.reject", "outbox/{item}/reject", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}, "reason": STRING},
                  ["item", "base_version"]),
              {"item": "OB-1", "base_version": 1, "reason": "Not needed"}, "POST",
              errors=("invalid_state", "validation", "gate_refused", "presence_expired")),
    operation("outbox.retry", "outbox/{item}/retry", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}}),
              {"item": "OB-1", "base_version": 1}, "POST",
              errors=("invalid_state", "gate_refused", "presence_expired")),
    operation("outbox.discard", "outbox/{item}/discard", envelope({"item": OUTBOX_ITEM}),
              obj({"item": STRING, "base_version": {**INTEGER, "minimum": 1}}),
              {"item": "OB-1", "base_version": 1}, "POST", errors=("invalid_state",)),
    operation("outbox.history", "outbox/{item}/history", envelope({"events": array(INBOX_EVENT)}),
              obj({"item": STRING}), {"item": "OB-1"}),
    {**operation("triage.send", "triage/{ticket}/send", envelope({"data": OUTBOX_ITEM}),
                 obj({"ticket": STRING, "base_version": {**INTEGER, "minimum": 1},
                      "questions": array(STRING), "answers": array(STRING), "repo": STRING},
                     ["ticket", "base_version", "questions", "answers"]),
                 {"ticket": "APP-7", "base_version": 1, "questions": [], "answers": [], "repo": "demo"},
                 "POST", errors=("invalid_state", "validation"), success_status=201),
     "scope": "task:write"},
]
TURN = obj({
    "step_id": INTEGER,
    "n": INTEGER,
    "run_id": STRING,
    "at": STRING,
    "status": STRING,
    "tool": STRING,
    "excerpt": STRING,
    "seconds": NUMBER,
    "tokens_in": INTEGER,
    "cache_read": INTEGER,
    "tokens_out": INTEGER,
    "cost_nusd": NULL_INT,
    "file": STRING,
    "member_offset": INTEGER,
    "member_len": INTEGER,
})
TURN_DETAIL = obj({
    **TURN["properties"],
    "raw": STRING,
    "lines": array(mapping(JSON)),
    "events": array(mapping(JSON)),
})
NULL_RUN = {"anyOf": [RUN, {"type": "null"}]}
EVENTS_INPUT = {"anyOf": [array(mapping(JSON)), STRING]}
OPERATIONS += [
    operation("turn.list", "runs/{run}/steps/{node}/{try}/turns",
              envelope({"turns": array(TURN)}),
              obj({"run": STRING, "node": STRING, "try": STRING}),
              {"run": "pip-demo-1", "node": "plan", "try": "1"}, errors=("files_purged",)),
    operation("turn.get", "runs/{run}/steps/{node}/{try}/turns/{n}",
              envelope({"turn": TURN_DETAIL}),
              obj({"run": STRING, "node": STRING, "try": STRING, "n": STRING}),
              {"run": "pip-demo-1", "node": "plan", "try": "1", "n": "1"}, errors=("files_purged",)),
    operation("task.run", "tasks/{task}/run",
              envelope({"run": NULL_RUN, "task": STRING, "repo": STRING, "state": STRING, "why": STRING}, ["seq", "run"]),
              obj({"task": STRING, "repo": STRING, "force": BOOLEAN}, ["task"]),
              {"task": "t-a1b2"}, "POST", success_status=201),
    operation("session.start", "sessions",
              envelope({"run": RUN, "session": mapping(JSON)}, ["seq", "run"]),
              obj({"prompt": STRING, "device": STRING, "model": STRING, "runner": STRING,
                   "tools": {"anyOf": [array(STRING), {"type": "null"}]}, "repo": STRING,
                   "budget_usd": {"type": ["number", "null"]}, "task": BOOLEAN, "draft_pr": BOOLEAN}, ["prompt"]),
              {"prompt": "Check the build"}, "POST", success_status=201),
    operation("run.resume", "runs/{run}/resume",
              envelope({"run": RUN}, ["seq", "run"]),
              obj({"run": STRING, "step": STRING, "node": STRING, "base_version": {**INTEGER, "minimum": 1}}, ["run"]),
              {"run": "pip-demo-1"}, "POST", errors=("files_purged",), success_status=201),
    operation("run.repair", "runs/{run}/repair",
              envelope({"run": RUN}, ["seq", "run"]),
              obj({"run": STRING, "base_version": {**INTEGER, "minimum": 1}}, ["run"]),
              {"run": "pip-demo-1"}, "POST", errors=("files_purged",), success_status=201),
    operation("run.step", "runs/{run}/step",
              envelope({"run": RUN}, ["seq", "run"]),
              obj({"run": STRING, "node": STRING, "set": mapping(JSON),
                   "base_version": {**INTEGER, "minimum": 1}}, ["run", "node"]),
              {"run": "pip-demo-1", "node": "plan"}, "POST", errors=("files_purged",), success_status=201),
    operation("run.import", "runs",
              envelope({"run": RUN}, ["seq", "run"]),
              obj({"kind": STRING, "repo": STRING, "issue": {"type": ["integer", "string", "null"]},
                   "summary": STRING, "branch": STRING, "pr_url": STRING}, ["kind"]),
              {"kind": "import", "summary": "Imported run"}, "POST", success_status=201),
    operation("loop.start", "loops/{loop}/start",
              envelope({"run": RUN}, ["seq", "run"]),
              obj({"loop": STRING, "events": EVENTS_INPUT}, ["loop"]),
              {"loop": "poll"}, "POST", success_status=201),
    operation("loop.try", "loops/{loop}/try",
              envelope({"run": RUN}, ["seq", "run"]),
              obj({"loop": STRING, "events": EVENTS_INPUT}, ["loop"]),
              {"loop": "poll"}, "POST", success_status=201),
    operation("loop.draft", "loops",
              envelope({"run": RUN}, ["seq", "run"]),
              obj({"sentence": STRING, "name": STRING, "repo": STRING, "model": STRING}, ["sentence"]),
              {"sentence": "Watch the build logs"}, "POST", success_status=201),
    operation("chat.create", "chats",
              envelope({"chat": CHAT, "run": NULL_RUN}, ["seq", "chat"]),
              obj({"project": STRING, "text": STRING}, []),
              {"project": ""}, "POST", success_status=201),
    operation("chat.send", "chats/{chat}/messages",
              envelope({"chat": CHAT, "run": NULL_RUN}, ["seq", "chat", "run"]),
              obj({"chat": STRING, "text": STRING}, ["chat", "text"]),
              {"chat": "1", "text": "Hello"}, "POST", success_status=201),
    operation("chat.session_start", "chats/{chat}/sessions",
              envelope({"run": RUN, "session": mapping(JSON)}, ["seq", "run"]),
              obj({"chat": STRING, "prompt": STRING, "repo": NULL_TEXT, "device": STRING, "model": STRING,
                   "runner": STRING, "budget_usd": {"type": ["number", "null"]}}, ["chat", "prompt"]),
              {"chat": "1", "prompt": "Inspect tests"}, "POST", success_status=201),
    operation("triage.refresh", "triage/refresh",
              envelope({"ok": BOOLEAN, "code": INTEGER, "run": NULL_TEXT}, ["seq", "ok"]),
              obj({"dry_run": BOOLEAN}, []),
              {}, "POST"),
    operation("triage.research", "triage/{ticket}/research",
              envelope({"ref": STRING, "code": INTEGER, "run": NULL_TEXT}, ["seq", "ref"]),
              obj({"ticket": STRING, "force": BOOLEAN, "dry_run": BOOLEAN, "next": BOOLEAN}, ["ticket"]),
              {"ticket": "demo#7"}, "POST"),
    operation("health.check", "health/check",
              envelope({"ok": BOOLEAN, "code": INTEGER}, ["seq", "ok"]),
              obj({"dry_run": BOOLEAN, "force_probes": BOOLEAN, "quiet": BOOLEAN}, []),
              {}, "POST"),
    operation("incident.scan", "incidents/scan",
              envelope({"summary": {"anyOf": [mapping(JSON), {"type": "null"}]}}, ["seq", "summary"]),
              obj({}, []),
              {}, "POST"),
]


def command_result(data_schema, extra=None, extra_required=()):
    props = {
        "data": data_schema,
        "event_seq": NULL_TEXT,
        "subject": STRING,
        "ver": INTEGER,
        "vers": mapping(INTEGER),
        "replayed": BOOLEAN,
        **(extra or {}),
    }
    req = ["seq", "data", "event_seq", "subject", "ver", "vers", "replayed", *extra_required]
    return envelope(props, req)


ENTITY_HISTORY_EVENT = obj({
    "seq": INTEGER,
    "id": STRING,
    "at": STRING,
    "actor": STRING,
    "actor_name": NULL_TEXT,
    "ver": NULL_INT,
    "data": mapping(JSON),
}, ["seq", "at", "actor", "ver", "data"])
BOOL_OR_INT = {"type": ["boolean", "integer"]}
BASE_VERSION = {**INTEGER, "minimum": 1}
SEQ_POS = {**INTEGER, "minimum": 1}

OPERATIONS += [
    operation(
        "project.create", "projects", command_result(PROJECT),
        obj({"id": STRING, "name": STRING, "repo": STRING, "priority": NULL_INT, "rank": NULL_NUM,
             "archived": BOOL_OR_INT}, ["id", "name"]),
        {"id": "demo-project", "name": "Demo Project"}, "POST",
        errors=("validation",), success_status=201,
    ),
    operation(
        "project.update", "projects/{project}", command_result(PROJECT),
        obj({"project": STRING, "base_version": BASE_VERSION, "name": STRING, "repo": STRING,
             "priority": NULL_INT, "rank": NULL_NUM, "archived": BOOL_OR_INT}, ["project", "base_version"]),
        {"project": "replay-sample", "base_version": 1, "name": "Replay Sample"}, "PATCH",
        errors=("validation",),
    ),
    operation(
        "project.restore", "projects/{project}/restore", command_result(PROJECT),
        obj({"project": STRING, "seq": SEQ_POS, "base_version": BASE_VERSION}, ["project", "seq", "base_version"]),
        {"project": "replay-sample", "seq": 1, "base_version": 2}, "POST",
        errors=("validation",),
    ),
    operation(
        "project.history", "projects/{project}/history",
        envelope({"events": array(ENTITY_HISTORY_EVENT), "next_after": NULL_INT}, ["seq", "events"]),
        obj({"project": STRING, "limit": STRING, "after": STRING}, ["project"]),
        {"project": "replay-sample"},
    ),
    operation(
        "line.create", "lines", command_result(LINE),
        obj({"id": STRING, "project": STRING, "name": STRING, "kind": STRING, "repo": STRING,
             "adw_route": STRING, "device_pool": array(STRING), "rank": NULL_NUM,
             "concurrency": INTEGER, "wip_limit": NULL_INT, "priority": NULL_INT,
             "paused": BOOL_OR_INT, "paused_why": STRING, "band": mapping(JSON)},
            ["id", "project", "name", "kind", "repo"]),
        {"id": "demo-secondary", "project": "replay-sample", "name": "Secondary", "kind": "pipeline",
         "repo": "replay-sample"}, "POST",
        errors=("validation",), success_status=201,
    ),
    operation(
        "line.update", "lines/{line}", command_result(LINE),
        obj({"line": STRING, "base_version": BASE_VERSION, "project": STRING, "name": STRING,
             "kind": STRING, "repo": STRING, "adw_route": STRING, "device_pool": array(STRING),
             "rank": NULL_NUM, "concurrency": INTEGER, "wip_limit": NULL_INT, "priority": NULL_INT,
             "paused": BOOL_OR_INT, "paused_why": STRING, "band": mapping(JSON)},
            ["line", "base_version"]),
        {"line": "replay-sample-pipeline", "base_version": 1, "wip_limit": 3}, "PATCH",
        errors=("validation", "invalid_state"),
    ),
    operation(
        "line.restore", "lines/{line}/restore", command_result(LINE),
        obj({"line": STRING, "seq": SEQ_POS, "base_version": BASE_VERSION}, ["line", "seq", "base_version"]),
        {"line": "replay-sample-pipeline", "seq": 1, "base_version": 2}, "POST",
        errors=("validation",),
    ),
    operation(
        "line.history", "lines/{line}/history",
        envelope({"events": array(ENTITY_HISTORY_EVENT), "next_after": NULL_INT}, ["seq", "events"]),
        obj({"line": STRING, "limit": STRING, "after": STRING}, ["line"]),
        {"line": "replay-sample-pipeline"},
    ),
    operation(
        "line.delete", "lines/{line}",
        command_result(LINE, {"pre_removal_seq": NULL_INT}, ("pre_removal_seq",)),
        obj({"line": STRING, "base_version": BASE_VERSION}, ["line", "base_version"]),
        {"line": "demo-secondary", "base_version": 1}, "DELETE",
        errors=("validation", "line_in_use", "incompatible_writers"),
    ),
    operation(
        "view.create", "views", command_result(VIEW),
        obj({"id": STRING, "name": STRING, "project": NULL_TEXT, "filter": STRING,
             "group_by": STRING, "sort": STRING, "layout": STRING, "columns": array(STRING),
             "rank": NULL_NUM}, ["id", "name"]),
        {"id": "ready-view", "name": "Ready", "filter": "state = ready"}, "POST",
        errors=("validation",), success_status=201,
    ),
    operation(
        "view.update", "views/{view}", command_result(VIEW),
        obj({"view": STRING, "base_version": BASE_VERSION, "name": STRING, "project": NULL_TEXT,
             "filter": STRING, "group_by": STRING, "sort": STRING, "layout": STRING,
             "columns": array(STRING), "rank": NULL_NUM}, ["view", "base_version"]),
        {"view": "ready-view", "base_version": 1, "name": "Ready Tasks"}, "PATCH",
        errors=("validation",),
    ),
    operation(
        "view.restore", "views/{view}/restore", command_result(VIEW),
        obj({"view": STRING, "seq": SEQ_POS, "base_version": BASE_VERSION}, ["view", "seq", "base_version"]),
        {"view": "ready-view", "seq": 1, "base_version": 2}, "POST",
        errors=("validation",),
    ),
    operation(
        "view.history", "views/{view}/history",
        envelope({"events": array(ENTITY_HISTORY_EVENT), "next_after": NULL_INT}, ["seq", "events"]),
        obj({"view": STRING, "limit": STRING, "after": STRING}, ["view"]),
        {"view": "ready-view"},
    ),
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


_MAX_VALIDATION_DEPTH = 64


def unknown(value, schema, path="", _depth=0):
    if _depth > _MAX_VALIDATION_DEPTH:
        raise Problem("invalid_json", f"{path or 'input'} exceeds maximum nesting depth")
    found = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            at = f"{path}.{key}" if path else key
            if key in properties:
                found.extend(unknown(item, properties[key], at, _depth + 1))
            elif extra is False:
                found.append(at)
            elif isinstance(extra, dict):
                found.extend(unknown(item, extra, at, _depth + 1))
    elif isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            found.extend(unknown(item, schema["items"], f"{path}[{i}]", _depth + 1))
    return sorted(found)


def reject_unknown(value, schema, path=""):
    if fields := unknown(value, schema, path):
        raise Problem("unknown_field", "Undeclared fields: " + ", ".join(fields), fields=fields)


def validate(value, schema, path="input", _depth=0):
    if _depth > _MAX_VALIDATION_DEPTH:
        raise Problem("invalid_json", f"{path} exceeds maximum nesting depth")
    if "anyOf" in schema:
        for candidate in schema["anyOf"]:
            try:
                validate(value, candidate, path, _depth + 1)
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
    if kind == "number" and not math.isfinite(value):
        raise Problem("invalid_input", f"{path} must be a finite number")
    if "enum" in schema and value not in schema["enum"]:
        raise Problem("invalid_input", f"{path} is not a declared value")
    if isinstance(value, str) and "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        raise Problem("invalid_input", f"{path} has an invalid format")
    if kind in ("integer", "number") and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
        raise Problem("invalid_input", f"{path} is outside its allowed range")
    if isinstance(value, (list, tuple)) and not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
        raise Problem("invalid_input", f"{path} has an invalid number of items")
    if isinstance(value, dict):
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise Problem("invalid_input", f"{path} has too many entries")
        if missing := set(schema.get("required", [])) - value.keys():
            raise Problem("invalid_input", f"{path} needs {', '.join(sorted(missing))}")
        for key, item in value.items():
            child = schema.get("properties", {}).get(key, schema.get("additionalProperties"))
            if isinstance(child, dict):
                validate(item, child, f"{path}.{key}", _depth + 1)
    if isinstance(value, (list, tuple)) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]", _depth + 1)


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
    "post_settings": obj({"max_parallel": JSON,
                          "models": {**mapping(STRING), "type": ["object", "null"]},
                          "base_version": INTEGER}, []),
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
DEPRECATED_ALIASES = frozenset((
    ("POST", "/api/skipped/*"),
    ("POST", "/api/tasks"),
    ("POST", "/api/sessions"),
    ("POST", "/api/runs/*"),
    ("POST", "/api/asks/*"),
    ("POST", "/api/loops/new"),
    ("POST", "/api/loops/*"),
    ("POST", "/api/chats/new"),
    ("POST", "/api/chats/*"),
    ("POST", "/api/triage/refresh"),
    ("POST", "/api/triage/*"),
    ("POST", "/api/checks/run"),
))
ALIASES.extend({"method": method, "path": path, "provider": provider, "adapter": "frozen-v0",
                "input": alias_input(provider, "") if method == "POST" else obj({}),
                "actions": {action: alias_input(provider, "id/" + action) for action in ALIAS_VARIANTS.get(provider, ())},
                **({"deprecated": True} if (method, path) in DEPRECATED_ALIASES else {})}
               for method, path, provider in ALIAS_ROUTES)
ALIASES[:0] = [{"method": method, "path": path, "provider": provider, "adapter": "identity", "transport": True,
               "input": mapping(STRING) if method == "POST" else obj({})} for method, path, provider in (
    ("POST", "/api/capture/evidence", "stage_evidence"),
    ("GET", "/api/capture/evidence/*", "send_evidence"),
    ("GET", "/api/runs/*/events", "send_events"),
    ("GET", "/api/events", "event_stream.serve"),
)]


def complete_examples():
    instances = {"task": "T-1", "project": "replay-sample", "line": "replay-sample-pipeline", "view": "ready-view",
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
