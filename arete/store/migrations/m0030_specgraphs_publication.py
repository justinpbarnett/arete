PHASE = "expand"

SQL = """
CREATE TABLE spec_graphs (
    scope TEXT NOT NULL,
    parent TEXT NOT NULL,
    repo TEXT NOT NULL,
    data TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
    PRIMARY KEY (scope, parent),
    UNIQUE (repo, parent)
);
CREATE TABLE spec_graph_children (
    scope TEXT NOT NULL,
    parent TEXT NOT NULL,
    node TEXT NOT NULL,
    ticket TEXT NOT NULL,
    native_id TEXT NOT NULL,
    PRIMARY KEY (scope, parent, node),
    UNIQUE (scope, ticket),
    UNIQUE (scope, native_id),
    FOREIGN KEY (scope, parent) REFERENCES spec_graphs (scope, parent)
);
CREATE TABLE spec_graph_writes (
    scope TEXT NOT NULL,
    parent TEXT NOT NULL,
    operation TEXT NOT NULL,
    dispatched INTEGER NOT NULL DEFAULT 0 CHECK (dispatched IN (0, 1)),
    received INTEGER NOT NULL DEFAULT 0 CHECK (received IN (0, 1)),
    PRIMARY KEY (scope, parent, operation),
    FOREIGN KEY (scope, parent) REFERENCES spec_graphs (scope, parent)
);
"""
