from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import config, outbound, research_plan, research_search, runner, steps, tracker, util
from . import research_synthesis as synth
from .pipeline import prompt

VERDICTS = ("supported", "contradicted", "inference", "unverifiable")
NOT_AUDITED = "not audited"
BATCH = 5
FETCHES = 3
QUOTE_CHARS = 20
COMPLETE, INCOMPLETE, FAILED = "complete", "incomplete", "failed"
NO_ROOM = "the research budget left no room for the audit"
NO_CLAIMS = "the synthesis listed no conclusions or inferences to audit"
LIMITED ="a usage limit stopped the audit"
WITHHELD = "the injection screen withheld the auditor's finding"
HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
KEYS = {"conclusion": "conclusions", "inference": "inferences"}
UNAUDITED = "Not audited"
LEAD_NOTE = ("**Audit note:** Arete wrote this lead and the summary from the claims the audit checked. The "
             "collator's own lead and summary are not published, because the audit does not check them.")
NO_LEAD = "No claim of the synthesis passed the audit."
SCHEMA = synth.shape(
    checks=synth.listed(synth.shape(claim=synth.TEXT, verdict=synth.TEXT, sources=synth.TEXTS, quote=synth.TEXT,
                                    fetched=synth.TEXTS, note=synth.TEXT, correction=synth.TEXT)),
    disagreements=synth.listed(synth.shape(issue=synth.TEXT, sources=synth.TEXTS)))
NO_FETCH = "You have no fetch tool in this call; judge from the retained passages."


@dataclass
class Audit:
    status: str = COMPLETE
    why: str = ""
    records: list[dict] = field(default_factory=list)
    disagreements: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    paid_usd: float = 0.0
    error: str = ""


@dataclass
class Asked:
    answer: dict | None = None
    tries: int = 0
    refused: list[str] = field(default_factory=list)
    fetch: list[str] = field(default_factory=list)
    stop: str = ""
    why: str = ""
    error: str = ""


def claims(answer: dict) -> list[dict]:
    found = []
    for kind, key in KEYS.items():
        for index, item in enumerate(answer[key]):
            found.append({"id": f"C{len(found) + 1}", "kind": kind, "index": index, "claim": item["claim"],
                          "basis": item.get("basis", ""), "cited": list(item["sources"]), "critical": item["critical"]})
    return found


def batches(found: list[dict]) -> list[list[dict]]:
    ordered = sorted(found, key=lambda c: not c["critical"])
    return [ordered[i:i + BATCH] for i in range(0, len(ordered), BATCH)]


def hosts(s: steps.Settings, batch: list[dict], sources: dict) -> dict[str, list[str]]:
    if "WebFetch" not in s.tools or "WebFetch" not in runner.web_tools(s.model, s.runner):
        return {}
    found: dict[str, list[str]] = {}
    for sid in dict.fromkeys(i for c in batch for i in c["cited"]):
        try:
            host = (urlsplit(sources[sid]["url"]).hostname or "").lower()
        except ValueError:
            continue
        if HOST.fullmatch(host) and not outbound.private_host(host):
            found.setdefault(host, []).append(sid)
    return found


def fetch_note(s: steps.Settings) -> str:
    if "WebFetch" not in runner.web_tools(s.model, s.runner):
        return f"{s.runner} has no WebFetch tool, so the auditor checks the retained passages only"
    return ""


def squeezed(text: str) -> str:
    return " ".join(text.split()).casefold()


def passages(source: dict) -> list[str]:
    return [squeezed(f["passage"]) for f in source["findings"]]


def plain(text: str, label: str, known, found: list[str]) -> None:
    found += [f"[{sid}] was not supplied" for sid in dict.fromkeys(synth.CITE.findall(text)) if sid not in known]
    if hits := synth.unsafe(text, known):
        found.append(f"{label} holds {', '.join(map(repr, hits[:5]))}; {synth.PLAIN}")


def check_faults(item: dict, label: str, known: dict, fetchable: set, found: list[str]) -> list[str]:
    verdict = item.get("verdict")
    if verdict not in VERDICTS:
        found.append(f"{label} verdict must be one of {', '.join(VERDICTS)}")
    ids = synth.cited(item, label, known, found)
    fetched = synth.cited({"sources": item.get("fetched")}, f"{label} fetched", known, found)
    if barred := [i for i in fetched if i not in fetchable]:
        found.append(f"{label} fetched {', '.join(barred)}, which the audit may not fetch")
    texts = {key: item.get(key) for key in ("quote", "note", "correction")}
    if not all(isinstance(t, str) for t in texts.values()):
        found.append(f"{label} quote, note and correction must be strings")
        return fetched
    quote, note, correction = (texts[k].strip() for k in ("quote", "note", "correction"))
    if verdict == "supported" and correction:
        found.append(f"{label} is supported, so it takes no correction")
    if verdict in VERDICTS and verdict != "supported" and not note:
        found.append(f"{label} is {verdict}, so it needs a note")
    if verdict in ("supported", "contradicted") or correction:
        if not ids:
            found.append(f"{label} needs at least one source")
        if not quote:
            found.append(f"{label} needs a quote")
        elif not fetched and ids:
            q = squeezed(quote)
            if not any(q in p and (len(q) >= QUOTE_CHARS or q == p) for sid in ids for p in passages(known[sid])):
                found.append(f"{label} quote is not in a retained passage of {', '.join(ids)}; copy it word for "
                             "word, or fetch the source and list it under fetched")
    for key in ("note", "correction"):
        plain(texts[key], f"{label} {key}", known, found)
    return fetched


def faults(answer, batch: list[dict], known: dict, fetchable: set) -> list[str]:
    if not isinstance(answer, dict):
        return ["the answer is not an object"]
    found: list[str] = []
    wanted = [c["id"] for c in batch]
    checks = synth.entries(answer, "checks", found)
    seen = [c.get("claim") for c in checks]
    if odd := [str(i) for i in dict.fromkeys(seen) if i not in wanted]:
        found.append(f"checks name {', '.join(odd)}, which is not a claim of this batch")
    if repeated := [i for i in wanted if seen.count(i) > 1]:
        found.append(f"checks list {', '.join(repeated)} more than once")
    if missing := [i for i in wanted if i not in seen]:
        found.append(f"checks leave out {', '.join(missing)}; check every claim")
    every: list[str] = []
    for item in checks:
        if item.get("claim") in wanted:
            every += check_faults(item, item["claim"], known, fetchable, found)
    if len(set(every)) > FETCHES:
        found.append(f"fetched names {len(set(every))} sources; fetch at most {FETCHES}")
    for i, item in enumerate(synth.entries(answer, "disagreements", found), 1):
        label = f"disagreement {i}"
        if not synth.present(item.get("issue")):
            found.append(f"{label} needs an issue")
        else:
            plain(item["issue"], f"{label} issue", known, found)
        if len(synth.cited(item, label, known, found)) < 2 and not any(f.startswith(label) for f in found):
            found.append(f"{label} needs two different supplied sources")
    return list(dict.fromkeys(found))


def outcome(kind: str, check: dict) -> str:
    verdict = check["verdict"]
    if verdict == "supported":
        return "kept"
    if check["correction"].strip():
        return "corrected"
    if verdict == "inference":
        return "kept" if kind == "inference" else "qualified"
    return "gap"


def record(claim: dict, check: dict | None, why: str = "") -> dict:
    base = {"id": claim["id"], "kind": claim["kind"], "critical": claim["critical"], "claim": claim["claim"],
            "cited": claim["cited"], "verdict": NOT_AUDITED, "why": why, "sources": [], "quote": "",
            "quote_from": "", "fetched": [], "note": "", "correction": "", "screen": {}, "outcome": NOT_AUDITED}
    if not check:
        return base
    fetched = list(dict.fromkeys(check["fetched"]))
    return {**base, "verdict": check["verdict"], "why": "", "sources": list(dict.fromkeys(check["sources"])),
            "quote": check["quote"].strip(), "fetched": fetched,
            "quote_from": "fetched" if fetched else "retained" if check["quote"].strip() else "",
            "note": check["note"].strip(), "correction": check["correction"].strip(),
            "outcome": outcome(claim["kind"], check)}


def fetch_text(fetchable: dict[str, list[str]], sources: dict) -> str:
    if not fetchable:
        return NO_FETCH
    lines = [f"- {sid}: {sources[sid]['url']}" for sids in fetchable.values() for sid in sids]
    return (f"You may fetch these sources again, at most {FETCHES} in this call, and only a source's own address, "
            "only when its retained passage does not settle a claim:\n" + "\n".join(lines))


def ask(cfg: config.Config, budget: research_plan.Budget, s: steps.Settings, read: list[str], batch: list[dict],
        n: int, evidence: dict, ref: str, cwd) -> Asked:
    supplied = research_search.supplied(evidence)
    known = {source["id"]: source for source in supplied["sources"]}
    fetchable = hosts(s, batch, known)
    got = Asked(fetch=list(fetchable))
    tools = [*read, "WebFetch"] if fetchable else read
    allowed = [*read, *(f"WebFetch(domain:{h})" for h in fetchable)]
    ids = {sid for sids in fetchable.values() for sid in sids}
    text = prompt("research_audit", today=util.iso()[:10], sources=json.dumps(supplied, indent=2),
                  fetch=fetch_text(fetchable, known),
                  claims=json.dumps([{k: c[k] for k in ("id", "kind", "claim", "basis", "cited", "critical")}
                                     for c in batch], indent=2))
    feedback = ""
    for attempt in range(1, s.tries + 1):
        usd, seconds = budget.final()
        seconds = min(seconds, s.timeout_minutes * 60)
        if budget.short(usd, seconds):
            got.stop = f"{NO_ROOM} ({budget.spent_words()})"
            return got
        got.tries = attempt
        res = budget.call(text + feedback, cwd, usd, seconds, model=s.model, runner=s.runner, effort=s.call["effort"],
                          tools=tools, allowed_tools=allowed, instructions=list(s.instructions), json_schema=SCHEMA,
                          tier=s.tier, trace=("", f"research {ref} batch {n} audit"), cfg=cfg, settings=s,
                          fallback=not fetchable)
        if budget.error:
            got.error = budget.error
            return got
        found = faults(res.structured, batch, known, ids) if res.ok else [
            util.first_line(res.error or "the auditor gave no answer")]
        if not found:
            got.answer = res.structured
            return got
        got.why = "; ".join(found)
        got.refused.append(f"try {attempt}: {got.why}"[:300])
        research_search.say(ref, f"audit batch {n} try {attempt} refused: {got.why[:300]}")
        if res.usage_limit:
            got.stop = LIMITED
            return got
        feedback = synth.refusal(found)
    return got


def run(cfg: config.Config, budget: research_plan.Budget, answer: dict, evidence: dict, ref: str,
        judged: bool) -> Audit:
    s = steps.resolve(cfg, None, "research_audit")
    read = [t for t in s.tools if t not in runner.WEB_TOOLS]
    cwd = research_search.FOLDER / "search" / tracker.safe_filename(ref)
    cwd.mkdir(parents=True, exist_ok=True)
    audit = Audit()
    started = budget.spent_usd
    stop, failed = "", False
    pending = batches(claims(answer))
    for n, batch in enumerate(pending, 1):
        if stop:
            audit.records += [record(c, None, stop) for c in batch]
            continue
        got = ask(cfg, budget, s, read, batch, n, evidence, ref, cwd)
        if got.error:
            return Audit(error=got.error)
        audit.calls.append({"batch": n, "tries": got.tries, "refused": got.refused, "fetch": got.fetch})
        stop = got.stop
        if not got.answer:
            failed = failed or got.tries > 0
            reason = got.stop or got.why
            audit.records += [record(c, None, reason) for c in batch]
            research_search.say(ref, f"audit batch {n}: not audited, {reason[:200]}")
            continue
        by_id = {c["claim"]: c for c in got.answer["checks"]}
        made = [record(c, by_id[c["id"]]) for c in batch]
        extra = [{**d, "sources": list(dict.fromkeys(d["sources"])), "screen": {}} for d in got.answer["disagreements"]]
        research_search.screen(cfg, ref, judged, [
            ({"claim": r["id"], "note": r["note"], "correction": r["correction"], "quote": r["quote"]}, r["screen"])
            for r in made] + [({"issue": d["issue"]}, d["screen"]) for d in extra])
        for i, r in enumerate(made):
            if r["screen"]["status"] in research_search.WITHHELD:
                made[i] = {**record(batch[i], None, WITHHELD), "screen": r["screen"]}
        audit.records += made
        audit.disagreements += [d for d in extra if d["screen"]["status"] in research_search.PASSED_ON]
        research_search.say(ref, f"audit batch {n}: {util.count(got.tries, 'try', 'tries')}, "
                                 f"{len(got.refused)} refused, {util.count(len(batch), 'claim')} checked")
    audit.records.sort(key=lambda r: int(r["id"][1:]))
    if not audit.records:
        audit.status, audit.why = INCOMPLETE, NO_CLAIMS
        research_search.say(ref, f"audit {audit.status}: {audit.why}")
        return audit
    unchecked = [r for r in audit.records if r["verdict"] == NOT_AUDITED]
    audit.why = unchecked[0]["why"] if unchecked else ""
    audit.status = FAILED if failed else INCOMPLETE if unchecked else COMPLETE
    audit.paid_usd = research_plan.plus(budget.spent_usd, -started)
    research_search.say(ref, f"audit {audit.status}" + (f": {audit.why[:200]}" if audit.why else ""))
    return audit


def applied(answer: dict, audit: Audit) -> dict:
    by_id = {r["id"]: r for r in audit.records}
    revised = {**answer, "conclusions": [], "inferences": [], "gaps": list(answer["gaps"]),
               "contradictions": list(answer["contradictions"])}
    for c in claims(answer):
        item, r = answer[KEYS[c["kind"]]][c["index"]], by_id[c["id"]]
        kept_in = KEYS[c["kind"]]
        if r["outcome"] == NOT_AUDITED:
            item = {**item, "mark": UNAUDITED}
        elif r["outcome"] == "corrected":
            item = {**item, "claim": r["correction"], "sources": r["sources"], "mark": "Corrected by the audit"}
        elif r["outcome"] == "qualified":
            item, kept_in = {**item, "basis": r["note"], "mark": "Qualified by the audit"}, "inferences"
        elif r["outcome"] == "gap":
            what = "a decision-critical claim" if c["critical"] else "a claim"
            revised["gaps"].append({"question": c["claim"],
                                    "why": f"{what} the audit found {r['verdict']}: {r['note']}"})
            continue
        revised[kept_in].append(item)
    for d in audit.disagreements:
        if not any(set(x["sources"]) == set(d["sources"]) for x in revised["contradictions"]):
            revised["contradictions"].append({"issue": d["issue"], "sources": d["sources"]})
    return revised


def tally(audit: Audit) -> tuple[int, int, int, int]:
    return tuple(sum(r["outcome"] == o for r in audit.records) for o in ("kept", "corrected", "qualified", "gap"))


def not_audited(audit: Audit) -> str:
    if not audit.records:
        return "nothing was audited"
    k = sum(r["verdict"] == NOT_AUDITED for r in audit.records)
    return f"{k} of {len(audit.records)} claims {'was' if k == 1 else 'were'} not audited"


def section(audit: Audit, urls: dict[str, str]) -> tuple[list[str], list[str]]:
    if audit.status == COMPLETE:
        a, b, c, d = tally(audit)
        head = (f"Audit complete: {len(audit.records)} claims checked against their sources; {a} supported, "
                f"{b} corrected, {c} qualified as inference, {d} left as gaps.")
    else:
        head = f"Audit {audit.status}: {synth.inert_text(audit.why)}; {not_audited(audit)}."
    lines, used = [head, ""], []
    for r in audit.records:
        claim = synth.link_ids(r["claim"], urls)
        if r["verdict"] == NOT_AUDITED:
            lines.append(f"- **{NOT_AUDITED}**: {claim}")
            used += synth.CITE.findall(r["claim"])
            continue
        label = r["verdict"] + (f", {r['outcome']}" if r["outcome"] != "kept" else "")
        links = ", ".join(synth.source_link(sid, urls) for sid in r["sources"] if sid in urls)
        lines.append(f"- **{label}**: {claim}{f' ({links})' if links else ''}. "
                     f"{synth.link_ids(r['note'], urls)}".rstrip())
        used += [*r["sources"], *synth.CITE.findall(r["claim"] + " " + r["note"])]
    ids = sorted({sid for sid in used if sid in urls}, key=lambda sid: int(sid[1:]))
    return lines, ids


def standing(revised: dict) -> list[tuple[str, dict]]:
    found = [(kind, item) for kind, key in KEYS.items() for item in revised[key] if item.get("mark") != UNAUDITED]
    return [f for f in found if f[1]["critical"]] or found


def lead(revised: dict, audit: Audit) -> str:
    bullets = []
    for kind, item in standing(revised):
        cites = "".join(f" [{sid}]" for sid in item["sources"] if f"[{sid}]" not in item["claim"])
        bullets.append("- " + ("Inference: " if kind == "inference" else "") + item["claim"] + cites)
    parts = [LEAD_NOTE, "\n".join(bullets) or f"{NO_LEAD} See Gaps and Audit below."]
    if n := sum(r["critical"] and r["outcome"] in ("gap", NOT_AUDITED) for r in audit.records):
        parts.append(f"{util.count(n, 'decision-critical claim')} did not pass the audit; see Gaps and Audit below.")
    return "\n\n".join(parts)


def summary(revised: dict, audit: Audit) -> str:
    found = standing(revised)
    if found:
        kind, item = found[0]
        text = " ".join(synth.CITE.sub("", item["claim"]).split())
        text = util.first_line(re.sub(r"\s+([.,;:])", r"\1", text), 300)
        text = ("Inference: " if kind == "inference" else "") + (text if text.endswith((".", "!", "?")) else text + ".")
    else:
        text = NO_LEAD
    return f"{text} {summary_words(audit)}"


def summary_words(audit: Audit) -> str:
    if audit.status == COMPLETE:
        a, b, c, d = tally(audit)
        words = (f"Audit complete: {len(audit.records)} claims checked; {a} supported, {b} corrected, "
                 f"{c} qualified, {d} left as gaps")
    else:
        words = f"Audit {audit.status}: {not_audited(audit)}"
    return words + "."
