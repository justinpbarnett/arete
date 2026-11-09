from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, cron, devices, paths, runner, state, util

ENGINE_DIR = paths.PACKAGE_DIR / "loops"
SWITCHES_FILE = paths.CONFIG_DIR / "loop-switches.json"
TRIES = 5
METRIC_RULE = re.compile(r"^\s*([\w.\-/]+)\s*(<=|>=|<|>|==|!=)\s*(-?\d+(?:\.\d+)?)\s*$")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TRUST_ORDER = ("untrusted", "internal", "owner")
CLIMB_GOALS = {"max": True, "higher": True, "up": True, "min": False, "lower": False, "down": False}
FINDING_EVENT = "finding"
GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$")


@dataclass
class Trigger:
    cron: cron.Cron | None = None
    every: float | None = None
    run_at_load: bool = False
    events: list[str] = field(default_factory=list)
    webhook: bool = False
    metric: tuple[str, str, float] | None = None
    group: str = ""
    trust: str = "untrusted"

    def group_of(self, e: dict) -> str:
        return str(e.get(self.group) or (e.get("data") or {}).get(self.group) or "") if self.group else ""

    def refusal(self, e: dict) -> str:
        takes = [*self.events, *(["metric"] if self.metric else [])]
        if self.events and e.get("type") not in takes:
            return f"takes only {', '.join(takes)} events, and this one is {e.get('type')}"
        if trust_rank(e.get("trust")) < trust_rank(self.trust):
            return f"takes only {self.trust} events or above, and this one is {e.get('trust') or 'untrusted'}"
        return ""

    @property
    def schedule(self) -> str:
        if self.cron:
            return f"cron {self.cron.text}"
        if self.every:
            return f"every {fmt_seconds(self.every)}"
        return ""

    @property
    def summary(self) -> str:
        parts = [self.schedule] if self.schedule else []
        if self.events:
            parts.append(" ".join(self.events))
        if self.metric:
            parts.append("metric " + " ".join(map(str, self.metric)))
        if self.webhook:
            parts.append("webhook")
        return ", ".join(parts)


@dataclass
class Loop:
    name: str
    dir: Path
    origin: str
    kind: str = "watch"
    description: str = ""
    enabled: bool = True
    repo: str = ""
    trigger: Trigger = field(default_factory=Trigger)
    sources: list[dict] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    try_command: list[str] = field(default_factory=list)
    script: list[str] = field(default_factory=list)
    model: str = ""
    runner: str = ""
    tools: list[str] = field(default_factory=lambda: list(runner.READ_TOOLS))
    device: str = "local"
    timeout_minutes: float = 30
    budget_usd: float | None = None
    daily_budget_usd: float | None = None
    queue: str = ""
    stale_hours: float | None = None
    classify: dict = field(default_factory=dict)
    emits: list[dict] = field(default_factory=list)
    spec: dict = field(default_factory=dict)
    error: str = ""

    @property
    def file(self) -> Path:
        return self.dir / "loop.toml"

    @property
    def prompt_file(self) -> Path:
        return self.dir / "prompt.md"

    @property
    def fed_by_events(self) -> bool:
        return bool(self.sources or self.trigger.webhook)

    @property
    def takes_findings(self) -> bool:
        return FINDING_EVENT in self.trigger.events

    @property
    def queue_name(self) -> str:
        return self.queue or self.name

    @property
    def agent(self) -> bool:
        return self.kind in KINDS and KINDS[self.kind].agent

    @property
    def tries(self) -> int:
        return int(self.spec.get("iterations", TRIES))

    def model_for(self, cfg: config.Config) -> str:
        return self.model or cfg.model("loop")

    def agent_models(self, cfg: config.Config) -> list[str]:
        if not self.agent:
            return []
        return [self.model_for(cfg), *([self.classify.get("model") or cfg.model("classify")] if self.classify else [])]

    def meters_spend(self, cfg: config.Config) -> bool:
        return any(runner.metered(model, self.runner) for model in self.agent_models(cfg))

    def usage_hold(self, cfg: config.Config, connect=state.db) -> str:
        held = next(filter(None, (runner.usage_hold(model, self.runner, connect) for model in self.agent_models(cfg))),
                    None)
        return runner.hold_words(held) if held else ""

    def work_repo(self, cfg: config.Config) -> config.Repo:
        for r in cfg.repos:
            if r.name == self.repo:
                return r
        path = paths.expand(self.repo)
        m = GITHUB_REMOTE.search(util.git(["remote", "get-url", "origin"], path, check=False).strip())
        return config.Repo(name=self.repo, path=path, github=m.group(1) if m else "")

    def view(self) -> dict:
        return {"name": self.name, "origin": self.origin, "kind": self.kind, "description": self.description,
                "enabled": self.enabled, "repo": self.repo,
                "trigger": "" if self.error else self.trigger.summary or ("any source event" if self.sources
                                                                          else "by hand"),
                "sources": [s.get("type") for s in self.sources], "model": self.model if self.agent else "",
                "runner": self.runner, "device": self.device, "queue": self.queue_name,
                "budget_usd": self.budget_usd, "daily_budget_usd": self.daily_budget_usd,
                "emits": [e.get("type") + (" (approve)" if e.get("approve") else "") for e in self.emits],
                "dir": paths.short_path(self.dir), "error": self.error}


@dataclass
class Kind:
    name: str
    table: str = ""
    needs: str = ""
    needs_hint: str = ""
    agent: bool = True
    worktree: bool = False
    run: Callable[..., tuple[str, str]] | None = None

    def problems(self, loop: Loop, remote: bool) -> list[str]:
        problems = []
        if self.agent and not loop.prompt_file.is_file():
            problems.append(f"a {self.name} loop needs prompt.md")
        if self.needs and blank(loop.spec.get(self.needs)):
            problems.append(f"a {self.name} loop needs [{self.table}] "
                            + ", ".join(filter(None, (self.needs, self.needs_hint))))
        if self.worktree:
            iterations = str(loop.spec.get("iterations", TRIES))
            if not iterations.isdigit() or int(iterations) < 1:
                problems.append(f"[{self.table}] iterations '{iterations}' must be a whole number above 0")
            if remote:
                problems.append(f"a {self.name} loop keeps or reverts each try in a local worktree, so it runs on "
                                "local")
            if not loop.repo:
                problems.append(f"a {self.name} loop needs [run] repo, a repo name from the config or a path to a git "
                                "repo")
        return problems


KINDS = {kind.name: kind for kind in (
    Kind("command", table="run", needs="command", agent=False),
    Kind("watch"),
    Kind("climb", table="climb", needs="measure", needs_hint="a command that prints a number", worktree=True),
    Kind("search", table="search", needs="check", needs_hint="a command that exits 0 on a hit", worktree=True),
)}


def runs_kind(name: str):
    def bind(run: Callable[..., tuple[str, str]]) -> Callable[..., tuple[str, str]]:
        KINDS[name].run = run
        return run
    return bind


def worktree_kinds() -> list[str]:
    return [kind.name for kind in KINDS.values() if kind.worktree]


def blank(value) -> bool:
    return not (value.strip() if isinstance(value, str) else value)


class LoopError(ValueError):
    pass


def trust_rank(trust) -> int:
    return TRUST_ORDER.index(trust) if trust in TRUST_ORDER else 0


def fmt_seconds(s: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size and s % size == 0:
            return f"{int(s // size)}{unit}"
    return f"{s:g}s"


def folders(cfg: config.Config) -> list[tuple[str, str, Path]]:
    out = [("arete", "", ENGINE_DIR)]
    out += [("private", "", d) for d in cfg.loop_dirs]
    out += [("repo", r.name, r.layer_dir / "loops") for r in cfg.repos]
    return [(origin, repo, d / name) for origin, repo, d in out if d.is_dir()
            for name in sorted(p.name for p in d.iterdir() if (p / "loop.toml").is_file())]


def load_all(cfg: config.Config | None = None) -> list[Loop]:
    cfg = cfg or config.load()
    loops: dict[str, Loop] = {}
    switched = switches()
    for origin, repo, folder in folders(cfg):
        name = f"{repo}/{folder.name}" if repo else folder.name
        try:
            loop = load(folder, origin, repo, cfg)
        except (LoopError, cron.BadSchedule, tomllib.TOMLDecodeError, ValueError, TypeError, KeyError,
                AttributeError) as e:
            loop = broken(name, folder, origin, str(e) if isinstance(e, LoopError | cron.BadSchedule)
                          else f"{type(e).__name__}: {e}")
        if not loop.error and name in switched:
            loop.enabled = switched[name]
        if name in loops:
            loop.enabled, loop.error = False, f"another loop is already named {name} in {loops[name].dir}"
            loops[f"{name}@{folder}"] = loop
            continue
        loops[name] = loop
    return list(loops.values())


def broken(name: str, folder: Path, origin: str, error: str) -> Loop:
    try:
        declared = tomllib.loads((folder / "loop.toml").read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        declared = {}
    return Loop(name=name, dir=folder, origin=origin, kind=str(declared.get("kind", "?")),
                description=str(declared.get("description", "")).strip(), enabled=False, error=error)


def find(name: str, cfg: config.Config | None = None) -> Loop:
    for loop in load_all(cfg):
        if loop.name == name:
            return loop
    raise LoopError(f"no loop named {name}; see `arete loop list`")


def load(folder: Path, origin: str, repo: str, cfg: config.Config) -> Loop:
    data = tomllib.loads((folder / "loop.toml").read_text())
    if not NAME.match(folder.name):
        raise LoopError(f"folder name '{folder.name}' must be lowercase letters, digits and dashes")
    run, trig = data.get("run", {}), data.get("trigger", {})
    kind = str(data.get("kind", "watch"))
    known = KINDS.get(kind)
    loop = Loop(
        name=f"{repo}/{folder.name}" if repo else folder.name,
        dir=folder,
        origin=origin,
        kind=kind,
        description=str(data.get("description", "")).strip(),
        enabled=bool(data.get("enabled", True)),
        repo=str(run.get("repo") or repo),
        sources=[dict(s) for s in data.get("source", [])],
        command=as_argv(run.get("command")),
        try_command=as_argv(run.get("try_command")),
        script=as_argv(run.get("script")),
        model=str(run.get("model", "")),
        runner=str(run.get("runner", "")),
        tools=list(run.get("tools", runner.EDIT_TOOLS if known and known.worktree else runner.READ_TOOLS)),
        device=str(run.get("device", "local")),
        timeout_minutes=float(run.get("timeout_minutes", 30)),
        budget_usd=float(run["budget_usd"]) if run.get("budget_usd") else None,
        daily_budget_usd=float(run["daily_budget_usd"]) if run.get("daily_budget_usd") else None,
        queue=str(run.get("queue", "")),
        stale_hours=float(run["stale_hours"]) if run.get("stale_hours") else None,
        classify=dict(data.get("classify", {})),
        emits=[dict(e) for e in data.get("emit", [])],
        spec=dict(data.get(known.table, {})) if known and known.table else {},
    )
    loop.trigger = Trigger(
        cron=cron.parse(trig["cron"]) if trig.get("cron") else None,
        every=cron.seconds(trig["every"]) if trig.get("every") else None,
        run_at_load=bool(trig.get("run_at_load", False)),
        events=as_list(trig.get("event")),
        webhook=bool(trig.get("webhook", False)),
        metric=metric_rule(trig["metric"]) if trig.get("metric") else None,
        group=str(trig.get("group", "")),
        trust=str(trig.get("trust", "untrusted")),
    )
    validate(loop, cfg)
    return loop


def as_argv(value) -> list[str]:
    if not value:
        return []
    return shlex.split(value) if isinstance(value, str) else [str(v) for v in value]


def climb_goal(spec: dict) -> str:
    return str(spec.get("goal", "max")).strip().lower()


def as_list(value) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else [str(v) for v in value]


def metric_rule(text: str) -> tuple[str, str, float]:
    m = METRIC_RULE.match(text)
    if not m:
        raise LoopError(f"metric trigger '{text}' must look like `name < 0.8`")
    return m.group(1), m.group(2), float(m.group(3))


def proof_problems(loop: Loop) -> list[str]:
    name, problems = loop.spec["proof"], []
    if not isinstance(name, str) or blank(name) or Path(name).is_absolute() or ".." in Path(name).parts:
        problems.append('[search] proof must name a file in the scratch folder, such as "repro.py"')
    if not any(e.get("type") == "push" for e in loop.emits):
        problems.append("[search] proof is checked before a push lands, so the loop needs a push emit")
    return problems


def validate(loop: Loop, cfg: config.Config) -> None:
    from .emits import EMITS
    from .sources import SOURCES, metric_event

    problems = []
    kind = KINDS.get(loop.kind)
    if not kind:
        problems.append(f"kind '{loop.kind}' is not one of {', '.join(KINDS)}")
    try:
        remote, device_problem = devices.get(loop.device).remote, ""
    except devices.DeviceError as e:
        remote, device_problem = False, str(e)
    if loop.trigger.cron and loop.trigger.every:
        problems.append("[trigger] takes cron or every, not both")
    if loop.trigger.trust not in TRUST_ORDER:
        problems.append(f"[trigger] trust '{loop.trigger.trust}' is not one of {', '.join(TRUST_ORDER)}")
    elif loop.trigger.metric and (why := loop.trigger.refusal(metric_event(loop, 0))):
        problems.append(f"[trigger] metric would never fire: the loop {why}")
    if kind:
        problems += kind.problems(loop, remote)
    if loop.kind == "climb" and (goal := climb_goal(loop.spec)) not in CLIMB_GOALS:
        problems.append(f"[climb] goal '{goal}' is not one of {', '.join(CLIMB_GOALS)}")
    if loop.kind == "search" and "proof" in loop.spec:
        problems += proof_problems(loop)
    problems += runner.agent_problems(loop.runner, loop.tools)
    problems += [device_problem] if device_problem else []
    if loop.agent:
        problems += [f"{reason}, so a loop run by pi cannot have {', '.join(tools)}"
                     for reason, tools in runner.withheld(loop.model_for(cfg), loop.runner, loop.tools, remote).items()]
    if loop.agent and loop.fed_by_events and (acting := sorted(set(loop.tools) - runner.READ_ONLY_TOOLS)):
        problems.append(f"quarantine: a loop fed by events or a webhook reads untrusted text, so it cannot have "
                        f"the acting tools {', '.join(acting)}; emit a ticket and let the pipeline act")
    elif (loop.agent and loop.takes_findings and trust_rank(loop.trigger.trust) < trust_rank("internal")
          and (acting := sorted(set(loop.tools) - runner.READ_ONLY_TOOLS))):
        problems.append(f"a loop that takes findings from other loops and has the acting tools {', '.join(acting)} "
                        f"needs [trigger] trust = \"internal\" or above, so a finding from untrusted text never "
                        f"reaches it")
    for s in loop.sources:
        if s.get("type") not in SOURCES:
            problems.append(f"source type '{s.get('type')}' is not one of {', '.join(SOURCES)}")
        else:
            problems += SOURCES[s["type"]].problems(s, loop, cfg)
    for e in loop.emits:
        if e.get("type") not in EMITS:
            problems.append(f"emit type '{e.get('type')}' is not one of {', '.join(EMITS)}")
        else:
            problems += EMITS[e["type"]].problems(e, loop, cfg)
    if loop.classify and not loop.classify.get("prompt"):
        problems.append("[classify] needs prompt, the yes or no question")
    if problems:
        raise LoopError("; ".join(problems))


def check_env(loop: Loop, run_id: str) -> dict[str, str]:
    return {"ARETE_RUN_ID": run_id, "ARETE_LOOP": loop.name, "ARETE_LOOP_DIR": str(loop.dir),
            "ARETE_SCRATCH": str(paths.scratch_dir(run_id))}


def prompt_text(loop: Loop) -> str:
    return loop.prompt_file.read_text() if loop.prompt_file.is_file() else ""


def switches() -> dict[str, bool]:
    try:
        return {str(k): bool(v) for k, v in json.loads(SWITCHES_FILE.read_text()).items()}
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return {}


def set_enabled(loop: Loop, enabled: bool) -> None:
    current = switches()
    current[loop.name] = enabled
    SWITCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SWITCHES_FILE.write_text(json.dumps(dict(sorted(current.items())), indent=2) + "\n")
