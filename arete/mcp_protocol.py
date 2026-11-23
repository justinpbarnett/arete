from __future__ import annotations

import json
import re
from html import escape

from . import contract

PATH = "/api/v1/mcp"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"
ROTATION_HEADER = "X-Arete-Rotated-From"
ROTATION_ACCEPTED_HEADER = "X-Arete-Rotation-Accepted"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,48}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_:.@/#-]{1,160}")
SELECTORS = {"id", "run", "run_id", "repo", "project", "issue", "ref", "seq", "state", "status", "kind",
             "created", "updated", "started", "finished"}
PROBLEM_METADATA = {"code", "type", "title", "status"}
PROBLEM_FIELDS = {"detail", "error", "fields", "kept", "reason", "required_version", "retry_after", "current"}


def request(value):
    if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
            or not isinstance(value.get("method"), str) or not value["method"]
            or "id" in value and type(value["id"]) not in (int, str)):
        return None
    return value["method"], value.get("id"), value.get("params", {})


def reply(identity, result):
    return {"jsonrpc": "2.0", "id": identity, "result": result}


def error(identity, code, message, data=None):
    options = {} if data is None else {"data": data}
    return {"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message, **options}}


def marked(text):
    return f"<untrusted>{escape(text, quote=False)}</untrusted>"


def untrusted(value, schema=None, field=None, *, selectors=True):
    schema = schema or {}
    if isinstance(value, str):
        if selectors and schema and field in SELECTORS and IDENTIFIER.fullmatch(value):
            return value
        return marked(value)
    if isinstance(value, (list, tuple)):
        return [untrusted(item, schema.get("items"), selectors=selectors) for item in value]
    if isinstance(value, dict):
        fields = schema.get("properties", {})
        return {key if key in fields else marked(str(key)): untrusted(item, fields.get(key), key, selectors=selectors)
                for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return marked(str(value))


def protect_problem(data):
    current = {"task_version_conflict": contract.TASK, "version_conflict": contract.RUN}.get(data["code"])
    details = {key: value for key, value in data.items() if key not in PROBLEM_METADATA}
    fields = {**dict.fromkeys(PROBLEM_FIELDS, {}), "current": current}
    return {**untrusted(details, {"properties": fields}, selectors=False),
            **{key: value for key, value in data.items() if key in PROBLEM_METADATA}}


def tool_result(data, *, failed=False):
    result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
    return {**result, "isError": True} if failed else result
