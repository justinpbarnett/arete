import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest import mock

from arete import codex, config, guard, paths, pi, record, replay, runner, sessions, util
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
replay.sample(root / "fixture")
paths.WORKTREES_DIR.mkdir(parents=True)
work = paths.WORKTREES_DIR / "sample-1"
util.git(["worktree", "add", "-q", "-b", "sample-1", str(work)], root / "fixture/repo")
paths.CONFIG_FILE.write_text(
    '[replay]\nall = true\ndir = ' + json.dumps(str(root / "fixture/replay"))
    + '\n[jev]\nenabled = false\nguard = false\n')
cfg = config.load()
foundation.migrate()
scratch = paths.RUNS_DIR / "r1/scratch"
scratch.mkdir(parents=True)
denied = [paths.STATE_DIR / "arete.db", paths.CONFIG_FILE, paths.PI_KEYS_FILE,
          paths.CLIENT_DIR / "token", paths.RUNS_DIR / "r2/run.log"]
for folder in guard.LOGIN_FOLDERS:
    if folder.is_relative_to(paths.HOME):
        denied.append(folder / "synthetic.txt")
for path in denied:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("synthetic protected value")
own = [work / "own.txt", scratch / "own.txt"]
for path in own:
    path.write_text("allowed")
boundary = guard.floor("r1", work, [scratch])


def payload(tool, args):
    return {"hook_event_name": "PreToolUse", "cwd": str(work), "tool_name": tool, "tool_input": args}


def invoke(command, tool, args):
    done = subprocess.run(shlex.split(command), input=json.dumps(payload(tool, args)), text=True,
                          capture_output=True, env={**os.environ, "ARETE_RUN_ID": "r1"})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"] if done.stdout.strip() else {}


def result(**fields):
    print("BEHAVIOR_RESULT=" + json.dumps(fields))
