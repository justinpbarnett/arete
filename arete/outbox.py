from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timedelta

from . import contract, gh, jira, outbox_config, principals, scrub, send_github, send_loop, send_push, senders, util
from .store import chats as chats_store
from .store import db as foundation
from .store import insights as insights_store
from .store import outbox as outbox_store
from .store import writes

RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 3600
MAX_ATTEMPTS = 8
HOLD_WINDOW_SECONDS = 60
STALE_SENDING_SECONDS = 120

CHECK_FIRST_ERROR = "the reply was lost; check the target object before running outbox.retry"

_HTTP_STATUS_RE = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry[- ]after[:\s]+(\d+)", re.IGNORECASE)


def _sync_inbox(c) -> None:
    from . import inbox

    inbox.sync(c, "outbox")
    inbox.sync(c, "approvals")


def _extract_http_status(text: str) -> int | None:
    match = _HTTP_STATUS_RE.search(text or "")
    return int(match.group(1)) if match else None


def _extract_retry_after(text: str) -> int | None:
    match = _RETRY_AFTER_RE.search(text or "")
    return int(match.group(1)) if match else None


def agent_slot(run_id: str, node: str, kind: str, obj: str, slot_name: str) -> str:
    digest = hashlib.sha256(json.dumps([run_id, node, kind, obj, slot_name]).encode()).hexdigest()
    return f"run:{digest}"


def is_person_drafter(drafted_by: str) -> bool:
    actor = (drafted_by or "").strip()
    if not actor:
        return False
    if actor in ("hand", "owner") or actor.startswith("u:") or "@" in actor:
        return True
    try:
        return bool(principals.is_person(actor))
    except Exception:
        return False


def _target_window(target: str) -> int:
    try:
        return outbox_config.target_for(target).send_window
    except ValueError:
        return HOLD_WINDOW_SECONDS


def _send_at_for(audience: str, now: datetime, *, send_window: int | None = None) -> str:
    if audience == "self":
        return util.iso(now)
    window = HOLD_WINDOW_SECONDS if send_window is None else send_window
    return util.iso(now + timedelta(seconds=window))


def _backoff_send_at(attempt_n: int, now: datetime) -> str:
    delay = min(MAX_RETRY_SECONDS, RETRY_SECONDS * (2 ** max(0, attempt_n - 1)))
    return util.iso(now + timedelta(seconds=delay))


def _require_item(c, item_id: str | int) -> tuple[int, dict, dict]:
    numeric = outbox_store.parse_id(item_id)
    if numeric is None:
        raise contract.Problem("not_found", f"No outbox item {item_id}")
    row = outbox_store.get_row(c, numeric)
    if row is None:
        raise contract.Problem("not_found", f"No outbox item {item_id}")
    return numeric, row, outbox_store.decode_on(c, row)


def _check_version(row: dict, current: dict, base_version: int | None) -> None:
    if base_version is not None and row["version"] != base_version:
        raise contract.Problem("version_conflict", f"Outbox item {current['id']} changed.", current=current)


def _check_rev(row: dict, current: dict, expected_rev: int | None, field: str) -> None:
    if expected_rev is not None and row["rev"] != expected_rev:
        raise contract.Problem(
            "version_conflict",
            f"Outbox item {current['id']} {field} is stale (current rev {row['rev']}).",
            current=current,
        )


def draft(
    c=None,
    *,
    target: str,
    kind: str,
    object: str = "",
    body: str,
    drafted_by: str,
    slot: str,
    audience: str | None = None,
    task_id: str | None = None,
    ref_id: str | None = None,
    run_id: str | None = None,
    step: str | None = None,
    loop: str | None = None,
    posts_as: str | None = None,
    base_remote_updated: str | None = None,
    summary: str = "",
    review: str = "",
    on_conflict_ignore: bool = False,
    hold_as_draft: bool = False,
    approved_by: str | None = None,
    opened_at: str | None = None,
) -> int:
    target_cfg = outbox_config.target_for(target)
    resolved_audience = target_cfg.audience if audience is None else audience
    resolved_posts_as = target_cfg.posts_as if posts_as is None else posts_as
    if resolved_audience not in outbox_store.AUDIENCES:
        raise ValueError(f"unsupported outbox audience {resolved_audience!r}")
    if kind not in target_cfg.kinds:
        raise ValueError(f"outbox target {target!r} does not allow kind {kind!r}")
    if not isinstance(slot, str) or not slot.strip():
        raise ValueError("outbox draft requires a non-empty slot")
    if not isinstance(drafted_by, str) or not drafted_by.strip():
        raise ValueError("outbox draft requires drafted_by")
    senders.resolve(target_cfg.connector, kind, object)
    cleaned, spans = scrub.text(body, "outbox", f"slot:{slot}", spans=True)
    now_dt = util.now()
    now = util.iso(now_dt)

    init_state = "draft"
    init_opened_at: str | None = None
    init_approved_by: str | None = None
    init_approved_rule: str | None = None
    init_approved_rule_version: str | None = None
    init_send_at: str | None = None
    if approved_by is not None:
        init_state = "approved"
        init_opened_at = opened_at or now
        init_approved_by = approved_by
        init_send_at = _send_at_for(resolved_audience, now_dt, send_window=target_cfg.send_window)
    elif not hold_as_draft:
        rule = outbox_config.match_auto_approve(
            target,
            kind,
            resolved_audience,
            drafted_by=drafted_by,
            person_drafted=is_person_drafter(drafted_by),
        )
        if rule is not None:
            init_state = "approved"
            init_approved_rule = rule.name
            init_approved_rule_version = rule.content_version
            init_send_at = now

    def _write(conn) -> int:
        existing = outbox_store.get_by_slot(conn, slot)
        if existing is not None:
            item_id = int(existing["id"])
            if not on_conflict_ignore and existing["state"] in ("draft", "edited"):
                latest = outbox_store.latest_revision(conn, item_id, existing["rev"])
                if latest is None or latest["body"] != cleaned:
                    outbox_store.append_revision(
                        conn,
                        item_id,
                        base_rev=existing["rev"],
                        by=drafted_by,
                        body=cleaned,
                        spans=spans,
                        now=now,
                        summary=summary or None,
                    )
                    _sync_inbox(conn)
            return item_id
        row = outbox_store.insert_draft(
            conn,
            target=target,
            kind=kind,
            object=object,
            audience=resolved_audience,
            posts_as=resolved_posts_as,
            task_id=task_id,
            ref_id=ref_id,
            run_id=run_id,
            step=step,
            loop=loop,
            drafted_by=drafted_by,
            slot=slot,
            base_remote_updated=base_remote_updated,
            summary=summary,
            body=cleaned,
            spans=spans,
            now=now,
            review=review,
            state=init_state,
            opened_at=init_opened_at,
            approved_by=init_approved_by,
            approved_rule=init_approved_rule,
            approved_rule_version=init_approved_rule_version,
            send_at=init_send_at,
        )
        item_id = int(row["id"])
        if kind == "label" and outbox_store.is_state_label_body(cleaned):
            outbox_store.discard_older_state_labels(conn, object, item_id, now)
        elif kind == "issue" and slot.startswith("board:"):
            outbox_store.discard_older_board_items(conn, object, item_id, now)
        _sync_inbox(conn)
        return item_id

    return outbox_store.run_with_tx(c, _write)


def list_items(
    *,
    state: str | tuple[str, ...] | None = None,
    task: str | None = None,
    run: str | None = None,
    connect=foundation.reader,
) -> list[dict]:
    return outbox_store.list_items(state=state, task=task, run=run, connect=connect)


def get(item_id: str | int, connect=foundation.reader) -> dict:
    detail = outbox_store.get_detail(item_id, connect=connect)
    if detail is None:
        raise contract.Problem("not_found", f"No outbox item {item_id}")
    return detail


def preview(item_id: str | int, connect=foundation.reader) -> dict:
    numeric = outbox_store.parse_id(item_id)
    if numeric is None:
        raise contract.Problem("not_found", f"No outbox item {item_id}")
    with connect() as c:
        row = outbox_store.get_row(c, numeric)
        if row is None:
            raise contract.Problem("not_found", f"No outbox item {item_id}")
        rev = outbox_store.latest_revision(c, numeric, row["rev"])
        raw_body = rev["body"] if rev is not None else row["summary"]
    rendered = senders.preview(row, raw_body)
    return {
        "item": outbox_store.format_id(numeric),
        "rev": row["rev"],
        "marker": row["marker"],
        "body": rendered,
    }


def history(item_id: str | int, connect=foundation.reader) -> list[dict]:
    rows = outbox_store.history(item_id, connect=connect)
    if rows is None:
        raise contract.Problem("not_found", f"No outbox item {item_id}")
    return rows


def needs_presence(op_name: str, item_id: str | int) -> bool:
    row = outbox_store.get_raw(item_id)
    if row is None:
        return False
    return op_name == "outbox.approve" and row["audience"] != "self"


def validate_for_presence(
    op_name: str,
    item_id: str | int,
    *,
    base_version: int | None = None,
    rev: int | None = None,
) -> None:
    with foundation.reader() as c:
        _, row, current = _require_item(c, item_id)
    _check_version(row, current, base_version)
    if op_name == "outbox.approve":
        _check_rev(row, current, rev, "rev")
        if row["state"] not in ("draft", "edited"):
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} cannot be approved from {row['state']}.",
                current=current,
            )
        if row["audience"] != "self" and row["opened_at"] is None:
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} must be opened before approving.",
                current=current,
            )
    elif op_name == "outbox.retry":
        if row["state"] != "failed":
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} cannot be retried from {row['state']}.",
                current=current,
            )
    elif op_name == "outbox.reject":
        allowed = ("draft", "edited", "approved", "held", "failed")
        if row["state"] not in allowed:
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} cannot be rejected from {row['state']}.",
                current=current,
            )


def open(item_id: str | int, *, base_version: int | None = None, c=None) -> dict:
    def _write(conn) -> dict:
        numeric, row, current = _require_item(conn, item_id)
        _check_version(row, current, base_version)
        if row["opened_at"] is not None:
            return current
        now = util.iso()
        updated = outbox_store.cas_update(
            conn,
            numeric,
            expected_version=row["version"],
            fields={"opened_at": now, "updated": now},
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, numeric))
            raise contract.Problem("version_conflict", f"Outbox item {current['id']} changed.", current=refreshed)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def edit(
    item_id: str | int,
    *,
    body: str,
    base_rev: int,
    base_version: int | None = None,
    by: str | None = None,
    c=None,
) -> dict:
    cleaned, spans = scrub.text(body, "outbox", f"outbox:{item_id}", spans=True)
    actor = by or principals.current()

    def _write(conn) -> dict:
        numeric, row, current = _require_item(conn, item_id)
        _check_version(row, current, base_version)
        _check_rev(row, current, base_rev, "base_rev")
        if row["state"] not in ("draft", "edited"):
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} cannot be edited from {row['state']}.",
                current=current,
            )
        now = util.iso()
        updated = outbox_store.append_revision(
            conn,
            numeric,
            base_rev=base_rev,
            by=actor,
            body=cleaned,
            spans=spans,
            now=now,
            next_state="edited",
            expected_version=row["version"],
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, numeric))
            raise contract.Problem("version_conflict", f"Outbox item {current['id']} changed.", current=refreshed)
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def approve(
    item_id: str | int,
    *,
    rev: int | None = None,
    base_version: int | None = None,
    approved_by: str | None = None,
    approved_rule: str | None = None,
    approved_rule_version: str | None = None,
    c=None,
) -> dict:
    actor = approved_by or principals.current()

    def _write(conn) -> dict:
        num, cur_row, cur_view = _require_item(conn, item_id)
        _check_version(cur_row, cur_view, base_version)
        _check_rev(cur_row, cur_view, rev, "rev")
        if cur_row["state"] not in ("draft", "edited"):
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {cur_view['id']} cannot be approved from {cur_row['state']}.",
                current=cur_view,
            )
        if cur_row["audience"] != "self":
            if approved_rule is not None:
                raise contract.Problem("validation", "Rules may only auto-approve self-audience items.")
            if cur_row["opened_at"] is None:
                raise contract.Problem(
                    "invalid_state",
                    f"Outbox item {cur_view['id']} must be opened before approving.",
                    current=cur_view,
                )
        now = util.now()
        now_s = util.iso(now)
        expected_rev = cur_row["rev"] if rev is None else rev
        updated = outbox_store.cas_update(
            conn,
            num,
            expected_version=cur_row["version"],
            expected_states=("draft", "edited"),
            expected_rev=expected_rev,
            fields={
                "state": "approved",
                "approved_by": actor,
                "approved_rule": approved_rule,
                "approved_rule_version": approved_rule_version,
                "send_at": _send_at_for(cur_row["audience"], now, send_window=_target_window(cur_row["target"])),
                "error": None,
                "updated": now_s,
            },
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, num))
            raise contract.Problem("version_conflict", f"Outbox item {cur_view['id']} changed.", current=refreshed)
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def undo(item_id: str | int, *, base_version: int | None = None, c=None) -> dict:
    def _write(conn) -> dict:
        numeric, row, current = _require_item(conn, item_id)
        _check_version(row, current, base_version)
        now_s = util.iso()
        can_undo = row["state"] == "held" or (
            row["state"] == "approved" and row["send_at"] is not None and now_s < row["send_at"]
        )
        if not can_undo:
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} cannot be undone from {row['state']}.",
                current=current,
            )
        next_state = "edited" if row["rev"] > 1 else "draft"
        updated = outbox_store.cas_update(
            conn,
            numeric,
            expected_version=row["version"],
            expected_states=("approved", "held"),
            fields={
                "state": next_state,
                "send_at": None,
                "approved_by": None,
                "approved_rule": None,
                "approved_rule_version": None,
                "updated": now_s,
            },
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, numeric))
            raise contract.Problem("version_conflict", f"Outbox item {current['id']} changed.", current=refreshed)
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def hold(item_id: str | int, *, base_version: int | None = None, c=None) -> dict:
    def _write(conn) -> dict:
        numeric, row, current = _require_item(conn, item_id)
        _check_version(row, current, base_version)
        now_s = util.iso()
        if row["state"] != "approved" or row["send_at"] is None or now_s >= row["send_at"]:
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} cannot be held from {row['state']}.",
                current=current,
            )
        updated = outbox_store.cas_update(
            conn,
            numeric,
            expected_version=row["version"],
            expected_states=("approved",),
            fields={"state": "held", "send_at": None, "updated": now_s},
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, numeric))
            raise contract.Problem("version_conflict", f"Outbox item {current['id']} changed.", current=refreshed)
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def release(item_id: str | int, *, base_version: int | None = None, c=None) -> dict:
    def _write(conn) -> dict:
        numeric, row, current = _require_item(conn, item_id)
        _check_version(row, current, base_version)
        if row["state"] != "held":
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} cannot be released from {row['state']}.",
                current=current,
            )
        now = util.now()
        now_s = util.iso(now)
        updated = outbox_store.cas_update(
            conn,
            numeric,
            expected_version=row["version"],
            expected_states=("held",),
            fields={
                "state": "approved",
                "send_at": _send_at_for(row["audience"], now, send_window=_target_window(row["target"])),
                "error": None,
                "updated": now_s,
            },
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, numeric))
            raise contract.Problem("version_conflict", f"Outbox item {current['id']} changed.", current=refreshed)
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def reject(item_id: str | int, *, reason: str = "", base_version: int | None = None, c=None) -> dict:
    cleanup_info: list[tuple[dict, str]] = []

    def _write(conn) -> dict:
        num, cur_row, cur_view = _require_item(conn, item_id)
        _check_version(cur_row, cur_view, base_version)
        if cur_row["state"] not in ("draft", "edited", "approved", "held", "failed"):
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {cur_view['id']} cannot be rejected from {cur_row['state']}.",
                current=cur_view,
            )
        now_s = util.iso()
        updated = outbox_store.cas_update(
            conn,
            num,
            expected_version=cur_row["version"],
            expected_states=("draft", "edited", "approved", "held", "failed"),
            fields={
                "state": "rejected",
                "reject_reason": reason,
                "send_at": None,
                "updated": now_s,
            },
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, num))
            raise contract.Problem("version_conflict", f"Outbox item {cur_view['id']} changed.", current=refreshed)
        if cur_row["kind"] == "push" and cur_row.get("run_id"):
            insights_store.settle_verdicts(
                cur_row["run_id"],
                "rejected",
                approval=cur_row.get("legacy_approval") or num,
                was=("", "waiting", "failed"),
                c=conn,
            )
        if cur_row["kind"] in ("push", "pr"):
            rev_row = outbox_store.latest_revision(conn, num, cur_row["rev"])
            if rev_row is not None:
                cleanup_info.append((cur_row, rev_row["body"]))
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    result = outbox_store.run_with_tx(c, _write)
    for row, body in cleanup_info:
        send_push.cleanup_rejected(row, body)
    return result


def retry(item_id: str | int, *, base_version: int | None = None, c=None) -> dict:
    def _write(conn) -> dict:
        num, cur_row, cur_view = _require_item(conn, item_id)
        _check_version(cur_row, cur_view, base_version)
        if cur_row["state"] != "failed":
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {cur_view['id']} cannot be retried from {cur_row['state']}.",
                current=cur_view,
            )
        now = util.now()
        now_s = util.iso(now)
        fields: dict[str, object] = {
            "state": "approved",
            "send_at": _send_at_for(cur_row["audience"], now, send_window=_target_window(cur_row["target"])),
            "error": None,
            "updated": now_s,
        }
        if not cur_row.get("approved_by"):
            fields["approved_by"] = principals.current()
        updated = outbox_store.cas_update(
            conn,
            num,
            expected_version=cur_row["version"],
            expected_states=("failed",),
            fields=fields,
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, num))
            raise contract.Problem("version_conflict", f"Outbox item {cur_view['id']} changed.", current=refreshed)
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def discard(item_id: str | int, *, base_version: int | None = None, c=None) -> dict:
    def _write(conn) -> dict:
        numeric, row, current = _require_item(conn, item_id)
        _check_version(row, current, base_version)
        if row["state"] not in ("draft", "edited", "held", "failed"):
            raise contract.Problem(
                "invalid_state",
                f"Outbox item {current['id']} cannot be discarded from {row['state']}.",
                current=current,
            )
        now_s = util.iso()
        updated = outbox_store.cas_update(
            conn,
            numeric,
            expected_version=row["version"],
            expected_states=("draft", "edited", "held", "failed"),
            fields={"state": "discarded", "send_at": None, "updated": now_s},
        )
        if updated is None:
            refreshed = outbox_store.decode_on(conn, outbox_store.get_row(conn, numeric))
            raise contract.Problem("version_conflict", f"Outbox item {current['id']} changed.", current=refreshed)
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def _parse_json_body(body: str) -> dict | None:
    stripped = (body or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _apply_sent_effects(c, row: dict, body: str) -> list[int]:
    cleanup_captures: list[int] = []
    kind = row["kind"]
    slot = row["slot"] or ""
    sent_ref = row.get("sent_ref")
    sent_url = row.get("sent_url") or ""
    payload = _parse_json_body(body) if kind in ("issue", "pr", "push") else None

    if kind == "issue" and payload is not None:
        finding = payload.get("finding")
        if isinstance(finding, dict) and finding.get("loop") and finding.get("fingerprint") and sent_url:
            c.execute(
                "UPDATE findings SET url = ? WHERE loop = ? AND fingerprint = ?",
                (sent_url, str(finding["loop"]), str(finding["fingerprint"])),
            )

    if kind == "issue" and slot.startswith("alert:") and sent_ref and str(sent_ref).isdigit():
        repo = (row.get("object") or "").removeprefix("github:")
        for alert_row in c.execute("SELECT key, opened FROM alerts").fetchall():
            if f"alert:{alert_row['key']}:{alert_row['opened']}" == slot:
                c.execute(
                    "UPDATE alerts SET issue = ?, repo = ? WHERE key = ? AND opened = ?",
                    (int(sent_ref), repo, alert_row["key"], alert_row["opened"]),
                )
                from . import inbox

                inbox.sync(c, "alerts")
                break

    if kind == "issue" and slot.startswith("chat:") and sent_url:
        parts = slot.split(":", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            chats_store.chat_say(int(parts[1]), "arete", f"Filed {sent_url}")

    if kind == "issue" and slot.startswith("capture:") and sent_ref and str(sent_ref).isdigit():
        draft_part = slot.removeprefix("capture:")
        if draft_part.isdigit():
            draft_id = int(draft_part)
            title = str((payload or {}).get("title") or "")
            insights_store.capture_filed(draft_id, int(sent_ref), sent_url, title)
            cleanup_captures.append(draft_id)

    if kind == "pr" and payload is not None and (payload.get("action") or "create") == "create" and sent_url:
        run_id = row.get("run_id")
        if run_id:
            run_row = c.execute("SELECT version, pr_url, branch FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is not None:
                urls = str(run_row["pr_url"] or "").split()
                if sent_url not in urls:
                    urls.append(sent_url)
                branch = str(payload.get("branch") or run_row["branch"] or "")
                fields: dict[str, object] = {"pr_url": " ".join(urls)}
                if branch:
                    fields["branch"] = branch
                writes.update("runs", run_id, fields, base_version=int(run_row["version"]))

    if kind == "push" and row.get("run_id"):
        run_id = str(row["run_id"])
        verdict_outcome = "approved" if row.get("approved_by") else "landed"
        approval_ref = row.get("legacy_approval") or (int(row["id"]) if row.get("approved_by") else None)
        insights_store.settle_verdicts(
            run_id,
            verdict_outcome,
            approval=approval_ref,
            was=("", "waiting", "failed"),
            c=c,
        )
        if sent_ref:
            insights_store.set_verdict_sha(run_id, str(sent_ref), c=c)

    return cleanup_captures


def _note_capture_retry(c, row: dict, event_kind: str) -> None:
    slot = row.get("slot") or ""
    if row.get("kind") != "issue" or not slot.startswith("capture:"):
        return
    draft_part = slot.removeprefix("capture:")
    if not draft_part.isdigit():
        return
    ob_id = outbox_store.format_id(int(row["id"]))
    err = util.first_line(str(row.get("error") or ""), 160)
    if event_kind == "outbox.failed":
        note = f"Filing in outbox {ob_id} failed ({err}); retry it from the outbox."
    else:
        note = f"Filing in outbox {ob_id} had a temporary error ({err}); it stays in the outbox and retries automatically."
    c.execute("UPDATE captures SET delivery = ? WHERE id = ? AND status = 'queued'", (note, int(draft_part)))


def _settle_tx(
    item_id: int,
    claimed_version: int,
    attempt_n: int,
    *,
    item_fields: dict[str, object],
    attempt_fields: dict[str, object],
    event_kind: str,
    body: str = "",
) -> dict | None:
    now_s = util.iso()
    cleanup_captures: list[int] = []
    with writes.atomic() as c:
        after = outbox_store.settle_sending(
            c,
            item_id,
            claimed_version,
            attempt_n,
            item_fields=item_fields,
            attempt_fields=attempt_fields,
            event_kind=event_kind,
            now_iso=now_s,
        )
        if after is not None:
            if event_kind == "outbox.sent":
                cleanup_captures = _apply_sent_effects(c, after, body)
            else:
                if event_kind == "outbox.failed" and after["kind"] == "push" and after.get("run_id"):
                    insights_store.settle_verdicts(
                        str(after["run_id"]),
                        "failed",
                        approval=after.get("legacy_approval") or int(after["id"]),
                        was=("", "waiting"),
                        c=c,
                    )
                _note_capture_retry(c, after, event_kind)
            _sync_inbox(c)
    if cleanup_captures:
        from . import capture

        with capture.FILES:
            for draft_id in cleanup_captures:
                shutil.rmtree(capture.CAPTURES_DIR / str(draft_id), ignore_errors=True)
    return after


def _settle_unknown(
    item_id: int,
    claimed_version: int,
    attempt_n: int,
    *,
    error_text: str,
    lookup_note: str | None = None,
    http_status: int | None = None,
) -> None:
    now = util.now()
    exhausted = attempt_n >= MAX_ATTEMPTS
    attempt_fields: dict[str, object] = {
        "outcome": "unknown",
        "error": error_text,
    }
    if lookup_note is not None:
        attempt_fields["lookup"] = lookup_note
    if http_status is not None:
        attempt_fields["http_status"] = http_status
    if exhausted:
        item_fields: dict[str, object] = {
            "state": "failed",
            "send_at": None,
            "error": CHECK_FIRST_ERROR,
        }
        event_kind = "outbox.failed"
    else:
        item_fields = {
            "state": "approved",
            "send_at": _backoff_send_at(attempt_n, now),
            "error": error_text,
        }
        event_kind = "outbox.changed"
    _settle_tx(
        item_id,
        claimed_version,
        attempt_n,
        item_fields=item_fields,
        attempt_fields=attempt_fields,
        event_kind=event_kind,
    )


def _deliver_one(item_id: int, expected_version: int, now_iso: str, *, allow_bypass_fifo: bool = False) -> None:
    with foundation.tx() as c:
        peek = outbox_store.get_row(c, item_id)
        if peek is None or peek["state"] != "approved":
            return
        if outbox_store._row_is_state_label(c, peek):
            if outbox_store.has_newer_active_state_label(c, peek["object"], item_id):
                outbox_store.discard_item(c, item_id, now_iso)
                _sync_inbox(c)
                return
            outbox_store.discard_older_state_labels(c, peek["object"], item_id, now_iso)
        claimed_pair = outbox_store.claim_due(
            c, item_id, expected_version, now_iso, allow_bypass_fifo=allow_bypass_fifo
        )
        if claimed_pair is None:
            return
        claimed, attempt_n = claimed_pair
        claimed_version = int(claimed["version"])
        rev_row = outbox_store.latest_revision(c, item_id, claimed["rev"])
        latest_body = rev_row["body"] if rev_row is not None else ""
        prev_attempt = outbox_store.last_attempt(c, item_id, before_n=attempt_n)
        first_started = outbox_store.first_attempt_started(c, item_id) or now_iso
        _sync_inbox(c)

    cleaned, spans = scrub.text(latest_body, "outbox", f"outbox:{item_id}", spans=True)
    if spans or cleaned != latest_body:
        _settle_tx(
            item_id,
            claimed_version,
            attempt_n,
            item_fields={"state": "held", "send_at": None, "error": "scrub hit"},
            attempt_fields={"outcome": "refused", "error": "scrub hit"},
            event_kind="outbox.changed",
        )
        return

    from . import inbox

    if missing := inbox.check_outbox_missing_secrets(claimed):
        names = ", ".join(f"{s['scope']}/{s['name']}" for s in missing)
        wait_err = f"waiting on missing secret {names}"
        _settle_tx(
            item_id,
            claimed_version,
            attempt_n,
            item_fields={"state": "approved", "send_at": None, "error": wait_err},
            attempt_fields={"outcome": "refused", "error": wait_err},
            event_kind="outbox.changed",
        )
        return

    if claimed["kind"] == "loop":
        try:
            with writes.atomic() as c:
                sent_result = send_loop.deliver_in_tx(c, claimed, latest_body)
                now_s = util.iso()
                outbox_store.settle_sending(
                    c,
                    item_id,
                    claimed_version,
                    attempt_n,
                    item_fields={
                        "state": "sent",
                        "send_at": None,
                        "sent_at": now_s,
                        "sent_ref": sent_result.external_id,
                        "sent_url": sent_result.external_url,
                        "error": None,
                    },
                    attempt_fields={
                        "outcome": "sent",
                        "external_id": sent_result.external_id,
                        "external_url": sent_result.external_url,
                    },
                    event_kind="outbox.sent",
                    now_iso=now_s,
                )
                _sync_inbox(c)
        except send_loop.LoopTemporaryError as err:
            _settle_unknown(
                item_id,
                claimed_version,
                attempt_n,
                error_text=util.first_line(str(err), 300),
            )
        except (send_loop.LoopPermanentError, ValueError) as err:
            short_err = util.first_line(str(err), 300)
            _settle_tx(
                item_id,
                claimed_version,
                attempt_n,
                item_fields={"state": "failed", "send_at": None, "error": short_err},
                attempt_fields={"outcome": "refused", "error": short_err},
                event_kind="outbox.failed",
            )
        except Exception as err:
            _settle_unknown(
                item_id,
                claimed_version,
                attempt_n,
                error_text=util.first_line(str(err), 300),
            )
        return

    if claimed["kind"] == "email":
        _settle_tx(
            item_id,
            claimed_version,
            attempt_n,
            item_fields={"state": "failed", "send_at": None, "error": "no email connector"},
            attempt_fields={"outcome": "refused", "error": "no email connector"},
            event_kind="outbox.failed",
        )
        return

    sender = senders.resolve(claimed["target"], claimed["kind"], claimed.get("object") or "")
    lookup_note: str | None = None
    claimed_ctx = {
        **claimed,
        "body": latest_body,
        "attempt_n": attempt_n,
        "prev_external_id": (prev_attempt or {}).get("external_id"),
    }

    if prev_attempt is not None and prev_attempt.get("outcome") == "unknown":
        since_iso = util.iso(util.parse_iso(first_started) - timedelta(seconds=60))
        try:
            look_result = sender.look(claimed_ctx, since_iso)
        except Exception as err:
            first_err = util.first_line(str(err), 300)
            _settle_unknown(
                item_id,
                claimed_version,
                attempt_n,
                error_text=first_err,
                lookup_note=f"failed: {first_err}",
                http_status=_extract_http_status(str(err)),
            )
            return
        lookup_note = look_result.summary
        if look_result.found:
            now_s = util.iso()
            _settle_tx(
                item_id,
                claimed_version,
                attempt_n,
                item_fields={
                    "state": "sent",
                    "send_at": None,
                    "sent_at": now_s,
                    "sent_ref": look_result.external_id,
                    "sent_url": look_result.external_url,
                    "error": None,
                },
                attempt_fields={
                    "outcome": "found",
                    "external_id": look_result.external_id,
                    "external_url": look_result.external_url,
                    "lookup": lookup_note,
                },
                event_kind="outbox.sent",
                body=latest_body,
            )
            return

    try:
        sent_result = sender.send(claimed_ctx, latest_body)
    except (gh.GhUnsure, jira.JiraUnsure, send_push.PushUnsure) as err:
        _settle_unknown(
            item_id,
            claimed_version,
            attempt_n,
            error_text=util.first_line(str(err), 300),
            lookup_note=lookup_note,
            http_status=_extract_http_status(str(err)) or (getattr(err, "status", None) or None),
        )
        return
    except send_push.PushRefused as err:
        short_err = util.first_line(str(err), 300)
        attempt_fields: dict[str, object] = {"outcome": "refused", "error": short_err}
        if lookup_note is not None:
            attempt_fields["lookup"] = lookup_note
        if err.retryable and attempt_n < MAX_ATTEMPTS:
            item_fields: dict[str, object] = {
                "state": "approved",
                "send_at": util.iso(),
                "error": short_err,
            }
            event_kind = "outbox.changed"
        else:
            item_fields = {"state": "failed", "send_at": None, "error": short_err}
            event_kind = "outbox.failed"
        _settle_tx(
            item_id,
            claimed_version,
            attempt_n,
            item_fields=item_fields,
            attempt_fields=attempt_fields,
            event_kind=event_kind,
        )
        return
    except (send_push.SecretScanError, send_push.PushError, send_loop.EmailUnavailable, send_loop.LoopPermanentError) as err:
        short_err = util.first_line(str(err), 300)
        attempt_fields = {"outcome": "refused", "error": short_err}
        if lookup_note is not None:
            attempt_fields["lookup"] = lookup_note
        _settle_tx(
            item_id,
            claimed_version,
            attempt_n,
            item_fields={"state": "failed", "send_at": None, "error": short_err},
            attempt_fields=attempt_fields,
            event_kind="outbox.failed",
        )
        return
    except send_github.PrMergeHeld as err:
        short_err = util.first_line(str(err.hold), 300)
        _settle_tx(
            item_id,
            claimed_version,
            attempt_n,
            item_fields={"state": "failed", "send_at": None, "error": short_err},
            attempt_fields={"outcome": "refused", "lookup": "merge_held", "error": short_err},
            event_kind="outbox.failed",
        )
        return
    except (gh.GhError, jira.JiraError) as err:
        err_text = str(err)
        short_err = util.first_line(err_text, 300)
        status = _extract_http_status(err_text) or (getattr(err, "status", None) or None)
        if status == 429 or "HTTP 429" in err_text or "rate limit" in err_text.lower():
            wait_seconds = _extract_retry_after(err_text)
            if wait_seconds is None:
                wait_seconds = 60
            now = util.now()
            exhausted = attempt_n >= MAX_ATTEMPTS
            attempt_fields = {
                "outcome": "refused",
                "http_status": status or 429,
                "retry_after": wait_seconds,
                "error": short_err,
            }
            if lookup_note is not None:
                attempt_fields["lookup"] = lookup_note
            if exhausted:
                item_fields = {
                    "state": "failed",
                    "send_at": None,
                    "error": short_err,
                }
                event_kind = "outbox.failed"
            else:
                item_fields = {
                    "state": "approved",
                    "send_at": util.iso(now + timedelta(seconds=wait_seconds)),
                    "error": short_err,
                }
                event_kind = "outbox.changed"
            _settle_tx(
                item_id,
                claimed_version,
                attempt_n,
                item_fields=item_fields,
                attempt_fields=attempt_fields,
                event_kind=event_kind,
            )
            return
        attempt_fields = {"outcome": "refused", "error": short_err}
        if status is not None:
            attempt_fields["http_status"] = status
        if lookup_note is not None:
            attempt_fields["lookup"] = lookup_note
        _settle_tx(
            item_id,
            claimed_version,
            attempt_n,
            item_fields={"state": "failed", "send_at": None, "error": short_err},
            attempt_fields=attempt_fields,
            event_kind="outbox.failed",
        )
        return
    except Exception as err:
        _settle_unknown(
            item_id,
            claimed_version,
            attempt_n,
            error_text=util.first_line(str(err), 300),
            lookup_note=lookup_note,
            http_status=_extract_http_status(str(err)),
        )
        return

    now_s = util.iso()
    attempt_fields = {
        "outcome": "sent",
        "external_id": sent_result.external_id,
        "external_url": sent_result.external_url,
    }
    if lookup_note is not None:
        attempt_fields["lookup"] = lookup_note
    _settle_tx(
        item_id,
        claimed_version,
        attempt_n,
        item_fields={
            "state": "sent",
            "send_at": None,
            "sent_at": now_s,
            "sent_ref": sent_result.external_id,
            "sent_url": sent_result.external_url,
            "error": None,
        },
        attempt_fields=attempt_fields,
        event_kind="outbox.sent",
        body=latest_body,
    )


def deliver_now(item_id: int, *, retry_unknown: bool = False) -> dict:
    for pass_no in range(2 if retry_unknown else 1):
        with foundation.reader() as c:
            row = outbox_store.get_row(c, item_id)
        if row is None:
            raise contract.Problem("not_found", f"No outbox item {outbox_store.format_id(item_id)}")
        if row["state"] != "approved":
            break
        now_iso = util.iso()
        if row.get("send_at") and str(row["send_at"]) > now_iso:
            with foundation.tx() as c:
                outbox_store.cas_update(
                    c,
                    item_id,
                    expected_version=int(row["version"]),
                    expected_states=("approved",),
                    fields={"send_at": now_iso, "updated": now_iso},
                )
            with foundation.reader() as c:
                row = outbox_store.get_row(c, item_id)
        _deliver_one(item_id, int(row["version"]), now_iso, allow_bypass_fifo=True)
        with foundation.reader() as c:
            after = outbox_store.get_row(c, item_id)
        if after is None or after["state"] != "approved" or pass_no > 0:
            break
    with foundation.reader() as c:
        final = outbox_store.get_row(c, item_id)
        if final is None:
            raise contract.Problem("not_found", f"No outbox item {outbox_store.format_id(item_id)}")
        return outbox_store.decode_on(c, final)


def send_due(now: datetime | str | None = None) -> int:
    if isinstance(now, datetime):
        now_iso = util.iso(now)
    elif isinstance(now, str):
        now_iso = now
    else:
        now_iso = util.iso()
    candidates = outbox_store.due_candidates(now_iso)
    processed = 0
    for item_id, expected_version in candidates:
        _deliver_one(item_id, expected_version, now_iso)
        processed += 1
    return processed


def sweep(c=None, since: datetime | str | None = None) -> list[int]:
    if isinstance(since, datetime):
        since_iso = util.iso(since)
    elif isinstance(since, str):
        since_iso = since
    else:
        since_iso = util.iso()
    now_iso = util.iso()

    def _run(conn) -> list[int]:
        if not outbox_store.available(conn):
            return []
        swept: list[int] = []
        for row in outbox_store.stale_sending(conn, since_iso):
            after = outbox_store.sweep_one(conn, int(row["id"]), int(row["version"]), int(row["n"]), now_iso)
            if after is not None:
                swept.append(int(row["id"]))
        if swept:
            _sync_inbox(conn)
        return swept

    return outbox_store.run_with_tx(c, _run)


def publish_push(
    *,
    repo_name: str,
    head: str,
    branch: str = "",
    base: str = "",
    lease: str = "",
    use_lease: bool = True,
    force: bool = False,
    start: str = "",
    mode: str = "branch",
    check: str = "",
    require_check: bool = False,
    run_id: str = "",
    slot: str = "",
    drafted_by: str = "arete",
    summary: str = "",
    timeout: int = 600,
) -> dict:
    import secrets

    run_id = run_id if isinstance(run_id, str) else ""
    payload: dict[str, object] = {
        "mode": mode,
        "repo": repo_name,
        "base": base,
        "branch": branch,
        "start": start,
        "head": head,
        "lease": lease,
        "use_lease": use_lease,
        "force": force,
        "timeout": timeout,
        "require_check": require_check,
    }
    if check:
        payload["check"] = check
    obj = f"git:{repo_name}"
    resolved_slot = slot or (
        agent_slot(run_id, "", "push", obj, f"push:{branch or base}:{head[:12]}:{secrets.token_hex(4)}")
        if run_id
        else f"push:{repo_name}:{branch or base}:{head[:12]}:{secrets.token_hex(4)}"
    )
    item_id = draft(
        target="git",
        kind="push",
        object=obj,
        body=json.dumps(payload),
        drafted_by=drafted_by or (f"run:{run_id}" if run_id else "arete"),
        slot=resolved_slot,
        run_id=run_id or None,
        summary=summary or f"push {repo_name} {head[:10]} -> {branch or base}",
    )
    result = deliver_now(item_id, retry_unknown=False)
    if result["state"] == "sent":
        return result
    if result["state"] == "approved":
        raise send_push.PushUnsure(result.get("error") or f"push to {repo_name} unconfirmed")
    raise RuntimeError(result.get("error") or f"push to {repo_name} failed")


def publish_pr(
    *,
    action: str,
    repo: str,
    name: str = "",
    base: str = "",
    branch: str = "",
    head: str = "",
    lease: str = "",
    url: str = "",
    number: int | None = None,
    title: str = "",
    body: str = "",
    link: str = "",
    labels: tuple[str, ...] | list[str] = (),
    draft_pr: bool = False,
    base_sha: str = "",
    track_moved_base: bool = False,
    required: dict | None = None,
    stage_checks: dict | None = None,
    run_id: str = "",
    slot: str = "",
    drafted_by: str = "arete",
    summary: str = "",
    retry_unknown: bool = False,
) -> dict:
    import secrets

    run_id = run_id if isinstance(run_id, str) else ""
    payload: dict[str, object] = {"action": action, "repo": repo}
    if name:
        payload["name"] = name
    if base:
        payload["base"] = base
    if branch:
        payload["branch"] = branch
    if head:
        payload["head"] = head
    if lease:
        payload["lease"] = lease
    if url:
        payload["url"] = url
    if number is not None:
        payload["number"] = number
    if action == "create":
        payload["title"] = title
        payload["body"] = body
        payload["link"] = link
        payload["labels"] = [str(lb) for lb in labels] if isinstance(labels, (list, tuple, set)) else []
        payload["draft"] = draft_pr
    elif action == "body":
        payload["body"] = body
        payload["link"] = link
    elif action == "draft":
        payload["draft"] = draft_pr
    elif action == "merge":
        payload["base_sha"] = base_sha
        payload["track_moved_base"] = track_moved_base
        if required is not None:
            payload["required"] = required
        if stage_checks is not None:
            payload["stage_checks"] = stage_checks

    pr_num = number if number is not None else (gh.pr_number(url) if url else None)
    obj = f"github:{repo}#{pr_num}" if (action != "create" and pr_num is not None) else f"github:{repo}"
    resolved_slot = slot or (
        agent_slot(run_id, "", "pr", obj, f"pr:{action}:{branch or pr_num}:{secrets.token_hex(4)}")
        if run_id
        else f"pr:{action}:{repo}:{branch or pr_num}:{secrets.token_hex(4)}"
    )
    item_id = draft(
        target="github",
        kind="pr",
        object=obj,
        body=json.dumps(payload),
        drafted_by=drafted_by or (f"run:{run_id}" if run_id else "arete"),
        slot=resolved_slot,
        run_id=run_id or None,
        summary=summary or f"pr {action} on {obj}",
    )
    result = deliver_now(item_id, retry_unknown=retry_unknown)
    if result["state"] == "sent":
        return result
    if result["state"] == "approved":
        raise gh.GhUnsure(result.get("error") or f"PR {action} on {obj} unconfirmed")
    err = result.get("error") or f"PR {action} on {obj} failed"
    if action == "merge" and track_moved_base and "base changed" in err.lower():
        raise gh.GhError(gh.BaseTipChanged(err))
    raise gh.GhError(err)


def publish_pr_merge(
    *,
    repo: str,
    number: int,
    head: str,
    base: str,
    base_sha: str,
    track_moved_base: bool = False,
    required: dict | None = None,
    stage_checks: dict | None = None,
    run_id: str = "",
    slot: str = "",
    drafted_by: str = "arete",
) -> str:
    import secrets

    run_id = run_id if isinstance(run_id, str) else ""
    payload: dict[str, object] = {
        "action": "merge",
        "repo": repo,
        "number": number,
        "url": f"https://github.com/{repo}/pull/{number}",
        "head": head,
        "base": base,
        "base_sha": base_sha,
        "track_moved_base": track_moved_base,
    }
    if required is not None:
        payload["required"] = required
    if stage_checks is not None:
        payload["stage_checks"] = stage_checks
    obj = f"github:{repo}#{number}"
    resolved_slot = slot or (
        agent_slot(run_id, "", "pr", obj, f"pr:merge:{number}:{head[:12]}:{secrets.token_hex(4)}")
        if run_id
        else f"pr:merge:{repo}#{number}:{head[:12]}:{secrets.token_hex(4)}"
    )
    item_id = draft(
        target="github",
        kind="pr",
        object=obj,
        body=json.dumps(payload),
        drafted_by=drafted_by or (f"run:{run_id}" if run_id else "arete"),
        slot=resolved_slot,
        run_id=run_id or None,
        summary=f"pr merge on {obj}",
    )
    result = deliver_now(item_id, retry_unknown=False)
    if result["state"] == "sent":
        return ""
    if result["state"] == "approved":
        raise gh.GhUnsure(result.get("error") or f"PR merge on {obj} unconfirmed")
    err = result.get("error") or f"PR merge on {obj} failed"
    with foundation.reader() as c:
        last = outbox_store.last_attempt(c, item_id)
    if last is not None and last.get("lookup") == "merge_held":
        if track_moved_base and "base changed" in err.lower():
            return gh.BaseTipChanged(err)
        return err
    raise gh.GhError(err)
