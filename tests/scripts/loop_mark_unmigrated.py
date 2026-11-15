
old = Path(sys.argv[1]) / "old.db"
c = sqlite3.connect(old)
from arete.migrations.m0001_foundation_ledger import SCHEMA

c.executescript(SCHEMA)
c.execute("INSERT INTO loop_state (loop) VALUES ('poll')")
c.commit()
before = [r[1] for r in c.execute("PRAGMA table_info(loop_state)")]
c.close()
real = paths.DB_FILE
paths.DB_FILE = old
try:
    unmigrated = [state.loop_marks(connect=state.reader), board.checks()["loops"]]
finally:
    paths.DB_FILE = real
c = sqlite3.connect(old)
after = [r[1] for r in c.execute("PRAGMA table_info(loop_state)")]
c.close()
state.set_loop_state("research", data={"k": 1}, last_fire="2026-01-01T00:00:00Z", next_due="2026-01-02T00:00:00Z", metric_on=1)
heartbeat.mark_loop("research", True, "ok")
row = state.loop_state("research")
print("RESULT:" + json.dumps({"unmigrated": unmigrated, "same": before == after, "new": "mark_at" in after,
                              "row": [row["data"], row["last_fire"], row["next_due"], row["metric_on"], row["mark_detail"]]}))
