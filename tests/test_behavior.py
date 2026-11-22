import argparse
import ast
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, chdir, closing, contextmanager, redirect_stderr, redirect_stdout
from contextvars import Context
from datetime import timedelta
from http.client import HTTPConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from socket import SHUT_WR, create_connection, socket, socketpair
from types import SimpleNamespace
from unittest import mock

from arete import (
    accounts, api_check, api_http, chat, cli, client, config, contract, control, dashboard, event_stream, events, gh,
    heartbeat, idempotency, jira, paths, pipeline, principals, record, sandbox, screening, serve, tailnet, tasks,
    tool_events, tracker, triage, util, verify_faults, workspace,
)
from arete.store import approvals as approvals_store
from arete.store import chats as chats_store
from arete.store import db
from arete.store import idempotency as claims
from arete.store import runs as run_store
from arete.store import tasks as task_store
from arete.store import triage as triage_store
from arete.store import workspaces as workspace_store
from tests import support, versioned_api_fixture
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT


def setUpModule():
    support.isolate_live_state()


class WorkspaceOwnerTests(unittest.TestCase):
    def test_owner_rows_match_legacy_records_and_ownerless_fallback(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(suffix=".noindex"))).resolve()
        env = api_check.isolated_env(root)
        (root / "config/config.toml").write_text("max_parallel = 1\n")
        entry = """
import json
from pathlib import Path
from arete import config, paths, pipeline, record, workspace
from arete.store import db, workspaces

db.migrate()
repo = config.Repo("sample-app", Path.cwd() / "repo", parts=[config.Part("api", "test/sample")])
short = config.Repo("sample", Path.cwd() / "other")
cfg = config.Config(repos=[short, repo])
ws = paths.WORKTREES_DIR / "sample-app-7"
ws.mkdir()
run = pipeline.Run("sample-run", repo, paths.RUNS_DIR / "sample-run", tracker=None, tid=7)
c = workspace.Checkout("api", "test/sample", repo.path / "api", ws / "api", "main", onto="saved")
legacy = {"run": "sample-run", "repo": "sample-app", "ticket": "7", "created": "2026-01-01T00:00:00Z",
          "checkouts": [{"dir": "api", "github": "test/sample", "src": str(c.src), "wt": str(c.wt)}]}
(ws / "sample.txt").write_text("sample\\n")
made = {"sample.txt": "old-fingerprint", "\\0head api/": "old-head", "\\0salt": "old-salt", "\\0owner": legacy}
file = workspace.made_record(ws)
file.parent.mkdir()
file.write_text(json.dumps(made))
owner = workspace.WorkspaceOwner.from_row(workspace.made_before(ws)["\\0owner"])
assert owner.checkouts == [workspace.Checkout(c.dir, c.github, c.src, c.wt, "")]
assert workspace.owner_of(ws, cfg) == owner
made_owner = pipeline.workspace_owner(run, ws, [c])
assert json.dumps(made_owner.to_row()) == json.dumps(legacy)
run.alone = True
assert pipeline.workspace_owner(run, ws, [c]) == owner
del made["\\0owner"]
file.write_text(json.dumps(made))
record.start("pipeline", repo="sample", issue="app-7", status="merged")
record.start("pipeline", repo="sample-app", issue="7", status="merged")
fallback = workspace.owner_of(ws, cfg)
assert fallback == workspace.WorkspaceOwner("", "sample-app", "7", "", owner.checkouts)
before = file.read_bytes()
workspace.note_made(ws, [c], made_owner)
row = workspaces.open_for(ws.name)
assert row["state"] == "live" and row["checkouts"] == legacy["checkouts"]
assert workspace.made_before(ws)["\\0owner"] == legacy
assert file.read_bytes() == before
"""
        command = api_check.child_command(Path(__file__).resolve().parents[1], entry)
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                                timeout=CHILD_HANG_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class _WorkspaceStoreCase(unittest.TestCase):
    def run_child(self, entry):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(suffix=".noindex"))).resolve()
        env = api_check.isolated_env(root)
        (root / "config/config.toml").write_text("max_parallel = 1\n")
        command = api_check.child_command(Path(__file__).resolve().parents[1], entry)
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                                timeout=CHILD_HANG_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WorkspaceBackfillTests(_WorkspaceStoreCase):
    def test_imports_legacy_records_and_file_bearing_orphan_without_removal(self):
        self.run_child(r"""
import json
from pathlib import Path
from arete import config, paths, record, util, workspace
from arete.store import db, workspaces

db.migrate()
repo = config.Repo("sample-app", Path.cwd() / "repo", parts=[config.Part("api", "test/sample")])
cfg = config.Config(repos=[config.Repo("sample", Path.cwd() / "short"), repo])
src = repo.path / "api"
src.mkdir(parents=True)
util.git(["init", "-q", "-b", "main"], src)
util.git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
          "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "sample"], src)

def checkout(ticket):
    folder = paths.WORKTREES_DIR / f"{repo.name}-{ticket}"
    c = workspace.Checkout("api", "test/sample", src, folder / "api", "main")
    workspace.add_worktree(c, f"fixture-{ticket}", "main")
    (c.wt / "notes.txt").write_text(f"ticket {ticket}\n")
    return folder, c

merged, a = checkout("7")
waiting, b = checkout("8")
orphan, _ = checkout("9")
started = "2026-01-01T00:00:00Z"
finished = "2026-01-01T00:01:00Z"
run_id = record.start("pipeline", repo=repo.name, issue="7", started=started, finished=finished, status="merged")
record.start("pipeline", repo=repo.name, issue="7", started=finished, status="running")
record.start("pipeline", repo="sample", issue="app-8", status="done")
record.start("pipeline", repo=repo.name, issue="8", status="needs-info")
owner = workspace.WorkspaceOwner(run_id, repo.name, "7", started, [a]).to_row()
records = {}
for folder, c, salt in ((merged, a, "merged-salt"), (waiting, b, "waiting-salt")):
    made = {"api/notes.txt": workspace.fingerprint(c.wt / "notes.txt", salt),
            "\0salt": salt, "\0head api/": util.git(["rev-parse", "HEAD"], c.wt).strip()}
    if folder == merged:
        made.update({"\0owner": owner, "\0onto api/": "saved-base",
                     "\0retained": {"roots": ["scratch-root"], "names": ["api/notes.txt"]}})
    source = workspace.made_record(folder)
    source.parent.mkdir(exist_ok=True)
    source.write_text(json.dumps(made))
    records[folder] = (source, source.read_bytes(), made)
(a.wt / "notes.txt").write_text("edited after the fingerprint was recorded\n")
folders = (merged, waiting, orphan)
registrations = util.git(["worktree", "list", "--porcelain"], src)
assert all(f"worktree {folder / 'api'}" in registrations.splitlines() for folder in folders)

assert workspaces.backfill(cfg) == 3
rows = [workspaces.open_for(folder.name) for folder in folders]
released, kept, held = rows
assert [row["state"] for row in rows] == ["released", "kept", "held"], rows
assert released["owner_run"] == run_id and released["released_at"] == finished
assert released["made_at"] == started and released["checkouts"] == owner["checkouts"]
assert released["onto"] == {"api/": "saved-base"}
assert released["retained"] == records[merged][2]["\0retained"]
assert (kept["repo"], kept["ticket"], kept["owner_run"], kept["kept_reason"]) == ("sample-app", "8", "", "needs-info")
assert kept["checkouts"] == workspace.WorkspaceOwner("", repo.name, "8", "", [b]).to_row()["checkouts"]
assert held["owner_run"] == "" and held["checkouts"] == [] and workspaces.files(held["id"]) == {}
for row, folder in zip(rows, folders, strict=True):
    assert row["bytes"] == workspace.tree_bytes(folder) > 0
    if folder in records:
        made = records[folder][2]
        assert row["salt"] == made["\0salt"]
        assert row["heads"] == {"api/": made["\0head api/"]}
        assert workspaces.files(row["id"]) == {"api/notes.txt": made["api/notes.txt"]}

assert workspaces.backfill(cfg) == 0
assert [workspaces.open_for(folder.name) for folder in folders] == rows
workspaces.update(released["id"], base_version=released["version"], state="removed")
assert workspaces.backfill(cfg) == 0
assert workspaces.open_for(merged.name) is None
with db.reader() as c:
    assert c.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM events_log WHERE kind = 'workspace.changed'").fetchone()[0] == 4
assert all((folder / "api/notes.txt").is_file() for folder in folders)
assert all(source.read_bytes() == saved for source, saved, _ in records.values())
assert util.git(["worktree", "list", "--porcelain"], src) == registrations
""")


class WorkspaceStoreTests(_WorkspaceStoreCase):
    def test_workspace_versions_files_and_events_are_atomic(self):
        self.run_child("""
import json
import sqlite3
import unittest
from unittest.mock import patch
from arete.store import db, events, workspaces, writes

case = unittest.TestCase()
db.migrate()
with db.reader() as c:
    tables = {r["name"]: r["strict"] for r in c.execute("PRAGMA table_list")}
    assert tables["workspaces"] == tables["workspace_files"] == 1
    assert {r["name"] for r in c.execute("PRAGMA index_list(workspaces)") if r["unique"] and r["partial"]} == {
        "workspaces_one_open"
    }
    assert [r["name"] for r in c.execute("PRAGMA index_info(workspaces_by_state)")] == ["state", "released_at"]
    assert [(r["table"], r["from"], r["to"]) for r in c.execute("PRAGMA foreign_key_list(workspace_files)")] == [
        ("workspaces", "workspace", "id")
    ]

for name, fields in (("/sample", {}), ("sample", {"state": "unknown"}), ("sample", {"bytes": "large"})):
    with case.assertRaises(sqlite3.IntegrityError):
        workspaces.create(name, **fields)
made = workspaces.create("sample", files={"app/input": "first"}, owner_run="r1")
wid = made["id"]
assert made["version"] == 1
with case.assertRaises(sqlite3.IntegrityError):
    workspaces.create("sample")

with db.tx():
    with patch.object(events, "emit", side_effect=RuntimeError("event write failed")):
        with case.assertRaisesRegex(RuntimeError, "event write failed"):
            workspaces.update(wid, base_version=1, state="removed", files={"app/alternate": "lost"})
    assert workspaces.get(wid) == made
    assert workspaces.files(wid) == {"app/input": "first"}

kept = workspaces.update(wid, base_version=1, state="kept", kept_reason="operator", owner_run="r2",
                         files={"app/input": "second"})
assert kept["version"] == 2
assert workspaces.files(wid) == {"app/input": "second"}
assert workspaces.update(wid, base_version=2, state="kept", files={"app/input": "second"}) == kept
with case.assertRaises(writes.VersionConflict):
    workspaces.update(wid, base_version=1, state="released")
removed = workspaces.update(wid, base_version=2, state="removed", kept_reason="")
assert removed["version"] == 3 and removed["removed_at"]
assert workspaces.open_for("sample") is None
new = workspaces.create("sample", state="held", held_by=[f"app/blocker-{i}" for i in range(21)])
assert new["id"] > wid and len(new["held_by"]) == 20
assert workspaces.by_state("held") == [new]
with db.reader() as c:
    changes = [(r["ver"], r["class"], json.loads(r["data"])) for r in c.execute(
        "SELECT * FROM events_log WHERE subject = ? AND kind = 'workspace.changed' ORDER BY seq",
        (f"workspace:{wid}",),
    )]
live = {"state": "live", "kept_reason": "", "owner_run": "r1"}
keep = {"state": "kept", "kept_reason": "operator", "owner_run": "r2"}
gone = {"state": "removed", "kept_reason": "", "owner_run": "r2"}
assert changes == [(1, "derived", {"old": None, "new": live}), (2, "derived", {"old": live, "new": keep}),
                   (3, "derived", {"old": keep, "new": gone})], changes
""")


class _WorkspaceWorkflowCase(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(suffix=".noindex"))).resolve()
        env = api_check.isolated_env(root)
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        state = root / "state"
        self.enterContext(mock.patch.multiple(paths, HOME=root / "home", CONFIG_DIR=root / "config",
                                             CONFIG_FILE=root / "config/config.toml", STATE_DIR=state,
                                             DB_FILE=state / "arete.db", LOCKS_DIR=state / "locks",
                                             RUNS_DIR=state / "runs", WORKTREES_DIR=root / "worktrees",
                                             LOG_DIR=root / "logs"))
        self.enterContext(mock.patch.object(sandbox, "AUTHORITY", root / "config/verification-authority"))
        paths.CONFIG_FILE.write_text("max_parallel = 1\n")
        db.migrate()
        src = root / "repo"
        src.mkdir()
        util.git(["init", "-q", "-b", "main"], src)
        (src / ".gitignore").write_text(".venv/\n")
        (src / "notes.txt").write_text("original\n")
        util.git(["add", "."], src)
        util.git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                  "-c", "commit.gpgsign=false", "commit", "-qm", "sample"], src)
        self.repo = config.Repo("sample", src, github="test/sample")
        self.cfg = config.Config(repos=[self.repo])

    def checkout(self, tid, status, *, owned=True, part="", **fields):
        ws = paths.WORKTREES_DIR / f"{self.repo.name}-{tid}"
        c = workspace.Checkout(part, self.repo.github, self.repo.path, ws / part, "main")
        workspace.add_worktree(c, f"arete/{tid}", "main")
        (c.wt / ".venv").mkdir()
        (c.wt / ".venv/pyvenv.cfg").write_text("home\n")
        rid = record.start("pipeline", repo=self.repo.name, issue=tid, status=status, **fields)
        run = pipeline.Run(rid, self.repo, paths.RUNS_DIR / rid, tracker=tracker.for_ticket(self.cfg, self.repo, tid),
                           tid=tid)
        run.dir.mkdir(parents=True)
        if owned:
            workspace.note_made(ws, [c], pipeline.workspace_owner(run, ws, [c]))
        else:
            made = workspace.scan_snapshot(ws, [c], {}, (), ())
            legacy = workspace.made_record(ws)
            legacy.parent.mkdir(exist_ok=True)
            legacy.write_text(json.dumps(made))
            workspace_store.backfill(self.cfg)
        return ws, c, run

    def registered(self, wt):
        found = util.git(["worktree", "list", "--porcelain"], self.repo.path)
        return f"worktree {wt}" in found.splitlines()


class WorkspaceSweepTests(_WorkspaceWorkflowCase):
    def test_sweep_distinguishes_waiting_workspaces_from_blocked_removal(self):
        old = util.iso(util.now() - timedelta(days=30))
        merged, _, _ = self.checkout("1", "merged", owned=False, started=old, finished=old)
        running, _, _ = self.checkout("2", "running")
        blocked, c, _ = self.checkout("3", "merged")
        hand = blocked / "hand.txt"
        hand.write_text("by hand\n")
        self.assertEqual(workspace.remove_workspace(blocked, [c]), workspace.Release("held", ["hand.txt"]))
        self.assertTrue(self.registered(blocked))
        gone, _, _ = self.checkout("4", "done")
        shutil.rmtree(gone)

        workspace.cleanup_worktrees(cfg=self.cfg)
        self.assertFalse(merged.exists())
        self.assertIsNone(workspace_store.open_for(merged.name))
        self.assertFalse(self.registered(merged))
        self.assertFalse(self.registered(gone))
        self.assertTrue(running.exists())
        self.assertEqual(hand.read_text(), "by hand\n")
        held, = workspace.held_summary()["rows"]
        self.assertTrue(held["blocked"])
        self.assertEqual(held["names"], ["hand.txt"])
        self.assertIn("hand.txt", held["why"])
        blocked_row = workspace_store.open_for(blocked.name)
        self.assertEqual(blocked_row["sweep_attempts"], 1)
        self.assertIn("hand.txt", blocked_row["last_sweep_error"])
        self.assertIn("hand.txt", heartbeat.check_workspaces()[0].detail)

        hand.unlink()
        notes = blocked / "notes.txt"
        notes.write_text("edited by hand\n")
        self.assertEqual(workspace.workspace_blockers(blocked, [c]), [])
        self.assertEqual(workspace.workspace_blockers(blocked, [c], edits=True), ["notes.txt"])
        workspace.cleanup_worktrees(cfg=self.cfg)
        held, = workspace.held_summary()["rows"]
        self.assertTrue(held["blocked"])
        self.assertEqual(held["names"], ["notes.txt"])
        self.assertEqual(notes.read_text(), "edited by hand\n")

        util.git(["restore", "notes.txt"], blocked)
        external, _, _ = self.checkout("5", "failed")
        local_id = task_store.task_add(self.repo.name, "closed task", reserved_id="t-a123")
        task_store.task_set_state(local_id, "closed")
        closed, _, _ = self.checkout(local_id, "failed")
        with mock.patch.object(tracker, "for_repo", side_effect=AssertionError("sweep read an external ticket")):
            workspace.cleanup_worktrees(cfg=self.cfg)
        self.assertFalse(blocked.exists())
        removed = workspace_store.get(blocked_row["id"])
        self.assertEqual((removed["state"], removed["sweep_attempts"], removed["last_sweep_error"]),
                         ("removed", 3, ""))
        self.assertFalse(closed.exists())
        self.assertTrue(external.exists())
        waiting, = workspace.held_summary()["rows"]
        self.assertFalse(waiting["blocked"])
        self.assertEqual(waiting["names"], [])
        self.assertTrue(waiting["why"].endswith("waits for the ticket"))


class WorkspaceReleaseTests(_WorkspaceWorkflowCase):
    def test_pr_release_records_setup_files_and_removes_the_workspace(self):
        ws, c, run = self.checkout("7", "pr")
        made = workspace_store.open_for(ws.name)
        self.assertEqual(made["owner_run"], run.id)
        (c.wt / ".venv/pyvenv.cfg").write_text("changed during the run\n")
        self.assertTrue(pipeline.release_workspace(run, ws, [c]))
        self.assertFalse(ws.exists())
        self.assertFalse(workspace.made_record(ws).exists())
        self.assertFalse(self.registered(ws))
        self.assertFalse((run.dir / "run.log").exists())
        removed = workspace_store.get(made["id"])
        self.assertEqual(removed["state"], "removed")
        self.assertTrue(removed["released_at"] and removed["removed_at"])
        self.assertEqual(removed["version"], made["version"] + 2)
        self.assertRegex(workspace_store.files(removed["id"])[".venv/"], r"^[0-9a-f]{64}$")
        workspace.note_made(ws, [c], pipeline.workspace_owner(run, ws, [c]), settled="pr")
        self.assertIsNone(workspace_store.open_for(ws.name))

    def test_removal_reports_files_that_arrive_under_lock_and_after_git(self):
        ws, c, _ = self.checkout("7", "done", part="api")
        repo_lock = util.repo_lock
        late = c.wt / "hand.txt"

        @contextmanager
        def write_under_lock(src):
            with repo_lock(src):
                late.write_text("arrived under lock\n")
                yield

        with mock.patch.object(util, "repo_lock", write_under_lock):
            with self.assertRaisesRegex(workspace.RunFailed, r"\(api/hand\.txt\)"):
                workspace.clear_workspace(ws, [c], c.src, [])
        self.assertTrue(self.registered(c.wt))
        late.unlink()

        outside = ws / "hand.txt"

        def write_after_git(args, *a, **kw):
            out = util.git(args, *a, **kw)
            if args[:2] == ["worktree", "remove"]:
                outside.write_text("arrived after git\n")
            return out

        with mock.patch.object(workspace, "git", write_after_git):
            release = workspace.remove_workspace(ws, [c])
        self.assertEqual(release, workspace.Release("held", ["hand.txt"]))
        self.assertEqual(outside.read_text(), "arrived after git\n")
        row = workspace_store.open_for(ws.name)
        self.assertEqual((row["state"], row["held_by"]), ("held", ["hand.txt"]))
        self.assertFalse(self.registered(c.wt))
        outside.unlink()
        self.assertEqual(workspace.remove_workspace(ws, [c]), workspace.Release("removed"))
        self.assertFalse(ws.exists())
        self.assertEqual(workspace_store.get(row["id"])["state"], "removed")

    def test_excluded_checkout_keeps_surviving_workspace_open(self):
        ws, c, _ = self.checkout("8", "done", part="api")
        row = workspace_store.open_for(ws.name)
        excluded = ws / "closed"
        excluded.mkdir()
        (excluded / "hand.txt").write_text("preserved\n")

        release = workspace.remove_workspace(ws, [c], excluded={excluded})

        self.assertEqual(release.state, "removed")
        remaining = workspace_store.open_for(ws.name)
        self.assertEqual((remaining["id"], remaining["state"], remaining["held_by"]),
                         (row["id"], "held", ["closed"]))
        self.assertEqual(remaining["removed_at"], "")
        self.assertEqual((excluded / "hand.txt").read_text(), "preserved\n")
        self.assertFalse(self.registered(c.wt))


class WorkspaceScratchTests(_WorkspaceStoreCase):
    def test_nested_scratch_refresh_updates_the_saved_fingerprint(self):
        self.run_child(r"""
from pathlib import Path
from unittest.mock import patch
from arete import paths, workspace
from arete.store import db, workspaces

db.migrate()
ws = paths.WORKTREES_DIR / "sample-7"
checkout = workspace.Checkout("api", "test/sample", Path.cwd() / "repo", ws / "api", "")
root = checkout.wt / (paths.SCRATCH_PREFIX + "fixture")
root.mkdir(parents=True)
output = checkout.wt / "output.txt"
output.write_text("before recovery")
selector = Path.cwd() / "registry" / "selector.json"
owner = workspace.WorkspaceOwner("r1", "sample", "7", "", [checkout])
with patch.object(workspace, "scratch_left", return_value=[ws, root, selector]):
    workspace.note_made(ws, [checkout], owner)
    saved = workspaces.open_for(ws.name)
    assert saved["retained"] == {"roots": [root.name], "names": ["api/output.txt"]}, saved
    assert workspace.remove_workspace(ws, [checkout]).state == "held"
held = workspaces.open_for(ws.name)
assert held["held_by"] == [".", f"api/{root.name}", selector.name], held
before = workspaces.files(held["id"])["api/output.txt"]
(ws / "hand.txt").write_text("arrived during removal")
workspace.note_made(ws, [checkout], owner, settled="done")
assert "hand.txt" not in workspaces.files(held["id"])
output.write_text("after recovery")
with patch.object(workspace.scratch, "recover_pending", return_value=[str(root)]):
    workspace.finalize_scratch()
refreshed = workspaces.open_for(ws.name)
assert refreshed["version"] == held["version"] + 1, refreshed
assert refreshed["state"] == "held" and refreshed["retained"] == {}
assert workspaces.files(held["id"])["api/output.txt"] == workspace.fingerprint(output, held["salt"]) != before
assert not workspace.made_record(ws).exists()
""")


class WorkspaceServeTests(_WorkspaceStoreCase):
    def test_serve_backfills_before_sweeping_and_records_the_attempt(self):
        self.run_child(r"""
import json
from pathlib import Path
from unittest.mock import Mock, patch
from arete import config, paths, record, serve, workspace
from arete.store import db, workspaces

db.migrate()
repo = config.Repo("sample", Path.cwd() / "repo", tracker="local")
cfg = config.Config(repos=[repo])
ws = paths.WORKTREES_DIR / "sample-7"
ws.mkdir()
(ws / "hand.txt").write_text("preserved")
run = record.start("pipeline", repo=repo.name, issue="7", status="merged")
c = workspace.Checkout("", "", repo.path, ws, "")
owner = workspace.WorkspaceOwner(run, repo.name, "7", "", [c])
legacy = workspace.made_record(ws)
legacy.parent.mkdir()
legacy.write_text(json.dumps({workspace.OWNER_KEY: owner.to_row()}))
stages = ("reap", "forget_unpolled_errors", "watch_oncall", "zte_when_due", "dispatch", "approvals",
          "rotate_when_due", "prune_when_due", "expire_asks", "release_closed_when_due", "remind")
with (patch.object(config, "load_or_problem", return_value=(cfg, "")),
      patch.object(serve, "code_stamp", return_value=frozenset()),
      patch.object(serve.devices, "sweep") as vms,
      patch.object(workspaces, "backfill", wraps=workspaces.backfill) as backfill):
    server = serve.Server()
    server.swept = float("-inf")
    try:
        with patch.multiple(server, load=Mock(return_value=True), **{name: Mock() for name in stages}):
            assert workspaces.open_for(ws.name) is None
            assert server.tick()
            server.sweep.result(timeout=3)
            row = workspaces.open_for(ws.name)
            assert row["owner_run"] == run and row["state"] == "held", row
            assert row["held_by"] == ["hand.txt"] and row["sweep_attempts"] == 1, row
            assert row["version"] == 2 and row["last_sweep_at"], row
            assert "hand.txt" in row["last_sweep_error"]
            with db.reader() as connection:
                event = connection.execute("SELECT data FROM events_log WHERE subject = ? AND ver = ?",
                                           (f"workspace:{row['id']}", row["version"])).fetchone()
            change = json.loads(event["data"])
            assert change["old"]["state"] == "released" and change["new"]["state"] == "held", change
            assert server.tick()
            assert workspaces.get(row["id"]) == row
        backfill.assert_called_once_with(cfg)
        vms.assert_called_once()
    finally:
        server.pool.shutdown()
assert (ws / "hand.txt").read_text() == "preserved" and legacy.is_file()
""")


class _CliThroughServeCase(unittest.TestCase):
    class_timeout = 30
    class_worker = None
    OWNER = "cli-owner@example.test"
    ONLINE = {
        "run replay-sample 7": "POST /api/tasks",
        "task add --repo replay-sample change": None,
        "task list": "GET /api/tasks",
        "task show t-1234": "GET /api/tasks/{task}",
        "task reply t-1234 answer": None,
        "task close t-1234": None,
        "task start --repo replay-sample change": "POST /api/tasks",
        "task run replay-sample 7": "POST /api/tasks",
        "answer r1": "POST /api/asks/{id}/answer",
        "resume r1": "POST /api/runs/{run}/resume",
        "repair r1": "POST /api/runs/{run}/repair",
        "step r1 build": "POST /api/runs/{run}/steps/{step}",
        "wait r1": "GET /api/runs/{run}/events",
        "lines r1": "GET /api/runs/{run}/events",
        "status": None,
        "stop r1": "POST /api/runs/{run}/stop",
        "approve 1": "POST /api/approvals/{id}",
        "forget 1": "POST /api/proposals/{id}/done",
        "file-rule 1": "POST /api/proposals/{id}/approve",
        "reject-rule 1": "POST /api/proposals/{id}/reject",
        "loop start example": "POST /api/loops/{name}/start",
        "loop enable example": "POST /api/loops/{name}/enable",
        "loop disable example": "POST /api/loops/{name}/disable",
        "loop stop example": "POST /api/runs/{run}/stop",
        "session start work": "POST /api/sessions",
        "session attach r1": None,
        "session send r1 hello": None,
        "session stop r1": "POST /api/runs/{run}/stop",
        "oncall off": "POST /api/incidents/switch",
        "oncall on": "POST /api/incidents/switch",
        "oncall cancel 1": "POST /api/incidents/{id}/cancel",
        "oncall dismiss 1": "POST /api/incidents/{id}/dismiss",
        "oncall retry 1": "POST /api/incidents/{id}/retry",
    }
    ONLINE_VARIANTS = {
        "status --json r1": "GET /api/runs/{run}/events",
        "status --json": "GET /api/live",
        "record pin r1": None,
        "record unpin r1": None,
        "approve 1 --reject": "POST /api/approvals/{id}",
        "loop start example -f": "POST /api/loops/{name}/start",
        "session start --attach work": "POST /api/sessions",
        "session --chat 1 start work": "POST /api/chats/{chat}/sessions",
        "session --chat 1 list": "GET /api/chats/{chat}/sessions",
        "session --chat 1 report r1": "GET /api/chats/{chat}/sessions/{session}/report",
        "session --chat 1 stop r1": "POST /api/chats/{chat}/sessions/{session}/stop",
    }
    READS = (
        "ab", "backup --check", "benchmark report missing", "costs", "devices", "doctor", "events",
        "experiment summary missing", "jev report", "jira-check", "loop feed r1", "loop list", "loop status poll",
        "merged", "oncall list", "oncall show 1", "prices diff", "prices recompute r1 build", "prompts",
        "hold", "record r1", "research", "secrets replay-sample", "session list", "session report r1",
        "steps --repo replay-sample", "triage", "verifiers report missing",
        "writers report r1", "zte status",
    )
    READ_VARIANTS = ("approve", "mcp-ask --check", "status --offline", "status --json --offline",
                     "status --json --offline r1", "lines --offline r1",
                     "task show --offline --json {task}")
    LOCAL = {
        "api check": "api_check", "api diff": "contract_evolution", "ask": "jev_ask", "audit": "audit", "background": "background",
        "benchmark fixture": "lab.benchmark", "benchmark run": "lab.benchmark", "benchmark sample": "lab.benchmark",
        "codex-guard": "codex", "dashboard": "dashboard", "devices probe": "devices",
        "document": "document", "eval": "evals", "experiment optimize": "lab.optimize",
        "experiment promote": "lab.optimize", "experiment run": "lab.experiment", "experiment sample": "lab.experiment",
        "guard": "guard", "heartbeat": "heartbeat", "hook": "tool_events", "init": "commands.setup",
        "install": "install", "jev compare": "lab.calibrate", "jev sample": "lab.calibrate",
        "loop exec": "loop_run", "loop new": "scaffold", "loop requeue": "store.loops", "loop try": "scaffold",
        "mcp-ask": "owner_asks", "merge-check": "mergecheck", "migrate": "store.db", "mine": "miner",
        "oncall scan": "oncall", "pilot-doc-fix": "pilot", "poll": "poll", "prices import": "prices",
        "release-journeys": "releasejourneys", "replay sample": "replay", "retrieval run": "lab.retrieval", "retrieval sample": "lab.retrieval",
        "rotate-authority": "scratch", "serve": "serve", "session exec": "sessions", "uninstall": "install",
        "upload-guard": "uploads", "webhook secret": "webhooks.secret",
        "verify-fault arm": "verify_faults", "verify-fault clear": "verify_faults",
        "verify-fault drop-streams": "verify_faults", "verify-fault event": "verify_faults",
        "verifiers run": "lab.verifier_suites", "verifiers sample": "lab.verifier_suites",
        "verify-fault arm": "verify_faults", "verify-fault clear": "verify_faults",
        "verify-fault drop-streams": "verify_faults", "verify-fault event": "verify_faults",
        "writers export": "lab.writer_eval", "writers rate": "lab.writer_eval", "writers run": "lab.writer_eval",
        "writers sample": "lab.writer_eval", "zte pause": "zte",
    }
    LOCAL_VARIANTS = {
        "run replay-sample 7 --dry-run": "pipeline.run_issue / record.start",
        "run replay-sample 7 --local": "pipeline.run_issue", "file-rule 1 --local": "miner.file_rule",
        "loop new --run-id r1 work": "scaffold.draft", "loop start example --events sample.json": "loop_run.launch",
        "session start --task work": "sessions.launch", "session start --task --draft-pr work": "sessions.launch",
        "research --next": "research.next_due", "research replay-sample#7 --run": "research.run_now",
        "triage --refresh": "triage.run", "hold claude 1h": "store.devices",
        "hold claude --release": "store.devices", "costs --correct r1 1 --why correction": "record.correct",
        "costs --backfill": "store.runs", "backup": "backup.snapshot", "backup --restore backup.db": "backup.restore",
        "secrets rm replay-sample secret": "vault",
    }

    @classmethod
    def setUpClass(cls):
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        cls.addClassCleanup(setattr, cls, "class_worker", None)
        cls.class_worker = stack.enter_context(cls.start_worker())

    @staticmethod
    def prepare_fixture(root):
        from arete import paths

        lines, _ = api_check.prepare_replay(root)
        config_text = "\n".join(lines) + "\n"
        paths.CONFIG_FILE.write_text(config_text)
        # The copied checkout should use the copy of its bare origin.
        subprocess.run(["git", "remote", "set-url", "origin", "../origin.git"],
                       cwd=root / "sample/repo", check=True, timeout=CHILD_HANG_TIMEOUT)
        db.migrate()
        return config_text

    @classmethod
    @contextmanager
    def start_worker(cls):
        with (tempfile.TemporaryDirectory(prefix="arete-cli-fixture-") as directory,
              ExitStack() as stack):
            root = Path(directory).resolve()
            parent, child = socketpair()
            stack.enter_context(parent)
            stack.enter_context(child)
            output = root / "worker.log"
            entry = ("from tests import test_behavior; "
                     "getattr(test_behavior, sys.argv[1]).run_checks(int(sys.argv[2]))")
            command = api_check.child_command(Path(__file__).resolve().parents[1], entry,
                                              cls.__name__, str(child.fileno()))
            with output.open("w") as log:
                process = subprocess.Popen(command, cwd=root, env=api_check.isolated_env(root),
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log, pass_fds=(child.fileno(),))
            child.close()
            parent.settimeout(cls.class_timeout - 2)
            try:
                with parent.makefile("rwb") as channel:
                    yield channel, output
            finally:
                parent.close()
                try:
                    status = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    status = process.wait()
            if status:
                raise RuntimeError(f"CLI test worker exited with status {status}\n{output.read_text(errors='replace')}")

    def in_subprocess(self, case):
        if self.class_worker is None:
            # PinCommandTests also uses this fixture outside the three classes.
            with self.start_worker() as worker:
                self.run_check(worker, case)
        else:
            self.run_check(self.class_worker, case)

    def run_check(self, worker, case):
        channel, log = worker
        try:
            channel.write(case.encode() + b"\n")
            channel.flush()
            reply = channel.readline()
        except OSError as error:
            self.fail(f"{case}: {error}\n{log.read_text(errors='replace')}")
        if not reply:
            self.fail(f"worker exited during {case}\n{log.read_text(errors='replace')}")
        ok, output = json.loads(reply)
        if not ok:
            self.fail(f"{output}\n{log.read_text(errors='replace')}")

    @classmethod
    def run_checks(cls, fd):
        import faulthandler

        from arete import codebase

        with (socket(fileno=fd) as connection, connection.makefile("rwb") as channel,
              mock.patch("urllib.request.urlopen", side_effect=AssertionError("urllib.request.urlopen")),
              mock.patch.object(gh, "gh", side_effect=AssertionError("arete.gh.gh"))):
            connection.set_inheritable(False)
            faulthandler.dump_traceback_later(cls.class_timeout - 4)
            fixture_root = Path.cwd()
            config_text = cls.prepare_fixture(fixture_root)
            # Import once so all module-level paths can be rebound for each check.
            codebase.import_everything()
            faulthandler.cancel_dump_traceback_later()
            for request in channel:
                test = cls(request.decode().strip())
                test.fixture_root = fixture_root
                test.fixture_config = config_text
                output = StringIO()
                faulthandler.dump_traceback_later(cls.class_timeout - 4)
                try:
                    result = Context().run(unittest.TextTestRunner(stream=output, verbosity=2).run, test)
                finally:
                    faulthandler.cancel_dump_traceback_later()
                channel.write(json.dumps([result.wasSuccessful(), output.getvalue()]).encode() + b"\n")
                channel.flush()

    def setUp(self):
        if not self._testMethodName.startswith("check_"):
            return
        from arete import client, dbstats, guard, paths, sandbox

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="check-", dir=self.fixture_root))
        root = Path(directory)
        env = api_check.isolated_env(root)
        self.stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        self.stack.enter_context(chdir(root))
        self.stack.enter_context(mock.patch.object(tempfile, "tempdir", env["TMPDIR"]))
        for name, module in tuple(sys.modules.items()):
            if name.startswith("arete.") and module is not None:
                paths_here = {key: root / value.relative_to(self.fixture_root) for key, value in vars(module).items()
                              if isinstance(value, Path) and value.is_relative_to(self.fixture_root)}
                if paths_here:
                    self.stack.enter_context(mock.patch.multiple(module, **paths_here))
        self.stack.enter_context(mock.patch.object(guard, "LOGIN_FILES", tuple(
            root / path.relative_to(self.fixture_root) for path in guard.LOGIN_FILES)))
        self.stack.enter_context(mock.patch.object(sandbox, "AUTHORITY", paths.CONFIG_DIR / "verification-authority"))
        # Keep code imports warm while resetting state keyed by requests, processes and databases.
        for patch in (mock.patch.object(api_http, "RATES", api_http.RateLimits()),
                      mock.patch.object(api_http, "ALIAS_DAYS", {}),
                      mock.patch.object(contract, "USES", contract.USES.copy()),
                      mock.patch.object(dashboard, "POSTS_IN_FLIGHT", dashboard.Gauge()),
                      mock.patch.object(db, "_CHECKED", set()),
                      mock.patch.object(dbstats, "SUMMARIES", {}),
                      mock.patch.object(idempotency, "ACTIVE", {}),
                      mock.patch.object(client, "_acquisition_thread", None)):
            self.stack.enter_context(patch)
        for cached in (gh.me, util.github_token):
            cached.cache_clear()
            self.stack.callback(cached.cache_clear)
        shutil.copytree(self.fixture_root / "sample", root / "sample")
        original = json.dumps(str(self.fixture_root / "sample"))[1:-1]
        copied = json.dumps(str(root / "sample"))[1:-1]
        self.fixture_config = self.fixture_config.replace(original, copied)
        paths.CONFIG_FILE.write_text(self.fixture_config)

    def prepare_state(self):
        from arete import paths

        if not paths.DB_FILE.exists():
            shutil.copytree(self.fixture_root / "state", paths.STATE_DIR, dirs_exist_ok=True)

    def cli(self, command, stdin=""):
        from arete import cli

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", StringIO(stdin)):
            code = cli.main(command.split())
        return code, out.getvalue().splitlines(), err.getvalue()

    def problem(self, command, reason):
        code, lines, err = self.cli(command)
        self.assertEqual(code, 1, (command, lines, err))
        self.assertEqual(len(lines), 1, (command, lines, err))
        self.assertNotIn("Traceback", err)
        answer = json.loads(lines[0])
        self.assertEqual(answer.get("reason") or answer.get("code"), reason, (command, answer))
        return answer

    @contextmanager
    def listening(self, server):
        # Join request threads before unwinding the environment and path patches.
        server.daemon_threads = False
        with mock.patch.dict(os.environ, {"ARETE_SERVER": f"http://127.0.0.1:{server.server_port}"}), server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                yield
            finally:
                server.shutdown()
                thread.join()

    @contextmanager
    def serve_at(self, requests, *, write_instances=None):
        self.prepare_state()
        principals.seed_owner(self.OWNER)

        class Handler(dashboard.Handler):
            def send_response(handler, code, message=None):
                requests.append((handler.command, handler.path, code, handler.headers.get("Idempotency-Key")))
                if write_instances is not None and handler.command != "GET":
                    write_instances.append(handler.headers.get("X-Arete-Instance"))
                super().send_response(code, message)

            def log_request(self, *_):
                pass

        address = "127.0.0.1"
        node = tailnet.Node((address,), frozenset({address}), address)
        logins = tailnet.Logins(mock.Mock())
        with mock.patch.object(logins, "of", return_value=self.OWNER), mock.patch("socket.getfqdn", return_value=address):
            server = tailnet.Server(address, 0, node, logins, False, Handler, dashboard.Redirect)
            with self.listening(server):
                yield server

    @contextmanager
    def stub(self, response):
        class Handler(BaseHTTPRequestHandler):
            def reply(handler):
                result = response(handler)
                if result is None:
                    handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
                    handler.close_connection = True
                    return
                status, answer, headers = result
                raw = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                handler.send_response(status)
                for name, value in headers.items():
                    handler.send_header(name, value)
                handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw)

            do_GET = do_POST = reply

            def log_message(self, *_):
                pass

        address = "127.0.0.1"
        with mock.patch("socket.getfqdn", return_value=address):
            server = ThreadingHTTPServer((address, 0), Handler)
        with self.listening(server):
            yield


class CliThroughServeAccessTests(_CliThroughServeCase):
    def test_parser_groups_and_cli_calls(self):
        self.in_subprocess("check_parser_groups_and_cli_calls")

    def check_parser_groups_and_cli_calls(self):
        from arete import cli
        from arete.commands import common

        def leaves(parser, prefix=()):
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    if prefix and not action.required:
                        yield prefix
                    for name, sub in action.choices.items():
                        yield from leaves(sub, (*prefix, name))
                    return
                if action.dest == "action" and action.choices:
                    yield from ((*prefix, name) for name in action.choices)
                    return
            yield prefix

        parser = cli.parser()
        actual = set(leaves(parser))
        groups = {}
        for group, case in ((1, self.ONLINE), (2, self.READS), (3, self.LOCAL)):
            for command in case:
                words = tuple(command.split())
                match = next((words[:n] for n in (2, 1) if words[:n] in actual), words)
                self.assertNotIn(match, groups)
                groups[match] = group
        self.assertSetEqual(actual, set(groups))
        for command in (*self.ONLINE, *self.ONLINE_VARIANTS, *self.READS, *self.READ_VARIANTS, *self.LOCAL_VARIANTS,
                        "api diff --check-artifact"):
            parser.parse_args(command.split())
        forbidden = {"record": {"pin", "unpin"}, "control": {"decide", "switch", "stop_run"},
                     "sessions": {"send"}, "chat": {"start_session", "stop_session"}, "oncall": {"switch"}}
        files = [Path(cli.__file__), *Path(common.__file__).parent.glob("*.py")]
        for path in files:
            for call in (node for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Call)):
                function = call.func
                if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
                    module, name = function.value.id, function.attr
                    self.assertNotIn(name, forbidden.get(module, ()), f"{path.name}:{call.lineno}")
                    if module == "state":
                        self.assertIn(("connect", "state.reader"),
                                      [(kw.arg, ast.unparse(kw.value)) for kw in call.keywords])
                elif isinstance(function, ast.Name) and function.id == "getattr" and call.args:
                    self.assertNotEqual(ast.unparse(call.args[0]), "oncall")

    def test_commands_fail_when_serve_is_down(self):
        self.in_subprocess("check_commands_fail_when_serve_is_down")

    def check_commands_fail_when_serve_is_down(self):
        from errno import ECONNREFUSED

        from arete import paths

        paths.SERVE_BEAT_FILE.write_text(json.dumps({"at": "2000-01-01T00:00:00Z", "engine": "old-engine"}))
        for name in ("pipeline.run_issue", "delivery.send", "miner.file_rule", "loop_run.launch", "sessions.launch",
                     "control.stop_run", "record.pin", "oncall.dismiss", "chat.start_session", "chat.stop_session"):
            self.stack.enter_context(mock.patch("arete." + name, side_effect=AssertionError(name)))
        host, port = "127.0.0.1", 1
        url = f"http://{host}:{port}"

        def refuse_connection(address, *args, **kwargs):
            self.assertEqual(address, (host, port))
            raise ConnectionRefusedError(ECONNREFUSED, os.strerror(ECONNREFUSED))

        self.stack.enter_context(mock.patch("socket.create_connection", side_effect=refuse_connection))
        paths.CONFIG_FILE.write_text(self.fixture_config + f'\n[server]\nurl = "{url}"\n')
        for command in (*self.ONLINE, *self.ONLINE_VARIANTS):
            with self.subTest(command=command):
                self.assertIn(url, self.problem(command, "server_down")["error"])
                self.assertFalse(paths.DB_FILE.exists())
        paths.CONFIG_FILE.write_text(self.fixture_config)
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=3)
        util.write_json(paths.SERVER_FILE, {**paths.state_and_config(), "pid": exited.pid,
                                           "addresses": ["127.0.0.1"], "port": port})
        self.assertIn("process has ended", self.problem("run replay-sample 7", "server_down")["error"])
        with mock.patch.dict(os.environ, {"ARETE_SERVER": url}):
            self.problem("run replay-sample 7", "server_down")
            self.problem("loop start example", "server_down")
        self.assertFalse(paths.DB_FILE.exists())

    def test_status_reports_stale_heartbeat_when_import_fails(self):
        self.in_subprocess("check_status_reports_stale_heartbeat_when_import_fails")

    def check_status_reports_stale_heartbeat_when_import_fails(self):
        from arete import codebase, paths

        paths.SERVE_BEAT_FILE.write_text(json.dumps({
            "at": "2000-01-01T00:00:00Z", "engine": "old-engine", "checkout_head": "broken-head",
        }))
        problem = "arete: cannot import arete.commands.setup: RuntimeError: broken candidate"
        with mock.patch.object(codebase, "load", side_effect=SystemExit(problem)):
            code, lines, err = self.cli("status --limit 1")
        self.assertEqual((code, lines), (1, []))
        self.assertEqual(err.splitlines(), [
            problem,
            "last recorded serve heartbeat at 2000-01-01T00:00:00Z, running code old-engine, checkout HEAD broken-head",
        ])
        self.assertFalse(paths.DB_FILE.exists())

    def test_readers_and_worker_guards(self):
        self.in_subprocess("check_readers_and_worker_guards")

    def test_readers_report_missing_database(self):
        self.in_subprocess("check_readers_report_missing_database")

    def check_readers_report_missing_database(self):
        from arete import paths

        with mock.patch.object(db, "connect_db", side_effect=AssertionError("opened missing database")) as connect_db:
            for command in (*self.READS, *self.READ_VARIANTS):
                with self.subTest(command=command):
                    code, lines, err = self.cli(command.format(task="t-1234"))
                    self.assertEqual((code, err), (1, ""))
                    self.assertEqual(len(lines), 1)
                    self.assertEqual(json.loads(lines[0])["error"],
                                     f"no arete.db at {paths.DB_FILE}; run arete serve or arete migrate")
                    self.assertFalse(paths.DB_FILE.exists())
            connect_db.assert_not_called()

    def test_events_read_the_local_log(self):
        self.in_subprocess("check_events_read_the_local_log")

    def check_events_read_the_local_log(self):
        from arete import client, paths

        self.prepare_state()
        event = {"ts": util.iso(), "kind": "tool", "cls": "fixture"}
        paths.EVENTS_FILE.write_text(json.dumps(event) + "\n")
        with mock.patch.object(client, "server", side_effect=AssertionError("events contacted serve")):
            code, lines, err = self.cli("events --raw")
        self.assertEqual((code, lines, err), (0, [json.dumps(event)], ""))

    def check_readers_and_worker_guards(self):
        from arete import record, run_lines
        from arete.store import tasks as task_store

        self.prepare_state()
        task = task_store.task_add("replay-sample", "work")
        run = record.start("session", run_id="r1", pid=None)
        record.settle(run, "done", end_line="done")
        run_lines.write(run, run_lines.END, status="done")
        calls = []
        real_connect = db.connect_db

        def readonly(*args, **kwargs):
            if not kwargs.get("readonly"):
                calls.append((args, kwargs))
                raise AssertionError("CLI opened the database for writing")
            return real_connect(*args, **kwargs)

        with mock.patch.object(db, "connect_db", readonly):
            for command in (*self.READS, *self.READ_VARIANTS):
                with self.subTest(command=command):
                    self.cli(command.format(task=task))
                    self.assertFalse(calls)
            for command in ("run replay-sample 7 --local", "file-rule 1 --local", "loop new --run-id r1 work",
                            "loop exec --run-id r1", "session exec r1"):
                code, _, error = self.cli(command)
                self.assertEqual(code, 1)
                self.assertIn("started by Arete", error)
            for command in ("lines --offline r1", "status --offline", "status --json --offline r1",
                            f"task show --offline {task}"):
                code, lines, _ = self.cli(command)
                self.assertEqual(code, 0)
                self.assertTrue(lines)
                self.assertTrue(all(json.loads(line)["source"] == "local" for line in lines))

    def test_identity_change_blocks_writes(self):
        self.in_subprocess("check_identity_change_blocks_writes")

    def check_identity_change_blocks_writes(self):
        from arete import paths

        paths.CONFIG_FILE.write_text(self.fixture_config + '\n[server]\nurl = "http://127.0.0.1:1"\n')
        sent = []
        instances = iter(("first", "second"))

        def response(request):
            sent.append(request.command)
            body = ({"instance": {"id": next(instances)}} if request.command == "GET" else
                    {"task": {"id": "t-1234"}})
            return 200, body, {}

        with self.stub(response):
            self.assertEqual(self.cli("task add --repo replay-sample first")[0], 0)
            self.problem("task add --repo replay-sample second", "server_identity_changed")
        self.assertEqual(sent, ["GET", "POST", "GET"])

    def test_receiving_serve_rejects_mismatched_write_instances(self):
        self.in_subprocess("check_receiving_serve_rejects_mismatched_write_instances")

    def check_receiving_serve_rejects_mismatched_write_instances(self):
        from arete import api
        from arete.store import tasks as task_store

        seen, instances = [], []
        with self.serve_at(seen, write_instances=instances) as server:
            headers = {"X-Arete": "1", "Idempotency-Key": "existing-key", "Content-Type": "application/json"}
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                conn.request("POST", "/api/v1/tasks",
                             json.dumps({"repo": "replay-sample", "body": "change", "title": "", "kind": "feat"}),
                             headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200, response.read())
            finally:
                conn.close()
            before = task_store.tasks()
            with (mock.patch.object(api, "meta", return_value={"instance": {"id": "previous-instance"}}),
                  mock.patch.object(idempotency, "owning", wraps=idempotency.owning) as owning):
                for command, key in (("task add --repo replay-sample change", "existing-key"),
                                     ("task add --repo replay-sample other", "fresh-key"),
                                     ("loop disable poll", "loop-key"),
                                     ("task start --repo replay-sample work", "start-key")):
                    with self.subTest(command=command), mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": key}):
                        self.assertIn("instance changed", self.problem(command, "conflict")["error"])
                owning.assert_not_called()
            self.assertEqual(len(before), 1)
            self.assertEqual(task_store.tasks(), before)
        self.assertEqual([code for method, _, code, _ in seen if method == "POST"], [200, 409, 409, 409, 409])
        self.assertEqual(instances, [None] + ["previous-instance"] * 4)

    def test_server_discovery_ignores_unrelated_config_values(self):
        self.in_subprocess("check_server_discovery_ignores_unrelated_config_values")

    def check_server_discovery_ignores_unrelated_config_values(self):
        from arete import paths, run_lines

        with self.serve_at([]) as server:
            port = server.server_port
            url = os.environ["ARETE_SERVER"]
            record = {**paths.state_and_config(), "pid": os.getpid(), "addresses": ["127.0.0.1"], "port": port}
            util.write_json(paths.SERVER_FILE, record)
            run_lines.write("r1", run_lines.END, status="done")
            invalid = self.fixture_config + '\n[document]\nenabled = "invalid"\n'

            def report():
                code, lines, err = self.cli("status --json r1")
                self.assertEqual(code, 0, (lines, err))
                self.assertEqual([json.loads(line)["status"] for line in lines], ["done"])

            with mock.patch.dict(os.environ, {"ARETE_SERVER": ""}):
                paths.CONFIG_FILE.write_text(invalid)
                report()
                util.write_json(paths.SERVER_FILE, {**record, "addresses": []})
                paths.CONFIG_FILE.write_text(invalid + f'\n[server]\nurl = "{url}"\n')
                report()
            paths.CONFIG_FILE.write_text(invalid + "\n[server]\nurl = false\n")
            report()


class CliThroughServeCommandTests(_CliThroughServeCase):
    def test_approval_waits_out_drain(self):
        self.in_subprocess("check_approval_waits_out_drain")

    def check_approval_waits_out_drain(self):
        from arete import client, delivery

        seen, refused, instances = [], [], []
        with (self.serve_at(seen, write_instances=instances),
              mock.patch.object(delivery.EMITS["push"], "execute", return_value="sent") as send):
            number = approvals_store.add_approval("example", "", {"type": "push", "item": {}}, "deliver")
            connect = client.connect

            def reconnect(where, *args):
                if not refused and any(status == 503 for _, _, status, _ in seen):
                    refused.append(True)
                    dashboard.POSTS_IN_FLIGHT.reopen()
                    raise client.Down(client.address(where), "connection refused")
                return connect(where, *args)

            dashboard.POSTS_IN_FLIGHT.close()
            try:
                with mock.patch.object(client, "connect", reconnect), mock.patch.object(client, "RETRY_SECONDS", .01):
                    code, lines, err = self.cli(f"approve {number}")
                self.assertEqual(code, 0, (lines, err))
            finally:
                dashboard.POSTS_IN_FLIGHT.reopen()
            send.assert_called_once()
            self.assertEqual(approvals_store.approval(number)["status"], "done")
        self.assertEqual(refused, [True])
        writes = [(code, key) for method, _, code, key in seen if method == "POST"]
        self.assertEqual([code for code, _ in writes], [503, 200])
        self.assertIsNotNone(writes[0][1])
        self.assertEqual(writes[0][1], writes[1][1])
        with db.reader() as conn:
            self.assertEqual(instances, [db.metadata(conn)["instance_id"]] * 2)

    def test_file_rule_detaches_a_marked_worker_once(self):
        self.in_subprocess("check_file_rule_detaches_a_marked_worker_once")

    def check_file_rule_detaches_a_marked_worker_once(self):
        from arete import miner

        seen, workers = [], []
        real_detach = control.detach

        def detach(args):
            workers.append(args)
            return real_detach(args)

        def spawn(argv, *, env, **_):
            self.assertEqual(env.get(control.WORKER_ENV), "file-rule")
            with mock.patch.dict(os.environ, env):
                thread = threading.Thread(target=lambda: self.cli(" ".join(argv[-3:])))
                thread.start()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            return SimpleNamespace(pid=1, wait=lambda: 0)

        child = SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT)
        with self.serve_at(seen), mock.patch.object(control, "detach", side_effect=detach), \
                mock.patch.object(control, "subprocess", child), \
                mock.patch.object(miner, "file_rule", return_value="filed") as file_rule:
            approvals_store.add_proposal("", {"repo": "replay-sample"}, "approve")
            number = approvals_store.proposals()[0]["id"]
            code, lines, err = self.cli(f"file-rule {number}")
            self.assertEqual(code, 0, (lines, err))
            file_rule.assert_called_once_with(number)
        self.assertEqual(workers, [["file-rule", str(number), "--local"]])
        self.assertEqual([path for method, path, _, _ in seen if method == "POST"], [f"/api/proposals/{number}/approve"])

    def test_version_and_missing_field_errors_are_flat(self):
        self.in_subprocess("check_version_and_missing_field_errors_are_flat")

    def check_version_and_missing_field_errors_are_flat(self):
        versions = []

        def old(request):
            versions.append(request.headers.get("X-Arete-Client"))
            return 400, {"reason": "client_too_old", "required_version": "1.99"}, {}

        with self.stub(old):
            for command in ("wait r1", "answer r1", "status --json r1"):
                self.assertIn("1.99", self.problem(command, "client_too_old")["error"])
        self.assertEqual(len(versions), 3)
        for version in versions:
            self.assertRegex(version, r"^cli/1\.\d+$")

        with self.stub(lambda request: (200, {"instance": {"id": "first"}} if request.path == "/api/meta" else {}, {})):
            self.assertIn("asks", self.problem("answer r1", None)["error"])

        def missing_task(request):
            if request.path == "/api/meta":
                return 200, {"instance": {"id": "session"}}, {}
            return 200, {"id": "r1", "runner": "pi", "model": "sample", "device": "local", "cwd": "/fixture",
                         "branch": "", "tools": []}, {}

        with self.stub(missing_task):
            self.assertIn("task", self.problem("session start work", None)["error"])

    def test_follow_preambles_wait_for_valid_feed(self):
        self.in_subprocess("check_follow_preambles_wait_for_valid_feed")

    def check_follow_preambles_wait_for_valid_feed(self):
        feed = {"lines": []}

        def response(request):
            if request.path == "/api/meta":
                return 200, {"instance": {"id": "follower"}}, {}
            if request.path.endswith("/steer"):
                return 405, {"code": "method_not_allowed"}, {"Allow": "POST"}
            if request.command == "POST":
                return 200, {"id": "r1", "task": "", "runner": "pi", "model": "sample", "device": "local",
                             "cwd": "/fixture", "branch": "", "tools": []}, {}
            if request.path == "/api/runs/r1":
                return 200, {"run": {"kind": "session", "status": "done"}}, {}
            return 200, feed, {}

        with self.stub(response):
            for command in ("loop start example -f", "session attach r1", "session start --attach work"):
                with self.subTest(command=command):
                    self.assertIn("restore_epoch", self.problem(command, None)["error"])
            feed.update(restore_epoch=0, lines=[{"id": 1, "text": "ready", "ts": "", "kind": "loop"}, {"id": 2}])
            self.assertIn("feed.lines.text", self.problem("loop start example -f", None)["error"])

    def test_task_updates_and_stop_replay(self):
        self.in_subprocess("check_task_updates_and_stop_replay")

    def check_task_updates_and_stop_replay(self):
        from arete import client, record
        from arete.store import runs, tasks as task_store

        seen = []
        with self.serve_at(seen):
            code, lines, err = self.cli("task add --repo replay-sample change")
            self.assertEqual(code, 0, (lines, err))
            task = lines[0]
            self.assertEqual(task_store.task_get(task)["state"], "todo")
            self.assertFalse(runs.runs())
            self.assertEqual(json.loads(self.cli("task list")[1][0])["id"], task)
            before = task_store.task_get(task)["version"]
            with client.invocation():
                path = f"/api/v1/tasks/{task}"
                changed = client.call("PATCH", path, {"base_version": before, "title": "Edited"})["task"]
                self.assertEqual(changed["title"], "Edited")
                with self.assertRaises(client.Failed) as stale:
                    client.call("PATCH", path, {"base_version": before, "title": "Stale"})
                self.assertEqual(stale.exception.code, 409)
            self.assertEqual(self.cli(f"task reply {task} reply")[0], 0)
            self.assertEqual([(c["author"], c["body"]) for c in task_store.task_comments(task)], [("owner", "reply")])
            self.assertEqual(self.cli(f"task close {task}")[0], 0)
            self.problem(f"task close {task}", "invalid_state")
            run = record.start("session", run_id="r1", pid=None)
            with mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "stop-replay"}):
                first = self.cli(f"stop {run}")
                self.assertEqual(first[0], 0, first)
                self.assertEqual(self.cli(f"stop {run}")[:2], first[:2])
            self.assertEqual(runs.run_get(run)["status"], "stopped")

    def test_loop_stop_resolves_the_current_run(self):
        self.in_subprocess("check_loop_stop_resolves_the_current_run")

    def check_loop_stop_resolves_the_current_run(self):
        from arete import record
        from arete.store import runs

        seen = []
        with self.serve_at(seen), mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "loop-stop"}):
            record.start("loop", run_id="r1", loop="poll", pid=None)
            first = self.cli("loop stop poll")
            self.assertEqual(first[0], 0, first)
            self.assertEqual(runs.run_get("r1")["status"], "stopped")
            self.assertEqual(self.cli("loop stop poll"), (0, ["not running"], ""))
            record.start("loop", run_id="r2", loop="poll", pid=None)
            next_run = self.cli("loop stop poll")
            self.assertEqual(next_run[0], 0, next_run)
            self.assertEqual(runs.run_get("r2")["status"], "stopped")
        self.assertEqual([key for method, _, _, key in seen if method == "POST"], ["loop-stop:r1", "loop-stop:r2"])
        self.assertEqual([path for method, path, _, _ in seen if method == "GET"].count("/api/loops/poll"), 3)


class CliThroughServeRunTests(_CliThroughServeCase):
    def test_task_start_recovers_from_a_dropped_response_and_reload(self):
        self.in_subprocess("check_task_start_recovers_from_a_dropped_response_and_reload")

    def check_task_start_recovers_from_a_dropped_response_and_reload(self):
        from errno import ECONNREFUSED
        from socket import create_connection

        from arete import client

        started = {"task": "t-1234", "state": "queued", "run": ""}
        sent, refused = [], []

        def response(request):
            if request.path == "/api/meta":
                return 200, {"instance": {"id": "reloading"}}, {}
            sent.append((request.path, request.headers.get("Idempotency-Key")))
            if len(sent) == 1:
                return None
            if len(sent) == 2:
                return 503, {"error": "server is draining", "code": "draining"}, {"Retry-After": "0"}
            return 200, started, {}

        def connect(address, *args, **kwargs):
            if len(sent) == 2 and not refused:
                refused.append(address)
                raise ConnectionRefusedError(ECONNREFUSED, os.strerror(ECONNREFUSED))
            return create_connection(address, *args, **kwargs)

        key = "restart-key"
        with (self.stub(response), mock.patch("socket.create_connection", side_effect=connect),
              mock.patch.object(client, "RETRY_SECONDS", 0), mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": key})):
            code, lines, err = self.cli("run replay-sample 7")
        self.assertEqual((code, list(map(json.loads, lines)), err), (0, [started], f"Idempotency-Key: {key}\n"))
        self.assertEqual(len(refused), 1)
        self.assertEqual(sent, [("/api/tasks", key)] * 3)

    def test_run_validates_terminal_status_before_printing(self):
        self.in_subprocess("check_run_validates_terminal_status_before_printing")

    def check_run_validates_terminal_status_before_printing(self):
        from arete import client

        started = {"task": "t-1234", "state": "running", "run": "r1"}
        step = {"event": "step_end", "seq": 1, "step": "plan"}
        report = {"event": "report", "seq": 2}
        events = client.events

        def response(request):
            if request.path == "/api/meta":
                return 200, {"instance": {"id": "reports"}}, {}
            if request.path == "/api/runs/r1/events?from=0&follow=1":
                return 200, b"".join(json.dumps(line).encode() + b"\n" for line in (step, report)), {}
            return 200, started, {}

        def observed_events(*args, **kwargs):
            self.assertEqual(list(map(json.loads, sys.stdout.getvalue().splitlines())), [started])
            with closing(events(*args, **kwargs)) as stream:
                yield next(stream)
                self.assertEqual(list(map(json.loads, sys.stdout.getvalue().splitlines())), [started, step])
                yield from stream

        with self.stub(response), mock.patch.object(client, "events", side_effect=observed_events):
            code, lines, err = self.cli("run replay-sample 7")
            error = {"error": "server answer is missing or has an invalid field status"}
            self.assertEqual((code, list(map(json.loads, lines))), (1, [started, step, error]), err)
            self.assertNotIn("Traceback", err)
            report["status"] = "done"
            code, lines, err = self.cli("run replay-sample 7")
        self.assertEqual((code, list(map(json.loads, lines))), (0, [started, step, report]), err)

    def test_run_retries_keep_the_original_version(self):
        self.in_subprocess("check_run_retries_keep_the_original_version")

    def check_run_retries_keep_the_original_version(self):
        from arete import record
        from arete.store import runs

        seen = []
        with self.serve_at(seen):
            record.start("loop", run_id="r2", loop="poll", pid=None)
            for action, command, args in (("resume", "resume r2", ("r2", "")),
                                          ("repair", "repair r2", ("r2",)),
                                          ("run_step", "step r2 build", ("r2", "build", {}))):
                version = runs.run_get("r2")["version"]
                with (mock.patch.object(tasks, action, return_value={"run": "r2"}) as handler,
                      mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": f"run-{action}"})):
                    first = self.cli(command)
                    self.assertEqual(first[0], 0, first)
                    runs.note("r2", summary=action)
                    self.assertGreater(runs.run_get("r2")["version"], version)
                    self.assertEqual(self.cli(command)[:2], first[:2])
                    handler.assert_called_once_with(*args, version)

        reads, writes = [], []

        def respond(request):
            if request.path == "/api/meta":
                return 200, {"instance": {"id": "ask-replay"}}, {}
            if request.command == "GET":
                reads.append(request.path)
                asks = [{"id": "ask1", "run": "r1"}] if not writes else []
                return 200, {"asks": asks}, {}
            writes.append((request.path, request.headers["Idempotency-Key"]))
            return 200, {"answered": True}, {}

        with self.stub(respond), mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "ask-replay"}):
            command = 'answer r1 --json {"answers":[{"text":"yes"}]}'
            first = self.cli(command)
            self.assertEqual(first[0], 0, first)
            self.assertEqual(self.cli(command)[:2], first[:2])
        self.assertEqual(reads, ["/api/asks"])
        self.assertEqual(writes, [("/api/asks/ask1/answer", "ask-replay")] * 2)

    def test_interrupted_launch_does_not_repeat(self):
        self.in_subprocess("check_interrupted_launch_does_not_repeat")

    def check_interrupted_launch_does_not_repeat(self):
        from arete import record, sessions
        from arete.store import runs

        def launched(*args, **kwargs):
            record.start("session", pid=None)
            raise RuntimeError("interrupted after launch")

        with (self.serve_at([]), mock.patch.object(sessions, "launch", side_effect=launched) as launch,
              mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "session-start"})):
            self.problem("session start work", 500)
            reply = self.problem("session start work", 409)
            self.assertIn("earlier request", reply["error"])
            launch.assert_called_once()
            self.assertEqual(len(runs.runs(kind="session")), 1)

    def test_deferred_session_attach_can_send_input(self):
        self.in_subprocess("check_deferred_session_attach_can_send_input")

    def check_deferred_session_attach_can_send_input(self):
        from arete import client, sessions
        from arete.commands import sessions as commands

        def follow(run, *, send, remote, on_start):
            self.assertFalse(remote)
            on_start()
            send()

        launched = {"id": "r1", "runner": "pi", "model": "sample", "device": "local", "cwd": str(Path.cwd()),
                    "branch": "", "tools": [], "task": True}
        with (mock.patch.object(client, "server", side_effect=AssertionError("contacted serve")),
              mock.patch.object(sessions, "launch", return_value=launched) as launch,
              mock.patch.object(sessions, "send") as send, mock.patch.object(commands, "follow", side_effect=follow),
              mock.patch("select.select", return_value=([True], [], []))):
            for options in ("", "--draft-pr --repo replay-sample"):
                code, lines, err = self.cli(f"session start --task --attach {options} work", "hello\n")
                self.assertEqual(code, 0, (lines, err))
                self.assertTrue(launch.call_args.kwargs["task"])
                self.assertEqual(launch.call_args.kwargs["draft_pr"], bool(options))
                send.assert_called_once_with("r1", "hello\n")
                send.reset_mock()

    def test_session_attach_uses_serve_for_feed_and_input(self):
        self.in_subprocess("check_session_attach_uses_serve_for_feed_and_input")

    def check_session_attach_uses_serve_for_feed_and_input(self):
        from arete import record, sessions
        from arete.store import runs

        send = sessions.send

        def finish_after_message(session, text):
            queued = send(session, text)
            record.settle(session, "done", end_line="done")
            return queued

        seen = []
        with (self.serve_at(seen), mock.patch.object(sessions, "send", side_effect=finish_after_message) as steer,
              mock.patch("select.select", return_value=([True], [], []))):
            record.start("session", run_id="r1", pid=None)
            runs.add_feed("r1", "session", "ready for input")
            code, lines, error = self.cli("session attach r1", "hello\n")
            self.assertEqual(code, 0, (lines, error))
            self.assertTrue(lines[0].startswith("following;"))
            self.assertTrue(any("ready for input" in line for line in lines), lines)
            steer.assert_called_once_with("r1", "hello\n")
            self.assertEqual(runs.run_get("r1")["status"], "done")
        routes = [(method, path) for method, path, status, _ in seen if status == 200]
        self.assertIn(("GET", "/api/feed/r1?after=0"), routes)
        self.assertIn(("GET", "/api/runs/r1"), routes)
        self.assertIn(("POST", "/api/v1/runs/r1/steer"), routes)

    def check_pin_commands(self):
        from arete import paths

        with self.serve_at([]):
            paths.record_dir("r1").mkdir(parents=True)
            for command, code in (("pin r1", 0), ("unpin r1", 0), ("unpin r1", 1), ("pin missing", 1), ("pin", 2)):
                self.assertEqual(self.cli(f"record {command}")[0], code, command)

    def test_invocation_write_keys(self):
        self.in_subprocess("check_invocation_write_keys")

    def check_invocation_write_keys(self):
        import hashlib

        from arete import client

        sent = []

        def response(request):
            if request.path == "/api/meta":
                return 200, {"instance": {"id": "keys"}}, {}
            sent.append((request.path, request.headers["Idempotency-Key"]))
            return 200, {"task": "t-1234", "state": "queued", "run": ""}, {}

        with self.stub(response):
            long_key = "a" * 128
            with mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": long_key}):
                code, lines, err = self.cli("task start --repo replay-sample work")
                self.assertEqual(code, 0, (lines, err))
                self.assertIn(long_key, err)
                with client.invocation(), redirect_stderr(StringIO()):
                    client.start_task({"repo": "replay-sample", "prompt": "work"})
                    client.call("POST", "/api/v1/tasks", {"repo": "replay-sample", "body": "work"})
            with mock.patch.dict(os.environ, {"ARETE_IDEMPOTENCY_KEY": "another-key"}), client.invocation():
                client.call("POST", "/api/v1/tasks")
                client.call("POST", "/api/v1/tasks")
        self.assertEqual(sent, [
            ("/api/tasks", long_key),
            ("/api/tasks", long_key),
            ("/api/v1/tasks", hashlib.sha256((long_key + ":2").encode()).hexdigest()),
            ("/api/v1/tasks", "another-key"),
            ("/api/v1/tasks", "another-key:2"),
        ])


class HookUploadTests(unittest.TestCase):
    aliases = ("first.example.test", "second.example.test")
    route = "/api/v1/hook-events"

    def setUp(self):
        from arete import codebase

        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.servers = {}
        self.enterContext(mock.patch.dict(os.environ, {client.ENV: ""}))
        self.enterContext(mock.patch.object(codebase, "load"))
        self.enterContext(mock.patch.object(paths, "ISOLATED", False))
        self.enterContext(mock.patch.object(client, "connect", side_effect=self.connect))
        self.enterContext(mock.patch.object(api_http, "log_request"))

    def connect(self, where, seconds=client.CONNECT_SECONDS):
        self.assertIn(where.host, self.aliases)
        root, server = self.servers[where.port]
        connection = HTTPConnection(where.host, where.port, timeout=seconds)
        request, response = BytesIO(), BytesIO()
        connection.sock = mock.Mock(sendall=request.write, makefile=lambda *_: response)
        getresponse = connection.getresponse

        def reply():
            request.seek(0)
            wire = mock.Mock(makefile=lambda *_: request, sendall=response.write)
            with self.state_at(root):
                dashboard.Handler(wire, (api_check.ADDRESS, 0), server)
            response.seek(0)
            return getresponse()

        connection.getresponse = reply
        return connection

    def serve_at(self, name):
        root = self.root / name
        template = paths.DB_FILE
        with self.state_at(root):
            paths.STATE_DIR.mkdir()
            instance = util.digest(name)[:32]
            with closing(sqlite3.connect(template)) as src, closing(sqlite3.connect(paths.DB_FILE)) as dest:
                src.backup(dest)
                with dest:
                    dest.executemany("UPDATE schema_meta SET value = ? WHERE key = ?",
                                     [(instance, "instance_id"), (instance[:8], "instance_tag")])
                identity = {key: value for key, value in db.metadata(dest).items() if key.startswith("instance_")}
            util.write_json(paths.backups_dir() / "instance.json", identity)
            principals.seed_owner(api_check.LOGIN)
        port = 8000 + len(self.servers)
        self.servers[port] = (root, SimpleNamespace(node=SimpleNamespace(names=set(self.aliases)),
                                                    logins=SimpleNamespace(of=lambda _: api_check.LOGIN)))
        return root, port

    def state_at(self, root):
        cfg, state = root / "config", root / "state"
        cfg.mkdir(parents=True, exist_ok=True)
        return mock.patch.multiple(paths, CONFIG_DIR=cfg, CONFIG_FILE=cfg / "config.toml", STATE_DIR=state,
                                   DB_FILE=state / "arete.db", EVENTS_FILE=state / "events.jsonl",
                                   LOCKS_DIR=state / "locks", RUNS_DIR=state / "runs",
                                   LOG_DIR=root / "logs", WORKTREES_DIR=root / "worktrees")

    @contextmanager
    def cli_at(self, root, host, port):
        with self.state_at(root):
            paths.CONFIG_FILE.write_text(f'[server]\nurl = "http://{host}:{port}"\n')
            yield

    def invoke(self, *argv, stdin="", code=0):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", StringIO(stdin)):
            self.assertEqual(cli.main(list(argv)), code, (out.getvalue(), err.getvalue()))
        return out.getvalue(), err.getvalue()

    def command(self, *argv, stdin=""):
        out, err = self.invoke(*argv, stdin=stdin)
        self.assertEqual(err, "")
        return [json.loads(line) for line in out.splitlines()]

    def hook(self, detail, *, session_id=""):
        self.command("hook", stdin=json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                                               "tool_input": {"skill": detail}, "session_id": session_id}))

    def recorded(self, root):
        path = root / "state" / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_hostname_aliases_share_the_upload_checkpoint(self):
        source = self.root / "client"
        target, port = self.serve_at("target")
        with mock.patch.object(client, "send_hook_batch", wraps=client.send_hook_batch) as send:
            with self.cli_at(source, self.aliases[0], port):
                self.hook("first")
                self.assertEqual(self.recorded(target), [])
                self.assertEqual([row["detail"] for row in self.command("events", "--raw")], ["first"])
            with self.cli_at(source, self.aliases[1], port):
                self.hook("second")
                self.assertEqual([row["detail"] for row in self.command("events", "--raw")], ["first", "second"])
        first, second = [call.args[0] for call in send.call_args_list]
        self.assertEqual([[row["detail"] for row in batch["events"]] for batch in (first, second)],
                         [["first"], ["second"]])
        self.assertGreater(second["offset"], first["offset"])
        self.assertEqual([row["detail"] for row in self.recorded(target)], ["first", "second"])

    def test_reciprocal_serves_do_not_upload_ingested_events(self):
        a, a_port = self.serve_at("a")
        b, b_port = self.serve_at("b")
        with self.cli_at(a, self.aliases[1], b_port):
            self.hook("from-a")
            source_id = tool_events.local_instance()
            self.assertIsNotNone(source_id)
        for _ in range(3):
            with self.cli_at(a, self.aliases[1], b_port):
                self.command("events", "--raw")
            with self.cli_at(b, self.aliases[0], a_port):
                self.command("events", "--raw")
        original, = self.recorded(a)
        self.assertEqual(self.recorded(b), [{**original, tool_events.INGEST_MARK: source_id}])
        self.assertNotIn(tool_events.INGEST_MARK, original)

    def test_resent_key_leaves_the_ingested_log_unchanged(self):
        target, port = self.serve_at("target")
        with self.cli_at(self.root / "client", self.aliases[0], port):
            event = {"ts": util.iso(), "kind": "skill", "cls": "invoked"}
            body = {"origin": "a" * 32, "offset": 0, "events": [event]}
            with client.invocation():
                first = client.call("POST", self.route, body, "hook-replay")
                before = self.recorded(target)
                response = client.request("POST", self.route, body, key="hook-replay")
                self.assertEqual(response.getheader("Idempotent-Replayed"), "true")
                self.assertEqual(client.decoded(response, "POST", self.route), first)
            self.assertEqual(first["received"], 1)
            self.assertEqual(before, [{**event, tool_events.INGEST_MARK: body["origin"]}])
            self.assertEqual(self.recorded(target), before)

    def test_ingest_remembers_each_offset_when_batches_arrive_out_of_order(self):
        target, port = self.serve_at("target")
        with self.cli_at(self.root / "client", self.aliases[0], port), client.invocation():
            later = {"ts": util.iso(), "kind": "skill", "cls": "invoked", "detail": "later"}
            earlier = {**later, "detail": "earlier"}
            body = {"origin": "a" * 32, "stream": "b" * 64, "offset": 200, "events": [later]}
            first = {**body, "offset": 0, "events": [earlier]}
            for batch, key in ((body, "batch-later"), (first, "batch-earlier"),
                               (first, "earlier-again"), (body, "later-again")):
                self.assertEqual(client.call("POST", self.route, batch, key)["received"], 1)
            for key, code in (("batch-earlier", 422), ("changed-earlier", 409)):
                with self.assertRaises(client.Failed) as error:
                    client.call("POST", self.route, {**first, "events": [later]}, key)
                self.assertEqual(error.exception.code, code)
        self.assertEqual([row["detail"] for row in self.recorded(target)], ["later", "earlier"])

    def test_ingest_validates_every_row_before_appending(self):
        target, port = self.serve_at("target")
        with self.cli_at(self.root / "client", self.aliases[0], port), client.invocation():
            valid = {"ts": util.iso(), "kind": "skill", "cls": "invoked", "detail": "valid"}
            invalid_rows = ({**valid, "detail": ["array"]}, {**valid, "ts": "tomorrow"}, {"kind": "skill"}, [])
            for i, invalid in enumerate(invalid_rows):
                body = {"origin": "a" * 32, "offset": 0, "events": [valid, invalid]}
                with self.assertRaises(client.Failed) as error:
                    client.call("POST", self.route, body, f"invalid-{i}")
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(error.exception.answer["code"], "invalid_input")
                self.assertIn("input.events[1]", error.exception.answer["error"])
                self.assertEqual(self.recorded(target), [])
                self.assertFalse((target / "state" / "hook-ingest.json").exists())

    def test_null_session_hooks_upload_and_retry_with_the_same_body_and_key(self):
        source = self.root / "client"
        target, port = self.serve_at("target")
        send_batch = client.send_hook_batch

        def lost_reply(body, key, deadline):
            send_batch(body, key, deadline)
            raise client.Broken({"error": "lost reply"})

        with self.cli_at(source, self.aliases[0], port):
            self.hook("pending", session_id=None)
            with mock.patch.object(client, "send_hook_batch", side_effect=lost_reply) as dropped:
                out, _ = self.invoke("events", "--raw", code=1)
            self.assertEqual(json.loads(out)["error"], "lost reply")
            self.assertEqual([row["detail"] for row in self.recorded(target)], ["pending"])
            self.command("hook", stdin=json.dumps({"hook_event_name": "StopFailure", "tool_name": None,
                                                   "session_id": None, "error_details": "retained"}))
            self.hook("later", session_id="session-2")
            with mock.patch.object(client, "send_hook_batch", wraps=send_batch) as send:
                listed = self.command("events", "--raw")
            self.assertEqual(listed, self.recorded(source))
            self.assertEqual(self.command("events", "--raw"), listed)
        retry, later = [call.args[:2] for call in send.call_args_list]
        self.assertEqual(dropped.call_args.args[:2], retry)
        self.assertNotEqual(retry[1], later[1])
        self.assertEqual([(row["detail"], row["session"]) for row in self.recorded(target)],
                         [("pending", None), ("retained", None), ("later", "session-2")])

    def test_infinite_hours_includes_rotated_and_current_history_locally_and_remotely(self):
        source, _ = self.serve_at("source")
        target, port = self.serve_at("target")
        with self.state_at(source):
            self.hook("old")
            old = {**self.recorded(source)[0], "ts": "2000-01-01T00:00:00Z"}
            paths.EVENTS_FILE.write_text(json.dumps(old) + "\n")
            tool_events.rotate_events(30)
            self.hook("recent")
            recent, = self.recorded(source)
            self.assertEqual(self.command("events", "--raw", "--hours", "inf"), [old, recent])
            self.assertEqual(self.command("events", "--raw"), [recent])
        with self.cli_at(source, self.aliases[0], port):
            self.assertEqual(self.command("events", "--raw", "--hours", "inf"), [old, recent])
            self.assertEqual(self.command("events", "--raw"), [recent])
        self.assertEqual([row["detail"] for row in self.recorded(target)], ["old", "recent"])

    def test_doctor_is_offline_and_preserves_state_with_a_pending_upload(self):
        source, _ = self.serve_at("source")
        target, port = self.serve_at("target")

        def files():
            return {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}

        with self.cli_at(source, self.aliases[0], port):
            self.hook("pending")
            before = files()
            out = StringIO()
            with mock.patch.object(client, "connect", side_effect=AssertionError("doctor contacted serve")) as connect:
                with redirect_stdout(out):
                    cli.main(["doctor"])
            connect.assert_not_called()
            self.assertIn("quick_check ok", out.getvalue())
            self.assertEqual(files(), before)
        self.assertEqual(self.recorded(target), [])

    def test_install_and_dry_runs_leave_pending_events_and_the_checkpoint_alone(self):
        from arete import install

        source = self.root / "client"
        target, port = self.serve_at("target")
        with self.cli_at(source, self.aliases[0], port):
            self.hook("uploaded")
            self.command("events", "--raw")
            self.hook("pending")
            checkpoint, = (paths.STATE_DIR / "hook-uploads").glob("*.json")
            before = checkpoint.read_bytes()
            client.connect.reset_mock()
            with mock.patch.object(install, "install", return_value=0) as install_serve:
                self.invoke("install")
            install_serve.assert_called_once_with(dry_run=False, platform="")
            out, _ = self.invoke("install", "--dry-run", "--platform", "linux")
            self.assertIn("[Unit]\nDescription=arete serve", out)
            with mock.patch.object(pipeline, "run_issue", return_value=0) as run_issue:
                self.invoke("run", "sample", "7", "--dry-run")
            run_issue.assert_called_once_with("sample", "7", dry_run=True, force=False)
            client.connect.assert_not_called()
            self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual([row["detail"] for row in self.recorded(target)], ["uploaded"])

    def test_only_serve_writes_upload_pending_events_during_other_commands(self):
        source = self.root / "client"
        target, port = self.serve_at("target")
        with self.cli_at(source, self.aliases[0], port):
            self.hook("first")
            self.assertEqual(self.command("task", "list"), [])
            self.assertEqual(self.recorded(target), [])
            self.assertEqual(list(paths.STATE_DIR.glob("hook-*.json")), [])
            self.assertFalse((paths.STATE_DIR / "hook-uploads").exists())
            out, err = self.invoke("oncall", "off")
            self.assertIn("wakes and rechecks are off", out)
            self.assertEqual(err, "")
            self.assertEqual([row["detail"] for row in self.recorded(target)], ["first"])

            self.hook("second")
            with mock.patch.object(tasks, "start", return_value={"queued": True}) as start:
                out, err = self.invoke("task", "run", "sample", "7", "--key", "run-seven")
            start.assert_called_once()
            self.assertEqual(json.loads(out), {"queued": True})
            self.assertEqual(err, "Idempotency-Key: run-seven\n")
        self.assertEqual([row["detail"] for row in self.recorded(target)], ["first", "second"])


class WaitReconnectTests(unittest.TestCase):
    def test_wait_resumes_after_stream_drop_and_status_502(self):
        from arete import client, codebase

        step = {"event": "step_end", "seq": 1, "step": "plan"}
        report = {"event": "report", "seq": 2, "status": "done"}
        responses = iter((
            (b"200 OK", json.dumps(step).encode() + b"\n"),
            (b"502 Bad Gateway", b'{"error":"bad gateway"}'),
            (b"200 OK", json.dumps(report).encode() + b"\n"),
        ))
        requests = []

        def connect(_):
            status, body = next(responses)
            transport = mock.Mock()
            transport.makefile.return_value = BytesIO(b"HTTP/1.0 " + status + b"\r\n\r\n" + body)
            response = HTTPResponse(transport)
            response.begin()
            conn = mock.Mock(sock=transport)
            conn.request.side_effect = lambda method, path, *_: requests.append((method, path))
            conn.getresponse.return_value = response
            return conn

        where = client.Server("http", "127.0.0.1", 80, "127.0.0.1")
        output = StringIO()
        with (mock.patch.object(codebase, "load"), mock.patch.object(client, "server", return_value=where),
              mock.patch.object(client, "connect", side_effect=connect),
              mock.patch.object(client, "RETRY_SECONDS", 0), redirect_stdout(output)):
            self.assertEqual(cli.main(["wait", "r1"]), 0, output.getvalue())

        self.assertEqual(list(map(json.loads, output.getvalue().splitlines())), [step, report])
        self.assertEqual(requests, [
            ("GET", "/api/runs/r1/events?from=0&follow=1"),
            ("GET", "/api/runs/r1"),
            ("GET", "/api/runs/r1/events?from=1&follow=1"),
        ])


class DashboardSnapshotTests(unittest.TestCase):
    def test_event_client_surface(self):
        web = Path(__file__).resolve().parents[1] / "arete" / "web"
        source = (web / "ui-events.js").read_text() + (web / "ui-live.js").read_text()
        for name in ("EventSource", "navigator.locks", "BroadcastChannel", "Last-Event-ID", "reset",
                     "/api/v1/events", "timeout"):
            self.assertIn(name, source)
        for name in ("innerHTML", "insertAdjacentHTML", "eval"):
            self.assertNotIn(name, source)

    def test_snapshot_and_event_behavior(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("ui_events.mjs").resolve())],
            capture_output=True, text=True, timeout=8, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class VerifyFaultTests(unittest.TestCase):
    board_route = "/api/v1/screen?view=board"

    def setUp(self):
        self.login = "fixture@example.test"
        self.actor = principals.seed_owner(self.login)
        self.marker = paths.STATE_DIR.parent / ".arete-owned"
        self.spec = paths.STATE_DIR / verify_faults.SPEC
        self.addCleanup(self.marker.unlink, missing_ok=True)
        self.addCleanup(self.spec.unlink, missing_ok=True)
        self.enterContext(mock.patch.dict(os.environ, {"ARETE_STATE_DIR": str(paths.STATE_DIR)}))

    def mark_scratch(self):
        self.marker.write_bytes(verify_faults.MARKER + b"\n")

    def command(self, *args):
        with redirect_stderr(StringIO()) as err, redirect_stdout(StringIO()):
            status = cli.main(["verify-fault", *args])
        return status, err.getvalue()

    def fault(self, *args):
        self.assertEqual(self.command(*args), (0, ""))

    def endpoint(self):
        handlers = dashboard.handlers()
        address = "127.0.0.1"
        node = SimpleNamespace(names={address})
        logins = SimpleNamespace(of=lambda _: self.login)
        server = SimpleNamespace(node=node, logins=logins)
        return handlers.open, server

    @contextmanager
    def client(self, endpoint):
        handler, server = endpoint
        browser, app = socketpair()
        browser.settimeout(3)

        def handle():
            with app:
                handler(app, ("127.0.0.1", 0), server)

        thread = threading.Thread(target=handle, daemon=True)
        thread.start()
        with closing(HTTPConnection("127.0.0.1")) as client:
            client.sock = browser
            yield client
        thread.join(timeout=1)

    def request(self, endpoint, method, path, *, body=None, headers=(), raw=False):
        headers = dict(headers)
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if method == "POST":
            headers["X-Arete"] = "1"
        with self.client(endpoint) as client:
            client.request(method, path, body, headers)
            with client.getresponse() as result:
                return result.status, result.read() if raw else json.load(result)

    def arm_board(self):
        self.fault("arm", "--route", self.board_route, "--status", "404", "--count", "2")

    def initial_cursor(self, endpoint):
        status, data = self.request(endpoint, "GET", "/api/v1/events?timeout=0", headers={"Accept": "application/json"})
        self.assertEqual(status, 200)
        return data["next"]

    @contextmanager
    def stream(self, endpoint, cursor=None):
        headers = {"Accept": "text/event-stream"}
        if cursor is not None:
            headers["Last-Event-ID"] = cursor
        with self.client(endpoint) as client:
            client.request("GET", "/api/v1/events", headers=headers)
            with client.getresponse() as result:
                self.assertEqual(result.status, 200)
                self.assertEqual(result.readline(), b"retry: 2000\n")
                self.assertEqual(result.readline(), b"\n")
                yield result

    def read_event(self, stream):
        position = stream.readline().removeprefix(b"id: ").strip().decode()
        data = json.loads(stream.readline().removeprefix(b"data: "))
        self.assertEqual(stream.readline(), b"\n")
        self.assertEqual(position, data["id"])
        return data

    def test_cli_refuses_and_dashboard_ignores_planted_spec_without_wrapper_marker(self):
        status, reason = self.command("clear")
        self.assertEqual(status, 1)
        self.assertIn("ARETE_STATE_DIR", reason)
        self.assertIn("verification sandbox wrapper", reason)
        self.assertFalse(self.spec.exists())

        self.mark_scratch()
        self.arm_board()
        saved = self.spec.read_bytes()
        self.marker.unlink()
        self.assertEqual(self.command("clear"), (status, reason))

        endpoint = self.endpoint()
        status, body = self.request(endpoint, "GET", self.board_route)
        self.assertEqual(status, 200)
        self.assertIn("view", body)
        self.assertEqual(self.spec.read_bytes(), saved)

        self.mark_scratch()
        with tempfile.TemporaryDirectory(dir=paths.STATE_DIR.parent) as folder:
            link = Path(folder) / "scratch"
            link.symlink_to(paths.STATE_DIR.parent, target_is_directory=True)
            with mock.patch.dict(os.environ, {"ARETE_STATE_DIR": str(link / "state")}):
                self.assertEqual(self.command("clear")[0], 1)
        self.assertEqual(self.spec.read_bytes(), saved)

    def test_board_fault_is_consumed_twice_across_unrelated_updates(self):
        self.mark_scratch()
        self.enterContext(mock.patch.object(util, "now", return_value=util.now()))
        route = self.board_route
        other = "/api/v1/screen?view=runs"
        endpoint = self.endpoint()
        before = self.request(endpoint, "GET", route)
        self.assertEqual(before[0], 200)
        self.arm_board()
        self.assertEqual(self.request(endpoint, "GET", other)[0], 200)
        first = self.request(endpoint, "GET", route)
        self.assertEqual((first[0], first[1]["code"]), (404, "not_found"))
        explicit = '{ "fault": "armed" }\n'
        self.fault("arm", "--route", other, "--status", "503", "--body", explicit)
        self.assertEqual(self.request(endpoint, "GET", route), first)
        self.assertEqual(self.request(endpoint, "GET", route), before)
        self.assertEqual(self.request(endpoint, "GET", other, raw=True), (503, explicit.encode()))
        self.assertEqual(self.request(endpoint, "GET", other)[0], 200)
        self.arm_board()
        self.fault("clear")
        self.assertEqual(self.request(endpoint, "GET", route), before)

    def start_run(self, run):
        with principals.acting(self.actor):
            run_store.insert_run(run, {"kind": "pipeline", "started": util.iso(), "status": "running"})

    def test_default_conflict_matches_contract_and_sets_dashboard_stale_notice(self):
        self.mark_scratch()
        run = "fault-conflict-run"
        route = f"/api/runs/{run}/stop"
        self.fault("arm", "--route", route, "--status", "409", "--count", "1")
        self.start_run(run)
        current = run_store.run_get(run)
        status, raw = self.request(self.endpoint(), "POST", route, body={"base_version": current["version"]}, raw=True)
        self.assertEqual(status, 409)
        schema = next(error["schema"] for error in contract.ERRORS if error["code"] == "version_conflict")
        problem = json.loads(raw)
        contract.validate(problem, schema)
        self.assertEqual(problem["current"], {k: v for k, v in current.items() if k in contract.RUN["properties"]})
        self.assertEqual(run_store.run_get(run), current)

        result = subprocess.run(["node", "--input-type=module", "-e", """
            import assert from "node:assert/strict";
            import { readFileSync } from "node:fs";
            import { prepareWrite, sendWrite } from "./arete/web/ui-write.js";
            import { staleWrite } from "./arete/web/ui-notice.js";

            const [route, status] = process.argv.slice(1);
            const body = readFileSync(0);
            const { current } = JSON.parse(body);
            globalThis.fetch = async () => new Response(body, { status: Number(status) });
            await assert.rejects(sendWrite(prepareWrite(route, {}, current)), { status: 409, conflict: true, current });
            assert.equal(staleWrite.value, "This item changed. Review the latest version before trying again.");
        """, route, str(status)], input=raw, capture_output=True, timeout=8, check=False,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_default_conflict_on_run_addressed_routes_returns_persisted_current(self):
        self.mark_scratch()
        run = "fault-read-run"
        with principals.acting(self.actor):
            run_store.insert_run(run, {"kind": "session", "started": util.iso(), "status": "running"})
            chat_id = chats_store.chat_add("fixture")
            chats_store.chat_link_session(chat_id, run, None)
        endpoint = self.endpoint()
        schema = next(error["schema"] for error in contract.ERRORS if error["code"] == "version_conflict")
        routes = (
            ("GET", "/api/log/fault%2Dread-run"),
            ("GET", f"/api/feed/{run}?after=0"),
            ("GET", f"/api/v1/screen?view=run&id={run}"),
            ("GET", f"/api/v1/line-stream?run={run}&after=0"),
            ("GET", f"/api/v1/steps?run={run}"),
            ("GET", f"/api/v1/steps/1?run={run}"),
            ("POST", f"/api/sessions/{run}/send"),
            ("POST", f"/api/sessions/{run}/stop"),
            ("GET", f"/api/chats/{chat_id}/sessions/{run}/report"),
            ("POST", f"/api/chats/{chat_id}/sessions/{run}/stop"),
        )
        for method, route in routes:
            with self.subTest(route=route):
                self.fault("arm", "--route", route, "--status", "409")
                with principals.acting(self.actor):
                    run_store.note(run, summary=f"Updated after arming {route}")
                current = run_store.run_get(run)
                body = {} if method == "POST" else None
                status, problem = self.request(endpoint, method, route, body=body)
                self.assertEqual(status, 409)
                contract.validate(problem, schema)
                self.assertEqual(problem["current"], {k: v for k, v in current.items() if k in contract.RUN["properties"]})

        route = f"/api/feed/{run}"
        explicit = '{ "fault": "armed" }\n'
        self.fault("arm", "--route", route, "--status", "409", "--body", explicit)
        self.assertEqual(self.request(endpoint, "GET", route, raw=True), (409, explicit.encode()))

    def test_default_event_conflict_returns_persisted_current_for_one_run(self):
        self.mark_scratch()
        run = "fault-stream-run"
        other = "fault-stream-other"
        self.start_run(run)
        self.start_run(other)
        endpoint = self.endpoint()
        conflict = next(error for error in contract.ERRORS if error["code"] == "version_conflict")
        selectors = (
            (f"run={run}", True),
            (f"subject=run%3A{run}", True),
            (f"run={other}&run={run}", True),
            (f"run={run}&subject=run:{run}", True),
            (f"subject=run:{run},run:{other}", False),
            (f"run={run}&subject=run:{other}", False),
        )
        for path in ("/api/v1/events", "/api/events"):
            for selector, selects_run in selectors:
                route = f"{path}?{selector}"
                with self.subTest(route=route):
                    self.fault("arm", "--route", route, "--status", "409")
                    with principals.acting(self.actor):
                        run_store.note(run, summary=f"Updated after arming {route}")
                    status, problem = self.request(endpoint, "GET", route)
                    self.assertEqual(status, 409)
                    contract.validate(problem, conflict["schema"])
                    current = conflict["example"]["current"]
                    if selects_run:
                        current = {k: v for k, v in run_store.run_get(run).items() if k in contract.RUN["properties"]}
                    self.assertEqual(problem["current"], current)

    def test_drop_closes_open_streams_and_reconnect_resumes_after_last_event(self):
        self.mark_scratch()
        endpoint = self.endpoint()
        cursor = self.initial_cursor(endpoint)
        with self.stream(endpoint, cursor) as first, self.stream(endpoint, cursor) as second:
            self.start_run("fault-drop-first")
            last = self.read_event(first)["id"]
            self.assertEqual(self.read_event(second)["id"], last)
            self.fault("drop-streams")
            self.assertEqual(first.read(), b"")
            self.assertEqual(second.read(), b"")
        status, polled = self.request(endpoint, "GET", "/api/v1/events?timeout=0",
                                      headers={"Accept": "application/json", "Last-Event-ID": last})
        self.assertEqual((status, polled), (200, {"events": [], "next": last, "reset": None}))
        with self.stream(endpoint, last) as reconnected:
            self.fault("clear")
            self.start_run("fault-drop-second")
            self.assertEqual(self.read_event(reconnected)["subject"], "run:fault-drop-second")
            self.fault("drop-streams")
            self.assertEqual(reconnected.read(), b"")

    def test_drop_after_heartbeat_reconnects_from_the_last_received_id(self):
        self.mark_scratch()
        clock = mock.Mock(wraps=event_stream.time)
        clock.monotonic.return_value = 0
        envelope = events.envelope

        def make_beat_due(conn, row, epoch):
            data = envelope(conn, row, epoch)
            if data["subject"] == "run:fault-before-beat":
                clock.monotonic.return_value = 15
            return data

        with (mock.patch.object(event_stream, "time", clock),
              mock.patch.object(events, "envelope", side_effect=make_beat_due)):
            endpoint = self.endpoint()
            with self.stream(endpoint, self.initial_cursor(endpoint)) as stream:
                self.start_run("fault-before-beat")
                before_beat = self.read_event(stream)
                last = stream.readline().removeprefix(b"id: ").strip().decode()
                self.assertEqual(stream.readline(), b"event: serve.beat\n")
                self.assertEqual(json.loads(stream.readline().removeprefix(b"data: "))["kind"], "serve.beat")
                self.assertEqual(stream.readline(), b"\n")
                self.fault("drop-streams")
                self.assertEqual(stream.read(), b"")
            with self.stream(endpoint, last) as reconnected:
                self.start_run("fault-after-beat")
                self.assertEqual(self.read_event(reconnected)["subject"], "run:fault-after-beat")
                self.fault("drop-streams")
                self.assertEqual(reconnected.read(), b"")
            self.assertEqual(last, before_beat["id"])

    def test_cursorless_stream_delivers_event_queued_after_opening_frame_once(self):
        self.mark_scratch()
        run = "fault-before-first-scan"
        self.start_run(run)
        endpoint = self.endpoint()
        controls = endpoint[0].verify_controls
        poll = controls.poll
        queued = threading.Event()

        def wait_for_queued_event(route="", query=""):
            if not route and not queued.wait(timeout=3):
                raise TimeoutError("event was not queued before the first stream tick")
            return poll(route, query)

        with mock.patch.object(controls, "poll", wait_for_queued_event), self.stream(endpoint) as stream:
            try:
                self.fault("event", "--kind", "run.ended", "--run", run)
                queued.set()
                ended = self.read_event(stream)
                self.assertEqual((ended["kind"], ended["subject"]), ("run.ended", f"run:{run}"))

                following = "fault-after-first-scan"
                self.start_run(following)
                self.assertEqual(self.read_event(stream)["subject"], f"run:{following}")
            finally:
                self.fault("drop-streams")
                queued.set()
            self.assertEqual(stream.read(), b"")

    def test_run_ended_is_delivered_through_the_real_event_log(self):
        self.mark_scratch()
        run = "fault-event-run"
        self.start_run(run)
        endpoint = self.endpoint()
        with self.stream(endpoint, self.initial_cursor(endpoint)) as stream:
            with principals.acting(self.actor):
                settled = run_store.settle(run, {"status": "failed", "finished": util.iso()})
            real = self.read_event(stream)
            self.assertEqual(real["kind"], "run.ended")
            self.fault("event", "--kind", "run.ended", "--run", run)
            with mock.patch.object(verify_faults, "write_spec", side_effect=OSError("acknowledgment failed")):
                synthetic = self.read_event(stream)
            self.assertGreater(synthetic["seq"], real["seq"])
            for field in real.keys() - {"seq", "at", "id", "via"}:
                self.assertEqual(synthetic[field], real[field], field)
            self.assertEqual(run_store.run_get(run), settled)
            self.assertEqual(self.initial_cursor(endpoint), synthetic["id"])
            self.assertEqual(json.loads(self.spec.read_bytes())["events"], [])
            self.fault("drop-streams")
            self.assertEqual(stream.read(), b"")


class ApiCheckTests(unittest.TestCase):
    serial = True
    class_timeout = 20  # Hosted macOS reached streaming but exhausted the unit-test budget.

    def test_api_check(self):
        with tempfile.TemporaryDirectory(prefix="arete-api-cli-") as temp:
            root = Path(temp).resolve()
            command = api_check.child_command(versioned_api_fixture.SOURCE, "from arete.cli import main; sys.exit(main())",
                                              "api", "check")
            result = subprocess.run(command, cwd=root, env=api_check.isolated_env(root),
                                    timeout=max(CHILD_HANG_TIMEOUT, api_check.CHECK_TIMEOUT + 5), check=False)
            self.assertEqual(result.returncode, 0, "the API smoke command failed; see its scenario output above")


class ApiCompatibilityTests(unittest.TestCase):
    serial = True

    def test_api_diff(self):
        versioned_api_fixture.evolution_temp()


class VersionedApiTests(unittest.TestCase):
    def setUp(self):
        self.actor = principals.seed_owner("fixture@example.test")
        self.repo = config.Repo("fixture", Path("."), github="fixture/repo", tracker="local")
        self.cfg = config.Config(repos=[self.repo])
        self.handler = SimpleNamespace(body=mock.Mock(), headers={"Idempotency-Key": self.id()},
                                       send=lambda *args: args, log_error=mock.Mock(), write_receipt=None,
                                       idempotent_replayed=False, keyed_delivery=False)

    def request(self, body):
        self.handler.body.return_value = body
        with principals.acting(self.actor), mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                mock.patch.object(tasks, "begin", side_effect=lambda cfg, repo, tid, **kw:
                                  tasks.answer(tid, repo.name, "", "queued", "capacity")):
            return dashboard.Handler.keyed_start(self.handler)

    def test_keyed_replay(self):
        body = {"repo": "fixture", "prompt": "Add kumquat"}
        first = self.request(body)
        self.assertEqual(first[0], 200)
        replayed = self.request({"prompt": "Add kumquat", "repo": "fixture"})
        self.assertEqual(replayed, first)
        self.assertTrue(self.handler.idempotent_replayed)
        with self.assertRaises(idempotency.Reused):
            self.request({**body, "prompt": "Changed"})
        with db.reader() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        saved = claims.lookup(self.actor, self.id())
        self.assertEqual(bytes(saved["response"]), first[1])
        self.assertEqual(claims.target(saved)[0], "fixture")
        self.handler.headers["Idempotency-Key"] = "handled-error"
        refused = self.request({"repo": "unknown", "prompt": "Add kumquat"})
        self.assertEqual(refused[0], 400)
        self.assertEqual(self.request({"repo": "unknown", "prompt": "Add kumquat"}), refused)
        with self.assertRaises(idempotency.Reused):
            self.request(body)

    def test_keyed_concurrency(self):
        sha = idempotency.fingerprint("POST", "/api/tasks", {}, {"repo": "fixture", "prompt": "Add kumquat"})
        with idempotency.owning(self.actor, "in-flight-key", sha):
            with self.assertRaises(idempotency.InFlight), idempotency.owning(self.actor, "in-flight-key", sha):
                self.fail("duplicate admitted")
            with self.assertRaises(idempotency.Reused), idempotency.owning(self.actor, "in-flight-key", "changed"):
                self.fail("changed request admitted")
            with idempotency.owning("another-actor", "in-flight-key", sha):
                pass
        with idempotency.owning(self.actor, "in-flight-key", sha):
            pass
        self.assertEqual(idempotency.ACTIVE, {})
        for key in ("", "short", "a" * 129, "bad key-572", "nonascii-é", "newline\n"):
            with self.subTest(key=key), self.assertRaises(control.Refused), idempotency.owning(self.actor, key, sha):
                self.fail("invalid key admitted")

    def test_versioned_read_surface(self):
        self.with_server(self.check_read_surface)

    def test_transport_and_publication_compatibility(self):
        self.with_server(self.check_transport)

    def test_restart_drains_without_discarding_an_admitted_publication(self):
        self.with_server(self.check_publication_drain)

    def with_server(self, exercise):
        address = "127.0.0.1"
        node = SimpleNamespace(names={address})
        logins = SimpleNamespace(of=lambda _: "fixture@example.test")
        with mock.patch("socket.getfqdn", return_value=address):
            server = tailnet.Server(address, 0, node, logins, False, dashboard.Handler, dashboard.Redirect)
        with server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                exercise(server.server_port)
            finally:
                server.shutdown()
                thread.join()

    def test_publication_recovers_remote_urls(self):
        self.cfg.arete_repo = self.repo.name
        source = triage.GitHubSource(self.repo.github, [self.repo])
        ticket = triage.Candidate("fixture#7", "7", "Question", gh.issue_url(self.repo.github, 7), "old")
        triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                         questions=["What happened?"]))
        chat_id = chats_store.chat_add(self.repo.name)
        chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Draft", draft_body="Chat issue body")
        approval_emit = {"type": "ticket", "item": {"repo": self.repo.name, "title": "Delivery",
                                                   "body": "Approval issue body", "labels": []}}
        approval_id = approvals_store.add_approval("fixture", "", approval_emit, "File the delivery")
        pr_item = {"repo": self.repo.github, "branch": "fixture/head", "base": "main", "workdir": ".",
                   "title": "Delivery PR", "body": "Approval PR body", "labels": [], "draft": True}
        pr_id = approvals_store.add_approval("fixture", "", {"type": "pr", "item": pr_item}, "File the PR")
        remote = {"comments": [], "issues": [], "pulls": []}
        searches = []
        labeled = []
        pushed = []
        comment_url = f"{ticket.url}#issuecomment-1"
        pr_url = f"https://github.com/{self.repo.github}/pull/42"

        def outside_tx():
            self.assertIsNone(getattr(db._OWNER, "active", None))

        def lookup(args):
            outside_tx()
            self.assertEqual(args[2:], ["--paginate", "--slurp"])
            endpoint, _, query = args[1].partition("?")
            kind = endpoint.split("/")[-1]
            prefix = f"repos/{self.repo.github}/issues/7" if kind == "comments" else f"repos/{self.repo.github}"
            self.assertEqual(endpoint, f"{prefix}/{kind}")
            if kind == "pulls":
                self.assertRegex(query, r"head=fixture%3Afixture%2F(?:head|retry)")
            searches.append(kind)
            return [remote[kind].copy()]

        def comment(repo, number, body, *, return_url, single_attempt):
            outside_tx()
            self.assertEqual((repo, number, return_url, single_attempt), (self.repo.github, 7, True, True))
            remote["comments"].append({"body": body, "html_url": comment_url})
            return comment_url

        def issue(repo, title, body, labels=(), *, single_attempt=False):
            outside_tx()
            self.assertEqual(repo, self.repo.github)
            self.assertEqual(single_attempt, title != "Legacy draft")
            number = len(remote["issues"]) + 20
            remote["issues"].append({"body": body, "html_url": gh.issue_url(repo, number)})
            return number

        def pull(repo, head, base, title, body, labels, *, draft, single_attempt):
            outside_tx()
            self.assertEqual((repo, base, draft, single_attempt), (self.repo.github, "main", True, True))
            self.assertIn(head, ("fixture/head", "fixture/retry"))
            url = pr_url if head == "fixture/head" else f"https://github.com/{repo}/pull/43"
            remote["pulls"].append({"body": body, "html_url": url})
            return url

        def label(row):
            outside_tx()
            labeled.append(row["ref"])

        def push(args, *unused, **kw):
            outside_tx()
            pushed.append(args)

        settle = approvals_store.settle_approval

        def settle_until_disconnect(ident, status, *args, **kwargs):
            if status != "failed":
                return settle(ident, status, *args, **kwargs)
            return False

        forbidden = AssertionError("credential lookup or external transport")
        with ExitStack() as patches:
            for patch in (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")),
              mock.patch.object(triage, "sources", return_value=[source]),
              mock.patch.object(source, "approve", side_effect=label),
              mock.patch.object(screening, "screen", return_value=None),
              mock.patch.object(screening, "held", return_value=False),
              mock.patch.object(chat.routing, "metered", return_value=False),
              mock.patch.object(dashboard, "start_poll_without_failing_the_send", return_value="Poll queued."),
              mock.patch.object(dashboard.api_http, "log_request"),
              mock.patch.object(accounts, "read", side_effect=forbidden),
              mock.patch.object(jira.Client, "from_env", side_effect=forbidden),
              mock.patch("urllib.request.urlopen", side_effect=forbidden),
              mock.patch.object(gh, "gh", side_effect=forbidden),
              mock.patch.object(gh, "gh_json", side_effect=lookup),
              mock.patch.object(gh, "issue", return_value={"labels": []}),
              mock.patch.object(gh, "issue_comment", side_effect=comment),
              mock.patch.object(gh, "issue_create", side_effect=issue),
              mock.patch.object(gh, "pr_create", side_effect=pull),
              mock.patch.object(util, "git", side_effect=push)):
                patches.enter_context(patch)
            read = patches.enter_context(mock.patch.object(source, "ticket",
                                                          return_value=triage.TicketText("", "Ticket", "", "old")))
            post = self.publication_post
            cases = [
                ("triage", "/api/triage/fixture%237/send", "/api/triage/{ref}/send", {"ref": "fixture%237"},
                 {"questions": ["What happened?"], "answers": ["It broke."], "repo": self.repo.name},
                 "comments", comment_url, "triage:fixture#7"),
                ("chat", f"/api/chats/{chat_id}/file", "/api/chats/{n}/file", {"n": str(chat_id)},
                 {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}, "issues",
                 gh.issue_url(self.repo.github, 20), f"chat:{chat_id}"),
                ("approval", f"/api/approvals/{approval_id}", "/api/approvals/{id}", {"id": str(approval_id)},
                 {"approve": True}, "issues", gh.issue_url(self.repo.github, 21), f"approval:{approval_id}"),
                ("approval-pr", f"/api/approvals/{pr_id}", "/api/approvals/{id}", {"id": str(pr_id)},
                 {"approve": True}, "pulls", pr_url, f"approval:{pr_id}"),
            ]
            for name, path, route, params, body, kind, url, subject in cases:
                with self.subTest(publication=name):
                    key = f"fixture-key-{name}"
                    before = len(remote[kind])
                    with (mock.patch.object(idempotency.Publication, "record", side_effect=RuntimeError("cut off")),
                          mock.patch.object(approvals_store, "settle_approval", side_effect=settle_until_disconnect)):
                        self.assertEqual(post(path, body, key)[0], 500)
                    self.assertEqual(len(remote[kind]), before + 1)
                    sha = idempotency.fingerprint("POST", route, params, body)
                    tag = db.schema_metadata()["instance_tag"]
                    self.assertIn(f"arete:{tag}:k-{sha[:8]}", remote[kind][-1]["body"].splitlines())
                    self.assertEqual(claims.lookup(self.actor, key)["status"], 0)
                    if name.startswith("approval"):
                        self.assertEqual(approvals_store.approval(int(params["id"]))["status"], "approved")
                    status, headers, answer = post(path, body, key)
                    self.assertEqual(status, 200, answer)
                    saved = claims.lookup(self.actor, key)
                    self.assertEqual(headers["Arete-Seq"], claims.receipt(saved["last_seq"])["event_seq"])
                    with db.reader() as conn:
                        event = conn.execute("SELECT subject, data FROM events_log WHERE seq=?",
                                             (saved["last_seq"],)).fetchone()
                    self.assertEqual((event["subject"], json.loads(event["data"])["url"]), (subject, url))
                    status, headers, again = post(path, body, key)
                    self.assertEqual((status, headers["Idempotent-Replayed"], again), (200, "true", answer))
                    self.assertEqual(len(remote[kind]), before + 1)

            self.assertEqual(post(f"/api/approvals/{approval_id}", {"approve": False}, "fixture-key-approval")[0], 422)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(labeled, [ticket.ref, ticket.ref])
            self.assertEqual(pushed, [["push", "-u", "origin", "fixture/head"]])
            self.assertEqual(searches, ["comments", "comments", "issues", "issues", "issues", "issues", "pulls", "pulls"])
            self.assertIsNone(triage.board_row(ticket.ref))
            self.assertEqual(triage_store.triage_approval(ticket.ref)["repo"], self.repo.name)
            self.assertEqual([m["text"] for m in chats_store.chat_messages(chat_id)],
                             ["Drafted", f"Filed {gh.issue_url(self.repo.github, 20)}"])
            self.assertEqual(approvals_store.approval(approval_id)["result"], gh.issue_url(self.repo.github, 21))
            self.assertEqual(approvals_store.approval(pr_id)["result"], pr_url)
            with self.subTest(publication="triage drift before first confirmed publication"):
                remote["comments"].clear()
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                                 questions=["What happened?"]))
                path = "/api/triage/fixture%237/send"
                payload = {"questions": ["What happened?"], "answers": ["A different answer."], "repo": self.repo.name}
                key = "fixture-triage-stale"
                with mock.patch.object(gh, "issue_comment", side_effect=gh.GhError("could not reach fixture")):
                    self.assertEqual(post(path, payload, key)[0], 502)
                self.assertEqual(claims.lookup(self.actor, key)["status"], 0)
                read.return_value = triage.TicketText("", "Edited", "", "new")
                labels_before = labeled.copy()
                self.assertEqual(post(path, payload, key)[0], 409)
                self.assertEqual(remote["comments"], [])
                self.assertEqual(labeled, labels_before)
                self.assertIsNotNone(triage.board_row(ticket.ref))
            with self.subTest(publication="label-only triage send recovers after its own timestamp change"):
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=1, code=1, repo=self.repo.name))
                path, key = "/api/triage/fixture%237/send", "fixture-triage-label-recovery"
                payload = {"questions": [], "answers": [], "repo": self.repo.name}
                read.return_value = triage.TicketText("", "Ticket", "", "old")
                with mock.patch.object(idempotency.Publication, "record", side_effect=RuntimeError("cut off")):
                    self.assertEqual(post(path, payload, key)[0], 500)
                wanted = triage.labels_to_add(triage.board_row(ticket.ref))
                read.return_value = triage.TicketText("", "Ticket", "", "after-labels", tuple(wanted))
                labels_before = labeled.copy()
                read.return_value.labels += ("agent:hold",)
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 409, answer)
                self.assertIsNotNone(triage.board_row(ticket.ref))
                read.return_value.labels = tuple(wanted)
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 200, answer)
                self.assertEqual(labeled, labels_before)
                self.assertIsNone(triage.board_row(ticket.ref))
            with self.subTest(publication="found comment cannot resume on a newer triage entry"):
                triage_store.triage_put(triage.row(source, ticket, updated="old", ready=0, code=1, repo=self.repo.name,
                                                 questions=["What happened?"]))
                read.return_value = triage.TicketText("", "Ticket", "", "old")
                payload = {"questions": ["What happened?"], "answers": ["Preserve the new row."], "repo": self.repo.name}
                path, key = "/api/triage/fixture%237/send", "fixture-triage-revised-board"
                with mock.patch.object(source, "approve", side_effect=gh.GhError("cannot add labels")):
                    self.assertEqual(post(path, payload, key)[0], 502)
                triage_store.triage_put(triage.row(source, ticket, updated="new", ready=0, code=1, repo=self.repo.name,
                                                 questions=["New question"]))
                labels_before = labeled.copy()
                status, _, answer = post(path, payload, key)
                self.assertEqual(status, 409, answer)
                self.assertEqual(labeled, labels_before)
                self.assertEqual(triage.board_row(ticket.ref)["questions"], ["New question"])
            with self.subTest(publication="upstream tracker failure is kept and redacted"):
                ident = approvals_store.add_approval("fixture", "", approval_emit, "File again")
                marker = "synthetic provider diagnostic should stay private"
                with mock.patch.object(gh, "gh_json", side_effect=gh.GhError(marker)):
                    status, _, failure = post(f"/api/approvals/{ident}", {"approve": True}, "fixture-approval-upstream")
                self.assertNotIn(marker.encode(), failure)
                self.assertEqual(status, 502)
                self.assertIs(json.loads(failure)["kept"], True)
                saved = approvals_store.approval(ident)
                self.assertEqual(saved["status"], "failed")
                self.assertNotIn(marker, saved["result"])
            with self.subTest(publication="identical chat content under a new key"):
                chats_store.chat_say(chat_id, "arete", "Drafted again", draft_title="Draft", draft_body="Chat issue body")
                before = len(remote["issues"])
                path = f"/api/chats/{chat_id}/file"
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                status, _, answer = post(path, body, "fixture-distinct-chat-key")
                self.assertEqual(status, 200, answer)
                self.assertEqual(len(remote["issues"]), before + 1)
                self.assertNotEqual(remote["issues"][-1]["body"], remote["issues"][0]["body"])
            with self.subTest(publication="chat reply lost before the marked issue is visible"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Delayed draft", draft_body="Delayed body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-lost-chat-reply"
                before = len(remote["issues"])
                hidden = []

                def lost_reply(*args, **kwargs):
                    issue(*args, **kwargs)
                    hidden.append(remote["issues"].pop())
                    raise gh.GhUnsure("synthetic lost reply")

                with mock.patch.object(gh, "issue_create", side_effect=lost_reply) as outbound:
                    self.assertEqual(post(path, body, key)[0], 502)
                    retry_status, _, retry = post(path, body, key)
                    self.assertIn(retry_status, (409, 502), retry)
                    self.assertEqual(outbound.call_count, 1)
                self.assertEqual(len(hidden), 1)
                self.assertEqual(chat.chat_row(chat_id)["draft_title"], "Delayed draft")
                remote["issues"].extend(hidden)
                self.assertEqual(post(path, body, key)[0], 200)
                self.assertEqual(len(remote["issues"]), before + 1)
            with self.subTest(publication="approval prerequisites fail before the create attempt"):
                retry_pr = {"type": "pr", "item": {**pr_item, "branch": "fixture/retry"}}
                for item, kind, adapter, prerequisite in ((approval_emit, "issues", gh, "ensure_label"),
                                                          (retry_pr, "pulls", util, "git")):
                    item = {**item, "item": {**item["item"], "labels": ["fixture-label"]}}
                    ident = approvals_store.add_approval("fixture", "", item, "Recover the prerequisite failure")
                    path, key = f"/api/approvals/{ident}", f"fixture-prerequisite-{ident}"
                    before = len(remote[kind])
                    with mock.patch.object(gh, "ensure_label", return_value=""), \
                            mock.patch.object(adapter, prerequisite, side_effect=gh.GhError("prerequisite failed")):
                        status, _, failure = post(path, {"approve": True}, key)
                        self.assertEqual(status, 502, failure)
                    self.assertEqual(approvals_store.approval(ident)["status"], "failed")
                    self.assertEqual(len(remote[kind]), before)
                    with mock.patch.object(gh, "ensure_label", return_value=""):
                        status, _, answer = post(path, {"approve": True}, key)
                    self.assertEqual(status, 200, answer)
                    self.assertEqual(len(remote[kind]), before + 1)
                    self.assertEqual(approvals_store.approval(ident)["status"], "done")
            with self.subTest(publication="lost approval reply cannot authorize another send under a fresh key"):
                ident = approvals_store.add_approval("fixture", "", approval_emit, "Send once")
                path, key = f"/api/approvals/{ident}", "fixture-approval-lost-reply"
                hidden = []

                def lost_issue(*args, **kwargs):
                    issue(*args, **kwargs)
                    hidden.append(remote["issues"].pop())
                    raise gh.GhUnsure("reply not received")

                with mock.patch.object(gh, "issue_create", side_effect=lost_issue):
                    self.assertEqual(post(path, {"approve": True}, key)[0], 502)
                self.assertEqual(approvals_store.approval(ident)["status"], "failed")
                before = len(remote["issues"])
                self.assertEqual(post(path, {"approve": True}, "fixture-fresh-approval-key")[0], 409)
                self.assertEqual(self.publication_post(path, {"approve": True}, None)[0], 409)
                self.assertEqual(len(remote["issues"]), before)
                remote["issues"].extend(hidden)
                self.assertEqual(post(path, {"approve": True}, key)[0], 200)
                self.assertEqual(len(remote["issues"]), before + 1)
            with self.subTest(publication="discarded chat draft cannot be sent after an initial lookup failure"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Discarded", draft_body="Old body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-chat-discarded"
                with mock.patch.object(gh, "find_issue_marker", side_effect=gh.GhError("lookup not available")):
                    self.assertEqual(post(path, body, key)[0], 502)
                chat.drop_draft(chat_id)
                before = len(remote["issues"])
                self.assertEqual(post(path, body, key)[0], 409)
                self.assertEqual(len(remote["issues"]), before)
            with self.subTest(publication="chat discard wins while prior publication is being looked up"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Will discard", draft_body="Pending body")
                body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
                path, key = f"/api/chats/{chat_id}/file", "fixture-chat-discard-at-lookup"
                before = len(remote["issues"])

                def discard(*unused):
                    chat.drop_draft(chat_id)
                    return ""

                with mock.patch.object(gh, "find_issue_marker", side_effect=discard):
                    self.assertEqual(post(path, body, key)[0], 409)
                saved = claims.lookup(self.actor, key)
                self.assertFalse(json.loads(saved["response"]).get("send_started"))
                self.assertEqual(len(remote["issues"]), before)
            with self.subTest(publication="chat without a bound draft"):
                chats_store.chat_say(chat_id, "arete", "Drafted again", draft_title="New draft", draft_body="Different body")
                path = f"/api/chats/{chat_id}/file"
                before = len(remote["issues"])
                status, _, _ = post(path, {}, "fixture-chat-without-draft")
                self.assertIn(status, (409, 422))
                self.assertEqual(len(remote["issues"]), before)
                self.assertEqual(chat.chat_row(chat_id)["draft_title"], "New draft")
                self.assertIsNone(claims.lookup(self.actor, "fixture-chat-without-draft"))
            with self.subTest(publication="legacy unkeyed filing without representation headers"):
                chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Legacy draft", draft_body="Legacy body")
                before = len(remote["issues"])
                status, _, answer = self.publication_post(f"/api/chats/{chat_id}/file", None, None)
                self.assertEqual(status, 200, answer)
                self.assertEqual(len(remote["issues"]), before + 1)
                self.assertEqual(remote["issues"][-1]["body"], "Legacy body")

    def test_keyed_tracker_adapters_send_at_most_once(self):
        marker = "arete:fixture:k-12345678.234567890abcdef1234567890abcdef1"
        with mock.patch.object(gh, "gh", side_effect=gh.GhUnsure("lost reply")) as send, \
                mock.patch.object(gh, "recovery_lookup", side_effect=AssertionError("internal retry")):
            writes = (
                lambda: gh.issue_create(self.repo.github, "Fixture", marker, single_attempt=True),
                lambda: gh.issue_comment(self.repo.github, 7, marker, return_url=True, single_attempt=True),
                lambda: gh.pr_create(self.repo.github, "fixture", "main", "Fixture", marker, single_attempt=True),
            )
            for write in writes:
                send.reset_mock()
                with self.assertRaises(gh.GhUnsure):
                    write()
                send.assert_called_once()
                self.assertIn(marker, send.call_args.kwargs["input"].splitlines())

        client = jira.Client("https://jira.example.test", "fixture@example.test", "not-a-credential")
        with mock.patch.object(jira._opener, "open", side_effect=TimeoutError("lost reply")) as send:
            with self.assertRaises(jira.JiraUnsure):
                client.comment("TEST-7", f"Approved answers\n\n{marker}", return_url=True, single_attempt=True)
            send.assert_called_once()
            request = send.call_args.args[0]
            self.assertEqual((request.method, request.selector), ("POST", "/rest/api/3/issue/TEST-7/comment"))
            self.assertIn(marker, json.dumps(json.loads(request.data)["body"]))
        pages = [BytesIO(b'{"total":0,"comments":[]}'), BytesIO(json.dumps({"total": 1, "comments": [
            {"id": "42", "body": f"Approved answers\n\n{marker}"}]}).encode())]
        with mock.patch.object(jira._opener, "open", side_effect=pages) as observe:
            self.assertEqual(client.find_comment_marker("TEST-7", marker), "")
            self.assertEqual(client.find_comment_marker("TEST-7", marker),
                             "https://jira.example.test/browse/TEST-7?focusedCommentId=42")
            self.assertEqual([call.args[0].method for call in observe.call_args_list], ["GET", "GET"])

    def publication_post(self, path, body, key):
        payload = json.dumps(body).encode() if body is not None else b""
        headers = {"X-Arete": "1"}
        if payload:
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(payload))})
        if key:
            headers["Idempotency-Key"] = key
        return self.wire_request("POST", path, headers, payload)

    def wire_request(self, method, path, headers, payload=b""):
        request = [f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"]
        request.extend(f"{name}: {value}\r\n" for name, value in headers.items())
        incoming = BytesIO(("".join(request) + "\r\n").encode() + payload)
        outgoing = bytearray()
        connection = SimpleNamespace(makefile=lambda *args: incoming, sendall=outgoing.extend, settimeout=lambda _: None)
        server = SimpleNamespace(node=SimpleNamespace(names={"127.0.0.1"}),
                                 logins=SimpleNamespace(of=lambda _: "fixture@example.test"))
        dashboard.Handler(connection, ("127.0.0.1", 0), server)
        response = HTTPResponse(SimpleNamespace(makefile=lambda *args: BytesIO(outgoing)))
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def check_read_surface(self, port):
        def request(path, *, body=None, headers=None, status=200):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                request_headers = {"X-Arete": "1", "Accept": "application/json"}
                if body is not None:
                    request_headers["Content-Type"] = "application/json"
                connection.request("GET" if body is None else "POST", path,
                                   body=None if body is None else json.dumps(body),
                                   headers={**request_headers, **(headers or {})})
                response = connection.getresponse()
                data = json.loads(response.read())
                self.assertEqual(response.status, status, (path, data))
                self.assertEqual(response.getheader("Arete-API"), contract.VERSION)
                if body is None and status == 200 and "seq" in data:
                    self.assertEqual(data["seq"], response.getheader("Arete-Seq"))
                return data, response
            finally:
                connection.close()

        registry, _ = request("/api/v1/contract")
        operations = registry["operations"]
        self.assertEqual(len(operations), 46)
        self.assertEqual(len({op["name"] for op in operations}), 46)
        for op in operations:
            with self.subTest(op=op["name"]):
                contract.validate(op["example"]["input"], op["input"])
                contract.validate(op["example"]["result"], op["result"])
        for problem in registry["errors"]:
            contract.validate(problem["example"], problem["schema"])
        meta, alias = request("/api/meta")
        self.assertEqual(meta, request("/api/v1/meta")[0])
        self.assertEqual(meta["api"]["contract_sha"], registry["contract_sha"])
        self.assertEqual(alias.getheader("Deprecation"), "true")
        with db.tx():
            events.emit("fixture.changed", "fixture:573", cls="derived", data={})
        stream, response = request("/api/events?since=0&timeout=0")
        self.assertEqual(response.getheader("Deprecation"), "true")
        self.assertEqual(response.getheader("Arete-Seq"), stream["next"])
        self.assertEqual(stream["events"][-1]["id"], stream["next"])
        request("/api/v1/missing", status=404)
        request("/api/sidebar", headers={"X-Arete-Client": "mcp/0.9"}, status=400)
        with mock.patch.object(tasks, "start", side_effect=AssertionError("unknown field reached task start")), \
                mock.patch.object(dashboard.api_http, "record_alias", wraps=dashboard.api_http.record_alias) as record:
            refused, _ = request("/api/tasks", body={"colour": "not an input"}, status=422)
            record.assert_not_called()
        self.assertEqual(refused["fields"], ["colour"])
        with mock.patch.object(db, "tx", side_effect=AssertionError("GET opened a write transaction")), \
                mock.patch.object(chat, "recover", side_effect=AssertionError("GET recovered chat files")):
            sidebar, _ = request("/api/sidebar")
            live, _ = request("/api/live")
            request("/api/chats")
            self.assertEqual(sidebar["seq"], live["seq"])
            self.assertEqual(sidebar["running"], len(live["live"]["rows"]["runs"]))
            batch, response = request("/api/v1/batch", body={"requests": [
                {"operation": "screen.get"}, {"operation": "task.list"}, {"operation": "meta.get"}]})
            contract.validate(batch, contract.BY_NAME["batch.run"]["result"])
            self.assertEqual({row["seq"] for row in batch["results"]}, {response.getheader("Arete-Seq")})
        def grace():
            with db.reader() as c:
                return c.execute("SELECT value FROM schema_meta WHERE key = 'versioned_api_grace_deadline'").fetchone()[0]

        def page():
            with self.subTest("first versioned screen"):
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", "/ui.html")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"<!doctype html>", response.read().lower())
                finally:
                    connection.close()

        self.assertEqual(grace(), "")
        page()
        deadline = grace()
        self.assertTrue(deadline)
        with mock.patch.object(db, "tx", side_effect=AssertionError("repeat page opened a write transaction")):
            page()
        self.assertEqual(grace(), deadline)

    def test_board_view(self):
        self.enterContext(mock.patch.object(paths, "DB_FILE", paths.DB_FILE.with_name("board.db")))
        db.migrate()
        principals.seed_owner("fixture@example.test")

        def request(path, *, headers=None, status=200):
            actual, response, raw = self.wire_request("GET", path, {"Accept": "application/json", **(headers or {})})
            self.assertEqual(actual, status, (path, raw))
            return (json.loads(raw) if raw else None), response

        task = task_store.task_add(self.repo.name, "Board fixture", "Board card")
        closed = task_store.task_add(self.repo.name, "Closed fixture", "Closed card")
        task_store.task_set_state(closed, "closed")
        board_url = "/api/v1/screen?view=board"
        before, response = request(board_url)
        self.assertEqual(before["seq"], response["Arete-Seq"])
        view = before["view"]
        listed, _ = request("/api/v1/tasks")
        self.assertEqual(view["tasks"], listed["tasks"])
        self.assertEqual(view["running_ids"], [])
        self.assertEqual({col["state"]: col["count"] for col in view["columns"]},
                         {row["state"]: sum(r["state"] == row["state"] for r in listed["tasks"]) for row in listed["tasks"]})
        headers = {"If-None-Match": response["ETag"]}
        self.assertTrue(headers["If-None-Match"])
        with mock.patch.object(dashboard, "get_sidebar", return_value={**before["sidebar"], "now": "later"}):
            body, unchanged = request(board_url, status=304, headers=headers)
        self.assertIsNone(body)
        self.assertEqual(unchanged["ETag"], headers["If-None-Match"])
        self.assertEqual(unchanged["Arete-Seq"], before["seq"])

        run = f"board-{task}"
        fields = {"kind": "pipeline", "repo": self.repo.name, "issue": task, "status": "running", "started": util.iso()}
        run_store.insert_run(run, fields)
        task_store.task_set_state(task, "running")
        active, _ = request(board_url)
        self.assertEqual(active["view"]["running_ids"], [run])
        self.assertEqual(active["sidebar"]["running"], 1)
        run_store.note(run, summary="Building")
        task_store.task_set_state(task, "pr")
        run_store.settle(run, {"status": "pr", "finished": util.iso()})
        found, _ = request(f"/api/v1/events?since={before['seq']}&timeout=0")
        lifecycle = [(event["kind"], event["data"]["board_task"]["state"]) for event in found["events"]
                     if event["subject"] == f"run:{run}"]
        self.assertEqual(lifecycle, [("run.started", "todo"), ("run.changed", "running"), ("run.ended", "pr")])
        after, response = request(board_url, headers=headers)
        self.assertNotEqual(response["ETag"], headers["If-None-Match"])
        self.assertEqual(next(row["state"] for row in after["view"]["tasks"] if row["id"] == task), "pr")
        with mock.patch.object(dashboard.api, "VIEWS", {k: v for k, v in dashboard.api.VIEWS.items() if k != "board"}):
            request(board_url, status=404)

    def check_transport(self, port):
        def exchange(method, path, headers=(), body=b""):
            raw = [f"{method} {path} HTTP/1.1\r\n", "Host: 127.0.0.1\r\n", "X-Arete: 1\r\n"]
            with create_connection(("127.0.0.1", port), timeout=3) as conn:
                conn.sendall(("".join(raw) + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode() + body)
                conn.shutdown(SHUT_WR)
                response = HTTPResponse(conn)
                response.begin()
                return response.status, response.headers, response.read()

        meta_route, batch_route = "/api/v1/meta", "/api/v1/batch"
        media = ("Content-Type", "application/json")

        def post(path, body, media_header=media):
            return exchange("POST", path, (media_header, ("Content-Length", len(body))), body)

        def problem(response, status, code):
            actual, headers, body = response
            self.assertEqual(actual, status, body)
            self.assertEqual(headers["Content-Type"], "application/problem+json")
            self.assertEqual(headers["Arete-API"], contract.VERSION)
            self.assertEqual(json.loads(body)["code"], code)

        now = [0.0]
        with mock.patch.object(api_http, "RATES", api_http.RateLimits(lambda: now[0])), \
                mock.patch.object(tasks, "start", side_effect=AssertionError("invalid request started a task")):
            problem(exchange("GET", meta_route + "?x=" + "a" * 65536), 414, "uri_too_long")
            for method, route, allow in (("GET", batch_route, "POST"), ("PUT", meta_route, "GET")):
                refused = exchange(method, route)
                problem(refused, 405, "method_not_allowed")
                self.assertEqual(refused[1]["Allow"], allow)
            for headers, body in (((), b""), ((media, ("Content-Length", "bad")), b""),
                                  ((media, ("Content-Length", "0"), ("Content-Length", "0")), b""),
                                  ((media, ("Transfer-Encoding", "chunked")), b""),
                                  ((media, ("Content-Length", "4")), b"{}")):
                with self.subTest(headers=headers):
                    problem(exchange("POST", batch_route, headers, body), 400, "invalid_framing")
            for raw in (b'{"requests":[],"requests":[]}', b'{"x":NaN}', b'{"x":Infinity}',
                        b'{"x":9007199254740992}', b'{"x":' + b"[" * 32 + b"0" + b"]" * 32 + b"}"):
                with self.subTest(raw=raw):
                    problem(post(batch_route, raw), 400, "invalid_json")
            problem(post(batch_route, b"{}", ("Content-Type", "text/plain")), 415, "unsupported_media_type")
            status, _, body = post("/api/tasks", b"{}", ("Content-Type", "text/plain"))
            self.assertEqual((status, json.loads(body)["code"]), (415, "unsupported_media_type"))
            problem(exchange("POST", batch_route, (media, ("Content-Length", api_http.MAX_JSON_BYTES + 1))),
                    413, "body_too_large")
            self.assertEqual(exchange("GET", meta_route, [("Fixture", "x")] * 98)[0], 200)
            problem(exchange("GET", meta_route, [("Fixture", "x")] * 99), 431, "headers_too_large")

            _, headers, _ = exchange("GET", meta_route)
            match = [("If-None-Match", headers["ETag"])]
            status, empty, body = exchange("GET", meta_route, match)
            self.assertEqual((status, body), (304, b""))
            for name in ("Arete-API", "Arete-Seq", "ETag"):
                self.assertEqual(empty[name], headers[name])
            self.assertIsNone(empty["Content-Length"])
            with db.tx() as conn:
                conn.execute("UPDATE schema_meta SET value = cast(value AS INTEGER) + 1 WHERE key = 'restore_epoch'")
            status, newer, _ = exchange("GET", meta_route, match)
            self.assertEqual(status, 200)
            self.assertNotEqual(headers["ETag"], newer["ETag"])
            self.assertNotEqual(headers["Arete-Seq"], newer["Arete-Seq"])

            dashboard.POSTS_IN_FLIGHT.close()
            try:
                for response in (exchange("GET", meta_route), post(batch_route, b"{}")):
                    problem(response, 503, "draining")
                    self.assertEqual(response[1]["Retry-After"], "2")
            finally:
                dashboard.POSTS_IN_FLIGHT.reopen()
            self.assertEqual(exchange("GET", meta_route)[0], 200)

            logged = StringIO()
            raw = b'{"colour":"private-body"}'
            with redirect_stderr(logged):
                exchange("POST", "/api/tasks?fixture=private-query",
                         (media, ("Content-Length", len(raw)), ("Idempotency-Key", "private-key")), raw)
            event = json.loads(logged.getvalue().splitlines()[-1])
            self.assertEqual((event["event"], event["route"], event["status"]), ("api.request", "/api/tasks", 422))
            self.assertNotIn("private-", logged.getvalue())

            with mock.patch.object(dashboard.webhooks, "receive", side_effect=AssertionError("GET webhook dispatched")):
                self.assertEqual(exchange("GET", dashboard.webhooks.HOOK_PATH + "fixture")[0], 405)

            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8WsAAAAASUVORK5CYII=")
            upload = "/api/capture/evidence?name=fixture.png"
            status, _, body = exchange("POST", upload, [("Content-Length", len(png))], png)
            self.assertEqual(status, 200, body)
            mime_status, _, _ = post(upload, png, ("Content-Type", "image/png"))
            self.assertEqual(mime_status, 200)
            evidence = json.loads(body)["evidence"]["id"]
            status, partial, body = exchange("GET", f"/api/capture/evidence/{evidence}", [("Range", "bytes=0-7")])
            self.assertEqual((status, body, partial["Content-Type"]), (206, png[:8], "image/png"))
            stale = headers["Arete-Seq"]
            status, stream_headers, body = exchange("GET", f"/api/v1/events?since={stale}",
                                                    [("Accept", "application/x-ndjson")])
            self.assertEqual((status, stream_headers["Content-Type"], json.loads(body)["kind"]),
                             (200, "application/x-ndjson", "reset"))

            now[0] += 10
            batch = json.dumps({"requests": [{"operation": "meta.get"}] * 50}).encode()
            self.assertEqual(post(batch_route, batch)[0], 200)
            for route in (meta_route, "/api/meta"):
                limit = exchange("GET", route)
                self.assertEqual((limit[0], limit[1]["Retry-After"], json.loads(limit[2])["code"]), (429, "1", "rate_limited"))
            now[0] += .021
            self.assertEqual(exchange("GET", meta_route)[0], 200)
            now[0] += .021
            problem(post(batch_route, b"{"), 400, "invalid_json")
            problem(post(batch_route, b"{"), 429, "rate_limited")

        now = [0.0]
        limits = api_http.RateLimits(lambda: now[0])
        limits.take("one", command=True, cost=60)
        with self.assertRaises(api_http.RateLimited):
            limits.take("one", command=True)
        limits.take("two", command=True)
        now[0] += .051
        limits.take("one", command=True)

    def check_publication_drain(self, port):
        def request(path, body=None, key=""):
            conn = HTTPConnection("127.0.0.1", port, timeout=3)
            headers = {"X-Arete": "1", "Accept": "application/json"}
            if body is not None:
                headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
            try:
                conn.request("GET" if body is None else "POST", path, headers=headers,
                             body=None if body is None else json.dumps(body))
                response = conn.getresponse()
                return response.status, response.headers, json.loads(response.read())
            finally:
                conn.close()

        self.cfg.arete_repo = self.repo.name
        chat_id = chats_store.chat_add(self.repo.name)
        chats_store.chat_say(chat_id, "arete", "Drafted", draft_title="Drain fixture", draft_body="Test the admitted write")
        body = {"draft": chat.draft_fingerprint(chat.chat_row(chat_id))}
        path, key = f"/api/chats/{chat_id}/file", "drain-fixture-key"
        entered, release = threading.Event(), threading.Event()
        responses = []

        def publish(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("the fixture did not release the admitted request")
            return 42

        sender = threading.Thread(target=lambda: responses.append(request(path, body, key)))
        replacement = frozenset({("fixture.py", 2)})
        old = frozenset({("fixture.py", 1)})
        coordinator = object.__new__(serve.Server)
        coordinator.code, coordinator.code_seen = old, replacement
        coordinator.migration_stamp, coordinator.migration_error = replacement, ""
        coordinator.polls, coordinator.oncall = {}, None
        coordinator.job_running = mock.Mock(return_value=False)
        coordinator.sweeping = mock.Mock(return_value=False)
        coordinator.note_once = mock.Mock()

        def busy():
            prior = serve.Server.busy_with(coordinator)
            if not dashboard.POSTS_IN_FLIGHT.draining():
                self.assertEqual(prior, "")
                sender.start()
                self.assertTrue(entered.wait(3))
            else:
                self.assertEqual(prior, "the dashboard is still answering a request")
                refused = request(path, body, "new-request-during-drain")
                self.assertEqual((refused[0], refused[1]["Retry-After"], refused[2]["code"]), (503, "2", "draining"))
            return prior

        coordinator.busy_with = busy
        forbidden = AssertionError("unexpected external transport or restart")
        with (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(config, "load_or_problem", return_value=(self.cfg, "")),
              mock.patch.object(serve, "code_stamp", return_value=replacement),
              mock.patch.object(util, "sh", return_value=SimpleNamespace(returncode=0)),
              mock.patch.object(serve.os, "execv", side_effect=forbidden),
              mock.patch.object(gh, "gh", side_effect=forbidden),
              mock.patch.object(gh, "find_issue_marker", return_value=""),
              mock.patch.object(gh, "issue_create", side_effect=publish) as sends,
              mock.patch.object(accounts, "read", side_effect=forbidden),
              mock.patch.object(chat.routing, "metered", return_value=False)):
            try:
                coordinator.load_new_code()
                coordinator.note_once.assert_called_once_with("new code waits to load: the dashboard is still answering a request")
            finally:
                release.set()
                if sender.ident is not None:
                    sender.join(4)
            self.assertEqual([response[0] for response in responses], [200])
            replay = request(path, body, key)
            self.assertEqual((replay[0], replay[1]["Idempotent-Replayed"]), (200, "true"))
            self.assertEqual(replay[2], responses[0][2])
            sends.assert_called_once()
            self.assertEqual(request("/api/v1/meta")[0], 200)

    def test_keyed_recovery(self):
        with db.reader() as c:
            before = c.execute("SELECT count(*) FROM tasks").fetchone()[0]
            events = c.execute("SELECT count(*) FROM events_log").fetchone()[0]
        with mock.patch.object(claims, "claim", side_effect=RuntimeError("claim failed")):
            answer = self.request({"repo": "fixture", "prompt": "Must roll back"})
        self.assertEqual(answer[0], 500)
        self.assertEqual(json.loads(answer[1])["error"], "The server could not complete the request.")
        with db.reader() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], before)
            self.assertEqual(c.execute("SELECT count(*) FROM events_log").fetchone()[0], events)
        self.assertEqual(claims.lookup(self.actor, self.id())["status"], 500)
        self.assertEqual(self.request({"repo": "fixture", "prompt": "Must roll back"}), answer)


class PrincipalTests(unittest.TestCase):
    def test_principal_gate(self):
        owner = principals.seed_owner("fixture@example.test")
        handler = SimpleNamespace(server=SimpleNamespace(node=SimpleNamespace(names={"fixture.test"}),
                                  logins=SimpleNamespace(of=mock.Mock(return_value="fixture@example.test"))),
                                  client_address=("127.0.0.1", 1), headers={"Host": "fixture.test", "X-Arete": "1"})
        with principals.acting("hand"):
            dashboard.Handler.admit(handler, "POST", "/api/tasks")
            self.assertEqual(principals.current(), owner)
            for host, login, header, status in (("foreign.test", "fixture@example.test", "1", 421),
                                               ("fixture.test", "stranger@example.test", "1", 403),
                                               ("fixture.test", "fixture@example.test", "", 403)):
                handler.headers = {"Host": host, "X-Arete": header}
                handler.server.logins.of.return_value = login
                with self.subTest(status=status), self.assertRaises(dashboard.Refusal) as got:
                    dashboard.Handler.admit(handler, "POST", "/api/tasks")
                self.assertEqual(got.exception.status, status)
        with self.assertRaises(sqlite3.IntegrityError), db.tx() as c:
            c.execute("INSERT INTO principals (id, kind, role, status, name, created) "
                      "VALUES ('u:other', 'human', 'owner', 'active', 'Other', ?)", (util.iso(),))
