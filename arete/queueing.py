from __future__ import annotations

import re
from collections import deque

from . import tracker


def dependencies(ticket: tracker.Ticket) -> set[str]:
    if tracker.is_task_id(ticket.id):
        return set(tracker.waits_on_tasks(ticket.body))
    if ticket.id.isdigit():
        return {str(number) for number in tracker.waits_on(ticket.body)}
    return {key.upper() for line in tracker.WAIT_LINE.findall(ticket.body)
            for key in re.findall(r"\b[A-Z][A-Z0-9_]*-\d+\b", line, re.IGNORECASE)}


def scores(tickets: list[tracker.Ticket]) -> dict[str, tuple[int, int]]:
    dependents = {str(ticket.id): set() for ticket in tickets}
    for ticket in tickets:
        for parent in dependencies(ticket) & dependents.keys():
            dependents[parent].add(str(ticket.id))
    parents = {key: set() for key in dependents}
    remaining = {key: len(children) for key, children in dependents.items()}
    depth = {key: 1 for key in dependents}
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


def prioritized(tickets: list[tracker.Ticket]) -> list[tracker.Ticket]:
    rank = scores(tickets)
    return sorted(tickets, key=lambda ticket: (-rank[str(ticket.id)][0], -rank[str(ticket.id)][1], ticket.created))
