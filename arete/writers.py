from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import statistics
import time
import tomllib
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path

from . import config, document, paths, record, replay, steps, util

KINDS = ("message", "ticket", "doc")
MIN_CASES, MAX_CASES = 15, 20
ROUTE_KEYS = ("tier", "runner", "provider", "model", "effort")
NAME = re.compile(r"[A-Za-z0-9_-]+")
RUN_ID = re.compile(r"[A-Za-z0-9._-]+")
SCALES = ("naturalness", "brevity", "clarity")
RATINGS = (*SCALES, "edits", "seconds")
LIMITS = {"naturalness": (1, 5), "brevity": (1, 5), "clarity": (1, 5), "edits": (0, 3), "seconds": (0, None)}
HELP = {"naturalness": "1 to 5", "brevity": "1 to 5", "clarity": "1 to 5",
        "edits": "0 none, 1 light, 2 heavy, 3 rewrite", "seconds": "seconds you spent making it sendable"}
FLAWS = ("missing", "invented", "damaged")
AGGREGATE = "all"
NOTE = "Proposals only: arete changed no route, tier or config."
SHOWN = ("audience", "action", "facts", "acceptance", "ask", "owner", "deadline", "draft")


class Refused(ValueError):
    pass


@dataclass
class Case:
    name: str
    brief: document.Brief
    evidence: list[list[str]]


@dataclass
class Spec:
    raters: int
    candidates: dict[str, steps.Settings]
    cases: list[Case]
    doc: config.Document
    samples: list[str]


def read_toml(path: Path) -> dict:
    if not path.is_file():
        raise Refused(f"{path} is missing")
    return tomllib.loads(path.read_text())


def read_candidates(cfg: config.Config, layer: config.RepoLayer, table) -> dict[str, steps.Settings]:
    if not isinstance(table, dict) or not 2 <= len(table) <= 26:
        raise Refused("[candidates] needs 2 to 26 entries, one [candidates.NAME] table for each route")
    found = {}
    for name, entry in table.items():
        if not NAME.fullmatch(name):
            raise Refused(f"candidate name {name!r} may hold only letters, digits, - and _")
        if not (isinstance(entry, dict) and entry and set(entry) <= set(ROUTE_KEYS)):
            raise Refused(f"candidate {name}: set one or more of {', '.join(ROUTE_KEYS)}")
        try:
            given = config.step_table(entry, f"candidates.{name}")
            found[name] = steps.resolve(cfg, layer, "document", "", given)
        except ValueError as e:
            raise Refused(f"candidate {name}: {e}") from e
    routes = [(s.runner, s.model, s.effort) for s in found.values()]
    if len(set(routes)) < len(routes):
        raise Refused("two candidates resolve to the same runner, model and effort")
    return found


def read_evidence(value, count: int) -> list[list[str]]:
    if not (isinstance(value, list) and len(value) == count):
        raise ValueError(f"evidence must be a list with one entry for each of the {count} acceptance conditions")
    found = []
    for entry in value:
        entry = [entry] if isinstance(entry, str) else entry
        if not (isinstance(entry, list) and entry and all(isinstance(p, str) and p.strip() for p in entry)):
            raise ValueError("each evidence entry must be a non-empty list of non-empty phrases")
        found.append([p.strip() for p in entry])
    return found


def read_case(path: Path) -> Case:
    if not NAME.fullmatch(path.stem):
        raise Refused(f"case file {path.name}: the name may hold only letters, digits, - and _")
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise TypeError("the case must be one JSON object")
        evidence = data.pop("evidence", None)
        brief = document.brief_from(data)
        if brief.kind not in KINDS:
            raise ValueError(f"the kind {brief.kind!r} is not one of {', '.join(KINDS)}")
        if brief.channel == AGGREGATE:
            raise ValueError(f"the channel {AGGREGATE!r} is reserved for the report's total; name the channel")
        return Case(path.stem, brief, read_evidence(evidence, len(brief.acceptance)))
    except (ValueError, TypeError) as e:
        raise Refused(f"case {path.stem}: {e}") from e


def read_spec(cfg: config.Config, repo: config.Repo, layer: config.RepoLayer) -> Spec:
    if not (repo.layer and not repo.layer.resolve().is_relative_to(repo.path.resolve())):
        raise Refused("writer comparisons read only a layer outside the repo, so private messages and voice samples "
                      "never land in a public tree")
    folder = repo.layer_dir / "writers"
    data = read_toml(folder / "config.toml")
    if bad := sorted(set(data) - {"raters", "candidates"}):
        raise Refused(f"{folder / 'config.toml'}: unknown key {', '.join(bad)}; use raters, candidates")
    raters = data.get("raters", 2)
    if type(raters) is not int or raters < 1:
        raise Refused(f"raters must be a whole number from 1, not {raters!r}")
    candidates = read_candidates(cfg, layer, data.get("candidates"))
    files = sorted((folder / "cases").glob("*.json")) if (folder / "cases").is_dir() else []
    if not MIN_CASES <= len(files) <= MAX_CASES:
        raise Refused(f"{folder / 'cases'} holds {util.count(len(files), 'case')}; a comparison needs "
                      f"{MIN_CASES} to {MAX_CASES}")
    doc = config.document_for(cfg, repo, layer)
    return Spec(raters, candidates, [read_case(f) for f in files], doc, document.voice_lines(doc.voice))


def scored(brief: document.Brief, evidence: list[list[str]], text: str, title: str, samples: list[str]) -> dict:
    found: dict[str, list[str]] = {"missing": [], "invented": [], "damaged": [], "other": []}
    for group, said in document.fault_pairs(brief, text, samples, title):
        found[group].append(said)
    whole = f"{title}\n{text}"
    found["missing"] += [f"left out the condition: {condition}" for condition, phrases in zip(brief.acceptance, evidence)
                         if not any(document.phrase_in(p, whole) for p in phrases)]
    return found


def attempt(run_id: str, name: str, case: Case, s: steps.Settings, spec: Spec, where: tuple, prompt: str,
            target: tuple[int, int] | None) -> dict:
    cfg, repo, layer = where
    began, res, error = time.monotonic(), None, ""
    try:
        res = document.ask(prompt, s, cfg, repo, layer, None, trace=(run_id, f"writer.{name}.{case.name}"),
                           fallback=False)
    except (OSError, RuntimeError) as e:
        error = f"the call failed ({type(e).__name__})"
    seconds = time.monotonic() - began
    found = res.structured if res and res.ok and isinstance(res.structured, dict) else {}
    text = str(found.get("text") or "").strip()
    title = str(found.get("title") or "").strip() if case.brief.kind == "ticket" else ""
    failed = not text
    words = len(text.split())
    if res and not error:
        error = util.first_line(res.error) if not res.ok else ""
    return {"failed": failed, "error": error or ("the writer gave no text" if failed else ""), "title": title,
            "text": text, "words": words, "over_target": bool(target and words > target[1]),
            "seconds": round(seconds, 3), "charged_usd": res.paid_usd if res else 0.0,
            "reference_usd": res.reference_usd if res else None, "runner": (res.runner if res else "") or s.runner,
            "model": (res.model if res else "") or s.model,
            "scored": scored(case.brief, case.evidence, text, title, spec.samples)}


def run_path(run_id: str, *parts: str) -> Path:
    if not RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
        raise Refused(f"{run_id!r} is not a run id")
    return paths.RUNS_DIR.joinpath(run_id, *parts)


def run(repo_name: str, dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    repo = cfg.repo(repo_name)
    layer = config.load_layer(repo.layer_dir)
    spec = read_spec(cfg, repo, layer)
    if dry_run:
        for name, s in spec.candidates.items():
            print(f"{name}: runner {s.runner}, model {s.model}, effort {s.effort or 'default'}")
        kinds = ", ".join(f"{sum(c.brief.kind == k for c in spec.cases)} {k}" for k in KINDS)
        print(f"{util.count(len(spec.cases), 'case')}: {kinds}")
        return 0
    run_id = record.start("eval", tag=f"writers-{repo.name}", repo=repo.name)
    with record.failed_on_crash(run_id):
        chance = random.SystemRandom()
        saved = {"run": run_id, "repo": repo.name, "raters": spec.raters,
                 "candidates": {n: {"runner": s.runner, "model": s.model, "provider": s.provider,
                                    "effort": s.effort, "tier": s.tier} for n, s in spec.candidates.items()},
                 "cases": {}}
        calls = failed = 0
        charged = 0.0
        for case in spec.cases:
            target = spec.doc.words.get(case.brief.kind)
            prompt = document.fill(case.brief, spec.doc, spec.samples, target)
            order = list(spec.candidates)
            chance.shuffle(order)
            outputs = {name: attempt(run_id, name, case, s, spec, (cfg, repo, layer), prompt, target)
                       for name, s in spec.candidates.items()}
            calls += len(outputs)
            failed += sum(o["failed"] for o in outputs.values())
            charged += sum(o["charged_usd"] for o in outputs.values())
            saved["cases"][case.name] = {
                "kind": case.brief.kind, "channel": case.brief.channel or case.brief.kind,
                "brief": asdict(case.brief), "evidence": case.evidence, "target": target,
                "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest(),
                "labels": {name: chr(ord("A") + i) for i, name in enumerate(order)}, "outputs": outputs}
        util.write_json(run_path(run_id, "writers.json"), saved)
        record.settle(run_id, "done", summary=f"{util.count(calls, 'call')}, {failed} failed, ${charged:.4f} charged")
    print(json.dumps({"run": run_id, "repo": repo.name, "cases": len(spec.cases), "candidates": len(spec.candidates),
                      "calls": calls, "failed": failed, "charged_usd": round(charged, 6)}))
    return 1 if failed else 0


def load(run_id: str) -> dict:
    path = run_path(run_id, "writers.json")
    if not path.is_file():
        raise Refused(f"no writer comparison {run_id}; `arete writers run REPO` prints its id")
    return json.loads(path.read_text())


def shown(saved: dict, name: str) -> list[tuple[str, dict]]:
    case = saved["cases"][name]
    by_label = {case["labels"][c]: o for c, o in case["outputs"].items() if o["text"]}
    return sorted(by_label.items())


def export(run_id: str) -> dict:
    saved = load(run_id)
    cases = []
    for name, case in saved["cases"].items():
        texts = [{"label": label, "title": o["title"], "text": o["text"], **dict.fromkeys(RATINGS)}
                 for label, o in shown(saved, name)]
        cases.append({"case": name, "kind": case["kind"], "channel": case["channel"],
                      **{k: case["brief"][k] for k in SHOWN}, "texts": texts})
    return {"run": run_id, "rater": "", "scales": dict(HELP), "cases": cases}


def checked(value, key: str, where: str):
    if value is None:
        return None
    low, high = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise Refused(f"{where}: {key} {value!r} is not a number")
    whole = key != "seconds"
    if (whole and float(value) != int(value)) or value < low or (high is not None and value > high):
        scale = f"{low} to {high}" if high is not None else f"{low} or more"
        raise Refused(f"{where}: {key} {value!r} is outside the {scale} scale"
                      + (" of whole numbers" if whole and float(value) != int(value) else ""))
    return int(value) if whole else value


def full(values: dict) -> bool:
    return all(values.get(k) is not None for k in RATINGS)


def rate(run_id: str, file: str, rater: str = "") -> dict:
    saved = load(run_id)
    data = json.loads(Path(file).read_text())
    if not isinstance(data, dict):
        raise Refused("the rating file must be one JSON object")
    if data.get("run") != run_id:
        raise Refused(f"the rating file is for run {data.get('run')!r}, not {run_id}")
    rater = rater or data.get("rater") or ""
    if not (isinstance(rater, str) and NAME.fullmatch(rater)):
        raise Refused(f"rater name {rater!r} may hold only letters, digits, - and _; set it in the file or --rater")
    given: dict[str, dict[str, dict]] = {}
    for entry in data.get("cases") or []:
        name = entry.get("case") if isinstance(entry, dict) else None
        if name not in saved["cases"]:
            raise Refused(f"the rating file names an unknown case {name!r}")
        known = set(saved["cases"][name]["labels"].values())
        for text in entry.get("texts") or []:
            label = text.get("label") if isinstance(text, dict) else None
            if label not in known:
                raise Refused(f"case {name} has no label {label!r}")
            given.setdefault(name, {})[label] = {k: checked(text.get(k), k, f"case {name} text {label}")
                                                 for k in RATINGS}
    util.write_json(run_path(run_id, "ratings", f"{rater}.json"), {"run": run_id, "rater": rater, "ratings": given})
    texts = [(name, label) for name in saved["cases"] for label, _ in shown(saved, name)]
    rated = sum(full(given.get(name, {}).get(label, {})) for name, label in texts)
    return {"run": run_id, "rater": rater, "rated": rated, "missing": len(texts) - rated}


def ratings_of(run_id: str) -> dict[str, dict]:
    folder = run_path(run_id, "ratings")
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    return {f.stem: json.loads(f.read_text())["ratings"] for f in files}


def marks(given: dict, case: str, label: str) -> dict[str, dict]:
    return {rater: r[case][label] for rater, r in given.items() if label in r.get(case, {})}


def preference(values: dict) -> float | None:
    return statistics.fmean(values[k] for k in SCALES) if all(values.get(k) is not None for k in SCALES) else None


def mean(values) -> float | None:
    found = [v for v in values if v is not None]
    return statistics.fmean(found) if found else None


def interval(values: list[float]) -> list[float] | None:
    if len(values) < 2:
        return None
    centre, spread = statistics.fmean(values), 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return [centre - spread, centre + spread]


def order(a: float, b: float) -> int:
    d = round(a - b, 9)
    return (d > 0) - (d < 0)


def candidate_row(saved: dict, given: dict, names: list[str], candidate: str) -> tuple[dict, dict[str, float]]:
    found = [(n, saved["cases"][n]["outputs"][candidate], saved["cases"][n]["labels"][candidate]) for n in names]
    counts = {k: sum(len(o["scored"][k]) for _, o, _ in found) for k in FLAWS}
    incomplete = sum(1 for _, o, _ in found if o["failed"] or any(o["scored"][k] for k in FLAWS))
    series, every = {}, []
    for name, _, label in found:
        got = list(marks(given, name, label).values())
        every += got
        if (p := mean(preference(v) for v in got)) is not None:
            series[name] = p
    refs = [o["reference_usd"] for _, o, _ in found]
    row = {"texts": len(found), "failed": sum(o["failed"] for _, o, _ in found), **counts, "incomplete": incomplete,
           "can_win": incomplete == 0, **{k: mean(v.get(k) for v in every) for k in SCALES},
           "preference": mean(series.values()), "interval": interval(list(series.values())),
           "edits": mean(v.get("edits") for v in every), "writing_seconds": mean(v.get("seconds") for v in every),
           "words": mean(o["words"] for _, o, _ in found), "over_target": sum(o["over_target"] for _, o, _ in found),
           "charged_usd": sum(o["charged_usd"] for _, o, _ in found),
           "reference_usd": None if any(r is None for r in refs) else sum(refs),
           "seconds": mean(o["seconds"] for _, o, _ in found)}
    return row, series


def agreement(saved: dict, given: dict, names: list[str]) -> dict:
    same = compared = 0
    for name in names:
        labels = saved["cases"][name]["labels"]
        prefs = {r: {c: p for c, label in labels.items() if label in got.get(name, {})
                     and (p := preference(got[name][label])) is not None} for r, got in given.items()}
        for a, b in combinations(prefs.values(), 2):
            for x, y in combinations(sorted(set(a) & set(b)), 2):
                compared += 1
                same += order(a[x], a[y]) == order(b[x], b[y])
    return {"agreement": same / compared if compared and len(given) >= 2 else None, "compared": compared}


def proposal(rows: dict, series: dict, status: str, unrated: int) -> tuple[dict | None, str]:
    if status != "reviewed":
        return None, f"{util.count(unrated, 'text')} lack ratings from enough raters"
    eligible = sorted((c for c, r in rows.items() if r["can_win"] and r["preference"] is not None),
                      key=lambda c: -rows[c]["preference"])
    if not eligible:
        return None, "every candidate left out a requirement, invented a fact or damaged a command"
    best = eligible[0]
    found = {"candidate": best, "preference": rows[best]["preference"], "lead": None, "lead_interval": None,
             "clear": True}
    if len(eligible) > 1:
        diffs = [series[best][n] - series[eligible[1]][n] for n in series[best] if n in series[eligible[1]]]
        span = interval(diffs)
        found.update(lead=mean(diffs), lead_interval=span, clear=bool(span and span[0] > 0))
    return found, ""


def group(saved: dict, given: dict, needed: int, names: list[str]) -> dict:
    built = {c: candidate_row(saved, given, names, c) for c in saved["candidates"]}
    rows = {c: row for c, (row, _) in built.items()}
    unrated = sum(1 for n in names for label, _ in shown(saved, n)
                  if sum(full(v) for v in marks(given, n, label).values()) < needed)
    status = "unreviewed" if unrated else "reviewed"
    found, why = proposal(rows, {c: s for c, (_, s) in built.items()}, status, unrated)
    return {"cases": len(names), "raters": len(given), **agreement(saved, given, names), "status": status,
            "unrated": unrated, "candidates": rows, "proposal": found, "why": why}


def report(run_id: str) -> dict:
    saved = load(run_id)
    given = ratings_of(run_id)
    channels = {AGGREGATE: list(saved["cases"])}
    for name, case in saved["cases"].items():
        channels.setdefault(case["channel"], []).append(name)
    found = {k: group(saved, given, saved["raters"], names) for k, names in channels.items()}
    return {"run": run_id, "repo": saved["repo"], "result": found["all"]["status"], "needed_raters": saved["raters"],
            "raters": list(given), "candidates": saved["candidates"], "note": NOTE, "channels": found}


def figure(value, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def money(value) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def report_lines(found: dict) -> list[str]:
    lines = [f"writers {found['run']} ({found['repo']}): {found['result']}", found["note"],
             f"raters: {', '.join(found['raters']) or 'none'}; {found['needed_raters']} needed for each text"]
    for name, g in found["channels"].items():
        agree = "-" if g["agreement"] is None else f"{g['agreement']:.2f} over {g['compared']} comparisons"
        lines += ["", f"{name}: {util.count(g['cases'], 'case')}, {g['status']}"
                      + (f", {util.count(g['unrated'], 'text')} unrated" if g["unrated"] else "")
                      + f", {util.count(g['raters'], 'rater')}, agreement {agree}",
                  (f"  {'candidate':<12}{'missing':>8}{'invented':>9}{'damaged':>8}  {'preference':<22}{'edits':>6}"
                   f"{'write s':>8}{'words':>7}{'charged':>10}{'reference':>10}{'latency s':>10}")]
        for c, r in g["candidates"].items():
            span = f" ({figure(r['interval'][0])} to {figure(r['interval'][1])})" if r["interval"] else ""
            lines.append(f"  {c:<12}{r['missing']:>8}{r['invented']:>9}{r['damaged']:>8}  "
                         f"{figure(r['preference']) + span:<22}{figure(r['edits']):>6}{figure(r['writing_seconds'], 0):>8}"
                         f"{figure(r['words'], 0):>7}{money(r['charged_usd']):>10}{money(r['reference_usd']):>10}"
                         f"{figure(r['seconds'], 3):>10}")
        lines += [f"  cannot win: {c} ({util.count(r['incomplete'], 'incomplete text')})"
                  for c, r in g["candidates"].items() if not r["can_win"]]
        p = g["proposal"]
        if p:
            lead = "" if p["lead"] is None else f", leads by {figure(p['lead'])}" + (
                f" ({figure(p['lead_interval'][0])} to {figure(p['lead_interval'][1])})" if p["lead_interval"] else "")
            lines.append(f"  proposal: {p['candidate']}{lead}, {'clear' if p['clear'] else 'not clear'}")
        else:
            lines.append(f"  proposal: none, {g['why']}")
    return lines


TOPICS = (("the image cache", "make cache-clear", "cache"), ("the search index", "make index-rebuild", "index"),
          ("the job queue", "make queue-drain", "queue"), ("the test certs", "make certs-renew", "certs"),
          ("the seed data", "make seeds-load", "seeds"))
VOICE = ("Hey all, quick one from me and then I am off.", "Short and kind wins, and say the ask first.")
CANDIDATES = {"plain": (0.004, 0.03), "long": (0.009, 0.06), "terse": (0.001, 0.01)}
FILLER = {
    "message": " I know this is easy to forget and everyone has a full plate right now, so thank you for taking a minute "
               "for it. It is a small step, but it keeps things steady for the whole team and spares us a round of "
               "odd questions later on. If anything about it looks strange, say so here and someone will pick it up "
               "with you.",
    "ticket": " Right now people do it by hand, and it is easy to forget when a release is busy. The result is a stale "
              "copy that nobody notices until a customer does, and then we lose time finding the cause. Doing it in "
              "the deploy keeps the steps in one place, written down, and the same for every release that goes out.",
    "doc": " Nothing else is needed. This keeps the page short for the next person who opens it, who will most likely "
           "be in a hurry and wants the one step without the background, the history or the reasons it came about."}


def sample_case(kind: str, thing: str, command: str) -> tuple[dict, dict[str, dict]]:
    stale = f"{thing[0].upper()}{thing[1:]} goes stale after a deploy."
    if kind == "message":
        brief = {"kind": "message", "audience": "the team", "channel": "team chat",
                 "facts": [stale, f"Running `{command}` brings it back."],
                 "action": "Ask the team to run the command and confirm in the channel.",
                 "acceptance": ["Names the command to run.", "Asks for a confirmation in the channel."],
                 "evidence": [[command], ["confirm"]]}
        short = f"Heads up: {thing} goes stale after a deploy. Please run `{command}` and confirm here in the " \
                "channel when it is done."
        texts = {"plain": short, "long": short + FILLER[kind], "terse": f"Run `{command}` to refresh {thing}."}
        titles = dict.fromkeys(texts, "")
    elif kind == "ticket":
        brief = {"kind": "ticket", "audience": "the platform team", "channel": "tracker ticket",
                 "facts": [stale, f"`{command}` brings it back."],
                 "action": "Ask for the deploy to run the command itself.",
                 "acceptance": ["The deploy runs the command itself.", "A failed command stops the deploy."],
                 "evidence": [["deploy runs", "deploy run"], ["stops the deploy"]]}
        short = f"The deploy runs `{command}` itself once the release step ends, so nobody has to remember it. If " \
                "the command fails, the failure stops the deploy and the team is told."
        texts = {"plain": short, "long": short + FILLER[kind], "terse": f"The deploy runs `{command}` itself."}
        titles = {"plain": f"Clear {thing} after each deploy", "long": f"Clear {thing} after each deploy",
                  "terse": f"Clear {thing}"}
    else:
        draft = (f"Runbook note.\n\nThis step is here because {thing} can look out of date for a while. When that "
                 f"happens, and only then, you could consider running `{command}`, which refreshes it. In most cases "
                 "people tend to do this after a deploy, but it is not strictly required every time, and running it "
                 "twice does no harm.\n")
        brief = {"kind": "doc", "audience": "whoever is on call", "channel": "runbook, markdown",
                 "facts": [f"`{command}` refreshes {thing}.", "It is safe to run more than once."],
                 "action": "Tighten the runbook step.", "acceptance": ["Keeps the command.", "Says when to run it."],
                 "draft": draft, "evidence": [[command], ["after a deploy"]]}
        short = f"Run `{command}` after a deploy to refresh {thing}. It is safe to run twice."
        texts = {"plain": short, "long": short + FILLER[kind], "terse": f"Run `{command}`."}
        titles = dict.fromkeys(texts, "")
    answers = {}
    for name, (cost, seconds) in CANDIDATES.items():
        structured = {"text": texts[name], **({"title": titles[name]} if kind == "ticket" else {})}
        answers[name] = {"structured": structured, "cost_usd": cost, "seconds": seconds}
    return brief, answers


def sample(where: Path) -> list[str]:
    where = Path(os.path.abspath(where))
    if where.exists() and any(where.iterdir()):
        raise FileExistsError(f"{where} is not empty")
    repo, layer, answers = where / "repo", where / "layer", where / "replay"
    cases = layer / "writers" / "cases"
    for made in (repo, cases, answers):
        made.mkdir(parents=True)
    (layer / "config.toml").write_text('[document]\nvoice = ["voice.md"]\n')
    (layer / "voice.md").write_text("\n".join(VOICE) + "\n")
    routes = "\n".join(f'\n[candidates.{name}]\nmodel = "{replay.RUNNER}:{name}"' for name in CANDIDATES)
    (layer / "writers" / "config.toml").write_text(f"raters = 2\n{routes}\n")
    for kind in KINDS:
        for thing, command, topic in TOPICS:
            brief, replies = sample_case(kind, thing, command)
            (cases / f"{kind}-{topic}.json").write_text(json.dumps(brief, indent=2) + "\n")
            for name, reply in replies.items():
                (answers / f"writer.{name}.{kind}-{topic}.json").write_text(json.dumps(reply, indent=2) + "\n")
    return [f"# wrote {repo}, {layer} and {answers}",
            f"# add these lines to {paths.short_path(paths.CONFIG_FILE)}, best in a scratch {replay.SCRATCH_CONFIG}:",
            "", "[replay]", "all = true", f"dir = {json.dumps(str(answers))}", "",
            "[[repo]]", 'name = "writers-sample"', f"path = {json.dumps(str(repo))}",
            f"layer = {json.dumps(str(layer))}", 'tracker = "local"', "jev = false"]
