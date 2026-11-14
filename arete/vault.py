from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from . import paths, util

FOLDER = paths.CONFIG_DIR / "secrets"
SUFFIX = ".secrets"
SHORTEST, LONGEST = 8, 4096
REDACTED = "[redacted]"
LOCK = "secret-store"


def file_of(repo: str) -> Path:
    return FOLDER / (quote(repo, safe="") + SUFFIX)


def load(path: Path) -> dict[str, str]:
    try:
        found = json.loads(paths.read_confined(path))
    except (OSError, ValueError):
        return {}
    if not isinstance(found, dict):
        return {}
    return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}


def read(repo: str) -> dict[str, str]:
    return load(file_of(repo))


def save(repo: str, found: dict[str, str]) -> None:
    target = file_of(repo)
    if not found:
        target.unlink(missing_ok=True)
        return
    FOLDER.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=FOLDER, prefix=".", suffix=SUFFIX)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(found))
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def put(repo: str, name: str, value: str) -> None:
    with util.lock(LOCK, wait=True):
        save(repo, {**read(repo), name: value})


def remove(repo: str, name: str) -> bool:
    with util.lock(LOCK, wait=True):
        found = read(repo)
        if name not in found:
            return False
        del found[name]
        save(repo, found)
        return True


def every() -> list[tuple[str, str]]:
    try:
        files = sorted(FOLDER.glob("*" + SUFFIX))
    except OSError:
        return []
    return [(name, value) for f in files for name, value in load(f).items() if value]


def values() -> set[str]:
    return {value for _, value in every()}


def unfit(value) -> str:
    if (isinstance(value, str) and SHORTEST <= len(value) <= LONGEST and "\n" not in value and "\r" not in value
            and value.isprintable()):
        return ""
    return f"the value must be one line of {SHORTEST} to {LONGEST} printable characters"


def forms(value: str) -> tuple[str, str]:
    return value, json.dumps(value)[1:-1]


def held(patch: str) -> dict[str, list[str]]:
    stored = every()
    if not stored or not patch:
        return {}
    found: dict[str, list[str]] = {}
    path, header = "", False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            header, path = True, ""
        elif header and line.startswith("@@"):
            header = False
        elif header and line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif not header and line.startswith("+"):
            for name, value in stored:
                if any(form in line for form in forms(value)) and name not in found.setdefault(path, []):
                    found[path].append(name)
    return found


def refusals(patch: str) -> list[str]:
    return [f"{path} (holds the stored value of {', '.join(names)})" for path, names in held(patch).items()]


def hidden(text: str) -> str:
    for value in sorted(values(), key=len, reverse=True):
        for form in forms(value):
            text = text.replace(form, REDACTED)
    return text
