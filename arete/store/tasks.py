from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import suppress

from .. import config, events, jira, principals, refusals, util
from ._rows import json_dict, json_list, validate_columns
from . import idempotency, writes
from .db import (
    TASK_IDENTITY_CONTRACT_SCHEMA,
    TASK_IDENTITY_EXPAND_SCHEMA,
    migration_applied,
    reader,
)

TABLES = {
    "tasks": "kept",
    "task_comments": "kept",
    "refs": "kept",
    "ref_fields": "kept",
    "task_refs": "kept",
    "task_links": "kept",
    "task_labels": "kept",
}

LEGACY_TASK_REFUSAL = "task ids are T-<n> now; arete task list shows them"
CANONICAL_TASK_RE = re.compile(r"T-([0-9]+)")
LEGACY_TASK_RE = re.compile(r"t-[0-9a-f]{4,}")
REF_ID_RE = re.compile(r"R-([0-9]+)")
GITHUB_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([0-9]+)",
    re.IGNORECASE,
)
GITHUB_PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]+)",
    re.IGNORECASE,
)
REF_SYSTEMS = frozenset(("github", "jira", "slack", "email", "meeting", "intake"))
REF_KINDS = frozenset(("issue", "ticket", "pr", "thread", "email", "transcript", "paste"))
REF_SYNC_STATES = frozenset(("local", "synced", "ahead", "behind", "conflict", "off"))
TASK_REF_ROLES = frozenset(("primary", "origin", "related", "parent"))
TASK_LINK_KINDS = frozenset(("waits_on", "blocks", "relates", "duplicate"))
TASK_LINK_ORIGINS = frozenset(("person", "agent", "body", "jira_link"))
DEFAULT_REF_KIND_BY_SYSTEM = {
    "github": "issue",
    "jira": "ticket",
    "slack": "thread",
    "email": "email",
    "meeting": "transcript",
    "intake": "paste",
}
WRITABLE_TASK_EXTRA_FIELDS = frozenset((
    "project", "line", "priority", "rank", "owner", "delegate", "estimate", "answer",
))
WRITABLE_TASK_UPDATE_FIELDS = frozenset((
    "title", "body", "kind", "state", *WRITABLE_TASK_EXTRA_FIELDS,
))
TASK_REF_UPDATABLE_FIELDS = frozenset((
    "role", "interpretation", "claims", "anchor", "source_rev", "confidence",
))
COMMENT_UPDATABLE_FIELDS = frozenset(("body", "audience"))
_WAIT_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|#{1,6}[ \t]+)?(?:\*\*|__)?"
    r"(?:waits[ \t]+on|blocked[ \t]+by)\b(?:\*\*|__)?[ \t]*:?[ \t]*(?:\*\*|__)?(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_WAIT_TASK_RE = re.compile(r"(?<![\w-])(?:T-[0-9]+|t-[0-9a-f]{4,})(?![\w-])")


class PrimaryRefConflict(refusals.Conflict):
    code = "invalid_state"

    def __init__(self, message: str, current: dict):
        super().__init__(message)
        self.current = current


class LegacyTaskIdRefused(refusals.Invalid, ValueError):
    pass


def has_identity_expand(c) -> bool:
    return migration_applied(TASK_IDENTITY_EXPAND_SCHEMA, c) or (
        c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'refs'").fetchone() is not None
    )


def is_contracted(c=None) -> bool:
    if c is None:
        try:
            with reader() as conn:
                return is_contracted(conn)
        except Exception:
            return False
    if migration_applied(TASK_IDENTITY_CONTRACT_SCHEMA, c):
        return True
    info = {r[1]: (r[2] or "").upper() for r in c.execute("PRAGMA table_info(tasks)")}
    return info.get("id") == "INTEGER"


def format_task_id(num: int) -> str:
    return f"T-{int(num)}"


def canonical_issue_key(repo, tid: str | int, task_num: int | None = None) -> str:
    raw = str(tid or "").strip()
    if CANONICAL_TASK_RE.fullmatch(raw) or LEGACY_TASK_RE.fullmatch(raw):
        return format_task_id(int(task_num)) if task_num is not None else raw
    if jira.KEY_RE.fullmatch(raw.upper()):
        return raw.upper()
    stripped = raw.lstrip("#")
    if stripped.isascii() and stripped.isdigit() and int(stripped) > 0:
        return str(int(stripped))
    return raw


def format_ref_id(ref_id: int) -> str:
    return f"R-{int(ref_id)}"


def parse_ref_id(value: str | int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raw = str(value or "").strip()
    if m := REF_ID_RE.fullmatch(raw):
        return int(m.group(1))
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def refuse_legacy_id_if_contracted(tid: str | int, conn=None) -> None:
    raw = str(tid or "").strip()
    if LEGACY_TASK_RE.fullmatch(raw) and is_contracted(conn):
        raise LegacyTaskIdRefused(LEGACY_TASK_REFUSAL)


def parse_task_num(tid: str | int, conn=None) -> int | None:
    if isinstance(tid, int) and not isinstance(tid, bool) and tid > 0:
        return tid
    raw = str(tid or "").strip()
    if not raw:
        return None
    if m := CANONICAL_TASK_RE.fullmatch(raw):
        return int(m.group(1))
    if LEGACY_TASK_RE.fullmatch(raw):
        if conn is not None:
            if is_contracted(conn):
                raise LegacyTaskIdRefused(LEGACY_TASK_REFUSAL)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "num" in cols:
                row = conn.execute("SELECT num FROM tasks WHERE id = ?", (raw,)).fetchone()
                return int(row[0]) if row and row[0] is not None else None
            row = conn.execute("SELECT rowid FROM tasks WHERE id = ?", (raw,)).fetchone()
            return int(row[0]) if row else None
        with reader() as c:
            return parse_task_num(raw, c)
    return None


def _extract_wait_tasks(body: str) -> list[str]:
    found = [tid for rest in _WAIT_LINE_RE.findall(body or "") for tid in _WAIT_TASK_RE.findall(rest)]
    return list(dict.fromkeys(found))


def _task_cols(c) -> set[str]:
    return {r[1] for r in c.execute("PRAGMA table_info(tasks)")}


def _comment_cols(c) -> set[str]:
    return {r[1] for r in c.execute("PRAGMA table_info(task_comments)")}


def _db_owner_principal(c) -> str:
    row = c.execute(
        "SELECT id FROM principals WHERE kind = 'human' AND status = 'active' AND role = 'owner' "
        "ORDER BY created, rowid LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else "hand"


def _db_is_person(c, actor: str | None) -> bool:
    if not actor:
        return False
    if actor == "owner":
        return True
    return c.execute("SELECT 1 FROM principals WHERE id = ? AND kind = 'human'", (actor,)).fetchone() is not None


def _resolve_default_owner(c, actor: str | None, *, repo_payer: str = "", cfg=None) -> str:
    if actor and _db_is_person(c, actor):
        return actor if actor != "owner" else _db_owner_principal(c)
    if repo_payer:
        return repo_payer
    if cfg is not None and getattr(cfg, "automation_payer", ""):
        return cfg.automation_payer
    with suppress(OSError, ValueError):
        loaded, _ = config.load_or_problem()
        if loaded and loaded.automation_payer:
            return loaded.automation_payer
    return _db_owner_principal(c)


def _row_to_ref(row) -> dict:
    d = dict(row)
    return {
        "id": format_ref_id(d["id"]),
        "num": int(d["id"]),
        "system": d["system"],
        "kind": d["kind"],
        "key": d["key"],
        "connector": d.get("connector") or "",
        "title": d.get("title") or "",
        "url": d.get("url") or "",
        "ext_state": d.get("ext_state") or "",
        "facts": json_dict(d.get("facts")),
        "run_id": d.get("run_id"),
        "remote_updated": d.get("remote_updated"),
        "remote_actor": d.get("remote_actor"),
        "fetched_at": d.get("fetched_at"),
        "sync": d.get("sync") or "local",
        "version": int(d.get("version") or 1),
        "created": d["created"],
        "updated": d["updated"],
    }


def _row_to_task_ref(row, ref_dict: dict | None = None) -> dict:
    d = dict(row)
    out = {
        "id": int(d["id"]),
        "task": format_task_id(d["task_id"]),
        "task_id": int(d["task_id"]),
        "ref": format_ref_id(d["ref_id"]),
        "ref_id": int(d["ref_id"]),
        "role": d["role"],
        "interpretation": d.get("interpretation") or "",
        "claims": json_dict(d.get("claims")),
        "anchor": json_list(d.get("anchor")),
        "source_rev": d.get("source_rev") or "",
        "linked_by": d.get("linked_by") or "",
        "confidence": d.get("confidence"),
        "version": int(d.get("version") or 1),
        "created": d["created"],
        "updated": d["updated"],
    }
    if ref_dict is not None:
        out["ref_detail"] = ref_dict
        out["system"] = ref_dict["system"]
        out["kind"] = ref_dict["kind"]
        out["key"] = ref_dict["key"]
        out["title"] = ref_dict["title"]
        out["url"] = ref_dict["url"]
    return out


def _row_to_link(row) -> dict:
    d = dict(row)
    other_task = d.get("other_task")
    return {
        "id": int(d["id"]),
        "task": format_task_id(d["task_id"]),
        "task_id": int(d["task_id"]),
        "kind": d["kind"],
        "other_task": format_task_id(other_task) if other_task is not None else None,
        "other_key": d.get("other_key"),
        "origin": d["origin"],
        "created": d["created"],
    }


def _present_task_row(c, row, *, include_relations: bool = True) -> dict:
    d = dict(row)
    contracted = is_contracted(c)
    expand = has_identity_expand(c)
    if contracted:
        num = int(d["id"])
        raw_id = format_task_id(num)
        state = str(d.get("state") or "ready")
        fault = int(d.get("fault") or 0)
        parent = format_task_id(d["parent"]) if isinstance(d.get("parent"), int) else ""
    elif expand and d.get("num") is not None:
        num = int(d["num"])
        raw_id = str(d["id"])
        state, fault = config.canonical_task_state(str(d.get("state") or "todo"), int(d.get("fault") or 0))
        parent = str(d.get("parent") or "")
    else:
        num = int(d["_rowid"]) if "_rowid" in d and d["_rowid"] is not None else None
        raw_id = str(d["id"])
        state = str(d.get("state") or "todo")
        fault = int(d.get("fault") or 0)
        parent = str(d.get("parent") or "")
    display_id = format_task_id(num) if num is not None and (contracted or expand) else raw_id
    out: dict[str, object] = {
        "id": display_id,
        "raw_id": raw_id,
        "num": num,
        "repo": d.get("repo") or "",
        "title": d.get("title") or "",
        "body": d.get("body") or "",
        "kind": d.get("kind") or "feat",
        "state": state,
        "parent": parent,
        "project": d.get("project"),
        "line": d.get("line"),
        "fault": fault,
        "fault_detail": d.get("fault_detail"),
        "priority": d.get("priority"),
        "rank": d.get("rank"),
        "owner": d.get("owner"),
        "origin": d.get("origin"),
        "delegate": d.get("delegate"),
        "estimate": d.get("estimate"),
        "answer": d.get("answer"),
        "version": int(d.get("version") or 1),
        "created": d.get("created") or "",
        "updated": d.get("updated") or "",
    }
    if include_relations and expand and num is not None:
        refs = TaskStore.task_refs(c, num)
        primary_link = next((r for r in refs if r["role"] == "primary"), None)
        out["refs"] = refs
        out["primary_ref"] = primary_link["ref_detail"] if primary_link else None
        out["links"] = TaskStore.task_links(c, num)
    elif include_relations:
        out["refs"] = []
        out["primary_ref"] = None
        out["links"] = []
    return out


def _select_task_row(c, tid: str | int):
    raw = str(tid or "").strip() if not isinstance(tid, int) else tid
    if isinstance(raw, str):
        refuse_legacy_id_if_contracted(raw, c)
    if is_contracted(c):
        num = parse_task_num(raw, c)
        if num is None:
            return None
        return c.execute("SELECT * FROM tasks WHERE id = ?", (num,)).fetchone()
    if has_identity_expand(c):
        num = parse_task_num(raw, c)
        if num is not None:
            row = c.execute("SELECT rowid AS _rowid, * FROM tasks WHERE num = ?", (num,)).fetchone()
            if row is not None:
                return row
        if isinstance(raw, str):
            return c.execute("SELECT rowid AS _rowid, * FROM tasks WHERE id = ?", (raw,)).fetchone()
        return None
    if isinstance(raw, str):
        return c.execute("SELECT rowid AS _rowid, * FROM tasks WHERE id = ?", (raw,)).fetchone()
    return c.execute("SELECT rowid AS _rowid, * FROM tasks WHERE rowid = ?", (raw,)).fetchone()


def _parse_system_key(value: str) -> tuple[str, str] | None:
    if ":" not in value:
        return None
    system, key = value.split(":", 1)
    system, key = system.strip().lower(), key.strip()
    if system in REF_SYSTEMS and key:
        return system, key
    return None


def _run_status_to_task_state(status: str | None) -> tuple[str, int]:
    st = (status or "").strip()
    if st == "merged":
        return "done", 0
    if st in ("rejected", "superseded"):
        return "closed", 0
    if st in ("stopped", "interrupted"):
        return "ready", 0
    if st == "pr":
        return "review", 0
    if st == "running":
        return "running", 0
    if st == "needs-info":
        return "needs-you", 0
    if st == "failed":
        return "needs-you", 1
    return "ready", 0


def _github_repo_for_name(repo_name: str, cfg=None) -> tuple[str, str, str]:
    """Return (github_slug, tracker_name, payer) for a configured repo name."""
    if cfg is None:
        try:
            cfg = config.load()
        except Exception:
            cfg = None
    if cfg is not None:
        for r in getattr(cfg, "repos", ()):
            if r.name == repo_name:
                return r.github or "", r.tracker or "github", r.payer or ""
    return "", "", ""


def _parse_external_ticket(repo_name: str, tid: str | int, *, cfg=None, url: str = "",
                           tracker_hint: str = "") -> tuple[str, str, str, str, str, str]:
    """Return (system, kind, key, issue_key, connector, sync) for a non-local ticket."""
    raw = str(tid or "").strip()
    if m := GITHUB_ISSUE_URL_RE.search(url or ""):
        slug, num = m.group(1), str(int(m.group(2)))
        _, tracker_name, _ = _github_repo_for_name(repo_name, cfg)
        return "github", "issue", f"{slug}#{num}", num, tracker_name or "github", "synced"
    if jira.KEY_RE.fullmatch(raw.upper()):
        key = raw.upper()
        _, tracker_name, _ = _github_repo_for_name(repo_name, cfg)
        return "jira", "ticket", key, key, tracker_name or tracker_hint or "jira", "synced"
    stripped = raw.lstrip("#")
    if stripped.isascii() and stripped.isdigit() and int(stripped) > 0:
        num = str(int(stripped))
        slug, tracker_name, _ = _github_repo_for_name(repo_name, cfg)
        if slug:
            return "github", "issue", f"{slug}#{num}", num, tracker_name or "github", "synced"
        return "github", "issue", f"{repo_name}#{num}", num, tracker_name or "github", "off"
    if "#" in raw:
        left, right = raw.rsplit("#", 1)
        if right.isascii() and right.isdigit() and int(right) > 0:
            num = str(int(right))
            if "/" in left:
                return "github", "issue", f"{left}#{num}", num, tracker_hint or "github", "synced"
            slug, tracker_name, _ = _github_repo_for_name(left or repo_name, cfg)
            if slug:
                return "github", "issue", f"{slug}#{num}", num, tracker_name or "github", "synced"
            return "github", "issue", f"{left or repo_name}#{num}", num, tracker_name or "github", "off"
    key = raw.upper() if tracker_hint == "jira" else f"{repo_name}#{raw}"
    return (
        ("jira", "ticket", key, key, "jira", "synced")
        if tracker_hint == "jira"
        else ("github", "issue", key, raw, tracker_hint or "github", "off")
    )


class TaskStore:
    """Transaction-aware task, ref, task_ref, and task_link primitives.

    Methods taking ``conn`` reuse the caller's open transaction without committing
    or opening an independent write transaction.
    """

    @staticmethod
    def _next_task_num(conn) -> int:
        if is_contracted(conn):
            return int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM tasks").fetchone()[0])
        if has_identity_expand(conn):
            return int(conn.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM tasks").fetchone()[0])
        return int(conn.execute("SELECT COALESCE(MAX(rowid), 0) + 1 FROM tasks").fetchone()[0])

    @staticmethod
    def create(conn, values: dict, actor: str, *, emit_event: bool = True,
               event_kind: str = "task.created") -> dict:
        from . import factory

        factory.admit(conn)
        now = util.iso()
        contracted = is_contracted(conn)
        expand = has_identity_expand(conn)
        body = str(values.get("body") or "")
        title = str(values.get("title") or "")
        repo = str(values.get("repo") or "")
        kind = str(values.get("kind") or "feat")
        raw_state = str(values.get("state") or ("ready" if contracted else "todo"))
        raw_fault = int(values.get("fault") or 0)
        canonical_state, fault = config.canonical_task_state(raw_state, raw_fault)
        stored_state = canonical_state if contracted else config.legacy_task_state(canonical_state, fault)
        owner = values.get("owner")
        if not owner:
            owner = _resolve_default_owner(conn, actor)
        origin = values.get("origin")
        if not origin:
            if actor and _db_is_person(conn, actor):
                origin = actor if actor != "owner" else _db_owner_principal(conn)
            else:
                origin = owner if str(owner).startswith("u:") else f"u:{owner}"
        created = str(values.get("created") or now)
        updated = str(values.get("updated") or created)
        raw_parent = values.get("parent")
        parent_num: int | None = None
        if isinstance(raw_parent, int) and not isinstance(raw_parent, bool):
            parent_num = raw_parent
        elif isinstance(raw_parent, str) and raw_parent.strip():
            parent_num = parse_task_num(raw_parent.strip(), conn)

        if contracted:
            reserved_id = str(values.get("reserved_id") or "").strip()
            if reserved_id:
                num = parse_task_num(reserved_id, conn)
                if num is None:
                    num = TaskStore._next_task_num(conn)
            else:
                num = TaskStore._next_task_num(conn)
            row_values = {
                "id": num,
                "repo": repo,
                "title": title,
                "body": body,
                "kind": kind,
                "state": stored_state,
                "parent": parent_num,
                "project": values.get("project"),
                "line": values.get("line"),
                "fault": fault,
                "fault_detail": values.get("fault_detail"),
                "priority": values.get("priority"),
                "rank": values.get("rank"),
                "owner": owner,
                "origin": origin,
                "delegate": values.get("delegate"),
                "estimate": values.get("estimate"),
                "answer": values.get("answer"),
                "version": 1,
                "created": created,
                "updated": updated,
            }
            conn.execute(
                "INSERT INTO tasks (id, repo, title, body, kind, state, parent, project, line, fault, fault_detail, "
                "priority, rank, owner, origin, delegate, estimate, answer, version, created, updated) "
                "VALUES (:id, :repo, :title, :body, :kind, :state, :parent, :project, :line, :fault, :fault_detail, "
                ":priority, :rank, :owner, :origin, :delegate, :estimate, :answer, :version, :created, :updated)",
                row_values,
            )
            saved = conn.execute("SELECT * FROM tasks WHERE id = ?", (num,)).fetchone()
        elif expand:
            num = TaskStore._next_task_num(conn)
            reserved_id = str(values.get("reserved_id") or "").strip()
            tid = reserved_id or format_task_id(num)
            row_values = {
                "id": tid,
                "num": num,
                "repo": repo,
                "title": title,
                "body": body,
                "kind": kind,
                "state": stored_state,
                "parent": str(raw_parent or ""),
                "project": values.get("project"),
                "line": values.get("line"),
                "fault": fault,
                "fault_detail": values.get("fault_detail"),
                "priority": values.get("priority"),
                "rank": values.get("rank"),
                "owner": owner,
                "origin": origin,
                "delegate": values.get("delegate"),
                "estimate": values.get("estimate"),
                "answer": values.get("answer"),
                "version": 1,
                "created": created,
                "updated": updated,
            }
            conn.execute(
                "INSERT INTO tasks (id, num, repo, title, body, kind, state, parent, project, line, fault, "
                "fault_detail, priority, rank, owner, origin, delegate, estimate, answer, version, created, updated) "
                "VALUES (:id, :num, :repo, :title, :body, :kind, :state, :parent, :project, :line, :fault, "
                ":fault_detail, :priority, :rank, :owner, :origin, :delegate, :estimate, :answer, :version, "
                ":created, :updated)",
                row_values,
            )
            saved = conn.execute("SELECT rowid AS _rowid, * FROM tasks WHERE num = ?", (num,)).fetchone()
        else:
            reserved_id = str(values.get("reserved_id") or "").strip()
            digits = 4
            while True:
                tid = reserved_id or f"t-{secrets.randbits(4 * digits):0{digits}x}"
                try:
                    conn.execute(
                        "INSERT INTO tasks (id, repo, title, body, kind, state, parent, created, updated) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (tid, repo, title, body, kind, stored_state, str(raw_parent or ""), created, updated),
                    )
                    break
                except sqlite3.IntegrityError as e:
                    if reserved_id or str(e) != "UNIQUE constraint failed: tasks.id":
                        raise
                    digits += 1
            saved = conn.execute("SELECT rowid AS _rowid, * FROM tasks WHERE id = ?", (tid,)).fetchone()
            num = int(saved["_rowid"])

        if expand and body:
            TaskStore.sync_body_links(conn, num, body)

        presented = _present_task_row(conn, saved)
        if emit_event:
            event_actor = actor if actor and actor != "owner" else _db_owner_principal(conn)
            with principals.acting(event_actor):
                events.emit(
                    event_kind,
                    f"task:{num}" if (contracted or expand) else f"task:{presented['raw_id']}",
                    cls="derived",
                    ver=presented["version"],
                    task=presented["id"],
                    data={k: v for k, v in presented.items() if k not in ("raw_id", "num", "refs", "primary_ref", "links")},
                    connection=conn,
                )
        return presented

    @staticmethod
    def ensure_ref(conn, system: str, key: str, *, kind: str | None = None, connector: str = "",
                   title: str = "", url: str = "", ext_state: str = "", facts: dict | None = None,
                   run_id: str | None = None, remote_updated: str | None = None,
                   fetched_at: str | None = None, sync: str = "local",
                   actor: str = "hand", emit_event: bool = True) -> tuple[dict, bool]:
        sys_norm = system.strip().lower()
        key_norm = key.strip()
        if sys_norm not in REF_SYSTEMS:
            raise ValueError(f"unsupported ref system {system!r}")
        if not key_norm:
            raise ValueError("ref key is required")
        existing = conn.execute(
            "SELECT * FROM refs WHERE system = ? AND key = ?", (sys_norm, key_norm)
        ).fetchone()
        if existing is not None:
            return _row_to_ref(existing), False
        ref_kind = (kind or DEFAULT_REF_KIND_BY_SYSTEM.get(sys_norm, "issue")).strip().lower()
        if ref_kind not in REF_KINDS:
            raise ValueError(f"unsupported ref kind {ref_kind!r}")
        sync_norm = sync if sync in REF_SYNC_STATES else "local"
        now = util.iso()
        cur = conn.execute(
            "INSERT INTO refs (system, kind, key, connector, title, url, ext_state, facts, run_id, "
            "remote_updated, fetched_at, sync, version, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (sys_norm, ref_kind, key_norm, connector or "", title or "", url or "", ext_state or "",
             json.dumps(facts or {}), run_id, remote_updated, fetched_at, sync_norm, now, now),
        )
        saved = _row_to_ref(conn.execute("SELECT * FROM refs WHERE id = ?", (cur.lastrowid,)).fetchone())
        if emit_event:
            event_actor = actor if actor and actor != "owner" else _db_owner_principal(conn)
            with principals.acting(event_actor):
                events.emit(
                    "ref.changed",
                    f"ref:{saved['num']}",
                    cls="durable",
                    ver=saved["version"],
                    data={k: v for k, v in saved.items() if k != "num"},
                    connection=conn,
                )
        return saved, True

    @staticmethod
    def resolve_ref_row(conn, ref: str | int | dict, *, actor: str = "hand",
                        create_if_missing: bool = False, emit_event: bool = True) -> tuple[dict | None, bool]:
        if isinstance(ref, dict):
            if ref.get("ref"):
                raw = str(ref["ref"]).strip()
                if (rid := parse_ref_id(raw)) is not None:
                    row = conn.execute("SELECT * FROM refs WHERE id = ?", (rid,)).fetchone()
                    return (_row_to_ref(row), False) if row else (None, False)
                parsed = _parse_system_key(raw)
                if parsed is None:
                    raise ValueError(f"invalid ref identifier {raw!r}; use R-<id> or <system>:<key>")
                system, key = parsed
            else:
                system = str(ref.get("system") or "").strip().lower()
                key = str(ref.get("key") or "").strip()
                if not system or not key:
                    raise ValueError("give ref (R-<id> or <system>:<key>) or both system and key")
            if create_if_missing:
                return TaskStore.ensure_ref(
                    conn,
                    system,
                    key,
                    kind=ref.get("kind"),
                    connector=str(ref.get("connector") or ""),
                    title=str(ref.get("title") or ""),
                    url=str(ref.get("url") or ""),
                    ext_state=str(ref.get("ext_state") or ""),
                    facts=ref.get("facts") if isinstance(ref.get("facts"), dict) else None,
                    run_id=ref.get("run_id"),
                    sync=str(ref.get("sync") or "local"),
                    actor=actor,
                    emit_event=emit_event,
                )
            row = conn.execute("SELECT * FROM refs WHERE system = ? AND key = ?", (system, key)).fetchone()
            return (_row_to_ref(row), False) if row else (None, False)

        if (rid := parse_ref_id(ref)) is not None:
            row = conn.execute("SELECT * FROM refs WHERE id = ?", (rid,)).fetchone()
            return (_row_to_ref(row), False) if row else (None, False)
        raw = str(ref or "").strip()
        if parsed := _parse_system_key(raw):
            system, key = parsed
            if create_if_missing:
                return TaskStore.ensure_ref(conn, system, key, actor=actor, emit_event=emit_event)
            row = conn.execute("SELECT * FROM refs WHERE system = ? AND key = ?", (system, key)).fetchone()
            return (_row_to_ref(row), False) if row else (None, False)
        return None, False

    @staticmethod
    def link_ref(conn, task: str | int, ref: str | int | dict, values: dict, actor: str,
                 *, emit_event: bool = True) -> dict:
        task_num = parse_task_num(task, conn)
        if task_num is None or _select_task_row(conn, task_num) is None:
            raise LookupError(f"no task {task}")
        ref_dict, _ = TaskStore.resolve_ref_row(conn, ref, actor=actor, create_if_missing=True, emit_event=emit_event)
        if ref_dict is None:
            raise LookupError(f"no ref {ref}")
        ref_id = ref_dict["num"]
        role = str(values.get("role") or "related").strip()
        if role not in TASK_REF_ROLES:
            raise ValueError(f"unsupported task_ref role {role!r}")
        now = util.iso()
        event_actor = actor if actor and actor != "owner" else _db_owner_principal(conn)

        if role == "primary":
            other = conn.execute(
                "SELECT * FROM task_refs WHERE ref_id = ? AND role = 'primary' AND task_id != ?",
                (ref_id, task_num),
            ).fetchone()
            if other is not None:
                raise PrimaryRefConflict(
                    f"Ref {ref_dict['id']} is already primary on {format_task_id(other['task_id'])}",
                    _row_to_task_ref(other, ref_dict),
                )
            prev_primary = conn.execute(
                "SELECT * FROM task_refs WHERE task_id = ? AND role = 'primary' AND ref_id != ?",
                (task_num, ref_id),
            ).fetchone()
            if prev_primary is not None:
                conn.execute(
                    "UPDATE task_refs SET role = 'related', version = version + 1, updated = ? WHERE id = ?",
                    (now, prev_primary["id"]),
                )
                if emit_event:
                    demoted = conn.execute("SELECT * FROM task_refs WHERE id = ?", (prev_primary["id"],)).fetchone()
                    demoted_ref = _row_to_ref(conn.execute("SELECT * FROM refs WHERE id = ?", (prev_primary["ref_id"],)).fetchone())
                    with principals.acting(event_actor):
                        events.emit(
                            "task_ref.changed",
                            f"task_ref:{demoted['id']}",
                            cls="durable",
                            ver=demoted["version"],
                            task=format_task_id(task_num),
                            data=_row_to_task_ref(demoted, demoted_ref),
                            connection=conn,
                        )

        existing = conn.execute(
            "SELECT * FROM task_refs WHERE task_id = ? AND ref_id = ?", (task_num, ref_id)
        ).fetchone()
        interpretation = str(values.get("interpretation") or "")
        claims_json = json.dumps(values.get("claims") if isinstance(values.get("claims"), dict) else {})
        anchor_json = json.dumps(values.get("anchor") if isinstance(values.get("anchor"), list) else [])
        source_rev = str(values.get("source_rev") or "")
        linked_by = str(values.get("linked_by") or event_actor)
        confidence = values.get("confidence")

        if existing is not None:
            conn.execute(
                "UPDATE task_refs SET role = ?, interpretation = ?, claims = ?, anchor = ?, source_rev = ?, "
                "linked_by = ?, confidence = ?, version = version + 1, updated = ? WHERE id = ?",
                (role, interpretation, claims_json, anchor_json, source_rev, linked_by, confidence, now, existing["id"]),
            )
            link_id = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO task_refs (task_id, ref_id, role, interpretation, claims, anchor, source_rev, "
                "linked_by, confidence, version, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (task_num, ref_id, role, interpretation, claims_json, anchor_json, source_rev, linked_by,
                 confidence, now, now),
            )
            link_id = int(cur.lastrowid)

        saved_row = conn.execute("SELECT * FROM task_refs WHERE id = ?", (link_id,)).fetchone()
        saved = _row_to_task_ref(saved_row, ref_dict)
        if emit_event:
            with principals.acting(event_actor):
                events.emit(
                    "task_ref.changed",
                    f"task_ref:{link_id}",
                    cls="durable",
                    ver=saved["version"],
                    task=format_task_id(task_num),
                    data=saved,
                    connection=conn,
                )
        if role == "primary" and ref_dict.get("fetched_at"):
            from .. import sync as sync_mod

            sync_mod.apply_primary_remote_fields(conn, task_num, ref_id, actor=event_actor)
        return saved

    @staticmethod
    def update_ref_link(conn, task: str | int, ref: str | int, values: dict, actor: str,
                        *, base_version: int | None = None) -> dict:
        task_num = parse_task_num(task, conn)
        if task_num is None or _select_task_row(conn, task_num) is None:
            raise LookupError(f"no task {task}")
        ref_dict, _ = TaskStore.resolve_ref_row(conn, ref, actor=actor, create_if_missing=False)
        if ref_dict is None:
            raise LookupError(f"no ref {ref}")
        ref_id = ref_dict["num"]
        existing = conn.execute(
            "SELECT * FROM task_refs WHERE task_id = ? AND ref_id = ?", (task_num, ref_id)
        ).fetchone()
        if existing is None:
            raise LookupError(f"no link between {format_task_id(task_num)} and {ref_dict['id']}")
        if base_version is not None and int(existing["version"]) != base_version:
            raise writes.VersionConflict(_row_to_task_ref(existing, ref_dict))
        fields = {k: v for k, v in values.items() if k in TASK_REF_UPDATABLE_FIELDS}
        if not fields:
            raise ValueError("send at least one editable task_ref field")
        role = str(fields.get("role", existing["role"]))
        if role not in TASK_REF_ROLES:
            raise ValueError(f"unsupported task_ref role {role!r}")
        now = util.iso()
        event_actor = actor if actor and actor != "owner" else _db_owner_principal(conn)

        if role == "primary" and existing["role"] != "primary":
            other = conn.execute(
                "SELECT * FROM task_refs WHERE ref_id = ? AND role = 'primary' AND task_id != ?",
                (ref_id, task_num),
            ).fetchone()
            if other is not None:
                raise PrimaryRefConflict(
                    f"Ref {ref_dict['id']} is already primary on {format_task_id(other['task_id'])}",
                    _row_to_task_ref(other, ref_dict),
                )
            prev_primary = conn.execute(
                "SELECT * FROM task_refs WHERE task_id = ? AND role = 'primary' AND ref_id != ?",
                (task_num, ref_id),
            ).fetchone()
            if prev_primary is not None:
                conn.execute(
                    "UPDATE task_refs SET role = 'related', version = version + 1, updated = ? WHERE id = ?",
                    (now, prev_primary["id"]),
                )
                demoted = conn.execute("SELECT * FROM task_refs WHERE id = ?", (prev_primary["id"],)).fetchone()
                demoted_ref = _row_to_ref(conn.execute("SELECT * FROM refs WHERE id = ?", (prev_primary["ref_id"],)).fetchone())
                with principals.acting(event_actor):
                    events.emit(
                        "task_ref.changed",
                        f"task_ref:{demoted['id']}",
                        cls="durable",
                        ver=demoted["version"],
                        task=format_task_id(task_num),
                        data=_row_to_task_ref(demoted, demoted_ref),
                        connection=conn,
                    )

        updates: dict[str, object] = {"updated": now}
        if "role" in fields:
            updates["role"] = role
        if "interpretation" in fields:
            updates["interpretation"] = str(fields["interpretation"] or "")
        if "claims" in fields:
            updates["claims"] = json.dumps(fields["claims"] if isinstance(fields["claims"], dict) else {})
        if "anchor" in fields:
            updates["anchor"] = json.dumps(fields["anchor"] if isinstance(fields["anchor"], list) else [])
        if "source_rev" in fields:
            updates["source_rev"] = str(fields["source_rev"] or "")
        if "confidence" in fields:
            updates["confidence"] = fields["confidence"]
        validate_columns(updates, TASK_REF_UPDATABLE_FIELDS | {"updated"}, "task_refs")
        setters = ", ".join(f"{col} = ?" for col in updates)
        conn.execute(
            f"UPDATE task_refs SET {setters}, version = version + 1 WHERE id = ?",  # noqa: S608
            (*updates.values(), existing["id"]),
        )
        saved_row = conn.execute("SELECT * FROM task_refs WHERE id = ?", (existing["id"],)).fetchone()
        saved = _row_to_task_ref(saved_row, ref_dict)
        if role == "primary" and existing["role"] != "primary" and ref_dict.get("fetched_at"):
            from .. import sync as sync_mod

            sync_mod.apply_primary_remote_fields(conn, task_num, ref_id, actor=event_actor)
        with principals.acting(event_actor):
            events.emit(
                "task_ref.changed",
                f"task_ref:{existing['id']}",
                cls="durable",
                ver=saved["version"],
                task=format_task_id(task_num),
                data=saved,
                connection=conn,
            )
        return saved

    @staticmethod
    def unlink_ref(conn, task: str | int, ref: str | int, actor: str) -> tuple[bool, int, dict]:
        task_num = parse_task_num(task, conn)
        if task_num is None or _select_task_row(conn, task_num) is None:
            raise LookupError(f"no task {task}")
        ref_dict, _ = TaskStore.resolve_ref_row(conn, ref, actor=actor, create_if_missing=False)
        if ref_dict is None:
            raise LookupError(f"no ref {ref}")
        existing = conn.execute(
            "SELECT * FROM task_refs WHERE task_id = ? AND ref_id = ?", (task_num, ref_dict["num"])
        ).fetchone()
        if existing is None:
            raise LookupError(f"no link between {format_task_id(task_num)} and {ref_dict['id']}")
        conn.execute("DELETE FROM task_refs WHERE id = ?", (existing["id"],))
        event_actor = actor if actor and actor != "owner" else _db_owner_principal(conn)
        with principals.acting(event_actor):
            events.emit(
                "task_ref.changed",
                f"task_ref:{existing['id']}",
                cls="durable",
                ver=int(existing["version"]) + 1,
                task=format_task_id(task_num),
                data={"unlinked": True, **_row_to_task_ref(existing, ref_dict)},
                connection=conn,
            )
        return True, task_num, ref_dict

    @staticmethod
    def find_task_num(conn, repo, tid: str | int, *, cfg=None, url: str = "") -> int | None:
        if not has_identity_expand(conn):
            return None
        repo_name = repo.name if hasattr(repo, "name") else str(repo or "")
        raw = str(tid or "").strip()
        if not raw:
            return None
        if CANONICAL_TASK_RE.fullmatch(raw) or LEGACY_TASK_RE.fullmatch(raw):
            if LEGACY_TASK_RE.fullmatch(raw) and is_contracted(conn):
                return None
            return parse_task_num(raw, conn)
        if hasattr(repo, "github") and repo.github and not (jira.KEY_RE.fullmatch(raw.upper())):
            stripped = raw.lstrip("#")
            if stripped.isascii() and stripped.isdigit() and int(stripped) > 0:
                system, key = "github", f"{repo.github}#{int(stripped)}"
            else:
                system, _, key, _, _, _ = _parse_external_ticket(repo_name, raw, cfg=cfg, url=url)
        else:
            system, _, key, _, _, _ = _parse_external_ticket(repo_name, raw, cfg=cfg, url=url)
        row = conn.execute(
            "SELECT tr.task_id FROM task_refs tr JOIN refs r ON r.id = tr.ref_id "
            "WHERE r.system = ? AND r.key = ? AND tr.role = 'primary' LIMIT 1",
            (system, key),
        ).fetchone()
        if row is not None:
            return int(row[0])
        if system == "github" and repo_name and key != f"{repo_name}#{raw.lstrip('#')}":
            fallback = conn.execute(
                "SELECT tr.task_id FROM task_refs tr JOIN refs r ON r.id = tr.ref_id "
                "WHERE r.system = 'github' AND r.key = ? AND tr.role = 'primary' LIMIT 1",
                (f"{repo_name}#{raw.lstrip('#')}",),
            ).fetchone()
            if fallback is not None:
                return int(fallback[0])
        if repo_name:
            from . import runs as run_store

            if run_store.has_issue_column(conn):
                run_row = conn.execute(
                    "SELECT task_id FROM runs WHERE repo = ? AND (issue_key = ? OR issue = ?) AND task_id IS NOT NULL "
                    "ORDER BY started DESC, rowid DESC LIMIT 1",
                    (repo_name, raw.lstrip("#"), raw),
                ).fetchone()
            else:
                run_row = conn.execute(
                    "SELECT task_id FROM runs WHERE repo = ? AND issue_key = ? AND task_id IS NOT NULL "
                    "ORDER BY started DESC, rowid DESC LIMIT 1",
                    (repo_name, raw.lstrip("#")),
                ).fetchone()
            if run_row is not None:
                return int(run_row[0])
        return None

    @staticmethod
    def task_for_ticket(conn, repo, tid: str | int, actor: str, *, cfg=None, title: str = "",
                        url: str = "", connector: str = "") -> tuple[int, str]:
        """Resolve or create the canonical task and ref on the caller's open tx().

        Returns ``(task_num, issue_key)``.
        """
        repo_name = repo.name if hasattr(repo, "name") else str(repo or "")
        repo_github = getattr(repo, "github", "") if hasattr(repo, "github") else ""
        repo_tracker = getattr(repo, "tracker", "") if hasattr(repo, "tracker") else ""
        repo_payer = getattr(repo, "payer", "") if hasattr(repo, "payer") else ""
        raw = str(tid or "").strip()
        refuse_legacy_id_if_contracted(raw, conn)
        if CANONICAL_TASK_RE.fullmatch(raw) or LEGACY_TASK_RE.fullmatch(raw):
            row = _select_task_row(conn, raw)
            if row is None:
                raise LookupError(f"no task {raw}")
            num = int(row["id"]) if is_contracted(conn) else int(row["num"])
            return num, format_task_id(num)

        if jira.KEY_RE.fullmatch(raw.upper()):
            system, kind, key, issue_key = "jira", "ticket", raw.upper(), raw.upper()
            conn_name = connector or repo_tracker or "jira"
            sync = "synced"
        else:
            stripped = raw.lstrip("#")
            if repo_github and stripped.isascii() and stripped.isdigit() and int(stripped) > 0:
                issue_key = str(int(stripped))
                system, kind, key = "github", "issue", f"{repo_github}#{issue_key}"
                conn_name = connector or repo_tracker or "github"
                sync = "synced"
            else:
                system, kind, key, issue_key, conn_name, sync = _parse_external_ticket(
                    repo_name, raw, cfg=cfg, url=url, tracker_hint=connector or repo_tracker,
                )

        if not repo_payer and repo_name:
            _, _, repo_payer = _github_repo_for_name(repo_name, cfg)

        ref_dict, _ = TaskStore.ensure_ref(
            conn, system, key, kind=kind, connector=conn_name, title=title, url=url,
            sync=sync, actor=actor, emit_event=True,
        )
        primary = conn.execute(
            "SELECT task_id FROM task_refs WHERE ref_id = ? AND role = 'primary' LIMIT 1",
            (ref_dict["num"],),
        ).fetchone()
        if primary is not None:
            return int(primary[0]), issue_key
        if system == "github" and repo_name and key != f"{repo_name}#{issue_key}":
            fallback_primary = conn.execute(
                "SELECT tr.task_id FROM task_refs tr JOIN refs r ON r.id = tr.ref_id "
                "WHERE r.system = 'github' AND r.key = ? AND tr.role = 'primary' LIMIT 1",
                (f"{repo_name}#{issue_key}",),
            ).fetchone()
            if fallback_primary is not None:
                return int(fallback_primary[0]), issue_key

        owner = _resolve_default_owner(conn, actor, repo_payer=repo_payer, cfg=cfg)
        created = TaskStore.create(
            conn,
            {
                "repo": repo_name,
                "title": title or ref_dict["title"] or key,
                "body": "",
                "kind": "feat",
                "state": "ready",
                "owner": owner,
                "origin": f"ref:{system}:{key}",
            },
            actor,
            emit_event=True,
            event_kind="task.created",
        )
        task_num = int(created["num"])
        TaskStore.link_ref(
            conn,
            task_num,
            ref_dict["num"],
            {"role": "primary", "linked_by": actor or "hand"},
            actor or "hand",
            emit_event=True,
        )
        return task_num, issue_key

    @staticmethod
    def ref_for_triage(conn, row: dict, *, cfg=None, actor: str = "hand",
                       emit_event: bool = True) -> int | None:
        if not has_identity_expand(conn):
            return None
        raw_ref = str(row.get("ref") or "").strip()
        if not raw_ref:
            return None
        repo_name = str(row.get("repo") or "").strip()
        url = str(row.get("url") or "").strip()
        tracker_hint = str(row.get("tracker") or "").strip().lower()
        title = str(row.get("title") or "").strip()
        system, kind, key, _, conn_name, sync = _parse_external_ticket(
            repo_name, raw_ref, cfg=cfg, url=url, tracker_hint=tracker_hint,
        )
        ref_dict, _ = TaskStore.ensure_ref(
            conn, system, key, kind=kind, connector=conn_name, title=title, url=url,
            remote_updated=row.get("updated") or None, sync=sync, actor=actor, emit_event=emit_event,
        )
        return int(ref_dict["num"])

    @staticmethod
    def sync_body_links(conn, task_num: int, body: str) -> None:
        if not has_identity_expand(conn):
            return
        conn.execute("DELETE FROM task_links WHERE task_id = ? AND kind = 'waits_on' AND origin = 'body'",
                     (task_num,))
        now = util.iso()
        seen: set[tuple[int | None, str | None]] = set()
        for target in _extract_wait_tasks(body):
            other_num: int | None = None
            other_key: str | None = None
            if m := CANONICAL_TASK_RE.fullmatch(target):
                candidate = int(m.group(1))
                if _select_task_row(conn, candidate) is not None:
                    other_num = candidate
                else:
                    other_key = target
            elif LEGACY_TASK_RE.fullmatch(target) and not is_contracted(conn):
                other_num = parse_task_num(target, conn)
                if other_num is None:
                    other_key = target
            else:
                other_key = target
            if other_num == task_num:
                continue
            pair = (other_num, other_key)
            if pair in seen:
                continue
            seen.add(pair)
            conn.execute(
                "INSERT INTO task_links (task_id, kind, other_task, other_key, origin, created) "
                "VALUES (?, 'waits_on', ?, ?, 'body', ?)",
                (task_num, other_num, other_key, now),
            )

    @staticmethod
    def create_link(conn, task: str | int, kind: str, *, other_task: str | int | None = None,
                    other_key: str | None = None, origin: str = "person") -> dict:
        task_num = parse_task_num(task, conn)
        if task_num is None or _select_task_row(conn, task_num) is None:
            raise LookupError(f"no task {task}")
        if kind not in TASK_LINK_KINDS:
            raise ValueError(f"unsupported link kind {kind!r}")
        if origin not in TASK_LINK_ORIGINS:
            raise ValueError(f"unsupported link origin {origin!r}")
        has_task = other_task is not None and str(other_task).strip() != ""
        has_key = other_key is not None and str(other_key).strip() != ""
        if has_task == has_key:
            raise ValueError("give exactly one of other_task or other_key")
        other_num: int | None = None
        norm_key: str | None = None
        if has_task:
            other_num = parse_task_num(other_task, conn)
            if other_num is None or _select_task_row(conn, other_num) is None:
                raise LookupError(f"no task {other_task}")
            if other_num == task_num:
                raise ValueError("a task cannot link to itself")
        else:
            norm_key = str(other_key).strip()
        cur = conn.execute(
            "INSERT INTO task_links (task_id, kind, other_task, other_key, origin, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_num, kind, other_num, norm_key, origin, util.iso()),
        )
        return _row_to_link(conn.execute("SELECT * FROM task_links WHERE id = ?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def delete_link(conn, task: str | int, link_id: int) -> bool:
        task_num = parse_task_num(task, conn)
        if task_num is None or _select_task_row(conn, task_num) is None:
            raise LookupError(f"no task {task}")
        return conn.execute(
            "DELETE FROM task_links WHERE id = ? AND task_id = ?", (int(link_id), task_num)
        ).rowcount > 0

    @staticmethod
    def task_links(conn, task_num: int, kind: str | None = None) -> list[dict]:
        if not has_identity_expand(conn):
            return []
        if kind:
            rows = conn.execute(
                "SELECT * FROM task_links WHERE task_id = ? AND kind = ? ORDER BY id", (task_num, kind)
            )
        else:
            rows = conn.execute("SELECT * FROM task_links WHERE task_id = ? ORDER BY id", (task_num,))
        return [_row_to_link(r) for r in rows]

    @staticmethod
    def task_refs(conn, task_num: int) -> list[dict]:
        if not has_identity_expand(conn):
            return []
        rows = conn.execute(
            "SELECT tr.*, r.id AS _r_id, r.system AS _r_system, r.kind AS _r_kind, r.key AS _r_key, "
            "r.connector AS _r_connector, r.title AS _r_title, r.url AS _r_url, r.ext_state AS _r_ext_state, "
            "r.facts AS _r_facts, r.run_id AS _r_run_id, r.remote_updated AS _r_remote_updated, "
            "r.fetched_at AS _r_fetched_at, r.sync AS _r_sync, r.version AS _r_version, "
            "r.created AS _r_created, r.updated AS _r_updated "
            "FROM task_refs tr JOIN refs r ON r.id = tr.ref_id "
            "WHERE tr.task_id = ? ORDER BY (tr.role = 'primary') DESC, tr.id",
            (task_num,),
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            ref_dict = _row_to_ref({
                "id": d["_r_id"], "system": d["_r_system"], "kind": d["_r_kind"], "key": d["_r_key"],
                "connector": d["_r_connector"], "title": d["_r_title"], "url": d["_r_url"],
                "ext_state": d["_r_ext_state"], "facts": d["_r_facts"], "run_id": d["_r_run_id"],
                "remote_updated": d["_r_remote_updated"], "fetched_at": d["_r_fetched_at"],
                "sync": d["_r_sync"], "version": d["_r_version"], "created": d["_r_created"],
                "updated": d["_r_updated"],
            })
            out.append(_row_to_task_ref(d, ref_dict))
        return out

    @staticmethod
    def ref_get(conn, ref: str | int) -> dict | None:
        if not has_identity_expand(conn):
            return None
        ref_dict, _ = TaskStore.resolve_ref_row(conn, ref, create_if_missing=False)
        if ref_dict is None:
            return None
        from .. import sync as sync_mod

        ref_dict["fields"] = sync_mod.ref_fields_for(conn, ref_dict["num"])
        return ref_dict

    @staticmethod
    def ref_list(conn, *, system: str | None = None, kind: str | None = None,
                 key: str | None = None, sync: str | None = None) -> list[dict]:
        if not has_identity_expand(conn):
            return []
        clauses, args = [], []
        for col, val in (("system", system), ("kind", kind), ("key", key), ("sync", sync)):
            if val:
                clauses.append(f"{col} = ?")
                args.append(val)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return [_row_to_ref(r) for r in conn.execute(f"SELECT * FROM refs{where} ORDER BY id", args)]  # noqa: S608

    @staticmethod
    def ref_tasks(conn, ref_id: int) -> list[dict]:
        if not has_identity_expand(conn):
            return []
        ref_row = conn.execute("SELECT * FROM refs WHERE id = ?", (ref_id,)).fetchone()
        if ref_row is None:
            return []
        ref_dict = _row_to_ref(ref_row)
        links = conn.execute(
            "SELECT * FROM task_refs WHERE ref_id = ? ORDER BY (role = 'primary') DESC, id", (ref_id,)
        ).fetchall()
        out = []
        for link in links:
            task_row = _select_task_row(conn, int(link["task_id"]))
            presented = _present_task_row(conn, task_row, include_relations=False) if task_row is not None else None
            out.append({
                **_row_to_task_ref(link, ref_dict),
                "task_title": presented["title"] if presented else "",
                "task_state": presented["state"] if presented else "",
                "task_repo": presented["repo"] if presented else "",
            })
        return out

    @staticmethod
    def _event_rows(conn, rows) -> list[dict]:
        epoch_row = conn.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()
        epoch = str(epoch_row[0]) if epoch_row else "0"
        out = []
        for r in rows:
            d = dict(r)
            out.append({
                "id": f"{epoch}.{d['seq']}",
                "seq": int(d["seq"]),
                "epoch": epoch,
                "at": d["at"],
                "kind": d["kind"],
                "subject": d["subject"],
                "ver": d.get("ver"),
                "actor": d["actor"],
                "actor_name": None,
                "data": json_dict(d.get("data")),
            })
        return out

    @staticmethod
    def ref_history(conn, ref_id: int) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM events_log WHERE subject = ? "
            "OR (kind = 'task_ref.changed' AND json_extract(data, '$.ref_id') = ?) ORDER BY seq",
            (f"ref:{ref_id}", int(ref_id)),
        ).fetchall()
        return TaskStore._event_rows(conn, rows)

    @staticmethod
    def task_history_events(conn, task_num: int, raw_id: str | None = None) -> list[dict]:
        subjects = [f"task:{task_num}", f"task:T-{task_num}"]
        if raw_id and raw_id not in (str(task_num), f"T-{task_num}"):
            subjects.append(f"task:{raw_id}")
        marks = ",".join("?" for _ in subjects)
        rows = conn.execute(
            f"SELECT * FROM events_log WHERE subject IN ({marks}) ORDER BY seq",  # noqa: S608
            subjects,
        ).fetchall()
        return TaskStore._event_rows(conn, rows)

    @staticmethod
    def upsert_pr_ref(conn, url: str, *, run_id: str | None = None, task_id: int | str | None = None,
                      repo: str = "", issue: str = "", ext_state: str | None = None,
                      force_open: bool = False, actor: str = "hand",
                      emit_event: bool = True) -> dict | None:
        if not has_identity_expand(conn):
            return None
        parsed = _parse_pr_url(url)
        if parsed is None:
            return None
        pr_key, clean_url = parsed
        run_row = None
        if run_id:
            from . import runs as run_store

            r = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            run_row = run_store.run_row(r) if r is not None else None
        task_num: int | None = None
        if task_id is not None and str(task_id).strip():
            task_num = parse_task_num(task_id, conn)
        if task_num is None and run_row is not None and run_row.get("task_id") is not None:
            task_num = int(run_row["task_id"])
        if task_num is None:
            lookup_repo = repo or (str(run_row.get("repo") or "") if run_row else "")
            lookup_issue = issue or (str(run_row.get("issue") or "") if run_row else "")
            if lookup_repo and lookup_issue:
                task_num = TaskStore.find_task_num(conn, lookup_repo, lookup_issue)

        run_status = str((run_row or {}).get("status") or "")
        default_state = (
            "open" if force_open
            else ext_state if ext_state is not None
            else "merged" if run_status == "merged"
            else "closed" if run_status == "rejected"
            else "open"
        )
        event_actor = actor if actor and actor != "owner" else _db_owner_principal(conn)

        existing = conn.execute(
            "SELECT * FROM refs WHERE system = 'github' AND key = ?", (pr_key,)
        ).fetchone()
        if existing is None:
            ref_dict, _ = TaskStore.ensure_ref(
                conn,
                "github",
                pr_key,
                kind="pr",
                connector="github",
                url=clean_url,
                ext_state=default_state,
                run_id=run_id,
                sync="synced",
                actor=event_actor,
                emit_event=emit_event,
            )
        else:
            existing_dict = dict(existing)
            new_kind = "pr"
            new_url = clean_url or str(existing_dict.get("url") or "")
            new_ext_state = "open" if force_open else (str(existing_dict.get("ext_state") or "") or default_state)
            new_run_id = existing_dict.get("run_id")
            if run_id and _is_newer_run(conn, run_id, new_run_id):
                new_run_id = run_id
            if (new_kind, new_url, new_ext_state, new_run_id) != (
                existing_dict.get("kind") or "",
                existing_dict.get("url") or "",
                existing_dict.get("ext_state") or "",
                existing_dict.get("run_id"),
            ):
                now = util.iso()
                conn.execute(
                    "UPDATE refs SET kind = ?, url = ?, ext_state = ?, run_id = ?, sync = 'synced', "
                    "version = version + 1, updated = ? WHERE id = ?",
                    (new_kind, new_url, new_ext_state, new_run_id, now, int(existing_dict["id"])),
                )
                saved_row = conn.execute("SELECT * FROM refs WHERE id = ?", (int(existing_dict["id"]),)).fetchone()
                ref_dict = _row_to_ref(saved_row)
                if emit_event:
                    with principals.acting(event_actor):
                        events.emit(
                            "ref.changed",
                            f"ref:{ref_dict['num']}",
                            cls="durable",
                            ver=ref_dict["version"],
                            data={k: v for k, v in ref_dict.items() if k != "num"},
                            connection=conn,
                        )
            else:
                ref_dict = _row_to_ref(existing_dict)

        if task_num is not None and _select_task_row(conn, task_num) is not None:
            linked = conn.execute(
                "SELECT 1 FROM task_refs WHERE task_id = ? AND ref_id = ?",
                (task_num, int(ref_dict["num"])),
            ).fetchone()
            if linked is None:
                TaskStore.link_ref(
                    conn,
                    task_num,
                    int(ref_dict["num"]),
                    {"role": "related", "linked_by": f"run:{run_id}/pr" if run_id else event_actor},
                    event_actor,
                    emit_event=emit_event,
                )
        return ref_dict

    @staticmethod
    def sync_run_pr_refs(conn, run_row: dict, *, force_open: bool = False,
                         actor: str | None = None, emit_event: bool = True) -> list[dict]:
        if not has_identity_expand(conn):
            return []
        if run_row.get("kind") != "pipeline" and run_row.get("task_id") is None:
            return []
        raw_urls = str(run_row.get("pr_url") or "").split()
        if not raw_urls:
            return []
        resolved_actor = actor or principals.current() or "hand"
        out: list[dict] = []
        for url in raw_urls:
            saved = TaskStore.upsert_pr_ref(
                conn,
                url,
                run_id=str(run_row["id"]) if run_row.get("id") else None,
                task_id=run_row.get("task_id"),
                repo=str(run_row.get("repo") or ""),
                issue=str(run_row.get("issue") or ""),
                force_open=force_open,
                actor=resolved_actor,
                emit_event=emit_event,
            )
            if saved is not None:
                out.append(saved)
        return out

    @staticmethod
    def update_pr_ref_state(conn, ref_id: int, *, ext_state: str, facts: dict,
                            fetched_at: str, remote_updated: str | None = None,
                            actor: str = "hand", emit_event: bool = True) -> dict | None:
        if not has_identity_expand(conn):
            return None
        existing = conn.execute("SELECT * FROM refs WHERE id = ?", (int(ref_id),)).fetchone()
        if existing is None:
            return None
        event_actor = actor if actor and actor != "owner" else _db_owner_principal(conn)
        conn.execute(
            "UPDATE refs SET ext_state = ?, facts = ?, fetched_at = ?, "
            "remote_updated = COALESCE(?, remote_updated), version = version + 1, updated = ? "
            "WHERE id = ?",
            (ext_state, json.dumps(facts or {}, sort_keys=True), fetched_at, remote_updated, fetched_at, int(ref_id)),
        )
        saved = _row_to_ref(conn.execute("SELECT * FROM refs WHERE id = ?", (int(ref_id),)).fetchone())
        if emit_event:
            with principals.acting(event_actor):
                events.emit(
                    "ref.changed",
                    f"ref:{saved['num']}",
                    cls="durable",
                    ver=saved["version"],
                    data={k: v for k, v in saved.items() if k != "num"},
                    connection=conn,
                )
        return saved


def _parse_pr_url(url: str) -> tuple[str, str] | None:
    clean = str(url or "").strip()
    if not clean:
        return None
    m = GITHUB_PR_URL_RE.fullmatch(clean)
    if not m:
        return None
    return f"{m.group(1)}#{int(m.group(2))}", clean


def _run_order_col(conn) -> str:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    return "seq" if "seq" in cols else "rowid"


def _is_newer_run(conn, candidate_run_id: str | None, existing_run_id: str | None) -> bool:
    if not candidate_run_id:
        return False
    if not existing_run_id:
        return True
    if candidate_run_id == existing_run_id:
        return False
    order_col = _run_order_col(conn)
    cand = conn.execute(
        f"SELECT started, {order_col} AS ord FROM runs WHERE id = ?",  # noqa: S608
        (candidate_run_id,),
    ).fetchone()
    if cand is None:
        return False
    prev = conn.execute(
        f"SELECT started, {order_col} AS ord FROM runs WHERE id = ?",  # noqa: S608
        (existing_run_id,),
    ).fetchone()
    if prev is None:
        return True
    return (cand["started"] or "", int(cand["ord"] or 0)) > (prev["started"] or "", int(prev["ord"] or 0))


def _is_local_tid(value: str) -> bool:
    s = (value or "").strip()
    return bool(CANONICAL_TASK_RE.fullmatch(s) or LEGACY_TASK_RE.fullmatch(s))


def backfill_identities(conn, cfg=None) -> dict[str, int]:
    """Idempotently backfill refs, primary tasks, task_refs, PR refs, task_id, ref_id, and issue_key."""
    if not has_identity_expand(conn):
        return {"refs": 0, "tasks": 0, "links": 0}
    if cfg is None:
        try:
            cfg, _ = config.load_or_problem()
        except Exception:
            cfg = None

    contracted = is_contracted(conn)
    new_refs = 0
    new_tasks = 0
    new_links = 0

    # Ensure every existing task has num filled (pre-contract).
    if not contracted:
        conn.execute("UPDATE tasks SET num = rowid WHERE num IS NULL")
        conn.execute("UPDATE task_comments SET num = rowid WHERE num IS NULL")

    # Build lookup map for existing local tasks.
    local_map: dict[str, int] = {}
    if contracted:
        for row in conn.execute("SELECT id FROM tasks"):
            n = int(row["id"])
            local_map[f"T-{n}"] = n
    else:
        for row in conn.execute("SELECT id, num FROM tasks"):
            n = int(row["num"])
            raw = str(row["id"])
            local_map[raw] = n
            local_map[raw.lower()] = n
            local_map[f"T-{n}"] = n

    # Collect known URLs and titles from triage / triage_approvals / research to enrich (repo, issue) refs.
    url_by_pair: dict[tuple[str, str], str] = {}
    title_by_ref_key: dict[tuple[str, str], str] = {}
    repo_by_ref_key: dict[tuple[str, str], str] = {}
    ref_meta_by_key: dict[tuple[str, str], dict] = {}

    def _register_ref_meta(system: str, kind: str, key: str, connector: str, sync: str,
                           title: str = "", url: str = "", repo: str = "", updated: str | None = None) -> None:
        sk = (system, key)
        cur = ref_meta_by_key.get(sk)
        if cur is None:
            ref_meta_by_key[sk] = {
                "system": system, "kind": kind, "key": key, "connector": connector,
                "sync": sync, "title": title, "url": url, "updated": updated,
            }
        else:
            if title and not cur["title"]:
                cur["title"] = title
            if url and not cur["url"]:
                cur["url"] = url
            if sync == "synced" and cur["sync"] == "off":
                cur["sync"] = "synced"
            if connector and not cur["connector"]:
                cur["connector"] = connector
            if updated and not cur["updated"]:
                cur["updated"] = updated
        if title and sk not in title_by_ref_key:
            title_by_ref_key[sk] = title
        if repo and sk not in repo_by_ref_key:
            repo_by_ref_key[sk] = repo

    # Scan triage, triage_approvals, research first so their URLs and titles are known.
    triage_tables = (
        ("triage", "SELECT ref, repo, tracker, title, url, updated, ref_id FROM triage"),
        ("triage_approvals", "SELECT ref, repo, tracker, title, url, ts AS updated, ref_id FROM triage_approvals"),
        ("research", "SELECT ref, '' AS repo, '' AS tracker, title, url, updated, ref_id FROM research"),
    )
    triage_parsed: list[tuple[str, str, str, str]] = []
    for tbl, sql in triage_tables:
        for row in conn.execute(sql).fetchall():
            raw_ref = str(row["ref"] or "").strip()
            if not raw_ref:
                continue
            url = str(row["url"] or "").strip()
            repo = str(row["repo"] or "").strip()
            tracker_hint = str(row["tracker"] or "").strip().lower()
            if not repo and "#" in raw_ref and "/" not in raw_ref.split("#", 1)[0]:
                repo = raw_ref.split("#", 1)[0]
            system, kind, key, issue_key, connector, sync = _parse_external_ticket(
                repo, raw_ref, cfg=cfg, url=url, tracker_hint=tracker_hint,
            )
            _register_ref_meta(system, kind, key, connector, sync,
                               title=str(row["title"] or "").strip(), url=url, repo=repo,
                               updated=row["updated"] or None)
            if url and repo and issue_key:
                url_by_pair[(repo, issue_key)] = url
            triage_parsed.append((tbl, raw_ref, system, key))

    # Scan pipeline runs, feedback, mined, passed_over, asks.
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    has_payer = "payer" in run_cols
    has_seq = "seq" in run_cols
    has_issue = "issue" in run_cols
    order_col = "seq" if has_seq else "rowid"
    payer_expr = "payer" if has_payer else "NULL AS payer"
    issue_expr = "issue" if has_issue else "issue_key AS issue"
    pipeline_runs = [
        dict(r) for r in conn.execute(
            f"SELECT id, repo, {issue_expr}, status, started, finished, only_step, pr_url, task_id, issue_key, {payer_expr} "  # noqa: S608
            f"FROM runs WHERE kind = 'pipeline' ORDER BY started ASC, {order_col} ASC"
        ).fetchall()
    ]

    # Track first run & newest whole run per external ref (system, key).
    first_run_by_sk: dict[tuple[str, str], dict] = {}
    newest_whole_by_sk: dict[tuple[str, str], dict] = {}
    run_identity_by_id: dict[str, tuple[str, tuple[str, str] | int, str]] = {}

    def _classify_pair(repo: str, issue: str) -> tuple[str, tuple[str, str] | int, str] | None:
        raw = str(issue or "").strip()
        if not raw:
            return None
        if _is_local_tid(raw):
            num = local_map.get(raw) or local_map.get(raw.lower())
            if num is None and (m := CANONICAL_TASK_RE.fullmatch(raw)):
                num = int(m.group(1))
            if num is not None:
                return ("local", num, format_task_id(num))
            return None
        url = url_by_pair.get((repo, raw.lstrip("#")), "")
        system, kind, key, issue_key, connector, sync = _parse_external_ticket(repo, raw, cfg=cfg, url=url)
        _register_ref_meta(system, kind, key, connector, sync, url=url, repo=repo)
        return ("ref", (system, key), issue_key)

    for run in pipeline_runs:
        repo = str(run.get("repo") or "").strip()
        issue = str(run.get("issue") or "").strip()
        if not issue:
            continue
        classified = _classify_pair(repo, issue)
        if classified is None:
            continue
        run_identity_by_id[run["id"]] = classified
        if classified[0] == "ref":
            sk = classified[1]
            if isinstance(sk, tuple):
                first_run_by_sk.setdefault(sk, run)
                if not (run.get("only_step") or ""):
                    newest_whole_by_sk[sk] = run

    # Scan feedback, mined, passed_over, asks for any additional (repo, issue) pairs.
    side_specs = (
        ("feedback", "SELECT rowid, repo, issue, run_id, task_id FROM feedback WHERE task_id IS NULL"),
        ("mined", "SELECT rowid, repo, issue, run_id, task_id FROM mined WHERE task_id IS NULL"),
        ("passed_over", "SELECT rowid, repo, issue, NULL AS run_id, task_id FROM passed_over WHERE task_id IS NULL"),
        ("asks", "SELECT rowid, repo, ticket AS issue, run AS run_id, task_id FROM asks WHERE task_id IS NULL"),
    )
    side_rows: dict[str, list[dict]] = {}
    for tbl, sql in side_specs:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
        side_rows[tbl] = rows
        for r in rows:
            _classify_pair(str(r.get("repo") or "").strip(), str(r.get("issue") or "").strip())

    # 1. Ensure all refs exist and emit ref.changed once per NEW ref (acting as 'hand').
    ref_id_by_sk: dict[tuple[str, str], int] = {}
    with principals.acting("hand"):
        for sk, meta in ref_meta_by_key.items():
            ref_dict, created = TaskStore.ensure_ref(
                conn,
                meta["system"],
                meta["key"],
                kind=meta["kind"],
                connector=meta["connector"],
                title=meta["title"],
                url=meta["url"],
                remote_updated=meta["updated"],
                sync=meta["sync"],
                actor="hand",
                emit_event=True,
            )
            ref_id_by_sk[sk] = int(ref_dict["num"])
            if created:
                new_refs += 1

        # 2. Ensure each external ref has a primary task + task_refs link.
        task_num_by_sk: dict[tuple[str, str], int] = {}
        for sk, ref_id in ref_id_by_sk.items():
            existing_primary = conn.execute(
                "SELECT task_id FROM task_refs WHERE ref_id = ? AND role = 'primary' LIMIT 1",
                (ref_id,),
            ).fetchone()
            if existing_primary is not None:
                task_num_by_sk[sk] = int(existing_primary[0])
                continue
            first_run = first_run_by_sk.get(sk)
            newest_whole = newest_whole_by_sk.get(sk) or first_run
            repo_name = (first_run.get("repo") if first_run else "") or repo_by_ref_key.get(sk, "")
            title = title_by_ref_key.get(sk) or sk[1]
            payer = (first_run.get("payer") if first_run else None) or ""
            owner = _resolve_default_owner(conn, payer or None, cfg=cfg)
            state, fault = _run_status_to_task_state(newest_whole.get("status") if newest_whole else None)
            created_at = (first_run.get("started") if first_run else None) or util.iso()
            updated_at = (
                (newest_whole.get("finished") or newest_whole.get("started")) if newest_whole else None
            ) or created_at
            created_task = TaskStore.create(
                conn,
                {
                    "repo": repo_name,
                    "title": title,
                    "body": "",
                    "kind": "feat",
                    "state": state,
                    "fault": fault,
                    "owner": owner,
                    "origin": f"ref:{sk[0]}:{sk[1]}",
                    "created": created_at,
                    "updated": updated_at,
                },
                "hand",
                emit_event=True,
                event_kind="task.changed",
            )
            tnum = int(created_task["num"])
            new_tasks += 1
            TaskStore.link_ref(
                conn,
                tnum,
                ref_id,
                {"role": "primary", "linked_by": "hand"},
                "hand",
                emit_event=False,
            )
            new_links += 1
            task_num_by_sk[sk] = tnum

        # 3. Populate runs.task_id and runs.issue_key where missing.
        run_task_by_id: dict[str, int] = {}
        for run in pipeline_runs:
            existing_tid = run.get("task_id")
            existing_ikey = run.get("issue_key")
            classified = run_identity_by_id.get(run["id"])
            if classified is None:
                if existing_tid is not None:
                    run_task_by_id[run["id"]] = int(existing_tid)
                continue
            kind_tag, target, issue_key = classified
            tnum = int(target) if kind_tag == "local" else task_num_by_sk[target]
            run_task_by_id[run["id"]] = tnum
            if existing_tid != tnum or existing_ikey != issue_key:
                conn.execute(
                    "UPDATE runs SET task_id = COALESCE(task_id, ?), issue_key = COALESCE(issue_key, ?) WHERE id = ?",
                    (tnum, issue_key, run["id"]),
                )

        # 4. Populate side tables: asks, feedback, mined, passed_over.
        for tbl, rows in side_rows.items():
            for r in rows:
                tnum: int | None = None
                if r.get("run_id") and r["run_id"] in run_task_by_id:
                    tnum = run_task_by_id[r["run_id"]]
                else:
                    classified = _classify_pair(str(r.get("repo") or "").strip(), str(r.get("issue") or "").strip())
                    if classified is not None:
                        kind_tag, target, _ = classified
                        tnum = int(target) if kind_tag == "local" else task_num_by_sk.get(target)
                if tnum is not None:
                    conn.execute(f"UPDATE {tbl} SET task_id = ? WHERE rowid = ? AND task_id IS NULL",  # noqa: S608
                                 (tnum, r["rowid"]))

        # 5. Populate ref_id on triage, triage_approvals, research.
        for tbl, raw_ref, system, key in triage_parsed:
            ref_id = ref_id_by_sk.get((system, key))
            if ref_id is not None:
                conn.execute(f"UPDATE {tbl} SET ref_id = ? WHERE ref = ? AND ref_id IS NULL",  # noqa: S608
                             (ref_id, raw_ref))

        # 6. Create PR refs from runs.pr_url and link them to each run's task.
        pr_newest_run: dict[tuple[str, str], tuple[dict, str]] = {}
        pr_task_links: list[tuple[int, str, str]] = []
        for run in pipeline_runs:
            tnum = run_task_by_id.get(run["id"])
            for pr_url in (run.get("pr_url") or "").split():
                m = GITHUB_PR_URL_RE.fullmatch(pr_url.strip())
                if not m:
                    continue
                pr_key = f"{m.group(1)}#{int(m.group(2))}"
                pr_newest_run[("github", pr_key)] = (run, pr_url.strip())
                if tnum is not None:
                    pr_task_links.append((tnum, pr_key, run["id"]))

        pr_ref_ids: dict[str, int] = {}
        for (_, pr_key), (newest_run, pr_url) in pr_newest_run.items():
            status = newest_run.get("status") or ""
            ext_state = "merged" if status == "merged" else "closed" if status == "rejected" else "open"
            ref_dict, created = TaskStore.ensure_ref(
                conn,
                "github",
                pr_key,
                kind="pr",
                connector="github",
                url=pr_url,
                ext_state=ext_state,
                run_id=newest_run["id"],
                sync="synced",
                actor="hand",
                emit_event=True,
            )
            pr_ref_ids[pr_key] = int(ref_dict["num"])
            if created:
                new_refs += 1

        for tnum, pr_key, run_id in pr_task_links:
            pr_ref_id = pr_ref_ids.get(pr_key)
            if pr_ref_id is None:
                continue
            exists = conn.execute(
                "SELECT 1 FROM task_refs WHERE task_id = ? AND ref_id = ?", (tnum, pr_ref_id)
            ).fetchone()
            if exists is None:
                TaskStore.link_ref(
                    conn,
                    tnum,
                    pr_ref_id,
                    {"role": "related", "linked_by": f"run:{run_id}/pr"},
                    "hand",
                    emit_event=False,
                )
                new_links += 1

    return {"refs": new_refs, "tasks": new_tasks, "links": new_links}


def reconcile_pr_refs(conn, cfg=None) -> dict[str, int]:
    """Idempotently reconcile PR refs and task_refs from all runs.pr_url rows and durable outbox PR receipts.

    Reconciles every URL written since #584 with no run-status or age filter, applying
    PR-ref uniqueness, role='related' task linking, and newest-run ownership while
    preserving numeric identities, versions, and event history.
    """
    if not has_identity_expand(conn):
        return {"refs": 0, "updated": 0, "links": 0}
    if cfg is None:
        try:
            cfg, _ = config.load_or_problem()
        except Exception:
            cfg = None

    from . import runs as run_store

    order_col = _run_order_col(conn)
    issue_expr = "issue" if run_store.has_issue_column(conn) else "issue_key AS issue"
    run_rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT id, kind, repo, {issue_expr}, status, started, finished, pr_url, task_id, {order_col} AS _ord "  # noqa: S608
            f"FROM runs WHERE COALESCE(pr_url, '') != '' AND (kind = 'pipeline' OR task_id IS NOT NULL) "
            f"ORDER BY started ASC, {order_col} ASC"
        ).fetchall()
    ]
    run_by_id: dict[str, dict] = {str(r["id"]): r for r in run_rows}

    # Map pr_key -> list of (sort_key, run_id, run_status, task_num, clean_url)
    observations: dict[str, list[tuple[tuple[str, int], str | None, str, int | None, str]]] = {}

    def _resolve_run_task(rrow: dict | None, explicit_task: object = None) -> int | None:
        if explicit_task is not None and str(explicit_task).strip():
            tnum = parse_task_num(str(explicit_task), conn)
            if tnum is not None:
                return tnum
        if rrow is not None:
            if rrow.get("task_id") is not None:
                return int(rrow["task_id"])
            if rrow.get("repo") and rrow.get("issue"):
                return TaskStore.find_task_num(conn, str(rrow["repo"]), str(rrow["issue"]), cfg=cfg)
        return None

    for rrow in run_rows:
        tnum = _resolve_run_task(rrow)
        sort_key = (str(rrow.get("started") or ""), int(rrow.get("_ord") or 0))
        rid = str(rrow["id"])
        status = str(rrow.get("status") or "")
        for tok in str(rrow.get("pr_url") or "").split():
            parsed = _parse_pr_url(tok)
            if parsed is None:
                continue
            pr_key, clean_url = parsed
            observations.setdefault(pr_key, []).append((sort_key, rid, status, tnum, clean_url))

    has_outbox = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbox'"
    ).fetchone() is not None
    if has_outbox:
        receipt_rows = conn.execute(
            "SELECT o.id, o.run_id, o.task_id, o.sent_url, o.sent_at, o.updated, "
            "(SELECT r.body FROM outbox_revisions r WHERE r.item = o.id AND r.rev = o.rev LIMIT 1) AS body, "
            "(SELECT da.external_url FROM delivery_attempts da "
            " WHERE da.item = o.id AND da.outcome IN ('sent', 'found') AND COALESCE(da.external_url, '') != '' "
            " ORDER BY da.n DESC LIMIT 1) AS attempt_url "
            "FROM outbox o "
            "WHERE o.kind = 'pr' AND ("
            "  (o.state = 'sent' AND COALESCE(o.sent_url, '') != '') "
            "  OR EXISTS ("
            "    SELECT 1 FROM delivery_attempts da "
            "    WHERE da.item = o.id AND da.outcome IN ('sent', 'found') AND COALESCE(da.external_url, '') != ''"
            "  )"
            ") ORDER BY o.id ASC"
        ).fetchall()
        for rec in receipt_rows:
            body_text = str(rec["body"] or "").strip()
            if body_text.startswith("{"):
                try:
                    payload = json.loads(body_text)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and (payload.get("action") or "create") != "create":
                    continue
            raw_url = str(rec["sent_url"] or rec["attempt_url"] or "").strip()
            parsed = _parse_pr_url(raw_url)
            if parsed is None:
                continue
            pr_key, clean_url = parsed
            rid = str(rec["run_id"]) if rec["run_id"] else None
            rrow = run_by_id.get(rid) if rid else None
            if rid and rrow is None:
                fetched = conn.execute(
                    f"SELECT id, kind, repo, {issue_expr}, status, started, finished, pr_url, task_id, {order_col} AS _ord "  # noqa: S608
                    "FROM runs WHERE id = ?",
                    (rid,),
                ).fetchone()
                if fetched is not None:
                    rrow = dict(fetched)
                    run_by_id[rid] = rrow
            if rrow is not None and rrow.get("kind") != "pipeline" and rrow.get("task_id") is None and not rec["task_id"]:
                continue
            if rrow is None and not rec["task_id"]:
                continue
            tnum = _resolve_run_task(rrow, rec["task_id"])
            sort_key = (
                (str(rrow.get("started") or ""), int(rrow.get("_ord") or 0))
                if rrow is not None
                else ("", 0)
            )
            status = str((rrow or {}).get("status") or "")
            observations.setdefault(pr_key, []).append((sort_key, rid, status, tnum, clean_url))

    new_refs = 0
    updated_refs = 0
    new_links = 0
    with principals.acting("hand"):
        for pr_key in sorted(observations):
            obs_list = observations[pr_key]
            obs_list.sort(key=lambda item: item[0])
            _, newest_run_id, newest_status, _, best_url = obs_list[-1]
            default_state = (
                "merged" if newest_status == "merged"
                else "closed" if newest_status == "rejected"
                else "open"
            )
            existing = conn.execute(
                "SELECT * FROM refs WHERE system = 'github' AND key = ?", (pr_key,)
            ).fetchone()
            if existing is None:
                ref_dict, created = TaskStore.ensure_ref(
                    conn,
                    "github",
                    pr_key,
                    kind="pr",
                    connector="github",
                    url=best_url,
                    ext_state=default_state,
                    run_id=newest_run_id,
                    sync="synced",
                    actor="hand",
                    emit_event=True,
                )
                ref_id = int(ref_dict["num"])
                if created:
                    new_refs += 1
            else:
                existing_dict = dict(existing)
                ref_id = int(existing_dict["id"])
                new_kind = "pr"
                new_url = best_url or str(existing_dict.get("url") or "")
                new_ext_state = str(existing_dict.get("ext_state") or "") or default_state
                new_run_id = existing_dict.get("run_id")
                if newest_run_id and _is_newer_run(conn, newest_run_id, new_run_id):
                    new_run_id = newest_run_id
                if (new_kind, new_url, new_ext_state, new_run_id) != (
                    existing_dict.get("kind") or "",
                    existing_dict.get("url") or "",
                    existing_dict.get("ext_state") or "",
                    existing_dict.get("run_id"),
                ):
                    now = util.iso()
                    conn.execute(
                        "UPDATE refs SET kind = ?, url = ?, ext_state = ?, run_id = ?, sync = 'synced', "
                        "version = version + 1, updated = ? WHERE id = ?",
                        (new_kind, new_url, new_ext_state, new_run_id, now, ref_id),
                    )
                    saved = _row_to_ref(conn.execute("SELECT * FROM refs WHERE id = ?", (ref_id,)).fetchone())
                    events.emit(
                        "ref.changed",
                        f"ref:{saved['num']}",
                        cls="durable",
                        ver=saved["version"],
                        data={k: v for k, v in saved.items() if k != "num"},
                        connection=conn,
                    )
                    updated_refs += 1

            seen_tasks: set[int] = set()
            for _, obs_run_id, _, tnum, _ in obs_list:
                if tnum is None or tnum in seen_tasks:
                    continue
                seen_tasks.add(tnum)
                if _select_task_row(conn, tnum) is None:
                    continue
                linked = conn.execute(
                    "SELECT 1 FROM task_refs WHERE task_id = ? AND ref_id = ?",
                    (tnum, ref_id),
                ).fetchone()
                if linked is None:
                    TaskStore.link_ref(
                        conn,
                        tnum,
                        ref_id,
                        {"role": "related", "linked_by": f"run:{obs_run_id}/pr" if obs_run_id else "hand"},
                        "hand",
                        emit_event=True,
                    )
                    new_links += 1

    return {"refs": new_refs, "updated": updated_refs, "links": new_links}


def pr_refs_to_sync(rejected_since: str, connect=reader) -> list[dict]:
    with connect() as c:
        if not has_identity_expand(c):
            return []
        from . import runs as run_store

        ic = run_store.issue_col(c, "runs")
        rows = c.execute(
            f"SELECT r.*, "  # noqa: S608
            f"runs.repo AS _run_repo, {ic} AS _run_issue, runs.issue_key AS _run_issue_key, "
            "runs.task_id AS _run_task_id, runs.status AS _run_status, runs.finished AS _run_finished, "
            "runs.started AS _run_started, "
            "COALESCE("
            "  (SELECT tr.task_id FROM task_refs tr WHERE tr.ref_id = r.id ORDER BY tr.id ASC LIMIT 1), "
            "  runs.task_id"
            ") AS _task_id "
            "FROM refs r "
            "JOIN runs ON runs.id = r.run_id "
            "WHERE r.kind = 'pr' "
            "  AND r.url != '' "
            "  AND runs.kind = 'pipeline' "
            "  AND ("
            "    r.ext_state IN ('open', '') "
            "    OR ("
            "      r.ext_state = 'closed' "
            "      AND COALESCE("
            "        NULLIF(json_extract(r.facts, '$.closed_at'), ''), "
            "        NULLIF(runs.finished, ''), "
            "        NULLIF(r.fetched_at, ''), "
            "        r.updated"
            "      ) >= ?"
            "    )"
            "  ) "
            "ORDER BY runs.started DESC, r.id ASC",
            (rejected_since,),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            d = dict(row)
            ref = _row_to_ref(d)
            ref["task_id"] = int(d["_task_id"]) if d.get("_task_id") is not None else None
            out.append(ref)
        return out


def run_pr_refs(run_id: str, connect=reader) -> list[dict]:
    with connect() as c:
        if not has_identity_expand(c):
            return []
        rows = c.execute(
            "SELECT * FROM refs WHERE kind = 'pr' AND run_id = ? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
        return [_row_to_ref(r) for r in rows]


def _resolve_pr_task_num(conn, repo_or_task: str | int, issue: int | str | None = None) -> int | None:
    if isinstance(repo_or_task, int) and not isinstance(repo_or_task, bool) and issue is None:
        return repo_or_task
    return TaskStore.find_task_num(conn, repo_or_task, issue if issue is not None else "")


def open_pr_refs(repo_or_task: str | int, issue: int | str | None = None, connect=reader) -> list[dict]:
    with connect() as c:
        if not has_identity_expand(c):
            return []
        tnum = _resolve_pr_task_num(c, repo_or_task, issue)
        if tnum is None:
            return []
        order_col = _run_order_col(c)
        rows = c.execute(
            f"SELECT r.* FROM task_refs tr "  # noqa: S608
            "JOIN refs r ON r.id = tr.ref_id "
            "LEFT JOIN runs ON runs.id = r.run_id "
            "WHERE tr.task_id = ? AND r.kind = 'pr' AND LOWER(r.ext_state) IN ('open', '') AND r.url != '' "
            f"ORDER BY COALESCE(runs.started, r.updated) DESC, COALESCE(runs.{order_col}, 0) DESC, r.id DESC",
            (tnum,),
        ).fetchall()
        return [_row_to_ref(r) for r in rows]


def open_pr_runs(repo_or_task: str | int, issue: int | str | None = None, connect=reader) -> list[dict]:
    with connect() as c:
        if not has_identity_expand(c):
            return []
        tnum = _resolve_pr_task_num(c, repo_or_task, issue)
        if tnum is None:
            return []
        order_col = _run_order_col(c)
        rows = c.execute(
            f"SELECT DISTINCT runs.* FROM task_refs tr "  # noqa: S608
            "JOIN refs r ON r.id = tr.ref_id "
            "JOIN runs ON runs.id = r.run_id "
            "WHERE tr.task_id = ? AND r.kind = 'pr' AND LOWER(r.ext_state) IN ('open', '') AND r.url != '' "
            f"ORDER BY runs.started DESC, runs.{order_col} DESC",
            (tnum,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_pr_ref_state(ref_id: int, *, ext_state: str, facts: dict, fetched_at: str,
                        remote_updated: str | None = None, actor: str = "hand") -> dict | None:
    with writes.atomic() as c:
        return TaskStore.update_pr_ref_state(
            c,
            ref_id,
            ext_state=ext_state,
            facts=facts,
            fetched_at=fetched_at,
            remote_updated=remote_updated,
            actor=actor,
            emit_event=True,
        )


# ---------------------------------------------------------------------------
# Module-level owner-store accessors
# ---------------------------------------------------------------------------

def task_add(repo: str, body: str, title: str = "", kind: str = "feat", parent: str = "", claim=None,
             *, reserved_id: str = "", extra: dict | None = None, actor: str | None = None) -> str:
    extra = extra or {}
    actor_id = actor or principals.current()
    with writes.atomic() as c:
        values = {
            "repo": repo,
            "title": title,
            "body": body,
            "kind": kind,
            "parent": parent,
            "reserved_id": reserved_id,
            **{k: v for k, v in extra.items() if k in WRITABLE_TASK_EXTRA_FIELDS},
        }
        created = TaskStore.create(c, values, actor_id, emit_event=True, event_kind="task.created")
        tid = created["id"] if (is_contracted(c) or has_identity_expand(c)) else created["raw_id"]
        if reserved_id and not is_contracted(c):
            tid = created["raw_id"]
        if claim:
            subject = f"task:{created['num']}" if created["num"] is not None and (is_contracted(c) or has_identity_expand(c)) else f"task:{tid}"
            seq = c.execute(
                "SELECT seq FROM events_log WHERE subject = ? AND kind = 'task.created' "
                "ORDER BY seq DESC LIMIT 1",
                (subject,),
            ).fetchone()[0]
            idempotency.claim(claim, seq)
        return tid


def task_get(tid: str | int, connect=reader, *, include_relations: bool = True) -> dict | None:
    with connect() as c:
        row = _select_task_row(c, tid)
        return _present_task_row(c, row, include_relations=include_relations) if row else None


def task_get_num(num: int, connect=reader, *, include_relations: bool = True) -> dict | None:
    with connect() as c:
        row = _select_task_row(c, int(num))
        return _present_task_row(c, row, include_relations=include_relations) if row else None


def tasks(repo: str = "", in_state: str = "", connect=reader, *, include_relations: bool = True) -> list[dict]:
    with connect() as c:
        contracted = is_contracted(c)
        expand = has_identity_expand(c)
        select = "SELECT * FROM tasks WHERE 1=1" if contracted else "SELECT rowid AS _rowid, * FROM tasks WHERE 1=1"
        args: list[object] = []
        if repo:
            select += " AND repo = ?"
            args.append(repo)
        if in_state:
            if contracted:
                canon, fault = config.canonical_task_state(in_state)
                select += " AND state = ?"
                args.append(canon)
                if in_state == "failed":
                    select += " AND fault = 1"
                elif in_state == "needs-info":
                    select += " AND fault = 0"
            else:
                legacy = config.legacy_task_state(in_state)
                select += " AND state IN (?, ?)"
                args.extend((in_state, legacy))
        order = " ORDER BY created, id" if contracted else (" ORDER BY created, num" if expand else " ORDER BY created, rowid")
        return [_present_task_row(c, r, include_relations=include_relations) for r in c.execute(select + order, args)]


def local_todo_tasks(repo: str, *, include_failed: bool = False, connect=reader) -> list[dict]:
    """List local ready tasks (with no primary ref) in ``repo``."""
    with connect() as c:
        contracted = is_contracted(c)
        expand = has_identity_expand(c)
        id_col = "t.id" if contracted else ("t.num" if expand else "t.rowid")
        no_primary = (
            f" AND NOT EXISTS (SELECT 1 FROM task_refs tr WHERE tr.task_id = {id_col} AND tr.role = 'primary')"  # noqa: S608
            if expand else ""
        )
        if contracted:
            if include_failed:
                state_clause = "((t.state = 'ready') OR (t.state = 'needs-you' AND t.fault = 1))"
            else:
                state_clause = "t.state = 'ready'"
            sql = f"SELECT t.* FROM tasks t WHERE t.repo = ? AND {state_clause}{no_primary} ORDER BY t.created, t.id"  # noqa: S608
        else:
            if include_failed:
                state_clause = "t.state IN ('todo', 'ready', 'failed')"
            else:
                state_clause = "t.state IN ('todo', 'ready')"
            sql = (
                f"SELECT t.rowid AS _rowid, t.* FROM tasks t WHERE t.repo = ? AND {state_clause}{no_primary} "  # noqa: S608
                f"ORDER BY t.created, {'t.num' if expand else 't.rowid'}"
            )
        return [_present_task_row(c, r, include_relations=False) for r in c.execute(sql, (repo,))]


def task_summaries(connect=reader) -> list[dict]:
    rows = tasks(connect=connect, include_relations=False)
    rows.sort(key=lambda r: (r["updated"], r["id"]), reverse=True)
    return [{k: r[k] for k in ("id", "repo", "title", "kind", "state", "updated")} for r in rows]


def task_children(parent: str | int, connect=reader) -> list[dict]:
    with connect() as c:
        contracted = is_contracted(c)
        if contracted:
            pnum = parse_task_num(parent, c)
            if pnum is None:
                return []
            return [_present_task_row(c, r, include_relations=False)
                    for r in c.execute("SELECT * FROM tasks WHERE parent = ? ORDER BY created, id", (pnum,))]
        return [_present_task_row(c, r, include_relations=False)
                for r in c.execute("SELECT rowid AS _rowid, * FROM tasks WHERE parent = ? ORDER BY created, rowid",
                                   (str(parent),))]


def _expand_from_states(from_states: Iterable[str], contracted: bool) -> tuple[str, ...]:
    if not from_states:
        return ()
    allowed: list[str] = []
    for st in from_states:
        canon, fault = config.canonical_task_state(st)
        if contracted:
            allowed.append(canon)
        else:
            allowed.append(st)
            allowed.append(config.legacy_task_state(canon, fault))
    return tuple(dict.fromkeys(allowed))


def task_set_state(tid: str | int, new: str, fault: int | None = None,
                   from_states: tuple[str, ...] = (), base_version=None,
                   *, fault_detail: str | None = None) -> bool:
    # Support legacy 3-positional call: task_set_state(tid, new, from_states_tuple)
    if isinstance(fault, (tuple, list, set)):
        from_states = tuple(fault)
        fault = None
    with writes.atomic() as c:
        row = _select_task_row(c, tid)
        if row is None:
            return False
        contracted = is_contracted(c)
        expand = has_identity_expand(c)
        canon_state, canon_fault = config.canonical_task_state(new, 0 if fault is None else int(fault))
        if fault is not None:
            canon_fault = 1 if fault else 0
        stored_state = canon_state if contracted else config.legacy_task_state(canon_state, canon_fault)
        expect = _expand_from_states(from_states, contracted)
        if expect and row["state"] not in expect:
            return False
        if contracted and from_states:
            # Respect fault distinction when caller guarded specifically on 'failed' or 'needs-info'.
            only_failed = set(from_states) == {"failed"}
            only_needs_info = set(from_states) == {"needs-info"}
            if only_failed and int(row["fault"] or 0) != 1:
                return False
            if only_needs_info and int(row["fault"] or 0) != 0:
                return False
        fields: dict[str, object] = {"state": stored_state, "updated": util.iso()}
        if contracted or expand:
            fields["fault"] = canon_fault
            if fault_detail is not None:
                fields["fault_detail"] = fault_detail
            elif canon_fault == 0:
                fields["fault_detail"] = None
        identity = int(row["id"]) if contracted else row["id"]
        return writes.update("tasks", identity, fields, expect=expect, base_version=base_version) is not None


def task_update(tid: str | int, base_version: int, fields: dict) -> dict | None:
    with writes.atomic() as c:
        row = _select_task_row(c, tid)
        if row is None:
            return None
        contracted = is_contracted(c)
        expand = has_identity_expand(c)
        if base_version is not None and int(row["version"]) != base_version:
            raise writes.VersionConflict(_present_task_row(c, row))
        identity = int(row["id"]) if contracted else row["id"]
        num = int(row["id"]) if contracted else (int(row["num"]) if expand else None)
        updates: dict[str, object] = {"updated": util.iso()}
        for key, value in fields.items():
            if key == "state":
                canon_state, canon_fault = config.canonical_task_state(str(value))
                updates["state"] = canon_state if contracted else config.legacy_task_state(canon_state, canon_fault)
                if contracted or expand:
                    updates["fault"] = canon_fault
                    if canon_fault == 0:
                        updates["fault_detail"] = None
            elif key in WRITABLE_TASK_UPDATE_FIELDS:
                if key in WRITABLE_TASK_EXTRA_FIELDS and not (contracted or expand):
                    continue
                updates[key] = value
            else:
                raise ValueError(f"invalid task field {key!r}")
        try:
            changed = writes.update("tasks", identity, updates, base_version=base_version)
        except writes.VersionConflict as err:
            raise writes.VersionConflict(_present_task_row(c, err.current)) from err
        if changed is None:
            return None
        if expand and num is not None and "body" in fields:
            TaskStore.sync_body_links(c, num, str(fields["body"] or ""))
        if expand and num is not None:
            from .. import sync as sync_mod

            sync_mod.draft_remote_field_edits(c, num, dict(row), fields, actor=principals.current() or "hand")
        refreshed = _select_task_row(c, identity)
        return _present_task_row(c, refreshed) if refreshed else None


class TaskStateError(refusals.Conflict):
    code = "invalid_state"


def task_restore(tid: str | int, base_version: int | None = None) -> dict | None:
    with writes.atomic() as c:
        row = _select_task_row(c, tid)
        if row is None:
            return None
        if base_version is not None and int(row["version"]) != base_version:
            raise writes.VersionConflict(_present_task_row(c, row))
        if row["state"] not in config.TASK_FINISHED:
            raise TaskStateError(f"Task {tid} is {row['state']} and cannot be restored")
        ok = task_set_state(tid, "ready", 0, from_states=config.TASK_FINISHED, base_version=base_version)
        if not ok:
            raise TaskStateError(f"Task {tid} cannot be restored")
        return _present_task_row(c, _select_task_row(c, tid))


def task_comment(tid: str | int, author: str, body: str, *, audience: str = "internal") -> bool:
    ts = util.iso()
    with writes.atomic() as c:
        row = _select_task_row(c, tid)
        if row is None:
            return False
        contracted = is_contracted(c)
        expand = has_identity_expand(c)
        identity = int(row["id"]) if contracted else row["id"]
        if not writes.update("tasks", identity, {"updated": ts}):
            return False
        norm_author = author
        if contracted:
            if norm_author == "owner":
                norm_author = _db_owner_principal(c)
            elif norm_author == "arete":
                actor = principals.current()
                norm_author = actor if actor and actor not in ("hand", "arete", "owner") else "agent:build"
            next_id = int(c.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM task_comments").fetchone()[0])
            writes.insert(
                "task_comments",
                {"task_id": int(row["id"]), "author": norm_author, "body": body,
                 "audience": audience or "internal", "created": ts, "updated": ts},
                next_id,
                kind="comment.changed",
            )
        elif expand:
            next_num = int(c.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM task_comments").fetchone()[0])
            writes.insert(
                "task_comments",
                {"task": row["id"], "author": norm_author, "body": body, "ts": ts,
                 "num": next_num, "audience": audience or "internal", "created": ts, "updated": ts},
                kind="comment.changed",
            )
        else:
            writes.insert("task_comments", {"task": row["id"], "author": norm_author, "body": body, "ts": ts})
        return True


def task_comment_update(tid: str | int, comment_id: int, base_version: int, fields: dict) -> dict | None:
    validate_columns(fields, COMMENT_UPDATABLE_FIELDS, "task_comments")
    if not fields:
        raise ValueError("send at least one editable comment field")
    with writes.atomic() as c:
        task_row = _select_task_row(c, tid)
        if task_row is None:
            raise LookupError(f"no task {tid}")
        contracted = is_contracted(c)
        expand = has_identity_expand(c)
        if contracted:
            row = c.execute(
                "SELECT * FROM task_comments WHERE id = ? AND task_id = ?",
                (int(comment_id), int(task_row["id"])),
            ).fetchone()
            identity = int(comment_id)
        elif expand:
            row = c.execute(
                "SELECT rowid AS _rowid, * FROM task_comments WHERE num = ? AND task = ?",
                (int(comment_id), task_row["id"]),
            ).fetchone()
            identity = int(row["_rowid"]) if row else None
        else:
            row = c.execute(
                "SELECT rowid AS _rowid, * FROM task_comments WHERE rowid = ? AND task = ?",
                (int(comment_id), task_row["id"]),
            ).fetchone()
            identity = int(row["_rowid"]) if row else None
        if row is None or identity is None:
            return None
        updates: dict[str, object] = {**fields}
        if contracted or expand:
            updates["updated"] = util.iso()
        if not contracted and "audience" in updates and not expand:
            updates.pop("audience")
        updated = writes.update("task_comments", identity, updates, base_version=base_version, kind="comment.changed")
        if updated is None:
            return None
        return _present_comment(updated, contracted=contracted, expand=expand)


def _present_comment(row, *, contracted: bool, expand: bool) -> dict:
    d = dict(row)
    cid = int(d["id"]) if contracted else int(d["num"]) if (expand and d.get("num") is not None) else int(d.get("_rowid") or d.get("rowid") or 0)
    created = d.get("created") or d.get("ts") or ""
    updated = d.get("updated") or created
    return {
        "id": cid,
        "version": int(d.get("version") or 1),
        "author": d["author"],
        "body": d.get("body") or "",
        "ts": created,
        "created": created,
        "updated": updated,
        "audience": d.get("audience") or "internal",
    }


def task_comments(tid: str | int, connect=reader) -> list[dict]:
    with connect() as c:
        row = _select_task_row(c, tid)
        if row is None:
            return []
        contracted = is_contracted(c)
        expand = has_identity_expand(c)
        if contracted:
            rows = c.execute("SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", (int(row["id"]),))
        else:
            order = "num" if expand else "rowid"
            rows = c.execute(f"SELECT rowid AS _rowid, * FROM task_comments WHERE task = ? ORDER BY {order}",  # noqa: S608
                             (row["id"],))
        return [_present_comment(r, contracted=contracted, expand=expand) for r in rows]


def versioned_comments(tid: str | int, connect=reader) -> list[dict]:
    return task_comments(tid, connect=connect)


def tasks_replied(repo: str) -> list[dict]:
    with reader() as c:
        contracted = is_contracted(c)
        if contracted:
            rows = c.execute(
                "SELECT * FROM tasks WHERE repo = ? AND state = 'needs-you' AND fault = 0 ORDER BY created, id",
                (repo,),
            ).fetchall()
            out = []
            for r in rows:
                last = c.execute(
                    "SELECT author FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (int(r["id"]),),
                ).fetchone()
                if last and _db_is_person(c, last["author"]):
                    out.append(_present_task_row(c, r, include_relations=False))
            return out
        rows = c.execute(
            "SELECT rowid AS _rowid, * FROM tasks WHERE repo = ? AND state IN ('needs-info', 'needs-you') "
            "ORDER BY created, rowid",
            (repo,),
        ).fetchall()
        out = []
        for r in rows:
            order = "num" if has_identity_expand(c) else "rowid"
            last = c.execute(
                f"SELECT author FROM task_comments WHERE task = ? ORDER BY {order} DESC LIMIT 1",  # noqa: S608
                (r["id"],),
            ).fetchone()
            if last and _db_is_person(c, last["author"]):
                out.append(_present_task_row(c, r, include_relations=False))
        return out
