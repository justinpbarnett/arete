from __future__ import annotations

import contextlib
import gzip
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from . import agent, gh, paths, state, util
from . import lines as run_lines

SETTLE_GRACE_SECONDS = 60
INTERRUPTED = "stopped before it finished (the Mac restarted, lost power or the process was killed)"
STOPPED = "stopped by request"
ID_PREFIX = {"pipeline": "pip", "miner": "min", "eval": "eva"}
FAILS_WHEN_DEAD = ("miner", "eval")
TICKET_KINDS = ("pipeline",)
RECORD_SUFFIX = ".jsonl.gz"
TIMELINE = f"steps{RECORD_SUFFIX}"
CLOCK_SECONDS = 1
HEADER_WAIT_SECONDS = 10
CREDENTIAL_NAME = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|^access$|^refresh$", re.IGNORECASE)
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)
SHORTEST_SECRET = 8
REDACTED = "[redacted]"
LARGEST_SECRET_FILE = 64 * 1024
INI_SECTION = re.compile(r"\[{1,2}[\w .:/\"-]*\]{1,2}")
NAMED_VALUE = re.compile(r"""["']?([\w.-]+)["']?\s*:[ \t]*("(?:[^"\\]|\\.)*"|'[^']*'"""
                         r"""|[^\s,'"{}\[\]#][^\s,{}\[\]]*(?:[ \t]+[^\s#,{}\[\]][^\s,{}\[\]]*)*)""")
NETRC_FILES = (".netrc", "_netrc")
NETRC_SECRET_WORDS = ("login", "password", "account")
PGPASS_FILE = ".pgpass"
PGPASS_FIELDS = 5


def new_id(prefix: str, tag: str = "") -> str:
    stamp = util.now().strftime("%Y%m%d-%H%M%S")
    return "-".join(filter(None, [prefix.replace("/", "."), stamp, os.urandom(2).hex(), tag]))


def start(kind: str, *, run_id: str = "", tag: str = "", lock: str = "", **fields) -> str:
    row = {"kind": kind, "status": "running", "started": util.iso(), "lock": lock or None, "pid": os.getpid(),
           **fields}
    cols, marks = ", ".join(row), ", ".join("?" * len(row))
    while True:
        minted = run_id or new_id(ID_PREFIX.get(kind, kind), tag)
        try:
            with state.db() as c:
                c.execute(f"INSERT INTO runs (id, {cols}) VALUES (?, {marks})", (minted, *row.values()))
            return minted
        except sqlite3.IntegrityError:
            if run_id:
                raise


def step(run_id: str, name: str, res, seconds: float, effort: str = "", tier: str = "", kind: str = "") -> None:
    with state.db() as c:
        c.execute("INSERT INTO steps (run_id, step, runner, model, provider, ok, cost_usd, tokens_in, tokens_out,"
                  " turns, seconds, error, ts, first_context_tokens, effort, tier, try, kind, cache_read, cache_write,"
                  " api_seconds, auth_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                  "(SELECT COUNT(*) + 1 FROM steps WHERE run_id = ? AND step = ?),?,?,?,?,?)",
                  (run_id, name, res.runner, res.model, res.provider, int(res.ok), res.cost_usd, res.tokens_in,
                   res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], util.iso(),
                   res.first_context_tokens, effort or "", tier or "", run_id, name, kind or "", res.cache_read,
                   res.cache_write, round(res.api_seconds, 2), res.auth_mode))
        c.execute("UPDATE runs SET cost_usd = COALESCE(cost_usd, 0) + ?, tokens_in = COALESCE(tokens_in, 0) + ?, "
                  "tokens_out = COALESCE(tokens_out, 0) + ? WHERE id = ?",
                  (res.cost_usd, res.tokens_in, res.tokens_out, run_id))


def metered_spent(run_id: str) -> float:
    return state.paid_by_run([run_id]).get(run_id, 0.0)


def note(run_id: str, **fields) -> None:
    with state.db() as c:
        c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), run_id))


def ask_stop(run_id: str) -> dict | None:
    with state.db() as c:
        c.execute("UPDATE runs SET stop_asked = ? WHERE id = ? AND status = 'running'", (util.iso(), run_id))
        r = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(r) if r else None


def stop_asked(run_id: str) -> bool:
    r = state.run_get(run_id)
    return bool(r and r["stop_asked"])


def settle(run_id: str, status: str, end_line: str = "", **fields) -> dict | None:
    fields = {"status": status, "finished": util.iso(), **fields}
    with state.db() as c:
        if not c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ? AND status = 'running'",
                         (*fields.values(), run_id)).rowcount:
            return None
        if end_line:
            state.insert_feed(c, run_id, "end", end_line)
        settled = dict(c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
    mark(run_id, "result", at=fields.pop("finished"), **fields)
    return settled


@contextmanager
def failed_on_crash(run_id: str):
    try:
        yield
    except BaseException as e:
        why = util.first_line(f"crashed: {type(e).__name__}: {e}", 300)
        settle(run_id, "failed", end_line=f"failed: {why}", summary=why)
        raise


def dead_status(row: dict) -> str:
    if stop_asked(row["id"]):
        return "stopped"
    if row.get("only_step"):
        return "interrupted"
    if row["kind"] == "pipeline":
        earlier = [r for r in state.ticket_runs(row["repo"], row["issue"], limit=3, whole=True) if r["id"] != row["id"]]
        return "failed" if earlier and earlier[0]["status"] == "interrupted" else "interrupted"
    return "failed" if row["kind"] in FAILS_WHEN_DEAD else "interrupted"


@contextmanager
def dead(row: dict):
    if not row.get("lock"):
        yield not util.pid_alive(row.get("pid"))
    elif util.lock_is_held(row["lock"]):
        yield False
    else:
        with util.lock(row["lock"]) as free:
            yield free


def interrupt(row: dict, notes: Sequence[str] = ()) -> dict | None:
    if not util.pid_alive(row.get("pid")):
        util.end_session(row.get("pid"))
    status = dead_status(row)
    summary = "; ".join([STOPPED if status == "stopped" else INTERRUPTED, *notes])
    settled = settle(row["id"], status, end_line=f"{status}: {summary}", summary=summary)
    if settled and row["kind"] == "draft":
        shutil.rmtree(paths.draft_dir(row["id"]), ignore_errors=True)
    return settled


def reap(*, tickets: bool, then: Callable[[dict], None] = lambda row: None) -> list[dict]:
    settled = []
    for row in state.runs_marked_running():
        if ((row["kind"] in TICKET_KINDS) != tickets
                or (util.now() - util.parse_iso(row["started"])).total_seconds() < SETTLE_GRACE_SECONDS):
            continue
        with dead(row) as gone:
            if gone and (done := interrupt(row)):
                then(done)
                settled.append(done)
    return settled


def secrets(*files: Path) -> tuple[str, ...]:
    found = {v for k, v in os.environ.items() if CREDENTIAL_NAME.search(k)}
    found |= {gh.auth_token(), *file_secrets(paths.PI_KEYS_FILE, every_value=True)}
    for f in files:
        found |= file_secrets(f)
    found = {v for v in found if len(v) >= SHORTEST_SECRET}
    as_json = {json.dumps(v, ensure_ascii=False)[1:-1] for v in found}
    return tuple(sorted(found | as_json, key=len, reverse=True))


def file_secrets(path: Path, every_value: bool = False) -> set[str]:
    try:
        if path.stat().st_size > LARGEST_SECRET_FILE:
            return set()
        text = path.read_text(errors="replace")
    except OSError:
        return set()
    found = set(PRIVATE_KEY.findall(text))
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#") and not INI_SECTION.fullmatch(line)]
    if path.name in NETRC_FILES:
        return found | netrc_secrets(lines)
    if path.name == PGPASS_FILE:
        return found | {fields[-1] for line in lines if len(fields := pgpass_fields(line)) == PGPASS_FIELDS}
    for line in lines:
        key, sep, raw = line.removeprefix("export ").partition("=")
        if sep and (every_value or CREDENTIAL_NAME.search(key) or looks_secret(util.env_value(raw))):
            found.add(util.env_value(raw))
        pairs = [(name, unquoted(value)) for name, value in NAMED_VALUE.findall(line)]
        found |= {value for name, value in pairs if CREDENTIAL_NAME.search(name) or looks_secret(value)}
        if not sep and not pairs:
            if every_value:
                found.add(line.strip("'\","))
            word = re.split(r"[\s:]+", line)[-1].strip("'\",")
            if every_value or looks_secret(word):
                found.add(word)
    return found


def netrc_secrets(lines: list[str]) -> set[str]:
    words = " ".join(lines).split()
    return {unquoted(value) for name, value in itertools.pairwise(words) if name in NETRC_SECRET_WORDS}


def pgpass_fields(line: str) -> list[str]:
    return [field.replace("\\:", ":").replace("\\\\", "\\") for field in re.split(r"(?<!\\):", line)]


def unquoted(value: str) -> str:
    value = value.strip().rstrip(",").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        with contextlib.suppress(json.JSONDecodeError):
            return str(json.loads(value))
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def looks_secret(value: str) -> bool:
    return len(value) >= SHORTEST_SECRET and not value.isalpha()


def scrub(text: str, hidden: Sequence[str]) -> str:
    plain = replaced(text, hidden)
    if plain == text or parses(plain) or not parses(text):
        return plain
    return json.dumps(scrub_value(json.loads(text), hidden), ensure_ascii=False)


def replaced(text: str, hidden: Sequence[str]) -> str:
    for value in hidden:
        text = text.replace(value, REDACTED)
    return PRIVATE_KEY.sub(REDACTED, text)


def parses(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def scrub_value(value, hidden: Sequence[str]):
    if isinstance(value, str):
        return replaced(value, hidden)
    if isinstance(value, list):
        return [scrub_value(item, hidden) for item in value]
    if isinstance(value, dict):
        return {key: scrub_value(item, hidden) for key, item in value.items()}
    return value


def slug(step: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", step).strip("-.") or "agent"


def fresh_record(folder: Path, name: str) -> tuple[Path, int]:
    folder.mkdir(parents=True, exist_ok=True)
    for n in itertools.count(1):
        path = folder / f"{name if n == 1 else f'{name}-{n}'}{RECORD_SUFFIX}"
        if path.name == TIMELINE:
            continue
        try:
            return path, os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


def append(fd: int, text: str) -> None:
    os.write(fd, gzip.compress(f"{text}\n".encode(errors="replace")))


class Recorder:
    def __init__(self, run_id: str, step: str, header: dict, hidden: Sequence[str]):
        self.run_id, self.step, self.hidden = run_id, step, hidden
        self.runner, self.model = header.get("runner", ""), header.get("model", "")
        self.lock = threading.RLock()
        self.at, self.started = util.iso(), time.monotonic()
        self.clocked, self.ended = self.started, False
        self.header = {"arete": "header", **header, "run_id": run_id, "step": step, "started": self.at}
        self.held: list[str] | None = []
        self.waiting = threading.Timer(HEADER_WAIT_SECONDS, self.release)
        self.waiting.daemon = True
        self.path: Path | None = None
        self.fd: int | None = None
        try:
            self.path, self.fd = fresh_record(paths.record_dir(run_id), slug(step))
        except OSError as e:
            self.fail(e)
        if self.model or self.fd is None:
            self.release()
        else:
            self.waiting.start()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind, error, trace) -> None:
        if error is not None:
            why = STOPPED if self.stop_requested(error) else f"crashed: {failure(error)}"
            self.end({"ok": False, "error": why, "seconds": self.elapsed()})
        self.close()

    def stop_requested(self, error: BaseException) -> bool:
        if isinstance(error, KeyboardInterrupt):
            return True
        try:
            return stop_asked(self.run_id)
        except sqlite3.Error:
            return False

    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 1)

    def line(self, raw: str) -> None:
        raw = raw.rstrip("\r\n")
        if not raw.strip():
            return
        with self.lock:
            clock = []
            if time.monotonic() - self.clocked >= CLOCK_SECONDS:
                self.clocked = time.monotonic()
                clock.append(json.dumps({"arete": "clock", "seconds": self.elapsed()}))
            text = "\n".join([*clock, scrub(raw, self.hidden)])
            if self.held is None:
                self.put(text)
                return
            self.held.append(text)
            if model := named_model(raw):
                self.model = self.header["model"] = model
                self.release()

    def release(self) -> None:
        with self.lock:
            if self.held is None:
                return
            held, self.held = self.held, None
            self.waiting.cancel()
            self.write(self.header)
            for text in held:
                self.put(text)

    def finish(self, res: agent.Result, seconds: float) -> None:
        self.end({"ok": res.ok, "error": res.error, "text": res.text, "structured": res.structured,
                  "cost_usd": res.cost_usd, "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
                  "first_context_tokens": res.first_context_tokens, "turns": res.turns, "model": res.model,
                  "provider": res.provider, "runner": res.runner, "auth_mode": res.auth_mode,
                  "auth_modes": list(res.modes), "paid_usd": res.paid_usd, "credential": res.credential,
                  "effort": res.effort, "requested_effort": res.requested_effort, "skipped": res.skipped,
                  **({"calls": [{"runner": c.runner, "model": c.model, "provider": c.provider,
                                "auth_mode": c.auth_mode, "cost_usd": c.cost_usd, "paid_usd": c.paid_usd,
                                "tokens_in": c.tokens_in, "tokens_out": c.tokens_out} for c in res.calls]}
                     if res.calls else {}),
                  "session": res.session.id, "seconds": round(seconds, 1)})

    def end(self, result: dict) -> None:
        if self.ended:
            return
        self.ended = True
        result = {"runner": self.runner, "model": self.model, "provider": self.header.get("provider", ""),
                  "auth_mode": self.header.get("auth_mode", ""), **result}
        self.release()
        self.write({"arete": "result", **result})
        self.close()
        mark(self.run_id, "agent", at=self.at, step=self.step, record=self.path.name if self.path else "",
             runner=self.runner, model=result.get("model") or self.model, ok=result["ok"],
             auth_mode=result["auth_mode"], provider=result.get("provider", self.header.get("provider", "")),
             **{k: result[k] for k in ("auth_modes", "paid_usd", "credential", "effort") if k in result},
             seconds=result["seconds"], **{k: result[k] for k in ("cost_usd", "turns") if k in result},
             **({"error": util.first_line(scrub(result["error"], self.hidden), 300)} if result["error"] else {}))

    def write(self, entry: dict) -> None:
        with self.lock:
            self.put(scrub(json.dumps(entry, ensure_ascii=False, default=str), self.hidden))

    def put(self, text: str) -> None:
        if self.fd is None:
            return
        try:
            append(self.fd, text)
        except OSError as e:
            self.fail(e)

    def fail(self, e: OSError) -> None:
        print(f"{util.iso()} stopped recording {self.run_id} {self.step}: {e}", file=sys.stderr, flush=True)
        self.close()

    def close(self) -> None:
        with self.lock:
            self.release()
            fd, self.fd = self.fd, None
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)


def mark(run_id: str, kind: str, **fields) -> None:
    if not run_id:
        return
    text = scrub(json.dumps({"kind": kind, "at": util.iso(), **fields}, ensure_ascii=False, default=str), secrets())
    folder = paths.record_dir(run_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / TIMELINE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            append(fd, text)
        finally:
            os.close(fd)
    except OSError as e:
        print(f"{util.iso()} could not mark {kind} for {run_id}: {e}", file=sys.stderr, flush=True)


@contextmanager
def timed(run_id: str, kind: str, **detail):
    at, started = util.iso(), time.monotonic()
    try:
        yield detail
    except BaseException as e:
        detail["error"] = failure(e)
        raise
    finally:
        mark(run_id, kind, at=at, **detail, seconds=round(time.monotonic() - started, 2))


def failure(e: BaseException) -> str:
    return ": ".join(filter(None, [type(e).__name__, util.first_line(str(e), 300)]))


def record_files(run_id: str) -> list[Path]:
    found = [f for f in paths.record_dir(run_id).glob(f"*{RECORD_SUFFIX}") if f.name != TIMELINE]
    return sorted(found, key=lambda f: f.stat().st_mtime)


def find_record(run_id: str, step: str) -> Path | None:
    folder = paths.record_dir(run_id)
    for name in (step, f"{step}{RECORD_SUFFIX}", f"{slug(step)}{RECORD_SUFFIX}"):
        if name != TIMELINE and (folder / name).is_file():
            return folder / name
    return None


class AppendedLines:
    def __init__(self, path: Path, compressed: bool = True):
        self.path, self.offset, self.pending = path, 0, b""
        self.decoder = zlib.decompressobj(wbits=31) if compressed else None
        self.failed = False

    def read(self) -> Iterator[str]:
        if self.failed:
            return
        try:
            f = self.path.open("rb")
        except FileNotFoundError:
            return
        with f:
            f.seek(self.offset)
            while chunk := f.read(64 * 1024):
                self.offset += len(chunk)
                while chunk:
                    if self.decoder is not None:
                        if self.decoder.eof:
                            self.decoder = zlib.decompressobj(wbits=31)
                        try:
                            data = self.decoder.decompress(chunk)
                        except zlib.error:
                            self.failed = True
                            return
                        chunk = self.decoder.unused_data
                    else:
                        data, chunk = chunk, b""
                    self.pending += data
                    complete = self.pending.split(b"\n")
                    self.pending = complete.pop()
                    for line in complete:
                        if line.strip():
                            yield line.decode(errors="replace")


def raw_lines(path: Path) -> Iterator[str]:
    yield from AppendedLines(path).read()


def run_status(run_id: str) -> str:
    try:
        row = state.run_get(run_id, connect=state.reader)
    except sqlite3.Error:
        return ""
    return (row.get("status") or "") if row else ""


def follow(run_id: str, step: str) -> Iterator[str]:
    path = find_record(run_id, step)
    if not path and (step in ("steps", TIMELINE) or run_status(run_id) != "running"):
        raise FileNotFoundError(step)
    source = AppendedLines(path) if path else None
    timeline = AppendedLines(paths.record_dir(run_id) / TIMELINE)
    report = AppendedLines(run_lines.path(run_id), compressed=False)
    ended = False
    while True:
        if source is None and (path := find_record(run_id, step)):
            source = AppendedLines(path)
        if source is not None:
            with contextlib.closing(source.read()) as available:
                for line in available:
                    result = next(entries([line]), {}).get("arete") == "result"
                    yield line
                    if result:
                        return
        if ended:
            if source is None:
                raise FileNotFoundError(step)
            return
        status = run_status(run_id)
        ended = bool(status and status != "running")
        for entry in entries(timeline.read()):
            ended |= entry.get("kind") == "result"
        for entry in entries(report.read()):
            ended |= entry.get("event") == run_lines.END
        if not ended:
            time.sleep(run_lines.FOLLOW_POLL_SECONDS)


def lines(path: Path) -> Iterator[dict]:
    yield from entries(raw_lines(path))


def entries(source: Iterable[str]) -> Iterator[dict]:
    for line in source:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def events(path: Path) -> Iterator[dict]:
    yield from normalized(lines(path))


def last_message(path: Path, notice: re.Pattern[str] | None = None) -> str:
    found = ""
    try:
        for event in events(path):
            text = str(event.get("text") or "").strip()
            if (event.get("kind") == "message" and event.get("role") == "assistant" and text
                    and not (notice and notice.search(text))):
                found = text
    except OSError:
        return ""
    return found


def normalized(source: Iterable[dict]) -> Iterator[dict]:
    seconds, read = 0.0, unread
    for line in source:
        own = line.pop("arete", None)
        if own == "clock":
            seconds = line.get("seconds", seconds)
        elif own in ("header", "result"):
            if own == "header":
                read = READERS.get(line.get("runner"), lambda header: unread)(line)
            yield {"kind": own, "seconds": seconds, **line}
        elif not own:
            for event in read(line):
                yield {**event, "seconds": seconds}


def unread(event: dict) -> list[dict]:
    return []


def blocks(content) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def block_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in blocks(content) if b.get("type") == "text")


def claude_reader(header: dict) -> Callable[[dict], list[dict]]:
    tool_names: dict[str, str] = {}
    counted: set[str] = set()
    resumed = header.get("resume") if isinstance(header.get("resume"), dict) else {}
    spent_before = float(resumed.get("spent_usd") or 0)

    def read(e: dict) -> list[dict]:
        message = e.get("message") if isinstance(e.get("message"), dict) else {}
        out = []
        if e.get("type") == "assistant":
            for b in blocks(message.get("content")):
                if b.get("type") == "text":
                    out.append({"kind": "message", "role": "assistant", "text": b.get("text", "")})
                elif b.get("type") == "thinking":
                    out.append({"kind": "message", "role": "thinking", "text": b.get("thinking", "")})
                elif b.get("type") == "tool_use":
                    tool_names[b.get("id", "")] = b.get("name", "")
                    out.append({"kind": "tool_call", "id": b.get("id", ""), "name": b.get("name", ""),
                                "input": b.get("input")})
            if isinstance(usage := message.get("usage"), dict) and message.get("id") not in counted:
                counted.add(message.get("id"))
                out.append({"kind": "usage", "model": message.get("model", ""), **agent.claude_tokens(usage),
                            "cost_usd": None})
        elif e.get("type") == "user":
            if isinstance(message.get("content"), str):
                out.append({"kind": "message", "role": "user", "text": message["content"]})
            for b in blocks(message.get("content")):
                if b.get("type") == "tool_result":
                    out.append({"kind": "tool_result", "id": b.get("tool_use_id", ""),
                                "name": tool_names.get(b.get("tool_use_id", ""), ""),
                                "text": block_text(b.get("content")), "error": bool(b.get("is_error"))})
                elif b.get("type") == "text":
                    out.append({"kind": "message", "role": "user", "text": b.get("text", "")})
        elif e.get("type") == "result":
            session_cost = e.get("total_cost_usd")
            out.append({"kind": "usage", "total": True, "model": next(iter(e.get("modelUsage") or {}), ""),
                        **agent.claude_tokens(e.get("usage") or {}),
                        "cost_usd": None if session_cost is None else max(0.0, float(session_cost) - spent_before)})
        return out

    return read


def read_pi(e: dict) -> list[dict]:
    message = e.get("message") if isinstance(e.get("message"), dict) else {}
    if e.get("type") != "message_end":
        return []
    if message.get("role") == "toolResult":
        return [{"kind": "tool_result", "id": message.get("toolCallId", ""), "name": message.get("toolName", ""),
                 "text": block_text(message.get("content")), "error": bool(message.get("isError"))}]
    if message.get("role") == "user":
        return [{"kind": "message", "role": "user", "text": block_text(message.get("content"))}]
    if message.get("role") != "assistant":
        return []
    out = []
    for b in blocks(message.get("content")):
        if b.get("type") == "text":
            out.append({"kind": "message", "role": "assistant", "text": b.get("text", "")})
        elif b.get("type") == "thinking":
            out.append({"kind": "message", "role": "thinking", "text": b.get("thinking", "")})
        elif b.get("type") == "toolCall":
            out.append({"kind": "tool_call", "id": b.get("id", ""), "name": b.get("name", ""),
                        "input": b.get("arguments")})
    if isinstance(usage := message.get("usage"), dict):
        totals, provider = agent.usage_totals([usage]), message.get("provider", "")
        out.append({"kind": "usage",
                    "model": f"{provider}/{message.get('model')}" if provider else message.get("model", ""),
                    "tokens_in": totals["tokens_in"], "tokens_out": totals["tokens_out"],
                    "cost_usd": totals["cost_usd"]})
    return out


def codex_reader(header: dict) -> Callable[[dict], list[dict]]:
    before: dict = {}

    def read(e: dict) -> list[dict]:
        nonlocal before
        if e.get("arete") == "command":
            before = e.get("usage_before") or {}
            return []
        item = e.get("item") if isinstance(e.get("item"), dict) else {}
        kind, at = item.get("type"), item.get("id", "")
        if e.get("type") == "turn.completed" and isinstance(usage := e.get("usage"), dict):
            return [{"kind": "usage", "total": True, "model": header.get("model", ""),
                     "tokens_in": max(0, int(usage.get("input_tokens") or 0) - int(before.get("input_tokens") or 0)),
                     "tokens_out": max(0, int(usage.get("output_tokens") or 0) - int(before.get("output_tokens") or 0)),
                     "cost_usd": None}]
        if e.get("type") == "item.started" and kind == "command_execution":
            return [{"kind": "tool_call", "id": at, "name": "Bash", "input": {"command": item.get("command", "")}}]
        if e.get("type") != "item.completed":
            return []
        if kind in ("agent_message", "reasoning"):
            role = "assistant" if kind == "agent_message" else "thinking"
            return [{"kind": "message", "role": role, "text": item.get("text", "")}]
        if kind == "command_execution":
            return [{"kind": "tool_result", "id": at, "name": "Bash", "text": item.get("aggregated_output", ""),
                     "error": item.get("exit_code") not in (0, None)}]
        if kind == "file_change":
            return [{"kind": "tool_call", "id": at, "name": "apply_patch", "input": {"changes": item.get("changes")}}]
        return []

    return read


READERS: dict[str, Callable[[dict], Callable[[dict], list[dict]]]] = {"claude": claude_reader,
                                                                      "pi": lambda header: read_pi,
                                                                      "codex": codex_reader}


def named_model(raw: str) -> str:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "system" and event.get("subtype") == "init":
        return str(event.get("model") or "")
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if message.get("role") != "assistant" or not message.get("model"):
        return ""
    return f"{message['provider']}/{message['model']}" if message.get("provider") else str(message["model"])
