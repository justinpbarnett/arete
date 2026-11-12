from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import paths

MODEL_ROLES = ("plan", "build", "verify", "review", "learn", "uat", "miner", "apply", "eval_mine", "triage", "chat",
               "classify", "loop", "session", "research", "explain")
MODEL_FALLBACKS = {"triage": "haiku", "chat": "opus", "classify": "haiku", "loop": "sonnet", "session": "sonnet",
                   "research": "opus", "explain": "haiku"}
STEP_ALIASES = {"implement": "build"}
STEP_NAMES = (*MODEL_ROLES, *STEP_ALIASES)
STEP_KEYS = ("tier", "runner", "provider", "model", "effort", "timeout_minutes", "tries", "tools", "mcp", "instructions",
             "session")
STEP_COUNTS = ("timeout_minutes", "tries")
STEP_LISTS = ("tools", "mcp", "instructions")
INSTRUCTIONS = ("user", "repo")
SESSIONS = ("resume", "fresh")
MCP_KEYS = ("command", "args", "url")
MCP_NAME = re.compile(r"[A-Za-z0-9_-]+")
TIER_KEYS = ("runner", "provider", "model")
KINDS = ("feat", "fix")
TRACKERS = ("github", "jira", "local")
REPLAY_KEYS = ("all", "dir")
FACTORY_PATHS = ("classic", "adw")
ENV_NAME = r"\*|[A-Za-z_][A-Za-z0-9_]*\*?"
ENV_KEYS = ("pass", "providers")


class UnknownRepo(KeyError):
    def __str__(self) -> str:
        return self.args[0]


@dataclass
class Part:
    dir: str
    github: str


@dataclass
class Repo:
    name: str
    path: Path
    github: str = ""
    pipeline: bool = False
    mine: bool = True
    public: bool = False
    tracker: Literal["github", "jira", "local"] = "github"
    jira_project: str = ""
    jira_label: str = ""
    layer: Path | None = None
    parts: list[Part] = field(default_factory=list)
    jev: bool = True

    @property
    def layer_dir(self) -> Path:
        return self.layer or self.path / ".agents"


STATES = ("running", "pr", "failed", "needs-info")
DELIVERED = "done"
RUN_LABELS = (*STATES, DELIVERED)
TASK_STATES = ("todo", *STATES, DELIVERED, "closed")
TASK_FINISHED = (DELIVERED, "closed")
RESEARCH_MOVES = ("running", "pr", "failed", DELIVERED)
DASHBOARD_PORT = 8796


@dataclass
class JiraConfig:
    env_file: Path | None = None
    project: str = ""
    transitions: dict[str, str] = field(default_factory=dict)
    research: dict[str, str] = field(default_factory=dict)
    landed: dict[str, str] = field(default_factory=dict)
    auto: bool = False
    auto_per_refresh: int = 3
    sprint_only: bool = True
    skip_types: list[str] = field(default_factory=list)


@dataclass
class JevConfig:
    enabled: bool = True
    model: str = "jev-latest"
    confidence: float = 0.8
    injection: float = 0.5
    risk_hold: float = 0.5
    irreversible_hold: float = 0.5
    secrets_hold: float = 0.55
    triage_skip: float = 0.2
    screen_lines: dict[str, float] = field(default_factory=lambda: {
        "weakens": 0.5, "secrets": 0.7, "outside": 0.7, "irreversible": 0.5, "feature": 0.6, "removes": 0.5,
        "renames": 0.5, "reach": 0.8})
    proof_hold: float = 0.5
    fixed_skip: float = 0.7
    research_rerun: float = 0.4
    research_answered: float = 0.8
    feedback_skip: float = 0.0
    risk_weights: dict[str, float] = field(default_factory=lambda: {"blast": 0.4, "sensitivity": 0.4, "surprise": 0.2})
    guard: bool = True
    compact_tokens: int = 120_000


@dataclass
class ReplayConfig:
    all: bool = False
    dir: Path | None = None


@dataclass
class Environment:
    passes: tuple[str, ...] = ()
    providers: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass
class Job:
    label: str
    log: Path | None = None
    max_age_hours: float | None = None
    keepalive: bool = False


@dataclass
class Probe:
    name: str
    command: list[str]
    expect: str = "ok"
    every_hours: float = 24
    timeout: int = 180


@dataclass
class Config:
    alert_repo: str = ""
    global_repo: str = "ai"
    arete_repo: str = "arete"
    models: dict[str, str] = field(default_factory=dict)
    repos: list[Repo] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    jira: JiraConfig = field(default_factory=JiraConfig)
    jev: JevConfig = field(default_factory=JevConfig)
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    environment: Environment = field(default_factory=Environment)
    miner_lookback_hours: float = 24
    miner_max_digest_chars: int = 90_000
    max_parallel: int = 1
    max_parallel_per_repo: int = 1
    dashboard_port: int = DASHBOARD_PORT
    dashboard_owner: str = ""
    loop_dirs: list[Path] = field(default_factory=lambda: [paths.CONFIG_DIR / "loops"])
    queues: dict[str, int] = field(default_factory=dict)
    pricing: dict[str, tuple[float, float]] = field(default_factory=dict)
    local_providers: list[str] = field(default_factory=lambda: ["ollama"])
    daily_budget_usd: float | None = None
    session_idle_hours: float = 2
    tiers: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    tier_map: str = ""
    adws: Path | None = None
    steps: dict[str, dict] = field(default_factory=dict)
    kinds: dict[str, dict[str, dict]] = field(default_factory=dict)
    mcp: dict[str, dict] = field(default_factory=dict)

    def repo(self, name: str) -> Repo:
        for r in self.repos:
            if r.name == name:
                return r
        raise UnknownRepo(f"unknown repo '{name}'; add it to {paths.CONFIG_FILE}")

    def repo_for_path(self, p: str | Path) -> Repo | None:
        p = Path(p).resolve()
        best = None
        for r in self.repos:
            rp = r.path.resolve()
            holds_path = p == rp or rp in p.parents
            if holds_path and (best is None or len(str(rp)) > len(str(best.path.resolve()))):
                best = r
        return best

    def repo_for_work_dir(self, p: str | Path) -> Repo | None:
        if found := self.repo_for_path(p):
            return found
        p, root = Path(p).resolve(), paths.WORKTREES_DIR.resolve()
        if root not in p.parents:
            return None
        top = p.relative_to(root).parts[0]
        return max((r for r in self.repos if top.startswith(f"{r.name}-")), key=lambda r: len(r.name), default=None)

    def jev_allowed(self, repos: list[Repo | None]) -> bool:
        return self.jev.enabled and all(r is None or r.jev for r in repos)

    def model(self, role: str) -> str:
        return self.models.get(role) or MODEL_FALLBACKS.get(role, "")

    def unused_model_keys(self) -> list[str]:
        return sorted(set(self.models) - set(MODEL_ROLES))


def moves_table(jira: dict, table: str, states: tuple[str, ...], hints: dict[str, str] | None = None) -> dict[str, str]:
    moves = {str(k): str(v) for k, v in (jira.get(table) or {}).items() if v}
    if bad := sorted(set(moves) - set(states)):
        raise ValueError(f"[jira.{table}]: unknown state {', '.join(bad)}; use {', '.join(states)}"
                         + "".join(f"; {hints[b]}" for b in bad if b in (hints or {})))
    return moves


def table_of(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"[{where}] must be a table, not {value!r}")
    return value


def step_table(table, where: str) -> dict:
    table = table_of(table, where)
    if bad := sorted(set(table) - set(STEP_KEYS)):
        raise ValueError(f"[{where}]: unknown key {', '.join(bad)}; use {', '.join(STEP_KEYS)}")
    for key in STEP_COUNTS:
        if key in table and (type(table[key]) is not int or table[key] < 1):
            raise ValueError(f"[{where}]: {key} must be a whole number of at least 1, not {table[key]!r}")
    for key in STEP_LISTS:
        if key in table and not (isinstance(table[key], list | tuple) and all(isinstance(v, str) for v in table[key])):
            raise ValueError(f"[{where}]: {key} must be a list of names, not {table[key]!r}")
    if bad := [v for v in table.get("instructions", ()) if v not in INSTRUCTIONS]:
        raise ValueError(f"[{where}]: instructions {', '.join(map(repr, bad))} is not one of {', '.join(INSTRUCTIONS)}")
    if "session" in table and table["session"] not in SESSIONS:
        raise ValueError(f"[{where}]: session {table['session']!r} is not one of {', '.join(SESSIONS)}")
    return {k: tuple(dict.fromkeys(v)) if k in STEP_LISTS else v if k in STEP_COUNTS else str(v) for k, v in table.items()}


def mcp_tables(data) -> dict[str, dict]:
    servers = {}
    for name, entry in table_of(data, "mcp").items():
        where = f"[mcp.{name}]"
        if not MCP_NAME.fullmatch(name):
            raise ValueError(f"{where}: a server name takes only letters, digits, _ and -")
        if bad := sorted(set(table_of(entry, f"mcp.{name}")) - set(MCP_KEYS)):
            raise ValueError(f"{where}: unknown key {', '.join(bad)}; use {', '.join(MCP_KEYS)}")
        if ("command" in entry) == ("url" in entry):
            raise ValueError(f"{where}: set exactly one of command and url")
        for key in ("command", "url"):
            if key in entry and not (isinstance(entry[key], str) and entry[key]):
                raise ValueError(f"{where}: {key} must be a string, not {entry[key]!r}")
        args = entry.get("args", [])
        if "args" in entry and "url" in entry:
            raise ValueError(f"{where}: args goes with command, not url")
        if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
            raise ValueError(f"{where}: args must be a list of strings, not {args!r}")
        servers[name] = ({"command": entry["command"], "args": args} if "command" in entry
                         else {"type": "http", "url": entry["url"]})
    return servers


def step_tables(data, where: str) -> dict[str, dict]:
    data = table_of(data, where)
    if bad := sorted(set(data) - set(STEP_NAMES)):
        raise ValueError(f"[{where}]: unknown step {', '.join(bad)}; use {', '.join(STEP_NAMES)}")
    for alias, role in STEP_ALIASES.items():
        if alias in data and role in data:
            raise ValueError(f"[{where}.{alias}] and [{where}.{role}] are the same step; name the step once")
    return {STEP_ALIASES.get(name, name): step_table(table, f"{where}.{name}") for name, table in data.items()}


def kind_tables(data, where: str) -> dict[str, dict[str, dict]]:
    data = table_of(data, where)
    if bad := sorted(set(data) - set(KINDS)):
        raise ValueError(f"[{where}]: unknown kind {', '.join(bad)}; use {', '.join(KINDS)}")
    kinds = {}
    for kind, table in data.items():
        if bad := sorted(set(table_of(table, f"{where}.{kind}")) - {"steps"}):
            raise ValueError(f"[{where}.{kind}]: unknown key {', '.join(bad)}; use steps")
        kinds[kind] = step_tables(table.get("steps") or {}, f"{where}.{kind}.steps")
    return kinds


def tier_maps(data, where: str) -> dict[str, dict[str, dict[str, str]]]:
    maps: dict[str, dict[str, dict[str, str]]] = {}
    for name, tiers in table_of(data, where).items():
        maps[name] = {}
        for tier, entry in table_of(tiers, f"{where}.{name}").items():
            if bad := sorted(set(table_of(entry, f"{where}.{name}.{tier}")) - set(TIER_KEYS)):
                raise ValueError(f"[{where}.{name}] {tier}: unknown key {', '.join(bad)}; use {', '.join(TIER_KEYS)}")
            maps[name][tier] = {k: str(v) for k, v in entry.items()}
    return maps


def replay_table(data) -> ReplayConfig:
    table = table_of(data, "replay")
    if bad := sorted(set(table) - set(REPLAY_KEYS)):
        raise ValueError(f"[replay]: unknown key {', '.join(bad)}; use {', '.join(REPLAY_KEYS)}")
    return ReplayConfig(all=bool(table.get("all", False)), dir=paths.expand(table["dir"]) if table.get("dir") else None)


def variable_names(value, where: str) -> tuple[str, ...]:
    if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise ValueError(f"{where}: must be a list of variable names, not {value!r}")
    if bad := [v for v in value if not re.fullmatch(ENV_NAME, v)]:
        raise ValueError(f"{where}: {bad[0]!r} is not a variable name; use a name such as HTTPS_PROXY, a prefix ending "
                         "in *, or \"*\"")
    return tuple(dict.fromkeys(value))


def environment_table(data, layer: bool = False) -> Environment:
    table = table_of(data, "environment")
    if layer and "providers" in table:
        raise ValueError("a layer's [environment] takes pass only; [environment.providers] goes in "
                         f"{paths.short_path(paths.CONFIG_FILE)}")
    if bad := sorted(set(table) - set(ENV_KEYS)):
        raise ValueError(f"[environment]: unknown key {', '.join(bad)}; use {'pass' if layer else ', '.join(ENV_KEYS)}")
    providers = {str(name): variable_names(names, f"[environment.providers] {name}")
                 for name, names in table_of(table.get("providers", {}), "environment.providers").items()}
    return Environment(variable_names(table.get("pass", []), "[environment] pass"), providers)


def load(path: Path | None = None) -> Config:
    path = path or paths.CONFIG_FILE
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    cfg = Config(
        alert_repo=data.get("alert_repo", ""),
        global_repo=data.get("global_repo", "ai"),
        arete_repo=data.get("arete_repo", "arete"),
        models=dict(data.get("models", {})),
        miner_lookback_hours=float(data.get("miner", {}).get("lookback_hours", 24)),
        miner_max_digest_chars=int(data.get("miner", {}).get("max_digest_chars", 90_000)),
        max_parallel=max(1, int(data.get("max_parallel", 1))),
        max_parallel_per_repo=max(1, int(data.get("max_parallel_per_repo", 1))),
        dashboard_port=int(data.get("dashboard", {}).get("port", DASHBOARD_PORT)),
        dashboard_owner=data.get("dashboard", {}).get("owner", ""),
    )
    cfg.tiers = tier_maps(data.get("tiers") or {}, "tiers")
    cfg.tier_map = str((data.get("factory") or {}).get("tier_map", ""))
    if (adws := (data.get("factory") or {}).get("adws")) is not None:
        if not isinstance(adws, str):
            raise TypeError(f"[factory] adws must be a folder path, not {adws!r}")
        cfg.adws = path.parent / paths.expand(adws)
    cfg.steps = step_tables(data.get("steps") or {}, "steps")
    cfg.kinds = kind_tables(data.get("kinds") or {}, "kinds")
    serve = data.get("serve", {})
    if "loop_dirs" in serve:
        cfg.loop_dirs = [paths.expand(d) for d in serve["loop_dirs"]]
    cfg.queues = {str(k): max(1, int(v)) for k, v in (serve.get("queues") or {}).items()}
    cfg.daily_budget_usd = float(serve["daily_budget_usd"]) if serve.get("daily_budget_usd") else None
    cfg.session_idle_hours = float(serve.get("session_idle_hours", cfg.session_idle_hours))
    pricing = data.get("pricing", {})
    cfg.local_providers = list(pricing.get("local_providers", cfg.local_providers))
    cfg.pricing = {str(m): (float(r.get("input", 0)), float(r.get("output", 0)))
                   for m, r in (pricing.get("models") or {}).items()}
    j = data.get("jira", {})
    cfg.jira = JiraConfig(
        env_file=paths.expand(j["env_file"]) if j.get("env_file") else None,
        project=j.get("project", ""),
        transitions=moves_table(j, "transitions", STATES, {"merged": "merges move tickets through [jira.landed]",
                                                            DELIVERED: "research tickets move through [jira.research]"}),
        research=moves_table(j, "research", RESEARCH_MOVES),
        landed={str(branch): str(target) for branch, target in (j.get("landed") or {}).items() if target},
        auto=bool(j.get("auto", False)),
        auto_per_refresh=max(0, int(j.get("auto_per_refresh", JiraConfig.auto_per_refresh))),
        sprint_only=bool(j.get("sprint_only", JiraConfig.sprint_only)),
        skip_types=[str(t) for t in j.get("skip_types") or [] if str(t).strip()],
    )
    jv = data.get("jev", {})
    cfg.jev = JevConfig(
        enabled=bool(jv.get("enabled", True)),
        model=str(jv.get("model", JevConfig.model)),
        confidence=float(jv.get("confidence", JevConfig.confidence)),
        injection=float(jv.get("injection", JevConfig.injection)),
        risk_hold=float(jv.get("risk_hold", JevConfig.risk_hold)),
        irreversible_hold=float(jv.get("irreversible_hold", JevConfig.irreversible_hold)),
        secrets_hold=float(jv.get("secrets_hold", JevConfig.secrets_hold)),
        triage_skip=float(jv.get("triage_skip", JevConfig.triage_skip)),
        screen_lines={**JevConfig().screen_lines,
                      **{str(k): float(v) for k, v in (jv.get("screen_lines") or {}).items()}},
        proof_hold=float(jv.get("proof_hold", JevConfig.proof_hold)),
        fixed_skip=float(jv.get("fixed_skip", JevConfig.fixed_skip)),
        research_rerun=float(jv.get("research_rerun", JevConfig.research_rerun)),
        research_answered=float(jv.get("research_answered", JevConfig.research_answered)),
        feedback_skip=float(jv.get("feedback_skip", JevConfig.feedback_skip)),
        risk_weights={**JevConfig().risk_weights, **{str(k): float(v) for k, v in (jv.get("risk_weights") or {}).items()}},
        guard=bool(jv.get("guard", True)),
        compact_tokens=int(jv.get("compact_tokens", JevConfig.compact_tokens)),
    )
    if bad := sorted(set(cfg.jev.risk_weights) - set(JevConfig().risk_weights)):
        raise ValueError(f"[jev.risk_weights]: unknown weight {', '.join(bad)}; use "
                         f"{', '.join(JevConfig().risk_weights)}")
    if bad := sorted(set(cfg.jev.screen_lines) - set(JevConfig().screen_lines)):
        raise ValueError(f"[jev.screen_lines]: unknown question {', '.join(bad)}; use "
                         f"{', '.join(JevConfig().screen_lines)}")
    for r in data.get("repo", []):
        tracker = r.get("tracker", "github")
        if tracker not in TRACKERS:
            raise ValueError(f"repo {r['name']}: tracker must be github, jira or local, not '{tracker}'")
        github = r.get("github", "")
        parts = [Part(p["dir"].strip("/"), p["github"]) for p in r.get("part", [])]
        if parts and (github or tracker != "jira"):
            raise ValueError(f"repo {r['name']}: a repo with parts takes its github from each part and needs "
                             "tracker = \"jira\"")
        cfg.repos.append(
            Repo(
                name=r["name"],
                path=paths.expand(r["path"]),
                github=github,
                pipeline=bool(r.get("pipeline", False)),
                mine=bool(r.get("mine", True)),
                public=bool(r.get("public", False)),
                tracker=tracker,
                jira_project=r.get("jira_project", ""),
                jira_label=r.get("jira_label", ""),
                layer=paths.expand(r["layer"]) if r.get("layer") else None,
                parts=parts or ([Part("", github)] if github else []),
                jev=bool(r.get("jev", True)),
            )
        )
    for j in data.get("job", []):
        cfg.jobs.append(
            Job(
                label=j["label"],
                log=paths.expand(j["log"]) if j.get("log") else None,
                max_age_hours=j.get("max_age_hours"),
                keepalive=bool(j.get("keepalive", False)),
            )
        )
    for p in data.get("probe", []):
        cfg.probes.append(
            Probe(
                name=p["name"],
                command=list(p["command"]),
                expect=p.get("expect", "ok"),
                every_hours=float(p.get("every_hours", 24)),
                timeout=int(p.get("timeout", 180)),
            )
        )
    cfg.replay = replay_table(data.get("replay") or {})
    cfg.mcp = mcp_tables(data.get("mcp") or {})
    cfg.environment = environment_table(data.get("environment") or {})
    from . import steps
    steps.check(cfg)
    return cfg


DEFAULT_ALLOW_BASH = [
    "git status*",
    "git diff*",
    "git log*",
    "git show*",
    "ls*",
    "cat *",
    "head *",
    "tail *",
    "wc *",
    "rg *",
    "grep *",
]

HARNESS_GLOBS = [
    "CLAUDE.md",
    "AGENTS.md",
    "**/CLAUDE.md",
    ".claude/skills/**",
    ".claude/commands/**",
    ".claude/agents/**",
    ".agents/expertise.md",
    ".agents/evals/**",
    "skills/**",
]


@dataclass
class Verify:
    start: str = ""
    url: str = "http://localhost:{port}"
    ready: str = "/"
    ready_seconds: int = 180
    allow_bash: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def bash(self) -> list[str]:
        return ["curl *", "echo *", *self.allow_bash]


@dataclass
class RepoLayer:
    dir: Path
    setup: list[str] = field(default_factory=list)
    copy: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    max_diff_lines: int = 400
    deny: list[str] = field(default_factory=list)
    max_attempts: int = 3
    allow_bash: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_BASH))
    trust_level: Literal["pr", "automerge"] = "pr"
    automerge_after: int = 5
    base_branch: str = ""
    subject: str = "{kind}: {title}"
    branch: str = "arete/{id}-{slug}"
    timeout_minutes: int = 45
    verify: Verify | None = None
    research_dir: str = ""
    tiers: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    steps: dict[str, dict] = field(default_factory=dict)
    kinds: dict[str, dict[str, dict]] = field(default_factory=dict)
    factory_path: str = FACTORY_PATHS[0]
    passes: tuple[str, ...] = ()

    @property
    def builder_bash(self) -> list[str]:
        return self.allow_bash + (self.verify.bash if self.verify else [])

    @property
    def config_file(self) -> Path:
        return self.dir / "config.toml"

    @property
    def expertise_file(self) -> Path:
        return self.dir / "expertise.md"

    @property
    def evals_dir(self) -> Path:
        return self.dir / "evals"


LAYER_SECTIONS = {"setup", "checks", "limits", "agent", "trust", "git", "verify", "research", "steps", "kinds", "tiers",
                  "factory", "environment"}


def section(data: dict, name: str) -> dict:
    return table_of(data.get(name, {}), name)


def load_layer(layer_dir: Path) -> RepoLayer:
    layer = RepoLayer(dir=layer_dir)
    if not layer.config_file.exists():
        return layer
    data = tomllib.loads(layer.config_file.read_text())
    setup = section(data, "setup")
    layer.setup = list(setup.get("commands", []))
    layer.copy = list(setup.get("copy", []))
    layer.checks = dict(section(data, "checks"))
    lim = section(data, "limits")
    layer.max_diff_lines = int(lim.get("max_diff_lines", layer.max_diff_lines))
    layer.deny = list(lim.get("deny", []))
    layer.max_attempts = int(lim.get("max_attempts", layer.max_attempts))
    layer.timeout_minutes = int(lim.get("timeout_minutes", layer.timeout_minutes))
    agent = section(data, "agent")
    layer.allow_bash = list(DEFAULT_ALLOW_BASH) + list(agent.get("allow_bash", []))
    trust = section(data, "trust")
    layer.trust_level = trust.get("level", "pr")
    layer.automerge_after = int(trust.get("automerge_after", 5))
    g = section(data, "git")
    layer.base_branch = g.get("base", "")
    layer.subject = g.get("subject", layer.subject)
    layer.branch = g.get("branch", layer.branch)
    layer.research_dir = str(section(data, "research").get("dir", "")).strip().strip("/")
    layer.tiers = tier_maps(section(data, "tiers"), "tiers")
    layer.steps = step_tables(section(data, "steps"), "steps")
    layer.kinds = kind_tables(section(data, "kinds"), "kinds")
    layer.factory_path = str(section(data, "factory").get("path", layer.factory_path)).strip()
    if layer.factory_path not in FACTORY_PATHS:
        raise ValueError(f"[factory] path '{layer.factory_path}' is not one of {', '.join(FACTORY_PATHS)}")
    layer.passes = environment_table(section(data, "environment"), layer=True).passes
    if "verify" in data:
        v = section(data, "verify")
        layer.verify = Verify(start=v.get("start", ""), url=v.get("url", Verify.url), ready=v.get("ready", "/"),
                              ready_seconds=int(v.get("ready_seconds", Verify.ready_seconds)),
                              allow_bash=list(v.get("allow_bash", [])), notes=v.get("notes", "").strip())
    return layer
