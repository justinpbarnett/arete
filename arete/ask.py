from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from . import config, guard, jev, paths

NOTE = ("You can run `{ask}`, which puts a question to JEV, a fast classifier that costs next to nothing, over text you "
        "pipe in or over files you name, and gives an answer and a confidence for each. Use it to sort or check many "
        "things before you spend reads on them: `{ask} \"Does this file check permissions?\" src -r --only yes` lists "
        "the files that do; `<test command> 2>&1 | tail -200 | {ask} \"Why does this run fail?\" --choice "
        "\"code=the code under test is wrong\" \"test=the test itself is wrong\" \"setup=the environment or setup is "
        "wrong\"` sorts a failure before you dig in; `git diff | {ask} \"Does this diff do only what the task asks?\"` "
        "checks your work. Give --score with levels from lowest to highest for a rating. Put the paths before --choice "
        "or --score. JEV cannot count, compare dates or reason through negation, and it leans to the first choice, so "
        "read the code yourself before you change it or rest a decision on an answer.")


def command() -> str:
    return "arete ask" if shutil.which("arete") else f"{shlex.quote(str(Path(sys.executable).parent / 'arete'))} ask"


def rule() -> str:
    return f"Bash({command()} *)"


def note() -> str:
    return NOTE.format(ask=command())


def question(text: str, choices: list[str], levels: list[str]) -> dict:
    if choices and levels:
        raise ValueError("give --choice or --score, not both")
    if len(choices) == 1 or len(levels) == 1:
        raise ValueError("give at least two choices or levels")
    if choices:
        options = {label.strip(): about.strip() or label.strip()
                   for label, _, about in (c.partition("=") for c in choices)}
        return jev.pick(text, options)
    return jev.rate(text, levels) if levels else jev.yes_no(text)


def label(a: jev.Answer, q: dict) -> str:
    if q["type"] == "noul":
        return "yes" if a.value else "no"
    if q["type"] == "choice":
        return str(a.value)
    levels = q["criteria"]
    return levels[round(float(a.value) * (len(levels) - 1))]


def main(args) -> int:
    cfg = config.load()
    try:
        q = question(args.question, args.choice or [], args.score or [])
        files = jev.files_matching(args.paths, args.recursive) if args.paths else []
    except ValueError as e:
        return fail(str(e))
    if args.paths and not files:
        return fail("no files match")
    here = Path.cwd().resolve()
    inside = os.environ.get("ARETE_AGENT") != "1"
    refused = {f: "looks like a secret file" if guard.secret_path(f) else "outside the working directory"
               for f in files if guard.secret_path(f) or not (inside or here in f.resolve().parents)}
    files = [f for f in files if f not in refused]
    if not cfg.jev_allowed([cfg.repo_for_work_dir(here), *map(cfg.repo_for_work_dir, files)]):
        return fail("JEV is off for this repo in the arete config")
    if not jev.key():
        return fail(f"no {jev.KEY_NAME}; put it in {paths.short_path(jev.KEY_FILE)}")
    if args.paths:
        rows = asked_files(files, q, cfg) + [{"path": str(f), "skipped": why} for f, why in refused.items()]
    elif sys.stdin.isatty():
        return fail("give paths, or pipe the text in")
    else:
        rows = [row("", jev.ask(sys.stdin.read(), {"q": q}, cfg, trace=("", "ask")), q)]
    if args.only:
        rows = [r for r in rows if r.get("answer") == args.only]
    if args.json:
        print(json.dumps(rows if args.paths else rows[0], indent=2))
    else:
        for r in rows:
            print("\t".join(filter(None, [r["path"], shown(r)])))
    return 1 if any("error" in r for r in rows) else 0


def asked_files(files: list[Path], q: dict, cfg: config.Config) -> list[dict]:
    texts = [(f, jev.readable(f)) for f in files]
    readable = [(f, t) for f, t in texts if t is not None]
    replies = jev.ask_many([(f"File: {f}\n\n{t}", {"q": q}) for f, t in readable], cfg, trace=("", "ask"))
    return ([row(str(f), r, q) for (f, _), r in zip(readable, replies, strict=True)]
            + [{"path": str(f), "skipped": "binary or unreadable"} for f, t in texts if t is None])


def row(path: str, reply: jev.Reply, q: dict) -> dict:
    a = reply.answers.get("q")
    if a is None:
        return {"path": path, "error": reply.error or "no answer"}
    return {"path": path, "answer": label(a, q), "confidence": round(a.confidence, 3),
            "probabilities": {k: round(v, 3) for k, v in a.probabilities.items()}}


def shown(r: dict) -> str:
    if "answer" in r:
        return f"{r['answer']} (confidence {r['confidence']:.2f})"
    return f"skipped: {r['skipped']}" if "skipped" in r else f"error: {r['error']}"


def fail(why: str) -> int:
    print(f"arete ask: {why}", file=sys.stderr)
    return 2
