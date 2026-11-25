PHASE = "expand"
SQL = """
CREATE TABLE spec_deliveries (
    identity TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    issue TEXT NOT NULL,
    root TEXT NOT NULL UNIQUE,
    run TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('building', 'published', 'evidenced', 'complete')),
    data TEXT NOT NULL,
    UNIQUE (repo, issue)
) STRICT;
CREATE INDEX spec_deliveries_pending ON spec_deliveries(state);
"""
