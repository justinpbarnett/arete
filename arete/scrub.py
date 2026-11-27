from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from . import config, paths, secret_move, secretstore, util, vault

PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)
LARGEST_SECRET_FILE = 64 * 1024
INI_SECTION = re.compile(r"\[{1,2}[\w .:/\"-]*\]{1,2}")
NAMED_VALUE = re.compile(r"""["']?([\w.-]+)["']?\s*:[ \t]*("(?:[^"\\]|\\.)*"|'[^']*'"""
                         r"""|[^\s,'"{}\[\]#][^\s,{}\[\]]*(?:[ \t]+[^\s#,{}\[\]][^\s,{}\[\]]*)*)""")
NETRC_FILES = (".netrc", "_netrc")
NETRC_SECRET_WORDS = ("login", "password", "account")
PGPASS_FILE = ".pgpass"
PGPASS_FIELDS = 5


class Redactions:
    def __init__(self, values: Iterable[str] = (), escape: bool = True):
        self.escape = escape
        self.values: tuple[str, ...] = ()
        self.held: set[str] = set()
        self.add(values)

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def add(self, values: Iterable[str]) -> None:
        new = {v for v in values if len(v) >= vault.SHORTEST and v not in self.held}
        if self.escape:
            new |= {json.dumps(v, ensure_ascii=False)[1:-1] for v in new}
        new -= self.held
        if new:
            self.held |= new
            self.values = tuple(sorted(self.held, key=len, reverse=True))


def secrets(*files: Path, given: Iterable[str] = ()) -> tuple[str, ...]:
    found = {v for k, v in os.environ.items() if paths.CREDENTIAL_NAME.search(k)}
    found |= {util.github_token(), *secretstore.values(), *(value for _, value in vault.legacy_every()), *given}
    jira_file = None
    if not secret_move.retired():
        if pi_file := secret_move.fallback(paths.PI_KEYS_FILE):
            found |= file_secrets(pi_file, every_value=True)
        with contextlib.suppress(OSError, ValueError):
            jira_file = secret_move.fallback(secret_move.configured_path(config.read_data(), "jira"))
            if jira_file:
                found |= file_secrets(jira_file, ignore_assignments=secret_move.JIRA_FIELDS)
                found.add(util.read_env_file(jira_file).get("JIRA_API_TOKEN", ""))
    skipped = secret_move.external_files()
    if jira_file:
        skipped.add(jira_file.resolve())
    for f in files:
        if not skipped or f.resolve() not in skipped:
            found |= file_secrets(f)
    found = {v for v in found if len(v) >= vault.SHORTEST}
    as_json = {json.dumps(v, ensure_ascii=ascii_only)[1:-1] for v in found for ascii_only in (False, True)}
    return tuple(sorted(found | as_json, key=len, reverse=True))


def file_secrets(path: Path, every_value: bool = False, *, ignore_assignments: Iterable[str] = ()) -> set[str]:
    try:
        if path.stat().st_size > LARGEST_SECRET_FILE:
            return set()
        text = paths.read_confined(path).decode(errors="replace")
    except OSError:
        return set()
    found = set(PRIVATE_KEY.findall(text))
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#") and not INI_SECTION.fullmatch(line)]
    if path.name in NETRC_FILES:
        return found | netrc_secrets(lines)
    if path.name == PGPASS_FILE:
        return found | {fields[-1] for line in lines if len(fields := pgpass_fields(line)) == PGPASS_FIELDS}
    for line in lines:
        key, sep, raw = line.removeprefix("export ").partition("=")
        if sep and key.strip() in ignore_assignments:
            continue
        if sep and (every_value or paths.CREDENTIAL_NAME.search(key) or looks_secret(util.env_value(raw))):
            found.add(util.env_value(raw))
        pairs = [(name, unquoted(value)) for name, value in NAMED_VALUE.findall(line)]
        found |= {value for name, value in pairs if paths.CREDENTIAL_NAME.search(name) or looks_secret(value)}
        if not sep and not pairs:
            if every_value:
                found.add(line.strip("'\","))
            word = re.split(r"[\s:]+", line)[-1].strip("'\",")
            if every_value or looks_secret(word):
                found.add(word)
    return found


def netrc_secrets(lines: list[str]) -> set[str]:
    words = " ".join(lines).split()
    return {unquoted(value) for name, value in itertools.pairwise(words) if name in NETRC_SECRET_WORDS}


def pgpass_fields(line: str) -> list[str]:
    return [field.replace("\\:", ":").replace("\\\\", "\\") for field in re.split(r"(?<!\\):", line)]


def unquoted(value: str) -> str:
    value = value.strip().rstrip(",").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        with contextlib.suppress(json.JSONDecodeError):
            return str(json.loads(value))
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def looks_secret(value: str) -> bool:
    return len(value) >= vault.SHORTEST and not value.isalpha()


def scrub(text: str, hidden: Sequence[str]) -> str:
    plain = replaced(text, hidden)
    if plain == text or parses(plain) or not parses(text):
        return plain
    return json.dumps(scrub_value(json.loads(text), hidden), ensure_ascii=False)


def replaced(text: str, hidden: Sequence[str]) -> str:
    for value in hidden:
        text = text.replace(value, vault.REDACTED)
    return PRIVATE_KEY.sub(vault.REDACTED, text)


def parses(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def scrub_value(value, hidden: Sequence[str]):
    if isinstance(value, str):
        return replaced(value, hidden)
    if isinstance(value, list):
        return [scrub_value(item, hidden) for item in value]
    if isinstance(value, dict):
        return {key: scrub_value(item, hidden) for key, item in value.items()}
    return value
