from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tomllib
from dataclasses import asdict
from difflib import unified_diff

from . import config, configkinds, paths, refusals, settings

MAX_CONTENT = 1024 * 1024
REVISION = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"


def listing(kind: str, *, name: str | None = None) -> list[dict]:
    root = paths.CONFIG_DIR.resolve()
    found = {}
    for home in configkinds.REGISTRY[kind].home:
        pattern = re.sub(r"\{\w+\}", "*", home.pattern)
        for path in sorted(root.glob(pattern)):
            if path.resolve() != path or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            item = ("attention", "defaults") if kind == "attention" else configkinds.classify(relative)
            if item is None or item[0] != kind:
                continue
            item_name = item[1]
            if kind == "step_settings" and relative == "layers/defaults/config.toml":
                item_name = configkinds.DEFAULTS_LAYER_ADDRESS
            if name is not None and item_name != name:
                continue
            if item_name in found:
                first = found[item_name]["path"]
                raise refusals.Conflict(f"Config item {kind}/{item_name} is ambiguous: {first} and {relative}")
            found[item_name] = {"kind": kind, "name": item_name, "path": relative}
    return [found[key] for key in sorted(found)]


def item(kind: str, name: str) -> dict:
    if found := listing(kind, name=name):
        return found[0]
    raise refusals.NotFound(f"No config item {kind}/{name}")


def read(found: dict) -> dict:
    if found["kind"] == "attention":
        try:
            content = settings.attention()
        except FileNotFoundError:
            raise refusals.NotFound("The selected config file is unavailable") from None
        except (TypeError, ValueError) as error:
            raise refusals.Invalid(str(error)) from error
    else:
        content = file_text(found["path"])
    return {**found, "content": content}


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


def git(*args: str) -> str | None:
    root = paths.CONFIG_DIR.resolve()
    git_dir = root / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return None
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        done = subprocess.run(["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
                               f"--git-dir={git_dir}", f"--work-tree={root}", *args], cwd=root, env=env,
                              capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode(errors="replace") if done.returncode == 0 else None


def revisions(found: dict) -> list[str]:
    return (git("log", "-n", "50", "--format=%H", "--", found["path"]) or "").splitlines()


def revision_text(ref: str, path: str) -> str | None:
    blob = f"{ref}^{{commit}}:{path}"
    size = git("cat-file", "-s", blob)
    if size is None:
        return None
    if int(size) > MAX_CONTENT:
        raise refusals.TooLarge("The selected config revision is too large")
    return git("cat-file", "blob", blob)


def diff(found: dict, first: str | None, last: str | None) -> str | None:
    if any(ref is not None and not re.fullmatch(REVISION, ref) for ref in (first, last)):
        raise refusals.Invalid("Config revisions must be full Git commit IDs")
    path = found["path"]
    start = first or (git("rev-parse", "--verify", "HEAD^{commit}") or "").strip()
    if not start:
        return None
    before = revision_text(start, path)
    if before is None:
        return None
    after = revision_text(last, path) if last else file_text(path)
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
