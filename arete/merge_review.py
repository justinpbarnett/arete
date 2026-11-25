from __future__ import annotations

import json
from dataclasses import asdict, replace

from . import config, delegation, gh, intake, issue_goal, jev, paths, pilot, run_lines, runlimits, tracker, util, vault
from .store import orchestration as controllers
from .store import triage as triage_store

PENDING = "Waiting for the issue orchestrator's merge decision"
MULTIPLE = "a change spanning repositories cannot be merged atomically"


def enrolled(run):
    return getattr(getattr(run, "budget", None), "issue_goal", False) is True


def candidate_path(repo, ticket):
    return issue_goal.directory(issue_goal.identity(repo, ticket)) / "merge-candidate.json"


def policy(cfg, layer):
    return controllers.digest([delegation.settings_sha(cfg), delegation.settings_sha(layer)])


def approval_details(repo, ticket):
    from . import unblock

    key = ticket.ref if repo.tracker == "jira" else tracker.ticket_name(repo.name, ticket.ref)
    approval = triage_store.triage_approval(key) or {}
    return {"automatic": intake.AUTO_LABEL in ticket.labels or approval.get("sent_by") == "triage",
            "assumptions": unblock.safe(json.dumps(approval.get("assumptions") or [])),
            "checks": unblock.safe(json.dumps(approval.get("checks") or []))}


def nominate(b, checkout, verified, review, base_sha, about):
    from . import pipeline, unblock

    run = b.run
    head = checkout.lease
    patch = pipeline.checkout_patch(checkout, [], f"{base_sha}..{head}")
    if not patch:
        return "there is no diff to risk screen"
    try:
        risk = jev.risk(vault.hidden(patch), vault.hidden(about), b.cfg, trace=(run.id, "automerge risk jev"))
    except jev.Unjudged:
        return "JEV could not judge the diff"
    excerpt = unblock.safe(patch, 80000)
    candidate = {
        "run": run.id, "repo": run.repo.name, "ticket": str(b.ticket.id), "url": checkout.pr, "github": checkout.github,
        "head": head, "base": base_sha, "target": checkout.base, "tree": b.checked["tree"],
        "specification": runlimits.specification(b.ticket), "policy": policy(b.cfg, replace(b.layer, dir=run.repo.layer_dir)),
        "triage": approval_details(run.repo, b.ticket),
        "risk_thresholds": [b.cfg.jev.risk_hold, b.cfg.jev.irreversible_hold],
        "local_checks": sorted(b.checked["commands"]), "check_report": unblock.safe(b.checked.get("report"), 6000),
        "verification": unblock.safe(verified.summary),
        "observed": unblock.safe(json.dumps(verified.checks), 10000),
        "review": unblock.safe(json.dumps(review.report()), 8000), "risk": asdict(risk),
        "files": unblock.safe(util.git(["diff", "--stat", f"{base_sha}..{head}"], checkout.wt), 8000),
        "diff_excerpt": excerpt, "diff_truncated": len(patch) > 80000,
        "rerun": (checkout.github, head) in b.ci_rerun_requested_heads,
    }
    path = candidate_path(run.repo.name, b.ticket.id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    util.write_durably(path, json.dumps(candidate).encode(), 0o600)
    return PENDING


def assess(cfg, repo, ticket, row, prs):
    from . import release_proof

    if (cfg.replay.all or paths.ISOLATED or row.get("status") != "pr" or row.get("dry_run")
            or row.get("only_step") or len(prs) != 1):
        return "", {}
    url, pr = next(iter(prs.items()))
    if (pr["state"] != "OPEN" or pr["isDraft"] or pr.get("reviewDecision") in ("CHANGES_REQUESTED", "REVIEW_REQUIRED")
            or pr.get("mergeStateStatus") not in ("BEHIND", "CLEAN") or pr.get("mergeable") != "MERGEABLE"):
        return "", {}
    layer = config.load_layer(repo.layer_dir)
    if layer.trust_level != "automerge":
        return "", {}
    candidate = util.read_json(candidate_path(repo.name, ticket.id), {})
    if not isinstance(candidate, dict) or candidate.get("run") != row["id"]:
        # Older releases recorded why they declined an otherwise eligible merge in the final run report.
        report = next((r for r in reversed(run_lines.read(row["id"])) if r.get("event") == run_lines.END), {})
        hold = report.get("merge_held")
        if not hold or hold in (MULTIPLE, "GitHub did not confirm whether it merged the PR"):
            return "", {}
        return "merge-verification", {}
    binding = {"repo": repo.name, "ticket": str(ticket.id), "url": url, "github": gh.pr_repo(url),
               "target": pr.get("baseRefName"), "head": pr["headRefOid"], "base": pr["baseRefOid"],
               "specification": runlimits.specification(ticket), "policy": policy(cfg, layer),
               "local_checks": sorted(layer.checks), "triage": approval_details(repo, ticket),
               "risk_thresholds": [cfg.jev.risk_hold, cfg.jev.irreversible_hold]}
    if (pr["mergeStateStatus"] == "BEHIND" or any(candidate.get(k) != v for k, v in binding.items())
            or not isinstance(candidate.get("rerun"), bool)
            or not {"tree", "review", "verification", "observed", "risk", "diff_excerpt", "diff_truncated"} <= candidate.keys()):
        return "merge-verification", {}
    if release_proof.missing_staging_promise(repo, ticket.id):
        return "", {}
    number = gh.pr_number(url)
    _, hold = gh.merge_readiness(binding["github"], number, binding["head"], binding["target"], binding["base"],
                                 required=pilot.required_check(cfg, repo, layer),
                                 **release_proof.pr_check_options(repo, ticket.id, binding["github"], binding["target"]))
    if hold or (candidate["rerun"] and not gh.publication_ci_passed(binding["github"], binding["head"])):
        return "", {}
    return "merge-review", candidate
