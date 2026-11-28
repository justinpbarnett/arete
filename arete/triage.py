from __future__ import annotations

import hashlib
import json
import re
import tempfile
import textwrap
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from . import (
    alerts,
    config,
    factory_control,
    gh,
    intake,
    jev,
    jira,
    paths,
    principals,
    prompt_loader,
    prompts,
    refusals,
    runner,
    screening,
    tracker,
    util,
)
from .agent import READ_TOOLS
from .heartbeat import mark_loop, network_up_or_skip
from .refusals import TrackerFailed
from .store import db as foundation
from .store import insights as insights_store
from .store import runs as run_store
from .store import triage as triage_store

ISSUE_TITLE = "[arete] triage"
ISSUE_LABEL = "arete-triage"
SKIP_LABELS = {tracker.LABEL, tracker.SPEC_LABEL, tracker.ZTE_LABEL, alerts.LABEL, ISSUE_LABEL, "loop"}
HOLD_LABELS = {"no-agent", "agent:hold"}
AUTO_SKIP_LABELS = {*HOLD_LABELS, "duplicate", "invalid", "wontfix",
                    *(f"agent:{state}" for state in config.RUN_LABELS), *tracker.JIRA_STATE_LABELS}
MAX_CALLS = 20
PER_SOURCE = 50
BUDGET_USD = 0.30
TIMEOUT_SECONDS = 300
MAX_PLAN_STEPS = 6
MAX_ISSUE_CHARS = 60000
META_FILE = paths.STATE_DIR / "triage.json"
TICKET_RECORDS_DIR = paths.STATE_DIR / "triage"
SEARCH_FIELDS = ["summary", "updated", "labels", "parent"]
SPRINT_JQL = "sprint in openSprints() OR (sprint in closedSprints() AND sprint not in futureSprints())"
JIRA_FIELDS = ["summary", "description", "status", "labels", "issuetype", "parent", "updated"]
MAX_ASSUMPTIONS = 8
MAX_CHECKS = 6
THIN_WORDS = 6
CLI_COMMAND_ENDING_OLDER_REASONS = re.compile(r"; (check the ticket, then )?`arete research \S+ --run --force`( researches it anyway)?$")
OUTSIDE_SPRINT = "it is outside the sprint, so the research loop leaves it to you"
NEEDS_FORCE = ("held", "parent", "answered")
MARKUP = re.compile(r"\[[^\]]*\]|\{[^}]*\}|https?://\S+")
CODE_FENCE = re.compile(r"(?m)^ {0,3}(?:(`{3,})[^`\n]*\n(?:.*\n)*? {0,3}\1`*|(~{3,}).*\n(?:.*\n)*? {0,3}\2~*)[ \t]*$")
UNSPACED_SCRIPTS = r"\U00003040-\U000030ff\U00003400-\U00004dbf\U00004e00-\U00009fff\U0000f900-\U0000faff"
WORDS = re.compile(rf"[{UNSPACED_SCRIPTS}]{{2}}|[^\W\d_{UNSPACED_SCRIPTS}]{{2,}}")
SENT_DAYS = 7
SIZES = ("S", "M", "L")
FIX, FEATURE, RESEARCH, PERSON = intake.KINDS
KINDS = intake.KINDS
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "ready": {"type": "boolean"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ASSUMPTIONS},
        "checks": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_CHECKS},
        "size": {"type": "string", "enum": list(SIZES)},
        "repos": {"type": "array", "items": {"type": "string"}},
        "plan": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_PLAN_STEPS},
        "why": {"type": "string"},
    },
    "required": ["kind", "ready", "questions", "assumptions", "checks", "size", "repos", "plan", "why"],
}
WAITING = f"waiting: more than {MAX_CALLS} tickets were due in one refresh"
ASKS_FOR_CODE = intake.ASKS_FOR_CODE
KIND_QUESTION = intake.KIND_QUESTION
KIND_OPTIONS = intake.KIND_OPTIONS
EARLIEST = datetime.min.replace(tzinfo=UTC)
READY, SENT, RESEARCH_WORK, WITH_QUESTIONS, NEEDS_PERSON, NOT_TRIAGED = (
    "Ready to build", "Sent to the pipeline", "Research", "With questions", "Needs a person", "Not triaged")
STANDINGS = (READY, SENT, RESEARCH_WORK, WITH_QUESTIONS, NEEDS_PERSON, NOT_TRIAGED)
SENDABLE = (READY, WITH_QUESTIONS)
WHICH_REPO = "Which repo is it for"
SEVERAL_REPOS = "It needs changes in"
BOARD_LOCK = "triage-board"


@dataclass
class Candidate:
    ref: str
    id: str
    title: str
    url: str
    updated: str
    in_sprint: bool | None = None
    parent: str = ""
    on_hold: bool = False


@dataclass
class TicketText:
    facts: str
    body: str
    comments: str
    updated: str = ""
    labels: tuple[str, ...] = ()
    title: str = ""

    @property
    def text(self) -> str:
        return "\n\n".join(p for p in (self.facts, self.body or "(no description)", self.comments) if p)

    def said(self, title: str) -> str:
        return "\n\n".join(p for p in (title, self.body, self.comments) if p)

    def thin(self) -> bool:
        return len(WORDS.findall(MARKUP.sub(" ", self.body))) < THIN_WORDS


def jql_list(names: list[str]) -> str:
    return ", ".join('"' + n.replace("\\", "\\\\").replace('"', '\\"') + '"' for n in names)


def issue_id(kind: str, ref: str) -> str:
    return ref.rpartition("#")[2] if kind == "github" else ref


@dataclass
class Source:
    key: str
    repos: list[config.Repo] = field(default_factory=list)
    tracker: ClassVar[str]

    def repos_text(self) -> str:
        lines = []
        for r in self.repos:
            limit = config.load_layer(r.layer_dir).max_diff_lines
            lines.append(f"- `{r.name}` at {r.path}{self.route_note(r)}; one run changes at most {limit} lines")
        return "\n".join(lines)

    def by_name(self) -> dict[str, config.Repo]:
        return {r.name: r for r in self.repos}

    def ticket_text(self, c: Candidate) -> str:
        return self.ticket(c).text

    def tid(self, ref: str) -> str:
        return issue_id(self.tracker, ref)

    def candidate(self, r: dict) -> Candidate:
        return Candidate(r["ref"], self.tid(r["ref"]), r["title"], r["url"], r["updated"])


@dataclass
class JiraSource(Source):
    jira_cfg: config.JiraConfig = field(default_factory=config.JiraConfig)
    tracker = "jira"

    @property
    def name(self) -> str:
        return f"Jira {self.key or '(no project)'}"

    @property
    def client(self) -> jira.Client:
        return tracker.JiraTracker(self.repos[0], self.jira_cfg).client

    def candidates(self) -> list[Candidate]:
        if not self.key:
            raise jira.JiraError("no Jira project; set [jira] project or jira_project on the repo")
        client = self.client
        mine = (f'project = "{self.key}" AND assignee = currentUser() AND statusCategory != Done '
                f'AND (labels is EMPTY OR labels not in ("{tracker.LABEL}"))')
        if self.jira_cfg.skip_types:
            mine += f" AND issuetype not in ({jql_list(self.jira_cfg.skip_types)})"
        out = []
        for i in client.search(f"{mine} ORDER BY updated DESC", SEARCH_FIELDS, limit=PER_SOURCE):
            f = i.get("fields") or {}
            if not {tracker.LABEL, tracker.SPEC_LABEL}.intersection(f.get("labels") or []):
                out.append(Candidate(i["key"], i["key"], (f.get("summary") or "").strip(), client.browse_url(i["key"]),
                                     f.get("updated") or "", parent=(f.get("parent") or {}).get("key") or "",
                                     on_hold=not HOLD_LABELS.isdisjoint(f.get("labels") or [])))
        if self.jira_cfg.sprint_only:
            scoped = {i["key"] for i in client.search(f"{mine} AND ({SPRINT_JQL}) ORDER BY updated DESC", ["updated"],
                                                      limit=PER_SOURCE)}
            for c in out:
                c.in_sprint = c.ref in scoped
        return out

    def ticket(self, c: Candidate) -> TicketText:
        client = self.client
        f = client.issue(c.id, JIRA_FIELDS).get("fields") or {}
        if tracker.SPEC_LABEL in (f.get("labels") or []):
            raise tracker.TaskError("specs require explicit tracker approval")
        facts = [f"Type: {(f.get('issuetype') or {}).get('name', '')}",
                 f"Status: {(f.get('status') or {}).get('name', '')}",
                 f"Labels: {', '.join(f.get('labels') or []) or 'none'}"]
        parent = f.get("parent") or {}
        if parent.get("key"):
            facts.append(f"Part of {parent['key']}: {(parent.get('fields') or {}).get('summary', '')}")
        comments = [((cm.get("author") or {}).get("displayName", "someone"), cm.get("created", ""), cm.get("body") or "")
                    for cm in client.comments(c.id)]
        return TicketText("\n".join(facts), (f.get("description") or "").strip(), tracker.comments_text(comments),
                          f.get("updated") or "", tuple(f.get("labels") or []), (f.get("summary") or "").strip())

    def brief(self, key: str) -> str:
        f = self.client.issue(key, ["summary", "description"]).get("fields") or {}
        return f"{key}: {(f.get('summary') or '').strip()}\n\n{(f.get('description') or '').strip() or '(no description)'}"

    def route_note(self, r: config.Repo) -> str:
        return f", route label `{tracker.route_label(r)}`"

    def repo_rule(self) -> str:
        names = ", ".join(f"`{r.name}`" for r in self.repos)
        return (f"the repositories the change belongs in, from {names}. Usually one. List more than one only when the "
                "change cannot land without edits in each. Empty when the ticket and the code do not settle it. A "
                "ticket that already carries a route label belongs to that label's repository. For `research`, the "
                "repositories the research has to read, which may be none.")

    def chosen_repos(self, answer: dict) -> list[str]:
        names = self.by_name()
        picked = (str(r).strip().strip("`") for r in answer.get("repos") or [])
        return list(dict.fromkeys(r for r in picked if r in names))

    def route(self, repo: str) -> str:
        return tracker.route_label(self.by_name()[repo]) if repo else ""

    def comment(self, ref: str, text: str, *, return_url=False) -> str | None:
        if return_url:
            return self.client.comment(ref, text, return_url=True, single_attempt=True)
        return self.client.comment(ref, text)

    def find_comment(self, ref: str, marker: str) -> str:
        return self.client.find_comment_marker(ref, marker)

    def approve(self, r: dict) -> None:
        self.client.edit_labels(r["ref"], labels_to_add(r))


@dataclass
class GitHubSource(Source):
    tracker = "github"

    @property
    def name(self) -> str:
        return f"GitHub {self.key}"

    @property
    def issues(self) -> tracker.GitHubTracker:
        return tracker.GitHubTracker(self.repos[0])

    def candidates(self) -> list[Candidate]:
        if not self.key:
            raise gh.GhError(f"{self.repos[0].name} has no github repo set")
        tk = self.issues
        out = []
        search = " ".join(f'-label:"{label}"' for label in sorted(SKIP_LABELS))
        for i in gh.issues(self.key, author=tk.owner, limit=PER_SOURCE, search=search):
            names = {lb["name"] for lb in i.get("labels", [])}
            if names & SKIP_LABELS or not tk.by_owner(i):
                continue
            out.append(Candidate(f"{tk.repo.name}#{i['number']}", str(i["number"]), i["title"].strip(),
                                 i.get("url", ""), i.get("updatedAt") or "", on_hold=bool(names & HOLD_LABELS)))
        return out

    def ticket(self, c: Candidate) -> TicketText:
        tk = self.issues
        issue = gh.issue(self.key, int(c.id))
        if not tk.by_owner(issue):
            raise gh.GhError(f"{c.ref} was not opened by {tk.owner}")
        if issue.get("state") != "OPEN":
            raise gh.GhError(f"{c.ref} is no longer open")
        labels = tuple(lb["name"] for lb in issue.get("labels", []))
        if tracker.ZTE_LABEL in labels:
            raise tracker.TaskError(tracker.ZTE_QUEUE)
        if tracker.SPEC_LABEL in labels:
            raise tracker.TaskError("specs require explicit tracker approval")
        comments = [(tk.owner, cm.get("createdAt", ""), cm.get("body", ""))
                    for cm in issue.get("comments", []) if tk.by_owner(cm)]
        return TicketText(f"Labels: {', '.join(labels) or 'none'}", (issue.get("body") or "").strip(),
                          tracker.comments_text(comments), issue.get("updatedAt") or "", labels,
                          (issue.get("title") or "").strip())

    def brief(self, key: str) -> str:
        return ""

    def route_note(self, r: config.Repo) -> str:
        return ""

    def repo_rule(self) -> str:
        return f"always `[\"{self.repos[0].name}\"]`."

    def chosen_repos(self, answer: dict) -> list[str]:
        return [self.repos[0].name]

    def route(self, repo: str) -> str:
        return ""

    def comment(self, ref: str, text: str, *, return_url=False, approved_by: str | None = None, c=None) -> str | None:
        import secrets

        from . import outbox

        tid = self.tid(ref)
        obj = f"github:{self.key}#{int(tid)}"
        outbox.draft(
            c,
            target="github",
            kind="comment",
            object=obj,
            body=text,
            drafted_by=approved_by or "triage",
            approved_by=approved_by,
            slot=f"triage:{ref}:comment:{secrets.token_hex(6)}",
            summary=f"comment on {self.key}#{int(tid)}",
        )
        return "" if return_url else None

    def find_comment(self, ref: str, marker: str) -> str:
        return gh.find_issue_comment_marker(self.key, int(self.tid(ref)), marker)

    def approve(self, r: dict, *, approved_by: str | None = None, c=None) -> None:
        import secrets

        from . import outbox

        tid = self.tid(r["ref"])
        labels = list(dict.fromkeys(labels_to_add(r)))
        obj = f"github:{self.key}#{int(tid)}"
        outbox.draft(
            c,
            target="github",
            kind="label",
            object=obj,
            body=json.dumps({"add": labels, "remove": [], "family": ""}),
            drafted_by=approved_by or "triage",
            approved_by=approved_by,
            slot=f"triage:{r['ref']}:{r.get('updated') or secrets.token_hex(6)}:approve",
            summary=f"add {', '.join(labels)} on {self.key}#{int(tid)}",
        )


TriageSource = JiraSource | GitHubSource


def sources(cfg: config.Config) -> list[TriageSource]:
    found: dict[tuple[str, str], TriageSource] = {}
    for r in cfg.repos:
        if not (r.pipeline and r.parts) or r.tracker == "local":
            continue
        if r.tracker == "jira":
            project = r.jira_project or cfg.jira.project
            src = found.setdefault(("jira", project), JiraSource(project, jira_cfg=cfg.jira))
        else:
            src = found.setdefault(("github", r.github), GitHubSource(r.github))
        src.repos.append(r)
    return list(found.values())


def moment(ts: str) -> datetime:
    try:
        when = jira.parse_time(ts)
    except ValueError:
        return EARLIEST
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def natural(ref: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", ref)]


def read_candidates(src: TriageSource) -> list[Candidate]:
    return sorted(src.candidates(), key=lambda c: moment(c.updated), reverse=True)


def record_file(ref: str) -> Path:
    return TICKET_RECORDS_DIR / f"{tracker.safe_filename(ref)}.md"


def classified_by_jev(cfg: config.Config, src: TriageSource, c: Candidate,
                      ticket: str) -> tuple[dict | None, float]:
    assessment = intake.classify(cfg, src.repos, c.title, ticket, trace=("", f"triage {c.ref} jev"))
    if assessment.kind in (FIX, FEATURE) and assessment.ready:
        settled = row(src, c, updated=c.updated, ready=1, code=1, kind=assessment.kind,
                      repo=assessment.repo, repos=[assessment.repo], route=src.route(assessment.repo),
                      why=assessment.why, cost=assessment.cost)
    elif assessment.kind in (RESEARCH, PERSON):
        settled = settle(src, c, {"kind": assessment.kind, "why": assessment.why}, assessment.cost)
    else:
        return None, assessment.cost
    TICKET_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    record_file(c.ref).write_text(f"{ticket}\n\n## Triage result ({util.paid_note(assessment.cost, 5)})\n\n```json\n"
                                  f"{json.dumps(settled, indent=2)}\n```\n")
    return settled, assessment.cost


def triage_one(cfg: config.Config, src: TriageSource, c: Candidate, ticket: str) -> tuple[dict | None, float, str]:
    text = prompts.render_engine("triage", repos=src.repos_text(), ref=c.ref, title=c.title, ticket=ticket,
                  repo_rule=src.repo_rule())
    dirs = list(dict.fromkeys(r.path for r in src.repos if r.path.is_dir()))
    TICKET_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    with nullcontext(dirs[0]) if dirs else tempfile.TemporaryDirectory() as cwd:
        res = runner.run(text, cwd, model=cfg.model("triage"), tools=READ_TOOLS,
                         allowed_tools=READ_TOOLS, json_schema=SCHEMA,
                         max_budget_usd=BUDGET_USD, timeout=TIMEOUT_SECONDS, persist=False, add_dirs=dirs[1:],
                         trace=("", f"triage {c.ref}"), cfg=cfg)
    answer = res.structured if res.ok and isinstance(res.structured, dict) else None
    record_file(c.ref).write_text(f"{text}\n\n## Triage result ({util.paid_note(res.paid_usd, 3)})\n\n```json\n"
                                  f"{json.dumps(answer or {'error': res.error}, indent=2)}\n```\n")
    return answer, res.paid_usd, "" if answer else util.first_line(res.error or "no answer")


def lines_of(items) -> list[str]:
    cleaned = (re.sub(r"^\s*(\d+[.)]|[-*])(\s+|$)", "", str(item)).strip() for item in items or [])
    return [item for item in cleaned if item]


def settle(src: TriageSource, c: Candidate, answer: dict, cost: float) -> dict:
    kind = answer.get("kind") if answer.get("kind") in KINDS else FEATURE
    repos = src.chosen_repos(answer)
    repo = repos[0] if len(repos) == 1 else ""
    why = str(answer.get("why") or "").strip()
    if kind in (RESEARCH, PERSON):
        return row(src, c, updated=c.updated, ready=0, code=0, kind=kind, repo=repo, repos=repos, why=why, cost=cost)
    questions = lines_of(answer.get("questions"))
    size = answer.get("size") if answer.get("size") in SIZES else "M"
    if not repos:
        questions.append(f"{WHICH_REPO}: {', '.join(src.by_name())}?")
    elif len(repos) > 1:
        questions.append(f"{SEVERAL_REPOS} {' and '.join(repos)}. Can you split it into one ticket per repo?")
    if size == "L":
        questions.append("It is too big for one pull request. Can you split it into smaller tickets?")
    return row(src, c, updated=c.updated, ready=int(bool(answer.get("ready")) and not questions), code=1, kind=kind,
               repo=repo, repos=repos, route=src.route(repo), size=size, questions=questions,
               assumptions=lines_of(answer.get("assumptions"))[:MAX_ASSUMPTIONS],
               checks=lines_of(answer.get("checks"))[:MAX_CHECKS],
               plan=lines_of(answer.get("plan"))[:MAX_PLAN_STEPS], why=why, cost=cost)


def row(src: TriageSource, c: Candidate, **fields) -> dict:
    base = {"ref": c.ref, "source": src.name, "tracker": src.tracker, "repo": "", "route": "", "title": c.title,
            "url": c.url, "updated": "", "ready": None, "code": None, "kind": "", "size": "", "repos": [],
            "questions": [], "assumptions": [], "checks": [], "plan": [], "why": "", "cost": 0.0, "ts": util.iso(),
            "in_sprint": c.in_sprint, "parent": c.parent, "closed": ""}
    return {**base, **fields}


def kept(src: TriageSource, c: Candidate, prev: dict | None, reason: str) -> dict:
    if prev and prev["ready"] is not None:
        route = src.route(prev["repo"]) if prev["route"] and prev["repo"] in src.by_name() else prev["route"]
        return {**prev, "source": src.name, "route": route, "title": c.title, "url": c.url, "in_sprint": c.in_sprint,
                "parent": c.parent, "closed": ""}
    return row(src, c, why=reason)


def delivered_since() -> dict[str, dict]:
    since = util.iso(util.now() - timedelta(days=SENT_DAYS))
    return {x["ref"]: x for x in insights_store.research_rows() if (x.get("delivered") or "") >= since}


def still_shown(src: TriageSource, read: list[Candidate], old: dict[str, dict],
                delivered: dict[str, dict]) -> list[dict]:
    open_refs = {c.ref for c in read}
    return [{**old[ref], "closed": old[ref].get("closed") or x["delivered"],
             "updated": x["updated"] or old[ref]["updated"]}
            for ref, x in delivered.items()
            if ref in old and old[ref]["source"] == src.name and ref not in open_refs]


def sent_since_read(c: Candidate, sent: dict[str, str]) -> bool:
    return c.ref in sent and util.parse_iso(sent[c.ref]) >= moment(c.updated).replace(microsecond=0)


def due(c: Candidate, prev: dict | None) -> bool:
    return not prev or prev["ready"] is None or prev["updated"] != c.updated or not prev["kind"]


def run(dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    with util.lock("triage") as got:
        if not got:
            print(f"{util.iso()} triage already running")
            return 0
        if not network_up_or_skip("triage", dry_run):
            return 0
        return refresh(cfg, dry_run)


def read_all(srcs):
    read: dict[str, list[Candidate]] = {}
    errors: list[str] = []
    for src in srcs:
        try:
            read[src.name] = read_candidates(src)
        except tracker.ERRORS as e:
            errors.append(f"{src.name}: {e}")
            print(f"{util.iso()} {errors[-1]}")
    return read, errors


@dataclass
class Triaged:
    answered: dict[str, dict] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    waiting: list[str] = field(default_factory=list)
    spent: float = 0.0
    calls: int = 0
    screened: int = 0


def triage_due(cfg, todo) -> Triaged:
    triaged = Triaged()
    for src, c in todo:
        print(f"{util.iso()} {c.ref}: triaging", flush=True)
        try:
            ticket = src.ticket_text(c)
        except tracker.ERRORS as e:
            triaged.failed[c.ref] = f"could not read it: {e}"
            print(f"{util.iso()} {c.ref}: triage failed: {triaged.failed[c.ref]}")
            continue
        settled, screening_cost = classified_by_jev(cfg, src, c, ticket)
        triaged.screened += 1
        triaged.spent += screening_cost
        if settled:
            triaged.answered[c.ref] = settled
            print(f"{util.iso()} {c.ref}: {verdict(settled)} by JEV, no model call")
            continue
        if triaged.calls >= MAX_CALLS:
            triaged.waiting.append(c.ref)
            continue
        triaged.calls += 1
        try:
            answer, cost, error = triage_one(cfg, src, c, ticket)
        except tracker.ERRORS as e:
            answer, cost, error = None, 0.0, f"could not read it: {e}"
        triaged.spent += cost
        if answer:
            triaged.answered[c.ref] = settle(src, c, answer, cost + screening_cost)
            print(f"{util.iso()} {c.ref}: {verdict(triaged.answered[c.ref])} ({util.paid_note(cost, 3)})")
        else:
            triaged.failed[c.ref] = error
            print(f"{util.iso()} {c.ref}: triage failed: {error}")
    return triaged


def write_board(srcs, read, triaged):
    with util.lock(BOARD_LOCK, wait=True):
        board = {r["ref"]: r for r in triage_store.triage_rows()}
        sent = {a["ref"]: a["ts"] for a in triage_store.triage_approvals()}
        delivered = delivered_since()
        for src in srcs:
            if src.name in read:
                triage_store.triage_replace(src.name, [
                    triaged.answered.get(c.ref) or kept(src, c, board.get(c.ref),
                                                f"triage failed: {triaged.failed[c.ref]}" if c.ref in triaged.failed else WAITING)
                    for c in read[src.name] if not sent_since_read(c, sent)]
                    + still_shown(src, read[src.name], board, delivered))
        triage_store.triage_prune([s.name for s in srcs])


def prune_records():
    keep = {record_file(r["ref"]).name for r in triage_store.triage_rows()}
    for f in TICKET_RECORDS_DIR.glob("*.md"):
        if f.name not in keep:
            f.unlink()


def preview_refresh(cfg, srcs, read, old, todo):
    preview(srcs, read, old, todo, [])
    print(f"at most {MAX_CALLS} uncertain tickets use the fallback agent")
    by_name = {s.name: s for s in srcs}
    ready = ", ".join(r["ref"] for r in approvable(srcs, list(old.values()))
                      if auto_limit(cfg, by_name[r["source"]])) or "none"
    print(f"automatic approval candidates within configured limits: {ready}; "
          "fresh labels, issue versions and required screens still decide admission")


@factory_control.work
def refresh(cfg: config.Config, dry_run: bool) -> int:
    old = {r["ref"]: r for r in triage_store.triage_rows()}
    meta = util.read_json(META_FILE, {})
    policy = intake.policy(cfg)
    changed_policy = meta.get("policy") != policy
    retry = set(meta.get("waiting", [])) | set(meta.get("failed", {}))
    srcs = sources(cfg)
    read, errors = read_all(srcs)
    todo = sorted(((src, c) for src in srcs for c in read.get(src.name, [])
                   if changed_policy or c.ref in retry or due(c, old.get(c.ref))),
                  key=lambda sc: moment(sc[1].updated), reverse=True)
    if dry_run:
        preview_refresh(cfg, srcs, read, old, todo)
        return 1 if errors else 0
    triaged = triage_due(cfg, todo)
    write_board(srcs, read, triaged)
    approved, held = approve_ready(cfg, srcs, excluded=set(triaged.failed) | set(triaged.waiting))
    prune_records()
    triaged.waiting += [r["ref"] for r in old.values() if r["source"] not in read
                        and (changed_policy or r["ref"] in retry)]
    previous_holds = meta.get("label_holds", {})
    label_holds = {src.name: previous_holds.get(src.name, []) for src in srcs}
    for source, candidates in read.items():
        label_holds[source] = [c.ref for c in candidates if c.on_hold]
    meta.update(refreshed=util.iso(), policy=policy, errors=errors, label_holds=label_holds, waiting=triaged.waiting,
                failed=triaged.failed, approved=approved, held=held)
    problems = list(errors)
    try:
        posted = publish(cfg, view(), meta)
    except (gh.GhError, BoardRefused) as e:
        problems.append(f"posting the board: {e}")
        posted = f"not posted: {e}"
    meta.update(posted=posted)
    util.write_json(META_FILE, meta)
    print(f"{util.iso()} triage: {util.count(triaged.calls, 'model call')}, {triaged.screened} JEV attempts "
          f"({util.paid_note(triaged.spent, 3)}), "
          f"{len(triaged.failed)} failed, {len(triaged.waiting)} waiting, {len(approved)} approved; board {posted}")
    if triaged.waiting:
        print(f"waiting for the next refresh: {', '.join(triaged.waiting)}")
    mark_loop("triage", not problems, "; ".join(problems))
    return 1 if problems else 0


def approvable(srcs: list[TriageSource], rows: list[dict]) -> list[dict]:
    by_name = {s.name: s for s in srcs}
    ready = [r for r in rows if r["source"] in by_name and standing(r) == READY
             and (not isinstance(by_name[r["source"]], JiraSource) or r["route"] and in_sprint(r))]
    return sorted(ready, key=lambda r: moment(r["updated"]), reverse=True)


def auto_limit(cfg: config.Config, src: TriageSource) -> int:
    if isinstance(src, JiraSource) and cfg.jira.auto:
        return cfg.jira.auto_per_refresh
    return cfg.triage.auto_per_refresh if cfg.triage.auto else 0


def approve_ready(cfg: config.Config, srcs: list[TriageSource], *,
                  excluded: set[str] | frozenset[str] = frozenset()) -> tuple[list[str], dict[str, str]]:
    approved: list[str] = []
    held: dict[str, str] = {}
    counts: dict[str, int] = {}
    by_name = {s.name: s for s in srcs}
    for r in approvable(srcs, triage_store.triage_rows()):
        if r["ref"] in excluded:
            continue
        src = by_name[r["source"]]
        if counts.get(src.tracker, 0) >= auto_limit(cfg, src):
            continue
        with util.lock(BOARD_LOCK, wait=True):
            if board_row(r["ref"]) != r:
                continue
            try:
                why = approve(cfg, src, r)
            except tracker.ERRORS as e:
                why = f"could not approve it: {e}"
        if why:
            held[r["ref"]] = why
            print(f"{util.iso()} {r['ref']}: not approved: {why}")
        else:
            approved.append(r["ref"])
            counts[src.tracker] = counts.get(src.tracker, 0) + 1
            print(f"{util.iso()} {r['ref']}: approved, added {' '.join(labels_to_add({**r, 'automatic': True}))}")
    return approved, held


def approve(cfg: config.Config, src: TriageSource, r: dict) -> str:
    repo = src.by_name().get(r["repo"])
    if not repo:
        return f"{r['repo'] or 'no repo'} is not a pipeline repo of {src.name}"
    if not jev.ready(cfg, [repo]):
        return "JEV is not on for it, so nothing screens the ticket; add the labels by hand"
    now = src.ticket(src.candidate(r))
    if stopped := sorted(set(now.labels) & AUTO_SKIP_LABELS):
        return f"its {', '.join(stopped)} label requires a person to release it"
    if now.updated != r["updated"]:
        return "it changed since triage, so the next refresh triages it again"
    screened = screening.screen(f"{r['title']}\n\n{now.text}", cfg, [repo], ("", f"triage {r['ref']} injection jev"))
    if screening.held(screened, screening.Posture.REQUIRED):
        if screened.status != screening.FLAGGED:
            return "JEV could not screen it this time"
        labels = " and ".join(f"`{lb}`" for lb in labels_to_add(r))
        triage_store.triage_put({**r, "ready": 0, "questions": [
            (f"JEV reads text in it as instructions to an agent (p {screened.p:.2f}). Check the ticket, then add "
             f"{labels} by hand to approve it.")]})
        return f"held for the injection screen (p {screened.p:.2f})"
    to_pipeline(src, r, "triage")
    return ""


def to_pipeline(src: TriageSource, r: dict, sent_by: str) -> None:
    if isinstance(src, GitHubSource):
        with foundation.tx() as c:
            src.approve({**r, "automatic": sent_by == "triage"}, c=c)
            record_send(r, sent_by)
        return
    src.approve({**r, "automatic": sent_by == "triage"})
    with foundation.tx():
        record_send(r, sent_by)


def record_send(row: dict, sent_by: str) -> None:
    triage_store.triage_approve(row, sent_by)
    triage_store.triage_drop(row["ref"])


class NotOnBoard(refusals.NotFound, LookupError):
    pass


class NotSendable(refusals.Conflict, RuntimeError):
    pass


class BadAnswers(refusals.Invalid, ValueError):
    pass


def tracker_said(e: Exception) -> str:
    return util.first_line(str(e), 200).rstrip(".")


def board_row(ref: str) -> dict | None:
    return next((r for r in triage_store.triage_rows() if r["ref"].lower() == ref.strip().lower()), None)


def repo_question(r: dict) -> str:
    return "" if r["repo"] else next((q for q in r["questions"] if q.startswith((WHICH_REPO, SEVERAL_REPOS))), "")


def repo_choices(src: TriageSource, r: dict) -> list[str]:
    names = src.by_name()
    return [n for n in ([r["repo"]] if r["repo"] else r["repos"] or names) if n in names]


def answers_comment(pairs: list[tuple[str, str]]) -> str:
    return f"{prompt_loader.TRIAGE_ANSWERS}:\n\n" + "\n\n".join(f"**{q}**\n{a}" for q, a in pairs)


@factory_control.work
def send(cfg: config.Config, ref: str, questions: list[str], answers: list[str], repo_name: str,
         *, publication=None) -> str:
    with util.lock(BOARD_LOCK, wait=True):
        if publication and publication.pending:
            pending = publication.pending
            if "result" in pending:
                return pending["result"]
            sent_row = pending["row"]
            src = send_source(cfg, sent_row)
            if comment_target(src, sent_row) != pending["target"]:
                raise NotSendable("The tracker target changed while this send was pending.")
            current = board_row(ref)
            repo = src.by_name().get(sent_row["repo"])
            if (not current or current != pending.get("board") or not repo or standing(current) not in SENDABLE
                    or repo.name not in repo_choices(src, current) or src.route(repo.name) != sent_row["route"]):
                raise NotSendable(f"Triage changed {ref} while the send was pending. Read it again.")

            def revalidate(*, posted=False):
                now = read_send_ticket(src, current)
                if [now.title, now.body] != pending["content"]:
                    raise NotSendable(f"{ref} changed since triage read it, so the next refresh triages it again.")
                if set(now.labels) & AUTO_SKIP_LABELS:
                    raise NotSendable(f"{ref} now has a label that requires a person to release it.")
                if pending.get("labels_started") and set(labels_to_add(sent_row)) <= set(now.labels):
                    return True
                if not posted:
                    require_send_revision(now, current)
                screen_send(cfg, current, repo, sent_row, now)
                return False

            return send_to_tracker(src, sent_row, pending["comment"], publication, revalidate=revalidate)
        r = board_row(ref)
        if not r:
            if a := triage_store.triage_approval(ref.strip()):
                raise NotOnBoard(f"{a['ref']} went to the pipeline {stamp(a['ts'])}, so there is nothing left to send.")
            raise NotOnBoard(f"{ref} is not on the triage board.")
        if standing(r) not in SENDABLE:
            raise NotSendable(f"{r['ref']} is under {standing(r)}. Only tickets that are ready or have questions go to "
                              "the pipeline from here.")
        if questions != r["questions"]:
            raise NotSendable(f"Triage asked new questions about {r['ref']} since the page loaded. Read them and send "
                              "again.")
        if len(answers) != len(questions):
            raise BadAnswers(f"Send one answer for each of the {util.count(len(questions), 'question')}.")
        src = send_source(cfg, r)
        if r["repo"] and repo_name not in ("", r["repo"]):
            raise BadAnswers(f"Triage settled {r['ref']} on {r['repo']}, not {repo_name}.")
        choices = repo_choices(src, r)
        if not choices:
            raise NotSendable(f"No repo triage named for {r['ref']} is a pipeline repo of {src.name} now.")
        if (r["repo"] or repo_name) not in choices:
            raise BadAnswers(f"{repo_name} is not one of the repos {r['ref']} can go to: {', '.join(choices)}."
                             if repo_name else f"Pick the repo {r['ref']} is for, from {', '.join(choices)}.")
        repo = src.by_name()[r["repo"] or repo_name]
        sent_row = {**r, "repo": repo.name, "route": src.route(repo.name)}
        now = read_send_ticket(src, r)
        require_send_revision(now, r)
        if not HOLD_LABELS.isdisjoint(now.labels):
            raise NotSendable(f"{ref} has a hold label. Release it on the tracker before sending.")
        screen_send(cfg, r, repo, sent_row, now)
        pairs = [(q, a.strip()) for q, a in zip(questions, answers, strict=True) if q != repo_question(r) and a.strip()]
        comment = answers_comment(pairs) if pairs else ""
        if publication:
            publication.remember({"row": sent_row, "board": r, "target": comment_target(src, sent_row),
                                  "comment": comment, "content": [now.title, now.body]})
        return send_to_tracker(src, sent_row, comment, publication)


def read_send_ticket(src, row):
    try:
        return src.ticket(src.candidate(row))
    except tracker.ERRORS as e:
        raise TrackerFailed(f"Could not read {row['ref']}.") from e


def require_send_revision(now, row):
    if now.updated != row["updated"]:
        raise NotSendable(f"{row['ref']} changed since triage read it, so the next refresh triages it again.")


def screen_send(cfg, row, repo, sent_row, now):
    ref = row["ref"]
    screened = screening.screen(f"{row['title']}\n\n{now.text}", cfg, [repo], ("", f"triage {ref} send injection jev"))
    if screening.held(screened, screening.Posture.WHEN_ON):
        if screened.status != screening.FLAGGED:
            raise NotSendable(f"JEV could not screen {ref} for injected instructions this time. Try again.")
        labels = " and ".join(labels_to_add(sent_row))
        raise NotSendable(f"JEV reads text in {ref} as instructions to an agent (p {screened.p:.2f}). "
                          f"Read the ticket, then add {labels} by hand to send it.")


def send_source(cfg, row):
    src = next((s for s in sources(cfg) if s.name == row["source"]), None)
    if not src:
        raise NotSendable(f"{row['source']} is no longer a triage source, so the next refresh drops {row['ref']}.")
    return src


def comment_target(src, row):
    return src.client.browse_url(row["ref"]) if isinstance(src, JiraSource) else row["url"]


def send_to_tracker(src, row, comment, publication, *, revalidate=None):
    ref = row["ref"]
    labels_list = list(dict.fromkeys(labels_to_add({**row, "automatic": False})))
    labels = " and ".join(labels_list)
    url = row["url"]
    if isinstance(src, GitHubSource):
        from . import outbox

        actor = principals.current()
        now = util.iso()
        tid = src.tid(ref)
        obj = f"github:{src.key}#{int(tid)}"
        stamp_key = row.get("updated") or now
        done = f"posted your answers and added {labels}" if comment else f"added {labels}"
        result = f"Sent {ref} to the pipeline for {row['repo']}: {done}."
        with foundation.tx() as c:
            if comment:
                outbox.draft(
                    c,
                    target="github",
                    kind="comment",
                    object=obj,
                    body=comment,
                    drafted_by=actor,
                    approved_by=actor,
                    opened_at=now,
                    slot=f"triage:{ref}:{stamp_key}:answers",
                    summary=f"post triage answers on {src.key}#{int(tid)}",
                )
            outbox.draft(
                c,
                target="github",
                kind="label",
                object=obj,
                body=json.dumps({"add": labels_list, "remove": [], "family": ""}),
                drafted_by=actor,
                approved_by=actor,
                opened_at=now,
                slot=f"triage:{ref}:{stamp_key}:approve",
                summary=f"add {labels} on {src.key}#{int(tid)}",
            )
            record_send(row, actor)
            if publication:
                publication.record(f"triage:{ref}", url, result)
        return result
    labels_present = revalidate() if revalidate and not comment else False
    if comment:
        try:
            if publication:
                url = src.find_comment(ref, publication.marker)
                if url and revalidate:
                    labels_present = revalidate(posted=True)
                if not url:
                    if revalidate:
                        revalidate()
                    publication.begin_send()
                    url = src.comment(ref, publication.text(comment), return_url=True)
                if not url:
                    raise TrackerFailed(f"Could not confirm the URL of the answers posted to {ref}. Try again.")
            else:
                src.comment(ref, comment)
        except tracker.ERRORS as e:
            raise TrackerFailed(f"Could not post your answers to {ref} ({tracker_said(e)}).") from e
    try:
        if not labels_present:
            if publication and not publication.pending.get("labels_started"):
                publication.remember({**publication.pending, "labels_started": True})
            src.approve({**row, "automatic": False})
    except tracker.ERRORS as e:
        if comment:
            raise TrackerFailed(f"Posted your answers, but could not add {labels} to {ref} ({tracker_said(e)}). "
                                "Add them by hand, or the next refresh triages it again with your answers.") from e
        raise TrackerFailed(f"Could not add {labels} to {ref} ({tracker_said(e)}).") from e
    done = f"posted your answers and added {labels}" if comment else f"added {labels}"
    result = f"Sent {ref} to the pipeline for {row['repo']}: {done}."
    with foundation.tx():
        record_send(row, principals.current())
        if publication:
            publication.record(f"triage:{ref}", url, result)
    return result


def preview(srcs: list[TriageSource], read: dict[str, list[Candidate]], old: dict[str, dict],
            now: list[tuple[TriageSource, Candidate]], later: list[tuple[TriageSource, Candidate]]) -> None:
    for src in srcs:
        if src.name not in read:
            continue
        print(f"{src.name}: {util.count(len(read[src.name]), 'candidate')}")
        for c in read[src.name]:
            prev = old.get(c.ref)
            seen = ("new" if not prev else "waiting" if prev["why"] == WAITING else "retry" if prev["ready"] is None
                    else "changed" if prev["updated"] != c.updated else "no kind" if not prev["kind"] else "unchanged")
            print(f"  {c.ref:<14} {seen:<9} {c.updated[:19]:<19}  {c.title}"[:160])
    print(f"would triage {len(now)}: {', '.join(c.ref for _, c in now) or 'none'}")
    if later:
        print(f"would leave waiting for the next refresh: {', '.join(c.ref for _, c in later)}")
    print("dry run: no model calls, nothing written")


def standing(r: dict) -> str:
    if r["ready"] is None:
        return NOT_TRIAGED
    if r["code"] == 0:
        return RESEARCH_WORK if r["kind"] == RESEARCH else NEEDS_PERSON
    return READY if r["ready"] else WITH_QUESTIONS


def sent(connect=foundation.db) -> list[dict]:
    out = []
    for a in triage_store.triage_approvals(util.iso(util.now() - timedelta(days=SENT_DAYS)), connect):
        runs = [r for r in run_store.ticket_runs(a["repo"], issue_id(a["tracker"], a["ref"]), limit=1, connect=connect)
                if r["started"] >= a["ts"]]
        out.append({**a, "run": runs[0] if runs else None})
    return out


def view(connect=foundation.db) -> dict:
    return {"rows": sorted(triage_store.triage_rows(connect), key=lambda r: natural(r["ref"])), "sent": sent(connect),
            "research": {r["ref"]: r for r in insights_store.research_rows(connect)}}


def groups(v: dict) -> dict[str, list[dict]]:
    out = {s: [r for r in v["rows"] if standing(r) == s] for s in STANDINGS}
    out[SENT] = v["sent"]
    return out


def labels_to_add(r: dict) -> list[str]:
    return [tracker.LABEL, *([r["route"]] if r["route"] else []),
            *([r["kind"]] if r.get("kind") in (FIX, FEATURE) else []),
            *([intake.AUTO_LABEL] if r.get("automatic") else [])]


def in_sprint(r: dict) -> bool:
    return r.get("in_sprint") != 0


def research_due(r: dict, report: dict | None) -> bool:
    return not report or report["status"] != "running" and report["updated"] != r["updated"]


def stamp(ts: str) -> str:
    return util.parse_iso(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z") if ts else ""


def filed_words(report: dict) -> str:
    if not report.get("filed_path"):
        return ""
    name = report["filed_path"].rpartition("/")[2]
    return f", filed as {name}" if report.get("filed_url") else f", not filed as {name} yet; the research loop tries again"


def research_progress(r: dict, report: dict | None) -> str:
    if not report:
        return "queued for research" if in_sprint(r) else OUTSIDE_SPRINT
    if report["status"] == "running":
        return f"researching since {stamp(report['ts'])}"
    if r.get("closed") and report["status"] == "done":
        return (f"report delivered {stamp(report.get('delivered') or r['closed'])}{filed_words(report)}, and arete "
                "moved the ticket to done")
    again = ((", and the ticket changed since, so the loop checks it again" if in_sprint(r)
              else f", and the ticket changed since; {OUTSIDE_SPRINT}") if research_due(r, report) else "")
    why = CLI_COMMAND_ENDING_OLDER_REASONS.sub("", report["error"] or "")
    if report["status"] == "done":
        unmoved = f"; {report['error']}" if report["error"] else ""
        return f"report ready {stamp(report['ts'])}{filed_words(report)}{unmoved}{again}"
    if report["status"] == "held":
        return f"held: {why}{again}"
    if report["status"] in ("parent", "answered"):
        return f"not researched: {why}{again}"
    return f"research failed: {why}{again}"


def research_command(r: dict, report: dict | None) -> str:
    ref = r["ref"]
    if report and report["status"] in NEEDS_FORCE:
        check = "check the ticket, then " if report["status"] == "held" else ""
        return f"{check}`arete research {ref} --run --force` researches it anyway"
    if report and report["status"] == "failed" and not (in_sprint(r) and research_due(r, report)):
        return f"`arete research {ref} --run` tries again"
    if not in_sprint(r) and research_due(r, report):
        return f"`arete research {ref} --run` researches it{' again' if report else ''}"
    return ""


def research_note(r: dict, report: dict | None, for_issue: bool = False) -> str:
    progress = research_progress(r, report)
    return "; ".join(x for x in (quiet(progress) if for_issue else progress, research_command(r, report)) if x)


def research_offer(r: dict, report: dict | None) -> dict | None:
    if not report:
        return {"label": "Research it now", "force": False}
    if report["status"] == "running":
        return None
    if report["status"] in NEEDS_FORCE:
        return {"label": "Research it anyway", "force": True}
    return {"label": "Research it again", "force": report["status"] == "done" and not research_due(r, report)}


def sent_prs(a: dict) -> list[str]:
    return ((a.get("run") or {}).get("pr_url") or "").split()


def sent_note(a: dict, with_prs: bool = True) -> str:
    run = a.get("run")
    by_you = principals.is_person(a["sent_by"])
    if not run:
        return f"{'sent by you' if by_you else 'approved'} {stamp(a['ts'])}, waiting for the pipeline"
    prs = sent_prs(a) if with_prs else []
    return ("sent by you, " if by_you else "") + f"pipeline {run['status']}" + (f": {', '.join(prs)}" if prs else "")


def verdict(r: dict) -> str:
    kind = standing(r)
    if kind == READY:
        return ", ".join(p for p in (f"ready {r['kind']}".strip(), r["size"], f"add {' '.join(labels_to_add(r))}",
                                     "" if in_sprint(r) else "outside the sprint") if p)
    if kind == WITH_QUESTIONS:
        return ", ".join(p for p in ("not ready", r["kind"], r["size"], util.count(len(r["questions"]), "question")) if p)
    if kind == RESEARCH_WORK:
        return "research"
    return kind.lower()


def summary(v: dict) -> str:
    g = groups(v)
    shut = sum(1 for r in v["rows"] if r.get("closed"))
    on_board = ", ".join(f"{sum(1 for r in g[s] if not r.get('closed'))} {s.lower()}" for s in STANDINGS if s != SENT)
    delivered = f"; {shut} research delivered in the last {SENT_DAYS} days" if shut else ""
    return (f"{util.count(len(v['rows']) - shut, 'open ticket')}: {on_board}; {len(g[SENT])} sent to the pipeline in "
            f"the last {SENT_DAYS} days{delivered}")


def auto_note(cfg: config.Config) -> str:
    if cfg.triage.auto:
        return (f"arete adds labels to up to {cfg.triage.auto_per_refresh} ready tickets per tracker each refresh; "
                "the existing Jira auto limit and sprint scope still apply; add labels yourself to approve sooner")
    if cfg.jira.auto:
        scope = " in the open sprint or left over from a closed one" if cfg.jira.sprint_only else ""
        return (f"arete adds the labels to up to {cfg.jira.auto_per_refresh} Jira tickets{scope} each refresh; add "
                "them yourself to approve sooner")
    return "add the labels to approve"


def board(connect=foundation.db) -> str:
    meta = util.read_json(META_FILE, {})
    v = view(connect=connect)
    rows = v["rows"]
    if not meta.get("refreshed") and not rows:
        return "no triage yet; run `arete triage --refresh`"
    lines = [f"Triage of {summary(v)}. Refreshed {stamp(meta.get('refreshed', '')) or 'never'}."]
    lines += [f"Not read this time: {e}" for e in meta.get("errors", [])]
    if meta.get("waiting"):
        lines.append(f"Waiting for the next refresh: {', '.join(meta['waiting'])}")
    lines += [f"Not approved this time: {ref}: {why}" for ref, why in (meta.get("held") or {}).items()]
    g = groups(v)
    width = max((len(r["ref"]) for r in rows + v["sent"]), default=0)
    repo_width = max((len(r["repo"] or "") for r in rows + v["sent"]), default=0)
    if g[READY]:
        lines += ["", f"Ready to build: {auto_note(config.load())}"]
        for r in g[READY]:
            lines.append(f"  {r['ref']:<{width}}  {r['size']}  {r['repo']:<{repo_width}}  "
                         f"{' '.join(labels_to_add(r)):<16}  "
                         f"{r['title']}{'' if in_sprint(r) else '  (outside the sprint)'}")
            lines += done_lines(r, "      ")
    if g[SENT]:
        lines += ["", f"Sent to the pipeline in the last {SENT_DAYS} days"]
        for a in g[SENT]:
            lines += [f"  {a['ref']:<{width}}  {a['size']}  {a['repo']:<{repo_width}}  {a['title']}",
                      f"      {sent_note(a)}", *done_lines(a, "      ")]
    if g[RESEARCH_WORK]:
        lines += ["", "Research: arete writes a report for each one"]
        for r in g[RESEARCH_WORK]:
            report = v["research"].get(r["ref"])
            lines += [f"  {r['ref']:<{width}}  {r['title']}", f"      {research_note(r, report)}"]
            if report and report["summary"]:
                lines.append(f"      {report['summary']}")
    if g[WITH_QUESTIONS]:
        lines += ["", "With questions"]
        for r in g[WITH_QUESTIONS]:
            lines.append(f"  {r['ref']:<{width}}  {r['size']}  {r['repo'] or '?':<{repo_width}}  {r['title']}")
            lines += [f"      - {q}" for q in r["questions"]] or [f"      {r['why']}"]
            lines += done_lines(r, "      ")
    if g[NEEDS_PERSON]:
        lines += ["", "Needs a person: nothing for arete to build or research"]
        for r in g[NEEDS_PERSON]:
            lines += [f"  {r['ref']:<{width}}  {r['title']}", f"      {r['why']}"]
    if g[NOT_TRIAGED]:
        lines += ["", "Not triaged"]
        lines += [f"  {r['ref']:<{width}}  {r['title']}  ({r['why']})" for r in g[NOT_TRIAGED]]
    lines += ["", "`arete triage REF` shows one ticket in full; `arete research REF` prints its report."]
    return "\n".join(lines)


def ticket(ref: str, connect=foundation.db) -> str | None:
    want = ref.strip().lower()
    r = next((r for r in triage_store.triage_rows(connect=connect) if r["ref"].lower() == want), None)
    if not r:
        a = (next((a for a in sent(connect=connect) if a["ref"].lower() == want), None)
             or triage_store.triage_approval(want, connect=connect))
        if not a:
            return None
        by = " by you" if principals.is_person(a["sent_by"]) else " by triage"
        lines = [f"{a['ref']}: {a['title']}", a["url"], f"Sent to the pipeline{by} {stamp(a['ts'])} for `{a['repo']}`."]
        if "run" in a:
            lines.append(f"Run: {a['run']['status']} {' '.join(sent_prs(a))}".rstrip() if a["run"] else "Run: none yet")
        lines += ["Assumed:"] + [f"  - {x}" for x in a["assumptions"]] if a["assumptions"] else []
        lines += done_lines(a)
        return "\n".join(x for x in lines if x)
    lines = [f"{r['ref']}: {r['title']}", r["url"], f"{r['source']}, updated {stamp(r['updated']) or 'unknown'}"]
    kind = standing(r)
    if kind == NOT_TRIAGED:
        return "\n".join(lines + [f"Not triaged: {r['why']}"])
    lines[-1] += f", triaged {stamp(r['ts'])} ({util.paid_note(r['cost'] or 0, 3)})"
    if kind == NEEDS_PERSON:
        return "\n".join(lines + ["Needs a person: nothing for arete to build or research.", f"Why: {r['why']}"])
    if kind == RESEARCH_WORK:
        report = insights_store.research_get(r["ref"])
        lines += [f"Research: {research_note(r, report)}.", f"Why: {r['why']}"]
        if report and report["summary"]:
            lines += [f"Summary: {report['summary']}", f"`arete research {r['ref']}` prints the report."]
        return "\n".join(lines)
    lines.append(f"Ready. Approve it: add {' and '.join(f'`{lb}`' for lb in labels_to_add(r))}." if kind == READY
                 else "Not ready.")
    lines += [f"{r['kind'].capitalize()}, size {r['size']}, repo {r['repo'] or ' and '.join(r['repos']) or 'not settled'}",
              f"Why: {r['why']}"]
    for heading, items in (("Questions", r["questions"]), ("Assumes", r["assumptions"])):
        if items:
            lines += [f"{heading}:"] + [f"  - {q}" for q in items]
    if r["plan"]:
        lines += ["Plan:"] + [f"  {n}. {step}" for n, step in enumerate(r["plan"], 1)]
    lines += done_lines(r)
    return "\n".join(lines)


def done_lines(r: dict, indent: str = "") -> list[str]:
    checks = r.get("checks") or []
    return [f"{indent}What done means:"] + [f"{indent}  {n}. {x}" for n, x in enumerate(checks, 1)] if checks else []


def issue_header(cfg: config.Config, changed: str) -> str:
    approving = (f" Automatic approval is on: {auto_note(cfg)}. Sent tickets appear under Sent to the pipeline."
                 if cfg.triage.auto or cfg.jira.auto else "")
    return (f"Last change {stamp(changed)}. Arete refreshes this every 15 minutes from your open tickets that have no "
            f"`{tracker.LABEL}` label, and edits it only when something changes.{approving} To approve a ready ticket "
            "yourself, add the labels it names. Research tickets get a report here for your review. `arete triage "
            "REF` on the Mac shows one ticket in full.")


def quiet(text: str) -> str:
    return quiet_prose(" ".join(text.split()))


def quiet_block(text: str) -> str:
    kept, at = [], 0
    for code in CODE_FENCE.finditer(text):
        kept += [quiet_prose(text[at:code.start()]), code.group()]
        at = code.end()
    return "".join(kept + [quiet_prose(text[at:])])


def quiet_prose(text: str) -> str:
    text = re.sub(r"(@|#|GH-|github\.com/)(?=\w)", "\\1\u200b", text, flags=re.IGNORECASE)
    text = re.sub(r"(?m)^#{1,3} ", "#### ", text)
    return re.sub(r"</?details", lambda m: m.group(0).replace("<", "&lt;"), text, flags=re.IGNORECASE)


def issue_link(r: dict) -> str:
    return f"[{r['ref']}]({r['url']})" if r["tracker"] == "jira" and r["url"] else f"`{r['ref']}`"


def fold(summary: str, body: str) -> str:
    return f"\n  <details><summary>{summary}</summary>\n\n{textwrap.indent(body, '  ')}\n\n  </details>\n"


def issue_block(r: dict, v: dict, kind: str, with_plan: bool) -> str:
    title = quiet(r["title"])
    if kind == SENT:
        return f"- {issue_link(r)} **{r['size']}** in `{r['repo']}`: {title}\n  {quiet(sent_note(r))}"
    if kind == RESEARCH_WORK:
        report = v["research"].get(r["ref"])
        lines = [f"- {issue_link(r)}: {title}", f"  {research_note(r, report, for_issue=True)}"]
        if report and report["summary"]:
            lines.append(f"  {quiet(report['summary'])}")
        if with_plan and report and report["report"]:
            lines.append(fold("Report", quiet_block(report["report"])))
        return "\n".join(lines)
    if kind in (NOT_TRIAGED, NEEDS_PERSON):
        return f"- {issue_link(r)}: {title} ({quiet(r['why'])})"
    lines = [f"- {issue_link(r)} **{r['size']}** {r['kind']} in `{r['repo'] or ' and '.join(r['repos']) or '?'}`: {title}"]
    if kind == READY:
        lines.append(f"  Add {' and '.join(f'`{lb}`' for lb in labels_to_add(r))}.")
    else:
        lines += [f"  - {quiet(q)}" for q in r["questions"]] or [f"  {quiet(r['why'])}"]
    if with_plan and r["assumptions"]:
        lines.append(fold("Assumes", "\n".join(f"- {quiet(x)}" for x in r["assumptions"])))
    if with_plan and r.get("checks"):
        lines.append(fold("What done means", "\n".join(f"{n}. {quiet(x)}" for n, x in enumerate(r["checks"], 1))))
    if with_plan and r["plan"]:
        lines.append(fold("Plan", "\n".join(f"{n}. {quiet(step)}" for n, step in enumerate(r["plan"], 1))))
    return "\n".join(lines)


def issue_sections(v: dict, with_plan: bool = True, limit: int | None = None) -> str:
    g = groups(v)
    total = sum(len(part) for part in g.values())
    shown = 0
    out = []
    for heading, part in g.items():
        blocks = [issue_block(r, v, heading, with_plan) for r in part]
        if limit is not None:
            blocks = blocks[:max(0, limit - shown)]
        shown += len(blocks)
        if blocks:
            out.append(f"## {heading} ({len(part)})\n\n" + "\n".join(blocks))
    if shown < total:
        out.append(f"({total - shown} more tickets left out to fit; `arete triage` on the Mac lists them all.)")
    return "\n\n".join(out) or "No open tickets without the `agent` label."


def fitted_sections(v: dict) -> str:
    text = issue_sections(v)
    if len(text) > MAX_ISSUE_CHARS:
        text = issue_sections(v, with_plan=False)
    keep = sum(len(part) for part in groups(v).values())
    while len(text) > MAX_ISSUE_CHARS and keep > 0:
        keep = keep * 9 // 10
        text = issue_sections(v, with_plan=False, limit=keep)
    return text


class BoardRefused(Exception):
    pass


@factory_control.work
def publish(cfg: config.Config, v: dict, meta: dict) -> str:
    from . import outbox
    from .store import outbox as outbox_store

    repo = cfg.alert_repo
    if not repo:
        return "local only: alert_repo is empty"
    private = gh.repo_private(repo)
    if private is None:
        raise BoardRefused(f"cannot read {repo}")
    if not private:
        raise BoardRefused(f"{repo} is public and the board is private")
    sections = fitted_sections(v)
    digest = hashlib.sha256(f"{issue_header(cfg, '')}\n\n{sections}".encode()).hexdigest()[:16]
    if meta.get("repo") != repo:
        meta.update(repo=repo, issue=None, digest="")
    changed = digest != meta.get("digest")
    if changed or not meta.get("changed"):
        meta["changed"] = util.iso()
    body = f"{issue_header(cfg, meta['changed'])}\n\n{sections}\n"
    number = meta.get("issue") or outbox_store.newest_sent_board_issue(repo)
    issue_state = gh.issue_state(repo, number) if number else ""
    if not issue_state:
        item_id = outbox.draft(
            target="github",
            kind="issue",
            object=f"github:{repo}",
            body=json.dumps({"title": ISSUE_TITLE, "body": body, "labels": [ISSUE_LABEL]}),
            drafted_by="board",
            slot=f"board:{repo}:create:{digest}",
            summary=f"create triage board issue on {repo}",
        )
        meta.update(issue=None, digest=digest)
        return f"drafted board issue for {repo} (OB-{item_id})"
    if not changed and issue_state != "CLOSED":
        meta.update(issue=number, digest=digest)
        return f"unchanged at {repo}#{number}"
    obj = f"github:{repo}#{number}"
    with foundation.tx() as c:
        item_id = outbox.draft(
            c,
            target="github",
            kind="issue",
            object=obj,
            body=json.dumps({"body": body}),
            drafted_by="board",
            slot=f"board:{repo}:{digest}",
            summary=f"update triage board {repo}#{number}",
        )
        said = f"drafted update for {repo}#{number} (OB-{item_id})"
        if issue_state == "CLOSED":
            outbox.draft(
                c,
                target="github",
                kind="status",
                object=obj,
                body=json.dumps({"state": "open"}),
                drafted_by="board",
                slot=f"board:{repo}:{digest}:reopen",
                summary=f"reopen triage board {repo}#{number}",
            )
            said += " and reopen"
    meta.update(issue=number, digest=digest)
    return said
