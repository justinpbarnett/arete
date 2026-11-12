from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from . import ask, claude, config, devices, environment, guard, jev, paths, pi, record, replay, state, util
from .agent import (
    NO_SESSION,
    READ_ONLY_TOOLS,
    READ_TOOLS,
    SUBSCRIPTION_RUNNER,
    Result,
    Session,
)

REPLAY = replay.RUNNER
RUNNERS = ("claude", "pi", REPLAY)
SECRET_SEARCH_SKIPS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__"})
SECRET_SEARCH_DEPTH = 4
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
SANDBOX_RUNS_PI = ("an exe device runs agents on pi, not claude; pick a provider/model such as exe-llm/gpt-5.4-mini "
                   "or openrouter/<model id>")
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


def chosen(model: str, run_with: str, cfg: config.Config) -> tuple[str, str]:
    which, name = route(model, run_with)
    return (REPLAY if cfg.replay.all else which), name


def stands_for(model: str, run_with: str) -> tuple[str, str]:
    which, name = route(model, "" if run_with == REPLAY else run_with)
    return (which if which != REPLAY else "pi" if "/" in name else "claude"), name


def run_env(run_id: str) -> dict[str, str]:
    return {"ARETE_RUN_ID": run_id} if run_id else {}


def env_names(model: str, run_with: str, cfg: config.Config, repo: config.Repo | None, run_id: str) -> list[str]:
    which, name = stands_for(model, run_with)
    passing = environment.passed(cfg, repo)
    if which == "pi":
        return sorted(pi.agent_env(name, passing, cfg.environment.providers))
    return sorted(claude.agent_env(passing, run_env(run_id)))


def judge_note(cfg: config.Config, passing: list[str] | None = None, repo: config.Repo | None = None) -> str:
    passing = environment.passed(cfg, repo) if passing is None else passing
    if not (cfg.jev.guard and jev.ready(cfg, [repo])) or jev.key(claude.agent_env(passing)):
        return ""
    return (f"the guard has no judge: JEV's key is only in the server's environment, which an agent no longer gets, so "
            f"the guard judges no command and keeps only its secret file rules; put {jev.KEY_NAME} in "
            f"{paths.short_path(pi.KEYS_FILE)}")


def metered(model: str, run_with: str = "") -> bool:
    return route(model, run_with)[0] != SUBSCRIPTION_RUNNER


def limit_applies(scope: str, model: str) -> bool:
    return scope == claude.EVERY_MODEL or scope in model.lower()


def usage_hold(model: str, run_with: str = "", connect=state.db, cfg: config.Config | None = None) -> dict | None:
    which, name = route(model, run_with)
    if which != "claude" or (cfg and cfg.replay.all):
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


def device_refusal(model: str, run_with: str, device: devices.Device) -> str:
    return SANDBOX_RUNS_PI if device.exe_sandbox and route(model, run_with)[0] == "claude" else ""


def withheld(model: str, run_with: str, tools: list[str], device: devices.Device) -> dict[str, list[str]]:
    if route(model, run_with)[0] != "pi" or device.exe_sandbox:
        return {}
    reasons: dict[str, list[str]] = {}
    for tool in tools:
        if tool == "Bash":
            reasons.setdefault(PI_HOLDS_NO_BASH_RULES, []).append(tool)
        elif tool in WRITING_TOOLS and not pi.can_confine(device.remote):
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
    paid = "subscription" if res.runner == SUBSCRIPTION_RUNNER else util.paid_note(res.cost_usd)
    return f"({util.count(res.turns, 'turn')}, {paid})"


def priced(res: Result, cfg: config.Config) -> float:
    if res.runner == REPLAY:
        return res.cost_usd
    rate = cfg.pricing.get(res.model) or cfg.pricing.get(f"{res.runner}:{res.model}")
    if rate:
        return (res.tokens_in * rate[0] + res.tokens_out * rate[1]) / 1_000_000
    if res.runner == "pi" and (res.provider or provider_of(res.model)) in cfg.local_providers:
        return 0.0
    return res.cost_usd


def run(prompt: str, cwd: Path | str, *, model: str = "", runner: str = "", trace: tuple[str, str] = ("", ""),
        resume: Session = NO_SESSION, cfg: config.Config | None = None, device: devices.Device | str = "local",
        wait_out_limits: bool = True, say: Callable[[str], None] = tell_stderr, ask_jev: bool = True, tier: str = "",
        kind: str = "", waits: Callable[[str], AbstractContextManager] = lambda cause: nullcontext(),
        repo: config.Repo | None = None, **kw) -> Result:
    cfg = cfg or config.load()
    which, name = chosen(model, runner or resume.runner, cfg)
    target = devices.LOCAL if which == REPLAY else devices.resolve(device)
    if target.remote:
        kw["remote"] = target
    elif which in ("claude", "pi"):
        repo = repo or cfg.repo_for_work_dir(cwd)
        kw["passing"] = environment.passed(cfg, repo)
    trace = trace if trace[0] else (os.environ.get("ARETE_RUN_ID", ""), trace[1] or "agent")
    record_id = trace[0] or record.new_id("call")
    if which == "claude":
        kw.pop("max_budget_usd", None)
        if not target.remote:
            kw = local_claude(kw, cfg, trace[0], ask_jev, say, repo)
    elif which == "pi":
        kw["price"] = lambda reply: priced(reply, cfg)
        if not target.remote:
            kw["providers"] = cfg.environment.providers
    limited: list[Result] = []
    while True:
        if wait_out_limits and (held := usage_hold(name, which, cfg=cfg)):
            say(hold_words(held))
            with waits("limit"):
                wait_out(held["scope"])
            say("the claude usage limit has lifted, going on")
        started = util.iso()
        res = attempt(which, name, prompt, cwd, resume, cfg, trace, record_id, target, kw, tier, kind)
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
        with waits("limit"):
            wait_out(limit.scope)
        say("the claude usage limit has lifted, going on")
        if res.session.id and res.turns > 1 and kw.get("persist", True):
            prompt, resume = CONTINUE_AFTER_LIMIT, res.session


def with_spent(res: Result, earlier: list[Result]) -> Result:
    res.cost_usd += sum(r.cost_usd for r in earlier)
    res.tokens_in += sum(r.tokens_in for r in earlier)
    res.tokens_out += sum(r.tokens_out for r in earlier)
    res.cache_read += sum(r.cache_read for r in earlier)
    res.cache_write += sum(r.cache_write for r in earlier)
    res.api_seconds += sum(r.api_seconds for r in earlier)
    return res


def attempt(which: str, name: str, prompt: str, cwd: Path | str, resume: Session, cfg: config.Config,
            trace: tuple[str, str], record_id: str, target: devices.Device, kw: dict, tier: str = "",
            kind: str = "") -> Result:
    header = {"runner": which, "model": name, "device": target.name, **origin(cwd, cfg, target), "cwd": str(cwd),
              "prompt": prompt, "system": kw.get("system", ""), "tools": kw.get("tools"),
              "allowed_tools": kw.get("allowed_tools"), "disallowed_tools": kw.get("disallowed_tools"),
              "json_schema": kw.get("json_schema"), "budget_usd": kw.get("max_budget_usd"),
              "timeout": kw.get("timeout"), "effort": kw.get("effort", ""),
              "resume": {"runner": resume.runner, "id": resume.id, "spent_usd": resume.spent_usd} if resume.id else None}
    hidden = record.secrets(*secret_files([cwd, *(kw.get("add_dirs") or [])], target, cfg))
    started = time.monotonic()
    with record.Recorder(record_id, trace[1], header, hidden) as rec:
        if which == REPLAY:
            res = replay.run(cwd, trace, cfg, rec.line, resume)
        else:
            res = (pi.run if which == "pi" else claude.run)(prompt, cwd, model=name, resume=resume, on_line=rec.line,
                                                             **kw)
        res.runner = which
        res.model = res.model or name
        res.provider = res.provider or provider_of(res.model)
        res.cost_usd = priced(res, cfg)
        rec.finish(res, time.monotonic() - started)
    if trace[0]:
        record.step(trace[0], trace[1], res, time.monotonic() - started, effort=kw.get("effort", ""), tier=tier,
                    kind=kind)
    return res


def origin(cwd: Path | str, cfg: config.Config, target: devices.Device) -> dict:
    if target.remote:
        return {"repo": "", "commit": ""}
    found = util.sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir", "HEAD"], cwd=cwd, timeout=10)
    common, _, commit = found.stdout.strip().partition("\n") if found.returncode == 0 else ("", "", "")
    repo = cfg.repo_for_work_dir(cwd) or (cfg.repo_for_path(Path(common).parent) if common else None)
    return {"repo": repo.name if repo else "", "commit": commit}


def secret_files(folders: list[Path | str], target: devices.Device, cfg: config.Config) -> list[Path]:
    found = [paths.HOME / name for name in (".netrc", ".pgpass", ".npmrc", ".pypirc", ".aws/credentials")]
    found += [cfg.jira.env_file] if cfg.jira.env_file else []
    for folder in [] if target.remote else folders:
        for here, subfolders, names in os.walk(folder):
            subfolders[:] = [d for d in subfolders if d not in SECRET_SEARCH_SKIPS]
            if Path(here).relative_to(folder).parts[SECRET_SEARCH_DEPTH:]:
                subfolders.clear()
                continue
            found += [Path(here, n) for n in names if guard.secret_path(n)]
    return found


def local_claude(kw: dict, cfg: config.Config, run_id: str, ask_jev: bool, say: Callable[[str], None],
                 repo: config.Repo | None) -> dict:
    kw = {**kw, "extra_env": run_env(run_id)}
    if cfg.jev.guard and guard.wanted(kw.get("tools")):
        kw["settings"] = guard.settings()
        if runs_bash(kw) and (note := judge_note(cfg, kw["passing"], repo)):
            say(note)
    if ask_jev and runs_bash(kw) and jev.ready(cfg, [repo], env=claude.agent_env(kw["passing"])):
        kw["allowed_tools"] = [*(kw.get("allowed_tools") or []), ask.rule()]
        kw["system"] = "\n\n".join(filter(None, [kw.get("system", ""), ask.note()]))
    return kw


def runs_bash(kw: dict) -> bool:
    if (tools := kw.get("tools")) is not None:
        return "Bash" in tools
    return any(r == "Bash" or r.startswith("Bash(") for r in kw.get("allowed_tools") or [])
