PHASE = "expand"

SQL = """
ALTER TABLE queue ADD COLUMN last_run TEXT;

DELETE FROM queue
WHERE id IN (
    SELECT q.id
    FROM queue q
    WHERE q.run_id IS NULL
      AND q.dead_at IS NULL
      AND q.payload = '[]'
      AND EXISTS (
          SELECT 1
          FROM queue older
          WHERE older.loop = q.loop
            AND older.trigger IS q.trigger
            AND older.run_id IS NULL
            AND older.dead_at IS NULL
            AND older.payload = '[]'
            AND older.id < q.id
      )
);

CREATE UNIQUE INDEX queue_one_waiting ON queue (loop, trigger)
    WHERE run_id IS NULL AND dead_at IS NULL AND payload = '[]';
"""
