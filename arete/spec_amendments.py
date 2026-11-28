"""Reconcile owner changes against the contracts of a published native graph."""
from __future__ import annotations

import copy
import re
from dataclasses import replace
from difflib import SequenceMatcher

from . import requirement_revision, spec_delivery, spec_graph, spec_graph_delivery, spec_tracker, tracker
from .store import orchestration as ledger
from .store import specgraphs, specs

KEY = re.compile(r"\bAC-[A-Za-z0-9][\w-]*\b")
PREFIX = re.compile(r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?")


def snapshot(ticket):
    return {"title": ticket.title, "body": ticket.body, "owner_comments": ticket.owner_comments or ""}


def original(saved, key):
    data = saved["data"]
    if base := data.get("accepted_parent"):
        return base
    claim = specs.find(saved["repo"], key.parent) or {}
    entry = ledger.find(claim.get("run", "")) or {}
    prepared = (entry.get("data") or {}).get("prepared", {}).get("ticket")
    if isinstance(prepared, dict):
        try:
            ticket = tracker.Ticket(**prepared)
        except TypeError:
            return {}
        if spec_delivery.source_version(ticket) == data["ticket_version"]:
            return snapshot(ticket)
    return {}


def classify(saved, key, ticket):
    data = saved["data"]
    before = original(saved, key)
    if (not before or before["title"] != ticket.title or before["owner_comments"] != (ticket.owner_comments or "")):
        return None
    old, new = before["body"].splitlines(), ticket.body.splitlines()
    nodes = {n["key"]: n["checks"] for n in data["plan"]["children"] if n["key"] not in data.get("replaced_nodes", {})}
    updated = {}
    for kind, a, b, i, j in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if kind == "equal":
            continue
        # Equal-length replacements preserve check identity; insertions/deletions require explicit mapping.
        if kind != "replace" or b - a != j - i:
            return None
        for previous, latest in zip(old[a:b], new[i:j]):
            previous, latest = (PREFIX.sub("", line, count=1).strip() for line in (previous, latest))
            ids = KEY.findall(previous)
            if not latest or (ids and KEY.findall(latest) != ids):
                return None
            matches = [(name, pos) for name, checks in nodes.items() for pos, check in enumerate(checks)
                       if previous == check or len(ids) == 1 and ids[0] in KEY.findall(check)]
            if len(matches) != 1:
                return None
            name, pos = matches[0]
            updated.setdefault(name, list(nodes[name]))[pos] = latest
    return updated or None


def verified_change(source, remote, key, saved, parent, ticket):
    data = saved["data"]
    if (not saved["complete"] or remote.scope != data["destination"] or remote.account() != data["account"]
            or parent.ref != key.parent or parent.native_id != data["native_id"] or not spec_graph.authorized(ticket, parent)):
        return False
    if isinstance(source, tracker.GitHubTracker) and requirement_revision.github_conflict(source, ticket):
        return False
    return True


def unaffected(source, remote, key, saved, parent, ticket, ref):
    """Read from the tracker during the gap before the next graph poll."""
    if not verified_change(source, remote, key, saved, parent, ticket):
        return False
    updates = classify(saved, key, ticket)
    if not updates:
        return False
    nodes = saved["data"]["plan"]["children"]
    blocked = set(updates)
    if pending := saved["data"].get("pending_amendment"):
        blocked.update(pending["open"])
        blocked.update(pending["corrections"])
        blocked.update(dependent for _, dependent in pending["edges"])
    for name in spec_graph.ordered({n["key"]: set(n["requires"]) for n in nodes}):
        node = next(n for n in nodes if n["key"] == name)
        if blocked.intersection(node["requires"]):
            blocked.add(name)
    return any(r == ref and name in {n["key"] for n in nodes} - blocked for name, (r, _) in specgraphs.children(key).items())


def child_ticket(source, ticket):
    if (not isinstance(source, (tracker.GitHubTracker, tracker.JiraTracker))
            or tracker.is_task_id(ticket.id) or not specgraphs.tracks_ticket(source.repo.name, ticket.id)
            or not (remote := spec_tracker.adapter(source))):
        return ticket
    key = specgraphs.membership(remote.scope, ticket.id)
    if not key:
        return ticket
    saved = specgraphs.get(key, source.repo.name)
    if not saved:
        return ticket
    mapping = specgraphs.children(key)
    for name, contract in saved["data"].get("amended_contracts", {}).items():
        if name in mapping and mapping[name][0] == ticket.id:
            return replace(ticket, graph_body=ticket.body, body=f"{ticket.body}\n\n{contract}")
    return ticket


def before_acceptance(run, ticket):
    """An edit reconciled while this process ran still invalidates its old assignment."""
    if not isinstance(run.tracker, (tracker.GitHubTracker, tracker.JiraTracker)) or not isinstance(ticket, tracker.Ticket):
        return ""
    original_ticket = replace(ticket, body=ticket.graph_body, graph_body=None) if ticket.graph_body is not None else ticket
    if child_ticket(run.tracker, original_ticket).body != ticket.body:
        return "the parent amended this child's reviewed acceptance; resume it against the current requirements"
    return ""


def explicit(source, key, token, names):
    choices = []
    for body, url, _ in spec_delivery.comments(source, key.parent):
        parsed = tracker.graph_resolution(body)
        if parsed and parsed[0] == token and (mapped := parsed[1]).keys() <= names and url and spec_delivery.confirmed_observation(source, key.parent, body, url):
            choices.append(mapped)
    return choices[0] if choices and all(c == choices[0] for c in choices) else None


def diff_checks(name: str, previous: list[str], latest: list[str]) -> list[dict]:
    changed = []
    if len(previous) == len(latest):
        for before, after in zip(previous, latest):
            if before != after:
                changed.append({"node": name, "before": before, "after": after})
        return changed
    old_set, new_set = set(previous), set(latest)
    removed = [c for c in previous if c not in new_set]
    added = [c for c in latest if c not in old_set]
    for before, after in zip(removed, added):
        changed.append({"node": name, "before": before, "after": after})
    for before in removed[len(added):]:
        changed.append({"node": name, "before": before, "after": ""})
    for after in added[len(removed):]:
        changed.append({"node": name, "before": "", "after": after})
    return changed


def plan_amendment(source, remote, key, saved, updates, version):
    data = saved["data"]
    children = specgraphs.children(key)
    plan = copy.deepcopy(data["plan"])
    by_key = {n["key"]: n for n in plan["children"]}
    replaced = data.get("replaced_nodes", {})
    open_nodes, corrections, edges, changed_checks = [], {}, [], []
    for name, checks in sorted(updates.items()):
        node = by_key[name]
        ref, native = children[name]
        item = remote.read(ref)
        if item.native_id != native or not spec_graph.same_parent(remote, ref, remote.read(key.parent)):
            raise spec_tracker.Held(f"the native parent or identity of {ref} changed")
        changed_checks.extend(diff_checks(name, list(node["checks"]), list(checks)))
        if item.open and not spec_graph_delivery.child_landing(source.repo.name, ref, data["targets"]):
            node["checks"] = checks
            open_nodes.append(name)
            continue
        correction = f"fix-{name[:19]}-{ledger.digest([*key, data['ticket_version'], name])[:12]}"
        if correction in by_key:
            raise spec_tracker.Held("the correction key already belongs to a different child")
        new = {**copy.deepcopy(node), "key": correction, "existing": "", "title": f"Correct {ref}: {node['title']}"[:180],
               "outcome": f"Correct the delivered behavior of {ref}: {node['outcome']}"[:1000], "checks": checks}
        plan["children"].append(new)
        corrections[name] = correction
    for name, correction in corrections.items():
        for node in by_key.values():
            if name not in node["requires"] or not remote.read(children[node["key"]][0]).open:
                continue
            node["requires"].append(correction)
            if name in node.get("release_requires", []):
                node["release_requires"].append(correction)
            if artifact := node.get("artifacts", {}).get(name):
                node["artifacts"][correction] = artifact.copy()
            edges.append([correction, node["key"]])
    spec_graph.ordered({n["key"]: set(n["requires"]) for n in plan["children"]})
    active_keys = {n["key"] for n in data["plan"]["children"] if n["key"] not in replaced}
    relinked = {dep for _, dep in edges}
    unaffected_nodes = sorted(active_keys - set(updates) - relinked)
    return {
        "version": version,
        "plan": plan,
        "open": open_nodes,
        "corrections": corrections,
        "edges": edges,
        "changed_checks": changed_checks,
        "unaffected": unaffected_nodes,
    }


def parent_summary(key, version, update, mapped) -> str:
    lines = [f"Reconciled parent spec amendment `{version[:12]}` for {key.parent}."]
    if changed := update.get("changed_checks"):
        lines.extend(["", "### Changed acceptance checks", ""])
        for item in changed:
            if item["before"] and item["after"]:
                lines.append(f"- `{item['node']}`: `{item['before']}` -> `{item['after']}`")
            elif item["after"]:
                lines.append(f"- `{item['node']}` (added): `{item['after']}`")
            else:
                lines.append(f"- `{item['node']}` (removed): `{item['before']}`")
    if update["open"]:
        affected = ", ".join(f"{mapped[n][0]} (`{n}`)" if n in mapped else f"`{n}`" for n in update["open"])
        lines.append(f"\nAffected open children redirected: {affected}.")
    if update["corrections"]:
        fixes = ", ".join(
            f"{mapped[orig][0]} (`{orig}`) -> {mapped[fix][0]} (`{fix}`)"
            if orig in mapped and fix in mapped else f"`{orig}` -> `{fix}`"
            for orig, fix in sorted(update["corrections"].items())
        )
        lines.append(f"\nCorrection children created (completed tickets stayed closed): {fixes}.")
    if update["edges"]:
        relinked = ", ".join(
            f"{mapped[dep][0]} (`{dep}`) now waits on {mapped[fix][0]} (`{fix}`)"
            if dep in mapped and fix in mapped else f"`{dep}` -> `{fix}`"
            for fix, dep in update["edges"]
        )
        lines.append(f"\nPrerequisites added to open dependents: {relinked}.")
    if untouched := update.get("unaffected"):
        kept = ", ".join(f"{mapped[n][0]} (`{n}`)" if n in mapped else f"`{n}`" for n in untouched)
        lines.append(f"\nUnaffected children continuing unchanged: {kept}.")
    return "\n".join(lines)


def note(publication, operation, ref, body):
    publication.operation(operation, lambda token: publication.remote.commented(ref, token, publication.account),
                          lambda token: publication.remote.comment(ref, body, token))


def reconcile(source, claim, ticket):
    remote = spec_tracker.adapter(source)
    key = specgraphs.GraphKey(remote.scope, claim["issue"])
    saved = specgraphs.get(key, source.repo.name)
    if not saved or not saved["complete"]:
        return False
    data = saved["data"]
    parent = remote.read(key.parent)
    version = spec_delivery.source_version(ticket)
    if not data.get("pending_amendment") and version == data["ticket_version"] and spec_graph.body_identity(parent) == data["parent"]:
        return True
    if claim["state"] != "published" or not verified_change(source, remote, key, saved, parent, ticket):
        return False

    def current():
        fresh = source.get(key.parent)
        return (spec_delivery.source_version(fresh) == version and remote.read(key.parent) == parent
                and verified_change(source, remote, key, saved, parent, fresh))

    publication = spec_graph.Publication(remote, parent, saved, current)
    update = data.get("pending_amendment")
    if not update or update["version"] != version:
        changes = classify(saved, key, ticket)
        token = ledger.digest([*key, data["ticket_version"], version])[:24]
        names = {n["key"] for n in data["plan"]["children"]} - data.get("replaced_nodes", {}).keys()
        if changes is None or update:
            chosen = explicit(source, key, token, names)
            if chosen is not None:
                changes = chosen
        proposed = plan_amendment(source, remote, key, saved, changes, version) if changes is not None else None
        if update and proposed and (not update["corrections"].items() <= proposed["corrections"].items()
                                    or not {tuple(e) for e in update["edges"]} <= {tuple(e) for e in proposed["edges"]}):
            proposed = None
        if proposed is None:
            text = (f"[arete] Which reviewed children does this parent change affect? Reply with "
                    f"`Graph amendment {token}` on the first line, then one `child-key: current acceptance check` "
                    f"line per check. Give the complete acceptance checks for each affected child, or write "
                    f"`No child acceptance changes`. The current keys are {', '.join(sorted(names))}.")
            if update and update["corrections"]:
                text += (" A native correction may already exist for " + ", ".join(sorted(update["corrections"]))
                         + ". Include the current checks for that work so its existing ticket can be reused.")
            note(publication, f"clarify:{token}", key.parent, text)
            return False
        update = proposed
        staged = {**data, "pending_amendment": update}
        specgraphs.amend(key, data, staged)
        data = staged
    nodes = {n["key"]: n for n in update["plan"]["children"]}
    for name in update["corrections"].values():
        publication.create(nodes[name])
    mapped = specgraphs.children(key)
    for name in update["corrections"].values():
        ref = mapped[name][0]
        child = remote.read(ref)
        publication.attach(name, child)
        for dep in nodes[name]["requires"]:
            publication.block(name, dep, child, remote.read(mapped[dep][0]))
    for dep, dependent in update["edges"]:
        publication.block(dependent, dep, remote.read(mapped[dependent][0]), remote.read(mapped[dep][0]))
    contracts = dict(data.get("amended_contracts", {}))
    for name in [*update["open"], *update["corrections"].values()]:
        contract = (f"Current acceptance from parent {key.parent} ({version[:12]}). "
                    f"This replaces the earlier child checks.\n\n{spec_tracker.contract(nodes[name])}")
        note(publication, f"amend:{version}:{name}", mapped[name][0], f"[arete] {contract}")
        contracts[name] = contract
    for name in update["corrections"].values():
        publication.enable(name, remote.read(mapped[name][0]))
    if update["open"] or update["corrections"] or update.get("changed_checks"):
        note(publication, f"parent-amend:{version}", key.parent,
             f"[arete] {parent_summary(key, version, update, mapped)}")
    if not current():
        raise spec_tracker.Held("parent authority changed during native amendment publication")
    updated = {**data, "plan": update["plan"], "accepted_parent": snapshot(ticket), "ticket_version": version,
               "parent": spec_graph.body_identity(parent), "amended_contracts": contracts,
               "replaced_nodes": {**data.get("replaced_nodes", {}), **update["corrections"]},
               "amendments": [*data.get("amendments", []), {
                   "version": version,
                   "open": update["open"],
                   "corrections": update["corrections"],
                   "changed_checks": update.get("changed_checks", []),
                   "unaffected": update.get("unaffected", []),
               }]}
    updated.pop("pending_amendment", None)
    specgraphs.amend(key, data, updated, claim=claim)
    return False  # The next settlement reads the updated parent and child receipts together.
