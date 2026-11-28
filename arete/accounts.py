from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, paths, secretstore, util
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
    legacy_identity: str = field(default="", repr=False, compare=False)
    legacy_generation: str = field(default="", repr=False, compare=False)
    legacy_credential: str = field(default="", repr=False, compare=False)


cache: dict[str, tuple[float, Auth]] = {}


def fingerprint(*values) -> str:
    return secretstore.fingerprint(*values)


def legacy_fingerprint(*values) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def instance_key() -> bytes | None:
    if not paths.DB_FILE.exists() and not paths.LOCKS_DIR.is_relative_to(paths.STATE_DIR):
        return None
    if db.transaction_mode() == "read":
        try:
            with db.reader() as c:
                meta = db.metadata(c)
                if not (meta.get(secretstore.KEY_REF) or meta.get(secretstore.KEY_LOCAL)):
                    return None
        except db.NoDatabase:
            return None
    return secretstore.fingerprint_key()


def keyed(provider: str, generation: tuple, credential: tuple, *, mode: str = "api_key") -> Auth:
    key = instance_key()
    return Auth(mode, provider=provider,
                generation=secretstore.digest(key, *generation) if key else "",
                credential=secretstore.digest(key, "credential-v2", *credential) if key else "",
                legacy_generation=legacy_fingerprint(*generation),
                legacy_credential=legacy_fingerprint("credential-v1", *credential))


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


def identities(key: bytes | None, family: str, values: list) -> tuple[str, str]:
    supplied = [v for v in values if v is not None and v != ""]
    normalized = [stable(v, family) for v in supplied]
    if not normalized or not all(normalized) or len(set(normalized)) != 1:
        return "", ""
    return (secretstore.digest(key, "account-v2", family, normalized[0]) if key else "",
            legacy_fingerprint("account-v1", family, normalized[0]))


def identity(family: str, values: list) -> str:
    return identities(secretstore.fingerprint_key(), family, values)[0]


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
    request = util.http_request(PROFILE_URL, headers={"Authorization": f"Bearer {token}",
                                "anthropic-beta": "oauth-2025-04-20", "anthropic-version": "2023-06-01"},
                                schemes=("https",))
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
    key = instance_key()
    inputs = ("claude", source, status, account, token)
    generation = secretstore.digest(key, *inputs) if key else ""
    legacy_generation = legacy_fingerprint(*inputs)

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
        ident, legacy = identities(key, "claude", values)
        return Auth(mode, "claude", ident, generation, legacy_identity=legacy)

    found = cached(generation or legacy_generation, discover)
    credential = ("claude", source, token or [status, account])
    return Auth(found.mode, found.family, found.identity, generation,
                secretstore.digest(key, "credential-v2", *credential) if key else "", "anthropic",
                found.legacy_identity, legacy_generation, legacy_fingerprint("credential-v1", *credential))


def codex(data=None, source: str = "", provider: str = "") -> Auth:
    data = mapping(data)
    inputs = ("codex", source, data, provider)
    selected = data.get("auth_mode")
    if (selected in ("apikey", "api_key") or provider == "openai" or
            (not selected and data.get("OPENAI_API_KEY"))):
        return keyed("openai", inputs, ("codex", source, data.get("OPENAI_API_KEY") or data))
    key = instance_key()
    generation = secretstore.digest(key, *inputs) if key else ""
    legacy_generation = legacy_fingerprint(*inputs)
    if selected and selected not in ("chatgpt", "subscription"):
        return Auth(generation=generation, legacy_generation=legacy_generation)
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
    ident, legacy = ("", "") if malformed else identities(key, "codex", values)
    credential = ("codex", source, data)
    return Auth(mode, "codex", ident, generation,
                secretstore.digest(key, "credential-v2", *credential) if key else "",
                "openai-codex", legacy, legacy_generation, legacy_fingerprint("credential-v1", *credential))


def pi(provider: str, credential=None, source: str = "") -> Auth:
    credential = mapping(credential)
    if not credential:
        return Auth("api_key", provider=provider)
    inputs = ("pi", source, provider, credential)
    if credential.get("type") != "oauth" or provider not in PI_SUBSCRIPTIONS:
        return keyed(provider, inputs, inputs)
    key = instance_key()
    ident = legacy = ""
    family = "codex" if provider == "openai-codex" else ""
    if family == "codex":
        token = credential.get("access")
        claims = jwt(token) if token else {}
        values = [credential.get("accountId"), *chatgpt_account_ids(claims)]
        if not token or claims:
            ident, legacy = identities(key, family, values)
    return Auth("subscription", family, ident,
                secretstore.digest(key, *inputs) if key else "",
                secretstore.digest(key, "credential-v2", *inputs) if key else "", provider,
                legacy, legacy_fingerprint(*inputs), legacy_fingerprint("credential-v1", *inputs))


def subscription_scope(version: str, family: str, account: str, limit: str) -> str:
    return f"subscription-{version}:" + json.dumps([family, account, limit], separators=(",", ":")) if account else ""


def scope(auth: Auth, limit: str, unknown: str = "") -> str:
    if auth.mode == "api_key":
        current = "credit-v2:" + fingerprint(auth.provider, auth.credential or unknown) if auth.credential or unknown else ""
    else:
        account = auth.identity or auth.credential or unknown
        current = subscription_scope("v2", auth.family, account, limit)
    if current and (auth.legacy_identity or auth.legacy_credential):
        if legacy := legacy_scope(auth, limit):
            devices.remember_legacy_scope(current, legacy)
    return current


def legacy_scope(auth: Auth, limit: str, unknown: str = "") -> str:
    if auth.mode == "api_key":
        account = auth.legacy_credential or unknown
        return "credit-v1:" + legacy_fingerprint(auth.provider, account) if account else ""
    return subscription_scope("v1", auth.family, auth.legacy_identity or auth.legacy_credential or unknown, limit)


def record(auth: Auth, *, secret_id: int | None = None, refresh_only: bool = False,
           defer_unconfigured: bool = False, usage: str = "personal") -> dict | None:
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
        return credentials.save_account(c, label, auth.provider, auth.mode, holder, usage, identity, digest,
                                        secret_id)


def record_workspace(row, provider, mode):
    if row is None:
        return
    try:
        return record(Auth(mode, provider=provider, credential=row["fingerprint"]),
                      secret_id=row["id"], usage="workspace")
    except (OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema, secretstore.StoreMissing) as error:
        LOG.warning("Could not record a workspace account (%s)", type(error).__name__)


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
                if db.in_write():
                    raise
                with secretstore.credential_activation(publishing=True, wait=True):
                    pass
    except (OSError, secretstore.StoreMissing) as error:
        if wait_for_activation:
            raise
        LOG.warning("Could not record a resolved account (%s)", type(error).__name__)
        return False
