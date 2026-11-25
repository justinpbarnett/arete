from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import socket
import sys
import time
from datetime import UTC, datetime

from . import paths, refusals, util

INGEST_MARK = "_arete_ingest"
WORKER_HOOK_SOCKET = "ARETE_WORKER_HOOK_SOCKET"
MAX_HOOK_EVENT = 64 * 1024
WORKER_JUDGE_CAPABILITY = "ARETE_WORKER_JUDGE_CAPABILITY"
MAX_JUDGE_REQUEST = 768 * 1024
MAX_JUDGE_REPLY = 32 * 1024
JUDGE_WAIT = 38

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


def append_hook_event(event):
    with event_lock("hook-events"):
        finish_ingest()
        with paths.EVENTS_FILE.open("a+b") as out:
            out.write(line_break(out) + (json.dumps(event, ensure_ascii=False) + "\n").encode())


@contextlib.contextmanager
def socket_service(server):
    import threading

    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def judge_frame(peer, limit, deadline):
    raw = bytearray()
    while len(raw) <= limit:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        peer.settimeout(left)
        chunk = peer.recv(min(16384, limit + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
        if b"\n" in chunk:
            if len(raw) <= limit and raw.endswith(b"\n") and raw.count(b"\n") == 1:
                return json.loads(raw.decode("utf-8"))
            break
    raise ValueError("invalid worker judge frame")


@contextlib.contextmanager
def worker_hook_sink(env, judge=None):
    if sys.platform != "linux":
        yield None
        return

    import socketserver
    import tempfile
    from pathlib import Path

    class HookRequest(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(2)
            try:
                line = self.rfile.readline(MAX_HOOK_EVENT + 1)
                if not line.endswith(b"\n") or len(line) > MAX_HOOK_EVENT:
                    return
                event = json.loads(line)
                fields = {"ts", "hook", "tool", "session", "cwd", "kind", "cls", "detail"}
                if (not isinstance(event, dict) or set(event) - fields - {"agent", "error"}
                        or type(event.get("agent")) is not bool
                        or not all(isinstance(event.get(k), str) for k in fields)
                        or not isinstance(event.get("error", ""), str)
                        or event["hook"] not in ("PostToolUse", "PostToolUseFailure", "StopFailure")):
                    return
                append_hook_event(event)
                self.wfile.write(b"1")
            except (OSError, ValueError, RecursionError):
                return

    capability = secrets.token_hex(32)
    run_id = env.get("ARETE_RUN_ID", "")

    class JudgeRequest(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                request = judge_frame(self.request, MAX_JUDGE_REQUEST, time.monotonic() + 2)
                if not isinstance(request, dict):
                    return
                supplied = request.pop("capability", None)
                if (not isinstance(supplied, str) or len(supplied) != len(capability) or not supplied.isascii()
                        or not secrets.compare_digest(supplied, capability) or request.pop("run", None) != run_id):
                    return
                reply = json.dumps(judge(request), allow_nan=False, ensure_ascii=False).encode(errors="backslashreplace")
                reply += b"\n"
                if len(reply) <= MAX_JUDGE_REPLY:
                    self.request.settimeout(1)
                    self.request.sendall(reply)
            except Exception:
                return

    env.pop(WORKER_JUDGE_CAPABILITY, None)
    with tempfile.TemporaryDirectory(prefix="arete-w-") as folder, contextlib.ExitStack() as opened:
        root = Path(folder).resolve()
        temporary = root / "tmp"
        temporary.mkdir(mode=0o700)
        address = str(root / "hook")
        opened.enter_context(socket_service(socketserver.UnixStreamServer(address, HookRequest)))
        if judge is not None:
            with contextlib.suppress(OSError):
                opened.enter_context(socket_service(socketserver.UnixStreamServer(str(root / "judge"), JudgeRequest)))
                env[WORKER_JUDGE_CAPABILITY] = capability
        env.update({WORKER_HOOK_SOCKET: address, "TMPDIR": str(temporary)})
        yield temporary


def in_worker() -> bool:
    return bool(os.environ.get(WORKER_HOOK_SOCKET))


def has_worker_judge() -> bool:
    return in_worker() and bool(os.environ.get(WORKER_JUDGE_CAPABILITY))


def worker_judge_request(op, cwd, state, **fields):
    address = os.environ.get(WORKER_HOOK_SOCKET)
    capability = os.environ.get(WORKER_JUDGE_CAPABILITY)
    if not address or not capability:
        return {}
    try:
        line = (json.dumps({"op": op, "cwd": str(cwd), "state": state, **fields,
                            "run": os.environ.get("ARETE_RUN_ID", ""), "capability": capability},
                           ensure_ascii=False) + "\n").encode(errors="backslashreplace")
        if len(line) > MAX_JUDGE_REQUEST:
            return {}
        end = time.monotonic() + JUDGE_WAIT
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2)
            peer.connect(os.path.join(os.path.dirname(address), "judge"))
            peer.sendall(line)
            reply = judge_frame(peer, MAX_JUDGE_REPLY, end)
            return reply if isinstance(reply, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def send_hook_event(event, address):
    line = (json.dumps(event) + "\n").encode()
    if len(line) > MAX_HOOK_EVENT:
        raise ValueError("worker hook event is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(address)
        client.sendall(line)
        if client.recv(1) != b"1":
            raise OSError("worker hook event could not be recorded")


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        ev = build_event(payload)
        if ev:
            if address := os.environ.get(WORKER_HOOK_SOCKET):
                send_hook_event(ev, address)
            else:
                append_hook_event(ev)
    except Exception as e:
        with contextlib.suppress(OSError):
            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(paths.LOG_DIR / "hook-errors.log", "a") as f:
                f.write(f"{datetime.now().isoformat()} {type(e).__name__}: {e}\n")
    return 0


def checkpoint(path, default):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default
    except ValueError as e:
        raise OSError(f"cannot read hook event checkpoint {path}") from e


@contextlib.contextmanager
def event_lock(name, *, wait=True):
    folder = paths.EVENTS_FILE.parent / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{name}.lock").open("a") as lock:
        yield util.take_lock(lock, wait)


def line_break(stream):
    if stream.seek(0, os.SEEK_END):
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) != b"\n":
            return b"\n"
    return b""


def finish_ingest():
    path = paths.EVENTS_FILE.with_name("hook-ingest.json")
    saved = checkpoint(path, {})
    batches = saved.setdefault("batches", {})
    for stream, (offset, digest) in saved.pop("offsets", {}).items():
        batches.setdefault(stream, {})[str(offset)] = digest
    if pending := saved.get("pending"):
        data = pending["data"].encode()
        start = pending["start"]
        with paths.EVENTS_FILE.open("a+b") as out:
            if not start <= out.seek(0, os.SEEK_END) <= start + len(data):
                raise OSError("hook event file changed during an unfinished ingest")
            out.seek(start)
            written = out.read()
            if not data.startswith(written):
                raise OSError("hook event file changed during an unfinished ingest")
            out.write(data[len(written):])
            out.flush()
            util.flush_fd(out.fileno())
        util.flush_folder(path.parent)
        batches.setdefault(pending["stream"], {})[str(pending["offset"])] = pending["digest"]
        del saved["pending"]
        util.write_json(path, saved)
    return path, saved


def ingest(origin, stream, offset, events):
    if not events:
        return 0
    stream_key = f"{origin}/{stream}"
    data = "".join(json.dumps({**event, INGEST_MARK: origin}, sort_keys=True) + "\n" for event in events)
    digest = util.digest(data)
    with event_lock("hook-events"):
        path, saved = finish_ingest()
        prior = saved["batches"].get(stream_key, {}).get(str(offset))
        if prior is not None:
            if digest != prior:
                raise refusals.Conflict("Hook events at this source offset have changed")
            return len(events)
        with paths.EVENTS_FILE.open("a+b") as out:
            prefix = line_break(out).decode()
            start = out.tell()
        saved["pending"] = {"stream": stream_key, "offset": offset, "digest": digest,
                            "start": start, "data": prefix + data}
        util.write_json(path, saved)
        finish_ingest()
    return len(events)


def local_instance():
    return checkpoint(paths.backups_dir() / "instance.json", {}).get("instance_id")


def source_instance():
    if instance := local_instance():
        return instance
    path = paths.STATE_DIR / "hook-source.json"
    with event_lock("hook-source"):
        if source := checkpoint(path, None):
            return source
        source = secrets.token_hex(16)
        util.write_json(path, source)
        return source


def upload_pending(instance, send, *, wait=False):
    path = paths.STATE_DIR / "hook-uploads" / f"{util.digest(instance)}.json"
    with event_lock(f"hook-upload-{path.stem}", wait=wait) as locked:
        if not locked:
            return
        saved = checkpoint(path, {"offsets": {}})
        if "pending" in saved:
            send_pending(path, saved, send)
        with contextlib.ExitStack() as opened:
            files = upload_snapshots(opened)
            for stream, stream_id, size in files:
                upload_file(stream, stream_id, size, path, saved, send)


def streams_path():
    return paths.EVENTS_FILE.with_name("hook-streams.json")


def file_identity(info):
    return [info.st_dev, info.st_ino, getattr(info, "st_birthtime", None)]


def tail_digest(stream, size):
    width = min(size, 4096)
    stream.seek(size - width)
    return util.digest(stream.read(width))


def same_log(previous, stream, size, head):
    return (previous is not None and previous["head"] == head and previous["size"] <= size
            and previous["tail"] == tail_digest(stream, previous["size"]))


def upload_snapshots(opened):
    with event_lock("hook-events"):
        finish_ingest()
        path = streams_path()
        previous = checkpoint(path, None)
        folder = str(path.parent.resolve())
        old = previous["files"] if previous else {}
        by_file = {tuple(row["file"]): row for row in old.values()}
        moved = previous is not None and previous["folder"] != folder
        legacy = previous is None and (paths.STATE_DIR / "hook-uploads").is_dir()
        current, files = {}, []
        for event_path in event_files():
            try:
                stream = opened.enter_context(event_path.open("rb"))
            except FileNotFoundError:
                continue
            info = os.fstat(stream.fileno())
            first = stream.readline(info.st_size)
            if not first.endswith(b"\n"):
                continue
            identity = file_identity(info)
            head = util.digest(first)
            prior = by_file.get(tuple(identity))
            if prior is None and (named := old.get(event_path.name)):
                if moved or named["file"][0] != info.st_dev:
                    prior = named
            if same_log(prior, stream, info.st_size, head):
                stream_id = prior["id"]
            elif legacy:
                stream_id = util.digest(str(info.st_dev), str(info.st_ino), first)
            else:
                stream_id = secrets.token_hex(32)
            current[event_path.name] = {"id": stream_id, "file": identity, "head": head,
                                        "size": info.st_size, "tail": tail_digest(stream, info.st_size)}
            files.append((stream, stream_id, info.st_size))
        next_state = {"folder": folder, "files": current}
        if (current or previous) and next_state != previous:
            util.write_json(path, next_state)
        return files


def send_pending(path, saved, send):
    pending = saved["pending"]
    body = pending["body"]
    send(body, f"hook:{body['origin']}:{body['stream']}:{body['offset']}")
    saved["offsets"][body["stream"]] = pending["end"]
    del saved["pending"]
    util.write_json(path, saved)


def upload_file(stream, stream_id, size, path, saved, send):
    offset = saved["offsets"].get(stream_id, 0)
    stream.seek(offset)
    while offset < size:
        body = {"origin": source_instance(), "stream": stream_id, "offset": offset, "events": []}
        end = read_batch(stream, body, size)
        if end == offset:
            break
        if body["events"]:
            saved["pending"] = {"body": body, "end": end}
            util.write_json(path, saved)
            send_pending(path, saved, send)
        else:
            saved["offsets"][stream_id] = end
            util.write_json(path, saved)
        offset = end


def read_batch(stream, body, size):
    from . import api_http

    end = stream.tell()
    used = len(api_http.json_bytes(body))
    events = body["events"]
    while end < size:
        raw = stream.readline(size - end)
        if not raw.endswith(b"\n"):
            break
        try:
            item = json.loads(raw)
        except ValueError:
            item = None
        if isinstance(item, dict) and INGEST_MARK not in item:
            added = len(api_http.json_bytes(item)) + (2 if events else 0)
            if used + added > api_http.MAX_JSON_BYTES:
                if not events:
                    raise OSError(f"hook event at offset {end} exceeds the server's JSON body limit")
                break
            events.append(item)
            used += added
        end = stream.tell()
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
    with event_lock("hook-events"):
        finish_ingest()
        rotate_locked(keep_days)


def rotate_locked(keep_days):
    try:
        stream = paths.EVENTS_FILE.open("rb")
    except FileNotFoundError:
        pass
    else:
        with stream:
            rotate_current(stream)
    cutoff = datetime.now(UTC).timestamp() - keep_days * 86400
    for path in dated_events():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue


def rotate_current(stream):
    first = stream.readline()
    try:
        month = datetime.fromisoformat(json.loads(first)["ts"]).astimezone(UTC).strftime("%Y%m")
    except (ValueError, KeyError, TypeError):
        return
    if month >= datetime.now(UTC).strftime("%Y%m"):
        return
    target = paths.EVENTS_FILE.with_name(f"events-{month}.jsonl")
    number = 0
    while target.exists() or target.is_symlink():
        number += 1
        target = target.with_name(f"events-{month}-{number:04d}.jsonl")
    path = streams_path()
    if (saved := checkpoint(path, None)) and (current := saved["files"].pop(paths.EVENTS_FILE.name, None)):
        info = os.fstat(stream.fileno())
        identity = file_identity(info)
        moved = saved["folder"] != str(path.parent.resolve()) or current["file"][0] != info.st_dev
        if (current["file"] == identity or moved) and same_log(current, stream, info.st_size, util.digest(first)):
            saved["files"][target.name] = {**current, "file": identity}
        util.write_json(path, saved)
    os.replace(paths.EVENTS_FILE, target)


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
            except ValueError:
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
