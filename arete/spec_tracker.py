from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlencode

from . import adf, gh, jira, outbound, tracker

SPEC = "spec"
CREATION_KEY = "specgraph-"
CREATION_LABEL = "arete-spec-"
DIGEST_PATTERN = "[a-f0-9]{64}"
GH_MARKER = re.compile(re.escape(gh.key_mark(CREATION_KEY + "TOKEN")).replace("TOKEN", DIGEST_PATTERN))
JIRA_MARKER = re.compile(re.escape(CREATION_LABEL) + DIGEST_PATTERN)
NUMBER = re.compile(r"[1-9][0-9]*")
GH_REPO = r"[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9_.-]*"
GH_REF = re.compile(rf"(?:({GH_REPO})?#)?({NUMBER.pattern})", re.IGNORECASE | re.ASCII)
GH_URL = re.compile(rf"https://github\.com/({GH_REPO})/issues/({NUMBER.pattern})/?", re.IGNORECASE | re.ASCII)


class Held(ValueError):
    pass


class Rejected(Held):
    pass


READ_ERRORS = (gh.GhError, jira.JiraError, ValueError, TypeError, KeyError, AttributeError)


@dataclass(frozen=True)
class Item:
    ref: str
    native_id: str
    title: str
    body: str
    labels: tuple[str, ...]
    open: bool
    tasks: tuple[str, ...] | None = None


def adapter(tk, *, fresh=False, repos=()):
    if isinstance(tk, tracker.GitHubTracker):
        return GitHub(tk.repo.github, (repo.github for repo in repos if repo.tracker == "github"))
    if isinstance(tk, tracker.JiraTracker):
        client = jira.Client.from_env(tk.cfg) if fresh else tk.client
        return Jira(client, tk.project, tk.route)
    return None


def valid_native(value):
    return type(value) in (str, int) and NUMBER.fullmatch(str(value))


def confirmed_item(ref, native_id, title, body, labels, opened, tasks=None):
    if not valid_native(native_id) or not isinstance(title, str) or not isinstance(body, str):
        raise Held(f"the tracker did not confirm ticket {ref}")
    return Item(ref, str(native_id), title, body, tuple(labels), opened, tasks)


class GitHub:
    def __init__(self, repo, allowed_repos=()):
        self.repo = repo.casefold()
        self.allowed_repos = {self.repo, *(slug.casefold() for slug in allowed_repos if slug)}
        self.scope = f"github:{self.repo}"

    def account(self):
        account = (self.get("user") or {}).get("id")
        if not valid_native(account):
            raise Held("GitHub did not identify the spec graph publisher")
        return str(account)

    def address(self, text):
        match = GH_REF.fullmatch(text) or GH_URL.fullmatch(text)
        if not match:
            raise Held(f"{text!r} is not a GitHub issue reference")
        repo, number = (match[1] or self.repo).casefold(), match[2]
        if repo not in self.allowed_repos:
            raise Held(f"GitHub repository {repo} is outside the configured spec graph authority")
        return repo, number

    def reference(self, repo, number):
        return str(number) if repo == self.repo else f"{repo}#{number}"

    def ref(self, text):
        return self.reference(*self.address(text))

    def claim_targets(self, ref, repos):
        slug, number = self.address(ref)
        return ((repo.name, number) for repo in repos if repo.tracker == "github" and repo.github.casefold() == slug)

    def task_ref(self, text):
        if ("#" in text and GH_REF.fullmatch(text)) or GH_URL.fullmatch(text):
            return self.ref(text)
        return None

    def path(self, ref=""):
        if not ref:
            return f"repos/{self.repo}/issues"
        repo, number = self.address(ref)
        return f"repos/{repo}/issues/{number}"

    def get(self, path):
        return gh.gh_json(["api", path, "--hostname", "github.com"])

    def rows(self, path, **query):
        query = urlencode({"per_page": 100, **query})
        pages = gh.gh_json(["api", f"{path}?{query}", "--hostname", "github.com", "--paginate", "--slurp"])
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise Held("GitHub did not return a paginated tracker list")
        return [row for page in pages for row in page]

    def send(self, path, payload):
        try:
            raw = gh.gh(["api", "-X", "POST", path, "--hostname", "github.com", "--input", "-"],
                        input=gh.Outgoing(json.dumps(payload)), write=True, retry=False)
        except gh.GhError as error:
            if not isinstance(error, gh.GhUnsure) and re.search(r"HTTP (400|401|403|404|422|429)\b", str(error)):
                raise Rejected(str(error)) from error
            raise
        return json.loads(raw) if raw.strip() else None

    def slug(self, row):
        url = row.get("repository_url") or ""
        match = re.fullmatch(rf"{re.escape(gh.GITHUB_API)}/repos/({GH_REPO})", url, re.IGNORECASE | re.ASCII)
        return match[1].casefold() if match else ""

    def item(self, row, *, scoped=True):
        if not isinstance(row, dict):
            raise Held("GitHub returned an unrecognized issue")
        repo = self.slug(row)
        raw_labels = row.get("labels")
        if ("pull_request" in row or not valid_native(row.get("number")) or not repo
                or row.get("state") not in ("open", "closed")
                or not isinstance(raw_labels, list)
                or any(not isinstance(lb, dict) or not isinstance(lb.get("name"), str) for lb in raw_labels)):
            raise Held("GitHub returned an unrecognized issue")
        if scoped and repo not in self.allowed_repos:
            raise Held(f"GitHub returned a native child outside the configured authority: {repo}#{row['number']}")
        return confirmed_item(self.reference(repo, row["number"]), row.get("id"), row.get("title"), row.get("body") or "",
                              [label["name"] for label in raw_labels], row["state"] == "open")

    def read(self, ref):
        item = self.item(self.get(self.path(ref)))
        if item.ref != ref:
            raise Held(f"GitHub issue #{ref} changed identity")
        return item

    def parent(self, ref):
        try:
            row = self.get(self.path(ref) + "/parent")
        except gh.GhError as error:
            if re.search(r"HTTP 404\b", str(error)):
                return None
            raise
        return self.item(row, scoped=False)

    def launch_blocker(self, ref):
        if any(GH_MARKER.fullmatch(line) for line in self.read(ref).body.splitlines()):
            return f"#{ref} was created for a spec graph"
        if (parent := self.parent(ref)) and SPEC in parent.labels:
            target = parent.ref if "#" in parent.ref else f"#{parent.ref}"
            return f"#{ref} belongs to native spec parent {target}"
        return ""

    def children(self, ref):
        return [self.item(row) for row in self.rows(self.path(ref) + "/sub_issues")]

    def blockers(self, ref, policy):
        result = set()
        for row in self.rows(self.path(ref) + "/dependencies/blocked_by"):
            repo, number = self.slug(row), row.get("number")
            if not repo or not valid_native(number):
                raise Held(f"GitHub did not confirm every blocker of {ref}")
            if repo in self.allowed_repos:
                result.add(self.reference(repo, number))
        return result

    def policy(self, parent, *, need_parent, need_create, need_links):
        return {}

    def check_attachment(self, child, policy):
        return None

    def create(self, node, parent, policy, token, account):
        row = self.send(self.path(), {"title": outbound.screen(node["title"]),
                                      "body": gh.outgoing(contract(node), key=CREATION_KEY + token)})
        return self.created(row, token, account)

    def created(self, row, token, account):
        return (self.item(row) if row and "pull_request" not in row and self.slug(row) == self.repo
                and str((row.get("user") or {}).get("id")) == account
                and gh.key_mark(CREATION_KEY + token) in (row.get("body") or "").splitlines() else None)

    def find_creation(self, token, account):
        return one([item for row in self.rows(self.path(), state="all") if (item := self.created(row, token, account))])

    def enable(self, ref):
        repo, number = self.address(ref)
        gh.issue_labels(repo, int(number), add=[tracker.LABEL], single_attempt=True)

    def attach(self, parent, child):
        self.send(self.path(parent.ref) + "/sub_issues", {"sub_issue_id": int(child.native_id), "replace_parent": False})

    def block(self, prerequisite, dependent, policy):
        self.send(self.path(dependent.ref) + "/dependencies/blocked_by", {"issue_id": int(prerequisite.native_id)})

    def comment(self, ref, body, token):
        self.send(self.path(ref) + "/comments", {"body": gh.outgoing(body, key="spec-contract-" + token)})

    def commented(self, ref, token, account):
        return any(str((row.get("user") or {}).get("id")) == account
                   and gh.key_mark("spec-contract-" + token) in (row.get("body") or "").splitlines()
                   for row in self.rows(self.path(ref) + "/comments"))


JIRA_ROOT = "/rest/api/3"
JIRA_FIELDS = "summary,description,labels,status,parent,project,issuetype,issuelinks,creator"


class Jira:
    def __init__(self, client, project, route):
        self.client = client
        self.project = project.upper()
        self.route = route
        self.scope = f"jira:{client.base}"

    def account(self):
        account = self.client.me().get("accountId")
        if not isinstance(account, str) or not account:
            raise Held("Jira did not identify the spec graph publisher")
        return account

    def ref(self, text, *, same_project=True):
        ref = text.upper()
        if not jira.KEY_RE.fullmatch(ref) or same_project and ref.rsplit("-", 1)[0] != self.project:
            raise Held(f"{text!r} is not a Jira issue key in project {self.project}")
        return ref

    def claim_targets(self, ref, repos):
        return ((repo.name, ref) for repo in repos if repo.tracker == "jira")

    def task_ref(self, text):
        key = text.removeprefix(self.client.base + "/browse/").rstrip("/")
        return self.ref(key) if jira.KEY_RE.fullmatch(key.upper()) else None

    def path(self, ref):
        return f"{JIRA_ROOT}/issue/{self.ref(ref, same_project=False)}"

    def row(self, ref, *, same_project=True, fields=JIRA_FIELDS):
        want = self.ref(ref, same_project=same_project)
        row = self.client.get(self.path(want), fields=fields)
        if not isinstance(row, dict) or row.get("key") != want:
            raise Held(f"Jira issue {ref} changed identity")
        return row

    def item(self, row, *, same_project=True):
        fields = row["fields"]
        category = fields["status"]["statusCategory"]["key"]
        if not category:
            raise Held(f"Jira did not confirm the status of {row['key']}")
        description, labels = fields.get("description"), fields.get("labels") or []
        body = adf.to_text(description)
        tasks = adf.task_items(description) if SPEC in labels and isinstance(description, dict) else None
        return confirmed_item(self.ref(row["key"], same_project=same_project), row.get("id"), fields["summary"],
                              body, labels, category != "done", tasks)

    def read(self, ref):
        return self.item(self.row(ref))

    def parent(self, ref):
        if parent := self.row(ref)["fields"].get("parent"):
            return self.item(self.row(parent["key"], same_project=False), same_project=False)
        return None

    def launch_blocker(self, ref):
        fields = self.row(ref, same_project=False, fields="labels,parent")["fields"]
        if any(JIRA_MARKER.fullmatch(label) for label in fields.get("labels") or []):
            return f"{ref} was created for a spec graph"
        if parent := fields.get("parent"):
            labels = self.row(parent["key"], same_project=False, fields="labels")["fields"].get("labels") or []
            if SPEC in labels:
                return f"{ref} belongs to native spec parent {parent['key']}"
        return ""

    def children(self, ref):
        return [self.item(row) for row in self.client.search(f'parent = "{self.ref(ref)}"', JIRA_FIELDS.split(","),
                                                             limit=sys.maxsize)]

    def send(self, method, path, payload):
        try:
            return self.client.request(method, path, body=payload, single_attempt=True)
        except jira.JiraError as error:
            if not isinstance(error, jira.JiraUnsure) and error.status in (400, 401, 403, 404, 422, 429):
                raise Rejected(str(error)) from error
            raise

    def policy(self, parent, *, need_parent, need_create, need_links):
        policy = {}
        if need_parent or need_create:
            fields = self.row(parent.ref)["fields"]
            project_id = fields["project"]["id"]
            types = self.client.get(f"{JIRA_ROOT}/issuetype/project", projectId=project_id)
            current = next((row for row in types if row["id"] == fields["issuetype"]["id"]), fields["issuetype"])
            level = current.get("hierarchyLevel")
            if type(level) is not int:
                raise Held("Jira did not report the parent issue type's hierarchy level")
            choices = [row for row in types if row.get("hierarchyLevel") == level - 1]
            preferred = "task" if level == 1 else "subtask" if level == 0 else ""
            preferred_choices = [row for row in choices if row["name"].casefold().replace("-", "") == preferred]
            policy["types"] = [row["id"] for row in choices]
            if preferred_choices or len(choices) == 1:
                policy["type"] = one(preferred_choices or choices)["id"]
            if need_create and not policy.get("type"):
                raise Held("Jira has no unambiguous child type for this spec; configure a Task or Sub-task at the next level")
        if need_links:
            found = self.client.get(f"{JIRA_ROOT}/issueLinkType")["issueLinkTypes"]
            choice = one([row for row in found if is_blocks(row)])
            if not choice or not choice.get("id"):
                raise Held("Jira needs a unique Blocks link type (blocks / is blocked by)")
            policy["blocks"] = choice["id"]
        return policy

    def check_attachment(self, child, policy):
        issue_type = self.row(child.ref)["fields"]["issuetype"]["id"]
        if issue_type not in policy["types"]:
            raise Held(f"{child.ref} is not a compatible Jira child issue type; correct its type before adoption")

    def create(self, node, parent, policy, token, account):
        fields = {"project": {"key": self.project}, "parent": {"key": parent.ref},
                  "assignee": {"accountId": account}, "issuetype": {"id": policy["type"]}, "summary": outbound.screen(node["title"]),
                  "description": adf.from_markdown(outbound.screen(contract(node))), "labels": [self.route, CREATION_LABEL + token]}
        row = self.send("POST", f"{JIRA_ROOT}/issue", {"fields": fields})
        if not row or not valid_native(row.get("id")) or not row.get("key"):
            return None
        return confirmed_item(self.ref(row["key"]), row["id"], fields["summary"], adf.to_text(fields["description"]),
                              fields["labels"], True)

    def find_creation(self, token, account):
        label = CREATION_LABEL + token
        query = f'project = {json.dumps(self.project)} AND creator = {json.dumps(account)} AND labels = "{label}"'
        rows = self.client.search(query, JIRA_FIELDS.split(","), limit=sys.maxsize)
        return one([self.item(row) for row in rows if label in (row["fields"].get("labels") or [])
                    and (row["fields"].get("creator") or {}).get("accountId") == account])

    def enable(self, ref):
        account = self.account()
        fields = self.row(ref, fields="assignee,labels")["fields"]
        if (fields.get("assignee") or {}).get("accountId") != account:
            raise Held(f"{ref} must be assigned to the configured Jira publisher before dispatch")
        self.send("PUT", self.path(ref), {"update": {"labels": [{"add": tracker.LABEL}]}})

    def attach(self, parent, child):
        self.send("PUT", self.path(child.ref), {"fields": {"parent": {"key": parent.ref}}})

    def blockers(self, ref, policy):
        result = set()
        for link in self.row(ref)["fields"].get("issuelinks") or []:
            kind = link["type"]
            if (is_blocks(kind) or kind.get("id") == policy.get("blocks", "")) and link.get("inwardIssue"):
                result.add(link["inwardIssue"]["key"])
        return result

    def block(self, prerequisite, dependent, policy):
        self.send("POST", f"{JIRA_ROOT}/issueLink", {"type": {"id": policy["blocks"]},
                                                   "inwardIssue": {"key": prerequisite.ref},
                                                   "outwardIssue": {"key": dependent.ref}})

    def comment(self, ref, body, token):
        markdown = f"{outbound.screen(body)}\n\naretespec{token}"
        self.send("POST", self.path(ref) + "/comment", {"body": adf.from_markdown(markdown)})

    def commented(self, ref, token, account):
        for row in self.client.comments(ref):
            if not isinstance(row, dict) or (row.get("author") or {}).get("accountId") != account:
                continue
            try:
                text = adf.to_text(row.get("body"))
            except ValueError:
                continue
            if f"aretespec{token}" in text.splitlines():
                return True
        return False


def one(items):
    if len(items) > 1:
        raise Held("tracker lookup returned more than one match; reconcile manually")
    return items[0] if items else None


def is_blocks(kind):
    return (kind.get("outward", "").strip().casefold() == "blocks"
            and kind.get("inward", "").strip().casefold() == "is blocked by")


def contract(node):
    checks = "\n".join(f"- {check}" for check in node["checks"])
    return f"### Verifiable outcome\n\n{node['outcome']}\n\n### Acceptance checks\n\n{checks}"
