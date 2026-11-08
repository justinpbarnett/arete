from __future__ import annotations

import json
from dataclasses import dataclass, field

READ_TOOLS = ["Read", "Glob", "Grep"]
READ_ONLY_TOOLS = frozenset({*READ_TOOLS, "LS"})


@dataclass(frozen=True)
class Session:
    id: str = ""
    spent_usd: float = 0.0
    runner: str = ""


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
    raw: dict = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    provider: str = ""
    runner: str = ""


NO_SESSION = Session()


def json_from_text(text: str):
    text = text.strip()
    if "```" in text:
        for chunk in reversed(text.split("```")):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith(("{", "[")):
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None
