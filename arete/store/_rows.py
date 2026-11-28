from __future__ import annotations

import json
import re
from collections.abc import Iterable

SQL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
QUALIFIED_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
INSERT_VERBS = frozenset(("INSERT", "INSERT OR REPLACE", "INSERT OR IGNORE"))


def validate_identifier(name: str, *, qualified: bool = False) -> str:
    pattern = QUALIFIED_IDENT if qualified else SQL_IDENT
    if not isinstance(name, str) or not pattern.fullmatch(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def validate_columns(fields: Iterable[str], allowed: frozenset[str], table: str) -> None:
    bad = [col for col in fields if not isinstance(col, str) or col not in allowed]
    if bad:
        raise ValueError(f"invalid {table} fields: {', '.join(sorted(map(str, bad)))}")


def json_dict(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, (str, bytes, bytearray)) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def json_list(raw) -> list:
    if isinstance(raw, list):
        return list(raw)
    if not isinstance(raw, (str, bytes, bytearray)) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def lists_read(row: dict, keys: tuple[str, ...]) -> dict:
    return {**row, **{k: json_list(row.get(k)) for k in keys}}


def lists_written(row: dict, columns: tuple[str, ...], keys: tuple[str, ...]) -> tuple:
    return tuple(json.dumps(row.get(k) or []) if k in keys else row.get(k) for k in columns)


def insert_sql(table: str, columns: tuple[str, ...], verb: str = "INSERT") -> str:
    if verb not in INSERT_VERBS:
        raise ValueError(f"invalid SQL insert verb: {verb!r}")
    validate_identifier(table)
    if not columns:
        raise ValueError(f"insert into {table} requires at least one column")
    for col in columns:
        validate_identifier(col)
    return f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
