from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import paths, record, run_lines, runlimits, util
from .store import asks, orchestration, runs, sources, unblock
from .store.db import reader

CATEGORIES = ("writing", "checks", "review", "owner_wait", "queue_wait", "retry")
GATES = {"checks.run": "checks", "verification.run": "verify", "review.run": "review"}
STEPS = {"implement": "writing", "build": "writing", "patch": "writing", "revise": "writing",
         "document": "writing", "checks": "checks", "verify": "checks", "review": "review"}
REPAIRS = runlimits.REPAIRS.union(runlimits.REPAIR_NAMES)
WAITS = {"person": ("owner_wait", "Owner answers the outstanding question"),
         "slot": ("queue_wait", "The required slot becomes available"),
         "limit": ("queue_wait", "The provider limit resets"),
         "ci": ("queue_wait", "CI reports a result"),
         "prerequisite": ("queue_wait", "Verification can retry after its prerequisites are ready"),
         "unblock": ("retry", "Recovery resolves the blocker or routes it to the owner")}
PARENT_FIELDS = {"route": "continues", "repair": "source", "recovery": "source", "answer": "asked_in"}


@dataclass(frozen=True)
class Span:
    run_id: str
    category: str
    activity: str
    start: datetime
    end: datetime | None
    source_id: str | None = None
    wait: bool = False

    def active(self, at: datetime) -> bool:
        return self.start <= at and (self.end is None or at < self.end)

    def identity(self) -> dict:
        return {"run_id": self.run_id, "category": self.category, "since": iso(self.start),
                "source_id": self.source_id}


@dataclass(frozen=True)
class Window:
    run_id: str
    start: datetime
    end: datetime | None
    running: bool

    def cap(self, end: datetime | None) -> datetime | None:
        return min(self.end, end) if self.end and end else self.end or end


def timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = util.parse_iso(value)
        return at.astimezone(UTC) if at.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def eligible(row: dict) -> bool:
    return (row["kind"] == "pipeline" and not row.get("only_step") and not row.get("dry_run")
            and row["status"] != "dry-run")


def read_lines(run_id: str) -> list[dict]:
    try:
        return run_lines.read(run_id)
    except OSError:
        return []


def lineage(run_id: str, rows: dict, logs: dict) -> tuple[list[str], list[str]]:
    neighbors = defaultdict(set)
    roots, representatives = {}, {}

    def link(child, parent):
        if isinstance(parent, str) and parent in rows and parent != child:
            neighbors[child].add(parent)
            neighbors[parent].add(child)

    for rid, row in rows.items():
        link(rid, row.get("environment_retry_of"))
        for line in logs[rid]:
            event = line.get("event")
            if isinstance(event, str) and (parent_field := PARENT_FIELDS.get(event)):
                link(rid, line.get(parent_field))
        grant = unblock.for_run(rid).get("grant", {})
        link(rid, (grant.get("binding") or {}).get("run"))
        if rid not in roots and (saved := orchestration.find(rid)):
            root = saved["root"]
            link(rid, representatives.setdefault(root, rid))
            members = [root, saved["current_run"], *saved["data"].get("runs", [])]
            for member in members:
                if isinstance(member, str) and member in rows:
                    roots[member] = root
                    link(rid, member)
            roots[rid] = root

    seen, todo = set(), [run_id]
    while todo:
        rid = todo.pop()
        if rid not in seen:
            seen.add(rid)
            todo.extend(neighbors[rid] - seen)
    linked = [rid for rid in rows if rid in seen]
    return linked, list(dict.fromkeys(roots[rid] for rid in linked if rid in roots))


def read_timeline(run_id: str) -> list[dict]:
    marks = []
    try:
        marks.extend(record.lines(paths.record_dir(run_id) / record.TIMELINE))
    except OSError:
        pass
    return marks


def settled_at(c, row: dict, start: datetime, timeline: list[dict]) -> datetime | None:
    if row["status"] == "running":
        return None
    rid = row["id"]
    end = c.execute("SELECT json_extract(data, '$.finished') FROM events_log "
                    "WHERE subject = ? AND kind = 'run.ended' ORDER BY ver LIMIT 1", (f"run:{rid}",)).fetchone()
    if end and (at := timestamp(end[0])) and at >= start:
        return at
    for mark in timeline:
        if mark.get("kind") == "result" and (at := timestamp(mark.get("at"))) and at >= start:
            return at
    at = timestamp(row.get("finished"))
    if at is None or at < start or row["status"] in ("merged", "rejected"):
        return None
    if row["status"] == "pr" and c.execute(
            "SELECT 1 FROM events_log WHERE subject = ? AND kind = 'run.changed' "
            "AND json_extract(data, '$.status') = 'rejected' AND json_extract(data, '$.finished') = ? LIMIT 1",
            (f"run:{rid}", row["finished"])).fetchone():
        return None
    return at


def action_spans(actions: list[dict], windows: dict[str, Window]) -> list[Span]:
    spans = []
    for action in actions:
        op, timing = action["op"], action["timing"]
        label = GATES.get(op, op)
        category = STEPS.get(label)
        if op in ("worker.start", "worker.continue"):
            if action["writing"] == 1:
                category = "retry" if action["role"] in REPAIRS else "writing"
            elif action["role"] in ("verify", "review"):
                category = STEPS[action["role"]]
        if not category or not (window := windows.get(timing["run_id"])):
            continue
        if not (start := timestamp(timing.get("started_at"))):
            continue
        start = max(window.start, start)
        end = window.cap(timestamp(timing.get("finished_at")))
        if (end is not None and start <= end) or (end is None and window.running and action["state"] == "running"):
            spans.append(Span(window.run_id, category, label, start, end, action["id"]))
    return spans


def step_spans(log: list[dict], window: Window, gates: list[Span]) -> list[Span]:
    spans, starts = [], {}
    for line in log:
        event, step = line.get("event"), line.get("step")
        if event not in ("step_start", "step_end") or not isinstance(step, str):
            continue
        attempt = line.get("try", 1)
        if type(attempt) is not int or not (at := timestamp(line.get("ts"))):
            continue
        key = (step, attempt)
        if line.get("carried") or line.get("preflight") or at < window.start:
            continue
        if event == "step_start":
            atom = line.get("atom")
            repair = step in REPAIRS or (isinstance(atom, str) and atom in REPAIRS)
            activity = "review" if atom == "review" and not repair else step
            category = "retry" if repair else STEPS.get(activity)
            if category:
                starts.setdefault(key, (at, category, activity))
        elif key in starts:
            start, category, activity = starts.pop(key)
            if start <= (end := window.cap(at)):
                spans.append(Span(window.run_id, category, activity, start, end))
    if window.running and window.end is None:
        spans.extend(Span(window.run_id, category, activity, start, None)
                     for start, category, activity in starts.values())
    return [span for span in spans if not any(gate.activity == span.activity and gate.category == span.category
                                             and gate.start <= span.start
                                             and (gate.end is None or span.end is not None and span.end <= gate.end)
                                             for gate in gates)]


def check_spans(timeline: list[dict], window: Window) -> list[Span]:
    spans = set()
    for mark in timeline:
        if mark.get("kind") != "check" or not (at := timestamp(mark.get("at"))):
            continue
        seconds = mark.get("seconds")
        if type(seconds) not in (float, int) or not seconds > 0:
            continue
        try:
            end = window.cap(at + timedelta(seconds=seconds))
        except OverflowError:
            continue
        start = max(at, window.start)
        if start < end:
            spans.add(Span(window.run_id, "checks", "checks", start, end))
    return sorted(spans, key=lambda span: (span.start, span.end))


def wait_span(run_id, cause, start, end, source_id=None) -> Span:
    return Span(run_id, WAITS[cause][0], cause, start, end, source_id, wait=True)


def ask_spans(row: dict, log: list[dict], latest: bool) -> list[Span]:
    rid = row["id"]
    items = asks.asks_of(rid)
    notified = timestamp(unblock.notified_at(rid)) if items else None
    spans = []
    for ask in items:
        start, end = timestamp(ask["asked"]), timestamp(ask["answered"])
        if start is None or (end is None and (not latest or ask["status"] != "open")):
            continue
        if end is not None and end < start:
            continue
        cause = "unblock" if ask["recovery_route"] == "unblock" else "person"
        if cause == "person" and notified and start < notified:
            spans.append(wait_span(rid, "unblock", start, min(notified, end) if end else notified, ask["id"]))
            start = notified
        if end is None or start <= end:
            spans.append(wait_span(rid, cause, start, end, ask["id"]))
    if not items and latest and row["status"] == "needs-info":
        question = next((line for line in reversed(log) if line.get("event") == "question"), {})
        if start := timestamp(question.get("ts")):
            cause = "unblock" if question.get("recovery") else "person"
            spans.append(wait_span(rid, cause, start, None))
    return spans


def wait_spans(log: list[dict], window: Window, recorded_asks: list[Span]) -> list[Span]:
    starts, spans = set(), []
    for line in log:
        cause = line.get("cause")
        if line.get("event") != "wait" or not isinstance(cause, str) or cause not in WAITS:
            continue
        at, since = timestamp(line.get("at")), timestamp(line.get("since"))
        if at and line.get("phase") == "start":
            starts.add((cause, at))
        elif at and since and line.get("phase") == "end":
            starts.discard((cause, since))
            end = window.cap(at)
            if end < since:
                continue
            if cause in ("person", "unblock") and since < window.start and any(
                    ask.run_id != window.run_id and ask.source_id is not None and ask.activity == cause
                    and ask.start < end and (ask.end is None or since < ask.end) for ask in recorded_asks):
                continue
            spans.append(wait_span(window.run_id, cause, since, end))
    if window.running and window.end is None:
        spans.extend(wait_span(window.run_id, cause, since, None) for cause, since in sorted(starts)
                     if since >= window.start)
    return spans


def prerequisite_waits(windows: dict[str, Window], rows: dict, candidates: list[dict], latest: str) -> list[Span]:
    retries = {parent: at for row in candidates if (parent := row.get("environment_retry_of")) in windows
               and (at := timestamp(row["started"]))}
    spans = []
    for rid, window in windows.items():
        retry_at = retries.get(rid)
        waiting = rid == latest and rows[rid]["status"] == "pr"
        if window.end is None or (retry_at is None and not waiting):
            continue
        hold = runs.environment_hold(rid)
        if not hold.get("heads"):
            continue
        if retry_at is not None:
            if window.end < retry_at:
                spans.append(wait_span(rid, "prerequisite", window.end, retry_at))
        elif not hold["released"]:
            spans.append(wait_span(rid, "prerequisite", window.end, None))
    return spans


def summarize(spans: list[Span], start: datetime, end: datetime) -> dict:
    changes = defaultdict(Counter, {start: Counter(), end: Counter()})
    for span in spans:
        lower, upper = max(start, span.start), min(end, span.end or end)
        if lower < upper:
            check_run = span.run_id if span.activity == "checks" else ""
            key = (span.category, span.wait, check_run)
            changes[lower][key] += 1
            changes[upper][key] -= 1
    totals = dict.fromkeys(CATEGORIES, 0.0)
    overlap, unattributed = Counter(), 0.0
    counts, last = Counter(), start
    for at in sorted(changes):
        seconds = (at - last).total_seconds()
        active = Counter()
        for (category, wait, check_run), count in counts.items():
            active[category] += 1 if wait or check_run else count
        for category in active:
            totals[category] += seconds
        if not active:
            unattributed += seconds
        elif sum(active.values()) > 1:
            overlap[tuple((category, active[category]) for category in CATEGORIES if active[category])] += seconds
        counts += changes[at]
        last = at
    return {"category_seconds": totals, "unattributed_seconds": unattributed,
            "overlap_seconds": sum(overlap.values(), 0.0),
            "overlap": [{"activity": dict(group), "seconds": seconds} for group, seconds in sorted(overlap.items())]}


def project(run_id: str, *, as_of: datetime) -> dict | None:
    if as_of.utcoffset() is None:
        raise ValueError("as_of must include a time zone")
    as_of = as_of.astimezone(UTC)
    with reader() as c:
        row = runs.run_get(run_id)
        if not row or not eligible(row) or not row["issue"]:
            return None
        candidates = runs.ticket_runs(row["repo"], row["issue"], limit=-1, whole=True, live=True)
        starts = {item["id"]: start for item in candidates
                  if (start := timestamp(item["started"])) and start <= as_of}
        rows = {item["id"]: item for item in reversed(candidates) if item["id"] in starts}
        if run_id not in rows:
            return None
        logs = {rid: read_lines(rid) for rid in rows}
        linked, roots = lineage(run_id, rows, logs)
        timelines = {rid: read_timeline(rid) for rid in linked}
        windows = {}
        for rid in linked:
            start = starts[rid]
            end = settled_at(c, rows[rid], start, timelines[rid])
            windows[rid] = Window(rid, start, end, rows[rid]["status"] == "running" or bool(end and end > as_of))
        latest, latest_ticket = linked[-1], next(reversed(rows))
        actions = action_spans([action for root in roots for action in orchestration.timed_actions(root)], windows)
        owner_waits = [span for rid in linked for span in ask_spans(rows[rid], logs[rid], rid == latest_ticket)]
        spans = [*actions, *owner_waits, *prerequisite_waits(windows, rows, candidates, latest_ticket)]
        for rid, window in windows.items():
            gates = [span for span in actions if span.run_id == rid and span.activity in GATES.values()]
            spans.extend(step_spans(logs[rid], window, gates))
            spans.extend(check_spans(timelines[rid], window))
            spans.extend(wait_spans(logs[rid], window, owner_waits))
        if latest == latest_ticket and not windows[latest].running:
            for item in sources.tickets_passed_over():
                if item["repo"] == row["repo"] and str(item["issue"]) == str(row["issue"]):
                    if (since := timestamp(item["since"])) and starts[latest] <= since <= as_of:
                        spans.append(wait_span(latest, "slot", since, None))
                    break
    visible = [span for span in spans if span.start <= as_of]
    start = min([*(window.start for window in windows.values()), *(span.start for span in visible)])
    ends = [window.end or (as_of if window.running else window.start) for window in windows.values()]
    through = min(as_of, max([*ends, *(span.end or as_of for span in visible)]))
    active = sorted((span for span in visible if span.active(as_of)),
                    key=lambda span: (span.start, span.run_id, span.activity, span.source_id or ""))
    blockers = {}
    for span in active:
        if span.wait:
            blockers.setdefault((span.run_id, span.activity, span.source_id),
                                {**span.identity(), "cause": span.activity, "next_event": WAITS[span.activity][1]})
    return {"run_ids": linked, "current_run_id": latest, "as_of": iso(as_of),
            "running_run_ids": [rid for rid, window in windows.items() if window.running],
            "started_at": iso(start), "through_at": iso(through), "elapsed_seconds": (through - start).total_seconds(),
            "elapsed_complete": all(window.end is not None or window.running for window in windows.values()),
            **summarize(visible, start, through),
            "active": [{**span.identity(), "activity": span.activity} for span in active if not span.wait],
            "blockers": list(blockers.values())}
