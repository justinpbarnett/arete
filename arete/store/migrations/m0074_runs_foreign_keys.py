from __future__ import annotations

from arete.store.migrations.m0002_foundation_time_shape import TIME_GLOB

PHASE = "backfill"
FOREIGN_KEYS_OFF = True


def _time_triggers(conn, table: str, columns: tuple[str, ...]) -> None:
    check = " OR ".join(
        f'(NEW."{col}" IS NOT NULL AND NEW."{col}" != \'\' AND NEW."{col}" NOT GLOB \'{TIME_GLOB}\')'
        for col in columns
    )
    cols = ", ".join(f'"{col}"' for col in columns)
    conn.execute(
        f'CREATE TRIGGER "{table}_time_insert" BEFORE INSERT ON "{table}" '
        f"WHEN {check} BEGIN SELECT RAISE(ABORT, '{table}: timestamp must have UTC iso shape'); END"
    )
    conn.execute(
        f'CREATE TRIGGER "{table}_time_update" BEFORE UPDATE OF {cols} ON "{table}" '
        f"WHEN {check} BEGIN SELECT RAISE(ABORT, '{table}: timestamp must have UTC iso shape'); END"
    )


def apply(conn) -> None:
    # 1. Rebuild steps with run_id REFERENCES runs(id) and chat_id REFERENCES chats(id).
    conn.execute("""
        CREATE TABLE steps_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
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
            ab_side TEXT DEFAULT '',
            settings_sha TEXT,
            prompt_version TEXT,
            prompt_level TEXT,
            schema_version TEXT,
            node TEXT,
            status TEXT,
            queued_at TEXT,
            waiting_since TEXT,
            device TEXT,
            account_id TEXT,
            chat_id INTEGER REFERENCES chats(id),
            version INTEGER NOT NULL DEFAULT 1,
            runner_version TEXT,
            unread INTEGER NOT NULL DEFAULT 0,
            cost_nusd INTEGER,
            paid_nusd INTEGER,
            price_version TEXT,
            billing_context TEXT
        )
    """)
    step_cols = (
        "id, run_id, step, runner, model, provider, auth_mode, ok, cost_usd, tokens_in, tokens_out, "
        "turns, seconds, error, ts, first_context_tokens, effort, tier, try, kind, cache_read, "
        "cache_write, api_seconds, atom, ab, ab_side, settings_sha, prompt_version, prompt_level, "
        "schema_version, node, status, queued_at, waiting_since, device, account_id, chat_id, "
        "version, runner_version, unread, cost_nusd, paid_nusd, price_version, billing_context"
    )
    conn.execute(f"INSERT INTO steps_new ({step_cols}) SELECT {step_cols} FROM steps")  # noqa: S608
    conn.execute("DROP TABLE steps")
    conn.execute("ALTER TABLE steps_new RENAME TO steps")
    conn.execute("CREATE INDEX steps_ts ON steps (ts)")
    conn.execute("CREATE INDEX steps_run ON steps (run_id)")
    conn.execute("CREATE INDEX steps_run_node ON steps (run_id, node, try)")
    conn.execute("CREATE INDEX steps_prompt ON steps (prompt_version, ts)")
    conn.execute("""
        CREATE TRIGGER steps_payer_floor BEFORE INSERT ON steps
        WHEN NEW.account_id IS NOT NULL AND EXISTS (
          SELECT 1 FROM accounts a WHERE a.id = NEW.account_id AND a.usage = 'personal'
            AND a.holder IS NOT COALESCE((SELECT payer FROM runs WHERE id = NEW.run_id),
                                         (SELECT payer FROM chats WHERE id = NEW.chat_id)))
        BEGIN SELECT RAISE(ABORT, 'personal account used for another payer'); END
    """)
    conn.execute("""
        CREATE TRIGGER steps_payer_floor_update BEFORE UPDATE OF account_id ON steps
        WHEN NEW.account_id IS NOT NULL AND EXISTS (
          SELECT 1 FROM accounts a WHERE a.id = NEW.account_id AND a.usage = 'personal'
            AND a.holder IS NOT COALESCE((SELECT payer FROM runs WHERE id = NEW.run_id),
                                         (SELECT payer FROM chats WHERE id = NEW.chat_id)))
        BEGIN SELECT RAISE(ABORT, 'personal account used for another payer'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS steps_cost_nusd_mirror_ins AFTER INSERT ON steps
        WHEN NEW.cost_nusd IS NULL AND NEW.cost_usd IS NOT NULL
        BEGIN
          UPDATE steps SET cost_nusd = CAST(round(NEW.cost_usd * 1e9) AS INTEGER)
          WHERE id = NEW.id;
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS steps_cost_nusd_mirror_upd AFTER UPDATE OF cost_usd ON steps
        WHEN NEW.cost_nusd IS OLD.cost_nusd
        AND (NEW.cost_usd IS NOT OLD.cost_usd
        OR (NEW.cost_nusd IS NULL AND NEW.cost_usd IS NOT NULL))
        BEGIN
          UPDATE steps SET cost_nusd = CASE WHEN NEW.cost_usd IS NULL THEN NULL
          ELSE CAST(round(NEW.cost_usd * 1e9) AS INTEGER) END
          WHERE id = NEW.id;
        END
    """)
    _time_triggers(conn, "steps", ("ts",))

    # 2. Rebuild feed with run_id REFERENCES runs(id).
    conn.execute("""
        CREATE TABLE feed_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            ts TEXT,
            kind TEXT,
            text TEXT
        )
    """)
    conn.execute("INSERT INTO feed_new (id, run_id, ts, kind, text) SELECT id, run_id, ts, kind, text FROM feed")
    conn.execute("DROP TABLE feed")
    conn.execute("ALTER TABLE feed_new RENAME TO feed")
    conn.execute("CREATE INDEX feed_run ON feed (run_id, id)")
    _time_triggers(conn, "feed", ("ts",))

    # 3. Rebuild verdicts with nullable run_id REFERENCES runs(id).
    conn.execute("""
        CREATE TABLE verdicts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT,
            run_id TEXT REFERENCES runs(id),
            loop TEXT,
            kind TEXT,
            subject TEXT,
            about TEXT,
            answers TEXT,
            held TEXT,
            outcome TEXT DEFAULT '',
            outcome_at TEXT,
            sha TEXT DEFAULT '',
            approval INTEGER,
            UNIQUE (run_id, kind, subject)
        )
    """)
    conn.execute(
        "INSERT INTO verdicts_new (id, at, run_id, loop, kind, subject, about, answers, held, "
        "outcome, outcome_at, sha, approval) "
        "SELECT id, at, NULLIF(run_id, ''), loop, kind, subject, about, answers, held, "
        "outcome, outcome_at, sha, approval FROM verdicts"
    )
    conn.execute("DROP TABLE verdicts")
    conn.execute("ALTER TABLE verdicts_new RENAME TO verdicts")
    conn.execute("CREATE INDEX verdicts_at ON verdicts (at)")
    _time_triggers(conn, "verdicts", ("at", "outcome_at"))

    # 4. Rebuild evals with nullable run_id REFERENCES runs(id).
    conn.execute("""
        CREATE TABLE evals_new (
            run_id TEXT REFERENCES runs(id),
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
            last_step INTEGER DEFAULT 0,
            cost_nusd INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO evals_new (run_id, repo, task, passed, seconds, cost_usd, ts, source, "
        "engine_sha, prompts_sha, prompts_from, context_sha, settings, base_commit, after_step, last_step, cost_nusd) "
        "SELECT NULLIF(run_id, ''), repo, task, passed, seconds, cost_usd, ts, source, "
        "engine_sha, prompts_sha, prompts_from, context_sha, settings, base_commit, after_step, last_step, cost_nusd "
        "FROM evals"
    )
    conn.execute("DROP TABLE evals")
    conn.execute("ALTER TABLE evals_new RENAME TO evals")
    conn.execute("CREATE UNIQUE INDEX evals_key ON evals (run_id, repo, task)")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS evals_cost_nusd_mirror_ins AFTER INSERT ON evals
        WHEN NEW.cost_nusd IS NULL AND NEW.cost_usd IS NOT NULL
        BEGIN
          UPDATE evals SET cost_nusd = CAST(round(NEW.cost_usd * 1e9) AS INTEGER)
          WHERE rowid = NEW.rowid;
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS evals_cost_nusd_mirror_upd AFTER UPDATE OF cost_usd ON evals
        WHEN NEW.cost_nusd IS OLD.cost_nusd
        AND (NEW.cost_usd IS NOT OLD.cost_usd
        OR (NEW.cost_nusd IS NULL AND NEW.cost_usd IS NOT NULL))
        BEGIN
          UPDATE evals SET cost_nusd = CASE WHEN NEW.cost_usd IS NULL THEN NULL
          ELSE CAST(round(NEW.cost_usd * 1e9) AS INTEGER) END
          WHERE rowid = NEW.rowid;
        END
    """)
    _time_triggers(conn, "evals", ("ts",))

    # 5. Rebuild zte_deliveries to drop its pre-m0059 TEXT REFERENCES tasks(id) (tasks.id became INTEGER in m0059).
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'zte_deliveries'").fetchone():
        conn.execute("""
            CREATE TABLE zte_deliveries_new (
                task TEXT PRIMARY KEY,
                policy TEXT NOT NULL,
                repo TEXT NOT NULL,
                base TEXT NOT NULL,
                created TEXT NOT NULL,
                deadline TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('queued', 'dispatching', 'running', 'prepared', 'push_intent',
                    'pushed', 'pr_intent', 'pr_ready', 'merge_intent', 'merged', 'release_ready', 'deploy_intent',
                    'rollback_intent', 'healthy', 'held', 'uncertain', 'rolled_back')),
                version INTEGER NOT NULL DEFAULT 1,
                data TEXT NOT NULL,
                conformed TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO zte_deliveries_new (task, policy, repo, base, created, deadline, phase, version, data, conformed) "
            "SELECT task, policy, repo, base, created, deadline, phase, version, data, conformed FROM zte_deliveries"
        )
        conn.execute("DROP TABLE zte_deliveries")
        conn.execute("ALTER TABLE zte_deliveries_new RENAME TO zte_deliveries")
        conn.execute(
            "CREATE UNIQUE INDEX one_open_zte_delivery ON zte_deliveries ((1)) "
            "WHERE phase NOT IN ('healthy', 'held', 'uncertain', 'rolled_back')"
        )
        conn.execute(
            "CREATE UNIQUE INDEX one_daily_zte_attempt ON zte_deliveries (SUBSTR(created, 1, 10)) "
            "WHERE json_type(data, '$.same_day_recovery') IS NOT 'true'"
        )
        conn.execute(
            "CREATE UNIQUE INDEX one_daily_zte_recovery ON zte_deliveries (SUBSTR(created, 1, 10)) "
            "WHERE json_type(data, '$.same_day_recovery') IS 'true'"
        )

    # 6. Require an empty PRAGMA foreign_key_check before committing.
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        sample = ", ".join(f"{r[0]}#{r[1]}->{r[2]}" for r in violations[:5])
        raise RuntimeError(f"foreign_key_check failed with {len(violations)} violation(s): {sample}")
