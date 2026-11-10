from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from datetime import UTC, datetime

from . import paths

BASH_CLASSES = [
    ("missing_tool", r"command not found"),
    ("missing_file", r"No such file or directory"),
    ("permission_denied", r"permission denied|operation not permitted"),
    ("missing_dependency", r"ModuleNotFoundError|ImportError|No module named|Cannot find module"),
    ("sqlite", r"sqlite3|OperationalError|database is locked"),
    ("network", r"ConnectionError|ETIMEDOUT|ConnectionRefused|ECONNREFUSED|Could not resolve host"),
    ("syntax", r"SyntaxError|IndentationError|parse error"),
    ("test_failure", r"(?-i:\bFAILED\b)|\d+ failed|AssertionError|Tests?:\s+\d+ failed"),
    ("type_error", r"error TS\d+|TypeError"),
    ("timeout", r"timed out|timeout"),
]
EDIT_CLASSES = [
    ("match_not_found", r"not found|not unique|no match|String to replace"),
    ("not_read", r"has not been read|Read it first|must read"),
    ("permission_denied", r"permission"),
]
STOP_CLASSES = [
    ("rate_limit", r"rate.?limit|\b429\b|usage limit"),
    ("auth", r"auth|\b40[13]\b|unauthori[sz]ed|forbidden|login"),
    ("overloaded", r"overload|capacity|\b529\b"),
    ("timeout", r"timeout|timed out"),
    ("context", r"context|too long|prompt is too long"),
]
DENIED = re.compile(r"doesn't want|declined|not allowed|permission to use\b|has been denied|denied by your permission",
                    re.IGNORECASE)


def classify(text: str, table: list[tuple[str, str]], default: str) -> str:
    for name, pat in table:
        if re.search(pat, text, re.IGNORECASE):
            return name
    return default


def _short(v, n=300) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        v = json.dumps(v, default=str)
    v = v.strip()
    return v if len(v) <= n else v[:n] + "..."


def build_event(p: dict) -> dict | None:
    hook = p.get("hook_event_name", "")
    tool = p.get("tool_name", "")
    ti = p.get("tool_input") or {}
    ev = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hook": hook,
        "tool": tool,
        "session": p.get("session_id", ""),
        "cwd": p.get("cwd") or os.getcwd(),
        "agent": os.environ.get("ARETE_AGENT") == "1",
    }

    if hook == "PostToolUse":
        if tool == "Skill":
            ev.update(kind="skill", cls="invoked", detail=_short(ti.get("skill") or ti.get("command"), 120))
        elif tool in ("Agent", "Task"):
            ev.update(kind="agent", cls="spawned",
                      detail=_short(f"{ti.get('subagent_type', 'general-purpose')}: {ti.get('description', '')}", 160))
        elif tool == "Bash":
            resp = p.get("tool_response") or {}
            if isinstance(resp, dict) and resp.get("interrupted"):
                ev.update(kind="bash", cls="interrupted", detail=_short(ti.get("command"), 200))
            else:
                return None
        else:
            return None
        return ev

    if hook == "PostToolUseFailure":
        err = _short(p.get("error"), 400)
        if p.get("is_interrupt"):
            cls = "interrupted"
        elif DENIED.search(err):
            cls = "denied"
        elif tool == "Bash":
            cls = classify(err, BASH_CLASSES, "command_failure")
        elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            cls = classify(err, EDIT_CLASSES, "failure")
        elif tool == "Read":
            cls = "missing_file" if re.search(r"not exist|no such|not found", err, re.IGNORECASE) else "failure"
        elif tool == "Skill":
            cls = "failure"
        else:
            cls = "failure"
        detail = ti.get("command") or ti.get("file_path") or ti.get("skill") or ti.get("pattern") or ti.get("url") or ""
        ev.update(kind=(tool or "tool").lower(), cls=cls, detail=_short(detail, 200), error=err)
        return ev

    if hook == "StopFailure":
        err = _short(p.get("error"), 300)
        details = _short(p.get("error_details"), 300)
        ev.update(kind="session", cls=classify(f"{err} {details}", STOP_CLASSES, "stop_failure"), error=err, detail=details)
        return ev

    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        ev = build_event(payload)
        if ev:
            paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(paths.EVENTS_FILE, "a") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception as e:
        with contextlib.suppress(OSError):
            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(paths.LOG_DIR / "hook-errors.log", "a") as f:
                f.write(f"{datetime.now().isoformat()} {type(e).__name__}: {e}\n")
    return 0


def read_events(since_hours: float | None = None) -> list[dict]:
    out = []
    if not paths.EVENTS_FILE.exists():
        return out
    cutoff = None
    if since_hours is not None:
        cutoff = datetime.now(UTC).timestamp() - since_hours * 3600
    with open(paths.EVENTS_FILE) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff is not None:
                try:
                    t = datetime.fromisoformat(ev["ts"]).timestamp()
                except (KeyError, ValueError):
                    continue
                if t < cutoff:
                    continue
            out.append(ev)
    return out
