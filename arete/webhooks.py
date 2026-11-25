from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from urllib.parse import parse_qs

from . import loops, paths, secret_move, secretstore, sources, util
from .refusals import Stated
from .store import db as foundation
from .store import sources as source_store

HOOKS_DIR = paths.CONFIG_DIR / "hooks"
KEY_NAME = "WEBHOOK_SECRET"
HOOK_PATH = "/hook/"
BODY_KEY_WINDOW = timedelta(minutes=10)
MAX_HOOK_BODY = 1024 * 1024
MAX_EVENT_BODY = 60_000
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


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
    if not loop or not loop.trigger.webhook:
        raise Stated(404, f"No webhook for {name}.")
    if loop.error:
        raise Stated(409, f"The loop {name} is broken.")
    unique_scope(name, found)
    target = scope(name)
    if not secretstore.SCOPE.fullmatch(target):
        raise Stated(400, "This loop name cannot be used as a secret address.")
    value = secrets.token_urlsafe(32)
    secretstore.put(target, KEY_NAME, value, "webhook_secret", actor, provider="webhook", managed_by="arete")
    return value


def bearer_token(authorization: str) -> str:
    scheme, _, credentials = authorization.strip().partition(" ")
    return credentials.strip() if scheme.lower() == "bearer" else authorization.strip()


def check(name: str, headers, body: bytes) -> None:
    key = secret(name)
    if not key:
        raise Stated(404, f"No webhook for {name}.")
    signature = headers.get("X-Hub-Signature-256", "")
    token = headers.get("X-Arete-Token", "") or bearer_token(headers.get("Authorization", ""))
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
    return headers.get("X-GitHub-Delivery") or headers.get("X-Request-Id") or ""


def event_of(name: str, headers, body: bytes) -> dict:
    text = body_text(headers, body)
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        data = {"text": text[:20000]}
    if not isinstance(data, dict):
        data = {"value": data}
    github_event = headers.get("X-GitHub-Event", "")
    action = data.get("action", "") if isinstance(data.get("action"), str) else ""
    title = (f"GitHub {github_event}{' ' + action if action else ''}" if github_event
             else str(data.get("title") or data.get("summary") or data.get("text") or f"webhook for {name}"))
    key = delivery_id(headers) or f"sha256:{hashlib.sha256(body).hexdigest()}"
    repo = ((data.get("repository") or {}).get("full_name", "") if isinstance(data.get("repository"), dict) else
            str(data.get("repo", "")))
    return sources.event("webhook" if not github_event else f"github.{github_event}", f"{name}#webhook", key, title,
                         repo=repo, url=str(data.get("url", ""))[:500], trust="untrusted",
                         data=data if len(text) <= MAX_EVENT_BODY else {"truncated": text[:MAX_EVENT_BODY]})


def receive(name: str, headers, body: bytes) -> dict:
    found = loops.load_all()
    loop = next((x for x in found if x.name == name), None)
    if not loop or not loop.trigger.webhook:
        raise Stated(404, f"No webhook for {name}.")
    unique_scope(name, found)
    check(name, headers, body)
    if not loop.enabled or loop.error:
        raise Stated(409, f"The loop {name} is turned off.")
    e = event_of(name, headers, body)
    if why := loop.trigger.refusal(e):
        raise Stated(403, f"The loop {name} {why}.")
    with foundation.tx():
        if not delivery_id(headers):
            source_store.forget_seen(e["source"], e["key"], util.now() - BODY_KEY_WINDOW)
        if not source_store.claim_seen(e["source"], [e["key"]]):
            return {"queued": False, "why": "already received", "key": e["key"]}
        [item] = sources.fire(loop, "webhook", [e])
    return {"queued": True, "item": item, "key": e["key"], "at": util.iso()}
