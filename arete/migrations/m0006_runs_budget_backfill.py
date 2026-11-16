import json
import math
import re
from datetime import UTC, datetime

from arete import paths

PHASE = "backfill"
IDLE_WAITS = ("limit", "slot")
TICKET_KINDS = ("pipeline",)
REPAIRS = {"check_repair", "review_repair", "ci_repair"}
REPAIR_NAMES = ("repair", "fix", "verify_fix", "review_fix", "ci_fix")


def iso():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    return datetime.fromisoformat(value)


def metadata(run_id, name):
    try:
        value = json.loads((paths.RUNS_DIR / run_id / name).read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"run {run_id} has invalid {name}")
    return value


def read_lines(run_id):
    try:
        data = (paths.RUNS_DIR / run_id / "run.jsonl").read_bytes()
    except FileNotFoundError:
        return []
    found = []
    for piece in data.split(b"\n")[:-1]:
        try:
            item = json.loads(piece)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def repair_count(run_id: str) -> int:
    read = read_lines(run_id)
    saved = metadata(run_id, "budget.json")
    if "repair_calls" in saved:
        count = saved["repair_calls"]
        if type(count) is not int or count < 0:
            raise ValueError(f"run {run_id} has invalid repair usage")
        return count
    if saved:
        return sum(item.get("event") == "budget_repair" for item in read)
    found = set()
    for item in read:
        name = str(item.get("call") or item.get("step") or "")
        if (item.get("event") == "step_start" and (item.get("atom") in REPAIRS
                or name.startswith(REPAIR_NAMES))):
            found.add((str(item.get("step") or ""), int(item.get("try") or 1)))
    return len(found)


def elapsed(row: dict) -> float:
    return max(0, (parse_iso(row.get("finished") or iso()) -
                   parse_iso(row["started"])).total_seconds())


def parsed(value):
    try:
        return parse_iso(value)
    except (TypeError, ValueError, AttributeError):
        return None


def idle_waits(run_id: str, row: dict) -> dict[str, float]:
    found = {cause: 0.0 for cause in IDLE_WAITS}
    began = parsed(row.get("started"))
    if began is None:
        return found
    ended = parsed(row.get("finished")) or parsed(iso())
    opened = {}

    def add(since, until, cause):
        since, until = max(since, began), min(until, ended)
        if until > since:
            found[cause] += (until - since).total_seconds()

    for item in read_lines(run_id):
        cause = item.get("cause")
        if item.get("event") != "wait" or cause not in IDLE_WAITS:
            continue
        at = parsed(item.get("at"))
        if item.get("phase") == "start" and at is not None:
            opened[cause] = at
        elif item.get("phase") == "end":
            since = parsed(item.get("since"))
            opened.pop(cause, None)
            if since is not None and at is not None:
                add(since, at, cause)
    for cause, since in opened.items():
        add(since, ended, cause)
    return found


def active_seconds(run_id: str, row: dict) -> float:
    saved = metadata(run_id, "budget.json")
    if "run_seconds" in saved:
        return measured_seconds(run_id, saved, "run_seconds")
    return max(0, elapsed(row) - sum(idle_waits(run_id, row).values()))


def measured_seconds(run_id: str, saved: dict, field: str) -> float:
    value = saved[field]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"run {run_id} has invalid {field}")
    return float(value)


def verification_seconds(run_id: str, row: dict, conn) -> float:
    read = read_lines(run_id)
    saved = metadata(run_id, "budget.json")
    active, spent = saved.get("verification_active_since"), saved.get("verification_seconds", 0)
    opened = (measured_seconds(run_id, saved, "verification_open_seconds")
              if "verification_open_seconds" in saved else None)
    if type(spent) not in (int, float) or not math.isfinite(spent) or spent < 0:
        raise ValueError(f"run {run_id} has invalid verification usage")
    if active is not None and (not isinstance(active, str) or not active):
        raise ValueError(f"run {run_id} has invalid active verification checkpoint")
    if active or spent or opened is not None:
        if opened is not None:
            return float(spent) + (opened if active else 0)
        return max(0, float(spent)) + (max(0, (parse_iso(row.get("finished") or iso()) -
                                             parse_iso(active)).total_seconds()) if active else 0)
    if saved:
        active, spent = None, 0.0
        for item in read:
            if item.get("event") == "budget_verify_start":
                active = item["ts"]
            elif item.get("event") == "budget_verify_end":
                spent += max(0, float(item.get("seconds") or 0))
                active = None
        return spent + (max(0, (parse_iso(row.get("finished") or iso()) -
                                parse_iso(active)).total_seconds()) if active else 0)
    return sum(max(0, float(seconds or 0)) for name, seconds in conn.execute(
        "SELECT step, seconds FROM steps WHERE run_id = ?", (run_id,))
        if str(name or "").startswith(("verify", "grade")))


def apply(conn):
    columns = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    for raw in conn.execute("SELECT * FROM runs WHERE kind IN ('pipeline') "
                            "AND status != 'running' AND measured_at IS NULL").fetchall():
        row = dict(zip(columns, raw))
        rid = row["id"]
        try:
            saved = metadata(rid, "budget.json")
            for field in ("run_seconds", "verification_seconds", "verification_open_seconds", "repair_calls"):
                if field in saved:
                    measured_seconds(rid, saved, field)
            if "repair_calls" in saved and type(saved["repair_calls"]) is not int:
                raise ValueError("invalid repair usage")
            if "run_seconds" in saved:
                at = saved.get("run_seconds_at")
                if not isinstance(at, str) or not re.fullmatch(
                        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", at):
                    raise ValueError("invalid measured timestamp")
                parse_iso(at)
                values = (saved["run_seconds"], saved.get("verification_seconds", 0),
                          saved.get("verification_open_seconds", 0), saved.get("repair_calls", 0), at)
            else:
                values = (active_seconds(rid, row), verification_seconds(rid, row, conn),
                          0, repair_count(rid), "")
            for field, value in zip(("active_seconds", "verify_seconds", "verify_open_seconds", "repair_calls"),
                                    values):
                measured_seconds(rid, {field: value}, field)
            conn.execute("UPDATE runs SET active_seconds=?, verify_seconds=?, verify_open_seconds=?, "
                         "repair_calls=?, measured_at=? WHERE id=?", (*values, rid))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as e:
            raise ValueError(f"run {rid}: {e}") from e

