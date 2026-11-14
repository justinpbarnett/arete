from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import config, outbound, paths, state, util

TIMEOUT_SECONDS = 10
TTL_SECONDS = 86400
TOKEN_SECONDS = 12 * 3600
MAX_DEVICES = 20
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
GONE = (404, 410)
KEY_FIELD = re.compile(r"[A-Za-z0-9_-]{1,200}={0,2}")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
SPKI_POINT_BYTES = 65

known_key: tuple[tuple[int, int], str] | None = None


class Refused(ValueError):
    pass


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def openssl(args: list[str], stdin: bytes | None = None) -> bytes:
    try:
        res = subprocess.run(["openssl", *args], input=stdin, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        raise Refused("openssl could not make the push key") from None
    if res.returncode != 0:
        raise Refused("openssl could not make the push key")
    return res.stdout


def private_key_file() -> Path:
    target = paths.PUSH_KEY_FILE
    if not target.exists():
        paths.ensure_dirs()
        pem = openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout"])
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".webpush.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
            try:
                os.link(temp, target)
            except FileExistsError:
                pass
        finally:
            os.unlink(temp)
    if stat.S_IMODE(target.stat().st_mode) & 0o077:
        target.chmod(0o600)
    return target


def public_key() -> str:
    global known_key
    file = private_key_file()
    info = file.stat()
    stamp = (info.st_ino, info.st_mtime_ns)
    if known_key and known_key[0] == stamp:
        return known_key[1]
    point = openssl(["ec", "-in", str(file), "-pubout", "-outform", "DER"])[-SPKI_POINT_BYTES:]
    if len(point) != SPKI_POINT_BYTES or point[0] != 4:
        raise Refused("openssl could not make the push key")
    known_key = (stamp, base64url(point))
    return known_key[1]


def raw_signature(der: bytes) -> bytes:
    def read_length(at: int) -> tuple[int, int]:
        first = der[at]
        if first < 0x80:
            return first, at + 1
        size = first & 0x7F
        return int.from_bytes(der[at + 1:at + 1 + size], "big"), at + 1 + size

    def read_integer(at: int) -> tuple[bytes, int]:
        length, at = read_length(at + 1)
        return int.from_bytes(der[at:at + length], "big").to_bytes(32, "big"), at + length

    _, at = read_length(1)
    r, at = read_integer(at)
    s, _ = read_integer(at)
    return r + s


def token(audience: str, contact: str) -> str:
    claims = {"aud": audience, "exp": int(util.now().timestamp()) + TOKEN_SECONDS}
    if contact:
        claims["sub"] = contact
    parts = [json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")),
             json.dumps(claims, separators=(",", ":"))]
    signing_input = ".".join(base64url(p.encode()) for p in parts)
    der = openssl(["dgst", "-sha256", "-sign", str(private_key_file())], signing_input.encode())
    return f"{signing_input}.{base64url(raw_signature(der))}"


def subject(cfg: config.Config, row: dict) -> str:
    if cfg.ask.push_contact:
        return cfg.ask.push_contact
    return f"mailto:{row['contact']}" if EMAIL.fullmatch(row.get("contact") or "") else ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kw):
        return None


def post(endpoint: str, headers: dict) -> int:
    request = urllib.request.Request(endpoint, data=b"", headers=headers, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=TIMEOUT_SECONDS) as reply:
            return reply.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, ValueError):
        return 0


@dataclass
class Sent:
    sent: int = 0
    removed: int = 0
    failed: int = 0


def audience_of(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


def send_all(cfg: config.Config) -> Sent:
    found = Sent()
    rows = state.push_list()
    if not rows:
        return found
    key = public_key()
    delivered, gone = [], []
    for row in rows:
        headers = {"Authorization": f"vapid t={token(audience_of(row['endpoint']), subject(cfg, row))}, k={key}",
                   "TTL": str(TTL_SECONDS), "Urgency": "high", "Content-Length": "0"}
        status = post(row["endpoint"], headers)
        if 200 <= status < 300:
            delivered.append(row["endpoint"])
        elif status in GONE:
            gone.append(row["endpoint"])
        else:
            found.failed += 1
    found.sent, found.removed = len(delivered), len(gone)
    state.push_succeeded(delivered)
    state.push_drop(gone)
    return found


def words(found: Sent) -> str:
    said = [f"sent to {found.sent} device{'' if found.sent == 1 else 's'}"]
    if found.removed:
        said.append(f"removed {found.removed} its push service no longer knows")
    if found.failed:
        said.append(f"{found.failed} failed")
    return ", ".join(said)


def ask_opened(cfg: config.Config, ask_ids: list[str]) -> list[str]:
    try:
        if not state.push_list():
            return []
        return [f"push for ask {ask_id}: {words(send_all(cfg))}" for ask_id in ask_ids]
    except Exception as e:
        return [f"push for a new ask failed: {type(e).__name__}"]


def remind(cfg: config.Config) -> list[str]:
    if not state.push_list():
        return []
    cutoff = util.iso(util.now() - timedelta(hours=cfg.ask.remind_hours))
    notes = []
    for ask in state.open_asks():
        if ask.get("reminded") or ask["asked"] > cutoff or not state.ask_remind(ask["id"]):
            continue
        notes.append(f"reminder push for ask {ask['id']}: {words(send_all(cfg))}")
    return notes


def device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def view() -> dict:
    return {"key": public_key(),
            "devices": [{"id": device_id(r["endpoint"]), "label": r["label"], "created": r["created"],
                         "last_success": r["last_success"]} for r in state.push_list()]}


def checked_endpoint(endpoint) -> str:
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        raise Refused("The subscription needs an endpoint address of at most 2048 characters.")
    try:
        parts = urlsplit(endpoint)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise Refused("The subscription's endpoint address is not valid.") from None
    if not host or port == 0 or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Refused("The subscription's endpoint address is not valid.")
    loopback = host in LOOPBACK_HOSTS
    if paths.ISOLATED and not loopback:
        raise Refused("Scratch state pushes only to a local listener.")
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise Refused("The subscription's endpoint must be an https address.")
    return endpoint


def subscribe(body: dict, contact: str) -> dict:
    endpoint = checked_endpoint(body.get("endpoint"))
    keys = body.get("keys")
    keys = keys if isinstance(keys, dict) else {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    for value in (p256dh, auth):
        if not isinstance(value, str) or not KEY_FIELD.fullmatch(value):
            raise Refused("The subscription's keys are missing or not valid.")
    if endpoint not in {r["endpoint"] for r in state.push_list()} and len(state.push_list()) >= MAX_DEVICES:
        raise Refused(f"At most {MAX_DEVICES} devices can be subscribed; remove one first.")
    state.push_add(endpoint, p256dh, auth, outbound.plain(body.get("label"), 40) or "Device", contact)
    return view()


def remove(device_id_: str) -> dict:
    state.push_drop([r["endpoint"] for r in state.push_list() if device_id(r["endpoint"]) == device_id_])
    return view()


def test(cfg: config.Config) -> dict:
    if not state.push_list():
        raise Refused("No device is subscribed, so there is nothing to test.")
    found = send_all(cfg)
    return {"sent": found.sent, "removed": found.removed, "failed": found.failed, **view()}
