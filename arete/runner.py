from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from . import (
    accounts,
    ask,
    claude,
    codex,
    config,
    devices,
    environment,
    guard,
    jev,
    paths,
    pi,
    record,
    replay,
    state,
    util,
)
from .agent import (
    NO_SESSION,
    READ_ONLY_TOOLS,
    READ_TOOLS,
    Result,
    Session,
    UsageLimit,
    model_scope,
    subscription,
)

REPLAY = replay.RUNNER
RUNNERS = ("claude", "pi", "codex", REPLAY)
CODEX_NAMES = ("gpt-", "codex-")
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


def route(model: str, runner: str = "") -> tuple[str, str]:
    prefix, _, rest = model.partition(":")
    if prefix in RUNNERS and rest:
        return prefix, rest
    if runner in RUNNERS:
        return runner, model
    if "/" in model:
        return "pi", model
    return ("codex" if model.lower().startswith(CODEX_NAMES) else "claude"), model


def chosen(model: str, run_with: str, cfg: config.Config) -> tuple[str, str]:
    which, name = route(model, run_with)
    return (REPLAY if cfg.replay.all else which), name


def stands_for(model: str, run_with: str) -> tuple[str, str]:
    which, name = route(model, "" if run_with == REPLAY else run_with)
    return (which if which != REPLAY else route(name)[0]), name


def run_env(run_id: str) -> dict[str, str]:
    return {"ARETE_RUN_ID": run_id} if run_id else {}


def env_names(model: str, run_with: str, cfg: config.Config, repo: config.Repo | None, run_id: str) -> list[str]:
    which, _ = stands_for(model, run_with)
    passing = environment.passed(cfg, repo)
    if which == "pi":
        return sorted(environment.build(passing))
    if which == "codex":
        return sorted(codex.agent_env(passing, run_env(run_id)))
    return sorted(claude.agent_env(passing, run_env(run_id)))


def judge_note(cfg: config.Config, passing: list[str] | None = None, repo: config.Repo | None = None) -> str:
    passing = environment.passed(cfg, repo) if passing is None else passing
    if not (cfg.jev.guard and jev.ready(cfg, [repo])) or jev.key(claude.agent_env(passing)):
        return ""
    return (f"the guard has no judge: JEV's key is only in the server's environment, which an agent no longer gets, so "
            f"the guard judges no command and keeps only its secret file rules; put {jev.KEY_NAME} in "
            f"{paths.short_path(pi.KEYS_FILE)}")


def authentication(model: str, run_with: str = "", *, cfg: config.Config | None = None,
                   repo: config.Repo | None = None, device: devices.Device | str = "local") -> accounts.Auth:
    which, name = route(model, run_with)
    if which == REPLAY or (cfg and cfg.replay.all):
        return accounts.Auth()
    cfg = cfg or config.load()
    target = devices.resolve(device)
    passing = environment.passed(cfg, repo)
    remote = target if target.remote else None
    if which == "pi":
        return (accounts.Auth("api_key") if remote else
                pi_route(name, cfg, repo, target).auth)
    return (codex if which == "codex" else claude).authentication(passing, remote=remote)


def metered(model: str, run_with: str = "", **context) -> bool:
    which = route(model, run_with)[0]
    auth = authentication(model, run_with, **context)
    return not subscription(which, "", auth.mode)


def limit_applies(scope: str, model: str) -> bool:
    if scope.startswith("model:"):
        return scope == model_scope(model)
    return scope in (claude.EVERY_MODEL, codex.LIMIT_SCOPE) or scope in re.split(r"[-_/.:]", model.lower())


def auth_hold(auth: accounts.Auth, model: str, connect=state.db, unknown: str = "") -> dict | None:
    if not (auth.identity or auth.credential or unknown):
        return None
    scopes = [accounts.scope(auth, s, unknown) for s in limit_scopes(auth, model)]
    found = [h for h in state.holds(connect, include_indefinite=True) if h["scope"] in scopes]
    return next((h for h in found if not h["until"]), found[-1] if found else None)


def limit_scopes(auth: accounts.Auth, model: str) -> tuple[str, ...]:
    if auth.mode == "api_key":
        return ("credit",)
    if auth.family == "claude":
        return (claude.EVERY_MODEL, *(s for s in claude.LIMIT_SCOPES.values() if limit_applies(s, model)),
                model_scope(model))
    if auth.family == "codex":
        return (codex.LIMIT_SCOPE, model_scope(model))
    return (auth.provider or provider_of(model), model_scope(model))


def pi_route(model: str, cfg: config.Config, repo=None, device="local", connect=state.db,
             mode: str = "") -> pi.Route | None:
    if devices.resolve(device).remote:
        return None
    return pi.select(model, environment.passed(cfg, repo), cfg.environment.providers,
                     held=lambda auth, name: auth_hold(auth, name, connect), mode=mode,
                     local_providers=cfg.local_providers)


def usage_hold(model: str, run_with: str = "", connect=state.db, cfg: config.Config | None = None,
               repo: config.Repo | None = None, device: devices.Device | str = "local",
               auth: accounts.Auth | None = None, unknown: str = "") -> dict | None:
    which, name = route(model, run_with)
    if which == REPLAY or (cfg and cfg.replay.all):
        return None
    if auth is None and which == "pi":
        selected = pi_route(name, cfg or config.load(), repo, device, connect)
        auth = selected.auth if selected else accounts.Auth("api_key")
        name = selected.model if selected else name
    auth = auth or authentication(model, run_with, cfg=cfg, repo=repo, device=device)
    return auth_hold(auth, name, connect, unknown)


def hold_words(hold: dict) -> str:
    return (f"{hold['why']}, waiting until {local_time(hold['until'])}" if hold["until"] else
            f"{hold['why']}, held until cleared")


def held_result(held: dict, auths: set[accounts.Auth], auth: accounts.Auth, which: str, name: str,
                resume: Session, unknown: str) -> Result:
    billing = "credit" if auth.mode == "api_key" else "subscription"
    scope = next(s for a in auths for s in limit_scopes(a, name)
                 if accounts.scope(a, s, unknown) == held["scope"])
    return Result(ok=False, error=hold_words(held), runner=which, model=name,
                  provider=auth.provider or provider_of(name), auth_mode=auth.mode, session=resume,
                  usage_limit=UsageLimit(held["until"], scope, "credit" if billing == "credit" else "usage", billing))


def local_time(stamp: str) -> str:
    return util.parse_iso(stamp).astimezone().strftime("%a %b %d %H:%M")


def wait_out(scope: str, changed: Callable[[], bool] = lambda: False) -> None:
    while until := state.hold_until(scope):
        if changed():
            return
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
    which = route(model, run_with)[0]
    if which == "codex" and device.remote:
        return codex.RUNS_LOCALLY
    return SANDBOX_RUNS_PI if device.exe_sandbox and which == "claude" else ""


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
    status = claude.auth_status()
    if not status:
        return False, "claude auth status failed"
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


def codex_login() -> tuple[bool, str]:
    ok, detail, _ = codex.login()
    return ok, detail


def cost_note(res: Result) -> str:
    paid = ("subscription" if subscription(res.runner, res.provider, res.auth_mode) and not res.paid_usd
            else util.paid_note(res.paid_usd))
    return f"({util.count(res.turns, 'turn')}, {paid})"


def priced(res: Result, cfg: config.Config) -> float:
    if res.runner == REPLAY:
        return res.cost_usd
    if res.runner == "pi" and res.auth_mode == "subscription":
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
        repo: config.Repo | None = None, selected: pi.Route | None = None,
        remaining_budget: Callable[[], float | None] | None = None,
        on_launch: Callable[[pi.Route], None] = lambda selected: None, **kw) -> Result:
    cfg = cfg or config.load()
    which, name = chosen(model, runner or (resume.runner if not model else ""), cfg)
    if resume.runner != which:
        resume = NO_SESSION
    target = devices.LOCAL if which == REPLAY else devices.resolve(device)
    if target.remote:
        kw["remote"] = target
    elif which in ("claude", "pi", "codex"):
        repo = repo or cfg.repo_for_work_dir(cwd)
        kw["passing"] = environment.passed(cfg, repo)
    trace = trace if trace[0] else (os.environ.get("ARETE_RUN_ID", ""), trace[1] or "agent")
    record_id = trace[0] or record.new_id("call")
    if which == "claude":
        kw.pop("max_budget_usd", None)
        if not target.remote:
            kw = local_claude(kw, cfg, trace[0], ask_jev, say, repo)
    elif which == "codex":
        kw.pop("max_budget_usd", None)
        if not target.remote:
            kw = local_codex(kw, cfg, trace[0], say, repo)
    elif which == "pi":
        kw["price"] = lambda reply: priced(reply, cfg)
        if not target.remote:
            kw["providers"] = cfg.environment.providers
    limited: list[Result] = []
    invocation = secrets.token_hex(16)
    pinned = selected is not None
    while True:
        if which == "pi" and not target.remote:
            selected = selected or pi_route(name, cfg, repo, target)
            auth = selected.auth
            name = selected.model
            kw["selected"] = selected
        else:
            auth = authentication(name, which, cfg=cfg, repo=repo, device=target) if which != REPLAY else accounts.Auth()
        unknown = "local-" + accounts.fingerprint(invocation, auth.generation)
        auths = {auth, *(selected.refreshed_auths if selected else ())}
        active = [h for a in auths if (h := usage_hold(name, which, cfg=cfg, auth=a, unknown=unknown))]
        held = max(active, key=lambda h: (not h["until"], h["until"]), default=None)
        if held:
            if not wait_out_limits or not held["until"]:
                return with_spent(held_result(held, auths, auth, which, name, resume, unknown), limited)
            say(hold_words(held))
            with waits("limit"):
                wait_out(held["scope"], lambda auth=auth, name=name, pinned=pinned: not (which == "pi" and pinned) and
                         authentication(name, which, cfg=cfg, repo=repo, device=target) != auth)
            say(f"the {which} usage limit has lifted, going on")
            if not pinned:
                selected = None
            continue
        if auth.mode == "subscription":
            if which == "pi":
                kw["max_budget_usd"] = None
        elif remaining_budget:
            remaining = remaining_budget()
            if which == "pi":
                kw["max_budget_usd"] = remaining
            elif remaining is not None and remaining <= BUDGET_FLOOR_USD:
                kw["budget_error"] = "API-key budget is spent"
        if (auth.mode != "subscription" and kw.get("max_budget_usd") is not None
                and kw["max_budget_usd"] <= BUDGET_FLOOR_USD):
            kw["budget_error"] = "API-key budget is spent"
        started = util.iso()
        if selected:
            on_launch(selected)
        res = attempt(which, name, prompt, cwd, resume, cfg, trace, record_id, target, kw, tier, kind, auth)
        if not res.usage_limit:
            if res.ok and which != REPLAY:
                state.clear_holds([accounts.scope(a, s, unknown)
                                   for a in {auth, *(selected.refreshed_auths if selected else ())}
                                   for s in limit_scopes(a, name)], set_before=started)
            return with_spent(res, limited)
        limit = res.usage_limit
        why = f"{which} {limit.kind or 'usage'} limit reached"
        scope = accounts.scope(auth, limit.scope, unknown)
        for a in {auth, *(selected.refreshed_auths if selected else ())}:
            state.hold(accounts.scope(a, limit.scope, unknown), limit.until, why)
        if not wait_out_limits or not limit.until:
            return with_spent(res, limited)
        limited.append(res)
        if which == "pi":
            pinned = True
        say(hold_words({"why": why, "until": limit.until}))
        with waits("limit"):
            wait_out(scope, lambda auth=auth, name=name: which != "pi" and
                     authentication(name, which, cfg=cfg, repo=repo, device=target) != auth)
        say(f"the {which} usage limit has lifted, going on")
        if res.session.id and res.turns > 1 and kw.get("persist", True):
            prompt, resume = CONTINUE_AFTER_LIMIT, res.session


def with_spent(res: Result, earlier: list[Result]) -> Result:
    if earlier:
        res.aggregate_paid_usd = res.paid_usd + sum(r.paid_usd for r in earlier)
        res.auth_modes = tuple(dict.fromkeys(mode for r in [*earlier, res] for mode in r.modes))
    res.cost_usd += sum(r.cost_usd for r in earlier)
    res.tokens_in += sum(r.tokens_in for r in earlier)
    res.tokens_out += sum(r.tokens_out for r in earlier)
    res.cache_read += sum(r.cache_read for r in earlier)
    res.cache_write += sum(r.cache_write for r in earlier)
    res.api_seconds += sum(r.api_seconds for r in earlier)
    return res


def attempt(which: str, name: str, prompt: str, cwd: Path | str, resume: Session, cfg: config.Config,
            trace: tuple[str, str], record_id: str, target: devices.Device, kw: dict, tier: str = "",
            kind: str = "", auth: accounts.Auth | None = None) -> Result:
    auth = auth or (authentication(name, which, cfg=cfg, device=target) if which != REPLAY else accounts.Auth())
    header = {"runner": which, "model": name, "device": target.name, **origin(cwd, cfg, target), "cwd": str(cwd),
              "auth_mode": auth.mode,
              "provider": provider_of(name) or ("anthropic" if which == "claude" else
                                                ("openai" if auth.mode == "api_key" else "chatgpt")
                                                if which == "codex" else ""),
              "prompt": prompt, "system": kw.get("system", ""), "tools": kw.get("tools"),
              "allowed_tools": kw.get("allowed_tools"), "disallowed_tools": kw.get("disallowed_tools"),
              "json_schema": kw.get("json_schema"), "budget_usd": kw.get("max_budget_usd"),
              "timeout": kw.get("timeout"), "effort": kw.get("effort", ""),
              "resume": {"runner": resume.runner, "id": resume.id, "spent_usd": resume.spent_usd} if resume.id else None}
    found = secret_files([cwd, *(kw.get("add_dirs") or [])], target, cfg)
    selected = kw.get("selected")
    if selected:
        found.extend(selected.sensitive_files)
    hidden = list(record.secrets(*found))
    if selected:
        hidden.extend(selected.hidden)
        hidden[:] = sorted(set(hidden) | {json.dumps(v, ensure_ascii=False)[1:-1] for v in hidden},
                           key=len, reverse=True)
        header["env"] = pi.launch_env_names(selected)
        header["provider"] = selected.provider
    def refresh_hidden():
        if selected and selected.using_login:
            hidden.extend(v for v in [*pi.credential_values(accounts.read(selected.owner)),
                                     *tuple(selected.refreshed_secrets)] if len(v) >= 8
                          and v not in hidden)
        if selected:
            hidden.extend(v for path in selected.sensitive_files for v in record.file_secrets(path, every_value=True)
                          if len(v) >= 8 and v not in hidden)
        if selected:
            hidden[:] = sorted(set(hidden) | {json.dumps(v, ensure_ascii=False)[1:-1] for v in hidden},
                               key=len, reverse=True)
    def safe_line(line):
        refresh_hidden()
        rec.line(line)
    refresh_hidden()
    started = time.monotonic()
    with record.Recorder(record_id, trace[1], header, hidden) as rec:
        budget_error = kw.pop("budget_error", "")
        if budget_error:
            res = Result(ok=False, error=budget_error, session=resume, runner=which, model=name)
        elif which == REPLAY:
            res = replay.run(cwd, trace, cfg, rec.line, resume)
        elif which == "codex":
            res = codex.run(prompt, cwd, model=name, resume=resume, on_line=rec.line, hidden=found, auth=auth, **kw)
        else:
            res = (pi.run if which == "pi" else claude.run)(prompt, cwd, model=name, resume=resume,
                                                             on_line=safe_line if selected else rec.line,
                                                             auth=auth, **kw)
        res.runner = which
        res.model = res.model or name
        res.provider = res.provider or provider_of(res.model)
        if which != REPLAY:
            res.auth_mode = auth.mode
            if res.usage_limit and (res.usage_limit.scope not in limit_scopes(auth, name) or
                                   res.usage_limit.billing != ("subscription" if auth.mode == "subscription" else "credit")):
                res.usage_limit = None
        if res.calls:
            for call in res.calls:
                call.cost_usd = priced(call, cfg)
            res.cost_usd = sum(call.cost_usd for call in res.calls)
            res.aggregate_paid_usd = sum(call.paid_usd for call in res.calls)
        else:
            res.cost_usd = priced(res, cfg)
        if selected:
            refresh_hidden()
            res.error = record.scrub(res.error, hidden)
            res.text = record.scrub(res.text, hidden)
            res.structured = record.scrub_value(res.structured, hidden)
            for call in res.calls:
                call.error = record.scrub(call.error, hidden)
        rec.finish(res, time.monotonic() - started)
    if trace[0]:
        for call in res.calls or [res]:
            record.step(trace[0], trace[1], call, call.call_seconds if res.calls else time.monotonic() - started,
                        effort=kw.get("effort", ""), tier=tier, kind=kind)
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


def local_codex(kw: dict, cfg: config.Config, run_id: str, say: Callable[[str], None],
                repo: config.Repo | None) -> dict:
    kw = {**kw, "extra_env": run_env(run_id)}
    if cfg.jev.guard and guard.wanted(kw.get("tools")):
        kw["judge"] = True
        if note := judge_note(cfg, kw["passing"], repo):
            say(note)
    return kw


def runs_bash(kw: dict) -> bool:
    if (tools := kw.get("tools")) is not None:
        return "Bash" in tools
    return any(r == "Bash" or r.startswith("Bash(") for r in kw.get("allowed_tools") or [])
