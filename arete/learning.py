from __future__ import annotations

import re
from typing import NamedTuple

MAX_LINES = 80
MAX_ADDED = 3


class Rewrite(NamedTuple):
    rewrite: str
    report: str
    added: int
    removed: int


def lesson_key(text: str) -> str:
    return re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", text.strip())


def lessons(text: str) -> set[str]:
    return {lesson_key(line) for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")}


def reported_lesson(value: str, available: set[str], field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip().splitlines()) != 1:
        raise ValueError(f"{field} must name nonempty, single-line lessons")
    key = lesson_key(value)
    if key in available:
        return key
    matches = {line for line in available if key == line.partition(": ")[2]}
    if len(matches) != 1:
        raise ValueError(f"{field} must identify exactly one lesson in the {'new' if field == 'added' else 'current'} file")
    return matches.pop()


def parse(found: dict, current: str) -> Rewrite:
    rewrite = found.get("expertise")
    if not isinstance(rewrite, str) or not rewrite.strip():
        raise ValueError("no expertise file came back")
    rewrite = rewrite.strip()
    if rewrite.splitlines()[0] != "# Expertise":
        raise ValueError("the expertise file must start with # Expertise")
    if (count := len(rewrite.splitlines())) > MAX_LINES:
        raise ValueError(f"the rewrite has {count} lines, over the {MAX_LINES} line cap")
    added, removed = found.get("added"), found.get("removed")
    if not isinstance(added, list) or len(added) > MAX_ADDED:
        raise ValueError(f"added must list at most {MAX_ADDED} lessons")
    if not isinstance(removed, list):
        raise TypeError("removed must list lessons and their reasons")
    before, after = lessons(current), lessons(rewrite)
    additions = {reported_lesson(value, after, "added") for value in added}
    removals = set()
    for entry in removed:
        if not isinstance(entry, dict) or not isinstance(entry.get("why"), str) or not entry["why"].strip():
            raise ValueError("each removed lesson needs a nonempty reason")
        removals.add(reported_lesson(entry.get("lesson"), before, "removed"))
    new, dropped = after - before, before - after
    if new - additions:
        raise ValueError("the rewrite contains new or changed lessons not declared in added")
    if dropped != removals:
        raise ValueError("every dropped or changed lesson must appear in removed, and a removed lesson must be absent")
    if not new and not dropped:
        return Rewrite(current, "", 0, 0)
    report = "\n".join([f"+ {value}" for value in sorted(new)] +
                       [f"- {entry['lesson']} ({entry['why'].strip()})" for entry in removed])
    return Rewrite(rewrite, report, len(new), len(dropped))
