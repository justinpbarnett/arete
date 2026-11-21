from __future__ import annotations

import re

from . import journeys, outbound

REFERENCE = re.compile(rf"(?:criterion|journey):{journeys.NAME.pattern}")
TICKET_CRITERION = re.compile(rf"- \[(criterion:{journeys.NAME.pattern})\] (.+)")
REFERENCE_SCHEMA = {
    "type": "string",
    "description": "Optional exact criterion: or journey: reference from the acceptance criteria. "
                   "Supply it only if the finding concerns that criterion.",
}

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


def reference(value) -> str:
    return value if isinstance(value, str) and REFERENCE.fullmatch(value) else ""


def criteria(body: str, feature_map: dict[str, journeys.Journey], title: str) -> dict[str, str]:
    listed = {}
    visible = re.sub(r"<!--.*?(?:-->|\Z)", lambda block: re.sub(r"[^\r\n]", " ", block[0]), body, flags=re.DOTALL)
    lines = visible.splitlines()
    fenced_until = -1
    raw = None
    for index, line in enumerate(lines):
        if index <= fenced_until:
            continue
        if raw:
            if raw.search(line):
                raw = None
            continue
        if (end := outbound.fence_end(lines, index)) is not None:
            fenced_until = end
            continue
        if started := outbound.RAW_START.match(line):
            raw_end = outbound.RAW_END[started.lastgroup]
            raw = None if raw_end.search(line) else raw_end
            continue
        if match := TICKET_CRITERION.fullmatch(line):
            key, criterion = match.groups()
            listed[key] = "" if key in listed else criterion.strip()
    for name in journeys.requested(feature_map, title, body):
        if (journey := feature_map.get(name)) and journey.tracker_criterion:
            listed[f"journey:{name}"] = journey.tracker_criterion
    return listed


def action(finding: dict, field: str) -> str:
    if key := action_key(finding):
        return ACTIONS[key]
    prose = finding.get(field)
    if isinstance(prose, str):
        for pattern, key in LEGACY_ACTIONS:
            if pattern.fullmatch(prose.strip().removesuffix(".")):
                return ACTIONS[key]
    return ""
