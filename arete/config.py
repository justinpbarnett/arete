from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import paths


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

    @property
    def layer_dir(self) -> Path:
        return self.layer or self.path / ".agents"


STATES = ("running", "pr", "failed", "needs-info")


@dataclass
class JiraConfig:
    env_file: Path | None = None
    project: str = ""
    transitions: dict[str, str] = field(default_factory=dict)
    landed: dict[str, str] = field(default_factory=dict)


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
    miner_lookback_hours: float = 24
    miner_max_digest_chars: int = 90_000
    max_parallel: int = 1

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

    def model(self, role: str) -> str:
        return self.models.get(role, "")


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
    )
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
    )
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
