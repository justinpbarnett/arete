from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import retry_findings

BLOCKER_CATEGORIES = ("issue", "regression", "security", "data_loss")
UNWAIVABLE = ("security", "data_loss")
EARLIER_STATUSES = ("fixed", "open", "waived")
NOTE_SEVERITIES = ("major", "minor")

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
                    "retry_action": retry_findings.ACTION_SCHEMA,
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
        found = cls.stored({**said, "problems": said.get("problems"), "earlier": answered, "trees": trees,
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
        notes = ([f"[{p['severity']}] {p['file']}: {p['problem']}" for p in self.notes()]
                 + [f"[waived {p.get('category') or 'blocker'}] {p.get('file', '')}: {p['problem']} ({p['why']})"
                    for p in self.waived or []])
        return ("".join(f"\n- [blocker, {p.get('category') or 'unstated'}] {p.get('file', '')}: {p['problem']}"
                        for p in self.blocking())
                + ("\n\n### Review notes (not blocking)\n" + "".join(f"\n- {n}" for n in notes) if notes else ""))

    def report(self) -> dict:
        return {"verdict": str(self.verdict or ""), "summary": str(self.summary or ""),
                "unresolved": self.verdict == "request_changes",
                "problems": reported(self.blocking()), "notes": reported(self.notes())}


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
    return "\n".join(f"- {blocker_id(n)} [{p.get('category') or 'unstated'}] {p.get('file', '')}: {p['problem']}"
                     for n, p in enumerate(listed, 1))


def review_problems(listed) -> list[dict]:
    if not isinstance(listed, list):
        return []
    return [{"file": p.get("file") or "", "problem": p.get("problem") or "",
             "severity": str(p.get("severity") or "unrated").strip().lower(),
             "category": str(p.get("category") or "").strip().lower()
             if str(p.get("category") or "").strip().lower() in BLOCKER_CATEGORIES else "",
             **action_field(p)}
            for p in listed if isinstance(p, dict)]


def action_field(problem: dict) -> dict:
    key = retry_findings.action_key(problem)
    return {"retry_action": key} if key else {}


def reported(listed: list[dict]) -> list[dict]:
    return [{"file": p["file"], "text": p["problem"], "severity": p["severity"], "category": p.get("category") or "",
             **action_field(p)} for p in listed]
