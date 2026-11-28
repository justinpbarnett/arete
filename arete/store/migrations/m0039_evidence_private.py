PHASE = "expand"

SQL = """
CREATE TABLE private_evidence (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    ticket TEXT NOT NULL,
    ticket_ref TEXT NOT NULL DEFAULT '',
    tracker TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    ref_key TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'body',
    name TEXT NOT NULL DEFAULT '',
    original_url TEXT NOT NULL DEFAULT '',
    attachment_id TEXT NOT NULL DEFAULT '',
    media_kind TEXT NOT NULL DEFAULT '',
    failure_status TEXT NOT NULL DEFAULT '',
    failure_detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    private_path TEXT NOT NULL DEFAULT '',
    private_sha256 TEXT NOT NULL DEFAULT '',
    private_mime TEXT NOT NULL DEFAULT '',
    private_name TEXT NOT NULL DEFAULT '',
    private_size INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    resume_pending INTEGER NOT NULL DEFAULT 0,
    resumed_run_id TEXT NOT NULL DEFAULT '',
    opened TEXT NOT NULL,
    updated TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    resolved_by TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX private_evidence_ticket_ref ON private_evidence (repo, ticket, ref_key);
CREATE INDEX private_evidence_open_ticket ON private_evidence (repo, ticket, status);
CREATE INDEX private_evidence_resume_pending ON private_evidence (repo, resume_pending, status);
"""
