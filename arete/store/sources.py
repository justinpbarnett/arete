from __future__ import annotations

from .. import util
from .db import db, reader

TABLES = {
    "seen": "until source forgets",
    "cursors": "kept",
    "source_owners": "kept",
    "passed_over": "until taken or dropped",
}


def source_seen(source: str, connect=reader) -> set[str]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT key FROM seen WHERE source = ?", (source,))}


def claim_seen(source: str, keys: list[str], connect=db) -> list[str]:
    with connect() as c:
        return [str(k) for k in keys if c.execute("INSERT OR IGNORE INTO seen (source, key, ts) VALUES (?,?,?)",
                                                 (source, str(k), util.iso())).rowcount == 1]


def forget_seen(source: str, key: str, before, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM seen WHERE source = ? AND key = ? AND ts < ?", (source, key, util.iso(before)))


def cursor(source: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM cursors WHERE source = ?", (source,)).fetchone()
        return dict(r) if r else None


def set_cursor(source: str, value: str | None, ok: bool, detail: str = "", connect=db, started=None) -> None:
    with connect() as c:
        row = c.execute("SELECT * FROM cursors WHERE source = ?", (source,)).fetchone()
        prev = dict(row) if row else {}
        last_ok = (started or util.iso()) if ok else prev.get("last_ok") or (prev.get("polled") if prev.get("ok") else None)
        c.execute("INSERT OR REPLACE INTO cursors (source, value, polled, ok, detail, last_ok) VALUES (?,?,?,?,?,?)",
                  (source, value if value is not None else prev.get("value"), util.iso(), int(ok), detail[:500],
                   last_ok))


def adopt_source(old: str, new: str) -> None:
    with db() as c:
        for table in ("cursors", "seen", "source_owners"):
            c.execute(f"UPDATE OR REPLACE {table} SET source = ? WHERE source = ?", (new, old))


def source_owner(source: str) -> str | None:
    with reader() as c:
        r = c.execute("SELECT fingerprint FROM source_owners WHERE source = ?", (source,)).fetchone()
        return r[0] if r else None


def set_source_owner(source: str, fingerprint: str, connect=db) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO source_owners (source, fingerprint) VALUES (?,?)", (source, fingerprint))


def cursors(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cursors ORDER BY source")]


def pass_over(repo: str, issue: str) -> None:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO passed_over (repo, issue, since) VALUES (?,?,?)", (repo, str(issue), util.iso()))


def tickets_passed_over() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM passed_over ORDER BY since")]


def take_passed_over(repo: str, issue: str) -> str:
    with db() as c:
        r = c.execute("SELECT since FROM passed_over WHERE repo = ? AND issue = ?", (repo, str(issue))).fetchone()
        c.execute("DELETE FROM passed_over WHERE repo = ? AND issue = ?", (repo, str(issue)))
    return r["since"] if r else ""


def drop_passed_over(repo: str, keep: str = "") -> None:
    with db() as c:
        c.execute("DELETE FROM passed_over WHERE repo = ? AND issue != ?", (repo, str(keep)))


def write_scan_cursor(c, source: str, value: str | None, ok: bool, detail: str) -> None:
    c.execute("INSERT OR REPLACE INTO cursors (source, value, polled, ok, detail) VALUES (?,?,?,?,?)",
              (source, value, util.iso(), int(ok), detail[:500]))
