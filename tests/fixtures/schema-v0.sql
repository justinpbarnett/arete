CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- pipeline | miner | eval | experiment | draft | loop | session
    repo TEXT,
    issue INTEGER,
    status TEXT NOT NULL,          -- running | pr | needs-info | skipped | noop | dry-run | failed | interrupted
                                   -- | superseded | merged | rejected | done | stopped | step
    started TEXT NOT NULL,
    finished TEXT,
    attempts INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    pr_url TEXT,
    branch TEXT,
    summary TEXT,
    landed TEXT
, files_purged TEXT, mined_until TEXT, loop TEXT, loop_kind TEXT, model TEXT, runner TEXT, device TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, log TEXT, trigger TEXT, queue TEXT, grp TEXT, pid INTEGER, lock TEXT, stop_asked TEXT, moved TEXT, label_pending TEXT, wait_minutes TEXT, only_step TEXT DEFAULT '', engine_sha TEXT, ab_held TEXT DEFAULT '');
CREATE INDEX runs_ticket ON runs (repo, issue, kind, started);
CREATE INDEX runs_started ON runs (started);
CREATE TABLE steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step TEXT,
    runner TEXT,
    model TEXT,
    provider TEXT,
    auth_mode TEXT DEFAULT '',
    ok INTEGER,
    cost_usd REAL DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    turns INTEGER DEFAULT 0,
    seconds REAL,
    error TEXT,
    ts TEXT,
    first_context_tokens INTEGER DEFAULT 0,
    effort TEXT DEFAULT '',
    tier TEXT DEFAULT '',
    try INTEGER DEFAULT 0,
    kind TEXT DEFAULT '',
    cache_read INTEGER DEFAULT 0,
    cache_write INTEGER DEFAULT 0,
    api_seconds REAL DEFAULT 0,
    atom TEXT DEFAULT '',
    ab TEXT DEFAULT '',
    ab_side TEXT DEFAULT ''
);
CREATE INDEX steps_ts ON steps (ts);
CREATE INDEX steps_run ON steps (run_id);
CREATE TABLE feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT,
    kind TEXT,
    text TEXT
);
CREATE INDEX feed_run ON feed (run_id, id);
CREATE TABLE loop_state (
    loop TEXT PRIMARY KEY,
    data TEXT DEFAULT '{}',
    last_fire TEXT,
    next_due TEXT,
    metric_on INTEGER DEFAULT 0,
    updated TEXT
, mark_ok INTEGER, mark_detail TEXT DEFAULT '', mark_at TEXT);
CREATE TABLE findings (
    loop TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    fingerprint_text TEXT DEFAULT '',
    title TEXT,
    url TEXT,
    run_id TEXT,
    seen INTEGER DEFAULT 1,
    first_seen TEXT,
    last_seen TEXT,
    emitted TEXT DEFAULT 'legacy',
    PRIMARY KEY (loop, fingerprint)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT,
    type TEXT,
    source TEXT,
    key TEXT,
    repo TEXT,
    title TEXT,
    url TEXT,
    trust TEXT,
    data TEXT,
    ts TEXT
);
CREATE INDEX events_ts ON events (ts);
CREATE TABLE seen (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    ts TEXT,
    PRIMARY KEY (source, key)
);
CREATE TABLE cursors (
    source TEXT PRIMARY KEY,
    value TEXT,
    polled TEXT,
    ok INTEGER,
    detail TEXT
, last_ok TEXT);
CREATE TABLE source_owners (
    source TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
);
CREATE TABLE queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT NOT NULL,
    queue TEXT NOT NULL,
    grp TEXT DEFAULT '',
    trigger TEXT,
    payload TEXT DEFAULT '[]',
    enqueued TEXT,
    run_id TEXT
, attempts INTEGER DEFAULT 0, dead_at TEXT, dead_reason TEXT DEFAULT '');
CREATE TABLE metrics (
    name TEXT NOT NULL,
    value REAL,
    loop TEXT,
    run_id TEXT,
    ts TEXT
);
CREATE INDEX metrics_name ON metrics (name, ts);
CREATE TABLE approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT,
    run_id TEXT,
    emit TEXT,
    summary TEXT,
    status TEXT,                   -- pending | approved | rejected | done | failed
    result TEXT,
    created TEXT,
    decided TEXT,
    plain_summary TEXT DEFAULT '',
    plain_summary_tried TEXT DEFAULT '',
    review TEXT DEFAULT '',
    review_tried TEXT DEFAULT '',
    claimed_by TEXT DEFAULT '',
    claimed_until TEXT DEFAULT ''
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    text TEXT,
    ts TEXT,
    taken TEXT
);
CREATE TABLE probes (
    name TEXT PRIMARY KEY,
    last_run TEXT,
    last_ok TEXT,
    ok INTEGER,
    detail TEXT
);
CREATE TABLE holds (
    scope TEXT PRIMARY KEY,        -- claude | opus | sonnet
    until TEXT NOT NULL,
    why TEXT,
    ts TEXT
);
CREATE TABLE alerts (
    key TEXT PRIMARY KEY,
    issue INTEGER,
    opened TEXT,
    last_seen TEXT,
    repo TEXT DEFAULT ''
);
CREATE TABLE proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    layer TEXT,
    repo TEXT,
    file TEXT,
    change TEXT,
    why TEXT,
    evidence TEXT,
    confidence REAL,
    status TEXT,                   -- applied | pending | merged | rejected | skipped
    pr_url TEXT,
    created TEXT
);
CREATE TABLE evals (
    run_id TEXT,
    repo TEXT,
    task TEXT,
    passed INTEGER,
    seconds REAL,
    cost_usd REAL,
    ts TEXT,
    source TEXT DEFAULT '',
    engine_sha TEXT DEFAULT '',
    prompts_sha TEXT DEFAULT '',
    prompts_from TEXT DEFAULT '',
    context_sha TEXT DEFAULT '',
    settings TEXT DEFAULT '',
    base_commit TEXT DEFAULT '',
    after_step INTEGER DEFAULT 0,
    last_step INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX evals_key ON evals (run_id, repo, task);
CREATE TABLE feedback (
    pr_url TEXT NOT NULL,
    kind TEXT NOT NULL,            -- review | line | comment
    gh_id INTEGER NOT NULL,
    repo TEXT,
    issue TEXT,
    body TEXT,
    path TEXT,
    line INTEGER,
    hunk TEXT,
    thread INTEGER,
    at_once INTEGER DEFAULT 0,
    created TEXT,
    pr_state TEXT,                 -- OPEN | MERGED | CLOSED when recorded
    status TEXT,                   -- new | taken | done | failed | recorded | skipped
    run_id TEXT,
    asks REAL, recorded TEXT,
    PRIMARY KEY (pr_url, kind, gh_id)
);
CREATE TABLE mined (
    pr_url TEXT PRIMARY KEY,
    repo TEXT,
    issue TEXT,
    run_id TEXT,
    status TEXT,                   -- accepted | rejected | skipped
    why TEXT,
    task TEXT,
    ts TEXT
);
CREATE TABLE triage (
    ref TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    tracker TEXT,
    repo TEXT,
    route TEXT,
    title TEXT,
    url TEXT,
    updated TEXT,
    ready INTEGER,
    code INTEGER,
    size TEXT,
    questions TEXT,
    plan TEXT,
    why TEXT,
    cost REAL DEFAULT 0,
    ts TEXT,
    kind TEXT DEFAULT '',
    repos TEXT DEFAULT '[]',
    assumptions TEXT DEFAULT '[]',
    checks TEXT DEFAULT '[]',
    in_sprint INTEGER DEFAULT 0,
    parent TEXT DEFAULT '',
    closed TEXT DEFAULT ''         -- delivery time of a research ticket shown for 7 days after it left the open ones
);
CREATE TABLE triage_approvals (
    ref TEXT PRIMARY KEY,
    repo TEXT,
    route TEXT,
    title TEXT,
    url TEXT,
    kind TEXT,
    size TEXT,
    plan TEXT,
    assumptions TEXT,
    checks TEXT DEFAULT '[]',
    why TEXT,
    ts TEXT,
    tracker TEXT DEFAULT 'jira',
    sent_by TEXT DEFAULT 'triage'  -- triage | owner
);
CREATE TABLE verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    run_id TEXT,
    loop TEXT,
    kind TEXT,                     -- push | proof | fixed
    subject TEXT,
    about TEXT,
    answers TEXT,
    held TEXT,
    outcome TEXT DEFAULT '',       -- '' | replaced | waiting | landed | failed | approved | rejected | skipped | went on | filed
    outcome_at TEXT,
    sha TEXT DEFAULT '',
    approval INTEGER,
    UNIQUE (run_id, kind, subject)
);
CREATE INDEX verdicts_at ON verdicts (at);
CREATE TABLE research (
    ref TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    url TEXT,
    updated TEXT,
    status TEXT,                   -- running | done | failed | held | parent | answered
    summary TEXT,
    report TEXT,
    error TEXT,
    run_id TEXT,
    cost REAL DEFAULT 0,
    ts TEXT,
    ticket TEXT DEFAULT '',
    basis TEXT DEFAULT '',
    answered_by TEXT DEFAULT '',
    filed_path TEXT DEFAULT '',
    filed_url TEXT DEFAULT '',
    moved TEXT DEFAULT '',         -- the state arete last moved the ticket into: running | failed | pr | done
    delivered TEXT DEFAULT ''      -- when arete moved the ticket to its done status
);
CREATE TABLE chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '',
    project TEXT DEFAULT '',       -- a repo name from the config, or '' for none
    created TEXT,
    updated TEXT,
    spent REAL DEFAULT 0,
    busy INTEGER DEFAULT 0,
    session TEXT DEFAULT '',
    session_spent REAL DEFAULT 0,
    runner TEXT DEFAULT '',
    draft_title TEXT DEFAULT '',
    draft_body TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    summarized_to INTEGER DEFAULT 0
);
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    sender TEXT NOT NULL,          -- you | arete | tool | session
    text TEXT,
    cost REAL DEFAULT 0,
    paid_usd REAL,
    ts TEXT
);
CREATE INDEX chat_messages_chat ON chat_messages (chat_id, id);
CREATE TABLE chat_sessions (
    run_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    budget_usd REAL,
    linked TEXT NOT NULL,
    told TEXT DEFAULT ''
);
CREATE INDEX chat_sessions_chat ON chat_sessions (chat_id);
CREATE TABLE usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    tab TEXT,                      -- one browser tab's random id
    device TEXT,                   -- desktop | phone
    kind TEXT,                     -- visit | action
    view TEXT,
    name TEXT,                     -- the action's name; empty on a visit
    via TEXT,                      -- how a visit began: start | sidebar | key | palette | link | back | close | step | return
    ms INTEGER DEFAULT 0
);
CREATE INDEX usage_at ON usage (at);
CREATE TABLE captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | checking | filing | filed | discarded
    kind TEXT DEFAULT 'idea',               -- idea | adjustment | bug
    text TEXT DEFAULT '',
    expected TEXT DEFAULT '',
    actual TEXT DEFAULT '',
    steps TEXT DEFAULT '',
    context TEXT DEFAULT '',                -- the snapshot's JSON text, or '' for none
    revision INTEGER DEFAULT 0,
    created TEXT,
    updated TEXT,
    jev_cost_usd REAL DEFAULT 0,
    inspect_cost_usd REAL DEFAULT 0,
    written_title TEXT DEFAULT '',
    written_body TEXT DEFAULT '',
    written_print TEXT DEFAULT '',
    written_note TEXT DEFAULT '',
    delivery TEXT DEFAULT '',               -- why the last delivery did not finish
    issue INTEGER,
    issue_url TEXT DEFAULT '',
    issue_title TEXT DEFAULT '',
    filed TEXT
);
CREATE UNIQUE INDEX captures_open ON captures (status) WHERE status = 'draft';
CREATE TABLE capture_checks (
    capture INTEGER NOT NULL,
    name TEXT NOT NULL,
    result TEXT NOT NULL,          -- running | pass | hold | fail | interrupted
    reason TEXT DEFAULT '',
    detail TEXT DEFAULT '{}',      -- JSON
    fingerprint TEXT DEFAULT '',   -- a hash of the inputs this check read
    checked TEXT,
    PRIMARY KEY (capture, name)
);
CREATE TABLE capture_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture INTEGER NOT NULL,      -- the captures row it is staged on
    name TEXT,
    kind TEXT,                     -- image | video
    type TEXT,                     -- the MIME type decided from the file's first bytes
    bytes INTEGER,
    sha256 TEXT,
    source TEXT,                   -- screenshot | recording | attached
    seconds REAL,                  -- NULL when unknown
    added TEXT,
    inspection TEXT DEFAULT '',    -- the inspection result's JSON, or '' for none
    link TEXT DEFAULT ''           -- where it was uploaded, or '' before that
);
CREATE UNIQUE INDEX capture_evidence_once ON capture_evidence (capture, sha256);
CREATE TABLE vms (
    name TEXT PRIMARY KEY,         -- a VM arete asked for and has not yet seen deleted
    run_id TEXT,                   -- NULL for a probe
    device TEXT NOT NULL,          -- JSON of the device that asked, so its host stays reachable if devices.toml changes
    vmid INTEGER,                  -- proxmox only
    created TEXT NOT NULL,
    answered INTEGER DEFAULT 0,    -- 1 once the host answered the create, so it cannot still finish it later
    failing_since TEXT             -- when a delete of it first failed
);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,           -- t- and hex digits, such as t-1a2b
    repo TEXT NOT NULL,
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    kind TEXT DEFAULT 'feat',      -- feat | fix
    state TEXT DEFAULT 'todo',     -- todo | running | pr | failed | needs-info | done | closed
    parent TEXT DEFAULT '',
    created TEXT,
    updated TEXT
);
CREATE INDEX tasks_repo ON tasks (repo, state, created);
CREATE TABLE task_comments (
    task TEXT NOT NULL,
    author TEXT NOT NULL,          -- owner | arete
    body TEXT,
    ts TEXT
);
CREATE INDEX task_comments_task ON task_comments (task);
CREATE TABLE reports (run_id TEXT PRIMARY KEY, saved TEXT NOT NULL, report TEXT NOT NULL);
CREATE TABLE passed_over (
    repo TEXT NOT NULL,
    issue TEXT NOT NULL,
    since TEXT NOT NULL,           -- when the poller first passed this ready ticket over for want of a slot
    PRIMARY KEY (repo, issue)
);
CREATE TABLE incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    class TEXT NOT NULL,
    source TEXT NOT NULL,
    repo TEXT DEFAULT '',
    signature TEXT DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    count INTEGER DEFAULT 1,
    evidence TEXT DEFAULT '',
    decision TEXT DEFAULT '',
    confidence REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    answers TEXT DEFAULT '',
    injection INTEGER DEFAULT 0,
    decided TEXT DEFAULT '',
    tries INTEGER DEFAULT 0,
    retry_at TEXT DEFAULT '',
    decided_count INTEGER DEFAULT 0,
    shapes TEXT DEFAULT '',
    policies TEXT,
    phase TEXT DEFAULT '',
    waiting TEXT DEFAULT '',
    rearmed INTEGER DEFAULT 0,
    landed TEXT DEFAULT '',
    recovery TEXT DEFAULT '',
    alerted INTEGER DEFAULT 0,
    closed TEXT DEFAULT ''
);
CREATE UNIQUE INDEX incidents_open ON incidents (fingerprint) WHERE state = 'open';
CREATE TABLE incident_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident INTEGER NOT NULL,
    ts TEXT NOT NULL,
    note TEXT
);
CREATE INDEX incident_notes_incident ON incident_notes (incident, id);
CREATE TABLE incident_repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident INTEGER NOT NULL,
    repo TEXT NOT NULL,
    task TEXT DEFAULT '',
    started TEXT NOT NULL,
    ended TEXT DEFAULT '',
    outcome TEXT DEFAULT ''
);
CREATE INDEX incident_repairs_incident ON incident_repairs (incident, id);
CREATE TABLE incident_aliases (
    fingerprint TEXT PRIMARY KEY,
    incident INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE asks (
    id TEXT PRIMARY KEY,
    run TEXT NOT NULL,
    repo TEXT NOT NULL,
    ticket TEXT NOT NULL,
    step TEXT NOT NULL,
    call TEXT DEFAULT '',
    why TEXT DEFAULT '',
    tried TEXT DEFAULT '',
    questions TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',    -- open | answered | stopped | expired
    asked TEXT NOT NULL,
    answered TEXT,
    session TEXT DEFAULT '{}',
    answers TEXT DEFAULT '[]',
    taken_by TEXT DEFAULT '',
    reminded TEXT DEFAULT ''
);
CREATE INDEX asks_run ON asks (run);
CREATE TABLE push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    label TEXT DEFAULT '',
    contact TEXT DEFAULT '',
    created TEXT NOT NULL,
    last_success TEXT DEFAULT ''
);
CREATE TABLE record_pins (run_id TEXT PRIMARY KEY, pinned TEXT NOT NULL);
CREATE TABLE run_purges (run_id TEXT PRIMARY KEY, reason TEXT NOT NULL, queued TEXT NOT NULL);
