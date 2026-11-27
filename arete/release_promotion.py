"""Observe and promote an exact staging revision under its enrolled authority."""
from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from contextlib import nullcontext
from urllib.parse import quote, urlencode

from . import adf, cohorts, config, gh, jira, release_profile, release_proof as proof, serve_guard, tracker, util
from .store import specgraphs, specs

NEUTRAL = {"thanks", "thank you", "looks good", "lgtm", "ack"}
EXPLICIT = {"promote this to production", "push this to production", "moving this to push to production on the sheet"}
APPROVE = re.compile(r"^promote ([a-f0-9]{40}(?:[a-f0-9]{24})?) to production[.!]?$", re.I)


def pages(endpoint):
    result = gh.gh_json(["api", endpoint, "--paginate", "--slurp"], timeout=20, retry=False)
    if not isinstance(result, list) or not all(isinstance(page, list) for page in result):
        raise gh.GhError("could not read deployment records")
    return [row for page in result for row in page]


def preview(repo, policy, revision):
    query = urlencode({"environment": "staging", "sha": revision, "per_page": 100})
    for deployment in pages(f"repos/{repo.github}/deployments?{query}"):
        if deployment.get("sha") != revision or deployment.get("environment") != "staging":
            continue
        statuses = pages(f"repos/{repo.github}/deployments/{int(deployment['id'])}/statuses?per_page=100")
        if statuses and statuses[0].get("state") == "success":
            url = statuses[0].get("environment_url", "").rstrip("/")
            if suffix := policy.get("immutable_host_suffix"):
                split = proof.server_url(url)
                if (split.scheme == "https" and split.port is None and split.hostname.endswith("." + suffix)
                        and url != policy["staging"].rstrip("/")):
                    return url
            elif url == policy["staging"].rstrip("/"):
                return url
    return ""


def selected_revision(repo, ticket):
    from . import spec_graph_delivery
    if (graph := spec_graph_delivery.promotion_target(repo, ticket)) is not None:
        return graph.get("commit", "")
    return proof.merge_revision(repo, ticket, repo.target_branch)


def observe_staging(repo, ticket, revision, *, pinned=""):
    release_profile.current(repo)
    policy = proof.goal(repo.name, ticket)
    proof.need(policy and policy["destination"] == repo.unattended_environment == "staging"
               and repo.pipeline and repo.landing == "pr" and repo.target_branch != policy["production_branch"],
               "no eligible staging enrollment")
    proof.need(serve_guard.SHA.fullmatch(revision or "") and (not policy.get("revision") or policy["revision"] == revision),
               "the release revision differs from the protected policy")
    origin = preview(repo, policy, revision)
    proof.need(origin, "waiting for the matching successful staging deployment")
    if checks := policy.get("checks", {}).get("staging"):
        proof.need(gh.merge_ci_passed(repo.github, revision, stage_checks=checks), "staging checks have not passed")

    def check_destination():
        proof.need(proof.deployed_revision(policy, policy["staging"]) == revision and preview(repo, policy, revision) == origin,
                   "staging or its immutable deployment changed during observation")

    return proof.observe(repo.name, ticket, revision, pinned=pinned or proof.digest(policy),
                         origin=origin if policy.get("immutable_host_suffix") else None, check_destination=check_destination)


def production_checks_passed(repo, state):
    """The staging and production pushes share a SHA, but only the production push can satisfy this gate."""
    revision, target = state["revision"], state["production_branch"]
    gate = state["production_checks"]
    query = urlencode({"head_sha": revision, "branch": target, "event": "push", "per_page": 100})
    base = f"repos/{repo.github}"
    runs = gh.actions_rows(f"{base}/actions/runs?{query}", "workflow_runs", timeout=20, retry=False)
    if not runs:
        return False
    passed = set()
    for run in runs:
        if ((run.get("head_sha"), run.get("head_branch"), run.get("event"), run.get("status"), run.get("conclusion"))
                != (revision, target, "push", "completed", "success") or type(run.get("check_suite_id")) is not int):
            return False
        suite = run["check_suite_id"]
        checks = gh.actions_rows(f"{base}/check-suites/{suite}/check-runs?filter=latest&per_page=100",
                                 "check_runs", timeout=20, retry=False)
        for check in checks:
            if (check.get("head_sha") != revision or (check.get("check_suite") or {}).get("id") != suite
                    or check.get("status") != "completed"):
                return False
            name = check.get("name")
            issuer = (check.get("app") or {}).get("id")
            if check.get("conclusion") == "success":
                if issuer == gate["app_id"]:
                    passed.add(name)
            elif check.get("conclusion") != "skipped" or issuer != gate["app_id"] or name not in gate["allow_skipped"]:
                return False
    return passed.issuperset(gate["required"])


def branch(repo, name):
    row = gh.gh_json(["api", f"repos/{repo.github}/git/ref/heads/{quote(name, safe='/')}"], timeout=15, retry=False)
    value = row.get("object") or {}
    proof.need(row.get("ref") == "refs/heads/" + name and value.get("type") == "commit"
               and serve_guard.SHA.fullmatch(value.get("sha", "")), "could not identify the enrolled branch")
    return value["sha"]


def parent(repo, revision):
    commit = gh.gh_json(["api", f"repos/{repo.github}/git/commits/{revision}"], timeout=15, retry=False)
    parents = commit.get("parents") or []
    proof.need(commit.get("sha") == revision and len(parents) == 1,
               "production requires an isolated single-parent change")
    return parents[0]["sha"]


def comments(repo, ticket, tk):
    if isinstance(tk, tracker.GitHubTracker):
        rows = pages(f"repos/{repo.github}/issues/{int(ticket)}/comments?per_page=100")
        return [((r.get("user") or {}).get("login", ""), r.get("body") or "", r.get("created_at", ""),
                 r.get("updated_at", ""), str(r.get("id", ""))) for r in rows]
    rows = tk.client.comments(ticket)
    return [((r.get("author") or {}).get("accountId", ""), adf.to_text(r["body"]) if isinstance(r["body"], dict)
             else r["body"], r.get("created", ""), r.get("updated", ""), str(r.get("id", ""))) for r in rows]


def review_comments(tk, repo, ticket, policy, revision):
    allowed = set(policy["qa"])
    notes = comments(repo, ticket, tk)
    approvals, changes = [], []
    for who, body, at, edited, cid in notes:
        # GitHub's visible bot types and Arete's own marked handoffs cannot grant release authority.
        if who not in allowed or tracker.from_arete(body) or gh.has_arete_mark(body) or not cid or not at:
            continue
        line = body.strip()
        directive = APPROVE.fullmatch(line)
        if directive or line.rstrip(".!").casefold() in EXPLICIT:
            if not directive and policy.get("qa_require_revision"):
                continue
            if (cid.isdigit() and (not directive or directive[1] == revision)
                    and (not edited or util.parse_iso(edited) == util.parse_iso(at))):
                url = (gh.issue_url(repo.github, ticket) + "#issuecomment-" + cid if isinstance(tk, tracker.GitHubTracker)
                       else tk.client.comment_url(ticket, cid))
                approvals.append({"id": cid, "at": cohorts.timestamp(at), "url": url, "actor": who})
        elif line.rstrip(".!").casefold() not in NEUTRAL:
            changes.append((who, cid, line))
    return changes, approvals


def authority(cfg, repo, ticket, policy, revision):
    from . import unblock

    tk = tracker.for_ticket(cfg, repo, ticket)
    proof.need(isinstance(tk, (tracker.GitHubTracker, tracker.JiraTracker)), "the release needs its enrolled tracker")
    t = tk.get(ticket)
    if t.is_spec:
        proof.need(not tracker.spec_blocker(cfg, tk, t), "spec is closed, no longer authorized, or blocked")
    else:
        proof.need(unblock.approval(tk, t)[0] and not t.blocker, "ticket is closed, no longer authorized, or blocked")
    changes, approvals = review_comments(tk, repo, ticket, policy, revision)
    return tk, t, proof.digest([t.spec_identity, t.title, t.body, sorted(changes)]), approvals


def note(tk, repo, ticket, state, state_file, policy, proof_row):
    # Only a handoff actually sent by this service can anchor a tester's subsequent direction.
    marker = state.setdefault("note_marker", "arete-qa-" + secrets.token_hex(16))
    found = ""
    if state.get("note_intent"):
        actor = gh.me() if isinstance(tk, tracker.GitHubTracker) else tk.client.me().get("accountId", "")
        proof.need(actor, "cannot confirm the staging handoff publisher")
        for who, body, _, _, cid in comments(repo, ticket, tk):
            if who == actor and marker in body.splitlines() and cid.isdigit():
                found = (gh.issue_url(repo.github, ticket) + "#issuecomment-" + cid if isinstance(tk, tracker.GitHubTracker)
                         else tk.client.comment_url(ticket, cid))
                break
        proof.need(found, "staging handoff awaiting tracker reconciliation")
    if not found:
        state["note_intent"] = True
        serve_guard.write(state_file, state)
        body = gh.marked(f"Staging verified at {state['origin']} for {state['revision']}.\n\n"
                         f"QA: {policy['instructions']}\n\nVerified fields: "
                         + ", ".join(f['field'] for f in proof_row["observed"])
                         + f". Receipt {proof_row['receipt_id']}.\n\n"
                         f"An authorized tester may reply: Promote {state['revision']} to production\n\n{marker}")
        try:
            if isinstance(tk, tracker.GitHubTracker):
                found = gh.issue_comment(repo.github, int(ticket), body, return_url=True, single_attempt=True)
            else:
                found = tk.client.comment(ticket, body, return_url=True, single_attempt=True)
        except (gh.GhUnsure, jira.JiraUnsure):
            raise
        except (gh.GhError, jira.JiraError):
            state.pop("note_intent")
            serve_guard.write(state_file, state)
            raise
    prefix = (gh.issue_url(repo.github, ticket) + "#issuecomment-" if isinstance(tk, tracker.GitHubTracker)
              else tk.client.browse_url(ticket) + "?focusedCommentId=")
    proof.need(isinstance(found, str) and found.startswith(prefix) and found[len(prefix):].isdigit(),
               "the tracker did not identify its QA handoff")
    state.update(phase="awaiting_qa", handoff=found, qa_since=util.iso())
    serve_guard.write(state_file, state)


def verified_graph_revisions(repo, graph_key):
    """The exact child revisions that passed this native graph's protected staging checks."""
    revisions = set()
    for child, _ in specgraphs.children(graph_key).values():
        scope, ticket = specgraphs.destination(graph_key.scope, child)
        if scope != graph_key.scope:
            continue
        state = serve_guard.read(proof.state_dir(repo.name, ticket) / "promotion.json") or {}
        if state.get("phase") != "staged_for_parent" or state.get("graph_parent") != graph_key.parent:
            continue
        revision = state["revision"]
        receipt = proof.evidence(repo.name, ticket, revision)
        if (receipt and receipt.get("revision") == revision and receipt.get("destination") == "staging"
                and receipt["receipt_id"] == state["staging_receipt"]):
            revisions.add(revision)
    return revisions


def promotion_range(repo, ticket, revision, production, *, require_staging=False):
    from . import spec_graph_delivery
    if (graph := spec_graph_delivery.promotion_target(repo, ticket)) is None:
        return parent(repo, revision), []
    proof.need(graph.get("commit") == revision, "the verified graph release changed")
    approved = set(graph["children"])
    members = []
    cursor = revision
    while cursor != production:
        proof.need(cursor in approved, "the staged range contains a change outside the verified spec graph")
        approved.remove(cursor)
        members.append(cursor)
        cursor = parent(repo, cursor)
    if require_staging:
        graph_key = specgraphs.GraphKey("github:" + repo.github.casefold(), str(ticket))
        proof.need(set(members) <= verified_graph_revisions(repo, graph_key),
                   "waiting for each graph child to verify its exact staged revision")
    return production, members


def graph_parent(tk, ticket):
    from . import spec_tracker

    remote = spec_tracker.adapter(tk)
    key = specgraphs.membership(remote.scope, ticket) if remote else None
    if key:
        proof.need(key.scope == remote.scope, "the child and its graph have different repository enrollments")
        return key.parent
    return ""


def after_handoff(state, approvals):
    anchor = re.search(r"(?:issuecomment-|focusedCommentId=)([0-9]+)(?:$|[&#])", state["handoff"])
    if anchor:
        return [row for row in approvals if int(row["id"]) > int(anchor[1])]
    return [row for row in approvals if util.parse_iso(row["at"]) > util.parse_iso(state["qa_since"])]


def ready(cfg, repo, ticket, policy, state):
    revision, target = state["revision"], policy["production_branch"]
    try:
        current = config.load_required().repo(repo.name)
    except (config.UnknownRepo, ValueError, OSError) as error:
        raise proof.Held("repository enrollment is unavailable") from error
    proof.need(current == repo and current.pipeline and current.landing == "pr", "repository enrollment changed")
    _, _, scope, approvals = authority(cfg, repo, ticket, policy, revision)
    proof.need(scope == state["scope"], "scope changed after staging; deploy the corrected revision for fresh QA")
    approval = None
    if state.get("standing_authority"):
        require_standing(repo, ticket, state)
    else:
        choices = after_handoff(state, approvals)
        proof.need(choices, "waiting for a new QA instruction for the tested revision")
        approval = min(choices, key=lambda row: (row["at"], int(row["id"])))
    proof.need(repo.unattended_environment == "staging" and repo.target_branch != target
               and proof.digest(proof.goal(repo.name, ticket)) == state["policy"], "release policy changed")
    proof.need(branch(repo, repo.target_branch) == revision and selected_revision(repo, ticket) == revision,
               "the staging branch or goal changed; fresh QA is required")
    production = branch(repo, target)
    base, _ = promotion_range(repo, ticket, revision, production, require_staging=bool(state.get("standing_authority")))
    proof.need(base == production == proof.deployed_revision(policy, policy["production"]),
               "promotion would include other unapproved changes or production identity is unconfirmed")
    proof.need(gh.merge_ci_passed(repo.github, revision, stage_checks=policy.get("checks", {}).get("staging")), "checks on the staged revision must pass before promotion")
    proof.need(proof.deployed_revision(policy, policy["staging"]) == revision
               and preview(repo, policy, revision) == state["origin"], "the tested staging deployment changed")
    return approval


def require_standing(repo, ticket, state):
    grant = release_profile.standing(repo, ticket)
    proof.need(grant and grant["id"] == state["standing_authority"], "standing production authorization changed")


def score_approval(cfg, repo, ticket, state):
    cid = state.get("approval")
    if not cid or state.get("approval_scored") == cid or not cohorts.location().is_file():
        return
    receipt = state.get("approval_comment")
    if not receipt:
        # Earlier promotion receipts have only an ID. A closed ticket still has its review history.
        policy = proof.goal(repo.name, ticket)
        tk = tracker.for_ticket(cfg, repo, ticket)
        if (not isinstance(tk, (tracker.GitHubTracker, tracker.JiraTracker)) or not policy
                or proof.digest(policy) != state.get("policy") or not state.get("handoff") or not state.get("qa_since")):
            return
        _, approvals = review_comments(tk, repo, ticket, policy, state["revision"])
        receipt = next((r for r in after_handoff(state, approvals) if r["id"] == cid), None)
        if not receipt:
            return
        state["approval_comment"] = receipt
    if cohorts.record_production_qa(repo.name, ticket, state["revision"], receipt):
        state["approval_scored"] = cid


def advance(cfg, repo, ticket):
    file = proof.state_dir(repo.name, ticket) / "promotion.json"
    with util.lock("promote-" + proof.key(repo.name, ticket)) as acquired:
        if not acquired:
            return {"phase": "in_progress"}
        try:
            state = _advance(cfg, repo, ticket, file)
        except (proof.Held, OSError, gh.GhError, jira.JiraError) as error:
            state = serve_guard.read(file) or {"phase": "waiting"}
            # Only controlled hold reasons may enter a release receipt.
            state["waiting"] = str(error) if isinstance(error, proof.Held) else type(error).__name__
        try:
            score_approval(cfg, repo, ticket, state)
        except (OSError, ValueError, sqlite3.Error, gh.GhError, jira.JiraError):
            logging.getLogger(__name__).exception("Could not reconcile production QA accounting for %s/%s", repo.name, ticket)
        if serve_guard.read(file) != state:
            serve_guard.write(file, state)
        return state


def _advance(cfg, repo, ticket, file):
    release_profile.current(repo)
    policy = proof.goal(repo.name, ticket)
    proof.need(policy and policy["destination"] == repo.unattended_environment == "staging" and repo.target_branch
               and repo.target_branch != policy["production_branch"], "no eligible staging enrollment")
    state = serve_guard.read(file) or {}
    state.pop("waiting", None)
    if state.get("phase") in ("production_verified", "staging_only"):
        return state
    # Reconcile sends first, even when a subsequent comment, policy or branch changed.
    if state.get("phase") == "promotion_intent":
        if branch(repo, state["production_branch"]) == state["revision"]:
            state.update(phase="observing_production")
            serve_guard.write(file, state)
        elif branch(repo, state["production_branch"]) != state["parent"]:
            raise proof.Held("production moved during promotion; reconcile the release")
    if state.get("phase") == "staged_for_parent":
        tk, _, scope, _ = authority(cfg, repo, ticket, policy, state["revision"])
        proof.need(scope == state["scope"] and graph_parent(tk, ticket) == state["graph_parent"],
                   "the staged graph child changed")
        parent_state = serve_guard.read(proof.state_dir(repo.name, state["graph_parent"]) / "promotion.json") or {}
        integrated = parent_state.get("revision", "")
        proof.need(state["revision"] in parent_state.get("graph_revisions", []) and
                   proof.completion(repo.name, state["graph_parent"], integrated),
                   "waiting for the parent to verify the integrated production revision")
        state.update(phase="observing_production", staged_revision=state["revision"],
                     revision=integrated, production_branch=policy["production_branch"])
        serve_guard.write(file, state)
    if state.get("phase") == "observing_production":
        proof.need(state["policy"] == proof.digest(policy), "release policy changed during promotion")
        if state.get("standing_authority"):
            proof.need(branch(repo, state["production_branch"]) == state["revision"]
                       and production_checks_passed(repo, state), "waiting for the exact production push and its required checks")
        found = proof.observe(repo.name, ticket, state["revision"], destination="production", pinned=state["policy"])
        if found["status"] == "verified":
            state.update(phase="production_verified", production_receipt=found["receipt_id"])
            serve_guard.write(file, state)
        else:
            state["waiting"] = found["reason"]
        return state
    revision = selected_revision(repo, ticket)
    proof.need(revision and serve_guard.SHA.fullmatch(revision), "waiting for the enrolled ticket's merged staging PR")
    proof.need(not policy.get("revision") or policy["revision"] == revision, "goal release differs from the pinned revision")
    tk, _, scope, _ = authority(cfg, repo, ticket, policy, revision)
    if state.get("revision") != revision:
        proof.need(state.get("phase") != "promotion_intent", "an earlier promotion needs reconciliation")
        state = {"revision": revision, "policy": proof.digest(policy), "scope": scope, "phase": "observing_staging"}
        if grant := release_profile.standing(repo, ticket):
            state.update(standing_authority=grant["id"], production_checks=grant["production_checks"])
        serve_guard.write(file, state)
    proof.need(state["policy"] == proof.digest(policy) and state["scope"] == scope,
               "the tested behavior changed; a corrected staging revision and fresh QA are required")
    if state["phase"] == "observing_staging":
        found = observe_staging(repo, ticket, revision, pinned=state["policy"])
        if found["status"] != "verified":
            state["waiting"] = found["reason"]
            return state
        state.update(origin=found.get("origin") or policy["staging"].rstrip("/"), staging_receipt=found["receipt_id"])
        if not state.get("standing_authority"):
            note(tk, repo, ticket, state, file, policy, found)
            return state
        claim = specs.find(repo.name, ticket)
        if claim.get("data", {}).get("definition", {}).get("environment") == "staging":
            state["phase"] = "staging_only"
            serve_guard.write(file, state)
            return state
        if parent_ticket := graph_parent(tk, ticket):
            state.update(phase="staged_for_parent", graph_parent=parent_ticket)
            serve_guard.write(file, state)
            return state
        state["phase"] = "staging_verified"
        serve_guard.write(file, state)
    approval = ready(cfg, repo, ticket, policy, state)
    result = observe_staging(repo, ticket, revision, pinned=state["policy"])
    proof.need(result["status"] == "verified", "the staged behavior no longer passes its protected check")
    proof.need(ready(cfg, repo, ticket, policy, state) == approval, "QA authorization changed during verification")
    if approval:
        state.update(approval=approval["id"], approval_comment=approval)
    base, graph_revisions = promotion_range(repo, ticket, revision, branch(repo, policy["production_branch"]),
                                           require_staging=bool(state.get("standing_authority")))
    if graph_revisions:
        state["graph_revisions"] = graph_revisions
    state.update(phase="promotion_intent", parent=base, production_branch=policy["production_branch"])
    serve_guard.write(file, state)
    # A non-forced update of a direct child cannot land unrelated work if the branch races.
    api = f"repos/{repo.github}/git/refs/heads/{quote(state['production_branch'], safe='/')}"
    with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
        if state.get("standing_authority"):
            require_standing(repo, ticket, state)
        gh.gh(["api", api, "--method", "PATCH", "--input", "-"],
              input=gh.Outgoing(json.dumps({"sha": revision, "force": False})), write=True, retry=False, timeout=20)
    return state
