from __future__ import annotations

import re

ACTIONS = {
    "preserve_draft_on_declined_save": "Preserve the draft after a declined save.",
    "preserve_draft_on_failed_save": "Preserve the draft when saving fails.",
    "preserve_draft_on_retry": "Preserve the draft when retrying.",
    "preserve_selection": "Restore the previously selected tab or item.",
    "show_field_error": "Show the failure beside the field that needs correction.",
    "explain_retry": "Explain when a retry is possible and how to start it.",
    "avoid_partial_write": "Keep existing data intact if any part of the write fails.",
    "avoid_duplicates": "Make repeated attempts produce a single result.",
    "preserve_access_controls": "Enforce the existing access controls on the affected operation.",
}
ACTION_SCHEMA = {
    "type": "string",
    "enum": list(ACTIONS),
    "description": "Optional action for an unattended retry. Choose it only when it describes the finding. "
                   + " ".join(f"{key} means {action}" for key, action in ACTIONS.items()),
}
LEGACY_ACTIONS = tuple((re.compile(pattern, re.IGNORECASE), key) for pattern, key in (
    (r"(?:saving the draft fails:\s*)?(?:keep|preserve) the draft after a declined save",
     "preserve_draft_on_declined_save"),
    (r"(?:keep|preserve) the draft (?:on failed save|after a failed save|when saving fails)",
     "preserve_draft_on_failed_save"),
    (r"retry(?:ing)? preserves the draft|preserve the draft (?:on|when) retry(?:ing)?", "preserve_draft_on_retry"),
    (r"(?:keep|restore) the (?:previously )?selected (?:tab|item)", "preserve_selection"),
    (r"show the (?:failure|error) beside the field(?: that needs correction)?", "show_field_error"),
    (r"explain the retry policy", "explain_retry"),
    (r"save the draft atomically|avoid partial writes", "avoid_partial_write"),
))


def action_key(finding: dict) -> str:
    key = finding.get("retry_action")
    return key if isinstance(key, str) and key in ACTIONS else ""


def action(finding: dict, field: str) -> str:
    if key := action_key(finding):
        return ACTIONS[key]
    prose = finding.get(field)
    if isinstance(prose, str):
        for pattern, key in LEGACY_ACTIONS:
            if pattern.fullmatch(prose.strip().removesuffix(".")):
                return ACTIONS[key]
    return ""
