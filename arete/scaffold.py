from __future__ import annotations

import os
import re
import shutil
import signal
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
    record,
    runner,
    sources,
    state,
    util,
)

DRAFT_TRIES = 3
REFERENCE = loops.ENGINE_DIR / "README.md"
DRAFTS_DIR = paths.STATE_DIR / "drafts"
DRAFTS_LOG = "drafts.log"
SAMPLE_EVENTS = 3
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OWN_FILES = ("loop.toml", "prompt.md")
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "lowercase letters, digits and dashes"},
        "loop_toml": {"type": "string"},
        "prompt_md": {"type": "string", "description": "empty for a command loop"},
        "files": {"type": "array", "description": "each script the loop calls, saved in the loop folder",
                  "items": {"type": "object", "properties": {"name": {"type": "string"}, "text": {"type": "string"}},
                            "required": ["name", "text"]}},
        "note": {"type": "string", "description": "one or two lines on what to check before turning it on"},
    },
    "required": ["name", "loop_toml", "prompt_md", "files", "note"],
}


def private_dir(cfg: config.Config) -> Path:
    return cfg.loop_dirs[0] if cfg.loop_dirs else paths.CONFIG_DIR / "loops"


def draft_prompt(sentence: str, cfg: config.Config, name: str, repo: str, taken: set[str]) -> str:
    repos = "\n".join(f"- {r.name}: {r.github or 'no GitHub remote'}{', pipeline' if r.pipeline else ''}"
                      for r in cfg.repos)
    parts = [
        f"Draft one new arete loop from this sentence:\n\n> {sentence}",
        "The reference below is the whole loop folder format. Use only what it describes.",
        "<reference>\n" + REFERENCE.read_text() + "\n</reference>",
        f"Repos in the config:\n{repos}",
        f"Devices: {', '.join(d.name for d in devices.known())}.",
        f"Loop names already taken: {', '.join(sorted(taken)) or 'none'}.",
        ("Pick the simplest kind that does the job: a command loop when a script can decide on its own, a watch "
         "loop when it needs judgment, climb or search only to change code. Put `enabled = false` at the top. Give "
         "an agent loop whose model runs on pi a run budget_usd, and a daily_budget_usd when it fires more than "
         "once a day; a loop that runs on claude takes no budget, since the subscription carries it. Set "
         "stale_hours on a scheduled loop. Prefer emits that need no approval only for digest, notify and metric; "
         "a ticket or pr emit on a repo you cannot see should set approve = true."),
    ]
    if name:
        parts.append(f"Name the loop `{name}`.")
    if repo:
        parts.append(f"The loop works on the repo `{repo}`.")
    parts.append("Return JSON: `name`, `loop_toml` (the whole file), `prompt_md` (the whole prompt for an agent "
                 "loop, empty for a command loop), `files` (each script the loop calls, with its whole text; call it "
                 "as `./NAME` in loop.toml), and `note`.")
    return "\n\n".join(parts)


def check_draft(draft: dict, cfg: config.Config, taken: set[str], work: Path) -> tuple[Path, str]:
    name = str(draft.get("name") or "").strip()
    if not loops.NAME.match(name):
        return Path(), f"the name '{name}' must be lowercase letters, digits and dashes"
    if name in taken:
        return Path(), f"a loop named {name} already exists; pick another name"
    folder = work / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / "loop.toml").write_text(str(draft.get("loop_toml") or "").strip() + "\n")
    if str(draft.get("prompt_md") or "").strip():
        (folder / "prompt.md").write_text(str(draft["prompt_md"]).strip() + "\n")
    for f in draft.get("files") or []:
        file_name = str(f.get("name") or "").strip()
        if not FILE_NAME.match(file_name) or file_name in OWN_FILES:
            return folder, f"the file name '{file_name}' must be a plain file name other than {' and '.join(OWN_FILES)}"
        script = folder / file_name
        script.write_text(str(f.get("text") or ""))
        if script.read_text().startswith("#!"):
            script.chmod(0o755)
    try:
        loop = loops.load(folder, "private", "", cfg)
    except (loops.LoopError, cron.BadSchedule, tomllib.TOMLDecodeError, ValueError, TypeError, KeyError) as e:
        return folder, f"{type(e).__name__}: {e}"
    for argv in (loop.command, loop.try_command, loop.script):
        if argv and argv[0].startswith("./") and not (folder / argv[0]).is_file():
            return folder, f"loop.toml calls {argv[0]}, which is not in files"
    return folder, ""


def check_request(sentence: str, name: str, repo: str, cfg: config.Config) -> set[str]:
    if not sentence:
        raise control.Refused("say in one sentence what the loop should do")
    taken = {x.name for x in loops.load_all(cfg)}
    if name and not loops.NAME.match(name):
        raise control.Refused(f"the name '{name}' must be lowercase letters, digits and dashes")
    if name in taken:
        raise control.Refused(f"a loop named {name} already exists")
    if repo and repo not in {r.name for r in cfg.repos} and not paths.expand(repo).is_dir():
        raise control.Refused(f"'{repo}' is neither a repo in the config nor a folder")
    return taken


def start_draft(sentence: str, *, name: str = "", repo: str = "", model: str = "") -> dict:
    sentence, name, repo, model = (x.strip() for x in (sentence, name, repo, model))
    check_request(sentence, name, repo, config.load())
    run_id = record.start("draft", trigger="manual", pid=None, summary=util.first_line(sentence, 200))
    with record.failed_on_crash(run_id):
        proc = control.detach(["loop", "new", "--name", name, "--repo", repo, "--model", model, "--run-id", run_id,
                               "--", sentence], DRAFTS_LOG)
    record.note(run_id, pid=proc.pid)
    return state.run_get(run_id)


def draft(sentence: str, *, name: str = "", repo: str = "", model: str = "", run_id: str = "", say=print) -> dict:
    sentence = sentence.strip()
    cfg = config.load()
    if run_id:
        record.note(run_id, pid=os.getpid())
    else:
        check_request(sentence, name, repo, cfg)
        run_id = record.start("draft", trigger="manual", summary=util.first_line(sentence, 200))

    def tell(kind: str, text: str) -> None:
        say(text)
        state.add_feed(run_id, kind, text)

    signal.signal(signal.SIGTERM, engine.stop_on_term)
    try:
        with record.failed_on_crash(run_id):
            try:
                made = write_draft(run_id, sentence, name, repo, model, cfg, tell)
            except (control.Refused, engine.Stopped) as e:
                status, why = ("stopped", record.STOPPED) if isinstance(e, engine.Stopped) else ("failed", str(e))
                state.add_feed(run_id, "end", f"{status}: {why}")
                record.settle(run_id, status, summary=why[:500])
                raise control.Refused(why) from None
            drafted = f"drafted {made['name']} in {paths.short_path(made['dir'])}"
            state.add_feed(run_id, "end", f"done: {drafted}, off. Try it, then turn it on.")
            record.settle(run_id, "done", summary=drafted)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    return {**made, "cost_usd": record.spent(run_id)}


def write_draft(run_id: str, sentence: str, name: str, repo: str, model: str, cfg: config.Config, tell) -> dict:
    taken = check_request(sentence, name, repo, cfg)
    prompt = draft_prompt(sentence, cfg, name, repo, taken)
    work = DRAFTS_DIR / run_id
    work.mkdir(parents=True, exist_ok=True)
    problem, folder, answer = "", Path(), {}
    for attempt in range(1, DRAFT_TRIES + 1):
        tell("draft", f"drafting, try {attempt} of {DRAFT_TRIES}")
        res = runner.run(prompt, work, model=model or cfg.model("loop"), trace=(run_id, f"draft {attempt}"),
                         cfg=cfg, tools=[], json_schema=DRAFT_SCHEMA, persist=False, timeout=600,
                         wait_out_limits=False)
        record.note(run_id, model=res.model, runner=res.runner)
        if not res.ok:
            problem = f"the model failed: {util.first_line(res.error, 300)}"
            tell("draft", f"  {problem}")
            if res.usage_limit:
                break
            continue
        answer = res.structured or {}
        folder, problem = check_draft(answer, cfg, taken, work)
        if not problem:
            break
        tell("draft", f"  the draft did not validate: {problem}")
        prompt += (f"\n\nYour last draft did not validate: {problem}\n\nThe last loop.toml:\n\n```toml\n"
                   f"{answer.get('loop_toml', '')}\n```\n\nFix it and return the whole draft again.")
    if problem:
        raise control.Refused(f"no valid draft after {DRAFT_TRIES} tries: {problem}")
    dest = private_dir(cfg) / folder.name
    if dest.exists():
        raise control.Refused(f"a loop named {folder.name} appeared while this one drafted; its draft stays in "
                              f"{paths.short_path(folder)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), dest)
    shutil.rmtree(work, ignore_errors=True)
    loop = loops.find(folder.name)
    loops.set_enabled(loop, False)
    loop_toml = (dest / "loop.toml").read_text()
    state.add_feed(run_id, "toml", loop_toml.rstrip())
    if note := str(answer.get("note") or "").strip():
        state.add_feed(run_id, "note", note)
    return {"name": loop.name, "dir": str(dest), "note": note, "run": run_id, "loop_toml": loop_toml}


def try_events(loop: loops.Loop, cfg: config.Config) -> list[dict]:
    if not loop.fed_by_events or loop.error:
        return []
    taken, refused = [], []
    for i in range(len(loop.sources)):
        found = sources.peek(loop, i, cfg)
        taken += [e for e in found if not loop.trigger.refusal(e)][-SAMPLE_EVENTS:]
        refused += [e for e in found if loop.trigger.refusal(e)]
    if taken:
        return taken
    if refused:
        raise control.Refused(f"{loop.name}'s sources hold events, but its trigger {loop.trigger.refusal(refused[-1])}")
    raise control.Refused(f"{loop.name} found no events to try on yet")


def start_try(name: str) -> dict:
    cfg = config.load()
    loop = control.working_loop(name, cfg)
    return engine.launch(loop.name, events=try_events(loop, cfg), trial=True, cfg=cfg)


def try_loop(name: str, events: list[dict] | None = None, follow=None) -> dict:
    cfg = config.load()
    loop = control.find_loop(name, cfg)
    if events is None:
        try:
            events = try_events(loop, cfg)
        except control.Refused as e:
            raise control.Refused(f"{e}; pass --events FILE with a JSON list of events") from None
    run_id = record.new_id(loop.name)
    finished = threading.Event()
    follower = threading.Thread(target=follow, args=(run_id, lambda: not finished.is_set()),
                                name=f"follow-{loop.name}", daemon=True) if follow else None
    if follower:
        follower.start()
    try:
        run = engine.launch(loop.name, events=events, trial=True, detach=False, run_id=run_id, cfg=cfg)
    finally:
        finished.set()
        if follower:
            follower.join()
    return {"id": run_id, "events": len(events or []), "status": run["status"], "run": run}
