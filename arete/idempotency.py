from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from urllib.parse import unquote

from . import refusals
from .store import db, events
from .store import idempotency as claims

KEY = re.compile(r"[A-Za-z0-9_.:/-]{8,128}")
LOCK = threading.Lock()
ACTIVE = {}


class Reused(refusals.Invalid):
    status = 422
    code = "idempotency_key_reused"


class InFlight(refusals.Conflict):
    status = 409
    code = "idempotency_in_flight"


class Unconfirmed(refusals.Conflict):
    pass


def fingerprint(method, route, parameters, body):
    canonical = [method.upper(), route, {k: unquote(v) for k, v in parameters.items()}, body]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


@contextmanager
def owning(actor, key, sha, route="/api/tasks"):
    if not KEY.fullmatch(key):
        from .control import Refused

        raise Refused("Idempotency-Key must contain 8-128 characters from A-Z, a-z, 0-9, _ . : / -")
    identity = actor, key
    with LOCK:
        if identity in ACTIVE:
            if ACTIVE[identity] != sha:
                raise Reused("This key was used with different content")
            raise InFlight("A request with this key is still in flight")
        ACTIVE[identity] = sha
    try:
        yield actor, key, route, sha
    finally:
        with LOCK:
            ACTIVE.pop(identity, None)


def matching_claim(context):
    row = claims.lookup(*context[:2])
    if row and (row["route"], row["request_sha"]) != context[2:]:
        raise Reused("This key was used with different content")
    return row


class Publication:
    def __init__(self, context, row):
        self.context = context
        self.bind_send = None
        self.pending = json.loads(row["response"]) if row else None
        tag = db.schema_metadata()["instance_tag"]
        self.request_marker = f"arete:{tag}:k-{context[3][:8]}"
        identity = hashlib.sha256(json.dumps(context, separators=(",", ":")).encode()).hexdigest()[:32]
        self.marker = (self.pending or {}).get("marker", f"{self.request_marker}.{identity}")

    def remember(self, target):
        target = {**target, "marker": self.marker}
        payload = json.dumps(target).encode()
        if self.pending is None:
            claims.claim(self.context, None, payload)
        else:
            claims.update_pending(self.context, payload)
        self.pending = target

    def require_unsent(self):
        if self.pending.get("send_started"):
            raise Unconfirmed("The target has not confirmed the earlier publication. Retry with the same key to check again.")

    def begin_send(self):
        self.require_unsent()
        with db.tx():
            if self.bind_send is not None:
                self.bind_send(self.marker)
            self.remember({**self.pending, "send_started": True})

    def text(self, body):
        markers = self.marker if self.marker == self.request_marker else f"{self.request_marker}\n{self.marker}"
        return f"{body.rstrip()}\n\n{markers}"

    def record(self, subject, url, result):
        seq = events.emit("publication.sent", subject, cls="durable", data={"url": url})
        self.remember({"result": result, "url": url, "seq": seq})

    def complete(self, payload):
        return claims.complete(self.context, 200, payload, (self.pending or {}).get("seq"))
