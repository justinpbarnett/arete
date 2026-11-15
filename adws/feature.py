from dataclasses import replace

from arete import factory
from arete.verify import Verified
from arete.pipeline import round_name
from arete.flows import BUILD, CHECK_REPAIR, REVIEW_FIX, documents, end_dry_run, ended, finish, step

SKIPPED_LABEL = "refactor"
SKIPPED_TEXT = f"Check in use skipped because the ticket is labeled `{SKIPPED_LABEL}`."
PREDATES = "This check in use predates the review fixes."


def feature(flow):
    return built(flow, [("plan", {}), ("implement", {"tree": str(flow.ctx.ws), "round": BUILD})])


def built(flow, first):
    job = flow.ctx
    ws = str(job.ws)
    for name, kw in [("prepare", {"tree": ws}), *first]:
        if not (outcome := step(flow, name, **kw)).passed:
            return ended(flow, outcome)
    tries = job.run.settings(CHECK_REPAIR).tries
    attempt, outcome = 1, step(flow, "checks", tree=ws, attempt=1, tries=tries)
    while not outcome.passed and attempt < tries:
        attempt += 1
        step(flow, "implement", tree=ws, round=f"fix{attempt}", feedback=outcome.feedback, attempt=attempt,
             tries=tries)
        outcome = step(flow, "checks", tree=ws, attempt=attempt, tries=tries)
    if not outcome.passed:
        step(flow, "learn", tree=ws, apply=False)
        return finish(flow, "failed", why=f"checks still failing after {attempt} attempts")
    skipped = SKIPPED_LABEL in job.ticket.labels
    if skipped:
        job.verified = Verified("unverified", SKIPPED_TEXT)
    else:
        step(flow, "verify", tree=ws)
    reviews = max(1, job.run.settings("review").tries)
    n, outcome = 1, step(flow, "review", attempt=1, tries=reviews)
    fixed = False
    while not outcome.passed and n < reviews:
        if outcome.feedback:
            step(flow, "implement", tree=ws, round=round_name(REVIEW_FIX, n), feedback=outcome.feedback)
            fixed = True
            if not (checked := checked_after(flow, ws, "the review fixes", n)).passed:
                return finish(flow, "failed", why="the checks failed after the review fixes", detail=checked.feedback)
            if not skipped:
                job.verified = predating_fixes(job.verified)
        n += 1
        outcome = step(flow, "review", attempt=n, tries=reviews)
    if outcome.passed and fixed and not skipped:
        step(flow, "verify", tree=ws)
    step(flow, "learn", tree=ws, apply=True)
    if documents(job):
        step(flow, "document", tree=ws)
    if job.run.dry_run:
        return end_dry_run(flow)
    for name, kw in [("pr", {"tree": ws}), *([("ci", {"tree": ws})] if job.run.repo.parts else []), ("publish", {})]:
        if not (outcome := step(flow, name, **kw)).passed:
            break
    return ended(flow, outcome)


def predating_fixes(verified):
    if verified.verdict == "unverified" or PREDATES in verified.summary:
        return verified
    return replace(verified, summary=f"{verified.summary}\n\n{PREDATES}".strip())


def checked_after(flow, ws, after, n):
    tries = flow.ctx.run.settings(CHECK_REPAIR).tries
    outcome, attempt = step(flow, "checks", tree=ws, after=after), 1
    while not outcome.passed and attempt < tries:
        attempt += 1
        step(flow, "implement", tree=ws, round=f"{CHECK_REPAIR}{n}_{attempt}", feedback=outcome.feedback,
             attempt=attempt, tries=tries)
        outcome = step(flow, "checks", tree=ws, after=after)
    return outcome


if __name__ == "__main__":
    factory.main(feature)
