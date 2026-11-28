"""Frozen Python parity oracle for the built-in ADWs (feature, revise, repair).

Kept under tests/ per #299 / #878 to verify that arete/adws/*.toml interpreted by
arete.adw.drive makes identical calls with identical arguments in identical order.
"""
from __future__ import annotations

from arete import flows
from arete.pipeline import round_name


def feature(flow):
    return built(flow, [("plan", {}), ("implement", {"tree": str(flow.ctx.ws), "round": flows.BUILD})])


def revise(flow):
    return built(flow, [("revise", {"tree": str(flow.ctx.ws)})])


def repair(flow):
    return built(flow, [("repair", {"tree": str(flow.ctx.ws)})])


def built(flow, first):
    job = flow.ctx
    ws = str(job.ws)
    for name, kw in [("prepare", {"tree": ws}), *first]:
        if not (outcome := flows.step(flow, name, **kw)).passed:
            return flows.ended(flow, outcome)
    tries = job.run.settings(flows.CHECK_REPAIR).tries
    attempt, outcome = 1, flows.step(flow, "checks", tree=ws, attempt=1, tries=tries)
    while not outcome.passed and attempt < tries:
        attempt += 1
        fixed = flows.step(flow, "implement", tree=ws, round=f"fix{attempt}", feedback=outcome.feedback,
                           attempt=attempt, tries=tries)
        if not fixed.passed:
            return flows.ended(flow, fixed)
        outcome = flows.step(flow, "checks", tree=ws, attempt=attempt, tries=tries)
    if not outcome.passed:
        flows.step(flow, "learn", tree=ws, apply=False)
        return flows.finish(flow, "failed", why=f"checks still failing after {attempt} attempts",
                            detail=outcome.feedback)
    if not (outcome := flows.step(flow, "verify", tree=ws)).passed:
        return flows.ended(flow, outcome)
    reviews = max(1, job.run.settings("review").tries)
    n, outcome = 1, flows.step(flow, "review", attempt=1, tries=reviews)
    while not outcome.passed and n < reviews:
        if outcome.feedback:
            repaired = flows.step(flow, "implement", tree=ws, round=round_name(flows.REVIEW_FIX, n),
                                  feedback=outcome.feedback)
            if not repaired.passed:
                return flows.ended(flow, repaired)
            if not (checked := checked_after(flow, ws, "the review fixes", n)).passed:
                return flows.finish(flow, "failed", why="the checks failed after the review fixes",
                                    detail=checked.feedback)
        n += 1
        outcome = flows.step(flow, "review", attempt=n, tries=reviews)
    flows.step(flow, "learn", tree=ws, apply=True)
    if flows.documents(job):
        flows.step(flow, "document", tree=ws)
    if job.run.dry_run:
        return flows.end_dry_run(flow)
    for name, kw in [("pr", {"tree": ws}), *([("ci", {"tree": ws})] if job.run.repo.parts else []), ("publish", {})]:
        if not (outcome := flows.step(flow, name, **kw)).passed:
            break
    return flows.ended(flow, outcome)


def checked_after(flow, ws, after, n):
    tries = flow.ctx.run.settings(flows.CHECK_REPAIR).tries
    outcome, attempt = flows.step(flow, "checks", tree=ws, after=after), 1
    while not outcome.passed and attempt < tries:
        attempt += 1
        repaired = flows.step(flow, "implement", tree=ws, round=f"{flows.CHECK_REPAIR}{n}_{attempt}",
                              feedback=outcome.feedback, attempt=attempt, tries=tries)
        if not repaired.passed:
            return repaired
        outcome = flows.step(flow, "checks", tree=ws, after=after)
    return outcome
