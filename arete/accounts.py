from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import config, paths, secretstore
from .store import credentials, db, devices, principals

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
LOOKUP_TIMEOUT = 3
MAX_METADATA = 64 * 1024
MAX_LOGIN_METADATA = 4 * 1024 * 1024
CACHE_SECONDS = 300
CACHE_SIZE = 128
PI_SUBSCRIPTIONS = frozenset({"openai", "openai-codex", "github-copilot", "xai", "kimi-coding", "meta"})
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Auth:
    mode: str = ""
    family: str = ""
    identity: str = ""
    generation: str = ""
    credential: str = ""
    provider: str = ""


cache: dict[str, tuple[float, Auth]] = {}


def fingerprint(*values) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def read(path: Path) -> dict:
    try:
        data = paths.read_confined(path, MAX_LOGIN_METADATA + 1)
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


def chatgpt_account_ids(claims: dict) -> list:
    return [mapping(claims.get("https://api.openai.com/auth")).get("chatgpt_account_id"),
            claims.get("chatgpt_account_id")]


def claude(status=None, account=None, token: str = "", source: str = "") -> Auth:
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
                found = mapping(profile(token))
            except Exception:
                found = {}
            account_uuid = mapping(found.get("account")).get("uuid")
            if not stable(account_uuid, "claude"):
                return Auth(mode, "claude", generation=generation)
            values += [account_uuid]
        return Auth(mode, "claude", identity("claude", values), generation)

    found = cached(generation, discover)
    return Auth(found.mode, found.family, found.identity, generation,
                fingerprint("credential-v1", "claude", source, token or [status, account]), "anthropic")


def codex(data=None, source: str = "", provider: str = "") -> Auth:
    data = mapping(data)
    generation = fingerprint("codex", source, data, provider)
    selected = data.get("auth_mode")
    if (selected in ("apikey", "api_key") or provider == "openai" or
            (not selected and data.get("OPENAI_API_KEY"))):
        return Auth("api_key", generation=generation,
                    credential=fingerprint("credential-v1", "codex", source, data.get("OPENAI_API_KEY") or data),
                    provider="openai")
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
        values += chatgpt_account_ids(claims)
    mode = "subscription" if tokens or provider == "chatgpt" or data.get("auth_mode") in ("chatgpt", "subscription") else ""
    return Auth(mode, "codex", "" if malformed else identity("codex", values), generation,
                fingerprint("credential-v1", "codex", source, data), "openai-codex")


def pi(provider: str, credential=None, source: str = "") -> Auth:
    credential = mapping(credential)
    generation = fingerprint("pi", source, provider, credential)
    local = fingerprint("credential-v1", "pi", source, provider, credential)
    if credential.get("type") != "oauth" or provider not in PI_SUBSCRIPTIONS:
        return Auth("api_key", generation=generation, credential=local, provider=provider)
    family = "codex" if provider == "openai-codex" else ""
    if family == "codex":
        token = credential.get("access")
        claims = jwt(token) if token else {}
        values = [credential.get("accountId"), *chatgpt_account_ids(claims)]
        return Auth("subscription", family, "" if token and not claims else identity(family, values), generation,
                    local, provider)
    return Auth("subscription", generation=generation, credential=local, provider=provider)


def scope(auth: Auth, limit: str, unknown: str = "") -> str:
    if auth.mode == "api_key":
        return "credit-v1:" + fingerprint(auth.provider, auth.credential or unknown) if auth.credential or unknown else ""
    account = auth.identity or auth.credential or unknown
    return "subscription-v1:" + json.dumps([auth.family, account, limit], separators=(",", ":")) if account else ""


def record(auth: Auth, *, secret_id: int | None = None, refresh_only: bool = False,
           defer_unconfigured: bool = False) -> dict | None:
    who = auth.identity or auth.credential
    if not (auth.provider and auth.mode and who) or not paths.DB_FILE.exists():
        return None
    provider = re.sub(r"[^a-z0-9_-]+", "-", auth.provider.lower()).strip("-_") or "provider"
    label = f"{provider[:48]}:{auth.mode}"
    secretstore.validate_account_id(label)
    if paths.DB_FILE.parent != paths.STATE_DIR or paths.FINGERPRINT_KEY_FILE.parent != paths.STATE_DIR:
        raise secretstore.StoreError("account registry files must belong to the current state directory")
    db.migrate_on_demand(db.SECRET_REGISTRY_SCHEMA)
    with db.tx() as c:
        settings = config.secret_settings() if defer_unconfigured else None
        if settings is not None and not settings.store and not secretstore.local_key_path().exists():
            if db.metadata(c).keys().isdisjoint((secretstore.KEY_LOCAL, secretstore.KEY_REF)):
                return None
        holder = principals.ensure_local_owner(c)
        if holder is None:
            return None
        key = secretstore.fingerprint_key(settings)
        digest = secretstore.digest(key, "credential", auth.provider, auth.mode, auth.credential or who)
        if not auth.identity and secret_id is None and auth.mode == "subscription":
            login = scope(auth, "")
            refreshes = devices.refresh_links(c)
            aliases = devices.linked_scopes(login, refreshes)
            identities = {s: secretstore.digest(key, "account", auth.provider, auth.mode, auth.family, s)
                          for s in aliases}
            existing = credentials.reconcile_logins(c, auth.provider, auth.mode, identities.values(), holder)
            if refresh_only and existing is None:
                return None
            identity = existing["identity"] if existing else identities[login]
            if existing and any(before == auth.generation and after != before for before, after, _, _ in refreshes):
                digest = existing["fingerprint"]
        else:
            identity = secretstore.digest(key, "account", auth.provider, auth.mode, auth.family,
                                          auth.identity or (secret_id if secret_id is not None else who))
        return credentials.save_account(c, label, auth.provider, auth.mode, holder, "personal", identity, digest,
                                        secret_id)


def record_resolved(auth: Auth, *, refresh_only: bool = False, defer_unconfigured: bool = False,
                    wait_for_activation: bool = False) -> bool:
    """Return true when the provider attempt should retry enrollment after its child exits."""
    try:
        while True:
            try:
                row = record(auth, refresh_only=refresh_only, defer_unconfigured=defer_unconfigured)
                return defer_unconfigured and row is None
            except secretstore.CredentialActivationBusy:
                if not wait_for_activation:
                    return True
                # Wait outside the SQLite transaction so the running worker can finish.
                with secretstore.credential_activation(publishing=True, wait=True):
                    pass
    except (OSError, secretstore.StoreMissing) as error:
        LOG.warning("Could not record a resolved account (%s)", type(error).__name__)
        return False
