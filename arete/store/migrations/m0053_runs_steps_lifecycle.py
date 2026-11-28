import re

PHASE = "backfill"

_CHAT_STEP_RE = re.compile(r"^chat (\d+)(?: (.+))?$")


def apply(conn):
    conn.execute("ALTER TABLE steps ADD COLUMN node TEXT")
    conn.execute("ALTER TABLE steps ADD COLUMN status TEXT")
    conn.execute("ALTER TABLE steps ADD COLUMN queued_at TEXT")
    conn.execute("ALTER TABLE steps ADD COLUMN waiting_since TEXT")
    conn.execute("ALTER TABLE steps ADD COLUMN device TEXT")
    conn.execute("ALTER TABLE steps ADD COLUMN account_id TEXT")
    conn.execute("ALTER TABLE steps ADD COLUMN chat_id INTEGER")
    conn.execute("ALTER TABLE steps ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX steps_run_node ON steps (run_id, node, try)")

    rows = conn.execute("SELECT id, run_id, step, ok, ts FROM steps").fetchall()
    updates = []
    for row in rows:
        step_name = row["step"] or ""
        node = step_name
        chat_id = None
        if m := _CHAT_STEP_RE.fullmatch(step_name):
            chat_id = int(m.group(1))
            node = f"chat {m.group(2)}" if m.group(2) else "chat"
        status = "done" if row["ok"] == 1 else "fault"
        queued_at = row["ts"]
        updates.append((node, status, queued_at, chat_id, row["id"]))
    if updates:
        conn.executemany(
            "UPDATE steps SET node = ?, status = ?, queued_at = ?, chat_id = ? WHERE id = ?",
            updates,
        )

    conn.execute("ALTER TABLE chats ADD COLUMN payer TEXT")
    owner_row = conn.execute(
        "SELECT id FROM principals WHERE kind = 'human' AND status = 'active' AND role = 'owner' "
        "ORDER BY created, oid LIMIT 1"
    ).fetchone()
    owner_id = owner_row[0] if owner_row else None
    if owner_id is not None:
        conn.execute("UPDATE chats SET payer = ? WHERE payer IS NULL", (owner_id,))

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
