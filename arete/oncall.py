from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

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


@dataclass(frozen=True)
class Origin:
    scope: str
    home: frozenset[tuple[str, str]]
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


def gone(name: str, mark: list | None) -> tuple[list[str], list | None]:
    if mark is None:
        return [], mark
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
    return older, [mark[0], mark[1], mark[2], pending, now]


def fresh(name: str, mark: list | None, baseline: bool) -> tuple[list[str], list | None]:
    try:
        info = os.stat(name)
    except OSError:
        return gone(name, mark)
    size, inode = info.st_size, info.st_ino
    now = stamp(name)
    if baseline:
        return [], [size, inode, digest(name, size) or "", None, now]
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
        return [], mark
    rows, upto = got
    if upto is None:
        upto = end if end - offset >= MAX_READ else offset
    older, pending = drain(name, pending) if pending else ([], None)
    return older + rows, [upto, inode, digest(name, upto) or "", pending, noted]


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


class Policies:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.files = [(s, sorted(glob.glob(str(paths.expand(s.path))))) for s in cfg.oncall.sources]
        self.named: dict[str, tuple[int, int] | str] = {}
        self.entries: dict[tuple[int, int] | str, list[config.OncallSource]] = {}
        for entry, files in self.files:
            for name in files:
                self.entries.setdefault(self.named.setdefault(name, identity(name) or name), []).append(entry)

    def matching(self, source: str) -> list[config.OncallSource]:
        key = self.named.get(source) or identity(source)
        if key is None:
            return [s for s in self.cfg.oncall.sources if fnmatch.fnmatchcase(source, str(paths.expand(s.path)))]
        return self.entries.get(key, [])

    def text(self, source: str) -> tuple[str, str]:
        return entry_policy(self.cfg, next(iter(self.matching(source)), None))

    def of(self, source: str) -> frozenset[tuple[str, str]]:
        return frozenset({entry_policy(self.cfg, s) for s in self.matching(source)} or {entry_policy(self.cfg)})


def bounded(scope: str, rule: frozenset[tuple[str, str]]) -> str:
    return f"{scope}\n{json.dumps(sorted(rule))}"


def fingerprint_of(kind: str, scope: str, signature: str) -> str:
    return hashlib.sha1(f"{kind}\n{scope}\n{signature}".encode()).hexdigest()[:16]


def questions(policies: Policies, incident: dict) -> dict[str, dict]:
    vision, scope = policies.text(incident["source"])
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
    for name, text in zip(("vision", "scope"), policies.text(row["source"]), strict=True):
        if not text:
            return UNSURE, least, f"no [oncall] {name} is set, so the owner decides", answers, 0
    if len(policies.of(row["source"])) > 1:
        why = "more than one [[oncall.source]] vision or scope applies to this file, so the owner decides"
        return UNSURE, least, why, answers, 0
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


def decide(cfg: config.Config, policies: Policies, budget: int = MAX_ASKS) -> dict:
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


def known_twins(cfg: config.Config, policies: Policies, rows: list[tuple], known: dict[str, str],
                budget: int = MAX_ASKS) -> tuple[list, dict, int]:
    open_rows = [r for r in state.open_incidents() if not r["injection"]]
    line, injection_line = thresholds(cfg)
    fresh, candidates = [], []
    homes = {r["id"]: origin_repo(r["source"], r["repo"]) for r in open_rows}
    rules = {r["id"]: policies.of(r["source"]) for r in open_rows}
    for row in rows:
        home = origin_repo(row[2], row[3])
        rule = policies.of(row[2])
        same = [r for r in open_rows if homes[r["id"]] == home and rules[r["id"]] == rule][:TWINS_OFFERED]
        if (row[0] not in known and same and not refused(cfg, home)
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


class Watch:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.policies = Policies(cfg)
        self.found: dict[tuple, Event] = {}
        self.sources = self.lines = self.asks = 0
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
        rule = self.policies.of(name)
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
                text = run_text(row)
            elif (origin.pattern.search(line) or EXPECTED_TEXT.search(line)
                  or (origin.folder and ORCHESTRATION_TEXT.search(line))):
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
            self.add(classify(text, row), scope, text, name, repo, window)

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
        held = state.known_fingerprints() if events else {}
        rows = []
        for e in events:
            signature = outbound.redact(sources.signature(record.scrub(e.signature, hidden)))
            if e.kind == ORCHESTRATION:
                signature = signature.removeprefix("<time> ")
            evidence = outbound.redact(record.scrub("\n".join(e.window), hidden))[:MAX_EVIDENCE]
            scope, rule = e.scope, self.policies.of(e.source)
            fingerprint = fingerprint_of(e.kind, scope, signature)
            while fingerprint in held and self.policies.of(held[fingerprint]) != rule:
                scope = bounded(scope, rule)
                fingerprint = fingerprint_of(e.kind, scope, signature)
            rows.append((fingerprint, e.kind, e.source, e.repo, signature, e.count, evidence))
        matched, marks, self.asks = known_twins(self.cfg, self.policies, rows, held)
        opened = sum(state.commit_scan(rows, self.cursors, matched, marks))
        return opened, len(events) - opened, len(self.found) - len(events)


def said(summary: dict) -> str:
    text = (f"{util.count(summary['sources'], 'source')}, {util.count(summary['lines'], 'line')}, "
            f"{util.count(summary['events'], 'event')}: {summary['opened']} opened, {summary['updated']} updated, "
            f"{summary['expected']} expected")
    if decisions := summary.get("decisions"):
        text += "; decided: " + ", ".join(f"{n} {name}" for name, n in decisions.items())
    return text


def scan_once(cfg: config.Config) -> dict:
    _, previous = cursor_value(SCAN)
    started = util.iso()
    watch = Watch(cfg)
    everywhere = frozenset({entry_policy(cfg)})
    logs = [str(p) for p in sorted(paths.LOG_DIR.glob("*.log"))]
    watch.follow("logs", logs, Origin("arete", everywhere, folder=True))
    watch.follow("runs", watch.run_files((previous or {}).get("since")), Origin("arete", everywhere))
    for source, files in watch.policies.files:
        pattern = re.compile(source.pattern or sources.LOG_PATTERN)
        own = frozenset({entry_policy(cfg, source)})
        watch.follow(f"path:{source.path}", files, Origin(source.path, own, source.repo, pattern))
    watch.heartbeat()
    watch.serve()
    watch.stalls()
    opened, updated, expected = watch.store()
    decisions = decide(cfg, watch.policies, MAX_ASKS - watch.asks)
    summary = {"sources": watch.sources, "lines": watch.lines, "events": len(watch.found), "opened": opened,
               "updated": updated, "expected": expected, "decisions": decisions}
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
            "detail": row["detail"] if row else "", "expected": (value or {}).get("expected", 0)}
