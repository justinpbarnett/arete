from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from . import config, util, vault

BASE = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_*", "TZ", "TMPDIR", "TERM", "XDG_CONFIG_HOME",
        "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "ARETE_CONFIG_DIR", "ARETE_STATE_DIR", "ARETE_LOG_DIR",
        "ARETE_WORKTREES_DIR")
SHELL_BOOKKEEPING = ("_", "PWD", "OLDPWD", "SHLVL", "ARETE_SERVE_RESTARTED_FOR_NEW_CODE")


def passes(name: str, patterns: Iterable[str]) -> bool:
    return any(p == "*" or p == name or (p.endswith("*") and name.startswith(p[:-1])) for p in patterns)


def build(names: Iterable[str], sets: Mapping[str, str] | None = None, *, never: Iterable[str] = (),
          source: Mapping[str, str] | None = None) -> dict[str, str]:
    patterns, never = [*BASE, *names], set(never)
    kept = {k: v for k, v in (os.environ if source is None else source).items()
            if k not in never and passes(k, patterns)}
    return {**kept, **(sets or {})}


def passed(cfg: config.Config, repo: config.Repo | None = None) -> list[str]:
    names = list(cfg.environment.passes)
    if repo is None:
        return names
    try:
        return names + list(config.load_layer(repo.layer_dir).passes)
    except (ValueError, TypeError, OSError):
        return names


def reserved(name: str) -> bool:
    return passes(name, BASE) or name in SHELL_BOOKKEEPING or name.startswith("ARETE_")


def stored(repo: config.Repo | None) -> dict[str, str]:
    if repo is None:
        return {}
    return {n: v for n, v in vault.read(repo.name).items() if not reserved(n)}


def started(cfg: config.Config, repo: config.Repo | None = None,
            sets: Mapping[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    kept = stored(repo)
    return build(passed(cfg, repo), {**kept, **(sets or {})}), kept


def commands(cfg: config.Config, repo: config.Repo | None = None, sets: Mapping[str, str] | None = None) -> dict:
    return started(cfg, repo, sets)[0]


def sh(cmd: list[str] | str, cfg: config.Config, repo: config.Repo | None = None,
       sets: Mapping[str, str] | None = None, **kw):
    env, kept = started(cfg, repo, sets)
    given = set(kept.values())
    res = util.sh(cmd, env=env, inherit=False, **kw)
    res.stdout = vault.hidden(res.stdout or "", given)
    res.stderr = vault.hidden(res.stderr or "", given)
    return res


def dropped(cfg: config.Config, source: Mapping[str, str] | None = None) -> list[str]:
    patterns = [*BASE, *cfg.environment.passes]
    return sorted(k for k in (os.environ if source is None else source)
                  if k not in SHELL_BOOKKEEPING and not passes(k, patterns))
