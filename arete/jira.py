from __future__ import annotations

import base64
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.client import RemoteDisconnected
from pathlib import Path

from . import adf, outbound, paths, util

ENV_KEYS = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
TIMEOUT = 30
RETRIES = 3
MAX_RETRY_WAIT = 120
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


class JiraError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class JiraUnsure(JiraError):
    pass


def credentials(env_file: Path | None) -> tuple[str, str, str]:
    if paths.ISOLATED:
        raise JiraError(paths.ISOLATED_LOGINS)
    from_file = util.read_env_file(env_file) if env_file else {}
    vals = [os.environ.get(k) or from_file.get(k, "") for k in ENV_KEYS]
    missing = [k for k, v in zip(ENV_KEYS, vals) if not v]
    if missing:
        where = f" or in {env_file}" if env_file else "; or name an env file in [jira] env_file"
        raise JiraError(f"missing {', '.join(missing)}: set them in the environment{where}")
    base = vals[0].rstrip("/")
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise JiraError(f"JIRA_BASE_URL {base!r} is not a full URL; write it like https://your-site.atlassian.net")
    return base, vals[1], vals[2]


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
        self._statuses: dict[str, set[str]] = {}

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
                if method == "POST" and e.code >= 500:
                    raise JiraUnsure(f"jira {method} {path}: HTTP {e.code} {_error_text(e)}", e.code) from None
                if (e.code == 429 or e.code >= 500) and attempt < RETRIES and wait <= MAX_RETRY_WAIT:
                    time.sleep(wait)
                    continue
                later = f" (retry after {wait:.0f} s)" if wait > MAX_RETRY_WAIT else ""
                raise JiraError(f"jira {method} {path}: HTTP {e.code} {_error_text(e)}{later}".rstrip(), e.code) from None
            except (urllib.error.URLError, TimeoutError, OSError, RemoteDisconnected) as e:
                never_sent = isinstance(e, urllib.error.URLError) and isinstance(
                    e.reason, (socket.gaierror, ConnectionRefusedError))
                if method == "POST" and not never_sent:
                    raise JiraUnsure(f"jira {method} {path}: {e}") from None
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
            out += [_read_description(issue) for issue in res.get("issues", [])]
            token = res.get("nextPageToken", "")
            if not token or res.get("isLast", True):
                break
        return out[:limit]

    def issue(self, key: str, fields: list[str]) -> dict:
        return _read_description(self.get(f"/rest/api/2/issue/{_path_segment(key)}", fields=",".join(fields)) or {})

    def statuses(self, project: str) -> set[str]:
        if project not in self._statuses:
            types = self.get(f"/rest/api/3/project/{_path_segment(project)}/statuses") or []
            self._statuses[project] = {st.get("name", "") for t in types for st in t.get("statuses") or []}
        return self._statuses[project]

    def changelog(self, key: str) -> list[dict]:
        return self._paged(f"/rest/api/2/issue/{_path_segment(key)}/changelog")

    def comments(self, key: str) -> list[dict]:
        return self._paged(f"/rest/api/2/issue/{_path_segment(key)}/comment", key="comments", orderBy="created")

    def find_comment_marker(self, key: str, marker: str) -> str:
        for row in self.comments(key):
            if marker in (row.get("body") or "").splitlines():
                return self.comment_url(key, row["id"])
        return ""

    def comment_url(self, key: str, comment_id) -> str:
        return f"{self.browse_url(key)}?focusedCommentId={_path_segment(str(comment_id))}"

    def comment(self, key: str, markdown: str, *, return_url: bool = False,
                single_attempt: bool = False) -> str | None:
        token, start = uuid.uuid4().hex, util.now()
        path = f"/rest/api/3/issue/{_path_segment(key)}/comment"
        body = {"body": adf.from_markdown(outbound.screen(markdown)), "properties": [{"key": "arete", "value": {"key": token}}]}
        try:
            posted = self.request("POST", path, body=body)
        except JiraUnsure:
            if single_attempt:
                raise
            try:
                if not util.wait_for_network():
                    raise JiraUnsure("network unavailable")
                if not (account := self.me().get("accountId")):
                    raise JiraUnsure("could not identify the comment author")
                recent = self.get(path, orderBy="-created", maxResults=10)
                if not isinstance(recent, dict) or not isinstance(recent.get("comments"), list):
                    raise JiraUnsure("comment lookup returned no list")
                for row in recent["comments"]:
                    if (row.get("author") or {}).get("accountId") != account:
                        continue
                    if parse_time(row["created"]) < start - timedelta(seconds=60):
                        continue
                    try:
                        prop = self.get(f"/rest/api/3/comment/{_path_segment(str(row['id']))}/properties/arete")
                    except JiraError as e:
                        if e.status == 404:
                            continue
                        raise
                    if not isinstance(prop, dict) or not isinstance(prop.get("value"), dict):
                        raise JiraUnsure("comment property lookup returned no value")
                    if prop["value"].get("key") == token:
                        return self.comment_url(key, row["id"]) if return_url else None
                posted = self.request("POST", path, body=body)
            except (JiraError, ValueError, TypeError, KeyError, AttributeError) as e:
                raise JiraUnsure(f"Jira comment reply was lost; check the ticket before sending again: {e}",
                                 getattr(e, "status", 0)) from e
        return self.comment_url(key, posted["id"]) if return_url and isinstance(posted, dict) and posted.get("id") else None

    def edit_labels(self, key: str, add: list[str] = (), remove: list[str] = ()) -> None:
        ops = [{"add": a} for a in add] + [{"remove": r} for r in remove]
        if ops:
            self.request("PUT", f"/rest/api/2/issue/{_path_segment(key)}", body={"update": {"labels": ops}})

    def move(self, key: str, target: str) -> bool:
        try:
            status_exists = target.lower() in {s.lower() for s in self.statuses(key.rsplit("-", 1)[0])}
        except JiraError:
            status_exists = False
        want = target.strip().lower()
        found = (self.get(f"/rest/api/2/issue/{_path_segment(key)}/transitions") or {}).get("transitions", [])
        pick = next((t for t in found if ((t.get("to") or {}).get("name") or "").lower() == want), None)
        if pick is None and not status_exists:
            pick = next((t for t in found if t.get("name", "").lower() == want), None)
        if pick is None:
            return False
        try:
            self.request("POST", f"/rest/api/2/issue/{_path_segment(key)}/transitions", body={"transition": {"id": pick["id"]}})
        except JiraUnsure as e:
            try:
                status = ((self.issue(key, ["status"]).get("fields") or {}).get("status") or {}).get("name")
            except (JiraError, ValueError, TypeError, KeyError) as lookup:
                raise JiraUnsure(f"{e}; status lookup failed: {lookup}") from lookup
            if not status or status != (pick.get("to") or {}).get("name"):
                raise
        return True


def _read_description(issue: dict) -> dict:
    fields = issue.get("fields")
    if isinstance(fields, dict) and "description" in fields:
        try:
            text = adf.to_text(fields["description"])
        except ValueError as e:
            raise JiraError(f"Jira {issue.get('key', '(unknown issue)')} description could not be read: {e}") from e
        return {**issue, "fields": {**fields, "description": text}}
    return issue


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
