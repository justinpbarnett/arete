
import json, os, sys, threading, time, urllib.request
from pathlib import Path
from unittest import mock
from arete.store import runs as run_store
from arete import accounts, config, control, gh, paths, replay, serve, settings, util
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
foundation.migrate()
root = Path(sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError("config compatibility case attempted an external transport or credential lookup")
urllib.request.urlopen = forbidden
accounts.read = forbidden
gh.gh = forbidden
GOOD = "max_parallel=1\n\n" + "\n".join(replay.sample(root / "sample", adw=True)) + "\n\n[jev]\nenabled=false\n"
BAD = GOOD + "\n[routing]\nfuture_key = 1\n"
NEWER = GOOD + """
[steps.future_step]
tries = 1

[kinds.chore]

[ask]
future_option = 1
max_per_run = 5

[retention]
future = 1
days = 10
lines_days = 40

[jev.risk_weights]
future_weight = 0.5
"""
def write(text):
    paths.CONFIG_FILE.write_text(text)
def result(**found):
    print("BEHAVIOR_RESULT=" + json.dumps(found))
def refused(text, **kw):
    write(text)
    try:
        config.load(**kw)
    except ValueError as e:
        return str(e)
    return ""
def quiet_server():
    serve.Server.sweep_vms = lambda self: None
    serve.Server.prune = lambda self: None
    serve.loop_run.launch = lambda *a, **k: (_ for _ in ()).throw(control.Refused("fixture"))
    server = serve.Server()
    said = []
    server.say = said.append
    return server, said
write(GOOD)
