from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from . import asks, config, paths, record, state, util
from .agent import NO_SESSION, Result, Session

ENV = "ARETE_REPLAY_DIR"
RUNNER = "replay"
SAMPLE_FILE = "notes.txt"
SAMPLE_TEXT = "A small file for replay checks.\n"
SAMPLE_WORD = "kumquat"
SAMPLE_REPO = "replay-sample"
SAMPLE_CHECK = f"grep -q {SAMPLE_WORD} {SAMPLE_FILE}"
SAMPLE_NOTES = f"There is no app to start. Check with grep that {SAMPLE_FILE} holds the word {SAMPLE_WORD}."
SCRATCH_CONFIG = "ARETE_CONFIG_DIR, since all = true sends every agent call to replay"
SAMPLE_USAGE = {"ok": True, "cost_usd": 0, "tokens_in": 0, "tokens_out": 0, "turns": 1, "seconds": 0}
SAMPLE_ANSWERS = {
    "plan": {"text": f"Add {SAMPLE_WORD} to {SAMPLE_FILE}.", "structured": {
        "plan": f"Add the word {SAMPLE_WORD} on a line of its own at the end of {SAMPLE_FILE}.",
        "ambiguous": False, "risk": "low", "estimated_lines": 1,
        "verify": f"1. Run `grep {SAMPLE_WORD} {SAMPLE_FILE}` in the repo.\n2. It prints the line {SAMPLE_WORD}.",
        "assumptions": [f"The word goes on a new last line of {SAMPLE_FILE}."]}},
    "build": {"text": f"Added {SAMPLE_WORD} to {SAMPLE_FILE}."},
    "verify": {"text": f"{SAMPLE_FILE} holds {SAMPLE_WORD}.", "structured": {
        "verdict": "pass", "summary": f"{SAMPLE_FILE} holds {SAMPLE_WORD}.",
        "checks": [{"what": f"{SAMPLE_FILE} holds {SAMPLE_WORD}", "how": f"grep {SAMPLE_WORD} {SAMPLE_FILE}",
                    "saw": SAMPLE_WORD, "passed": True}]}},
    "review": {"text": "The change does what the plan says.", "structured": {
        "verdict": "approve", "summary": "One line added, as planned.", "problems": []}},
    "document": {"text": "", "structured": {
        "title": "", "text": f"Adds the word {SAMPLE_WORD} on a last line of its own in {SAMPLE_FILE}, "
                             "which is all the task asks for."}},
    "learn": {"text": "Nothing new to learn.", "structured": {"expertise": "# Expertise\n", "added": [], "removed": []}},
}


def folder(cfg: config.Config) -> Path | None:
    found = os.environ.get(ENV, "").strip() or cfg.replay.dir
    return Path(os.path.abspath(paths.expand(found))) if found else None


def answer_name(run_id: str, step: str) -> str:
    earlier = sum(1 for s in state.steps(run_id) if s["step"] == step) if run_id else 0
    return record.slug(step) + (str(earlier + 1) if earlier else "")


def failed(error: str) -> Result:
    return Result(ok=False, error=error)


def run(cwd: Path | str, trace: tuple[str, str], cfg: config.Config, on_line: Callable[[str], None],
        resume: Session = NO_SESSION) -> Result:
    if not (where := folder(cfg)):
        return failed(f"replay: no answer folder; set [replay] dir in {paths.short_path(paths.CONFIG_FILE)} or {ENV}")
    name = answer_name(*trace)
    answer_file, patch_file = where / f"{name}.json", where / f"{name}.patch"
    patched = patch_file.exists()
    on_line(json.dumps({"type": "replay", "answer": str(answer_file), "patch": str(patch_file) if patched else ""}))
    try:
        answer = json.loads(answer_file.read_text())
    except FileNotFoundError:
        return failed(f"replay: no answer file {answer_file}")
    except OSError as e:
        return failed(f"replay: cannot read {answer_file}: {e}")
    except ValueError as e:
        return failed(f"replay: {answer_file} is not JSON: {e}")
    if not isinstance(answer, dict):
        return failed(f"replay: {answer_file} is not one JSON object")
    try:
        res = Result(ok=bool(answer.get("ok", True)), text=str(answer.get("text") or ""),
                     structured=answer.get("structured"), error=str(answer.get("error") or ""),
                     cost_usd=float(answer.get("cost_usd") or 0), tokens_in=int(answer.get("tokens_in") or 0),
                     tokens_out=int(answer.get("tokens_out") or 0), turns=int(answer.get("turns") or 0),
                     cache_read=int(answer.get("cache_read") or 0), cache_write=int(answer.get("cache_write") or 0),
                     api_seconds=float(answer.get("api_seconds") or 0),
                     first_context_tokens=int(answer.get("first_context_tokens") or 0),
                     session=resume if resume.id else Session(str(answer.get("session") or uuid.uuid4().hex), 0.0,
                                                              RUNNER))
        seconds = max(0.0, float(answer.get("seconds") or 0))
    except (TypeError, ValueError) as e:
        return failed(f"replay: {answer_file} has a bad value: {e}")
    if not res.ok and not res.error:
        res.error = f"replay: {answer_file} says ok is false"
    calls = answer.get("tool_calls") or []
    if not isinstance(calls, list) or not all(isinstance(c, dict) and isinstance(c.get("name"), str) for c in calls):
        return failed(f"replay: {answer_file} has a bad value: tool_calls is not a list of objects with a name")
    for call in calls:
        at = uuid.uuid4().hex
        on_line(json.dumps({"type": "tool_call", "id": at, "name": call["name"], "input": call.get("input")}))
        if asks.is_ask(call["name"]):
            text, error = asks.reply(call.get("input"))
            on_line(json.dumps({"type": "tool_result", "id": at, "name": call["name"], "text": text, "error": error}))
    time.sleep(seconds)
    if patched and (applied := util.sh(["git", "apply", str(patch_file)], cwd=cwd)).returncode != 0:
        return failed(f"replay: {patch_file} does not apply: {util.first_line(applied.stderr or applied.stdout)}")
    return res


def sample(where: Path, adw: bool = False) -> list[str]:
    where = Path(os.path.abspath(where))
    if where.exists() and any(where.iterdir()):
        raise FileExistsError(f"{where} is not empty")
    repo, origin, layer, answers = where / "repo", where / "origin.git", where / "layer", where / "replay"
    for made in (repo, layer, answers):
        made.mkdir(parents=True)
    util.git(["init", "-q", "-b", "main"], repo)
    (repo / SAMPLE_FILE).write_text(SAMPLE_TEXT)
    util.git(["add", SAMPLE_FILE], repo)
    util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "first commit"], repo)
    util.git(["init", "-q", "--bare", "-b", "main", str(origin)], where)
    util.git(["remote", "add", "origin", str(origin)], repo)
    util.git(["push", "-q", "-u", "origin", "main"], repo)
    (repo / SAMPLE_FILE).write_text(f"{SAMPLE_TEXT}{SAMPLE_WORD}\n")
    patch = util.git(["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"], repo)
    (repo / SAMPLE_FILE).write_text(SAMPLE_TEXT)
    (answers / "build.patch").write_text(patch)
    for step, answer in SAMPLE_ANSWERS.items():
        (answers / f"{step}.json").write_text(json.dumps({**answer, **SAMPLE_USAGE}, indent=2) + "\n")
    (layer / "config.toml").write_text(f"[checks]\nword = {json.dumps(SAMPLE_CHECK)}\n\n[git]\nbase = \"main\"\n\n"
                                       f"[verify]\nnotes = {json.dumps(SAMPLE_NOTES)}\n"
                                       + ("\n[factory]\npath = \"adw\"\n" if adw else ""))
    return [f"# wrote {repo}, {origin}, {layer} and {answers}",
            f"# add these lines to {paths.short_path(paths.CONFIG_FILE)}, best in a scratch {SCRATCH_CONFIG}:",
            "",
            "[replay]", "all = true", f"dir = {json.dumps(str(answers))}",
            "",
            "[[repo]]", f"name = {json.dumps(SAMPLE_REPO)}", f"path = {json.dumps(str(repo))}",
            f"layer = {json.dumps(str(layer))}", "tracker = \"local\"", "jev = false"]
