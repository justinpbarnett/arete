from . import orchestration
from .db import SPEC_DELIVERY_SCHEMA, migrate_on_demand, migration_applied, reader, tx

TABLES = {"spec_deliveries": "published specs and their delivery receipts"}


def run_root(run) -> str:
    root = getattr(run, "spec_root", "")
    return root if isinstance(root, str) else ""


def find(repo: str, issue: str, *, identity: str = "") -> dict:
    with reader() as c:
        if not migration_applied(SPEC_DELIVERY_SCHEMA, c):
            return {}
        row = c.execute("SELECT * FROM spec_deliveries WHERE repo = ? AND issue = ?", (repo, issue)).fetchone()
        if row is None and identity:
            row = c.execute("SELECT * FROM spec_deliveries WHERE identity = ?", (identity,)).fetchone()
        return orchestration.decoded(row)


def bind(identity: str, repo: str, issue: str, previous: str, start) -> str:
    migrate_on_demand(SPEC_DELIVERY_SCHEMA)
    with tx() as c:
        claim = find(repo, issue, identity=identity)
        if claim:
            if ((claim["identity"], claim["repo"], claim["issue"], claim["run"]) != (identity, repo, issue, previous)
                    or claim["state"] in ("evidenced", "complete")):
                raise orchestration.Conflict("this spec already has a delivery lineage")
            status = c.execute("SELECT status FROM runs WHERE id = ?", (previous,)).fetchone()
            if status is None or status[0] == "running":
                raise orchestration.Conflict("the previous spec run has not settled")
        run = start()
        if claim:
            c.execute("UPDATE spec_deliveries SET run = ?, state = 'building', data = '{}' WHERE identity = ?",
                      (run, identity))
        else:
            c.execute("INSERT INTO spec_deliveries VALUES (?, ?, ?, ?, ?, 'building', '{}')",
                      (identity, repo, issue, run, run))
        return run


def pending() -> list[dict]:
    with reader() as c:
        if not migration_applied(SPEC_DELIVERY_SCHEMA, c):
            return []
        return [orchestration.decoded(row) for row in c.execute(
            "SELECT * FROM spec_deliveries WHERE state IN ('published', 'evidenced')")]


def answering(repo: str) -> list[str]:
    with reader() as c:
        if not migration_applied(SPEC_DELIVERY_SCHEMA, c):
            return []
        return [row[0] for row in c.execute(
            "SELECT s.issue FROM spec_deliveries s JOIN runs r ON r.id = s.run "
            "WHERE s.repo = ? AND s.state = 'building' AND r.status = 'needs-info'", (repo,))]


def advance(identity: str, run: str, current: str, state: str, data: dict) -> bool:
    with tx() as c:
        return bool(c.execute("UPDATE spec_deliveries SET state = ?, data = ? WHERE identity = ? AND run = ? AND state = ?",
                              (state, orchestration.encoded(data), identity, run, current)).rowcount)
