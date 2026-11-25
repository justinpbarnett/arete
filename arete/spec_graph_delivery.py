"""Dispatch a reviewed graph from its recorded identities and verify the integrated parent."""
from __future__ import annotations

from . import flows, gh, spec_delivery, spec_tracker, verify
from .store import runs, specgraphs


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


def current_parent(source, remote, key, saved):
    from . import spec_graph

    data = saved["data"]
    parent = remote.read(key.parent)
    ticket = source.get(key.parent)
    if (not saved["complete"] or remote.scope != data["destination"] or remote.account() != data["account"]
            or parent.native_id != data["native_id"] or spec_graph.body_identity(parent) != data["parent"]
            or spec_delivery.source_version(ticket) != data["ticket_version"] or not spec_graph.authorized(ticket, parent)):
        raise spec_tracker.Held("the published parent identity, scope or pickup authority changed")
    return parent


def ready(source, remote, key, ref):
    if key.scope != remote.scope:
        return "child and parent belong to different enrollment scopes"
    saved = specgraphs.get(key, source.repo.name)
    if not saved or not saved["complete"]:
        return "the parent spec graph is still being published"
    data, mapped = saved["data"], specgraphs.children(key)
    try:
        parent = current_parent(source, remote, key, saved)
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


def integrated(source, claim):
    from . import release_promotion, release_proof
    observed = integrated_targets(source, claim)
    if not release_proof.required(source.repo.name, claim["issue"]):
        return {}
    primary = next((x for x in observed if x["repo"].casefold() == source.repo.github.casefold()), None)
    if not primary:
        return {}
    owner, ticket, sha = source.repo.name, claim["issue"], primary["commit"]
    policy = release_proof.goal(owner, ticket)
    if policy["destination"] == "arete-serve":
        receipt = release_proof.observe(owner, ticket, sha)
    elif policy["destination"] == "staging":
        receipt = release_promotion.observe_staging(source.repo, ticket, sha)
        if claim["data"]["definition"].get("environment") == "production":
            receipt = release_proof.completion(owner, ticket, sha)
    else:
        receipt = release_proof.observe(owner, ticket, sha)
    if not receipt or receipt["status"] != "verified" or receipt["revision"] != sha:
        return {}
    if any(spec_delivery.revision(x["repo"], x["branch"])[0] != x["commit"] for x in observed):
        return {}
    return {"method": "protected integrated release observation", "landed": observed,
            "how": f"Exercised {receipt['path']} on {receipt['destination']} at {sha}",
            "saw": f"Integrated assertions matched. Receipt {receipt['receipt_id']}."}
