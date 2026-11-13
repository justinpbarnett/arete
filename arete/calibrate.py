from __future__ import annotations

import hashlib
import json
import math
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from . import config, environment, jev, paths, research, triage, util

DUPLICATE_ASK = ("Is this new finding the same problem as the known finding, even when it is worded differently or "
                 "found at another time?")
QUESTIONS = {
    "kind": (triage.ASKS_FOR_CODE, "fallback"),
    "duplicate": (DUPLICATE_ASK, "no"),
    "refresh": (research.CHANGED, "yes"),
    "risk": (jev.IRREVERSIBLE, "yes"),
}
OUTCOMES = ("answered", "abstained", "timeout", "malformed", "failed")
OUTCOME_WORDS = {"answered": "answered", "abstained": "abstained", "timeout": "timed out", "malformed": "malformed",
                 "failed": "failed"}
SPLITS = ("tune", "held_out")
LINES = [i / 100 for i in range(5, 100, 5)]
LABEL_KEYS = {"id", "text", "label", "split", "reviewed", "note"}
ANSWER_KEYS = {"question", "id", "response", "tokens_in", "tokens_out", "seconds", "cost_usd", "timeout"}
PROBABILITY_KEYS = ("p", "probability", "noul")
SPEC_KEYS = {"min_answered", "min_examples", "question", "endpoint"}
QUESTION_KEYS = {"examples", "ask", "false_positive_cost", "false_negative_cost", "fallback_cost"}
ENDPOINT_KEYS = {"transport", "answers", "command", "model", "response", "timeout_seconds", "usd_per_million_in",
                 "usd_per_million_out", "lines"}
LIVE_LINES = ("triage_skip", "confidence", "research_rerun", "risk_hold", "irreversible_hold")
APPLIES = "nothing: a person edits [jev] by hand"
SAMPLE_LEVELS = (0.1, 0.3, 0.5, 0.7, 0.9)
SAMPLE_REVIEWED = "fixture label, not a real sample"
SAMPLE_ENDPOINTS = {
    "calibrated": ("probability", True), "overconfident": ("probability", True),
    "abstaining": ("verdict", False), "malformed": ("probability", False),
}
PREDICTION_KEYS = ("endpoint", "fingerprint", "question", "id", "outcome", "p", "tokens_in", "tokens_out", "seconds",
                   "cost_usd", "detail", "billed")
USAGE_KEYS = ("tokens_in", "tokens_out", "cost_usd")
ENVELOPE_KEYS = {"response", *USAGE_KEYS}
LIMIT = 1e9
TIMEOUT_LIMIT = 3600
MAX_DIGITS = 300


def problem(text: str) -> ValueError:
    return ValueError(text)


def check_keys(found, allowed: set, where: str) -> None:
    extra = sorted(set(found) - allowed)
    if extra:
        raise ValueError(f"{where} holds {', '.join(extra)}; use {', '.join(sorted(allowed))}")


def whole(digits: str):
    return int(digits) if len(digits) <= MAX_DIGITS else float(digits)


def parsed(text: str):
    try:
        return json.loads(text, parse_int=whole)
    except RecursionError as e:
        raise ValueError("nested too deeply") from e


def figure(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def amount(value) -> float | None:
    value = figure(value)
    return value if value is not None and 0 <= value <= LIMIT else None


def number(value, where: str, high: float = LIMIT) -> float:
    found = figure(value)
    if found is None or found < 0 or found > high:
        raise ValueError(f"{where} must be a number from 0 to {high:g}")
    return found


def table(data: dict, name: str) -> dict:
    found = data.get(name, {})
    if not isinstance(found, dict) or not all(isinstance(v, dict) for v in found.values()):
        raise ValueError(f"[{name}] must hold named tables")
    return found


def fingerprint(endpoint: dict) -> str:
    source = endpoint["answers"] if endpoint["transport"] == "fixture" else endpoint["command"]
    text = json.dumps([endpoint["name"], endpoint["transport"], endpoint["model"], endpoint["response"], str(source)])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load_endpoint(name: str, raw: dict, here: Path, questions: dict) -> dict:
    where = f"[endpoint.{name}]"
    check_keys(raw, ENDPOINT_KEYS, where)
    transport = raw.get("transport")
    if transport not in ("fixture", "command"):
        raise ValueError(f"{where} transport must be fixture or command")
    own, other = ("answers", "command") if transport == "fixture" else ("command", "answers")
    if own not in raw or other in raw:
        raise ValueError(f"{where} with transport {transport} needs {own} and not {other}")
    if transport == "fixture":
        if not isinstance(raw["answers"], str):
            raise ValueError(f"{where} answers must be a path")
    elif not (isinstance(raw["command"], list) and raw["command"] and all(isinstance(a, str) for a in raw["command"])):
        raise ValueError(f"{where} command must be a list of words")
    response = raw.get("response", "probability")
    if response not in ("probability", "verdict"):
        raise ValueError(f"{where} response must be probability or verdict")
    lines = {}
    lines_table = raw.get("lines", {})
    if not isinstance(lines_table, dict):
        raise problem(f"{where} lines must be a table")
    for question, value in lines_table.items():
        if question not in QUESTIONS:
            raise ValueError(f"unknown question {question}; use {', '.join(QUESTIONS)}")
        if question not in questions:
            raise ValueError(f"{where} has a line for {question}, which the spec does not define")
        lines[question] = number(value, f"{where} line for {question}", 1)
    endpoint = {
        "name": name, "transport": transport, "model": str(raw.get("model", "")), "response": response,
        "answers": here / raw["answers"] if transport == "fixture" else None,
        "command": raw["command"] if transport == "command" else None, "dir": here,
        "timeout": number(raw.get("timeout_seconds", 30), f"{where} timeout_seconds", high=TIMEOUT_LIMIT),
        "rate_in": number(raw.get("usd_per_million_in", 0), f"{where} usd_per_million_in"),
        "rate_out": number(raw.get("usd_per_million_out", 0), f"{where} usd_per_million_out"), "lines": lines,
    }
    endpoint["fingerprint"] = fingerprint(endpoint)
    return endpoint


def load_spec(path: Path) -> dict:
    path = Path(path).resolve()
    try:
        data = tomllib.loads(path.read_text())
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e
    if "lines" in data:
        raise ValueError("a line belongs to one endpoint and question; put it under [endpoint.NAME.lines]")
    check_keys(data, SPEC_KEYS, "the spec")
    here = path.parent
    raw_questions = table(data, "question")
    for name in raw_questions:
        if name not in QUESTIONS:
            raise ValueError(f"unknown question {name}; use {', '.join(QUESTIONS)}")
    questions = {}
    for name in QUESTIONS:
        if name not in raw_questions:
            continue
        raw, where = raw_questions[name], f"[question.{name}]"
        check_keys(raw, QUESTION_KEYS, where)
        if not isinstance(raw.get("examples"), str):
            raise problem(f"{where} needs examples, a path to the labels file")
        questions[name] = {
            "examples": here / raw["examples"], "ask": str(raw.get("ask") or QUESTIONS[name][0]),
            "false_positive": number(raw.get("false_positive_cost", 1), f"{where} false_positive_cost"),
            "false_negative": number(raw.get("false_negative_cost", 1), f"{where} false_negative_cost"),
            "fallback": number(raw.get("fallback_cost", 0.5), f"{where} fallback_cost"),
        }
    endpoints = {name: load_endpoint(name, raw, here, questions) for name, raw in table(data, "endpoint").items()}
    if not questions or not endpoints:
        raise ValueError("the spec needs at least one [question.NAME] and one [endpoint.NAME]")
    return {"path": path, "dir": here, "questions": questions, "endpoints": endpoints,
            "min_answered": number(data.get("min_answered", 0.75), "min_answered", 1),
            "min_examples": int(number(data.get("min_examples", 20), "min_examples"))}


def jsonl(path: Path):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        where = f"{path}:{n}"
        try:
            row = parsed(line)
        except ValueError as e:
            raise ValueError(f"{where}: not JSON") from e
        if not isinstance(row, dict):
            raise problem(f"{where}: not a JSON object")
        yield where, row


def load_labels(path: Path) -> tuple[list[dict], int]:
    rows, unreviewed, ids, tune_texts, held = [], 0, set(), set(), []
    for where, row in jsonl(path):
        extra = sorted(set(row) - LABEL_KEYS)
        if extra:
            raise ValueError(f"{where}: holds {', '.join(extra)}; keep predictions out of the labels file")
        for key in ("id", "text", "label", "split"):
            if key not in row:
                raise ValueError(f"{where}: needs {key}")
        if not isinstance(row["id"], str) or not isinstance(row["text"], str):
            raise problem(f"{where}: id and text must be strings")
        if not isinstance(row["label"], bool):
            raise problem(f"{where}: label must be true or false")
        if row["split"] not in SPLITS:
            raise ValueError(f"{where}: split must be {' or '.join(SPLITS)}")
        if row["id"] in ids:
            raise ValueError(f"{where}: duplicate id {row['id']}")
        ids.add(row["id"])
        if row["split"] == "tune":
            tune_texts.add(row["text"])
        else:
            held.append((where, row["text"]))
        if str(row.get("reviewed") or "").strip():
            rows.append({k: row[k] for k in ("id", "text", "label", "split")})
        else:
            unreviewed += 1
    for where, text in held:
        if text in tune_texts:
            raise ValueError(f"{where}: this held out text is also in the tune split")
    return rows, unreviewed


def load_answers(path: Path) -> dict:
    found = {}
    for where, row in jsonl(path):
        extra = sorted(set(row) - ANSWER_KEYS)
        if extra:
            raise ValueError(f"{where}: holds {', '.join(extra)}; an answers file carries no labels")
        names = (row.get("question"), row.get("id"))
        if not all(isinstance(n, str) for n in names):
            raise ValueError(f"{where}: question and id must be strings")
        found[names] = row
    return found


def chance(value) -> tuple[str, float | None]:
    p = figure(value)
    return ("answered", round(p, 6)) if p is not None and 0 <= p <= 1 else ("malformed", None)


def read(response, shape: str) -> tuple[str, float | None]:
    if isinstance(response, str):
        try:
            response = parsed(response)
        except ValueError:
            return "malformed", None
    if response is None:
        return "abstained", None
    if shape == "probability":
        if isinstance(response, dict):
            if response.get("abstain") is True:
                return "abstained", None
            keys = [k for k in PROBABILITY_KEYS if k in response]
            if not keys:
                return "malformed", None
            response = response[keys[0]]
            if response is None:
                return "abstained", None
        return chance(response)
    if not isinstance(response, dict) or response.get("verdict") not in ("yes", "no", "abstain"):
        return "malformed", None
    if response["verdict"] == "abstain":
        return "abstained", None
    outcome, confidence = chance(response.get("confidence"))
    if confidence is None:
        return outcome, None
    return "answered", round(confidence if response["verdict"] == "yes" else 1 - confidence, 6)


def ask_fixture(endpoint: dict, answers: dict, question: str, example: dict) -> dict:
    row = answers.get((question, example["id"]))
    if row is None:
        return {"outcome": "failed", "detail": "no answer row"}
    raw = {k: amount(row.get(k)) for k in ("tokens_in", "tokens_out", "cost_usd", "seconds")}
    raw["seconds"] = raw["seconds"] or 0.0
    raw["response"] = row.get("response")
    if row.get("timeout") or raw["seconds"] > endpoint["timeout"]:
        raw.update(outcome="timeout", detail=f"no answer within {endpoint['timeout']:g}s")
    return raw


def ask_command(endpoint: dict, cfg: config.Config, question: str, ask: str, example: dict) -> dict:
    payload = {"endpoint": endpoint["name"], "model": endpoint["model"], "question": question, "ask": ask,
               "id": example["id"], "text": example["text"]}
    started = time.monotonic()
    res = environment.sh(endpoint["command"], cfg, cwd=endpoint["dir"], input=json.dumps(payload),
                         timeout=endpoint["timeout"])
    try:
        body = parsed(res.stdout)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = None
    raw = {"seconds": round(time.monotonic() - started, 3), **{k: amount((body or {}).get(k)) for k in USAGE_KEYS}}
    if res.returncode == util.TIMED_OUT:
        return raw | {"outcome": "timeout", "detail": f"no answer within {endpoint['timeout']:g}s"}
    if res.returncode != 0:
        lines = res.stderr.strip().splitlines()
        return raw | {"outcome": "failed", "detail": lines[-1] if lines else f"exit {res.returncode}"}
    if body is None or "response" not in body:
        return raw | {"outcome": "malformed", "detail": "stdout is not one JSON object with a response"}
    extra = sorted(set(body) - ENVELOPE_KEYS)
    if extra:
        return raw | {"outcome": "malformed",
                      "detail": f"stdout holds {', '.join(extra)}; use only {', '.join(sorted(ENVELOPE_KEYS))}"}
    return raw | {"response": body["response"]}


def call_cost(endpoint: dict, raw: dict) -> float:
    if raw.get("cost_usd") is not None:
        return raw["cost_usd"]
    tokens = (raw.get("tokens_in") or 0) * endpoint["rate_in"] + (raw.get("tokens_out") or 0) * endpoint["rate_out"]
    return tokens / 1e6


def run_endpoint(endpoint: dict, spec: dict, examples: dict, cfg: config.Config) -> list[tuple[dict, dict]]:
    answers = load_answers(endpoint["answers"]) if endpoint["transport"] == "fixture" else {}
    results = []
    for question, rows in examples.items():
        for example in rows:
            if endpoint["transport"] == "fixture":
                raw = ask_fixture(endpoint, answers, question, example)
            else:
                raw = ask_command(endpoint, cfg, question, spec["questions"][question]["ask"], example)
            if "outcome" in raw:
                outcome, p, detail = raw["outcome"], None, raw.get("detail", "")
            else:
                outcome, p = read(raw.get("response"), endpoint["response"])
                detail = "the response does not fit the declared shape" if outcome == "malformed" else ""
            results.append((example, {
                "endpoint": endpoint["name"], "fingerprint": endpoint["fingerprint"], "question": question,
                "id": example["id"], "outcome": outcome, "p": p, "tokens_in": raw.get("tokens_in"),
                "tokens_out": raw.get("tokens_out"), "seconds": raw.get("seconds") or 0.0,
                "cost_usd": call_cost(endpoint, raw), "detail": detail,
                "billed": any(raw.get(k) is not None for k in ("tokens_in", "tokens_out", "cost_usd")),
                "context": hashlib.sha256(example["text"].encode()).hexdigest()}))
    return results


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def question_costs(q: dict) -> tuple[float, float, float]:
    return q["false_positive"], q["false_negative"], q["fallback"]


def score(pairs: list, line: float, rule: str, costs: tuple[float, float, float]) -> dict | None:
    if not pairs:
        return None
    fp = fn = fallbacks = 0
    for example, record in pairs:
        if record["outcome"] == "answered":
            predicted = round(record["p"], 4) >= round(line, 4)
        elif rule == "fallback":
            fallbacks += 1
            continue
        else:
            predicted = rule == "yes"
        fp += predicted and not example["label"]
        fn += (not predicted) and example["label"]
    cost = (fp * costs[0] + fn * costs[1] + fallbacks * costs[2]) / len(pairs)
    return {"line": line, "cost": round(cost, 6), "false_positives": fp, "false_negatives": fn,
            "fallbacks": fallbacks}


def calibration(pairs: list) -> dict | None:
    answered = [(r["p"], e["label"]) for e, r in pairs if r["outcome"] == "answered"]
    if not answered:
        return None
    bins = [[] for _ in range(10)]
    for p, label in answered:
        bins[min(int(p * 10 + 1e-9), 9)].append((p, float(label)))
    ece = sum(len(b) / len(answered) * abs(mean([p for p, _ in b]) - mean([y for _, y in b])) for b in bins if b)
    gap = mean([max(p, 1 - p) for p, _ in answered]) - mean([float((p >= 0.5) == y) for p, y in answered])
    reads = "overconfident" if round(gap, 6) >= 0.1 else "underconfident" if round(gap, 6) <= -0.1 else "calibrated"
    return {"brier": round(mean([(p - y) ** 2 for p, y in answered]), 6), "ece": round(ece, 6),
            "confidence_gap": round(gap, 6), "reads": reads,
            "bins": [{"mean_p": round(mean([p for p, _ in b]), 4), "observed": round(mean([y for _, y in b]), 4),
                      "count": len(b)} for b in bins if b]}


def answered_share(pairs: list) -> float:
    return sum(r["outcome"] == "answered" for _, r in pairs) / len(pairs)


def choose(endpoint: dict, question: str, tune: list, held: list, tradeoffs: list, spec: dict) -> tuple:
    if not held:
        return None, "no held out examples for this question"
    if len(tune) < spec["min_examples"]:
        return None, f"{len(tune)} tune examples, fewer than the {spec['min_examples']} needed"
    share = min(answered_share(tune), answered_share(held))
    if share < spec["min_answered"]:
        return None, f"too few calls were answered ({share:.0%}, under {spec['min_answered']:.0%})"
    current = endpoint["lines"].get(question)
    centre = 0.5 if current is None else current
    best = min(tradeoffs, key=lambda row: (round(row["cost"], 9), round(abs(row["line"] - centre), 6)))
    rule, costs = QUESTIONS[question][1], question_costs(spec["questions"][question])
    at_best, at_current = score(held, best["line"], rule, costs), None
    if current is not None:
        at_current = score(held, current, rule, costs)
        if at_best["cost"] >= at_current["cost"]:
            return None, (f"the current line {current:.2f} does as well on held out "
                          f"(cost {at_current['cost']:.2f}, against {at_best['cost']:.2f} for {best['line']:.2f})")
    return {"line": best["line"], "endpoint": endpoint["name"], "model": endpoint["model"],
            "fingerprint": endpoint["fingerprint"], "question": question, "tune_cost": best["cost"],
            "held_out": at_best, "held_out_at_current": at_current}, None


def analyse(endpoint: dict, question: str, pairs: list, spec: dict) -> dict:
    rule, costs = QUESTIONS[question][1], question_costs(spec["questions"][question])
    split = {s: [(e, r) for e, r in pairs if e["split"] == s] for s in SPLITS}
    current = endpoint["lines"].get(question)
    tradeoffs = [score(split["tune"], line, rule, costs) for line in LINES] if split["tune"] else []
    if tradeoffs:
        proposed, why = choose(endpoint, question, split["tune"], split["held_out"], tradeoffs, spec)
    else:
        proposed, why = None, "no tune examples for this question"
    return {
        "line": current,
        "outcomes": {s: {o: sum(r["outcome"] == o for _, r in split[s]) for o in OUTCOMES} for s in SPLITS},
        "calibration": {s: calibration(split[s]) for s in SPLITS},
        "costs_at_line": None if current is None else {s: score(split[s], current, rule, costs) for s in SPLITS},
        "tradeoffs": tradeoffs, "proposed": proposed, "why_not": why,
    }


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)] if ordered else 0.0


def usage(records: list[dict]) -> dict:
    seen, repeats, repeat_tokens = set(), 0, 0
    for r in records:
        if r["context"] in seen:
            repeats += 1
            repeat_tokens += r["tokens_in"] or 0
        seen.add(r["context"])
    seconds = [r["seconds"] for r in records]
    return {
        "calls": len(records), "billed": sum(r["billed"] for r in records),
        "billed_unanswered": sum(r["billed"] and r["outcome"] != "answered" for r in records),
        "tokens_in": int(sum(r["tokens_in"] or 0 for r in records)),
        "tokens_out": int(sum(r["tokens_out"] or 0 for r in records)),
        "repeated_context_calls": repeats, "repeated_context_tokens": int(repeat_tokens),
        "seconds": round(sum(seconds), 3), "p50_seconds": percentile(seconds, 0.5),
        "p95_seconds": percentile(seconds, 0.95), "cost_usd": round(sum(r["cost_usd"] for r in records), 6),
    }


def compare(spec: dict, cfg: config.Config) -> tuple[dict, list[dict]]:
    examples, unreviewed = {}, 0
    for question, q in spec["questions"].items():
        examples[question], missed = load_labels(q["examples"])
        unreviewed += missed
    endpoints, calls = {}, []
    for name, endpoint in spec["endpoints"].items():
        results = run_endpoint(endpoint, spec, examples, cfg)
        calls += [r for _, r in results]
        endpoints[name] = {
            "fingerprint": endpoint["fingerprint"], "transport": endpoint["transport"], "model": endpoint["model"],
            "response": endpoint["response"], "usage": usage([r for _, r in results]),
            "questions": {q: analyse(endpoint, q, [(e, r) for e, r in results if r["question"] == q], spec)
                          for q in spec["questions"]}}
    data = {
        "written": datetime.now(UTC).isoformat(timespec="seconds"), "spec": str(spec["path"]),
        "applies": APPLIES,
        "live": {"endpoint": f"jev {cfg.jev.model}", "lines": {k: getattr(cfg.jev, k) for k in LIVE_LINES}},
        "labels": {"questions": {q: {s: sum(e["split"] == s for e in rows) for s in SPLITS}
                                 for q, rows in examples.items()}, "unreviewed": unreviewed},
        "questions": {q: {"false_positive_cost": v["false_positive"], "false_negative_cost": v["false_negative"],
                          "fallback_cost": v["fallback"],
                          "unanswered": {"fallback": "fallback"}.get(QUESTIONS[q][1], QUESTIONS[q][1])}
                      for q, v in spec["questions"].items()},
        "endpoints": endpoints}
    return data, calls


def cost_text(row: dict) -> str:
    return (f"cost {row['cost']:.2f} (false positives {row['false_positives']}, "
            f"false negatives {row['false_negatives']}, fallback {row['fallbacks']})")


def render(data: dict) -> str:
    labels = data["labels"]
    counts = {s: sum(v[s] for v in labels["questions"].values()) for s in SPLITS}
    total = sum(counts.values())
    head = (f"{total} reviewed examples, {counts['tune']} tune, {counts['held_out']} held out; "
            f"{labels['unreviewed']} unreviewed left out")
    out = [head]
    for question, q in data["questions"].items():
        costs = (f"false positive costs {q['false_positive_cost']:g}, false negative costs "
                 f"{q['false_negative_cost']:g}, fallback costs {q['fallback_cost']:g}")
        out += ["", f"{question}: {costs}; unanswered counts as {q['unanswered']}"]
        for name, e in data["endpoints"].items():
            r = e["questions"][question]
            out.append(f"  {name} ({e['model'] or 'no model'}, {e['fingerprint']})")
            for split, label in (("tune", "tune"), ("held_out", "held out")):
                seen = r["outcomes"][split]
                out.append(f"    {label}: " + ", ".join(f"{seen[o]} {OUTCOME_WORDS[o]}" for o in OUTCOMES))
            cal = r["calibration"]["held_out"]
            out.append("    calibration (held out): " + (
                f"Brier {cal['brier']:.3f}, ECE {cal['ece']:.3f}, confidence gap {cal['confidence_gap']:+.2f}, "
                f"reads {cal['reads']}" if cal else "no answered examples"))
            if r["line"] is None:
                out.append("    no line of its own")
            else:
                scored = r["costs_at_line"]["held_out"]
                out.append(f"    current line {r['line']:.2f}: " + (
                    f"held out {cost_text(scored)}" if scored else "nothing held out to score"))
            if r["proposed"]:
                p = r["proposed"]
                out.append(f"    proposed line {p['line']:.2f}: tune cost {p['tune_cost']:.2f}, "
                           f"held out {cost_text(p['held_out'])}")
            else:
                out.append(f"    no line proposed: {r['why_not']}")
            if r["tradeoffs"]:
                out.append("    tune cost by line: " + ", ".join(f"{t['line']:.2f} {t['cost']:.2f}"
                                                                  for t in r["tradeoffs"]))
    out += ["", "Usage by endpoint:"]
    for name, e in data["endpoints"].items():
        u = e["usage"]
        out.append(f"  {name}: {u['calls']} calls, {u['billed']} billed questions ({u['billed_unanswered']} without "
                   f"an answer), {u['tokens_in']} tokens in, {u['tokens_out']} out, {u['repeated_context_calls']} "
                   f"calls repeated a context already sent ({u['repeated_context_tokens']} tokens), "
                   f"{u['seconds']:.1f}s total, p50 {u['p50_seconds']:.2f}s, p95 {u['p95_seconds']:.2f}s, "
                   f"${u['cost_usd']:.4f}")
    return "\n".join(out)


def write(out: Path, data: dict, calls: list[dict]) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    proposal, predictions = out / "proposal.json", out / "predictions.jsonl"
    predictions.write_text("".join(json.dumps({k: c[k] for k in PREDICTION_KEYS}) + "\n" for c in calls))
    proposal.write_text(json.dumps(data, indent=2) + "\n")
    return proposal, predictions


def out_folder(args, spec: dict) -> Path:
    out = Path(args.out).resolve() if args.out else spec["dir"] / "out"
    if out in (paths.CONFIG_DIR.resolve(), paths.STATE_DIR.resolve()):
        raise ValueError(f"will not write into {out}; pick another --out")
    return out


def sample_answer(name: str, level: float, j: int) -> tuple[object, bool] | None:
    if name == "calibrated":
        return {"p": level}, False
    if name == "overconfident":
        return {"p": 0.02 if level < 0.5 or (level == 0.5 and j % 2) else 0.98}, False
    if name == "abstaining":
        if level == 0.5:
            return {"verdict": "abstain"}, False
        return {"verdict": "yes" if level > 0.5 else "no", "confidence": max(level, 1 - level)}, False
    if j in (0, 5):
        return "maybe", False
    if j in (1, 6):
        return {"p": 1.7}, False
    if j == 2:
        return {"p": level}, True
    return None if j == 7 else ({"p": level}, False)


def sample_examples() -> dict[str, list[dict]]:
    texts, examples = {}, {}
    for question in QUESTIONS:
        count, examples[question] = 0, []
        for split in SPLITS:
            for level in SAMPLE_LEVELS:
                for j in range(10):
                    count += 1
                    if question == "refresh" and level == 0.5:
                        text = texts[("duplicate", split, level, j)]
                    else:
                        text = f"Fixture {question} example {count}: a made up item, {split} split, level {level}"
                    texts[(question, split, level, j)] = text
                    examples[question].append({
                        "id": f"{question}-{split}-{level}-{j}", "text": text, "label": j < round(level * 10),
                        "split": split, "reviewed": SAMPLE_REVIEWED, "level": level, "j": j})
    return examples


def sample(where: Path) -> list[str]:
    where = Path(where).resolve()
    if where.exists() and any(where.iterdir()):
        raise FileExistsError(f"{where} is not empty")
    (where / "labels").mkdir(parents=True, exist_ok=True)
    (where / "answers").mkdir(parents=True, exist_ok=True)
    examples = sample_examples()
    spec = ["min_answered = 0.75", "min_examples = 20", ""]
    for question in QUESTIONS:
        (where / "labels" / f"{question}.jsonl").write_text("".join(
            json.dumps({k: e[k] for k in ("id", "text", "label", "split", "reviewed")}) + "\n"
            for e in examples[question]))
        spec += [f"[question.{question}]", f'examples = "labels/{question}.jsonl"', "false_positive_cost = 1",
                 f"false_negative_cost = {10 if question == 'risk' else 5}", "fallback_cost = 0.5", ""]
    for name, (shape, has_lines) in SAMPLE_ENDPOINTS.items():
        rows = []
        for question in QUESTIONS:
            for e in examples[question]:
                answer = sample_answer(name, e["level"], e["j"])
                if answer is None:
                    continue
                row = {"question": question, "id": e["id"], "response": answer[0],
                       "tokens_in": 40 + len(e["text"]) // 4, "tokens_out": 4 if shape == "probability" else 12,
                       "seconds": round(0.2 + 0.05 * (e["j"] % 5), 2)}
                if answer[1]:
                    row.update(timeout=True, seconds=30)
                rows.append(row)
        (where / "answers" / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        spec += [f"[endpoint.{name}]", 'transport = "fixture"', f'answers = "answers/{name}.jsonl"',
                 f'model = "fixture-{name}"', f'response = "{shape}"', "usd_per_million_in = 1",
                 "usd_per_million_out = 4", ""]
        if has_lines:
            spec += [f"[endpoint.{name}.lines]", *(f"{q} = 0.5" for q in QUESTIONS), ""]
    (where / "compare.toml").write_text("\n".join(spec))
    wrote = (f"# wrote {where / 'compare.toml'}, four labels files under {where / 'labels'} and four answers files "
             f"under {where / 'answers'}")
    return [wrote,
            "# everything in it is made up; real labels and endpoints stay in private configuration",
            f"# compare with: arete jev compare {where / 'compare.toml'}"]


def main(args) -> int:
    if not args.target:
        what = "a spec file" if args.action == "compare" else "a folder"
        print(f"arete jev {args.action}: needs {what}", file=sys.stderr)
        return 2
    if args.action == "sample":
        try:
            print("\n".join(sample(Path(args.target))))
        except OSError as e:
            print(f"arete jev sample: {e}", file=sys.stderr)
            return 1
        return 0
    try:
        cfg = config.load()
        spec = load_spec(Path(args.target))
        out = out_folder(args, spec)
        data, calls = compare(spec, cfg)
        proposal, predictions = write(out, data, calls)
    except (ValueError, OSError, OverflowError) as e:
        print(f"arete jev compare: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(render(data))
    print(f"\nProposal only: wrote {proposal} and {predictions}. No setting changed; [jev] lines stay as they are "
          "until a person edits the config.")
    return 0
