from __future__ import annotations

import json

from .. import util
from .db import db, reader

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
    return {**dict(r), "questions": json.loads(r["questions"]), "session": json.loads(r["session"] or "{}"),
            "answers": json.loads(r["answers"] or "[]")}


def ask_add(ask_id: str, run: str, repo: str, ticket: str, step: str, call: str, why: str, tried: str,
            questions: list[dict], session: dict) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO asks (id, run, repo, ticket, step, call, why, tried, questions, asked, "
                  "session) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (ask_id, run, repo, str(ticket), step, call, why, tried, json.dumps(questions), util.iso(),
                   json.dumps(session)))


def ask_get(ask_id: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        return ask_row(r) if r else None


def asks_of(run: str, status: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        return [ask_row(r) for r in c.execute(
            "SELECT * FROM asks WHERE run = ?" + (" AND status = ?" if status else "") + " ORDER BY asked, rowid",
            (run, status) if status else (run,))]


def open_asks(connect=reader) -> list[dict]:
    with connect() as c:
        return [ask_row(r) for r in c.execute(
            "SELECT * FROM asks WHERE status = 'open' AND NOT EXISTS (SELECT 1 FROM runs a, runs later WHERE "
            "a.id = asks.run AND later.kind = 'pipeline' AND later.repo = a.repo AND later.issue = a.issue AND "
            "COALESCE(later.only_step, '') = '' AND later.rowid > a.rowid) ORDER BY asked, rowid")]


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
        return c.execute("UPDATE asks SET status = ?, answered = ?, answers = ? WHERE id = ? AND status = 'open'",
                         (status, util.iso(), json.dumps(answers or []), ask_id)).rowcount > 0


def asks_retire(run_id: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE asks SET status = ?, answered = COALESCE(answered, ?) WHERE run = ? AND "
                  "(status = 'open' OR (status = 'answered' AND taken_by = ''))", (status, util.iso(), run_id))


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
