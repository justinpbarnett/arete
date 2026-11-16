from arete.migrations.m0002_foundation_time_shape import TIME_COLUMNS, TIME_GLOB

PHASE = "expand"


def apply(conn):
    for column in ("active_seconds", "verify_seconds", "verify_open_seconds"):
        conn.execute(f"ALTER TABLE runs ADD COLUMN {column} REAL CHECK "
                     f"({column} IS NULL OR (typeof({column}) IN ('integer', 'real') AND {column} >= 0))")
    conn.execute("ALTER TABLE runs ADD COLUMN repair_calls INTEGER CHECK "
                 "(repair_calls IS NULL OR (typeof(repair_calls) = 'integer' AND repair_calls >= 0))")
    conn.execute("ALTER TABLE runs ADD COLUMN measured_at TEXT")
    columns = (*TIME_COLUMNS["runs"], "measured_at")
    invalid = " OR ".join(f'(NEW."{c}" IS NOT NULL AND NEW."{c}" != \'\' '
                          f'AND NEW."{c}" NOT GLOB \'{TIME_GLOB}\')' for c in columns)
    updates = ", ".join(f'"{c}"' for c in columns)
    for kind, event in (("insert", "INSERT"), ("update", f"UPDATE OF {updates}")):
        conn.execute(f'DROP TRIGGER "runs_time_{kind}"')
        conn.execute(f"""CREATE TRIGGER "runs_time_{kind}" BEFORE {event} ON runs
            WHEN {invalid} BEGIN SELECT RAISE(ABORT, 'runs: timestamp must have UTC iso shape'); END""")
