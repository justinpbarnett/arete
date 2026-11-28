from __future__ import annotations

import builtins
import json
import re
import threading
from collections.abc import Callable, Sequence
from contextlib import nullcontext, suppress
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
    record,
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
from .store import credentials as credentials_store
from .store import db as foundation
from .store import devices as device_store
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
_EXC_CLASS_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Timeout|Failure|Fault|Refused|Denied|Interrupt))\b")
_HTTP_STATUS_RE = re.compile(r"\b(?:HTTP\s+)?([45]\d{2})\b")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")

PRESENCE_TTL_MINUTES = 10
REOPEN_WINDOW_SECONDS = 10
RESERVED_MANUAL_PREFIXES = (
    "presence:",
    "approval:",
    "ask:",
    "run:",
    "proposal:",
    "draft:",
    "skipped:",
    "ticket:",
    "chat:",
    "incident:",
    "workspace:",
    "evidence:",
    "config:",
    "queue:",
    "outbox:",
    "secret:",
    "step:",
    "ref:",
    "project:",
    "line:",
    "view:",
    "source:",
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
    ic = run_store.issue_col(c)
    private_clause = (
        f"AND NOT EXISTS (SELECT 1 FROM private_evidence e WHERE e.repo = r.repo "  # noqa: S608
        f"  AND e.ticket = CAST(r.{ic} AS TEXT) AND e.status = 'open') "
        if evidence_store.available(c)
        else ""
    )
    rows = [
        run_store.run_row(r)
        for r in c.execute(
            f"SELECT r.* FROM runs r WHERE r.kind = 'pipeline' AND r.status = 'needs-info' "  # noqa: S608
            f"AND {run_store.WHOLE_RUN} AND {run_store.LIVE_RUN} "
            "AND NOT EXISTS (SELECT 1 FROM asks a WHERE a.run = r.id "
            "  AND (a.status = 'open' OR (a.status = 'answered' AND a.taken_by = ''))) "
            f"{private_clause}"
            "AND NOT EXISTS (SELECT 1 FROM runs later WHERE later.kind = 'pipeline' "
            f"  AND later.repo = r.repo AND later.{ic} = r.{ic} AND {run_store.WHOLE_RUN} "
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
    from . import drafts, miner

    if not drafts.available(c):
        return []
    cfg = config.load_or_problem()[0]
    rows = c.execute(
        "SELECT * FROM config_drafts WHERE "
        "(state = 'valid' AND COALESCE(pr_url, '') = '') "
        "OR (state = 'draft' AND (kind = 'miner' OR (gate = 'C' AND author LIKE 'run:%'))) "
        "ORDER BY id DESC"
    ).fetchall()
    items = []
    for raw in rows:
        d = drafts.decode(raw)
        num_id = d["numeric_id"]
        if d["kind"] == "miner":
            ev = d["evidence"] if isinstance(d["evidence"], dict) else {}
            repo = (d["origin"] or "").removeprefix("repo:")
            bucket = miner.classify_draft(cfg, d)
            legacy_status = ev.get("legacy_status")
            priority = 2 if bucket in ("approve", "pending") or d["state"] == "draft" else 3
            if legacy_status is not None:
                title = f"Miner proposal #{num_id} (unknown status {legacy_status})"
            else:
                title = d["path"] or d["body"] or f"Proposal #{num_id}"
            detail = {
                "id": num_id,
                "draft_id": d["id"],
                "kind": "miner",
                "name": d["name"],
                "repo": repo,
                "file": d["path"],
                "change": d["body"],
                "why": d["why"],
                "status": d["state"],
                "state": d["state"],
                "bucket": bucket,
                "gate": d["gate"],
                "check_report": d["check_report"],
                **({"legacy_status": legacy_status} if legacy_status is not None else {}),
                "stamp": f"{d['state']}:{num_id}:{d['version']}",
            }
            items.append({
                "kind": "proposal",
                "subject": f"draft:{num_id}",
                "task_id": "",
                "run_id": str(ev.get("run") or ""),
                "line": repo,
                "priority": priority,
                "title": title,
                "detail": detail,
                "verbs": ["accept", "decline"],
                "options": [],
            })
        else:
            title = f"Config draft {d['id']}: {d['kind']}/{d['name']}"
            if d["why"]:
                title += f" — {util.first_line(d['why'], 100)}"
            detail = {
                "id": num_id,
                "draft_id": d["id"],
                "kind": d["kind"],
                "name": d["name"],
                "path": d["path"],
                "author": d["author"],
                "why": d["why"],
                "gate": d["gate"],
                "state": d["state"],
                "base_version": d["base_version"],
                "check_report": d["check_report"],
                "stamp": f"{d['state']}:{num_id}:{d['version']}",
            }
            items.append({
                "kind": "proposal",
                "subject": f"draft:{num_id}",
                "task_id": "",
                "run_id": d["author"].removeprefix("run:").partition("/")[0] if d["author"].startswith("run:") else "",
                "line": d["name"],
                "priority": 2,
                "title": title,
                "detail": detail,
                "verbs": ["accept", "decline"],
                "options": [],
            })
    return items


def _skipped_items(c) -> list[dict]:
    ic = run_store.issue_col(c)
    running = {
        (r["repo"], str(r[ic]))
        for r in c.execute(f"SELECT repo, {ic} FROM runs WHERE status = 'running' AND {ic} IS NOT NULL")  # noqa: S608
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
    from . import drafts

    if drafts.available(c):
        seen_subjects = {item["subject"] for item in items}
        invalid_hands = c.execute(
            "SELECT * FROM config_drafts WHERE author = 'hand' AND state = 'invalid' ORDER BY id DESC"
        ).fetchall()
        for raw in invalid_hands:
            d = drafts.decode(raw)
            rel = d["path"]
            if rel:
                disk = paths.CONFIG_DIR / rel
                try:
                    if d["body"] is None or not disk.is_file() or disk.read_bytes() != d["body"].encode("utf-8"):
                        continue
                except OSError:
                    continue
            subj = f"config:{d['kind']}/{d['name']}"
            if subj in seen_subjects:
                continue
            seen_subjects.add(subj)
            report = d.get("check_report") or {}
            probs = [p for st in report.get("stages", ()) for p in st.get("problems", ())]
            summary = "; ".join(probs) or f"Invalid hand edit on {d['path']}"
            items.append({
                "kind": "config_invalid",
                "subject": subj,
                "task_id": "",
                "run_id": "",
                "line": d["name"] if d["name"] != "defaults" else "",
                "priority": 1,
                "title": f"Invalid hand edit in {d['path'] or subj}",
                "detail": {
                    "path": d["path"],
                    "draft_id": d["id"],
                    "problems": probs,
                    "summary": summary,
                    "check_report": report,
                    "stamp": f"hand_invalid:{d['numeric_id']}:{d['version']}",
                },
                "verbs": ["continue"],
                "options": [],
            })
    from . import prompts

    seen_subjects = {item["subject"] for item in items}
    for spec in prompts.prompt_invalid_specs(cfg):
        if spec["subject"] not in seen_subjects:
            seen_subjects.add(spec["subject"])
            items.append(spec)
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
            "legacy_approval": row.get("legacy_approval"),
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


def error_line(error: str | None) -> str:
    for line in str(error or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def error_class(error: str | None) -> str:
    line = error_line(error)
    if not line:
        return "unknown"
    if match := _EXC_CLASS_RE.search(line):
        return match.group(1)
    if match := _HTTP_STATUS_RE.search(line):
        return f"http_{match.group(1)}"
    head, sep, _ = line.partition(":")
    if sep:
        head_clean = head.strip()
        if 0 < len(head_clean) <= 48 and "/" not in head_clean:
            slug = _NON_WORD_RE.sub("_", head_clean.lower()).strip("_")
            if slug:
                return slug
    slug = _NON_WORD_RE.sub("_", line[:48].lower()).strip("_")
    return slug or "unknown"


def _secret_subject_context(c, scope: str, waiting_subjects: builtins.list[str]) -> tuple[str, str, str]:
    line = scope.partition(":")[2] if scope.startswith(("repo:", "line:")) else ""
    run_id = ""
    task_id = ""
    for subj in waiting_subjects:
        if subj.startswith("run:"):
            rid = subj.removeprefix("run:")
            row = run_store.run_get(rid, connect=lambda: nullcontext(c))
            if row:
                run_id = rid
                task_id = str(row.get("issue") or "")
                line = str(row.get("repo") or row.get("loop") or line)
                break
        elif subj.startswith("outbox:"):
            oid = subj.removeprefix("outbox:")
            if oid.isdecimal():
                from .store import outbox as outbox_store

                if outbox_store.available(c):
                    row = outbox_store.get_row(c, int(oid))
                    if row:
                        run_id = str(row.get("run_id") or "")
                        task_id = str(row.get("task_id") or "")
                        line = str(row.get("loop") or row.get("object") or row.get("target") or line)
                        break
    return line, run_id, task_id


def _secret_continue_spec(c, scope: str, name: str, waiting_subjects: builtins.list[str]) -> dict | None:
    if not waiting_subjects:
        return None
    has_secrets = c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'secrets'").fetchone() is not None
    row = credentials_store.secret(c, scope, name) if has_secrets else None
    if row is not None and row["status"] == "set":
        return None
    line, run_id, task_id = _secret_subject_context(c, scope, waiting_subjects)
    count = len(waiting_subjects)
    scope_part = "" if scope == "global" else f" ({scope})"
    title = f"{name} missing{scope_part}: {count} waiting"
    members = builtins.list(waiting_subjects)
    missing_since = row.get("missing_since") if row else None
    detail = {
        "name": name,
        "scope": scope,
        "required": bool(row["required"]) if row else True,
        "status": row["status"] if row else "missing",
        "missing_since": missing_since,
        "last_check": {
            "checked_at": row.get("checked_at") if row else None,
            "ok": None if (row is None or row.get("check_ok") is None) else bool(row["check_ok"]),
        },
        "waiting": members,
        "group_key": f"continue:{scope}/{name}",
        "group_members": members,
        "stamp": f"{scope}/{name}:{missing_since or ''}:{members[0]}",
    }
    return {
        "kind": "continue",
        "subject": f"secret:{scope}/{name}",
        "task_id": task_id,
        "run_id": run_id,
        "line": line,
        "priority": 1,
        "title": title,
        "detail": detail,
        "verbs": ["continue"],
        "options": [],
    }


def _secrets_items(c) -> builtins.list[dict]:
    waits = device_store.open_secret_waits(c)
    if not waits:
        return []
    grouped: dict[str, builtins.list[str]] = {}
    for w in waits:
        blocker = w["blocker"]
        bucket = grouped.setdefault(blocker, [])
        if w["subject"] not in bucket:
            bucket.append(w["subject"])
    items = []
    for blocker, subjects in grouped.items():
        scope, sep, name = blocker.removeprefix("secret:").partition("/")
        if not sep or not scope or not name:
            continue
        spec = _secret_continue_spec(c, scope, name, subjects)
        if spec is not None:
            items.append(spec)
    return items


def _run_label(run: dict) -> str:
    return tracker.run_ticket_name(run) if run.get("issue") else str(run["id"])


def _chain_run_ids(c, run_id: str) -> builtins.list[str]:
    seen: builtins.list[str] = [run_id]
    if not run_store.has_lineage(c):
        return seen
    row = c.execute("SELECT continues, chain FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return seen
    chain = row["chain"] or ""
    if chain:
        for r in c.execute("SELECT id FROM runs WHERE chain = ? ORDER BY id", (chain,)):
            if r["id"] not in seen:
                seen.append(r["id"])
    cur = row["continues"]
    while cur and cur not in seen:
        seen.append(cur)
        parent = c.execute("SELECT continues FROM runs WHERE id = ?", (cur,)).fetchone()
        cur = parent["continues"] if parent else None
    return seen


def _later_node_passed(c, run_id: str, node: str, latest_step_id: int) -> bool:
    if not run_store.has_steps_lifecycle(c):
        return False
    related = _chain_run_ids(c, run_id)
    if len(related) <= 1:
        return False
    placeholders = ",".join("?" for _ in related)
    return c.execute(
        f"SELECT 1 FROM steps WHERE run_id IN ({placeholders}) "  # noqa: S608
        "AND COALESCE(NULLIF(node, ''), step) = ? AND status = 'done' AND id > ? LIMIT 1",
        (*related, node, latest_step_id),
    ).fetchone() is not None


def _node_terminal_steps(c, run_id: str, node: str) -> builtins.list[dict]:
    if not run_store.has_steps_lifecycle(c):
        return []
    rows = c.execute(
        "SELECT id, try, status, ok, error, ts FROM steps "
        "WHERE run_id = ? AND COALESCE(NULLIF(node, ''), step) = ? AND status IN ('done', 'fault') "
        "ORDER BY id DESC",
        (run_id, node),
    ).fetchall()
    return [dict(r) for r in rows]


def _is_consecutive_same_fault(steps: builtins.list[dict]) -> bool:
    if len(steps) < 2:
        return False
    if steps[0]["status"] != "fault" or steps[1]["status"] != "fault":
        return False
    err1 = error_line(steps[0].get("error"))
    return bool(err1) and err1 == error_line(steps[1].get("error"))


def _attempt_count(steps: builtins.list[dict]) -> int:
    if not steps:
        return 0
    return max(len(steps), max(int(s.get("try") or 0) for s in steps))


def _stop_spec(run: dict, node: str, latest: dict, attempts: int) -> dict:
    from . import board

    run_id = str(run["id"])
    err = error_line(latest.get("error"))
    err_cls = error_class(err)
    label = _run_label(run)
    last_lines = board.log_lines(run)[-20:]
    jidoka = {
        "detect": f"{node} failed twice in a row with the same error: {err}",
        "stop": f"run {run_id} halted at {node} after {attempts} attempts",
        "fix": f"Inspect {node} error ({err}) and redirect the run with a fix",
        "prevent": f"Add a preflight check or guard for {err_cls} before {node}",
    }
    detail = {
        "run_id": run_id,
        "node": node,
        "error": err,
        "error_class": err_cls,
        "attempts": attempts,
        "jidoka": jidoka,
        "last_lines": last_lines,
        "group_key": f"stop:{err_cls}",
        "stamp": f"stop:{latest['id']}",
    }
    return {
        "kind": "stop",
        "subject": f"run:{run_id}",
        "task_id": str(run.get("issue") or ""),
        "run_id": run_id,
        "line": str(run.get("repo") or run.get("loop") or ""),
        "priority": 1,
        "title": f"{label} stopped itself at {node}: same failure twice",
        "detail": detail,
        "verbs": ["redirect", "retry", "stop"],
        "options": [],
    }


def _fault_spec(c, run: dict, node: str, latest: dict, attempts: int) -> dict:
    from . import board

    run_id = str(run["id"])
    err = error_line(latest.get("error"))
    err_cls = error_class(err)
    label = _run_label(run)
    last_lines = board.log_lines(run)[-20:]
    subject = f"step:{run_id}/{node}"
    detail: dict[str, object] = {
        "run_id": run_id,
        "node": node,
        "error": err,
        "error_class": err_cls,
        "attempts": attempts,
        "last_lines": last_lines,
        "group_key": f"fault:{err_cls}",
        "stamp": f"step:{latest['id']}",
    }
    existing = inbox_store.get_open_by_kind_and_subject("fault", subject, connect=lambda: nullcontext(c))
    if existing is not None and existing["state"] == "shelved":
        detail["shelved_reason"] = existing["shelve_reason"]
        detail["shelve_until"] = existing["shelve_until"]
    attempt_word = "attempt" if attempts == 1 else "attempts"
    title = f"{label}: {node} failed ({attempts} {attempt_word})"
    return {
        "kind": "fault",
        "subject": subject,
        "task_id": str(run.get("issue") or ""),
        "run_id": run_id,
        "line": str(run.get("repo") or run.get("loop") or ""),
        "priority": 2,
        "title": title,
        "detail": detail,
        "verbs": ["retry", "dismiss"],
        "options": [],
    }


def _upsert_item_in_tx(c, spec: dict) -> dict:
    existing = inbox_store.get_open_by_kind_and_subject(spec["kind"], spec["subject"], connect=lambda: nullcontext(c))
    if existing is not None:
        numeric_id = inbox_store.parse_id(existing["id"])
        updated = inbox_store.update_item(
            c,
            numeric_id,
            title=spec["title"],
            detail=spec["detail"],
            priority=spec["priority"],
            verbs=spec["verbs"],
            options=spec["options"],
            task_id=spec["task_id"],
            run_id=spec["run_id"],
            line=spec["line"],
        )
        return updated[0] if updated is not None else existing
    item, _, _ = inbox_store.raise_item(
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
    return item


def _escalate_fault_in_tx(c, run_id: str, node: str) -> None:
    subject = f"step:{run_id}/{node}"
    existing = inbox_store.get_open_by_kind_and_subject("fault", subject, connect=lambda: nullcontext(c))
    if existing is not None:
        inbox_store.resolve_item(
            c,
            inbox_store.parse_id(existing["id"]),
            resolution="escalated",
            answer="escalated",
        )


def on_step_settled(c, step_id: int) -> None:
    if not inbox_store.available(c) or not run_store.has_steps_lifecycle(c):
        return
    row = c.execute(
        "SELECT id, run_id, step, node, status, ok, error, try, ts FROM steps WHERE id = ?",
        (step_id,),
    ).fetchone()
    if row is None or not row["run_id"]:
        return
    run_id = str(row["run_id"])
    node = str(row["node"] or row["step"] or "").strip()
    if not node:
        return
    status = row["status"] or ("done" if row["ok"] == 1 else "fault")
    if status == "done":
        for related_run in _chain_run_ids(c, run_id):
            fault_item = inbox_store.get_open_by_kind_and_subject(
                "fault", f"step:{related_run}/{node}", connect=lambda: nullcontext(c)
            )
            if fault_item is not None:
                inbox_store.resolve_item(
                    c,
                    inbox_store.parse_id(fault_item["id"]),
                    resolution="passed",
                    answer="passed",
                )
            stop_item = inbox_store.get_open_by_kind_and_subject(
                "stop", f"run:{related_run}", connect=lambda: nullcontext(c)
            )
            if stop_item is not None and stop_item["detail"].get("node") == node:
                inbox_store.resolve_item(
                    c,
                    inbox_store.parse_id(stop_item["id"]),
                    resolution="passed",
                    answer="passed",
                )
        return
    if status != "fault":
        return
    run = run_store.run_get(run_id, connect=lambda: nullcontext(c))
    if run is None:
        return
    node_steps = _node_terminal_steps(c, run_id, node)
    if not node_steps or node_steps[0]["status"] != "fault":
        return
    attempts = _attempt_count(node_steps)
    if run.get("status") != "needs-info" and _is_consecutive_same_fault(node_steps):
        _escalate_fault_in_tx(c, run_id, node)
        _upsert_item_in_tx(c, _stop_spec(run, node, node_steps[0], attempts))
    else:
        _upsert_item_in_tx(c, _fault_spec(c, run, node, node_steps[0], attempts))


def _stops_items(c) -> builtins.list[dict]:
    if not run_store.has_steps_lifecycle(c):
        return []
    pairs = c.execute(
        "SELECT DISTINCT run_id, COALESCE(NULLIF(node, ''), step) AS node_name "
        "FROM steps WHERE status = 'fault' AND COALESCE(run_id, '') != '' "
        "ORDER BY run_id, node_name"
    ).fetchall()
    by_run: dict[str, tuple[int, dict]] = {}
    for pair in pairs:
        run_id, node = str(pair["run_id"]), str(pair["node_name"] or "").strip()
        if not node:
            continue
        run = run_store.run_get(run_id, connect=lambda: nullcontext(c))
        if run is None or run.get("status") == "needs-info":
            continue
        node_steps = _node_terminal_steps(c, run_id, node)
        if not _is_consecutive_same_fault(node_steps):
            continue
        latest_id = int(node_steps[0]["id"])
        if _later_node_passed(c, run_id, node, latest_id):
            continue
        _escalate_fault_in_tx(c, run_id, node)
        prev = by_run.get(run_id)
        if prev is None or latest_id > prev[0]:
            by_run[run_id] = (latest_id, _stop_spec(run, node, node_steps[0], _attempt_count(node_steps)))
    return [entry[1] for entry in by_run.values()]


def _faults_items(c) -> builtins.list[dict]:
    if not run_store.has_steps_lifecycle(c):
        return []
    pairs = c.execute(
        "SELECT DISTINCT run_id, COALESCE(NULLIF(node, ''), step) AS node_name "
        "FROM steps WHERE status = 'fault' AND COALESCE(run_id, '') != '' "
        "ORDER BY run_id, node_name"
    ).fetchall()
    items = []
    for pair in pairs:
        run_id, node = str(pair["run_id"]), str(pair["node_name"] or "").strip()
        if not node:
            continue
        run = run_store.run_get(run_id, connect=lambda: nullcontext(c))
        if run is None:
            continue
        node_steps = _node_terminal_steps(c, run_id, node)
        if not node_steps or node_steps[0]["status"] != "fault":
            continue
        latest_id = int(node_steps[0]["id"])
        if _later_node_passed(c, run_id, node, latest_id):
            continue
        if run.get("status") != "needs-info" and _is_consecutive_same_fault(node_steps):
            continue
        items.append(_fault_spec(c, run, node, node_steps[0], _attempt_count(node_steps)))
    return items


def check_run_missing_secrets(run_id: str, *, repo: str = "", loop: str = "") -> builtins.list[dict]:
    with foundation.tx() as c:
        missing = secretstore.missing_required_for_run(c, repo=repo, loop=loop)
        if not missing:
            return []
        for row in missing:
            blocker = f"secret:{row['scope']}/{row['name']}"
            device_store.open_wait(c, f"run:{run_id}", blocker, f"waiting on missing secret {row['scope']}/{row['name']}")
        sync(c, "secrets")
        return missing


def check_outbox_missing_secrets(row: dict) -> builtins.list[dict]:
    item_id = int(row["id"])
    with foundation.tx() as c:
        missing = secretstore.missing_required_for_outbox(
            c,
            str(row.get("target") or ""),
            kind=str(row.get("kind") or ""),
            loop=str(row.get("loop") or ""),
        )
        if not missing:
            return []
        for sec in missing:
            blocker = f"secret:{sec['scope']}/{sec['name']}"
            device_store.open_wait(c, f"outbox:{item_id}", blocker, f"waiting on missing secret {sec['scope']}/{sec['name']}")
        sync(c, "secrets")
        return missing


def resolve_secret_continue_in_tx(c, scope: str, name: str, *, actor: str, at: str) -> dict | None:
    if not inbox_store.available(c):
        return None
    existing = inbox_store.get_open_by_kind_and_subject(
        "continue", f"secret:{scope}/{name}", connect=lambda: nullcontext(c)
    )
    if existing is None:
        return None
    resolved, _, ok = inbox_store.resolve_item(
        c,
        inbox_store.parse_id(existing["id"]),
        resolution="continued",
        answer="continue",
        answered_by=actor,
        answered_at=at,
    )
    return resolved if ok else None


def resume_unblocked_subjects(subjects: Sequence[str]) -> builtins.list[str]:
    from . import sessions

    resumed: builtins.list[str] = []
    for subject in subjects:
        if subject.startswith("run:"):
            run_id = subject.removeprefix("run:")
            row = run_store.run_get(run_id)
            if row is None:
                continue
            if row["status"] == "queued":
                saved = record.read_launch(run_id)
                cmd = saved.get("command") if isinstance(saved.get("command"), builtins.list) and saved.get("command") else None
                log_name = str(saved.get("log") or "")
                kind = row.get("kind") or ""
                if cmd is None:
                    if kind == "session" and sessions.spec_file(run_id).is_file():
                        cmd = control.command(["session", "exec", run_id])
                        log_name = sessions.SESSION_LOG
                    elif kind == "loop" and row.get("loop"):
                        cmd = control.command(["loop", "exec", "--run-id", run_id])
                        log_name = control.DETACHED_LOG
                    elif kind == "pipeline" and row.get("repo") and row.get("issue"):
                        cmd = pipeline.run_command(row["repo"], str(row["issue"]), run_id=run_id)
                        log_name = pipeline.RUNS_LOG
                if not cmd:
                    continue
                with suppress(Exception):
                    if kind == "pipeline":
                        tasks.spawn_queued(
                            row["repo"],
                            str(row["issue"]),
                            lambda c=cmd, log=log_name or pipeline.RUNS_LOG: control.launch(c, log),
                        )
                    else:
                        control.launch(cmd, log_name or control.DETACHED_LOG)
                    resumed.append(subject)
            elif row.get("kind") == "pipeline" and row["status"] in tasks.RESUMABLE:
                with suppress(Exception):
                    tasks.resume(run_id)
                    resumed.append(subject)
        elif subject.startswith("outbox:"):
            ident = subject.removeprefix("outbox:")
            if not ident.isdecimal():
                continue
            from .store import outbox as outbox_store

            ob_id = int(ident)
            with foundation.tx() as c:
                if outbox_store.available(c):
                    changed = c.execute(
                        "UPDATE outbox SET send_at = COALESCE(send_at, ?), error = NULL, updated = ? "
                        "WHERE id = ? AND state = 'approved'",
                        (util.iso(), util.iso(), ob_id),
                    ).rowcount
                    if changed:
                        resumed.append(subject)
    return resumed


ADAPTERS: dict[str, tuple[str, Callable[..., builtins.list[dict]]]] = {
    "approvals": ("approval:%", _approvals_items),
    "asks": ("ask:%", _asks_items),
    "runs": ("run:%", _runs_items),
    "secrets": ("secret:%", _secrets_items),
    "stops": ("run:%", _stops_items),
    "faults": ("step:%", _faults_items),
    "evidence": ("evidence:%", _evidence_items),
    "proposals": ("draft:%", _proposals_items),
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
ADAPTER_KINDS: dict[str, tuple[str, ...]] = {
    "runs": ("freeform",),
    "secrets": ("continue",),
    "stops": ("stop",),
    "faults": ("fault",),
    "config": ("config_invalid",),
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
    if source == "faults" and subject.startswith("step:"):
        run_id, sep, node = subject.removeprefix("step:").partition("/")
        if sep and run_id and node:
            node_steps = _node_terminal_steps(c, run_id, node)
            if node_steps and node_steps[0]["status"] == "done":
                return "passed"
            if node_steps and _later_node_passed(c, run_id, node, int(node_steps[0]["id"])):
                return "passed"
            run = run_store.run_get(run_id, connect=lambda: nullcontext(c))
            if run is not None and run.get("status") != "needs-info" and _is_consecutive_same_fault(node_steps):
                return "escalated"
    return "cleared"


def sync(c, source: str) -> None:
    if not inbox_store.available(c):
        return
    if source == "all":
        for name in ADAPTERS:
            sync(c, name)
        return
    pattern, producer = ADAPTERS[source]
    kinds = ADAPTER_KINDS.get(source)
    resolving = _RESOLVING.get()
    desired = producer(c)
    wanted = {(spec["kind"], spec["subject"]): spec for spec in desired}
    existing_rows = inbox_store.unresolved_for_pattern(c, pattern, kinds=kinds)
    if source == "proposals":
        existing_rows = [r for r in existing_rows if r["kind"] == "proposal"]
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
            prev = inbox_store.newest_resolved_by_subject(c, spec["subject"], kind=spec["kind"])
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
    from . import attention

    with connect() as c:
        items = inbox_store.list_items(state="open", connect=lambda: nullcontext(c))
        counts = inbox_store.counts_by_state(connect=lambda: nullcontext(c))
        andon = attention.andon(c)
    return {
        "items": items,
        "by_priority": {
            "high": [i for i in items if i["priority"] == 1],
            "med": [i for i in items if i["priority"] == 2],
            "low": [i for i in items if i["priority"] == 3],
        },
        "counts": counts,
        "andon": andon,
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
        if kind == "draft":
            from . import drafts

            return drafts.public(drafts.get(ident, connect=lambda: nullcontext(c)))
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
        if kind == "secret":
            from . import api

            scope, sep, name = ident.partition("/")
            if sep and scope and name:
                row = credentials_store.secret(c, scope, name)
                return api.secret_view(row) if row else None
        if kind == "step":
            run_id, sep, node = ident.partition("/")
            if sep and run_id and node:
                steps_found = _node_terminal_steps(c, run_id, node)
                return steps_found[0] if steps_found else None
        if kind == "ref" and ident.isdecimal():
            from .store import tasks as task_store

            return task_store.TaskStore.ref_get(c, int(ident))
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


def _approval_was_sent(c, item: dict) -> bool:
    subject = item["subject"]
    if subject.startswith("presence:"):
        return True
    if subject.startswith("approval:"):
        ident = subject.removeprefix("approval:")
        if ident.isdecimal():
            from .store import outbox as outbox_store

            row = outbox_store.get_by_legacy_approval(c, int(ident)) or outbox_store.get_row(c, int(ident))
            if row is not None:
                return row["state"] != "failed"
        return True
    if subject.startswith("outbox:"):
        ident = subject.removeprefix("outbox:")
        if ident.isdecimal():
            from .store import outbox as outbox_store

            row = outbox_store.get_row(c, int(ident))
            if row is not None:
                return row["state"] in ("sent", "sending") or bool(row["sent_at"])
        return False
    return True


def reopen(item_id: str | int, *, base_version: int) -> dict:
    numeric = inbox_store.parse_id(item_id)
    if numeric is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    with foundation.tx() as c:
        current = inbox_store.get(numeric, connect=lambda: nullcontext(c))
        if current is None:
            raise contract.Problem("not_found", f"No inbox item {item_id}")
        if current["version"] != base_version:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=current)
        state = current["state"]
        if state == "open":
            raise contract.Problem("already_open", f"Inbox item {current['id']} is already open.", item=current["id"])
        if state in ("snoozed", "shelved"):
            pass
        elif state == "resolved":
            if current["kind"] == "candidate" and current["resolution"] in ("declined", "decline"):
                pass
            elif current["kind"] == "approval" and (
                current["resolution"] == "approved" or current["answer"] == "approve"
            ):
                if _approval_was_sent(c, current):
                    raise contract.Problem("already_sent", f"Inbox item {current['id']} was already sent.")
                raise contract.Problem("not_reversible", f"Inbox item {current['id']} cannot be reopened.")
            elif current["kind"] == "outbox" and (
                current["resolution"] == "approved" or current["answer"] == "approve"
            ):
                if _approval_was_sent(c, current):
                    raise contract.Problem("already_sent", f"Inbox item {current['id']} was already sent.")
                raise contract.Problem("not_reversible", f"Inbox item {current['id']} cannot be reopened.")
            else:
                raise contract.Problem("not_reversible", f"Inbox item {current['id']} cannot be reopened.")
        else:
            raise contract.Problem("not_reversible", f"Inbox item {current['id']} cannot be reopened.")

        last_at = inbox_store.latest_transition_at(c, numeric)
        if not last_at:
            raise contract.Problem("too_late", f"Inbox item {current['id']} can no longer be reopened.")
        try:
            elapsed = (util.now() - util.parse_iso(last_at)).total_seconds()
        except (ValueError, TypeError):
            raise contract.Problem("too_late", f"Inbox item {current['id']} can no longer be reopened.") from None
        if elapsed > REOPEN_WINDOW_SECONDS:
            raise contract.Problem("too_late", f"Inbox item {current['id']} can no longer be reopened.")

        if state == "resolved":
            other = inbox_store.other_open_by_kind_and_subject(c, current["kind"], current["subject"], numeric)
            if other is not None:
                raise contract.Problem(
                    "already_open",
                    f"Another inbox item {other['id']} is already open for {current['kind']} {current['subject']}.",
                    item=other["id"],
                )

        updated, _, ok = inbox_store.reopen_item(c, numeric, base_version=base_version)
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
    extra_detail: dict | None = None,
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
                    **(extra_detail or {}),
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
    if op_name == "config.activate":
        from . import drafts

        draft_id = values.get("draft") or ""
        if not draft_id:
            found = drafts.list_drafts(state="valid", kind=values["kind"], name=values["name"], limit=1)
            if not found:
                raise contract.Problem("not_found", f"No valid draft for {values['kind']}/{values['name']}")
            draft_id = found[0]["id"]
        activated = drafts.activate(
            draft_id,
            actor,
            values["base_version"],
            idem=req.get("idem", ""),
            presence_passed=True,
        )
        return 200, api.with_seq(activated)
    if op_name == "config.rollback":
        from . import drafts

        rolled = drafts.rollback(
            values["kind"],
            values["name"],
            values["to"],
            values["base_version"],
            actor,
            idem=req.get("idem", ""),
            presence_passed=True,
        )
        return 200, api.with_seq(rolled)
    if op_name == "config.delete":
        from . import drafts

        deleted = drafts.delete_item(
            values["kind"],
            values["name"],
            values["base_version"],
            actor,
            idem=req.get("idem", ""),
            why=str(values.get("why") or ""),
            presence_passed=True,
        )
        return 200, api.with_seq(deleted)
    if op_name == "settings.update":
        from . import settings

        saved = settings.save(
            values["values"],
            base_version=values["base_version"],
            actor=actor,
            source="dashboard",
            idem=req.get("idem", ""),
            presence_passed=True,
        )
        return 200, api.with_seq({"settings": saved})
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

        req = {**pending["request"], "idem": key}
        run_outside_tx = req["operation"] in ("config.activate", "config.rollback", "config.delete", "settings.update")
        if req["operation"] in ("outbox.approve", "outbox.retry", "outbox.reject"):
            from .store import outbox as outbox_store

            ob_row = outbox_store.get_raw(req["values"]["item"], connect=lambda: nullcontext(c))
            run_outside_tx = ob_row is not None and ob_row.get("legacy_approval") is not None

        if run_outside_tx:
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


def raise_rescope(c, src, held_events: builtins.list[dict]) -> dict | None:
    if not inbox_store.available(c) or not held_events:
        return None
    sid = src.id
    subject = f"source:{sid}"
    title = f"{sid}: {len(held_events)} older matches after a scope change"
    detail = {
        "source": sid,
        "held": builtins.list(held_events),
        "keys": [str(e.get("key", "")) for e in held_events],
    }
    item, _, created = inbox_store.raise_item(
        c,
        kind="rescope",
        subject=subject,
        priority=2,
        title=title,
        detail=detail,
        verbs=["fire", "dismiss"],
        options=[],
        line=src.line or "",
    )
    if created or item is None:
        return item
    existing_detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    existing_held = builtins.list(existing_detail.get("held") or ())
    seen_keys = {str(e.get("key", "")) for e in existing_held if isinstance(e, dict)}
    merged = builtins.list(existing_held)
    for ev in held_events:
        k = str(ev.get("key", ""))
        if k not in seen_keys:
            seen_keys.add(k)
            merged.append(ev)
    numeric = inbox_store.parse_id(item["id"])
    if numeric is None:
        return item
    new_title = f"{sid}: {len(merged)} older matches after a scope change"
    new_detail = {
        **existing_detail,
        "source": sid,
        "held": merged,
        "keys": [str(e.get("key", "")) for e in merged],
    }
    updated = inbox_store.update_item(
        c,
        numeric,
        title=new_title,
        detail=new_detail,
        priority=2,
        verbs=["fire", "dismiss"],
        options=[],
        task_id="",
        run_id="",
        line=src.line or str(item.get("line") or ""),
    )
    return updated[0] if updated is not None else item


def _resolve_rescope(
    item: dict,
    *,
    numeric_id: int,
    base_version: int,
    verb: str,
) -> dict:
    from . import loops as loops_mod
    from . import sources as sources_mod

    sid = item["subject"].removeprefix("source:")
    actor = principals.current()
    now = util.iso()
    if verb == "fire":
        held = builtins.list((item.get("detail") or {}).get("held") or ())
        cfg = config.load_or_problem()[0]
        all_loops = loops_mod.load_all(cfg)
        live_by_name = {loop.name: loop for loop in all_loops if loop.enabled and not loop.error}
        src = next((s for s in sources_mod.load_all(cfg, all_loops) if s.id == sid), None)
        target_loops = [live_by_name[name] for name in src.loops if name in live_by_name] if src else []
        queued: builtins.list[int] = []
        with foundation.tx() as c:
            updated, _, ok = inbox_store.resolve_item(
                c,
                numeric_id,
                resolution="fired",
                answer="fire",
                answered_by=actor,
                answered_at=now,
                base_version=base_version,
            )
            if not ok:
                raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=updated)
            for loop_obj in target_loops:
                if held:
                    queued.extend(sources_mod.fire(loop_obj, "event", held))
        return {"item": updated, "result": {"source": sid, "fired": len(held), "items": queued}}
    if verb == "dismiss":
        with foundation.tx() as c:
            updated, _, ok = inbox_store.resolve_item(
                c,
                numeric_id,
                resolution="dismissed",
                answer="dismiss",
                answered_by=actor,
                answered_at=now,
                base_version=base_version,
            )
            if not ok:
                raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=updated)
        return {"item": updated, "result": {"source": sid, "dismissed": True}}
    raise contract.Problem("validation", f"Verb {verb!r} is not allowed on {item['id']}.")


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


def _resolve_secret_continue(
    item: dict,
    *,
    numeric_id: int,
    base_version: int,
    actor: str,
) -> dict:
    scope, sep, name = item["subject"].removeprefix("secret:").partition("/")
    if not sep or not scope or not name:
        raise contract.Problem("validation", f"Invalid secret subject {item['subject']!r}.")
    try:
        checked = secretstore.check(scope, name, actor)
    except (secretstore.StoreMissing, secretstore.StoreError, ValueError):
        checked = None
    ok_check = checked is not None and checked.get("status") == "set" and bool(checked.get("check_ok"))
    if not ok_check:
        sync_source("secrets")
        refreshed = inbox_store.get(numeric_id) or item
        raise contract.Problem(
            "still_missing",
            f"Secret {scope}/{name} is still missing.",
            current=refreshed,
        )
    now = util.iso()
    blocker = f"secret:{scope}/{name}"
    with foundation.tx() as c:
        current = inbox_store.get(numeric_id, connect=lambda: nullcontext(c))
        if current is None:
            raise contract.Problem("not_found", f"No inbox item {item['id']}")
        if current["state"] == "resolved":
            raise contract.Problem("already_resolved", f"Inbox item {current['id']} is already resolved.", current=current)
        if current["version"] != base_version:
            raise contract.Problem("version_conflict", f"Inbox item {current['id']} changed.", current=current)
        waiting_subjects = [w["subject"] for w in device_store.open_waits_for_blocker(c, blocker)]
        device_store.clear_waits_for_blocker(c, blocker)
        updated, _, ok = inbox_store.resolve_item(
            c,
            numeric_id,
            resolution="continued",
            answer="continue",
            answered_by=actor,
            answered_at=now,
            base_version=base_version,
        )
        if not ok:
            raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=updated)
        unblocked = tuple(s for s in waiting_subjects if not device_store.has_open_waits(c, s))
        if unblocked:
            foundation.after_commit(lambda subjects=unblocked: resume_unblocked_subjects(subjects))
    return {"item": updated, "result": {"secret": blocker, "continued": True, "resumed": builtins.list(unblocked)}}


def retry_item(
    item_id: str | int,
    *,
    base_version: int | None = None,
    credential=None,
    pending_secrets=None,
) -> dict:
    numeric = inbox_store.parse_id(item_id)
    if numeric is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    item = inbox_store.get(numeric)
    if item is None:
        raise contract.Problem("not_found", f"No inbox item {item_id}")
    if item["state"] == "resolved":
        raise contract.Problem("already_resolved", f"Inbox item {item['id']} is already resolved.", current=item)
    if base_version is not None and item["version"] != base_version:
        raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=item)
    effective_version = item["version"] if base_version is None else base_version
    return resolve(
        item_id,
        base_version=effective_version,
        verb="retry",
        credential=credential,
        pending_secrets=pending_secrets,
    )


def resolve(
    item_id: str | int,
    *,
    base_version: int,
    verb: str,
    option=None,
    answer: str | None = None,
    answers: builtins.list | None = None,
    reason: str | None = None,
    fix: str | None = None,
    prevent: str | None = None,
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
    if subject.startswith("secret:"):
        return _resolve_secret_continue(
            item,
            numeric_id=numeric,
            base_version=base_version,
            actor=principals.current(),
        )
    if item["kind"] == "rescope" or subject.startswith("source:"):
        return _resolve_rescope(
            item,
            numeric_id=numeric,
            base_version=base_version,
            verb=verb,
        )
    if subject.startswith("ref:") and item["kind"] == "conflict":
        from . import sync as sync_mod

        ident_ref = subject.partition(":")[2]
        if not ident_ref.isdecimal():
            raise contract.Problem("validation", f"Invalid conflict subject {subject!r}")
        actor = principals.current()
        now = util.iso()
        with foundation.tx() as c:
            res_data = sync_mod.resolve_conflict(c, int(ident_ref), verb, actor=actor)
            resolved_item, _, changed = inbox_store.resolve_item(
                c,
                numeric,
                resolution=verb,
                answer=verb,
                answered_by=actor,
                answered_at=now,
                base_version=base_version,
            )
            if not changed:
                current = inbox_store.get(numeric, connect=lambda: nullcontext(c)) or item
                if current["state"] == "resolved":
                    raise contract.Problem("already_resolved", f"Inbox item {item['id']} is already resolved.", current=current)
                raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=current)
        return {"item": resolved_item, "result": res_data}

    if (item["kind"] == "approval" or subject.startswith("approval:")) and not allow_legacy_auth:
        if credential is None or credential.kind != "session":
            raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required")

    actor = principals.current()
    prefix, _, ident = subject.partition(":")
    handler_result: object = {}
    stored_answer = str(answer or "")
    resolution = ""
    keep_open = False
    updated_detail: dict | None = None

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
        elif prefix == "run" and item["kind"] == "stop":
            node = str(item["detail"].get("node") or "")
            if verb == "redirect":
                if fix is not None:
                    edited_fix = str(fix).strip()
                elif answer is not None:
                    edited_fix = str(answer).strip()
                else:
                    edited_fix = str((item["detail"].get("jidoka") or {}).get("fix") or "").strip()
                if not edited_fix:
                    raise contract.Problem("validation", "fix or answer is required for redirect.")
                handler_result = tasks.reply(ident, edited_fix)
                new_detail = dict(item["detail"])
                jidoka = dict(new_detail.get("jidoka") or {})
                jidoka["fix"] = edited_fix
                if prevent is not None:
                    jidoka["prevent"] = str(prevent).strip()
                new_detail["jidoka"] = jidoka
                updated_detail = new_detail
                resolution = "redirected"
                stored_answer = edited_fix
            elif verb == "retry":
                handler_result = tasks.resume(ident, node)
                resolution = "retried"
                stored_answer = verb
            else:
                try:
                    handler_result = control.stop_run(ident)
                except control.Refused:
                    run_row = run_store.run_get(ident)
                    if run_row is not None and run_row["status"] in ("failed", "stopped", "interrupted"):
                        handler_result = {"id": ident, "stopped": True, "was": run_row["status"]}
                    else:
                        raise
                resolution = "stopped"
                stored_answer = verb
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
        elif prefix == "step":
            run_id, sep, node = ident.partition("/")
            if not sep or not run_id or not node:
                raise contract.Problem("validation", f"Invalid step subject {subject!r}.")
            if verb == "retry":
                handler_result = tasks.resume(run_id, node)
                keep_open = True
                stored_answer = verb
            else:
                handler_result = {"run_id": run_id, "node": node, "dismissed": True}
                resolution = "dismissed"
                stored_answer = verb
        elif prefix == "draft":
            from . import drafts, miner

            draft_num = drafts.parse_id(ident)
            draft_row = drafts.get(draft_num) if draft_num is not None else None
            if draft_row is None:
                sync_source("proposals")
                current = inbox_store.get(numeric) or item
                raise contract.Problem("already_resolved", f"Draft {ident} no longer exists.", current=current)
            if item["kind"] == "watch":
                if verb == "roll_back":
                    prev_ver = item["detail"].get("previous_version")
                    if not prev_ver:
                        prev_info, _ = drafts._previous_and_new_version_info(draft_row)
                        if prev_info is not None:
                            prev_ver = (
                                str(prev_info["version"]) if prev_info.get("version") is not None
                                else prev_info.get("content_version") or prev_info.get("commit_sha") or ""
                            )
                    if not prev_ver:
                        raise contract.Problem("validation", f"No previous version recorded for {draft_row['id']}.")
                    rel = draft_row["path"] or drafts.default_path(draft_row["kind"], draft_row["name"])
                    live_ver = drafts._live_ver_for(draft_row["kind"], draft_row["name"], rel)
                    presence_ok = (credential is None and allow_legacy_auth) or (
                        credential is not None and credential.kind == "session"
                    )
                    try:
                        handler_result = drafts.rollback(
                            draft_row["kind"],
                            draft_row["name"],
                            prev_ver,
                            live_ver,
                            actor,
                            presence_passed=presence_ok,
                        )
                    except drafts.PresenceRequired:
                        raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required") from None
                    resolution = "rolled_back"
                else:
                    handler_result = {"draft": draft_row["id"], "continued": True}
                    resolution = "continued"
                stored_answer = verb
            elif draft_row["kind"] == "miner":
                cfg = config.load_or_problem()[0]
                if draft_row["state"] == "draft":
                    drafts.move(draft_row["numeric_id"], "draft", "valid", sync_inbox=False)
                    draft_row = drafts.get(draft_row["numeric_id"]) or draft_row
                bucket = miner.classify_draft(cfg, draft_row)
                if bucket in ("memory", "hand"):
                    if not drafts.move(draft_row["numeric_id"], "valid", "discarded", sync_inbox=False):
                        raise contract.Problem("invalid_state", f"Draft {draft_row['id']} is no longer open.")
                    handler_result = {"id": draft_row["numeric_id"]}
                    resolution = "accepted" if verb == "accept" else "declined"
                    stored_answer = verb
                elif bucket == "approve":
                    if verb == "decline":
                        if not drafts.move(
                            draft_row["numeric_id"], "valid", "declined",
                            reason=str(reason or answer or "declined"), sync_inbox=False,
                        ):
                            raise contract.Problem("invalid_state", f"Draft {draft_row['id']} is no longer open.")
                        handler_result = {"id": draft_row["numeric_id"]}
                        resolution = "declined"
                        stored_answer = verb
                    else:
                        handler_result = dashboard.approve_rule(draft_row["numeric_id"])
                        keep_open = True
                        stored_answer = verb
                else:
                    if verb == "decline":
                        if not drafts.move(
                            draft_row["numeric_id"], "valid", "declined",
                            reason=str(reason or answer or "declined"), sync_inbox=False,
                        ):
                            raise contract.Problem("invalid_state", f"Draft {draft_row['id']} is no longer open.")
                        handler_result = {"id": draft_row["numeric_id"]}
                        resolution = "declined"
                        stored_answer = verb
                    else:
                        handler_result = miner.apply_miner_draft(draft_row["numeric_id"])
                        resolution = "accepted"
                        stored_answer = verb
            else:
                if draft_row["gate"] == "C" and draft_row["author"].startswith("run:"):
                    if verb == "accept":
                        new_draft, _ = drafts.create_and_check(
                            draft_row["kind"],
                            draft_row["name"],
                            draft_row["body"],
                            author=actor,
                            path=draft_row["path"],
                            base_version=draft_row["base_version"],
                            why=draft_row["why"] or f"accepted from {draft_row['id']}",
                            evidence=draft_row["evidence"],
                            run_check=True,
                        )
                        drafts.move(
                            draft_row["numeric_id"],
                            draft_row["state"],
                            "discarded",
                            reason=f"accepted as {new_draft['id']}",
                            sync_inbox=False,
                        )
                        handler_result = {"draft": drafts.public(new_draft), "discarded": draft_row["id"]}
                        resolution = "accepted"
                    else:
                        if draft_row["state"] == "valid":
                            drafts.move(draft_row["numeric_id"], "valid", "declined",
                                        reason=str(reason or answer or ""), sync_inbox=False)
                        else:
                            drafts.move(draft_row["numeric_id"], draft_row["state"], "discarded",
                                        reason=str(reason or answer or ""), sync_inbox=False)
                        handler_result = {"draft": draft_row["id"], "declined": True}
                        resolution = "declined"
                    stored_answer = verb
                elif verb == "accept":
                    rel = draft_row["path"] or drafts.default_path(draft_row["kind"], draft_row["name"])
                    base_ver = draft_row["base_version"]
                    if not (base_ver and base_ver.isascii() and base_ver.isdecimal()):
                        base_ver = drafts._live_ver_for(draft_row["kind"], draft_row["name"], rel)
                    presence_ok = (credential is not None and credential.kind == "session") or allow_legacy_auth
                    try:
                        handler_result = drafts.activate(
                            draft_row["id"],
                            actor,
                            base_ver,
                            presence_passed=presence_ok,
                        )
                    except drafts.PresenceRequired:
                        raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required") from None
                    resolution = "accepted"
                    stored_answer = verb
                else:
                    if draft_row["state"] == "valid":
                        drafts.move(draft_row["numeric_id"], "valid", "declined",
                                    reason=str(reason or answer or ""), sync_inbox=False)
                    elif draft_row["state"] == "draft":
                        drafts.move(draft_row["numeric_id"], "draft", "discarded",
                                    reason=str(reason or answer or ""), sync_inbox=False)
                    handler_result = {"draft": draft_row["id"], "declined": True}
                    resolution = "declined"
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
        elif prefix in ("alert", "push"):
            handler_result = {"subject": subject, "continued": True}
            resolution = "continued"
            stored_answer = verb
        elif prefix == "config":
            if ident.startswith("prompt/") and verb in ("keep_mine", "take_engine"):
                import hashlib

                from . import configrepo, prompts

                prompt_name = ident.removeprefix("prompt/")
                rel = f"prompts/{prompt_name}.md"
                root = paths.CONFIG_DIR.resolve()
                use_repo = configrepo.is_repo(root)
                if verb == "keep_mine":
                    engine_file = paths.PROMPTS_DIR / f"{prompt_name}.md"
                    if not engine_file.is_file():
                        raise contract.Problem("not_found", f"No engine prompt {prompt_name!r}")
                    engine_sha = hashlib.sha256(engine_file.read_bytes()).hexdigest()
                    raw = configrepo.live_bytes(rel, root) if use_repo else (
                        (root / rel).read_bytes() if (root / rel).is_file() else None
                    )
                    if raw is None:
                        raise contract.Problem("not_found", f"No config override {rel}")
                    updated_text = prompts.rebase_override_base(raw.decode("utf-8"), engine_sha)
                    commit_sha = ""
                    if use_repo:
                        cur_blob = configrepo._live_blob(root, rel)
                        commit_sha = configrepo.commit(
                            {rel: updated_text},
                            {rel: cur_blob},
                            actor=actor,
                            source="dashboard",
                            note=f"keep_mine: rebase prompt/{prompt_name} to engine",
                        )
                    else:
                        (root / rel).write_text(updated_text, encoding="utf-8")
                    handler_result = {
                        "subject": subject,
                        "base": f"sha256:{engine_sha}",
                        "commit": commit_sha,
                    }
                    resolution = "kept_mine"
                else:
                    commit_sha = ""
                    if use_repo:
                        cur_blob = configrepo._live_blob(root, rel)
                        if cur_blob != configrepo.ABSENT:
                            commit_sha = configrepo.commit(
                                {rel: None},
                                {rel: cur_blob},
                                actor=actor,
                                source="dashboard",
                                note=f"take_engine: remove prompt/{prompt_name} override",
                            )
                    else:
                        with suppress(FileNotFoundError):
                            (root / rel).unlink()
                    handler_result = {"subject": subject, "deleted": True, "commit": commit_sha}
                    resolution = "took_engine"
            else:
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
            from .store import outbox as outbox_store

            ob_id = int(ident)
            raw_ob = outbox_store.get_raw(ob_id)
            if verb == "approve":
                if outbox.needs_presence("outbox.approve", ob_id) and not allow_legacy_auth:
                    if credential is None or credential.kind != "session":
                        raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required")
                if raw_ob is not None and raw_ob["state"] == "failed":
                    handler_result = outbox.retry(ob_id)
                else:
                    if raw_ob is not None and raw_ob["audience"] != "self" and raw_ob["opened_at"] is None:
                        outbox.open(ob_id)
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
            current = inbox_store.get(numeric, connect=lambda: nullcontext(c))
            if current is not None and current["state"] == "resolved" and prefix == "step" and verb == "retry":
                return {"item": current, "result": handler_result}
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
                detail=updated_detail,
            )
        if not ok:
            if updated and updated["state"] == "resolved":
                raise contract.Problem("already_resolved", f"Inbox item {updated['id']} is already resolved.", current=updated)
            raise contract.Problem("version_conflict", f"Inbox item {item['id']} changed.", current=updated)
        if prefix == "config" and ident.startswith("prompt/") and verb == "take_engine":
            for sibling_kind in ("drift", "config_invalid"):
                if sibling_kind == item["kind"]:
                    continue
                sibling = inbox_store.get_open_by_kind_and_subject(
                    sibling_kind, subject, connect=lambda: nullcontext(c)
                )
                if sibling is not None:
                    inbox_store.resolve_item(
                        c,
                        inbox_store.parse_id(sibling["id"]),
                        resolution="cleared",
                        answered_by=actor,
                        answered_at=now,
                    )
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
