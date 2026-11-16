from __future__ import annotations

import json


def lists_read(row: dict, keys: tuple[str, ...]) -> dict:
    return {**row, **{k: json.loads(row.get(k) or "[]") for k in keys}}


def lists_written(row: dict, columns: tuple[str, ...], keys: tuple[str, ...]) -> tuple:
    return tuple(json.dumps(row.get(k) or []) if k in keys else row.get(k) for k in columns)


def insert_sql(table: str, columns: tuple[str, ...], verb: str = "INSERT") -> str:
    return f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
