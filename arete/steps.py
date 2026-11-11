from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import claude, config, pi, runner

EFFORTS = {"claude": claude.EFFORTS, "pi": pi.THINKING_LEVELS,
           runner.REPLAY: tuple(dict.fromkeys([*claude.EFFORTS, *pi.THINKING_LEVELS]))}
DEFAULTS = {"tools": tuple(runner.READ_TOOLS), "mcp": (), "session": "fresh"}
BUILT_IN = {"build": {"tools": tuple(runner.BUILD_TOOLS), "session": "resume"},
            "learn": {"timeout_minutes": 15}, "uat": {"timeout_minutes": 10, "tries": 3}}
MODEL_SOURCE = ("runner", "provider")
STEP_TOOLS = ("Read", "Glob", "Grep", "LS", "Edit", "MultiEdit", "Write", "TodoWrite")
KEEPS_NO_SESSION = ("learn", "uat")


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

    @property
    def call(self) -> dict:
        return {"model": self.model, "runner": self.runner, "effort": self.effort, "timeout": self.timeout_minutes * 60}

    def agent_kw(self, cfg: config.Config, rules=(), own_mcp: str = "") -> dict:
        return {"allowed_tools": [*self.tools, *rules, *(f"mcp__{n}" for n in self.mcp)],
                "mcp_config": mcp_config(cfg, self.mcp, own_mcp), "instructions": list(self.instructions)}


def mcp_config(cfg: config.Config, names, own: str = "") -> str:
    if not names:
        return own
    own_servers = json.loads(own)["mcpServers"] if own else {}
    return json.dumps({"mcpServers": {**{n: cfg.mcp[n] for n in names}, **own_servers}})


def tier_map(cfg: config.Config, layer: config.RepoLayer | None = None) -> tuple[str, dict[str, dict[str, str]]]:
    own = layer.tiers if layer else {}
    maps = {name: {**cfg.tiers.get(name, {}), **own.get(name, {})} for name in dict.fromkeys([*cfg.tiers, *own])}
    if cfg.tier_map:
        if cfg.tier_map not in maps:
            raise ValueError(f"[factory] tier_map '{cfg.tier_map}' names no [tiers.{cfg.tier_map}]")
        return cfg.tier_map, maps[cfg.tier_map]
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
    if bad := [t for t in tools if t not in STEP_TOOLS]:
        raise ValueError(f"{where}: tools {', '.join(bad)} is not a built in tool a step takes; use "
                         f"{', '.join(STEP_TOOLS)}, since shell rules come from the layer and servers from mcp")
    if which == "pi" and (missing := [t for t in runner.READ_TOOLS if t not in tools]):
        raise ValueError(f"{where}: tools {', '.join(tools) or '[]'} leaves out {', '.join(missing)}, which pi always "
                         "grants; list them")
    if which == "pi" and mcp:
        raise ValueError(f"{where}: mcp {', '.join(mcp)} cannot load on pi, which runs with --no-mcp; run this step "
                         "on claude")
    if bad := [n for n in mcp if n not in cfg.mcp]:
        raise ValueError(f"{where}: mcp '{bad[0]}' names no [mcp.{bad[0]}]")
    if which == "pi" and instructions:
        raise ValueError(f"{where}: instructions {', '.join(instructions)} cannot load on pi, which loads no "
                         "instruction files; use []")
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
    if found.get("model"):
        source = found
    elif found.get("tier"):
        tier = found["tier"]
        map_name, tiers = tier_map(cfg, layer)
        if tier not in tiers:
            defined = f"[tiers.{map_name}], which has {', '.join(tiers) or 'no tiers'}" if map_name else "no tier map"
            raise ValueError(f"{where}: tier '{tier}' is not in {defined}")
        source = tiers[tier]
    else:
        source = {**found, "model": cfg.model(role)}
    hint = source.get("runner", "")
    if hint and hint not in runner.RUNNERS:
        raise ValueError(f"{where}: runner '{hint}' is not one of {', '.join(runner.RUNNERS)}")
    which, name = runner.route(source.get("model", ""), hint)
    provider = source.get("provider", "") or runner.provider_of(name)
    if which == "pi" and source.get("provider") and not name.startswith(f"{provider}/"):
        name = f"{provider}/{name}"
    effort = found.get("effort", "")
    if effort and effort not in EFFORTS[which]:
        raise ValueError(f"{where}: effort '{effort}' is not a level {which} takes; use {', '.join(EFFORTS[which])}")
    instructions = found.get("instructions", () if which == "pi" else config.INSTRUCTIONS)
    check_reach(where, cfg, role, which, found, instructions)
    if cfg.replay.all:
        which = runner.REPLAY
    return Settings(step=role, tier=tier, runner=which, provider=provider, model=name, effort=effort,
                    timeout_minutes=found["timeout_minutes"], tries=found["tries"], tools=found["tools"],
                    mcp=found["mcp"], instructions=instructions, session=found["session"])


def check(cfg: config.Config, layer: config.RepoLayer | None = None) -> None:
    tier_map(cfg, layer)
    for kind in ("", *config.KINDS):
        for role in config.MODEL_ROLES:
            resolve(cfg, layer, role, kind)
