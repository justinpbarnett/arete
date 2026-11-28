from __future__ import annotations

import json
import os
import sys

from . import gh, outbound, outbox, util
from .store import db as foundation
from .store import incidents as incident_store
from .store import outbox as outbox_store

LABEL = "arete-alert"
NOTIFY_SCRIPT = ("-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                 "-e", "end run")


def notify(title: str, message: str) -> None:
    if os.environ.get("ARETE_DESKTOP_NOTIFY") == "0":
        return
    msg, title = message[:200], title[:100]
    if sys.platform == "darwin":
        util.sh(["osascript", *NOTIFY_SCRIPT, "--", msg, title], timeout=10, keeps=("launch",))
    else:
        util.sh(["notify-send", "--", title, msg], timeout=10)


def ask_notice(where: str, summary: str) -> None:
    notify("arete: a run asks", f"{outbound.sanitize(where, 60)}: {outbound.sanitize(summary, 140)}")


def _draft_close(c, repo: str, number: int, comment: str, *, comment_slot: str, close_slot: str) -> str:
    if not repo:
        return left_open(f"issue #{number}", "it was opened before arete noted each alert's repo, and alerts are off now")
    obj = f"github:{repo}#{number}"
    outbox.draft(
        c,
        target="github",
        kind="comment",
        object=obj,
        body=comment,
        drafted_by="alert",
        slot=comment_slot,
        summary=f"comment on {repo}#{number}",
        on_conflict_ignore=True,
    )
    outbox.draft(
        c,
        target="github",
        kind="status",
        object=obj,
        body=json.dumps({"state": "closed"}),
        drafted_by="alert",
        slot=close_slot,
        summary=f"close {repo}#{number}",
        on_conflict_ignore=True,
    )
    return ""


def raise_alert(repo: str, key: str, title: str, status: str, dry_run: bool = False) -> str:
    status = outbound.sanitize(status)
    prev = incident_store.alert_get(key)
    if dry_run:
        return f"would {'refresh' if prev else 'open'} alert {key}: {status}"
    if not repo:
        if not prev:
            notify("arete", f"{title}: {status}")
        incident_store.alert_set(key, (prev or {}).get("issue"))
        return f"notified {key}"

    new_episode = prev is None
    moved_from: tuple[str, int, str] | None = None
    discard_old_slot = ""

    if prev is not None:
        issue_repo = opened_in(prev, repo)
        if prev.get("issue"):
            if issue_repo != repo:
                moved_from = (issue_repo, int(prev["issue"]), prev["opened"])
                new_episode = True
            else:
                try:
                    state = gh.issue_state(repo, int(prev["issue"]))
                except gh.GhError as e:
                    incident_store.alert_set(key, prev["issue"], repo)
                    return f"gh failed for {key} ({e})"
                if state == "OPEN":
                    incident_store.alert_set(key, prev["issue"], repo)
                    return f"alert {key} still open (#{prev['issue']})"
                new_episode = True
        elif issue_repo != repo:
            discard_old_slot = f"alert:{key}:{prev['opened']}"
            new_episode = True

    now_iso = util.iso()
    opened = now_iso if new_episode else prev["opened"]
    body = (
        f"Check `{key}` is failing.\n\n"
        f"Status: {status}\n\n"
        f"First seen: {opened}\n\n"
        "Arete closes this issue when the check passes again. Details: `arete status` on the Mac."
    )
    payload = json.dumps({"title": f"[arete] {title}", "body": body, "labels": [LABEL]})
    slot = f"alert:{key}:{opened}"
    left = ""

    with foundation.tx() as c:
        if moved_from is not None:
            old_repo, old_num, old_opened = moved_from
            left = _draft_close(
                c,
                old_repo,
                old_num,
                f"Arete now files its alerts in {repo}, so this alert moves there.",
                comment_slot=f"alert:{key}:{old_opened}:moved",
                close_slot=f"alert:{key}:{old_opened}:moved-closed",
            )
        elif discard_old_slot:
            old_item = outbox_store.get_by_slot(c, discard_old_slot)
            if old_item is not None and int(old_item["attempts"]) == 0:
                outbox_store.discard_item(c, int(old_item["id"]), now_iso)
        incident_store.alert_set(key, None, repo, opened=opened)
        item_id = outbox.draft(
            c,
            target="github",
            kind="issue",
            object=f"github:{repo}",
            body=payload,
            drafted_by="alert",
            slot=slot,
            summary=f"open alert {key} on {repo}",
            on_conflict_ignore=True,
        )
    return f"drafted OB-{item_id} for {key}{left}"


def clear_alert(repo: str, key: str, dry_run: bool = False) -> str | None:
    prev = incident_store.alert_get(key)
    if not prev:
        return None
    if dry_run:
        return f"would close alert {key}"
    if not prev.get("opened") and not prev.get("issue"):
        incident_store.alert_clear(key)
        return f"cleared {key}"
    now_iso = util.iso()
    left = ""
    with foundation.tx() as c:
        row = c.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        current = dict(row)
        slot = f"alert:{key}:{current['opened']}"
        item = outbox_store.get_by_slot(c, slot) if outbox_store.available(c) else None
        if item is not None:
            last = outbox_store.last_attempt(c, int(item["id"]))
            if item["state"] == "sending" or (item["state"] != "sent" and last is not None and last.get("outcome") == "unknown"):
                return f"alert {key} issue item OB-{item['id']} is still in flight; retrying next pass"
            if item["state"] in ("draft", "edited", "approved", "held", "failed") and int(item["attempts"]) == 0:
                outbox_store.discard_item(c, int(item["id"]), now_iso)
                incident_store.alert_clear(key)
                return f"cleared {key}"
        issue_num = current.get("issue")
        if issue_num is None and item is not None and item["state"] == "sent" and str(item.get("sent_ref") or "").isdigit():
            issue_num = int(item["sent_ref"])
        if issue_num:
            issue_repo = opened_in(current, repo)
            left = _draft_close(
                c,
                issue_repo,
                int(issue_num),
                f"Recovered at {now_iso}.",
                comment_slot=f"alert:{key}:{current['opened']}:recovered",
                close_slot=f"alert:{key}:{current['opened']}:closed",
            )
        incident_store.alert_clear(key)
    return f"cleared {key}{left}"


def opened_in(alert: dict, alert_repo: str) -> str:
    return alert.get("repo") or alert_repo


def issue_link(alert: dict, alert_repo: str) -> str:
    repo = opened_in(alert, alert_repo)
    return gh.issue_url(repo, alert["issue"]) if repo and alert.get("issue") else ""


def left_open(issue: str, why: str) -> str:
    notify("arete", f"Close {issue} by hand: {why}.")
    return f"; {why}, so close {issue} by hand"
