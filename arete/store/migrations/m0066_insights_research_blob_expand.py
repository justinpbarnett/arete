from __future__ import annotations

PHASE = "expand"


def apply(conn) -> None:
    from arete.store import blobs, tasks

    cols = {r[1] for r in conn.execute("PRAGMA table_info(research)")}
    if "report_blob" not in cols:
        conn.execute("ALTER TABLE research ADD COLUMN report_blob TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS research_ref_id ON research (ref_id)")

    has_report = "report" in cols
    has_ref_id = "ref_id" in cols
    rows = [dict(r) for r in conn.execute("SELECT * FROM research").fetchall()]
    with blobs.lock():
        for row in rows:
            sha: str | None = None
            ref_id: int | None = None
            if has_report and row.get("report") and not row.get("report_blob"):
                sha = blobs.put(str(row["report"]).encode("utf-8"), "research_report", conn=conn)
            if has_ref_id and row.get("ref_id") is None and row.get("ref"):
                ref_id = tasks.TaskStore.ref_for_triage(conn, row, actor="hand", emit_event=False)
            if sha is not None and ref_id is not None:
                conn.execute(
                    "UPDATE research SET report_blob = ?, ref_id = ? WHERE ref = ?",
                    (sha, ref_id, row["ref"]),
                )
            elif sha is not None:
                conn.execute("UPDATE research SET report_blob = ? WHERE ref = ?", (sha, row["ref"]))
            elif ref_id is not None:
                conn.execute("UPDATE research SET ref_id = ? WHERE ref = ?", (ref_id, row["ref"]))
