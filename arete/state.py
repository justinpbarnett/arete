from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager, suppress
from datetime import timedelta

from . import dbstats, migrations, paths, util
from .agent import billing_of
from .migrations import m0002_foundation_time_shape as time_shape
from .sighting import Sighting

TIME_COLUMNS = time_shape.TIME_COLUMNS
TIME_GLOB = time_shape.TIME_GLOB

_CHAIN = migrations.load()
LATEST = _CHAIN[-1].n
NEEDED = max(m.n for m in _CHAIN if m.phase != "contract")
MIGRATE_WAIT_SECONDS = 60
_CHECKED = set()
_OWNER = threading.local()


class NoDatabase(RuntimeError):
    pass


class IncompatibleSchema(RuntimeError):
    pass


OPEN_PR_RUN = "(runs.status = 'pr' OR (runs.status IN ('failed', 'stopped') AND runs.pr_url != ''))"
DECIDABLE = ("pending", "failed")
APPROVAL_NOTES = ("plain_summary", "plain_summary_tried", "review", "review_tried")
WHOLE_RUN = "COALESCE(only_step, '') = ''"
USAGE_COLUMNS = ("at", "tab", "device", "kind", "view", "name", "via", "ms")
REFRESH_SCOPE = "credential-refresh-v1:"


def refuse_empty_file() -> None:
    if paths.DB_FILE.exists() and paths.DB_FILE.stat().st_size > 0:
        return
    from . import backup

    if newest := backup.newest_snapshot():
        raise NoDatabase(f"{paths.DB_FILE} is missing or empty but {newest} exists; stop arete serve and run "
                         f"`arete backup --restore {newest}` instead of starting over with an empty file")


def missing_database():
    refuse_empty_file()
    return NoDatabase(f"{paths.DB_FILE} is missing; run arete serve or arete migrate")


def contract_blockers(conn, n):
    from . import control, record

    blockers = []
    for row in conn.execute("SELECT * FROM runs WHERE status = 'running' "
                            "AND (schema_version IS NULL OR schema_version < ?)", (n,)):
        row = dict(row)
        with record.dead(row) as gone:
            if not gone:
                blockers.append(row["id"])
    beat = util.read_json(control.BEAT_FILE, {})
    if util.pid_alive(beat.get("pid")) and (beat.get("schema") or 0) < n:
        blockers.append(f"serve {beat.get('pid')}")
    return blockers


def execute_statements(conn, sql):
    pending = ""
    for char in sql:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip():
        conn.execute(pending)


def migrate(contracts=True):
    from . import backup

    chain = migrations.load()
    paths.ensure_dirs()
    with util.lock("migrate", wait=True):
        refuse_empty_file()
        conn = paths.connect_db(paths.DB_FILE, create=True)
        conn.row_factory = sqlite3.Row
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            migrations.validate(conn, chain, version)
            conn.close()
            conn = None
            snapshot = None
            started_chain = False
            for migration in chain:
                if migration.n <= version:
                    continue
                if migration.phase == "contract":
                    if not contracts:
                        return True
                    conn = paths.connect_db(paths.DB_FILE)
                    conn.row_factory = sqlite3.Row
                    try:
                        blockers = contract_blockers(conn, migration.n)
                    finally:
                        conn.close()
                        conn = None
                    if blockers:
                        print(f"migration {migration.name} waits for {', '.join(blockers)}", flush=True)
                        return True
                if not started_chain:
                    if paths.DB_FILE.stat().st_size:
                        snapshot = backup.snapshot(f"m{migration.n}")
                    started_chain = True
                started = time.monotonic()
                try:
                    timing = dbstats.Timing(paths.DB_FILE, "write")
                    with owning_transaction(lambda: paths.connect_db(paths.DB_FILE), "BEGIN EXCLUSIVE", timing) as writer:
                        if migration.sql is not None:
                            execute_statements(writer, migration.sql)
                        else:
                            migration.apply(writer)
                        writer.execute("INSERT INTO schema_migrations "
                                       "(n, name, area, phase, sha256, applied, backup, seconds) VALUES (?,?,?,?,?,?,?,?)",
                                       (migration.n, migration.name, migration.area, migration.phase, migration.sha256,
                                        util.iso(), snapshot.name if snapshot else "", time.monotonic() - started))
                        writer.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                                       (str(migration.n),))
                        writer.execute(f"PRAGMA user_version = {migration.n}")
                except BaseException as e:
                    raise migrations.Refused(f"{migration.name}: {e}") from e
                version = migration.n
                print(f"applied {migration.name}", flush=True)
            backup.finish_restore()
            return False
        finally:
            if conn is not None:
                conn.close()


def checked_connection():
    from . import backup

    if backup.pending_restore():
        raise IncompatibleSchema("pending restore requires arete serve or arete migrate before writing")
    try:
        identity = paths.DB_FILE.stat()
    except FileNotFoundError:
        if not paths.ISOLATED:
            raise missing_database() from None
        migrate()
        identity = paths.DB_FILE.stat()
    if identity.st_size == 0:
        refuse_empty_file()
    try:
        conn = paths.connect_db(paths.DB_FILE)
    except (sqlite3.OperationalError, FileNotFoundError):
        if not paths.DB_FILE.exists():
            raise missing_database() from None
        raise
    try:
        key = (os.getpid(), str(paths.DB_FILE), identity.st_dev, identity.st_ino,
               getattr(identity, "st_birthtime", None), LATEST, NEEDED)
        if key not in _CHECKED:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if paths.ISOLATED and version == 0:
                conn.close()
                migrate()
                return checked_connection()
            deadline = time.monotonic() + MIGRATE_WAIT_SECONDS
            if version < NEEDED:
                print(f"schema {version} waits for schema {NEEDED}; run arete serve or arete migrate", flush=True)
            while version < NEEDED:
                if time.monotonic() >= deadline:
                    raise IncompatibleSchema(f"schema {version} needs {NEEDED}; run arete serve or arete migrate")
                time.sleep(0.5)
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            oldest = int(conn.execute("SELECT value FROM schema_meta WHERE key = 'oldest_writer'").fetchone()[0] or 0)
            if oldest > LATEST:
                raise IncompatibleSchema(f"oldest_writer {oldest} is above this code's schema {LATEST}")
            _CHECKED.add(key)
        return conn
    except BaseException:
        with suppress(BaseException):
            conn.close()
        raise


@contextmanager
def owning_transaction(connect, begin="BEGIN IMMEDIATE", timing=None, *, commit=True):
    conn = None
    failed = False
    try:
        conn = connect()
        conn.row_factory = sqlite3.Row
        conn.execute(begin)
        if timing:
            timing.begun()
        yield conn
        if commit:
            if timing:
                timing.committing()
            try:
                conn.execute("COMMIT")
            finally:
                if timing:
                    timing.committed()
    except BaseException as e:
        failed = True
        if timing:
            timing.error = type(e).__name__
            timing.sqlite_error = isinstance(e, sqlite3.Error)
        if conn is not None:
            with suppress(BaseException):
                conn.execute("ROLLBACK")
        raise
    finally:
        try:
            if conn is not None:
                if failed:
                    with suppress(BaseException):
                        conn.close()
                else:
                    conn.close()
        finally:
            if timing:
                timing.finished()


def read_connection(*, streaming=False):
    try:
        return paths.connect_db(paths.DB_FILE, readonly=True, streaming=streaming)
    except (sqlite3.OperationalError, FileNotFoundError):
        if not paths.DB_FILE.exists():
            raise missing_database() from None
        raise


@contextmanager
def owned(mode):
    active = getattr(_OWNER, "active", None)
    if active and active[0] == os.getpid():
        _, held_mode, conn = active
        if held_mode == "read" and mode == "write":
            raise RuntimeError("cannot promote a read snapshot to a writer")
        yield conn
        return
    timing = dbstats.Timing(paths.DB_FILE, mode)
    connect = checked_connection if mode == "write" else read_connection
    try:
        with owning_transaction(connect, "BEGIN IMMEDIATE" if mode == "write" else "BEGIN", timing,
                                commit=mode == "write") as conn:
            _OWNER.active = (os.getpid(), mode, conn)
            yield conn
    finally:
        _OWNER.active = None


@contextmanager
def tx():
    with owned("write") as conn:
        yield conn


db = tx


@contextmanager
def one_read():
    with owned("read") as conn:
        yield conn


@contextmanager
def reader(*, streaming=False):
    if not streaming:
        with one_read() as conn:
            yield conn
        return
    conn = read_connection(streaming=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def read_snapshot(conn):
    timing = dbstats.Timing(paths.DB_FILE, "read")
    try:
        conn.execute("BEGIN")
        timing.begun()
        yield conn
    except BaseException as e:
        timing.error = type(e).__name__
        timing.sqlite_error = isinstance(e, sqlite3.Error)
        raise
    finally:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            timing.finished()


def write_connection():
    active = getattr(_OWNER, "active", None)
    if not active or active[:2] != (os.getpid(), "write") or not active[2].in_transaction:
        raise RuntimeError("event emission needs an open write transaction")
    return active[2]


def ensure_schema() -> None:
    with db():
        pass


def backfill_legacy_cost(before: dict | None = None) -> int:
    before = {} if before is None else before
    if not isinstance(before, dict) or any(not isinstance(k, str) or isinstance(v, bool)
                                         or not isinstance(v, (int, float))
                                         for k, v in before.items()):
        raise ValueError("before must map run IDs to finite dollar amounts")
    try:
        before = {k: float(v) for k, v in before.items()}
    except OverflowError:
        raise ValueError("before must map run IDs to finite dollar amounts") from None
    if any(not math.isfinite(v) for v in before.values()):
        raise ValueError("before must map run IDs to finite dollar amounts")
    with db() as c:
        missing = sorted(set(before) - {r[0] for r in c.execute("SELECT id FROM runs")})
        if missing:
            raise ValueError(f"no run: {missing[0]}")
        c.execute("""
            WITH totals AS MATERIALIZED (
                SELECT run_id, SUM(cost_usd) AS usd, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout
                FROM steps GROUP BY run_id
            ), eligible AS MATERIALIZED (
                SELECT runs.id, runs.started, COALESCE(runs.cost_usd, 0) AS usd,
                       COALESCE(totals.usd, 0) AS spent,
                       COALESCE(runs.tokens_in, 0) - COALESCE(totals.tin, 0) AS tin,
                       COALESCE(runs.tokens_out, 0) - COALESCE(totals.tout, 0) AS tout,
                       edits.value AS old
                FROM runs LEFT JOIN totals ON totals.run_id = runs.id
                LEFT JOIN json_each(?) AS edits ON edits.key = runs.id
                WHERE runs.started < (SELECT MIN(ts) FROM steps)
                  AND COALESCE(runs.cost_usd, 0) != COALESCE(totals.usd, 0)
            )
            INSERT INTO steps (run_id, step, kind, runner, ok, auth_mode, ts,
                               cost_usd, tokens_in, tokens_out, error)
            SELECT id, 'legacy', 'legacy', 'legacy', 1, 'subscription', started,
                   COALESCE(old, usd) - spent, tin, tout, '' FROM eligible
            UNION ALL
            SELECT id, 'correction', 'correction', 'correction', 1, 'subscription', started,
                   usd - old, 0, 0, 'legacy repricing from supplied before value'
            FROM eligible WHERE old IS NOT NULL AND usd != old
        """, (json.dumps(before),))
        return c.execute("SELECT changes()").fetchone()[0]


def cost_drift(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("""
            SELECT runs.id, COALESCE(runs.cost_usd, 0) AS run_usd,
                   COALESCE(t.usd, 0) AS steps_usd,
                   COALESCE(runs.cost_usd, 0) - COALESCE(t.usd, 0) AS difference
            FROM runs LEFT JOIN (SELECT run_id, SUM(cost_usd) AS usd FROM steps GROUP BY run_id) t
            ON t.run_id = runs.id
            WHERE ABS(COALESCE(runs.cost_usd, 0) - COALESCE(t.usd, 0)) > 0.000001
            ORDER BY runs.id
        """)]


FIRINGS = ("(runs.kind = 'loop' AND runs.loop_kind = 'command' AND runs.status IN ('done', 'skipped') "
           "AND COALESCE(runs.cost_usd, 0) = 0)")


def runs(kind: str | None = None, repo: str | None = None, limit: int = 50, connect=reader, loop: str | None = None,
         firings: bool = False, statuses: tuple[str, ...] = (), running_first: bool = False) -> list[dict]:
    q, args = "SELECT * FROM runs WHERE 1=1", []
    if not firings:
        q += f" AND NOT {FIRINGS}"
    for column, value in (("kind", kind), ("loop", loop), ("repo", repo)):
        if value:
            q += f" AND {column} = ?"
            args.append(value)
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    q += " ORDER BY " + ("status = 'running' DESC, " if running_first else "") + "started DESC, rowid DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def run_facets(connect=reader) -> dict[str, list[str]]:
    with connect() as c:
        return {column: [r[0] for r in c.execute(f"SELECT DISTINCT {column} FROM runs WHERE NOT {FIRINGS} "
                                                 f"AND COALESCE({column}, '') != '' ORDER BY 1")]
                for column in ("kind", "repo", "loop")}


def paid_by_run(run_ids: list[str], connect=reader, local_providers=None) -> dict[str, float]:
    if not run_ids:
        return {}
    if local_providers is None:
        from . import config

        local_providers = config.load().local_providers
    paid: dict[str, float] = {}
    with connect() as c:
        rows = c.execute("SELECT run_id, cost_usd, runner, provider, model, auth_mode FROM steps "
                         f"WHERE run_id IN ({','.join('?' * len(run_ids))})", run_ids)
        for r in rows:
            if billing_of(dict(r), local_providers) == "api_key":
                paid[r[0]] = paid.get(r[0], 0.0) + float(r["cost_usd"] or 0)
    return {k: round(v, 6) for k, v in paid.items()}


def merged_runs(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM runs WHERE kind = 'pipeline' AND status = 'merged' AND {WHOLE_RUN} "
            "AND COALESCE(pr_url, '') != '' ORDER BY finished, id")]


def source_seen(source: str, connect=reader) -> set[str]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT key FROM seen WHERE source = ?", (source,))}


def runs_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE started >= ? ORDER BY started", (since,))]


def runs_reading_pr_comments(rejected_since: str, limit: int = 200) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND pr_url != '' "
                                           f"AND ({OPEN_PR_RUN} OR (status = 'rejected' AND finished >= ?)) "
                                           "ORDER BY started DESC LIMIT ?", (rejected_since, limit))]


def open_runs(repo: str, issue: int | str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE repo = ? AND issue = ? AND {OPEN_PR_RUN} "
                                           "ORDER BY started DESC", (repo, issue))]


def run_lock(kind: str, repo: str | None = None, issue: int | str | None = None) -> str:
    return {"pipeline": f"run-{repo}-{issue}", "slot": f"slot-{repo}-{issue}", "slot-wait": f"slot-wait-{repo}-{issue}",
            "eval": f"eval-{repo}", "experiment": f"experiment-{repo}", "eval-mine": "eval-mine", "miner": "mine"}[kind]


def loop_lock(name: str) -> str:
    return "loop-" + name.replace("/", ".")


def run_get(run_id: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(r) if r else None


def save_report(run_id: str, report: dict, connect=db) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO reports (run_id, saved, report) VALUES (?, ?, ?)",
                  (run_id, util.iso(), json.dumps(report)))


def report(run_id: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT report FROM reports WHERE run_id = ?", (run_id,)).fetchone()
    try:
        found = json.loads(r["report"]) if r else None
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def steps(run_id: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY id", (run_id,))]


def step_tries(run_id: str, step: str) -> int:
    with reader() as c:
        return c.execute("SELECT COALESCE(MAX(try), 0) FROM steps WHERE run_id = ? AND step = ?", (run_id, step)).fetchone()[0]


def steps_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT steps.*, r.loop, r.kind AS run_kind, r.repo FROM steps "
                                           "LEFT JOIN runs r ON r.id = steps.run_id WHERE steps.ts >= ? "
                                           "ORDER BY steps.ts", (since,))]
    return [{**d, "task_kind": d.get("kind") or "", "kind": d["run_kind"]} for d in rows]


def add_feed(run_id: str, kind: str, text: str) -> None:
    insert_feed(run_id, kind, text)


def insert_feed(run_id: str, kind: str, text: str) -> None:
    with tx() as c:
        c.execute("INSERT INTO feed (run_id, ts, kind, text) VALUES (?,?,?,?)",
                  (run_id, util.iso(), kind, util.both_ends(text, 4000)))


def feed(run_id: str, after: int = 0, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feed WHERE run_id = ? AND id > ? ORDER BY id",
                                           (run_id, after))]


def loop_state(name: str, connect=reader) -> dict:
    with connect() as c:
        r = c.execute("SELECT * FROM loop_state WHERE loop = ?", (name,)).fetchone()
    if not r:
        return {"loop": name, "data": {}, "last_fire": None, "next_due": None, "metric_on": 0}
    return {**dict(r), "data": json.loads(r["data"] or "{}")}


def loop_states(connect=reader) -> dict[str, dict]:
    with connect() as c:
        return {r["loop"]: {**dict(r), "data": json.loads(r["data"] or "{}")} for r in c.execute("SELECT * FROM loop_state")}


def loop_marks(connect=reader) -> dict[str, dict]:
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM loop_state")]
    return {r["loop"]: {"ts": r["mark_at"], "ok": bool(r.get("mark_ok")), "detail": r.get("mark_detail") or ""}
            for r in rows if r.get("mark_at")}


def set_loop_state(name: str, **fields) -> None:
    if "data" in fields:
        fields["data"] = json.dumps(fields["data"], default=str)
    fields["updated"] = util.iso()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO loop_state (loop) VALUES (?)", (name,))
        c.execute(f"UPDATE loop_state SET {', '.join(f'{k} = ?' for k in fields)} WHERE loop = ?",
                  (*fields.values(), name))


def finding_seen(loop: str, fingerprint: str) -> bool:
    with reader() as c:
        return c.execute("SELECT 1 FROM findings WHERE loop = ? AND fingerprint = ?", (loop, fingerprint)).fetchone() \
            is not None


def record_finding(loop: str, fingerprint: str, fingerprint_text: str, title: str, url: str, run_id: str) -> bool:
    with db() as c:
        before = c.total_changes
        c.execute("INSERT OR IGNORE INTO findings (loop, fingerprint, fingerprint_text, title, url, run_id, first_seen, "
                  "last_seen, emitted) VALUES (?,?,?,?,?,?,?,?,'')",
                  (loop, fingerprint, fingerprint_text, title, url, run_id, util.iso(), util.iso()))
        if c.total_changes > before:
            return True
        c.execute("UPDATE findings SET run_id = ? WHERE loop = ? AND fingerprint = ? AND emitted = '' "
                  "AND COALESCE(url, '') = '' AND COALESCE(run_id, '') != ?", (run_id, loop, fingerprint, run_id))
        taken_over = c.total_changes > before
        c.execute("UPDATE findings SET seen = seen + 1, last_seen = ? WHERE loop = ? AND fingerprint = ?",
                  (util.iso(), loop, fingerprint))
        return taken_over


def mark_emitted(loop: str, fingerprints: list[str], run_id: str) -> None:
    with db() as c:
        c.executemany("UPDATE findings SET emitted = ? WHERE loop = ? AND fingerprint = ? AND run_id = ? "
                      "AND emitted = ''", [(run_id, loop, fingerprint, run_id) for fingerprint in fingerprints])


def set_finding_url(loop: str, fingerprint: str, url: str) -> None:
    with db() as c:
        c.execute("UPDATE findings SET url = ? WHERE loop = ? AND fingerprint = ?", (url, loop, fingerprint))


def findings(loop: str, limit: int = 50, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM findings WHERE loop = ? ORDER BY last_seen DESC LIMIT ?",
                                           (loop, limit))]


def add_event(loop: str, e: dict, connect=db) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO events (loop, type, source, key, repo, title, url, trust, ts) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (loop, e.get("type"), e.get("source"), str(e.get("key")), e.get("repo"), e.get("title"),
                         e.get("url"), e.get("trust"), util.iso()))
        return cur.lastrowid


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


def enqueue(loop: str, queue: str, grp: str, trigger: str, payload: list | None = None, connect=db) -> int:
    with connect() as c:
        if not payload and c.execute("SELECT 1 FROM queue WHERE loop = ? AND run_id IS NULL AND payload = '[]' "
                                     "AND trigger = ?", (loop, trigger)).fetchone():
            return 0
        cur = c.execute("INSERT INTO queue (loop, queue, grp, trigger, payload, enqueued) VALUES (?,?,?,?,?,?)",
                        (loop, queue, grp, trigger, json.dumps(payload or [], default=str), util.iso()))
        return cur.lastrowid


def pending(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM queue WHERE run_id IS NULL ORDER BY id")]


def queue_item(item_id: int) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    return {**dict(r), "payload": json.loads(r["payload"] or "[]")} if r else None


def take_queue_item(item_id: int, run_id: str) -> bool:
    with db() as c:
        return c.execute("UPDATE queue SET run_id = ? WHERE id = ? AND run_id IS NULL",
                         (run_id, item_id)).rowcount == 1


def drop_queue_item(item_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM queue WHERE id = ?", (item_id,))


def release_claimed_items(run_id: str) -> None:
    with db() as c:
        c.execute("UPDATE queue SET run_id = NULL WHERE run_id = ?", (run_id,))


def finish_claim(c, row: dict, how: str, summary: str) -> None:
    if how == "retry":
        duplicate = not json.loads(row["payload"] or "[]") and c.execute(
            "SELECT 1 FROM queue WHERE loop = ? AND trigger = ? AND payload = '[]' AND run_id IS NULL",
            (row["loop"], row["trigger"])).fetchone()
        if duplicate:
            how = "drop"
        elif row["attempts"] + 1 < 2:
            c.execute("UPDATE queue SET run_id = NULL, attempts = attempts + 1 WHERE id = ?", (row["id"],))
            return
        else:
            how = "dead"
    if how == "drop" or not json.loads(row["payload"] or "[]"):
        c.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
    else:
        c.execute("UPDATE queue SET dead_at = ?, dead_reason = ? WHERE id = ?",
                  (util.iso(), summary, row["id"]))


def end_claim(run_id: str, how: str, summary: str) -> None:
    with db() as c:
        for row in c.execute("SELECT * FROM queue WHERE run_id = ? AND dead_at IS NULL", (run_id,)).fetchall():
            finish_claim(c, dict(row), how, summary)


def release_settled_claims() -> None:
    with db() as c:
        rows = c.execute("SELECT q.*, r.status, r.summary FROM queue q LEFT JOIN runs r ON r.id = q.run_id "
                         "WHERE q.run_id IS NOT NULL AND q.dead_at IS NULL "
                         "AND (r.status IS NULL OR r.status != 'running')").fetchall()
        for row in rows:
            finish_claim(c, dict(row), "dead" if row["status"] == "stopped" else "retry",
                         row["summary"] or f"run {row['run_id']} {row['status'] or 'is missing'}")


def dead_items(connect=reader) -> list[dict]:
    with connect() as c:
        return [{**{k: row[k] for k in ("id", "loop", "run_id", "dead_reason", "dead_at")},
                 "events": len(json.loads(row["payload"] or "[]"))}
                for row in c.execute("SELECT * FROM queue WHERE dead_at IS NOT NULL ORDER BY id")]


def requeue_item(item_id: int) -> None:
    from . import control

    with db() as c:
        if not c.execute("UPDATE queue SET run_id = NULL, attempts = 0, dead_at = NULL, dead_reason = '' "
                         "WHERE id = ? AND dead_at IS NOT NULL", (item_id,)).rowcount:
            raise control.NotFound(f"no dead queue item {item_id}")


SEEN_AGAIN = "seen again"
CLOSED_AT = "COALESCE(NULLIF(closed, ''), last_seen)"


def prune(days: float = 14, keep_days: float = 30, lines_days: float = 90) -> None:
    since = util.iso(util.now() - timedelta(days=days))
    lines_since, now = util.iso(util.now() - timedelta(days=lines_days)), util.iso()
    with db() as c:
        c.execute(f"INSERT OR IGNORE INTO run_purges SELECT id, 'firing', ? FROM runs "
                  f"WHERE {FIRINGS} AND started < ?", (now, since))
        c.execute(f"DELETE FROM feed WHERE run_id IN (SELECT id FROM runs WHERE {FIRINGS} AND started < ?)", (since,))
        c.execute(f"DELETE FROM runs WHERE {FIRINGS} AND started < ?", (since,))
        eligible = c.execute(
            "SELECT id FROM runs WHERE files_purged IS NULL AND COALESCE(finished, started) < ? "
            "AND status NOT IN ('running', 'pr', 'needs-info') "
            "AND NOT EXISTS (SELECT 1 FROM record_pins WHERE run_id = runs.id) "
            "AND NOT EXISTS (SELECT 1 FROM asks WHERE run = runs.id AND "
            "(status = 'open' OR (status = 'answered' AND COALESCE(taken_by, '') = ''))) "
            "AND NOT EXISTS (SELECT 1 FROM proposals WHERE "
            "(status IS NULL OR status NOT IN ('merged', 'rejected', 'skipped', 'filed')) "
            "AND (run_id = runs.id OR instr(COALESCE(evidence, ''), runs.id) > 0)) "
            "AND NOT EXISTS (SELECT 1 FROM runs recent WHERE recent.repo = runs.repo "
            "AND recent.issue = runs.issue AND recent.started >= ?)", (lines_since, lines_since)).fetchall()
        for row in eligible:
            c.execute("INSERT OR IGNORE INTO run_purges VALUES (?, 'lines', ?)", (row["id"], now))
            c.execute("UPDATE runs SET files_purged = ? WHERE id = ?", (now, row["id"]))
        c.execute("DELETE FROM queue WHERE dead_at < ?", (util.iso(util.now() - timedelta(days=days * 4)),))
        c.execute("DELETE FROM events WHERE ts < ?", (util.iso(util.now() - timedelta(days=days * 4)),))
        old = util.iso(util.now() - timedelta(days=days * 4))
        kept = util.iso(util.now() - timedelta(days=keep_days))
        done = f"SELECT id FROM incidents WHERE state != 'open' AND {CLOSED_AT} < ?"
        c.execute("DELETE FROM incident_notes WHERE ts < ? AND note LIKE ?", (since, f"{SEEN_AGAIN}:%"))
        c.execute(f"DELETE FROM incident_notes WHERE incident IN ({done})", (kept,))
        c.execute(f"UPDATE incidents SET evidence = '' WHERE id IN ({done})", (kept,))
        c.execute(f"DELETE FROM incidents WHERE last_seen < ? AND (state = 'open' OR {CLOSED_AT} < ?)", (old, kept))
        c.execute("DELETE FROM incident_notes WHERE incident NOT IN (SELECT id FROM incidents)")
        c.execute("DELETE FROM incident_aliases WHERE incident NOT IN (SELECT id FROM incidents)")
        c.execute("DELETE FROM incident_repairs WHERE incident NOT IN (SELECT id FROM incidents)")


def write_incident(c, sighting: Sighting) -> tuple[int, bool]:
    fingerprint, kind, source, repo = sighting.fingerprint, sighting.kind, sighting.source, sighting.repo
    signature, count, evidence, policies = sighting.signature, sighting.count, sighting.evidence, sighting.policies
    now = util.iso()
    row = c.execute("SELECT id, count FROM incidents WHERE state = 'open' AND (fingerprint = ? OR id IN "
                    "(SELECT incident FROM incident_aliases WHERE fingerprint = ?)) ORDER BY fingerprint = ? DESC "
                    "LIMIT 1", (fingerprint, fingerprint, fingerprint)).fetchone()
    if row:
        c.execute("UPDATE incidents SET count = count + ?, last_seen = ?, evidence = ?, "
                  "policies = COALESCE(policies, ?) WHERE id = ?", (count, now, evidence, policies, row["id"]))
        note = f"{SEEN_AGAIN}: {count} more from {source}, {row['count'] + count} in all"
        opened, incident_id = False, row["id"]
    else:
        incident_id = c.execute("INSERT INTO incidents (fingerprint, class, source, repo, signature, first_seen, "
                                "last_seen, count, evidence, policies) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (fingerprint, kind, source, repo, signature, now, now, count, evidence,
                                 policies)).lastrowid
        note, opened = f"opened: {kind}, {count} from {source}", True
    c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)", (incident_id, now, note))
    return incident_id, opened


def record_incident(sighting: Sighting) -> tuple[int, bool]:
    with db() as c:
        return write_incident(c, sighting)


def commit_scan(incident_rows: list[Sighting], cursor_rows: list[tuple[str, str, bool, str]],
                aliases=(), marks=None) -> list[bool]:
    with db() as c:
        for fingerprint, incident_id, note in aliases:
            c.execute("INSERT OR REPLACE INTO incident_aliases (fingerprint, incident, ts) VALUES (?,?,?)",
                      (fingerprint, incident_id, util.iso()))
            c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)",
                      (incident_id, util.iso(), note))
        opened = []
        for row in incident_rows:
            incident_id, new = write_incident(c, row)
            opened.append(new)
            if new and row.fingerprint in (marks or {}):
                fields, note = marks[row.fingerprint]
                c.execute(f"UPDATE incidents SET {', '.join(f'{name} = ?' for name in fields)} WHERE id = ?",
                          (*fields.values(), incident_id))
                c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)",
                          (incident_id, util.iso(), note))
        for source, value, ok, detail in cursor_rows:
            c.execute("INSERT OR REPLACE INTO cursors (source, value, polled, ok, detail) VALUES (?,?,?,?,?)",
                      (source, value, util.iso(), int(ok), detail[:500]))
        return opened


def add_incident_note(incident_id: int, note: str) -> None:
    with db() as c:
        c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)", (incident_id, util.iso(), note))


def open_incidents(limit: int = 500) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM incidents WHERE state = 'open' "
                                           "ORDER BY last_seen DESC, id DESC LIMIT ?", (limit,))]


def incidents_to_decide() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM incidents WHERE state = 'open' AND injection = 0 AND class != 'expected' "
            "AND decision IN ('', 'ignore') ORDER BY id")]


def known_fingerprints() -> dict[str, dict]:
    with reader() as c:
        held = {r[0]: {"source": r[1], "repo": r[2], "policies": r[3]} for r in c.execute(
            "SELECT a.fingerprint, i.source, i.repo, i.policies FROM incident_aliases a JOIN incidents i "
            "ON i.id = a.incident WHERE i.state = 'open'")}
        held.update((r[0], {"source": r[1], "repo": r[2], "policies": r[3]}) for r in c.execute(
            "SELECT fingerprint, source, repo, policies FROM incidents WHERE state = 'open'"))
        return held


def update_incident(incident_id: int, note: str = "", **fields) -> None:
    if fields.get("state", "open") != "open" and "closed" not in fields:
        fields["closed"] = util.iso()
    with db() as c:
        if fields:
            c.execute(f"UPDATE incidents SET {', '.join(f'{name} = ?' for name in fields)} WHERE id = ?",
                      (*fields.values(), incident_id))
        if note:
            c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?,?,?)",
                      (incident_id, util.iso(), note))


def woken_incidents() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM incidents WHERE state = 'open' AND decision = 'wake' "
                                           "AND injection = 0 ORDER BY id")]


def closed_alerted() -> list[int]:
    with reader() as c:
        return [r[0] for r in c.execute("SELECT id FROM incidents WHERE alerted = 1 AND state != 'open' ORDER BY id")]


def add_repair(incident_id: int, repo: str) -> int:
    with db() as c:
        return c.execute("INSERT INTO incident_repairs (incident, repo, started) VALUES (?,?,?)",
                         (incident_id, repo, util.iso())).lastrowid


def set_repair(repair_id: int, **fields) -> None:
    with db() as c:
        c.execute(f"UPDATE incident_repairs SET {', '.join(f'{name} = ?' for name in fields)} WHERE id = ?",
                  (*fields.values(), repair_id))


def incident_repairs(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM incident_repairs ORDER BY id")]


def parsed(row: dict) -> dict:
    return {**row, "answers": json.loads(row["answers"] or "{}")}


def incidents(connect=reader, limit: int = 200, open_first: bool = False) -> list[dict]:
    order = "state != 'open', " if open_first else ""
    with connect() as c:
        return [parsed(dict(r)) for r in c.execute(f"SELECT * FROM incidents ORDER BY {order}last_seen DESC, id DESC "
                                                   "LIMIT ?", (limit,))]


def blocked_incidents(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT id, class, source, signature, waiting, last_seen FROM incidents "
                                           "WHERE state = 'open' AND phase = 'blocked' ORDER BY id")]


def incident(incident_id: int, connect=reader, last_notes: int = 0) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not r:
            return None
        total = c.execute("SELECT COUNT(*) FROM incident_notes WHERE incident = ?", (incident_id,)).fetchone()[0]
        if last_notes > 0:
            notes = c.execute("SELECT ts, note FROM (SELECT id, ts, note FROM incident_notes WHERE incident = ? "
                              "ORDER BY id DESC LIMIT ?) ORDER BY id", (incident_id, last_notes))
        else:
            notes = c.execute("SELECT ts, note FROM incident_notes WHERE incident = ? ORDER BY id", (incident_id,))
        repairs = c.execute("SELECT * FROM incident_repairs WHERE incident = ? ORDER BY id", (incident_id,))
        return {**parsed(dict(r)), "notes": [dict(n) for n in notes], "notes_total": total,
                "repairs": [dict(x) for x in repairs]}


def add_approval(loop: str, run_id: str, emit: dict, summary: str, failed: str = "") -> int:
    with db() as c:
        cur = c.execute("INSERT INTO approvals (loop, run_id, emit, summary, status, result, created) "
                        "VALUES (?,?,?,?,?,?,?)", (loop, run_id, json.dumps(emit, default=str), summary[:2000],
                                                   "failed" if failed else "pending", failed[:1000], util.iso()))
        return cur.lastrowid


def approvals(connect=reader) -> list[dict]:
    with connect() as c:
        return [approval_of(r) for r in c.execute("SELECT a.*, COALESCE(r.repo, '') AS run_repo FROM approvals a "
                                                 "LEFT JOIN runs r ON r.id = a.run_id "
                                                 f"WHERE a.status IN ({','.join('?' * len(DECIDABLE))}) "
                                                 "ORDER BY a.id DESC LIMIT 100", DECIDABLE)]


def set_approval_note(approval_id: int, note: str, value: str, while_in: tuple[str, ...] = DECIDABLE) -> bool:
    if note not in APPROVAL_NOTES:
        raise ValueError(f"an approval has no note {note}; it has {', '.join(APPROVAL_NOTES)}")
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET {note} = ? WHERE id = ? AND status IN ({','.join('?' * len(while_in))})",
                  (value, approval_id, *while_in))
        return c.total_changes > before


def approval(approval_id: int) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return approval_of(r) if r else None


def approval_of(row) -> dict:
    return {**dict(row), "emit": json.loads(row["emit"] or "{}"), "review": json.loads(row["review"] or "{}")}


def claim_approval(approval_id: int, until: str, came_from: tuple[str, ...] = DECIDABLE) -> str:
    token = secrets.token_hex(8)
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET status = 'approved', result = '', decided = ?, claimed_by = ?, "
                  f"claimed_until = ? WHERE id = ? AND status IN ({','.join('?' * len(came_from))})",
                  (util.iso(), token, until, approval_id, *came_from))
        return token if c.total_changes > before else ""


def renew_claim(approval_id: int, token: str, until: str) -> bool:
    with db() as c:
        before = c.total_changes
        c.execute("UPDATE approvals SET claimed_until = ? WHERE id = ? AND status = 'approved' AND claimed_by = ?",
                  (until, approval_id, token))
        return c.total_changes > before


def lapsed_claims() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT id, claimed_by, claimed_until FROM approvals "
                                           "WHERE status = 'approved' AND claimed_until < ? ORDER BY id", (util.iso(),))]


def settle_approval(approval_id: int, status: str, outcome: str, result: str = "", *, judged: bool,
                    came_from: tuple[str, ...] = ("approved",), token: str | None = None,
                    until: str | None = None) -> bool:
    held = [f"status IN ({','.join('?' * len(came_from))})", *(["claimed_by = ?"] if token is not None else []),
            *(["claimed_until = ?"] if until is not None else [])]
    args = [*came_from, *(a for a in (token, until) if a is not None)]
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET status = ?, result = ?, decided = ?, claimed_by = '', claimed_until = '' "
                  f"WHERE id = ? AND {' AND '.join(held)}", (status, result[:1000], util.iso(), approval_id, *args))
        if c.total_changes == before:
            return False
        if judged:
            c.execute("UPDATE verdicts SET outcome = ?, outcome_at = ?, approval = ? WHERE kind IN ('push', 'proof') "
                      "AND outcome IN ('waiting', 'failed', 'approved') "
                      "AND run_id = (SELECT run_id FROM approvals WHERE id = ?)",
                      (outcome, util.iso(), approval_id, approval_id))
        return True


def add_verdict(run_id: str, kind: str, subject: str, about: str, answers: dict, held: str,
                outcome: str = "") -> None:
    with db() as c:
        c.execute("INSERT INTO verdicts (at, run_id, loop, kind, subject, about, answers, held, outcome, outcome_at) "
                  "VALUES (?,?,(SELECT loop FROM runs WHERE id = ?),?,?,?,?,?,?,?) "
                  "ON CONFLICT (run_id, kind, subject) DO UPDATE SET at = excluded.at, about = excluded.about, "
                  "answers = excluded.answers, held = excluded.held, outcome = excluded.outcome, "
                  "outcome_at = excluded.outcome_at, sha = '', approval = NULL",
                  (util.iso(), run_id, run_id, kind, subject, about[:300], json.dumps(answers), held[:1000], outcome,
                   util.iso() if outcome else None))


def settle_verdicts(run_id: str, outcome: str, approval: int | None = None, was: tuple[str, ...] = ("",)) -> None:
    with db() as c:
        c.execute(f"UPDATE verdicts SET outcome = ?, outcome_at = ?, approval = COALESCE(?, approval) "
                  f"WHERE run_id = ? AND kind IN ('push', 'proof') AND outcome IN ({','.join('?' * len(was))})",
                  (outcome, util.iso(), approval, run_id, *was))


def set_verdict_sha(run_id: str, sha: str) -> None:
    with db() as c:
        c.execute("UPDATE verdicts SET sha = ? WHERE run_id = ? AND kind IN ('push', 'proof') AND outcome != 'replaced'",
                  (sha, run_id))


def verdicts_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "answers": json.loads(r["answers"] or "{}")}
                for r in c.execute("SELECT * FROM verdicts WHERE at >= ? ORDER BY at", (since,))]


def add_message(run_id: str, text: str) -> None:
    with db() as c:
        c.execute("INSERT INTO messages (run_id, text, ts) VALUES (?,?,?)", (run_id, text, util.iso()))


def take_messages(run_id: str) -> list[str]:
    with db() as c:
        rows = c.execute("SELECT id, text FROM messages WHERE run_id = ? AND taken IS NULL ORDER BY id",
                         (run_id,)).fetchall()
        c.executemany("UPDATE messages SET taken = ? WHERE id = ?", [(util.iso(), r["id"]) for r in rows])
        return [r["text"] for r in rows]


CHAT_USAGE = ("(SELECT COALESCE(SUM(m.paid_usd), 0) FROM chat_messages m WHERE m.chat_id = chats.id) AS paid_usd, "
              "(SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id = chats.id "
              "AND m.paid_usd IS NULL AND m.cost != 0) AS unclassified_messages")


def chat_add(project: str) -> int:
    ts = util.iso()
    with db() as c:
        return c.execute("INSERT INTO chats (project, created, updated) VALUES (?,?,?)", (project, ts, ts)).lastrowid


def chat_get(chat_id: int) -> dict | None:
    with reader() as c:
        r = c.execute(f"SELECT chats.*, {CHAT_USAGE} FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return dict(r) if r else None


def chats() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(f"SELECT chats.*, {CHAT_USAGE} FROM chats ORDER BY created DESC, id DESC")]


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


def chat_say(chat_id: int, sender: str, text: str, cost: float = 0.0, *, paid_usd: float | None = None,
             reported_cost: float | None = None, before: Sequence[tuple[str, str]] = (), **fields) -> None:
    ts = util.iso()
    reported_cost = cost if reported_cost is None else reported_cost
    paid_usd = 0.0 if reported_cost == 0 and paid_usd is None else paid_usd
    with db() as c:
        for kind, note in before:
            c.execute("INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) VALUES (?,?,?,?,?,?)",
                      (chat_id, kind, note, 0, 0, ts))
        c.execute("INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) VALUES (?,?,?,?,?,?)",
                  (chat_id, sender, text, reported_cost, paid_usd, ts))
        c.execute(f"UPDATE chats SET {''.join(f'{k} = ?, ' for k in fields)}updated = ?, spent = spent + ? WHERE id = ?",
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
            f"SELECT {CHAT_SESSION_COLUMNS} FROM chat_sessions l LEFT JOIN runs r ON r.id = l.run_id {where} "
            "ORDER BY l.linked DESC, l.rowid DESC", args)]


def chat_of_session(run_id: str, connect=reader) -> int | None:
    with connect() as c:
        r = c.execute("SELECT chat_id FROM chat_sessions WHERE run_id = ?", (run_id,)).fetchone()
        return r["chat_id"] if r else None


def chat_sessions_untold() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            f"SELECT {CHAT_SESSION_COLUMNS} FROM chat_sessions l LEFT JOIN runs r ON r.id = l.run_id "
            "WHERE l.told = '' AND COALESCE(r.status, '') != 'running' ORDER BY l.rowid")]


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


def loop_spent_since(since: str, loop: str | None = None, local_providers=()) -> float:
    q = ("SELECT steps.cost_usd, steps.runner, steps.provider, steps.model, steps.auth_mode FROM steps "
         "JOIN runs r ON r.id = steps.run_id WHERE steps.ts >= ? AND r.kind = 'loop'")
    args: list = [since]
    if loop:
        q += " AND r.loop = ?"
        args.append(loop)
    with reader() as c:
        return sum(float(r["cost_usd"] or 0) for r in c.execute(q, args)
                   if billing_of(dict(r), local_providers) == "api_key")


def hold(scope: str, until: str, why: str) -> None:
    with tx() as c:
        links = refresh_links(c)
        ts = util.iso()
        for linked in linked_scopes(scope, links):
            save_hold(c, linked, until, why, ts)


def save_hold(c, scope: str, until: str, why: str, ts: str) -> None:
    c.execute("INSERT INTO holds (scope, until, why, ts) VALUES (?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
              "until = CASE WHEN holds.until = '' OR excluded.until = '' THEN '' "
              "ELSE max(holds.until, excluded.until) END, "
              "why = CASE WHEN holds.until != '' AND (excluded.until = '' OR excluded.until > holds.until) "
              "THEN excluded.why ELSE holds.why END, ts = max(holds.ts, excluded.ts)", (scope, until, why, ts))


def holds(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM holds WHERE scope NOT LIKE ? "
                                           "AND (until > ? OR until = '') ORDER BY until",
                                           (REFRESH_SCOPE + "%", util.iso()))]


def clear_holds(scopes: list[str], set_before: str) -> None:
    with tx() as c:
        links = refresh_links(c)
        linked = {s for scope in scopes for s in linked_scopes(scope, links)}
        c.executemany("DELETE FROM holds WHERE scope = ? AND ts < ?", [(s, set_before) for s in linked])


def refresh_links(c) -> list[tuple[str, str, str, str]]:
    links = []
    for row in c.execute("SELECT scope, why FROM holds WHERE scope LIKE ?", (REFRESH_SCOPE + "%",)):
        try:
            before, after = row["scope"][len(REFRESH_SCOPE):].split(":")
            previous, latest = json.loads(row["why"])
            if before and after and all(isinstance(s, str) and s for s in (previous, latest)):
                links.append((before, after, previous, latest))
        except (ValueError, TypeError):
            continue
    return links


def linked_scopes(scope: str, links: list[tuple[str, str, str, str]]) -> set[str]:
    found, pending = set(), [scope]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        for _, _, previous, latest in links:
            for before, after in ((previous, latest), (latest, previous)):
                if current.startswith(before):
                    linked = after + current[len(before):]
                    if linked not in found:
                        pending.append(linked)
    return found


def refresh_continues(previous: str, latest: str, connect=reader) -> bool:
    if not previous or not latest:
        return False
    if previous == latest:
        return True
    with connect() as c:
        links = refresh_links(c)
    found, pending = set(), [previous]
    while pending:
        current = pending.pop()
        if current == latest:
            return True
        if current not in found:
            found.add(current)
            pending.extend(after for before, after, _, _ in links if before == current and after not in found)
    return False


def record_refresh(before: str, after: str, previous: str, latest: str) -> None:
    if not all((before, after, previous, latest)) or before == after:
        return
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO holds (scope, until, why, ts) VALUES (?,?,?,?)",
                  (REFRESH_SCOPE + before + ":" + after, "", json.dumps([previous, latest]), util.iso()))
        links = refresh_links(c)
        rows = c.execute("SELECT * FROM holds WHERE scope NOT LIKE ? AND (until = '' OR until > ?)",
                         (REFRESH_SCOPE + "%", util.iso())).fetchall()
        for row in rows:
            for scope in linked_scopes(row["scope"], links) - {row["scope"]}:
                save_hold(c, scope, row["until"], row["why"], row["ts"])


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


def runs_marked_running(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY started")]


def add_vm(name: str, run_id: str | None, device: dict, vmid: int | None = None, connect=db) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO vms (name, run_id, device, vmid, created) VALUES (?,?,?,?,?)",
                  (name, run_id, json.dumps(device), vmid, util.iso()))


def note_vm_answered(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("UPDATE vms SET answered = 1 WHERE name = ?", (name,))


def forget_vm(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM vms WHERE name = ?", (name,))


def forget_gone_vm(name: str, unanswered_before: str, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM vms WHERE name = ? AND (answered = 1 OR created < ?)", (name, unanswered_before))


def note_delete_failed(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("UPDATE vms SET failing_since = COALESCE(failing_since, ?) WHERE name = ?", (util.iso(), name))


def vms_of_run(run_id: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "device": json.loads(r["device"])}
                for r in c.execute("SELECT * FROM vms WHERE run_id = ? ORDER BY created", (run_id,))]


def vms_left_over(probes_created_before: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "device": json.loads(r["device"])} for r in c.execute(
            "SELECT * FROM vms v WHERE (v.run_id IS NULL AND v.created < ?) OR (v.run_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM runs r WHERE r.id = v.run_id AND r.status = 'running')) ORDER BY created",
            (probes_created_before,))]


def ticket_runs(repo: str, issue: int | str, limit: int = 10, connect=reader, whole: bool = False) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
                                           + (f"AND {WHOLE_RUN} " if whole else "")
                                           + "ORDER BY started DESC, rowid DESC LIMIT ?", (repo, issue, limit))]


def newest_whole_run(repo: str, issue: int | str) -> dict | None:
    return next(iter(ticket_runs(repo, issue, limit=1, whole=True)), None)


def ticket_runs_marked_running(repo: str, issue: int | str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
                                           "AND status = 'running' ORDER BY started ASC, rowid ASC", (repo, issue))]


def ab_placements(since: str, name: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        labelled = [dict(r) for r in c.execute(
            "SELECT DISTINCT steps.ab, steps.ab_side, steps.atom, r.repo, r.issue FROM steps "
            "JOIN runs r ON r.id = steps.run_id WHERE COALESCE(steps.ab, '') != '' AND r.kind = 'pipeline' "
            "AND r.started >= ?" + (" AND steps.ab = ?" if name else ""), (since, *([name] if name else [])))]
        held = c.execute("SELECT repo, issue, ab_held FROM runs WHERE kind = 'pipeline' AND started >= ? "
                         "AND COALESCE(ab_held, '') != ''", (since,)).fetchall()
    assigned = [{"ab": ab, "ab_side": side, "atom": role, "repo": r["repo"], "issue": r["issue"]}
                for r in held for role, (ab, side, _) in json.loads(r["ab_held"]).items() if not name or ab == name]
    return list({tuple(p.values()): p for p in labelled + assigned}.values())


def ab_held(run_id: str, connect=reader) -> dict[str, tuple[str, str, dict]]:
    with connect() as c:
        row = c.execute("SELECT ab_held FROM runs WHERE id = ?", (run_id,)).fetchone()
    return {role: tuple(held) for role, held in json.loads(row["ab_held"] or "{}").items()} if row else {}


def ticket_runs_since(repo: str, issue: int | str, since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
                                           "AND started >= ? ORDER BY started", (repo, issue, since))]


def later_run(repo: str, issue: int | str, run_id: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? AND {WHOLE_RUN} "
                      "AND rowid > (SELECT rowid FROM runs WHERE id = ?) ORDER BY rowid LIMIT 1",
                      (repo, issue, run_id)).fetchone()
        return dict(r) if r else None


def interrupted_tickets(repo: str) -> list[str]:
    with reader() as c:
        rows = c.execute(f"SELECT issue, status FROM runs WHERE kind = 'pipeline' AND repo = ? AND {WHOLE_RUN} "
                         "AND rowid = (SELECT r2.rowid FROM runs r2 WHERE r2.kind = 'pipeline' "
                         "AND r2.repo = runs.repo AND r2.issue = runs.issue AND COALESCE(r2.only_step, '') = '' "
                         "ORDER BY r2.started DESC, r2.rowid DESC LIMIT 1) "
                         "ORDER BY started, rowid", (repo,))
        return [str(r["issue"]) for r in rows if r["status"] == "interrupted"]


def last_pipeline_starts() -> dict[str, str]:
    with reader() as c:
        return {r[0]: r[1] for r in c.execute("SELECT repo, MAX(started) FROM runs WHERE kind = 'pipeline' "
                                              "GROUP BY repo")}


def running_run(repo: str, issue: int | str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? AND status = 'running' "
                      "ORDER BY started DESC, rowid DESC LIMIT 1", (repo, issue)).fetchone()
        return dict(r) if r else None


def pending_label(repo: str, issue: int | str) -> str:
    with reader() as c:
        r = c.execute("SELECT label_pending FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
                      "AND label_pending IS NOT NULL ORDER BY started DESC, rowid DESC LIMIT 1",
                      (repo, issue)).fetchone()
        return r["label_pending"] if r else ""


def issue_running(repo: str, issue: int | str) -> bool:
    with reader() as c:
        return c.execute("SELECT 1 FROM runs WHERE repo = ? AND issue = ? AND status = 'running' LIMIT 1",
                         (repo, issue)).fetchone() is not None


def probe_get(name: str) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM probes WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None


def probe_set(name: str, ok: bool, detail: str) -> None:
    ts = util.iso()
    prev = probe_get(name) or {}
    last_ok = ts if ok else prev.get("last_ok")
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO probes (name, last_run, last_ok, ok, detail) VALUES (?,?,?,?,?)",
            (name, ts, last_ok, int(ok), detail[:500]),
        )


def alert_get(key: str) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
        return dict(r) if r else None


def alert_set(key: str, issue: int | None, repo: str = "") -> None:
    prev = alert_get(key)
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO alerts (key, issue, opened, last_seen, repo) VALUES (?,?,?,?,?)",
            (key, issue, (prev or {}).get("opened") or util.iso(), util.iso(), repo or (prev or {}).get("repo") or ""),
        )


def alert_clear(key: str) -> None:
    with db() as c:
        c.execute("DELETE FROM alerts WHERE key = ?", (key,))


def probes(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM probes ORDER BY name")]


def alerts(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY opened")]


def add_proposal(run_id: str, p: dict, status: str, pr_url: str = "") -> None:
    with db() as c:
        c.execute(
            "INSERT INTO proposals (run_id, layer, repo, file, change, why, evidence, confidence, status, pr_url, created)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                p.get("layer"),
                p.get("repo"),
                p.get("file"),
                p.get("change"),
                p.get("why"),
                json.dumps(p.get("evidence", [])),
                float(p.get("confidence", 0)),
                status,
                pr_url,
                util.iso(),
            ),
        )


RELEASING_PROPOSALS = ("rejected", "skipped", "filed")


def pin_record(run_id: str) -> bool:
    with db() as c:
        return c.execute("INSERT OR IGNORE INTO record_pins (run_id, pinned) VALUES (?, ?)",
                         (run_id, util.iso())).rowcount > 0


def unpin_record(run_id: str) -> bool:
    with db() as c:
        return c.execute("DELETE FROM record_pins WHERE run_id = ?", (run_id,)).rowcount > 0


def record_pins(connect=reader) -> set[str]:
    with connect() as c:
        return {r["run_id"] for r in c.execute("SELECT run_id FROM record_pins")}


def runs_cited_by_kept_proposals(run_ids, connect=reader) -> set[str]:
    marks = ",".join("?" * len(RELEASING_PROPOSALS))
    with connect() as c:
        rows = c.execute(f"SELECT run_id, evidence FROM proposals WHERE status IS NULL OR status NOT IN ({marks})",
                         RELEASING_PROPOSALS).fetchall()
    return {r for r in run_ids if any(r == row["run_id"] or r in (row["evidence"] or "") for row in rows)}


def proposals(status: list[str] | None = None, limit: int = 200, connect=reader) -> list[dict]:
    q, args = "SELECT * FROM proposals", []
    if status:
        q += f" WHERE status IN ({','.join('?' * len(status))})"
        args += status
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def proposal(proposal_id: int, connect=reader) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    return dict(row) if row else None


def move_proposal(proposal_id: int, before: tuple[str, ...], after: str) -> bool:
    with db() as c:
        return c.execute(f"UPDATE proposals SET status = ? WHERE id = ? AND status IN ({','.join('?' * len(before))})",
                         (after, proposal_id, *before)).rowcount > 0


def forget_proposal(proposal_id: int) -> bool:
    return move_proposal(proposal_id, ("memory", "hand"), "filed")


def set_proposal_status_by_pr(pr_url: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE proposals SET status = ? WHERE pr_url = ?", (status, pr_url))


EVAL_LINEAGE = ("engine_sha", "prompts_sha", "prompts_from", "context_sha", "settings", "base_commit",
                "after_step", "last_step")


def add_eval(run_id: str, repo: str, task: str, passed: bool, seconds: float, cost: float, source: str = "",
             lineage: dict | None = None) -> None:
    lineage = lineage or {}
    with db() as c:
        c.execute(
            f"INSERT INTO evals (run_id, repo, task, passed, seconds, cost_usd, ts, source, {', '.join(EVAL_LINEAGE)}) "
            f"VALUES ({','.join('?' * 16)})",
            (run_id, repo, task, int(passed), seconds, cost, util.iso(), source,
             *(lineage.get(k, 0 if k in ("after_step", "last_step") else "") for k in EVAL_LINEAGE)),
        )


BASELINE_SOURCE = "worktree"


def task_history(repo: str, task: str, limit: int = 5, source: str = BASELINE_SOURCE) -> list[dict]:
    with reader() as c:
        rows = c.execute(f"SELECT passed, {', '.join(EVAL_LINEAGE)} FROM evals "
                         "WHERE repo = ? AND task = ? AND source = ? ORDER BY ts DESC LIMIT ?",
                         (repo, task, source, limit))
        return [{**dict(r), "passed": bool(r["passed"])} for r in rows]


def last_step_id(run_id: str) -> int:
    with reader() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM steps WHERE run_id = ?", (run_id,)).fetchone()[0]


def eval_rows(run_id: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM evals WHERE run_id = ? ORDER BY ts", (run_id,))]


def eval_history(repo: str, limit: int = 20) -> list[dict]:
    with reader() as c:
        rows = c.execute(
            "SELECT run_id, MIN(ts) AS ts, MAX(source) AS source, SUM(passed) AS passed, COUNT(*) AS total,"
            " SUM(cost_usd) AS cost FROM evals WHERE repo = ? GROUP BY run_id ORDER BY ts DESC LIMIT ?",
            (repo, limit),
        )
        return [dict(r) for r in rows]


def evals_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM evals WHERE ts >= ? ORDER BY ts", (since,))]


FEEDBACK_COLUMNS = ("pr_url", "kind", "gh_id", "repo", "issue", "body", "path", "line", "hunk", "thread", "at_once",
                    "created", "pr_state", "status", "asks", "recorded")


def feedback_keys(pr_url: str) -> set[tuple[str, int]]:
    with reader() as c:
        return {(r["kind"], r["gh_id"]) for r in c.execute("SELECT kind, gh_id FROM feedback WHERE pr_url = ?",
                                                           (pr_url,))}


def add_feedback(rows: list[dict]) -> int:
    with db() as c:
        before = c.total_changes
        c.executemany(f"INSERT OR IGNORE INTO feedback ({', '.join(FEEDBACK_COLUMNS)}) "
                      f"VALUES ({', '.join('?' * len(FEEDBACK_COLUMNS))})",
                      [(*(r.get(k) for k in FEEDBACK_COLUMNS[:-1]), util.iso()) for r in rows])
        return c.total_changes - before


def feedback_due(repo: str, quiet_since: str) -> list[str]:
    with reader() as c:
        rows = c.execute("SELECT issue, MAX(at_once) AS at_once, MAX(created) AS last FROM feedback f "
                         "WHERE repo = ? AND status = 'new' AND EXISTS (SELECT 1 FROM runs WHERE runs.repo = f.repo "
                         f"AND runs.issue = f.issue AND {OPEN_PR_RUN} "
                         "AND ' ' || runs.pr_url || ' ' LIKE '% ' || f.pr_url || ' %') "
                         "GROUP BY issue ORDER BY last", (repo,))
        return [str(r["issue"]) for r in rows if r["at_once"] or r["last"] <= quiet_since]


def close_feedback(pr_url: str, pr_state: str) -> None:
    with db() as c:
        c.execute("UPDATE feedback SET pr_state = ?, status = CASE WHEN status IN ('new', 'failed') THEN 'recorded' "
                  "ELSE status END WHERE pr_url = ?", (pr_state, pr_url))


def feedback_waiting(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT repo, issue, COUNT(*) AS n, MAX(at_once) AS at_once, "
                                           "MAX(CASE WHEN at_once THEN created END) AS submitted, "
                                           "MAX(created) AS last FROM feedback WHERE status = 'new' "
                                           "GROUP BY repo, issue ORDER BY last")]


def open_feedback(pr_urls: list[str]) -> list[dict]:
    if not pr_urls:
        return []
    with reader() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM feedback WHERE pr_url IN ({','.join('?' * len(pr_urls))}) "
            "AND status IN ('new', 'taken', 'failed') ORDER BY created", pr_urls)]


def take_feedback(rows: list[dict], run_id: str) -> None:
    with db() as c:
        c.executemany("UPDATE feedback SET status = 'taken', run_id = ? WHERE pr_url = ? AND kind = ? AND gh_id = ?",
                      [(run_id, r["pr_url"], r["kind"], r["gh_id"]) for r in rows])


def settle_feedback(run_id: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE feedback SET status = ? WHERE run_id = ? AND status = 'taken'", (status, run_id))


def feedback_between(since: str, until: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback WHERE COALESCE(recorded, created) >= ? "
                                           "AND COALESCE(recorded, created) < ? ORDER BY pr_url, created",
                                           (since, until))]


def mined_until() -> str | None:
    with reader() as c:
        return c.execute("SELECT MAX(mined_until) FROM runs WHERE kind = 'miner'").fetchone()[0]


def add_mined(pr_url: str, repo: str, issue: str, run_id: str, status: str, why: str = "", task: str = "") -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO mined (pr_url, repo, issue, run_id, status, why, task, ts) "
                  "VALUES (?,?,?,?,?,?,?,?)", (pr_url, repo, issue, run_id, status, why, task, util.iso()))


def mined_row(pr_url: str) -> dict:
    with reader() as c:
        row = c.execute("SELECT * FROM mined WHERE pr_url = ?", (pr_url,)).fetchone()
    return dict(row) if row else {}


def mined_urls() -> set[str]:
    with reader() as c:
        return {r["pr_url"] for r in c.execute("SELECT pr_url FROM mined")}


TRIAGE_COLUMNS = ("ref", "source", "tracker", "repo", "route", "title", "url", "updated", "ready", "code", "size",
                  "questions", "plan", "why", "cost", "ts", "kind", "repos", "assumptions", "in_sprint", "parent",
                  "closed", "checks")
TRIAGE_LISTS = ("questions", "plan", "repos", "assumptions", "checks")
APPROVAL_COLUMNS = ("ref", "repo", "route", "title", "url", "kind", "size", "plan", "assumptions", "why", "ts", "tracker",
                    "sent_by", "checks")
APPROVAL_LISTS = ("plan", "assumptions", "checks")
RESEARCH_COLUMNS = ("ref", "source", "title", "url", "updated", "status", "summary", "report", "error", "run_id",
                    "cost", "ts", "ticket", "basis", "answered_by", "filed_path", "filed_url", "moved", "delivered")


def lists_read(row: dict, keys: tuple[str, ...]) -> dict:
    return {**row, **{k: json.loads(row.get(k) or "[]") for k in keys}}


def lists_written(row: dict, columns: tuple[str, ...], keys: tuple[str, ...]) -> tuple:
    return tuple(json.dumps(row.get(k) or []) if k in keys else row.get(k) for k in columns)


def insert_sql(table: str, columns: tuple[str, ...], verb: str = "INSERT") -> str:
    return f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"


def triage_rows(connect=reader) -> list[dict]:
    with connect() as c:
        return [lists_read(dict(r), TRIAGE_LISTS) for r in c.execute("SELECT * FROM triage")]


def triage_replace(source: str, rows: list[dict]) -> None:
    refs = [r["ref"] for r in rows]
    with db() as c:
        c.execute("DELETE FROM triage WHERE source = ?", (source,))
        c.executemany("DELETE FROM triage WHERE ref = ?", [(ref,) for ref in refs])
        c.executemany(insert_sql("triage", TRIAGE_COLUMNS),
                      [lists_written(r, TRIAGE_COLUMNS, TRIAGE_LISTS) for r in rows])


def triage_put(row: dict) -> None:
    with db() as c:
        c.execute(insert_sql("triage", TRIAGE_COLUMNS, "INSERT OR REPLACE"), lists_written(row, TRIAGE_COLUMNS, TRIAGE_LISTS))


def triage_drop(ref: str) -> None:
    with db() as c:
        c.execute("DELETE FROM triage WHERE ref = ?", (ref,))


def triage_approve(row: dict, sent_by: str) -> None:
    with db() as c:
        c.execute(insert_sql("triage_approvals", APPROVAL_COLUMNS, "INSERT OR REPLACE"),
                  lists_written({**row, "ts": util.iso(), "sent_by": sent_by}, APPROVAL_COLUMNS, APPROVAL_LISTS))


def triage_approvals(since: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM triage_approvals WHERE ts >= ? ORDER BY ts DESC", (since,))
        return [lists_read(dict(r), APPROVAL_LISTS) for r in rows]


def triage_approval(ref: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM triage_approvals WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return lists_read(dict(r), APPROVAL_LISTS) if r else None


def research_rows(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM research ORDER BY ts DESC")]


def research_get(ref: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM research WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return dict(r) if r else None


def research_set(ref: str, **fields) -> None:
    row = {**(research_get(ref) or {"ref": ref, "cost": 0.0}), **fields, "ts": util.iso()}
    with db() as c:
        c.execute(insert_sql("research", RESEARCH_COLUMNS, "INSERT OR REPLACE"),
                  tuple(row.get(k) for k in RESEARCH_COLUMNS))


def research_keep(ref: str, **fields) -> None:
    with db() as c:
        c.execute(f"UPDATE research SET {', '.join(f'{k} = ?' for k in fields)} WHERE ref = ? COLLATE NOCASE",
                  (*fields.values(), ref))


def triage_prune(sources: list[str]) -> None:
    with db() as c:
        c.execute(f"DELETE FROM triage WHERE source NOT IN ({','.join('?' * len(sources))})", sources)


def add_usage(row: dict, keep_days: float) -> None:
    with db() as c:
        c.execute(insert_sql("usage", USAGE_COLUMNS), tuple(row.get(k) for k in USAGE_COLUMNS))
        c.execute("DELETE FROM usage WHERE at < ?", (util.iso(util.now() - timedelta(days=keep_days)),))


OPEN_CAPTURE = "status IN ('draft', 'checking', 'filing')"
EDITABLE_CAPTURE = "status IN ('draft', 'checking')"


def capture_draft(connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute(f"SELECT * FROM captures WHERE {OPEN_CAPTURE}").fetchone()
        if not r:
            return None
        draft = dict(r)
        draft["evidence"] = [{**dict(e), "inspection": json.loads(e["inspection"] or "{}")} for e in c.execute(
            "SELECT * FROM capture_evidence WHERE capture = ? ORDER BY id", (draft["id"],))]
        draft["checks"] = {k["name"]: {**dict(k), "detail": json.loads(k["detail"] or "{}")} for k in c.execute(
            "SELECT * FROM capture_checks WHERE capture = ?", (draft["id"],))}
        return draft


def capture_move(draft_id: int, revision: int, was: str, to: str, delivery: str | None = None) -> bool:
    with tx() as c:
        note = "" if delivery is None else ", delivery = ?"
        args = (to, *(() if delivery is None else (delivery,)), draft_id, was, revision)
        return c.execute(f"UPDATE captures SET status = ?{note} WHERE id = ? AND status = ? AND revision = ?",
                         args).rowcount == 1


def capture_begin_checks(draft_id: int, prints: dict[str, str]) -> bool:
    with tx() as c:
        if not c.execute("SELECT 1 FROM captures WHERE id = ? AND status = 'checking'", (draft_id,)).fetchone():
            return False
        for name, print_ in prints.items():
            c.execute("INSERT OR REPLACE INTO capture_checks (capture, name, result, reason, detail, fingerprint, checked) "
                      "VALUES (?, ?, 'running', '', '{}', ?, ?)", (draft_id, name, print_, util.iso()))
        return True


def capture_written(draft_id: int, print_: str, title: str, body: str, note: str) -> None:
    with db() as c:
        c.execute("UPDATE captures SET written_print = ?, written_title = ?, written_body = ?, written_note = ? "
                  "WHERE id = ?", (print_, title, body, note, draft_id))


def capture_linked(evidence_id: int, sha256: str, link: str) -> None:
    with db() as c:
        c.execute("UPDATE capture_evidence SET link = ? WHERE id = ? AND sha256 = ?", (link, evidence_id, sha256))


def capture_note(draft_id: int | None, text: str) -> None:
    with db() as c:
        c.execute("UPDATE captures SET delivery = ? WHERE status = 'filing'" + ("" if draft_id is None else " AND id = ?"),
                  (text, *(() if draft_id is None else (draft_id,))))


def capture_filed(draft_id: int, number: int, url: str, title: str) -> bool:
    now = util.iso()
    with tx() as c:
        changed = c.execute("UPDATE captures SET status = 'filed', issue = ?, issue_url = ?, issue_title = ?, filed = ?, "
                            "updated = ?, delivery = '' WHERE id = ? AND status = 'filing'",
                            (number, url, title, now, now, draft_id)).rowcount
        if changed:
            c.execute("UPDATE verdicts SET outcome = 'filed', outcome_at = ? WHERE run_id = ? AND kind = 'capture'",
                      (now, f"capture-{draft_id}"))
        return bool(changed)


def capture_filed_row(draft_id: int | None = None, connect=reader) -> dict | None:
    with connect() as c:
        if draft_id is None:
            r = c.execute("SELECT * FROM captures WHERE status = 'filed' ORDER BY filed DESC, id DESC").fetchone()
        else:
            r = c.execute("SELECT * FROM captures WHERE status = 'filed' AND id = ?", (draft_id,)).fetchone()
        return dict(r) if r else None


def capture_filing(connect=reader) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM captures WHERE status = 'filing'").fetchone() is not None


def capture_check_done(draft_id: int, name: str, result: str, reason: str, detail: dict, cost: float) -> bool:
    with tx() as c:
        changed = c.execute(
            "UPDATE capture_checks SET result = ?, reason = ?, detail = ?, checked = ? WHERE capture = ? AND name = ? "
            "AND result = 'running' AND EXISTS (SELECT 1 FROM captures WHERE id = ? AND status = 'checking')",
            (result, reason, json.dumps(detail), util.iso(), draft_id, name, draft_id)).rowcount
        if changed:
            c.execute("UPDATE captures SET jev_cost_usd = COALESCE(jev_cost_usd, 0) + ? WHERE id = ?", (cost, draft_id))
        return bool(changed)


def capture_inspected(evidence_id: int, sha256: str, inspection: dict, cost: float) -> bool:
    with tx() as c:
        row = c.execute("SELECT capture FROM capture_evidence WHERE id = ? AND sha256 = ?",
                        (evidence_id, sha256)).fetchone()
        if not row:
            return False
        c.execute("UPDATE capture_evidence SET inspection = ? WHERE id = ?", (json.dumps(inspection), evidence_id))
        c.execute("UPDATE captures SET inspect_cost_usd = COALESCE(inspect_cost_usd, 0) + ? WHERE id = ?",
                  (cost, row["capture"]))
        return True


def capture_end_checks(draft_id: int | None, reason: str) -> None:
    with tx() as c:
        args = () if draft_id is None else (draft_id,)
        c.execute("UPDATE capture_checks SET result = 'interrupted', reason = ?, checked = ? WHERE result = 'running'"
                  + ("" if draft_id is None else " AND capture = ?"), (reason, util.iso(), *args))
        c.execute("UPDATE captures SET status = 'draft' WHERE status = 'checking'"
                  + ("" if draft_id is None else " AND id = ?"), args)


def capture_checking(connect=reader) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM captures WHERE status = 'checking'").fetchone() is not None


def capture_evidence(evidence_id: int, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT e.* FROM capture_evidence e JOIN captures k ON k.id = e.capture "
                      "WHERE e.id = ? AND k.status IN ('draft', 'checking', 'filing')", (evidence_id,)).fetchone()
        return dict(r) if r else None


def capture_stage(row: dict, admit, keep) -> tuple[dict, bool]:
    now = util.iso()
    with tx() as c:
        draft = c.execute(f"SELECT id, status FROM captures WHERE {OPEN_CAPTURE}").fetchone()
        draft_id = draft["id"] if draft else c.execute(
            "INSERT INTO captures (status, revision, created, updated) VALUES ('draft', 0, ?, ?)", (now, now)).lastrowid
        held = [dict(r) for r in c.execute("SELECT * FROM capture_evidence WHERE capture = ? ORDER BY id", (draft_id,))]
        admit(held, draft["status"] if draft else "draft")
        same = next((r for r in held if r["sha256"] == row["sha256"]), None)
        if same:
            return same, True
        keep(draft_id)
        row = {**row, "capture": draft_id, "added": now}
        columns = tuple(row)
        row["id"] = c.execute(insert_sql("capture_evidence", columns), tuple(row[k] for k in columns)).lastrowid
        c.execute("UPDATE captures SET revision = revision + 1, updated = ? WHERE id = ?", (now, draft_id))
        return row, False


def capture_drop_evidence(evidence_id: int) -> dict | None:
    with tx() as c:
        r = c.execute("SELECT e.* FROM capture_evidence e JOIN captures k ON k.id = e.capture "
                      "WHERE e.id = ? AND k.status IN ('draft', 'checking')", (evidence_id,)).fetchone()
        if not r:
            return None
        c.execute("DELETE FROM capture_evidence WHERE id = ?", (evidence_id,))
        c.execute("UPDATE captures SET revision = revision + 1, updated = ? WHERE id = ?", (util.iso(), r["capture"]))
        return dict(r)


def capture_save(**fields) -> bool:
    now = util.iso()
    names = list(fields)
    sets = "".join(f"{n} = ?, " for n in names)
    with tx() as c:
        changed = c.execute(f"UPDATE captures SET {sets}revision = revision + 1, updated = ? WHERE {EDITABLE_CAPTURE}",
                            (*fields.values(), now)).rowcount
        if changed:
            return True
        if c.execute("SELECT 1 FROM captures WHERE status = 'filing'").fetchone():
            return False
        c.execute(f"INSERT INTO captures ({', '.join(names)}, status, revision, created, updated) "
                  f"VALUES ({', '.join('?' * len(names))}, 'draft', 1, ?, ?)", (*fields.values(), now, now))
        return True


def capture_discard() -> int | None:
    with tx() as c:
        draft = c.execute(f"SELECT id FROM captures WHERE {EDITABLE_CAPTURE}").fetchone()
        if not draft:
            return None
        c.execute("DELETE FROM capture_evidence WHERE capture = ?", (draft["id"],))
        c.execute("DELETE FROM capture_checks WHERE capture = ?", (draft["id"],))
        c.execute("UPDATE captures SET status = 'discarded', revision = revision + 1, updated = ? WHERE id = ?",
                  (util.iso(), draft["id"]))
        return draft["id"]


def usage_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM usage WHERE at >= ? ORDER BY at", (since,))]


def task_add(repo: str, body: str, title: str = "", kind: str = "feat", parent: str = "") -> str:
    ts = util.iso()
    digits = 4
    with db() as c:
        while True:
            tid = f"t-{secrets.randbits(4 * digits):0{digits}x}"
            try:
                c.execute("INSERT INTO tasks (id, repo, title, body, kind, state, parent, created, updated) "
                          "VALUES (?,?,?,?,?,'todo',?,?,?)", (tid, repo, title, body, kind, parent, ts, ts))
                return tid
            except sqlite3.IntegrityError:
                digits += 1


def task_get(tid: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        return dict(r) if r else None


def tasks(repo: str = "", in_state: str = "", connect=reader) -> list[dict]:
    q, args = "SELECT * FROM tasks WHERE 1=1", []
    for column, value in (("repo", repo), ("state", in_state)):
        if value:
            q += f" AND {column} = ?"
            args.append(value)
    with connect() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY created, rowid", args)]


def task_children(parent: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks WHERE parent = ? ORDER BY created, rowid", (parent,))]


def task_set_state(tid: str, new: str, from_states: tuple[str, ...] = ()) -> bool:
    q, args = "UPDATE tasks SET state = ?, updated = ? WHERE id = ?", [new, util.iso(), tid]
    if from_states:
        q += f" AND state IN ({','.join('?' * len(from_states))})"
        args.extend(from_states)
    with db() as c:
        return c.execute(q, args).rowcount > 0


def task_comment(tid: str, author: str, body: str) -> bool:
    ts = util.iso()
    with db() as c:
        if not c.execute("UPDATE tasks SET updated = ? WHERE id = ?", (ts, tid)).rowcount:
            return False
        c.execute("INSERT INTO task_comments (task, author, body, ts) VALUES (?,?,?,?)", (tid, author, body, ts))
        return True


def task_comments(tid: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT author, body, ts FROM task_comments WHERE task = ? ORDER BY rowid",
                                           (tid,))]


def tasks_replied(repo: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE repo = ? AND state = 'needs-info' AND "
            "(SELECT author = 'owner' OR author LIKE 'u:%' FROM task_comments c "
            "WHERE c.task = tasks.id ORDER BY c.rowid DESC LIMIT 1) ORDER BY created, rowid", (repo,))]


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


def push_add(endpoint: str, p256dh: str, auth: str, label: str, contact: str) -> None:
    with db() as c:
        c.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth, label, contact, created) "
                  "VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
                  "auth = excluded.auth, label = excluded.label, contact = excluded.contact",
                  (endpoint, p256dh, auth, label, contact, util.iso()))


def push_list(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM push_subscriptions ORDER BY created, rowid")]


def push_drop(endpoints: list[str]) -> int:
    with db() as c:
        return sum(c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (e,)).rowcount for e in endpoints)


def push_succeeded(endpoints: list[str]) -> None:
    with db() as c:
        c.executemany("UPDATE push_subscriptions SET last_success = ? WHERE endpoint = ?",
                      [(util.iso(), e) for e in endpoints])


def asks_asked_before(iso: str) -> list[dict]:
    with reader() as c:
        return [ask_row(r) for r in c.execute("SELECT * FROM asks WHERE status = 'open' AND asked < ? "
                                              "ORDER BY asked, rowid", (iso,))]
