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
RUN_ID = re.compile(r"pip-\d{8}-\d{6}-[a-f0-9]{4}(?:-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100})?")
BUDGET_PARKS = (
    (re.compile(r"the repair chain used its \d+ repair cycles"), "The run used all of its repair cycles."),
    (re.compile(r"the run chain reached its cumulative \d+(?:\.\d+)?(?:e[+-]\d+)? minute budget"),
     "The run reached its time budget. Replan the work around the smallest steps that satisfy the ticket."),
    (re.compile(r"verification reached its cumulative \d+(?:\.\d+)?(?:e[+-]\d+)? minute budget"),
     "Verification reached its time budget. Plan focused checks for the requested behavior."),
)
REVIEW_STEP = re.compile(r"review\d*")
VERIFY_FILE = re.compile(r"verify(\d*)(-outcome|-preflight|-probes)?\.json")


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
                return [{**row, "problem": row.get("text")} for row in report.get("problems", [])
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
    files = []
    for path in runlimits.artifact(run_id, "parked.json").parent.iterdir():
        if match := VERIFY_FILE.fullmatch(path.name):
            number, kind = match.groups()
            priority = {"-outcome": 3, "-preflight": 2, "-probes": 1, None: 0}[kind]
            files.append((int(number or "1"), priority, path.name))
    for _, priority, name in sorted(files, reverse=True):
        saved = artifact(run_id, name)
        if priority == 0:
            saved = saved.get("structured") if saved.get("ok") is True else None
        if isinstance(saved, dict) and saved.get("verdict") in ("pass", "fail", "blocked", "unverified"):
            if priority not in (1, 2) or saved["verdict"] != "pass":
                return saved
    for line in reversed(lines):
        data = line.get("data")
        if (line.get("event") == "step_end" and isinstance(data, dict)
                and isinstance(data.get("verified"), dict)):
            return data["verified"]
    return {}


def screened(text: str, secrets: tuple[str, ...]) -> bool:
    return (scrub.replaced(text, secrets) == text and outbound.redact(text) == text
            and outbound.inert(outbound.screen(text)) == text)


def findings(run_id: str) -> list[str]:
    log = run_lines.path(run_id)
    lines = [line for line in run_lines.read(run_id) if line.get("run") == run_id] if owned_file(log) else []
    listed = []
    for index, blocker in enumerate(blockers(run_id, lines), 1):
        category = blocker.get("category")
        kind = f" ({category})" if category in BLOCKER_CATEGORIES else ""
        detail = retry_findings.action(blocker, "problem") or "See this blocker in the run’s review."
        listed.append(f"- Review blocker {index}{kind}. {detail}")
    checked = verification(run_id, lines)
    if checked.get("verdict") == "fail":
        failed = [row for row in checked.get("checks", []) if isinstance(row, dict) and row.get("passed") is False
                  and str(row.get("cause") or "").strip().lower() != "environment"]
        if checked.get("checks") == []:
            listed.append("- Verification failed before recording a check result.")
        for index, check in enumerate(failed, 1):
            detail = retry_findings.action(check, "what") or "See this check in the run’s verification."
            listed.append(f"- Failed verification check {index}. {detail}")
    return listed


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


def eligible(source: tracker.Tracker, ticket: tracker.Ticket, other_routes: set[str]) -> bool:
    if not ticket.open or ticket.blocker or other_routes.intersection(ticket.labels):
        return False
    labels = {source.label_name(state) for state in source.states if state != "failed"}
    if isinstance(source, tracker.LocalTracker):
        row = tasks_store.task_get(ticket.id)
        return bool(row and row["state"] in ("todo", "failed"))
    return not labels.intersection(ticket.labels) and tracker.BATCH_LABEL not in ticket.labels


def append(source: tracker.LocalTracker, current: tracker.Ticket, section: str) -> bool:
    row = tasks_store.task_get(current.id)
    if not row or row["repo"] != source.repo.name:
        return False
    snapshot = tracker.task_ticket(row)
    if (snapshot.title, snapshot.body) != (current.title, current.body):
        return False
    return tasks_store.task_append_body(current.id, row["body"] or "", section, base_version=row["version"])


def ask_owner(source: tracker.Tracker, issue: str, run_id: str, question: str) -> None:
    marker = f"Parked retry question for run {run_id}."
    if isinstance(source, tracker.GitHubTracker):
        marker = f"<!-- arete:parked-question={run_id} -->"
        repo = source.repo.github
        source.require_ordinary(issue)
        if not gh.find_issue_comment_marker(repo, int(issue), marker):
            gh.issue_comment(repo, int(issue), f"{question}\n\n{marker}", single_attempt=True)
    elif isinstance(source, tracker.JiraTracker):
        if not source.client.find_comment_marker(issue, f"{tracker.ARETE_TAG} {marker}"):
            source.client.comment(issue, question, single_attempt=True)
    elif not any(f"{tracker.ARETE_TAG} {marker}" in row["body"].splitlines()
                 for row in tasks_store.task_comments(issue) if row["author"] == "arete"):
        if source.comment(issue, question):
            raise tracker.TaskError("could not post the parked retry question")
    if source.label(issue, add=["needs-info"], remove=["failed"]):
        raise tracker.TaskError("could not label the parked ticket needs-info")


def retry_ticket(source: tracker.Tracker, issue: str, other_routes: set[str], secrets: tuple[str, ...]) -> str | None:
    from . import tasks

    repo = source.repo
    if pilot.is_pilot_task(issue) or zte.reserved(issue):
        return
    with util.lock(run_store.run_lock("pipeline", repo.name, issue)) as free:
        if (not free or tasks.pending_start(repo.name, issue) or run_store.issue_running(repo.name, issue)
                or run_store.pending_label(repo.name, issue)):
            return
        run = next(iter(run_store.ticket_runs(repo.name, issue, limit=1)), {})
        if (run.get("status") != "failed" or run.get("dry_run") or run.get("only_step")
                or not RUN_ID.fullmatch(run["id"])):
            return
        rid = run["id"]
        saved = artifact(rid, "parked.json")
        summary = budget_summary(saved)
        if not summary or not retryable(repo, issue, run, saved):
            return
        key = runlimits.readmission_key([repo.name, source.name, repo.github, issue])
        journal = paths.STATE_DIR / "park-retries" / f"{key}.json"
        try:
            state = json.loads(journal.read_text())
        except FileNotFoundError:
            state = {"rewrites": {}, "escalated": False}
        rewrites = state["rewrites"]
        if state["escalated"] or rewrites.get(rid) == "done":
            return
        ticket = source.get(issue)
        if ticket.id != issue:
            return
        lines = set(ticket.body.splitlines())
        settled = [prior for prior, status in rewrites.items()
                   if status == "pending" and f"## From parked run {prior}" in lines]
        if settled:
            rewrites.update(dict.fromkeys(settled, "done"))
            util.write_json(journal, state)
        heading = f"## From parked run {rid}"
        if (not eligible(source, ticket, other_routes) or heading in lines
                or runlimits.specification(ticket) != saved["specification"]):
            return
        if "pending" in rewrites.values():
            return "Could not confirm an earlier parked retry append."
        if list(rewrites.values()).count("done") >= MAX_REWRITES:
            resume = ("return the task to todo" if isinstance(source, tracker.LocalTracker)
                      else f"remove the `{source.label_name('needs-info')}` label")
            listed = "\n".join(findings(rid) or [f"- {summary}"])
            question = (f"{tracker.ARETE_TAG} Parked retry question for run {rid}.\n\n{listed}\n\n"
                        "What behavior should the next attempt implement? "
                        f"Please update the ticket specification with the expected result and {resume}.")
            if not screened(question, secrets):
                return "Outbound screening held the parked retry question."
            ask_owner(source, issue, rid, question)
            state["escalated"] = True
            util.write_json(journal, state)
            return
        if not isinstance(source, tracker.LocalTracker):
            backend = "GitHub" if isinstance(source, tracker.GitHubTracker) else "Jira"
            return f"{backend} has no supported atomic description append or conditional edit. The ticket remains parked."
        listed = "\n".join(findings(rid) or [f"- {summary}"])
        section = f"\n\n{heading}\n\n{listed}\n"
        if not screened(section, secrets):
            return "Outbound screening held the parked retry section."
        rewrites[rid] = "pending"
        util.write_json(journal, state)
        if not append(source, ticket, section):
            del rewrites[rid]
            util.write_json(journal, state)
            return "The ticket changed before the parked retry section could be appended."
        rewrites[rid] = "done"
        util.write_json(journal, state)


def refresh(cfg: config.Config) -> tuple[dict[str, str], list[str]]:
    held = {}
    errors = []
    secrets = None
    for repo in cfg.repos:
        if not repo.pipeline:
            continue
        on_call = oncall.parked(repo.name)
        routes = {tracker.route_label(other) for other in cfg.repos if other != repo and other.pipeline
                  and other.tracker == "jira"
                  and (other.jira_project or cfg.jira.project) == (repo.jira_project or cfg.jira.project)}
        for issue in run_store.latest_failed_tickets(repo.name):
            if issue in on_call:
                continue
            ref = tracker.issue_name(repo.name, issue)
            try:
                source = tracker.for_ticket(cfg, repo, issue)
                if secrets is None:
                    secrets = scrub.secrets()
                if reason := retry_ticket(source, issue, routes if isinstance(source, tracker.JiraTracker) else set(),
                                          secrets):
                    held[ref] = reason
            except (*tracker.ERRORS, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                errors.append(f"{ref} parked retry could not settle ({type(error).__name__})")
    return held, errors
