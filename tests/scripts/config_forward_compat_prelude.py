
import json, os, sys, threading, time, urllib.request
from pathlib import Path
from unittest import mock
from arete import accounts, config, control, gh, paths, replay, serve, settings, state, util
state.migrate()
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
    serve.engine.launch = lambda *a, **k: (_ for _ in ()).throw(control.Refused("fixture"))
    server = serve.Server()
    said = []
    server.say = said.append
    return server, said
write(GOOD)
