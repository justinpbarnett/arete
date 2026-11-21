PHASE = "expand"

SQL = """
ALTER TABLE runs ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0;
ALTER TABLE environment_requeues ADD COLUMN edit_pending TEXT;
ALTER TABLE environment_requeues ADD COLUMN requeue_notified INTEGER NOT NULL DEFAULT 0;
UPDATE environment_requeues SET requeue_notified = 1 WHERE requeued = 1;
CREATE INDEX environment_requeues_unfinished ON environment_requeues (repo)
    WHERE edit_pending IS NOT NULL OR (requeued = 1 AND requeue_notified = 0);
"""
