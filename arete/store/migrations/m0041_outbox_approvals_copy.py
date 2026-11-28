PHASE = "expand"

SQL = """
INSERT INTO outbox (
    marker, target, kind, object, audience, run_id, loop, drafted_by, slot, rev, state,
    sent_at, sent_ref, sent_url, error, attempts, reject_reason,
    summary, plain_summary, review, shape, legacy_approval, version, created, updated
)
SELECT
    NULL,
    'legacy',
    CASE
        WHEN json_valid(a.emit) THEN
            CASE COALESCE(json_extract(a.emit, '$.type'), 'push')
                WHEN 'ticket' THEN 'issue'
                ELSE COALESCE(json_extract(a.emit, '$.type'), 'push')
            END
        ELSE 'push'
    END,
    CASE WHEN COALESCE(r.repo, '') != '' THEN 'github:' || r.repo ELSE '' END,
    'self',
    a.run_id,
    a.loop,
    'run:' || COALESCE(a.run_id, '') || '/emit',
    'approval:' || a.id,
    1,
    CASE a.status
        WHEN 'pending' THEN 'draft'
        WHEN 'approved' THEN 'sending'
        WHEN 'done' THEN 'sent'
        WHEN 'rejected' THEN 'rejected'
        WHEN 'failed' THEN 'failed'
        ELSE 'draft'
    END,
    CASE WHEN a.status = 'done' THEN COALESCE(NULLIF(a.decided, ''), NULLIF(a.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) ELSE NULL END,
    CASE WHEN a.status = 'done' THEN CASE WHEN COALESCE(a.result, '') != '' THEN a.result ELSE 'approval:' || a.id END ELSE NULL END,
    CASE WHEN a.status = 'done' THEN NULLIF(a.result, '') ELSE NULL END,
    CASE WHEN a.status = 'failed' THEN NULLIF(a.result, '') ELSE NULL END,
    CASE WHEN a.status = 'approved' THEN 1 ELSE 0 END,
    CASE WHEN a.status = 'rejected' THEN NULLIF(a.result, '') ELSE NULL END,
    COALESCE(a.summary, ''),
    COALESCE(a.plain_summary, ''),
    COALESCE(a.review, ''),
    1,
    a.id,
    1,
    COALESCE(NULLIF(a.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    COALESCE(NULLIF(a.decided, ''), NULLIF(a.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
FROM approvals a
LEFT JOIN runs r ON r.id = a.run_id
ORDER BY a.id;

INSERT INTO outbox_revisions (item, rev, base_rev, by, body, at)
SELECT o.id, 1, NULL, o.drafted_by, o.summary, o.created
FROM outbox o
WHERE o.legacy_approval IS NOT NULL;

INSERT INTO delivery_attempts (item, n, started)
SELECT o.id, 1, COALESCE(NULLIF(a.decided, ''), o.created)
FROM outbox o
JOIN approvals a ON a.id = o.legacy_approval
WHERE a.status = 'approved';

CREATE TRIGGER approvals_copy
AFTER INSERT ON approvals
BEGIN
    INSERT INTO outbox (
        marker, target, kind, object, audience, run_id, loop, drafted_by, slot, rev, state,
        sent_at, sent_ref, sent_url, error, attempts, reject_reason,
        summary, plain_summary, review, shape, legacy_approval, version, created, updated
    )
    VALUES (
        NULL,
        'legacy',
        CASE
            WHEN json_valid(NEW.emit) THEN
                CASE COALESCE(json_extract(NEW.emit, '$.type'), 'push')
                    WHEN 'ticket' THEN 'issue'
                    ELSE COALESCE(json_extract(NEW.emit, '$.type'), 'push')
                END
            ELSE 'push'
        END,
        COALESCE(
            (SELECT CASE WHEN COALESCE(repo, '') != '' THEN 'github:' || repo ELSE '' END FROM runs WHERE id = NEW.run_id),
            ''
        ),
        'self',
        NEW.run_id,
        NEW.loop,
        'run:' || COALESCE(NEW.run_id, '') || '/emit',
        'approval:' || NEW.id,
        1,
        CASE NEW.status
            WHEN 'pending' THEN 'draft'
            WHEN 'approved' THEN 'sending'
            WHEN 'done' THEN 'sent'
            WHEN 'rejected' THEN 'rejected'
            WHEN 'failed' THEN 'failed'
            ELSE 'draft'
        END,
        CASE WHEN NEW.status = 'done' THEN COALESCE(NULLIF(NEW.decided, ''), NULLIF(NEW.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) ELSE NULL END,
        CASE WHEN NEW.status = 'done' THEN CASE WHEN COALESCE(NEW.result, '') != '' THEN NEW.result ELSE 'approval:' || NEW.id END ELSE NULL END,
        CASE WHEN NEW.status = 'done' THEN NULLIF(NEW.result, '') ELSE NULL END,
        CASE WHEN NEW.status = 'failed' THEN NULLIF(NEW.result, '') ELSE NULL END,
        CASE WHEN NEW.status = 'approved' THEN 1 ELSE 0 END,
        CASE WHEN NEW.status = 'rejected' THEN NULLIF(NEW.result, '') ELSE NULL END,
        COALESCE(NEW.summary, ''),
        COALESCE(NEW.plain_summary, ''),
        COALESCE(NEW.review, ''),
        1,
        NEW.id,
        1,
        COALESCE(NULLIF(NEW.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        COALESCE(NULLIF(NEW.decided, ''), NULLIF(NEW.created, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );

    INSERT INTO outbox_revisions (item, rev, base_rev, by, body, at)
    SELECT o.id, 1, NULL, o.drafted_by, o.summary, o.created
    FROM outbox o
    WHERE o.legacy_approval = NEW.id;

    INSERT INTO delivery_attempts (item, n, started)
    SELECT o.id, 1, COALESCE(NULLIF(NEW.decided, ''), o.created)
    FROM outbox o
    WHERE o.legacy_approval = NEW.id
      AND NEW.status = 'approved';
END;

CREATE TRIGGER approvals_mirror
AFTER UPDATE OF status, plain_summary, review ON approvals
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
            WHEN NEW.status = 'done' THEN CASE WHEN COALESCE(NEW.result, '') != '' THEN NEW.result ELSE 'approval:' || NEW.id END
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
    WHERE item = (SELECT id FROM outbox WHERE legacy_approval = NEW.id)
      AND ended IS NULL
      AND NEW.status IN ('done', 'failed', 'rejected');

    INSERT INTO delivery_attempts (item, n, started)
    SELECT o.id,
           COALESCE((SELECT MAX(n) FROM delivery_attempts WHERE item = o.id), 0) + 1,
           COALESCE(NULLIF(NEW.decided, ''), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    FROM outbox o
    WHERE o.legacy_approval = NEW.id
      AND NEW.status = 'approved'
      AND COALESCE(OLD.status, '') != 'approved';

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
            WHEN NEW.status = 'done' THEN CASE WHEN COALESCE(NEW.result, '') != '' THEN NEW.result ELSE 'approval:' || NEW.id END
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
        plain_summary = COALESCE(NEW.plain_summary, ''),
        review = COALESCE(NEW.review, ''),
        version = version + 1,
        updated = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE legacy_approval = NEW.id;
END;
"""
