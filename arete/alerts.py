from __future__ import annotations

import sys

from . import gh, outbound, state, util

LABEL = "arete-alert"
NOTIFY_SCRIPT = ("-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                 "-e", "end run")


def notify(title: str, message: str) -> None:
    msg, title = message[:200], title[:100]
    if sys.platform == "darwin":
        util.sh(["osascript", *NOTIFY_SCRIPT, "--", msg, title], timeout=10)
    else:
        util.sh(["notify-send", "--", title, msg], timeout=10)


def raise_alert(repo: str, key: str, title: str, status: str, dry_run: bool = False) -> str:
    status = outbound.sanitize(status)
    prev = state.alert_get(key)
    if dry_run:
        return f"would {'refresh' if prev else 'open'} alert {key}: {status}"
    if not repo:
        if not prev:
            notify("arete", f"{title}: {status}")
        state.alert_set(key, (prev or {}).get("issue"))
        return f"notified {key}"
    try:
        if prev and prev.get("issue") and prev.get("repo") in ("", repo) and gh.issue_state(repo, prev["issue"]) == "OPEN":
            state.alert_set(key, prev["issue"], repo)
            return f"alert {key} still open (#{prev['issue']})"
        gh.ensure_label(repo, LABEL, "d93f0b", "Opened by arete heartbeat")
        body = (
            f"Check `{key}` is failing.\n\n"
            f"Status: {status}\n\n"
            f"First seen: {(prev or {}).get('opened') or util.iso()}\n\n"
            "Arete closes this issue when the check passes again. Details: `arete status` on the Mac."
        )
        num = gh.issue_create(repo, f"[arete] {title}", body, [LABEL])
        state.alert_set(key, num, repo)
        return f"opened #{num} for {key}"
    except gh.GhError as e:
        if not prev:
            notify("arete", f"{title}: {status}")
        state.alert_set(key, (prev or {}).get("issue"))
        return f"gh failed for {key} ({e}); notified locally"


def clear_alert(repo: str, key: str, dry_run: bool = False) -> str | None:
    prev = state.alert_get(key)
    if not prev:
        return None
    if dry_run:
        return f"would close alert {key}"
    if repo and prev.get("issue") and prev.get("repo") in ("", repo):
        try:
            gh.issue_close(repo, prev["issue"], f"Recovered at {util.iso()}.")
        except gh.GhError as e:
            if "Could not resolve to an issue" not in str(e):
                return f"gh failed closing #{prev['issue']} for {key} ({e}); retrying next pass"
    state.alert_clear(key)
    return f"cleared {key}"
