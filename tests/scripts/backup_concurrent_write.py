
seed()
with foundation.db() as c:
    c.executemany("INSERT INTO feed (run_id, ts, kind, text) VALUES ('bulk', ?, 'note', 'original')", [(ago(0),)] * 20000)
CHILD = """
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], timeout=30)
c.execute("PRAGMA cache_size = 10")
c.execute("UPDATE feed SET text = 'ROLLEDBACK' WHERE run_id = 'bulk'")
print("ready", flush=True)
time.sleep(1)
c.rollback()
"""
writer = subprocess.Popen([sys.executable, "-I", "-c", CHILD, str(paths.DB_FILE)], stdout=subprocess.PIPE, text=True)
assert writer.stdout.readline().strip() == "ready"
snap = backup.snapshot("hand")
writer.wait(timeout=60)
out |= {"writer": writer.returncode, "quick_check": backup.inspect(snap)["quick_check"],
        "rows": column("SELECT count(*) FROM feed WHERE run_id = 'bulk'", snap)[0],
        "leaked": column("SELECT count(*) FROM feed WHERE text = 'ROLLEDBACK'", snap)[0],
        "live_leaked": column("SELECT count(*) FROM feed WHERE text = 'ROLLEDBACK'")[0]}
