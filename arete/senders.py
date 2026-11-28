from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

from . import gh, outbound

_DIGITS_RE = re.compile(r"^[1-9][0-9]{0,17}$")


@dataclass(frozen=True)
class SendResult:
    external_id: str
    external_url: str


@dataclass(frozen=True)
class LookResult:
    found: bool
    external_id: str | None
    external_url: str | None
    summary: str


@dataclass(frozen=True)
class Sender:
    connector: str
    kind: str
    send: Callable[[dict, str], SendResult]
    look: Callable[[dict, str], LookResult]
    preview: Callable[[dict, str], str]


def marks_for(marker: str) -> str:
    parts = marker.split(":", 2)
    tag = parts[1] if len(parts) >= 2 and parts[0] == "arete" else ""
    first = f"<!-- arete:{tag} -->" if tag else gh.instance_mark()
    return f"{first} <!-- {marker} -->"


def rendered_github_body(body: str, marker: str) -> str:
    screened = str(gh.outgoing(body)).rstrip()
    marks = marks_for(marker)
    return f"{screened}\n\n{marks}" if screened else marks


def _parse_positive_int(text: str, label: str) -> int:
    if not _DIGITS_RE.fullmatch(text):
        raise ValueError(f"invalid GitHub {label}: {text!r}")
    return int(text)


def _parse_github_repo(obj: str) -> str:
    if not obj.startswith("github:"):
        raise ValueError(f"expected github:<owner/repo>, got {obj!r}")
    repo = obj.removeprefix("github:")
    try:
        return gh._safe_repo(repo)
    except gh.GhError as error:
        raise ValueError(str(error)) from error


def _parse_github_issue(obj: str) -> tuple[str, int]:
    if not obj.startswith("github:") or "#" not in obj:
        raise ValueError(f"expected github:<owner/repo>#<number>, got {obj!r}")
    repo_part, num_part = obj.removeprefix("github:").split("#", 1)
    try:
        repo = gh._safe_repo(repo_part)
    except gh.GhError as error:
        raise ValueError(str(error)) from error
    return repo, _parse_positive_int(num_part, "issue number")


def _parse_github_reply(obj: str) -> tuple[str, int, int]:
    if not obj.startswith("github:") or "#" not in obj:
        raise ValueError(f"expected github:<owner/repo>#<pr>/<comment>, got {obj!r}")
    repo_part, tail = obj.removeprefix("github:").split("#", 1)
    if "/" not in tail:
        raise ValueError(f"expected github:<owner/repo>#<pr>/<comment>, got {obj!r}")
    num_part, comment_part = tail.split("/", 1)
    try:
        repo = gh._safe_repo(repo_part)
    except gh.GhError as error:
        raise ValueError(str(error)) from error
    return repo, _parse_positive_int(num_part, "PR number"), _parse_positive_int(comment_part, "review comment id")


def _flatten_api_list(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise gh.GhUnsure("GitHub lookup returned no list")
    rows: list[dict] = []
    for entry in raw:
        if isinstance(entry, list):
            for item in entry:
                if isinstance(item, dict):
                    rows.append(item)
        elif isinstance(entry, dict):
            rows.append(entry)
        else:
            raise gh.GhUnsure("GitHub lookup returned an unexpected entry")
    return rows


def _parse_json_reply(raw: str, label: str) -> dict:
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except ValueError as error:
        raise gh.GhUnsure(f"GitHub {label} reply was not valid JSON") from error
    if not isinstance(parsed, dict):
        raise gh.GhUnsure(f"GitHub {label} reply was not an object")
    return parsed


def _split_issue_title_body(body: str) -> tuple[str, str]:
    lines = body.splitlines()
    title = ""
    rest_start = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped:
            title = stripped
            rest_start = idx + 1
            break
    if not title:
        raise ValueError("GitHub issue body must have a non-empty first line for the title")
    rest = "\n".join(lines[rest_start:]).lstrip("\n")
    return outbound.screen(title), rest


def _gh_comment_send(item: dict, body: str) -> SendResult:
    repo, number = _parse_github_issue(item["object"])
    full_body = rendered_github_body(body, item["marker"])
    payload = gh.Outgoing(json.dumps({"body": full_body}))
    raw = gh.gh(
        ["api", "--method", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-"],
        input=payload,
        write=True,
    )
    reply = _parse_json_reply(raw, "comment")
    comment_id = reply.get("id")
    if comment_id is None:
        raise gh.GhUnsure("GitHub comment reply did not include an id")
    return SendResult(external_id=str(comment_id), external_url=str(reply.get("html_url") or ""))


def _gh_comment_look(item: dict, since_iso: str) -> LookResult:
    repo, number = _parse_github_issue(item["object"])
    query = urlencode({"since": since_iso, "per_page": 100})
    path = f"repos/{repo}/issues/{number}/comments?{query}"
    rows = _flatten_api_list(gh.gh_json(["api", path, "--paginate", "--slurp"]))
    needle = f"<!-- {item['marker']} -->"
    for row in rows:
        if needle in (row.get("body") or ""):
            ext_id = row.get("id")
            if ext_id is None:
                raise gh.GhUnsure("GitHub comment lookup hit missing id")
            return LookResult(
                found=True,
                external_id=str(ext_id),
                external_url=str(row.get("html_url") or ""),
                summary=f"GET {path}: found id={ext_id} ({len(rows)} comments)",
            )
    return LookResult(found=False, external_id=None, external_url=None, summary=f"GET {path}: not found ({len(rows)} comments)")


def _gh_reply_send(item: dict, body: str) -> SendResult:
    repo, number, comment_id = _parse_github_reply(item["object"])
    full_body = rendered_github_body(body, item["marker"])
    payload = gh.Outgoing(json.dumps({"body": full_body}))
    raw = gh.gh(
        ["api", "--method", "POST", f"repos/{repo}/pulls/{number}/comments/{comment_id}/replies", "--input", "-"],
        input=payload,
        write=True,
    )
    reply = _parse_json_reply(raw, "reply")
    reply_id = reply.get("id")
    if reply_id is None:
        raise gh.GhUnsure("GitHub reply response did not include an id")
    return SendResult(external_id=str(reply_id), external_url=str(reply.get("html_url") or ""))


def _gh_reply_look(item: dict, since_iso: str) -> LookResult:
    repo, number, _ = _parse_github_reply(item["object"])
    query = urlencode({"since": since_iso, "per_page": 100})
    path = f"repos/{repo}/pulls/{number}/comments?{query}"
    rows = _flatten_api_list(gh.gh_json(["api", path, "--paginate", "--slurp"]))
    needle = f"<!-- {item['marker']} -->"
    for row in rows:
        if needle in (row.get("body") or ""):
            ext_id = row.get("id")
            if ext_id is None:
                raise gh.GhUnsure("GitHub reply lookup hit missing id")
            return LookResult(
                found=True,
                external_id=str(ext_id),
                external_url=str(row.get("html_url") or ""),
                summary=f"GET {path}: found id={ext_id} ({len(rows)} comments)",
            )
    return LookResult(found=False, external_id=None, external_url=None, summary=f"GET {path}: not found ({len(rows)} comments)")


def _gh_issue_send(item: dict, body: str) -> SendResult:
    repo = _parse_github_repo(item["object"])
    title, rest = _split_issue_title_body(body)
    full_body = rendered_github_body(rest, item["marker"])
    payload = gh.Outgoing(json.dumps({"title": title, "body": full_body}))
    raw = gh.gh(
        ["api", "--method", "POST", f"repos/{repo}/issues", "--input", "-"],
        input=payload,
        write=True,
    )
    reply = _parse_json_reply(raw, "issue")
    number = reply.get("number")
    if number is None:
        raise gh.GhUnsure("GitHub issue reply did not include a number")
    return SendResult(
        external_id=str(number),
        external_url=str(reply.get("html_url") or gh.issue_url(repo, number)),
    )


def _gh_issue_look(item: dict, since_iso: str) -> LookResult:
    repo = _parse_github_repo(item["object"])
    query = urlencode({
        "state": "all",
        "sort": "created",
        "direction": "desc",
        "since": since_iso,
        "per_page": 100,
    })
    path = f"repos/{repo}/issues?{query}"
    rows = _flatten_api_list(gh.gh_json(["api", path, "--paginate", "--slurp"]))
    needle = f"<!-- {item['marker']} -->"
    issues_seen = 0
    for row in rows:
        if "pull_request" in row:
            continue
        issues_seen += 1
        if needle in (row.get("body") or ""):
            number = row.get("number")
            if number is None:
                raise gh.GhUnsure("GitHub issue lookup hit missing number")
            return LookResult(
                found=True,
                external_id=str(number),
                external_url=str(row.get("html_url") or gh.issue_url(repo, number)),
                summary=f"GET {path}: found number={number} ({issues_seen} issues)",
            )
    return LookResult(found=False, external_id=None, external_url=None, summary=f"GET {path}: not found ({issues_seen} issues)")


def _gh_comment_preview(item: dict, body: str) -> str:
    return rendered_github_body(body, item["marker"])


def _gh_issue_preview(item: dict, body: str) -> str:
    title, rest = _split_issue_title_body(body)
    full_body = rendered_github_body(rest, item["marker"])
    return f"{title}\n\n{full_body}"


REGISTRY: dict[tuple[str, str], Sender] = {
    ("github", "comment"): Sender("github", "comment", _gh_comment_send, _gh_comment_look, _gh_comment_preview),
    ("github", "reply"): Sender("github", "reply", _gh_reply_send, _gh_reply_look, _gh_comment_preview),
    ("github", "issue"): Sender("github", "issue", _gh_issue_send, _gh_issue_look, _gh_issue_preview),
}


def connector_of(target: str, obj: str = "") -> str:
    if isinstance(obj, str) and ":" in obj:
        return obj.partition(":")[0].strip().lower()
    if isinstance(target, str) and target.strip():
        return target.partition(":")[0].strip().lower()
    return ""


def resolve(target: str, kind: str, obj: str = "") -> Sender:
    connector = connector_of(target, obj)
    sender = REGISTRY.get((connector, kind))
    if sender is None:
        raise ValueError(f"no outbox sender for {connector or target!r} kind {kind!r}")
    return sender


def preview(item: dict, body: str) -> str:
    if item.get("legacy_approval") is not None or not item.get("marker"):
        return body
    sender = resolve(item["target"], item["kind"], item.get("object") or "")
    return sender.preview(item, body)
