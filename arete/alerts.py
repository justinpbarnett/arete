from __future__ import annotations

import os
import sys

from . import gh, outbound, util
from .store import incidents as incident_store

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
    left = ""
    try:
        if prev and prev.get("issue"):
            if (issue_repo := opened_in(prev, repo)) != repo:
                left = close_issue(issue_repo, prev["issue"], f"Arete now files its alerts in {repo}, so this alert moves there.")
                incident_store.alert_set(key, None, repo)
                prev = incident_store.alert_get(key)
            elif gh.issue_state(repo, prev["issue"]) == "OPEN":
                incident_store.alert_set(key, prev["issue"], repo)
                return f"alert {key} still open (#{prev['issue']})"
        pages = gh.gh_json(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=100",
                            "--paginate", "--slurp"])
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise gh.GhUnsure("GitHub alert lookup returned no list")
        for page in pages:
            for row in page:
                if f"Check `{key}` is failing." in (row.get("body") or ""):
                    incident_store.alert_set(key, row["number"], repo)
                    return f"alert {key} still open (#{row['number']})"
        gh.ensure_label(repo, LABEL, "d93f0b", "Opened by arete heartbeat")
        body = (
            f"Check `{key}` is failing.\n\n"
            f"Status: {status}\n\n"
            f"First seen: {(prev or {}).get('opened') or util.iso()}\n\n"
            "Arete closes this issue when the check passes again. Details: `arete status` on the Mac."
        )
        num = gh.issue_create(repo, f"[arete] {title}", body, [LABEL])
        incident_store.alert_set(key, num, repo)
        return f"opened #{num} for {key}{left}"
    except (gh.GhError, ValueError, TypeError, KeyError, AttributeError) as e:
        if not prev:
            notify("arete", f"{title}: {status}")
        incident_store.alert_set(key, (prev or {}).get("issue"))
        return f"gh failed for {key} ({e}){'' if prev else '; notified locally'}{left}"


def clear_alert(repo: str, key: str, dry_run: bool = False) -> str | None:
    prev = incident_store.alert_get(key)
    if not prev:
        return None
    if dry_run:
        return f"would close alert {key}"
    left = ""
    if prev.get("issue"):
        issue_repo = opened_in(prev, repo)
        try:
            left = close_issue(issue_repo, prev["issue"], f"Recovered at {util.iso()}.")
        except gh.GhError as e:
            return f"gh failed closing {issue_repo}#{prev['issue']} for {key} ({e}); retrying next pass"
    incident_store.alert_clear(key)
    return f"cleared {key}{left}"


def opened_in(alert: dict, alert_repo: str) -> str:
    return alert.get("repo") or alert_repo


def issue_link(alert: dict, alert_repo: str) -> str:
    repo = opened_in(alert, alert_repo)
    return gh.issue_url(repo, alert["issue"]) if repo and alert.get("issue") else ""


def close_issue(repo: str, number: int, comment: str) -> str:
    if not repo:
        return left_open(f"issue #{number}", "it was opened before arete noted each alert's repo, and alerts are off now")
    try:
        gh.issue_close(repo, number, comment)
    except gh.GhError as e:
        if "Could not resolve to an issue" in str(e):
            return ""
        if "Could not resolve to a Repository" not in str(e):
            raise
        return left_open(gh.issue_url(repo, number), f"arete cannot see {repo} any more")
    return ""


def left_open(issue: str, why: str) -> str:
    notify("arete", f"Close {issue} by hand: {why}.")
    return f"; {why}, so close {issue} by hand"
