from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from . import codebase, codex, config, configschema, paths, routing
from .agent import READ_ONLY_TOOLS, REPLAY, TRAITS, WEB_TOOLS, traits_of

MODEL_SOURCE = ("runner", "provider")
STEP_TOOLS = ("Read", "Glob", "Grep", "LS", "Edit", "MultiEdit", "Write", "TodoWrite")
LEVELS = ("inherit", "engine", "config", "config kind", "layer", "layer kind", "arm", "run")
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}


@cache
def engine_defaults() -> tuple[dict, dict[str, dict]]:
    path = paths.DEFAULTS_DIR / "steps.toml"
    data = tomllib.loads(path.read_text())
    configschema.validate("step_settings", data, where=str(path))
    return config.step_table(data["defaults"], "defaults"), config.step_tables(data["steps"], "steps")


@dataclass(frozen=True)
class Settings:
    step: str
    tier: str
    runner: str
    provider: str
    model: str
    effort: str
    timeout_minutes: int
    tries: int
    tools: tuple[str, ...]
    mcp: tuple[str, ...]
    instructions: tuple[str, ...]
    session: str
    notes: tuple[str, ...] = ()
    chain: tuple[routing.Link, ...] = ()
    fallback: bool = True
    instructions_explicit: bool = False
    requires: routing.Requirements = field(default_factory=routing.Requirements)
    denied: tuple[str, ...] = ()
    inherits: str = ""
    budget_usd: float | None = None
    explicit_route: bool = False
    ab_name: str = ""
    ab_side: str = ""
    ab_arm: str = ""
    prompts: dict[str, str] = field(default_factory=dict)
    schemas: dict[str, str] = field(default_factory=dict)

    def effort_on(self, which: str) -> str:
        return routing.nearest_effort(which, self.effort)

    def instructions_on(self, which: str) -> tuple[str, ...]:
        return self.instructions if self.instructions_explicit else traits_of(which).default_instructions

    @property
    def call(self) -> dict:
        return {"model": self.model, "runner": self.runner, "effort": self.effort_on(self.runner),
                "timeout": self.timeout_minutes * 60,
                **({"max_budget_usd": self.budget_usd} if self.budget_usd else {})}

    def agent_kw(self, cfg: config.Config, rules=(), own_mcp: str = "") -> dict:
        return {"allowed_tools": [*self.tools, *rules, *(f"mcp__{n}" for n in self.mcp)],
                "mcp_config": mcp_config(cfg, self.mcp, own_mcp), "instructions": list(self.instructions)}


def mcp_config(cfg: config.Config, names, own: str = "") -> str:
    if not names:
        return own
    own_servers = json.loads(own)["mcpServers"] if own else {}
    return json.dumps({"mcpServers": {**{n: cfg.mcp[n] for n in names}, **own_servers}})


def selected_tiers(cfg: config.Config, layer: config.RepoLayer) -> dict[str, tuple[dict[str, str], ...]]:
    try:
        return tier_map(cfg, layer)[1]
    except ValueError:
        return {}


def holds_model(entries: tuple[dict[str, str], ...], wanted: tuple[str, str]) -> bool:
    return any(e.get("model") and routing.identity(*routing.source_route(e)) == wanted for e in entries)


def tier_of(cfg: config.Config, layer: config.RepoLayer, named: str, wanted: tuple[str, str]) -> tuple[str, tuple]:
    tiers = selected_tiers(cfg, layer)
    if named in tiers and holds_model(tiers[named], wanted):
        return named, tiers[named]
    return next(((n, e) for n, e in tiers.items() if holds_model(e, wanted)), ("", ()))


def chain_of(requested: tuple[str, str, str], tier: str, entries: tuple[dict[str, str], ...], effort: str,
             explicit: tuple[str, ...] | None) -> tuple[routing.Link, ...]:
    def link(which: str, name: str, provider: str, position: int) -> routing.Link:
        return routing.Link(name, which, provider, tier, position, position == 0, routing.nearest_effort(which, effort),
                            traits_of(which).default_instructions if explicit is None else explicit)

    wanted = routing.identity(*requested)
    openrouter = routing.claude_openrouter(requested[0], requested[2])
    chain = [link(*requested, 0)]
    for entry in entries:
        which, name, provider = routing.source_route(entry)
        same_route = (routing.identity(which, name, provider) == wanted
                      and routing.claude_openrouter(which, provider) == openrouter)
        if entry.get("model") and not same_route:
            chain.append(link(which, name, provider, len(chain)))
    return tuple(chain)


def chain_for(cfg: config.Config, layer: config.RepoLayer | None, which: str, name: str, effort: str,
              explicit: tuple[str, ...] | None) -> tuple[tuple[routing.Link, ...], bool]:
    layer = layer or config.RepoLayer(dir=Path())
    tier, entries = tier_of(cfg, layer, "", routing.identity(which, name))
    requested = routing.source_route({"model": name, "runner": which})
    return chain_of(requested, tier, entries, effort, explicit), config.routing_for(cfg, layer).fallback


def tier_map(cfg: config.Config, layer: config.RepoLayer | None = None) -> tuple[str, dict[str, tuple[dict[str, str], ...]]]:
    own = layer.tiers if layer else {}
    maps = {name: {**cfg.tiers.get(name, {}), **own.get(name, {})} for name in dict.fromkeys([*cfg.tiers, *own])}
    if picked := (layer.tier_map if layer else "") or cfg.tier_map:
        if picked not in maps:
            raise ValueError(f"[factory] tier_map '{picked}' names no [tiers.{picked}]")
        return picked, maps[picked]
    if len(maps) > 1:
        raise ValueError(f"several tier maps ({', '.join(maps)}); pick one with [factory] tier_map")
    return next(iter(maps.items()), ("", {}))


def experiments(cfg: config.Config, layer: config.RepoLayer | None) -> dict[str, dict]:
    return {**cfg.ab, **(layer.ab if layer else {})}


def experiment_for(cfg: config.Config, layer: config.RepoLayer | None, role: str,
                   kind: str) -> tuple[str, dict] | None:
    return next(((name, test) for name, test in experiments(cfg, layer).items()
                 if test["step"] == role and test["kind"] in ("", kind)), None)


def in_arm(name: str, share: float, ticket: tuple[str, str]) -> bool:
    digest = hashlib.sha256("\0".join((name, *ticket)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < share


def ab_view(cfg: config.Config, layer: config.RepoLayer | None, role: str, kind: str) -> dict | None:
    found = experiment_for(cfg, layer, role, kind)
    if not found:
        return None
    name, test = found
    return {"name": name, "share": test["share"], "kind": test["kind"], "arm": test["arm"]}


def _json_value(value):
    if isinstance(value, tuple):
        return list(value)
    return value


def _merge_map(settings: dict, prov: dict[str, dict], key: str, values: dict[str, str],
               level_name: str, source_name: str) -> None:
    settings[key] = {**settings.get(key, {}), **values}
    merged_prov = dict(prov.get(key, {}))
    for call_name, file_name in values.items():
        merged_prov[call_name] = {"value": file_name, "level": level_name, "from": source_name}
    prov[key] = merged_prov


def merged_provenance(cfg: config.Config, layer: config.RepoLayer, role: str, kind: str, overrides: dict,
                      inherited: dict | None = None, arm: dict | None = None, *,
                      config_commit: str = "", layer_from: str = "", arm_from: str = "",
                      overrides_from: str = "--set", engine_sha: str = "") -> tuple[dict, dict[str, dict]]:
    defaults, built_in = engine_defaults()
    engine_ref = engine_sha or codebase.STAMP or "engine"
    engine_source = f"arete/defaults/steps.toml@{engine_ref}"
    cfg_source = f"config.toml@{config_commit}" if config_commit else "config.toml"
    lay_source = layer_from or (f"{layer.dir.name}/config.toml@{config_commit}" if config_commit else "layer/config.toml")
    base = config.INHERITS.get(role, "")
    prov: dict[str, dict] = {}
    if base:
        base_settings, base_prov = merged_provenance(
            cfg, layer, base, kind, inherited or {}, None, None,
            config_commit=config_commit, layer_from=layer_from, overrides_from=overrides_from,
            engine_sha=engine_sha,
        )
        settings, own_route = dict(base_settings), False
        for k in config.STEP_MAPS:
            if k in settings:
                settings[k] = dict(settings[k])
        for k, entry in base_prov.items():
            if k in config.STEP_MAPS:
                prov[k] = {ck: {"value": cv["value"], "level": "inherit", "from": cv["from"]}
                           for ck, cv in entry.items()}
            else:
                prov[k] = {"value": entry["value"], "level": "inherit", "from": entry["from"]}
        for k, v in built_in.get(role, {}).items():
            if k in config.STEP_MAPS:
                _merge_map(settings, prov, k, v, "engine", engine_source)
            else:
                settings[k] = v
                prov[k] = {"value": _json_value(v), "level": "engine", "from": engine_source}
    else:
        limits_set = getattr(layer, "_limits_set", frozenset())
        settings, own_route = {"timeout_minutes": layer.timeout_minutes, "tries": layer.max_attempts}, True
        prov["timeout_minutes"] = {
            "value": layer.timeout_minutes,
            "level": "layer" if "timeout_minutes" in limits_set else "engine",
            "from": lay_source if "timeout_minutes" in limits_set else engine_source,
        }
        prov["tries"] = {
            "value": layer.max_attempts,
            "level": "layer" if "max_attempts" in limits_set else "engine",
            "from": lay_source if "max_attempts" in limits_set else engine_source,
        }
        for table in (defaults, built_in.get(role, {})):
            for k, v in table.items():
                if k in config.STEP_MAPS:
                    _merge_map(settings, prov, k, v, "engine", engine_source)
                else:
                    settings[k] = v
                    prov[k] = {"value": _json_value(v), "level": "engine", "from": engine_source}
    explicit_runner = False
    layers = (
        (cfg.steps.get(role), "config", cfg_source),
        (cfg.kinds.get(kind, {}).get(role), "config kind", cfg_source),
        (layer.steps.get(role), "layer", lay_source),
        (layer.kinds.get(kind, {}).get(role), "layer kind", lay_source),
        (arm, "arm", arm_from or "arm:default"),
        (overrides, "run", overrides_from),
    )
    for table, level_name, source_name in layers:
        explicit_runner = explicit_runner or bool(table and "runner" in table)
        if table and "model" in table:
            settings = {k: v for k, v in settings.items() if k not in MODEL_SOURCE}
            for k in MODEL_SOURCE:
                prov.pop(k, None)
            own_route = True
        elif table and "tier" in table and not own_route:
            settings = {k: v for k, v in settings.items() if k not in (*MODEL_SOURCE, "model")}
            for k in (*MODEL_SOURCE, "model"):
                prov.pop(k, None)
            own_route = True
        if table:
            for k, v in table.items():
                if k in config.STEP_MAPS:
                    if level_name in ("layer", "layer kind"):
                        continue
                    _merge_map(settings, prov, k, v, level_name, source_name)
                else:
                    settings[k] = v
                    prov[k] = {"value": _json_value(v), "level": level_name, "from": source_name}
    if base:
        settings["explicit_route"] = own_route or explicit_runner
    return settings, prov


def merged(cfg: config.Config, layer: config.RepoLayer, role: str, kind: str, overrides: dict,
           inherited: dict | None = None, arm: dict | None = None) -> dict:
    return merged_provenance(cfg, layer, role, kind, overrides, inherited, arm)[0]


def role_level(resolved: Settings, prov: dict[str, dict] | None) -> str | None:
    if not prov:
        return None
    winning: list[str] = []
    for key, val in (("runner", resolved.runner), ("model", resolved.model), ("effort", resolved.effort)):
        if not val:
            continue
        entry = prov.get(key)
        level = entry.get("level") if isinstance(entry, dict) else None
        if level in LEVEL_RANK:
            winning.append(level)
    return max(winning, key=LEVEL_RANK.__getitem__) if winning else None


def check_reach(where: str, cfg: config.Config, role_info: config.Role, which: str, found: dict,
                instructions: tuple) -> None:
    role = role_info.name
    tools, mcp = found["tools"], found["mcp"]
    traits = traits_of(which)
    web = WEB_TOOLS if role_info.web == "search" else ("WebFetch",) if role_info.web == "fetch" else ()
    if role_info.web == "fetch" and "WebSearch" in tools:
        raise ValueError(f"{where}: tools WebSearch cannot load on {role}, which may only fetch again the sources "
                         "it audits")
    if role_info.web == "none" and (found_web := [t for t in tools if t in WEB_TOOLS]):
        raise ValueError(f"{where}: tools {', '.join(found_web)} cannot load on {role}, which writes only from the "
                         "evidence it is given")
    if role_info.read_only:
        if acting := [t for t in tools if t not in READ_ONLY_TOOLS and t not in web]:
            raise ValueError(f"{where}: tools {', '.join(acting)} would let {role} change files; it is read only, "
                             "so use Read, Glob, Grep and LS")
        if mcp:
            raise ValueError(f"{where}: mcp {', '.join(mcp)} cannot load on {role}, which is read only and takes "
                             "no servers")
    if bad := [t for t in tools if t not in STEP_TOOLS and t not in web]:
        raise ValueError(f"{where}: tools {', '.join(bad)} is not a built in tool a step takes; use "
                         f"{', '.join(STEP_TOOLS)}, since shell rules come from the layer and servers from mcp")
    if role_info.reach == "controller" and (tools or mcp or instructions):
        raise ValueError(f"{where}: the controller takes no tools, MCP servers or instruction scopes")
    if (role_info.reach != "controller" and role_info.web != "search" and traits.always_granted_tools
            and (missing := [t for t in traits.always_granted_tools if t not in tools])):
        raise ValueError(f"{where}: tools {', '.join(tools) or '[]'} leaves out {', '.join(missing)}, which {which} always "
                         "grants; list them")
    if mcp and not traits.takes_mcp:
        raise ValueError(f"{where}: mcp {', '.join(mcp)} cannot load on {which}, which runs with --no-mcp; run this "
                         "step on claude")
    if bad := [n for n in mcp if n not in cfg.mcp]:
        raise ValueError(f"{where}: mcp '{bad[0]}' names no [mcp.{bad[0]}]")
    if instructions and not traits.instruction_scopes:
        raise ValueError(f"{where}: instructions {', '.join(instructions)} cannot load on {which}, which loads no "
                         "instruction files; use []")
    if traits.instruction_scopes and (refused := [i for i in instructions if i not in traits.instruction_scopes]):
        raise ValueError(f"{where}: instructions {', '.join(map(repr, refused))} cannot load on {which}, which runs "
                         f"without your own {which} setup; use {json.dumps(list(traits.instruction_scopes))} or []")
    if not role_info.keeps_session and found["session"] == "resume":
        raise ValueError(f"{where}: session 'resume' needs a kept session and {role} keeps none; use fresh")


def _repo_name_for_layer(cfg: config.Config, layer: config.RepoLayer | None) -> str:
    if layer is None or layer.dir == Path():
        return ""
    resolved = layer.dir.resolve()
    for repo in cfg.repos:
        if repo.layer_dir.resolve() == resolved:
            return repo.name
    return layer.dir.parent.name if layer.dir.name == ".agents" else layer.dir.name


def resolve(cfg: config.Config, layer: config.RepoLayer | None, step: str, kind: str = "",
            overrides: dict | None = None, inherited: dict | None = None, *,
            ticket: tuple[str, str] | None = None, side: str = "",
            held: tuple[str, str, dict] | None = None,
            provenance: bool = False,
            config_commit: str = "", layer_from: str = "",
            arm_name: str = "", arm_overrides: dict | None = None,
            overrides_from: str = "--set", engine_sha: str = ""):
    role = config.STEP_ALIASES.get(step, step)
    if (role_info := config.ROLE_BY_NAME.get(role)) is None:
        raise ValueError(f"unknown step '{step}'; use {', '.join(config.STEP_NAMES)}")
    where = f"step {role}" + (f" for {kind} tickets" if kind else "")
    repo_name = _repo_name_for_layer(cfg, layer)
    if why := config.paused(cfg, repo=repo_name, role=role, kind=kind, layer=layer):
        raise config.Paused(why)
    layer = layer or config.RepoLayer(dir=Path())
    ab_name = ab_side = ""
    arm = None
    arm_from = ""
    if arm_overrides is not None:
        arm = config.step_table(arm_overrides, f"{where}: arm")
        arm_from = f"arm:{arm_name or 'default'}"
    if held:
        ab_name, ab_side, ab_arm_dict = held[0], held[1], (held[2] if held[1] == "arm" else None)
        if ab_arm_dict:
            arm = {**(arm or {}), **ab_arm_dict}
            arm_from = f"ab:{ab_name}"
    elif (test := experiment_for(cfg, layer, role, kind)) and (ticket or side):
        ab_name = test[0]
        ab_side = side or ("arm" if in_arm(ab_name, test[1]["share"], ticket) else "baseline")
        if ab_side == "arm":
            arm = {**(arm or {}), **test[1]["arm"]}
            arm_from = f"ab:{ab_name}"
    found, prov = merged_provenance(
        cfg, layer, role, kind, config.step_table(overrides or {}, f"{where}: overrides"),
        config.step_table(inherited or {}, f"{where}: overrides"), arm,
        config_commit=config_commit, layer_from=layer_from, arm_from=arm_from,
        overrides_from=overrides_from, engine_sha=engine_sha,
    )
    if why := config.paused(cfg, repo=repo_name, role=role, kind=kind, mcp=found["mcp"], layer=layer):
        raise config.Paused(why)
    engine_ref = engine_sha or codebase.STAMP or "engine"
    engine_source = f"arete/defaults/steps.toml@{engine_ref}"
    cfg_source = f"config.toml@{config_commit}" if config_commit else "config.toml"
    tier = ""
    entries: tuple[dict[str, str], ...] = ()
    route_prov: dict[str, str]
    if found.get("model"):
        source = found
        route_prov = prov.get("model", {"level": "engine", "from": engine_source})
    elif found.get("tier"):
        tier = found["tier"]
        map_name, tiers = tier_map(cfg, layer)
        if tier not in tiers:
            defined = f"[tiers.{map_name}], which has {', '.join(tiers) or 'no tiers'}" if map_name else "no tier map"
            raise ValueError(f"{where}: tier '{tier}' is not in {defined}")
        entries = tiers[tier]
        source = entries[0]
        route_prov = prov.get("tier", {"level": "engine", "from": engine_source})
    else:
        base_role = config.INHERITS.get(role, "")
        target_role = base_role or role
        fallback_model = cfg.model(target_role)
        source = {**found, "model": fallback_model}
        in_cfg = bool(cfg.models.get(target_role))
        if base_role:
            route_prov = {"level": "inherit", "from": cfg_source if in_cfg else engine_source}
        else:
            route_prov = {"level": "config" if in_cfg else "engine", "from": cfg_source if in_cfg else engine_source}
    hint = source.get("runner", "")
    if hint and hint not in routing.RUNNERS:
        raise ValueError(f"{where}: runner '{hint}' is not one of {', '.join(routing.RUNNERS)}")
    which, name, provider = routing.source_route(source)
    effort = found.get("effort", "")
    instructions = found.get("instructions", traits_of(which).default_instructions)
    check_reach(where, cfg, role_info, which, found, instructions)
    notes = codex.NOTES if which == "codex" else ()
    link_tier = tier
    if not tier:
        link_tier, entries = tier_of(cfg, layer, found.get("tier", ""), routing.identity(which, name, provider))
    if cfg.replay.all:
        name = f"codex:{name}" if which == "codex" and routing.route(name)[0] != which else name
        which = REPLAY
        entries = ()
    explicit = tuple(found["instructions"]) if "instructions" in found else None
    chain = chain_of((which, name, provider), link_tier, entries, effort, explicit)
    routes = config.routing_for(cfg, layer)
    fallback = routes.fallback
    runners = {link.runner for link in chain if fallback or link.requested}
    if fallback:
        runners.update({"openai": "codex", "anthropic": "claude"}[vendor]
                       for link in chain if (vendor := routing.identity(link.runner, link.model, link.provider)[0])
                       in ("openai", "anthropic"))
    supported = {level for run_with in runners for level in TRAITS[run_with].efforts}
    if effort and effort not in supported:
        raise ValueError(f"{where}: effort '{effort}' is not a level its routes take; use "
                         f"{', '.join(level for level in routing.EFFORT_ORDER if level in supported)}")
    floor = found.get("floor", "")
    floors = routes.floors
    if floor and floor not in floors:
        raise ValueError(f"{where}: floor '{floor}' is not in [routing.floors]")
    requires = routing.Requirements(
        modalities=tuple(found.get("modalities", ())), min_context=found.get("min_context", 0),
        structured_output=found.get("structured_output", False), data_handling=tuple(found.get("data_handling", ())),
        floor=floor, approved=floors.get(floor, ()), directory_cleanup=found.get("directory_cleanup", False),
        delegation_only=role_info.reach == "controller")
    resolved = Settings(step=role, tier=tier, runner=which, provider=provider, model=name, effort=effort,
                        timeout_minutes=found["timeout_minutes"], tries=found["tries"], tools=found["tools"],
                        mcp=found["mcp"], instructions=instructions, session=found["session"], notes=notes,
                        chain=chain, fallback=fallback,
                        instructions_explicit=explicit is not None, requires=requires,
                        inherits=config.INHERITS.get(role, ""), budget_usd=found.get("budget_usd"),
                        explicit_route=found.get("explicit_route", False), ab_name=ab_name, ab_side=ab_side,
                        ab_arm=json.dumps(arm) if arm else "",
                        prompts=dict(found.get("prompt", {})), schemas=dict(found.get("schema", {})))
    if not provenance:
        return resolved
    runner_src = prov.get("runner", route_prov)
    provider_src = prov.get("provider", route_prov)
    out_prov: dict[str, dict] = {}
    for k, v in prov.items():
        if k in config.STEP_MAPS:
            out_prov[k] = {ck: {"value": cv["value"], "level": cv["level"], "from": cv["from"]}
                           for ck, cv in v.items()}
        else:
            out_prov[k] = {"value": _json_value(v["value"]), "level": v["level"], "from": v["from"]}
    if name:
        out_prov["model"] = {"value": name, "level": route_prov["level"], "from": route_prov["from"]}
    if which:
        out_prov["runner"] = {"value": which, "level": runner_src["level"], "from": runner_src["from"]}
    if provider:
        out_prov["provider"] = {"value": provider, "level": provider_src["level"], "from": provider_src["from"]}
    if effort and "effort" in prov:
        out_prov["effort"] = {"value": effort, "level": prov["effort"]["level"], "from": prov["effort"]["from"]}
    if "instructions" not in out_prov:
        out_prov["instructions"] = {"value": list(instructions), "level": "engine", "from": engine_source}
    if ab_name:
        out_prov["ab"] = {"value": {"name": ab_name, "side": ab_side}, "level": "arm", "from": f"ab:{ab_name}"}
    return resolved, out_prov


def check(cfg: config.Config, layer: config.RepoLayer | None = None, *, defer_tiers: bool = False) -> None:
    layer = layer or config.RepoLayer(dir=Path())
    repo_name = _repo_name_for_layer(cfg, layer)
    deferred = defer_tiers and not (layer.tier_map or cfg.tier_map) and len(set(cfg.tiers) | set(layer.tiers)) > 1
    if not deferred and not config.paused(cfg, repo=repo_name, layer=layer):
        tier_map(cfg, layer)
    for kind in ("", *config.KINDS):
        for role in config.STEP_ROLES:
            if config.paused(cfg, repo=repo_name, role=role, kind=kind, layer=layer):
                continue
            if deferred:
                found = merged(cfg, layer, role, kind, {})
                if found.get("tier") and not found.get("model"):
                    continue
            try:
                resolve(cfg, layer, role, kind)
            except config.Paused:
                continue
    tests = experiments(cfg, layer)
    config.ab_clash(tests)
    for name, test in tests.items():
        if deferred and "tier" in test["arm"] and "model" not in test["arm"]:
            continue
        for kind in (test["kind"],) if test["kind"] else ("", *config.KINDS):
            if config.paused(cfg, repo=repo_name, role=test["step"], kind=kind, layer=layer):
                continue
            try:
                resolve(cfg, layer, test["step"], kind, side="arm")
            except config.Paused:
                continue
            except ValueError as e:
                raise ValueError(f"[ab.{name}]: {e}") from None
