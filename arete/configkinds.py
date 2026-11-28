from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
PROMPT_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]*"
VERBS = ("list", "read", "versions", "diff")
DEFAULTS_LAYER_ADDRESS = "defaults@layer"
ROLE_NAMES = (
    "plan", "build", "verify", "review", "learn", "uat", "miner", "apply", "eval_mine", "triage",
    "chat", "classify", "loop", "session", "research", "research_plan", "research_search",
    "research_synthesis", "research_audit", "explain", "document", "capture_evidence",
    "config_ask", "check_repair", "review_repair", "ci_repair", "eval_grade", "orchestrate",
)


@dataclass(frozen=True)
class Home:
    pattern: str
    name: str = "{name}"
    escape_repo: bool = False
    name_pattern: str = NAME_PATTERN

    def item(self, path: str) -> str | None:
        pattern = re.escape(self.pattern)
        pattern = pattern.replace(re.escape("{repo}"), f"(?P<repo>{NAME_PATTERN})")
        pattern = pattern.replace(re.escape("{name}"), f"(?P<name>{self.name_pattern})")
        if found := re.fullmatch(pattern, path):
            values = found.groupdict()
            if self.escape_repo:
                values["repo"] = values["repo"].replace(".", "..")
            return self.name.format(**values)
        return None


@dataclass(frozen=True)
class Kind:
    home: tuple[Home, ...]
    schema: PurePosixPath
    gate: str
    verbs: tuple[str, ...] = VERBS


def kind(name: str, gate: str, *home: Home) -> Kind:
    return Kind(home, PurePosixPath("arete/config_schemas") / f"{name}.json", gate)


REGISTRY = {
    "prompt": kind("prompt", "review", Home("prompts/{name}.md", name_pattern=PROMPT_NAME_PATTERN),
                   Home("layers/{repo}/prompts/{name}.append.md", "{repo}.{name}.append",
                        name_pattern=PROMPT_NAME_PATTERN)),
    "schema": kind("schema", "check", Home("schemas/{name}.json")),
    "adw": kind("adw", "review", Home("adws/{name}.toml")),
    "routes": kind("routes", "review", Home("routes/{name}.toml")),
    "step_settings": kind("step_settings", "review", Home("config.toml", "defaults"),
                          Home("layers/{repo}/config.toml", "{repo}")),
    "models": kind("models", "check", Home("models/{name}.toml")),
    "prices": kind("prices", "check", Home("prices/{name}.json")),
    "budgets": kind("budgets", "review", Home("budgets/{name}.toml")),
    "connector": kind("connector", "review", Home("connectors/{name}.toml")),
    "source": kind("source", "review", Home("sources/{name}.toml")),
    "target": kind("target", "review", Home("targets/{name}.toml")),
    "rule": kind("rule", "review", Home("rules/{name}.toml")),
    "sync": kind("sync", "review", Home("sync/{name}.toml")),
    "vocabularies": kind("vocabularies", "check", Home("vocabularies.toml", "defaults"),
                         Home("vocabularies/{name}.toml")),
    "device": kind("device", "review", Home("devices/{name}.toml")),
    "loop": kind("loop", "review", Home("loops/{name}/loop.toml"),
                 Home("layers/{repo}/loops/{name}/loop.toml", "{repo}.{name}", escape_repo=True)),
    "attention": kind("attention", "check", Home("attention.toml", "defaults"), Home("config.toml", "defaults")),
    "sound": kind("sound", "check", Home("sounds/{name}.toml")),
    "experiment": kind("experiment", "review", Home("experiments/{name}.toml")),
    "retention": kind("retention", "check", Home("retention.toml", "defaults")),
    "library": kind("library", "check", Home("libraries/{name}.toml")),
}


def classify(path: str | PurePosixPath) -> tuple[str, str] | None:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    path = relative.as_posix()
    for name, entry in REGISTRY.items():
        for home in entry.home:
            item = home.item(path)
            if item is None:
                continue
            if name == "loop" and path.startswith("loops/") and "." in item:
                return None
            return name, item
    return None
