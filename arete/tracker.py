from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import config, gh, jira, state

LABEL = "agent"
APPROVAL_LABEL_STYLE = ("0052cc", "Approved for arete to build")
STATE_COLORS = {"running": "fbca04", "pr": "0e8a16", "failed": "d93f0b", "needs-info": "c5def5"}
FIX_LABELS = {"bug", "fix"}
FIX_TYPES = {"bug", "fire", "incident", "problem"}
JIRA_FIELDS = ["summary", "description", "status", "labels", "issuetype", "created", "updated", "creator", "reporter",
               "assignee", "parent", "attachment"]
JIRA_STATE_LABELS = {f"agent-{s}" for s in config.STATES}
JIRA_LABEL_ARETE_LEAVES = "agent-running"
SEARCH_FIELDS = ["summary", "created", "labels"]
MAX_AGENT_ISSUES = 1000
MAX_COMMENTS = 10
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ARETE_TAG = "[arete]"
TASK_ID = re.compile(r"t-[0-9a-f]{4,}", re.IGNORECASE)


class TaskError(LookupError):
    pass


ERRORS = (gh.GhError, jira.JiraError, TaskError)


@dataclass
class Image:
    id: str
    name: str
    size: int


@dataclass
class SavedImages:
    paths: list[Path] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)


@dataclass
class Moved:
    error: str = ""
    before: str = ""
    after: str = ""
    moved: bool = False


@dataclass
class Ticket:
    id: str
    ref: str
    title: str
    body: str = ""
    comments: str = ""
    open: bool = True
    kind: Literal["feat", "fix"] = "feat"
    url: str = ""
    created: str = ""
    updated: str = ""
    labels: list[str] = field(default_factory=list)
    blocker: str = ""
    images: list[Image] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.title = self.title.strip()


def is_task_id(tid) -> bool:
    return bool(TASK_ID.fullmatch(str(tid or "").strip()))


def ref_for(issue) -> str:
    s = str(issue or "")
    return s if jira.KEY_RE.match(s) or is_task_id(s) else f"#{s}"


def ticket_name(repo: str, ref: str) -> str:
    return f"{repo}{ref}" if ref.startswith("#") else f"{repo} {ref}"


def run_ticket_name(run: dict) -> str:
    return ticket_name(run["repo"] or "", ref_for(run["issue"]))


def with_ref(subject: str, ref: str) -> str:
    return subject if re.search(rf"(?<![\w-]){re.escape(ref)}(?!\w)", subject) else f"{subject} ({ref})"


def error_of(call: Callable[[], object]) -> str:
    try:
        call()
    except ERRORS as e:
        return str(e)
    return ""


_clients: dict[str, jira.Client] = {}


def for_repo(cfg: config.Config, repo: config.Repo) -> Tracker:
    if repo.tracker == "local":
        return LocalTracker(repo)
    if repo.tracker == "jira":
        return JiraTracker(repo, cfg.jira)
    return GitHubTracker(repo)


def for_ticket(cfg: config.Config, repo: config.Repo, tid: str | int) -> Tracker:
    return LocalTracker(repo) if is_task_id(tid) else for_repo(cfg, repo)


class GitHubTracker:
    name = "github"
    pr_labels = ("arete",)

    def __init__(self, repo: config.Repo):
        self.repo = repo
        self.owner = repo.github.split("/")[0]
        self._bodies: dict[str, str] = {}
        self._open: dict[int, bool | None] = {}

    @property
    def queue_hint(self) -> str:
        return f"issues labeled `{LABEL}`"

    def label_name(self, state: str) -> str:
        return f"agent:{state}"

    def normalize(self, tid: str | int) -> str:
        s = str(tid).strip().lstrip("#")
        if not s.isdigit():
            raise ValueError(f"'{tid}' is not an issue number")
        return str(int(s))

    def ref(self, tid: str) -> str:
        return f"#{tid}"

    def todo(self) -> list[Ticket]:
        skip = {self.label_name(s) for s in config.STATES}
        out = []
        self._bodies, self._open = {}, {}
        for i in gh.issues(self.repo.github, LABEL, limit=MAX_AGENT_ISSUES):
            self._bodies[str(i["number"])] = i.get("body") or ""
            self._open[int(i["number"])] = True
            names = {lb["name"] for lb in i.get("labels", [])}
            if names & skip or not self.by_owner(i):
                continue
            out.append(Ticket(str(i["number"]), f"#{i['number']}", i["title"], url=i.get("url", ""),
                              created=i.get("createdAt", ""), labels=sorted(names)))
        return sorted(out, key=lambda t: t.created)

    def by_owner(self, item: dict) -> bool:
        login = (item.get("author") or {}).get("login") or ""
        return bool(login) and login.lower() == self.owner.lower()

    def blocker(self, tid: str) -> str:
        body = self._bodies.get(tid)
        if body is None:
            body = (gh.issue(self.repo.github, int(tid)) or {}).get("body") or ""
        return wait_blocker(waits_on(body), self.is_open)

    def is_open(self, number: int) -> bool | None:
        if number not in self._open:
            try:
                state = (gh.issue(self.repo.github, number) or {}).get("state")
            except gh.GhError:
                state = None
            self._open[number] = True if state == "OPEN" else False if state in ("CLOSED", "MERGED") else None
        return self._open[number]

    def replied(self) -> list[Ticket]:
        out = []
        for i in gh.issues(self.repo.github, self.label_name("needs-info")):
            if LABEL not in {lb["name"] for lb in i.get("labels", [])} or not self.by_owner(i):
                continue
            full = gh.issue(self.repo.github, int(i["number"]))
            self._bodies[str(i["number"])] = full.get("body") or ""
            mine = [(datetime.fromisoformat(c["createdAt"]), c.get("body") or "") for c in full.get("comments", [])
                    if self.by_owner(c) and c.get("createdAt")]
            if answered(mine):
                out.append(Ticket(str(i["number"]), f"#{i['number']}", i["title"], url=i.get("url", ""),
                                  created=i.get("createdAt", "")))
        return sorted(out, key=lambda t: t.created)

    def save_images(self, t: Ticket, dest: Path) -> SavedImages:
        return SavedImages()

    def get(self, tid: str) -> Ticket:
        i = gh.issue(self.repo.github, int(tid))
        labels = [lb["name"] for lb in i.get("labels", [])]
        author = (i.get("author") or {}).get("login", "")
        comments = [(self.owner, c.get("createdAt", ""), c.get("body", "")) for c in i.get("comments", [])
                    if self.by_owner(c)]
        blocker = ("" if self.by_owner(i) else f"it was opened by {author or 'someone else'}, not {self.owner}") or (
            "" if LABEL in labels else f"it has no `{LABEL}` label") or wait_blocker(
            waits_on(i.get("body") or ""), self.is_open)
        return Ticket(str(i["number"]), f"#{i['number']}", i["title"], body=i.get("body") or "(no description)",
                      comments=comments_text(comments), open=i.get("state") == "OPEN",
                      kind="fix" if FIX_LABELS & set(labels) else "feat", url=i.get("url", ""),
                      updated=i.get("updatedAt") or "", labels=labels, blocker=blocker)

    def prepare(self) -> list[str]:
        made = [gh.ensure_label(self.repo.github, LABEL, *APPROVAL_LABEL_STYLE),
                gh.ensure_label(self.repo.github, "arete", "5319e7", "Arete pipeline state")]
        made += [gh.ensure_label(self.repo.github, self.label_name(s), STATE_COLORS[s], "Arete pipeline state")
                 for s in config.STATES]
        return [err for err in made if err]

    def approve(self, tid: str) -> None:
        if failed := gh.ensure_label(self.repo.github, LABEL, *APPROVAL_LABEL_STYLE, restyle=False):
            raise gh.GhError(failed)
        gh.issue_labels(self.repo.github, int(tid), add=[LABEL])

    def label(self, tid: str, add=(), remove=()) -> str:
        return error_of(lambda: gh.issue_labels(self.repo.github, int(tid), add=[self.label_name(s) for s in add],
                                                remove=[self.label_name(s) for s in remove]))

    def comment(self, tid: str, body: str) -> str:
        return error_of(lambda: gh.issue_comment(self.repo.github, int(tid), body))

    def move(self, tid: str, state: str) -> str:
        return ""

    def pr_title(self, subject: str, t: Ticket) -> str:
        return subject

    def pr_link(self, t: Ticket, whole: bool) -> str:
        return f"Closes #{t.id}" if whole else f"Part of #{t.id}"


class JiraTracker:
    name = "jira"
    pr_labels: tuple[str, ...] = ()

    def __init__(self, repo: config.Repo, jcfg: config.JiraConfig):
        self.repo = repo
        self.cfg = jcfg
        self.project = repo.jira_project or jcfg.project
        self.route = route_label(repo)

    @property
    def client(self) -> jira.Client:
        key = str(self.cfg.env_file or "")
        if key not in _clients:
            _clients[key] = jira.Client.from_env(self.cfg.env_file)
        return _clients[key]

    @property
    def queue_hint(self) -> str:
        return f"tickets labeled `{LABEL}` and `{self.route}`, assigned to you"

    def label_name(self, state: str) -> str:
        return f"agent-{state}"

    def normalize(self, tid: str | int) -> str:
        key = str(tid).strip().upper()
        if not jira.KEY_RE.match(key):
            raise ValueError(f"'{tid}' is not a Jira key like {self.project or 'ABC'}-1234")
        return key

    def ref(self, tid: str) -> str:
        return tid

    def todo(self) -> list[Ticket]:
        if not self.project:
            raise jira.JiraError(f"{self.repo.name}: no Jira project; set [jira] project or jira_project on the repo")
        jql = candidates_jql(self.project, f'labels = "{self.route}"')
        return [search_ticket(self.client, i) for i in self.client.search(jql, SEARCH_FIELDS, limit=20)]

    def blocker(self, tid: str) -> str:
        issue = self.client.issue(tid, JIRA_FIELDS)
        return check_approval(self.client.me().get("accountId", ""), issue, self.client.changelog(tid), self.route)[0]

    def replied(self) -> list[Ticket]:
        jql = candidates_jql(self.project, f'labels = "{self.route}" AND labels = "{self.label_name("needs-info")}"',
                             skip_states=False)
        me = self.client.me().get("accountId", "")
        out = []
        for i in self.client.search(jql, SEARCH_FIELDS, limit=20):
            mine = [(jira.parse_time(c["created"]), c.get("body") or "") for c in self.client.comments(i["key"])
                    if _authored_by(c, me) and c.get("created")]
            if answered(mine):
                out.append(search_ticket(self.client, i))
        return out

    def get(self, tid: str) -> Ticket:
        issue = self.client.issue(tid, JIRA_FIELDS)
        me = self.client.me().get("accountId", "")
        blocker, approved_at = check_approval(me, issue, self.client.changelog(tid), self.route)
        f = issue.get("fields") or {}
        labels = list(f.get("labels") or [])
        body = f.get("description") or "(no description)"
        parent = f.get("parent") or {}
        if parent.get("key"):
            body = f"Part of {parent['key']}: {(parent.get('fields') or {}).get('summary', '')}\n\n{body}"
        itype = (f.get("issuetype") or {}).get("name", "").lower()
        status = f.get("status") or {}
        comments = [((c.get("author") or {}).get("displayName", "someone"), c.get("created", ""), c.get("body") or "")
                    for c in trusted(me, approved_at, self.client.comments(tid))]
        images = [Image(str(a["id"]), a.get("filename") or "image", int(a.get("size") or 0))
                  for a in trusted(me, approved_at, f.get("attachment") or [])
                  if (a.get("mimeType") or "").startswith("image/")]
        return Ticket(tid, tid, f.get("summary", ""), body=body, comments=comments_text(comments),
                      open=((status.get("statusCategory") or {}).get("key") != "done"),
                      kind="fix" if itype in FIX_TYPES or FIX_LABELS & set(labels) else "feat",
                      url=self.client.browse_url(tid), created=f.get("created", ""), updated=f.get("updated") or "",
                      labels=labels, blocker=blocker, images=images)

    def save_images(self, t: Ticket, dest: Path) -> SavedImages:
        out = SavedImages()
        for img in t.images:
            if len(out.paths) >= MAX_IMAGES or img.size > MAX_IMAGE_BYTES:
                out.left_out.append(img.name)
                continue
            dest.mkdir(parents=True, exist_ok=True)
            path = dest / f"{img.id}-{safe_filename(img.name)}"
            path.write_bytes(self.client.attachment(img.id))
            out.paths.append(path)
        return out

    def prepare(self) -> list[str]:
        return []

    def label(self, tid: str, add=(), remove=()) -> str:
        return error_of(lambda: self.client.edit_labels(tid, [self.label_name(s) for s in add],
                                                        [self.label_name(s) for s in remove]))

    def comment(self, tid: str, body: str) -> str:
        return error_of(lambda: self.client.comment(tid, body))

    def move(self, tid: str, state: str) -> str:
        try:
            return self.move_to(tid, self.cfg.transitions.get(state, ""))
        except jira.JiraError as e:
            return str(e)

    def move_to(self, tid: str, target: str) -> str:
        return self.shift(tid, target).error

    def shift(self, tid: str, target: str, stamps: bool = False, keep_done: bool = False) -> Moved:
        if not target:
            return Moved()
        f = self.client.issue(tid, ["status", "updated"]).get("fields") or {}
        status = (f.get("status") or {}).get("name", "")
        done = ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done"
        if status.lower() == target.lower() or keep_done and done:
            return Moved(before=f.get("updated") or "")
        if not self.client.transition(tid, target, by_name=not self.is_status(tid, target)):
            return Moved(f"{tid} has no transition to '{target}' from '{status}'")
        after = ""
        if stamps:
            after = (self.client.issue(tid, ["updated"]).get("fields") or {}).get("updated") or ""
        return Moved(before=f.get("updated") or "", after=after, moved=True)

    def is_status(self, tid: str, name: str) -> bool:
        try:
            return name.lower() in {s.lower() for s in self.client.statuses(tid.rsplit("-", 1)[0])}
        except jira.JiraError:
            return False

    def pr_title(self, subject: str, t: Ticket) -> str:
        return with_ref(subject, t.ref)

    def pr_link(self, t: Ticket, whole: bool) -> str:
        return f"Jira: [{t.ref}]({t.url})"


class LocalTracker:
    name = "local"
    pr_labels: tuple[str, ...] = ()

    def __init__(self, repo: config.Repo):
        self.repo = repo

    @property
    def queue_hint(self) -> str:
        return "local tasks, with `arete task add`"

    def label_name(self, state: str) -> str:
        return state

    def normalize(self, tid: str | int) -> str:
        s = str(tid).strip().lower()
        if not is_task_id(s):
            raise ValueError(f"'{tid}' is not a task id like t-1a2b")
        return s

    def ref(self, tid: str) -> str:
        return tid

    def todo(self) -> list[Ticket]:
        return [task_ticket(row) for row in state.tasks(self.repo.name, "todo")]

    def by_owner(self, item: dict) -> bool:
        return True

    def blocker(self, tid: str) -> str:
        if (row := state.task_get(tid)) is None:
            return f"there is no task {tid}"
        return task_wait_blocker(waits_on_tasks(row["body"]), task_state)

    def replied(self) -> list[Ticket]:
        return [task_ticket(row) for row in state.tasks_replied(self.repo.name)]

    def save_images(self, t: Ticket, dest: Path) -> SavedImages:
        return SavedImages()

    def get(self, tid: str) -> Ticket:
        if (row := state.task_get(tid)) is None:
            raise TaskError(f"no task {tid}")
        comments = [("Arete" if c["author"] == "arete" else "Owner", c["ts"] or "", c["body"] or "")
                    for c in state.task_comments(tid)]
        blocker = (f"it is a task of {row['repo']}, not {self.repo.name}" if row["repo"] != self.repo.name
                   else task_wait_blocker(waits_on_tasks(row["body"]), task_state))
        return task_ticket(row, comments_text(comments), blocker)

    def prepare(self) -> list[str]:
        return []

    def approve(self, tid: str) -> None:
        if not state.task_set_state(tid, "todo"):
            raise TaskError(f"no task {tid}")

    def label(self, tid: str, add=(), remove=()) -> str:
        if state.task_get(tid) is None:
            return f"no task {tid}"
        if add:
            if add[0] not in config.TASK_STATES:
                return f"'{add[0]}' is not a task state; use {', '.join(config.TASK_STATES)}"
            state.task_set_state(tid, add[0])
        elif remove:
            state.task_set_state(tid, "todo", from_states=tuple(remove))
        return ""

    def comment(self, tid: str, body: str) -> str:
        return "" if state.task_comment(tid, "arete", body) else f"no task {tid}"

    def move(self, tid: str, state: str) -> str:
        return ""

    def pr_title(self, subject: str, t: Ticket) -> str:
        return with_ref(subject, t.ref)

    def pr_link(self, t: Ticket, whole: bool) -> str:
        return ""


Tracker = GitHubTracker | JiraTracker | LocalTracker


def task_state(tid: str) -> str | None:
    return (state.task_get(tid) or {}).get("state")


def task_ticket(row: dict, comments: str = "", blocker: str = "") -> Ticket:
    return Ticket(row["id"], row["id"], row["title"] or "", body=row["body"] or "(no description)",
                  comments=comments, open=row["state"] not in config.TASK_FINISHED,
                  kind=row["kind"] if row["kind"] in config.KINDS else "feat", created=row["created"] or "",
                  updated=row["updated"] or "", blocker=blocker)


def route_label(repo: config.Repo) -> str:
    if repo.jira_label:
        return repo.jira_label
    return "repo-" + (repo.github.split("/")[-1] if repo.github else repo.name)


def candidates_jql(project: str, extra: str = "", skip_states: bool = True) -> str:
    skip = ", ".join(f'"{s}"' for s in sorted(JIRA_STATE_LABELS))
    parts = [f'project = "{project}"', f'labels = "{LABEL}"', "assignee = currentUser()", "statusCategory != Done"]
    if skip_states:
        parts.append(f"labels not in ({skip})")
    if extra:
        parts.append(extra)
    return " AND ".join(parts) + " ORDER BY created ASC"


def _tokens(s: str | None) -> set[str]:
    return set((s or "").split())


def _label_items(h: dict) -> list[dict]:
    return [it for it in h.get("items", []) if it.get("field") == "labels"]


def is_arete_edit(h: dict) -> bool:
    items = _label_items(h)
    if not items or len(items) != len(h.get("items", [])):
        return False
    added: set[str] = set()
    removed: set[str] = set()
    for it in items:
        before, after = _tokens(it.get("fromString")), _tokens(it.get("toString"))
        added |= after - before
        removed |= before - after
    if not added:
        return removed == {JIRA_LABEL_ARETE_LEAVES}
    return (added | removed) <= JIRA_STATE_LABELS


def _authored_by(item: dict, account_id: str) -> bool:
    return (item.get("author") or {}).get("accountId") == account_id


def check_approval(me: str, issue: dict, history: list[dict], route: str) -> tuple[str, datetime | None]:
    f = issue.get("fields") or {}
    for needed in (LABEL, route):
        if needed not in (f.get("labels") or []):
            return f"it has no `{needed}` label", None
    assignee = f.get("assignee") or {}
    if assignee.get("accountId") != me:
        return (f"it is assigned to {assignee['displayName']}, not you" if assignee.get("displayName")
                else "it is not assigned to you"), None
    created = jira.parse_time(f["created"]) if f.get("created") else None
    creator = f.get("creator") or f.get("reporter") or {}
    history = sorted(history, key=lambda h: jira.parse_time(h["created"]))

    label_adds = [h for h in history for it in _label_items(h)
                  if LABEL in _tokens(it.get("toString")) and LABEL not in _tokens(it.get("fromString"))]
    adder = (label_adds[-1].get("author") or {}) if label_adds else creator
    if adder.get("accountId") != me:
        return f"`{LABEL}` was added by {adder.get('displayName') or 'someone else'}, not you", None

    my_approvals = [jira.parse_time(h["created"]) for h in history
                    if _authored_by(h, me) and _label_items(h) and not is_arete_edit(h)]
    if creator.get("accountId") == me and created:
        my_approvals.append(created)
    approved_at = max(my_approvals) if my_approvals else created
    for h in history:
        changed = [it for it in h.get("items", []) if it.get("field") in ("summary", "description")]
        if approved_at and jira.parse_time(h["created"]) > approved_at and not _authored_by(h, me) and changed:
            who = (h.get("author") or {}).get("displayName") or "someone else"
            return (f"{who} changed the {changed[0]['field']} after you approved it; "
                    f"touch its labels to approve it again"), approved_at
    return "", approved_at


def trusted(me: str, approved_at: datetime | None, items: list[dict]) -> list[dict]:
    out = []
    for c in items:
        if _authored_by(c, me) or approved_at and jira.parse_time(c.get("updated") or c["created"]) <= approved_at:
            out.append(c)
    return out


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name).strip(".-") or "image"


def from_arete(body: str) -> bool:
    return body.lstrip().startswith(ARETE_TAG)


def answered(comments: list[tuple[datetime, str]]) -> bool:
    asked = [when for when, body in comments if from_arete(body)]
    replies = [when for when, body in comments if not from_arete(body)]
    return bool(asked and replies) and max(replies) > max(asked)


def comments_text(comments: list[tuple[str, str, str]]) -> str:
    if not comments:
        return ""
    parts = []
    for who, when, body in comments[-MAX_COMMENTS:]:
        text = body.strip()
        if from_arete(text):
            who, text = "Arete", text.removeprefix(ARETE_TAG).lstrip()
        heading = who + (f" on {when[:10]}" if when else "")
        parts.append(f"### {heading}\n\n{text}")
    return "## Comments\n\n" + "\n\n".join(parts)


def search_ticket(client: jira.Client, issue: dict, blocker: str = "") -> Ticket:
    f = issue.get("fields") or {}
    return Ticket(issue["key"], issue["key"], f.get("summary", ""), url=client.browse_url(issue["key"]),
                  created=f.get("created", ""), labels=list(f.get("labels") or []), blocker=blocker)


def route_blocker(routes: set[str], labels: list[str]) -> str:
    hits = routes & set(labels)
    if not hits:
        return f"it has no route label; add one of {', '.join(f'`{r}`' for r in sorted(routes))}"
    if len(hits) > 1:
        return f"it has more than one route label: {', '.join(sorted(hits))}"
    return ""


WAIT_LINE = re.compile(r"^[ \t]*waits[ \t]+on\b(.*)$", re.IGNORECASE | re.MULTILINE)
WAIT_REF = re.compile(r"(?<![\w/])#(\d+)")
WAIT_TASK = re.compile(r"(?<![\w-])t-[0-9a-f]{4,}(?![\w-])", re.IGNORECASE)


def waits_on(body: str) -> list[int]:
    found = [int(n) for rest in WAIT_LINE.findall(body or "") for n in WAIT_REF.findall(rest)]
    return list(dict.fromkeys(found))


def waits_on_tasks(body: str) -> list[str]:
    found = [tid.lower() for rest in WAIT_LINE.findall(body or "") for tid in WAIT_TASK.findall(rest)]
    return list(dict.fromkeys(found))


def joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _refs(numbers: list[int]) -> str:
    return joined([f"#{n}" for n in numbers])


def task_wait_blocker(waits: list[str], state_of: Callable[[str], str | None]) -> str:
    seen = {tid: state_of(tid) for tid in waits}
    held = [f"{tid} ({s})" for tid, s in seen.items() if s is not None and s not in config.TASK_FINISHED]
    missing = [tid for tid, s in seen.items() if s is None]
    parts = []
    if held:
        parts.append(f"it waits on {joined(held)}, not yet done or closed")
    if missing:
        parts.append(f"it waits on {joined(missing)}, which {'matches' if len(missing) == 1 else 'match'} no task")
    return "; ".join(parts)


def wait_blocker(waits: list[int], is_open: Callable[[int], bool | None]) -> str:
    seen = {n: is_open(n) for n in waits}
    still = [n for n, v in seen.items() if v is True]
    unread = [n for n, v in seen.items() if v is None]
    parts = []
    if still:
        parts.append(f"it waits on {_refs(still)}, which {'is' if len(still) == 1 else 'are'} still open")
    if unread:
        parts.append(f"it waits on {_refs(unread)}, which arete could not read")
    return "; ".join(parts)


def jira_unrouted(project: str, routes: set[str], client: jira.Client) -> list[Ticket]:
    jql = candidates_jql(project)
    out = []
    for i in client.search(jql, SEARCH_FIELDS, limit=50):
        if blocker := route_blocker(routes, (i.get("fields") or {}).get("labels") or []):
            out.append(search_ticket(client, i, blocker))
    return out
