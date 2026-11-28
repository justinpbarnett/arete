from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from . import config, gh, jira, outbound, paths, pilot, release_profile, release_proof, requirement_revision, tracker, util, vault, verify, zte
from .store import orchestration, runs, specs, workspaces

GUIDANCE = (
    "Deliver this spec on its own ticket, with no child tickets or graph. Inspect the checkout if needed. "
    "Before implementation, use spec.define for one observable outcome and a reproducible procedure grounded in "
    "the spec and owner answers. If they leave a product decision open, ask one focused owner question instead "
    "of assuming an answer. Leave environment blank unless the owner requires proof at a named runtime. "
    "Confine changes to the spec's explicitly authorized repositories; stop and ask the owner before touching "
    "any repository outside the published scope. "
    "Run checks, independent review and in-use verification of the exact criterion. Do not request an "
    "unverified exception. Publication produces a candidate, not a delivery receipt. The engine checks landing "
    "and keeps the ticket open until it has evidence at the delivery point."
)
SHA = re.compile(r"[0-9a-f]{40}")


def definition(values: dict) -> dict:
    found = {key: verify.one_line(value) for key, value in values.items()}
    if not found["criterion"] or not found["procedure"] or any(len(v) > 1000 for v in found.values()):
        raise ValueError("spec.define needs one observable criterion and its verification procedure (up to 1000 characters)")
    return found


def protected_environment(repo, ticket, requested):
    policy = release_proof.goal(repo, ticket)
    if not policy:
        return requested
    default = policy["destination"]
    options = {default: default}
    if default == "staging":
        options["production"] = "production"
    aliases = {str(policy[point]).rstrip("/").casefold(): point for point in options if point in policy}
    value = requested.strip().rstrip("/").casefold()
    if not value:
        if default == "staging" and release_profile.standing_file(repo).exists():
            if release_profile.standing(config.load_required().repo(repo), ticket):
                return "production"
        return default
    if (target := (options | aliases).get(value)):
        return target
    raise ValueError("Use the enrolled final delivery point: " + " or ".join(options))


def describe(done: dict) -> str:
    return (f"## Definition of done\n\n{done['criterion']}\n\nObserve it by: {done['procedure']}"
            + (f"\n\nRequired runtime: {done['environment']}" if done.get("environment") else ""))


def obligation(repo, ticket, done, identity=""):
    """Reserve the protected observation that will run after this spec lands."""
    target = done.get("environment", "")
    if not target:
        return {}
    policy = release_proof.goal(repo, ticket.id)
    if not policy:
        release_proof.need(target != "arete-serve" and not release_proof.required(repo, ticket.id),
                           "the native spec's protected delivery enrollment is missing")
        return {}
    release_proof.need(protected_environment(repo, ticket.id, target) == target,
                       "the spec destination does not match its protected enrollment")
    release_proof.need(bool(identity or ticket.spec_identity) and not (identity and ticket.spec_identity and identity != ticket.spec_identity),
                       "the protected delivery obligation needs the same authorized spec identity")
    return {"repo": repo, "ticket": ticket.id, "identity": identity or ticket.spec_identity, "source": source_version(ticket),
            "definition": orchestration.digest(done),
            "destination": target, "path": policy["path"], "policy": release_proof.digest(policy)}


def delivery_declarations(ticket):
    return [line.strip().removeprefix("Delivery observation:").strip() for line in ticket.body.splitlines()
            if line.strip().startswith("Delivery observation:")]


def declared_observations(ticket, promise):
    rows = delivery_declarations(ticket)
    if not rows:
        return {}, ""
    release_proof.need(bool(promise), "the declared delivery observation needs a protected native enrollment")
    name = f"Protected release receipt for {promise['destination']} {promise['path']} under policy {promise['policy']}"
    release_proof.need(rows == [name], f"the only enrolled delivery observation is: {name}")
    checks = requirement_revision.criteria(ticket.body)[1]
    release_proof.need(bool(checks), "declare distinct AC-N candidate checks alongside the protected delivery observation")
    return checks, name


def superseded_observations(actions, revised, carried, superseded, promise):
    """Identify prior verifier findings that the publisher may explicitly replace with revised checks."""
    if not carried or not declared_observations(revised, promise)[0]:
        return {}
    declared = (revised.title + "\n" + revised.body + "\n" + (revised.owner_comments or "")).casefold()
    found = {}
    for action in actions:
        result = action["result"]
        if (action["id"] in superseded or action["op"] != "verification.run" or action["state"] != "succeeded"
                or not result.get("operation_passed") or not result.get("native_trees") or result.get("verification_requirements")
                or result.get("delivery_obligation") != promise or result.get("pending_delivery") != orchestration.digest(promise)):
            continue
        stamp = result.get("evidence", {})
        report = Path(result.get("saved") or "")
        if (stamp.get("producer") != action["id"] or stamp.get("result", {}).get("saved") != str(report)
                or report.parent != paths.RUNS_DIR / action["run"] or not re.fullmatch(r"verify(?:[1-9][0-9]*)?\.md", report.name)):
            continue
        try:
            raw = json.loads(zte.private_bytes(report.with_suffix(".json"), 2_000_000))
        except (OSError, ValueError, pilot.Held):
            continue
        if (not isinstance(raw, dict) or raw.get("ok") is not True or raw.get("runner") not in ("claude", "codex", "pi")
                or not isinstance(raw.get("structured"), dict)):
            continue
        try:
            payload = raw["structured"]
            typed = any(isinstance(v, dict) for v in (payload.get("unmeasured_requirements") or []))
            measured = verify.Verified.from_agent(payload, delivery_token=result["pending_delivery"] if typed else "")
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if measured.verdict != "pass" or measured.pending_delivery != result["pending_delivery"]:
            continue
        for name in measured.unmeasured_requirements:
            if name in carried and name in result.get("unattempted_requirements", []) and name.casefold() not in declared:
                found.setdefault(name, []).append({"action": action["id"], "report": orchestration.digest(raw["structured"])})
    return found


def candidate_instructions(promise):
    token = orchestration.digest(promise)
    return (f"\n\nArete has reserved the post-merge release observation for this spec at {promise['destination']} "
            f"on {promise['path']}, under protected policy {promise['policy']}. Use the candidate to independently "
            "exercise every observable requirement, including access rules and error cases. Return the exact spec "
            "criterion in a check describing what you actually observed on the candidate. The service will exercise "
            "the promised behavior on the running release and retain that receipt before closing the spec. "
            f"Report this later step using pending_delivery = {token}. Keep every unknown or failed candidate "
            "requirement in your evidence and unmeasured_requirements. Never claim a release observation here.")


def candidate_report(report, promise):
    expected = orchestration.digest(promise) if promise else ""
    if report.verdict == "pass" and report.pending_delivery != expected:
        return replace(report, verdict="blocked", blocked_by="The candidate verifier did not identify the enrolled delivery obligation.",
                       delivery_obligation=promise)
    return replace(report, delivery_obligation=promise)


def legacy_directions(lease, done, carried, promise, superseded):
    directions = {}
    for action in orchestration.actions(lease) if carried and promise else ():
        row = action["result"]
        acceptance = row.get("evidence", {}).get("acceptance")
        if (action["op"] != "verification.run" or action["state"] != "succeeded" or row.get("delivery_obligation")
                or not acceptance or action["id"] in superseded or row.get("verdict") != "blocked"
                or not row.get("operation_passed") or not row.get("finished") or not row.get("behavior_reached")):
            continue
        gaps, checks = row.get("unmeasured_requirements", []), row.get("checks", [])
        if (len(gaps) != 1 or gaps[0] not in carried or gaps[0] in directions or row.get("unattempted_requirements") != gaps
                or row.get("verification_requirements") or done["criterion"].casefold() not in {c["what"].casefold() for c in checks}
                or not all(c.get("passed") is True and c.get("how") and c.get("saw") for c in checks)):
            continue
        token = orchestration.digest([lease.root, action["id"], acceptance, promise, gaps[0]])[:24]
        directions[gaps[0]] = {"text": f"Assign release observation {token} to delivery",
                               "checks": [c["what"] for c in checks]}
    return directions


def confirmed_directions(tk, ticket, directions):
    found = {}
    for body, url, _ in comments(tk, ticket) if directions else ():
        for name, direction in directions.items():
            if body.strip() == direction["text"] and url and confirmed_observation(tk, ticket, body, url):
                found[name] = {"comment": url, "text": body}
    return found


def behavior(report: verify.Verified, done: dict) -> dict:
    criterion = done["criterion"].casefold()
    if (report.verdict != "pass" or not report.finished or not report.behavior_reached or report.failed_checks
            or criterion not in {name.casefold() for name in report.observed_requirements()}):
        return {}
    for check in report.checks:
        if verify.one_line(check.get("what", "")).casefold() == criterion:
            how, saw = (verify.one_line(check.get(key, ""))[:2000] for key in ("how", "saw"))
            if how and saw:
                return {"how": how, "saw": saw}
    return {}


def source_version(ticket: tracker.Ticket) -> str:
    return requirement_revision.version(ticket)


def landing_policy(tk: tracker.Tracker) -> list[list[str]]:
    return [list(pair) for pair in tk.cfg.landed.items()] if isinstance(tk, tracker.JiraTracker) else []


def candidate(job, done: dict, policy: str, acceptance: str, trees: dict) -> dict:
    from . import cross_repo

    observed = behavior(job.verified, done)
    raw_direct = getattr(job, "direct_push", None)
    direct = raw_direct if isinstance(raw_direct, dict) else {}
    if not observed or not job.review.approved() or not (job.with_prs or direct):
        raise ValueError("publication has no reviewed, observed spec outcome on its PR candidate")
    cfg = getattr(job, "cfg", None)
    if cfg is None:
        cfg = config.Config(repos=[job.run.repo])
    authorized, auth_error = cross_repo.authorized_repos(cfg, job.run.repo, job.ticket)
    if auth_error:
        raise ValueError(auth_error)
    _, unauthorized = cross_repo.check_repo_scope(
        cfg, job.run.repo, job.ticket, [c.github or job.run.repo.name for c in job.checkouts]
    )
    if unauthorized:
        raise ValueError(f"checkout repository outside spec authorization: {', '.join(unauthorized)}")
    targets = []
    for c in job.checkouts:
        pushed = direct.get(c.github or job.run.repo.name)
        head = c.lease if (c.pr or pushed) else util.git(["rev-parse", "HEAD"], c.wt).strip()
        tree = util.git(["rev-parse", f"{head}^{{tree}}"], c.wt).strip() if SHA.fullmatch(head or "") else ""
        if (not SHA.fullmatch(tree) or tree != trees.get(c.prefix)
                or c.pr and gh.pr_repo(c.pr).casefold() != c.github.casefold()):
            raise ValueError("a release checkout does not match the verified spec tree")
        target = {"pr": c.pr, "repo": c.github, "base": c.base, "tree": tree, "head": head}
        if len(authorized) > 1:
            matched = cross_repo.resolve_repo(cfg, c.github) if c.github else job.run.repo
            target["repo_name"] = matched.name if matched else job.run.repo.name
        if pushed and not c.pr:
            target["direct_push"] = True
        targets.append(target)
    receipt = {"definition": done, "source_format": 2, "source": source_version(job.ticket), "policy": policy, "acceptance": acceptance,
               "landing": landing_policy(job.run.tracker), "targets": targets, "behavior": observed}
    if len(authorized) > 1:
        receipt["authorized_repos"] = [r.name for r in authorized]
        obligations = {
            r.name: ob for r in authorized
            if (ob := obligation(r.name, job.ticket, done, job.run.spec_identity))
        }
        if obligations:
            receipt["delivery_obligations"] = obligations
    elif promise := job.verified.delivery_obligation:
        release_proof.need(promise == obligation(job.run.repo.name, job.ticket, done, job.run.spec_identity),
                           "the spec's protected delivery obligation changed before publication")
        receipt["delivery_obligation"] = promise
    if consumed := cross_repo.consumed_peers_context(cfg, job.run.repo, job.ticket):
        receipt["consumed_peers"] = consumed
    return receipt


def revision(repo: str, ref: str) -> tuple[str, str]:
    row = gh.gh_json(["api", f"repos/{repo}/commits/{quote(ref, safe='')}"]) or {}
    sha, tree = row.get("sha", ""), ((row.get("commit") or {}).get("tree") or {}).get("sha", "")
    if not SHA.fullmatch(sha or "") or not SHA.fullmatch(tree or ""):
        raise gh.GhError(f"GitHub did not identify the commit and tree at {repo}:{ref}")
    return sha, tree


def reached(data: dict) -> list[dict]:
    landed = []
    for target in data["targets"]:
        branch = data["landing"][-1][0] if data["landing"] else target["base"]
        merge = ""
        if target["pr"]:
            pr = gh.pr_view(target["pr"])
            merge = (pr.get("mergeCommit") or {}).get("oid", "")
            if (pr.get("state") != "MERGED" or not pr.get("mergedAt") or not SHA.fullmatch(merge or "")
                    or pr.get("headRefOid") != target["head"] or pr.get("baseRefName") != target["base"]):
                return []
        elif target.get("direct_push"):
            merge = target.get("head", "")
            if not SHA.fullmatch(merge or ""):
                return []
        at, tree = revision(target["repo"], branch)
        if merge and merge != at:
            if not gh.branch_contains(target["repo"], at, merge):
                return []
            if branch == target["base"]:
                at, tree = revision(target["repo"], merge)
                if at != merge:
                    return []
        landed.append({**target, "branch": branch, "commit": at, "exact_tree": tree == target["tree"]})
    return landed


def comments(tk: tracker.Tracker, tid: str):
    if isinstance(tk, tracker.GitHubTracker):
        return [(c.get("body") or "", c.get("url") or "", c.get("createdAt") or "")
                for c in (gh.issue(tk.repo.github, int(tid)) or {}).get("comments", []) if tk.by_publisher(c, True)]
    if isinstance(tk, tracker.JiraTracker):
        me = tk.client.me().get("accountId")
        return [(c.get("body") or "", tk.client.comment_url(tid, c["id"]), c.get("created") or "")
                for c in tk.client.comments(tid) if me and (c.get("author") or {}).get("accountId") == me]
    return []


def confirmed_observation(tk, tid, body, url):
    if isinstance(tk, tracker.GitHubTracker):
        for item in requirement_revision.github_issue_pages(tk, tid):
            for comment in item["comments"]["nodes"]:
                if comment["url"] == url:
                    return (comment["body"] == body and tk.by_publisher(comment, True)
                            and (not comment["lastEditedAt"] or tk.by_publisher({"author": comment["editor"]}, True)))
    if isinstance(tk, tracker.JiraTracker):
        me = tk.client.me().get("accountId")
        for comment in tk.client.comments(tid):
            if tk.client.comment_url(tid, comment["id"]) == url:
                editor = (comment.get("updateAuthor") or {}).get("accountId")
                edited = comment.get("updated") not in (None, comment.get("created"))
                return bool(me and comment.get("body") == body and (comment.get("author") or {}).get("accountId") == me
                            and (editor == me or not editor and not edited))
    return False


def save(claim: dict, *, state: str = "") -> None:
    after = state or claim["state"]
    if not specs.advance(claim["identity"], claim["run"], claim["state"], after, claim["data"]):
        raise tracker.TaskError("the spec delivery receipt changed")
    claim["state"] = after


def post_once(claim: dict, tk: tracker.Tracker, kind: str, body: str) -> str:
    data, tid = claim["data"], claim["issue"]
    notes = data.setdefault("notes", {})
    marker_fields = [claim["root"], claim["run"], kind]
    if data.get("amended"):
        marker_fields.append(data["amended"])
    marker = "Arete spec receipt " + orchestration.digest(marker_fields)[:32]
    if notes.get(kind):
        return notes[kind]
    if kind in notes:
        if found := next((url for text, url, _ in comments(tk, tid) if marker in text.splitlines() and url), ""):
            notes[kind] = found
            save(claim)
            return found
        raise tracker.TaskError(f"the spec {kind} comment has no confirmed receipt yet; reconcile it before resending")
    text = outbound.inert(outbound.screen(vault.hidden(f"{tracker.ARETE_TAG} {body}\n\n{marker}")))
    notes[kind] = ""
    save(claim)
    try:
        if isinstance(tk, tracker.GitHubTracker):
            url = gh.issue_comment(tk.repo.github, int(tid), text, return_url=True, single_attempt=True)
        else:
            url = tk.client.comment(tid, text, return_url=True, single_attempt=True)
    except (gh.GhUnsure, jira.JiraUnsure):
        raise
    except (gh.GhError, jira.JiraError):
        notes.pop(kind)
        save(claim)
        raise
    expected = (gh.issue_url(tk.repo.github, tid) + "#issuecomment-" if isinstance(tk, tracker.GitHubTracker)
                else tk.client.browse_url(tid) + "?focusedCommentId=")
    if not url or not url.casefold().startswith(expected.casefold()):
        raise tracker.TaskError(f"the tracker did not confirm the spec {kind} comment")
    notes[kind] = url
    save(claim)
    return url


def location(landed: list[dict]) -> str:
    return "\n".join(f"- `{t['repo']}:{t['branch']}` at `{t['commit']}`" + (f" ({t['pr']})" if t["pr"] else "")
                     for t in landed)


def public_text(tk: tracker.Tracker, text: str) -> str:
    return outbound.redact(text) if tk.repo.public else text


def _target_repo_name(target: dict, tk: tracker.Tracker, cfg: config.Config | None) -> str:
    if name := target.get("repo_name"):
        return name
    slug = (target.get("repo") or "").casefold()
    if tk.repo.github and slug == tk.repo.github.casefold():
        return tk.repo.name
    if cfg is not None:
        from . import cross_repo

        if matched := cross_repo.resolve_repo(cfg, slug):
            return matched.name
    return ""


def format_per_repo_evidence(entries: list[dict]) -> str:
    lines = ["### Per-repository release evidence", ""]
    for entry in entries:
        ref = f"`{entry['repo']}` (`{entry['github']}:{entry['branch']}` at `{entry['commit']}`)"
        receipt = f" — receipt `{entry['receipt_id']}` ({entry['destination']} {entry['path']})" if entry.get("receipt_id") else ""
        lines.append(f"- {ref}: {entry['method']}{receipt}")
        lines.append(f"  - How: {entry['how']}")
        lines.append(f"  - Saw: {entry['saw']}")
    return "\n".join(lines)


def _multi_repo_delivery(claim: dict, tk: tracker.Tracker, landed: list[dict],
                         authorized_names: list[str], cfg: config.Config | None) -> dict:
    data, done = claim["data"], claim["data"]["definition"]
    obligations = data.get("delivery_obligations") or {}
    allowed = set(authorized_names)
    per_repo: list[dict] = []
    needs_observation: list[tuple[str, dict]] = []
    seen_repos: set[str] = set()
    for target in landed:
        rname = _target_repo_name(target, tk, cfg)
        if not rname or rname not in allowed:
            return {}
        seen_repos.add(rname)
        repo_promise = obligations.get(rname)
        if repo_promise or release_proof.required(rname, claim["issue"]):
            proof = release_proof.for_delivery(rname, claim["issue"], target["commit"], done.get("environment"))
            if not proof:
                return {}
            if repo_promise and (proof.get("status") != "verified" or not proof.get("receipt_id") or any(
                    proof.get(k) != repo_promise[k] for k in ("repo", "ticket", "destination", "path", "policy"))):
                return {}
            per_repo.append({
                "repo": rname,
                "github": target["repo"],
                "branch": target["branch"],
                "commit": target["commit"],
                "pr": target.get("pr", ""),
                "method": "protected release observation",
                "receipt_id": proof["receipt_id"],
                "destination": proof["destination"],
                "path": proof["path"],
                "how": f"Observed {proof['path']} on {proof['destination']} at {proof['revision']}",
                "saw": f"All protected assertions matched. Receipt {proof['receipt_id']}.",
            })
        elif not done.get("environment") and target["exact_tree"]:
            per_repo.append({
                "repo": rname,
                "github": target["repo"],
                "branch": target["branch"],
                "commit": target["commit"],
                "pr": target.get("pr", ""),
                "method": "verified candidate tree matches the landed tree",
                "how": data["behavior"]["how"],
                "saw": data["behavior"]["saw"],
            })
        else:
            needs_observation.append((rname, target))
    if not seen_repos:
        return {}
    source_url = ""
    if needs_observation:
        if not (request := data.get("observation_request")):
            token = orchestration.digest([claim["root"], claim["run"], landed])[:24]
            request = data["observation_request"] = {"landed": landed, "token": token, "since": util.iso()}
            save(claim)
        where = public_text(tk, done.get("environment") or "the landed revision(s) below")
        definition_text = public_text(tk, describe(done))
        post_once(claim, tk, "observation", f"{definition_text}\n\nCandidate verification is not proof at {where}. "
                  f"Please observe the outcome there for these landed revisions:\n\n{location(request['landed'])}\n\n"
                  f"Reply with `Spec evidence {request['token']}` on its own first line, followed by `How:` and `Saw:` "
                  "lines describing how you checked it and what matched the expected outcome. Do not include secrets.")
        matched_obs = None
        for body, url, created in comments(tk, claim["issue"]):
            observed = tracker.spec_observation(body)
            if (observed and observed[0] == request["token"] and url and created
                    and jira.parse_time(created) > util.parse_iso(request["since"])
                    and confirmed_observation(tk, claim["issue"], body, url)):
                matched_obs = (observed[1][:2000], observed[2][:2000], url)
                break
        if not matched_obs:
            return {}
        obs_how, obs_saw, source_url = matched_obs
        for rname, target in needs_observation:
            per_repo.append({
                "repo": rname,
                "github": target["repo"],
                "branch": target["branch"],
                "commit": target["commit"],
                "pr": target.get("pr", ""),
                "method": "publisher's delivery-point observation",
                "how": obs_how,
                "saw": obs_saw,
                "source": source_url,
            })
    all_protected = all(entry["method"] == "protected release observation" for entry in per_repo)
    return {
        "method": "protected release observation" if all_protected else "per-repository release evidence",
        "landed": landed,
        "per_repo": per_repo,
        "how": "; ".join(f"{entry['repo']}: {entry['how']}" for entry in per_repo)[:2000],
        "saw": "; ".join(f"{entry['repo']}: {entry['saw']}" for entry in per_repo)[:2000],
        **({"source": source_url} if source_url else {}),
    }


def at_delivery(claim: dict, tk: tracker.Tracker, ticket: tracker.Ticket | None = None, *,
                cfg: config.Config | None = None) -> dict:
    data = claim["data"]
    if data.get("graph"):
        from . import spec_graph_delivery
        return spec_graph_delivery.integrated(tk, claim)
    landed = reached(data)
    if not landed:
        return {}
    authorized_names = data.get("authorized_repos") or []
    if len(authorized_names) > 1:
        return _multi_repo_delivery(claim, tk, landed, authorized_names, cfg)
    done = data["definition"]
    promise = data.get("delivery_obligation")
    if promise or release_proof.required(tk.repo.name, claim["issue"]):
        if len(landed) != 1 or landed[0]["repo"].casefold() != tk.repo.github.casefold():
            return {}
        proof = release_proof.for_delivery(tk.repo.name, claim["issue"], landed[0]["commit"], done.get("environment"))
        if not proof:
            return {}
        if promise and (proof.get("status") != "verified" or not proof.get("receipt_id") or any(
                proof.get(k) != promise[k] for k in ("repo", "ticket", "destination", "path", "policy"))):
            return {}
        from . import spec_graph_delivery
        policy = release_proof.goal(tk.repo.name, claim["issue"]) or {}
        item = ticket if ticket is not None else tk.get(claim["issue"])
        activations = release_proof.required_activations(policy, item)
        if activations and spec_graph_delivery.activations_verified(activations, receipt=proof):
            return {}
        return {"method": "protected release observation", "landed": landed, "how": f"Observed {proof['path']} "
                f"on {proof['destination']} at {proof['revision']}", "saw": f"All protected assertions matched. "
                f"Receipt {proof['receipt_id']}.",
                **({"activations": activations} if activations else {})}
    if not done.get("environment") and all(t["exact_tree"] for t in landed):
        return {"method": "verified candidate tree matches the landed tree", "landed": landed, **data["behavior"]}
    if not (request := data.get("observation_request")):
        token = orchestration.digest([claim["root"], claim["run"], landed])[:24]
        request = data["observation_request"] = {"landed": landed, "token": token, "since": util.iso()}
        save(claim)
    where = public_text(tk, done.get("environment") or "the landed revision(s) below")
    definition_text = public_text(tk, describe(done))
    post_once(claim, tk, "observation", f"{definition_text}\n\nCandidate verification is not proof at {where}. "
              f"Please observe the outcome there for these landed revisions:\n\n{location(request['landed'])}\n\n"
              f"Reply with `Spec evidence {request['token']}` on its own first line, followed by `How:` and `Saw:` "
              "lines describing how you checked it and what matched the expected outcome. Do not include secrets.")
    from . import spec_graph_delivery
    item = ticket if ticket is not None else tk.get(claim["issue"])
    activations = spec_graph_delivery.activation_requirements(item)
    for body, url, created in comments(tk, claim["issue"]):
        observed = tracker.spec_observation(body)
        if (observed and observed[0] == request["token"] and url and created
                and jira.parse_time(created) > util.parse_iso(request["since"])
                and confirmed_observation(tk, claim["issue"], body, url)):
            if activations and spec_graph_delivery.activations_verified(activations, text=f"{observed[1]}\n{observed[2]}"):
                continue
            return {"method": "publisher's delivery-point observation", "landed": request["landed"],
                    "how": observed[1][:2000], "saw": observed[2][:2000], "source": url,
                    **({"activations": activations} if activations else {})}
    return {}


def settle(cfg, repo, claim: dict) -> None:
    from . import cross_repo

    tid, run, data = claim["issue"], claim["run"], claim["data"]
    if (runs.run_get(run) or {}).get("status") in (None, "running"):
        return
    tk = tracker.for_ticket(cfg, repo, tid)
    saved = {}
    if "source_format" not in data:
        state = orchestration.find(run).get("data", {})
        saved = state.get("prepared", {}).get("ticket", {})
    legacy = (saved.get("id") == tid and data["source"] == orchestration.digest([saved.get("title"), saved.get("body")]))
    t = tk.get(tid, saved_comments=saved.get("comments", "")) if legacy else tk.get(tid)
    if data.get("graph"):
        from . import spec_amendments
        if not spec_amendments.reconcile(tk, claim, t):
            return
    if legacy:
        same_source = t.saved_scope_matches and (t.title, t.body) == (saved["title"], saved["body"])
    else:
        same_source = source_version(t) == data["source"]
    if (t.spec_identity != claim["identity"] or tracker.spec_blocker(cfg, tk, t, require_open=False)
            or not same_source or landing_policy(tk) != data["landing"]):
        return
    if cross_repo.peer_blocker(cfg, repo, t):
        return
    protected = None
    if obligations := data.get("delivery_obligations"):
        if any(ob != obligation(rname, t, data["definition"]) for rname, ob in obligations.items()):
            return
        if not (protected := at_delivery(claim, tk, ticket=t, cfg=cfg)):
            return
        if claim["state"] == "evidenced" and data.get("delivery") != protected:
            return
    elif promise := data.get("delivery_obligation"):
        if promise != obligation(repo.name, t, data["definition"]) or not (protected := at_delivery(claim, tk, ticket=t, cfg=cfg)):
            return
        if claim["state"] == "evidenced" and data.get("delivery") != protected:
            return
    if claim["state"] == "published":
        observed = protected or data.get("delivery") or at_delivery(claim, tk, ticket=t, cfg=cfg)
        if not observed:
            return
        data["delivery"] = observed
        save(claim)
        source = f"\n\nObservation: {observed['source']}" if observed.get("source") else ""
        definition_text = public_text(tk, describe(data["definition"]))
        outcome_text = public_text(tk, f"How: {observed['how']}\n\nSaw: {observed['saw']}")
        extra_sections = []
        if observed.get("per_repo"):
            extra_sections.append(public_text(tk, format_per_repo_evidence(observed["per_repo"])))
        if data.get("consumed_peers"):
            extra_sections.append(public_text(tk, cross_repo.format_consumed_peers(data["consumed_peers"])))
        extra_text = ("\n\n" + "\n\n".join(extra_sections)) if extra_sections else ""
        post_once(claim, tk, "delivery", f"{definition_text}\n\n## Delivery evidence\n\n"
                  f"Reviewed and verified in run `{run}` (spec run `{claim['root']}`). "
                  f"Evidence at delivery: {observed['method']}.\n\n{location(observed['landed'])}\n\n"
                  f"{outcome_text}{extra_text}{source}")
        save(claim, state="evidenced")
    if t.open:
        if isinstance(tk, tracker.GitHubTracker):
            gh.issue_close(repo.github, int(tid))
        elif data["landing"]:
            if error := tk.shift(tid, data["landing"][-1][1], keep_done=True).error:
                raise tracker.TaskError(error)
    save(claim, state="complete")


def sync(cfg) -> None:
    for pending in specs.pending():
        repo_name, tid = pending["repo"], pending["issue"]
        with workspaces.ticket_lock(repo_name, tid) as got:
            if not got:
                continue
            claim = specs.find(repo_name, tid)
            if claim.get("state") not in ("published", "evidenced"):
                continue
            try:
                repo = cfg.repo(repo_name)
                settle(cfg, repo, claim)
            except (KeyError, ValueError, OSError, gh.GhError, jira.JiraError, tracker.TaskError) as error:
                print(f"{util.iso()} {tracker.issue_name(repo_name, tid)}: spec delivery held: "
                      f"{outbound.sanitize(str(error), 300)}")
