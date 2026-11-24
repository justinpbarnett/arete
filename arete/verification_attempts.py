"""Decide whether a behavioral requirement needs another verifier launch."""
from __future__ import annotations

import platform
import re
from dataclasses import dataclass, replace

from . import prompt_loader, sandbox, verify
from .agent import traits_of
from .store.orchestration import digest


# Explicit issue conventions. A mention of verification inside product prose is common.
REQUIRED_IN_USE = re.compile(
    r"(?im)^\s*(?:Answer:\s*)?(?:#{1,6}\s*)?(?:[-*]\s+)?(?:\*\*)?"
    r"(?:done when\s*:?\s*(?:\*\*)?[\s,]*in verif(?:y|ication)\b|"
    r"(?:in-use|behavio[u]?ral|in use) verification (?:is )?required\b|"
    r"required (?:in-use |behavio[u]?ral )?verification\b|verification required\b|(?:verify|test) in use\b|verify (?:that|using|with|in the browser)\b)"
)


class Refused(ValueError):
    """The request did not launch a verifier or invalidate earlier evidence."""


def names(values):
    if not isinstance(values, list) or len(values) > 64 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in values):
        raise ValueError("requirements must be a list of up to 64 nonempty names of at most 2000 characters")
    return list(dict.fromkeys(verify.one_line(v) for v in values))


def capabilities(build, setting):
    """Use the approved tool surface, not the verifier's model name or effort."""
    traits = traits_of(setting.runner)
    tools = set(setting.tools) | set(traits.always_granted_tools)
    configured = build.layer.verify
    if configured:
        tools.update(prompt_loader.bash_rules(build.read_bash + configured.bash))
        if configured.start and traits.takes_mcp:
            tools.update(f"mcp__playwright__{tool}" for tool in verify.PLAYWRIGHT_TOOLS)
    if traits.takes_mcp:
        tools.update(f"mcp__{name}" for name in setting.mcp)
    tools.difference_update(setting.denied)
    return sorted(tools)


def host():
    return digest([platform.node(), platform.system(), platform.release(), sandbox.can_confine(False)])


def environment(host_id, tools):
    return digest([host_id, tools])


def supplies_missing(previous, tools, missing):
    approved = set(tools)
    return bool(missing) and set(missing) <= approved and not set(missing) <= set(previous)


def already_blocked(history, environment_id, requirements):
    covered = history.get(environment_id)
    return covered is not None and {name.casefold() for name in requirements} <= set(covered)


@dataclass
class Scope:
    prior: verify.Verified | None
    requirements: list[str]
    baseline_targets: list[str]
    reuse: bool


def plan(source, requested, host_id, tools):
    """Authorize a focused observation only on compatible inputs, or request a new baseline."""
    same_host = source.get("verification_host") == host_id
    prior = verify.Verified.stored(source) if same_host else None
    if not prior:
        if requested:
            raise Refused("focused verification needs a current baseline; use verification.run without requirements")
        return Scope(None, [], [], False)

    environment_id = environment(host_id, tools)
    same = source.get("verification_environment") == environment_id
    old_tools = source.get("verification_tools", [])
    history = source.get("blocked_environments", {})
    gaps = outstanding(prior)
    improved = supplies_missing(old_tools, tools, prior.missing_tools) and not already_blocked(history, environment_id, gaps)
    compatible = same or improved and set(old_tools) <= set(tools)
    if improved and not compatible:
        return Scope(prior, [], requested or gaps, False)
    if requested and not (prior.finished and (compatible or already_blocked(history, environment_id, requested))):
        raise Refused("focused verification needs a compatible baseline; use verification.run without requirements")

    unattempted = source.get("unattempted_requirements", [])
    targets = requested or (gaps if improved and prior.finished else unattempted if same else [])
    seen = observed(prior)
    pending = [name for name in targets if name.casefold() not in seen]
    if prior.verdict == "blocked" and not improved:
        # A new scenario can still be measured while an unrelated fixture remains unavailable.
        unavailable = {name.casefold() for name in gaps} - {name.casefold() for name in unattempted}
        pending = [name for name in pending if name.casefold() not in unavailable]
    reuse = (prior.verdict == "fail" or prior.verdict == "blocked" and not improved and not pending
             or prior.verdict == "pass" and same and not pending)
    return Scope(prior, pending, [], reuse)


def partial(report):
    if report.verdict == "pass" and report.unmeasured_requirements:
        return replace(report, verdict="blocked", blocked_by=report.blocked_by or
                       "The verifier could not measure: " + ", ".join(report.unmeasured_requirements))
    return report


def unattempted(report):
    """A successful baseline may name a scenario it never tried. Allow one focused measurement."""
    if report.verdict != "pass" or report.missing_capability or report.blocked_by or not report.finished:
        return []
    attempted = {verify.one_line(c.get("what", "")).casefold() for c in report.checks}
    return [name for name in report.unmeasured_requirements if name.casefold() not in attempted]


def outstanding(report):
    return list(dict.fromkeys([*report.unmeasured_requirements,
                               *(c["what"] for c in report.failed_checks if c.get("what"))]))


def observed(report):
    missing = {verify.one_line(name).casefold() for name in outstanding(report)}
    return {verify.one_line(c["what"]).casefold() for c in report.checks if c.get("passed") is True and c.get("what")} - missing


def advisory(report, authority):
    gaps = {verify.one_line(name).casefold() for name in outstanding(report)}
    owner = verify.one_line(authority).casefold()
    declared = {verify.one_line(name).casefold() for name in report.advisory_gaps}
    return (report.verdict == "blocked" and report.finished and report.behavior_reached and bool(gaps) and bool(observed(report))
            and gaps <= declared and all(name not in owner for name in gaps)
            and all(c.get("cause") == "environment" for c in report.failed_checks))


def combine(previous, measured, requirements):
    """Replace only the requested observations and retain failures outside the scope."""
    requested = {name.casefold() for name in requirements}
    measured_names = {verify.one_line(c["what"]).casefold() for c in measured.checks if c.get("what")}
    absent = [name for name in requirements if name.casefold() not in measured_names]
    retained = [c for c in previous.checks if verify.one_line(c.get("what", "")).casefold() not in requested]
    retained_names = {verify.one_line(c.get("what", "")).casefold() for c in retained}
    gaps = [name for name in outstanding(previous) if verify.one_line(name).casefold() not in requested]
    gaps = list(dict.fromkeys([*gaps, *measured.unmeasured_requirements, *absent]))
    retained = [{**c, "evidence_file": previous.evidence_of(c)} if previous.saved else c for c in retained]
    checks = retained + [c for c in measured.checks if not c.get("passed")
                         or verify.one_line(c.get("what", "")).casefold() not in retained_names]
    verdict = measured.verdict
    blocker = measured.blocked_by
    if any(c.get("passed") is False and c.get("cause") != "environment" for c in checks) or measured.verdict == "fail":
        verdict = "fail"
    elif absent or gaps or any(c.get("passed") is False for c in checks):
        verdict = "blocked"
        blocker = blocker or "Remaining verification requirements: " + ", ".join(gaps)
    unresolved = any(name.casefold() not in requested for name in outstanding(previous))
    return replace(measured, verdict=verdict, checks=checks, blocked_by=blocker, unmeasured_requirements=gaps,
                   missing_capability=measured.missing_capability or (previous.missing_capability if unresolved else ""),
                   missing_tools=sorted(set(measured.missing_tools) | (set(previous.missing_tools) if unresolved else set())),
                   native_trees=measured.native_trees if previous.native_trees == measured.native_trees else {},
                   advisory_gaps=[name for name in previous.advisory_gaps if name in gaps] + measured.advisory_gaps)
