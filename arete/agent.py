from __future__ import annotations

import json
from dataclasses import dataclass, field

READ_TOOLS = ["Read", "Glob", "Grep"]
READ_ONLY_TOOLS = frozenset({*READ_TOOLS, "LS"})
SUBSCRIPTION_RUNNER = "claude"


@dataclass(frozen=True)
class Session:
    id: str = ""
    spent_usd: float = 0.0
    runner: str = ""


@dataclass(frozen=True)
class UsageLimit:
    until: str
    scope: str
    kind: str = ""


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
    model: str = ""
    provider: str = ""
    runner: str = ""

    @property
    def paid_usd(self) -> float:
        return 0.0 if self.runner == SUBSCRIPTION_RUNNER else self.cost_usd


NO_SESSION = Session()


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
