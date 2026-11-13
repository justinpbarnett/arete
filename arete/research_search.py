from __future__ import annotations

import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import claude, config, jev, paths, research_plan, runner, steps, tracker, util
from .pipeline import prompt

FOLDER = paths.STATE_DIR / "research"
SOURCE_KEYS = ("question", "url", "title", "published", "event_date", "passage", "uncertainty")
TEXT = {"type": "string"}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "sources": {"type": "array", "items": {
            "type": "object", "properties": {key: TEXT for key in SOURCE_KEYS}, "required": list(SOURCE_KEYS)}},
        "notes": TEXT,
    },
    "required": ["sources", "notes"],
}
DATE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")
PASSAGE_CHARS = 1500
PASSED_ON = ("passed", "unscreened")
WITHHELD = ("flagged", "failed")
CAP_REACHED = "the search cap was reached"
ROUTING_SECONDS = 20
GRACE_SECONDS = ROUTING_SECONDS + claude.STOP_GRACE_SECONDS + claude.DRAIN_SECONDS
NO_REPORT =", so no report was written"
USAGE_KEYS = ("cost_usd", "tokens_in", "tokens_out", "turns")


@dataclass
class Found:
    error: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class Outcome:
    worker: dict
    found: list[dict] = field(default_factory=list)
    paid: float = 0.0
    fatal: str = ""
    limited: str = ""


def evidence_file(ref: str) -> Path:
    return FOLDER / f"{tracker.safe_filename(ref)}.evidence.json"


def dec(value) -> Decimal:
    return Decimal(str(value))


def unavailable(s: steps.Settings) -> str:
    wanted = [t for t in s.tools if t in runner.WEB_TOOLS]
    if "WebSearch" not in wanted:
        return "[steps.research_search] tools leave out WebSearch, so a worker cannot search"
    has = runner.web_tools(s.model, s.runner)
    if missing := [t for t in wanted if t not in has]:
        return (f"{runner.stands_for(s.model, s.runner)[0]} has no {', '.join(missing)} tool, "
                "and arete grants a worker nothing in its place")
    return ""


def text_of(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def web_url(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
        return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def canonical(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def stated(value: str) -> str:
    return value if DATE.fullmatch(value) else ""


def flaw(item) -> str:
    if not isinstance(item, dict):
        return "the source is not an object"
    if bad := [key for key in SOURCE_KEYS if not isinstance(item.get(key), str)]:
        return f"{', '.join(bad)} must be given as a string"
    url, passage = item.get("url"), item.get("passage")
    if not (isinstance(url, str) and web_url(url)):
        return "the URL is not http or https with a host"
    if not (isinstance(passage, str) and passage.strip()):
        return "the passage is empty"
    if len(passage) > PASSAGE_CHARS:
        return f"the passage is over {PASSAGE_CHARS} characters"
    return ""


def vetted(answer, limit: int) -> tuple[list[str], list[dict], list[dict]]:
    if not isinstance(answer, dict):
        return ["the answer is not an object"], [], []
    sources = answer.get("sources")
    if not isinstance(sources, list):
        return ["sources must be a list"], [], []
    faults = [] if isinstance(answer.get("notes"), str) else ["notes must be given as a string"]
    valid, dropped = [], []
    for item in sources:
        if why := flaw(item):
            dropped.append({"url": text_of(item.get("url")) if isinstance(item, dict) else "", "why": why})
        else:
            valid.append({key: text_of(item.get(key)) for key in SOURCE_KEYS})
    if sources and not valid:
        faults.append("none of the sources were usable: " + "; ".join(dict.fromkeys(d["why"] for d in dropped)))
    dropped += [{"url": extra["url"], "why": f"past the task's limit of {limit} sources"} for extra in valid[limit:]]
    return faults, valid[:limit], dropped


def refusal(faults: list[str]) -> str:
    return ("\n\n## Your last answer was refused\n\n" + "\n".join(f"- {f}" for f in faults)
            + "\n\nReturn a corrected answer.")


def skeleton(n: int, task: dict) -> dict:
    return {"task": n, "goal": task["goal"], "queries": task["queries"], "max_sources": task["max_sources"],
            "status": "cancelled", "tries": 0, "error": "", "allowance_usd": 0.0, "paid_usd": 0.0, "cost_usd": 0.0,
            "tokens_in": 0, "tokens_out": 0, "turns": 0, "seconds": 0.0, "sources": [], "dropped": [], "notes": "",
            "notes_screen": {"status": "none", "p": None}, "raw": []}


def work(cfg: config.Config, s: steps.Settings, plan: dict, ref: str, n: int, cwd: Path, allowance: float,
         seconds: float) -> Outcome:
    task = plan["tasks"][n - 1]
    which = runner.chosen(s.model, s.runner, cfg)[0]
    text = prompt("research_search", goal=task["goal"], queries=research_plan.bullets(task["queries"]),
                  max_sources=task["max_sources"], criteria=research_plan.bullets(plan["source_criteria"]))
    started = time.monotonic()
    deadline = started + seconds
    worker = skeleton(n, task)
    paid, faults, feedback, kept, fatal, limited = 0.0, [], "", [], "", ""
    answer = None
    for _ in range(s.tries):
        remaining, left = float(dec(allowance) - dec(paid)), deadline - time.monotonic() - GRACE_SECONDS
        if remaining <= runner.BUDGET_FLOOR_USD or left < research_plan.MIN_CALL_SECONDS:
            faults = faults or ["the worker's share of the search cap was used up"]
            break
        worker["tries"] += 1
        try:
            res = runner.run(text + feedback, cwd, model=s.model, runner=s.runner, effort=s.call["effort"],
                             tools=list(s.tools), allowed_tools=list(s.tools), instructions=list(s.instructions),
                             json_schema=RESULT_SCHEMA, max_budget_usd=remaining, timeout=int(left), persist=False,
                             wait_out_limits=False,
                             remaining_budget=lambda amount=remaining: 0.0 if which in ("claude", "codex") else amount,
                             tier=s.tier, trace=("", f"research {ref} task {n} search"), cfg=cfg, settings=s,
                             fallback=False)
        except Exception as e:
            fatal = (f"a search call failed with {util.first_line(str(e)) or type(e).__name__}, so its charge is "
                     f"unknown; known prior spend is {util.money(paid)} on this worker")
            break
        for key in USAGE_KEYS:
            worker[key] = research_plan.plus(worker[key], getattr(res, key)) if key == "cost_usd" else (
                worker[key] + getattr(res, key))
        worker["raw"].append(res.structured)
        if not research_plan.known(res.paid_usd):
            fatal = f"a search call's paid cost is unknown; known prior spend is {util.money(paid)} on this worker"
            break
        paid = research_plan.plus(paid, res.paid_usd)
        if dec(paid) > dec(allowance):
            fatal = "a search worker exceeded its allowance; its incurred cost is recorded"
            break
        if time.monotonic() > deadline:
            fatal = "a search worker exceeded its share of the search deadline"
            break
        if res.usage_limit:
            limited = "a usage limit stopped the search"
            faults = [util.first_line(res.error or limited)]
            break
        if res.ok:
            faults, kept, dropped = vetted(res.structured, task["max_sources"])
            worker["dropped"], answer = dropped, res.structured
        else:
            faults = [util.first_line(res.error or "the worker gave no answer")]
        if not faults:
            break
        feedback = refusal(faults)
    failed = bool(faults or fatal or limited)
    worker.update(status="failed" if failed else "ok", paid_usd=paid, seconds=round(time.monotonic() - started, 1),
                  error=fatal or "; ".join(faults)[:300] if failed else "")
    if failed:
        return Outcome(worker, paid=paid, fatal=fatal, limited=limited)
    worker["notes"] = text_of(answer.get("notes"))
    retrieved = util.iso()
    return Outcome(worker, [{**source, "retrieved": retrieved} for source in kept], paid)


def previous_ids(ref: str) -> dict[str, str]:
    try:
        ids = json.loads(evidence_file(ref).read_text()).get("ids")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(ids, dict):
        return {}
    return {u: i for u, i in ids.items() if isinstance(u, str) and isinstance(i, str) and re.fullmatch(r"S\d+", i)}


def next_id(ids: dict[str, str]) -> str:
    return "S" + str(1 + max((int(i[1:]) for i in ids.values()), default=0))


def collect(outcomes: dict[int, Outcome], ids: dict[str, str]) -> list[dict]:
    sources: dict[str, dict] = {}
    for n in sorted(outcomes):
        out = outcomes[n]
        for item in out.found:
            url = canonical(item["url"])
            sid = ids.setdefault(url, next_id(ids))
            source = sources.setdefault(sid, {"id": sid, "url": url, "title": "", "published": "", "event_date": "",
                                              "retrieved": item["retrieved"], "tasks": [], "findings": []})
            if n not in source["tasks"]:
                source["tasks"].append(n)
            source["findings"].append({"task": n, "question": item["question"], "passage": item["passage"],
                                       "uncertainty": item["uncertainty"], "title": item["title"],
                                       "published": stated(item["published"]),
                                       "event_date": stated(item["event_date"]),
                                       "screen": {"status": "none", "p": None}})
        out.worker["sources"] = list(dict.fromkeys(
            ids[canonical(item["url"])] for item in out.found))
    return sorted(sources.values(), key=lambda source: int(source["id"][1:]))


def whole(state) -> bool:
    text = state if isinstance(state, str) else json.dumps(state, default=str)
    return len(text) <= jev.STATE_CHARS


def screen(cfg: config.Config, ref: str, judged: bool, items: list[tuple[object, dict]]) -> None:
    if not items:
        return
    if not judged:
        for _, target in items:
            target.update(status="unscreened", p=None)
        print(f"{util.iso()} {ref}: JEV is off, so search notes go on unscreened", flush=True)
        return
    questions = {"injection": jev.yes_no(jev.INJECTION)}
    fits = [whole(state) for state, _ in items]
    sent = [item for item, fit in zip(items, fits) if fit]
    replies = iter(jev.ask_many([(state, questions) for state, _ in sent], cfg,
                                trace=("", f"research {ref} notes jev")) if sent else [])
    for (_, target), fit in zip(items, fits):
        answer = next(replies).answers.get("injection") if fit else None
        if answer is None:
            target.update(status="failed", p=None)
        else:
            target.update(status="flagged" if answer.yes >= cfg.jev.injection else "passed", p=round(answer.yes, 3))


def screening(cfg: config.Config, ref: str, judged: bool, workers: list[dict], sources: list[dict]) -> None:
    items = [({"url": s["url"], "title": f["title"], "question": f["question"], "passage": f["passage"],
               "uncertainty": f["uncertainty"]}, f["screen"]) for s in sources for f in s["findings"]]
    items += [(w["notes"], w["notes_screen"]) for w in workers if w["notes"]]
    screen(cfg, ref, judged, items)
    for source in sources:
        for finding in source["findings"]:
            if finding["screen"]["status"] in PASSED_ON:
                for key in ("title", "published", "event_date"):
                    source[key] = source[key] or finding[key]


def totals(workers: list[dict], sources: list[dict]) -> dict:
    findings = [f for s in sources for f in s["findings"]]
    held = [f["screen"]["status"] for f in findings] + [w["notes_screen"]["status"] for w in workers]
    sums = {key: 0.0 if key == "cost_usd" else 0 for key in USAGE_KEYS}
    paid = Decimal(0)
    for w in workers:
        paid += dec(w["paid_usd"])
        for key in USAGE_KEYS:
            sums[key] = research_plan.plus(sums[key], w[key]) if key == "cost_usd" else sums[key] + w[key]
    count = {status: sum(1 for w in workers if w["status"] == status) for status in ("ok", "failed", "cancelled")}
    return {"tasks": len(workers), **count, "sources": len(sources), "findings": len(findings),
            "withheld": sum(1 for status in held if status in WITHHELD), "paid_usd": float(paid), **sums,
            "seconds": round(sum(w["seconds"] for w in workers), 1)}


def document(cfg: config.Config, s: steps.Settings, ref: str, status: str, stop: str, limits: dict,
             workers: list[dict], sources: list[dict], ids: dict[str, str]) -> dict:
    return {"ref": ref, "at": util.iso(), "status": status, "stop": stop, "runner": s.runner, "model": s.model,
            "tools": list(s.tools), "limits": {"workers": cfg.research_search_workers,
                                               "share": cfg.research_search_share, **limits},
            "workers": workers, "sources": sources, "ids": ids, "totals": totals(workers, sources)}


def say(ref: str, line: str) -> None:
    print(f"{util.iso()} {ref}: {line}", flush=True)


def worker_line(ref: str, w: dict) -> None:
    say(ref, f"search task {w['task']} {w['status']}: {util.count(len(w['sources']), 'source')}, "
             f"{util.count(w['tries'], 'try', 'tries')}, {util.money(w['paid_usd'], 3)} paid, {w['seconds']}s"
             + (f": {w['error']}" if w["error"] else ""))


def run(cfg: config.Config, budget: research_plan.Budget, plan: dict, ref: str, judged: bool) -> Found:
    s = steps.resolve(cfg, None, "research_search")
    ids = previous_ids(ref)
    if reason := unavailable(s):
        evidence = document(cfg, s, ref, "unavailable", reason, {"usd": 0.0, "seconds": 0}, [], [], ids)
        util.write_json(evidence_file(ref), evidence)
        return Found(f"search is unavailable: {reason}, so no search ran and no report was written", evidence)
    left_usd, left_seconds = budget.final()
    share = cfg.research_search_share
    cap_usd = min(share * left_usd, left_usd - budget.reserve_usd)
    cap_seconds = min(share * left_seconds, left_seconds - budget.reserve_seconds)
    room = not budget.short(cap_usd, cap_seconds - GRACE_SECONDS)
    tasks, size = plan["tasks"], cfg.research_search_workers
    cap = dec(cap_usd) if room else Decimal(0)
    stage_deadline = time.monotonic() + cap_seconds if room else 0.0
    say(ref, f"searching: {util.count(len(tasks), 'task')}, {size} workers, {s.runner} {s.model}, "
             f"tools {', '.join(s.tools)}, cap {util.money(float(cap))} and {round(cap_seconds / 60) if room else 0} minutes")
    cwd = FOLDER / "search" / tracker.safe_filename(ref)
    cwd.mkdir(parents=True, exist_ok=True)
    outcomes: dict[int, Outcome] = {}
    allowances: dict[int, Decimal] = {}
    pending = list(range(1, len(tasks) + 1))
    running: dict = {}
    spent = committed = Decimal(0)
    status, stop = ("done", "") if room else ("capped", "planning left no room for search")
    floor = dec(runner.BUDGET_FLOOR_USD)
    with ThreadPoolExecutor(size) as pool:
        while room and (pending or running):
            while pending and not stop and len(running) < size:
                allowance = (cap - spent - committed) / min(size - len(running), len(pending))
                seconds = min(s.timeout_minutes * 60, stage_deadline - time.monotonic())
                if allowance <= floor or seconds < research_plan.MIN_CALL_SECONDS + GRACE_SECONDS:
                    status, stop = "capped", CAP_REACHED
                    break
                n = pending.pop(0)
                committed += allowance
                allowances[n] = allowance
                running[pool.submit(work, cfg, s, plan, ref, n, cwd, float(allowance), seconds)] = n
            if not running:
                break
            for future in wait(running, return_when=FIRST_COMPLETED)[0]:
                n = running.pop(future)
                committed -= allowances[n]
                try:
                    out = future.result()
                except Exception as e:
                    note = (f"a search worker failed with {util.first_line(str(e)) or type(e).__name__}, "
                            "so its charge is unknown")
                    out = Outcome({**skeleton(n, tasks[n - 1]), "status": "failed", "error": note}, fatal=note)
                out.worker["allowance_usd"] = float(allowances[n])
                outcomes[n] = out
                spent += dec(out.paid)
                if out.fatal:
                    budget.error = budget.error or out.fatal
                if (out.fatal or out.limited) and not stop:
                    status, stop = "stopped", out.fatal or out.limited
    for n in pending:
        outcomes[n] = Outcome({**skeleton(n, tasks[n - 1]), "error": stop})
    budget.add(float(spent))
    budget.elapsed()
    sources = collect(outcomes, ids)
    workers = [outcomes[n].worker for n in sorted(outcomes)]
    screening(cfg, ref, judged, workers, sources)
    limits = {"usd": float(cap), "seconds": round(cap_seconds) if room else 0}
    evidence = document(cfg, s, ref, status, stop, limits, workers, sources, ids)
    util.write_json(evidence_file(ref), evidence)
    for w in workers:
        worker_line(ref, w)
    total = evidence["totals"]
    say(ref, f"search {status}: {util.money(total['paid_usd'])} paid of {util.money(float(cap))}, "
             f"{total['withheld']} withheld" + (f"; {stop}" if stop else ""))
    say(ref, f"search evidence {paths.short_path(evidence_file(ref))}")
    return Found(verdict(budget, room, stop, workers, evidence), evidence)


def passed_on(evidence: dict) -> list[tuple[dict, dict]]:
    return [(s, f) for s in evidence["sources"] for f in s["findings"] if f["screen"]["status"] in PASSED_ON]


def verdict(budget: research_plan.Budget, room: bool, stop: str, workers: list[dict], evidence: dict) -> str:
    if budget.error:
        return budget.error + NO_REPORT
    if not room:
        return f"{stop}{NO_REPORT}"
    if passed_on(evidence):
        return ""
    if evidence["totals"]["findings"]:
        return f"the injection screen withheld every note{NO_REPORT}"
    first = next((w["error"] for w in workers if w["error"]), "")
    return f"no search task returned a usable source{': ' + first if first else ''}{NO_REPORT}"


def brief(plan: dict, evidence: dict) -> str:
    kept = []
    for source, _ in passed_on(evidence):
        if source["id"] not in [k["id"] for k in kept]:
            kept.append({**{k: source[k] for k in ("id", "url", "title", "published", "event_date", "retrieved")},
                         "findings": [{k: f[k] for k in ("task", "question", "passage", "uncertainty")}
                                      for f in source["findings"] if f["screen"]["status"] in PASSED_ON]})
    notes = [{"task": w["task"], "text": w["notes"]} for w in evidence["workers"]
             if w["notes"] and w["notes_screen"]["status"] in PASSED_ON]
    tasks = "\n".join(f"{w['task']}. {w['goal']} ({w['status']}; sources: {', '.join(w['sources']) or 'none'})"
                      for w in evidence["workers"])
    gaps = [f"- task {w['task']} {w['status']}: {w['goal']}"
            for w in evidence["workers"] if w["status"] != "ok"]
    if withheld := evidence["totals"]["withheld"]:
        gaps.append(f"- {util.count(withheld, 'note')} withheld by the injection screen and not shown")
    return prompt("research_evidence", questions=research_plan.bullets(plan["questions"]),
                  criteria=research_plan.bullets(plan["source_criteria"]), claims=research_plan.bullets(plan["claims"]),
                  tasks=tasks, sources=json.dumps({"sources": kept, "notes": notes}, indent=2),
                  gaps="\n".join(gaps) or "none")


def paid_words(paid: float) -> str:
    return util.money(paid) + (f" ({paid:g})" if round(paid, 2) != paid else "")


def finding_lines(f: dict, path: Path) -> list[str]:
    screen = f["screen"]
    chance = f" (p {screen['p']})" if screen.get("p") is not None else ""
    if screen["status"] in WITHHELD:
        return [f"  - task {f['task']}: {screen['status']}{chance}, withheld; the text is in {paths.short_path(path)}"]
    return [f"  - task {f['task']}: {screen['status']}{chance}", f"    question: {f['question']}",
            f"    passage: {f['passage']}", f"    uncertainty: {f['uncertainty']}"]


def render(e: dict, path: Path) -> str:
    totals_ = e["totals"]
    lines = [f"search {e['status']} for {e['ref']}" + (f": {e['stop']}" if e["stop"] else "")
             + f" ({e['runner']} {e['model']}, {e['at']})"]
    for w in e["workers"]:
        lines.append(f"task {w['task']} {w['status']}: {w['goal']} ({util.count(len(w['sources']), 'source')}, "
                     f"{util.count(w['tries'], 'try', 'tries')}, {paid_words(w['paid_usd'])} paid, {w['seconds']}s)"
                     + (f": {w['error']}" if w["error"] else ""))
        if w["notes"]:
            held = w["notes_screen"]["status"] in WITHHELD
            lines.append(f"  notes ({w['notes_screen']['status']}): "
                         + (f"withheld; the text is in {paths.short_path(path)}" if held else w["notes"]))
    lines.append(f"total: {paid_words(totals_['paid_usd'])} paid, {totals_['tokens_in']} tokens in, "
                 f"{totals_['tokens_out']} out, {util.count(totals_['turns'], 'turn')}, "
                 f"{util.count(totals_['sources'], 'source')}, {util.count(totals_['findings'], 'finding')}, "
                 f"{totals_['withheld']} withheld")
    for source in e["sources"]:
        lines += ["", f"{source['id']} {source['url']}", f"  title: {source['title'] or 'not stated'}",
                  (f"  published: {source['published'] or 'not stated'}, "
                   f"event: {source['event_date'] or 'not stated'}, retrieved: {source['retrieved']}"),
                  "  tasks: " + ", ".join(str(n) for n in source["tasks"])]
        for f in source["findings"]:
            lines += finding_lines(f, path)
    return "\n".join(lines)


def shown(ref: str) -> str | None:
    path = evidence_file(ref)
    try:
        return render(json.loads(path.read_text()), path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return f"cannot read {paths.short_path(path)}: {util.first_line(str(e))}"
