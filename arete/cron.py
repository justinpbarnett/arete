from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7))
ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *"}
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
SEARCH_DAYS = 8 * 366


class BadSchedule(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    def day_matches(self, t: datetime) -> bool:
        if t.month not in self.months:
            return False
        dom, dow = t.day in self.days, (t.isoweekday() % 7) in self.weekdays
        if self.any_day or self.any_weekday:
            return dom and dow
        return dom or dow

    def next_after(self, t: datetime) -> datetime:
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=SEARCH_DAYS)
        while t < limit:
            if not self.day_matches(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minutes:
                t += timedelta(minutes=1)
            else:
                return t
        raise BadSchedule(f"cron '{self.text}' never fires")


def field_values(spec: str, name: str, low: int, high: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        m = re.fullmatch(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?", part)
        if not m:
            raise BadSchedule(f"cron {name} '{part}' is not a number, range, list or step")
        start, end, step = m.group(1), m.group(2), int(m.group(3) or 1)
        a, b = (low, high) if start == "*" else (int(start), int(end) if end else int(start))
        if start != "*" and m.group(3) and not end:
            b = high
        if not (low <= a <= high and low <= b <= high) or a > b or step < 1:
            raise BadSchedule(f"cron {name} '{part}' is outside {low}-{high}")
        out.update(range(a, b + 1, step))
    return out


def parse(text: str) -> Cron:
    text = ALIASES.get(text.strip(), text.strip())
    parts = text.split()
    if len(parts) != 5:
        raise BadSchedule(f"cron '{text}' needs 5 fields: minute hour day month weekday")
    values = [field_values(p, *f) for p, f in zip(parts, FIELDS, strict=True)]
    weekdays = frozenset(d % 7 for d in values[4])
    return must_fire(Cron(text, *map(frozenset, values[:4]), weekdays, parts[2] == "*", parts[4] == "*"))


def must_fire(schedule: Cron) -> Cron:
    schedule.next_after(datetime(2000, 1, 1))
    return schedule


def seconds(text: str | float) -> float:
    if isinstance(text, float | int):
        return float(text)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", text)
    if not m:
        raise BadSchedule(f"every '{text}' needs a number and a unit: 30s, 5m, 1h, 1d")
    return float(m.group(1)) * UNITS[m.group(2)]
