
import contextlib, io, json, sys
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete.store import runs as run_store
from arete import config, jev, miner, paths, record, replay, util
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
root, source = Path(sys.argv[1]), sys.argv[2]
repos = "".join(f'\n[[repo]]\nname = "{n}"\npath = {json.dumps(source)}\ngithub = "example/{n}"\njev = {j}\n'
                for n, j in (("arete", "false"), ("open", "true")))
sample = [line for line in replay.sample(root / "sample") if not line.startswith("#")]
paths.CONFIG_FILE.write_text('arete_repo = "arete"\nmax_parallel = 1\n' + "\n".join(sample) + repos +
                             "\n[jev]\nenabled = " + sys.argv[3] + "\n")
def made(tag, status="done", minutes=2, repo="replay-sample", kind="pipeline", parked="", issue=None, settled=True):
    rid = record.start(kind, tag=tag, repo=repo, issue=tag if issue is None else issue)
    if settled:
        record.settle(rid, status, summary=tag + " summary")
    with foundation.db() as c:
        from tests.harness import age_run
        age_run(rid, started=util.iso(util.now() - timedelta(minutes=minutes)), connection=c)
    if parked:
        util.write_json(paths.RUNS_DIR / rid / "parked.json", {"reason": parked})
    return rid
def call(rid, step, ok=True, runner="claude", events=(), error=""):
    rec = record.Recorder(rid, step, {"runner": runner, "model": "m1"}, ())
    for event in events:
        rec.line(json.dumps(event))
    rec.end({"ok": ok, "error": error, "seconds": 3.0})
    rec.close()
def claude_events(text="boom"):
    return [{"type": "assistant", "message": {"id": "a1", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": text}]}},
            {"type": "assistant", "message": {"id": "a2", "content": [{"type": "text", "text": "gave up"}]}}]
def pi_events(text="boom"):
    return [{"type": "message_end", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "ls"}}]}},
            {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "t1", "toolName": "bash",
                                                "isError": True, "content": [{"type": "text", "text": text}]}},
            {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "gave up"}]}}]
def since(minutes=60):
    return util.iso(util.now() - timedelta(minutes=minutes))
def result(**fields):
    print("BEHAVIOR_RESULT=" + json.dumps(fields))
