from __future__ import annotations

import configparser
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    accounts,
    child,
    claude,
    devices,
    environment,
    issue_goal,
    owner_asks,
    paths,
    pi_shell,
    sandbox,
    scratch,
    secret_move,
    secretstore,
    util,
    vault,
)
from .agent import (
    NO_SESSION,
    READ_ONLY_TOOLS,
    READ_TOOLS,
    Result,
    Session,
    UsageLimit,
    credit_limit,
    json_from_text,
    overload_limit,
    provider_failure_fields,
    provider_reset,
    unreachable_limit,
    usage_totals,
)
from .store import db
from .store import devices as devices_store

if TYPE_CHECKING:
    from . import guard

TOOLS_OF = {"Read": ("read",), "Glob": ("find", "ls"), "Grep": ("grep",), "LS": ("ls",), "Edit": ("edit",),
            "MultiEdit": ("edit",), "Write": ("write",), "Bash": (pi_shell.TOOL,)}
WRITING = {"edit", "write", pi_shell.TOOL}
PI_HOME = paths.HOME / ".pi"
GUARD_FILE = paths.PACKAGE_DIR / "pi_guard.mjs"
ASK_FILE = paths.PACKAGE_DIR / "pi_ask.mjs"
SHELL_FILE = paths.PACKAGE_DIR / "pi_shell.mjs"
GUARDED_TOOLS = frozenset({"read", "edit", "write", "grep", "find", "ls", pi_shell.TOOL})
SCHEMA_RULE = ("No tool takes this answer, so write it as the text of your last reply. Reply with only one JSON value "
               "that matches this JSON schema, in a ```json fence, and nothing after it:\n\n{schema}")
PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
ANTHROPIC_LOGIN_REASON = "a Claude plan login runs only on the claude runner"
ANTHROPIC_KEY_REQUIRED = (f"pi anthropic: {ANTHROPIC_LOGIN_REASON}; "
                          f"set an Anthropic Console key at {secretstore.address('global', 'ANTHROPIC_API_KEY')} "
                          "or use claude:<model>")
REMOTE_AUTH_UNVERIFIED = ("pi cannot validate or isolate credentials on a remote device; "
                          "run pi locally or use the claude runner")
KEY_NAMES = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "azure": "AZURE_OPENAI_API_KEY",
             "amazon-bedrock": "AWS_BEARER_TOKEN_BEDROCK",
             "google": "GEMINI_API_KEY", "google-vertex": "GOOGLE_CLOUD_API_KEY", "huggingface": "HF_TOKEN",
             "vercel-ai-gateway": "AI_GATEWAY_API_KEY", "moonshotai": "MOONSHOT_API_KEY",
             "moonshotai-cn": "MOONSHOT_API_KEY", "kimi-coding": "KIMI_API_KEY", "opencode-go": "OPENCODE_API_KEY",
             **{p: p.upper().replace("-", "_") + "_API_KEY" for p in
                ("nvidia", "deepseek", "groq", "cerebras", "xai", "typesafe", "radius", "openrouter", "zai",
                 "mistral", "minimax", "minimax-cn", "fireworks", "together", "baseten", "opencode", "meta",
                 "ant-ling", "qwen-token-plan", "qwen-token-plan-cn", "zai-coding-cn", "xiaomi",
                 "xiaomi-token-plan-cn", "xiaomi-token-plan-ams", "xiaomi-token-plan-sgp")},
             "cloudflare-workers-ai": "CLOUDFLARE_API_KEY", "cloudflare-ai-gateway": "CLOUDFLARE_API_KEY"}
KEY_NAMES.update({"qwen-token-plan-individual": "QWEN_TOKEN_PLAN_API_KEY",
                  "qwen-token-plan-individual-cn": "QWEN_TOKEN_PLAN_CN_API_KEY", "github-copilot": "COPILOT_GITHUB_TOKEN"})
TRANSPORT_NAMES = {"amazon-bedrock": ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_EC2_METADATA_DISABLED",
                                     "AWS_EC2_METADATA_V1_DISABLED", "AWS_EC2_METADATA_SERVICE_ENDPOINT",
                                     "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
                                     "AWS_ENDPOINT_URL_STS", "AWS_ENDPOINT_URL", "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS",
                                     "AWS_USE_FIPS_ENDPOINT", "AWS_USE_DUALSTACK_ENDPOINT", "AWS_AUTH_SCHEME_PREFERENCE",
                                     "AWS_MAX_ATTEMPTS", "AWS_RETRY_MODE", "AWS_DEFAULTS_MODE", "AWS_BEDROCK_FORCE_HTTP1"),
                   "azure": ("AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_RESOURCE_NAME", "AZURE_OPENAI_API_VERSION",
                             "AZURE_OPENAI_DEPLOYMENT_NAME_MAP"),
                   "google-vertex": ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"),
                   "cloudflare-workers-ai": ("CLOUDFLARE_ACCOUNT_ID",)}
MANAGED_NAMES = ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
                 "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                 "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
                 "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_SDK_LOAD_CONFIG",
                 "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_IDENTITY_TOKEN_FILE",
                 "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_WORKSPACE_ID",
                 "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_GATEWAY_ID")
CREDENTIAL_FILES = ("AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
                    "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
                    "ANTHROPIC_IDENTITY_TOKEN_FILE")
SERVICE_KEYS = {"JIRA_API_TOKEN", "GITHUB_APP_PRIVATE_KEY", "VAPID_PRIVATE_KEY", "FINGERPRINT_KEY"}
REFERENCES = re.compile(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")
SAFE_HEADERS = {"accept": r"application/json|text/event-stream", "content-type": r"application/json(?:; ?charset=utf-8)?",
                "anthropic-version": r"\d{4}-\d{2}-\d{2}",
                "anthropic-beta": r"[a-z][a-z0-9-]*-\d{4}-\d{2}-\d{2}(?:, ?[a-z][a-z0-9-]*-\d{4}-\d{2}-\d{2})*"}


def owner_dir() -> Path:
    if paths.ISOLATED:
        return (paths.SCRATCH_STATE and paths.confined(paths.STATE_DIR / "pi-agent")) or paths.NOWHERE / "pi-agent"
    return paths.expand(os.environ.get("PI_CODING_AGENT_DIR") or PI_HOME / "agent").absolute()


def remote_refusal(remote: devices.Device | str | None) -> str:
    return REMOTE_AUTH_UNVERIFIED if remote and devices.resolve(remote).remote else ""


@dataclass(frozen=True)
class ManagedCredential:
    kind: str
    env: dict[str, str] = field(repr=False)
    files: tuple[Path, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    auth: accounts.Auth = field(repr=False)
    error: str = ""
    token_error: str = ""
    login_available: bool = False
    login_refused: str = ""
    login_held: bool = False
    owner: Path = field(default_factory=lambda: owner_dir() / "auth.json", repr=False)
    key: str = field(default="", repr=False)
    key_name: str = ""
    models: dict = field(default_factory=dict, repr=False, compare=False)
    settings: dict = field(default_factory=dict, repr=False, compare=False)
    env: dict = field(default_factory=dict, repr=False, compare=False)
    hidden: tuple[str, ...] = field(default=(), repr=False, compare=False)
    view: Path | None = field(default=None, repr=False, compare=False)
    refreshed: set[str] = field(default_factory=set, repr=False, compare=False)
    refreshed_secrets: set[str] = field(default_factory=set, repr=False, compare=False)
    refreshed_auths: set[accounts.Auth] = field(default_factory=set, repr=False, compare=False)
    managed: ManagedCredential | None = field(default=None, repr=False)
    using_login: bool = False
    sensitive_files: tuple[Path, ...] = field(default=(), repr=False)
    local: bool = False
    key_supported: bool = False
    key_identity: str = field(default="", repr=False)

    @property
    def key_available(self) -> bool:
        return bool(self.key or self.key_identity or self.managed)


def managed_credential(provider: str, available: dict, *, opaque=()) -> ManagedCredential | None:
    if provider == "cloudflare-ai-gateway":
        names = ("CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_GATEWAY_ID")
        if all(available.get(name) for name in names):
            return ManagedCredential("gateway_header", {name: available[name] for name in names})
    if provider == "amazon-bedrock":
        if available.get("AWS_PROFILE"):
            kind, names = "profile", ["AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_SDK_LOAD_CONFIG"]
        elif available.get("AWS_ACCESS_KEY_ID") and available.get("AWS_SECRET_ACCESS_KEY"):
            kind, names = "iam", ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
        elif available.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") or available.get("AWS_CONTAINER_CREDENTIALS_FULL_URI"):
            kind = "container"
            names = ["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" if available.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
                     else "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                     "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE" if available.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
                     else "AWS_CONTAINER_AUTHORIZATION_TOKEN"]
        elif available.get("AWS_WEB_IDENTITY_TOKEN_FILE") and available.get("AWS_ROLE_ARN"):
            kind, names = "web_identity", ["AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME"]
        else:
            return None
        env = {name: available[name] for name in [*names, *TRANSPORT_NAMES[provider]] if available.get(name)}
        defaults = [paths.HOME / ".aws" / name for name in ("credentials", "config")] if kind == "profile" else []
        files = list(defaults)
        for name in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
                     "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
            if env.get(name):
                path = paths.expand(env[name]).absolute()
                env[name] = str(path)
                files.append(path)
        return ManagedCredential(kind, env, tuple(files))
    if provider == "google-vertex":
        project = available.get("GOOGLE_CLOUD_PROJECT") or available.get("GCLOUD_PROJECT")
        location = available.get("GOOGLE_CLOUD_LOCATION")
        path = paths.expand(available.get("GOOGLE_APPLICATION_CREDENTIALS") or
                            paths.HOME / ".config" / "gcloud" / "application_default_credentials.json").absolute()
        if project and location and ("GOOGLE_APPLICATION_CREDENTIALS" in opaque or path.is_file()):
            return ManagedCredential("adc", {"GOOGLE_CLOUD_PROJECT": project, "GOOGLE_CLOUD_LOCATION": location,
                                             "GOOGLE_APPLICATION_CREDENTIALS": str(path)}, (path,))
    return None


def managed_material(credential: ManagedCredential, provider: str) -> list:
    env = {n: v for n, v in credential.env.items() if n not in TRANSPORT_NAMES.get(provider, ())}
    files = credential.files
    if credential.kind == "profile":
        files = tuple(paths.expand(env.get(name) or paths.HOME / ".aws" / filename).absolute()
                      for name, filename in (("AWS_SHARED_CREDENTIALS_FILE", "credentials"), ("AWS_CONFIG_FILE", "config")))
    values = []
    for path in files:
        try:
            data = paths.read_confined(path, accounts.MAX_LOGIN_METADATA)
            if credential.kind == "profile":
                parser = configparser.RawConfigParser()
                parser.read_string(data.decode())
                profile = env.get("AWS_PROFILE", "default")
                selected, visited = {}, set()
                while profile and profile not in visited:
                    visited.add(profile)
                    section = profile if parser.has_section(profile) else "profile " + profile
                    fields = dict(parser.items(section)) if parser.has_section(section) else {}
                    selected[profile] = {k: v for k, v in fields.items()
                                         if k.startswith(("aws_", "sso_")) or k in
                                         ("role_arn", "source_profile", "credential_source", "credential_process",
                                          "web_identity_token_file", "external_id", "role_session_name")}
                    session = fields.get("sso_session", "")
                    if parser.has_section("sso-session " + session):
                        selected["sso-session " + session] = dict(parser.items("sso-session " + session))
                    profile = fields.get("source_profile", "")
                data = selected
            values.append((str(path), accounts.legacy_fingerprint(data)))
        except (OSError, ValueError, configparser.Error):
            values.append((str(path), ""))
    return [credential.kind, env, values]


def credential_values(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, dict):
        return [s for v in value.values() for s in credential_values(v)]
    if isinstance(value, list):
        return [s for v in value for s in credential_values(v)]
    return []


def plan_token_error(values, source: str = "") -> str:
    for name, value in values:
        if any("sk-ant-oat" in v for v in credential_values(value)):
            return f"pi: refused Claude plan token in {source}{name}; {ANTHROPIC_LOGIN_REASON}"
    return ""


def route_token_error(route: Route) -> str:
    values = [*route.env.items(), (route.key_name or "selected key", route.key)]
    if route.managed:
        values.extend(route.managed.env.items())
    if route.using_login:
        values.append((f"{route.owner} provider {route.provider}", accounts.read(route.owner).get(route.provider)))
    return plan_token_error(values)


def credential_config(value) -> list[str]:
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in
                (credential_values(v) if k in ("apiKey", "headers", "env") else credential_config(v))]
    return [s for v in value for s in credential_config(v)] if isinstance(value, list) else []


def key_addresses(values):
    return ((secretstore.address("global", n) if secretstore.NAME.fullmatch(n) else n, v) for n, v in values.items())


def key_references(value):
    return {n for pair in REFERENCES.findall(value) for n in pair if n} if isinstance(value, str) else set()


def configured_key(value, available: dict) -> str:
    if not isinstance(value, str) or value.startswith("!"):
        return ""
    missing = False

    def part(match):
        nonlocal missing
        if match[0] in ("$$", "$!"):
            return match[0][1:]
        name = match[1] if match[1] is not None else match[2]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return match[0]
        if not available.get(name):
            missing = True
        return available.get(name, "")

    resolved = re.sub(r"\$\$|\$!|\$\{([^}]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", part, value)
    return "" if missing else resolved


def sanitized(value):
    if isinstance(value, list):
        return [sanitized(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: sanitized(v) for k, v in value.items() if k not in ("apiKey", "authHeader", "env", "headers")}
    headers = value.get("headers")
    if isinstance(headers, dict):
        safe = {k: v for k, v in headers.items() if k.lower() in SAFE_HEADERS and isinstance(v, str)
                and re.fullmatch(SAFE_HEADERS[k.lower()], v)}
        if safe:
            out["headers"] = safe
    return out


def model_configuration(folder: Path | None = None) -> dict:
    try:
        text = paths.read_confined((folder or owner_dir()) / "models.json").decode("utf-8-sig")
        strings = r'"(?:\\.|[^"\\])*"'
        text = re.sub(strings + r'|//[^\n]*', lambda m: m[0] if m[0].startswith('"') else "", text)
        text = re.sub(strings + r'|,(\s*[}\]])', lambda m: m[1] or m[0], text)
        return accounts.mapping(json.loads(text))
    except (OSError, ValueError):
        return {}


def child_env(provider: str, available: dict, passing: list[str], providers: dict[str, tuple[str, ...]],
              raw_credentials: list[str], chosen_login: bool, managed: ManagedCredential | None, key: str,
              key_name: str) -> tuple[dict[str, str], set[str]]:
    refs = {n for v in raw_credentials for pair in REFERENCES.findall(v) for n in pair if n}
    aliases = {n for values in providers.values() for n in values}
    transport = TRANSPORT_NAMES.get(provider, ())
    never = {n for n in available if re.search(r"TOKEN|SECRET|PASSW|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL", n, re.IGNORECASE)
             or n in refs or n in KEY_NAMES.values() or (n in MANAGED_NAMES and n not in transport)
             or (environment.passes(n, aliases) and n not in transport)}
    names = [*passing, re.sub(r"\W", "_", provider).upper() + "_*", *transport]
    env = environment.build(names, {"ARETE_AGENT": "1"}, never=(*PARENT_SESSION_VARS, *never), source=available)
    for n in ("PI_CODING_AGENT_DIR", "ARETE_PI_GUARD_PATHS", "ARETE_PI_REFRESH_FD", "ARETE_PI_REFRESH_ACK_FD",
              "ARETE_PI_REFRESH_PROVIDER",
              "ARETE_PI_SHELL_FD", "ARETE_PI_OBSERVE", "NODE_OPTIONS", "NODE_PATH"):
        env.pop(n, None)
    if not chosen_login and managed:
        env.update(managed.env)
    elif not chosen_login and key:
        env[key_name] = key
        env["ARETE_PI_SELECTED_KEY"] = key
    return env, never


def views(models: dict, settings: dict, provider: str, chosen_login: bool, managed: ManagedCredential | None,
          key: str, local: bool) -> tuple[dict, dict]:
    clean_models = sanitized(models)
    if isinstance(clean_models.get("providers"), dict):
        clean_models["providers"] = {p: v for p, v in clean_models["providers"].items() if v}
    if not chosen_login and provider and not managed and (key or local):
        view = clean_models.setdefault("providers", {}).setdefault(provider, {})
        view["apiKey"] = "$ARETE_PI_SELECTED_KEY" if key else "arete-local"
    clean_settings = sanitized(settings)
    for n in ("extensions", "packages", "skills", "promptTemplates", "themes", "mcpServers"):
        clean_settings.pop(n, None)
    return clean_models, clean_settings


def withheld(store: dict, raw_credentials: list[str], keys: dict[str, str], available: dict, never: set[str],
             key: str, managed: ManagedCredential | None) -> tuple[tuple[str, ...], tuple[Path, ...]]:
    hidden = [*credential_values(store), *raw_credentials, *keys.values(),
              *(available[n] for n in never if available.get(n)), key,
              *(managed.env.values() if managed else ())]
    files = {paths.expand(available[name]).absolute() for name in CREDENTIAL_FILES if available.get(name)}
    if managed:
        files.update(managed.files)
    return tuple(v for v in hidden if len(v) >= vault.SHORTEST), tuple(sorted(files))


def key_setting(provider):
    name = KEY_NAMES.get(provider)
    return secretstore.address("global", name) if name else "the provider's global secret address"


def missing_key(provider, owner):
    return (f"pi {provider}: no usable login or supported key; checked {owner}, {key_setting(provider)} "
            "and Arete environment")


def select(model: str, passing=None, providers=None, *, held=lambda auth, model: None, mode: str = "",
           source=None, local_providers=(), owner_auth: Path | None = None, metadata_only=False) -> Route:
    if passing is None or providers is None:
        from . import pins

        cfg = pins.active_config()
        passing = environment.passed(cfg) if passing is None else passing
        providers = cfg.environment.providers if providers is None else providers
        local_providers = cfg.local_providers
    owner = Path(owner_auth).absolute() if owner_auth is not None else owner_dir() / "auth.json"
    if paths.ISOLATED:
        owner, source = paths.confined(owner) or paths.NOWHERE / "auth.json", {}
    settings = accounts.read(owner.parent / "settings.json")
    models = model_configuration(owner.parent)
    provider, sep, name = model.partition("/")
    if not model:
        provider, name = settings.get("defaultProvider", ""), settings.get("defaultModel", "")
    elif not sep:
        provider, name = settings.get("defaultProvider", ""), model
    provider = provider if isinstance(provider, str) else ""
    name = name if isinstance(name, str) else ""
    resolved = f"{provider}/{name or '*'}" if provider else model
    store = accounts.read(owner)
    credential = accounts.mapping(store.get(provider))
    login = (credential.get("type") == "oauth" and isinstance(credential.get("access"), str)
             and bool(credential.get("access")) and isinstance(credential.get("refresh"), str)
             and isinstance(credential.get("expires"), (int, float))
             and not isinstance(credential["expires"], bool) and math.isfinite(credential["expires"]))
    login_refused = ANTHROPIC_LOGIN_REASON if provider == "anthropic" and credential.get("type") == "oauth" else ""
    login = login and provider != "anthropic"
    source = os.environ if source is None else source
    legacy = secret_move.pi_env()
    configured = accounts.mapping(accounts.mapping(models.get("providers")).get(provider))
    supported = (provider in KEY_NAMES or bool(configured.get("api")) or
                 any(accounts.mapping(m).get("api") for m in configured.get("models", []) if isinstance(m, dict)))
    names = [KEY_NAMES[provider]] if provider in KEY_NAMES else []
    raw = configured.get("apiKey", "")
    bound_key = bool(configured.get("baseUrl") and "apiKey" in configured)
    login = login and not bound_key
    auth = accounts.pi(provider, credential, str(owner)) if login else accounts.Auth("api_key")
    if supported and provider != "openai-codex" and not bound_key:
        names += list(providers.get(provider, ()))
    else:
        names = []
    context = {*MANAGED_NAMES, *TRANSPORT_NAMES.get(provider, ())}
    exact = [n for n in names if isinstance(n, str) and re.fullmatch(r"[A-Za-z_]\w*", n) and n not in context]
    wanted = {*exact, *context, *key_references(raw)} - SERVICE_KEYS
    fingerprints = {}
    exposed_pi = environment.build((), role="runner:pi")
    read_keys = secretstore.env_status if metadata_only else secretstore.env
    keys = read_keys(wanted, source=source, fingerprints=fingerprints, legacy=legacy)
    available = {**exposed_pi, **source, **keys}
    resolved_key = configured_key(raw, available)
    token_error = (plan_token_error(source.items()) or plan_token_error(key_addresses(legacy)) or
                   plan_token_error(key_addresses(keys)) or
                   plan_token_error([(f"{owner.parent / 'models.json'} provider {provider} apiKey", resolved_key)]))
    if login:
        token_error = token_error or plan_token_error([(f"{owner} provider {provider}", credential)])
    key_name = next((n for n in exact if source.get(n)), "") or next((n for n in exact if fingerprints.get(n)), "")
    key_name = key_name or next((n for n in exact if available.get(n)), "")
    key = available.get(key_name, "")
    if supported and provider != "openai-codex" and raw and not key:
        key = resolved_key
        key_name = "ARETE_PI_SELECTED_KEY" if key else ""
    managed = (managed_credential(provider, available, opaque=fingerprints if metadata_only else ())
               if not bound_key and provider != "anthropic" and (not key or provider == "cloudflare-ai-gateway")
               else None)
    if provider == "cloudflare-ai-gateway":
        key, key_name = "", ""
    if provider == "cloudflare-workers-ai" and not available.get("CLOUDFLARE_ACCOUNT_ID"):
        key, key_name = "", ""
    has_key = bool(key or managed)
    local = provider == "ollama" or (provider in local_providers and bool(configured))
    local |= bool(configured and re.match(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::|/|$)",
                                         str(configured.get("baseUrl", ""))))
    local = local and provider not in ("openai-codex", "anthropic")
    login_held = bool(login and not token_error and held(auth, resolved))
    chosen_login = (login and not token_error and mode != "api_key" and
                    (mode in ("login", "subscription") or auth.mode != "subscription" or not login_held or not has_key))
    error = token_error
    if not error and not provider:
        error = f"pi needs provider/model or defaultProvider in {owner.parent / 'settings.json'} before choosing credentials"
    elif not error and bound_key and not key:
        error = f"pi {provider}: no usable apiKey for the custom endpoint in {owner.parent / 'models.json'}"
    elif not error and provider == "anthropic" and (mode in ("login", "subscription") or not key):
        error = ANTHROPIC_KEY_REQUIRED
    elif not error and provider != "anthropic" and (
            (mode in ("login", "subscription") and not login) or (not chosen_login and not (has_key or local))):
        error = missing_key(provider, owner)
    identity_env = {**available, **fingerprints}
    key_identity = (configured_key(raw, identity_env) if key_name == "ARETE_PI_SELECTED_KEY" else
                    identity_env.get(key_name, "")) if key else ""
    chosen_auth = auth if chosen_login else accounts.Auth(
        "subscription" if mode in ("login", "subscription") and provider in accounts.PI_SUBSCRIPTIONS else "api_key",
        provider=provider)
    if not chosen_login and has_key:
        source_kind = ("managed" if managed else "configured" if key_name == "ARETE_PI_SELECTED_KEY" else
                       "registry" if key_name in fingerprints else "keys" if key_name in legacy and
                       not source.get(key_name) else "environment")
        credential_context = {n: identity_env[n] for n in TRANSPORT_NAMES.get(provider, ()) if identity_env.get(n)
                              and re.search(r"REGION|PROJECT|ACCOUNT|RESOURCE|GATEWAY|BASE_URL|ENDPOINT", n)}
        credential_context["baseUrl"] = configured.get("baseUrl", "")
        material = (managed_material(replace(managed, env={n: fingerprints.get(n, v) for n, v in managed.env.items()}),
                                     provider) if managed else [key_name, key_identity])
        chosen_auth = accounts.keyed(provider, ("pi-managed" if managed else "pi-key", material),
                                     ("pi", provider, source_kind,
                                      str(owner) if source_kind == "configured" else
                                      str(paths.PI_KEYS_FILE) if source_kind == "keys" else "",
                                      material, credential_context),
                                     mode=chosen_auth.mode)
    raw_credentials = credential_config(models)
    env, never = child_env(provider, available, passing, providers, raw_credentials, chosen_login, managed, key,
                           key_name)
    clean_models, clean_settings = views(models, settings, provider, chosen_login, managed, key, local)
    hidden, sensitive_files = withheld(store, raw_credentials, {**legacy, **keys}, available, never, key, managed)
    if metadata_only:
        env, key = {}, ""
        if managed:
            managed = replace(managed, env={})
    return Route(provider, resolved, chosen_auth, error, token_error=token_error,
                 login_available=login, login_refused=login_refused,
                 login_held=login_held,
                 owner=owner, key=key, key_name=key_name, models=clean_models, settings=clean_settings, env=env,
                 hidden=hidden, managed=managed if not chosen_login else None,
                 using_login=chosen_login, sensitive_files=sensitive_files, local=local,
                 key_supported=supported and provider != "openai-codex", key_identity=key_identity)


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("pi route directory must be a private directory")


def prepare(route: Route, *, delegation_only: bool = False, structured: bool = False) -> Path:
    root = sandbox.LOGIN_DIR if route.using_login else sandbox.KEY_DIR
    private_dir(root)
    auth = root / "auth.json"
    if auth.is_symlink() or (auth.exists() and (not auth.is_file() or auth.stat().st_nlink != 1)):
        raise ValueError("pi route auth store must not be an unexpected link")
    folder = Path(tempfile.mkdtemp(prefix="call-", dir=root))
    if route.using_login:
        (folder / "auth.json").symlink_to(route.owner)
    else:
        with (folder / "auth.json").open("x") as out:
            out.write("{}")
        (folder / "auth.json").chmod(0o600)
    settings = route.settings
    if delegation_only or structured:
        settings = {**settings, "retry": {"enabled": False, "provider": {"maxRetries": 0}},
                    "compaction": {"enabled": False}, "cacheWarming": "off", "transport": "sse"}
    for name, value in (("models.json", route.models), ("settings.json", settings)):
        with (folder / name).open("x") as out:
            json.dump(value, out)
    return folder


def cleanup(folder: Path) -> None:
    for name in ("auth.json", "models.json", "settings.json", "models-store.json"):
        (folder / name).unlink(missing_ok=True)
    with suppress(OSError):
        folder.rmdir()


_VERSION: str | None = None


def binary() -> str:
    installed = sorted((paths.HOME / ".nvm" / "versions" / "node").glob("v*/bin/pi"), key=lambda p: p.parent.parent.name)
    return shutil.which("pi") or (str(installed[-1]) if installed else "pi")


def version() -> str:
    global _VERSION
    if _VERSION is not None:
        return _VERSION
    try:
        found = util.sh([binary(), "--version"], timeout=10)
    except OSError:
        _VERSION = ""
        return _VERSION
    if found.returncode != 0:
        _VERSION = ""
        return _VERSION
    words = found.stdout.strip().split()
    _VERSION = next((w for w in words if w and w[0].isdigit()), words[-1] if words else "")
    return _VERSION


def is_pattern_rule(rule: str) -> bool:
    return "(" in rule


def tool_names(tools: list[str] | None, allowed_tools: list[str] | None, disallowed_tools: list[str] | None) -> list[str]:
    granted = tools if tools is not None else [*(allowed_tools or []), *READ_TOOLS]
    allowed = set(allowed_tools or []) | set(READ_TOOLS)
    denied = {t.split("(", 1)[0] for t in disallowed_tools or []
              if not t.startswith("Bash(")}
    names: list[str] = []
    for tool in granted:
        if tool in denied or is_pattern_rule(tool):
            continue
        if tool not in allowed and tool not in READ_ONLY_TOOLS:
            continue
        names += [n for n in TOOLS_OF.get(tool, ()) if n not in names]
    if "Bash" not in denied and (tools is None or "Bash" in tools) and any(
            t.startswith("Bash(") for t in allowed_tools or []) and pi_shell.TOOL not in names:
        names.append(pi_shell.TOOL)
    return names


def default_provider() -> str:
    try:
        settings = json.loads(paths.read_confined(owner_dir() / "settings.json"))
    except (OSError, ValueError):
        settings = {}
    provider = settings.get("defaultProvider") if isinstance(settings, dict) else ""
    return provider if isinstance(provider, str) else ""


def agent_env(model: str, passing: list[str] | None = None, providers: dict[str, tuple[str, ...]] | None = None,
              source=None, selected: Route | None = None) -> dict[str, str]:
    return dict((selected or select(model, passing, providers, source=source)).env)


def launch_env_names(route: Route) -> list[str]:
    controls = ({"ARETE_PI_REFRESH_FD", "ARETE_PI_REFRESH_ACK_FD", "ARETE_PI_REFRESH_PROVIDER"}
                if route.using_login else set())
    return sorted({*route.env, "PI_CODING_AGENT_DIR", "ARETE_PI_GUARD_PATHS", *controls})


def guard_env(floor: guard.Floor, auth_paths: list[Path] | None = None,
              secret_paths: tuple[Path, ...] = (), *, delegation_only: bool = False,
              shell: bool = False, protected: tuple[Path, ...] | None = None) -> dict[str, str]:
    protected = sandbox.authority_roots() if protected is None else protected
    sources = {PI_HOME / "agent", owner_dir(), *(path.parent for path in auth_paths or [])}
    roots = [*floor.closed, *protected, PI_HOME, *sorted(sources),
             sandbox.AGENT_DIR, sandbox.LOGIN_DIR, sandbox.KEY_DIR,
             Path("/proc"), Path("/dev")]
    auth = [PI_HOME / "agent" / "auth.json", owner_dir() / "auth.json", sandbox.AGENT_DIR / "auth.json",
            *(auth_paths or [])]
    files = [*floor.files, *protected, paths.PI_KEYS_FILE, paths.CONFIG_FILE,
             *(source / name for source in sorted(sources) for name in ("models.json", "settings.json"))]
    for path in auth:
        files.extend((path, path.resolve(), Path(str(path) + ".lock"), Path(str(path.resolve()) + ".lock")))
    files.extend(path for source in secret_paths for path in (source, source.resolve()))
    inodes = []
    if not delegation_only:
        for path in files:
            try:
                stat = path.stat()
                inodes.append(f"{stat.st_dev}:{stat.st_ino}")
            except OSError:
                pass
    return {"ARETE_PI_GUARD_PATHS": json.dumps({"delegation_only": delegation_only,
                                              "shell_tool": pi_shell.TOOL if shell else "", "shell_file": str(SHELL_FILE),
                                              "inodes": inodes,
                                              "secret_names": sandbox.SECRET_NAMES, "safe_names": sandbox.SAFE_NAMES,
                                              "secret_dirs": sorted(sandbox.SECRET_DIRS),
                                              "roots": list(map(str, roots)), "files": list(map(str, files)),
                                              "open": [str(p) for p in floor.open if not any(
                                                  p.resolve().is_relative_to(root.resolve()) for root in protected)],
                                              "write_roots": [str(paths.PACKAGE_DIR)],
                                              "write_files": [str(GUARD_FILE), str(ASK_FILE), str(SHELL_FILE)]})}


def confinement(cwd: Path, add_dirs: list[Path], floor: guard.Floor, selected: Route | None,
                goal_paths: tuple[Path, ...] = ()) -> list[str]:
    return sandbox.write_confinement(
        cwd, add_dirs, selected.owner if selected and selected.using_login else None,
        state=(paths.STATE_DIR, (*floor.open, *goal_paths, sandbox.SESSIONS_DIR, sandbox.AGENT_DIR, sandbox.LOGIN_DIR,
                                 sandbox.KEY_DIR, paths.PACKAGE_DIR)),
        hidden=(paths.CONFIG_DIR, paths.LOG_DIR, paths.CLIENT_DIR), peers=paths.WORKTREES_DIR)


def command(names: list[str], *, model: str = "", effort: str = "", resume: Session = NO_SESSION, system: str = "",
            persist: bool = True, add_dirs: list[Path] | None = None, ask_owner: bool = False,
            observe: bool = False) -> list[str]:
    cmd = [binary(), "-p", "--mode", "json", "--no-context-files", "--no-skills", "--no-extensions",
           "--no-prompt-templates", "--no-themes", "--no-approve", "--no-mcp", "--extension", str(GUARD_FILE)]
    if ask_owner:
        cmd += ["--extension", str(ASK_FILE)]
    if observe or pi_shell.TOOL in names:
        cmd += ["--extension", str(SHELL_FILE)]
    names = [*names, owner_asks.TOOL] if ask_owner else names
    cmd += ["--tools", ",".join(names)] if names else ["--no-tools"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--thinking", effort]
    if persist or resume.id:
        cmd += ["--session-dir", str(sandbox.SESSIONS_DIR)]
        if resume.id:
            cmd += ["--session", resume.id]
    else:
        cmd += ["--no-session"]
    if system:
        cmd += ["--append-system-prompt", system]
    if add_dirs:
        cmd += ["--append-system-prompt", "You may also read and change files under: " + ", ".join(map(str, add_dirs))]
    return cmd


def authentication(model: str, passing=None, providers=None) -> accounts.Auth:
    return select(model, passing, providers).auth


def usage_limit(auth: accounts.Auth, provider: str, text: str | dict | list, model: str = "", *,
                failure_text: str | list[str | int | None] | None = None) -> UsageLimit | None:
    data = text
    if failure_text is None:
        failure_text = text if isinstance(text, str) else ""
    if auth.mode != "subscription":
        return credit_limit(data) or overload_limit(data, model) or unreachable_limit(failure_text, model)
    text = data if isinstance(data, str) else json.dumps(data, default=str)
    native_limit = (claude.usage_limit(data, "\n".join([text, *error_messages(data)]), model)
                    if auth.family == "claude" else None)
    if native_limit and native_limit.kind == "overloaded":
        return native_limit
    if (not native_limit and not re.search(r"usage[_ ]limit(?:[_ ]reached)?|subscription.{0,30}limit|"
                                          r"you.ve (?:hit|reached).{0,30}limit|weekly limit|five.hour limit",
                                          text, re.IGNORECASE) and not claude.USAGE_LIMIT_TEXT.search(text)):
        return overload_limit(text, model) or unreachable_limit(failure_text, model)
    from . import codex

    legacy = claude.LEGACY_LIMIT_RESET.search(text)
    until = provider_reset(data) or (provider_reset({"reset": legacy[1]}) if legacy else None)
    if until is None:
        for message in error_messages(data):
            try:
                iso = re.search(r'''(?:resets?[_ ]?at|reset|try again at)["'\s:=]+'''
                                r'(\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2}))', message, re.IGNORECASE)
                until = (util.parse_iso(iso[1]) if iso else
                         codex.reset_time(re.sub(r"\bresets?\s+(at\s+|in\s+)?", lambda m: "try again " +
                                                (m[1] or "at "), message, flags=re.IGNORECASE)))
            except (ValueError, OverflowError, OSError):
                continue
            if until is not None:
                break
    now = util.now()
    if until is None or until > now + timedelta(days=366):
        until = now + timedelta(seconds=claude.LIMIT_RECHECK_SECONDS)
    else:
        until = max(now, until) + timedelta(seconds=claude.LIMIT_SLACK_SECONDS)
    scope = claude.EVERY_MODEL if auth.family == "claude" else codex.LIMIT_SCOPE if auth.family == "codex" else provider
    if auth.family == "claude":
        kind = next((name for name in claude.LIMIT_SCOPES if name in text), "")
        scope = native_limit.scope if native_limit else claude.LIMIT_SCOPES.get(kind, scope)
    return UsageLimit(util.iso(until.astimezone(UTC)), scope, "subscription")


def login_unchanged(route: Route) -> bool:
    credential = accounts.mapping(accounts.read(route.owner).get(route.provider))
    if credential.get("type") != "oauth":
        return False
    latest = accounts.pi(route.provider, credential, str(route.owner))
    return (latest.generation in {route.auth.generation, *route.refreshed}
            or devices_store.refresh_continues(route.auth.generation, latest.generation)
            or (route.auth.mode == latest.mode == "subscription" and
                route.auth.provider == latest.provider == route.provider and route.auth.family == latest.family and
                bool(route.auth.identity) and latest.identity == route.auth.identity))


def observed_refresh(route: Route, event: dict) -> accounts.Auth | None:
    if event.get("provider") != route.provider:
        return None
    before, after = accounts.mapping(event.get("before")), accounts.mapping(event.get("after"))
    route.refreshed_secrets.update(v for v in credential_values([before, after]) if len(v) >= vault.SHORTEST)
    if before.get("type") != "oauth" or after.get("type") != "oauth":
        return None
    db.migrate_on_demand(db.SECRET_REGISTRY_SCHEMA)
    with nullcontext() if db.in_write() else db.one_read():
        earlier = accounts.pi(route.provider, before, str(route.owner))
        previous = earlier.generation
        if (previous not in {route.auth.generation, *route.refreshed}
                and not devices_store.refresh_continues(route.auth.generation, previous)):
            return None
        latest = accounts.pi(route.provider, after, str(route.owner))
        markers = ("accountId", "accountUuid", "account_id", "account_uuid", "userId", "user_id")
        if route.auth.identity:
            if latest.identity != route.auth.identity:
                return None
        elif any(before.get(name) != after.get(name) for name in markers):
            return None
        prefixes = [accounts.scope(a, "")[:-2] if a.mode == "subscription" else accounts.scope(a, "credit")
                    for a in (earlier, latest)]
        legacy = [accounts.legacy_scope(a, "")[:-2] if a.mode == "subscription" else accounts.legacy_scope(a, "credit")
                  for a in (earlier, latest)]
    with db.tx():
        devices_store.record_refresh(previous, latest.generation, *prefixes)
        devices_store.record_refresh(earlier.legacy_generation, latest.legacy_generation, *legacy)
    route.refreshed.add(latest.generation)
    route.refreshed_auths.add(latest)
    if previous != latest.generation and not (earlier.identity or latest.identity):
        if accounts.record_resolved(latest, refresh_only=True):
            return latest
    return None


@contextmanager
def refresh_channel(route: Route | None, env: dict, deferred: list[accounts.Auth]):
    if route is None or not route.using_login:
        yield ()
        return
    reader, writer = os.pipe()
    ack_reader, ack_writer = os.pipe()

    def receive():
        try:
            with os.fdopen(reader, "rb") as source:
                for raw in source:
                    try:
                        if pending := observed_refresh(route, accounts.mapping(json.loads(raw))):
                            deferred.append(pending)
                    except (ValueError, TypeError):
                        pass
                    try:
                        os.write(ack_writer, b"1")
                    except OSError:
                        break
        finally:
            os.close(ack_writer)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    env["ARETE_PI_REFRESH_FD"] = str(writer)
    env["ARETE_PI_REFRESH_ACK_FD"] = str(ack_reader)
    env["ARETE_PI_REFRESH_PROVIDER"] = route.provider
    try:
        yield (writer, ack_reader)
    finally:
        os.close(writer)
        os.close(ack_reader)
        thread.join()


def run(*args, auth: accounts.Auth | None = None, selected: Route | None = None, **kw) -> Result:
    if refusal := remote_refusal(kw.get("remote")):
        return Result(ok=False, error=refusal, runner="pi", model=kw.get("model", ""),
                      session=kw.get("resume", NO_SESSION))
    kw.pop("remote", None)
    selected = selected or select(kw.get("model", ""), kw.get("passing"), kw.get("providers"))
    auth = selected.auth
    kw["model"] = selected.model
    auth = auth or accounts.Auth("api_key")
    if auth.mode == "subscription":
        kw["max_budget_usd"] = None
    started = time.monotonic()
    error = ((selected.token_error or route_token_error(selected) or selected.error) if selected else
             plan_token_error(environment.build((), role="runner:pi").items()))
    if error:
        result = Result(ok=False, error=error, runner="pi", model=kw.get("model", ""),
                        session=kw.get("resume", NO_SESSION))
    elif selected and selected.using_login and not login_unchanged(selected):
        result = Result(ok=False, error=f"pi {selected.provider}: selected login changed or is no longer available",
                        runner="pi", model=selected.model, session=kw.get("resume", NO_SESSION))
    elif (selected and selected.managed and selected.auth.generation !=
          accounts.fingerprint("pi-managed", managed_material(selected.managed, selected.provider))):
        result = Result(ok=False, error=f"pi {selected.provider}: selected credential changed or is no longer available",
                        runner="pi", model=selected.model, session=kw.get("resume", NO_SESSION))
    else:
        folder = None
        deferred = []
        try:
            if selected and selected.view is None:
                folder = prepare(selected, delegation_only=kw.get("delegation_only", False),
                                 structured=kw.get("json_schema") is not None)
                selected = replace(selected, view=folder)
            with sandbox.WorkerLease() as activation:
                activation.acquire()
                result = _run(*args, auth=auth, selected=selected, activation=activation, deferred=deferred, **kw)
        except (OSError, ValueError) as e:
            result = Result(ok=False, error=f"pi setup failed: {e}", runner="pi", model=kw.get("model", ""),
                            session=kw.get("resume", NO_SESSION))
        finally:
            try:
                for refreshed in deferred:
                    accounts.record_resolved(refreshed, refresh_only=True, wait_for_activation=True)
            finally:
                if folder is not None:
                    cleanup(folder)
    result.call_seconds = time.monotonic() - started
    if result.calls:
        result.calls[0].call_seconds = result.call_seconds - sum(call.call_seconds for call in result.calls[1:])
    result.auth_mode = auth.mode
    result.provider = selected.provider if selected else result.provider
    resume = kw.get("resume", NO_SESSION)
    if selected and result.session.id:
        charged = sum(0 if c.auth_mode == "subscription" else kw.get("price", lambda r: r.cost_usd)(c)
                      for c in result.calls or [result])
        result.session = replace(result.session, spent_usd=resume.spent_usd + charged)
    return result


def _run(
    prompt: str,
    cwd: Path | str,
    *,
    model: str = "",
    effort: str = "",
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    permission_mode: str = "dontAsk",
    json_schema: dict | None = None,
    resume: Session = NO_SESSION,
    system: str = "",
    max_budget_usd: float | None = None,
    timeout: float = 1800,
    persist: bool = True,
    add_dirs: list[Path] | None = None,
    mcp_config: str = "",
    instructions: list[str] | None = None,
    price: Callable[[Result], float] = lambda reply: reply.cost_usd,
    on_line: Callable[[str], None] = lambda line: None,
    passing: list[str] | None = None,
    providers: dict[str, tuple[str, ...]] | None = None,
    auth: accounts.Auth | None = None,
    selected: Route | None = None,
    extra_env: dict[str, str] | None = None,
    ask_owner: bool = False,
    floor: guard.Floor | None = None,
    delegation_only: bool = False,
    issue_session: tuple[str, str] | None = None,
    judge: bool = False,
    read_roots: tuple[Path, ...] = (),
    private_worker: bool = False,
    private_hidden: tuple[Path, ...] = (),
    native_observation: str = "",
    activation: sandbox.WorkerLease,
    deferred: list[accounts.Auth],
) -> Result:
    auth = auth if auth is not None else accounts.Auth("api_key")
    if mcp_servers(mcp_config):
        return Result(ok=False, error="pi takes MCP servers only from its own mcp.json, so run this step on claude",
                      session=resume, runner="pi", model=model)
    if instructions:
        return Result(ok=False, error="pi runs with --no-context-files and loads no instruction files, so run this "
                                      "step on claude or set instructions = []", session=resume, runner="pi", model=model)
    cwd = Path(cwd).absolute()
    add_dirs = [Path(d).absolute() for d in add_dirs or []]
    if floor is None:
        from . import guard

        floor = guard.floor(os.environ.get("ARETE_RUN_ID", ""), cwd, add_dirs)
    names = [name for name in tool_names(tools, allowed_tools, disallowed_tools) if name in GUARDED_TOOLS]
    observing = bool(native_observation)
    if observing and (names or ask_owner or persist or resume.id or add_dirs or delegation_only
                      or auth.mode != "subscription" or not json_schema or not pi_shell.simple_command(native_observation)
                      or f"Bash({native_observation})" not in (allowed_tools or [])):
        raise ValueError("native observation requires a fresh tool-free subscription with one exact command")
    if delegation_only and (names or ask_owner or resume.id or json_schema is not None or system or add_dirs or read_roots or persist):
        raise ValueError("controller requires a fresh tool-free, non-restating invocation")
    if persist or resume.id:
        sandbox.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    launch_model = selected.model.partition("/")[2] if selected else model
    cmd = command(names, model=launch_model, effort=effort, resume=resume, system=system, persist=persist, add_dirs=add_dirs,
                  ask_owner=ask_owner, observe=observing)
    if selected and selected.provider:
        cmd += ["--provider", selected.provider]
    if json_schema is not None:
        prompt = f"{prompt}\n\n{SCHEMA_RULE.format(schema=json.dumps(json_schema))}"
    goal_paths = ()
    session_lock = nullcontext(True)
    if issue_session:
        if not delegation_only:
            raise ValueError("a durable issue session requires a tool-free controller")
        key, event = issue_session
        cwd = issue_goal.directory(key)
        private_dir(cwd)
        bundle = issue_goal.runtime()
        goal_paths = (cwd, bundle)
        session_lock = util.lock("issue-goal-" + key, wait=True)
        cmd = [issue_goal.executable("node"), str(bundle / "issue.mjs"), str(cwd), "turn", event,
               selected.provider, launch_model, effort]
    confined = False
    writing = sorted(WRITING & set(names))
    if private_worker:
        if add_dirs or resume.id or persist or ask_owner or not sandbox.can_confine(False):
            raise ValueError("private Pi workers require a fresh local confined invocation")
        floor = replace(floor, open=(cwd,))
        # Private assignments run inside their candidate. External fixture
        # runtimes belong to the engine's independent checks and verification.
        read_roots = ()
        call = selected.view
        cmd = sandbox.write_confinement(
            cwd, [call], selected.owner if selected.using_login else None,
            agent_roots=False, private_temporary=True, peers=paths.WORKTREES_DIR,
            hidden=(paths.CONFIG_DIR, paths.LOG_DIR, paths.CLIENT_DIR, *private_hidden),
            state=(paths.STATE_DIR, (cwd, call, selected.owner, paths.PACKAGE_DIR))) + cmd
        confined = True
    elif writing or observing:
        if not sandbox.can_confine(False):
            return Result(ok=False, error=f"{sandbox.NO_CONFINEMENT_REASON}, so pi cannot run confined commands here",
                          session=resume, runner="pi", model=model)
        cmd = confinement(cwd, add_dirs, floor, selected) + cmd
        confined = True
    if selected and selected.using_login and not confined and sandbox.can_confine(False):
        cmd = confinement(cwd, add_dirs, floor, selected, goal_paths) + cmd

    cmd += [prompt] if issue_session else ["--", prompt]
    folder = selected.view
    env = {**selected.env, **(extra_env or {}), "PI_CODING_AGENT_DIR": str(folder),
           **guard_env(floor, [selected.owner, folder / "auth.json"], selected.sensitive_files,
                       delegation_only=delegation_only, shell=pi_shell.TOOL in names,
                       protected=sandbox.worker_authority(activation)),
           **({"ARETE_ASK_TOOL": json.dumps(owner_asks.pi_tool())} if ask_owner else {})}
    protection = json.loads(env["ARETE_PI_GUARD_PATHS"])
    if private_worker:
        protection["workspace"] = str(cwd)
        # The child cannot resolve paths hidden by its OS sandbox. Pin their denied
        # aliases before launch, separately from the worker's permitted workspace.
        protection["denied_aliases"] = {
            path: str(Path(path).resolve())
            for group in ("roots", "files", "write_roots", "write_files")
            for path in protection[group]
        }
        env["ARETE_PI_GUARD_PATHS"] = json.dumps(protection)
        env["TMPDIR"] = str(folder)
    env.pop("ARETE_PI_OBSERVE", None)
    if observing:
        env["ARETE_PI_OBSERVE"] = native_observation
    allowed = (f"Bash({native_observation})",) if observing else tuple(allowed_tools or ())
    shell = (pi_shell.Shell(cwd, tuple(add_dirs), floor, allowed, tuple(disallowed_tools or ()),
                            pi_shell.command_env(passing, extra_env, selected.hidden),
                            (selected.owner, folder, *selected.sensitive_files, *map(Path, protection["files"])),
                            judge, time.monotonic() + timeout, tuple(read_roots))
             if observing or pi_shell.TOOL in names else None)
    try:
        with session_lock, refresh_channel(selected, env, deferred) as refresh, pi_shell.channel(shell, env) as terminal:
            descriptors = (*refresh, *terminal)
            if error := plan_token_error(env.items()):
                return Result(ok=False, session=resume, error=error, runner="pi", model=model)
            proc, cut_short_by = stream(activation.command(cmd, descriptors), str(cwd), env, timeout, max_budget_usd,
                                       lambda line: reply_cost(line, model, price, auth.mode), on_line,
                                       **({"pass_fds": descriptors} if descriptors else {}))
    except FileNotFoundError as e:
        return Result(ok=False, session=resume, error=f"pi not found: {e}", runner="pi", model=model)
    if cut_short_by:
        return cut_short(proc, resume, model, cut_short_by)
    result = parse(proc, resume, model, json_schema, auth)
    result.auth_mode = auth.mode
    if selected and result.session.id:
        result.session = replace(result.session, spent_usd=resume.spent_usd +
                                 (0 if auth.mode == "subscription" else price(result)))
    if json_schema is not None and result.ok and result.structured is None:
        result.ok = False
        result.error = "pi returned no structured answer; no unadmitted restatement was launched"
    return result


def mcp_servers(mcp_config: str) -> dict:
    try:
        return json.loads(mcp_config).get("mcpServers") or {} if mcp_config else {}
    except (json.JSONDecodeError, AttributeError):
        return {"unreadable": mcp_config}


def stream(cmd: list[str], cwd: str | None, env: dict, timeout: float, max_budget_usd: float | None,
           cost_of: Callable[[str], float],
           on_line: Callable[[str], None], *, pass_fds: tuple[int, ...] = ()) -> tuple[subprocess.CompletedProcess, str]:
    from . import factory_control
    from .store import factory

    key, proc = factory_control.spawn(scratch.spawn, cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace", env=env, pass_fds=pass_fds,
                         actor_confined=bool(cmd and cmd[0] == "/usr/bin/sandbox-exec"))
    stderr: list[str] = []
    reader = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
    reader.start()
    out_of_time = threading.Event()
    timer = threading.Timer(timeout, lambda: (out_of_time.set(), proc.kill()))
    timer.start()
    lines, spent, over_budget = [], 0.0, ""
    try:
        for line in proc.stdout:
            lines.append(line)
            on_line(line)
            if max_budget_usd and '"message_end"' in line:
                spent += cost_of(line)
                if spent > max_budget_usd:
                    over_budget = f"stopped at its ${max_budget_usd:g} budget after ${spent:.2f}"
                    proc.kill()
                    break
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        child.release(proc)
        factory.forget(key)
    reader.join(5)
    done = subprocess.CompletedProcess(cmd, proc.returncode, "".join(lines), "".join(stderr))
    return done, over_budget or (f"timed out after {timeout:g}s" if out_of_time.is_set() else "")


def reply_cost(line: str, model: str, price: Callable[[Result], float], auth_mode: str = "") -> float:
    if auth_mode == "subscription":
        return 0.0
    try:
        message = json.loads(line).get("message") or {}
    except json.JSONDecodeError:
        return 0.0
    if message.get("role") != "assistant":
        return 0.0
    provider = message.get("provider", "")
    return price(Result(ok=True, runner="pi", provider=provider, auth_mode=auth_mode,
                        model=f"{provider}/{message.get('model')}" if provider else model,
                        **usage_totals([message.get("usage") or {}])))


def cut_short(proc: subprocess.CompletedProcess, resume: Session, model: str, reason: str) -> Result:
    stream = events(proc.stdout)
    replies = [e["message"] for e in stream
               if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"]
    totals = usage_totals([m.get("usage") or {} for m in replies])
    return Result(ok=False, session=session_after(stream, resume, totals["cost_usd"]), error=reason,
                  turns=len(replies), runner="pi", model=model, first_context_tokens=first_context_tokens(proc.stdout),
                  **totals)


def first_context_tokens(stream: str) -> int:
    first = next((e["message"] for e in events(stream)
                  if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"), None)
    return usage_totals([first.get("usage") or {}])["tokens_in"] if first else 0


def session_after(stream: list[dict], resume: Session, cost_usd: float) -> Session:
    session_id = next((e.get("id", "") for e in stream if e.get("type") == "session"), "") or resume.id
    return Session(session_id, resume.spent_usd + cost_usd, "pi") if session_id else resume


def events(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                if isinstance(event := json.loads(line), dict):
                    out.append(event)
            except json.JSONDecodeError:
                continue
    return out


def error_metadata(value):
    if isinstance(value, dict):
        return {k: error_metadata(v) for k, v in value.items() if k not in ("content", "usage", "messages")}
    return [error_metadata(v) for v in value] if isinstance(value, list) else value


def error_messages(data) -> list[str]:
    if isinstance(data, str):
        try:
            decoded = json.loads(data)
        except ValueError:
            return [data]
        return error_messages(decoded) if isinstance(decoded, (dict, list)) else [data]
    values = data.values() if isinstance(data, dict) else data if isinstance(data, list) else ()
    return [message for value in values for message in error_messages(value)]


def reply_failures(message: dict) -> list[str | int | None]:
    if message.get("role") != "assistant" or message.get("stopReason") != "error":
        return []
    failures = [message.get("errorMessage")]
    diagnostics = message.get("diagnostics")
    if isinstance(diagnostics, list):
        failures.extend(accounts.mapping(diagnostic.get("details")).get("status")
                        for diagnostic in map(accounts.mapping, diagnostics)
                        if diagnostic.get("type") == "bedrock_response_failure")
    return failures


def stream_failures(stream: list[dict]) -> list[str | int | None]:
    failures = []
    for event in stream:
        kind = event.get("type")
        if kind in ("error", "api_error", "turn.failed"):
            error = event.get("error") or event.get("api_error")
            failures.append(error)
            failures.extend(provider_failure_fields(event))
            failures.extend(provider_failure_fields(accounts.mapping(error)))
        elif kind == "message_end":
            failures.extend(reply_failures(accounts.mapping(event.get("message"))))
    return failures


def parse(proc: subprocess.CompletedProcess, resume: Session, model: str, json_schema: dict | None,
          auth: accounts.Auth | None = None) -> Result:
    provider = model.partition("/")[0]
    auth = auth or accounts.Auth("api_key" if provider == "anthropic" else "subscription", provider=provider)
    stream = events(proc.stdout)
    end = next((e for e in reversed(stream) if e.get("type") == "agent_end"), {})
    replies = [m for m in end.get("messages", []) if m.get("role") == "assistant"]
    diagnostics = [error_metadata(e) for e in stream if e.get("type") in ("error", "api_error", "turn.failed")
                   or ((e.get("error") or e.get("api_error")) and not str(e.get("type", "")).startswith("tool_")
                       and not (isinstance(e.get("message"), dict)
                                and e["message"].get("role") in ("user", "toolResult")))]
    diagnostics.extend(error_metadata(e["message"]) for e in stream
                       if e.get("type") == "message_end" and isinstance(e.get("message"), dict)
                       and e["message"].get("role") == "assistant" and e["message"].get("stopReason") == "error")
    failures = [*stream_failures(stream), proc.stderr or ""]
    if not replies:
        error = util.tail(proc.stderr or proc.stdout or f"exit {proc.returncode}", 1500)
        diagnostic = [*diagnostics, proc.stderr] if stream else proc.stderr or proc.stdout
        return Result(ok=False, session=resume, error=error, runner="pi", model=model,
                      connection_failed=util.is_network_error(error),
                      usage_limit=usage_limit(auth, provider, diagnostic, model, failure_text=failures))
    last = replies[-1]
    text = "".join(c.get("text", "") for c in last.get("content", []) if c.get("type") == "text")
    usages = [m.get("usage") or {} for m in replies]
    totals = usage_totals(usages)
    failed = last.get("stopReason") == "error" or proc.returncode != 0
    error = (last.get("errorMessage") or util.first_line(proc.stderr, 1500) or "pi stopped on an error") if failed else ""
    limit = None
    if failed:
        for reply in replies:
            failures.extend(reply_failures(reply))
        if last.get("stopReason") != "error":
            failures.append(last.get("errorMessage"))
        limit = usage_limit(auth, provider, [*diagnostics, error_metadata(last), proc.stderr], model, failure_text=failures)
    return Result(
        ok=not failed,
        text=text,
        session=session_after(stream, resume, totals["cost_usd"]),
        structured=json_from_text(text) if json_schema is not None and not failed else None,
        turns=0 if end.get("durableReplay") is True else len(replies),
        error=error,
        connection_failed=failed and util.is_network_error(error),
        usage_limit=limit,
        raw={"stopReason": last.get("stopReason"), "usage": usages[-1]},
        **totals,
        first_context_tokens=first_context_tokens(proc.stdout),
        model=f"{last.get('provider')}/{last.get('model')}" if last.get("provider") else model,
        provider=last.get("provider", ""),
        runner="pi",
    )
