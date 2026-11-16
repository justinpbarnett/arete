from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from decimal import Decimal

from . import agent, routing, runner, util
from .agent import Result

MIN_CALL_SECONDS = 60


def remaining_budget(which, amount):
    return 0.0 if which in agent.SUBSCRIPTION_RUNNERS else amount


def known(paid) -> bool:
    return type(paid) in (float, int) and math.isfinite(paid) and paid >= 0


def plus(a: float, b: float) -> float:
    return float(Decimal(str(a)) + Decimal(str(b)))


@dataclass
class Budget:
    usd: float
    seconds: float
    reserve_usd: float
    reserve_seconds: float
    spent_usd: float = 0.0
    used_seconds: float = 0.0
    waited: float = 0.0
    started: float = field(default_factory=lambda: time.monotonic())
    error: str = ""

    def elapsed(self) -> float:
        self.used_seconds = max(self.used_seconds, time.monotonic() - self.started)
        return self.used_seconds

    def stage(self, timeout_seconds: float) -> tuple[float, float]:
        return (self.usd - self.spent_usd - self.reserve_usd,
                min(timeout_seconds, self.seconds - self.elapsed() - self.reserve_seconds))

    def final(self) -> tuple[float, float]:
        return self.usd - self.spent_usd, self.seconds - self.elapsed()

    def short(self, usd: float, seconds: float) -> bool:
        return bool(self.error) or not (math.isfinite(usd) and math.isfinite(seconds)) or (
            usd <= routing.BUDGET_FLOOR_USD or seconds < MIN_CALL_SECONDS)

    def add(self, paid: float) -> None:
        self.spent_usd = plus(self.spent_usd, paid)

    def wait_until(self, until: str, deadline: float) -> bool:
        started = time.monotonic()
        while (left := (util.parse_iso(until) - util.now()).total_seconds()) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(runner.HOLD_CHECK_SECONDS, left, remaining))
        self.waited += time.monotonic() - started
        self.elapsed()
        return (util.parse_iso(until) - util.now()).total_seconds() <= 0

    def call(self, text: str, cwd, usd: float, seconds: float, *, reserve: bool = False, **kw) -> Result:
        spent = 0.0
        usd = min(usd, self.usd - self.spent_usd - (self.reserve_usd if reserve else 0))
        deadline = min(self.started + self.seconds - (self.reserve_seconds if reserve else 0),
                       time.monotonic() + seconds)
        which = routing.chosen(kw.get("model", ""), kw.get("runner", ""), kw["cfg"])[0]
        while True:
            remaining, seconds = usd - spent, deadline - time.monotonic()
            if self.short(remaining, seconds):
                return Result(ok=False, error=self.error or "the research call exhausted its shared budget")
            res = runner.run(text, cwd, max_budget_usd=remaining, timeout=int(seconds), persist=False,
                             wait_out_limits=False,
                             remaining_budget=lambda amount=remaining: remaining_budget(which, amount),
                             **kw)
            paid = res.paid_usd
            self.elapsed()
            if not known(paid):
                self.error = f"the research call's paid cost is unknown; known prior spend is {util.money(self.spent_usd)}"
            else:
                spent = plus(spent, paid)
                self.add(paid)
                if spent > usd:
                    self.error = "the research call exceeded its allowance; its incurred cost is recorded"
            if time.monotonic() > deadline:
                self.error = self.error or "the research call exceeded its shared deadline"
            if self.error:
                res.ok, res.error = False, self.error
                return res
            limit = res.usage_limit
            if not limit or not limit.until:
                return res
            if not self.wait_until(limit.until, deadline - MIN_CALL_SECONDS):
                res.ok, res.error = False, "the usage limit outlasts the research call's remaining deadline"
                return res

    def spent_words(self) -> str:
        return (f"{util.money(self.spent_usd)} of {util.money(self.usd)} and "
                f"{round(self.used_seconds / 60)} of {round(self.seconds / 60)} minutes used")
