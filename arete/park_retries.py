from __future__ import annotations

import json
import re
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
RUN_ID = re.compile(tracker.PARK_RUN_ID)
BUDGET_PARKS = (
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


def owned_file(path: Path) -> bool:
    return not path.parent.is_symlink() and not path.is_symlink() and path.is_file()


def artifact(run_id: str, name: str) -> dict:
    return runlimits.metadata(run_id, name) if owned_file(runlimits.artifact(run_id, name)) else {}


def blockers(run_id: str, lines: list[dict]) -> list[dict]:
    for line in reversed(lines):
        if line.get("event") == run_lines.END:
            report = line.get("review")
            if not isinstance(report, dict):
                continue
            if report.get("verdict") == "approve":
                return []
            if report.get("verdict") == "request_changes":
                return [{**row, "problem": row.get("text")} for row in (report.get("problems") or [])
                        if isinstance(row, dict)]
        if line.get("event") != "step_end":
            continue
        step = line.get("call") or line.get("step")
        if not isinstance(step, str) or not REVIEW_STEP.fullmatch(step):
            continue
        data = line.get("data")
        if isinstance(data, dict) and isinstance(data.get("review"), dict):
            return Review.stored(data["review"]).blocking()
        if line.get("ok") is True:
            saved = artifact(run_id, f"{step}.json")
            if saved.get("ok") is True and isinstance(saved.get("structured"), dict):
                return Review.stored({"earlier": [], **saved["structured"]}).blocking()
    return []


def verification(run_id: str, lines: list[dict]) -> dict:
    minimum, outer, outer_verdict = 0, False, {}
    for line in reversed(lines):
        event = line.get("event")
        if event not in ("verification_start", "verification_end", "step_start", "step_end") or line.get("carried"):
            continue
        if not (match := VERIFY_STEP.fullmatch(str(line.get("call") or line.get("step") or ""))):
            continue
        attempt = int(match[1] or "1")
        if event == "verification_end":
            verdict, startup_failed = line.get("verdict"), line.get("startup_failed") is True
            saved = verification_files(run_id, attempt, only_attempt=True) if verdict == "fail" and not startup_failed else {}
            return {"verdict": verdict, "checks": saved.get("checks", []), "startup_failed": startup_failed}
        if event == "verification_start":
            return verification_files(run_id, attempt, only_attempt=True) or NO_CHECK_RESULT
        if event == "step_start":
            if outer and not line.get("call"):
                break
            continue
        data = line.get("data")
        verdict = data.get("verified") if isinstance(data, dict) else None
        if "result" in line and not line.get("call"):
            if outer:
                break
            if isinstance(verdict, dict):
                outer_verdict = verdict
            elif line["result"] not in ("fail", "stop"):
                return {}
            outer = True
            if line["result"] == "fail" and isinstance(line.get("try"), int):
                minimum = line["try"]
            continue
        if isinstance(verdict, dict):
            return verdict
        if line.get("ok") is False:
            return outer_verdict or verification_files(run_id, max(minimum, attempt + 1)) or NO_CHECK_RESULT
        minimum = max(attempt, minimum)
        break
    return outer_verdict or verification_files(run_id, minimum) or (NO_CHECK_RESULT if outer else {})


def verification_files(run_id: str, minimum: int, *, only_attempt: bool = False) -> dict:
    files = []
    for path in runlimits.artifact(run_id, "parked.json").parent.iterdir():
        if match := VERIFY_FILE.fullmatch(path.name):
            number, kind, server_log = match.groups()
            number = int(number or "1")
            if number >= minimum and (not only_attempt or number == minimum) and owned_file(path):
                priority = -1 if server_log else {"-outcome": 3, "-preflight": 2, "-probes": 1, None: 0}[kind]
                files.append((number, priority, path.name))
    latest = max([minimum, *(number for number, _, _ in files)])
    for number, priority, name in sorted(files, reverse=True):
        if number != latest:
            break
        if priority == -1:
            return NO_CHECK_RESULT
        saved = artifact(run_id, name)
        if priority == 0:
            saved = saved.get("structured") if saved.get("ok") is True else None
        if isinstance(saved, dict) and saved.get("verdict") in ("pass", "fail", "blocked", "unverified"):
            if priority not in (1, 2) or saved["verdict"] != "pass":
                return saved
    return {}


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


def findings(run_id: str, criteria: dict[str, str]) -> list[str]:
    log = run_lines.path(run_id)
    lines = [line for line in run_lines.read(run_id) if line.get("run") == run_id] if owned_file(log) else []
    listed = []
    for index, blocker in enumerate(blockers(run_id, lines), 1):
        category = blocker.get("category")
        kind = f" ({category})" if category in BLOCKER_CATEGORIES else ""
        detail = (criterion_detail(blocker, criteria) or retry_findings.action(blocker, "problem")
                  or "See this blocker in the run’s review.")
        listed.append(f"- Review blocker {index}{kind}. {detail}")
    checked = verification(run_id, lines)
    if checked.get("startup_failed"):
        listed.append("- The app did not start during the newest verification attempt.")
    elif checked.get("verdict") == "fail":
        checks = checked.get("checks") or []
        failed = [row for row in checks if isinstance(row, dict) and row.get("passed") is False
                  and str(row.get("cause") or "").strip().lower() != "environment"]
        if not checks:
            listed.append("- Verification failed before recording a check result.")
        for index, check in enumerate(failed, 1):
            detail = (criterion_detail(check, criteria, verification=True) or retry_findings.action(check, "what")
                      or "See this check in the run’s verification.")
            listed.append(f"- Failed verification check {index}. {detail}")
    elif checked.get("verdict") == "blocked":
        listed.append("- The newest verification attempt was blocked before it could confirm the change.")
    return listed


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


def eligible(source: ParkTracker, ticket: tracker.Ticket, issue: str, saved: dict) -> bool:
    if (ticket.id != issue or not ticket.open or ticket.blocker
            or runlimits.specification(ticket) != saved["specification"]):
        return False
    if isinstance(source, tracker.LocalTracker):
        row = tasks_store.task_get(ticket.id)
        return bool(row and row["version"] == ticket.version and row["state"] in ("todo", "failed"))
    labels = {source.label_name(state) for state in config.RUN_LABELS if state != "failed"}
    return not labels.intersection(ticket.labels)


def append(source: ParkTracker, ticket: tracker.Ticket, body: str, *, needs_info: bool = False) -> bool:
    if isinstance(source, tracker.LocalTracker):
        return tasks_store.task_append_comment(ticket.id, body, base_version=ticket.version, title=ticket.title,
                                               description=ticket.body, needs_info=needs_info)
    gh.issue_comment(source.repo.github, int(ticket.id), body, single_attempt=True)
    return True


def asked(ticket: tracker.Ticket, run_id: str) -> bool:
    return bool(ticket.park_question and ticket.park_question.run_id == run_id)


def ask_owner(source: ParkTracker, issue: str, run_id: str, saved: dict, question: str) -> bool:
    ticket = source.get(issue)
    if not eligible(source, ticket, issue, saved):
        return False
    if isinstance(source, tracker.LocalTracker):
        if asked(ticket, run_id):
            return tasks_store.task_set_state(issue, "needs-info", ("todo", "failed"), base_version=ticket.version)
        return append(source, ticket, question, needs_info=True)
    if not asked(ticket, run_id):
        append(source, ticket, question)
        ticket = source.get(issue)
        if not eligible(source, ticket, issue, saved):
            return False
    gh.issue_labels(source.repo.github, int(issue), add=[source.label_name("needs-info")],
                    remove=[source.label_name("failed")], single_attempt=True)
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
    listed = "\n".join(findings(rid, tracker_criteria(source.repo, ticket, secrets)) or [f"- {summary}"])
    if count >= MAX_REWRITES:
        reply = (f'Reply with `arete task reply {issue} "your reply"`.'
                 if isinstance(source, tracker.LocalTracker) else "Reply in a comment on this GitHub issue.")
        question = (f"{tracker.ARETE_TAG} Parked retry question for run {rid}.\n\n{listed}\n\n"
                    f"What behavior should the next attempt implement? {reply}")
        if not screened(question, secrets):
            return "Outbound screening held the parked retry question."
        if ask_owner(source, issue, rid, saved, question):
            state["escalated"] = rid
            util.write_json(journal, state)
        return
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
            state = {"rewrites": {}, "escalated": False}
        ticket = None
        if "pending" in state["rewrites"].values():
            ticket = source.get(issue)
            if ticket.id != issue:
                return
            reconcile(journal, state, ticket)
        if (tasks.pending_start(repo.name, issue) or run_store.issue_running(repo.name, issue)
                or run_store.pending_label(repo.name, issue)):
            return
        run = run_store.newest_live_whole_run(repo.name, issue) or {}
        if run.get("status") != "failed" or not RUN_ID.fullmatch(run["id"]):
            return
        if state["rewrites"].get(run["id"]) == "done" or state["escalated"] == run["id"]:
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
