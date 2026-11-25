"""Keep a standing production grant's staging branch available to its current delivery."""
from __future__ import annotations

from . import config, release_profile, release_promotion as promotion, release_proof as proof, serve_guard
from .store import specgraphs


def sibling_range(repo, ticket, tip, production):
    """Only verified children of the same native graph may share an in-flight staging lane."""
    scope = "github:" + repo.github.casefold()
    graph = specgraphs.membership(scope, ticket)
    if not graph or graph.scope != scope:
        return False
    siblings = promotion.verified_graph_revisions(repo, graph)
    while tip != production:
        if tip not in siblings:
            return False
        siblings.remove(tip)
        tip = promotion.parent(repo, tip)
    return True


def merge_hold(repo, ticket, base):
    # Both merge entry points call this under the existing per-repository merge lease.
    if not isinstance(repo, config.Repo) or not release_profile.exists(repo.name):
        return ""
    profile = release_profile.current(repo)
    grant = release_profile.standing_status(repo, profile)
    states = {previous: serve_guard.read(proof.state_dir(repo.name, previous) / "promotion.json") or {}
              for previous in proof.enrolled(repo.name)}
    in_progress = any(state.get("standing_authority") and state.get("phase") not in ("production_verified", "staging_only")
                      for state in states.values())
    if not (grant and grant["active"]) and not in_progress:
        return ""
    policy = profile["settings"]
    production = promotion.branch(repo, policy["production_branch"])
    if proof.deployed_revision(policy, policy["production"]) != production:
        return "Waiting for the current production revision to deploy before landing another issue"
    if base != production:
        if sibling_range(repo, str(ticket), base, production):
            return ""
        return "Waiting for the previous staged issue to reach verified production"
    for previous, state in states.items():
        if state.get("phase") == "staged_for_parent":
            parent = serve_guard.read(proof.state_dir(repo.name, str(state["graph_parent"])) / "promotion.json") or {}
            if parent.get("revision") == base:
                return "Waiting for the integrated production observation of the previous spec graph"
            continue
        if state.get("revision") != base:
            continue
        if not proof.completion(repo.name, previous, base):
            return "Waiting for the protected production observation of the previous release"
    return ""
