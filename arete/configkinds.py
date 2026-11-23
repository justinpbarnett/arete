from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
VERBS = ("list", "read", "versions", "diff")
DEFAULTS_LAYER_NAME = re.compile(r"defaults(?:\.layer)*")


@dataclass(frozen=True)
class Home:
    pattern: str
    name: str = "{name}"
    escape_repo: bool = False

    def item(self, path: str) -> str | None:
        pattern = re.escape(self.pattern)
        for field in ("repo", "name"):
            pattern = pattern.replace(re.escape("{" + field + "}"), f"(?P<{field}>{NAME_PATTERN})")
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
    "prompt": kind("prompt", "review", Home("prompts/{name}.md"),
                   Home("layers/{repo}/prompts/{name}.append.md", "{repo}.{name}.append")),
    "schema": kind("schema", "check", Home("schemas/{name}.json")),
    "adw": kind("adw", "review", Home("adws/{name}.py")),
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
    "vocabularies": kind("vocabularies", "check", Home("vocabularies/{name}.toml")),
    "device": kind("device", "review", Home("devices.toml", "defaults")),
    "loop": kind("loop", "review", Home("loops/{name}/loop.toml"),
                 Home("layers/{repo}/loops/{name}/loop.toml", "{repo}.{name}", escape_repo=True)),
    "attention": kind("attention", "check", Home("config.toml", "defaults")),
    "sound": kind("sound", "check", Home("sounds/{name}.toml")),
    "experiment": kind("experiment", "review", Home("experiments/{name}.toml")),
}


def prompt_append_address(path: str) -> str:
    _, repo, _, filename = path.split("/")
    name = filename.removesuffix(".append.md")
    return f"layer-{repo.encode().hex()}-{name.encode().hex()}.append"


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
            if name == "prompt" and path.startswith("prompts/") and item.endswith(".append"):
                return None
            if name == "step_settings" and "{repo}" in home.pattern and DEFAULTS_LAYER_NAME.fullmatch(item):
                item += ".layer"
            if name == "loop" and path.startswith("loops/") and "." in item:
                return None
            return name, item
    return None
