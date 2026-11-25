"""Observe an enrolled staging deployment and promote its QA-approved revision."""
from __future__ import annotations

import json
import re
import secrets
from urllib.parse import quote, urlencode

from . import adf, config, gh, jira, release_proof as proof, serve_guard, tracker, util

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
    return proof.merge_revision(repo, ticket, repo.target_branch)


def observe_staging(repo, ticket, revision, *, pinned=""):
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


def authority(cfg, repo, ticket, policy, revision):
    from . import unblock

    tk = tracker.for_ticket(cfg, repo, ticket)
    proof.need(isinstance(tk, (tracker.GitHubTracker, tracker.JiraTracker)), "the release needs its enrolled tracker")
    t = tk.get(ticket)
    if t.is_spec:
        proof.need(not tracker.spec_blocker(cfg, tk, t), "spec is closed, no longer authorized, or blocked")
    else:
        proof.need(unblock.approval(tk, t)[0] and not t.blocker, "ticket is closed, no longer authorized, or blocked")
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
            if (not directive or directive[1] == revision) and (not edited or util.parse_iso(edited) == util.parse_iso(at)):
                approvals.append((util.parse_iso(at), cid))
        elif line.rstrip(".!").casefold() not in NEUTRAL:
            changes.append((who, cid, line))
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


def ready(cfg, repo, ticket, policy, state):
    revision, target = state["revision"], policy["production_branch"]
    try:
        current = config.load_required().repo(repo.name)
    except (config.UnknownRepo, ValueError, OSError) as error:
        raise proof.Held("repository enrollment is unavailable") from error
    proof.need(current == repo and current.pipeline and current.landing == "pr", "repository enrollment changed")
    _, _, scope, approvals = authority(cfg, repo, ticket, policy, revision)
    proof.need(scope == state["scope"], "scope changed after staging; deploy the corrected revision for fresh QA")
    after = util.parse_iso(state["qa_since"])
    anchor = re.search(r"(?:issuecomment-|focusedCommentId=)([0-9]+)(?:$|[&#])", state["handoff"])
    choices = [(at, cid) for at, cid in approvals
               if (cid.isdigit() and int(cid) > int(anchor[1]) if anchor else at > after)]
    proof.need(choices, "waiting for a new QA instruction for the tested revision")
    proof.need(repo.unattended_environment == "staging" and repo.target_branch != target
               and proof.digest(proof.goal(repo.name, ticket)) == state["policy"], "release policy changed")
    proof.need(branch(repo, repo.target_branch) == revision and selected_revision(repo, ticket) == revision,
               "the staging branch or goal changed; fresh QA is required")
    proof.need(parent(repo, revision) == branch(repo, target) == proof.deployed_revision(policy, policy["production"]),
               "promotion would include other unapproved changes or production identity is unconfirmed")
    proof.need(gh.merge_ci_passed(repo.github, revision, stage_checks=policy.get("checks", {}).get("staging")), "checks on the staged revision must pass before promotion")
    proof.need(proof.deployed_revision(policy, policy["staging"]) == revision
               and preview(repo, policy, revision) == state["origin"], "the tested staging deployment changed")
    return max(choices)[1]


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
        if serve_guard.read(file) != state:
            serve_guard.write(file, state)
        return state


def _advance(cfg, repo, ticket, file):
    policy = proof.goal(repo.name, ticket)
    proof.need(policy and policy["destination"] == repo.unattended_environment == "staging" and repo.target_branch
               and repo.target_branch != policy["production_branch"], "no eligible staging enrollment")
    state = serve_guard.read(file) or {}
    state.pop("waiting", None)
    if state.get("phase") == "production_verified":
        return state
    # Reconcile sends first, even when a subsequent comment, policy or branch changed.
    if state.get("phase") == "promotion_intent":
        if branch(repo, state["production_branch"]) == state["revision"]:
            state.update(phase="observing_production")
            serve_guard.write(file, state)
        elif branch(repo, state["production_branch"]) != state["parent"]:
            raise proof.Held("production moved during promotion; reconcile the release")
    if state.get("phase") == "observing_production":
        proof.need(state["policy"] == proof.digest(policy), "release policy changed during promotion")
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
        serve_guard.write(file, state)
    proof.need(state["policy"] == proof.digest(policy) and state["scope"] == scope,
               "the tested behavior changed; a corrected staging revision and fresh QA are required")
    if state["phase"] == "observing_staging":
        found = observe_staging(repo, ticket, revision, pinned=state["policy"])
        if found["status"] != "verified":
            state["waiting"] = found["reason"]
            return state
        state.update(origin=found.get("origin") or policy["staging"].rstrip("/"), staging_receipt=found["receipt_id"])
        note(tk, repo, ticket, state, file, policy, found)
        return state
    approval = ready(cfg, repo, ticket, policy, state)
    result = observe_staging(repo, ticket, revision, pinned=state["policy"])
    proof.need(result["status"] == "verified", "the staged behavior no longer passes its protected check")
    proof.need(ready(cfg, repo, ticket, policy, state) == approval, "QA authorization changed during verification")
    state.update(phase="promotion_intent", approval=approval, parent=parent(repo, revision),
                 production_branch=policy["production_branch"])
    serve_guard.write(file, state)
    # A non-forced update of a direct child cannot land unrelated work if the branch races.
    api = f"repos/{repo.github}/git/refs/heads/{quote(state['production_branch'], safe='/')}"
    gh.gh(["api", api, "--method", "PATCH", "--input", "-"],
          input=gh.Outgoing(json.dumps({"sha": revision, "force": False})), write=True, retry=False, timeout=20)
    return state
