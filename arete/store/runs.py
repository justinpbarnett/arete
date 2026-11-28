from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta

from .. import refusals, util
from ..agent import billing_of
from . import events, writes
from .db import SPEC_DELIVERY_SCHEMA, WORKER_EXCHANGE_SCHEMA, db, migration_applied, reader, tx

TABLES = {
    "runs": "free command firings: 14 days; others kept",
    "steps": "kept",
    "feed": "with run",
    "record_pins": "kept",
    "reports": "kept",
    "run_purges": "until file cleanup",
    "environment_requeues": "kept",
    "environment_holds": "with run",
    "environment_hold_prs": "with run",
    "environment_gap_creations": "kept",
    "environment_budget_resets": "kept",
    "pr_gate_refreshes": "until refreshed or the PR head changes",
}


OPEN_PR_RUN = "(runs.status = 'pr' OR (runs.status IN ('failed', 'stopped') AND runs.pr_url != ''))"

WHOLE_RUN = "COALESCE(only_step, '') = ''"
LIVE_RUN = "dry_run = 0 AND status != 'dry-run'"

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
        return {column: [r[0] for r in c.execute(f"SELECT DISTINCT {column} FROM runs WHERE NOT {FIRINGS} "  # noqa: S608
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
        rows = c.execute("SELECT run_id, cost_usd, runner, provider, model, auth_mode FROM steps "  # noqa: S608
                         f"WHERE run_id IN ({','.join('?' * len(run_ids))})", run_ids)
        for r in rows:
            if billing_of(dict(r), local_providers) == "api_key":
                paid[r[0]] = paid.get(r[0], 0.0) + float(r["cost_usd"] or 0)
    return {k: round(v, 6) for k, v in paid.items()}


def merged_runs(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM runs WHERE kind = 'pipeline' AND status = 'merged' AND {WHOLE_RUN} "  # noqa: S608
            "AND COALESCE(pr_url, '') != '' ORDER BY finished, id")]


def runs_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE started >= ? ORDER BY started", (since,))]


def runs_reading_pr_comments(rejected_since: str, limit: int = 200) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND pr_url != '' "  # noqa: S608
                                           f"AND ({OPEN_PR_RUN} OR (status = 'rejected' AND finished >= ?)) "
                                           "ORDER BY started DESC LIMIT ?", (rejected_since, limit))]


def open_runs(repo: str, issue: int | str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE repo = ? AND issue = ? AND {OPEN_PR_RUN} "  # noqa: S608
                                           "ORDER BY started DESC", (repo, issue))]


DRAFT_READY = "run.draft-review.ready"
DRAFT_OBSERVED = "run.draft-review.observed"
DRAFT_RECEIPT = ("SELECT e.at FROM events_log e WHERE e.subject = 'run:' || r.id AND e.kind = ? "
                 "AND json_extract(e.data, '$.pr_url') = r.pr_url")


def ticket_attention_runs(*, questions: bool = False) -> list[dict]:
    with reader() as c:
        return [dict(row) for row in c.execute(f"""
            SELECT r.* FROM runs r WHERE r.kind = 'pipeline' AND r.issue != '' AND r.repo != ''
                AND COALESCE(r.stop_asked, '') = '' AND {WHOLE_RUN} AND {LIVE_RUN}
                AND ((r.status = 'pr' AND r.pr_url != '' AND COALESCE(r.environment_retry_of, '') = ''
                      AND NOT EXISTS ({DRAFT_RECEIPT})) OR (? AND r.status = 'needs-info' AND NOT EXISTS (
                    SELECT 1 FROM runs later WHERE later.repo = r.repo AND later.issue = r.issue
                      AND later.kind = 'pipeline' AND {WHOLE_RUN} AND {LIVE_RUN}
                      AND (later.started, later.rowid) > (r.started, r.rowid)))) ORDER BY r.started DESC, r.rowid DESC
        """, (DRAFT_READY, questions))]  # noqa: S608

def draft_review_at(run_id: str, receipt: str = DRAFT_READY) -> str | None:
    with reader() as c:
        row = c.execute(f"SELECT ({DRAFT_RECEIPT} ORDER BY e.seq LIMIT 1) FROM runs r WHERE r.id = ?",  # noqa: S608
                        (receipt, run_id)).fetchone()
        return row[0] if row else None

def superseded_at(run_id: str) -> str | None:
    with reader() as c:
        row = c.execute("SELECT at FROM events_log WHERE subject = ? AND kind = 'run.changed' "
                        "AND json_extract(data, '$.status') = 'superseded' ORDER BY ver LIMIT 1",
                        (f"run:{run_id}",)).fetchone()
        return row[0] if row else None


def execution_finished(row: dict) -> tuple[str | None, str | None]:
    subject, status, finished = f"run:{row['id']}", row["status"], row.get("finished")
    with reader() as c:
        ended, rejected = c.execute("""
            SELECT (SELECT json_extract(data, '$.finished') FROM events_log
                    WHERE subject = :run AND kind = 'run.ended' ORDER BY ver LIMIT 1),
                   (:status = 'pr' AND EXISTS (SELECT 1 FROM events_log WHERE subject = :run AND kind = 'run.changed'
                       AND json_extract(data, '$.status') = 'rejected' AND json_extract(data, '$.finished') = :finished))
        """, {"run": subject, "status": status, "finished": finished}).fetchone()
    return ended, None if rejected or status in ("merged", "rejected") else finished

def note_draft_review(run_id: str, pr_url: str, *, ready: bool) -> None:
    kind = DRAFT_READY if ready else DRAFT_OBSERVED
    with tx() as c:
        if c.execute(f"SELECT 1 FROM runs r WHERE id = ? AND status = 'pr' AND pr_url = ? "  # noqa: S608
                     f"AND NOT EXISTS ({DRAFT_RECEIPT}) AND NOT EXISTS ({DRAFT_RECEIPT})",
                     (run_id, pr_url, DRAFT_READY, kind)).fetchone():
            events.emit(kind, f"run:{run_id}", cls="durable", data={"pr_url": pr_url})


HOLD_RELEASED = "run.environment-hold.released"
OWNER_REVIEW_ENDED = "run.environment-hold.owner-review-ended"
HOLD_LINKS = f"""
FROM environment_holds h JOIN runs r ON h.run_id = r.id
LEFT JOIN runs retry ON retry.environment_retry_of = r.id AND retry.dry_run = 0 AND retry.status != 'dry-run'
LEFT JOIN events_log rel ON rel.subject = 'run:' || r.id AND rel.kind = '{HOLD_RELEASED}'
LEFT JOIN events_log ended ON ended.subject = 'run:' || r.id AND ended.kind = '{OWNER_REVIEW_ENDED}'
"""
HOLD_LIFECYCLE = """
    h.released, rel.at AS released_at,
    COALESCE(json_extract(rel.data, '$.state'), CASE WHEN h.released = 1 THEN 'legacy' END) AS release_state,
    retry.id AS retry_id, retry.status AS retry_status, retry.started AS retry_started_at,
    json_extract(rel.data, '$.owner_review_started_at') AS owner_review_started_at,
    ended.at AS owner_review_ended_at
"""


def _restore_legacy_owner_review(hold: dict, repo: str, issue: str, run_id: str) -> dict:
    if hold["released"] == 1 and hold["released_at"] is None and (retry_id := hold["retry_id"]):
        from . import ticket_progress

        if started_at := ticket_progress.owner_review_started_at(repo, issue, run_id, retry_id):
            hold.update(release_state="owner-review", owner_review_started_at=started_at)
    return hold


def environment_holds(repo: str) -> list[dict]:
    with reader() as c:
        rows = c.execute(f"""
            SELECT * FROM (SELECT r.*, {HOLD_LIFECYCLE} {HOLD_LINKS} WHERE r.repo = ? AND r.dry_run = 0)
            WHERE (released = 0 AND status NOT IN ('running', 'dry-run'))
               OR (owner_review_ended_at IS NULL AND (release_state = 'owner-review'
                    OR (released = 1 AND released_at IS NULL AND retry_id IS NOT NULL)))
            ORDER BY started, id
        """, (repo,))  # noqa: S608
        holds = (_restore_legacy_owner_review(dict(row), repo, row["issue"], row["id"]) for row in rows)
        return [hold for hold in holds if not hold["released"] or hold["release_state"] == "owner-review"]


def environment_owner_review_active(row: dict) -> bool:
    if row["retry_status"] != "pr":
        return False
    newest = newest_live_whole_run(row["repo"], row["issue"]) or {}
    return newest.get("id") == row["retry_id"] and not newest.get("stop_asked")


def save_environment_hold(run_id: str, prs: list[tuple[str, int, str, str]]) -> None:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO environment_holds (run_id) VALUES (?)", (run_id,))
        c.executemany("INSERT OR IGNORE INTO environment_hold_prs (run_id, repo, number, url, head) "
                      "VALUES (?, ?, ?, ?, ?)", [(run_id, *pr) for pr in prs])


def held_environment_branch(repo: str, issue: str) -> str:
    with reader() as c:
        row = c.execute("SELECT r.branch FROM environment_holds h JOIN runs r ON h.run_id = r.id "
                        "WHERE r.repo = ? AND r.issue = ? AND r.status = 'pr' AND r.dry_run = 0 AND h.released = 0 "
                        "ORDER BY r.rowid DESC LIMIT 1", (repo, issue)).fetchone()
    return (row["branch"] or "") if row else ""


def environment_hold(run_id: str) -> dict:
    with reader() as c:
        row = c.execute(f"SELECT h.run_id, h.waits, r.repo, r.issue, {HOLD_LIFECYCLE} {HOLD_LINKS} WHERE h.run_id = ?",
                        (run_id,)).fetchone()
        if row is None:
            return {}
        heads = {r["url"]: r["head"] for r in c.execute(
            "SELECT url, head FROM environment_hold_prs WHERE run_id = ? ORDER BY repo, number", (run_id,))}
        hold = dict(row)
        hold = _restore_legacy_owner_review(hold, hold.pop("repo"), hold.pop("issue"), run_id)
        return {**hold, "heads": heads, "waits": json.loads(hold["waits"])}


def set_environment_waits(run_id: str, waits: dict[str, tuple[str, int]]) -> None:
    with db() as c:
        c.execute("UPDATE environment_holds SET waits = ? WHERE run_id = ? AND released = 0",
                  (json.dumps(waits, sort_keys=True), run_id))


def release_environment_hold(run_id: str, state: str = "inactive") -> None:
    with db() as c:
        if not c.execute("UPDATE environment_holds SET released = 1 WHERE run_id = ? AND released = 0",
                         (run_id,)).rowcount:
            return
        data = {"state": state}
        if state == "owner-review":
            retry = c.execute(f"SELECT finished FROM runs WHERE environment_retry_of = ? AND {LIVE_RUN}",  # noqa: S608
                              (run_id,)).fetchone()
            data["owner_review_started_at"] = (retry and retry["finished"]) or util.iso()
        events.emit(HOLD_RELEASED, f"run:{run_id}", cls="durable", data=data)


def end_environment_owner_review(run_id: str) -> None:
    with db():
        hold = environment_hold(run_id)
        if hold.get("owner_review_started_at") and not hold["owner_review_ended_at"]:
            events.emit(OWNER_REVIEW_ENDED, f"run:{run_id}", cls="durable", data={})


def environment_waits(repo: str, issue: str) -> list[tuple[str, int]]:
    with reader() as c:
        return [(r[0], r[1]) for r in c.execute(
            "SELECT DISTINCT json_extract(w.value, '$[0]'), json_extract(w.value, '$[1]') "
            "FROM environment_holds h JOIN runs r ON h.run_id = r.id, json_each(h.waits) w "
            "WHERE r.repo = ? AND r.issue = ? AND r.status = 'pr' AND r.dry_run = 0 AND h.released = 0", (repo, issue))]


def environment_retry(run_id: str) -> bool:
    with reader() as c:
        return c.execute(f"SELECT 1 FROM runs WHERE environment_retry_of = ? AND {LIVE_RUN}", (run_id,)).fetchone() is not None  # noqa: S608


def claim_environment_gap(repo: str, signature: str, day: str) -> bool:
    with db() as c:
        return c.execute("INSERT OR IGNORE INTO environment_gap_creations VALUES (?, ?, ?)",
                         (repo, signature, day)).rowcount == 1


def unclaim_environment_gap(repo: str, signature: str, day: str) -> None:
    with db() as c:
        c.execute("DELETE FROM environment_gap_creations WHERE repo = ? AND signature = ? AND day = ?",
                  (repo, signature, day))


def claim_environment_budget_resets(repo: str, issue: str, run_id: str,
                                    gaps: tuple[tuple[str, int], ...]) -> list[int]:
    with tx() as c:
        return [number for project, number in gaps if c.execute(
            "INSERT INTO environment_budget_resets (repo, issue, gap_repo, gap_issue, run_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (repo, issue, gap_repo, gap_issue) DO UPDATE SET run_id = excluded.run_id "
            "WHERE environment_budget_resets.run_id = excluded.run_id",
            (repo, issue, project, number, run_id)).rowcount]


def run_lock(kind: str, repo: str | None = None, issue: int | str | None = None) -> str:
    return {"pipeline": f"run-{repo}-{issue}", "slot": f"slot-{repo}-{issue}", "slot-wait": f"slot-wait-{repo}-{issue}",
            "merge-wait": f"merge-wait-{repo}-{issue}",
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


def ticket_runs(repo: str, issue: int | str, limit: int = 10, connect=reader, whole: bool = False,
                live: bool = False) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "  # noqa: S608
                                           + (f"AND {WHOLE_RUN} " if whole else "")
                                           + (f"AND {LIVE_RUN} " if live else "")
                                           + "ORDER BY started DESC, rowid DESC LIMIT ?", (repo, issue, limit))]


def newest_whole_run(repo: str, issue: int | str) -> dict | None:
    return next(iter(ticket_runs(repo, issue, limit=1, whole=True)), None)


def newest_live_whole_run(repo: str, issue: int | str) -> dict | None:
    return next(iter(ticket_runs(repo, issue, limit=1, whole=True, live=True)), None)


def failed_ticket_runs_since(repo: str, issue: int | str, since: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT failure FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
                                           "AND status = 'failed' AND finished > ?", (repo, issue, since))]


def pr_gate_refreshes(repo: str, issue: str) -> list[dict]:
    with reader() as c:
        if not migration_applied(20, c):
            return []
        rows = c.execute("SELECT pr_url, run_id, head, evidence FROM pr_gate_refreshes WHERE repo = ? AND issue = ?",
                         (repo, issue))
        return [{**row, "evidence": json.loads(row["evidence"])} for row in rows]


def save_pr_gate_refresh(repo: str, issue: str, pr_url: str, run_id: str, head: str, evidence: dict) -> bool:
    with tx() as c:
        target = repo, issue, pr_url
        pending = c.execute("SELECT 1 FROM pr_gate_refreshes WHERE repo = ? AND issue = ? AND pr_url = ?", target)
        retry = pending.fetchone() is not None
        c.execute("INSERT INTO pr_gate_refreshes VALUES (?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT (repo, issue, pr_url) DO UPDATE SET "
                  "run_id = excluded.run_id, head = excluded.head, evidence = excluded.evidence",
                  (*target, run_id, head, json.dumps(evidence)))
        return retry


def clear_pr_gate_refresh(repo: str, issue: str, pr_url: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM pr_gate_refreshes WHERE repo = ? AND issue = ? AND pr_url = ?", (repo, issue, pr_url))


def environment_requeue(run: dict, since: str) -> tuple[dict, int]:
    with reader() as c:
        entry = c.execute("SELECT * FROM environment_requeues WHERE run_id = ?", (run["id"],)).fetchone()
        count = c.execute("SELECT COUNT(*) FROM environment_requeues WHERE repo = ? AND issue = ? "
                          "AND attempted > ? AND requeued = 1 AND run_id != ?",
                          (run["repo"], run["issue"], since, run["id"])).fetchone()
        return dict(entry) if entry else {}, count[0]


def unfinished_environment_requeues(repo: str) -> list[dict]:
    with reader() as c:
        return [dict(row) for row in c.execute(
            "SELECT * FROM environment_requeues WHERE repo = ? "
            "AND (edit_pending IS NOT NULL OR (requeued = 1 AND requeue_notified = 0)) ORDER BY issue, run_id", (repo,))]


def start_requeue_edit(run: dict, at: str) -> None:
    with db() as c:
        c.execute("INSERT INTO environment_requeues (run_id, repo, issue, edit_pending) VALUES (?, ?, ?, ?) "
                  "ON CONFLICT(run_id) DO UPDATE SET edit_pending = excluded.edit_pending WHERE requeued = 0",
                  (run["id"], run["repo"], run["issue"], at))


def settle_requeue_edit(run_id: str, confirmed_at: str | None = None) -> None:
    with db() as c:
        c.execute("UPDATE environment_requeues SET edit_pending = NULL, attempted = ?, requeued = ? "
                  "WHERE run_id = ? AND edit_pending IS NOT NULL", (confirmed_at, int(confirmed_at is not None), run_id))


def note_requeue_comment(run: dict, *, capped: bool = False) -> None:
    with db() as c:
        if capped:
            c.execute("INSERT INTO environment_requeues (run_id, repo, issue, cap_notified) VALUES (?, ?, ?, 1) "
                      "ON CONFLICT(run_id) DO UPDATE SET cap_notified = 1", (run["id"], run["repo"], run["issue"]))
        else:
            c.execute("UPDATE environment_requeues SET requeue_notified = 1 WHERE run_id = ? AND requeued = 1",
                      (run["id"],))


def ticket_runs_marked_running(repo: str, issue: int | str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
                                           "AND status = 'running' ORDER BY started ASC, rowid ASC", (repo, issue))]


def ab_placements(since: str, name: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        labelled = [dict(r) for r in c.execute(
            "SELECT DISTINCT steps.ab, steps.ab_side, steps.atom, r.repo, r.issue FROM steps "  # noqa: S608
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


def later_run(repo: str, issue: int | str, run_id: str, connect=reader, *, live: bool = False,
              candidate: str = "") -> dict | None:
    with connect() as c:
        r = c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? AND {WHOLE_RUN} "  # noqa: S608
                      + (f"AND {LIVE_RUN} " if live else "")
                      + ("AND id = ? " if candidate else "")
                      + "AND rowid > (SELECT rowid FROM runs WHERE id = ?) ORDER BY rowid LIMIT 1",
                      (repo, issue, *([candidate] if candidate else []), run_id)).fetchone()
        return dict(r) if r else None


def interrupted_tickets(repo: str) -> list[str]:
    with reader() as c:
        rows = c.execute(f"SELECT issue, status FROM runs WHERE kind = 'pipeline' AND repo = ? AND {WHOLE_RUN} "  # noqa: S608
                         "AND rowid = (SELECT r2.rowid FROM runs r2 WHERE r2.kind = 'pipeline' "
                         f"AND r2.repo = runs.repo AND r2.issue = runs.issue AND {WHOLE_RUN} AND {LIVE_RUN} "
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
    with writes.atomic():
        _prune(c, days, lines_days)


def _prune(c, days, lines_days):
    since = util.iso(util.now() - timedelta(days=days))
    lines_since, now = util.iso(util.now() - timedelta(days=lines_days)), util.iso()
    c.execute(f"INSERT OR IGNORE INTO run_purges SELECT id, 'firing', ? FROM runs "  # noqa: S608
              f"WHERE {FIRINGS} AND started < ?", (now, since))
    c.execute(f"DELETE FROM feed WHERE run_id IN (SELECT id FROM runs WHERE {FIRINGS} AND started < ?)", (since,))  # noqa: S608
    for row in c.execute(f"SELECT id FROM runs WHERE {FIRINGS} AND started < ?", (since,)).fetchall():  # noqa: S608
        writes.update("runs", row["id"], {}, delete=True, kind="run.deleted")
    keep_workers = (
        "AND NOT EXISTS (SELECT 1 FROM orchestration_workers w JOIN orchestrations o ON o.root=w.root "
        "WHERE w.phase NOT IN ('completed','integrated','cancelled') "
        "AND EXISTS (SELECT 1 FROM json_each(o.data,'$.runs') WHERE value=runs.id)) "
    ) if migration_applied(WORKER_EXCHANGE_SCHEMA, c) else ""
    keep_specs = (
        "AND NOT EXISTS (SELECT 1 FROM spec_deliveries s WHERE s.repo = runs.repo AND s.issue = runs.issue "
        "AND s.state != 'complete') "
    ) if migration_applied(SPEC_DELIVERY_SCHEMA, c) else ""
    eligible = c.execute(
        "SELECT id FROM runs WHERE files_purged IS NULL AND COALESCE(finished, started) < ? "  # noqa: S608
        "AND status NOT IN ('running', 'pr', 'needs-info') "
        "AND NOT EXISTS (SELECT 1 FROM record_pins WHERE run_id = runs.id) "
        + keep_workers + keep_specs
        + "AND NOT EXISTS (SELECT 1 FROM asks WHERE run = runs.id AND "
        "(status = 'open' OR (status = 'answered' AND COALESCE(taken_by, '') = ''))) "
        "AND NOT EXISTS (SELECT 1 FROM proposals WHERE "
        "(status IS NULL OR status NOT IN ('merged', 'rejected', 'skipped', 'filed')) "
        "AND (run_id = runs.id OR instr(COALESCE(evidence, ''), runs.id) > 0)) "
        "AND NOT EXISTS (SELECT 1 FROM runs recent WHERE recent.repo = runs.repo "
        "AND recent.issue = runs.issue AND recent.started >= ?)", (lines_since, lines_since)).fetchall()
    for row in eligible:
        c.execute("INSERT OR IGNORE INTO run_purges VALUES (?, 'lines', ?)", (row["id"], now))
        writes.update("runs", row["id"], {"files_purged": now})
        from . import orchestration

        orchestration.purge(c, row["id"])


def running_runs(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
        return []
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM runs WHERE status='running'")]


def _sync_inbox_runs(c) -> None:
    from .. import inbox

    inbox.sync(c, "runs")
    inbox.sync(c, "asks")


def insert_run(run_id: str, row: dict) -> None:
    from . import factory

    with writes.atomic() as c:
        writes.insert("runs", row, run_id, "run.started")
        factory.registered_run(c, run_id, row)
        if row.get("kind") == "pipeline":
            _sync_inbox_runs(c)


def insert_steps(run_id: str, name: str, calls, effort: str = "", tier: str = "", kind: str = "",
                 try_no: int = 0, auth_mode: str = "") -> None:
    with writes.atomic() as c:
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
            writes.update("runs", run_id, {}, increments={"cost_usd": res.cost_usd, "tokens_in": res.tokens_in,
                                                         "tokens_out": res.tokens_out})


STEP_FIELDS = frozenset({"runner", "model", "provider", "auth_mode", "ok", "cost_usd", "tokens_in", "tokens_out",
                         "turns", "seconds", "error", "ts", "first_context_tokens", "effort", "tier", "kind",
                         "cache_read", "cache_write", "api_seconds", "atom", "ab", "ab_side"})


def label_step(run_id: str, step: str, try_no: int, **labels) -> None:
    if unknown := set(labels) - STEP_FIELDS:
        raise ValueError(f"invalid step fields: {', '.join(sorted(unknown))}")
    if not labels:
        return
    with db() as c:
        c.execute(f"UPDATE steps SET {', '.join(f'{k} = ?' for k in labels)} WHERE run_id = ? AND step = ? AND try = ?",  # noqa: S608
                  (*labels.values(), run_id, step, try_no))


def hold_ab(run_id: str, role: str, name: str, side: str, arm: dict) -> None:
    with writes.atomic() as c:
        row = c.execute("SELECT ab_held FROM runs WHERE id = ?", (run_id,)).fetchone()
        held = {**json.loads((row["ab_held"] if row else "") or "{}"), role: [name, side, arm]}
        writes.update("runs", run_id, {"ab_held": json.dumps(held)})


def note(run_id: str, *, base_version=None, **fields) -> None:
    if "status" in fields:
        raise ValueError("use move to change a run's status")
    if fields.get("pid"):
        from . import factory

        with writes.atomic() as c:
            writes.update("runs", run_id, fields, base_version=base_version)
            factory.register(c, run_id, fields["pid"], "run")
    else:
        writes.update("runs", run_id, fields, base_version=base_version)


def checkpoint(run_id: str, active: float, verified: float, opened: float, repairs: int, at: str) -> None:
    writes.update("runs", run_id, {"active_seconds": active, "verify_seconds": verified, "verify_open_seconds": opened,
                                  "repair_calls": repairs, "measured_at": at})


def move(run_id: str, to: str, expect: tuple[str, ...], *, base_version=None, **fields) -> bool:
    if not expect:
        if base_version is not None:
            writes.check_run(run_id, base_version)
        return False
    fields = {**fields, "status": to}
    with writes.atomic() as c:
        updated = writes.update("runs", run_id, fields, expect=expect, base_version=base_version)
        if updated is not None and updated.get("kind") == "pipeline" and (to == "needs-info" or "needs-info" in expect):
            _sync_inbox_runs(c)
        return updated is not None


def ask_stop(run_id: str, base_version=None) -> dict | None:
    with writes.atomic():
        row = writes.check_run(run_id, base_version, for_stop=True)
        if not row or row["status"] != "running":
            return row
        if row["stop_asked"]:
            if not events.reuse_receipt(f"run:{run_id}", row["version"]):
                raise refusals.Conflict("This run already has a stop request, but no retained receipt for its "
                                        "current version (legacy or expired history); a repeated stop cannot "
                                        "be acknowledged with an audit receipt.")
            return row
        return writes.update("runs", run_id, {"stop_asked": util.iso()}, base_version=row["version"],
                             expect=("running",))


def settle(run_id: str, fields: dict, end_line: str = "", base_version=None) -> dict | None:
    from . import factory

    with writes.atomic() as c:
        settled = writes.update("runs", run_id, fields, expect=("running",), kind="run.ended", base_version=base_version)
        if not settled:
            return None
        factory.finish_run(c, run_id)
        if settled["kind"] == "pipeline" and settled["status"] in ("interrupted", "stopped"):
            from . import insights

            insights.release_interrupted_feedback(run_id)
        if end_line:
            insert_feed(run_id, "end", end_line)
        if settled["kind"] == "pipeline" and settled["status"] == "needs-info":
            _sync_inbox_runs(c)
        return settled


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


def orphan_rows(c, table, limit):
    if table not in ("steps", "feed"):
        raise ValueError(table)
    extra = " AND t.run_id <> 'chat'" if table == "steps" else ""
    query = (f"FROM {table} t LEFT JOIN runs ON runs.id = t.run_id "
             f"WHERE t.run_id IS NOT NULL AND t.run_id <> '' AND runs.id IS NULL{extra}")
    count = c.execute(f"SELECT COUNT(*) {query}").fetchone()[0]
    rows = c.execute(f"SELECT t.rowid, t.run_id {query} ORDER BY t.rowid LIMIT ?", (limit,)).fetchall() if limit else []
    return count, rows


def pseudo_chat_steps(c, run_id):
    return c.execute("SELECT id FROM steps WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()


def folder_rows(c):
    return c.execute("SELECT id, kind, started, log, files_purged FROM runs ORDER BY id").fetchall()
