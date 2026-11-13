from __future__ import annotations

import http.client
import json
import math
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, timedelta

from . import accounts, claude, codex, config, devices, environment, paths, pi, record, runner, state, util
from .agent import model_scope

CLAUDE_PI_MODELS = {"fable": "claude-fable-5-2", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                    "haiku": "claude-haiku-5-5"}
VENDORS = {"openai-codex": "openai", "openai": "openai", "anthropic": "anthropic"}
CLAUDE_FAMILIES = ("fable", "opus", "sonnet", "haiku")
API_KEY_METHODS = ("api_key", "apiKey", "api-key")
EFFORT_ORDER = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_LEVELS = {"claude": claude.EFFORTS, "pi": pi.THINKING_LEVELS, "codex": codex.EFFORTS,
                 runner.REPLAY: EFFORT_ORDER}
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_PROVIDER = "openrouter"
PRICE_FILE = "openrouter-prices.json"
PRICE_FRESH = timedelta(hours=6)
PRICE_TIMEOUT_SECONDS = 5
PRICE_MAX_BYTES = 8 * 1024 * 1024
PAID_RANK = {"pi_oauth": 0, "pi_key": 1, "cli_key": 2}


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
class Quote:
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    source: str = ""

    @property
    def total(self) -> float | None:
        return None if self.input is None or self.output is None else number(self.input + self.output)


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


@dataclass(frozen=True)
class Way:
    link: Link
    runner: str
    model: str
    credential: str
    billing: str
    mode: str = ""


def nearest_effort(which: str, effort: str) -> str:
    levels = EFFORT_LEVELS.get(which, ())
    if effort in levels:
        return effort
    if effort not in EFFORT_ORDER:
        return ""
    lower = [level for level in EFFORT_ORDER[:EFFORT_ORDER.index(effort)] if level in levels]
    return lower[-1] if lower else ""


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
    which, name = runner.route(source.get("model", ""), source.get("runner", ""))
    provider = source.get("provider", "") or runner.provider_of(name)
    if which == "pi" and source.get("provider") and not name.startswith(f"{provider}/"):
        name = f"{provider}/{name}"
    return which, name, provider


def entry_problem(entry: dict) -> str:
    hint = entry.get("runner", "")
    if hint and hint not in runner.RUNNERS:
        return f"runner '{hint}' is not one of {', '.join(runner.RUNNERS)}"
    if not entry.get("model"):
        return ""
    which, name, provider = source_route(entry)
    lowered = name.lower()
    wrong = False
    if which == "claude":
        wrong = "/" in name or lowered.startswith(runner.CODEX_NAMES) or provider not in ("", "anthropic")
    elif which == "codex":
        wrong = (lowered.startswith("claude-") or model_scope(name).removeprefix("model:") in CLAUDE_FAMILIES
                 or "/" in name or provider not in ("", "openai"))
    return f"runner '{which}' cannot run model '{entry['model']}'" if wrong else ""


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        found = float(value)
    except ValueError:
        return None
    return found if math.isfinite(found) and found >= 0 else None


def per_million(value) -> float | None:
    found = number(value)
    scaled = None if found is None else number(found * 1e6)
    return None if scaled is None else round(scaled, 6)


def openrouter_prices() -> dict[str, dict]:
    file = paths.STATE_DIR / PRICE_FILE
    try:
        cached = util.read_json(file, {})
        if isinstance(cached.get("models"), dict) and util.now() - util.parse_iso(cached["fetched"]) < PRICE_FRESH:
            return {name: {key: number(accounts.mapping(rates).get(key))
                           for key in ("input", "output", "cache_read", "cache_write")}
                    for name, rates in cached["models"].items()}
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        pass
    request = urllib.request.Request(OPENROUTER_URL, headers={"Accept": "application/json", "User-Agent": "arete"})
    try:
        with urllib.request.build_opener(accounts.NoRedirect).open(request, timeout=PRICE_TIMEOUT_SECONDS) as response:
            data = response.read(PRICE_MAX_BYTES + 1)
            if response.status != 200 or len(data) > PRICE_MAX_BYTES:
                return {}
        models = {}
        for item in json.loads(data)["data"]:
            rates = item.get("pricing") or {}
            models[item["id"]] = {"input": per_million(rates.get("prompt")), "output": per_million(rates.get("completion")),
                                  "cache_read": per_million(rates.get("input_cache_read")),
                                  "cache_write": per_million(rates.get("input_cache_write"))}
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
            return Quote(number(cost.get("input")), number(cost.get("output")), number(cost.get("cacheRead")),
                         number(cost.get("cacheWrite")), "models.json")
    return None


def free_key(found: Candidate) -> tuple:
    return not found.requested, found.position, found.runner == "pi"


def paid_key(found: Candidate) -> tuple:
    total = found.price.total
    return (total is None, total or 0.0, not found.requested, found.position, PAID_RANK.get(found.credential, 9))


class Discovery:
    def __init__(self, cfg: config.Config, repo: config.Repo | None = None, device: devices.Device | str = "local",
                 connect=state.db):
        self.cfg = cfg
        self.repo = repo
        self.device = devices.resolve(device)
        self.connect = connect
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
            self.probes[model, mode] = runner.pi_route(model, self.cfg, self.repo, self.device, self.connect, mode=mode)
        return self.probes[model, mode]

    def claude_status(self) -> dict:
        return self.remembered("claude_status", lambda: claude.auth_status(claude.agent_env(self.passing)))

    def claude_auth(self) -> accounts.Auth:
        remote = self.device if self.device.remote else None
        return self.remembered("claude_auth", lambda: claude.authentication(self.passing, remote=remote))

    def codex_auth(self) -> accounts.Auth:
        return self.remembered("codex_auth", lambda: codex.authentication(self.passing))

    def openrouter(self) -> dict:
        if self.listed is None:
            self.listed = openrouter_prices()
        return self.listed

    def ways(self, link: Link) -> list[Way]:
        if link.runner == runner.REPLAY:
            return [Way(link, runner.REPLAY, link.model, "replay", "free")]
        vendor, ident = identity(link.runner, link.model)
        if vendor == "anthropic":
            native = link.model if link.runner == "claude" else link.model.partition("/")[2]
            keyed = f"anthropic/{ident}"
            probe = self.probe(keyed, "")
            key = (Way(link, "pi", keyed, "local", "free") if probe.local or probe.provider in self.cfg.local_providers
                   else Way(link, "pi", keyed, "pi_key", "paid", "api_key"))
            return [Way(link, "claude", native, "cli_login", "free"), key]
        if vendor == "openai":
            native = link.model if link.runner == "codex" else ident
            cli = (Way(link, "codex", native, "cli_key", "paid") if self.codex_auth().mode == "api_key"
                   else Way(link, "codex", native, "cli_login", "free"))
            keyed = f"openai/{ident}"
            probe = self.probe(keyed, "")
            key = (Way(link, "pi", keyed, "local", "free") if probe.local or probe.provider in self.cfg.local_providers
                   else Way(link, "pi", keyed, "pi_key", "paid", "api_key"))
            return [cli, Way(link, "pi", f"openai-codex/{ident}", "pi_login", "free", "login"), key]
        probe = self.probe(link.model, "")
        if probe.local or probe.provider in self.cfg.local_providers:
            return [Way(link, "pi", link.model, "local", "free")]
        found = []
        if probe.provider in accounts.PI_SUBSCRIPTIONS:
            found.append(Way(link, "pi", link.model, "pi_login", "free", "login"))
        elif probe.login_available:
            found.append(Way(link, "pi", link.model, "pi_oauth", "paid", "login"))
        if probe.key_supported or not found:
            found.append(Way(link, "pi", link.model, "pi_key", "paid", "api_key"))
        return found

    def quote(self, way: Way) -> Quote:
        rate = self.cfg.pricing.get(way.model) or self.cfg.pricing.get(f"{way.runner}:{way.model}")
        if rate:
            return Quote(rate[0], rate[1], source="pricing")
        if way.runner != "pi":
            return Quote()
        if found := local_quote(way.model, self.probe(way.model, way.mode)):
            return found
        if way.model.startswith(f"{OPENROUTER_PROVIDER}/") and (listed := self.openrouter().get(way.model.partition("/")[2])):
            return Quote(listed["input"], listed["output"], listed["cache_read"], listed["cache_write"], "openrouter")
        return Quote()

    def incompatible(self, way: Way, settings) -> str:
        if way.runner == "pi":
            if settings.mcp:
                return f"mcp {', '.join(settings.mcp)} cannot load on pi, which runs with --no-mcp"
            if settings.instructions_on("pi"):
                return f"instructions {', '.join(settings.instructions_on('pi'))} cannot load on pi"
            if runner.WRITING_TOOLS & set(settings.tools) and not pi.can_confine(False):
                return pi.NO_CONFINEMENT_REASON
        if way.runner in ("pi", "codex") and (missing := [t for t in runner.READ_TOOLS if t not in settings.tools]):
            return f"tools leave out {', '.join(missing)}, which {way.runner} always grants"
        if way.runner == "codex" and "user" in settings.instructions_on("codex"):
            return "instructions user cannot load on codex, which runs without your own codex setup"
        return ""

    def judge(self, way: Way, settings, remaining: float | None) -> tuple[str, str]:
        if why := runner.device_refusal(way.model, way.runner, self.device):
            return "excluded", why
        if why := self.incompatible(way, settings):
            return "incompatible", why
        auth = accounts.Auth()
        if way.runner == "pi":
            selected = self.probe(way.model, way.mode)
            if selected.token_error:
                return "refused", record.scrub(selected.token_error, selected.hidden)
            if way.mode == "login" and not selected.login_available:
                return "missing_login", f"no {selected.provider or 'pi'} login in pi's auth store"
            if way.mode == "api_key" and not selected.key_available:
                return "missing_key", f"no supported {selected.provider or 'pi'} key in {pi.KEYS_FILE.name} or the environment"
            if selected.error:
                return "unavailable", record.scrub(selected.error, selected.hidden)
            auth, model = selected.auth, selected.model
        elif way.runner == "codex":
            auth, model = self.codex_auth(), way.model
            if auth.mode == "":
                return "missing_login", "codex CLI is not logged in; run `codex login`"
        elif way.runner == "claude":
            auth, model = self.claude_auth(), way.model
            if self.device.remote:
                return "ready", "claude login is not checked on a remote device"
            status = self.claude_status()
            if not status or not status.get("loggedIn") or (status.get("authMethod") or status.get("auth_mode")) in API_KEY_METHODS:
                return "missing_login", "claude CLI is not logged in with a plan; run `claude auth login`"
            if expired(status.get("loginExpiresAt")):
                return "expired_login", "claude login expired; run `claude auth login`"
        else:
            return "ready", ""
        if hold := runner.auth_hold(auth, model, self.connect):
            return "held", runner.hold_words(hold)
        if way.billing == "paid" and remaining is not None and remaining <= runner.BUDGET_FLOOR_USD:
            return "budget_spent", f"the daily loop budget has {max(remaining, 0):.4f} USD left"
        return "ready", ""

    def candidate(self, way: Way, settings, remaining: float | None) -> Candidate:
        link = way.link
        state_name, detail = self.judge(way, settings, remaining)
        return Candidate(0, way.model, way.runner, self.provider(way), way.credential, way.billing, link.tier,
                         link.position, link.requested, settings.effort_on(way.runner),
                         settings.instructions_on(way.runner), self.quote(way) if way.billing == "paid" else None,
                         state_name, " ".join(detail.split()))

    def provider(self, way: Way) -> str:
        if way.runner == "claude":
            return "anthropic"
        if way.runner == "codex":
            return "openai" if way.credential == "cli_key" else "openai-codex"
        return runner.provider_of(way.model) or way.link.provider

    def routes(self, settings, remaining_budget: float | None = None) -> list[Candidate]:
        seen = set()
        found = []
        for link in settings.chain if settings.fallback else settings.chain[:1]:
            if settings.fallback:
                ways = self.ways(link)
            elif link.runner == "pi":
                selected = self.probe(link.model, "")
                local = selected.local or selected.provider in self.cfg.local_providers
                paid = not local and selected.auth.mode != "subscription"
                credential = ("local" if local else "pi_oauth" if selected.using_login and paid else
                              "pi_login" if selected.using_login else "pi_key")
                ways = [Way(link, "pi", selected.model, credential, "paid" if paid else "free",
                            "" if local else "login" if selected.using_login else "api_key")]
            elif link.runner == "codex" and self.codex_auth().mode == "api_key":
                ways = [Way(link, link.runner, link.model, "cli_key", "paid")]
            else:
                ways = [Way(link, link.runner, link.model, "replay" if link.runner == runner.REPLAY else
                            "cli_login", "free")]
            for way in ways:
                key = (way.runner, identity(way.runner, way.model), way.credential)
                if key not in seen:
                    seen.add(key)
                    found.append(self.candidate(way, settings, remaining_budget))
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
            "cache_write": c.price.cache_write, "total": c.price.total, "source": c.price.source}
        rows.append({"order": c.order, "model": c.model, "runner": c.runner, "provider": c.provider,
                     "credential": c.credential, "billing": c.billing, "tier": c.tier, "position": c.position,
                     "requested": c.requested, "effort": c.effort, "instructions": list(c.instructions),
                     "price": price, "state": c.state, "detail": c.detail})
    return rows


def loop_budget_left(cfg: config.Config) -> float | None:
    if not cfg.daily_budget_usd:
        return None
    return cfg.daily_budget_usd - state.loop_spent_since(util.iso(util.now() - timedelta(days=1)))
