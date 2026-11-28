from __future__ import annotations

import json

from .. import owner_asks, util
from ._rows import json_dict, json_list
from .db import db, reader

# Ignore a question from a ticket that has since started another whole run.
LATEST = """NOT EXISTS (SELECT 1 FROM runs a, runs later
    WHERE a.id = asks.run AND later.kind = 'pipeline' AND later.repo = a.repo AND later.issue = a.issue
      AND COALESCE(later.only_step, '') = '' AND NOT later.dry_run AND later.rowid > a.rowid)"""

TABLES = {
    "asks": "kept",
    "messages": "kept",
}


def add_message(run_id: str, text: str) -> None:
    with db() as c:
        c.execute("INSERT INTO messages (run_id, text, ts) VALUES (?,?,?)", (run_id, text, util.iso()))


def take_messages(run_id: str) -> list[str]:
    with db() as c:
        rows = c.execute("SELECT id, text FROM messages WHERE run_id = ? AND taken IS NULL ORDER BY id",
                         (run_id,)).fetchall()
        c.executemany("UPDATE messages SET taken = ? WHERE id = ?", [(util.iso(), r["id"]) for r in rows])
        return [r["text"] for r in rows]


def ask_row(r) -> dict:
    return {**dict(r), "questions": json_list(r["questions"]), "session": json_dict(r["session"]),
            "answers": json_list(r["answers"]), "staged_answers": json_dict(r["staged_answers"])}


def _sync_inbox(c, *sources: str) -> None:
    from .. import inbox

    for source in sources:
        inbox.sync(c, source)


def ask_add(ask_id: str, run: str, repo: str, ticket: str, step: str, call: str, why: str, tried: str,
            questions: list[dict], session: dict, *, recovery_route: str = "owner") -> bool:
    with db() as c:
        first = call != owner_asks.CHECK_RUNNER_CALL or c.execute(
            f"SELECT 1 FROM asks WHERE status='open' AND call=? AND {LATEST} LIMIT 1", (call,)).fetchone() is None
        c.execute("INSERT OR REPLACE INTO asks (id, run, repo, ticket, step, call, why, tried, questions, asked, "
                  "session, recovery_route) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ask_id, run, repo, str(ticket), step, call, why, tried, json.dumps(questions), util.iso(),
                   json.dumps(session), recovery_route))
        _sync_inbox(c, "asks", "runs")
        return first


def ask_get(ask_id: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        return ask_row(r) if r else None


def individual_scope_notes(runs, notes):
    """Recover separately answered questions from historical notes using their actual stored questions and answers."""
    if not any("\n2. " in note for note in notes):
        return notes
    groups = {}
    expanded = {}
    for run in runs:
        for row in asks_of(run, "answered"):
            expanded[owner_asks.fresh_note([row])] = owner_asks.decision_notes([row])
            groups.setdefault(row["taken_by"], []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda r: (r["asked"], r["id"]))
        expanded[owner_asks.fresh_note(rows)] = owner_asks.decision_notes(rows)
    return [part for note in notes for part in expanded.get(note, [note])]


def carry_scope_ask(repo: str, ticket: str, call: str, run: str) -> dict | None:
    with db() as c:
        row = c.execute("SELECT * FROM asks WHERE repo=? AND ticket=? AND call=? LIMIT 1",
                        (repo, str(ticket), call)).fetchone()
        if row and row["status"] == "open":
            # Keep the original ID, date, session and notification, with a live continuation for the answer.
            c.execute("UPDATE asks SET run=? WHERE id=?", (run, row["id"]))
        return ask_row(row) if row else None


def asks_of(run: str, status: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        return [ask_row(r) for r in c.execute(
            "SELECT * FROM asks WHERE run = ?" + (" AND status = ?" if status else "") + " ORDER BY asked, rowid",
            (run, status) if status else (run,))]


def routes_of(runs: list[str], *, connect=reader) -> dict[str, str]:
    if not runs:
        return {}
    places = ",".join("?" for _ in runs)
    with connect() as c:
        rows = c.execute(f"SELECT run, MAX(CASE WHEN status<>'open' THEN 0 "
                         f"WHEN recovery_route='unblock' THEN 1 ELSE 2 END) AS route "
                         f"FROM asks WHERE run IN ({places}) GROUP BY run", runs)
        return {r["run"]: ("closed", "unblock", "owner")[r["route"]] for r in rows}


def open_asks(connect=reader, *, owner_only=False, group_runners=False) -> list[dict]:
    with connect() as c:
        rows = [ask_row(r) for r in c.execute(
            "SELECT * FROM asks WHERE status = 'open' "
            + ("AND recovery_route='owner' " if owner_only else "")
            + f"AND {LATEST} ORDER BY asked, rowid")]
    if group_runners:
        shared = [r for r in rows if r["call"] == owner_asks.CHECK_RUNNER_CALL]
        if len(shared) > 1:
            shared[0]["why"] += " Affected tickets: " + ", ".join(f"{r['repo']}#{r['ticket']}" for r in shared) + "."
            return [r for r in rows if r["call"] != owner_asks.CHECK_RUNNER_CALL or r is shared[0]]
    return rows


def ready_runner_tickets(repo: str) -> list[str]:
    with reader() as c:
        return [str(r[0]) for r in c.execute(
            f"""SELECT ticket FROM asks JOIN runs ON runs.id=asks.run
                WHERE asks.repo=? AND call=? AND asks.status='answered' AND taken_by=''
                  AND json_extract(asks.answers, '$[0].done')=1 AND runs.status='needs-info' AND {LATEST}
                  AND NOT EXISTS (SELECT 1 FROM asks other WHERE other.run=asks.run AND other.status='open')
                ORDER BY asked, asks.rowid""", (repo, owner_asks.CHECK_RUNNER_CALL))]


def stage(ask_id: str, answers: dict) -> bool:
    with db() as c:
        changed = bool(c.execute("UPDATE asks SET staged_answers=? WHERE id=? AND status='open'",
                                 (json.dumps(answers), ask_id)).rowcount)
        if changed:
            _sync_inbox(c, "asks")
        return changed


def route_to_owner(ask_id: str) -> bool:
    with db() as c:
        changed = bool(c.execute("UPDATE asks SET recovery_route='owner' WHERE id=? AND status='open' "
                                 "AND recovery_route='unblock'", (ask_id,)).rowcount)
        if changed:
            _sync_inbox(c, "asks")
        return changed


def asks_due(run_ids) -> list[dict]:
    ids = list(run_ids)
    if not ids:
        return []
    with reader() as c:
        return [ask_row(r) for r in c.execute(
            f"SELECT * FROM asks WHERE status = 'answered' AND taken_by = '' AND run IN ({','.join('?' * len(ids))}) "
            "ORDER BY asked, rowid", ids)]


def ask_close(ask_id: str, status: str, answers: list[dict] | None = None) -> bool:
    with db() as c:
        row = c.execute("SELECT call FROM asks WHERE id=? AND status='open'", (ask_id,)).fetchone()
        if row is None:
            return False
        now, encoded = util.iso(), json.dumps(answers or [])
        c.execute("UPDATE asks SET status=?, answered=?, answers=? WHERE id=?", (status, now, encoded, ask_id))
        if (row["call"] == owner_asks.CHECK_RUNNER_CALL and status == "answered" and len(answers or []) == 1
                and answers[0] == {"header": "macOS runner", "kind": "done", "done": True}):
            c.execute(f"UPDATE asks SET status='answered', answered=?, answers=? "
                      f"WHERE status='open' AND call=? AND {LATEST}", (now, encoded, owner_asks.CHECK_RUNNER_CALL))
        _sync_inbox(c, "asks")
        return True


def asks_retire(run_id: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE asks SET status = ?, answered = COALESCE(answered, ?) WHERE run = ? AND "
                  "(status = 'open' OR (status = 'answered' AND taken_by = ''))", (status, util.iso(), run_id))
        _sync_inbox(c, "asks")


def asks_taken(ids: list[str], run_id: str) -> None:
    with db() as c:
        c.executemany("UPDATE asks SET taken_by = ? WHERE id = ?", [(run_id, i) for i in ids])


def ask_remind(ask_id: str) -> bool:
    with db() as c:
        return c.execute("UPDATE asks SET reminded = ? WHERE id = ? AND status = 'open' AND reminded = ''",
                         (util.iso(), ask_id)).rowcount > 0


def asks_asked_before(iso: str) -> list[dict]:
    with reader() as c:
        return [ask_row(r) for r in c.execute("SELECT * FROM asks WHERE status = 'open' AND asked < ? "
                                              "ORDER BY asked, rowid", (iso,))]
