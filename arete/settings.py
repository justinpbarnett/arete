from __future__ import annotations

import re

from . import config, paths, util

PARALLEL_MAX = 8
MODEL_ALIASES = ("opus", "sonnet", "haiku")
MODEL_ID = re.compile(r"claude-[a-z0-9.-]+(\[1m\])?")
TABLE_HEADER = re.compile(r"\s*\[")


class Refused(ValueError):
    pass


def current() -> dict:
    cfg = config.load()
    return {"max_parallel": cfg.max_parallel, "parallel_max": PARALLEL_MAX,
            "models": {role: cfg.model(role) for role in config.MODEL_ROLES}}


def not_this(text: str) -> str:
    return f", not '{util.first_line(text, 40)}'." if text else "."


def parallel_from(value) -> int:
    text = str(value).strip()
    if isinstance(value, bool) or not text.isdigit() or not 1 <= int(text) <= PARALLEL_MAX:
        raise Refused(f"Runs at once takes a whole number from 1 to {PARALLEL_MAX}{not_this(text)}")
    return int(text)


def model_from(role, value) -> str:
    if role not in config.MODEL_ROLES:
        raise Refused(f"'{util.first_line(str(role), 40)}' is not a model role arete reads.")
    text = str(value).strip()
    if text not in MODEL_ALIASES and not MODEL_ID.fullmatch(text):
        raise Refused(f"The {role.replace('_', ' ')} model takes opus, sonnet, haiku or a claude model id{not_this(text)}")
    return text


def save(values: dict) -> dict:
    wanted_parallel = parallel_from(values["max_parallel"]) if "max_parallel" in values else None
    wanted_models = {role: model_from(role, m) for role, m in (values.get("models") or {}).items()}
    with util.lock("config", wait=True):
        lines = paths.CONFIG_FILE.read_text().splitlines(keepends=True)
        if wanted_parallel is not None:
            set_key(lines, None, "max_parallel", str(wanted_parallel))
        for role, model in wanted_models.items():
            set_key(lines, "models", role, f'"{model}"')
        replace_config("".join(lines), wanted_parallel, wanted_models)
    return current()


def replace_config(text: str, wanted_parallel: int | None, wanted_models: dict[str, str]) -> None:
    draft = paths.CONFIG_FILE.with_suffix(".toml.tmp")
    draft.write_text(text)
    try:
        cfg = config.load(draft)
    except Exception as e:
        draft.unlink()
        raise Refused(f"The new config would not load ({util.first_line(str(e), 120)}), so config.toml is unchanged.") from e
    if (wanted_parallel is not None and cfg.max_parallel != wanted_parallel) or any(
            cfg.models.get(role) != model for role, model in wanted_models.items()):
        draft.unlink()
        raise Refused("The new config read back other values than the ones saved, so config.toml is unchanged.")
    draft.replace(paths.CONFIG_FILE)


def set_key(lines: list[str], table: str | None, key: str, value: str) -> None:
    start, end = table_span(lines, table)
    if start is None:
        lines += ["\n" if lines and lines[-1].strip() else "", f"[{table}]\n", f"{key} = {value}\n"]
        return
    key_line = re.compile(rf"(\s*{re.escape(key)}\s*=\s*)[^#\r\n]*?(\s*(?:#[^\r\n]*)?)(\r?\n?)\Z")
    for i in range(start, end):
        if m := key_line.match(lines[i]):
            lines[i] = f"{m.group(1)}{value}{m.group(2)}{m.group(3)}"
            return
    last = max((i for i in range(start, end) if lines[i].strip()), default=start - 1)
    if last >= 0 and not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, f"{key} = {value}\n")


def table_span(lines: list[str], table: str | None) -> tuple[int | None, int]:
    headers = [i for i, line in enumerate(lines) if TABLE_HEADER.match(line)]
    if table is None:
        return 0, headers[0] if headers else len(lines)
    own = re.compile(rf"\s*\[{re.escape(table)}\]\s*(#.*)?\s*")
    start = next((i + 1 for i in headers if own.fullmatch(lines[i])), None)
    if start is None:
        return None, len(lines)
    return start, next((i for i in headers if i >= start), len(lines))
