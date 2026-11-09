from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import ask, claude, config, devices, guard, jev, pi, record, state, util
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
HOLD_CHECK_SECONDS = 60
CONTINUE_AFTER_LIMIT = "The usage limit cut the last step short. Carry on from where you stopped and finish the step."
LIMIT_SCOPES = (claude.EVERY_MODEL, *claude.LIMIT_SCOPES.values())


def route(model: str, runner: str = "") -> tuple[str, str]:
    prefix, _, rest = model.partition(":")
    if prefix in RUNNERS and rest:
        return prefix, rest
    if runner in RUNNERS:
        return runner, model
    return ("pi" if "/" in model else "claude"), model


def metered(model: str, run_with: str = "") -> bool:
    return route(model, run_with)[0] != "claude"


def limit_applies(scope: str, model: str) -> bool:
    return scope == claude.EVERY_MODEL or scope in model.lower()


def usage_hold(model: str, run_with: str = "", connect=state.db) -> dict | None:
    which, name = route(model, run_with)
    if which != "claude":
        return None
    return next((h for h in reversed(state.holds(connect)) if limit_applies(h["scope"], name)), None)


def hold_words(hold: dict) -> str:
    return f"{hold['why']}, waiting until {local_time(hold['until'])}"


def local_time(stamp: str) -> str:
    return util.parse_iso(stamp).astimezone().strftime("%a %b %d %H:%M")


def wait_out(scope: str) -> None:
    while until := state.hold_until(scope):
        time.sleep(max(1.0, min(HOLD_CHECK_SECONDS, (util.parse_iso(until) - util.now()).total_seconds())))


def tell_stderr(text: str) -> None:
    print(f"{util.iso()} {text}", file=sys.stderr, flush=True)


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
        wait_out_limits: bool = True, say: Callable[[str], None] = tell_stderr, **kw) -> Result:
    cfg = cfg or config.load()
    target = devices.resolve(device)
    if target.remote:
        kw["remote"] = target
    which, name = route(model, runner or resume.runner)
    trace = trace if trace[0] else (os.environ.get("ARETE_RUN_ID", ""), trace[1] or "agent")
    if which == "claude":
        kw.pop("max_budget_usd", None)
        if not target.remote:
            kw = local_claude(kw, cwd, cfg, trace[0])
    else:
        kw["price"] = lambda reply: priced(reply, cfg)
    limited: list[Result] = []
    while True:
        if wait_out_limits and (held := usage_hold(name, which)):
            say(hold_words(held))
            wait_out(held["scope"])
            say("the claude usage limit has lifted, going on")
        started = util.iso()
        res = attempt(which, name, prompt, cwd, resume, cfg, trace, kw)
        if not res.usage_limit:
            if which == "claude" and res.ok:
                state.clear_holds([s for s in LIMIT_SCOPES if limit_applies(s, res.model)], set_before=started)
            return with_spent(res, limited)
        limit = res.usage_limit
        state.hold(limit.scope, limit.until, f"claude {limit.kind or 'usage'} limit reached")
        if not wait_out_limits:
            return res
        limited.append(res)
        say(hold_words({"why": f"claude {limit.kind or 'usage'} limit reached", "until": limit.until}))
        wait_out(limit.scope)
        say("the claude usage limit has lifted, going on")
        if res.session.id and res.turns > 1 and kw.get("persist", True):
            prompt, resume = CONTINUE_AFTER_LIMIT, res.session


def with_spent(res: Result, earlier: list[Result]) -> Result:
    res.cost_usd += sum(r.cost_usd for r in earlier)
    res.tokens_in += sum(r.tokens_in for r in earlier)
    res.tokens_out += sum(r.tokens_out for r in earlier)
    return res


def attempt(which: str, name: str, prompt: str, cwd: Path | str, resume: Session, cfg: config.Config,
            trace: tuple[str, str], kw: dict) -> Result:
    started = time.monotonic()
    res = (pi.run if which == "pi" else claude.run)(prompt, cwd, model=name, resume=resume, **kw)
    res.runner = which
    res.model = res.model or name
    res.provider = res.provider or provider_of(res.model)
    res.cost_usd = priced(res, cfg)
    if trace[0]:
        record.step(trace[0], trace[1], res, time.monotonic() - started)
    return res


def local_claude(kw: dict, cwd: Path | str, cfg: config.Config, run_id: str) -> dict:
    kw = {**kw, "extra_env": {"ARETE_RUN_ID": run_id} if run_id else {}}
    if cfg.jev.guard and guard.wanted(kw.get("tools")):
        kw["settings"] = guard.settings()
    if runs_bash(kw) and jev.ready(cfg, [cfg.repo_for_work_dir(cwd)]):
        kw["allowed_tools"] = [*(kw.get("allowed_tools") or []), ask.rule()]
        kw["system"] = "\n\n".join(filter(None, [kw.get("system", ""), ask.note()]))
    return kw


def runs_bash(kw: dict) -> bool:
    if (tools := kw.get("tools")) is not None:
        return "Bash" in tools
    return any(r == "Bash" or r.startswith("Bash(") for r in kw.get("allowed_tools") or [])
