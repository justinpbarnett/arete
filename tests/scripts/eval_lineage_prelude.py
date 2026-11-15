
import contextlib
import io
import shutil
import sys
from pathlib import Path

from arete import evals, optimize, paths, scratch, state

root = Path(sys.argv[1])
probe = [sys.executable, "-V"]
if scratch.protected_command(probe) != probe:
    scratch.AUTHORITY = root / "authority-parent/verification-authority"
paths.ensure_dirs()
def sample():
    state.migrate()
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
    with state.db() as c:
        return c.execute("SELECT run_id FROM evals WHERE repo = 'replay-sample' ORDER BY rowid DESC").fetchone()[0]
