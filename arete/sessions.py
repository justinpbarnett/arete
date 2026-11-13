from __future__ import annotations

import base64
import os
import posixpath
import signal
import tempfile
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
    routing,
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
    cloned: str = ""
    fetched: str = ""
    pushed: str = ""
    held: str = ""
    unreturned: str = ""
    deny: list[str] = field(default_factory=list)


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
    if device.remote and (refusal := remote_refusal(repo, device, cfg)):
        raise control.Refused(refusal)
    return repo


def remote_refusal(repo: config.Repo, device: devices.Device, cfg: config.Config) -> str:
    if not repo.github:
        return (f"repo {repo.name} has no github setting, so {device.name} cannot clone it; "
                "a repo with parts or no github starts on local")
    if missing := cfg.github_app.missing():
        return (f"a session on {device.name} clones {repo.name} with a GitHub App token; set {', '.join(missing)} "
                f"in [github_app] of {paths.short_path(paths.CONFIG_FILE)}")
    if not cfg.github_app.private_key.is_file():
        return f"[github_app] private_key {paths.short_path(cfg.github_app.private_key)} is not a file"
    if not repo.path.is_dir():
        return f"repo {repo.name} has no folder at {paths.short_path(repo.path)} to fetch the session's work into"
    return ""


def session_repo(spec: Spec, cfg: config.Config) -> config.Repo | None:
    try:
        return cfg.repo(spec.repo) if spec.repo else None
    except config.UnknownRepo:
        return None


def workspace(spec: Spec, device: devices.Device, repo: config.Repo | None) -> None:
    if device.remote and repo:
        layer = config.load_layer(repo.layer_dir)
        spec.github = repo.github
        spec.base = layer.base_branch or gh.default_branch(repo.github)
        spec.deny = diffguard.denied_paths(layer)
        spec.branch = f"session/{spec.id}"
        spec.cwd = posixpath.join(device.dir, f"{repo.name}-{spec.id}")
        return
    if device.remote:
        spec.cwd = device.dir
        return
    if not repo:
        scratch = SESSIONS_DIR / spec.id / "work"
        scratch.mkdir(parents=True, exist_ok=True)
        spec.cwd = str(scratch)
        return
    spec.source = str(repo.path)
    spec.branch = f"session/{spec.id}"
    wt = paths.WORKTREES_DIR / f"session-{spec.id}"
    with record.timed(spec.id, "worktree", repo=repo.name, branch=spec.branch, path=str(wt)):
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
    off = runner.withheld(model, run_with, tools, target)
    tools = [t for t in tools if not any(t in withheld for withheld in off.values())]
    asked = runner.request_for(cfg, work_repo, which, name, None, True,
                               {"tools": tools, "disallowed_tools": runner.NO_GIT_WRITES})
    if (refusal := "" if cfg.replay.all else routing.fit_refusal(asked, target, routing.modes_for(cfg, work_repo))):
        raise control.Refused(refusal)
    spec = Spec(record.new_id("ses"), prompt, target.name, model, run_with, tools, work_repo.name if work_repo else "",
                budget_usd, created=util.iso())
    workspace(spec, target, work_repo)
    save_spec(spec)
    record.start("session", run_id=spec.id, lock=session_lock(spec.id), pid=None, repo=spec.repo or None,
                 device=spec.device, model=name, runner=which, trigger="manual",
                 log=paths.short_path(SESSIONS_DIR / spec.id / "session.log"), summary=util.first_line(prompt, 200),
                 branch=spec.branch or None)
    clone = f" (a clone of {spec.github} on {spec.branch} from {spec.base})" if spec.github else ""
    state.add_feed(spec.id, "start", f"{which} {name} on {spec.device} in {paths.short_path(spec.cwd)}{clone}; tools "
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

    def remaining_budget():
        return spec.budget_usd - record.metered_spent(spec.id) if spec.budget_usd else None

    resume = compacted(spec, cfg, device, prompt, recent, turn, resume)
    res = runner.run(prompt, spec.cwd, model=spec.model, runner=spec.runner, trace=(spec.id, f"turn {turn}"),
                     resume=resume, cfg=cfg, device=device, tools=spec.tools, allowed_tools=spec.tools,
                     disallowed_tools=runner.NO_GIT_WRITES, remaining_budget=remaining_budget, timeout=TURN_TIMEOUT_SECONDS,
                     say=lambda text: log_line(spec, "wait", text), repo=session_repo(spec, cfg))
    record.note(spec.id, model=res.model, runner=res.runner)
    note = runner.cost_note(res)
    log_line(spec, "reply" if res.ok else "error", (res.text.strip() or "(no text)") + f"\n{note}" if res.ok
             else f"{res.error.strip()} {note}")
    return res.session if res.session.id else resume


def compacted(spec: Spec, cfg: config.Config, device: devices.Device, prompt: str, recent: str, turn: int,
              resume: Session) -> Session:
    row = state.run_get(spec.id) or {}
    if not resume.id or resume.runner != "claude" or row.get("runner") != "claude" or device.remote or not recent:
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
    res = runner.run("/compact", spec.cwd, model=row.get("model") or spec.model, runner="claude",
                     trace=(spec.id, f"turn {turn} compact"), resume=resume, cfg=cfg, device=device,
                     timeout=TURN_TIMEOUT_SECONDS, repo=session_repo(spec, cfg), fallback=False,
                     wait_out_limits=False, resume_only=True)
    log_line(spec, "compact" if res.ok else "error",
             f"compacted {tokens:,} tokens of context before a new task (JEV {switch.note()}) {runner.cost_note(res)}"
             if res.ok else f"compaction failed: {util.first_line(res.error)}")
    return res.session if res.ok and res.session.id else resume


def clone_repo(spec: Spec, cfg: config.Config, device: devices.Device) -> str:
    repo = cfg.repo(spec.repo)
    token = gh.clone_token(cfg.github_app, spec.github)
    head = devices.clone(device, spec.github, spec.base, spec.branch, spec.cwd, token)
    with util.repo_lock(repo.path):
        util.git(["fetch", "-q", "origin", spec.base], repo.path, timeout=600)
        util.git(["cat-file", "-e", f"{head}^{{commit}}"], repo.path)
    spec.cloned = spec.fetched = head
    save_spec(spec)
    log_line(spec, "device", f"cloned {spec.github} on {device.name} at {head[:10]}, on {spec.branch} from {spec.base}")
    return head


def fetch_back(spec: Spec, repo: config.Repo, device: devices.Device, head: str) -> None:
    ref = f"refs/heads/{spec.branch}"
    text = devices.bundle(device, spec.cwd, spec.branch, spec.fetched)
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "work.bundle"
        file.write_bytes(base64.b64decode(text))
        with util.repo_lock(repo.path):
            util.git(["-c", "fetch.fsckObjects=true", "fetch", "-q", "--no-tags", str(file), f"+{ref}:{ref}"],
                     repo.path, timeout=600)
            fetched = util.git(["rev-parse", ref], repo.path).strip()
    if fetched != head:
        raise RuntimeError(f"fetched {fetched[:10]} for {spec.branch} but the device is at {head[:10]}")
    spec.fetched = head


def denied_work(spec: Spec, repo: config.Repo) -> list[str]:
    found = {path: path for path in diffguard.between(repo.path, spec.cloned, spec.fetched, deny=spec.deny).denied}
    empty = util.git(["hash-object", "-t", "tree", os.devnull], repo.path).strip()
    listed = util.git(["rev-list", "--reverse", "--parents", f"{spec.cloned}..{spec.fetched}"], repo.path)
    for line in listed.splitlines():
        commit, *parents = line.split()
        for parent in parents or [empty]:
            for path in diffguard.between(repo.path, parent, commit, deny=spec.deny).denied:
                found.setdefault(path, f"{path} in {commit[:10]}")
    return list(found.values())


def push_fetched(spec: Spec, repo: config.Repo) -> None:
    if denied := denied_work(spec, repo):
        held = ", ".join(denied)
        if held != spec.held:
            log_line(spec, "error", f"not pushing {spec.branch}: its commits touch denied paths {held}")
        spec.held = held
        return
    util.git(["push", "-q", "origin", f"{spec.fetched}:refs/heads/{spec.branch}"], repo.path, timeout=600)
    spec.pushed, spec.held = spec.fetched, ""
    log_line(spec, "device", f"pushed {spec.fetched[:10]} to {spec.branch}")


def return_work(spec: Spec, cfg: config.Config, device: devices.Device, message: str, last: bool = False) -> None:
    try:
        repo = cfg.repo(spec.repo)
        spec.unreturned = "bringing the work back did not finish"
        spec.unreturned = attempted(spec, "bringing back", last, lambda: bring_back(spec, repo, device, message))
        if not spec.unreturned and spec.fetched not in (spec.pushed, spec.cloned):
            attempted(spec, "pushing", last, lambda: push_fetched(spec, repo))
    finally:
        save_spec(spec)


def bring_back(spec: Spec, repo: config.Repo, device: devices.Device, message: str) -> None:
    head, left_out = devices.commit_all(device, spec.cwd, message, spec.deny)
    for path in left_out:
        log_line(spec, "device", f"left {path} out of the commit: a denied path")
    if head != spec.fetched:
        fetch_back(spec, repo, device, head)


def attempted(spec: Spec, what: str, last: bool, step) -> str:
    try:
        step()
    except (RuntimeError, ValueError, OSError) as e:
        text = util.one_line(str(e), 400)
        log_line(spec, "error", f"{what} the work of {spec.branch}{' at the end' if last else ''}: {text}")
        return text
    return ""


def unreturned_reason(spec: Spec) -> str:
    return f"the work of {spec.branch} is not fetched back from {spec.cwd}" if spec.unreturned else ""


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
            with devices.lease(spec.device, spec.id, lambda text: log_line(spec, "device", text),
                               lambda: unreturned_reason(spec)) as device:
                if spec.github:
                    clone_repo(spec, cfg, device)
                resume, turn, prompt = NO_SESSION, 0, spec.prompt
                idle_since = time.monotonic()
                try:
                    while True:
                        if prompt:
                            turn += 1
                            resume = take_turn(spec, cfg, device, prompt, turn, resume)
                            if spec.github:
                                return_work(spec, cfg, device, f"arete session {spec.id}, turn {turn}")
                            idle_since = time.monotonic()
                        prompt = "\n\n".join(state.take_messages(session_id))
                        if prompt:
                            continue
                        if time.monotonic() - idle_since > cfg.session_idle_hours * 3600:
                            summary = f"closed after {cfg.session_idle_hours:g}h with no message"
                            break
                        time.sleep(INBOX_POLL_SECONDS)
                finally:
                    if spec.github and spec.cloned:
                        return_work(spec, cfg, device, f"arete session {spec.id}, last changes", last=True)
                if spec.unreturned:
                    status, summary = "failed", "the session's work did not come back from the device"
                if summary:
                    close(spec, status, summary)
        except SessionStopped:
            status, summary = "stopped", record.STOPPED if record.stop_asked(session_id) else "stopped by a signal"
        except devices.DeviceError as e:
            status, summary = "failed", util.one_line(str(e), 500)
        except Exception as e:
            status, summary = "failed", f"{type(e).__name__}: {util.first_line(str(e), 300)}"
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        close(spec, status, devices.with_left_up(summary, session_id))
        return status


def work_note(spec: Spec) -> str:
    if not spec.cloned:
        return f"; nothing was cloned from {spec.github}"
    if spec.unreturned:
        return (f"; the work on {spec.branch} did not come back: it stays on {spec.device} in {spec.cwd} "
                f"({spec.unreturned}); {spec.fetched[:10]} is the last commit fetched")
    if spec.fetched == spec.pushed:
        return f"; the work is pushed as {spec.pushed[:10]} on {spec.branch} of {spec.github}"
    earlier = f" ({spec.pushed[:10]} was pushed earlier)" if spec.pushed else ""
    if spec.fetched == spec.cloned:
        return f"; the session made no changes to {spec.github}"
    if spec.held:
        return (f"; the work is fetched into the local {spec.repo} on {spec.branch} but not pushed: "
                f"its commits touch denied paths {spec.held}{earlier}")
    return f"; the work is fetched into the local {spec.repo} on {spec.branch} but not pushed; see the log{earlier}"


def close(spec: Spec, status: str, summary: str) -> None:
    kept = work_note(spec) if spec.github else (
        f"; the worktree stays at {paths.short_path(spec.cwd)} on {spec.branch}" if spec.branch else "")
    paid = record.metered_spent(spec.id)
    end_line = f"{status}: {summary}{kept} ({util.paid_note(paid)})"
    if record.settle(spec.id, status, end_line=end_line, summary=f"{summary}{kept}"[:500]):
        write_log(spec, "end", end_line)


def views(limit: int = 30, connect=state.db) -> list[dict]:
    out = []
    rows = state.runs(kind="session", limit=limit, connect=connect)
    paid = state.paid_by_run([r["id"] for r in rows], connect)
    for r in rows:
        spec = util.read_json(spec_file(r["id"]), {})
        out.append({k: r.get(k) for k in ("id", "status", "started", "finished", "model", "runner", "device",
                                          "cost_usd", "summary", "branch")}
                   | {"paid": paid.get(r["id"], 0), "prompt": util.first_line(spec.get("prompt", ""), 200),
                      "tools": spec.get("tools", []), "cwd": paths.short_path(spec.get("cwd", "")),
                      "repo": spec.get("repo", "")})
    return out


def known_models(cfg: config.Config) -> list[str]:
    since = util.iso(util.now() - timedelta(days=SEEN_MODEL_DAYS))
    seen = {s["model"] for s in state.steps_since(since, connect=state.reader) if s["model"]}
    return sorted({*settings.MODEL_ALIASES, *(m for m in cfg.models.values() if m), *cfg.pricing, *seen})


def choices() -> dict:
    cfg = config.load()
    fleet = devices.known()
    return {"devices": [d.name for d in fleet],
            "repos": [r.name for r in cfg.repos], "models": known_models(cfg), "runners": list(runner.RUNNERS),
            "tools": sorted(runner.KNOWN_TOOLS), "default_tools": SESSION_TOOLS, "default_model": cfg.model("session"),
            "idle_hours": cfg.session_idle_hours}
