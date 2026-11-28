from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta

from . import config, control, derived, jev_verdicts, prices, principals, run_lines, util
from .store import db as foundation
from .store import events as events_store

CHARTS = ("shipped", "flow", "needs-you", "cost", "grid", "burnup", "evals", "jev")
DEFAULT_DAYS = 7.0
COST_GROUPINGS = ("step", "kind", "model", "route", "day", "runner", "loop")
SHIPPED_STATUSES = frozenset(("pr", "merged", "done"))
FAILED_STATUSES = frozenset(("failed", "rejected", "interrupted"))
LANDED_OUTCOMES = frozenset(("landed", "merged", "pushed"))
BASELINE_EVAL_SOURCE = "worktree"

CHART_STORES: dict[str, tuple[str, ...]] = {
    "shipped": ("runs.cost_nusd",),
    "flow": ("runs.cost_nusd",),
    "needs-you": ("runs.cost_nusd",),
    "cost": ("runs.cost_nusd", "runs.tokens_in", "runs.tokens_out"),
    "grid": ("runs.cost_nusd",),
    "burnup": ("runs.cost_nusd",),
    "evals": ("runs.cost_nusd",),
    "jev": ("runs.cost_nusd",),
}

_SEQ_RE = re.compile(r"(?:^|\D)(\d+)$")


def _parse_seq(mark: str | None) -> int | None:
    if not mark:
        return None
    text = str(mark).strip()
    if text.isascii() and text.isdigit():
        return int(text)
    if "." in text:
        tail = text.rsplit(".", 1)[1]
        if tail.isascii() and tail.isdigit():
            return int(tail)
    if m := _SEQ_RE.search(text):
        return int(m.group(1))
    return None


def _has_money_backfill(conn: sqlite3.Connection) -> bool:
    return foundation.migration_applied(foundation.MONEY_BACKFILL_SCHEMA, conn)


def _has_money_expand(conn: sqlite3.Connection) -> bool:
    return foundation.migration_applied(foundation.MONEY_EXPAND_SCHEMA, conn)


def _window(days: float | None) -> tuple[float, str]:
    d = DEFAULT_DAYS if days is None else float(days)
    if not math.isfinite(d) or d < 0:
        raise ValueError("days must be a finite nonnegative number")
    try:
        return d, util.iso(util.now() - timedelta(days=d))
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"cannot look back {d:g} days: {exc}") from exc


def _local_day(ts: str) -> str:
    if not ts:
        return ""
    try:
        return util.parse_iso(ts).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def _coverage_state(
    conn: sqlite3.Connection,
    chart: str,
    *,
    min_seq: int | None = None,
) -> tuple[int, bool]:
    current_seq = _parse_seq(str(events_store.metadata(conn).get("head") or "")) or 0
    stores = CHART_STORES.get(chart, ("runs.cost_nusd",))
    reg = derived.REGISTRY
    stale = False
    recorded_seqs: list[int] = []
    for store_name in stores:
        st = reg.store_status(conn, store_name)
        if not st.fresh:
            stale = True
        seq = _parse_seq(st.recorded_source_mark)
        if seq is None:
            stale = True
        else:
            recorded_seqs.append(seq)
    derived_seq = min(recorded_seqs) if len(recorded_seqs) == len(stores) and recorded_seqs else 0
    if derived_seq < current_seq:
        stale = True
    if min_seq is not None and derived_seq < min_seq:
        stale = True
    return derived_seq, stale


def _billing_class(row: dict, local_providers: tuple[str, ...]) -> str:
    return control.billing_of(row, local_providers)


def _route_info(row: dict, local_providers: tuple[str, ...]) -> dict[str, str]:
    which = str(row.get("runner") or "")
    model = str(row.get("model") or "")
    billing = _billing_class(row, local_providers)
    fallback = {
        "claude": "anthropic",
        "codex": "openai" if row.get("auth_mode") == "api_key" else "chatgpt",
        "pi": model.partition("/")[0] if "/" in model else "",
    }.get(which, "")
    provider = str(row.get("provider") or fallback)
    return {
        "runner": which,
        "provider": provider,
        "model": model,
        "billing": billing,
        "name": f"{which or 'no runner'}/{provider or '-'}/{model or 'no model'}/{billing}",
    }


def _reprice_step(
    row: dict,
    kind: str,
    rates_obj: object,
) -> tuple[int | None, int | None, dict | None]:
    runner = str(row.get("runner") or "")
    model = str(row.get("model") or "")
    provider = str(row.get("provider") or "")
    auth_mode = str(row.get("auth_mode") or "")
    tin = int(row.get("tokens_in") or 0)
    tout = int(row.get("tokens_out") or 0)
    cr = int(row.get("cache_read") or 0)
    cw = int(row.get("cache_write") or 0)
    if kind == "models" and isinstance(rates_obj, dict):
        ts = str(row.get("ts") or "")
        try:
            at = util.parse_iso(ts) if ts else util.now()
        except ValueError:
            at = util.now()
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        quote = prices.applied(rates_obj, runner, (model,), at, tin if int(row.get("turns") or 0) <= 1 else None)
        ref_nusd = prices.reference_nusd(quote, tin, tout, cr, cw)
        if ref_nusd is None:
            return None, None, quote
        paid_nusd = 0 if auth_mode in ("subscription", "local") or runner in ("replay", "guard") else ref_nusd
        return ref_nusd, paid_nusd, quote
    if kind == "billing" and isinstance(rates_obj, prices.BillingPolicy):
        rec_cost = (
            int(row["cost_nusd"]) / 1_000_000_000
            if row.get("cost_nusd") is not None
            else float(row.get("cost_usd") or 0.0)
        )
        outcome = prices.price_call(
            runner=runner,
            model=model,
            provider=provider,
            auth_mode=auth_mode,
            tokens_in=tin,
            tokens_out=tout,
            cache_read=cr,
            cache_write=cw,
            provider_cost_usd=rec_cost,
            policy=rates_obj,
            selected_local=(auth_mode == "local"),
        )
        return outcome.cost_nusd, outcome.paid_nusd, None
    return None, None, None


def _query_cost(
    conn: sqlite3.Connection,
    *,
    days: float | None = None,
    since_override: str | None = None,
    by: str | None = None,
    price_version: str | None = None,
    run_id: str | None = None,
    step: str | None = None,
) -> dict[str, object]:
    if by is not None and by not in COST_GROUPINGS:
        raise ValueError(f"cannot group costs by {by}; pick one of {', '.join(COST_GROUPINGS)}")
    if since_override is not None:
        win_days = DEFAULT_DAYS if days is None else float(days)
        since = since_override
    else:
        win_days, since = _window(days)

    reprice_kind = ""
    reprice_obj: object = None
    if price_version:
        reprice_kind, reprice_obj = prices.load_version(price_version)

    backfill = _has_money_backfill(conn)
    expand = _has_money_expand(conn)
    local_providers: tuple[str, ...] = ()
    if not backfill:
        local_providers = tuple(config.load_or_problem()[0].local_providers)

    cost_col = (
        "COALESCE(s.cost_nusd, CAST(round(COALESCE(s.cost_usd, 0.0) * 1e9) AS INTEGER))"
        if expand
        else "CAST(round(COALESCE(s.cost_usd, 0.0) * 1e9) AS INTEGER)"
    )
    paid_col = "s.paid_nusd" if expand else "NULL"
    pv_col = "s.price_version" if expand else "NULL"

    where = ["1=1"]
    params: list[object] = []
    if run_id:
        where.append("s.run_id = ?")
        params.append(run_id)
    else:
        where.append("s.ts >= ?")
        params.append(since)
    if step:
        where.append("(s.step = ? OR s.node = ?)")
        params.extend((step, step))

    rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT s.id, s.run_id, s.step, s.kind AS task_kind, s.runner, s.provider, s.model, s.auth_mode, "  # noqa: S608
            f"{cost_col} AS cost_nusd, {paid_col} AS paid_nusd, {pv_col} AS price_version, "
            f"s.cost_usd, s.tokens_in, s.tokens_out, s.cache_read, s.cache_write, s.turns, "
            f"s.seconds, s.api_seconds, s.ts, r.loop, r.kind AS run_kind, r.repo "
            f"FROM steps s LEFT JOIN runs r ON r.id = s.run_id "
            f"WHERE {' AND '.join(where)} ORDER BY s.ts, s.id",
            params,
        )
    ]

    group_by = by or "route"
    grouped: dict[str, dict] = {}
    paid_groups: dict[str, dict[str, dict]] = {"model": {}, "runner": {}, "loop": {}, "day": {}}
    route_groups: dict[str, dict] = {}
    total_cost_nusd = 0
    total_paid_nusd = 0
    total_sub_nusd = 0
    step_items: list[dict[str, object]] = []

    for r in rows:
        route = _route_info(r, local_providers)
        billing = route["billing"]
        rec_cost_nusd = int(r["cost_nusd"] or 0)
        if backfill and r.get("paid_nusd") is not None:
            rec_paid_nusd = int(r["paid_nusd"])
        else:
            rec_paid_nusd = rec_cost_nusd if billing == "api_key" else 0

        quote_used: dict | None = None
        if reprice_kind:
            rp_cost, rp_paid, quote_used = _reprice_step(r, reprice_kind, reprice_obj)
            eff_cost_nusd = 0 if rp_cost is None else int(rp_cost)
            eff_paid_nusd = 0 if rp_paid is None else int(rp_paid)
        else:
            eff_cost_nusd = 0 if billing in ("local", "replay") else rec_cost_nusd
            eff_paid_nusd = rec_paid_nusd

        eff_sub_nusd = eff_cost_nusd if billing == "subscription" else 0
        total_cost_nusd += eff_cost_nusd
        total_paid_nusd += eff_paid_nusd
        total_sub_nusd += eff_sub_nusd

        if run_id or step or reprice_kind:
            step_items.append({
                "id": r["id"],
                "run_id": r["run_id"],
                "step": r["step"],
                "runner": r["runner"] or "",
                "model": r["model"] or "",
                "provider": route["provider"],
                "auth_mode": r["auth_mode"] or "",
                "price_version": r.get("price_version") or "",
                "recorded_cost_nusd": rec_cost_nusd,
                "recorded_cost_usd": rec_cost_nusd / 1_000_000_000,
                "recorded_paid_nusd": rec_paid_nusd,
                "recorded_paid_usd": rec_paid_nusd / 1_000_000_000,
                "cost_nusd": eff_cost_nusd,
                "cost_usd": eff_cost_nusd / 1_000_000_000,
                "paid_nusd": eff_paid_nusd,
                "paid_usd": eff_paid_nusd / 1_000_000_000,
                "tokens_in": int(r["tokens_in"] or 0),
                "tokens_out": int(r["tokens_out"] or 0),
                "cache_read": int(r["cache_read"] or 0),
                "cache_write": int(r["cache_write"] or 0),
                **({"quote": quote_used} if quote_used is not None else {}),
            })

        # Accumulate route groups
        rrow = route_groups.setdefault(
            route["name"],
            {
                "by": "route",
                "name": route["name"],
                "runs": set(),
                "calls": 0,
                "cost_nusd": 0,
                "paid_nusd": 0,
                "subscription_nusd": 0,
                "runner": route["runner"],
                "provider": route["provider"],
                "model": route["model"],
                "billing": billing,
                "tokens_in": 0,
                "cache_read": 0,
                "cache_write": 0,
                "tokens_out": 0,
                "minutes": 0.0,
                "api_minutes": 0.0,
            },
        )
        rrow["runs"].add(r["run_id"])
        rrow["calls"] += 1
        rrow["cost_nusd"] += eff_cost_nusd
        rrow["paid_nusd"] += eff_paid_nusd
        rrow["subscription_nusd"] += eff_sub_nusd
        for tf in ("tokens_in", "cache_read", "cache_write", "tokens_out"):
            rrow[tf] += int(r.get(tf) or 0)
        rrow["minutes"] += float(r["seconds"] or 0) / 60
        rrow["api_minutes"] += float(r.get("api_seconds") or 0) / 60

        # Accumulate requested `by` grouping
        if group_by == "route":
            gname = route["name"]
        elif group_by == "step":
            gname = r["step"] or "no step"
        elif group_by == "kind":
            gname = r["task_kind"] or "no kind"
        elif group_by == "model":
            gname = r["model"] or "no model"
        elif group_by == "runner":
            gname = r["runner"] or "no runner"
        elif group_by == "loop":
            gname = r["loop"] or r["run_kind"] or r["run_id"] or "no loop"
        else:
            gname = _local_day(str(r["ts"] or ""))

        grow = grouped.setdefault(
            gname,
            {
                "by": group_by,
                "name": gname,
                "runs": set(),
                "calls": 0,
                "cost_nusd": 0,
                "paid_nusd": 0,
                "subscription_nusd": 0,
                **(
                    {k: route[k] for k in ("runner", "provider", "model", "billing")}
                    if group_by == "route"
                    else {}
                ),
                "tokens_in": 0,
                "cache_read": 0,
                "cache_write": 0,
                "tokens_out": 0,
                "minutes": 0.0,
                "api_minutes": 0.0,
            },
        )
        grow["runs"].add(r["run_id"])
        grow["calls"] += 1
        grow["cost_nusd"] += eff_cost_nusd
        grow["paid_nusd"] += eff_paid_nusd
        grow["subscription_nusd"] += eff_sub_nusd
        for tf in ("tokens_in", "cache_read", "cache_write", "tokens_out"):
            grow[tf] += int(r.get(tf) or 0)
        grow["minutes"] += float(r["seconds"] or 0) / 60
        grow["api_minutes"] += float(r.get("api_seconds") or 0) / 60

        # Accumulate paid breakdowns (model, runner, loop, day)
        if eff_paid_nusd:
            pkeys = {
                "model": r["model"] or "no model",
                "runner": r["runner"] or "no runner",
                "loop": r["loop"] or r["run_kind"] or r["run_id"] or "no loop",
                "day": _local_day(str(r["ts"] or "")),
            }
            for grp, key in pkeys.items():
                prow = paid_groups[grp].setdefault(
                    key, {"name": key, "cost_nusd": 0, "steps": 0, "tokens_in": 0, "tokens_out": 0}
                )
                prow["cost_nusd"] += eff_paid_nusd
                prow["steps"] += 1
                prow["tokens_in"] += int(r["tokens_in"] or 0)
                prow["tokens_out"] += int(r["tokens_out"] or 0)

    def _finish_groups(src: dict[str, dict]) -> list[dict[str, object]]:
        out = [
            {
                **{k: v for k, v in item.items() if k not in ("runs",)},
                "runs": len(item["runs"]),
                "cost_usd": item["cost_nusd"] / 1_000_000_000,
                "paid_usd": item["paid_nusd"] / 1_000_000_000,
                "subscription_usd": item["subscription_nusd"] / 1_000_000_000,
                "minutes": round(item["minutes"], 2),
                "api_minutes": round(item["api_minutes"], 2),
            }
            for item in src.values()
        ]
        return sorted(out, key=lambda item: (-int(item["cost_nusd"]), -int(item["calls"]), str(item["name"])))

    waits = dict.fromkeys(run_lines.WAIT_CAUSES, 0.0)
    run_where = "id = ?" if run_id else "started >= ?"
    run_arg = run_id if run_id else since
    for rrow in conn.execute(f"SELECT wait_minutes FROM runs WHERE {run_where}", (run_arg,)):  # noqa: S608
        try:
            recorded = json.loads(rrow["wait_minutes"] or "{}")
        except ValueError:
            continue
        if not isinstance(recorded, dict):
            continue
        for cause in waits:
            if type(recorded.get(cause)) in (int, float):
                waits[cause] += recorded[cause]

    series: dict[str, object] = {
        "days": win_days,
        "since": since,
        "by": group_by,
        "cost_nusd": total_cost_nusd,
        "cost_usd": total_cost_nusd / 1_000_000_000,
        "total_nusd": total_paid_nusd,
        "total_usd": total_paid_nusd / 1_000_000_000,
        "paid_nusd": total_paid_nusd,
        "paid_usd": total_paid_nusd / 1_000_000_000,
        "subscription_nusd": total_sub_nusd,
        "subscription_usd": total_sub_nusd / 1_000_000_000,
        "groups": _finish_groups(grouped),
        "by_route": _finish_groups(route_groups),
        **{
            f"by_{grp}": sorted(
                (
                    {
                        "name": row["name"],
                        "cost_nusd": row["cost_nusd"],
                        "cost_usd": row["cost_nusd"] / 1_000_000_000,
                        "steps": row["steps"],
                        "tokens_in": row["tokens_in"],
                        "tokens_out": row["tokens_out"],
                    }
                    for row in rows_map.values()
                ),
                key=lambda item: (-int(item["cost_nusd"]), str(item["name"])),
            )
            for grp, rows_map in paid_groups.items()
        },
        "wait_minutes": {cause: round(minutes, 2) for cause, minutes in waits.items()},
    }
    if price_version:
        series["price_version"] = price_version
    if step_items or run_id or step:
        series["steps"] = step_items
    return series


def _outcome_bucket(status: str) -> str:
    if status in SHIPPED_STATUSES:
        return "shipped"
    if status in FAILED_STATUSES:
        return "failed"
    return "other"


def _seconds_between(started: str | None, finished: str | None) -> int | None:
    if not started or not finished:
        return None
    try:
        return max(0, int((util.parse_iso(finished) - util.parse_iso(started)).total_seconds()))
    except ValueError:
        return None


def _query_shipped(
    conn: sqlite3.Connection,
    *,
    days: float | None = None,
    since_override: str | None = None,
) -> dict[str, object]:
    if since_override is not None:
        win_days = 14.0 if days is None else float(days)
        since = since_override
        first_day = util.parse_iso(since).astimezone().date()
        day_count = max(1, int(round(win_days)))
        day_keys = [(first_day + timedelta(days=n)).isoformat() for n in range(day_count)]
    else:
        win_days = 14.0 if days is None else float(days)
        if not math.isfinite(win_days) or win_days < 0:
            raise ValueError("days must be a finite nonnegative number")
        day_count = max(1, int(math.ceil(win_days)))
        first_day = datetime.now().astimezone().date() - timedelta(days=day_count - 1)
        day_keys = [(first_day + timedelta(days=n)).isoformat() for n in range(day_count)]
        since = util.iso(datetime.combine(first_day, time.min).astimezone().astimezone(UTC))

    paid_expr = "COALESCE(SUM(s.paid_nusd), 0)" if _has_money_backfill(conn) else "0"
    rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT r.id, r.repo, r.status, r.started, r.finished, "  # noqa: S608
            f"(SELECT {paid_expr} FROM steps s WHERE s.run_id = r.id) AS paid_nusd "
            "FROM runs r WHERE r.kind = 'pipeline' AND r.status != 'running' AND r.started >= ? "
            "ORDER BY r.started",
            (since,),
        )
    ]

    cost_data = _query_cost(conn, days=win_days, since_override=since)
    paid_on = {str(item["name"]): int(item["cost_nusd"]) for item in cost_data["by_day"]}  # type: ignore[index]
    by_day: dict[str, dict[str, object]] = {
        d: {"day": d, "paid_nusd": paid_on.get(d, 0), "paid": paid_on.get(d, 0) / 1_000_000_000,
            "shipped": 0, "failed": 0, "other": 0}
        for d in day_keys
    }
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        repo_name = str(r.get("repo") or "")
        by_repo[repo_name].append(r)
        day = _local_day(str(r.get("started") or ""))
        if day in by_day:
            bucket = _outcome_bucket(str(r.get("status") or ""))
            by_day[day][bucket] = int(by_day[day][bucket]) + 1

    repos: list[dict[str, object]] = []
    for name, repo_rows in sorted(by_repo.items()):
        outcomes = Counter(_outcome_bucket(str(r.get("status") or "")) for r in repo_rows)
        shipped_n, failed_n = outcomes["shipped"], outcomes["failed"]
        durations = [sec for r in repo_rows if (sec := _seconds_between(r.get("started"), r.get("finished"))) is not None]
        repo_paid_nusd = sum(int(r.get("paid_nusd") or 0) for r in repo_rows)
        repos.append({
            "repo": name,
            "runs": len(repo_rows),
            "shipped": shipped_n,
            "failed": failed_n,
            "pass_rate": round(shipped_n / (shipped_n + failed_n), 2) if shipped_n + failed_n else None,
            "median_seconds": int(statistics.median(durations)) if durations else None,
            "paid_nusd": repo_paid_nusd,
            "paid": repo_paid_nusd / 1_000_000_000,
        })

    return {
        "days": win_days,
        "since": since,
        "by_day": list(by_day.values()),
        "repos": repos,
        "total_paid_nusd": cost_data["total_nusd"],
        "total_paid": cost_data["total_usd"],
        "by_model": cost_data["by_model"],
        "by_runner": cost_data["by_runner"],
        "by_loop": cost_data["by_loop"],
    }


def _query_flow(conn: sqlite3.Connection, *, days: float | None = None) -> dict[str, object]:
    win_days, since = _window(days)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS runs, COALESCE(SUM(active_seconds), 0) AS active_seconds, "
        "COALESCE(SUM(verify_seconds), 0) AS verify_seconds, "
        "COALESCE(SUM(verify_open_seconds), 0) AS verify_open_seconds, "
        "COALESCE(SUM(repair_calls), 0) AS repair_calls "
        "FROM runs WHERE started >= ? GROUP BY status ORDER BY status",
        (since,),
    ).fetchall()
    return {
        "days": win_days,
        "since": since,
        "statuses": [
            {
                "status": str(r["status"]),
                "runs": int(r["runs"]),
                "active_seconds": round(float(r["active_seconds"]), 2),
                "verify_seconds": round(float(r["verify_seconds"]), 2),
                "verify_open_seconds": round(float(r["verify_open_seconds"]), 2),
                "repair_calls": int(r["repair_calls"]),
            }
            for r in rows
        ],
    }


def _query_needs_you(
    conn: sqlite3.Connection,
    *,
    days: float | None = None,
    since_override: str | None = None,
) -> dict[str, object]:
    if since_override is not None:
        win_days = DEFAULT_DAYS if days is None else float(days)
        since = since_override
    else:
        win_days, since = _window(days)
    pushes: Counter[str] = Counter()
    for r in conn.execute("SELECT outcome, approval FROM verdicts WHERE at >= ? AND kind = 'push'", (since,)):
        outcome = str(r["outcome"] or "")
        if outcome in ("rejected", "waiting"):
            pushes[outcome] += 1
        elif outcome in LANDED_OUTCOMES:
            pushes["auto" if r["approval"] is None else "approved"] += 1
        else:
            pushes["other"] += 1
    sent: Counter[str] = Counter()
    for r in conn.execute("SELECT sent_by FROM triage_approvals WHERE ts >= ?", (since,)):
        sent[str(r["sent_by"] or "")] += 1
    needs_info = int(
        conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status = 'needs-info' AND started >= ?",
            (since,),
        ).fetchone()[0]
    )
    return {
        "days": win_days,
        "since": since,
        "pushes": {k: pushes[k] for k in ("auto", "approved", "rejected", "waiting", "other")},
        "tickets": {
            "triage": sent["triage"],
            "owner": sum(n for actor, n in sent.items() if principals.is_person(actor)),
        },
        "needs_info": needs_info,
    }


def _query_grid(conn: sqlite3.Connection, *, days: float | None = None) -> dict[str, object]:
    win_days, since = _window(days)
    expand = _has_money_expand(conn)
    cost_expr = (
        "COALESCE(cost_nusd, CAST(round(COALESCE(cost_usd, 0.0) * 1e9) AS INTEGER))"
        if expand
        else "CAST(round(COALESCE(cost_usd, 0.0) * 1e9) AS INTEGER)"
    )
    paid_expr = "COALESCE(paid_nusd, 0)" if _has_money_backfill(conn) else "0"
    days_map: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        f"SELECT substr(started, 1, 10) AS day, COUNT(*) AS runs, "  # noqa: S608
        f"SUM(CASE WHEN status IN ('pr', 'merged', 'done') THEN 1 ELSE 0 END) AS shipped, "
        f"SUM(CASE WHEN status IN ('failed', 'rejected', 'interrupted') THEN 1 ELSE 0 END) AS failed, "
        f"COALESCE(SUM({cost_expr}), 0) AS cost_nusd "
        "FROM runs WHERE started >= ? GROUP BY substr(started, 1, 10) ORDER BY day",
        (since,),
    ):
        days_map[str(r["day"])] = {
            "runs": int(r["runs"]),
            "shipped": int(r["shipped"]),
            "failed": int(r["failed"]),
            "cost_nusd": int(r["cost_nusd"]),
            "steps": 0,
            "paid_nusd": 0,
        }
    for s in conn.execute(
        f"SELECT substr(ts, 1, 10) AS day, COUNT(*) AS steps, COALESCE(SUM({paid_expr}), 0) AS paid_nusd "  # noqa: S608
        "FROM steps WHERE ts >= ? GROUP BY substr(ts, 1, 10) ORDER BY day",
        (since,),
    ):
        entry = days_map.setdefault(
            str(s["day"]), {"runs": 0, "shipped": 0, "failed": 0, "cost_nusd": 0, "steps": 0, "paid_nusd": 0}
        )
        entry["steps"] = int(s["steps"])
        entry["paid_nusd"] = int(s["paid_nusd"])
    return {
        "days": win_days,
        "since": since,
        "cells": [
            {
                "day": day,
                **vals,
                "cost_usd": vals["cost_nusd"] / 1_000_000_000,
                "paid_usd": vals["paid_nusd"] / 1_000_000_000,
            }
            for day, vals in sorted(days_map.items())
        ],
    }


def _query_burnup(conn: sqlite3.Connection, *, days: float | None = None) -> dict[str, object]:
    win_days, since = _window(days)
    opened: Counter[str] = Counter()
    closed: Counter[str] = Counter()
    for r in conn.execute("SELECT substr(created, 1, 10) AS day, COUNT(*) AS n FROM tasks WHERE created >= ? GROUP BY 1", (since,)):
        if r["day"]:
            opened[str(r["day"])] += int(r["n"])
    for r in conn.execute(
        "SELECT substr(updated, 1, 10) AS day, COUNT(*) AS n FROM tasks "
        "WHERE state = 'done' AND updated >= ? GROUP BY 1",
        (since,),
    ):
        if r["day"]:
            closed[str(r["day"])] += int(r["n"])
    all_days = sorted(opened.keys() | closed.keys())
    cum_open = 0
    cum_done = 0
    points: list[dict[str, object]] = []
    for day in all_days:
        cum_open += opened[day]
        cum_done += closed[day]
        points.append({"day": day, "opened": opened[day], "done": closed[day],
                       "total_opened": cum_open, "total_done": cum_done})
    return {"days": win_days, "since": since, "points": points}


def _query_evals(
    conn: sqlite3.Connection,
    *,
    days: float | None = None,
    since_override: str | None = None,
) -> dict[str, object]:
    if since_override is not None:
        win_days = 14.0 if days is None else float(days)
        since = since_override
    else:
        win_days, since = _window(14.0 if days is None else days)
    cost_expr = (
        "COALESCE(cost_nusd, CAST(round(COALESCE(cost_usd, 0.0) * 1e9) AS INTEGER))"
        if _has_money_expand(conn)
        else "CAST(round(COALESCE(cost_usd, 0.0) * 1e9) AS INTEGER)"
    )
    rows = conn.execute(
        f"SELECT repo, COUNT(*) AS tasks, COALESCE(SUM(passed), 0) AS passed, "  # noqa: S608
        f"COALESCE(SUM({cost_expr}), 0) AS cost_nusd, MAX(ts) AS last "
        "FROM evals WHERE ts >= ? AND source = ? GROUP BY repo ORDER BY repo",
        (since, BASELINE_EVAL_SOURCE),
    ).fetchall()
    return {
        "days": win_days,
        "since": since,
        "repos": [
            {
                "repo": str(r["repo"] or ""),
                "tasks": int(r["tasks"]),
                "passed": int(r["passed"]),
                "pass_rate": round(int(r["passed"]) / int(r["tasks"]), 2) if int(r["tasks"]) else None,
                "cost_nusd": int(r["cost_nusd"]),
                "cost_usd": int(r["cost_nusd"]) / 1_000_000_000,
                "last": str(r["last"] or ""),
            }
            for r in rows
        ],
    }


def _query_jev(conn: sqlite3.Connection, *, days: float | None = None) -> dict[str, object]:
    win_days, since = _window(days)
    cfg = config.load_or_problem()[0]
    rows = [
        {**dict(r), "answers": json.loads(r["answers"]) if r["answers"] else {}}
        for r in conn.execute("SELECT * FROM verdicts WHERE at >= ? ORDER BY at", (since,))
    ]
    kinds: dict[str, dict] = {}
    questions: dict[str, dict] = {}
    near: list[dict] = []
    close: list[dict] = []
    for r in rows:
        k = kinds.setdefault(r["kind"], {"verdicts": 0, "held": 0, "outcomes": Counter()})
        k["verdicts"] += 1
        k["held"] += bool(r["held"])
        k["outcomes"][r["outcome"] or "open"] += 1
        for name, score in r["answers"].items():
            q = questions.setdefault(f"{r['kind']} {name}", {"asked": 0, "held": 0, "outcomes": Counter()})
            q["asked"] += 1
            if jev_verdicts.holds(score):
                q["held"] += 1
                q["outcomes"][r["outcome"] or "open"] += 1
                if jev_verdicts.distance(score) < jev_verdicts.NEAR:
                    close.append(jev_verdicts.scored_row(r, name, score))
            elif jev_verdicts.distance(score) <= jev_verdicts.NEAR:
                near.append(jev_verdicts.scored_row(r, name, score))
    landed = {r["sha"]: r for r in rows if r["sha"]}
    reverts = jev_verdicts.reverted_shas(cfg, since) if landed else {}
    reverted = [
        {
            "run_id": r["run_id"],
            "loop": r["loop"] or "",
            "about": r["about"] or "",
            "sha": full,
            "approval": r["approval"],
            "reverted_by": by,
        }
        for sha, by in reverts.items()
        for full, r in landed.items()
        if full.startswith(sha)
    ]
    held_by_jev = {r["run_id"] for r in rows if r["outcome"] == "rejected" and r["held"]}
    cleared: dict[str, dict] = {}
    for r in rows:
        if r["outcome"] != "rejected" or r["run_id"] in held_by_jev:
            continue
        x = cleared.setdefault(
            r["run_id"],
            {
                "run_id": r["run_id"],
                "loop": r["loop"] or "",
                "about": r["about"] or "",
                "approval": r["approval"],
                "sha": r["sha"] or "",
                "closest": None,
            },
        )
        for name, score in r["answers"].items():
            if x["closest"] is None or jev_verdicts.distance(score) < x["closest"]["distance"]:
                x["closest"] = jev_verdicts.scored_row(r, name, score) | {
                    "distance": round(jev_verdicts.distance(score), 4)
                }

    cost_expr = (
        "COALESCE(cost_nusd, CAST(round(COALESCE(cost_usd, 0.0) * 1e9) AS INTEGER))"
        if _has_money_expand(conn)
        else "CAST(round(COALESCE(cost_usd, 0.0) * 1e9) AS INTEGER)"
    )
    jev_cost_nusd = int(
        conn.execute(
            f"SELECT COALESCE(SUM({cost_expr}), 0) FROM steps WHERE runner = 'jev' AND ts >= ?",  # noqa: S608
            (since,),
        ).fetchone()[0]
    )
    return {
        "since": since,
        "days": win_days,
        "cost_nusd": jev_cost_nusd,
        "cost_usd": jev_cost_nusd / 1_000_000_000,
        "kinds": {k: {**v, "outcomes": dict(v["outcomes"])} for k, v in sorted(kinds.items())},
        "questions": {k: {**v, "outcomes": dict(v["outcomes"])} for k, v in sorted(questions.items())},
        "close_holds": sorted(close, key=lambda x: (x["kind"], x["question"], x["p"])),
        "near_misses": sorted(near, key=lambda x: (x["kind"], x["question"], x["p"])),
        "reverted": reverted,
        "cleared_rejected": list(cleared.values()),
    }


def query(
    chart: str,
    *,
    days: float | None = None,
    by: str | None = None,
    min_seq: int | None = None,
    price_version: str | None = None,
    run_id: str | None = None,
    step: str | None = None,
    since_override: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, object], str]:
    if chart not in CHARTS:
        raise ValueError(f"unknown insight chart {chart!r}; pick one of {', '.join(CHARTS)}")
    if conn is None:
        with foundation.one_read() as read_conn:
            return query(
                chart,
                days=days,
                by=by,
                min_seq=min_seq,
                price_version=price_version,
                run_id=run_id,
                step=step,
                since_override=since_override,
                conn=read_conn,
            )

    if chart == "cost":
        series = _query_cost(
            conn,
            days=days,
            since_override=since_override,
            by=by,
            price_version=price_version,
            run_id=run_id,
            step=step,
        )
    elif chart == "shipped":
        series = _query_shipped(conn, days=days, since_override=since_override)
    elif chart == "flow":
        series = _query_flow(conn, days=days)
    elif chart == "needs-you":
        series = _query_needs_you(conn, days=days, since_override=since_override)
    elif chart == "grid":
        series = _query_grid(conn, days=days)
    elif chart == "burnup":
        series = _query_burnup(conn, days=days)
    elif chart == "evals":
        series = _query_evals(conn, days=days, since_override=since_override)
    else:
        series = _query_jev(conn, days=days)

    derived_seq, stale = _coverage_state(conn, chart, min_seq=min_seq)
    payload: dict[str, object] = {
        "chart": chart,
        "series": series,
        "derived_seq": derived_seq,
        "stale": stale,
    }
    validator = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return payload, validator
