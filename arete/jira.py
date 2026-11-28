from __future__ import annotations

import base64
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from http.client import RemoteDisconnected

from . import accounts, adf, config, outbound, paths, secret_move, secretstore, util

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


def site_url(cfg: config.JiraConfig) -> str:
    base_env = ENV_KEYS[0]
    source = {} if paths.ISOLATED else os.environ
    base = source.get(base_env) or cfg.base_url or _legacy_env(cfg).get(base_env)
    if not base:
        raise JiraError("no Jira site configured for ticket publication")
    return checked_base(base)


def checked_base(value: str) -> str:
    base = value.rstrip("/")
    parts = urllib.parse.urlsplit(base)
    if (parts.scheme not in ("http", "https") or not parts.netloc or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise JiraError("JIRA_BASE_URL must be a full HTTP(S) URL without credentials, query, or fragment")
    return base


def _legacy_env(cfg: config.JiraConfig) -> dict[str, str]:
    legacy = secret_move.fallback(cfg.env_file)
    return util.read_env_file(legacy) if legacy else {}


def credentials(cfg: config.JiraConfig) -> tuple[str, str, str]:
    return credential_source(cfg)[0]


def credential_source(cfg: config.JiraConfig):
    base_env, email_env, token_env = ENV_KEYS
    source = {} if paths.ISOLATED else os.environ
    base = source.get(base_env) or cfg.base_url
    email = source.get(email_env) or cfg.email
    token, row = source.get(token_env, ""), None
    if not token:
        row, token = secretstore.lookup("global", token_env)
    if not base or not email or (not token and row is None):
        old = _legacy_env(cfg)
        base = base or old.get(base_env, "")
        email = email or old.get(email_env, "")
        if row is None:
            token = token or old.get(token_env, "")
    missing = [setting for value, setting in ((base, "[jira] base_url"), (email, "[jira] email"),
               (token, secretstore.address("global", token_env))) if not value]
    if missing:
        raise JiraError(f"missing {', '.join(missing)}; set them in the environment or at the named settings")
    return (checked_base(base), email, token), row


def parse_time(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")


class AuthStaysOnHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_local_if_isolated(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


class _LazyOpener:
    def __init__(self):
        self._inner = None

    def open(self, *args, **kwargs):
        if self._inner is None:
            self._inner = urllib.request.build_opener(AuthStaysOnHost)
        return self._inner.open(*args, **kwargs)


_opener = _LazyOpener()


def require_local_if_isolated(url):
    if paths.ISOLATED and urllib.parse.urlsplit(url).hostname not in ("127.0.0.1", "::1", "localhost"):
        raise JiraError(paths.ISOLATED_LOGINS)


class Client:
    def __init__(self, base_url: str, email: str, token: str):
        self.base = checked_base(base_url)
        self._auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        self._secret_row: dict | None = None
        self._me: dict | None = None
        self._statuses: dict[str, set[str]] = {}

    @classmethod
    def from_env(cls, cfg: config.JiraConfig) -> Client:
        values, row = credential_source(cfg)
        found = cls(*values)
        found._secret_row = row
        return found

    def browse_url(self, key: str) -> str:
        return f"{self.base}/browse/{_path_segment(key)}"

    def request(self, method: str, path: str, params: dict | None = None, body=None, *, single_attempt=False):
        raw = self._send(method, path, params, body, **({"single_attempt": True} if single_attempt else {}))
        return json.loads(raw) if raw.strip() else None

    def attachment(self, attachment_id: str) -> bytes:
        return self._send("GET", f"/rest/api/2/attachment/content/{_path_segment(attachment_id)}", accept="*/*")

    def _send(self, method: str, path: str, params: dict | None = None, body=None,
              accept: str = "application/json", *, single_attempt=False) -> bytes:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise JiraError(f"invalid Jira API path: {path!r}")
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        require_local_if_isolated(url)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self._auth, "Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"
        retries = 0 if single_attempt else RETRIES
        for attempt in range(retries + 1):
            from .store import factory
            factory.check_worker()
            req = util.http_request(url, data=data, method=method, headers=headers)
            try:
                with _opener.open(req, timeout=TIMEOUT) as r:
                    raw = r.read()
                    if self._secret_row and urllib.parse.urlsplit(r.geturl()).netloc == urllib.parse.urlsplit(url).netloc:
                        accounts.record_workspace(self._secret_row, "jira", "api_key")
                        self._secret_row = None
                    return raw
            except urllib.error.HTTPError as e:
                wait = _retry_after(e, attempt)
                if (single_attempt or method == "POST") and e.code >= 500:
                    raise JiraUnsure(f"jira {method} {path}: HTTP {e.code} {_error_text(e)}", e.code) from None
                if (e.code == 429 or e.code >= 500) and attempt < retries and wait <= MAX_RETRY_WAIT:
                    time.sleep(wait)
                    continue
                later = f" (retry after {wait:.0f} s)" if wait > MAX_RETRY_WAIT else ""
                raise JiraError(f"jira {method} {path}: HTTP {e.code} {_error_text(e)}{later}".rstrip(), e.code) from None
            except (urllib.error.URLError, TimeoutError, OSError, RemoteDisconnected) as e:
                never_sent = isinstance(e, urllib.error.URLError) and isinstance(
                    e.reason, (socket.gaierror, ConnectionRefusedError))
                if (single_attempt or method == "POST") and not never_sent:
                    raise JiraUnsure(f"jira {method} {path}: {e}") from None
                if attempt < retries:
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
        for _ in range(1000):
            res = self.get(path, startAt=len(out), maxResults=100, **params)
            if not isinstance(res, dict):
                return out
            vals = res.get(key)
            if not isinstance(vals, list) or not vals:
                return out
            out += vals
            total = res.get("total", len(out))
            if res.get("isLast") or (isinstance(total, int) and len(out) >= total):
                return out
        raise JiraError(f"Jira pagination exceeded maximum pages for {path}")

    def me(self) -> dict:
        if self._me is None:
            self._me = self.get("/rest/api/3/myself") or {}
        return self._me

    def search(self, jql: str, fields: list[str], limit: int = 50, *,
               include: Callable[[dict], bool] | None = None) -> list[dict]:
        out: list[dict] = []
        token = ""
        seen = set()
        while len(out) < limit:
            page_size = min(100, limit - len(out)) if include is None else 100
            params = {"jql": jql, "fields": ",".join(fields), "maxResults": page_size}
            if token:
                params["nextPageToken"] = token
            res = self.get("/rest/api/3/search/jql", **params) or {}
            out.extend(_read_description(issue) for issue in res.get("issues", []) if include is None or include(issue))
            token = res.get("nextPageToken", "")
            if not token or res.get("isLast"):
                break
            if token in seen:
                raise JiraError("Jira search repeated a page token")
            seen.add(token)
        return out[:limit]

    def issue(self, key: str, fields: list[str]) -> dict:
        return _read_description(self.get(f"/rest/api/2/issue/{_path_segment(key)}", fields=",".join(fields)) or {})

    def project_key(self, key: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", key):
            raise ValueError("enter a Jira project key, such as APP")
        row = self.get(f"/rest/api/3/project/{_path_segment(key)}")
        if not isinstance(row, dict) or row.get("key") != key or row.get("archived"):
            raise JiraError(f"Jira did not confirm an active project with key {key}")
        return key

    def subtask_type(self, project: str) -> str:
        types = self._paged(f"/rest/api/3/issue/createmeta/{_path_segment(project)}/issuetypes", key="issueTypes")
        ids = {row.get("id") for row in types if row.get("subtask") is True and row.get("id")}
        if len(ids) != 1:
            raise JiraError(f"{project} needs exactly one available subtask type to publish a child")
        return str(ids.pop())

    def statuses(self, project: str) -> set[str]:
        if project not in self._statuses:
            types = self.get(f"/rest/api/3/project/{_path_segment(project)}/statuses") or []
            self._statuses[project] = {st.get("name", "") for t in types for st in t.get("statuses") or []}
        return self._statuses[project]

    def changelog(self, key: str) -> list[dict]:
        return self._paged(f"/rest/api/2/issue/{_path_segment(key)}/changelog")

    def comments(self, key: str) -> list[dict]:
        return self._paged(f"/rest/api/2/issue/{_path_segment(key)}/comment", key="comments", orderBy="created")

    def find_comment_marker(self, key: str, marker: str, *, author: str = "") -> str:
        for row in self.comments(key):
            if not isinstance(row, dict):
                continue
            if author and ((row.get("author") or {}).get("accountId") or "") != author:
                continue
            body = row.get("body")
            text = body if isinstance(body, str) else ""
            if marker in text.splitlines():
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

    def find_task_marker(self, project: str, account: str, marker: str) -> str:
        # A single alphanumeric word survives Jira's description tokenization.
        if not re.fullmatch(r"areteemit[a-f0-9]{64}", marker):
            raise ValueError("invalid ticket publication marker")
        query = (f"project = {json.dumps(project)} AND creator = {json.dumps(account)} "
                 f'AND description ~ "{marker}"')
        issues = self.search(query, ["description"], limit=1, include=lambda issue:
                             marker in adf.to_text((issue.get("fields") or {}).get("description")).splitlines())
        return self.browse_url(issues[0]["key"]) if issues else ""

    def create_task(self, project: str, account: str, title: str, body: str, labels: list[str], *,
                    parent: str = "", subtask_type: str = "", issue_type: str = "") -> str:
        fields = {"project": {"key": project}, "issuetype": {"name": issue_type or "Task"}, "assignee": {"accountId": account},
                  "summary": outbound.screen(title), "description": adf.from_markdown(outbound.screen(body)),
                  "labels": labels}
        if parent:
            fields.update(parent={"key": parent}, issuetype={"id": subtask_type})
        try:
            result = self.request("POST", "/rest/api/3/issue", body={"fields": fields})
        except JiraError as error:
            if (not parent and issue_type and issue_type.casefold() != "task"
                    and error.status == 400 and "issuetype" in str(error).casefold()):
                fields["issuetype"] = {"name": "Task"}
                result = self.request("POST", "/rest/api/3/issue", body={"fields": fields})
            else:
                raise
        except (ValueError, UnicodeError) as error:
            raise JiraUnsure("Jira returned an unreadable reply after creating the task") from error
        key = result.get("key") if isinstance(result, dict) else None
        if not isinstance(key, str) or not KEY_RE.fullmatch(key) or key.rsplit("-", 1)[0] != project.upper():
            raise JiraUnsure("Jira did not confirm the created task key")
        return self.browse_url(key)

    def issue_links(self, key: str) -> list[dict]:
        fields = (self.issue(key, ["issuelinks"]) or {}).get("fields") or {}
        links = fields.get("issuelinks")
        return links if isinstance(links, list) else []

    def _is_blocks_link(self, kind: dict) -> bool:
        return (str(kind.get("outward") or "").strip().casefold() == "blocks"
                and str(kind.get("inward") or "").strip().casefold() == "is blocked by")

    def _pick_link_type(self, *, blocks: bool = False, preferred: str = "Relates") -> dict:
        found = (self.get("/rest/api/3/issueLinkType") or {}).get("issueLinkTypes") or []
        if blocks:
            for row in found:
                if self._is_blocks_link(row):
                    return {"id": row["id"]} if row.get("id") else {"name": row["name"]}
            raise JiraError("Jira has no Blocks issue link type configured")
        want = preferred.strip().casefold()
        for row in found:
            if not self._is_blocks_link(row) and (
                str(row.get("name") or "").strip().casefold() == want
                or want in str(row.get("inward") or "").casefold()
                or want in str(row.get("outward") or "").casefold()
            ):
                return {"id": row["id"]} if row.get("id") else {"name": row["name"]}
        for row in found:
            if not self._is_blocks_link(row) and (row.get("id") or row.get("name")):
                return {"id": row["id"]} if row.get("id") else {"name": row["name"]}
        return {"name": preferred}

    def link_issues(self, source_key: str, target_key: str, *, preferred_type: str = "Relates") -> bool:
        """Create a non-blocking historical provenance link between source_key and target_key once."""
        target = target_key.upper()
        for link in self.issue_links(source_key):
            if self._is_blocks_link(link.get("type") or {}):
                continue
            inward = str((link.get("inwardIssue") or {}).get("key") or "").upper()
            outward = str((link.get("outwardIssue") or {}).get("key") or "").upper()
            if target in (inward, outward):
                return False
        link_type = self._pick_link_type(blocks=False, preferred=preferred_type)
        self.request(
            "POST",
            "/rest/api/3/issueLink",
            body={"type": link_type, "inwardIssue": {"key": source_key.upper()}, "outwardIssue": {"key": target}},
            single_attempt=True,
        )
        return True

    def link_blocks(self, prerequisite_key: str, dependent_key: str) -> bool:
        """Create a native Jira Blocks dependency link once."""
        prereq, dep = prerequisite_key.upper(), dependent_key.upper()
        for link in self.issue_links(dep):
            if (self._is_blocks_link(link.get("type") or {})
                    and str((link.get("inwardIssue") or {}).get("key") or "").upper() == prereq):
                return False
        link_type = self._pick_link_type(blocks=True)
        self.request(
            "POST",
            "/rest/api/3/issueLink",
            body={"type": link_type, "inwardIssue": {"key": prereq}, "outwardIssue": {"key": dep}},
            single_attempt=True,
        )
        return True

    def edit_labels(self, key: str, add: list[str] = (), remove: list[str] = ()) -> None:
        ops = [{"add": a} for a in add] + [{"remove": r} for r in remove]
        if ops:
            self.request("PUT", f"/rest/api/2/issue/{_path_segment(key)}", body={"update": {"labels": ops}})

    def move(self, key: str, target: str, *, category: str = "") -> bool:
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
        if category:
            destination = ((pick.get("to") or {}).get("statusCategory") or {}).get("key")
            if not destination or (destination == "done") != (category == "done"):
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
        value = float(e.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return float(2**attempt)
    return max(0.0, value) if math.isfinite(value) else float(2**attempt)


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
