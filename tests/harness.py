from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parent.parent
TESTS = SOURCE / "tests"
UNSET = object()


def scratch_env(root, *, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"), **extra):
    root = Path(root)
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env.update(ARETE_CONFIG_DIR=str(root / "config"), ARETE_STATE_DIR=str(root / "state"),
               ARETE_LOG_DIR=str(root / "logs"))
    env.update(extra)
    return env


def state_env(state_dir, **extra):
    return {"ARETE_STATE_DIR": str(state_dir), **extra}


def inherited_env(*, drop=(), **extra):
    return {**{name: value for name, value in os.environ.items() if name not in drop}, **extra}


def make_folders(root, *names):
    for name in names:
        (Path(root) / name).mkdir()


def bootstrap(source):
    return f"import sys;sys.path.insert(0,{str(SOURCE)!r});sys.path.insert(0,{str(TESTS)!r});" + source


def run_script(source, root, *args, env, timeout=120):
    return subprocess.run([sys.executable, "-I", "-c", bootstrap(source), *args],
                          cwd=root, env=env, text=True, capture_output=True, timeout=timeout)


def behavior_result(done, marker="BEHAVIOR_RESULT=", *, last=False):
    lines = done.stdout.splitlines()
    matches = ([line[len(marker):] for line in lines if line.startswith(marker)]
               if marker else done.stdout.strip().splitlines()[-1:])
    if not matches:
        raise AssertionError(f"Missing result marker {marker!r}\nstdout:\n{done.stdout}\nstderr:\n{done.stderr}")
    return json.loads(matches[-1] if last else matches[0])


def set_task_body(task_id, body, *, connection=None):
    from arete.store import db as foundation

    with (foundation.db() if connection is None else nullcontext(connection)) as db:
        db.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))


def age_run(run_id, *, started=UNSET, finished=UNSET, connection=None):
    from arete.store import db as foundation

    fields = {name: value for name, value in (("started", started), ("finished", finished)) if value is not UNSET}
    if not fields:
        return
    with (foundation.db() if connection is None else nullcontext(connection)) as db:
        db.execute("UPDATE runs SET " + ", ".join(name + " = ?" for name in fields) + " WHERE id = ?",
                   (*fields.values(), run_id))

