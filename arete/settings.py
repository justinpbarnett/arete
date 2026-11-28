from __future__ import annotations

import json
import re
import tempfile
import tomllib
from dataclasses import asdict, replace
from io import StringIO
from pathlib import Path

from . import config, configrepo, configschema, paths, principals, refusals, routing, steps, util
from .store import config as config_store
from .store import writes

PARALLEL_MAX = 8
RUN_LIMITS = {"max_parallel": "Runs at once", "max_parallel_per_repo": "Runs per repo"}
MODEL_ALIASES = ("opus", "sonnet", "haiku")
MODEL_ID = re.compile(r"(claude:)?claude-[a-z0-9.-]+(\[1m\])?|(pi:)?[a-z0-9._-]+/[A-Za-z0-9._:-]+|"
                      r"(?:codex:)?(?:gpt-|codex-)[A-Za-z0-9._:-]+|codex:[A-Za-z0-9._-]+")
ROLE_OWNED_FIELDS = ("runner", "model", "effort")
TABLE_HEADER = re.compile(r"^[ \t]*\[")
ASSIGNMENT = re.compile(r"""^(?:[A-Za-z0-9_. \t-]|"(?:[^"\\]|\\.)*"|'[^']*')+=[ \t]*""")
ASSIGNMENT_END = re.compile(r"[#\r\n]")
INLINE_FIELD_END = re.compile(r"[#,}\r\n]")
TOML_TRIVIA = re.compile(r"(?:[ \t\r\n]|#[^\r\n]*)*")


class Refused(refusals.Invalid, ValueError):
    pass


def current_version(conn=None) -> int:
    row = config_store.live_item("step_settings", "defaults", conn)
    if row is None:
        return 0
    return int(row.get("ver") or 0)


def _load_authoritative() -> tuple[config.Config, dict]:
    try:
        raw_bytes = paths.read_confined(paths.CONFIG_FILE)
    except FileNotFoundError:
        return config.Config(), {}
    try:
        raw_text = raw_bytes.decode("utf-8")
        raw_data = tomllib.loads(raw_text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        cfg, _ = config.load_or_problem()
        return cfg, {}
    with tempfile.TemporaryDirectory(prefix="arete-settings-read-") as tmp:
        draft = Path(tmp) / "config.toml"
        draft.write_bytes(raw_bytes)
        try:
            cfg = config.load(draft, strict=False, base_dir=paths.CONFIG_DIR)
        except Exception:
            cfg, _ = config.load_or_problem()
    return cfg, raw_data if isinstance(raw_data, dict) else {}


def _physical_steps(raw_data: dict) -> dict[str, tuple[str, dict]]:
    raw_steps = raw_data.get("steps")
    if not isinstance(raw_steps, dict):
        return {}
    found: dict[str, tuple[str, dict]] = {}
    for phys_key, table in raw_steps.items():
        if not isinstance(table, dict):
            continue
        canonical = config.STEP_ALIASES.get(phys_key, phys_key)
        if canonical in config.ROLE_BY_NAME:
            found[canonical] = (phys_key, table)
    return found


def role_overrides_from(raw_data: dict, role: str) -> dict:
    raw_models = raw_data.get("models")
    models_map: dict[str, str] = {}
    if role in config.MODEL_ROLES and isinstance(raw_models, dict) and role in raw_models:
        if isinstance(raw_models[role], str):
            models_map[role] = raw_models[role]
    steps_map: dict[str, dict[str, str]] = {}
    phys_by_role = _physical_steps(raw_data)
    if role in phys_by_role:
        phys_key, table = phys_by_role[role]
        steps_map[phys_key] = {k: table[k] for k in ROLE_OWNED_FIELDS if k in table and isinstance(table[k], str)}
    return {"models": models_map, "steps": steps_map}


def role_views(cfg: config.Config, raw_data: dict, *, layer: config.RepoLayer | None = None,
               task_kind: str = "", config_commit: str = "", layer_from: str = "") -> list[dict]:
    cfg_no_replay = replace(cfg, replay=config.ReplayConfig())
    views: list[dict] = []
    for role_info in config.ROLES:
        role = role_info.name
        try:
            resolved, prov = steps.resolve(
                cfg_no_replay, layer, role, task_kind,
                provenance=True, config_commit=config_commit, layer_from=layer_from,
            )
            runner_val = resolved.runner or None
            model_val = resolved.model or None
            effort_val = resolved.effort or None
            level_val = steps.role_level(resolved, prov)
        except ValueError:
            runner_val = model_val = effort_val = level_val = None
        views.append({
            "role": role,
            "runner": runner_val,
            "model": model_val,
            "effort": effort_val,
            "level": level_val,
            "overrides": role_overrides_from(raw_data, role),
        })
    return views


def resolved(repo: str = "", task_kind: str = "", node: str = "") -> dict:
    from . import pins

    if task_kind and task_kind not in config.KINDS:
        raise ValueError(f"unknown task_kind '{task_kind}'; use {', '.join(config.KINDS)}")
    cfg, raw_data = _load_authoritative()
    cfg_no_replay = replace(cfg, replay=config.ReplayConfig())
    commit = configrepo.live_sha() if configrepo.is_repo() else ""
    repo_obj = cfg_no_replay.repo(repo) if repo else None
    layer = config.load_layer(repo_obj.layer_dir) if repo_obj is not None else None
    layer_from = ""
    if layer is not None and layer.config_file.is_file():
        rel_layer = pins.relative_path(layer.config_file, repo_root=repo_obj.path if repo_obj else None)
        layer_from = f"{rel_layer}@{commit}" if commit else rel_layer
    values_out: dict[str, dict] = {}
    levels_out: dict[str, dict] = {}
    roles_out: list[dict] = []
    for role_info in config.ROLES:
        role = role_info.name
        try:
            res, prov = steps.resolve(
                cfg_no_replay, layer, role, task_kind,
                provenance=True, config_commit=commit, layer_from=layer_from,
            )
            vals: dict[str, object] = {}
            lvls: dict[str, dict] = {}
            for k, v in prov.items():
                if k in config.STEP_MAPS:
                    vals[k] = {ck: cv["value"] for ck, cv in v.items()}
                    for ck, cv in v.items():
                        lvls[f"{k}.{ck}"] = {"level": cv["level"], "from": cv["from"]}
                else:
                    vals[k] = v["value"]
                    lvls[k] = {"level": v["level"], "from": v["from"]}
            values_out[role] = vals
            levels_out[role] = lvls
            roles_out.append({
                "role": role,
                "runner": res.runner or None,
                "model": res.model or None,
                "effort": res.effort or None,
                "level": steps.role_level(res, prov),
                "overrides": role_overrides_from(raw_data, role),
            })
        except ValueError:
            values_out[role] = {}
            levels_out[role] = {}
            roles_out.append({
                "role": role,
                "runner": None,
                "model": None,
                "effort": None,
                "level": None,
                "overrides": role_overrides_from(raw_data, role),
            })
    return {
        "values": values_out,
        "levels": levels_out,
        "roles": roles_out,
        "config_commit": commit or None,
    }


def current(conn=None) -> dict:
    cfg, raw_data = _load_authoritative()
    ver = current_version(conn)
    commit = configrepo.live_sha() if configrepo.is_repo() else ""
    roles = role_views(cfg, raw_data, config_commit=commit)
    values = {
        **{key: getattr(cfg, key) for key in RUN_LIMITS},
        "parallel_max": PARALLEL_MAX,
        "aliases": list(MODEL_ALIASES),
        "models": {role: cfg.models.get(role, "") for role in config.MODEL_ROLES},
        "defaults": {role: config.MODEL_FALLBACKS.get(role, "") for role in config.MODEL_ROLES},
        "roles": roles,
    }
    return {"version": ver, "values": values, **values}


def attention() -> dict:
    return asdict(config.ask_table(config.read_data().get("ask", {})))


def save_attention(values: dict) -> dict:
    configschema.validate("attention", values)
    with configrepo.writer_lock() as fence:
        text = paths.read_confined(paths.CONFIG_FILE).decode()
        data = tomllib.loads(text)
        existing = config.table_of(data.pop("ask", {}), "ask")
        merged = {**existing, **values}
        wanted = config.ask_table(merged)
        if not values:
            return asdict(wanted)
        updated = with_attention(text, values)

        def refusal(cfg: config.Config) -> str:
            other = tomllib.loads(updated)
            other.pop("ask", None)
            if cfg.ask != wanted or repr(other) != repr(data):
                return "The new attention settings did not round trip through config.toml."
            return ""

        write_checked(updated, refusal, note="attention settings update", fence=fence)
        return asdict(wanted)


def with_attention(text: str, values: dict) -> str:
    kept, statement = [], ""
    remaining = dict(values)
    at_root, in_ask, dotted = True, False, False
    insertion = None
    for line in StringIO(text, newline="\n"):
        statement += line
        parsed = complete_toml(statement)
        if parsed is None:
            continue
        if statement.lstrip().startswith("["):
            at_root, in_ask = False, "ask" in parsed
            if in_ask:
                insertion = len(kept) + 1
        elif parsed and (in_ask or at_root and "ask" in parsed):
            start = ASSIGNMENT.match(statement).end()
            if at_root and tomllib.loads(statement[:start] + "0") == {"ask": 0}:
                statement = inline_attention(statement, start, remaining)
            else:
                dotted = at_root
                insertion = len(kept) + 1
                key, old = next(iter((parsed["ask"] if dotted else parsed).items()))
                if key in remaining:
                    value = remaining.pop(key)
                    if value != old or type(value) is not type(old):
                        end = attention_assignment_end(statement, 0, ASSIGNMENT_END)
                        statement = replace_attention_value(statement, start, end, value)
        kept.append(statement)
        statement = ""
    if remaining:
        newline = "\r\n" if "\r\n" in text else "\n"
        additions = []
        if insertion is None:
            insertion = len(kept)
            additions.append(f"[ask]{newline}")
        if insertion > 0 and not kept[insertion - 1].endswith("\n"):
            additions.insert(0, newline)
        prefix = "ask." if dotted else ""
        additions.extend(f"{prefix}{key} = {attention_value(value)}{newline}" for key, value in remaining.items())
        kept[insertion:insertion] = additions
    return "".join(kept)


def complete_toml(text: str) -> dict | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def attention_value(value) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def attention_assignment_end(text: str, start: int, delimiters: re.Pattern) -> int:
    return next((token.start() for token in delimiters.finditer(text, start)
                 if complete_toml(text[start:token.start()])), len(text))


def replace_attention_value(text: str, start: int, end: int, value) -> str:
    end = start + len(text[start:end].rstrip(" \t\r\n"))
    return text[:start] + attention_value(value) + text[end:]


def inline_attention(statement: str, start: int, remaining: dict) -> str:
    start += 1
    kept = [statement[:start]]
    while True:
        field_start = TOML_TRIVIA.match(statement, start).end()
        kept.append(statement[start:field_start])
        if field_start >= len(statement):
            raise Refused("The inline [ask] table in config.toml is malformed.")
        if statement[field_start] == "}":
            kept.append(inline_additions(remaining))
            break
        start = field_start
        end = attention_assignment_end(statement, start, INLINE_FIELD_END)
        field = statement[start:end]
        parsed_field = complete_toml(field)
        if not parsed_field:
            raise Refused("The inline [ask] table in config.toml is malformed.")
        key, old = next(iter(parsed_field.items()))
        if key in remaining:
            value = remaining.pop(key)
            if value != old or type(value) is not type(old):
                matched = ASSIGNMENT.match(field)
                if matched is None:
                    raise Refused("The inline [ask] table in config.toml is malformed.")
                field = replace_attention_value(field, matched.end(), len(field), value)
        trimmed = field.rstrip(" \t")
        delimiter = TOML_TRIVIA.match(statement, end).end()
        if delimiter >= len(statement):
            raise Refused("The inline [ask] table in config.toml is malformed.")
        if statement[delimiter] == "}":
            trivia = field[len(trimmed):] + statement[end:delimiter]
            if remaining:
                addition = inline_additions(remaining)
                trivia = f",{trivia}{addition}" if "#" in trivia else f", {addition}{trivia}"
            kept.extend((trimmed, trivia))
            field_start = delimiter
            break
        kept.extend((field, statement[end:delimiter + 1]))
        start = delimiter + 1
    kept.append(statement[field_start:])
    return "".join(kept)


def inline_additions(remaining: dict) -> str:
    additions = ", ".join(f"{key} = {attention_value(value)}" for key, value in remaining.items())
    remaining.clear()
    return additions


def instead_of(text: str) -> str:
    return f", not '{util.first_line(text, 40)}'." if text else "."


def run_limit_from(key: str, value) -> int:
    if isinstance(value, bool):
        raise Refused(f"{RUN_LIMITS[key]} takes a whole number from 1 to {PARALLEL_MAX}.")
    text = value.strip() if isinstance(value, str) else json.dumps(value)
    if not (text.isascii() and text.isdecimal()) or len(text) > len(str(PARALLEL_MAX)) or not 1 <= int(text) <= PARALLEL_MAX:
        raise Refused(f"{RUN_LIMITS[key]} takes a whole number from 1 to {PARALLEL_MAX}{instead_of(text)}")
    return int(text)


def _valid_model_text(text: str) -> bool:
    return text in MODEL_ALIASES or bool(MODEL_ID.fullmatch(text))


def model_from(role, value) -> str:
    if role not in config.MODEL_ROLES:
        raise Refused(f"'{util.first_line(str(role), 40)}' is not a model role arete reads.")
    text = str(value).strip()
    if text and not _valid_model_text(text):
        raise Refused(f"The {role.replace('_', ' ')} model takes opus, sonnet, haiku, a claude model id, a pi model "
                      f"as provider/model, a codex model id or codex:<model>, or nothing for its default{instead_of(text)}")
    return text


def _validate_raw_model(role: str, value: str, *, allow_empty: bool) -> str:
    if type(value) is not str:
        raise Refused(f"Model for {role} must be a string.")
    if not value:
        if allow_empty:
            return value
        raise Refused(f"Model for {role} cannot be empty; use null to clear it.")
    if not _valid_model_text(value):
        raise Refused(f"The {role.replace('_', ' ')} model takes opus, sonnet, haiku, a claude model id, a pi model "
                      f"as provider/model, a codex model id or codex:<model>{instead_of(value)}")
    return value


def _validate_raw_runner(role: str, value: str) -> str:
    if type(value) is not str or value not in routing.RUNNERS:
        raise Refused(f"Runner for {role} must be one of {', '.join(routing.RUNNERS)}.")
    return value


def _validate_raw_effort(role: str, value: str) -> str:
    if type(value) is not str or not value or value not in routing.EFFORT_ORDER:
        raise Refused(f"Effort for {role} must be one of {', '.join(routing.EFFORT_ORDER)}.")
    return value


def _validate_role_patches(raw_roles) -> list[dict]:
    if not isinstance(raw_roles, list):
        raise Refused("roles takes a list of role patches.")
    seen: set[str] = set()
    validated: list[dict] = []
    for index, entry in enumerate(raw_roles):
        if not isinstance(entry, dict):
            raise Refused(f"roles[{index}] must be an object.")
        role = entry.get("role")
        if type(role) is not str or role not in config.ROLE_BY_NAME:
            raise Refused(f"roles[{index}].role must be a canonical role name.")
        if role in seen:
            raise Refused(f"Duplicate role patch for '{role}'.")
        seen.add(role)
        if "restore" in entry:
            extra = set(entry) - {"role", "restore"}
            if extra:
                raise Refused(f"roles[{index}]: restore cannot be combined with {', '.join(sorted(extra))}.")
            restore = entry["restore"]
            if not isinstance(restore, dict) or set(restore) != {"models", "steps"}:
                raise Refused(f"roles[{index}].restore requires exactly models and steps maps.")
            r_models, r_steps = restore["models"], restore["steps"]
            if not isinstance(r_models, dict) or len(r_models) > 1:
                raise Refused(f"roles[{index}].restore.models must be an object with at most one entry.")
            if not isinstance(r_steps, dict) or len(r_steps) > 1:
                raise Refused(f"roles[{index}].restore.steps must be an object with at most one entry.")
            clean_models: dict[str, str] = {}
            for mk, mv in r_models.items():
                if type(mk) is not str or mk not in config.MODEL_ROLES or mk != role:
                    raise Refused(f"roles[{index}].restore.models key '{mk}' is not valid for role '{role}'.")
                if type(mv) is not str:
                    raise Refused(f"roles[{index}].restore.models['{mk}'] must be a string, not null or another type.")
                clean_models[mk] = _validate_raw_model(role, mv, allow_empty=True)
            clean_steps: dict[str, dict[str, str]] = {}
            for sk, sv in r_steps.items():
                if type(sk) is not str or sk not in config.STEP_NAMES or config.STEP_ALIASES.get(sk, sk) != role:
                    raise Refused(f"roles[{index}].restore.steps key '{sk}' is not a physical step key for role '{role}'.")
                if not isinstance(sv, dict):
                    raise Refused(f"roles[{index}].restore.steps['{sk}'] must be an object.")
                unknown = set(sv) - set(ROLE_OWNED_FIELDS)
                if unknown:
                    raise Refused(f"roles[{index}].restore.steps['{sk}'] contains unsupported fields: {', '.join(sorted(unknown))}.")
                clean_inner: dict[str, str] = {}
                for fk, fv in sv.items():
                    if type(fv) is not str:
                        raise Refused(f"roles[{index}].restore.steps['{sk}'].{fk} must be a string.")
                    if fk == "runner":
                        clean_inner[fk] = _validate_raw_runner(role, fv)
                    elif fk == "model":
                        clean_inner[fk] = _validate_raw_model(role, fv, allow_empty=False)
                    else:
                        clean_inner[fk] = _validate_raw_effort(role, fv)
                clean_steps[sk] = clean_inner
            validated.append({"role": role, "restore": {"models": clean_models, "steps": clean_steps}})
        else:
            extra = set(entry) - {"role", *ROLE_OWNED_FIELDS}
            if extra:
                raise Refused(f"roles[{index}] contains unsupported fields: {', '.join(sorted(extra))}.")
            clean: dict = {"role": role}
            for fk in ROLE_OWNED_FIELDS:
                if fk not in entry:
                    continue
                fv = entry[fk]
                if fv is None:
                    clean[fk] = None
                elif fk == "runner":
                    clean[fk] = _validate_raw_runner(role, fv)
                elif fk == "model":
                    clean[fk] = _validate_raw_model(role, fv, allow_empty=False)
                else:
                    clean[fk] = _validate_raw_effort(role, fv)
            validated.append(clean)
    return validated


def _find_physical_step_key(raw_data: dict, role: str) -> str | None:
    raw_steps = raw_data.get("steps")
    if not isinstance(raw_steps, dict):
        return None
    matches = [k for k in raw_steps if config.STEP_ALIASES.get(k, k) == role]
    if len(matches) > 1:
        raise Refused(f"config.toml defines conflicting step tables for '{role}': {', '.join(sorted(matches))}.")
    return matches[0] if matches else None


def _apply_role_patches(lines: list[str], patches: list[dict]) -> dict[str, dict]:
    expected_overrides: dict[str, dict] = {}
    for patch in patches:
        role = patch["role"]
        raw_data = tomllib.loads("".join(lines))
        current_phys = _find_physical_step_key(raw_data, role)
        if "restore" in patch:
            restore = patch["restore"]
            r_models: dict[str, str] = restore["models"]
            r_steps: dict[str, dict[str, str]] = restore["steps"]
            if role in config.MODEL_ROLES:
                drop_key(lines, "models", role)
            saved_phys = next(iter(r_steps), None)
            if saved_phys is not None:
                saved_fields = r_steps[saved_phys]
                if current_phys is None:
                    ensure_table(lines, f"steps.{saved_phys}")
                elif current_phys != saved_phys:
                    rename_table(lines, f"steps.{current_phys}", f"steps.{saved_phys}")
                table_name = f"steps.{saved_phys}"
                for fk in ROLE_OWNED_FIELDS:
                    drop_key(lines, table_name, fk)
                for fk in ROLE_OWNED_FIELDS:
                    if fk in saved_fields:
                        set_key(lines, table_name, fk, json.dumps(saved_fields[fk], ensure_ascii=False))
            else:
                if current_phys is not None:
                    table_name = f"steps.{current_phys}"
                    for fk in ROLE_OWNED_FIELDS:
                        drop_key(lines, table_name, fk)
                    drop_table_if_empty(lines, table_name, current_phys)
            for mk, mv in r_models.items():
                set_key(lines, "models", mk, json.dumps(mv, ensure_ascii=False))
            after_raw = tomllib.loads("".join(lines))
            expected_step = {}
            if saved_phys is not None:
                expected_step[saved_phys] = dict(r_steps[saved_phys])
            elif current_phys is not None and current_phys in (after_raw.get("steps") or {}):
                expected_step[current_phys] = {}
            expected_overrides[role] = {"models": dict(r_models), "steps": expected_step}
        else:
            has_non_null = any(patch.get(fk) is not None for fk in ROLE_OWNED_FIELDS if fk in patch)
            phys = current_phys
            if phys is None and has_non_null:
                phys = role
            if "model" in patch and role in config.MODEL_ROLES:
                drop_key(lines, "models", role)
            if phys is not None:
                table_name = f"steps.{phys}"
                for fk in ROLE_OWNED_FIELDS:
                    if fk not in patch:
                        continue
                    if patch[fk] is None:
                        drop_key(lines, table_name, fk)
                    else:
                        set_key(lines, table_name, fk, json.dumps(patch[fk], ensure_ascii=False))
    return expected_overrides


def save(values: dict, *, base_version: int | None = None, actor: str | None = None,
         source: str = "dashboard", idem: str = "", presence_passed: bool = False) -> dict:
    from . import drafts

    if not isinstance(values, dict):
        raise Refused("Settings values must be an object.")
    if "models" in values and "roles" in values:
        raise Refused("Supply models and roles in separate requests.")
    wanted_limits = {key: run_limit_from(key, values[key]) for key in RUN_LIMITS if key in values}
    wanted_models: dict[str, str] = {}
    if "models" in values:
        models = values.get("models")
        if models is None:
            models = {}
        if not isinstance(models, dict):
            raise Refused("Models takes an object that maps each role to its model.")
        wanted_models = {role: model_from(role, m) for role, m in models.items()}
    role_patches = _validate_role_patches(values["roles"]) if "roles" in values else []

    with configrepo.writer_lock() as fence:
        configrepo.ensure_repo(fence=fence)
        configrepo.reconcile(fence=fence)
        configrepo.commit_hand_edits(fence=fence)
        live_ver = current_version()
        if base_version is not None:
            writes.validate_version(base_version)
            if base_version != live_ver:
                raise writes.VersionConflict(current())
        text, base_blob = configrepo.read("config.toml")
        lines = text.splitlines(keepends=True)
        for key, limit in wanted_limits.items():
            set_key(lines, None, key, str(limit))
        for role, model in wanted_models.items():
            if model:
                set_key(lines, "models", role, json.dumps(model, ensure_ascii=False))
            else:
                drop_key(lines, "models", role)
        expected_overrides = _apply_role_patches(lines, role_patches) if role_patches else {}
        updated = "".join(lines)

        def refusal(cfg: config.Config) -> str:
            if any(getattr(cfg, key) != limit for key, limit in wanted_limits.items()) or any(
                    cfg.models.get(role, "") != model for role, model in wanted_models.items()):
                return "The new config read back other values than the ones saved, so config.toml is unchanged."
            if expected_overrides:
                parsed = tomllib.loads(updated)
                for role, expected in expected_overrides.items():
                    if role_overrides_from(parsed, role) != expected:
                        return f"Role '{role}' did not round trip cleanly through config.toml."
            return ""

        check_draft(updated, refusal, strict_steps=bool(role_patches))

        if paths.DB_FILE.exists() and drafts.available():
            caller = actor or principals.current()
            draft_row, report = drafts.create_and_check(
                "step_settings",
                "defaults",
                updated,
                author=caller,
                path="config.toml",
                base_version=live_ver,
                why="settings update",
                run_check=True,
            )
            if not report or not report["ok"]:
                probs = [p for st in (report or {}).get("stages", ()) for p in st.get("problems", ())]
                raise Refused("; ".join(probs) or "The new config failed configcheck.")
            drafts.activate(
                draft_row["id"],
                caller,
                live_ver,
                idem=idem,
                presence_passed=presence_passed,
                source=source,
                fence=fence,
            )
        else:
            replace_config(
                updated,
                wanted_limits,
                wanted_models,
                expected_overrides=expected_overrides,
                base=base_blob,
                actor=actor,
                source=source,
                idem=idem,
                fence=fence,
                strict_steps=bool(role_patches),
            )
    return current()


def replace_config(text: str, wanted_limits: dict[str, int], wanted_models: dict[str, str], *,
                   expected_overrides: dict[str, dict] | None = None, base: str | None = None,
                   actor: str | None = None, source: str = "dashboard", idem: str = "", fence=None,
                   strict_steps: bool = False) -> None:
    def refusal(cfg: config.Config) -> str:
        if any(getattr(cfg, key) != limit for key, limit in wanted_limits.items()) or any(
                cfg.models.get(role, "") != model for role, model in wanted_models.items()):
            return "The new config read back other values than the ones saved, so config.toml is unchanged."
        if expected_overrides:
            parsed = tomllib.loads(text)
            for role, expected in expected_overrides.items():
                if role_overrides_from(parsed, role) != expected:
                    return f"Role '{role}' did not round trip cleanly through config.toml."
        return ""

    write_checked(text, refusal, base=base, actor=actor, source=source, idem=idem, note="settings update",
                  fence=fence, strict_steps=strict_steps)


def check_draft(text: str, refusal=None, *, strict_steps: bool = False) -> config.Config:
    with tempfile.TemporaryDirectory(prefix="arete-settings-check-") as tmp:
        draft = Path(tmp) / "config.toml"
        draft.write_text(text, encoding="utf-8")
        try:
            cfg = config.load(draft, strict=True, base_dir=paths.CONFIG_DIR)
            if strict_steps:
                for step_name, table in cfg.steps.items():
                    if problem := routing.entry_problem(table):
                        raise ValueError(f"step {step_name}: {problem}")
                steps.check(replace(cfg, replay=config.ReplayConfig()), defer_tiers=True)
        except Exception as e:
            raise Refused(f"The new config would not load ({util.first_line(str(e), 120)}), so config.toml is unchanged.") from e
    if refusal is not None and (why := refusal(cfg)):
        raise Refused(why)
    return cfg


def write_checked(text: str, refusal, *, base: str | None = None, actor: str | None = None,
                  source: str = "dashboard", idem: str = "", note: str = "config update", fence=None,
                  strict_steps: bool = False) -> str:
    check_draft(text, refusal, strict_steps=strict_steps)
    with configrepo.writer_lock(fence) as owned:
        configrepo.ensure_repo(fence=owned)
        if base is None:
            _, base = configrepo.read("config.toml")
        return configrepo.commit(
            {"config.toml": text},
            {"config.toml": base},
            actor=actor or principals.current(),
            source=source,
            idem=idem,
            note=note,
            fence=owned,
        )


def set_key(lines: list[str], table: str | None, key: str, value: str) -> None:
    start, end = table_span(lines, table)
    if start is None:
        lines += ["\n" if lines and lines[-1].strip() else "", f"[{table}]\n", f"{key} = {value}\n"]
        return
    for i in range(start, end):
        if m := key_line(key).match(lines[i]):
            lines[i] = f"{m.group(1)}{value}{m.group(2)}{m.group(3) or chr(10)}"
            return
    last = max((i for i in range(start, end) if lines[i].strip()), default=start - 1)
    if last >= 0 and not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, f"{key} = {value}\n")


def drop_key(lines: list[str], table: str | None, key: str) -> None:
    start, end = table_span(lines, table)
    if start is not None:
        lines[start:end] = [line for line in lines[start:end] if not key_line(key).match(line)]


def ensure_table(lines: list[str], table: str) -> None:
    start, _ = table_span(lines, table)
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"[{table}]\n")


def rename_table(lines: list[str], old_table: str, new_table: str) -> None:
    start, _ = table_span(lines, old_table)
    if start is None:
        ensure_table(lines, new_table)
        return
    header_idx = start - 1
    pat = re.compile(rf"(\s*)\[{re.escape(old_table)}\](.*)", re.DOTALL)
    if m := pat.fullmatch(lines[header_idx]):
        lines[header_idx] = f"{m.group(1)}[{new_table}]{m.group(2)}"


def drop_table_if_empty(lines: list[str], table: str, key_name: str) -> None:
    start, end = table_span(lines, table)
    if start is None:
        return
    header_idx = start - 1
    segment = "".join(lines[header_idx:end])
    try:
        parsed = tomllib.loads(segment)
    except tomllib.TOMLDecodeError:
        return
    section = parsed.get("steps", {}).get(key_name) if table.startswith("steps.") else parsed.get(key_name)
    if section == {}:
        del lines[header_idx:end]


def key_line(key: str) -> re.Pattern:
    return re.compile(rf"(\s*{re.escape(key)}\s*=\s*)[^#\r\n]*?(\s*(?:#[^\r\n]*)?)(\r?\n?)\Z")


def table_span(lines: list[str], table: str | None) -> tuple[int | None, int]:
    headers = [i for i, line in enumerate(lines) if TABLE_HEADER.match(line)]
    if table is None:
        return 0, headers[0] if headers else len(lines)
    own = re.compile(rf"\s*\[{re.escape(table)}\]\s*(#.*)?\s*")
    start = next((i + 1 for i in headers if own.fullmatch(lines[i])), None)
    if start is None:
        return None, len(lines)
    return start, next((i for i in headers if i >= start), len(lines))
