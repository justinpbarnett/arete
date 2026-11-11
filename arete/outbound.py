from __future__ import annotations

import re

from . import util

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
