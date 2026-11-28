from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit

from . import api_http, config, contract, idempotency, principals, util
from .store import db, events, tokens
from .store import idempotency as claims

COOKIE = "__Host-arete_session"
PAIRING_START_ROUTE = "/api/v1/auth/pairings"
PAIRING_CLAIM_ROUTE = "/api/v1/auth/pairings/claim"
TLS_OPERATIONS = {"token.issue", "token.rotate", "pairing.claim", "secret.set", "webhook.rotate"}
PRESENCE = {"token.issue", "token.rotate", "secret.set", "secret.remove", "webhook.rotate"}
WRITES = ("POST", "PATCH", "PUT", "DELETE")
SCOPE = re.compile(r"read|[a-z_]+:(?:read|write)|config:activate")
AUTH_MESSAGES = {error["code"]: error["description"] for error in contract.ERRORS}
LEGACY_SCOPES = {"skipped": "task", "checks": "check", "sessions": "session", "stage": "capture"}


def problem(code, **details):
    return contract.Problem(code, AUTH_MESSAGES[code], **details)


@dataclass
class Pairing:
    code: str | None
    key: str
    key_ref: str
    deadline: float
    expires: str
    remaining: int = 5


class Traffic:
    def __init__(self):
        self.lock = threading.Lock()
        self.failed = {}
        self.blocked = {}
        self.issues = {}
        self.pairings = {}
        self.claimed = {}
        self.swept = time.monotonic()

    def check(self, peer):
        now = time.monotonic()
        with self.lock:
            if now - self.swept >= 60:
                self.failed = {k: v for k, v in self.failed.items() if v and v[-1] > now - 300}
                self.blocked = {k: v for k, v in self.blocked.items() if v > now}
                self.issues = {k: v for k, v in self.issues.items() if v and v[-1] > now - 600}
                for pairing in self.pairings.values():
                    if pairing.deadline <= now:
                        pairing.code = None
                self.claimed = {k: v for k, v in self.claimed.items() if v[0] > now}
                self.swept = now
            if (until := self.blocked.get(peer, 0)) > now:
                raise problem("auth_blocked", retry_after=math.ceil(until - now))

    def fail(self, peer):
        now = time.monotonic()
        with self.lock:
            if self.blocked.get(peer, 0) > now:
                return
            attempts = recent(self.failed, peer, now - 300)
            attempts.append(now)
            if len(attempts) >= 10:
                self.blocked[peer] = now + 900
                del self.failed[peer]

    def issue(self, peer):
        now = time.monotonic()
        with self.lock:
            attempts = recent(self.issues, peer, now - 600)
            if len(attempts) >= 3:
                raise api_http.RateLimited(math.ceil(attempts[0] + 600 - now))
            attempts.append(now)

    def start_pairing(self, person, key, ref, origin):
        now = time.monotonic()
        with self.lock:
            pairing = self.pairings.get(person)
            if pairing is not None and pairing.key == key:
                if pairing.code is None or pairing.deadline <= now or pairing.key_ref != ref:
                    raise pairing_claimed()
            else:
                pairing = Pairing(f"{secrets.randbelow(1_000_000):06d}", key, ref, now + 300,
                                  util.iso(util.now() + timedelta(minutes=5)))
                self.pairings[person] = pairing
            return {"code": pairing.code, "link": f"{origin}/pair#{pairing.code}", "expires": pairing.expires}

    def reject_pairing_key(self, person, key):
        with self.lock:
            if (pairing := self.pairings.get(person)) and pairing.key == key:
                raise idempotency.Reused("This key was used with different content")

    def claim_pairing(self, person, code, ref):
        now = time.monotonic()
        with self.lock:
            pairing = self.pairings.get(person)
            if not pairing or pairing.code is None or pairing.key_ref != ref or pairing.deadline <= now:
                raise problem("unauthenticated")
            if hmac.compare_digest(code.encode(), pairing.code.encode()):
                pairing.code = None
                return
            pairing.remaining -= 1
            if pairing.remaining == 0:
                pairing.code = None
            raise problem("unauthenticated")

    def claim_receipt(self, person, key, ref, session):
        now = time.monotonic()
        identity = person, key, ref
        with self.lock:
            if session:
                self.claimed[identity] = (now + 300, session)
            deadline, text = self.claimed.get(identity, (0, None))
            return text if deadline > now else None


def recent(buckets, key, since):
    values = buckets.setdefault(key, deque())
    while values and values[0] <= since:
        values.popleft()
    return values


def require_scope(credential, scope):
    if credential is None:
        if scope != "read":
            raise problem("unauthenticated")
    elif not credential.allows(scope):
        raise problem("scope_missing")


def session_cookie(text, age):
    return f"{COOKIE}={text}; Max-Age={age}; HttpOnly; Secure; SameSite=Strict; Path=/"


def session_values(headers):
    return [value for line in headers.get_all("Cookie", []) for piece in line.split(";")
            for name, _, value in [piece.strip().partition("=")] if name == COOKIE]


def require_presence(credential):
    if credential is None or credential.kind != "session":
        raise problem("forbidden", reason="presence_required")


def require_tls(op):
    return op is not None and op["name"] in TLS_OPERATIONS


def registry_operation(name):
    return isinstance(name, str) and (name.startswith("secret.") or name in ("account.list", "webhook.rotate"))


def origin_of(scheme, host):
    if not isinstance(scheme, str) or not isinstance(host, str):
        return None
    scheme = scheme.strip().lower()
    if (scheme not in ("http", "https") or not host
            or any(ord(c) <= 0x20 or ord(c) == 0x7f or c in "@/?#\\" for c in host)):
        return None
    try:
        parsed = urlsplit(f"{scheme}://{host}")
        if (parsed.hostname and parsed.username is None and parsed.password is None
                and not (parsed.path or parsed.query or parsed.fragment)):
            port = parsed.port
            if port == 0:
                return None
            return scheme, parsed.hostname, port if port is not None else (443 if scheme == "https" else 80)
    except ValueError:
        pass
    return None


def check_origin(handler):
    origins = handler.headers.get_all("Origin", [])
    if any(site.strip().lower() == "cross-site" for site in handler.headers.get_all("Sec-Fetch-Site", [])):
        raise problem("bad_origin")
    if origins:
        scheme, _, host = origins[0].strip().partition("://")
        if len(origins) != 1 or origin_of(scheme, host) != handler.origin:
            raise problem("bad_origin")


def authenticate(handler, method, path, *, browser_asset=False, mcp=False, scratch=False):
    op = handler.operation[0] if handler.operation else None
    name = op["name"] if op else ""
    secure = handler.origin[0] == "https"
    auth = handler.headers.get_all("Authorization", [])
    cookies = session_values(handler.headers)
    claim = name == "pairing.claim"
    meta = method == "GET" and path in ("/api/meta", "/api/v1/meta")
    api_route = path == "/api" or path.startswith("/api/")
    scratch_exempt = scratch and not registry_operation(name)
    requires_credential = api_route and not (mcp or scratch_exempt or meta or claim or name == "token.issue")
    if (mcp or require_tls(op) and (auth or cookies or not requires_credential)) and not secure:
        raise problem("tls_required")
    peer = handler.client_address[0]
    logins = handler.server.logins
    traffic = logins.credentials
    traffic.check(peer)
    if mcp and not auth:
        raise problem("unauthenticated")
    if auth and cookies:
        raise problem("ambiguous_credential")
    if claim and auth:
        raise problem("unauthenticated")

    text, credential = None, None
    if auth or (cookies and not claim):
        try:
            if auth:
                parts = auth[0].split()
                if len(auth) != 1 or len(parts) != 2 or parts[0].lower() != "bearer":
                    raise tokens.InvalidCredential()
                text, kind = parts[1], "token"
            else:
                if not secure or len(cookies) != 1:
                    raise tokens.InvalidCredential()
                text, kind = cookies[0], "session"
            credential = tokens.resolve(text, kinds=(kind,))
        except tokens.InvalidCredential as error:
            traffic.fail(peer)
            if cookies and secure:
                handler.set_cookie = session_cookie("", 0)
            if not (cookies and browser_asset and method == "GET"):
                raise problem(error.code) from None
    handler.credential = credential
    handler.person = credential.principal if credential else principals.for_login(logins.of(peer))
    if name == "meta.get" and (not handler.person and not (auth or cookies) or
                               credential and not credential.allows("read")):
        handler.instance_header = db.schema_metadata()["instance_id"]
    if not handler.person:
        raise problem("forbidden")
    handler.actor = credential.actor if credential else handler.person
    principals.admit(handler.actor)

    write = method in WRITES and name != "batch.run" and (method != "PUT" or op is not None)
    origin_checked = mcp or (method in WRITES and (method != "PUT" or op is not None)) and (credential or claim)
    if origin_checked:
        check_origin(handler)
        if cookies and handler.headers.get_all("X-Arete", []) != ["1"]:
            raise problem("csrf")
    if not (claim or mcp):
        authorize(handler, op, name, write, secure, peer, requires_credential=requires_credential)
    if credential:
        logins.of(peer)
        try:
            tokens.note_use(credential, logins.node_key(peer))
        except tokens.InvalidCredential as error:
            raise problem(error.code) from None
        if credential.kind == "session":
            handler.set_cookie = session_cookie(text, tokens.SESSION_SECONDS)


def unauthenticated(handler, write, secure):
    if write and handler.headers.get_all("X-Arete", []) == ["1"]:
        message = ("Pair this browser using ‘arete pair’ or Pair a device in a paired browser." if secure else
                   "Browser pairing needs HTTPS. Set https = true in [dashboard] on the serve host.")
        return contract.Problem("unauthenticated", message)
    return problem("unauthenticated")


def authorize(handler, op, name, write, secure, peer, *, requires_credential):
    credential = handler.credential
    if requires_credential and credential is None:
        raise unauthenticated(handler, write, secure)
    if name.startswith("secret.") or name == "webhook.rotate":
        secret_access(unquote(handler.operation[1].get("scope", "")))
    if name == "pairing.start" and (credential is None or credential.kind != "session"):
        with db.reader() as c:
            if tokens.live_session(c):
                require_presence(credential)
    if name in PRESENCE:
        if credential is None and name == "token.issue":
            logins = handler.server.logins
            logins.credentials.issue(logins.node_key(peer))
        require_presence(credential)
    if write and credential is None:
        raise unauthenticated(handler, write, secure)
    if name == "session.end" and credential.kind != "session":
        raise problem("unauthenticated")
    if name == "session.end" or (name == "token.revoke" and credential.kind == "token"
            and unquote(handler.operation[1]["token"]) == credential.id):
        return
    scope = op["scope"] if op else "read"
    if write and not op:
        provider = (handler.alias or {}).get("provider", "")
        resource = provider.removeprefix("post_").split("_", 1)[0]
        scope = f"{LEGACY_SCOPES.get(resource, resource)}:write" if resource else "*"
    require_scope(credential, scope)


def secret_access(scope):
    if scope.startswith("principal:"):
        raise problem("not_found")
    if not config.secret_settings().store:
        raise problem("secret_store_missing")


def issue_grants(values):
    label = values["label"].strip()
    if not label or len(label) > 120 or any(not SCOPE.fullmatch(scope) for scope in values["scopes"]):
        raise contract.Problem("invalid_input", "Send a label of up to 120 characters and valid scopes.")
    now = util.now()
    expires = now + timedelta(days=90)
    if "expires" in values:
        try:
            parsed = util.parse_iso(values["expires"])
            if parsed.tzinfo is None:
                raise ValueError()
            expires = parsed.astimezone(UTC)
            if not now < expires <= now + timedelta(days=365):
                raise ValueError()
        except (ValueError, OverflowError):
            raise contract.Problem("invalid_input", "Expires must be a timestamp within the next 365 days.") from None
    cap = None
    if "spend_cap_usd" in values:
        try:
            nanos = Decimal(str(values["spend_cap_usd"])) * 10**9
        except InvalidOperation:
            raise contract.Problem("invalid_input", "Spend cap must fit in whole nano-US dollars.") from None
        if not nanos.is_finite() or nanos < 0 or nanos > 2**63 - 1 or nanos != nanos.to_integral_value():
            raise contract.Problem("invalid_input", "Spend cap must fit in whole nano-US dollars.")
        cap = int(nanos)
    return label, {"expires": util.iso(expires), "profile": values.get("profile", "remote"),
                   "commands": values.get("commands"), "spend_cap_nusd": cap}


def write_operation(name, values, handler):
    person = handler.person
    if name == "session.end":
        tokens.end_session(handler.credential.id)
        return {"ended": True}
    if name == "principal.revoke":
        principal = values["principal"]
        count = tokens.revoke_principal(principal)
        if count is not None:
            return {"principal": principal, "revoked": count}
    elif name == "pairing.claim":
        traffic = handler.server.logins.credentials
        ref = db.schema_metadata()["token_key_ref"]
        try:
            traffic.claim_pairing(person, values["code"], ref)
        except contract.Problem:
            traffic.fail(handler.client_address[0])
            raise
        _, text = tokens.issue(person, "Browser session", ["*"], kind="session", expires=tokens.session_lifetime(util.now()))
        traffic.claim_receipt(person, handler.headers["Idempotency-Key"], ref, text)
        return {"principal": person}
    elif name == "token.revoke":
        if revoked := tokens.revoke(values["token"], person):
            return {"token": revoked}
    else:
        issued = issue_or_rotate(name, person, values)
        if issued:
            token, text = issued
            return {"token": token, "text": text}
    raise problem("not_found")


def issue_or_rotate(name, person, values):
    if name == "token.issue":
        label, grants = issue_grants(values)
        return tokens.issue(person, label, values["scopes"], **grants)
    try:
        return tokens.rotate(values["token"], person, values.get("grace_seconds", 300))
    except tokens.InvalidCredential:
        raise contract.Problem("conflict", "This token can no longer be rotated.") from None
    except OverflowError:
        raise contract.Problem("invalid_input", "Grace period is too large.") from None


def pairing_claim_fingerprint(sha, salt=None):
    salt = secrets.token_hex(16) if salt is None else salt
    proof = hashlib.scrypt(sha.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return f"{salt}:{proof.hex()}"


def pairing_claimed():
    return contract.Problem("conflict", "This pairing is no longer active. Start a new pairing.", reason="pairing_claimed")


def matching_pairing_claim(context):
    row = claims.lookup(*context[:2])
    if row is None:
        return None
    saved = row["request_sha"]
    salt = saved.partition(":")[0]
    if (row["route"] == PAIRING_CLAIM_ROUTE and re.fullmatch("[0-9a-f]{32}", salt)
            and hmac.compare_digest(saved, pairing_claim_fingerprint(context[3], salt))):
        return row
    raise idempotency.Reused("This key was used with different content")


def finish_pairing_claim(handler):
    ref = db.schema_metadata()["token_key_ref"]
    traffic = handler.server.logins.credentials
    text = traffic.claim_receipt(handler.person, handler.headers["Idempotency-Key"], ref, None)
    if text:
        try:
            tokens.resolve(text, kinds=("session",))
        except tokens.InvalidCredential:
            pass
        else:
            handler.set_cookie = session_cookie(text, tokens.SESSION_SECONDS)
            return
    raise pairing_claimed()


def start_pairing(handler, key):
    with db.tx() as c:
        credential = handler.credential
        if tokens.live_session(c):
            require_presence(credential)
        if credential.role != "owner":
            raise problem("scope_missing")
        ref = db.metadata(c)["token_key_ref"]
        origin = "https://" + handler.headers["Host"]
        result = handler.server.logins.credentials.start_pairing(credential.principal, key, ref, origin)
        return {"seq": events.metadata(c)["head"], **result}
