from __future__ import annotations

import json

PHASE = "expand"


def _iso_updated(raw: str | None) -> str:
    from arete import jira, util
    from datetime import UTC, datetime

    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        when = jira.parse_time(text)
    except ValueError:
        return util.iso(datetime.min.replace(tzinfo=UTC))
    if not when.tzinfo:
        when = when.replace(tzinfo=UTC)
    return util.iso(when)


def _json_text(value: object, default: str = "[]") -> str:
    if isinstance(value, str):
        return value if value.strip() else default
    if value is None:
        return default
    return json.dumps(value)


def backfill_triage_rows(conn) -> None:
    from arete import util
    from arete.store import tasks

    tbl = conn.execute("SELECT type FROM sqlite_master WHERE name = 'triage'").fetchone()
    if tbl is None or tbl[0] != "table":
        return

    now = util.iso()
    rows = [dict(r) for r in conn.execute("SELECT * FROM triage").fetchall()]
    sources_seen: set[str] = set()

    for row in rows:
        source = str(row.get("source") or "").strip()
        if source:
            sources_seen.add(source)

    for source in sorted(sources_seen):
        cur_name = f"triage:{source}"
        existing_cur = conn.execute("SELECT value FROM cursors WHERE source = ?", (cur_name,)).fetchone()
        if existing_cur is None or not existing_cur[0]:
            conn.execute(
                "INSERT OR REPLACE INTO cursors (source, value, polled, ok, detail, last_ok) VALUES (?, ?, ?, 1, '', ?)",
                (cur_name, now, now, now),
            )

    cursor_by_source = {
        str(r["source"]).removeprefix("triage:"): str(r["value"])
        for r in conn.execute("SELECT source, value FROM cursors WHERE source LIKE 'triage:%' AND value IS NOT NULL")
    }

    for row in rows:
        raw_ref = str(row.get("ref") or "").strip()
        if not raw_ref:
            continue
        source = str(row.get("source") or "").strip()
        stamp = cursor_by_source.get(source, now)
        updated_iso = _iso_updated(row.get("updated"))
        ref_id = row.get("ref_id")
        if ref_id is None:
            ref_id = tasks.TaskStore.ref_for_triage(conn, row, actor="hand", emit_event=False)
        if ref_id is None:
            continue
        existing_ref = conn.execute("SELECT facts, title, url, connector FROM refs WHERE id = ?", (ref_id,)).fetchone()
        facts: dict[str, object] = {}
        if existing_ref is not None and existing_ref["facts"]:
            try:
                parsed = json.loads(existing_ref["facts"])
                if isinstance(parsed, dict):
                    facts = parsed
            except ValueError:
                facts = {}
        facts["triage_ref"] = raw_ref
        if row.get("in_sprint") is not None:
            facts["sprint"] = row.get("in_sprint")
        if row.get("parent") is not None:
            facts["parent"] = str(row.get("parent") or "")
        listed_at = None if row.get("closed") else stamp
        conn.execute(
            "UPDATE refs SET connector = COALESCE(NULLIF(?, ''), connector), "
            "title = COALESCE(NULLIF(?, ''), title), url = COALESCE(NULLIF(?, ''), url), "
            "remote_updated = COALESCE(NULLIF(?, ''), remote_updated), "
            "facts = ?, listed_at = COALESCE(?, listed_at), updated = ? WHERE id = ?",
            (
                source,
                str(row.get("title") or ""),
                str(row.get("url") or ""),
                updated_iso,
                json.dumps(facts),
                listed_at,
                now,
                ref_id,
            ),
        )
        if row.get("ref_id") is None:
            conn.execute("UPDATE triage SET ref_id = ? WHERE ref = ?", (ref_id, raw_ref))
        at_ts = str(row.get("ts") or now)
        exists = conn.execute(
            "SELECT 1 FROM triage_verdicts WHERE ref_id = ? AND at = ? AND judged_updated = ? LIMIT 1",
            (ref_id, at_ts, updated_iso),
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO triage_verdicts "
                "(ref, ref_id, source, tracker, repo, route, ready, code, size, kind, reason, model, run_id, at, "
                " questions, plan, assumptions, checks, repos, why, cost, judged_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    raw_ref,
                    int(ref_id),
                    source,
                    str(row.get("tracker") or ""),
                    str(row.get("repo") or ""),
                    str(row.get("route") or ""),
                    row.get("ready"),
                    row.get("code"),
                    str(row.get("size") or ""),
                    str(row.get("kind") or ""),
                    at_ts,
                    _json_text(row.get("questions"), "[]"),
                    _json_text(row.get("plan"), "[]"),
                    _json_text(row.get("assumptions"), "[]"),
                    _json_text(row.get("checks"), "[]"),
                    _json_text(row.get("repos"), "[]"),
                    str(row.get("why") or ""),
                    float(row.get("cost") or 0.0),
                    updated_iso,
                ),
            )

    for arow in conn.execute("SELECT * FROM triage_approvals WHERE ref_id IS NULL").fetchall():
        d = dict(arow)
        if d.get("ref"):
            rid = tasks.TaskStore.ref_for_triage(conn, d, actor="hand", emit_event=False)
            if rid is not None:
                conn.execute("UPDATE triage_approvals SET ref_id = ? WHERE ref = ?", (rid, d["ref"]))


def apply(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS triage_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref TEXT NOT NULL,
            ref_id INTEGER,
            source TEXT NOT NULL DEFAULT '',
            tracker TEXT NOT NULL DEFAULT '',
            repo TEXT NOT NULL DEFAULT '',
            route TEXT NOT NULL DEFAULT '',
            ready INTEGER,
            code INTEGER,
            size TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            at TEXT NOT NULL,
            questions TEXT NOT NULL DEFAULT '[]',
            plan TEXT NOT NULL DEFAULT '[]',
            assumptions TEXT NOT NULL DEFAULT '[]',
            checks TEXT NOT NULL DEFAULT '[]',
            repos TEXT NOT NULL DEFAULT '[]',
            why TEXT NOT NULL DEFAULT '',
            cost REAL NOT NULL DEFAULT 0,
            judged_updated TEXT NOT NULL DEFAULT ''
        ) STRICT
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS triage_verdicts_ref ON triage_verdicts (ref_id, id)")

    ref_cols = {r[1] for r in conn.execute("PRAGMA table_info(refs)")}
    if "listed_at" not in ref_cols:
        conn.execute("ALTER TABLE refs ADD COLUMN listed_at TEXT")

    backfill_triage_rows(conn)
