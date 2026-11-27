from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from . import config, gh, idempotency, jira, tracker, util
from .store import db as foundation

LOOP_ROUTE = "/loops/configured-ticket"
PLAN_ROUTE = "/repo/publish"


def target_for(cfg: config.Config, repo: config.Repo) -> dict:
    if repo.tracker == "github" and repo.github:
        return {"tracker": "github", "github": repo.github}
    if repo.tracker == "jira":
        if project := tracker.jira_project(repo, cfg.jira):
            return {"tracker": "jira", "project": project, "route": tracker.route_label(repo),
                    "site": repo.jira_site or jira.site_url(cfg.jira)}
        raise ValueError(f"{repo.name} has no Jira project configured")
    raise ValueError(f"{repo.name} needs a configured GitHub or Jira destination")


def plan(cfg: config.Config, repo: config.Repo, kind: str, title: str, body: str, key: str, *,
         parent: str = "", source: str = "", draft: bool = False) -> str:
    if draft and kind != "spec":
        raise ValueError("--draft applies only to a spec.")
    if kind not in ("spec", "child", "follow-up"):
        raise ValueError("Publish a spec, child or follow-up.")
    if (kind == "child") != bool(parent):
        raise ValueError("A child needs a parent issue in its destination repo; only a child takes --parent.")
    if (kind == "follow-up") != bool(source):
        raise ValueError("A follow-up needs a source issue URL; only a follow-up takes --source.")
    if not title.strip() or "\n" in title.strip() or "\r" in title.strip() or not body.strip():
        raise ValueError("Publication needs a single-line title and a nonempty body.")
    if source:
        url = urlsplit(source)
        if (not url.hostname or url.scheme not in ("https", "http") or url.username is not None
                or url.password is not None or url.query or url.fragment or re.search(r"[\s<>()]", source)):
            raise ValueError("Use a full source issue URL without credentials, a query or a fragment.")
    target = target_for(cfg, repo)
    if parent:
        parent = parent.strip()
        if target["tracker"] == "jira":
            parent = parent.upper()
            if not jira.KEY_RE.fullmatch(parent) or parent.rsplit("-", 1)[0] != target["project"].upper():
                raise ValueError("The parent must be an issue key in the enrolled Jira project.")
        else:
            parent = parent.removeprefix("#")
            if not re.fullmatch(r"[1-9][0-9]*", parent):
                raise ValueError("The parent must be an issue number in the enrolled GitHub repository.")
    payload = {"repo": repo.name, "kind": kind, "title": title.strip(), "body": body, "parent": parent, "source": source}
    labels = ["spec"] if kind == "spec" else []
    if draft:
        payload["draft"] = True
    elif kind == "spec" and repo.pipeline and repo.target_branch:
        labels.append("agent")
        payload["admitted"] = True
    sha = idempotency.fingerprint("POST", PLAN_ROUTE, {}, payload)
    item = {"destination": target, "title": payload["title"], "parent": parent, "labels": labels,
            "body": gh.marked(body + (f"\n\nSource: [source issue]({source})" if source else ""))}
    lock_key = hashlib.sha256(key.encode()).hexdigest()
    with idempotency.owning("repo.publish", key, sha, PLAN_ROUTE) as context:
        with util.lock(f"repo-publish-{lock_key}", wait=True):
            return publish(item, cfg, context)


def finding(item: dict, cfg: config.Config, approval) -> str:
    key = item["publication_key"]
    try:
        with idempotency.owning("loop.ticket", key, key, LOOP_ROUTE) as context:
            return publish(item, cfg, context, approval)
    except (idempotency.Unconfirmed, idempotency.InFlight) as error:
        raise RuntimeError(str(error)) from error


def publish(item: dict, cfg: config.Config, context, approval=None) -> str:
    with foundation.tx():
        row = idempotency.matching_claim(context)
        if row and row["status"]:
            return json.loads(row["response"])
        receipt = idempotency.Publication(context, row)
        if receipt.pending is None:
            receipt.remember({"destination": item["destination"]})
    return send(item, cfg, receipt, approval)


def jira_parent(client: jira.Client, target: dict, parent: str) -> str:
    row = client.issue(parent, ["project", "labels", "issuetype"])
    fields = row.get("fields") or {}
    if (row.get("key") != parent or (fields.get("project") or {}).get("key") != target["project"]
            or not {"spec", target["route"]} <= set(fields.get("labels") or [])
            or (fields.get("issuetype") or {}).get("subtask")):
        raise ValueError("The Jira parent must be a spec for the enrolled repo, not a subtask.")
    return client.subtask_type(target["project"])


def github_child(repo: str, parent: str, url: str, receipt: idempotency.Publication) -> None:
    prefix = gh.issue_url(repo, "")
    number = url[len(prefix):].rstrip("/") if url.lower().startswith(prefix.lower()) else ""
    if not re.fullmatch(r"[1-9][0-9]*", number):
        raise gh.GhError("the published child URL does not match the enrolled repo")
    if not receipt.pending.get("issue_url"):
        receipt.remember({**receipt.pending, "issue_url": url})
    gh.ensure_sub_issue(repo, int(parent), int(number))


def send(item: dict, cfg: config.Config, receipt: idempotency.Publication, approval) -> str:
    target = receipt.pending["destination"]
    if target != item["destination"]:
        raise ValueError("The queued ticket's destination differs from its earlier publication receipt")
    marker = "areteemit" + hashlib.sha256(receipt.marker.encode()).hexdigest()
    body = item["body"] + "\n\n" + marker
    if approval:
        body = approval.text(body)
    parent = item.get("parent", "")

    if target["tracker"] == "jira":
        client = jira.Client.from_env(cfg.jira)
        if client.base != target["site"]:
            raise jira.JiraError("the queued ticket belongs to a different Jira site")
        account = client.me().get("accountId")
        if not account or (receipt.pending.get("account") and receipt.pending["account"] != account):
            raise jira.JiraError("cannot confirm the Jira account for this ticket")
        found = receipt.pending.get("issue_url") or client.find_task_marker(target["project"], account, marker)
        labels = list(dict.fromkeys([*item["labels"], target["route"]]))
        options = {"parent": parent, "subtask_type": jira_parent(client, target, parent)} if parent and not found else {}

        def create():
            return client.create_task(target["project"], account, item["title"], body, labels, **options)
    else:
        account = ""
        github = target["github"]
        if parent and not any(row.get("name") == "spec" for row in gh.issue_node(github, int(parent)).get("labels", [])):
            raise ValueError("The GitHub parent must be a spec in the enrolled repo.")
        found = receipt.pending.get("issue_url") or gh.find_issue_marker(github, marker)

        def create():
            number = gh.issue_create(github, item["title"], body, item["labels"], single_attempt=True)
            return gh.issue_url(github, number)

    if not found:
        receipt.require_unsent()
        if target["tracker"] == "github":
            for label in item["labels"]:
                gh.ensure_label(github, label, restyle=False)
        with foundation.tx():
            row = idempotency.matching_claim(receipt.context)
            if row["status"]:
                return json.loads(row["response"])
            receipt.pending = json.loads(row["response"])
            receipt.require_unsent()
            if account:
                if receipt.pending.get("account") not in (None, account):
                    raise jira.JiraError("cannot confirm the Jira account for this ticket")
                receipt.remember({**receipt.pending, "account": account})
            if approval and not (approval.pending or {}).get("send_started"):
                if approval.pending is None:
                    approval.remember({})
                approval.begin_send()
            receipt.begin_send()
        try:
            found = create()
        except (gh.GhUnsure, jira.JiraUnsure):
            raise
        except (gh.GhError, jira.JiraError):
            receipt.remember({**receipt.pending, "send_started": False})
            raise
    if parent:
        if target["tracker"] == "github":
            github_child(target["github"], parent, found, receipt)
        else:
            if not receipt.pending.get("issue_url"):
                receipt.remember({**receipt.pending, "issue_url": found})
            child_key = found.rsplit("/", 1)[-1]
            fields = client.issue(child_key, ["parent"]).get("fields") or {}
            if (fields.get("parent") or {}).get("key") != parent:
                raise jira.JiraError("Jira did not confirm the published child's parent")
    receipt.complete(json.dumps(found).encode())
    return found
