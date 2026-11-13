from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
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
    prices,
    record,
    replay,
    state,
    util,
)
from . import scratch as verification_scratch
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
NO_ROUTE = "no route can run this call"
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


def env_names(model: str, run_with: str, cfg: config.Config, repo: config.Repo | None, run_id: str,
              extra: dict | None = None) -> list[str]:
    which, _ = stands_for(model, run_with)
    passing = environment.passed(cfg, repo)
    if which == "pi":
        return sorted({*environment.build(passing), *(extra or {})})
    if which == "codex":
        return sorted(codex.agent_env(passing, {**(extra or {}), **run_env(run_id)}))
    return sorted(claude.agent_env(passing, {**(extra or {}), **run_env(run_id)}))


def judge_note(cfg: config.Config, passing: list[str] | None = None, repo: config.Repo | None = None) -> str:
    passing = environment.passed(cfg, repo) if passing is None else passing
    if not (cfg.jev.guard and jev.ready(cfg, [repo])) or jev.key(claude.agent_env(passing)):
        return ""
    return (f"the guard has no judge: JEV's key is only in the server's environment, which an agent no longer gets, so "
            f"the guard judges no command and keeps only its secret file rules; put {jev.KEY_NAME} in "
            f"{paths.short_path(pi.KEYS_FILE)}")


def authentication(model: str, run_with: str = "", *, cfg: config.Config | None = None,
                   repo: config.Repo | None = None, device: devices.Device | str = "local",
                   mode: str = "") -> accounts.Auth:
    which, name = route(model, run_with)
    if which == REPLAY or (cfg and cfg.replay.all):
        return accounts.Auth()
    cfg = cfg or config.load()
    target = devices.resolve(device)
    passing = environment.passed(cfg, repo)
    remote = target if target.remote else None
    if which == "pi":
        return pi_route(name, cfg, repo, target, mode=mode).auth
    return (codex if which == "codex" else claude).authentication(passing, remote=remote)


def metered(model: str, run_with: str = "", *, cfg: config.Config | None = None, repo: config.Repo | None = None,
            device: devices.Device | str = "local", tools: list[str] | None = None) -> bool:
    from . import routing

    cfg = cfg or config.load()
    return routing.can_charge(routing.outlook(cfg, repo, device, model, run_with, tools, NO_GIT_WRITES))


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
        return pi.Route(provider_of(model), model, accounts.Auth(), error=pi.REMOTE_AUTH_UNVERIFIED)
    return pi.select(model, environment.passed(cfg, repo), cfg.environment.providers,
                     held=lambda auth, name: auth_hold(auth, name, connect), mode=mode,
                     local_providers=cfg.local_providers)


def usage_hold(model: str, run_with: str = "", connect=state.db, cfg: config.Config | None = None,
               repo: config.Repo | None = None, device: devices.Device | str = "local",
               tools: list[str] | None = None, remaining: float | None = None, denied=NO_GIT_WRITES) -> str:
    from . import routing

    cfg = cfg or config.load()
    return routing.held_words(routing.outlook(cfg, repo, device, model, run_with, tools, denied, remaining,
                                               connect))


def hold_words(hold: dict) -> str:
    return (f"{hold['why']}, waiting until {local_time(hold['until'])}" if hold["until"] else
            f"{hold['why']}, held until cleared")


def local_time(stamp: str) -> str:
    return util.parse_iso(stamp).astimezone().strftime("%a %b %d %H:%M")


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
    if which == "pi" and device.remote:
        return pi.REMOTE_AUTH_UNVERIFIED
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
        return (res.tokens_in * (rate[0] or 0) + res.tokens_out * (rate[1] or 0)) / 1_000_000
    if res.runner == "pi" and (res.provider or provider_of(res.model)) in cfg.local_providers:
        return 0.0
    return res.cost_usd


def run(prompt: str, cwd: Path | str, *, model: str = "", runner: str = "", trace: tuple[str, str] = ("", ""),
        resume: Session = NO_SESSION, cfg: config.Config | None = None, device: devices.Device | str = "local",
        wait_out_limits: bool = True, say: Callable[[str], None] = tell_stderr, ask_jev: bool = True, tier: str = "",
        kind: str = "", waits: Callable[[str], AbstractContextManager] = lambda cause: nullcontext(),
        repo: config.Repo | None = None, remaining_budget: Callable[[], float | None] | None = None,
        on_launch: Callable[[dict], None] = lambda route: None, settings=None, fallback: bool = True,
        fresh_prompt: Callable[[str], str] | None = None, try_no: int = 0, resume_only: bool = False,
        route: dict | None = None, refusal: str = "", mode: str = "", layer: config.RepoLayer | None = None,
        scratch: verification_scratch.Attempt | None = None, **kw) -> Result:
    cfg = cfg or config.load()
    run_with = runner or (resume.runner if not model else "")
    which, name = chosen(model, run_with, cfg)
    kept = resume if resume.runner == which else NO_SESSION
    target = devices.LOCAL if which == REPLAY else devices.resolve(device)
    trace = trace if trace[0] else (os.environ.get("ARETE_RUN_ID", ""), trace[1] or "agent")
    record_id = trace[0] or record.new_id("call")
    try_no = try_no or (state.step_tries(trace[0], trace[1]) + 1 if trace[0] else 1)
    prior = route
    chosen_by = {"launch": 1, "selection": prior} if prior else None
    atom = settings.step if settings else ""
    if refusal:
        return refused(which, name, prompt, cwd, resume, cfg, trace, record_id, target, kw, tier, kind,
                       chosen_by, try_no, refusal, atom)
    if which == REPLAY and (cfg.replay.all or not (settings and settings.requires.declared)):
        on_launch({"runner": which, "model": name, "effort": kw.get("effort", ""), "skipped": [], "launch": 1,
                   "env": env_names(model, run_with, cfg, repo, trace[0], kw.get("extra_env"))})
        return attempt(which, name, prompt, cwd, kept, cfg, trace, record_id, target, kw, tier, kind,
                       accounts.Auth(), {"launch": 1, **({"selection": route} if route else {})}, try_no,
                       atom)
    if not target.remote:
        repo = repo or cfg.repo_for_work_dir(cwd)
    size = routing_size(prompt, settings, kw)
    active = layer if layer is not None else layer_of(repo)
    trip = Trip(cfg, repo, target, request_for(cfg, repo, which, name, settings, fallback, kw, size, active), resume,
                remaining_budget, kw.get("max_budget_usd"), layer=active, scratch=scratch)
    from . import routing

    limited: list[Result] = []
    previous: Stopped | None = None
    launches, measured = 0, size
    while True:
        picked, skipped = pick(trip)
        if not picked:
            wake = wake_up(skipped)
            if wait_out_limits and wake:
                say(f"{NO_ROUTE}, waiting until {local_time(util.iso(wake))}: {route_reasons(skipped)}")
                with waits("limit"):
                    wait_until(trip, wake)
                continue
            done = exhausted(trip, skipped, limited, which, name, wake)
            if trip.request.requires.declared:
                given = {"launch": launches + 1, "skipped": skipped, "requested_effort": trip.request.effort,
                         "selection": launched_selection(trip, prior, skipped, measured, error=done.error)}
                res = refused(which, name, prompt, cwd, done.session, cfg, trace, record_id, target, kw, tier, kind,
                              given, try_no, done.error, atom)
                res.skipped, res.usage_limit = done.skipped, done.usage_limit
                return with_spent(res, limited)
            return with_spent(done, limited)
        chosen_route = picked.candidate
        text, session = session_for(trip, picked, prompt, fresh_prompt, previous, kw, record_id)
        launch_kw = launch_kwargs(kw, picked, trip, trace, ask_jev, say)
        final_size = routing_size(text, settings, launch_kw)
        measured = max(measured, final_size)
        if (trip.request.requires.declared and final_size > trip.request.size
                and (why := outgrown(trip, chosen_route, final_size))):
            trip.barred[routing.route_key(chosen_route)] = why
            continue
        if resume_only and not session.id:
            return with_spent(Result(ok=False, error=f"the kept session cannot continue on {chosen_route.runner} "
                                                      f"{picked.name}", runner=chosen_route.runner, model=picked.name,
                                     session=trip.resume), limited)
        launches += 1
        trip.launched = True
        selection = launched_selection(trip, prior, skipped, final_size, chosen_route, picked.name)
        route = {**({"selection": selection} if selection else {}),
                 "runner": chosen_route.runner, "model": picked.name,
                 "provider": picked.selected.provider if picked.selected else chosen_route.provider,
                 "auth_mode": picked.auth.mode, "credential": chosen_route.credential, "billing": chosen_route.billing,
                 "effort": chosen_route.effort, "requested_effort": trip.request.effort, "tier": chosen_route.tier,
                 "position": chosen_route.position, "requested": chosen_route.requested,
                 "env": launch_env(picked, cfg, repo, trace[0], kw.get("extra_env")), "skipped": skipped, "launch": launches,
                 **({"request_tokens": final_size} if trip.request.requires.declared else {})}
        on_launch(route)
        started = util.iso()
        with verification_scratch.producing(launch_kw.pop("verification_attempt", None)):
            res = attempt(chosen_route.runner, picked.name, text, cwd, session, cfg, trace, record_id, target, launch_kw,
                          tier, kind, picked.auth, route, try_no, atom)
        res.credential, res.effort, res.requested_effort, res.skipped = (
            chosen_route.credential, chosen_route.effort, trip.request.effort, skipped)
        if res.session.id:
            res.session = replace(res.session, credential=chosen_route.credential, account=account_of(picked.auth),
                                  generation=picked.auth.generation)
        if not res.usage_limit:
            if res.ok:
                state.clear_holds([accounts.scope(a, s, picked.unknown) for a in picked.auths
                                   for s in limit_scopes(a, picked.name)], set_before=started)
            return with_spent(res, limited)
        limit = res.usage_limit
        why = f"{chosen_route.runner} {limit.kind or 'usage'} limit reached"
        for a in picked.auths:
            state.hold(accounts.scope(a, limit.scope, picked.unknown), limit.until, why)
        limited.append(res)
        previous = Stopped(res, picked)
        key = routing.route_key(chosen_route)
        if picked.selected:
            trip.pinned[key] = picked.selected
        if limit.until:
            ends = util.parse_iso(limit.until)
            trip.floors[key] = (ends if ends > util.now() else util.now() + timedelta(seconds=HOLD_CHECK_SECONDS),
                                picked.auth)
        say(f"{hold_words({'why': why, 'until': limit.until})}, trying another route")


@dataclass
class Trip:
    cfg: config.Config
    repo: config.Repo | None
    target: devices.Device
    request: object
    resume: Session
    budget: Callable[[], float | None] | None
    max_budget: float | None
    invocation: str = field(default_factory=lambda: secrets.token_hex(16))
    pinned: dict = field(default_factory=dict)
    floors: dict = field(default_factory=dict)
    launched: bool = False
    scratch: verification_scratch.Attempt | None = None
    barred: dict = field(default_factory=dict)
    layer: config.RepoLayer | None = None


@dataclass
class Picked:
    candidate: object
    auth: accounts.Auth
    selected: pi.Route | None
    name: str
    unknown: str
    auths: set
    budget: float | None


@dataclass
class Stopped:
    res: Result
    picked: Picked


def reach(kw: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    granted = kw["tools"] if kw.get("tools") is not None else (kw.get("allowed_tools") or [])
    denied = tuple(dict.fromkeys(kw.get("disallowed_tools") or []))
    bare = tuple(t for t in dict.fromkeys(granted)
                 if "(" not in t and not t.startswith("mcp__") and t not in denied)
    return bare, denied


def routing_size(prompt: str, settings, kw: dict) -> int:
    from . import routing

    if not (settings and settings.requires.declared):
        return 0
    return routing.request_tokens(prompt, kw.get("system", ""), kw.get("json_schema"))


def request_for(cfg: config.Config, repo: config.Repo | None, which: str, name: str, settings, fallback: bool,
                kw: dict, size: int = 0, layer: config.RepoLayer | None = None):
    from . import routing, steps

    bare, denied = reach(kw)
    try:
        servers = json.loads(kw.get("mcp_config") or "{}").get("mcpServers") or {}
    except (ValueError, AttributeError):
        servers = {}
    effort = settings.effort if settings else kw.get("effort", "")
    given = kw.get("instructions")
    explicit = (tuple(settings.instructions) if settings.instructions_explicit else None) if settings else (
        None if given is None else tuple(given))
    requires = settings.requires if settings else routing.Requirements()
    if settings and settings.chain and (requires.declared or routing.identity(which, name) == routing.identity(
            settings.chain[0].runner, settings.chain[0].model)):
        chain, allowed = settings.chain, settings.fallback
    else:
        layer = layer if layer is not None else layer_of(repo)
        chain, allowed = steps.chain_for(cfg, layer, which, name, effort, explicit)
    return routing.Request(chain, allowed and fallback, bare, tuple(servers), effort, explicit, denied, requires, size)


def outgrown(trip: Trip, chosen, size: int) -> str:
    from . import routing

    discovery = routing.Discovery(trip.cfg, trip.repo, trip.target, layer=trip.layer)
    key = routing.route_key(chosen)
    again = discovery.routes(replace(trip.request, size=size), request_tokens=size)
    return next((c.detail for c in again if routing.route_key(c) == key and c.state == "ineligible"), "")


def launched_selection(trip: Trip, prior: dict | None, skipped: list[dict], size: int, chosen=None, name: str = "",
                       error: str = "") -> dict | None:
    from . import routing

    if not trip.request.requires.declared:
        return prior
    base = prior or routing.described_selection(routing.Selection(None, False, ""), trip.request, size)
    passed = [{"model": s["model"], "runner": s["runner"], "credential": s["credential"], "state": s["state"],
               "reason": s["detail"]} for s in skipped]
    seen = {(p["runner"], p["model"], p["credential"]) for p in passed}
    if chosen:
        seen.add((chosen.runner, chosen.model, chosen.credential))
    passed += [p for p in base.get("skipped") or [] if (p["runner"], p["model"], p["credential"]) not in seen]
    selected = None
    if chosen:
        why = "; ".join(f"{p['model']} ({p['runner']}, {p['credential']}) {p['state']}: {p['reason']}" for p in passed)
        reason = ("the requested route meets the step's requirements" if chosen.requested and not why
                  else f"took {name}" + (f" after passing over {why}" if why else ""))
        selected = {"model": name, "runner": chosen.runner, "provider": chosen.provider,
                    "credential": chosen.credential, "billing": chosen.billing, "reason": reason}
    return {**base, "selected": selected, "skipped": passed, "request_tokens": size, "error": error}


def refused(which: str, name: str, prompt: str, cwd: Path | str, session: Session, cfg: config.Config,
            trace: tuple[str, str], record_id: str, target: devices.Device, kw: dict, tier: str, kind: str,
            route: dict | None, try_no: int, error: str, atom: str = "") -> Result:
    return attempt(which, name, prompt, cwd, session, cfg, trace, record_id, target,
                   {**kw, **({"remote": target} if target.remote else {}), "budget_error": error},
                   tier, kind, accounts.Auth(), route, try_no, atom)


def layer_of(repo: config.Repo | None):
    try:
        return config.load_layer(repo.layer_dir) if repo else None
    except (ValueError, OSError):
        return None


def passed_over(found, why: str, detail: str, until: str = "") -> dict:
    return {"runner": found.runner, "model": found.model, "credential": found.credential, "state": why,
            "detail": detail, "until": until}


def pick(trip: Trip) -> tuple[Picked | None, list[dict]]:
    from . import routing

    discovery = routing.Discovery(trip.cfg, trip.repo, trip.target, layer=trip.layer)
    found = discovery.routes(trip.request, request_tokens=trip.request.size)
    wanted = trip.resume
    if wanted.id and not trip.launched:
        found.sort(key=lambda c: not (c.runner == wanted.runner and (not wanted.credential
                                                                    or c.credential == wanted.credential)))
    skipped: list[dict] = []
    for candidate in found:
        key = routing.route_key(candidate)
        if key in trip.barred:
            skipped.append(passed_over(candidate, "ineligible", trip.barred[key]))
            continue
        remaining = None
        if candidate.billing == "paid" and candidate.state in ("ready", "held"):
            remaining = trip.budget() if trip.budget else trip.max_budget
            if remaining is not None and remaining <= BUDGET_FLOOR_USD:
                skipped.append(passed_over(candidate, "budget_spent", "API-key budget is spent"))
                continue
        if candidate.state != "ready":
            skipped.append(passed_over(candidate, candidate.state, candidate.detail, candidate.until))
            continue
        selected = None
        if candidate.runner == "pi":
            selected = trip.pinned.get(key) or discovery.probe(candidate.model, candidate.mode)
            auth, name = selected.auth, selected.model
        elif candidate.runner == "claude":
            auth, name = discovery.claude_auth(), candidate.model
        elif candidate.runner == "codex":
            auth, name = discovery.codex_auth(), candidate.model
        else:
            auth, name = accounts.Auth(), candidate.model
        if why := discovery.policy_refusal(candidate.credential, auth.mode):
            skipped.append(passed_over(candidate, "policy", why))
            continue
        unknown = "local-" + accounts.fingerprint(trip.invocation, auth.generation)
        auths = {auth, *(selected.refreshed_auths if selected else ())}
        active = [h for a in auths if (h := auth_hold(a, name, unknown=unknown))]
        held = max(active, key=lambda h: (not h["until"], h["until"]), default=None)
        if held:
            skipped.append(passed_over(candidate, "held", hold_words(held), held["until"]))
            continue
        floor = trip.floors.get(key)
        if floor and floor[0] > util.now() and floor[1] == auth:
            skipped.append(passed_over(candidate, "held", f"limit reported, rechecking after {local_time(util.iso(floor[0]))}",
                                       util.iso(floor[0])))
            continue
        return Picked(candidate, auth, selected, name, unknown, auths, remaining), skipped
    return None, skipped


def wake_up(skipped: list[dict]):
    now = util.now()
    return min((when for s in skipped if s["state"] == "held" and s["until"]
                and (when := util.parse_iso(s["until"])) > now), default=None)


def wait_until(trip: Trip, wake) -> None:
    while (left := (wake - util.now()).total_seconds()) > 0:
        time.sleep(max(1.0, min(HOLD_CHECK_SECONDS, left)))
        if pick(trip)[0]:
            return


def route_reasons(skipped: list[dict]) -> str:
    return "; ".join(f"{s['runner']} {s['model']} ({s['credential']}) {s['state']}: {s['detail']}" for s in skipped)


def exhausted(trip: Trip, skipped: list[dict], limited: list[Result], which: str, name: str, wake) -> Result:
    held = any(s["state"] == "held" for s in skipped)
    return Result(ok=False, error=" ".join(f"{NO_ROUTE}: {route_reasons(skipped)}".split()),
                  runner=which, model=name, provider=provider_of(name),
                  session=limited[-1].session if limited else trip.resume, skipped=skipped,
                  usage_limit=UsageLimit(util.iso(wake) if wake else "", "", "usage") if held else None)


def out_of_budget(res: Result) -> bool:
    return (not res.ok and res.error.startswith(NO_ROUTE)
            and any(s["state"] == "budget_spent" for s in res.skipped))


def account_of(auth: accounts.Auth) -> str:
    who = auth.identity or auth.credential
    return accounts.fingerprint(auth.mode, auth.family, auth.provider, who) if who else ""


def continues(stopped: Stopped, picked: Picked, persist: bool) -> bool:
    before, now = stopped.picked.auth, picked.auth
    res = stopped.res
    return (stopped.picked.candidate.runner == picked.candidate.runner and bool(res.session.id) and res.turns > 1
            and persist and (before == now or (bool(account_of(before)) and account_of(before) == account_of(now))
                             or state.refresh_continues(before.generation, now.generation)))


def session_for(trip: Trip, picked: Picked, prompt: str, fresh_prompt: Callable[[str], str] | None,
                previous: Stopped | None, kw: dict, record_id: str) -> tuple[str, Session]:
    which = picked.candidate.runner
    saved = trip.resume
    usable = saved.id and saved.runner == which and (
        not saved.account or saved.account == account_of(picked.auth)
        or state.refresh_continues(saved.generation, picked.auth.generation))
    fresh = fresh_prompt(which) if fresh_prompt and saved.id else prompt
    if previous is None:
        return (prompt, saved) if usable else (fresh, NO_SESSION)
    if continues(previous, picked, kw.get("persist", True)):
        return CONTINUE_AFTER_LIMIT, previous.res.session
    stopped = previous.res
    last = (record.last_message(paths.record_dir(record_id) / stopped.record, claude.USAGE_LIMIT_TEXT)
            if stopped.record else "") or stopped.text
    note = (f"An earlier attempt on {stopped.runner} {stopped.model} stopped at a limit. Its work is in the working "
            "folder; inspect it, then carry on and finish the task.")
    if last:
        note += f"\n\nIts last message:\n\n{last.strip()[-4000:]}"
    return f"{fresh}\n\n## Interrupted attempt\n\n{note}", NO_SESSION


def launch_kwargs(base: dict, picked: Picked, trip: Trip, trace: tuple[str, str], ask_jev: bool,
                  say: Callable[[str], None]) -> dict:
    kw = {k: list(v) if isinstance(v, list) else v for k, v in base.items()}
    cfg, repo, target = trip.cfg, trip.repo, trip.target
    which = picked.candidate.runner
    kw["effort"] = picked.candidate.effort
    kw["instructions"] = list(picked.candidate.instructions)
    if target.remote:
        kw["remote"] = target
    else:
        kw["passing"] = environment.passed(cfg, repo)
    hook = ()
    kw.pop("verification_attempt", None)
    if trip.scratch:
        try:
            hook = verification_scratch.activate(trip.scratch, which, bool(target.remote), kw.get("add_dirs", ()),
                                                 kw.get("mcp_config", ""))
        except (OSError, ValueError, KeyError, TypeError):
            hook = ()
        if not hook:
            say("trusted scratch authority unavailable for this confinement; fixture-dependent checks may be blocked")
            kw["system"] = "\n\n".join(filter(None, [kw.get("system", ""), verification_scratch.note(None)]))
        kw["verification_attempt"] = trip.scratch
    if which == "claude":
        kw.pop("max_budget_usd", None)
        if not target.remote:
            kw = local_claude(kw, cfg, trace[0], ask_jev, say, repo, hook)
    elif which == "codex":
        kw.pop("max_budget_usd", None)
        if not target.remote:
            kw = local_codex(kw, cfg, trace[0], say, repo, hook)
    elif which == "pi":
        kw["price"] = lambda reply: priced(reply, cfg)
        kw["max_budget_usd"] = picked.budget
        kw["selected"] = picked.selected
        if not target.remote:
            kw["providers"] = cfg.environment.providers
    return kw


def launch_env(picked: Picked, cfg: config.Config, repo: config.Repo | None, run_id: str,
               extra: dict | None = None) -> list[str]:
    if picked.selected:
        return sorted({*pi.launch_env_names(picked.selected), *(extra or {})})
    passing = environment.passed(cfg, repo)
    return sorted((codex if picked.candidate.runner == "codex" else claude).agent_env(
        passing, {**(extra or {}), **run_env(run_id)}))


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
            kind: str = "", auth: accounts.Auth | None = None, route: dict | None = None, try_no: int = 0,
            atom: str = "") -> Result:
    auth = auth or (authentication(name, which, cfg=cfg, device=target) if which != REPLAY else accounts.Auth())
    route = route or {}
    header = {"runner": which, "model": name, "device": target.name, **origin(cwd, cfg, target), "cwd": str(cwd),
              "auth_mode": auth.mode, "credential": route.get("credential", ""), "billing": route.get("billing", ""),
              "requested_effort": route.get("requested_effort", ""), "tier": route.get("tier", tier),
              "position": route.get("position", 0), "requested": route.get("requested", True),
              "launch": route.get("launch", 1), "skipped": route.get("skipped", []),
              "provider": provider_of(name) or ("anthropic" if which == "claude" else
                                                ("openai" if auth.mode == "api_key" else "chatgpt")
                                                if which == "codex" else ""),
              "prompt": prompt, "system": kw.get("system", ""), "tools": kw.get("tools"),
              "allowed_tools": kw.get("allowed_tools"), "disallowed_tools": kw.get("disallowed_tools"),
              "json_schema": kw.get("json_schema"), "budget_usd": kw.get("max_budget_usd"),
              "timeout": kw.get("timeout"), "effort": kw.get("effort", ""),
              "resume": {"runner": resume.runner, "id": resume.id, "spent_usd": resume.spent_usd} if resume.id else None}
    if atom:
        header["atom"] = atom
    if route.get("selection"):
        header["route"] = route
    found = secret_files([cwd, *(kw.get("add_dirs") or [])], target, cfg)
    selected = kw.get("selected")
    if selected:
        found.extend(selected.sensitive_files)
    hidden = list(record.secrets(*found))
    if selected:
        hidden.extend(selected.hidden)
        hidden[:] = sorted(set(hidden) | {json.dumps(v, ensure_ascii=False)[1:-1] for v in hidden},
                           key=len, reverse=True)
        header["env"] = sorted({*pi.launch_env_names(selected), *(kw.get("extra_env") or {})})
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
    at = util.now()
    with record.Recorder(record_id, trace[1], header, hidden) as rec:
        budget_error = kw.pop("budget_error", "")
        if budget_error:
            res = Result(ok=False, error=budget_error, session=resume, runner=which, model=name)
        elif which == REPLAY:
            res = replay.run(cwd, trace, cfg, rec.line, resume, kw.get("timeout"))
        elif which == "codex":
            protected = [verification_scratch.AUTHORITY, paths.WORKTREES_DIR]
            res = codex.run(prompt, cwd, model=name, resume=resume, on_line=rec.line,
                            hidden=[*found, *protected], auth=auth, **kw)
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
        on_local = route.get("credential") == "local"
        if res.calls:
            for call in res.calls:
                call.cost_usd = 0.0 if on_local else priced(call, cfg)
            res.cost_usd = sum(call.cost_usd for call in res.calls)
            res.aggregate_paid_usd = sum(call.paid_usd for call in res.calls)
        else:
            res.cost_usd = 0.0 if on_local else priced(res, cfg)
        if not budget_error:
            quote_calls(res, which, name, at)
        if selected:
            refresh_hidden()
            res.error = record.scrub(res.error, hidden)
            res.text = record.scrub(res.text, hidden)
            res.structured = record.scrub_value(res.structured, hidden)
            for call in res.calls:
                call.error = record.scrub(call.error, hidden)
        res.effort, res.credential = kw.get("effort", ""), route.get("credential", "")
        res.requested_effort, res.skipped = route.get("requested_effort", ""), route.get("skipped", [])
        res.record = rec.path.name if rec.path else ""
        rec.finish(res, time.monotonic() - started)
    if trace[0]:
        for call in res.calls or [res]:
            record.step(trace[0], trace[1], call, call.call_seconds if res.calls else time.monotonic() - started,
                        effort=kw.get("effort", ""), tier=tier, kind=kind, try_no=try_no,
                        auth_mode="local" if route.get("credential") == "local" else "")
    return res


def quote_calls(res: Result, which: str, name: str, at: datetime) -> None:
    snapshot = prices.load()
    for call in res.calls or [res]:
        call.quote = prices.applied(snapshot, which, (call.model, name), at,
                                    call.tokens_in if call.turns <= 1 else None)
        call.reference_usd = prices.reference_usd(call.quote, call.tokens_in, call.tokens_out, call.cache_read,
                                                  call.cache_write)
    if res.calls:
        res.quote = res.calls[-1].quote
        known = [call.reference_usd for call in res.calls]
        res.reference_usd = None if None in known else round(sum(known), 6)


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
                 repo: config.Repo | None, scratch: tuple[str, ...] = ()) -> dict:
    kw = {**kw, "extra_env": {**(kw.get("extra_env") or {}), **run_env(run_id)}}
    if cfg.jev.guard and guard.wanted(kw.get("tools")):
        kw["settings"] = guard.settings()
        if scratch:
            for group in kw["settings"]["hooks"]["PreToolUse"]:
                for hook in group["hooks"]:
                    hook["command"] = verification_scratch.hook_command(hook["command"], list(scratch))
        if runs_bash(kw) and (note := judge_note(cfg, kw["passing"], repo)):
            say(note)
    if ask_jev and runs_bash(kw) and jev.ready(cfg, [repo], env=claude.agent_env(kw["passing"])):
        kw["allowed_tools"] = [*(kw.get("allowed_tools") or []), ask.rule()]
        kw["system"] = "\n\n".join(filter(None, [kw.get("system", ""), ask.note()]))
    return kw


def local_codex(kw: dict, cfg: config.Config, run_id: str, say: Callable[[str], None],
                repo: config.Repo | None, scratch: tuple[str, ...] = ()) -> dict:
    kw = {**kw, "extra_env": {**(kw.get("extra_env") or {}), **run_env(run_id)}}
    if cfg.jev.guard and guard.wanted(kw.get("tools")):
        kw["judge"] = True
        if scratch:
            kw["scratch"] = tuple(scratch)
        if note := judge_note(cfg, kw["passing"], repo):
            say(note)
    return kw


def runs_bash(kw: dict) -> bool:
    if (tools := kw.get("tools")) is not None:
        return "Bash" in tools
    return any(r == "Bash" or r.startswith("Bash(") for r in kw.get("allowed_tools") or [])
