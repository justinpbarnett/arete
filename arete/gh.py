from __future__ import annotations

import functools
import json
from collections.abc import Sequence
from dataclasses import dataclass

from . import util

ARETE_MARK = "<!-- arete -->"


class GhError(RuntimeError):
    pass


def marked(body: str) -> str:
    return f"{body.rstrip()}\n\n{ARETE_MARK}"


def has_arete_mark(body: str | None) -> bool:
    return ARETE_MARK in (body or "")


def gh(args: list[str], cwd=None, timeout: float = 120, input: str | None = None) -> str:
    res = util.sh_with_network(["gh", *args], cwd=cwd, timeout=timeout, input=input)
    if res.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}: {util.first_line(res.stderr or res.stdout, 300)}")
    return res.stdout


def gh_json(args: list[str], cwd=None, timeout: float = 120):
    out = gh(args, cwd=cwd, timeout=timeout)
    return json.loads(out) if out.strip() else None


def ensure_label(repo: str, name: str, color: str = "5319e7", description: str = "") -> str:
    try:
        gh(["label", "create", name, "--repo", repo, "--color", color, "--description", description, "--force"])
        return ""
    except GhError as e:
        return f"{name}: {e}"


def issues(repo: str, label: str = "", state: str = "open", limit: int = 30, author: str = "") -> list[dict]:
    args = ["issue", "list", "--repo", repo, "--state", state, "--limit", str(limit),
            "--json", "number,title,body,labels,url,createdAt,updatedAt,author"]
    if label:
        args += ["--label", label]
    if author:
        args += ["--author", author]
    return gh_json(args) or []


def issue(repo: str, number: int) -> dict:
    return gh_json(
        ["issue", "view", str(number), "--repo", repo, "--json", "number,title,body,labels,url,comments,state,author"]
    )


def issue_create(repo: str, title: str, body: str, labels: Sequence[str] = ()) -> int:
    args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", "-"]
    for lb in labels:
        args += ["--label", lb]
    url = gh(args, input=body).strip().splitlines()[-1]
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def issue_url(repo: str, number: int | str) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def issue_comment(repo: str, number: int, body: str) -> None:
    gh(["issue", "comment", str(number), "--repo", repo, "--body-file", "-"], input=body)


def issue_close(repo: str, number: int, comment: str = "") -> None:
    args = ["issue", "close", str(number), "--repo", repo]
    if comment:
        args += ["--comment", comment]
    gh(args)


def issue_labels(repo: str, number: int, add: list[str] = (), remove: list[str] = ()) -> None:
    args = ["issue", "edit", str(number), "--repo", repo]
    for lb in add:
        args += ["--add-label", lb]
    for lb in remove:
        args += ["--remove-label", lb]
    if len(args) > 5:
        gh(args)


def issue_state(repo: str, number: int) -> str:
    try:
        return (gh_json(["issue", "view", str(number), "--repo", repo, "--json", "state"]) or {}).get("state", "")
    except GhError as e:
        if "Could not resolve to an issue" in str(e):
            return ""
        raise


def issue_edit_body(repo: str, number: int, body: str) -> None:
    gh(["issue", "edit", str(number), "--repo", repo, "--body-file", "-"], input=body)


def issue_reopen(repo: str, number: int) -> None:
    gh(["issue", "reopen", str(number), "--repo", repo])


def pr_create(repo: str, head: str, base: str, title: str, body: str, labels: Sequence[str] = (),
              draft: bool = False, cwd=None) -> str:
    args = ["pr", "create", "--repo", repo, "--head", head, "--base", base, "--title", title, "--body-file", "-"]
    for lb in labels:
        args += ["--label", lb]
    if draft:
        args.append("--draft")
    return gh(args, cwd=cwd, input=body).strip().splitlines()[-1]


def prs(repo: str, label: str = "", state: str = "open", limit: int = 50) -> list[dict]:
    args = ["pr", "list", "--repo", repo, "--state", state, "--limit", str(limit),
            "--json", "number,title,url,state,mergedAt,closedAt,headRefName,labels,body"]
    if label:
        args += ["--label", label]
    return gh_json(args) or []


def open_pr_url(repo: str, head: str) -> str:
    rows = gh_json(["pr", "list", "--repo", repo, "--head", head, "--state", "open", "--limit", "1", "--json", "url"])
    return rows[0]["url"] if rows else ""


def closed_pr_heads(repo: str, head: str) -> set[str]:
    rows = gh_json(["pr", "list", "--repo", repo, "--head", head, "--state", "closed", "--limit", "20",
                    "--json", "headRefOid"]) or []
    return {r["headRefOid"] for r in rows}


def pr_body(url: str) -> str:
    return (gh_json(["pr", "view", url, "--json", "body"]) or {}).get("body") or ""


def pr_diff(url: str) -> str:
    return gh(["pr", "diff", url])


def pr_edit_body(url: str, body: str) -> None:
    gh(["pr", "edit", url, "--body-file", "-"], input=body)


def pr_comment(url: str, body: str) -> None:
    gh(["pr", "comment", url, "--body-file", "-"], input=marked(body))


def pr_reply(url: str, comment_id: int, body: str) -> None:
    gh(["api", "-X", "POST", f"repos/{pr_repo(url)}/pulls/{pr_number(url)}/comments/{comment_id}/replies",
        "--input", "-"], input=json.dumps({"body": marked(body)}))


def api_list(path: str) -> list[dict]:
    pages = gh_json(["api", "--paginate", "--slurp", f"{path}?per_page=100"]) or []
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
    args = ["pr", "view", str(url_or_number), "--json", "number,state,mergedAt,mergeCommit,closedAt,updatedAt,url,headRefName"]
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


def pr_set_draft(url: str, draft: bool) -> None:
    gh(["pr", "ready", url, *(["--undo"] if draft else [])])


def pr_merge_auto(repo: str, number: int) -> None:
    gh(["pr", "merge", str(number), "--repo", repo, "--squash", "--auto", "--delete-branch"])


def default_branch(repo: str) -> str:
    try:
        return (gh_json(["repo", "view", repo, "--json", "defaultBranchRef"]) or {})["defaultBranchRef"]["name"]
    except (GhError, KeyError, TypeError):
        return "main"


def repo_exists(repo: str) -> bool:
    try:
        gh(["repo", "view", repo, "--json", "name"])
        return True
    except GhError:
        return False


def repo_private(repo: str) -> bool | None:
    try:
        return bool((gh_json(["repo", "view", repo, "--json", "isPrivate"]) or {}).get("isPrivate"))
    except GhError:
        return None


PASSING_CONCLUSIONS = {"success", "neutral"}
STATUS_STATES = {"success": "pass", "pending": "pending", "failure": "fail", "error": "fail"}


@dataclass
class Check:
    name: str
    state: str
    updated: str = ""
    link: str = ""
    job: int = 0
    description: str = ""
    check_run_id: int = 0


def check_from_run(row: dict) -> Check:
    if row.get("status") != "completed":
        state = "pending"
    elif row.get("conclusion") == "skipped":
        state = "skipped"
    else:
        state = "pass" if row.get("conclusion") in PASSING_CONCLUSIONS else "fail"
    actions = (row.get("app") or {}).get("slug") == "github-actions"
    return Check(row["name"], state, row.get("completed_at") or row.get("started_at") or "",
                 row.get("html_url") or row.get("details_url") or "", row["id"] if actions else 0,
                 ((row.get("output") or {}).get("title") or "").strip(), row["id"])


def check_from_status(row: dict) -> Check:
    return Check(row["context"], STATUS_STATES.get(row.get("state"), "fail"), row.get("updated_at") or "",
                 row.get("target_url") or "", 0, (row.get("description") or "").strip())


def commit_checks(repo: str, ref: str) -> dict[str, Check]:
    pages = gh_json(["api", "--paginate", "--slurp", f"repos/{repo}/commits/{ref}/check-runs?per_page=100"]) or []
    runs = sorted((check_from_run(r) for page in pages for r in page.get("check_runs", [])), key=lambda c: c.check_run_id)
    statuses = (gh_json(["api", f"repos/{repo}/commits/{ref}/status?per_page=100"]) or {}).get("statuses", [])
    newest: dict[str, Check] = {c.name: c for c in runs}
    for c in map(check_from_status, statuses):
        if c.name not in newest or c.updated > newest[c.name].updated:
            newest[c.name] = c
    return newest


def branch_sha(repo: str, branch: str) -> str:
    return gh(["api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"]).strip()


def job_log(repo: str, job: int) -> str:
    return gh(["api", f"repos/{repo}/actions/jobs/{job}/logs"], timeout=300)
