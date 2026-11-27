PHASE = "expand"

SQL = """
CREATE TABLE ticket_comments (
    repo TEXT NOT NULL,
    ticket TEXT NOT NULL,
    tracker TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    author_display TEXT NOT NULL DEFAULT '',
    relayed_by TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL DEFAULT '',
    ticket_state TEXT NOT NULL DEFAULT 'open',
    authorized INTEGER NOT NULL DEFAULT 0,
    auth_reason TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT 'neutral',
    route TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL DEFAULT 'recorded',
    action_id TEXT NOT NULL DEFAULT '',
    parent_ticket TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    recorded TEXT NOT NULL,
    PRIMARY KEY (repo, ticket, tracker, comment_id)
);

CREATE INDEX ticket_comments_pending ON ticket_comments(repo, ticket, status, intent);
CREATE INDEX ticket_comments_route ON ticket_comments(repo, route, status);

CREATE TABLE ticket_clarifications (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    ticket TEXT NOT NULL,
    tracker TEXT NOT NULL,
    source_comment_id TEXT NOT NULL,
    question TEXT NOT NULL,
    marker TEXT NOT NULL DEFAULT '',
    comment_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'asking',
    answer_comment_id TEXT NOT NULL DEFAULT '',
    answer_author TEXT NOT NULL DEFAULT '',
    answer_body TEXT NOT NULL DEFAULT '',
    asked_at TEXT NOT NULL,
    answered_at TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX ticket_clarifications_open ON ticket_clarifications(repo, ticket, status);
"""
