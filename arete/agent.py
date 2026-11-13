from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import util

READ_TOOLS = ["Read", "Glob", "Grep"]
READ_ONLY_TOOLS = frozenset({*READ_TOOLS, "LS"})
SUBSCRIPTION_RUNNERS = frozenset({"claude", "codex"})
KEY_PROVIDERS = frozenset({"openai"})


def subscription(runner: str, provider: str = "", auth_mode: str | None = "") -> bool:
    if auth_mode in ("subscription", "api_key"):
        return auth_mode == "subscription"
    return runner in SUBSCRIPTION_RUNNERS and not (runner == "codex" and provider in KEY_PROVIDERS)


@dataclass(frozen=True)
class Session:
    id: str = ""
    spent_usd: float = 0.0
    runner: str = ""
    credential: str = ""
    account: str = ""
    generation: str = ""


@dataclass(frozen=True)
class UsageLimit:
    until: str
    scope: str
    kind: str = ""
    billing: str = "subscription"


def model_scope(model: str) -> str:
    name = model.strip().lower().removeprefix("claude:").removeprefix("pi:")
    name = name.removeprefix("anthropic/")
    family = re.fullmatch(r"(?:claude-(?:\d+-)*)?(fable|opus|sonnet|haiku)(?:-(?:\d[\w.-]*|latest))?", name)
    return "model:" + (family[1] if family else name)


def reset_at(value) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        stamp = float(value)
        return datetime.fromtimestamp(stamp / 1000 if stamp >= 1e12 else stamp, UTC) if stamp else None
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            parsed = util.parse_iso(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (ValueError, TypeError, OverflowError):
            return None


def provider_reset(data) -> datetime | None:
    if isinstance(data, dict):
        for key, value in data.items():
            if (re.sub(r"[_-]", "", key).lower() in ("reset", "resetat", "resetsat", "resettime", "resetson")
                    and (found := reset_at(value))):
                return found
        return next((found for value in data.values() if (found := provider_reset(value))), None)
    if isinstance(data, list):
        return next((found for value in data if (found := provider_reset(value))), None)
    if isinstance(data, str):
        try:
            decoded = json.loads(data)
        except ValueError:
            decoded = None
        if isinstance(decoded, (dict, list)) and (found := provider_reset(decoded)):
            return found
        match = re.search(r'''(?:resets?[_-]?(?:at|time|on)?)["'\s:=]+'''
                          r'(\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})|\d{10,16})(?!\d)',
                          data, re.IGNORECASE)
        return reset_at(match[1]) if match else None
    return None


def rate_limit_type(data) -> str:
    if isinstance(data, dict):
        for key, value in data.items():
            if (re.sub(r"[_-]", "", key).lower() == "ratelimittype"
                    and isinstance(value, str) and value.strip()):
                return value.strip()
        return next((found for key, value in data.items() if key not in ("content", "usage", "messages")
                     and (found := rate_limit_type(value))), "")
    if isinstance(data, list):
        return next((found for value in data if (found := rate_limit_type(value))), "")
    if isinstance(data, str):
        try:
            decoded = json.loads(data)
        except ValueError:
            lines = data.splitlines()
            if len(lines) > 1:
                return next((found for line in lines if (found := rate_limit_type(line))), "")
            match = re.search(r'''["']?rate[_-]?limit[_-]?type["']?\s*[:=]\s*["']?([A-Za-z_][\w-]*)''',
                              data, re.IGNORECASE)
            return match[1] if match and match[1].lower() not in ("null", "none", "undefined", "true", "false") else ""
        return rate_limit_type(decoded) if isinstance(decoded, (dict, list, str)) and decoded != data else ""
    return ""


def credit_limit(data) -> UsageLimit | None:
    text = (data if isinstance(data, str) else json.dumps(data, default=str)).lower()
    text = text.replace("_", " ").replace("-", " ")
    explicit = re.search(r'\b(?:insufficient (?:quota|credits?|funds)|payment required|'
                         r'billing (?:hard )?limit reached|(?:out of|not enough|no remaining) credits?|'
                         r'no credits? (?:left|available)|'
                         r'(?:request )?requires more credits)\b|'
                         r'\b(?:credit balance|credits?|(?:billing|credit|spend(?:ing)?) (?:cap|limit|quota)) '
                         r'(?:(?:is|are|was|were|has been|have been|already|fully|completely) )?'
                         r'(?:insufficient|exhausted|depleted|exceeded|too low|reached)\b|'
                         r'\b(?:insufficient|exhausted|depleted|exceeded) '
                         r'(?:(?:your|the|all|available|remaining|monthly|daily|current) )*'
                         r'(?:credits?|credit balance|billing quota|spend(?:ing)? (?:cap|limit))\b', text)
    if not explicit:
        return None
    now = util.now()
    reset = provider_reset(data)
    until = max(now + timedelta(seconds=1), reset) if reset and reset > now else now + timedelta(hours=1)
    return UsageLimit(util.iso(until), "credit", "credit", "credit")


@dataclass
class Result:
    ok: bool
    text: str = ""
    session: Session = field(default_factory=Session)
    structured: dict | list | None = None
    cost_usd: float = 0.0
    turns: int = 0
    error: str = ""
    connection_failed: bool = False
    usage_limit: UsageLimit | None = None
    raw: dict = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    api_seconds: float = 0.0
    first_context_tokens: int = 0
    model: str = ""
    provider: str = ""
    runner: str = ""
    auth_mode: str = ""
    aggregate_paid_usd: float | None = None
    auth_modes: tuple[str, ...] = ()
    calls: list[Result] = field(default_factory=list, repr=False)
    call_seconds: float = 0.0
    credential: str = ""
    effort: str = ""
    requested_effort: str = ""
    skipped: list[dict] = field(default_factory=list)
    record: str = ""
    quote: dict | None = None
    reference_usd: float | None = None

    @property
    def modes(self) -> tuple[str, ...]:
        return self.auth_modes or ((self.auth_mode,) if self.auth_mode else ())

    @property
    def paid_usd(self) -> float:
        if self.aggregate_paid_usd is not None:
            return self.aggregate_paid_usd
        return 0.0 if subscription(self.runner, self.provider, self.auth_mode) else self.cost_usd


NO_SESSION = Session()


def usage_totals(usages: list[dict]) -> dict:
    return {"cost_usd": sum(float((u.get("cost") or {}).get("total") or 0) for u in usages),
            "tokens_in": sum(int(u.get("input") or 0) + int(u.get("cacheRead") or 0) + int(u.get("cacheWrite") or 0)
                             for u in usages),
            "tokens_out": sum(int(u.get("output") or 0) for u in usages),
            "cache_read": sum(int(u.get("cacheRead") or 0) for u in usages),
            "cache_write": sum(int(u.get("cacheWrite") or 0) for u in usages)}


def claude_tokens(usage: dict) -> dict:
    return {"tokens_in": sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                               "cache_read_input_tokens")),
            "tokens_out": int(usage.get("output_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens") or 0)}


def fenced_json(text: str):
    decoder = json.JSONDecoder()
    answer = None
    at = text.find("```")
    while at != -1:
        opening = text[at + 3 :].lstrip().removeprefix("json").lstrip()
        at = text.find("```", at + 3)
        if not opening.startswith(("{", "[")):
            continue
        try:
            value, end = decoder.raw_decode(text, len(text) - len(opening))
        except json.JSONDecodeError:
            continue
        rest = text[end:].lstrip()
        if not rest or rest.startswith("```"):
            answer = value
            at = text.find("```", end)
    return answer


def json_from_text(text: str):
    text = text.strip()
    if (fenced := fenced_json(text)) is not None:
        return fenced
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None
