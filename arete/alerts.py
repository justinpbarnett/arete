from __future__ import annotations

import re

from . import gh, state, util

LABEL = "arete-alert"
_SECRETISH = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|[A-Za-z0-9+/]{40,}|/Users/[^/\s]+)")


def sanitize(text: str, n: int = 200) -> str:
    text = util.first_line(text, n)
    return _SECRETISH.sub("[redacted]", text)


def notify(title: str, message: str) -> None:
    msg = message.replace('"', "'")[:200]
    util.sh(["osascript", "-e", f'display notification "{msg}" with title "{title}"'], timeout=10)


def raise_alert(repo: str, key: str, title: str, status: str, dry_run: bool = False) -> str:
    status = sanitize(status)
    prev = state.alert_get(key)
    if dry_run:
        return f"would {'refresh' if prev else 'open'} alert {key}: {status}"
    if not repo:
        if not prev:
            notify("arete", f"{title}: {status}")
        state.alert_set(key, None)
        return f"notified {key}"
    try:
        if prev and prev.get("issue") and gh.issue_state(repo, prev["issue"]) == "OPEN":
            state.alert_set(key, prev["issue"])
            return f"alert {key} still open (#{prev['issue']})"
        gh.ensure_label(repo, LABEL, "d93f0b", "Opened by arete heartbeat")
        body = (
            f"Check `{key}` is failing.\n\n"
            f"Status: {status}\n\n"
            f"First seen: {util.iso()}\n\n"
            "Arete closes this issue when the check passes again. Details: `arete status` on the Mac."
        )
        num = gh.issue_create(repo, f"[arete] {title}", body, [LABEL])
        state.alert_set(key, num)
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
    if repo and prev.get("issue"):
        try:
            gh.issue_close(repo, prev["issue"], f"Recovered at {util.iso()}.")
        except gh.GhError:
            pass
    state.alert_clear(key)
    return f"cleared {key}"
