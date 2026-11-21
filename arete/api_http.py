from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import threading
import time
from email.parser import BytesHeaderParser
from http import HTTPStatus
from http.client import HTTPMessage

from . import contract, paths, util

MAX_HEADERS = 100
MAX_JSON_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_INTEGER = 2**53 - 1
HEADER = re.compile(rb"[!#$%&'*+.^_`|~0-9a-zA-Z-]+:[\t\x20-\x7e\x80-\xff]*\r\n")
ETAG = re.compile(r'(?:W/)?("[\x21\x23-\x7e\x80-\xff]*")')
STATUS_CODES = {
    400: "invalid_input", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
    409: "conflict", 413: "body_too_large", 414: "uri_too_long", 415: "unsupported_media_type",
    421: "misdirected_request", 422: "invalid_fields", 429: "rate_limited", 431: "headers_too_large",
    500: "internal_error", 501: "not_implemented", 502: "upstream_failed", 503: "draining",
    505: "unsupported_http_version",
}


class RateLimited(contract.Problem):
    def __init__(self, seconds):
        self.retry_after = seconds
        super().__init__("rate_limited", "Too many requests. Try again shortly.", retry_after=seconds)


def read_headers(stream):
    lines = []
    while True:
        line = stream.readline(65537)
        if line == b"\r\n":
            return BytesHeaderParser(_class=HTTPMessage).parsebytes(b"".join(lines))
        if len(line) > 65536 or len(lines) >= MAX_HEADERS:
            raise contract.Problem("headers_too_large", "A request takes at most 100 headers.")
        if not HEADER.fullmatch(line):
            raise contract.Problem("invalid_framing", "The request headers are malformed.")
        lines.append(line)


def content_length(headers, required):
    lengths = headers.get_all("Content-Length", [])
    if headers.get_all("Transfer-Encoding") or len(lengths) > 1:
        raise contract.Problem("invalid_framing", "Send a single Content-Length and omit Transfer-Encoding.")
    if not lengths:
        if required:
            raise contract.Problem("invalid_framing", "The request needs Content-Length.")
        return 0
    value = lengths[0].strip(" \t")
    if not re.fullmatch(r"[0-9]+", value):
        raise contract.Problem("invalid_framing", "Content-Length takes a whole number of bytes.")
    digits = value.lstrip("0")
    return int(digits or "0") if len(digits) <= 19 else 2**63


def check_media(headers, allowed, size):
    types = headers.get_all("Content-Type", [])
    if not types and not size:
        return
    if len(types) != 1 or types[0].partition(";")[0].strip().lower() not in allowed:
        raise contract.Problem("unsupported_media_type", "The route does not accept this Content-Type.")


def read_body(stream, size, limit):
    if size > limit:
        raise contract.Problem("body_too_large", f"A request body holds at most {limit // 1024} KB.")
    data = stream.read(size)
    if len(data) != size:
        raise contract.Problem("invalid_framing", "The request ended before Content-Length bytes arrived.")
    return data


def json_depth(text):
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise contract.Problem("invalid_json", "JSON may nest at most 32 levels.")
        elif char in "]}":
            depth -= 1


def json_pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise contract.Problem("invalid_json", "JSON object names must be unique.")
    return result


def json_integer(text):
    if len(text.lstrip("-")) > 16 or abs(value := int(text)) > MAX_JSON_INTEGER:
        raise contract.Problem("invalid_json", "JSON integers must be between -9007199254740991 and 9007199254740991.")
    return value


def json_number(text):
    value = float(text)
    if not math.isfinite(value):
        raise contract.Problem("invalid_json", "JSON numbers must be finite.")
    return value


def json_object(raw):
    try:
        text = raw.decode("utf-8") or "{}"
        json_depth(text)
        value = json.loads(text, object_pairs_hook=json_pairs, parse_int=json_integer,
                           parse_float=json_number, parse_constant=json_number)
        if not isinstance(value, dict):
            raise contract.Problem("invalid_json", "The request body is not a JSON object.")
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):
        raise contract.Problem("invalid_json", "The request body is not valid JSON.") from None
    return value


def json_bytes(value):
    return json.dumps(value, default=str).encode()


def problem(status, answer):
    code = answer.get("code")
    if not any(error["code"] == code and error["status"] == status for error in contract.ERRORS):
        code = STATUS_CODES[status]
    return {**answer, "code": code, "type": f"urn:arete:problem:{code}", "title": HTTPStatus(status).phrase,
            "status": status, "detail": answer["error"]}


def etag(payload, kind, seq):
    key = f"{kind}\0{seq}\0".encode() + payload
    return f'"{hashlib.sha256(key).hexdigest()}"'


def matches_etag(headers, tag):
    value = ",".join(headers.get_all("If-None-Match", [])).strip()
    return value == "*" or any(found == tag for found in ETAG.findall(value))


class RateLimits:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.buckets = {}
        self.swept = self.clock()

    def take(self, actor, command, cost=1):
        rate, burst = (20, 60) if command else (50, 50)
        identity = actor, command
        with self.lock:
            now = self.clock()
            if now - self.swept >= 60:
                self.buckets = {key: bucket for key, bucket in self.buckets.items() if now - bucket[0] < 60}
                self.swept = now
            at, available = self.buckets.get(identity, (now, burst))
            available = min(burst, available + (now - at) * rate)
            if available < cost:
                raise RateLimited(math.ceil((cost - available) / rate))
            self.buckets[identity] = now, available - cost


RATES = RateLimits()
ALIAS_DAYS = {}


def log_request(method, route, status, actor, alias, started):
    print(json.dumps({"event": "api.request", "at": util.iso(), "method": method, "route": route,
                      "status": status, "actor": actor, "alias": alias,
                      "ms": round((time.monotonic() - started) * 1000)}), file=sys.stderr, flush=True)


def record_alias(method, route):
    key = method, route
    day = util.iso()[:10]
    with contract.USE_LOCK:
        contract.USES[key] += 1
        path = paths.STATE_DIR / "api-aliases.json"
        if ALIAS_DAYS.get((path, key)) == day:
            return
        with util.lock("api-aliases", wait=True):
            days = util.read_json(path, {})
            name = f"{method} {route}"
            if days.get(name, "") < day:
                util.write_json(path, {**days, name: day})
            ALIAS_DAYS[path, key] = day
