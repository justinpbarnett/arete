from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import paths

MODEL_ROLES = ("plan", "build", "verify", "review", "learn", "uat", "miner", "apply", "eval_mine", "triage", "chat",
               "classify", "loop", "session", "research")
MODEL_FALLBACKS = {"triage": "haiku", "chat": "opus", "classify": "haiku", "loop": "sonnet", "session": "sonnet",
                   "research": "opus"}


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
    tracker: Literal["github", "jira"] = "github"
    jira_project: str = ""
    jira_label: str = ""
    layer: Path | None = None
    parts: list[Part] = field(default_factory=list)
    jev: bool = True

    @property
    def layer_dir(self) -> Path:
        return self.layer or self.path / ".agents"


STATES = ("running", "pr", "failed", "needs-info")
DASHBOARD_PORT = 8796


@dataclass
class JiraConfig:
    env_file: Path | None = None
    project: str = ""
    transitions: dict[str, str] = field(default_factory=dict)
    landed: dict[str, str] = field(default_factory=dict)
    auto: bool = False
    auto_per_refresh: int = 3
    sprint_only: bool = True


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
    screen_hold: float = 0.4
    screen_reach: float = 0.6
    proof_hold: float = 0.5
    fixed_skip: float = 0.7
    research_rerun: float = 0.4
    research_answered: float = 0.8
    feedback_skip: float = 0.0
    risk_weights: dict[str, float] = field(default_factory=lambda: {"blast": 0.4, "sensitivity": 0.4, "surprise": 0.2})
    guard: bool = True
    compact_tokens: int = 120_000


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
    miner_lookback_hours: float = 24
    miner_max_digest_chars: int = 90_000
    max_parallel: int = 1
    dashboard_port: int = DASHBOARD_PORT
    dashboard_owner: str = ""
    loop_dirs: list[Path] = field(default_factory=lambda: [paths.CONFIG_DIR / "loops"])
    queues: dict[str, int] = field(default_factory=dict)
    pricing: dict[str, tuple[float, float]] = field(default_factory=dict)
    local_providers: list[str] = field(default_factory=lambda: ["ollama"])
    daily_budget_usd: float | None = None
    session_idle_hours: float = 2

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
        dashboard_port=int(data.get("dashboard", {}).get("port", DASHBOARD_PORT)),
        dashboard_owner=data.get("dashboard", {}).get("owner", ""),
    )
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
    transitions = {k: str(v) for k, v in (j.get("transitions") or {}).items() if v}
    if bad := sorted(set(transitions) - set(STATES)):
        raise ValueError(f"[jira.transitions]: unknown state {', '.join(bad)}; use {', '.join(STATES)}"
                         + ("; merges move tickets through [jira.landed]" if "merged" in bad else ""))
    cfg.jira = JiraConfig(
        env_file=paths.expand(j["env_file"]) if j.get("env_file") else None,
        project=j.get("project", ""),
        transitions=transitions,
        landed={str(branch): str(target) for branch, target in (j.get("landed") or {}).items() if target},
        auto=bool(j.get("auto", False)),
        auto_per_refresh=max(0, int(j.get("auto_per_refresh", JiraConfig.auto_per_refresh))),
        sprint_only=bool(j.get("sprint_only", JiraConfig.sprint_only)),
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
        screen_hold=float(jv.get("screen_hold", JevConfig.screen_hold)),
        screen_reach=float(jv.get("screen_reach", JevConfig.screen_reach)),
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
    for r in data.get("repo", []):
        tracker = r.get("tracker", "github")
        if tracker not in ("github", "jira"):
            raise ValueError(f"repo {r['name']}: tracker must be github or jira, not '{tracker}'")
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

    @property
    def builder_bash(self) -> list[str]:
        return self.allow_bash + list(self.checks.values()) + (self.verify.bash if self.verify else [])

    @property
    def config_file(self) -> Path:
        return self.dir / "config.toml"

    @property
    def expertise_file(self) -> Path:
        return self.dir / "expertise.md"

    @property
    def evals_dir(self) -> Path:
        return self.dir / "evals"


LAYER_SECTIONS = {"setup", "checks", "limits", "agent", "trust", "git", "verify"}


def load_layer(layer_dir: Path) -> RepoLayer:
    layer = RepoLayer(dir=layer_dir)
    if not layer.config_file.exists():
        return layer
    data = tomllib.loads(layer.config_file.read_text())
    setup = data.get("setup", {})
    layer.setup = list(setup.get("commands", []))
    layer.copy = list(setup.get("copy", []))
    layer.checks = dict(data.get("checks", {}))
    lim = data.get("limits", {})
    layer.max_diff_lines = int(lim.get("max_diff_lines", layer.max_diff_lines))
    layer.deny = list(lim.get("deny", []))
    layer.max_attempts = int(lim.get("max_attempts", layer.max_attempts))
    layer.timeout_minutes = int(lim.get("timeout_minutes", layer.timeout_minutes))
    agent = data.get("agent", {})
    layer.allow_bash = list(DEFAULT_ALLOW_BASH) + list(agent.get("allow_bash", []))
    trust = data.get("trust", {})
    layer.trust_level = trust.get("level", "pr")
    layer.automerge_after = int(trust.get("automerge_after", 5))
    g = data.get("git", {})
    layer.base_branch = g.get("base", "")
    layer.subject = g.get("subject", layer.subject)
    layer.branch = g.get("branch", layer.branch)
    if "verify" in data:
        v = data["verify"]
        layer.verify = Verify(start=v.get("start", ""), url=v.get("url", Verify.url), ready=v.get("ready", "/"),
                              ready_seconds=int(v.get("ready_seconds", Verify.ready_seconds)),
                              allow_bash=list(v.get("allow_bash", [])), notes=v.get("notes", "").strip())
    return layer
