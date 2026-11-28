"""Observe and promote an exact staging revision under its enrolled authority."""
from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import tempfile
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import quote, urlencode

from . import adf, cohorts, config, gh, jira, paths, release_profile, release_proof as proof, serve_guard, tracker, util
from .store import specgraphs, specs

NEUTRAL = {"thanks", "thank you", "looks good", "lgtm", "ack"}
EXPLICIT = {"promote this to production", "push this to production", "moving this to push to production on the sheet"}
APPROVE = re.compile(r"^promote ([a-f0-9]{40}(?:[a-f0-9]{24})?) to production[.!]?$", re.I)
LIVE_DEPENDENCY = re.compile(r"\b(?:live|deployed|deployment|in production|production)\b", re.I)


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
    proof.need(isinstance(revision, str) and serve_guard.SHA.fullmatch(revision),
               "production requires a valid commit SHA")
    commit = gh.gh_json(["api", f"repos/{gh._safe_repo(repo.github)}/git/commits/{revision}"], timeout=15, retry=False)
    if not isinstance(commit, dict):
        raise proof.Held("could not read commit metadata")
    parents = commit.get("parents") or []
    proof.need(commit.get("sha") == revision and len(parents) == 1 and isinstance(parents[0], dict)
               and serve_guard.SHA.fullmatch(str(parents[0].get("sha") or "")),
               "production requires an isolated single-parent change")
    return parents[0]["sha"]


def _comment_body_text(raw) -> str:
    if isinstance(raw, dict):
        try:
            return adf.to_text(raw)
        except ValueError:
            return ""
    return raw if isinstance(raw, str) else ""


def comments(repo, ticket, tk):
    if isinstance(tk, tracker.GitHubTracker):
        rows = pages(f"repos/{gh._safe_repo(repo.github)}/issues/{int(ticket)}/comments?per_page=100")
        return [((r.get("user") or {}).get("login", ""), _comment_body_text(r.get("body")),
                 r.get("created_at", ""), r.get("updated_at", ""), str(r.get("id", "")))
                for r in rows if isinstance(r, dict)]
    rows = tk.client.comments(ticket)
    return [((r.get("author") or {}).get("accountId", ""), _comment_body_text(r.get("body")),
             r.get("created", ""), r.get("updated", ""), str(r.get("id", "")))
            for r in rows if isinstance(r, dict)]


def review_comments(tk, repo, ticket, policy, revision):
    from . import ticket_feedback

    allowed = set(policy["qa"])
    notes = comments(repo, ticket, tk)
    approvals, changes = [], []
    for who, body, at, edited, cid in notes:
        # GitHub's visible bot types and Arete's own marked handoffs cannot grant release authority.
        if who not in allowed or ticket_feedback.is_arete_comment(body) or not cid or not at:
            continue
        line = body.strip()
        directive = APPROVE.fullmatch(line)
        matched_prod, prod_rev = ticket_feedback.match_production_instruction(line)
        explicit_sha = directive[1] if directive else prod_rev
        if directive or matched_prod or line.rstrip(".!").casefold() in EXPLICIT:
            if not explicit_sha and policy.get("qa_require_revision"):
                continue
            if (cid.isascii() and cid.isdigit() and (not explicit_sha or explicit_sha.lower() == revision.lower())
                    and (not edited or util.parse_iso(edited) == util.parse_iso(at))):
                url = (gh.issue_url(repo.github, ticket) + "#issuecomment-" + cid if isinstance(tk, tracker.GitHubTracker)
                       else tk.client.comment_url(ticket, cid))
                approvals.append({"id": cid, "at": cohorts.timestamp(at), "url": url, "actor": who})
        elif line.rstrip(".!").casefold() not in NEUTRAL and not ticket_feedback.is_neutral_acknowledgment(line):
            changes.append((who, cid, line))
    return changes, approvals


def promotion_mode(policy):
    return (policy or {}).get("promotion_mode") or "single"


def commit_range(repo, start, stop, *, limit=200):
    """Return linear commits in stop..start from oldest to newest, or None if stop is not reached."""
    if start == stop:
        return []
    commits, seen = [], set()
    cursor = start
    while cursor != stop:
        if cursor in seen or len(commits) >= limit:
            return None
        seen.add(cursor)
        commits.append(cursor)
        try:
            cursor = parent(repo, cursor)
        except (proof.Held, gh.GhError):
            return None
    commits.reverse()
    return commits


def on_branch(repo, tip, revision, *, limit=200):
    """Check whether revision equals tip or is a linear ancestor of tip."""
    if tip == revision:
        return True
    seen = set()
    cursor = tip
    for _ in range(limit):
        if cursor in seen:
            return False
        seen.add(cursor)
        try:
            cursor = parent(repo, cursor)
        except (proof.Held, gh.GhError):
            return False
        if cursor == revision:
            return True
    return False


def ticket_dependencies(cfg, repo, tk, ticket, t):
    """Collect same-repo and external prerequisites declared via spec graph, native blockers, or Waits on."""
    from . import spec_tracker

    found = {}

    def add(dep_id, *, same_repo, require_live, source):
        dep_id = str(dep_id)
        if same_repo and dep_id == str(ticket):
            return
        key = (same_repo, dep_id)
        if key in found:
            found[key]["require_live"] = found[key]["require_live"] or require_live
        else:
            found[key] = {"ticket": dep_id, "same_repo": same_repo, "require_live": require_live, "source": source}

    remote = spec_tracker.adapter(tk)
    if remote and (graph_key := specgraphs.membership(remote.scope, str(ticket))) and graph_key.scope == remote.scope:
        if saved := specgraphs.get(graph_key, repo.name):
            mapped = specgraphs.children(graph_key)
            by_ref = {ref: name for name, (ref, _) in mapped.items()}
            if name := by_ref.get(str(ticket)):
                node = next((n for n in saved["data"]["plan"]["children"] if n["key"] == name), {})
                live_deps = set(node.get("release_requires") or ()) | set(node.get("artifacts") or ())
                for dep_name in node.get("requires") or ():
                    if dep_name in mapped:
                        add(mapped[dep_name][0], same_repo=True, require_live=dep_name in live_deps, source="spec-graph")

    own = repo.github.casefold()
    for blk in getattr(t, "github_blockers", ()) or ():
        if isinstance(blk, int):
            add(str(blk), same_repo=True, require_live=False, source="github-blocker")
        elif isinstance(blk, str) and "#" in blk:
            blk_repo, blk_num = blk.rsplit("#", 1)
            if blk_repo.casefold() == own and blk_num.isdigit():
                add(blk_num, same_repo=True, require_live=False, source="github-blocker")
            else:
                add(blk, same_repo=False, require_live=False, source="github-blocker")

    for line in tracker.WAIT_LINE.findall(t.body or ""):
        live = bool(LIVE_DEPENDENCY.search(line))
        if isinstance(tk, tracker.GitHubTracker):
            for w_repo, w_num in tracker.github_waits(f"Waits on: {line}", own):
                if w_repo == own:
                    add(str(w_num), same_repo=True, require_live=live, source="waits-on")
                else:
                    add(f"{w_repo}#{w_num}", same_repo=False, require_live=live, source="waits-on")
        elif isinstance(tk, tracker.JiraTracker):
            for key in tracker.waits_on_jira(f"Waits on: {line}"):
                add(key, same_repo=True, require_live=live, source="waits-on")

    return list(found.values())


def staged_prerequisites_only(cfg, repo, tk, ticket, t):
    if not t.blocker:
        return True
    if not t.blocker.startswith("it waits on "):
        return False
    deps = ticket_dependencies(cfg, repo, tk, ticket, t)
    if not deps or not all(dep["same_repo"] for dep in deps):
        return False
    for dep in deps:
        dep_policy = proof.goal(repo.name, dep["ticket"])
        if not dep_policy or dep_policy["destination"] != "staging":
            return False
    return True


def authority(cfg, repo, ticket, policy, revision):
    from . import unblock

    tk = tracker.for_ticket(cfg, repo, ticket)
    proof.need(isinstance(tk, (tracker.GitHubTracker, tracker.JiraTracker)), "the release needs its enrolled tracker")
    t = tk.get(ticket)
    if t.is_spec:
        spec_block = tracker.spec_blocker(cfg, tk, t)
        if spec_block and not (spec_block == t.blocker and staged_prerequisites_only(cfg, repo, tk, ticket, t)):
            raise proof.Held("spec is closed, no longer authorized, or blocked")
    else:
        proof.need(unblock.approval(tk, t)[0] and staged_prerequisites_only(cfg, repo, tk, ticket, t),
                   "ticket is closed, no longer authorized, or blocked")
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


def ticket_approval(cfg, repo, ticket, policy, state):
    from . import ticket_feedback

    staged_rev = state.get("staged_revision") or state["revision"]
    tk, t, scope, approvals = authority(cfg, repo, ticket, policy, staged_rev)
    proof.need(not ticket_feedback.pending_corrections(repo.name, ticket),
               "pending authorized feedback requires a corrected staging revision and fresh QA")
    proof.need(scope == state["scope"], "scope changed after staging; deploy the corrected revision for fresh QA")
    approval = None
    if state.get("standing_authority"):
        require_standing(repo, ticket, state)
    else:
        proof.need(state.get("handoff"), "waiting for a QA handoff on the tested revision")
        choices = after_handoff(state, approvals)
        proof.need(choices, "waiting for a new QA instruction for the tested revision")
        approval = min(choices, key=lambda row: (row["at"], int(row["id"])))
    return tk, t, approval


def is_ticket_approved(cfg, repo, ticket, policy, state):
    if not state or state.get("phase") not in ("awaiting_qa", "staging_verified", "staged_for_parent",
                                                "promotion_intent", "observing_production", "production_verified"):
        return False, None, "waiting for staging verification"
    try:
        _, _, approval = ticket_approval(cfg, repo, ticket, policy, state)
    except (proof.Held, gh.GhError, jira.JiraError) as error:
        return False, None, str(error)
    return True, approval, ""


def is_production_verified(repo, dep_ticket):
    dep_state = serve_guard.read(proof.state_dir(repo.name, dep_ticket) / "promotion.json") or {}
    if dep_state.get("phase") != "production_verified" or not dep_state.get("revision"):
        return False
    return bool(proof.completion(repo.name, dep_ticket, dep_state["revision"]))


def dependency_hold(cfg, repo, tk, ticket, t, policy, *, range_commits=None, range_tickets=None, approved_tickets=None):
    mode = promotion_mode(policy)
    for dep in ticket_dependencies(cfg, repo, tk, ticket, t):
        dep_tid = dep["ticket"]
        if not dep["same_repo"]:
            is_open = tk.wait_is_open(dep_tid) if isinstance(tk, tracker.GitHubTracker) else tk.is_open(dep_tid)
            if is_open is not False:
                return f"waiting for external prerequisite {dep_tid} to complete"
            continue
        dep_policy = proof.goal(repo.name, dep_tid)
        if not dep_policy:
            is_open = tk.is_open(int(dep_tid)) if isinstance(tk, tracker.GitHubTracker) else tk.is_open(dep_tid)
            if is_open is not False:
                return f"waiting for prerequisite {dep_tid} to close"
            continue
        if dep_policy["destination"] != "staging":
            dep_rev = dep_policy.get("revision") or proof.merge_revision(repo, dep_tid, repo.target_branch)
            if not (dep_rev and proof.completion(repo.name, dep_tid, dep_rev)):
                return f"waiting for prerequisite {dep_tid} to be deployed and verified"
            continue
        if is_production_verified(repo, dep_tid):
            continue
        dep_state = serve_guard.read(proof.state_dir(repo.name, dep_tid) / "promotion.json") or {}
        if approved_tickets is not None and dep_tid in approved_tickets:
            dep_approved = True
        else:
            dep_approved, _, _ = is_ticket_approved(cfg, repo, dep_tid, dep_policy, dep_state)
        must_be_live = dep["require_live"] or mode != "whole-branch"
        if not dep_approved:
            return f"waiting for prerequisite {dep_tid} to be approved and deployed to production"
        if must_be_live:
            return f"waiting for prerequisite {dep_tid} to be deployed and verified in production"
        if not (range_tickets and dep_tid in range_tickets):
            return f"waiting for prerequisite {dep_tid} to be included in the release or deployed to production"
    return ""


def staging_receipt_valid(repo, ticket, revision, state):
    receipt = proof.evidence(repo.name, ticket, revision)
    return bool(receipt and receipt.get("revision") == revision and receipt.get("destination") == "staging"
                and receipt.get("receipt_id") == state.get("staging_receipt")
                and receipt.get("policy") == state.get("policy"))


def whole_branch_plan(cfg, repo, ticket, tk, t, policy, state, production, staging_tip):
    staged_rev = state.get("staged_revision") or state["revision"]
    commits = commit_range(repo, staging_tip, production)
    proof.need(commits, "promotion would include other unapproved changes or production identity is unconfirmed")
    proof.need(staged_rev in commits, "the staging branch or goal changed; fresh QA is required")
    rev_to_ticket = {}
    for other in proof.enrolled(repo.name):
        other_policy = proof.goal(repo.name, other)
        if not other_policy or other_policy["destination"] != "staging":
            continue
        if (other_rev := selected_revision(repo, other)) in commits:
            rev_to_ticket[other_rev] = other
    rev_to_ticket[staged_rev] = str(ticket)
    unknown = [sha for sha in commits if sha not in rev_to_ticket]
    proof.need(not unknown,
               f"promotion would include unapproved commits without enrolled tickets: {', '.join(s[:12] for s in unknown)}")

    # If a later ticket in the range requires this prerequisite live first, allow this direct child of production to ship first.
    if commits[0] == staged_rev and len(commits) > 1:
        for later_sha in commits[1:]:
            later_tid = rev_to_ticket[later_sha]
            later_policy = proof.goal(repo.name, later_tid)
            later_tk = tracker.for_ticket(cfg, repo, later_tid)
            if isinstance(later_tk, (tracker.GitHubTracker, tracker.JiraTracker)) and later_policy:
                later_ticket = later_tk.get(later_tid)
                later_deps = ticket_dependencies(cfg, repo, later_tk, later_tid, later_ticket)
                if any(d["same_repo"] and d["ticket"] == str(ticket) and d["require_live"] for d in later_deps):
                    hold = dependency_hold(cfg, repo, tk, ticket, t, policy,
                                           range_commits=[staged_rev], range_tickets={str(ticket)},
                                           approved_tickets={str(ticket)})
                    proof.need(not hold, hold)
                    return {"base": production, "target": staged_rev, "revisions": [staged_rev],
                            "tickets": [str(ticket)], "isolated": False, "graph_revisions": [],
                            "reobserve_staging": staging_tip == staged_rev or bool(policy.get("immutable_host_suffix"))}

    included_tickets = [rev_to_ticket[sha] for sha in commits]
    unapproved = []
    ticket_details = {}
    for sha, tid in zip(commits, included_tickets, strict=True):
        t_policy = proof.goal(repo.name, tid)
        t_state = serve_guard.read(proof.state_dir(repo.name, tid) / "promotion.json") or {}
        if not t_policy or not staging_receipt_valid(repo, tid, sha, t_state):
            unapproved.append(tid)
            continue
        ok, _, _ = is_ticket_approved(cfg, repo, tid, t_policy, t_state)
        if not ok:
            unapproved.append(tid)
            continue
        t_tk = tracker.for_ticket(cfg, repo, tid)
        ticket_details[tid] = (t_tk, t_tk.get(tid), t_policy)
    proof.need(not unapproved,
               f"waiting for approval on all changes in {policy['production_branch']}..{repo.target_branch}; "
               f"unapproved tickets: {', '.join(unapproved)}")

    range_set = set(included_tickets)
    for tid in included_tickets:
        t_tk, t_obj, t_policy = ticket_details[tid]
        hold = dependency_hold(cfg, repo, t_tk, tid, t_obj, t_policy,
                               range_commits=commits, range_tickets=range_set, approved_tickets=range_set)
        proof.need(not hold, hold)
    proof.need(gh.merge_ci_passed(repo.github, staging_tip, stage_checks=policy.get("checks", {}).get("staging")),
               "checks on the staged revision must pass before promotion")
    proof.need(proof.deployed_revision(policy, policy["staging"]) == staging_tip,
               "the tested staging deployment changed")
    return {"base": production, "target": staging_tip, "revisions": commits, "tickets": included_tickets,
            "isolated": False, "graph_revisions": [],
            "reobserve_staging": staging_tip == staged_rev or bool(policy.get("immutable_host_suffix"))}


def promotion_plan(cfg, repo, ticket, policy, state):
    staged_rev = state.get("staged_revision") or state["revision"]
    target = policy["production_branch"]
    try:
        current = config.load_required().repo(repo.name)
    except (config.UnknownRepo, ValueError, OSError) as error:
        raise proof.Held("repository enrollment is unavailable") from error
    proof.need(current == repo and current.pipeline and current.landing == "pr", "repository enrollment changed")
    tk, t, approval = ticket_approval(cfg, repo, ticket, policy, state)
    proof.need(repo.unattended_environment == "staging" and repo.target_branch != target
               and proof.digest(proof.goal(repo.name, ticket)) == state["policy"], "release policy changed")
    proof.need(selected_revision(repo, ticket) == staged_rev,
               "the staging branch or goal changed; fresh QA is required")
    staging_tip = branch(repo, repo.target_branch)
    production = branch(repo, target)
    proof.need(gh.merge_ci_passed(repo.github, staged_rev, stage_checks=policy.get("checks", {}).get("staging")),
               "checks on the staged revision must pass before promotion")

    mode = promotion_mode(policy)
    if mode == "per-change":
        proof.need(on_branch(repo, staging_tip, staged_rev),
                   "the staging branch or goal changed; fresh QA is required")
        proof.need(staging_receipt_valid(repo, ticket, staged_rev, state),
                   "the tested staging receipt is missing or no longer matches")
        reobserve = staging_tip == staged_rev or bool(policy.get("immutable_host_suffix"))
        if reobserve:
            if staging_tip == staged_rev:
                proof.need(proof.deployed_revision(policy, policy["staging"]) == staged_rev,
                           "the tested staging deployment changed")
            proof.need(preview(repo, policy, staged_rev) == state["origin"], "the tested staging deployment changed")
        hold = dependency_hold(cfg, repo, tk, ticket, t, policy)
        proof.need(not hold, hold)
        proof.need(production == proof.deployed_revision(policy, policy["production"]),
                   "promotion would include other unapproved changes or production identity is unconfirmed")
        rev_parent = parent(repo, staged_rev)
        isolated = rev_parent != production
        return approval, {"base": production, "target": "" if isolated else staged_rev, "revisions": [staged_rev],
                          "tickets": [str(ticket)], "isolated": isolated, "graph_revisions": [],
                          "reobserve_staging": reobserve}

    if mode == "whole-branch":
        proof.need(staging_receipt_valid(repo, ticket, staged_rev, state),
                   "the tested staging receipt is missing or no longer matches")
        proof.need(production == proof.deployed_revision(policy, policy["production"]),
                   "promotion would include other unapproved changes or production identity is unconfirmed")
        return approval, whole_branch_plan(cfg, repo, ticket, tk, t, policy, state, production, staging_tip)

    proof.need(staging_tip == staged_rev, "the staging branch or goal changed; fresh QA is required")
    hold = dependency_hold(cfg, repo, tk, ticket, t, policy)
    proof.need(not hold, hold)
    base, graph_revisions = promotion_range(repo, ticket, staged_rev, production,
                                            require_staging=bool(state.get("standing_authority")))
    proof.need(base == production == proof.deployed_revision(policy, policy["production"]),
               "promotion would include other unapproved changes or production identity is unconfirmed")
    proof.need(proof.deployed_revision(policy, policy["staging"]) == staged_rev
               and preview(repo, policy, staged_rev) == state["origin"], "the tested staging deployment changed")
    return approval, {"base": production, "target": staged_rev, "revisions": graph_revisions or [staged_rev],
                      "tickets": [str(ticket)], "isolated": False, "graph_revisions": graph_revisions,
                      "reobserve_staging": True}


def ready(cfg, repo, ticket, policy, state):
    approval, _ = promotion_plan(cfg, repo, ticket, policy, state)
    return approval


def peer_promotion(repo, ticket, state, policy):
    staged_rev = state.get("staged_revision") or state.get("revision")
    if not staged_rev:
        return None
    for other in proof.enrolled(repo.name):
        if other == str(ticket):
            continue
        other_state = serve_guard.read(proof.state_dir(repo.name, other) / "promotion.json") or {}
        if (other_state.get("phase") in ("promotion_intent", "observing_production", "production_verified")
                and str(ticket) in (other_state.get("promoted_tickets") or [])
                and staged_rev in (other_state.get("promoted_revisions") or [])
                and other_state.get("production_branch") == policy["production_branch"]
                and serve_guard.SHA.fullmatch(other_state.get("revision", ""))):
            return other_state
    return None


def isolate_revision(repo, revision, production, production_branch, *, on_prepared=None):
    """Cherry-pick a single staged commit onto production in an isolated worktree and push non-force."""
    proof.need(serve_guard.SHA.fullmatch(revision or "") and serve_guard.SHA.fullmatch(production or "")
               and config.valid_target_branch(production_branch),
               "per-change isolation requires validated commit SHAs and branch names")
    proof.need(repo.path.is_dir() and (repo.path / ".git").exists(),
               "per-change isolation requires a local repository checkout")
    with util.repo_lock(repo.path):
        if "origin" in util.git(["remote"], repo.path, check=False).split():
            util.git(["fetch", "-q", "origin", production_branch, repo.target_branch], repo.path, check=False)
        with tempfile.TemporaryDirectory(prefix="arete-promote-", dir=paths.STATE_DIR) as tmp:
            wt = Path(tmp) / "wt"
            util.git(["worktree", "add", "-q", "--detach", str(wt), production], repo.path)
            try:
                try:
                    util.git_as_owner(["cherry-pick", "-x", revision], wt)
                except RuntimeError as error:
                    util.git(["cherry-pick", "--abort"], wt, check=False)
                    raise proof.Held(
                        f"change {revision[:12]} cannot be isolated cleanly onto {production_branch}"
                    ) from error
                isolated = util.git(["rev-parse", "HEAD"], wt).strip()
                proof.need(serve_guard.SHA.fullmatch(isolated) and isolated != production,
                           "isolated promotion produced no new commit")
                parents = util.git(["rev-list", "--parents", "-n", "1", isolated], wt).split()[1:]
                proof.need(parents == [production], "isolated promotion must have production as its sole parent")
                if on_prepared:
                    on_prepared(isolated)
                util.git(["push", "-q", "--no-follow-tags", "--recurse-submodules=no",
                          "origin", f"{isolated}:refs/heads/{production_branch}"], wt, timeout=120, retry=False)
                return isolated
            finally:
                util.git(["worktree", "remove", "--force", str(wt)], repo.path, check=False)


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
        _, approvals = review_comments(tk, repo, ticket, policy, state.get("staged_revision") or state["revision"])
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
        current_prod = branch(repo, state["production_branch"])
        if current_prod == state["revision"]:
            state.update(phase="observing_production")
            serve_guard.write(file, state)
        elif current_prod != state["parent"]:
            raise proof.Held("production moved during promotion; reconcile the release")
        elif state.get("isolated"):
            raise proof.Held("an earlier isolated promotion did not reach production; reconcile before retrying")
        elif state.get("promoted_revisions") and branch(repo, repo.target_branch) != state["revision"]:
            raise proof.Held("staging moved after promotion intent; cannot silently widen the approved release")
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
    current_staged = state.get("staged_revision") or state.get("revision")
    if current_staged != revision:
        proof.need(state.get("phase") != "promotion_intent", "an earlier promotion needs reconciliation")
        state = {"revision": revision, "policy": proof.digest(policy), "scope": scope, "phase": "observing_staging"}
        if grant := release_profile.standing(repo, ticket):
            state.update(standing_authority=grant["id"], production_checks=grant["production_checks"])
        serve_guard.write(file, state)
    proof.need(state["policy"] == proof.digest(policy) and state["scope"] == scope,
               "the tested behavior changed; a corrected staging revision and fresh QA are required")
    from . import ticket_feedback

    proof.need(not ticket_feedback.pending_corrections(repo.name, ticket),
               "pending authorized feedback requires a corrected staging revision and fresh QA")
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
    if state.get("phase") in ("awaiting_qa", "staging_verified") and (peer := peer_promotion(repo, ticket, state, policy)):
        _, _, peer_approval = ticket_approval(cfg, repo, ticket, policy, state)
        current_prod = branch(repo, policy["production_branch"])
        if current_prod == peer["revision"]:
            if peer_approval:
                state.update(approval=peer_approval["id"], approval_comment=peer_approval)
            state.update(phase="observing_production", staged_revision=revision, revision=peer["revision"],
                         production_branch=policy["production_branch"],
                         promoted_revisions=peer["promoted_revisions"], promoted_tickets=peer["promoted_tickets"])
            serve_guard.write(file, state)
            found = proof.observe(repo.name, ticket, state["revision"], destination="production", pinned=state["policy"])
            if found["status"] == "verified":
                state.update(phase="production_verified", production_receipt=found["receipt_id"])
                serve_guard.write(file, state)
            else:
                state["waiting"] = found["reason"]
            return state
        if current_prod == peer["parent"]:
            raise proof.Held("waiting for the active whole-branch promotion to reach production")
    approval, plan = promotion_plan(cfg, repo, ticket, policy, state)
    if plan["reobserve_staging"]:
        result = observe_staging(repo, ticket, revision, pinned=state["policy"])
        proof.need(result["status"] == "verified", "the staged behavior no longer passes its protected check")
    rechecked_approval, rechecked_plan = promotion_plan(cfg, repo, ticket, policy, state)
    proof.need(rechecked_approval == approval and rechecked_plan == plan,
               "QA authorization or release scope changed during verification")
    if approval:
        state.update(approval=approval["id"], approval_comment=approval)
    if plan["graph_revisions"]:
        state["graph_revisions"] = plan["graph_revisions"]
    if plan["isolated"]:
        def record_isolated_intent(isolated_sha):
            state.update(phase="promotion_intent", parent=plan["base"], production_branch=policy["production_branch"],
                         staged_revision=revision, revision=isolated_sha,
                         promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"], isolated=True)
            serve_guard.write(file, state)

        with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
            if state.get("standing_authority"):
                require_standing(repo, ticket, state)
            isolate_revision(repo, revision, plan["base"], policy["production_branch"], on_prepared=record_isolated_intent)
        return state
    target_sha = plan["target"]
    if target_sha != revision:
        state["staged_revision"] = revision
    state.update(phase="promotion_intent", parent=plan["base"], production_branch=policy["production_branch"],
                 revision=target_sha, promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"])
    serve_guard.write(file, state)
    # A non-forced update of a direct child cannot land unrelated work if the branch races.
    api = f"repos/{repo.github}/git/refs/heads/{quote(state['production_branch'], safe='/')}"
    with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
        if state.get("standing_authority"):
            require_standing(repo, ticket, state)
        gh.gh(["api", api, "--method", "PATCH", "--input", "-"],
              input=gh.Outgoing(json.dumps({"sha": target_sha, "force": False})), write=True, retry=False, timeout=20)
    return state
