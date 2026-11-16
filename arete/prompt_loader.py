from __future__ import annotations

import re
from dataclasses import dataclass, fields
from pathlib import Path
from string import Template

from . import owner_asks, paths

TRIAGE_ANSWERS = "Answers to triage's questions"


def prompt(name: str, prompts_dir: Path = paths.PROMPTS_DIR, **values) -> str:
    text = (prompts_dir / f"{name}.md").read_text()
    values.setdefault("owner", owner_asks.NOBODY)
    return Template(text).safe_substitute({k: ("" if v is None else str(v)) for k, v in values.items()})


def bash_rules(patterns: list[str]) -> list[str]:
    return [f"Bash({p})" for p in patterns]


def code_list(names: list[str]) -> str:
    return ", ".join(f"`{n}`" for n in names)


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > n:
        s = s[:n + 1].rpartition("-")[0] or s[:n]
    return s or "change"


def fill_template(template: str, default: str, **values) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        return default.format(**values)


@dataclass(frozen=True)
class Brief:
    repo: str
    workspace: str
    ref: str
    title: str
    body: str
    owner: str = owner_asks.NOBODY
    comments: str | None = None
    attachments: str | None = None
    triage: str | None = None
    expertise: str | None = None
    commands: str | None = None
    max_lines: int | None = None

    def prompt(self, name: str, prompts_dir: Path = paths.PROMPTS_DIR, **step) -> str:
        values = {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) is not None}
        return prompt(name, prompts_dir, **(values | step))
