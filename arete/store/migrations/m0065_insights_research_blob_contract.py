from __future__ import annotations

PHASE = "contract"


def apply(conn) -> None:
    from arete.store import blobs, db, tasks

    cols = {r[1] for r in conn.execute("PRAGMA table_info(research)")}
    if "report" in cols:
        rows = [dict(r) for r in conn.execute("SELECT * FROM research").fetchall()]
        with blobs.lock():
            for row in rows:
                sha: str | None = None
                ref_id: int | None = None
                if row.get("report") and not row.get("report_blob"):
                    sha = blobs.put(str(row["report"]).encode("utf-8"), "research_report", conn=conn)
                if "ref_id" in cols and row.get("ref_id") is None and row.get("ref"):
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
        conn.execute("ALTER TABLE research DROP COLUMN report")

    target = str(db.RESEARCH_BLOB_CONTRACT_SCHEMA)
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < ? "
        "THEN ? ELSE value END WHERE key = 'oldest_writer'",
        (db.RESEARCH_BLOB_CONTRACT_SCHEMA, target),
    )
