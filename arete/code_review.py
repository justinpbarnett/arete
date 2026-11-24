"""Matt Pocock's Standards and Spec review, applied to Arete's staged candidate."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace

from . import factory, journeys, prompt_loader, util
from .review import Review
from .verify import Verified

METHOD = "code-review"
AXES = ("Standards", "Spec")
FINDING_SCHEMA = {
    "type": "object", "required": ["summary", "findings"], "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object", "required": ["file", "finding"],
            "properties": {"file": {"type": "string"}, "finding": {"type": "string"}}}},
    },
}
GUIDANCE = {
    "Standards": """Find the repository's documented rules, including CONTRIBUTING.md and CODING_STANDARDS.md
when present. Cite the file and rule for each violation. Separately flag possible Fowler smells in the changed
hunks: mysterious names, duplicate code, feature envy, data clumps, primitive obsession, repeated switches,
shotgun surgery, divergent change, speculative generality, message chains, middle men and refused bequests.
Treat smells as judgment calls. Repository standards take precedence. Skip rules enforced by tooling.""",
    "Spec": """Compare the changed behavior with the originating ticket and owner clarifications.
Look for missing or partial requirements, unrequested scope and incorrectly implemented requirements.
Cite the requirement and the changed path for each finding.""",
}


def requires_behavior(build):
    policy = build.layer.verify
    return bool(policy and (policy.probes or journeys.requested(policy.feature_map, build.ticket.title, build.ticket.body)))


def unverified():
    return Verified("unverified", "The orchestrator used code review and the configured checks for this candidate.",
                    finished=False, behavior_reached=False)


def completed(review):
    return review.asdict().get("method") == METHOD and review.verdict in ("pending", "approve") and not review.failed


def bases(checkouts):
    return {c.prefix: util.git(["merge-base", c.origin_base, "HEAD"], c.wt).strip() for c in checkouts}


def candidate_trees(checkouts):
    return {c.prefix: factory.fingerprint(c.wt) for c in checkouts}


def run_axes(build, excerpt, trees, spec):
    forks = bases(build.checkouts)
    comparison = ", ".join(f"{path or '.'}: git diff --cached {sha}" for path, sha in forks.items())

    brief = build.brief
    reports = {}
    runs = {axis: replace(build.run, sessions={}, step_calls={}, cost=0, paid=0) for axis in AXES}
    try:
        with ThreadPoolExecutor(max_workers=len(AXES)) as pool:
            futures = {}
            for axis, run in runs.items():
                source = (f"Issue {brief.ref}: {brief.title}\n{brief.body}\n{brief.comments or ''}\n"
                          f"{brief.attachments or ''}\n{spec}" if axis == "Spec" else "")
                text = prompt_loader.task_prompt(
                    f"Review the staged diff for {brief.repo} along the {axis} axis. {GUIDANCE[axis]}\n"
                    f"{source}\n{brief.workspace}\nThe pinned comparison is {comparison}. The changes are staged; "
                    f"use the staged diff against the named commit. Report findings only, under 400 words.\n{excerpt}")
                futures[axis] = pool.submit(copy_context().run, run.agent, f"review-{axis.lower()}", text, build.ws,
                    role="review", read_only=True, persist=False, allowed_tools=prompt_loader.bash_rules(build.read_bash),
                    json_schema=FINDING_SCHEMA)
            for axis, future in futures.items():
                reply = future.result()
                report = reply.structured
                if not (reply.ok and isinstance(report, dict) and isinstance(report.get("summary"), str)
                        and isinstance(report.get("findings"), list) and all(
                            isinstance(item, dict) and all(isinstance(item.get(k), str) and item[k].strip()
                                                          for k in ("finding", "file")) for item in report["findings"])):
                    return Review.failed_step([], [], {})
                reports[axis] = report
    finally:
        build.run.cost += sum(run.cost for run in runs.values())
        build.run.paid += sum(run.paid for run in runs.values())
    if not trees or candidate_trees(build.checkouts) != trees or bases(build.checkouts) != forks:
        return Review.failed_step([], [], {})
    findings = [{"id": f"{axis.lower()}-{i}", "axis": axis, "file": item["file"], "finding": item["finding"]}
                for axis in AXES for i, item in enumerate(reports[axis]["findings"], 1)]
    summary = "\n\n".join(f"**{axis}** — {reports[axis]['summary']}" for axis in AXES)
    return Review.stored({"method": METHOD, "verdict": "pending" if findings else "approve", "summary": summary,
                          "changes": reports["Spec"]["summary"], "findings": findings, "problems": [],
                          "earlier": [], "trees": trees, "bases": forks})


def disposition(report, decisions, trees):
    findings = report["findings"]
    by_id = {item["id"]: item for item in findings}
    if not trees or not isinstance(decisions, dict) or decisions.keys() != by_id.keys():
        raise ValueError("decide every finding by its reported ID")
    notes = []
    repaired = False
    for key, finding in by_id.items():
        decision = decisions[key]
        if not isinstance(decision, dict) or set(decision) != {"status", "reason"}:
            raise ValueError(f"{key} needs a status and a reason")
        status, reason = decision["status"], decision["reason"]
        if status not in ("fixed", "nonblocking") or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError(f"{key} needs fixed or nonblocking and a specific reason of 1–2000 characters")
        repaired |= status == "fixed"
        notes.append({"severity": "minor", "file": finding["file"],
                      "problem": f"{key} ({finding['axis']}) {finding['finding']} — {status}: {reason.strip()}"})
    if repaired != (report["trees"] != trees):
        raise ValueError("fixed findings require a changed candidate; changes beyond finding repairs need a new review")
    return Review.stored({**report, "source_trees": report["trees"], "trees": trees, "problems": notes,
                          "verdict": "approve", "decisions": decisions,
                          "summary": report["summary"] + "\n\nThe orchestrator accepted the candidate after assessing the findings."})
