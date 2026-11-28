import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from arete import config, dbstats, gh, paths, sandbox, util
from arete.store import db as foundation

WORKSPACE_GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                          "-c", "commit.gpgsign=false"]


def init_workspace_repo(src):
    src.mkdir(parents=True)
    util.git(["init", "-q", "-b", "main"], src)
    (src / ".gitignore").write_text(".venv/\n")
    (src / "notes.txt").write_text("original\n")
    util.git(["add", "."], src)
    util.git([*WORKSPACE_GIT_IDENTITY, "commit", "-qm", "sample"], src)


def write_workspace_output(ws, content):
    output = ws / ".venv/output"
    output.parent.mkdir()
    output.write_text(content)
    return output


@contextmanager
def workspace_state(root):
    state = root / "state"
    config = root / "config"
    with mock.patch.multiple(paths, STATE_DIR=state, DB_FILE=state / "arete.db",
                             CONFIG_DIR=config, CONFIG_FILE=config / "config.toml",
                             LOCKS_DIR=state / "locks", RUNS_DIR=state / "runs", LOG_DIR=root / "logs",
                             WORKTREES_DIR=root / "fixtures", SERVER_FILE=state / "server.json"):
        yield


def parentless_github_issue(repo, number, issue_view):
    path = f"repos/{repo.casefold()}/issues/{number}"

    def api(args):
        assert args[:1] == ["api"] and args[2:] == ["--hostname", "github.com"], args
        if args[1] == f"{path}/parent":
            raise gh.GhError("HTTP 404")
        assert args[1] == path, args
        issue = issue_view(repo, int(number))
        return {**issue, "id": int(number), "state": issue["state"].lower(),
                "repository_url": f"{gh.GITHUB_API}/repos/{repo}"}

    return api


def seed_db(state_dir, *, db_file=None):
    template_env = os.environ.get("ARETE_TEST_DB_TEMPLATE")
    if template_env:
        template = Path(template_env)
        src_db = template / "state" / "arete.db"
        src_instance = template / "state" / "backups" / "instance.json"
        if src_db.is_file() and src_instance.is_file():
            state = Path(state_dir)
            target_db = Path(db_file) if db_file is not None else state / "arete.db"
            for directory in (state / "backups", state / "locks", state / "runs", target_db.parent,
                              paths.RUNS_DIR, paths.WORKTREES_DIR, paths.LOCKS_DIR, paths.LOG_DIR):
                directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_db, target_db)
            shutil.copyfile(src_instance, target_db.parent / "backups" / "instance.json")
            return
    foundation.migrate()


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
    seed_db(root / "state")
