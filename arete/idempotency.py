from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from urllib.parse import unquote

from . import control

KEY = re.compile(r"[A-Za-z0-9_.:/-]{8,128}")
LOCK = threading.Lock()
ACTIVE = {}


class Reused(control.Refused):
    status = 422
    code = "idempotency_key_reused"


class InFlight(control.Refused):
    status = 409
    code = "idempotency_in_flight"


def fingerprint(method, route, parameters, body):
    canonical = [method.upper(), route, {k: unquote(v) for k, v in parameters.items()}, body]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


@contextmanager
def owning(actor, key, sha):
    if not KEY.fullmatch(key):
        raise control.Refused("Idempotency-Key must contain 8-128 characters from A-Z, a-z, 0-9, _ . : / -")
    identity = actor, key
    with LOCK:
        if identity in ACTIVE:
            if ACTIVE[identity] != sha:
                raise Reused("This key was used with different content")
            raise InFlight("This task start is still in flight")
        ACTIVE[identity] = sha
    try:
        yield actor, key, "/api/tasks", sha
    finally:
        with LOCK:
            ACTIVE.pop(identity, None)
