from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import claude, codex, config, pi, routing, runner

EFFORTS = {"claude": claude.EFFORTS, "pi": pi.THINKING_LEVELS, "codex": codex.EFFORTS,
           runner.REPLAY: tuple(dict.fromkeys([*claude.EFFORTS, *pi.THINKING_LEVELS, *codex.EFFORTS]))}
CODEX_INSTRUCTIONS = ("repo",)
DEFAULTS = {"tools": tuple(runner.READ_TOOLS), "mcp": (), "session": "fresh"}
BUILT_IN = {"build": {"tools": tuple(runner.BUILD_TOOLS), "session": "resume"},
            "learn": {"timeout_minutes": 15}, "uat": {"timeout_minutes": 10, "tries": 3},
            "research_plan": {"timeout_minutes": 10, "tries": 2}}
MODEL_SOURCE = ("runner", "provider")
STEP_TOOLS = ("Read", "Glob", "Grep", "LS", "Edit", "MultiEdit", "Write", "TodoWrite")
KEEPS_NO_SESSION = ("learn", "uat", "research_plan")
READ_ONLY = ("research_plan",)


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

    def effort_on(self, which: str) -> str:
        return routing.nearest_effort(which, self.effort)

    def instructions_on(self, which: str) -> tuple[str, ...]:
        return self.instructions if self.instructions_explicit else default_instructions(which)

    @property
    def denied(self) -> tuple[str, ...]:
        return ()

    @property
    def call(self) -> dict:
        return {"model": self.model, "runner": self.runner, "effort": self.effort_on(self.runner),
                "timeout": self.timeout_minutes * 60}

    def agent_kw(self, cfg: config.Config, rules=(), own_mcp: str = "") -> dict:
        return {"allowed_tools": [*self.tools, *rules, *(f"mcp__{n}" for n in self.mcp)],
                "mcp_config": mcp_config(cfg, self.mcp, own_mcp), "instructions": list(self.instructions)}


def mcp_config(cfg: config.Config, names, own: str = "") -> str:
    if not names:
        return own
    own_servers = json.loads(own)["mcpServers"] if own else {}
    return json.dumps({"mcpServers": {**{n: cfg.mcp[n] for n in names}, **own_servers}})


def default_instructions(which: str) -> tuple[str, ...]:
    return {"pi": (), "codex": CODEX_INSTRUCTIONS}.get(which, config.INSTRUCTIONS)


def selected_tiers(cfg: config.Config, layer: config.RepoLayer) -> dict[str, tuple[dict[str, str], ...]]:
    try:
        return tier_map(cfg, layer)[1]
    except ValueError:
        return {}


def holds_model(entries: tuple[dict[str, str], ...], wanted: tuple[str, str]) -> bool:
    return any(e.get("model") and routing.identity(*routing.source_route(e)[:2]) == wanted for e in entries)


def tier_of(cfg: config.Config, layer: config.RepoLayer, named: str, wanted: tuple[str, str]) -> tuple[str, tuple]:
    tiers = selected_tiers(cfg, layer)
    if named in tiers and holds_model(tiers[named], wanted):
        return named, tiers[named]
    return next(((n, e) for n, e in tiers.items() if holds_model(e, wanted)), ("", ()))


def chain_of(requested: tuple[str, str, str], tier: str, entries: tuple[dict[str, str], ...], effort: str,
             explicit: tuple[str, ...] | None) -> tuple[routing.Link, ...]:
    def link(which: str, name: str, provider: str, position: int) -> routing.Link:
        return routing.Link(name, which, provider, tier, position, position == 0, routing.nearest_effort(which, effort),
                            default_instructions(which) if explicit is None else explicit)

    wanted = routing.identity(*requested[:2])
    chain = [link(*requested, 0)]
    for entry in entries:
        which, name, provider = routing.source_route(entry)
        if entry.get("model") and routing.identity(which, name) != wanted:
            chain.append(link(which, name, provider, len(chain)))
    return tuple(chain)


def chain_for(cfg: config.Config, layer: config.RepoLayer | None, which: str, name: str, effort: str,
              explicit: tuple[str, ...] | None) -> tuple[tuple[routing.Link, ...], bool]:
    layer = layer or config.RepoLayer(dir=Path())
    tier, entries = tier_of(cfg, layer, "", routing.identity(which, name))
    requested = routing.source_route({"model": name, "runner": which})
    return chain_of(requested, tier, entries, effort, explicit), cfg.routing_fallback and layer.routing_fallback


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


def merged(cfg: config.Config, layer: config.RepoLayer, role: str, kind: str, overrides: dict) -> dict:
    settings = {"timeout_minutes": layer.timeout_minutes, "tries": layer.max_attempts, **DEFAULTS,
                **BUILT_IN.get(role, {})}
    for level in (cfg.steps.get(role), cfg.kinds.get(kind, {}).get(role), layer.steps.get(role),
                  layer.kinds.get(kind, {}).get(role), overrides):
        if level and "model" in level:
            settings = {k: v for k, v in settings.items() if k not in MODEL_SOURCE}
        settings.update(level or {})
    return settings


def check_reach(where: str, cfg: config.Config, role: str, which: str, found: dict, instructions: tuple) -> None:
    tools, mcp = found["tools"], found["mcp"]
    if role in READ_ONLY:
        if acting := [t for t in tools if t not in runner.READ_ONLY_TOOLS]:
            raise ValueError(f"{where}: tools {', '.join(acting)} would let {role} change files; it is read only, "
                             "so use Read, Glob, Grep and LS")
        if mcp:
            raise ValueError(f"{where}: mcp {', '.join(mcp)} cannot load on {role}, which is read only and takes "
                             "no servers")
    if bad := [t for t in tools if t not in STEP_TOOLS]:
        raise ValueError(f"{where}: tools {', '.join(bad)} is not a built in tool a step takes; use "
                         f"{', '.join(STEP_TOOLS)}, since shell rules come from the layer and servers from mcp")
    if which in ("pi", "codex") and (missing := [t for t in runner.READ_TOOLS if t not in tools]):
        raise ValueError(f"{where}: tools {', '.join(tools) or '[]'} leaves out {', '.join(missing)}, which {which} always "
                         "grants; list them")
    if which == "pi" and mcp:
        raise ValueError(f"{where}: mcp {', '.join(mcp)} cannot load on pi, which runs with --no-mcp; run this step "
                         "on claude")
    if bad := [n for n in mcp if n not in cfg.mcp]:
        raise ValueError(f"{where}: mcp '{bad[0]}' names no [mcp.{bad[0]}]")
    if which == "pi" and instructions:
        raise ValueError(f"{where}: instructions {', '.join(instructions)} cannot load on pi, which loads no "
                         "instruction files; use []")
    if which == "codex" and "user" in instructions:
        raise ValueError(f"{where}: instructions 'user' cannot load on codex, which runs without your own codex "
                         "setup; use [\"repo\"] or []")
    if role in KEEPS_NO_SESSION and found["session"] == "resume":
        raise ValueError(f"{where}: session 'resume' needs a kept session and {role} keeps none; use fresh")


def resolve(cfg: config.Config, layer: config.RepoLayer | None, step: str, kind: str = "",
            overrides: dict | None = None) -> Settings:
    role = config.STEP_ALIASES.get(step, step)
    if role not in config.MODEL_ROLES:
        raise ValueError(f"unknown step '{step}'; use {', '.join(config.STEP_NAMES)}")
    where = f"step {role}" + (f" for {kind} tickets" if kind else "")
    layer = layer or config.RepoLayer(dir=Path())
    found = merged(cfg, layer, role, kind, config.step_table(overrides or {}, f"{where}: overrides"))
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
        source = {**found, "model": cfg.model(role)}
    hint = source.get("runner", "")
    if hint and hint not in runner.RUNNERS:
        raise ValueError(f"{where}: runner '{hint}' is not one of {', '.join(runner.RUNNERS)}")
    which, name, provider = routing.source_route(source)
    effort = found.get("effort", "")
    instructions = found.get("instructions", default_instructions(which))
    check_reach(where, cfg, role, which, found, instructions)
    notes = codex.NOTES if which == "codex" else ()
    link_tier = tier
    if not tier:
        link_tier, entries = tier_of(cfg, layer, found.get("tier", ""), routing.identity(which, name))
    if cfg.replay.all:
        name = f"codex:{name}" if which == "codex" and runner.route(name)[0] != which else name
        which = runner.REPLAY
        entries = ()
    explicit = tuple(found["instructions"]) if "instructions" in found else None
    chain = chain_of((which, name, provider), link_tier, entries, effort, explicit)
    fallback = cfg.routing_fallback and layer.routing_fallback
    runners = {link.runner for link in chain if fallback or link.requested}
    if fallback:
        runners.update({"openai": "codex", "anthropic": "claude"}[vendor]
                       for link in chain if (vendor := routing.identity(link.runner, link.model)[0])
                       in ("openai", "anthropic"))
    supported = {level for run_with in runners for level in EFFORTS[run_with]}
    if effort and effort not in supported:
        raise ValueError(f"{where}: effort '{effort}' is not a level its routes take; use "
                         f"{', '.join(level for level in routing.EFFORT_ORDER if level in supported)}")
    return Settings(step=role, tier=tier, runner=which, provider=provider, model=name, effort=effort,
                    timeout_minutes=found["timeout_minutes"], tries=found["tries"], tools=found["tools"],
                    mcp=found["mcp"], instructions=instructions, session=found["session"], notes=notes,
                    chain=chain, fallback=fallback,
                    instructions_explicit=explicit is not None)


def check(cfg: config.Config, layer: config.RepoLayer | None = None, *, defer_tiers: bool = False) -> None:
    layer = layer or config.RepoLayer(dir=Path())
    deferred = defer_tiers and not (layer.tier_map or cfg.tier_map) and len(set(cfg.tiers) | set(layer.tiers)) > 1
    if not deferred:
        tier_map(cfg, layer)
    for kind in ("", *config.KINDS):
        for role in config.MODEL_ROLES:
            if deferred:
                found = merged(cfg, layer, role, kind, {})
                if found.get("tier") and not found.get("model"):
                    continue
            resolve(cfg, layer, role, kind)
