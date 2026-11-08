from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from . import config, state, util

NEAR = 0.1
REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")


def scored(p: float, line: float, over: bool = True) -> dict:
    return {"p": round(float(p), 4), "line": line, "over": over}


def holds(score: dict) -> bool:
    return score["p"] >= score["line"] if score["over"] else score["p"] < score["line"]


def distance(score: dict) -> float:
    return abs(score["p"] - score["line"])


def record(run_id: str, kind: str, subject: str, about: str, scores: dict[str, dict], held: str,
           outcome: str = "") -> None:
    if run_id:
        state.add_verdict(run_id, kind, subject, about, scores, held, outcome)


def reverted_shas(cfg: config.Config, since: str) -> dict[str, str]:
    found = {}
    for repo in cfg.repos:
        if not (repo.path / ".git").exists():
            continue
        log = util.git(["log", "--all", f"--since={since}", "--grep=This reverts commit", "--format=%H%x1f%B%x1e"],
                       repo.path, check=False)
        for entry in filter(str.strip, log.split("\x1e")):
            revert, _, body = entry.strip().partition("\x1f")
            for sha in REVERTS.findall(body):
                found[sha] = revert
    return found


def scored_row(r: dict, question: str, score: dict) -> dict:
    return {"run_id": r["run_id"], "loop": r["loop"] or "", "kind": r["kind"], "question": question,
            "p": score["p"], "line": score["line"], "holds_over": score["over"], "about": r["about"] or "",
            "subject": r["subject"] if r["kind"] == "fixed" else "", "outcome": r["outcome"] or "open",
            "approval": r["approval"], "sha": r["sha"] or ""}


def report(days: float, cfg: config.Config) -> dict:
    since = util.iso(util.now() - timedelta(days=days))
    rows = state.verdicts_since(since)
    kinds: dict[str, dict] = {}
    questions: dict[str, dict] = {}
    near, close = [], []
    for r in rows:
        k = kinds.setdefault(r["kind"], {"verdicts": 0, "held": 0, "outcomes": Counter()})
        k["verdicts"] += 1
        k["held"] += bool(r["held"])
        k["outcomes"][r["outcome"] or "open"] += 1
        for name, score in r["answers"].items():
            q = questions.setdefault(f"{r['kind']} {name}", {"asked": 0, "held": 0, "outcomes": Counter()})
            q["asked"] += 1
            if holds(score):
                q["held"] += 1
                q["outcomes"][r["outcome"] or "open"] += 1
                if distance(score) < NEAR:
                    close.append(scored_row(r, name, score))
            elif distance(score) <= NEAR:
                near.append(scored_row(r, name, score))
    landed = {r["sha"]: r for r in rows if r["sha"]}
    reverts = reverted_shas(cfg, since) if landed else {}
    reverted = [{"run_id": r["run_id"], "loop": r["loop"] or "", "about": r["about"] or "", "sha": full,
                 "approval": r["approval"], "reverted_by": by}
                for sha, by in reverts.items() for full, r in landed.items() if full.startswith(sha)]
    return {"since": since, "days": days,
            "kinds": {k: {**v, "outcomes": dict(v["outcomes"])} for k, v in sorted(kinds.items())},
            "questions": {k: {**v, "outcomes": dict(v["outcomes"])} for k, v in sorted(questions.items())},
            "close_holds": sorted(close, key=lambda x: (x["kind"], x["question"], x["p"])),
            "near_misses": sorted(near, key=lambda x: (x["kind"], x["question"], x["p"])),
            "reverted": reverted}


def counted(outcomes: dict[str, int]) -> str:
    return ", ".join(f"{name or 'open'} {outcomes[name]}" for name in sorted(outcomes, key=lambda n: -outcomes[n]))


def where(x: dict) -> str:
    parts = [x["run_id"]]
    if x["approval"]:
        parts.append(f"approval #{x['approval']}")
    if x["sha"]:
        parts.append(x["sha"][:10])
    if x.get("subject"):
        parts.append(x["subject"])
    return ", ".join(parts)


def scored_line(x: dict) -> str:
    side = "at or over" if x["holds_over"] else "under"
    return (f"  {x['kind']} {x['question']} p {x['p']:.2f}, holds {side} {x['line']:g}: {x['about'] or 'no title'} "
            f"({where(x)}) -> {x['outcome']}")


def render(data: dict) -> str:
    days = data["days"]
    out = [f"JEV verdicts in the last {days:g} {'day' if days == 1 else 'days'} "
           f"(since {data['since'][:16].replace('T', ' ')})", ""]
    if not data["kinds"]:
        return "\n".join(out + ["No verdicts yet."])
    for kind, k in data["kinds"].items():
        out.append(f"{kind:<6} {util.count(k['verdicts'], 'verdict')}, {k['held']} held: {counted(k['outcomes'])}")
    out += ["", "Holds by question, and what came of them:"]
    for name, q in data["questions"].items():
        if q["held"]:
            out.append(f"  {name:<20} held {q['held']} of {q['asked']}: {counted(q['outcomes'])}")
    if not any(q["held"] for q in data["questions"].values()):
        out.append("  none")
    for title, key in ((f"Close holds (held within {NEAR:g} of the line):", "close_holds"),
                       (f"Near misses (passed within {NEAR:g} of the line):", "near_misses")):
        out += ["", title] + ([scored_line(x) for x in data[key]] or ["  none"])
    out += ["", "Landed, then reverted:"] + ([f"  {x['loop']}: {x['about'] or 'no title'} ({where(x)}) reverted by "
                                              f"{x['reverted_by'][:10]}" for x in data["reverted"]] or ["  none"])
    return "\n".join(out)
