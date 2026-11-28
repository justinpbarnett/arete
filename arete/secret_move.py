from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections import Counter
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import quote, unquote

from . import config, paths, secretstore, util
from .refusals import Refusal

LOCK = "secret-move"
STATE = paths.STATE_DIR / "secret-move.json"
REPOS = paths.CONFIG_DIR / "secrets"
CONFIG_KEYS = {"jira": "env_file", "github_app": "private_key"}
JIRA_FIELDS = {"JIRA_BASE_URL": "base_url", "JIRA_EMAIL": "email"}
REPO_SUFFIX = ".secrets"
HOOK_SUFFIX = ".secret"


def state():
    try:
        return json.loads(paths.read_confined(STATE))
    except FileNotFoundError:
        return {}


def retired():
    return state().get("retired", False)


def external_files():
    return {Path(path) for path in state().get("external", ())}


def protected_config_files():
    found = external_files()
    with suppress(FileNotFoundError):
        data = config.read_data()
        found.update(path for section in CONFIG_KEYS if (path := configured_path(data, section)) and path.is_file())
    return found


def configured_path(data, section):
    table = data.get(section, {})
    value = table.get(CONFIG_KEYS[section]) if isinstance(table, dict) else None
    return paths.expand(value) if isinstance(value, str) and value.strip() else None


def fallback(path, *, repo=False):
    if path is None:
        return None
    found = state()
    if found.get("retired") and (not repo or config.secret_settings().store):
        return None
    if str(path.resolve()) not in found.get("external", ()):
        return path
    return None


def pi_env():
    source = fallback(paths.PI_KEYS_FILE)
    return util.read_env_file(source) if source else {}


def repo_file(repo):
    return REPOS / (quote(repo, safe="") + REPO_SUFFIX)


def files_with_suffix(folder, suffix):
    try:
        return sorted(p for p in folder.iterdir() if p.name.endswith(suffix))
    except FileNotFoundError:
        return []


def repo_files():
    return files_with_suffix(REPOS, REPO_SUFFIX)


def repo_contents(raw):
    try:
        found = json.loads(raw, object_pairs_hook=unique_fields)
    except (ValueError, RecursionError):
        raise ValueError("repo file must be a JSON object with unique names and text values") from None
    if not isinstance(found, dict) or not all(isinstance(v, str) for v in found.values()):
        raise ValueError("repo file must be a JSON object with unique names and text values")
    return found


def unique_fields(pairs):
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("legacy file contains duplicate names")
    return found


def read_repo(path):
    with suppress(OSError, ValueError):
        found = json.loads(paths.read_confined(path))
        if isinstance(found, dict):
            return {name: value for name, value in found.items() if isinstance(value, str)}
    return {}


def repo_target(repo):
    path = repo_file(repo)
    root = paths.CONFIG_DIR.resolve()
    parent = path.parent.resolve()
    if not parent.is_relative_to(root):
        raise secretstore.StoreError("legacy repo folder is not inside CONFIG_DIR")
    if paths.ISOLATED and paths.confined(path) is None:
        raise secretstore.StoreError("legacy repo file is not inside the instance's scratch folder")
    return parent / path.name, root


@contextmanager
def legacy_repo_write():
    with util.lock(LOCK, wait=True), util.lock("config", wait=True):
        if config.secret_settings().store:
            raise secretstore.StoreError("a secret store is configured; retry through the server")
        yield


def put_legacy_repo(repo, values):
    with legacy_repo_write():
        path, root = repo_target(repo)
        try:
            found = repo_contents(snapshot(path))
        except FileNotFoundError:
            found = {}
        secretstore.write_file(path, root, json.dumps({**found, **values}).encode())


def remove_legacy_repo(repo, name):
    with legacy_repo_write():
        remove = repo_removal(repo, name)
        if remove is None:
            return False
        remove()
        return True


def repo_removal(repo, name):
    path, root = repo_target(repo)
    try:
        raw = snapshot(path)
    except FileNotFoundError:
        return None
    found = repo_contents(raw)
    if name not in found:
        return None
    del found[name]

    def remove():
        if snapshot(path) != raw:
            raise secretstore.StoreError("legacy repo file changed; retry removal")
        if found:
            secretstore.write_file(path, root, json.dumps(found).encode())
        else:
            unlink(path, raw)

    return remove


def snapshot(path):
    parent = path.parent.resolve()
    if paths.ISOLATED and (paths.confined(path) is None or
                           paths.scratch_root(path) != paths.scratch_root(paths.STATE_DIR)):
        raise ValueError("legacy file must be inside the instance's scratch folder")
    with paths.directory_fd(parent) as directory:
        return snapshot_at(directory, path.name)


def snapshot_at(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("legacy input must be a regular file")
        return file.read()


def unlink(path, raw):
    parent = path.parent.resolve()
    if not parent.is_relative_to(paths.CONFIG_DIR.resolve()):
        raise secretstore.StoreError("legacy file is not inside the config folder")
    with paths.directory_fd(parent) as directory:
        if snapshot_at(directory, path.name) != raw:
            raise secretstore.StoreError("legacy file changed during the move")
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)


def env_contents(text):
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, equal, value = line.removeprefix("export ").partition("=")
            if not equal:
                raise ValueError("legacy env file must contain assignments")
            pairs.append((name.strip(), util.env_value(value)))
    return unique_fields(pairs)


def copy_jira_fields(source, values):
    from . import settings

    fields = {field: values[env] for env, field in JIRA_FIELDS.items() if values.get(env)}
    if not fields:
        return
    with util.lock("config", wait=True):
        data = config.read_data()
        if configured_path(data, "jira") != source:
            raise ValueError("the configured Jira env file changed")
        fields = {key: value for key, value in fields.items() if not data["jira"].get(key)}
        if not fields:
            return
        lines = paths.CONFIG_FILE.read_text().splitlines(keepends=True)
        for key, value in fields.items():
            settings.set_key(lines, "jira", key, json.dumps(value, ensure_ascii=False))
        settings.write_checked("".join(lines), lambda cfg: (
            "Jira settings failed readback" if any(getattr(cfg.jira, k) != v for k, v in fields.items()) else ""))


def adopt_values(scope, values, active, provider=lambda _name: None, expose=lambda _name: None):
    rejected = []
    for name, value in values.items():
        try:
            secretstore.adopt(scope, name, value, "api_key", provider=provider(name), managed_by="external",
                              settings=active, expose=expose(name))
        except (ValueError, OSError, sqlite3.Error) as error:
            rejected.append(failure_rule(error))
    if rejected:
        raise secretstore.StoreError("; ".join(dict.fromkeys(rejected)))


def adopt_file(kind, path, raw, active):
    text = raw.decode()
    adopt = secretstore.adopt
    if kind == "pi":
        from . import pi

        def provider(name):
            return next((p for p, key in pi.KEY_NAMES.items() if key == name), None)

        adopt_values("global", env_contents(text), active, provider,
                     expose=lambda name: [{"env": name, "roles": ["runner:pi"]}])
    elif kind == "jira":
        values = env_contents(text)
        if values.keys() - {*JIRA_FIELDS, "JIRA_API_TOKEN"}:
            raise ValueError("Jira env file can only contain JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN")
        if "JIRA_API_TOKEN" in values:
            adopt("global", "JIRA_API_TOKEN", values["JIRA_API_TOKEN"], "api_key", provider="jira",
                  managed_by="external", settings=active)
        copy_jira_fields(path, values)
    elif kind == "github_app":
        adopt("global", "GITHUB_APP_PRIVATE_KEY", text, "app_private_key", provider="github", managed_by="external",
              settings=active)
    elif kind == "hook":
        from . import webhooks

        loop = path.name.removesuffix(HOOK_SUFFIX)
        adopt(webhooks.scope(loop), webhooks.KEY_NAME, text.strip(), "webhook_secret", provider="webhook",
              managed_by="arete", settings=active)
    elif kind == "repo":
        repo = unquote(path.name.removesuffix(REPO_SUFFIX))
        adopt_values(f"repo:{repo}", repo_contents(raw), active,
                     expose=lambda name: [{"env": name, "roles": ["step:*", "runner:*"]}])


def files(data):
    from . import webhooks

    yield "pi", paths.PI_KEYS_FILE
    for section in CONFIG_KEYS:
        if path := configured_path(data, section):
            yield section, path
    yield from (("hook", file) for file in files_with_suffix(webhooks.HOOKS_DIR, HOOK_SUFFIX))
    yield from (("repo", file) for file in repo_files())


def failure_rule(error):
    if isinstance(error, UnicodeError):
        return "the legacy file must be UTF-8"
    if isinstance(error, Refusal):
        return "config changes must pass checked readback"
    if isinstance(error, (ValueError, secretstore.StoreError)):
        return str(error)
    return f"could not safely read, persist or remove the file ({type(error).__name__})"


def failure(path, error):
    return f"{paths.short_path(path)}: {failure_rule(error)}"


def cleanup(path, raw, active):
    removed = False

    def remove(_c):
        nonlocal removed
        unlink(path, raw)
        removed = True

    secretstore.sync_before_cleanup(lambda _c: config.secret_settings() == active, remove, timeout=5)
    if not removed:
        raise secretstore.StoreError("the configured store changed during the move")


def run():
    active = config.secret_settings()
    if not active.store:
        return {}
    with util.lock(LOCK) as held:
        if not held or config.secret_settings() != active:
            return None
        if secretstore.db.transaction_mode() is not None:
            raise secretstore.StoreError("file adoption requires its own committed registry writes")
        previous = state()
        external = set(previous.get("external", ()))
        done = previous.get("retired", False)
        problems = []
        try:
            data = config.read_data()
        except FileNotFoundError:
            data = {}
        pending = []
        retry = ((kind, Path(path)) for kind, path in previous.get("pending", ())
                 if kind not in CONFIG_KEYS or configured_path(data, kind) == Path(path))
        try:
            candidates = [(kind, path, path.resolve()) for kind, path in (retry if done else files(data))]
        except (OSError, RuntimeError):
            candidates = []
            problems.append("Could not enumerate legacy secret files safely.")
        seen = Counter(actual for _, _, actual in candidates)
        root = paths.CONFIG_DIR.resolve()
        declared_root = Path(os.path.abspath(paths.CONFIG_DIR))
        inside_left = bool(problems)
        for kind, path, actual in candidates:
            inside = actual.is_relative_to(root)
            inside_config = inside or Path(os.path.abspath(path)).is_relative_to(declared_root)
            if not inside and str(actual) in external:
                continue
            try:
                raw = snapshot(path)
            except FileNotFoundError:
                continue
            except (ValueError, OSError) as error:
                inside_left |= inside_config
                if not inside_config:
                    pending.append([kind, str(path)])
                problems.append(failure(path, error))
                continue
            try:
                if seen[actual] > 1 or actual == paths.CONFIG_FILE.resolve():
                    raise ValueError("the path is also used as another config input")
                if actual.is_relative_to(paths.ZTE_AUTHORITY.resolve()):
                    raise ValueError("the release supervisor's files are not registry inputs")
                adopt_file(kind, path, raw, active)
                if inside:
                    cleanup(path, raw, active)
                elif config.secret_settings() == active:
                    external.add(str(actual))
                else:
                    raise secretstore.StoreError("the configured store changed during the move")
            except (ValueError, OSError, Refusal, sqlite3.Error) as error:
                inside_left |= inside_config
                if not inside_config:
                    pending.append([kind, str(path)])
                problems.append(failure(path, error))
        if config.secret_settings() != active:
            return None
        done = done or not inside_left
        updated = {"retired": done, "external": sorted(external), "pending": pending}
        if updated != previous:
            util.write_durably(STATE, json.dumps(updated).encode(), 0o600)
        push_registered = False
        try:
            if paths.PUSH_KEY_FILE.is_file():
                row = secretstore.get("global", "VAPID_PRIVATE_KEY")
                if row is None:
                    value = snapshot(paths.PUSH_KEY_FILE).decode()
                    row = secretstore.adopt("global", "VAPID_PRIVATE_KEY", value, "signing_key", managed_by="arete",
                                            owner_file=paths.PUSH_KEY_FILE.name, settings=active)
                if row["store"] != "owner_file":
                    raise secretstore.StoreError("registry already contains this address in another store")
                push_registered = True
        except (ValueError, OSError, sqlite3.Error) as error:
            problems.append(failure(paths.PUSH_KEY_FILE, error))
        notes = [f"Moved secrets from {path}; the file was left outside CONFIG_DIR and will no longer be read."
                 for path in sorted(external)]
        if done:
            notes.append("Legacy secret-file readers are retired: no secret files remain in CONFIG_DIR.")
        return {"retired": done, "pending": not done or bool(pending), "push_registered": push_registered,
                "notes": notes, "problems": problems}


def ignored_notes(data):
    return [f"config.toml still names [{section}] {key}; the legacy key is ignored."
            for section, key in CONFIG_KEYS.items() if key in data.get(section, {})]
