from __future__ import annotations

import html
import json
import re
import string
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import config, research_plan, research_search, runner, steps, util
from .agent import Result
from .pipeline import prompt

MAX_ROUNDS = 2
GAP_TASKS = 3
NO_ROOM = research_search.NO_ROOM
ROUNDS_USED = "both extra search rounds were used"
NOTHING_NEW = "a further search returned no usable source"
NOT_SYNTHESIZED = "the evidence from the last search round was not synthesized"
TEXT = {"type": "string"}
TEXTS = {"type": "array", "items": TEXT}
BOOL = {"type": "boolean"}
CITE = re.compile(r"\[(S\d+)\]")
CITE_LINK = re.compile(r"\[(S\d+)\](\((?:[^()\s]|\([^()\s]*\))*\))?")
INLINE = re.compile(r"\]\(")
DEFINITION = re.compile(r"^ {0,3}\[(?:\\.|[^\\\[\]])+\]:", re.MULTILINE)
SPACING = re.compile(r"[ \t]*(?:\n[ \t]*)?")
ANGLED = re.compile(r"<((?:\\.|[^<>\\\n])*)>")
AUTOLINK = re.compile(r"<([a-z][a-z0-9+.-]{1,31}:[^\x00-\x20\x7f<>]*)>", re.IGNORECASE)
AUTOLINK_END = re.compile(r"[^\x00-\x20\x7f<>]*>")
ATTRIBUTE = re.compile(r"(?<![\w-])(?:href|src|srcset|longdesc|cite)[ \t\n\r\f\v]*=[ \t\n\r\f\v]*"
                       r"(\"[^\"]*\"|'[^']*'|[^ \t\n\r\f\v\"'=<>`]+)", re.IGNORECASE)
BARE = re.compile(r"(?:https?://|ftp://|www\.)[^ \t\n\r\f\v<]*", re.IGNORECASE)
ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
ENTITY_END = re.compile(r"&[a-z]+;$", re.IGNORECASE)
TRAILING = "?!.,:*_~'\""
PORTS = {"http": 80, "https": 443}
SECTIONS = ("Supported conclusions", "Inference", "Unresolved contradictions", "Gaps", "Sources")


def shape(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props)}


def listed(item: dict) -> dict:
    return {"type": "array", "items": item}


SCHEMA = shape(
    summary=TEXT, name=TEXT, answer=TEXT,
    conclusions=listed(shape(claim=TEXT, sources=TEXTS, critical=BOOL)),
    inferences=listed(shape(claim=TEXT, basis=TEXT, sources=TEXTS, critical=BOOL)),
    contradictions=listed(shape(issue=TEXT, sources=TEXTS)),
    gaps=listed(shape(question=TEXT, why=TEXT)),
    searches=listed(shape(goal=TEXT, queries=TEXTS, max_sources={"type": "integer"})))


@dataclass
class Asked:
    answer: dict | None = None
    tries: int = 0
    refused: list[str] = field(default_factory=list)
    may_search: bool = False
    error: str = ""
    why: str = ""


def escaped(text: str, at: int) -> bool:
    back = at
    while back and text[back - 1] == "\\":
        back -= 1
    return (at - back) % 2 == 1


def literal(text: str) -> str:
    return html.unescape(ESCAPE.sub(r"\1", text))


def target(text: str, at: int) -> str:
    at = SPACING.match(text, at).end()
    if angled := ANGLED.match(text, at):
        return literal(angled[1])
    end, depth = at, 0
    while end < len(text) and " " < text[end] != "\x7f":
        if text[end] == "\\" and end + 1 < len(text) and text[end + 1] in string.punctuation:
            end += 2
            continue
        if text[end] == ")" and not depth:
            break
        depth += {"(": 1, ")": -1}.get(text[end], 0)
        end += 1
    return literal(text[at:end])


def trimmed(token: str) -> str:
    while token:
        if token[-1] in TRAILING:
            token = token[:-1]
        elif token[-1] == ";":
            token = token[:entity.start()] if (entity := ENTITY_END.search(token)) else token[:-1]
        elif token[-1] == ")" and token.count(")") > token.count("("):
            token = token[:-1]
        else:
            break
    return token


def bare(text: str, found: re.Match) -> str:
    token, start = found[0], found.start()
    opener = text[start - 1] if start and not escaped(text, start - 1) else ""
    if opener == "<" and (end := AUTOLINK_END.match(token)):
        return token[:end.end() - 1]
    if opener == "[" and "]" in token:
        token = token[:token.index("]")]
    token = trimmed(token)
    return f"http://{token}" if token[:4].lower() == "www." else token


def link_targets(text: str) -> list[str]:
    found = [target(text, m.end()) for m in INLINE.finditer(text)]
    found += [target(text, m.end()) for m in DEFINITION.finditer(text)]
    found += [m[1] for m in AUTOLINK.finditer(text)]
    found += [html.unescape(m[1][1:-1] if m[1][0] in "\"'" else m[1]) for m in ATTRIBUTE.finditer(text)]
    found += [bare(text, m) for m in BARE.finditer(text)]
    return [url for url in dict.fromkeys(found) if url]


def destination(url: str) -> tuple:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError(f"the link {url} is not a valid address") from None
    scheme = parts.scheme.lower()
    if "@" in parts.netloc:
        raise ValueError(f"the link {url} puts user details before its host, so it may lead elsewhere")
    if scheme not in PORTS or not parts.hostname or "\\" in parts.netloc:
        raise ValueError(f"the link {url} is not an http or https address with a plain host")
    plain = urlsplit(research_search.canonical(url))
    return scheme, parts.hostname, PORTS[scheme] if port is None else port, plain.path, plain.query


def destinations(urls) -> set[tuple]:
    found = set()
    for url in urls:
        try:
            found.add(destination(url))
        except ValueError:
            continue
    return found


def ticket_links(text: str) -> set[tuple]:
    return destinations(link_targets(text or ""))


def without_cited_targets(text: str, known) -> str:
    return CITE_LINK.sub(lambda m: f"[{m[1]}]" if m[1] in known else m[0], text)


def present(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def entries(answer: dict, key: str, found: list[str]) -> list[dict]:
    items = answer.get(key)
    if not (isinstance(items, list) and all(isinstance(i, dict) for i in items)):
        found.append(f"{key} must be a list of objects")
        return []
    return items


def cited(item: dict, label: str, known, found: list[str]) -> list[str]:
    given = item.get("sources")
    if not (isinstance(given, list) and all(isinstance(g, str) for g in given)):
        found.append(f"{label} sources must be a list of source ids")
        return []
    given = list(dict.fromkeys(given))
    if unknown := [g for g in given if g not in known]:
        found.append(f"{label} cites {', '.join(unknown)}, which was not supplied")
    return [g for g in given if g in known]


def text_fields(answer: dict) -> list[str]:
    values = [answer.get("summary"), answer.get("answer")]
    for key, names in (("conclusions", ("claim",)), ("inferences", ("claim", "basis")),
                       ("contradictions", ("issue",)), ("gaps", ("question", "why")), ("searches", ("goal",))):
        for item in answer.get(key) if isinstance(answer.get(key), list) else []:
            if isinstance(item, dict):
                values += [item.get(n) for n in names]
                values += item.get("queries") if isinstance(item.get("queries"), list) else []
    return [v for v in values if isinstance(v, str)]


def faults(answer, known, links) -> list[str]:
    if not isinstance(answer, dict):
        return ["the answer is not an object"]
    found = [f"{key} must not be empty" for key in ("summary", "answer") if not present(answer.get(key))]
    if not isinstance(answer.get("name"), str):
        found.append("name must be a string")
    for i, item in enumerate(entries(answer, "conclusions", found), 1):
        label = f"conclusion {i}"
        found += [f"{label} needs a claim"] if not present(item.get("claim")) else []
        if not cited(item, label, known, found) and not any(f.startswith(label) for f in found):
            found.append(f"{label} has no source, so it is an unsupported assertion")
        found += [f"{label} needs critical as true or false"] if type(item.get("critical")) is not bool else []
    for i, item in enumerate(entries(answer, "inferences", found), 1):
        label = f"inference {i}"
        found += [f"{label} needs a claim"] if not present(item.get("claim")) else []
        found += [f"{label} needs a basis"] if not present(item.get("basis")) else []
        ids = cited(item, label, known, found)
        if type(item.get("critical")) is not bool:
            found.append(f"{label} needs critical as true or false")
        elif item["critical"] and not ids and not any(f.startswith(label) for f in found):
            found.append(f"{label} is critical and has no source")
    for i, item in enumerate(entries(answer, "contradictions", found), 1):
        label = f"contradiction {i}"
        found += [f"{label} needs an issue"] if not present(item.get("issue")) else []
        if len(cited(item, label, known, found)) < 2 and not any(f.startswith(label) for f in found):
            found.append(f"{label} needs two different supplied sources")
    for i, item in enumerate(entries(answer, "gaps", found), 1):
        if not (present(item.get("question")) and isinstance(item.get("why"), str)):
            found.append(f"gap {i} needs a question and a why")
    searches = entries(answer, "searches", found)
    if len(searches) > GAP_TASKS:
        found.append(f"searches must be at most {GAP_TASKS} tasks")
    for i, task in enumerate(searches[:GAP_TASKS], 1):
        found += research_plan.task_problems(i, task)
    allowed = destinations(known.values()) | links
    for text in text_fields(answer):
        found += [f"[{sid}] was not supplied" for sid in dict.fromkeys(CITE.findall(text)) if sid not in known]
        for url in link_targets(without_cited_targets(text, known)):
            try:
                outside = destination(url) not in allowed
            except ValueError as e:
                found.append(str(e))
                continue
            found += [f"the link {url} is neither a supplied source nor in the ticket"] if outside else []
    return list(dict.fromkeys(found))


def refusal(found: list[str]) -> str:
    return ("\n\n## Your last answer was refused\n\n" + "\n".join(f"- {f}" for f in found)
            + "\n\nReturn a corrected answer.")


def more_text(round_no: int, may_search: bool, why: str) -> str:
    if not may_search:
        return f"return [] because no further search is possible: {why}."
    left = MAX_ROUNDS - round_no
    return (f"up to {GAP_TASKS} search tasks, each a `goal`, 1 to 3 general `queries` and `max_sources`, for what the "
            f"evidence cannot settle; {util.count(left, 'search round')} left. Return [] when the evidence is enough.")


def ask(cfg: config.Config, budget: research_plan.Budget, s: steps.Settings, tools: list[str], plan: dict,
        evidence: dict, ref: str, values: dict, where: dict, cwd, add_dirs: list, round_no: int, links: set[str]) -> Asked:
    got = Asked()
    label = f"research {ref} synthesis" if round_no == 0 else f"research {ref} gap {round_no} synthesis"
    known = {source["id"]: source["url"] for source in research_search.supplied(evidence)["sources"]}
    feedback = ""
    for n in range(1, s.tries + 1):
        got.tries = n
        usd, seconds = budget.stage(s.timeout_minutes * 60)
        got.may_search = round_no < MAX_ROUNDS and not budget.short(
            usd, seconds - research_search.GRACE_SECONDS - research_plan.MIN_CALL_SECONDS)
        if not got.may_search:
            usd, seconds = budget.final()
            seconds = min(seconds, s.timeout_minutes * 60)
        text = prompt("research_synthesis", **values, **where,
                      questions=research_plan.bullets(plan["questions"]),
                      criteria=research_plan.bullets(plan["source_criteria"]),
                      claims=research_plan.bullets(plan["claims"]), tasks=research_search.task_lines(evidence),
                      sources=json.dumps(research_search.supplied(evidence), indent=2),
                      gaps="\n".join(research_search.gap_lines(evidence)) or "none",
                      more=more_text(round_no, got.may_search, ROUNDS_USED if round_no >= MAX_ROUNDS else NO_ROOM))
        res = budget.call(text + feedback, cwd, usd, seconds, reserve=got.may_search, model=s.model, runner=s.runner,
                          effort=s.call["effort"], tools=tools, allowed_tools=tools, instructions=list(s.instructions),
                          json_schema=SCHEMA, add_dirs=add_dirs, tier=s.tier, trace=("", label), cfg=cfg, settings=s)
        if budget.error:
            got.error = budget.error
            return got
        found = faults(res.structured, known, links) if res.ok else [
            util.first_line(res.error or "the collator gave no answer")]
        if not found:
            got.answer = res.structured
            return got
        got.why = "; ".join(found)
        got.refused.append(f"try {n}: {got.why}"[:300])
        research_search.say(ref, f"synthesis round {round_no} try {n} refused: {got.why[:300]}")
        if res.usage_limit:
            break
        feedback = refusal(found)
    return got


def link_ids(text: str, urls: dict[str, str]) -> str:
    return CITE_LINK.sub(lambda m: f"[{m[1]}]({urls[m[1]]})" if m[1] in urls else m[0], text)


def source_link(sid: str, urls: dict[str, str]) -> str:
    return f"[{sid}]({urls[sid]})"


def ids_in(answer: dict) -> list[str]:
    found = [sid for text in text_fields(answer) for sid in CITE.findall(text)]
    for key in ("conclusions", "inferences", "contradictions"):
        for item in answer[key]:
            found += item["sources"]
    return sorted(dict.fromkeys(found), key=lambda sid: int(sid[1:]))


def assemble(answer: dict, evidence: dict, extra: list[tuple[str, str]]) -> tuple[str, list[str]]:
    sources = {source["id"]: source for source in evidence["sources"]}
    urls = {sid: source["url"] for sid, source in sources.items()}

    def linked(item: dict) -> str:
        return ", ".join(source_link(sid, urls) for sid in item["sources"] if sid in urls)

    def marked(item: dict, text: str) -> str:
        return ("**Decision-critical:** " if item.get("critical") else "") + link_ids(text, urls)

    conclusions = [f"- {marked(c, c['claim'])} ({linked(c)})" for c in answer["conclusions"]]
    inferences = [f"- {marked(i, i['claim'])} Basis: {link_ids(i['basis'], urls)}"
                  + (f" ({linked(i)})" if i["sources"] else "") for i in answer["inferences"]]
    contradictions = [f"- {link_ids(c['issue'], urls)} ({linked(c)}); unresolved" for c in answer["contradictions"]]
    gaps = [f"- {link_ids(g['question'], urls)}: {link_ids(g['why'], urls)}" for g in answer["gaps"]]
    gaps += [f"- {question}: {why}" for question, why in extra]
    used = [sid for sid in ids_in(answer) if sid in urls]
    lines = []
    for sid in used:
        source = sources[sid]
        lines.append(f"- {source_link(sid, urls)} {source['title'] or 'untitled'}; published "
                     f"{source['published'] or 'not stated'}, event {source['event_date'] or 'not stated'}")
    parts = [link_ids(answer["answer"].strip(), urls)]
    for title, body in zip(SECTIONS, (conclusions, inferences, contradictions, gaps, lines)):
        parts.append(f"## {title}\n\n" + ("\n".join(body) or "None."))
    return "\n\n".join(parts), used


def run(cfg: config.Config, budget: research_plan.Budget, plan: dict, evidence: dict, ref: str, judged: bool,
        values: dict, where: dict, cwd, add_dirs: list) -> Result:
    s = steps.resolve(cfg, None, "research_synthesis")
    tools = [t for t in s.tools if t not in runner.WEB_TOOLS]
    links = ticket_links(values.get("ticket", "")) | ticket_links(values.get("related", ""))
    current, last, rounds, extra = evidence, None, [], []
    for round_no in range(MAX_ROUNDS + 1):
        got = ask(cfg, budget, s, tools, plan, current, ref, values, where, cwd, add_dirs, round_no, links)
        if got.error:
            return Result(ok=False, error=got.error + research_search.NO_REPORT)
        entry = {"round": round_no, "tries": got.tries, "refused": got.refused, "requests": [], "outcome": ""}
        rounds.append(entry)
        if not got.answer:
            if not last:
                return Result(ok=False, error=f"the collator gave no valid synthesis in "
                                              f"{util.count(got.tries, 'try', 'tries')}, so no report was written: "
                                              f"{got.why[:300]}")
            extra.append(("new evidence", NOT_SYNTHESIZED))
            entry["outcome"] = "no valid synthesis; the earlier one stands"
        else:
            last = got.answer
            requests = last["searches"]
            entry["requests"] = [r["goal"] for r in requests]
            reason = "" if got.may_search else ROUNDS_USED if round_no >= MAX_ROUNDS else NO_ROOM
            if requests and not reason:
                found = research_search.run(cfg, budget, {**plan, "tasks": requests}, ref, judged, earlier=current)
                if found.error:
                    return Result(ok=False, error=found.error)
                current = found.evidence or current
                if found.added:
                    entry["outcome"] = f"searched; {util.count(found.added, 'new finding')}"
                    research_search.say(ref, f"synthesis round {round_no}: {entry['outcome']}")
                    continue
                reason = found.stop or NOTHING_NEW
            extra += [(r["goal"], reason) for r in requests]
            entry["outcome"] = reason if requests else "complete"
        research_search.say(ref, f"synthesis round {round_no}: {util.count(got.tries, 'try', 'tries')}, "
                                 f"{len(got.refused)} refused, {util.count(len(entry['requests']), 'request')}; "
                                 f"{entry['outcome']}")
        break
    report, used = assemble(last, current, extra)
    current["synthesis"] = {"at": util.iso(), "runner": s.runner, "model": s.model, "tools": tools, "rounds": rounds,
                            "cited": used}
    util.write_json(research_search.evidence_file(ref), current)
    return Result(ok=True, structured={"summary": last["summary"], "report": report, "name": last["name"]})
