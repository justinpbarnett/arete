from __future__ import annotations

PHASE = "contract"
FAILS_WHEN_DEAD = ("miner", "eval")
INTERRUPTED = "stopped before it finished (the Mac restarted, lost power or the process was killed)"
STOPPED = "stopped by request"


def _specgraph_unfinished(conn, repo: str | None, issue) -> bool:
    if not repo or issue in (None, ""):
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spec_deliveries'").fetchone():
        return False
    row = conn.execute(
        "SELECT 1 FROM spec_deliveries WHERE repo = ? AND issue = ? AND state != 'complete' LIMIT 1",
        (repo, str(issue)),
    ).fetchone()
    return row is not None


def _dead_status_in_conn(conn, row: dict) -> str:
    if row.get("dry_run") or row.get("stop_asked"):
        return "stopped"
    if row.get("only_step"):
        return "interrupted"
    if row["kind"] == "pipeline":
        if row.get("task_id") is not None:
            earlier = conn.execute(
                "SELECT id, status FROM runs WHERE kind = 'pipeline' AND task_id = ? "
                "AND COALESCE(only_step, '') = '' AND dry_run = 0 AND status != 'dry-run' "
                "AND status != 'queued' AND id != ? "
                "ORDER BY started DESC, COALESCE(seq, 0) DESC, id DESC LIMIT 1",
                (int(row["task_id"]), row["id"]),
            ).fetchone()
        else:
            issue_val = row.get("issue_key") or row.get("issue")
            earlier = conn.execute(
                "SELECT id, status FROM runs WHERE kind = 'pipeline' AND repo = ? AND issue_key = ? "
                "AND COALESCE(only_step, '') = '' AND dry_run = 0 AND status != 'dry-run' "
                "AND status != 'queued' AND id != ? "
                "ORDER BY started DESC, COALESCE(seq, 0) DESC, id DESC LIMIT 1",
                (row.get("repo"), issue_val, row["id"]),
            ).fetchone()
        if earlier and earlier["status"] == "interrupted" and not _specgraph_unfinished(
            conn, row.get("repo"), row.get("issue_key") or row.get("issue"),
        ):
            return "failed"
        return "interrupted"
    return "failed" if row["kind"] in FAILS_WHEN_DEAD else "interrupted"


def apply(conn) -> None:
    from arete import util

    now = util.iso()
    dups = conn.execute(
        "SELECT lock FROM runs WHERE status = 'running' AND lock IS NOT NULL "
        "GROUP BY lock HAVING COUNT(*) > 1 ORDER BY lock"
    ).fetchall()
    for dup in dups:
        lock = dup["lock"]
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM runs WHERE status = 'running' AND lock = ? "
                "ORDER BY started DESC, COALESCE(seq, 0) DESC, id DESC",
                (lock,),
            ).fetchall()
        ]
        # Keep rows[0] (newest); settle older rows chronologically.
        for older in reversed(rows[1:]):
            status = _dead_status_in_conn(conn, older)
            summary = STOPPED if status == "stopped" else INTERRUPTED
            conn.execute(
                "UPDATE runs SET status = ?, finished = COALESCE(finished, ?), summary = ?, "
                "version = version + 1 WHERE id = ? AND status = 'running'",
                (status, now, summary, older["id"]),
            )

    conn.execute(
        "CREATE UNIQUE INDEX runs_one_live ON runs (lock) WHERE status = 'running' AND lock IS NOT NULL"
    )
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < 74 "
        "THEN '74' ELSE value END WHERE key = 'oldest_writer'"
    )
