from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from dataclasses import dataclass

from . import config, control, lines, outbound, paths, record, sources, state, util

UNEXPECTED, HEARTBEAT, STALLED, ORCHESTRATION, EXPECTED = "unexpected", "heartbeat", "stalled", "orchestration", "expected"
PREFIX = "oncall:"
SCAN = PREFIX + "scan"
MAX_READ = 1_000_000
CONTEXT_LINES = 5
MAX_EVIDENCE = 4000
TAIL_BYTES = 256
RUN_TAIL_BYTES = 65536
MAX_RUN_TAIL = 16_777_216
STALL_SECONDS = 300
STALE_SECONDS = 300
RECURRING_BEATS = 2
FAILURE = re.compile(sources.LOG_PATTERN + r"|\bFAIL(?:ED)?\b|\bfailed\b|\bcrashed\b")
EXPECTED_TEXT = re.compile(
    r"limit reached|waiting until|held until cleared|trying another route|waiting for the network|tries again"
    r"|\bretry(?:ing)?\b|\bbackoff\b|back off|rate.?limit|\b429\b|usage limit|no run slot is free|already running"
    r"|already has \d+ runs? going|is running; stop it or wait|check \S+: FAIL \(exit"
    r"|checks (?:still failing|failed on attempt)"
    r"|\b(?:fix\d+|review_fix\w*|ci_\w*fix\w*|(?:check|review|ci)_repair\w*): FAILED", re.IGNORECASE)
ORCHESTRATION_TEXT = re.compile(r"tick failed:|poll failed:|dropped queue item|sweeping left-over VMs failed"
                                r"|new code does not import|: loop broken:")
OWN_LINE = re.compile(r"\bon-call\b")
TRACEBACK = "Traceback (most recent call last):"
CHAINED = re.compile(r"During handling of the above exception|The above exception was the direct cause")
REPAIR_STEPS = ("check_repair", "review_repair", "ci_repair")


@dataclass(frozen=True)
class Origin:
    scope: str
    repo: str = ""
    pattern: re.Pattern = FAILURE
    folder: bool = False


@dataclass
class Event:
    kind: str
    scope: str
    signature: str
    source: str
    repo: str
    window: list[str]
    count: int = 1


def digest(name: str, end: int) -> str | None:
    if end <= 0:
        return ""
    try:
        with open(name, "rb") as f:
            f.seek(max(0, end - TAIL_BYTES))
            return hashlib.sha1(f.read(min(end, TAIL_BYTES))).hexdigest()
    except OSError:
        return None


def drain(name: str, pending: list) -> tuple[list[str], list | None]:
    older = name + ".1"
    offset, tail = pending
    try:
        size = os.stat(older).st_size
        if size < offset or digest(older, offset) != tail:
            return [], None
        end = min(size, offset + MAX_READ)
        with open(older, "rb") as f:
            f.seek(offset)
            data = f.read(end - offset)
    except OSError:
        return [], None
    if end < size:
        data = data[:data.rfind(b"\n") + 1 or len(data)]
        end = offset + len(data)
    return data.decode("utf-8", "replace").splitlines(), ([end, digest(older, end)] if end < size else None)


def gone(name: str, mark: list | None) -> tuple[list[str], list | None]:
    if mark is None:
        return [], mark
    pending = mark[3] if len(mark) > 3 else None
    if not pending and mark[0] > 0 and digest(name + ".1", mark[0]) == mark[2]:
        pending, mark = [mark[0], mark[2]], [0, mark[1], "", None]
    if not pending:
        return [], mark
    older, pending = drain(name, pending)
    return older, [mark[0], mark[1], mark[2], pending]


def fresh(name: str, mark: list | None, baseline: bool) -> tuple[list[str], list | None]:
    try:
        info = os.stat(name)
    except OSError:
        return gone(name, mark)
    size, inode = info.st_size, info.st_ino
    if baseline:
        return [], [size, inode, digest(name, size) or "", None]
    offset, pending = 0, None
    if mark is not None:
        offset, pending = mark[0], (mark[3] if len(mark) > 3 else None)
        if sources.log_start(mark[:2], size, inode) != offset or digest(name, offset) != mark[2]:
            pending, offset = ([offset, mark[2]] if offset > 0 else pending), 0
    end = min(size, offset + MAX_READ)
    if (got := sources.read_lines(name, offset, end)) is None:
        return [], mark
    rows, upto = got
    if upto is None:
        upto = end if end - offset >= MAX_READ else offset
    older, pending = drain(name, pending) if pending else ([], None)
    return older + rows, [upto, inode, digest(name, upto) or "", pending]


def undrained(name: str, mark: list) -> bool:
    if any(mark[3:]):
        return True
    try:
        more = sources.read_lines(name, mark[0], min(os.stat(name).st_size, mark[0] + MAX_READ))
    except OSError:
        return False
    return bool(more and more[1] is not None)


def traceback_end(rows: list[str], begin: int) -> tuple[int, str]:
    j = begin
    while True:
        j += 1
        while j < len(rows) and (not rows[j].strip() or rows[j][0].isspace()):
            j += 1
        if j >= len(rows):
            return len(rows) - 1, ""
        if not (TRACEBACK in rows[j] or CHAINED.match(rows[j])):
            return j, rows[j]


def run_line(line: str) -> dict | None:
    if not line.startswith("{"):
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) and all(k in row for k in lines.FIXED if k != "ts") else None


def run_text(row: dict) -> str:
    said = util.first_line(str(row.get("message") or row.get("error") or ""), 300)
    return f"{row['event']} {row.get('step') or ''}: {said}".strip()


def run_failure(row: dict) -> bool:
    return row["event"] in ("error", "wait") or (row["event"] == "step_end" and row.get("ok") is False)


def own_step(row: dict) -> bool:
    return row["event"] == "step_end" and (row.get("step") == "checks" or row.get("atom") in REPAIR_STEPS)


def ticket_has_live_run(row: dict) -> bool:
    found = state.run_get(str(row["run"])) or {}
    if not (found.get("repo") and found.get("issue")):
        return False
    other = state.running_run(found["repo"], found["issue"])
    return bool(other) and other["id"] != row["run"]


def classify(text: str, row: dict | None) -> str:
    if row and (row["event"] == "wait" or own_step(row)):
        return EXPECTED
    if EXPECTED_TEXT.search(text) or (row and ticket_has_live_run(row)):
        return EXPECTED
    return ORCHESTRATION if ORCHESTRATION_TEXT.search(text) else UNEXPECTED


def idle(rows: list[dict]) -> bool:
    try:
        return bool(rows) and (util.now() - util.parse_iso(str(rows[-1]["ts"]))).total_seconds() > STALL_SECONDS
    except (KeyError, ValueError):
        return False


def tail_of(path) -> tuple[list[dict], list[str]]:
    size = RUN_TAIL_BYTES
    while True:
        try:
            with open(path, "rb") as f:
                end = f.seek(0, 2)
                begin = max(0, end - size)
                f.seek(begin)
                data = f.read()
        except OSError:
            return [], []
        if begin:
            data = data[data.find(b"\n") + 1:]
        rows = lines.whole(data)
        events = {row.get("event") for row in rows}
        known = bool(events & {"step_start", "step_end"}) and "wait" in events
        if not begin or size >= MAX_RUN_TAIL or known or not idle(rows):
            return rows, data.decode("utf-8", "replace").splitlines()[-CONTEXT_LINES * 2:]
        size *= 4


def silent(rows: list[dict]) -> bool:
    open_step = waiting = False
    for row in rows:
        if row.get("event") == "step_start":
            open_step = True
        elif row.get("event") == "step_end":
            open_step = False
        elif row.get("event") == "wait":
            waiting = row.get("phase") == "start"
    return open_step and not waiting and idle(rows)


def process_gone(row: dict) -> bool:
    return not util.lock_is_held(row["lock"]) if row.get("lock") else not util.pid_alive(row.get("pid"))


def cursor_value(name: str):
    row = state.cursor(name)
    return row, (json.loads(row["value"]) if row and row["value"] else None)


class Watch:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.found: dict[tuple, Event] = {}
        self.sources = self.lines = 0
        self.cursors: list[tuple[str, str, bool, str]] = []

    def add(self, kind: str, scope: str, text: str, source: str, repo: str, window: list[str],
            count: int = 1) -> None:
        key = (kind, scope, sources.signature(text))
        if key in self.found:
            self.found[key].count += count
        else:
            self.found[key] = Event(kind, scope, text, source, repo, window, count)

    def consider(self, name: str, rows: list[str], origin: Origin) -> None:
        self.lines += len(rows)
        i = 0
        while i < len(rows):
            line, begin, row = rows[i], i, run_line(rows[i])
            last = i
            i += 1
            if OWN_LINE.search(line):
                continue
            if row:
                if not run_failure(row) or (origin.folder and lines.path(str(row["run"])).exists()):
                    continue
                text = run_text(row)
            elif origin.pattern.search(line) or EXPECTED_TEXT.search(line):
                text = line
                if TRACEBACK in line:
                    last, final = traceback_end(rows, begin)
                    text, i = f"Traceback: {final}".strip(), last + 1
            else:
                continue
            window = rows[max(0, begin - CONTEXT_LINES):last + CONTEXT_LINES + 1]
            self.add(classify(text, row), origin.scope, text, name, origin.repo, window)

    def follow(self, group: str, watched: list[str], origin: Origin) -> None:
        stored, marks = cursor_value(PREFIX + group)
        marks = marks or {}
        for name in dict.fromkeys([*watched, *marks]):
            rows, marks[name] = fresh(name, marks.get(name), stored is None)
            if rows:
                self.consider(name, rows, origin)
        kept = {name: mark for name, mark in marks.items()
                if mark is not None and (name in watched or undrained(name, mark))}
        self.defer(group, json.dumps(kept), util.count(len(watched), "file"))
        self.sources += len(watched)

    def defer(self, name: str, value: str, detail: str) -> None:
        self.cursors.append((PREFIX + name, value, True, detail))

    def run_files(self, since: str | None) -> list[str]:
        ids = [r["id"] for r in state.runs_marked_running()]
        ids += [r["id"] for r in state.runs_since(since)] if since else []
        return [str(lines.path(run_id)) for run_id in dict.fromkeys(ids)]

    def heartbeat(self) -> None:
        stored, kept = cursor_value(PREFIX + "heartbeat")
        kept = kept or {"ts": "", "failing": {}}
        beat = util.read_json(paths.HEARTBEAT_FILE, {})
        if beat.get("ts") and beat["ts"] != kept["ts"]:
            failing = {c["key"]: kept["failing"].get(c["key"], 0) + 1 for c in beat.get("checks", [])
                       if not c.get("ok") and c.get("severity", "fail") == "fail"
                       and not str(c.get("key", "")).startswith(PREFIX)}
            detail = {c["key"]: c.get("detail", "") for c in beat.get("checks", [])}
            for key, beats in failing.items():
                if stored is not None and beats >= RECURRING_BEATS:
                    self.add(HEARTBEAT, "arete", f"heartbeat check {key} failing", "heartbeat",
                             "", [f"{key}: {detail.get(key, '')}"])
            kept = {"ts": beat["ts"], "failing": failing}
        self.defer("heartbeat", json.dumps(kept), f"{len(kept['failing'])} failing")
        self.sources += 1

    def serve(self) -> None:
        stored, before = cursor_value(PREFIX + "serve")
        beat = control.serve_beat()
        now = {f"broken:{k}": f"{k}: loop broken: {v}" for k, v in (beat.get("broken") or {}).items()}
        now |= {f"poll:{k}": f"{k}: poll failed: {v}" for k, v in (beat.get("poll_errors") or {}).items()}
        if stored is not None:
            for key in sorted(set(now) - set(before)):
                self.add(ORCHESTRATION, "arete", now[key], "serve", "", [now[key]])
        self.defer("serve", json.dumps(sorted(now)), f"{len(now)} failing")
        self.sources += 1

    def stalls(self) -> None:
        _, flagged = cursor_value(PREFIX + "stalls")
        flagged = flagged or []
        stalled = []
        for row in state.runs_marked_running():
            if (util.now() - util.parse_iso(row["started"])).total_seconds() <= record.SETTLE_GRACE_SECONDS:
                continue
            rows, texts = tail_of(lines.path(row["id"]))
            why = "its process is gone" if process_gone(row) else "its heartbeats stopped" if silent(rows) else ""
            if not why:
                continue
            stalled.append(row["id"])
            if row["id"] not in flagged:
                head = f"run {row['id']} ({row.get('repo') or '-'}#{row.get('issue') or '-'}) stalled: {why}"
                self.add(STALLED, f"run:{row['id']}", f"run stalled: {why}", f"run {row['id']}", "", [head, *texts])
        self.defer("stalls", json.dumps(stalled), f"{len(stalled)} stalled")

    def store(self) -> tuple[int, int, int]:
        events = [e for e in self.found.values() if e.kind != EXPECTED]
        hidden = record.secrets() if events else ()
        rows = []
        for e in events:
            signature = outbound.redact(sources.signature(record.scrub(e.signature, hidden)))
            if e.kind == ORCHESTRATION:
                signature = signature.removeprefix("<time> ")
            evidence = outbound.redact(record.scrub("\n".join(e.window), hidden))[:MAX_EVIDENCE]
            fingerprint = hashlib.sha1(f"{e.kind}\n{e.scope}\n{signature}".encode()).hexdigest()[:16]
            rows.append((fingerprint, e.kind, e.source, e.repo, signature, e.count, evidence))
        opened = sum(state.commit_scan(rows, self.cursors))
        return opened, len(events) - opened, len(self.found) - len(events)


def said(summary: dict) -> str:
    return (f"{util.count(summary['sources'], 'source')}, {util.count(summary['lines'], 'line')}, "
            f"{util.count(summary['events'], 'event')}: {summary['opened']} opened, {summary['updated']} updated, "
            f"{summary['expected']} expected")


def scan_once(cfg: config.Config) -> dict:
    row, previous = cursor_value(SCAN)
    watch = Watch(cfg)
    watch.follow("logs", [str(p) for p in sorted(paths.LOG_DIR.glob("*.log"))], Origin("arete", folder=True))
    watch.follow("runs", watch.run_files(row["polled"] if row else None), Origin("arete"))
    for source in cfg.oncall.sources:
        files = sorted(glob.glob(str(paths.expand(source.path))))
        pattern = re.compile(source.pattern or sources.LOG_PATTERN)
        watch.follow(f"path:{source.path}", files, Origin(source.path, source.repo, pattern))
    watch.heartbeat()
    watch.serve()
    watch.stalls()
    opened, updated, expected = watch.store()
    summary = {"sources": watch.sources, "lines": watch.lines, "events": len(watch.found), "opened": opened,
               "updated": updated, "expected": expected}
    total = (previous or {}).get("expected", 0) + expected
    state.set_cursor(SCAN, json.dumps({"expected": total, "last": summary}), True, said(summary))
    return summary


def scan(cfg: config.Config) -> dict | None:
    if not cfg.oncall.enabled:
        return None
    with util.lock("oncall") as got:
        if not got:
            return {"skipped": "another scan is running"}
        return scan_once(cfg)


def crashed(text: str) -> None:
    state.set_cursor(SCAN, None, False, text)


def age(stamp: str) -> int:
    return round((util.now() - util.parse_iso(stamp)).total_seconds())


def health() -> tuple[bool, str]:
    row = state.cursor(SCAN)
    if row is None:
        beat = control.serve_beat()
        if beat.get("alive") and beat.get("started") and age(beat["started"]) < STALE_SECONDS:
            return True, "waiting for its first scan"
        return False, "the watcher is on and has never scanned"
    if not row["ok"]:
        return False, f"last scan crashed: {row['detail']}"
    if (seconds := age(row["polled"])) > STALE_SECONDS:
        return False, f"last scan is stale: {seconds}s ago, limit {STALE_SECONDS}s"
    return True, f"last scan {seconds}s ago"


def status(cfg: config.Config) -> dict:
    row, value = cursor_value(SCAN)
    return {"enabled": cfg.oncall.enabled, "last_scan": row["polled"] if row else "", "ok": bool(row and row["ok"]),
            "detail": row["detail"] if row else "", "expected": (value or {}).get("expected", 0)}
