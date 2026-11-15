import shlex
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from arete import paths, pi, scratch
from tests.harness import SOURCE as SOURCE

PYTHON = shlex.quote(sys.executable)


def isolate_live_state():
    folder = tempfile.TemporaryDirectory(prefix="arete-no-live-")
    unittest.addModuleCleanup(folder.cleanup)
    root = Path(folder.name).resolve()
    for patch in (mock.patch.object(scratch, "AUTHORITY", root / "authority" / "verification-authority"),
                  mock.patch.object(paths, "SERVER_FILE", root / "state" / "server.json"),
                  mock.patch.object(paths, "RUNS_DIR", root / "state" / "runs"),
                  mock.patch.object(paths, "DB_FILE", root / "state" / "arete.db"),
                  *(mock.patch.object(pi, name, root / "state" / getattr(pi, name).name)
                    for name in ("AGENT_DIR", "LOGIN_DIR", "KEY_DIR", "SESSIONS_DIR"))):
        patch.start()
        unittest.addModuleCleanup(patch.stop)
