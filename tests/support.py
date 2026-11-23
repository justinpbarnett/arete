import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from arete import dbstats, paths, sandbox
from arete.store import db as foundation


@contextmanager
def workspace_state(root):
    state = root / "state"
    config = root / "config"
    with mock.patch.multiple(paths, STATE_DIR=state, DB_FILE=state / "arete.db",
                             CONFIG_DIR=config, CONFIG_FILE=config / "config.toml",
                             LOCKS_DIR=state / "locks", RUNS_DIR=state / "runs", LOG_DIR=root / "logs",
                             WORKTREES_DIR=root / "fixtures", SERVER_FILE=state / "server.json"):
        yield


def isolate_live_state():
    folder = tempfile.TemporaryDirectory(prefix="arete-no-live-")
    unittest.addModuleCleanup(folder.cleanup)
    root = Path(folder.name).resolve()
    for patch in (mock.patch.object(dbstats.Timing, "finished"),
                  mock.patch.object(sandbox, "AUTHORITY", root / "authority" / "verification-authority"),
                  mock.patch.object(paths, "CONFIG_DIR", root / "config"),
                  mock.patch.object(paths, "CONFIG_FILE", root / "config/config.toml"),
                  mock.patch.object(paths, "PI_KEYS_FILE", root / "config/pi.env"),
                  mock.patch.object(paths, "STATE_DIR", root / "state"),
                  mock.patch.object(paths, "TOKEN_CLIENT_DIR", root / "state/client"),
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
    foundation.migrate()
