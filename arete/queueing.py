from __future__ import annotations

from collections.abc import Iterable, Iterator

from . import tracker


def dependencies(ticket: tracker.Ticket, *, github_repo: str | None = None) -> set[str]:
    if tracker.is_task_id(ticket.id):
        return set(tracker.waits_on_tasks(ticket.body))
    if ticket.id.isdigit():
        if github_repo is not None:
            own = github_repo.casefold()
            return {str(number) for repo, number in tracker.github_waits(ticket.body, own) if repo == own}
        return {str(number) for number in tracker.waits_on(ticket.body)}
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


def scores(tickets: list[tracker.Ticket], *, open_tickets: Iterable[tracker.Ticket] = (),
           github_repo: str | None = None) -> dict[str, tuple[int, int]]:
    graph = {str(ticket.id): ticket for ticket in tickets}
    graph.update((str(ticket.id), ticket) for ticket in open_tickets)
    parents = {tid: dependencies(ticket, github_repo=github_repo) & graph.keys() for tid, ticket in graph.items()}
    dependents = {tid: set() for tid in graph}
    for child, prerequisites in parents.items():
        for parent in prerequisites:
            dependents[parent].add(child)
    depth = dict.fromkeys(graph, 1)
    for component in _topological_components(parents, dependents):
        level = max(depth[tid] for tid in component)
        for tid in component:
            depth[tid] = level
            for parent in parents[tid] - component:
                depth[parent] = max(depth[parent], level + 1)
    result = {}
    for key, children in dependents.items():
        seen, pending = {key}, list(children)
        while pending:
            child = pending.pop()
            if child not in seen:
                seen.add(child)
                pending.extend(dependents[child] - seen)
        result[key] = (depth[key], len(seen) - 1)
    return result


def prioritized(tickets: list[tracker.Ticket], *, open_tickets: Iterable[tracker.Ticket] = (),
                github_repo: str | None = None) -> list[tracker.Ticket]:
    rank = scores(tickets, open_tickets=open_tickets, github_repo=github_repo)
    return sorted(tickets, key=lambda ticket: (-rank[str(ticket.id)][0], -rank[str(ticket.id)][1], ticket.created))
