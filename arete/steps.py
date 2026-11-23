from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from . import codex, config, configschema, paths, routing
from .agent import READ_ONLY_TOOLS, REPLAY, TRAITS, WEB_TOOLS, traits_of

MODEL_SOURCE = ("runner", "provider")
STEP_TOOLS = ("Read", "Glob", "Grep", "LS", "Edit", "MultiEdit", "Write", "TodoWrite")


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


def merged(cfg: config.Config, layer: config.RepoLayer, role: str, kind: str, overrides: dict,
           inherited: dict | None = None, arm: dict | None = None) -> dict:
    defaults, built_in = engine_defaults()
    base = config.INHERITS.get(role, "")
    if base:
        settings, own_route = {**merged(cfg, layer, base, kind, inherited or {}), **built_in.get(role, {})}, False
    else:
        settings, own_route = {"timeout_minutes": layer.timeout_minutes, "tries": layer.max_attempts, **defaults,
                               **built_in.get(role, {})}, True
    explicit_runner = False
    for level in (cfg.steps.get(role), cfg.kinds.get(kind, {}).get(role), layer.steps.get(role),
                  layer.kinds.get(kind, {}).get(role), overrides, arm):
        explicit_runner = explicit_runner or bool(level and "runner" in level)
        if level and "model" in level:
            settings = {k: v for k, v in settings.items() if k not in MODEL_SOURCE}
            own_route = True
        elif level and "tier" in level and not own_route:
            settings = {k: v for k, v in settings.items() if k not in (*MODEL_SOURCE, "model")}
            own_route = True
        settings.update(level or {})
    if base:
        settings["explicit_route"] = own_route or explicit_runner
    return settings


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


def resolve(cfg: config.Config, layer: config.RepoLayer | None, step: str, kind: str = "",
            overrides: dict | None = None, inherited: dict | None = None, *,
            ticket: tuple[str, str] | None = None, side: str = "",
            held: tuple[str, str, dict] | None = None) -> Settings:
    role = config.STEP_ALIASES.get(step, step)
    if (role_info := config.ROLE_BY_NAME.get(role)) is None:
        raise ValueError(f"unknown step '{step}'; use {', '.join(config.STEP_NAMES)}")
    where = f"step {role}" + (f" for {kind} tickets" if kind else "")
    layer = layer or config.RepoLayer(dir=Path())
    ab_name = ab_side = ""
    arm = None
    if held:
        ab_name, ab_side, arm = held[0], held[1], (held[2] if held[1] == "arm" else None)
    elif (test := experiment_for(cfg, layer, role, kind)) and (ticket or side):
        ab_name = test[0]
        ab_side = side or ("arm" if in_arm(ab_name, test[1]["share"], ticket) else "baseline")
        arm = test[1]["arm"] if ab_side == "arm" else None
    found = merged(cfg, layer, role, kind, config.step_table(overrides or {}, f"{where}: overrides"),
                   config.step_table(inherited or {}, f"{where}: overrides"), arm)
    tier = ""
    entries: tuple[dict[str, str], ...] = ()
    if found.get("model"):
        source = found
    elif found.get("tier"):
        tier = found["tier"]
        map_name, tiers = tier_map(cfg, layer)
        if tier not in tiers:
            defined = f"[tiers.{map_name}], which has {', '.join(tiers) or 'no tiers'}" if map_name else "no tier map"
            raise ValueError(f"{where}: tier '{tier}' is not in {defined}")
        entries = tiers[tier]
        source = entries[0]
    else:
        source = {**found, "model": cfg.model(config.INHERITS.get(role, role))}
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
    return Settings(step=role, tier=tier, runner=which, provider=provider, model=name, effort=effort,
                    timeout_minutes=found["timeout_minutes"], tries=found["tries"], tools=found["tools"],
                    mcp=found["mcp"], instructions=instructions, session=found["session"], notes=notes,
                    chain=chain, fallback=fallback,
                    instructions_explicit=explicit is not None, requires=requires,
                    inherits=config.INHERITS.get(role, ""), budget_usd=found.get("budget_usd"),
                    explicit_route=found.get("explicit_route", False), ab_name=ab_name, ab_side=ab_side,
                    ab_arm=json.dumps(arm) if arm else "")


def check(cfg: config.Config, layer: config.RepoLayer | None = None, *, defer_tiers: bool = False) -> None:
    layer = layer or config.RepoLayer(dir=Path())
    deferred = defer_tiers and not (layer.tier_map or cfg.tier_map) and len(set(cfg.tiers) | set(layer.tiers)) > 1
    if not deferred:
        tier_map(cfg, layer)
    for kind in ("", *config.KINDS):
        for role in config.STEP_ROLES:
            if deferred:
                found = merged(cfg, layer, role, kind, {})
                if found.get("tier") and not found.get("model"):
                    continue
            resolve(cfg, layer, role, kind)
    tests = experiments(cfg, layer)
    config.ab_clash(tests)
    for name, test in tests.items():
        if deferred and "tier" in test["arm"] and "model" not in test["arm"]:
            continue
        for kind in (test["kind"],) if test["kind"] else ("", *config.KINDS):
            try:
                resolve(cfg, layer, test["step"], kind, side="arm")
            except ValueError as e:
                raise ValueError(f"[ab.{name}]: {e}") from None
