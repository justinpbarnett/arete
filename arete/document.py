from __future__ import annotations

import difflib
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path
from string import Template

from . import config, outbound, paths, runner, steps, util

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
          "required": ["text"]}
CHECK_SAW_CHARS = 300
TICKET_BODY_CHARS = 4000
VOICE_LINE_CHARS = 400
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
WARNING_LINE = re.compile(r"^\W*(?:warning|caution|danger|important|prerequisites?|requires?|before you start)\b",
                          re.IGNORECASE)
HANDLE = re.compile(r"(?<![\w@.])@[A-Za-z0-9_][\w-]*")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
WEEKDAY = re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE)
MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
         r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
MONTH_DAY = re.compile(rf"\b{MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH}\b",
                       re.IGNORECASE)
RELATIVE_DAY = re.compile(r"\b(?:today|tomorrow|tonight|eod|eow)\b", re.IGNORECASE)
REASONING_TAG = re.compile(r"</?(?:thinking|reasoning|scratchpad)\b", re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
SUPPLIED =("ask", "owner", "deadline")


@dataclass(frozen=True)
class Brief:
    kind: str
    facts: tuple[str, ...]
    action: str
    acceptance: tuple[str, ...] = ()
    audience: str = ""
    channel: str = ""
    draft: str = ""
    ask: str = ""
    owner: str = ""
    deadline: str = ""
    keep: tuple[str, ...] = ()
    drop: tuple[str, ...] = ()


@dataclass(frozen=True)
class Written:
    text: str
    title: str = ""
    source: str = "writer"
    faults: tuple[str, ...] = ()
    words: int = 0
    target: tuple[int, int] | None = None
    tries: int = 0
    kept: float | None = None


def brief_from(data: dict) -> Brief:
    if not isinstance(data, dict):
        raise TypeError("the brief must be one JSON object")
    names = [f.name for f in fields(Brief)]
    if bad := sorted(set(data) - set(names)):
        raise ValueError(f"the brief has an unknown key {', '.join(bad)}; use {', '.join(names)}")
    kind = data.get("kind")
    if kind not in config.DOCUMENT_KINDS:
        raise ValueError(f"the brief's kind {kind!r} is not one of {', '.join(config.DOCUMENT_KINDS)}")
    found = {"kind": kind}
    for name in names[1:]:
        value = data.get(name, () if name in ("facts", "acceptance", "keep", "drop") else "")
        if name in ("facts", "acceptance", "keep", "drop"):
            if isinstance(value, str):
                value = [value]
            if not (isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)):
                raise ValueError(f"the brief's {name} must be a list of strings")
            found[name] = tuple(v for v in (v.strip() for v in value) if v)
        elif not isinstance(value, str):
            raise ValueError(f"the brief's {name} must be a string")
        else:
            found[name] = value.strip() if name != "draft" else value
    if not found["facts"]:
        raise ValueError("the brief has no facts")
    if not found["action"]:
        raise ValueError("the brief has no action")
    return Brief(**found)


def code_in(text: str, *, fenced_only: bool = False, inline_only: bool = False) -> list[str]:
    found, fence = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        marker = re.fullmatch(r"(`{3,}|~{3,})(.*)", stripped)
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                    and not marker[2].strip()):
                fence = ""
            elif stripped and not inline_only:
                found.append(line)
        elif marker and (marker[1][0] == "~" or "`" not in marker[2]):
            fence = marker[1]
        elif not fenced_only:
            found += [m[2].strip() for m in CODE_SPAN.finditer(line) if m[2].strip()]
    return found


def warnings_in(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if WARNING_LINE.match(line)]


def protected(brief: Brief) -> list[str]:
    found = list(brief.keep)
    if brief.kind != "pr":
        for text in (*brief.facts, brief.action, *brief.acceptance, brief.ask):
            found += code_in(text)
        if brief.kind == "doc":
            found += code_in(brief.draft) + warnings_in(brief.draft) + warnings_in("\n".join(brief.facts))
    return [s for s in dict.fromkeys(found) if s not in brief.drop]


def flat(text: str) -> str:
    return " ".join(text.split()).lower()


def supplied_in(value: str, text: str) -> bool:
    return re.search(r"(?<![\w@-])" + re.escape(flat(value)) + r"(?![\w-])", flat(text)) is not None


def brief_text(brief: Brief) -> str:
    return flat("\n".join([*brief.facts, brief.action, *brief.acceptance, brief.audience, brief.channel, brief.draft,
                           brief.ask, brief.owner, brief.deadline, *brief.keep]))


def invented(brief: Brief, text: str) -> list[str]:
    if brief.kind not in ("message", "ticket"):
        return []
    patterns = (HANDLE, ISO_DATE, WEEKDAY, MONTH_DAY, RELATIVE_DAY)
    known = {flat(m.group(0)) for pattern in patterns for m in pattern.finditer(brief_text(brief))}
    tokens = [m.group(0) for pattern in patterns for m in pattern.finditer(text)]
    return [t for t in dict.fromkeys(tokens) if flat(t) not in known]


def voice_lines(files: tuple[str, ...]) -> list[str]:
    found = []
    for name in files:
        found += [line.strip() for line in Path(name).read_text().splitlines()]
    return [line for line in found if line]


def voice_pieces(samples: list[str]) -> list[str]:
    pieces = []
    for line in samples:
        pieces += [line, line[:VOICE_LINE_CHARS], *SENTENCE_END.split(line)]
    return pieces


def plain_code_in(span: str, text: str) -> bool:
    return re.search(r"(?<![\w`/-])" + re.escape(span) + r"(?=[ \t]*(?:$|[.,;:!?](?:\s|$)))",
                     text, re.MULTILINE) is not None


def faults(brief: Brief, text: str, samples: list[str], title: str = "") -> list[str]:
    if not text.strip():
        return ["the answer held no text"]
    whole = f"{title}\n{text}"
    found = [f"dropped `{s}`" for s in protected(brief) if s not in text]
    if brief.kind != "pr":
        sources = (*brief.facts, brief.action, *brief.acceptance, brief.ask,
                   *([brief.draft] if brief.kind == "doc" else []))
        required = {line for source in sources for line in code_in(source, fenced_only=True)} - set(brief.drop)
        said_code = set(code_in(text, fenced_only=True))
        found += [f"changed fenced code line `{line}`" for line in sorted(required - said_code) if line in text]
        required_inline = {span for source in sources for span in code_in(source, inline_only=True)} - set(brief.drop)
        said_inline = set(code_in(text, inline_only=True)) | said_code
        if re.search(r"\b(?:plain[ -]?text|sms)\b", brief.channel or "plain text", re.IGNORECASE):
            said_inline.update(span for span in required_inline if plain_code_in(span, text))
        found += [f"changed inline code span `{span}`" for span in sorted(required_inline - said_inline) if span in text]
    if brief.kind == "message":
        found += [f"left out the {name}, {value}" for name in SUPPLIED if (value := getattr(brief, name))
                  and not supplied_in(value, text)]
    found += [f"names {t}, which the brief does not supply" for t in invented(brief, whole)]
    known = brief_text(brief)
    if any(piece.strip() and supplied_in(piece, whole) and not supplied_in(piece, known)
           for piece in voice_pieces(samples)):
        found.append("repeats a voice example")
    if REASONING_TAG.search(whole):
        found.append("holds a reasoning tag")
    return found


def bullets(items) -> str:
    return "\n".join(f"- {item}" for item in items) or "- none"


def fill(brief: Brief, doc: config.Document, samples: list[str], target: tuple[int, int] | None) -> str:
    supplied = [f"{name}: {getattr(brief, name)}" for name in SUPPLIED if getattr(brief, name)]
    voice = "\n".join(f"- {line[:VOICE_LINE_CHARS]}" for line in samples)
    words = f"about {target[0]} to {target[1]} words" if target else "as short as completeness allows"
    pinned = protected(brief)
    return Template((paths.PROMPTS_DIR / "document.md").read_text()).safe_substitute(
        kind=brief.kind, audience=brief.audience or "the reader", channel=brief.channel or "plain text",
        action=brief.action, facts=bullets(brief.facts), acceptance=bullets(brief.acceptance),
        supplied="\n".join(f"- {s}" for s in supplied) or "- none", draft=brief.draft.strip() or "none",
        voice=voice or "none", style=doc.style or "none", target=words, keep=bullets(pinned) if pinned else "",
        mark=secrets.token_hex(8))


def refusal(problems: list[str]) -> str:
    return ("\n\n## Your last draft was refused\n\n" + "\n".join(f"- {p}" for p in problems)
            + "\n\nReturn the whole text again with these fixed.")


def ask(text: str, s: steps.Settings, cfg: config.Config, repo, layer, run):
    if run is not None:
        cwd = run.dir / "document"
        shutil.rmtree(cwd, ignore_errors=True)
        cwd.mkdir(parents=True)
        return run.agent("document", text, cwd, role="document", json_schema=SCHEMA, persist=False,
                         wait_out_limits=False)
    with tempfile.TemporaryDirectory() as empty:
        return runner.run(text, empty, **s.call, tools=list(s.tools), allowed_tools=list(s.tools),
                          instructions=list(s.instructions), json_schema=SCHEMA, persist=False,
                          wait_out_limits=False, tier=s.tier, trace=("", "document"), cfg=cfg, repo=repo, layer=layer,
                          settings=s)


def measured(text: str, title: str, source: str, problems, target, tries: int, brief: Brief) -> Written:
    kept = difflib.SequenceMatcher(None, brief.draft, text).ratio() if brief.draft else None
    return Written(text, title, source, tuple(problems), len(text.split()), target, tries, kept)


def write(brief: Brief, cfg: config.Config, *, repo=None, layer=None, run=None) -> Written:
    target, n = None, 0
    try:
        s = run.settings("document") if run is not None else steps.resolve(cfg, layer, "document")
        doc = config.document_for(cfg, repo, layer)
        target = doc.words.get(brief.kind)
        samples = voice_lines(doc.voice)
        text, feedback, problems = fill(brief, doc, samples, target), "", []
        for n in range(1, s.tries + 1):
            res = ask(text + feedback, s, cfg, repo, layer, run)
            found = res.structured if res.ok and isinstance(res.structured, dict) else {}
            said = str(found.get("text") or "").strip()
            title = str(found.get("title") or "").strip() if brief.kind == "ticket" else ""
            problems = (faults(brief, said, samples, title) if res.ok
                        else [util.first_line(res.error or "the writer gave no answer")])
            if not problems:
                return measured(said, title, "writer", (), target, n, brief)
            if res.usage_limit:
                break
            feedback = refusal(problems)
    except (OSError, UnicodeError, RuntimeError) as e:
        problems = [f"the writer failed ({type(e).__name__})"]
    if brief.draft.strip():
        return measured(brief.draft, "", "draft", problems, target, n, brief)
    return Written("", "", "fallback", tuple(problems), 0, target, n)


def one_line(text: str, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def pr_brief(ticket, changes: str, review: dict, verified, size: str, held: str) -> Brief:
    checks = [f"Check \"{c.get('what', '')}\": {'passed' if c.get('passed') else 'failed'}; saw: "
              f"{one_line(c.get('saw'), CHECK_SAW_CHARS)}" for c in verified.checks]
    facts = [f"Change: {changes}", f"Size: {size}", f"Checked in use: {verified.verdict}. {verified.summary}".strip(),
             *checks, f"Review: {review.get('verdict', 'none')}. {review.get('summary', '')}".strip(),
             *([f"Held as a draft: {held}"] if held else [])]
    return Brief(
        "pr", tuple(outbound.screen(f) for f in facts if f.strip()),
        outbound.screen(f"Write the summary a reviewer reads before reviewing and merging the change for "
                        f"{ticket.ref}: {ticket.title}\n\nTicket:\n{ticket.body[:TICKET_BODY_CHARS]}"),
        tuple(str(c.get("what", "")) for c in verified.checks if c.get("what")),
        "the repo owner, before merging", "pull request description, markdown")


def pr_text(run, ticket, changes: str, review: dict, verified, size: str, held: str, cfg: config.Config, layer) -> str:
    try:
        if not config.document_for(cfg, run.repo, layer).enabled:
            return ""
        done = write(pr_brief(ticket, changes, review, verified, size, held), cfg, repo=run.repo, layer=layer, run=run)
        if done.source == "writer":
            run.save("document.md", done.text)
            run.log(f"document: wrote the PR text, {util.count(done.words, 'word')}")
            return done.text
        problems = done.faults
    except (OSError, UnicodeError, RuntimeError) as e:
        problems = (f"the writer failed ({type(e).__name__})",)
    run.log(f"document: kept the build summary: {util.first_line('; '.join(problems), 300)}")
    return ""
