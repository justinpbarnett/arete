from __future__ import annotations

import contextlib
import math
import re
import shlex
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from . import journeys, outbound, paths, refusals, util

MODEL_ROLES = ("plan", "build", "verify", "review", "learn", "uat", "miner", "apply", "eval_mine", "triage", "chat",
               "classify", "loop", "session", "research", "research_plan", "research_search",
               "research_synthesis", "research_audit", "explain", "document", "capture_evidence")
MODEL_FALLBACKS = {"triage": "haiku", "chat": "opus", "classify": "haiku", "loop": "sonnet", "session": "sonnet",
                   "research": "opus", "research_plan": "opus", "research_search": "sonnet",
                   "research_synthesis": "opus", "research_audit": "opus", "explain": "haiku",
                   "capture_evidence": "sonnet"}
STEP_ALIASES = {"implement": "build"}
INHERITS = {"check_repair": "build", "review_repair": "build", "ci_repair": "build", "eval_grade": "verify",
            "orchestrate": "plan"}
STEP_ROLES = (*MODEL_ROLES, *INHERITS)
STEP_NAMES = (*STEP_ROLES, *STEP_ALIASES)
AB_KEYS = ("step", "kind", "share", "arm")
ARM_KEYS = ("tier", "runner", "provider", "model", "effort")
STEP_KEYS = ("tier", "runner", "provider", "model", "effort", "timeout_minutes", "tries", "tools", "mcp", "instructions",
             "session", "modalities", "min_context", "structured_output", "data_handling", "floor", "budget_usd",
             "directory_cleanup")
STEP_COUNTS = ("timeout_minutes", "tries", "min_context")
STEP_AMOUNTS = ("budget_usd",)
STEP_LISTS = ("tools", "mcp", "instructions", "modalities", "data_handling")
CAPABILITY_KEYS = ("modalities", "context", "structured_output", "data_handling")
INSTRUCTIONS = ("user", "repo")
SESSIONS = ("resume", "fresh")
MCP_KEYS = ("command", "args", "url")
MCP_NAME = re.compile(r"[A-Za-z0-9_-]+")
TIER_KEYS = ("runner", "provider", "model")
PAYMENT_MODES = ("subscription", "api_key", "local")
DOCUMENT_KINDS = ("message", "ticket", "pr", "doc")
DOCUMENT_KEYS = ("enabled", "voice", "style", "words")
CAPTURE_KEYS = ("vision", "evidence_branch", "public_evidence")
EVIDENCE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
DOCUMENT_WORDS = {"message": (20, 60), "ticket": (80, 140)}
WORD_KINDS = ("message", "ticket", "pr")
KINDS = ("feat", "fix")
TRACKERS = ("github", "jira", "local")
REPLAY_KEYS = ("all", "dir")
ASK_KEYS = ("max_per_run", "settle", "expire_days", "remind_hours", "push_contact")
MAX_ASK_EXPIRE_DAYS = 3650
RETENTION_KEYS = ("days", "max_gb", "lines_days", "db_max_mb")
BACKUP_KEYS = ("copy_to",)
CHAT_KEYS = ("context_tokens", "summary_model", "max_sessions", "budget_usd")
FACTORY_PATHS = ("orchestrated", "classic", "adw")


@dataclass(frozen=True)
class Orchestration:
    max_decisions: int = 64
    max_actions: int = 128
    max_workers: int = 3
    progress_seconds: int = 30


def orchestration_table(data) -> Orchestration:
    defaults = Orchestration()
    if not isinstance(data, dict) or set(data) - defaults.__dataclass_fields__.keys():
        raise ValueError("[orchestration] contains unknown settings")
    values = {name: data.get(name, getattr(defaults, name)) for name in defaults.__dataclass_fields__}
    if any(type(value) is not int or not 0 < value <= 10000 for value in values.values()):
        raise ValueError("[orchestration] limits must be integers between 1 and 10000")
    return Orchestration(**values)
ENV_NAME = r"\*|[A-Za-z_][A-Za-z0-9_]*\*?"
ENV_KEYS = ("pass", "providers")


class UnknownRepo(refusals.Refusal, KeyError):
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


@dataclass(frozen=True)
class TriageConfig:
    auto: bool = False
    route_fixes: bool = False
    auto_per_refresh: int = 50


@dataclass
class GithubApp:
    app_id: str = ""
    installation_id: str = ""
    private_key: Path | None = None

    def missing(self) -> list[str]:
        return [name for name in ("app_id", "installation_id", "private_key") if not getattr(self, name)]


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
class AskConfig:
    max_per_run: int = 3
    settle: bool = True
    expire_days: float = 7
    remind_hours: float = 24
    push_contact: str = ""


@dataclass(frozen=True)
class Retention:
    days: float = 30
    max_gb: float = 20
    lines_days: float = 90
    db_max_mb: float = 500


@dataclass(frozen=True)
class BackupConfig:
    copy_to: str = ""


@dataclass(frozen=True)
class ChatConfig:
    context_tokens: int = 40_000
    summary_model: str = "haiku"
    max_sessions: int = 2
    budget_usd: float = 10.0


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


@dataclass(frozen=True)
class OncallSource:
    path: str
    pattern: str = ""
    repo: str = ""
    check: str = ""
    vision: str = ""
    scope: str = ""


@dataclass(frozen=True)
class OncallConfig:
    enabled: bool = False
    sources: tuple[OncallSource, ...] = ()
    vision: str = ""
    scope: str = ""
    confidence: float | None = None
    injection: float | None = None
    max_repairs: int = 1
    wakes_per_hour: int = 10
    max_attempts: int = 2
    incident_budget_usd: float = 5.0
    daily_budget_usd: float = 10.0
    cooldown_minutes: float = 30.0
    keep_days: float = 30.0


@dataclass(frozen=True)
class PaidRoutes:
    providers: tuple[str, ...] | None = None
    max_input: float | None = None
    max_output: float | None = None

    @property
    def limited(self) -> bool:
        return self.providers is not None or self.max_input is not None or self.max_output is not None

    @property
    def capped(self) -> bool:
        return self.max_input is not None or self.max_output is not None


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
    triage: TriageConfig = field(default_factory=TriageConfig)
    jev: JevConfig = field(default_factory=JevConfig)
    github_app: GithubApp = field(default_factory=GithubApp)
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    environment: Environment = field(default_factory=Environment)
    miner_lookback_hours: float = 24
    miner_max_digest_chars: int = 90_000
    max_parallel: int = 1
    max_parallel_per_repo: int = 1
    max_parallel_checks: int = 2
    dashboard_port: int = paths.DASHBOARD_PORT
    server_url: str = ""
    dashboard_owner: str = ""
    dashboard_https: bool = False
    loop_dirs: list[Path] = field(default_factory=lambda: [paths.CONFIG_DIR / "loops"])
    queues: dict[str, int] = field(default_factory=dict)
    pricing: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    local_providers: list[str] = field(default_factory=lambda: ["ollama"])
    daily_budget_usd: float | None = None
    session_idle_hours: float = 2
    tiers: dict[str, dict[str, tuple[dict[str, str], ...]]] = field(default_factory=dict)
    tier_map: str = ""
    routing_fallback: bool = True
    capabilities: dict[str, dict] = field(default_factory=dict)
    floors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    payment_modes: tuple[str, ...] = PAYMENT_MODES
    paid: PaidRoutes = PaidRoutes()
    adws: Path | None = None
    steps: dict[str, dict] = field(default_factory=dict)
    kinds: dict[str, dict[str, dict]] = field(default_factory=dict)
    mcp: dict[str, dict] = field(default_factory=dict)
    research_planner: bool = False
    research_search: bool = False
    research_synthesis: bool = False
    research_audit: bool = False
    research_search_workers: int = 3
    research_search_share: float = 0.6
    ask: AskConfig = field(default_factory=AskConfig)
    retention: Retention = field(default_factory=Retention)
    backup: BackupConfig = field(default_factory=BackupConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    document: dict = field(default_factory=dict)
    capture_vision: str = ""
    capture_evidence_branch: str = "feedback-evidence"
    capture_public_evidence: bool = False
    oncall: OncallConfig = field(default_factory=OncallConfig)
    ab: dict[str, dict] = field(default_factory=dict)
    ignored: tuple[str, ...] = ()

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


def remove_controller_only_settings(data: dict) -> None:
    for name in ("max_parallel", "max_parallel_per_repo"):
        data.pop(name, None)
    if "jev" in data:
        for name in ("risk_hold", "irreversible_hold", "secrets_hold"):
            data["jev"].pop(name, None)
        if not data["jev"]:
            del data["jev"]


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


def known(table: dict, allowed, where: str, ignored: list[str] | None, noun: str = "key") -> dict:
    if bad := sorted(set(table) - set(allowed)):
        if ignored is None:
            raise ValueError(f"[{where}]: unknown {noun} {', '.join(bad)}; use {', '.join(allowed)}")
        ignored.extend(f"[{where}] {name}" for name in bad)
    return {name: value for name, value in table.items() if name in allowed}


def step_table(table, where: str) -> dict:
    table = table_of(table, where)
    if bad := sorted(set(table) - set(STEP_KEYS)):
        raise ValueError(f"[{where}]: unknown key {', '.join(bad)}; use {', '.join(STEP_KEYS)}")
    for key in STEP_COUNTS:
        if key in table and (type(table[key]) is not int or table[key] < 1):
            raise ValueError(f"[{where}]: {key} must be a whole number of at least 1, not {table[key]!r}")
    for key in STEP_AMOUNTS:
        if key in table and (type(table[key]) not in (int, float) or not table[key] > 0):
            raise ValueError(f"[{where}]: {key} must be a number of dollars above 0, not {table[key]!r}")
    for key in STEP_LISTS:
        if key in table and not (isinstance(table[key], list | tuple) and all(isinstance(v, str) for v in table[key])):
            raise ValueError(f"[{where}]: {key} must be a list of names, not {table[key]!r}")
    if bad := [v for v in table.get("instructions", ()) if v not in INSTRUCTIONS]:
        raise ValueError(f"[{where}]: instructions {', '.join(map(repr, bad))} is not one of {', '.join(INSTRUCTIONS)}")
    if "session" in table and table["session"] not in SESSIONS:
        raise ValueError(f"[{where}]: session {table['session']!r} is not one of {', '.join(SESSIONS)}")
    if "structured_output" in table and type(table["structured_output"]) is not bool:
        raise ValueError(f"[{where}]: structured_output must be true or false, not {table['structured_output']!r}")
    if "directory_cleanup" in table and type(table["directory_cleanup"]) is not bool:
        raise ValueError(f"[{where}]: directory_cleanup must be true or false, not {table['directory_cleanup']!r}")
    return {k: tuple(dict.fromkeys(v)) if k in STEP_LISTS else float(v) if k in STEP_AMOUNTS
            else v if k in STEP_COUNTS or type(v) is bool else str(v)
            for k, v in table.items()}


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


def step_tables(data, where: str, ignored: list[str] | None = None) -> dict[str, dict]:
    data = known(table_of(data, where), STEP_NAMES, where, ignored, "step")
    for alias, role in STEP_ALIASES.items():
        if alias in data and role in data:
            raise ValueError(f"[{where}.{alias}] and [{where}.{role}] are the same step; name the step once")
    return {STEP_ALIASES.get(name, name): step_table(table, f"{where}.{name}") for name, table in data.items()}


def ab_tables(data, where: str = "ab") -> dict[str, dict]:
    found = {}
    for name, table in table_of(data, where).items():
        label = f"[{where}.{name}]"
        table = table_of(table, f"{where}.{name}")
        if bad := sorted(set(table) - set(AB_KEYS)):
            raise ValueError(f"{label}: unknown key {', '.join(bad)}; use {', '.join(AB_KEYS)}")
        step = table.get("step")
        if step not in STEP_NAMES:
            raise ValueError(f"{label}: unknown step {step!r}; use {', '.join(STEP_NAMES)}")
        kind = table.get("kind", "")
        if kind and kind not in KINDS:
            raise ValueError(f"{label}: unknown kind {kind!r}; use {', '.join(KINDS)}")
        share = table.get("share")
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share <= 0.5:
            raise ValueError(f"{label}: share must be a number above 0 and at most 0.5, not {share!r}")
        arm = table_of(table.get("arm"), f"{where}.{name}.arm") if "arm" in table else {}
        if not arm:
            raise ValueError(f"{label}: arm must set at least one of {', '.join(ARM_KEYS)}")
        if bad := sorted(set(arm) - set(ARM_KEYS)):
            raise ValueError(f"[{where}.{name}.arm]: unknown key {', '.join(bad)}; use {', '.join(ARM_KEYS)}")
        found[name] = {"step": STEP_ALIASES.get(step, step), "kind": kind, "share": float(share),
                       "arm": step_table(arm, f"{where}.{name}.arm")}
    ab_clash(found)
    return found


def ab_clash(tests: dict) -> None:
    names = list(tests)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            one, two = tests[a], tests[b]
            if one["step"] == two["step"] and (one["kind"] == two["kind"] or not one["kind"] or not two["kind"]):
                kind = one["kind"] or two["kind"]
                raise ValueError(f"[ab.{a}] and [ab.{b}] both change step {one['step']}"
                                 + (f" for {kind} tickets" if kind else "") + "; keep one A/B per step and kind")


def kind_tables(data, where: str, ignored: list[str] | None = None) -> dict[str, dict[str, dict]]:
    data = known(table_of(data, where), KINDS, where, ignored, "kind")
    kinds = {}
    for kind, table in data.items():
        table = known(table_of(table, f"{where}.{kind}"), ("steps",), f"{where}.{kind}", ignored)
        kinds[kind] = step_tables(table.get("steps") or {}, f"{where}.{kind}.steps", ignored)
    return kinds


def tier_maps(data, where: str) -> dict[str, dict[str, tuple[dict[str, str], ...]]]:
    from . import routing

    maps: dict[str, dict[str, tuple[dict[str, str], ...]]] = {}
    for name, tiers in table_of(data, where).items():
        maps[name] = {}
        for tier, found in table_of(tiers, f"{where}.{name}").items():
            listed = isinstance(found, list)
            if listed and not found:
                raise ValueError(f"[{where}.{name}] {tier}: needs at least one table")
            entries = []
            for n, entry in enumerate(found if listed else [found], 1):
                label = f"[{where}.{name}] {tier}" + (f" entry {n}" if listed else "")
                if not isinstance(entry, dict):
                    raise TypeError(f"{label}: must be a table, not {entry!r}")
                if bad := sorted(set(entry) - set(TIER_KEYS)):
                    raise ValueError(f"{label}: unknown key {', '.join(bad)}; use {', '.join(TIER_KEYS)}")
                entry = {k: str(v) for k, v in entry.items()}
                if problem := routing.entry_problem(entry):
                    raise ValueError(f"{label}: {problem}")
                entries.append(entry)
            maps[name][tier] = tuple(entries)
    return maps


@dataclass(frozen=True)
class Routing:
    fallback: bool = True
    capabilities: dict[str, dict] = field(default_factory=dict)
    floors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    payment_modes: tuple[str, ...] | None = None
    paid: PaidRoutes = PaidRoutes()


def capability_table(key: str, table) -> dict:
    where = f"[routing.capabilities.{key}]"
    table = table_of(table, f"routing.capabilities.{key}")
    if bad := sorted(set(table) - set(CAPABILITY_KEYS)):
        raise ValueError(f"{where}: unknown key {', '.join(bad)}; use {', '.join(CAPABILITY_KEYS)}")
    found = table.get("modalities", [])
    if not (isinstance(found, list) and all(isinstance(v, str) and v for v in found)):
        raise ValueError(f"{where}: modalities must be a list of names, not {found!r}")
    if "context" in table and (type(table["context"]) is not int or table["context"] < 1):
        raise ValueError(f"{where}: context must be a whole number of at least 1, not {table['context']!r}")
    if "structured_output" in table and type(table["structured_output"]) is not bool:
        raise ValueError(f"{where}: structured_output must be true or false, not {table['structured_output']!r}")
    if "data_handling" in table and not (isinstance(table["data_handling"], str) and table["data_handling"]):
        raise ValueError(f"{where}: data_handling must be one label, not {table['data_handling']!r}")
    return {**table, **({"modalities": tuple(dict.fromkeys(found))} if "modalities" in table else {})}


def paid_table(data) -> PaidRoutes:
    table = table_of(data, "routing.paid")
    if bad := sorted(set(table) - {"providers", "max_input", "max_output"}):
        raise ValueError(f"[routing.paid]: unknown key {', '.join(bad)}; use providers, max_input, max_output")
    providers = table.get("providers")
    if providers is not None:
        if not (isinstance(providers, list) and all(isinstance(p, str) and p for p in providers)):
            raise ValueError(f"[routing.paid] providers must be a list of provider names, not {providers!r}")
        providers = tuple(dict.fromkeys(providers))
    return PaidRoutes(providers, *(price_rate(table[key], f"[routing.paid] {key}") if key in table else None
                                   for key in ("max_input", "max_output")))


def tighter(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else min(a, b)


def allowed_paid_routes(cfg: Config, layer: RepoLayer | None = None) -> PaidRoutes:
    own = layer.paid if layer else PaidRoutes()
    providers = (own.providers if cfg.paid.providers is None else cfg.paid.providers if own.providers is None
                 else tuple(p for p in cfg.paid.providers if p in own.providers))
    return PaidRoutes(providers, tighter(cfg.paid.max_input, own.max_input),
                      tighter(cfg.paid.max_output, own.max_output))


def routing_table(data) -> Routing:
    table = table_of(data, "routing")
    if bad := sorted(set(table) - {"fallback", "payment_modes", "capabilities", "floors", "paid"}):
        raise ValueError(f"[routing]: unknown key {', '.join(bad)}; "
                         "use fallback, payment_modes, capabilities, floors, paid")
    value = table.get("fallback", True)
    if type(value) is not bool:
        raise ValueError(f"[routing] fallback must be true or false, not {value!r}")
    capabilities = {str(key): capability_table(str(key), entry)
                    for key, entry in table_of(table.get("capabilities", {}), "routing.capabilities").items()}
    floors = {}
    for name, models in table_of(table.get("floors", {}), "routing.floors").items():
        if not (isinstance(models, list) and models and all(isinstance(m, str) and m for m in models)):
            raise ValueError(f"[routing.floors] {name}: must be a nonempty list of model names, not {models!r}")
        floors[str(name)] = tuple(dict.fromkeys(models))
    modes = table.get("payment_modes")
    if modes is not None:
        if (not isinstance(modes, list) or not modes
                or not all(isinstance(m, str) and m in PAYMENT_MODES for m in modes)
                or len(set(modes)) != len(modes)):
            raise ValueError(f"[routing] payment_modes must be a nonempty list of distinct values from "
                             f"{', '.join(PAYMENT_MODES)}, not {modes!r}")
        modes = tuple(modes)
    return Routing(value, capabilities, floors, modes, paid_table(table.get("paid", {})))


def allowed_payment_modes(cfg: Config, layer: RepoLayer | None = None) -> tuple[str, ...]:
    if layer is None or layer.payment_modes is None:
        return cfg.payment_modes
    return tuple(m for m in cfg.payment_modes if m in layer.payment_modes)


def price_rate(value, where: str) -> float:
    found = util.nonnegative_number(value)
    if found is None:
        raise ValueError(f"{where}: price must be a finite nonnegative number")
    return found


def replay_table(data, ignored: list[str] | None = None) -> ReplayConfig:
    table = known(table_of(data, "replay"), REPLAY_KEYS, "replay", ignored)
    return ReplayConfig(all=bool(table.get("all", False)), dir=paths.expand(table["dir"]) if table.get("dir") else None)


def ask_table(data, ignored: list[str] | None = None) -> AskConfig:
    table = known(table_of(data, "ask"), ASK_KEYS, "ask", ignored)
    limit, settle = table.get("max_per_run", 3), table.get("settle", True)
    if type(limit) is not int or limit < 0:
        raise ValueError(f"[ask] max_per_run must be a whole number of at least 0, not {limit!r}")
    if type(settle) is not bool:
        raise ValueError(f"[ask] settle must be true or false, not {settle!r}")
    days = table.get("expire_days", 7)
    if type(days) not in (int, float) or not math.isfinite(days) or not 0 < days <= MAX_ASK_EXPIRE_DAYS:
        raise ValueError(f"[ask] expire_days must be a number above 0 and at most {MAX_ASK_EXPIRE_DAYS}, not {days!r}")
    hours = table.get("remind_hours", 24)
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 0 < hours <= MAX_ASK_EXPIRE_DAYS * 24:
        raise ValueError(f"[ask] remind_hours must be a number above 0 and at most {MAX_ASK_EXPIRE_DAYS * 24}, not {hours!r}")
    contact = table.get("push_contact", "")
    if (not isinstance(contact, str) or len(contact) > 200 or re.search(r"\s", contact)
            or contact and not contact.startswith(("mailto:", "https://"))):
        raise ValueError("[ask] push_contact must be empty or a mailto: or https:// address without spaces, at most 200 characters")
    return AskConfig(max_per_run=limit, settle=settle, expire_days=days, remind_hours=hours, push_contact=contact)


def retention_table(data, ignored: list[str] | None = None) -> Retention:
    table = known(table_of(data, "retention"), RETENTION_KEYS, "retention", ignored)
    found = {}
    for key, default in (("days", Retention.days), ("max_gb", Retention.max_gb), ("lines_days", Retention.lines_days),
                        ("db_max_mb", Retention.db_max_mb)):
        value = table.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"[retention] {key} must be a number above 0, not {value!r}")
        found[key] = value
    if found["lines_days"] < found["days"]:
        raise ValueError("[retention] lines_days must be at least days")
    return Retention(**found)


def backup_table(data) -> BackupConfig:
    table = table_of(data, "backup")
    if bad := sorted(set(table) - set(BACKUP_KEYS)):
        raise ValueError(f"[backup]: unknown key {', '.join(bad)}; use {', '.join(BACKUP_KEYS)}")
    copy_to = table.get("copy_to", BackupConfig.copy_to)
    if not isinstance(copy_to, str) or copy_to != copy_to.strip() or copy_to.startswith("-"):
        raise ValueError(f"[backup] copy_to must be an rsync destination without surrounding spaces, not {copy_to!r}")
    return BackupConfig(copy_to=copy_to)


def chat_table(data, ignored: list[str] | None = None) -> ChatConfig:
    table = known(table_of(data, "chat"), CHAT_KEYS, "chat", ignored)
    tokens = table.get("context_tokens", ChatConfig.context_tokens)
    if type(tokens) is not int or tokens < 100:
        raise ValueError(f"[chat] context_tokens must be a whole number of at least 100, not {tokens!r}")
    model = table.get("summary_model", ChatConfig.summary_model)
    if type(model) is not str or not model.strip():
        raise ValueError(f"[chat] summary_model must be a model name, not {model!r}")
    most = table.get("max_sessions", ChatConfig.max_sessions)
    if type(most) is not int or most < 1:
        raise ValueError(f"[chat] max_sessions must be a whole number of at least 1, not {most!r}")
    budget = table.get("budget_usd", ChatConfig.budget_usd)
    if isinstance(budget, bool) or not isinstance(budget, int | float) or not math.isfinite(budget) or budget <= 0:
        raise ValueError(f"[chat] budget_usd must be a number of dollars above 0, not {budget!r}")
    return ChatConfig(context_tokens=tokens, summary_model=model.strip(), max_sessions=most, budget_usd=float(budget))


def research_table(data, ignored: list[str] | None = None) -> dict:
    keys = ("planner", "search", "synthesis", "audit", "search_workers", "search_share")
    table = known(table_of(data, "research"), keys, "research", ignored)
    planner = table.get("planner", False)
    if type(planner) is not bool:
        raise ValueError(f"[research] planner must be true or false, not {planner!r}")
    search = table.get("search", False)
    if type(search) is not bool:
        raise ValueError(f"[research] search must be true or false, not {search!r}")
    if search and not planner:
        raise ValueError("[research] search needs planner = true, because the planner sets the search tasks")
    synthesis = table.get("synthesis", False)
    if type(synthesis) is not bool:
        raise ValueError(f"[research] synthesis must be true or false, not {synthesis!r}")
    if synthesis and not search:
        raise ValueError("[research] synthesis needs search = true, because the collator writes from the search "
                         "workers' evidence")
    audit = table.get("audit", False)
    if type(audit) is not bool:
        raise ValueError(f"[research] audit must be true or false, not {audit!r}")
    if audit and not synthesis:
        raise ValueError("[research] audit needs synthesis = true, because the auditor checks the collator's claims")
    workers = table.get("search_workers", Config.research_search_workers)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError(f"[research] search_workers must be a whole number from 1 to 8, not {workers!r}")
    share = table.get("search_share", Config.research_search_share)
    if type(share) not in (int, float) or not 0 < share < 1:
        raise ValueError(f"[research] search_share must be a number above 0 and under 1, not {share!r}")
    return {"planner": planner, "search": search, "synthesis": synthesis, "audit": audit,
            "search_workers": workers, "search_share": float(share)}


def oncall_source(entry, repos: list[Repo]) -> OncallSource:
    table = table_of(entry, "oncall.source")
    keys = ("path", "pattern", "repo", "check", "vision", "scope")
    if bad := sorted(set(table) - set(keys)):
        raise ValueError(f"[[oncall.source]]: unknown key {', '.join(bad)}; use {', '.join(keys)}")
    for key in keys:
        if not isinstance(table.get(key, ""), str):
            raise TypeError(f"[[oncall.source]] {key} must be a string, not {table[key]!r}")
    if not table.get("path", "").strip():
        raise ValueError("[[oncall.source]] needs a path")
    if table.get("pattern"):
        try:
            re.compile(table["pattern"])
        except re.error as e:
            raise ValueError(f"[[oncall.source]] pattern {table['pattern']!r} is not a regex: {e}") from e
    if table.get("repo") and table["repo"] not in {r.name for r in repos}:
        raise ValueError(f"[[oncall.source]] repo {table['repo']!r} is not a [[repo]] in the config")
    return OncallSource(**{key: table.get(key, "") for key in keys})


ONCALL_BOUNDS = {"max_repairs": (int, 0), "wakes_per_hour": (int, 0), "max_attempts": (int, 1),
                 "incident_budget_usd": (float, 0), "daily_budget_usd": (float, 0), "cooldown_minutes": (float, 0),
                 "keep_days": (float, 0)}


def oncall_bounds(table: dict) -> dict:
    found = {}
    for key, (kind, least) in ONCALL_BOUNDS.items():
        if key not in table:
            continue
        value = table[key]
        spend = key.endswith("_usd")
        whole = type(value) is int
        if not (whole if kind is int else whole or type(value) is float) or not (value > least if spend else value >= least):
            what = "a whole number" if kind is int else "a number"
            bound = f"above {least}" if spend else f"{least} or more"
            raise ValueError(f"[oncall] {key} must be {what} {bound}, not {value!r}")
        found[key] = kind(value)
    return found


def oncall_table(data, repos: list[Repo]) -> OncallConfig:
    table = table_of(data, "oncall")
    allowed = ("enabled", "source", "vision", "scope", "confidence", "injection", *ONCALL_BOUNDS)
    if bad := sorted(set(table) - set(allowed)):
        raise ValueError(f"[oncall]: unknown key {', '.join(bad)}; use {', '.join(allowed)}")
    enabled = table.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError(f"[oncall] enabled must be true or false, not {enabled!r}")
    entries = table.get("source", [])
    if not isinstance(entries, list):
        raise TypeError(f"[oncall] source must be a list of [[oncall.source]] tables, not {entries!r}")
    for key in ("vision", "scope"):
        if not isinstance(table.get(key, ""), str):
            raise TypeError(f"[oncall] {key} must be a string, not {table[key]!r}")
    for key in ("confidence", "injection"):
        value = table.get(key)
        if value is not None and (type(value) not in (int, float) or not 0 <= value <= 1):
            raise ValueError(f"[oncall] {key} must be a number from 0 to 1, not {value!r}")
    return OncallConfig(enabled, tuple(oncall_source(entry, repos) for entry in entries),
                        table.get("vision", ""), table.get("scope", ""),
                        None if table.get("confidence") is None else float(table["confidence"]),
                        None if table.get("injection") is None else float(table["injection"]), **oncall_bounds(table))


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


def voice_file(entry, folder: Path) -> str:
    if not isinstance(entry, str):
        raise TypeError(f"[document] voice: must be a list of file names, not {entry!r}")
    named = Path(entry)
    found = (folder / named).resolve()
    if (named.is_absolute() or ".." in named.parts or not found.is_relative_to(folder.resolve())
            or not found.is_file()):
        raise ValueError(f"[document] voice: {entry!r} is not a file in {paths.short_path(folder)}")
    return str(found)


def capture_table(data, folder: Path) -> tuple[str, str, bool]:
    table = table_of(data, "capture")
    if bad := sorted(set(table) - set(CAPTURE_KEYS)):
        raise ValueError(f"[capture]: unknown key {', '.join(bad)}; use {', '.join(CAPTURE_KEYS)}")
    branch = table.get("evidence_branch", "feedback-evidence")
    if (not isinstance(branch, str) or not EVIDENCE_BRANCH.fullmatch(branch) or ".." in branch
            or branch.endswith(("/", ".", ".lock"))):
        raise ValueError(f"[capture] evidence_branch must be a branch name such as feedback-evidence, not {branch!r}")
    public = table.get("public_evidence", False)
    if type(public) is not bool:
        raise TypeError(f"[capture] public_evidence must be true or false, not {public!r}")
    return capture_vision_file(table, folder), branch, public


def capture_vision_file(table: dict, folder: Path) -> str:
    named = table.get("vision", "")
    if not isinstance(named, str):
        raise TypeError(f"[capture] vision must be a file name, not {named!r}")
    if not named.strip():
        return ""
    entry = Path(named)
    found = (folder / entry).resolve()
    if entry.is_absolute() or ".." in entry.parts or not found.is_relative_to(folder.resolve()):
        raise ValueError(f"[capture] vision: {named!r} is not a file in {paths.short_path(folder)}")
    return str(found)


def word_range(kind: str, value) -> tuple[int, int]:
    whole = isinstance(value, list) and len(value) == 2 and all(type(n) is int for n in value)
    if kind not in WORD_KINDS or not whole or not 1 <= value[0] <= value[1]:
        raise ValueError(f"[document] words: {kind} must be two whole numbers, low then high, from 1 up, "
                         f"for {', '.join(WORD_KINDS)}; got {value!r}")
    return value[0], value[1]


def triage_table(data, ignored: list[str] | None = None) -> TriageConfig:
    defaults = TriageConfig()
    table = known(table_of(data, "triage"), defaults.__dict__, "triage", ignored)
    for name in ("auto", "route_fixes"):
        if type(table.get(name, False)) is not bool:
            raise ValueError(f"[triage] {name} must be true or false")
    limit = table.get("auto_per_refresh", defaults.auto_per_refresh)
    if type(limit) is not int or not 0 <= limit <= 1000:
        raise ValueError("[triage] auto_per_refresh must be a whole number from 0 to 1000")
    return TriageConfig(**table)


def jev_names(jv: dict, name: str, noun: str, ignored: list[str] | None) -> dict:
    return known(jv.get(name) or {}, JevConfig().__dict__[name], f"jev.{name}", ignored, noun)


def document_table(data, folder: Path, ignored: list[str] | None = None) -> dict:
    table = known(table_of(data, "document"), DOCUMENT_KEYS, "document", ignored)
    found = {}
    if "enabled" in table:
        if type(table["enabled"]) is not bool:
            raise ValueError(f"[document] enabled must be true or false, not {table['enabled']!r}")
        found["enabled"] = table["enabled"]
    if "style" in table:
        if not isinstance(table["style"], str):
            raise ValueError(f"[document] style must be a string, not {table['style']!r}")
        found["style"] = table["style"].strip()
    if "voice" in table:
        if not isinstance(table["voice"], list):
            raise ValueError(f"[document] voice must be a list of file names, not {table['voice']!r}")
        found["voice"] = tuple(dict.fromkeys(voice_file(entry, folder) for entry in table["voice"]))
    if "words" in table:
        words = table_of(table["words"], "document.words")
        found["words"] = {kind: word_range(kind, value) for kind, value in words.items()}
    return found


@dataclass(frozen=True)
class Document:
    enabled: bool = False
    voice: tuple[str, ...] = ()
    style: str = ""
    words: dict[str, tuple[int, int]] = field(default_factory=lambda: dict(DOCUMENT_WORDS))


def document_for(cfg: Config, repo: Repo | None = None, layer: RepoLayer | None = None) -> Document:
    own = dict(layer.document) if layer else {}
    if not (repo and repo.layer and not repo.layer.resolve().is_relative_to(repo.path.resolve())):
        own.pop("voice", None)
    found = {**cfg.document, **own}
    return Document(found.get("enabled", False), tuple(found.get("voice", ())), found.get("style", ""),
                    {**DOCUMENT_WORDS, **cfg.document.get("words", {}), **own.get("words", {})})


def dashboard_https(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"[dashboard] https must be true or false, not {value!r}")
    return value


def read_data(path: Path | None = None) -> dict:
    return tomllib.loads(paths.read_confined(path or paths.CONFIG_FILE).decode())


def discovery_port(data: dict | None = None) -> int:
    try:
        data = read_data() if data is None else data
        dashboard = data.get("dashboard", {})
        return int(dashboard.get("port", paths.DASHBOARD_PORT))
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return paths.DASHBOARD_PORT


def load_or_problem(path: Path | None = None) -> tuple[Config, str]:
    try:
        return load(path), ""
    except Exception as e:
        problem = f"{type(e).__name__}: {e}"
    fallback = Config()
    try:
        data = read_data(path)
        dashboard = data.get("dashboard", {})
        dashboard = dashboard if isinstance(dashboard, dict) else {}
    except Exception:
        return fallback, problem
    fallback.dashboard_port = discovery_port(data)
    with contextlib.suppress(Exception):
        fallback.dashboard_https = dashboard_https(dashboard.get("https", False))
    if isinstance(dashboard.get("owner", ""), str):
        fallback.dashboard_owner = dashboard.get("owner", "")
    with contextlib.suppress(TypeError):
        fallback.server_url = server_url(data)
    return fallback, problem


def server_url(data: dict) -> str:
    server = data.get("server", {})
    if not isinstance(server, dict) or not isinstance(server.get("url", ""), str):
        raise TypeError("[server] url must be a URL string")
    return server.get("url", "").strip()


def load_required() -> Config:
    return load(required=True)


def load(path: Path | None = None, strict: bool = False, *, required: bool = False) -> Config:
    path = path or paths.CONFIG_FILE
    ignored: list[str] | None = None if strict else []
    try:
        data = read_data(path)
    except FileNotFoundError:
        if required:
            raise
        return Config()
    cfg = Config(
        alert_repo=data.get("alert_repo", ""),
        global_repo=data.get("global_repo", "ai"),
        arete_repo=data.get("arete_repo", "arete"),
        models=dict(data.get("models", {})),
        miner_lookback_hours=float(data.get("miner", {}).get("lookback_hours", 24)),
        miner_max_digest_chars=int(data.get("miner", {}).get("max_digest_chars", 90_000)),
        max_parallel=max(1, int(data.get("max_parallel", 1))),
        max_parallel_per_repo=max(1, int(data.get("max_parallel_per_repo", 1))),
        max_parallel_checks=max(1, int(data.get("max_parallel_checks", 2))),
        dashboard_port=int(data.get("dashboard", {}).get("port", paths.DASHBOARD_PORT)),
        server_url=server_url(data),
        dashboard_owner=data.get("dashboard", {}).get("owner", ""),
        dashboard_https=dashboard_https(data.get("dashboard", {}).get("https", False)),
    )
    cfg.tiers = tier_maps(data.get("tiers") or {}, "tiers")
    cfg.tier_map = str((data.get("factory") or {}).get("tier_map", ""))
    if (adws := (data.get("factory") or {}).get("adws")) is not None:
        if not isinstance(adws, str):
            raise TypeError(f"[factory] adws must be a folder path, not {adws!r}")
        cfg.adws = path.parent / paths.expand(adws)
    cfg.steps = step_tables(data.get("steps") or {}, "steps", ignored)
    cfg.kinds = kind_tables(data.get("kinds") or {}, "kinds", ignored)
    serve = data.get("serve", {})
    if "loop_dirs" in serve:
        cfg.loop_dirs = [paths.expand(d) for d in serve["loop_dirs"]]
    cfg.queues = {str(k): max(1, int(v)) for k, v in (serve.get("queues") or {}).items()}
    cfg.daily_budget_usd = float(serve["daily_budget_usd"]) if serve.get("daily_budget_usd") else None
    cfg.session_idle_hours = float(serve.get("session_idle_hours", cfg.session_idle_hours))
    pricing = data.get("pricing", {})
    cfg.local_providers = list(pricing.get("local_providers", cfg.local_providers))
    cfg.pricing = {str(m): tuple(price_rate(r[k], f"[pricing.models.{m}] {k}") if k in r else None
                                for k in ("input", "output"))
                   for m, r in (pricing.get("models") or {}).items()}
    found = routing_table(data.get("routing", {}))
    cfg.routing_fallback, cfg.capabilities, cfg.floors = found.fallback, found.capabilities, found.floors
    cfg.payment_modes = found.payment_modes or PAYMENT_MODES
    cfg.paid = found.paid
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
                      **{str(k): float(v) for k, v in jev_names(jv, "screen_lines", "question", ignored).items()}},
        proof_hold=float(jv.get("proof_hold", JevConfig.proof_hold)),
        fixed_skip=float(jv.get("fixed_skip", JevConfig.fixed_skip)),
        research_rerun=float(jv.get("research_rerun", JevConfig.research_rerun)),
        research_answered=float(jv.get("research_answered", JevConfig.research_answered)),
        feedback_skip=float(jv.get("feedback_skip", JevConfig.feedback_skip)),
        risk_weights={**JevConfig().risk_weights,
                      **{str(k): float(v) for k, v in jev_names(jv, "risk_weights", "weight", ignored).items()}},
        guard=bool(jv.get("guard", True)),
        compact_tokens=int(jv.get("compact_tokens", JevConfig.compact_tokens)),
    )
    app = section(data, "github_app")
    cfg.github_app = GithubApp(
        app_id=str(app.get("app_id", "")).strip(),
        installation_id=str(app.get("installation_id", "")).strip(),
        private_key=paths.expand(app["private_key"]) if app.get("private_key") else None,
    )
    if cfg.github_app.installation_id and not cfg.github_app.installation_id.isdigit():
        raise ValueError("[github_app] installation_id must be digits only")
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
    cfg.oncall = oncall_table(data.get("oncall") or {}, cfg.repos)
    cfg.replay = replay_table(data.get("replay") or {}, ignored)
    cfg.mcp = mcp_tables(data.get("mcp") or {})
    research = research_table(data.get("research") or {}, ignored)
    cfg.research_planner = research["planner"]
    cfg.research_search = research["search"]
    cfg.research_synthesis = research["synthesis"]
    cfg.research_audit = research["audit"]
    cfg.research_search_workers = research["search_workers"]
    cfg.research_search_share = research["search_share"]
    cfg.ask = ask_table(data.get("ask") or {}, ignored)
    cfg.triage = triage_table(data.get("triage", {}), ignored)
    cfg.retention = retention_table(data.get("retention") or {}, ignored)
    cfg.backup = backup_table(data.get("backup") or {})
    cfg.chat = chat_table(data.get("chat") or {}, ignored)
    cfg.environment = environment_table(data.get("environment") or {})
    cfg.document = document_table(data.get("document") or {}, path.parent, ignored)
    cfg.capture_vision, cfg.capture_evidence_branch, cfg.capture_public_evidence = capture_table(
        data.get("capture") or {}, path.parent)
    cfg.ab = ab_tables(data.get("ab") or {})
    from . import steps
    steps.check(cfg, defer_tiers=True)
    cfg.ignored = tuple(ignored or ())
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


@dataclass(frozen=True)
class PatchPolicy:
    labels: tuple[str, ...] = ("patch",)
    max_lines: int = 200
    max_files: int = 5
    review_tries: int = 2
    sensitive: tuple[str, ...] = ("*auth*", "*secret*", "*credential*", "*password*", "*token*", "*login*", "*session*",
                                  "*jwt*", "*oauth*", "*privilege*", "*migration*", "*backup*", "*restore*",
                                  "*payment*", "*billing*", "*guard*", "*sandbox*", "*scratch*", "*permission*",
                                  "*security*", "*routing*", "*config*", "*runlimit*", "*principal*",
                                  "*outbox*", "*store/*", "*database*", "*storage*", "*persist*", "*schema*",
                                  "*transaction*", "*/db.*", "db.*", "*diffguard*", "*.sql", "*lock*", "*pyproject.toml",
                                  "*package.json", "*requirements*", "*dockerfile*", "*terraform*")


@dataclass
class Verify:
    start: str = ""
    url: str = "http://localhost:{port}"
    ready: str = "/"
    ready_seconds: int = 180
    allow_bash: list[str] = field(default_factory=list)
    notes: str = ""
    publish_evidence: bool = True
    preflight: dict[str, str] = field(default_factory=dict)
    preflight_seconds: float = 30
    probes: dict[str, str] = field(default_factory=dict)
    probe_seconds: float = 120
    allow_patch_gaps: bool = False
    feature_map: dict[str, journeys.Journey] = field(default_factory=dict)
    drivers: dict[str, str] = field(default_factory=dict)

    @property
    def bash(self) -> list[str]:
        return ["curl *", "echo *", *self.allow_bash]

    def policy(self) -> dict:
        values = asdict(self)
        for name in ("feature_map", "drivers"):
            if not values[name]:
                del values[name]
        return values


@dataclass
class RepoLayer:
    dir: Path
    setup: list[str] = field(default_factory=list)
    copy: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    quick_checks: tuple[str, ...] = ("lint", "format", "compile", "imports", "web", "types")
    targeted_checks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    targeted_commands: dict[str, str] = field(default_factory=dict)
    patch: PatchPolicy = field(default_factory=PatchPolicy)
    max_diff_lines: int = 400
    deny: list[str] = field(default_factory=list)
    max_attempts: int = 3
    allow_bash: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_BASH))
    trust_level: Literal["pr", "automerge"] = "pr"
    automerge_after: int = 5
    base_branch: str = ""
    required_check: dict[str, str | int] | None = None
    subject: str = "{kind}: {title}"
    branch: str = "arete/{id}-{slug}"
    timeout_minutes: int = 45
    max_repair_cycles: int = 2
    max_run_minutes: int = 60
    max_verify_minutes: int = 20
    parallel_checks: bool = False
    verify: Verify | None = None
    research_dir: str = ""
    tiers: dict[str, dict[str, tuple[dict[str, str], ...]]] = field(default_factory=dict)
    routing_fallback: bool = True
    capabilities: dict[str, dict] = field(default_factory=dict)
    floors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    payment_modes: tuple[str, ...] | None = None
    paid: PaidRoutes = PaidRoutes()
    steps: dict[str, dict] = field(default_factory=dict)
    kinds: dict[str, dict[str, dict]] = field(default_factory=dict)
    factory_path: str = FACTORY_PATHS[0]
    orchestration: Orchestration = field(default_factory=Orchestration)
    tier_map: str = ""
    passes: tuple[str, ...] = ()
    document: dict = field(default_factory=dict)
    ab: dict[str, dict] = field(default_factory=dict)
    ignored: tuple[str, ...] = ()
    read_roots: tuple[str, ...] = ()

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


LAYER_SECTIONS = {"setup", "checks", "targeted_commands", "check_policy", "limits", "agent", "trust", "git", "verify", "research", "steps", "kinds", "tiers",
                  "factory", "environment", "routing", "document", "ab", "orchestration"}


def string_list(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"[{where}] must be a list of nonempty strings")
    return tuple(dict.fromkeys(value))


def check_policy(layer: RepoLayer, table: dict) -> None:
    if unknown := set(table) - {"quick", "targeted"}:
        raise ValueError(f"[check_policy] unknown keys: {', '.join(sorted(unknown))}")
    if "quick" in table:
        layer.quick_checks = string_list(table["quick"], "check_policy.quick")
        if missing := set(layer.quick_checks) - layer.checks.keys():
            raise ValueError(f"[check_policy.quick] unknown checks: {', '.join(sorted(missing))}")
    for pattern, names in table_of(table.get("targeted", {}), "check_policy.targeted").items():
        selected = string_list(names, f"check_policy.targeted.{pattern}")
        if not pattern.strip() or not selected or (set(selected) - (layer.checks.keys() | layer.targeted_commands.keys())):
            raise ValueError(f"[check_policy.targeted] {pattern!r} must name existing checks")
        layer.targeted_checks[pattern] = selected


def patch_policy(table: dict) -> PatchPolicy:
    defaults = PatchPolicy()
    if unknown := set(table) - set(defaults.__dataclass_fields__):
        raise ValueError(f"[factory.patch] unknown keys: {', '.join(sorted(unknown))}")
    values = {}
    for name in ("max_lines", "max_files", "review_tries"):
        value = table.get(name, getattr(defaults, name))
        if type(value) is not int or value <= 0:
            raise ValueError(f"[factory.patch] {name} must be a positive integer")
        values[name] = value
    for name in ("labels", "sensitive"):
        values[name] = string_list(table[name], f"factory.patch.{name}") if name in table else getattr(defaults, name)
    values["sensitive"] = tuple(dict.fromkeys((*defaults.sensitive, *values["sensitive"])))
    return PatchPolicy(**values)


def section(data: dict, name: str) -> dict:
    try:
        return table_of(data.get(name) or {}, name)
    except TypeError:
        raise ValueError(f"[{name}] is not a table") from None


def class_command_words(value: object) -> list[str]:
    error = "targeted_commands.classes must be a plain command line"
    if not isinstance(value, str) or "\n" in value or "\0" in value:
        raise ValueError(error)
    try:
        words = shlex.split(value)
    except ValueError as exc:
        raise ValueError(error) from exc
    if (not words or not words[0] or words[0].startswith("-")
            or any(char in word for word in words for char in ";&|<>()$`#")):
        raise ValueError(error)
    return words


def load_layer(layer_dir: Path, strict: bool = False, *, source_file: Path | None = None) -> RepoLayer:
    ignored: list[str] | None = None if strict else []
    layer = RepoLayer(dir=layer_dir)
    config_file = source_file or layer.config_file
    if not config_file.exists():
        return layer
    data = tomllib.loads(config_file.read_text())
    setup = section(data, "setup")
    layer.setup = list(setup.get("commands", []))
    layer.copy = list(setup.get("copy", []))
    for name in ("checks", "targeted_commands"):
        commands = dict(section(data, name))
        if name == "targeted_commands" and "classes" in commands:
            class_command_words(commands["classes"])
        if any(not isinstance(command, str) or not command.strip() for command in commands.values()):
            raise ValueError(f"[{name}] must map names to nonempty commands")
        setattr(layer, name, commands)
    if shared := layer.checks.keys() & layer.targeted_commands.keys():
        raise ValueError(f"[targeted_commands] names collide with required checks: {', '.join(sorted(shared))}")
    check_policy(layer, section(data, "check_policy"))
    lim = section(data, "limits")
    layer.max_diff_lines = int(lim.get("max_diff_lines", layer.max_diff_lines))
    layer.deny = list(lim.get("deny", []))
    layer.max_attempts = int(lim.get("max_attempts", layer.max_attempts))
    layer.timeout_minutes = int(lim.get("timeout_minutes", layer.timeout_minutes))
    for name in ("max_repair_cycles", "max_run_minutes", "max_verify_minutes"):
        value = lim.get(name, getattr(layer, name))
        if type(value) is not int or value <= 0:
            raise ValueError(f"[limits] {name} must be a positive integer")
        setattr(layer, name, value)
    layer.parallel_checks = lim.get("parallel_checks", False)
    if type(layer.parallel_checks) is not bool:
        raise ValueError("[limits] parallel_checks must be true or false")
    agent = section(data, "agent")
    layer.allow_bash = list(DEFAULT_ALLOW_BASH) + list(agent.get("allow_bash", []))
    layer.read_roots = string_list(agent.get("read_roots", []), "agent.read_roots")
    if any(not Path(root).is_absolute() for root in layer.read_roots):
        raise ValueError("[agent] read_roots must contain absolute paths")
    trust = section(data, "trust")
    layer.trust_level = trust.get("level", "pr")
    layer.automerge_after = int(trust.get("automerge_after", 5))
    g = section(data, "git")
    layer.base_branch = g.get("base", "")
    if "required_check" in g:
        check = table_of(g["required_check"], "git.required_check")
        if (set(check) != {"context", "app_id"} or not isinstance(check["context"], str)
                or not check["context"].strip() or type(check["app_id"]) is not int or check["app_id"] < 1):
            raise ValueError("[git.required_check] needs a nonempty context and a positive integer app_id")
        layer.required_check = check
    layer.subject = g.get("subject", layer.subject)
    layer.branch = g.get("branch", layer.branch)
    layer.research_dir = str(section(data, "research").get("dir", "")).strip().strip("/")
    layer.tiers = tier_maps(section(data, "tiers"), "tiers")
    found = routing_table(section(data, "routing"))
    layer.routing_fallback, layer.capabilities, layer.floors = found.fallback, found.capabilities, found.floors
    layer.payment_modes = found.payment_modes
    layer.paid = found.paid
    layer.steps = step_tables(section(data, "steps"), "steps", ignored)
    layer.kinds = kind_tables(section(data, "kinds"), "kinds", ignored)
    layer.ab = ab_tables(section(data, "ab"))
    layer.orchestration = orchestration_table(section(data, "orchestration"))
    layer.factory_path = str(section(data, "factory").get("path", layer.factory_path)).strip()
    layer.tier_map = str(section(data, "factory").get("tier_map", "")).strip()
    layer.patch = patch_policy(table_of(section(data, "factory").get("patch", {}), "factory.patch"))
    if layer.factory_path not in FACTORY_PATHS:
        raise ValueError(f"[factory] path '{layer.factory_path}' is not one of {', '.join(FACTORY_PATHS)}")
    layer.passes = environment_table(section(data, "environment"), layer=True).passes
    source_dir = source_file.parent if source_file else layer_dir
    layer.document = document_table(section(data, "document"), source_dir, ignored)
    if source_file and "voice" in layer.document:
        layer.document["voice"] = tuple(str((layer_dir / Path(name).relative_to(source_dir.resolve())).resolve())
                                        for name in layer.document["voice"])
    if "verify" in data:
        v = section(data, "verify")
        publish_evidence = v.get("publish_evidence", True)
        if type(publish_evidence) is not bool:
            raise ValueError("[verify] publish_evidence must be true or false")
        preflight = table_of(v.get("preflight", {}), "verify.preflight")
        if any(not isinstance(name, str) or not name.strip() or not isinstance(command, str) or not command.strip()
               for name, command in preflight.items()):
            raise ValueError("[verify.preflight] must map names to nonempty command strings")
        seconds = v.get("preflight_seconds", Verify.preflight_seconds)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 300:
            raise ValueError("[verify] preflight_seconds must be greater than 0 and at most 300")
        probes = table_of(v.get("probes", {}), "verify.probes")
        if any(not name.strip() or not isinstance(command, str) or not command.strip()
               for name, command in probes.items()):
            raise ValueError("[verify.probes] must map names to nonempty commands")
        probe_seconds = v.get("probe_seconds", Verify.probe_seconds)
        if isinstance(probe_seconds, bool) or not isinstance(probe_seconds, (int, float)) or not 0 < probe_seconds <= 300:
            raise ValueError("[verify] probe_seconds must be greater than 0 and at most 300")
        allow_gaps = v.get("allow_patch_gaps", False)
        if type(allow_gaps) is not bool:
            raise ValueError("[verify] allow_patch_gaps must be true or false")
        drivers = table_of(v.get("drivers", {}), "verify.drivers")
        if any(not journeys.NAME.fullmatch(name) or not isinstance(cmd, str) or not cmd.strip()
               for name, cmd in drivers.items()):
            raise ValueError("[verify.drivers] must map lowercase, hyphen-separated names to nonempty commands")
        bindings = table_of(v.get("journey_drivers", {}), "verify.journey_drivers")
        for name, driver in bindings.items():
            if not isinstance(driver, str) or driver not in drivers:
                raise ValueError(f"[verify.journey_drivers] {name!r} must name a command in [verify.drivers]")
        feature_map = journeys.load(v.get("feature_map", {}), source_dir, bindings)
        layer.verify = Verify(publish_evidence=publish_evidence, start=v.get("start", ""), url=v.get("url", Verify.url), ready=v.get("ready", "/"),
                              ready_seconds=int(v.get("ready_seconds", Verify.ready_seconds)),
                              allow_bash=list(v.get("allow_bash", [])), notes=v.get("notes", "").strip(),
                              preflight=dict(preflight), preflight_seconds=float(seconds), probes=dict(probes),
                              probe_seconds=float(probe_seconds), allow_patch_gaps=allow_gaps,
                              feature_map=feature_map, drivers=drivers)
        outbound.keep_verify_private(layer.verify.url)
    layer.ignored = tuple(ignored or ())
    return layer
