from __future__ import annotations

import os
import time
from pathlib import Path

from . import claude, config, devices, pi, record
from .agent import NO_SESSION, Result, Session

RUNNERS = ("claude", "pi")
BUDGET_FLOOR_USD = 0.005


def route(model: str, runner: str = "") -> tuple[str, str]:
    prefix, _, rest = model.partition(":")
    if prefix in RUNNERS and rest:
        return prefix, rest
    if runner in RUNNERS:
        return runner, model
    return ("pi" if "/" in model else "claude"), model


def provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def priced(res: Result, cfg: config.Config) -> float:
    rate = cfg.pricing.get(res.model) or cfg.pricing.get(f"{res.runner}:{res.model}")
    if rate:
        return (res.tokens_in * rate[0] + res.tokens_out * rate[1]) / 1_000_000
    if res.runner == "pi" and (res.provider or provider_of(res.model)) in cfg.local_providers:
        return 0.0
    return res.cost_usd


def run(prompt: str, cwd: Path | str, *, model: str = "", runner: str = "", trace: tuple[str, str] = ("", ""),
        resume: Session = NO_SESSION, cfg: config.Config | None = None, device: devices.Device | str = "local",
        **kw) -> Result:
    cfg = cfg or config.load()
    target = devices.resolve(device)
    if target.remote:
        kw["remote"] = target
    which, name = route(model, runner or resume.runner)
    started = time.monotonic()
    res = (pi.run if which == "pi" else claude.run)(prompt, cwd, model=name, resume=resume, **kw)
    res.runner = which
    res.model = res.model or name
    res.provider = res.provider or provider_of(res.model)
    res.cost_usd = priced(res, cfg)
    run_id, step = trace if trace[0] else (os.environ.get("ARETE_RUN_ID", ""), trace[1] or "agent")
    if run_id:
        record.step(run_id, step, res, time.monotonic() - started)
    return res
