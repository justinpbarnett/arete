from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
LOOKUP_TIMEOUT = 3
MAX_METADATA = 64 * 1024
MAX_LOGIN_METADATA = 4 * 1024 * 1024
CACHE_SECONDS = 300
CACHE_SIZE = 128
PI_SUBSCRIPTIONS = frozenset({"anthropic", "openai", "openai-codex", "github-copilot", "xai", "kimi-coding", "meta"})


@dataclass(frozen=True)
class Auth:
    mode: str = ""
    family: str = ""
    identity: str = ""
    generation: str = ""


cache: dict[str, tuple[float, Auth]] = {}


def fingerprint(*values) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def read(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            data = source.read(MAX_LOGIN_METADATA + 1)
        return mapping(json.loads(data)) if len(data) <= MAX_LOGIN_METADATA else {}
    except (OSError, ValueError):
        return {}


def stable(value, family: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or value != value.strip():
        return ""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value if family == "codex" and re.fullmatch(r"[A-Za-z0-9_-]+", value) else ""


def identity(family: str, values: list) -> str:
    supplied = [v for v in values if v is not None and v != ""]
    normalized = [stable(v, family) for v in supplied]
    if not normalized or not all(normalized) or len(set(normalized)) != 1:
        return ""
    return fingerprint("account-v1", family, normalized[0])


def jwt(token) -> dict:
    if not isinstance(token, str) or len(token) > MAX_METADATA:
        return {}
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        return mapping(json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)))
    except (ValueError, UnicodeError):
        return {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def profile(token: str) -> dict:
    if not isinstance(token, str) or not token or len(token) > MAX_METADATA:
        return {}
    request = urllib.request.Request(PROFILE_URL, headers={"Authorization": f"Bearer {token}",
                                      "anthropic-beta": "oauth-2025-04-20", "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=LOOKUP_TIMEOUT) as response:
            data = response.read(MAX_METADATA + 1)
            if response.status != 200 or response.geturl() != PROFILE_URL or len(data) > MAX_METADATA:
                return {}
        return mapping(json.loads(data))
    except (OSError, ValueError):
        return {}


def cached(generation: str, discover) -> Auth:
    at, found = cache.get(generation, (0, Auth()))
    if at > time.monotonic():
        return found
    found = discover()
    if len(cache) >= CACHE_SIZE:
        cache.clear()
    cache[generation] = (time.monotonic() + CACHE_SECONDS, found)
    return found


def claude(status=None, account=None, token: str = "", source: str = "", lookup=None) -> Auth:
    status, account = mapping(status), mapping(account)
    generation = fingerprint("claude", source, status, account, token)

    def discover():
        method = status.get("authMethod") or status.get("auth_mode")
        mode = "api_key" if method in ("api_key", "apiKey", "api-key") else "subscription"
        if mode == "api_key":
            return Auth(mode, generation=generation)
        values = [status.get("accountUuid"), status.get("account_uuid"), account.get("accountUuid"),
                  account.get("uuid"), mapping(status.get("oauthAccount")).get("accountUuid"),
                  mapping(status.get("account")).get("uuid")]
        if token:
            try:
                found = mapping((lookup or profile)(token))
            except Exception:
                found = {}
            account_uuid = mapping(found.get("account")).get("uuid")
            if not stable(account_uuid, "claude"):
                return Auth(mode, "claude", generation=generation)
            values += [account_uuid]
        return Auth(mode, "claude", identity("claude", values), generation)

    return cached(generation, discover)


def codex(data=None, source: str = "", provider: str = "") -> Auth:
    data = mapping(data)
    generation = fingerprint("codex", source, data, provider)
    selected = data.get("auth_mode")
    if (selected in ("apikey", "api_key") or provider == "openai" or
            (not selected and data.get("OPENAI_API_KEY"))):
        return Auth("api_key", generation=generation)
    if selected and selected not in ("chatgpt", "subscription"):
        return Auth(generation=generation)
    tokens = mapping(data.get("tokens"))
    values = [tokens.get("account_id"), data.get("account_id"), data.get("accountId")]
    malformed = False
    for name in ("id_token", "access_token"):
        token = tokens.get(name)
        if not token:
            continue
        claims = jwt(token)
        malformed |= not bool(claims)
        values += [mapping(claims.get("https://api.openai.com/auth")).get("chatgpt_account_id"),
                   claims.get("chatgpt_account_id")]
    mode = "subscription" if tokens or provider == "chatgpt" or data.get("auth_mode") in ("chatgpt", "subscription") else ""
    return Auth(mode, "codex", "" if malformed else identity("codex", values), generation)


def pi(provider: str, credential=None, source: str = "", lookup=None) -> Auth:
    credential = mapping(credential)
    generation = fingerprint("pi", source, provider, credential)
    if credential.get("type") != "oauth":
        return Auth("api_key", generation=generation)
    if provider not in PI_SUBSCRIPTIONS:
        return Auth("api_key", generation=generation)
    family = {"anthropic": "claude", "openai-codex": "codex"}.get(provider, "")
    if family == "claude":
        found = claude(account={"accountUuid": credential.get("accountUuid")},
                       token=credential.get("access", ""), source=source, lookup=lookup)
        return Auth("subscription", family, found.identity, generation)
    if family == "codex":
        token = credential.get("access")
        claims = jwt(token) if token else {}
        values = [credential.get("accountId"),
                  mapping(claims.get("https://api.openai.com/auth")).get("chatgpt_account_id"),
                  claims.get("chatgpt_account_id")]
        return Auth("subscription", family, "" if token and not claims else identity(family, values), generation)
    return Auth("subscription", generation=generation)


def scope(auth: Auth, limit: str, unknown: str = "") -> str:
    account = auth.identity or unknown
    return "subscription-v1:" + json.dumps([auth.family, account, limit], separators=(",", ":")) if account else ""
