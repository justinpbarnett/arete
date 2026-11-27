from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from . import tracker


def dependencies(ticket: tracker.Ticket) -> set[str]:
    if tracker.is_task_id(ticket.id):
        return set(tracker.waits_on_tasks(ticket.body))
    if ticket.id.isdigit():
        numbers = set(tracker.waits_on(ticket.body))
        numbers.update(ref for ref in ticket.github_blockers if isinstance(ref, int))
        if ticket.github_repo:
            own = ticket.github_repo.casefold()
            numbers.update(number for repo, number in tracker.github_waits(ticket.body, ticket.github_repo) if repo == own)
        return {str(number) for number in numbers}
    return set(tracker.waits_on_jira(ticket.body))


def scores(tickets: list[tracker.Ticket]) -> dict[str, tuple[int, int]]:
    dependents = {str(ticket.id): set() for ticket in tickets}
    for ticket in tickets:
        for parent in dependencies(ticket) & dependents.keys():
            dependents[parent].add(str(ticket.id))
    parents = {key: set() for key in dependents}
    remaining = {key: len(children) for key, children in dependents.items()}
    depth = dict.fromkeys(dependents, 1)
    for parent, children in dependents.items():
        for child in children:
            parents[child].add(parent)
    ready = deque(key for key, count in remaining.items() if not count)
    while ready:
        child = ready.popleft()
        for parent in parents[child]:
            depth[parent] = max(depth[parent], depth[child] + 1)
            remaining[parent] -= 1
            if not remaining[parent]:
                ready.append(parent)
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


def prioritized(tickets: list[tracker.Ticket], *, open_tickets: Iterable[tracker.Ticket] = ()) -> list[tracker.Ticket]:
    graph = {str(ticket.id): ticket for ticket in (*tickets, *open_tickets)}
    rank = scores(list(graph.values()))
    return sorted(tickets, key=lambda ticket: (-rank[str(ticket.id)][0], -rank[str(ticket.id)][1], ticket.created))
