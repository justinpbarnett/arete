from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from . import config, gh, jev, tracker

FIX, FEATURE, RESEARCH, PERSON = "fix", "feature", "research", "person"
KINDS = (FIX, FEATURE, RESEARCH, PERSON)
AUTO_LABEL = "agent:auto"
ASKS_FOR_CODE = "Does this ticket ask for a change to code, configuration or documentation in a software repository?"
KIND_QUESTION = "What does this ticket ask for?"
KIND_OPTIONS = {
    RESEARCH: ("finding something out: investigate, compare options, estimate or recommend, where the answer is a "
               "written report"),
    PERSON: ("work only a person can do: a meeting, a decision, access or an account, a test or check done by hand, "
             "data entry, a purchase, or settings in an outside tool"),
    FIX: "a repair to existing software that is broken or wrong: a bug, an error, a crash, or wrong data shown",
    FEATURE: "a new or changed capability, screen, field, behavior, setting, refactoring or document in a repository",
    "none": "the text gives no identifiable task",
}
ACTIONABLE = ("Does this ticket request concrete work in a software repository that a coding agent can implement? "
              "Size and unfinished dependencies do not make repository work a human-only task.")
LABEL_STYLES = {
    tracker.LABEL: tracker.APPROVAL_LABEL_STYLE,
    AUTO_LABEL: ("c5def5", "Approved by automatic triage; a person merges the PR"),
    FIX: ("d73a4a", "Repair existing behavior; eligible for Arete's guarded patch flow"),
    FEATURE: ("a2eeef", "New or changed capability; use Arete's full feature flow"),
}


@dataclass(frozen=True)
class Assessment:
    kind: str = ""
    repo: str = ""
    ready: bool = False
    why: str = ""
    cost: float = 0.0


def policy(cfg: config.Config) -> str:
    data = {"version": 2, "code": ASKS_FOR_CODE, "kind_question": KIND_QUESTION,
            "kind": KIND_OPTIONS, "actionable": ACTIONABLE,
            "model": cfg.jev.model, "confidence": cfg.jev.confidence, "skip": cfg.jev.triage_skip,
            "available": jev.ready(cfg),
            "repos": [(r.name, r.pipeline, r.jev, r.tracker, r.github, r.jira_project, r.jira_label) for r in cfg.repos]}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def classify(cfg: config.Config, repos: list[config.Repo], title: str, text: str, *,
             trace: tuple[str, str] = ("", "")) -> Assessment:
    if not repos or not jev.ready(cfg, repos):
        return Assessment(why="JEV is unavailable for this source")
    questions = {"code": jev.yes_no(ASKS_FOR_CODE), "kind": jev.pick(KIND_QUESTION, KIND_OPTIONS),
                 "actionable": jev.yes_no(ACTIONABLE)}
    if len(repos) > 1:
        questions["repo"] = jev.pick("Which single repository owns all the requested changes?", {
            **{r.name: f"the {r.name} repository at {r.path}, route label {tracker.route_label(r)}" for r in repos},
            "none": "the repository is unclear, or changes are required in several repositories",
        })
    reply = jev.ask(f"{title}\n\n{text}", questions, cfg, trace=trace)
    code, kind, actionable = (reply.answers.get(key) for key in ("code", "kind", "actionable"))
    if not code:
        return Assessment(why=reply.error or "JEV gave no code assessment", cost=reply.cost_usd)
    sure = str(kind.value) if kind and kind.value in KINDS and kind.sure(cfg.jev.confidence) else ""
    clear_code = code.yes >= 0.5 and code.sure(cfg.jev.confidence)
    can_implement = bool(actionable and actionable.value is True and actionable.sure(cfg.jev.confidence))
    if not sure and kind and kind.value in (FIX, FEATURE) and clear_code and can_implement:
        sure = FEATURE
    if code.yes <= cfg.jev.triage_skip or code.yes < 0.5 and sure in (RESEARCH, PERSON):
        settled = RESEARCH if sure == RESEARCH else PERSON
        return Assessment(settled, why=f"JEV reads it as {settled} work ({code.note()})", cost=reply.cost_usd)
    if sure not in (FIX, FEATURE) or code.yes < 0.5:
        return Assessment(why="JEV is unsure of the work's kind", cost=reply.cost_usd)
    named = reply.answers.get("repo")
    repo = repos[0].name if len(repos) == 1 else (
        str(named.value) if named and named.sure(cfg.jev.confidence)
        and named.value != "none" and named.value in {r.name for r in repos} else "")
    ready = bool(repo and clear_code and can_implement)
    evidence = f"kind {kind.note()}; code {code.note()}; actionable {actionable.note() if actionable else 'unknown'}"
    if named:
        evidence += f"; repo {named.note()}"
    return Assessment(sure, repo, ready, f"JEV reads it as {sure} work ({evidence}); "
                       + ("the pipeline can begin" if ready else "readiness needs closer triage"), reply.cost_usd)


def add_labels(source: tracker.Tracker, tid: str, labels: list[str]) -> None:
    labels = list(dict.fromkeys(labels))
    if isinstance(source, tracker.GitHubTracker):
        for label in labels:
            color, description = LABEL_STYLES[label]
            if error := gh.ensure_label(source.repo.github, label, color, description, restyle=False):
                raise gh.GhError(error)
        gh.issue_labels(source.repo.github, int(tid), add=labels)
    elif isinstance(source, tracker.JiraTracker):
        source.client.edit_labels(tid, labels)


def route_ticket(cfg: config.Config, repo: config.Repo, source: tracker.Tracker, ticket: tracker.Ticket, *,
                 dry_run: bool = False, log=print) -> tracker.Ticket:
    if not cfg.triage.route_fixes or dry_run or not isinstance(source, (tracker.GitHubTracker, tracker.JiraTracker)):
        return ticket
    if ticket.blocker or not ticket.open:
        return ticket
    if ticket.kind == "fix" or FIX in ticket.labels or FEATURE in ticket.labels:
        return ticket
    assessment = classify(cfg, [repo], ticket.title, f"{ticket.body}\n\n{ticket.comments}",
                          trace=("", f"intake {repo.name} {ticket.ref} jev"))
    if assessment.kind not in (FIX, FEATURE):
        log(f"intake {ticket.ref}: {assessment.why}; keeping the existing route")
        return ticket
    try:
        add_labels(source, ticket.id, [assessment.kind])
    except tracker.ERRORS as e:
        log(f"intake {ticket.ref}: could not save the {assessment.kind} label: {e}")
    log(f"intake {ticket.ref}: {assessment.why}")
    return replace(ticket, kind="fix" if assessment.kind == FIX else "feat",
                   labels=[*ticket.labels, assessment.kind])
