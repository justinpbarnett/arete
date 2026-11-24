from dataclasses import replace

from arete import factory, patching
from arete.flows import BUILD, CHECK_REPAIR, REVIEW_FIX, deliver, ended, finish, retry_environment, step
from arete.pipeline import round_name
from arete.verify import Verified

SKIPPED_LABEL = "refactor"
SKIPPED_TEXT = f"Check in use skipped because the ticket is labeled `{SKIPPED_LABEL}`."
PREDATES = "This check in use predates the review fixes."


def feature(flow):
    return start(flow)


def start(flow, request_patch=False):
    job = flow.ctx
    if job.run.environment_release:
        return retry_environment(flow)
    ws = str(job.ws)
    if not (outcome := step(flow, "prepare", tree=ws)).passed:
        return ended(flow, outcome)
    requested = request_patch or patching.requested(job.layer, job.ticket, automatic=job.cfg.triage.route_fixes)
    refusal = ("multi-repository changes require a full plan" if len(job.checkouts) != 1 else
               patching.specification_refusal(job.ticket))
    if requested and not refusal:
        if not (outcome := step(flow, "patch", tree=ws)).passed:
            return ended(flow, outcome)
        if not outcome.data.get("escalate"):
            return built(flow, [], prepared=True, patch=True)
    elif requested:
        job.run.log(f"patch: escalating to the feature flow: {refusal}")
    return built(flow, [("plan", {}), ("implement", {"tree": ws, "round": BUILD})], prepared=True)


def built(flow, first, prepared=False, patch=False):
    job = flow.ctx
    ws = str(job.ws)
    for name, kw in [*([] if prepared else [("prepare", {"tree": ws})]), *first]:
        if not (outcome := step(flow, name, **kw)).passed:
            return ended(flow, outcome)
    tries = job.run.settings(CHECK_REPAIR).tries
    attempt, outcome = 1, step(flow, "checks", tree=ws, attempt=1, tries=tries, **({"quick": True} if patch else {}))
    while not outcome.passed and attempt < tries:
        attempt += 1
        if not (fixed := step(flow, "implement", tree=ws, round=f"fix{attempt}", feedback=outcome.feedback,
                              attempt=attempt, tries=tries)).passed:
            return ended(flow, fixed)
        outcome = step(flow, "checks", tree=ws, attempt=attempt, tries=tries, **({"quick": True} if patch else {}))
    if not outcome.passed:
        step(flow, "learn", tree=ws, apply=False)
        return finish(flow, "failed", why=f"checks still failing after {attempt} attempts")
    skipped = SKIPPED_LABEL in job.ticket.labels
    if skipped:
        job.verified = Verified("unverified", SKIPPED_TEXT)
    elif patch:
        job.verified = Verified("unverified", "Focused verification follows review convergence on the final tree.")
    else:
        if not (verified := step(flow, "verify", tree=ws)).passed:
            return ended(flow, verified)
    reviews = max(1, job.run.settings("review").tries)
    if patch:
        reviews = min(reviews, job.layer.patch.review_tries)
    n, outcome = 1, step(flow, "review", attempt=1, tries=reviews)
    fixed = False
    while not outcome.passed and n < reviews:
        if outcome.feedback:
            repaired = step(flow, "implement", tree=ws, round=round_name(REVIEW_FIX, n), feedback=outcome.feedback)
            if not repaired.passed:
                return ended(flow, repaired)
            fixed = True
            if not (checked := checked_after(flow, ws, "the review fixes", n, quick=True)).passed:
                return finish(flow, "failed", why="the checks failed after the review fixes", detail=checked.feedback)
            if not skipped:
                job.verified = predating_fixes(job.verified)
        n += 1
        outcome = step(flow, "review", attempt=n, tries=reviews)
    why = patching.change_refusal(job.layer.patch, job.b.restage().files, job.b.staged.lines) if patch else ""
    if patch and (why or not outcome.passed):
        job.run.log(f"patch: escalating to the feature flow: {why or 'review needs more than the patch repair budget'}")
        return built(flow, [("plan", {}), ("implement", {"tree": ws, "round": BUILD})], prepared=True)
    review_needed = False
    while blocked := final_blocker(flow, ws, patch=patch, fixed=fixed, skipped=skipped, review_needed=review_needed):
        why, outcome = blocked
        if not why:
            return ended(flow, outcome)
        if not (outcome and outcome.next == "implement" and outcome.data.get("focused_hold") and n < reviews):
            return finish(flow, "failed", why=why, detail=outcome.feedback if outcome else "")
        repaired = step(flow, "implement", tree=ws, round=round_name(REVIEW_FIX, n), feedback=outcome.feedback)
        if not repaired.passed:
            return ended(flow, repaired)
        n += 1
        fixed = review_needed = True
        if not skipped:
            job.verified = predating_fixes(job.verified)
    if patch and (why := patching.change_refusal(job.layer.patch, job.b.restage().files, job.b.staged.lines)):
        job.run.log(f"patch: escalating to the feature flow: {why}")
        return built(flow, [("plan", {}), ("implement", {"tree": ws, "round": BUILD})], prepared=True)
    return deliver(flow)


def final_blocker(flow, ws, *, patch, fixed, skipped, review_needed):
    job = flow.ctx
    before = factory.fingerprint(ws)
    if not (checked := final_checks(flow, ws, review_needed=review_needed)).passed:
        return "the final full checks did not converge with review", checked
    if skipped or not job.review.approved() or not (patch or fixed or factory.fingerprint(ws) != before):
        return None
    before = factory.fingerprint(ws)
    if not (verified := step(flow, "verify", tree=ws)).passed:
        return "", verified
    if patch and patching.allowed_gap(job.layer, job.ticket, job.verified):
        job.verified = replace(job.verified, verdict="unverified", summary=(
            job.verified.summary + "\nThe owner's patch policy permits this environmental gap; it is not a pass."))
    if (verified_tree := factory.fingerprint(ws)) == before:
        return None
    if not (checked := final_checks(flow, ws)).passed:
        return "the verification repairs did not converge with review", checked
    if factory.fingerprint(ws) != verified_tree:
        return "the final check repairs require new behavioral verification", None
    if not (reviewed := review_after_fixes(flow)).passed:
        return "the verification repairs need further review", reviewed
    return None


def final_checks(flow, ws, *, review_needed=False):
    job = flow.ctx
    before = factory.fingerprint(ws)
    checked = job.b.checked
    if checked.get("tree") and checked["tree"] == before and checked.get("commands") == job.layer.checks:
        job.b.report = checked["report"]
        outcome = factory.Outcome("pass")
    else:
        outcome = checked_after(flow, ws, "the final full checks", job.reviews + 1)
        if not outcome.passed:
            return outcome
        if factory.fingerprint(ws) != before:
            job.verified = predating_fixes(job.verified)
            review_needed = True
    return review_after_fixes(flow) if review_needed else outcome


def review_after_fixes(flow):
    attempt = flow.ctx.reviews + 1
    return step(flow, "review", attempt=attempt, tries=attempt)


def predating_fixes(verified):
    if verified.verdict == "unverified" or PREDATES in verified.summary:
        return verified
    return replace(verified, summary=f"{verified.summary}\n\n{PREDATES}".strip())


def checked_after(flow, ws, after, n, quick=False):
    tries = flow.ctx.run.settings(CHECK_REPAIR).tries
    extra = {"quick": True} if quick else {}
    outcome, attempt = step(flow, "checks", tree=ws, after=after, **extra), 1
    while not outcome.passed and attempt < tries:
        attempt += 1
        repaired = step(flow, "implement", tree=ws, round=f"{CHECK_REPAIR}{n}_{attempt}", feedback=outcome.feedback,
                        attempt=attempt, tries=tries)
        if not repaired.passed:
            return repaired
        outcome = step(flow, "checks", tree=ws, after=after, **extra)
    return outcome


if __name__ == "__main__":
    factory.main(feature)
