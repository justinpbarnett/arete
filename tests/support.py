import shlex
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from arete import paths, sandbox, state
from tests.harness import SOURCE as SOURCE

PYTHON = shlex.quote(sys.executable)


def isolate_live_state():
    folder = tempfile.TemporaryDirectory(prefix="arete-no-live-")
    unittest.addModuleCleanup(folder.cleanup)
    root = Path(folder.name).resolve()
    for patch in (mock.patch.object(sandbox, "AUTHORITY", root / "authority" / "verification-authority"),
                  mock.patch.object(paths, "STATE_DIR", root / "state"),
                  mock.patch.object(paths, "LOCKS_DIR", root / "state" / "locks"),
                  mock.patch.object(paths, "WORKTREES_DIR", root / "worktrees"),
                  mock.patch.object(paths, "LOG_DIR", root / "logs"),
                  mock.patch.object(paths, "SERVER_FILE", root / "state" / "server.json"),
                  mock.patch.object(paths, "RUNS_DIR", root / "state" / "runs"),
                  mock.patch.object(paths, "DB_FILE", root / "state" / "arete.db"),
                  *(mock.patch.object(sandbox, name, root / "state" / getattr(sandbox, name).name)
                    for name in ("AGENT_DIR", "LOGIN_DIR", "KEY_DIR", "SESSIONS_DIR"))):
        patch.start()
        unittest.addModuleCleanup(patch.stop)
    state.migrate()
