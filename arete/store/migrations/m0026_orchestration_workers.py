PHASE = "expand"

SQL = """
CREATE TABLE orchestration_workers (
    root TEXT NOT NULL,
    id TEXT NOT NULL,
    attempt TEXT NOT NULL,
    generation INTEGER NOT NULL,
    phase TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY(root, id)
) STRICT;
CREATE TABLE orchestration_messages (
    seq INTEGER PRIMARY KEY,
    root TEXT NOT NULL,
    id TEXT NOT NULL,
    worker TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    reply_to TEXT NOT NULL,
    action TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt TEXT NOT NULL,
    history TEXT NOT NULL,
    UNIQUE(root, id)
) STRICT;
CREATE INDEX orchestration_inbox ON orchestration_messages(root, worker, state, seq);
"""
