from __future__ import annotations

import json
import re
import threading
from urllib.parse import urlencode

from . import config, emits, gh, outbound, send_push, senders, tracker, util

_DIGITS_RE = re.compile(r"^[1-9][0-9]{0,17}$")
_TRANSIENT_RE = re.compile(r"HTTP 5\d\d|Something went wrong while executing your query", re.IGNORECASE)
_FORBIDDEN_HOST_KEYS = frozenset(("workdir", "file", "env"))


class PrMergeHeld(gh.GhError):
    def __init__(self, hold: str):
        super().__init__(str(hold))
        self.hold = hold

LABEL_STYLES: dict[str, tuple[str, str]] = {
    tracker.LABEL: tracker.APPROVAL_LABEL_STYLE,
    "arete": ("5319e7", "Arete pipeline state"),
    **{f"agent:{state}": (color, "Arete pipeline state") for state, color in tracker.STATE_COLORS.items()},
    "arete-alert": ("d93f0b", "Opened by arete heartbeat"),
    "arete-triage": ("c5def5", "Arete triage board"),
    "arete-miner": ("1d76db", "Harness fixes mined by arete"),
    "arete-trust": ("0e8a16", "Trust level change proposed by arete"),
    "agent:auto": ("c5def5", "Approved by automatic triage"),
    "fix": ("d73a4a", "Repair existing behavior; eligible for Arete's guarded patch flow"),
    "feature": ("a2eeef", "New or changed capability; use Arete's full feature flow"),
}

_ENSURED_LOCK = threading.Lock()
_ENSURED_LABELS: set[tuple[str, str]] = set()


def clear_label_cache() -> None:
    with _ENSURED_LOCK:
        _ENSURED_LABELS.clear()


def _parse_positive_int(text: str, label: str) -> int:
    if not _DIGITS_RE.fullmatch(text):
        raise ValueError(f"invalid GitHub {label}: {text!r}")
    return int(text)


def _parse_repo(obj: str) -> str:
    if not obj.startswith("github:") or "#" in obj:
        raise ValueError(f"expected github:<owner/repo>, got {obj!r}")
    repo = obj.removeprefix("github:")
    try:
        return gh._safe_repo(repo)
    except gh.GhError as error:
        raise ValueError(str(error)) from error


def _parse_issue(obj: str) -> tuple[str, int]:
    if not obj.startswith("github:") or "#" not in obj:
        raise ValueError(f"expected github:<owner/repo>#<number>, got {obj!r}")
    repo_part, num_part = obj.removeprefix("github:").split("#", 1)
    try:
        repo = gh._safe_repo(repo_part)
    except gh.GhError as error:
        raise ValueError(str(error)) from error
    return repo, _parse_positive_int(num_part, "issue number")


def _parse_reply(obj: str) -> tuple[str, int, int]:
    if not obj.startswith("github:") or "#" not in obj:
        raise ValueError(f"expected github:<owner/repo>#<pr>/r<comment>, got {obj!r}")
    repo_part, tail = obj.removeprefix("github:").split("#", 1)
    if "/" not in tail:
        raise ValueError(f"expected github:<owner/repo>#<pr>/r<comment>, got {obj!r}")
    num_part, comment_part = tail.split("/", 1)
    if comment_part.startswith("r"):
        comment_part = comment_part[1:]
    try:
        repo = gh._safe_repo(repo_part)
    except gh.GhError as error:
        raise ValueError(str(error)) from error
    return repo, _parse_positive_int(num_part, "PR number"), _parse_positive_int(comment_part, "review comment id")


def _ensure_labels(repo: str, labels: list[str]) -> None:
    for label in labels:
        key = (repo, label)
        with _ENSURED_LOCK:
            if key in _ENSURED_LABELS:
                continue
        color, description = LABEL_STYLES.get(label, ("5319e7", ""))
        err = gh.ensure_label(repo, label, color, description, restyle=False)
        if isinstance(err, str) and err:
            if util.NETWORK_ERROR.search(err) or _TRANSIENT_RE.search(err):
                raise gh.GhUnsure(err)
            raise gh.GhError(err)
        with _ENSURED_LOCK:
            _ENSURED_LABELS.add(key)


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


def parse_issue_payload(body: str, *, is_edit: bool = False) -> dict[str, object]:
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            if is_edit:
                return {"body": str(parsed.get("body") or "")}
            title = str(parsed.get("title") or "").strip()
            if not title:
                raise ValueError("GitHub issue payload requires a non-empty title")
            raw_labels = parsed.get("labels") or []
            if not isinstance(raw_labels, list):
                raise ValueError("GitHub issue payload labels must be a list")
            labels = [str(lb) for lb in raw_labels if str(lb).strip()]
            finding = parsed.get("finding")
            return {
                "title": outbound.screen(title),
                "body": str(parsed.get("body") or ""),
                "labels": labels,
                "finding": finding if isinstance(finding, dict) else None,
            }
    if is_edit:
        return {"body": body}
    title, rest = _split_issue_title_body(body)
    return {"title": title, "body": rest, "labels": [], "finding": None}


def parse_label_payload(body: str) -> dict[str, object]:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise ValueError(f"GitHub label payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("GitHub label payload must be a JSON object")
    add = [str(lb) for lb in (parsed.get("add") or ()) if str(lb).strip()]
    remove = [str(lb) for lb in (parsed.get("remove") or ()) if str(lb).strip()]
    family = str(parsed.get("family") or "")
    if family not in ("", "state"):
        raise ValueError(f"unsupported label family: {family!r}")
    return {"add": add, "remove": remove, "family": family}


def parse_status_payload(body: str) -> str:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise ValueError(f"GitHub status payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("GitHub status payload must be a JSON object")
    state = str(parsed.get("state") or "")
    if state not in ("open", "closed"):
        raise ValueError(f"unsupported GitHub status state: {state!r}")
    return state


def _comment_send(item: dict, body: str) -> senders.SendResult:
    repo, number = _parse_issue(item["object"])
    full_body = senders.rendered_github_body(body, item["marker"])
    comment_id, url = gh.api_create_comment(repo, number, full_body)
    return senders.SendResult(external_id=comment_id, external_url=url)


def _comment_look(item: dict, since_iso: str) -> senders.LookResult:
    repo, number = _parse_issue(item["object"])
    params = {"since": since_iso, "per_page": 100}
    path = f"repos/{repo}/issues/{number}/comments"
    rows = gh.api_list(path, params, strict=True)
    needle = f"<!-- {item['marker']} -->"
    query_str = urlencode(params)
    for row in rows:
        if needle in (row.get("body") or ""):
            ext_id = row.get("id")
            if ext_id is None:
                raise gh.GhUnsure("GitHub comment lookup hit missing id")
            return senders.LookResult(
                found=True,
                external_id=str(ext_id),
                external_url=str(row.get("html_url") or ""),
                summary=f"GET {path}?{query_str}: found id={ext_id} ({len(rows)} comments)",
            )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"GET {path}?{query_str}: not found ({len(rows)} comments)",
    )


def _reply_send(item: dict, body: str) -> senders.SendResult:
    repo, number, comment_id = _parse_reply(item["object"])
    full_body = senders.rendered_github_body(body, item["marker"])
    reply_id, url = gh.api_create_reply(repo, number, comment_id, full_body)
    return senders.SendResult(external_id=reply_id, external_url=url)


def _reply_look(item: dict, since_iso: str) -> senders.LookResult:
    repo, number, _ = _parse_reply(item["object"])
    params = {"since": since_iso, "per_page": 100}
    path = f"repos/{repo}/pulls/{number}/comments"
    rows = gh.api_list(path, params, strict=True)
    needle = f"<!-- {item['marker']} -->"
    query_str = urlencode(params)
    for row in rows:
        if needle in (row.get("body") or ""):
            ext_id = row.get("id")
            if ext_id is None:
                raise gh.GhUnsure("GitHub reply lookup hit missing id")
            return senders.LookResult(
                found=True,
                external_id=str(ext_id),
                external_url=str(row.get("html_url") or ""),
                summary=f"GET {path}?{query_str}: found id={ext_id} ({len(rows)} comments)",
            )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"GET {path}?{query_str}: not found ({len(rows)} comments)",
    )


def _issue_send(item: dict, body: str) -> senders.SendResult:
    obj = item["object"]
    if "#" in obj:
        repo, number = _parse_issue(obj)
        payload = parse_issue_payload(body, is_edit=True)
        full_body = senders.rendered_github_body(str(payload["body"]), item["marker"])
        gh.issue_edit_body(repo, number, full_body)
        return senders.SendResult(external_id=str(number), external_url=gh.issue_url(repo, number))
    repo = _parse_repo(obj)
    payload = parse_issue_payload(body, is_edit=False)
    labels = list(payload["labels"])
    if labels:
        _ensure_labels(repo, labels)
    full_body = senders.rendered_github_body(str(payload["body"]), item["marker"])
    number, url = gh.api_create_issue(repo, str(payload["title"]), full_body, labels=labels)
    return senders.SendResult(external_id=str(number), external_url=url)


def _issue_look(item: dict, since_iso: str) -> senders.LookResult:
    obj = item["object"]
    if "#" in obj:
        repo, number = _parse_issue(obj)
        payload = parse_issue_payload(str(item.get("body") or ""), is_edit=True)
        expected = senders.rendered_github_body(str(payload["body"]), item["marker"]).replace("\r\n", "\n").rstrip()
        current = gh.issue_body(repo, number).replace("\r\n", "\n").rstrip()
        needle = f"<!-- {item['marker']} -->"
        if current == expected or needle in current:
            return senders.LookResult(
                found=True,
                external_id=str(number),
                external_url=gh.issue_url(repo, number),
                summary=f"issue view {repo}#{number}: body matched",
            )
        return senders.LookResult(
            found=False,
            external_id=None,
            external_url=None,
            summary=f"issue view {repo}#{number}: body did not match",
        )
    repo = _parse_repo(obj)
    params = {
        "state": "all",
        "sort": "created",
        "direction": "desc",
        "since": since_iso,
        "per_page": 100,
    }
    path = f"repos/{repo}/issues"
    rows = gh.api_list(path, params, strict=True)
    needle = f"<!-- {item['marker']} -->"
    query_str = urlencode(params)
    issues_seen = 0
    for row in rows:
        if "pull_request" in row:
            continue
        issues_seen += 1
        if needle in (row.get("body") or ""):
            number = row.get("number")
            if number is None:
                raise gh.GhUnsure("GitHub issue lookup hit missing number")
            return senders.LookResult(
                found=True,
                external_id=str(number),
                external_url=str(row.get("html_url") or gh.issue_url(repo, number)),
                summary=f"GET {path}?{query_str}: found number={number} ({issues_seen} issues)",
            )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"GET {path}?{query_str}: not found ({issues_seen} issues)",
    )


def _label_send(item: dict, body: str) -> senders.SendResult:
    repo, number = _parse_issue(item["object"])
    payload = parse_label_payload(body)
    add = list(payload["add"])
    remove = list(payload["remove"])
    if add:
        _ensure_labels(repo, add)
    if add or remove:
        gh.issue_labels(repo, number, add=add, remove=remove)
    return senders.SendResult(external_id=str(number), external_url=gh.issue_url(repo, number))


def _label_look(item: dict, since_iso: str) -> senders.LookResult:
    repo, number = _parse_issue(item["object"])
    payload = parse_label_payload(str(item.get("body") or "{}"))
    add = set(payload["add"])
    remove = set(payload["remove"])
    path = f"repos/{repo}/issues/{number}/labels"
    rows = gh.api_list(path, strict=True)
    current = {str(row.get("name") or "") for row in rows if row.get("name")}
    if add <= current and remove.isdisjoint(current):
        return senders.LookResult(
            found=True,
            external_id=str(number),
            external_url=gh.issue_url(repo, number),
            summary=f"GET {path}: labels matched ({len(current)} labels)",
        )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"GET {path}: labels did not match ({len(current)} labels)",
    )


def _status_send(item: dict, body: str) -> senders.SendResult:
    repo, number = _parse_issue(item["object"])
    state = parse_status_payload(body)
    if state == "closed":
        gh.issue_close(repo, number)
    else:
        gh.issue_reopen(repo, number)
    return senders.SendResult(external_id=str(number), external_url=gh.issue_url(repo, number))


def _status_look(item: dict, since_iso: str) -> senders.LookResult:
    repo, number = _parse_issue(item["object"])
    wanted = parse_status_payload(str(item.get("body") or "{}"))
    expected = "CLOSED" if wanted == "closed" else "OPEN"
    current = gh.issue_state(repo, number)
    if current == expected:
        return senders.LookResult(
            found=True,
            external_id=str(number),
            external_url=gh.issue_url(repo, number),
            summary=f"issue_state {repo}#{number}: {current}",
        )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"issue_state {repo}#{number}: {current or 'missing'} != {expected}",
    )


def parse_pr_payload(body: str) -> dict[str, object]:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise ValueError(f"GitHub PR payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("GitHub PR payload must be a JSON object")
    for forbidden in _FORBIDDEN_HOST_KEYS:
        if forbidden in parsed:
            raise ValueError(f"GitHub PR payload must not hold host key {forbidden!r}")
    action = str(parsed.get("action") or "create").strip()
    if action not in ("create", "body", "draft", "merge"):
        raise ValueError(f"unsupported GitHub PR action: {action!r}")
    return {**parsed, "action": action}


def _pr_url_from_payload(item: dict, payload: dict[str, object]) -> tuple[str, str, int]:
    url = str(payload.get("url") or "").strip()
    if url:
        return url, gh.pr_repo(url), gh.pr_number(url)
    repo = str(payload.get("repo") or "").strip()
    number = payload.get("number")
    if repo and number is not None:
        num = _parse_positive_int(str(number), "PR number")
        return f"https://github.com/{gh._safe_repo(repo)}/pull/{num}", gh._safe_repo(repo), num
    obj_repo, obj_num = _parse_issue(item["object"])
    return f"https://github.com/{obj_repo}/pull/{obj_num}", obj_repo, obj_num


def _scan_pr_payload(payload: dict[str, object]) -> None:
    for field in ("title", "body", "link"):
        val = payload.get(field)
        if isinstance(val, str) and (hit := send_push.scan_text_for_secrets(val)):
            raise send_push.SecretScanError(f"secret scan blocked PR {payload['action']}: {field} contains {hit}")
    head = str(payload.get("head") or "").strip()
    repo_name = str(payload.get("name") or payload.get("repo") or "").strip()
    base = str(payload.get("base") or "").strip()
    if head and repo_name:
        try:
            cfg = config.load()
        except Exception:
            cfg = None
        repo = emits.config_repo(cfg, repo_name) if cfg is not None else None
        if repo is not None and repo.path.is_dir():
            has_head = util.sh(["git", "cat-file", "-e", f"{head}^{{commit}}"], cwd=repo.path).returncode == 0
            if has_head:
                send_push.scan_commit_diff(repo.path, head, base=base)


def _pr_send(item: dict, body: str) -> senders.SendResult:
    payload = parse_pr_payload(body)
    action = str(payload["action"])
    _scan_pr_payload(payload)

    if action == "create":
        repo = gh._safe_repo(str(payload.get("repo") or _parse_repo(item["object"])))
        branch = str(payload.get("branch") or "").strip()
        base = str(payload.get("base") or "").strip()
        title = str(payload.get("title") or "").strip()
        pr_body = str(payload.get("body") or "")
        link = str(payload.get("link") or "")
        labels = [str(lb) for lb in (payload.get("labels") or ()) if str(lb).strip()]
        draft = bool(payload.get("draft", False))
        name = str(payload.get("name") or "").strip()
        head = str(payload.get("head") or "").strip()
        lease = str(payload.get("lease") or "").strip()
        if payload.get("push_branch"):
            cfg = config.load()
            cfg_repo = emits.config_repo(cfg, name or repo)
            if cfg_repo and cfg_repo.target_branch and (base != cfg_repo.target_branch or branch == cfg_repo.target_branch):
                raise ValueError(f"the queued PR must target {cfg_repo.target_branch} from a separate work branch")
            send_push.push_pr_branch(
                cfg,
                repo_name=name or repo,
                base=base,
                branch=branch,
                head=head,
                lease=lease,
                use_lease=bool(lease),
                item=item,
            )
        if labels:
            _ensure_labels(repo, labels)
        full_body = senders.rendered_github_body(pr_body, item["marker"])
        try:
            url = gh.pr_create(
                repo,
                branch,
                base,
                title,
                full_body,
                labels=labels,
                draft=draft,
                link=link,
                single_attempt=True,
            )
        except gh.GhError as error:
            if not isinstance(error, gh.GhUnsure) and "already exists" in str(error).lower():
                existing_url = gh.open_pr_url(repo, branch)
                if existing_url:
                    return senders.SendResult(external_id=str(gh.pr_number(existing_url)), external_url=existing_url)
            raise
        return senders.SendResult(external_id=str(gh.pr_number(url)), external_url=url)

    url, repo, number = _pr_url_from_payload(item, payload)
    if action == "body":
        pr_body = str(payload.get("body") or "")
        link = str(payload.get("link") or "")
        full_body = senders.rendered_github_body(pr_body, item["marker"])
        gh.pr_edit_body(url, full_body, link=link)
        return senders.SendResult(external_id=str(number), external_url=url)

    if action == "draft":
        draft = bool(payload.get("draft", False))
        gh.pr_set_draft(url, draft)
        return senders.SendResult(external_id=str(number), external_url=url)

    if action == "merge":
        head = str(payload.get("head") or "")
        base = str(payload.get("base") or "")
        base_sha = str(payload.get("base_sha") or "")
        track_moved_base = bool(payload.get("track_moved_base", False))
        required = payload.get("required") if isinstance(payload.get("required"), dict) else None
        stage_checks = payload.get("stage_checks") if isinstance(payload.get("stage_checks"), dict) else None
        hold = gh.pr_merge_auto(
            repo,
            number,
            head,
            base,
            base_sha,
            track_moved_base=track_moved_base,
            required=required,
            stage_checks=stage_checks,
        )
        if hold:
            raise PrMergeHeld(hold)
        return senders.SendResult(external_id=str(number), external_url=url)

    raise ValueError(f"unsupported GitHub PR action: {action!r}")


def _pr_look(item: dict, since_iso: str) -> senders.LookResult:
    payload = parse_pr_payload(str(item.get("body") or "{}"))
    action = str(payload["action"])

    if action == "create":
        repo = gh._safe_repo(str(payload.get("repo") or _parse_repo(item["object"])))
        branch = str(payload.get("branch") or "").strip()
        url = gh.open_pr_url(repo, branch)
        if url:
            num = gh.pr_number(url)
            return senders.LookResult(
                found=True,
                external_id=str(num),
                external_url=url,
                summary=f"open_pr_url({repo}, {branch}): found {url}",
            )
        return senders.LookResult(
            found=False,
            external_id=None,
            external_url=None,
            summary=f"open_pr_url({repo}, {branch}): not found",
        )

    url, repo, number = _pr_url_from_payload(item, payload)
    if action == "body":
        current = gh.pr_body(url)
        needle = f"<!-- {item['marker']} -->"
        if needle in current:
            return senders.LookResult(
                found=True,
                external_id=str(number),
                external_url=url,
                summary=f"pr_body {repo}#{number}: marker matched",
            )
        return senders.LookResult(
            found=False,
            external_id=None,
            external_url=None,
            summary=f"pr_body {repo}#{number}: marker not found",
        )

    if action == "draft":
        wanted = bool(payload.get("draft", False))
        current_draft, _head = gh.pr_draft_and_head(url)
        if current_draft == wanted:
            return senders.LookResult(
                found=True,
                external_id=str(number),
                external_url=url,
                summary=f"pr_draft_and_head {repo}#{number}: draft={current_draft}",
            )
        return senders.LookResult(
            found=False,
            external_id=None,
            external_url=None,
            summary=f"pr_draft_and_head {repo}#{number}: draft={current_draft} != {wanted}",
        )

    if action == "merge":
        row = gh.pr_view(url) or {}
        state = str(row.get("state") or "")
        auto_merge = bool(row.get("autoMergeRequest"))
        if state == "MERGED" or auto_merge:
            return senders.LookResult(
                found=True,
                external_id=str(number),
                external_url=url,
                summary=f"pr_view {repo}#{number}: state={state or 'OPEN'} auto_merge={auto_merge}",
            )
        return senders.LookResult(
            found=False,
            external_id=None,
            external_url=None,
            summary=f"pr_view {repo}#{number}: state={state or 'unknown'} auto_merge={auto_merge}",
        )

    raise ValueError(f"unsupported GitHub PR action: {action!r}")


def _pr_preview(item: dict, body: str) -> str:
    payload = parse_pr_payload(body)
    action = str(payload["action"])
    if action == "create":
        rendered = senders.rendered_github_body(str(payload.get("body") or ""), item["marker"])
        link = str(payload.get("link") or "").strip()
        parts = [str(payload.get("title") or "").strip(), link, rendered]
        return "\n\n".join(p for p in parts if p)
    if action == "body":
        rendered = senders.rendered_github_body(str(payload.get("body") or ""), item["marker"])
        link = str(payload.get("link") or "").strip()
        return f"{link}\n\n{rendered}" if link else rendered
    if action == "draft":
        return f"pr draft={bool(payload.get('draft', False))}"
    return f"pr merge {payload.get('repo') or item.get('object')}#{payload.get('number') or ''}"


def _comment_preview(item: dict, body: str) -> str:
    return senders.rendered_github_body(body, item["marker"])


def _issue_preview(item: dict, body: str) -> str:
    try:
        raw = json.loads(body)
    except ValueError:
        raw = None
    if isinstance(raw, dict) and raw.get("action") == "edit" and "field" in raw:
        return f"{raw['field']}: {raw.get('value', '')}"
    is_edit = "#" in (item.get("object") or "")
    payload = parse_issue_payload(body, is_edit=is_edit)
    full_body = senders.rendered_github_body(str(payload["body"]), item["marker"])
    if is_edit:
        return full_body
    return f"{payload['title']}\n\n{full_body}"


def _label_preview(item: dict, body: str) -> str:
    payload = parse_label_payload(body)
    parts: list[str] = []
    if payload["add"]:
        parts.append("+" + ", +".join(payload["add"]))
    if payload["remove"]:
        parts.append("-" + ", -".join(payload["remove"]))
    return " ".join(parts) or "(no label changes)"


def _status_preview(item: dict, body: str) -> str:
    return f"state={parse_status_payload(body)}"


def register_all() -> None:
    senders.register(senders.Sender("github", "comment", _comment_send, _comment_look, _comment_preview))
    senders.register(senders.Sender("github", "reply", _reply_send, _reply_look, _comment_preview))
    senders.register(senders.Sender("github", "issue", _issue_send, _issue_look, _issue_preview))
    senders.register(senders.Sender("github", "label", _label_send, _label_look, _label_preview))
    senders.register(senders.Sender("github", "status", _status_send, _status_look, _status_preview))
    senders.register(senders.Sender("github", "pr", _pr_send, _pr_look, _pr_preview))
