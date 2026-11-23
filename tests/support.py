import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arete import config, dbstats, paths, sandbox
from arete.store import db as foundation


def isolate_live_state():
    folder = tempfile.TemporaryDirectory(prefix="arete-no-live-", dir=Path("/tmp").resolve(), ignore_cleanup_errors=True)
    unittest.addModuleCleanup(folder.cleanup)
    root = Path(folder.name).resolve()
    store = config.SecretsConfig("pve_store", root / "store")
    for patch in (mock.patch.object(dbstats.Timing, "finished"),
                  mock.patch.object(config, "secret_settings", return_value=store),
                  mock.patch.object(sandbox, "AUTHORITY", root / "authority" / "verification-authority"),
                  mock.patch.object(paths, "CONFIG_DIR", root / "config"),
                  mock.patch.object(paths, "CONFIG_FILE", root / "config/config.toml"),
                  mock.patch.object(paths, "PI_KEYS_FILE", root / "config/pi.env"),
                  mock.patch.object(paths, "STATE_DIR", root / "state"),
                  mock.patch.object(paths, "FINGERPRINT_KEY_FILE", root / "state" / "fingerprint-fixture"),
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
