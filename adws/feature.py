from arete import factory
from arete.pipeline import round_name
from arete.flows import BUILD, REVIEW_FIX, end_dry_run, ended, finish, step


def feature(flow):
    return built(flow, [("plan", {}), ("implement", {"tree": str(flow.ctx.ws), "round": BUILD})])


def built(flow, first):
    job = flow.ctx
    ws = str(job.ws)
    for name, kw in [("prepare", {"tree": ws}), *first]:
        if not (outcome := step(flow, name, **kw)).passed:
            return ended(flow, outcome)
    tries = job.run.settings(BUILD).tries
    attempt, outcome = 1, step(flow, "checks", tree=ws, attempt=1, tries=tries)
    while not outcome.passed and attempt < tries:
        attempt += 1
        step(flow, "implement", tree=ws, round=f"fix{attempt}", feedback=outcome.feedback, attempt=attempt,
             tries=tries)
        outcome = step(flow, "checks", tree=ws, attempt=attempt, tries=tries)
    if not outcome.passed:
        step(flow, "learn", tree=ws, apply=False)
        return finish(flow, "failed", why=f"checks still failing after {attempt} attempts")
    step(flow, "verify", tree=ws)
    reviews = max(1, job.run.settings("review").tries)
    n, outcome = 1, step(flow, "review", attempt=1, tries=reviews)
    while not outcome.passed and n < reviews:
        if outcome.feedback:
            step(flow, "implement", tree=ws, round=round_name(REVIEW_FIX, n), feedback=outcome.feedback)
            if not (checked := step(flow, "checks", tree=ws, after="the review fixes")).passed:
                return finish(flow, "failed", why="the checks failed after the review fixes", detail=checked.feedback)
            step(flow, "verify", tree=ws)
        n += 1
        outcome = step(flow, "review", attempt=n, tries=reviews)
    step(flow, "learn", tree=ws, apply=True)
    if job.run.dry_run:
        return end_dry_run(flow)
    for name, kw in [("pr", {"tree": ws}), *([("ci", {"tree": ws})] if job.run.repo.parts else []), ("publish", {})]:
        if not (outcome := step(flow, name, **kw)).passed:
            break
    return ended(flow, outcome)


if __name__ == "__main__":
    factory.main(feature)
