from __future__ import annotations

import contextlib
import math
import os
import pwd
import re
import shlex
import tomllib
from dataclasses import InitVar, asdict, dataclass, field
from pathlib import Path
from typing import Literal

from . import configschema, journeys, outbound, paths, refusals, util


@dataclass(frozen=True)
class Role:
    name: str
    fallback_model: str = ""
    inherits: str = ""
    reach: Literal["step", "controller"] = "step"
    read_only: bool = False
    web: Literal["search", "fetch", "none", ""] = ""
    keeps_session: bool = True


ROLES = (
    Role("plan"),
    Role("build"),
    Role("verify"),
    Role("review"),
    Role("learn", read_only=True, keeps_session=False),
    Role("uat", keeps_session=False),
    Role("miner"),
    Role("apply"),
    Role("eval_mine"),
    Role("triage", "haiku"),
    Role("chat", "opus"),
    Role("classify", "haiku"),
    Role("loop", "sonnet"),
    Role("session", "sonnet"),
    Role("research", "opus"),
    Role("research_plan", "opus", read_only=True, keeps_session=False),
    Role("research_search", "sonnet", read_only=True, web="search", keeps_session=False),
    Role("research_synthesis", "opus", read_only=True, web="none", keeps_session=False),
    Role("research_audit", "opus", read_only=True, web="fetch", keeps_session=False),
    Role("explain", "haiku"),
    Role("document", read_only=True, keeps_session=False),
    Role("capture_evidence", "sonnet", read_only=True, keeps_session=False),
    Role("check_repair", inherits="build"),
    Role("review_repair", inherits="build"),
    Role("ci_repair", inherits="build"),
    Role("eval_grade", inherits="verify"),
    Role("orchestrate", inherits="plan", reach="controller"),
)
ROLE_BY_NAME = {role.name: role for role in ROLES}
MODEL_ROLES = tuple(role.name for role in ROLES if not role.inherits)
MODEL_FALLBACKS = {role.name: role.fallback_model for role in ROLES if role.fallback_model}
STEP_ALIASES = {"implement": "build"}
INHERITS = {role.name: role.inherits for role in ROLES if role.inherits}
STEP_ROLES = tuple(ROLE_BY_NAME)
STEP_NAMES = (*STEP_ROLES, *STEP_ALIASES)
STEP = configschema.schema("step_settings", "$defs", "step")
STEP_COUNTS = configschema.fields_of_type(STEP, "integer")
STEP_AMOUNTS = configschema.fields_of_type(STEP, "number")
STEP_LISTS = configschema.fields_of_type(STEP, "array")
INSTRUCTIONS = configschema.enum("step_settings", "$defs", "step", "properties", "instructions", "items")
SESSIONS = configschema.enum("step_settings", "$defs", "step", "properties", "session")
MCP_NAME = re.compile(r"[A-Za-z0-9_-]+")
PAYMENT_MODES = configschema.enum("routes", "properties", "payment_modes", "items")
EVIDENCE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
DOCUMENT_WORDS = {"message": (20, 60), "ticket": (80, 140)}
KINDS = configschema.keys(configschema.schema("step_settings", "$defs", "kinds"))
FACTORY_PATHS = configschema.enum("layer_toml", "properties", "factory", "properties", "path")
MAX_ASK_EXPIRE_DAYS = 3650


@dataclass(frozen=True)
class Orchestration:
    max_decisions: int = 64
    max_actions: int = 128
    max_workers: int = 3
    progress_seconds: int = 30
    max_builders: int | None = None
    private_worker_model: str = ""

    @property
    def builder_limit(self) -> int:
        return min(self.max_builders or 1, self.max_workers)


def orchestration_table(data) -> Orchestration:
    defaults = Orchestration()
    message = "[orchestration] contains unknown settings"
    if not isinstance(data, dict):
        raise ValueError(message)
    only(data, layer_schema("orchestration"), "[orchestration]", message=message)
    values = {name: data.get(name, getattr(defaults, name)) for name in defaults.__dataclass_fields__}
    model = values.pop("private_worker_model")
    if "private_worker_model" in data and (not isinstance(model, str) or len(model) > 200
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9][A-Za-z0-9._:/-]*", model)):
        raise ValueError("[orchestration] private_worker_model requires a provider/model name")
    if any(type(value) is not int or not 0 < value <= 10000 for name, value in values.items()
           if name != "max_builders" or name in data):
        raise ValueError("[orchestration] limits must be integers between 1 and 10000")
    return Orchestration(**values, private_worker_model=model)
ENV_NAME = r"\*|[A-Za-z_][A-Za-z0-9_]*\*?"


class UnknownRepo(refusals.Refusal, KeyError):
    def __str__(self) -> str:
        return self.args[0]


@dataclass
class Part:
    dir: str
    github: str


LANDING_METHODS = ("pr", "direct-push")
DEPLOYMENT_ENVIRONMENTS = ("none", "development", "staging", "production")


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
    jira_site: str = ""
    target_branch: str = ""
    landing: str = ""
    unattended_environment: str = ""
    commenters: list[str] = field(default_factory=list)

    def allows_unattended_deploy(self, environment: str) -> bool:
        return (environment in DEPLOYMENT_ENVIRONMENTS[1:] and
                (not self.unattended_environment or
                 DEPLOYMENT_ENVIRONMENTS.index(environment) <=
                 DEPLOYMENT_ENVIRONMENTS.index(self.unattended_environment)))

    def github_commenters(self) -> tuple[str, ...]:
        if self.commenters:
            seen, out = set(), []
            for login in self.commenters:
                cleaned = login.strip()
                if cleaned and cleaned.casefold() not in seen:
                    seen.add(cleaned.casefold())
                    out.append(cleaned)
            return tuple(out)
        if self.github and "/" in self.github:
            owner = self.github.split("/", 1)[0].strip()
            if owner:
                return (owner,)
        return ()

    @property
    def layer_dir(self) -> Path:
        return self.layer or self.path / ".agents"


STATES = ("running", "pr", "failed", "needs-info")
DELIVERED = "done"
RUN_LABELS = (*STATES, DELIVERED)
TASK_STATES = ("todo", *STATES, DELIVERED, "closed")
TASK_FINISHED = (DELIVERED, "closed")


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
    github_owner: str = ""
    base_url: str = ""
    email: str = ""


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
        from . import secret_move, secretstore

        missing = [name for name in ("app_id", "installation_id") if not getattr(self, name)]
        present = not paths.ISOLATED and bool(os.environ.get("GITHUB_APP_PRIVATE_KEY"))
        if not present:
            row = secretstore.registered("global", "GITHUB_APP_PRIVATE_KEY")
            if row is not None:
                present = row["status"] == "set"
            else:
                old = secret_move.fallback(self.private_key)
                present = bool(old and (not paths.ISOLATED or paths.confined(old)) and old.is_file()
                               and not old.resolve().is_relative_to(paths.ZTE_AUTHORITY.resolve()))
        if not present:
            missing.append(secretstore.address("global", "GITHUB_APP_PRIVATE_KEY"))
        return missing


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
class SecretsConfig:
    store: str | None = None
    store_dir: Path | None = None


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


@dataclass(frozen=True)
class Routing:
    fallback: bool = True
    capabilities: dict[str, dict] = field(default_factory=dict)
    floors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    payment_modes: tuple[str, ...] | None = None
    paid: PaidRoutes = PaidRoutes()


@dataclass
class ResearchConfig:
    planner: bool = False
    search: bool = False
    synthesis: bool = False
    audit: bool = False
    search_workers: int = 3
    search_share: float = 0.6


@dataclass
class CaptureConfig:
    vision: str = ""
    evidence_branch: str = "feedback-evidence"
    public_evidence: bool = False


@dataclass
class Config:
    alert_repo: str = ""
    global_repo: str = "ai"
    arete_repo: str = "arete"
    zte_repo: InitVar[str | None] = field(default=None, kw_only=True)
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
    workspace_snapshots: InitVar[Path | None] = field(default=None, kw_only=True)
    queues: dict[str, int] = field(default_factory=dict)
    pricing: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    local_providers: list[str] = field(default_factory=lambda: ["ollama"])
    daily_budget_usd: float | None = None
    session_idle_hours: float = 2
    tiers: dict[str, dict[str, tuple[dict[str, str], ...]]] = field(default_factory=dict)
    tier_map: str = ""
    routing: Routing = field(default_factory=lambda: Routing(payment_modes=PAYMENT_MODES))
    adws: Path | None = None
    steps: dict[str, dict] = field(default_factory=dict)
    kinds: dict[str, dict[str, dict]] = field(default_factory=dict)
    mcp: dict[str, dict] = field(default_factory=dict)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    ask: AskConfig = field(default_factory=AskConfig)
    retention: Retention = field(default_factory=Retention)
    backup: BackupConfig = field(default_factory=BackupConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    document: dict = field(default_factory=dict)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    oncall: OncallConfig = field(default_factory=OncallConfig)
    unblock_repos: tuple[str, ...] = ()
    unblock_step: dict = field(default_factory=dict)
    ab: dict[str, dict] = field(default_factory=dict)
    ignored: tuple[str, ...] = ()

    def __post_init__(self, zte_repo: str | None, workspace_snapshots: Path | None) -> None:
        self.zte_repo = self.arete_repo if zte_repo is None else zte_repo
        self.workspace_snapshots = workspace_snapshots

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


def policy_dict(value: Config | RepoLayer, data: dict | None = None) -> dict:
    sparse = data is not None
    if data is None:
        data = asdict(value)
    if isinstance(value, Config):
        if "jira" in data:
            data["jira"] = {key: field for key, field in data["jira"].items() if key != "github_owner"}
            if not sparse:
                for name in ("base_url", "email"):
                    if data["jira"].get(name) == "":
                        del data["jira"][name]
        if not sparse:
            for repo in data["repos"]:
                for key in ("jira_site", "target_branch", "landing", "unattended_environment", "commenters"):
                    if not repo[key]:
                        del repo[key]
    if "orchestration" in data and data["orchestration"].get("max_builders") is None:
        data["orchestration"].pop("max_builders", None)
    if "orchestration" in data and not data["orchestration"].get("private_worker_model"):
        data["orchestration"].pop("private_worker_model", None)
    routing = data.pop("routing", {})
    if sparse and isinstance(value, Config):
        if value.routing.payment_modes == PAYMENT_MODES:
            routing.pop("payment_modes", None)
        else:
            routing.setdefault("payment_modes", value.routing.payment_modes)
    if "fallback" in routing:
        routing["routing_fallback"] = routing.pop("fallback")
    data.update(routing)
    for section in ("research", "capture"):
        data.update({f"{section}_{name}": item for name, item in data.pop(section, {}).items()})
    return data


def remove_controller_only_settings(data: dict) -> None:
    for name in ("max_parallel", "max_parallel_per_repo"):
        data.pop(name, None)
    if "jev" in data:
        for name in ("risk_hold", "irreversible_hold", "secrets_hold"):
            data["jev"].pop(name, None)
        if not data["jev"]:
            del data["jev"]


def moves_table(jira: dict, table: str, hints: dict[str, str] | None = None) -> dict[str, str]:
    states = configschema.keys(configschema.schema("config_toml", "properties", "jira", "properties", table))
    moves = {str(k): str(v) for k, v in (jira.get(table) or {}).items() if v}
    if bad := sorted(set(moves) - set(states)):
        raise ValueError(f"[jira.{table}]: unknown state {', '.join(bad)}; use {', '.join(states)}"
                         + "".join(f"; {hints[b]}" for b in bad if b in (hints or {})))
    return moves


def root_schema(section: str) -> dict:
    return configschema.schema("config_toml", "properties", section)


def layer_schema(section: str) -> dict:
    return configschema.schema("layer_toml", "properties", section)


def table_of(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"[{where}] must be a table, not {value!r}")
    return value


def only(table: dict, shape: dict, where: str, noun: str = "key", *, ignored: list[str] | None = None,
         message: str = "") -> dict:
    allowed = configschema.keys(shape)
    if bad := sorted(set(table) - set(allowed)):
        if ignored is None:
            names = ", ".join(bad)
            raise ValueError(message.format(names) if message else
                             f"{where}: unknown {noun} {names}; use {', '.join(allowed)}")
        ignored.extend(f"{where} {name}" for name in bad)
        return {name: value for name, value in table.items() if name in allowed}
    return table


def flag(table: dict, key: str, default: bool, where: str, error=ValueError, tail: bool = True) -> bool:
    value = table.get(key, default)
    if type(value) is not bool:
        suffix = f", not {value!r}" if tail else ""
        raise error(f"{where} {key} must be true or false{suffix}")
    return value


def within(value, least, above, most) -> bool:
    return ((least is None or value >= least) and (above is None or value > above)
            and (most is None or value <= most))


def number_error(value, where: str, what: str, least, above, most, bound: str) -> ValueError:
    if not bound:
        bound = " and ".join(text for limit, text in (
            (least, f"at least {least}"), (above, f"above {above}"), (most, f"at most {most}"))
            if limit is not None)
    return ValueError(f"{where} must be {what}{' ' + bound if bound else ''}, not {value!r}")


def whole(value, where: str, *, least=None, above=None, most=None, bound: str = "") -> int:
    if type(value) is not int or not within(value, least, above, most):
        raise number_error(value, where, "a whole number", least, above, most, bound)
    return value


def number(value, where: str, *, least=None, above=None, most=None, finite: bool = False,
           what: str = "a number", bound: str = "") -> int | float:
    if (type(value) not in (int, float) or (finite and not math.isfinite(value))
            or not within(value, least, above, most)):
        raise number_error(value, where, what, least, above, most, bound)
    return value


def step_table(table, where: str) -> dict:
    table = only(table_of(table, where), STEP, f"[{where}]")
    for key in STEP_COUNTS:
        if key in table:
            whole(table[key], f"[{where}]: {key}", least=1, bound="of at least 1")
    for key in STEP_AMOUNTS:
        if key in table:
            number(table[key], f"[{where}]: {key}", above=0, what="a number of dollars")
    for key in STEP_LISTS:
        if key in table and not (isinstance(table[key], list | tuple) and all(isinstance(v, str) for v in table[key])):
            raise ValueError(f"[{where}]: {key} must be a list of names, not {table[key]!r}")
    if bad := [v for v in table.get("instructions", ()) if v not in INSTRUCTIONS]:
        raise ValueError(f"[{where}]: instructions {', '.join(map(repr, bad))} is not one of {', '.join(INSTRUCTIONS)}")
    if "session" in table and table["session"] not in SESSIONS:
        raise ValueError(f"[{where}]: session {table['session']!r} is not one of {', '.join(SESSIONS)}")
    flag(table, "structured_output", False, f"[{where}]:")
    flag(table, "directory_cleanup", False, f"[{where}]:")
    return {k: tuple(dict.fromkeys(v)) if k in STEP_LISTS else float(v) if k in STEP_AMOUNTS
            else v if k in STEP_COUNTS or type(v) is bool else str(v)
            for k, v in table.items()}


def mcp_tables(data) -> dict[str, dict]:
    servers = {}
    for name, entry in table_of(data, "mcp").items():
        where = f"[mcp.{name}]"
        if not MCP_NAME.fullmatch(name):
            raise ValueError(f"{where}: a server name takes only letters, digits, _ and -")
        entry = only(table_of(entry, f"mcp.{name}"), root_schema("mcp")["additionalProperties"], where)
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
    data = only(table_of(data, where), root_schema("steps"), f"[{where}]", "step", ignored=ignored)
    for alias, role in STEP_ALIASES.items():
        if alias in data and role in data:
            raise ValueError(f"[{where}.{alias}] and [{where}.{role}] are the same step; name the step once")
    return {STEP_ALIASES.get(name, name): step_table(table, f"{where}.{name}") for name, table in data.items()}


def ab_tables(data, where: str = "ab") -> dict[str, dict]:
    found = {}
    shape = configschema.schema("experiment")
    step_names = configschema.enum("experiment", "properties", "step")
    kinds = configschema.enum("experiment", "properties", "kind")[1:]
    arm_shape = configschema.schema("experiment", "properties", "arm")
    for name, table in table_of(data, where).items():
        label = f"[{where}.{name}]"
        table = only(table_of(table, f"{where}.{name}"), shape, label)
        step = table.get("step")
        if step not in step_names:
            raise ValueError(f"{label}: unknown step {step!r}; use {', '.join(step_names)}")
        kind = table.get("kind", "")
        if kind and kind not in kinds:
            raise ValueError(f"{label}: unknown kind {kind!r}; use {', '.join(kinds)}")
        share = table.get("share")
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share <= 0.5:
            raise ValueError(f"{label}: share must be a number above 0 and at most 0.5, not {share!r}")
        arm = table_of(table.get("arm"), f"{where}.{name}.arm") if "arm" in table else {}
        if not arm:
            raise ValueError(f"{label}: arm must set at least one of {', '.join(configschema.keys(arm_shape))}")
        arm = only(arm, arm_shape, f"[{where}.{name}.arm]")
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
    data = only(table_of(data, where), root_schema("kinds"), f"[{where}]", "kind", ignored=ignored)
    kinds = {}
    shape = configschema.schema("step_settings", "$defs", "kind")
    for kind, table in data.items():
        table = only(table_of(table, f"{where}.{kind}"), shape, f"[{where}.{kind}]", ignored=ignored)
        kinds[kind] = step_tables(table.get("steps") or {}, f"{where}.{kind}.steps", ignored)
    return kinds


def tier_maps(data, where: str) -> dict[str, dict[str, tuple[dict[str, str], ...]]]:
    from . import routing

    maps: dict[str, dict[str, tuple[dict[str, str], ...]]] = {}
    shape = configschema.schema("routes", "$defs", "tier_entry")
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
                entry = {k: str(v) for k, v in only(entry, shape, label).items()}
                if problem := routing.entry_problem(entry):
                    raise ValueError(f"{label}: {problem}")
                entries.append(entry)
            maps[name][tier] = tuple(entries)
    return maps


def capability_table(key: str, table) -> dict:
    where = f"[routing.capabilities.{key}]"
    table = only(table_of(table, f"routing.capabilities.{key}"), configschema.schema("routes", "$defs", "capability"), where)
    found = table.get("modalities", [])
    if not (isinstance(found, list) and all(isinstance(v, str) and v for v in found)):
        raise ValueError(f"{where}: modalities must be a list of names, not {found!r}")
    if "context" in table:
        whole(table["context"], f"{where}: context", least=1, bound="of at least 1")
    flag(table, "structured_output", False, f"{where}:")
    if "data_handling" in table and not (isinstance(table["data_handling"], str) and table["data_handling"]):
        raise ValueError(f"{where}: data_handling must be one label, not {table['data_handling']!r}")
    return {**table, **({"modalities": tuple(dict.fromkeys(found))} if "modalities" in table else {})}


def paid_table(data) -> PaidRoutes:
    table = only(table_of(data, "routing.paid"), configschema.schema("routes", "$defs", "paid"), "[routing.paid]")
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
    paid = cfg.routing.paid
    own = layer.routing.paid if layer else PaidRoutes()
    providers = (own.providers if paid.providers is None else paid.providers if own.providers is None
                 else tuple(p for p in paid.providers if p in own.providers))
    return PaidRoutes(providers, tighter(paid.max_input, own.max_input), tighter(paid.max_output, own.max_output))


def routing_table(data, *, default_modes: tuple[str, ...] | None = None) -> Routing:
    table = only(table_of(data, "routing"), root_schema("routing"), "[routing]")
    fallback = flag(table, "fallback", True, "[routing]")
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
    return Routing(fallback, capabilities, floors, modes or default_modes, paid_table(table.get("paid", {})))


def routing_for(cfg: Config, layer: RepoLayer | None = None) -> Routing:
    base = cfg.routing
    if layer is None:
        return base
    own = layer.routing
    capabilities = {key: {**base.capabilities.get(key, {}), **own.capabilities.get(key, {})}
                    for key in dict.fromkeys([*base.capabilities, *own.capabilities])}
    modes = base.payment_modes
    if own.payment_modes is not None:
        modes = tuple(mode for mode in (modes or ()) if mode in own.payment_modes)
    return Routing(base.fallback and own.fallback, capabilities, {**base.floors, **own.floors}, modes,
                   allowed_paid_routes(cfg, layer))


def allowed_payment_modes(cfg: Config, layer: RepoLayer | None = None) -> tuple[str, ...]:
    return routing_for(cfg, layer).payment_modes or ()


def price_rate(value, where: str) -> float:
    found = util.nonnegative_number(value)
    if found is None:
        raise ValueError(f"{where}: price must be a finite nonnegative number")
    return found


def replay_table(data, ignored: list[str] | None = None) -> ReplayConfig:
    table = only(table_of(data, "replay"), root_schema("replay"), "[replay]", ignored=ignored)
    return ReplayConfig(all=bool(table.get("all", False)), dir=paths.expand(table["dir"]) if table.get("dir") else None)


def ask_table(data, ignored: list[str] | None = None) -> AskConfig:
    table = only(table_of(data, "ask"), root_schema("ask"), "[ask]", ignored=ignored)
    limit = whole(table.get("max_per_run", 3), "[ask] max_per_run", least=0, bound="of at least 0")
    settle = flag(table, "settle", True, "[ask]")
    days = number(table.get("expire_days", 7), "[ask] expire_days", above=0, most=MAX_ASK_EXPIRE_DAYS, finite=True)
    hours = number(table.get("remind_hours", 24), "[ask] remind_hours", above=0, most=MAX_ASK_EXPIRE_DAYS * 24,
                   finite=True)
    contact = table.get("push_contact", "")
    if (not isinstance(contact, str) or len(contact) > 200 or re.search(r"\s", contact)
            or contact and not contact.startswith(("mailto:", "https://"))):
        raise ValueError("[ask] push_contact must be empty or a mailto: or https:// address without spaces, at most 200 characters")
    return AskConfig(max_per_run=limit, settle=settle, expire_days=days, remind_hours=hours, push_contact=contact)


def retention_table(data, ignored: list[str] | None = None) -> Retention:
    table = only(table_of(data, "retention"), root_schema("retention"), "[retention]", ignored=ignored)
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
    table = only(table_of(data, "backup"), root_schema("backup"), "[backup]")
    copy_to = table.get("copy_to", BackupConfig.copy_to)
    if not isinstance(copy_to, str) or copy_to != copy_to.strip() or copy_to.startswith("-"):
        raise ValueError(f"[backup] copy_to must be an rsync destination without surrounding spaces, not {copy_to!r}")
    return BackupConfig(copy_to=copy_to)


def store_path_parts(path: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts) if paths.ON_MAC else path.parts


def store_path_lineage(path: Path) -> list[tuple[int, int, tuple[str, ...]]]:
    top, *descendants = (*reversed(path.parents), path)
    info = top.stat()
    device, inode = info.st_dev, info.st_ino
    missing_tail: tuple[str, ...] = ()
    lineage = [(device, inode, missing_tail)]
    for descendant in descendants:
        try:
            info = descendant.stat()
        except (FileNotFoundError, NotADirectoryError):
            name = descendant.name
            missing_tail += (name.casefold() if paths.ON_MAC else name,)
        else:
            device, inode, missing_tail = info.st_dev, info.st_ino, ()
        lineage.append((device, inode, missing_tail))
    return lineage


def store_locations_overlap(directory: Path, protected: tuple[Path, ...]) -> bool:
    parts = store_path_parts(directory)
    for root in map(store_path_parts, protected):
        if parts[:len(root)] == root or root[:len(parts)] == parts:
            return True
    lineage = store_path_lineage(directory)
    for root in protected:
        protected_lineage = store_path_lineage(root)
        if protected_lineage[-1] in lineage or lineage[-1] in protected_lineage:
            return True
    return False


def validate_store_location(directory: Path, config_file: Path, repo_paths=()) -> None:
    directory = directory.resolve()
    protected = (config_file.parent, config_file.resolve().parent, paths.CONFIG_DIR,
                 paths.STATE_DIR, paths.LOG_DIR, paths.WORKTREES_DIR, *repo_paths)
    if store_locations_overlap(directory, tuple(path.resolve() for path in protected)):
        raise ValueError("[secrets] store_dir must be separate from config, state, logs, worktrees and repositories")
    for parent in (directory, *directory.parents):
        try:
            (parent / ".git").lstat()
        except (FileNotFoundError, NotADirectoryError):
            if not ((parent / "HEAD").is_file() and (parent / "objects").is_dir() and (parent / "refs").is_dir()):
                continue
        raise ValueError("[secrets] store_dir must be outside git repositories and work trees")


def keychain_program() -> str | None:
    if not paths.ON_MAC or paths.ISOLATED:
        return None
    from . import sandbox

    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        config_dir, state_dir = paths.CONFIG_DIR.resolve(), paths.STATE_DIR.resolve()
        if paths.HOME.resolve() != home or sandbox.sandboxed():
            return None
        if (config_dir, state_dir) != (home / ".config/arete", home / ".local/share/arete"):
            return None
        if paths.CONFIG_FILE.is_symlink() or paths.CONFIG_FILE.resolve() != config_dir / "config.toml":
            return None
    except (AttributeError, KeyError, OSError, RuntimeError):
        return None
    return "/usr/bin/security"


def secrets_table(data, config_file: Path, *, repo_paths=()) -> SecretsConfig:
    if not isinstance(data, dict):
        raise ValueError("[secrets] must be a table")
    table = only(data, root_schema("secrets"), "[secrets]")
    store = table.get("store", "keychain")
    if store not in ("", "keychain", "pve_store"):
        raise ValueError("[secrets] store must be keychain, pve_store or empty")
    if store == "keychain" and not keychain_program():
        store = ""
    if "store_dir" not in table:
        if store == "pve_store":
            raise ValueError("[secrets] pve_store requires store_dir")
        return SecretsConfig(store or None)
    raw = table["store_dir"]
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise ValueError("[secrets] store_dir must be a directory path")
    directory = config_file.parent / paths.expand(raw)
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("[secrets] store_dir must be a plain directory")
    if paths.ISOLATED and (paths.confined(directory) is None or
                           paths.scratch_root(directory) != paths.scratch_root(paths.STATE_DIR)):
        raise ValueError("[secrets] store_dir must be inside the instance's scratch folder")
    validate_store_location(directory, config_file, repo_paths)
    return SecretsConfig(store or None, directory.resolve())


def secret_settings() -> SecretsConfig:
    try:
        data = paths.parse_toml_bytes(paths.read_confined(paths.CONFIG_FILE))
    except FileNotFoundError:
        data = {}
    return secrets_table(data.get("secrets", {}), paths.CONFIG_FILE,
                         repo_paths=(paths.expand(repo["path"]) for repo in data.get("repo", ())))


def chat_table(data, ignored: list[str] | None = None) -> ChatConfig:
    table = only(table_of(data, "chat"), root_schema("chat"), "[chat]", ignored=ignored)
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


def research_table(data, ignored: list[str] | None = None) -> ResearchConfig:
    table = only(table_of(data, "research"), root_schema("research"), "[research]", ignored=ignored)
    planner = flag(table, "planner", ResearchConfig.planner, "[research]")
    search = flag(table, "search", ResearchConfig.search, "[research]")
    if search and not planner:
        raise ValueError("[research] search needs planner = true, because the planner sets the search tasks")
    synthesis = flag(table, "synthesis", ResearchConfig.synthesis, "[research]")
    if synthesis and not search:
        raise ValueError("[research] synthesis needs search = true, because the collator writes from the search "
                         "workers' evidence")
    audit = flag(table, "audit", ResearchConfig.audit, "[research]")
    if audit and not synthesis:
        raise ValueError("[research] audit needs synthesis = true, because the auditor checks the collator's claims")
    workers = table.get("search_workers", ResearchConfig.search_workers)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError(f"[research] search_workers must be a whole number from 1 to 8, not {workers!r}")
    share = table.get("search_share", ResearchConfig.search_share)
    if type(share) not in (int, float) or not 0 < share < 1:
        raise ValueError(f"[research] search_share must be a number above 0 and under 1, not {share!r}")
    return ResearchConfig(planner, search, synthesis, audit, workers, float(share))


def oncall_source(entry, repos: list[Repo]) -> OncallSource:
    shape = configschema.schema("config_toml", "properties", "oncall", "properties", "source", "items")
    keys = configschema.keys(shape)
    table = only(table_of(entry, "oncall.source"), shape, "[[oncall.source]]")
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
        where = f"[oncall] {key}"
        if key.endswith("_usd"):
            found[key] = float(number(table[key], where, above=least))
        elif kind is int:
            found[key] = whole(table[key], where, least=least, bound=f"{least} or more")
        else:
            found[key] = float(number(table[key], where, least=least, bound=f"{least} or more"))
    return found


def oncall_table(data, repos: list[Repo]) -> OncallConfig:
    table = only(table_of(data, "oncall"), root_schema("oncall"), "[oncall]")
    enabled = flag(table, "enabled", False, "[oncall]")
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
    shape = layer_schema("environment") if layer else root_schema("environment")
    table = only(table, shape, "[environment]")
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


def capture_table(data, folder: Path) -> CaptureConfig:
    table = only(table_of(data, "capture"), root_schema("capture"), "[capture]")
    branch = table.get("evidence_branch", CaptureConfig.evidence_branch)
    if (not isinstance(branch, str) or not EVIDENCE_BRANCH.fullmatch(branch) or ".." in branch
            or branch.endswith(("/", ".", ".lock"))):
        raise ValueError(f"[capture] evidence_branch must be a branch name such as feedback-evidence, not {branch!r}")
    public = flag(table, "public_evidence", CaptureConfig.public_evidence, "[capture]", error=TypeError)
    return CaptureConfig(capture_vision_file(table, folder), branch, public)


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
    kinds = configschema.keys(configschema.schema("config_toml", "properties", "document", "properties", "words"))
    if kind not in kinds or not whole or not 1 <= value[0] <= value[1]:
        raise ValueError(f"[document] words: {kind} must be two whole numbers, low then high, from 1 up, "
                         f"for {', '.join(kinds)}; got {value!r}")
    return value[0], value[1]


def triage_table(data, ignored: list[str] | None = None) -> TriageConfig:
    defaults = TriageConfig()
    table = only(table_of(data, "triage"), root_schema("triage"), "[triage]", ignored=ignored)
    for name in ("auto", "route_fixes"):
        flag(table, name, False, "[triage]", tail=False)
    limit = table.get("auto_per_refresh", defaults.auto_per_refresh)
    if type(limit) is not int or not 0 <= limit <= 1000:
        raise ValueError("[triage] auto_per_refresh must be a whole number from 0 to 1000")
    return TriageConfig(**table)


def jev_names(jv: dict, name: str, noun: str, ignored: list[str] | None) -> dict:
    return only(jv.get(name) or {}, configschema.schema("config_toml", "properties", "jev", "properties", name),
                f"[jev.{name}]", noun, ignored=ignored)


def document_table(data, folder: Path, ignored: list[str] | None = None) -> dict:
    table = only(table_of(data, "document"), root_schema("document"), "[document]", ignored=ignored)
    found = {}
    if "enabled" in table:
        found["enabled"] = flag(table, "enabled", False, "[document]")
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
    return flag({"https": value}, "https", False, "[dashboard]", error=TypeError)


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


def jira_table(j: dict) -> JiraConfig:
    from . import secret_move

    owner = j.get("github_owner", "")
    if not isinstance(owner, str) or owner and not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise ValueError("[jira] github_owner must be a GitHub organization name")
    return JiraConfig(
        env_file=secret_move.fallback(secret_move.configured_path({"jira": j}, "jira")),
        base_url=j.get("base_url", ""),
        email=j.get("email", ""),
        project=j.get("project", ""),
        github_owner=owner,
        transitions=moves_table(j, "transitions", {"merged": "merges move tickets through [jira.landed]",
                                                            DELIVERED: "research tickets move through [jira.research]"}),
        research=moves_table(j, "research"),
        landed={str(branch): str(target) for branch, target in (j.get("landed") or {}).items() if target},
        auto=bool(j.get("auto", False)),
        auto_per_refresh=max(0, int(j.get("auto_per_refresh", JiraConfig.auto_per_refresh))),
        sprint_only=bool(j.get("sprint_only", JiraConfig.sprint_only)),
        skip_types=[str(t) for t in j.get("skip_types") or [] if str(t).strip()],
    )


def jev_table(jv: dict, ignored: list[str] | None = None) -> JevConfig:
    return JevConfig(
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


def github_app_table(app: dict) -> GithubApp:
    from . import secret_move

    found = GithubApp(
        app_id=str(app.get("app_id", "")).strip(),
        installation_id=str(app.get("installation_id", "")).strip(),
        private_key=secret_move.fallback(secret_move.configured_path({"github_app": app}, "github_app")),
    )
    if found.installation_id and not (found.installation_id.isascii() and found.installation_id.isdigit()):
        raise ValueError("[github_app] installation_id must be digits only")
    return found


def valid_target_branch(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and value != "HEAD"
            and not value.startswith(("-", "refs/")) and not value.endswith(".")
            and not re.search(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{", value)
            and all(part and not part.startswith(".") and not part.endswith(".lock") for part in value.split("/"))
            and value != "@")


def repo_table(repo: dict) -> Repo:
    for key, choices in (("landing", LANDING_METHODS), ("unattended_environment", DEPLOYMENT_ENVIRONMENTS)):
        if key in repo and repo[key] not in choices:
            raise ValueError(f"repo {repo['name']}: {key} must be one of {', '.join(choices)}")
    if "target_branch" in repo and not valid_target_branch(repo["target_branch"]):
        raise ValueError(f"repo {repo['name']}: target_branch must be a Git branch name")
    site = repo.get("jira_site", "")
    if "jira_site" in repo:
        from . import jira

        try:
            valid_site = isinstance(site, str) and jira.checked_base(site) == site
        except (ValueError, jira.JiraError):
            valid_site = False
        if not valid_site:
            raise ValueError(f"repo {repo['name']}: jira_site must be a Jira base URL without a trailing slash")
    tracker = repo.get("tracker", "github")
    if tracker not in configschema.enum("config_toml", "properties", "repo", "items", "properties", "tracker"):
        raise ValueError(f"repo {repo['name']}: tracker must be github, jira or local, not '{tracker}'")
    github = repo.get("github", "")
    parts = [Part(p["dir"].strip("/"), p["github"]) for p in repo.get("part", [])]
    if parts and (github or tracker != "jira"):
        raise ValueError(f"repo {repo['name']}: a repo with parts takes its github from each part and needs "
                         'tracker = "jira"')
    raw_commenters = repo.get("commenters", [])
    if not isinstance(raw_commenters, list):
        raise ValueError(f"repo {repo['name']}: commenters must be a list of GitHub usernames")
    commenters: list[str] = []
    seen_commenters: set[str] = set()
    for login in raw_commenters:
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}", login.strip()):
            raise ValueError(f"repo {repo['name']}: invalid commenter login {login!r}")
        cleaned = login.strip()
        if cleaned.casefold() not in seen_commenters:
            seen_commenters.add(cleaned.casefold())
            commenters.append(cleaned)
    return Repo(
        name=repo["name"],
        path=paths.expand(repo["path"]),
        github=github,
        pipeline=bool(repo.get("pipeline", False)),
        mine=bool(repo.get("mine", True)),
        public=bool(repo.get("public", False)),
        tracker=tracker,
        jira_project=repo.get("jira_project", ""),
        jira_label=repo.get("jira_label", ""),
        layer=paths.expand(repo["layer"]) if repo.get("layer") else None,
        parts=parts or ([Part("", github)] if github else []),
        jev=bool(repo.get("jev", True)),
        jira_site=site,
        target_branch=repo.get("target_branch", ""),
        landing=repo.get("landing", ""),
        unattended_environment=repo.get("unattended_environment", ""),
        commenters=commenters,
    )


def job_table(job: dict) -> Job:
    return Job(
        label=job["label"],
        log=paths.expand(job["log"]) if job.get("log") else None,
        max_age_hours=job.get("max_age_hours"),
        keepalive=bool(job.get("keepalive", False)),
    )


def probe_table(probe: dict) -> Probe:
    return Probe(
        name=probe["name"],
        command=list(probe["command"]),
        expect=probe.get("expect", "ok"),
        every_hours=float(probe.get("every_hours", 24)),
        timeout=int(probe.get("timeout", 180)),
    )


def load(path: Path | None = None, strict: bool = False, *, required: bool = False,
         base_dir: Path | None = None) -> Config:
    path = path or paths.CONFIG_FILE
    anchor_dir = base_dir or path.parent
    anchor_file = anchor_dir / "config.toml" if base_dir is not None else path
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
        zte_repo=data.get("zte_repo"),
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
        cfg.adws = anchor_dir / paths.expand(adws)
    cfg.steps = step_tables(data.get("steps") or {}, "steps", ignored)
    cfg.kinds = kind_tables(data.get("kinds") or {}, "kinds", ignored)
    serve = data.get("serve", {})
    if "loop_dirs" in serve:
        cfg.loop_dirs = [paths.expand(d) for d in serve["loop_dirs"]]
    if "workspace_snapshots" in serve:
        value = serve["workspace_snapshots"]
        snapshots = paths.expand(value) if isinstance(value, str) else None
        if snapshots is None or not snapshots.is_absolute():
            raise ValueError("[serve] workspace_snapshots must be an absolute directory path")
        cfg.workspace_snapshots = snapshots
    cfg.queues = {str(k): max(1, int(v)) for k, v in (serve.get("queues") or {}).items()}
    cfg.daily_budget_usd = float(serve["daily_budget_usd"]) if serve.get("daily_budget_usd") else None
    cfg.session_idle_hours = float(serve.get("session_idle_hours", cfg.session_idle_hours))
    pricing = data.get("pricing", {})
    cfg.local_providers = list(pricing.get("local_providers", cfg.local_providers))
    cfg.pricing = {str(m): tuple(price_rate(r[k], f"[pricing.models.{m}] {k}") if k in r else None
                                for k in ("input", "output"))
                   for m, r in (pricing.get("models") or {}).items()}
    cfg.routing = routing_table(data.get("routing", {}), default_modes=PAYMENT_MODES)
    cfg.jira = jira_table(data.get("jira", {}))
    cfg.jev = jev_table(data.get("jev", {}), ignored)
    cfg.github_app = github_app_table(section(data, "github_app"))
    cfg.repos = [repo_table(repo) for repo in data.get("repo", [])]
    if "zte_repo" in data:
        repo_names = [repo.name for repo in cfg.repos]
        if cfg.zte_repo not in repo_names:
            raise UnknownRepo(f"zte_repo names unknown repo {cfg.zte_repo!r}")
        if cfg.zte_repo != cfg.arete_repo and cfg.arete_repo not in repo_names[:repo_names.index(cfg.zte_repo)]:
            raise ValueError(f"zte_repo {cfg.zte_repo!r} must be listed after arete_repo {cfg.arete_repo!r}")
    cfg.jobs = [job_table(job) for job in data.get("job", [])]
    cfg.probes = [probe_table(probe) for probe in data.get("probe", [])]
    cfg.oncall = oncall_table(data.get("oncall") or {}, cfg.repos)
    unblock = only(table_of(data.get("unblock", {}), "unblock"), root_schema("unblock"), "[unblock]")
    enabled = flag(unblock, "enabled", False, "[unblock]")
    repos = unblock.get("repos", [])
    if not isinstance(repos, list) or any(not isinstance(r, str) for r in repos):
        raise TypeError("[unblock] repos must be a list of repo names")
    if set(repos) - {repo.name for repo in cfg.repos if repo.pipeline}:
        raise ValueError("[unblock] repos must name configured pipeline repositories")
    cfg.unblock_step = step_table(unblock.get("step", {}), "[unblock.step]")
    if enabled:
        cfg.unblock_repos = tuple(dict.fromkeys(repos))
        if not cfg.unblock_repos:
            raise ValueError("[unblock] needs an explicit repo allowlist")
    cfg.replay = replay_table(data.get("replay") or {}, ignored)
    cfg.mcp = mcp_tables(data.get("mcp") or {})
    cfg.research = research_table(data.get("research") or {}, ignored)
    cfg.ask = ask_table(data.get("ask", {}) if strict else data.get("ask") or {}, ignored)
    cfg.triage = triage_table(data.get("triage", {}), ignored)
    cfg.retention = retention_table(data.get("retention") or {}, ignored)
    cfg.backup = backup_table(data.get("backup") or {})
    secrets_table(data.get("secrets", {}), anchor_file, repo_paths=(repo.path for repo in cfg.repos))
    cfg.chat = chat_table(data.get("chat") or {}, ignored)
    cfg.environment = environment_table(data.get("environment") or {})
    cfg.document = document_table(data.get("document") or {}, anchor_dir, ignored)
    cfg.capture = capture_table(data.get("capture") or {}, anchor_dir)
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
    routing: Routing = field(default_factory=Routing)
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


def string_list(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"[{where}] must be a list of nonempty strings")
    return tuple(dict.fromkeys(value))


def check_policy(layer: RepoLayer, table: dict) -> None:
    only(table, layer_schema("check_policy"), "[check_policy]", message="[check_policy] unknown keys: {}")
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
    shape = configschema.schema("layer_toml", "properties", "factory", "properties", "patch")
    only(table, shape, "[factory.patch]", message="[factory.patch] unknown keys: {}")
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
    layer.parallel_checks = flag(lim, "parallel_checks", False, "[limits]", tail=False)
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
        required = configschema.schema("layer_toml", "properties", "git", "properties", "required_check")["required"]
        if (set(check) != set(required) or not isinstance(check["context"], str)
                or not check["context"].strip() or type(check["app_id"]) is not int or check["app_id"] < 1):
            raise ValueError("[git.required_check] needs a nonempty context and a positive integer app_id")
        layer.required_check = check
    layer.subject = g.get("subject", layer.subject)
    layer.branch = g.get("branch", layer.branch)
    layer.research_dir = str(section(data, "research").get("dir", "")).strip().strip("/")
    layer.tiers = tier_maps(section(data, "tiers"), "tiers")
    layer.routing = routing_table(section(data, "routing"))
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
        publish_evidence = flag(v, "publish_evidence", True, "[verify]", tail=False)
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
        allow_gaps = flag(v, "allow_patch_gaps", False, "[verify]", tail=False)
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
