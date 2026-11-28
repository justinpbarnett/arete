from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from . import adw, attention, bands, config, util
from . import lines as lines_mod
from .store import inbox as inbox_store
from .store import projects as projects_store
from .store import tasks as task_store

STATE_PRIORITY: dict[str, int] = {
    "fault": 0,
    "running": 1,
    "waiting": 2,
    "queued": 3,
    "done": 4,
    "idle": 5,
}
IN_FLIGHT_RUN_STATUSES = frozenset(("running", "queued", "waiting", "needs-info"))


def _utc_now(now: datetime | str | None = None) -> datetime:
    if now is None:
        current = util.now()
    elif isinstance(now, str):
        current = util.parse_iso(now)
    else:
        current = now
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _walk_outline_steps(entries: Sequence[Mapping[str, object]], out: list[str], seen: set[str]) -> None:
    for entry in entries:
        kind = entry.get("kind")
        if kind == "step":
            step_name = str(entry.get("step") or "").strip()
            if step_name and step_name not in seen:
                seen.add(step_name)
                out.append(step_name)
        elif kind == "repeat":
            first = entry.get("first")
            if isinstance(first, Mapping):
                _walk_outline_steps((first,), out, seen)
        elif kind == "when":
            then_entries = entry.get("then")
            if isinstance(then_entries, Sequence):
                _walk_outline_steps(then_entries, out, seen)


def adw_station_names(adw_name: str, cfg: config.Config | None = None) -> list[str]:
    if not adw_name or adw_name in adw.PYTHON_ADWS:
        return []
    try:
        loaded = adw.load(cfg, adw_name)
    except (adw.UnknownAdw, adw.AdwError):
        return []
    names: list[str] = []
    _walk_outline_steps(adw.outline(loaded), names, set())
    return names


def line_adw_name(line: Mapping[str, object], cfg: config.Config | None = None) -> str:
    if str(line.get("kind") or "") == "loops":
        return ""
    explicit = str(line.get("adw_route") or "").strip()
    if explicit:
        return explicit
    try:
        routes = adw.load_routes(cfg)
    except adw.AdwError:
        return "feature"
    repo_name = str(line.get("repo") or "")
    line_kind = str(line.get("kind") or "")
    for rule in routes.rules:
        when = rule.when
        if "event" in when or "repaired_by" in when:
            continue
        if "repo" in when and not adw._matches_value(when["repo"], repo_name):
            continue
        if "kind" in when and not adw._matches_value(when["kind"], line_kind):
            continue
        return rule.adw
    return "feature"


def _step_status(step: Mapping[str, object]) -> str:
    status = step.get("status")
    if isinstance(status, str) and status:
        return status
    return "done" if step.get("ok") else "fault"


def _step_node(step: Mapping[str, object]) -> str:
    node = step.get("node")
    if isinstance(node, str) and node.strip():
        return node.strip()
    return str(step.get("step") or "").strip()


def _open_waiting_by_run(conn: sqlite3.Connection) -> dict[str, str]:
    waiting: dict[str, str] = {}
    if not inbox_store.available(conn):
        return waiting
    rows = conn.execute(
        "SELECT kind, run_id, subject, detail FROM inbox "
        "WHERE state = 'open' AND kind IN ('continue', 'choice', 'freeform', 'approval')"
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
            if not isinstance(detail, dict):
                detail = {}
        except ValueError:
            detail = {}
        node = str(detail.get("node") or "").strip()
        rids: list[str] = []
        if raw_run := str(row["run_id"] or "").strip():
            rids.append(raw_run)
        subject = str(row["subject"] or "")
        if subject.startswith("run:"):
            rids.append(subject.removeprefix("run:").strip())
        elif subject.startswith("step:"):
            rids.append(subject.removeprefix("step:").partition("/")[0].strip())
        if isinstance(detail.get("waiting"), list):
            for member in detail["waiting"]:
                if isinstance(member, str) and member.strip():
                    rids.append(member.strip())
        for rid in rids:
            if rid and rid not in waiting:
                waiting[rid] = node
    return waiting


def _active_runs_and_steps(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]], dict[str, str]]:
    waiting_map = _open_waiting_by_run(conn)
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    has_line = "line" in run_cols
    has_adw = "adw" in run_cols
    has_task_id = "task_id" in run_cols
    select_cols = [
        "r.id", "r.kind", "r.repo", "r.issue", "r.status", "r.started", "r.finished",
        "r.line" if has_line else "NULL AS line",
        "r.adw" if has_adw else "NULL AS adw",
        "r.task_id" if has_task_id else "NULL AS task_id",
    ]
    rows = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM runs r "  # noqa: S608
        "WHERE COALESCE(r.dry_run, 0) = 0 AND r.status != 'dry-run' AND r.kind != 'chat' "
        "AND (r.status IN ('running', 'queued', 'waiting', 'needs-info') "
        "  OR EXISTS (SELECT 1 FROM steps s WHERE s.run_id = r.id AND s.status IN ('running', 'waiting', 'queued'))) "
        "ORDER BY r.started DESC, r.id DESC"
    ).fetchall()
    run_list = [dict(r) for r in rows]
    seen_ids = {str(r["id"]) for r in run_list}
    extra_ids = sorted(set(waiting_map) - seen_ids)
    if extra_ids:
        placeholders = ",".join("?" for _ in extra_ids)
        extra_rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM runs r "  # noqa: S608
            f"WHERE r.id IN ({placeholders}) AND COALESCE(r.dry_run, 0) = 0 "
            "AND r.status NOT IN ('done', 'pr', 'merged', 'closed', 'skipped', 'stopped', 'dry-run') "
            "ORDER BY r.started DESC, r.id DESC",
            tuple(extra_ids),
        ).fetchall()
        run_list.extend(dict(r) for r in extra_rows)

    steps_by_run: dict[str, list[dict[str, object]]] = {}
    if run_list:
        rids = [str(r["id"]) for r in run_list]
        placeholders = ",".join("?" for _ in rids)
        step_rows = conn.execute(
            f"SELECT id, run_id, step, node, try, status, ok, seconds, queued_at, ts "  # noqa: S608
            f"FROM steps WHERE run_id IN ({placeholders}) ORDER BY id ASC",
            tuple(rids),
        ).fetchall()
        for srow in step_rows:
            steps_by_run.setdefault(str(srow["run_id"]), []).append(dict(srow))
    return run_list, steps_by_run, waiting_map


def _run_line_id(
    conn: sqlite3.Connection,
    run_row: Mapping[str, object],
    default_by_repo: dict[str, str | None],
) -> str | None:
    if lid := str(run_row.get("line") or "").strip():
        return lid
    repo_name = str(run_row.get("repo") or "").strip()
    if not repo_name:
        return None
    if repo_name not in default_by_repo:
        default_row = projects_store.default_pipeline_line(conn, repo_name) if projects_store.available(conn) else None
        default_by_repo[repo_name] = default_row["id"] if default_row else None
    return default_by_repo[repo_name]


def line_stations(
    conn: sqlite3.Connection,
    line: Mapping[str, object],
    cfg: config.Config | None = None,
    *,
    line_runs: Sequence[Mapping[str, object]] | None = None,
    steps_by_run: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    waiting_map: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    adw_name = line_adw_name(line, cfg)
    station_names = adw_station_names(adw_name, cfg)
    if not station_names:
        return []
    if line.get("paused"):
        return [{"name": name, "state": "idle", "wip": 0} for name in station_names]

    if line_runs is None or steps_by_run is None or waiting_map is None:
        all_runs, all_steps, all_waiting = _active_runs_and_steps(conn)
        default_by_repo: dict[str, str | None] = {}
        lid = str(line["id"])
        line_runs = [r for r in all_runs if _run_line_id(conn, r, default_by_repo) == lid]
        steps_by_run = all_steps
        waiting_map = all_waiting

    if not line_runs:
        return [{"name": name, "state": "idle", "wip": 0} for name in station_names]

    station_index = {name: idx for idx, name in enumerate(station_names)}
    states_by_station: dict[str, set[str]] = {name: set() for name in station_names}
    wip_by_station: Counter[str] = Counter()
    newest_run_id = str(line_runs[0]["id"])

    for run_row in line_runs:
        rid = str(run_row["id"])
        run_steps = steps_by_run.get(rid, ())
        latest_by_node: dict[str, Mapping[str, object]] = {}
        for step in run_steps:
            node = _step_node(step)
            if node not in station_index:
                continue
            prev = latest_by_node.get(node)
            if prev is None or (int(step.get("try") or 0), int(step.get("id") or 0)) >= (
                int(prev.get("try") or 0),
                int(prev.get("id") or 0),
            ):
                latest_by_node[node] = step

        run_active_nodes: set[str] = set()
        highest_seen_idx = -1
        for node, step in latest_by_node.items():
            st = _step_status(step)
            idx = station_index[node]
            if st in ("fault", "running", "waiting", "done") and idx > highest_seen_idx:
                highest_seen_idx = idx
            if st in ("running", "waiting"):
                wip_by_station[node] += 1
                run_active_nodes.add(node)
            if st in ("fault", "running", "waiting", "queued"):
                states_by_station[node].add(st)
            elif st == "done" and rid == newest_run_id:
                states_by_station[node].add("done")

        is_waiting = rid in waiting_map or str(run_row.get("status") or "") in ("waiting", "needs-info")
        if is_waiting:
            target_node = waiting_map.get(rid, "")
            if target_node not in station_index:
                target_node = next(
                    (
                        name
                        for name in station_names
                        if name in latest_by_node and _step_status(latest_by_node[name]) in ("waiting", "running", "queued")
                    ),
                    "",
                )
            if not target_node:
                target_node = next(
                    (
                        name
                        for name in station_names
                        if name not in latest_by_node or _step_status(latest_by_node[name]) != "done"
                    ),
                    "",
                )
            if target_node in station_index:
                states_by_station[target_node].add("waiting")
                if target_node not in run_active_nodes:
                    wip_by_station[target_node] += 1
                    run_active_nodes.add(target_node)
                if station_index[target_node] > highest_seen_idx:
                    highest_seen_idx = station_index[target_node]

        for idx, name in enumerate(station_names):
            if idx > highest_seen_idx and name not in latest_by_node:
                states_by_station[name].add("queued")

    result: list[dict[str, object]] = []
    for name in station_names:
        candidates = states_by_station[name]
        picked = min(candidates, key=lambda s: STATE_PRIORITY[s]) if candidates else "idle"
        result.append({"name": name, "state": picked, "wip": int(wip_by_station[name])})
    return result


def shell_screen(
    conn: sqlite3.Connection,
    cfg: object = None,
    *,
    now: datetime | str | None = None,
) -> dict[str, object]:
    current = _utc_now(now)
    andon_data = attention.andon(conn, cfg, now=current)
    return {
        "now": util.iso(current),
        "andon": andon_data,
        "zone": andon_data["zone"],
        "handledWithoutYouPct": attention.handled_without_you_pct(conn, now=current),
    }


def floor_screen(
    conn: sqlite3.Connection,
    cfg: config.Config | None = None,
    *,
    project: str | None = None,
    include_archived: bool = False,
    now: datetime | str | None = None,
) -> dict[str, object]:
    loaded_cfg = config.load_or_problem()[0] if cfg is None else cfg
    current = _utc_now(now)
    andon_data = attention.andon(conn, now=current)
    pct = attention.handled_without_you_pct(conn, now=current)

    all_runs, steps_by_run, waiting_map = _active_runs_and_steps(conn)
    default_by_repo: dict[str, str | None] = {}
    runs_by_line: dict[str, list[dict[str, object]]] = {}
    for run_row in all_runs:
        lid = _run_line_id(conn, run_row, default_by_repo)
        if lid:
            runs_by_line.setdefault(lid, []).append(run_row)

    summary_lines = lines_mod.board_lines_summary(
        conn,
        project=project,
        include_archived=include_archived,
        live_runs=[r for r in all_runs if r.get("status") == "running"],
    )
    band_by_line: dict[str, dict[str, list[int | float]]] = {}
    shown_line_ids: set[str] = set()
    floor_lines: list[dict[str, object]] = []
    for line_row in summary_lines:
        lid = str(line_row["id"])
        shown_line_ids.add(lid)
        band_by_line[lid] = line_row.get("band") or {}
        stations = line_stations(
            conn,
            line_row,
            loaded_cfg,
            line_runs=runs_by_line.get(lid, ()),
            steps_by_run=steps_by_run,
            waiting_map=waiting_map,
        )
        floor_lines.append({**line_row, "stations": stations})

    live_rows: list[dict[str, object]] = []
    for run_row in all_runs:
        rid = str(run_row["id"])
        lid = _run_line_id(conn, run_row, default_by_repo)
        if project is not None and lid not in shown_line_ids:
            continue
        run_steps = steps_by_run.get(rid, ())
        line_band = band_by_line.get(lid or "") if lid in band_by_line else bands.line_band_for_run(conn, run_row)
        eta, eta_partial = bands.run_eta(run_steps, line_band or {}, now=current)
        active_node: str | None = None
        for step in reversed(run_steps):
            if _step_status(step) in ("running", "waiting", "fault"):
                active_node = _step_node(step) or None
                break
        task_label: str | None = None
        if run_row.get("task_id") is not None:
            task_label = task_store.format_task_id(int(run_row["task_id"]))
        elif run_row.get("issue"):
            task_label = str(run_row["issue"])
        live_rows.append({
            "id": rid,
            "task": task_label,
            "line": lid,
            "repo": run_row.get("repo"),
            "kind": run_row.get("kind"),
            "status": str(run_row.get("status") or ""),
            "started": run_row.get("started"),
            "adw": run_row.get("adw"),
            "node": active_node,
            "eta": eta,
            "eta_partial": eta_partial,
        })

    return {
        "now": util.iso(current),
        "andon": andon_data,
        "zone": andon_data["zone"],
        "handledWithoutYouPct": pct,
        "lines": floor_lines,
        "live": live_rows,
    }
