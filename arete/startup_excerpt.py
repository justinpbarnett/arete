from __future__ import annotations

import re
import sys

WITHHELD = "[withheld]"
RAW_LINES = 80
KEPT_LINES = 40
LINE_CHARS = 400
TOTAL_CHARS = 4000
EDGE = "()[]{}'\",;:."
WORDS = frozenset(["a", "about", "access", "address", "after", "again", "all", "already", "also", "an", "and", "any", "applied", "are", "arete", "as", "at", "backfill", "backup", "be", "because", "been", "before", "block", "both", "brace", "bracket", "busy", "but", "by", "call", "can", "cannot", "catalog", "changes", "checkout", "class", "close", "closed", "closing", "code", "column", "commit", "commits", "config", "configuration", "connection", "contract", "could", "current", "database", "deploy", "deployed", "denied", "dashboard", "def", "did", "differs", "digest", "directory", "disk", "do", "does", "during", "each", "eof", "else", "error", "errors", "except", "exception", "exist", "exists", "existing", "exit", "exited", "expand", "expansion", "expected", "failed", "failure", "fails", "file", "files", "folder", "for", "found", "from", "full", "guard", "has", "hash", "have", "healthy", "held", "hold", "holds", "if", "import", "imported", "in", "indent", "indentation", "indented", "into", "invalid", "is", "it", "its", "last", "ledger", "line", "load", "loaded", "lock", "locked", "may", "migrate", "migration", "migrations", "mismatch", "missing", "module", "modules", "more", "most", "must", "named", "need", "needs", "network", "never", "new", "no", "none", "not", "of", "old", "on", "only", "open", "opened", "opening", "or", "other", "out", "over", "package", "packages", "parenthesis", "parse", "parsed", "pending", "permission", "phase", "port", "previous", "raise", "read", "ready", "recent", "refuse", "refused", "rejected", "restart", "restarted", "restarts", "restore", "return", "rolled", "rollback", "run", "running", "runs", "schema", "serve", "sha", "sha256", "snapshot", "so", "space", "start", "started", "starting", "startup", "state", "statement", "status", "still", "stop", "stopped", "such", "syntax", "than", "that", "the", "their", "then", "there", "these", "this", "tick", "ticks", "timed", "timeout", "to", "too", "traceback", "tracked", "try", "unable", "under", "unexpected", "unhealthy", "unindent", "unknown", "unmatched", "until", "up", "use", "used", "using", "version", "waiting", "waits", "was", "were", "what", "when", "where", "which", "while", "with", "without", "would", "write", "yet"])
KNOWN_EXCEPTIONS = frozenset({"IncompatibleSchema", "NoDatabase", "Refused", "Down", "Failed", "GhError", "GhUnsure",
                              "UnknownRepo"})
EXCEPTION = re.compile(r"(?:[a-z][a-z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exit|Exception|Warning|Interrupt)")
FRAME = re.compile(r'\s*File "([^"]{1,500})", line ([0-9]{1,6}), in ([A-Za-z_][A-Za-z0-9_]{0,80}|<[a-z]{1,20}>)\s*')
PSEUDO_FILE = re.compile(r"<[a-z][a-z ._]{0,40}>")
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})")
COMMIT = re.compile(r"[0-9a-f]{7,64}")
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def known_module(core: str, tracked: frozenset[str]) -> bool:
    if not MODULE.fullmatch(core):
        return False
    path = core.replace(".", "/")
    return f"{path}.py" in tracked or f"{path}/__init__.py" in tracked or core in sys.stdlib_module_names


def safe(core: str, tracked: frozenset[str], commits: tuple[str, ...], after_line: bool) -> bool:
    return bool(
        core.lower() in WORDS
        or core in KNOWN_EXCEPTIONS
        or EXCEPTION.fullmatch(core)
        or core in tracked
        or after_line and core.isdecimal() and len(core) <= 6
        or COMMIT.fullmatch(core) and any(commit.startswith(core) for commit in commits)
        or known_module(core, tracked))


def chunk_text(chunk: str, tracked: frozenset[str], commits: tuple[str, ...], after_line: bool) -> str:
    core = chunk.strip(EDGE)
    if not core:
        return chunk if len(chunk) <= 3 else WITHHELD
    return chunk if safe(core, tracked, commits, after_line) else WITHHELD


def frame_text(found: re.Match, checkout: str, tracked: frozenset[str]) -> str:
    name = found[1]
    relative = name.removeprefix(checkout.rstrip("/") + "/") if name.startswith(checkout.rstrip("/") + "/") else ""
    shown = name if PSEUDO_FILE.fullmatch(name) else relative if relative in tracked else "[file outside the checkout]"
    return f'  File "{shown}", line {found[2]}, in {found[3]}'


def line_text(line: str, checkout: str, tracked: frozenset[str], commits: tuple[str, ...]) -> str | None:
    line = CONTROL.sub("", line)[:LINE_CHARS].rstrip()
    if not line.strip():
        return None
    if found := FRAME.fullmatch(line):
        return frame_text(found, checkout, tracked)
    if line[0].isspace():
        return None
    shown: list[str] = []
    previous = ""
    for chunk in line.split():
        text = chunk if STAMP.fullmatch(chunk) else chunk_text(chunk, tracked, commits, previous == "line")
        previous = chunk.strip(EDGE).lower()
        if not (text == WITHHELD and shown and shown[-1] == WITHHELD):
            shown.append(text)
    return " ".join(shown)


def redacted(raw: str, checkout: str, tracked: frozenset[str], commits: tuple[str, ...]) -> str:
    lines = [text for line in raw.splitlines()[-RAW_LINES:]
             if (text := line_text(line, checkout, tracked, commits)) is not None]
    joined = "\n".join(lines[-KEPT_LINES:])
    if len(joined) > TOTAL_CHARS:
        joined = joined[-TOTAL_CHARS:].partition("\n")[2]
    return joined.replace("`", "'") or "(no serve.log lines were available)"
