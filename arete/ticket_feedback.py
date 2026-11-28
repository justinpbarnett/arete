"""Contextual ticket-thread feedback consumer, commenter authorization, and intent routing."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from . import (
    adf,
    config,
    gh,
    jira,
    outbound,
    owner_asks,
    paths,
    principals,
    release_proof,
    serve_guard,
    tracker,
    util,
    vault,
)
from .store import asks as ask_store
from .store import db
from .store import feedback as feedback_store
from .store import runs as run_store
from .store import specgraphs
from .store import specs as spec_store
from .store import tasks as tasks_store

Intent = Literal["correction", "product_question", "production_instruction", "neutral"]

CLARIFY_MARKER_PREFIX = "arete-clarify-"
SHA_RE = re.compile(r"\b([a-f0-9]{40}(?:[a-f0-9]{24})?)\b", re.I)
APPROVE_SHA_RE = re.compile(r"^\s*promote\s+([a-f0-9]{40}(?:[a-f0-9]{24})?)\s+to\s+production[.!]?\s*$", re.I)

# Explicit production instructions recognized in context (including QA sheet handoff phrasing).
PRODUCTION_EXPLICIT_PATTERNS = (
    re.compile(r"\bpromote\s+(?:(?:this|it|the\s+(?:change|fix|revision|build|branch|pr)|[a-f0-9]{40}(?:[a-f0-9]{24})?)\s+)?to\s+prod(?:uction)?\b", re.I),
    re.compile(r"\b(?:push|deploy|ship|release)\s+(?:(?:this|it|the\s+(?:change|fix|revision|build|branch|pr))\s+)?to\s+prod(?:uction)?\b", re.I),
    re.compile(r"\bmov(?:ing|ed)\s+(?:this|it)\s+to\s+(?:push\s+to\s+)?prod(?:uction)?(?:\s+on\s+the\s+sheet)?\b", re.I),
    re.compile(r"\b(?:approved?|ready|good|clear)\s+(?:to\s+(?:push|promote|deploy|ship|release)\s+(?:(?:this|it)\s+)?to|for)\s+prod(?:uction)?\b", re.I),
    re.compile(r"\bgo\s+ahead\s+(?:and\s+)?(?:push|promote|deploy|ship|release)(?:\s+(?:this|it))?\s+to\s+prod(?:uction)?\b", re.I),
)

RELEASE_OWNER_APPROVAL_PATTERNS = (
    re.compile(r"\b(?:approve[ds]?|sign(?:ed)?\s+off(?:\s+on)?)\s+(?:the\s+)?(?:irreversible\s+)?(?:release|promotion|prod(?:uction)?\s+release)\b", re.I),
    re.compile(r"\b(?:release|promotion|irreversible\s+action)\s+(?:is\s+)?approved\b", re.I),
    re.compile(r"\bapproved\s+for\s+(?:release|promotion)\b", re.I),
    re.compile(r"\brelease[- ]owner\s+(?:approved|sign[- ]off)\b", re.I),
)

PRODUCTION_BLOCKING_RE = re.compile(
    r"\b(?:don['’]?t|do\s+not|not\s+(?:approved?|ready|good|clear|yet|now)|no\s+go|"
    r"cannot|can['’]?t|shouldn['’]?t|won['’]?t|hold\s+(?:off|on)|hold\b|wait\s+(?:to|before|on)|"
    r"before\s+(?:we|you|pushing|promoting|deploying|shipping)|stop|pause|cancel|block|never)\b",
    re.I,
)

PRODUCTION_CONDITIONAL_RE = re.compile(
    r"(?:^\s*(?:can|could|should|would|shall|do|does|did|is|are|will)\s+(?:we|you|i|this|it)\b|"
    r"\b(?:if|unless|maybe|might|pending|assuming)\b|"
    r"\b(?:once|when|after)\s+(?:we|you|the|it|this|that|ci|tests?|checks?)\b|"
    r"\bas\s+soon\s+as\b)",
    re.I,
)

CHANGE_CONJUNCTION_RE = re.compile(
    r"\b(?:but|except|however|although|though|first\s+(?:fix|change|update)|after\s+(?:fixing|changing|updating)|"
    r"still\s+(?:broken|fails?|failing|loses?|missing|wrong|clears?|resets?)|please\s+(?:fix|change|update|add|remove|keep|preserve))\b",
    re.I,
)

# Pure acknowledgments that must never trigger work or alter approvals.
NEUTRAL_PHRASES = frozenset({
    "thanks", "thank you", "thanks a lot", "thanks so much", "many thanks", "thx", "ty",
    "cheers", "appreciated", "much appreciated",
    "looks good", "looks good to me", "lgtm", "looks great", "sounds good", "sounds great",
    "works for me", "working great", "all good", "nice", "nice work", "great", "great work",
    "awesome", "perfect", "cool", "ok", "okay", "got it", "ack", "acknowledged", "noted",
    "understood", "makes sense", "+1", "verified", "tested", "tested on staging",
    "verified on staging", "looks good on staging", "thanks for the update", "thank you for the update",
    "thanks for fixing", "thank you for fixing", "thanks for shipping", "thank you for shipping",
    "thanks for this", "thank you for this",
})

ACTION_SIGNAL_RE = re.compile(
    r"\b(?:fix|change|update|add|remove|delete|rename|keep|preserve|prevent|allow|show|hide|disable|enable|"
    r"return|include|exclude|sort|filter|format|validate|broken|bug|error|fail(?:s|ed|ing)?|crash(?:es|ed|ing)?|"
    r"lose[sd]?|losing|missing|wrong|instead|should|need(?:s)?|must|cannot|can['’]?t|doesn['’]?t|don['’]?t|won['’]?t|"
    r"still|reset(?:s|ting)?|clear(?:s|ed|ing)?)\b",
    re.I,
)

POLITE_REQUEST_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can|could|would|will)\s+(?:you|we|arete)\s+"
    r"(?:please\s+)?(?:fix|change|update|add|remove|delete|rename|keep|preserve|make|show|hide|move|set|use|include|exclude|return|revert)\b",
    re.I,
)

QUESTION_LEAD_RE = re.compile(
    r"^\s*(?:quick\s+question\s*[:,-]?\s*)?(?:should|what|which|how|why|when|where|does|do|is|are|will|can\s+you\s+clarify|could\s+you\s+clarify)\b",
    re.I,
)

AMBIGUITY_PATTERNS = (
    re.compile(r"\bnot\s+sure\s+(?:whether|if|which)\b", re.I),
    re.compile(r"\bunclear\s+(?:whether|if|which|what)\b", re.I),
    re.compile(r"\b(?:whichever|either\s+way|either\s+one)\b", re.I),
    re.compile(r"\b(?:tbd|to\s+be\s+decided|needs?\s+(?:a\s+)?product\s+decision|ambiguous)\b", re.I),
    re.compile(r"\bdecide\s+whether\b", re.I),
    re.compile(r"\bor\s+should\s+(?:it|we|this)\b", re.I),
)
_EITHER_RE = re.compile(r"\beither\b", re.I)
_OR_RE = re.compile(r"\bor\b", re.I)
_DECIDE_RE = re.compile(r"\b(?:whichever|not\s+sure|up\s+to\s+you|let\s+me\s+know|decide|which)\b", re.I)

RELAY_BODY_RE = re.compile(
    r"^\s*(?:\[(?:slack|teams|discord|email|relay|zendesk|intercom|support|bridge)[^\]]*\]|"
    r"(?:relayed|forwarded|posted)\s+(?:from|via|by)\s+[^:\n]{1,60}:|"
    r"from\s+(?:slack|teams|discord|email|#[a-z0-9_-]+)[^:\n]{0,60}:)",
    re.I,
)

EMOJI_STRIP_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]+")
MENTION_LEAD_RE = re.compile(r"^(?:@[A-Za-z0-9_.-]+[,:]?\s+)+")


@dataclass(frozen=True)
class ThreadComment:
    comment_id: str
    author: str
    author_display: str
    body: str
    url: str
    created: str
    updated: str = ""
    relayed_by: str = ""
    is_bot: bool = False


@dataclass(frozen=True)
class ThreadContext:
    repo: str
    ticket: str
    tracker: str
    title: str = ""
    body: str = ""
    ticket_open: bool = True
    is_spec: bool = False
    parent_ticket: str = ""
    staged: bool = False
    staged_revision: str = ""
    delivered: bool = False
    promotion_phase: str = ""
    qa_handoff_at: str = ""
    qa_handoff_comment_id: str = ""
    open_clarifications: tuple[dict, ...] = ()
    prior_comments: tuple[ThreadComment, ...] = ()


@dataclass(frozen=True)
class AuthDecision:
    authorized: bool
    reason: str
    relayed: bool = False


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    reason: str
    summary: str = ""
    ambiguous: bool = False
    clarifying_question: str = ""
    production_revision: str = ""
    answers_clarification_id: str = ""


@dataclass(frozen=True)
class FeedbackOutcome:
    repo: str
    ticket: str
    tracker: str
    comment_id: str
    authorized: bool
    intent: Intent
    route: str
    status: str
    reason: str
    acted: bool = False
    replayed: bool = False
    clarification_id: str = ""
    clarification_url: str = ""
    steered_run_id: str = ""


Classifier = Callable[[ThreadComment, ThreadContext], IntentResult | None]


def digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def parse_timestamp(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_arete_comment(body: str) -> bool:
    text = body or ""
    if tracker.from_arete(text) or gh.has_arete_mark(text):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return any(line.startswith(CLARIFY_MARKER_PREFIX) for line in lines)


def github_commenter_allowlist(repo: config.Repo) -> tuple[str, ...]:
    return repo.github_commenters()


def authorize_comment(repo: config.Repo, tracker_name: str, comment: ThreadComment, *,
                      extra_allowed: tuple[str, ...] | list[str] = ()) -> AuthDecision:
    if comment.is_bot or is_arete_comment(comment.body):
        return AuthDecision(False, "arete bot comment")
    if not comment.body.strip():
        return AuthDecision(False, "empty comment")
    if tracker_name == "github":
        allowlist = {login.casefold() for login in github_commenter_allowlist(repo)}
        allowlist.update(login.strip().casefold() for login in extra_allowed if isinstance(login, str) and login.strip())
        actor = (comment.author or "").strip()
        if not actor:
            return AuthDecision(False, "missing GitHub commenter login")
        if actor.casefold() in allowlist:
            return AuthDecision(True, f"allowlisted GitHub commenter ({actor})")
        return AuthDecision(False, f"outside GitHub commenter ({actor}) is not in allowlist")
    if tracker_name == "jira":
        who = comment.author_display or comment.author or "team member"
        if comment.relayed_by:
            return AuthDecision(True, f"relayed Jira team feedback ({comment.relayed_by})", relayed=True)
        return AuthDecision(True, f"Jira team commenter ({who})")
    if tracker_name == "local":
        if comment.author != "arete" and principals.is_person(comment.author):
            return AuthDecision(True, f"local principal ({comment.author})")
        return AuthDecision(False, f"non-person local commenter ({comment.author})")
    return AuthDecision(False, f"unsupported tracker ({tracker_name})")


def _strip_relay_prefix(text: str) -> str:
    return RELAY_BODY_RE.sub("", text.strip(), count=1).strip()


def _normalize_neutral(text: str) -> str:
    cleaned = _strip_relay_prefix(text)
    cleaned = MENTION_LEAD_RE.sub("", cleaned)
    cleaned = EMOJI_STRIP_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def is_neutral_acknowledgment(body: str) -> bool:
    text = _normalize_neutral(body)
    if not text:
        return True
    if "?" in text:
        return False
    if CHANGE_CONJUNCTION_RE.search(text) or ACTION_SIGNAL_RE.search(text):
        return False
    if match_production_instruction(text)[0]:
        return False
    segments = [seg.strip(" .!,-;:") for seg in re.split(r"[.!,\n;]+", text) if seg.strip(" .!,-;:")]
    if not segments:
        return True
    return all(seg.casefold() in NEUTRAL_PHRASES for seg in segments)


def match_production_instruction(body: str) -> tuple[bool, str]:
    text = MENTION_LEAD_RE.sub("", _strip_relay_prefix(body)).strip()
    if not text:
        return False, ""
    if m := APPROVE_SHA_RE.fullmatch(text):
        return True, m[1].lower()
    if "?" in text:
        return False, ""
    if PRODUCTION_BLOCKING_RE.search(text) or CHANGE_CONJUNCTION_RE.search(text) or PRODUCTION_CONDITIONAL_RE.search(text):
        return False, ""
    for pattern in PRODUCTION_EXPLICIT_PATTERNS:
        if pattern.search(text):
            sha = SHA_RE.search(text)
            return True, sha[1].lower() if sha else ""
    return False, ""


def match_release_owner_approval(body: str) -> tuple[bool, str]:
    matched, sha = match_production_instruction(body)
    if matched:
        return True, sha
    text = MENTION_LEAD_RE.sub("", _strip_relay_prefix(body)).strip()
    if not text or "?" in text:
        return False, ""
    if PRODUCTION_BLOCKING_RE.search(text) or CHANGE_CONJUNCTION_RE.search(text) or PRODUCTION_CONDITIONAL_RE.search(text):
        return False, ""
    for pattern in RELEASE_OWNER_APPROVAL_PATTERNS:
        if pattern.search(text):
            found = SHA_RE.search(text)
            return True, found[1].lower() if found else ""
    return False, ""


def _comment_id_from_url(url: str) -> str:
    match = re.search(r"(?:issuecomment-|focusedCommentId=)([0-9]+)(?:$|[&#])", url or "")
    return match[1] if match else ""


def _after_clarification(comment: ThreadComment, clarification: dict) -> bool:
    raw_cid = str(comment.comment_id)
    if raw_cid == str(clarification.get("source_comment_id") or ""):
        return False
    clarify_cid = _comment_id_from_url(clarification.get("comment_url", ""))
    if clarify_cid.isascii() and clarify_cid.isdigit() and raw_cid.isascii() and raw_cid.isdigit():
        return int(raw_cid) > int(clarify_cid)
    source_created = (clarification.get("data") or {}).get("source_created", "")
    comment_ts = parse_timestamp(comment.created)
    source_ts = parse_timestamp(source_created)
    if comment_ts and source_ts:
        return comment_ts > source_ts
    asked_ts = parse_timestamp(clarification.get("asked_at", ""))
    if comment_ts and asked_ts:
        return comment_ts >= asked_ts
    return True


def _is_direct_question(text: str) -> bool:
    stripped = MENTION_LEAD_RE.sub("", _strip_relay_prefix(text)).strip()
    if not stripped:
        return False
    if POLITE_REQUEST_RE.search(stripped):
        return False
    if "?" in stripped:
        return True
    return bool(QUESTION_LEAD_RE.search(stripped)) and not ACTION_SIGNAL_RE.search(stripped)


def _has_either_or_ambiguity(text: str) -> bool:
    for line in text.splitlines():
        if m1 := _EITHER_RE.search(line):
            if m2 := _OR_RE.search(line, m1.end()):
                if _DECIDE_RE.search(line, m2.end()):
                    return True
    return False


def _detect_ambiguity(text: str) -> bool:
    stripped = _strip_relay_prefix(text)
    if not stripped:
        return False
    return _has_either_or_ambiguity(stripped) or any(pattern.search(stripped) for pattern in AMBIGUITY_PATTERNS)


def build_clarifying_question(comment: ThreadComment, ctx: ThreadContext) -> str:
    excerpt = util.first_line(_strip_relay_prefix(comment.body), 220)
    subject = f"on {ctx.ticket}" if ctx.ticket else "on this ticket"
    return (
        f"Before changing behavior {subject}, could you clarify the intended product behavior for: "
        f'"{excerpt}"?\n\n'
        "Please reply in this thread with the exact outcome you want so work can continue."
    )


def classify_intent(comment: ThreadComment, ctx: ThreadContext, *,
                    classifier: Classifier | None = None) -> IntentResult:
    if classifier is not None:
        custom = classifier(comment, ctx)
        if custom is not None:
            return custom

    text = _strip_relay_prefix(comment.body)
    if not text:
        return IntentResult("neutral", "empty comment")

    # 1. Check if this authorized comment answers an open product clarification in the same thread.
    open_list = [c for c in ctx.open_clarifications if _after_clarification(comment, c)]
    if open_list:
        if is_neutral_acknowledgment(text):
            return IntentResult("neutral", "non-actionable acknowledgment while clarification remains open")
        matched_prod, prod_rev = match_production_instruction(text)
        if matched_prod:
            return IntentResult("production_instruction", "explicit production direction in ticket thread",
                                summary=util.first_line(text, 240),
                                production_revision=prod_rev or ctx.staged_revision)
        if not _is_direct_question(text) and not _detect_ambiguity(text):
            target = open_list[-1]
            return IntentResult(
                "correction",
                "authorized answer to open product clarification",
                summary=util.first_line(text, 240),
                answers_clarification_id=target["id"],
            )

    # 2. Explicit production direction or release-owner sign-off in context.
    matched_prod, prod_rev = match_production_instruction(text)
    if matched_prod:
        return IntentResult(
            "production_instruction",
            "explicit production direction in ticket thread",
            summary=util.first_line(text, 240),
            production_revision=prod_rev or ctx.staged_revision,
        )
    matched_owner, owner_rev = match_release_owner_approval(text)
    if matched_owner:
        return IntentResult(
            "production_instruction",
            "explicit release-owner approval in ticket thread",
            summary=util.first_line(text, 240),
            production_revision=owner_rev or ctx.staged_revision,
        )

    # 3. Pure neutral acknowledgment (thank-you / looks good / ack).
    if is_neutral_acknowledgment(text):
        return IntentResult("neutral", "non-actionable acknowledgment")

    # 4. Product question or genuine ambiguity about product behavior.
    if _detect_ambiguity(text):
        return IntentResult(
            "product_question",
            "genuine product ambiguity requiring clarification before acting",
            summary=util.first_line(text, 240),
            ambiguous=True,
            clarifying_question=build_clarifying_question(comment, ctx),
        )
    if _is_direct_question(text):
        return IntentResult(
            "product_question",
            "product question in ticket thread",
            summary=util.first_line(text, 240),
            ambiguous=True,
            clarifying_question=build_clarifying_question(comment, ctx),
        )

    # 5. Actionable correction / requested change.
    return IntentResult(
        "correction",
        "actionable requested change in ticket thread",
        summary=util.first_line(text, 240),
    )


def normalize_github_comment(repo: config.Repo, tid: str, row: dict) -> ThreadComment:
    raw_actor = row.get("user") or row.get("author")
    actor = raw_actor if isinstance(raw_actor, dict) else {}
    login = str(actor.get("login") or "").strip()
    raw_body = row.get("body")
    body = raw_body if isinstance(raw_body, str) else ""
    url = str(row.get("html_url") or row.get("url") or "")
    cid = str(row.get("id") or _comment_id_from_url(url) or "")
    if not cid:
        cid = "gh-" + digest([tid, login, str(row.get("created_at") or row.get("createdAt") or ""), body])[:16]
    if not url and cid.isascii() and cid.isdigit() and repo.github:
        url = f"{gh.issue_url(repo.github, tid)}#issuecomment-{cid}"
    created = str(row.get("created_at") or row.get("createdAt") or "")
    updated = str(row.get("updated_at") or row.get("updatedAt") or "")
    return ThreadComment(
        comment_id=cid,
        author=login,
        author_display=login,
        body=body,
        url=url,
        created=created,
        updated=updated,
        is_bot=is_arete_comment(body),
    )


def normalize_jira_comment(tk: tracker.JiraTracker, tid: str, row: dict) -> ThreadComment:
    raw_author = row.get("author")
    author_obj = raw_author if isinstance(raw_author, dict) else {}
    account_id = str(author_obj.get("accountId") or author_obj.get("name") or "").strip()
    display = str(author_obj.get("displayName") or account_id or "").strip()
    raw_body = row.get("body")
    if isinstance(raw_body, dict):
        try:
            body = adf.to_text(raw_body)
        except ValueError:
            body = ""
    elif isinstance(raw_body, str):
        body = raw_body
    else:
        body = ""
    cid = str(row.get("id") or "")
    if not cid:
        cid = "jira-" + digest([tid, account_id, str(row.get("created") or ""), body])[:16]
    url = tk.client.comment_url(tid, cid) if cid else ""
    relayed_by = ""
    if row.get("relayed_by"):
        relayed_by = str(row["relayed_by"])
    elif str(author_obj.get("accountType") or "").casefold() == "app":
        relayed_by = display or account_id or "jira-integration"
    elif RELAY_BODY_RE.match(body or ""):
        relayed_by = display or "team-relay"
    return ThreadComment(
        comment_id=cid,
        author=account_id,
        author_display=display,
        body=body,
        url=url,
        created=str(row.get("created") or ""),
        updated=str(row.get("updated") or ""),
        relayed_by=relayed_by,
        is_bot=is_arete_comment(body),
    )


def fetch_thread_comments(repo: config.Repo, tk: tracker.Tracker, tid: str) -> list[ThreadComment]:
    if isinstance(tk, tracker.GitHubTracker):
        result = gh.gh_json(
            ["api", f"repos/{repo.github}/issues/{int(tid)}/comments?per_page=100", "--paginate", "--slurp"],
            timeout=20,
            retry=False,
        )
        if not isinstance(result, list):
            return []
        rows = [row for page in result for row in (page if isinstance(page, list) else [page]) if isinstance(row, dict)]
        return [normalize_github_comment(repo, str(tid), row) for row in rows]
    if isinstance(tk, tracker.JiraTracker):
        return [normalize_jira_comment(tk, str(tid), row) for row in tk.client.comments(str(tid))]
    if isinstance(tk, tracker.LocalTracker):
        out = []
        for idx, row in enumerate(tasks_store.task_comments(str(tid)), 1):
            body = row.get("body") or ""
            cid = str(row.get("id") or idx)
            author = row.get("author") or ""
            out.append(ThreadComment(
                comment_id=cid,
                author=author,
                author_display=author,
                body=body,
                url=f"local:{tid}#comment-{cid}",
                created=row.get("ts") or "",
                is_bot=(author == "arete") or is_arete_comment(body),
            ))
        return out
    return []


def _staging_state(repo_name: str, tid: str) -> dict:
    try:
        return serve_guard.read(release_proof.state_dir(repo_name, str(tid)) / "promotion.json") or {}
    except (OSError, ValueError):
        return {}


def _parent_ticket_for(repo: config.Repo, tk: tracker.Tracker, tid: str) -> str:
    try:
        from . import spec_tracker
        remote = spec_tracker.adapter(tk)
        if remote and (key := specgraphs.membership(remote.scope, str(tid))):
            return key.parent
    except (OSError, ValueError, KeyError):
        pass
    return ""


def _pickup_time(repo_name: str, tid: str) -> str:
    try:
        runs = run_store.ticket_runs(repo_name, str(tid), limit=50, live=True)
    except (OSError, ValueError, db.NoDatabase):
        return ""
    starts = [r["started"] for r in runs if r.get("started")]
    return min(starts) if starts else ""


def build_context(cfg: config.Config, repo: config.Repo, tk: tracker.Tracker, tid: str, *,
                  ticket: tracker.Ticket | None = None,
                  comments: list[ThreadComment] | None = None) -> ThreadContext:
    tid = str(tid)
    ticket_open = True
    title, body, is_spec = "", "", False
    if ticket is not None:
        title, body = ticket.title, ticket.body
        ticket_open = ticket.open
        is_spec = ticket.is_spec
    else:
        claim = spec_store.find(repo.name, tid)
        if claim and claim.get("state") == "complete":
            ticket_open = False
    promo = _staging_state(repo.name, tid)
    promo_phase = promo.get("phase", "")
    delivered = promo_phase == "production_verified"
    staged = promo_phase in ("observing_staging", "awaiting_qa", "staging_verified", "staged_for_parent", "paused")
    handoff_url = promo.get("handoff", "")
    parent = promo.get("graph_parent", "") or _parent_ticket_for(repo, tk, tid)
    open_clars = tuple(feedback_store.open_clarifications(repo.name, tid))
    return ThreadContext(
        repo=repo.name,
        ticket=tid,
        tracker=tk.name,
        title=title,
        body=body,
        ticket_open=ticket_open,
        is_spec=is_spec,
        parent_ticket=parent,
        staged=staged,
        staged_revision=promo.get("staged_revision") or promo.get("revision", ""),
        delivered=delivered,
        promotion_phase=promo_phase,
        qa_handoff_at=promo.get("qa_since", ""),
        qa_handoff_comment_id=_comment_id_from_url(handoff_url),
        open_clarifications=open_clars,
        prior_comments=tuple(comments or ()),
    )


def _public_safe(repo: config.Repo, text: str) -> str:
    cleaned = outbound.inert(outbound.screen(vault.hidden(text)))
    return outbound.redact(cleaned) if repo.public else cleaned


def _is_trusted_marker_author(tk: tracker.Tracker, item: ThreadComment) -> bool:
    """Only Arete's own tracker identity may reconcile a durable marker."""
    actor = (item.author or "").strip()
    if not actor:
        return False
    if isinstance(tk, tracker.LocalTracker):
        return actor == "arete"
    if isinstance(tk, tracker.JiraTracker):
        try:
            me = str((tk.client.me() or {}).get("accountId") or "").strip()
        except (jira.JiraError, ValueError, OSError):
            return False
        return bool(me) and actor == me
    if isinstance(tk, tracker.GitHubTracker):
        if tk.owner and actor.casefold() == tk.owner.casefold():
            return True
        try:
            me = gh.me()
        except (gh.GhError, OSError):
            return False
        return bool(me) and actor.casefold() == me.casefold()
    return False


def _find_marker_url(repo: config.Repo, tk: tracker.Tracker, tid: str, marker: str,
                     comments: list[ThreadComment] | None = None) -> str:
    rows = comments if comments is not None else fetch_thread_comments(repo, tk, tid)
    for item in rows:
        if _is_trusted_marker_author(tk, item) and marker in (item.body or "").splitlines() and item.url:
            return item.url
    return ""


def _post_clarification(repo: config.Repo, tk: tracker.Tracker, tid: str, comment: ThreadComment,
                        question: str, *, existing_comments: list[ThreadComment] | None = None,
                        dry_run: bool = False) -> tuple[str, str]:
    """Post a focused product clarification question once, reconciling retries via a durable marker."""
    cid = "clarify-" + digest([repo.name, str(tid), tk.name, str(comment.comment_id)])[:16]
    fresh_marker = f"{CLARIFY_MARKER_PREFIX}{cid}-{secrets.token_hex(12)}"
    saved = feedback_store.begin_clarification(
        cid,
        repo.name,
        str(tid),
        tk.name,
        str(comment.comment_id),
        question,
        fresh_marker,
        data={"source_author": comment.author, "source_body": comment.body, "source_created": comment.created},
    )
    marker = saved["marker"]
    if saved["status"] == "open" and saved.get("comment_url"):
        return cid, saved["comment_url"]
    if saved["status"] == "answered":
        return cid, saved.get("comment_url", "")

    # Reconcile if a previous attempt posted the comment but lost the response.
    if found := _find_marker_url(repo, tk, tid, marker, existing_comments):
        feedback_store.mark_clarification_open(cid, found)
        return cid, found

    if dry_run:
        return cid, ""

    body = gh.marked(f"{tracker.ARETE_TAG} {_public_safe(repo, question)}\n\n{marker}")
    url = ""
    try:
        if isinstance(tk, tracker.GitHubTracker):
            url = gh.issue_comment(repo.github, int(tid), body, return_url=True, single_attempt=True) or ""
        elif isinstance(tk, tracker.JiraTracker):
            url = tk.client.comment(str(tid), body, return_url=True, single_attempt=True) or ""
        elif isinstance(tk, tracker.LocalTracker):
            if err := tk.comment(str(tid), body):
                raise tracker.TaskError(err)
            url = f"local:{tid}:{marker}"
    except (gh.GhUnsure, jira.JiraUnsure):
        # Leave status='asking' so the next poll reconciles via marker without double-posting.
        raise
    except (gh.GhError, jira.JiraError, tracker.TaskError):
        feedback_store.abort_clarification(cid)
        raise

    if not url:
        url = _find_marker_url(repo, tk, tid, marker) or f"{tk.name}:{tid}:{marker}"
    feedback_store.mark_clarification_open(cid, url)
    return cid, url


def _format_steer_instruction(comment: ThreadComment, instruction: str) -> str:
    who = comment.author_display or comment.author or "authorized commenter"
    via = f" (via {comment.relayed_by})" if comment.relayed_by else ""
    ref = f" ({comment.url})" if comment.url else ""
    return f"Authorized ticket feedback from {who}{via}{ref}:\n\n{instruction.strip()}"


def steer_active_worker(repo_name: str, tid: str, comment: ThreadComment, instruction: str) -> str:
    """Deliver an authorized correction immediately to a currently running worker on this ticket."""
    try:
        running = run_store.ticket_runs_marked_running(repo_name, str(tid))
    except (OSError, ValueError, db.NoDatabase):
        return ""
    if not running:
        return ""
    run_id = running[0]["id"]
    ask_store.add_message(run_id, _format_steer_instruction(comment, instruction))
    return run_id


def _resolve_open_worker_asks(repo_name: str, tid: str, answer_text: str) -> bool:
    """If a needs-info run is waiting on non-secret asks, close them with an authorized thread answer."""
    try:
        newest = run_store.newest_live_whole_run(repo_name, str(tid))
    except (OSError, ValueError, db.NoDatabase):
        return False
    if not newest or newest["status"] != "needs-info":
        return False
    run_id = newest["id"]
    open_asks = ask_store.asks_of(run_id, "open")
    if not open_asks or any(owner_asks.has_secret(a["questions"]) for a in open_asks):
        return False
    with util.lock(owner_asks.STEER_LOCK, wait=True), db.tx():
        current = run_store.newest_live_whole_run(repo_name, str(tid))
        if not current or current["id"] != run_id or current["status"] != "needs-info":
            return False
        current_open = ask_store.asks_of(run_id, "open")
        if not current_open or any(owner_asks.has_secret(a["questions"]) for a in current_open):
            return False
        for ask in current_open:
            answers = owner_asks.text_answers(owner_asks.unstaged(ask), answer_text)
            ask_store.ask_close(ask["id"], "answered", owner_asks.combine_staged(ask, answers))
    return True


def process_comment(cfg: config.Config, repo: config.Repo, tk: tracker.Tracker,
                    comment: ThreadComment, ctx: ThreadContext, *,
                    classifier: Classifier | None = None, dry_run: bool = False) -> FeedbackOutcome:
    """Authorize, classify, durably record, and route a single ticket-thread comment (replay-safe)."""
    tid = str(ctx.ticket)
    existing = feedback_store.get_comment(repo.name, tid, tk.name, comment.comment_id)
    if existing is not None:
        # Reconcile an in-flight clarification whose tracker confirmation was lost.
        if existing["status"] == "asking" and existing["route"] == "clarify":
            cid = existing.get("action_id", "")
            clar = feedback_store.get_clarification(cid) if cid else None
            question = (clar or {}).get("question") or build_clarifying_question(comment, ctx)
            cid, url = _post_clarification(repo, tk, tid, comment, question,
                                           existing_comments=list(ctx.prior_comments) or None, dry_run=dry_run)
            if url and not dry_run:
                feedback_store.update_comment(repo.name, tid, tk.name, comment.comment_id,
                                              status="waiting_answer", action_id=cid)
                return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "product_question",
                                       "clarify", "waiting_answer", "reconciled pending product clarification",
                                       acted=True, replayed=False, clarification_id=cid, clarification_url=url)
        return FeedbackOutcome(
            repo.name, tid, tk.name, comment.comment_id,
            authorized=bool(existing["authorized"]),
            intent=existing["intent"],
            route=existing["route"],
            status=existing["status"],
            reason="already processed (replay no-op)",
            acted=False,
            replayed=True,
            clarification_id=existing.get("action_id", "") if existing.get("route") == "clarify" else "",
            steered_run_id=existing.get("run_id", ""),
        )

    extra_allowed: tuple[str, ...] = ()
    try:
        if policy := release_proof.goal(repo.name, tid):
            extra_allowed = tuple(policy.get("qa") or ()) + tuple(policy.get("release_owners") or ())
    except (OSError, ValueError):
        extra_allowed = ()
    auth = authorize_comment(repo, tk.name, comment, extra_allowed=extra_allowed)
    ticket_state = "closed" if not ctx.ticket_open else ("delivered" if ctx.delivered else "open")
    base_row = {
        "repo": repo.name,
        "ticket": tid,
        "tracker": tk.name,
        "comment_id": comment.comment_id,
        "author": comment.author,
        "author_display": comment.author_display,
        "relayed_by": comment.relayed_by,
        "body": comment.body,
        "url": comment.url,
        "created": comment.created,
        "updated": comment.updated,
        "ticket_state": ticket_state,
        "authorized": auth.authorized,
        "auth_reason": auth.reason,
        "parent_ticket": ctx.parent_ticket,
    }

    if not auth.authorized:
        if not dry_run:
            feedback_store.record_comment({**base_row, "intent": "neutral", "route": "none", "status": "ignored"})
            if auth.reason != "arete bot comment":
                print(f"{util.iso()} {tracker.issue_name(repo.name, tid)}: ignored comment {comment.comment_id}: {auth.reason}")
        return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, False, "neutral", "none", "ignored",
                               auth.reason, acted=False, replayed=False)

    classified = classify_intent(comment, ctx, classifier=classifier)

    # Neutral acknowledgment: no-op, preserves existing approvals.
    if classified.intent == "neutral":
        if not dry_run:
            feedback_store.record_comment({**base_row, "intent": "neutral", "route": "none", "status": "noop",
                                           "data": {"reason": classified.reason}})
        return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "neutral", "none", "noop",
                               classified.reason, acted=False, replayed=False)

    # Explicit production direction: make available to the release workflow.
    if classified.intent == "production_instruction":
        data = {
            "reason": classified.reason,
            "summary": classified.summary,
            "revision": classified.production_revision,
            "staged": ctx.staged,
        }
        if not dry_run:
            feedback_store.record_comment({**base_row, "intent": "production_instruction", "route": "release",
                                           "status": "routed", "data": data})
        return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "production_instruction",
                               "release", "routed", classified.reason, acted=True, replayed=False)

    # Product question / genuine ambiguity: ask a focused question in the same thread and wait.
    if classified.intent == "product_question":
        question = classified.clarifying_question or build_clarifying_question(comment, ctx)
        if dry_run:
            return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "product_question",
                                   "clarify", "waiting_answer", classified.reason, acted=True, replayed=False)
        cid_preview = "clarify-" + digest([repo.name, tid, tk.name, str(comment.comment_id)])[:16]
        inserted = feedback_store.record_comment({
            **base_row,
            "intent": "product_question",
            "route": "clarify",
            "status": "asking",
            "action_id": cid_preview,
            "data": {"reason": classified.reason, "question": question, "ambiguous": classified.ambiguous},
        })
        if not inserted:
            return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "product_question",
                                   "clarify", "waiting_answer", "already recorded (replay no-op)",
                                   acted=False, replayed=True, clarification_id=cid_preview)
        cid, url = _post_clarification(repo, tk, tid, comment, question,
                                       existing_comments=list(ctx.prior_comments) or None, dry_run=False)
        feedback_store.update_comment(repo.name, tid, tk.name, comment.comment_id,
                                      status="waiting_answer", action_id=cid,
                                      data={"reason": classified.reason, "question": question,
                                            "ambiguous": classified.ambiguous, "clarification_url": url})
        return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "product_question",
                               "clarify", "waiting_answer", classified.reason, acted=True, replayed=False,
                               clarification_id=cid, clarification_url=url)

    # Actionable correction / requested change.
    # If this answers an open clarification, resolve it and combine original request + authorized answer.
    combined_instruction = comment.body.strip()
    extra_data: dict = {"reason": classified.reason, "summary": classified.summary}
    resolved_clar_id = classified.answers_clarification_id
    if resolved_clar_id:
        clar = feedback_store.get_clarification(resolved_clar_id)
        if clar:
            orig_body = (clar.get("data") or {}).get("source_body", "")
            combined_instruction = (
                f"Original feedback: {orig_body}\n\n"
                f"Clarification asked: {clar['question']}\n\n"
                f"Authorized answer from {comment.author_display or comment.author}: {comment.body.strip()}"
            )
            extra_data.update(clarification_id=resolved_clar_id, original_comment_id=clar["source_comment_id"],
                              question=clar["question"], combined_instruction=combined_instruction)

    # Release already in progress -> reconcile actual deployment outcome first before routing.
    if ctx.promotion_phase in ("promotion_intent", "observing_production"):
        return FeedbackOutcome(
            repo.name, tid, tk.name, comment.comment_id, True, "correction",
            "reconcile_release", "deferred",
            "release already in progress; reconcile actual deployment outcome first",
            acted=False, replayed=False, clarification_id=resolved_clar_id,
        )

    # Closed or confirmed-delivered ticket -> route to follow-up workflow (keeps original ticket closed/delivered).
    if not ctx.ticket_open or ctx.delivered:
        if not dry_run:
            inserted = feedback_store.record_comment({
                **base_row,
                "intent": "correction",
                "route": "follow_up",
                "status": "follow_up_pending",
                "action_id": resolved_clar_id,
                "data": extra_data,
            })
            if not inserted:
                return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "correction",
                                       "follow_up", "follow_up_pending", "already recorded (replay no-op)",
                                       acted=False, replayed=True)
            if resolved_clar_id:
                feedback_store.resolve_clarification(resolved_clar_id, comment.comment_id,
                                                     comment.author_display or comment.author, comment.body)
                if clar := feedback_store.get_clarification(resolved_clar_id):
                    feedback_store.update_comment(repo.name, tid, tk.name, clar["source_comment_id"], status="answered")
        return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "correction",
                               "follow_up", "follow_up_pending",
                               "actionable request on completed ticket queued for follow-up workflow",
                               acted=True, replayed=False, clarification_id=resolved_clar_id)

    # Open ticket (active spec, active ticket, or staged child) -> steer running worker or queue for worker pickup.
    if dry_run:
        return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "correction",
                               "worker", "queued", classified.reason, acted=True, replayed=False,
                               clarification_id=resolved_clar_id)

    inserted = feedback_store.record_comment({
        **base_row,
        "intent": "correction",
        "route": "worker",
        "status": "queued",
        "action_id": resolved_clar_id,
        "data": extra_data,
    })
    if not inserted:
        return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "correction",
                               "worker", "queued", "already recorded (replay no-op)",
                               acted=False, replayed=True)

    if resolved_clar_id:
        feedback_store.resolve_clarification(resolved_clar_id, comment.comment_id,
                                             comment.author_display or comment.author, comment.body)
        if clar := feedback_store.get_clarification(resolved_clar_id):
            feedback_store.update_comment(repo.name, tid, tk.name, clar["source_comment_id"], status="answered")

    # If a staging promotion can still safely pause, pause it and reset prior QA approval for the new revision.
    if ctx.promotion_phase in ("awaiting_qa", "staging_verified", "staged_for_parent"):
        from . import release_promotion
        release_promotion.pause_for_feedback(
            repo.name, tid,
            reason="paused: the tested behavior changed; returning to staging for fresh QA",
            comment_id=comment.comment_id,
        )

    # Also check if a needs-info worker run is waiting on non-secret asks and can resume directly.
    resumed_ask = _resolve_open_worker_asks(repo.name, tid, combined_instruction)
    steered_run = steer_active_worker(repo.name, tid, comment, combined_instruction)
    if steered_run:
        feedback_store.update_comment(repo.name, tid, tk.name, comment.comment_id,
                                      status="steered", run_id=steered_run)
        return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "correction",
                               "worker", "steered", f"steered active worker run {steered_run}",
                               acted=True, replayed=False, clarification_id=resolved_clar_id,
                               steered_run_id=steered_run)

    status = "queued"
    reason = "answered waiting worker question and queued continuation" if resumed_ask else classified.reason
    return FeedbackOutcome(repo.name, tid, tk.name, comment.comment_id, True, "correction",
                           "worker", status, reason, acted=True, replayed=False,
                           clarification_id=resolved_clar_id)


def consume_ticket(cfg: config.Config, repo: config.Repo, tid: str, *,
                   tk: tracker.Tracker | None = None,
                   ticket: tracker.Ticket | None = None,
                   comments: list[ThreadComment] | None = None,
                   since: str | None = None,
                   classifier: Classifier | None = None,
                   dry_run: bool = False) -> list[FeedbackOutcome]:
    """Process all new post-pickup comments on a ticket thread in chronological order."""
    tid = str(tid)
    tk = tk or tracker.for_ticket(cfg, repo, tid)
    if comments is None:
        comments = fetch_thread_comments(repo, tk, tid)
    cutoff_str = _pickup_time(repo.name, tid) if since is None else since
    cutoff = parse_timestamp(cutoff_str) if cutoff_str else None

    outcomes: list[FeedbackOutcome] = []
    for item in comments:
        item_ts = parse_timestamp(item.created)
        if cutoff is not None and item_ts is not None and item_ts <= cutoff:
            continue
        ctx = build_context(cfg, repo, tk, tid, ticket=ticket, comments=comments)
        outcome = process_comment(cfg, repo, tk, item, ctx, classifier=classifier, dry_run=dry_run)
        outcomes.append(outcome)
    return outcomes


def watched_tickets(cfg: config.Config, repo: config.Repo) -> list[str]:
    """Return active specs, staged/active children, active tickets, and completed tickets to watch."""
    found: list[str] = []
    seen: set[str] = set()

    def add(tid) -> None:
        s = str(tid or "").strip()
        if s and s not in seen:
            seen.add(s)
            found.append(s)

    if not paths.DB_FILE.exists():
        return []
    try:
        with db.reader() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'spec_deliveries' AND type = 'table'").fetchone():
                for row in c.execute("SELECT issue FROM spec_deliveries WHERE repo = ?", (repo.name,)):
                    add(row["issue"])
            if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'spec_graphs' AND type = 'table'").fetchone():
                for row in c.execute(
                    "SELECT g.parent, n.ticket FROM spec_graphs g "
                    "LEFT JOIN spec_graph_children n ON n.scope = g.scope AND n.parent = g.parent "
                    "WHERE g.repo = ?",
                    (repo.name,),
                ):
                    add(row["parent"])
                    if row["ticket"] and "#" not in str(row["ticket"]):
                        add(row["ticket"])
            for row in c.execute(
                "SELECT DISTINCT issue FROM runs WHERE kind = 'pipeline' AND repo = ? AND dry_run = 0 "
                "AND COALESCE(issue, '') != '' AND status IN ('running', 'pr', 'needs-info', 'merged', 'done') "
                "ORDER BY started DESC LIMIT 200",
                (repo.name,),
            ):
                add(row["issue"])
    except (db.NoDatabase, OSError, ValueError):
        return []
    for tid in release_proof.enrolled(repo.name):
        add(tid)
    return found


def sync(cfg: config.Config, *, classifier: Classifier | None = None) -> list[FeedbackOutcome]:
    """Poll watched ticket threads across enabled repos and route new authorized feedback."""
    if not feedback_store.installed():
        return []
    all_outcomes: list[FeedbackOutcome] = []
    for repo in cfg.repos:
        if not repo.pipeline:
            continue
        for tid in watched_tickets(cfg, repo):
            try:
                tk = tracker.for_ticket(cfg, repo, tid)
                ticket = tk.get(tid)
                all_outcomes.extend(consume_ticket(cfg, repo, tid, tk=tk, ticket=ticket, classifier=classifier))
            except (gh.GhError, jira.JiraError, tracker.TaskError, KeyError, ValueError, OSError) as error:
                print(f"{util.iso()} {tracker.issue_name(repo.name, tid)}: ticket feedback check skipped: "
                      f"{util.first_line(str(error), 300)}")
    return all_outcomes


def worker_instructions_markdown(rows: list[dict]) -> str:
    """Render authorized ticket-thread corrections/clarifications for worker briefs and goals."""
    if not rows:
        return ""
    parts = ["## Authorized Ticket Feedback"]
    for row in rows:
        who = row.get("author_display") or row.get("author") or "authorized commenter"
        via = f" (relayed by {row['relayed_by']})" if row.get("relayed_by") else ""
        when = f" on {row['created'][:10]}" if row.get("created") else ""
        ref = f" ({row['url']})" if row.get("url") else ""
        data = row.get("data") or {}
        text = (data.get("combined_instruction") or row.get("body") or "").strip()
        parts.append(f"### {who}{via}{when}{ref}\n\n{text}")
    return "\n\n".join(parts)


def pending_corrections(repo_name: str, tid: str) -> list[dict]:
    return feedback_store.pending_corrections(repo_name, str(tid))


def production_instructions(repo_name: str, tid: str) -> list[dict]:
    return feedback_store.production_instructions(repo_name, str(tid))


def pending_follow_ups(repo_name: str = "") -> list[dict]:
    return feedback_store.pending_follow_ups(repo_name)
