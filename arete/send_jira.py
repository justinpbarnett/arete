from __future__ import annotations

import json
from datetime import UTC

from . import adf, config, jira, senders, util


def _parse_key(obj: str) -> str:
    if not isinstance(obj, str) or not obj.startswith("jira:"):
        raise ValueError(f"expected jira:<KEY-123>, got {obj!r}")
    key = obj.removeprefix("jira:").strip().upper()
    if not jira.KEY_RE.fullmatch(key):
        raise ValueError(f"invalid Jira issue key: {key!r}")
    return key


def _client_for(item: dict) -> jira.Client:
    cfg = config.load()
    client = jira.Client.from_env(cfg.jira)
    task_id = str(item.get("task_id") or "")
    repo_name = task_id.split(" ", 1)[0] if " " in task_id else ""
    if repo_name:
        for repo in cfg.repos:
            if repo.name == repo_name and repo.jira_site and client.base != repo.jira_site:
                raise jira.JiraError(f"{repo.name}: connected Jira site differs from the enrolled site")
    return client


def parse_label_payload(body: str) -> dict[str, object]:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise ValueError(f"Jira label payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("Jira label payload must be a JSON object")
    add = [str(lb) for lb in (parsed.get("add") or ()) if str(lb).strip()]
    remove = [str(lb) for lb in (parsed.get("remove") or ()) if str(lb).strip()]
    family = str(parsed.get("family") or "")
    if family not in ("", "state"):
        raise ValueError(f"unsupported label family: {family!r}")
    return {"add": add, "remove": remove, "family": family}


def parse_transition_payload(body: str) -> dict[str, object]:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise ValueError(f"Jira transition payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("Jira transition payload must be a JSON object")
    target = str(parsed.get("target") or parsed.get("status") or "").strip()
    if not target:
        raise ValueError("Jira transition payload requires a non-empty target status")
    return {
        "target": target,
        "keep_open": bool(parsed.get("keep_open", False)),
        "keep_done": bool(parsed.get("keep_done", False)),
    }


def _comment_text(raw_body: object) -> str:
    if isinstance(raw_body, str):
        return raw_body
    if isinstance(raw_body, dict):
        try:
            return adf.to_text(raw_body)
        except ValueError:
            return ""
    return ""


def _comment_has_marker_prop(row: dict, marker: str) -> bool:
    props = row.get("properties")
    if isinstance(props, dict):
        val = props.get("arete.ob")
        return val == marker or (isinstance(val, dict) and val.get("value") == marker)
    if isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict) or prop.get("key") != "arete.ob":
                continue
            val = prop.get("value")
            if val == marker or (isinstance(val, dict) and val.get("value") == marker):
                return True
    return False


def _created_after(created: object, since_dt) -> bool:
    if not isinstance(created, str) or not created.strip():
        return True
    try:
        dt = jira.parse_time(created)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC) >= since_dt


def _comment_send(item: dict, body: str) -> senders.SendResult:
    key = _parse_key(item["object"])
    client = _client_for(item)
    full_body = senders.rendered_jira_body(body, item["marker"])
    comment_id, url = client.comment_outbox(key, full_body, item["marker"])
    return senders.SendResult(external_id=comment_id, external_url=url)


def _comment_look(item: dict, since_iso: str) -> senders.LookResult:
    key = _parse_key(item["object"])
    client = _client_for(item)
    marker = str(item["marker"])
    since_dt = util.parse_iso(since_iso).astimezone(UTC)
    rows = client.comments_with_properties(key)
    checked = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _created_after(row.get("created"), since_dt):
            continue
        checked += 1
        matched_prop = _comment_has_marker_prop(row, marker)
        text = _comment_text(row.get("body"))
        if matched_prop or marker in text:
            ext_id = row.get("id")
            if ext_id is None or not str(ext_id).strip():
                raise jira.JiraUnsure("Jira comment lookup hit missing id")
            cid = str(ext_id)
            return senders.LookResult(
                found=True,
                external_id=cid,
                external_url=client.comment_url(key, cid),
                summary=f"GET issue/{key}/comment?expand=properties: found id={cid} ({checked} recent comments)",
            )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"GET issue/{key}/comment?expand=properties: not found ({checked} recent comments)",
    )


def _issue_status(client: jira.Client, key: str) -> tuple[str, str, str]:
    fields = (client.issue(key, ["status", "updated"]).get("fields") or {})
    status = fields.get("status") or {}
    name = str(status.get("name") or "")
    category = str((status.get("statusCategory") or {}).get("key") or "")
    updated = str(fields.get("updated") or "")
    return name, category, updated


def _transition_send(item: dict, body: str) -> senders.SendResult:
    from contextlib import suppress

    key = _parse_key(item["object"])
    payload = parse_transition_payload(body)
    target = str(payload["target"])
    keep_open = bool(payload["keep_open"])
    keep_done = bool(payload["keep_done"])
    client = _client_for(item)

    status, category, before = _issue_status(client, key)
    if status.lower() == target.lower() or (keep_done and category == "done"):
        return senders.SendResult(external_id=key, external_url=client.browse_url(key))

    try:
        status_exists = target.lower() in {s.lower() for s in client.statuses(key.rsplit("-", 1)[0])}
    except jira.JiraError:
        status_exists = False
    want = target.strip().lower()
    found = (client.get(f"/rest/api/2/issue/{jira._path_segment(key)}/transitions") or {}).get("transitions", [])
    pick = next((t for t in found if ((t.get("to") or {}).get("name") or "").lower() == want), None)
    if pick is None and not status_exists:
        pick = next((t for t in found if t.get("name", "").lower() == want), None)
    if pick is None:
        raise jira.JiraError(f"{key} has no {'non-closing ' if keep_open else ''}transition to '{target}' from '{status}'")
    if keep_open:
        destination = ((pick.get("to") or {}).get("statusCategory") or {}).get("key")
        if not destination or destination == "done":
            raise jira.JiraError(f"{key} has no non-closing transition to '{target}' from '{status}'")
    client.transition(key, str(pick["id"]))
    if before:
        with suppress(Exception):
            after = str(((client.issue(key, ["updated"]).get("fields") or {}).get("updated")) or "")
            if after and after != before:
                from .store import triage as triage_store

                triage_store.copy_verdict_after_move(key, before, after)
    return senders.SendResult(external_id=key, external_url=client.browse_url(key))


def _transition_look(item: dict, since_iso: str) -> senders.LookResult:
    key = _parse_key(item["object"])
    payload = parse_transition_payload(str(item.get("body") or "{}"))
    target = str(payload["target"])
    keep_done = bool(payload["keep_done"])
    client = _client_for(item)
    status, category, _ = _issue_status(client, key)
    if status.lower() == target.lower() or (keep_done and category == "done"):
        return senders.LookResult(
            found=True,
            external_id=key,
            external_url=client.browse_url(key),
            summary=f"GET issue/{key} status={status!r}: matched target {target!r}",
        )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"GET issue/{key} status={status!r}: != {target!r}",
    )


def _label_send(item: dict, body: str) -> senders.SendResult:
    key = _parse_key(item["object"])
    payload = parse_label_payload(body)
    add = list(payload["add"])
    remove = list(payload["remove"])
    client = _client_for(item)
    if add or remove:
        client.edit_labels(key, add=add, remove=remove)
    return senders.SendResult(external_id=key, external_url=client.browse_url(key))


def _label_look(item: dict, since_iso: str) -> senders.LookResult:
    key = _parse_key(item["object"])
    payload = parse_label_payload(str(item.get("body") or "{}"))
    add = set(payload["add"])
    remove = set(payload["remove"])
    client = _client_for(item)
    fields = (client.issue(key, ["labels"]).get("fields") or {})
    current = {str(lb) for lb in (fields.get("labels") or ()) if str(lb).strip()}
    if add <= current and remove.isdisjoint(current):
        return senders.LookResult(
            found=True,
            external_id=key,
            external_url=client.browse_url(key),
            summary=f"GET issue/{key} labels: matched ({len(current)} labels)",
        )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"GET issue/{key} labels: did not match ({len(current)} labels)",
    )


def _parse_project(obj: str) -> str:
    if not isinstance(obj, str) or not obj.startswith("jira:"):
        raise ValueError(f"expected jira:<PROJECT>, got {obj!r}")
    project = obj.removeprefix("jira:").strip().upper()
    if not project or not all(ch.isalnum() or ch == "_" for ch in project) or not project[0].isalpha():
        raise ValueError(f"invalid Jira project key: {project!r}")
    return project


def parse_issue_payload(body: str) -> dict[str, object]:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise ValueError(f"Jira issue payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("Jira issue payload must be a JSON object")
    title = str(parsed.get("title") or "").strip()
    if not title:
        raise ValueError("Jira issue payload requires a non-empty title")
    labels = [str(lb) for lb in (parsed.get("labels") or ()) if str(lb).strip()]
    dest = parsed.get("destination") if isinstance(parsed.get("destination"), dict) else {}
    return {
        "title": title,
        "body": str(parsed.get("body") or ""),
        "labels": labels,
        "destination": dest,
        "issue_type": str(parsed.get("issue_type") or ""),
    }


def _issue_send(item: dict, body: str) -> senders.SendResult:
    project = _parse_project(item["object"])
    payload = parse_issue_payload(body)
    dest = payload["destination"]
    client = _client_for(item)
    site = str(dest.get("site") or "").strip().rstrip("/")
    if site and client.base.rstrip("/") != site:
        raise jira.JiraError("connected Jira site differs from the enrolled site")
    account = str((client.me() or {}).get("accountId") or "").strip()
    if not account:
        raise jira.JiraError("cannot confirm the Jira account for this ticket")
    route = str(dest.get("route") or "").strip()
    labels = list(dict.fromkeys([*payload["labels"], *([route] if route else [])]))
    full_body = senders.rendered_jira_body(str(payload["body"]), item["marker"])
    url = client.create_task(
        project,
        account,
        str(payload["title"]),
        full_body,
        labels,
        issue_type=str(payload["issue_type"] or ""),
    )
    key = url.rsplit("/", 1)[-1]
    return senders.SendResult(external_id=key, external_url=url)


def _issue_look(item: dict, since_iso: str) -> senders.LookResult:
    project = _parse_project(item["object"])
    marker = str(item["marker"])
    client = _client_for(item)
    query = f"project = {json.dumps(project)} AND description ~ {json.dumps(marker)}"
    rows = client.search(
        query,
        ["description"],
        limit=10,
        include=lambda issue: marker in _comment_text((issue.get("fields") or {}).get("description")),
    )
    if rows:
        key = str(rows[0]["key"])
        return senders.LookResult(
            found=True,
            external_id=key,
            external_url=client.browse_url(key),
            summary=f"search {project}: found {key}",
        )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"search {project}: not found",
    )


def _issue_preview(item: dict, body: str) -> str:
    try:
        raw = json.loads(body)
    except ValueError:
        raw = None
    if isinstance(raw, dict) and raw.get("action") == "edit" and "field" in raw:
        return f"{raw['field']}: {raw.get('value', '')}"
    payload = parse_issue_payload(body)
    rendered = senders.rendered_jira_body(str(payload["body"]), item["marker"])
    return f"{payload['title']}\n\n{rendered}"


def _comment_preview(item: dict, body: str) -> str:
    return senders.rendered_jira_body(body, item["marker"])


def _transition_preview(item: dict, body: str) -> str:
    payload = parse_transition_payload(body)
    return f"transition={payload['target']}"


def _label_preview(item: dict, body: str) -> str:
    payload = parse_label_payload(body)
    parts: list[str] = []
    if payload["add"]:
        parts.append("+" + ", +".join(payload["add"]))
    if payload["remove"]:
        parts.append("-" + ", -".join(payload["remove"]))
    return " ".join(parts) or "(no label changes)"


def register_all() -> None:
    senders.register(senders.Sender("jira", "comment", _comment_send, _comment_look, _comment_preview))
    senders.register(senders.Sender("jira", "issue", _issue_send, _issue_look, _issue_preview))
    senders.register(senders.Sender("jira", "transition", _transition_send, _transition_look, _transition_preview))
    senders.register(senders.Sender("jira", "status", _transition_send, _transition_look, _transition_preview))
    senders.register(senders.Sender("jira", "label", _label_send, _label_look, _label_preview))
