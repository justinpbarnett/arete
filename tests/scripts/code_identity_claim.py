
import json, os
from unittest import mock
from arete.store import runs as run_store
from arete import codebase, control, loop_run, record, scaffold, sessions
from arete.store import db as foundation
foundation.migrate()
codebase.STAMP = "a"
loop = record.start("loop", pid=None, loop="x")
session = record.start("session", pid=None)
draft = record.start("draft", pid=None)
codebase.STAMP = "b"
with mock.patch.object(loop_run.loops, "find"), mock.patch.object(loop_run, "LoopRun"), \
        mock.patch.object(loop_run.util, "read_json", return_value=[]), mock.patch.object(loop_run, "carry", return_value="ok"):
    loop_run.carry_opened(loop)
with mock.patch.object(sessions, "load_spec", return_value=mock.Mock(device="", id=session)), \
        mock.patch.object(sessions.devices, "lease", side_effect=RuntimeError("stop")):
    try:
        sessions.execute(session)
    except Exception:
        pass
with mock.patch.object(scaffold, "write_draft", side_effect=control.Refused("stop")):
    try:
        scaffold.draft(mock.Mock(), run_id=draft)
    except control.Refused:
        pass
print(json.dumps([[run_store.run_get(r)["engine_sha"], run_store.run_get(r)["pid"] == os.getpid()] for r in (loop, session, draft)]))
