from __future__ import annotations

import json
import time
import uuid

from .. import util
from .db import db, reader, tx

TABLES = {"unblock_cases": "kept", "unblock_attempts": "kept", "unblock_notices": "kept", "unblock_queue": "kept"}


def save_queue(repo: str, source: str, snapshot: dict) -> None:
    with db() as c:
        c.execute("INSERT INTO unblock_queue VALUES (?,?,?) ON CONFLICT(repo, tracker) DO UPDATE SET "
                  "snapshot=excluded.snapshot WHERE snapshot<>excluded.snapshot", (repo, source, encoded(snapshot)))


def queue(repo: str) -> dict:
    with reader() as c:
        return {r["tracker"]: json.loads(r["snapshot"]) for r in c.execute(
            "SELECT * FROM unblock_queue WHERE repo=?", (repo,))}


def candidates(repo: str, limit: int, offset: int) -> list[dict]:
    with reader() as c:
        return [dict(row) for row in c.execute("""SELECT r.* FROM runs r WHERE r.repo=? AND r.kind='pipeline'
            AND r.status IN ('failed','pr','needs-info') AND r.dry_run=0 AND COALESCE(r.only_step,'')=''
            AND NOT EXISTS (SELECT 1 FROM runs later WHERE later.repo=r.repo AND later.issue=r.issue
              AND later.kind='pipeline' AND later.dry_run=0 AND COALESCE(later.only_step,'')='' AND later.rowid>r.rowid)
            ORDER BY (r.status='needs-info') DESC, r.rowid DESC LIMIT ? OFFSET ?""", (repo, limit, offset))]


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def cases(repo: str = "", *, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM unblock_cases WHERE phase NOT IN ('resolved', 'cancelled')"  # noqa: S608
            + (" AND repo = ?" if repo else "") + " ORDER BY updated", (repo,) if repo else ())]


def get(repo: str, ticket: str, *, connect=reader) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM unblock_cases WHERE repo = ? AND ticket = ?", (repo, ticket)).fetchone()
    return dict(row) if row else {}


def follow(repo: str, ticket: str, run: str) -> bool:
    with tx() as c:
        result = c.execute("""INSERT INTO unblock_cases
                  (repo,ticket,source,cause,fingerprint,generation,snapshot,phase,detail,updated)
                  VALUES (?,?,?,'goal','monitor',1,'{}','resuming','Following delivery',?)
                  ON CONFLICT(repo,ticket) DO UPDATE SET source=excluded.source, phase='resuming',
                  detail=excluded.detail, due=0, updated=excluded.updated
                  WHERE unblock_cases.phase IN ('resolved','cancelled')""", (repo, ticket, run, util.iso()))
        return bool(result.rowcount)


def observe(repo: str, ticket: str, cause: str, fingerprint: str, snapshot: dict) -> dict:
    with tx() as c:
        current = get(repo, ticket, connect=db)
        binding = encoded(snapshot)
        if (current.get("snapshot") != binding or current.get("fingerprint") != fingerprint
                or current.get("phase") in ("resolved", "cancelled")):
            c.execute("""INSERT INTO unblock_cases (repo, ticket, source, cause, fingerprint, generation,
                            snapshot, phase, updated) VALUES (?, ?, ?, ?, ?, 1, ?, 'queued', ?)
                         ON CONFLICT(repo, ticket) DO UPDATE SET source=excluded.source, cause=excluded.cause,
                            fingerprint=excluded.fingerprint, generation=generation+1, snapshot=excluded.snapshot,
                            phase='queued', detail='', due=0, updated=excluded.updated""",
                      (repo, ticket, snapshot["run"], cause, fingerprint, binding, util.iso()))
        return get(repo, ticket, connect=db)


def mark(case: dict, phase: str, detail: str = "", delay: float = 0) -> bool:
    with db() as c:
        return bool(c.execute("UPDATE unblock_cases SET phase=?, detail=?, due=?, updated=? "
                              "WHERE repo=? AND ticket=? AND generation=?",
                              (phase, detail, time.time() + delay, util.iso(),
                               case["repo"], case["ticket"], case["generation"])).rowcount)


def begin(case: dict) -> str:
    attempt = uuid.uuid4().hex
    with tx() as c:
        c.execute("INSERT INTO unblock_attempts (id, repo, ticket, generation, fingerprint, observed) "
                  "VALUES (?,?,?,?,?,?)", (attempt, case["repo"], case["ticket"], case["generation"],
                                           case["fingerprint"], util.iso()))
        mark(case, "diagnosing")
    return attempt


def launched(attempt: str) -> bool:
    with tx() as c:
        return bool(c.execute("UPDATE unblock_attempts SET launched=launched+1 WHERE id=?", (attempt,)).rowcount)


def decide(case: dict, attempt: str, decision: dict, grant: dict) -> None:
    with tx() as c:
        c.execute("UPDATE unblock_attempts SET decision=?, grant=? WHERE id=?",
                  (encoded(decision), encoded(grant), attempt))
        mark(case, "queued", f"Proposed {decision['action']}")


def decode(row) -> dict:
    return {**dict(row), "grant": json.loads(row["grant"]), "decision": json.loads(row["decision"])} if row else {}


def attempt_of(attempt: str, *, connect=reader) -> dict:
    with connect() as c:
        return decode(c.execute("SELECT * FROM unblock_attempts WHERE id=?", (attempt,)).fetchone())


def latest_attempt(case: dict) -> dict:
    with reader() as c:
        return decode(c.execute("SELECT * FROM unblock_attempts WHERE repo=? AND ticket=? AND generation=? "
                                "ORDER BY rowid DESC LIMIT 1",
                                (case["repo"], case["ticket"], case["generation"])).fetchone())


def register(case: dict, attempt: str, run: str) -> None:
    with tx() as c:
        current = get(case["repo"], case["ticket"], connect=db)
        if current.get("generation") != case["generation"]:
            raise ValueError("recovery grant no longer available")
        if not c.execute("UPDATE unblock_attempts SET child=?, used=? WHERE id=? AND child=''",
                         (run, util.iso(), attempt)).rowcount:
            raise ValueError("recovery grant already used")
        mark(case, "resuming", f"Continued in {run}")


def posted(attempt: dict) -> None:
    with db() as c:
        c.execute("UPDATE unblock_attempts SET grant=? WHERE id=? AND child=?",
                  (encoded({**attempt["grant"], "public_posted": True}), attempt["id"], attempt["child"]))


def for_run(run: str) -> dict:
    with reader() as c:
        return decode(c.execute("SELECT * FROM unblock_attempts WHERE child=?", (run,)).fetchone())


def notice(run: str) -> bool:
    with db() as c:
        return bool(c.execute("INSERT OR IGNORE INTO unblock_notices VALUES (?,?)", (run, util.iso())).rowcount)


def notified_at(run: str) -> str:
    with reader() as c:
        row = c.execute("SELECT created FROM unblock_notices WHERE run=?", (run,)).fetchone()
        return row[0] if row else ""
