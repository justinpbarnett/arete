PHASE = "expand"

SQL = """
CREATE TABLE zte_deliveries (
    task TEXT PRIMARY KEY REFERENCES tasks(id),
    policy TEXT NOT NULL,
    repo TEXT NOT NULL,
    base TEXT NOT NULL,
    created TEXT NOT NULL,
    deadline TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('queued', 'dispatching', 'running', 'prepared', 'push_intent', 'pushed', 'pr_intent',
        'pr_ready', 'merge_intent', 'merged', 'release_ready', 'deploy_intent', 'rollback_intent',
        'healthy', 'held', 'uncertain', 'rolled_back')),
    version INTEGER NOT NULL DEFAULT 1,
    data TEXT NOT NULL,
    conformed TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX one_open_zte_delivery ON zte_deliveries ((1))
    WHERE phase NOT IN ('healthy', 'held', 'uncertain', 'rolled_back');
CREATE UNIQUE INDEX one_daily_zte_attempt ON zte_deliveries (SUBSTR(created, 1, 10));
"""
