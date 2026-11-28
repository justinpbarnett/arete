from __future__ import annotations

PHASE = "contract"

_VIEW_AND_TRIGGERS = """
CREATE VIEW approvals AS
SELECT
    o.legacy_approval AS id,
    o.loop AS loop,
    o.run_id AS run_id,
    COALESCE(
        (SELECT r.body FROM outbox_revisions r WHERE r.item = o.id AND r.rev = o.rev),
        o.summary
    ) AS emit,
    o.summary AS summary,
    CASE o.state
        WHEN 'draft' THEN 'pending'
        WHEN 'edited' THEN 'pending'
        WHEN 'held' THEN 'pending'
        WHEN 'approved' THEN 'approved'
        WHEN 'sending' THEN 'approved'
        WHEN 'sent' THEN 'done'
        WHEN 'rejected' THEN 'rejected'
        WHEN 'discarded' THEN 'rejected'
        WHEN 'failed' THEN 'failed'
        ELSE 'pending'
    END AS status,
    CASE o.state
        WHEN 'sent' THEN COALESCE(o.sent_url, o.sent_ref, '')
        WHEN 'failed' THEN COALESCE(o.error, '')
        WHEN 'rejected' THEN COALESCE(o.reject_reason, '')
        ELSE ''
    END AS result,
    o.created AS created,
    CASE
        WHEN o.state = 'sent' THEN COALESCE(o.sent_at, o.updated)
        WHEN o.state IN ('approved', 'sending', 'rejected', 'discarded', 'failed') THEN o.updated
        ELSE NULL
    END AS decided,
    o.plain_summary AS plain_summary,
    '' AS plain_summary_tried,
    o.review AS review,
    '' AS review_tried,
    '' AS claimed_by,
    '' AS claimed_until
FROM outbox o
WHERE o.legacy_approval IS NOT NULL;

CREATE TRIGGER approvals_instead_insert
INSTEAD OF INSERT ON approvals
BEGIN
    INSERT INTO outbox (
        marker, target, kind, object, audience, run_id, loop, drafted_by, slot, rev, state,
        sent_at, sent_ref, sent_url, error, attempts, reject_reason,
        summary, plain_summary, review, shape, legacy_approval, version, created, updated
    )
    VALUES (
        NULL,
        CASE
            WHEN json_valid(NEW.emit) THEN
                CASE COALESCE(json_extract(NEW.emit, '$.type'), 'push')
                    WHEN 'ticket' THEN 'github'
                    WHEN 'pr' THEN 'github'
                    WHEN 'loop' THEN 'loop'
                    ELSE 'git'
                END
            ELSE 'git'
        END,
        CASE
            WHEN json_valid(NEW.emit) THEN
                CASE COALESCE(json_extract(NEW.emit, '$.type'), 'push')
                    WHEN 'ticket' THEN 'issue'
                    ELSE COALESCE(json_extract(NEW.emit, '$.type'), 'push')
                END
            ELSE 'push'
        END,
        COALESCE(
            CASE
                WHEN json_valid(NEW.emit) AND COALESCE(json_extract(NEW.emit, '$.type'), '') = 'push'
                    AND COALESCE(json_extract(NEW.emit, '$.item.repo'), '') != ''
                    THEN 'git:' || json_extract(NEW.emit, '$.item.repo')
                WHEN json_valid(NEW.emit) AND COALESCE(json_extract(NEW.emit, '$.type'), '') IN ('pr', 'ticket')
                    AND COALESCE(json_extract(NEW.emit, '$.item.repo'), '') != ''
                    THEN 'github:' || json_extract(NEW.emit, '$.item.repo')
                WHEN json_valid(NEW.emit) AND COALESCE(json_extract(NEW.emit, '$.type'), '') = 'loop'
                    AND COALESCE(json_extract(NEW.emit, '$.item.loop'), '') != ''
                    THEN 'loop:' || json_extract(NEW.emit, '$.item.loop')
                ELSE NULL
            END,
            (SELECT CASE WHEN COALESCE(repo, '') != '' THEN 'github:' || repo ELSE '' END FROM runs WHERE id = NEW.run_id),
            ''
        ),
        'self',
        NEW.run_id,
        NEW.loop,
        'run:' || COALESCE(NEW.run_id, '') || '/emit',
        'approval:' || COALESCE(NEW.id, (SELECT COALESCE(MAX(legacy_approval), 0) + 1 FROM outbox)),
        1,
        CASE COALESCE(NEW.status, 'pending')
            WHEN 'pending' THEN 'draft'
            WHEN 'approved' THEN 'sending'
            WHEN 'done' THEN 'sent'
            WHEN 'rejected' THEN 'rejected'
            WHEN 'failed' THEN 'failed'
            ELSE 'draft'
        END,
        CASE WHEN NEW.status = 'done' THEN COALESCE(NULLIF(NEW.decided, ''), NULLIF(NEW.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) ELSE NULL END,
        CASE WHEN NEW.status = 'done' THEN CASE WHEN COALESCE(NEW.result, '') != '' THEN NEW.result ELSE 'approval:' || COALESCE(NEW.id, (SELECT COALESCE(MAX(legacy_approval), 0) + 1 FROM outbox)) END ELSE NULL END,
        CASE WHEN NEW.status = 'done' THEN NULLIF(NEW.result, '') ELSE NULL END,
        CASE WHEN NEW.status = 'failed' THEN NULLIF(NEW.result, '') ELSE NULL END,
        CASE WHEN NEW.status = 'approved' THEN 1 ELSE 0 END,
        CASE WHEN NEW.status = 'rejected' THEN NULLIF(NEW.result, '') ELSE NULL END,
        COALESCE(NEW.summary, ''),
        COALESCE(NEW.plain_summary, ''),
        COALESCE(NEW.review, ''),
        1,
        COALESCE(NEW.id, (SELECT COALESCE(MAX(legacy_approval), 0) + 1 FROM outbox)),
        1,
        COALESCE(NULLIF(NEW.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        COALESCE(NULLIF(NEW.decided, ''), NULLIF(NEW.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );

    UPDATE outbox
    SET marker = 'arete:' || COALESCE((SELECT value FROM schema_meta WHERE key = 'instance_tag'), 'local') || ':ob-' || id
    WHERE id = (SELECT MAX(id) FROM outbox)
      AND marker IS NULL;

    INSERT INTO outbox_revisions (item, rev, base_rev, by, body, at)
    SELECT o.id, 1, NULL, o.drafted_by, COALESCE(NULLIF(NEW.emit, ''), o.summary), o.created
    FROM outbox o
    WHERE o.id = (SELECT MAX(id) FROM outbox);

    INSERT INTO delivery_attempts (item, n, started)
    SELECT o.id, 1, COALESCE(NULLIF(NEW.decided, ''), o.created)
    FROM outbox o
    WHERE o.id = (SELECT MAX(id) FROM outbox)
      AND NEW.status = 'approved';
END;

CREATE TRIGGER approvals_instead_update
INSTEAD OF UPDATE ON approvals
BEGIN
    UPDATE delivery_attempts
    SET ended = COALESCE(NULLIF(NEW.decided, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        outcome = CASE NEW.status
            WHEN 'done' THEN 'sent'
            WHEN 'failed' THEN 'unknown'
            WHEN 'rejected' THEN 'refused'
            ELSE outcome
        END,
        external_id = CASE
            WHEN NEW.status = 'done' THEN CASE WHEN COALESCE(NEW.result, '') != '' THEN NEW.result ELSE 'approval:' || OLD.id END
            ELSE external_id
        END,
        external_url = CASE
            WHEN NEW.status = 'done' THEN NULLIF(NEW.result, '')
            ELSE external_url
        END,
        error = CASE
            WHEN NEW.status IN ('failed', 'rejected') THEN NULLIF(NEW.result, '')
            ELSE error
        END
    WHERE item = (SELECT id FROM outbox WHERE legacy_approval = OLD.id)
      AND ended IS NULL
      AND NEW.status IN ('done', 'failed', 'rejected');

    INSERT INTO delivery_attempts (item, n, started)
    SELECT o.id,
           COALESCE((SELECT MAX(n) FROM delivery_attempts WHERE item = o.id), 0) + 1,
           COALESCE(NULLIF(NEW.decided, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    FROM outbox o
    WHERE o.legacy_approval = OLD.id
      AND NEW.status = 'approved'
      AND COALESCE(OLD.status, '') != 'approved';

    UPDATE outbox_revisions
    SET body = NEW.emit
    WHERE item = (SELECT id FROM outbox WHERE legacy_approval = OLD.id)
      AND rev = (SELECT rev FROM outbox WHERE legacy_approval = OLD.id)
      AND COALESCE(NEW.emit, '') != COALESCE(OLD.emit, '');

    UPDATE outbox
    SET state = CASE NEW.status
            WHEN 'pending' THEN 'draft'
            WHEN 'approved' THEN 'sending'
            WHEN 'done' THEN 'sent'
            WHEN 'rejected' THEN 'rejected'
            WHEN 'failed' THEN 'failed'
            ELSE state
        END,
        attempts = CASE
            WHEN NEW.status = 'approved' AND COALESCE(OLD.status, '') != 'approved' THEN attempts + 1
            ELSE attempts
        END,
        sent_at = CASE
            WHEN NEW.status = 'done' THEN COALESCE(NULLIF(NEW.decided, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ELSE sent_at
        END,
        sent_ref = CASE
            WHEN NEW.status = 'done' THEN CASE WHEN COALESCE(NEW.result, '') != '' THEN NEW.result ELSE 'approval:' || OLD.id END
            ELSE sent_ref
        END,
        sent_url = CASE
            WHEN NEW.status = 'done' THEN NULLIF(NEW.result, '')
            ELSE sent_url
        END,
        error = CASE
            WHEN NEW.status = 'failed' THEN NULLIF(NEW.result, '')
            WHEN NEW.status IN ('pending', 'approved', 'done', 'rejected') THEN NULL
            ELSE error
        END,
        reject_reason = CASE
            WHEN NEW.status = 'rejected' THEN NULLIF(NEW.result, '')
            ELSE reject_reason
        END,
        summary = COALESCE(NEW.summary, summary),
        plain_summary = COALESCE(NEW.plain_summary, plain_summary),
        review = COALESCE(NEW.review, review),
        version = version + 1,
        updated = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE legacy_approval = OLD.id;
END;
"""


def apply(conn) -> None:
    from arete.store import db

    conn.execute("DROP TRIGGER IF EXISTS approvals_copy")
    conn.execute("DROP TRIGGER IF EXISTS approvals_mirror")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'approvals'").fetchone():
        conn.execute("DROP TABLE approvals")
    conn.execute("DROP VIEW IF EXISTS approvals")
    db.execute_statements(conn, _VIEW_AND_TRIGGERS)
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < 52 "
        "THEN '52' ELSE value END WHERE key = 'oldest_writer'"
    )
