from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import socket
import sys
from datetime import UTC, datetime

from . import api_http, paths, refusals, util

INGEST_MARK = "_arete_ingest"

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
            with util.lock("hook-events", wait=True):
                append_events([ev])
    except Exception as e:
        with contextlib.suppress(OSError):
            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(paths.LOG_DIR / "hook-errors.log", "a") as f:
                f.write(f"{datetime.now().isoformat()} {type(e).__name__}: {e}\n")
    return 0


def append_events(events):
    finish_ingest()
    with paths.EVENTS_FILE.open("a+b") as out:
        out.seek(0, os.SEEK_END)
        if out.tell():
            out.seek(-1, os.SEEK_END)
            if out.read(1) != b"\n":
                out.write(b"\n")
        for event in events:
            out.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")


def ingest_checkpoint():
    path = paths.EVENTS_FILE.with_name("hook-ingest.json")
    checkpoint = util.read_json(path, None)
    if checkpoint is None:
        if path.exists():
            raise OSError(f"cannot read hook ingest checkpoint {path}")
        checkpoint = {"offsets": {}}
    return path, checkpoint


def finish_ingest():
    path, checkpoint = ingest_checkpoint()
    if not (pending := checkpoint.get("pending")):
        return path, checkpoint
    data = pending["data"].encode()
    with paths.EVENTS_FILE.open("a+b") as out:
        out.seek(0, os.SEEK_END)
        start = pending["start"]
        if not start <= out.tell() <= start + len(data):
            raise OSError("hook event file changed during an unfinished upload")
        out.seek(start)
        written = out.read()
        if not data.startswith(written):
            raise OSError("hook event file changed during an unfinished upload")
        out.write(data[len(written):])
        out.flush()
        util.flush_fd(out.fileno())
        util.flush_folder(paths.EVENTS_FILE.parent)
    checkpoint["offsets"][pending["origin"]] = [pending["offset"], pending["sha"]]
    del checkpoint["pending"]
    util.write_json(path, checkpoint)
    return path, checkpoint


def ingest(origin, offset, events):
    if not re.fullmatch(r"[0-9a-f]{32}", origin) or offset < 0:
        raise refusals.Invalid("Hook ingest needs a source identifier and a nonnegative offset")
    with util.lock("hook-events", wait=True):
        checkpoint_path, checkpoint = finish_ingest()
        data = "".join(json.dumps(event) + "\n" for event in events)
        sha = hashlib.sha256(data.encode()).hexdigest()
        received, prior = checkpoint["offsets"].get(origin, (-1, ""))
        if offset == received and sha != prior:
            raise refusals.Conflict("Hook events at this source offset have changed")
        if offset <= received or not data:
            return len(events)
        for path in event_files():
            with path.open("rb") as source:
                if event_origin(source, os.fstat(source.fileno())) == origin:
                    return len(events)
        start = 0
        try:
            with paths.EVENTS_FILE.open("rb") as out:
                start = out.seek(0, os.SEEK_END)
                if start:
                    out.seek(-1, os.SEEK_END)
                    if out.read(1) != b"\n":
                        data = "\n" + data
        except FileNotFoundError:
            pass
        checkpoint["pending"] = {"origin": origin, "offset": offset, "sha": sha, "start": start, "data": data}
        util.write_json(checkpoint_path, checkpoint)
        finish_ingest()
    return len(events)


def event_origin(stream, info):
    first = stream.readline()
    if not first.endswith(b"\n"):
        return ""
    location = f"{socket.gethostname()}:{paths.STATE_DIR}:{info.st_dev}:{info.st_ino}:".encode()
    return hashlib.sha256(location + first).hexdigest()[:32]


def send_pending(url, send):
    if not paths.EVENTS_FILE.exists() and not dated_events():
        return
    checkpoint = paths.STATE_DIR / ("hook-offsets-" + hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")
    with util.lock(checkpoint.stem, wait=True):
        offsets = util.read_json(checkpoint, {})
        for path in event_files():
            try:
                stream = path.open("rb")
            except FileNotFoundError:
                continue
            with stream:
                info = os.fstat(stream.fileno())
                if not (origin := event_origin(stream, info)):
                    continue
                offset, pending = offsets.get(origin, (0, None))
                stream.seek(offset)
                while offset < info.st_size:
                    body = {"origin": origin, "offset": offset, "events": []}
                    end = read_batch(stream, body, info.st_size, pending)
                    if end == offset:
                        break
                    if body["events"]:
                        offsets[origin] = (offset, end)
                        util.write_json(checkpoint, offsets)
                        send(body, f"hook:{origin}:{offset}")
                    offset, pending = end, None
                    offsets[origin] = (offset, pending)
        util.write_json(checkpoint, offsets)


def read_batch(stream, body, size, pending):
    end = stream.tell()
    used = len(api_http.json_bytes(body))
    events = body["events"]
    while end < (size if pending is None else pending):
        raw = stream.readline()
        if not raw.endswith(b"\n") or stream.tell() > size:
            break
        try:
            item = json.loads(raw)
        except (ValueError, UnicodeError):
            item = None
        if isinstance(item, dict) and INGEST_MARK not in item:
            added = len(api_http.json_bytes(item)) + (2 if events else 0)
            if pending is None and used + added > api_http.MAX_JSON_BYTES:
                if not events:
                    raise refusals.Invalid(f"A hook event at offset {end} exceeds the server's JSON body limit")
                break
            events.append(item)
            used += added
        end = stream.tell()
    if pending is not None and end != pending:
        raise refusals.Invalid("The pending hook batch is no longer in the local event file")
    stream.seek(end)
    return end


def dated_events():
    return sorted((p for p in paths.EVENTS_FILE.parent.glob("events-*.jsonl")
                   if re.fullmatch(r"events-\d{4}(0[1-9]|1[0-2])(-\d{4,})?\.jsonl", p.name)
                   and not p.is_symlink() and p.is_file()), key=lambda p: p.stem)


def event_files():
    return [p for p, _, _ in event_stats()]


def event_stats():
    out = []
    for path in dated_events() + [paths.EVENTS_FILE]:
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        if path == paths.EVENTS_FILE and not os.path.isfile(path):
            continue
        out.append((path, st.st_mtime, st.st_ino))
    return out


def rotate_events(keep_days: float) -> None:
    with util.lock("hook-events", wait=True):
        rotate_locked(keep_days)


def rotate_locked(keep_days: float) -> None:
    finish_ingest()
    try:
        with paths.EVENTS_FILE.open() as f:
            first = f.readline()
    except FileNotFoundError:
        first = ""
    try:
        month = datetime.fromisoformat(json.loads(first)["ts"]).astimezone(UTC).strftime("%Y%m")
    except (ValueError, KeyError, TypeError):
        month = ""
    if month and month < datetime.now(UTC).strftime("%Y%m"):
        target = paths.EVENTS_FILE.with_name(f"events-{month}.jsonl")
        number = 0
        while target.exists() or target.is_symlink():
            number += 1
            target = target.with_name(f"events-{month}-{number:04d}.jsonl")
        os.replace(paths.EVENTS_FILE, target)
    cutoff = datetime.now(UTC).timestamp() - keep_days * 86400
    for path in dated_events():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue


def read_events(since_hours: float | None = None, since_ts: float | None = None,
                until_ts: float | None = None) -> list[dict]:
    cutoff = since_ts
    if since_hours is not None:
        cutoff = datetime.now(UTC).timestamp() - since_hours * 3600
    out = []
    for attempt in range(8):
        before = event_stats()
        out = []
        try:
            for path, mtime, _ in before:
                if path != paths.EVENTS_FILE and cutoff is not None and mtime < cutoff:
                    continue
                out.extend(read_event_file(path, cutoff, until_ts))
        except FileNotFoundError:
            continue
        if [(p, i) for p, _, i in event_stats()] == [(p, i) for p, _, i in before]:
            return out
    return out


def read_event_file(path, cutoff, until=None) -> list[dict]:
    out = []
    with path.open("rb") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(ev, dict):
                continue
            if cutoff is not None or until is not None:
                try:
                    t = datetime.fromisoformat(ev["ts"]).timestamp()
                except (KeyError, ValueError, TypeError):
                    continue
                if (cutoff is not None and t < cutoff) or (until is not None and t >= until):
                    continue
            ev.pop(INGEST_MARK, None)
            out.append(ev)
    return out
