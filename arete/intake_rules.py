from __future__ import annotations

import hashlib
import json
import re
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from . import config, configkinds, configrepo, configschema, loops, paths, prompts, sources
from .store import config as config_store
from .store import db as foundation

CANDIDATE_KINDS = frozenset(("feat", "fix", "chore", "research", "bug", "task", "decision", "action_item"))
CANDIDATE_ACTIONS = frozenset(("create", "link", "ignore"))
BUILTIN_SOURCES = frozenset(("intake",))
DEFAULT_PROMPT = "intake_extract"
DEFAULT_SCHEMA = "intake_extract"

_NAME_RE = re.compile(rf"^{configkinds.NAME_PATTERN}$")


@dataclass(frozen=True)
class IntakeRule:
    name: str
    domain: str
    enabled: bool = True
    priority: int = 100
    sources: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    match: dict[str, object] = field(default_factory=dict)
    prompt: str = DEFAULT_PROMPT
    schema: str = DEFAULT_SCHEMA
    guidance: str = ""
    candidate_kinds: tuple[str, ...] = ()
    candidate_actions: tuple[str, ...] = ()
    defaults: dict[str, object] = field(default_factory=dict)
    task_permissions: dict[str, object] = field(default_factory=dict)
    level: str = "config"
    content_version: str = ""


def _content_version(name: str, raw: bytes) -> str:
    return f"rule/{name}@{hashlib.sha256(raw).hexdigest()[:12]}"


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid rule name: {name!r}")
    return name


def _known_sources(config_dir: Path, extra_sources: set[str] | None = None) -> set[str]:
    known = set(BUILTIN_SOURCES)
    if extra_sources:
        known.update(extra_sources)
    for folder in (paths.DEFAULTS_DIR / "sources", config_dir / "sources"):
        if folder.is_dir():
            for file in folder.glob("*.toml"):
                if file.is_file():
                    known.add(file.stem)
    cfg, problem = config.load_or_problem()
    if not problem:
        with suppress(Exception):
            all_loops = loops.load_all(cfg, check=False)
            for src in sources.load_all(cfg, all_loops, config_dir=config_dir, include_derived=True):
                known.add(src.id)
    return known


def _known_prompts(config_dir: Path, extra_prompts: set[str] | None = None) -> set[str]:
    known = set(prompts.engine_prompt_names())
    if extra_prompts:
        known.update(extra_prompts)
    prompt_dir = config_dir / "prompts"
    if prompt_dir.is_dir():
        for file in prompt_dir.glob("*.md"):
            if file.is_file():
                known.add(file.stem)
    return known


def _known_schemas(config_dir: Path, extra_schemas: set[str] | None = None) -> set[str]:
    known = set(prompts.engine_schema_names())
    if extra_schemas:
        known.update(extra_schemas)
    schema_dir = config_dir / "schemas"
    if schema_dir.is_dir():
        for file in schema_dir.glob("*.json"):
            if file.is_file():
                known.add(file.stem)
    return known


def normalize_intake_rule(data: object, *, where: str = "rule") -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError(f"{where} must be object")
    if data.get("domain") != "intake":
        raise ValueError(f"{where}.domain must be 'intake'")
    normalized = dict(data)
    if "enabled" not in normalized:
        normalized["enabled"] = True
    return normalized


def validate_rule(
    name: str,
    data: object,
    *,
    config_dir: Path | None = None,
    extra_sources: set[str] | None = None,
    extra_prompts: set[str] | None = None,
    extra_schemas: set[str] | None = None,
    where: str | None = None,
) -> dict[str, object]:
    _safe_name(name)
    label = where or f"rule/{name}"
    normalized = normalize_intake_rule(data, where=label)
    configschema.validate("rule", normalized, where=label)

    if not normalized.get("sources") and not normalized.get("kinds"):
        raise ValueError(f"{label}: intake rule must specify at least one of sources or kinds")

    root = (config_dir or paths.CONFIG_DIR).resolve()
    known_sources = _known_sources(root, extra_sources=extra_sources)
    for sid in normalized.get("sources") or ():
        if str(sid) not in known_sources:
            raise ValueError(f"{label}.sources references unknown source {sid!r}")

    prompt_name = str(normalized.get("prompt") or DEFAULT_PROMPT)
    known_prompts = _known_prompts(root, extra_prompts=extra_prompts)
    if prompt_name not in known_prompts:
        raise ValueError(f"{label}.prompt references unknown prompt {prompt_name!r}")

    schema_name = str(normalized.get("schema") or DEFAULT_SCHEMA)
    known_schemas = _known_schemas(root, extra_schemas=extra_schemas)
    if schema_name not in known_schemas:
        raise ValueError(f"{label}.schema references unknown schema {schema_name!r}")

    match_cfg = normalized.get("match")
    if isinstance(match_cfg, dict) and isinstance(match_cfg.get("title_regex"), str):
        try:
            re.compile(match_cfg["title_regex"])
        except re.error as err:
            raise ValueError(f"{label}.match.title_regex is not a valid regex: {err}") from err

    allowed_kinds = tuple(str(k) for k in (normalized.get("candidate_kinds") or ()))
    allowed_actions = tuple(str(a) for a in (normalized.get("candidate_actions") or ()))
    defaults = normalized.get("defaults") if isinstance(normalized.get("defaults"), dict) else {}
    perms = normalized.get("task_permissions") if isinstance(normalized.get("task_permissions"), dict) else {}

    if defaults.get("kind") and allowed_kinds and str(defaults["kind"]) not in allowed_kinds:
        raise ValueError(f"{label}.defaults.kind {defaults['kind']!r} is not in candidate_kinds")
    if defaults.get("action") and allowed_actions and str(defaults["action"]) not in allowed_actions:
        raise ValueError(f"{label}.defaults.action {defaults['action']!r} is not in candidate_actions")

    allow_create = perms.get("allow_create", True)
    allow_link = perms.get("allow_link", True)
    if allow_create is False:
        if defaults.get("action") == "create":
            raise ValueError(f"{label}.defaults.action cannot be 'create' when task_permissions.allow_create is false")
        if allowed_actions and allowed_actions == ("create",):
            raise ValueError(f"{label}.candidate_actions permits only 'create' while task_permissions.allow_create is false")
    if allow_link is False and defaults.get("action") == "link":
        raise ValueError(f"{label}.defaults.action cannot be 'link' when task_permissions.allow_link is false")

    perm_repos = {str(r) for r in (perms.get("repos") or ())}
    if defaults.get("repo") and perm_repos and str(defaults["repo"]) not in perm_repos:
        raise ValueError(f"{label}.defaults.repo {defaults['repo']!r} is not allowed by task_permissions.repos")
    perm_projects = {str(p) for p in (perms.get("projects") or ())}
    if defaults.get("project") and perm_projects and str(defaults["project"]) not in perm_projects:
        raise ValueError(f"{label}.defaults.project {defaults['project']!r} is not allowed by task_permissions.projects")
    perm_lines = {str(ln) for ln in (perms.get("lines") or ())}
    if defaults.get("line") and perm_lines and str(defaults["line"]) not in perm_lines:
        raise ValueError(f"{label}.defaults.line {defaults['line']!r} is not allowed by task_permissions.lines")

    cfg, problem = config.load_or_problem()
    if not problem and cfg.repos:
        cfg_repo_names = {r.name for r in cfg.repos}
        for rname in perm_repos:
            if rname not in cfg_repo_names:
                raise ValueError(f"{label}.task_permissions.repos references unknown repo {rname!r}")
        if defaults.get("repo") and str(defaults["repo"]) not in cfg_repo_names:
            raise ValueError(f"{label}.defaults.repo references unknown repo {defaults['repo']!r}")

    return normalized


def _parse_rule_bytes(name: str, raw: bytes, *, level: str, config_dir: Path, where: str) -> IntakeRule:
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{where} is not valid TOML: {error}") from error
    checked = validate_rule(name, parsed, config_dir=config_dir, where=where)
    return IntakeRule(
        name=name,
        domain="intake",
        enabled=bool(checked.get("enabled", True)),
        priority=int(checked.get("priority", 100)),
        sources=tuple(str(s) for s in (checked.get("sources") or ())),
        kinds=tuple(str(k) for k in (checked.get("kinds") or ())),
        match=dict(checked.get("match") or {}),
        prompt=str(checked.get("prompt") or DEFAULT_PROMPT),
        schema=str(checked.get("schema") or DEFAULT_SCHEMA),
        guidance=str(checked.get("guidance") or ""),
        candidate_kinds=tuple(str(k) for k in (checked.get("candidate_kinds") or ())),
        candidate_actions=tuple(str(a) for a in (checked.get("candidate_actions") or ())),
        defaults=dict(checked.get("defaults") or {}),
        task_permissions=dict(checked.get("task_permissions") or {}),
        level=level,
        content_version=_content_version(name, raw),
    )


def load_rules(root: Path | None = None) -> dict[str, IntakeRule]:
    cfg_root = (root or paths.CONFIG_DIR).resolve()
    found: dict[str, IntakeRule] = {}
    for folder, level in ((paths.DEFAULTS_DIR / "rules", "engine"), (cfg_root / "rules", "config")):
        if not folder.is_dir():
            continue
        for file in sorted(folder.glob("*.toml")):
            if not file.is_file():
                continue
            raw = file.read_bytes()
            try:
                parsed = tomllib.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                continue
            if not isinstance(parsed, dict) or parsed.get("domain") != "intake":
                continue
            name = file.stem
            found[name] = _parse_rule_bytes(
                name, raw, level=level, config_dir=cfg_root, where=f"rules/{file.name}"
            )
    return found


def _rule_matches(rule: IntakeRule, item: dict) -> bool:
    if not rule.enabled:
        return False
    if rule.sources and str(item.get("source") or "") not in rule.sources:
        return False
    if rule.kinds and str(item.get("kind") or "") not in rule.kinds:
        return False
    match_cfg = rule.match
    if match_cfg:
        title = str(item.get("title") or "")
        if "title_contains" in match_cfg and str(match_cfg["title_contains"]).lower() not in title.lower():
            return False
        if "title_regex" in match_cfg and not re.search(str(match_cfg["title_regex"]), title):
            return False
        if "author" in match_cfg and str(item.get("author") or "") != str(match_cfg["author"]):
            return False
        if "trust" in match_cfg and str(item.get("trust") or "untrusted") not in match_cfg["trust"]:
            return False
    return True


def match_rule(item: dict, *, root: Path | None = None) -> IntakeRule | None:
    rules = load_rules(root)
    ordered = sorted(rules.values(), key=lambda r: (r.priority, r.name))
    for rule in ordered:
        if _rule_matches(rule, item):
            return rule
    return None


def load_pinned_rule(rule_name: str, rule_version: str, *, root: Path | None = None) -> IntakeRule:
    """Resolve the exact pinned rule version without silently substituting the live rule."""
    cfg_root = (root or paths.CONFIG_DIR).resolve()
    live = load_rules(cfg_root).get(rule_name)
    if live is not None and live.content_version == rule_version:
        return live
    rel = f"rules/{rule_name}.toml"
    if configrepo.is_repo(cfg_root):
        try:
            with foundation.reader() as c:
                versions = config_store.versions("rule", rule_name, c)
        except (foundation.NoDatabase, foundation.IncompatibleSchema):
            versions = []
        for entry in versions:
            if entry.get("version") == rule_version and entry.get("commit"):
                raw = configrepo.blob_at(str(entry["commit"]), rel, root=cfg_root)
                if raw is not None and _content_version(rule_name, raw) == rule_version:
                    return _parse_rule_bytes(
                        rule_name, raw, level="config", config_dir=cfg_root, where=f"{rel}@{rule_version}"
                    )
    raise ValueError(f"pinned intake rule {rule_name!r} version {rule_version!r} is unavailable")


def resolve_prompt_and_schema(
    rule: IntakeRule | None,
    *,
    config_dir: Path | None = None,
) -> tuple[prompts.ResolvedPrompt, prompts.LoadedSchema]:
    """Resolve extraction prompt and output schema strictly (fail closed on missing refs)."""
    root = (config_dir or paths.CONFIG_DIR).resolve()
    prompt_name = rule.prompt if rule is not None else DEFAULT_PROMPT
    schema_name = rule.schema if rule is not None else DEFAULT_SCHEMA

    cfg_prompt = root / "prompts" / f"{prompt_name}.md"
    eng_prompt = paths.PROMPTS_DIR / f"{prompt_name}.md"
    if cfg_prompt.is_file():
        raw = cfg_prompt.read_bytes()
        _, body_text = prompts.parse(raw)
        sha = hashlib.sha256(raw).hexdigest()
        resolved_prompt = prompts.ResolvedPrompt(
            call="intake_extract",
            role="intake_extract",
            name=prompt_name,
            version=f"prompt/{prompt_name}@{sha[:12]}",
            level="config",
            sha256=sha,
            text=body_text,
            source=f"prompts/{prompt_name}.md",
        )
    elif eng_prompt.is_file():
        raw = eng_prompt.read_bytes()
        _, body_text = prompts.parse(raw)
        sha = hashlib.sha256(raw).hexdigest()
        resolved_prompt = prompts.ResolvedPrompt(
            call="intake_extract",
            role="intake_extract",
            name=prompt_name,
            version=f"prompt/{prompt_name}@{sha[:12]}",
            level="engine",
            sha256=sha,
            text=body_text,
            source=f"arete/prompts/{prompt_name}.md",
        )
    else:
        raise ValueError(f"intake_extract prompt {prompt_name!r} does not exist")

    cfg_schema = root / "schemas" / f"{schema_name}.json"
    eng_schema = paths.SCHEMAS_DIR / f"{schema_name}.json"
    if cfg_schema.is_file():
        raw_s = cfg_schema.read_bytes()
        data_s = json.loads(raw_s.decode("utf-8"))
        sha_s = hashlib.sha256(raw_s).hexdigest()
        resolved_schema = prompts.LoadedSchema(
            data_s,
            name=schema_name,
            version=f"schema/{schema_name}@{sha_s[:12]}",
            level="config",
            sha256=sha_s,
        )
    elif eng_schema.is_file():
        resolved_schema = prompts.engine_schema(schema_name)
    else:
        raise ValueError(f"intake_extract schema {schema_name!r} does not exist")

    return resolved_prompt, resolved_schema


def apply_rule_to_candidates(
    raw_candidates: list[dict],
    raw_text: str,
    rule: IntakeRule | None,
) -> list[dict]:
    """Validate extracted candidates against quote bounds and apply pinned rule defaults/permissions."""
    allowed_kinds = set(rule.candidate_kinds) if rule and rule.candidate_kinds else set(CANDIDATE_KINDS)
    allowed_actions = set(rule.candidate_actions) if rule and rule.candidate_actions else set(CANDIDATE_ACTIONS)
    defaults = dict(rule.defaults) if rule and rule.defaults else {}
    perms = dict(rule.task_permissions) if rule and rule.task_permissions else {}
    allow_create = perms.get("allow_create", True) is not False
    allow_link = perms.get("allow_link", True) is not False
    max_len = len(raw_text)

    out: list[dict] = []
    for raw_c in raw_candidates:
        if not isinstance(raw_c, dict):
            continue
        title = str(raw_c.get("title") or "").strip()
        if not title:
            continue
        kind = str(raw_c.get("kind") or defaults.get("kind") or "feat").strip()
        if kind not in allowed_kinds:
            kind = str(defaults.get("kind") or next(iter(sorted(allowed_kinds))))
        action = str(raw_c.get("action") or defaults.get("action") or "create").strip()
        if action not in allowed_actions:
            action = str(defaults.get("action") or "ignore")
        if action == "create" and not allow_create:
            action = "link" if ("link" in allowed_actions and allow_link) else "ignore"
        if action == "link" and not allow_link:
            action = "ignore"

        q_start = raw_c.get("quote_start")
        q_end = raw_c.get("quote_end")
        if isinstance(q_start, bool) or not isinstance(q_start, int):
            q_start = None
        if isinstance(q_end, bool) or not isinstance(q_end, int):
            q_end = None
        if q_start is not None and q_end is not None:
            if q_start < 0 or q_end < q_start or q_end > max_len:
                q_start, q_end = None, None
        else:
            q_start, q_end = None, None

        cand_fields = dict(raw_c.get("fields") or {}) if isinstance(raw_c.get("fields"), dict) else {}
        for k, v in defaults.items():
            if k in ("kind", "action"):
                continue
            if k not in cand_fields or cand_fields[k] in (None, "", []):
                cand_fields[k] = list(v) if isinstance(v, (list, tuple)) else v

        match_task = str(raw_c["match_task"]).strip() if isinstance(raw_c.get("match_task"), str) and raw_c["match_task"].strip() else None
        raw_conf = raw_c.get("match_confidence")
        match_conf = float(raw_conf) if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool) else None
        if match_conf is not None:
            match_conf = max(0.0, min(1.0, match_conf))

        out.append({
            "kind": kind,
            "action": action,
            "title": title,
            "quote_start": q_start,
            "quote_end": q_end,
            "fields": cand_fields,
            "match_task": match_task,
            "match_confidence": match_conf,
        })
    return out
