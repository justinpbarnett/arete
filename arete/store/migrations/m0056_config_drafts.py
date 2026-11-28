from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

PHASE = "expand"

LEGACY_STATE_MAP: dict[str, str] = {
    "pending": "valid",
    "hand": "valid",
    "memory": "valid",
    "approve": "valid",
    "filing": "valid",
    "applied": "valid",
    "merged": "active",
    "rejected": "declined",
    "skipped": "declined",
    "filed": "discarded",
    "superseded": "discarded",
}

_DDL = """
CREATE TABLE config_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    base_version TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    diff TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    instruction TEXT NOT NULL DEFAULT '',
    why TEXT NOT NULL DEFAULT '',
    gate TEXT NOT NULL CHECK (gate IN ('A', 'B', 'C')),
    check_report TEXT NOT NULL DEFAULT '',
    validator TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft',
    decline_reason TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '{}',
    activated_commit TEXT NOT NULL DEFAULT '',
    legacy_proposal INTEGER UNIQUE,
    pr_url TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    CHECK (NOT (gate = 'C' AND author LIKE 'run:%' AND state NOT IN ('draft', 'invalid', 'declined', 'discarded')))
);

CREATE INDEX config_drafts_state ON config_drafts (state, kind, name);
"""

_VIEW_AND_TRIGGERS = """
DROP TABLE proposals;

CREATE VIEW proposals AS
SELECT
    id AS id,
    COALESCE(json_extract(evidence, '$.run'), '') AS run_id,
    name AS layer,
    CASE WHEN origin LIKE 'repo:%' THEN substr(origin, 6) ELSE origin END AS repo,
    path AS file,
    body AS change,
    why AS why,
    COALESCE(json_extract(evidence, '$.cites'), '[]') AS evidence,
    COALESCE(json_extract(evidence, '$.confidence'), 0.0) AS confidence,
    state AS status,
    COALESCE(pr_url, '') AS pr_url,
    created AS created
FROM config_drafts
WHERE kind = 'miner';

CREATE TRIGGER proposals_instead_insert
INSTEAD OF INSERT ON proposals
BEGIN
    INSERT INTO config_drafts (
        id, kind, name, origin, path, base_version, body, diff, author, model,
        instruction, why, gate, check_report, validator, state, decline_reason,
        evidence, activated_commit, legacy_proposal, pr_url, version, created, updated
    )
    VALUES (
        NEW.id,
        'miner',
        COALESCE(NEW.layer, ''),
        'repo:' || COALESCE(NEW.repo, ''),
        COALESCE(NEW.file, ''),
        '',
        COALESCE(NEW.change, ''),
        '',
        'miner',
        '',
        '',
        COALESCE(NEW.why, ''),
        'B',
        '',
        '',
        CASE NEW.status
            WHEN 'pending' THEN 'valid'
            WHEN 'hand' THEN 'valid'
            WHEN 'memory' THEN 'valid'
            WHEN 'approve' THEN 'valid'
            WHEN 'filing' THEN 'valid'
            WHEN 'applied' THEN 'valid'
            WHEN 'merged' THEN 'active'
            WHEN 'rejected' THEN 'declined'
            WHEN 'skipped' THEN 'declined'
            WHEN 'filed' THEN 'discarded'
            WHEN 'superseded' THEN 'discarded'
            WHEN 'valid' THEN 'valid'
            WHEN 'active' THEN 'active'
            WHEN 'declined' THEN 'declined'
            WHEN 'discarded' THEN 'discarded'
            WHEN 'invalid' THEN 'invalid'
            WHEN 'draft' THEN 'draft'
            ELSE 'draft'
        END,
        '',
        json_object(
            'run', COALESCE(NEW.run_id, ''),
            'cites', CASE WHEN json_valid(NEW.evidence) THEN json(NEW.evidence) ELSE json('[]') END,
            'confidence', COALESCE(NEW.confidence, 0.0)
        ),
        '',
        NEW.id,
        NULLIF(COALESCE(NEW.pr_url, ''), ''),
        1,
        COALESCE(NULLIF(NEW.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        COALESCE(NULLIF(NEW.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );
END;

CREATE TRIGGER proposals_instead_update
INSTEAD OF UPDATE ON proposals
BEGIN
    UPDATE config_drafts
    SET
        name = COALESCE(NEW.layer, name),
        origin = 'repo:' || COALESCE(NEW.repo, CASE WHEN origin LIKE 'repo:%' THEN substr(origin, 6) ELSE origin END),
        path = COALESCE(NEW.file, path),
        body = COALESCE(NEW.change, body),
        why = COALESCE(NEW.why, why),
        evidence = json_object(
            'run', COALESCE(NEW.run_id, ''),
            'cites', CASE WHEN json_valid(NEW.evidence) THEN json(NEW.evidence) ELSE json('[]') END,
            'confidence', COALESCE(NEW.confidence, 0.0)
        ),
        state = CASE NEW.status
            WHEN 'pending' THEN 'valid'
            WHEN 'hand' THEN 'valid'
            WHEN 'memory' THEN 'valid'
            WHEN 'approve' THEN 'valid'
            WHEN 'filing' THEN 'valid'
            WHEN 'applied' THEN 'valid'
            WHEN 'merged' THEN 'active'
            WHEN 'rejected' THEN 'declined'
            WHEN 'skipped' THEN 'declined'
            WHEN 'filed' THEN 'discarded'
            WHEN 'superseded' THEN 'discarded'
            WHEN 'valid' THEN 'valid'
            WHEN 'active' THEN 'active'
            WHEN 'declined' THEN 'declined'
            WHEN 'discarded' THEN 'discarded'
            WHEN 'invalid' THEN 'invalid'
            WHEN 'draft' THEN 'draft'
            ELSE 'draft'
        END,
        pr_url = NULLIF(COALESCE(NEW.pr_url, ''), ''),
        version = version + 1,
        updated = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE id = OLD.id AND kind = 'miner';
END;
"""


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_cites(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [raw]
    if isinstance(parsed, list):
        return parsed
    return [parsed]


def _exec_sql(conn: sqlite3.Connection, sql: str) -> None:
    pending = ""
    for char in sql:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip():
        conn.execute(pending)


def apply(conn: sqlite3.Connection) -> None:
    now = _iso_now()
    _exec_sql(conn, _DDL)

    has_proposals = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'proposals'"
    ).fetchone() is not None
    has_inbox = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inbox'"
    ).fetchone() is not None

    if not has_proposals:
        return

    rows = [dict(r) for r in conn.execute("SELECT * FROM proposals ORDER BY id ASC").fetchall()]
    max_id = 0
    for row in rows:
        pid = int(row["id"])
        if pid > max_id:
            max_id = pid
        old_status = row["status"]
        unknown_word: str | None = None
        if old_status is None:
            new_state = "draft"
            unknown_word = "NULL"
        elif old_status in LEGACY_STATE_MAP:
            new_state = LEGACY_STATE_MAP[old_status]
        else:
            new_state = "draft"
            unknown_word = str(old_status)

        pr_url = (row["pr_url"] or "").strip() or None
        run_id = row["run_id"] or ""
        layer = row["layer"] or ""
        repo = row["repo"] or ""
        file_path = row["file"] or ""
        change = row["change"] or ""
        why = row["why"] or ""
        confidence = float(row["confidence"]) if row["confidence"] is not None else 0.0
        created = row["created"] or now
        evidence_obj: dict[str, object] = {
            "run": run_id,
            "cites": _parse_cites(row["evidence"]),
            "confidence": confidence,
        }
        if unknown_word is not None:
            evidence_obj["legacy_status"] = unknown_word

        conn.execute(
            "INSERT INTO config_drafts ("
            "id, kind, name, origin, path, base_version, body, diff, author, model, "
            "instruction, why, gate, check_report, validator, state, decline_reason, "
            "evidence, activated_commit, legacy_proposal, pr_url, version, created, updated"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                "miner",
                layer,
                f"repo:{repo}",
                file_path,
                "",
                change,
                "",
                "miner",
                "",
                "",
                why,
                "B",
                "",
                "",
                new_state,
                "",
                json.dumps(evidence_obj, sort_keys=True, separators=(",", ":")),
                "",
                pid,
                pr_url,
                1,
                created,
                created,
            ),
        )

        if has_inbox:
            needs_item = (new_state == "valid" and pr_url is None) or new_state == "draft"
            old_subj = f"proposal:{pid}"
            new_subj = f"draft:{pid}"
            if unknown_word is not None:
                title = f"Miner proposal #{pid} (unknown status {unknown_word})"
            else:
                title = file_path or change or f"Proposal #{pid}"
            priority = 2 if old_status in ("approve", "filing") else 3
            detail = json.dumps(
                {
                    "id": pid,
                    "draft_id": f"CD-{pid}",
                    "kind": "miner",
                    "name": layer,
                    "repo": repo,
                    "file": file_path,
                    "change": change,
                    "why": why,
                    "state": new_state,
                    "gate": "B",
                    **({"legacy_status": unknown_word} if unknown_word is not None else {}),
                    "stamp": f"{new_state}:{pid}:1",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            verbs = json.dumps(["accept", "decline"], separators=(",", ":"))
            existing_old = conn.execute(
                "SELECT id FROM inbox WHERE subject = ? AND state != 'resolved' ORDER BY id DESC LIMIT 1",
                (old_subj,),
            ).fetchone()
            if needs_item:
                if existing_old is not None:
                    conn.execute(
                        "UPDATE inbox SET subject = ?, title = ?, detail = ?, priority = ?, verbs = ?, "
                        "version = version + 1 WHERE id = ?",
                        (new_subj, title, detail, priority, verbs, existing_old["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO inbox "
                        "(kind, subject, task_id, run_id, line, priority, title, detail, verbs, options, state, raised, version) "
                        "VALUES ('proposal', ?, '', ?, ?, ?, ?, ?, ?, '[]', 'open', ?, 1)",
                        (new_subj, run_id, repo, priority, title, detail, verbs, now),
                    )
            elif existing_old is not None:
                conn.execute(
                    "UPDATE inbox SET state = 'resolved', resolution = 'cleared', resolved = ?, "
                    "version = version + 1 WHERE id = ?",
                    (now, existing_old["id"]),
                )

    if has_inbox:
        conn.execute(
            "UPDATE inbox SET state = 'resolved', resolution = 'cleared', resolved = ?, "
            "version = version + 1 WHERE subject LIKE 'proposal:%' AND state != 'resolved'",
            (now,),
        )

    if max_id > 0:
        seq_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'config_drafts'"
        ).fetchone()
        if seq_row is None:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('config_drafts', ?)", (max_id,))
        elif int(seq_row["seq"]) < max_id:
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'config_drafts'", (max_id,))

    _exec_sql(conn, _VIEW_AND_TRIGGERS)
