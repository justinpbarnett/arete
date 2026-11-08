from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from . import loops, paths, sources, state, util

HOOKS_DIR = paths.CONFIG_DIR / "hooks"
HOOK_PATH = "/hook/"
MAX_HOOK_BODY = 1024 * 1024
MAX_EVENT_BODY = 60_000


class HookRefused(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


def hook_path(name: str) -> str:
    return HOOK_PATH + name


def secret_file(name: str):
    return HOOKS_DIR / f"{name.replace('/', '.')}.secret"


def secret(name: str, create: bool = True) -> str:
    p = secret_file(name)
    if p.is_file():
        return p.read_text().strip()
    if not create:
        return ""
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32) + "\n")
    return p.read_text().strip()


def check(name: str, headers, body: bytes) -> None:
    key = secret(name, create=False)
    if not key:
        raise HookRefused(404, f"No webhook for {name}.")
    signature = headers.get("X-Hub-Signature-256", "")
    token = headers.get("X-Arete-Token", "") or headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if signature:
        expected = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            raise HookRefused(403, "The signature does not match the webhook secret.")
        return
    if not token:
        raise HookRefused(401, "A webhook needs the X-Arete-Token header, a Bearer token, or a GitHub signature.")
    if not hmac.compare_digest(token.encode(), key.encode()):
        raise HookRefused(403, "The token does not match the webhook secret.")


def event_of(name: str, headers, body: bytes) -> dict:
    text = body.decode("utf-8", "replace")
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
    key = headers.get("X-GitHub-Delivery") or headers.get("X-Request-Id") or secrets.token_hex(10)
    repo = ((data.get("repository") or {}).get("full_name", "") if isinstance(data.get("repository"), dict) else
            str(data.get("repo", "")))
    return sources.event("webhook" if not github_event else f"github.{github_event}", f"{name}#webhook", key, title,
                         repo=repo, url=str(data.get("url", ""))[:500], trust="untrusted",
                         data=data if len(body) <= MAX_EVENT_BODY else {"truncated": text[:MAX_EVENT_BODY]})


def receive(name: str, headers, body: bytes) -> dict:
    loop = next((x for x in loops.load_all() if x.name == name), None)
    if not loop or not loop.trigger.webhook:
        raise HookRefused(404, f"No webhook for {name}.")
    check(name, headers, body)
    if not loop.enabled or loop.error:
        raise HookRefused(409, f"The loop {name} is turned off.")
    e = event_of(name, headers, body)
    if why := loop.trigger.refusal(e):
        raise HookRefused(403, f"The loop {name} {why}.")
    if e["key"] in state.seen_keys(e["source"]):
        return {"queued": False, "why": "already received", "key": e["key"]}
    state.mark_seen(e["source"], [e["key"]])
    [item] = sources.fire(loop, "webhook", [e])
    return {"queued": True, "item": item, "key": e["key"], "at": util.iso()}
