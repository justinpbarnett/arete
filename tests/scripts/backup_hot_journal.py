
seed()
with state.db() as c:
    c.executemany("INSERT INTO feed (run_id, ts, kind, text) VALUES ('bulk', ?, 'note', 'original')", [(ago(0),)] * 20000)
snap = backup.snapshot("hand")
with state.db() as c:
    c.execute("INSERT INTO runs (id, kind, status, started, pid) VALUES ('live', 'pipeline', 'running', ?, ?)", (ago(0), os.getpid()))
CHILD = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA cache_size = 10")
c.execute("UPDATE feed SET text = 'UNCOMMITTED'")
os._exit(0)
"""
subprocess.run([sys.executable, "-I", "-c", CHILD, str(paths.DB_FILE)], check=True)
journal = Path(str(paths.DB_FILE) + "-journal")
out["hot"] = journal.exists()
with dead():
    out["refused"] = run_cli("backup", "--restore", str(snap))[0]
with state.db() as c:
    c.execute("DELETE FROM runs WHERE id = 'live'")
with dead():
    out["rc"] = run_cli("backup", "--restore", str(snap))[0]
def archived():
    return [p for p in paths.STATE_DIR.glob("arete.db.before-restore-*") if not p.name.endswith("-journal")]
out["uncommitted"] = column("SELECT count(*) FROM feed WHERE text = 'UNCOMMITTED'", archived()[0])[0]
out["quick_check"] = column("PRAGMA quick_check")
out["live_journal"] = journal.exists()
journal.write_bytes(b"\0" * 512)
with dead():
    out["rc2"] = run_cli("backup", "--restore", str(snap))[0]
pairs = [p.name for p in paths.STATE_DIR.glob("arete.db.before-restore-*-journal")]
out["pairs"] = [name.removesuffix("-journal") in [p.name for p in archived()] for name in pairs]
out["live_journal2"] = journal.exists()
