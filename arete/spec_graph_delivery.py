"""Dispatch a reviewed graph from its recorded identities and verify the integrated parent."""
from __future__ import annotations

import re

from . import factory, flows, gh, jira, outbound, spec_delivery, spec_tracker, tracker, util, verify
from .store import orchestration, runs, specgraphs, specs

_READINESS_ONLY = object()
PARENT_AMENDMENT_HOLD = "the published parent requirements changed; waiting for the affected child contract"
FEATURE_FLAG_RE = re.compile(r"^(?:feature|activation)\s+flag:\s*(.+)$", re.IGNORECASE)
ACTIVATION_RE = re.compile(r"^(?:audience\s+)?activation:\s*(.+)$", re.IGNORECASE)


def child_landing(repo, ticket, targets):
    run = runs.newest_live_whole_run(repo, ticket) or {}
    if run.get("status") not in ("merged", "done", "pr"):
        return []
    publication = flows.publication(run["id"])
    if not (publication.get("verified") or {}).get("verdict"):
        return []
    observed = verify.Verified.stored(publication["verified"])
    if (observed.verdict != "pass" or not observed.finished or not observed.behavior_reached or not observed.checks or observed.failed_checks
            or publication.get("draft_why") or publication.get("ci_stopped")):
        return []
    branches = {row["repo"].casefold(): row["branch"] for row in targets}
    result = []
    for slug, url in (publication.get("prs") or {}).items():
        branch = branches.get(slug.casefold())
        if not branch or gh.pr_repo(url).casefold() != slug.casefold():
            return []
        pr = gh.pr_view(url)
        merged = (pr.get("mergeCommit") or {}).get("oid", "")
        head = (publication.get("heads") or {}).get(slug)
        if not (head and pr.get("state") == "MERGED" and pr.get("mergedAt") and pr.get("headRefOid") == head
                and pr.get("baseRefName") == branch and spec_delivery.SHA.fullmatch(merged)
                and gh.branch_contains(slug, branch, merged)):
            return []
        result.append({"repo": slug, "branch": branch, "commit": merged, "pr": url})
    return result


def usable(repo, node, mapped, graph, *, released=False, artifact=None):
    from . import release_proof

    ticket = mapped[node][0]
    if contract := graph.get("amended_contracts", {}).get(node):
        from .store.orchestration import digest
        latest = runs.newest_live_whole_run(repo, ticket) or {}
        if not latest or flows.publication(latest["id"]).get("graph_contract") != digest(contract):
            return []
    landed = child_landing(repo, ticket, graph["targets"])
    if artifact or released:
        for target in landed:
            receipt = release_proof.evidence(repo, ticket, target["commit"])
            if not receipt:
                return []
            if artifact:
                fields = {f["field"]: f["value_sha256"] for f in receipt.get("observed", [])}
                if any(fields.get("/" + k) != release_proof.digest(v) for k, v in artifact.items()):
                    return []
    return landed


def satisfied_native(source, child, blockers):
    """An owned dependency may already be usable while its tracker ticket is still open."""
    if not blockers:
        return set()
    remote = spec_tracker.adapter(source)
    key = specgraphs.membership(remote.scope, str(child)) if remote else None
    if not key or key.scope != remote.scope:
        return set()
    saved = specgraphs.get(key, source.repo.name)
    if not saved or not saved["complete"]:
        return set()
    graph, mapped = saved["data"], specgraphs.children(key)
    by_ref = {ref: name for name, (ref, _) in mapped.items()}
    dependent = next((n for n in graph["plan"]["children"] if n["key"] == by_ref.get(str(child))), {})
    return {ref for ref in blockers if (name := by_ref.get(str(ref))) and
            usable(source.repo.name, name, mapped, graph, released=name in dependent.get("release_requires", []),
                   artifact=dependent.get("artifacts", {}).get(name))}


def active_child_blocker(run, ticket=_READINESS_ONLY):
    source = getattr(run, "tracker", None)
    if (not isinstance(source, (tracker.GitHubTracker, tracker.JiraTracker)) or tracker.is_task_id(run.tid)
            or not specgraphs.tracks_ticket(source.repo.name, str(run.tid))):
        return ""
    remote = spec_tracker.adapter(source)
    if not remote or not (key := specgraphs.membership(remote.scope, str(run.tid))):
        return "the native graph child's tracker enrollment could not be confirmed"
    from . import spec_amendments
    if reason := ready(source, remote, key, str(run.tid)):
        return reason
    if ticket is _READINESS_ONLY:
        return ""
    if not isinstance(ticket, tracker.Ticket) or str(ticket.id) != str(run.tid):
        return "the native graph child has no prepared assignment for its current ticket"
    return spec_amendments.before_acceptance(run, ticket)


def require_active_child(run, ticket=None):
    if reason := active_child_blocker(run, ticket):
        raise factory.Refused(reason)


def current_parent(source, remote, key, saved, child=""):
    from . import spec_amendments, spec_graph

    data = saved["data"]
    parent = remote.read(key.parent)
    ticket = source.get(key.parent)
    if (not saved["complete"] or remote.scope != data["destination"] or remote.account() != data["account"]
            or parent.native_id != data["native_id"] or not spec_graph.authorized(ticket, parent)):
        raise spec_tracker.Held("the published parent identity, scope or pickup authority changed")
    if (data.get("pending_amendment") or spec_graph.body_identity(parent) != data["parent"]
            or spec_delivery.source_version(ticket) != data["ticket_version"]):
        if not child or not spec_amendments.unaffected(source, remote, key, saved, parent, ticket, child):
            raise spec_tracker.Held(PARENT_AMENDMENT_HOLD)
    return parent


def ready(source, remote, key, ref):
    if key.scope != remote.scope:
        return "child and parent belong to different enrollment scopes"
    saved = specgraphs.get(key, source.repo.name)
    if not saved or not saved["complete"]:
        return "the parent spec graph is still being published"
    data, mapped = saved["data"], specgraphs.children(key)
    try:
        parent = current_parent(source, remote, key, saved, ref)
        by_ref = {r: name for name, (r, _) in mapped.items()}
        name = by_ref[ref]
        item = remote.read(ref)
        actual = remote.parent(ref)
        if (not actual or actual.native_id != parent.native_id or item.native_id != mapped[name][1]
                or not item.open or {"no-agent", "agent:hold"}.intersection(item.labels)):
            return "the child identity, status or native parent changed"
        node = next(n for n in data["plan"]["children"] if n["key"] == name)
        prerequisites = set(node["requires"])
        for block in remote.blockers(ref, data["policy"]):
            if block in by_ref:
                prerequisites.add(by_ref[block])
            elif remote.read(block).open:
                return f"external prerequisite {block} is still open"
        for dep in sorted(prerequisites):
            released = dep in node.get("release_requires", [])
            artifact = node.get("artifacts", {}).get(dep)
            if not usable(source.repo.name, dep, mapped, data, released=released, artifact=artifact):
                point = f"{artifact['name']} {artifact['version']}" if artifact else "release" if released else "merge"
                return f"waiting for {mapped[dep][0]} to reach its required {point}"
    except (OSError, *spec_tracker.READ_ERRORS) as error:
        return f"the reviewed spec graph could not be confirmed: {error}"
    return ""


def integrated_targets(source, claim):

    remote = spec_tracker.adapter(source)
    key = specgraphs.GraphKey(remote.scope, claim["issue"])
    saved = specgraphs.get(key, source.repo.name)
    parent = current_parent(source, remote, key, saved)
    data, mapped = saved["data"], specgraphs.children(key)
    if {x.native_id for x in remote.children(parent.ref)} != {v[1] for v in mapped.values()}:
        return {}
    targets = data["targets"]
    if not targets:
        return []
    observed = []
    for entry in targets:
        commit, _ = spec_delivery.revision(entry["repo"], entry["branch"])
        observed.append({**entry, "commit": commit, "pr": "", "children": []})
    for node in data["plan"]["children"]:
        landed = usable(source.repo.name, node["key"], mapped, data)
        if not landed:
            return []
        for child in landed:
            target = next((x for x in observed if x["repo"] == child["repo"] and x["branch"] == child["branch"]), None)
            if not target or not gh.branch_contains(target["repo"], target["commit"], child["commit"]):
                return []
            target["children"].append(child["commit"])
    return observed


def promotion_target(repo, ticket):
    from . import config, tracker
    from .store import specs
    claim = specs.find(repo.name, ticket)
    if claim.get("state") not in ("published", "evidenced") or not claim["data"].get("graph"):
        return None
    source = tracker.for_ticket(config.load_required(), repo, ticket)
    targets = integrated_targets(source, claim)
    return next((x for x in targets if x["repo"].casefold() == repo.github.casefold()), {})


def _local_release_verified(repo, ticket, *, artifact=None):
    from . import release_proof, serve_guard

    policy = release_proof.goal(repo, ticket)
    if not policy:
        return False
    want_policy = release_proof.digest(policy)
    root = release_proof.state_dir(repo, ticket)
    if not root.is_dir():
        return False
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir() or not spec_delivery.SHA.fullmatch(candidate.name):
            continue
        receipt = serve_guard.read(candidate / policy["destination"] / "behavior" / "latest.json")
        if not receipt or (receipt.get("status"), receipt.get("policy"), receipt.get("revision")) != (
                "verified", want_policy, candidate.name):
            continue
        if artifact:
            fields = {f["field"]: f["value_sha256"] for f in receipt.get("observed", [])}
            if any(fields.get("/" + k) != release_proof.digest(v) for k, v in artifact.items()):
                continue
        return True
    return False


def _local_child_landed(repo, node, mapped, data):
    if node not in mapped:
        return False, {}
    ticket = mapped[node][0]
    latest = runs.newest_live_whole_run(repo, ticket) or {}
    if latest.get("status") not in ("merged", "done"):
        return False, latest
    if contract := data.get("amended_contracts", {}).get(node):
        if flows.publication(latest["id"]).get("graph_contract") != orchestration.digest(contract):
            return False, latest
    publication = flows.publication(latest["id"])
    if not (publication.get("verified") or {}).get("verdict"):
        return False, latest
    observed = verify.Verified.stored(publication["verified"])
    ok = (observed.verdict == "pass" and observed.finished and observed.behavior_reached
          and bool(observed.checks) and not observed.failed_checks
          and not publication.get("draft_why") and not publication.get("ci_stopped"))
    return ok, latest


def graph_status(repo: str, parent: str, *, skipped: dict | None = None, running: list[dict] | None = None) -> dict | None:
    """Summarize per-child state and wait reasons for a spec graph parent using local state only."""
    found = specgraphs.find_parent(repo, parent)
    if not found:
        return None
    _, saved, mapped = found
    data = saved["data"]
    claim = specs.find(repo, parent)
    replaced = data.get("replaced_nodes", {})
    running_tickets = {(r["repo"], str(r["issue"])) for r in (running or ()) if r.get("issue")}
    skip_map = skipped or {}
    landed_cache: dict[str, tuple[bool, dict]] = {}

    def check_landed(name: str) -> tuple[bool, dict]:
        if name not in landed_cache:
            landed_cache[name] = _local_child_landed(repo, name, mapped, data)
        return landed_cache[name]

    children_rows = []
    for node in data["plan"]["children"]:
        key = node["key"]
        ref = mapped[key][0] if key in mapped else ""
        reqs = []
        first_wait = ""
        for dep in sorted(node["requires"]):
            dep_ref = mapped[dep][0] if dep in mapped else dep
            released = dep in node.get("release_requires", [])
            artifact = node.get("artifacts", {}).get(dep)
            point = f"{artifact['name']} {artifact['version']}" if artifact else "release" if released else "merge"
            dep_landed, dep_run = check_landed(dep)
            satisfied = dep_landed and (not (released or artifact) or _local_release_verified(repo, dep_ref, artifact=artifact))
            reqs.append({"key": dep, "ticket": dep_ref, "need": point, "satisfied": satisfied})
            if not satisfied and not first_wait:
                reason = f"waiting for {dep_ref} to reach its required {point}"
                if dep_run.get("status") in ("failed", "needs-info"):
                    reason += f" ({dep_ref} {dep_run['status']})"
                first_wait = reason
        if key in replaced:
            state, wait_reason = "replaced", ""
        elif ref and (repo, ref) in running_tickets:
            state, wait_reason = "running", ""
        else:
            is_landed, latest = check_landed(key)
            skip_row = skip_map.get((repo, ref), {}) if ref else {}
            if is_landed:
                state, wait_reason = "landed", ""
            elif skip_row.get("why"):
                state, wait_reason = "waiting", outbound.sanitize(skip_row["why"], 300)
            elif not saved["complete"]:
                state, wait_reason = "waiting", "the parent spec graph is still being published"
            elif first_wait:
                state, wait_reason = "waiting", first_wait
            elif latest.get("status") in ("failed", "interrupted", "needs-info", "stopped", "pr"):
                state, wait_reason = latest["status"], ""
            else:
                state, wait_reason = "ready", ""
        children_rows.append({
            "key": key,
            "ticket": ref,
            "title": node["title"],
            "state": state,
            "wait_reason": wait_reason,
            "requires": reqs,
        })
    return {
        "complete": bool(saved["complete"]),
        "state": claim.get("state", ""),
        "children": children_rows,
        "waiting": [{"key": c["key"], "ticket": c["ticket"], "reason": c["wait_reason"]}
                    for c in children_rows if c["wait_reason"]],
    }


def activation_requirements(ticket: tracker.Ticket) -> list[dict]:
    from . import spec_graph

    found = []
    for raw in spec_graph.breakdown_lines(ticket.body or ""):
        line = raw.strip()
        if match := FEATURE_FLAG_RE.fullmatch(line):
            if value := match[1].strip():
                found.append({"kind": "feature_flag", "value": value})
        elif match := ACTIVATION_RE.fullmatch(line):
            if value := match[1].strip():
                found.append({"kind": "activation", "value": value})
    return found


def activations_verified(activations: list[dict], *, receipt: dict | None = None,
                         text: str = "", confirmed: list[str] | None = None) -> list[str]:
    if not activations:
        return []
    from . import release_proof

    confirmed_lower = {str(v).strip().casefold() for v in (confirmed or ()) if str(v).strip()}
    text_lower = (text or "").casefold()
    fields = {f["field"]: f["value_sha256"] for f in (receipt or {}).get("observed", [])}
    missing = []
    for req in activations:
        value = req["value"]
        low = value.casefold()
        matched = low in confirmed_lower or (text_lower and low in text_lower)
        if not matched and fields:
            val_digest = release_proof.digest(value)
            true_digest = release_proof.digest(True)
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
            matched = (fields.get("/" + req["kind"]) == val_digest
                       or fields.get("/activation") == val_digest
                       or fields.get("/feature_flag") == val_digest
                       or (bool(slug) and fields.get("/" + slug) in (val_digest, true_digest)))
        if not matched:
            label = "Feature flag" if req["kind"] == "feature_flag" else "Activation"
            missing.append(f"{label}: {value}")
    return missing


def combined_review_summary(source, saved, mapped, observed) -> tuple[list[dict], str]:
    data = saved["data"]
    replaced = data.get("replaced_nodes", {})
    uses_prs = getattr(source.repo, "landing", "") == "pr"
    entries = []
    seen_prs: dict[tuple[str, ...], str] = {}
    for node in data["plan"]["children"]:
        key = node["key"]
        if key in replaced:
            continue
        ticket = mapped[key][0]
        run = runs.newest_live_whole_run(source.repo.name, ticket) or {}
        publication = flows.publication(run["id"]) if run else {}
        verified_raw = publication.get("verified") or {}
        if not verified_raw.get("verdict"):
            return entries, f"child {ticket} ({key}) has no verified passing checks"
        check_report = verify.Verified.stored(verified_raw)
        if (check_report.verdict != "pass" or not check_report.finished or not check_report.behavior_reached
                or not check_report.checks or check_report.failed_checks
                or publication.get("draft_why") or publication.get("ci_stopped")):
            return entries, f"child {ticket} ({key}) did not pass all required verification checks"
        prs = sorted((publication.get("prs") or {}).values())
        if (uses_prs or prs) and not prs:
            return entries, f"child {ticket} ({key}) has no independently reversible merged PR"
        if prs:
            pr_key = tuple(prs)
            if other := seen_prs.get(pr_key):
                return entries, f"child {ticket} ({key}) shares the same PR set as {other}; changes must be independently reversible"
            seen_prs[pr_key] = key
        entries.append({
            "key": key,
            "ticket": ticket,
            "run": run.get("id", ""),
            "prs": prs,
            "checks": len(check_report.checks),
            "summary": outbound.sanitize(publication.get("summary") or node["outcome"], 200),
        })
    return entries, ""


def format_combined_summary(entries: list[dict], activations: list[dict]) -> str:
    lines = ["## Combined child verification", ""]
    for entry in entries:
        prs = ", ".join(entry["prs"]) if entry["prs"] else "no PR"
        lines.append(f"- `{entry['key']}` ({entry['ticket']}): {entry['checks']} verified check(s), {prs}")
    if activations:
        lines.extend(["", "## Required activation", ""])
        for req in activations:
            label = "Feature flag" if req["kind"] == "feature_flag" else "Activation"
            lines.append(f"- {label}: `{req['value']}`")
    return "\n".join(lines)


def post_unmet_check(claim: dict, source, sha: str, unmet: str) -> str:
    kind = f"unmet:{sha[:12]}:{orchestration.digest(unmet)[:12]}"
    message = spec_delivery.public_text(
        source,
        f"Integrated verification is unmet at `{sha}` — keeping this parent spec open.\n\n"
        f"Unmet check: {unmet}",
    )
    return spec_delivery.post_once(claim, source, kind, message)


def integrated(source, claim):
    from . import release_promotion, release_proof

    observed = integrated_targets(source, claim)
    if not observed:
        return {}
    remote = spec_tracker.adapter(source)
    key = specgraphs.GraphKey(remote.scope, claim["issue"])
    saved = specgraphs.get(key, source.repo.name)
    mapped = specgraphs.children(key)
    parent_ticket = source.get(claim["issue"])
    primary = next((x for x in observed if x["repo"].casefold() == source.repo.github.casefold()), observed[0])
    sha = primary["commit"]
    entries, review_unmet = combined_review_summary(source, saved, mapped, observed)
    if review_unmet:
        post_unmet_check(claim, source, sha, f"Combined cross-child review check failed: {review_unmet}")
        return {}
    activations = activation_requirements(parent_ticket)
    summary_text = format_combined_summary(entries, activations)

    if release_proof.required(source.repo.name, claim["issue"]):
        primary_match = next((x for x in observed if x["repo"].casefold() == source.repo.github.casefold()), None)
        if not primary_match:
            return {}
        owner, ticket, sha = source.repo.name, claim["issue"], primary_match["commit"]
        policy = release_proof.goal(owner, ticket)
        if not policy:
            post_unmet_check(claim, source, sha, "No protected release policy is enrolled for this spec.")
            return {}
        try:
            if policy["destination"] == "arete-serve":
                receipt = release_proof.observe(owner, ticket, sha)
            elif policy["destination"] == "staging":
                receipt = release_promotion.observe_staging(source.repo, ticket, sha)
                if claim["data"]["definition"].get("environment") == "production":
                    receipt = release_proof.completion(owner, ticket, sha)
            else:
                receipt = release_proof.observe(owner, ticket, sha)
        except release_proof.Held as error:
            post_unmet_check(claim, source, sha, f"Integrated release check unmet: {error}")
            return {}
        if not receipt or receipt["status"] != "verified" or receipt["revision"] != sha:
            dest = claim["data"]["definition"].get("environment") or policy["destination"]
            reason = (receipt or {}).get("reason") or f"waiting for verified {dest} release observation"
            post_unmet_check(claim, source, sha, f"Integrated release check failed on {dest} ({policy['path']}): {reason}")
            return {}
        if any(spec_delivery.revision(x["repo"], x["branch"])[0] != x["commit"] for x in observed):
            return {}
        if missing := activations_verified(activations, receipt=receipt):
            post_unmet_check(claim, source, sha, "Required activation not yet verified: " + "; ".join(missing))
            return {}
        return {
            "method": "protected integrated release observation",
            "landed": observed,
            "how": f"Exercised {receipt['path']} on {receipt['destination']} at {sha}",
            "saw": f"Integrated assertions matched. Receipt {receipt['receipt_id']}.\n\n{summary_text}",
            "children": entries,
            **({"activations": activations} if activations else {}),
        }

    # Non-release_proof fallback: verify the assembled target branch and combined child evidence.
    if any(spec_delivery.revision(x["repo"], x["branch"])[0] != x["commit"] for x in observed):
        return {}
    assembled_key = orchestration.digest([(x["repo"], x["branch"], x["commit"]) for x in observed])[:24]
    recorded = claim["data"].get("integrated_verification") or {}
    if recorded.get("assembled") == assembled_key:
        if recorded.get("status") != "pass":
            unmet = recorded.get("unmet") or recorded.get("reason") or "integrated user action check failed"
            post_unmet_check(claim, source, sha, unmet)
            return {}
        if missing := activations_verified(
            activations,
            confirmed=recorded.get("activations", []),
            text=f"{recorded.get('how', '')}\n{recorded.get('saw', '')}",
        ):
            post_unmet_check(claim, source, sha, "Required activation not yet verified: " + "; ".join(missing))
            return {}
        return {
            "method": "assembled target verification",
            "landed": observed,
            "how": str(recorded.get("how") or f"Verified assembled revision {sha}")[:2000],
            "saw": f"{str(recorded.get('saw') or 'Integrated assertions matched.')[:1500]}\n\n{summary_text}",
            "children": entries,
            **({"activations": activations} if activations else {}),
        }

    request = claim["data"].get("observation_request")
    if not request or request.get("assembled") != assembled_key:
        token = orchestration.digest([claim["root"], claim["run"], observed])[:24]
        request = claim["data"]["observation_request"] = {
            "landed": observed,
            "assembled": assembled_key,
            "token": token,
            "since": util.iso(),
        }
        spec_delivery.save(claim)
    done = claim["data"]["definition"]
    where = spec_delivery.public_text(source, done.get("environment") or "the assembled target revision(s) below")
    definition_text = spec_delivery.public_text(source, spec_delivery.describe(done))
    spec_delivery.post_once(
        claim,
        source,
        f"observation:{request['token']}",
        f"{definition_text}\n\n{summary_text}\n\n"
        f"All child checks passed, but child closure and PR merges alone do not verify the assembled outcome at {where}. "
        f"Please observe the integrated outcome for these landed revisions:\n\n{spec_delivery.location(request['landed'])}\n\n"
        f"Reply with `Spec evidence {request['token']}` on its own first line, followed by `How:` and `Saw:` "
        "lines describing how you verified the assembled outcome"
        + (" (including the required feature flag / activation)" if activations else "")
        + ". Do not include secrets.",
    )
    for body, url, created in spec_delivery.comments(source, claim["issue"]):
        replied = tracker.spec_observation(body)
        if (replied and replied[0] == request["token"] and url and created
                and jira.parse_time(created) > util.parse_iso(request["since"])
                and spec_delivery.confirmed_observation(source, claim["issue"], body, url)):
            if missing := activations_verified(activations, text=f"{replied[1]}\n{replied[2]}"):
                post_unmet_check(claim, source, sha, "Required activation not yet verified: " + "; ".join(missing))
                return {}
            return {
                "method": "assembled target observation",
                "landed": request["landed"],
                "how": replied[1][:2000],
                "saw": f"{replied[2][:1500]}\n\n{summary_text}",
                "source": url,
                "children": entries,
                **({"activations": activations} if activations else {}),
            }
    return {}
