from arete.store.migrations.m0002_foundation_time_shape import TIME_GLOB

PHASE = "expand"


def apply(conn):
    for table in ("runs", "tasks", "task_comments"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE events_log ADD COLUMN via TEXT")
    conn.execute("""CREATE TABLE idempotency (
        actor TEXT NOT NULL, key TEXT NOT NULL, route TEXT NOT NULL, request_sha TEXT NOT NULL,
        status INTEGER NOT NULL, response BLOB NOT NULL, first_seq INTEGER, last_seq INTEGER,
        at TEXT NOT NULL CHECK (at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
        via TEXT, PRIMARY KEY (actor, key))""")
    conn.execute("INSERT INTO schema_meta VALUES ('versioned_api_grace_deadline', '')")
    invalid = f'(NEW."at" IS NOT NULL AND NEW."at" != \'\' AND NEW."at" NOT GLOB \'{TIME_GLOB}\')'
    for kind, event in (("insert", "INSERT"), ("update", 'UPDATE OF "at"')):
        conn.execute(f"""CREATE TRIGGER idempotency_time_{kind} BEFORE {event} ON idempotency
            WHEN {invalid}
            BEGIN SELECT RAISE(ABORT, 'idempotency: timestamp must have UTC iso shape'); END""")
    for event in ("INSERT", "UPDATE"):
        conn.execute(f"""CREATE TRIGGER versioned_api_grace_{event.lower()} BEFORE {event} ON schema_meta
            WHEN NEW.key = 'versioned_api_grace_deadline' AND NEW.value != '' AND
            (NEW.value NOT GLOB
             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
             OR strftime('%Y-%m-%dT%H:%M:%SZ', NEW.value) IS NULL)
            BEGIN SELECT RAISE(ABORT, 'invalid versioned API grace deadline'); END""")
