from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from . import config, research_plan, research_search, runner, steps, util
from .agent import Result
from .pipeline import prompt

MAX_ROUNDS = 2
GAP_TASKS = 3
NO_ROOM = research_search.NO_ROOM
ROUNDS_USED = "both extra search rounds were used"
NOTHING_NEW = "a further search returned no usable source"
NOT_SYNTHESIZED = "the evidence from the last search round was not synthesized"
AUDITED_LEAD = ("The audit checks only conclusions and inferences, and Arete writes the published lead and summary "
                "from the ones that pass, so list every assertion the answer rests on, the direct answer included, as "
                "a conclusion or an inference.")
TEXT = {"type": "string"}
TEXTS = {"type": "array", "items": TEXT}
BOOL = {"type": "boolean"}
CITE = re.compile(r"\[(S\d+)\]")
IMAGE_OR_CITE = re.compile(r"(!?)\[(S\d+)\]")
LINK_SCHEMES = ("https", "http", "ftp", "mailto", "xmpp", "file", "data", "javascript", "vbscript")
AUTOLINK = r"://|www\.|@|\b(?:" + "|".join(LINK_SCHEMES) + r"):(?=[a-z/])"
AUTOLINKS = re.compile(AUTOLINK, re.IGNORECASE)
UNSAFE = re.compile(r"[\[\]<&\\\x00-\x08\x0b-\x1f\x7f-\x9f]|" + AUTOLINK, re.IGNORECASE)
CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
PLAIN = ("write plain text with no link, address, HTML, entity or escape of its own, and cite a source only as [S1], "
         "which Arete turns into its link")
BACKTICKS = re.compile(r"`+")
URL_KEPT = frozenset(string.ascii_letters + string.digits + "-.~!$&+,;=:@/?%")
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


def text_fields(answer: dict) -> list[tuple[str, str]]:
    fields = [("summary", answer.get("summary")), ("answer", answer.get("answer"))]
    for key, label, names in (("conclusions", "conclusion", ("claim",)), ("inferences", "inference", ("claim", "basis")),
                              ("contradictions", "contradiction", ("issue",)), ("gaps", "gap", ("question", "why")),
                              ("searches", "search", ("goal",))):
        items = answer.get(key)
        for i, item in enumerate(items if isinstance(items, list) else [], 1):
            if isinstance(item, dict):
                fields += [(f"{label} {i} {name}", item.get(name)) for name in names]
    return [(label, text) for label, text in fields if isinstance(text, str)]


def unsafe(text: str, known) -> list[str]:
    return list(dict.fromkeys(UNSAFE.findall(CITE.sub(lambda m: "" if m[1] in known else m[0], text))))


def faults(answer, known) -> list[str]:
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
    for label, text in text_fields(answer):
        found += [f"[{sid}] was not supplied" for sid in dict.fromkeys(CITE.findall(text)) if sid not in known]
        if hits := unsafe(text, () if label == "summary" else known):
            found.append(f"{label} holds {', '.join(map(repr, hits[:5]))}; {PLAIN}")
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
        evidence: dict, ref: str, values: dict, where: dict, cwd, add_dirs: list, round_no: int) -> Asked:
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
                      audit=AUDITED_LEAD if cfg.research_audit else "",
                      more=more_text(round_no, got.may_search, ROUNDS_USED if round_no >= MAX_ROUNDS else NO_ROOM))
        res = budget.call(text + feedback, cwd, usd, seconds, reserve=got.may_search, model=s.model, runner=s.runner,
                          effort=s.call["effort"], tools=tools, allowed_tools=tools, instructions=list(s.instructions),
                          json_schema=SCHEMA, add_dirs=add_dirs, tier=s.tier, trace=("", label), cfg=cfg, settings=s)
        if budget.error:
            got.error = budget.error
            return got
        found = faults(res.structured, known) if res.ok else [
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
    def linked(m: re.Match) -> str:
        return ("\\!" if m[1] else "") + source_link(m[2], urls) if m[2] in urls else m[0]

    return IMAGE_OR_CITE.sub(linked, text)


def source_link(sid: str, urls: dict[str, str]) -> str:
    return f"[{sid}]({urls[sid]})"


def encoded(text: str) -> str:
    return "".join(c if c in URL_KEPT else "".join(f"%{b:02X}" for b in c.encode(errors="replace")) for c in text)


def written(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, encoded(parts.netloc.rpartition("@")[2]), encoded(parts.path),
                       encoded(parts.query), encoded(parts.fragment))).replace("&", "&amp;")


def inert_text(text: str) -> str:
    text = " ".join(CONTROLS.sub(" ", text).split())
    if AUTOLINKS.search(text):
        fence = "`" * (max(map(len, BACKTICKS.findall(text)), default=0) + 1)
        pad = " " if "`" in (text[0], text[-1]) else ""
        return f"{fence}{pad}{text}{pad}{fence}"
    return "".join(f"\\{c}" if c in string.punctuation else c for c in text)


def source_line(sid: str, urls: dict[str, str], source: dict) -> str:
    return (f"- {source_link(sid, urls)} {inert_text(source.get('title') or '') or 'untitled'}; published "
            f"{source.get('published') or 'not stated'}, event {source.get('event_date') or 'not stated'}")


def ids_in(answer: dict) -> list[str]:
    found = [sid for _, text in text_fields(answer) for sid in CITE.findall(text)]
    for key in ("conclusions", "inferences", "contradictions"):
        for item in answer[key]:
            found += item["sources"]
    return sorted(dict.fromkeys(found), key=lambda sid: int(sid[1:]))


def report(answer: dict, urls: dict[str, str], extra: list[tuple[str, str]], sources: dict,
           audit: tuple[list[str], list[str]] | None = None) -> tuple[str, list[str]]:
    def linked(item: dict) -> str:
        return ", ".join(source_link(sid, urls) for sid in item["sources"] if sid in urls)

    def marked(item: dict, text: str) -> str:
        return (("**Decision-critical:** " if item.get("critical") else "")
                + (f"**{item['mark']}:** " if item.get("mark") else "") + link_ids(text, urls))

    conclusions = [f"- {marked(c, c['claim'])} ({linked(c)})" for c in answer["conclusions"]]
    inferences = [f"- {marked(i, i['claim'])} Basis: {link_ids(i['basis'], urls)}"
                  + (f" ({linked(i)})" if i["sources"] else "") for i in answer["inferences"]]
    contradictions = [f"- {link_ids(c['issue'], urls)} ({linked(c)}); unresolved" for c in answer["contradictions"]]
    gaps = [f"- {link_ids(g['question'], urls)}: {link_ids(g['why'], urls)}" for g in answer["gaps"]]
    gaps += [f"- {link_ids(question, urls)}: {inert_text(why)}" for question, why in extra]
    used = [sid for sid in ids_in(answer) if sid in urls]
    if audit:
        used = sorted({*used, *(sid for sid in audit[1] if sid in urls)}, key=lambda sid: int(sid[1:]))
    cited_sources = [source_line(sid, urls, sources.get(sid, {})) for sid in used]
    sections = list(zip(SECTIONS, (conclusions, inferences, contradictions, gaps, cited_sources)))
    if audit:
        sections.insert(SECTIONS.index("Sources"), ("Audit", audit[0]))
    parts = [link_ids(answer["answer"].strip(), urls)]
    for title, lines in sections:
        parts.append(f"## {title}\n\n" + ("\n".join(lines) or "None."))
    return "\n\n".join(parts), used


def assemble(answer: dict, evidence: dict, extra: list[tuple[str, str]],
             audit: tuple[list[str], list[str]] | None = None) -> tuple[str, list[str]]:
    sources = {source["id"]: source for source in evidence["sources"]}
    return report(answer, {sid: written(source["url"]) for sid, source in sources.items()}, extra, sources, audit)


def audited(cfg: config.Config, budget: research_plan.Budget, last: dict, current: dict, extra: list, ref: str,
            judged: bool) -> Result:
    from . import research_audit

    audit = research_audit.run(cfg, budget, last, current, ref, judged)
    if audit.error:
        return Result(ok=False, error=audit.error + research_search.NO_REPORT)
    revised = research_audit.applied(last, audit)
    lead, summary = research_audit.lead(revised, audit), research_audit.summary(revised, audit)
    revised["answer"], revised["summary"] = lead, summary
    urls = {source["id"]: written(source["url"]) for source in current["sources"]}
    text, used = assemble(revised, current, extra, research_audit.section(audit, urls))
    a = steps.resolve(cfg, None, "research_audit")
    current["audit"] = {"at": util.iso(), "runner": a.runner, "model": a.model, "tools": list(a.tools),
                        "status": audit.status, "why": audit.why, "paid_usd": audit.paid_usd, "calls": audit.calls,
                        "claims": audit.records, "disagreements": audit.disagreements}
    current["synthesis"]["cited"] = used
    return Result(ok=True, structured={"summary": summary, "report": text, "name": last["name"]})


def run(cfg: config.Config, budget: research_plan.Budget, plan: dict, evidence: dict, ref: str, judged: bool,
        values: dict, where: dict, cwd, add_dirs: list) -> Result:
    s = steps.resolve(cfg, None, "research_synthesis")
    tools = [t for t in s.tools if t not in runner.WEB_TOOLS]
    current, last, rounds, extra = evidence, None, [], []
    for round_no in range(MAX_ROUNDS + 1):
        got = ask(cfg, budget, s, tools, plan, current, ref, values, where, cwd, add_dirs, round_no)
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
    current["synthesis"] = {"at": util.iso(), "runner": s.runner, "model": s.model, "tools": tools, "rounds": rounds,
                            "cited": []}
    if cfg.research_audit:
        res = audited(cfg, budget, last, current, extra, ref, judged)
        if not res.ok:
            return res
    else:
        report, current["synthesis"]["cited"] = assemble(last, current, extra)
        res = Result(ok=True, structured={"summary": last["summary"], "report": report, "name": last["name"]})
    util.write_json(research_search.evidence_file(ref), current)
    return res
