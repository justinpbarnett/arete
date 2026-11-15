
import io, json, os, sys, time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from arete import backup, config, control, heartbeat, tool_events, paths, record, replay, serve, state, tasks, util
root = Path(sys.argv[1])
paths.CONFIG_FILE.write_text("\n".join(replay.sample(root / "sample")))
state.ensure_schema()
server = serve.Server()
messages = []
server.say = messages.append
def age(days):
    return util.iso(util.now() - timedelta(days=days))
def backdate(folder, days):
    at = time.time() - days * 86400
    for p in folder.rglob("*"):
        if not p.is_symlink():
            os.utime(p, (at, at))
def made(tag, days, status="failed", kind="pipeline", raw=True, issue=None):
    rid = record.start(kind, tag=tag, repo="replay-sample", issue=issue or tag,
                       **({"loop_kind": "command"} if kind == "loop" else {}))
    if raw:
        with record.Recorder(rid, "build", {"runner": "claude", "model": "m"}, ()) as rec:
            rec.line(json.dumps({"type": "text", "text": tag}))
            rec.end({"ok": True, "error": "", "seconds": 1})
    record.settle(rid, status, end_line=tag)
    folder = paths.RUNS_DIR / rid
    (folder / "run.log").write_text(tag)
    (folder / "run.jsonl").write_text('{}\n')
    with state.db() as c:
        from tests.harness import age_run
        age_run(rid, started=age(days), finished=age(days), connection=c)
        c.execute("INSERT INTO steps (run_id, step, ok) VALUES (?, 'build', 1)", (rid,))
    backdate(folder, days)
    return rid
def folder(rid):
    return paths.RUNS_DIR / rid
def queued(rid):
    with state.db() as c:
        return c.execute("SELECT 1 FROM run_purges WHERE run_id = ?", (rid,)).fetchone() is not None
def proposal(rid, status):
    state.add_proposal(rid, {"layer": "repo", "repo": "replay-sample", "file": "f", "change": "c",
                                  "why": "w", "evidence": [rid], "confidence": 0.9}, "hand")
    pid = state.proposals()[0]["id"]
    state.move_proposal(pid, ("hand",), status)
def result(**fields):
    print("BEHAVIOR_RESULT=" + json.dumps(fields))
