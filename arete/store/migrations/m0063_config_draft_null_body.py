PHASE = "expand"

SQL = """
DROP VIEW IF EXISTS proposals;

ALTER TABLE config_drafts RENAME TO config_drafts_old;

CREATE TABLE config_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    base_version TEXT NOT NULL DEFAULT '',
    body TEXT,
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

INSERT INTO config_drafts (
    id, kind, name, origin, path, base_version, body, diff, author, model,
    instruction, why, gate, check_report, validator, state, decline_reason,
    evidence, activated_commit, legacy_proposal, pr_url, version, created, updated
)
SELECT
    id, kind, name, origin, path, base_version, body, diff, author, model,
    instruction, why, gate, check_report, validator, state, decline_reason,
    evidence, activated_commit, legacy_proposal, pr_url, version, created, updated
FROM config_drafts_old;

DROP TABLE config_drafts_old;

CREATE INDEX config_drafts_state ON config_drafts (state, kind, name);

CREATE VIEW proposals AS
SELECT
    id AS id,
    COALESCE(json_extract(evidence, '$.run'), '') AS run_id,
    name AS layer,
    CASE WHEN origin LIKE 'repo:%' THEN substr(origin, 6) ELSE origin END AS repo,
    path AS file,
    COALESCE(body, '') AS change,
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
