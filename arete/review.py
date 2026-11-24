from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field

from . import sandbox, util
from .vault import git_unquoted

BLOCKER_CATEGORIES = ("issue", "regression", "security", "data_loss")
UNWAIVABLE = ("security", "data_loss")
EARLIER_STATUSES = ("fixed", "open", "waived")
NOTE_SEVERITIES = ("major", "minor")
REPAIR_METHODS = ("check", "review", "in_use")
REPAIR_FIELDS = ("acceptance", "verification", "check", "test_class", "verification_reason")
REPAIR_SELECTOR_SCHEMA = {
    "type": "object",
    "properties": {"selectors": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "verification": {"type": "string", "enum": list(REPAIR_METHODS)},
                       "check": {"type": "string"}, "test_class": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["id", "verification"],
    }}},
    "required": ["selectors"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {"type": "string"},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "problem": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "category": {"type": "string", "enum": list(BLOCKER_CATEGORIES)},
                    "acceptance": {"type": "string"},
                    "verification": {
                        "type": "string",
                        "enum": list(REPAIR_METHODS),
                        "description": "Defaults to check for blockers. Use review for inspection or in_use for a "
                                       "live observation.",
                    },
                    "check": {"type": "string"},
                    "test_class": {"type": "string"},
                    "verification_reason": {"type": "string"},
                },
                "required": ["problem", "severity"],
            },
        },
        "earlier": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": list(EARLIER_STATUSES)},
                    "why": {"type": "string"},
                },
                "required": ["id", "status", "why"],
            },
        },
    },
    "required": ["summary", "problems"],
}

_LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "description": "Repository-relative path."},
        "line": {"type": "integer", "minimum": 1, "description": "Changed line. Required for files with text hunks."},
        "side": {"type": "string", "enum": ["before", "after"]},
    },
    "required": ["file"],
}
_PROBLEMS_SCHEMA = REVIEW_SCHEMA["properties"]["problems"]
_PROBLEM_SCHEMA = _PROBLEMS_SCHEMA["items"]
CATCH_UP_REVIEW_SCHEMA = {
    **REVIEW_SCHEMA,
    "properties": {
        "summary": REVIEW_SCHEMA["properties"]["summary"],
        "changes": REVIEW_SCHEMA["properties"]["changes"],
        "problems": {
            **_PROBLEMS_SCHEMA,
            "items": {
                **_PROBLEM_SCHEMA,
                "properties": {
                    **_PROBLEM_SCHEMA["properties"],
                    "attribution": {
                        **_LOCATION_SCHEMA,
                        "description": "Cite a changed line, or a changed file in the base or resolution part with no text hunk.",
                        "properties": {
                            **_LOCATION_SCHEMA["properties"],
                            "part": {"type": "string", "enum": ["base", "resolution", "approved"]},
                            "base_change": _LOCATION_SCHEMA,
                            "dependency": {"type": "string"},
                        },
                        "required": [*_LOCATION_SCHEMA["required"], "part"],
                    },
                },
            },
        },
    },
}
_HUNK_START = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", re.MULTILINE)
_PATCH_SECTION = re.compile(r"^diff --git ", re.MULTILINE)
_SAME_PATH = re.compile(r'("?)a/(.+)\1 \1b/\2\1')
_RENAME_PATHS = re.compile(r"^rename from (.+)\nrename to (.+)$", re.MULTILINE)
_PATCH_BODY = re.compile(r"^(?:@@ |GIT binary patch$)", re.MULTILINE)


@dataclass
class ChangedLocations:
    lines: set[tuple[str, str, int]]
    files_without_hunks: set[str]


@dataclass(frozen=True)
class ReviewPatchSection:
    paths: tuple[str, ...]
    header: str
    metadata: str
    text: str

    def without_binary_payload(self) -> str:
        if "\0" in self.text or "\nGIT binary patch\n" in self.text:
            return f"diff --git {self.header}\n{self.metadata}Binary files differ\n"
        return f"diff --git {self.text}"


@dataclass
class Review:
    verdict: str | None = None
    summary: str = ""
    changes: str = ""
    problems: list[dict] = field(default_factory=list)
    earlier: list[dict] | None = None
    waived: list[dict] = field(default_factory=list)
    trees: dict = field(default_factory=dict)
    failed: bool = False

    def __post_init__(self):
        self.problems = review_problems(self.problems)
        self._blocking = ([p for p in self.problems if not is_note(self, p)]
                          + [e for e in self.earlier or [] if e.get("status") == "open"])
        self._notes = [p for p in self.problems if is_note(self, p)]
        self._keys = tuple(k for k in self.__dataclass_fields__ if k != "earlier" or self.earlier is not None)
        self._extra = {}

    @classmethod
    def stored(cls, data: dict) -> Review:
        found = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        found._keys = tuple(data)
        found._extra = {k: v for k, v in data.items() if k not in cls.__dataclass_fields__}
        return found

    @classmethod
    def judged(cls, said: dict, earlier: list[dict], waived: list[dict], trees: dict) -> Review:
        answered = settled(earlier, said.get("earlier"))
        problems = [{k: v for k, v in p.items() if k != "finding"} for p in review_problems(said.get("problems"))]
        for problem in problems:
            if problem["severity"] not in NOTE_SEVERITIES:
                problem.setdefault("verification", "check")
        found = cls.stored({**said, "problems": problems, "earlier": answered, "trees": trees,
                            "waived": waived + [e for e in answered if e["status"] == "waived"]})
        found.verdict = "request_changes" if found.blocking() else "approve"
        if "verdict" not in found._keys:
            found._keys += ("verdict",)
        return found

    @classmethod
    def failed_step(cls, earlier: list[dict], waived: list[dict], trees: dict) -> Review:
        return cls.stored({"verdict": "request_changes", "summary": "review step failed", "problems": [], "failed": True,
                           "earlier": [{**p, "id": blocker_id(n), "status": "open", "why": "the review step failed"}
                                       for n, p in enumerate(earlier, 1)],
                           "waived": waived, "trees": trees})

    def asdict(self) -> dict:
        data = {**asdict(self), **self._extra}
        return {k: data[k] for k in self._keys if k != "earlier" or self.earlier is not None}

    def blocking(self) -> list[dict]:
        return self._blocking

    def notes(self) -> list[dict]:
        return self._notes

    def approved(self) -> bool:
        return self.verdict == "approve"

    def findings(self) -> str:
        return "" if self.failed else numbered(self.blocking())

    def fix_notes(self) -> str:
        return "\n".join(f"- [{p['severity']}] {p['file']}: {p['problem']}" for p in self.notes()) or "None."

    def markdown(self) -> str:
        return f"## Review\n\n**{self.display_verdict}**: {self.summary}\n" + self.lists()

    @property
    def display_verdict(self) -> str | None:
        return self.verdict if "verdict" in self._keys else "none"

    def lists(self) -> str:
        notes = ([f"[{p['severity']}] {p['file']}: {problem_text(p)}" for p in self.notes()]
                 + [f"[waived {p.get('category') or 'blocker'}] {p.get('file', '')}: {p['problem']} ({p['why']})"
                    for p in self.waived or []])
        return ("".join(f"\n- [blocker, {p.get('category') or 'unstated'}] {p.get('file', '')}: {problem_text(p)}"
                        for p in self.blocking())
                + ("\n\n### Review notes (not blocking)\n" + "".join(f"\n- {n}" for n in notes) if notes else ""))

    def report(self) -> dict:
        return {"verdict": str(self.verdict or ""), "summary": str(self.summary or ""),
                "unresolved": self.verdict == "request_changes",
                "problems": reported(self.blocking()), "notes": reported(self.notes()),
                **({"trees": self.trees} if self.trees and self.blocking() else {})}

    def identify(self, receipt: str) -> Review:
        for n, p in enumerate(self._blocking, 1):
            p.setdefault("finding", f"{receipt}:{blocker_id(n)}")
        return self

    def repair_requirements(self) -> dict[str, dict]:
        requirements = {}
        for p in self._blocking:
            method = p.get("verification") or "check"
            acceptance = p.get("acceptance") or f"Exercise the reported path and observe it no longer fails: {p['problem']}"
            reason = p.get("verification_reason", "")
            if not reason and method in ("review", "in_use"):
                reason = "No alternate verification reason was recorded."
            requirements[p["finding"]] = {"file": p.get("file", ""), "problem": p["problem"], "acceptance": acceptance,
                                           "verification": method, "check": p.get("check", ""),
                                           "test_class": p.get("test_class", ""), "reason": reason}
        return requirements


def settled(earlier: list[dict], answers) -> list[dict]:
    said = ({str(a.get("id") or "").strip().upper(): a for a in answers if isinstance(a, dict)}
            if isinstance(answers, list) else {})
    out = []
    for n, p in enumerate(earlier, 1):
        answer = said.get(blocker_id(n), {})
        status = answer.get("status") if answer.get("status") in EARLIER_STATUSES else "open"
        if status == "waived" and (p.get("category") or "") in (*UNWAIVABLE, ""):
            status = "open"
        out.append({**p, "id": blocker_id(n), "status": status, "why": str(answer.get("why") or "")})
    return out


def stored_before_blockers(review: Review) -> bool:
    return review.earlier is None and review.verdict == "request_changes"


def is_note(review: Review, p: dict) -> bool:
    return p["severity"] in NOTE_SEVERITIES and not stored_before_blockers(review)


def blocker_id(n: int) -> str:
    return f"B{n}"


def numbered(listed: list[dict]) -> str:
    return "\n".join(f"- {blocker_id(n)} [{p.get('category') or 'unstated'}] {p.get('file', '')}: {problem_text(p)}"
                     for n, p in enumerate(listed, 1))


def review_problems(listed) -> list[dict]:
    if not isinstance(listed, list):
        return []
    return [{"file": p.get("file") or "", "problem": p.get("problem") or "",
             "severity": str(p.get("severity") or "unrated").strip().lower(),
             "category": str(p.get("category") or "").strip().lower()
             if str(p.get("category") or "").strip().lower() in BLOCKER_CATEGORIES else "",
             **repair_fields(p), **{key: p[key] for key in ("cause", "catch_up_note") if p.get(key)}}
            for p in listed if isinstance(p, dict)]


def repair_fields(problem: dict, *, identified: bool = True) -> dict:
    keys = (*REPAIR_FIELDS, "finding") if identified else REPAIR_FIELDS
    return {key: problem[key].strip() for key in keys if isinstance(problem.get(key), str) and problem[key].strip()}


def problem_text(problem: dict) -> str:
    text = problem["problem"]
    if problem.get("cause"):
        return f"{text} ({problem['cause']})"
    return f"{text} (found on catch-up)" if problem.get("catch_up_note") else text


def section_paths(header: str, metadata: str) -> tuple[str, ...]:
    if rename := _RENAME_PATHS.search(metadata):
        old, new = rename.groups()
        return git_unquoted(old), git_unquoted(new)
    if match := _SAME_PATH.fullmatch(header):
        quote, path = match.groups()
        return (git_unquoted(f"{quote}{path}{quote}"),)
    return ()


def review_patch_sections(patch: str) -> Iterator[ReviewPatchSection]:
    for section in _PATCH_SECTION.split(patch)[1:]:
        header, _, body = section.partition("\n")
        metadata = _PATCH_BODY.split(body, maxsplit=1)[0]
        yield ReviewPatchSection(section_paths(header, metadata), header, metadata, section)


def permitted_review_patches(patches: dict[str, str], deny: list[str], prefix: str) -> dict[str, str]:
    sections = {part: list(review_patch_sections(patch)) for part, patch in patches.items()}
    paths = [section.paths for group in sections.values() for section in group]
    protected = {path for names in paths for path in names
                 if sandbox.secret_path(prefix + path) or util.matches_any(path, deny)
                 or util.matches_any(prefix + path, deny)}
    renames = [set(names) for names in paths if len(names) == 2]
    while aliases := {path for names in renames if names & protected for path in names} - protected:
        protected.update(aliases)
    return {part: "".join(section.without_binary_payload() for section in group
                          if section.paths and protected.isdisjoint(section.paths))
            for part, group in sections.items()}


def changed_lines(patch: str) -> set[tuple[str, str, int]]:
    locations = set()
    in_hunk = False
    old_file = new_file = ""
    old_line = new_line = 0
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
            old_file = new_file = ""
        elif match := _HUNK_START.match(line):
            in_hunk = True
            old_line, new_line = map(int, match.groups())
        elif not in_hunk:
            if line.startswith("--- "):
                old_file = git_unquoted(line[4:].rstrip("\t")).removeprefix("a/")
            elif line.startswith("+++ "):
                new_file = git_unquoted(line[4:].rstrip("\t")).removeprefix("b/")
        elif line.startswith("-"):
            locations.add((old_file, "before", old_line))
            old_line += 1
        elif line.startswith("+"):
            locations.add((new_file, "after", new_line))
            new_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1
    return locations


def changed_locations(patch: str, *, text_only: bool) -> ChangedLocations:
    lines = changed_lines(patch)
    if text_only:
        return ChangedLocations(lines, set())
    touched, with_hunks = set(), set()
    for section in review_patch_sections(patch):
        touched.update(section.paths)
        if _HUNK_START.search(section.text):
            with_hunks.update(section.paths)
    return ChangedLocations(lines, touched - with_hunks)


def changed_location(given, changes: ChangedLocations) -> str:
    if not isinstance(given, dict):
        return ""
    file = given.get("file")
    if not isinstance(file, str):
        return ""
    if file in changes.files_without_hunks:
        return file
    side, line = given.get("side", "after"), given.get("line")
    if isinstance(side, str) and type(line) is int and (file, side, line) in changes.lines:
        return f"{file}:{line} ({side})"
    return ""


def catch_up_cause(attribution, changes: dict[str, ChangedLocations]) -> str:
    if not isinstance(attribution, dict):
        return ""
    part = attribution.get("part")
    if part not in ("base", "resolution", "approved") or part not in changes:
        return ""
    if not (location := changed_location(attribution, changes[part])):
        return ""
    if part in ("base", "resolution"):
        return f"{part} change at {location}"
    if "base" not in changes:
        return ""
    cause = changed_location(attribution.get("base_change"), changes["base"])
    dependency = attribution.get("dependency")
    if cause and isinstance(dependency, str) and dependency.strip():
        return f"approved change at {location} depends on base change at {cause}. {dependency.strip()}"
    return ""


def catch_up_problems(problems: list, patches: dict[str, str]) -> list[dict]:
    changes = {part: changed_locations(patch, text_only=part == "approved") for part, patch in patches.items()}
    findings = []
    for raw in problems:
        if not isinstance(raw, dict):
            continue
        problem = {key: raw[key] for key in ("file", "problem", "severity", "category") if key in raw}
        problem.update(repair_fields(raw, identified=False))
        if str(problem.get("severity") or "").strip().lower() not in NOTE_SEVERITIES:
            if cause := catch_up_cause(raw.get("attribution"), changes):
                problem["cause"] = cause
            else:
                problem.update(severity="major", category="", catch_up_note=True)
        findings.append(problem)
    return findings


def reported(listed: list[dict]) -> list[dict]:
    return [{"file": p["file"], "text": problem_text(p), "severity": p["severity"], "category": p.get("category") or "",
             **repair_fields(p)} for p in listed]
