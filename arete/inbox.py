from __future__ import annotations

import builtins
import json
import threading
from collections.abc import Callable
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import timedelta

from . import (
    api_http,
    config,
    contract,
    control,
    delivery,
    oncall,
    owner_asks,
    paths,
    pipeline,
    principals,
    retention,
    secretstore,
    tasks,
    tracker,
    triage,
    util,
    webhooks,
    webpush,
    workspace,
)
from .store import approvals as approvals_store
from .store import asks as asks_store
from .store import db as foundation
from .store import events as event_store
from .store import evidence as evidence_store
from .store import idempotency as claims_store
from .store import inbox as inbox_store
from .store import incidents as incident_store
from .store import loops as loop_store
from .store import runs as run_store
from .store import workspaces as workspaces_store

_RESOLVING: ContextVar[str | None] = ContextVar("inbox_resolving", default=None)
_SECRETS_LOCK = threading.Lock()
_FALLBACK_SECRETS: dict[tuple[str, str], str] = {}

PRESENCE_TTL_MINUTES = 10
RESERVED_MANUAL_PREFIXES = (
    "presence:",
    "approval:",
    "ask:",
    "run:",
    "proposal:",
    "skipped:",
    "ticket:",
    "chat:",
    "incident:",
    "workspace:",
    "evidence:",
    "config:",
    "queue:",
    "outbox:",
)


def server_secrets(server=None) -> dict[tuple[str, str], str]:
    if server is None:
        return _FALLBACK_SECRETS
    if not hasattr(server, "pending_secrets"):
        server.pending_secrets = {}
    return server.pending_secrets


def _approvals_items(c) -> list[dict]:
    rows = [
        approvals_store.approval_of(r)
        for r in c.execute(
            "SELECT a.*, COALESCE(r.repo, '') AS run_repo FROM approvals a "  # noqa: S608
            "LEFT JOIN runs r ON r.id = a.run_id "
            f"WHERE a.status IN ({','.join('?' * len(approvals_store.DECIDABLE))}) "
            "ORDER BY a.id DESC",
            approvals_store.DECIDABLE,
        )
    ]
    items = []
    for a in rows:
        emit = delivery.emit_of(a)
        repo = delivery.repo_of(a)
        try:
            detail_text = emit.detail(a["emit"]["item"])
        except (KeyError, TypeError, ValueError):
            detail_text = ""
        row_view = {
            "id": a["id"],
            "loop": a["loop"],
            "run_id": a["run_id"],
            "created": a["created"],
            "repo": repo,
            "what": a["summary"],
            "plain_summary": a["plain_summary"],
            "failed": a["result"] if a["status"] == "failed" else "",
            "held": a["emit"].get("held", ""),
            "review": delivery.review_shown(a),
            "approving": emit.approving,
            "detail": detail_text,
            "stamp": f"{a['status']}:{a['decided'] or ''}",
        }
        items.append({
            "kind": "approval",
            "subject": f"approval:{a['id']}",
            "task_id": "",
            "run_id": a["run_id"] or "",
            "line": repo or a["loop"] or "",
            "priority": 2,
            "title": a["plain_summary"] or f"{a['loop']}: {a['summary']}",
            "detail": row_view,
            "verbs": ["approve", "reject"],
            "options": [],
        })
    return items


def _asks_items(c) -> list[dict]:
    rows = asks_store.open_asks(connect=lambda: nullcontext(c), owner_only=True, group_runners=True)
    items = []
    for raw in rows:
        ask = owner_asks.unstaged(raw)
        questions = ask["questions"]
        if not questions:
            continue
        q_kinds = [q.get("kind") for q in questions]
        if all(k == "choice" for k in q_kinds):
            kind = "choice"
            verbs = ["choose", "answer"]
            options = [q.get("options", []) for q in questions]
        elif all(k == "done" for k in q_kinds):
            kind = "continue"
            verbs = ["continue", "answer"]
            options = []
        else:
            kind = "freeform"
            verbs = ["answer"]
            options = []
        first_q = questions[0]
        title = first_q.get("header") or first_q.get("question") or f"{ask['repo']} {ask['ticket']}"
        detail = {
            **{k: ask[k] for k in ("id", "repo", "ticket", "run", "step", "asked", "why", "tried", "questions")},
            "stamp": ask["asked"],
        }
        items.append({
            "kind": kind,
            "subject": f"ask:{ask['id']}",
            "task_id": str(ask["ticket"] or ""),
            "run_id": ask["run"] or "",
            "line": ask["repo"] or "",
            "priority": 1,
            "title": title,
            "detail": detail,
            "verbs": verbs,
            "options": options,
        })
    return items


def _runs_items(c) -> list[dict]:
    private_clause = (
        "AND NOT EXISTS (SELECT 1 FROM private_evidence e WHERE e.repo = r.repo "
        "  AND e.ticket = CAST(r.issue AS TEXT) AND e.status = 'open') "
        if evidence_store.available(c)
        else ""
    )
    rows = [
        dict(r)
        for r in c.execute(
            f"SELECT r.* FROM runs r WHERE r.kind = 'pipeline' AND r.status = 'needs-info' "  # noqa: S608
            f"AND {run_store.WHOLE_RUN} AND {run_store.LIVE_RUN} "
            "AND NOT EXISTS (SELECT 1 FROM asks a WHERE a.run = r.id "
            "  AND (a.status = 'open' OR (a.status = 'answered' AND a.taken_by = ''))) "
            f"{private_clause}"
            "AND NOT EXISTS (SELECT 1 FROM runs later WHERE later.kind = 'pipeline' "
            f"  AND later.repo = r.repo AND later.issue = r.issue AND {run_store.WHOLE_RUN} "
            f"  AND {run_store.LIVE_RUN} AND {run_store.later_than(c, 'later', 'r')}) "
            f"ORDER BY r.started, {run_store.seq_col(c, 'r')}"
        )
    ]
    items = []
    for r in rows:
        label = tracker.run_ticket_name(r) if r.get("issue") else r["id"]
        summary = (r.get("summary") or "").strip()
        title = f"{label}: {summary}" if summary else f"{label}: needs info"
        detail = {
            "id": r["id"],
            "repo": r["repo"] or "",
            "issue": str(r["issue"] or ""),
            "summary": summary,
            "started": r["started"] or "",
            "finished": r["finished"] or "",
            "stamp": f"{r['id']}:{r['finished'] or r['started'] or ''}",
        }
        items.append({
            "kind": "freeform",
            "subject": f"run:{r['id']}",
            "task_id": str(r["issue"] or ""),
            "run_id": r["id"],
            "line": r["repo"] or "",
            "priority": 1,
            "title": title,
            "detail": detail,
            "verbs": ["answer"],
            "options": [],
        })
    return items


def _proposals_items(c) -> list[dict]:
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM proposals WHERE status IN ('memory', 'hand', 'approve', 'filing') ORDER BY id DESC"
        )
    ]
    items = []
    for p in rows:
        priority = 2 if p["status"] in ("approve", "filing") else 3
        detail = {
            "id": p["id"],
            "repo": p["repo"] or "",
            "file": p["file"] or "",
            "change": p["change"] or "",
            "why": p["why"] or "",
            "status": p["status"],
            "stamp": f"{p['status']}:{p['id']}",
        }
        items.append({
            "kind": "proposal",
            "subject": f"proposal:{p['id']}",
            "task_id": "",
            "run_id": p["run_id"] or "",
            "line": p["repo"] or "",
            "priority": priority,
            "title": p["file"] or p["change"] or f"Proposal #{p['id']}",
            "detail": detail,
            "verbs": ["accept", "decline"],
            "options": [],
        })
    return items


def _skipped_items(c) -> list[dict]:
    running = {
        (r["repo"], str(r["issue"]))
        for r in c.execute("SELECT repo, issue FROM runs WHERE status = 'running' AND issue IS NOT NULL")
    }
    cfg, problem = config.load_or_problem()
    unblock_repos = set() if problem else set(cfg.unblock_repos)
    items = []
    for name, v in sorted(pipeline.skipped_tickets().items()):
        if not isinstance(v, dict):
            continue
        if v.get("blocker_kind") == tracker.DEPENDENCY_WAIT:
            continue
        repo = v.get("repo") or ""
        tid = str(v.get("id") or "")
        if (repo, tid) in running or repo in unblock_repos:
            continue
        can_run = bool(repo)
        detail = {
            "ticket": name,
            "repo": repo,
            "id": tid,
            "title": v.get("title") or "",
            "why": v.get("why") or "",
            "since": v.get("since") or "",
            "url": v.get("url") or "",
            "can_run": can_run,
            "stamp": v.get("since") or "",
        }
        items.append({
            "kind": "candidate",
            "subject": f"skipped:{name}",
            "task_id": tid,
            "run_id": "",
            "line": repo,
            "priority": 2,
            "title": f"{name}: {v.get('title') or v.get('why') or ''}".rstrip(": "),
            "detail": detail,
            "verbs": ["accept", "decline"] if can_run else ["decline"],
            "options": [],
        })
    return items


def _ready_items(c) -> list[dict]:
    from . import board

    cfg, problem = config.load_or_problem()
    if problem:
        return []
    try:
        view = triage.view(connect=lambda: nullcontext(c))
        ready_rows = board.ready_in_one_click(view)
    except Exception:
        return []
    items = []
    for r in ready_rows:
        detail = {**r, "stamp": r.get("verdict") or ""}
        title = f"{r['ref']}: {r['title']}" if r.get("title") else r["ref"]
        items.append({
            "kind": "candidate",
            "subject": f"ticket:{r['ref']}",
            "task_id": r["ref"],
            "run_id": "",
            "line": r.get("repo") or "",
            "priority": 2,
            "title": title,
            "detail": detail,
            "verbs": ["accept", "decline"],
            "options": [],
        })
    return items


def _chats_items(c) -> list[dict]:
    from . import chat

    cfg, _ = config.load_or_problem()
    rows = [dict(r) for r in c.execute("SELECT * FROM chats WHERE draft_title != '' ORDER BY updated DESC, id DESC")]
    items = []
    for row in rows:
        draft = chat.draft_of(row, cfg)
        if not draft:
            continue
        detail = {**draft, "updated": row["updated"] or "", "stamp": draft["fingerprint"]}
        items.append({
            "kind": "proposal",
            "subject": f"chat:{row['id']}/draft",
            "task_id": "",
            "run_id": "",
            "line": draft.get("repo") or row.get("project") or "",
            "priority": 2,
            "title": row["draft_title"],
            "detail": detail,
            "verbs": ["accept", "decline"],
            "options": [],
        })
    return items


def _alerts_items(c) -> list[dict]:
    rows = [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY opened")]
    items = []
    for a in rows:
        detail = {**a, "stamp": a["opened"]}
        items.append({
            "kind": "alert",
            "subject": f"alert:{a['key']}",
            "task_id": "",
            "run_id": "",
            "line": a.get("repo") or "",
            "priority": 2,
            "title": a["key"],
            "detail": detail,
            "verbs": ["continue"],
            "options": [],
        })
    return items


def _incidents_items(c) -> list[dict]:
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT id, class, source, repo, signature, waiting, last_seen, decision FROM incidents "
            "WHERE state = 'open' AND phase = 'blocked' ORDER BY id"
        )
    ]
    items = []
    for r in rows:
        title = f"Incident #{r['id']}: {r['signature']}" if r.get("signature") else f"Incident #{r['id']}"
        detail = {
            "id": r["id"],
            "class": r["class"],
            "source": r["source"],
            "repo": r["repo"] or "",
            "signature": r["signature"],
            "waiting": r["waiting"],
            "last_seen": r["last_seen"],
            "decision": r["decision"],
            "stamp": f"{r['id']}:{r['waiting']}",
        }
        items.append({
            "kind": "alert",
            "subject": f"incident:{r['id']}",
            "task_id": "",
            "run_id": "",
            "line": r.get("repo") or r.get("source") or "",
            "priority": 2,
            "title": title,
            "detail": detail,
            "verbs": ["retry", "dismiss"],
            "options": [],
        })
    return items


def _workspaces_items(c) -> list[dict]:
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspaces'").fetchone():
        return []
    rows = [
        workspaces_store._decode(r)
        for r in c.execute("SELECT * FROM workspaces WHERE state = 'held' ORDER BY released_at, id")
    ]
    now = util.now()
    items = []
    for row in rows:
        try:
            age_days = max(0, int((now - util.parse_iso(row["made_at"])).total_seconds() // 86400))
        except (ValueError, TypeError):
            age_days = 0
        detail = {
            "id": row["id"],
            "name": row["name"],
            "repo": row["repo"] or "",
            "ticket": row["ticket"] or "",
            "bytes": row["bytes"],
            "age_days": age_days,
            "held_by": builtins.list(row["held_by"]),
            "owner_run": row["owner_run"] or "",
            "last_sweep_error": row["last_sweep_error"] or "",
            "stamp": f"{row['id']}:{row['made_at']}",
        }
        items.append({
            "kind": "workspace",
            "subject": f"workspace:{row['id']}",
            "task_id": str(row["ticket"] or ""),
            "run_id": row["owner_run"] or "",
            "line": row["repo"] or "",
            "priority": 3,
            "title": f"Held workspace {row['name']}",
            "detail": detail,
            "verbs": ["keep", "remove"],
            "options": [],
        })
    return items


def _evidence_items(c) -> list[dict]:
    if not evidence_store.available(c):
        return []
    rows = evidence_store.list_open(connect=lambda: nullcontext(c))
    items = []
    for row in rows:
        label = row.get("ticket_ref") or row["ticket"]
        name = row.get("name") or row["ref_key"]
        detail = {
            "id": row["id"],
            "repo": row["repo"],
            "ticket": row["ticket"],
            "ticket_ref": label,
            "tracker": row["tracker"],
            "run_id": row["run_id"],
            "ref_key": row["ref_key"],
            "ref_kind": row["ref_kind"],
            "source": row["source"],
            "name": name,
            "original_url": row["original_url"],
            "attachment_id": row["attachment_id"],
            "media_kind": row["media_kind"],
            "failure_status": row["failure_status"],
            "failure_detail": row["failure_detail"],
            "last_error": row["last_error"],
            "opened": row["opened"],
            "updated": row["updated"],
            "private": True,
            "stamp": f"{row['id']}:{row['opened']}",
        }
        items.append({
            "kind": "evidence",
            "subject": f"evidence:{row['id']}",
            "task_id": str(row["ticket"] or ""),
            "run_id": row["run_id"] or "",
            "line": row["repo"] or "",
            "priority": 1,
            "title": f"{label}: missing evidence {name}",
            "detail": detail,
            "verbs": ["retry", "dismiss"],
            "options": [],
        })
    return items


def _config_paused_lines(problem: config.Problem, cfg: config.Config) -> list[str]:
    scope = problem.scope
    if scope == "global":
        repos = [r.name for r in cfg.repos]
        return [*repos, "agent loops", "oncall"] if repos else ["all launches"]
    if scope.startswith("layer:"):
        return [scope.removeprefix("layer:")]
    if scope.startswith("step:"):
        return [scope]
    if scope.startswith("mcp:"):
        return [scope, "agent loops"]
    if scope == "oncall":
        return ["oncall"]
    return [scope] if scope else ["all launches"]


def _config_items(c) -> list[dict]:
    cfg, _ = config.load_or_problem()
    items = []
    if cfg.problems:
        grouped: dict[tuple[str, str], list[config.Problem]] = {}
        for problem in cfg.problems:
            if problem.scope.startswith("layer:"):
                repo_name = problem.scope.removeprefix("layer:")
                key = (f"config:step_settings/{repo_name}", repo_name)
            else:
                key = ("config:step_settings/defaults", "")
            grouped.setdefault(key, []).append(problem)
        for (subject, line), file_problems in grouped.items():
            entries = [
                {
                    "path": p.path,
                    "section": p.section,
                    "name": p.name,
                    "value": p.value,
                    "scope": p.scope,
                    "pauses": _config_paused_lines(p, cfg),
                    "text": p.text(),
                }
                for p in file_problems
            ]
            first = file_problems[0]
            label = f"layer {line}" if line else "config.toml"
            if first.section or first.name:
                title = f"Invalid config in {label}: {f'{first.section} {first.name}'.strip()}"
            else:
                title = f"{label} does not load"
            detail = {
                "path": first.path,
                "problems": entries,
                "summary": "; ".join(p.text() for p in file_problems),
                "stamp": util.digest(json.dumps(entries, sort_keys=True, default=str)),
            }
            items.append({
                "kind": "config_invalid",
                "subject": subject,
                "task_id": "",
                "run_id": "",
                "line": line,
                "priority": 1,
                "title": title,
                "detail": detail,
                "verbs": ["continue"],
                "options": [],
            })
    ret_path = retention.policy_file()
    if ret_path.is_file():
        try:
            retention.load(ret_path)
        except Exception as e:
            detail_text = f"{type(e).__name__}: {e}"
            entry = {
                "path": str(ret_path),
                "section": "",
                "name": "",
                "value": detail_text,
                "scope": "global",
                "pauses": ["retention"],
                "text": detail_text,
            }
            items.append({
                "kind": "config_invalid",
                "subject": "config:retention/defaults",
                "task_id": "",
                "run_id": "",
                "line": "",
                "priority": 1,
                "title": f"{retention.FILENAME} does not load",
                "detail": {
                    "path": str(ret_path),
                    "problems": [entry],
                    "summary": detail_text,
                    "stamp": util.digest(json.dumps([entry], sort_keys=True, default=str)),
                },
                "verbs": ["continue"],
                "options": [],
            })
    return items


def queue_dead_spec(row: dict, events_count: int) -> dict:
    summary = (row.get("dead_reason") or "").strip()
    title = f"{row['loop']}: {util.first_line(summary, 120)}" if summary else f"{row['loop']}: dead queue item"
    return {
        "kind": "queue.dead",
        "subject": f"queue:{row['id']}",
        "task_id": "",
        "run_id": str(row.get("run_id") or ""),
        "line": str(row.get("loop") or ""),
        "priority": 2,
        "title": title,
        "detail": {
            "loop": row["loop"],
            "run_id": str(row.get("run_id") or ""),
            "summary": row.get("dead_reason") or "",
            "events": int(events_count),
            "attempts": int(row.get("attempts") or 0),
        },
        "verbs": ["requeue", "dismiss"],
        "options": [],
    }


def raise_queue_dead(c, row: dict, events_count: int) -> tuple[dict, int | None, bool] | None:
    if not inbox_store.available(c):
        return None
    spec = queue_dead_spec(row, events_count)
    return inbox_store.raise_item(
        c,
        kind=spec["kind"],
        subject=spec["subject"],
        priority=spec["priority"],
        title=spec["title"],
        detail=spec["detail"],
        verbs=spec["verbs"],
        options=spec["options"],
        task_id=spec["task_id"],
        run_id=spec["run_id"],
        line=spec["line"],
    )


def _queue_dead_items(c) -> list[dict]:
    rows = c.execute(
        "SELECT id, loop, run_id, dead_reason, attempts, COALESCE(json_array_length(payload), 0) AS events_count "
        "FROM queue WHERE dead_at IS NOT NULL ORDER BY id"
    ).fetchall()
    return [queue_dead_spec(dict(r), r["events_count"]) for r in rows]


def _outbox_items(c) -> list[dict]:
    from . import outbox
    from .store import outbox as outbox_store

    if not outbox_store.available(c):
        return []
    items = []
    for row in outbox_store.inbox_pending_items(c):
        ob_id = outbox_store.format_id(row["id"])
        title = row["plain_summary"] or row["summary"] or f"{row['kind']} on {row['object'] or row['target']}"
        detail = {
            "outbox_id": ob_id,
            "id": row["id"],
            "rev": row["rev"],
            "version": row["version"],
            "state": row["state"],
            "target": row["target"],
            "kind": row["kind"],
            "object": row["object"],
            "audience": row["audience"],
            "summary": row["summary"],
            "stamp": f"{row['state']}:{row['rev']}:{row['version']}",
        }
        items.append({
            "kind": "outbox",
            "subject": f"outbox:{row['id']}",
            "task_id": str(row["task_id"] or ""),
            "run_id": str(row["run_id"] or ""),
            "line": str(row["loop"] or row["object"] or row["target"] or ""),
            "priority": 2,
            "title": title,
            "detail": detail,
            "verbs": ["approve", "reject"],
            "options": [],
        })
    cutoff = util.iso(util.now() - timedelta(seconds=outbox.STALE_SENDING_SECONDS))
    for row in outbox_store.inbox_stale_sending_items(c, cutoff):
        ob_id = outbox_store.format_id(row["id"])
        title = row["plain_summary"] or row["summary"] or f"Sending {ob_id}"
        detail = {
            "outbox_id": ob_id,
            "id": row["id"],
            "rev": row["rev"],
            "version": row["version"],
            "state": row["state"],
            "target": row["target"],
            "kind": row["kind"],
            "object": row["object"],
            "audience": row["audience"],
            "sending_since": row["sending_since"],
            "stamp": f"sending:{row['sending_since']}",
        }
        items.append({
            "kind": "outbox",
            "subject": f"outbox:{row['id']}",
            "task_id": str(row["task_id"] or ""),
            "run_id": str(row["run_id"] or ""),
            "line": str(row["loop"] or row["object"] or row["target"] or ""),
            "priority": 3,
            "title": title,
            "detail": detail,
            "verbs": [],
            "options": [],
        })
    return items


ADAPTERS: dict[str, tuple[str, Callable[..., list[dict]]]] = {
    "approvals": ("approval:%", _approvals_items),
    "asks": ("ask:%", _asks_items),
    "runs": ("run:%", _runs_items),
    "evidence": ("evidence:%", _evidence_items),
    "proposals": ("proposal:%", _proposals_items),
    "skipped": ("skipped:%", _skipped_items),
    "ready": ("ticket:%", _ready_items),
    "chats": ("chat:%/draft", _chats_items),
    "alerts": ("alert:%", _alerts_items),
    "incidents": ("incident:%", _incidents_items),
    "workspaces": ("workspace:%", _workspaces_items),
    "config": ("config:%", _config_items),
    "queue": ("queue:%", _queue_dead_items),
    "outbox": ("outbox:%", _outbox_items),
}


def _clear_resolution(c, source: str, subject: str) -> str:
    if source == "asks" and subject.startswith("ask:"):
        ask_id = subject.removeprefix("ask:")
        row = c.execute("SELECT status FROM asks WHERE id = ?", (ask_id,)).fetchone()
        if row and row["status"] in ("stopped", "expired"):
            return row["status"]
    if source == "evidence" and subject.startswith("evidence:") and evidence_store.available(c):
        ev_id = subject.removeprefix("evidence:")
        row = c.execute("SELECT status FROM private_evidence WHERE id = ?", (ev_id,)).fetchone()
        if row and row["status"] in ("uploaded", "access_restored", "stopped"):
            return "dismissed" if row["status"] == "stopped" else row["status"]
    return "cleared"


def sync(c, source: str) -> None:
    if not inbox_store.available(c):
        return
    if source == "all":
        for name in ADAPTERS:
            sync(c, name)
        return
    pattern, producer = ADAPTERS[source]
    resolving = _RESOLVING.get()
    desired = producer(c)
    wanted = {(spec["kind"], spec["subject"]): spec for spec in desired}
    existing_rows = inbox_store.unresolved_for_pattern(c, pattern)
    present_keys: set[tuple[str, str]] = set()
    for row in existing_rows:
        key = (row["kind"], row["subject"])
        if row["subject"] == resolving:
            present_keys.add(key)
            continue
        spec = wanted.get(key)
        if spec is None:
            inbox_store.resolve_item(c, row["id"], resolution=_clear_resolution(c, source, row["subject"]))
            continue
        present_keys.add(key)
        inbox_store.update_item(
            c,
            row["id"],
            title=spec["title"],
            detail=spec["detail"],
            priority=spec["priority"],
            verbs=spec["verbs"],
            options=spec["options"],
            task_id=spec["task_id"],
            run_id=spec["run_id"],
            line=spec["line"],
        )
    for key, spec in wanted.items():
        if key in present_keys or spec["subject"] == resolving:
            continue
        stamp = (spec["detail"] or {}).get("stamp")
        if stamp:
            prev = inbox_store.newest_resolved_by_subject(c, spec["subject"])
            if prev and prev["resolution"] != "cleared" and prev["detail"].get("stamp") == stamp:
                continue
        inbox_store.raise_item(
            c,
            kind=spec["kind"],
            subject=spec["subject"],
            priority=spec["priority"],
            title=spec["title"],
            detail=spec["detail"],
            verbs=spec["verbs"],
            options=spec["options"],
            task_id=spec["task_id"],
            run_id=spec["run_id"],
            line=spec["line"],
        )


def sync_source(source: str) -> None:
    if not paths.DB_FILE.exists():
        return
    try:
        with foundation.tx() as c:
            sync(c, source)
    except (foundation.NoDatabase, foundation.IncompatibleSchema):
        pass


def sync_all(c=None) -> None:
    if c is not None:
        sync(c, "all")
        return
    if not paths.DB_FILE.exists():
        return
    with foundation.tx() as conn:
        sync(conn, "all")


def list(
    *,
    state: str | tuple[str, ...] | None = "open",
    kind: str | None = None,
    priority: int | None = None,
    task: str | None = None,
    run: str | None = None,
    line: str | None = None,
    connect=foundation.reader,
) -> list[dict]:
    return inbox_store.list_items(
        state=state, kind=kind, priority=priority, task=task, run=run, line=line, connect=connect
    )


def open_count(connect=foundation.reader) -> int:
    return inbox_store.count_open(connect=connect)


def triage_screen(connect=foundation.reader) -> dict:
    items = inbox_store.list_items(state="open", connect=connect)
    return {
        "items": items,
        "by_priority": {
            "high": [i for i in items if i["priority"] == 1],
            "med": [i for i in items if i["priority"] == 2],
            "low": [i for i in items if i["priority"] == 3],
        },
        "counts": inbox_store.counts_by_state(connect=connect),
    }


def subject_object(item: dict, connect=foundation.reader) -> dict | None:
    subj = item["subject"]
    kind, _, ident = subj.partition(":")
    with connect() as c:
        if kind == "approval" and ident.isdecimal():
            row = c.execute("SELECT * FROM approvals WHERE id = ?", (int(ident),)).fetchone()
            return approvals_store.approval_of(row) if row else None
        if kind == "ask":
            return asks_store.ask_get(ident, connect=lambda: nullcontext(c))
        if kind == "run":
            return run_store.run_get(ident, connect=lambda: nullcontext(c))
        if kind == "proposal" and ident.isdecimal():
            return approvals_store.proposal(int(ident), connect=lambda: nullcontext(c))
        if kind == "incident" and ident.isdecimal():
            return incident_store.incident(int(ident), connect=lambda: nullcontext(c))
        if kind == "alert":
            row = c.execute("SELECT * FROM alerts WHERE key = ?", (ident,)).fetchone()
            return dict(row) if row else None
        if kind == "chat" and ident.endswith("/draft"):
            cid = ident.removesuffix("/draft")
            if cid.isdecimal():
                row = c.execute("SELECT * FROM chats WHERE id = ?", (int(cid),)).fetchone()
                return dict(row) if row else None
        if kind == "workspace" and ident.isdecimal():
            row = c.execute("SELECT * FROM workspaces WHERE id = ?", (int(ident),)).fetchone()
            return workspaces_store._decode(row) if row else None
        if kind == "evidence":
            from . import private_evidence

            row = evidence_store.get(ident, connect=lambda: nullcontext(c))
            return private_evidence.safe_view(row) if row else None
        if kind == "queue" and ident.isdecimal():
            return loop_store.queue_item(int(ident), c)
        if kind == "outbox" and ident.isdecimal():
            from .store import outbox as outbox_store

            return outbox_store.get_detail(int(ident), connect=lambda: nullcontext(c))
        if kind == "presence":
            actor, _, key = ident.partition("/")
            pending = inbox_store.get_pending(c, actor, key)
            return pending
        if kind == "skipped":
            return pipeline.skipped_tickets().get(ident)
        if kind == "ticket":
            view = triage.view(connect=lambda: nullcontext(c))
            return next((r for r in view["rows"] if r.get("ref") == ident), None)
        if kind == "config":
            return dict(item["detail"])
    return None


def get(item_id: str | int, connect=foundation.reader) -> dict:
    item = inbox_store.get(item_id, connect=connect)
    if item is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    return {"item": item, "subject_object": subject_object(item, connect=connect)}


def history(item_id: str | int, connect=foundation.reader) -> list[dict]:
    rows = inbox_store.history(item_id, connect=connect)
    if rows is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    return rows


def raise_manual(body: dict, *, credential=None) -> dict:
    if "ask" in body and body["ask"] is not None:
        ask = body["ask"]
        if not isinstance(ask, dict):
            raise contract.Problem("validation", "ask must be an object")
        questions = ask.get("questions")
        if not isinstance(questions, builtins.list) or not questions:
            raise contract.Problem("validation", "ask.questions must be a non-empty list")
        run_id = str(body.get("run_id") or ask.get("run") or "").strip()
        if not run_id:
            raise contract.Problem("validation", "ask requires run_id")
        run = run_store.run_get(run_id)
        if not run:
            raise contract.Problem("not_found", f"No run {run_id}")
        ask_id = str(ask.get("id") or f"{run_id}-a{util.now().strftime('%H%M%S%f')}")
        step = str(ask.get("step") or "ask")
        call = str(ask.get("call") or "ask")
        why = str(ask.get("why") or "")
        tried = str(ask.get("tried") or "")
        session = ask.get("session") if isinstance(ask.get("session"), dict) else {}
        with foundation.tx() as c:
            asks_store.ask_add(
                ask_id, run_id, run["repo"] or "", str(run["issue"] or ""), step, call, why, tried,
                questions, session, recovery_route="owner",
            )
            item = inbox_store.get_open_by_subject(f"ask:{ask_id}", connect=lambda: nullcontext(c))
        if not item:
            raise contract.Problem("validation", "Could not raise ask item")
        return item

    subject = str(body.get("subject") or "").strip()
    if not subject:
        raise contract.Problem("validation", "subject is required when ask is not provided")
    if subject.lower().startswith(RESERVED_MANUAL_PREFIXES):
        raise contract.Problem("validation", f"Subject prefix {subject.partition(':')[0]!r} is reserved for system items.")
    kind = str(body.get("kind") or "freeform").strip()
    if kind not in inbox_store.KINDS:
        raise contract.Problem("validation", f"Unsupported inbox kind {kind!r}")
    raw_priority = body.get("priority", 2)
    if isinstance(raw_priority, bool):
        raise contract.Problem("validation", "priority must be 1, 2, or 3")
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        raise contract.Problem("validation", "priority must be 1, 2, or 3") from None
    if priority not in inbox_store.PRIORITIES:
        raise contract.Problem("validation", "priority must be 1, 2, or 3")
    title = str(body.get("title") or subject)
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    verbs = body.get("verbs") if isinstance(body.get("verbs"), builtins.list) else ["continue"]
    options = body.get("options") if isinstance(body.get("options"), builtins.list) else []
    with foundation.tx() as c:
        item, _, _ = inbox_store.raise_item(
            c,
            kind=kind,
            subject=subject,
            priority=priority,
            title=title,
            detail=detail,
            verbs=[str(v) for v in verbs],
            options=options,
            task_id=str(body.get("task_id") or ""),
            run_id=str(body.get("run_id") or ""),
            line=str(body.get("line") or ""),
        )
    return item


def snooze(item_id: str | int, *, base_version: int, until: str | None = None, until_changed: bool = False) -> dict:
    numeric = inbox_store.parse_id(item_id)
    if numeric is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    until_text = str(until or "").strip()
    if not until_text and not until_changed:
        raise contract.Problem("validation", "Provide until or until_changed to snooze an item.")
    if until_text:
        try:
            util.parse_iso(until_text)
        except (ValueError, TypeError):
            raise contract.Problem("validation", "until must be an ISO timestamp.") from None
    with foundation.tx() as c:
        current = inbox_store.get(numeric, connect=lambda: nullcontext(c))
        if current is None:
            raise contract.Problem("not_found", f"No inbox item {item_id}")
        if current["state"] == "resolved":
            raise contract.Problem("already_resolved", f"Inbox item {current['id']} is already resolved.", current=current)
        if current["version"] != base_version:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=current)
        updated, _, ok = inbox_store.snooze_item(
            c, numeric, base_version=base_version, until=until_text, until_changed=bool(until_changed)
        )
        if not ok:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=updated)
        return updated


def shelve(item_id: str | int, *, base_version: int, reason: str, until: str | None = None) -> dict:
    numeric = inbox_store.parse_id(item_id)
    if numeric is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise contract.Problem("validation", "Shelving an item requires a reason.")
    until_text = str(until or "").strip()
    if until_text:
        try:
            util.parse_iso(until_text)
        except (ValueError, TypeError):
            raise contract.Problem("validation", "until must be an ISO timestamp.") from None
    with foundation.tx() as c:
        current = inbox_store.get(numeric, connect=lambda: nullcontext(c))
        if current is None:
            raise contract.Problem("not_found", f"No inbox item {item_id}")
        if current["state"] == "resolved":
            raise contract.Problem("already_resolved", f"Inbox item {current['id']} is already resolved.", current=current)
        if current["version"] != base_version:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=current)
        updated, _, ok = inbox_store.shelve_item(
            c, numeric, base_version=base_version, reason=reason_text, until=until_text
        )
        if not ok:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=updated)
        return updated


def _redact_ask_answers(questions: list[dict], answers: list[dict]) -> list[dict]:
    safe = []
    for i, ans in enumerate(answers):
        if isinstance(ans, dict) and (
            "secret" in ans or (i < len(questions) and questions[i].get("kind") == "secret")
        ):
            safe.append({k: ("[redacted]" if k == "secret" else v) for k, v in ans.items()})
        else:
            safe.append(ans)
    return safe


def _build_ask_body(item: dict, verb: str, *, option=None, answer=None, answers=None) -> tuple[dict, str]:
    questions = item["detail"].get("questions") or []
    if answers is not None:
        redacted = _redact_ask_answers(questions, answers)
        return {"answers": answers}, json.dumps(redacted, sort_keys=True)
    if option is not None:
        built = [{"choice": option}]
        return {"answers": built}, json.dumps(built, sort_keys=True)
    if verb == "continue" and all(q.get("kind") == "done" for q in questions):
        built = [{"done": True} for _ in questions]
        return {"answers": built}, json.dumps(built, sort_keys=True)
    if answer is not None:
        built = [{"text": str(answer)} for _ in questions]
        return {"answers": built}, str(answer)
    raise contract.Problem("validation", f"Verb {verb!r} requires answers, option, or answer.")


def _args_summary(op_name: str, values: dict, *, fingerprint: str | None = None) -> dict:
    if op_name == "secret.set":
        return {"scope": values.get("scope"), "name": values.get("name"), "fingerprint": fingerprint}
    if op_name == "secret.remove":
        return {"scope": values.get("scope"), "name": values.get("name")}
    if op_name == "token.issue":
        return {"label": values.get("label"), "scopes": values.get("scopes"), "profile": values.get("profile")}
    if op_name == "token.rotate":
        return {"token": values.get("token"), "grace_seconds": values.get("grace_seconds")}
    if op_name == "webhook.rotate":
        return {"loop": values.get("loop")}
    if op_name == "push.subscribe":
        from urllib.parse import urlsplit

        host = urlsplit(str(values.get("endpoint") or "")).hostname or ""
        return {"endpoint_host": host, "label": values.get("label") or ""}
    return {k: v for k, v in values.items() if k not in ("keys", "value")}


def defer_presence(
    handler,
    op: dict,
    values: dict,
    path_values: dict,
    *,
    hash_body=None,
    secret_value: str | None = None,
    fingerprint: str | None = None,
    before_defer: Callable[[], None] | None = None,
) -> dict:
    from . import dashboard, idempotency

    actor = handler.actor
    key = handler.headers["Idempotency-Key"]
    route = op["path"]
    sha = idempotency.fingerprint(op["method"], route, path_values, values if hash_body is None else hash_body)
    with dashboard.write_claim(handler, key, sha, route, actor=actor) as (context, row):
        if row and row["status"]:
            handler.idempotent_replayed = True
            handler.success_status = row["status"]
            handler.write_receipt = claims_store.receipt(row["last_seq"])
            result = json.loads(row["response"])
            if (
                row["status"] in (200, 201)
                and op["name"] in ("token.issue", "token.rotate", "webhook.rotate")
                and isinstance(result, dict)
                and "text" in result
                and result["text"] != "[shown once]"
            ):
                redacted = {**result, "text": "[shown once]"}
                claims_store.overwrite(actor, key, row["status"], api_http.json_bytes(redacted), row["last_seq"],
                                       expected_status=row["status"])
            return result
        if row:
            raise idempotency.Unconfirmed("The earlier request did not record a result. Check its effect before trying again.")
        if before_defer is not None:
            before_defer()

        expires = util.iso(util.now() + timedelta(minutes=PRESENCE_TTL_MINUTES))
        summary = _args_summary(op["name"], values, fingerprint=fingerprint)
        stored_values = (
            {"scope": values["scope"], "name": values["name"], "fingerprint": fingerprint}
            if op["name"] == "secret.set"
            else values
        )
        req_record = {
            "operation": op["name"],
            "method": op["method"],
            "path": route,
            "values": stored_values,
            "person": handler.person,
            "actor": actor,
            "has_secret": secret_value is not None,
        }
        if fingerprint is not None:
            req_record["fingerprint"] = fingerprint

        with foundation.tx() as c:
            inbox_store.add_pending(c, actor, key, req_record, expires)
            item, raised_seq, _ = inbox_store.raise_item(
                c,
                kind="approval",
                subject=f"presence:{actor}/{key}",
                priority=1,
                title=f"Approve {op['name']} from {actor}",
                detail={
                    "command": op["name"],
                    "actor": actor,
                    "person": handler.person,
                    "key": key,
                    "args": summary,
                    "expires": expires,
                    "stamp": f"{actor}/{key}",
                },
                verbs=["approve", "reject"],
                options=[],
            )
            payload = {
                "state": "awaiting_presence",
                "idem": key,
                "item": item["id"],
                "watch": f"/api/v1/inbox/{item['id']}",
                "expires": expires,
            }
            c.execute(
                "INSERT INTO idempotency (actor,key,route,request_sha,status,response,last_seq,at,via) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (*context, 202, api_http.json_bytes(payload), raised_seq, util.iso(), event_store.provenance()),
            )
            if secret_value is not None:
                secrets_map = server_secrets(handler.server)
                with _SECRETS_LOCK:
                    secrets_map[(actor, key)] = secret_value
        handler.success_status = 202
        handler.write_receipt = claims_store.receipt(raised_seq) if raised_seq else None
        return payload


def _execute_pending_command(req: dict, secret_value: str | None) -> tuple[int, dict]:
    from . import api, credential_http

    op_name = req["operation"]
    values = req["values"]
    person = req["person"]
    actor = req["actor"]

    if op_name == "secret.set":
        scope, name = values["scope"], values["name"]
        kind, provider = api.secret_set_options(scope, name)
        row = secretstore.put(scope, name, secret_value, kind, actor, provider=provider)
        return 200, api.with_seq(api.secret_result(scope, name, row))
    if op_name == "secret.remove":
        return 200, api.with_seq(api.remove_secret(values["scope"], values["name"]))
    if op_name in ("token.issue", "token.rotate"):
        issued = credential_http.issue_or_rotate(op_name, person, values)
        if not issued:
            raise contract.Problem("not_found", "Token could not be rotated.")
        token, text = issued
        return 201, api.with_seq({"token": token, "text": text})
    if op_name == "webhook.rotate":
        text = webhooks.rotate(values["loop"], actor)
        return 200, api.with_seq({"text": text})
    if op_name == "push.subscribe":
        sub = webpush.subscribe_v1(values, principals.contact(person))
        return 201, api.with_seq({"subscription": sub})
    if op_name == "outbox.approve":
        from . import outbox

        updated = outbox.approve(values["item"], rev=values["rev"], base_version=values["base_version"])
        return 200, api.with_seq({"item": updated})
    if op_name == "outbox.retry":
        from . import outbox

        updated = outbox.retry(values["item"], base_version=values["base_version"])
        return 200, api.with_seq({"item": updated})
    if op_name == "outbox.reject":
        from . import outbox

        updated = outbox.reject(values["item"], reason=str(values.get("reason") or ""), base_version=values["base_version"])
        return 200, api.with_seq({"item": updated})
    raise contract.Problem("not_found", f"Unsupported pending operation {op_name}")


def _expire_one_pending(c, pending: dict, secrets_map: dict[tuple[str, str], str]) -> bool:
    actor, key = pending["actor"], pending["key"]
    with _SECRETS_LOCK:
        secrets_map.pop((actor, key), None)
    inbox_store.delete_pending(c, actor, key)
    item = inbox_store.get_open_by_subject(f"presence:{actor}/{key}", connect=lambda: nullcontext(c))
    seq = None
    if item is not None:
        _, seq, _ = inbox_store.resolve_item(c, inbox_store.parse_id(item["id"]), resolution="expired")
    problem = {"code": "presence_expired", "error": "The presence approval expired before it was approved."}
    claims_store.overwrite(actor, key, 409, api_http.json_bytes(problem), seq, expected_status=202, connection=c)
    return True


def expire_pending(c=None, *, startup: bool = False, pending_secrets=None) -> int:
    secrets_map = server_secrets(None) if pending_secrets is None else pending_secrets

    def _run(conn) -> int:
        if not inbox_store.available(conn):
            return 0
        now = util.iso()
        expired = 0
        for row in inbox_store.list_pending(conn):
            key = (row["actor"], row["key"])
            missing_secret = startup and bool(row["request"].get("has_secret")) and key not in secrets_map
            if row["expires"] <= now or missing_secret:
                _expire_one_pending(conn, row, secrets_map)
                expired += 1
        return expired

    if c is not None:
        return _run(c)
    if not paths.DB_FILE.exists():
        return 0
    with foundation.tx() as conn:
        return _run(conn)


def _resolve_presence(
    item: dict,
    *,
    numeric_id: int,
    base_version: int,
    verb: str,
    credential,
    pending_secrets=None,
) -> dict:
    if credential is None or credential.kind != "session":
        raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required")
    ident = item["subject"].removeprefix("presence:")
    actor, _, key = ident.partition("/")
    approver = principals.current()
    secrets_map = server_secrets(None) if pending_secrets is None else pending_secrets

    with foundation.tx() as c:
        current = inbox_store.get(numeric_id, connect=lambda: nullcontext(c))
        if current is None:
            raise contract.Problem("not_found", f"No inbox item {item['id']}")
        if current["state"] == "resolved":
            raise contract.Problem("already_resolved", f"Inbox item {current['id']} is already resolved.", current=current)
        if current["version"] != base_version:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=current)
        pending = inbox_store.get_pending(c, actor, key)
        if pending is None:
            raise contract.Problem("already_resolved", f"Pending command for {current['id']} is gone.", current=current)

        now = util.iso()
        if verb == "reject":
            with _SECRETS_LOCK:
                secrets_map.pop((actor, key), None)
            inbox_store.delete_pending(c, actor, key)
            resolved, seq, ok = inbox_store.resolve_item(
                c,
                numeric_id,
                resolution="rejected",
                answer="reject",
                answered_by=approver,
                answered_at=now,
                base_version=base_version,
            )
            if not ok:
                raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=resolved)
            problem = {"code": "gate_refused", "error": "The owner refused the pending presence request."}
            claims_store.overwrite(actor, key, 403, api_http.json_bytes(problem), seq, expected_status=202, connection=c)
            return {"item": resolved, "result": {"rejected": True}}

        # approve
        has_secret = bool(pending["request"].get("has_secret"))
        with _SECRETS_LOCK:
            secret_value = secrets_map.get((actor, key))
        if pending["expires"] <= now or (has_secret and secret_value is None):
            _expire_one_pending(c, pending, secrets_map)
            refreshed = inbox_store.get(numeric_id, connect=lambda: nullcontext(c))
            raise contract.Problem("presence_expired", "The presence approval expired before it was approved.",
                                   current=refreshed)

        resolved, approval_seq, ok = inbox_store.resolve_item(
            c,
            numeric_id,
            resolution="approved",
            answer="approve",
            answered_by=approver,
            answered_at=now,
            base_version=base_version,
        )
        if not ok:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=resolved)

        req = pending["request"]
        legacy_outbox = False
        if req["operation"] in ("outbox.approve", "outbox.retry", "outbox.reject"):
            from .store import outbox as outbox_store

            ob_row = outbox_store.get_raw(req["values"]["item"], connect=lambda: nullcontext(c))
            legacy_outbox = ob_row is not None and ob_row.get("legacy_approval") is not None

        if legacy_outbox:
            inbox_store.delete_pending(c, actor, key)
        else:
            with principals.acting(actor), event_store.caused_by(approval_seq), event_store.collect_receipts() as collected:
                cmd_status, cmd_result = _execute_pending_command(req, secret_value)
            last_seq = collected[-1][0] if collected else approval_seq
            inbox_store.delete_pending(c, actor, key)
            claims_store.overwrite(actor, key, cmd_status, api_http.json_bytes(cmd_result), last_seq,
                                   expected_status=202, connection=c)
            if has_secret:
                def _drop_secret(k=(actor, key), m=secrets_map):
                    with _SECRETS_LOCK:
                        m.pop(k, None)

                foundation.after_commit(_drop_secret)
            return {"item": resolved, "result": {"approved": True, "status": cmd_status}}

    with principals.acting(actor), event_store.caused_by(approval_seq), event_store.collect_receipts() as collected:
        cmd_status, cmd_result = _execute_pending_command(req, secret_value)
    last_seq = collected[-1][0] if collected else approval_seq
    claims_store.overwrite(actor, key, cmd_status, api_http.json_bytes(cmd_result), last_seq, expected_status=202)
    return {"item": resolved, "result": {"approved": True, "status": cmd_status}}


def _resolve_queue_dead(
    item: dict,
    *,
    numeric_id: int,
    base_version: int,
    verb: str,
) -> dict:
    ident = item["subject"].removeprefix("queue:")
    if not ident.isdecimal():
        raise contract.Problem("validation", f"Invalid queue subject {item['subject']!r}.")
    queue_id = int(ident)
    actor = principals.current()
    now = util.iso()
    missing = False
    with foundation.tx() as c:
        current = inbox_store.get(numeric_id, connect=lambda: nullcontext(c))
        if current is None:
            raise contract.Problem("not_found", f"No inbox item {item['id']}")
        if current["state"] == "resolved":
            raise contract.Problem("already_resolved", f"Inbox item {current['id']} is already resolved.", current=current)
        if current["version"] != base_version:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=current)
        try:
            if verb == "requeue":
                resolved = loop_store.requeue_item(
                    c, queue_id, answered_by=actor, answered_at=now, base_version=base_version
                )
                handler_result = {"queue_id": queue_id, "requeued": True}
            else:
                resolved = loop_store.dismiss_item(
                    c, queue_id, answered_by=actor, answered_at=now, base_version=base_version
                )
                handler_result = {"queue_id": queue_id, "dismissed": True}
        except control.NotFound:
            missing = True
        else:
            if resolved is None:
                refreshed = inbox_store.get(numeric_id, connect=lambda: nullcontext(c)) or current
                raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=refreshed)
            return {"item": resolved, "result": handler_result}
    if missing:
        sync_source("queue")
        refreshed = inbox_store.get(numeric_id) or item
        raise contract.Problem("already_resolved", f"Dead queue item {queue_id} is no longer dead.", current=refreshed)
    raise AssertionError("unreachable")


def resolve(
    item_id: str | int,
    *,
    base_version: int,
    verb: str,
    option=None,
    answer: str | None = None,
    answers: list | None = None,
    reason: str | None = None,
    credential=None,
    publication=None,
    pending_secrets=None,
    allow_legacy_auth: bool = False,
) -> dict:
    from . import chat, dashboard

    numeric = inbox_store.parse_id(item_id)
    if numeric is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    item = inbox_store.get(numeric)
    if item is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    if item["state"] == "resolved":
        raise contract.Problem("already_resolved", f"Inbox item {item['id']} is already resolved.", current=item)
    if item["version"] != base_version:
        raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=item)
    if verb not in item["verbs"]:
        raise contract.Problem("validation", f"Verb {verb!r} is not allowed on {item['id']}; allowed: {', '.join(item['verbs'])}.")

    subject = item["subject"]
    if subject.startswith("presence:"):
        return _resolve_presence(
            item,
            numeric_id=numeric,
            base_version=base_version,
            verb=verb,
            credential=credential,
            pending_secrets=pending_secrets,
        )
    if subject.startswith("queue:"):
        return _resolve_queue_dead(
            item,
            numeric_id=numeric,
            base_version=base_version,
            verb=verb,
        )

    if (item["kind"] == "approval" or subject.startswith("approval:")) and not allow_legacy_auth:
        if credential is None or credential.kind != "session":
            raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required")

    actor = principals.current()
    prefix, _, ident = subject.partition(":")
    handler_result: object = {}
    stored_answer = str(answer or "")
    resolution = ""
    keep_open = False

    token = _RESOLVING.set(subject)
    try:
        if prefix == "approval":
            yes = verb == "approve"
            try:
                handler_result = control.decide(int(ident), yes, publication=publication)
            except control.Refused as e:
                if isinstance(e.__cause__, delivery.AlreadyDecided):
                    current = inbox_store.get(numeric) or item
                    raise contract.Problem("already_resolved", str(e), current=current) from None
                raise
            resolution = "approved" if yes else "rejected"
            stored_answer = verb
        elif prefix == "ask":
            ask_body, stored_answer = _build_ask_body(item, verb, option=option, answer=answer, answers=answers)
            try:
                handler_result = tasks.answer_ask(ident, ask_body)
            except tasks.AlreadyResolved as e:
                current = inbox_store.get(numeric) or item
                raise contract.Problem("already_resolved", str(e), current=current) from None
            resolution = "answered"
        elif prefix == "run":
            text = str(answer or "").strip()
            if not text:
                raise contract.Problem("validation", "answer is required.")
            try:
                handler_result = tasks.reply(ident, text)
            except tasks.AlreadyResolved as e:
                current = inbox_store.get(numeric) or item
                raise contract.Problem("already_resolved", str(e), current=current) from None
            resolution = "answered"
            stored_answer = text
        elif prefix == "proposal":
            pid = int(ident)
            prop = approvals_store.proposal(pid)
            status = (prop or {}).get("status") or item["detail"].get("status")
            if status in ("approve", "filing"):
                if verb == "decline":
                    if status == "filing":
                        raise contract.Problem("invalid_state", f"Proposal #{pid} is filing and cannot be declined.")
                    handler_result = dashboard.reject_rule(pid)
                    resolution = "declined"
                    stored_answer = verb
                else:
                    handler_result = dashboard.approve_rule(pid)
                    keep_open = True
                    stored_answer = verb
            else:
                handler_result = dashboard.forget_proposal(pid)
                resolution = "accepted" if verb == "accept" else "declined"
                stored_answer = verb
        elif prefix == "skipped":
            if verb == "accept":
                handler_result = pipeline.run_skipped(ident)
                resolution = "accepted"
            else:
                handler_result = {"ticket": ident, "declined": True}
                resolution = "declined"
            stored_answer = verb
        elif prefix == "ticket":
            if verb == "accept":
                questions = item["detail"].get("questions") or []
                repo = item["detail"].get("repo") or ""
                said = triage.send(config.load_or_problem()[0], ident, questions, ["" for _ in questions], repo,
                                   publication=publication)
                handler_result = {"ref": ident, "said": said}
                resolution = "accepted"
            else:
                handler_result = {"ref": ident, "declined": True}
                resolution = "declined"
            stored_answer = verb
        elif prefix == "chat" and ident.endswith("/draft"):
            cid = int(ident.removesuffix("/draft"))
            if verb == "accept":
                handler_result = chat.file_draft(cid, publication=publication, draft=item["detail"].get("fingerprint"))
                resolution = "accepted"
            else:
                handler_result = chat.drop_draft(cid)
                resolution = "declined"
            stored_answer = verb
        elif prefix in ("alert", "push", "config"):
            handler_result = {"subject": subject, "continued": True}
            resolution = "continued"
            stored_answer = verb
        elif prefix == "incident":
            inc_id = int(ident)
            if verb == "retry":
                handler_result = oncall.retry(inc_id)
                resolution = "retried"
            else:
                handler_result = oncall.dismiss(inc_id)
                resolution = "dismissed"
            stored_answer = verb
        elif prefix == "workspace":
            ws_id = int(ident)
            row = workspaces_store.get(ws_id)
            if row is None or row["state"] != "held":
                sync_source("workspaces")
                current = inbox_store.get(numeric) or item
                raise contract.Problem("already_resolved", f"Workspace {ws_id} is no longer held.", current=current)
            if verb == "keep":
                updated_ws = workspaces_store.change(row, state="kept", kept_reason="operator", held_by=[],
                                                     last_sweep_error="")
                handler_result = {"workspace": ws_id, "state": (updated_ws or row)["state"]}
                resolution = "kept"
            else:
                if not principals.is_person(actor):
                    raise contract.Problem("forbidden", "Removing a workspace requires a person.")
                ws_path = paths.WORKTREES_DIR / row["name"]
                checkouts = workspaces_store.owner_of(row).checkouts
                left = workspace.scratch_left(ws_path, checkouts)
                if left:
                    roots = ", ".join(str(p) for p in left)
                    raise contract.Problem("invalid_state",
                                           f"Workspace {row['name']} has pending verification scratch: {roots}")
                updated_ws = workspaces_store.change(row, state="released", remove_asked_by=actor, held_by=[],
                                                     last_sweep_error="")
                handler_result = {"workspace": ws_id, "state": (updated_ws or row)["state"]}
                resolution = "removed"
            stored_answer = verb
        elif prefix == "outbox":
            from . import outbox

            ob_id = int(ident)
            if verb == "approve":
                if outbox.needs_presence("outbox.approve", ob_id) and not allow_legacy_auth:
                    if credential is None or credential.kind != "session":
                        raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required")
                rev = item["detail"].get("rev")
                handler_result = outbox.approve(ob_id, rev=rev)
                resolution = "approved"
            else:
                handler_result = outbox.reject(ob_id, reason=str(reason or answer or ""))
                resolution = "rejected"
            stored_answer = verb
        elif prefix == "evidence":
            from . import private_evidence

            try:
                if verb == "retry":
                    handler_result = private_evidence.confirm_access_restored(ident, actor=actor)
                    resolution = "access_restored"
                else:
                    handler_result = private_evidence.dismiss(ident, actor=actor)
                    resolution = "dismissed"
            except private_evidence.EvidenceAlreadyResolved as e:
                _RESOLVING.reset(token)
                token = None
                sync_source("evidence")
                current = inbox_store.get(numeric) or item
                raise contract.Problem("already_resolved", str(e), current=current) from None
            except private_evidence.EvidenceNotFound as e:
                raise contract.Problem("not_found", str(e)) from None
            except private_evidence.StillInaccessible as e:
                _RESOLVING.reset(token)
                token = None
                sync_source("evidence")
                raise contract.Problem("validation", str(e)) from None
            stored_answer = verb
        else:
            resolution = verb
            stored_answer = str(answer or verb)
    finally:
        if token is not None:
            _RESOLVING.reset(token)

    now = util.iso()
    with foundation.tx() as c:
        if keep_open:
            updated, _, ok = inbox_store.record_answer(
                c, numeric, answer=stored_answer, answered_by=actor, answered_at=now, base_version=base_version
            )
        else:
            updated, _, ok = inbox_store.resolve_item(
                c,
                numeric,
                resolution=resolution,
                answer=stored_answer,
                answered_by=actor,
                answered_at=now,
                base_version=base_version,
            )
        if not ok:
            if updated and updated["state"] == "resolved":
                raise contract.Problem("already_resolved", f"Inbox item {updated['id']} is already resolved.", current=updated)
            raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=updated)
    return {"item": updated, "result": handler_result}


def upload_evidence(
    item_id: str | int,
    data: bytes,
    *,
    filename: str = "",
    content_type: str = "",
    actor: str = "",
    keyframe_extractor=None,
    auto_resume: bool = True,
    resumer=None,
) -> dict:
    from . import private_evidence

    numeric = inbox_store.parse_id(item_id)
    if numeric is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    item = inbox_store.get(numeric)
    if item is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    if item["state"] == "resolved":
        raise contract.Problem("already_resolved", f"Inbox item {item['id']} is already resolved.", current=item)
    subject = item["subject"]
    if not subject.startswith("evidence:"):
        raise contract.Problem("invalid_state", f"Inbox item {item['id']} does not accept private evidence uploads.")
    ev_id = subject.removeprefix("evidence:")
    who = actor or principals.current()

    token = _RESOLVING.set(subject)
    try:
        uploaded = private_evidence.upload_copy(
            ev_id,
            data,
            filename=filename,
            content_type=content_type,
            actor=who,
            keyframe_extractor=keyframe_extractor,
            auto_resume=auto_resume,
            resumer=resumer,
        )
    except private_evidence.EvidenceAlreadyResolved as e:
        _RESOLVING.reset(token)
        token = None
        sync_source("evidence")
        current = inbox_store.get(numeric) or item
        raise contract.Problem("already_resolved", str(e), current=current) from None
    except private_evidence.EvidenceNotFound as e:
        raise contract.Problem("not_found", str(e)) from None
    except private_evidence.UploadTooLarge as e:
        _RESOLVING.reset(token)
        token = None
        sync_source("evidence")
        raise contract.Problem("body_too_large", str(e)) from None
    except private_evidence.EvidenceError as e:
        _RESOLVING.reset(token)
        token = None
        sync_source("evidence")
        raise contract.Problem("validation", str(e)) from None
    finally:
        if token is not None:
            _RESOLVING.reset(token)

    now = util.iso()
    with foundation.tx() as c:
        updated, _, ok = inbox_store.resolve_item(
            c,
            numeric,
            resolution="uploaded",
            answer="uploaded",
            answered_by=who,
            answered_at=now,
        )
        if not ok:
            if updated and updated["state"] == "resolved":
                return {"item": updated, "evidence": uploaded["evidence"], "result": uploaded["resumed"]}
            raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=updated)
    return {"item": updated, "evidence": uploaded["evidence"], "result": uploaded["resumed"]}


def on_server_start(pending_secrets=None) -> None:
    if not paths.DB_FILE.exists():
        return
    try:
        with foundation.tx() as c:
            if not inbox_store.available(c):
                return
            expire_pending(c, startup=True, pending_secrets=pending_secrets)
            inbox_store.reopen_due(c)
            sync(c, "all")
    except (foundation.NoDatabase, foundation.IncompatibleSchema):
        pass


def tick(pending_secrets=None) -> None:
    if not paths.DB_FILE.exists():
        return
    with foundation.tx() as c:
        if not inbox_store.available(c):
            return
        inbox_store.reopen_due(c)
        expire_pending(c, startup=False, pending_secrets=pending_secrets)
        sync(c, "skipped")
        sync(c, "ready")
        sync(c, "config")
        sync(c, "outbox")
