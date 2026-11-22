from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path

from . import (
    config,
    control,
    diffguard,
    jev,
    log_follower,
    outbound,
    paths,
    prompt_loader,
    record,
    run_lines,
    screening,
    scrub,
    sources,
    util,
)
from .sighting import Sighting
from .store import db as foundation
from .store import incidents as incident_store
from .store import runs as run_store
from .store import sources as source_store
from .store import tasks as tasks_store

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
CONTEXT_LINES = 5
MAX_EVIDENCE = 4000
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


def commit_scan(incident_rows: list[Sighting], cursor_rows: list[tuple[str, str, bool, str]],
                aliases=(), marks=None) -> list[bool]:
    with foundation.db() as c:
        for fingerprint, incident_id, note in aliases:
            incident_store.write_alias(c, fingerprint, incident_id, note)
        opened = []
        for row in incident_rows:
            incident_id, new = incident_store.write_incident(c, row)
            opened.append(new)
            if new and row.fingerprint in (marks or {}):
                fields, note = marks[row.fingerprint]
                incident_store.write_mark(c, incident_id, fields, note)
        for source, value, ok, detail in cursor_rows:
            source_store.write_scan_cursor(c, source, value, ok, detail)
        return opened


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
    return row if isinstance(row, dict) and all(k in row for k in run_lines.FIXED if k != "ts") else None


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


def other_live_run(run_id, found: dict) -> dict | None:
    if not (found.get("repo") and found.get("issue")):
        return None
    other = run_store.running_run(found["repo"], found["issue"])
    return other if other and other["id"] != run_id else None


def ticket_has_live_run(row: dict) -> bool:
    return bool(other_live_run(row["run"], run_store.run_get(str(row["run"])) or {}))


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
        rows = run_lines.whole(data)
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


def cursor_value(name: str, connect=foundation.db):
    row = source_store.cursor(name, connect)
    return row, (json.loads(row["value"]) if row and row["value"] else None)


def ask_jev(items, cfg) -> list[jev.Reply]:
    return jev.ask_many(items, cfg, trace=("", "oncall jev"))


def start_task(repo: str, prompt: str = "", title: str = "", ticket: str = "") -> dict:
    from . import tasks

    return tasks.start(repo, prompt, title, "fix", ticket)


def stop_run(run_id: str) -> dict:
    return control.stop_run(run_id)


def switched_off(connect=foundation.db) -> bool:
    return cursor_value(SWITCH, connect)[1] == "off"


def switch(on: bool) -> dict:
    word = "on" if on else "off"
    with util.lock("oncall", wait=True):
        source_store.set_cursor(SWITCH, json.dumps(word), True, f"wakes {word}")
        for row in incident_store.woken_incidents():
            incident_store.add_incident_note(row["id"], f"wakes and rechecks switched {word} by the owner")
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
    return incident_store.alert_get(key) is None


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
    run_id = run_lines.run_of_path(Path(row["source"])) or next(
        (str(r["run"]) for r in map(run_line, str(row.get("evidence") or "").splitlines()) if r), "")
    if not run_id:
        return ""
    found = run_store.run_get(run_id) or {}
    if found.get("status") == "running" and not process_gone(found):
        return run_id
    other = other_live_run(run_id, found)
    return other["id"] if other else ""


def injected(a: jev.Answer) -> str:
    return f"the evidence reads as instructions to an agent (p {a.yes:.2f})"


@dataclass(frozen=True)
class Verdict:
    decision: str
    confidence: float
    reason: str
    answers: dict
    flagged: int


def verdict(reply: jev.Reply, asking: dict, cfg: config.Config, policies: Policies, row: dict) -> Verdict:
    line, injection_line = thresholds(cfg)
    if reply.error or set(asking) - set(reply.answers):
        return Verdict("", 0.0, reply.error or "its reply left questions out", {}, 0)
    got = {name: reply.answers[name] for name in asking}
    answers = {name: {"value": a.value, "confidence": a.confidence, "p": a.yes} for name, a in got.items()}
    if screening.held(screening.of_answer(got["injection"], injection_line, reply), screening.Posture.REQUIRED):
        return Verdict(IGNORE, got["injection"].confidence, injected(got["injection"]), answers, 1)
    for name, why in NOT_ACTIONABLE.items():
        if name in got and got[name].sure(line) and got[name].value is False:
            return Verdict(IGNORE, got[name].confidence, why, answers, 0)
    for name, a in got.items():
        if not a.sure(line):
            return Verdict(UNSURE, a.confidence, f"{name} is uncertain ({a.note()})", answers, 0)
    least = min(a.confidence for a in got.values())
    rule = policies.read_under(row)
    if rule is None:
        why = ("it was opened before incidents recorded the vision and scope their lines were read under, and the "
               "config holds more than one; the owner decides")
        return Verdict(UNSURE, least, why, answers, 0)
    if not rule:
        why = ("its file is gone or no [[oncall.source]] glob lists it, so which vision and scope apply cannot be "
               "told; the owner decides")
        return Verdict(UNSURE, least, why, answers, 0)
    if not rule <= policies.every:
        why = "the vision or scope its file was under when read is no longer in the config, so the owner decides"
        return Verdict(UNSURE, least, why, answers, 0)
    if len(rule) > 1:
        why = "more than one [[oncall.source]] vision or scope applies to this file, so the owner decides"
        return Verdict(UNSURE, least, why, answers, 0)
    for name, text in zip(("vision", "scope"), next(iter(rule)), strict=True):
        if not text:
            return Verdict(UNSURE, least, f"no [oncall] {name} is set, so the owner decides", answers, 0)
    return Verdict(WAKE, least, "actionable, fits the vision and is in scope", answers, 0)


def due(row: dict, now: str) -> bool:
    return (not row["injection"] and row["class"] != EXPECTED and (not row["retry_at"] or row["retry_at"] <= now)
            and (row["decision"] == "" or (row["decision"] == IGNORE and changed(row))))


def origin_repo(source: str, repo: str) -> str:
    if repo:
        return repo
    run_id = run_lines.run_of_path(Path(source)) or (source.removeprefix("run ") if source.startswith("run ") else "")
    return ((run_store.run_get(run_id) or {}).get("repo") or "") if run_id else ""


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
    incident_store.update_incident(row["id"], note, **fields)


def count_undecided(counts: dict[str, int], row: dict, reason: str) -> None:
    failed(row, reason)
    counts["undecided"] = counts.get("undecided", 0) + (row["decision"] == "")


def settle(row: dict, outcome: Verdict) -> None:
    decision, confidence, reason, answers, flagged = (outcome.decision, outcome.confidence, outcome.reason,
                                                      outcome.answers, outcome.flagged)
    seen = sorted(shapes(row["evidence"]))
    kept = list(dict.fromkeys([*seen, *json.loads(row["shapes"] or "[]")]))[:SHAPES_KEPT]
    detail = ", ".join(f"{name} {'yes' if a['value'] else 'no'} {a['confidence']:.2f}" for name, a in answers.items())
    incident_store.update_incident(row["id"], f"decided {decision} (confidence {confidence:.2f}): {reason}; {detail}",
                          decision=decision, confidence=confidence, reason=reason, answers=json.dumps(answers),
                          injection=flagged, decided=util.iso(), tries=0, retry_at="", decided_count=row["count"],
                          shapes=json.dumps(kept))


def decide(cfg: config.Config, policies: Policies | None = None, budget: int = MAX_ASKS) -> dict:
    policies = policies or Policies(cfg)
    counts: dict[str, int] = {}
    now = util.iso()
    ask = []
    for row in incident_store.incidents_to_decide():
        if not due(row, now):
            continue
        if owner := owner_run(row):
            if row["decision"] == "" and (why := f"run {owner} owns it and is still live") != row["reason"]:
                incident_store.update_incident(row["id"], reason=why)
        elif why := refused(cfg, origin_repo(row["source"], row["repo"])):
            count_undecided(counts, row, why)
        else:
            ask.append(row)
    ask = ask[:max(budget, 0)]
    asking = [questions(policies, row) for row in ask]
    replies = asked([(subject(row), q) for row, q in zip(ask, asking, strict=True)], cfg) if ask else []
    for row, q, reply in zip(ask, asking, replies, strict=True):
        outcome = verdict(reply, q, cfg, policies, row)
        if outcome.decision:
            settle(row, outcome)
            counts[outcome.decision] = counts.get(outcome.decision, 0) + 1
        else:
            count_undecided(counts, row, outcome.reason)
    return {name: n for name, n in counts.items() if n}


def decision_line(row: dict) -> str:
    if not row["decision"]:
        text = f"undecided: {row['reason']}" if row["reason"] else "undecided"
        return text + (f", next try {row['retry_at']}" if row["retry_at"] else "")
    mark = " (injection)" if row["injection"] else ""
    return f"{row['decision']} {row['confidence']:.2f}{mark}: {row['reason']}"


def known_twins(cfg: config.Config, policies: Policies, sightings: list[Sighting], known: dict[str, dict],
                budget: int = MAX_ASKS) -> tuple[list, dict, int]:
    open_rows = [r for r in incident_store.open_incidents() if not r["injection"]]
    line, injection_line = thresholds(cfg)
    fresh, candidates = [], []
    homes = {r["id"]: origin_repo(r["source"], r["repo"]) for r in open_rows}
    rules = {r["id"]: policies.standing(r) for r in open_rows}
    for sighting in sightings:
        home = origin_repo(sighting.source, sighting.repo)
        rule = unpacked(sighting.policies)
        same = [r for r in open_rows if homes[r["id"]] == home and rules[r["id"]] == rule][:TWINS_OFFERED]
        owned = {"class": sighting.kind, "source": sighting.source, "evidence": sighting.evidence}
        if sighting.fingerprint not in known and rule and same and not refused(cfg, home) and not owner_run(owned):
            fresh.append(sighting)
            candidates.append(same)
    fresh, candidates = fresh[:budget], candidates[:budget]
    items = []
    for sighting, same in zip(fresh, candidates, strict=True):
        options = {"new": "a different problem from every open incident listed in the text",
                   **{f"i{r['id']}": f"the same problem as open incident i{r['id']} listed in the text" for r in same}}
        text = fenced({"new symptom": {"class": sighting.kind, "signature": sighting.signature,
                                       "evidence": sighting.evidence},
                       "open incidents": {f"i{r['id']}": {"class": r["class"], "signature": r["signature"]}
                                          for r in same}})
        items.append((text, {"twin": jev.pick(TWIN, options), "injection": jev.yes_no(jev.INJECTION)}))
    matched, marks = [], {}
    for sighting, (_, q), reply in zip(fresh, items, asked(items, cfg) if items else [], strict=True):
        a, flagged = reply.answers.get("twin"), reply.answers.get("injection")
        screened = screening.of_answer(flagged, injection_line, reply)
        if screened.status == screening.FLAGGED:
            answers = {"injection": {"value": flagged.value, "confidence": flagged.confidence, "p": flagged.yes}}
            fields = {"decision": IGNORE, "confidence": flagged.confidence, "reason": injected(flagged),
                      "answers": json.dumps(answers), "injection": 1, "decided": util.iso(),
                      "decided_count": sighting.count}
            said = (f"decided {IGNORE} (confidence {flagged.confidence:.2f}): {injected(flagged)}; "
                    f"injection yes {flagged.confidence:.2f}")
            marks[sighting.fingerprint] = (fields, said)
        elif (not screening.held(screened, screening.Posture.REQUIRED) and a and flagged.value is False
              and flagged.sure(line) and a.value != "new" and a.value in q["twin"]["criteria"] and a.sure(line)):
            note = (f"JEV matches a differently worded symptom to this incident ({a.note()}): "
                    f"{outbound.plain(sighting.signature, 200)}")
            matched.append((sighting.fingerprint, int(str(a.value)[1:]), note))
    return matched, marks, len(items)


def serve_failures(beat: dict) -> dict[str, str]:
    now = {f"broken:{k}": f"{k}: loop broken: {v}" for k, v in (beat.get("broken") or {}).items()}
    now |= {f"poll:{k}": f"{k}: poll failed: {v}" for k, v in (beat.get("poll_errors") or {}).items()}
    return now | {f"tick:{k}": f"{k}: tick failed: {f['error']}" for k, f in (beat.get("failing") or {}).items()
                  if f.get("ticks", 0) >= control.STUCK_TICKS}


class Watch:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.policies = Policies(cfg)
        self.found: dict[tuple, Event] = {}
        self.sources = self.lines = self.asks = 0
        self.cursors: list[tuple[str, str, bool, str]] = []
        self.owned = {(r["repo"], r["task"]): r["incident"] for r in incident_store.incident_repairs() if r["task"]}
        self.runs: dict[str, dict] = {}
        self.quiet: dict[int, int] = {}

    def repairing(self, run_id: str) -> int:
        if not self.owned:
            return 0
        if run_id not in self.runs:
            self.runs[run_id] = run_store.run_get(run_id) or {}
        found = self.runs[run_id]
        return self.owned.get((found.get("repo"), found.get("issue")), 0)

    def mute_if_repairing(self, incident: int) -> bool:
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
                if not run_failure(row) or (origin.folder and run_lines.path(str(row["run"])).exists()):
                    continue
                if self.mute_if_repairing(self.repairing(str(row["run"]))):
                    continue
                text = run_text(row)
            elif (origin.pattern.search(line) or EXPECTED_TEXT.search(line)
                  or (origin.folder and ORCHESTRATION_TEXT.search(line))):
                if (own := RUN_PREFIX.search(line)) and self.mute_if_repairing(self.owned.get(own.groups(), 0)):
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
                repo = (run_store.run_get(str(row["run"])) or {}).get("repo") or ""
                if repo:
                    scope = f"{policy_scope}:{repo}"
            self.add(classify(text, row), scope, text, name, repo, window, rule=rule, legacy=origin.scope)

    def follow(self, group: str, watched: list[str], origin: Origin, relist=None) -> None:
        stored, marks = cursor_value(PREFIX + group)
        marks = marks or {}
        for name in dict.fromkeys([*watched, *marks]):
            older, rows, marks[name] = log_follower.fresh(name, marks.get(name), stored is None)
            if older:
                self.consider(name, older, origin, self.policies.of_copy(name, origin.listed))
            if rows:
                self.consider(name, rows, origin, self.policies.of_file(name, origin.listed))
        kept = {name: mark for name, mark in marks.items()
                if mark is not None and (name in watched or log_follower.undrained(name, mark))}
        if relist and (dropped := {name for name, mark in marks.items() if mark is not None} - kept.keys()):
            kept |= {name: marks[name] for name in dropped.intersection(relist())}
        self.defer(group, json.dumps(kept), util.count(len(watched), "file"))
        self.sources += len(watched)

    def defer(self, name: str, value: str, detail: str) -> None:
        self.cursors.append((PREFIX + name, value, True, detail))

    def run_files(self, since: str | None) -> list[str]:
        ids = [r["id"] for r in run_store.runs_marked_running()]
        ids += [r["id"] for r in run_store.runs_since(since)] if since else []
        return [str(run_lines.path(run_id)) for run_id in dict.fromkeys(ids)]

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
        for row in run_store.runs_marked_running():
            if (util.now() - util.parse_iso(row["started"])).total_seconds() <= record.SETTLE_GRACE_SECONDS:
                continue
            rows, texts = tail_of(run_lines.path(row["id"]))
            why = "its process is gone" if process_gone(row) else "its heartbeats stopped" if silent(rows) else ""
            if not why:
                continue
            stalled.append(row["id"])
            if row["id"] not in flagged and not self.mute_if_repairing(self.owned.get((row.get("repo"), row.get("issue")), 0)):
                head = f"run {row['id']} ({row.get('repo') or '-'}#{row.get('issue') or '-'}) stalled: {why}"
                self.add(STALLED, f"run:{row['id']}", f"run stalled: {why}", f"run {row['id']}", "", [head, *texts])
        self.defer("stalls", json.dumps(stalled), f"{len(stalled)} stalled")

    def store(self) -> tuple[int, int, int]:
        events = [e for e in self.found.values() if e.kind != EXPECTED]
        hidden = scrub.secrets() if events else ()
        held = incident_store.known_fingerprints() if events else {}
        rows = []
        for e in events:
            signature = outbound.redact(sources.signature(scrub.scrub(e.signature, hidden)))
            if e.kind == ORCHESTRATION:
                signature = signature.removeprefix("<time> ")
            evidence = outbound.redact(scrub.scrub("\n".join(e.window), hidden))[:MAX_EVIDENCE]
            scope = e.scope
            fingerprint = fingerprint_of(e.kind, scope, signature)
            older = fingerprint_of(e.kind, e.legacy, signature)
            if fingerprint not in held and older in held and self.inherits(held[older], e):
                fingerprint = older
            while fingerprint in held and self.policies.standing(held[fingerprint]) != e.rule:
                scope = bounded(scope, e.rule)
                fingerprint = fingerprint_of(e.kind, scope, signature)
            rows.append(Sighting(fingerprint, e.kind, e.source, e.repo, signature, e.count, evidence, packed(e.rule)))
        matched, marks, self.asks = known_twins(self.cfg, self.policies, rows, held)
        opened = sum(commit_scan(rows, self.cursors, matched, marks))
        for incident, n in self.quiet.items():
            incident_store.add_incident_note(incident, f"its repair wrote {util.count(n, 'failure line')}; none opened an incident")
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
    incident_store.update_incident(row["id"], note or (f"{phase}: {waiting}" if waiting else phase),
                          phase=phase, waiting=waiting, **fields)


def alert_key(incident_id: int) -> str:
    return f"{PREFIX}incident:{incident_id}"


def escalate(cfg: config.Config, row: dict, kind: str, run: str = "") -> None:
    latest = incident_store.incident(row["id"])
    if latest["alerted"]:
        return
    line = outbound.sanitize(ESCALATIONS[kind].format(
        id=row["id"], repo=repair_repo(cfg, row) or "-", run=run or "-", n=len(attempts(latest)),
        budget=f"{cfg.oncall.incident_budget_usd:.2f}"))
    raise_alert(cfg, alert_key(row["id"]), f"on-call incident {row['id']} needs you", line)
    incident_store.update_incident(row["id"], f"escalated once: {line}", alerted=1)


def hold(cfg: config.Config, row: dict, why: str, kind: str, run: str) -> None:
    moved(row, "waiting", why)
    escalate(cfg, row, kind, run)


def end_repair(repair: dict, outcome: str) -> None:
    incident_store.set_repair(repair["id"], ended=util.iso(), outcome=outcome)


def lose(cfg: config.Config, row: dict, why: str) -> None:
    used = len(attempts(incident_store.incident(row["id"])))
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
        found = run_store.run_get(row["source"].removeprefix("run ")) or {}
        gone = found.get("status") != "running" or not (process_gone(found) or silent(tail_of(run_lines.path(found["id"]))[0]))
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


def recover(row: dict, what: str) -> None:
    text = f"{what} passed at {util.iso()}; no recurrence since the repair landed at {row['landed']}"
    incident_store.update_incident(row["id"], f"resolved: {text}", state="resolved", phase="", waiting="", recovery=text)


def recheck(cfg: config.Config, policies: Policies, row: dict) -> None:
    if row["last_seen"] > row["landed"]:
        return lose(cfg, row, "recheck failed: the signature recurred after the repair landed")
    found = reread(cfg, policies, row)
    if found is None:
        return
    ok, detail = found
    if ok:
        return recover(row, detail)
    lose(cfg, row, f"recheck failed: {detail}")


def cooling(cfg: config.Config, row: dict) -> str:
    ended = max((r["ended"] for r in row["repairs"] if r["ended"] and r["id"] > since(row)), default="")
    if not ended:
        return ""
    until = util.iso(util.parse_iso(ended) + timedelta(minutes=cfg.oncall.cooldown_minutes))
    return until if until > util.iso() else ""


def spent(cfg: config.Config, repairs: list[dict]) -> float:
    ids = [r["id"] for x in repairs if x["task"] for r in run_store.ticket_runs(x["repo"], x["task"], limit=50)]
    return sum(run_store.paid_by_run(ids, local_providers=cfg.local_providers).values())


def last_run_id(repair: dict) -> str:
    return next((r["id"] for r in run_store.ticket_runs(repair["repo"], repair["task"], limit=1)), "-")


def repair_line(row: dict) -> str:
    made = len([r for r in row.get("repairs", []) if r["task"]])
    parts = [row["phase"] or "", row["waiting"], f"{made} attempt{'' if made == 1 else 's'}" if made else ""]
    if last := next((r for r in reversed(row.get("repairs", [])) if r["task"]), None):
        parts.append(f"task {last['task']}")
    if row.get("recovery"):
        parts.append(f"recovered: {row['recovery']}")
    return "; ".join(p for p in parts if p)


def route_refusal(cfg: config.Config, repo: config.Repo) -> tuple[str, bool]:
    from . import devices, routing, steps
    from .agent import REPLAY

    try:
        layer = config.load_layer(repo.layer_dir)
        modes = routing.modes_for(cfg, repo)
        for role in ("plan", "build", *(("verify",) if layer.verify else ()), "review", *REPAIR_STEPS):
            setting = steps.resolve(cfg, layer, role, "fix")
            which, name = routing.chosen(setting.model, setting.runner, cfg)
            if which == REPLAY:
                continue
            request = routing.request_for(cfg, repo, which, name, None, True,
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


def repair_prompt(row: dict, repairs: list[dict], limit: int, layer: config.RepoLayer) -> str:
    earlier = "".join(f"- attempt {n}: task {r['task']}, run {last_run_id(r)}, ended {r['outcome'] or 'unfinished'}\n"
                      for n, r in enumerate(repairs, 1))
    return prompt_loader.prompt("oncall_repair", paths.PROMPTS_DIR,
        incident=row["id"], kind=row["class"], source=outbound.plain(row["source"], 200), count=row["count"],
        first_seen=row["first_seen"], last_seen=row["last_seen"], attempt=len(repairs) + 1,
        max_attempts=limit, earlier=f"Earlier attempts:\n{earlier}" if earlier else "", evidence=subject(row),
        denied=prompt_loader.code_list(diffguard.denied_paths(layer)))


def exhausted(cfg: config.Config, row: dict) -> str:
    mine = attempts(row)
    if len(mine) >= cfg.oncall.max_attempts:
        return f"{len(mine)} of {cfg.oncall.max_attempts} attempts used"
    if (money := spent(cfg, mine)) >= cfg.oncall.incident_budget_usd:
        return f"{money:.2f} of {cfg.oncall.incident_budget_usd:.2f} USD spent on it"
    return ""


def day_spent(cfg: config.Config, repairs: list[dict] | None = None) -> str:
    every = incident_store.incident_repairs() if repairs is None else repairs
    day = [r for r in every if r["started"] >= util.iso(util.now() - timedelta(days=1))]
    if (money := spent(cfg, day)) >= cfg.oncall.daily_budget_usd:
        return f"{money:.2f} of {cfg.oncall.daily_budget_usd:.2f} USD spent on repairs today"
    return ""


def interrupted_run(repair: dict) -> dict | None:
    if not repair["task"]:
        return None
    run = run_store.newest_whole_run(repair["repo"], repair["task"])
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
    rows = [r for r in incident_store.incident_repairs() if r["task"] and r["repo"] == repo]
    if not rows:
        return set()
    off = switched_off()
    cfg = None
    held = set()
    for r in rows:
        if off or r["ended"]:
            held.add(r["task"])
        elif interrupted_run(r) and (row := incident_store.incident(r["incident"])):
            cfg = cfg or config.load()
            if exhausted(cfg, row) or day_spent(cfg):
                held.add(r["task"])
    return held


@dataclass(frozen=True)
class Hold:
    phase: str
    why: str
    escalate: str = ""


def hold_for(cfg: config.Config, row: dict) -> Hold | None:
    limits = cfg.oncall
    if why := exhausted(cfg, row):
        return Hold("blocked", why, limit_kind(cfg, row))
    if until := cooling(cfg, row):
        return Hold("waiting", f"cooling down until {until}")
    if owner := owner_run(row):
        return Hold("waiting", f"run {owner} owns it and is still live")
    name = repair_repo(cfg, row)
    repo = next((r for r in cfg.repos if r.name == name), None)
    if repo is None:
        return Hold("waiting", f"{name!r} is not a [[repo]] to repair in" if name else "it has no repo to repair in")
    every = incident_store.incident_repairs()
    if why := day_spent(cfg, every):
        return Hold("waiting", why)
    if len([r for r in every if not r["ended"]]) >= limits.max_repairs:
        return Hold("waiting", f"{limits.max_repairs} repairs are already running")
    if len([r for r in every if r["started"] >= util.iso(util.now() - timedelta(hours=1))]) >= limits.wakes_per_hour:
        return Hold("waiting", f"{limits.wakes_per_hour} wakes in the last hour is the limit")
    why, lasting = route_refusal(cfg, repo)
    if why:
        return Hold("waiting", why, "policy" if lasting else "")
    return None


def wake(cfg: config.Config, row: dict) -> bool:
    if held := hold_for(cfg, row):
        moved(row, held.phase, held.why)
        if held.escalate:
            escalate(cfg, row, held.escalate)
        return False
    limits = cfg.oncall
    mine = attempts(row)
    name = repair_repo(cfg, row)
    try:
        layer = config.load_layer(cfg.repo(name).layer_dir)
        text = repair_prompt(row, mine, limits.max_attempts, layer)
    except (OSError, ValueError) as error:
        moved(row, "waiting", f"repair prompt refused: {outbound.sanitize(str(error), 160)}")
        return False
    repair = incident_store.add_repair(row["id"], name)
    try:
        started = start_task(name, text, repair_title(row["id"]))
    except (control.Refused, control.NotFound) as e:
        made = next(r for r in incident_store.incident(row["id"])["repairs"] if r["id"] == repair)
        why = outbound.sanitize(str(e), 160)
        if task := adopt(made):
            moved(row, "repairing", note=f"woke repair attempt {len(mine) + 1} of {limits.max_attempts}: "
                  f"task {task} in {name}; its first run did not start: {why}")
            return True
        moved(row, "waiting", f"the repair task would not start: {why}")
        return False
    incident_store.set_repair(repair, task=started["task"])
    moved(row, "repairing", note=f"woke repair attempt {len(mine) + 1} of {limits.max_attempts}: task "
          f"{started['task']} in {name}, run {started.get('run') or '-'}")
    return True


def created_task(repair: dict) -> str:
    found = next((t for t in tasks_store.tasks(repair["repo"]) if t["title"] == repair_title(repair["incident"])
                  and t["created"] >= repair["started"]), None)
    return found["id"] if found else ""


def adopt(repair: dict) -> str:
    if task := created_task(repair):
        incident_store.set_repair(repair["id"], task=task)
        return task
    incident_store.set_repair(repair["id"], ended=util.iso(), outcome="not started")
    return ""


def adopt_unfinished() -> None:
    for repair in incident_store.incident_repairs():
        if not (repair["ended"] or repair["task"]):
            adopt(repair)


def held_for_owner(cfg: config.Config, repair: dict, run_id: str) -> bool:
    from . import tasks

    report = tasks.last_report(run_id)
    try:
        trust = config.load_layer(cfg.repo(repair["repo"]).layer_dir).trust_level
    except (config.UnknownRepo, ValueError, TypeError, OSError):
        trust = ""
    return bool(report.get("held") or report.get("merge_held")) or trust != "automerge"


def track_repair(cfg: config.Config, row: dict, repair: dict) -> bool:
    run = run_store.newest_whole_run(repair["repo"], repair["task"])
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
    for listed in [] if switched_off() else incident_store.woken_incidents():
        row = incident_store.incident(listed["id"])
        if live := live_repair(row):
            counts["restarted"] += track_repair(cfg, row, live)
        elif row["phase"] == "verifying":
            recheck(cfg, policies, row)
        row = incident_store.incident(row["id"])
        counts["resolved"] += row["state"] == "resolved"
        if row["state"] == "open" and row["phase"] != "verifying" and all(r["ended"] for r in row["repairs"]):
            counts["woken"] += wake(cfg, row)
    for incident_id in incident_store.closed_alerted():
        if clear_alert(cfg, alert_key(incident_id)):
            incident_store.update_incident(incident_id, "its alert is cleared", alerted=0)
    return {name: n for name, n in counts.items() if n}


def steered(incident_id: int) -> dict:
    row = incident_store.incident(incident_id)
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
        run = run_store.running_run(repair["repo"], repair["task"]) if repair["task"] else None
        if run:
            stop_run(run["id"])
        if repair["task"]:
            tasks_store.task_set_state(repair["task"], "closed", ("todo",))
        incident_store.set_repair(repair["id"], ended=util.iso(), outcome="cancelled")
        until = cooling(config.load(), incident_store.incident(incident_id))
        stopped = f"stopped run {run['id']}" if run else "no run was live"
        incident_store.update_incident(incident_id, f"cancelled by the owner: {stopped}; cooling down until {until or '-'}",
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
        incident_store.update_incident(incident_id, "dismissed by the owner", state="dismissed", phase="", waiting="")
        return {"incident": incident_id, "state": "dismissed"}


def retry(incident_id: int) -> dict:
    with util.lock("oncall", wait=True):
        row = closable(incident_id)
        if row["decision"] != "wake":
            raise control.Refused(f"incident {incident_id} was not decided wake, so no repair is armed for it")
        incident_store.update_incident(incident_id, "re-armed by the owner: attempts, spend and the cooldown count from here",
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
    source_store.set_cursor(SCAN, json.dumps({"expected": total, "last": summary, "since": started}), True, said(summary))
    return summary


def scan(cfg: config.Config) -> dict | None:
    if not cfg.oncall.enabled:
        return None
    with util.lock("oncall") as got:
        if not got:
            return {"skipped": "another scan is running"}
        try:
            return scan_once(cfg)
        except BaseException as e:
            with suppress(BaseException):
                crashed(f"{type(e).__name__}: {util.first_line(str(e), 300)}")
            raise


def crashed(text: str) -> None:
    source_store.set_cursor(SCAN, None, False, text)


def age(stamp: str) -> int:
    return round((util.now() - util.parse_iso(stamp)).total_seconds())


def health() -> tuple[bool, str]:
    row = source_store.cursor(SCAN)
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


def status(cfg: config.Config, connect=foundation.db) -> dict:
    row, value = cursor_value(SCAN, connect)
    switch_row = source_store.cursor(SWITCH, connect)
    return {"enabled": cfg.oncall.enabled, "last_scan": row["polled"] if row else "", "ok": bool(row and row["ok"]),
            "detail": row["detail"] if row else "", "expected": (value or {}).get("expected", 0),
            "wakes": "off" if switched_off(connect) else "on",
            "switched": switch_row["polled"] if switch_row else ""}
