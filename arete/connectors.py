from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import config, configkinds, configschema, paths, secretstore, send_push, util
from .store import config as config_store
from .store import credentials as cred_store
from .store import db as foundation

SUPPORTED_SERVICES = frozenset(("github", "jira"))
PROTECTED_CONNECTORS = frozenset(("github", "jira"))
SERVICE_MODES: dict[str, frozenset[str]] = {
    "github": frozenset(("app", "api_key", "token", "pat", "oauth", "cli")),
    "jira": frozenset(("api_key", "token", "oauth")),
}
SERVICE_OPTIONS: dict[str, frozenset[str]] = {
    "github": frozenset(("app_id", "installation_id", "owner")),
    "jira": frozenset(("email", "project")),
}
SERVICE_SECRETS: dict[str, frozenset[str]] = {
    "github": frozenset(("api_token", "app_private_key")),
    "jira": frozenset(("api_token",)),
}
DEFAULT_ENDPOINTS: dict[str, str] = {
    "github": "https://api.github.com",
    "jira": "",
}
_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1", "localhost"))
_NAME_RE = re.compile(rf"^{configkinds.NAME_PATTERN}$")
_SECRET_REF_RE = re.compile(
    r"^secret:(?P<scope>global|(?:repo|line|device|connector):[A-Za-z0-9][A-Za-z0-9_.-]{0,63})"
    r"/(?P<name>[A-Z][A-Z0-9_]{0,63})$"
)
_CREDENTIAL_LITERAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub token literal", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,})\b")),
    ("Atlassian token literal", re.compile(r"\bATATT[A-Za-z0-9_\-=]{10,}")),
    ("API key literal", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")),
    ("private key literal", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("authorization header literal", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")),
)
_FORBIDDEN_KEY_NAMES = frozenset((
    "token", "api_token", "api_key", "password", "passwd", "secret", "private_key",
    "app_private_key", "access_token", "refresh_token", "authorization", "auth",
))


class ConnectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class Connector:
    schema: int
    id: str
    name: str
    service: str
    driver: str
    account: str
    enabled: bool
    endpoint: str
    options: dict[str, str]
    secrets: dict[str, str]
    level: str = "config"
    protected: bool = False
    path: str = ""
    version: int = 0
    content_version: str = ""


def _content_version(name: str, raw: bytes) -> str:
    return f"connector/{name}@{hashlib.sha256(raw).hexdigest()[:12]}"


def _is_loopback(host: str | None) -> bool:
    return (host or "").lower() in _LOOPBACK_HOSTS


def normalize_endpoint(endpoint: str, *, service: str, allow_empty: bool = False) -> str:
    text = endpoint.strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{service} connector requires a non-empty endpoint URL")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("endpoint must not contain whitespace or control characters")
    parts = urlsplit(text)
    if parts.username is not None or parts.password is not None:
        raise ValueError("endpoint must not contain embedded credentials")
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.hostname:
        raise ValueError("endpoint must be a full HTTP(S) URL")
    if parts.query or parts.fragment:
        raise ValueError("endpoint must not contain a query string or fragment")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ValueError("non-loopback endpoint must use https://")
    normalized_path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{normalized_path}"


def parse_secret_ref(ref: str) -> tuple[str, str]:
    if not isinstance(ref, str):
        raise ValueError("secret reference must be a string")
    match = _SECRET_REF_RE.fullmatch(ref)
    if not match:
        raise ValueError("secret reference must use secret:<scope>/<NAME> format (literal values are not allowed)")
    scope, name = match["scope"], match["name"]
    secretstore.validate(scope, name)
    if f"{scope}/{name}" in (secretstore.KEY_ITEM, secretstore.VERIFICATION_KEY_ITEM):
        raise ValueError("internal Arete secrets cannot be referenced by a connector")
    return scope, name


def _iter_strings(value: object) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_iter_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_iter_strings(item))
    return out


def contains_credential_literal(raw_text: str = "", data: object = None) -> str | None:
    """Return a non-secret description if a literal credential value is detected, else None."""
    samples: list[str] = []
    if raw_text:
        samples.append(raw_text)
    if data is not None:
        samples.extend(_iter_strings(data))
    combined = "\n".join(text for text in samples if text)
    if combined:
        for label, pattern in _CREDENTIAL_LITERAL_PATTERNS:
            if pattern.search(combined):
                return f"literal credential values are forbidden ({label})"
        if send_push.scan_text_for_secrets(combined) is not None:
            return "literal credential values are forbidden (matches a stored secret)"
    if isinstance(data, dict):
        endpoint = data.get("endpoint")
        if isinstance(endpoint, str):
            parts = urlsplit(endpoint.strip())
            if parts.username is not None or parts.password is not None:
                return "literal credential values are forbidden (embedded URL credentials)"
        raw_secrets = data.get("secrets")
        if isinstance(raw_secrets, dict):
            for key, val in raw_secrets.items():
                if not isinstance(val, str) or not _SECRET_REF_RE.fullmatch(val):
                    return f"secrets.{key} must be a secret:<scope>/<NAME> reference, not a literal credential value"
        for key in data:
            if key in _FORBIDDEN_KEY_NAMES:
                return f"unknown key {key} (literal credential fields are forbidden; use secret references)"
    return None


def validate_structure(
    item_name: str,
    data: dict[str, object],
    *,
    where: str | None = None,
    allow_protected: bool = False,
    raw_text: str = "",
) -> Connector:
    label = where or f"connector/{item_name}"
    if not isinstance(item_name, str) or not _NAME_RE.fullmatch(item_name):
        raise ValueError(f"invalid connector name: {item_name!r}")
    if not allow_protected and item_name in PROTECTED_CONNECTORS:
        raise ValueError(f"{label}: connector {item_name!r} is a protected engine default and cannot be overwritten")
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a TOML table")

    if cred_problem := contains_credential_literal(raw_text, data):
        raise ValueError(f"{label}: {cred_problem}")

    configschema.validate("connector", data, where=label)

    conn_id = str(data["id"])
    if conn_id != item_name:
        raise ValueError(f"{label}: id {conn_id!r} must match connector name {item_name!r}")

    service = str(data["service"])
    if service not in SUPPORTED_SERVICES:
        raise ValueError(f"{label}: unsupported connector service {service!r}")
    driver = str(data.get("driver") or service)
    if driver != service:
        raise ValueError(f"{label}: driver {driver!r} does not match service {service!r}")

    display_name = str(data.get("name") or conn_id).strip()
    if not display_name:
        raise ValueError(f"{label}.name must not be empty")
    enabled = bool(data.get("enabled", True))
    account = str(data.get("account") or "").strip()
    if not allow_protected and not account:
        raise ValueError(f"{label}: account is required for connector {item_name!r}")

    raw_options = data.get("options") or {}
    options: dict[str, str] = {}
    allowed_opts = SERVICE_OPTIONS[service]
    for opt_key, opt_val in raw_options.items():
        if opt_key not in allowed_opts:
            raise ValueError(f"{label}.options.{opt_key} is not supported for {service} connectors")
        options[opt_key] = str(opt_val)

    raw_secrets = data.get("secrets") or {}
    secret_refs: dict[str, str] = {}
    allowed_secrets = SERVICE_SECRETS[service]
    allowed_scopes = frozenset(("global", f"connector:{item_name}"))
    for sec_key, sec_val in raw_secrets.items():
        if sec_key not in allowed_secrets:
            raise ValueError(f"{label}.secrets.{sec_key} is not supported for {service} connectors")
        scope, sec_name = parse_secret_ref(str(sec_val))
        if scope not in allowed_scopes:
            raise ValueError(
                f"{label}.secrets.{sec_key} scope {scope!r} is not allowed for connector {item_name!r} "
                f"(allowed: global or connector:{item_name})"
            )
        secret_refs[sec_key] = f"secret:{scope}/{sec_name}"

    raw_endpoint = str(data.get("endpoint") or "").strip()
    if not raw_endpoint:
        raw_endpoint = DEFAULT_ENDPOINTS[service]
    endpoint = normalize_endpoint(
        raw_endpoint,
        service=service,
        allow_empty=(allow_protected and service == "jira"),
    )

    return Connector(
        schema=int(data["schema"]),
        id=conn_id,
        name=display_name,
        service=service,
        driver=driver,
        account=account,
        enabled=enabled,
        endpoint=endpoint,
        options=options,
        secrets=secret_refs,
        level="engine" if allow_protected else "config",
        protected=allow_protected,
        path=where or f"connectors/{item_name}.toml",
    )


def _check_secret_readable_ro(c: sqlite3.Connection, scope: str, name: str) -> tuple[dict | None, str | None]:
    """Strictly read-only secret check using an existing reader connection. Never returns secret bytes."""
    try:
        item = secretstore.external_item(scope, name)
    except (ValueError, secretstore.InternalSecret) as error:
        return None, str(error)
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'secrets'").fetchone() is None:
        return None, "secret registry is not initialized"
    row = cred_store.secret(c, scope, name)
    if row is None:
        return None, f"secret {secretstore.address(scope, name)} is not registered"
    if row["status"] != "set" or not row.get("fingerprint"):
        return row, f"secret {secretstore.address(scope, name)} is missing"
    settings = config.secret_settings()
    if settings.store is None:
        return row, "secret store is not configured"
    try:
        meta = foundation.metadata(c)
        _, key = secretstore.lookup_key(c, settings, secretstore.local_key_path(), meta)
        if key is None:
            return row, "secret store fingerprint key is unavailable"
        ref = secretstore.registry_reference(c, row, item)
        if ref.store not in (settings.store, "owner_file"):
            return row, f"secret {secretstore.address(scope, name)} belongs to a different store backend"
        value = secretstore.read_item(ref, settings)
    except (
        secretstore.StoreError,
        secretstore.StoreMissing,
        secretstore.FingerprintKeyMissing,
        secretstore.InternalSecret,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        return row, f"secret {secretstore.address(scope, name)} is unreadable ({type(error).__name__})"
    if value is None:
        return row, f"secret {secretstore.address(scope, name)} is unreadable in the secret store"
    if secretstore.item_digest(key, value) != row["fingerprint"]:
        return row, f"secret {secretstore.address(scope, name)} failed fingerprint verification"
    return row, None


def _principal_row(c: sqlite3.Connection, principal_id: str) -> dict | None:
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'principals'").fetchone() is None:
        return None
    row = c.execute("SELECT id, kind, role, status FROM principals WHERE id = ?", (principal_id,)).fetchone()
    return dict(row) if row else None


def _owner_id_in(c: sqlite3.Connection) -> str:
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'principals'").fetchone() is None:
        return "hand"
    row = c.execute(
        "SELECT id FROM principals WHERE kind = 'human' AND status = 'active' AND role = 'owner' "
        "ORDER BY created, rowid LIMIT 1"
    ).fetchone()
    return str(row["id"]) if row else "hand"


def _resolve_caller_principal(c: sqlite3.Connection, caller: str) -> tuple[str | None, str | None, str | None]:
    """Return (principal_id, role, error) for a caller actor string without writing to SQLite."""
    actor = (caller or "").strip()
    if not actor:
        return None, None, "caller identity is required"
    if actor.startswith(("run:", "loop:")) or actor in ("miner", "optimize", "agent", "arete"):
        return None, None, f"caller {actor!r} lacks authority to bind registry accounts"
    if actor == "hand":
        owner_id = _owner_id_in(c)
        return owner_id, "owner", None
    if actor.startswith("client:"):
        token_id = actor.removeprefix("client:")
        if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tokens'").fetchone() is None:
            return None, None, f"caller token {actor!r} cannot be verified"
        tok = c.execute(
            "SELECT principal, revoked_at, expires FROM tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
        if tok is None or tok["revoked_at"] is not None or (
            tok["expires"] is not None and str(tok["expires"]) <= util.iso()
        ):
            return None, None, f"caller token {actor!r} is inactive or revoked"
        actor = str(tok["principal"])
    row = _principal_row(c, actor)
    if row is None:
        return None, None, f"caller principal {actor!r} is not registered"
    if row["status"] != "active":
        return None, None, f"caller principal {actor!r} is not active"
    return str(row["id"]), str(row["role"]), None


def _check_caller_authority(c: sqlite3.Connection, account_row: dict, caller: str) -> str | None:
    holder = str(account_row.get("holder") or "")
    usage = str(account_row.get("usage") or "")
    holder_row = _principal_row(c, holder) if holder and holder != "hand" else None
    if holder_row is not None and holder_row["status"] != "active":
        return f"account {account_row['id']!r} holder {holder!r} is not active"

    principal_id, role, err = _resolve_caller_principal(c, caller)
    if err:
        return err
    if usage == "personal":
        if caller == "hand":
            if holder not in ("hand", principal_id):
                return (
                    f"caller {caller!r} is not authorized to use personal account "
                    f"{account_row['id']!r} held by {holder!r}"
                )
            return None
        if principal_id != holder:
            return (
                f"caller {principal_id!r} is not authorized to use personal account "
                f"{account_row['id']!r} held by {holder!r}"
            )
        return None
    if usage == "workspace":
        if role != "owner":
            return f"caller {principal_id!r} lacks owner authority to bind workspace account {account_row['id']!r}"
        return None
    return f"account {account_row['id']!r} has unsupported usage {usage!r}"


def _check_account_scope(
    usage_scope: str | None,
    *,
    service: str,
    connector_id: str,
    endpoint: str,
    options: dict[str, str],
) -> str | None:
    raw = (usage_scope or "").strip()
    if not raw:
        return None
    norm_ep = normalize_endpoint(endpoint, service=service, allow_empty=True) if endpoint else ""
    ep_parts = urlsplit(norm_ep) if norm_ep else None
    ep_host = (ep_parts.hostname or "").lower() if ep_parts else ""
    ep_netloc = (ep_parts.netloc or "").lower() if ep_parts else ""
    owner_opt = str(options.get("owner") or "").strip().lower()

    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return "account usage_scope is malformed JSON"
        if not isinstance(parsed, dict):
            return "account usage_scope must be a JSON object or scope string"
        for ep_key in ("endpoint", "base_url", "site"):
            if ep_val := parsed.get(ep_key):
                try:
                    expected_ep = normalize_endpoint(str(ep_val), service=service)
                except ValueError:
                    return f"account usage_scope {ep_key} is invalid"
                if norm_ep != expected_ep:
                    return f"connector endpoint {norm_ep!r} does not match account tenant/endpoint {expected_ep!r}"
        if host_val := parsed.get("host"):
            expected_host = str(host_val).strip().lower()
            if expected_host not in (ep_host, ep_netloc):
                return f"connector endpoint host {ep_netloc!r} does not match account host {expected_host!r}"
        for tenant_key in ("tenant", "owner"):
            if tenant_val := parsed.get(tenant_key):
                expected = str(tenant_val).strip().lower()
                if owner_opt:
                    if owner_opt != expected:
                        return f"connector owner {owner_opt!r} does not match account {tenant_key} {expected!r}"
                elif expected not in (ep_host, ep_netloc):
                    return f"connector does not match account {tenant_key} {expected!r}"
        if conn_val := parsed.get("connector"):
            if str(conn_val).strip() != connector_id:
                return f"account is restricted to connector {conn_val!r}, not {connector_id!r}"
        return None

    if raw.startswith(("http://", "https://")):
        try:
            expected_ep = normalize_endpoint(raw, service=service)
        except ValueError:
            return "account usage_scope endpoint URL is invalid"
        if norm_ep != expected_ep:
            return f"connector endpoint {norm_ep!r} does not match account tenant/endpoint {expected_ep!r}"
        return None

    if raw.startswith("connector:"):
        expected_conn = raw.removeprefix("connector:").strip()
        if expected_conn != connector_id:
            return f"account is restricted to connector {expected_conn!r}, not {connector_id!r}"
        return None

    if raw.startswith(("tenant:", "owner:")):
        expected = raw.partition(":")[2].strip().lower()
        if owner_opt:
            if owner_opt != expected:
                return f"connector owner {owner_opt!r} does not match account scope {raw!r}"
            return None
        if expected in (ep_host, ep_netloc):
            return None
        return f"connector does not match account scope {raw!r}"

    expected_low = raw.lower()
    if expected_low in (ep_host, ep_netloc) or (owner_opt and owner_opt == expected_low):
        return None
    return f"connector endpoint/tenant does not match account scope {raw!r}"


def _expected_secret_key(service: str, mode: str) -> str | None:
    if service == "github":
        if mode == "app":
            return "app_private_key"
        if mode == "cli":
            return None
        return "api_token"
    if service == "jira":
        return "api_token"
    return None


def _account_status_summary(
    c: sqlite3.Connection,
    row: dict,
    *,
    caller: str | None = None,
    connector_id: str | None = None,
    endpoint: str | None = None,
    options: dict[str, str] | None = None,
    service_filter: str | None = None,
) -> dict:
    service = str(row["service"] or "").lower()
    mode = str(row["mode"] or "")
    usage = str(row["usage"] or "")
    holder = str(row["holder"] or "")
    scope_val = (row.get("usage_scope") or "").strip() or None
    sec_scope = row.get("secret_scope")
    sec_name = row.get("secret_name")
    sec_ref = f"secret:{sec_scope}/{sec_name}" if sec_scope and sec_name else None
    supported = service in SUPPORTED_SERVICES
    problems: list[str] = []
    status = "ready"

    if not supported:
        status = "unsupported_service"
        problems.append(f"service {row['service']!r} is not a supported connector service")
    elif service_filter and service != service_filter:
        status = "service_mismatch"
        problems.append(f"account service {service!r} does not match {service_filter!r}")
    elif mode not in SERVICE_MODES[service]:
        status = "incompatible_mode"
        problems.append(f"account mode {mode!r} is not supported for {service} connectors")

    holder_row = _principal_row(c, holder) if holder and holder != "hand" else None
    if holder_row is not None and holder_row["status"] != "active":
        if status == "ready":
            status = "inactive_holder"
        problems.append(f"account holder {holder!r} is not active")

    if caller is not None:
        if auth_err := _check_caller_authority(c, row, caller):
            if status == "ready":
                status = "unauthorized"
            problems.append(auth_err)

    if supported and (endpoint or connector_id):
        if scope_err := _check_account_scope(
            scope_val,
            service=service,
            connector_id=connector_id or "",
            endpoint=endpoint or DEFAULT_ENDPOINTS[service],
            options=options or {},
        ):
            if status == "ready":
                status = "scope_mismatch"
            problems.append(scope_err)

    if sec_scope and sec_name:
        if row.get("secret_provider") and str(row["secret_provider"]).lower() != service:
            if status == "ready":
                status = "secret_mismatch"
            problems.append(f"linked secret provider {row['secret_provider']!r} does not match {service!r}")
        _, sec_err = _check_secret_readable_ro(c, sec_scope, sec_name)
        if sec_err:
            if status == "ready":
                status = "missing_secret" if row.get("secret_status") != "set" else "unreadable_secret"
            problems.append(sec_err)
    elif supported and mode != "cli":
        if status == "ready":
            status = "missing_secret"
        problems.append(f"account {row['id']!r} has no linked credential secret")

    return {
        "id": str(row["id"]),
        "service": service,
        "mode": mode,
        "usage": usage,
        "holder": holder,
        "scope": scope_val,
        "secret_name": str(sec_name) if sec_name else None,
        "secret_ref": sec_ref,
        "supported_service": supported,
        "status": status,
        "usable": not problems,
        "problems": problems,
    }


def _validate_account_binding_conn(
    c: sqlite3.Connection,
    connector: Connector,
    *,
    caller: str | None = None,
) -> tuple[dict | None, list[str]]:
    if connector.protected and not connector.account:
        return None, []
    row = cred_store.account_detail(c, connector.account)
    if row is None:
        return None, [f"account {connector.account!r} does not exist in the account registry"]

    problems: list[str] = []
    acct_service = str(row["service"] or "").lower()
    acct_mode = str(row["mode"] or "")

    if acct_service != connector.service:
        problems.append(
            f"account {connector.account!r} belongs to service {acct_service!r}, not {connector.service!r}"
        )
    if acct_mode not in SERVICE_MODES[connector.service]:
        problems.append(
            f"account {connector.account!r} has mode {acct_mode!r}, which is incompatible with {connector.service!r}"
        )

    if connector.service == "github" and acct_mode == "app":
        if not connector.options.get("app_id"):
            problems.append("github app connector requires options.app_id")
        if not connector.options.get("installation_id"):
            problems.append("github app connector requires options.installation_id")
    if connector.service == "jira" and acct_mode == "api_key":
        if not connector.options.get("email"):
            problems.append("jira api_key connector requires options.email")
        if not connector.endpoint:
            problems.append("jira connector requires endpoint")

    if scope_err := _check_account_scope(
        row.get("usage_scope"),
        service=connector.service,
        connector_id=connector.id,
        endpoint=connector.endpoint,
        options=connector.options,
    ):
        problems.append(scope_err)

    if caller is not None:
        if auth_err := _check_caller_authority(c, row, caller):
            problems.append(auth_err)
    else:
        holder = str(row.get("holder") or "")
        holder_row = _principal_row(c, holder) if holder and holder != "hand" else None
        if holder_row is not None and holder_row["status"] != "active":
            problems.append(f"account {connector.account!r} holder {holder!r} is not active")

    expected_sec_key = _expected_secret_key(connector.service, acct_mode)
    linked_scope = row.get("secret_scope")
    linked_name = row.get("secret_name")
    linked_ref = f"secret:{linked_scope}/{linked_name}" if linked_scope and linked_name else None
    allowed_scopes = frozenset(("global", f"connector:{connector.id}"))

    if connector.secrets:
        if expected_sec_key is None:
            problems.append(f"{connector.service} account mode {acct_mode!r} does not accept secret references")
        else:
            for key_name in sorted(connector.secrets):
                if key_name != expected_sec_key:
                    problems.append(
                        f"secrets.{key_name} is incompatible with {connector.service} account mode {acct_mode!r} "
                        f"(expected secrets.{expected_sec_key})"
                    )
            if expected_sec_key in connector.secrets:
                ref_str = connector.secrets[expected_sec_key]
                sec_scope, sec_name = parse_secret_ref(ref_str)
                if linked_ref is not None and ref_str != linked_ref:
                    problems.append(
                        f"secrets.{expected_sec_key} ({ref_str}) does not match account "
                        f"{connector.account!r} secret ({linked_ref})"
                    )
                sec_row, sec_err = _check_secret_readable_ro(c, sec_scope, sec_name)
                if sec_err:
                    problems.append(sec_err)
                elif sec_row is not None:
                    sec_provider = str(sec_row.get("provider") or "").lower()
                    if sec_provider and sec_provider != connector.service:
                        problems.append(
                            f"secret {ref_str} belongs to provider {sec_provider!r}, not {connector.service!r}"
                        )
    elif expected_sec_key is not None:
        if linked_scope and linked_name:
            if linked_scope not in allowed_scopes:
                problems.append(
                    f"account {connector.account!r} linked secret scope {linked_scope!r} is not allowed for "
                    f"connector {connector.id!r}"
                )
            sec_provider = str(row.get("secret_provider") or "").lower()
            if sec_provider and sec_provider != connector.service:
                problems.append(
                    f"account {connector.account!r} linked secret belongs to provider {sec_provider!r}, "
                    f"not {connector.service!r}"
                )
            _, sec_err = _check_secret_readable_ro(c, linked_scope, linked_name)
            if sec_err:
                problems.append(sec_err)
        else:
            problems.append(f"account {connector.account!r} has no readable secret reference")

    summary = _account_status_summary(
        c,
        row,
        caller=caller,
        connector_id=connector.id,
        endpoint=connector.endpoint,
        options=connector.options,
        service_filter=connector.service,
    )
    if problems:
        summary = {**summary, "usable": False, "problems": list(dict.fromkeys([*summary["problems"], *problems]))}
    return summary, list(dict.fromkeys(problems))


def validate_account_binding(
    connector: Connector,
    *,
    caller: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict | None, list[str]]:
    if connector.protected and not connector.account:
        return None, []
    if conn is not None:
        return _validate_account_binding_conn(conn, connector, caller=caller)
    if not paths.DB_FILE.exists():
        return None, [f"account {connector.account!r} cannot be verified: database is unavailable"]
    try:
        with foundation.reader() as c:
            return _validate_account_binding_conn(c, connector, caller=caller)
    except (foundation.NoDatabase, sqlite3.Error, foundation.IncompatibleSchema) as error:
        return None, [f"account {connector.account!r} cannot be verified: {error}"]


def validate_connector(
    item_name: str,
    data: dict[str, object],
    *,
    where: str | None = None,
    caller: str | None = None,
    raw_text: str = "",
    allow_protected: bool = False,
) -> tuple[Connector, dict | None]:
    connector = validate_structure(
        item_name,
        data,
        where=where,
        allow_protected=allow_protected,
        raw_text=raw_text,
    )
    summary, problems = validate_account_binding(connector, caller=caller)
    if problems:
        label = where or f"connector/{item_name}"
        raise ValueError(f"{label}: {'; '.join(problems)}")
    return connector, summary


def _parse_toml_file(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConnectorError(f"{label} is not valid TOML: {error}") from error
    if not isinstance(parsed, dict):
        raise ConnectorError(f"{label} must be a TOML table")
    return parsed, raw


def _live_meta(name: str, rel: str, raw: bytes, *, level: str) -> tuple[int, str]:
    if level == "engine":
        return 0, _content_version(name, raw)
    if paths.DB_FILE.exists():
        try:
            row = config_store.live_item("connector", name) or config_store.live_by_path(rel)
            if row is not None:
                return int(row.get("ver") or 0), str(row.get("version") or _content_version(name, raw))
        except (foundation.NoDatabase, sqlite3.Error, foundation.IncompatibleSchema):
            pass
    return 0, _content_version(name, raw)


def _load_default_connectors() -> dict[str, Connector]:
    out: dict[str, Connector] = {}
    folder = paths.DEFAULTS_DIR / "connectors"
    if not folder.is_dir():
        return out
    for file in sorted(folder.glob("*.toml")):
        if not file.is_file():
            continue
        name = file.stem
        rel = f"arete/defaults/connectors/{file.name}"
        data, raw = _parse_toml_file(file, rel)
        parsed = validate_structure(
            name,
            data,
            where=rel,
            allow_protected=True,
            raw_text=raw.decode("utf-8", errors="replace"),
        )
        ver, cv = _live_meta(name, rel, raw, level="engine")
        out[name] = Connector(
            schema=parsed.schema,
            id=parsed.id,
            name=parsed.name,
            service=parsed.service,
            driver=parsed.driver,
            account=parsed.account,
            enabled=parsed.enabled,
            endpoint=parsed.endpoint,
            options=parsed.options,
            secrets=parsed.secrets,
            level="engine",
            protected=True,
            path=rel,
            version=ver,
            content_version=cv,
        )
    return out


def load_connectors(config_dir: Path | None = None) -> dict[str, Connector]:
    found = _load_default_connectors()
    root = (config_dir or paths.CONFIG_DIR).resolve()
    folder = root / "connectors"
    if folder.is_dir():
        for file in sorted(folder.glob("*.toml")):
            if not file.is_file() or file.is_symlink():
                continue
            name = file.stem
            if name in PROTECTED_CONNECTORS:
                continue
            rel = f"connectors/{file.name}"
            data, raw = _parse_toml_file(file, rel)
            try:
                parsed = validate_structure(
                    name,
                    data,
                    where=rel,
                    allow_protected=False,
                    raw_text=raw.decode("utf-8", errors="replace"),
                )
            except ValueError as error:
                raise ConnectorError(str(error)) from error
            ver, cv = _live_meta(name, rel, raw, level="config")
            found[name] = Connector(
                schema=parsed.schema,
                id=parsed.id,
                name=parsed.name,
                service=parsed.service,
                driver=parsed.driver,
                account=parsed.account,
                enabled=parsed.enabled,
                endpoint=parsed.endpoint,
                options=parsed.options,
                secrets=parsed.secrets,
                level="config",
                protected=False,
                path=rel,
                version=ver,
                content_version=cv,
            )
    return found


def load_connector(connector_id: str, *, config_dir: Path | None = None) -> Connector:
    if not isinstance(connector_id, str) or not _NAME_RE.fullmatch(connector_id):
        raise ConnectorError(f"invalid connector id: {connector_id!r}")
    root = (config_dir or paths.CONFIG_DIR).resolve()
    user_file = root / "connectors" / f"{connector_id}.toml"
    if connector_id in PROTECTED_CONNECTORS:
        if user_file.exists():
            raise ConnectorError(
                f"connectors/{connector_id}.toml conflicts with protected engine default connector {connector_id!r}"
            )
        defaults = _load_default_connectors()
        if connector_id not in defaults:
            raise ConnectorError(f"protected default connector {connector_id!r} is unavailable")
        return defaults[connector_id]
    if not user_file.is_file() or user_file.is_symlink():
        raise ConnectorError(f"unknown connector {connector_id!r}")
    rel = f"connectors/{connector_id}.toml"
    data, raw = _parse_toml_file(user_file, rel)
    try:
        parsed = validate_structure(
            connector_id,
            data,
            where=rel,
            allow_protected=False,
            raw_text=raw.decode("utf-8", errors="replace"),
        )
    except ValueError as error:
        raise ConnectorError(str(error)) from error
    ver, cv = _live_meta(connector_id, rel, raw, level="config")
    return Connector(
        schema=parsed.schema,
        id=parsed.id,
        name=parsed.name,
        service=parsed.service,
        driver=parsed.driver,
        account=parsed.account,
        enabled=parsed.enabled,
        endpoint=parsed.endpoint,
        options=parsed.options,
        secrets=parsed.secrets,
        level="config",
        protected=False,
        path=rel,
        version=ver,
        content_version=cv,
    )


def discover_accounts(
    *,
    caller: str | None = None,
    service: str | None = None,
    connector_id: str | None = None,
    endpoint: str | None = None,
) -> list[dict]:
    """Strictly read-only account discovery and status check. Never writes or initiates login."""
    if not paths.DB_FILE.exists():
        return []
    service_filter = (service or "").strip().lower() or None
    if service_filter and service_filter not in SUPPORTED_SERVICES:
        return []
    try:
        with foundation.reader() as c:
            rows = cred_store.account_detail_rows(c)
            out: list[dict] = []
            for row in rows:
                row_service = str(row["service"] or "").lower()
                if service_filter and row_service != service_filter:
                    continue
                out.append(
                    _account_status_summary(
                        c,
                        row,
                        caller=caller,
                        connector_id=connector_id,
                        endpoint=endpoint,
                        service_filter=service_filter,
                    )
                )
            return out
    except (foundation.NoDatabase, sqlite3.Error, foundation.IncompatibleSchema):
        return []


def _default_connector_status(connector: Connector, *, caller: str | None = None) -> tuple[str, bool, list[str], dict | None]:
    """Inspect implicit/default GitHub or Jira readiness without writing or copying credentials."""
    if not connector.enabled:
        return "disabled", False, [f"connector {connector.id!r} is disabled"], None
    acct_list = discover_accounts(caller=caller, service=connector.service)
    usable_acct = next((a for a in acct_list if a["usable"]), None)
    if connector.service == "jira":
        try:
            cfg = config.load_or_problem()[0]
            jira_cfg = cfg.jira
        except Exception:
            jira_cfg = config.JiraConfig()
        has_site = bool(jira_cfg.base_url)
        has_email = bool(jira_cfg.email)
        if usable_acct is not None and has_site and has_email:
            return "ready", True, [], usable_acct
        if usable_acct is not None:
            return "ready", True, [], usable_acct
        return "unconfigured", True, [], acct_list[0] if acct_list else None
    if usable_acct is not None:
        return "ready", True, [], usable_acct
    return "ready", True, [], acct_list[0] if acct_list else None


def connector_view(connector: Connector, *, caller: str | None = None) -> dict:
    if connector.protected and not connector.account:
        status, usable, problems, acct_summary = _default_connector_status(connector, caller=caller)
    else:
        acct_summary, problems = validate_account_binding(connector, caller=caller)
        if not connector.enabled:
            status = "disabled"
            usable = False
        elif not problems:
            status = "ready"
            usable = True
        elif acct_summary is None:
            status = "account_unavailable"
            usable = False
        else:
            status = acct_summary["status"] if acct_summary["status"] != "ready" else "invalid"
            usable = False
    return {
        "id": connector.id,
        "name": connector.name,
        "service": connector.service,
        "driver": connector.driver,
        "account": connector.account or (acct_summary["id"] if acct_summary else None),
        "enabled": connector.enabled,
        "endpoint": connector.endpoint or None,
        "options": dict(connector.options),
        "secrets": dict(connector.secrets),
        "level": connector.level,
        "protected": connector.protected,
        "path": connector.path,
        "version": connector.version,
        "content_version": connector.content_version,
        "status": status,
        "usable": usable,
        "problems": problems,
        "account_status": acct_summary,
    }


def list_connector_views(*, caller: str | None = None, config_dir: Path | None = None) -> list[dict]:
    found: dict[str, dict] = {}
    for cid, conn_obj in _load_default_connectors().items():
        found[cid] = connector_view(conn_obj, caller=caller)
    root = (config_dir or paths.CONFIG_DIR).resolve()
    folder = root / "connectors"
    if folder.is_dir():
        for file in sorted(folder.glob("*.toml")):
            if not file.is_file() or file.is_symlink():
                continue
            cid = file.stem
            if cid in PROTECTED_CONNECTORS:
                continue
            rel = f"connectors/{file.name}"
            try:
                conn_obj = load_connector(cid, config_dir=root)
                found[cid] = connector_view(conn_obj, caller=caller)
            except ConnectorError as error:
                raw = file.read_bytes() if file.is_file() else b""
                ver, cv = _live_meta(cid, rel, raw, level="config")
                found[cid] = {
                    "id": cid,
                    "name": cid,
                    "service": "",
                    "driver": "",
                    "account": None,
                    "enabled": False,
                    "endpoint": None,
                    "options": {},
                    "secrets": {},
                    "level": "config",
                    "protected": False,
                    "path": rel,
                    "version": ver,
                    "content_version": cv,
                    "status": "invalid",
                    "usable": False,
                    "problems": [str(error)],
                    "account_status": None,
                }
    return [found[key] for key in sorted(found)]


def get_connector_view(
    connector_id: str,
    *,
    caller: str | None = None,
    config_dir: Path | None = None,
) -> dict | None:
    if not isinstance(connector_id, str) or not _NAME_RE.fullmatch(connector_id):
        return None
    root = (config_dir or paths.CONFIG_DIR).resolve()
    user_file = root / "connectors" / f"{connector_id}.toml"
    if connector_id in PROTECTED_CONNECTORS:
        defaults = _load_default_connectors()
        if connector_id not in defaults:
            return None
        return connector_view(defaults[connector_id], caller=caller)
    if not user_file.is_file() or user_file.is_symlink():
        return None
    try:
        conn_obj = load_connector(connector_id, config_dir=root)
    except ConnectorError as error:
        raw = user_file.read_bytes()
        rel = f"connectors/{connector_id}.toml"
        ver, cv = _live_meta(connector_id, rel, raw, level="config")
        return {
            "id": connector_id,
            "name": connector_id,
            "service": "",
            "driver": "",
            "account": None,
            "enabled": False,
            "endpoint": None,
            "options": {},
            "secrets": {},
            "level": "config",
            "protected": False,
            "path": rel,
            "version": ver,
            "content_version": cv,
            "status": "invalid",
            "usable": False,
            "problems": [str(error)],
            "account_status": None,
        }
    return connector_view(conn_obj, caller=caller)


def resolve_for_execution(
    connector_id: str | None,
    *,
    service: str,
    caller: str | None = None,
    config_dir: Path | None = None,
) -> tuple[Connector, str | None, dict | None, str]:
    """Resolve an authorized connector for driver execution.

    Returns (connector, secret_value_or_none, secret_row_or_none, account_mode).
    Implicit/default connector ids (None, '', or the service's protected default name) return the
    protected default connector with secret_value=None so existing implicit execution is preserved.
    Any explicit connector id fails closed without falling back to the default or another account.
    """
    if service not in SUPPORTED_SERVICES:
        raise ConnectorError(f"unsupported connector service {service!r}")
    requested = (connector_id or "").strip()
    if not requested or requested == service:
        default_conn = load_connector(service, config_dir=config_dir)
        if not default_conn.enabled:
            raise ConnectorError(f"connector {default_conn.id!r} is disabled")
        return default_conn, None, None, ""

    connector = load_connector(requested, config_dir=config_dir)
    if connector.service != service:
        raise ConnectorError(
            f"connector {connector.id!r} is for service {connector.service!r}, not {service!r}"
        )
    if not connector.enabled:
        raise ConnectorError(f"connector {connector.id!r} is disabled")

    acct_summary, problems = validate_account_binding(connector, caller=caller)
    if problems or acct_summary is None:
        detail = "; ".join(problems) or f"account {connector.account!r} is unusable"
        raise ConnectorError(f"connector {connector.id!r} cannot execute: {detail}")

    mode = str(acct_summary["mode"])
    expected_key = _expected_secret_key(service, mode)
    if expected_key is None:
        return connector, None, None, mode

    ref_str = connector.secrets.get(expected_key) or acct_summary.get("secret_ref")
    if not ref_str:
        raise ConnectorError(f"connector {connector.id!r} has no secret reference")
    scope, name = parse_secret_ref(str(ref_str))
    try:
        sec_row, sec_bytes = secretstore.read_record(scope, name)
    except (
        secretstore.StoreError,
        secretstore.StoreMissing,
        secretstore.FingerprintKeyMissing,
        secretstore.InternalSecret,
        OSError,
        ValueError,
    ) as error:
        raise ConnectorError(f"connector {connector.id!r} secret is unreadable: {type(error).__name__}") from None
    if sec_row is None or sec_bytes is None:
        raise ConnectorError(f"connector {connector.id!r} secret {ref_str} is unavailable")
    try:
        secret_value = sec_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ConnectorError(f"connector {connector.id!r} secret {ref_str} is not valid UTF-8") from None
    return connector, secret_value, sec_row, mode


def jira_client(
    connector_id: str | None = None,
    cfg: config.JiraConfig | None = None,
    *,
    caller: str | None = None,
    config_dir: Path | None = None,
):
    """Build a real jira.Client bound to an explicit connector or the protected default."""
    from . import jira

    connector, secret_value, sec_row, _mode = resolve_for_execution(
        connector_id,
        service="jira",
        caller=caller,
        config_dir=config_dir,
    )
    if connector.protected and secret_value is None:
        jira_cfg = cfg if cfg is not None else config.load().jira
        client = jira.Client.from_env(jira_cfg)
        client.connector_id = connector.id
        return client
    email = connector.options.get("email") or ""
    if not email:
        raise ConnectorError(f"connector {connector.id!r} is missing options.email")
    if not connector.endpoint:
        raise ConnectorError(f"connector {connector.id!r} is missing endpoint")
    client = jira.Client(
        connector.endpoint,
        email,
        secret_value or "",
        connector_id=connector.id,
        account_id=connector.account,
    )
    return client


def github_client(
    connector_id: str | None = None,
    cfg: config.GithubApp | None = None,
    *,
    caller: str | None = None,
    config_dir: Path | None = None,
):
    """Build a real gh.Client bound to an explicit connector or the protected default."""
    from . import gh

    connector, secret_value, _sec_row, mode = resolve_for_execution(
        connector_id,
        service="github",
        caller=caller,
        config_dir=config_dir,
    )
    if connector.protected and secret_value is None:
        app_cfg = cfg if cfg is not None else config.load().github_app
        return gh.Client(
            endpoint=connector.endpoint or gh.GITHUB_API,
            token="",
            connector_id=connector.id,
            account_id=connector.account,
            mode="default",
            app=app_cfg,
        )
    if mode == "app":
        app_cfg = config.GithubApp(
            app_id=connector.options.get("app_id", ""),
            installation_id=connector.options.get("installation_id", ""),
        )
        return gh.Client(
            endpoint=connector.endpoint or gh.GITHUB_API,
            token="",
            connector_id=connector.id,
            account_id=connector.account,
            mode="app",
            app=app_cfg,
            app_key_bytes=(secret_value or "").encode("utf-8"),
        )
    return gh.Client(
        endpoint=connector.endpoint or gh.GITHUB_API,
        token=secret_value or "",
        connector_id=connector.id,
        account_id=connector.account,
        mode=mode,
    )
