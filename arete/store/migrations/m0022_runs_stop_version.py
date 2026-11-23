PHASE = "expand"

SQL = """
ALTER TABLE runs ADD COLUMN stop_version INTEGER NOT NULL DEFAULT 1;
UPDATE runs SET stop_version = version WHERE version > 1;

CREATE TRIGGER runs_stop_version AFTER UPDATE OF status, pid, stop_asked ON runs
WHEN NEW.status IS NOT OLD.status OR NEW.pid IS NOT OLD.pid OR NEW.stop_asked IS NOT OLD.stop_asked
BEGIN
    UPDATE runs SET stop_version = NEW.version WHERE id = NEW.id;
END;
"""
