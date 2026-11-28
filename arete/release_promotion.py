"""Observe and promote an exact staging revision under its enrolled authority."""
from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import tempfile
import threading
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import quote, urlencode

from . import adf, cohorts, config, gh, jira, paths, release_profile, release_proof as proof, serve_guard, tracker, util
from .store import specgraphs, specs

NEUTRAL = {"thanks", "thank you", "looks good", "lgtm", "ack"}
EXPLICIT = {"promote this to production", "push this to production", "moving this to push to production on the sheet"}
APPROVE = re.compile(r"^promote ([a-f0-9]{40}(?:[a-f0-9]{24})?) to production[.!]?$", re.I)
LIVE_DEPENDENCY = re.compile(r"\b(?:live|deployed|deployment|in production|production)\b", re.I)
_HELD_LOCKS = threading.local()


def _thread_locks():
    locks = getattr(_HELD_LOCKS, "keys", None)
    if locks is None:
        locks = _HELD_LOCKS.keys = set()
    return locks


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


def _actor_in_list(tk, actor, accounts):
    if not actor or not accounts:
        return False
    if isinstance(tk, tracker.GitHubTracker):
        return actor.casefold() in {a.casefold() for a in accounts}
    return actor in set(accounts)


def _qa_authorized(tk, repo, policy, actor):
    if not actor:
        return False
    if _actor_in_list(tk, actor, policy.get("qa") or ()):
        return True
    if isinstance(tk, tracker.GitHubTracker) and repo.commenters:
        return actor.casefold() in {c.casefold() for c in repo.commenters}
    if isinstance(tk, tracker.JiraTracker) and policy.get("qa_authority") in ("tracker", "team"):
        return True
    return False


def _release_owner_authorized(tk, policy, actor):
    return _actor_in_list(tk, actor, policy.get("release_owners") or ())


def _comment_url(tk, repo, ticket, cid):
    return (gh.issue_url(repo.github, ticket) + "#issuecomment-" + cid if isinstance(tk, tracker.GitHubTracker)
            else tk.client.comment_url(ticket, cid))


def _match_approval(line, policy, revision, *, for_release_owner=False):
    from . import ticket_feedback

    directive = APPROVE.fullmatch(line)
    matched_prod, prod_rev = ticket_feedback.match_production_instruction(line)
    matched_owner, owner_rev = (ticket_feedback.match_release_owner_approval(line)
                                if for_release_owner else (False, ""))
    explicit_text = line.rstrip(".!").casefold() in EXPLICIT
    if not (directive or matched_prod or matched_owner or explicit_text):
        return False, False
    explicit_sha = directive[1] if directive else (prod_rev or owner_rev)
    if not explicit_sha and policy.get("qa_require_revision"):
        return True, False
    if explicit_sha and explicit_sha.lower() != revision.lower():
        return True, False
    return True, True


def _valid_approval_metadata(cid, at, edited):
    return bool(cid and at and cid.isdigit() and (not edited or util.parse_iso(edited) == util.parse_iso(at)))


def review_comments(tk, repo, ticket, policy, revision):
    from . import ticket_feedback

    notes = comments(repo, ticket, tk)
    approvals, changes, seen_approvals = [], [], set()
    for who, body, at, edited, cid in notes:
        # GitHub's visible bot types and Arete's own marked handoffs cannot grant release authority.
        qa_ok = _qa_authorized(tk, repo, policy, who)
        owner_ok = _release_owner_authorized(tk, policy, who)
        if not (qa_ok or owner_ok) or ticket_feedback.is_arete_comment(body) or not cid or not at:
            continue
        line = body.strip()
        is_qa_dir, qa_matches = _match_approval(line, policy, revision, for_release_owner=False)
        is_owner_dir, _ = _match_approval(line, policy, revision, for_release_owner=True) if owner_ok else (False, False)
        if is_qa_dir:
            if qa_ok and qa_matches and _valid_approval_metadata(cid, at, edited):
                seen_approvals.add(cid)
                approvals.append({"id": cid, "at": cohorts.timestamp(at), "url": _comment_url(tk, repo, ticket, cid), "actor": who})
        elif is_owner_dir:
            # Release-owner sign-off comments are not behavior-changing corrections.
            continue
        elif line.rstrip(".!").casefold() not in NEUTRAL and not ticket_feedback.is_neutral_acknowledgment(line):
            changes.append((who, cid, line))

    # Also consume authorized production instructions routed through #988 ticket_feedback (including Jira team commenters).
    for row in ticket_feedback.production_instructions(repo.name, str(ticket)):
        if row.get("tracker") != tk.name or not row.get("authorized"):
            continue
        cid = str(row.get("comment_id") or "")
        body = row.get("body") or ""
        at = row.get("created") or ""
        edited = row.get("updated") or ""
        actor = row.get("author") or ""
        if not cid or cid in seen_approvals or ticket_feedback.is_arete_comment(body):
            continue
        recorded_rev = (row.get("data") or {}).get("revision") or ""
        if recorded_rev and recorded_rev.lower() != revision.lower():
            continue
        _, qa_matches = _match_approval(body.strip(), policy, revision, for_release_owner=False)
        if qa_matches and _valid_approval_metadata(cid, at, edited):
            seen_approvals.add(cid)
            url = row.get("url") or _comment_url(tk, repo, ticket, cid)
            approvals.append({"id": cid, "at": cohorts.timestamp(at), "url": url, "actor": actor})
    return changes, approvals


def release_owner_required(policy, t=None):
    if not policy:
        return False
    if policy.get("require_release_owner") or policy.get("irreversible"):
        return True
    if policy.get("release_owners") and policy.get("require_release_owner") is not False:
        return True
    labels = set(getattr(t, "labels", None) or ()) if t is not None else set()
    return bool(labels & {"irreversible", "release-owner-required"})


def release_owner_approvals(tk, repo, ticket, policy, revision):
    from . import ticket_feedback

    if not policy.get("release_owners"):
        return []
    found, seen = [], set()
    for who, body, at, edited, cid in comments(repo, ticket, tk):
        if not _release_owner_authorized(tk, policy, who) or ticket_feedback.is_arete_comment(body) or not cid or not at:
            continue
        _, matched = _match_approval(body.strip(), policy, revision, for_release_owner=True)
        if matched and _valid_approval_metadata(cid, at, edited):
            seen.add(cid)
            found.append({"id": cid, "at": cohorts.timestamp(at), "url": _comment_url(tk, repo, ticket, cid), "actor": who})
    for row in ticket_feedback.production_instructions(repo.name, str(ticket)):
        if row.get("tracker") != tk.name or not row.get("authorized"):
            continue
        cid = str(row.get("comment_id") or "")
        actor = row.get("author") or ""
        body = row.get("body") or ""
        at = row.get("created") or ""
        edited = row.get("updated") or ""
        if not cid or cid in seen or not _release_owner_authorized(tk, policy, actor) or ticket_feedback.is_arete_comment(body):
            continue
        recorded_rev = (row.get("data") or {}).get("revision") or ""
        if recorded_rev and recorded_rev.lower() != revision.lower():
            continue
        _, matched = _match_approval(body.strip(), policy, revision, for_release_owner=True)
        if matched and _valid_approval_metadata(cid, at, edited):
            seen.add(cid)
            url = row.get("url") or _comment_url(tk, repo, ticket, cid)
            found.append({"id": cid, "at": cohorts.timestamp(at), "url": url, "actor": actor})
    return found


def release_workflow_config(policy):
    wf = (policy or {}).get("release_workflow")
    if not wf:
        return {"kind": "ref_update"}
    if isinstance(wf, str):
        return {"kind": wf}
    return dict(wf)


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


def _release_owner_signoff(tk, repo, ticket, policy, state, staged_rev, t):
    if not release_owner_required(policy, t):
        return None
    proof.need(bool(policy.get("release_owners")),
               "irreversible production action requires configured release_owners")
    owner_list = release_owner_approvals(tk, repo, ticket, policy, staged_rev)
    choices = after_handoff(state, owner_list) if state.get("handoff") else owner_list
    proof.need(choices, "waiting for release-owner approval before irreversible production action")
    return min(choices, key=lambda row: (row["at"], int(row["id"])))


def ticket_approval(cfg, repo, ticket, policy, state):
    from . import ticket_feedback

    staged_rev = state.get("staged_revision") or state["revision"]
    tk, t, scope, approvals = authority(cfg, repo, ticket, policy, staged_rev)
    proof.need(state.get("phase") != "paused",
               state.get("paused_reason") or "paused: the tested behavior changed; returning to staging for fresh QA")
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
    _release_owner_signoff(tk, repo, ticket, policy, state, staged_rev, t)
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
    owner_approval = _release_owner_signoff(tk, repo, ticket, policy, state, staged_rev, t)
    owner_meta = {"release_owner_approval": owner_approval} if owner_approval else {}
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
                          "reobserve_staging": reobserve, **owner_meta}

    if mode == "whole-branch":
        proof.need(staging_receipt_valid(repo, ticket, staged_rev, state),
                   "the tested staging receipt is missing or no longer matches")
        proof.need(production == proof.deployed_revision(policy, policy["production"]),
                   "promotion would include other unapproved changes or production identity is unconfirmed")
        return approval, {**whole_branch_plan(cfg, repo, ticket, tk, t, policy, state, production, staging_tip),
                          **owner_meta}

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
                      "reobserve_staging": True, **owner_meta}


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


def isolate_revision(repo, revision, production, production_branch, *, on_prepared=None, push_ref=""):
    """Cherry-pick a single staged commit onto production in an isolated worktree and push non-force."""
    proof.need(repo.path.is_dir() and (repo.path / ".git").exists(),
               "per-change isolation requires a local repository checkout")
    target_ref = push_ref or f"refs/heads/{production_branch}"
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
                          "origin", f"{isolated}:{target_ref}"], wt, timeout=120, retry=False)
                return isolated
            finally:
                util.git(["worktree", "remove", "--force", str(wt)], repo.path, check=False)


def pause_for_feedback(repo_name, ticket, *, reason, comment_id=""):
    """Safely pause a staged promotion before it enters production and clear stale QA approvals."""
    file = proof.state_dir(repo_name, str(ticket)) / "promotion.json"
    lock_name = "promote-" + proof.key(repo_name, str(ticket))
    held = _thread_locks()
    cm = nullcontext(True) if lock_name in held else util.lock(lock_name, wait=True)
    with cm:
        state = serve_guard.read(file) or {}
        if state.get("phase") not in ("awaiting_qa", "staging_verified", "staged_for_parent", "paused"):
            return None
        state.update(
            phase="paused",
            paused_reason=reason,
            paused_revision=state.get("staged_revision") or state.get("revision", ""),
            paused_at=util.iso(),
            waiting=reason,
        )
        if comment_id:
            state["paused_by_comment"] = str(comment_id)
        for stale in ("approval", "approval_comment", "release_owner_approval"):
            state.pop(stale, None)
        serve_guard.write(file, state)
        return state


def _queue_behavior_change_feedback(cfg, repo, tk, ticket, t, state, policy, revision):
    """Ensure post-handoff behavior-changing comments are queued in ticket_feedback for the worker."""
    from . import ticket_feedback
    from .store import feedback as feedback_store

    if not feedback_store.installed():
        return
    handoff = state.get("handoff") or ""
    anchor = re.search(r"(?:issuecomment-|focusedCommentId=)([0-9]+)(?:$|[&#])", handoff)
    anchor_id = int(anchor[1]) if anchor else 0
    qa_since = util.parse_iso(state["qa_since"]) if state.get("qa_since") else None
    thread_comments = []
    for who, body, at, edited, cid in comments(repo, ticket, tk):
        if not cid or not at or ticket_feedback.is_arete_comment(body):
            continue
        if anchor_id and cid.isdigit():
            if int(cid) <= anchor_id:
                continue
        elif qa_since:
            ts = ticket_feedback.parse_timestamp(at)
            if ts and ts <= qa_since:
                continue
        line = body.strip()
        if not line or line.rstrip(".!").casefold() in NEUTRAL or ticket_feedback.is_neutral_acknowledgment(line):
            continue
        if _match_approval(line, policy, revision, for_release_owner=True)[0]:
            continue
        thread_comments.append(ticket_feedback.ThreadComment(
            comment_id=cid,
            author=who,
            author_display=who,
            body=body,
            url=_comment_url(tk, repo, ticket, cid),
            created=at,
            updated=edited or "",
        ))
    if thread_comments:
        ticket_feedback.consume_ticket(cfg, repo, str(ticket), tk=tk, ticket=t, comments=thread_comments, since="")


def tag_sha(repo, tag_name):
    try:
        row = gh.gh_json(["api", f"repos/{repo.github}/git/ref/tags/{quote(tag_name, safe='/')}"],
                         timeout=15, retry=False)
    except gh.GhError:
        return ""
    value = row.get("object") or {}
    if row.get("ref") == "refs/tags/" + tag_name and serve_guard.SHA.fullmatch(value.get("sha", "")):
        return value["sha"]
    return ""


def workflow_dispatch_run(repo, state):
    wf = state.get("workflow", "")
    if not wf:
        return None
    runs = gh.actions_rows(
        f"repos/{repo.github}/actions/workflows/{quote(wf, safe='')}/runs?event=workflow_dispatch&per_page=20",
        "workflow_runs", timeout=20, retry=False,
    )
    want_id = state.get("workflow_run_id")
    if want_id is not None:
        for run in runs:
            if run.get("id") == want_id:
                return run
    dispatched = util.parse_iso(state["dispatched_at"]) if state.get("dispatched_at") else None
    wf_ref = state.get("workflow_ref", "")
    for run in runs:
        if wf_ref and run.get("head_branch") and run.get("head_branch") != wf_ref:
            continue
        created = run.get("created_at") or ""
        if dispatched and created:
            try:
                if util.parse_iso(created) < dispatched:
                    continue
            except ValueError:
                pass
        return run
    return None


def _ensure_remote_ref(repo, full_ref, sha):
    api = f"repos/{repo.github}/git/refs"
    try:
        gh.gh(["api", api, "--method", "POST", "--input", "-"],
              input=gh.Outgoing(json.dumps({"ref": full_ref, "sha": sha})),
              write=True, retry=False, timeout=20)
    except gh.GhError as error:
        if "Reference already exists" not in str(error):
            raise
        ref_path = full_ref.removeprefix("refs/")
        existing = gh.gh_json(["api", f"repos/{repo.github}/git/ref/{quote(ref_path, safe='/')}"],
                              timeout=15, retry=False)
        existing_sha = (existing.get("object") or {}).get("sha", "")
        proof.need(existing_sha == sha, f"release ref {full_ref} already points to a different revision")


def reconcile_promotion_intent(repo, ticket, policy, state, file):
    """Reconcile an in-flight promotion across restarts and lost write confirmations."""
    wf_kind = state.get("release_workflow") or "ref_update"
    if wf_kind == "release_pr":
        pr_url = state.get("release_pr", "")
        if not pr_url and state.get("release_branch"):
            pr_url = (gh.open_pr_url(repo.github, state["release_branch"])
                      or (gh.find_pr_marker(repo.github, state["release_branch"], state["release_marker"])
                          if state.get("release_marker") else ""))
        if pr_url:
            state.update(release_pr=pr_url, release_ref=pr_url)
            pr = gh.pr_view(pr_url)
            pr_state = pr.get("state")
            if pr_state == "MERGED":
                merged_sha = (pr.get("mergeCommit") or {}).get("oid", "")
                if serve_guard.SHA.fullmatch(merged_sha or ""):
                    state["revision"] = merged_sha
                state.update(phase="observing_production")
                serve_guard.write(file, state)
                return True
            serve_guard.write(file, state)
            if pr_state == "OPEN":
                raise proof.Held(f"waiting for release PR {pr_url} to merge")
            raise proof.Held(f"release PR {pr_url} closed without merging; reconcile before retrying")
        current_prod = branch(repo, state["production_branch"])
        if current_prod == state["revision"]:
            state.update(phase="observing_production")
            serve_guard.write(file, state)
            return True
        if current_prod != state["parent"]:
            raise proof.Held("production moved during release PR promotion; reconcile the release")
        raise proof.Held("release PR confirmation was lost; reconcile before retrying")

    if wf_kind == "tag":
        tag_name = state.get("release_tag", "")
        found_sha = tag_sha(repo, tag_name) if tag_name else ""
        if found_sha == state["revision"]:
            state.update(phase="observing_production", release_ref=f"refs/tags/{tag_name}")
            serve_guard.write(file, state)
            return True
        if found_sha:
            raise proof.Held(f"release tag {tag_name} points to a different revision; reconcile the release")
        current_prod = branch(repo, state["production_branch"])
        if current_prod == state["revision"]:
            state.update(phase="observing_production")
            serve_guard.write(file, state)
            return True
        if current_prod != state["parent"]:
            raise proof.Held("production moved during release tag promotion; reconcile the release")
        if state.get("isolated"):
            raise proof.Held("an earlier isolated tag promotion did not land; reconcile before retrying")
        return False

    if wf_kind == "workflow_dispatch":
        run = workflow_dispatch_run(repo, state)
        if run:
            state["workflow_run_id"] = run["id"]
            if run.get("html_url"):
                state["release_ref"] = run["html_url"]
            if run.get("status") == "completed":
                if run.get("conclusion") == "success":
                    state.update(phase="observing_production")
                    serve_guard.write(file, state)
                    return True
                serve_guard.write(file, state)
                raise proof.Held(
                    f"release workflow {state.get('workflow', '')} failed ({run.get('conclusion') or 'failure'}); spec stays open"
                )
            serve_guard.write(file, state)
            raise proof.Held(f"waiting for release workflow {state.get('workflow', '')} run {run['id']} to complete")
        try:
            if proof.deployed_revision(policy, policy["production"]) == state["revision"]:
                state.update(phase="observing_production")
                serve_guard.write(file, state)
                return True
        except (OSError, ValueError, proof.Held):
            pass
        raise proof.Held(f"waiting for release workflow {state.get('workflow', '')} confirmation; reconcile before retrying")

    # Default ref_update reconciliation.
    current_prod = branch(repo, state["production_branch"])
    if current_prod == state["revision"]:
        state.update(phase="observing_production")
        serve_guard.write(file, state)
        return True
    if current_prod != state["parent"]:
        raise proof.Held("production moved during promotion; reconcile the release")
    if state.get("isolated"):
        raise proof.Held("an earlier isolated promotion did not reach production; reconcile before retrying")
    if state.get("promoted_revisions") and branch(repo, repo.target_branch) != state["revision"]:
        raise proof.Held("staging moved after promotion intent; cannot silently widen the approved release")
    return False


def execute_release_workflow(repo, ticket, policy, state, file, plan, revision):
    wf = release_workflow_config(policy)
    kind = wf["kind"]
    prod_branch = policy["production_branch"]

    if kind == "ref_update":
        if plan["isolated"]:
            def record_isolated_intent(isolated_sha):
                state.update(phase="promotion_intent", parent=plan["base"], production_branch=prod_branch,
                             staged_revision=revision, revision=isolated_sha,
                             promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"], isolated=True)
                serve_guard.write(file, state)

            with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
                if state.get("standing_authority"):
                    require_standing(repo, ticket, state)
                isolate_revision(repo, revision, plan["base"], prod_branch, on_prepared=record_isolated_intent)
            return state
        target_sha = plan["target"]
        if target_sha != revision:
            state["staged_revision"] = revision
        state.update(phase="promotion_intent", parent=plan["base"], production_branch=prod_branch,
                     revision=target_sha, promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"])
        serve_guard.write(file, state)
        # A non-forced update of a direct child cannot land unrelated work if the branch races.
        api = f"repos/{repo.github}/git/refs/heads/{quote(prod_branch, safe='/')}"
        with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
            if state.get("standing_authority"):
                require_standing(repo, ticket, state)
            gh.gh(["api", api, "--method", "PATCH", "--input", "-"],
                  input=gh.Outgoing(json.dumps({"sha": target_sha, "force": False})), write=True, retry=False, timeout=20)
        return state

    if kind == "release_pr":
        prefix = wf.get("branch_prefix") or "arete/release"
        release_branch = f"{prefix}/{ticket}-{revision[:12]}"
        marker = f"arete-release-{ticket}-{revision[:12]}"
        if plan["isolated"]:
            def record_pr_isolated(isolated_sha):
                state.update(phase="promotion_intent", release_workflow="release_pr",
                             release_branch=release_branch, release_marker=marker,
                             parent=plan["base"], production_branch=prod_branch,
                             staged_revision=revision, revision=isolated_sha,
                             promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"], isolated=True)
                serve_guard.write(file, state)

            with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
                if state.get("standing_authority"):
                    require_standing(repo, ticket, state)
                isolate_revision(repo, revision, plan["base"], prod_branch,
                                 on_prepared=record_pr_isolated, push_ref=f"refs/heads/{release_branch}")
        else:
            target_sha = plan["target"]
            state.update(phase="promotion_intent", release_workflow="release_pr",
                         release_branch=release_branch, release_marker=marker,
                         parent=plan["base"], production_branch=prod_branch,
                         staged_revision=revision, revision=target_sha,
                         promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"])
            serve_guard.write(file, state)
            with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
                if state.get("standing_authority"):
                    require_standing(repo, ticket, state)
                _ensure_remote_ref(repo, f"refs/heads/{release_branch}", target_sha)
        pr_url = (gh.open_pr_url(repo.github, release_branch)
                  or gh.find_pr_marker(repo.github, release_branch, marker))
        if not pr_url:
            title = f"Release {ticket} ({state['revision'][:12]}) to {prod_branch}"
            body = gh.marked(
                f"Promote verified staging revision `{state['revision']}` for ticket `{ticket}` to `{prod_branch}`.\n\n{marker}"
            )
            pr_url = gh.pr_create(repo.github, release_branch, prod_branch, title, body)
        state.update(release_pr=pr_url, release_ref=pr_url)
        serve_guard.write(file, state)
        return state

    if kind == "tag":
        prefix = wf.get("prefix") or "release-"
        tag_name = f"{prefix}{ticket}-{revision[:12]}"
        if plan["isolated"]:
            def record_tag_isolated(isolated_sha):
                state.update(phase="promotion_intent", release_workflow="tag", release_tag=tag_name,
                             parent=plan["base"], production_branch=prod_branch,
                             staged_revision=revision, revision=isolated_sha,
                             promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"], isolated=True)
                serve_guard.write(file, state)

            with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
                if state.get("standing_authority"):
                    require_standing(repo, ticket, state)
                isolate_revision(repo, revision, plan["base"], prod_branch,
                                 on_prepared=record_tag_isolated, push_ref=f"refs/tags/{tag_name}")
        else:
            target_sha = plan["target"]
            state.update(phase="promotion_intent", release_workflow="tag", release_tag=tag_name,
                         parent=plan["base"], production_branch=prod_branch,
                         staged_revision=revision, revision=target_sha,
                         promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"])
            serve_guard.write(file, state)
            with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
                if state.get("standing_authority"):
                    require_standing(repo, ticket, state)
                _ensure_remote_ref(repo, f"refs/tags/{tag_name}", target_sha)
        state["release_ref"] = f"refs/tags/{tag_name}"
        serve_guard.write(file, state)
        return state

    # workflow_dispatch
    wf_name = wf["workflow"]
    wf_ref = wf.get("ref") or prod_branch
    dispatch_id = f"arete-{ticket}-{revision[:12]}"
    if plan["isolated"]:
        release_branch = f"arete/release/{ticket}-{revision[:12]}"

        def record_wf_isolated(isolated_sha):
            state.update(phase="promotion_intent", release_workflow="workflow_dispatch",
                         workflow=wf_name, workflow_ref=release_branch, dispatch_id=dispatch_id,
                         dispatched_at=util.iso(), parent=plan["base"], production_branch=prod_branch,
                         staged_revision=revision, revision=isolated_sha,
                         promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"], isolated=True)
            serve_guard.write(file, state)

        with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
            if state.get("standing_authority"):
                require_standing(repo, ticket, state)
            isolate_revision(repo, revision, plan["base"], prod_branch,
                             on_prepared=record_wf_isolated, push_ref=f"refs/heads/{release_branch}")
        wf_ref = release_branch
    else:
        target_sha = plan["target"]
        state.update(phase="promotion_intent", release_workflow="workflow_dispatch",
                     workflow=wf_name, workflow_ref=wf_ref, dispatch_id=dispatch_id,
                     dispatched_at=util.iso(), parent=plan["base"], production_branch=prod_branch,
                     staged_revision=revision, revision=target_sha,
                     promoted_revisions=plan["revisions"], promoted_tickets=plan["tickets"])
        serve_guard.write(file, state)
    inputs = {**(wf.get("inputs") or {}), "revision": state["revision"], "ticket": str(ticket), "dispatch_id": dispatch_id}
    api = f"repos/{repo.github}/actions/workflows/{quote(wf_name, safe='')}/dispatches"
    with util.lock("config", wait=True) if state.get("standing_authority") else nullcontext():
        if state.get("standing_authority"):
            require_standing(repo, ticket, state)
        gh.gh(["api", api, "--method", "POST", "--input", "-"],
              input=gh.Outgoing(json.dumps({"ref": wf_ref, "inputs": inputs})),
              write=True, retry=False, timeout=20)
    return state


def verify_production_observation(cfg, repo, ticket, policy, state, file, found):
    from . import spec_graph_delivery

    if found["status"] != "verified":
        state["waiting"] = found["reason"]
        return False
    ticket_obj = None
    try:
        tk = tracker.for_ticket(cfg, repo, ticket)
        if isinstance(tk, (tracker.GitHubTracker, tracker.JiraTracker)):
            ticket_obj = tk.get(ticket)
    except (gh.GhError, jira.JiraError, tracker.TaskError, KeyError, ValueError, OSError):
        ticket_obj = None
    reqs = proof.required_activations(policy, ticket_obj)
    if missing := spec_graph_delivery.activations_verified(reqs, receipt=found):
        state["waiting"] = "waiting for required feature-flag / audience activation: " + "; ".join(missing)
        return False
    state.update(phase="production_verified", production_receipt=found["receipt_id"],
                 production_revision=state["revision"])
    if reqs:
        state["activations_verified"] = reqs
    serve_guard.write(file, state)
    return True


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
    lock_name = "promote-" + proof.key(repo.name, ticket)
    with util.lock(lock_name) as acquired:
        if not acquired:
            return {"phase": "in_progress"}
        held = _thread_locks()
        held.add(lock_name)
        try:
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
        finally:
            held.discard(lock_name)


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
        reconcile_promotion_intent(repo, ticket, policy, state, file)
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
        verify_production_observation(cfg, repo, ticket, policy, state, file, found)
        return state
    revision = selected_revision(repo, ticket)
    proof.need(revision and serve_guard.SHA.fullmatch(revision), "waiting for the enrolled ticket's merged staging PR")
    proof.need(not policy.get("revision") or policy["revision"] == revision, "goal release differs from the pinned revision")
    tk, t, scope, _ = authority(cfg, repo, ticket, policy, revision)
    current_staged = state.get("staged_revision") or state.get("revision")
    if current_staged != revision:
        proof.need(state.get("phase") != "promotion_intent", "an earlier promotion needs reconciliation")
        state = {"revision": revision, "policy": proof.digest(policy), "scope": scope, "phase": "observing_staging"}
        if grant := release_profile.standing(repo, ticket):
            state.update(standing_authority=grant["id"], production_checks=grant["production_checks"])
        serve_guard.write(file, state)
    proof.need(state["policy"] == proof.digest(policy),
               "the tested behavior changed; a corrected staging revision and fresh QA are required")
    from . import ticket_feedback

    pending_fb = ticket_feedback.pending_corrections(repo.name, ticket)
    scope_changed = state["scope"] != scope
    if state.get("phase") in ("awaiting_qa", "staging_verified", "staged_for_parent", "paused") and (
            scope_changed or pending_fb or state.get("phase") == "paused"):
        if scope_changed and not pending_fb:
            _queue_behavior_change_feedback(cfg, repo, tk, ticket, t, state, policy, revision)
        reason = (state.get("paused_reason") if state.get("phase") == "paused" and not scope_changed and not pending_fb
                  else "paused: the tested behavior changed; returning to staging for fresh QA")
        state.update(phase="paused", paused_reason=reason, paused_revision=current_staged)
        for stale in ("approval", "approval_comment", "release_owner_approval"):
            state.pop(stale, None)
        serve_guard.write(file, state)
        raise proof.Held(reason)
    proof.need(not scope_changed, "the tested behavior changed; a corrected staging revision and fresh QA are required")
    proof.need(not pending_fb, "pending authorized feedback requires a corrected staging revision and fresh QA")
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
            verify_production_observation(cfg, repo, ticket, policy, state, file, found)
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
    if plan.get("release_owner_approval"):
        state["release_owner_approval"] = plan["release_owner_approval"]
    if plan["graph_revisions"]:
        state["graph_revisions"] = plan["graph_revisions"]
    return execute_release_workflow(repo, ticket, policy, state, file, plan, revision)
