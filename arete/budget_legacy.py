import math

from . import run_lines, state, util

IDLE_WAITS = ("limit", "slot")
REPAIRS = {"check_repair", "review_repair", "ci_repair"}
REPAIR_NAMES = ("repair", "fix", "verify_fix", "review_fix", "ci_fix")


def metadata(run_id, name):
    from .runlimits import metadata as read

    return read(run_id, name)


def repair_count(run_id: str) -> int:
    read = run_lines.read(run_id)
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
    return max(0, (util.parse_iso(row.get("finished") or util.iso()) -
                   util.parse_iso(row["started"])).total_seconds())


def parsed(value):
    try:
        return util.parse_iso(value)
    except (TypeError, ValueError, AttributeError):
        return None


def idle_waits(run_id: str, row: dict) -> dict[str, float]:
    found = {cause: 0.0 for cause in IDLE_WAITS}
    began = parsed(row.get("started"))
    if began is None:
        return found
    ended = parsed(row.get("finished")) or parsed(util.iso())
    opened = {}

    def add(since, until, cause):
        since, until = max(since, began), min(until, ended)
        if until > since:
            found[cause] += (until - since).total_seconds()

    for item in run_lines.read(run_id):
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


def verification_seconds(run_id: str, row: dict) -> float:
    read = run_lines.read(run_id)
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
        return max(0, float(spent)) + (max(0, (util.parse_iso(row.get("finished") or util.iso()) -
                                             util.parse_iso(active)).total_seconds()) if active else 0)
    if saved:
        active, spent = None, 0.0
        for item in read:
            if item.get("event") == "budget_verify_start":
                active = item["ts"]
            elif item.get("event") == "budget_verify_end":
                spent += max(0, float(item.get("seconds") or 0))
                active = None
        return spent + (max(0, (util.parse_iso(row.get("finished") or util.iso()) -
                                util.parse_iso(active)).total_seconds()) if active else 0)
    return sum(max(0, float(step.get("seconds") or 0)) for step in state.steps(run_id)
               if str(step.get("step") or "").startswith(("verify", "grade")))


