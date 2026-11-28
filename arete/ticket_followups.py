"""Crash-resilient completed-ticket follow-up engine, mixed-request splitter, and duplicate-bug reconciler."""
from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from . import (
    config,
    gh,
    idempotency,
    jira,
    outbound,
    ticket_feedback,
    ticket_publication,
    tracker,
    util,
    vault,
)
from .store import db
from .store import feedback as feedback_store
from .store import runs as run_store
from .store import specs as spec_store
from .store import tasks as tasks_store

FollowUpKind = Literal["bug", "change_request"]
FollowUpAction = Literal["create", "append"]

REPLY_MARKER_PREFIX = "arete-followup-reply-"
EVIDENCE_MARKER_PREFIX = "arete-followup-evidence-"
LOCAL_MARKER_PREFIX = "arete-followup-task-"

COUNT_WORDS = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can", "could",
    "did", "do", "does", "done", "for", "from", "get", "gets", "got", "had", "has", "have",
    "here", "how", "i", "if", "in", "into", "is", "it", "its", "just", "let", "like", "me",
    "more", "my", "need", "needs", "no", "not", "now", "of", "on", "one", "only", "or", "our",
    "out", "please", "same", "see", "seeing", "should", "so", "some", "still", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "those", "to", "too",
    "up", "us", "use", "very", "want", "was", "we", "were", "what", "when", "where", "which",
    "while", "who", "will", "with", "would", "you", "your",
    "also", "again", "another", "bug", "issue", "problem", "request", "ticket", "report",
    "reported", "hitting", "reproduced", "evidence", "detail", "details", "note", "notes",
    "chrome", "safari", "firefox", "edge", "browser", "desktop", "mobile", "macos", "windows",
})

LEMMA_MAP = {
    "loses": "lose", "losing": "lose", "lost": "lose",
    "drops": "drop", "dropping": "drop", "dropped": "drop",
    "wipes": "wipe", "wiping": "wipe", "wiped": "wipe",
    "clears": "clear", "clearing": "clear", "cleared": "clear",
    "resets": "reset", "resetting": "reset",
    "reverts": "revert", "reverting": "revert", "reverted": "revert",
    "saves": "save", "saving": "save", "saved": "save",
    "crashes": "crash", "crashing": "crash", "crashed": "crash",
    "fails": "fail", "failing": "fail", "failed": "fail", "failure": "fail",
    "breaks": "break", "breaking": "break", "broke": "break", "broken": "break",
    "filters": "filter", "filtering": "filter", "filtered": "filter",
    "exports": "export", "exporting": "export", "exported": "export",
    "reloads": "reload", "reloading": "reload", "reloaded": "reload",
    "clicks": "click", "clicking": "click", "clicked": "click",
    "selects": "select", "selecting": "select", "selected": "select", "selection": "select",
    "values": "value", "fields": "field", "records": "record", "items": "item",
    "buttons": "button", "pages": "page", "rows": "row", "columns": "column",
    "totals": "total", "errors": "error", "exceptions": "exception", "settings": "setting",
    "timezones": "timezone", "dropdowns": "dropdown", "users": "user", "files": "file",
}

BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$")
REPRO_HEADER_RE = re.compile(
    r"^\s*(?:steps\s+to\s+reproduce|repro(?:\s+steps)?|reproduction|expected(?:\s+behavior)?|"
    r"actual(?:\s+behavior)?|observed|details|evidence|logs?|environment)\s*:?\s*$",
    re.I,
)
EVIDENCE_LEAD_RE = re.compile(
    r"^\s*(?:steps\s+to\s+reproduce|repro(?:\s+steps)?|expected|actual|observed|"
    r"for\s+example|e\.g\.|i\s+tested|tested\s+(?:in|on)|happens\s+(?:in|when|after|on)|"
    r"reproduced\s+(?:in|on|when)|here(?:'s|\s+is)|attached|screenshot|recording|video|"
    r"see\s+attached|log\s*:|error\s+message\s*:|stack\s+trace|when\s+i\s+|after\s+i\s+|"
    r"if\s+i\s+|in\s+(?:chrome|safari|firefox|edge)|picking\s+|selecting\s+|clicking\s+|"
    r"it\s+(?:shows|reverts|resets|throws|returns|displays|drops|wipes|clears)|"
    r"(?:this|that)\s+(?:happens|causes|triggers))\b",
    re.I,
)
INTRO_LINE_RE = re.compile(
    r"^\s*(?:(?:hi|hey|hello)(?:\s+\w+)?[,!.]?\s*)?"
    r"(?:thanks?(?:\s+(?:so\s+much|a\s+lot|for\s+(?:shipping|fixing|this|the\s+update)))?[!.]?\s*)?"
    r"(?:(?:a\s+)?(?:few|couple\s+of|two|three|several)\s+(?:things?|items?|follow-?ups?|requests?|notes?)|"
    r"follow-?ups?|requests?|feedback)\s*:\s*$",
    re.I,
)
LEADING_GREETING_RE = re.compile(
    r"^\s*(?:(?:hi|hey|hello)(?:\s+team|\s+all)?[,!]\s+|"
    r"thanks?(?:\s+(?:so\s+much|a\s+lot|for\s+(?:shipping|fixing|this|the\s+update)))?[!.,]\s+)+",
    re.I,
)
SENTENCE_PIVOT_RE = re.compile(
    r"^\s*(?:also|additionally|separately|second(?:ly)?|third(?:ly)?|next|another(?:\s+(?:thing|item|request|issue|bug))?|"
    r"plus|on\s+top\s+of\s+that|besides\s+that|on\s+another\s+note|one\s+more\s+thing|"
    r"please\s+also|can\s+(?:we|you)\s+also|could\s+(?:we|you)\s+also|"
    r"we(?:'d|\s+would)\s+also\s+(?:like|need|want)|we\s+also\s+(?:need|want|request))\b",
    re.I,
)
COMPOUND_SPLIT_PATTERNS = (
    re.compile(
        r"(?:,[ \t]*|(?<![ \t])[ \t]+)and[ \t]+(?:(?:please[ \t]+)?also[ \t]+|also[ \t]+(?:please[ \t]+)?)"
        r"(?=(?:add|create|support|implement|include|allow|enable|request(?:ing)?|change|update|fix|build|export|"
        r"we(?:'d|[ \t]+would)?[ \t]+(?:like|need|want)|can[ \t]+(?:we|you)\b|could[ \t]+(?:we|you)\b|a[ \t]+new\b|please\b))",
        re.I,
    ),
    re.compile(
        r",[ \t]*and[ \t]+(?=(?:please[ \t]+(?:add|create|support|implement|include|allow|enable|request|change|update|fix|build|export)|"
        r"request(?:ing)?[ \t]+(?:a[ \t]+)?new\b|add[ \t]+(?:a[ \t]+)?new\b|"
        r"we(?:'d|[ \t]+would)?[ \t]+(?:also[ \t]+)?(?:like|need|want)\b|"
        r"can[ \t]+(?:we|you)[ \t]+(?:also[ \t]+)?(?:add|create|support|change|update|fix)\b|"
        r"could[ \t]+(?:we|you)[ \t]+(?:also[ \t]+)?(?:add|create|support|change|update|fix)\b))",
        re.I,
    ),
    re.compile(
        r"(?<![ \t])[ \t]+and[ \t]+(?=(?:request(?:ing)?[ \t]+(?:a[ \t]+)?new\b|please[ \t]+(?:also[ \t]+)?(?:add|create|support|implement|change|update|fix)\b))",
        re.I,
    ),
    re.compile(
        r"(?:,[ \t]*|(?<![ \t])[ \t]+)plus[ \t]+(?=(?:please[ \t]+|also[ \t]+|add\b|a[ \t]+new\b|fix\b|support\b|change\b|update\b|request\b|"
        r"we(?:'d|[ \t]+would)?[ \t]+(?:like|need|want)\b|can[ \t]+(?:we|you)\b|could[ \t]+(?:we|you)\b))",
        re.I,
    ),
    re.compile(r";[ \t]+(?=(?:also[ \t]+|please[ \t]+|add\b|fix\b|change\b|update\b|support\b|request\b))", re.I),
)

DEPENDENCY_RE = re.compile(
    r"\b(?:after\s+(?:fixing|resolving|that(?:'s|\s+is)\s+fixed)|"
    r"once\s+(?:that|the\s+(?:first|above|previous|save)\s+(?:bug|fix|issue))\s+(?:is\s+)?(?:fixed|resolved|done|merged)|"
    r"depends?\s+on\s+(?:the\s+)?(?:first|previous|above)|"
    r"blocked\s+by\s+(?:the\s+)?(?:first|previous|above))\b",
    re.I,
)

BUG_STRONG_PATTERNS = (
    re.compile(r"\b(?:bug|regression|broken|breaks?|broke|crash(?:es|ed|ing)?|exception|traceback|"
               r"500\b|502\b|503\b|504\b|404\b|error(?:\s+message)?|fails?|failed|failing|failure)\b", re.I),
    re.compile(r"\b(?:loses?|lost|losing|drops?|dropped|dropping|wipes?|wiped|wiping|"
               r"clears?|cleared|clearing|resets?|resetting|reverts?|reverted|reverting|"
               r"corrupts?|corrupted)\b", re.I),
    re.compile(r"\b(?:doesn['’]?t\s+work|does\s+not\s+work|didn['’]?t\s+work|not\s+working|"
               r"stopped\s+working|no\s+longer\s+\w+|unable\s+to|still\s+(?:broken|fails?|failing|loses?|drops?|clears?|resets?|wipes?|happening)|"
               r"previously\s+supported|used\s+to\s+work|supposed\s+to|same\s+(?:bug|issue|problem))\b", re.I),
)
CHANGE_STRONG_PATTERNS = (
    re.compile(r"\b(?:change\s+the\s+default|switch\s+(?:the\s+default\s+)?to\b|"
               r"we(?:'d|\s+would)\s+(?:also\s+)?like\s+(?:a|an|to\s+add|to\s+support)|"
               r"would\s+be\s+(?:great|nice|helpful)\s+to\s+(?:have|add|support)|"
               r"allow\s+users?\s+to|enable\s+users?\s+to)\b", re.I),
    re.compile(r"\b(?:change\s+request|feature\s+request|new\s+feature|enhancement|"
               r"(?:add|request(?:ing)?|introduce|build|create)\s+(?:a\s+)?new\b|"
               r"(?:please\s+|can\s+(?:we|you)\s+|could\s+(?:we|you)\s+|let['’]?s\s+)?(?:also\s+)?add\s+(?:a|an|the)?\s*[a-z0-9_-]+|"
               r"new\s+(?:csv|pdf|json|excel|xlsx)?\s*export\b|(?:csv|pdf|json|excel|xlsx)\s+export\b|"
               r"support\s+(?:exporting|importing|filtering|sorting|searching)|"
               r"export\s+(?:to|as)\s+(?:csv|pdf|json|excel|xlsx))\b", re.I),
)
_INSTEAD_OF_RE = re.compile(r"\binstead\s+of\b", re.I)
_INSTEAD_VERB_RE = re.compile(r"\b(?:show|use|display|sort|return|default)\b", re.I)
_MAX_SPLIT_DEPTH = 16
REPEAT_BUG_RE = re.compile(
    r"\b(?:same\s+(?:[\w-]+\s+){0,2}(?:bug|issue|problem|regression)|"
    r"(?:also\s+)?seeing\s+(?:this|the\s+same)|"
    r"hitting\s+(?:this|the\s+same)|"
    r"reproduced\s+(?:this|the\s+same|in|on)|"
    r"still\s+(?:happening|seeing|reproducing|broken|loses?|drops?|wipes?|resets?)|"
    r"another\s+(?:report|example|repro)\s+of)\b",
    re.I,
)
LEADING_LABEL_FLUFF_RE = re.compile(
    r"^(?:(?:also|additionally|separately|second(?:ly)?|third(?:ly)?|plus|and|please|can\s+(?:we|you)|could\s+(?:we|you)|"
    r"would\s+you|we(?:'d|\s+would)\s+(?:also\s+)?(?:like|need|want)\s+(?:to\s+)?|"
    r"we\s+(?:also\s+)?(?:need|want)\s+(?:to\s+)?|let['’]?s\s+(?:also\s+)?|"
    r"request(?:ing)?\s+(?:a\s+)?(?:new\s+)?|add(?:ing)?\s+(?:a\s+)?(?:new\s+)?|"
    r"seeing\s+(?:the\s+)?same\s+|same\s+issue\s+with\s+|same\s+bug\s+with\s+|"
    r"report(?:ing)?\s+(?:a\s+)?)[,:]?\s*)+",
    re.I,
)
CLARIFIED_RE = re.compile(
    r"^Original feedback:\s*(?P<orig>.*?)\n\nClarification asked:\s*(?P<q>.*?)\n\n"
    r"Authorized answer from [^:\n]+:\s*(?P<ans>.*)$",
    re.S,
)
GH_ISSUE_URL_RE = re.compile(r"^https?://[^/]+/[^/]+/[^/]+/issues/([1-9][0-9]*)/?$")


@dataclass(frozen=True)
class DeliveredOutcome:
    repo: str
    ticket: str
    tracker: str
    title: str = ""
    body: str = ""
    url: str = ""
    delivery_summary: str = ""


@dataclass(frozen=True)
class PlannedRequest:
    kind: FollowUpKind
    outcome_label: str
    title: str
    summary: str
    evidence: str
    definition_of_done: tuple[str, ...]
    fingerprint: str
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class FollowUpItemResult:
    item_id: str
    kind: FollowUpKind
    action: FollowUpAction
    outcome_label: str
    target_ticket: str
    target_ref: str
    target_url: str
    evidence_url: str = ""
    linked: bool = False
    status: str = ""


@dataclass(frozen=True)
class FollowUpResult:
    followup_id: str
    repo: str
    original_ticket: str
    tracker: str
    comment_id: str
    status: str
    reply_url: str
    reply_body: str
    items: tuple[FollowUpItemResult, ...]
    replayed: bool = False


Analyzer = Callable[[str, DeliveredOutcome], list[PlannedRequest] | None]
OpenChecker = Callable[[str], bool | None]


def _public_safe(repo: config.Repo, text: str) -> str:
    cleaned = outbound.inert(outbound.screen(vault.hidden(text)))
    return outbound.redact(cleaned) if repo.public else cleaned


def _domain_tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", (text or "").casefold())
    tokens: list[str] = []
    for tok in raw:
        if len(tok) <= 1 or tok.isdigit():
            continue
        lemma = LEMMA_MAP.get(tok, tok)
        if lemma in STOPWORDS or len(lemma) <= 1:
            continue
        tokens.append(lemma)
    return tokens


def _fingerprint(kind: FollowUpKind, text: str) -> str:
    unique = sorted(set(_domain_tokens(text)))
    basis = unique[:12] if unique else [re.sub(r"\s+", " ", text.strip().casefold())[:80]]
    return ticket_feedback.digest([kind, basis])[:16]


def _has_instead_of_change(text: str) -> bool:
    for line in text.splitlines():
        if m := _INSTEAD_OF_RE.search(line):
            if _INSTEAD_VERB_RE.search(line, m.end()):
                return True
    return False


def _has_action_content(clause: str) -> bool:
    tokens = _domain_tokens(clause)
    if len(tokens) < 2:
        return False
    return bool(
        ticket_feedback.ACTION_SIGNAL_RE.search(clause)
        or _has_instead_of_change(clause)
        or any(p.search(clause) for p in (*BUG_STRONG_PATTERNS, *CHANGE_STRONG_PATTERNS))
    )


def _is_evidence_sentence(sentence: str) -> bool:
    stripped = sentence.strip()
    if not stripped:
        return True
    if SENTENCE_PIVOT_RE.match(stripped):
        return False
    return bool(EVIDENCE_LEAD_RE.match(stripped))


def _split_compound_sentence(sentence: str, *, _depth: int = 0) -> list[str]:
    text = re.sub(r"[ \t\r\n]+", " ", sentence.strip())
    if not text:
        return []
    if _depth >= _MAX_SPLIT_DEPTH:
        return [text]
    for pattern in COMPOUND_SPLIT_PATTERNS:
        parts = [p.strip(" ,;") for p in pattern.split(text) if p and p.strip(" ,;")]
        if len(parts) >= 2 and all(_has_action_content(p) and not _is_evidence_sentence(p) for p in parts):
            out: list[str] = []
            for part in parts:
                out.extend(_split_compound_sentence(part, _depth=_depth + 1) if part != text else [part])
            return out
    return [text]


def _split_sentences(paragraph: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", paragraph.strip())
    return [c.strip() for c in chunks if c and c.strip()]


def _split_paragraph(paragraph: str) -> list[str]:
    cleaned = LEADING_GREETING_RE.sub("", paragraph.strip()).strip()
    if not cleaned:
        return []
    sentences = _split_sentences(cleaned)
    if not sentences:
        return []
    groups: list[str] = []
    for sent in sentences:
        if not groups:
            groups.extend(_split_compound_sentence(sent))
            continue
        if _is_evidence_sentence(sent):
            groups[-1] = f"{groups[-1]} {sent}"
            continue
        if SENTENCE_PIVOT_RE.match(sent) and _has_action_content(sent):
            groups.extend(_split_compound_sentence(sent))
            continue
        groups[-1] = f"{groups[-1]} {sent}"
    return groups


def split_requests(text: str) -> list[str]:
    """Split a comment into independently actionable request clauses while keeping evidence attached."""
    raw = (text or "").strip()
    if not raw:
        return []
    if clarified := CLARIFIED_RE.fullmatch(raw):
        combined = f"{clarified.group('orig').strip()} {clarified.group('ans').strip()}".strip()
        return [combined] if combined else []

    cleaned = ticket_feedback._strip_relay_prefix(raw)
    cleaned = ticket_feedback.MENTION_LEAD_RE.sub("", cleaned).strip()
    if not cleaned:
        return []

    # Parse bullet / numbered lists while keeping repro-step sublists attached to their parent item.
    lines = cleaned.splitlines()
    bullet_items: list[str] = []
    non_bullet_prefix: list[str] = []
    in_repro_sublist = False
    saw_top_bullet = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            in_repro_sublist = False
            continue
        if REPRO_HEADER_RE.match(stripped):
            in_repro_sublist = True
            if bullet_items:
                bullet_items[-1] = f"{bullet_items[-1]}\n{stripped}"
            else:
                non_bullet_prefix.append(stripped)
            continue
        if m := BULLET_LINE_RE.match(line):
            item_text = m.group(1).strip()
            if in_repro_sublist and bullet_items:
                bullet_items[-1] = f"{bullet_items[-1]}\n- {item_text}"
            elif in_repro_sublist and non_bullet_prefix:
                non_bullet_prefix.append(f"- {item_text}")
            else:
                saw_top_bullet = True
                bullet_items.append(item_text)
            continue
        if saw_top_bullet and bullet_items:
            bullet_items[-1] = f"{bullet_items[-1]} {stripped}"
        elif not INTRO_LINE_RE.match(stripped):
            non_bullet_prefix.append(stripped)

    if len(bullet_items) >= 2:
        results: list[str] = []
        if non_bullet_prefix:
            prefix_text = " ".join(non_bullet_prefix).strip()
            if _has_action_content(prefix_text) and not INTRO_LINE_RE.match(prefix_text):
                results.extend(_split_paragraph(prefix_text))
        for item in bullet_items:
            if _has_action_content(item):
                results.append(item)
            elif results:
                results[-1] = f"{results[-1]}\n{item}"
            else:
                results.append(item)
        return results

    # Paragraph-level splitting, keeping evidence paragraphs attached to the preceding item.
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n+", cleaned) if p.strip()]
    clauses: list[str] = []
    for para in paragraphs:
        single_line = " ".join(line.strip() for line in para.splitlines() if line.strip())
        if INTRO_LINE_RE.match(single_line):
            continue
        if clauses and (REPRO_HEADER_RE.match(para.splitlines()[0].strip()) or _is_evidence_sentence(single_line)):
            clauses[-1] = f"{clauses[-1]}\n\n{para}"
            continue
        clauses.extend(_split_paragraph(single_line))
    return clauses or [cleaned]


def classify_request(clause: str, delivered: DeliveredOutcome) -> FollowUpKind:
    """Compare a follow-up request against the delivered outcome to distinguish bug vs change_request."""
    text = (clause or "").strip()
    bug_hits = sum(1 for pat in BUG_STRONG_PATTERNS if pat.search(text))
    change_hits = sum(1 for pat in CHANGE_STRONG_PATTERNS if pat.search(text)) + int(_has_instead_of_change(text))

    delivered_tokens = set(_domain_tokens(f"{delivered.title} {delivered.body} {delivered.delivery_summary}"))
    clause_tokens = set(_domain_tokens(text))
    overlap = delivered_tokens & clause_tokens

    # Strong regression/failure signals always classify as bug unless explicitly framed solely as a new feature.
    if bug_hits > 0 and change_hits == 0:
        return "bug"
    if change_hits > 0 and bug_hits == 0:
        return "change_request"
    if bug_hits > 0 and change_hits > 0:
        # e.g., "instead of keeping the selected value, Save resets it" -> bug;
        # "add a new CSV export" has no bug hits.
        if re.search(r"\b(?:regression|broken|crash|500|error|loses?|lost|drops?|wipes?|resets?|reverts?|fails?|same\s+bug)\b",
                     text, re.I):
            return "bug"
        return "change_request"

    # Tie-breaker: compare against delivered outcome capabilities.
    if re.search(r"\b(?:missing|wrong|incorrect|unexpected|still\b|cannot|can['’]?t|won['’]?t)\b", text, re.I):
        if overlap or not re.search(r"\b(?:add|new|support|export|option|feature)\b", text, re.I):
            return "bug"
        return "change_request"
    if re.search(r"\b(?:add|new|change|update|switch|allow|enable|include|support|export|show|hide|sort)\b", text, re.I):
        return "change_request"
    return "bug" if overlap else "change_request"


def _outcome_label(clause: str, kind: FollowUpKind) -> str:
    first = util.first_line(clause, 140)
    first = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", first).strip()
    first = LEADING_LABEL_FLUFF_RE.sub("", first).strip()
    # Trim trailing evidence clause after " — " or " - " or "; " if the lead is already descriptive.
    for sep in (" — ", " -- ", "; "):
        if sep in first:
            lead = first.split(sep, 1)[0].strip()
            if len(lead.split()) >= 2:
                first = lead
                break
    # Keep first sentence if multi-sentence.
    sentences = _split_sentences(first)
    if sentences:
        first = sentences[0]
    first = first.strip(" .!?:;,-")
    if not first:
        return "Follow-up bug fix" if kind == "bug" else "Follow-up change request"
    if len(first) > 72:
        first = first[:69].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."
    return first[0].upper() + first[1:]


def _ticket_title(outcome_label: str, kind: FollowUpKind) -> str:
    clean = re.sub(r"\s+", " ", outcome_label).strip().rstrip(".")
    if kind == "bug":
        if re.match(r"^(?:fix|bug|regression)\b", clean, re.I):
            title = clean
        else:
            title = f"Fix: {clean}"
    else:
        title = clean
    if len(title) > 100:
        title = title[:97].rsplit(" ", 1)[0] + "..."
    return title


def _definition_of_done(clause: str, kind: FollowUpKind, outcome_label: str, delivered: DeliveredOutcome) -> tuple[str, ...]:
    summary = util.first_line(clause, 180)
    if kind == "bug":
        return (
            f"Resolve the reported regression: {summary}",
            f"Verify previously delivered behavior from {tracker.ref_for(delivered.ticket)} remains intact.",
            "Add or update a focused regression check covering the reported failure.",
        )
    return (
        f"Implement the requested change: {summary}",
        f"Preserve existing delivered behavior from {tracker.ref_for(delivered.ticket)}.",
        f"Verify {outcome_label} works end-to-end according to the repository's release policy.",
    )


def analyze_requests(text: str, delivered: DeliveredOutcome, *,
                     analyzer: Analyzer | None = None) -> list[PlannedRequest]:
    """Extract distinct outcomes, classify bug vs change request, and build executable definitions of done."""
    if analyzer is not None:
        custom = analyzer(text, delivered)
        if custom is not None:
            return custom

    clauses = split_requests(text)
    planned: list[PlannedRequest] = []
    for idx, clause in enumerate(clauses):
        kind = classify_request(clause, delivered)
        label = _outcome_label(clause, kind)
        title = _ticket_title(label, kind)
        dod = _definition_of_done(clause, kind, label, delivered)
        deps = (idx - 1,) if idx > 0 and DEPENDENCY_RE.search(clause) else ()
        fp = _fingerprint(kind, f"{label} {clause}")
        planned.append(PlannedRequest(
            kind=kind,
            outcome_label=label,
            title=title,
            summary=util.first_line(clause, 240),
            evidence=clause.strip(),
            definition_of_done=dod,
            fingerprint=fp,
            depends_on=deps,
        ))
    return planned


def is_same_bug(planned: PlannedRequest, existing_item: dict, *, single_open_bug: bool = False) -> bool:
    """Return True when a newly reported bug matches an existing open follow-up bug on the same origin."""
    if planned.kind != "bug" or existing_item.get("kind") != "bug":
        return False
    if planned.fingerprint and planned.fingerprint == existing_item.get("fingerprint"):
        return True
    new_tokens = set(_domain_tokens(f"{planned.outcome_label} {planned.title} {planned.evidence}"))
    existing_text = (
        f"{existing_item.get('outcome_label', '')} {existing_item.get('title', '')} "
        f"{(existing_item.get('data') or {}).get('evidence', '')}"
    )
    old_tokens = set(_domain_tokens(existing_text))
    if not new_tokens or not old_tokens:
        return False
    overlap = new_tokens & old_tokens
    if REPEAT_BUG_RE.search(planned.evidence):
        if overlap:
            return True
        if single_open_bug and len(new_tokens) <= 2:
            return True
    if len(overlap) < 2:
        return False
    smaller = min(len(new_tokens), len(old_tokens))
    union = len(new_tokens | old_tokens)
    return (len(overlap) / smaller) >= 0.5 or (len(overlap) / union) >= 0.35


def _is_ticket_open(tk: tracker.Tracker, tid: str, *, open_checker: OpenChecker | None = None) -> bool:
    if open_checker is not None:
        checked = open_checker(str(tid))
        if checked is not None:
            return bool(checked)
    try:
        if isinstance(tk, tracker.GitHubTracker):
            state = tk.is_open(int(tid))
            return bool(state) if state is not None else False
        if isinstance(tk, tracker.JiraTracker):
            state = tk.is_open(str(tid))
            return bool(state) if state is not None else False
        if isinstance(tk, tracker.LocalTracker):
            state = tracker.task_state(str(tid))
            return state is not None and state not in config.TASK_FINISHED
    except (gh.GhError, jira.JiraError, tracker.TaskError, ValueError, OSError):
        return False
    return False


def find_matching_open_bug(repo: config.Repo, tk: tracker.Tracker, original_ticket: str,
                           planned: PlannedRequest, *, open_checker: OpenChecker | None = None) -> dict | None:
    """Find an already-open follow-up bug linked to original_ticket matching planned."""
    if planned.kind != "bug":
        return None
    candidates = [
        row for row in feedback_store.followup_items_for_origin(
            repo.name, str(original_ticket), tk.name, kind="bug", action="create",
        )
        if row.get("target_ticket")
    ]
    if not candidates:
        return None
    open_candidates = [row for row in candidates if _is_ticket_open(tk, row["target_ticket"], open_checker=open_checker)]
    if not open_candidates:
        return None
    single_open = len(open_candidates) == 1
    for candidate in open_candidates:
        if is_same_bug(planned, candidate, single_open_bug=single_open):
            return candidate
    return None


def delivered_outcome(cfg: config.Config, repo: config.Repo, tk: tracker.Tracker, tid: str, *,
                      ticket: tracker.Ticket | None = None) -> DeliveredOutcome:
    """Read delivered context for the completed original ticket without mutating its state or retro."""
    tid = str(tid)
    title, body, url = "", "", ""
    if ticket is not None:
        title, body, url = ticket.title, ticket.body, ticket.url
    else:
        try:
            if isinstance(tk, tracker.JiraTracker):
                fields = (tk.client.issue(tid, ["summary", "description"]) or {}).get("fields") or {}
                title = fields.get("summary") or ""
                body = fields.get("description") or ""
                url = tk.client.browse_url(tid)
            else:
                fetched = tk.get(tid)
                title, body, url = fetched.title, fetched.body, fetched.url
        except (gh.GhError, jira.JiraError, tracker.TaskError, ValueError, OSError):
            pass
    if not url:
        if isinstance(tk, tracker.GitHubTracker) and repo.github:
            url = gh.issue_url(repo.github, tid)
        elif isinstance(tk, tracker.JiraTracker):
            url = tk.client.browse_url(tid)
        elif isinstance(tk, tracker.LocalTracker):
            url = f"local:{tid}"

    summary_parts: list[str] = []
    try:
        claim = spec_store.find(repo.name, tid)
        if claim and isinstance(claim.get("data"), dict):
            if delivered_note := claim["data"].get("summary") or claim["data"].get("outcome"):
                summary_parts.append(str(delivered_note))
    except (db.NoDatabase, OSError, ValueError):
        pass
    try:
        runs = run_store.ticket_runs(repo.name, tid, limit=5, live=True)
        for run in runs:
            if run.get("title"):
                summary_parts.append(str(run["title"]))
    except (db.NoDatabase, OSError, ValueError):
        pass
    return DeliveredOutcome(
        repo=repo.name,
        ticket=tid,
        tracker=tk.name,
        title=title,
        body=body,
        url=url,
        delivery_summary=" ".join(dict.fromkeys(summary_parts)),
    )


def _format_followup_body(repo: config.Repo, delivered: DeliveredOutcome, planned: PlannedRequest,
                          comment_row: dict, dep_refs: list[str]) -> str:
    kind_label = "Bug" if planned.kind == "bug" else "Change request"
    orig_ref = tracker.ref_for(delivered.ticket)
    provenance = f"[{orig_ref}]({delivered.url})" if delivered.url else orig_ref
    who = comment_row.get("author_display") or comment_row.get("author") or "requester"
    via = f" (via {comment_row['relayed_by']})" if comment_row.get("relayed_by") else ""
    source_link = f" ([source comment]({comment_row['url']}))" if comment_row.get("url") else ""

    lines = [
        f"Follow-up to {provenance}",
        f"Classification: {kind_label}",
        f"Reported by: {who}{via}{source_link}",
    ]
    if dep_refs:
        lines.append(f"Waits on {', '.join(dep_refs)}")
    lines.extend([
        "",
        "### Requested Outcome",
        "",
        planned.evidence,
    ])
    if delivered.title:
        lines.extend([
            "",
            "### Completed Original Context",
            "",
            f"Original ticket {provenance}: {delivered.title}",
        ])
    if planned.definition_of_done:
        lines.extend(["", "### Definition of Done", ""])
        lines.extend(f"- {check}" for check in planned.definition_of_done)
    return _public_safe(repo, "\n".join(lines))


def _ticket_labels(repo: config.Repo, kind: FollowUpKind, dod: tuple[str, ...]) -> list[str]:
    labels = ["bug" if kind == "bug" else "change-request"]
    if dod and repo.pipeline:
        labels.append(tracker.LABEL)
    return labels


def _parse_created_target(tk: tracker.Tracker, url: str) -> tuple[str, str, str]:
    clean_url = (url or "").strip()
    if isinstance(tk, tracker.GitHubTracker):
        match = GH_ISSUE_URL_RE.fullmatch(clean_url)
        if not match:
            raise gh.GhError(f"could not parse GitHub follow-up issue URL: {clean_url!r}")
        number = match.group(1)
        return number, f"#{number}", clean_url
    if isinstance(tk, tracker.JiraTracker):
        key = clean_url.rstrip("/").rsplit("/", 1)[-1].upper()
        if not jira.KEY_RE.fullmatch(key):
            raise jira.JiraError(f"could not parse Jira follow-up issue key from URL: {clean_url!r}")
        return key, key, clean_url
    tid = clean_url.removeprefix("local:")
    return tid, tid, clean_url


def _ensure_local_followup_task(repo: config.Repo, item: dict) -> tuple[str, str, str]:
    marker = item["marker"]
    for row in tasks_store.tasks(repo.name):
        if marker in (row.get("body") or "").splitlines():
            tid = row["id"]
            return tid, tid, f"local:{tid}"
    kind = "fix" if item["kind"] == "bug" else "feat"
    body = f"{item['body'].rstrip()}\n\n{marker}"
    tid = tasks_store.task_add(repo.name, body, title=item["title"], kind=kind)
    return tid, tid, f"local:{tid}"


def _create_item_ticket(cfg: config.Config, repo: config.Repo, tk: tracker.Tracker,
                        item: dict, dep_items: list[dict]) -> tuple[str, str, str]:
    body = item["body"]
    if dep_items:
        dep_refs = [d["target_ref"] for d in dep_items if d.get("target_ref")]
        if dep_refs and not re.search(r"^Waits on\b", body, re.M):
            body = f"Waits on {', '.join(dep_refs)}\n\n{body}"

    if isinstance(tk, tracker.LocalTracker):
        return _ensure_local_followup_task(repo, {**item, "body": body})

    data = item.get("data") or {}
    dod = tuple(data.get("definition_of_done") or ())
    labels = _ticket_labels(repo, item["kind"], dod)
    issue_type = "Bug" if item["kind"] == "bug" else "Task"
    jira_client = tk.client if isinstance(tk, tracker.JiraTracker) else None
    url = ticket_publication.follow_up(
        cfg,
        repo,
        item["title"],
        body,
        item["publication_key"],
        labels=labels,
        issue_type=issue_type,
        jira_client=jira_client,
    )
    return _parse_created_target(tk, url)


def _ensure_github_dependency(repo: config.Repo, prereq_tid: str, dependent_tid: str) -> None:
    slug = gh._safe_repo(repo.github)
    path = f"repos/{slug}/issues/{int(dependent_tid)}/dependencies/blocked_by"
    existing = gh.api_list(path)
    if any(str(row.get("number")) == str(prereq_tid) for row in existing):
        return
    prereq_id = gh.issue_node(slug, int(prereq_tid))["id"]
    gh.gh(
        ["api", "-X", "POST", path, "--input", "-"],
        input=gh.Outgoing(json.dumps({"issue_id": int(prereq_id)})),
        write=True,
        retry=False,
    )


def _ensure_backlink(repo: config.Repo, tk: tracker.Tracker, followup: dict,
                     item: dict, dep_items: list[dict]) -> None:
    target = item["target_ticket"]
    original = followup["ticket"]
    if isinstance(tk, tracker.JiraTracker):
        tk.client.link_issues(target, original, preferred_type="Relates")
        for dep in dep_items:
            if dep.get("target_ticket"):
                tk.client.link_blocks(dep["target_ticket"], target)
    elif isinstance(tk, tracker.GitHubTracker):
        # Provenance backlink (`Follow-up to [#<orig>](<url>)`) is embedded in the issue body,
        # creating GitHub's native timeline cross-reference without adding a false blocker.
        for dep in dep_items:
            if dep.get("target_ticket"):
                _ensure_github_dependency(repo, dep["target_ticket"], target)


def _find_marker_comment_url(repo: config.Repo, tk: tracker.Tracker, tid: str, marker: str) -> str:
    for row in ticket_feedback.fetch_thread_comments(repo, tk, str(tid)):
        if ticket_feedback._is_trusted_marker_author(tk, row) and marker in (row.body or "").splitlines():
            return row.url or f"{tk.name}:{tid}:{marker}"
    return ""


def _post_marked_comment(repo: config.Repo, tk: tracker.Tracker, tid: str, text: str, marker: str) -> str:
    if existing := _find_marker_comment_url(repo, tk, tid, marker):
        return existing
    body = gh.marked(f"{tracker.ARETE_TAG} {_public_safe(repo, text)}\n\n{marker}")
    url = ""
    if isinstance(tk, tracker.GitHubTracker):
        url = gh.issue_comment(repo.github, int(tid), body, return_url=True, single_attempt=True) or ""
    elif isinstance(tk, tracker.JiraTracker):
        url = tk.client.comment(str(tid), body, return_url=True, single_attempt=True) or ""
    elif isinstance(tk, tracker.LocalTracker):
        if err := tk.comment(str(tid), body):
            raise tracker.TaskError(err)
        url = f"local:{tid}:{marker}"
    return url or _find_marker_comment_url(repo, tk, tid, marker) or f"{tk.name}:{tid}:{marker}"


def _format_evidence_comment(followup: dict, item: dict, comment_row: dict) -> str:
    who = comment_row.get("author_display") or comment_row.get("author") or "requester"
    via = f" (via {comment_row['relayed_by']})" if comment_row.get("relayed_by") else ""
    orig_ref = tracker.ref_for(followup["ticket"])
    orig_link = f"[{orig_ref}]({followup['original_url']})" if followup.get("original_url") else orig_ref
    source_ref = f" ([source comment]({comment_row['url']}))" if comment_row.get("url") else ""
    evidence = (item.get("data") or {}).get("evidence") or comment_row.get("body") or ""
    return (
        f"Additional report and evidence for this open bug from {who}{via} on {orig_link}{source_ref}:\n\n"
        f"{evidence.strip()}"
    )


def format_requester_reply(items: list[dict]) -> str:
    """Build ONE plain-language requester reply pairing every distinct outcome with its clickable link."""
    def link(item: dict) -> str:
        ref = item.get("target_ref") or tracker.ref_for(item.get("target_ticket", ""))
        url = item.get("target_url") or ""
        return f"[{ref}]({url})" if url else ref

    def kind_word(item: dict) -> str:
        return "bug" if item.get("kind") == "bug" else "change request"

    created = [i for i in items if i.get("action") == "create"]
    appended = [i for i in items if i.get("action") == "append"]

    if len(items) == 1:
        only = items[0]
        if only.get("action") == "append":
            return (
                f"We added your new evidence to the existing open follow-up ticket for "
                f"{only['outcome_label']}: {link(only)}."
            )
        return (
            f"We're implementing your request in a new ticket: "
            f"{only['outcome_label']} ({kind_word(only)}) is tracked in {link(only)}."
        )

    if created and not appended:
        count = COUNT_WORDS.get(len(created), str(len(created)))
        header = f"We're implementing your requests in {count} new tickets:"
        bullets = [f"- {i['outcome_label']} ({kind_word(i)}): {link(i)}" for i in items]
        return "\n".join([header, "", *bullets])

    if appended and not created:
        header = "We added your new evidence to the existing open follow-up tickets:"
        bullets = [f"- {i['outcome_label']} ({kind_word(i)}): {link(i)}" for i in items]
        return "\n".join([header, "", *bullets])

    if len(created) == 1:
        header = ("We're implementing your new request in a new ticket and added your additional evidence "
                  "to the existing open follow-up:")
    else:
        count = COUNT_WORDS.get(len(created), str(len(created)))
        header = (f"We're implementing your new requests in {count} new tickets and added your additional evidence "
                  "to the existing open follow-up:")
    bullets = [
        f"- {i['outcome_label']} ({kind_word(i)}, "
        f"{'existing open ticket' if i.get('action') == 'append' else 'new ticket'}): {link(i)}"
        for i in items
    ]
    return "\n".join([header, "", *bullets])


def _to_result(followup: dict, items: list[dict], *, replayed: bool = False) -> FollowUpResult:
    return FollowUpResult(
        followup_id=followup["id"],
        repo=followup["repo"],
        original_ticket=followup["ticket"],
        tracker=followup["tracker"],
        comment_id=followup["comment_id"],
        status=followup["status"],
        reply_url=followup.get("reply_url", ""),
        reply_body=followup.get("reply_body", ""),
        items=tuple(
            FollowUpItemResult(
                item_id=i["id"],
                kind=i["kind"],
                action=i["action"],
                outcome_label=i["outcome_label"],
                target_ticket=i.get("target_ticket", ""),
                target_ref=i.get("target_ref", ""),
                target_url=i.get("target_url", ""),
                evidence_url=i.get("evidence_url", ""),
                linked=bool(i.get("linked", False)),
                status=i.get("status", ""),
            )
            for i in items
        ),
        replayed=replayed,
    )


def _build_plan_rows(followup_id: str, repo: config.Repo, tk: tracker.Tracker,
                     delivered: DeliveredOutcome, comment_row: dict,
                     planned_list: list[PlannedRequest], *,
                     open_checker: OpenChecker | None = None) -> tuple[dict, list[dict]]:
    tid = str(comment_row["ticket"])
    cid = str(comment_row["comment_id"])
    reply_marker = f"{REPLY_MARKER_PREFIX}{followup_id}-{secrets.token_hex(12)}"
    followup_row = {
        "id": followup_id,
        "repo": repo.name,
        "ticket": tid,
        "tracker": tk.name,
        "comment_id": cid,
        "original_url": delivered.url,
        "status": "planned",
        "reply_marker": reply_marker,
        "data": {
            "source_author": comment_row.get("author", ""),
            "source_url": comment_row.get("url", ""),
            "original_title": delivered.title,
        },
    }
    item_ids = [
        "fupi-" + ticket_feedback.digest([followup_id, seq])[:16]
        for seq in range(len(planned_list))
    ]
    item_rows: list[dict] = []
    for seq, planned in enumerate(planned_list):
        item_id = item_ids[seq]
        matched = find_matching_open_bug(repo, tk, tid, planned, open_checker=open_checker)
        dep_ids = [item_ids[d] for d in planned.depends_on if 0 <= d < seq]
        if matched is not None:
            marker = f"{EVIDENCE_MARKER_PREFIX}{item_id}-{secrets.token_hex(12)}"
            item_rows.append({
                "id": item_id,
                "followup_id": followup_id,
                "seq": seq,
                "repo": repo.name,
                "original_ticket": tid,
                "tracker": tk.name,
                "kind": "bug",
                "action": "append",
                "outcome_label": matched.get("outcome_label") or planned.outcome_label,
                "title": matched.get("title") or planned.title,
                "body": "",
                "fingerprint": matched.get("fingerprint") or planned.fingerprint,
                "publication_key": "",
                "marker": marker,
                "target_ticket": matched["target_ticket"],
                "target_ref": matched["target_ref"],
                "target_url": matched["target_url"],
                "status": "pending",
                "linked": 0,
                "depends_on": "",
                "data": {
                    "evidence": planned.evidence,
                    "matched_item_id": matched["id"],
                    "summary": planned.summary,
                },
            })
        else:
            pub_key = f"fup-{ticket_feedback.digest([repo.name, tid, tk.name, cid, seq])[:32]}"
            marker = f"{LOCAL_MARKER_PREFIX}{item_id}-{secrets.token_hex(12)}"
            body = _format_followup_body(repo, delivered, planned, comment_row, [])
            item_rows.append({
                "id": item_id,
                "followup_id": followup_id,
                "seq": seq,
                "repo": repo.name,
                "original_ticket": tid,
                "tracker": tk.name,
                "kind": planned.kind,
                "action": "create",
                "outcome_label": planned.outcome_label,
                "title": planned.title,
                "body": body,
                "fingerprint": planned.fingerprint,
                "publication_key": pub_key,
                "marker": marker,
                "target_ticket": "",
                "target_ref": "",
                "target_url": "",
                "status": "pending",
                "linked": 0,
                "depends_on": ",".join(dep_ids),
                "data": {
                    "evidence": planned.evidence,
                    "summary": planned.summary,
                    "definition_of_done": list(planned.definition_of_done),
                },
            })
    return followup_row, item_rows


def reconcile_follow_up(cfg: config.Config, repo: config.Repo, comment_row: dict, *,
                        tk: tracker.Tracker | None = None,
                        original_ticket: tracker.Ticket | None = None,
                        analyzer: Analyzer | None = None,
                        open_checker: OpenChecker | None = None) -> FollowUpResult:
    """Reconcile one completed-ticket comment into linked follow-up(s) and a single requester reply."""
    tid = str(comment_row["ticket"])
    cid = str(comment_row["comment_id"])
    tk = tk or tracker.for_ticket(cfg, repo, tid)
    followup_id = "fup-" + ticket_feedback.digest([repo.name, tid, tk.name, cid])[:16]

    with util.lock(f"ticket-followup-{followup_id}", wait=True):
        existing = feedback_store.get_followup(followup_id)
        if existing and existing["status"] == "done":
            return _to_result(existing, feedback_store.followup_items(followup_id), replayed=True)

        if existing is None:
            delivered = delivered_outcome(cfg, repo, tk, tid, ticket=original_ticket)
            feedback_text = (comment_row.get("data") or {}).get("combined_instruction") or comment_row.get("body") or ""
            planned_list = analyze_requests(feedback_text, delivered, analyzer=analyzer)
            if not planned_list:
                raise ValueError(f"no actionable follow-up requests found in comment {cid} on {tid}")
            f_row, i_rows = _build_plan_rows(
                followup_id, repo, tk, delivered, comment_row, planned_list, open_checker=open_checker,
            )
            followup, items = feedback_store.plan_followup(f_row, i_rows)
        else:
            followup = existing
            items = feedback_store.followup_items(followup_id)

        if followup["status"] == "planned":
            feedback_store.update_followup(followup_id, status="executing")
            followup["status"] = "executing"

        by_id = {i["id"]: i for i in items}
        for idx, item in enumerate(items):
            dep_ids = [d for d in (item.get("depends_on") or "").split(",") if d]
            dep_items = [by_id[d] for d in dep_ids if d in by_id]
            if item["action"] == "create":
                if item["status"] in ("pending", "creating") or not item.get("target_url"):
                    if item["status"] == "pending":
                        feedback_store.update_followup_item(item["id"], status="creating")
                        item["status"] = "creating"
                    target_tid, target_ref, target_url = _create_item_ticket(cfg, repo, tk, item, dep_items)
                    feedback_store.update_followup_item(
                        item["id"],
                        target_ticket=target_tid,
                        target_ref=target_ref,
                        target_url=target_url,
                        status="created",
                    )
                    item.update(target_ticket=target_tid, target_ref=target_ref, target_url=target_url, status="created")
                if not item.get("linked"):
                    if item["status"] != "linking":
                        feedback_store.update_followup_item(item["id"], status="linking")
                        item["status"] = "linking"
                    _ensure_backlink(repo, tk, followup, item, dep_items)
                    feedback_store.update_followup_item(item["id"], linked=True, status="linked")
                    item.update(linked=True, status="linked")
            elif item["action"] == "append":
                if item["status"] in ("pending", "appending") or not item.get("evidence_url"):
                    if item["status"] == "pending":
                        feedback_store.update_followup_item(item["id"], status="appending")
                        item["status"] = "appending"
                    ev_text = _format_evidence_comment(followup, item, comment_row)
                    ev_url = _post_marked_comment(repo, tk, item["target_ticket"], ev_text, item["marker"])
                    feedback_store.update_followup_item(
                        item["id"],
                        evidence_url=ev_url,
                        linked=True,
                        status="appended",
                    )
                    item.update(evidence_url=ev_url, linked=True, status="appended")
            items[idx] = item
            by_id[item["id"]] = item

        # All items exist/updated and linked -> post ONE complete plain-language reply on original ticket.
        reply_body = followup.get("reply_body") or format_requester_reply(items)
        if followup["status"] != "replying" or not followup.get("reply_body"):
            feedback_store.update_followup(followup_id, status="replying", reply_body=reply_body)
            followup.update(status="replying", reply_body=reply_body)

        reply_url = followup.get("reply_url") or _post_marked_comment(
            repo, tk, tid, reply_body, followup["reply_marker"],
        )
        summary_items = [
            {
                "item_id": i["id"],
                "kind": i["kind"],
                "action": i["action"],
                "outcome_label": i["outcome_label"],
                "target_ticket": i["target_ticket"],
                "target_ref": i["target_ref"],
                "target_url": i["target_url"],
                "evidence_url": i.get("evidence_url", ""),
            }
            for i in items
        ]
        completed = feedback_store.complete_followup(
            followup_id,
            reply_url,
            reply_body,
            comment_data={"followup_id": followup_id, "reply_url": reply_url, "items": summary_items},
        )
        final_items = feedback_store.followup_items(followup_id)
        return _to_result(completed or {**followup, "status": "done", "reply_url": reply_url}, final_items, replayed=False)


def sync(cfg: config.Config, *, analyzer: Analyzer | None = None) -> list[FollowUpResult]:
    """Consume pending completed-ticket follow-ups and reconcile them across configured trackers."""
    if not feedback_store.followups_installed():
        return []
    results: list[FollowUpResult] = []
    for row in feedback_store.pending_follow_ups():
        try:
            repo = cfg.repo(row["repo"])
        except (config.UnknownRepo, KeyError):
            continue
        if not repo.pipeline:
            continue
        try:
            results.append(reconcile_follow_up(cfg, repo, row, analyzer=analyzer))
        except (gh.GhError, jira.JiraError, tracker.TaskError, idempotency.Unconfirmed,
                idempotency.InFlight, ValueError, KeyError, OSError) as error:
            print(f"{util.iso()} {tracker.issue_name(repo.name, row['ticket'])}: "
                  f"follow-up reconciliation deferred: {util.first_line(str(error), 300)}")
    return results
