from __future__ import annotations

import time
from dataclasses import dataclass

from . import config, runner, steps, util
from .agent import Result
from .pipeline import prompt

TEXTS = {"type": "array", "items": {"type": "string"}}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_search": {"type": "boolean"},
        "covered_by": TEXTS,
        "questions": TEXTS,
        "tasks": {"type": "array", "items": {
            "type": "object",
            "properties": {"goal": {"type": "string"}, "queries": TEXTS, "max_sources": {"type": "integer"}},
            "required": ["goal", "queries", "max_sources"]}},
        "source_criteria": TEXTS,
        "claims": TEXTS,
    },
    "required": ["needs_search", "covered_by", "questions", "tasks", "source_criteria", "claims"],
}
TASKS = (3, 7)
QUERIES = (1, 3)
QUERY_CHARS = 120
MAX_SOURCES = (1, 8)
LISTED = (1, 8)
MIN_CALL_SECONDS = 60


def texts(value, name: str, limits: tuple[int, int]) -> list[str]:
    low, high = limits
    if not (isinstance(value, list) and low <= len(value) <= high
            and all(isinstance(v, str) and v.strip() for v in value)):
        return [f"{name} must be {low} to {high} non-empty strings"]
    return []


def task_problems(i: int, task) -> list[str]:
    if not isinstance(task, dict):
        return [f"task {i} must be an object"]
    found = []
    if not (isinstance(task.get("goal"), str) and task["goal"].strip()):
        found.append(f"task {i} needs a goal")
    found += texts(task.get("queries"), f"task {i} queries", QUERIES)
    if isinstance(task.get("queries"), list) and any(isinstance(q, str) and len(q) > QUERY_CHARS
                                                      for q in task["queries"]):
        found.append(f"task {i} has a query over {QUERY_CHARS} characters")
    cap = task.get("max_sources")
    if type(cap) is not int or not MAX_SOURCES[0] <= cap <= MAX_SOURCES[1]:
        found.append(f"task {i} max_sources must be a whole number from {MAX_SOURCES[0]} to {MAX_SOURCES[1]}")
    return found


def problems(plan, family: list[str]) -> list[str]:
    if not isinstance(plan, dict):
        return ["the plan is not an object"]
    if type(plan.get("needs_search")) is not bool:
        return ["needs_search must be true or false"]
    found = texts(plan.get("questions"), "questions", LISTED)
    covered = plan.get("covered_by")
    known = {ref.lower() for ref in family}
    if not isinstance(covered, list) or not all(isinstance(c, str) for c in covered):
        return [*found, "covered_by must be a list of ticket keys"]
    unknown = [c for c in covered if c.lower() not in known]
    tasks = plan.get("tasks")
    if plan["needs_search"]:
        if not (isinstance(tasks, list) and TASKS[0] <= len(tasks) <= TASKS[1]):
            found.append(f"tasks must be {TASKS[0]} to {TASKS[1]} search tasks")
        else:
            for i, task in enumerate(tasks, 1):
                found += task_problems(i, task)
        found += texts(plan.get("source_criteria"), "source_criteria", LISTED)
        found += texts(plan.get("claims"), "claims", LISTED)
    else:
        if tasks != []:
            found.append("tasks must be empty when needs_search is false")
        if not covered:
            found.append("covered_by must name the family reports that cover the ticket")
    if unknown:
        found.append(f"covered_by names {', '.join(unknown)}, which is not a related report")
    return found


@dataclass
class Budget:
    usd: float
    seconds: float
    reserve_usd: float
    reserve_seconds: float
    spent_usd: float = 0.0
    used_seconds: float = 0.0
    waited: float = 0.0

    def stage(self, timeout_seconds: float) -> tuple[float, float]:
        return (self.usd - self.spent_usd - self.reserve_usd,
                min(timeout_seconds, self.seconds - self.used_seconds - self.reserve_seconds))

    def final(self) -> tuple[float, float]:
        return self.usd - self.spent_usd, self.seconds - self.used_seconds

    def short(self, usd: float, seconds: float) -> bool:
        return usd <= runner.BUDGET_FLOOR_USD or seconds < MIN_CALL_SECONDS

    def wait_until(self, until: str) -> None:
        started = time.monotonic()
        while (left := (util.parse_iso(until) - util.now()).total_seconds()) > 0:
            time.sleep(max(1.0, min(runner.HOLD_CHECK_SECONDS, left)))
        self.waited += time.monotonic() - started

    def call(self, text: str, cwd, usd: float, seconds: float, **kw) -> Result:
        spent, active = 0.0, 0.0
        while True:
            started = time.monotonic()
            res = runner.run(text, cwd, max_budget_usd=usd - spent, timeout=int(seconds - active), persist=False,
                             wait_out_limits=False, **kw)
            took = time.monotonic() - started
            active += took
            spent += res.paid_usd
            self.used_seconds += took
            self.spent_usd += res.paid_usd
            limit = res.usage_limit
            if not limit or not limit.until or self.short(usd - spent, seconds - active):
                return res
            self.wait_until(limit.until)

    def spent_words(self) -> str:
        return (f"{util.money(self.spent_usd)} of {util.money(self.usd)} and "
                f"{round(self.used_seconds / 60)} of {round(self.seconds / 60)} minutes used")


@dataclass
class Planned:
    plan: dict | None
    error: str = ""
    tries: int = 0


def refusal(faults: list[str]) -> str:
    return ("\n\n## Your last plan was refused\n\n" + "\n".join(f"- {f}" for f in faults)
            + "\n\nReturn a corrected plan.")


def plan(cfg: config.Config, budget: Budget, text: str, cwd, add_dirs: list, ref: str, family: list[str]) -> Planned:
    s = steps.resolve(cfg, None, "research_plan")
    feedback, faults, n = "", [], 0
    for n in range(1, s.tries + 1):
        usd, seconds = budget.stage(s.timeout_minutes * 60)
        if budget.short(usd, seconds):
            return Planned(None, f"planning used its share of the research budget ({budget.spent_words()}), "
                                 "so no search ran", n - 1)
        res = budget.call(text + feedback, cwd, usd, seconds, model=s.model, runner=s.runner, effort=s.effort,
                          tools=list(s.tools), allowed_tools=list(s.tools), instructions=list(s.instructions),
                          json_schema=PLAN_SCHEMA, add_dirs=add_dirs, tier=s.tier, trace=("", f"research {ref} plan"),
                          cfg=cfg)
        faults = problems(res.structured, family) if res.ok else [
            util.first_line(res.error or "the planner gave no plan")]
        if not faults:
            return Planned(res.structured, tries=n)
        if res.usage_limit:
            break
        feedback = refusal(faults)
    return Planned(None, f"the planner gave no valid plan in {util.count(n, 'try', 'tries')}, so no search ran: "
                         f"{'; '.join(faults)[:300]}", n)


def bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def brief(plan: dict) -> str:
    questions = bullets(plan["questions"])
    if not plan["needs_search"]:
        return prompt("research_covered", covered=", ".join(plan["covered_by"]), questions=questions)
    tasks = "\n".join(
        f"{i}. {t['goal']} (at most {util.count(t['max_sources'], 'source')})\n"
        + "\n".join(f"   - search: {q}" for q in t["queries"])
        for i, t in enumerate(plan["tasks"], 1))
    return prompt("research_planned", questions=questions, tasks=tasks, criteria=bullets(plan["source_criteria"]),
                  claims=bullets(plan["claims"]))
