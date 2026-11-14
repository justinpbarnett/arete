from __future__ import annotations

import re
import socket
import tomllib
from urllib.parse import urlsplit

from . import paths, util

SECRETISH = re.compile(r"((?:(?<![A-Za-z])|(?=sk-[a-z-]*[A-Z0-9_]))sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}"
                       r"|[A-Za-z0-9+/]{40,}|/Users/[^/\s]+)")
FENCE = re.compile(r"`{3,}")
TAGS = re.compile(r"<[A-Za-z/!?][^>]{0,200}>")
LINKS = re.compile(r"!?\[([^\]]{0,200})\]\([^)]{0,500}\)")


def redact(text: str) -> str:
    return SECRETISH.sub("[redacted]", text)


def sanitize(text: str, n: int = 200) -> str:
    return util.first_line(redact(text), n)


def plain(text, n: int) -> str:
    text = LINKS.sub(r"\1", TAGS.sub("", str(text or "")))
    return redact(FENCE.sub("'''", text)).strip()[:n]


PRIVATE_ADDRESS = "[private address]"
PRIVATE_SUFFIXES = (".localhost", ".ts.net", ".local", ".internal", ".lan")
ORIGIN = re.compile(r"(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):|(?<![\w/]))//(?:[^\s/@?#]+@)?"
                    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)(?::\d+)?")
BARE_PRIVATE = re.compile(r"(?<![\w.@/-])(?:[\w.+-]+@)?(?:localhost:\d+|fd7a:115c:a1e0:[0-9A-Fa-f:]+"
                          r"|[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.ts\.net(?::\d+)?"
                          r"|\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?)(?![\w-]|\.\w)", re.IGNORECASE)
HOME_PATH = re.compile(r"(?<![\w~])/(?:Users|home)/[^/\s]+")
extra_hosts: set[str] = set()
own_hosts: list[str] | None = None


def normalized_host(host: str) -> str:
    return host.strip().lower().strip("[]")


def keep_private(*hosts: str) -> None:
    extra_hosts.update(h for h in (normalized_host(h) for h in hosts if h) if h and h != "localhost")


def keep_verify_private(url: str) -> None:
    try:
        keep_private(urlsplit(re.sub(r"\{port\d*\}", "0", url)).hostname or "")
    except ValueError:
        pass


def keep_configured_private() -> None:
    try:
        repos = tomllib.loads(paths.CONFIG_FILE.read_text()).get("repo", [])
    except (OSError, ValueError):
        return
    for repo in repos:
        layer = paths.expand(repo["layer"]) if repo.get("layer") else paths.expand(repo["path"]) / ".agents"
        try:
            data = tomllib.loads((layer / "config.toml").read_text())
            keep_verify_private(data.get("verify", {}).get("url", ""))
        except (OSError, ValueError):
            continue


def machine_hosts() -> list[str]:
    global own_hosts
    if own_hosts is None:
        found = []
        try:
            name = socket.gethostname()
            found += [name, name.split(".")[0]]
        except OSError:
            pass
        try:
            data = tomllib.loads((paths.CONFIG_DIR / "devices.toml").read_text())
            found += [str(d.get("host", "")).rpartition("@")[2] for d in (data.get("device") or {}).values()
                      if isinstance(d, dict)]
        except (OSError, ValueError, AttributeError):
            pass
        own_hosts = [normalized_host(h) for h in dict.fromkeys(found) if len(h) >= 4 and not h.isalpha()]
    return own_hosts


def ipv4_octets(host: str) -> list[int] | None:
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() and len(p) <= 3 for p in parts):
        return [int(p) for p in parts]
    return None


def tailnet_ipv4(octets: list[int]) -> bool:
    return octets[0] == 100 and 64 <= octets[1] <= 127


def private_ipv4(octets: list[int]) -> bool:
    a, b = octets[0], octets[1]
    return (a in (10, 127) or octets == [0, 0, 0, 0] or (a, b) in ((192, 168), (169, 254))
            or (a == 172 and 16 <= b <= 31) or tailnet_ipv4(octets))


def private_host(host: str) -> bool:
    host = normalized_host(host)
    if host in extra_hosts or host in machine_hosts():
        return True
    if ":" in host:
        return host == "::1" or host.startswith("fd7a:115c:a1e0:")
    if (octets := ipv4_octets(host)) is not None:
        return private_ipv4(octets)
    return host == "localhost" or host.endswith(PRIVATE_SUFFIXES) or "." not in host


def screen_origin(found: re.Match) -> str:
    return PRIVATE_ADDRESS if private_host(found.group("host")) else found.group(0)


def screen_bare(found: re.Match) -> str:
    token = found.group(0)
    octets = ipv4_octets(token.rpartition("@")[2].partition(":")[0])
    if octets and not (octets[0] == 127 or tailnet_ipv4(octets)):
        return token
    return PRIVATE_ADDRESS


def registered(text: str) -> str:
    names = sorted({*extra_hosts, *machine_hosts()}, key=len, reverse=True)
    names = [variant for name in names for variant in ((f"[{name}]", name) if ":" in name else (name,))]
    if not names:
        return text
    pattern = (r"(?<![\w.@/-])(?:[\w.+-]+@)?(?:" + "|".join(re.escape(n) for n in names)
               + r")(?::\d+)?(?![\w-]|\.\w)")
    return re.sub(re.escape(PRIVATE_ADDRESS) + "|" + pattern, PRIVATE_ADDRESS, text, flags=re.IGNORECASE)


def screen(text: str) -> str:
    keep_configured_private()
    text = ORIGIN.sub(screen_origin, text)
    text = BARE_PRIVATE.sub(screen_bare, text)
    return HOME_PATH.sub("~", registered(text))


FENCE_LINE = re.compile(r"[ \t]*(`{3,}(?=[^`\n]*$)|~{3,})", re.MULTILINE)
FENCE_END = r"[ \t]*{mark}{{{size},}}[ \t]*$"
TICKS = re.compile(r"`+")
REFERENCE = r"(?:https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+|[\w.-]+/[\w.-]+#\d+|#\d+)(?![\w-])"
CLOSING = re.compile(r"(?<![\w-])((?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*)(" + REFERENCE + ")", re.IGNORECASE)
MENTION = re.compile(r"(?<![\w/])@[A-Za-z0-9](?:[\w./-]*\w)?")


def split_fences(text: str) -> list[tuple[bool, str]]:
    parts: list[tuple[bool, str]] = []
    lines = text.splitlines(keepends=True)
    prose: list[str] = []
    i = 0
    while i < len(lines):
        opening = FENCE_LINE.match(lines[i])
        if not opening:
            prose.append(lines[i])
            i += 1
            continue
        if prose:
            parts.append((False, "".join(prose)))
            prose = []
        mark = opening.group(1)
        closing = re.compile(FENCE_END.format(mark=re.escape(mark[0]), size=len(mark)))
        j = i + 1
        while j < len(lines) and not closing.match(lines[j]):
            j += 1
        parts.append((True, "".join(lines[i:j + 1])))
        i = j + 1
    if prose:
        parts.append((False, "".join(prose)))
    return parts


def split_spans(text: str) -> tuple[list[tuple[bool, str]], set[int]]:
    runs = list(TICKS.finditer(text))
    parts: list[tuple[bool, str]] = []
    unmatched: set[int] = set()
    at = 0
    i = 0
    while i < len(runs):
        size = len(runs[i].group())
        j = next((k for k in range(i + 1, len(runs)) if len(runs[k].group()) == size), None)
        if j is None:
            unmatched.add(size)
            i += 1
            continue
        parts.append((False, text[at:runs[i].start()]))
        parts.append((True, text[runs[i].start():runs[j].end()]))
        at = runs[j].end()
        i = j + 1
    parts.append((False, text[at:]))
    return parts, unmatched


def inert_prose(text: str) -> str:
    parts, unmatched = split_spans(text)
    tick = "`" * next(n for n in range(1, len(unmatched) + 2) if n not in unmatched)

    def wrap(found: re.Match) -> str:
        return f"{tick}{found.group()}{tick}"

    def close(found: re.Match) -> str:
        return f"{found.group(1)}{tick}{found.group(2)}{tick}"

    return "".join(part if code else MENTION.sub(wrap, CLOSING.sub(close, part)) for code, part in parts)


def inert(text: str) -> str:
    return "".join(part if code else inert_prose(part) for code, part in split_fences(text))
