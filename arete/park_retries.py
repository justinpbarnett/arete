from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from . import (
    config,
    gh,
    oncall,
    outbound,
    paths,
    pilot,
    retry_findings,
    run_lines,
    runlimits,
    scrub,
    tracker,
    util,
    workspace,
    zte,
)
from .review import BLOCKER_CATEGORIES, Review
from .store import orchestration
from .store import runs as run_store
from .store import tasks as tasks_store

MAX_REWRITES = 2
RUN_ID = re.compile(r"pip-\d{8}-\d{6}-[a-f0-9]{4}(?:-[^/\r\n]+)?")
CONTROLLER_ALLOWANCE = re.compile(
    r"(?:controller used its \d+ (?:decision|inference-launch)|run used its \d+ action) allowance")
BUDGET_PARKS = (
    (CONTROLLER_ALLOWANCE, "The controller reached its allowance. Focus the next attempt on the required behavior."),
    (re.compile(r"the repair chain used its \d+ repair cycles"), "The run used all of its repair cycles."),
    (re.compile(r"the run chain reached its cumulative \d+(?:\.\d+)?(?:e[+-]\d+)? minute budget"),
     "The run reached its time budget. Replan the work around the smallest steps that satisfy the ticket."),
    (re.compile(r"verification reached its cumulative \d+(?:\.\d+)?(?:e[+-]\d+)? minute budget"),
     "Verification reached its time budget. Plan focused checks for the requested behavior."),
)
REVIEW_STEP = re.compile(r"review\d*")
VERIFY_STEP = re.compile(r"verify(\d*)")
VERIFY_FILE = re.compile(r"verify(\d*)(?:(-outcome|-preflight|-probes)?\.json|(-server\.log))")
NO_CHECK_RESULT = {"verdict": "fail", "checks": []}
PLAIN_FINDING = re.compile(r"[A-Za-z][A-Za-z0-9 ,.!?'’()\-]{7,299}")
ParkTracker = tracker.GitHubTracker | tracker.LocalTracker


@dataclass(frozen=True)
class Finding:
    kind: str
    file: str
    line: int
    evidence: dict

    def pointer(self, run_id: str) -> str:
        return f"{self.kind} — run `{run_id}`, `{self.file}:{self.line}`."


def owned_file(path: Path) -> bool:
    return not path.parent.is_symlink() and not path.is_symlink() and path.is_file()


def artifact(run_id: str, name: str) -> dict:
    return runlimits.metadata(run_id, name) if owned_file(runlimits.artifact(run_id, name)) else {}


def numbered_log(run_id: str) -> list[tuple[int, dict]]:
    log = run_lines.path(run_id)
    if not owned_file(log):
        return []
    return [(n, line) for n, raw in enumerate(log.read_bytes().split(b"\n")[:-1], 1)
            for line in run_lines.whole(raw + b"\n") if line.get("run") == run_id]


def blockers(run_id: str, lines: list[tuple[int, dict]]) -> tuple[list[dict], str, int]:
    for number, line in reversed(lines):
        if line.get("event") == run_lines.END:
            report = line.get("review")
            if not isinstance(report, dict):
                continue
            if report.get("verdict") == "approve":
                return [], run_lines.FILE, number
            if report.get("verdict") == "request_changes":
                return [{**row, "problem": row.get("text")} for row in (report.get("problems") or [])
                        if isinstance(row, dict)], run_lines.FILE, number
        if line.get("event") != "step_end":
            continue
        step = line.get("call") or line.get("step")
        if not isinstance(step, str) or not REVIEW_STEP.fullmatch(step):
            continue
        data = line.get("data")
        if isinstance(data, dict) and isinstance(data.get("review"), dict):
            return Review.stored(data["review"]).blocking(), run_lines.FILE, number
        if line.get("ok") is True:
            file = f"{step}.json"
            saved = artifact(run_id, file)
            if saved.get("ok") is True and isinstance(saved.get("structured"), dict):
                return Review.stored({"earlier": [], **saved["structured"]}).blocking(), file, 1
    saved = artifact(run_id, "review.json")
    if saved.get("ok") is True and isinstance(saved.get("structured"), dict):
        return Review.stored({"earlier": [], **saved["structured"]}).blocking(), "review.json", 1
    return [], run_lines.FILE, 1


def verify_event(run_id: str, step: str, line: dict, number: int) -> tuple[dict, str, int]:
    data = line.get("data")
    saved = data.get("verified") if isinstance(data, dict) else None
    if isinstance(saved, dict):
        return saved, run_lines.FILE, number
    detail = {key: line[key] for key in ("error", "summary", "blocked_by", "feedback") if line.get(key)}
    if "result" not in line or line.get("call"):
        detail = with_worker_error(run_id, step, detail)
    return {**NO_CHECK_RESULT, **detail}, run_lines.FILE, number


def with_worker_error(run_id: str, step: str, detail: dict) -> dict:
    if reported := detail.get("error") or detail.get("blocked_by"):
        worker = artifact(run_id, f"{step}.json")
        full_error = worker.get("error")
        if worker.get("ok") is False and matches_worker_error(full_error, reported):
            return {**detail, "error": full_error}
    return detail


def matches_worker_error(full_error, reported) -> bool:
    return (isinstance(full_error, str) and isinstance(reported, str) and bool(reported)
            and (full_error.startswith(reported) or util.first_line(full_error, 300) == reported))


def verify_name(attempt: int) -> str:
    return f"verify{attempt}" if attempt > 1 else "verify"


def verification(run_id: str, lines: list[tuple[int, dict]]) -> tuple[dict, str, int]:
    minimum, outer_attempt, outer, outer_result = 0, None, None, None
    for number, line in reversed(lines):
        event = line.get("event")
        if event not in ("verification_start", "verification_end", "step_start", "step_end") or line.get("carried"):
            continue
        if not (match := VERIFY_STEP.fullmatch(str(line.get("call") or line.get("step") or ""))):
            continue
        is_outer = "result" in line and not line.get("call")
        attempt = int(match[1] or "1")
        if event == "step_end" and not (match[1] or line.get("call") or is_outer) and type(line.get("try")) is int:
            attempt = max(attempt, line["try"])
        if attempt < minimum or (outer_attempt and attempt != outer_attempt):
            break
        if event == "verification_end":
            verdict = line.get("verdict")
            found, file, at = verify_event(run_id, match[0], line, number)
            attempt_file = f"{match[0]}-attempt.json"
            if attempt_result := artifact(run_id, attempt_file):
                found, file, at = with_worker_error(run_id, match[0], attempt_result), attempt_file, 1
            elif not isinstance(line.get("data"), dict) and not line.get("startup_failed"):
                if saved := verification_files(run_id, attempt, only_attempt=True, server_fallback=False):
                    found, file, at = saved
                    found = with_worker_error(run_id, match[0], found)
            return {**found, "verdict": verdict, "startup_failed": line.get("startup_failed") is True}, file, at
        if event == "verification_start":
            return verification_files(run_id, attempt, only_attempt=True) or verify_event(run_id, match[0], line, number)
        if event == "step_start":
            if line.get("call"):
                minimum = max(minimum, attempt)
            elif outer:
                break
            continue
        data = line.get("data")
        found = verify_event(run_id, verify_name(attempt), line, number)
        has_result = isinstance(data, dict) and isinstance(data.get("verified"), dict)
        if is_outer:
            if outer:
                break
            if has_result:
                if isinstance(names := data.get("names"), dict):
                    named = names.get("verify")
                    if type(named) is int and named > 0:
                        outer_attempt = named
                if not outer_attempt and found[0].get("verdict") != "blocked":
                    return found
                outer_result = found
            elif line["result"] not in ("fail", "stop"):
                return {}, run_lines.FILE, number
            outer = found
            if type(line.get("try")) is int and not outer_attempt:
                minimum = max(attempt, line["try"])
            continue
        if has_result or line.get("ok") is False:
            return found
        minimum = max(attempt, minimum)
        break
    if outer_result and outer_attempt:
        step = verify_name(outer_attempt)
        full = with_worker_error(run_id, step, outer_result[0])
        return (full, f"{step}.json", 1) if full is not outer_result[0] else outer_result
    saved = verification_files(run_id, minimum)
    if outer_result:
        detail = outer_result[0]
        if saved and VERIFY_STEP.fullmatch(saved[1].removesuffix(".json")) and matches_worker_error(
                saved[0].get("error"), detail.get("error") or detail.get("blocked_by")):
            return {**detail, "error": saved[0]["error"]}, saved[1], saved[2]
        return outer_result
    return saved or outer or ({}, run_lines.FILE, 1)


def verification_files(run_id: str, minimum: int, *, only_attempt: bool = False,
                       server_fallback: bool = True) -> tuple[dict, str, int] | None:
    files = []
    folder = runlimits.artifact(run_id, "parked.json").parent
    for path in folder.iterdir() if folder.is_dir() and not folder.is_symlink() else ():
        if match := VERIFY_FILE.fullmatch(path.name):
            number, kind, server_log = match.groups()
            if server_log and not server_fallback:
                continue
            number = int(number or "1")
            if number >= minimum and (not only_attempt or number == minimum) and owned_file(path):
                priority = -1 if server_log else {"-outcome": 3, "-preflight": 2, "-probes": 1, None: 0}[kind]
                files.append((number, priority, path.name))
    latest = max([minimum, *(number for number, _, _ in files)])
    for number, priority, name in sorted(files, reverse=True):
        if number != latest:
            break
        if priority == -1:
            return {**NO_CHECK_RESULT, "error": (folder / name).read_text(errors="replace")}, name, 1
        saved = artifact(run_id, name)
        if priority == 0:
            if saved.get("ok") is False and isinstance(saved.get("error"), str) and saved["error"]:
                return {**NO_CHECK_RESULT, "error": saved["error"]}, name, 1
            saved = saved.get("structured") if saved.get("ok") is True else None
        if isinstance(saved, dict) and saved.get("verdict") in ("pass", "fail", "blocked", "unverified"):
            if priority not in (1, 2) or saved["verdict"] != "pass":
                if priority == 3:
                    step = name.removesuffix("-outcome.json")
                    saved = complete_checks(run_id, step, saved)
                return saved, name, 1
    return None


def complete_checks(run_id: str, step: str, outcome: dict) -> dict:
    checks = outcome.get("checks") or []
    if not any(isinstance(check, dict) and ("how" not in check or "saw" not in check) for check in checks):
        return outcome
    worker = artifact(run_id, f"{step}.json")
    reported = worker.get("structured") if worker.get("ok") is True else None
    probes = artifact(run_id, f"{step}-probes.json")
    preflight = artifact(run_id, f"{step}-preflight.json")
    details = [check for report in (preflight, probes, reported) if isinstance(report, dict)
               for check in report.get("checks") or [] if isinstance(check, dict)]
    combined = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        for i, detail in enumerate(details):
            if (detail.get("what"), detail.get("passed")) == (check.get("what"), check.get("passed")):
                check = {**details.pop(i), **check}
                break
        combined.append(check)
    return {**outcome, "checks": combined}


def screened(text: str, secrets: tuple[str, ...]) -> bool:
    return (scrub.replaced(text, secrets) == text and outbound.redact(text) == text
            and outbound.inert(outbound.screen(text)) == text)


def criterion_detail(finding: dict, criteria: dict[str, str], *, verification: bool = False) -> str:
    ref = retry_findings.reference(finding.get("criterion_ref"))
    if not ref and verification:
        check_name = retry_findings.reference(finding.get("what"))
        if check_name.startswith("journey:"):
            ref = check_name
    return criteria.get(ref, "")


def record_lines(run_id: str, file: str, line: int, rows: list[dict], field: str) -> list[int]:
    if not rows or file == run_lines.FILE or not file.endswith(".json"):
        return [line] * len(rows)
    path = runlimits.artifact(run_id, file)
    if not owned_file(path):
        return [line] * len(rows)
    locations = defaultdict(deque)
    prefix = f'"{field}": '
    for number, text in enumerate(path.read_text().splitlines(), 1):
        text = text.strip()
        if text.startswith(prefix):
            locations[text.removeprefix(prefix).removesuffix(",")].append(number)
    numbers = [locations[json.dumps(row.get(field), default=str)] for row in rows]
    return [matches.popleft() if matches else line for matches in numbers]


def verification_failure_class(verdict: str, failed: int, startup_failed: bool) -> str:
    if startup_failed:
        return "Verification startup failure"
    if verdict == "blocked":
        return "Verification blocked"
    return "Verification failure" if verdict == "fail" and not failed else ""


def verification_classes(verdict: str, failed: int, startup_failed: bool) -> list[str]:
    classes = ["Failed verification check"] * failed
    if kind := verification_failure_class(verdict, failed, startup_failed):
        classes.append(kind)
    return classes


def evidence(run_id: str) -> list[Finding]:
    lines = numbered_log(run_id)
    listed = []
    review, file, number = blockers(run_id, lines)
    for blocker, at in zip(review, record_lines(run_id, file, number, review, "problem")):
        category = blocker.get("category")
        kind = f"Review blocker ({category})" if category in BLOCKER_CATEGORIES else "Review blocker"
        listed.append(Finding(kind, file, at, blocker))
    checked, file, number = verification(run_id, lines)
    checks = [row for row in checked.get("checks") or [] if isinstance(row, dict)]
    failed = [Finding("Failed verification check", file, at, check)
              for check, at in zip(checks, record_lines(run_id, file, number, checks, "what"))
              if check.get("passed") is False]
    listed.extend(failed)
    if kind := verification_failure_class(checked.get("verdict"), len(failed), checked.get("startup_failed")):
        listed.append(Finding(kind, file, number, {k: checked[k] for k in ("error", "summary", "blocked_by", "feedback")
                                                   if checked.get(k)}))
    return listed


def findings(run_id: str, criteria: dict[str, str]) -> list[str]:
    listed = []
    for finding in evidence(run_id):
        detail = criterion_detail(finding.evidence, criteria, verification=finding.kind == "Failed verification check")
        listed.append(f"- {detail or finding.pointer(run_id)}")
    return listed


def ticket_evidence(repo: str, ticket: tracker.Ticket, run_id: str) -> list[Finding]:
    row = run_store.run_get(run_id) if RUN_ID.fullmatch(run_id) else None
    if (not row or row["kind"] != "pipeline" or row.get("only_step")
            or (row["repo"], str(row["issue"])) != (repo, ticket.id) or not artifact(run_id, "parked.json")):
        return []
    return evidence(run_id)


def task_evidence(run_id: str, findings: list[Finding]) -> str:
    entries = [f"### {finding.pointer(run_id)}\n\n"
               + "\n".join(f"{name}: {value}" for name, value in finding.evidence.items() if value not in (None, ""))
               for finding in findings]
    return f"## Local evidence from parked run {run_id}\n\n" + "\n\n".join(entries) if entries else ""


def tracker_criteria(repo: config.Repo, ticket: tracker.Ticket, secrets: tuple[str, ...]) -> dict[str, str]:
    verify = config.load_layer(repo.layer_dir).verify
    criteria = retry_findings.criteria(ticket.body, verify.feature_map if verify else {}, ticket.title)
    return {key: text if text.endswith((".", "!", "?")) else f"{text}." for key, text in criteria.items()
            if PLAIN_FINDING.fullmatch(text) and screened(text, secrets)}


def budget_summary(saved: dict) -> str:
    reason = saved.get("reason")
    if isinstance(reason, str):
        return next((summary for pattern, summary in BUDGET_PARKS if pattern.fullmatch(reason)), "")
    return ""


def retryable(repo: config.Repo, issue: str, run: dict, saved: dict) -> bool:
    failure = json.loads(run.get("failure") or "{}")
    if (saved.get("kept") != {}
            or not isinstance(saved.get("specification"), str) or not saved["specification"]
            or not isinstance(failure, dict) or failure.get("class") == "environment"
            or orchestration.publication_pending(run["id"])):
        return False
    if not saved.get("workspace"):
        return True
    ws = paths.WORKTREES_DIR / f"{repo.name}-{issue}"
    if saved["workspace"] != str(ws) or ws.is_symlink():
        return False
    checkouts = [workspace.Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir, "")
                 for p in workspace.parts_of(repo)]
    return not workspace.scratch_left(ws, checkouts, strict=True) and not workspace.kept_work(
        ws, checkouts, repo.path, config.load_layer(repo.layer_dir).copy)


def restart_parked_revision(cfg, repo, layer, closed_context, row, park, ticket):
    from . import pipeline
    from .orchestration import lease_continuation_blocker, policy_fingerprint

    if row["status"] != "failed" or not row.get("pr_url"):
        return False
    prior = orchestration.find(row["id"])
    if not prior or prior["current_run"] != row["id"]:
        return False
    data = prior["data"]
    saved_policy = data.get("policy_sha")
    shared_policy_changed = (data.get("prepared", {}).get("layer") == str(layer.dir) and saved_policy
                             and saved_policy != policy_fingerprint(cfg, layer, closed_context))
    if not (shared_policy_changed or CONTROLLER_ALLOWANCE.fullmatch(str(park.get("reason") or ""))):
        return False
    lease = orchestration.Lease(prior["root"], prior["current_run"], prior["generation"])
    return (retryable(repo, ticket.id, row, park) and not lease_continuation_blocker(lease)
            and bool(pipeline.pending_feedback(repo.name, ticket.id)))


def eligible(source: ParkTracker, ticket: tracker.Ticket, issue: str, saved: dict) -> bool:
    if (ticket.id != issue or runlimits.retry_hold(ticket)
            or tracker.BATCH_LABEL in ticket.labels
            or runlimits.specification(ticket) != saved["specification"]):
        return False
    if isinstance(source, tracker.LocalTracker):
        row = tasks_store.task_get(ticket.id)
        return bool(row and row["version"] == ticket.version and row["state"] in ("todo", "failed"))
    return True


def append(source: ParkTracker, ticket: tracker.Ticket, body: str) -> bool:
    if isinstance(source, tracker.LocalTracker):
        return tasks_store.task_append_comment(ticket.id, body, base_version=ticket.version, title=ticket.title,
                                               description=ticket.body)
    gh.issue_comment(source.repo.github, int(ticket.id), body, single_attempt=True)
    return True


def journal_for(source: ParkTracker, issue: str) -> Path:
    key = runlimits.readmission_key([source.repo.name, source.name, source.repo.github, issue])
    return paths.STATE_DIR / "park-retries" / f"{key}.json"


def reconcile(journal: Path, state: dict, ticket: tracker.Ticket) -> None:
    rewrites = state["rewrites"]
    pending = [rid for rid, status in rewrites.items() if status == "pending"]
    if not pending:
        return
    posted = {comment.run_id for comment in ticket.retries}
    headings = set(ticket.body.splitlines())
    for rid in pending:
        if rid in posted or f"## From parked run {rid}" in headings:
            rewrites[rid] = "done"
        else:
            del rewrites[rid]
    util.write_json(journal, state)


def settle_retry(source: ParkTracker, issue: str, run: dict, saved: dict, summary: str,
                 ticket: tracker.Ticket, journal: Path, state: dict, secrets: tuple[str, ...]) -> str | None:
    rid = run["id"]
    rewrites = state["rewrites"]
    posted = {comment.run_id for comment in ticket.retries}
    if rid in posted:
        if rewrites.get(rid) != "done":
            rewrites[rid] = "done"
            util.write_json(journal, state)
        return
    if f"## From parked run {rid}" in ticket.body.splitlines() or not eligible(source, ticket, issue, saved):
        return
    if not runlimits.newer(util.iso(), runlimits.parked_at(run, saved)):
        return
    count = max(len(posted | {prior for prior, status in rewrites.items() if status == "done"}),
                max((comment.number for comment in ticket.retries), default=0))
    if count >= MAX_REWRITES:
        return
    listed = "\n".join(findings(rid, tracker_criteria(source.repo, ticket, secrets)) or [f"- {summary}"])
    marker = f"<!-- arete:retry run={rid} n={count + 1} -->"
    body = (f"{marker}\n\n## From parked run {rid}\n\n"
            "Keep the ticket's requirements and address these findings in the next attempt. "
            "Use focused verification for the affected behavior.\n\n"
            f"{listed}")
    if not screened(body, secrets):
        return "Outbound screening held the parked retry comment."
    rewrites[rid] = "pending"
    util.write_json(journal, state)
    current = source.get(issue)
    if current.id == issue and any(comment.run_id == rid for comment in current.retries):
        rewrites[rid] = "done"
    elif eligible(source, current, issue, saved) and append(source, current, body):
        rewrites[rid] = "done"
    else:
        del rewrites[rid]
    util.write_json(journal, state)


def retry_ticket(source: ParkTracker, issue: str, secrets: tuple[str, ...]) -> str | None:
    from . import tasks

    repo = source.repo
    if pilot.is_pilot_task(issue) or zte.reserved(issue):
        return
    with util.lock(run_store.run_lock("pipeline", repo.name, issue)) as free:
        if not free:
            return
        journal = journal_for(source, issue)
        try:
            state = json.loads(journal.read_text())
        except FileNotFoundError:
            state = {"rewrites": {}}
        ticket = None
        if "pending" in state["rewrites"].values():
            try:
                ticket = source.get(issue)
            except tracker.TaskError:
                if not isinstance(source, tracker.LocalTracker) or tasks_store.task_get(issue):
                    raise
            except gh.GhError as error:
                if gh.NO_ISSUE not in str(error):
                    raise
            if ticket is None or ticket.id != issue:
                state["rewrites"] = {rid: status for rid, status in state["rewrites"].items() if status != "pending"}
                util.write_json(journal, state)
                return
            reconcile(journal, state, ticket)
        if (tasks.pending_start(repo.name, issue) or run_store.issue_running(repo.name, issue)
                or run_store.pending_label(repo.name, issue)):
            return
        run = run_store.newest_live_whole_run(repo.name, issue) or {}
        if run.get("status") != "failed" or not RUN_ID.fullmatch(run["id"]):
            return
        parked, _ = runlimits.latest_park(repo.name, issue)
        if parked.get("id") != run["id"]:
            return
        if state["rewrites"].get(run["id"]) == "done":
            return
        saved = artifact(run["id"], "parked.json")
        summary = budget_summary(saved)
        if summary and retryable(repo, issue, run, saved):
            return settle_retry(source, issue, run, saved, summary, ticket or source.get(issue), journal, state, secrets)


def pending_tickets() -> dict[str, set[str]]:
    pending: dict[str, set[str]] = {}
    for journal in (paths.STATE_DIR / "park-retries").glob("*.json"):
        try:
            state = json.loads(journal.read_text()) if owned_file(journal) else {}
            for rid, status in state.get("rewrites", {}).items():
                if status == "pending" and RUN_ID.fullmatch(rid) and (run := run_store.run_get(rid)):
                    pending.setdefault(run["repo"], set()).add(str(run["issue"]))
        except (OSError, ValueError, AttributeError):
            continue
    return pending


def refresh(cfg: config.Config) -> tuple[dict[str, str], list[str]]:
    held = {}
    errors = []
    secrets = None
    pending = pending_tickets()
    for repo in cfg.repos:
        if not repo.pipeline:
            continue
        on_call = oncall.parked(repo.name)
        issues = set(run_store.latest_failed_tickets(repo.name)) | pending.get(repo.name, set())
        for issue in sorted(issues):
            if issue in on_call or (repo.tracker == "jira" and not tracker.is_task_id(issue)):
                continue
            ref = tracker.issue_name(repo.name, issue)
            try:
                source = tracker.for_ticket(cfg, repo, issue)
                if isinstance(source, tracker.JiraTracker):
                    continue
                if secrets is None:
                    secrets = scrub.secrets()
                if reason := retry_ticket(source, issue, secrets):
                    held[ref] = reason
            except (*tracker.ERRORS, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                errors.append(f"{ref} parked retry could not settle ({type(error).__name__})")
    return held, errors
