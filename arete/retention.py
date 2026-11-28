from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from . import configschema, paths

FILENAME = "retention.toml"

TABLE_ORDER = (
    "firings",
    "queue",
    "records",
    "folders",
    "events_log",
    "idempotency",
    "loop_events",
    "incidents",
    "dead_letters",
    "events_file",
    "source_polls",
    "sources",
    "db",
)


@dataclass(frozen=True)
class DaysLimit:
    days: float


@dataclass(frozen=True)
class RecordsLimit:
    days: float
    max_gb: float


@dataclass(frozen=True)
class EventsLogLimit:
    notice_days: float
    derived_days: float


@dataclass(frozen=True)
class IncidentsLimit:
    days: float
    notes_days: float
    keep_days: float


@dataclass(frozen=True)
class SourcesLimit:
    unused_days: float


@dataclass(frozen=True)
class DbLimit:
    max_mb: float


@dataclass(frozen=True)
class Policy:
    firings: DaysLimit
    queue: DaysLimit
    records: RecordsLimit
    folders: DaysLimit
    events_log: EventsLogLimit
    idempotency: DaysLimit
    loop_events: DaysLimit
    incidents: IncidentsLimit
    dead_letters: DaysLimit
    events_file: DaysLimit
    source_polls: DaysLimit
    sources: SourcesLimit
    db: DbLimit


def policy_file(config_dir: Path | None = None) -> Path:
    return (config_dir or paths.CONFIG_DIR) / FILENAME


def _schema() -> dict:
    return configschema.schema("retention")


def _positive_number(value: object, where: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{where} must be a number above 0, not {value!r}")
    return float(value)


def _validate_tables(data: object, *, where_prefix: str = "") -> dict[str, dict[str, float]]:
    if not isinstance(data, dict):
        raise ValueError(f"{where_prefix or FILENAME} must be a TOML table")
    root_props = _schema()["properties"]
    if bad_tables := sorted(set(data) - set(root_props)):
        raise ValueError(
            f"{where_prefix}unknown retention table {', '.join(bad_tables)}; "
            f"use {', '.join(root_props)}"
        )
    cleaned: dict[str, dict[str, float]] = {}
    for table_name, table_value in data.items():
        if not isinstance(table_value, dict):
            raise ValueError(f"[{table_name}] must be a table, not {table_value!r}")
        key_props = root_props[table_name]["properties"]
        if bad_keys := sorted(set(table_value) - set(key_props)):
            raise ValueError(
                f"[{table_name}]: unknown key {', '.join(bad_keys)}; "
                f"use {', '.join(key_props)}"
            )
        cleaned[table_name] = {
            key: _positive_number(val, f"[{table_name}] {key}")
            for key, val in table_value.items()
        }
    return cleaned


@cache
def _default_tables() -> dict[str, dict[str, float]]:
    raw = (paths.DEFAULTS_DIR / FILENAME).read_bytes()
    parsed = _validate_tables(tomllib.loads(raw.decode("utf-8")), where_prefix="defaults/retention.toml: ")
    root_props = _schema()["properties"]
    for table_name, table_schema in root_props.items():
        if table_name not in parsed or set(parsed[table_name]) != set(table_schema["properties"]):
            raise ValueError(f"defaults/retention.toml is incomplete for [{table_name}]")
    return parsed


def _build_policy(merged: dict[str, dict[str, float]]) -> Policy:
    if merged["records"]["days"] > merged["folders"]["days"]:
        raise ValueError("[records] days must not exceed [folders] days")
    return Policy(
        firings=DaysLimit(days=merged["firings"]["days"]),
        queue=DaysLimit(days=merged["queue"]["days"]),
        records=RecordsLimit(days=merged["records"]["days"], max_gb=merged["records"]["max_gb"]),
        folders=DaysLimit(days=merged["folders"]["days"]),
        events_log=EventsLogLimit(
            notice_days=merged["events_log"]["notice_days"],
            derived_days=merged["events_log"]["derived_days"],
        ),
        idempotency=DaysLimit(days=merged["idempotency"]["days"]),
        loop_events=DaysLimit(days=merged["loop_events"]["days"]),
        incidents=IncidentsLimit(
            days=merged["incidents"]["days"],
            notes_days=merged["incidents"]["notes_days"],
            keep_days=merged["incidents"]["keep_days"],
        ),
        dead_letters=DaysLimit(days=merged["dead_letters"]["days"]),
        events_file=DaysLimit(days=merged["events_file"]["days"]),
        source_polls=DaysLimit(days=merged["source_polls"]["days"]),
        sources=SourcesLimit(unused_days=merged["sources"]["unused_days"]),
        db=DbLimit(max_mb=merged["db"]["max_mb"]),
    )


def default_policy() -> Policy:
    return _build_policy({name: dict(values) for name, values in _default_tables().items()})


def parse_bytes(raw: bytes) -> Policy:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"{FILENAME} is not valid TOML: {e}") from e
    overrides = _validate_tables(data)
    merged = {name: dict(values) for name, values in _default_tables().items()}
    for table_name, values in overrides.items():
        merged[table_name].update(values)
    return _build_policy(merged)


def load(path: Path | None = None) -> Policy:
    target = path or policy_file()
    try:
        raw = paths.read_confined(target)
    except FileNotFoundError:
        return default_policy()
    return parse_bytes(raw)


def effective_tables(raw: bytes | None) -> dict[str, dict[str, float]]:
    """Return all retention tables with overrides from raw merged over engine defaults."""
    merged = {name: dict(values) for name, values in _default_tables().items()}
    if raw is not None:
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"{FILENAME} is not valid TOML: {e}") from e
        for table_name, values in _validate_tables(data).items():
            merged[table_name].update(values)
    return merged


def lowered_keys(old_bytes: bytes | None, new_bytes: bytes) -> list[str]:
    """Return sorted 'table.key' paths whose effective numeric limit decreased."""
    old_eff = effective_tables(old_bytes)
    new_eff = effective_tables(new_bytes)
    lowered: list[str] = []
    for table_name in TABLE_ORDER:
        old_tbl = old_eff[table_name]
        new_tbl = new_eff[table_name]
        for key in sorted(old_tbl):
            if new_tbl[key] < old_tbl[key]:
                lowered.append(f"{table_name}.{key}")
    return lowered


def load_live() -> Policy:
    """Load retention policy from git live when a config repo exists, else working retention.toml or defaults."""
    from . import configrepo, util
    from .store import db as foundation

    if configrepo.is_repo():
        if (not configrepo._ACTIVE_REPO_LOCK.get() and not foundation.in_write()
                and util.lock_is_held(configrepo.REPO_LOCK)):
            with util.lock(configrepo.REPO_LOCK, wait=True):
                live = configrepo.live_bytes(FILENAME)
        else:
            live = configrepo.live_bytes(FILENAME)
        if live is None:
            return default_policy()
        return parse_bytes(live)
    return load()


def _format_number(value: int | float) -> str:
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return f"{value:g}"


def format_toml(tables: dict[str, dict[str, int | float]]) -> str:
    blocks: list[str] = []
    root_props = _schema()["properties"]
    for table_name in TABLE_ORDER:
        values = tables.get(table_name)
        if not values:
            continue
        key_order = tuple(root_props[table_name]["properties"])
        lines = [f"[{table_name}]"]
        for key in key_order:
            if key in values:
                lines.append(f"{key} = {_format_number(values[key])}")
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def legacy_values(data: dict) -> dict[str, dict[str, int | float]]:
    """Extract legacy [retention] and [oncall] keep_days values mapped to retention.toml tables."""
    moved: dict[str, dict[str, int | float]] = {}
    old_ret = data.get("retention")
    if isinstance(old_ret, dict):
        mapping = (
            ("days", "records", "days"),
            ("max_gb", "records", "max_gb"),
            ("lines_days", "folders", "days"),
            ("db_max_mb", "db", "max_mb"),
        )
        for old_key, new_table, new_key in mapping:
            if old_key in old_ret:
                val = old_ret[old_key]
                if type(val) in (int, float) and math.isfinite(val) and val > 0:
                    moved.setdefault(new_table, {})[new_key] = val
    old_oncall = data.get("oncall")
    if isinstance(old_oncall, dict) and "keep_days" in old_oncall:
        val = old_oncall["keep_days"]
        if type(val) in (int, float) and math.isfinite(val) and val > 0:
            moved.setdefault("incidents", {})["keep_days"] = val
    return moved


def has_legacy_settings(data: dict) -> bool:
    if "retention" in data:
        return True
    old_oncall = data.get("oncall")
    return isinstance(old_oncall, dict) and "keep_days" in old_oncall


def merge_toml(existing_bytes: bytes | None, updates: dict[str, dict[str, int | float]]) -> bytes:
    existing: dict[str, dict[str, int | float]] = {}
    if existing_bytes is not None:
        raw_existing = tomllib.loads(existing_bytes.decode("utf-8"))
        for table_name, values in _validate_tables(raw_existing).items():
            existing[table_name] = dict(values)
    for table_name, values in updates.items():
        bucket = existing.setdefault(table_name, {})
        for key, val in values.items():
            bucket.setdefault(key, val)
    text = format_toml(existing)
    parse_bytes(text.encode("utf-8"))
    return text.encode("utf-8")


_TABLE_HEADER_RE = re.compile(r"^\s*\[(\[)?\s*([^\[\]#]+?)\s*\](\])?\s*(?:#.*)?$")
_KEEP_DAYS_RE = re.compile(r"^\s*keep_days\s*=")
_TOP_RETENTION_RE = re.compile(r"^\s*retention(?:\.[A-Za-z0-9_-]+)?\s*=")
_TOP_ONCALL_KEEP_RE = re.compile(r"^\s*oncall\.keep_days\s*=")


def strip_legacy_config_toml(text: str) -> str:
    """Remove [retention] table and [oncall] keep_days from config.toml text while preserving other content."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    section = ""
    oncall_start: int | None = None
    oncall_has_other = False
    oncall_removed_keep = False

    def flush_oncall() -> None:
        nonlocal oncall_start, oncall_has_other, oncall_removed_keep
        if oncall_start is not None and oncall_removed_keep and not oncall_has_other:
            del out[oncall_start:]
        oncall_start = None
        oncall_has_other = False
        oncall_removed_keep = False

    for line in lines:
        m = _TABLE_HEADER_RE.match(line)
        if m:
            is_array = bool(m.group(1) and m.group(3))
            name = m.group(2).strip()
            flush_oncall()
            section = f"[[{name}]]" if is_array else name
            if section == "retention" or section.startswith("retention."):
                continue
            if section == "oncall":
                oncall_start = len(out)
            out.append(line)
            continue
        if section == "retention" or section.startswith("retention."):
            continue
        if not section and (_TOP_RETENTION_RE.match(line) or _TOP_ONCALL_KEEP_RE.match(line)):
            continue
        if section == "oncall":
            if _KEEP_DAYS_RE.match(line):
                oncall_removed_keep = True
                continue
            if line.strip() and not line.lstrip().startswith("#"):
                oncall_has_other = True
        out.append(line)

    flush_oncall()
    result = "".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result).lstrip("\n")
    if result and not result.endswith("\n"):
        result += "\n"
    parsed = tomllib.loads(result) if result.strip() else {}
    if has_legacy_settings(parsed):
        raise ValueError("could not remove legacy retention settings from config.toml")
    return result
