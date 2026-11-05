from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import util

ENV_KEYS = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
TIMEOUT = 30
RETRIES = 3
MAX_RETRY_WAIT = 120
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


class JiraError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.removeprefix("export ").partition("=")
        if sep:
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def credentials(env_file: Path | None) -> tuple[str, str, str]:
    from_file = read_env_file(env_file) if env_file else {}
    vals = [os.environ.get(k) or from_file.get(k, "") for k in ENV_KEYS]
    missing = [k for k, v in zip(ENV_KEYS, vals) if not v]
    if missing:
        where = f" or in {env_file}" if env_file else "; or name an env file in [jira] env_file"
        raise JiraError(f"missing {', '.join(missing)}: set them in the environment{where}")
    return vals[0].rstrip("/"), vals[1], vals[2]


def parse_time(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")


class AuthStaysOnHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(AuthStaysOnHost)


class Client:
    def __init__(self, base_url: str, email: str, token: str):
        self.base = base_url.rstrip("/")
        self._auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        self._me: dict | None = None

    @classmethod
    def from_env(cls, env_file: Path | None) -> Client:
        return cls(*credentials(env_file))

    def browse_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    def request(self, method: str, path: str, params: dict | None = None, body=None):
        raw = self._send(method, path, params, body)
        return json.loads(raw) if raw.strip() else None

    def attachment(self, attachment_id: str) -> bytes:
        return self._send("GET", f"/rest/api/2/attachment/content/{_path_segment(attachment_id)}", accept="*/*")

    def _send(self, method: str, path: str, params: dict | None = None, body=None,
              accept: str = "application/json") -> bytes:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self._auth, "Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(RETRIES + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with _opener.open(req, timeout=TIMEOUT) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                wait = _retry_after(e, attempt)
                if (e.code == 429 or e.code >= 500) and attempt < RETRIES and wait <= MAX_RETRY_WAIT:
                    time.sleep(wait)
                    continue
                later = f" (retry after {wait:.0f} s)" if wait > MAX_RETRY_WAIT else ""
                raise JiraError(f"jira {method} {path}: HTTP {e.code} {_error_text(e)}{later}".rstrip()) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < RETRIES:
                    if not util.online():
                        util.wait_for_network()
                    time.sleep(2**attempt)
                    continue
                raise JiraError(f"jira {method} {path}: {e}") from None
        raise JiraError(f"jira {method} {path}: out of retries")

    def get(self, path: str, **params):
        return self.request("GET", path, params or None)

    def _paged(self, path: str, key: str = "values", **params) -> list[dict]:
        out: list[dict] = []
        while True:
            res = self.get(path, startAt=len(out), maxResults=100, **params) or {}
            vals = res.get(key, [])
            out += vals
            if not vals or res.get("isLast") or len(out) >= res.get("total", len(out)):
                return out

    def me(self) -> dict:
        if self._me is None:
            self._me = self.get("/rest/api/3/myself") or {}
        return self._me

    def search(self, jql: str, fields: list[str], limit: int = 50) -> list[dict]:
        out: list[dict] = []
        token = ""
        while len(out) < limit:
            params = {"jql": jql, "fields": ",".join(fields), "maxResults": min(100, limit - len(out))}
            if token:
                params["nextPageToken"] = token
            res = self.get("/rest/api/3/search/jql", **params) or {}
            out += res.get("issues", [])
            token = res.get("nextPageToken", "")
            if not token or res.get("isLast", True):
                break
        return out[:limit]

    def issue(self, key: str, fields: list[str]) -> dict:
        return self.get(f"/rest/api/2/issue/{_path_segment(key)}", fields=",".join(fields)) or {}

    def changelog(self, key: str) -> list[dict]:
        return self._paged(f"/rest/api/2/issue/{_path_segment(key)}/changelog")

    def comments(self, key: str) -> list[dict]:
        return self._paged(f"/rest/api/2/issue/{_path_segment(key)}/comment", key="comments", orderBy="created")

    def comment(self, key: str, markdown: str) -> None:
        self.request("POST", f"/rest/api/3/issue/{_path_segment(key)}/comment", body={"body": adf(markdown)})

    def edit_labels(self, key: str, add: list[str] = (), remove: list[str] = ()) -> None:
        ops = [{"add": a} for a in add] + [{"remove": r} for r in remove]
        if ops:
            self.request("PUT", f"/rest/api/2/issue/{_path_segment(key)}", body={"update": {"labels": ops}})

    def transition(self, key: str, name: str) -> bool:
        want = name.strip().lower()
        res = self.get(f"/rest/api/2/issue/{_path_segment(key)}/transitions") or {}
        for t in res.get("transitions", []):
            if want in (t.get("name", "").lower(), ((t.get("to") or {}).get("name") or "").lower()):
                self.request("POST", f"/rest/api/2/issue/{_path_segment(key)}/transitions", body={"transition": {"id": t["id"]}})
                return True
        return False


def _path_segment(key: str) -> str:
    return urllib.parse.quote(key, safe="")


def _retry_after(e: urllib.error.HTTPError, attempt: int) -> float:
    try:
        return max(0.0, float(e.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return float(2**attempt)


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read().decode(errors="replace")
    except OSError:
        return ""
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return util.first_line(raw, 300)
    errors = body.get("errors") if isinstance(body.get("errors"), dict) else {}
    msgs = [str(m) for m in body.get("errorMessages") or []] + [f"{k}: {v}" for k, v in errors.items()]
    return util.first_line("; ".join(msgs) or raw, 300)


_INLINE = re.compile(r"`(?P<code>[^`]+)`|\*\*(?P<strong>[^*]+)\*\*|\[(?P<link>[^\]]+)\]\((?P<href>https?://[^)\s]+)\)")


def _text(text: str, *marks: dict) -> dict:
    return {"type": "text", "text": text, "marks": list(marks)} if marks else {"type": "text", "text": text}


def _inline(text: str) -> list[dict]:
    nodes, at = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > at:
            nodes.append(_text(text[at:m.start()]))
        if m["code"]:
            nodes.append(_text(m["code"], {"type": "code"}))
        elif m["strong"]:
            nodes.append(_text(m["strong"], {"type": "strong"}))
        else:
            nodes.append(_text(m["link"], {"type": "link", "attrs": {"href": m["href"]}}))
        at = m.end()
    if at < len(text):
        nodes.append(_text(text[at:]))
    return nodes


def adf(markdown: str) -> dict:
    content: list[dict] = []
    para: list[str] = []
    bullets: list[str] = []
    code: list[str] | None = None

    def flush() -> None:
        if para:
            nodes: list[dict] = []
            for i, ln in enumerate(para):
                if i:
                    nodes.append({"type": "hardBreak"})
                nodes += _inline(ln)
            content.append({"type": "paragraph", "content": nodes})
            para.clear()
        if bullets:
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": _inline(b)}]} for b in bullets
            ]})
            bullets.clear()

    def code_block(lines: list[str]) -> dict:
        text = "\n".join(lines)
        return {"type": "codeBlock", "content": [{"type": "text", "text": text}] if text else []}

    for line in markdown.splitlines():
        fence = line.strip().startswith("```")
        if code is not None:
            if fence:
                content.append(code_block(code))
                code = None
            else:
                code.append(line)
            continue
        if fence:
            flush()
            code = []
        elif not line.strip():
            flush()
        elif m := re.match(r"^(#{1,6})\s+(.*)$", line):
            flush()
            content.append({"type": "heading", "attrs": {"level": len(m.group(1))}, "content": _inline(m.group(2))})
        elif m := re.match(r"^\s*[-*]\s+(.*)$", line):
            if para:
                flush()
            bullets.append(m.group(1))
        else:
            if bullets:
                flush()
            para.append(line)
    if code is not None:
        content.append(code_block(code))
    flush()
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}
