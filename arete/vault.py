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


def put_all(repo: str, given: dict[str, str]) -> None:
    if not given:
        return
    with util.lock(LOCK, wait=True):
        save(repo, {**read(repo), **given})


def put(repo: str, name: str, value: str) -> None:
    put_all(repo, {name: value})


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


def forms(value: str) -> tuple[str, ...]:
    return value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]


def holding(text: str, value: str) -> bool:
    return any(form in text for form in forms(value))


ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def unquoted(text: str) -> str:
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body, out, i = text[1:-1], bytearray(), 0
    while i < len(body):
        if body[i] != "\\" or i + 1 >= len(body):
            out += body[i].encode()
            i += 1
        elif body[i + 1] in "01234567":
            out.append(int(body[i + 1:i + 4], 8) & 0xFF)
            i += 4
        else:
            out += ESCAPES.get(body[i + 1], body[i + 1]).encode()
            i += 2
    return out.decode(errors="replace")


def patch_path(text: str) -> str:
    return unquoted(text.rstrip("\t")).removeprefix("b/")


def held(patch: str) -> dict[str, list[str]]:
    stored = every()
    if not stored or not patch:
        return {}
    found: dict[str, list[str]] = {}
    path, header = "", False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            header = True
            tail = line.rpartition(' "b/')
            path = patch_path('"b/' + tail[2]) if tail[1] else line.rpartition(" b/")[2]
        elif header and line.startswith("@@"):
            header = False
        elif header and line.startswith("+++ ") and line != "+++ /dev/null":
            path = patch_path(line[4:])
        elif not header and not line.startswith("+"):
            continue
        texts = (line, path) if header else (line,)
        for name, value in stored:
            if any(holding(t, value) for t in texts) and name not in found.setdefault(path, []):
                found[path].append(name)
    return found


def refusals(patch: str) -> list[str]:
    return [f"{hidden(path)} (holds the stored value of {', '.join(names)})" for path, names in held(patch).items()]


def hidden(text: str, given: set[str] | None = None) -> str:
    for value in sorted(values() if given is None else given, key=len, reverse=True):
        for form in forms(value):
            text = text.replace(form, REDACTED)
    return text
