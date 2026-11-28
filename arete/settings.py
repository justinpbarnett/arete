from __future__ import annotations

import json
import re
import stat
import tomllib
from dataclasses import asdict
from io import StringIO

from . import config, configschema, paths, refusals, util

PARALLEL_MAX = 8
RUN_LIMITS = {"max_parallel": "Runs at once", "max_parallel_per_repo": "Runs per repo"}
MODEL_ALIASES = ("opus", "sonnet", "haiku")
MODEL_ID = re.compile(r"(claude:)?claude-[a-z0-9.-]+(\[1m\])?|(pi:)?[a-z0-9._-]+/[A-Za-z0-9._:-]+|"
                      r"(?:codex:)?(?:gpt-|codex-)[A-Za-z0-9._:-]+|codex:[A-Za-z0-9._-]+")
TABLE_HEADER = re.compile(r"^[ \t]*\[")
ASSIGNMENT = re.compile(r"""^(?:[A-Za-z0-9_. \t-]|"(?:[^"\\]|\\.)*"|'[^']*')+=[ \t]*""")
ASSIGNMENT_END = re.compile(r"[#\r\n]")
INLINE_FIELD_END = re.compile(r"[#,}\r\n]")
TOML_TRIVIA = re.compile(r"(?:[ \t\r\n]|#[^\r\n]*)*")


class Refused(refusals.Invalid, ValueError):
    pass


def current() -> dict:
    cfg, _ = config.load_or_problem()
    return {**{key: getattr(cfg, key) for key in RUN_LIMITS}, "parallel_max": PARALLEL_MAX, "aliases": MODEL_ALIASES,
            "models": {role: cfg.models.get(role, "") for role in config.MODEL_ROLES},
            "defaults": {role: config.MODEL_FALLBACKS.get(role, "") for role in config.MODEL_ROLES}}


def attention() -> dict:
    return asdict(config.ask_table(config.read_data().get("ask", {})))


def save_attention(values: dict) -> dict:
    configschema.validate("attention", values)
    with util.lock("config", wait=True):
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

        write_checked(updated, refusal)
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
    text = value.strip() if isinstance(value, str) else json.dumps(value)
    if not (text.isascii() and text.isdecimal()) or len(text) > len(str(PARALLEL_MAX)) or not 1 <= int(text) <= PARALLEL_MAX:
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
    def refusal(cfg: config.Config) -> str:
        if any(getattr(cfg, key) != limit for key, limit in wanted_limits.items()) or any(
                cfg.models.get(role, "") != model for role, model in wanted_models.items()):
            return "The new config read back other values than the ones saved, so config.toml is unchanged."
        return ""

    write_checked(text, refusal)


def write_checked(text: str, refusal) -> None:
    def check(draft):
        try:
            cfg = config.load(draft, strict=True)
        except Exception as e:
            raise Refused(f"The new config would not load ({util.first_line(str(e), 120)}), so config.toml is unchanged.") from e
        if why := refusal(cfg):
            raise Refused(why)

    try:
        mode = stat.S_IMODE(paths.CONFIG_FILE.stat().st_mode)
    except FileNotFoundError:
        util.write_durably(paths.CONFIG_FILE, text.encode(), 0o600, check, replace=False)
    else:
        util.write_durably(paths.CONFIG_FILE, text.encode(), mode, check)


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
