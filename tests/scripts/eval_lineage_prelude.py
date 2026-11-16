
import contextlib, io, json, os, shutil, sqlite3, sys
from pathlib import Path
from arete.store import runs as run_store
from arete import board, codebase, config, evals, paths, sandbox, scratch, util
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
from arete.lab import experiment, optimize
root = Path(sys.argv[1])
probe = [sys.executable, "-V"]
if sandbox.protected_command(probe) != probe:
    sandbox.AUTHORITY = root / "authority-parent/verification-authority"
paths.ensure_dirs()
def sample():
    foundation.migrate()
    text = "\n".join(optimize.sample(root / "sample"))
    paths.CONFIG_FILE.write_text(text.replace("[models]", '[models]\nbuild = "scratch-a"'))
    replies = root / "sample/replay"
    for n in (2, 3):
        for name in ("plan.json", "build.json", "build.patch"):
            p = Path(name)
            shutil.copyfile(replies / name, replies / f"{p.stem}{n}{p.suffix}")
    return replies
def call(*args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = evals.run("replay-sample", *args, **kwargs)
    return code, out.getvalue()
def latest():
    with foundation.db() as c:
        return c.execute("SELECT run_id FROM evals WHERE repo = 'replay-sample' ORDER BY rowid DESC").fetchone()[0]
