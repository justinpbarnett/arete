from __future__ import annotations

import os
import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path

from . import (
    config,
    control,
    devices,
    loops,
    paths,
    pi,
    record,
    runner,
    settings,
    state,
    util,
)
from .agent import NO_SESSION, Session
from .pipeline import NO_GIT_WRITES

SESSIONS_DIR = paths.STATE_DIR / "sessions"
SESSION_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
TURN_TIMEOUT_SECONDS = 3600
INBOX_POLL_SECONDS = 2
SESSION_LOG = "sessions.log"
SEEN_MODEL_DAYS = 30


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


def repo_path(name: str, cfg: config.Config) -> Path:
    for r in cfg.repos:
        if r.name == name:
            return r.path
    path = paths.expand(name)
    if not (path / ".git").exists():
        raise control.Refused(f"repo '{name}' is not a repo in the config or a path to a git repo")
    return path


def workspace(spec: Spec, cfg: config.Config) -> None:
    device = devices.get(spec.device)
    if device.remote:
        spec.cwd = spec.repo or device.dir
        return
    if not spec.repo:
        scratch = SESSIONS_DIR / spec.id / "work"
        scratch.mkdir(parents=True, exist_ok=True)
        spec.cwd = str(scratch)
        return
    src = repo_path(spec.repo, cfg)
    spec.source = str(src)
    spec.branch = f"session/{spec.id}"
    wt = paths.WORKTREES_DIR / f"session-{spec.id}"
    util.git(["worktree", "add", "-q", "-b", spec.branch, str(wt), "HEAD"], src)
    spec.cwd = str(wt)


def check_choices(device: str, model: str, run_with: str, tools: list[str]) -> None:
    try:
        devices.get(device)
    except devices.DeviceError as e:
        raise control.Refused(str(e)) from e
    if run_with and run_with not in runner.RUNNERS:
        raise control.Refused(f"runner '{run_with}' is not one of {', '.join(runner.RUNNERS)}")
    if unknown := sorted(set(tools) - loops.KNOWN_TOOLS):
        raise control.Refused(f"unknown tools {', '.join(unknown)}; use {', '.join(sorted(loops.KNOWN_TOOLS))}")
    if not model.strip() and not config.load().model("session"):
        raise control.Refused("pick a model, or set [models] session in the config")


def launch(prompt: str, *, device: str = "local", model: str = "", run_with: str = "", tools: list[str] | None = None,
           repo: str = "", budget_usd: float | None = None) -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise control.Refused("a session needs a first message")
    tools = list(SESSION_TOOLS if tools is None else tools)
    check_choices(device, model, run_with, tools)
    cfg = config.load()
    model = model.strip() or cfg.model("session")
    which, name = runner.route(model, run_with)
    off = pi.withheld(tools, devices.get(device or "local").remote) if which == "pi" else {}
    tools = [t for t in tools if not any(t in withheld for withheld in off.values())]
    spec = Spec(record.new_id("ses"), prompt, device or "local", model, run_with, tools, repo.strip(), budget_usd,
                created=util.iso())
    workspace(spec, cfg)
    save_spec(spec)
    record.start("session", run_id=spec.id, lock=session_lock(spec.id), pid=None, repo=spec.repo or None,
                 device=spec.device, model=name, runner=which, trigger="manual",
                 log=paths.short_path(SESSIONS_DIR / spec.id / "session.log"), summary=util.first_line(prompt, 200),
                 branch=spec.branch or None)
    state.add_feed(spec.id, "start", f"{which} {name} on {spec.device} in {paths.short_path(spec.cwd)}; tools "
                                     f"{', '.join(tools) or 'none'}" + "".join(f"; {', '.join(withheld)} off: {reason}"
                                                                for reason, withheld in off.items()))
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
    f = SESSIONS_DIR / spec.id / "session.log"
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as out:
        out.write(f"{util.iso()} [{kind}] {text}\n")
    state.add_feed(spec.id, kind, text)


def stop_on_term(*_) -> None:
    raise SessionStopped("stopped by request")


def session_lock(session_id: str) -> str:
    return f"session-{session_id}"


def take_turn(spec: Spec, cfg: config.Config, device: devices.Device, prompt: str, turn: int,
              resume: Session) -> Session:
    log_line(spec, "you", prompt)
    left = None
    if spec.budget_usd:
        left = round(spec.budget_usd - record.spent(spec.id), 2)
        if left <= runner.BUDGET_FLOOR_USD:
            log_line(spec, "error", f"the session budget of ${spec.budget_usd:g} is spent")
            return resume
    res = runner.run(prompt, spec.cwd, model=spec.model, runner=spec.runner, trace=(spec.id, f"turn {turn}"),
                     resume=resume, cfg=cfg, device=device, tools=spec.tools, allowed_tools=spec.tools,
                     disallowed_tools=NO_GIT_WRITES, max_budget_usd=left, timeout=TURN_TIMEOUT_SECONDS)
    record.note(spec.id, model=res.model, runner=res.runner)
    note = f"({res.turns} turns, ${res.cost_usd:.2f})"
    log_line(spec, "reply" if res.ok else "error", (res.text.strip() or "(no text)") + f"\n{note}" if res.ok
             else f"{res.error} {note}")
    return res.session if res.session.id else resume


def execute(session_id: str) -> str:
    spec = load_spec(session_id)
    cfg = config.load()
    with util.lock(session_lock(session_id)) as free:
        if not free:
            return "busy"
        record.note(session_id, pid=os.getpid())
        signal.signal(signal.SIGTERM, stop_on_term)
        status, summary = "done", ""
        try:
            with devices.lease(spec.device, spec.id, lambda text: log_line(spec, "device", text)) as device:
                if device.remote and not spec.cwd:
                    spec.cwd = device.dir
                resume, turn, prompt = NO_SESSION, 0, spec.prompt
                idle_since = time.monotonic()
                while True:
                    if prompt:
                        turn += 1
                        resume = take_turn(spec, cfg, device, prompt, turn, resume)
                        idle_since = time.monotonic()
                    prompt = "\n\n".join(state.take_messages(session_id))
                    if prompt:
                        continue
                    if time.monotonic() - idle_since > cfg.session_idle_hours * 3600:
                        summary = f"closed after {cfg.session_idle_hours:g}h with no message"
                        break
                    time.sleep(INBOX_POLL_SECONDS)
        except SessionStopped as e:
            status, summary = "stopped", str(e)
        except Exception as e:
            status, summary = "failed", f"{type(e).__name__}: {util.first_line(str(e), 300)}"
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        kept = f"; the worktree stays at {paths.short_path(spec.cwd)} on {spec.branch}" if spec.branch else ""
        log_line(spec, "end", f"{status}: {summary}{kept} (${record.spent(session_id):.2f})")
        record.settle(session_id, status, summary=f"{summary}{kept}"[:500])
        return status


def views(limit: int = 30) -> list[dict]:
    out = []
    for r in state.runs(kind="session", limit=limit):
        spec = util.read_json(spec_file(r["id"]), {})
        out.append({k: r.get(k) for k in ("id", "status", "started", "finished", "model", "runner", "device",
                                          "cost_usd", "summary", "branch")}
                   | {"prompt": util.first_line(spec.get("prompt", ""), 200), "tools": spec.get("tools", []),
                      "cwd": paths.short_path(spec.get("cwd", "")), "repo": spec.get("repo", "")})
    return out


def choices() -> dict:
    cfg = config.load()
    since = util.iso(util.now() - timedelta(days=SEEN_MODEL_DAYS))
    seen = {s["model"] for s in state.steps_since(since) if s["model"]}
    models = sorted({*settings.MODEL_ALIASES, *(m for m in cfg.models.values() if m), *cfg.pricing, *seen})
    return {"devices": devices.names(), "repos": [r.name for r in cfg.repos], "models": models, "runners": list(runner.RUNNERS),
            "tools": sorted(loops.KNOWN_TOOLS), "default_tools": SESSION_TOOLS, "default_model": cfg.model("session"),
            "idle_hours": cfg.session_idle_hours}
