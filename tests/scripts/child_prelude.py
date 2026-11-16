
import json, os, shlex, signal, sqlite3, subprocess, sys, time
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete.store import runs as run_store
from arete import board, cli, config, delivery, loop_run, loops, paths, push, sources, util
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
root = Path(sys.argv[1])
foundation.migrate()
def verdicts():
    with foundation.db() as c:
        return [dict(r) for r in c.execute("SELECT kind, outcome, approval FROM verdicts")]
def set_check(command):
    with foundation.db() as c:
        emit = json.loads(c.execute("SELECT emit FROM approvals WHERE id = 1").fetchone()["emit"])
        emit["item"]["check"] = command
        c.execute("UPDATE approvals SET emit = ? WHERE id = 1", (json.dumps(emit),))
def report(**facts):
    print("RESULT " + json.dumps(facts, default=str), flush=True)
def no_wake():
    util.last_wake = lambda: 0.0
def later(seconds):
    real = util.now
    util.now = lambda: real() + timedelta(seconds=seconds)
SEED = r"""
paths.CONFIG_FILE.write_text("[jev]\nenabled=false\n")
item = {"repo": "clone", "workdir": str(root / "clone"), "branch": "fix", "base": "main", "check": "true", "env": {},
        "timeout": 60, "lines": 1, "files": ["fix.txt"], "deleted": [], "title": "fix", "report": "", "about": "fix",
        "story": "", "patch": "", "proof": None}
insights_store.add_verdict("run-1", "push", "push", "fix", {}, "", "waiting")
approvals_store.add_approval("fixer", "run-1", {"type": "push", "item": item, "batch": [], "held": "a person lands it"},
                   "push fix to main")
"""
