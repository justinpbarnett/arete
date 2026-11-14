from __future__ import annotations

import base64
import functools
import itertools
import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass

from . import config, outbound, paths, util

ARETE_MARK = "<!-- arete -->"
GITHUB_API = "https://api.github.com"
CLONE_TOKEN_PERMISSIONS = {"contents": "read"}
NO_ISSUE = "Could not resolve to an issue"


class GhError(RuntimeError):
    pass


def marked(body: str) -> str:
    return f"{body.rstrip()}\n\n{ARETE_MARK}"


def has_arete_mark(body: str | None) -> bool:
    return ARETE_MARK in (body or "")


@functools.cache
def auth_token() -> str:
    if paths.ISOLATED:
        return ""
    res = util.sh(["gh", "auth", "token"], timeout=10)
    return res.stdout.strip() if res.returncode == 0 else ""


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


def gh(args: list[str], cwd=None, timeout: float = 120, input: str | None = None) -> str:
    if unscreened_text(args, input):
        raise TypeError("text sent to GitHub must come from gh.outgoing")
    res = util.sh_with_network(["gh", *args], cwd=cwd, timeout=timeout, input=input)
    if res.returncode != 0:
        command = " ".join(itertools.takewhile(lambda arg: not arg.startswith("-"), args))
        raise GhError(f"gh {command}: {util.first_line(res.stderr or res.stdout, 300)}")
    return res.stdout


def gh_json(args: list[str], cwd=None, timeout: float = 120):
    out = gh(args, cwd=cwd, timeout=timeout)
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
        ["issue", "view", str(number), "--repo", repo, "--json", "number,title,body,labels,url,comments,state,author,updatedAt"]
    )


def outgoing(text: str, link: str = "", mark: bool = False) -> Outgoing:
    text = outbound.inert(outbound.screen(text))
    if link:
        text = f"{outbound.screen(link)}\n\n{text}"
    return Outgoing(marked(text) if mark else text)


def split_link(body: str, links) -> tuple[str, str]:
    first, _, rest = body.partition("\n")
    if first and first in links:
        return first, rest.lstrip("\n")
    return "", body


def issue_create(repo: str, title: str, body: str, labels: Sequence[str] = ()) -> int:
    args = ["issue", "create", "--repo", repo, "--title", outbound.screen(title), "--body-file", "-"]
    for lb in labels:
        args += ["--label", lb]
    url = gh(args, input=outgoing(body)).strip().splitlines()[-1]
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def recent_issues(repo: str, limit: int = 100) -> list[dict]:
    return gh_json(["issue", "list", "--repo", repo, "--state", "all", "--limit", str(limit), "--search",
                    "sort:updated-desc", "--json", "number,title,body,state,stateReason,updatedAt"]) or []


def issue_url(repo: str, number: int | str) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def issue_comment(repo: str, number: int, body: str) -> None:
    gh(["issue", "comment", str(number), "--repo", repo, "--body-file", "-"], input=outgoing(body))


def issue_close(repo: str, number: int, comment: str = "") -> None:
    args = ["issue", "close", str(number), "--repo", repo]
    if comment:
        args += ["--comment", outgoing(comment)]
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
        if NO_ISSUE in str(e):
            return ""
        raise


def issue_edit_body(repo: str, number: int, body: str) -> None:
    gh(["issue", "edit", str(number), "--repo", repo, "--body-file", "-"], input=outgoing(body))


def issue_reopen(repo: str, number: int) -> None:
    gh(["issue", "reopen", str(number), "--repo", repo])


def pr_create(repo: str, head: str, base: str, title: str, body: str, labels: Sequence[str] = (),
              draft: bool = False, cwd=None, link: str = "") -> str:
    args = ["pr", "create", "--repo", repo, "--head", head, "--base", base, "--title", outbound.screen(title),
            "--body-file", "-"]
    for lb in labels:
        args += ["--label", lb]
    if draft:
        args.append("--draft")
    return gh(args, cwd=cwd, input=outgoing(body, link)).strip().splitlines()[-1]


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
    return ((gh_json(["pr", "view", url, "--json", "body"]) or {}).get("body") or "").replace("\r\n", "\n")


def pr_diff(url: str) -> str:
    return gh(["pr", "diff", url])


def pr_edit_body(url: str, body: str, link: str = "") -> None:
    gh(["pr", "edit", url, "--body-file", "-"], input=outgoing(body, link))


def pr_comment(url: str, body: str) -> None:
    gh(["pr", "comment", url, "--body-file", "-"], input=outgoing(body, mark=True))


def pr_reply(url: str, comment_id: int, body: str) -> None:
    gh(["api", f"repos/{pr_repo(url)}/pulls/{pr_number(url)}/comments/{comment_id}/replies", "-X", "POST",
        "--input", "-"], input=Outgoing(json.dumps({"body": outgoing(body, mark=True)})))


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


def pr_draft_and_head(url: str) -> tuple[bool, str]:
    row = gh_json(["pr", "view", url, "--json", "isDraft,headRefOid"]) or {}
    return bool(row.get("isDraft")), str(row.get("headRefOid") or "")


MERGE_NOW = {"CLEAN", "HAS_HOOKS", "UNSTABLE"}
MERGE_LATER = {"BLOCKED", "BEHIND"}
MERGE_WAIT_SECONDS = 120
MERGE_POLL_SECONDS = 5


def merge_action(state: str, mergeable: str, auto_allowed: bool) -> str:
    state, mergeable = (state or "UNKNOWN").upper(), (mergeable or "UNKNOWN").upper()
    if "UNKNOWN" in (state, mergeable):
        return "wait"
    if mergeable == "CONFLICTING":
        return "leave"
    if state in MERGE_NOW:
        return "merge"
    if state in MERGE_LATER and auto_allowed:
        return "auto"
    return "leave"


def leave_reason(state: str, mergeable: str, auto_allowed: bool) -> str:
    if mergeable == "CONFLICTING" or state == "DIRTY":
        return "it conflicts with the base branch"
    if state == "DRAFT":
        return "it is a draft"
    if state == "BLOCKED" and not auto_allowed:
        return "GitHub blocks the merge until a required review or check passes, and the repo has auto merge off"
    if state == "BEHIND" and not auto_allowed:
        return "it is behind the base branch, and the repo has auto merge off"
    return f"GitHub reports merge state {state}, mergeable {mergeable}"


def pr_merge_state(repo: str, number: int) -> tuple[str, str]:
    row = gh_json(["pr", "view", str(number), "--repo", repo, "--json", "mergeStateStatus,mergeable"]) or {}
    return (row.get("mergeStateStatus") or "UNKNOWN").upper(), (row.get("mergeable") or "UNKNOWN").upper()


def squash_subject(repo: str, number: int) -> list[str]:
    title = ((gh_json(["pr", "view", str(number), "--repo", repo, "--json", "title"]) or {}).get("title") or "").strip()
    return ["--subject", f"{title} (#{number})"] if title else []


def repo_allows_auto_merge(repo: str) -> bool:
    return bool((gh_json(["api", f"repos/{repo}"]) or {}).get("allow_auto_merge"))


def pr_merge_auto(repo: str, number: int, wait: float = MERGE_WAIT_SECONDS,
                  poll: float = MERGE_POLL_SECONDS) -> tuple[str, str]:
    auto_allowed = repo_allows_auto_merge(repo)
    deadline = time.monotonic() + wait
    while True:
        state, mergeable = pr_merge_state(repo, number)
        action = merge_action(state, mergeable, auto_allowed)
        if action != "wait" or time.monotonic() >= deadline:
            break
        time.sleep(max(0.0, min(poll, deadline - time.monotonic())))
    if action == "merge":
        gh(["pr", "merge", str(number), "--repo", repo, "--squash", "--delete-branch", *squash_subject(repo, number)])
        return "merged", ""
    if action == "auto":
        gh(["pr", "merge", str(number), "--repo", repo, "--squash", "--auto", "--delete-branch",
            *squash_subject(repo, number)])
        return "auto", ""
    if action == "wait":
        return "left", f"GitHub still reported the merge state as unknown after {wait:g} seconds"
    return "left", leave_reason(state, mergeable, auto_allowed)


def default_branch(repo: str) -> str:
    try:
        return (gh_json(["repo", "view", repo, "--json", "defaultBranchRef"]) or {})["defaultBranchRef"]["name"]
    except (GhError, KeyError, TypeError):
        return "main"


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def key_signature(app: config.GithubApp, signing_input: bytes) -> bytes:
    failed = f"openssl could not sign with [github_app] private_key {paths.short_path(app.private_key)}"
    if paths.ISOLATED and paths.confined(app.private_key) is None:
        raise GhError("isolated arete uses no key of this machine; put the [github_app] private_key in the scratch folder")
    try:
        res = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(app.private_key)], input=signing_input,
                             capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise GhError(f"{failed}: {e}") from None
    if res.returncode != 0:
        raise GhError(f"{failed}: {util.first_line(res.stderr.decode(errors='replace')) or f'exit {res.returncode}'}")
    return res.stdout


def app_jwt(app: config.GithubApp) -> str:
    now = int(time.time())
    signed = ".".join(base64url(json.dumps(part, separators=(",", ":")).encode()) for part in (
        {"alg": "RS256", "typ": "JWT"}, {"iat": now - 60, "exp": now + 540, "iss": app.app_id}))
    return f"{signed}.{base64url(key_signature(app, signed.encode()))}"


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
    body = {"repositories": [repo.rsplit("/", 1)[-1]], "permissions": CLONE_TOKEN_PERMISSIONS}
    request = urllib.request.Request(
        f"{GITHUB_API}/app/installations/{app.installation_id}/access_tokens", data=json.dumps(body).encode(),
        method="POST", headers={"Authorization": f"Bearer {app_jwt(app)}", "Accept": "application/vnd.github+json",
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
    return reply if isinstance(reply, dict) else {}


def http_message(e: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(e.read()).get("message") or e.reason)
    except (ValueError, OSError, AttributeError):
        return str(e.reason)


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
    pages = gh_json(["api", f"repos/{repo}/commits/{ref}/check-runs?per_page=100", "--paginate", "--slurp"]) or []
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
