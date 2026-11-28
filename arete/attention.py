from __future__ import annotations

import json
import sqlite3
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from . import configschema, paths, util
from .store import inbox as inbox_store
from .store import tasks as task_store

FILENAME = "attention.toml"
THRESHOLD_KEYS = (
    "newPer10Min",
    "floodBurst",
    "standingLimit",
    "standingAim",
    "highOnScreen",
    "shelvedLimit",
    "demoteAfterShelves",
    "needsYouPerDay",
)
ASK_KEYS = frozenset(("max_per_run", "settle", "expire_days", "remind_hours", "push_contact"))
DEFAULTS: dict[str, int] = {
    "newPer10Min": 1,
    "floodBurst": 10,
    "standingLimit": 10,
    "standingAim": 5,
    "highOnScreen": 1,
    "shelvedLimit": 30,
    "demoteAfterShelves": 3,
    "needsYouPerDay": 6,
}


@cache
def default_thresholds() -> dict[str, int]:
    default_file = paths.DEFAULTS_DIR / FILENAME
    if not default_file.is_file():
        return dict(DEFAULTS)
    parsed = tomllib.loads(default_file.read_bytes().decode("utf-8"))
    return parse_thresholds(parsed, where=f"defaults/{FILENAME}")


def parse_thresholds(data: object, *, where: str = FILENAME) -> dict[str, int]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{where} must be a TOML table")
    raw_dict = dict(data)
    configschema.validate("attention", raw_dict, where=where)
    merged = dict(DEFAULTS)
    for key in THRESHOLD_KEYS:
        if key not in raw_dict:
            continue
        value = raw_dict[key]
        if type(value) is not int or value < 1:
            raise ValueError(f"{where}: {key} must be an integer >= 1, not {value!r}")
        merged[key] = value
    return merged


def parse_bytes(raw: bytes, *, where: str = FILENAME) -> dict[str, int]:
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{where} is not valid TOML: {error}") from error
    return parse_thresholds(parsed, where=where)


def load_thresholds(config_dir: Path | None = None) -> dict[str, int]:
    root = (config_dir or paths.CONFIG_DIR).resolve()
    target = root / FILENAME
    if target.is_file():
        return parse_bytes(paths.read_confined(target), where=FILENAME)
    cfg_file = root / "config.toml" if config_dir is not None else paths.CONFIG_FILE
    if cfg_file.is_file():
        with suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
            raw_cfg = tomllib.loads(paths.read_confined(cfg_file).decode("utf-8"))
            ask_tbl = raw_cfg.get("ask")
            if isinstance(ask_tbl, dict) and any(k in ask_tbl for k in THRESHOLD_KEYS):
                return parse_thresholds(ask_tbl, where="config.toml [ask]")
    return dict(default_thresholds())


def _resolve_thresholds(cfg: object = None) -> dict[str, int]:
    if cfg is None:
        return load_thresholds()
    if isinstance(cfg, Path):
        return load_thresholds(cfg)
    if isinstance(cfg, Mapping):
        return parse_thresholds(cfg, where="attention")
    return load_thresholds()


def _utc_now(now: datetime | str | None = None) -> datetime:
    if now is None:
        current = util.now()
    elif isinstance(now, str):
        current = util.parse_iso(now)
    else:
        current = now
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def zone(
    standing: int,
    new_10m: int,
    buckets_6: Sequence[int] | None = None,
    cfg: object = None,
) -> tuple[str, str]:
    limits = _resolve_thresholds(cfg)
    standing_limit = limits["standingLimit"]
    standing_aim = limits["standingAim"]
    new_per_10m = limits["newPer10Min"]
    flood_burst = limits["floodBurst"]

    if new_10m >= flood_burst:
        return "OVERLOADED", f"flood in progress: {new_10m} new items in 10 minutes (burst limit {flood_burst})"
    if standing >= 2 * standing_limit:
        return "OVERLOADED", f"{standing} standing, at or above twice limit {standing_limit}"
    if standing >= standing_limit:
        return "REACTIVE", f"{standing} standing, limit {standing_limit}"
    if buckets_6 is not None and len(buckets_6) == 6 and all(count > new_per_10m for count in buckets_6):
        return "REACTIVE", f"sustained arrival above {new_per_10m} per 10 minutes for an hour"
    if standing < standing_aim and new_10m <= new_per_10m:
        return "ROBUST", f"{standing} standing, under aim {standing_aim}"
    if standing < standing_aim:
        return "STABLE", f"{standing} standing, {new_10m} new in 10 minutes"
    return "STABLE", f"{standing} standing, under limit {standing_limit}"


def andon(
    conn: sqlite3.Connection,
    cfg: object = None,
    *,
    now: datetime | str | None = None,
) -> dict[str, object]:
    limits = _resolve_thresholds(cfg)
    current = _utc_now(now)
    high = med = low = 0
    buckets = [0, 0, 0, 0, 0, 0]
    if inbox_store.available(conn):
        for prio, cnt in conn.execute(
            "SELECT priority, COUNT(*) FROM inbox WHERE state = 'open' GROUP BY priority"
        ).fetchall():
            if prio == 1:
                high = int(cnt)
            elif prio == 2:
                med = int(cnt)
            elif prio == 3:
                low = int(cnt)
        cutoff_60m = util.iso(current - timedelta(minutes=60))
        for (raised,) in conn.execute(
            "SELECT raised FROM inbox WHERE raised >= ? AND state != 'paused'",
            (cutoff_60m,),
        ).fetchall():
            if not isinstance(raised, str) or not raised:
                continue
            with suppress(ValueError):
                raised_dt = _utc_now(raised)
                age_sec = (current - raised_dt).total_seconds()
                if 0 <= age_sec < 3600:
                    idx = int(age_sec // 600)
                    if 0 <= idx < 6:
                        buckets[idx] += 1
    standing = high + med + low
    new_10m = buckets[0]
    zone_word, why = zone(standing, new_10m, buckets, limits)
    return {
        "count": high + med,
        "high": high,
        "med": med,
        "low": low,
        "standing": standing,
        "limit": limits["standingLimit"],
        "aim": limits["standingAim"],
        "zone": zone_word,
        "why": why,
    }


def _subject_run_id(subject: str) -> str:
    if subject.startswith("run:"):
        return subject.removeprefix("run:").strip()
    if subject.startswith("step:"):
        return subject.removeprefix("step:").partition("/")[0].strip()
    return ""


def needs_you(conn: sqlite3.Connection) -> dict[str, object]:
    task_items: dict[str, list[str]] = {}
    step_counts: Counter[tuple[str, str]] = Counter()
    if not inbox_store.available(conn):
        return {"tasks": task_items, "steps": dict(step_counts)}

    rows = conn.execute(
        "SELECT id, subject, task_id, run_id, detail FROM inbox WHERE state = 'open' ORDER BY priority ASC, id ASC"
    ).fetchall()
    if not rows:
        return {"tasks": task_items, "steps": dict(step_counts)}

    run_ids: set[str] = set()
    parsed_rows: list[tuple[str, str, str, str, dict[str, object]]] = []
    for row in rows:
        item_id = inbox_store.format_id(int(row["id"]))
        subject = str(row["subject"] or "")
        raw_task = str(row["task_id"] or "").strip()
        raw_run = str(row["run_id"] or "").strip() or _subject_run_id(subject)
        if raw_run:
            run_ids.add(raw_run)
        detail: dict[str, object] = {}
        try:
            loaded = json.loads(row["detail"] or "{}")
            if isinstance(loaded, dict):
                detail = loaded
        except ValueError:
            detail = {}
        parsed_rows.append((item_id, subject, raw_task, raw_run, detail))

    run_to_task_keys: dict[str, list[str]] = {}
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        has_task_id = "task_id" in run_cols
        select_sql = (
            f"SELECT id, repo, issue{', task_id' if has_task_id else ''} FROM runs WHERE id IN ({placeholders})"  # noqa: S608
        )
        run_rows = conn.execute(select_sql, tuple(sorted(run_ids))).fetchall()
        issue_pairs: set[tuple[str, str]] = set()
        for r in run_rows:
            rid = str(r["id"])
            keys: list[str] = []
            if has_task_id and r["task_id"] is not None:
                keys.append(task_store.format_task_id(int(r["task_id"])))
            issue_str = str(r["issue"] or "").strip()
            repo_str = str(r["repo"] or "").strip()
            if issue_str:
                keys.append(issue_str)
                if repo_str:
                    issue_pairs.add((repo_str, issue_str))
            run_to_task_keys[rid] = keys
        if issue_pairs:
            task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            has_num = "num" in task_cols
            for repo_str, issue_str in issue_pairs:
                found = None
                if issue_str.startswith(("T-", "t-")) and issue_str[2:].isdecimal() and has_num:
                    found = conn.execute(
                        "SELECT id, num FROM tasks WHERE num = ? LIMIT 1",
                        (int(issue_str[2:]),),
                    ).fetchone()
                if found is None:
                    found = conn.execute(
                        f"SELECT id{', num' if has_num else ''} FROM tasks WHERE repo = ? AND id = ? LIMIT 1",  # noqa: S608
                        (repo_str, issue_str),
                    ).fetchone()
                if found is not None:
                    canonical = task_store.format_task_id(int(found["num"])) if has_num and found["num"] is not None else str(found["id"])
                    for r in run_rows:
                        if str(r["repo"] or "") == repo_str and str(r["issue"] or "") == issue_str:
                            rid = str(r["id"])
                            if canonical not in run_to_task_keys[rid]:
                                run_to_task_keys[rid].append(canonical)
                            raw_id = str(found["id"])
                            if raw_id not in run_to_task_keys[rid]:
                                run_to_task_keys[rid].append(raw_id)

    def _add_task(key: str, item_id: str) -> None:
        if not key:
            return
        bucket = task_items.setdefault(key, [])
        if item_id not in bucket:
            bucket.append(item_id)

    for item_id, subject, raw_task, raw_run, detail in parsed_rows:
        if raw_run:
            node = detail.get("node")
            if isinstance(node, str) and node.strip():
                step_counts[(raw_run, node.strip())] += 1
        task_keys: list[str] = []
        if raw_task:
            task_keys.append(raw_task)
        if subject.startswith("task:"):
            task_keys.append(subject.removeprefix("task:").strip())
        elif subject.startswith("ticket:"):
            ticket_key = subject.removeprefix("ticket:").strip()
            if ticket_key:
                task_keys.append(ticket_key)
                if "#" in ticket_key:
                    task_keys.append(ticket_key.partition("#")[2])
        if raw_run and raw_run in run_to_task_keys:
            task_keys.extend(run_to_task_keys[raw_run])
        for key in task_keys:
            _add_task(key, item_id)

    return {"tasks": task_items, "steps": dict(step_counts)}


def handled_without_you_pct(
    conn: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
) -> int | None:
    current = _utc_now(now)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    ended_rows = conn.execute(
        "SELECT id FROM runs "
        "WHERE finished IS NOT NULL AND finished >= ? AND finished < ? "
        "AND COALESCE(dry_run, 0) = 0 AND status != 'dry-run' AND kind != 'chat'",
        (util.iso(day_start), util.iso(day_end)),
    ).fetchall()
    ended_ids = {str(r["id"]) for r in ended_rows}
    if not ended_ids:
        return None
    touched: set[str] = set()
    if inbox_store.available(conn):
        answered_rows = conn.execute(
            "SELECT run_id, subject FROM inbox "
            "WHERE answered_by != '' AND answered_by NOT LIKE 'run:%' AND answered_by != 'system'"
        ).fetchall()
        for row in answered_rows:
            rid = str(row["run_id"] or "").strip() or _subject_run_id(str(row["subject"] or ""))
            if rid in ended_ids:
                touched.add(rid)
    without_you = len(ended_ids) - len(touched)
    quotient, remainder = divmod(without_you * 100, len(ended_ids))
    twice = remainder * 2
    if twice > len(ended_ids) or (twice == len(ended_ids) and quotient % 2 == 1):
        quotient += 1
    return quotient
