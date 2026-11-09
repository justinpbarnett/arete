from __future__ import annotations

import os
import posixpath
import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path

from . import (
    claude,
    config,
    control,
    devices,
    diffguard,
    gh,
    jev,
    paths,
    record,
    runner,
    settings,
    state,
    util,
)
from .agent import NO_SESSION, Session

SESSIONS_DIR = paths.STATE_DIR / "sessions"
SESSION_TOOLS = [*runner.EDIT_TOOLS, "Bash"]
TURN_TIMEOUT_SECONDS = 3600
INBOX_POLL_SECONDS = 2
SESSION_LOG = "sessions.log"
SEEN_MODEL_DAYS = 30
RECENT_CHARS = 8000
SWITCHES_TASK = ("Does the new message start a different task from the recent work, so that the details of the recent "
                 "work no longer matter for it?")


@dataclass
class Spec:
    id: str
    prompt: str
    device: str = "local"
    model: str = ""
    runner: str = ""
    tools: list[str] = field(default_factory=lambda: list(SESSION_TOOLS))
    repo: str = ""
    budget_usd: float | None = None
    cwd: str = ""
    branch: str = ""
    source: str = ""
    created: str = ""
    github: str = ""
    base: str = ""
    deny: list[str] = field(default_factory=list)
    pushed: str = ""
    unpushed: bool = False


class SessionStopped(Exception):
    pass


def spec_file(session_id: str) -> Path:
    return SESSIONS_DIR / session_id / "session.json"


def load_spec(session_id: str) -> Spec:
    data = util.read_json(spec_file(session_id), None)
    if not data:
        raise control.NotFound(f"no session {session_id}")
    return Spec(**data)


def save_spec(spec: Spec) -> None:
    util.write_json(spec_file(spec.id), asdict(spec))


def device_named(name: str) -> devices.Device:
    try:
        return devices.get(name)
    except devices.DeviceError as e:
        raise control.Refused(str(e)) from e


def repo_named(name: str, device: devices.Device, cfg: config.Config) -> config.Repo | None:
    if not name.strip():
        return None
    try:
        repo = cfg.repo(name.strip())
    except config.UnknownRepo as e:
        raise control.Refused(e.args[0]) from e
    if not device.remote:
        return repo
    config_file = paths.short_path(paths.CONFIG_FILE)
    if not repo.github:
        raise control.Refused(f"a session on {device.name} clones its repo from GitHub, and repo {repo.name} has no "
                              f"github in {config_file}" + ("; its parts are separate repos, so start it on local"
                                                            if repo.parts else ""))
    if missing := cfg.github_app.missing():
        raise control.Refused(f"a session on {device.name} clones {repo.github} with a token from a GitHub App; set "
                              f"{', '.join(missing)} under [github_app] in {config_file}")
    if not cfg.github_app.private_key.is_file():
        raise control.Refused(f"[github_app] private_key {paths.short_path(cfg.github_app.private_key)} is not a file")
    return repo


def workspace(spec: Spec, device: devices.Device, repo: config.Repo | None) -> None:
    if device.remote and not repo:
        spec.cwd = device.dir
        return
    if device.remote:
        layer = config.load_layer(repo.layer_dir)
        spec.github = repo.github
        spec.base = layer.base_branch or gh.default_branch(repo.github)
        spec.deny = diffguard.denied_paths(layer)
        spec.branch = f"session/{spec.id}"
        spec.cwd = posixpath.join(device.dir, f"{repo.name}-{spec.id}")
        return
    if not repo:
        scratch = SESSIONS_DIR / spec.id / "work"
        scratch.mkdir(parents=True, exist_ok=True)
        spec.cwd = str(scratch)
        return
    spec.source = str(repo.path)
    spec.branch = f"session/{spec.id}"
    wt = paths.WORKTREES_DIR / f"session-{spec.id}"
    util.git(["worktree", "add", "-q", "-b", spec.branch, str(wt), "HEAD"], repo.path)
    spec.cwd = str(wt)


def launch(prompt: str, *, device: str = "local", model: str = "", run_with: str = "", tools: list[str] | None = None,
           repo: str = "", budget_usd: float | None = None) -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise control.Refused("a session needs a first message")
    tools = list(SESSION_TOOLS if tools is None else tools)
    if problems := runner.agent_problems(run_with, tools):
        raise control.Refused("; ".join(problems))
    if budget_usd is not None and budget_usd <= 0:
        raise control.Refused("the budget is a number of dollars above zero, or none for no limit")
    cfg = config.load()
    model = model.strip() or cfg.model("session")
    if not model:
        raise control.Refused("pick a model, or set [models] session in the config")
    target = device_named(device)
    work_repo = repo_named(repo, target, cfg)
    which, name = runner.route(model, run_with)
    off = runner.withheld(model, run_with, tools, target.remote)
    tools = [t for t in tools if not any(t in withheld for withheld in off.values())]
    spec = Spec(record.new_id("ses"), prompt, target.name, model, run_with, tools, work_repo.name if work_repo else "",
                budget_usd, created=util.iso())
    workspace(spec, target, work_repo)
    save_spec(spec)
    record.start("session", run_id=spec.id, lock=session_lock(spec.id), pid=None, repo=spec.repo or None,
                 device=spec.device, model=name, runner=which, trigger="manual",
                 log=paths.short_path(SESSIONS_DIR / spec.id / "session.log"), summary=util.first_line(prompt, 200),
                 branch=spec.branch or None)
    state.add_feed(spec.id, "start", f"{which} {name} on {spec.device} in {paths.short_path(spec.cwd)}"
                                     + (f"; a clone of {spec.github} on {spec.branch} from {spec.base}"
                                        if spec.github else "")
                                     + f"; tools {', '.join(tools) or 'none'}"
                                     + "".join(f"; {', '.join(withheld)} off: {reason}" for reason, withheld in off.items()))
    proc = control.detach(["session", "exec", spec.id], SESSION_LOG)
    record.note(spec.id, pid=proc.pid)
    return {"id": spec.id, "cwd": spec.cwd, "branch": spec.branch, "model": name, "runner": which,
            "device": spec.device, "tools": tools}


def send(session_id: str, text: str) -> dict:
    text = text.strip()
    if not text:
        raise control.Refused("write a message first")
    r = state.run_get(session_id)
    if not r or r["kind"] != "session":
        raise control.NotFound(f"no session {session_id}")
    if r["status"] != "running":
        raise control.Refused(f"session {session_id} has ended ({r['status']}); start a new one")
    state.add_message(session_id, text)
    return {"id": session_id, "queued": True}


def log_line(spec: Spec, kind: str, text: str) -> None:
    write_log(spec, kind, text)
    state.add_feed(spec.id, kind, text)


def write_log(spec: Spec, kind: str, text: str) -> None:
    f = SESSIONS_DIR / spec.id / "session.log"
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as out:
        out.write(f"{util.iso()} [{kind}] {text}\n")


def stop_on_term(*_) -> None:
    raise SessionStopped


def session_lock(session_id: str) -> str:
    return f"session-{session_id}"


def take_turn(spec: Spec, cfg: config.Config, device: devices.Device, prompt: str, turn: int,
              resume: Session) -> Session:
    log = SESSIONS_DIR / spec.id / "session.log"
    recent = util.tail(log.read_text(errors="replace"), RECENT_CHARS) if log.exists() else ""
    log_line(spec, "you", prompt)
    left = None
    if spec.budget_usd and runner.metered(spec.model, spec.runner or resume.runner):
        left = round(spec.budget_usd - record.metered_spent(spec.id), 2)
        if left <= runner.BUDGET_FLOOR_USD:
            log_line(spec, "error", f"the session budget of ${spec.budget_usd:g} is spent")
            return resume
    resume = compacted(spec, cfg, device, prompt, recent, turn, resume)
    res = runner.run(prompt, spec.cwd, model=spec.model, runner=spec.runner, trace=(spec.id, f"turn {turn}"),
                     resume=resume, cfg=cfg, device=device, tools=spec.tools, allowed_tools=spec.tools,
                     disallowed_tools=runner.NO_GIT_WRITES, max_budget_usd=left, timeout=TURN_TIMEOUT_SECONDS,
                     say=lambda text: log_line(spec, "wait", text))
    record.note(spec.id, model=res.model, runner=res.runner)
    note = runner.cost_note(res)
    log_line(spec, "reply" if res.ok else "error", (res.text.strip() or "(no text)") + f"\n{note}" if res.ok
             else f"{res.error.strip()} {note}")
    return res.session if res.session.id else resume


def compacted(spec: Spec, cfg: config.Config, device: devices.Device, prompt: str, recent: str, turn: int,
              resume: Session) -> Session:
    if not resume.id or resume.runner != "claude" or device.remote or not recent:
        return resume
    if not jev.ready(cfg, [cfg.repo_for_work_dir(spec.cwd)]):
        return resume
    if (tokens := claude.context_tokens(resume.id)) < cfg.jev.compact_tokens:
        return resume
    switch = jev.ask(f"Recent work in this session:\n{recent}\n\nNew message:\n{prompt}",
                     {"switch": jev.yes_no(SWITCHES_TASK)}, cfg, trace=(spec.id, f"turn {turn} switch jev")
                     ).answers.get("switch")
    if not switch or switch.value is not True or not switch.sure(cfg.jev.confidence):
        return resume
    res = runner.run("/compact", spec.cwd, model=spec.model, runner="claude", trace=(spec.id, f"turn {turn} compact"),
                     resume=resume, cfg=cfg, device=device, timeout=TURN_TIMEOUT_SECONDS)
    log_line(spec, "compact" if res.ok else "error",
             f"compacted {tokens:,} tokens of context before a new task (JEV {switch.note()}) {runner.cost_note(res)}"
             if res.ok else f"compaction failed: {util.first_line(res.error)}")
    return res.session if res.ok and res.session.id else resume


def execute(session_id: str) -> str:
    spec = load_spec(session_id)
    cfg = config.load()
    with util.lock(session_lock(session_id)) as free:
        if not free:
            return "busy"
        if (ended := (state.run_get(session_id) or {}).get("status", "gone")) != "running":
            return ended
        record.note(session_id, pid=os.getpid())
        signal.signal(signal.SIGTERM, stop_on_term)
        status, summary = "done", ""
        try:
            with devices.lease(spec.device, spec.id, lambda text: log_line(spec, "device", text)) as device:
                head = clone_repo(spec, cfg, device) if spec.github else ""
                resume, turn, prompt = NO_SESSION, 0, spec.prompt
                idle_since = time.monotonic()
                try:
                    while True:
                        if prompt:
                            turn += 1
                            resume = take_turn(spec, cfg, device, prompt, turn, resume)
                            if spec.github:
                                head = push_work(spec, cfg, device, f"arete session {spec.id}, turn {turn}", head)
                            idle_since = time.monotonic()
                        prompt = "\n\n".join(state.take_messages(session_id))
                        if prompt:
                            continue
                        if time.monotonic() - idle_since > cfg.session_idle_hours * 3600:
                            summary = f"closed after {cfg.session_idle_hours:g}h with no message"
                            break
                        time.sleep(INBOX_POLL_SECONDS)
                finally:
                    if spec.github:
                        push_work(spec, cfg, device, f"arete session {spec.id}, turn {turn}", head, last=True)
                close(spec, status, summary)
        except SessionStopped:
            status, summary = "stopped", record.STOPPED if record.stop_asked(session_id) else "stopped by a signal"
        except Exception as e:
            status, summary = "failed", f"{type(e).__name__}: {util.first_line(str(e), 300)}"
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        close(spec, status, summary)
        return status


def clone_repo(spec: Spec, cfg: config.Config, device: devices.Device) -> str:
    log_line(spec, "git", f"cloning {spec.github} into {spec.cwd} on {spec.branch} from {spec.base}")
    head = devices.clone(device, spec.github, spec.base, spec.branch, spec.cwd,
                         gh.installation_token(cfg.github_app, spec.github))
    log_line(spec, "git", f"cloned {spec.github} at {head[:10]}")
    return head


def push_work(spec: Spec, cfg: config.Config, device: devices.Device, message: str, known: str,
              last: bool = False) -> str:
    spec.unpushed = True
    try:
        head, left_out = devices.commit_all(device, spec.cwd, message, spec.deny)
        if left_out:
            log_line(spec, "git", f"left {', '.join(left_out)} out of the commit, as only a person may change them")
        if head != known:
            devices.push(device, spec.cwd, spec.github, spec.branch, gh.installation_token(cfg.github_app, spec.github))
            spec.pushed = head
            log_line(spec, "git", f"pushed {head[:10]} to {spec.branch} on {spec.github}")
        spec.unpushed = False
        return head
    except (gh.GhError, devices.DeviceError) as e:
        log_line(spec, "error", f"the push of {spec.branch} failed: {e}"
                                + ("" if last else "; arete tries again after the next turn"))
        return known
    finally:
        save_spec(spec)


def pushed_note(spec: Spec) -> str:
    if spec.pushed and spec.unpushed:
        return f"; {spec.branch} of {spec.github} has its work up to {spec.pushed[:10]}, but its last changes were not pushed"
    if spec.pushed:
        return f"; its work is on {spec.branch} of {spec.github} at {spec.pushed[:10]}"
    return f"; its changes were not pushed to {spec.github}" if spec.unpushed else f"; it pushed nothing to {spec.github}"


def close(spec: Spec, status: str, summary: str) -> None:
    if spec.github:
        kept = pushed_note(spec)
    else:
        kept = f"; the worktree stays at {paths.short_path(spec.cwd)} on {spec.branch}" if spec.branch else ""
    end_line = f"{status}: {summary}{kept} (${record.spent(spec.id):.2f})"
    if record.settle(spec.id, status, end_line=end_line, summary=f"{summary}{kept}"[:500]):
        write_log(spec, "end", end_line)


def views(limit: int = 30, connect=state.db) -> list[dict]:
    out = []
    for r in state.runs(kind="session", limit=limit, connect=connect):
        spec = util.read_json(spec_file(r["id"]), {})
        out.append({k: r.get(k) for k in ("id", "status", "started", "finished", "model", "runner", "device",
                                          "cost_usd", "summary", "branch")}
                   | {"prompt": util.first_line(spec.get("prompt", ""), 200), "tools": spec.get("tools", []),
                      "cwd": paths.short_path(spec.get("cwd", "")), "repo": spec.get("repo", "")})
    return out


def known_models(cfg: config.Config) -> list[str]:
    since = util.iso(util.now() - timedelta(days=SEEN_MODEL_DAYS))
    seen = {s["model"] for s in state.steps_since(since, connect=state.reader) if s["model"]}
    return sorted({*settings.MODEL_ALIASES, *(m for m in cfg.models.values() if m), *cfg.pricing, *seen})


def choices() -> dict:
    cfg = config.load()
    return {"devices": [d.name for d in devices.known()], "repos": [r.name for r in cfg.repos],
            "models": known_models(cfg), "runners": list(runner.RUNNERS), "tools": sorted(runner.KNOWN_TOOLS),
            "default_tools": SESSION_TOOLS, "default_model": cfg.model("session"), "idle_hours": cfg.session_idle_hours}
