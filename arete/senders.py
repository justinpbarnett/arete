from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from . import gh, outbound

_PRIOR_OUTBOX_MARK_RE = re.compile(r"\n*<!-- arete:[^\s>]+ --> <!-- arete:[^\s>]+:ob-\d+ -->\s*")


@dataclass(frozen=True)
class SendResult:
    external_id: str
    external_url: str


@dataclass(frozen=True)
class LookResult:
    found: bool
    external_id: str | None
    external_url: str | None
    summary: str


@dataclass(frozen=True)
class Sender:
    connector: str
    kind: str
    send: Callable[[dict, str], SendResult]
    look: Callable[[dict, str], LookResult]
    preview: Callable[[dict, str], str]


REGISTRY: dict[tuple[str, str], Sender] = {}
_BUILTINS_LOADED = False


def register(sender: Sender) -> Sender:
    REGISTRY[(sender.connector, sender.kind)] = sender
    return sender


def _ensure_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from . import send_github, send_jira, send_loop, send_push

    send_github.register_all()
    send_jira.register_all()
    send_push.register_all()
    send_loop.register_all()


def marks_for(marker: str) -> str:
    parts = marker.split(":", 2)
    tag = parts[1] if len(parts) >= 2 and parts[0] == "arete" else ""
    first = f"<!-- arete:{tag} -->" if tag else gh.instance_mark()
    return f"{first} <!-- {marker} -->"


def rendered_github_body(body: str, marker: str) -> str:
    cleaned = _PRIOR_OUTBOX_MARK_RE.sub("\n\n", body).strip()
    screened = str(gh.outgoing(cleaned)).rstrip()
    marks = marks_for(marker)
    return f"{screened}\n\n{marks}" if screened else marks


def rendered_jira_body(body: str, marker: str) -> str:
    screened = outbound.screen(body).rstrip()
    return f"{screened}\n\n{marker}" if screened else marker


def known_connectors() -> frozenset[str]:
    _ensure_builtins()
    return frozenset(connector for connector, _ in REGISTRY)


def supported_kinds(connector: str) -> frozenset[str]:
    _ensure_builtins()
    return frozenset(kind for conn, kind in REGISTRY if conn == connector)


def connector_of(target: str, obj: str = "") -> str:
    if isinstance(obj, str) and ":" in obj:
        return obj.partition(":")[0].strip().lower()
    if isinstance(target, str) and target.strip():
        return target.partition(":")[0].strip().lower()
    return ""


def resolve(target: str, kind: str, obj: str = "") -> Sender:
    _ensure_builtins()
    connector = connector_of(target, obj)
    sender = REGISTRY.get((connector, kind))
    if sender is None:
        raise ValueError(f"no outbox sender for {connector or target!r} kind {kind!r}")
    return sender


def preview(item: dict, body: str) -> str:
    if item.get("legacy_approval") is not None or not item.get("marker"):
        return body
    sender = resolve(item["target"], item["kind"], item.get("object") or "")
    return sender.preview(item, body)
