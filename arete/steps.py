from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import claude, config, pi, runner

EFFORTS = {"claude": claude.EFFORTS, "pi": pi.THINKING_LEVELS,
           runner.REPLAY: tuple(dict.fromkeys([*claude.EFFORTS, *pi.THINKING_LEVELS]))}
BUILT_IN = {"learn": {"timeout_minutes": 15}, "uat": {"timeout_minutes": 10, "tries": 3}}
MODEL_SOURCE = ("runner", "provider")


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

    @property
    def call(self) -> dict:
        return {"model": self.model, "runner": self.runner, "effort": self.effort, "timeout": self.timeout_minutes * 60}


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
    settings = {"timeout_minutes": layer.timeout_minutes, "tries": layer.max_attempts, **BUILT_IN.get(role, {})}
    for level in (cfg.steps.get(role), cfg.kinds.get(kind, {}).get(role), layer.steps.get(role),
                  layer.kinds.get(kind, {}).get(role), overrides):
        if level and "model" in level:
            settings = {k: v for k, v in settings.items() if k not in MODEL_SOURCE}
        settings.update(level or {})
    return settings


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
    if cfg.replay.all:
        which = runner.REPLAY
    return Settings(step=role, tier=tier, runner=which, provider=provider, model=name, effort=effort,
                    timeout_minutes=found["timeout_minutes"], tries=found["tries"])


def check(cfg: config.Config, layer: config.RepoLayer | None = None) -> None:
    tier_map(cfg, layer)
    for kind in ("", *config.KINDS):
        for role in config.MODEL_ROLES:
            resolve(cfg, layer, role, kind)
