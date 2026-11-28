PHASE = "expand"

SQL = """
CREATE TABLE ticket_followups (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    ticket TEXT NOT NULL,
    tracker TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    original_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planned',
    reply_marker TEXT NOT NULL DEFAULT '',
    reply_url TEXT NOT NULL DEFAULT '',
    reply_body TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    UNIQUE (repo, ticket, tracker, comment_id)
);

CREATE INDEX ticket_followups_status ON ticket_followups(repo, status);

CREATE TABLE ticket_followup_items (
    id TEXT PRIMARY KEY,
    followup_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    repo TEXT NOT NULL,
    original_ticket TEXT NOT NULL,
    tracker TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'create',
    outcome_label TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    publication_key TEXT NOT NULL DEFAULT '',
    marker TEXT NOT NULL DEFAULT '',
    target_ticket TEXT NOT NULL DEFAULT '',
    target_ref TEXT NOT NULL DEFAULT '',
    target_url TEXT NOT NULL DEFAULT '',
    evidence_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    linked INTEGER NOT NULL DEFAULT 0,
    depends_on TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    UNIQUE (followup_id, seq)
);

CREATE INDEX ticket_followup_items_origin ON ticket_followup_items(repo, original_ticket, tracker, kind, action);
CREATE INDEX ticket_followup_items_target ON ticket_followup_items(repo, tracker, target_ticket);
"""
