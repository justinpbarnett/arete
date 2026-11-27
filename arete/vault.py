from __future__ import annotations

import json

from . import config, secret_move, secretstore

SHORTEST, LONGEST = 8, 4096
REDACTED = "[redacted]"
SCAN = ("--no-ext-diff", "--no-textconv", "--no-color", "--text")


def item_value(row):
    if row["status"] == "set":
        value = secretstore.read(row["scope"], row["name"])
        if value is not None:
            return value.decode()
    return ""


def read(repo: str) -> dict[str, str]:
    scope = f"repo:{repo}"
    legacy = secret_move.fallback(secret_move.repo_file(repo), repo=True)
    found = secret_move.read_repo(legacy) if legacy else {}
    for row in secretstore.rows():
        if row["scope"] == scope:
            name = row["name"]
            found.pop(name, None)
            if value := item_value(row):
                found[name] = value
    return found


def legacy_every() -> list[tuple[str, str]]:
    if secret_move.retired() and config.secret_settings().store:
        return []
    return [(name, value) for file in secret_move.repo_files() if secret_move.fallback(file, repo=True)
            for name, value in secret_move.read_repo(file).items() if value]


def every() -> list[tuple[str, str]]:
    stored = [(row["name"], value) for row in secretstore.rows() if row["scope"].startswith("repo:")
              and (value := item_value(row))]
    return list(dict.fromkeys([*stored, *legacy_every()]))


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


def git_unquoted(text: str) -> str:
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
    return git_unquoted(text.rstrip("\t")).removeprefix("b/")


def held(patch: str) -> dict[str, list[str]]:
    stored = every()
    if not stored or not patch:
        return {}
    found: dict[str, list[str]] = {}
    path, header = "", False
    for line in patch.split("\n"):
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
