from __future__ import annotations

import json
import os
import secrets
import shlex
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import (
    accounts,
    browser,
    claude,
    codex,
    config,
    context,
    devices,
    environment,
    guard,
    jev,
    jev_ask,
    owner_asks,
    paths,
    pi,
    prices,
    record,
    replay,
    routing,
    sandbox,
    scrub,
    steps,
    uploads,
    util,
)
from . import scratch as verification_scratch
from .agent import (
    NO_SESSION,
    READ_ONLY_TOOLS,
    REPLAY,
    TRAITS,
    USAGE,
    Cut,
    Result,
    Session,
    UsageLimit,
    streamed_turn,
    subscription,
    token_cost,
    traits_of,
)
from .store import devices as devices_store
from .store import runs as run_store

SECRET_SEARCH_SKIPS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__"})
SECRET_SEARCH_DEPTH = 4
KNOWN_TOOLS = READ_ONLY_TOOLS | routing.WRITING_TOOLS | {"Bash"}
LOGIN_WARNING_DAYS = 7
HOLD_CHECK_SECONDS = 60
NO_ROUTE = "no route can run this call"
SANDBOX_START_ERROR = "sandbox-exec: sandbox_apply: operation not permitted"
HEADROOM_STATE = "headroom"
CONTINUE_AFTER_LIMIT = "The usage limit cut the last step short. Carry on from where you stopped and finish the step."


def short_of_headroom(headroom: dict, tokens_left: int, dollars_left: float | None) -> str:
    if tokens_left < headroom["tokens"]:
        return f"codex_headroom.tokens is {headroom['tokens']} and {tokens_left} tokens are left"
    if "spend_usd" in headroom and dollars_left is not None and dollars_left < headroom["spend_usd"]:
        return f"codex_headroom.spend_usd is ${headroom['spend_usd']:g} and ${dollars_left:.4f} is left"
    return ""


def codex_refusal(which: str, headroom: dict | None, tokens: Callable[[], int],
                  budget: float | None) -> tuple[str, str]:
    if not (why := traits_of(which).cannot_stop):
        return "", ""
    if headroom is None:
        return "ineligible", why
    return HEADROOM_STATE, short_of_headroom(headroom, tokens(), budget)


def web_tools(model: str, run_with: str) -> tuple[str, ...]:
    return traits_of(routing.stands_for(model, run_with)[0]).web_tools


def run_env(run_id: str) -> dict[str, str]:
    return {"ARETE_RUN_ID": run_id} if run_id else {}


def env_names(model: str, run_with: str, cfg: config.Config, repo: config.Repo | None, run_id: str,
              extra: dict | None = None) -> list[str]:
    which, _ = routing.stands_for(model, run_with)
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
            f"the guard judges no command; its directory floor and secret file rules remain active; put {jev.KEY_NAME} in "
            f"{paths.short_path(pi.KEYS_FILE)}")


def authentication(model: str, run_with: str = "", *, cfg: config.Config | None = None,
                   repo: config.Repo | None = None, device: devices.Device | str = "local",
                   mode: str = "") -> accounts.Auth:
    which, name = routing.route(model, run_with)
    if which == REPLAY or (cfg and cfg.replay.all):
        return accounts.Auth()
    cfg = cfg or config.load()
    target = devices.resolve(device)
    passing = environment.passed(cfg, repo)
    remote = target if target.remote else None
    if which == "pi":
        return routing.pi_route(name, cfg, repo, target, mode=mode).auth
    return (codex if which == "codex" else claude).authentication(passing, remote=remote)


def tell_stderr(text: str) -> None:
    print(f"{util.iso()} {text}", file=sys.stderr, flush=True)


def agent_problems(run_with: str, tools: list[str]) -> list[str]:
    problems = []
    if run_with and run_with not in routing.RUNNERS:
        problems.append(f"runner '{run_with}' is not one of {', '.join(routing.RUNNERS)}")
    if unknown := sorted(set(tools) - KNOWN_TOOLS):
        problems.append(f"unknown tools {', '.join(unknown)}; use {', '.join(sorted(KNOWN_TOOLS))}")
    return problems


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


def start_failure(res: Result, role: str) -> dict | None:
    if res.ok:
        return None
    error = res.error.lower()
    if not res.turns and SANDBOX_START_ERROR in error:
        return {"reason": "sandbox-start"}
    if not res.turns and res.runner in routing.RUNNERS and error.startswith(f"{res.runner} not found:"):
        return {"reason": "runner-start", "runner": res.runner}
    if error.startswith(NO_ROUTE):
        targets = [[r["runner"], r["model"], r["credential"]] for r in res.skipped
                   if r.get("state") in ("missing_login", "expired_login")]
        if targets:
            return {"reason": "runner-login", "role": role, "routes": targets}
    if (res.runner in ("codex", "claude") and res.credential == "cli_login" and res.auth_generation
            and (res.raw.get("api_error") in ("authentication_failed", "authentication_error")
                 or any(phrase in error for phrase in ("authentication failed", "authentication_error",
                                                       "failed to authenticate", "401 unauthorized", "not logged in")))):
        return {"reason": "runner-login", "role": role, "routes": [[res.runner, res.model, res.credential]],
                "generation": res.auth_generation}
    return None


def started_now(which: str) -> bool:
    module = {"claude": claude, "codex": codex, "pi": pi}.get(which)
    return module is not None and util.sh([module.binary(), "--version"], timeout=10).returncode == 0


def login_restored(cfg: config.Config, repo: config.Repo, layer: config.RepoLayer, kind: str,
                   issue: str, failure: dict) -> bool:
    setting = steps.resolve(cfg, layer, failure["role"], kind, ticket=(repo.name, issue))
    found = routing.Discovery(cfg, repo, priced=False, layer=layer)
    for route in found.routes(setting):
        if route.state != "ready" or [route.runner, route.model, route.credential] not in failure["routes"]:
            continue
        if route.runner == "codex" and route.credential == "cli_login" and not codex_login()[0]:
            continue
        generation = failure.get("generation")
        if not generation:
            return True
        auth = authentication(route.model, route.runner, cfg=cfg, repo=repo)
        if auth.generation and auth.generation != generation:
            return True
    return False


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
    if res.runner == "pi" and (res.provider or routing.provider_of(res.model)) in cfg.local_providers:
        return 0.0
    return res.cost_usd


def admitted(kw: dict, limits: dict | None) -> dict:
    if limits is None:
        return kw
    if type(limits) is not dict or limits.keys() - {"timeout", "max_tokens", "max_budget_usd"}:
        raise ValueError("launch admission may narrow only timeout, tokens and spend")
    for key, value in limits.items():
        if util.nonnegative_number(value) is None or value <= 0 or (key == "max_tokens" and type(value) is not int):
            raise ValueError("launch limits must be positive finite numbers, with whole tokens")
    return {**kw, **{key: min(value, kw[key]) if kw.get(key) is not None else value
                    for key, value in limits.items()}}


def run(prompt: str, cwd: Path | str, *, model: str = "", runner: str = "", trace: tuple[str, str] = ("", ""),
        resume: Session = NO_SESSION, cfg: config.Config | None = None, device: devices.Device | str = "local",
        wait_out_limits: bool = True, say: Callable[[str], None] = tell_stderr, ask_jev: bool = True, tier: str = "",
        kind: str = "", waits: Callable[[str], AbstractContextManager] = lambda cause: nullcontext(),
        repo: config.Repo | None = None, remaining_budget: Callable[[], float | None] | None = None,
        on_launch: Callable[[dict], dict | None] = lambda route: None, settings=None, fallback: bool = True,
        fresh_prompt: Callable[[str], str] | None = None, try_no: int = 0, resume_only: bool = False,
        route: dict | None = None, refusal: str = "", mode: str = "", layer: config.RepoLayer | None = None,
        scratch: verification_scratch.Attempt | None = None, remaining_tokens: Callable[[], int] | None = None,
        hard_cap: bool = False, needs_server: bool = False, codex_headroom: dict | None = None,
        max_steps: int | None = None, claude_tools: tuple[str, ...] = (), **kw) -> Result:
    from .store import factory as factory_state

    factory_state.admit()
    cfg = cfg or config.load()
    if settings and settings.requires.delegation_only:
        kw.update(tools=[], allowed_tools=[], disallowed_tools=[], instructions=[], mcp_config="", add_dirs=[],
                  ask_owner=False, persist=False)
        resume = NO_SESSION
        ask_jev = False
        if kw.get("json_schema") is not None:
            raise ValueError("controller JSON validation belongs to the engine, without hidden restatement calls")
    if max_steps is not None:
        kw["action_limit"] = ActionLimit(max_steps)
    run_with = runner or (resume.runner if not model else "")
    which, name = routing.chosen(model, run_with, cfg)
    kept = resume if resume.runner == which else NO_SESSION
    target = devices.LOCAL if which == REPLAY else devices.resolve(device)
    trace = trace if trace[0] else (os.environ.get("ARETE_RUN_ID", ""), trace[1] or "agent")
    record_id = trace[0] or record.new_id("call")
    try_no = try_no or (run_store.step_tries(trace[0], trace[1]) + 1 if trace[0] else 1)
    prior = route
    chosen_by = {"launch": 1, "selection": prior} if prior else None
    atom = settings.step if settings else ""
    refusal = refusal or guard.launch_refusal(trace[0], cwd, kw.get("add_dirs") or [])
    if refusal:
        return refused(which, name, prompt, cwd, resume, cfg, trace, record_id, target, kw, tier, kind,
                       chosen_by, try_no, refusal, atom)
    if which == REPLAY and (cfg.replay.all or not (settings and settings.requires.declared)):
        if resume_only and not kept.id:
            return unresumable(which, name, resume)
        if remaining_tokens:
            stood = routing.stands_for(model, run_with)[0]
            verdict, why = codex_refusal(stood, codex_headroom, remaining_tokens,
                                         remaining_budget() if remaining_budget else kw.get("max_budget_usd"))
            if why:
                skipped = [{"runner": stood, "model": name, "credential": "replay", "state": verdict, "detail": why,
                            "until": ""}]
                return Result(ok=False, runner=which, model=name, session=resume, skipped=skipped,
                              error=" ".join(f"{NO_ROUTE}: {route_reasons(skipped)}".split()))
        kw = admitted(kw, on_launch({"runner": which, "model": name, "effort": kw.get("effort", ""), "skipped": [],
                       "launch": 1, "env": env_names(model, run_with, cfg, repo, trace[0], kw.get("extra_env"))}))
        text = fresh_prompt(which) if fresh_prompt and not kept.id else prompt
        if remaining_tokens:
            remaining = remaining_tokens()
            kw = {**kw, "max_tokens": min(kw.get("max_tokens") or remaining, remaining)}
        return attempt(which, name, text, cwd, kept, cfg, trace, record_id, target, kw, tier, kind,
                       accounts.Auth(), {"launch": 1, **({"selection": route} if route else {})}, try_no,
                       atom)
    if not target.remote:
        repo = repo or cfg.repo_for_work_dir(cwd)
    size = routing.routing_size(prompt, settings, kw)
    active = layer if layer is not None else routing.layer_of(repo)
    trip = Trip(cfg, repo, target,
                routing.request_for(cfg, repo, which, name, settings, fallback, kw, size, active), resume,
                remaining_budget, kw.get("max_budget_usd"), layer=active, scratch=scratch,
                tokens=remaining_tokens, structured=kw.get("json_schema") is not None,
                hard_cap=hard_cap, needs_server=needs_server, headroom=codex_headroom,
                private_worker=bool(kw.get("private_worker")))
    limited: list[Result] = []
    previous: Stopped | None = None
    launches, measured = 0, size
    while True:
        if hard_cap and kw.get("max_budget_usd") is not None:
            trip.max_budget = kw["max_budget_usd"] - sum(r.paid_usd for r in limited)
        picked, skipped = pick(trip)
        if not picked:
            wake = wake_up(skipped)
            if wait_out_limits and wake:
                say(f"{NO_ROUTE}, waiting until {routing.local_time(util.iso(wake))}: {route_reasons(skipped)}")
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
        if kw.get("native_observation") and (target.remote or chosen_route.runner != "pi"
                                             or picked.auth.mode != "subscription"):
            return Result(ok=False, runner=chosen_route.runner, model=name,
                          error="native observation requires local Pi subscription")
        text, session = session_for(trip, picked, prompt, fresh_prompt, previous, kw, record_id)
        launch_kw = launch_kwargs(kw, picked, trip, trace, ask_jev, say, cwd=cwd, claude_tools=claude_tools)
        openrouter = routing.claude_openrouter(chosen_route.runner, chosen_route.provider)
        config_run = session.openrouter_run or record_id
        if openrouter:
            launch_kw["openrouter_config"] = paths.RUNS_DIR / config_run / "claude-openrouter"
        final_size = routing.routing_size(text, settings, launch_kw)
        measured = max(measured, final_size)
        if (trip.request.requires.declared and final_size > trip.request.size
                and (why := outgrown(trip, chosen_route, final_size))):
            trip.barred[routing.route_key(chosen_route)] = why
            continue
        if resume_only and not session.id:
            return with_spent(unresumable(chosen_route.runner, picked.name, trip.resume), limited)
        launches += 1
        trip.launched = True
        selection = launched_selection(trip, prior, skipped, final_size, chosen_route, picked.name)
        route = {**({"selection": selection} if selection else {}),
                 "runner": chosen_route.runner, "model": picked.name,
                 "provider": picked.selected.provider if picked.selected else chosen_route.provider,
                 "auth_mode": picked.auth.mode, "credential": chosen_route.credential, "billing": chosen_route.billing,
                 "effort": chosen_route.effort, "requested_effort": trip.request.effort, "tier": chosen_route.tier,
                 "position": chosen_route.position, "requested": chosen_route.requested,
                 "env": launch_env(picked, cfg, repo, trace[0], launch_kw.get("extra_env"),
                                   launch_kw.get("ask_owner", False),
                                   openrouter_config=launch_kw.get("openrouter_config")),
                 "skipped": skipped, "launch": launches,
                 **({"request_tokens": final_size} if trip.request.requires.declared else {})}
        launch_kw = admitted(launch_kw, on_launch(route))
        account = account_of(picked.auth)
        with ExitStack() as after_attempt:
            if account and (picked.selected is None or picked.selected.using_login or picked.selected.key_available):
                local_claude = sys.platform == "linux" and chosen_route.runner == "claude" and not target.remote
                if accounts.record_resolved(picked.auth, defer_unconfigured=local_claude):
                    after_attempt.callback(enroll_after_attempt, picked)
            started = util.iso()
            with verification_scratch.producing(launch_kw.pop("verification_attempt", None)):
                res = attempt(chosen_route.runner, picked.name, text, cwd, session, cfg, trace, record_id, target,
                              launch_kw, tier, kind, picked.auth, route, try_no, atom,
                              openrouter_quote=chosen_route.price if openrouter else None)
        res.credential, res.effort, res.requested_effort, res.skipped = (
            chosen_route.credential, chosen_route.effort, trip.request.effort, skipped)
        if res.session.id:
            res.session = replace(res.session, credential=chosen_route.credential, account=account,
                                  generation=picked.auth.generation)
            if openrouter:
                res.session = replace(res.session, openrouter_model=picked.name, openrouter_run=config_run)
        if not res.usage_limit:
            if res.ok:
                devices_store.clear_holds([accounts.scope(a, s, picked.unknown) for a in picked.auths
                                   for s in routing.limit_scopes(a, picked.name)], set_before=started)
            return with_spent(res, limited)
        limit = res.usage_limit
        why = (f"{chosen_route.runner} model overloaded" if limit.kind == "overloaded"
               else f"{chosen_route.runner} provider could not be reached" if limit.kind == "unreachable"
               else f"{chosen_route.runner} {limit.kind or 'usage'} limit reached")
        for a in picked.auths:
            devices_store.hold(accounts.scope(a, limit.scope, picked.unknown), limit.until, why)
        limited.append(res)
        previous = Stopped(res, picked)
        key = routing.route_key(chosen_route)
        if picked.selected:
            trip.pinned[key] = picked.selected
        if limit.until:
            ends = util.parse_iso(limit.until)
            trip.floors[key] = (ends if ends > util.now() else util.now() + timedelta(seconds=HOLD_CHECK_SECONDS),
                                picked.auth)
        say(f"{routing.hold_words({'why': why, 'until': limit.until})}, trying another route")


def enroll_after_attempt(picked: Picked) -> None:
    accounts.record_resolved(picked.auth, wait_for_activation=True)
    if picked.selected:
        for refreshed in picked.selected.refreshed_auths:
            if not refreshed.identity:
                accounts.record_resolved(refreshed, refresh_only=True, wait_for_activation=True)


def unresumable(which: str, name: str, resume: Session) -> Result:
    return Result(ok=False, error=f"the kept session cannot continue on {which} {name}", runner=which, model=name,
                  session=resume)


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
    tokens: Callable[[], int] | None = None
    structured: bool = False
    hard_cap: bool = False
    needs_server: bool = False
    headroom: dict | None = None
    private_worker: bool = False


@dataclass
class ActionLimit:
    limit: int
    used: int = 0
    seen: set = field(default_factory=set)

    def watch(self, event: dict, scope: str) -> None:
        if event.get("kind") != "tool_call":
            return
        identity = (scope, event["id"]) if event.get("id") else None
        if identity is not None and identity in self.seen:
            return
        if identity is not None:
            self.seen.add(identity)
        self.used += 1
        if self.used >= self.limit:
            raise Cut(f"blocked: hard action limit of {self.limit} reached")


@dataclass
class Meter:
    tokens: int | None
    usd: float | None
    charged: Callable[[Result], float]
    seen: Result
    ids: dict = field(default_factory=dict)
    actions: ActionLimit | None = None
    reader: Callable | None = None
    action_scope: str = field(default_factory=lambda: secrets.token_hex(8))

    def watching(self, on_line: Callable[[str], None]) -> Callable[[str], None]:
        def watched(line: str) -> None:
            on_line(line)
            if turn := streamed_turn(line, self.ids):
                self.add(turn)
            if self.actions and self.reader:
                try:
                    event = json.loads(line)
                except ValueError:
                    return
                if not isinstance(event, dict):
                    return
                for normalized in self.reader(event):
                    if normalized.get("kind") == "usage" and event.get("type") == "turn.completed":
                        self.seen.tokens_in = normalized.get("tokens_in", 0)
                        self.seen.tokens_out = normalized.get("tokens_out", 0)
                    self.actions.watch(normalized, self.action_scope)
                if event.get("type") == "tool_execution_start":
                    self.actions.watch({"kind": "tool_call", "id": event.get("toolCallId")}, self.action_scope)

        return watched

    def add(self, turn: dict) -> None:
        seen = self.seen
        seen.turns += turn.get("turns", 1)
        for key in USAGE:
            setattr(seen, key, getattr(seen, key) + turn[key])
        used = seen.tokens_in + seen.tokens_out
        if self.tokens is not None and used >= self.tokens:
            raise Cut(f"stopped at the {self.tokens} tokens left under the limit, after {used}")
        if self.usd is not None and (spent := self.charged(seen)) >= self.usd:
            raise Cut(f"stopped at the ${self.usd:.4f} left under the limit, after ${spent:.4f}")

    def guard(self, call: Callable[..., Result], *args, **kw) -> Result:
        try:
            res = call(*args, **kw)
        except Cut as cut:
            return replace(self.seen, error=str(cut))
        reported = res.tokens_in or res.tokens_out or res.cost_usd or res.calls
        if reported or not self.seen.turns:
            return res
        keys = ("turns", *USAGE)
        return replace(res, **{k: getattr(self.seen, k) for k in keys})


@dataclass
class Picked:
    candidate: object
    auth: accounts.Auth
    selected: pi.Route | None
    name: str
    unknown: str
    auths: set
    budget: float | None
    api_key: str = field(default="", repr=False)


@dataclass
class Stopped:
    res: Result
    picked: Picked


def outgrown(trip: Trip, chosen, size: int) -> str:
    discovery = routing.Discovery(trip.cfg, trip.repo, trip.target, layer=trip.layer)
    key = routing.route_key(chosen)
    again = discovery.routes(replace(trip.request, size=size), request_tokens=size)
    return next((c.detail for c in again if routing.route_key(c) == key and c.state == "ineligible"), "")


def launched_selection(trip: Trip, prior: dict | None, skipped: list[dict], size: int, chosen=None, name: str = "",
                       error: str = "") -> dict | None:
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


def passed_over(found, why: str, detail: str, until: str = "") -> dict:
    return {"runner": found.runner, "model": found.model, "credential": found.credential, "state": why,
            "detail": detail, "until": until}


def pick(trip: Trip) -> tuple[Picked | None, list[dict]]:
    discovery = routing.Discovery(trip.cfg, trip.repo, trip.target, layer=trip.layer)
    found = discovery.routes(trip.request, request_tokens=trip.request.size)
    wanted = trip.resume
    if wanted.id and not trip.launched:
        found.sort(key=lambda c: not (session_route(wanted, c) and (not wanted.credential
                                                                   or c.credential == wanted.credential)))
    skipped: list[dict] = []
    for candidate in found:
        key = routing.route_key(candidate)
        if trip.private_worker and (trip.target.remote or candidate.runner not in ("pi", REPLAY)):
            skipped.append(passed_over(candidate, "ineligible", "private workers require an isolated local route"))
            continue
        if key in trip.barred:
            skipped.append(passed_over(candidate, "ineligible", trip.barred[key]))
            continue
        if trip.tokens is not None:
            verdict, why = codex_refusal(candidate.runner, trip.headroom, trip.tokens,
                                         trip.budget() if trip.budget else trip.max_budget)
            if why:
                skipped.append(passed_over(candidate, verdict, why))
                continue
        traits = traits_of(candidate.runner)
        if trip.tokens is not None and trip.structured and traits.cannot_gate:
            skipped.append(passed_over(candidate, "ineligible", traits.cannot_gate))
            continue
        remaining = None
        if trip.needs_server and traits.no_server_access:
            skipped.append(passed_over(candidate, "ineligible", traits.no_server_access))
            continue
        if trip.hard_cap and candidate.billing == "paid" and candidate.runner == "codex":
            skipped.append(passed_over(candidate, "ineligible", "codex cannot stop a paid call at a dollar limit"))
            continue
        if candidate.billing == "paid" and candidate.state in ("ready", "held"):
            remaining = trip.budget() if trip.budget else trip.max_budget
            if remaining is not None and remaining <= routing.BUDGET_FLOOR_USD:
                skipped.append(passed_over(candidate, "budget_spent", "API-key budget is spent"))
                continue
        if candidate.state != "ready":
            skipped.append(passed_over(candidate, candidate.state, candidate.detail, candidate.until))
            continue
        selected = None
        api_key = ""
        if candidate.runner == "pi":
            selected = trip.pinned.get(key) or discovery.probe(candidate.model, candidate.mode)
            auth, name = selected.auth, selected.model
        elif candidate.credential == "cli_key" and candidate.mode == "api_key":
            api_key = discovery.cli_key_route(candidate).key
            auth, name = discovery.cli_key_auth(candidate), candidate.model
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
        active = [h for a in auths if (h := routing.auth_hold(a, name, unknown=unknown))]
        held = max(active, key=lambda h: (not h["until"], h["until"]), default=None)
        if held:
            skipped.append(passed_over(candidate, "held", routing.hold_words(held), held["until"]))
            continue
        floor = trip.floors.get(key)
        if floor and floor[0] > util.now() and floor[1] == auth:
            skipped.append(passed_over(candidate, "held",
                                       f"limit reported, rechecking after {routing.local_time(util.iso(floor[0]))}",
                                       util.iso(floor[0])))
            continue
        return Picked(candidate, auth, selected, name, unknown, auths, remaining, api_key), skipped
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
                  runner=which, model=name, provider=routing.provider_of(name),
                  session=limited[-1].session if limited else trip.resume, skipped=skipped,
                  usage_limit=UsageLimit(util.iso(wake) if wake else "", "", "usage") if held else None)


def out_of_budget(res: Result) -> bool:
    return (not res.ok and res.error.startswith(NO_ROUTE)
            and any(s["state"] == "budget_spent" for s in res.skipped))


def out_of_headroom(res: Result) -> bool:
    return (not res.ok and res.error.startswith(NO_ROUTE)
            and any(s["state"] == HEADROOM_STATE for s in res.skipped))


def account_of(auth: accounts.Auth) -> str:
    who = auth.identity or auth.credential
    return accounts.fingerprint(auth.mode, auth.family, auth.provider, who) if who else ""


def session_route(session: Session, candidate: routing.Candidate) -> bool:
    model = candidate.model if routing.claude_openrouter(candidate.runner, candidate.provider) else ""
    return (session.runner == candidate.runner and session.openrouter_model == model
            and (not model or record.one_component(session.openrouter_run)))


def continues(stopped: Stopped, picked: Picked, persist: bool) -> bool:
    before, now = stopped.picked.auth, picked.auth
    res = stopped.res
    return (session_route(res.session, picked.candidate) and bool(res.session.id) and res.turns > 1
            and persist and (before == now or (bool(account_of(before)) and account_of(before) == account_of(now))
                             or devices_store.refresh_continues(before.generation, now.generation)))


def session_for(trip: Trip, picked: Picked, prompt: str, fresh_prompt: Callable[[str], str] | None,
                previous: Stopped | None, kw: dict, record_id: str) -> tuple[str, Session]:
    which = picked.candidate.runner
    saved = trip.resume
    usable = saved.id and session_route(saved, picked.candidate) and (
        not saved.account or saved.account == account_of(picked.auth)
        or devices_store.refresh_continues(saved.generation, picked.auth.generation))
    if previous is None and usable:
        return prompt, saved
    if previous is not None and continues(previous, picked, kw.get("persist", True)):
        return CONTINUE_AFTER_LIMIT, previous.res.session
    fresh = fresh_prompt(which) if fresh_prompt else prompt
    if previous is None:
        return fresh, NO_SESSION
    stopped = previous.res
    last = (record.last_message(paths.record_dir(record_id) / stopped.record, claude.USAGE_LIMIT_TEXT)
            if stopped.record else "") or stopped.text
    note = (f"An earlier attempt on {stopped.runner} {stopped.model} stopped at a limit. Its work is in the working "
            "folder; inspect it, then carry on and finish the task.")
    if last:
        note += f"\n\nIts last message:\n\n{last.strip()[-4000:]}"
    return f"{fresh}\n\n## Interrupted attempt\n\n{note}", NO_SESSION


LOGIN_NAMES = frozenset({*claude.LOGIN_VARS, *claude.PARENT_SESSION_VARS, *codex.LOGIN_VARS, *codex.NEVER,
                         *pi.PARENT_SESSION_VARS})


def reserved(name: str) -> str:
    if environment.reserved(name):
        return f"{name} is a variable arete sets itself"
    if name in LOGIN_NAMES:
        return f"{name} is a login variable of an agent runner"
    return ""


def launch_kwargs(base: dict, picked: Picked, trip: Trip, trace: tuple[str, str], ask_jev: bool,
                  say: Callable[[str], None], *, cwd: Path | str | None = None, claude_tools: tuple[str, ...] = ()) -> dict:
    kw = {k: list(v) if isinstance(v, list) else v for k, v in base.items()}
    cfg, repo, target = trip.cfg, trip.repo, trip.target
    which = picked.candidate.runner
    if which == "claude" and claude_tools:
        kw["allowed_tools"] = [*(kw.get("allowed_tools") or ()), *claude_tools]
    floor = None if target.remote else guard.floor(trace[0], cwd or Path.cwd(), kw.get("add_dirs") or [])
    closed_context = tuple(Path(path).absolute() for path in kw.pop("closed_context", ()))
    if kw.get("private_worker") and which == "pi":
        kw["private_hidden"] = closed_context
    if closed_context:
        if target.remote:
            raise ValueError("closed grading context requires a local runner")
        floor = replace(floor, closed=(*floor.closed, *closed_context))
    kw["effort"] = picked.candidate.effort
    kw["instructions"] = list(picked.candidate.instructions)
    request = getattr(trip, "request", None)
    if request and request.requires.delegation_only:
        if target.remote or not traits_of(which).delegation_only:
            raise ValueError("controller route lacks local tool-free execution")
        kw.update(tools=[], allowed_tools=[], instructions=[], mcp_config="", add_dirs=[], ask_owner=False)
    asking = bool(kw.pop("ask_owner", False)) and not target.remote
    if target.remote:
        kw["remote"] = target
    else:
        supervisor = getattr(getattr(trip, "settings", None), "step", "") == "unblock" or trace[1] == "orchestrate-monitor"
        kw["passing"] = [] if supervisor else environment.passed(cfg, repo)
        if which == "pi":
            roots = trip.layer.read_roots if trip.layer and not (request and request.requires.delegation_only) else ()
            kw["read_roots"] = tuple(Path(root) for root in roots)
        kept = {} if supervisor else {n: v for n, v in environment.stored(repo).items() if not reserved(n)}
        if picked.selected:
            kept = {n: v for n, v in kept.items() if n not in picked.selected.env}
        if kept:
            kw["extra_env"] = {**kept, **(kw.get("extra_env") or {})}
            kw["stored_values"] = sorted(kept.values())
        if api_key := getattr(picked, "api_key", ""):
            kw["api_key"] = api_key
            kw["stored_values"] = [*(kw.get("stored_values") or ()), api_key]
    hook = ()
    kw.pop("verification_attempt", None)
    if trip.scratch:
        kw, hook = browser.confine(kw, trip.scratch, which, bool(target.remote), say)
    if asking and hook:
        browser.with_note(kw, owner_asks.WITHHELD)
    elif asking:
        browser.with_note(kw, owner_asks.NOTE)
        if which in ("claude", "codex"):
            kw["mcp_config"] = owner_asks.with_server(kw.get("mcp_config") or "")
            kw["allowed_tools"] = [*(kw.get("allowed_tools") or []), owner_asks.RULE]
        elif which == "pi":
            kw["ask_owner"] = True
    if trip.tokens is not None:
        kw["max_tokens"] = trip.tokens()
    if which == "claude":
        if trip.tokens is not None or (trip.hard_cap and picked.candidate.billing == "paid" and picked.budget):
            kw["max_budget_usd"] = picked.budget
        else:
            kw.pop("max_budget_usd", None)
        if not target.remote:
            if routing.claude_openrouter(which, picked.candidate.provider):
                configs = (os.environ.get("CLAUDE_CONFIG_DIR"), (kw.get("extra_env") or {}).get("CLAUDE_CONFIG_DIR"))
                closed_context += tuple(Path(folder) for folder in dict.fromkeys(configs) if folder)
            kw = local_claude(kw, cfg, trace[0], ask_jev, say, repo, hook, floor=floor, closed_context=closed_context)
    elif which == "codex":
        kw.pop("max_budget_usd", None)
        if not target.remote:
            kw = local_codex(kw, cfg, trace[0], say, repo, hook, floor=floor)
    elif which == "pi":
        if request and request.requires.delegation_only:
            kw["delegation_only"] = True
        kw["price"] = lambda reply: priced(reply, cfg)
        kw["max_budget_usd"] = picked.budget
        kw["selected"] = picked.selected
        if not target.remote:
            kw["providers"] = cfg.environment.providers
            kw["floor"] = floor
            kw["judge"] = cfg.jev.guard
    if uploads.granted(kw.get("allowed_tools")) and (not TRAITS[which].uploads_supported or target.remote):
        kw["allowed_tools"] = uploads.without(kw["allowed_tools"])
        browser.with_note(kw, uploads.WITHHELD)
    return kw


def launch_env(picked: Picked, cfg: config.Config, repo: config.Repo | None, run_id: str,
               extra: dict | None = None, asking: bool = False, *, openrouter_config: Path | None = None) -> list[str]:
    if picked.selected:
        return sorted({*pi.launch_env_names(picked.selected), *(extra or {}), *(["ARETE_ASK_TOOL"] if asking else [])})
    agent = codex if picked.candidate.runner == "codex" else claude
    transport = {"openrouter_config": openrouter_config, "model": picked.name} if openrouter_config else {}
    return sorted(agent.agent_env(environment.passed(cfg, repo), {**(extra or {}), **run_env(run_id)},
                                  api_key=getattr(picked, "api_key", ""), **transport))


def with_spent(res: Result, earlier: list[Result]) -> Result:
    if earlier:
        res.aggregate_paid_usd = res.paid_usd + sum(r.paid_usd for r in earlier)
        res.auth_modes = tuple(dict.fromkeys(mode for r in [*earlier, res] for mode in r.modes))
    res.add_usage(earlier)
    res.api_seconds += sum(r.api_seconds for r in earlier)
    return res


def described_servers(mcp_config: str) -> dict:
    try:
        servers = json.loads(mcp_config or "{}").get("mcpServers") or {}
    except (ValueError, AttributeError):
        return {}
    found = {}
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        if server.get("url"):
            found[name] = {"type": server.get("type") or "http", "url": server["url"]}
        else:
            words = str(server.get("command") or "").split()
            found[name] = {"type": "stdio", "command": words[0] if words else ""}
    return found


def refresh_redactions(hidden: scrub.Redactions, selected: pi.Route | None) -> None:
    if selected and selected.using_login:
        hidden.add([*pi.credential_values(accounts.read(selected.owner)), *selected.refreshed_secrets])
    if selected:
        hidden.add(v for path in selected.sensitive_files for v in scrub.file_secrets(path, every_value=True))


def attempt(which: str, name: str, prompt: str, cwd: Path | str, resume: Session, cfg: config.Config,
            trace: tuple[str, str], record_id: str, target: devices.Device, kw: dict, tier: str = "",
            kind: str = "", auth: accounts.Auth | None = None, route: dict | None = None, try_no: int = 0,
            atom: str = "", *, openrouter_quote: routing.Quote | None = None) -> Result:
    auth = auth or (authentication(name, which, cfg=cfg, device=target) if which != REPLAY else accounts.Auth())
    if target.remote and which == "claude":
        kw["leaf"] = f"{record.slug(trace[1])}-{try_no}"
    route = route or {}
    header = {"runner": which, "model": name, "device": target.name, **origin(cwd, cfg, target), "cwd": str(cwd),
              "auth_mode": auth.mode, "credential": route.get("credential", ""), "billing": route.get("billing", ""),
              "requested_effort": route.get("requested_effort", ""), "tier": route.get("tier", tier),
              "position": route.get("position", 0), "requested": route.get("requested", True),
              "launch": route.get("launch", 1), "skipped": route.get("skipped", []),
              "provider": routing.provider_of(name) or ("anthropic" if which == "claude" else
                                                ("openai" if auth.mode == "api_key" else "chatgpt")
                                                if which == "codex" else ""),
              "prompt": prompt, "system": kw.get("system", ""), "tools": kw.get("tools"),
              "add_dirs": [str(p) for p in kw.get("add_dirs") or []],
              "allowed_tools": kw.get("allowed_tools"), "disallowed_tools": kw.get("disallowed_tools"),
              "json_schema": kw.get("json_schema"), "budget_usd": kw.get("max_budget_usd"),
              "mcp_servers": described_servers(kw.get("mcp_config", "")),
              "budget_tokens": kw.get("max_tokens"), "refused": kw.get("budget_error", ""),
              "timeout": kw.get("timeout"), "effort": kw.get("effort", ""),
              "resume": {"runner": resume.runner, "id": resume.id, "spent_usd": resume.spent_usd} if resume.id else None}
    header["context"] = context.inventory(
        prompt, kw.get("system", ""), kw.get("json_schema"), resumed=bool(resume.id),
        instructions=kw.get("instructions"), allowed_tools=kw.get("allowed_tools"), mcp_servers=header["mcp_servers"])
    if atom:
        header["atom"] = atom
    if openrouter_quote is not None:
        header.update(provider=route["provider"], env=route["env"], usage_rates=openrouter_quote.rates)
    if route.get("selection"):
        header["route"] = route
    found = secret_files([cwd, *(kw.get("add_dirs") or [])], target, cfg)
    selected = kw.get("selected")
    if selected:
        found.extend(selected.sensitive_files)
    hidden = scrub.Redactions(scrub.secrets(*found, given=kw.pop("stored_values", ())), escape=bool(selected))
    if selected:
        hidden.add(selected.hidden)
        header["env"] = route["env"]
        header["provider"] = selected.provider

    def safe_line(line):
        refresh_redactions(hidden, selected)
        rec.line(line)
    refresh_redactions(hidden, selected)
    started = time.monotonic()
    at = util.now()
    max_tokens = kw.pop("max_tokens", None)
    on_local = route.get("credential") == "local"

    def charged(res: Result) -> float:
        if openrouter_quote is not None:
            return token_cost(openrouter_quote.rates, res.tokens_in, res.tokens_out, res.cache_read, res.cache_write)
        return 0.0 if on_local else priced(res, cfg)

    def persist(res: Result) -> None:
        try:
            if trace[0]:
                record.steps(trace[0], trace[1],
                             [(call, call.call_seconds if res.calls else time.monotonic() - started)
                              for call in res.calls or [res]], effort=kw.get("effort", ""),
                             tier=tier, kind=kind, try_no=try_no, auth_mode="local" if on_local else "")
        except BaseException as error:
            rec.finish(replace(res, ok=False, error=scrub.scrub(
                f"cost is not in steps: {record.failure(error)}; {res.error}", hidden)), time.monotonic() - started)
            raise

    def keep_lost(error: BaseException) -> None:
        refresh_redactions(hidden, selected)
        seen = replace(meter.seen, provider=header["provider"],
                       error=scrub.scrub(rec.exception_text(error), hidden))
        seen.cost_usd = charged(seen)
        persist(seen)
        rec.finish(seen, time.monotonic() - started)

    meter = Meter(max_tokens, kw.get("max_budget_usd") if max_tokens is not None or openrouter_quote is not None else None,
                  (lambda r: priced(r, cfg)) if openrouter_quote is None else charged,
                  Result(ok=False, session=resume, runner=which, model=name, auth_mode=auth.mode),
                  actions=kw.pop("action_limit", None), reader=record.READERS[which](header))
    with record.Recorder(record_id, trace[1], header, hidden) as rec:
        try:
            budget_error = kw.pop("budget_error", "")
            if budget_error:
                res = Result(ok=False, error=budget_error, session=resume, runner=which, model=name)
            elif which == REPLAY:
                res = meter.guard(replay.run, cwd, trace, cfg, meter.watching(rec.line), resume, kw.get("timeout"),
                                  prompt=prompt)
            elif which == "codex":
                protected = [sandbox.AUTHORITY, paths.WORKTREES_DIR]
                res = meter.guard(codex.run, prompt, cwd, model=name, resume=resume,
                                  on_line=meter.watching(rec.line), hidden=[*found, *protected], auth=auth, **kw)
            else:
                res = meter.guard(pi.run if which == "pi" else claude.run, prompt, cwd, model=name, resume=resume,
                                  on_line=meter.watching(safe_line if selected else rec.line), auth=auth, **kw)
            res.runner = which
            res.model = res.model or name
            res.provider = res.provider or routing.provider_of(res.model)
            if openrouter_quote is not None:
                res.model, res.provider = name, header["provider"]
            if which != REPLAY:
                res.auth_mode = auth.mode
                if res.usage_limit and (
                        res.usage_limit.scope not in routing.limit_scopes(auth, name) or
                        res.usage_limit.billing != ("subscription" if auth.mode == "subscription" else "credit")):
                    res.usage_limit = None
            if res.calls:
                for call in res.calls:
                    call.cost_usd = charged(call)
                res.cost_usd = sum(call.cost_usd for call in res.calls)
                res.aggregate_paid_usd = sum(call.paid_usd for call in res.calls)
            else:
                res.cost_usd = charged(res)
            if openrouter_quote is not None and res.session.id:
                res.session = replace(res.session, spent_usd=resume.spent_usd + res.cost_usd)
            if not budget_error:
                if openrouter_quote is not None:
                    res.quote = openrouter_quote.applied
                    res.reference_usd = prices.reference_usd(res.quote, res.tokens_in, res.tokens_out,
                                                            res.cache_read, res.cache_write)
                else:
                    quote_calls(res, which, name, at)
            refresh_redactions(hidden, selected)
            res.error = scrub.scrub(res.error, hidden)
            res.text = scrub.scrub(res.text, hidden)
            res.structured = scrub.scrub_value(res.structured, hidden)
            for call in res.calls:
                call.error = scrub.scrub(call.error, hidden)
            res.effort, res.credential = kw.get("effort", ""), route.get("credential", "")
            if not target.remote:
                res.auth_generation = auth.generation
            res.requested_effort, res.skipped = route.get("requested_effort", ""), route.get("skipped", [])
            res.record = rec.path.name if rec.path else ""
        except BaseException as error:
            keep_lost(error)
            raise
        persist(res)
        rec.finish(res, time.monotonic() - started)
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
            found += [Path(here, n) for n in names if sandbox.secret_path(n)]
    return found


def local_claude(kw: dict, cfg: config.Config, run_id: str, ask_jev: bool, say: Callable[[str], None],
                 repo: config.Repo | None, scratch: tuple[str, ...] = (), *, floor: guard.Floor,
                 closed_context: tuple[Path, ...] = ()) -> dict:
    kw = {**kw, "extra_env": {**(kw.get("extra_env") or {}), **run_env(run_id)}}
    if guard.wanted(kw.get("tools")):
        kw["settings"] = guard.settings(cfg.jev.guard, floor.open)
        if closed_context:
            for group in kw["settings"]["hooks"]["PreToolUse"]:
                for hook in group["hooks"]:
                    hook["command"] += " " + shlex.join([arg for path in closed_context for arg in ("--closed", str(path))])
        if scratch:
            for group in kw["settings"]["hooks"]["PreToolUse"]:
                for hook in group["hooks"]:
                    hook["command"] = verification_scratch.hook_command(hook["command"], list(scratch))
        if runs_bash(kw) and (note := judge_note(cfg, kw["passing"], repo)):
            say(note)
    if ask_jev and runs_bash(kw) and jev.ready(cfg, [repo], env=claude.agent_env(kw["passing"])):
        kw["allowed_tools"] = [*(kw.get("allowed_tools") or []), jev_ask.rule()]
        browser.with_note(kw, jev_ask.note())
    if uploads.granted(kw.get("allowed_tools")):
        kw["settings"] = uploads.with_hook(kw.get("settings"))
    return kw


def local_codex(kw: dict, cfg: config.Config, run_id: str, say: Callable[[str], None],
                repo: config.Repo | None, scratch: tuple[str, ...] = (), *, floor: guard.Floor) -> dict:
    kw = {**kw, "extra_env": {**(kw.get("extra_env") or {}), **run_env(run_id)}}
    kw["floor"] = floor
    kw["judge"] = cfg.jev.guard
    if scratch:
        kw["scratch"] = tuple(scratch)
    if cfg.jev.guard and guard.wanted(kw.get("tools")) and (note := judge_note(cfg, kw["passing"], repo)):
        say(note)
    return kw


def runs_bash(kw: dict) -> bool:
    if (tools := kw.get("tools")) is not None:
        return "Bash" in tools
    return any(r == "Bash" or r.startswith("Bash(") for r in kw.get("allowed_tools") or [])
