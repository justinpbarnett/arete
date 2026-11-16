from __future__ import annotations

import http.client
import json
import math
import re
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, timedelta

from . import accounts, claude, codex, config, devices, environment, paths, pi, prices, sandbox, scrub, state, util
from .agent import REPLAY, TRAITS, model_scope, traits_of

RUNNERS = tuple(TRAITS)
CODEX_NAMES = ("gpt-", "codex-")
BUDGET_FLOOR_USD = 0.005
WRITING_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})
NO_GIT_WRITES = [
    "Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)", "Bash(git checkout*)",
    "Bash(git switch*)", "Bash(git rebase*)", "Bash(git merge*)", "Bash(gh *)",
]
PI_HOLDS_NO_BASH_RULES = "pi cannot hold the rules that stop git writes"
SANDBOX_RUNS_PI = ("an exe device runs agents on pi, not claude; pick a provider/model such as exe-llm/gpt-5.4-mini "
                   "or openrouter/<model id>")
CLAUDE_PI_MODELS = {"fable": "claude-fable-5-2", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                    "haiku": "claude-haiku-5-5"}
VENDORS = {"openai-codex": "openai", "openai": "openai", "anthropic": "anthropic"}
CLAUDE_FAMILIES = ("fable", "opus", "sonnet", "haiku")
API_KEY_METHODS = ("api_key", "apiKey", "api-key")
EFFORT_ORDER = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_PROVIDER = "openrouter"
PRICE_FILE = "openrouter-prices.json"
PRICE_FRESH = timedelta(hours=6)
PRICE_TIMEOUT_SECONDS = 5
PRICE_MAX_BYTES = 8 * 1024 * 1024
PAID_RANK = {"claude_key": -1, "pi_oauth": 0, "pi_key": 1, "cli_key": 2}
PAYMENT_OF = {"cli_login": "subscription", "pi_login": "subscription", "cli_key": "api_key", "pi_key": "api_key",
              "pi_oauth": "api_key", "local": "local"}
SERVES = {"anthropic": ("claude", "pi"), "openai": ("codex", "pi")}


@dataclass(frozen=True)
class Link:
    model: str
    runner: str
    provider: str
    tier: str
    position: int
    requested: bool
    effort: str
    instructions: tuple[str, ...]


@dataclass(frozen=True)
class Requirements:
    modalities: tuple[str, ...] = ()
    min_context: int = 0
    structured_output: bool = False
    data_handling: tuple[str, ...] = ()
    floor: str = ""
    approved: tuple[str, ...] = ()
    directory_cleanup: bool = False

    @property
    def declared(self) -> bool:
        return bool(self.modalities or self.min_context or self.structured_output or self.data_handling or self.floor
                    or self.directory_cleanup)


@dataclass(frozen=True)
class Quote:
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    source: str = ""
    applied: dict | None = None

    @property
    def total(self) -> float | None:
        return None if self.input is None or self.output is None else util.nonnegative_number(self.input + self.output)


@dataclass(frozen=True)
class Candidate:
    order: int
    model: str
    runner: str
    provider: str
    credential: str
    billing: str
    tier: str
    position: int
    requested: bool
    effort: str
    instructions: tuple[str, ...]
    price: Quote | None
    state: str
    detail: str
    capabilities: dict | None = None
    mode: str = ""
    until: str = ""


@dataclass(frozen=True)
class Request:
    chain: tuple[Link, ...]
    fallback: bool
    tools: tuple[str, ...]
    mcp: tuple[str, ...]
    effort: str
    instructions: tuple[str, ...] | None = None
    denied: tuple[str, ...] = ()
    requires: Requirements = Requirements()
    size: int = 0

    def effort_on(self, which: str) -> str:
        return nearest_effort(which, self.effort)

    def instructions_on(self, which: str) -> tuple[str, ...]:
        return traits_of(which).default_instructions if self.instructions is None else self.instructions


@dataclass(frozen=True)
class Way:
    link: Link
    runner: str
    model: str
    credential: str
    billing: str
    mode: str = ""


@dataclass(frozen=True)
class Selection:
    route: Candidate | None
    kept: bool
    reason: str
    skipped: tuple[Candidate, ...] = ()
    error: str = ""


def nearest_effort(which: str, effort: str) -> str:
    levels = traits_of(which).efforts
    if effort in levels:
        return effort
    if effort not in EFFORT_ORDER:
        return ""
    lower = [level for level in EFFORT_ORDER[:EFFORT_ORDER.index(effort)] if level in levels]
    return lower[-1] if lower else ""


def way_key(found: Way | Candidate) -> tuple:
    return found.runner, identity(found.runner, found.model), found.credential


def route_key(found: Candidate) -> tuple:
    return way_key(found)


def claude_id(name: str) -> str:
    name = name.removesuffix("[1m]").removeprefix("anthropic/")
    return CLAUDE_PI_MODELS.get(name, name)


def identity(which: str, model: str) -> tuple[str, str]:
    name = model.lower()
    if which == "claude":
        return "anthropic", claude_id(name)
    if which == "codex":
        return "openai", name
    if which == "pi":
        provider, sep, rest = name.partition("/")
        if not sep:
            return "pi", name
        vendor = VENDORS.get(provider, provider)
        return vendor, claude_id(rest) if vendor == "anthropic" else rest
    return which, name


def source_route(source: dict) -> tuple[str, str, str]:
    which, name = route(source.get("model", ""), source.get("runner", ""))
    provider = source.get("provider", "") or provider_of(name)
    if which == "pi" and source.get("provider") and not name.startswith(f"{provider}/"):
        name = f"{provider}/{name}"
    return which, name, provider


def entry_problem(entry: dict) -> str:
    hint = entry.get("runner", "")
    if hint and hint not in RUNNERS:
        return f"runner '{hint}' is not one of {', '.join(RUNNERS)}"
    if not entry.get("model"):
        return ""
    which, name, provider = source_route(entry)
    lowered = name.lower()
    wrong = False
    if which == "claude":
        wrong = "/" in name or lowered.startswith(CODEX_NAMES) or provider not in ("", "anthropic")
    elif which == "codex":
        wrong = (lowered.startswith("claude-") or model_scope(name).removeprefix("model:") in CLAUDE_FAMILIES
                 or "/" in name or provider not in ("", "openai"))
    return f"runner '{which}' cannot run model '{entry['model']}'" if wrong else ""


def payment_of(credential: str, auth_mode: str = "") -> str:
    if credential == "local":
        return "local"
    if credential not in PAYMENT_OF:
        return ""
    return "api_key" if PAYMENT_OF[credential] == "api_key" or auth_mode == "api_key" else "subscription"


def incompatible(way: Way, settings, device: devices.Device) -> str:
    traits = traits_of(way.runner)
    wanted = settings.instructions_on(way.runner)
    if not traits.takes_mcp and settings.mcp:
        return f"mcp {', '.join(settings.mcp)} cannot load on {way.runner}, which runs with --no-mcp"
    if wanted and not traits.instruction_scopes:
        return f"instructions {', '.join(wanted)} cannot load on {way.runner}"
    if not traits.takes_mcp and WRITING_TOOLS & set(settings.tools) and not sandbox.can_confine(False):
        return sandbox.NO_CONFINEMENT_REASON
    fallback = not (way.link.requested and way.runner == way.link.runner)
    if not traits.takes_mcp and fallback and "Bash" in settings.tools and not device.exe_sandbox:
        return PI_HOLDS_NO_BASH_RULES
    if (traits.always_granted_tools and fallback
            and (missing := [t for t in traits.always_granted_tools if t not in settings.tools])):
        return f"tools leave out {', '.join(missing)}, which {way.runner} always grants"
    if traits.cannot_stop and fallback and (loose := [d for d in settings.denied if d not in NO_GIT_WRITES]):
        return f"{way.runner} cannot hold the denial of {', '.join(loose)}"
    if traits.instruction_scopes and (refused := [i for i in wanted if i not in traits.instruction_scopes]):
        return (f"instructions {', '.join(refused)} cannot load on {way.runner}, which runs without your own "
                f"{way.runner} setup")
    return ""


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


def provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def limit_applies(scope: str, model: str) -> bool:
    if scope.startswith("model:"):
        return scope == model_scope(model)
    return scope in (claude.EVERY_MODEL, codex.LIMIT_SCOPE) or scope in re.split(r"[-_/.:]", model.lower())


def auth_hold(auth: accounts.Auth, model: str, connect=state.reader, unknown: str = "") -> dict | None:
    if not (auth.identity or auth.credential or unknown):
        return None
    scopes = [accounts.scope(auth, s, unknown) for s in limit_scopes(auth, model)]
    found = [h for h in state.holds(connect) if h["scope"] in scopes]
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


def pi_route(model: str, cfg: config.Config, repo=None, device="local", connect=state.reader,
             mode: str = "") -> pi.Route | None:
    if devices.resolve(device).remote:
        return pi.Route(provider_of(model), model, accounts.Auth(), error=pi.REMOTE_AUTH_UNVERIFIED)
    return pi.select(model, environment.passed(cfg, repo), cfg.environment.providers,
                     held=lambda auth, name: auth_hold(auth, name, connect), mode=mode,
                     local_providers=cfg.local_providers)


def hold_words(hold: dict) -> str:
    return (f"{hold['why']}, waiting until {local_time(hold['until'])}" if hold["until"] else
            f"{hold['why']}, held until cleared")


def local_time(stamp: str) -> str:
    return util.parse_iso(stamp).astimezone().strftime("%a %b %d %H:%M")


def metered(model: str, run_with: str = "", *, cfg: config.Config | None = None, repo: config.Repo | None = None,
            device: devices.Device | str = "local", tools: list[str] | None = None) -> bool:
    cfg = cfg or config.load()
    return can_charge(outlook(cfg, repo, device, model, run_with, tools, NO_GIT_WRITES))


def usage_hold(model: str, run_with: str = "", connect=state.reader, cfg: config.Config | None = None,
               repo: config.Repo | None = None, device: devices.Device | str = "local",
               tools: list[str] | None = None, remaining: float | None = None, denied=NO_GIT_WRITES) -> str:
    cfg = cfg or config.load()
    return held_words(outlook(cfg, repo, device, model, run_with, tools, denied, remaining, connect))


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
        elif tool in WRITING_TOOLS and not sandbox.can_confine(device.remote):
            reasons.setdefault(sandbox.NO_CONFINEMENT_REASON, []).append(tool)
    return reasons


def reach(kw: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    granted = kw["tools"] if kw.get("tools") is not None else (kw.get("allowed_tools") or [])
    denied = tuple(dict.fromkeys(kw.get("disallowed_tools") or []))
    bare = tuple(t for t in dict.fromkeys(granted)
                 if "(" not in t and not t.startswith("mcp__") and t not in denied)
    return bare, denied


def routing_size(prompt: str, settings, kw: dict) -> int:
    if not (settings and settings.requires.declared):
        return 0
    return request_tokens(prompt, kw.get("system", ""), kw.get("json_schema"))


def request_for(cfg: config.Config, repo: config.Repo | None, which: str, name: str, settings, fallback: bool,
                kw: dict, size: int = 0, layer: config.RepoLayer | None = None):
    from . import steps

    bare, denied = reach(kw)
    try:
        servers = json.loads(kw.get("mcp_config") or "{}").get("mcpServers") or {}
    except (ValueError, AttributeError):
        servers = {}
    effort = settings.effort if settings else kw.get("effort", "")
    given = kw.get("instructions")
    explicit = (tuple(settings.instructions) if settings.instructions_explicit else None) if settings else (
        None if given is None else tuple(given))
    requires = settings.requires if settings else Requirements()
    if settings and settings.chain and (requires.declared or identity(which, name) == identity(
            settings.chain[0].runner, settings.chain[0].model)):
        chain, allowed = settings.chain, settings.fallback
    else:
        layer = layer if layer is not None else layer_of(repo)
        chain, allowed = steps.chain_for(cfg, layer, which, name, effort, explicit)
    return Request(chain, allowed and fallback, bare, tuple(servers), effort, explicit, denied, requires, size)


def layer_of(repo: config.Repo | None):
    try:
        return config.load_layer(repo.layer_dir) if repo else None
    except (ValueError, OSError):
        return None


def outlook(cfg: config.Config, repo: config.Repo | None, device: devices.Device | str, model: str, run_with: str = "",
            tools: list[str] | None = None, denied=(), remaining: float | None = None,
            connect=state.reader) -> list[Candidate]:
    which, name = chosen(model, run_with, cfg)
    if which == REPLAY:
        return []
    request = request_for(cfg, repo, which, name, None, True, {"tools": tools, "disallowed_tools": list(denied)})
    return Discovery(cfg, repo, device, connect, priced=False).routes(request, remaining)


def held_words(found: list[Candidate]) -> str:
    holds = [c for c in found if c.state == "held"]
    if not holds or any(c.state == "ready" for c in found):
        return ""
    return min(holds, key=lambda c: (not c.until, util.parse_iso(c.until).timestamp() if c.until else 0.0)).detail


def can_charge(found: list[Candidate]) -> bool:
    return any(c.billing == "paid" and c.state in ("ready", "held", "budget_spent") for c in found)


def budget_blocked(found: list[Candidate]) -> bool:
    return (not any(c.state in ("ready", "held") for c in found)) and any(c.state == "budget_spent" for c in found)


def modes_for(cfg: config.Config, repo: config.Repo | None) -> tuple[str, ...]:
    try:
        return config.allowed_payment_modes(cfg, config.load_layer(repo.layer_dir) if repo else None)
    except (ValueError, TypeError, OSError):
        return ()


def paid_for(cfg: config.Config, repo: config.Repo | None,
             layer: config.RepoLayer | None = None) -> config.PaidRoutes:
    try:
        return config.allowed_paid_routes(cfg, layer or (config.load_layer(repo.layer_dir) if repo else None))
    except (ValueError, TypeError, OSError):
        return cfg.paid


def policy_fit(which: str, modes: tuple[str, ...]) -> str:
    possible = traits_of(which).payment_modes
    if not possible or any(m in modes for m in possible):
        return ""
    return f"[routing] payment_modes allows {', '.join(modes) or 'no mode'}; {which} routes are {' or '.join(possible)}"


def fit_refusal(request: Request, device: devices.Device, modes: tuple[str, ...] | None = None) -> str:
    first = ""
    for link in request.chain if request.fallback else request.chain[:1]:
        vendor, _ = identity(link.runner, link.model)
        serving = SERVES.get(vendor, (link.runner,)) if request.fallback else (link.runner,)
        for which in serving:
            way = Way(link, which, link.model, "", "")
            why = (policy_fit(which, modes) if modes is not None else "") or device_refusal(
                link.model, which, device) or incompatible(way, request, device)
            if not why:
                return ""
            first = first or why
    return first


def per_million(value) -> float | None:
    found = util.nonnegative_number(value)
    scaled = None if found is None else util.nonnegative_number(found * 1e6)
    return None if scaled is None else round(scaled, 6)


def whole(value) -> int | None:
    return value if type(value) is int and value >= 1 else None


def names(value) -> list[str] | None:
    return list(value) if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value) else None


def listing(entry) -> dict:
    entry = accounts.mapping(entry)
    return {**{key: util.nonnegative_number(entry.get(key)) for key in ("input", "output", "cache_read", "cache_write")},
            "context": whole(entry.get("context")), "modalities": names(entry.get("modalities")),
            "structured_output": entry["structured_output"] if type(entry.get("structured_output")) is bool else None}


def openrouter_prices() -> dict[str, dict]:
    file = paths.STATE_DIR / PRICE_FILE
    try:
        cached = util.read_json(file, {})
        if isinstance(cached.get("models"), dict) and util.now() - util.parse_iso(cached["fetched"]) < PRICE_FRESH:
            return {name: listing(rates) for name, rates in cached["models"].items()}
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        pass
    if paths.ISOLATED:
        return {}
    request = urllib.request.Request(OPENROUTER_URL, headers={"Accept": "application/json", "User-Agent": "arete"})
    try:
        with urllib.request.build_opener(accounts.NoRedirect).open(request, timeout=PRICE_TIMEOUT_SECONDS) as response:
            data = response.read(PRICE_MAX_BYTES + 1)
            if response.status != 200 or len(data) > PRICE_MAX_BYTES:
                return {}
        models = {}
        for item in json.loads(data)["data"]:
            rates = item.get("pricing") or {}
            parameters = item.get("supported_parameters")
            models[item["id"]] = listing({
                "input": per_million(rates.get("prompt")), "output": per_million(rates.get("completion")),
                "cache_read": per_million(rates.get("input_cache_read")),
                "cache_write": per_million(rates.get("input_cache_write")),
                "context": item.get("context_length"),
                "modalities": accounts.mapping(item.get("architecture")).get("input_modalities"),
                "structured_output": "structured_outputs" in parameters if isinstance(parameters, list) else None})
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
        return {}
    try:
        util.write_json(file, {"fetched": util.now().astimezone(UTC).isoformat(timespec="seconds"), "models": models})
    except OSError:
        pass
    return models


def local_quote(model: str, selected: pi.Route) -> Quote | None:
    configured = accounts.mapping(accounts.mapping(selected.models.get("providers")).get(selected.provider))
    name = model.partition("/")[2]
    for item in configured.get("models", []):
        item = accounts.mapping(item)
        if item.get("id") == name and isinstance(item.get("cost"), dict):
            cost = item["cost"]
            return Quote(util.nonnegative_number(cost.get("input")), util.nonnegative_number(cost.get("output")), util.nonnegative_number(cost.get("cacheRead")),
                         util.nonnegative_number(cost.get("cacheWrite")), "models.json")
    return None


def free_key(found: Candidate) -> tuple:
    return not found.requested, found.position, found.runner == "pi"


def paid_key(found: Candidate) -> tuple:
    total = found.price.total
    rank = PAID_RANK.get("claude_key" if found.runner == "claude" and found.credential == "cli_key"
                         else found.credential, 9)
    return (total is None, total or 0.0, not found.requested, found.position, rank)


class Discovery:
    def __init__(self, cfg: config.Config, repo: config.Repo | None = None, device: devices.Device | str = "local",
                 connect=state.reader, priced: bool = True, layer: config.RepoLayer | None = None):
        self.declared = {key: {**cfg.capabilities.get(key, {}), **(layer.capabilities.get(key, {}) if layer else {})}
                         for key in dict.fromkeys([*cfg.capabilities, *(layer.capabilities if layer else {})])}
        self.cfg = cfg
        self.repo = repo
        self.device = devices.resolve(device)
        self.connect = connect
        self.priced = priced
        self.modes = modes_for(cfg, repo)
        self.paid = paid_for(cfg, repo, layer)
        self.passing = environment.passed(cfg, repo)
        self.probes: dict[tuple[str, str], pi.Route] = {}
        self.cache: dict[str, object] = {}
        self.listed: dict | None = None

    def remembered(self, name: str, make):
        if name not in self.cache:
            self.cache[name] = make()
        return self.cache[name]

    def probe(self, model: str, mode: str) -> pi.Route:
        if (model, mode) not in self.probes:
            self.probes[model, mode] = pi_route(model, self.cfg, self.repo, self.device, self.connect, mode=mode)
        return self.probes[model, mode]

    def claude_status(self) -> dict:
        return self.remembered("claude_status", lambda: claude.auth_status(claude.agent_env(self.passing)))

    def claude_auth(self) -> accounts.Auth:
        remote = self.device if self.device.remote else None
        return self.remembered("claude_auth", lambda: claude.authentication(self.passing, remote=remote))

    def codex_auth(self) -> accounts.Auth:
        return self.remembered("codex_auth", lambda: codex.authentication(self.passing))

    def cli_key_route(self, way: Way | Candidate) -> pi.Route:
        vendor, ident = identity(way.runner, way.model)
        return self.probe(f"{vendor}/{ident}", "api_key")

    def cli_key_auth(self, way: Way | Candidate) -> accounts.Auth:
        selected = self.cli_key_route(way)
        return accounts.Auth("api_key", generation=accounts.fingerprint("cli-key", selected.provider, selected.key),
                             credential=accounts.fingerprint("credential-v1", way.runner, selected.provider, selected.key),
                             provider=selected.provider)

    def openrouter(self) -> dict:
        if self.listed is None:
            self.listed = openrouter_prices()
        return self.listed

    def is_local(self, probe: pi.Route) -> bool:
        return probe.local or probe.provider in self.cfg.local_providers

    def keyed_way(self, link: Link, keyed: str) -> Way:
        probe = self.probe(keyed, "")
        return (Way(link, "pi", keyed, "local", "free") if self.is_local(probe)
                else Way(link, "pi", keyed, "pi_key", "paid", "api_key"))

    def codex_cli_way(self, link: Link, native: str) -> Way:
        return (Way(link, "codex", native, "cli_key", "paid") if self.codex_auth().mode == "api_key"
                else Way(link, "codex", native, "cli_login", "free"))

    def requested_way(self, link: Link) -> Way:
        if link.runner == "pi":
            selected = self.probe(link.model, "")
            local = self.is_local(selected)
            paid = not local and selected.auth.mode != "subscription"
            credential = ("local" if local else "pi_oauth" if selected.using_login and paid else
                          "pi_login" if selected.using_login else "pi_key")
            if (not local and "subscription" in self.modes and self.policy_refusal(credential)
                    and selected.provider in accounts.PI_SUBSCRIPTIONS and self.probe(link.model, "login").login_available):
                selected, credential, paid = self.probe(link.model, "login"), "pi_login", False
            return Way(link, "pi", selected.model, credential, "paid" if paid else "free",
                       "" if local else "login" if selected.using_login else "api_key")
        if link.runner == "codex":
            return self.codex_cli_way(link, link.model)
        return Way(link, link.runner, link.model, "replay" if link.runner == REPLAY else "cli_login", "free")

    def ways(self, link: Link) -> list[Way]:
        if link.runner == REPLAY:
            return [Way(link, REPLAY, link.model, "replay", "free")]
        vendor, ident = identity(link.runner, link.model)
        if vendor == "anthropic":
            native = link.model if link.runner == "claude" else link.model.partition("/")[2]
            found = [Way(link, "claude", native, "cli_login", "free")]
            if not self.device.remote and self.probe(f"anthropic/{ident}", "api_key").key:
                found.append(Way(link, "claude", native, "cli_key", "paid", "api_key"))
            return [*found, self.keyed_way(link, f"anthropic/{ident}")]
        if vendor == "openai":
            native = link.model if link.runner == "codex" else ident
            cli = self.codex_cli_way(link, native)
            found = [cli, Way(link, "pi", f"openai-codex/{ident}", "pi_login", "free", "login")]
            if not self.device.remote and self.codex_auth().mode == "subscription" and self.probe(
                    f"openai/{ident}", "api_key").key:
                found.append(Way(link, "codex", native, "cli_key", "paid", "api_key"))
            return [*found, self.keyed_way(link, f"openai/{ident}")]
        probe = self.probe(link.model, "")
        if self.is_local(probe):
            return [Way(link, "pi", link.model, "local", "free")]
        found = []
        if probe.provider in accounts.PI_SUBSCRIPTIONS:
            found.append(Way(link, "pi", link.model, "pi_login", "free", "login"))
        elif probe.login_available:
            found.append(Way(link, "pi", link.model, "pi_oauth", "paid", "login"))
        if probe.key_supported or not found:
            found.append(Way(link, "pi", link.model, "pi_key", "paid", "api_key"))
        return found

    def quote(self, way: Way, request_tokens: int = 0) -> Quote:
        if not (self.priced or self.paid.capped):
            return Quote()
        vendor, ident = identity(way.runner, way.model)
        name = f"{vendor}/{ident}" if way.credential == "cli_key" else way.model
        rate = self.cfg.pricing.get(name) or self.cfg.pricing.get(f"{way.runner}:{way.model}")
        if rate:
            return Quote(rate[0], rate[1], source="pricing")
        snapshot = self.remembered("price_snapshot", prices.load)
        if found := prices.applied(snapshot, way.runner, (way.model,), util.now(), request_tokens or None):
            return Quote(*prices.per_million_usd(found), source="snapshot", applied=found)
        if way.runner != "pi":
            return Quote()
        if found := local_quote(way.model, self.probe(way.model, way.mode)):
            return found
        if way.model.startswith(f"{OPENROUTER_PROVIDER}/") and (listed := self.openrouter().get(way.model.partition("/")[2])):
            return Quote(listed["input"], listed["output"], listed["cache_read"], listed["cache_write"], "openrouter")
        return Quote()

    def incompatible(self, way: Way, settings) -> str:
        return incompatible(way, settings, self.device)

    def pi_capabilities(self, model: str) -> dict:
        configured = self.remembered("models.json", pi.model_configuration)
        provider, _, name = model.partition("/")
        listed = accounts.mapping(accounts.mapping(configured.get("providers")).get(provider)).get("models", [])
        entry = next((item for item in listed if accounts.mapping(item).get("id") == name), {})
        entry = accounts.mapping(entry)
        return {"modalities": names(entry.get("input")), "context": whole(entry.get("contextWindow"))}

    def capabilities(self, way: Way) -> dict:
        found: dict = {}
        sources: dict[str, str] = {}

        def take(values: dict, source: str, only_missing: bool = False) -> None:
            for key in config.CAPABILITY_KEYS:
                if values.get(key) is not None and not (only_missing and key in found):
                    found[key], sources[key] = values[key], source

        if way.runner == "pi":
            take(self.pi_capabilities(way.model), "models.json")
            if way.model.startswith(f"{OPENROUTER_PROVIDER}/") and any(
                    key not in found for key in ("modalities", "context", "structured_output")):
                take(self.openrouter().get(way.model.partition("/")[2], {}), "openrouter", only_missing=True)
        for key in (way.model, f"{way.runner}:{way.model}"):
            take(self.declared.get(key, {}), "config")
        return {**found, "source": sources}

    def unmet(self, way: Way, settings, request_tokens: int = 0) -> str:
        need = settings.requires
        if not need.declared:
            return ""
        have = self.capabilities(way)
        reasons = []
        if need.floor and not way.link.requested:
            approved = {identity(*route(m)) for m in need.approved}
            if identity(way.runner, way.model) not in approved:
                reasons.append(f"is not in [routing.floors] {need.floor}")
        if need.modalities:
            offered = have.get("modalities")
            if offered is None:
                reasons.append(f"modalities are not declared for this route; the step needs {', '.join(need.modalities)}")
            elif missing := [m for m in need.modalities if m not in offered]:
                reasons.append(f"takes {', '.join(offered) or 'nothing'} only; the step needs {', '.join(missing)}")
        if need.min_context:
            wanted = max(need.min_context, request_tokens)
            offered = have.get("context")
            if offered is None:
                reasons.append("context is not declared for this route")
            elif offered < wanted:
                who = "this request needs about" if request_tokens > need.min_context else "the step needs"
                reasons.append(f"advertises {offered} tokens of context; {who} {wanted}")
        if need.structured_output:
            offered = have.get("structured_output")
            if offered is None:
                reasons.append("structured output is not declared for this route")
            elif not offered:
                reasons.append("does not give structured output")
        if need.data_handling:
            offered = have.get("data_handling")
            if offered is None:
                reasons.append("data handling is not declared for this route")
            elif offered not in need.data_handling:
                reasons.append(f"data handling is '{offered}'; the step allows {', '.join(need.data_handling)}")
        if need.directory_cleanup and way.runner == "codex":
            found = self.remembered("codex_cleanup", codex.cleanup)
            if found.status == "denied":
                reasons.append(found.detail)
            elif found.status != "able":
                reasons.append(f"directory cleanup could not be confirmed on codex: {found.detail}")
        return "; ".join(reasons)

    def paid_refusal(self, way: Way, price: Quote | None) -> str:
        rules = self.paid
        if way.billing != "paid" or not rules.limited:
            return ""
        provider = self.provider(way)
        if rules.providers is not None and provider not in rules.providers:
            return (f"[routing.paid] allows paid routes on {', '.join(rules.providers) or 'no provider'}; "
                    f"this route is on {provider}")
        for side, cap in (("input", rules.max_input), ("output", rules.max_output)):
            rate = getattr(price, side) if price else None
            if cap is not None and rate is None:
                return f"[routing.paid] max_{side} is {cap:g} USD per million tokens; this route's {side} price is unknown"
            if cap is not None and rate > cap:
                return f"[routing.paid] max_{side} is {cap:g} USD per million tokens; this route charges {rate:g}"
        return ""

    def policy_refusal(self, credential: str, auth_mode: str = "") -> str:
        mode = payment_of(credential, auth_mode)
        if not mode or mode in self.modes:
            return ""
        return (f"[routing] payment_modes allows {', '.join(self.modes) or 'no mode'}; this route is {mode}")

    def judge(self, way: Way, settings, remaining: float | None, request_tokens: int = 0) -> tuple[str, str, str]:
        if why := self.policy_refusal(way.credential):
            return "policy", why, ""
        if why := device_refusal(way.model, way.runner, self.device):
            return "excluded", why, ""
        if why := self.incompatible(way, settings):
            return "incompatible", why, ""
        if why := self.unmet(way, settings, request_tokens):
            return "ineligible", why, ""
        auth = accounts.Auth()
        if way.runner == "pi":
            selected = self.probe(way.model, way.mode)
            if selected.token_error:
                return "refused", scrub.scrub(selected.token_error, selected.hidden), ""
            if way.mode == "login" and not selected.login_available:
                return "missing_login", f"no {selected.provider or 'pi'} login in pi's auth store", ""
            if way.mode == "api_key" and not selected.key_available:
                found = f"no supported {selected.provider or 'pi'} key in {pi.KEYS_FILE.name} or the environment"
                return "missing_key", found, ""
            if selected.error:
                return "unavailable", scrub.scrub(selected.error, selected.hidden), ""
            auth, model = selected.auth, selected.model
        elif way.credential == "cli_key" and way.mode == "api_key":
            selected = self.cli_key_route(way)
            if selected.token_error:
                return "refused", scrub.scrub(selected.token_error, selected.hidden), ""
            if not selected.key:
                return "missing_key", f"no supported {selected.provider} key in {pi.KEYS_FILE.name} or the environment", ""
            if selected.error:
                return "unavailable", scrub.scrub(selected.error, selected.hidden), ""
            auth, model = self.cli_key_auth(way), way.model
        elif way.runner == "codex":
            auth, model = self.codex_auth(), way.model
            if auth.mode == "":
                return "missing_login", "codex CLI is not logged in; run `codex login`", ""
        elif way.runner == "claude":
            auth, model = self.claude_auth(), way.model
            if self.device.remote:
                return "ready", "claude login is not checked on a remote device", ""
            status = self.claude_status()
            if not status or not status.get("loggedIn") or (status.get("authMethod") or status.get("auth_mode")) in API_KEY_METHODS:
                return "missing_login", "claude CLI is not logged in with a plan; run `claude auth login`", ""
            if expired(status.get("loginExpiresAt")):
                return "expired_login", "claude login expired; run `claude auth login`", ""
        else:
            return "ready", "", ""
        if way.billing == "paid" and remaining is not None and remaining <= BUDGET_FLOOR_USD:
            return "budget_spent", f"the daily loop budget has {max(remaining, 0):.4f} USD left", ""
        if hold := auth_hold(auth, model, self.connect):
            return "held", hold_words(hold), hold["until"]
        return "ready", "", ""

    def candidate(self, way: Way, settings, remaining: float | None, request_tokens: int = 0) -> Candidate:
        link = way.link
        price = self.quote(way, request_tokens) if way.billing == "paid" else None
        if why := self.policy_refusal(way.credential) or self.paid_refusal(way, price):
            state_name, detail, until = "policy", why, ""
        else:
            state_name, detail, until = self.judge(way, settings, remaining, request_tokens)
        return Candidate(0, way.model, way.runner, self.provider(way), way.credential, way.billing, link.tier,
                         link.position, link.requested, settings.effort_on(way.runner),
                         settings.instructions_on(way.runner), price,
                         state_name, " ".join(detail.split()),
                         self.capabilities(way) if settings.requires.declared else None, way.mode, until)

    def provider(self, way: Way) -> str:
        if way.runner == "claude":
            return "anthropic"
        if way.runner == "codex":
            return "openai" if way.credential == "cli_key" else "openai-codex"
        return provider_of(way.model) or way.link.provider

    def routes(self, settings, remaining_budget: float | None = None, request_tokens: int = 0) -> list[Candidate]:
        seen = set()
        found = []
        for link in settings.chain if settings.fallback else settings.chain[:1]:
            ways = self.ways(link) if settings.fallback else [self.requested_way(link)]
            for way in ways:
                key = way_key(way)
                if key not in seen:
                    seen.add(key)
                    found.append(self.candidate(way, settings, remaining_budget, request_tokens))
        free = sorted((c for c in found if c.billing == "free"), key=free_key)
        paid = sorted((c for c in found if c.billing == "paid"), key=paid_key)
        return [replace(c, order=n) for n, c in enumerate([*free, *paid], 1)]


def expired(stamp) -> bool:
    try:
        moment = util.parse_iso(stamp)
    except (TypeError, ValueError):
        return False
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)) < util.now()


def described(candidates: list[Candidate]) -> list[dict]:
    rows = []
    for c in candidates:
        price = None if c.price is None else {
            "input": c.price.input, "output": c.price.output, "cache_read": c.price.cache_read,
            "cache_write": c.price.cache_write, "total": c.price.total, "source": c.price.source,
            "quote": c.price.applied}
        rows.append({"order": c.order, "model": c.model, "runner": c.runner, "provider": c.provider,
                     "credential": c.credential, "billing": c.billing, "tier": c.tier, "position": c.position,
                     "requested": c.requested, "effort": c.effort, "instructions": list(c.instructions),
                     "price": price, "state": c.state, "detail": c.detail, "capabilities": c.capabilities,
                     "payment_mode": payment_of(c.credential)})
    return rows


def request_tokens(prompt: str, system: str, schema) -> int:
    text = prompt + system + (json.dumps(schema) if schema else "")
    return math.ceil(len(text.encode("utf-8")) / 3)


def select(found: Discovery, settings, remaining: float | None = None, request_tokens: int = 0) -> Selection:
    own = Way(settings.chain[0], settings.runner, settings.model, "", "")
    why = found.unmet(own, settings, request_tokens)
    if not why:
        return Selection(None, True, "the requested route meets the step's requirements")
    candidates = found.routes(settings, remaining, request_tokens)
    chosen = next((n for n, c in enumerate(candidates)
                   if c.state == "ready" and not (c.requested and c.runner == settings.runner)), None)
    if chosen is None and any(c.state == "held" for c in candidates):
        return Selection(None, False, f"requested {settings.model} {why}; waiting on held routes", tuple(candidates))
    if chosen is None:
        tried = "; ".join(f"{c.model} ({c.runner}, {c.credential}): {c.state}: {c.detail}" for c in candidates)
        return Selection(None, False, "", tuple(candidates),
                         f"no route meets step {settings.step}'s requirements: {tried}")
    picked = candidates[chosen]
    skipped = tuple(c for n, c in enumerate(candidates) if n != chosen and (c.requested or n < chosen))
    return Selection(picked, False, f"requested {settings.model} {why}; took {picked.model}", skipped)


def described_selection(selection: Selection, settings, request_tokens: int) -> dict:
    need = settings.requires
    if selection.kept:
        selected = {"model": settings.model, "runner": settings.runner, "provider": settings.provider,
                    "credential": "", "billing": "", "reason": selection.reason}
    elif picked := selection.route:
        selected = {"model": picked.model, "runner": picked.runner, "provider": picked.provider,
                    "credential": picked.credential, "billing": picked.billing, "reason": selection.reason}
    else:
        selected = None
    return {"selected": selected,
            "skipped": [{"model": c.model, "runner": c.runner, "credential": c.credential, "state": c.state,
                         "reason": c.detail} for c in selection.skipped],
            "requires": {"modalities": list(need.modalities), "min_context": need.min_context,
                         "structured_output": need.structured_output, "data_handling": list(need.data_handling),
                         "floor": need.floor, "directory_cleanup": need.directory_cleanup},
            "request_tokens": request_tokens, "error": selection.error}


def loop_budget_left(cfg: config.Config) -> float | None:
    if not cfg.daily_budget_usd:
        return None
    return cfg.daily_budget_usd - state.loop_spent_since(util.iso(util.now() - timedelta(days=1)), None,
                                                         cfg.local_providers)
