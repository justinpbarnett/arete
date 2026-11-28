from __future__ import annotations

import atexit
import contextlib
import itertools
import json
import os
import re
import sqlite3
import sys
import threading
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from . import config, events, paths, principals, secret_move, secretstore, util, vault
from .store import credentials, db

PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)
PRIVATE_KEY_BRANCH = r"(?s:-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----)"
TOKEN_PATTERN = re.compile(r"\bar_[a-z0-9]+_([0-9a-f]{8})_[A-Za-z0-9_-]+")
TOKEN_BRANCH = r"(?P<token>\bar_[a-z0-9]+_(?P<token_id>[0-9a-f]{8})_[A-Za-z0-9_-]+)"
LARGEST_SECRET_FILE = 64 * 1024
INI_SECTION = re.compile(r"\[{1,2}[\w .:/\"-]*\]{1,2}")
NAMED_VALUE = re.compile(r"""["']?([\w.-]+)["']?\s*:[ \t]*("(?:[^"\\]|\\.)*"|'[^']*'"""
                         r"""|[^\s,'"{}\[\]#][^\s,{}\[\]]*(?:[ \t]+[^\s#,{}\[\]][^\s,{}\[\]]*)*)""")
NETRC_FILES = (".netrc", "_netrc")
NETRC_SECRET_WORDS = ("login", "password", "account")
PGPASS_FILE = ".pgpass"
PGPASS_FIELDS = 5

_state = threading.local()
_cache_lock = threading.Lock()
_generation = 0
_process_extra: set[str] = set()
_cached: tuple | None = None


def busy() -> bool:
    return getattr(_state, "depth", 0) > 0


@contextlib.contextmanager
def _guard() -> Iterator[None]:
    depth = getattr(_state, "depth", 0)
    _state.depth = depth + 1
    try:
        yield
    finally:
        _state.depth = depth


def invalidate() -> None:
    global _generation, _cached
    with _cache_lock:
        _generation += 1
        _cached = None


def remember(*values: str) -> None:
    global _generation, _cached
    added = {v for v in values if isinstance(v, str) and len(v) >= vault.SHORTEST}
    if not added:
        return
    with _cache_lock:
        if not added <= _process_extra:
            _process_extra.update(added)
            _generation += 1
            _cached = None


def _forms(value: str, escape: bool) -> set[str]:
    if not escape:
        return {value}
    return {value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value, ensure_ascii=True)[1:-1]}


def _compile_pattern(literals: Iterable[str]) -> re.Pattern:
    ordered = sorted(set(literals), key=len, reverse=True)
    parts = [TOKEN_BRANCH, *(re.escape(v) for v in ordered), PRIVATE_KEY_BRANCH]
    return re.compile("|".join(parts))


class Redactions:
    def __init__(self, values: Iterable[str] = (), escape: bool = True):
        self.escape = escape
        self.labelled: dict[str, tuple[str, dict]] = {}
        self.unlabelled: set[str] = set()
        self.held: set[str] = set()
        self.values: tuple[str, ...] = ()
        self._version = 0
        self._compiled_version = -1
        self._pattern: re.Pattern | None = None
        self._merged_key: tuple | None = None
        self._merged_redactions: Redactions | None = None
        self.add(values)

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def add_labelled(self, row: dict, value: str) -> None:
        if not isinstance(value, str) or len(value) < vault.SHORTEST:
            return
        marker = f"[{secretstore.address(row['scope'], row['name'])}]"
        changed = False
        for form in _forms(value, self.escape):
            if len(form) < vault.SHORTEST:
                continue
            existing = self.labelled.get(form)
            if existing is None or existing[0] != marker or existing[1].get("id") != row.get("id"):
                self.labelled[form] = (marker, row)
                self.held.add(form)
                self.unlabelled.discard(form)
                changed = True
        if changed:
            self._version += 1
            self.values = tuple(sorted(self.held, key=len, reverse=True))

    def add(self, values: Iterable[str]) -> None:
        raw = {v for v in values if isinstance(v, str) and len(v) >= vault.SHORTEST}
        if not raw:
            return
        new: set[str] = set()
        for v in raw:
            for form in _forms(v, self.escape):
                if len(form) >= vault.SHORTEST and form not in self.held:
                    new.add(form)
        if new:
            self.unlabelled |= new
            self.held |= new
            self._version += 1
            self.values = tuple(sorted(self.held, key=len, reverse=True))

    def pattern(self) -> re.Pattern:
        if self._pattern is None or self._compiled_version != self._version:
            self._pattern = _compile_pattern(self.held)
            self._compiled_version = self._version
        return self._pattern

    def merged_with(self, base: Redactions) -> Redactions:
        if not self.held:
            return base
        key = (id(base), base._version, self._version)
        if self._merged_key == key and self._merged_redactions is not None:
            return self._merged_redactions
        combined = Redactions(escape=False)
        combined.labelled = {**self.labelled, **base.labelled}
        combined.unlabelled = (base.unlabelled | self.unlabelled) - combined.labelled.keys()
        combined.held = combined.labelled.keys() | combined.unlabelled
        combined.values = tuple(sorted(combined.held, key=len, reverse=True))
        combined._version = 1
        self._merged_key = key
        self._merged_redactions = combined
        return combined

    def apply(self, text: str) -> tuple[str, dict[int, tuple[dict, int]], Counter[str]]:
        secret_hits: dict[int, tuple[dict, int]] = {}
        token_hits: Counter[str] = Counter()
        labelled = self.labelled

        def replace(match: re.Match) -> str:
            if token_id := match.group("token_id"):
                token_hits[token_id] += 1
                return f"[token:{token_id}]"
            matched = match.group(0)
            if info := labelled.get(matched):
                marker, row = info
                sid = row["id"]
                _, count = secret_hits.get(sid, (row, 0))
                secret_hits[sid] = (row, count + 1)
                return marker
            return vault.REDACTED

        return self.pattern().sub(replace, text), secret_hits, token_hits


def _stat_sig(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _db_sig() -> tuple:
    p = paths.DB_FILE
    return (_stat_sig(p), _stat_sig(p.with_name(p.name + "-wal")))


def _env_sig() -> frozenset[tuple[str, str]]:
    return frozenset((k, v) for k, v in os.environ.items()
                     if paths.CREDENTIAL_NAME.search(k) and len(v) >= vault.SHORTEST)


def _read_lifecycle_id() -> int:
    if not paths.DB_FILE.is_file() or db.isolated_busy():
        return -1
    try:
        with _guard(), db.reader() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='secret_events'").fetchone():
                return 0
            return credentials.latest_lifecycle_id(c)
    except (OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema, RuntimeError):
        return -1


def _load_fresh(lifecycle_id: int) -> Redactions:
    redactions = Redactions(escape=True)
    with _guard():
        if secretstore.configured_registry() and not db.isolated_busy():
            with contextlib.suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema,
                                     secretstore.StoreError, secretstore.StoreMissing, RuntimeError, ValueError):
                for row, value in secretstore.entries():
                    redactions.add_labelled(row, value)
        extra = {v for k, v in os.environ.items() if paths.CREDENTIAL_NAME.search(k)}
        extra.add(util.github_token())
        extra.update(value for _, value in vault.legacy_every())
        if not secret_move.retired():
            if pi_file := secret_move.fallback(paths.PI_KEYS_FILE):
                extra |= file_secrets(pi_file, every_value=True)
            with contextlib.suppress(OSError, ValueError):
                jira_file = secret_move.fallback(secret_move.configured_path(config.read_data(), "jira"))
                if jira_file:
                    extra |= file_secrets(jira_file, ignore_assignments=secret_move.JIRA_FIELDS)
                    extra.add(util.read_env_file(jira_file).get("JIRA_API_TOKEN", ""))
        with _cache_lock:
            extra |= _process_extra
        redactions.add(extra)
        redactions.pattern()
    return redactions


def load() -> Redactions:
    global _cached
    db_path = str(paths.DB_FILE)
    config_path = str(paths.CONFIG_FILE)
    db_sig = _db_sig()
    env_sig = _env_sig()
    with _cache_lock:
        gen = _generation
        extra_sig = frozenset(_process_extra)
        cached = _cached
    static_key = (db_path, config_path, env_sig, gen, extra_sig)
    if cached is not None and cached[0] == static_key and cached[1] == db_sig:
        return cached[3]
    lifecycle_id = _read_lifecycle_id()
    if cached is not None and cached[0] == static_key and lifecycle_id >= 0 and cached[2] == lifecycle_id:
        with _cache_lock:
            if _generation == gen:
                _cached = (static_key, db_sig, lifecycle_id, cached[3])
        return cached[3]
    fresh = _load_fresh(lifecycle_id)
    with _cache_lock:
        if _generation == gen:
            _cached = (static_key, _db_sig(), lifecycle_id, fresh)
    return fresh


def file_values(*files: Path, given: Iterable[str] = ()) -> set[str]:
    found = {v for v in given if isinstance(v, str) and len(v) >= vault.SHORTEST}
    skipped = secret_move.external_files()
    if not secret_move.retired():
        with contextlib.suppress(OSError, ValueError):
            jira_file = secret_move.fallback(secret_move.configured_path(config.read_data(), "jira"))
            if jira_file:
                skipped.add(jira_file.resolve())
    for f in files:
        if not skipped or f.resolve() not in skipped:
            found |= file_secrets(f)
    return {v for v in found if len(v) >= vault.SHORTEST}


def secrets(*files: Path, given: Iterable[str] = ()) -> tuple[str, ...]:
    base = load()
    extra = file_values(*files, given=given)
    if not extra:
        return base.values
    merged = Redactions(extra, escape=True).merged_with(base)
    return merged.values


def _resolve_redactions(extra: Iterable[str] | Redactions = ()) -> Redactions:
    base = load()
    if not extra:
        return base
    if isinstance(extra, Redactions):
        return extra.merged_with(base)
    additions = [v for v in extra if isinstance(v, str) and len(v) >= vault.SHORTEST
                 and v not in base.held]
    if not additions:
        return base
    return Redactions(additions, escape=True).merged_with(base)


def _record_hits(channel: str, subject: str, secret_hits: dict[int, tuple[dict, int]],
                 token_hits: Counter[str], redactions: Redactions) -> None:
    if (not secret_hits and not token_hits) or busy() or not paths.DB_FILE.is_file():
        return
    actor = principals.current()
    safe_subject = redactions.apply(str(subject or ""))[0][:200] if subject else ""
    at = util.iso()
    secret_items = list(secret_hits.values())
    token_items = list(token_hits.items())

    def write() -> None:
        if busy():
            return
        with _guard(), contextlib.suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema, RuntimeError):
            with db.tx() as c:
                for row, count in secret_items:
                    credentials.record_found_in_output(c, row, actor, channel, safe_subject, count, at)
                for token_id, count in token_items:
                    with principals.acting(actor):
                        events.emit("token.found_in_output", f"token:{token_id}", cls="durable",
                                    data={"token": token_id, "channel": channel, "subject": safe_subject,
                                          "count": count}, connection=c)

    if db.transaction_mode() is not None:
        db.after_transaction(write)
    elif not db.isolated_busy():
        write()


def _apply_json_safe(s: str, redactions: Redactions) -> tuple[str, dict[int, tuple[dict, int]], Counter[str]]:
    plain, secret_hits, token_hits = redactions.apply(s)
    if plain == s or parses(plain) or not parses(s):
        return plain, secret_hits, token_hits
    secret_hits.clear()
    token_hits.clear()

    def walk(item):
        if isinstance(item, str):
            cleaned, sh, th = redactions.apply(item)
            for sid, (row, count) in sh.items():
                _, prev = secret_hits.get(sid, (row, 0))
                secret_hits[sid] = (row, prev + count)
            token_hits.update(th)
            return cleaned
        if isinstance(item, list):
            return [walk(x) for x in item]
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        return item

    return json.dumps(walk(json.loads(s)), ensure_ascii=False), secret_hits, token_hits


def text(s: str, channel: str, subject: str = "", extra: Iterable[str] | Redactions = ()) -> str:
    if not isinstance(s, str) or not s:
        return s
    redactions = _resolve_redactions(extra)
    cleaned, secret_hits, token_hits = _apply_json_safe(s, redactions)
    if secret_hits or token_hits:
        _record_hits(channel, subject, secret_hits, token_hits, redactions)
    return cleaned


def value(v, channel: str, subject: str = "", extra: Iterable[str] | Redactions = ()):
    if isinstance(v, str):
        return text(v, channel, subject, extra=extra)
    if not isinstance(v, (list, dict)):
        return v
    redactions = _resolve_redactions(extra)
    total_secrets: dict[int, tuple[dict, int]] = {}
    total_tokens: Counter[str] = Counter()

    def walk(item):
        if isinstance(item, str):
            cleaned, sh, th = _apply_json_safe(item, redactions)
            for sid, (row, count) in sh.items():
                _, prev = total_secrets.get(sid, (row, 0))
                total_secrets[sid] = (row, prev + count)
            total_tokens.update(th)
            return cleaned
        if isinstance(item, list):
            return [walk(x) for x in item]
        if isinstance(item, dict):
            return {k: walk(val) for k, val in item.items()}
        return item

    cleaned = walk(v)
    if total_secrets or total_tokens:
        _record_hits(channel, subject, total_secrets, total_tokens, redactions)
    return cleaned


class LineScrubber:
    def __init__(self, stream, channel: str = "log", subject: str = "cli"):
        self.stream = stream
        self.channel = channel
        self.subject = subject
        self._buf = ""
        self._lock = threading.Lock()
        self._writing = threading.local()

    @property
    def encoding(self):
        return getattr(self.stream, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self.stream, "errors", "replace")

    def isatty(self) -> bool:
        return self.stream.isatty()

    def fileno(self) -> int:
        return self.stream.fileno()

    def _emit(self, chunk: str) -> None:
        if not chunk:
            return
        if getattr(self._writing, "active", False) or busy():
            self.stream.write(chunk)
            return
        self._writing.active = True
        try:
            self.stream.write(text(chunk, self.channel, self.subject))
        finally:
            self._writing.active = False

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            self._buf += s
            if "\n" in self._buf:
                complete, self._buf = self._buf.rsplit("\n", 1)
                self._emit(complete + "\n")
        return len(s)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                pending, self._buf = self._buf, ""
                self._emit(pending)
            self.stream.flush()

    def __getattr__(self, name: str):
        return getattr(self.stream, name)


def raw_stream(stream):
    return stream.stream if isinstance(stream, LineScrubber) else stream


def wrap_stdio(subject: str = "cli") -> None:
    if not isinstance(sys.stdout, LineScrubber):
        out = LineScrubber(sys.stdout, "log", subject)
        sys.stdout = out
        atexit.register(out.flush)
    else:
        sys.stdout.subject = subject
    if not isinstance(sys.stderr, LineScrubber):
        err = LineScrubber(sys.stderr, "log", subject)
        sys.stderr = err
        atexit.register(err.flush)
    else:
        sys.stderr.subject = subject


def file_secrets(path: Path, every_value: bool = False, *, ignore_assignments: Iterable[str] = ()) -> set[str]:
    try:
        if path.stat().st_size > LARGEST_SECRET_FILE:
            return set()
        content = paths.read_confined(path).decode(errors="replace")
    except OSError:
        return set()
    found = set(PRIVATE_KEY.findall(content))
    lines = [line.strip() for line in content.splitlines()]
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
        pairs = [(name, unquoted(val)) for name, val in NAMED_VALUE.findall(line)]
        found |= {val for name, val in pairs if paths.CREDENTIAL_NAME.search(name) or looks_secret(val)}
        if not sep and not pairs:
            if every_value:
                found.add(line.strip("'\","))
            word = re.split(r"[\s:]+", line)[-1].strip("'\",")
            if every_value or looks_secret(word):
                found.add(word)
    return found


def netrc_secrets(lines: list[str]) -> set[str]:
    words = " ".join(lines).split()
    return {unquoted(val) for name, val in itertools.pairwise(words) if name in NETRC_SECRET_WORDS}


def pgpass_fields(line: str) -> list[str]:
    return [field.replace("\\:", ":").replace("\\\\", "\\") for field in re.split(r"(?<!\\):", line)]


def unquoted(val: str) -> str:
    val = val.strip().rstrip(",").strip()
    if len(val) >= 2 and val[0] == val[-1] == '"':
        with contextlib.suppress(json.JSONDecodeError):
            return str(json.loads(val))
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
        return val[1:-1]
    return val


def looks_secret(val: str) -> bool:
    return len(val) >= vault.SHORTEST and not val.isalpha()


def scrub(s: str, hidden: Sequence[str] | Redactions) -> str:
    if isinstance(hidden, Redactions):
        return _apply_json_safe(s, hidden)[0]
    plain = replaced(s, hidden)
    if plain == s or parses(plain) or not parses(s):
        return plain
    return json.dumps(scrub_value(json.loads(s), hidden), ensure_ascii=False)


def replaced(s: str, hidden: Sequence[str] | Redactions) -> str:
    if isinstance(hidden, Redactions):
        return hidden.apply(s)[0]
    if not hidden:
        return PRIVATE_KEY.sub(vault.REDACTED, TOKEN_PATTERN.sub(lambda m: f"[token:{m.group(1)}]", s))
    pattern = _compile_pattern(hidden)
    return pattern.sub(lambda m: f"[token:{m.group('token_id')}]" if m.group("token_id") else vault.REDACTED, s)


def parses(s: str) -> bool:
    try:
        json.loads(s)
    except ValueError:
        return False
    return True


def scrub_value(val, hidden: Sequence[str] | Redactions):
    if isinstance(val, str):
        return replaced(val, hidden)
    if isinstance(val, list):
        return [scrub_value(item, hidden) for item in val]
    if isinstance(val, dict):
        return {key: scrub_value(item, hidden) for key, item in val.items()}
    return val
