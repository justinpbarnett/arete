from __future__ import annotations

import sqlite3
from enum import Enum, auto
from typing import NamedTuple

from . import db, orchestration

TABLES = {"spec_graphs": "kept", "spec_graph_children": "kept", "spec_graph_writes": "kept"}


class Conflict(ValueError):
    pass


class GraphKey(NamedTuple):
    scope: str
    parent: str


class Receipt(Enum):
    MISSING = auto()
    PREPARED = auto()
    DISPATCHED = auto()
    RECEIVED = auto()


def installed(c):
    return c.execute("SELECT 1 FROM sqlite_master WHERE name = 'spec_graphs' AND type = 'table'").fetchone()


def get(key: GraphKey, repo):
    with db.reader() as c:
        if not installed(c):
            return None
        row = c.execute("SELECT * FROM spec_graphs WHERE scope = ? AND parent = ?", key).fetchone()
        previous = c.execute("SELECT scope FROM spec_graphs WHERE repo = ? AND parent = ?", (repo, key.parent)).fetchone()
        if previous and previous["scope"] != key.scope:
            raise Conflict("the configured tracker changed for a saved spec graph; restore the original destination")
        return orchestration.decoded(row) if row else None


def destination(scope, ticket):
    if scope.startswith("github:") and "#" in ticket:
        repo, number = ticket.split("#", 1)
        return f"github:{repo}", number
    return scope, ticket


def owner(c, scope, ticket):
    return c.execute("SELECT scope, parent FROM spec_graph_children WHERE (scope = ? AND ticket = ?) "
                     "OR (? AND scope LIKE 'github:%' AND ticket = ?)",
                     (scope, ticket, scope.startswith("github:"),
                      f"{scope.removeprefix('github:')}#{ticket}")).fetchone()


def membership(scope, ticket):
    with db.reader() as c:
        row = owner(c, scope, ticket) if installed(c) else None
        return GraphKey(*row) if row else None


def launch_blocker(scope, ticket):
    with db.reader() as c:
        if not installed(c):
            return ""
        if row := owner(c, scope, ticket):
            parent = row["parent"]
            if row["scope"] != scope:
                parent = f"{row['scope'].removeprefix('github:')}#{parent}"
            return f"{ticket} is owned by the spec graph of {parent}"
        if c.execute("SELECT 1 FROM spec_graphs WHERE scope = ? AND parent = ? AND complete = 1",
                     (scope, ticket)).fetchone():
            return f"{ticket} already has a published spec graph"
        return ""


def unfinished(repo, parent):
    with db.reader() as c:
        return bool(installed(c) and c.execute("SELECT 1 FROM spec_graphs WHERE repo = ? AND parent = ? AND complete = 0",
                                               (repo, parent)).fetchone())


def bind(c, key: GraphKey, node, ticket, native_id):
    scope, ref = destination(key.scope, ticket)
    if c.execute("SELECT 1 FROM spec_graphs WHERE scope = ? AND parent = ?", (scope, ref)).fetchone():
        raise Conflict(f"{ticket} already owns a spec graph")
    prior = c.execute("SELECT ticket, native_id FROM spec_graph_children WHERE scope = ? AND parent = ? AND node = ?",
                      (*key, node)).fetchone()
    if prior:
        if tuple(prior) != (ticket, native_id):
            raise Conflict(f"{node} already has a different tracker identity")
        return
    if owner(c, scope, ref):
        raise Conflict(f"{ticket} already belongs to another spec graph")
    try:
        c.execute("INSERT INTO spec_graph_children VALUES (?, ?, ?, ?, ?)", (*key, node, ticket, native_id))
    except sqlite3.IntegrityError as error:
        raise Conflict(f"{ticket} already belongs to another spec graph") from error


def accept(key: GraphKey, repo, data, existing):
    with db.tx() as c:
        if saved := get(key, repo):
            return saved
        if launch_blocker(key.scope, key.parent):
            raise Conflict("a spec-owned child cannot publish a second graph")
        c.execute("INSERT INTO spec_graphs (scope, parent, repo, data) VALUES (?, ?, ?, ?)",
                  (*key, repo, orchestration.encoded(data)))
        for node, ticket, native_id in existing:
            bind(c, key, node, ticket, native_id)
        return get(key, repo)


def children(key):
    with db.reader() as c:
        rows = c.execute("SELECT node, ticket, native_id FROM spec_graph_children WHERE scope = ? AND parent = ?", key)
        return {row["node"]: (row["ticket"], row["native_id"]) for row in rows}


def receipt(key: GraphKey, operation) -> Receipt:
    with db.reader() as c:
        row = c.execute("SELECT received, dispatched FROM spec_graph_writes "
                        "WHERE scope = ? AND parent = ? AND operation = ?", (*key, operation)).fetchone()
        if row is None:
            return Receipt.MISSING
        if row["received"]:
            return Receipt.RECEIVED
        return Receipt.DISPATCHED if row["dispatched"] else Receipt.PREPARED


def prepare(key, operation):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO spec_graph_writes (scope, parent, operation, dispatched) VALUES (?, ?, ?, 0)",
                  (*key, operation))


def dispatch(key, operation):
    with db.tx() as c:
        return c.execute("UPDATE spec_graph_writes SET dispatched = 1 WHERE scope = ? AND parent = ? AND operation = ? "
                         "AND received = 0 AND dispatched = 0", (*key, operation)).rowcount == 1


def rejected(key, operation):
    with db.tx() as c:
        c.execute("DELETE FROM spec_graph_writes WHERE scope = ? AND parent = ? AND operation = ? "
                  "AND received = 0 AND dispatched = 1", (*key, operation))


def receive(key, operation, child=None):
    with db.tx() as c:
        if child:
            bind(c, key, *child)
        c.execute("INSERT INTO spec_graph_writes (scope, parent, operation, received) VALUES (?, ?, ?, 1) "
                  "ON CONFLICT DO UPDATE SET received = 1", (*key, operation))


def unconfirmed(key):
    with db.reader() as c:
        return bool(installed(c) and c.execute("SELECT 1 FROM spec_graph_writes WHERE scope = ? AND parent = ? "
                                               "AND received = 0", key).fetchone())


def finish(key):
    with db.tx() as c:
        if unconfirmed(key):
            raise Conflict("the spec graph still has an unconfirmed tracker write")
        c.execute("UPDATE spec_graphs SET complete = 1 WHERE scope = ? AND parent = ?", key)
