from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
from datetime import timedelta
from urllib.parse import parse_qs

from . import loops, paths, secret_move, secretstore, sources, util
from .refusals import Stated
from .store import db as foundation
from .store import sources as source_store

HOOKS_DIR = paths.CONFIG_DIR / "hooks"
KEY_NAME = "WEBHOOK_SECRET"
HOOK_PATH = "/hook/"
SPEC_HOOK = "spec-amendments"
BODY_KEY_WINDOW = timedelta(minutes=10)
MAX_HOOK_BODY = 1024 * 1024
MAX_EVENT_BODY = 60_000
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
GITHUB_SPEC_EVENTS = {
    "github.issues": {"edited"},
    "github.issue_comment": {"created", "edited"},
}
JIRA_SPEC_EVENTS = {"jira:issue_updated", "comment_created", "comment_updated"}


def hook_path(name: str) -> str:
    return HOOK_PATH + name


def scope(name):
    return "connector:loop." + name.replace("/", ".")


def legacy_file(name):
    stem = scope(name).removeprefix("connector:loop.")
    return secret_move.fallback(HOOKS_DIR / (stem + secret_move.HOOK_SUFFIX))


def present(name):
    target = scope(name)
    row = secretstore.registered(target, KEY_NAME) if secretstore.SCOPE.fullmatch(target) else None
    if row is not None:
        return row["status"] == "set"
    old = legacy_file(name)
    return bool(old and old.is_file() and old.stat().st_size)


def secret(name: str) -> str:
    target = scope(name)
    if secretstore.SCOPE.fullmatch(target):
        row, value = secretstore.lookup(target, KEY_NAME)
        if row is not None:
            return value
    old = legacy_file(name)
    try:
        return paths.read_confined(old).decode().strip() if old else ""
    except FileNotFoundError:
        return ""


def unique_scope(name, found):
    if any(other.name != name and other.trigger.webhook and scope(other.name) == scope(name) for other in found):
        raise Stated(409, "More than one webhook uses that secret address.")


def rotate(name, actor):
    found = loops.load_all()
    loop = next((loop for loop in found if loop.name == name), None)
    is_spec_hook = loop is None and name == SPEC_HOOK
    if not is_spec_hook and (not loop or not loop.trigger.webhook):
        raise Stated(404, f"No webhook for {name}.")
    if loop and loop.error:
        raise Stated(409, f"The loop {name} is broken.")
    unique_scope(name, found)
    target = scope(name)
    if not secretstore.SCOPE.fullmatch(target):
        raise Stated(400, "This loop name cannot be used as a secret address.")
    value = secrets.token_urlsafe(32)
    secretstore.put(target, KEY_NAME, value, "webhook_secret", actor, provider="webhook", managed_by="arete")
    return value


def _single_header(headers, name: str) -> str:
    if hasattr(headers, "get_all"):
        values = headers.get_all(name) or []
        if len(values) > 1:
            raise Stated(400, f"Send at most one {name} header.")
        return (values[0] if values else "").strip()
    return str(headers.get(name) or "").strip()


def bearer_token(authorization: str) -> str:
    scheme, _, credentials = authorization.strip().partition(" ")
    return credentials.strip() if scheme.lower() == "bearer" else authorization.strip()


def check(name: str, headers, body: bytes) -> None:
    key = secret(name)
    if not key:
        raise Stated(404, f"No webhook for {name}.")
    signature = _single_header(headers, "X-Hub-Signature-256")
    header_token = _single_header(headers, "X-Arete-Token")
    auth_header = _single_header(headers, "Authorization")
    token = header_token or bearer_token(auth_header)
    if signature:
        expected = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            raise Stated(403, "The signature does not match the webhook secret.")
        return
    if not token:
        raise Stated(401, "A webhook needs the X-Arete-Token header, a Bearer token, or a GitHub signature.")
    if not hmac.compare_digest(token.encode(), key.encode()):
        raise Stated(403, "The token does not match the webhook secret.")


def body_text(headers, body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    if headers.get("Content-Type", "").partition(";")[0].strip().lower() != FORM_CONTENT_TYPE:
        return text
    return (parse_qs(text).get("payload") or [text])[0]


def delivery_id(headers) -> str:
    raw = headers.get("X-GitHub-Delivery") or headers.get("X-Request-Id") or ""
    return str(raw).strip()[:200]


def event_of(name: str, headers, body: bytes) -> dict:
    text = body_text(headers, body)
    try:
        data = json.loads(text) if text.strip() else {}
    except (json.JSONDecodeError, RecursionError, ValueError):
        data = {"text": text[:20000]}
    if not isinstance(data, dict):
        data = {"value": data}
    github_event = str(headers.get("X-GitHub-Event") or "").strip()[:100]
    action = data.get("action", "").strip()[:100] if isinstance(data.get("action"), str) else ""
    title = (f"GitHub {github_event}{' ' + action if action else ''}" if github_event
             else str(data.get("title") or data.get("summary") or data.get("text") or f"webhook for {name}"))
    key = delivery_id(headers) or f"sha256:{hashlib.sha256(body).hexdigest()}"
    raw_repo = (data.get("repository") or {}).get("full_name") if isinstance(data.get("repository"), dict) else data.get("repo")
    repo = raw_repo.strip()[:200] if isinstance(raw_repo, str) else ""
    raw_url = data.get("url")
    url = raw_url.strip()[:500] if isinstance(raw_url, str) else ""
    return sources.event("webhook" if not github_event else f"github.{github_event}", f"{name}#webhook", key, title,
                         repo=repo, url=url, trust="untrusted",
                         data=data if len(text) <= MAX_EVENT_BODY else {"truncated": text[:MAX_EVENT_BODY]})


def spec_amendment_target(event: dict) -> tuple[str, str, str] | None:
    """Identify a GitHub or Jira parent issue from an edit/comment webhook event."""
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    kind = event.get("type") or event.get("kind", "")
    if kind in GITHUB_SPEC_EVENTS:
        action = data.get("action")
        if action not in GITHUB_SPEC_EVENTS[kind]:
            return None
        repo = (data.get("repository") or {}).get("full_name") if isinstance(data.get("repository"), dict) else event.get("repo")
        issue = data.get("issue")
        number = issue.get("number") if isinstance(issue, dict) else None
        if isinstance(repo, str) and repo.strip() and isinstance(number, int) and not isinstance(number, bool) and number > 0:
            return "github", repo.strip().casefold(), str(number)
        return None
    jira_event = data.get("webhookEvent")
    if isinstance(jira_event, str) and jira_event in JIRA_SPEC_EVENTS:
        issue = data.get("issue")
        issue_key = issue.get("key") if isinstance(issue, dict) else None
        if isinstance(issue_key, str) and issue_key.strip():
            return "jira", "", issue_key.strip().upper()
    return None


def reconcile_spec_event(event: dict, *, cfg=None) -> list[tuple[str, str]]:
    """Reconcile any matching published spec graph when a webhook notifies of an issue update."""
    target = spec_amendment_target(event)
    if not target:
        return []
    kind, slug, ref = target
    from . import config, gh, jira, spec_amendments, tracker
    from .store import specs, workspaces

    cfg = cfg or config.load()
    matched: list[tuple[str, str]] = []
    for pending in specs.pending():
        if pending.get("state") != "published" or not pending.get("data", {}).get("graph"):
            continue
        repo_name, tid = pending["repo"], str(pending["issue"])
        try:
            repo = cfg.repo(repo_name)
        except config.UnknownRepo:
            continue
        if kind == "github":
            if repo.tracker != "github" or repo.github.casefold() != slug or tid != ref:
                continue
        elif repo.tracker != "jira" or tid.upper() != ref:
            continue
        with workspaces.ticket_lock(repo_name, tid) as got:
            if not got:
                continue
            claim = specs.find(repo_name, tid)
            if not claim or claim.get("state") != "published" or not claim.get("data", {}).get("graph"):
                continue
            try:
                source = tracker.for_ticket(cfg, repo, tid)
                spec_amendments.reconcile(source, claim, source.get(tid))
                matched.append((repo_name, tid))
            except (KeyError, ValueError, OSError, gh.GhError, jira.JiraError, tracker.TaskError):
                logging.getLogger(__name__).exception("Webhook spec amendment reconciliation failed for %s/%s", repo_name, tid)
    return matched


def schedule_spec_reconcile(event: dict) -> bool:
    if not spec_amendment_target(event):
        return False

    def _run():
        try:
            reconcile_spec_event(event)
        except Exception:
            logging.getLogger(__name__).exception("Background webhook spec amendment reconciliation failed")

    threading.Thread(target=_run, name="arete-spec-webhook", daemon=True).start()
    return True


def receive(name: str, headers, body: bytes) -> dict:
    found = loops.load_all()
    loop = next((x for x in found if x.name == name), None)
    is_spec_hook = loop is None and name == SPEC_HOOK
    if not is_spec_hook and (not loop or not loop.trigger.webhook):
        raise Stated(404, f"No webhook for {name}.")
    unique_scope(name, found)
    check(name, headers, body)
    from .store import factory
    try:
        version = factory.admit()
    except factory.Off as error:
        raise Stated(503, str(error)) from error
    if loop and (not loop.enabled or loop.error):
        raise Stated(409, f"The loop {name} is turned off.")
    e = event_of(name, headers, body)
    if loop and (why := loop.trigger.refusal(e)):
        raise Stated(403, f"The loop {name} {why}.")
    with foundation.tx() as c:
        try:
            factory.admit(c, version=version)
        except factory.Off as error:
            raise Stated(503, str(error)) from error
        if not delivery_id(headers):
            source_store.forget_seen(e["source"], e["key"], util.now() - BODY_KEY_WINDOW)
        if not source_store.claim_seen(e["source"], [e["key"]]):
            return {"queued": False, "why": "already received", "key": e["key"]}
        if is_spec_hook:
            item = None
        else:
            [item] = sources.fire(loop, "webhook", [e])
    reconcile = schedule_spec_reconcile(e)
    if is_spec_hook:
        return {"queued": reconcile, "reconcile": reconcile, "key": e["key"], "at": util.iso()}
    return {"queued": True, "item": item, "key": e["key"], "at": util.iso(),
            **({"reconcile": True} if reconcile else {})}
