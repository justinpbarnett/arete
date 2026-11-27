from __future__ import annotations

import base64
import functools
import itertools
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlencode

from . import accounts, config, outbound, paths, secret_move, secretstore, util

ARETE_MARK = "<!-- arete -->"
GITHUB_API = "https://api.github.com"
GITHUB_ACTIONS_APP_ID = 15368
CLONE_TOKEN_PERMISSIONS = {"contents": "read"}
NO_ISSUE = "Could not resolve to an issue"
_deployment_ci_deadline: ContextVar[float | None] = ContextVar("deployment_ci_deadline", default=None)


class GhError(RuntimeError):
    pass


class GhUnsure(GhError):
    pass


class WorkflowNotForPR(GhError):
    pass


def marked(body: str) -> str:
    return f"{body.rstrip()}\n\n{ARETE_MARK}"


def has_arete_mark(body: str | None) -> bool:
    return ARETE_MARK in (body or "")


class Outgoing(str):
    pass


TEXT_FLAGS = ("--body", "-b", "--comment", "-c", "--field", "-f", "--raw-field", "-F")
JOINED_TEXT_FLAGS = ("--body=", "--comment=", "--body-file=", "--field=", "--raw-field=", "--input=")
STDIN_ONLY_FLAGS = ("--body-file", "--input")
ATTACHED_SHORT_FLAGS = ("-b", "-c", "-f", "-F")


def joined_text_flag(arg: str) -> bool:
    return arg.startswith(JOINED_TEXT_FLAGS) or (len(arg) > 2 and arg.startswith(ATTACHED_SHORT_FLAGS))


def unscreened_text(args: list[str], input: str | None) -> bool:
    if input is not None and not isinstance(input, Outgoing):
        return True
    if any(joined_text_flag(str(arg)) for arg in args):
        return True
    return any((flag in TEXT_FLAGS and not isinstance(value, Outgoing)) or (flag in STDIN_ONLY_FLAGS and value != "-")
               for flag, value in itertools.pairwise(args))


def gh(args: list[str], cwd=None, timeout: float = 120, input: str | None = None, write: bool = False,
       *, retry: bool = True) -> str:
    from .store import factory

    factory.check_worker()
    if unscreened_text(args, input):
        raise TypeError("text sent to GitHub must come from gh.outgoing")
    if retry:
        res = util.sh_with_network(["gh", *args], cwd=cwd, timeout=timeout, input=input,
                                   rerun=util.NEVER_SENT if write else util.NETWORK_ERROR)
    else:
        res = util.sh(["gh", *args], cwd=cwd, timeout=timeout, input=input)
    if res.returncode != 0:
        command = " ".join(itertools.takewhile(lambda arg: not arg.startswith("-"), args))
        output = res.stdout + res.stderr
        unsure = write and (util.NETWORK_ERROR.search(output) or res.returncode == util.TIMED_OUT or
                            re.search(r"HTTP 5\d\d|Something went wrong while executing your query", output, re.IGNORECASE))
        error = GhUnsure if unsure else GhError
        raise error(f"gh {command}: {util.first_line(res.stderr or res.stdout, 300)}")
    return res.stdout


def gh_json(args: list[str], cwd=None, timeout: float = 120, *, retry: bool = True):
    if (deadline := _deployment_ci_deadline.get()) is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GhError("deployment CI lookup timed out")
        timeout, retry = min(timeout, remaining), False
    out = gh(args, cwd=cwd, timeout=timeout, retry=retry)
    return json.loads(out) if out.strip() else None


def ensure_label(repo: str, name: str, color: str = "5319e7", description: str = "", restyle: bool = True) -> str:
    args = ["label", "create", name, "--repo", repo, "--color", color, "--description", description]
    try:
        gh([*args, "--force"] if restyle else args)
        return ""
    except GhError as e:
        if not restyle and "already exists" in str(e):
            return ""
        return f"{name}: {e}"


def read_blocking_links(issues: list[dict]) -> None:
    by_id = {row["id"]: row for row in issues if isinstance(row.get("id"), str) and row["id"]}
    query = ("query($ids: [ID!]!) { nodes(ids: $ids) { ... on Issue { id blockedBy(first: 100) "
             "{ totalCount nodes { number state url } } } } }")
    for ids in itertools.batched(by_id, 100, strict=False):
        payload = Outgoing(json.dumps({"query": query, "variables": {"ids": ids}}))
        try:
            reply = json.loads(gh(["api", "graphql", "--input", "-"], input=payload, timeout=15, retry=False))
        except (GhError, ValueError):
            continue
        data = reply.get("data") if isinstance(reply, dict) else None
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict) and node.get("id") in ids:
                    by_id[node["id"]]["blockedBy"] = node.get("blockedBy")


def with_blocking_links(args: list[str]):
    fields = args.index("--json") + 1
    projected = args.copy()
    projected[fields] += ",blockedBy"
    try:
        data = gh_json(projected)
    except GhError as error:
        if 'Unknown JSON field: "blockedBy"' not in str(error):
            raise
        projected[fields] = args[fields] + ",id"
        data = gh_json(projected)
        read_blocking_links([row for row in (data if isinstance(data, list) else [data]) if isinstance(row, dict)])
    # An unavailable projection must hold admission even when the ticket itself is readable.
    for issue in data if isinstance(data, list) else [data]:
        if not isinstance(issue, dict):
            raise GhError("GitHub issue lookup returned no issue metadata")
        issue.setdefault("blockedBy", None)
    return data


def issues(repo: str, label: str = "", state: str = "open", limit: int = 30, author: str = "", *,
           search: str = "") -> list[dict]:
    args = ["issue", "list", "--repo", repo, "--state", state, "--limit", str(limit),
            "--json", "number,title,body,labels,url,createdAt,updatedAt,author"]
    if label:
        args += ["--label", label]
    if author:
        args += ["--author", author]
    if search:
        args += ["--search", search]
    return (with_blocking_links(args) if label == "agent" else gh_json(args)) or []


def issue(repo: str, number: int) -> dict:
    return with_blocking_links(
        ["issue", "view", str(number), "--repo", repo, "--json",
         "number,title,body,labels,url,comments,state,author,updatedAt"]
    )


def open_issue_blockers(repo: str, number: int, issue: dict) -> list[int | str]:
    connection = issue.get("blockedBy", {"nodes": [], "totalCount": 0})
    missing = f"could not read all GitHub blocking links for {repo}#{number}"
    if not isinstance(connection, dict):
        raise GhError(missing)
    nodes, count = connection.get("nodes"), connection.get("totalCount")
    if not isinstance(nodes, list) or type(count) is not int or count < 0:
        raise GhError(missing)
    if len(nodes) != count:
        nodes = api_list(f"repos/{repo}/issues/{number}/dependencies/blocked_by")
        if len(nodes) != count:
            raise GhError(missing)
    blockers = []
    for node in nodes:
        if not isinstance(node, dict):
            raise GhError(missing)
        state = str(node.get("state", "")).upper()
        link = re.fullmatch(r"https://[^/]+/([\w.-]+/[\w.-]+)/issues/([1-9][0-9]*)",
                            node.get("html_url") or node.get("url") or "")
        if not link or state not in {"OPEN", "CLOSED"} or int(link[2]) != node.get("number"):
            raise GhError(missing)
        if state == "OPEN":
            other, dependency = link[1].casefold(), int(link[2])
            blockers.append(dependency if other == repo.casefold() else f"{other}#{dependency}")
    return blockers


def issue_node(repo: str, number: int) -> dict:
    row = gh_json(["api", f"repos/{repo}/issues/{number}"])
    if not isinstance(row, dict) or row.get("number") != number or "pull_request" in row or type(row.get("id")) is not int:
        raise GhError(f"could not confirm issue {repo}#{number}")
    return row


def ensure_sub_issue(repo: str, parent: int, child: int) -> None:
    path = f"repos/{repo}/issues/{parent}/sub_issues"
    child_id = issue_node(repo, child)["id"]
    if any(row.get("id") == child_id for row in api_list(path)):
        return
    gh(["api", path, "--method", "POST", "--input", "-"],
       input=Outgoing(json.dumps({"sub_issue_id": child_id, "replace_parent": False})), write=True, retry=False)


def outgoing(text: str, link: str = "", mark: bool = False, key: str = "") -> Outgoing:
    text = outbound.inert(outbound.screen(text))
    if link:
        text = f"{outbound.screen(link)}\n\n{text}"
    text = marked(text) if mark else text
    return Outgoing(f"{text.rstrip()}\n\n{key_mark(key)}" if key else text)


def key_mark(key: str) -> str:
    return f"<!-- arete:key={key} -->"


def recovery_lookup(lookup):
    try:
        if not util.wait_for_network():
            raise GhUnsure("GitHub reply was lost and the network is unavailable")
        return lookup()
    except (GhError, ValueError, TypeError, KeyError, AttributeError) as e:
        raise GhUnsure(f"GitHub reply was lost; could not reconcile the write: {e}") from e


def comment_key(path: str, key: str, start):
    query = urlencode({"since": util.iso(start - timedelta(seconds=60)), "per_page": 100})
    pages = gh_json(["api", f"{path}?{query}", "--paginate", "--slurp"])
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise GhUnsure("GitHub comment lookup returned no list")
    return next((row for page in pages for row in page if key_mark(key) in (row.get("body") or "")), None)


def keyed_comment(args: list[str], body: str, path: str, mark: bool = False, envelope: bool = False,
                  *, key: str = "", single_attempt: bool = False) -> str:
    key, start = key or uuid.uuid4().hex, util.now()
    payload = outgoing(body, mark=mark, key=key)
    if envelope:
        payload = Outgoing(json.dumps({"body": payload}))
    try:
        return gh(args, input=payload, write=True).strip()
    except GhUnsure:
        if single_attempt:
            raise
        if found := recovery_lookup(lambda: comment_key(path, key, start)):
            return found.get("html_url", "")
        return gh(args, input=payload, write=True).strip()


def split_link(body: str, links) -> tuple[str, str]:
    for link in sorted(filter(None, links), key=len, reverse=True):
        if "\n" in link:
            approved = link.split("\n")
            if not all(re.fullmatch(r"Closes #[1-9][0-9]*", line) for line in approved):
                continue
            lines = body.split("\n")
            prefix = list(itertools.takewhile(lambda line: re.fullmatch(r"Closes #[1-9][0-9]*", line), lines))
            if len(prefix) == len(approved) and set(prefix) == set(approved):
                return link, "\n".join(lines[len(prefix):]).lstrip("\n")
        elif body == link or body.startswith(f"{link}\n"):
            return link, body[len(link):].lstrip("\n")
    return "", body


def issue_create(repo: str, title: str, body: str, labels: Sequence[str] = (), *, key: str = "",
                 single_attempt: bool = False) -> int:
    key = key or uuid.uuid4().hex
    args = ["issue", "create", "--repo", repo, "--title", outbound.screen(title), "--body-file", "-"]
    for lb in labels:
        args += ["--label", lb]
    payload = outgoing(body, key=key)
    try:
        reply = gh(args, input=payload, write=True)
    except GhUnsure:
        if single_attempt:
            raise

        def find():
            rows = gh_json(["issue", "list", "--repo", repo, "--state", "all", "--limit", "20", "--json", "number,body"])
            if not isinstance(rows, list):
                raise GhUnsure("GitHub issue lookup returned no list")
            return next((r["number"] for r in rows if key_mark(key) in (r.get("body") or "")), None)
        if number := recovery_lookup(find):
            return int(number)
        reply = gh(args, input=payload, write=True)
    parts = reply.strip().splitlines()
    if not parts or not (number := re.fullmatch(r"https://[^/]+/[^/]+/[^/]+/issues/([1-9][0-9]*)/?", parts[-1])):
        raise GhUnsure("GitHub did not confirm the created issue number")
    return int(number[1])


def recent_issues(repo: str, limit: int = 100) -> list[dict]:
    return gh_json(["issue", "list", "--repo", repo, "--state", "all", "--limit", str(limit), "--search",
                    "sort:updated-desc", "--json", "number,title,body,state,stateReason,updatedAt"]) or []


def issue_url(repo: str, number: int | str) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def publication_url(path: str, marker: str, **query) -> str:
    query = urlencode({"per_page": 100, **query})
    pages = gh_json(["api", f"{path}?{query}", "--paginate", "--slurp"])
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise GhError("GitHub publication lookup returned no list")
    for page in pages:
        for row in page:
            if "pull_request" not in row and marker in (row.get("body") or "").splitlines():
                if not row.get("html_url"):
                    raise GhError("GitHub publication lookup returned no URL")
                return row["html_url"]
    return ""


def find_issue_marker(repo: str, marker: str) -> str:
    return publication_url(f"repos/{repo}/issues", marker, state="all")


def find_issue_comment_marker(repo: str, number: int, marker: str) -> str:
    return publication_url(f"repos/{repo}/issues/{number}/comments", marker)


def find_pr_marker(repo: str, branch: str, marker: str) -> str:
    return publication_url(f"repos/{repo}/pulls", marker, state="all", head=f"{repo.split('/')[0]}:{branch}")


def issue_comment(repo: str, number: int, body: str, *, return_url: bool = False,
                  single_attempt: bool = False) -> str | None:
    url = keyed_comment(["issue", "comment", str(number), "--repo", repo, "--body-file", "-"], body,
                        f"repos/{repo}/issues/{number}/comments", single_attempt=single_attempt)
    return url if return_url else None


def issue_close(repo: str, number: int, comment: str = "") -> None:
    key, start = uuid.uuid4().hex, util.now()
    args = ["issue", "close", str(number), "--repo", repo]
    if comment:
        args += ["--comment", outgoing(comment, key=key)]
    try:
        gh(args, write=True)
    except GhUnsure:
        def reconcile():
            status = issue_state(repo, number)
            if status == "CLOSED":
                return None
            if status != "OPEN":
                raise GhUnsure(f"GitHub issue state is {status!r}")
            found = comment and comment_key(f"repos/{repo}/issues/{number}/comments", key, start)
            return args[:5] if found else args
        retry = recovery_lookup(reconcile)
        if retry is not None:
            gh(retry, write=True)


def issue_labels(repo: str, number: int, add: list[str] = (), remove: list[str] = (), *, single_attempt=False) -> None:
    args = ["issue", "edit", str(number), "--repo", repo]
    for lb in add:
        args += ["--add-label", lb]
    for lb in remove:
        args += ["--remove-label", lb]
    if len(args) > 5:
        gh(args, write=single_attempt)


def issue_state(repo: str, number: int) -> str:
    try:
        return (gh_json(["issue", "view", str(number), "--repo", repo, "--json", "state"]) or {}).get("state", "")
    except GhError as e:
        if NO_ISSUE in str(e):
            return ""
        raise


def issue_edit_body(repo: str, number: int, body: str) -> None:
    gh(["issue", "edit", str(number), "--repo", repo, "--body-file", "-"], input=outgoing(body))


def issue_graph(repo: str, number: int, query: str, *, paginate: bool = False) -> list[dict]:
    owner, name = repo.split("/", 1)
    args = ["api", "graphql", "-f", outgoing(f"query={query}"), "-f", outgoing(f"owner={owner}"),
            "-f", outgoing(f"name={name}"), "-F", outgoing(f"number={number}")]
    try:
        result = gh_json([*args, "--paginate", "--slurp"] if paginate else args)
        pages = result if paginate else [result]
        if isinstance(pages, list) and pages and not any(page.get("errors") for page in pages):
            issues = [page["data"]["repository"]["issue"] for page in pages]
            if all(isinstance(issue, dict) for issue in issues):
                return issues
    except (json.JSONDecodeError, AttributeError, TypeError, KeyError):
        pass
    raise GhError(f"Could not read GitHub issue {repo}#{number}")


def issue_merge_commit(repo: str, number: int, base: str) -> str:
    query = """
    query($owner:String!, $name:String!, $number:Int!) {
      repository(owner:$owner, name:$name) {
        issue(number:$number) {
          state stateReason
          timelineItems(last:1, itemTypes:[CLOSED_EVENT, REOPENED_EVENT]) {
            nodes { ... on ClosedEvent { closer {
              __typename
              ... on PullRequest { ...Fix }
              ... on Commit { oid associatedPullRequests(first:10) { nodes { ...Fix } } }
            } } }
          }
        }
      }
    }
    fragment Fix on PullRequest {
      merged baseRefName repository { nameWithOwner } mergeCommit { oid }
    }
    """
    unreadable = f"Could not confirm closure of {repo}#{number}"
    issue, = issue_graph(repo, number, query)
    if issue.get("state") not in ("OPEN", "CLOSED"):
        raise GhError(unreadable)
    if (issue["state"], issue.get("stateReason")) != ("CLOSED", "COMPLETED"):
        return ""
    events = (issue.get("timelineItems") or {}).get("nodes")
    if not isinstance(events, list):
        raise GhError(unreadable)
    closer = ((events[-1] or {}).get("closer") or {}) if events else {}
    pulls = ([closer] if closer.get("__typename") == "PullRequest" else
             (closer.get("associatedPullRequests") or {}).get("nodes") or [])
    for pr in filter(None, pulls):
        sha = (pr.get("mergeCommit") or {}).get("oid") or ""
        actual_repo = (pr.get("repository") or {}).get("nameWithOwner") or ""
        if (pr.get("merged") is True and pr.get("baseRefName") == base and actual_repo.casefold() == repo.casefold()
                and re.fullmatch(r"[0-9a-f]{40}", sha) and (pr is closer or closer.get("oid") == sha)):
            return sha
    return ""


def prerequisite_pulls(repo: str, number: int) -> dict[tuple[str, int], tuple[str, str, str]]:
    query = """
    query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
      repository(owner:$owner, name:$name) {
        issue(number:$number) {
          closedByPullRequestsReferences(first:50, after:$endCursor, includeClosedPrs:true) {
            nodes { number state baseRefName repository { nameWithOwner } mergeCommit { oid } }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    unreadable = f"Could not read linked PRs for {repo}#{number}"
    pulls = {}
    try:
        for issue in issue_graph(repo, number, query, paginate=True):
            nodes = (issue.get("closedByPullRequestsReferences") or {}).get("nodes")
            if not isinstance(nodes, list):
                raise GhError(unreadable)
            for pr in filter(None, nodes):
                name = ((pr.get("repository") or {}).get("nameWithOwner") or "").casefold()
                n, state = pr.get("number"), pr.get("state")
                if (not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", name) or type(n) is not int or n < 1
                        or state not in ("OPEN", "MERGED", "CLOSED")):
                    continue
                sha = (pr.get("mergeCommit") or {}).get("oid") or ""
                sha = sha if state == "MERGED" and re.fullmatch(r"[0-9a-f]{40}", sha) else ""
                base = pr.get("baseRefName")
                pulls[name, n] = state.lower(), base if isinstance(base, str) else "", sha
    except (AttributeError, TypeError) as error:
        raise GhError(unreadable) from error
    return pulls


def issue_reopen(repo: str, number: int) -> None:
    gh(["issue", "reopen", str(number), "--repo", repo])


def pr_create(repo: str, head: str, base: str, title: str, body: str, labels: Sequence[str] = (),
              draft: bool = False, cwd=None, link: str = "", *, single_attempt: bool = False) -> str:
    args = ["pr", "create", "--repo", repo, "--head", head, "--base", base, "--title", outbound.screen(title),
            "--body-file", "-"]
    for lb in labels:
        args += ["--label", lb]
    if draft:
        args.append("--draft")
    try:
        return gh(args, cwd=cwd, input=outgoing(body, link), write=True).strip().splitlines()[-1]
    except GhError as e:
        if single_attempt or (not isinstance(e, GhUnsure) and "already exists" not in str(e).lower()):
            raise
        url = recovery_lookup(lambda: open_pr_url(repo, head)) if isinstance(e, GhUnsure) else open_pr_url(repo, head)
        if url:
            return url
        raise


def prs(repo: str, label: str = "", state: str = "open", limit: int = 50) -> list[dict]:
    args = ["pr", "list", "--repo", repo, "--state", state, "--limit", str(limit),
            "--json", "number,title,url,state,mergedAt,closedAt,headRefName,labels,body"]
    if label:
        args += ["--label", label]
    return gh_json(args) or []


def open_labeled_pr_urls(repo: str, label: str) -> list[str]:
    query = urlencode({"state": "open", "labels": label, "per_page": 100})
    pages = gh_json(["api", f"repos/{repo}/issues?{query}", "--paginate", "--slurp"]) or []
    return [row["html_url"] for page in pages for row in page if "pull_request" in row]


def open_pr_url(repo: str, head: str) -> str:
    rows = gh_json(["pr", "list", "--repo", repo, "--head", head, "--state", "open", "--limit", "1", "--json", "url"])
    return rows[0]["url"] if rows else ""


def closed_pr_heads(repo: str, head: str) -> set[str]:
    rows = gh_json(["pr", "list", "--repo", repo, "--head", head, "--state", "closed", "--limit", "20",
                    "--json", "headRefOid"]) or []
    return {r["headRefOid"] for r in rows}


def pr_body(url: str) -> str:
    return ((gh_json(["pr", "view", url, "--json", "body"]) or {}).get("body") or "").replace("\r\n", "\n")


def pr_diff(url: str) -> str:
    return gh(["pr", "diff", url])


def pr_edit_body(url: str, body: str, link: str = "") -> None:
    pr_write_body(url, outgoing(body, link))


def pr_write_body(url: str, body: Outgoing) -> None:
    gh(["pr", "edit", url, "--body-file", "-"], input=body)


def pr_comment(url: str, body: str, *, key: str = "", single_attempt: bool = False) -> None:
    keyed_comment(["pr", "comment", url, "--body-file", "-"], body,
                  f"repos/{pr_repo(url)}/issues/{pr_number(url)}/comments", mark=True,
                  key=key, single_attempt=single_attempt)


def find_own_pr_comment(repo: str, number: int, *markers: str) -> dict | None:
    author = me()
    if not author:
        return None
    own = [(row, (row.get("body") or "").splitlines())
           for row in api_list(f"repos/{repo}/issues/{number}/comments")
           if (row.get("user") or {}).get("login") == author]
    return next((row for marker in markers for row, lines in own if marker in lines), None)


def pr_edit_comment(repo: str, comment_id: int, body: str, *, key: str = "") -> None:
    payload = Outgoing(json.dumps({"body": outgoing(body, mark=True, key=key)}))
    gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "--input", "-"],
       input=payload, write=True)


def pr_reply(url: str, comment_id: int, body: str) -> None:
    keyed_comment(["api", f"repos/{pr_repo(url)}/pulls/{pr_number(url)}/comments/{comment_id}/replies", "-X", "POST",
                   "--input", "-"], body, f"repos/{pr_repo(url)}/pulls/{pr_number(url)}/comments",
                  mark=True, envelope=True)


def ensure_branch(repo: str, branch: str) -> None:
    try:
        gh(["api", f"repos/{repo}/git/ref/heads/{branch}", "--jq", ".object.sha"])
        return
    except GhError as e:
        if "HTTP 404" not in str(e):
            raise
    sha = branch_sha(repo, default_branch(repo))
    try:
        gh(["api", "-X", "POST", f"repos/{repo}/git/refs", "--input", "-"],
           input=Outgoing(json.dumps({"ref": f"refs/heads/{branch}", "sha": sha})))
    except GhError as e:
        if "Reference already exists" not in str(e):
            raise


def file_sha(repo: str, path: str, ref: str) -> str:
    try:
        return gh(["api", f"repos/{repo}/contents/{path}?ref={ref}", "--jq", ".sha"]).strip()
    except GhError as e:
        if "HTTP 404" in str(e):
            return ""
        raise


def put_file(repo: str, path: str, data: bytes, branch: str, message: str, replaces: str = "") -> str:
    payload = {"message": message, "content": base64.b64encode(data).decode(), "branch": branch,
               **({"sha": replaces} if replaces else {})}
    out = gh(["api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-"],
             input=Outgoing(json.dumps(payload)), timeout=600)
    return json.loads(out)["commit"]["sha"]


def blob_url(repo: str, commit: str, path: str) -> str:
    return f"https://github.com/{repo}/blob/{commit}/{path}"


def api_list(path: str) -> list[dict]:
    pages = gh_json(["api", f"{path}?per_page=100", "--paginate", "--slurp"]) or []
    return [item for page in pages for item in page]


DEPENDABOT_OFF = "Dependabot alerts are disabled"


def open_dependabot_alerts(repo: str) -> list[dict]:
    try:
        return [a for a in api_list(f"repos/{repo}/dependabot/alerts") if a.get("state") == "open"]
    except GhError as e:
        if DEPENDABOT_OFF in str(e):
            return []
        raise


@dataclass
class Discussion:
    line_comments: list[dict]
    reviews: list[dict]
    comments: list[dict]


def pr_discussion(url: str) -> Discussion:
    base = f"repos/{pr_repo(url)}"
    n = pr_number(url)
    return Discussion(api_list(f"{base}/pulls/{n}/comments"), api_list(f"{base}/pulls/{n}/reviews"),
                      api_list(f"{base}/issues/{n}/comments"))


@functools.cache
def me() -> str:
    return (gh_json(["api", "user"]) or {}).get("login", "")


def pr_number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def pr_view(url_or_number: str, repo: str = "") -> dict:
    args = ["pr", "view", str(url_or_number), "--json",
            "number,state,mergedAt,mergeCommit,closedAt,updatedAt,url,headRefName,headRefOid,baseRefName"]
    if repo:
        args += ["--repo", repo]
    return gh_json(args) or {}


def pr_repo(url: str) -> str:
    return "/".join(url.split("/")[3:5])


def branch_contains(repo: str, branch: str, sha: str) -> bool:
    try:
        status = gh(["api", f"repos/{repo}/compare/{branch}...{sha}", "--jq", ".status"]).strip()
    except GhError:
        return False
    return status in ("identical", "behind")


def merged_pr_names(repo: str, branch: str, text: str) -> bool:
    try:
        found = gh_json(["pr", "list", "--repo", repo, "--state", "merged", "--base", branch,
                         "--search", f'"{text}"', "--json", "number", "--limit", "1"])
    except GhError:
        return False
    return bool(found)


def pr_set_draft(url: str, draft: bool, *, single_attempt: bool = False) -> None:
    gh(["pr", "ready", url, *(["--undo"] if draft else [])], write=single_attempt, retry=not single_attempt)


def pr_close(url: str) -> None:
    gh(["pr", "close", url], write=True)


def pr_add_label(url: str, name: str) -> None:
    gh(["pr", "edit", url, "--add-label", name], write=True)


def pr_update_branch(repo: str, number: int, head: str) -> None:
    gh(["api", "-X", "PUT", f"repos/{repo}/pulls/{number}/update-branch", "--input", "-"],
       input=Outgoing(json.dumps({"expected_head_sha": head})), write=True)


def pr_squash(repo: str, number: int, head: str) -> None:
    gh(["pr", "merge", str(number), "--repo", repo, "--squash", "--match-head-commit", head], write=True)


def actions_check_passed(repo: str, head: str, name: str) -> bool:
    query = urlencode({"check_name": name, "filter": "latest", "per_page": 100})
    path = f"repos/{repo}/commits/{head}/check-runs?{query}"
    reply = json.loads(gh(["api", path], retry=False) or "{}")
    rows = reply.get("check_runs", [])
    if reply.get("total_count", len(rows)) > len(rows):
        return False
    runs = [row for row in rows if row.get("name") == name
            and row.get("head_sha") == head and (row.get("app") or {}).get("id") == GITHUB_ACTIONS_APP_ID]
    return bool(runs) and all(row.get("status") == "completed" and row.get("conclusion") == "success" for row in runs)


def pr_draft_and_head(url: str) -> tuple[bool, str]:
    row = gh_json(["pr", "view", url, "--json", "isDraft,headRefOid"]) or {}
    return bool(row.get("isDraft")), str(row.get("headRefOid") or "")


MERGE_WAIT_SECONDS = 120
MERGE_POLL_SECONDS = 5
MOVED_BASE_STATES = {"BEHIND", "DIRTY"}
BASE_MERGE_REJECTION = r"(?:Pull Request is not mergeable|Base branch was modified\. Review and try the merge again)\.?"
BASE_MERGE_HTTP_REJECTION = re.compile(rf"(?:^|: ){BASE_MERGE_REJECTION}\s*\(HTTP (?:405|409)\)$", re.IGNORECASE)


class MovedBaseHold(str):
    pass


class BaseTipChanged(str):
    pass


def pr_merge_details(pr: str, repo: str = "", *, timeout: float = 120, retry: bool = True,
                     watch_checks: bool = False, identity: bool = False) -> dict:
    fields = "state,isDraft,headRefOid,baseRefName,baseRefOid,mergeStateStatus,mergeable,title,reviewDecision"
    if identity:
        fields += ",url,headRefName,isCrossRepository"
    if watch_checks:
        fields += ",statusCheckRollup"
    args = ["pr", "view", pr, "--json", fields]
    if repo:
        args += ["--repo", repo]
    return gh_json(args, timeout=timeout, retry=retry) or {}


def merge_ref_hold(row: dict, head: str, base: str) -> str:
    if row.get("state") != "OPEN" or row.get("isDraft") is not False:
        return "the PR is not open and ready for review"
    if row.get("headRefOid") != head:
        return "the PR head changed after verification"
    if row.get("baseRefName") != base:
        return "the PR base changed after verification"
    return ""


def merge_ci_passed(repo: str, head: str, *, required: dict | None = None, stage_checks: dict | None = None) -> bool:
    path = f"repos/{repo}/commits/{head}"
    checks = gh_json(["api", f"{path}/check-runs?filter=latest&per_page=100", "--paginate", "--slurp"])
    statuses = gh_json(["api", f"{path}/statuses?per_page=100", "--paginate", "--slurp"])
    if (not isinstance(checks, list) or not checks
            or any(not isinstance(page, dict) or not isinstance(page.get("check_runs"), list) for page in checks)
            or not isinstance(statuses, list) or not statuses):
        return False
    runs = [row for page in checks for row in page["check_runs"]]
    if required and not any(row.get("head_sha") == head and row.get("name") == required["context"]
                            and (row.get("app") or {}).get("id") == required["app_id"]
                            and row.get("status") == "completed" and row.get("conclusion") == "success" for row in runs):
        return False
    skippable = set()
    if stage_checks:
        issuer = stage_checks["app_id"]
        seen = {row.get("name") for row in runs if row.get("head_sha") == head and (row.get("app") or {}).get("id") == issuer
                and row.get("status") == "completed" and row.get("conclusion") == "success"}
        if not seen.issuperset(stage_checks["required"]):
            return False
        skippable = {(name, issuer, head) for name in stage_checks["allow_skipped"]}
    contexts = {}
    for page in statuses:
        if not isinstance(page, list):
            return False
        for row in page:
            contexts.setdefault(row["context"], row["state"])
    def passing(row):
        if row.get("status") != "completed":
            return False
        identity = (row.get("name"), (row.get("app") or {}).get("id"), row.get("head_sha"))
        return row.get("conclusion") == "success" or (row.get("conclusion") == "skipped" and identity in skippable)

    return bool(runs or contexts) and all(map(passing, runs)) and all(state == "success" for state in contexts.values())


def deployment_ci_refusal(repo: str, head: str, required: dict, *, timeout: float) -> str:
    token = _deployment_ci_deadline.set(time.monotonic() + timeout)
    try:
        return "" if merge_ci_passed(repo, head, required=required) else _required_ci_refusal(repo, head, required)
    except (GhError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return "CI unavailable"
    finally:
        _deployment_ci_deadline.reset(token)


def _required_ci_refusal(repo: str, head: str, required: dict) -> str:
    query = urlencode({"filter": "latest", "per_page": 100, "check_name": required["context"]})
    pages = gh_json(["api", f"repos/{repo}/commits/{head}/check-runs?{query}", "--paginate", "--slurp"])
    runs = [row for page in pages for row in page["check_runs"]
            if row.get("head_sha") == head and row.get("name") == required["context"]
            and (row.get("app") or {}).get("id") == required["app_id"]]
    if any(row["status"] == "completed" and row["conclusion"] != "success" for row in runs):
        return "CI failed"
    if not runs or any(row["status"] != "completed" for row in runs):
        return "CI pending"
    return "CI not passing"


def merge_readiness(repo: str, number: int, head: str, base: str, base_sha: str, *, wait: float = 0,
                    required: dict | None = None, track_moved_base: bool = False, stage_checks: dict | None = None) -> tuple[str, str]:
    if not head or not base or not base_sha:
        return "", "the checked head and base are not known"
    deadline = time.monotonic() + wait
    while True:
        row = pr_merge_details(str(number), repo)
        if hold := merge_ref_hold(row, head, base):
            return "", hold
        if row.get("reviewDecision") in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            return "", f"GitHub review is {row['reviewDecision']}"
        state, mergeable = row.get("mergeStateStatus"), row.get("mergeable")
        if track_moved_base and state in MOVED_BASE_STATES:
            return "", MovedBaseHold(f"GitHub reports merge state {state}, mergeable {mergeable}")
        if row.get("baseRefOid") != base_sha:
            hold = "the PR base changed after verification"
            return "", BaseTipChanged(hold) if track_moved_base else hold
        remaining = deadline - time.monotonic()
        if "UNKNOWN" not in (state, mergeable) or remaining <= 0:
            break
        time.sleep(min(MERGE_POLL_SECONDS, remaining))
    if state != "CLEAN" or mergeable != "MERGEABLE":
        return "", f"GitHub reports merge state {state}, mergeable {mergeable}"
    if not merge_ci_passed(repo, head, required=required, **({"stage_checks": stage_checks} if stage_checks else {})):
        return "", "GitHub has no fully passing CI result for the verified head"
    return str(row.get("title") or "").strip(), ""


def merge_rejection_hold(repo: str, number: int, head: str, base: str, base_sha: str) -> str:
    row = pr_merge_details(str(number), repo)
    if hold := merge_ref_hold(row, head, base):
        return hold
    if row.get("baseRefOid") and row["baseRefOid"] != base_sha:
        return BaseTipChanged("GitHub rejected the merge after the PR base changed")
    return ""


def pr_merge_auto(repo: str, number: int, head: str, base: str, base_sha: str, *, track_moved_base: bool = False,
                  required: dict | None = None, stage_checks: dict | None = None) -> str:
    options = {"required": required} if required else {}
    if stage_checks:
        options["stage_checks"] = stage_checks
    subject, hold = merge_readiness(repo, number, head, base, base_sha, track_moved_base=track_moved_base, **options)
    if hold:
        return hold
    payload = {"sha": head, "merge_method": "squash"}
    if subject:
        payload["commit_title"] = f"{subject} (#{number})"
    try:
        reply = gh(["api", "-X", "PUT", f"repos/{repo}/pulls/{number}/merge", "--input", "-"],
                   input=Outgoing(json.dumps(payload)), write=True)
    except GhError as error:
        if track_moved_base and not isinstance(error, GhUnsure) and BASE_MERGE_HTTP_REJECTION.search(str(error)):
            if hold := merge_rejection_hold(repo, number, head, base, base_sha):
                return hold
        raise
    if not track_moved_base:
        result = json.loads(reply or "{}")
        return "" if result.get("merged") is True else "GitHub did not confirm the merge"
    try:
        result = json.loads(reply)
    except ValueError:
        raise GhUnsure("GitHub did not return a definite merge outcome") from None
    if not isinstance(result, dict) or type(result.get("merged")) is not bool:
        raise GhUnsure("GitHub did not return a definite merge outcome")
    if result["merged"]:
        return ""
    if re.fullmatch(BASE_MERGE_REJECTION, str(result.get("message", "")).strip(), re.IGNORECASE):
        if hold := merge_rejection_hold(repo, number, head, base, base_sha):
            return hold
    return "GitHub rejected the merge"


def default_branch(repo: str, *, fallback: str = "main", timeout: float = 120, retry: bool = True) -> str:
    try:
        row = gh_json(["repo", "view", repo, "--json", "defaultBranchRef"], timeout=timeout, retry=retry) or {}
        return row["defaultBranchRef"]["name"] or fallback
    except (GhError, KeyError, TypeError):
        return fallback


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


APP_KEY = "GITHUB_APP_PRIVATE_KEY"


def key_pipe_sign(key, data, timeout):
    read_fd, write_fd = os.pipe()

    def feed():
        with suppress(BrokenPipeError), os.fdopen(write_fd, "wb") as pipe:
            pipe.write(key)

    writer = threading.Thread(target=feed, name="app-key-pipe")
    writer.start()
    try:
        return subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_fd}"], input=data,
                              pass_fds=(read_fd,), capture_output=True, timeout=timeout, check=False)
    finally:
        os.close(read_fd)
        writer.join()


def app_key(app):
    if not paths.ISOLATED and (key := os.environ.get(APP_KEY)):
        return key.encode()
    try:
        row, key = secretstore.lookup("global", APP_KEY)
        if row is not None:
            return key.encode()
        if old := secret_move.fallback(app.private_key):
            if not old.resolve().is_relative_to(paths.ZTE_AUTHORITY.resolve()):
                return paths.read_confined(old)
    except (OSError, ValueError):
        pass
    return b""


def key_signature(app: config.GithubApp, signing_input: bytes, *, timeout: float = 30, key=None) -> bytes:
    address = secretstore.address("global", APP_KEY)
    key = app_key(app) if key is None else key
    if not key:
        raise GhError(f"No usable GitHub App signing key at {address}")
    return sign_result(lambda: key_pipe_sign(key, signing_input, timeout), address)


def sign_result(run, source):
    try:
        res = run()
    except (OSError, subprocess.SubprocessError):
        raise GhError(f"openssl could not sign with {source}") from None
    if res.returncode != 0:
        raise GhError(f"openssl could not sign with {source} (exit {res.returncode})")
    return res.stdout


def app_jwt(app: config.GithubApp, *, timeout: float = 30, key=None, key_file=None) -> str:
    now = int(time.time())
    signed = ".".join(base64url(json.dumps(part, separators=(",", ":")).encode()) for part in (
        {"alg": "RS256", "typ": "JWT"}, {"iat": now - 60, "exp": now + 540, "iss": app.app_id}))
    if key_file is not None:
        if paths.ISOLATED and paths.confined(key_file) is None:
            raise GhError(paths.ISOLATED_LOGINS)
        signature = sign_result(lambda: subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_file)], input=signed.encode(), capture_output=True,
            timeout=timeout, check=False), "the release supervisor App key")
    else:
        signature = key_signature(app, signed.encode(), timeout=timeout, **({"key": key} if key is not None else {}))
    return f"{signed}.{base64url(signature)}"


def clone_token(app: config.GithubApp, repo: str) -> str:
    where = f"for {repo} from [github_app] installation {app.installation_id}"
    try:
        reply = token_reply(app, repo, retry=True)
    except urllib.error.HTTPError as e:
        raise GhError(f"GitHub gave no token {where}: HTTP {e.code}: {http_message(e)}") from None
    except OSError as e:
        raise GhError(f"GitHub gave no token {where}: {getattr(e, 'reason', None) or e}") from None
    if not (token := reply.get("token")):
        raise GhError(f"GitHub gave no token {where}: its reply held none")
    if problem := token_overreach(reply, repo):
        raise GhError(f"GitHub gave a token {where} that is not read only on that one repository, so arete "
                      f"discarded it: {problem}")
    return token


def token_overreach(reply: dict, repo: str) -> str:
    permissions = reply.get("permissions")
    if not isinstance(permissions, dict) or not permissions:
        return "its reply named no permissions"
    if wider := sorted(name for name, level in permissions.items() if level != "read"):
        return f"it grants more than read on {', '.join(wider)}"
    if reply.get("repository_selection") == "all":
        return "it reaches every repository of the installation"
    listed = reply.get("repositories")
    if listed is not None and (not isinstance(listed, list) or
                               [str(r.get("full_name", "")).lower() for r in listed if isinstance(r, dict)] != [repo.lower()]):
        return "it reaches repositories other than this one"
    return ""


def token_reply(app: config.GithubApp, repo: str, retry: bool) -> dict:
    if not paths.ISOLATED and (key := os.environ.get(APP_KEY)):
        row = None
        jwt = app_jwt(app, key=key.encode())
    else:
        row, key = secretstore.lookup("global", APP_KEY)
        jwt = app_jwt(app, key=key.encode()) if row is not None else app_jwt(app)
    body = {"repositories": [repo.rsplit("/", 1)[-1]], "permissions": CLONE_TOKEN_PERMISSIONS}
    request = urllib.request.Request(
        f"{GITHUB_API}/app/installations/{app.installation_id}/access_tokens", data=json.dumps(body).encode(),
        method="POST", headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "arete"})
    try:
        with urllib.request.urlopen(request, timeout=30) as res:
            reply = json.loads(res.read())
    except urllib.error.HTTPError:
        raise
    except OSError:
        if not (retry and util.wait_for_network()):
            raise
        return token_reply(app, repo, retry=False)
    except ValueError:
        return {}
    if not isinstance(reply, dict):
        return {}
    if reply.get("token"):
        accounts.record_workspace(row, "github", "app")
    return reply


def http_message(e: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(e.read()).get("message") or e.reason)
    except (ValueError, OSError, AttributeError):
        return str(e.reason)


def repo_private(repo: str) -> bool | None:
    try:
        return bool((gh_json(["repo", "view", repo, "--json", "isPrivate"]) or {}).get("isPrivate"))
    except GhError:
        return None


PASSING_CONCLUSIONS = {"success", "neutral"}
STATUS_STATES = {"success": "pass", "pending": "pending", "failure": "fail", "error": "fail"}
CheckKey = tuple[str, str]


@dataclass
class Check:
    name: str
    state: str
    updated: str = ""
    link: str = ""
    job: int = 0
    description: str = ""
    check_run_id: int = 0
    source: str = "status"
    workflow_run_id: int = 0
    details_url: str = ""
    actions_only: bool = False

    @property
    def key(self) -> CheckKey:
        if self.actions_only:
            return f"actions-job:{self.job}", self.name
        if self.workflow_run_id:
            return f"workflow-job:{self.workflow_run_id}", self.name
        return self.context_key

    @property
    def context_key(self) -> CheckKey:
        return self.source, self.name

    def actions_workflow(self, repo: str) -> int:
        from_link = actions_run_id(repo, self.link)
        from_details = actions_run_id(repo, self.details_url)
        if from_link and from_details and from_link != from_details:
            return 0
        return from_link or from_details


def check_state(row: dict) -> str:
    if row.get("status") != "completed":
        return "pending"
    if row.get("conclusion") == "skipped":
        return "skipped"
    return "pass" if row.get("conclusion") in PASSING_CONCLUSIONS else "fail"


def check_from_run(row: dict) -> Check:
    app = row.get("app") or {}
    actions = app.get("slug") == "github-actions"
    details_url = row.get("details_url") or ""
    return Check(row["name"], check_state(row), row.get("completed_at") or row.get("started_at") or "",
                 row.get("html_url") or details_url, row["id"] if actions else 0,
                 ((row.get("output") or {}).get("title") or "").strip(), row["id"],
                 f"app:{app.get('id') or app.get('slug') or ''}", details_url=details_url)


def check_from_status(row: dict) -> Check:
    return Check(row["context"], STATUS_STATES.get(row.get("state"), "fail"), row.get("updated_at") or "",
                 row.get("target_url") or "", 0, (row.get("description") or "").strip())


def commit_checks(repo: str, ref: str, *, timeout: float = 120, retry: bool = True,
                  red_workflows: set[int] | None = None, latest: bool = False) -> dict[CheckKey, Check]:
    path = f"repos/{repo}/commits/{ref}"
    filter_runs = "latest" if latest else "all"
    pages = gh_json(["api", f"{path}/check-runs?filter={filter_runs}&per_page=100", "--paginate", "--slurp"],
                    timeout=timeout, retry=retry)
    if not isinstance(pages, list) or not pages or any(
            not isinstance(page, dict) or not isinstance(page.get("check_runs"), list) for page in pages):
        raise GhError("GitHub did not return check runs")
    runs = sorted((check_from_run(r) for page in pages for r in page["check_runs"]), key=lambda c: c.check_run_id)
    statuses = gh_json(["api", f"{path}/statuses?per_page=100", "--paginate", "--slurp"], timeout=timeout, retry=retry)
    if not isinstance(statuses, list) or not statuses or any(
            not isinstance(page, list) for page in statuses):
        raise GhError("GitHub did not return commit statuses")
    newest = ({c.key: c for c in runs} if red_workflows is None
              else current_workflow_checks(repo, ref, runs, red_workflows, timeout=timeout))
    # GitHub returns commit statuses in reverse chronological order.
    for c in (check_from_status(row) for page in statuses for row in page):
        newest.setdefault(c.key, c)
    return newest


def publication_ci_passed(repo: str, head: str, *,
                          actions_failures: Sequence[tuple[int, str] | tuple[int, str, int]] = ()) -> bool:
    checks = commit_checks(repo, head, latest=True, red_workflows=set(), retry=False)
    if any(check.state not in {"pass", "skipped"} for check in checks.values()):
        return False
    current_jobs = [(check.job, check.name, check.actions_workflow(repo)) for check in checks.values() if check.job]
    unresolved = set()
    for job, name, *workflow in actions_failures:
        run = workflow[0] if workflow else 0
        if not name:
            row = actions_job(repo, head, job, retry=False)
            name, run = row["name"], row.get("run_id")
            if not run:
                return False
        if job and any(newer > job and current_name == name and (not run or current_run == run)
                       for newer, current_name, current_run in current_jobs):
            continue
        if not run and job:
            run = actions_job(repo, head, job, retry=False).get("run_id")
        if type(run) is not int or run <= 0:
            return False
        unresolved.add(run)
    unresolved.update(row["id"] for row in head_workflow_runs(repo, head, retry=False))
    for run in sorted(unresolved):
        row = workflow_run(repo, head, run, retry=False)
        if row.get("status") != "completed":
            return False
        failures, _ = workflow_job_failures(repo, head, run, row["run_attempt"], retry=False)
        if failures or check_state(row) == "fail":
            return False
    return bool(checks or unresolved)


def run_number(row: dict) -> int:
    return row["run_number"] if type(row.get("run_number")) is int else 0


def head_workflow_runs(repo: str, head: str, **options) -> list[dict]:
    query = urlencode({"head_sha": head, "per_page": 100})
    latest: dict[object, dict] = {}
    for row in actions_rows(f"repos/{repo}/actions/runs?{query}", "workflow_runs", **options):
        if not isinstance(row, dict) or row.get("head_sha") != head or type(row.get("id")) is not int:
            continue
        key = row.get("workflow_id") or ("run", row["id"])
        if key not in latest or run_number(row) >= run_number(latest[key]):
            latest[key] = row
    return list(latest.values())


def current_workflow_checks(repo: str, head: str, runs: list[Check], red_workflows: set[int], *,
                            timeout: float) -> dict[CheckKey, Check]:
    red_workflows.clear()
    red_workflows.update(c.actions_workflow(repo) for c in runs if c.state == "fail")
    current, superseded = {}, set()
    for run in sorted(red_workflows - {0}):
        failures, replaced = current_workflow_failures(repo, head, run, timeout=timeout, retry=False)
        superseded.update(replaced)
        current.update((job, (run, name)) for job, name in failures.items())
    runs = [c for c in runs if c.state != "fail" or c.job not in superseded]
    red_workflows.intersection_update(c.actions_workflow(repo) for c in runs if c.state == "fail")
    red_workflows.update(run for run, _ in current.values())
    checks = {c.key: c for c in runs}
    reported = {c.job for c in checks.values() if c.state == "fail"}
    for job in sorted(current.keys() - reported):
        run, name = current[job]
        check = Check(name, "fail", link=f"https://github.com/{repo}/actions/runs/{run}/job/{job}", job=job,
                      check_run_id=job, source=f"app:{GITHUB_ACTIONS_APP_ID}", actions_only=True)
        checks[check.key] = check
    return checks


def branch_sha(repo: str, branch: str) -> str:
    return gh(["api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"]).strip()


def origin_project(url: str) -> str:
    match = re.fullmatch(
        r"(?:(?:https://(?:[A-Za-z0-9._~%:-]+@)?github\.com(?::443)?"
        r"|ssh://git@(?:github\.com(?::22)?|ssh\.github\.com:443))/|git@github\.com:)"
        r"([A-Za-z0-9-]+/[A-Za-z0-9_.-]+)", url, re.IGNORECASE | re.ASCII)
    return match[1].casefold().removesuffix(".git") if match else ""


def branch_heads(projects: set[str], branch: str) -> dict[str, str]:
    aliases = {f"part{i}": project for i, project in enumerate(sorted(projects))}
    selections = []
    for alias, project in aliases.items():
        owner, name = project.split("/")
        selections.append(f'{alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) '
                          '{ nameWithOwner ref(qualifiedName: $ref) { prefix name target { oid } } }')
    query = "query($ref: String!) { " + " ".join(selections) + " }"
    payload = Outgoing(json.dumps({"query": query, "variables": {"ref": f"refs/heads/{branch}"}}))
    reply = json.loads(gh(["api", "graphql", "--hostname", "github.com", "--input", "-"],
                          input=payload, timeout=10, retry=False))
    incomplete = "GitHub did not return complete parked branch heads"
    if (not isinstance(reply, dict) or reply.get("errors", []) != []
            or not isinstance(data := reply.get("data"), dict)):
        raise GhError(incomplete)
    found = {}
    for alias, project in aliases.items():
        repo = data.get(alias)
        if (not isinstance(repo, dict) or not isinstance(name := repo.get("nameWithOwner"), str)
                or name.casefold() != project
                or "ref" not in repo):
            raise GhError(incomplete)
        ref = repo["ref"]
        if ref is None:
            found[project] = ""
            continue
        if (not isinstance(ref, dict) or ref.get("prefix") != "refs/heads/" or ref.get("name") != branch
                or not isinstance(target := ref.get("target"), dict)
                or not isinstance(oid := target.get("oid"), str) or not re.fullmatch(r"[0-9a-f]{40}", oid)):
            raise GhError(incomplete)
        found[project] = oid
    return found


def job_log(repo: str, job: int) -> str:
    return gh(["api", f"repos/{repo}/actions/jobs/{job}/logs"], timeout=300)


@dataclass(frozen=True)
class WorkflowRerun:
    run: int
    attempt: int
    jobs: dict[str, int]
    unrelated: dict[str, str] = field(default_factory=dict)

    @property
    def check_keys(self) -> set[CheckKey]:
        return {(f"app:{GITHUB_ACTIONS_APP_ID}", name) for name in self.jobs}


def workflow_lists_pr(pulls, number: int) -> bool:
    return (isinstance(pulls, list) and len(pulls) == 1 and isinstance(pulls[0], dict)
            and type(pulls[0].get("number")) is int and pulls[0]["number"] == number)


def workflow_belongs_to_pr(row: dict, number: int) -> bool:
    return row.get("event") == "pull_request" and workflow_lists_pr(row.get("pull_requests"), number)


def workflow_belongs_to_head_branch(row: dict, repo: str, number: int, head: str, **options) -> bool:
    pulls = row.get("pull_requests")
    if not row.get("head_branch") or not (pulls == [] or row.get("event") == "push" and isinstance(pulls, list)):
        return False
    pull = gh_json(["api", f"repos/{repo}/pulls/{number}"], **options) or {}
    ref = pull.get("head") or {}
    branch_repo = (ref.get("repo") or {}).get("full_name")
    run_repo = (row.get("head_repository") or {}).get("full_name")
    return (type(pull.get("number")) is int and pull["number"] == number and pull.get("state") == "open"
            and ref.get("sha") == head and ref.get("ref") == row["head_branch"]
            and isinstance(branch_repo, str) and isinstance(run_repo, str)
            and bool(branch_repo) and branch_repo.casefold() == run_repo.casefold())


def workflow_run(repo: str, head: str, run: int, *, pr: str = "", allow_head_branch: bool = False, attempt: int = 0,
                 timeout: float = 120, retry: bool = True) -> dict:
    path = f"repos/{repo}/actions/runs/{run}" + (f"/attempts/{attempt}" if attempt else "")
    row = gh_json(["api", path], timeout=timeout, retry=retry) or {}
    actual = row.get("run_attempt")
    if (type(row.get("id")) is not int or row["id"] != run or row.get("head_sha") != head
            or type(actual) is not int or actual <= 0
            or (attempt and actual != attempt)):
        raise GhError(f"GitHub did not identify the workflow attempt for run {run} on {head}")
    if pr:
        number = pr_number(pr)
        if pr_repo(pr).casefold() != repo.casefold() or not (
                workflow_belongs_to_pr(row, number) or allow_head_branch and workflow_belongs_to_head_branch(
                    row, repo, number, head, timeout=timeout, retry=retry)):
            raise WorkflowNotForPR(f"GitHub did not identify workflow run {run} as belonging to {pr}")
    return row


def actions_rows(path: str, field: str, *, timeout: float = 120, retry: bool = True) -> list[dict]:
    pages = gh_json(["api", path, "--paginate", "--slurp"], timeout=timeout, retry=retry)
    if not isinstance(pages, list) or not pages or any(
            not isinstance(page, dict) or not isinstance(page.get(field), list) for page in pages):
        raise GhError(f"GitHub did not return Actions {field}")
    return [row for page in pages for row in page[field]]


def workflow_attempt_jobs(repo: str, head: str, run: int, attempt: int, *,
                          timeout: float = 120, retry: bool = True) -> list[dict]:
    path = f"repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100"
    return [row for row in actions_rows(path, "jobs", timeout=timeout, retry=retry)
            if row.get("run_id") == run and row.get("head_sha") == head
            and row.get("run_attempt", attempt) == attempt
            and type(row.get("id")) is int and row["id"] > 0
            and isinstance(row.get("name"), str) and row["name"]]


def failed_workflow_jobs(repo: str, head: str, run: int, attempt: int) -> dict[int, str]:
    failed, _ = workflow_job_failures(repo, head, run, attempt)
    return failed


def current_workflow_failures(repo: str, head: str, run: int, **options) -> tuple[dict[int, str], set[int]]:
    try:
        row = workflow_run(repo, head, run, **options)
        if row["run_attempt"] > 1:
            return workflow_job_failures(repo, head, run, row["run_attempt"], **options)
    except (GhError, OSError, KeyError, TypeError, ValueError, AttributeError):
        pass
    return {}, set()


def workflow_job_failures(repo: str, head: str, run: int, attempt: int, **options) -> tuple[dict[int, str], set[int]]:
    path = f"repos/{repo}/actions/runs/{run}/jobs?filter=all&per_page=100"
    pages = gh_json(["api", path, "--paginate", "--slurp"], **options)
    incomplete = f"GitHub did not return complete job results for workflow run {run} on {head}"
    if not isinstance(pages, list) or not pages or any(
            not isinstance(page, dict) or not isinstance(page.get("jobs"), list) for page in pages):
        raise GhError(incomplete)
    rows = [row for page in pages for row in page["jobs"]]
    if not rows or any(type(page.get("total_count")) is not int or page["total_count"] != len(rows) for page in pages):
        raise GhError(incomplete)
    seen, attempts = set(), set()
    jobs_by_name: dict[str, dict[int, list[dict]]] = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("run_id")) is not int or row["run_id"] != run
                or row.get("head_sha") != head or type(row.get("run_attempt")) is not int
                or not 0 < row["run_attempt"] <= attempt or row.get("status") != "completed"
                or not isinstance(row.get("conclusion"), str) or not row["conclusion"]
                or not isinstance(row.get("name"), str) or not row["name"]
                or type(row.get("id")) is not int or row["id"] <= 0 or row["id"] in seen):
            raise GhError(incomplete)
        job_attempt = row["run_attempt"]
        seen.add(row["id"])
        attempts.add(job_attempt)
        jobs_by_name.setdefault(row["name"], {}).setdefault(job_attempt, []).append(row)
    if attempt not in attempts:
        raise GhError(incomplete)
    failed, superseded = {}, set()
    for name, attempts_for_job in jobs_by_name.items():
        if len(attempts_for_job) > 1 and any(len(jobs) > 1 for jobs in attempts_for_job.values()):
            raise GhError(f"GitHub returned ambiguous attempts for jobs named {name} in workflow run {run}")
        latest = max(attempts_for_job)
        for job_attempt, jobs in attempts_for_job.items():
            failures = [job["id"] for job in jobs if check_state(job) == "fail"]
            if job_attempt == latest:
                failed.update((job, name) for job in failures)
            else:
                superseded.update(failures)
    return failed, superseded


def latest_pr_workflows(repo: str, pr: str, head: str, **options) -> dict[int, dict]:
    query = urlencode({"event": "pull_request", "head_sha": head, "per_page": 100})
    number = pr_number(pr)
    candidates = [row for row in actions_rows(f"repos/{repo}/actions/runs?{query}", "workflow_runs", **options)
                  if row.get("head_sha") == head and workflow_belongs_to_pr(row, number)]
    return {row["workflow_id"]: row for row in sorted(candidates, key=lambda row: row["run_number"])}


def actions_job(repo: str, head: str, job: int, **options) -> dict:
    row = gh_json(["api", f"repos/{repo}/actions/jobs/{job}"], **options) or {}
    name = row.get("name")
    if row.get("id") != job or row.get("head_sha") != head or not isinstance(name, str) or not name:
        raise GhError(f"GitHub did not identify the Actions job {job} on {head}")
    return row


def failed_job_reruns(repo: str, pr: str, head: str, jobs: list[int]) -> list[WorkflowRerun]:
    if not jobs or pr_repo(pr).casefold() != repo.casefold():
        return []
    names = {actions_job(repo, head, job)["name"] for job in jobs}

    reruns = []
    for current in latest_pr_workflows(repo, pr, head).values():
        if not current.get("conclusion") or check_state(current) != "fail":
            continue
        run, attempt = current.get("id"), current.get("run_attempt")
        if type(run) is not int or run <= 0 or type(attempt) is not int or attempt <= 0:
            raise GhError(f"GitHub did not identify the workflow attempt for PR {pr_number(pr)} on {head}")
        failed = {}
        for row in workflow_attempt_jobs(repo, head, run, attempt):
            if row.get("conclusion") and check_state(row) == "fail":
                name, job = row["name"], row["id"]
                if failed.setdefault(name, job) != job:
                    raise GhError(f"GitHub returned multiple failed jobs named {name} in workflow run {run}")
        if names.intersection(failed):
            reruns.append(WorkflowRerun(run, attempt + 1, failed))
    return reruns


def workflow_check(run: dict) -> Check:
    attempt, conclusion = run["run_attempt"], run.get("conclusion")
    state, description = "pending", f"Workflow attempt {attempt} has no terminal result yet"
    if run.get("status") == "completed" and conclusion:
        state = "pass" if conclusion == "success" else "fail"
        description = f"Workflow attempt {attempt} concluded {conclusion}"
    return Check(run.get("name") or "GitHub Actions", state, run.get("updated_at") or "", run.get("html_url") or "",
                 description=description, source=f"workflow:{run['id']}")


def rerun_checks(repo: str, pr: str, head: str, rerun: WorkflowRerun, run: dict, **options) -> dict[CheckKey, Check]:
    if run["run_attempt"] > rerun.attempt:
        run = workflow_run(repo, head, rerun.run, pr=pr, allow_head_branch=bool(rerun.unrelated),
                           attempt=rerun.attempt, **options)
    if run["run_attempt"] != rerun.attempt or (workflow := workflow_check(run)).state == "pending":
        return {}
    try:
        rows = workflow_attempt_jobs(repo, head, rerun.run, rerun.attempt, **options)
    except GhError:
        if workflow.state == "fail":
            return {workflow.key: workflow}
        raise
    found: dict[CheckKey, Check] = {}
    for row in rows:
        name, job = row["name"], row["id"]
        if job == rerun.jobs.get(name):
            continue
        check = Check(name, check_state(row), row.get("completed_at") or "", row.get("html_url") or "",
                      job=job, check_run_id=job, source=f"app:{GITHUB_ACTIONS_APP_ID}")
        if check.key in found or check.state == "pending" or not row.get("conclusion"):
            return {workflow.key: workflow} if workflow.state == "fail" else {}
        found[check.key] = check
    if workflow.state == "pass":
        return found if rerun.check_keys <= found.keys() else {}
    if not any(check.state == "fail" for check in found.values()):
        found[workflow.key] = workflow
    return found


def rerun_failed_jobs(repo: str, run: int) -> None:
    gh(["run", "rerun", str(run), "--failed", "--repo", repo], write=True)


def actions_run_id(repo: str, link: str) -> int:
    if not isinstance(link, str):
        return 0
    match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/actions/runs/([1-9][0-9]{{0,19}})(?:[/?#].*)?",
                         link, re.IGNORECASE)
    return int(match[1]) if match else 0
