from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta

from .. import util
from ..agent import billing_of
from .db import db, reader, tx

TABLES = {
    "runs": "free command firings: 14 days; others kept",
    "steps": "kept",
    "feed": "with run",
    "record_pins": "kept",
    "reports": "kept",
    "run_purges": "until file cleanup",
}


OPEN_PR_RUN = "(runs.status = 'pr' OR (runs.status IN ('failed', 'stopped') AND runs.pr_url != ''))"

WHOLE_RUN = "COALESCE(only_step, '') = ''"

FIRINGS = ("(runs.kind = 'loop' AND runs.loop_kind = 'command' AND runs.status IN ('done', 'skipped') "
           "AND COALESCE(runs.cost_usd, 0) = 0)")


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
        from .. import config

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


def runs_marked_running(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY started")]


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


def last_step_id(run_id: str) -> int:
    with reader() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM steps WHERE run_id = ?", (run_id,)).fetchone()[0]


def prune(c, days: float = 14, lines_days: float = 90) -> None:
    since = util.iso(util.now() - timedelta(days=days))
    lines_since, now = util.iso(util.now() - timedelta(days=lines_days)), util.iso()
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


def running_runs(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
        return []
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM runs WHERE status='running'")]


def insert_run(run_id: str, row: dict) -> None:
    cols, marks = ", ".join(row), ", ".join("?" * len(row))
    with db() as c:
        c.execute(f"INSERT INTO runs (id, {cols}) VALUES (?, {marks})", (run_id, *row.values()))


def insert_steps(run_id: str, name: str, calls, effort: str = "", tier: str = "", kind: str = "",
                 try_no: int = 0, auth_mode: str = "") -> None:
    with db() as c:
        for res, seconds in calls:
            c.execute("INSERT INTO steps (run_id, step, runner, model, provider, ok, cost_usd, tokens_in, tokens_out,"
                      " turns, seconds, error, ts, first_context_tokens, effort, tier, try, kind, cache_read, cache_write,"
                      " api_seconds, auth_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                      "COALESCE(NULLIF(?, 0), (SELECT COALESCE(MAX(try), 0) + 1 FROM steps WHERE run_id = ? AND step = ?)),"
                      "?,?,?,?,?)",
                      (run_id, name, res.runner, res.model, res.provider, int(res.ok), res.cost_usd, res.tokens_in,
                       res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], util.iso(),
                       res.first_context_tokens, effort or "", tier or "", try_no, run_id, name, kind or "", res.cache_read,
                       res.cache_write, round(res.api_seconds, 2), auth_mode or res.auth_mode))
            c.execute("UPDATE runs SET cost_usd = COALESCE(cost_usd, 0) + ?, tokens_in = COALESCE(tokens_in, 0) + ?, "
                      "tokens_out = COALESCE(tokens_out, 0) + ? WHERE id = ?",
                      (res.cost_usd, res.tokens_in, res.tokens_out, run_id))


def label_step(run_id: str, step: str, try_no: int, **labels) -> None:
    with db() as c:
        c.execute(f"UPDATE steps SET {', '.join(f'{k} = ?' for k in labels)} WHERE run_id = ? AND step = ? AND try = ?",
                  (*labels.values(), run_id, step, try_no))


def hold_ab(run_id: str, role: str, name: str, side: str, arm: dict) -> None:
    with db() as c:
        row = c.execute("SELECT ab_held FROM runs WHERE id = ?", (run_id,)).fetchone()
        held = {**json.loads((row["ab_held"] if row else "") or "{}"), role: [name, side, arm]}
        c.execute("UPDATE runs SET ab_held = ? WHERE id = ?", (json.dumps(held), run_id))


def note(run_id: str, **fields) -> None:
    if "status" in fields:
        raise ValueError("use move to change a run's status")
    with db() as c:
        c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), run_id))


def checkpoint(run_id: str, active: float, verified: float, opened: float, repairs: int, at: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE runs SET active_seconds=?, verify_seconds=?, verify_open_seconds=?, "
                     "repair_calls=?, measured_at=? WHERE id=?", (active, verified, opened, repairs, at, run_id))


def move(run_id: str, to: str, expect: tuple[str, ...], **fields) -> bool:
    if not expect:
        return False
    fields = {**fields, "status": to}
    with db() as c:
        return c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} "
                         f"WHERE id = ? AND status IN ({','.join('?' * len(expect))})",
                         (*fields.values(), run_id, *expect)).rowcount > 0


def ask_stop(run_id: str) -> dict | None:
    with db() as c:
        c.execute("UPDATE runs SET stop_asked = ? WHERE id = ? AND status = 'running'", (util.iso(), run_id))
        r = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(r) if r else None


def settle(run_id: str, fields: dict, end_line: str = "") -> dict | None:
    with db() as c:
        if not c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ? AND status = 'running'",
                         (*fields.values(), run_id)).rowcount:
            return None
        if end_line:
            insert_feed(run_id, "end", end_line)
        return dict(c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def finished_runs_since(since: str, until: str = "9999") -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE status != 'running' "
                                           "AND COALESCE(finished, started) >= ? AND COALESCE(finished, started) < ? "
                                           "ORDER BY started", (since, until))]


def purge_inventory() -> tuple[set[str], set[str]]:
    with db() as c:
        queued = {r["run_id"] for r in c.execute("SELECT run_id FROM run_purges")}
        known = {r["id"] for r in c.execute("SELECT id FROM runs")}
    return queued, known


def forget_purge(run_id: str) -> None:
    with db() as c:
        c.execute("DELETE FROM run_purges WHERE run_id = ?", (run_id,))


def mined_until() -> str | None:
    with reader() as c:
        return c.execute("SELECT MAX(mined_until) FROM runs WHERE kind = 'miner'").fetchone()[0]
