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


FENCE_OPEN = re.compile(r"( {0,3})(`{3,}(?=[^`\n]*$)|~{3,})")
CONTAINER = r"[ \t>]*(?:(?:[-*+]|\d+[.)])[ \t>]+)*"
HTML_START = re.compile(CONTAINER + "<")
RAW_START = re.compile(CONTAINER + r"<(?:(?P<tag>(?:pre|script|style|textarea)(?![\w-]))|(?P<comment>!--)"
                       r"|(?P<pi>\?)|(?P<cdata>!\[CDATA\[)|(?P<decl>![A-Za-z]))", re.IGNORECASE)
RAW_END = {"tag": re.compile(r"</(?:pre|script|style|textarea)>", re.IGNORECASE), "comment": re.compile("-->"),
           "pi": re.compile(r"\?>"), "cdata": re.compile(r"\]\]>"), "decl": re.compile(">")}
TICKS = re.compile(r"`+")
LINK_DESTINATION = re.compile(r"\]\([^)]*$|(?:^|\n) {0,3}\[[^\]\n]*\]:[^\n]*$")
MENTION = r"@[A-Za-z0-9](?:[\w./-]*\w)?"
REFERENCE = (r"(?:https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+|[\w.-]+/[\w.-]+#\d+"
             r"|GH-\d+|#\d+)(?![\w-])")
OWN = re.compile(r"(?<!\\)<code>(?:" + MENTION + "|" + REFERENCE + r")</code>", re.IGNORECASE)
CLOSING = re.compile(r"(?P<closing>(?<![A-Za-z0-9_])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?!\w)[\s:*_~\\]*)"
                     r"(?P<reference>" + REFERENCE + ")", re.IGNORECASE)
URL = r"(?P<url>(?<![A-Za-z])https?://[A-Za-z0-9][A-Za-z0-9.-]*(?![\w.-])[^\s<>|]*)"
MENTIONED = r"(?P<mention>(?<![A-Za-z0-9])" + MENTION + ")"
MENTIONS_AND_URLS = re.compile(URL + "|" + MENTIONED, re.IGNORECASE)
MENTIONS = re.compile(MENTIONED, re.IGNORECASE)


def between_own(text: str, rewrite) -> str:
    parts = []
    at = 0
    for own in OWN.finditer(text):
        parts.append(rewrite(text[at:own.start()]))
        parts.append(own.group())
        at = own.end()
    parts.append(rewrite(text[at:]))
    return "".join(parts)


def close_reference(found: re.Match) -> str:
    closing = found.group("closing")
    gap = " " if closing.endswith("\\") else ""
    return f"{closing}{gap}<code>{found.group('reference')}</code>"


def inert_prose(text: str, urls: bool = True) -> str:
    pattern = MENTIONS_AND_URLS if urls else MENTIONS

    def mention(found: re.Match) -> str:
        if found.groupdict().get("url"):
            return found.group()
        gap = " " if found.string[found.start() - 1:found.start()] == "\\" else ""
        return f"{gap}<code>{found.group()}</code>"

    text = between_own(text, lambda gap: CLOSING.sub(close_reference, gap))
    return between_own(text, lambda gap: pattern.sub(mention, gap))


def span_end(runs: list[re.Match], i: int, size: int) -> int | None:
    return next((k for k in range(i + 1, len(runs)) if runs[k].end() - runs[k].start() == size), None)


def inert_markdown(text: str) -> str:
    runs = list(TICKS.finditer(text))
    out = []
    at = 0
    i = 0
    while i < len(runs):
        start, end = runs[i].span()
        before = text[:start]
        if (len(before) - len(before.rstrip("\\"))) % 2:
            start += 1
        closer = span_end(runs, i, end - start) if end > start else None
        if closer is None:
            i += 1
            continue
        span = text[start:runs[closer].end()]
        since = OWN.sub("", text[at:start])
        if "\n" in span or "|" in span or "<" in since or LINK_DESTINATION.search(since):
            break
        out.append(inert_prose(text[at:start]))
        out.append(span)
        at = runs[closer].end()
        i = closer + 1
    out.append(inert_prose(text[at:]))
    return "".join(out)


def fence_end(lines: list[str], i: int) -> int | None:
    opening = FENCE_OPEN.match(lines[i])
    if not opening:
        return None
    indent = len(opening.group(1))
    mark = opening.group(2)
    closing = re.compile(rf" {{0,3}}{re.escape(mark[0])}{{{len(mark)},}}[ \t]*$")
    j = i + 1
    while j < len(lines) and not closing.match(lines[j].rstrip("\r\n")):
        j += 1
    if indent == 0:
        return min(j, len(lines) - 1)
    if j >= len(lines):
        return None
    margin = " " * indent
    return j if all(line.startswith(margin) for line in lines[i:j + 1] if line.strip()) else None


def inert(text: str) -> str:
    out: list[str] = []
    block: list[str] = []
    html = False
    raw = None

    def flush() -> None:
        if block:
            joined = "".join(block)
            out.append(inert_prose(joined, urls=False) if html else inert_markdown(joined))
            block.clear()

    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if raw:
            block.append(line)
            if raw.search(line):
                raw = None
        elif not line.strip():
            flush()
            out.append(line)
            html = False
        elif not html and (end := fence_end(lines, i)) is not None:
            flush()
            out.append("".join(lines[i:end + 1]))
            i = end
        else:
            bare = OWN.sub("", line)
            if not html and HTML_START.match(bare):
                flush()
                html = True
            started = RAW_START.match(bare) if html else None
            if started:
                kind = next(name for name in RAW_END if started.group(name) is not None)
                if not RAW_END[kind].search(line):
                    raw = RAW_END[kind]
            block.append(line)
        i += 1
    flush()
    return "".join(out)
