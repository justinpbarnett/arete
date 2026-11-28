from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import contract, delivery, gh, jira, principals, scrub, senders, util
from .store import db as foundation
from .store import outbox as outbox_store

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


def _extract_http_status(text: str) -> int | None:
    match = _HTTP_STATUS_RE.search(text or "")
    return int(match.group(1)) if match else None


def _extract_retry_after(text: str) -> int | None:
    match = _RETRY_AFTER_RE.search(text or "")
    return int(match.group(1)) if match else None


def _send_at_for(audience: str, now: datetime) -> str:
    if audience == "self":
        return util.iso(now)
    return util.iso(now + timedelta(seconds=HOLD_WINDOW_SECONDS))


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
    audience: str = "self",
    task_id: str | None = None,
    ref_id: str | None = None,
    run_id: str | None = None,
    step: str | None = None,
    loop: str | None = None,
    posts_as: str | None = None,
    base_remote_updated: str | None = None,
    summary: str = "",
) -> int:
    if audience not in outbox_store.AUDIENCES:
        raise ValueError(f"unsupported outbox audience {audience!r}")
    if not isinstance(slot, str) or not slot.strip():
        raise ValueError("outbox draft requires a non-empty slot")
    if not isinstance(drafted_by, str) or not drafted_by.strip():
        raise ValueError("outbox draft requires drafted_by")
    senders.resolve(target, kind, object)
    cleaned, spans = scrub.text(body, "outbox", f"slot:{slot}", spans=True)
    now = util.iso()

    def _write(conn) -> int:
        existing = outbox_store.get_by_slot(conn, slot)
        if existing is not None:
            item_id = int(existing["id"])
            if existing["state"] in ("draft", "edited"):
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
            audience=audience,
            posts_as=posts_as,
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
        )
        _sync_inbox(conn)
        return int(row["id"])

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
    if row["legacy_approval"] is not None:
        return op_name in ("outbox.approve", "outbox.retry", "outbox.reject")
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
        if row["legacy_approval"] is not None:
            if row["state"] != "draft":
                raise contract.Problem(
                    "invalid_state",
                    f"Legacy outbox item {current['id']} cannot be approved from {row['state']}.",
                    current=current,
                )
        else:
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
        allowed = ("draft", "failed") if row["legacy_approval"] is not None else ("draft", "edited", "approved", "held")
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
        if row["legacy_approval"] is not None:
            raise contract.Problem(
                "invalid_state",
                f"Legacy outbox item {current['id']} cannot be edited.",
                current=current,
            )
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


def _run_legacy_decide(numeric_id: int, legacy_id: int, yes: bool) -> dict:
    try:
        delivery.decide(legacy_id, yes)
    except delivery.AlreadyDecided as error:
        current = outbox_store.get(numeric_id)
        raise contract.Problem("invalid_state", str(error), current=current) from None
    except delivery.Failed:
        pass
    refreshed = outbox_store.get(numeric_id)
    if refreshed is None:
        raise contract.Problem("not_found", f"No outbox item {outbox_store.format_id(numeric_id)}")
    return refreshed


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
    with outbox_store.reader_for(c)() as reader_conn:
        numeric, row, current = _require_item(reader_conn, item_id)
    if row["legacy_approval"] is not None:
        _check_version(row, current, base_version)
        _check_rev(row, current, rev, "rev")
        if row["state"] != "draft":
            raise contract.Problem(
                "invalid_state",
                f"Legacy outbox item {current['id']} cannot be approved from {row['state']}.",
                current=current,
            )
        return _run_legacy_decide(numeric, int(row["legacy_approval"]), True)

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
                "send_at": _send_at_for(cur_row["audience"], now),
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
        if row["legacy_approval"] is not None:
            raise contract.Problem(
                "invalid_state",
                f"Legacy outbox item {current['id']} does not support undo.",
                current=current,
            )
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
        if row["legacy_approval"] is not None:
            raise contract.Problem(
                "invalid_state",
                f"Legacy outbox item {current['id']} does not support hold.",
                current=current,
            )
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
        if row["legacy_approval"] is not None:
            raise contract.Problem(
                "invalid_state",
                f"Legacy outbox item {current['id']} does not support release.",
                current=current,
            )
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
                "send_at": _send_at_for(row["audience"], now),
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
    with outbox_store.reader_for(c)() as reader_conn:
        numeric, row, current = _require_item(reader_conn, item_id)
    if row["legacy_approval"] is not None:
        _check_version(row, current, base_version)
        if row["state"] not in ("draft", "failed"):
            raise contract.Problem(
                "invalid_state",
                f"Legacy outbox item {current['id']} cannot be rejected from {row['state']}.",
                current=current,
            )
        return _run_legacy_decide(numeric, int(row["legacy_approval"]), False)

    def _write(conn) -> dict:
        num, cur_row, cur_view = _require_item(conn, item_id)
        _check_version(cur_row, cur_view, base_version)
        if cur_row["state"] not in ("draft", "edited", "approved", "held"):
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
            expected_states=("draft", "edited", "approved", "held"),
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
        _sync_inbox(conn)
        return outbox_store.decode_on(conn, updated)

    return outbox_store.run_with_tx(c, _write)


def retry(item_id: str | int, *, base_version: int | None = None, c=None) -> dict:
    with outbox_store.reader_for(c)() as reader_conn:
        numeric, row, current = _require_item(reader_conn, item_id)
    if row["legacy_approval"] is not None:
        _check_version(row, current, base_version)
        if row["state"] != "failed":
            raise contract.Problem(
                "invalid_state",
                f"Legacy outbox item {current['id']} cannot be retried from {row['state']}.",
                current=current,
            )
        return _run_legacy_decide(numeric, int(row["legacy_approval"]), True)

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
        updated = outbox_store.cas_update(
            conn,
            num,
            expected_version=cur_row["version"],
            expected_states=("failed",),
            fields={
                "state": "approved",
                "send_at": _send_at_for(cur_row["audience"], now),
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


def discard(item_id: str | int, *, base_version: int | None = None, c=None) -> dict:
    def _write(conn) -> dict:
        numeric, row, current = _require_item(conn, item_id)
        if row["legacy_approval"] is not None:
            raise contract.Problem(
                "invalid_state",
                f"Legacy outbox item {current['id']} does not support discard.",
                current=current,
            )
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


def _settle_tx(
    item_id: int,
    claimed_version: int,
    attempt_n: int,
    *,
    item_fields: dict[str, object],
    attempt_fields: dict[str, object],
    event_kind: str,
) -> dict | None:
    now_s = util.iso()
    with foundation.tx() as c:
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
            _sync_inbox(c)
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


def _deliver_one(item_id: int, expected_version: int, now_iso: str) -> None:
    with foundation.tx() as c:
        claimed_pair = outbox_store.claim_due(c, item_id, expected_version, now_iso)
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

    sender = senders.resolve(claimed["target"], claimed["kind"], claimed.get("object") or "")
    lookup_note: str | None = None

    if prev_attempt is not None and prev_attempt.get("outcome") == "unknown":
        since_iso = util.iso(util.parse_iso(first_started) - timedelta(seconds=60))
        try:
            look_result = sender.look(claimed, since_iso)
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
            )
            return

    try:
        sent_result = sender.send(claimed, latest_body)
    except (gh.GhUnsure, jira.JiraUnsure) as err:
        _settle_unknown(
            item_id,
            claimed_version,
            attempt_n,
            error_text=util.first_line(str(err), 300),
            lookup_note=lookup_note,
            http_status=_extract_http_status(str(err)),
        )
        return
    except gh.GhError as err:
        err_text = str(err)
        short_err = util.first_line(err_text, 300)
        status = _extract_http_status(err_text)
        if "HTTP 429" in err_text or "rate limit" in err_text.lower():
            wait_seconds = _extract_retry_after(err_text)
            if wait_seconds is None:
                wait_seconds = 60
            now = util.now()
            exhausted = attempt_n >= MAX_ATTEMPTS
            attempt_fields: dict[str, object] = {
                "outcome": "refused",
                "http_status": status or 429,
                "retry_after": wait_seconds,
                "error": short_err,
            }
            if lookup_note is not None:
                attempt_fields["lookup"] = lookup_note
            if exhausted:
                item_fields: dict[str, object] = {
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
    )


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
