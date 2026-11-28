from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from datetime import UTC, datetime, timedelta
from typing import Protocol

from . import util

PASS_RUN_STATUSES = frozenset(("pr", "noop", "done"))


class MonetaryProjection(Protocol):
    capability: str

    def for_prompt(
        self, conn: sqlite3.Connection, name: str, start: str, end: str
    ) -> tuple[int | None, float | None, str]: ...

    def for_adw(
        self, conn: sqlite3.Connection, name: str, start: str, end: str
    ) -> tuple[int | None, float | None, str]: ...


MONETARY_PRODUCER: MonetaryProjection | None = None

_PROMPT_SQL = (
    "SELECT s.id, s.run_id, s.status, s.ok, s.seconds, s.ts, s.prompt_version, s.version, "
    "r.id, r.status, r.started, r.finished, r.adw, r.adw_version, r.stop_version, NULL "
    "FROM steps s LEFT JOIN runs r ON r.id = s.run_id "
    "WHERE s.prompt_version >= ? AND s.prompt_version < ? AND s.ts >= ? AND s.ts < ? "
    "ORDER BY s.id ASC"
)
_PROMPT_SQL_COST = (
    "SELECT s.id, s.run_id, s.status, s.ok, s.seconds, s.ts, s.prompt_version, s.version, "
    "r.id, r.status, r.started, r.finished, r.adw, r.adw_version, r.stop_version, s.cost_nusd "
    "FROM steps s LEFT JOIN runs r ON r.id = s.run_id "
    "WHERE s.prompt_version >= ? AND s.prompt_version < ? AND s.ts >= ? AND s.ts < ? "
    "ORDER BY s.id ASC"
)
_ADW_SQL = (
    "SELECT r.id, r.status, r.started, r.finished, r.adw, r.adw_version, r.stop_version, "
    "s.id, s.node, s.step, s.status, s.ok, s.seconds, s.ts, s.prompt_version, s.version, NULL "
    "FROM runs r LEFT JOIN steps s ON s.run_id = r.id "
    "WHERE r.adw IS NOT NULL AND r.adw = ? AND r.started >= ? AND r.started < ? "
    "ORDER BY r.id ASC, s.id ASC"
)
_ADW_SQL_COST = (
    "SELECT r.id, r.status, r.started, r.finished, r.adw, r.adw_version, r.stop_version, "
    "s.id, s.node, s.step, s.status, s.ok, s.seconds, s.ts, s.prompt_version, s.version, s.cost_nusd "
    "FROM runs r LEFT JOIN steps s ON s.run_id = r.id "
    "WHERE r.adw IS NOT NULL AND r.adw = ? AND r.started >= ? AND r.started < ? "
    "ORDER BY r.id ASC, s.id ASC"
)


def window_bounds(now: datetime | str | None = None) -> tuple[str, str]:
    if now is None:
        current = util.now()
    elif isinstance(now, str):
        current = util.parse_iso(now)
    else:
        current = now
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return util.iso(today - timedelta(days=6)), util.iso(today + timedelta(days=1))


def _pass_rate(passed: int, ended: int) -> int | None:
    if ended <= 0:
        return None
    quotient, remainder = divmod(passed * 100, ended)
    twice = remainder * 2
    if twice > ended or (twice == ended and quotient % 2 == 1):
        quotient += 1
    return quotient


def _median_seconds(values: list[float]) -> int | float | None:
    if not values:
        return None
    med = round(float(statistics.median(values)), 3)
    return int(med) if med.is_integer() else med


def _run_seconds(started: object, finished: object) -> float | None:
    if not isinstance(started, str) or not started or not isinstance(finished, str) or not finished:
        return None
    try:
        return max(0.0, (util.parse_iso(finished) - util.parse_iso(started)).total_seconds())
    except ValueError:
        return None


def _step_status(status: object, ok: object) -> str | None:
    if isinstance(status, str) and status:
        return status
    if ok == 1:
        return "done"
    if ok == 0:
        return "fault"
    return None


def _has_step_cost_nusd(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "cost_nusd" for row in conn.execute("PRAGMA table_info(steps)").fetchall())


def _monetary(
    conn: sqlite3.Connection,
    kind: str,
    name: str,
    start: str,
    end: str,
    has_cost_nusd: bool,
) -> tuple[int | None, float | None, tuple[str, bool, str]]:
    producer = MONETARY_PRODUCER
    if producer is None:
        return None, None, ("none", has_cost_nusd, "")
    if kind == "prompt":
        cost_nusd, cost_usd, rev = producer.for_prompt(conn, name, start, end)
    else:
        cost_nusd, cost_usd, rev = producer.for_adw(conn, name, start, end)
    return cost_nusd, cost_usd, (producer.capability, has_cost_nusd, rev)


def _revision_digest(payload: object) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validator_token(config_parts: dict[str, object], source_revision: str) -> str:
    return _revision_digest({"config": config_parts, "source": source_revision})


def prompt_stats_snapshot(
    conn: sqlite3.Connection, name: str, now: datetime | str | None = None
) -> tuple[dict[str, object], str]:
    start, end = window_bounds(now)
    has_cost_nusd = _has_step_cost_nusd(conn)
    rows: list[tuple[object, ...]] = []
    if name:
        prefix = f"prompt/{name}@"
        upper = f"prompt/{name}A"
        sql = _PROMPT_SQL_COST if has_cost_nusd else _PROMPT_SQL
        rows = [tuple(row) for row in conn.execute(sql, (prefix, upper, start, end)).fetchall()]
    run_ids: set[str] = set()
    done = 0
    fault = 0
    for row in rows:
        run_id = row[1]
        if isinstance(run_id, str) and run_id:
            run_ids.add(run_id)
        status = _step_status(row[2], row[3])
        if status == "done":
            done += 1
        elif status == "fault":
            fault += 1
    cost_nusd, cost_usd, money_state = _monetary(conn, "prompt", name, start, end, has_cost_nusd)
    result: dict[str, object] = {
        "runs7d": len(run_ids),
        "passRate": _pass_rate(done, done + fault),
        "costPerRunNusd": cost_nusd,
        "costPerRun": cost_usd,
    }
    revision = _revision_digest(("prompt_stats", name, start, end, money_state, rows))
    return result, revision


def prompt_stats(conn: sqlite3.Connection, name: str, now: datetime | str | None = None) -> dict[str, object]:
    return prompt_stats_snapshot(conn, name, now)[0]


def adw_stats_snapshot(
    conn: sqlite3.Connection, name: str, now: datetime | str | None = None
) -> tuple[dict[str, object], str]:
    start, end = window_bounds(now)
    has_cost_nusd = _has_step_cost_nusd(conn)
    rows: list[tuple[object, ...]] = []
    if name:
        sql = _ADW_SQL_COST if has_cost_nusd else _ADW_SQL
        rows = [tuple(row) for row in conn.execute(sql, (name, start, end)).fetchall()]
    seen_runs: set[str] = set()
    passed_runs = 0
    ended_runs = 0
    run_durations: list[float] = []
    node_runs: dict[str, set[str]] = {}
    node_done: dict[str, int] = {}
    node_fault: dict[str, int] = {}
    node_durations: dict[str, list[float]] = {}
    for row in rows:
        run_id = str(row[0])
        if run_id not in seen_runs:
            seen_runs.add(run_id)
            run_status = row[1]
            if isinstance(run_status, str) and run_status != "running":
                ended_runs += 1
                if run_status in PASS_RUN_STATUSES:
                    passed_runs += 1
            seconds = _run_seconds(row[2], row[3])
            if seconds is not None:
                run_durations.append(seconds)
        if row[7] is None:
            continue
        node = (row[8] if isinstance(row[8], str) and row[8] else "") or (
            row[9] if isinstance(row[9], str) and row[9] else ""
        )
        if not node:
            continue
        node_runs.setdefault(node, set()).add(run_id)
        node_done.setdefault(node, 0)
        node_fault.setdefault(node, 0)
        node_durations.setdefault(node, [])
        step_status = _step_status(row[10], row[11])
        if step_status == "done":
            node_done[node] += 1
        elif step_status == "fault":
            node_fault[node] += 1
        step_seconds = row[12]
        if isinstance(step_seconds, (int, float)) and not isinstance(step_seconds, bool):
            node_durations[node].append(float(step_seconds))
    nodes: dict[str, dict[str, object]] = {
        node: {
            "runs": len(node_runs[node]),
            "passRate": _pass_rate(node_done[node], node_done[node] + node_fault[node]),
            "medianSeconds": _median_seconds(node_durations[node]),
        }
        for node in sorted(node_runs)
    }
    cost_nusd, cost_usd, money_state = _monetary(conn, "adw", name, start, end, has_cost_nusd)
    result: dict[str, object] = {
        "runs": len(seen_runs),
        "passRate": _pass_rate(passed_runs, ended_runs),
        "medianSeconds": _median_seconds(run_durations),
        "costPerRunNusd": cost_nusd,
        "costPerRun": cost_usd,
        "nodes": nodes,
    }
    revision = _revision_digest(("adw_stats", name, start, end, money_state, rows))
    return result, revision


def adw_stats(conn: sqlite3.Connection, name: str, now: datetime | str | None = None) -> dict[str, object]:
    return adw_stats_snapshot(conn, name, now)[0]
