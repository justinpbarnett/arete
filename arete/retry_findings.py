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
