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
         "provider": ("queue_wait", "An eligible provider route becomes available"),
         "prerequisite": ("queue_wait", "Verification can retry after its prerequisites are ready"),
         "owner-review": ("owner_wait", "Owner reviews the draft and decides how to unblock verification"),
         "draft-review": ("owner_wait", "The owner addresses the blocker or takes over the draft"),
         "draft-ready": ("owner_wait", "The owner marks the draft ready for review"),
         "unblock": ("retry", "Recovery resolves the blocker or routes it to the owner")}
CHECK_RESOURCES = {"check-slot": "A check slot becomes available",
                   "full-suite-lock": "The full-suite lock becomes available"}
PARENT_FIELDS = {"route": "continues", "repair": "source", "recovery": "source", "answer": "asked_in"}
RESTART = re.compile(r"(\S+) \[[^\]]+\] restarting after run (\S+) was interrupted")


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

    def active(self, at: datetime) -> bool:
        return self.start <= at and (self.end is None or at < self.end)

    def identity(self) -> dict:
        return {"run_id": self.run_id, "category": self.category, "since": iso(self.start),
                "source_id": self.source_id}

    def blocker(self) -> dict:
        detail = {**self.identity(), "cause": self.activity, "next_event": WAITS[self.activity][1]}
        if self.resource:
            detail.update(resource=self.resource, suite=self.suite, next_event=CHECK_RESOURCES[self.resource])
            if self.step_attempt:
                detail.update(step=self.step_attempt[0], **{"try": self.step_attempt[1]})
        return detail


@dataclass(frozen=True)
class Window:
    run_id: str
    start: datetime
    end: datetime | None
    running: bool
    interrupted: bool = False


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


def draft_chains(rows: dict, logs: dict) -> Iterator[tuple[dict[str, DraftReview], list[str], datetime | None]]:
    inherited, chains, repairs = {}, defaultdict(dict), defaultdict(list)
    for rid, row in rows.items():
        log = logs[rid]
        source = last_event(log, "repair").get("source")
        origin, prior = inherited.get(source, (rid, None)) if isinstance(source, str) else (rid, None)
        if prior:
            inherited[rid] = origin, prior
            repairs[origin].append(rid)
        if (row["status"] not in ("pr", "superseded", "merged", "rejected") or not row.get("pr_url")
                or row.get("environment_retry_of") or (report := last_event(log, run_lines.END)).get("status") != "pr"):
            continue
        prs, reason = tuple(row["pr_url"].split()), str(report.get("held") or "")
        if prior and set(prior.prs).issubset(prs):
            review = report.get("review")
            draft = (DraftReview(prs, "The repair review approved it, but the PR is still a draft", "draft-ready")
                     if not reason and isinstance(review, dict) and review.get("verdict") == "approve"
                     else DraftReview(prs, reason) if reason else replace(prior, prs=prs))
        elif reason and not runs.environment_hold(rid):
            origin, draft = rid, DraftReview(prs, reason)
        else:
            continue
        inherited[rid] = origin, draft
        chains[origin][rid] = draft
    for origin, members in chains.items():
        last = next(reversed(members))
        status = rows[last]["status"]
        end = earliest(*(timestamp(runs.draft_review_ready_at(rid)) for rid in members),
                       *(timestamp(rows[rid]["finished"]) for rid in members if rows[rid]["status"] in ("merged", "rejected")),
                       timestamp(runs.run_change_at(last, "superseded")) if status == "superseded" else None)
        if end is not None or status == "pr":
            yield members, repairs[origin], end


def pending_draft_reviews(attention: Iterable[dict]) -> Iterator[tuple[dict, DraftReview]]:
    pending = {}
    for row in attention:
        if row["status"] != "pr":
            continue
        rid, key = row["id"], (row["repo"], row["issue"])
        if key not in pending:
            log = read_log(rid)
            report = last_event(log, run_lines.END)
            if report.get("status") != "pr" or not (report.get("held") or last_event(log, "repair")):
                continue
            history = runs.ticket_runs(*key, limit=-1, whole=True, live=True)
            rows = {item["id"]: item for item in reversed(history)}
            logs = {item: log if item == rid else read_log(item) for item in rows}
            pending[key] = dict(next(reversed(members.items())) for members, _, end in draft_chains(rows, logs)
                                if end is None)
        if draft := pending[key].get(rid):
            yield row, draft


def restart_source(run_id: str, started: datetime, interrupted: set[str]) -> str | None:
    with suppress(OSError), (paths.RUNS_DIR / run_id / "run.log").open(errors="replace") as log:
        for line in log:
            if ((match := RESTART.fullmatch(line.rstrip("\r\n"))) and match[2] in interrupted
                    and (at := timestamp(match[1])) and at >= started):
                return match[2]
    return None


def lineage(run_id: str, rows: dict, logs: dict, starts: dict[str, datetime]) -> tuple[list[str], list[str]]:
    neighbors, roots, interrupted = defaultdict(set), {}, set()
    for rid, row in rows.items():
        log = logs[rid]
        binding = unblock.for_run(rid).get("grant", {}).get("binding") or {}
        parents = [row.get("environment_retry_of"), binding.get("run")]
        parents.extend(line.get(PARENT_FIELDS[event]) for line in log
                       if isinstance(event := line.get("event"), str) and event in PARENT_FIELDS)
        if interrupted and not any(line.get("event") == "execution" and line.get("path") != "classic" for line in log):
            parents.append(restart_source(rid, starts[rid], interrupted))
        if row["status"] == "interrupted":
            interrupted.add(rid)
        for parent in parents:
            if isinstance(parent, str) and parent in rows:
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
    seen, todo = set(), {run_id}
    while todo:
        node = todo.pop()
        seen.add(node)
        todo.update(neighbors[node] - seen)
    linked = [rid for rid in rows if rid in seen]
    return linked, list(dict.fromkeys(roots[rid] for rid in linked if rid in roots))


def settled_at(row: dict, start: datetime, timeline: list[dict]) -> datetime | None:
    if row["status"] == "running":
        return None
    settled, legacy = runs.execution_finished(row)
    results = (mark.get("at") for mark in timeline if mark.get("kind") == "result")
    return next((at for value in chain((settled,), results, (legacy,))
                 if (at := timestamp(value)) and at >= start), None)


def action_spans(actions: Iterable[dict], windows: dict[str, Window]) -> dict[str, list[Span]]:
    spans = defaultdict(list)
    for action in actions:
        op, timing = action["op"], action["timing"]
        label = GATES.get(op, op)
        category = STEPS.get(label)
        if op in ("worker.start", "worker.continue"):
            if action["writing"] == 1:
                category = "retry" if action["role"] in REPAIRS else "writing"
            elif action["role"] in ("verify", "review"):
                category = STEPS[action["role"]]
        if not category or not (window := windows.get(timing["run_id"])) or not (
                start := timestamp(timing.get("started_at"))):
            continue
        start = max(window.start, start)
        end = earliest(window.end, timestamp(timing.get("finished_at")))
        if (end is not None and start <= end) or (end is None and window.running and action["state"] == "running"):
            spans[window.run_id].append(Span(window.run_id, category, label, start, end, action["id"]))
    return spans


def step_attempt(line: dict, default: int | None = None) -> tuple[str, int] | None:
    step, attempt = line.get("step"), line.get("try", default)
    return (step, attempt) if isinstance(step, str) and step and type(attempt) is int else None


def suite_id(line: dict) -> str | None:
    suite = line.get("suite")
    return suite if isinstance(suite, str) and suite else None


def timed_marks(timeline: list[dict], kind: str) -> Iterator[tuple[dict, datetime, datetime]]:
    for mark in timeline:
        seconds = mark.get("seconds")
        if (mark.get("kind") == kind and type(seconds) in (float, int) and seconds >= 0
                and (at := timestamp(mark.get("at")))):
            with suppress(OverflowError):
                yield mark, at, at + timedelta(seconds=seconds)


def inline_repairs(step: Span, calls: list, receipts: dict, boundaries: list[Span], window: Window) -> Iterator[Span]:
    for (start, call, repair), (later, _, _) in pairwise([*calls, (None, "", False)]):
        if not repair:
            continue
        resumed = min((span.start for span in boundaries
                       if span.step_attempt == step.step_attempt and start < span.start), default=None)
        end = earliest(step.end, window.end, later, resumed)
        measured = [until for at, until in receipts[call] if start <= at and (end is None or at < end)]
        if measured or step.end is None:
            end = earliest(end, max(measured, default=None))
            if end is None or start < end:
                yield replace(step, category="retry", activity=call, start=start, end=end)


def step_spans(log: list[dict], timeline: list[dict], window: Window, boundaries: list[Span]) -> tuple[list[Span], list[Span]]:
    starts, calls, receipts, steps, repairs = {}, defaultdict(list), defaultdict(list), [], []
    for mark, at, end in timed_marks(timeline, "agent"):
        if isinstance(call := mark.get("step"), str):
            receipts[call].append((at, end))
    for line in log:
        event = line.get("event")
        if (event not in ("step_start", "step_end") or not (key := step_attempt(line, 1)) or line.get("carried")
                or line.get("preflight") or not (at := timestamp(line.get("ts"))) or at < window.start):
            continue
        atom, call = line.get("atom"), line.get("call")
        repair = isinstance(atom, str) and atom in REPAIRS
        if call:
            if (event == "step_start" and isinstance(call, str) and (parent := starts.get(key))
                    and parent.activity == "verify" and parent.category == "checks" and parent.start <= at):
                calls[key].append((at, call, repair or call.rstrip("0123456789") in REPAIRS))
        elif event == "step_start":
            step = key[0]
            activity = atom if atom == "review" or atom == "verify" and step.rstrip("0123456789") == atom else step
            if category := ("retry" if repair or step in REPAIRS else STEPS.get(activity)):
                starts.setdefault(key, Span(window.run_id, category, activity, at, None, step_attempt=key))
        elif (span := starts.pop(key, None)) and span.start <= (end := earliest(window.end, at)):
            step = replace(span, end=end)
            steps.append(step)
            repairs.extend(inline_repairs(step, calls.pop(key, []), receipts, boundaries, window))
    for key, step in starts.items():
        if window.interrupted or (window.running and window.end is None):
            nested = list(inline_repairs(step, calls[key], receipts, boundaries, window))
            if nested or not window.interrupted:
                steps.append(replace(step, end=window.end))
                repairs.extend(nested)
    return steps, repairs


def check_spans(timeline: list[dict], window: Window) -> list[Span]:
    spans = set()
    for mark, at, end in timed_marks(timeline, "check"):
        start = max(at, window.start)
        if start < (end := earliest(window.end, end)):
            spans.add(Span(window.run_id, "checks", "checks", start, end,
                           suite=suite_id(mark), step_attempt=step_attempt(mark)))
    return sorted(spans, key=lambda span: (span.start, span.end, span.suite or ""))


def covers(gate: Span, step: Span) -> bool:
    return (gate.activity == step.activity and gate.category == step.category and gate.start <= step.start
            and (gate.end is None or step.end is not None and step.end <= gate.end))


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
    gates = [action for action in actions if action.activity in GATES.values()]
    covered = {step: [gate for gate in gates if covers(gate, step)] for step in steps}
    pauses = defaultdict(list)
    for step, gates_of_step in covered.items():
        if (step.activity, step.category) == ("verify", "checks"):
            nested = [s for s in chain(commands, repairs) if s.step_attempt == step.step_attempt]
            pauses[step].extend(nested)
            if len(gates_of_step) == 1:
                pauses[gates_of_step[0]].extend(nested)
    for command in commands:
        pauses[command] = [r for r in repairs if r.step_attempt == command.step_attempt]
        pauses[command].extend(w for w in waits if w.resource and w.suite == command.suite)
    for wait in waits:
        active = [step for step in steps if step.active(wait.start)]
        if wait.resource:
            if not (scope := wait.step_attempt):
                continue
            checks = [s for s in active if s.category == "checks"]
            matches = [s for s in checks if s.step_attempt == scope]
            covering = {gate for step in (matches or checks) for gate in covered[step]}
            candidates = [g for g in gates if g.category == "checks" and g.active(wait.start)
                          and (g in covering if matches else g.activity == scope[0] and g not in covering)]
        elif wait.activity == "limit" and wait.start != wait.end:
            matches = [s for s in active if s.activity != "checks" and not s.step_attempt[0].startswith("worker-")]
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
        reply = saved_reply_at(rid) if not (question.get("recovery") or question.get("ask_id")) else None
        items = [dict(id=None, asked=question.get("ts"), answered=iso(reply) if reply else None, status="open",
                      recovery_route="unblock" if question.get("recovery") else "owner")]
    notified = timestamp(unblock.notified_at(rid)) if any(ask["id"] for ask in items) else None
    for ask in items:
        start, end = timestamp(ask["asked"]), timestamp(ask["answered"])
        if start is None or (end < start if end is not None else not latest or ask["status"] != "open"):
            continue
        cause = "unblock" if ask["recovery_route"] == "unblock" else "person"
        if cause == "person" and notified and start < notified:
            yield wait_span(rid, "unblock", start, earliest(notified, end), ask["id"])
            start = notified
        if end is None or start <= end:
            yield wait_span(rid, cause, start, end, ask["id"])


def already_recorded(ask: Span, run_id: str, cause: str, since: datetime, end: datetime) -> bool:
    return ask.run_id != run_id and ask.activity == cause and (
        (ask.end is not None and ask.start == since)
        or (ask.source_id is not None and ask.start < end and (ask.end is None or since < ask.end)))


def wait_spans(log: list[dict], window: Window, recorded_asks: list[Span], commands: list[Span],
               provider_since: datetime | None, provider_end: datetime | None) -> list[Span]:
    scopes, pending, spans = defaultdict(set), {}, []
    for command in commands:
        if command.suite and command.step_attempt:
            scopes[command.suite].add(command.step_attempt)
    for line in log:
        cause, resource = line.get("cause"), line.get("resource")
        if (line.get("event") != "wait" or not isinstance(cause, str) or cause not in WAITS
                or line.get("carried") or not (at := timestamp(line.get("at")))):
            continue
        if cause == "slot" and isinstance(resource, str) and resource in CHECK_RESOURCES:
            if line.get("run") != window.run_id or at < window.start or not (suite := suite_id(line)):
                continue
        else:
            suite = resource = None
        phase = line.get("phase")
        if phase == "start" and cause != "provider":
            pending.setdefault((cause, suite, resource, at), wait_span(
                window.run_id, cause, at, None, suite, suite=suite, resource=resource,
                step_attempt=step_attempt(line) if resource else None))
        elif phase == "end" and (since := timestamp(line.get("since"))) and since <= at:
            start = pending.pop((cause, suite, resource, since), None)
            end = earliest(at, provider_end if cause == "provider" else window.end)
            if cause == "provider" and provider_since == since:
                provider_since = None
            if resource and start and since < end:
                spans.append(replace(start, end=end, step_attempt=start.step_attempt or step_attempt(line)))
            elif not resource and since <= end and not (cause in ("person", "unblock") and since <= window.start and any(
                    already_recorded(ask, window.run_id, cause, since, end) for ask in recorded_asks)):
                spans.append(wait_span(window.run_id, cause, since, end))
    spans.extend(replace(span, end=window.end) for span in pending.values() if span.start >= window.start and (
        window.running and window.end is None or window.end and span.start < window.end
        and (span.resource or span.activity == "limit" or window.interrupted and span.activity in ("ci", "slot"))))
    if provider_since and (provider_end is None or provider_since < provider_end):
        spans.append(wait_span(window.run_id, "provider", provider_since, provider_end))
    suite_scopes = {suite: next(iter(attempts)) for suite, attempts in scopes.items() if len(attempts) == 1}
    return [replace(span, step_attempt=suite_scopes[span.suite])
            if span.step_attempt is None and span.suite in suite_scopes else span for span in spans]


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

        if hold["release_state"] != "owner-review" or retry_id not in windows:
            continue
        review_start = timestamp(hold["owner_review_started_at"])
        if review_start is None or review_start < windows[retry_id].start:
            continue
        retry = rows[retry_id]
        closed = retry["finished"] if retry["status"] in ("merged", "rejected") else None
        review_end = earliest(timestamp(closed), timestamp(hold["owner_review_ended_at"]))
        reviewing = (review_end is None and retry_id == latest and retry["status"] == "pr" and not retry["stop_asked"])
        if reviewing or (review_end is not None and review_start < review_end):
            yield wait_span(retry_id, "owner-review", review_start, review_end)


def draft_waits(windows: dict[str, Window], rows: dict, logs: dict) -> Iterator[Span]:
    for members, repairs, end in draft_chains({rid: rows[rid] for rid in windows}, logs):
        pauses = [windows[rid] for rid in repairs if windows[rid].running or windows[rid].end is not None]
        next_publication = None
        for rid, draft in reversed(members.items()):
            window = windows[rid]
            published = timestamp(runs.run_change_at(rid, "running", pr_url=rows[rid]["pr_url"]))
            settled = window.end or timestamp(last_event(logs[rid], run_lines.END).get("ts"))
            if published := earliest(published, settled):
                start, until = max(window.start, published), earliest(end, next_publication)
                next_publication = start
                if until is None or start < until:
                    yield from unpaused(wait_span(rid, draft.cause, start, until), pauses)


def summarize(spans: list[Span], start: datetime, end: datetime) -> dict:
    changes = defaultdict(Counter, {start: Counter(), end: Counter()})
    for span in spans:
        lower, upper = max(start, span.start), min(end, span.end or end)
        if lower < upper:
            check_run = span.run_id if span.activity == "checks" else ""
            key = (span.category, span.wait, check_run, (span.suite or "") if check_run else "")
            changes[lower][key] += 1
            changes[upper][key] -= 1
    totals = dict.fromkeys(CATEGORIES, 0.0)
    overlap, unattributed = Counter(), 0.0
    counts, last = Counter(), start
    for at in sorted(changes):
        seconds = (at - last).total_seconds()
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
        counts += changes[at]
        last = at
    return {"category_seconds": totals, "unattributed_seconds": unattributed,
            "overlap_seconds": sum(overlap.values(), 0.0),
            "overlap": [{"activity": dict(group), "seconds": seconds} for group, seconds in sorted(overlap.items())]}


def project(run_id: str, *, as_of: datetime) -> dict | None:
    if as_of.utcoffset() is None:
        raise ValueError("as_of must include a time zone")
    as_of = as_of.astimezone(UTC)
    with reader():
        row = runs.run_get(run_id)
        if not row or not row["issue"]:
            return None
        candidates = runs.ticket_runs(row["repo"], row["issue"], limit=-1, whole=True, live=True)
        starts, rows = {}, {}
        for item in reversed(candidates):
            if (start := timestamp(item["started"])) and start <= as_of:
                starts[item["id"]], rows[item["id"]] = start, item
        if run_id not in rows:
            return None
        logs = {rid: read_log(rid) for rid in rows}
        linked, roots = lineage(run_id, rows, logs, starts)
        timelines, windows = {}, {}
        for rid in linked:
            timelines[rid] = read_log(rid, timeline=True)
            current = rows[rid]
            end = settled_at(current, starts[rid], timelines[rid])
            running = current["status"] == "running" or bool(end and end > as_of)
            interrupted = end is not None and (current["status"] == "interrupted" or (
                current["status"] == "failed" and (current.get("summary") or "").startswith(record.INTERRUPTED)))
            windows[rid] = Window(rid, starts[rid], end, running, interrupted)
        latest, latest_ticket = linked[-1], next(reversed(rows))
        following = {rid: starts[after] for rid, after in pairwise(rows)}
        skips = {entry["answer_run"]: entry for entry in pipeline.skipped_tickets().values() if entry.get("answer_run")}
        actions = action_spans((action for root in roots for action in orchestration.timed_actions(root)), windows)
        owner_waits = [span for rid in linked for span in ask_spans(rows[rid], logs[rid], rid == latest_ticket)]
        spans = [*owner_waits, *environment_waits(windows, rows, latest_ticket),
                 *draft_waits(windows, rows, logs)]
        for rid, window in windows.items():
            log, timeline = logs[rid], timelines[rid]
            commands = check_spans(timeline, window)
            waits = wait_spans(log, window, owner_waits, commands, timestamp(skips.get(rid, {}).get("since")),
                               earliest(following.get(rid), timestamp(rows[rid].get("stop_asked"))))
            steps, repairs = step_spans(log, timeline, window, [*commands, *waits])
            spans.extend(chain(activity_spans(actions[rid], steps, commands, waits, repairs), waits))
        if latest == latest_ticket and not windows[latest].running:
            passed = next((item for item in sources.tickets_passed_over()
                           if (item["repo"], str(item["issue"])) == (row["repo"], str(row["issue"]))), {})
            if (since := timestamp(passed.get("since"))) and starts[latest] <= since <= as_of:
                spans.append(wait_span(latest, "slot", since, None))
    visible = [span for span in spans if span.start <= as_of]
    start = min([*(window.start for window in windows.values()), *(span.start for span in visible)])
    ends = [window.end or (as_of if window.running else window.start) for window in windows.values()]
    through = min(as_of, max([*ends, *(span.end or as_of for span in visible)]))
    active = sorted((span for span in visible if span.active(as_of)),
                    key=lambda s: (s.start, s.run_id, s.activity, s.source_id or "", s.resource or ""))
    blockers = {}
    for span in active:
        if span.wait:
            blockers.setdefault((span.run_id, span.activity, span.source_id, span.resource), span.blocker())
    return {"run_ids": linked, "current_run_id": latest, "as_of": iso(as_of),
            "running_run_ids": [rid for rid, window in windows.items() if window.running],
            "started_at": iso(start), "through_at": iso(through), "elapsed_seconds": (through - start).total_seconds(),
            "elapsed_complete": all(window.end is not None or window.running for window in windows.values()),
            **summarize(visible, start, through),
            "active": [{**span.identity(), "activity": span.activity} for span in active if not span.wait],
            "blockers": list(blockers.values())}
