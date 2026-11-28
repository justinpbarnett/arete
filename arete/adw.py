from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    config,
    configkinds,
    configschema,
    factory,
    flows,
    patching,
    paths,
    pipeline,
    run_lines,
    tracker,
)
from .store import runs as run_store
from .store import specs as spec_store
from .store import tasks as task_store
from .verify import Verified

PYTHON_ADWS = frozenset(("orchestrate", "single_worker"))
CONDITIONS = frozenset(("documents", "dry_run", "repo.parts", "feedback"))
BRACE_PLACEHOLDERS = frozenset(("{try}", "{prev?}", "{tries}", "{outer.prev}"))
AT_PLACEHOLDERS = frozenset(("@last",))
ROUTE_EVENTS = frozenset(("interrupted", "feedback"))
ROUTE_WHEN_KEYS = frozenset(("event", "repaired_by", "repo", "kind"))
RESERVED_RULES = frozenset(("@continue", "@repair", "@asked"))

_NAME_RE = re.compile(rf"^{configkinds.NAME_PATTERN}$")
_TRIES_RE = re.compile(r"^setting:([a-z0-9_]+)\.tries$")
_BRACE_RE = re.compile(r"\{[^{}]*\}")
_CONTROL_KEYS = frozenset(("do", "end", "when", "then", "first", "repeat", "until", "min", "else", "finish", "code"))
_REPEAT_KEYS = frozenset(("first", "repeat", "until", "tries", "min", "else", "when"))
_WHEN_BLOCK_KEYS = frozenset(("when", "then"))


class AdwError(ValueError):
    pass


class UnknownAdw(FileNotFoundError):
    pass


@dataclass(frozen=True)
class Adw:
    name: str
    steps: tuple[dict, ...]
    path: Path
    level: str
    raw: bytes
    sha256: str
    version: str


@dataclass(frozen=True)
class RouteRule:
    id: str
    adw: str
    when: dict[str, object] = field(default_factory=dict)
    set: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Routes:
    rules: tuple[RouteRule, ...]
    path: Path
    level: str
    raw: bytes
    sha256: str
    version: str


@dataclass(frozen=True)
class RouteDecision:
    adw: str
    rule: str
    version: str
    loaded: Adw | None
    continues: str = ""
    overrides: dict[str, dict] = field(default_factory=dict)
    routes_version: str = ""
    routes_level: str = ""
    answered: bool = False


def _active_config_dir(config_dir: Path | None = None) -> Path:
    if config_dir is not None:
        return config_dir.resolve()
    from . import pins

    if (pin := pins.current()) is not None:
        return pin.config_dir
    return paths.CONFIG_DIR.resolve()


def _valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.fullmatch(name)) and "/" not in name and "\\" not in name


def resolve_adw_path(name: str, *, config_dir: Path | None = None) -> tuple[Path, str] | None:
    if not _valid_name(name):
        return None
    root = _active_config_dir(config_dir)
    cfg_file = root / "adws" / f"{name}.toml"
    if cfg_file.is_file():
        return cfg_file, "config"
    eng_file = paths.ENGINE_ADWS_DIR / f"{name}.toml"
    if eng_file.is_file():
        return eng_file, "engine"
    return None


def resolve_routes_path(*, config_dir: Path | None = None) -> tuple[Path, str]:
    root = _active_config_dir(config_dir)
    cfg_file = root / "routes" / "adw.toml"
    if cfg_file.is_file():
        return cfg_file, "config"
    return paths.ENGINE_ROUTES_DIR / "adw.toml", "engine"


def version(adw_obj: Adw) -> str:
    return adw_obj.version


def python_adw_version(name: str) -> str:
    from . import codebase

    stamp = codebase.STAMP[:12] if codebase.STAMP else "engine"
    return f"adw/{name}@{stamp}"


_TREE_STEPS = frozenset((
    "checks", "ci", "document", "implement", "learn", "patch", "pr", "prepare", "repair", "revise", "verify",
))


def _step_takes_tree(step_name: str) -> bool:
    return step_name in _TREE_STEPS


def _validate_placeholders(value: object, where: str, depth: int, errors: list[str]) -> None:
    if isinstance(value, str):
        if value.startswith("@") and value not in AT_PLACEHOLDERS:
            errors.append(f"{where}: unknown placeholder {value!r}")
        elif value == "@last" and depth < 1:
            errors.append(f"{where}: placeholder '@last' requires an enclosing repeat")
        stripped = _BRACE_RE.sub("", value)
        if "{" in stripped or "}" in stripped:
            errors.append(f"{where}: malformed placeholder in {value!r}")
        for match in _BRACE_RE.finditer(value):
            token = match.group(0)
            if token not in BRACE_PLACEHOLDERS:
                errors.append(f"{where}: unknown placeholder {token!r}")
            elif token == "{outer.prev}" and depth < 2:
                errors.append(f"{where}: placeholder '{{outer.prev}}' requires a nested repeat")
            elif depth < 1:
                errors.append(f"{where}: placeholder {token!r} requires an enclosing repeat")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _validate_placeholders(item, f"{where}[{i}]", depth, errors)
    elif isinstance(value, dict):
        for k, v in value.items():
            _validate_placeholders(v, f"{where}.{k}", depth, errors)


def _validate_condition(cond: object, where: str, errors: list[str]) -> None:
    if not isinstance(cond, str) or cond not in CONDITIONS:
        errors.append(f"{where}.when: unknown condition {cond!r}; expected one of {', '.join(sorted(CONDITIONS))}")


def _validate_step_list(entries: object, where: str, depth: int, errors: list[str], *, allow_empty: bool = False) -> None:
    if not isinstance(entries, list) or (not allow_empty and not entries):
        errors.append(f"{where} must be a non-empty list of steps")
        return
    for idx, entry in enumerate(entries):
        _validate_entry(entry, f"{where}[{idx}]", depth, errors)


def _validate_do(entry: dict, where: str, depth: int, errors: list[str]) -> None:
    step_name = entry.get("do")
    if not isinstance(step_name, str) or step_name not in factory.STEPS:
        errors.append(f"{where}.do: unknown step {step_name!r}")
    if "end" in entry and type(entry["end"]) is not bool:
        errors.append(f"{where}.end must be a boolean")
    if "when" in entry:
        _validate_condition(entry["when"], where, errors)
    bad = (set(entry) & _CONTROL_KEYS) - {"do", "end", "when"}
    if bad:
        errors.append(f"{where}: disallowed keys in do entry: {', '.join(sorted(bad))}")
    for key, value in entry.items():
        if key not in ("do", "end", "when"):
            _validate_placeholders(value, f"{where}.{key}", depth, errors)


def _validate_entry(entry: object, where: str, depth: int, errors: list[str]) -> None:
    if not isinstance(entry, dict):
        errors.append(f"{where} must be a table")
        return
    if "code" in entry:
        errors.append(f"{where}: 'code' is not allowed")
        return
    has_do = "do" in entry
    has_repeat = any(k in entry for k in ("first", "repeat", "until"))
    has_then = "then" in entry
    has_finish = "finish" in entry
    kinds = sum((has_do, has_repeat, has_then, has_finish))
    if kinds != 1:
        errors.append(f"{where} must be exactly one of: do, repeat, when/then, or finish")
        return
    if has_do:
        _validate_do(entry, where, depth, errors)
    elif has_repeat:
        extra = set(entry) - _REPEAT_KEYS
        if extra:
            errors.append(f"{where}: unknown repeat keys: {', '.join(sorted(extra))}")
        if "when" in entry:
            _validate_condition(entry["when"], where, errors)
        if entry.get("until") != "passed":
            errors.append(f"{where}.until must be 'passed'")
        tries = entry.get("tries")
        m = _TRIES_RE.fullmatch(tries) if isinstance(tries, str) else None
        if not m or m.group(1) not in flows.SETTABLE:
            errors.append(f"{where}.tries: invalid setting reference {tries!r}")
        if "min" in entry and (type(entry["min"]) is not int or entry["min"] < 0):
            errors.append(f"{where}.min must be a non-negative integer")
        first = entry.get("first")
        if not isinstance(first, dict) or "do" not in first:
            errors.append(f"{where}.first must be a do entry")
        else:
            _validate_do(first, f"{where}.first", depth + 1, errors)
        _validate_step_list(entry.get("repeat"), f"{where}.repeat", depth + 1, errors, allow_empty=False)
        if "else" in entry:
            _validate_step_list(entry.get("else"), f"{where}.else", depth + 1, errors, allow_empty=False)
    elif has_then:
        extra = set(entry) - _WHEN_BLOCK_KEYS
        if extra:
            errors.append(f"{where}: unknown when-block keys: {', '.join(sorted(extra))}")
        if "when" not in entry:
            errors.append(f"{where}: when-block requires 'when'")
        else:
            _validate_condition(entry["when"], where, errors)
        _validate_step_list(entry.get("then"), f"{where}.then", depth, errors, allow_empty=False)
    elif has_finish:
        if "when" in entry:
            _validate_condition(entry["when"], where, errors)
        ending = entry.get("finish")
        if not isinstance(ending, str) or not ending.strip():
            errors.append(f"{where}.finish must be a non-empty string")
        bad = (set(entry) & _CONTROL_KEYS) - {"finish", "when"}
        if bad:
            errors.append(f"{where}: disallowed keys in finish entry: {', '.join(sorted(bad))}")
        for key, value in entry.items():
            if key not in ("finish", "when"):
                _validate_placeholders(value, f"{where}.{key}", depth, errors)


def always_finishes(entries: list[dict]) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "finish" in entry and "when" not in entry:
            return True
    return False


def _validate_and_steps(data: object, *, where: str = "adw") -> tuple[list[dict] | None, list[str]]:
    errors: list[str] = []
    try:
        configschema.validate("adw", data, where=where)
    except ValueError as e:
        errors.append(str(e))
        return None, errors
    assert isinstance(data, dict)
    if not _valid_name(data["name"]):
        errors.append(f"{where}.name: invalid ADW name {data['name']!r}")
    from . import adw_library, adwgraph

    if adwgraph.is_graph(data):
        try:
            compiled = adwgraph.compile(data, adw_library.load())
        except adwgraph.GraphError as e:
            errors.append(f"{where}: {e}")
            return None, errors
    else:
        compiled = data["steps"]
    _validate_step_list(compiled, f"{where}.steps", 0, errors, allow_empty=False)
    if not errors and not always_finishes(compiled):
        errors.append(f"{where}.steps: steps can reach their end without a finish")
    return (None if errors else compiled), errors


def validate_adw(data: object, *, where: str = "adw") -> list[str]:
    return _validate_and_steps(data, where=where)[1]


def parse_rule_set(raw_set: dict, *, where: str = "set") -> dict[str, dict]:
    if not isinstance(raw_set, dict):
        raise AdwError(f"{where} must be a table")
    items: list[str] = []
    for key, value in raw_set.items():
        if not isinstance(key, str) or "." not in key:
            raise AdwError(f"{where}.{key}: write step.key = value")
        if type(value) is bool:
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float, str)):
            rendered = str(value)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            rendered = ",".join(value)
        else:
            raise AdwError(f"{where}.{key}: unsupported override value {value!r}")
        items.append(f"{key}={rendered}")
    try:
        return factory.overrides(items, flows.SETTABLE)
    except (ValueError, TypeError) as e:
        raise AdwError(f"{where}: {e}") from None


def validate_routes(data: object, *, cfg: config.Config | None = None, config_dir: Path | None = None,
                    extra_adws: set[str] | None = None, where: str = "routes/adw.toml") -> list[str]:
    errors: list[str] = []
    try:
        configschema.validate("routes", data, where=where)
    except ValueError as e:
        errors.append(str(e))
        return errors
    assert isinstance(data, dict)
    rules = data["rule"]
    if not rules:
        errors.append(f"{where}.rule must not be empty")
        return errors
    seen_ids: set[str] = set()
    extras = extra_adws or set()
    for idx, rule in enumerate(rules):
        loc = f"{where}.rule[{idx}]"
        rid = rule.get("id", "")
        if not isinstance(rid, str) or not rid.strip():
            errors.append(f"{loc}.id must be a non-empty string")
        elif rid.startswith("@"):
            errors.append(f"{loc}.id: {rid!r} starts with reserved prefix '@'")
        elif rid in seen_ids:
            errors.append(f"{loc}.id: duplicate rule id {rid!r}")
        else:
            seen_ids.add(rid)
        adw_name = rule.get("adw", "")
        if not isinstance(adw_name, str) or not adw_name.strip():
            errors.append(f"{loc}.adw must be a non-empty string")
        elif (adw_name not in extras and adw_name not in PYTHON_ADWS
              and resolve_adw_path(adw_name, config_dir=config_dir) is None):
            errors.append(f"{loc}.adw: unknown ADW {adw_name!r}")
        if "when" in rule:
            if idx == len(rules) - 1:
                errors.append(f"{loc}: last rule must not have a 'when' condition")
            when = rule["when"]
            for key, val in when.items():
                if key not in ROUTE_WHEN_KEYS:
                    errors.append(f"{loc}.when: unknown key {key!r}")
                elif key == "event":
                    vals = [val] if isinstance(val, str) else list(val)
                    if not vals:
                        errors.append(f"{loc}.when.event must not be empty")
                    for ev in vals:
                        if ev not in ROUTE_EVENTS:
                            errors.append(f"{loc}.when.event: unknown event {ev!r}")
                elif key == "repaired_by":
                    if type(val) is not bool:
                        errors.append(f"{loc}.when.repaired_by must be a boolean")
                elif key in ("repo", "kind"):
                    vals = [val] if isinstance(val, str) else list(val)
                    if not vals or any(not isinstance(v, str) or not v.strip() for v in vals):
                        errors.append(f"{loc}.when.{key} must be a non-empty string or list of strings")
        if "set" in rule:
            try:
                parse_rule_set(rule["set"], where=f"{loc}.set")
            except AdwError as e:
                errors.append(str(e))
    return errors


def load(cfg: config.Config | None, name: str, *, config_dir: Path | None = None) -> Adw:
    found = resolve_adw_path(name, config_dir=config_dir)
    if found is None:
        raise UnknownAdw(f"no adw {name}")
    path, level = found
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise AdwError(f"{path}: invalid TOML: {e}") from e
    compiled, errors = _validate_and_steps(data, where=str(path))
    if errors:
        raise AdwError("; ".join(errors))
    assert compiled is not None
    if data["name"] != name:
        raise AdwError(f"{path}: name {data['name']!r} does not match file name {name!r}")
    sha = hashlib.sha256(raw).hexdigest()
    return Adw(
        name=name,
        steps=tuple(compiled),
        path=path,
        level=level,
        raw=raw,
        sha256=sha,
        version=f"adw/{name}@{sha[:12]}",
    )


def parse_routes_data(data: dict, *, path: Path, level: str, raw: bytes,
                      config_dir: Path | None = None, extra_adws: set[str] | None = None) -> Routes:
    if errors := validate_routes(data, config_dir=config_dir, extra_adws=extra_adws, where=str(path)):
        raise AdwError("; ".join(errors))
    sha = hashlib.sha256(raw).hexdigest()
    rules = tuple(
        RouteRule(
            id=str(r["id"]),
            adw=str(r["adw"]),
            when=dict(r.get("when") or {}),
            set=dict(r.get("set") or {}),
        )
        for r in data["rule"]
    )
    return Routes(
        rules=rules,
        path=path,
        level=level,
        raw=raw,
        sha256=sha,
        version=f"routes/adw@{sha[:12]}",
    )


def load_routes(cfg: config.Config | None = None, *, config_dir: Path | None = None) -> Routes:
    path, level = resolve_routes_path(config_dir=config_dir)
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise AdwError(f"{path}: invalid TOML: {e}") from e
    return parse_routes_data(data, path=path, level=level, raw=raw, config_dir=config_dir)


def _adw_has_step(steps: list[dict] | tuple[dict, ...], step_name: str) -> bool:
    for entry in steps:
        if entry.get("do") == step_name:
            return True
        if "first" in entry and entry["first"].get("do") == step_name:
            return True
        for key in ("repeat", "else", "then"):
            if key in entry and _adw_has_step(entry[key], step_name):
                return True
    return False


def first_roles(adw_obj: Adw) -> tuple[str, ...]:
    roles: list[str] = []
    for entry in adw_obj.steps:
        if "first" in entry or "repeat" in entry:
            break
        if "do" in entry and "when" not in entry:
            step_name = entry["do"]
            kw = {k: v for k, v in entry.items() if k not in ("do", "end", "when")}
            if atom := flows.atom_of(step_name, kw):
                roles.append(atom)
    return tuple(dict.fromkeys(roles))


def _resolve_value(value: object, frames: list[dict]) -> object:
    if isinstance(value, str):
        cur = frames[-1] if frames else None
        outer = frames[-2] if len(frames) >= 2 else None
        if value == "@last":
            return cur["last"] if cur else ""
        if value == "{try}" and cur is not None:
            return int(cur["try"])
        if value == "{tries}" and cur is not None:
            return int(cur["tries"]) if isinstance(cur["tries"], int) else cur["tries"]
        if value == "{outer.prev}" and outer is not None:
            return int(outer["try"] - 1)
        if value == "{prev?}" and cur is not None:
            prev = cur["try"] - 1
            return "" if prev == 1 else str(prev)
        if "{" not in value:
            return value
        out = value
        if cur is not None:
            prev = cur["try"] - 1
            out = out.replace("{try}", str(cur["try"]))
            out = out.replace("{tries}", str(cur["tries"]))
            out = out.replace("{prev?}", "" if prev == 1 else str(prev))
        if outer is not None:
            out = out.replace("{outer.prev}", str(outer["try"] - 1))
        return out
    if isinstance(value, list):
        return [_resolve_value(item, frames) for item in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, frames) for k, v in value.items()}
    return value


def _outline_steps(entries: list[dict] | tuple[dict, ...], frames: list[dict]) -> list[dict]:
    nodes: list[dict] = []
    for entry in entries:
        if "do" in entry:
            step_name = entry["do"]
            kw = {k: _resolve_value(v, frames) for k, v in entry.items() if k not in ("do", "end", "when")}
            node: dict[str, object] = {
                "kind": "step",
                "step": step_name,
                "role": flows.atom_of(step_name, kw),
                "end": bool(entry.get("end", False)),
            }
            if "round" in kw:
                node["round"] = str(kw["round"])
            if "when" in entry:
                node["when"] = entry["when"]
            nodes.append(node)
        elif "first" in entry:
            first_frame = [*frames, {"try": 1, "tries": entry["tries"], "last": ""}]
            repeat_frame = [*frames, {"try": 2, "tries": entry["tries"], "last": ""}]
            node = {
                "kind": "repeat",
                "tries": entry["tries"],
                "min": int(entry.get("min", 0)),
                "until": entry["until"],
                "first": _outline_steps([entry["first"]], first_frame)[0],
                "repeat": _outline_steps(entry["repeat"], repeat_frame),
                "else": _outline_steps(entry.get("else", []), first_frame),
            }
            if "when" in entry:
                node["when"] = entry["when"]
            nodes.append(node)
        elif "then" in entry:
            nodes.append({
                "kind": "when",
                "when": entry["when"],
                "then": _outline_steps(entry["then"], frames),
            })
        elif "finish" in entry:
            node = {"kind": "finish", "finish": entry["finish"]}
            if "why" in entry:
                node["why"] = entry["why"]
            if "when" in entry:
                node["when"] = entry["when"]
            nodes.append(node)
    return nodes


def outline(adw_obj: Adw) -> list[dict]:
    return _outline_steps(adw_obj.steps, [])


def _eval_condition(cond: str, flow: factory.Flow, last_outcome: factory.Outcome | None) -> bool:
    if cond == "documents":
        return bool(flows.documents(flow.ctx))
    if cond == "dry_run":
        return bool(flow.ctx.run.dry_run)
    if cond == "repo.parts":
        return bool(flow.ctx.run.repo.parts) and not flows.direct_push_info(flow.ctx)
    if cond == "feedback":
        return bool(last_outcome is not None and last_outcome.feedback)
    raise AdwError(f"unknown condition {cond!r}")


def _do_kwargs(entry: dict, flow: factory.Flow, frames: list[dict]) -> dict:
    step_name = entry["do"]
    kw = {k: _resolve_value(v, frames) for k, v in entry.items() if k not in ("do", "end", "when")}
    if _step_takes_tree(step_name) and "tree" not in kw:
        kw["tree"] = str(flow.ctx.ws)
    return kw


def _drive_pure(adw_obj: Adw, flow: factory.Flow) -> tuple[str, str, str]:
    last_outcome: factory.Outcome | None = None

    def run_list(entries: list[dict] | tuple[dict, ...], frames: list[dict]) -> tuple[str, str, str] | None:
        nonlocal last_outcome
        for entry in entries:
            if "when" in entry and "then" not in entry and not _eval_condition(entry["when"], flow, last_outcome):
                continue
            if "finish" in entry:
                ending = entry["finish"]
                if ending == "dry_run":
                    return flows.end_dry_run(flow)
                if ending == "last":
                    return flows.ended(flow, last_outcome)
                kw = {k: _resolve_value(v, frames) for k, v in entry.items() if k not in ("finish", "when")}
                return flows.finish(flow, ending, **kw)
            if "then" in entry:
                if _eval_condition(entry["when"], flow, last_outcome):
                    if (done := run_list(entry["then"], frames)) is not None:
                        return done
                continue
            if "do" in entry:
                kw = _do_kwargs(entry, flow, frames)
                outcome = flows.step(flow, entry["do"], **kw)
                last_outcome = outcome
                if entry.get("end") and not outcome.passed:
                    return flows.ended(flow, outcome)
                continue
            if "first" in entry:
                role = _TRIES_RE.fullmatch(entry["tries"]).group(1)
                limit = max(int(entry.get("min", 0)), int(flow.ctx.run.settings(role).tries))
                attempt = 1
                frame = {"try": attempt, "tries": limit, "last": ""}
                nested = [*frames, frame]
                first_kw = _do_kwargs(entry["first"], flow, nested)
                outcome = flows.step(flow, entry["first"]["do"], **first_kw)
                frame["last"] = outcome.feedback
                last_outcome = outcome
                while not outcome.passed and attempt < limit:
                    attempt += 1
                    frame["try"] = attempt
                    if (done := run_list(entry["repeat"], nested)) is not None:
                        return done
                    first_kw = _do_kwargs(entry["first"], flow, nested)
                    outcome = flows.step(flow, entry["first"]["do"], **first_kw)
                    frame["last"] = outcome.feedback
                    last_outcome = outcome
                if not outcome.passed and entry.get("else"):
                    if (done := run_list(entry["else"], nested)) is not None:
                        return done
        return None

    result = run_list(adw_obj.steps, [])
    if result is None:
        raise AdwError(f"adw {adw_obj.name} reached the end without a finish")
    return result


def _run_real_final_convergence(flow: factory.Flow, state: dict, reviews: int, attempt: int) -> tuple[str, str, str] | None:
    job: flows.Job = flow.ctx
    ws = str(job.ws)
    n = attempt
    review_needed = False
    while blocked := flows.final_blocker(
        flow, ws,
        patch=state["patch"],
        fixed=state["fixed"],
        skipped=state["skipped"],
        review_needed=review_needed,
        allow_verify=state["had_verify"],
    ):
        why, outcome = blocked
        if not (outcome and outcome.next == "implement" and outcome.data.get("focused_hold")):
            if why:
                return flows.finish(flow, "failed", why=why, detail=outcome.feedback if outcome else "")
            return flows.ended(flow, outcome)
        if n >= reviews:
            return flows.finish(flow, "failed", why=flows.FOCUSED_FAILURE, detail=outcome.feedback)
        repaired = flows.step(flow, "implement", tree=ws, round=pipeline.round_name(flows.REVIEW_FIX, n),
                              feedback=outcome.feedback)
        if not repaired.passed:
            return flows.ended(flow, repaired)
        n += 1
        state["fixed"] = True
        review_needed = True
        if not state["skipped"]:
            job.verified = flows.predating_fixes(job.verified)
    return None


def _drive_real_job(adw_obj: Adw, flow: factory.Flow) -> tuple[str, str, str]:
    job: flows.Job = flow.ctx
    if job.run.environment_release:
        return flows.retry_environment(flow)
    ws = str(job.ws)
    last_outcome: factory.Outcome | None = None
    state: dict = {
        "patch": False,
        "fixed": False,
        "skipped": False,
        "had_verify": _adw_has_step(adw_obj.steps, "verify"),
        "had_checks": _adw_has_step(adw_obj.steps, "checks"),
        "pending_review_outcome": None,
        "escalated": False,
        "skip_plan_build": False,
        "skip_patch": False,
    }

    def run_feature_escalation_prefix() -> tuple[str, str, str] | None:
        nonlocal last_outcome
        for name, kw in (("plan", {}), ("implement", {"tree": ws, "round": flows.BUILD})):
            outcome = flows.step(flow, name, **kw)
            last_outcome = outcome
            if not outcome.passed:
                return flows.ended(flow, outcome)
        return None

    def run_real_checks_repeat(entry: dict, frames: list[dict], *, in_review: bool) -> tuple[str, str, str] | factory.Outcome | None:
        nonlocal last_outcome
        role = _TRIES_RE.fullmatch(entry["tries"]).group(1)
        limit = max(int(entry.get("min", 0)), int(job.run.settings(role).tries))
        attempt = 1
        frame = {"try": attempt, "tries": limit, "last": ""}
        nested = [*frames, frame]

        def check_once() -> factory.Outcome:
            first_kw = _do_kwargs(entry["first"], flow, nested)
            if in_review or state["patch"]:
                first_kw["quick"] = True
            after = str(first_kw.get("after") or "")
            outer_n = frames[-1]["try"] - 1 if in_review and frames else 0
            producer = f"{after}:{outer_n}:{attempt}" if in_review else f"build:{attempt}"
            tree = first_kw.pop("tree", ws)
            return flows.check_candidate(flow, tree, producer, **first_kw)

        outcome = check_once()
        frame["last"] = outcome.feedback
        last_outcome = outcome
        while not outcome.passed and attempt < limit:
            if in_review and outcome.data.get("focused_hold"):
                break
            attempt += 1
            frame["try"] = attempt
            if not in_review and outcome.data.get("focused_hold"):
                fixed = flows.step(flow, "implement", tree=ws, round=f"{flows.REVIEW_FIX}_checks{attempt}",
                                   feedback=outcome.feedback, attempt=attempt, tries=limit)
                last_outcome = fixed
                if not fixed.passed:
                    return flows.ended(flow, fixed)
            else:
                if (done := run_list(entry["repeat"], nested)) is not None:
                    return done
            outcome = check_once()
            frame["last"] = outcome.feedback
            last_outcome = outcome
        if in_review:
            if not state["skipped"]:
                job.verified = flows.predating_fixes(job.verified)
            if not outcome.passed:
                if outcome.data.get("focused_hold"):
                    return outcome
                if entry.get("else"):
                    if (done := run_list(entry["else"], nested)) is not None:
                        return done
            return None
        if not outcome.passed:
            if outcome.data.get("focused_hold"):
                flows.step(flow, "learn", tree=ws, apply=False)
                return flows.finish(flow, "failed", why=flows.FOCUSED_FAILURE, detail=outcome.feedback)
            if entry.get("else"):
                if (done := run_list(entry["else"], nested)) is not None:
                    return done
        return None

    def run_real_review_repeat(entry: dict, frames: list[dict]) -> tuple[str, str, str] | str | None:
        nonlocal last_outcome
        role = _TRIES_RE.fullmatch(entry["tries"]).group(1)
        limit = max(int(entry.get("min", 0)), int(job.run.settings(role).tries))
        if state["patch"]:
            limit = min(limit, job.layer.patch.review_tries)
        attempt = 1
        frame = {"try": attempt, "tries": limit, "last": ""}
        nested = [*frames, frame]
        pending = state.pop("pending_review_outcome", None)
        if pending is not None:
            outcome = pending
        else:
            first_kw = _do_kwargs(entry["first"], flow, nested)
            outcome = flows.step(flow, entry["first"]["do"], **first_kw)
        frame["last"] = outcome.feedback
        last_outcome = outcome
        while not outcome.passed and attempt < limit:
            attempt += 1
            frame["try"] = attempt
            hold_continue: factory.Outcome | None = None
            if _eval_condition("feedback", flow, outcome):
                state["fixed"] = True
                res = run_review_body(entry["repeat"], nested)
                if isinstance(res, tuple):
                    return res
                if isinstance(res, factory.Outcome):
                    hold_continue = res
            if hold_continue is not None:
                outcome = hold_continue
            else:
                first_kw = _do_kwargs(entry["first"], flow, nested)
                outcome = flows.step(flow, entry["first"]["do"], **first_kw)
            frame["last"] = outcome.feedback
            last_outcome = outcome

        why = (patching.change_refusal(job.layer.patch, job.b.restage().files, job.b.staged.lines)
               if state["patch"] else "")
        if state["patch"] and (why or not outcome.passed):
            job.run.log(f"patch: escalating to the feature flow: {why or 'review needs more than the patch repair budget'}")
            return "escalate"
        if not outcome.passed and outcome.data.get("focused_hold"):
            return flows.finish(flow, "failed", why=flows.FOCUSED_FAILURE, detail=outcome.feedback)
        if state["had_checks"] and job.built is not None:
            if (done := _run_real_final_convergence(flow, state, limit, attempt)) is not None:
                return done
        if state["patch"] and (why := patching.change_refusal(job.layer.patch, job.b.restage().files, job.b.staged.lines)):
            job.run.log(f"patch: escalating to the feature flow: {why}")
            return "escalate"
        return None

    def run_review_body(entries: list[dict] | tuple[dict, ...], frames: list[dict]) -> tuple[str, str, str] | factory.Outcome | None:
        nonlocal last_outcome
        for entry in entries:
            if "when" in entry and "then" not in entry and not _eval_condition(entry["when"], flow, last_outcome):
                continue
            if "then" in entry:
                if _eval_condition(entry["when"], flow, last_outcome):
                    res = run_review_body(entry["then"], frames)
                    if res is not None:
                        return res
                continue
            if "first" in entry and entry["first"].get("do") == "checks":
                res = run_real_checks_repeat(entry, frames, in_review=True)
                if res is not None:
                    return res
                continue
            if (done := run_list([entry], frames)) is not None:
                return done
        return None

    def run_list(entries: list[dict] | tuple[dict, ...], frames: list[dict]) -> tuple[str, str, str] | None:
        nonlocal last_outcome
        idx = 0
        while idx < len(entries):
            entry = entries[idx]
            idx += 1
            if "when" in entry and "then" not in entry and not _eval_condition(entry["when"], flow, last_outcome):
                continue
            if "finish" in entry:
                ending = entry["finish"]
                if ending == "dry_run":
                    return flows.end_dry_run(flow)
                if ending == "last":
                    return flows.ended(flow, last_outcome)
                kw = {k: _resolve_value(v, frames) for k, v in entry.items() if k not in ("finish", "when")}
                return flows.finish(flow, ending, **kw)
            if "then" in entry:
                if _eval_condition(entry["when"], flow, last_outcome):
                    if (done := run_list(entry["then"], frames)) is not None:
                        return done
                continue
            if "do" in entry:
                step_name = entry["do"]
                if state["skip_plan_build"] and step_name in ("plan", "implement") and entry.get("round", flows.BUILD) == flows.BUILD:
                    continue
                if state["skip_patch"] and step_name == "patch":
                    continue
                if step_name == "verify":
                    before_verify = pipeline.verification_trees(job.b) if job.built is not None and job.b.repair_findings else {}
                    state["skipped"] = flows.SKIPPED_LABEL in job.ticket.labels
                    if state["skipped"]:
                        job.verified = Verified("unverified", flows.SKIPPED_TEXT)
                        continue
                    if state["patch"]:
                        job.verified = Verified("unverified", "Focused verification follows review convergence on the final tree.")
                        continue
                    kw = _do_kwargs(entry, flow, frames)
                    kw.setdefault("return_focused_hold", True)
                    outcome = flows.step(flow, "verify", **kw)
                    last_outcome = outcome
                    if not outcome.passed:
                        if outcome.data.get("focused_hold"):
                            state["pending_review_outcome"] = outcome
                            continue
                        if entry.get("end"):
                            return flows.ended(flow, outcome)
                    elif before_verify and pipeline.verification_trees(job.b) != before_verify:
                        state["fixed"] = True
                        post = flows.checked_after(flow, ws, "in-use verification", 1)
                        last_outcome = post
                        if not post.passed:
                            if post.data.get("focused_hold"):
                                state["pending_review_outcome"] = post
                            else:
                                return flows.finish(flow, "failed", why="the checks failed after in-use verification",
                                                    detail=post.feedback)
                    continue
                kw = _do_kwargs(entry, flow, frames)
                outcome = flows.step(flow, step_name, **kw)
                last_outcome = outcome
                if entry.get("end") and not outcome.passed:
                    return flows.ended(flow, outcome)
                if step_name == "prepare" and not state["escalated"] and adw_obj.name in ("feature", "patch"):
                    requested = (adw_obj.name == "patch") or patching.requested(
                        job.layer, job.ticket, automatic=job.cfg.triage.route_fixes)
                    refusal = ("multi-repository changes require a full plan" if len(job.checkouts) != 1 else
                               patching.specification_refusal(job.ticket))
                    if requested and not refusal:
                        patched = flows.step(flow, "patch", tree=ws)
                        last_outcome = patched
                        if not patched.passed:
                            return flows.ended(flow, patched)
                        if not patched.data.get("escalate"):
                            state["patch"] = True
                            state["skip_plan_build"] = True
                            state["skip_patch"] = True
                        else:
                            state["skip_patch"] = True
                            if adw_obj.name == "patch":
                                if (done := run_feature_escalation_prefix()) is not None:
                                    return done
                    elif requested:
                        job.run.log(f"patch: escalating to the feature flow: {refusal}")
                        state["skip_patch"] = True
                        if adw_obj.name == "patch":
                            if (done := run_feature_escalation_prefix()) is not None:
                                return done
                continue
            if "first" in entry:
                first_step = entry["first"].get("do")
                if first_step == "checks" and not frames:
                    res = run_real_checks_repeat(entry, frames, in_review=False)
                    if isinstance(res, tuple):
                        return res
                    continue
                if first_step == "review" and not frames:
                    res = run_real_review_repeat(entry, frames)
                    if res == "escalate":
                        state["patch"] = False
                        state["fixed"] = False
                        state["escalated"] = True
                        state["skip_plan_build"] = False
                        state["skip_patch"] = True
                        if (done := run_feature_escalation_prefix()) is not None:
                            return done
                        # Re-run from the first checks/verify/review repeat onward
                        restart_idx = next(
                            (i for i, e in enumerate(entries) if "first" in e or e.get("do") == "verify"),
                            idx,
                        )
                        idx = restart_idx
                        continue
                    if isinstance(res, tuple):
                        return res
                    continue
                # Generic repeat (for any custom config ADW step)
                role = _TRIES_RE.fullmatch(entry["tries"]).group(1)
                limit = max(int(entry.get("min", 0)), int(job.run.settings(role).tries))
                attempt = 1
                frame = {"try": attempt, "tries": limit, "last": ""}
                nested = [*frames, frame]
                first_kw = _do_kwargs(entry["first"], flow, nested)
                outcome = flows.step(flow, first_step, **first_kw)
                frame["last"] = outcome.feedback
                last_outcome = outcome
                while not outcome.passed and attempt < limit:
                    attempt += 1
                    frame["try"] = attempt
                    if (done := run_list(entry["repeat"], nested)) is not None:
                        return done
                    first_kw = _do_kwargs(entry["first"], flow, nested)
                    outcome = flows.step(flow, first_step, **first_kw)
                    frame["last"] = outcome.feedback
                    last_outcome = outcome
                if not outcome.passed and entry.get("else"):
                    if (done := run_list(entry["else"], nested)) is not None:
                        return done
        return None

    result = run_list(adw_obj.steps, [])
    if result is None:
        raise AdwError(f"adw {adw_obj.name} reached the end without a finish")
    return result


def drive(adw_obj: Adw) -> Callable[[factory.Flow], tuple[str, str, str]]:
    def _run(flow: factory.Flow) -> tuple[str, str, str]:
        if isinstance(flow.ctx, flows.Job):
            return _drive_real_job(adw_obj, flow)
        return _drive_pure(adw_obj, flow)

    _run.__name__ = adw_obj.name
    return _run


def drive_named(cfg: config.Config | None, name: str, *, config_dir: Path | None = None) -> Callable[[factory.Flow], tuple[str, str, str]]:
    if name == "orchestrate":
        from . import orchestration

        return orchestration.orchestrate
    if name == "single_worker":
        from . import orchestration

        return orchestration.single_worker
    return drive(load(cfg, name, config_dir=config_dir))


def _matches_value(expected: object, actual: str) -> bool:
    if isinstance(expected, str):
        return actual == expected
    if isinstance(expected, (list, tuple, set)):
        return actual in expected
    return False


def _matches_rule(rule: RouteRule, *, events: set[str], has_repair: bool, repo_name: str, ticket_kind: str) -> bool:
    when = rule.when
    if not when:
        return True
    if "event" in when:
        wanted = [when["event"]] if isinstance(when["event"], str) else list(when["event"])
        if not any(ev in events for ev in wanted):
            return False
    if "repaired_by" in when and bool(has_repair) is not bool(when["repaired_by"]):
        return False
    if "repo" in when and not _matches_value(when["repo"], repo_name):
        return False
    return not ("kind" in when and not _matches_value(when["kind"], ticket_kind))


def _resolve_ticket_kind(cfg: config.Config, repo: config.Repo, tid: str, *, kind: str = "",
                         routes_need_kind: bool = False) -> str:
    if kind:
        return kind
    if tracker.is_task_id(tid):
        row = task_store.task_get(tid)
        return str((row or {}).get("kind") or "")
    if routes_need_kind:
        try:
            return str(tracker.for_ticket(cfg, repo, tid).get(tid).kind or "")
        except Exception:
            return ""
    return ""


def _earlier_adw(run_id: str) -> str:
    row = run_store.run_get(run_id)
    if row and row.get("adw"):
        return str(row["adw"])
    from .store import orchestration

    if found := orchestration.find(run_id):
        return str(found["data"].get("mode") or "orchestrate")
    return ""


def _build_decision(cfg: config.Config | None, name: str, rule_id: str, *, continues: str = "",
                    overrides: dict[str, dict] | None = None, config_dir: Path | None = None,
                    routes_version: str = "", routes_level: str = "", answered: bool = False) -> RouteDecision:
    if name in PYTHON_ADWS:
        return RouteDecision(
            adw=name,
            rule=rule_id,
            version=python_adw_version(name),
            loaded=None,
            continues=continues,
            overrides=overrides or {},
            routes_version=routes_version,
            routes_level=routes_level,
            answered=answered,
        )
    loaded = load(cfg, name, config_dir=config_dir)
    return RouteDecision(
        adw=loaded.name,
        rule=rule_id,
        version=version(loaded),
        loaded=loaded,
        continues=continues,
        overrides=overrides or {},
        routes_version=routes_version,
        routes_level=routes_level,
        answered=answered,
    )


def _match_routes(cfg: config.Config, repo: config.Repo, tid: str, routes: Routes, *,
                  continues: str = "", kind: str = "", config_dir: Path | None = None,
                  newest: dict | None = None, feedback: list[dict] | None = None,
                  has_repair: bool | None = None) -> RouteDecision:
    if newest is None:
        newest = run_store.newest_whole_run(repo.name, str(tid))
    if feedback is None:
        feedback = pipeline.pending_feedback(repo.name, str(tid))
    events_set: set[str] = set()
    if newest and newest["status"] == "interrupted":
        events_set.add("interrupted")
    if feedback:
        events_set.add("feedback")
    if has_repair is None:
        has_repair = bool(newest and pipeline.repaired_by(newest["id"]))
    needs_kind = any("kind" in r.when for r in routes.rules)
    ticket_kind = _resolve_ticket_kind(cfg, repo, str(tid), kind=kind, routes_need_kind=needs_kind)
    for rule in routes.rules:
        if _matches_rule(rule, events=events_set, has_repair=has_repair, repo_name=repo.name, ticket_kind=ticket_kind):
            parsed_set = parse_rule_set(rule.set) if rule.set else {}
            return _build_decision(
                cfg, rule.adw, rule.id,
                continues=continues,
                overrides=parsed_set,
                config_dir=config_dir,
                routes_version=routes.version,
                routes_level=routes.level,
            )
    raise AdwError(f"no route rule in {routes.path} matched {repo.name} {tid}")


def route(cfg: config.Config, repo: config.Repo, tid: str | int, *, requested: str = "",
          continues: str = "", repairs: str = "", dry_run: bool = False, force: bool = False,
          kind: str = "", config_dir: Path | None = None, spec: bool = False,
          layer: config.RepoLayer | None = None, read_only: bool = False) -> RouteDecision:
    tid_s = str(tid)
    # 1. Explicit --adw NAME -> @asked
    if requested:
        return _build_decision(cfg, requested, "@asked", continues=continues, config_dir=config_dir)
    # 2. Explicit --repair RUN -> @repair
    if repairs:
        repaired_adw = _earlier_adw(repairs) if repairs != "repair" else ""
        target = "orchestrate" if repaired_adw == "orchestrate" else "repair"
        return _build_decision(cfg, target, "@repair", config_dir=config_dir)
    # 3. Explicit --continue RUN -> @continue (or fall back to rules when earlier has NULL adw)
    if continues:
        if earlier := _earlier_adw(continues):
            return _build_decision(cfg, earlier, "@continue", continues=continues, config_dir=config_dir)
    # 4. Answered run check -> @continue (when not dry_run and not already continuing)
    if not dry_run and not continues:
        if asked := pipeline.answered_run(repo.name, tid_s):
            if asked_adw := _earlier_adw(asked["id"]):
                return _build_decision(
                    cfg, asked_adw, "@continue",
                    continues=asked["id"], config_dir=config_dir, answered=True,
                )
    # 5. Preserve durable issue orchestrator for specs & enrolled orchestrated lineages (#946/#949)
    if not tracker.is_task_id(tid_s) and (spec or spec_store.find(repo.name, tid_s)):
        return _build_decision(cfg, "orchestrate", "default", continues=continues, config_dir=config_dir)
    newest = run_store.newest_whole_run(repo.name, tid_s)
    feedback = pipeline.pending_feedback(repo.name, tid_s)
    if not continues:
        from . import requeue
        from .store import orchestration as orch_store

        policy_restart = (
            requeue.policy_retry(run_store.newest_live_whole_run(repo.name, tid_s))
            or (dry_run and bool(run_store.held_environment_branch(repo.name, tid_s)))
        )
        if newest and newest["status"] == "interrupted" and _earlier_adw(newest["id"]) == "orchestrate":
            prev = "" if policy_restart else (orch_store.find(newest["id"]) or {}).get("current_run", newest["id"])
            return _build_decision(
                cfg, "orchestrate", "@continue" if prev else "default", continues=prev, config_dir=config_dir,
            )
        if feedback and (source := next(iter(task_store.open_pr_runs(repo.name, tid_s)), None)):
            if _earlier_adw(source["id"]) == "orchestrate":
                prev = ("" if (force or policy_restart)
                        else (orch_store.find(source["id"]) or {}).get("current_run", source["id"]))
                return _build_decision(
                    cfg, "orchestrate", "feedback-revise", continues=prev, config_dir=config_dir,
                )
        elif layer is not None and layer.factory_path == pipeline.ORCHESTRATED:
            if not (newest and newest["status"] == "interrupted" and _earlier_adw(newest["id"]) not in ("", "orchestrate")):
                return _build_decision(cfg, "orchestrate", "default", config_dir=config_dir)
    # 6. Evaluate routes/adw.toml
    routes = load_routes(cfg, config_dir=config_dir)
    return _match_routes(
        cfg, repo, tid_s, routes,
        continues=continues,
        kind=kind,
        config_dir=config_dir,
        newest=newest,
        feedback=feedback,
    )


def _offline_ticket_kind(repo_name: str, tid: str, run_id: str) -> str:
    if tracker.is_task_id(tid):
        row = task_store.task_get(tid)
        if row and row.get("kind"):
            return str(row["kind"])
    task_line = factory.first(run_lines.read(run_id), "task")
    return str(task_line.get("kind") or "")


def dry_run_routes(cfg: config.Config, draft_routes: Routes, *, config_dir: Path | None = None,
                   limit_per_repo: int = 50) -> list[dict]:
    live_routes = load_routes(cfg, config_dir=config_dir)
    repo_names = list(dict.fromkeys([r.name for r in cfg.repos]))
    changes: list[dict] = []
    for repo_name in repo_names:
        repo = cfg.repo(repo_name)
        recent = run_store.runs(kind="pipeline", repo=repo_name, limit=limit_per_repo)
        seen_issues: set[str] = set()
        for row in recent:
            tid = str(row.get("issue") or "")
            if not tid or tid in seen_issues:
                continue
            seen_issues.add(tid)
            newest = run_store.newest_whole_run(repo_name, tid)
            feedback = pipeline.pending_feedback(repo_name, tid)
            has_repair = bool(newest and pipeline.repaired_by(newest["id"]))
            kind = _offline_ticket_kind(repo_name, tid, row["id"])
            live_decision = _match_routes(
                cfg, repo, tid, live_routes, kind=kind, config_dir=config_dir,
                newest=newest, feedback=feedback, has_repair=has_repair,
            )
            draft_decision = _match_routes(
                cfg, repo, tid, draft_routes, kind=kind, config_dir=config_dir,
                newest=newest, feedback=feedback, has_repair=has_repair,
            )
            if (live_decision.adw, live_decision.rule) != (draft_decision.adw, draft_decision.rule):
                changes.append({
                    "repo": repo_name,
                    "issue": tid,
                    "kind": kind,
                    "before": {"adw": live_decision.adw, "rule": live_decision.rule},
                    "after": {"adw": draft_decision.adw, "rule": draft_decision.rule},
                })
    return changes


def can_auto_activate(kind: str, actor: str) -> bool:
    entry = configkinds.REGISTRY.get(kind)
    if entry is None:
        return False
    return not (entry.gate == "review" and actor.startswith("run:"))


def check_draft(kind: str, data: object, *, cfg: config.Config | None = None,
                config_dir: Path | None = None, extra_adws: set[str] | None = None,
                where: str = "", actor: str = "") -> dict:
    """Reusable validator and stage-6 dry-run report for config kinds 'adw' and 'routes'.

    Structured so #296 (configcheck.py) can call it directly for Stage 3 (schema & references)
    and Stage 6 (dry run / outline).
    """
    gate = configkinds.REGISTRY[kind].gate if kind in configkinds.REGISTRY else "review"
    requires_accept = not can_auto_activate(kind, actor) if actor else gate == "review"
    if kind == "adw":
        loc = where or "adw"
        compiled, errors = _validate_and_steps(data, where=loc)
        if errors:
            return {
                "status": "invalid",
                "stage": "schema_and_references",
                "stage_number": 3,
                "errors": errors,
                "gate": gate,
                "requires_owner_accept": requires_accept,
                "stages": {
                    "schema_and_references": {"status": "invalid", "errors": errors},
                    "dry_run": {"status": "skipped"},
                },
            }
        assert isinstance(data, dict)
        assert compiled is not None
        raw = repr(data).encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        adw_obj = Adw(
            name=str(data["name"]),
            steps=tuple(compiled),
            path=Path(loc),
            level="draft",
            raw=raw,
            sha256=sha,
            version=f"adw/{data['name']}@{sha[:12]}",
        )
        nodes = outline(adw_obj)
        return {
            "status": "valid",
            "stage": None,
            "errors": [],
            "gate": gate,
            "requires_owner_accept": requires_accept,
            "stages": {
                "schema_and_references": {"status": "valid", "errors": []},
                "dry_run": {"status": "valid", "outline": nodes},
            },
        }
    if kind == "routes":
        loc = where or "routes/adw.toml"
        errors = validate_routes(data, cfg=cfg, config_dir=config_dir, extra_adws=extra_adws, where=loc)
        if errors:
            return {
                "status": "invalid",
                "stage": "schema_and_references",
                "stage_number": 3,
                "errors": errors,
                "gate": gate,
                "requires_owner_accept": requires_accept,
                "stages": {
                    "schema_and_references": {"status": "invalid", "errors": errors},
                    "dry_run": {"status": "skipped"},
                },
            }
        assert isinstance(data, dict)
        raw = repr(data).encode("utf-8")
        draft_routes = parse_routes_data(
            data, path=Path(loc), level="draft", raw=raw,
            config_dir=config_dir, extra_adws=extra_adws,
        )
        cfg_use = cfg if cfg is not None else config.load()
        changed = dry_run_routes(cfg_use, draft_routes, config_dir=config_dir)
        return {
            "status": "valid",
            "stage": None,
            "errors": [],
            "gate": gate,
            "requires_owner_accept": requires_accept,
            "stages": {
                "schema_and_references": {"status": "valid", "errors": []},
                "dry_run": {"status": "valid", "changes": changed},
            },
        }
    raise AdwError(f"unsupported check_draft kind {kind!r}")
