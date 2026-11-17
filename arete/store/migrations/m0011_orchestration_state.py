from arete.store.migrations.m0002_foundation_time_shape import TIME_GLOB

PHASE = "expand"
SQL = """
CREATE TABLE orchestrations (
    root TEXT PRIMARY KEY,
    current_run TEXT NOT NULL,
    generation INTEGER NOT NULL,
    owner INTEGER NOT NULL,
    version INTEGER NOT NULL,
    turns INTEGER NOT NULL,
    launches INTEGER NOT NULL,
    data TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??Z')
);
CREATE UNIQUE INDEX orchestration_run ON orchestrations(current_run);
CREATE TABLE orchestration_turns (
    root TEXT NOT NULL,
    id TEXT NOT NULL,
    run TEXT NOT NULL,
    observation_sha TEXT NOT NULL,
    response TEXT NOT NULL,
    state TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??Z'),
    PRIMARY KEY(root, id)
);
CREATE TABLE orchestration_actions (
    root TEXT NOT NULL,
    id TEXT NOT NULL,
    turn TEXT NOT NULL,
    run TEXT NOT NULL,
    op TEXT NOT NULL,
    state TEXT NOT NULL,
    request TEXT NOT NULL,
    result TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??Z'),
    PRIMARY KEY(root, id)
);
"""
for table in ("orchestrations", "orchestration_turns", "orchestration_actions"):
    invalid = f'(NEW."at" IS NOT NULL AND NEW."at" != \'\' AND NEW."at" NOT GLOB \'{TIME_GLOB}\')'
    for kind, event in (("insert", "INSERT"), ("update", 'UPDATE OF "at"')):
        SQL += f"""CREATE TRIGGER {table}_time_{kind} BEFORE {event} ON {table}
            WHEN {invalid}
            BEGIN SELECT RAISE(ABORT, '{table}: timestamp must have UTC iso shape'); END;
        """
