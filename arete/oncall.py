from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path
from string import Template

from . import config, control, jev, lines, outbound, paths, record, sources, state, util

UNEXPECTED, HEARTBEAT, STALLED, ORCHESTRATION, EXPECTED = "unexpected", "heartbeat", "stalled", "orchestration", "expected"
WAKE, IGNORE, UNSURE = "wake", "ignore", "unsure"
MAX_ASKS = 20
TWINS_OFFERED = 50
RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 3600
RECOUNT_FACTOR = 10
SHAPES_KEPT = 64
ACTIONABLE = ("Is this an actionable failure that a code or config change would repair, rather than noise such as an "
              "expected retry, a blip that resolved itself, a user mistake or routine output?")
VISION = "The owner describes the app's vision as: {text}\nWould repairing the failure the text reports fit that vision?"
SCOPE = ("The owner allows repairs only within this scope: {text}\nIs the repair of the failure the text reports "
         "inside that scope?")
TWIN = ("Which open incident listed in the text is the same underlying problem as the new symptom, even when it is "
        "worded differently? Answer new when none is.")
NOT_ACTIONABLE = {"actionable": "noise, not an actionable failure",
                  "vision": "repairing it does not fit the app's vision",
                  "scope": "the repair is outside the allowed scope"}
PREFIX = "oncall:"
SCAN = PREFIX + "scan"
SWITCH = PREFIX + "switch"
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
    r"|checks (?:still failing|failed on attempt)|the checks failed after"
    r"|\b(?:fix\d+|review_fix\w*|ci_\w*fix\w*|(?:check|review|ci)_repair\w*): FAILED", re.IGNORECASE)
ORCHESTRATION_TEXT = re.compile(r"tick failed:|poll failed:|dropped queue item|sweeping left-over VMs failed"
                                r"|new code does not import|: loop broken:")
OWN_LINE = re.compile(r"\bon-call\b")
URL_ONLY_END = re.compile(r"\] done: failed https?://\S+$")
TRACEBACK = "Traceback (most recent call last):"
CHAINED = re.compile(r"During handling of the above exception|The above exception was the direct cause")
REPAIR_STEPS = ("check_repair", "review_repair", "ci_repair")
OWN_SOURCES = ("heartbeat", "serve")
CHECK_SECONDS = 300
ESCALATIONS = {
    "asks": "on-call incident {id}: repair run {run} in {repo} asked a question and waits for your answer",
    "held": "on-call incident {id}: the repair's change from run {run} in {repo} is held for your review",
    "policy": "on-call incident {id}: no repair route in {repo} passes the routing policy",
    "attempts": "on-call incident {id}: {n} repair attempts in {repo} did not fix it",
    "budget": "on-call incident {id}: the repair budget of {budget} USD in {repo} is used up",
}
RUN_PREFIX = re.compile(r"\[(\S+) (t-[0-9a-f]+)\]")


@dataclass(frozen=True)
class Origin:
    scope: str
    home: frozenset[tuple[str, str]]
    repo: str = ""
    pattern: re.Pattern = FAILURE
    folder: bool = False
    listed: bool = False


@dataclass
class Event:
    kind: str
    scope: str
    signature: str
    source: str
    repo: str
    window: list[str]
    count: int = 1
    rule: frozenset[tuple[str, str]] = frozenset()
    legacy: str = ""


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


def stamp(name: str) -> list | None:
    try:
        info = os.stat(name + ".1")
    except OSError:
        return None
    return [info.st_ino, info.st_size, info.st_mtime_ns]


def whole_digest(name: str, end: int) -> str | None:
    try:
        with open(name, "rb") as f:
            h, left = hashlib.sha1(), end
            while left > 0 and (chunk := f.read(min(left, 1 << 20))):
                h.update(chunk)
                left -= len(chunk)
    except OSError:
        return None
    return h.hexdigest() if left == 0 else None


def still_holds(name: str, size: int, older: list, inode: int, same: int) -> bool:
    head = min(older[1], MAX_READ)
    return (inode == same and size >= older[1]
            and whole_digest(name, head) == whole_digest(name + ".1", head)
            and digest(name, older[1]) == digest(name + ".1", older[1]))


def untouched(info: os.stat_result, older: list) -> bool:
    return info.st_mtime_ns < older[2] or (info.st_mtime_ns == older[2] and info.st_size == older[1])


def parts(mark: list, now: list | None) -> tuple[list | None, list | None]:
    return (mark[3] if len(mark) > 3 else None), (mark[4] if len(mark) > 4 else now)


def gone(name: str, mark: list | None) -> tuple[list[str], list[str], list | None]:
    if mark is None:
        return [], [], mark
    now = stamp(name)
    pending, seen = parts(mark, now)
    if not pending and now:
        if mark[0] == 0:
            remainder = now != seen
        else:
            remainder = digest(name + ".1", mark[0]) == mark[2]
        if remainder:
            pending, mark = [mark[0], mark[2]], [0, mark[1], "", None]
    older, pending = drain(name, pending) if pending else ([], None)
    return older, [], [mark[0], mark[1], mark[2], pending, now]


def fresh(name: str, mark: list | None, baseline: bool) -> tuple[list[str], list[str], list | None]:
    try:
        info = os.stat(name)
    except OSError:
        return gone(name, mark)
    size, inode = info.st_size, info.st_ino
    now = stamp(name)
    if baseline:
        return [], [], [size, inode, digest(name, size) or "", None, now]
    offset, pending, seen, noted = 0, None, now, now
    if mark is not None:
        offset = mark[0]
        pending, seen = parts(mark, now)
        if sources.log_start(mark[:2], size, inode) != offset or digest(name, offset) != mark[2]:
            pending, offset = ([offset, mark[2]] if offset > 0 else pending), 0
        elif now and now != seen and not pending and now[1] > offset and digest(name + ".1", offset) == mark[2]:
            copied = still_holds(name, size, now, inode, mark[1])
            if copied and untouched(info, now):
                noted = seen
            else:
                pending, offset = [offset, mark[2]], 0
    end = min(size, offset + MAX_READ)
    if (got := sources.read_lines(name, offset, end)) is None:
        return [], [], mark
    rows, upto = got
    if upto is None:
        upto = end if end - offset >= MAX_READ else offset
    older, pending = drain(name, pending) if pending else ([], None)
    return older, rows, [upto, inode, digest(name, upto) or "", pending, noted]


def undrained(name: str, mark: list) -> bool:
    if parts(mark, None)[0]:
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


def same_owner(line: str, row: dict | None) -> bool:
    other = run_line(line)
    if row is None:
        return other is None
    return other is not None and other["run"] == row["run"]


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


def ask_jev(items, cfg) -> list[jev.Reply]:
    return jev.ask_many(items, cfg, trace=("", "oncall jev"))


def start_task(repo: str, prompt: str = "", title: str = "", ticket: str = "") -> dict:
    from . import tasks

    return tasks.start(repo, prompt, title, "fix", ticket)


def stop_run(run_id: str) -> dict:
    return control.stop_run(run_id)


def switched_off() -> bool:
    return cursor_value(SWITCH)[1] == "off"


def switch(on: bool) -> dict:
    word = "on" if on else "off"
    with util.lock("oncall", wait=True):
        state.set_cursor(SWITCH, json.dumps(word), True, f"wakes {word}")
        for row in state.woken_incidents():
            state.add_incident_note(row["id"], f"wakes and rechecks switched {word} by the owner")
    return {"wakes": word}


def run_check(command: str, cwd) -> tuple[bool, str]:
    done = util.sh(command, cwd=cwd, timeout=CHECK_SECONDS)
    return done.returncode == 0, f"exit {done.returncode}"


def raise_alert(cfg: config.Config, key: str, title: str, line: str) -> None:
    from . import alerts

    alerts.raise_alert(cfg.alert_repo, key, title, line)


def clear_alert(cfg: config.Config, key: str) -> bool:
    from . import alerts

    alerts.clear_alert(cfg.alert_repo, key)
    return state.alert_get(key) is None


def route_outlook(cfg: config.Config, repo: config.Repo, setting) -> list:
    from . import routing

    return routing.outlook(cfg, repo, "local", setting.model, setting.runner, list(setting.tools), setting.denied)


def fenced(data) -> str:
    return "```json\n" + json.dumps(data, indent=2, default=str).replace("```", "'''") + "\n```"


def asked(items: list, cfg: config.Config) -> list[jev.Reply]:
    try:
        replies = ask_jev(items, cfg)
        if len(replies) == len(items):
            return replies
        error = "its reply count did not match the questions asked"
    except Exception as e:
        error = f"{type(e).__name__}: {util.first_line(str(e), 200)}"
    return [jev.Reply(error=error) for _ in items]


def thresholds(cfg: config.Config) -> tuple[float, float]:
    return (cfg.jev.confidence if cfg.oncall.confidence is None else cfg.oncall.confidence,
            cfg.jev.injection if cfg.oncall.injection is None else cfg.oncall.injection)


def entry_policy(cfg: config.Config, entry: config.OncallSource | None = None) -> tuple[str, str]:
    if entry is None:
        return cfg.oncall.vision, cfg.oncall.scope
    return entry.vision or cfg.oncall.vision, entry.scope or cfg.oncall.scope


def identity(name: str) -> tuple[int, int] | None:
    try:
        info = os.stat(name)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def source_files(entry: config.OncallSource) -> list[str]:
    return sorted(glob.glob(str(paths.expand(entry.path))))


def snapshot(cfg: config.Config) -> tuple[list, dict, dict]:
    files = [(s, source_files(s)) for s in cfg.oncall.sources]
    named: dict[str, tuple[int, int] | str] = {}
    entries: dict[tuple[int, int] | str, list[config.OncallSource]] = {}
    for entry, names in files:
        for name in names:
            entries.setdefault(named.setdefault(name, identity(name) or name), []).append(entry)
        for copy in glob.glob(str(paths.expand(entry.path)) + ".1"):
            if (key := identity(copy)) is not None:
                entries.setdefault(key, []).append(entry)
    return files, named, entries


class Policies:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.files, self.named, self.entries = snapshot(cfg)
        self.every = frozenset({entry_policy(cfg), *(entry_policy(cfg, s) for s in cfg.oncall.sources)})

    def under(self, found: list[config.OncallSource]) -> frozenset[tuple[str, str]]:
        return frozenset({entry_policy(self.cfg, s) for s in found} or {entry_policy(self.cfg)})

    def untold(self) -> frozenset[tuple[str, str]]:
        return self.every if len(self.every) == 1 else frozenset()

    def key(self, name: str, listed: bool) -> tuple[int, int] | str | None:
        now = identity(name)
        if listed and self.named.get(name) != (now or name):
            _, self.named, self.entries = snapshot(self.cfg)
        return self.named.get(name) or now

    def matched(self, found: list[config.OncallSource], listed: bool) -> frozenset[tuple[str, str]]:
        return self.untold() if listed and not found else self.under(found)

    def of_file(self, name: str, listed: bool = False) -> frozenset[tuple[str, str]]:
        key = self.key(name, listed)
        return self.untold() if key is None else self.matched(self.entries.get(key, []), listed)

    def of_copy(self, name: str, listed: bool = False) -> frozenset[tuple[str, str]]:
        key = self.key(name, listed)
        if key is None:
            return self.untold()
        copy = identity(name + ".1")
        return self.matched([*self.entries.get(key, []), *(self.entries.get(copy, []) if copy else [])], listed)

    def of(self, source: str) -> frozenset[tuple[str, str]]:
        if os.path.isabs(source) or source in self.named or identity(source):
            return self.of_file(source)
        return frozenset({entry_policy(self.cfg)})

    def standing(self, row: dict) -> frozenset[tuple[str, str]] | None:
        now, rule = self.of(row["source"]), recorded(row)
        return now if rule is None or rule == now else None

    def read_under(self, row: dict) -> frozenset[tuple[str, str]] | None:
        rule = recorded(row)
        return self.every if rule is None and len(self.every) == 1 else rule

    def judged(self, row: dict) -> tuple[str, str] | None:
        rule = self.read_under(row)
        return next(iter(rule)) if rule is not None and len(rule) == 1 and rule <= self.every else None


def packed(rule: frozenset[tuple[str, str]]) -> str:
    return json.dumps(sorted(rule))


def unpacked(text: str) -> frozenset[tuple[str, str]]:
    return frozenset((str(vision), str(scope)) for vision, scope in json.loads(text))


def recorded(row: dict) -> frozenset[tuple[str, str]] | None:
    return None if row.get("policies") is None else unpacked(row["policies"])


def bounded(scope: str, rule: frozenset[tuple[str, str]]) -> str:
    return f"{scope}\n{packed(rule)}"


def fingerprint_of(kind: str, scope: str, signature: str) -> str:
    return hashlib.sha1(f"{kind}\n{scope}\n{signature}".encode()).hexdigest()[:16]


def questions(policies: Policies, incident: dict) -> dict[str, dict]:
    vision, scope = policies.judged(incident) or ("", "")
    asking = {"injection": jev.yes_no(jev.INJECTION), "actionable": jev.yes_no(ACTIONABLE)}
    if vision:
        asking["vision"] = jev.yes_no(VISION.format(text=vision))
    if scope:
        asking["scope"] = jev.yes_no(SCOPE.format(text=scope))
    return asking


def subject(incident: dict) -> str:
    return fenced({"class": incident["class"], "signature": incident["signature"], "count": incident["count"],
                   "evidence": incident["evidence"]})


def shapes(evidence: str) -> set[str]:
    return {hashlib.sha1(sources.signature(line).encode()).hexdigest()[:8]
            for line in evidence.splitlines() if FAILURE.search(line)}


def changed(row: dict) -> bool:
    return (row["count"] >= max(row["decided_count"], 1) * RECOUNT_FACTOR
            or bool(shapes(row["evidence"]) - set(json.loads(row["shapes"] or "[]"))))


def owner_run(row: dict) -> str:
    if row["class"] == STALLED:
        return ""
    where = Path(row["source"])
    if where.name == lines.FILE and where.parent.parent == paths.RUNS_DIR:
        run_id = where.parent.name
    else:
        run_id = next((str(r["run"]) for r in map(run_line, str(row.get("evidence") or "").splitlines()) if r), "")
    if not run_id:
        return ""
    found = state.run_get(run_id) or {}
    if found.get("status") == "running" and not process_gone(found):
        return run_id
    if found.get("repo") and found.get("issue"):
        other = state.running_run(found["repo"], found["issue"])
        if other and other["id"] != run_id:
            return other["id"]
    return ""


def injected(a: jev.Answer) -> str:
    return f"the evidence reads as instructions to an agent (p {a.yes:.2f})"


def verdict(reply: jev.Reply, asking: dict, cfg: config.Config, policies: Policies,
            row: dict) -> tuple[str, float, str, dict, int]:
    line, injection_line = thresholds(cfg)
    if reply.error or set(asking) - set(reply.answers):
        return "", 0.0, reply.error or "its reply left questions out", {}, 0
    got = {name: reply.answers[name] for name in asking}
    answers = {name: {"value": a.value, "confidence": a.confidence, "p": a.yes} for name, a in got.items()}
    if got["injection"].yes >= injection_line:
        return IGNORE, got["injection"].confidence, injected(got["injection"]), answers, 1
    for name, why in NOT_ACTIONABLE.items():
        if name in got and got[name].sure(line) and got[name].value is False:
            return IGNORE, got[name].confidence, why, answers, 0
    for name, a in got.items():
        if not a.sure(line):
            return UNSURE, a.confidence, f"{name} is uncertain ({a.note()})", answers, 0
    least = min(a.confidence for a in got.values())
    rule = policies.read_under(row)
    if rule is None:
        why = ("it was opened before incidents recorded the vision and scope their lines were read under, and the "
               "config holds more than one; the owner decides")
        return UNSURE, least, why, answers, 0
    if not rule:
        why = ("its file is gone or no [[oncall.source]] glob lists it, so which vision and scope apply cannot be "
               "told; the owner decides")
        return UNSURE, least, why, answers, 0
    if not rule <= policies.every:
        why = "the vision or scope its file was under when read is no longer in the config, so the owner decides"
        return UNSURE, least, why, answers, 0
    if len(rule) > 1:
        why = "more than one [[oncall.source]] vision or scope applies to this file, so the owner decides"
        return UNSURE, least, why, answers, 0
    for name, text in zip(("vision", "scope"), next(iter(rule)), strict=True):
        if not text:
            return UNSURE, least, f"no [oncall] {name} is set, so the owner decides", answers, 0
    return WAKE, least, "actionable, fits the vision and is in scope", answers, 0


def due(row: dict, now: str) -> bool:
    return (not row["injection"] and row["class"] != EXPECTED and (not row["retry_at"] or row["retry_at"] <= now)
            and (row["decision"] == "" or (row["decision"] == IGNORE and changed(row))))


def origin_repo(source: str, repo: str) -> str:
    if repo:
        return repo
    where = Path(source)
    if where.name == lines.FILE and where.parent.parent == paths.RUNS_DIR:
        run_id = where.parent.name
    else:
        run_id = source.removeprefix("run ") if source.startswith("run ") else ""
    return ((state.run_get(run_id) or {}).get("repo") or "") if run_id else ""


def refused(cfg: config.Config, repo: str) -> str:
    if not cfg.jev.enabled:
        return "jev is off in the config"
    found = next((r for r in cfg.repos if r.name == repo), None)
    return "" if cfg.jev_allowed([found]) else f"JEV is not allowed for repo {repo}"


def failed(row: dict, reason: str) -> None:
    wait = min(RETRY_SECONDS * 2 ** min(row["tries"], 30), MAX_RETRY_SECONDS)
    fields = {"tries": row["tries"] + 1, "retry_at": util.iso(util.now() + timedelta(seconds=wait))}
    note = ""
    if row["decision"] == "":
        fields["reason"] = reason
        note = f"undecided: {reason}" if reason != row["reason"] else ""
    elif row["tries"] == 0:
        note = f"could not judge it again: {reason}"
    state.update_incident(row["id"], note, **fields)


def settle(row: dict, outcome: tuple[str, float, str, dict, int]) -> None:
    decision, confidence, reason, answers, flagged = outcome
    seen = sorted(shapes(row["evidence"]))
    kept = list(dict.fromkeys([*seen, *json.loads(row["shapes"] or "[]")]))[:SHAPES_KEPT]
    detail = ", ".join(f"{name} {'yes' if a['value'] else 'no'} {a['confidence']:.2f}" for name, a in answers.items())
    state.update_incident(row["id"], f"decided {decision} (confidence {confidence:.2f}): {reason}; {detail}",
                          decision=decision, confidence=confidence, reason=reason, answers=json.dumps(answers),
                          injection=flagged, decided=util.iso(), tries=0, retry_at="", decided_count=row["count"],
                          shapes=json.dumps(kept))


def decide(cfg: config.Config, policies: Policies | None = None, budget: int = MAX_ASKS) -> dict:
    policies = policies or Policies(cfg)
    counts: dict[str, int] = {}
    now = util.iso()
    ask = []
    for row in state.incidents_to_decide():
        if not due(row, now):
            continue
        if owner := owner_run(row):
            if row["decision"] == "" and (why := f"run {owner} owns it and is still live") != row["reason"]:
                state.update_incident(row["id"], reason=why)
        elif why := refused(cfg, origin_repo(row["source"], row["repo"])):
            failed(row, why)
            counts["undecided"] = counts.get("undecided", 0) + (row["decision"] == "")
        else:
            ask.append(row)
    ask = ask[:max(budget, 0)]
    asking = [questions(policies, row) for row in ask]
    replies = asked([(subject(row), q) for row, q in zip(ask, asking, strict=True)], cfg) if ask else []
    for row, q, reply in zip(ask, asking, replies, strict=True):
        outcome = verdict(reply, q, cfg, policies, row)
        if outcome[0]:
            settle(row, outcome)
            counts[outcome[0]] = counts.get(outcome[0], 0) + 1
        else:
            failed(row, outcome[2])
            counts["undecided"] = counts.get("undecided", 0) + (row["decision"] == "")
    return {name: n for name, n in counts.items() if n}


def decision_line(row: dict) -> str:
    if not row["decision"]:
        text = f"undecided: {row['reason']}" if row["reason"] else "undecided"
        return text + (f", next try {row['retry_at']}" if row["retry_at"] else "")
    mark = " (injection)" if row["injection"] else ""
    return f"{row['decision']} {row['confidence']:.2f}{mark}: {row['reason']}"


def known_twins(cfg: config.Config, policies: Policies, rows: list[tuple], known: dict[str, dict],
                budget: int = MAX_ASKS) -> tuple[list, dict, int]:
    open_rows = [r for r in state.open_incidents() if not r["injection"]]
    line, injection_line = thresholds(cfg)
    fresh, candidates = [], []
    homes = {r["id"]: origin_repo(r["source"], r["repo"]) for r in open_rows}
    rules = {r["id"]: policies.standing(r) for r in open_rows}
    for row in rows:
        home = origin_repo(row[2], row[3])
        rule = unpacked(row[7])
        same = [r for r in open_rows if homes[r["id"]] == home and rules[r["id"]] == rule][:TWINS_OFFERED]
        if (row[0] not in known and rule and same and not refused(cfg, home)
                and not owner_run({"class": row[1], "source": row[2], "evidence": row[6]})):
            fresh.append(row)
            candidates.append(same)
    fresh, candidates = fresh[:budget], candidates[:budget]
    items = []
    for row, same in zip(fresh, candidates, strict=True):
        options = {"new": "a different problem from every open incident listed in the text",
                   **{f"i{r['id']}": f"the same problem as open incident i{r['id']} listed in the text" for r in same}}
        text = fenced({"new symptom": {"class": row[1], "signature": row[4], "evidence": row[6]},
                       "open incidents": {f"i{r['id']}": {"class": r["class"], "signature": r["signature"]}
                                          for r in same}})
        items.append((text, {"twin": jev.pick(TWIN, options), "injection": jev.yes_no(jev.INJECTION)}))
    matched, marks = [], {}
    for row, (_, q), reply in zip(fresh, items, asked(items, cfg) if items else [], strict=True):
        a, flagged = reply.answers.get("twin"), reply.answers.get("injection")
        if flagged and flagged.yes >= injection_line:
            answers = {"injection": {"value": flagged.value, "confidence": flagged.confidence, "p": flagged.yes}}
            fields = {"decision": IGNORE, "confidence": flagged.confidence, "reason": injected(flagged),
                      "answers": json.dumps(answers), "injection": 1, "decided": util.iso(),
                      "decided_count": row[5]}
            said = (f"decided {IGNORE} (confidence {flagged.confidence:.2f}): {injected(flagged)}; "
                    f"injection yes {flagged.confidence:.2f}")
            marks[row[0]] = (fields, said)
        elif (a and flagged and flagged.value is False and flagged.sure(line) and a.value != "new"
              and a.value in q["twin"]["criteria"] and a.sure(line)):
            note = (f"JEV matches a differently worded symptom to this incident ({a.note()}): "
                    f"{outbound.plain(row[4], 200)}")
            matched.append((row[0], int(str(a.value)[1:]), note))
    return matched, marks, len(items)


def serve_failures(beat: dict) -> dict[str, str]:
    now = {f"broken:{k}": f"{k}: loop broken: {v}" for k, v in (beat.get("broken") or {}).items()}
    return now | {f"poll:{k}": f"{k}: poll failed: {v}" for k, v in (beat.get("poll_errors") or {}).items()}


class Watch:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.policies = Policies(cfg)
        self.found: dict[tuple, Event] = {}
        self.sources = self.lines = self.asks = 0
        self.cursors: list[tuple[str, str, bool, str]] = []
        self.owned = {(r["repo"], r["task"]): r["incident"] for r in state.incident_repairs() if r["task"]}
        self.runs: dict[str, dict] = {}
        self.quiet: dict[int, int] = {}

    def repairing(self, run_id: str) -> int:
        if not self.owned:
            return 0
        if run_id not in self.runs:
            self.runs[run_id] = state.run_get(run_id) or {}
        found = self.runs[run_id]
        return self.owned.get((found.get("repo"), found.get("issue")), 0)

    def hushed(self, incident: int) -> bool:
        if incident:
            self.quiet[incident] = self.quiet.get(incident, 0) + 1
        return bool(incident)

    def add(self, kind: str, scope: str, text: str, source: str, repo: str, window: list[str],
            count: int = 1, rule: frozenset[tuple[str, str]] | None = None, legacy: str = "") -> None:
        key = (kind, scope, sources.signature(text))
        if key in self.found:
            self.found[key].count += count
        else:
            read = self.policies.of(source) if rule is None else rule
            self.found[key] = Event(kind, scope, text, source, repo, window, count, read, legacy or scope)

    def consider(self, name: str, rows: list[str], origin: Origin, rule: frozenset[tuple[str, str]]) -> None:
        self.lines += len(rows)
        policy_scope = origin.scope if rule == origin.home else bounded(origin.scope, rule)
        i = 0
        while i < len(rows):
            line, begin, row = rows[i], i, run_line(rows[i])
            last = i
            i += 1
            if OWN_LINE.search(line) or URL_ONLY_END.search(line):
                continue
            if row:
                if not run_failure(row) or (origin.folder and lines.path(str(row["run"])).exists()):
                    continue
                if self.hushed(self.repairing(str(row["run"]))):
                    continue
                text = run_text(row)
            elif (origin.pattern.search(line) or EXPECTED_TEXT.search(line)
                  or (origin.folder and ORCHESTRATION_TEXT.search(line))):
                if (own := RUN_PREFIX.search(line)) and self.hushed(self.owned.get(own.groups(), 0)):
                    continue
                text = line
                if TRACEBACK in line:
                    last, final = traceback_end(rows, begin)
                    text, i = f"Traceback: {final}".strip(), last + 1
            else:
                continue
            near = rows[max(0, begin - CONTEXT_LINES):last + CONTEXT_LINES + 1]
            window = [r for r in near if same_owner(r, row)]
            repo, scope = origin.repo, policy_scope
            if row and not repo:
                repo = (state.run_get(str(row["run"])) or {}).get("repo") or ""
                if repo:
                    scope = f"{policy_scope}:{repo}"
            self.add(classify(text, row), scope, text, name, repo, window, rule=rule, legacy=origin.scope)

    def follow(self, group: str, watched: list[str], origin: Origin, relist=None) -> None:
        stored, marks = cursor_value(PREFIX + group)
        marks = marks or {}
        for name in dict.fromkeys([*watched, *marks]):
            older, rows, marks[name] = fresh(name, marks.get(name), stored is None)
            if older:
                self.consider(name, older, origin, self.policies.of_copy(name, origin.listed))
            if rows:
                self.consider(name, rows, origin, self.policies.of_file(name, origin.listed))
        kept = {name: mark for name, mark in marks.items()
                if mark is not None and (name in watched or undrained(name, mark))}
        if relist and (dropped := {name for name, mark in marks.items() if mark is not None} - kept.keys()):
            kept |= {name: marks[name] for name in dropped.intersection(relist())}
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
        now = serve_failures(beat)
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
            if row["id"] not in flagged and not self.hushed(self.owned.get((row.get("repo"), row.get("issue")), 0)):
                head = f"run {row['id']} ({row.get('repo') or '-'}#{row.get('issue') or '-'}) stalled: {why}"
                self.add(STALLED, f"run:{row['id']}", f"run stalled: {why}", f"run {row['id']}", "", [head, *texts])
        self.defer("stalls", json.dumps(stalled), f"{len(stalled)} stalled")

    def store(self) -> tuple[int, int, int]:
        events = [e for e in self.found.values() if e.kind != EXPECTED]
        hidden = record.secrets() if events else ()
        held = state.known_fingerprints() if events else {}
        rows = []
        for e in events:
            signature = outbound.redact(sources.signature(record.scrub(e.signature, hidden)))
            if e.kind == ORCHESTRATION:
                signature = signature.removeprefix("<time> ")
            evidence = outbound.redact(record.scrub("\n".join(e.window), hidden))[:MAX_EVIDENCE]
            scope = e.scope
            fingerprint = fingerprint_of(e.kind, scope, signature)
            older = fingerprint_of(e.kind, e.legacy, signature)
            if fingerprint not in held and older in held and self.inherits(held[older], e):
                fingerprint = older
            while fingerprint in held and self.policies.standing(held[fingerprint]) != e.rule:
                scope = bounded(scope, e.rule)
                fingerprint = fingerprint_of(e.kind, scope, signature)
            rows.append((fingerprint, e.kind, e.source, e.repo, signature, e.count, evidence, packed(e.rule)))
        matched, marks, self.asks = known_twins(self.cfg, self.policies, rows, held)
        opened = sum(state.commit_scan(rows, self.cursors, matched, marks))
        for incident, n in self.quiet.items():
            state.add_incident_note(incident, f"its repair wrote {util.count(n, 'failure line')}; none opened an incident")
        return opened, len(events) - opened, len(self.found) - len(events)

    def inherits(self, row: dict, e: Event) -> bool:
        return (row["policies"] is None and self.policies.standing(row) == e.rule
                and origin_repo(row["source"], row["repo"]) == origin_repo(e.source, e.repo))


def own_source(source: str) -> bool:
    inside = [paths.LOG_DIR, paths.RUNS_DIR]
    return source in OWN_SOURCES or source.startswith("run ") or any(Path(source).is_relative_to(p) for p in inside)


def repair_repo(cfg: config.Config, row: dict) -> str:
    return cfg.arete_repo if own_source(row["source"]) else row["repo"]


def repair_title(incident_id: int) -> str:
    return f"On-call incident {incident_id}: repair"


def since(row: dict) -> int:
    return int(row.get("rearmed") or 0)


def attempts(row: dict) -> list[dict]:
    return [r for r in row["repairs"] if r["task"] and r["id"] > since(row)]


def moved(row: dict, phase: str, waiting: str = "", note: str = "", **fields) -> None:
    if (row["phase"], row["waiting"]) == (phase, waiting) and not fields:
        return
    state.update_incident(row["id"], note or (f"{phase}: {waiting}" if waiting else phase),
                          phase=phase, waiting=waiting, **fields)


def alert_key(incident_id: int) -> str:
    return f"{PREFIX}incident:{incident_id}"


def escalate(cfg: config.Config, row: dict, kind: str, run: str = "") -> None:
    latest = state.incident(row["id"])
    if latest["alerted"]:
        return
    line = outbound.sanitize(ESCALATIONS[kind].format(
        id=row["id"], repo=repair_repo(cfg, row) or "-", run=run or "-", n=len(attempts(latest)),
        budget=f"{cfg.oncall.incident_budget_usd:.2f}"))
    raise_alert(cfg, alert_key(row["id"]), f"on-call incident {row['id']} needs you", line)
    state.update_incident(row["id"], f"escalated once: {line}", alerted=1)


def hold(cfg: config.Config, row: dict, why: str, kind: str, run: str) -> None:
    moved(row, "waiting", why)
    escalate(cfg, row, kind, run)


def end_repair(repair: dict, outcome: str) -> None:
    state.set_repair(repair["id"], ended=util.iso(), outcome=outcome)


def lose(cfg: config.Config, row: dict, why: str) -> None:
    used = len(attempts(state.incident(row["id"])))
    moved(row, "waiting", why, f"attempt failed: {why}; {used} of {cfg.oncall.max_attempts} attempts used", landed="")


def check_for(cfg: config.Config, policies: Policies, row: dict) -> tuple[str, Path] | None:
    found = policies.entries.get(policies.key(row["source"], False), []) or [
        e for e in cfg.oncall.sources if fnmatch.fnmatchcase(row["source"], str(paths.expand(e.path)))]
    for entry in found:
        if entry.check:
            return entry.check, Path(cfg.repo(entry.repo).path if entry.repo else Path(row["source"]).parent)
    return None


def reread(cfg: config.Config, policies: Policies, row: dict) -> tuple[bool, str] | None:
    head = (row["evidence"].splitlines() or [""])[0]
    if row["class"] == HEARTBEAT:
        beat = util.read_json(paths.HEARTBEAT_FILE, {})
        if not beat.get("ts") or beat["ts"] <= row["landed"]:
            return None
        mine = [c for c in beat.get("checks", []) if c.get("key") == head.partition(": ")[0]]
        if not mine:
            return False, f"heartbeat read of {beat['ts']} has no result for the check"
        return all(c.get("ok") for c in mine), f"heartbeat read of {beat['ts']}"
    if row["class"] == STALLED:
        found = state.run_get(row["source"].removeprefix("run ")) or {}
        gone = found.get("status") != "running" or not (process_gone(found) or silent(tail_of(lines.path(found["id"]))[0]))
        return gone, f"run {found.get('id', '-')} {found.get('status', 'is unknown')}"
    if row["source"] == "serve":
        beat = control.serve_beat()
        failing = any(text.partition(": ")[0] == head.partition(": ")[0] for text in serve_failures(beat).values())
        return bool(beat.get("alive")) and not failing, "serve's beat"
    if found := check_for(cfg, policies, row):
        ok, detail = run_check(*found)
        return ok, f"check `{found[0]}` ({detail})"
    waited = util.parse_iso(row["landed"]) + timedelta(minutes=cfg.oncall.cooldown_minutes)
    return (True, f"{cfg.oncall.cooldown_minutes:g} minutes without a recurrence") if util.now() >= waited else None


def recover(cfg: config.Config, row: dict, what: str) -> None:
    text = f"{what} passed at {util.iso()}; no recurrence since the repair landed at {row['landed']}"
    state.update_incident(row["id"], f"resolved: {text}", state="resolved", phase="", waiting="", recovery=text)


def recheck(cfg: config.Config, policies: Policies, row: dict) -> None:
    if row["last_seen"] > row["landed"]:
        return lose(cfg, row, "recheck failed: the signature recurred after the repair landed")
    found = reread(cfg, policies, row)
    if found is None:
        return
    ok, detail = found
    if ok:
        return recover(cfg, row, detail)
    lose(cfg, row, f"recheck failed: {detail}")


def cooling(cfg: config.Config, row: dict) -> str:
    ended = max((r["ended"] for r in row["repairs"] if r["ended"] and r["id"] > since(row)), default="")
    if not ended:
        return ""
    until = util.iso(util.parse_iso(ended) + timedelta(minutes=cfg.oncall.cooldown_minutes))
    return until if until > util.iso() else ""


def spent(cfg: config.Config, repairs: list[dict]) -> float:
    ids = [r["id"] for x in repairs if x["task"] for r in state.ticket_runs(x["repo"], x["task"], limit=50)]
    return sum(state.paid_by_run(ids, local_providers=cfg.local_providers).values())


def last_run_id(repair: dict) -> str:
    return next((r["id"] for r in state.ticket_runs(repair["repo"], repair["task"], limit=1)), "-")


def repair_line(row: dict) -> str:
    made = len([r for r in row.get("repairs", []) if r["task"]])
    parts = [row["phase"] or "", row["waiting"], f"{made} attempt{'' if made == 1 else 's'}" if made else ""]
    if last := next((r for r in reversed(row.get("repairs", [])) if r["task"]), None):
        parts.append(f"task {last['task']}")
    if row.get("recovery"):
        parts.append(f"recovered: {row['recovery']}")
    return "; ".join(p for p in parts if p)


def route_refusal(cfg: config.Config, repo: config.Repo) -> tuple[str, bool]:
    from . import devices, routing, runner, steps

    try:
        layer = config.load_layer(repo.layer_dir)
        modes = routing.modes_for(cfg, repo)
        for role in ("plan", "build", *(("verify",) if layer.verify else ()), "review", *REPAIR_STEPS):
            setting = steps.resolve(cfg, layer, role, "fix")
            which, name = runner.chosen(setting.model, setting.runner, cfg)
            if which == runner.REPLAY:
                continue
            request = runner.request_for(cfg, repo, which, name, None, True,
                                         {"tools": list(setting.tools), "disallowed_tools": list(setting.denied)})
            if why := routing.fit_refusal(request, devices.LOCAL, modes):
                return f"{role}: {why}", True
            routes = route_outlook(cfg, repo, setting)
            if any(c.state == "ready" for c in routes):
                continue
            if words := routing.held_words(routes):
                return f"{role}: {words}", False
            if blocked := next((c for c in routes if c.state == "policy"), None):
                return f"{role}: {blocked.detail}", True
            if routing.budget_blocked(routes):
                return f"{role}: {next(c.detail for c in routes if c.state == 'budget_spent')}", False
            details = "; ".join(sorted({c.detail for c in routes if c.detail})) or "no route was found"
            return f"{role}: {util.first_line(details, 200)}", False
    except (ValueError, TypeError, OSError) as e:
        return util.first_line(str(e), 200), False
    return "", False


def repair_prompt(row: dict, repairs: list[dict], limit: int) -> str:
    earlier = "".join(f"- attempt {n}: task {r['task']}, run {last_run_id(r)}, ended {r['outcome'] or 'unfinished'}\n"
                      for n, r in enumerate(repairs, 1))
    return Template((paths.PROMPTS_DIR / "oncall_repair.md").read_text()).safe_substitute(
        incident=row["id"], kind=row["class"], source=outbound.plain(row["source"], 200), count=row["count"],
        first_seen=row["first_seen"], last_seen=row["last_seen"], attempt=len(repairs) + 1,
        max_attempts=limit, earlier=f"Earlier attempts:\n{earlier}" if earlier else "", evidence=subject(row))


def exhausted(cfg: config.Config, row: dict) -> str:
    mine = attempts(row)
    if len(mine) >= cfg.oncall.max_attempts:
        return f"{len(mine)} of {cfg.oncall.max_attempts} attempts used"
    if (money := spent(cfg, mine)) >= cfg.oncall.incident_budget_usd:
        return f"{money:.2f} of {cfg.oncall.incident_budget_usd:.2f} USD spent on it"
    return ""


def day_spent(cfg: config.Config) -> str:
    day = [r for r in state.incident_repairs() if r["started"] >= util.iso(util.now() - timedelta(days=1))]
    if (money := spent(cfg, day)) >= cfg.oncall.daily_budget_usd:
        return f"{money:.2f} of {cfg.oncall.daily_budget_usd:.2f} USD spent on repairs today"
    return ""


def interrupted_run(repair: dict) -> dict | None:
    if not repair["task"]:
        return None
    run = next(iter(state.ticket_runs(repair["repo"], repair["task"], limit=1, whole=True)), None)
    return run if run and run["status"] == "interrupted" else None


def limit_kind(cfg: config.Config, row: dict) -> str:
    return "attempts" if len(attempts(row)) >= cfg.oncall.max_attempts else "budget"


def resume(cfg: config.Config, row: dict, repair: dict, why: str) -> bool:
    if limit := exhausted(cfg, row):
        end_repair(repair, "interrupted")
        moved(row, "blocked", limit, f"{why} and is not restarted: {limit}")
        escalate(cfg, row, limit_kind(cfg, row))
        return False
    if limit := day_spent(cfg):
        moved(row, "waiting", limit)
        return False
    try:
        started = start_task(repair["repo"], ticket=repair["task"])
    except (control.Refused, control.NotFound) as e:
        end_repair(repair, "not restarted")
        lose(cfg, row, f"{why} and would not start: {outbound.sanitize(str(e), 160)}")
        return False
    if started.get("run"):
        moved(row, "repairing", note=f"{why}; restarted task {repair['task']} as run {started['run']}")
        return True
    moved(row, "waiting", f"{why}; its restart is queued")
    return False


def parked(repo: str) -> set[str]:
    rows = [r for r in state.incident_repairs() if r["task"] and r["repo"] == repo]
    if not rows:
        return set()
    off = switched_off()
    cfg = None
    held = set()
    for r in rows:
        if off or r["ended"]:
            held.add(r["task"])
        elif interrupted_run(r) and (row := state.incident(r["incident"])):
            cfg = cfg or config.load()
            if exhausted(cfg, row) or day_spent(cfg):
                held.add(r["task"])
    return held


def wake(cfg: config.Config, row: dict) -> bool:
    limits = cfg.oncall
    mine = attempts(row)
    if why := exhausted(cfg, row):
        moved(row, "blocked", why)
        escalate(cfg, row, limit_kind(cfg, row))
        return False
    if until := cooling(cfg, row):
        moved(row, "waiting", f"cooling down until {until}")
        return False
    if owner := owner_run(row):
        moved(row, "waiting", f"run {owner} owns it and is still live")
        return False
    name = repair_repo(cfg, row)
    repo = next((r for r in cfg.repos if r.name == name), None)
    if repo is None:
        moved(row, "waiting", f"{name!r} is not a [[repo]] to repair in" if name else "it has no repo to repair in")
        return False
    every = state.incident_repairs()
    if why := day_spent(cfg):
        moved(row, "waiting", why)
        return False
    if len([r for r in every if not r["ended"]]) >= limits.max_repairs:
        moved(row, "waiting", f"{limits.max_repairs} repairs are already running")
        return False
    if len([r for r in every if r["started"] >= util.iso(util.now() - timedelta(hours=1))]) >= limits.wakes_per_hour:
        moved(row, "waiting", f"{limits.wakes_per_hour} wakes in the last hour is the limit")
        return False
    why, lasting = route_refusal(cfg, repo)
    if why:
        moved(row, "waiting", why)
        if lasting:
            escalate(cfg, row, "policy")
        return False
    repair = state.add_repair(row["id"], name)
    try:
        started = start_task(name, repair_prompt(row, mine, limits.max_attempts), repair_title(row["id"]))
    except (control.Refused, control.NotFound) as e:
        made = next(r for r in state.incident(row["id"])["repairs"] if r["id"] == repair)
        why = outbound.sanitize(str(e), 160)
        if task := created_task(made):
            state.set_repair(repair, task=task)
            moved(row, "repairing", note=f"woke repair attempt {len(mine) + 1} of {limits.max_attempts}: "
                  f"task {task} in {name}; its first run did not start: {why}")
            return True
        state.set_repair(repair, ended=util.iso(), outcome="not started")
        moved(row, "waiting", f"the repair task would not start: {why}")
        return False
    state.set_repair(repair, task=started["task"])
    moved(row, "repairing", note=f"woke repair attempt {len(mine) + 1} of {limits.max_attempts}: task "
          f"{started['task']} in {name}, run {started.get('run') or '-'}")
    return True


def created_task(repair: dict) -> str:
    found = next((t for t in state.tasks(repair["repo"]) if t["title"] == repair_title(repair["incident"])
                  and t["created"] >= repair["started"]), None)
    return found["id"] if found else ""


def adopt_unfinished() -> None:
    for repair in state.incident_repairs():
        if repair["ended"] or repair["task"]:
            continue
        if task := created_task(repair):
            state.set_repair(repair["id"], task=task)
        else:
            state.set_repair(repair["id"], ended=util.iso(), outcome="not started")


def held_for_owner(cfg: config.Config, repair: dict, run_id: str) -> bool:
    from . import tasks

    report = tasks.last_report(run_id)
    try:
        trust = config.load_layer(cfg.repo(repair["repo"]).layer_dir).trust_level
    except (config.UnknownRepo, ValueError, TypeError, OSError):
        trust = ""
    return bool(report.get("held") or report.get("merge_held")) or trust != "automerge"


def follow(cfg: config.Config, row: dict, repair: dict) -> bool:
    run = next(iter(state.ticket_runs(repair["repo"], repair["task"], limit=1, whole=True)), None)
    if run is None:
        return resume(cfg, row, repair, "the repair task has no run yet")
    status = run["status"]
    if status == "running":
        moved(row, "repairing")
    elif status == "needs-info":
        hold(cfg, row, f"run {run['id']} asked a question and waits for the owner", "asks", run["id"])
    elif status == "merged":
        end_repair(repair, "merged")
        moved(row, "verifying", note=f"run {run['id']} merged the repair; rechecking the original failure",
              landed=run.get("finished") or util.iso())
    elif status == "interrupted":
        return resume(cfg, row, repair, f"run {run['id']} was interrupted")
    elif status in ("pr", "done") or (status == "failed" and run.get("pr_url")):
        if status == "pr" and not held_for_owner(cfg, repair, run["id"]):
            moved(row, "waiting", f"run {run['id']}'s pull request waits to merge")
        else:
            hold(cfg, row, f"run {run['id']}'s change is held for the owner", "held", run["id"])
    else:
        end_repair(repair, status)
        lose(cfg, row, f"run {run['id']} ended {status}")
    return False


def tend(cfg: config.Config, policies: Policies) -> dict:
    counts = {"woken": 0, "resolved": 0, "restarted": 0}
    for listed in [] if switched_off() else state.woken_incidents():
        row = state.incident(listed["id"])
        if live := live_repair(row):
            counts["restarted"] += follow(cfg, row, live)
        elif row["phase"] == "verifying":
            recheck(cfg, policies, row)
        row = state.incident(row["id"])
        counts["resolved"] += row["state"] == "resolved"
        if row["state"] == "open" and row["phase"] != "verifying" and all(r["ended"] for r in row["repairs"]):
            counts["woken"] += wake(cfg, row)
    for incident_id in state.closed_alerted():
        if clear_alert(cfg, alert_key(incident_id)):
            state.update_incident(incident_id, "its alert is cleared", alerted=0)
    return {name: n for name, n in counts.items() if n}


def steered(incident_id: int) -> dict:
    row = state.incident(incident_id)
    if row is None:
        raise control.NotFound(f"no incident {incident_id}")
    return row


def live_repair(row: dict) -> dict | None:
    return next((r for r in row["repairs"] if not r["ended"]), None)


def cancel(incident_id: int) -> dict:
    with util.lock("oncall", wait=True):
        repair = live_repair(steered(incident_id))
        if repair is None:
            raise control.Refused(f"incident {incident_id} has no repair running")
        run = state.running_run(repair["repo"], repair["task"]) if repair["task"] else None
        if run:
            stop_run(run["id"])
        if repair["task"]:
            state.task_set_state(repair["task"], "closed", ("todo",))
        state.set_repair(repair["id"], ended=util.iso(), outcome="cancelled")
        until = cooling(config.load(), state.incident(incident_id))
        stopped = f"stopped run {run['id']}" if run else "no run was live"
        state.update_incident(incident_id, f"cancelled by the owner: {stopped}; cooling down until {until or '-'}",
                              phase="waiting", waiting=f"cooling down until {until}" if until else "")
        return {"incident": incident_id, "stopped": run["id"] if run else "", "cooldown_until": until}


def closable(incident_id: int) -> dict:
    row = steered(incident_id)
    if row["state"] != "open":
        raise control.Refused(f"incident {incident_id} is already {row['state']}")
    if live_repair(row):
        raise control.Refused(f"incident {incident_id} has a repair running; cancel it first")
    return row


def dismiss(incident_id: int) -> dict:
    with util.lock("oncall", wait=True):
        closable(incident_id)
        state.update_incident(incident_id, "dismissed by the owner", state="dismissed", phase="", waiting="")
        return {"incident": incident_id, "state": "dismissed"}


def retry(incident_id: int) -> dict:
    with util.lock("oncall", wait=True):
        row = closable(incident_id)
        if row["decision"] != "wake":
            raise control.Refused(f"incident {incident_id} was not decided wake, so no repair is armed for it")
        state.update_incident(incident_id, "re-armed by the owner: attempts, spend and the cooldown count from here",
                              rearmed=max((r["id"] for r in row["repairs"]), default=0), phase="", waiting="")
        return {"incident": incident_id, "rearmed": True}


def said(summary: dict) -> str:
    text = (f"{util.count(summary['sources'], 'source')}, {util.count(summary['lines'], 'line')}, "
            f"{util.count(summary['events'], 'event')}: {summary['opened']} opened, {summary['updated']} updated, "
            f"{summary['expected']} expected")
    if decisions := summary.get("decisions"):
        text += "; decided: " + ", ".join(f"{n} {name}" for name, n in decisions.items())
    if repairs := summary.get("repairs"):
        text += "; repairs: " + ", ".join(f"{n} {name}" for name, n in repairs.items())
    return text


def arete_logs() -> list[str]:
    return [str(p) for p in sorted(paths.LOG_DIR.glob("*.log"))]


def scan_once(cfg: config.Config) -> dict:
    _, previous = cursor_value(SCAN)
    started = util.iso()
    adopt_unfinished()
    watch = Watch(cfg)
    everywhere = frozenset({entry_policy(cfg)})
    watch.follow("logs", arete_logs(), Origin("arete", everywhere, folder=True), arete_logs)
    watch.follow("runs", watch.run_files((previous or {}).get("since")), Origin("arete", everywhere))
    for source, files in watch.policies.files:
        pattern = re.compile(source.pattern or sources.LOG_PATTERN)
        own = frozenset({entry_policy(cfg, source)})
        watch.follow(f"path:{source.path}", files, Origin(source.path, own, source.repo, pattern, listed=True),
                      partial(source_files, source))
    watch.heartbeat()
    watch.serve()
    watch.stalls()
    opened, updated, expected = watch.store()
    decisions = decide(cfg, watch.policies, MAX_ASKS - watch.asks)
    repairs = tend(cfg, watch.policies)
    summary = {"sources": watch.sources, "lines": watch.lines, "events": len(watch.found), "opened": opened,
               "updated": updated, "expected": expected, "decisions": decisions,
               "repairs": repairs}
    total = (previous or {}).get("expected", 0) + expected
    state.set_cursor(SCAN, json.dumps({"expected": total, "last": summary, "since": started}), True, said(summary))
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
            "detail": row["detail"] if row else "", "expected": (value or {}).get("expected", 0),
            "wakes": "off" if switched_off() else "on"}
