from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from . import config, configrepo, derived, refusals, util
from . import lines as lines_mod
from .store import db as foundation
from .store import derived as derived_store
from .store import projects as projects_store
from .store import writes

PREFIX = "band:"
CODE_VERSION = 1
MIN_TRIES = 5
WINDOW_TRIES = 30


class GateRefused(refusals.Refusal):
    status = 403
    code = "gate_refused"


class SourceMoved(refusals.Conflict):
    code = "conflict"


def parse_provider_target(target: str) -> str:
    if not isinstance(target, str) or not target.startswith(PREFIX):
        raise lines_mod.ValidationError(
            "provider",
            f"unsupported derived provider {target!r}; only 'band:<line>' is supported",
        )
    line_id = target.removeprefix(PREFIX)
    if not line_id or not projects_store.ID_RE.fullmatch(line_id):
        raise lines_mod.ValidationError(
            "provider",
            f"invalid line id in derived target {target!r}",
        )
    return line_id


def percentile(sorted_vals: Sequence[float], pct: float) -> int:
    if len(sorted_vals) == 1:
        return max(0, round(sorted_vals[0]))
    pos = (len(sorted_vals) - 1) * pct
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return max(0, round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac))


def _canonical_band(raw: object) -> dict[str, list[int | float]]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, list[int | float]] = {}
    for node in sorted(raw):
        bounds = raw[node]
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            continue
        lo, hi = float(bounds[0]), float(bounds[1])
        out[str(node)] = [int(lo) if lo.is_integer() else lo, int(hi) if hi.is_integer() else hi]
    return out


def _line_run_clause(conn: sqlite3.Connection, line_row: Mapping[str, object]) -> tuple[str, tuple[object, ...]]:
    line_id = str(line_row["id"])
    repo_name = str(line_row.get("repo") or "")
    if line_row.get("kind") == "pipeline" and repo_name:
        default_row = projects_store.default_pipeline_line(conn, repo_name)
        if default_row is not None and default_row["id"] == line_id:
            return (
                "(r.line = ? OR (COALESCE(r.line, '') = '' AND r.repo = ? AND r.kind = 'pipeline'))",
                (line_id, repo_name),
            )
    return "r.line = ?", (line_id,)


def compute_line_band(conn: sqlite3.Connection, line_id: str) -> tuple[dict[str, list[int]], str]:
    line_row = projects_store.get_line(conn, line_id)
    if line_row is None:
        return {}, util.digest(f"missing:{line_id}")
    where_sql, params = _line_run_clause(conn, line_row)
    rows = conn.execute(
        f"SELECT s.id, COALESCE(NULLIF(s.node, ''), s.step) AS node_name, s.seconds "  # noqa: S608
        f"FROM steps s JOIN runs r ON r.id = s.run_id "
        f"WHERE {where_sql} "
        f"AND COALESCE(NULLIF(s.status, ''), CASE WHEN s.ok = 1 THEN 'done' ELSE 'fault' END) = 'done' "
        f"AND s.seconds IS NOT NULL AND s.seconds >= 0 "
        f"ORDER BY COALESCE(s.ts, s.queued_at, '') DESC, s.id DESC",
        params,
    ).fetchall()
    per_node: dict[str, list[float]] = {}
    mark_entries: list[tuple[str, int, float]] = []
    for row in rows:
        node = str(row["node_name"] or "").strip()
        if not node:
            continue
        sec = float(row["seconds"])
        if not math.isfinite(sec) or sec < 0:
            continue
        bucket = per_node.setdefault(node, [])
        if len(bucket) >= WINDOW_TRIES:
            continue
        bucket.append(sec)
        mark_entries.append((node, int(row["id"]), round(sec, 6)))
    band: dict[str, list[int]] = {}
    for node in sorted(per_node):
        values = per_node[node]
        if len(values) < MIN_TRIES:
            continue
        ordered = sorted(values)
        lo = percentile(ordered, 0.10)
        hi = max(lo, percentile(ordered, 0.90))
        band[node] = [lo, hi]
    mark = util.digest(json.dumps(mark_entries, separators=(",", ":")))
    return band, mark


def line_band_for_run(conn: sqlite3.Connection, run_row: Mapping[str, object]) -> dict[str, list[int | float]]:
    if not projects_store.available(conn):
        return {}
    line_row = None
    if lid := str(run_row.get("line") or "").strip():
        line_row = projects_store.get_line(conn, lid)
    elif repo_name := str(run_row.get("repo") or "").strip():
        line_row = projects_store.default_pipeline_line(conn, repo_name)
    if line_row is None:
        return {}
    return _canonical_band(line_row.get("band"))


def _step_status(step: Mapping[str, object]) -> str:
    status = step.get("status")
    if isinstance(status, str) and status:
        return status
    return "done" if step.get("ok") else "fault"


def _step_node(step: Mapping[str, object]) -> str:
    node = step.get("node")
    if isinstance(node, str) and node.strip():
        return node.strip()
    name = step.get("step") or step.get("name") or ""
    return str(name).strip()


def _utc_now(now: datetime | str | None = None) -> datetime:
    if now is None:
        current = util.now()
    elif isinstance(now, str):
        current = util.parse_iso(now)
    else:
        current = now
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def run_eta(
    step_rows: Sequence[Mapping[str, object]],
    band: Mapping[str, Sequence[int | float]],
    *,
    now: datetime | str | None = None,
) -> tuple[str | None, bool]:
    running_idx: int | None = None
    for idx, step in enumerate(step_rows):
        if _step_status(step) == "running":
            running_idx = idx
    if running_idx is None:
        return None, False

    current = _utc_now(now)
    running_step = step_rows[running_idx]
    running_node = _step_node(running_step)
    partial = False
    remaining = 0.0

    running_band = band.get(running_node)
    if running_band is not None and len(running_band) == 2:
        hi = float(running_band[1])
        elapsed = 0.0
        raw_sec = running_step.get("seconds")
        if isinstance(raw_sec, (int, float)) and not isinstance(raw_sec, bool) and raw_sec > 0:
            elapsed = float(raw_sec)
        else:
            started_stamp = running_step.get("queued_at") or running_step.get("ts")
            if isinstance(started_stamp, str) and started_stamp:
                with suppress(ValueError):
                    started_dt = _utc_now(started_stamp)
                    elapsed = max(0.0, (current - started_dt).total_seconds())
        remaining += max(0.0, hi - elapsed)
    else:
        partial = True

    for step in step_rows[running_idx + 1:]:
        if _step_status(step) != "queued":
            continue
        q_band = band.get(_step_node(step))
        if q_band is not None and len(q_band) == 2:
            remaining += (float(q_band[0]) + float(q_band[1])) / 2.0
        else:
            partial = True

    return util.iso(current + timedelta(seconds=round(remaining))), partial


def recompute(
    line_id: str,
    base_version: int,
    *,
    max_retries: int = 2,
    before_write: Callable[[], None] | None = None,
) -> dict[str, object]:
    writes.validate_version(base_version)
    if not isinstance(line_id, str) or not projects_store.ID_RE.fullmatch(line_id):
        raise lines_mod.ValidationError("line", "must be a valid line id")
    cfg, problem = config.load_or_problem()
    if problem:
        raise GateRefused(f"config gate refused band recompute: {problem}")

    for attempt in range(max_retries + 1):
        with foundation.one_read() as rconn:
            existing = projects_store.get_line(rconn, line_id)
            if existing is None:
                raise lines_mod.NotFound(f"No line {line_id}")
            if existing["archived"]:
                raise lines_mod.ValidationError("archived", f"line '{line_id}' is archived; restore it before recomputing")
            if existing["version"] != base_version:
                raise writes.VersionConflict(lines_mod.present_line(existing))
            computed_band, mark_before = compute_line_band(rconn, line_id)

        if before_write is not None:
            before_write()

        with configrepo.maybe_instance_fence(), foundation.tx() as conn:
            current = projects_store.get_line(conn, line_id)
            if current is None:
                raise lines_mod.NotFound(f"No line {line_id}")
            if current["archived"]:
                raise lines_mod.ValidationError("archived", f"line '{line_id}' is archived; restore it before recomputing")
            if current["version"] != base_version:
                raise writes.VersionConflict(lines_mod.present_line(current))
            _, mark_after = compute_line_band(conn, line_id)
            if mark_after != mark_before:
                if attempt < max_retries:
                    continue
                raise SourceMoved(f"band:{line_id} source changed during recompute; retry")

            if _canonical_band(current["band"]) == computed_band:
                return lines_mod._receipt_result(
                    conn,
                    data=lines_mod.present_line(current),
                    subject=f"line:{line_id}",
                    ver=int(current["version"]),
                    seq=None,
                    extra={"changed": False, "source_mark": mark_after},
                )

            receipt = lines_mod.apply_line_update(conn, current, {"band": computed_band}, cfg=cfg)
            if derived_store.available(conn):
                derived_store.record_rebuilt(
                    conn,
                    f"{PREFIX}{line_id}",
                    code_version=CODE_VERSION,
                    source_mark=mark_after,
                )
            return {**receipt, "changed": True, "source_mark": mark_after}

    raise SourceMoved(f"band:{line_id} source changed during recompute; retry")


def recompute_provider(target: str, base_version: int) -> dict[str, object]:
    line_id = parse_provider_target(target)
    return recompute(line_id, base_version)


def _enumerate_band_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT id FROM lines WHERE archived = 0 ORDER BY id").fetchall()
    return [f"{PREFIX}{row[0]}" for row in rows]


def _band_source_mark(conn: sqlite3.Connection, store_name: str) -> str:
    line_id = store_name.removeprefix(PREFIX)
    _, mark = compute_line_band(conn, line_id)
    return mark


def _band_diff(conn: sqlite3.Connection, store_name: str) -> derived.DiffReport:
    line_id = store_name.removeprefix(PREFIX)
    line_row = projects_store.get_line(conn, line_id)
    if line_row is None:
        return derived.DiffReport(diff_rows=1, sample_ids=(line_id,), detail=f"unknown line '{line_id}'")
    expected_band, mark = compute_line_band(conn, line_id)
    expected_json = json.dumps(expected_band, sort_keys=True, separators=(",", ":"))
    conn.execute("CREATE TEMP TABLE temp_line_band (id TEXT PRIMARY KEY, band TEXT NOT NULL) STRICT")
    conn.execute("INSERT INTO temp.temp_line_band (id, band) VALUES (?, ?)", (line_id, expected_json))
    return derived.exact_table_diff(
        conn,
        "lines",
        "temp_line_band",
        ("id", "band"),
        key_columns=("id",),
        where="id = ?",
        params=(line_id,),
        source_mark=mark,
    )


def _band_rebuild(conn: sqlite3.Connection, store_name: str) -> str:
    line_id = store_name.removeprefix(PREFIX)
    existing = projects_store.get_line(conn, line_id)
    if existing is None:
        raise derived.UnavailableStoreError(f"unknown line {line_id!r}")
    expected_band, mark = compute_line_band(conn, line_id)
    if _canonical_band(existing["band"]) != expected_band:
        cfg = lines_mod._load_cfg()
        lines_mod.apply_line_update(conn, existing, {"band": expected_band}, cfg=cfg)
    return mark


BAND_PROVIDER = derived.Provider(
    name=PREFIX,
    code_version=CODE_VERSION,
    required_tables=("lines", "runs", "steps"),
    required_columns={
        "lines": ("id", "band", "archived", "version"),
        "runs": ("id", "line", "repo"),
        "steps": ("id", "run_id", "node", "step", "status", "ok", "seconds"),
    },
    source_mark=_band_source_mark,
    diff=_band_diff,
    rebuild=_band_rebuild,
    enumerate_names=_enumerate_band_names,
)

derived.register(BAND_PROVIDER, replace=True)
