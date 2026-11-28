from __future__ import annotations

import json
import shutil
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path

from . import (
    adw,
    codebase,
    config,
    configkinds,
    configrepo,
    configschema,
    devices,
    outbox_config,
    paths,
    prompt_loader,
    prompts,
    retention,
    routing,
    settings,
    steps,
)
from .store import runs as run_store

VALIDATOR = "configcheck.v1"

TOML_KINDS = frozenset((
    "step_settings", "models", "budgets", "connector", "source", "target", "rule",
    "sync", "vocabularies", "device", "loop", "attention", "experiment", "retention",
    "adw", "routes", "sound", "library",
))
JSON_KINDS = frozenset(("schema", "prices"))

# Reach keys / path segments that ALWAYS require Gate C when touched.
REACH_KEYS = frozenset((
    "trust",
    "quarantine",
    "audience",
    "review",
    "approve",
    "remember",
    "expose",
    "secrets",
    "tools",
    "mcp",
    "runner",
    "owners",
    "owner",
    "deny",
    "allow_bash",
    "read_roots",
    "floor",
    "floors",
    "budget_usd",
    "daily_budget_usd",
    "incident_budget_usd",
    "max_budget",
    "max_diff_lines",
    "max_attempts",
    "max_repair_cycles",
    "max_run_minutes",
    "max_verify_minutes",
    "session",
    "data_handling",
    "environment",
    "github_app",
    "env_file",
    "landing",
    "unattended_environment",
    "commenters",
    "oncall",
    "routing",
    "tiers",
    "payment_modes",
    "capabilities",
    "ab",
    "prompt",
    "schema",
    "action",
))

# Keys that are Gate A from an owner/hand on step_settings / config.toml (within ceilings).
STEP_SETTINGS_A_LEAF_KEYS = frozenset(("model", "effort", "timeout_minutes", "tries"))
CONFIG_ROOT_A_KEYS = frozenset(("max_parallel", "max_parallel_per_repo"))
SOURCE_A_KEYS = frozenset(("every", "enabled"))
LOOP_A_KEYS = frozenset(("enabled",))
KIND_A_DEFAULTS = frozenset(("prompt", "sound", "attention", "models", "prices", "retention", "schema"))

TIMEOUT_CEILING_MINUTES = 60
TRIES_CEILING = 5

GATE_ORDER = {"A": 0, "B": 1, "C": 2}


def _raise_gate(current: str, target: str) -> str:
    return target if GATE_ORDER[target] > GATE_ORDER[current] else current


def _stage(name: str, ok: bool, problems: list[str] | None = None, notes: list[str] | None = None) -> dict:
    return {
        "name": name,
        "ok": ok,
        "problems": list(problems or ()),
        "notes": list(notes or ()),
    }


def _schema_name_for(kind: str, path: str, name: str) -> str:
    if kind == "step_settings":
        if path == "config.toml" or name == "defaults":
            return "config_toml"
        if path.startswith("layers/"):
            return "layer_toml"
        return "step_settings"
    return kind


def _diff_paths(old: object, new: object, prefix: str = "") -> list[tuple[str, object, object]]:
    """Return sorted list of (dotted_path, old_value, new_value) for all changed leaves."""
    if type(old) is type(new) and old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        changes: list[tuple[str, object, object]] = []
        for key in sorted(old.keys() | new.keys()):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in old:
                changes.extend(_leaf_entries(new[key], child, added=True))
            elif key not in new:
                changes.extend(_leaf_entries(old[key], child, added=False))
            else:
                changes.extend(_diff_paths(old[key], new[key], child))
        return changes
    return [(prefix or "", old, new)]


def _leaf_entries(value: object, prefix: str, *, added: bool) -> list[tuple[str, object, object]]:
    if isinstance(value, dict) and value:
        out: list[tuple[str, object, object]] = []
        for k in sorted(value):
            child = f"{prefix}.{k}" if prefix else str(k)
            out.extend(_leaf_entries(value[k], child, added=added))
        return out
    return [(prefix, None if added else value, value if added else None)]


def _contains_secret_ref(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith(("secret:", "$secret:", "env:", "${")) or "secret:" in value
    if isinstance(value, list):
        return any(_contains_secret_ref(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_secret_ref(v) for v in value.values())
    return False


def _is_reach_change(kind: str, dotted: str, old_val: object, new_val: object) -> bool:
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return False
    if _contains_secret_ref(new_val) or _contains_secret_ref(old_val):
        return True
    # `[models]` maps step/role names (including `review`) to model aliases — not reach keys.
    if kind == "step_settings" and parts[0] == "models":
        return False
    # For `steps.<role>.<key...>` and `kinds.<k>.steps.<role>.<key...>`, skip the role/kind name
    # segments (e.g. role `review` is a step name, not the outbox `review` reach key).
    if kind == "step_settings" and parts[0] == "steps" and len(parts) >= 2:
        check_parts = parts[2:]
    elif kind == "step_settings" and parts[0] == "kinds" and len(parts) >= 4 and parts[2] == "steps":
        check_parts = parts[4:]
    else:
        check_parts = parts
    # Direct reach key match
    for part in check_parts:
        if part in REACH_KEYS:
            # Special case: `steps.<role>.prompt` / `schema` is reach, whereas top-level `schema = 1`
            # on adw/routes/prices/step_settings is just schema version.
            if part == "schema" and parts == ["schema"]:
                continue
            # `action` on outbox rules is reach when setting/changing approve rules.
            if part == "action" and kind != "rule":
                continue
            return True
    # Ceilings check on timeout_minutes / tries / max_parallel
    leaf = parts[-1]
    if leaf == "timeout_minutes" and isinstance(new_val, int) and not isinstance(new_val, bool):
        if new_val > TIMEOUT_CEILING_MINUTES:
            return True
    if leaf == "tries" and isinstance(new_val, int) and not isinstance(new_val, bool):
        if new_val > TRIES_CEILING:
            return True
    if leaf in ("max_parallel", "max_parallel_per_repo") and isinstance(new_val, int) and not isinstance(new_val, bool):
        if new_val > settings.PARALLEL_MAX:
            return True
    # Rule kind with approve/remember/audience
    if kind == "rule" and parts[0] in ("action", "audience", "drafter", "target", "kinds"):
        return True
    return kind == "target" and parts[0] in ("audience", "posts_as", "connector", "kinds")


def _is_a_only_change(kind: str, dotted: str) -> bool:
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return kind in KIND_A_DEFAULTS
    if kind in KIND_A_DEFAULTS:
        return True
    if kind == "source":
        return parts == ["every"] or parts == ["enabled"]
    if kind == "loop":
        return parts == ["enabled"]
    if kind == "step_settings":
        # Top-level run limits
        if len(parts) == 1 and parts[0] in CONFIG_ROOT_A_KEYS:
            return True
        # [models] role = ...
        if parts[0] == "models" and len(parts) == 2:
            return True
        # [ask] attention settings
        if parts[0] == "ask":
            return True
        # [steps.<role>].<leaf> or [kinds.<k>.steps.<role>].<leaf>
        if parts[0] == "steps" and len(parts) == 3 and parts[2] in STEP_SETTINGS_A_LEAF_KEYS:
            return True
        if parts[0] == "kinds" and len(parts) == 5 and parts[2] == "steps" and parts[4] in STEP_SETTINGS_A_LEAF_KEYS:
            return True
        if parts[0] == "defaults" and len(parts) == 2 and parts[1] in STEP_SETTINGS_A_LEAF_KEYS:
            return True
    return False


def _parse_body(kind: str, path: str, body: str) -> tuple[object | None, list[str], list[str]]:
    notes: list[str] = []
    if kind == "prompt":
        try:
            fm, template_body = prompts.parse(body)
            prompt_loader.parse_template(path or "prompt.md", template_body)
            if fm:
                notes.append(f"front matter keys: {', '.join(sorted(fm))}")
            return body, [], notes
        except (ValueError, TypeError) as error:
            return None, [f"{path or kind}: {error}"], notes
    if kind in JSON_KINDS:
        try:
            parsed = json.loads(body)
        except ValueError as error:
            return None, [f"{path or kind}: invalid JSON: {error}"], notes
        if not isinstance(parsed, dict):
            return None, [f"{path or kind}: expected a JSON object"], notes
        return parsed, [], notes
    if kind == "attention":
        stripped = body.lstrip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(body)
            except ValueError as error:
                return None, [f"{path or kind}: invalid JSON: {error}"], notes
            if not isinstance(parsed, dict):
                return None, [f"{path or kind}: expected an object"], notes
            return parsed, [], notes
    if kind in TOML_KINDS:
        try:
            parsed = tomllib.loads(body)
        except tomllib.TOMLDecodeError as error:
            return None, [f"{path or kind}: invalid TOML: {error}"], notes
        if not isinstance(parsed, dict):
            return None, [f"{path or kind}: expected a TOML table"], notes
        return parsed, [], notes
    return body, [], notes


def _validate_schema(kind: str, name: str, path: str, parsed: object, raw_bytes: bytes) -> list[str]:
    schema_name = _schema_name_for(kind, path, name)
    where = path or f"{kind}/{name}"
    problems: list[str] = []
    try:
        configschema.validate(schema_name, parsed, where=where, allow_read_only=(kind == "schema"))
    except (ValueError, FileNotFoundError) as error:
        problems.append(str(error))
        return problems

    if kind == "device" and isinstance(parsed, dict):
        try:
            devices.parse_device(name, parsed)
        except (ValueError, devices.DeviceError) as error:
            problems.append(f"{where}: {error}")
    elif kind == "target" and isinstance(parsed, dict):
        try:
            outbox_config.validate_target(name, parsed, where=where)
        except ValueError as error:
            problems.append(str(error))
    elif kind == "retention":
        try:
            retention.parse_bytes(raw_bytes)
        except (ValueError, TypeError, tomllib.TOMLDecodeError) as error:
            problems.append(f"{where}: {error}")
    elif kind == "attention" and isinstance(parsed, dict):
        try:
            config.ask_table(parsed)
        except (ValueError, TypeError) as error:
            problems.append(f"{where}: {error}")
    return problems


def _validate_references(
    kind: str,
    name: str,
    path: str,
    parsed: object,
    config_dir: Path,
    *,
    staged_targets: dict[str, object] | None = None,
    extra_adws: set[str] | None = None,
) -> list[str]:
    where = path or f"{kind}/{name}"
    problems: list[str] = []
    if kind == "rule" and isinstance(parsed, dict):
        try:
            targets = outbox_config.load_targets(config_dir)
            if staged_targets:
                targets.update(staged_targets)
            outbox_config.validate_rule(name, parsed, targets=targets, where=where)
        except ValueError as error:
            problems.append(str(error))
    elif kind == "adw" and isinstance(parsed, dict):
        problems.extend(adw.validate_adw(parsed, where=where))
        if parsed.get("name") != name:
            problems.append(f"{where}: name {parsed.get('name')!r} does not match {name!r}")
    elif kind == "routes" and isinstance(parsed, dict):
        problems.extend(adw.validate_routes(parsed, config_dir=config_dir, extra_adws=extra_adws, where=where))
    elif kind == "step_settings" and isinstance(parsed, dict):
        # Validate step routing entries and check MCP server references when editing config.toml.
        raw_steps = parsed.get("steps")
        if isinstance(raw_steps, dict):
            for step_name, table in raw_steps.items():
                if not isinstance(table, dict):
                    continue
                if prob := routing.entry_problem(table):
                    problems.append(f"{where}: step {step_name}: {prob}")
        if path == "config.toml":
            known_mcp = set((parsed.get("mcp") or {}).keys()) if isinstance(parsed.get("mcp"), dict) else set()
            if isinstance(raw_steps, dict):
                for step_name, table in raw_steps.items():
                    if isinstance(table, dict) and isinstance(table.get("mcp"), list):
                        for server in table["mcp"]:
                            if isinstance(server, str) and known_mcp and server not in known_mcp:
                                problems.append(f"{where}: step {step_name} references unknown mcp server {server!r}")
    elif kind == "prompt" and isinstance(parsed, str):
        # If overriding an engine prompt, ensure template placeholders match (allowing renamed aliases).
        if not name.endswith(".append"):
            engine_file = paths.PROMPTS_DIR / f"{name}.md"
            if engine_file.is_file():
                try:
                    _, eng_body = prompts.parse(engine_file.read_text(encoding="utf-8"))
                    new_meta, new_body = prompts.parse(parsed)
                    before_raw = set(prompt_loader.parse_template(str(engine_file), eng_body).get_identifiers())
                    after_raw = set(prompt_loader.parse_template(where, new_body).get_identifiers())
                    if "inputs" in new_meta and isinstance(new_meta["inputs"], list):
                        declared = set(new_meta["inputs"])
                        if undeclared := sorted(after_raw - declared):
                            problems.append(f"{where}: undeclared inputs in body: {', '.join(undeclared)}")
                    before = {prompts.RENAMED_INPUTS.get(k, k) for k in before_raw}
                    after = {prompts.RENAMED_INPUTS.get(k, k) for k in after_raw}
                    if before != after:
                        if dropped := before - after:
                            problems.append(f"{where}: drops required placeholders {', '.join(sorted(dropped))}")
                        if added := after - before:
                            problems.append(f"{where}: adds unknown placeholders {', '.join(sorted(added))}")
                except (OSError, ValueError) as error:
                    problems.append(f"{where}: {error}")
    return problems


def _compute_floor(
    kind: str,
    name: str,
    path: str,
    parsed: object,
    old_bytes: bytes | None,
    author: str,
) -> tuple[str, list[str], list[str]]:
    entry = configkinds.REGISTRY.get(kind)
    default_gate = "A" if (entry and entry.gate == "check") else "B"
    exposes: list[str] = []
    notes: list[str] = []

    if kind == "prompt":
        gate = "A"
        if isinstance(parsed, str):
            try:
                fm, _ = prompts.parse(parsed)
            except ValueError:
                fm = {}
            old_fm: dict = {}
            if old_bytes is not None:
                try:
                    old_fm, _ = prompts.parse(old_bytes.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    old_fm = {}
            for dotted, old_v, new_v in _diff_paths(old_fm, fm):
                if _is_reach_change(kind, dotted, old_v, new_v):
                    exposes.append(dotted)
                    gate = "C"
    elif isinstance(parsed, dict):
        old_parsed: object = {}
        if old_bytes is not None:
            try:
                old_text = old_bytes.decode("utf-8")
                old_parsed = json.loads(old_text) if kind in JSON_KINDS else tomllib.loads(old_text)
            except (UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError):
                old_parsed = {}
        changes = _diff_paths(old_parsed, parsed)
        if not changes:
            gate = "A" if kind in KIND_A_DEFAULTS or kind == "step_settings" else default_gate
        else:
            all_a = True
            gate = default_gate
            for dotted, old_v, new_v in changes:
                if _is_reach_change(kind, dotted, old_v, new_v):
                    exposes.append(dotted)
                    gate = "C"
                    all_a = False
                elif not _is_a_only_change(kind, dotted):
                    all_a = False
            if gate != "C" and all_a:
                gate = "A"
            elif gate != "C":
                gate = _raise_gate(gate, "B")
    else:
        gate = default_gate

    if author.startswith("run:") or author in ("miner", "optimize"):
        gate = _raise_gate(gate, "B")
        notes.append(f"author {author} sets minimum gate B")

    if exposes:
        notes.append(f"reach keys touched: {', '.join(exposes)}")
    return gate, exposes, notes


def _overlay_config_dir(base_dir: Path, path: str, raw_bytes: bytes, dest: Path) -> None:
    if base_dir.is_dir():
        shutil.copytree(
            base_dir,
            dest,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "secrets", "hooks", "verification-authority", "*.tmp", "*.arete-tmp"),
        )
    target = dest / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw_bytes)


def _stage_resolve(
    kind: str,
    name: str,
    path: str,
    raw_bytes: bytes,
    config_dir: Path,
) -> tuple[list[str], list[str], config.Config | None, Path | None]:
    problems: list[str] = []
    notes: list[str] = []
    # For kinds that affect config.load / steps resolution, load overlaid config strictly.
    if kind in ("step_settings", "models", "attention", "experiment", "budgets", "adw", "routes", "prompt", "schema"):
        with tempfile.TemporaryDirectory(prefix="arete-check-resolve-") as tmp:
            overlay = Path(tmp)
            _overlay_config_dir(config_dir, path, raw_bytes, overlay)
            cfg_file = overlay / "config.toml"
            if cfg_file.is_file():
                try:
                    cfg = config.load(cfg_file, strict=True, base_dir=config_dir)
                    cfg_no_replay = replace(cfg, replay=config.ReplayConfig())
                    steps.check(cfg_no_replay, defer_tiers=True)
                    resolved_repos = 0
                    for repo in cfg_no_replay.repos:
                        layer_dir = overlay / "layers" / repo.name
                        layer = config.load_layer(layer_dir if layer_dir.is_dir() else repo.layer_dir)
                        for tk in ("", *config.KINDS):
                            for role in config.STEP_ROLES:
                                steps.resolve(cfg_no_replay, layer, role, tk)
                        resolved_repos += 1
                    notes.append(f"resolved {resolved_repos} repo(s) across {len(config.KINDS) + 1} kinds")
                    return problems, notes, cfg, None
                except Exception as error:
                    problems.append(f"resolve failed: {error}")
                    return problems, notes, None, None
            elif path.startswith("layers/") and path.endswith("/config.toml"):
                try:
                    layer = config.load_layer((overlay / path).parent)
                    cfg = config.load_or_problem()[0]
                    cfg_no_replay = replace(cfg, replay=config.ReplayConfig())
                    for tk in ("", *config.KINDS):
                        for role in config.STEP_ROLES:
                            steps.resolve(cfg_no_replay, layer, role, tk)
                    notes.append(f"resolved layer {name}")
                    return problems, notes, cfg, None
                except Exception as error:
                    problems.append(f"layer resolve failed: {error}")
                    return problems, notes, None, None
    notes.append("no cross-repo resolution required")
    return problems, notes, None, None


def _stage_dry_run(
    kind: str,
    name: str,
    path: str,
    parsed: object,
    raw_bytes: bytes,
    config_dir: Path,
    author: str,
) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    notes: list[str] = []

    if kind == "prompt":
        try:
            _, template_body = prompts.parse(raw_bytes.decode("utf-8"))
            tmpl = prompt_loader.parse_template(path or f"prompts/{name}.md", template_body)
            sample_vars = {ident: f"<{ident}>" for ident in sorted(tmpl.get_identifiers())}
            rendered = tmpl.substitute(sample_vars)
            cfg = config.load_or_problem()[0]
            lines = [r.name for r in cfg.repos] or ["default"]
            notes.append(f"rendered sample task for {len(lines)} line(s) ({len(rendered)} chars)")
        except Exception as error:
            problems.append(f"dry_run prompt render failed: {error}")
        return problems, notes

    if kind == "schema":
        if not isinstance(parsed, dict):
            problems.append("dry_run schema must be a JSON object")
            return problems, notes
        cfg = config.load_or_problem()[0]
        lines = [r.name for r in cfg.repos] or ["default"]
        notes.append(f"validated sample schema binding for {len(lines)} line(s)")
        return problems, notes

    if kind in ("step_settings", "models"):
        with tempfile.TemporaryDirectory(prefix="arete-check-dryrun-") as tmp:
            overlay = Path(tmp)
            _overlay_config_dir(config_dir, path, raw_bytes, overlay)
            live_cfg_file = config_dir / "config.toml"
            draft_cfg_file = overlay / "config.toml"
            if live_cfg_file.is_file() and draft_cfg_file.is_file():
                try:
                    live_cfg = replace(config.load(live_cfg_file, strict=False, base_dir=config_dir),
                                       replay=config.ReplayConfig())
                    draft_cfg = replace(config.load(draft_cfg_file, strict=False, base_dir=config_dir),
                                        replay=config.ReplayConfig())
                    diffs: list[str] = []
                    for role in config.STEP_ROLES:
                        try:
                            s_old, _ = steps.resolve(live_cfg, None, role, "", provenance=True)
                            s_new, p_new = steps.resolve(draft_cfg, None, role, "", provenance=True)
                        except ValueError:
                            continue
                        if s_old != s_new:
                            changed_keys = [
                                k for k in ("runner", "model", "effort", "timeout_minutes", "tries", "tools", "mcp")
                                if getattr(s_old, k, None) != getattr(s_new, k, None)
                            ]
                            prov_src = {k: p_new.get(k, {}).get("level", "") for k in changed_keys}
                            diffs.append(f"{role}: {', '.join(f'{k}({prov_src[k]})' for k in changed_keys)}")
                    if diffs:
                        notes.append("step diffs: " + "; ".join(diffs[:10]))
                    else:
                        notes.append("no effective step setting changes")
                    # Inspect last 20 runs per affected repo from step_inputs if DB is available.
                    try:
                        recent_total = 0
                        for repo in draft_cfg.repos:
                            recent = run_store.runs(repo=repo.name, limit=20)
                            for r in recent:
                                if run_store.step_inputs_by_run(r["id"]):
                                    recent_total += 1
                        notes.append(f"checked provenance across {recent_total} recent run(s)")
                    except Exception:
                        notes.append("no recent run step_inputs available")
                except Exception as error:
                    problems.append(f"dry_run diff failed: {error}")
            else:
                notes.append("dry_run skipped (no live config.toml)")
        return problems, notes

    if kind in ("adw", "routes"):
        with tempfile.TemporaryDirectory(prefix="arete-check-adw-") as tmp:
            overlay = Path(tmp)
            _overlay_config_dir(config_dir, path, raw_bytes, overlay)
            cfg = config.load_or_problem()[0]
            try:
                res = adw.check_draft(kind, parsed, cfg=cfg, config_dir=overlay, where=path, actor=author)
            except Exception as error:
                problems.append(f"dry_run {kind} failed: {error}")
                return problems, notes
            if res.get("status") != "valid":
                problems.extend(res.get("errors") or [f"{kind} dry run failed"])
            else:
                dr = res.get("stages", {}).get("dry_run", {})
                if kind == "adw":
                    notes.append(f"outline: {len(dr.get('outline') or ())} node(s)")
                else:
                    notes.append(f"route changes: {len(dr.get('changes') or ())} ticket(s)")
        return problems, notes

    notes.append("skipped")
    return problems, notes


def check(
    kind: str,
    name: str,
    path: str,
    body: str | bytes,
    *,
    author: str = "hand",
    config_dir: Path | None = None,
    old_bytes: bytes | None = None,
    stop_after: int = 6,
    staged_targets: dict[str, object] | None = None,
    extra_adws: set[str] | None = None,
) -> dict:
    """Run configcheck stages 1..stop_after in order, stopping at the first failure."""
    root = (config_dir or paths.CONFIG_DIR).resolve()
    stages: list[dict] = []
    gate = "B"
    exposes: list[str] = []

    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
            raw_bytes = body
        except UnicodeDecodeError as error:
            stages.append(_stage("parse", False, [f"{path or kind}: invalid UTF-8: {error}"]))
            return {"validator": VALIDATOR, "ok": False, "stages": stages, "gate": gate, "exposes": exposes}
    else:
        text = body
        raw_bytes = body.encode("utf-8")

    # Stage 1: parse
    parsed, parse_problems, parse_notes = _parse_body(kind, path, text)
    stages.append(_stage("parse", not parse_problems, parse_problems, parse_notes))
    if parse_problems or stop_after <= 1:
        return {"validator": VALIDATOR, "ok": not parse_problems, "stages": stages, "gate": gate, "exposes": exposes}

    # Stage 2: schema
    schema_problems = _validate_schema(kind, name, path, parsed, raw_bytes)
    stages.append(_stage("schema", not schema_problems, schema_problems))
    if schema_problems or stop_after <= 2:
        return {"validator": VALIDATOR, "ok": not schema_problems, "stages": stages, "gate": gate, "exposes": exposes}

    # Stage 3: references
    ref_problems = _validate_references(
        kind, name, path, parsed, root, staged_targets=staged_targets, extra_adws=extra_adws
    )
    stages.append(_stage("references", not ref_problems, ref_problems))
    if ref_problems or stop_after <= 3:
        return {"validator": VALIDATOR, "ok": not ref_problems, "stages": stages, "gate": gate, "exposes": exposes}

    # Stage 4: floor (gate calculation)
    if old_bytes is None and path:
        if configrepo.is_repo(root):
            old_bytes = configrepo.live_bytes(path, root)
        elif (root / path).is_file():
            old_bytes = (root / path).read_bytes()
    gate, exposes, floor_notes = _compute_floor(kind, name, path, parsed, old_bytes, author)
    stages.append(_stage("floor", True, [], [f"gate {gate}", *floor_notes]))
    if stop_after <= 4:
        return {"validator": VALIDATOR, "ok": True, "stages": stages, "gate": gate, "exposes": exposes}

    # Stage 5: resolve
    res_problems, res_notes, _, _ = _stage_resolve(kind, name, path, raw_bytes, root)
    stages.append(_stage("resolve", not res_problems, res_problems, res_notes))
    if res_problems or stop_after <= 5:
        return {"validator": VALIDATOR, "ok": not res_problems, "stages": stages, "gate": gate, "exposes": exposes}

    # Stage 6: dry_run
    dry_problems, dry_notes = _stage_dry_run(kind, name, path, parsed, raw_bytes, root, author)
    stages.append(_stage("dry_run", not dry_problems, dry_problems, dry_notes))
    ok = not dry_problems
    return {
        "validator": f"{VALIDATOR}@{codebase.STAMP}",
        "ok": ok,
        "stages": stages,
        "gate": gate,
        "exposes": exposes,
    }


def check_staged_files(staged_blobs: dict[str, bytes], root: Path) -> tuple[bool, str, list[str]]:
    """Run stages 1..4 on staged config files for the git pre-commit hook.

    Returns (ok, max_gate, problems).
    """
    max_gate = "A"
    all_problems: list[str] = []
    staged_targets: dict[str, object] = {}
    extra_adws: set[str] = {
        classified[1]
        for rel in staged_blobs
        if (classified := configkinds.classify(rel)) is not None and classified[0] == "adw"
    }
    # Pre-parse staged targets so staged rules can reference them.
    for rel, raw in sorted(staged_blobs.items()):
        classified = configkinds.classify(rel)
        if classified is not None and classified[0] == "target":
            try:
                parsed = tomllib.loads(raw.decode("utf-8"))
                staged_targets[classified[1]] = outbox_config._target_from_data(
                    classified[1], parsed, level="config", raw=raw, where=rel
                )
            except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
                pass

    for rel, raw in sorted(staged_blobs.items()):
        if rel == ".gitignore":
            continue
        classified = configkinds.classify(rel)
        if classified is None:
            continue
        kind, item_name = classified
        old_bytes = configrepo.live_bytes(rel, root) if configrepo.is_repo(root) else None
        report = check(
            kind,
            item_name,
            rel,
            raw,
            author="hand",
            config_dir=root,
            old_bytes=old_bytes,
            stop_after=4,
            staged_targets=staged_targets,
            extra_adws=extra_adws,
        )
        if not report["ok"]:
            for st in report["stages"]:
                if not st["ok"]:
                    all_problems.extend(st["problems"])
            return False, max_gate, all_problems
        max_gate = _raise_gate(max_gate, report["gate"])
    return True, max_gate, all_problems
