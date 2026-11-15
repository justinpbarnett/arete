
seed()
s = server()
pre = counts()
with mock.patch.object(state, "FIRINGS", "(1 = 1)"):
    prune(s)
snap = max(paths.backups_dir().glob("arete-*.db"))
out["deleted"] = "pipe-old" not in column("SELECT id FROM runs")
with state.db() as c:
    for n in range(5):
        c.execute("INSERT INTO feed (run_id, ts, kind, text) VALUES ('later', ?, 'note', 'later')", (ago(0),))
seq = dict(sqlite3.connect(paths.DB_FILE).execute("SELECT name, seq FROM sqlite_sequence").fetchall())
def untouched(rc, patch):
    held = tree()
    with patch:
        got = run_cli("backup", "--restore", str(snap))
    return [got[0], tree() == held, "stop" in got[1]]
out["alive"] = untouched(0, mock.patch.object(control, "serve_beat", return_value={"alive": True}))
with state.db() as c:
    c.execute("INSERT INTO runs (id, kind, status, started, pid) VALUES ('live', 'pipeline', 'running', ?, ?)", (ago(0), os.getpid()))
out["running"] = untouched(0, dead())
with state.db() as c:
    c.execute("DELETE FROM runs WHERE id = 'live'")
with dead():
    out["missing"] = run_cli("backup", "--restore", str(root / "nope.db"))[0]
    (root / "bad.db").write_bytes(b"not a database" * 100)
    out["corrupt"] = run_cli("backup", "--restore", str(root / "bad.db"))[0]
    out["rc"] = run_cli("backup", "--restore", str(snap))[0]
out["restored"] = counts() == pre
after = dict(sqlite3.connect(paths.DB_FILE).execute("SELECT name, seq FROM sqlite_sequence").fetchall())
out["seq_kept"] = all(after.get(name, 0) >= value for name, value in seq.items())
out["kept_file"] = len(list(paths.STATE_DIR.glob("arete.db.before-restore-*")))
snap_seq = dict(sqlite3.connect(snap).execute("SELECT name, seq FROM sqlite_sequence").fetchall())
paths.DB_FILE.write_bytes(b"garbage" * 500)
with dead():
    out["rc_corrupt_current"] = run_cli("backup", "--restore", str(snap))[0]
after = dict(sqlite3.connect(paths.DB_FILE).execute("SELECT name, seq FROM sqlite_sequence").fetchall())
out["gap"] = all(after[name] == value + 10000 for name, value in snap_seq.items())
