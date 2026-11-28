from __future__ import annotations

from collections.abc import Sequence

from .. import util
from . import factory
from .db import db, reader

TABLES = {
    "chats": "kept",
    "chat_messages": "kept",
    "chat_sessions": "kept",
}


CHAT_USAGE = ("(SELECT COALESCE(SUM(m.paid_usd), 0) FROM chat_messages m WHERE m.chat_id = chats.id) AS paid_usd, "
              "(SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id = chats.id "
              "AND m.paid_usd IS NULL AND m.cost != 0) AS unclassified_messages")


def chat_add(project: str) -> int:
    ts = util.iso()
    with db() as c:
        return c.execute("INSERT INTO chats (project, created, updated) VALUES (?,?,?)", (project, ts, ts)).lastrowid


def chat_get(chat_id: int) -> dict | None:
    with reader() as c:
        r = c.execute(f"SELECT chats.*, {CHAT_USAGE} FROM chats WHERE id = ?", (chat_id,)).fetchone()  # noqa: S608
        return dict(r) if r else None


def chats() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(f"SELECT chats.*, {CHAT_USAGE} FROM chats ORDER BY created DESC, id DESC")]  # noqa: S608


def chat_messages(chat_id: int) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT id, sender, text, cost, paid_usd, ts FROM chat_messages WHERE chat_id = ? "
                                           "ORDER BY id", (chat_id,))]


def chat_claim(chat_id: int, text: str, title: str) -> int:
    ts = util.iso()
    with db() as c:
        claimed = c.execute("UPDATE chats SET busy = 1, updated = ?, title = CASE WHEN title = '' THEN ? ELSE title END "
                            "WHERE id = ? AND busy = 0", (ts, title, chat_id)).rowcount
        if claimed:
            return c.execute("INSERT INTO chat_messages (chat_id, sender, text, ts) VALUES (?,?,?,?)",
                             (chat_id, "you", text, ts)).lastrowid
        return 0


CHAT_FIELDS = frozenset({"title", "project", "created", "updated", "spent", "busy", "session",
                         "session_spent", "runner", "draft_title", "draft_body", "summary", "summarized_to"})


def chat_say(chat_id: int, sender: str, text: str, cost: float = 0.0, *, paid_usd: float | None = None,
             reported_cost: float | None = None, before: Sequence[tuple[str, str]] = (), **fields) -> None:
    if unknown := set(fields) - CHAT_FIELDS:
        raise ValueError(f"invalid chat fields: {', '.join(sorted(unknown))}")
    ts = util.iso()
    reported_cost = cost if reported_cost is None else reported_cost
    paid_usd = 0.0 if reported_cost == 0 and paid_usd is None else paid_usd
    with db() as c:
        for kind, note in before:
            c.execute("INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) VALUES (?,?,?,?,?,?)",
                      (chat_id, kind, note, 0, 0, ts))
        c.execute("INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) VALUES (?,?,?,?,?,?)",
                  (chat_id, sender, text, reported_cost, paid_usd, ts))
        c.execute(f"UPDATE chats SET {''.join(f'{k} = ?, ' for k in fields)}updated = ?, spent = spent + ? WHERE id = ?",  # noqa: S608
                  (*fields.values(), ts, reported_cost, chat_id))


CHAT_SESSION_COLUMNS = ("l.run_id, l.chat_id, l.budget_usd, l.told, r.status, r.started, r.finished, r.repo, "
                        "r.branch, r.device, r.model, r.runner, r.summary, r.pr_url, r.lock, r.pid")


def chat_link_session(chat_id: int, run_id: str, budget_usd: float | None) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO chat_sessions (run_id, chat_id, budget_usd, linked) VALUES (?,?,?,?)",
                  (run_id, chat_id, budget_usd, util.iso()))


def chat_sessions(chat_id: int | None = None, connect=reader) -> list[dict]:
    where, args = ("WHERE l.chat_id = ?", (chat_id,)) if chat_id is not None else ("", ())
    with connect() as c:
        return [dict(r) for r in c.execute(
            f"SELECT {CHAT_SESSION_COLUMNS} FROM chat_sessions l LEFT JOIN runs r ON r.id = l.run_id {where} "  # noqa: S608
            "ORDER BY l.linked DESC, l.rowid DESC", args)]


def chat_of_session(run_id: str, connect=reader) -> int | None:
    with connect() as c:
        r = c.execute("SELECT chat_id FROM chat_sessions WHERE run_id = ?", (run_id,)).fetchone()
        return r["chat_id"] if r else None


def chat_sessions_untold() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            f"SELECT {CHAT_SESSION_COLUMNS} FROM chat_sessions l LEFT JOIN runs r ON r.id = l.run_id "  # noqa: S608
            "WHERE l.told = '' AND COALESCE(r.status, '') != 'running' "
            f"{factory.exclude_retained(c, 'l.run_id')} ORDER BY l.rowid")]


def chat_tell_session(run_id: str, text: str) -> bool:
    ts = util.iso()
    with db() as c:
        row = c.execute("SELECT chat_id FROM chat_sessions WHERE run_id = ? AND told = ''", (run_id,)).fetchone()
        if not row or not c.execute("UPDATE chat_sessions SET told = ? WHERE run_id = ? AND told = ''",
                                    (ts, run_id)).rowcount:
            return False
        c.execute("INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) VALUES (?,?,?,?,?,?)",
                  (row["chat_id"], "session", text, 0, 0, ts))
        c.execute("UPDATE chats SET updated = ? WHERE id = ?", (ts, row["chat_id"]))
        return True


def chat_summarize(chat_id: int, summary: str, upto: int) -> None:
    with db() as c:
        c.execute("UPDATE chats SET summary = ?, summarized_to = ? WHERE id = ?", (summary, upto, chat_id))


def chat_drop_draft(chat_id: int, title: str, body: str) -> None:
    with db() as c:
        c.execute("UPDATE chats SET draft_title = '', draft_body = '' WHERE id = ? AND draft_title = ? AND draft_body = ?",
                  (chat_id, title, body))


def chat_newest_draft() -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM chats WHERE draft_title != '' ORDER BY updated DESC, id DESC LIMIT 1").fetchone()
        return dict(r) if r else None


def busy_chats() -> list[int]:
    with reader() as c:
        return [r[0] for r in c.execute("SELECT id FROM chats WHERE busy = 1 ORDER BY id")]


def stale_busy(c, cutoff):
    return c.execute("SELECT id FROM chats WHERE busy = 1 AND updated < ? ORDER BY id", (cutoff,)).fetchall()
