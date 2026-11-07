from __future__ import annotations

import os
import shutil
import threading
import tomllib
from pathlib import Path

from . import (
    config,
    control,
    cron,
    devices,
    engine,
    loops,
    paths,
    runner,
    sources,
    state,
    util,
)

DRAFT_TRIES = 3
REFERENCE = loops.ENGINE_DIR / "README.md"
DRAFTS_DIR = paths.STATE_DIR / "drafts"
SAMPLE_EVENTS = 3
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "lowercase letters, digits and dashes"},
        "loop_toml": {"type": "string"},
        "prompt_md": {"type": "string", "description": "empty for a command loop"},
        "note": {"type": "string", "description": "one or two lines on what to check before turning it on"},
    },
    "required": ["name", "loop_toml", "prompt_md", "note"],
}


def private_dir(cfg: config.Config) -> Path:
    return cfg.loop_dirs[0] if cfg.loop_dirs else paths.CONFIG_DIR / "loops"


def device_names() -> list[str]:
    try:
        return [d.name for d in devices.load()]
    except devices.DeviceError:
        return ["local"]


def draft_prompt(sentence: str, cfg: config.Config, name: str, repo: str, taken: set[str]) -> str:
    repos = "\n".join(f"- {r.name}: {r.github or 'no GitHub remote'}{', pipeline' if r.pipeline else ''}"
                      for r in cfg.repos)
    parts = [
        f"Draft one new arete loop from this sentence:\n\n> {sentence}",
        "The reference below is the whole loop folder format. Use only what it describes.",
        "<reference>\n" + REFERENCE.read_text() + "\n</reference>",
        f"Repos in the config:\n{repos}",
        f"Devices: {', '.join(device_names())}.",
        f"Loop names already taken: {', '.join(sorted(taken)) or 'none'}.",
        ("Pick the simplest kind that does the job: a command loop when a script can decide on its own, a watch "
         "loop when it needs judgment, climb or search only to change code. Put `enabled = false` at the top. Give "
         "every agent loop a run budget_usd, and a daily_budget_usd when it fires more than once a day. Set "
         "stale_hours on a scheduled loop. Prefer emits that need no approval only for digest, notify and metric; "
         "a ticket or pr emit on a repo you cannot see should set approve = true."),
    ]
    if name:
        parts.append(f"Name the loop `{name}`.")
    if repo:
        parts.append(f"The loop works on the repo `{repo}`.")
    parts.append("Return JSON: `name`, `loop_toml` (the whole file), `prompt_md` (the whole prompt for an agent "
                 "loop, empty for a command loop), and `note`.")
    return "\n\n".join(parts)


def check_draft(draft: dict, cfg: config.Config, taken: set[str]) -> tuple[Path, str]:
    name = str(draft.get("name") or "").strip()
    if not loops.NAME.match(name):
        return Path(), f"the name '{name}' must be lowercase letters, digits and dashes"
    if name in taken:
        return Path(), f"a loop named {name} already exists; pick another name"
    folder = DRAFTS_DIR / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / "loop.toml").write_text(str(draft.get("loop_toml") or "").strip() + "\n")
    if str(draft.get("prompt_md") or "").strip():
        (folder / "prompt.md").write_text(str(draft["prompt_md"]).strip() + "\n")
    try:
        loops.load(folder, "private", "", cfg)
    except (loops.LoopError, cron.BadSchedule, tomllib.TOMLDecodeError, ValueError, TypeError, KeyError) as e:
        return folder, f"{type(e).__name__}: {e}"
    return folder, ""


def draft(sentence: str, *, name: str = "", repo: str = "", model: str = "", say=print) -> dict:
    sentence = sentence.strip()
    if not sentence:
        raise control.Refused("say in one sentence what the loop should do")
    cfg = config.load()
    taken = {x.name for x in loops.load_all(cfg)}
    if name and name in taken:
        raise control.Refused(f"a loop named {name} already exists")
    if name and not loops.NAME.match(name):
        raise control.Refused(f"the name '{name}' must be lowercase letters, digits and dashes")
    run_id = f"draft-{util.now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
    state.start_run(run_id, "draft")
    state.update_run(run_id, pid=os.getpid(), trigger="manual", summary=util.first_line(sentence, 200))
    prompt = draft_prompt(sentence, cfg, name, repo, taken)
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    problem, folder, answer = "", Path(), {}
    try:
        for attempt in range(1, DRAFT_TRIES + 1):
            say(f"drafting, try {attempt} of {DRAFT_TRIES}")
            res = runner.run(prompt, DRAFTS_DIR, model=model or cfg.model("loop"), trace=(run_id, f"draft {attempt}"),
                             cfg=cfg, tools=[], json_schema=DRAFT_SCHEMA, persist=False, timeout=600)
            state.update_run(run_id, model=res.model, runner=res.runner)
            if not res.ok:
                problem = f"the model failed: {util.first_line(res.error, 300)}"
                continue
            answer = res.structured or {}
            folder, problem = check_draft(answer, cfg, taken)
            if not problem:
                break
            say(f"  the draft did not validate: {problem}")
            prompt += (f"\n\nYour last draft did not validate: {problem}\n\nThe last loop.toml:\n\n```toml\n"
                       f"{answer.get('loop_toml', '')}\n```\n\nFix it and return the whole draft again.")
        totals = state.run_totals(run_id)
        if problem:
            state.finish_run(run_id, "failed", summary=problem[:500], **totals)
            raise control.Refused(f"no valid draft after {DRAFT_TRIES} tries: {problem}")
        dest = private_dir(cfg) / folder.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), dest)
        loop = loops.find(folder.name)
        loops.set_enabled(loop, False)
        state.finish_run(run_id, "done", summary=f"drafted {loop.name} in {paths.short_path(dest)}", **totals)
    except BaseException:
        if state.run_get(run_id)["status"] == "running":
            state.finish_run(run_id, "failed", summary="the draft stopped early", **state.run_totals(run_id))
        raise
    return {"name": loop.name, "dir": str(dest), "note": str(answer.get("note") or ""), "run": run_id,
            "cost_usd": totals["cost_usd"], "loop_toml": (dest / "loop.toml").read_text()}


def sample_events(loop: loops.Loop, cfg: config.Config) -> list[dict]:
    found = []
    for i in range(len(loop.sources)):
        found += sources.peek(loop, i, cfg, SAMPLE_EVENTS)
    return found


def try_loop(name: str, events: list[dict] | None = None, follow=None) -> dict:
    cfg = config.load()
    loop = control.find_loop(name, cfg)
    if loop.error:
        raise control.Refused(f"{name} is broken: {loop.error}")
    if events is None and loop.fed_by_events:
        events = sample_events(loop, cfg)
        if not events:
            raise control.Refused(f"{name} found no events to try on; pass --events FILE with a JSON list of events")
    run_id = state.new_loop_run_id(loop.name)
    result: dict = {}
    finished = threading.Event()
    follower = threading.Thread(target=follow, args=(run_id, lambda: not finished.is_set()),
                                name=f"follow-{loop.name}", daemon=True) if follow else None
    if follower:
        follower.start()
    try:
        result["status"], _ = engine.execute(loop.name, run_id=run_id, trial=True, events=events, cfg=cfg)
    except Exception as e:
        result["status"], result["error"] = "failed", f"{type(e).__name__}: {e}"
    finally:
        finished.set()
        if follower:
            follower.join()
    return {"id": run_id, "events": len(events or []), **result, "run": state.run_get(run_id)}
