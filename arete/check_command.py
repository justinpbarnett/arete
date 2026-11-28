from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

from . import check_process, config, paths, slots, util

FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
PARALLEL = "parallel"


class ScriptRun:
    def __init__(self, log_file: str, where: str):
        self.log_file, self.where = Path(log_file), where
        self.config = SimpleNamespace(max_parallel_checks=config.Config.max_parallel_checks)

    def log(self, message: str) -> None:
        line = f"{util.iso()} [{self.where}] {message}"
        print(line, file=sys.stderr, flush=True)
        with suppress(OSError), self.log_file.open("a") as file:
            file.write(line + "\n")

    @contextmanager
    def waiting(self, cause: str):
        yield


@contextmanager
def forwarding(child: subprocess.Popen):
    def forward(signum, _frame):
        child.send_signal(signum)

    previous = {signum: signal.signal(signum, forward) for signum in FORWARDED}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def launcher(folder: Path, where: str, parallel: bool) -> str:
    entrypoint = (f"import sys; sys.path.insert(0, {str(paths.PACKAGE_DIR.parent)!r}); "
                  "from arete.check_command import main; raise SystemExit(main(sys.argv[1:]))")
    return shlex.join([sys.executable, "-I", "-c", entrypoint, str(paths.CHECK_LOCKS_DIR), str(paths.CONFIG_FILE),
                       str(folder / "run.log"), where, PARALLEL if parallel else "serial"])


def main(argv: list[str]) -> int:
    lock_dir, config_file, log_file, where, mode, command = argv
    paths.CHECK_LOCKS_DIR, paths.CONFIG_FILE = Path(lock_dir), Path(config_file)
    with slots.check_slot(ScriptRun(log_file, where), parallel=mode == PARALLEL) as lock_fds:
        child = subprocess.Popen(["/bin/sh", "-c", command], env=os.environ, pass_fds=lock_fds)
        with forwarding(child):
            code = child.wait()
        check_process.release(lock_fds)
    if code < 0:
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    return code
