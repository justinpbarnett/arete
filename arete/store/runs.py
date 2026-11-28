from __future__ import annotations

from collections.abc import Callable
import json
import math
import re
import sqlite3

from .. import refusals, util
from ..agent import billing_of
from . import events, writes
from ._rows import json_dict, validate_columns
from .db import (
    RECORD_TURNS_SCHEMA,
    RUN_RETENTION_SCHEMA,
    RUNS_ADW_SCHEMA,
    RUNS_LINEAGE_SCHEMA,
    RUNS_PINS_SCHEMA,
    RUNS_PROMPT_BINDING_SCHEMA,
    STEPS_LIFECYCLE_SCHEMA,
    TASK_IDENTITY_EXPAND_SCHEMA,
    db,
    migration_applied,
    reader,
    tx,
)

STEP_LABEL_FIELDS = frozenset(("atom", "ab", "ab_side", "effort", "tier", "kind", "auth_mode"))
PINS_RUN_FIELDS = frozenset(("config_commit", "resolved_sha"))
LINEAGE_RUN_FIELDS = frozenset(("continues", "chain", "restart", "started_by", "payer", "seq"))
ADW_RUN_FIELDS = frozenset(("adw", "adw_version", "adw_rule"))
TASK_IDENTITY_RUN_FIELDS = frozenset(("task_id", "issue_key", "capabilities"))
_CHAT_STEP_RE = re.compile(r"^chat (\d+)(?: (.+))?$")


def has_pins(c) -> bool:
    return migration_applied(RUNS_PINS_SCHEMA, c)


def has_prompt_binding(c) -> bool:
    return migration_applied(RUNS_PROMPT_BINDING_SCHEMA, c)


def has_lineage(c) -> bool:
    return migration_applied(RUNS_LINEAGE_SCHEMA, c)


def has_adw(c) -> bool:
    return migration_applied(RUNS_ADW_SCHEMA, c)


def has_steps_lifecycle(c) -> bool:
    return migration_applied(STEPS_LIFECYCLE_SCHEMA, c)


def has_record_turns(c) -> bool:
    return migration_applied(RECORD_TURNS_SCHEMA, c)


def has_task_identity(c) -> bool:
    return migration_applied(TASK_IDENTITY_EXPAND_SCHEMA, c) or (
        "task_id" in {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    )


def _resolve_task_target(c, repo_or_task: str | int, issue: int | str | None = None) -> tuple[int | None, str | None, int | str | None]:
    if isinstance(repo_or_task, int) and not isinstance(repo_or_task, bool) and issue is None:
        return repo_or_task, None, None
    repo = str(repo_or_task)
    if has_task_identity(c):
        from . import tasks as task_store

        tnum = task_store.TaskStore.find_task_num(c, repo, issue)
        if tnum is not None:
            return tnum, repo, issue
    return None, repo, issue


def step_node_and_chat(step_name: str, chat_id: int | None = None) -> tuple[str, int | None]:
    name = step_name or ""
    if chat_id is not None:
        prefix = f"chat {chat_id}"
        if name == prefix:
            return "chat", chat_id
        if name.startswith(prefix + " "):
            return "chat " + name[len(prefix) + 1:], chat_id
        return name, chat_id
    if m := _CHAT_STEP_RE.fullmatch(name):
        return (f"chat {m.group(2)}" if m.group(2) else "chat"), int(m.group(1))
    return name, None


def seq_col(c, alias: str = "") -> str:
    col = "seq" if has_lineage(c) else "oid"
    return f"{alias}.{col}" if alias else col


def later_than(c, later: str, earlier: str) -> str:
    col = "seq" if has_lineage(c) else "oid"
    return f"{later}.{col} > {earlier}.{col}"


def filter_run_fields(c, fields: dict) -> dict:
    drop: set[str] = set()
    if not has_pins(c):
        drop |= PINS_RUN_FIELDS
    if not has_lineage(c):
        drop |= LINEAGE_RUN_FIELDS
    if not has_adw(c):
        drop |= ADW_RUN_FIELDS
    if not has_task_identity(c):
        drop |= TASK_IDENTITY_RUN_FIELDS
    if not drop:
        return fields
    return {k: v for k, v in fields.items() if k not in drop}

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
    "step_inputs": "kept",
    "settings_snapshots": "kept",
    "turn_index": "derived from record files",
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
         firings: bool = False, statuses: tuple[str, ...] = (), running_first: bool = False,
         include_chat: bool = False) -> list[dict]:
    q, args = "SELECT * FROM runs WHERE 1=1", []
    if not firings:
        q += f" AND NOT {FIRINGS}"
    if not kind and not include_chat:
        q += " AND kind != 'chat'"
    for column, value in (("kind", kind), ("loop", loop), ("repo", repo)):
        if value:
            q += f" AND {column} = ?"
            args.append(value)
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    with connect() as c:
        q += (" ORDER BY " + ("status = 'running' DESC, " if running_first else "")
              + f"started DESC, {seq_col(c)} DESC LIMIT ?")
        args.append(limit)
        return [dict(r) for r in c.execute(q, args)]


def run_facets(connect=reader) -> dict[str, list[str]]:
    with connect() as c:
        return {column: [r[0] for r in c.execute(f"SELECT DISTINCT {column} FROM runs WHERE NOT {FIRINGS} "  # noqa: S608
                                                 f"AND kind != 'chat' AND COALESCE({column}, '') != '' ORDER BY 1")]
                for column in ("kind", "repo", "loop")}


def paid_by_run(run_ids: list[str], connect=reader, local_providers=None) -> dict[str, float]:
    if not run_ids:
        return {}
    if local_providers is None:
        from .. import pins

        local_providers = pins.active_config().local_providers
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


def open_runs(repo: str | int, issue: int | str | None = None) -> list[dict]:
    with reader() as c:
        tnum, repo_name, raw_issue = _resolve_task_target(c, repo, issue)
        if tnum is not None:
            return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND task_id = ? AND {OPEN_PR_RUN} "  # noqa: S608
                                               "ORDER BY started DESC", (tnum,))]
        if repo_name is None:
            return []
        return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE repo = ? AND issue = ? AND {OPEN_PR_RUN} "  # noqa: S608
                                           "ORDER BY started DESC", (repo_name, raw_issue))]


DRAFT_READY = "run.draft-review.ready"
DRAFT_OBSERVED = "run.draft-review.observed"
DRAFT_RECEIPT = ("SELECT e.at FROM events_log e WHERE e.subject = 'run:' || r.id AND e.kind = ? "
                 "AND json_extract(e.data, '$.pr_url') = r.pr_url")


def ticket_attention_runs(*, questions: bool = False) -> list[dict]:
    with reader() as c:
        k = seq_col(c)
        return [dict(row) for row in c.execute(f"""
            SELECT r.* FROM runs r WHERE r.kind = 'pipeline' AND r.issue != '' AND r.repo != ''
                AND COALESCE(r.stop_asked, '') = '' AND {WHOLE_RUN} AND {LIVE_RUN}
                AND ((r.status = 'pr' AND r.pr_url != '' AND COALESCE(r.environment_retry_of, '') = ''
                      AND NOT EXISTS ({DRAFT_RECEIPT})) OR (? AND r.status = 'needs-info' AND NOT EXISTS (
                    SELECT 1 FROM runs later WHERE later.repo = r.repo AND later.issue = r.issue
                      AND later.kind = 'pipeline' AND {WHOLE_RUN} AND {LIVE_RUN}
                      AND (later.started, later.{k}) > (r.started, r.{k})))) ORDER BY r.started DESC, r.{k} DESC
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
        row = c.execute(f"SELECT r.branch FROM environment_holds h JOIN runs r ON h.run_id = r.id "  # noqa: S608
                        "WHERE r.repo = ? AND r.issue = ? AND r.status = 'pr' AND r.dry_run = 0 AND h.released = 0 "
                        f"ORDER BY {seq_col(c, 'r')} DESC LIMIT 1", (repo, issue)).fetchone()
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
        return {**hold, "heads": heads, "waits": json_dict(hold["waits"])}


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


def run_lock(kind: str, repo: str | None = None, issue: int | str | None = None, *, task: int | None = None) -> str:
    if task is not None:
        return {
            "pipeline": f"run-T{int(task)}",
            "slot": f"slot-T{int(task)}",
            "slot-wait": f"slot-wait-T{int(task)}",
            "merge-wait": f"merge-wait-T{int(task)}",
        }[kind]
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


def step_get(step_id: int, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return dict(r) if r else None


def open_steps(run_id: str, connect=reader) -> list[dict]:
    with connect() as c:
        if not has_steps_lifecycle(c):
            return []
        return [dict(r) for r in c.execute(
            "SELECT * FROM steps WHERE run_id = ? AND status IN ('queued', 'waiting', 'running') ORDER BY id",
            (run_id,),
        )]


def step_tries(run_id: str, step: str) -> int:
    node, _ = step_node_and_chat(step)
    with reader() as c:
        lifecycle = has_steps_lifecycle(c)
        if has_lineage(c):
            chain_row = c.execute("SELECT chain FROM runs WHERE id = ?", (run_id,)).fetchone()
            chain = chain_row["chain"] if chain_row and chain_row["chain"] else None
            if chain:
                if lifecycle:
                    return c.execute(
                        "SELECT COALESCE(MAX(s.try), 0) FROM steps s JOIN runs r ON r.id = s.run_id "
                        "WHERE r.chain = ? AND (s.node = ? OR s.step = ?)",
                        (chain, node, step),
                    ).fetchone()[0]
                return c.execute(
                    "SELECT COALESCE(MAX(s.try), 0) FROM steps s JOIN runs r ON r.id = s.run_id "
                    "WHERE r.chain = ? AND s.step = ?",
                    (chain, step),
                ).fetchone()[0]
        if lifecycle:
            return c.execute(
                "SELECT COALESCE(MAX(try), 0) FROM steps WHERE run_id = ? AND (node = ? OR step = ?)",
                (run_id, node, step),
            ).fetchone()[0]
        return c.execute(
            "SELECT COALESCE(MAX(try), 0) FROM steps WHERE run_id = ? AND step = ?",
            (run_id, step),
        ).fetchone()[0]


def step_version(run_id: str, node: str, try_no: int, connect=reader) -> int | None:
    norm_node, _ = step_node_and_chat(node)
    with connect() as c:
        if not has_steps_lifecycle(c):
            return None
        row = c.execute(
            "SELECT version FROM steps WHERE run_id = ? AND (node = ? OR step = ?) AND try = ? "
            "ORDER BY id DESC LIMIT 1",
            (run_id, norm_node, node, try_no),
        ).fetchone()
        return int(row["version"]) if row and row["version"] is not None else None


def has_accounts(connect=reader) -> bool:
    with connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
            return False
        return c.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None


def account_id_for_fingerprint(fingerprint: str, connect=reader) -> str | None:
    if not fingerprint:
        return None
    with connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
            return None
        row = c.execute("SELECT id FROM accounts WHERE fingerprint = ? LIMIT 1", (fingerprint,)).fetchone()
        return str(row["id"]) if row else None


def queue_step(run_id: str, step_name: str, try_no: int, *, chat_id: int | None = None,
               effort: str = "", tier: str = "", kind: str = "") -> int | None:
    if not run_id:
        return None
    node, resolved_chat_id = step_node_and_chat(step_name, chat_id)
    now = util.iso()
    with writes.atomic() as c:
        if not has_steps_lifecycle(c):
            return None
        cur = c.execute(
            "INSERT INTO steps (run_id, step, node, status, queued_at, ts, try, chat_id, effort, tier, kind, ok, version) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, 0, 1)",
            (run_id, step_name, node, now, now, try_no, resolved_chat_id, effort or "", tier or "", kind or ""),
        )
        return cur.lastrowid


def wait_step(step_id: int | None) -> None:
    if step_id is None:
        return
    with writes.atomic() as c:
        if not has_steps_lifecycle(c):
            return
        c.execute(
            "UPDATE steps SET status = 'waiting', waiting_since = ?, version = version + 1 WHERE id = ?",
            (util.iso(), step_id),
        )


def start_step(step_id: int | None, *, account_id: str | None, device: str, runner: str, model: str,
               provider: str, auth_mode: str, runner_version: str = "") -> None:
    if step_id is None:
        return
    with writes.atomic() as c:
        if not has_steps_lifecycle(c):
            return
        if has_record_turns(c):
            c.execute(
                "UPDATE steps SET status = 'running', waiting_since = NULL, account_id = ?, device = ?, "
                "runner = ?, runner_version = ?, model = ?, provider = ?, auth_mode = ?, version = version + 1 "
                "WHERE id = ?",
                (account_id, device or "", runner or "", runner_version or "", model or "", provider or "",
                 auth_mode or "", step_id),
            )
        else:
            c.execute(
                "UPDATE steps SET status = 'running', waiting_since = NULL, account_id = ?, device = ?, "
                "runner = ?, model = ?, provider = ?, auth_mode = ?, version = version + 1 WHERE id = ?",
                (account_id, device or "", runner or "", model or "", provider or "", auth_mode or "", step_id),
            )


def fail_step(step_id: int | None, error: str, *, runner: str = "", model: str = "", provider: str = "") -> None:
    if step_id is None:
        return
    from .. import inbox

    with writes.atomic() as c:
        if not has_steps_lifecycle(c):
            return
        c.execute(
            "UPDATE steps SET status = 'fault', ok = 0, error = ?, ts = ?, "
            "runner = COALESCE(NULLIF(runner, ''), ?), model = COALESCE(NULLIF(model, ''), ?), "
            "provider = COALESCE(NULLIF(provider, ''), ?), version = version + 1 WHERE id = ?",
            ((error or "")[:500], util.iso(), runner or "", model or "", provider or "", step_id),
        )
        inbox.on_step_settled(c, step_id)


def insert_turn(step_id: int, n: int, run_id: str, *, at: str, status: str, tool: str, excerpt: str,
                seconds: float, tokens_in: int, cache_read: int, tokens_out: int, cost_nusd: int | None,
                file: str, member_offset: int, member_len: int) -> None:
    with db() as c:
        if not has_record_turns(c):
            return
        c.execute(
            "INSERT OR REPLACE INTO turn_index "
            "(step_id, n, run_id, at, status, tool, excerpt, seconds, tokens_in, cache_read, tokens_out, "
            "cost_nusd, file, member_offset, member_len) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (step_id, n, run_id, at, status, tool, excerpt, seconds, tokens_in, cache_read, tokens_out,
             cost_nusd, file, member_offset, member_len),
        )


def step_turns(step_id: int, connect=reader) -> list[dict]:
    with connect() as c:
        if not has_record_turns(c):
            return []
        return [dict(r) for r in c.execute(
            "SELECT * FROM turn_index WHERE step_id = ? ORDER BY n", (step_id,)
        )]


def run_step_turns(run_id: str, node: str, try_no: int, connect=reader) -> tuple[list[dict], list[dict]]:
    norm_node, _ = step_node_and_chat(node)
    with connect() as c:
        if has_steps_lifecycle(c):
            matched_steps = [dict(r) for r in c.execute(
                "SELECT * FROM steps WHERE run_id = ? AND (node = ? OR step = ?) AND try = ? ORDER BY id",
                (run_id, norm_node, node, try_no),
            )]
        else:
            matched_steps = [dict(r) for r in c.execute(
                "SELECT * FROM steps WHERE run_id = ? AND step = ? AND try = ? ORDER BY id",
                (run_id, node, try_no),
            )]
        if not matched_steps or not has_record_turns(c):
            return matched_steps, []
        step_ids = [s["id"] for s in matched_steps]
        placeholders = ",".join("?" * len(step_ids))
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM turn_index WHERE step_id IN ({placeholders}) ORDER BY step_id, n",  # noqa: S608
            step_ids,
        )]
        return matched_steps, rows


def clear_turn_index(run_id: str | None = None, *, step_id: int | None = None) -> None:
    with db() as c:
        if not has_record_turns(c):
            return
        if step_id is not None:
            c.execute("DELETE FROM turn_index WHERE step_id = ?", (step_id,))
        elif run_id is not None:
            c.execute("DELETE FROM turn_index WHERE run_id = ?", (run_id,))
        else:
            c.execute("DELETE FROM turn_index")


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


def runs_queued(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY started")]


def live_chat_run(chat_id: int, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute(
            f"SELECT * FROM runs WHERE kind = 'chat' AND id LIKE ? AND status IN ('running', 'queued') "  # noqa: S608
            f"ORDER BY started DESC, {seq_col(c)} DESC LIMIT 1",
            (f"%-chat-{chat_id}",),
        ).fetchone()
        return dict(r) if r else None


def ticket_runs(repo: str | int, issue: int | str | None = None, limit: int = 10, connect=reader, whole: bool = False,
                live: bool = False, *, include_queued: bool | None = None) -> list[dict]:
    show_queued = (not whole and not live) if include_queued is None else include_queued
    extra = (
        (f"AND {WHOLE_RUN} " if whole else "")
        + (f"AND {LIVE_RUN} " if live else "")
        + ("" if show_queued else "AND status != 'queued' AND NOT (pid IS NULL AND status = 'stopped' AND stop_asked IS NULL) ")
    )
    with connect() as c:
        tnum, repo_name, raw_issue = _resolve_task_target(c, repo, issue)
        if tnum is not None:
            return [dict(r) for r in c.execute(
                f"SELECT * FROM runs WHERE kind = 'pipeline' AND task_id = ? {extra}"  # noqa: S608
                f"ORDER BY started DESC, {seq_col(c)} DESC LIMIT ?",
                (tnum, limit),
            )]
        if repo_name is None:
            return []
        return [dict(r) for r in c.execute(
            f"SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? {extra}"  # noqa: S608
            f"ORDER BY started DESC, {seq_col(c)} DESC LIMIT ?",
            (repo_name, raw_issue, limit),
        )]


def newest_whole_run(repo: str | int, issue: int | str | None = None) -> dict | None:
    return next(iter(ticket_runs(repo, issue, limit=1, whole=True)), None)


def newest_live_whole_run(repo: str | int, issue: int | str | None = None) -> dict | None:
    return next(iter(ticket_runs(repo, issue, limit=1, whole=True, live=True)), None)


def failed_ticket_runs_since(repo: str | int, issue: int | str | None, since: str | None = None) -> list[dict]:
    if isinstance(repo, int) and not isinstance(repo, bool) and since is None:
        since_val = str(issue)
        target_repo: str | int = repo
        target_issue: int | str | None = None
    else:
        since_val = str(since or "")
        target_repo, target_issue = repo, issue
    with reader() as c:
        tnum, repo_name, raw_issue = _resolve_task_target(c, target_repo, target_issue)
        if tnum is not None:
            return [dict(r) for r in c.execute(
                "SELECT failure FROM runs WHERE kind = 'pipeline' AND task_id = ? "
                "AND status = 'failed' AND finished > ?",
                (tnum, since_val),
            )]
        if repo_name is None:
            return []
        return [dict(r) for r in c.execute(
            "SELECT failure FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
            "AND status = 'failed' AND finished > ?",
            (repo_name, raw_issue, since_val),
        )]


def pr_gate_refreshes(repo: str, issue: str) -> list[dict]:
    with reader() as c:
        if not migration_applied(20, c):
            return []
        rows = c.execute("SELECT pr_url, run_id, head, evidence FROM pr_gate_refreshes WHERE repo = ? AND issue = ?",
                         (repo, issue))
        return [{**row, "evidence": json_dict(row["evidence"])} for row in rows]


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


def ticket_runs_marked_running(repo: str | int, issue: int | str | None = None, connect=reader) -> list[dict]:
    with connect() as c:
        tnum, repo_name, raw_issue = _resolve_task_target(c, repo, issue)
        if tnum is not None:
            return [dict(r) for r in c.execute(
                f"SELECT * FROM runs WHERE kind = 'pipeline' AND task_id = ? "  # noqa: S608
                f"AND status = 'running' ORDER BY started ASC, {seq_col(c)} ASC",
                (tnum,),
            )]
        if repo_name is None:
            return []
        return [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "  # noqa: S608
                                           f"AND status = 'running' ORDER BY started ASC, {seq_col(c)} ASC",
                                           (repo_name, raw_issue))]


def ab_placements(since: str, name: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        labelled = [dict(r) for r in c.execute(
            "SELECT DISTINCT steps.ab, steps.ab_side, steps.atom, r.repo, r.issue FROM steps "  # noqa: S608
            "JOIN runs r ON r.id = steps.run_id WHERE COALESCE(steps.ab, '') != '' AND r.kind = 'pipeline' "
            "AND r.started >= ?" + (" AND steps.ab = ?" if name else ""), (since, *([name] if name else [])))]
        held = c.execute("SELECT repo, issue, ab_held FROM runs WHERE kind = 'pipeline' AND started >= ? "
                         "AND COALESCE(ab_held, '') != ''", (since,)).fetchall()
    assigned = [{"ab": ab, "ab_side": side, "atom": role, "repo": r["repo"], "issue": r["issue"]}
                for r in held for role, item in json_dict(r["ab_held"]).items()
                if isinstance(item, (list, tuple)) and len(item) == 3
                for ab, side, _ in (item,) if not name or ab == name]
    return list({tuple(p.values()): p for p in labelled + assigned}.values())


def ab_held(run_id: str, connect=reader) -> dict[str, tuple[str, str, dict]]:
    with connect() as c:
        row = c.execute("SELECT ab_held FROM runs WHERE id = ?", (run_id,)).fetchone()
    return {role: tuple(held) for role, held in json_dict(row["ab_held"]).items()
            if isinstance(held, (list, tuple))} if row else {}


def ticket_runs_since(repo: str | int, issue: int | str | None, since: str | None = None, connect=reader) -> list[dict]:
    if isinstance(repo, int) and not isinstance(repo, bool) and since is None:
        since_val = str(issue)
        target_repo: str | int = repo
        target_issue: int | str | None = None
    else:
        since_val = str(since or "")
        target_repo, target_issue = repo, issue
    with connect() as c:
        tnum, repo_name, raw_issue = _resolve_task_target(c, target_repo, target_issue)
        if tnum is not None:
            return [dict(r) for r in c.execute(
                "SELECT * FROM runs WHERE kind = 'pipeline' AND task_id = ? AND started >= ? ORDER BY started",
                (tnum, since_val),
            )]
        if repo_name is None:
            return []
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "
                                           "AND started >= ? ORDER BY started", (repo_name, raw_issue, since_val))]


def later_run(repo: str | int, issue: int | str | None, run_id: str | None = None, connect=reader, *, live: bool = False,
              candidate: str = "") -> dict | None:
    if isinstance(repo, int) and not isinstance(repo, bool) and run_id is None:
        target_run_id = str(issue)
        target_repo: str | int = repo
        target_issue: int | str | None = None
    else:
        target_run_id = str(run_id or "")
        target_repo, target_issue = repo, issue
    with connect() as c:
        k = seq_col(c)
        tnum, repo_name, raw_issue = _resolve_task_target(c, target_repo, target_issue)
        if tnum is not None:
            r = c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND task_id = ? AND {WHOLE_RUN} "  # noqa: S608
                          + (f"AND {LIVE_RUN} " if live else "")
                          + ("AND id = ? " if candidate else "")
                          + f"AND {k} > (SELECT {k} FROM runs WHERE id = ?) ORDER BY {k} LIMIT 1",  # noqa: S608
                          (tnum, *([candidate] if candidate else []), target_run_id)).fetchone()
            return dict(r) if r else None
        if repo_name is None:
            return None
        r = c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? AND {WHOLE_RUN} "  # noqa: S608
                      + (f"AND {LIVE_RUN} " if live else "")
                      + ("AND id = ? " if candidate else "")
                      + f"AND {k} > (SELECT {k} FROM runs WHERE id = ?) ORDER BY {k} LIMIT 1",  # noqa: S608
                      (repo_name, raw_issue, *([candidate] if candidate else []), target_run_id)).fetchone()
        return dict(r) if r else None


def interrupted_tickets(repo: str) -> list[str]:
    with reader() as c:
        k = seq_col(c)
        if has_task_identity(c):
            rows = c.execute(
                f"SELECT COALESCE(issue_key, CAST(issue AS TEXT)) AS ticket, status FROM runs "  # noqa: S608
                f"WHERE kind = 'pipeline' AND repo = ? AND task_id IS NOT NULL AND {WHOLE_RUN} "
                f"AND {k} = (SELECT r2.{k} FROM runs r2 WHERE r2.kind = 'pipeline' "
                f"AND r2.task_id = runs.task_id AND {WHOLE_RUN} AND {LIVE_RUN} "
                f"AND r2.status != 'queued' "
                f"ORDER BY r2.started DESC, r2.{k} DESC LIMIT 1) "
                f"ORDER BY started, {k}",
                (repo,),
            ).fetchall()
            return [str(r["ticket"]) for r in rows if r["status"] == "interrupted"]
        rows = c.execute(f"SELECT issue, status FROM runs WHERE kind = 'pipeline' AND repo = ? AND {WHOLE_RUN} "  # noqa: S608
                         f"AND {k} = (SELECT r2.{k} FROM runs r2 WHERE r2.kind = 'pipeline' "
                         f"AND r2.repo = runs.repo AND r2.issue = runs.issue AND {WHOLE_RUN} AND {LIVE_RUN} "
                         f"AND r2.status != 'queued' "
                         f"ORDER BY r2.started DESC, r2.{k} DESC LIMIT 1) "
                         f"ORDER BY started, {k}", (repo,))
        return [str(r["issue"]) for r in rows if r["status"] == "interrupted"]


def last_pipeline_starts() -> dict[str, str]:
    with reader() as c:
        return {r[0]: r[1] for r in c.execute("SELECT repo, MAX(started) FROM runs WHERE kind = 'pipeline' "
                                              "GROUP BY repo")}


def running_run(repo: str | int, issue: int | str | None = None, connect=reader) -> dict | None:
    with connect() as c:
        tnum, repo_name, raw_issue = _resolve_task_target(c, repo, issue)
        if tnum is not None:
            r = c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND task_id = ? "  # noqa: S608
                          f"AND status IN ('running', 'queued') "
                          f"ORDER BY status = 'running' DESC, started DESC, {seq_col(c)} DESC LIMIT 1",
                          (tnum,)).fetchone()
            return dict(r) if r else None
        if repo_name is None:
            return None
        r = c.execute(f"SELECT * FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "  # noqa: S608
                      f"AND status IN ('running', 'queued') "
                      f"ORDER BY status = 'running' DESC, started DESC, {seq_col(c)} DESC LIMIT 1",
                      (repo_name, raw_issue)).fetchone()
        return dict(r) if r else None


def pending_label(repo: str, issue: int | str) -> str:
    raw = str(issue or "").strip()
    if raw.isascii() and raw.isdigit():
        return ""
    with reader() as c:
        r = c.execute(f"SELECT label_pending FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue = ? "  # noqa: S608
                      f"AND label_pending IS NOT NULL ORDER BY started DESC, {seq_col(c)} DESC LIMIT 1",
                      (repo, raw)).fetchone()
        return r["label_pending"] if r else ""


def issue_running(repo: str | int, issue: int | str | None = None) -> bool:
    with reader() as c:
        tnum, repo_name, raw_issue = _resolve_task_target(c, repo, issue)
        if tnum is not None:
            return c.execute(
                "SELECT 1 FROM runs WHERE kind = 'pipeline' AND task_id = ? AND status IN ('running', 'queued') LIMIT 1",
                (tnum,),
            ).fetchone() is not None
        if repo_name is None:
            return False
        return c.execute("SELECT 1 FROM runs WHERE repo = ? AND issue = ? AND status IN ('running', 'queued') LIMIT 1",
                         (repo_name, raw_issue)).fetchone() is not None


def pin_record(run_id: str, *, require_record_dir: Callable[[], bool] | None = None) -> bool:
    with db() as c:
        if migration_applied(RUN_RETENTION_SCHEMA, c):
            row = c.execute("SELECT files_purged, records_purged FROM runs WHERE id = ?", (run_id,)).fetchone()
        else:
            row = c.execute("SELECT files_purged, NULL AS records_purged FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row and row["files_purged"]:
            raise refusals.Gone(
                f"run {run_id} files were removed on {row['files_purged']}",
                purged_at=row["files_purged"],
            )
        if row and row["records_purged"]:
            raise refusals.Gone(
                f"run {run_id} records were removed on {row['records_purged']}",
                purged_at=row["records_purged"],
            )
        if c.execute("SELECT 1 FROM run_purges WHERE run_id = ?", (run_id,)).fetchone():
            raise refusals.Conflict(f"run {run_id} is already queued for removal")
        if require_record_dir is not None and not require_record_dir():
            raise LookupError(f"no record for run {run_id}")
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


def running_runs(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
        return []
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM runs WHERE status='running'")]


def _sync_inbox_runs(c) -> None:
    from .. import inbox

    inbox.sync(c, "runs")
    inbox.sync(c, "stops")
    inbox.sync(c, "asks")


def insert_run(run_id: str, row: dict) -> None:
    from . import factory

    with writes.atomic() as c:
        writes.insert("runs", filter_run_fields(c, row), run_id, "run.started")
        factory.registered_run(c, run_id, row)
        if row.get("kind") == "pipeline":
            _sync_inbox_runs(c)


def save_settings_snapshots(c, snapshots: dict[str, str]) -> None:
    if not snapshots or not has_pins(c):
        return
    now = util.iso()
    c.executemany(
        "INSERT OR IGNORE INTO settings_snapshots (sha256, body, created) VALUES (?, ?, ?)",
        [(sha, body, now) for sha, body in snapshots.items()],
    )


def settings_snapshot(sha256: str, connect=reader) -> dict | None:
    if not sha256:
        return None
    with connect() as c:
        if not has_pins(c):
            return None
        row = c.execute("SELECT body FROM settings_snapshots WHERE sha256 = ?", (sha256,)).fetchone()
    if not row:
        return None
    try:
        parsed = json.loads(row["body"])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def step_inputs(step_id: int, connect=reader) -> list[dict]:
    with connect() as c:
        if not has_pins(c):
            return []
        return [dict(r) for r in c.execute(
            "SELECT step_id, role, name, version, level, sha256, call FROM step_inputs "
            "WHERE step_id = ? ORDER BY rowid", (step_id,))]


def step_inputs_by_run(run_id: str, connect=reader) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    with connect() as c:
        if not has_pins(c):
            return {}
        rows = c.execute(
            "SELECT si.step_id, si.role, si.name, si.version, si.level, si.sha256, si.call "
            "FROM step_inputs si JOIN steps s ON s.id = si.step_id "
            "WHERE s.run_id = ? ORDER BY si.step_id, si.rowid",
            (run_id,),
        )
        for row in rows:
            item = dict(row)
            grouped.setdefault(item["step_id"], []).append(item)
    return grouped


def running_config_commits(connect=reader) -> set[str]:
    with connect() as c:
        if not has_pins(c):
            return set()
        return {
            r[0] for r in c.execute(
                "SELECT DISTINCT config_commit FROM runs WHERE status = 'running' AND COALESCE(config_commit, '') != ''"
            )
        }


def highest_contract_migration(connect=reader) -> int:
    with connect() as c:
        row = c.execute("SELECT COALESCE(MAX(n), 0) FROM schema_migrations WHERE phase = 'contract'").fetchone()
    return int(row[0]) if row else 0


def insert_steps(run_id: str, name: str, calls, effort: str = "", tier: str = "", kind: str = "",
                 try_no: int = 0, auth_mode: str = "", *, inputs: tuple[dict, ...] | list[dict] = (),
                 settings_sha: str = "", settings_snapshots: dict[str, str] | None = None,
                 blob_rows: tuple[tuple[str, str, int], ...] = (),
                 prompt_version: str = "", prompt_level: str = "", schema_version: str = "",
                 step_id: int | None = None, chat_id: int | None = None, device: str = "",
                 account_id: str | None = None, runner_version: str = "", unread: int = 0) -> None:
    from . import blobs as blob_store

    node, resolved_chat_id = step_node_and_chat(name, chat_id)
    with writes.atomic() as c:
        pinned = has_pins(c)
        bound = has_prompt_binding(c)
        lifecycle = has_steps_lifecycle(c)
        turns_schema = has_record_turns(c)
        if pinned:
            for sha, blob_kind, size in blob_rows:
                blob_store.record_row(c, sha, blob_kind, size)
            if settings_snapshots:
                save_settings_snapshots(c, settings_snapshots)
        for index, (res, seconds) in enumerate(calls):
            now = util.iso()
            status = "done" if res.ok else "fault"
            if index == 0 and step_id is not None and lifecycle:
                existing = c.execute(
                    "SELECT cost_usd, tokens_in, tokens_out, device, account_id, chat_id FROM steps WHERE id = ?",
                    (step_id,),
                ).fetchone()
                if existing is not None:
                    prev_cost = float(existing["cost_usd"] or 0.0)
                    prev_tin = int(existing["tokens_in"] or 0)
                    prev_tout = int(existing["tokens_out"] or 0)
                    if not device:
                        device = existing["device"] or ""
                    if account_id is None:
                        account_id = existing["account_id"]
                    if resolved_chat_id is None:
                        resolved_chat_id = existing["chat_id"]
                    if turns_schema:
                        c.execute(
                            "UPDATE steps SET runner=?, model=?, provider=?, ok=?, status=?, cost_usd=?, tokens_in=?, "
                            "tokens_out=?, turns=?, seconds=?, error=?, ts=?, first_context_tokens=?, effort=?, "
                            "tier=?, kind=?, cache_read=?, cache_write=?, api_seconds=?, auth_mode=?, settings_sha=?, "
                            "prompt_version=?, prompt_level=?, schema_version=?, "
                            "unread=?, runner_version=COALESCE(NULLIF(?, ''), runner_version), "
                            "waiting_since=NULL, version=version+1 WHERE id=?",
                            (res.runner, res.model, res.provider, int(res.ok), status, res.cost_usd, res.tokens_in,
                             res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], now,
                             res.first_context_tokens, effort or "", tier or "", kind or "", res.cache_read,
                             res.cache_write, round(res.api_seconds, 2), auth_mode or res.auth_mode,
                             settings_sha or None, prompt_version or None, prompt_level or None,
                             schema_version or None, int(unread or 0), runner_version or "", step_id),
                        )
                    else:
                        c.execute(
                            "UPDATE steps SET runner=?, model=?, provider=?, ok=?, status=?, cost_usd=?, tokens_in=?, "
                            "tokens_out=?, turns=?, seconds=?, error=?, ts=?, first_context_tokens=?, effort=?, "
                            "tier=?, kind=?, cache_read=?, cache_write=?, api_seconds=?, auth_mode=?, settings_sha=?, "
                            "prompt_version=?, prompt_level=?, schema_version=?, "
                            "waiting_since=NULL, version=version+1 WHERE id=?",
                            (res.runner, res.model, res.provider, int(res.ok), status, res.cost_usd, res.tokens_in,
                             res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], now,
                             res.first_context_tokens, effort or "", tier or "", kind or "", res.cache_read,
                             res.cache_write, round(res.api_seconds, 2), auth_mode or res.auth_mode,
                             settings_sha or None, prompt_version or None, prompt_level or None,
                             schema_version or None, step_id),
                        )
                    written_step_id = step_id
                    delta_cost = res.cost_usd - prev_cost
                    delta_tin = res.tokens_in - prev_tin
                    delta_tout = res.tokens_out - prev_tout
                else:
                    written_step_id = None
            else:
                written_step_id = None
            if written_step_id is None:
                call_unread = int(unread or 0) if index == 0 else 0
                if lifecycle and turns_schema:
                    cur = c.execute(
                        "INSERT INTO steps (run_id, step, node, status, queued_at, device, account_id, chat_id, "
                        "runner, runner_version, unread, model, provider, ok, cost_usd, tokens_in, tokens_out, "
                        "turns, seconds, error, ts, first_context_tokens, effort, tier, try, kind, cache_read, "
                        "cache_write, api_seconds, auth_mode, settings_sha, prompt_version, prompt_level, "
                        "schema_version, version) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "COALESCE(NULLIF(?, 0), (SELECT COALESCE(MAX(try), 0) + 1 FROM steps WHERE run_id = ? AND step = ?)),"
                        "?,?,?,?,?,?,?,?,?,1)",
                        (run_id, name, node, status, now, device or "", account_id, resolved_chat_id,
                         res.runner, runner_version or "", call_unread, res.model, res.provider, int(res.ok),
                         res.cost_usd, res.tokens_in, res.tokens_out, res.turns, round(seconds, 2),
                         (res.error or "")[:500], now, res.first_context_tokens, effort or "", tier or "",
                         try_no, run_id, name, kind or "", res.cache_read, res.cache_write,
                         round(res.api_seconds, 2), auth_mode or res.auth_mode, settings_sha or None,
                         prompt_version or None, prompt_level or None, schema_version or None),
                    )
                elif lifecycle:
                    cur = c.execute(
                        "INSERT INTO steps (run_id, step, node, status, queued_at, device, account_id, chat_id, "
                        "runner, model, provider, ok, cost_usd, tokens_in, tokens_out, turns, seconds, error, ts, "
                        "first_context_tokens, effort, tier, try, kind, cache_read, cache_write, api_seconds, "
                        "auth_mode, settings_sha, prompt_version, prompt_level, schema_version, version) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "COALESCE(NULLIF(?, 0), (SELECT COALESCE(MAX(try), 0) + 1 FROM steps WHERE run_id = ? AND step = ?)),"
                        "?,?,?,?,?,?,?,?,?,1)",
                        (run_id, name, node, status, now, device or "", account_id, resolved_chat_id,
                         res.runner, res.model, res.provider, int(res.ok), res.cost_usd, res.tokens_in,
                         res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], now,
                         res.first_context_tokens, effort or "", tier or "", try_no, run_id, name, kind or "",
                         res.cache_read, res.cache_write, round(res.api_seconds, 2), auth_mode or res.auth_mode,
                         settings_sha or None, prompt_version or None, prompt_level or None, schema_version or None),
                    )
                elif bound:
                    cur = c.execute(
                        "INSERT INTO steps (run_id, step, runner, model, provider, ok, cost_usd, tokens_in, tokens_out,"
                        " turns, seconds, error, ts, first_context_tokens, effort, tier, try, kind, cache_read, cache_write,"
                        " api_seconds, auth_mode, settings_sha, prompt_version, prompt_level, schema_version) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "COALESCE(NULLIF(?, 0), (SELECT COALESCE(MAX(try), 0) + 1 FROM steps WHERE run_id = ? AND step = ?)),"
                        "?,?,?,?,?,?,?,?,?)",
                        (run_id, name, res.runner, res.model, res.provider, int(res.ok), res.cost_usd, res.tokens_in,
                         res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], now,
                         res.first_context_tokens, effort or "", tier or "", try_no, run_id, name, kind or "",
                         res.cache_read, res.cache_write, round(res.api_seconds, 2), auth_mode or res.auth_mode,
                         settings_sha or None, prompt_version or None, prompt_level or None, schema_version or None),
                    )
                elif pinned:
                    cur = c.execute(
                        "INSERT INTO steps (run_id, step, runner, model, provider, ok, cost_usd, tokens_in, tokens_out,"
                        " turns, seconds, error, ts, first_context_tokens, effort, tier, try, kind, cache_read, cache_write,"
                        " api_seconds, auth_mode, settings_sha) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "COALESCE(NULLIF(?, 0), (SELECT COALESCE(MAX(try), 0) + 1 FROM steps WHERE run_id = ? AND step = ?)),"
                        "?,?,?,?,?,?)",
                        (run_id, name, res.runner, res.model, res.provider, int(res.ok), res.cost_usd, res.tokens_in,
                         res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], now,
                         res.first_context_tokens, effort or "", tier or "", try_no, run_id, name, kind or "", res.cache_read,
                         res.cache_write, round(res.api_seconds, 2), auth_mode or res.auth_mode, settings_sha or None))
                else:
                    cur = c.execute(
                        "INSERT INTO steps (run_id, step, runner, model, provider, ok, cost_usd, tokens_in, tokens_out,"
                        " turns, seconds, error, ts, first_context_tokens, effort, tier, try, kind, cache_read, cache_write,"
                        " api_seconds, auth_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "COALESCE(NULLIF(?, 0), (SELECT COALESCE(MAX(try), 0) + 1 FROM steps WHERE run_id = ? AND step = ?)),"
                        "?,?,?,?,?)",
                        (run_id, name, res.runner, res.model, res.provider, int(res.ok), res.cost_usd, res.tokens_in,
                         res.tokens_out, res.turns, round(seconds, 2), (res.error or "")[:500], now,
                         res.first_context_tokens, effort or "", tier or "", try_no, run_id, name, kind or "", res.cache_read,
                         res.cache_write, round(res.api_seconds, 2), auth_mode or res.auth_mode))
                written_step_id = cur.lastrowid
                delta_cost = res.cost_usd
                delta_tin = res.tokens_in
                delta_tout = res.tokens_out
            if pinned and inputs and written_step_id is not None:
                c.executemany(
                    "INSERT INTO step_inputs (step_id, role, name, version, level, sha256, call) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(written_step_id, str(item.get("role") or ""), str(item.get("name") or ""),
                      str(item.get("version") or ""), str(item.get("level") or ""),
                      str(item.get("sha256") or ""), int(item.get("call") or 0))
                     for item in inputs],
                )
            writes.update("runs", run_id, {}, increments={"cost_usd": delta_cost, "tokens_in": delta_tin,
                                                         "tokens_out": delta_tout})
            if lifecycle and written_step_id is not None:
                from .. import inbox

                inbox.on_step_settled(c, written_step_id)


def label_step(run_id: str, step: str, try_no: int, **labels) -> None:
    validate_columns(labels, STEP_LABEL_FIELDS, "steps")
    if not labels:
        return
    node, _ = step_node_and_chat(step)
    with db() as c:
        if has_steps_lifecycle(c):
            c.execute(
                f"UPDATE steps SET {', '.join(f'{k} = ?' for k in labels)}, version = version + 1 "  # noqa: S608
                "WHERE run_id = ? AND (node = ? OR step = ?) AND try = ?",
                (*labels.values(), run_id, node, step, try_no),
            )
        else:
            c.execute(f"UPDATE steps SET {', '.join(f'{k} = ?' for k in labels)} WHERE run_id = ? AND step = ? AND try = ?",  # noqa: S608
                      (*labels.values(), run_id, step, try_no))


def hold_ab(run_id: str, role: str, name: str, side: str, arm: dict) -> None:
    with writes.atomic() as c:
        row = c.execute("SELECT ab_held FROM runs WHERE id = ?", (run_id,)).fetchone()
        held = {**json_dict(row["ab_held"] if row else ""), role: [name, side, arm]}
        writes.update("runs", run_id, {"ab_held": json.dumps(held)})


def note(run_id: str, *, base_version=None, **fields) -> None:
    if "status" in fields:
        raise ValueError("use move to change a run's status")
    if fields.get("pid"):
        from . import factory

        with writes.atomic() as c:
            filtered = filter_run_fields(c, fields)
            if filtered:
                writes.update("runs", run_id, filtered, base_version=base_version)
            factory.register(c, run_id, fields["pid"], "run")
    else:
        with writes.atomic() as c:
            filtered = filter_run_fields(c, fields)
            if filtered:
                writes.update("runs", run_id, filtered, base_version=base_version)


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
        if not row or row["status"] not in ("running", "queued"):
            return row
        if row["stop_asked"]:
            if not events.reuse_receipt(f"run:{run_id}", row["version"]):
                raise refusals.Conflict("This run already has a stop request, but no retained receipt for its "
                                        "current version (legacy or expired history); a repeated stop cannot "
                                        "be acknowledged with an audit receipt.")
            return row
        return writes.update("runs", run_id, {"stop_asked": util.iso()}, base_version=row["version"],
                             expect=("running", "queued"))


def _close_open_steps_in_tx(c, run_id: str) -> dict[str, float | int]:
    if not has_steps_lifecycle(c):
        return {}
    rows = c.execute(
        "SELECT id, cost_usd, tokens_in, tokens_out, cache_read, turns, ts FROM steps "
        "WHERE run_id = ? AND status IN ('queued', 'waiting', 'running') ORDER BY id",
        (run_id,),
    ).fetchall()
    if not rows:
        return {}
    turns_available = has_record_turns(c)
    now = util.iso()
    delta_cost = 0.0
    delta_tin = 0
    delta_tout = 0
    for row in rows:
        prev_cost = float(row["cost_usd"] or 0.0)
        prev_tin = int(row["tokens_in"] or 0)
        prev_tout = int(row["tokens_out"] or 0)
        prev_cr = int(row["cache_read"] or 0)
        prev_turns = int(row["turns"] or 0)
        new_cost, new_tin, new_tout, new_cr, new_turns = prev_cost, prev_tin, prev_tout, prev_cr, prev_turns
        if turns_available:
            agg = c.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(tokens_in), 0) AS tin, COALESCE(SUM(cache_read), 0) AS cr, "
                "COALESCE(SUM(tokens_out), 0) AS tout, COALESCE(SUM(cost_nusd), 0) AS cnusd "
                "FROM turn_index WHERE step_id = ?",
                (row["id"],),
            ).fetchone()
            if agg and agg["n"]:
                recovered_cost = agg["cnusd"] / 1_000_000_000
                new_cost = max(prev_cost, recovered_cost)
                new_tin = max(prev_tin, int(agg["tin"]))
                new_tout = max(prev_tout, int(agg["tout"]))
                new_cr = max(prev_cr, int(agg["cr"]))
                new_turns = max(prev_turns, int(agg["n"]))
        c.execute(
            "UPDATE steps SET status = 'fault', ok = 0, error = 'interrupted', "
            "ts = COALESCE(NULLIF(ts, ''), ?), cost_usd = ?, tokens_in = ?, tokens_out = ?, "
            "cache_read = ?, turns = ?, waiting_since = NULL, version = version + 1 WHERE id = ?",
            (now, new_cost, new_tin, new_tout, new_cr, new_turns, row["id"]),
        )
        delta_cost += new_cost - prev_cost
        delta_tin += new_tin - prev_tin
        delta_tout += new_tout - prev_tout
    increments: dict[str, float | int] = {}
    if delta_cost:
        increments["cost_usd"] = delta_cost
    if delta_tin:
        increments["tokens_in"] = delta_tin
    if delta_tout:
        increments["tokens_out"] = delta_tout
    return increments


def settle(run_id: str, fields: dict, end_line: str = "", base_version=None) -> dict | None:
    from . import factory

    with writes.atomic() as c:
        current = c.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not current or current["status"] not in ("running", "queued"):
            if base_version is not None:
                writes.check_run(run_id, base_version)
            return None
        increments = _close_open_steps_in_tx(c, run_id)
        settled = writes.update("runs", run_id, fields, expect=("running", "queued"), kind="run.ended",
                                base_version=base_version, increments=increments or None)
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
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE status NOT IN ('running', 'queued') "
                                           "AND COALESCE(finished, started) >= ? AND COALESCE(finished, started) < ? "
                                           "ORDER BY started", (since, until))]


def purge_queue(connect=reader) -> list[dict]:
    with connect() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT p.run_id, p.reason, p.queued, COALESCE(r.kind, '') AS kind "
                "FROM run_purges p LEFT JOIN runs r ON r.id = p.run_id ORDER BY p.run_id"
            )
        ]


def forget_purge(run_id: str) -> None:
    with db() as c:
        c.execute("DELETE FROM run_purges WHERE run_id = ?", (run_id,))


def mined_until() -> str | None:
    with reader() as c:
        return c.execute("SELECT MAX(mined_until) FROM runs WHERE kind = 'miner'").fetchone()[0]


def orphan_rows(c, table, limit):
    if table not in ("steps", "feed"):
        raise ValueError(table)
    query = (f"FROM {table} t LEFT JOIN runs ON runs.id = t.run_id "
             "WHERE t.run_id IS NOT NULL AND t.run_id <> '' AND runs.id IS NULL")
    count = c.execute(f"SELECT COUNT(*) {query}").fetchone()[0]
    rows = c.execute(f"SELECT t.rowid, t.run_id {query} ORDER BY t.rowid LIMIT ?", (limit,)).fetchall() if limit else []
    return count, rows


def folder_rows(c):
    return c.execute("SELECT id, kind, started, log, files_purged FROM runs ORDER BY id").fetchall()
