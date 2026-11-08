from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import claude, config, devices, pi, record, util
from .agent import NO_SESSION, READ_ONLY_TOOLS, READ_TOOLS, Result, Session

RUNNERS = ("claude", "pi")
BUDGET_FLOOR_USD = 0.005
EDIT_TOOLS = [*READ_TOOLS, "Edit", "Write"]
BUILD_TOOLS = [*EDIT_TOOLS, "TodoWrite"]
WRITING_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})
KNOWN_TOOLS = READ_ONLY_TOOLS | WRITING_TOOLS | {"Bash"}
NO_GIT_WRITES = [
    "Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)", "Bash(git checkout*)",
    "Bash(git switch*)", "Bash(git rebase*)", "Bash(git merge*)", "Bash(gh *)",
]
PI_HOLDS_NO_BASH_RULES = "pi cannot hold the rules that stop git writes"
LOGIN_WARNING_DAYS = 7


def route(model: str, runner: str = "") -> tuple[str, str]:
    prefix, _, rest = model.partition(":")
    if prefix in RUNNERS and rest:
        return prefix, rest
    if runner in RUNNERS:
        return runner, model
    return ("pi" if "/" in model else "claude"), model


def provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def agent_problems(run_with: str, tools: list[str]) -> list[str]:
    problems = []
    if run_with and run_with not in RUNNERS:
        problems.append(f"runner '{run_with}' is not one of {', '.join(RUNNERS)}")
    if unknown := sorted(set(tools) - KNOWN_TOOLS):
        problems.append(f"unknown tools {', '.join(unknown)}; use {', '.join(sorted(KNOWN_TOOLS))}")
    return problems


def withheld(model: str, run_with: str, tools: list[str], remote: bool) -> dict[str, list[str]]:
    if route(model, run_with)[0] != "pi":
        return {}
    reasons: dict[str, list[str]] = {}
    for tool in tools:
        if tool == "Bash":
            reasons.setdefault(PI_HOLDS_NO_BASH_RULES, []).append(tool)
        elif tool in WRITING_TOOLS and not pi.can_confine(remote):
            reasons.setdefault(pi.NO_CONFINEMENT_REASON, []).append(tool)
    return reasons


def claude_login() -> tuple[bool, str]:
    res = util.sh([claude.binary(), "auth", "status"], timeout=30)
    try:
        status = json.loads(res.stdout)
    except json.JSONDecodeError:
        return False, f"claude auth status failed: {util.first_line(res.stderr or res.stdout) or f'exit {res.returncode}'}"
    if not status.get("loggedIn"):
        return False, "claude CLI is not logged in"
    if not status.get("loginExpiresAt"):
        return True, "logged in"
    days = (util.parse_iso(status["loginExpiresAt"]) - util.now()).total_seconds() / 86400
    if days < 0:
        return False, "login expired; run `claude auth login`"
    if days < LOGIN_WARNING_DAYS:
        return False, f"login expires in {days:.1f} days; run `claude auth login`"
    return True, f"login good for {days:.0f} days"


def cost_note(res: Result) -> str:
    return f"({util.count(res.turns, 'turn')}, ${res.cost_usd:.2f})"


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
