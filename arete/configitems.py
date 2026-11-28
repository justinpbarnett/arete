from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tomllib
from contextlib import nullcontext, suppress
from dataclasses import asdict
from difflib import unified_diff
from pathlib import Path

from . import config, configkinds, configrepo, paths, prompts, refusals, settings
from .store import config as config_store
from .store import db as foundation
from .store import inbox as inbox_store

MAX_CONTENT = 1024 * 1024
REVISION = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
CONTENT_VERSION = re.compile(r"^[a-z_]+/.+@[0-9a-f]{12}$")


def kinds() -> list[dict]:
    return [
        {
            "kind": name,
            "home": [h.pattern for h in entry.home],
            "schema": entry.schema.as_posix(),
            "gate": entry.gate,
            "verbs": list(entry.verbs),
        }
        for name, entry in configkinds.REGISTRY.items()
    ]


def _content_version_of(kind: str, name: str, raw: bytes) -> str:
    return f"{kind}/{name}@{hashlib.sha256(raw).hexdigest()[:12]}"


def _prompt_appends(name: str, conn=None) -> list[dict[str, str]]:
    if name.endswith(".append") or not prompts.NAME_RE.fullmatch(name):
        return []
    root = paths.CONFIG_DIR.resolve()
    candidates: list[tuple[str, Path]] = []
    seen_paths: set[Path] = set()

    layers_root = root / "layers"
    if layers_root.is_dir():
        for file in sorted(layers_root.glob(f"*/prompts/{name}.append.md")):
            if not file.is_file():
                continue
            resolved = file.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            repo_name = file.parent.parent.name
            candidates.append((repo_name, resolved))

    cfg, _ = config.load_or_problem()
    for repo in cfg.repos:
        append_file = repo.layer_dir / "prompts" / f"{name}.append.md"
        if not append_file.is_file():
            continue
        resolved = append_file.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        candidates.append((repo.name, resolved))

    out: list[dict[str, str]] = []
    for repo_name, file_path in sorted(candidates, key=lambda pair: (pair[0], str(pair[1]))):
        append_name = f"{repo_name}.{name}.append"
        rel_path = (
            file_path.relative_to(root).as_posix()
            if file_path.is_relative_to(root)
            else f"layers/{repo_name}/prompts/{name}.append.md"
        )
        row = config_store.live_item("prompt", append_name, conn) or config_store.live_by_path(rel_path, conn)
        if row and row.get("version"):
            version = str(row["version"])
        else:
            raw = file_path.read_bytes() if file_path.is_file() else b""
            version = _content_version_of("prompt", append_name, raw)
        out.append({
            "layer": f"layer:{repo_name}",
            "name": append_name,
            "path": rel_path,
            "version": version,
        })
    return out


def _prompt_has_drift(name: str, conn=None) -> bool:
    if name.endswith(".append"):
        return False
    subject = f"config:prompt/{name}"
    if conn is not None:
        return inbox_store.get_open_by_kind_and_subject("drift", subject, connect=lambda: nullcontext(conn)) is not None
    if not paths.DB_FILE.exists():
        return False
    try:
        return inbox_store.get_open_by_kind_and_subject("drift", subject) is not None
    except (foundation.NoDatabase, foundation.IncompatibleSchema, OSError):
        return False


def _prompt_fields(name: str, raw: bytes, conn=None) -> dict[str, object]:
    overrides: str | None = None
    base: str | None = None
    with suppress(UnicodeDecodeError, ValueError):
        meta, _ = prompts.parse(raw)
        if isinstance(meta.get("overrides"), str) and meta["overrides"]:
            overrides = meta["overrides"]
        if isinstance(meta.get("base"), str) and meta["base"]:
            base = meta["base"]
    return {
        "overrides": overrides,
        "base": base,
        "drift": _prompt_has_drift(name, conn),
        "appends": _prompt_appends(name, conn),
    }


def _enrich(entry: dict, conn=None) -> dict:
    kind, name, rel = entry["kind"], entry["name"], entry["path"]
    if entry.get("engine"):
        engine_path = Path(entry["engine"])
        raw = engine_path.read_bytes() if engine_path.is_file() else b""
        out: dict[str, object] = {
            "kind": kind,
            "name": name,
            "path": rel,
            "subject": f"config:{kind}/{name}",
            "version": 0,
            "content_version": _content_version_of(kind, name, raw),
            "level": "engine",
        }
        if kind == "prompt":
            out.update(_prompt_fields(name, raw, conn))
        return out
    lookup_kind = "step_settings" if kind == "attention" else kind
    row = config_store.live_item(lookup_kind, name, conn) or config_store.live_by_path(rel, conn)
    ver = int(row["ver"]) if row and row.get("ver") is not None else 0
    file_path = paths.CONFIG_DIR.resolve() / rel
    raw = file_path.read_bytes() if file_path.is_file() else b""
    if row and kind != "attention":
        cv = row["version"]
    else:
        cv = _content_version_of(kind, name, raw)
    level = row["origin"] if row and row["origin"].startswith("layer:") else configrepo.level_of(rel)
    out = {
        "kind": kind,
        "name": name,
        "path": rel,
        "subject": f"config:{kind}/{name}",
        "version": ver,
        "content_version": cv,
        "level": level,
    }
    if kind == "prompt":
        out.update(_prompt_fields(name, raw, conn))
    return out


def _engine_items(kind: str) -> dict[str, dict]:
    if kind == "library":
        target = paths.ENGINE_ADWS_DIR / "library.toml"
        if not target.is_file():
            return {}
        return {
            "adw": _enrich({
                "kind": "library",
                "name": "adw",
                "path": "arete/adws/library.toml",
                "engine": str(target),
            }),
        }
    if kind not in ("target", "rule"):
        return {}
    folder = paths.DEFAULTS_DIR / f"{kind}s"
    if not folder.is_dir():
        return {}
    out: dict[str, dict] = {}
    for file in sorted(folder.glob("*.toml")):
        if not file.is_file():
            continue
        name = file.stem
        if not re.fullmatch(configkinds.NAME_PATTERN, name):
            continue
        rel = f"arete/defaults/{kind}s/{file.name}"
        out[name] = _enrich({"kind": kind, "name": name, "path": rel, "engine": str(file)})
    return out


def listing(kind: str, *, name: str | None = None, conn=None) -> list[dict]:
    if kind not in configkinds.REGISTRY:
        raise refusals.NotFound(f"Unknown config kind {kind}")
    if kind == "library":
        return [entry for item_name, entry in sorted(_engine_items(kind).items())
                if name is None or item_name == name]
    root = paths.CONFIG_DIR.resolve()
    found = {}
    for home in configkinds.REGISTRY[kind].home:
        pattern = re.sub(r"\{\w+\}", "*", home.pattern)
        for path in sorted(root.glob(pattern)):
            if path.resolve() != path or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            item_info = ("attention", "defaults") if kind == "attention" else configkinds.classify(relative)
            if item_info is None or item_info[0] != kind:
                continue
            item_name = item_info[1]
            if kind == "step_settings" and relative == "layers/defaults/config.toml":
                item_name = configkinds.DEFAULTS_LAYER_ADDRESS
            if name is not None and item_name != name:
                continue
            if item_name in found:
                first = found[item_name]["path"]
                raise refusals.Conflict(f"Config item {kind}/{item_name} is ambiguous: {first} and {relative}")
            found[item_name] = _enrich({"kind": kind, "name": item_name, "path": relative}, conn)
    for item_name, entry in _engine_items(kind).items():
        if name is not None and item_name != name:
            continue
        found.setdefault(item_name, entry)
    return [found[key] for key in sorted(found)]


def _engine_item(kind: str, name: str) -> dict | None:
    if kind == "step_settings" and name == "defaults":
        target = paths.DEFAULTS_DIR / "steps.toml"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": "arete/defaults/steps.toml", "engine": str(target)}),
                "_engine_file": str(target),
            }
    if kind == "retention" and name == "defaults":
        target = paths.DEFAULTS_DIR / "retention.toml"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": "arete/defaults/retention.toml", "engine": str(target)}),
                "_engine_file": str(target),
            }
    if kind == "loop":
        target = paths.PACKAGE_DIR / "loops" / name / "loop.toml"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": f"arete/loops/{name}/loop.toml", "engine": str(target)}),
                "_engine_file": str(target),
            }
    if kind == "device" and name == "local":
        target = paths.DEFAULTS_DIR / "devices" / "local.toml"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": "arete/defaults/devices/local.toml", "engine": str(target)}),
                "_engine_file": str(target),
            }
    if kind in ("target", "rule") and re.fullmatch(configkinds.NAME_PATTERN, name):
        target = paths.DEFAULTS_DIR / f"{kind}s" / f"{name}.toml"
        if target.is_file():
            return {
                **_enrich({
                    "kind": kind,
                    "name": name,
                    "path": f"arete/defaults/{kind}s/{name}.toml",
                    "engine": str(target),
                }),
                "_engine_file": str(target),
            }
    if kind == "adw" and name != "library" and re.fullmatch(configkinds.NAME_PATTERN, name):
        target = paths.ENGINE_ADWS_DIR / f"{name}.toml"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": f"arete/adws/{name}.toml", "engine": str(target)}),
                "_engine_file": str(target),
            }
    if kind == "routes" and name == "adw":
        target = paths.ENGINE_ROUTES_DIR / "adw.toml"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": "arete/routes/adw.toml", "engine": str(target)}),
                "_engine_file": str(target),
            }
    if kind == "library" and name == "adw":
        target = paths.ENGINE_ADWS_DIR / "library.toml"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": "arete/adws/library.toml", "engine": str(target)}),
                "_engine_file": str(target),
            }
    if kind == "prompt" and re.fullmatch(configkinds.PROMPT_NAME_PATTERN, name):
        target = paths.PROMPTS_DIR / f"{name}.md"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": f"arete/prompts/{name}.md", "engine": str(target)}),
                "_engine_file": str(target),
            }
    if kind == "schema" and re.fullmatch(configkinds.NAME_PATTERN, name):
        target = paths.SCHEMAS_DIR / f"{name}.json"
        if target.is_file():
            return {
                **_enrich({"kind": kind, "name": name, "path": f"arete/schemas/{name}.json", "engine": str(target)}),
                "_engine_file": str(target),
            }
    return None


def item(kind: str, name: str, *, level: str | None = None, conn=None) -> dict:
    if kind not in configkinds.REGISTRY:
        raise refusals.NotFound(f"Unknown config kind {kind}")
    if level == "engine":
        engine = _engine_item(kind, name)
        if engine is not None:
            return engine
        raise refusals.NotFound(f"No engine config item {kind}/{name}")
    if found := listing(kind, name=name, conn=conn):
        entry = found[0]
        if entry["level"] == "engine":
            engine = _engine_item(kind, name)
            if engine is not None:
                entry = engine
        if level is not None and entry["level"] != level:
            raise refusals.NotFound(f"No config item {kind}/{name} at level {level}")
        return entry
    if level is None and kind in ("prompt", "schema", "retention") and (engine := _engine_item(kind, name)) is not None:
        return engine
    raise refusals.NotFound(f"No config item {kind}/{name}")


def item_or_historical(kind: str, name: str, *, level: str | None = None, conn=None) -> dict:
    try:
        return item(kind, name, level=level, conn=conn)
    except refusals.NotFound:
        if kind not in configkinds.REGISTRY or level == "engine":
            raise
    lookup_kind = "step_settings" if kind == "attention" else kind
    row = config_store.latest_item(lookup_kind, name, conn)
    if row is not None:
        item_level = row["origin"] if row["origin"].startswith("layer:") else configrepo.level_of(row["path"])
        if level is not None and item_level != level:
            raise refusals.NotFound(f"No config item {kind}/{name} at level {level}")
        return {
            "kind": kind,
            "name": name,
            "path": row["path"],
            "subject": f"config:{kind}/{name}",
            "version": int(row.get("ver") or 0),
            "content_version": row["version"],
            "level": item_level,
        }
    if configrepo.is_repo():
        from . import drafts

        rel = drafts.default_path(kind, name)
        entries = configrepo.version_history(lookup_kind, name, rel)
        if entries:
            latest = entries[0]
            item_level = latest["origin"] if latest["origin"].startswith("layer:") else configrepo.level_of(rel)
            if level is not None and item_level != level:
                raise refusals.NotFound(f"No config item {kind}/{name} at level {level}")
            return {
                "kind": kind,
                "name": name,
                "path": rel,
                "subject": f"config:{kind}/{name}",
                "version": int(latest["version"]),
                "content_version": latest["content_version"],
                "level": item_level,
            }
    raise refusals.NotFound(f"No config item {kind}/{name}")


def is_tombstone_selector(found: dict, selector: str, conn=None) -> bool:
    lookup_kind = "step_settings" if found["kind"] == "attention" else found["kind"]
    tombstone_cv = f"{lookup_kind}/{found['name']}{config_store.DELETED_SUFFIX}"
    if selector in (tombstone_cv, f"{found['kind']}/{found['name']}{config_store.DELETED_SUFFIX}"):
        return True
    row = config_store.by_selector(lookup_kind, found["name"], selector, conn)
    if row is not None:
        return not row["sha256"] or str(row["version"]).endswith(config_store.DELETED_SUFFIX)
    history = configrepo.version_history(lookup_kind, found["name"], found["path"])
    for entry in history:
        if (
            (selector.isdecimal() and entry["version"] == int(selector))
            or entry["commit_sha"] == selector
            or entry["content_version"] == selector
        ):
            return not entry["sha256"] or entry["content_version"].endswith(config_store.DELETED_SUFFIX)
    return False


def _validate_subfile(file_name: str) -> str:
    if not file_name or "/" in file_name or "\\" in file_name or file_name in (".", "..") or "\0" in file_name:
        raise refusals.Invalid(f"Invalid file selector {file_name!r}")
    return file_name


def _target_path(found: dict, file_name: str | None) -> str:
    if not file_name:
        return found["path"]
    clean = _validate_subfile(file_name)
    parent = Path(found["path"]).parent
    return (parent / clean).as_posix()


def _resolve_selector(found: dict, selector: str, conn=None) -> tuple[str, int | None, str | None] | None:
    """Resolve a version selector (integer ver, content_version, or full git SHA) to (commit_sha, ver, content_version)."""
    if re.fullmatch(REVISION, selector):
        return selector, None, None
    lookup_kind = "step_settings" if found["kind"] == "attention" else found["kind"]
    history = configrepo.version_history(lookup_kind, found["name"], found["path"])
    if selector.isdecimal():
        wanted_ver = int(selector)
        if wanted_ver < 1:
            return None
        for entry in history:
            if entry["version"] == wanted_ver:
                return entry["commit_sha"], entry["version"], entry["content_version"]
        row = config_store.by_selector(lookup_kind, found["name"], selector, conn)
        if row:
            return row["commit_sha"], int(row.get("ver") or 0), row["version"]
        return None
    if CONTENT_VERSION.fullmatch(selector):
        for entry in history:
            if entry["content_version"] == selector or (
                found["kind"] == "attention" and entry["content_version"].endswith(selector.partition("@")[2])
            ):
                return entry["commit_sha"], entry["version"], entry["content_version"]
        row = config_store.by_selector(lookup_kind, found["name"], selector, conn)
        if row:
            return row["commit_sha"], int(row.get("ver") or 0), row["version"]
        return None
    raise refusals.Invalid("Config version selectors must be a positive integer, content_version, or full Git commit ID")


def read(found: dict, *, version: str | None = None, level: str | None = None,
         from_ref: str | None = None, file: str | None = None, conn=None) -> dict:
    public = {k: v for k, v in found.items() if not k.startswith("_")}
    if "_engine_file" in found:
        engine_path = Path(found["_engine_file"])
        if public["kind"] == "library":
            if file:
                raise refusals.NotFound(f"No file {file} for {found['kind']}/{found['name']}")
            from . import adw_library

            try:
                return {**public, "content": adw_library.load(engine_path)}
            except ValueError as error:
                raise refusals.Invalid(str(error)) from error
        if file:
            engine_path = engine_path.parent / _validate_subfile(file)
            if not engine_path.is_file():
                raise refusals.NotFound(f"No file {file} for {found['kind']}/{found['name']}")
        return {**public, "content": engine_path.read_text(encoding="utf-8")}
    rel = _target_path(public, file)
    if version is not None or (from_ref is not None and from_ref != "working"):
        if (location := git_location(public["path"])) is None:
            raise refusals.NotFound(f"No git history for {public['kind']}/{public['name']}")
        root, _ = location
        git_rel = (paths.CONFIG_DIR.resolve() / rel).relative_to(root).as_posix()
        if version is not None:
            resolved = _resolve_selector(public, version, conn=conn)
            if resolved is None:
                raise refusals.NotFound(f"No version {version} for {public['kind']}/{public['name']}")
            commit_sha, matched_ver, matched_cv = resolved
        elif from_ref == "live":
            commit_sha = configrepo.live_sha(root)
            if not commit_sha:
                raise refusals.NotFound(f"No live revision for {public['kind']}/{public['name']}")
            matched_ver, matched_cv = public["version"], public["content_version"]
        else:
            resolved = _resolve_selector(public, from_ref, conn=conn)
            if resolved is None:
                raise refusals.NotFound(f"No revision {from_ref} for {public['kind']}/{public['name']}")
            commit_sha, matched_ver, matched_cv = resolved
        raw_text = revision_text(root, commit_sha, git_rel)
        if raw_text is None:
            raise refusals.NotFound(f"The selected config revision is unavailable for {rel}")
        if public["kind"] == "attention" and not file:
            try:
                content = asdict(config.ask_table(tomllib.loads(raw_text).get("ask", {})))
            except (TypeError, ValueError) as error:
                raise refusals.Invalid(str(error)) from error
        else:
            content = raw_text
        if matched_ver is not None:
            public["version"] = matched_ver
        if matched_cv is not None:
            public["content_version"] = matched_cv
        return {**public, "content": content}
    if public["kind"] == "attention" and not file:
        try:
            content = settings.attention()
        except FileNotFoundError:
            raise refusals.NotFound("The selected config file is unavailable") from None
        except (TypeError, ValueError) as error:
            raise refusals.Invalid(str(error)) from error
    else:
        content = file_text(rel)
    return {**public, "content": content}


def file_text(relative: str) -> str:
    path = paths.CONFIG_DIR.resolve() / relative
    try:
        with paths.directory_fd(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PermissionError("expected an unlinked regular file")
                data = source.read(MAX_CONTENT + 1)
    except OSError:
        raise refusals.NotFound("The selected config file is unavailable") from None
    if len(data) > MAX_CONTENT:
        raise refusals.TooLarge("The selected config file is too large")
    try:
        return data.decode()
    except UnicodeDecodeError:
        raise refusals.Invalid("The selected config file must be UTF-8 text") from None


def git_location(relative: str) -> tuple[Path, str] | None:
    root = paths.CONFIG_DIR.resolve()
    path = root / relative
    if path.parent.resolve() != path.parent:
        return None
    for directory in path.parents:
        if not directory.is_relative_to(root):
            break
        git_dir = directory / ".git"
        if git_dir.is_symlink():
            return None
        if git_dir.is_dir():
            return directory, path.relative_to(directory).as_posix()
        if git_dir.exists():
            return None
    return None


def git(root: Path, *args: str) -> str | None:
    from . import environment

    git_dir = root / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return None
    env = environment.build((), {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_OPTIONAL_LOCKS": "0"})
    try:
        done = subprocess.run(["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
                               f"--git-dir={git_dir}", f"--work-tree={root}", *args], cwd=root, env=env,
                              capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode(errors="replace") if done.returncode == 0 else None


def revisions(found: dict) -> list[str]:
    if (location := git_location(found["path"])) is None:
        return []
    root, path = location
    return (git(root, "log", "-n", "50", "--format=%H", "--", path) or "").splitlines()


def versions(found: dict, conn=None) -> tuple[list[str], list[dict]]:
    location = git_location(found["path"])
    if location is not None and location[0] == paths.CONFIG_DIR.resolve() and configrepo.is_repo():
        lookup_kind = "step_settings" if found["kind"] == "attention" else found["kind"]
        entries = configrepo.version_history(lookup_kind, found["name"], found["path"])
        if entries:
            return [entry["commit_sha"] for entry in entries], entries
    shas = revisions(found)
    return shas, []


def revision_text(root: Path, ref: str, path: str) -> str | None:
    blob = f"{ref}^{{commit}}:{path}"
    size = git(root, "cat-file", "-s", blob)
    if size is None:
        return None
    if int(size) > MAX_CONTENT:
        raise refusals.TooLarge("The selected config revision is too large")
    return git(root, "cat-file", "blob", blob)


def diff(found: dict, first: str | None, last: str | None, conn=None) -> str | None:
    if (location := git_location(found["path"])) is None:
        return None
    root, path = location
    if first is not None:
        resolved = _resolve_selector(found, first, conn=conn)
        if resolved is None:
            raise refusals.NotFound(f"No version {first} for {found['kind']}/{found['name']}")
        start = resolved[0]
    else:
        start = (git(root, "rev-parse", "--verify", "HEAD^{commit}") or "").strip()
    if not start:
        return None
    before = revision_text(root, start, path)
    if before is None:
        return None
    if last is not None:
        resolved_last = _resolve_selector(found, last, conn=conn)
        if resolved_last is None:
            raise refusals.NotFound(f"No version {last} for {found['kind']}/{found['name']}")
        after = revision_text(root, resolved_last[0], path)
    else:
        after = file_text(found["path"])
    if after is None:
        return None
    if found["kind"] == "attention":
        before, after = attention_text(before), attention_text(after)
        path = "attention/defaults"
    return "".join(unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                fromfile=f"a/{path}", tofile=f"b/{path}"))


def attention_text(text: str) -> str:
    try:
        ask = config.ask_table(tomllib.loads(text).get("ask", {}))
    except (TypeError, ValueError) as error:
        raise refusals.Invalid(str(error)) from error
    return json.dumps(asdict(ask), indent=2) + "\n"
