import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace as NS
from unittest.mock import patch

from arete import config, engine, paths, pipeline, replay, scratch, util

root = Path(sys.argv[1])
scratch.AUTHORITY = root / 'authority-parent' / 'verification-authority'
replay.sample(root / 'sample')
src = root / 'sample' / 'repo'
repo = config.Repo('replay-sample', src, layer=root / 'sample' / 'layer')
run = NS(id='probe', cfg=config.Config(), log=lambda *a: None, loop=NS(work_repo=lambda cfg: repo))

def git(*args):
    return subprocess.run(['git', *map(str, args)], cwd=src, text=True, capture_output=True, timeout=10)

def assert_no_loop():
    assert not git('branch', '--list', 'loop/*').stdout.strip()
    assert git('worktree', 'list', '--porcelain').stdout.count('worktree ') == 1
    assert not (paths.WORKTREES_DIR / 'loop-probe').exists()
    assert not list(paths.WORKTREES_DIR.iterdir())
