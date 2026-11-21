from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator

from . import tracker


def dependencies(ticket: tracker.Ticket, *, github_repo: str | None = None) -> set[str]:
    if tracker.is_task_id(ticket.id):
        return set(tracker.waits_on_tasks(ticket.body))
    if ticket.id.isdigit():
        numbers = set(tracker.waits_on(ticket.body))
        if github_repo is not None:
            own = github_repo.casefold()
            numbers.update(number for repo, number in tracker.github_waits(ticket.body, github_repo) if repo == own)
        return {str(number) for number in numbers}
    return set(tracker.waits_on_jira(ticket.body))


def _topological_components(parents: dict[str, set[str]], dependents: dict[str, set[str]]) -> Iterator[set[str]]:
    finished = []
    seen = set()
    for start in parents:
        if start in seen:
            continue
        seen.add(start)
        pending = [(start, iter(parents[start]))]
        while pending:
            tid, prerequisites = pending[-1]
            parent = next(prerequisites, None)
            if parent is None:
                finished.append(tid)
                pending.pop()
            elif parent not in seen:
                seen.add(parent)
                pending.append((parent, iter(parents[parent])))

    assigned = set()
    for start in reversed(finished):
        if start in assigned:
            continue
        component, pending_tickets = {start}, [start]
        assigned.add(start)
        while pending_tickets:
            found = dependents[pending_tickets.pop()] - assigned
            component.update(found)
            assigned.update(found)
            pending_tickets.extend(found)
        yield component


def _depths_from_leaves(parents: dict[str, set[str]], dependents: dict[str, set[str]]) -> dict[str, int]:
    remaining = {tid: len(children) for tid, children in dependents.items()}
    depth = dict.fromkeys(parents, 1)
    ready = deque(tid for tid, count in remaining.items() if not count)
    while ready:
        child = ready.popleft()
        for parent in parents[child]:
            depth[parent] = max(depth[parent], depth[child] + 1)
            remaining[parent] -= 1
            if not remaining[parent]:
                ready.append(parent)
    return depth


def _expanded_depths(parents: dict[str, set[str]], dependents: dict[str, set[str]]) -> dict[str, int]:
    depth = dict.fromkeys(parents, 1)
    for component in _topological_components(parents, dependents):
        level = max(depth[tid] for tid in component)
        for tid in component:
            depth[tid] = level
            for parent in parents[tid] - component:
                depth[parent] = max(depth[parent], level + 1)
    return depth


def scores(tickets: list[tracker.Ticket], *, open_tickets: Iterable[tracker.Ticket] = (),
           github_repo: str | None = None) -> dict[str, tuple[int, int]]:
    graph = {str(ticket.id): ticket for ticket in tickets}
    candidates = set(graph)
    graph.update((str(ticket.id), ticket) for ticket in open_tickets)
    unlisted = graph.keys() - candidates
    parents = {tid: dependencies(ticket, github_repo=github_repo) & graph.keys() for tid, ticket in graph.items()}
    dependents = {tid: set() for tid in graph}
    for child, prerequisites in parents.items():
        for parent in prerequisites:
            dependents[parent].add(child)
    depth = _depths_from_leaves(parents, dependents)
    expanded_depth = _expanded_depths(parents, dependents) if unlisted else depth
    result = {}
    for key, children in dependents.items():
        seen, pending = {key}, list(children)
        while pending:
            child = pending.pop()
            if child not in seen:
                seen.add(child)
                pending.extend(dependents[child] - seen)
        levels = depth if unlisted.isdisjoint(seen) else expanded_depth
        result[key] = (levels[key], len(seen) - 1)
    return result


def prioritized(tickets: list[tracker.Ticket], *, open_tickets: Iterable[tracker.Ticket] = (),
                github_repo: str | None = None) -> list[tracker.Ticket]:
    rank = scores(tickets, open_tickets=open_tickets, github_repo=github_repo)
    return sorted(tickets, key=lambda ticket: (-rank[str(ticket.id)][0], -rank[str(ticket.id)][1], ticket.created))
