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
    codebase,
    config,
    configrepo,
    evals,
    paths,
    pins,
    pipeline,
    prices,
    prompts,
    record,
    replay,
    routing,
    run_lines,
    runner,
    settings,
    slots,
    steps,
    util,
    workspace,
)
from ..agent import traits_of
from ..diffguard import denied_paths
from ..store import blobs as blob_store
from ..store import runs as run_store
from ..store import sources as source_store
from ..util import engine_commit

FIXED_GRADERS = ("verify", "eval_grade")
TRIAL_ROLES = ("plan", "build", "check_repair")
SPLITS = ("train", "validation", "holdout")
TOP_KEYS = ("name", "repo", "atom", "repeats", "cache_policy", "vary", "arms", "tasks", "accept", "limits")
EXTRA_KEYS = ("prompts", "context", "settings", "schemas", "adws", "routes", "files")
ARM_KEYS = ("tier", "runner", "provider", "model", "effort", "tools", "mcp", "instructions",
            "timeout_minutes", "tries", "prompt", "schema", *EXTRA_KEYS)
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
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
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
    vary: tuple[str, ...] | None = None
    engine_sha: str = ""
    arm_refs: dict[str, str] = field(default_factory=dict)
    arm_commits: dict[str, str] = field(default_factory=dict)
    arm_bases: dict[str, str] = field(default_factory=dict)
    arm_resolutions: dict[str, pins.Resolution] = field(default_factory=dict)
    grader_pins: dict[str, object] = field(default_factory=dict)


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
    found: dict[str, object] = {}
    if "prompts" in entry:
        raw_prompts = entry["prompts"]
        if isinstance(raw_prompts, str):
            folder = paths.expand(text(entry, "prompts", f"arms.{name}."))
            if missing := evals.Prompts(folder).missing():
                raise Refused(f"arms.{name}: prompts {paths.short_path(folder)} has no {', '.join(missing)}")
            found["prompts"] = str(folder)
        elif isinstance(raw_prompts, dict) and raw_prompts:
            prompt_map: dict[str, str] = {}
            for pname, pval in raw_prompts.items():
                stem = Path(str(pname)).stem
                if not prompts.NAME_RE.fullmatch(stem):
                    raise Refused(f"arms.{name}.prompts.{pname}: invalid prompt name")
                if not isinstance(pval, str) or not pval.strip():
                    raise Refused(f"arms.{name}.prompts.{pname}: must be a non-empty string or file path")
                candidate = paths.expand(pval) if ("\n" not in pval and len(pval) < 512) else None
                if candidate is not None and candidate.is_file():
                    prompt_map[stem] = candidate.read_text(encoding="utf-8")
                else:
                    prompt_map[stem] = pval
            found["prompts"] = prompt_map
        else:
            raise Refused(f"arms.{name}.prompts must be a folder path or table of prompt files")
    if "context" in entry:
        found["context"] = commit_of(repo, f"arms.{name}: context", text(entry, "context", f"arms.{name}."))
    if "settings" in entry:
        raw_settings = entry["settings"]
        if not isinstance(raw_settings, dict):
            raise Refused(f"arms.{name}.settings must be a table of step settings")
        step_map: dict[str, dict] = {}
        for role_name, table in raw_settings.items():
            canonical = config.STEP_ALIASES.get(role_name, role_name)
            if canonical not in config.STEP_ROLES:
                raise Refused(f"arms.{name}.settings.{role_name}: unknown step role")
            if canonical in FIXED_GRADERS:
                raise Refused(f"arms.{name}.settings.{role_name}: the grader stays fixed")
            try:
                step_map[canonical] = config.step_table(table, f"arms.{name}.settings.{role_name}")
            except ValueError as e:
                raise Refused(f"arm {name}: {e}") from e
        if step_map:
            found["settings"] = step_map
    for map_key in ("schemas", "adws", "routes", "files"):
        if map_key in entry:
            raw_map = entry[map_key]
            if not isinstance(raw_map, dict):
                raise Refused(f"arms.{name}.{map_key} must be a table")
            found[map_key] = dict(raw_map)
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
        extra = read_extras(repo, name, entry)
        try:
            flat = config.step_table({k: v for k, v in entry.items() if k not in EXTRA_KEYS}, f"arms.{name}")
            extra_atom = (extra.get("settings") or {}).get(atom, {})
            given[name] = {**flat, **extra_atom}
            resolved[name] = steps.resolve(cfg, layer, atom, "", given[name])
        except ValueError as e:
            raise Refused(f"arm {name}: {e}") from e
        if extra:
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
    return {"prompt": sha(*[prompts.body(prompts_dir / f"{n}.md") for n in evals.BUILDER_PROMPTS]),
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
    return [r for r, b in config.INHERITS.items() if b == atom] if follow else []


def freeze(cfg: config.Config, layer: config.RepoLayer, atom: str, baseline: dict,
           follow: bool = False) -> dict[str, dict]:
    route = {k: v for k, v in baseline.items() if k in ROUTE_KEYS}
    return {role: route for role in TRIAL_ROLES
            if role != atom and role not in followers(atom, follow) and route
            and not steps.resolve(cfg, layer, role, "").model}


def evidence(task: evals.Task) -> str:
    hidden = [part for n in sorted(task.hidden_files) for part in (n, task.hidden_files[n])]
    return sha(task.title, task.prompt, task.check, task.verify, *hidden)


def read_vary(data: dict) -> tuple[str, ...] | None:
    if "vary" not in data:
        return None
    raw = data["vary"]
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise Refused("vary must be a list of non-empty resolved.json key paths")
    return tuple(x.strip() for x in raw)


def config_manifest_path(name: str, *, config_commit: str = "") -> Path:
    slug = name.removeprefix("experiment:")
    if not SLUG.fullmatch(slug):
        raise Refused(f"experiment name {name!r} must match {SLUG.pattern}")
    if configrepo.is_repo():
        commit = config_commit or configrepo.live_sha()
        if commit:
            return pins.config_checkout(commit) / "experiments" / f"{slug}.toml"
    return paths.CONFIG_DIR / "experiments" / f"{slug}.toml"


def commit_manifest(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    name = text(data, "name")
    if not SLUG.fullmatch(name):
        raise Refused(f"name {name!r} must match {SLUG.pattern}")
    rel = f"experiments/{name}.toml"
    if not configrepo.is_repo():
        try:
            configrepo.init()
        except configrepo.ConfigRepoError:
            return name
    _, base = configrepo.read(rel)
    configrepo.commit({rel: raw}, {rel: base}, actor="hand", source="hand", note=f"hand edit: {rel}")
    return name


def load_from_config(name: str, carried: Totals | None = None, follow: bool = False) -> Experiment:
    manifest_file = config_manifest_path(name)
    if not manifest_file.is_file():
        raise Refused(f"no experiment manifest at {paths.short_path(manifest_file)}")
    return designed(tomllib.loads(manifest_file.read_text(encoding="utf-8")), carried, follow)


def load(path: Path) -> Experiment:
    return designed(tomllib.loads(path.read_text(encoding="utf-8")))


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(
            f"{k if re.fullmatch(r'[A-Za-z0-9_-]+', str(k)) else json.dumps(str(k))} = {_toml_value(v)}"
            for k, v in value.items()
        )
        return "{ " + items + " }"
    return json.dumps(str(value), ensure_ascii=False)


def _collapse_toml_key(lines: list[str], table: str, key: str) -> None:
    start, end = settings.table_span(lines, table)
    if start is None:
        return
    for i in range(start, end):
        if not (m := settings.key_line(key).match(lines[i])):
            continue
        for j in range(i, end):
            try:
                tomllib.loads("".join(lines[i:j + 1]))
            except tomllib.TOMLDecodeError:
                continue
            lines[i:j + 1] = [f"{m.group(1)}[]{m.group(3) or chr(10)}"]
            return
        return


def _step_table_name(raw: dict, role: str) -> str:
    held = raw.get("steps", {})
    alias = next((a for a, target in config.STEP_ALIASES.items() if target == role and a in held), "")
    return f"steps.{alias or role}"


def edit_step_toml(text_in: str, role_updates: dict[str, dict]) -> str:
    raw = tomllib.loads(text_in) if text_in.strip() else {}
    lines = text_in.splitlines(keepends=True)
    for role, keys in role_updates.items():
        if not keys:
            continue
        table = _step_table_name(raw, role)
        for key, value in keys.items():
            _collapse_toml_key(lines, table, key)
            if value is None:
                settings.drop_key(lines, table, key)
            else:
                settings.set_key(lines, table, key, _toml_value(value))
    return "".join(lines)


def arm_ref_name(exp_name: str, arm: str) -> str:
    return f"refs/arete/arms/{exp_name}/{arm}"


def _arm_prompt_bytes(stem: str, raw_text: str) -> bytes:
    prepared = prompts.ensure_override_front_matter(stem, raw_text)
    return prepared.encode("utf-8")


def arm_file_changes(exp: Experiment, arm: str, live_cfg_text: str) -> dict[str, bytes]:
    changes: dict[str, bytes] = {}
    extra = exp.extras.get(arm, {})
    step_updates: dict[str, dict] = {}
    if exp.arms.get(arm):
        step_updates[exp.atom] = dict(exp.arms[arm])
    if isinstance(extra.get("settings"), dict):
        for role, tbl in extra["settings"].items():
            step_updates[role] = {**step_updates.get(role, {}), **tbl}
    if step_updates:
        updated_cfg = edit_step_toml(live_cfg_text, step_updates)
        if updated_cfg != live_cfg_text:
            changes["config.toml"] = updated_cfg.encode("utf-8")
    raw_prompts = extra.get("prompts")
    if isinstance(raw_prompts, str):
        folder = Path(raw_prompts)
        for file in sorted(folder.glob("*.md")):
            stem = file.stem
            if stem not in prompts.engine_prompt_names():
                continue
            raw_bytes = file.read_bytes()
            live_bytes = configrepo.live_bytes(f"prompts/{file.name}")
            engine_file = paths.PROMPTS_DIR / file.name
            engine_bytes = engine_file.read_bytes() if engine_file.is_file() else b""
            if raw_bytes == engine_bytes and live_bytes is None:
                continue
            if live_bytes is not None and raw_bytes == live_bytes:
                continue
            changes[f"prompts/{file.name}"] = _arm_prompt_bytes(stem, raw_bytes.decode("utf-8"))
    elif isinstance(raw_prompts, dict):
        for stem, text_val in sorted(raw_prompts.items()):
            changes[f"prompts/{stem}.md"] = _arm_prompt_bytes(stem, str(text_val))
    if isinstance(extra.get("schemas"), dict):
        for sname, sval in sorted(extra["schemas"].items()):
            stem = Path(str(sname)).stem
            raw_s = sval if isinstance(sval, str) else json.dumps(sval, indent=2) + "\n"
            changes[f"schemas/{stem}.json"] = raw_s.encode("utf-8")
    if isinstance(extra.get("adws"), dict):
        for aname, aval in sorted(extra["adws"].items()):
            stem = Path(str(aname)).stem
            changes[f"adws/{stem}.toml"] = str(aval).encode("utf-8")
    if isinstance(extra.get("routes"), dict):
        for rname, rval in sorted(extra["routes"].items()):
            stem = Path(str(rname)).stem
            changes[f"routes/{stem}.toml"] = str(rval).encode("utf-8")
    if isinstance(extra.get("files"), dict):
        for rel, fval in sorted(extra["files"].items()):
            changes[str(rel)] = fval if isinstance(fval, bytes) else str(fval).encode("utf-8")
    return changes


def _matches_declared(key: str, declared: set[str]) -> bool:
    return key in declared or any(key.startswith(prefix + ".") for prefix in declared)


def inferred_vary(exp: Experiment) -> set[str]:
    keys: set[str] = set()
    for arm, vals in exp.arms.items():
        for k in vals:
            keys.add(f"settings.{exp.atom}.{k}")
        if exp.atom and vals:
            for rk in ("model", "runner", "provider", "tier"):
                if any(k in ROUTE_KEYS for k in vals):
                    keys.add(f"settings.{exp.atom}.{rk}")
        extra = exp.extras.get(arm, {})
        if isinstance(extra.get("settings"), dict):
            for role, tbl in extra["settings"].items():
                for k in tbl:
                    keys.add(f"settings.{role}.{k}")
                if any(k in ROUTE_KEYS for k in tbl):
                    for rk in ROUTE_KEYS:
                        keys.add(f"settings.{role}.{rk}")
        if "prompts" in extra:
            raw_p = extra["prompts"]
            if isinstance(raw_p, dict):
                for stem in raw_p:
                    keys.add(f"prompts.{stem}")
            else:
                keys.add("prompts")
        if "context" in extra:
            keys.add("harness")
        if isinstance(extra.get("schemas"), dict):
            for sname in extra["schemas"]:
                keys.add(f"schemas.{Path(str(sname)).stem}")
        if isinstance(extra.get("adws"), dict):
            for aname in extra["adws"]:
                keys.add(f"adws.{Path(str(aname)).stem}")
        if isinstance(extra.get("routes"), dict):
            for rname in extra["routes"]:
                keys.add(f"routes.{Path(str(rname)).stem}")
        if isinstance(extra.get("files"), dict):
            keys.update(("settings", "prompts", "schemas", "adws", "routes"))
    return keys


def diff_resolved_docs(
    expected_doc: dict,
    actual_doc: dict,
    *,
    ignore_declared: set[str] | None = None,
    map_grader: bool = False,
) -> list[str]:
    declared = ignore_declared or set()
    changed: list[str] = []

    def add(key: str) -> None:
        if key not in changed:
            changed.append(key)

    if expected_doc.get("engine_sha") != actual_doc.get("engine_sha") and not _matches_declared("engine_sha", declared):
        add("engine_sha")

    exp_settings = expected_doc.get("settings") or {}
    act_settings = actual_doc.get("settings") or {}
    for role in sorted(set(exp_settings) | set(act_settings)):
        e_role = exp_settings.get(role) or {}
        a_role = act_settings.get(role) or {}
        for field_name in sorted(set(e_role) | set(a_role)):
            e_entry = e_role.get(field_name) if isinstance(e_role, dict) else None
            a_entry = a_role.get(field_name) if isinstance(a_role, dict) else None
            e_val = e_entry.get("value") if isinstance(e_entry, dict) else e_entry
            a_val = a_entry.get("value") if isinstance(a_entry, dict) else a_entry
            if e_val != a_val:
                dotted = f"settings.{role}.{field_name}"
                if _matches_declared(dotted, declared):
                    continue
                base_role = config.INHERITS.get(role, "")
                a_level = a_entry.get("level") if isinstance(a_entry, dict) else ""
                e_level = e_entry.get("level") if isinstance(e_entry, dict) else ""
                if base_role and "inherit" in (a_level, e_level) and _matches_declared(f"settings.{base_role}.{field_name}", declared):
                    continue
                add(dotted)
                if map_grader and role == "eval_grade":
                    add("grader")

    for section_name in ("prompts", "schemas", "adws", "routes"):
        e_sec = expected_doc.get(section_name) or {}
        a_sec = actual_doc.get(section_name) or {}
        for item_name in sorted(set(e_sec) | set(a_sec)):
            e_meta = e_sec.get(item_name) or {}
            a_meta = a_sec.get(item_name) or {}
            e_sha = e_meta.get("sha256") if isinstance(e_meta, dict) else e_meta
            a_sha = a_meta.get("sha256") if isinstance(a_meta, dict) else a_meta
            if e_sha != a_sha:
                dotted = f"{section_name}.{item_name}"
                if _matches_declared(dotted, declared):
                    continue
                add(dotted)
                if map_grader and section_name == "prompts" and item_name == "grade":
                    add("grader")

    for scalar in ("adw", "adw_version", "adw_rule"):
        if expected_doc.get(scalar) != actual_doc.get(scalar) and not _matches_declared(scalar, declared):
            add(scalar)

    def _harness_content(doc: dict) -> dict | None:
        h = doc.get("harness")
        if not isinstance(h, dict):
            return None
        return {k: v for k, v in h.items() if k != "label"}

    if _harness_content(expected_doc) != _harness_content(actual_doc) and not _matches_declared("harness", declared):
        add("harness")
    else:
        e_exp = (expected_doc.get("expertise") or {}).get("sha256") if isinstance(expected_doc.get("expertise"), dict) else None
        a_exp = (actual_doc.get("expertise") or {}).get("sha256") if isinstance(actual_doc.get("expertise"), dict) else None
        if e_exp != a_exp and not _matches_declared("harness", declared):
            add("harness")

    e_prices = expected_doc.get("prices")
    a_prices = actual_doc.get("prices")
    if e_prices != a_prices and not _matches_declared("prices", declared):
        add("prices")

    return changed


def _resolve_arm(
    exp: Experiment,
    arm: str,
    arm_ref_or_commit: str,
    *,
    engine_sha: str,
) -> pins.Resolution:
    extra = exp.extras.get(arm, {})
    src = evals.Source(exp.repo, str(extra.get("context") or ""))
    return pins.resolve(
        "experiment",
        repo=exp.repo,
        layer=exp.layer,
        overrides=exp.frozen or None,
        arm=arm_ref_or_commit,
        harness_source=src,
        engine_sha=engine_sha,
    )


def prepare_arms(
    exp: Experiment,
    *,
    reuse_commits: dict[str, str] | None = None,
    engine_sha: str = "",
) -> dict[str, pins.Resolution]:
    configrepo.ensure_repo()
    cur_engine = engine_sha or codebase.current_stamp()
    live_base = configrepo.live_sha()
    live_cfg_text = (configrepo.live_bytes("config.toml") or b"").decode("utf-8")
    baseline = exp.accept["baseline"]
    refs: dict[str, str] = {}
    commits: dict[str, str] = {}
    bases: dict[str, str] = {}
    resolutions: dict[str, pins.Resolution] = {}

    for arm in exp.arms:
        ref = arm_ref_name(exp.name, arm)
        refs[arm] = ref
        if reuse_commits and arm in reuse_commits and configrepo.resolve_ref(reuse_commits[arm]):
            commit_sha = reuse_commits[arm]
            base_sha = configrepo.parent_sha(commit_sha) or live_base
        else:
            changes = {} if (arm == baseline and not exp.arms[arm] and not exp.extras.get(arm)) else arm_file_changes(
                exp, arm, live_cfg_text
            )
            try:
                commit_sha = configrepo.commit(
                    changes,
                    {},
                    actor="engine",
                    source="engine",
                    note=f"experiment {exp.name} arm {arm}",
                    ref=ref,
                    allow_empty=True,
                )
            except configrepo.ConfigRepoError as e:
                raise Refused(f"arm {arm}: {e}") from e
            base_sha = live_base
        commits[arm] = commit_sha
        bases[arm] = base_sha
        target_ref = commit_sha if (reuse_commits and arm in reuse_commits) else ref
        resolutions[arm] = _resolve_arm(exp, arm, target_ref, engine_sha=cur_engine)

    base_doc = json.loads(resolutions[baseline].resolved_bytes)
    declared = set(exp.vary) if exp.vary is not None else inferred_vary(exp)
    for arm, res in resolutions.items():
        if arm == baseline:
            continue
        arm_doc = json.loads(res.resolved_bytes)
        extra_diffs = diff_resolved_docs(base_doc, arm_doc, ignore_declared=declared)
        if extra_diffs:
            raise Refused(f"arm {arm} changes undeclared key(s): {', '.join(extra_diffs)}")

    exp.engine_sha = cur_engine
    exp.arm_refs = refs
    exp.arm_commits = commits
    exp.arm_bases = bases
    exp.arm_resolutions = resolutions
    grade_meta = resolutions[baseline].prompts.get("grade") or {}
    exp.grader_pins = {
        "prompt": dict(grade_meta),
        "settings_sha": resolutions[baseline].settings_sha_for("eval_grade"),
        "verify": exp.design["grader"]["verify"],
    }
    return resolutions


def designed(data: dict, carried: Totals | None = None, follow: bool = False) -> Experiment:
    if "executor" in data:
        from . import workflows

        return workflows.designed(data, carried, follow)
    unknown(data, TOP_KEYS, "the manifest")
    name = text(data, "name")
    if not SLUG.fullmatch(name):
        raise Refused("name must match ^[a-z0-9][a-z0-9-]{0,63}$")
    raw_atom = text(data, "atom", "", "build")
    atom = config.STEP_ALIASES.get(raw_atom, raw_atom)
    if atom in FIXED_GRADERS:
        raise Refused(f"atom {raw_atom!r}: the grader stays fixed")
    if atom not in config.STEP_ROLES:
        raise Refused(f"atom must be a known step role, not {raw_atom!r}")
    vary = read_vary(data)
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
    given, resolved_settings, extras = read_arms(cfg, layer, atom, section(data, "arms"), repo)
    tasks, splits = read_tasks(repo, section(data, "tasks"))
    accept = read_accept(section(data, "accept"), given, splits)
    limits = read_limits(section(data, "limits"))
    frozen = freeze(cfg, layer, atom, given[accept["baseline"]], follow)
    following = followers(atom, follow)
    held = {role: steps.resolve(cfg, layer, role, "", frozen.get(role)) for role in TRIAL_ROLES
            if role != atom and role not in following}
    inherited = {n: {role: steps.resolve(cfg, layer, role, "", None, given[n]) for role in following}
                 for n in resolved_settings}
    routes = unstoppable(
        cfg, atom, resolved_settings,
        {**held, **{f"{role} of arm {n}": s for n, roles in inherited.items() for role, s in roles.items()}},
        grader, limits)
    design = {
        "name": name, "repo": repo.name, "atom": atom, "repeats": repeats, "cache_policy": cache_policy,
        **({"vary": list(vary)} if vary is not None else {}),
        "accept": accept,
        "arms": {n: {**given[n], "runner": s.runner, "model": s.model, "provider": s.provider, "tier": s.tier,
                     "effort": s.effort, "settings": effective(cfg, s),
                     **{k: (paths.short_path(v) if k == "prompts" and isinstance(v, str) else v)
                        for k, v in extras.get(n, {}).items()},
                     **({"inheritors": {r: effective(cfg, s2) for r, s2 in inherited[n].items()}} if follow else {})}
                 for n, s in resolved_settings.items()},
        "frozen": {role: {"overrides": frozen.get(role, {}), "settings": effective(cfg, s)}
                   for role, s in held.items()},
        "tasks": {n: {"split": splits[n], "base": t.base, "evidence": evidence(t)} for n, t in tasks.items()},
        "grader": {"runner": grader.runner, "model": grader.model, "effort": grader.effort,
                   "settings": effective(cfg, grader), "verify": sha_json(layer.verify.policy()) if layer.verify else "",
                   "prompt": sha(prompts.body(paths.PROMPTS_DIR / "grade.md"))},
        "engine": engine_code(),
        "money": {"prices": (prices.load() or {}).get("version", ""),
                  "local_providers": sorted(cfg.local_providers)},
        **({HEADROOM: limits[HEADROOM]} if HEADROOM in limits else {}),
    }
    digest = sha_json(design)[:12]
    return Experiment(name, cfg, repo, layer, atom, repeats, cache_policy, given, resolved_settings, tasks, splits,
                      accept, limits, frozen, design, digest, extras, carried or Totals(), follow, codex=routes,
                      vary=vary)


def drift_before(exp: Experiment, arm: str) -> tuple[list[str], pins.Resolution]:
    if arm not in exp.arm_resolutions:
        prepare_arms(exp)
    cur_engine = codebase.current_stamp()
    arm_target = exp.arm_commits.get(arm) or exp.arm_refs[arm]
    pin_res = _resolve_arm(exp, arm, arm_target, engine_sha=cur_engine)
    start_res = exp.arm_resolutions[arm]
    start_doc = json.loads(start_res.resolved_bytes)
    cur_doc = json.loads(pin_res.resolved_bytes)
    changed = diff_resolved_docs(start_doc, cur_doc, ignore_declared=None, map_grader=True)
    if exp.executors:
        from . import workflows

        layer = pin_res.layer or exp.layer
        if workflows.fingerprint(pin_res.cfg, layer) != exp.design.get("workflow_sha"):
            if "workflow" not in changed:
                changed.append("workflow")
    return changed, pin_res


def drift_after(run_id: str, pin_res: pins.Resolution) -> list[str]:
    changed: list[str] = []
    after_engine = codebase.current_stamp()
    if after_engine != pin_res.engine_sha:
        changed.append("engine_sha")
    resolved_doc = json.loads(pin_res.resolved_bytes)
    expected_prompts = resolved_doc.get("prompts") or {}
    for rows in run_store.step_inputs_by_run(run_id).values():
        for inp in rows:
            if inp.get("role") != "prompt":
                continue
            pname = str(inp.get("name") or "")
            expected_id = str((expected_prompts.get(pname) or {}).get("id") or "")
            if expected_id and inp.get("version") != expected_id:
                key = f"prompts.{pname}"
                if key not in changed:
                    changed.append(key)
                if pname == "grade" and "grader" not in changed:
                    changed.append("grader")
    return changed


def drift_reason(changed: list[str]) -> str:
    return (f"the {', '.join(changed)} changed since the experiment started; "
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
               *, resolved_sha: str = "", engine_sha: str = "", config_commit: str = "") -> dict:
    task, arm = begun["task"], begun["arm"]
    used = usage(exp, arm, begun["run"])
    if (outcomes or {}).get("usage_unknown"):
        used.update(charged_usd=None, reference_usd=None,
                    reference_unknown_calls=max(1, used["reference_unknown_calls"]))
    used["wait_seconds"] = round(used["wait_seconds"] + slot_seconds(events(path), begun), 2)
    run_row = run_store.run_get(begun["run"]) or {}
    r_sha = resolved_sha or begun.get("resolved_sha") or str(run_row.get("resolved_sha") or "")
    e_sha = engine_sha or begun.get("engine_sha") or str(run_row.get("engine_sha") or "")
    c_sha = config_commit or begun.get("config_commit") or str(run_row.get("config_commit") or "")
    return {"event": "trial", "at": util.iso(), "task": task, "split": exp.splits[task],
            "base": exp.tasks[task].base, "arm": arm, "repeat": begun["repeat"], "attempt": begun["attempt"],
            "run": begun["run"], "status": status, "accepted": accepted, "failures": failures or [],
            "repairs": repairs, "resolved_sha": r_sha, "engine_sha": e_sha,
            **({"config_commit": c_sha} if c_sha else {}),
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


def take_slot(exp: Experiment, path: Path, waiting: dict, used: Totals, *,
              resolution=None, arm: str = "") -> tuple[str, TextIO, str, float]:
    tag = f"{exp.name}-{waiting['task']}-{waiting['arm']}-{waiting['repeat']}"
    began, noted, since, told = time.monotonic(), time.monotonic(), "", ""
    while True:
        with slots.admitting():
            free = slots.slots_free_to_start(slots.live_max_parallel(exp.cfg.max_parallel))
            passed = source_store.tickets_passed_over()
            if free > 0 and not passed:
                extra_fields = {"arm": arm} if arm else {}
                run_id = record.start("experiment", tag=tag, lock=run_store.run_lock("experiment", exp.repo.name),
                                      repo=exp.repo.name, resolution=resolution, **extra_fields)
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


def run_trial(exp: Experiment, path: Path, task: str, arm: str, repeat: int, used: Totals,
              seen: dict | None = None) -> str:
    if arm not in exp.arm_resolutions:
        prepare_arms(exp)
    changed, pin_res = drift_before(exp, arm)
    if changed:
        return drift_reason(changed)

    found = events(path)
    attempt = 1 + max((e["attempt"] for e in found if e.get("event") == "start" and key_of(e) == (task, arm, repeat)),
                      default=0)
    waiting = {"event": "wait", "task": task, "arm": arm, "repeat": repeat, "attempt": attempt}
    extra = exp.extras.get(arm, {})
    raw_prompts = extra.get("prompts")
    trial_prompts = (
        evals.Prompts(Path(raw_prompts), paths.short_path(raw_prompts))
        if isinstance(raw_prompts, str)
        else evals.Prompts()
    )
    src = evals.Source(exp.repo, str(extra.get("context") or ""))
    run_id, held, since, waited = take_slot(
        exp, path, waiting, used, resolution=pin_res, arm=f"{exp.name}:{arm}",
    )
    try:
        with record.failed_on_crash(run_id), pins.use(pin_res):
            if since:
                run_lines.waited(run_id, "slot", since, util.iso())
            begun = {"event": "start", "task": task, "arm": arm, "repeat": repeat, "attempt": attempt,
                     "run": run_id, "at": util.iso(), "resolved_sha": pin_res.resolved_sha,
                     "engine_sha": pin_res.engine_sha, "config_commit": pin_res.config_commit}
            append(path, begun)
            started = time.monotonic()
            overrides = exp.frozen if pin_res.config_commit else trial_overrides(exp, arm)
            given = {"id": run_id, "repo": pin_res.repo or exp.repo, "dir": paths.RUNS_DIR / run_id,
                     "cfg": pin_res.cfg, "task": task,
                     "trial": f"{arm}-{repeat}", "step_overrides": overrides, "limits": exp.limits,
                     "pinned": pin_res.layer or exp.layer, "follow": exp.follow, "reviews": exp.reviews,
                     "checks_required": exp.checks_required, "before": replace(used, seconds=used.seconds + waited)}
            trial = exp.make_run(exp, arm, given) if exp.make_run else TrialRun(**given)
            trial.dir.mkdir(parents=True, exist_ok=True)
            try:
                with alive(path, run_id, started), replay_folder(exp, task, arm, repeat):
                    result = evals.run_task(pin_res.cfg, src, exp.tasks[task], trial_prompts, run=trial)
            except (KeyboardInterrupt, pipeline.Stopped):
                record.settle(run_id, "stopped", summary=record.STOPPED)
                append(path, trial_line(exp, path, begun, "interrupted", time.monotonic() - started,
                                        outcomes=trial.outcomes(), resolved_sha=pin_res.resolved_sha,
                                        engine_sha=pin_res.engine_sha, config_commit=pin_res.config_commit))
                raise
            failures = result.failures or ([result.why] if result.why else [])
            changed = drift_after(run_id, pin_res)
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
                                     **(overrun(exp.limits, trial.used()) if stopped_by else {})},
                                    resolved_sha=pin_res.resolved_sha, engine_sha=pin_res.engine_sha,
                                    config_commit=pin_res.config_commit))
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
            why = run_trial(exp, path, task, arm, repeat, used)
        except Capped as e:
            why = str(e)
        if why:
            append(path, {"event": "stop", "why": why, "at": util.iso()})
            print(f"stopped: {why}", flush=True)
            return LIMIT_EXIT
    return 0


def terminate(*_) -> None:
    raise pipeline.Stopped(record.STOPPED)


def check_resume(exp: Experiment, path: Path, found: list[dict], *, prepare: bool = True) -> None:
    started = next((e for e in found if e.get("event") == "manifest"), None)
    if started is None:
        if prepare and not exp.arm_resolutions:
            prepare_arms(exp)
        return
    rec_arms = started.get("arms") if isinstance(started.get("arms"), dict) else {}
    rec_engine = str(started.get("engine_sha") or "")
    rec_grader = started.get("grader") if isinstance(started.get("grader"), dict) else {}
    if prepare:
        reuse = {
            arm: str(info["commit"])
            for arm, info in rec_arms.items()
            if isinstance(info, dict) and info.get("commit")
        }
        prepare_arms(exp, reuse_commits=reuse or None)
    problems: list[str] = []

    def add(item: str) -> None:
        if item not in problems:
            problems.append(item)

    if rec_engine and exp.engine_sha and exp.engine_sha != rec_engine:
        add("engine_sha")
    if rec_grader and exp.grader_pins:
        rec_gp = (rec_grader.get("prompt") or {}).get("sha256") if isinstance(rec_grader.get("prompt"), dict) else ""
        cur_gp = (exp.grader_pins.get("prompt") or {}).get("sha256") if isinstance(exp.grader_pins.get("prompt"), dict) else ""
        if (rec_gp and cur_gp and rec_gp != cur_gp) or (
            rec_grader.get("settings_sha") and exp.grader_pins.get("settings_sha") != rec_grader.get("settings_sha")
        ) or (
            rec_grader.get("verify") is not None and exp.grader_pins.get("verify") != rec_grader.get("verify")
        ):
            add("grader")
    for arm, info in rec_arms.items():
        if not isinstance(info, dict):
            continue
        rec_sha = str(info.get("resolved_sha") or "")
        cur_res = exp.arm_resolutions.get(arm)
        if cur_res is None:
            add(f"arms.{arm}")
            continue
        if rec_sha and cur_res.resolved_sha != rec_sha:
            raw_old = blob_store.read(rec_sha)
            if raw_old is not None:
                try:
                    old_doc = json.loads(raw_old)
                except ValueError:
                    old_doc = None
            else:
                old_doc = None
            if isinstance(old_doc, dict):
                for key in diff_resolved_docs(old_doc, json.loads(cur_res.resolved_bytes), map_grader=True):
                    add(key)
            else:
                add(f"arms.{arm}")
    if problems:
        raise Refused(
            f"cannot resume {paths.short_path(path)}: {', '.join(problems)} changed since the experiment started; "
            "restore them or pass --out for a new file"
        )
    if started.get("digest") != exp.digest:
        raise Refused(f"the design changed since {paths.short_path(path)} was started; pass --out for a new file")


def check_design(exp: Experiment, path: Path, found: list[dict]) -> None:
    check_resume(exp, path, found, prepare=False)


def _manifest_event(exp: Experiment) -> dict:
    return {
        "event": "manifest",
        "digest": exp.digest,
        "design": exp.design,
        "engine": engine_commit(),
        "engine_sha": exp.engine_sha,
        "arms": {
            arm: {
                "ref": exp.arm_refs[arm],
                "commit": exp.arm_commits[arm],
                "base": exp.arm_bases.get(arm, ""),
                "resolved_sha": exp.arm_resolutions[arm].resolved_sha,
            }
            for arm in exp.arms
            if arm in exp.arm_resolutions
        },
        "grader": exp.grader_pins,
    }


def _persist_arm_blobs(exp: Experiment) -> None:
    if not exp.arm_resolutions or not paths.DB_FILE.exists():
        return
    with blob_store.lock():
        rows: list[tuple[str, str, int]] = []
        for res in exp.arm_resolutions.values():
            rows.extend(res.write_blobs())
        if rows:
            from ..store import db as foundation

            with foundation.tx() as c:
                if run_store.has_pins(c):
                    for digest_sha, blob_kind, size in rows:
                        blob_store.record_row(c, digest_sha, blob_kind, size)


def drive(exp: Experiment, path: Path) -> int:
    found = events(path)
    check_resume(exp, path, found, prepare=True)
    _persist_arm_blobs(exp)
    if not any(e.get("event") == "manifest" for e in found):
        append(path, _manifest_event(exp))
    append(path, {"event": "session", "at": util.iso(), "limits": exp.limits, "pid": os.getpid(),
                  "engine": engine_commit(), "engine_sha": exp.engine_sha})
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
    check_resume(exp, path, found, prepare=False)
    grader, accept = exp.design["grader"], exp.accept
    mode = "whole workflow" if exp.executors else f"atom {exp.atom}"
    out = [f"{label} {exp.name} (design {exp.digest}): {mode} on {exp.repo.name}",
           f"results file: {paths.short_path(path)}"]
    for name, s in exp.settings.items():
        extra_parts = []
        for k, v in exp.extras.get(name, {}).items():
            if k == "prompts" and isinstance(v, str):
                extra_parts.append(f", {k} {paths.short_path(v)}")
            elif isinstance(v, str):
                extra_parts.append(f", {k} {v[:12]}")
            elif isinstance(v, dict):
                extra_parts.append(f", {k} {','.join(sorted(v))}")
        extra = "".join(extra_parts)
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
        is_main = threading.current_thread() is threading.main_thread()
        earlier = signal.signal(signal.SIGTERM, terminate) if is_main else None
        try:
            return job()
        finally:
            if is_main and earlier is not None:
                signal.signal(signal.SIGTERM, earlier)


def run(manifest: Path, out: str = "", dry: bool = False) -> int:
    if not dry and manifest.is_file():
        slug = commit_manifest(manifest)
        exp = load_from_config(slug) if config_manifest_path(slug).is_file() else load(manifest)
    else:
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
    mine = [t for t in trials if t.get("arm") == arm and t.get("status") != "drifted"]
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


def experiment_slug(raw: str) -> str:
    slug = raw.strip().removeprefix("experiment:")
    if not SLUG.fullmatch(slug):
        raise Refused(f"invalid experiment id {raw!r}; expected slug matching {SLUG.pattern}")
    return slug


def _manifest_bytes_for(slug: str) -> bytes | None:
    rel = f"experiments/{slug}.toml"
    if configrepo.is_repo():
        if raw := configrepo.live_bytes(rel):
            return raw
    candidate = paths.CONFIG_DIR / rel
    return candidate.read_bytes() if candidate.is_file() else None


def _optimize_trail_results(slug: str) -> dict | None:
    trail = paths.STATE_DIR / "experiments" / slug / "trail.jsonl"
    found = events(trail)
    search_ev = next((e for e in reversed(found) if e.get("event") == "search"), None)
    if search_ev is None:
        return None
    rounds_ev = [e for e in found if e.get("event") == "round"]
    holdout_ev = next((e for e in reversed(found) if e.get("event") == "holdout"), None)
    end_ev = next((e for e in reversed(found) if e.get("event") == "end"), None)
    stop_ev = next((e for e in reversed(found) if e.get("event") == "stop"), None)
    return {
        "name": slug,
        "mode": "optimize",
        "digest": search_ev.get("digest", ""),
        "rounds": rounds_ev,
        "holdout": holdout_ev,
        "end": end_ev,
        "stopped": stop_ev,
    }


def experiment_view(raw_id: str) -> dict | None:
    from ..store import config as config_store

    try:
        slug = experiment_slug(raw_id)
    except Refused:
        return None
    raw = _manifest_bytes_for(slug)
    if raw is None:
        return None
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None

    rel = f"experiments/{slug}.toml"
    live_row = None
    if paths.DB_FILE.exists() and config_store.available():
        live_row = config_store.live_item("experiment", slug) or config_store.live_by_path(rel)
    version = int(live_row["ver"]) if live_row and live_row.get("ver") is not None else 1

    res_path = default_path(slug)
    res_events = events(res_path)
    manifest_ev = next((e for e in res_events if e.get("event") == "manifest"), None)
    manifest_arms = manifest_ev.get("arms", {}) if isinstance(manifest_ev, dict) else {}

    arms_list: list[dict] = []
    if isinstance(parsed.get("arms"), dict):
        for arm_name, arm_cfg in parsed["arms"].items():
            ref = arm_ref_name(slug, arm_name)
            m_info = manifest_arms.get(arm_name) if isinstance(manifest_arms, dict) else None
            commit = configrepo.resolve_ref(ref) or (m_info.get("commit") if isinstance(m_info, dict) else "") or None
            resolved_sha = (m_info.get("resolved_sha") if isinstance(m_info, dict) else None) or None
            arms_list.append({
                "name": str(arm_name),
                "ref": ref,
                "commit": commit,
                "resolved_sha": resolved_sha,
                "settings": dict(arm_cfg) if isinstance(arm_cfg, dict) else {},
            })
    elif isinstance(parsed.get("rounds"), list):
        seen_names: set[str] = set()
        arms_list.append({
            "name": "baseline",
            "ref": arm_ref_name(slug, "baseline"),
            "commit": configrepo.resolve_ref(arm_ref_name(slug, "baseline")) or None,
            "resolved_sha": None,
            "settings": {},
        })
        seen_names.add("baseline")
        for rnd in parsed["rounds"]:
            cands = rnd.get("candidates") if isinstance(rnd, dict) else None
            if not isinstance(cands, dict):
                continue
            for cname, cval in cands.items():
                if cname in seen_names:
                    continue
                seen_names.add(cname)
                ref = arm_ref_name(slug, str(cname))
                arms_list.append({
                    "name": str(cname),
                    "ref": ref,
                    "commit": configrepo.resolve_ref(ref) or None,
                    "resolved_sha": None,
                    "settings": dict(cval) if isinstance(cval, dict) else {},
                })

    results: dict | None = None
    if manifest_ev is not None:
        results = summary(res_path)
    else:
        results = _optimize_trail_results(slug)

    return {
        "id": f"experiment:{slug}",
        "subject": f"experiment:{slug}",
        "version": version,
        "arms": arms_list,
        "results": results,
    }


def experiment_views() -> list[dict]:
    from .. import configitems

    slugs: set[str] = set()
    for item in configitems.listing("experiment"):
        if name := str(item.get("name") or "").strip():
            slugs.add(name)
    exp_dir = paths.CONFIG_DIR / "experiments"
    if exp_dir.is_dir():
        for file in sorted(exp_dir.glob("*.toml")):
            if file.is_file():
                slugs.add(file.stem)
    views: list[dict] = []
    for slug in sorted(slugs):
        if view := experiment_view(slug):
            views.append(view)
    return views


def launch_async(
    raw_id: str,
    *,
    mode: str = "",
    run_args: dict | None = None,
    actor: str = "",
) -> tuple[str, object]:
    import concurrent.futures

    from . import optimize

    slug = experiment_slug(raw_id)
    raw = _manifest_bytes_for(slug)
    if raw is None:
        raise FileNotFoundError(f"No experiment {slug}")
    parsed = tomllib.loads(raw.decode("utf-8"))
    resolved_mode = mode or ("optimize" if "rounds" in parsed else "run")
    args = dict(run_args or {})
    out_arg = str(args.get("out") or "")

    if resolved_mode == "optimize":
        search = optimize.load_from_config(slug)
        out_folder = optimize.folder_of(search, out_arg)
        optimize.check_digest(search, out_folder / "trail.jsonl", events(out_folder / "trail.jsonl"))
        repo_name = search.repo

        def runner_job() -> int:
            return exclusive(repo_name, lambda: optimize.climb(search, out_folder))
    else:
        exp = load_from_config(slug)
        out_path = paths.expand(out_arg) if out_arg else default_path(slug)
        check_resume(exp, out_path, events(out_path), prepare=True)
        _persist_arm_blobs(exp)
        repo_name = exp.repo.name

        def runner_job() -> int:
            return exclusive(repo_name, lambda: drive(exp, out_path))

    run_id = record.start(
        "experiment_run",
        tag=slug,
        repo=repo_name,
        started_by=actor or None,
        summary=f"experiment {resolved_mode} {slug}",
    )
    future: concurrent.futures.Future[int] = concurrent.futures.Future()

    def _worker() -> None:
        try:
            code = runner_job()
        except BaseException as exc:
            record.settle(run_id, "failed", summary=util.first_line(str(exc), 200))
            if not future.done():
                future.set_exception(exc)
            return
        record.settle(
            run_id,
            "done" if code == 0 else "stopped",
            summary=f"experiment {resolved_mode} {slug} exited {code}",
        )
        if not future.done():
            future.set_result(code)

    thread = threading.Thread(target=_worker, name=f"experiment-{slug}", daemon=True)
    thread.start()
    return run_id, future
