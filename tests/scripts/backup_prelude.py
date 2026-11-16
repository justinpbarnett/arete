
import contextlib, dataclasses, fcntl, io, json, os, sqlite3, stat, subprocess, sys, tarfile, time
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete.store import runs as run_store
from arete.store import loops as loop_store, sources as source_store
from arete import store, backup, cli, config, control, heartbeat, install, paths, serve, settings, util
from arete.store import _rows as _rows_store
from arete.store import approvals as approvals_store
from arete.store import asks as asks_store
from arete.store import chats as chats_store
from arete.store import db as foundation
from arete.store import devices as devices_store
from arete.store import incidents as incidents_store
from arete.store import insights as insights_store
from arete.store import loops as loops_store
from arete.store import push as push_store
from arete.store import runs as runs_store
from arete.store import sources as sources_store
from arete.store import tasks as tasks_store
from arete.store import triage as triage_store
from arete.lab import optimize
root = Path(sys.argv[1])
util.last_wake = lambda: 0.0
out = {}
def ago(days):
    return util.iso(util.now() - timedelta(days=days))
def counts(p=None):
    c = sqlite3.connect(p or paths.DB_FILE)
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
        return {n: c.execute(f"SELECT count(*) FROM {n}").fetchone()[0] for n in names}
    finally:
        c.close()
def column(sql, p=None):
    c = sqlite3.connect(p or paths.DB_FILE)
    try:
        return [r[0] for r in c.execute(sql).fetchall()]
    finally:
        c.close()
def incident(fp, days, closed=None):
    with foundation.db() as c:
        i = c.execute("INSERT INTO incidents (fingerprint, class, source, state, first_seen, last_seen, closed) "
                      "VALUES (?, 'k', 's', ?, ?, ?, ?)", (fp, "dismissed" if closed else "open", ago(days + 1), ago(days),
                                                           ago(closed) if closed else "")).lastrowid
        c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?, ?, ?)", (i, ago(days), "note " + fp))
        c.execute("INSERT INTO incident_aliases (fingerprint, incident, ts) VALUES (?, ?, ?)", (fp + "-alias", i, ago(days)))
        c.execute("INSERT INTO incident_repairs (incident, repo, started) VALUES (?, 'r', ?)", (i, ago(days)))
    return i
def seed():
    foundation.migrate()
    with foundation.db() as c:
        for rid, kind, loop_kind, days, cost in (
                ("pipe-old", "pipeline", "", 30, 0), ("pipe-new", "pipeline", "", 1, 0),
                ("loop-old", "loop", "agent", 30, 0), ("loop-new", "loop", "agent", 1, 0),
                ("paid-old", "loop", "command", 30, 0.5), ("free-old", "loop", "command", 30, 0),
                ("free-new", "loop", "command", 1, 0)):
            c.execute("INSERT INTO runs (id, kind, loop_kind, status, started, cost_usd) VALUES (?, ?, ?, 'done', ?, ?)",
                      (rid, kind, loop_kind, ago(days), cost))
            c.execute("INSERT INTO feed (run_id, ts, kind, text) VALUES (?, ?, 'note', ?)", (rid, ago(days), rid))
        for name, days, run in (("q-old", 30, "x"), ("q-new", 1, "x"), ("q-null", 30, None)):
            c.execute("INSERT INTO queue (loop, queue, enqueued, run_id) VALUES (?, 'q', ?, ?)", (name, ago(days), run))
        for name, days in (("e-old", 60), ("e-new", 1)):
            c.execute("INSERT INTO events (loop, type, ts) VALUES (?, ?, ?)", (name, name, ago(days)))
    incident("open-old", 60)
    new = incident("open-new", 1)
    incident("closed-old", 60, 40)
    incident("closed-recent", 60, 5)
    with foundation.db() as c:
        c.execute("INSERT INTO incident_notes (incident, ts, note) VALUES (?, ?, 'seen again: x')", (new, ago(30)))
def server():
    s = serve.Server()
    s.said = []
    s.say = s.said.append
    return s
def settle():
    while backup.COPYING:
        time.sleep(0.02)
def prune(s=None):
    s = s or server()
    s.prune()
    settle()
    return s
def tree():
    return {p.name: p.read_bytes() for p in paths.STATE_DIR.iterdir() if p.is_file()}
def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = cli.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()
def dead():
    return mock.patch.object(control, "serve_beat", return_value={"alive": False})
real_sh = util.sh
def rsync_only(fake):
    def sh(cmd, *a, **k):
        return fake(cmd) if cmd[0] == "rsync" else real_sh(cmd, *a, **k)
    util.sh = sh
