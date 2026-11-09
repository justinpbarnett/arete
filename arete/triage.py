from __future__ import annotations

import hashlib
import json
import re
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from . import alerts, config, gh, jev, jira, paths, runner, state, tracker, util
from .heartbeat import mark_loop, network_up_or_skip
from .pipeline import TRIAGE_ANSWERS, prompt

ISSUE_TITLE = "[arete] triage"
ISSUE_LABEL = "arete-triage"
SKIP_LABELS = {tracker.LABEL, alerts.LABEL, ISSUE_LABEL}
MAX_CALLS = 20
PER_SOURCE = 50
BUDGET_USD = 0.30
TIMEOUT_SECONDS = 300
MAX_PLAN_STEPS = 6
MAX_ISSUE_CHARS = 60000
META_FILE = paths.STATE_DIR / "triage.json"
TICKET_RECORDS_DIR = paths.STATE_DIR / "triage"
NO_MCP_SERVERS = json.dumps({"mcpServers": {}})
SEARCH_FIELDS = ["summary", "updated", "labels", "parent"]
SPRINT_JQL = "sprint in openSprints() OR (sprint in closedSprints() AND sprint not in futureSprints())"
JIRA_FIELDS = ["summary", "description", "status", "labels", "issuetype", "parent", "updated"]
MAX_ASSUMPTIONS = 8
THIN_WORDS = 6
# research reasons stored by older versions end in a CLI command, which the dashboard leaves out
OLD_RESEARCH_HINT = re.compile(r"; (check the ticket, then )?`arete research \S+ --run --force`( researches it anyway)?$")
OUTSIDE_SPRINT = "it is outside the sprint, so the research loop leaves it to you"
NEEDS_FORCE = ("held", "parent", "answered")
MARKUP = re.compile(r"\[[^\]]*\]|\{[^}]*\}|https?://\S+")
CODE_FENCE = re.compile(r"(?m)^ {0,3}(?:(`{3,})[^`\n]*\n(?:.*\n)*? {0,3}\1`*|(~{3,}).*\n(?:.*\n)*? {0,3}\2~*)[ \t]*$")
UNSPACED_SCRIPTS = r"\U00003040-\U000030ff\U00003400-\U00004dbf\U00004e00-\U00009fff\U0000f900-\U0000faff"
WORDS = re.compile(rf"[{UNSPACED_SCRIPTS}]{{2}}|[^\W\d_{UNSPACED_SCRIPTS}]{{2,}}")
SENT_DAYS = 7
SIZES = ("S", "M", "L")
FIX, FEATURE, RESEARCH, PERSON = "fix", "feature", "research", "person"
KINDS = (FIX, FEATURE, RESEARCH, PERSON)
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "ready": {"type": "boolean"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ASSUMPTIONS},
        "size": {"type": "string", "enum": list(SIZES)},
        "repos": {"type": "array", "items": {"type": "string"}},
        "plan": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_PLAN_STEPS},
        "why": {"type": "string"},
    },
    "required": ["kind", "ready", "questions", "assumptions", "size", "repos", "plan", "why"],
}
WAITING = f"waiting: more than {MAX_CALLS} tickets were due in one refresh"
ASKS_FOR_CODE = "Does this ticket ask for a change to code, configuration or documentation in a software repository?"
KIND_QUESTION = "What does this ticket ask for?"
KIND_OPTIONS = {
    RESEARCH: ("finding something out: investigate, compare options, estimate or recommend, where the answer is a "
               "written report"),
    PERSON: ("work only a person can do: a meeting, a decision, access or an account, a test or check done by hand, "
             "data entry, a purchase, or settings in an outside tool"),
    FIX: "a repair to software that is broken or wrong: a bug, an error, a crash, or wrong data shown",
    FEATURE: "a new or changed screen, field, behavior, report, setting or document in a software repository",
}
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


@dataclass
class TicketText:
    facts: str
    body: str
    comments: str
    updated: str = ""

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
            if tracker.LABEL not in (f.get("labels") or []):
                out.append(Candidate(i["key"], i["key"], (f.get("summary") or "").strip(), client.browse_url(i["key"]),
                                     f.get("updated") or "", parent=(f.get("parent") or {}).get("key") or ""))
        if self.jira_cfg.sprint_only:
            scoped = {i["key"] for i in client.search(f"{mine} AND ({SPRINT_JQL}) ORDER BY updated DESC", ["updated"],
                                                      limit=PER_SOURCE)}
            for c in out:
                c.in_sprint = c.ref in scoped
        return out

    def ticket(self, c: Candidate) -> TicketText:
        client = self.client
        f = client.issue(c.id, JIRA_FIELDS).get("fields") or {}
        facts = [f"Type: {(f.get('issuetype') or {}).get('name', '')}",
                 f"Status: {(f.get('status') or {}).get('name', '')}",
                 f"Labels: {', '.join(f.get('labels') or []) or 'none'}"]
        parent = f.get("parent") or {}
        if parent.get("key"):
            facts.append(f"Part of {parent['key']}: {(parent.get('fields') or {}).get('summary', '')}")
        comments = [((cm.get("author") or {}).get("displayName", "someone"), cm.get("created", ""), cm.get("body") or "")
                    for cm in client.comments(c.id)]
        return TicketText("\n".join(facts), (f.get("description") or "").strip(), tracker.comments_text(comments),
                          f.get("updated") or "")

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

    def comment(self, ref: str, text: str) -> None:
        self.client.comment(ref, text)

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
        for i in gh.issues(self.key, author=tk.owner, limit=PER_SOURCE):
            names = {lb["name"] for lb in i.get("labels", [])}
            if names & SKIP_LABELS or not tk.by_owner(i):
                continue
            out.append(Candidate(f"{tk.repo.name}#{i['number']}", str(i["number"]), i["title"].strip(),
                                 i.get("url", ""), i.get("updatedAt") or ""))
        return out

    def ticket(self, c: Candidate) -> TicketText:
        t = self.issues.get(c.id)
        return TicketText(f"Labels: {', '.join(t.labels) or 'none'}", (t.body or "").strip(), t.comments, t.updated)

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

    def comment(self, ref: str, text: str) -> None:
        gh.issue_comment(self.key, int(self.tid(ref)), text)

    def approve(self, r: dict) -> None:
        self.issues.approve(self.tid(r["ref"]))


TriageSource = JiraSource | GitHubSource


def sources(cfg: config.Config) -> list[TriageSource]:
    found: dict[tuple[str, str], TriageSource] = {}
    for r in cfg.repos:
        if not (r.pipeline and r.parts):
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


def not_code_by_jev(cfg: config.Config, src: TriageSource, c: Candidate, ticket: str) -> dict | None:
    if not jev.ready(cfg, list(src.repos)):
        return None
    reply = jev.ask(f"{c.title}\n\n{ticket}",
                    {"code": jev.yes_no(ASKS_FOR_CODE), "kind": jev.pick(KIND_QUESTION, KIND_OPTIONS)}, cfg,
                    trace=("", f"triage {c.ref} jev"))
    code, kind = reply.answers.get("code"), reply.answers.get("kind")
    if not code:
        return None
    sure = str(kind.value) if kind and kind.value in (RESEARCH, PERSON) and kind.sure(cfg.jev.confidence) else ""
    if code.yes <= cfg.jev.triage_skip:
        settled = RESEARCH if sure == RESEARCH else PERSON
        why = f"JEV reads it as asking for no change to a repo ({code.note()}, at or under {cfg.jev.triage_skip:g})"
    elif code.yes < 0.5 and sure:
        settled = sure
        why = f"JEV reads it as asking for no change to a repo ({code.note()})"
    else:
        return None
    why += f" and as {settled} work ({kind.note()})" if kind and kind.value == settled else ""
    cost = reply.cost_usd
    answer = {"kind": settled, "ready": False, "questions": [], "assumptions": [], "plan": [], "repos": [],
              "size": "", "why": why}
    TICKET_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    record_file(c.ref).write_text(f"{ticket}\n\n## Triage result ({util.paid_note(cost, 5)})\n\n```json\n"
                                  f"{json.dumps(answer, indent=2)}\n```\n")
    return settle(src, c, answer, cost)


def triage_one(cfg: config.Config, src: TriageSource, c: Candidate, ticket: str) -> tuple[dict | None, float, str]:
    text = prompt("triage", repos=src.repos_text(), ref=c.ref, title=c.title, ticket=ticket,
                  repo_rule=src.repo_rule())
    dirs = list(dict.fromkeys(r.path for r in src.repos if r.path.is_dir()))
    TICKET_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    res = runner.run(text, dirs[0] if dirs else TICKET_RECORDS_DIR, model=cfg.model("triage"), tools=runner.READ_TOOLS,
                     allowed_tools=runner.READ_TOOLS, mcp_config=NO_MCP_SERVERS, json_schema=SCHEMA,
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
               plan=lines_of(answer.get("plan"))[:MAX_PLAN_STEPS], why=why, cost=cost)


def row(src: TriageSource, c: Candidate, **fields) -> dict:
    base = {"ref": c.ref, "source": src.name, "tracker": src.tracker, "repo": "", "route": "", "title": c.title,
            "url": c.url, "updated": "", "ready": None, "code": None, "kind": "", "size": "", "repos": [],
            "questions": [], "assumptions": [], "plan": [], "why": "", "cost": 0.0, "ts": util.iso(),
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
    return {x["ref"]: x for x in state.research_rows() if (x.get("delivered") or "") >= since}


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


def refresh(cfg: config.Config, dry_run: bool) -> int:
    old = {r["ref"]: r for r in state.triage_rows()}
    srcs = sources(cfg)
    read: dict[str, list[Candidate]] = {}
    errors: list[str] = []
    for src in srcs:
        try:
            read[src.name] = read_candidates(src)
        except tracker.ERRORS as e:
            errors.append(f"{src.name}: {e}")
            print(f"{util.iso()} {errors[-1]}")
    todo = sorted(((src, c) for src in srcs for c in read.get(src.name, []) if due(c, old.get(c.ref))),
                  key=lambda sc: moment(sc[1].updated), reverse=True)
    if dry_run:
        preview(srcs, read, old, todo[:MAX_CALLS], todo[MAX_CALLS:])
        ready = ", ".join(r["ref"] for r in approvable(srcs, list(old.values()))) or "none"
        print(f"would approve up to {cfg.jira.auto_per_refresh} now ready: {ready}" if cfg.jira.auto
              else f"ready now, approved only with `[jira] auto = true`: {ready}")
        return 1 if errors else 0

    answered: dict[str, dict] = {}
    failed: dict[str, str] = {}
    waiting: list[str] = []
    spent = 0.0
    calls = 0
    for src, c in todo:
        if calls >= MAX_CALLS:
            waiting.append(c.ref)
            continue
        print(f"{util.iso()} {c.ref}: triaging", flush=True)
        try:
            ticket = src.ticket_text(c)
        except tracker.ERRORS as e:
            calls += 1
            failed[c.ref] = f"could not read it: {e}"
            print(f"{util.iso()} {c.ref}: triage failed: {failed[c.ref]}")
            continue
        if settled := not_code_by_jev(cfg, src, c, ticket):
            answered[c.ref] = settled
            spent += settled["cost"]
            print(f"{util.iso()} {c.ref}: {verdict(settled)} by JEV, no model call")
            continue
        calls += 1
        try:
            answer, cost, error = triage_one(cfg, src, c, ticket)
        except tracker.ERRORS as e:
            answer, cost, error = None, 0.0, f"could not read it: {e}"
        spent += cost
        if answer:
            answered[c.ref] = settle(src, c, answer, cost)
            print(f"{util.iso()} {c.ref}: {verdict(answered[c.ref])} ({util.paid_note(cost, 3)})")
        else:
            failed[c.ref] = error
            print(f"{util.iso()} {c.ref}: triage failed: {error}")
    with util.lock(BOARD_LOCK, wait=True):
        board = {r["ref"]: r for r in state.triage_rows()}
        sent = {a["ref"]: a["ts"] for a in state.triage_approvals()}
        delivered = delivered_since()
        for src in srcs:
            if src.name in read:
                state.triage_replace(src.name, [
                    answered.get(c.ref) or kept(src, c, board.get(c.ref),
                                                f"triage failed: {failed[c.ref]}" if c.ref in failed else WAITING)
                    for c in read[src.name] if not sent_since_read(c, sent)]
                    + still_shown(src, read[src.name], board, delivered))
        state.triage_prune([s.name for s in srcs])
    approved, held = approve_ready(cfg, srcs)
    keep = {record_file(r["ref"]).name for r in state.triage_rows()}
    for f in TICKET_RECORDS_DIR.glob("*.md"):
        if f.name not in keep:
            f.unlink()

    meta = util.read_json(META_FILE, {})
    meta.update(refreshed=util.iso(), errors=errors, waiting=waiting, failed=failed, approved=approved, held=held)
    problems = list(errors)
    try:
        posted = publish(cfg, view(), meta)
    except (gh.GhError, BoardRefused) as e:
        problems.append(f"posting the board: {e}")
        posted = f"not posted: {e}"
    meta.update(posted=posted)
    util.write_json(META_FILE, meta)
    print(f"{util.iso()} triage: {util.count(calls, 'model call')} ({util.paid_note(spent, 3)}), "
          f"{len(failed)} failed, {len(waiting)} waiting, {len(approved)} approved; board {posted}")
    if waiting:
        print(f"waiting for the next refresh: {', '.join(waiting)}")
    mark_loop("triage", not problems, "; ".join(problems))
    return 1 if problems else 0


def approvable(srcs: list[TriageSource], rows: list[dict]) -> list[dict]:
    jira_sources = {s.name for s in srcs if isinstance(s, JiraSource)}
    ready = [r for r in rows if r["source"] in jira_sources and standing(r) == READY and r["route"] and in_sprint(r)]
    return sorted(ready, key=lambda r: moment(r["updated"]), reverse=True)


def approve_ready(cfg: config.Config, srcs: list[TriageSource]) -> tuple[list[str], dict[str, str]]:
    approved: list[str] = []
    held: dict[str, str] = {}
    if not cfg.jira.auto:
        return approved, held
    by_name = {s.name: s for s in srcs}
    for r in approvable(srcs, state.triage_rows()):
        if len(approved) >= cfg.jira.auto_per_refresh:
            break
        with util.lock(BOARD_LOCK, wait=True):
            if board_row(r["ref"]) != r:
                continue
            try:
                why = approve(cfg, by_name[r["source"]], r)
            except tracker.ERRORS as e:
                why = f"could not approve it: {e}"
        if why:
            held[r["ref"]] = why
            print(f"{util.iso()} {r['ref']}: not approved: {why}")
        else:
            approved.append(r["ref"])
            print(f"{util.iso()} {r['ref']}: approved, added {' '.join(labels_to_add(r))}")
    return approved, held


def approve(cfg: config.Config, src: JiraSource, r: dict) -> str:
    repo = src.by_name().get(r["repo"])
    if not repo:
        return f"{r['repo'] or 'no repo'} is not a pipeline repo of {src.name}"
    if not jev.ready(cfg, [repo]):
        return "JEV is not on for it, so nothing screens the ticket; add the labels by hand"
    now = src.ticket(src.candidate(r))
    if now.updated != r["updated"]:
        return "it changed since triage, so the next refresh triages it again"
    screened = screen(cfg, r, now, "injection")
    if screened is None:
        return "JEV could not screen it this time"
    if screened.yes >= cfg.jev.injection:
        labels = " and ".join(f"`{lb}`" for lb in labels_to_add(r))
        state.triage_put({**r, "ready": 0, "questions": [
            (f"JEV reads text in it as instructions to an agent (p {screened.yes:.2f}). Check the ticket, then add "
             f"{labels} by hand to approve it.")]})
        return f"held for the injection screen (p {screened.yes:.2f})"
    to_pipeline(src, r, "triage")
    return ""


def screen(cfg: config.Config, r: dict, now: TicketText, step: str) -> jev.Answer | None:
    return jev.injection(f"{r['title']}\n\n{now.text}", cfg, trace=("", f"triage {r['ref']} {step} jev"))


def to_pipeline(src: TriageSource, r: dict, sent_by: str) -> None:
    src.approve(r)
    state.triage_approve(r, sent_by)
    state.triage_drop(r["ref"])


class NotOnBoard(LookupError):
    pass


class NotSendable(RuntimeError):
    pass


class BadAnswers(ValueError):
    pass


class TrackerFailed(RuntimeError):
    pass


def tracker_said(e: Exception) -> str:
    return util.first_line(str(e), 200).rstrip(".")


def board_row(ref: str) -> dict | None:
    return next((r for r in state.triage_rows() if r["ref"].lower() == ref.strip().lower()), None)


def repo_question(r: dict) -> str:
    return "" if r["repo"] else next((q for q in r["questions"] if q.startswith((WHICH_REPO, SEVERAL_REPOS))), "")


def repo_choices(src: TriageSource, r: dict) -> list[str]:
    names = src.by_name()
    return [n for n in ([r["repo"]] if r["repo"] else r["repos"] or names) if n in names]


def answers_comment(pairs: list[tuple[str, str]]) -> str:
    return f"{TRIAGE_ANSWERS}:\n\n" + "\n\n".join(f"**{q}**\n{a}" for q, a in pairs)


def send(cfg: config.Config, ref: str, questions: list[str], answers: list[str], repo_name: str) -> str:
    with util.lock(BOARD_LOCK, wait=True):
        r = board_row(ref)
        if not r:
            if a := state.triage_approval(ref.strip()):
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
        src = next((s for s in sources(cfg) if s.name == r["source"]), None)
        if not src:
            raise NotSendable(f"{r['source']} is no longer a triage source, so the next refresh drops {r['ref']}.")
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
        labels = " and ".join(labels_to_add(sent_row))
        try:
            now = src.ticket(src.candidate(r))
        except tracker.ERRORS as e:
            raise TrackerFailed(f"Could not read {r['ref']} ({tracker_said(e)}).") from e
        if now.updated != r["updated"]:
            raise NotSendable(f"{r['ref']} changed since triage read it, so the next refresh triages it again.")
        if jev.ready(cfg, [repo]):
            screened = screen(cfg, r, now, "send injection")
            if screened is None:
                raise NotSendable(f"JEV could not screen {r['ref']} for injected instructions this time. Try again.")
            if screened.yes >= cfg.jev.injection:
                raise NotSendable(f"JEV reads text in {r['ref']} as instructions to an agent (p {screened.yes:.2f}). "
                                  f"Read the ticket, then add {labels} by hand to send it.")
        pairs = [(q, a.strip()) for q, a in zip(questions, answers, strict=True) if q != repo_question(r) and a.strip()]
        if pairs:
            try:
                src.comment(r["ref"], answers_comment(pairs))
            except tracker.ERRORS as e:
                raise TrackerFailed(f"Could not post your answers to {r['ref']} ({tracker_said(e)}).") from e
        try:
            to_pipeline(src, sent_row, "owner")
        except tracker.ERRORS as e:
            if pairs:
                raise TrackerFailed(f"Posted your answers, but could not add {labels} to {r['ref']} ({tracker_said(e)}). "
                                    "Add them by hand, or the next refresh triages it again with your answers.") from e
            raise TrackerFailed(f"Could not add {labels} to {r['ref']} ({tracker_said(e)}).") from e
    done = f"posted your answers and added {labels}" if pairs else f"added {labels}"
    return f"Sent {r['ref']} to the pipeline for {repo.name}: {done}."


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


def sent(connect=state.db) -> list[dict]:
    out = []
    for a in state.triage_approvals(util.iso(util.now() - timedelta(days=SENT_DAYS)), connect):
        runs = [r for r in state.ticket_runs(a["repo"], issue_id(a["tracker"], a["ref"]), limit=1, connect=connect)
                if r["started"] >= a["ts"]]
        out.append({**a, "run": runs[0] if runs else None})
    return out


def view(connect=state.db) -> dict:
    return {"rows": sorted(state.triage_rows(connect), key=lambda r: natural(r["ref"])), "sent": sent(connect),
            "research": {r["ref"]: r for r in state.research_rows(connect)}}


def groups(v: dict) -> dict[str, list[dict]]:
    out = {s: [r for r in v["rows"] if standing(r) == s] for s in STANDINGS}
    out[SENT] = v["sent"]
    return out


def labels_to_add(r: dict) -> list[str]:
    return [tracker.LABEL, r["route"]] if r["route"] else [tracker.LABEL]


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
    why = OLD_RESEARCH_HINT.sub("", report["error"] or "")
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
    by_you = a["sent_by"] == "owner"
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
    if cfg.jira.auto:
        scope = " in the open sprint or left over from a closed one" if cfg.jira.sprint_only else ""
        return (f"arete adds the labels to up to {cfg.jira.auto_per_refresh} Jira tickets{scope} each refresh; add "
                "them yourself to approve sooner")
    return "add the labels to approve"


def board() -> str:
    meta = util.read_json(META_FILE, {})
    v = view()
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
        lines += [f"  {r['ref']:<{width}}  {r['size']}  {r['repo']:<{repo_width}}  {' '.join(labels_to_add(r)):<16}  "
                  f"{r['title']}{'' if in_sprint(r) else '  (outside the sprint)'}" for r in g[READY]]
    if g[SENT]:
        lines += ["", f"Sent to the pipeline in the last {SENT_DAYS} days"]
        for a in g[SENT]:
            lines += [f"  {a['ref']:<{width}}  {a['size']}  {a['repo']:<{repo_width}}  {a['title']}",
                      f"      {sent_note(a)}"]
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
    if g[NEEDS_PERSON]:
        lines += ["", "Needs a person: nothing for arete to build or research"]
        for r in g[NEEDS_PERSON]:
            lines += [f"  {r['ref']:<{width}}  {r['title']}", f"      {r['why']}"]
    if g[NOT_TRIAGED]:
        lines += ["", "Not triaged"]
        lines += [f"  {r['ref']:<{width}}  {r['title']}  ({r['why']})" for r in g[NOT_TRIAGED]]
    lines += ["", "`arete triage REF` shows one ticket in full; `arete research REF` prints its report."]
    return "\n".join(lines)


def ticket(ref: str) -> str | None:
    want = ref.strip().lower()
    r = next((r for r in state.triage_rows() if r["ref"].lower() == want), None)
    if not r:
        a = next((a for a in sent() if a["ref"].lower() == want), None) or state.triage_approval(want)
        if not a:
            return None
        by = " by you" if a["sent_by"] == "owner" else " by triage"
        lines = [f"{a['ref']}: {a['title']}", a["url"], f"Sent to the pipeline{by} {stamp(a['ts'])} for `{a['repo']}`."]
        if "run" in a:
            lines.append(f"Run: {a['run']['status']} {' '.join(sent_prs(a))}".rstrip() if a["run"] else "Run: none yet")
        lines += ["Assumed:"] + [f"  - {x}" for x in a["assumptions"]] if a["assumptions"] else []
        return "\n".join(x for x in lines if x)
    lines = [f"{r['ref']}: {r['title']}", r["url"], f"{r['source']}, updated {stamp(r['updated']) or 'unknown'}"]
    kind = standing(r)
    if kind == NOT_TRIAGED:
        return "\n".join(lines + [f"Not triaged: {r['why']}"])
    lines[-1] += f", triaged {stamp(r['ts'])} ({util.paid_note(r['cost'] or 0, 3)})"
    if kind == NEEDS_PERSON:
        return "\n".join(lines + ["Needs a person: nothing for arete to build or research.", f"Why: {r['why']}"])
    if kind == RESEARCH_WORK:
        report = state.research_get(r["ref"])
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
    return "\n".join(lines)


def issue_header(cfg: config.Config, changed: str) -> str:
    approving = (f" Arete adds the labels to up to {cfg.jira.auto_per_refresh} ready Jira tickets each refresh and lists "
                 "them under Sent to the pipeline." if cfg.jira.auto else "")
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


def publish(cfg: config.Config, v: dict, meta: dict) -> str:
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
    number = meta.get("issue") or find_issue(repo)
    issue_state = gh.issue_state(repo, number) if number else ""
    if not issue_state:
        gh.ensure_label(repo, ISSUE_LABEL, "c5def5", "Arete triage board")
        number = gh.issue_create(repo, ISSUE_TITLE, body, [ISSUE_LABEL])
        said = f"opened {repo}#{number}"
    elif not changed:
        said = f"unchanged at {repo}#{number}"
    else:
        gh.issue_edit_body(repo, number, body)
        said = f"updated {repo}#{number}"
        if issue_state == "CLOSED":
            gh.issue_reopen(repo, number)
            said += " and reopened it"
    meta.update(issue=number, digest=digest)
    return said


def find_issue(repo: str) -> int | None:
    found = [i for i in gh.issues(repo, ISSUE_LABEL, state="all", limit=10) if i["title"] == ISSUE_TITLE]
    return int(found[0]["number"]) if found else None
