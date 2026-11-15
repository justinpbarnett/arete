import getpass
import json
import re
import secrets
import sqlite3

from arete import paths, util

PHASE = "expand"
TIME_GLOB = '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
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
);
CREATE TABLE IF NOT EXISTS steps (
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
CREATE INDEX IF NOT EXISTS steps_ts ON steps (ts);
CREATE INDEX IF NOT EXISTS steps_run ON steps (run_id);
CREATE TABLE IF NOT EXISTS feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT,
    kind TEXT,
    text TEXT
);
CREATE INDEX IF NOT EXISTS feed_run ON feed (run_id, id);
CREATE TABLE IF NOT EXISTS loop_state (
    loop TEXT PRIMARY KEY,
    data TEXT DEFAULT '{}',
    last_fire TEXT,
    next_due TEXT,
    metric_on INTEGER DEFAULT 0,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS findings (
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
CREATE TABLE IF NOT EXISTS events (
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
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    ts TEXT,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS cursors (
    source TEXT PRIMARY KEY,
    value TEXT,
    polled TEXT,
    ok INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS source_owners (
    source TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT NOT NULL,
    queue TEXT NOT NULL,
    grp TEXT DEFAULT '',
    trigger TEXT,
    payload TEXT DEFAULT '[]',
    enqueued TEXT,
    run_id TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    name TEXT NOT NULL,
    value REAL,
    loop TEXT,
    run_id TEXT,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS metrics_name ON metrics (name, ts);
CREATE TABLE IF NOT EXISTS approvals (
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
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    text TEXT,
    ts TEXT,
    taken TEXT
);
CREATE TABLE IF NOT EXISTS probes (
    name TEXT PRIMARY KEY,
    last_run TEXT,
    last_ok TEXT,
    ok INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS holds (
    scope TEXT PRIMARY KEY,        -- claude | opus | sonnet
    until TEXT NOT NULL,
    why TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    issue INTEGER,
    opened TEXT,
    last_seen TEXT,
    repo TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS proposals (
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
CREATE TABLE IF NOT EXISTS evals (
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
CREATE UNIQUE INDEX IF NOT EXISTS evals_key ON evals (run_id, repo, task);
CREATE TABLE IF NOT EXISTS feedback (
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
    asks REAL,
    PRIMARY KEY (pr_url, kind, gh_id)
);
CREATE TABLE IF NOT EXISTS mined (
    pr_url TEXT PRIMARY KEY,
    repo TEXT,
    issue TEXT,
    run_id TEXT,
    status TEXT,                   -- accepted | rejected | skipped
    why TEXT,
    task TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS triage (
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
CREATE TABLE IF NOT EXISTS triage_approvals (
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
CREATE TABLE IF NOT EXISTS verdicts (
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
CREATE INDEX IF NOT EXISTS verdicts_at ON verdicts (at);
CREATE TABLE IF NOT EXISTS research (
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
CREATE TABLE IF NOT EXISTS chats (
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
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    sender TEXT NOT NULL,          -- you | arete | tool | session
    text TEXT,
    cost REAL DEFAULT 0,
    paid_usd REAL,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages (chat_id, id);
CREATE TABLE IF NOT EXISTS chat_sessions (
    run_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    budget_usd REAL,
    linked TEXT NOT NULL,
    told TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS chat_sessions_chat ON chat_sessions (chat_id);
CREATE TABLE IF NOT EXISTS usage (
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
CREATE INDEX IF NOT EXISTS usage_at ON usage (at);
CREATE TABLE IF NOT EXISTS captures (
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
CREATE UNIQUE INDEX IF NOT EXISTS captures_open ON captures (status) WHERE status = 'draft';
CREATE TABLE IF NOT EXISTS capture_checks (
    capture INTEGER NOT NULL,
    name TEXT NOT NULL,
    result TEXT NOT NULL,          -- running | pass | hold | fail | interrupted
    reason TEXT DEFAULT '',
    detail TEXT DEFAULT '{}',      -- JSON
    fingerprint TEXT DEFAULT '',   -- a hash of the inputs this check read
    checked TEXT,
    PRIMARY KEY (capture, name)
);
CREATE TABLE IF NOT EXISTS capture_evidence (
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
CREATE UNIQUE INDEX IF NOT EXISTS capture_evidence_once ON capture_evidence (capture, sha256);
CREATE TABLE IF NOT EXISTS vms (
    name TEXT PRIMARY KEY,         -- a VM arete asked for and has not yet seen deleted
    run_id TEXT,                   -- NULL for a probe
    device TEXT NOT NULL,          -- JSON of the device that asked, so its host stays reachable if devices.toml changes
    vmid INTEGER,                  -- proxmox only
    created TEXT NOT NULL,
    answered INTEGER DEFAULT 0,    -- 1 once the host answered the create, so it cannot still finish it later
    failing_since TEXT             -- when a delete of it first failed
);
CREATE TABLE IF NOT EXISTS tasks (
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
CREATE INDEX IF NOT EXISTS tasks_repo ON tasks (repo, state, created);
CREATE TABLE IF NOT EXISTS task_comments (
    task TEXT NOT NULL,
    author TEXT NOT NULL,          -- owner | arete
    body TEXT,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS task_comments_task ON task_comments (task);
CREATE TABLE IF NOT EXISTS reports (run_id TEXT PRIMARY KEY, saved TEXT NOT NULL, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS passed_over (
    repo TEXT NOT NULL,
    issue TEXT NOT NULL,
    since TEXT NOT NULL,           -- when the poller first passed this ready ticket over for want of a slot
    PRIMARY KEY (repo, issue)
);
CREATE TABLE IF NOT EXISTS incidents (
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
CREATE UNIQUE INDEX IF NOT EXISTS incidents_open ON incidents (fingerprint) WHERE state = 'open';
CREATE TABLE IF NOT EXISTS incident_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident INTEGER NOT NULL,
    ts TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS incident_notes_incident ON incident_notes (incident, id);
CREATE TABLE IF NOT EXISTS incident_repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident INTEGER NOT NULL,
    repo TEXT NOT NULL,
    task TEXT DEFAULT '',
    started TEXT NOT NULL,
    ended TEXT DEFAULT '',
    outcome TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS incident_repairs_incident ON incident_repairs (incident, id);
CREATE TABLE IF NOT EXISTS incident_aliases (
    fingerprint TEXT PRIMARY KEY,
    incident INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asks (
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
CREATE INDEX IF NOT EXISTS asks_run ON asks (run);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    label TEXT DEFAULT '',
    contact TEXT DEFAULT '',
    created TEXT NOT NULL,
    last_success TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS record_pins (run_id TEXT PRIMARY KEY, pinned TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_purges (run_id TEXT PRIMARY KEY, reason TEXT NOT NULL, queued TEXT NOT NULL);
"""
MIGRATIONS = [("cursors", "last_ok", "ALTER TABLE cursors ADD COLUMN last_ok TEXT"),
("asks", "reminded", "ALTER TABLE asks ADD COLUMN reminded TEXT DEFAULT ''"),
              ("runs", "files_purged", "ALTER TABLE runs ADD COLUMN files_purged TEXT"),
              ("queue", "attempts", "ALTER TABLE queue ADD COLUMN attempts INTEGER DEFAULT 0"),
              ("queue", "dead_at", "ALTER TABLE queue ADD COLUMN dead_at TEXT"),
              ("queue", "dead_reason", "ALTER TABLE queue ADD COLUMN dead_reason TEXT DEFAULT ''"),
("evals", "source", "ALTER TABLE evals ADD COLUMN source TEXT DEFAULT ''"),
              ("evals", "engine_sha", "ALTER TABLE evals ADD COLUMN engine_sha TEXT DEFAULT ''"),
              ("evals", "prompts_sha", "ALTER TABLE evals ADD COLUMN prompts_sha TEXT DEFAULT ''"),
              ("evals", "prompts_from", "ALTER TABLE evals ADD COLUMN prompts_from TEXT DEFAULT ''"),
              ("evals", "context_sha", "ALTER TABLE evals ADD COLUMN context_sha TEXT DEFAULT ''"),
              ("evals", "settings", "ALTER TABLE evals ADD COLUMN settings TEXT DEFAULT ''"),
              ("evals", "base_commit", "ALTER TABLE evals ADD COLUMN base_commit TEXT DEFAULT ''"),
              ("evals", "after_step", "ALTER TABLE evals ADD COLUMN after_step INTEGER DEFAULT 0"),
              ("evals", "last_step", "ALTER TABLE evals ADD COLUMN last_step INTEGER DEFAULT 0"),
              ("loop_state", "mark_ok", "ALTER TABLE loop_state ADD COLUMN mark_ok INTEGER"),
              ("loop_state", "mark_detail", "ALTER TABLE loop_state ADD COLUMN mark_detail TEXT DEFAULT ''"),
              ("loop_state", "mark_at", "ALTER TABLE loop_state ADD COLUMN mark_at TEXT"),
              ("captures", "jev_cost_usd", "ALTER TABLE captures ADD COLUMN jev_cost_usd REAL DEFAULT 0"),
              ("captures", "inspect_cost_usd", "ALTER TABLE captures ADD COLUMN inspect_cost_usd REAL DEFAULT 0"),
              ("capture_evidence", "inspection", "ALTER TABLE capture_evidence ADD COLUMN inspection TEXT DEFAULT ''"),
              ("captures", "written_title", "ALTER TABLE captures ADD COLUMN written_title TEXT DEFAULT ''"),
              ("captures", "written_body", "ALTER TABLE captures ADD COLUMN written_body TEXT DEFAULT ''"),
              ("captures", "written_print", "ALTER TABLE captures ADD COLUMN written_print TEXT DEFAULT ''"),
              ("captures", "written_note", "ALTER TABLE captures ADD COLUMN written_note TEXT DEFAULT ''"),
              ("captures", "delivery", "ALTER TABLE captures ADD COLUMN delivery TEXT DEFAULT ''"),
              ("captures", "issue", "ALTER TABLE captures ADD COLUMN issue INTEGER"),
              ("captures", "issue_url", "ALTER TABLE captures ADD COLUMN issue_url TEXT DEFAULT ''"),
              ("captures", "issue_title", "ALTER TABLE captures ADD COLUMN issue_title TEXT DEFAULT ''"),
              ("captures", "filed", "ALTER TABLE captures ADD COLUMN filed TEXT"),
              ("capture_evidence", "link", "ALTER TABLE capture_evidence ADD COLUMN link TEXT DEFAULT ''"),
              ("chats", "summary","ALTER TABLE chats ADD COLUMN summary TEXT DEFAULT ''"),
              ("chats", "summarized_to", "ALTER TABLE chats ADD COLUMN summarized_to INTEGER DEFAULT 0"),
              ("chat_messages", "paid_usd", "ALTER TABLE chat_messages ADD COLUMN paid_usd REAL"),
              ("runs", "landed", "ALTER TABLE runs ADD COLUMN landed TEXT"),
              ("triage", "code", "ALTER TABLE triage ADD COLUMN code INTEGER"),
              ("triage", "kind", "ALTER TABLE triage ADD COLUMN kind TEXT DEFAULT ''"),
              ("triage", "repos", "ALTER TABLE triage ADD COLUMN repos TEXT DEFAULT '[]'"),
              ("triage", "assumptions", "ALTER TABLE triage ADD COLUMN assumptions TEXT DEFAULT '[]'"),
              ("triage", "checks", "ALTER TABLE triage ADD COLUMN checks TEXT DEFAULT '[]'"),
              ("triage_approvals", "checks", "ALTER TABLE triage_approvals ADD COLUMN checks TEXT DEFAULT '[]'"),
              ("triage", "in_sprint", "ALTER TABLE triage ADD COLUMN in_sprint INTEGER DEFAULT 0"),
              ("triage", "parent", "ALTER TABLE triage ADD COLUMN parent TEXT DEFAULT ''"),
              ("feedback", "asks", "ALTER TABLE feedback ADD COLUMN asks REAL"),
              ("feedback", "recorded", "ALTER TABLE feedback ADD COLUMN recorded TEXT"),
              ("runs", "mined_until", "ALTER TABLE runs ADD COLUMN mined_until TEXT"),
              ("triage_approvals", "tracker", "ALTER TABLE triage_approvals ADD COLUMN tracker TEXT DEFAULT 'jira'"),
              ("triage_approvals", "sent_by", "ALTER TABLE triage_approvals ADD COLUMN sent_by TEXT DEFAULT 'triage'"),
              *[("research", col, f"ALTER TABLE research ADD COLUMN {col} TEXT DEFAULT ''")
                for col in ("ticket", "basis", "answered_by", "filed_path", "filed_url", "moved", "delivered")],
              ("triage", "closed", "ALTER TABLE triage ADD COLUMN closed TEXT DEFAULT ''"),
              *[("approvals", col, f"ALTER TABLE approvals ADD COLUMN {col} TEXT DEFAULT ''")
                for col in ("plain_summary", "plain_summary_tried", "review", "review_tried", "claimed_by",
                            "claimed_until")],
              ("findings", "emitted", "ALTER TABLE findings ADD COLUMN emitted TEXT DEFAULT 'legacy'"),
              ("findings", "fingerprint_text", "ALTER TABLE findings ADD COLUMN fingerprint_text TEXT DEFAULT ''"),
              ("alerts", "repo", "ALTER TABLE alerts ADD COLUMN repo TEXT DEFAULT ''"),
              *[("runs", col, f"ALTER TABLE runs ADD COLUMN {col} {kind}") for col, kind in (
                  ("loop", "TEXT"), ("loop_kind", "TEXT"), ("model", "TEXT"), ("runner", "TEXT"), ("device", "TEXT"),
                  ("tokens_in", "INTEGER DEFAULT 0"), ("tokens_out", "INTEGER DEFAULT 0"), ("log", "TEXT"),
                  ("trigger", "TEXT"), ("queue", "TEXT"), ("grp", "TEXT"), ("pid", "INTEGER"), ("lock", "TEXT"),
                  ("stop_asked", "TEXT"), ("moved", "TEXT"), ("label_pending", "TEXT"), ("wait_minutes", "TEXT"), ("only_step", "TEXT DEFAULT ''"),
                  ("engine_sha", "TEXT"))],
              ("steps", "first_context_tokens", "ALTER TABLE steps ADD COLUMN first_context_tokens INTEGER DEFAULT 0"),
              *[("steps", col, f"ALTER TABLE steps ADD COLUMN {col} {kind}") for col, kind in (
                  ("effort", "TEXT DEFAULT ''"), ("tier", "TEXT DEFAULT ''"), ("try", "INTEGER DEFAULT 0"),
                  ("kind", "TEXT DEFAULT ''"), ("cache_read", "INTEGER DEFAULT 0"), ("cache_write", "INTEGER DEFAULT 0"),
                  ("api_seconds", "REAL DEFAULT 0"), ("auth_mode", "TEXT DEFAULT ''"),
                  ("atom", "TEXT DEFAULT ''"), ("ab", "TEXT DEFAULT ''"), ("ab_side", "TEXT DEFAULT ''"))],
              ("runs", "ab_held", "ALTER TABLE runs ADD COLUMN ab_held TEXT DEFAULT ''"),
              *[("incidents", col, f"ALTER TABLE incidents ADD COLUMN {col} {kind}") for col, kind in (
                  ("decision", "TEXT DEFAULT ''"), ("confidence", "REAL DEFAULT 0"), ("reason", "TEXT DEFAULT ''"),
                  ("answers", "TEXT DEFAULT ''"), ("injection", "INTEGER DEFAULT 0"), ("decided", "TEXT DEFAULT ''"),
                  ("tries", "INTEGER DEFAULT 0"), ("retry_at", "TEXT DEFAULT ''"),
                  ("decided_count", "INTEGER DEFAULT 0"), ("shapes", "TEXT DEFAULT ''"), ("policies", "TEXT"),
                  ("phase", "TEXT DEFAULT ''"), ("waiting", "TEXT DEFAULT ''"),
                  ("rearmed", "INTEGER DEFAULT 0"),
                  ("landed", "TEXT DEFAULT ''"), ("recovery", "TEXT DEFAULT ''"), ("alerted", "INTEGER DEFAULT 0"),
                  ("closed", "TEXT DEFAULT ''"))]]



def identity():
    target = paths.backups_dir() / "instance.json"
    if target.exists():
        try:
            found = json.loads(target.read_text())
            valid = (isinstance(found, dict) and set(found) == {"instance_id", "instance_tag", "instance_name",
                                                              "instance_created"}
                     and all(isinstance(v, str) and v for v in found.values())
                     and re.fullmatch("[0-9a-f]{32}", found["instance_id"])
                     and found["instance_tag"] == found["instance_id"][:8]
                     and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", found["instance_created"]))
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise ValueError(f"{target}: malformed instance identity")
        return found
    instance_id = secrets.token_hex(16)
    found = dict(instance_id=instance_id, instance_tag=instance_id[:8], instance_name=getpass.getuser(),
                 instance_created=util.iso())
    target.parent.mkdir(parents=True, exist_ok=True)
    util.write_json(target, found)
    util.flush_file(target)
    util.flush_folder(target.parent)
    return found


def apply(conn):
    statement = ""
    for line in SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    for table, column, sql in MIGRATIONS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(sql)
    conn.execute("ALTER TABLE runs ADD COLUMN schema_version INTEGER")
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT")
    conn.execute(f"""CREATE TABLE schema_migrations (
        n INTEGER PRIMARY KEY, name TEXT NOT NULL, area TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN ('expand','backfill','contract')),
        sha256 TEXT NOT NULL, applied TEXT NOT NULL CHECK (applied GLOB '{TIME_GLOB}'),
        backup TEXT NOT NULL DEFAULT '', seconds REAL NOT NULL DEFAULT 0) STRICT""")
    conn.executemany("INSERT INTO schema_meta VALUES (?, ?)",
                     [("schema_version", "1"), ("oldest_writer", "0"), *identity().items()])

