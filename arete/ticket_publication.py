from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from . import config, gh, idempotency, jira, tracker, util
from .store import db as foundation

LOOP_ROUTE = "/loops/configured-ticket"
PLAN_ROUTE = "/repo/publish"
PAIR_ROUTE = "/repo/publish-pair"


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


def _issue_id_from_url(target: dict, url: str) -> str:
    if target["tracker"] == "jira":
        key = url.rstrip("/").rsplit("/", 1)[-1].upper()
        if not jira.KEY_RE.fullmatch(key):
            raise jira.JiraError(f"cannot determine Jira key from {url}")
        return key
    prefix = gh.issue_url(target["github"], "")
    number = url[len(prefix):].rstrip("/") if url.lower().startswith(prefix.lower()) else ""
    if not re.fullmatch(r"[1-9][0-9]*", number):
        raise gh.GhError(f"cannot determine GitHub issue number from {url}")
    return number


def _normalize_pair_entry(cfg: config.Config, raw: dict, index: int, ready_only: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Each paired spec entry must be an object.")
    repo_val = raw.get("repo")
    repo = repo_val if isinstance(repo_val, config.Repo) else cfg.repo(str(repo_val or ""))
    title = str(raw.get("title") or "").strip()
    body = str(raw.get("body") or "")
    if not title or "\n" in title or "\r" in title or not body.strip():
        raise ValueError("Each paired spec needs a single-line title and a nonempty body.")
    role = str(raw.get("role") or repo.name or f"spec-{index}").strip()
    if not role:
        raise ValueError("Each paired spec needs a non-empty role.")
    draft = bool(raw.get("draft", False))
    if ready_only:
        ready = ready_only.casefold() in (role.casefold(), repo.name.casefold())
    else:
        ready = bool(raw.get("ready", True))
    from . import cross_repo

    need = "merge"
    artifact = None
    raw_artifact = raw.get("artifact")
    raw_need = str(raw.get("need") or "").strip()
    if raw_artifact:
        if isinstance(raw_artifact, dict):
            name, ver = str(raw_artifact.get("name") or "").strip(), str(raw_artifact.get("version") or "").strip()
            need, artifact = cross_repo.parse_need(f"artifact={name}:{ver}")
        else:
            text = str(raw_artifact).strip()
            if not text.startswith("artifact="):
                text = f"artifact={text}"
            need, artifact = cross_repo.parse_need(text)
    elif raw_need:
        need, artifact = cross_repo.parse_need(raw_need)
    requires_peer = bool(raw.get("requires_peer", False) or raw_artifact or raw_need)
    destination = target_for(cfg, repo)
    return {
        "repo": repo,
        "role": role,
        "title": title,
        "body": body,
        "draft": draft,
        "ready": ready,
        "requires_peer": requires_peer,
        "need": need,
        "artifact": artifact,
        "destination": destination,
    }


def _need_suffix(need: str, artifact: dict | None) -> str:
    if artifact:
        return f" `@artifact={artifact['name']}:{artifact['version']}`"
    if need == "release":
        return " `@release`"
    return " `@merge`"


def _post_peer_comment(cfg: config.Config, target: dict, issue_id: str, text: str, marker: str) -> None:
    comment_body = f"{tracker.ARETE_TAG} {text}\n\n{marker}"
    if target["tracker"] == "jira":
        client = jira.Client.from_env(cfg.jira)
        if any(marker in (c.get("body") or "") for c in client.comments(issue_id)):
            return
        client.comment(issue_id, comment_body, single_attempt=True)
    else:
        slug = target["github"]
        if gh.find_issue_comment_marker(slug, int(issue_id), marker):
            return
        gh.issue_comment(slug, int(issue_id), comment_body, single_attempt=True)


def _link_native_blocker(cfg: config.Config, src_entry: dict, src_pub: dict,
                         dst_entry: dict, dst_pub: dict) -> None:
    from . import spec_tracker

    if src_entry["destination"]["tracker"] != dst_entry["destination"]["tracker"]:
        return
    if (src_entry["destination"]["tracker"] == "jira"
            and src_entry["destination"]["site"] != dst_entry["destination"]["site"]):
        return
    tk = tracker.for_repo(cfg, src_entry["repo"])
    remote = spec_tracker.adapter(tk, repos=cfg.repos)
    if remote is None:
        return
    try:
        src_ref = src_pub["issue"]
        dst_ref = (remote.reference(dst_entry["repo"].github.casefold(), dst_pub["issue"])
                   if isinstance(remote, spec_tracker.GitHub) else dst_pub["issue"])
        src_item = remote.read(src_ref)
        dst_item = remote.read(dst_ref)
        policy = remote.policy(src_item, need_parent=False, need_create=False, need_links=True)
        if dst_ref not in remote.blockers(src_ref, policy):
            remote.block(dst_item, src_item, policy)
    except spec_tracker.Rejected:
        return


def plan_pair(cfg: config.Config, entries, key: str, *, ready_only: str = "") -> dict:
    """Publish two linked specs together (or only the ready one) with durable idempotent retry."""
    from . import cross_repo

    if len(entries) != 2:
        raise ValueError("Paired spec publication requires exactly two specs.")
    normalized = [_normalize_pair_entry(cfg, raw, i, ready_only) for i, raw in enumerate(entries)]
    if normalized[0]["repo"].name == normalized[1]["repo"].name:
        raise ValueError("Paired specs must target two distinct enrolled repositories.")
    if normalized[0]["role"] == normalized[1]["role"]:
        raise ValueError("Paired specs must have distinct roles.")
    if all(entry["requires_peer"] for entry in normalized):
        raise ValueError("Paired specs cannot circularly require each other.")
    ready_entries = [entry for entry in normalized if entry["ready"]]
    if not ready_entries:
        raise ValueError("At least one spec in the pair must be ready to publish.")

    canonical = {
        "ready_only": ready_only,
        "specs": [
            {
                "role": e["role"],
                "repo": e["repo"].name,
                "title": e["title"],
                "body": e["body"],
                "draft": e["draft"],
                "ready": e["ready"],
                "requires_peer": e["requires_peer"],
                "need": e["need"],
                "artifact": e["artifact"],
            }
            for e in normalized
        ],
    }
    sha = idempotency.fingerprint("POST", PAIR_ROUTE, {}, canonical)
    lock_key = hashlib.sha256(key.encode()).hexdigest()

    with idempotency.owning("repo.publish.pair", key, sha, PAIR_ROUTE) as context:
        with util.lock(f"repo-publish-pair-{lock_key}", wait=True):
            with foundation.tx():
                row = idempotency.matching_claim(context)
                if row and row["status"]:
                    return json.loads(row["response"])
                receipt = idempotency.Publication(context, row)
                if receipt.pending is None:
                    receipt.remember({"published": {}, "linked": {}, "native_linked": {}})

            published = dict(receipt.pending.get("published") or {})
            linked = dict(receipt.pending.get("linked") or {})
            native_linked = dict(receipt.pending.get("native_linked") or {})

            # Publish prerequisites before dependents so retries reuse already-published work.
            ordered = sorted(ready_entries, key=lambda e: (1 if e["requires_peer"] else 0, e["role"]))
            for entry in ordered:
                role = entry["role"]
                if role in published:
                    continue
                member_key = "pair-" + hashlib.sha256(f"{key}:{role}".encode()).hexdigest()[:40]
                url = plan(cfg, entry["repo"], "spec", entry["title"], entry["body"], member_key, draft=entry["draft"])
                issue_id = _issue_id_from_url(entry["destination"], url)
                published[role] = {
                    "repo": entry["repo"].name,
                    "issue": issue_id,
                    "url": url,
                    "tracker": entry["destination"]["tracker"],
                    "draft": entry["draft"],
                    "published": True,
                }
                receipt.remember({**receipt.pending, "published": published, "linked": linked,
                                  "native_linked": native_linked})

            both_published = len(published) == 2
            if both_published:
                by_role = {e["role"]: e for e in normalized}
                roles = [e["role"] for e in normalized]
                for src_role, dst_role in ((roles[0], roles[1]), (roles[1], roles[0])):
                    src_entry, dst_entry = by_role[src_role], by_role[dst_role]
                    src_pub, dst_pub = published[src_role], published[dst_role]
                    edge = f"{src_role}->{dst_role}"
                    if edge not in linked:
                        lines = [f"Linked spec: {dst_pub['repo']} {dst_pub['url']}"]
                        if src_entry["requires_peer"]:
                            lines.append(
                                f"Requires spec: {dst_pub['repo']} {dst_pub['url']}"
                                f"{_need_suffix(src_entry['need'], src_entry.get('artifact'))}"
                            )
                        marker = "aretepeer" + hashlib.sha256(f"{receipt.marker}:{edge}".encode()).hexdigest()[:32]
                        _post_peer_comment(cfg, src_entry["destination"], src_pub["issue"], "\n".join(lines), marker)
                        linked[edge] = True
                        receipt.remember({**receipt.pending, "published": published, "linked": linked,
                                          "native_linked": native_linked})
                    if src_entry["requires_peer"] and edge not in native_linked:
                        _link_native_blocker(cfg, src_entry, src_pub, dst_entry, dst_pub)
                        native_linked[edge] = True
                        receipt.remember({**receipt.pending, "published": published, "linked": linked,
                                          "native_linked": native_linked})
                    cross_repo.record_link(
                        src_pub["repo"],
                        src_pub["issue"],
                        dst_pub["repo"],
                        dst_pub["issue"],
                        url=src_pub["url"],
                        peer_url=dst_pub["url"],
                        role=src_role,
                        requires=src_entry["requires_peer"],
                        need=src_entry["need"],
                        artifact=src_entry.get("artifact"),
                    )

            specs_out = {}
            for entry in normalized:
                role = entry["role"]
                if role in published:
                    peers = [
                        {
                            "role": other["role"],
                            "repo": published[other["role"]]["repo"],
                            "issue": published[other["role"]]["issue"],
                            "url": published[other["role"]]["url"],
                            "requires": entry["requires_peer"],
                            "need": entry["need"],
                            **({"artifact": entry["artifact"]} if entry.get("artifact") else {}),
                        }
                        for other in normalized if other["role"] != role and other["role"] in published
                    ]
                    specs_out[role] = {**published[role], "peers": peers}
                else:
                    specs_out[role] = {
                        "repo": entry["repo"].name,
                        "issue": "",
                        "url": "",
                        "tracker": entry["destination"]["tracker"],
                        "draft": entry["draft"],
                        "published": False,
                        "peers": [],
                    }
            result = {"specs": specs_out, "linked": both_published}
            receipt.complete(json.dumps(result).encode())
            return result
