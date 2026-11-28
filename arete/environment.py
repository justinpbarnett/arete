from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterable, Mapping

from . import config, paths, scrub, secretstore, util, vault
from .store import db

BASE = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_*", "TZ", "TMPDIR", "TERM", "XDG_CONFIG_HOME",
        "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "ARETE_CONFIG_DIR", "ARETE_STATE_DIR", "ARETE_LOG_DIR",
        "ARETE_WORKTREES_DIR", "SSH_AUTH_SOCK", "ARETE_REPLAY_DIR", "ARETE_SERVER")
SHELL_BOOKKEEPING = ("_", "PWD", "OLDPWD", "SHLVL", "ARETE_SERVE_RESTARTED_FOR_NEW_CODE")
LOADER_HOOKS = ("LD_", "DYLD_", "PYTHON", "GIT_", "NPM_CONFIG_", "CLAUDE_CODE_", "CODEX_")
SHELL_HOOKS = ("BASH_ENV", "ENV", "ZDOTDIR", "PROMPT_COMMAND", "PERL5OPT", "RUBYOPT", "JAVA_TOOL_OPTIONS",
               "NODE_OPTIONS", "NODE_PATH", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL")
INTERNAL_ARETE = ("ARETE_DRIVE_FLUSH", "ARETE_VERIFY_SCRATCH", "ARETE_DESKTOP_NOTIFY", "ARETE_DEVELOPMENT",
                  "ARETE_TEST_STACK_PREFIX")


def passes(name: str, patterns: Iterable[str]) -> bool:
    return any(p == "*" or p == name or (p.endswith("*") and name.startswith(p[:-1])) for p in patterns)


def without(*drop: str) -> dict[str, str]:
    excluded = set(drop)
    return {k: v for k, v in os.environ.items() if k not in excluded}


def exposed_env_names() -> set[str]:
    if not secretstore.configured_registry() or db.isolated_busy():
        return set()
    names: set[str] = set()
    with contextlib.suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema, RuntimeError, ValueError):
        for row in secretstore.rows():
            for rule in secretstore.expose_rules(row):
                names.add(rule["env"])
    return names


def role_secrets(role: str | None, *, repo: str | None = None, never: Iterable[str] = ()) -> dict[str, str]:
    if role is None:
        return {}
    excluded = set(never)
    found: dict[str, str] = {}
    if secretstore.configured_registry() and not db.isolated_busy():
        with contextlib.suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema, RuntimeError, ValueError):
            for row in secretstore.rows():
                if row["status"] != "set":
                    continue
                scope = row["scope"]
                if scope.startswith("repo:") and (repo is None or scope != f"repo:{repo}"):
                    continue
                matching = [rule["env"] for rule in secretstore.expose_rules(row)
                            if any(secretstore.role_matches(role, pat) for pat in rule["roles"])]
                if not matching:
                    continue
                eligible = [env_name for env_name in matching
                            if env_name not in excluded and not (scope.startswith("repo:") and reserved(env_name))]
                if not eligible:
                    continue
                value, reason = secretstore.read_for_expose(row)
                if reason == "below_floor":
                    secretstore.record_refused(row, role, eligible[0], "below_floor")
                    continue
                if value is not None:
                    for env_name in eligible:
                        found[env_name] = value
    elif repo is not None and (secretstore.role_matches(role, "step:*") or secretstore.role_matches(role, "runner:*")):
        for name, value in vault.read(repo).items():
            if name not in excluded and not reserved(name) and len(value) >= vault.SHORTEST:
                found[name] = value
    return found


def build(names: Iterable[str], sets: Mapping[str, str] | None = None, *, role: str | None = None,
          repo: str | None = None, never: Iterable[str] = (),
          source: Mapping[str, str] | None = None) -> dict[str, str]:
    patterns, excluded = [*BASE, *names], set(never)
    blocked = exposed_env_names()
    raw = os.environ if source is None else source
    kept = {k: v for k, v in raw.items()
            if k not in excluded and k not in blocked and not paths.CREDENTIAL_NAME.search(k) and passes(k, patterns)}
    exposed = role_secrets(role, repo=repo, never=excluded)
    return util.foreground_maintenance({**kept, **exposed, **(sets or {})})


def passed(cfg: config.Config, repo: config.Repo | None = None) -> list[str]:
    names = list(cfg.environment.passes)
    if repo is None:
        return names
    try:
        return names + list(config.load_layer(repo.layer_dir).passes)
    except (ValueError, TypeError, OSError):
        return names


def reserved(name: str) -> bool:
    return (passes(name, BASE) or name in SHELL_BOOKKEEPING or name in SHELL_HOOKS or name.startswith("ARETE_")
            or name.upper().startswith(LOADER_HOOKS))


def stored(repo: config.Repo | None) -> dict[str, str]:
    if repo is None:
        return {}
    return {n: v for n, v in vault.read(repo.name).items() if not reserved(n)}


def started(cfg: config.Config, repo: config.Repo | None = None,
            sets: Mapping[str, str] | None = None, *, role: str = "step:command") -> tuple[dict[str, str], dict[str, str]]:
    kept = role_secrets(role, repo=repo.name if repo is not None else None)
    return build(passed(cfg, repo), {**kept, **(sets or {})}), kept


def commands(cfg: config.Config, repo: config.Repo | None = None, sets: Mapping[str, str] | None = None,
             *, role: str = "step:command") -> dict:
    return started(cfg, repo, sets, role=role)[0]


def sh(cmd: list[str] | str, cfg: config.Config, repo: config.Repo | None = None,
       sets: Mapping[str, str] | None = None, *, role: str = "step:check", **kw):
    env, kept = started(cfg, repo, sets, role=role)
    given = set(kept.values())
    res = util.sh(cmd, env=env, **kw)
    res.stdout = scrub.text(res.stdout or "", "command_output", role, extra=given)
    res.stderr = scrub.text(res.stderr or "", "command_output", role, extra=given)
    return res


def dropped(cfg: config.Config, source: Mapping[str, str] | None = None) -> list[str]:
    patterns = [*BASE, *cfg.environment.passes]
    blocked = exposed_env_names()
    raw = os.environ if source is None else source
    return sorted(k for k in raw
                  if k not in SHELL_BOOKKEEPING
                  and (not passes(k, patterns) or bool(paths.CREDENTIAL_NAME.search(k)) or k in blocked))
