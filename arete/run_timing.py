from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import chain, pairwise
from stat import S_ISREG

from . import paths, pipeline, record, run_lines, runlimits, util
from .store import asks, db, orchestration, runs, sources, unblock

CATEGORIES = ("writing", "checks", "review", "owner_wait", "queue_wait", "retry")
GATES = {"checks.run": "checks", "verification.run": "verify", "review.run": "review"}
STEPS = {"implement": "writing", "build": "writing", "patch": "writing", "revise": "writing",
         "document": "writing", "checks": "checks", "verify": "checks", "review": "review"}
REPAIRS = runlimits.REPAIRS.union(runlimits.REPAIR_NAMES)
WAITS = {"person": ("owner_wait", "The owner answers the open question"),
         "slot": ("queue_wait", "A run slot becomes available"),
         "limit": ("queue_wait", "The provider limit resets"),
         "ci": ("queue_wait", "CI reports its result"),
         "provider": ("queue_wait", "An eligible provider route becomes available"),
         "prerequisite": ("queue_wait", "Verification can retry after its prerequisites are ready"),
         "owner-review": ("owner_wait", "Owner reviews the draft and decides how to unblock verification"),
         "draft-review": ("owner_wait", "The owner addresses the blocker or takes over the draft"),
         "draft-ready": ("owner_wait", "The owner marks the draft ready for review"),
         "unblock": ("retry", "Automatic recovery reports its result")}
CHECK_RESOURCES = {"check-slot": "A check slot becomes available",
                   "full-suite-lock": "The full-suite lock becomes available"}
PARENT_FIELDS = {"route": "continues", "repair": "source", "recovery": "source", "answer": "asked_in"}
RUN_MESSAGE = re.compile(r"(\S+) \[[^\]]+\] (.*)")
RESTART = re.compile(r"restarting after run (\S+) was interrupted")
DRAFT_CHANGE = re.compile(r"(?:opened (\S+) as draft|(?:ci|pr|automerge|failed): marked (\S+) (a draft|ready for review))")


def earliest(*times: datetime | None) -> datetime | None:
    return min((at for at in times if at is not None), default=None)


@dataclass(frozen=True)
class Span:
    run_id: str
    category: str
    activity: str
    start: datetime
    end: datetime | None
    source_id: str | None = None
    wait: bool = False
    suite: str | None = None
    resource: str | None = None
    step_attempt: tuple[str, int] | None = None
    name: str = ""
    check_no: int | None = None

    def active(self, at: datetime) -> bool:
        return self.start <= at and (self.end is None or at < self.end)

    def detail(self) -> dict:
        detail = {"run_id": self.run_id, "category": self.category, "since": iso(self.start), "source_id": self.source_id}
        detail["cause" if self.wait else "activity"] = self.activity
        if self.wait:
            detail["next_event"] = WAITS[self.activity][1]
            if self.resource:
                detail.update(resource=self.resource, suite=self.suite, next_event=CHECK_RESOURCES[self.resource])
        elif self.name:
            detail.update(name=self.name, suite=self.suite, check_no=self.check_no)
        if self.step_attempt and (not self.wait or self.resource):
            detail.update(zip(("step", "try"), self.step_attempt, strict=True))
        return detail


@dataclass(frozen=True)
class Window:
    run_id: str
    start: datetime
    end: datetime | None
    running: bool
    interrupted: bool = False

def timestamp(value) -> datetime | None:
    with suppress(ValueError, OverflowError):
        if isinstance(value, str) and (at := util.parse_iso(value)).utcoffset() is not None:
            return at.astimezone(UTC)
    return None

def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")

def read_log(run_id: str, *, timeline: bool = False) -> list[dict]:
    lines = []
    with suppress(OSError):
        lines.extend(record.lines(paths.record_dir(run_id) / record.TIMELINE) if timeline else run_lines.read(run_id))
    return lines

def last_event(log: list[dict], event: str) -> dict:
    return next((line for line in reversed(log) if line.get("event") == event and not line.get("carried")), {})

def tracker_question(log: list[dict]) -> dict:
    question = last_event(log, "question")
    return {} if question.get("ask_id") or question.get("recovery") else question


@dataclass(frozen=True)
class DraftReview:
    prs: tuple[str, ...]
    reason: str
    cause: str = "draft-review"
    since: datetime | None = None


def run_messages(run_id: str) -> Iterator[tuple[datetime, str]]:
    with suppress(OSError), (paths.RUNS_DIR / run_id / "run.log").open(errors="replace") as log:
        for line in log:
            if (match := RUN_MESSAGE.fullmatch(line.rstrip("\r\n"))) and (at := timestamp(match[1])):
                yield at, match[2]

def draft_report(row: dict, log: list[dict]) -> dict:
    report = last_event(log, run_lines.END)
    return report if (row.get("pr_url") and not row.get("environment_retry_of") and report.get("status") == "pr"
                      and (report.get("held") or last_event(log, "repair")) and not runs.environment_hold(row["id"])) else {}

def draft_since(run_id: str, prs: tuple[str, ...]) -> datetime | None:
    opened = {}
    for at, message in run_messages(run_id):
        if (change := DRAFT_CHANGE.fullmatch(message)) and (pr := change[1] or change[2]) in prs:
            if change[3] == "ready for review":
                opened.pop(pr, None)
            else:
                opened.setdefault(pr, at)
    return earliest(*opened.values(), timestamp(runs.draft_review_at(run_id, runs.DRAFT_OBSERVED)))

def draft_chains(rows: dict, logs: dict) -> Iterator[tuple[dict[str, DraftReview], list[str], datetime | None]]:
    inherited, chains, repairs, ready = {}, defaultdict(dict), defaultdict(list), {}
    for rid, row in rows.items():
        log = logs[rid]
        source = last_event(log, "repair").get("source")
        origin, prior = inherited.get(source, (rid, None)) if isinstance(source, str) else (rid, None)
        if prior and (ended := ready.get(origin)) and (started := timestamp(row["started"])) and ended <= started:
            origin, prior = rid, None
        if prior:
            inherited[rid] = origin, prior
            repairs[origin].append(rid)
        if row["status"] not in ("pr", "superseded", "merged", "rejected") or not (report := draft_report(row, log)):
            continue
        prs, reason = tuple(row["pr_url"].split()), str(report.get("held") or "")
        since = draft_since(rid, prs)
        if not (prior and set(prior.prs).issubset(prs)):
            if since is None:
                continue
            origin, prior = rid, None
        draft = DraftReview(prs, reason, since=since) if reason else prior and replace(prior, prs=prs, since=since)
        if not reason and isinstance(review := report.get("review"), dict) and review.get("verdict") == "approve":
            draft = DraftReview(prs, "The repair review approved it, but the PR is still a draft", "draft-ready", since)
        if draft:
            inherited[rid], chains[origin][rid] = (origin, draft), draft
            ready[origin] = earliest(ready.get(origin), timestamp(runs.draft_review_at(rid)))
    for origin, members in chains.items():
        last = next(reversed(members))
        status = rows[last]["status"]
        closed = (timestamp(rows[rid]["finished"]) for rid in members if rows[rid]["status"] in ("merged", "rejected"))
        superseded = timestamp(runs.superseded_at(last)) if status == "superseded" else None
        if (end := earliest(ready[origin], superseded, *closed)) is not None or status == "pr":
            yield members, repairs[origin], end


def lineage(run_id: str, rows: dict, logs: dict, starts: dict[str, datetime]) -> tuple[list[str], list[str]]:
    neighbors, roots, interrupted = defaultdict(set), {}, set()
    for rid, row in rows.items():
        log = logs[rid]
        binding = unblock.for_run(rid).get("grant", {}).get("binding") or {}
        parents = [row.get("environment_retry_of"), binding.get("run")]
        parents.extend(line.get(PARENT_FIELDS[event]) for line in log
                       if isinstance(event := line.get("event"), str) and event in PARENT_FIELDS)
        if interrupted and not any(line.get("event") == "execution" and line.get("path") != "classic" for line in log):
            parents.append(next((match[1] for at, msg in run_messages(rid) if at >= starts[rid]
                                 and (match := RESTART.fullmatch(msg)) and match[1] in interrupted), None))
        if row["status"] == "interrupted":
            interrupted.add(rid)
        for parent in (p for p in parents if isinstance(p, str) and p in rows):
            neighbors[rid].add(parent)
            neighbors[parent].add(rid)
        if rid not in roots and (saved := orchestration.find(rid)):
            root = saved["root"]
            group = ("orchestration", root)
            for member in (rid, root, saved["current_run"], *saved["data"].get("runs", [])):
                if isinstance(member, str) and member in rows:
                    roots[member] = root
                    neighbors[group].add(member)
                    neighbors[member].add(group)
    seen, todo = {run_id}, {run_id}
    while todo:
        todo.update(new := neighbors[todo.pop()] - seen)
        seen.update(new)
    linked = [rid for rid in rows if rid in seen]
    return linked, list(dict.fromkeys(roots[rid] for rid in linked if rid in roots))

def settled_at(row: dict, start: datetime, timeline: list[dict]) -> datetime | None:
    if row["status"] == "running":
        return None
    settled, legacy = runs.execution_finished(row)
    results = (mark.get("at") for mark in timeline if mark.get("kind") == "result")
    return next((at for value in chain((settled,), results, (legacy,))
                 if (at := timestamp(value)) and at >= start), None)

def action_spans(actions: Iterable[dict], windows: dict[str, Window], receipts: dict) -> dict[str, list[Span]]:
    spans = defaultdict(list)
    for action in actions:
        op, timing, role = action["op"], action["timing"], action["role"]
        label, worker = GATES.get(op, op), op in ("worker.start", "worker.continue")
        category = STEPS.get(role if worker and role in ("verify", "review") else label)
        if worker and action["writing"] == 1:
            category = "retry" if role in REPAIRS else "writing"
        rid, aid = timing["run_id"], action["id"]
        if not category or not (window := windows.get(rid)) or not (at := timestamp(timing.get("started_at"))):
            continue
        start, finished = max(window.start, at), timestamp(timing.get("finished_at"))
        span = Span(rid, category, label, start, earliest(window.end, finished), aid)
        if span.end is None or start <= span.end:
            if finished or (window.running and action["state"] == "running"):
                spans[rid].append(span)
            elif worker:
                spans[rid].extend(measured(span, receipts[rid][f"worker-{aid}"]))
    return spans

def step_attempt(line: dict, default: int | None = None) -> tuple[str, int] | None:
    step, attempt = line.get("step"), line.get("try", default)
    return (step, attempt) if isinstance(step, str) and step and type(attempt) is int else None

def suite_id(line: dict) -> str | None:
    suite = line.get("suite")
    return suite if isinstance(suite, str) and suite else None

def duration_end(mark: dict, at: datetime) -> datetime | None:
    with suppress(OverflowError):
        if type(seconds := mark.get("seconds")) in (float, int) and seconds >= 0:
            return at + timedelta(seconds=seconds)
    return None

def agent_receipts(timeline: list[dict]) -> dict:
    receipts = defaultdict(set)
    for mark in timeline:
        if (mark.get("kind") == "agent" and isinstance(call := mark.get("step"), str)
                and (at := timestamp(mark.get("at"))) and (end := duration_end(mark, at)) and at < end):
            receipts[call].add((at, end))
    return receipts

def measured(span: Span, receipts: Iterable[tuple[datetime, datetime]]) -> Iterator[Span]:
    through = span.start
    for start, end in sorted(receipts):
        if start < span.start:
            continue
        start, end = max(through, start), earliest(end, span.end)
        if start < end:
            yield replace(span, start=start, end=end)
            through = end

def step_kind(label: str, atom) -> tuple[str, str]:
    base = label.rstrip("0123456789")
    if base in REPAIRS or isinstance(atom, str) and atom in REPAIRS:
        return "retry", label
    activity = base if base in STEPS else atom if isinstance(atom, str) and atom in STEPS else label
    return STEPS.get(activity, ""), activity

def step_spans(log: list[dict], receipts: dict, window: Window,
               boundaries: list[Span]) -> tuple[list[Span], list[Span], list[str]]:
    frames, steps, repairs = {}, [], []

    def finish(step, calls, end, complete=False):
        outer = step.category and (complete or window.running)
        step = replace(step, end=end)
        if step.category and (outer or not calls):
            steps.extend([step] if outer else measured(step, receipts[step.step_attempt[0]]))
        for (start, call, atom), (later, _, _) in pairwise([*calls, (None, "", None)]):
            category, activity = step_kind(call, atom)
            inline = step.activity == "verify" and category == "retry"
            catch_up = step.activity == "publish" and category in ("checks", "review")
            if inline or (category and not outer and (step.category or catch_up)):
                resume = earliest(*(s.start for s in boundaries
                                    if inline and s.step_attempt == step.step_attempt and start < s.start))
                span = replace(step, category=category, activity=activity, start=start, end=earliest(end, later, resume))
                (repairs if inline else steps).extend(list(measured(span, receipts[call]))
                                                      or ([span] if window.running and span.end is None else []))

    for line in log:
        event = line.get("event")
        if (event not in ("step_start", "step_end") or not (key := step_attempt(line, 1)) or line.get("carried")
                or line.get("preflight") or key[0].startswith("worker-")
                or not (at := timestamp(line.get("ts"))) or at < window.start):
            continue
        atom, call = line.get("atom"), line.get("call")
        if call:
            if event == "step_start" and isinstance(call, str) and key in frames:
                frames[key][1].append((at, call, atom))
        elif event == "step_start":
            category, activity = step_kind(key[0], atom)
            frames.setdefault(key, (Span(window.run_id, category, activity, at, None, step_attempt=key), []))
        elif (frame := frames.pop(key, None)) and frame[0].start <= (end := earliest(window.end, at)):
            finish(*frame, end, True)
    for frame in frames.values():
        finish(*frame, window.end)
    return steps, repairs, [step for step, _ in reversed(frames)]

def check_spans(timeline: list[dict], window: Window) -> list[Span]:
    starts, completed = {}, {}
    for mark in timeline:
        kind, number = mark.get("kind"), mark.get("check_no")
        if kind not in ("check_start", "check") or not (at := timestamp(mark.get("at"))):
            continue
        suite, name = suite_id(mark), mark.get("name")
        key = (suite, number) if suite and type(number) is int and number > 0 else None
        end = duration_end(mark, at) if kind == "check" else None
        span = Span(window.run_id, "checks", "checks", max(at, window.start), earliest(end, window.end),
                    suite=suite, step_attempt=step_attempt(mark), name=name if isinstance(name, str) else "",
                    check_no=key[1] if key else None)
        if end is not None:
            completed.setdefault(key or span, span)
        elif kind == "check_start" and key:
            starts.setdefault(key, span)
    for key, start in starts.items():
        if (result := completed.get(key)) and start.start <= result.end:
            completed[key] = replace(result, start=start.start, name=start.name or result.name,
                                     step_attempt=start.step_attempt or result.step_attempt)
        elif window.running:
            completed.setdefault(key, start)
    return [s for s in completed.values() if s.end is None or s.start < s.end]

def unpaused(span: Span, waits: Iterable[Span | Window]):
    start = span.start
    for wait in sorted(waits, key=lambda pause: pause.start):
        if span.end is not None and span.end <= wait.start:
            break
        if wait.end is not None and wait.end <= start:
            continue
        if start < wait.start:
            yield replace(span, start=start, end=wait.start)
        if wait.end is None:
            return
        start = max(start, wait.end)
    if span.end is None or start < span.end:
        yield replace(span, start=start)

def activity_spans(actions: list[Span], steps: list[Span], commands: list[Span], waits: list[Span],
                   repairs: list[Span]) -> Iterator[Span]:
    gates = [s for s in actions if s.activity in GATES.values()]
    covered = {s: [g for g in gates if (g.activity, g.category) == (s.activity, s.category) and g.start <= s.start
                   and (g.end is None or s.end is not None and s.end <= g.end)] for s in steps}
    pauses = defaultdict(list)
    for step, matching in covered.items():
        if (step.activity, step.category) == ("verify", "checks"):
            nested = [s for s in chain(commands, repairs) if s.step_attempt == step.step_attempt]
            for span in (step, *(matching if len(matching) == 1 else ())):
                pauses[span].extend(nested)
    for command in commands:
        pauses[command] = [r for r in repairs if r.step_attempt == command.step_attempt] + [
            w for w in waits if w.resource and w.suite == command.suite]
    for wait in waits:
        active = (s for s in steps if s.active(wait.start))
        if wait.resource and (scope := wait.step_attempt):
            checks = [s for s in active if s.category == "checks"]
            matches = [s for s in checks if s.step_attempt == scope]
            covering = {gate for step in (matches or checks) for gate in covered[step]}
            candidates = [g for g in gates if g.category == "checks" and g.active(wait.start)
                          and (g in covering if matches else g.activity == scope[0] and g not in covering)]
        elif wait.activity == "limit" and wait.start != wait.end:
            matches = [s for s in active if s.activity != "checks"]
            if len(matches) != 1:
                continue
            step = matches[0]
            candidates = covered[step]
            matches.extend(r for r in repairs if r.step_attempt == step.step_attempt)
        else:
            continue
        for span in [*matches, *(candidates if len(candidates) == 1 else ())]:
            pauses[span].append(wait)
    independent = (s for s in steps if not any(pauses[s] == pauses[g] for g in covered[s]))
    for span in chain(actions, independent, commands, repairs):
        yield from unpaused(span, pauses[span])

def wait_span(run_id, cause, start, end, source_id=None, **scope) -> Span:
    return Span(run_id, WAITS[cause][0], cause, start, end, source_id, wait=True, **scope)

def saved_reply_at(run_id: str) -> datetime | None:
    with suppress(OSError, ValueError, OverflowError):
        saved = (run_lines.path(run_id).parent / pipeline.ANSWER_FILE).stat()
        if S_ISREG(saved.st_mode) and saved.st_size:
            return datetime.fromtimestamp(saved.st_mtime, UTC).replace(microsecond=0)
    return None

def ask_spans(row: dict, log: list[dict], latest: bool) -> Iterator[Span]:
    rid = row["id"]
    items = asks.asks_of(rid)
    if not items and row["status"] == "needs-info" and (question := last_event(log, "question")):
        cause = "unblock" if question.get("recovery") else "person"
        end = saved_reply_at(rid) if cause == "person" and not question.get("ask_id") else None
        if (start := timestamp(question.get("ts"))) and (start <= end if end else latest):
            yield wait_span(rid, cause, start, end)
    notified = timestamp(unblock.notified_at(rid)) if items else None
    for ask in items:
        start, end = timestamp(ask["asked"]), timestamp(ask["answered"])
        if not start or (end < start if end else not latest or ask["status"] != "open"):
            continue
        cause = "unblock" if ask["recovery_route"] == "unblock" else "person"
        if cause == "person" and notified and start < notified:
            yield wait_span(rid, "unblock", start, earliest(notified, end), ask["id"])
            start = notified
        if end is None or start <= end:
            yield wait_span(rid, cause, start, end, ask["id"])

def wait_spans(log: list[dict], window: Window, recorded_asks: list[Span], commands: list[Span],
               provider_since: datetime | None, provider_end: datetime | None) -> Iterator[Span]:
    scopes, pending = defaultdict(set), {}
    for check in commands:
        if check.suite and check.step_attempt:
            scopes[check.suite].add(check.step_attempt)
    prior_asks = [a for a in recorded_asks if a.run_id != window.run_id]

    def span(key, end, attempt=None):
        cause, suite, resource, since = key
        attempt = attempt or (next(iter(scopes[suite])) if suite and len(scopes[suite]) == 1 else None)
        return wait_span(window.run_id, cause, since, end, suite, suite=suite, resource=resource, step_attempt=attempt)

    for line in log:
        cause, resource = line.get("cause"), line.get("resource")
        phase = line.get("phase")
        if (line.get("event") != "wait" or phase not in ("start", "end") or not isinstance(cause, str)
                or cause not in WAITS or line.get("carried") or not (at := timestamp(line.get("at")))):
            continue
        scoped = cause == "slot" and isinstance(resource, str) and resource in CHECK_RESOURCES
        suite = suite_id(line) if scoped else None
        since = at if phase == "start" else timestamp(line.get("since"))
        if not since or since > at or (scoped and (not suite or line.get("run") != window.run_id or at < window.start)):
            continue
        key = cause, suite, resource if scoped else None, since
        if phase == "start":
            if cause != "provider":
                pending.setdefault(key, step_attempt(line) if scoped else None)
            continue
        end = earliest(at, provider_end if cause == "provider" else window.end)
        matched = key in pending
        attempt = pending.pop(key, None)
        if scoped:
            if matched and since < end:
                yield span(key, end, attempt or step_attempt(line))
            continue
        if cause == "provider" and since == provider_since:
            provider_since = None
        if since <= end and not any(a.activity == cause and (a.start == since and a.end is not None
                                    or a.source_id and a.start < end and (a.end is None or since < a.end))
                                    for a in prior_asks if since <= window.start):
            yield span(key, end)
    for key, attempt in pending.items():
        cause, _, resource, since = key
        truncated = resource or cause == "limit" or (window.interrupted and cause in ("ci", "slot"))
        if since >= window.start and (since < window.end and truncated if window.end else window.running):
            yield span(key, window.end, attempt)
    if provider_since and (provider_end is None or provider_since < provider_end):
        yield wait_span(window.run_id, "provider", provider_since, provider_end)

def environment_waits(windows: dict[str, Window], rows: dict, latest: str) -> Iterator[Span]:
    for rid, window in windows.items():
        hold = runs.environment_hold(rid)
        if not hold.get("heads"):
            continue
        retry_id = hold["retry_id"]
        queue_end = earliest(timestamp(hold["retry_started_at"]), timestamp(hold["released_at"]))
        queued = not hold["released"] and not retry_id and rid == latest and rows[rid]["status"] == "pr"
        if window.end is not None and (queued if queue_end is None else window.end < queue_end):
            yield wait_span(rid, "prerequisite", window.end, queue_end)
        if (hold["release_state"] != "owner-review" or retry_id not in windows
                or not (review_start := timestamp(hold["owner_review_started_at"])) or review_start < windows[retry_id].start):
            continue
        retry = rows[retry_id]
        closed = timestamp(retry["finished"]) if retry["status"] in ("merged", "rejected") else None
        review_end = earliest(closed, timestamp(hold["owner_review_ended_at"]))
        reviewing = retry_id == latest and retry["status"] == "pr" and not retry["stop_asked"]
        if review_start < review_end if review_end else reviewing:
            yield wait_span(retry_id, "owner-review", review_start, review_end)

def draft_waits(chains: list, windows: dict[str, Window], logs: dict) -> Iterator[Span]:
    for members, repairs, end in (c for c in chains if next(iter(c[0])) in windows):
        pauses = [windows[rid] for rid in repairs if windows[rid].running or windows[rid].end is not None]
        next_publication = None
        for rid, draft in reversed(members.items()):
            window = windows[rid]
            if published := draft.since or window.end or timestamp(last_event(logs[rid], run_lines.END).get("ts")):
                start, until = max(window.start, published), earliest(end, next_publication)
                yield from unpaused(wait_span(rid, draft.cause, start, until), pauses)
                next_publication = start

def summarize(spans: list[Span], start: datetime, end: datetime) -> dict:
    changes = defaultdict(Counter, {start: Counter(), end: Counter()})
    for span in spans:
        lower, upper = max(start, span.start), min(end, span.end or end)
        if lower < upper:
            check_run = span.run_id if span.activity == "checks" else ""
            key = (span.category, span.wait, check_run, (span.suite or "") if check_run else "")
            changes[lower][key] += 1
            changes[upper][key] -= 1
    totals, overlap, counts = dict.fromkeys(CATEGORIES, 0.0), Counter(), Counter()
    unattributed = 0.0
    for lower, upper in pairwise(sorted(changes)):
        counts += changes[lower]
        seconds = (upper - lower).total_seconds()
        active = Counter()
        named_check_runs = {run for _, _, run, suite in counts if suite}
        for (category, wait, check_run, suite), count in counts.items():
            if suite or check_run not in named_check_runs:
                active[category] += 1 if wait or check_run else count
        for category in active:
            totals[category] += seconds
        if not active:
            unattributed += seconds
        elif sum(active.values()) > 1:
            overlap[tuple((category, active[category]) for category in CATEGORIES if active[category])] += seconds
    return {"category_seconds": totals, "unattributed_seconds": unattributed,
            "overlap_seconds": sum(overlap.values(), 0.0),
            "overlap": [{"activity": dict(group), "seconds": seconds} for group, seconds in sorted(overlap.items())]}


def project(run_id: str, *, as_of: datetime) -> dict | None:
    if as_of.utcoffset() is None:
        raise ValueError("as_of must include a time zone")
    as_of = as_of.astimezone(UTC)
    with db.one_read():
        row = runs.run_get(run_id)
        if not row or not row["issue"]:
            return None
        candidates = runs.ticket_runs(row["repo"], row["issue"], limit=-1, whole=True, live=True)
        starts = {r["id"]: at for r in reversed(candidates) if (at := timestamp(r["started"])) and at <= as_of}
        rows = {r["id"]: r for r in reversed(candidates) if r["id"] in starts}
        if run_id not in rows:
            return None
        logs = {rid: read_log(rid) for rid in rows}
        linked, roots = lineage(run_id, rows, logs, starts)
        timelines = {rid: read_log(rid, timeline=True) for rid in linked}
        windows = {}
        for rid in linked:
            current, start = rows[rid], starts[rid]
            end = settled_at(current, start, timelines[rid])
            status = current["status"]
            interrupted = bool(end) and (status == "interrupted" or (
                status == "failed" and (current.get("summary") or "").startswith(record.INTERRUPTED)))
            windows[rid] = Window(rid, start, end, status == "running" or bool(end and end > as_of), interrupted)
        latest, latest_ticket = linked[-1], next(reversed(rows))
        following = {rid: starts[after] for rid, after in pairwise(rows)}
        skips = {entry["answer_run"]: entry for entry in pipeline.skipped_tickets().values() if entry.get("answer_run")}
        receipts = {rid: agent_receipts(timeline) for rid, timeline in timelines.items()}
        actions = action_spans((action for root in roots for action in orchestration.timed_actions(root)), windows, receipts)
        owner_waits = [span for rid in linked for span in ask_spans(rows[rid], logs[rid], rid == latest_ticket)]
        drafts = list(draft_chains(rows, logs))
        pending = dict(next(reversed(members.items())) for members, _, end in drafts if end is None)
        draft = next((pending[rid] for rid in reversed(rows) if rid in pending and not rows[rid]["stop_asked"]), None)
        spans = [*owner_waits, *environment_waits(windows, rows, latest_ticket), *draft_waits(drafts, windows, logs)]
        open_steps = {}
        for rid, window in windows.items():
            commands = check_spans(timelines[rid], window)
            stopped = timestamp(rows[rid].get("stop_asked"))
            waits = list(wait_spans(logs[rid], window, owner_waits, commands, timestamp(skips.get(rid, {}).get("since")),
                                    earliest(following.get(rid), stopped)))
            steps, repairs, open_steps[rid] = step_spans(logs[rid], receipts[rid], window, [*commands, *waits])
            spans.extend(chain(activity_spans(actions[rid], steps, commands, waits, repairs), waits))
        if latest == latest_ticket and not windows[latest].running:
            passed = next((item for item in sources.tickets_passed_over()
                           if (item["repo"], str(item["issue"])) == (row["repo"], str(row["issue"]))), {})
            if (since := timestamp(passed.get("since"))) and starts[latest] <= since <= as_of:
                spans.append(wait_span(latest, "slot", since, None))
    visible = [span for span in spans if span.start <= as_of]
    start = min([*(window.start for window in windows.values()), *(span.start for span in visible)])
    ends = (w.end or (as_of if w.running else w.start) for w in windows.values())
    through = min(as_of, max(chain(ends, (s.end or as_of for s in visible))))
    active = sorted((span for span in visible if span.active(as_of)),
                    key=lambda s: (s.start, s.run_id, s.activity, s.source_id or "", s.resource or "",
                                   s.suite or "", s.check_no or 0, s.name))
    blockers = {}
    for s in (span for span in active if span.wait):
        blockers.setdefault((s.run_id, s.activity, s.source_id, s.resource), s.detail())
    return {"run_ids": linked, "current_run_id": latest, "as_of": iso(as_of), "draft": draft, "open_steps": open_steps,
            "running_run_ids": [rid for rid, window in windows.items() if window.running],
            "started_at": iso(start), "through_at": iso(through), "elapsed_seconds": (through - start).total_seconds(),
            "elapsed_complete": all(w.end is not None or w.running for w in windows.values()), **summarize(visible, start, through),
            "active": [s.detail() for s in active if not s.wait], "blockers": list(blockers.values())}
