
import json, os
from unittest import mock
from arete import codebase, control, engine, record, scaffold, sessions, state
state.migrate()
codebase.STAMP = "a"
loop = record.start("loop", pid=None, loop="x")
session = record.start("session", pid=None)
draft = record.start("draft", pid=None)
codebase.STAMP = "b"
with mock.patch.object(engine.loops, "find"), mock.patch.object(engine, "LoopRun"), \
        mock.patch.object(engine.util, "read_json", return_value=[]), mock.patch.object(engine, "carry", return_value="ok"):
    engine.carry_opened(loop)
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
print(json.dumps([[state.run_get(r)["engine_sha"], state.run_get(r)["pid"] == os.getpid()] for r in (loop, session, draft)]))
