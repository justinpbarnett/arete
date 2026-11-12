from __future__ import annotations

import json
import re
import stat

from . import config, paths, util

PARALLEL_MAX = 8
RUN_LIMITS = {"max_parallel": "Runs at once", "max_parallel_per_repo": "Runs per repo"}
MODEL_ALIASES = ("opus", "sonnet", "haiku")
MODEL_ID = re.compile(r"(claude:)?claude-[a-z0-9.-]+(\[1m\])?|(pi:)?[a-z0-9._-]+/[A-Za-z0-9._:-]+|"
                      r"(?:codex:)?(?:gpt-|codex-)[A-Za-z0-9._:-]+|codex:[A-Za-z0-9._-]+")
TABLE_HEADER = re.compile(r"\s*\[")


class Refused(ValueError):
    pass


def current() -> dict:
    cfg = config.load()
    return {**{key: getattr(cfg, key) for key in RUN_LIMITS}, "parallel_max": PARALLEL_MAX, "aliases": MODEL_ALIASES,
            "models": {role: cfg.models.get(role, "") for role in config.MODEL_ROLES},
            "defaults": {role: config.MODEL_FALLBACKS.get(role, "") for role in config.MODEL_ROLES}}


def instead_of(text: str) -> str:
    return f", not '{util.first_line(text, 40)}'." if text else "."


def run_limit_from(key: str, value) -> int:
    text = value.strip() if isinstance(value, str) else json.dumps(value)
    if not text.isdecimal() or len(text) > len(str(PARALLEL_MAX)) or not 1 <= int(text) <= PARALLEL_MAX:
        raise Refused(f"{RUN_LIMITS[key]} takes a whole number from 1 to {PARALLEL_MAX}{instead_of(text)}")
    return int(text)


def model_from(role, value) -> str:
    if role not in config.MODEL_ROLES:
        raise Refused(f"'{util.first_line(str(role), 40)}' is not a model role arete reads.")
    text = str(value).strip()
    if text and text not in MODEL_ALIASES and not MODEL_ID.fullmatch(text):
        raise Refused(f"The {role.replace('_', ' ')} model takes opus, sonnet, haiku, a claude model id, a pi model "
                      f"as provider/model, a codex model id or codex:<model>, or nothing for its default{instead_of(text)}")
    return text


def save(values: dict) -> dict:
    wanted_limits = {key: run_limit_from(key, values[key]) for key in RUN_LIMITS if key in values}
    models = values.get("models") or {}
    if not isinstance(models, dict):
        raise Refused("Models takes an object that maps each role to its model.")
    wanted_models = {role: model_from(role, m) for role, m in models.items()}
    with util.lock("config", wait=True):
        lines = paths.CONFIG_FILE.read_text().splitlines(keepends=True)
        for key, limit in wanted_limits.items():
            set_key(lines, None, key, str(limit))
        for role, model in wanted_models.items():
            if model:
                set_key(lines, "models", role, f'"{model}"')
            else:
                drop_key(lines, "models", role)
        replace_config("".join(lines), wanted_limits, wanted_models)
    return current()


def replace_config(text: str, wanted_limits: dict[str, int], wanted_models: dict[str, str]) -> None:
    draft = paths.CONFIG_FILE.with_suffix(".toml.tmp")
    mode = stat.S_IMODE(paths.CONFIG_FILE.stat().st_mode)
    draft.touch(mode=mode)
    draft.chmod(mode)
    draft.write_text(text)
    try:
        cfg = config.load(draft)
    except Exception as e:
        draft.unlink()
        raise Refused(f"The new config would not load ({util.first_line(str(e), 120)}), so config.toml is unchanged.") from e
    if any(getattr(cfg, key) != limit for key, limit in wanted_limits.items()) or any(
            cfg.models.get(role, "") != model for role, model in wanted_models.items()):
        draft.unlink()
        raise Refused("The new config read back other values than the ones saved, so config.toml is unchanged.")
    draft.replace(paths.CONFIG_FILE)


def set_key(lines: list[str], table: str | None, key: str, value: str) -> None:
    start, end = table_span(lines, table)
    if start is None:
        lines += ["\n" if lines and lines[-1].strip() else "", f"[{table}]\n", f"{key} = {value}\n"]
        return
    for i in range(start, end):
        if m := key_line(key).match(lines[i]):
            lines[i] = f"{m.group(1)}{value}{m.group(2)}{m.group(3)}"
            return
    last = max((i for i in range(start, end) if lines[i].strip()), default=start - 1)
    if last >= 0 and not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, f"{key} = {value}\n")


def drop_key(lines: list[str], table: str | None, key: str) -> None:
    start, end = table_span(lines, table)
    if start is not None:
        lines[start:end] = [line for line in lines[start:end] if not key_line(key).match(line)]


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
