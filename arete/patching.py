from __future__ import annotations

import re

from . import config, tracker, util

SENSITIVE_WORK = re.compile(
    r"\b(auth(?:entication|orization)?|credentials?|secrets?|passwords?|tokens?|jwt|oauth|"
    r"log(?:in|out)|sign[ -]?(?:in|out)|sessions?|permissions?|privileges?|security|payments?|billing|"
    r"migrations?|restore|backups?|encryption|keychain|data loss|schema|idempotency|sandbox|confinement|"
    r"storage|database|persistence|destructive|data retention)\b",
    re.IGNORECASE)
SCHEMA = {
    "type": "object",
    "properties": {
        "needs_feature": {"type": "boolean"},
        "reason": {"type": "string"},
        "plan": {"type": "string"},
        "verify": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["needs_feature", "reason", "plan", "verify", "summary"],
}


def requested(layer: config.RepoLayer, ticket: tracker.Ticket, *, automatic: bool = False) -> bool:
    return bool(set(layer.patch.labels) & set(ticket.labels)) or (
        automatic and ticket.kind == "fix" and "feature" not in ticket.labels)


def specification_refusal(ticket: tracker.Ticket) -> str:
    return "the ticket describes sensitive work" if SENSITIVE_WORK.search(
        f"{ticket.title}\n{ticket.body}\n{ticket.comments}") else ""


def change_refusal(policy: config.PatchPolicy, files: list[str], lines: int) -> str:
    if lines > policy.max_lines or len(files) > policy.max_files:
        return f"the patch exceeds {policy.max_files} files or {policy.max_lines} changed lines"
    if sensitive := sensitive_files(policy, files):
        return "sensitive paths require the feature flow: " + ", ".join(sensitive)
    return ""


def sensitive_files(policy: config.PatchPolicy, files: list[str]) -> list[str]:
    patterns = [pattern.lower() for pattern in policy.sensitive]
    return [name for name in files if util.matches_any(name.lower(), patterns)]


def allowed_gap(layer: config.RepoLayer, ticket: tracker.Ticket, verified) -> bool:
    if not layer.verify or not layer.verify.allow_patch_gaps or verified.verdict != "blocked" or not verified.finished:
        return False
    if specification_refusal(ticket) or not verified.failed_checks:
        return False
    reason = "\n".join((verified.blocked_by, verified.summary,
                        *(str(check.get(key) or "") for check in verified.failed_checks for key in ("what", "how", "saw"))))
    if re.search(r"guard|jev|credential|secret|confine|sandbox|permission|eperm|operation not permitted|denied|"
                 r"auth|login|log in|sign in|token|\b40[13]\b|retained|unresolved|probes", reason, re.IGNORECASE):
        return False
    return all(check.get("cause") == "environment" for check in verified.failed_checks)
