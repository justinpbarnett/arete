from __future__ import annotations

import json
import math
import os
import re
import signal
import threading
import time
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from statistics import NormalDist
from typing import TextIO

from .. import (
    config,
    evals,
    paths,
    pipeline,
    prices,
    record,
    replay,
    routing,
    run_lines,
    runner,
    slots,
    steps,
    util,
    workspace,
)
from ..agent import traits_of
from ..diffguard import denied_paths
from ..store import runs as run_store
from ..store import sources as source_store
from ..util import engine_commit

ATOMS = ("plan", "build", "check_repair")
FIXED_GRADERS = ("verify", "eval_grade")
SPLITS = ("train", "validation", "holdout")
TOP_KEYS = ("name", "repo", "atom", "repeats", "cache_policy", "arms", "tasks", "accept", "limits")
EXTRA_KEYS = ("prompts", "context")
ARM_KEYS = ("tier", "runner", "provider", "model", "effort", "tools", "mcp", "instructions", *EXTRA_KEYS)
ROUTE_KEYS = ("tier", "runner", "provider", "model")
ALIVE_SECONDS = 10
TASK_KEYS = ("base", "split")
ACCEPT_KEYS = ("baseline", "split", "min_pairs", "min_difference", "confidence")
LIMIT_KEYS = ("spend_usd", "minutes", "calls", "tokens")
HEADROOM = "codex_headroom"
HEADROOM_KEYS = ("tokens", "spend_usd")
LIMIT_LABELS = {"spend_usd": "spend", "minutes": "time", "calls": "calls", "tokens": "tokens"}
MIN_PAIRS = 6
DEFAULT_CACHE_POLICY = "provider-default"
NAME = re.compile(r"[A-Za-z0-9_-]+")
HEX = re.compile(r"[0-9a-fA-F]{7,40}")
LIMIT_EXIT = 3
INTERRUPT_EXIT = 130
NO_SELECTION = "This summary selects no live setting."


class Refused(ValueError):
    pass


class Capped(Exception):
    pass


@dataclass
class Totals:
    spend: float = 0.0
    calls: int = 0
    tokens: int = 0
    seconds: float = 0.0

    def plus(self, other: Totals) -> Totals:
        return Totals(self.spend + other.spend, self.calls + other.calls, self.tokens + other.tokens,
                      self.seconds + other.seconds)


@dataclass
class Experiment:
    name: str
    cfg: config.Config
    repo: config.Repo
    layer: config.RepoLayer
    atom: str
    repeats: int
    cache_policy: str
    arms: dict[str, dict]
    settings: dict[str, steps.Settings]
    tasks: dict[str, evals.Task]
    splits: dict[str, str]
    accept: dict
    limits: dict
    frozen: dict[str, dict]
    design: dict
    digest: str
    extras: dict[str, dict] = field(default_factory=dict)
    carried: Totals = field(default_factory=Totals)
    follow: bool = False
    reviews: bool = False
    checks_required: bool = False
    skip: frozenset = frozenset()
    make_run: Callable | None = None
    recovered: Callable | None = None
    codex: list[str] = field(default_factory=list)
    executors: dict[str, str] = field(default_factory=dict)


def unknown(table: dict, allowed: tuple[str, ...], where: str) -> None:
    if bad := sorted(set(table) - set(allowed)):
        raise Refused(f"{where}: unknown key {', '.join(bad)}; use {', '.join(allowed)}")


def section(table: dict, key: str, where: str = "") -> dict:
    found = table.get(key)
    if not isinstance(found, dict) or not found:
        raise Refused(f"{where}{key} is required and must be a table with entries" if key in ("arms", "tasks")
                      else f"{where}{key} is required and must be a table")
    return found


def text(table: dict, key: str, where: str = "", default: str | None = None) -> str:
    if key not in table:
        if default is None:
            raise Refused(f"{where}{key} is required")
        return default
    if not isinstance(table[key], str) or not table[key].strip():
        raise Refused(f"{where}{key} must be a non-empty string, not {table[key]!r}")
    return table[key]


def number(table: dict, key: str, where: str = "", default: float | None = None, whole: bool = False) -> int | float:
    if key not in table:
        if default is None:
            raise Refused(f"{where}{key} is required")
        return default
    value = table[key]
    if type(value) is bool or not isinstance(value, int | float) or not math.isfinite(value) \
            or (whole and type(value) is not int):
        raise Refused(f"{where}{key} must be a finite {'whole ' if whole else ''}number, not {value!r}")
    return value


sha = util.digest


def sha_json(value) -> str:
    return sha(json.dumps(value, sort_keys=True))


def engine_code(root: Path = paths.PACKAGE_DIR) -> str:
    files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.relative_to(root).parts
                   and (p.suffix in (".py", ".mjs")
                        or (p.suffix == ".md" and p.relative_to(root).parts[0] == "prompts")))
    return sha(*(part for p in files for part in (p.relative_to(root).as_posix(), p.read_bytes())))


def content(src: evals.Source, rel: str) -> bytes:
    link = src.link(rel)
    return link.encode() if link is not None else src.read(rel)


def commit_of(repo: config.Repo, where: str, base: str) -> str:
    if not HEX.fullmatch(base):
        raise Refused(f"{where} {base!r} must be 7 to 40 hex digits of a commit")
    found = util.sh(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=repo.path, timeout=30)
    if found.returncode != 0 or not found.stdout.strip():
        raise Refused(f"{where} {base} is not a commit in {repo.name}")
    return found.stdout.strip()


def base_commit(repo: config.Repo, name: str, base: str, key: str = "base") -> str:
    return commit_of(repo, f"tasks.{name}: {key}", base)


def read_extras(repo: config.Repo, name: str, entry: dict) -> dict:
    found = {}
    if "prompts" in entry:
        folder = paths.expand(text(entry, "prompts", f"arms.{name}."))
        if missing := evals.Prompts(folder).missing():
            raise Refused(f"arms.{name}: prompts {paths.short_path(folder)} has no {', '.join(missing)}")
        found["prompts"] = str(folder)
    if "context" in entry:
        found["context"] = commit_of(repo, f"arms.{name}: context", text(entry, "context", f"arms.{name}."))
    return found


def read_arms(cfg: config.Config, layer: config.RepoLayer, atom: str, table: dict,
              repo: config.Repo) -> tuple[dict, dict, dict]:
    if len(table) < 2:
        raise Refused("arms: name two or more arms")
    given, resolved, extras = {}, {}, {}
    for name, entry in table.items():
        if not NAME.fullmatch(name):
            raise Refused(f"arms.{name}: a name takes only letters, digits, _ and -")
        if not isinstance(entry, dict):
            raise Refused(f"arms.{name} must be a table")
        unknown(entry, ARM_KEYS, f"arms.{name}")
        try:
            given[name] = config.step_table({k: v for k, v in entry.items() if k not in EXTRA_KEYS}, f"arms.{name}")
            resolved[name] = steps.resolve(cfg, layer, atom, "", given[name])
        except ValueError as e:
            raise Refused(f"arm {name}: {e}") from e
        if extra := read_extras(repo, name, entry):
            extras[name] = extra
    return given, resolved, extras


def read_tasks(repo: config.Repo, table: dict) -> tuple[dict[str, evals.Task], dict[str, str]]:
    try:
        known = {t.name: t for t in evals.load_tasks(*evals.task_source(repo, evals.Source(repo)))}
    except (ValueError, TypeError, KeyError) as e:
        raise Refused(f"the eval tasks of {repo.name} do not load: {type(e).__name__}: {e}") from e
    tasks, splits = {}, {}
    for name, entry in table.items():
        if not isinstance(entry, dict):
            raise Refused(f"tasks.{name} must be a table")
        unknown(entry, TASK_KEYS, f"tasks.{name}")
        if name not in known:
            raise Refused(f"tasks.{name}: {repo.name} has no eval task of that name")
        task = known[name]
        if not task.check and not task.verify:
            raise Refused(f"tasks.{name}: the task has neither `check` nor `verify`")
        split = text(entry, "split", f"tasks.{name}.")
        if split not in SPLITS:
            raise Refused(f"tasks.{name}.split must be one of {', '.join(SPLITS)}, not {split!r}")
        base = text(entry, "base", f"tasks.{name}.", task.base) if (entry.get("base") or task.base) else ""
        if not base:
            raise Refused(f"tasks.{name}.base is required, as the eval task names none")
        tasks[name] = replace(task, base=base_commit(repo, name, base))
        splits[name] = split
    return tasks, splits


def read_accept(table: dict, arms: dict, splits: dict[str, str]) -> dict:
    unknown(table, ACCEPT_KEYS, "accept")
    baseline = text(table, "baseline", "accept.")
    if baseline not in arms:
        raise Refused(f"accept.baseline {baseline!r} is not an arm; use {', '.join(arms)}")
    split = text(table, "split", "accept.", "validation")
    if split not in splits.values():
        raise Refused(f"accept.split {split!r} is used by no task")
    min_pairs = number(table, "min_pairs", "accept.", MIN_PAIRS, whole=True)
    if min_pairs < MIN_PAIRS:
        raise Refused(f"accept.min_pairs must be at least {MIN_PAIRS}, not {min_pairs}")
    min_difference = float(number(table, "min_difference", "accept.", 0.0))
    if not 0 <= min_difference <= 1:
        raise Refused(f"accept.min_difference must be from 0 to 1, not {min_difference}")
    confidence = float(number(table, "confidence", "accept.", 0.95))
    if not 0.5 < confidence < 1:
        raise Refused(f"accept.confidence must be above 0.5 and under 1, not {confidence}")
    return {"baseline": baseline, "split": split, "min_pairs": min_pairs, "min_difference": min_difference,
            "confidence": confidence}


def read_headroom(table) -> dict:
    if not isinstance(table, dict) or not table:
        raise Refused("limits.codex_headroom must be a table with tokens")
    unknown(table, HEADROOM_KEYS, "limits.codex_headroom")
    found = {"tokens": number(table, "tokens", "limits.codex_headroom.", whole=True)}
    if "spend_usd" in table:
        found["spend_usd"] = number(table, "spend_usd", "limits.codex_headroom.")
    for key, value in found.items():
        if value <= 0:
            raise Refused(f"limits.codex_headroom.{key} must be above 0, not {value}")
    return found


def read_limits(table: dict) -> dict:
    unknown(table, (*LIMIT_KEYS, HEADROOM), "limits")
    found = {}
    for key in LIMIT_KEYS:
        found[key] = number(table, key, "limits.", whole=key in ("calls", "tokens"))
        if found[key] <= 0:
            raise Refused(f"limits.{key} must be above 0, not {found[key]}")
    if HEADROOM in table:
        found[HEADROOM] = read_headroom(table[HEADROOM])
    return found


def fingerprints(repo: config.Repo, layer: config.RepoLayer, prompts_dir: Path = paths.PROMPTS_DIR,
                 ref: str = "") -> dict:
    src = evals.Source(repo, ref)
    harness = [part for f in sorted(evals.harness_files(src)) for part in (f, content(src, f))]
    expertise = layer.expertise_file.read_bytes() if layer.expertise_file.exists() else b""
    return {"prompt": sha(*[(prompts_dir / f"{n}.md").read_bytes() for n in evals.BUILDER_PROMPTS]),
            "context": sha(*harness, expertise),
            "tools": sha_json([layer.builder_bash, layer.checks, denied_paths(layer), routing.NO_GIT_WRITES])}


def mcp_definitions(cfg: config.Config, s: steps.Settings) -> str:
    return sha_json({n: cfg.mcp.get(n) for n in s.mcp})


def arm_fingerprints(base: dict, cfg: config.Config, s: steps.Settings) -> dict:
    return {"prompt": sha(base["prompt"], json.dumps(list(s.instructions_on(s.runner)))),
            "context": base["context"],
            "tools": sha(base["tools"], json.dumps([list(s.tools), list(s.mcp)]), mcp_definitions(cfg, s))}


def effective(cfg: config.Config, s: steps.Settings) -> str:
    settings = asdict(s)
    for name in ("delegation_only", "gated_launches"):
        if not settings["requires"].get(name):
            settings["requires"].pop(name, None)
    return sha(json.dumps(settings, sort_keys=True, default=str), mcp_definitions(cfg, s))


def codex_route(label: str, s: steps.Settings, limits: dict) -> list[str]:
    which = routing.stands_for(s.model, s.runner)[0]
    if not (why := traits_of(which).cannot_stop):
        return []
    if HEADROOM not in limits:
        raise Refused(f"{label}: {why}")
    return [f"{label} ({which} {s.model})"]


def unstoppable(cfg: config.Config, atom: str, arms: dict, frozen: dict, grader: steps.Settings,
                limits: dict) -> list[str]:
    held = {**{f"arm {n}": (s, atom) for n, s in arms.items()}, **{f"step {r}": (s, r) for r, s in frozen.items()},
            "the grader": (grader, "grade")}
    routes = []
    for label, (s, role) in held.items():
        routes += codex_route(label, s, limits)
        which = routing.chosen(s.model, s.runner, cfg)[0]
        if role in ("plan", "grade") and (why := traits_of(which).cannot_gate):
            raise Refused(f"{label}: {why}")
    return routes


def codex_lines(limits: dict, routes: list[str]) -> list[str]:
    if HEADROOM not in limits:
        return []
    room = limits[HEADROOM]
    words = f"{room['tokens']} tokens" + (f" and ${room['spend_usd']:g}" if "spend_usd" in room else "")
    return [f"codex headroom: each codex launch needs at least {words} left",
            *[f"codex route {r}: launched only with at least {words} left" for r in routes]]


def followers(atom: str, follow: bool) -> list[str]:
    return [r for r, b in config.INHERITS.items() if b == atom and r in ATOMS] if follow else []


def freeze(cfg: config.Config, layer: config.RepoLayer, atom: str, baseline: dict,
           follow: bool = False) -> dict[str, dict]:
    route = {k: v for k, v in baseline.items() if k in ROUTE_KEYS}
    return {role: route for role in ATOMS
            if role != atom and role not in followers(atom, follow) and route
            and not steps.resolve(cfg, layer, role, "").model}


def evidence(task: evals.Task) -> str:
    hidden = [part for n in sorted(task.hidden_files) for part in (n, task.hidden_files[n])]
    return sha(task.title, task.prompt, task.check, task.verify, *hidden)


def load(path: Path) -> Experiment:
    return designed(tomllib.loads(path.read_text()))


def designed(data: dict, carried: Totals | None = None, follow: bool = False) -> Experiment:
    if "executor" in data:
        from . import workflows

        return workflows.designed(data, carried, follow)
    unknown(data, TOP_KEYS, "the manifest")
    name = text(data, "name")
    if not NAME.fullmatch(name):
        raise Refused("name takes only letters, digits, _ and -")
    atom = text(data, "atom")
    if atom in FIXED_GRADERS:
        raise Refused(f"atom {atom!r}: the grader stays fixed")
    if atom not in ATOMS:
        raise Refused(f"atom must be one of {', '.join(ATOMS)}, not {atom!r}")
    repeats = number(data, "repeats", whole=True)
    if repeats < 1:
        raise Refused(f"repeats must be 1 or more, not {repeats}")
    cache_policy = text(data, "cache_policy", "", DEFAULT_CACHE_POLICY)
    cfg = config.load()
    repo = cfg.repo(text(data, "repo"))
    if any(p.dir for p in repo.parts):
        raise Refused(f"repo {repo.name}: experiments run on a single repo, and this one has parts")
    try:
        layer = config.load_layer(repo.layer_dir)
        grader = steps.resolve(cfg, layer, "eval_grade", "")
    except (ValueError, OSError) as e:
        raise Refused(f"repo {repo.name}: {e}") from e
    given, settings, extras = read_arms(cfg, layer, atom, section(data, "arms"), repo)
    tasks, splits = read_tasks(repo, section(data, "tasks"))
    accept = read_accept(section(data, "accept"), given, splits)
    limits = read_limits(section(data, "limits"))
    shared = fingerprints(repo, layer)
    frozen = freeze(cfg, layer, atom, given[accept["baseline"]], follow)
    following = followers(atom, follow)
    held = {role: steps.resolve(cfg, layer, role, "", frozen.get(role)) for role in ATOMS
            if role != atom and role not in following}
    inherited = {n: {role: steps.resolve(cfg, layer, role, "", None, given[n]) for role in following}
                 for n in settings}
    routes = unstoppable(
        cfg, atom, settings,
        {**held, **{f"{role} of arm {n}": s for n, roles in inherited.items() for role, s in roles.items()}},
        grader, limits)
    own = {n: fingerprints(repo, layer, Path(x.get("prompts", paths.PROMPTS_DIR)), x.get("context", ""))
           for n, x in extras.items()}
    design = {
        "name": name, "repo": repo.name, "atom": atom, "repeats": repeats, "cache_policy": cache_policy,
        "accept": accept,
        "arms": {n: {**given[n], "runner": s.runner, "model": s.model, "provider": s.provider, "tier": s.tier,
                     "effort": s.effort, "settings": effective(cfg, s),
                     "fingerprints": arm_fingerprints(own.get(n, shared), cfg, s),
                     **{k: paths.short_path(v) if k == "prompts" else v for k, v in extras.get(n, {}).items()},
                     **({"inheritors": {r: effective(cfg, s2) for r, s2 in inherited[n].items()}} if follow else {})}
                 for n, s in settings.items()},
        "frozen": {role: {"overrides": frozen.get(role, {}), "settings": effective(cfg, s)}
                   for role, s in held.items()},
        "tasks": {n: {"split": splits[n], "base": t.base, "evidence": evidence(t)} for n, t in tasks.items()},
        "grader": {"runner": grader.runner, "model": grader.model, "effort": grader.effort,
                   "settings": effective(cfg, grader), "verify": sha_json(layer.verify.policy()) if layer.verify else "",
                   "prompt": sha((paths.PROMPTS_DIR / "grade.md").read_bytes())},
        "fingerprints": shared,
        "engine": engine_code(),
        "money": {"prices": (prices.load() or {}).get("version", ""),
                  "local_providers": sorted(cfg.local_providers)},
        **({HEADROOM: limits[HEADROOM]} if HEADROOM in limits else {}),
    }
    digest = sha_json(design)[:12]
    return Experiment(name, cfg, repo, layer, atom, repeats, cache_policy, given, settings, tasks, splits, accept,
                      limits, frozen, design, digest, extras, carried or Totals(), follow, codex=routes)


def observed(exp: Experiment, arm: str) -> dict:
    invalid = False
    try:
        layer = config.load_layer(exp.repo.layer_dir)
    except (ValueError, TypeError, OSError):
        layer, invalid = exp.layer, True
    extra = exp.extras.get(arm, {})
    base = fingerprints(exp.repo, layer, Path(extra.get("prompts", paths.PROMPTS_DIR)), extra.get("context", ""))
    found = arm_fingerprints(base, exp.cfg, exp.settings[arm])
    if invalid:
        found["context"] = "unreadable layer"
    workflow = {}
    if exp.executors:
        from . import workflows

        workflow = {"workflow": workflows.fingerprint(config.load(), layer)}
    return {"fingerprints": found, "grader": sha((paths.PROMPTS_DIR / "grade.md").read_bytes()),
            "engine": engine_code(), "prices": (prices.load() or {}).get("version", ""), **workflow}


def drift(exp: Experiment, arm: str, seen: dict) -> list[str]:
    changed = [k for k in ("prompt", "context", "tools")
               if seen["fingerprints"][k] != exp.design["arms"][arm]["fingerprints"][k]]
    if exp.executors and seen.get("workflow") != exp.design["workflow_sha"]:
        changed.append("workflow")
    return changed + [k for k, expected in (("grader", exp.design["grader"]["prompt"]),
                                           ("engine", exp.design["engine"]),
                                           ("prices", exp.design["money"]["prices"])) if seen[k] != expected]


def drift_reason(changed: list[str]) -> str:
    return (f"the {', '.join(changed)} changed since the design; "
            "restore them to continue or pass --out for a new file")


def default_path(name: str) -> Path:
    return paths.STATE_DIR / "experiments" / f"{name}.jsonl"


_appending = threading.Lock()


def append(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _appending, open(path, "a+b") as f:
        size = f.seek(0, 2)
        torn = False
        if size:
            f.seek(size - 1)
            torn = f.read(1) != b"\n"
        f.seek(0, 2)
        f.write((b"\n" if torn else b"") + json.dumps(event, ensure_ascii=False).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())


def events(path: Path) -> list[dict]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    found = []
    for piece in data.split(b"\n"):
        try:
            line = json.loads(piece)
        except (ValueError, RecursionError):
            continue
        if isinstance(line, dict):
            found.append(line)
    return found


def key_of(line: dict) -> tuple:
    return line.get("task"), line.get("arm"), line.get("repeat")


def completed(found: list[dict]) -> set[tuple]:
    return {key_of(e) for e in found if e.get("event") == "trial" and e.get("status") == "done"}


def schedule(exp: Experiment) -> list[tuple[str, str, int]]:
    arms = list(exp.arms)
    order = []
    for repeat in range(1, exp.repeats + 1):
        for index, task in enumerate(exp.tasks):
            turn = (repeat - 1 + index) % len(arms)
            order += [(task, arm, repeat) for arm in arms[turn:] + arms[:turn]]
    return order


def attempt_of(line: dict) -> tuple:
    return (*key_of(line), line.get("attempt"))


def slot_seconds(found: list[dict], begun: dict) -> float:
    return sum(e.get("seconds") or 0 for e in found if e.get("event") == "wait" and attempt_of(e) == attempt_of(begun))


def totals_of(found: list[dict]) -> Totals:
    used = Totals()
    settled = {attempt_of(e) for e in found if e.get("event") == "trial"}
    for e in found:
        if e.get("event") == "trial":
            used.spend += e.get("charged_usd") or 0
            used.calls += e.get("calls") or 0
            used.tokens += (e.get("tokens_in") or 0) + (e.get("tokens_out") or 0)
            used.seconds += (e.get("wall_seconds") or 0) + (e.get("wait_seconds") or 0)
        elif e.get("event") == "setup" or (e.get("event") == "wait" and attempt_of(e) not in settled):
            used.seconds += e.get("seconds") or 0
    return used


def reached(limits: dict, used: Totals, launching: bool = True) -> str:
    calls = used.calls + 1 if launching else used.calls
    over = {"spend_usd": used.spend >= limits["spend_usd"], "calls": calls > limits["calls"],
            "tokens": used.tokens >= limits["tokens"], "minutes": used.seconds / 60 >= limits["minutes"]}
    return next((f"the {LIMIT_LABELS[key]} limit ({key} = {limits[key]:g}) is reached"
                 for key in ("spend_usd", "calls", "tokens", "minutes") if over[key]), "")


def seconds_left(limits: dict, used: Totals) -> float:
    return limits["minutes"] * 60 - used.seconds


def timed_out(limits: dict, used: Totals, launching: bool = True) -> str:
    return reached(limits, replace(used, seconds=limits["minutes"] * 60), launching)


def framed(path: Path) -> tuple[dict, dict | None]:
    header, result = {}, None
    for entry in record.lines(path):
        kind = entry.get("arete")
        if kind == "header" and not header:
            header = entry
        elif kind == "result":
            result = entry
            break
    return header, result


def missing_schema_keys(header: dict, result: dict | None) -> bool:
    schema = header.get("json_schema")
    if not schema or not result or not result.get("ok"):
        return False
    structured = result.get("structured")
    return not isinstance(structured, dict) or any(k not in structured for k in schema.get("required") or [])


def faults(exp: Experiment, run_id: str) -> dict:
    tool_errors = schema_errors = 0
    for file in record.record_files(run_id):
        header, result = framed(file)
        if header.get("atom") != exp.atom:
            continue
        tool_errors += sum(e.get("kind") == "tool_result" and bool(e.get("error")) for e in record.events(file))
        schema_errors += missing_schema_keys(header, result)
    return {"tool_errors": tool_errors, "schema_errors": schema_errors}


def usage(exp: Experiment, arm: str, run_id: str) -> dict:
    rows = run_store.steps(run_id)
    charged = run_store.paid_by_run([run_id], local_providers=exp.cfg.local_providers).get(run_id, 0.0)
    records = [framed(f) for f in record.record_files(run_id)]
    header, result = next(((h, r) for h, r in records if h.get("atom") == exp.atom), ({}, None))
    launched = bool(header) and not header.get("refused")
    asked = exp.design["arms"][arm]
    reference, unpriced = 0.0, 0
    for _, r in records:
        if r is None or r.get("tokens_in") is None:
            continue
        if isinstance(r.get("reference_usd"), int | float):
            reference += r["reference_usd"]
        else:
            unpriced += 1

    def seen(key: str):
        return (result or {}).get(key) or header.get(key) or (asked.get(key, "") if not launched else "")

    return {
        **faults(exp, run_id),
        "targeted_step": "launched" if launched else "never launched",
        "route": {**{k: seen(k) for k in ("runner", "model", "provider", "tier", "credential", "billing",
                                          "auth_mode")},
                  "position": header.get("position"), "requested": header.get("requested")},
        "effort": seen("effort"), "requested_effort": seen("requested_effort") or (
            asked["effort"] if not launched else ""),
        "endpoint": {"provider": seen("provider"), "device": header.get("device", ""),
                     "credential": seen("credential"), "billing": seen("billing"), "auth_mode": seen("auth_mode")},
        "cache_policy": exp.cache_policy,
        "calls": len(rows), "tokens_in": sum(r["tokens_in"] or 0 for r in rows),
        "tokens_out": sum(r["tokens_out"] or 0 for r in rows),
        "cache_read": sum(r["cache_read"] or 0 for r in rows), "cache_write": sum(r["cache_write"] or 0 for r in rows),
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 6), "charged_usd": charged,
        "reference_usd": None if unpriced else round(reference, 6), "reference_unknown_calls": unpriced,
        "api_seconds": round(sum(r["api_seconds"] or 0 for r in rows), 2),
        "wait_seconds": round(sum(m for cause, m in run_lines.wait_minutes(run_id).items() if cause != "slot") * 60,
                              2),
    }


def trial_line(exp: Experiment, path: Path, begun: dict, status: str, wall: float, accepted: bool = False,
               failures: list[str] | None = None, repairs: int = 0, outcomes: dict | None = None,
               seen: dict | None = None) -> dict:
    task, arm = begun["task"], begun["arm"]
    seen = seen if seen is not None else begun["seen"]
    used = usage(exp, arm, begun["run"])
    if (outcomes or {}).get("usage_unknown"):
        used.update(charged_usd=None, reference_usd=None,
                    reference_unknown_calls=max(1, used["reference_unknown_calls"]))
    used["wait_seconds"] = round(used["wait_seconds"] + slot_seconds(events(path), begun), 2)
    return {"event": "trial", "at": util.iso(), "task": task, "split": exp.splits[task],
            "base": exp.tasks[task].base, "arm": arm, "repeat": begun["repeat"], "attempt": begun["attempt"],
            "run": begun["run"], "status": status, "accepted": accepted, "failures": failures or [],
            "repairs": repairs, "fingerprints": seen["fingerprints"], "engine": seen["engine"],
            **(outcomes or {}), **used, "wall_seconds": round(wall, 2)}


def last_alive(found: list[dict], run_id: str) -> float:
    return max((e.get("seconds") or 0 for e in found if e.get("event") == "alive" and e.get("run") == run_id),
               default=0.0)


def settle_interrupted(exp: Experiment, path: Path) -> None:
    found = events(path)
    written = {e.get("run") for e in found if e.get("event") == "trial"}
    for begun in (e for e in found if e.get("event") == "start" and e.get("run") not in written):
        record.settle(begun["run"], "interrupted", summary=record.INTERRUPTED)
        outcomes = exp.recovered(begun["run"]) if exp.recovered else {}
        if exp.executors:
            outcomes = {**(outcomes or {}), "executor": exp.executors[begun["arm"]], "usage_unknown": True}
        append(path, trial_line(exp, path, begun, "interrupted", last_alive(found, begun["run"]), outcomes=outcomes))


@dataclass
class TrialRun(evals.EvalRun):
    max_builders: int | None = None
    limits: dict = field(default_factory=dict)
    before: Totals = field(default_factory=Totals)
    began: float = field(default_factory=time.monotonic)
    call_timeout: float = 0.0
    stopped_by: str = ""
    pinned: config.RepoLayer | None = None
    follow: bool = False

    def outcomes(self) -> dict:
        return self.execution

    def used(self) -> Totals:
        rows = run_store.steps(self.id)
        paid = run_store.paid_by_run([self.id], local_providers=self.config.local_providers).get(self.id, 0.0)
        return Totals(self.before.spend + paid, self.before.calls + len(rows),
                      self.before.tokens + sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in rows),
                      self.before.seconds + time.monotonic() - self.began)

    def seconds_left(self) -> float:
        return seconds_left(self.limits, self.used())

    def repair_limit(self, layer) -> int:
        return self.settings("check_repair").tries

    def call_kw(self, cfg: config.Config, role: str, tools: list[str], rules: list[str]) -> dict:
        return {"role": role, "allowed_tools": rules}

    def adopt(self, cfg: config.Config, layer: config.RepoLayer) -> None:
        self.cfg, self.layer = cfg, layer

    def stop(self, why: str):
        self.stopped_by = why
        raise workspace.RunFailed(why)

    def write_line(self, event: str, **fields) -> None:
        if event == "step_start":
            if why := reached(self.limits, self.used()):
                self.stop(why)
            if fields.get("launch", 1) > 1 and self.seconds_left() < self.call_timeout:
                self.stop(timed_out(self.limits, self.used()))
        super().write_line(event, **fields)

    def wait_for_network(self) -> bool:
        left = self.seconds_left()
        if left <= 0 or not util.wait_for_network(min(30, left / 60)):
            return False
        return self.seconds_left() > 0

    def retry_kw(self) -> dict:
        return {"timeout": min(self.call_timeout, self.seconds_left())}

    def settings(self, role: str):
        role = config.STEP_ALIASES.get(role, role)
        return steps.resolve(self.config, self.pinned or self.layer, role, self.kind,
                             {**self.overrides, **self.step_overrides.get(role, {})},
                             self.step_overrides.get(config.INHERITS.get(role, ""), {}) if self.follow else None)

    def time_for(self, seconds: float) -> float:
        left = self.seconds_left()
        if left <= 0:
            self.stop(timed_out(self.limits, self.used()))
        return min(seconds, left)

    def dollars_left(self) -> float:
        return max(0.0, self.limits["spend_usd"] - self.used().spend)

    def tokens_left(self) -> int:
        return max(0, self.limits["tokens"] - self.used().tokens)

    def agent(self, step: str, text: str, cwd: Path, role: str = "", **kw):
        if why := reached(self.limits, self.used()):
            self.stop(why)
        left = self.seconds_left()
        given = kw.get("timeout") or (self.settings(role).timeout_minutes * 60 if role else left)
        self.call_timeout = min(given, left)
        kw.update(timeout=self.call_timeout, wait_out_limits=False, remaining_budget=self.dollars_left,
                  remaining_tokens=self.tokens_left, codex_headroom=self.limits.get(HEADROOM))
        res = super().agent(step, text, cwd, role, **kw)
        if not res.ok and res.error.startswith(runner.NO_ROUTE):
            self.stopped_by = self.stopped_by or res.error
            if runner.out_of_headroom(res):
                self.stop(self.stopped_by)
        elif why := reached(self.limits, self.used(), launching=False):
            self.stop(self.stopped_by or why)
        return res


@contextmanager
def replay_folder(exp: Experiment, task: str, arm: str, repeat: int):
    base = replay.folder(exp.cfg)
    if base is None:
        yield
        return
    wanted = (base / arm / task / str(repeat), base / arm / task, base / arm, base)
    earlier = os.environ.get(replay.ENV)
    os.environ[replay.ENV] = str(next((d for d in wanted if d.is_dir() and any(d.glob("*.json"))), base))
    try:
        yield
    finally:
        if earlier is None:
            os.environ.pop(replay.ENV, None)
        else:
            os.environ[replay.ENV] = earlier


def slot_reason(exp: Experiment, free: int, passed: list[dict]) -> str:
    if passed:
        return f"{util.count(len(passed), 'ready ticket')} {'waits' if len(passed) == 1 else 'wait'} for one"
    return slots.no_free_slot(exp.cfg.max_parallel) if free <= 0 else ""


def take_slot(exp: Experiment, path: Path, waiting: dict, used: Totals) -> tuple[str, TextIO, str, float]:
    tag = f"{exp.name}-{waiting['task']}-{waiting['arm']}-{waiting['repeat']}"
    began, noted, since, told = time.monotonic(), time.monotonic(), "", ""
    while True:
        with slots.admitting():
            free = slots.slots_free_to_start(slots.live_max_parallel(exp.cfg.max_parallel))
            passed = source_store.tickets_passed_over()
            if free > 0 and not passed:
                run_id = record.start("experiment", tag=tag, lock=run_store.run_lock("experiment", exp.repo.name),
                                      repo=exp.repo.name)
                held = slots.hold(exp.repo.name, None)
                if since:
                    append(path, {**waiting, "seconds": round(time.monotonic() - noted, 2), "at": util.iso()})
                return run_id, held, since, time.monotonic() - began
        if (why := slot_reason(exp, free, passed)) != told:
            told = why
            print(f"waiting for a run slot: {why}", flush=True)
        left = seconds_left(exp.limits, used) - (time.monotonic() - began)
        if left <= 0:
            raise Capped(timed_out(exp.limits, used))
        since = since or util.iso()
        try:
            time.sleep(min(slots.SLOT_POLL_SECONDS, ALIVE_SECONDS, left))
        finally:
            append(path, {**waiting, "seconds": round(time.monotonic() - noted, 2), "at": util.iso()})
            noted = time.monotonic()


def trial_overrides(exp: Experiment, arm: str) -> dict[str, dict]:
    return {**exp.frozen, exp.atom: exp.arms[arm]}


@contextmanager
def alive(path: Path, run_id: str, started: float):
    def beat() -> None:
        append(path, {"event": "alive", "run": run_id, "seconds": round(time.monotonic() - started, 2)})

    with util.repeating(ALIVE_SECONDS, beat, f"experiment {run_id}"):
        yield


def overrun(limits: dict, used: Totals) -> dict:
    tokens = used.tokens - limits["tokens"]
    spend = used.spend - limits["spend_usd"]
    if tokens <= 0 and spend <= 0:
        return {}
    return {"overrun": {"tokens": max(0, tokens), "spend_usd": round(max(0.0, spend), 6)}}


def run_trial(exp: Experiment, path: Path, task: str, arm: str, repeat: int, used: Totals, seen: dict) -> str:
    found = events(path)
    attempt = 1 + max((e["attempt"] for e in found if e.get("event") == "start" and key_of(e) == (task, arm, repeat)),
                      default=0)
    waiting = {"event": "wait", "task": task, "arm": arm, "repeat": repeat, "attempt": attempt}
    run_id, held, since, waited = take_slot(exp, path, waiting, used)
    try:
        with record.failed_on_crash(run_id):
            if since:
                run_lines.waited(run_id, "slot", since, util.iso())
            seen = observed(exp, arm)
            if changed := drift(exp, arm, seen):
                record.settle(run_id, "stopped", summary=drift_reason(changed))
                return drift_reason(changed)
            begun ={"event": "start", "task": task, "arm": arm, "repeat": repeat, "attempt": attempt,
                     "run": run_id, "at": util.iso(), "seen": seen}
            append(path, begun)
            started = time.monotonic()
            given = {"id": run_id, "repo": exp.repo, "dir": paths.RUNS_DIR / run_id, "cfg": exp.cfg, "task": task,
                     "trial": f"{arm}-{repeat}", "step_overrides": trial_overrides(exp, arm), "limits": exp.limits,
                     "pinned": exp.layer, "follow": exp.follow, "reviews": exp.reviews,
                     "checks_required": exp.checks_required, "before": replace(used, seconds=used.seconds + waited)}
            trial = exp.make_run(exp, arm, given) if exp.make_run else TrialRun(**given)
            trial.dir.mkdir(parents=True, exist_ok=True)
            try:
                with alive(path, run_id, started), replay_folder(exp, task, arm, repeat):
                    extra = exp.extras.get(arm, {})
                    prompts = evals.Prompts(Path(extra["prompts"]), paths.short_path(extra["prompts"])) \
                        if "prompts" in extra else evals.Prompts()
                    result = evals.run_task(exp.cfg, evals.Source(exp.repo, extra.get("context", "")),
                                            exp.tasks[task], prompts, run=trial)
                seen = observed(exp, arm)
            except (KeyboardInterrupt, pipeline.Stopped):
                record.settle(run_id, "stopped", summary=record.STOPPED)
                append(path, trial_line(exp, path, begun, "interrupted", time.monotonic() - started,
                                        outcomes=trial.outcomes()))
                raise
            failures = result.failures or ([result.why] if result.why else [])
            changed = drift(exp, arm, seen)
            stopped_by = drift_reason(changed) if changed else trial.stopped_by or reached(
                exp.limits, trial.used(), launching=False)
            record.settle(run_id, "stopped" if stopped_by else "done",
                          summary=stopped_by if stopped_by else "accepted" if result.passed
                          else f"not accepted: {util.first_line(result.why, 200)}")
            append(path, trial_line(exp, path, begun, "drifted" if changed else "capped" if stopped_by else "done",
                                    time.monotonic() - started,
                                    result.passed and not stopped_by, failures, result.repairs,
                                    {"checks": result.checks, "verify": result.verify,
                                     "hidden_check": result.hidden, "blocking_findings": result.blocking,
                                     **trial.outcomes(),
                                     **({"drift": changed} if changed else {}),
                                     **(overrun(exp.limits, trial.used()) if stopped_by else {})}, seen=seen))
            return stopped_by
    finally:
        held.close()


def work(exp: Experiment, path: Path) -> int:
    for task, arm, repeat in schedule(exp):
        found = events(path)
        if arm in exp.skip or (task, arm, repeat) in completed(found):
            continue
        used = totals_of(found).plus(exp.carried)
        try:
            if why := reached(exp.limits, used):
                raise Capped(why)
            seen = observed(exp, arm)
            if changed := drift(exp, arm, seen):
                raise Capped(drift_reason(changed))
            why = run_trial(exp, path, task, arm, repeat, used, seen)
        except Capped as e:
            why = str(e)
        if why:
            append(path, {"event": "stop", "why": why, "at": util.iso()})
            print(f"stopped: {why}", flush=True)
            return LIMIT_EXIT
    return 0


def terminate(*_) -> None:
    raise pipeline.Stopped(record.STOPPED)


def check_design(exp: Experiment, path: Path, found: list[dict]) -> None:
    started = next((e for e in found if e.get("event") == "manifest"), None)
    if started and started.get("digest") != exp.digest:
        raise Refused(f"the design changed since {paths.short_path(path)} was started; pass --out for a new file")


def drive(exp: Experiment, path: Path) -> int:
    found = events(path)
    check_design(exp, path, found)
    if not any(e.get("event") == "manifest" for e in found):
        append(path, {"event": "manifest", "digest": exp.digest, "design": exp.design, "engine": engine_commit()})
    append(path, {"event": "session", "at": util.iso(), "limits": exp.limits, "pid": os.getpid(),
                  "engine": engine_commit()})
    settle_interrupted(exp, path)
    if exp.executors and any(e.get("usage_unknown") for e in events(path) if e.get("event") == "trial"):
        raise Refused("workflow usage is unresolved; reconcile retained receipts before authorizing a new output file")
    if done := len(completed(events(path))):
        print(f"{done} of {len(schedule(exp))} trials already done in {paths.short_path(path)}", flush=True)
    try:
        code = work(exp, path)
    except (KeyboardInterrupt, pipeline.Stopped):
        append(path, {"event": "stop", "why": "interrupted", "at": util.iso()})
        code = INTERRUPT_EXIT
    print(render(summary(path)))
    return code


def plan_lines(exp: Experiment, path: Path, label: str = "experiment") -> list[str]:
    found = events(path)
    check_design(exp, path, found)
    grader, accept = exp.design["grader"], exp.accept
    mode = "whole workflow" if exp.executors else f"atom {exp.atom}"
    out = [f"{label} {exp.name} (design {exp.digest}): {mode} on {exp.repo.name}",
           f"results file: {paths.short_path(path)}"]
    for name, s in exp.settings.items():
        extra = "".join(f", {k} {paths.short_path(v) if k == 'prompts' else v[:12]}"
                        for k, v in exp.extras.get(name, {}).items())
        out.append(f"arm {name}: runner {s.runner}, model {s.model}, effort {s.effort or 'default'}, "
                   f"tier {s.tier or 'none'}, timeout {s.timeout_minutes} minutes{extra}")
    out.append(f"grader (fixed): runner {grader['runner']}, model {grader['model']}, "
               f"effort {grader['effort'] or 'default'}")
    for name, t in exp.tasks.items():
        out.append(f"task {name}: {exp.splits[name]}, base {t.base}, graded by {t.graded_by}, "
                   f"{len(t.hidden_files)} hidden files")
    total = len(schedule(exp))
    out += [f"{util.count(total, 'trial')}, {len(completed(found))} done",
            (f"accept: {accept['baseline']} is the baseline on {accept['split']}; at least "
             f"{max(MIN_PAIRS, accept['min_pairs'])} pairs, a difference above {accept['min_difference']:g} at "
             f"{accept['confidence']:g} confidence"),
            "limits: " + ", ".join(f"{k} {exp.limits[k]:g}" for k in LIMIT_KEYS)]
    out += codex_lines(exp.limits, exp.codex)
    return out


def dry_run(exp: Experiment, path: Path) -> int:
    print("\n".join(plan_lines(exp, path)))
    return 0


def exclusive(repo_name: str, job) -> int:
    paths.ensure_dirs()
    with util.lock(run_store.run_lock("experiment", repo_name)) as got:
        if not got:
            print(f"an experiment is already running for {repo_name}")
            return 0
        earlier = signal.signal(signal.SIGTERM, terminate)
        try:
            return job()
        finally:
            signal.signal(signal.SIGTERM, earlier)


def run(manifest: Path, out: str = "", dry: bool = False) -> int:
    exp = load(manifest)
    path = paths.expand(out) if out else default_path(exp.name)
    if dry:
        return dry_run(exp, path)
    return exclusive(exp.repo.name, lambda: drive(exp, path))


def wilson(k: int, n: int, confidence: float) -> list[float] | None:
    if not n:
        return None
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p, spread = k / n, 1 + z * z / n
    centre = (p + z * z / (2 * n)) / spread
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / spread
    return [max(0.0, centre - half), min(1.0, centre + half)]


def sign_p(b: int, c: int) -> float:
    if not b + c:
        return 1.0
    return min(1.0, 2 * sum(math.comb(b + c, i) for i in range(min(b, c) + 1)) / 2 ** (b + c))


def total_of(mine: list[dict], key: str) -> float | None:
    values = [t.get(key) for t in mine]
    return None if any(v is None for v in values) else round(sum(values), 6)


def per_accepted(total: float | None, k: int) -> float | None:
    return None if total is None or not k else round(total / k, 6)


def arm_summary(arm: str, trials: list[dict], confidence: float) -> dict:
    mine = [t for t in trials if t.get("arm") == arm]
    finished = {key_of(t): t for t in mine if t.get("status") == "done"}
    n, k = len(finished), sum(bool(t.get("accepted")) for t in finished.values())
    sums = {key: sum(t.get(key) or 0 for t in mine) for key in ("repairs", "calls", "tokens_in", "tokens_out",
                                                                 "cache_read", "cache_write")}
    seconds = {key: round(sum(t.get(key) or 0 for t in mine), 2) for key in ("api_seconds", "wall_seconds",
                                                                            "wait_seconds")}
    charged, reference = total_of(mine, "charged_usd"), total_of(mine, "reference_usd")
    return {"arm": arm, "attempts": len(mine), "done": n, "accepted": k, "rate": k / n if n else None,
            "interval": wilson(k, n, confidence), **sums, **seconds,
            **({"usage_unknown": any(t.get("usage_unknown") for t in mine)}
               if any("executor" in t for t in mine) else {}), "charged_usd": charged,
            "reference_usd": reference, "charged_per_accepted": per_accepted(charged, k),
            "reference_per_accepted": per_accepted(reference, k)}


def pairing(done: dict, baseline: str, candidate: str, split: str, confidence: float) -> dict:
    keys = sorted(k for k, t in done.items() if k[1] == candidate and t.get("split") == split
                  and (k[0], baseline, k[2]) in done)
    b = sum(bool(done[k].get("accepted") and not done[(k[0], baseline, k[2])].get("accepted")) for k in keys)
    c = sum(bool(done[(k[0], baseline, k[2])].get("accepted") and not done[k].get("accepted")) for k in keys)
    n = len(keys)
    found = {"split": split, "candidate": candidate, "pairs": n, "candidate_only": b, "baseline_only": c,
             "difference": None, "interval": None, "p": sign_p(b, c)}
    if n:
        z = NormalDist().inv_cdf((1 + confidence) / 2)
        difference = (b - c) / n
        margin = z * math.sqrt(max(0.0, (b + c) - (b - c) ** 2 / n)) / n + 1 / n
        found["difference"] = difference
        found["interval"] = [max(-1.0, difference - margin), min(1.0, difference + margin)]
    return found


def judge(pair: dict, accept: dict) -> dict:
    needed = max(MIN_PAIRS, accept["min_pairs"])
    if pair["pairs"] < needed:
        return {"state": "tiny", "text": f"no winner: {pair['pairs']} pairs is a tiny sample (needs {needed})"}
    low, high = pair["interval"]
    floor = accept["min_difference"]
    if (low <= floor and high >= -floor) or pair["p"] > 1 - accept["confidence"]:
        return {"state": "overlap", "text": "no winner: the uncertainty overlaps"}
    if low > floor:
        return {"state": "wins", "text": f"{pair['candidate']} beats {accept['baseline']}"}
    return {"state": "holds", "text": f"{accept['baseline']} holds against {pair['candidate']}"}


def overall(verdicts: dict[str, dict], baseline: str) -> str:
    winners = [arm for arm, v in verdicts.items() if v["state"] == "wins"]
    if len(winners) == 1:
        return f"winner: {winners[0]} over {baseline}"
    if winners:
        return f"no single winner: {', '.join(winners)} each beat {baseline}"
    for state_name in ("tiny", "overlap"):
        if found := next((v["text"] for v in verdicts.values() if v["state"] == state_name), ""):
            return found
    return f"no winner: {baseline} holds"


def summary(path: Path) -> dict:
    found = events(path)
    manifest = next((e for e in found if e.get("event") == "manifest"), None)
    if manifest is None:
        raise Refused(f"{paths.short_path(path)} has no manifest line")
    design = manifest["design"]
    accept = design["accept"]
    trials = [e for e in found if e.get("event") == "trial"]
    if design.get("executor") == "workflow":
        settled = {t["run"] for t in trials}
        trials += [{**e, "status": "pending", "usage_unknown": True, "charged_usd": None, "reference_usd": None,
                    "executor": design["arms"][e["arm"]]["executor"]}
                   for e in found if e.get("event") == "start" and e["run"] not in settled]
    done = {key_of(t): t for t in trials if t.get("status") == "done"}
    baseline = accept["baseline"]
    splits = [s for s in SPLITS if s in {t["split"] for t in design["tasks"].values()}]
    candidates = [arm for arm in design["arms"] if arm != baseline]
    pairs = {s: [pairing(done, baseline, arm, s, accept["confidence"]) for arm in candidates] for s in splits}
    verdicts = {p["candidate"]: judge(p, accept) for p in pairs[accept["split"]]}
    planned = len(design["tasks"]) * design["repeats"] * len(design["arms"])
    return {"name": design["name"], "digest": manifest["digest"], "atom": design.get("atom", ""), "accept": accept,
            **({"executor": design["executor"]} if "executor" in design else {}),
            "planned": planned, "done": len(done), "drifted": sum(t.get("status") == "drifted" for t in trials),
            "arms": {arm: arm_summary(arm, trials, accept["confidence"]) for arm in design["arms"]},
            "pairs": pairs, "verdict": {"split": accept["split"], "candidates": verdicts,
                                        "text": overall(verdicts, baseline)}}


def money(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"${value:,.2f}" if value >= 1 else "$" + f"{value:.4f}".rstrip("0").rstrip(".")


def percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def spread(interval: list[float] | None, shown=percent) -> str:
    return f"{shown(interval[0])} to {shown(interval[1])}" if interval else "none"


def signed(value: float) -> str:
    return f"{value:+.2f}"


def cost_line(label: str, total: float | None, each: float | None, accepted: int) -> str:
    per = f"{money(each)} per accepted task" if accepted else "no accepted task"
    return f"  {label}: {money(total)} over every attempt, {per}"


def render_arm(a: dict, confidence: float, baseline: str) -> list[str]:
    rate = f"{percent(a['rate'])}, {percent(confidence)} interval {spread(a['interval'])}" if a["done"] \
        else "no done trial"
    return [(f"arm {a['arm']}{' (baseline)' if a['arm'] == baseline else ''}: accepted {a['accepted']} of "
             f"{a['done']} done trials ({rate}), {a['attempts']} attempts"),
            (f"  repairs {a['repairs']}, calls {a['calls']}, tokens {a['tokens_in']} in and {a['tokens_out']} out, "
             f"cache {a['cache_read']} read and {a['cache_write']} written"
             f"{'; known usage only, outstanding receipts need reconciliation' if a.get('usage_unknown') else ''}"),
            f"  seconds: api {a['api_seconds']:g}, wall {a['wall_seconds']:g}, wait {a['wait_seconds']:g}",
            cost_line("charged", a["charged_usd"], a["charged_per_accepted"], a["accepted"]),
            cost_line("reference", a["reference_usd"], a["reference_per_accepted"], a["accepted"])]


def render_pair(p: dict, baseline: str, confidence: float) -> str:
    head = f"  {p['candidate']} against {baseline}: {util.count(p['pairs'], 'pair')}"
    if not p["pairs"]:
        return head
    return (f"{head}, {p['candidate']} only {p['candidate_only']}, {baseline} only {p['baseline_only']}, "
            f"difference {signed(p['difference'])} ({percent(confidence)} interval {spread(p['interval'], signed)}), "
            f"sign test p {p['p']:.3f}")


def render(found: dict) -> str:
    accept = found["accept"]
    baseline, confidence = accept["baseline"], accept["confidence"]
    mode = "whole workflow" if found.get("executor") == "workflow" else f"atom {found['atom']}"
    out = [f"experiment {found['name']} (design {found['digest']}), {mode}",
           f"{found['done']} of {found['planned']} trials done",
           f"{util.count(found['drifted'], 'drifted trial')} left out of rates and pairs"]
    for a in found["arms"].values():
        out += render_arm(a, confidence, baseline)
    for split, pairs in found["pairs"].items():
        deciding = split == accept["split"]
        out.append(f"pairs on {split}" + ("" if deciding else " (does not decide)") + ":")
        out += [render_pair(p, baseline, confidence) for p in pairs]
        if deciding:
            out += [f"  {arm}: {v['text']}" for arm, v in found["verdict"]["candidates"].items()]
    out += [f"verdict on {found['verdict']['split']}: {found['verdict']['text']}", NO_SELECTION]
    return "\n".join(out)


def show(path: Path, as_json: bool) -> int:
    found = summary(path)
    print(json.dumps(found) if as_json else render(found))
    return 0
