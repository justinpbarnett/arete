import json
from dataclasses import asdict
from pathlib import Path

from . import config


def arguments(cfg: config.Config) -> tuple[str, ...]:
    if not cfg.jev.guard:
        return ()
    repos = [(repo.name, str(repo.path), repo.jev) for repo in cfg.repos]
    return "--judge-settings", json.dumps([asdict(cfg.jev), repos], separators=(",", ":"))


def decode(raw: str) -> config.Config:
    jev, repos = json.loads(raw)
    return config.Config(jev=config.JevConfig(**jev),
                         repos=[config.Repo(name, Path(path), jev=allowed) for name, path, allowed in repos])
