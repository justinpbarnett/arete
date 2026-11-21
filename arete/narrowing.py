from __future__ import annotations

import re
import uuid
from dataclasses import asdict

from . import gh, outbound, paths, run_lines, runlimits, tracker, util
from .review import RECURRING_FAILURES, UNWAIVABLE, Review
from .store import narrowings as store
from .store import runs as run_store

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.()/ -]{0,79}")
HEADING = re.compile(r"#{1,6} +(.+)")
BULLET = re.compile(r"(?:[-*+] |[1-9][0-9]*[.)] )(.+)")
UNSUPPORTED_MARKUP = re.compile(r"^(?: {0,3}(?:```|~~~|<)|[ \t>]+(?:```|~~~))", re.MULTILINE)
PARENT_MARKER = re.compile(r"^<!-- arete:narrowing:parent=[0-9a-f]{32} -->$", re.MULTILINE)
CAP = "the ticket was narrowed twice"
PENDING = "its automatic narrowing has tracker writes awaiting confirmation"
NARROWED = "the ticket was narrowed for poll readmission"


def review_record(run, ticket, review) -> None:
    if run.budget:
        run_lines.write(run.id, "narrowing_review", specification=runlimits.specification(ticket), review=review.asdict())


def verification_record(run, ticket, verified) -> None:
    if run.budget:
        run_lines.write(run.id, "narrowing_verify", specification=runlimits.specification(ticket),
                        verified=asdict(verified))


def evidence(run_ids: list[str], specification: str) -> tuple[int, list[tuple[int, Review]], dict]:
    cycle, reviews, verified = 0, [], {}
    for rid in run_store.chronological_run_ids(run_ids):
        recorded = runlimits.recorded_specification(rid) == specification
        for line in run_lines.read(rid):
            event = line.get("event")
            if event == "budget_repair":
                cycle += 1
                continue
            if event in ("narrowing_review", "narrowing_verify"):
                if line.get("specification") == specification:
                    if event == "narrowing_review":
                        reviews.append((cycle, Review.stored(line["review"])))
                    else:
                        verified = line["verified"]
                continue
            if event != "step_end" or line.get("carried"):
                continue
            data = line.get("data") or {}
            if data.get("specification", specification if recorded else "") != specification:
                continue
            if line.get("step") == "review" and "review" in data:
                reviews.append((cycle, Review.stored(data["review"])))
            elif line.get("step") == "verify" and "verified" in data:
                verified = data["verified"]
    return cycle, reviews, verified


def name(value) -> str:
    return value if (isinstance(value, str) and NAME.fullmatch(value) and value == value.strip()
                     and outbound.redact(value) == value) else ""


def mentions(text, term: str) -> bool:
    return isinstance(text, str) and bool(re.search(rf"(?<![\w.-]){re.escape(term)}(?![\w-]|\.\w)", text, re.I))


def observes_absence(text, term: str) -> bool:
    symbol = rf"`?{re.escape(term)}`?"
    return isinstance(text, str) and bool(re.search(
        rf"\b(?:no|missing) {symbol}(?![\w-]|\.\w)|(?<![\w.-]){symbol} "
        r"(?:does not exist|is (?:absent|missing|not (?:present|available|implemented|exposed|supported)))\b",
        text, re.I))


def acceptance(body: str) -> tuple[list[str], int | None, int]:
    lines = body.splitlines(keepends=True)
    headings = [(i, h[1].strip().casefold()) for i, line in enumerate(lines)
                if (h := HEADING.fullmatch(line.rstrip("\r\n")))]
    sections = [n for n, (_, title) in enumerate(headings) if title == "acceptance criteria"]
    if len(sections) != 1:
        return lines, None, len(lines)
    n = sections[0]
    return lines, headings[n][0] + 1, headings[n + 1][0] if n + 1 < len(headings) else len(lines)


def scope_gap(lines: list[str], start: int, end: int, blockers: list[dict], verified: dict) -> dict:
    if not verified.get("finished", True):
        return {}
    absent = {term.casefold() for check in verified.get("checks", []) if isinstance(check, dict)
              and check.get("cause") != "environment" and (term := name(check.get("missing_prerequisite")))
              and observes_absence(check.get("saw"), term)}
    found = {}
    for blocker in blockers:
        gap = blocker.get("scope_gap")
        if blocker.get("category") != "issue" or not isinstance(gap, dict):
            continue
        requirement, term = gap.get("requirement"), name(gap.get("prerequisite"))
        if (not isinstance(requirement, str) or len(requirement) > 500 or not BULLET.fullmatch(requirement)
                or requirement.startswith("- Out of scope here and deferred to #")
                or tracker.WAIT_LINE.search(requirement) or not term or term.casefold() not in absent
                or not observes_absence(blocker.get("problem"), term)):
            continue
        at = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == requirement]
        if len(at) != 1 or not start <= at[0] < end:
            continue
        after = lines[at[0] + 1].rstrip("\r\n") if at[0] + 1 < end else ""
        if after.strip() and not BULLET.fullmatch(after):
            continue
        found[requirement, term] = {"requirement": requirement, "prerequisite": term, "line": at[0]}
    return next(iter(found.values())) if len(found) == 1 else {}


def recurring(blocker: dict) -> tuple[tuple[str, str], set[str]] | None:
    detail = blocker.get("recurring_failure")
    if blocker.get("category") not in ("issue", "regression") or not isinstance(detail, dict):
        return None
    path, failure, instances = name(detail.get("path")), detail.get("failure"), detail.get("instances")
    problem = blocker.get("problem")
    if (not path or not isinstance(failure, str) or failure not in RECURRING_FAILURES
            or not mentions(problem, path) or not isinstance(instances, list) or not 1 <= len(instances) <= 20
            or any(not name(i) or not mentions(problem, i) for i in instances)):
        return None
    return (failure, path), set(instances)


def recurrence(reviews: list[tuple[int, Review]], blockers: list[dict]) -> dict:
    active = {match[0] for blocker in blockers if (match := recurring(blocker))}
    seen: dict[tuple[str, str], set[str]] = {}
    cycles: dict[tuple[str, str], set[int]] = {}
    for cycle, review in reviews:
        if review.failed:
            continue
        for blocker in review.problems:
            if blocker["severity"] != "blocker" or (match := recurring(blocker)) is None:
                continue
            key, instances = match
            known = seen.setdefault(key, set())
            if instances - known:
                cycles.setdefault(key, set()).add(cycle)
                known.update(instances)
    for failure, path in sorted(active, key=lambda key: (-len(cycles.get(key, ())), key)):
        key = failure, path
        if len(cycles.get(key, ())) >= 2 and len(seen[key]) <= 30:
            return {"failure": failure, "path": path, "instances": sorted(seen[key])}
    return {}


def acceptance_line(recurring: dict) -> str:
    return (f"- Verify that these cases {RECURRING_FAILURES[recurring['failure']]} through `{recurring['path']}`: "
            + ", ".join(f"`{i}`" for i in recurring["instances"]) + ".")


def with_acceptance(lines: list[str], start: int | None, end: int, recurring: dict) -> str:
    added = acceptance_line(recurring)
    prefix = added.split(": ", 1)[0] + ": "
    for i in range(end if start is None else start, end):
        existing = lines[i].rstrip("\r\n")
        if existing == added:
            return "".join(lines)
        if existing.startswith(prefix) and re.fullmatch(r"`[^`]+`(?:, `[^`]+`)*\.", existing[len(prefix):]):
            old = set(re.findall(r"`([^`]+)`", existing[len(prefix):]))
            if not all(map(name, old)) or set(recurring["instances"]) <= old:
                return "".join(lines)
            extended = {**recurring, "instances": sorted(old | set(recurring["instances"]))}
            lines[i] = acceptance_line(extended) + lines[i][len(existing):]
            return "".join(lines)
    body = "".join(lines[:end])
    heading = "## Acceptance criteria\n\n" if start is None else ""
    return body + ("\n" if body.endswith("\n") else "\n\n") + heading + added + "\n" + (
        "\n" + "".join(lines[end:]) if end < len(lines) else "")


def revised(plan: dict, child: int | None = None) -> str:
    lines, start, end = acceptance(plan["body"])
    if gap := plan["gap"]:
        old = lines[gap["line"]]
        requirement = gap["requirement"]
        lines[gap["line"]] = (f"- Out of scope here and deferred to #{child}: {BULLET.fullmatch(requirement)[1]}"
                              + old[len(requirement):])
    body = with_acceptance(lines, start, end, plan["recurrence"]) if plan["recurrence"] else "".join(lines)
    if body == plan["body"]:
        return body
    gap = "\n" if body.endswith("\n") else "\n\n"
    return body + gap + marker(plan, "parent")


def proposal(ticket: tracker.Ticket, run_ids: list[str]) -> dict:
    cycle, reviews, verified = evidence(run_ids, runlimits.specification(ticket))
    if not reviews or reviews[-1][0] != cycle or UNSUPPORTED_MARKUP.search(PARENT_MARKER.sub("", ticket.body)):
        return {}
    final = reviews[-1][1]
    blockers = final.blocking()
    if final.failed or final.verdict != "request_changes" or any(b.get("category") in UNWAIVABLE for b in blockers):
        return {}
    lines, start, end = acceptance(ticket.body)
    gap = scope_gap(lines, start, end, blockers, verified) if start is not None else {}
    repeated = recurrence(reviews, blockers)
    if repeated and with_acceptance(lines, start, end, repeated) == ticket.body:
        repeated = {}
    plan = {"title": ticket.title, "body": ticket.body, "gap": gap, "recurrence": repeated, "key": uuid.uuid4().hex}
    target = revised(plan, 1)
    if target == ticket.body or gh.outgoing(target) != target:
        return {}
    return plan


def marker(plan: dict, purpose: str) -> str:
    return f"<!-- arete:narrowing:{purpose}={plan['key']} -->"


def ordinary(ticket: tracker.Ticket) -> bool:
    return ticket.open and not ticket.blocker and tracker.LABEL in ticket.labels and tracker.BATCH_LABEL not in ticket.labels


def pending(source, tid: str) -> bool:
    return isinstance(source, tracker.GitHubTracker) and bool(store.pending(source.repo.github.casefold(), str(tid)))


def completed(source, tid: str, run_id: str) -> bool:
    return (isinstance(source, tracker.GitHubTracker)
            and store.completed(source.repo.github.casefold(), str(tid), run_id))


def attempt(run) -> str:
    source, tid = run.tracker, str(run.tid)
    if run.dry_run or run.alone or not isinstance(source, tracker.GitHubTracker):
        return ""
    project = source.repo.github.casefold()
    if store.capped(project, tid):
        return CAP
    if pending(source, tid):
        return PENDING
    ticket = source.get(tid)
    if not ordinary(ticket) or runlimits.specification(ticket) != run.budget.specification:
        return ""
    plan = proposal(ticket, [run.id, *run.budget.parents])
    if not plan:
        return ""
    if plan["gap"] and not source.by_owner({"author": {"login": gh.me()}}):
        return "the authenticated GitHub account cannot create an owner-queued child"
    plan["dashboard_port"] = run.cfg.dashboard_port if run.cfg else paths.DASHBOARD_PORT
    if operation := store.reserve(run.id, project, tid, plan):
        outcome = reconcile(source, operation)
        run_lines.write(run.id, "narrowing", outcome=outcome)
        return outcome
    return CAP if store.capped(project, tid) else PENDING


def child_for(source: tracker.GitHubTracker, operation: dict, number: int | None) -> int | None:
    repo, tid, plan = source.repo.github, operation["issue"], operation["plan"]
    if number is None:
        found = gh.find_issue_marker(repo, marker(plan, "child"))
        if not found:
            return None
        match = re.fullmatch(re.escape(gh.issue_url(repo, "")) + r"([1-9][0-9]*)", found, re.I)
        if not match:
            raise ValueError("could not confirm the narrowing child")
        number = int(match[1])
    if number == int(tid):
        raise ValueError("could not confirm the narrowing child")
    child = source.get(str(number))
    if (marker(plan, "child") not in child.body.splitlines() or not child.open or tracker.LABEL not in child.labels
            or tracker.ZTE_LABEL in child.labels or tracker.BATCH_LABEL in child.labels
            or tracker.waits_on(child.body) != [int(tid)] or child.blocker.startswith("it was opened by")):
        raise ValueError("the narrowing child is outside the ordinary queue")
    return number


def original(source: tracker.GitHubTracker, operation: dict) -> bool:
    ticket = source.get(operation["issue"])
    plan = operation["plan"]
    return ordinary(ticket) and (ticket.title, ticket.body) == (plan["title"], plan["body"])


def reconcile(source: tracker.GitHubTracker, operation: dict) -> str:
    repo, tid, plan = source.repo.github, operation["issue"], operation["plan"]
    created = None
    if operation["phase"] == "ready":
        if not original(source, operation):
            store.advance(operation, "cancelled")
            return "the ticket changed before automatic narrowing"
        if gap := plan["gap"]:
            if not source.by_owner({"author": {"login": gh.me()}}):
                return PENDING
            operation = store.advance(operation, "child_sent")
            child_body = (f"Waits on #{tid}\n\n"
                          f"Add the missing prerequisite `{gap['prerequisite']}` and complete this deferred requirement "
                          f"from #{tid}.\n\n{gap['requirement']}\n\n{marker(plan, 'child')}")
            created = gh.issue_create(repo, f"Add {gap['prerequisite']} for #{tid}", child_body,
                                      labels=[tracker.LABEL], single_attempt=True)
        else:
            operation = store.advance(operation, "child_ready")
    if operation["phase"] == "child_sent":
        if not (child := child_for(source, operation, created)):
            return PENDING
        operation = store.advance(operation, "child_ready", child)
    if operation["phase"] == "child_ready":
        target = revised(plan, operation["child"])
        if gh.outgoing(target) != target or not original(source, operation):
            if operation["child"] is None:
                store.advance(operation, "cancelled")
            return "the ticket changed before the narrowing edit"
        operation = store.advance(operation, "edit_sent")
        gh.issue_edit_body(repo, int(tid), target, single_attempt=True)
    if operation["phase"] == "edit_sent":
        current = source.get(tid)
        if marker(plan, "parent") not in current.body.splitlines():
            return PENDING
        operation = store.advance(operation, "edited")
    if operation["phase"] == "edited":
        if not ordinary(source.get(tid)):
            return PENDING
        changes = []
        if child := operation["child"]:
            changes.append(f"- Deferred `{plan['gap']['prerequisite']}` and its requirement to #{child}.")
        if repeated := plan["recurrence"]:
            changes.extend(["- Updated acceptance criteria for the recurring failure.", acceptance_line(repeated)])
        message = f"{tracker.ARETE_TAG} Narrowed this ticket after the repair budget was exhausted.\n\n" + "\n".join(changes)
        operation = store.advance(operation, "comment_sent")
        gh.issue_comment(repo, int(tid), f"{message}\n\n{marker(plan, 'comment')}", single_attempt=True,
                         run_link=(operation["run_id"], plan["dashboard_port"]))
        store.advance(operation, "complete")
        return NARROWED
    if operation["phase"] == "comment_sent":
        if gh.find_issue_comment_marker(repo, int(tid), marker(plan, "comment")):
            store.advance(operation, "complete")
            return NARROWED
    return PENDING


def recover(source: tracker.GitHubTracker, operation: dict) -> None:
    row = run_store.run_get(operation["run_id"])
    repo = (row or {}).get("repo") or source.repo.name
    with util.lock(run_store.run_lock("pipeline", repo, operation["issue"])) as got:
        if got and (current := store.pending(operation["project"], operation["issue"])):
            if current[0]["run_id"] == operation["run_id"]:
                outcome = reconcile(source, current[0])
                if row and outcome == NARROWED:
                    run_lines.write(row["id"], "narrowing", outcome=outcome)
