from __future__ import annotations

import base64
import functools
import itertools
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

from . import config, outbound, paths, util

ARETE_MARK = "<!-- arete -->"
GITHUB_API = "https://api.github.com"
CLONE_TOKEN_PERMISSIONS = {"contents": "read"}
NO_ISSUE = "Could not resolve to an issue"


class GhError(RuntimeError):
    pass


class GhUnsure(GhError):
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
    return gh_json(args) or []


def issue(repo: str, number: int) -> dict:
    return gh_json(
        ["issue", "view", str(number), "--repo", repo, "--json", "number,title,body,labels,url,comments,state,author,updatedAt"]
    )


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
                  *, single_attempt: bool = False) -> str:
    key, start = uuid.uuid4().hex, util.now()
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


def issue_create(repo: str, title: str, body: str, labels: Sequence[str] = (), *, single_attempt: bool = False) -> int:
    key = uuid.uuid4().hex
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
    url = reply.strip().splitlines()[-1]
    return int(url.rstrip("/").rsplit("/", 1)[-1])


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
    keyed_comment(["pr", "comment", url, "--body-file", "-"], body,
                  f"repos/{pr_repo(url)}/issues/{pr_number(url)}/comments", mark=True)


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


MERGE_WAIT_SECONDS = 120
MERGE_POLL_SECONDS = 5


def merge_ci_passed(repo: str, head: str, *, required: dict | None = None) -> bool:
    path = f"repos/{repo}/commits/{head}"
    checks = gh_json(["api", f"{path}/check-runs?filter=latest&per_page=100", "--paginate", "--slurp"])
    statuses = gh_json(["api", f"{path}/statuses?per_page=100", "--paginate", "--slurp"])
    if (not isinstance(checks, list) or not checks
            or any(not isinstance(page, dict) or not isinstance(page.get("check_runs"), list) for page in checks)
            or not isinstance(statuses, list) or not statuses):
        return False
    runs = [row for page in checks for row in page["check_runs"]]
    if required and not any(row.get("head_sha") == head and row.get("name") == required["context"]
                            and row.get("app", {}).get("id") == required["app_id"] for row in runs):
        return False
    contexts = {}
    for page in statuses:
        if not isinstance(page, list):
            return False
        for row in page:
            contexts.setdefault(row["context"], row["state"])
    return (bool(runs or contexts) and all(row["status"] == "completed" and row["conclusion"] == "success"
                                           for row in runs) and all(state == "success" for state in contexts.values()))


def merge_readiness(repo: str, number: int, head: str, base: str, base_sha: str, *, wait: float = 0,
                    required: dict | None = None) -> tuple[str, str]:
    if not head or not base or not base_sha:
        return "", "the checked head and base are not known"
    deadline = time.monotonic() + wait
    fields = "state,isDraft,headRefOid,baseRefName,baseRefOid,mergeStateStatus,mergeable,title"
    while True:
        row = gh_json(["pr", "view", str(number), "--repo", repo, "--json", fields]) or {}
        if row.get("state") != "OPEN" or row.get("isDraft") is not False:
            return "", "the PR is not open and ready for review"
        if row.get("headRefOid") != head:
            return "", "the PR head changed after verification"
        if row.get("baseRefName") != base or row.get("baseRefOid") != base_sha:
            return "", "the PR base changed after verification"
        state, mergeable = row.get("mergeStateStatus"), row.get("mergeable")
        remaining = deadline - time.monotonic()
        if "UNKNOWN" not in (state, mergeable) or remaining <= 0:
            break
        time.sleep(min(MERGE_POLL_SECONDS, remaining))
    if state != "CLEAN" or mergeable != "MERGEABLE":
        return "", f"GitHub reports merge state {state}, mergeable {mergeable}"
    if not merge_ci_passed(repo, head, required=required):
        return "", "GitHub has no fully passing CI result for the verified head"
    return str(row.get("title") or "").strip(), ""


def pr_merge_auto(repo: str, number: int, head: str, base: str, base_sha: str) -> str:
    subject, hold = merge_readiness(repo, number, head, base, base_sha)
    if hold:
        return hold
    payload = {"sha": head, "merge_method": "squash"}
    if subject:
        payload["commit_title"] = f"{subject} (#{number})"
    result = json.loads(gh(["api", "-X", "PUT", f"repos/{repo}/pulls/{number}/merge", "--input", "-"],
                           input=Outgoing(json.dumps(payload)), write=True) or "{}")
    return "" if result.get("merged") is True else "GitHub did not confirm the merge"


def default_branch(repo: str) -> str:
    try:
        return (gh_json(["repo", "view", repo, "--json", "defaultBranchRef"]) or {})["defaultBranchRef"]["name"]
    except (GhError, KeyError, TypeError):
        return "main"


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def key_signature(app: config.GithubApp, signing_input: bytes, *, timeout: float = 30) -> bytes:
    failed = f"openssl could not sign with [github_app] private_key {paths.short_path(app.private_key)}"
    if paths.ISOLATED and paths.confined(app.private_key) is None:
        raise GhError("isolated arete uses no key of this machine; put the [github_app] private_key in the scratch folder")
    try:
        res = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(app.private_key)], input=signing_input,
                             capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise GhError(f"{failed}: {e}") from None
    if res.returncode != 0:
        raise GhError(f"{failed}: {util.first_line(res.stderr.decode(errors='replace')) or f'exit {res.returncode}'}")
    return res.stdout


def app_jwt(app: config.GithubApp, *, timeout: float = 30) -> str:
    now = int(time.time())
    signed = ".".join(base64url(json.dumps(part, separators=(",", ":")).encode()) for part in (
        {"alg": "RS256", "typ": "JWT"}, {"iat": now - 60, "exp": now + 540, "iss": app.app_id}))
    return f"{signed}.{base64url(key_signature(app, signed.encode(), timeout=timeout))}"


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
