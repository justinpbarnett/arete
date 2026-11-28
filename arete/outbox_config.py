from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import configkinds, configschema, paths, senders

PROTECTED_TARGETS = frozenset(("github",))
PROTECTED_RULES = frozenset(("github-own",))
IMPLICIT_CONNECTORS = frozenset(("github",))

_NAME_RE = re.compile(rf"^{configkinds.NAME_PATTERN}$")

ConnectorValidator = Callable[[str, dict[str, object]], None]
CONNECTOR_VALIDATORS: list[ConnectorValidator] = []


@dataclass(frozen=True)
class OutboxTarget:
    name: str
    connector: str
    kinds: tuple[str, ...]
    audience: str
    posts_as: str
    send_window: int
    level: str
    content_version: str


@dataclass(frozen=True)
class OutboxRule:
    name: str
    domain: str
    target: str
    audience: str
    drafter: str
    action: str
    kinds: tuple[str, ...] = ()
    enabled: bool = True
    level: str = "engine"
    content_version: str = ""


def register_connector_validator(validator: ConnectorValidator) -> None:
    if validator not in CONNECTOR_VALIDATORS:
        CONNECTOR_VALIDATORS.append(validator)


def unregister_connector_validator(validator: ConnectorValidator) -> None:
    if validator in CONNECTOR_VALIDATORS:
        CONNECTOR_VALIDATORS.remove(validator)


def _content_version(kind: str, name: str, raw: bytes) -> str:
    return f"{kind}/{name}@{hashlib.sha256(raw).hexdigest()[:12]}"


def _safe_name(kind: str, name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid {kind} name: {name!r}")
    return name


def _known_connectors() -> frozenset[str]:
    return IMPLICIT_CONNECTORS | senders.known_connectors()


def validate_target(name: str, data: dict[str, object], *, where: str | None = None) -> dict[str, object]:
    _safe_name("target", name)
    label = where or f"target/{name}"
    configschema.validate("target", data, where=label)
    connector = str(data["connector"])
    known = _known_connectors()
    if not CONNECTOR_VALIDATORS and connector not in known:
        raise ValueError(f"{label}.connector must be a supported connector ({', '.join(sorted(known))})")
    supported = senders.supported_kinds(connector)
    if supported:
        for kind in data["kinds"]:
            if kind not in supported:
                raise ValueError(f"{label}.kinds: unsupported kind {kind!r} for connector {connector!r}")
    for validator in tuple(CONNECTOR_VALIDATORS):
        validator(name, data)
    return dict(data)


def normalize_outbox_rule(data: object, *, where: str = "rule") -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError(f"{where} must be object")
    if "domain" not in data:
        raise ValueError(f"{where}.domain must be one of outbox, intake")
    normalized = dict(data)
    if normalized.get("domain") == "outbox" and "enabled" not in normalized:
        normalized["enabled"] = True
    return normalized


def validate_rule(
    name: str,
    data: object,
    *,
    targets: dict[str, OutboxTarget] | None = None,
    root: Path | None = None,
    where: str | None = None,
) -> dict[str, object]:
    _safe_name("rule", name)
    label = where or f"rule/{name}"
    normalized = normalize_outbox_rule(data, where=label)
    configschema.validate("rule", normalized, where=label)
    domain = str(normalized["domain"])
    if domain != "outbox":
        raise ValueError(f"{label}: {domain} rules are unavailable")
    action = str(normalized["action"])
    audience = str(normalized["audience"])
    drafter = str(normalized["drafter"])
    if action == "approve":
        if audience != "self":
            raise ValueError(f"{label}: outbox rules may only auto-approve self-audience items")
        if drafter in ("person", "any"):
            raise ValueError(f"{label}: outbox rules may not auto-approve person-drafted items")
    target_map = load_targets(root) if targets is None else targets
    target_name = str(normalized["target"])
    target = target_map.get(target_name)
    if target is None:
        raise ValueError(f"{label}.target references unknown target {target_name!r}")
    rule_kinds = normalized.get("kinds") or ()
    if rule_kinds:
        missing = [k for k in rule_kinds if k not in target.kinds]
        if missing:
            raise ValueError(
                f"{label}.kinds contains kinds not enabled on target {target_name!r}: {', '.join(missing)}"
            )
    return normalized


def _parse_toml_file(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{label} is not valid TOML: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a TOML table")
    return parsed, raw


def _target_from_data(name: str, data: dict[str, object], *, level: str, raw: bytes, where: str) -> OutboxTarget:
    checked = validate_target(name, data, where=where)
    return OutboxTarget(
        name=name,
        connector=str(checked["connector"]),
        kinds=tuple(str(k) for k in checked["kinds"]),
        audience=str(checked["audience"]),
        posts_as=str(checked["posts_as"]),
        send_window=int(checked["send_window"]),
        level=level,
        content_version=_content_version("target", name, raw),
    )


def _rule_from_data(
    name: str,
    data: dict[str, object],
    *,
    targets: dict[str, OutboxTarget],
    level: str,
    raw: bytes,
    where: str,
) -> OutboxRule:
    checked = validate_rule(name, data, targets=targets, where=where)
    return OutboxRule(
        name=name,
        domain=str(checked["domain"]),
        target=str(checked["target"]),
        audience=str(checked["audience"]),
        drafter=str(checked["drafter"]),
        action=str(checked["action"]),
        kinds=tuple(str(k) for k in (checked.get("kinds") or ())),
        enabled=bool(checked.get("enabled", True)),
        level=level,
        content_version=_content_version("rule", name, raw),
    )


def load_targets(root: Path | None = None) -> dict[str, OutboxTarget]:
    found: dict[str, OutboxTarget] = {}
    defaults_dir = paths.DEFAULTS_DIR / "targets"
    if defaults_dir.is_dir():
        for file in sorted(defaults_dir.glob("*.toml")):
            name = file.stem
            data, raw = _parse_toml_file(file, f"target/{name}")
            found[name] = _target_from_data(name, data, level="engine", raw=raw, where=f"target/{name}")
    cfg_root = (root or paths.CONFIG_DIR).resolve()
    user_dir = cfg_root / "targets"
    if user_dir.is_dir():
        for file in sorted(user_dir.glob("*.toml")):
            if not file.is_file():
                continue
            name = file.stem
            data, raw = _parse_toml_file(file, f"targets/{file.name}")
            found[name] = _target_from_data(name, data, level="config", raw=raw, where=f"targets/{file.name}")
    return found


def load_rules(root: Path | None = None, *, targets: dict[str, OutboxTarget] | None = None) -> dict[str, OutboxRule]:
    target_map = load_targets(root) if targets is None else targets
    found: dict[str, OutboxRule] = {}
    defaults_dir = paths.DEFAULTS_DIR / "rules"
    if defaults_dir.is_dir():
        for file in sorted(defaults_dir.glob("*.toml")):
            name = file.stem
            data, raw = _parse_toml_file(file, f"rule/{name}")
            if data.get("domain") != "outbox":
                continue
            found[name] = _rule_from_data(name, data, targets=target_map, level="engine", raw=raw, where=f"rule/{name}")
    cfg_root = (root or paths.CONFIG_DIR).resolve()
    user_dir = cfg_root / "rules"
    if user_dir.is_dir():
        for file in sorted(user_dir.glob("*.toml")):
            if not file.is_file():
                continue
            name = file.stem
            data, raw = _parse_toml_file(file, f"rules/{file.name}")
            if data.get("domain") != "outbox":
                continue
            found[name] = _rule_from_data(
                name, data, targets=target_map, level="config", raw=raw, where=f"rules/{file.name}"
            )
    return found


def target_for(name: str, root: Path | None = None) -> OutboxTarget:
    targets = load_targets(root)
    target = targets.get(name)
    if target is None:
        raise ValueError(f"unknown outbox target {name!r}")
    return target


def _drafter_matches(rule_drafter: str, *, drafted_by: str, person_drafted: bool) -> bool:
    if rule_drafter == "any":
        return True
    if rule_drafter == "person":
        return person_drafted
    if rule_drafter == "non-person":
        return not person_drafted
    if rule_drafter == "agent":
        return not person_drafted and (drafted_by.startswith(("run:", "loop:", "agent")) or drafted_by == "arete")
    if rule_drafter == "system":
        return not person_drafted
    return False


def match_auto_approve(
    target: str,
    kind: str,
    audience: str,
    *,
    drafted_by: str,
    person_drafted: bool,
    root: Path | None = None,
) -> OutboxRule | None:
    if audience != "self" or person_drafted:
        return None
    targets = load_targets(root)
    if target not in targets:
        return None
    rules = load_rules(root, targets=targets)
    for name in sorted(rules):
        rule = rules[name]
        if not rule.enabled or rule.domain != "outbox" or rule.action != "approve":
            continue
        if rule.target != target or rule.audience != audience:
            continue
        if rule.kinds and kind not in rule.kinds:
            continue
        if _drafter_matches(rule.drafter, drafted_by=drafted_by, person_drafted=person_drafted):
            return rule
    return None
