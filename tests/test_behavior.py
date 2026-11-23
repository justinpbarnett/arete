import argparse
import ast
import base64
import hmac
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, chdir, closing, contextmanager, redirect_stderr, redirect_stdout
from contextvars import Context
from dataclasses import asdict, replace
from datetime import timedelta
from functools import wraps
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from socket import SHUT_WR, SO_SNDBUF, SOL_SOCKET, create_connection, socket, socketpair
from types import SimpleNamespace
from unittest import mock

from arete import (
    accounts, api, api_check, api_http, backup, chat, cli, client, codebase, config, contract, control, credential_files,
    dashboard, event_stream, events, gh, heartbeat, idempotency, jira, paths, pipeline, principals, record, run_lines,
    sandbox, screening, serve, tailnet, tasks, tool_events, tracker, triage, util, verify_faults, webpush, workspace,
)
from arete.store import approvals as approvals_store
from arete.store import chats as chats_store
from arete.store import db
from arete.store import idempotency as claims
from arete.store import runs as run_store
from arete.store import tasks as task_store
from arete.store import tokens as token_store
from arete.store import triage as triage_store
from tests import support, versioned_api_fixture
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT


def setUpModule():
    support.isolate_live_state()


def fixture_bearer(person):
    with db.tx() as c:
        token_store.ensure_key(c)
        _, text = token_store.issue(person, "Behavior fixture", ["*"])
    return "Bearer " + text


class _WorkspaceChildCase(unittest.TestCase):
    def run_child(self, entry):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(suffix=".noindex"))).resolve()
        env = api_check.isolated_env(root)
        (root / "config/config.toml").write_text("max_parallel = 1\n")
        command = api_check.child_command(Path(__file__).resolve().parents[1], entry)
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                                timeout=CHILD_HANG_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WorkspaceOwnerRecordTests(_WorkspaceChildCase):
    def test_owner_rows_match_legacy_records_and_ownerless_fallback(self):
        self.run_child(r"""
import json
from pathlib import Path
from arete import config, paths, pipeline, record, workspace
from arete.store import db

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
(ws / "sample.txt").write_text("sample\n")
made = {"sample.txt": "old-fingerprint", "\0head api/": "old-head", "\0salt": "old-salt", "\0owner": legacy}
file = workspace.made_record(ws)
file.parent.mkdir()
file.write_text(json.dumps(made))
owner = workspace.WorkspaceOwner.from_row(workspace.made_before(ws)["\0owner"])
assert owner.checkouts == [workspace.Checkout(c.dir, c.github, c.src, c.wt, "")]
assert workspace.owner_of(ws, cfg) == owner
made_owner = pipeline.workspace_owner(run, ws, [c])
assert json.dumps(made_owner.to_row()) == json.dumps(legacy)
run.alone = True
assert pipeline.workspace_owner(run, ws, [c]) == owner
del made["\0owner"]
file.write_text(json.dumps(made))
record.start("pipeline", repo="sample", issue="app-7", status="merged")
record.start("pipeline", repo="sample-app", issue="7", status="merged")
fallback = workspace.owner_of(ws, cfg)
assert fallback == workspace.WorkspaceOwner("", "sample-app", "7", "", owner.checkouts)
workspace.note_made(ws, [c], made_owner)
saved = json.loads(file.read_text())
assert saved["\0owner"] == legacy
assert saved["sample.txt"] == workspace.fingerprint(ws / "sample.txt", saved["\0salt"])
""")


class WorkspaceBackfillTests(_WorkspaceChildCase):
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


class WorkspaceStoreTests(_WorkspaceChildCase):
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
        self.enterContext(mock.patch.object(workspace, "HELD_FILE", state / "held-workspaces.json"))
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
            workspace.note_made(ws, [c])
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
        self.assertFalse(workspace.made_record(merged).exists())
        self.assertFalse(self.registered(merged))
        self.assertFalse(self.registered(gone))
        self.assertTrue(running.exists())
        self.assertEqual(hand.read_text(), "by hand\n")
        held, = workspace.held_summary()["rows"]
        self.assertTrue(held["blocked"])
        self.assertEqual(held["names"], ["hand.txt"])
        self.assertEqual(json.loads(workspace.HELD_FILE.read_text()), [held])
        self.assertIn("hand.txt", held["why"])
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
        self.assertFalse(workspace.made_record(blocked).exists())
        self.assertFalse(closed.exists())
        self.assertTrue(external.exists())
        waiting, = workspace.held_summary()["rows"]
        self.assertFalse(waiting["blocked"])
        self.assertEqual(waiting["names"], [])
        self.assertTrue(waiting["why"].endswith("waits for the ticket"))


class WorkspaceReleaseTests(_WorkspaceWorkflowCase):
    def test_pr_release_records_setup_files_and_removes_the_workspace(self):
        ws, c, run = self.checkout("7", "pr")
        made = workspace.made_before(ws)
        self.assertEqual(made[workspace.OWNER_KEY]["run"], run.id)
        self.assertIn(".venv/", made)
        (c.wt / ".venv/pyvenv.cfg").write_text("changed during the run\n")
        self.assertTrue(pipeline.release_workspace(run, ws, [c]))
        self.assertFalse(ws.exists())
        self.assertFalse(workspace.made_record(ws).exists())
        self.assertFalse(self.registered(ws))
        self.assertFalse((run.dir / "run.log").exists())

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
        self.assertTrue(workspace.made_record(ws).is_file())
        self.assertFalse(self.registered(c.wt))
        outside.unlink()
        self.assertEqual(workspace.remove_workspace(ws, [c]), workspace.Release("removed"))
        self.assertFalse(ws.exists())
        self.assertFalse(workspace.made_record(ws).exists())

    def test_excluded_checkout_keeps_surviving_workspace_and_record(self):
        ws, c, _ = self.checkout("8", "done", part="api")
        saved = workspace.made_record(ws).read_bytes()
        excluded = ws / "closed"
        excluded.mkdir()
        (excluded / "hand.txt").write_text("preserved\n")

        release = workspace.remove_workspace(ws, [c], excluded={excluded})

        self.assertEqual(release.state, "removed")
        self.assertEqual(workspace.made_record(ws).read_bytes(), saved)
        self.assertEqual((excluded / "hand.txt").read_text(), "preserved\n")
        self.assertFalse(self.registered(c.wt))


class WorkspaceBlockerTests(_WorkspaceWorkflowCase):
    def test_removal_preserves_untracked_file_saved_during_sibling_scan(self):
        ws = paths.WORKTREES_DIR / "sample-9"
        api, web = [workspace.Checkout(part, self.repo.github, self.repo.path, ws / part, "main")
                    for part in ("api", "web")]
        for c in (api, web):
            workspace.add_worktree(c, f"arete/9-{c.dir}", "main")
        late = api.wt / "late.txt"
        checkout_blockers = workspace.checkout_blockers
        repo_lock = util.repo_lock
        saved = False

        def save_during_scan(c, made, edits):
            nonlocal saved
            if c is web and not saved:
                late.write_text("saved during the sibling scan\n")
                saved = True
            return checkout_blockers(c, made, edits)

        @contextmanager
        def save_under_lock(src):
            with repo_lock(src), mock.patch.object(workspace, "checkout_blockers", save_during_scan):
                yield

        with mock.patch.object(util, "repo_lock", save_under_lock):
            release = workspace.remove_workspace(ws, [api, web])

        self.assertTrue(saved, "the locked sibling scan never saved the file")
        self.assertTrue(late.is_file(), "the file saved during the sibling scan was removed")
        self.assertEqual(late.read_text(), "saved during the sibling scan\n")
        self.assertEqual(release, workspace.Release("held", ["api/late.txt"]))
        self.assertTrue(self.registered(api.wt))
        self.assertTrue(self.registered(web.wt))

    def test_removal_preserves_checkout_restored_during_sibling_scan(self):
        ws = paths.WORKTREES_DIR / "sample-10"
        api, web = [workspace.Checkout(part, self.repo.github, self.repo.path, ws / part, "main")
                    for part in ("api", "web")]
        for c in (api, web):
            workspace.add_worktree(c, f"arete/10-{c.dir}", "main")
        untracked = api.wt / "hand.txt"
        untracked.write_text("work from the restored checkout\n")
        parked = paths.WORKTREES_DIR.parent / "parked-api"
        api.wt.rename(parked)
        checkout_blockers = workspace.checkout_blockers
        repo_lock = util.repo_lock

        def restore_during_scan(c, made, edits):
            if c is web and parked.exists():
                parked.rename(api.wt)
            return checkout_blockers(c, made, edits)

        @contextmanager
        def restore_under_lock(src):
            with repo_lock(src), mock.patch.object(workspace, "checkout_blockers", restore_during_scan):
                yield

        with mock.patch.object(util, "repo_lock", restore_under_lock):
            release = workspace.remove_workspace(ws, [api, web])

        self.assertFalse(parked.exists(), "the locked sibling scan never restored the checkout")
        self.assertTrue(untracked.is_file(), "the restored checkout's untracked file was removed")
        self.assertEqual(untracked.read_text(), "work from the restored checkout\n")
        self.assertEqual(release, workspace.Release("held", ["api/hand.txt"]))
        self.assertTrue(self.registered(api.wt))
        self.assertTrue(self.registered(web.wt))

    def test_removal_names_files_outside_and_inside_checkouts(self):
        ws, c, _ = self.checkout("9", "done", part="api")
        second = workspace.Checkout("web", c.github, c.src, ws / "web", "main")
        workspace.add_worktree(second, "arete/9-web", "main")
        (ws / "hand.txt").write_text("outside\n")
        (c.wt / ".venv/another.cfg").write_text("ignored\n")
        (second.wt / "local.txt").write_text("untracked\n")

        release = workspace.remove_workspace(ws, [c, second])

        self.assertEqual(release.state, "held")
        self.assertEqual(set(release.held_by), {"hand.txt", "api/.venv/", "web/local.txt"})
        self.assertTrue(self.registered(c.wt))
        self.assertTrue(self.registered(second.wt))

        scratch_record = sandbox.AUTHORITY / "held.json"
        with mock.patch.object(workspace, "scratch_left", return_value=[ws, c.wt, scratch_record]):
            held = workspace.remove_workspace(ws, [c, second])
        self.assertEqual(held.state, "held")
        self.assertEqual(set(held.held_by), {*release.held_by, str(ws), str(c.wt), str(scratch_record)})


class WorkspaceSymlinkTests(_WorkspaceWorkflowCase):
    def assert_late_link_held(self, ws, checkouts, link, destination, other, *, scan_name, sibling=None):
        checkout = workspace.Checkout("", self.repo.github, self.repo.path, other, "main")
        workspace.add_worktree(checkout, f"arete/{ws.name}-other", "main")
        untracked = other / "hand.txt"
        untracked.write_text("keep the other checkout's work\n")
        scan = getattr(workspace, scan_name)
        repo_lock = util.repo_lock

        def link_after_scan(*args):
            names = scan(*args)
            if (sibling is None or args[0] is sibling) and not link.is_symlink():
                link.symlink_to(destination, target_is_directory=True)
            return names

        @contextmanager
        def link_under_lock(src):
            with repo_lock(src), mock.patch.object(workspace, scan_name, link_after_scan):
                yield

        def guard_git(args, *a, **kw):
            self.assertNotEqual(args[:3], ["worktree", "remove", "--force"])
            return util.git(args, *a, **kw)

        with (mock.patch.object(util, "repo_lock", link_under_lock),
              mock.patch.object(workspace, "git", guard_git)):
            release = workspace.remove_workspace(ws, checkouts)

        self.assertEqual(link.readlink(), destination)
        self.assertEqual(release.state, "held")
        self.assertIn(str(link), release.held_by)
        self.assertTrue(self.registered(other))
        self.assertEqual(untracked.read_text(), "keep the other checkout's work\n")

    def test_removal_holds_checkout_linked_during_sibling_scan(self):
        ws = paths.WORKTREES_DIR / "sample-parts"
        api, web = [workspace.Checkout(part, self.repo.github, self.repo.path, ws / part, "main")
                    for part in ("api", "web")]
        workspace.add_worktree(web, "arete/sibling", "main")
        other = paths.WORKTREES_DIR / "other"

        self.assert_late_link_held(ws, [api, web], api.wt, other, other,
                                  scan_name="checkout_blockers", sibling=web)

    def test_removal_holds_workspace_root_linked_after_outside_scan(self):
        for part in ("", "api"):
            with self.subTest(part=part):
                ws = paths.WORKTREES_DIR / f"sample-{part or 'single'}"
                other_root = paths.WORKTREES_DIR / f"other-{part or 'single'}"
                checkouts = [workspace.Checkout(name, self.repo.github, self.repo.path, ws / name, "main")
                             for name in ((part, "web") if part else (part,))]

                self.assert_late_link_held(ws, checkouts, ws, other_root, other_root / part,
                                          scan_name="outside_blockers")


class ConfigRegistryTests(unittest.TestCase):
    class_timeout = CHILD_HANG_TIMEOUT
    golden = Path(__file__).parent / "golden" / "config_errors.json"
    config_errors = {
        "dashboard flag": "[dashboard]\nhttps = 1",
        "server URL": "[server]\nurl = false",
        "factory folder": "[factory]\nadws = false",
        "tier keys": '[tiers.default.fast]\nz = 1\na = 2',
        "tier route": '[tiers.default.fast]\nrunner = "bad"\nmodel = "sonnet"',
        "step names": "[steps.z]\n[steps.a]",
        "step keys": "[steps.build]\nz = 1\na = 2",
        "step count": "[steps.build]\ntries = true",
        "step amount": "[steps.build]\nbudget_usd = nan",
        "step amount bool": "[steps.build]\nbudget_usd = true",
        "step flag": "[steps.build]\nstructured_output = 1",
        "step cleanup flag": "[steps.build]\ndirectory_cleanup = 1",
        "step alias": "[steps.implement]\n[steps.build]",
        "kind names": "[kinds.z]\n[kinds.a]",
        "kind keys": "[kinds.feat]\nz = 1",
        "kind step names": "[kinds.feat.steps.bad]",
        "pricing number": "[pricing.models.sonnet]\ninput = false",
        "routing keys": "[routing]\nz = 1",
        "routing flag": "[routing]\nfallback = 0",
        "routing modes": "[routing]\npayment_modes = []",
        "capability keys": "[routing.capabilities.sonnet]\nz = 1",
        "capability context": "[routing.capabilities.sonnet]\ncontext = true",
        "capability flag": "[routing.capabilities.sonnet]\nstructured_output = 0",
        "paid keys": "[routing.paid]\nz = 1",
        "paid number": "[routing.paid]\nmax_input = inf",
        "jira state": '[jira.transitions]\nmerged = "Done"\ndone = "Done"',
        "jira number": '[jira]\nauto_per_refresh = "bad"',
        "jev question": '[jev.screen_lines]\nz = 1',
        "jev weight": '[jev.risk_weights]\nz = 1',
        "jev number": '[jev]\nconfidence = "bad"',
        "github app table": 'github_app = true',
        "github app ID": '[github_app]\ninstallation_id = "bad"',
        "repo tracker": '[[repo]]\nname = "extra"\ntracker = "bad"',
        "job label": "[[job]]\nkeepalive = 1",
        "probe number": '[[probe]]\nname = "bad"\ncommand = ["test"]\ntimeout = "bad"',
        "oncall keys": "[oncall]\nz = 1",
        "oncall flag": "[oncall]\nenabled = 1",
        "oncall whole": "[oncall]\nmax_attempts = true",
        "oncall amount": "[oncall]\nincident_budget_usd = 0",
        "oncall inclusive amount": "[oncall]\nkeep_days = nan",
        "oncall source keys": "[[oncall.source]]\nz = 1",
        "oncall source path": "[[oncall.source]]\npath = true",
        "replay keys": "[replay]\nz = 1",
        "mcp keys": "[mcp.example]\nz = 1",
        "mcp command": "[mcp.example]\ncommand = false",
        "research keys": "[research]\nz = 1",
        "research planner": "[research]\nplanner = 1",
        "research search": "[research]\nsearch = 1",
        "research synthesis": "[research]\nsynthesis = 1",
        "research audit": "[research]\naudit = 1",
        "research workers": "[research]\nsearch_workers = 9",
        "research share": "[research]\nsearch_share = 1",
        "ask keys": "[ask]\nz = 1",
        "ask count": "[ask]\nmax_per_run = -1",
        "ask flag": "[ask]\nsettle = 1",
        "ask expiration": "[ask]\nexpire_days = inf",
        "ask reminder": "[ask]\nremind_hours = true",
        "triage keys": "[triage]\nz = 1",
        "triage auto": "[triage]\nauto = 1",
        "triage route fixes": "[triage]\nroute_fixes = 1",
        "triage count": "[triage]\nauto_per_refresh = true",
        "retention keys": "[retention]\nz = 1",
        "retention number": "[retention]\ndays = nan",
        "backup keys": "[backup]\nz = 1",
        "backup destination": "[backup]\ncopy_to = false",
        "chat keys": "[chat]\nz = 1",
        "chat count": "[chat]\ncontext_tokens = true",
        "chat number": "[chat]\nbudget_usd = inf",
        "environment keys": "[environment]\nz = 1",
        "environment names": "[environment]\npass = false",
        "document keys": "[document]\nz = 1",
        "document flag": "[document]\nenabled = 1",
        "capture keys": "[capture]\nz = 1",
        "capture flag": "[capture]\npublic_evidence = 1",
        "capture order": "[capture]\nevidence_branch = false\npublic_evidence = 1\nvision = 1",
        "ab keys": "[ab.test]\nz = 1",
        "ab step": "[ab.test]\nstep = []",
        "ab kind": '[ab.test]\nstep = "build"\nkind = "bad"',
        "ab share": '[ab.test]\nstep = "build"\nshare = true',
        "ab arm keys": '[ab.test]\nstep = "build"\nshare = 0.25\n[ab.test.arm]\nz = 1',
        "section order": "[capture]\npublic_evidence = 1\n[ask]\nsettle = 1\n[routing]\nfallback = 1",
        "step order": "[steps.build]\ndirectory_cleanup = 1\nbudget_usd = 0\ntries = 0",
        "research order": "[research]\nsearch = true\nsynthesis = 1\naudit = 1",
    }
    layer_errors = {
        "check policy keys": "[check_policy]\nz = 1",
        "limits flag": "[limits]\nparallel_checks = 1",
        "limits number": "[limits]\nmax_run_minutes = false",
        "verify publish flag": "[verify]\npublish_evidence = 1",
        "verify patch flag": "[verify]\nallow_patch_gaps = 1",
        "verify number": "[verify]\npreflight_seconds = true",
        "verify order": "[verify]\nallow_patch_gaps = 1\npreflight_seconds = true\npublish_evidence = 1",
        "orchestration keys": "[orchestration]\nz = 1",
        "orchestration number": "[orchestration]\nmax_actions = true",
        "patch policy keys": "[factory.patch]\nz = 1",
        "patch policy number": "[factory.patch]\nmax_lines = true",
        "layer environment keys": "[environment]\nz = 1",
    }
    settings_config = """
[research]
planner = true
search = true
synthesis = true
audit = true
search_workers = 4
search_share = 0.5
[capture]
vision = "capture.md"
evidence_branch = "capture/test"
public_evidence = true
[routing]
payment_modes = ["api_key", "subscription"]
[routing.capabilities.sonnet]
modalities = ["text", "image"]
context = 8000
[routing.floors]
quality = ["opus"]
global = ["haiku"]
[routing.paid]
providers = ["anthropic", "openai"]
max_input = 8
[steps.build]
floor = "quality"
budget_usd = 3.5
[steps.verify]
floor = "global"
[kinds.fix.steps.review]
tries = 2
[oncall]
keep_days = inf
daily_budget_usd = inf
"""
    settings_layer = """
[routing]
fallback = false
payment_modes = ["local", "subscription", "api_key"]
[routing.capabilities.sonnet]
context = 16000
structured_output = true
[routing.capabilities.opus]
context = 40000
[routing.floors]
quality = ["sonnet", "opus"]
[routing.paid]
providers = ["openai"]
max_input = 4
[steps.build]
min_context = 8000
[kinds.feat.steps.build]
timeout_minutes = 40
"""

    @classmethod
    def observe(cls, case):
        with tempfile.TemporaryDirectory(prefix="arete-config-registry-") as directory:
            root = Path(directory).resolve()
            command = api_check.child_command(Path(__file__).resolve().parents[1],
                                              "from tests.test_behavior import ConfigRegistryTests as T; "
                                              "T.run_case(sys.argv[1])", case)
            done = subprocess.run(command, cwd=root, env=api_check.isolated_env(root), capture_output=True,
                                  text=True, timeout=CHILD_HANG_TIMEOUT)
            if done.returncode:
                raise AssertionError(done.stdout + done.stderr)
            return json.loads(done.stdout)

    @classmethod
    def run_case(cls, case):
        from arete import loop_run, sources
        from arete.commands import setup

        with (api_check.offline(), mock.patch.object(pipeline, "spawn_run") as spawn,
              mock.patch.object(loop_run, "launch") as launch, mock.patch.object(sources, "fire") as fire,
              mock.patch.object(serve.Server, "scan_oncall") as scan):
            with redirect_stdout(StringIO()):
                assert setup.cmd_init(SimpleNamespace(force=False)) == 0
            root = Path.cwd()
            lines, _ = api_check.prepare_replay(root)
            base = "\n".join(lines) + "\n"
            layer_dir = root / "sample/layer"
            if case == "errors":
                result = cls.errors(base, layer_dir)
            elif case == "roles":
                result = cls.roles()
            else:
                paths.CONFIG_FILE.write_text(base + cls.settings_config)
                layer_file = layer_dir / "config.toml"
                layer_file.write_text(layer_file.read_text() + cls.settings_layer)
                result = cls.steps_output() if case == "cli" else cls.settings(layer_dir)
            for fake in (spawn, launch, fire, scan):
                fake.assert_not_called()
            print(json.dumps(result))

    @classmethod
    def errors(cls, base, layer_dir):
        def error(name, read):
            try:
                read()
            except (ValueError, TypeError, KeyError) as exc:
                return [type(exc).__name__, str(exc)]
            raise AssertionError(f"expected an error for {name}")

        found = {}
        for name, snippet in cls.config_errors.items():
            # The replay sample already declares [replay].
            paths.CONFIG_FILE.write_text(snippet if name in ("replay keys", "github app table") else base + snippet)
            found[name] = error(name, lambda: config.load(strict=True))
        for name, snippet in cls.layer_errors.items():
            (layer_dir / "config.toml").write_text(snippet)
            found[name] = error(name, lambda: config.load_layer(layer_dir, strict=True))
        paths.CONFIG_FILE.write_text(base + "[research]\nz = 1\nplanner = true\n[steps.a]\n")
        cfg = config.load()
        assert cfg.ignored == ("[steps] a", "[research] z")
        assert cfg.research.planner
        return found

    @staticmethod
    def roles():
        from arete import steps

        for name in ("KEEPS_NO_SESSION", "READ_ONLY", "SEARCHES", "FETCHES", "NO_WEB"):
            assert not hasattr(steps, name), name
        return {name: list(getattr(config, name)) if isinstance(getattr(config, name), tuple)
                else list(getattr(config, name).items())
                for name in ("MODEL_ROLES", "MODEL_FALLBACKS", "INHERITS", "STEP_ROLES", "STEP_NAMES")}

    @staticmethod
    def steps_output():
        with redirect_stdout(StringIO()):
            db.migrate()
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = cli.main(["steps", "--repo", "replay-sample"])
        assert status == 0 and not err.getvalue(), err.getvalue()
        return util.digest(out.getvalue())

    @staticmethod
    def settings(layer_dir):
        from arete import routing, steps

        cfg, layer = config.load(), config.load_layer(layer_dir)
        assert config.Config().routing.payment_modes == config.PAYMENT_MODES
        assert config.RepoLayer(dir=layer_dir).routing.payment_modes is None
        restricted = replace(layer, routing=replace(layer.routing, payment_modes=("local", "subscription")))
        assert config.allowed_payment_modes(cfg, restricted) == ("subscription",)
        local_only = replace(layer, routing=replace(layer.routing, payment_modes=("local",)))
        assert config.allowed_payment_modes(cfg, local_only) == ()
        return {
            "steps": {kind: {step: util.digest(json.dumps(asdict(steps.resolve(cfg, layer, step, kind)), sort_keys=True))
                             for step in config.STEP_NAMES} for kind in ("", *config.KINDS)},
            "research": asdict(cfg.research),
            "capture": {"evidence_branch": cfg.capture.evidence_branch, "public_evidence": cfg.capture.public_evidence},
            "capture_vision": Path(cfg.capture.vision).relative_to(paths.CONFIG_DIR).as_posix(),
            "payment_modes": config.allowed_payment_modes(cfg, layer),
            "capabilities": routing.Discovery(cfg, layer=layer).declared,
            "paid": asdict(config.allowed_paid_routes(cfg, layer)),
            "oncall_accepts_inf": cfg.oncall.keep_days == cfg.oncall.daily_budget_usd == float("inf"),
        }

    def assert_observation(self, case):
        expected = json.loads(self.golden.read_text())[case]
        self.assertEqual(self.observe(case), expected)

    def test_config_errors_and_order(self):
        self.assert_observation("errors")

    def test_roles_and_order(self):
        self.assert_observation("roles")

    def test_steps_output(self):
        self.assert_observation("cli")

    def test_resolved_settings(self):
        self.assert_observation("settings")


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
    CREDENTIAL_COMMANDS = ("login --token-stdin", "logout", "pair")
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

    def problem(self, command, reason, stdin=""):
        code, lines, err = self.cli(command, stdin)
        self.assertEqual(code, 1, (command, lines, err))
        self.assertEqual(len(lines), 1, (command, lines, err))
        self.assertNotIn("Traceback", err)
        answer = json.loads(lines[0])
        self.assertEqual(answer.get("reason") or answer.get("code"), reason, (command, answer))
        return answer

    @staticmethod
    @contextmanager
    def listening(server):
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
        token_store.init_owner()
        token = credential_files.read_client(db.schema_metadata()["instance_tag"])

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
            server.fixture_auth = f"Bearer {token}"
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

    @staticmethod
    def stub_meta(instance):
        return 200, {}, {"Arete-Instance": util.digest(instance)[:32]}


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
        for group, case in ((1, (*self.ONLINE, *self.CREDENTIAL_COMMANDS)), (2, self.READS), (3, self.LOCAL)):
            for command in case:
                words = tuple(command.split())
                match = next((words[:n] for n in (2, 1) if words[:n] in actual), words)
                self.assertNotIn(match, groups)
                groups[match] = group
        self.assertSetEqual(actual, set(groups))
        for command in (*self.ONLINE, *self.ONLINE_VARIANTS, *self.CREDENTIAL_COMMANDS, *self.READS,
                        *self.READ_VARIANTS, *self.LOCAL_VARIANTS, "api diff --check-artifact"):
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
        for command in (*self.ONLINE, *self.ONLINE_VARIANTS, *self.CREDENTIAL_COMMANDS):
            with self.subTest(command=command):
                stdin = "ar_01234567_abcdef01_fixture" if command.startswith("login ") else ""
                self.assertIn(url, self.problem(command, "server_down", stdin)["error"])
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
            if request.path == "/api/v1/meta":
                return self.stub_meta(next(instances))
            return 200, {"task": {"id": "t-1234"}}, {}

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
            headers = {"Authorization": server.fixture_auth, "Idempotency-Key": "existing-key",
                       "Content-Type": "application/json"}
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
            previous = db.schema_metadata()["instance_tag"] + "0" * 24
            metadata = {"instance": {"id": previous}}
            with (mock.patch.object(api, "meta", return_value=metadata),
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
        self.assertEqual(instances, [None] + [previous] * 4)

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
                if not refused and any(method == "POST" and status == 503 for method, _, status, _ in seen):
                    refused.append(True)
                    dashboard.POSTS_IN_FLIGHT.reopen()
                    raise client.Down(client.address(where), "connection refused")
                if where.instance and not refused:
                    dashboard.POSTS_IN_FLIGHT.close()
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
        self.assertIn(("GET", client.META, 503, None), seen)
        self.assertIn(("GET", "/api/meta", 200, None), seen)
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

        with self.stub(lambda request: self.stub_meta("first") if request.path == "/api/v1/meta" else (200, {}, {})):
            self.assertIn("asks", self.problem("answer r1", None)["error"])

        def missing_task(request):
            if request.path == "/api/v1/meta":
                return self.stub_meta("session")
            return 200, {"id": "r1", "runner": "pi", "model": "sample", "device": "local", "cwd": "/fixture",
                         "branch": "", "tools": []}, {}

        with self.stub(missing_task):
            self.assertIn("task", self.problem("session start work", None)["error"])

    def test_follow_preambles_wait_for_valid_feed(self):
        self.in_subprocess("check_follow_preambles_wait_for_valid_feed")

    def check_follow_preambles_wait_for_valid_feed(self):
        feed = {"lines": []}

        def response(request):
            if request.path == "/api/v1/meta":
                return self.stub_meta("follower")
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
            if request.path == "/api/v1/meta":
                return self.stub_meta("reloading")
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
            if request.path == "/api/v1/meta":
                return self.stub_meta("reports")
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
            if request.path == "/api/v1/meta":
                return self.stub_meta("ask-replay")
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
            if request.path == "/api/v1/meta":
                return self.stub_meta("keys")
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
            token_store.init_owner()
        port = 8000 + len(self.servers)
        logins = tailnet.Logins(mock.Mock())
        logins.of = lambda _: api_check.LOGIN
        self.servers[port] = (root, SimpleNamespace(node=SimpleNamespace(names=set(self.aliases)),
                                                    logins=logins, tls=None))
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
        logins = tailnet.Logins(mock.Mock())
        logins.of = lambda _: self.login
        server = SimpleNamespace(node=node, logins=logins, tls=None, authorization=None)
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
            if endpoint[1].authorization is None:
                endpoint[1].authorization = fixture_bearer(self.actor)
            headers["Authorization"] = endpoint[1].authorization
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
        self.authorization = None
        self.repo = config.Repo("fixture", Path("."), github="fixture/repo", tracker="local")
        self.cfg = config.Config(repos=[self.repo])
        self.handler = SimpleNamespace(body=mock.Mock(), headers={"Idempotency-Key": self.id()},
                                       person=self.actor, server=SimpleNamespace(logins=tailnet.Logins(mock.Mock())),
                                       send=lambda *args: args, log_error=mock.Mock(), write_receipt=None,
                                       idempotent_replayed=False, keyed_delivery=False, credential=None)

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
        logins = tailnet.Logins(mock.Mock())
        logins.of = lambda _: "fixture@example.test"
        self.authorization = fixture_bearer(self.actor)
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
        headers = {}
        if payload:
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(payload))})
        if key:
            headers["Idempotency-Key"] = key
        return self.wire_request("POST", path, headers, payload)

    def wire_request(self, method, path, headers, payload=b""):
        request = [f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"]
        if method != "GET":
            if self.authorization is None:
                self.authorization = fixture_bearer(self.actor)
            request.append(f"Authorization: {self.authorization}\r\n")
        request.extend(f"{name}: {value}\r\n" for name, value in headers.items())
        incoming = BytesIO(("".join(request) + "\r\n").encode() + payload)
        outgoing = bytearray()
        connection = SimpleNamespace(makefile=lambda *args: incoming, sendall=outgoing.extend, settimeout=lambda _: None)
        logins = tailnet.Logins(mock.Mock())
        logins.of = lambda _: "fixture@example.test"
        server = SimpleNamespace(node=SimpleNamespace(names={"127.0.0.1"}), logins=logins, tls=None)
        dashboard.Handler(connection, ("127.0.0.1", 0), server)
        response = HTTPResponse(SimpleNamespace(makefile=lambda *args: BytesIO(outgoing)))
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def check_read_surface(self, port):
        def request(path, *, body=None, headers=None, status=200):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                request_headers = {"Accept": "application/json"}
                if body is not None:
                    request_headers.update({"Content-Type": "application/json", "Authorization": self.authorization})
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
        self.assertEqual(len(operations), 55)
        self.assertEqual(len({op["name"] for op in operations}), 55)
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
            raw = [f"{method} {path} HTTP/1.1\r\n", "Host: 127.0.0.1\r\n", f"Authorization: {self.authorization}\r\n"]
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
            headers = {"Accept": "application/json"}
            if body is not None:
                headers.update({"Content-Type": "application/json", "Idempotency-Key": key,
                                "Authorization": self.authorization})
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
        from email.message import Message

        def headers(host):
            result = Message()
            result["Host"] = host
            result["X-Arete"] = "1"
            return result

        logins = tailnet.Logins(mock.Mock())
        logins.of = mock.Mock(return_value="fixture@example.test")
        handler = SimpleNamespace(server=SimpleNamespace(node=SimpleNamespace(names={"fixture.test"}),
                                  logins=logins, tls=None), operation=None, alias=None,
                                  client_address=("127.0.0.1", 1), headers=headers("fixture.test"))
        with principals.acting("hand"):
            dashboard.Handler.admit(handler, "GET", "/api/tasks")
            self.assertEqual(principals.current(), owner)
            for host, login, status in (("foreign.test", "fixture@example.test", 421),
                                        ("fixture.test", "stranger@example.test", 403),
                                        ("fixture.test", "fixture@example.test", 401)):
                handler.headers = headers(host)
                handler.server.logins.of.return_value = login
                with self.subTest(status=status), self.assertRaises(dashboard.Refusal) as got:
                    dashboard.Handler.admit(handler, "POST", "/api/tasks")
                self.assertEqual(got.exception.status, status)
        with self.assertRaises(sqlite3.IntegrityError), db.tx() as c:
            c.execute("INSERT INTO principals (id, kind, role, status, name, created) "
                      "VALUES ('u:other', 'human', 'owner', 'active', 'Other', ?)", (util.iso(),))


class _CredentialCase(unittest.TestCase):
    PRELUDE = "from tests.test_behavior import _CredentialCase; _CredentialCase.run_case(sys.argv[1])"
    LOGIN = "owner@example.com"
    TASKS = "/api/v1/tasks"
    TOKENS = "/api/v1/tokens"
    PAIRINGS = "/api/v1/auth/pairings"

    def scratch(self, case, value=None):
        with tempfile.TemporaryDirectory(prefix="arete-credential-test-") as directory:
            root = Path(directory).resolve()
            command = api_check.child_command(api_check.SOURCE, self.PRELUDE, case)
            done = subprocess.run(command, cwd=root, env=api_check.isolated_env(root), input=json.dumps(value),
                                  capture_output=True, text=True, timeout=CHILD_HANG_TIMEOUT)
            self.assertEqual(done.returncode, 0, done.stderr)
            line, = (line for line in done.stdout.splitlines() if line.startswith("BEHAVIOR_RESULT="))
            return json.loads(line.partition("=")[2])

    @staticmethod
    def run_case(case):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request")):
            name, fixture_method = case.split(".")
            fixture = globals()[name]()
            fixture.prepare()
            check = getattr(type(fixture), fixture_method)
            result = getattr(check, "__wrapped__", check)(fixture, json.load(sys.stdin))
        print("BEHAVIOR_RESULT=" + json.dumps(result))

    def prepare(self):
        root = Path.cwd()
        paths.CONFIG_FILE.write_text(f'arete_repo = "fixture"\n[dashboard]\nowner = "{self.LOGIN}"\nhttps = true\n'
                                     f'[[repo]]\nname = "fixture"\npath = {json.dumps(str(root))}\n'
                                     'tracker = "local"\ngithub = "fixture/repo"\n')
        self.prepare_store()
        self.logins = tailnet.Logins(mock.Mock())
        self.logins.of = mock.Mock(return_value=self.LOGIN)
        self.keys = iter(range(1000))
        cert, private = root / "tls.crt", root / "tls-private"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "1", "-subj", "/CN=arete-fixture", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(private), "-out", str(cert)], check=True, capture_output=True, timeout=10)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(cert, private)
        self.trust = ssl.create_default_context(cafile=str(cert))

    def prepare_store(self):
        db.migrate()
        self.owner = principals.seed_owner(self.LOGIN)
        self.start_credentials()

    def start_credentials(self):
        token_store.init_owner()
        self.meta = db.schema_metadata()
        self.owner_text = credential_files.read_client(self.meta["instance_tag"])

    @contextmanager
    def listen(self, secure=True):
        address = "127.0.0.1"
        node = tailnet.Node((address,), frozenset({address}), address)
        with mock.patch("socket.getfqdn", return_value=address):
            server = tailnet.Server(address, 0, node, self.logins, False, dashboard.Handler, dashboard.Redirect,
                                    tls=self.tls if secure else None)
        with _CliThroughServeCase.listening(server):
            yield server

    def connect(self, server):
        if server.tls is not None:
            return HTTPSConnection("127.0.0.1", server.server_port, timeout=3, context=self.trust)
        return HTTPConnection("127.0.0.1", server.server_port, timeout=3)

    def request(self, server, method, path, *, body=None, bearer=None, cookie=None, key=None, headers=None,
                status=200, code=None):
        sent = {"X-Arete": "1"} if cookie is not None else {}
        sent.update(headers or {})
        if bearer:
            sent["Authorization"] = f"Bearer {bearer}"
        if cookie:
            sent["Cookie"] = cookie
        if method == "POST":
            body = json.dumps(body or {})
            sent.update({"Content-Type": "application/json", "Idempotency-Key": key or f"credential-{next(self.keys)}"})
        with closing(self.connect(server)) as connection:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, status, (method, path, result.get("code"), result.get("detail")))
            if code:
                self.assertEqual(result.get("code"), code)
            return result, response.headers

    def code(self, server):
        return self.request(server, "POST", self.PAIRINGS, bearer=self.owner_text, status=201)[0]["code"]

    def claim(self, server, code, *, cookie=None, key=None):
        _, response = self.request(server, "POST", self.PAIRINGS + "/claim", body={"code": code},
                                   cookie=cookie, key=key, status=201)
        session, *attributes = response["Set-Cookie"].split("; ")
        self.assertTrue(session.startswith("__Host-arete_session="))
        self.assertEqual(set(attributes), {"HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=34560000"})
        return session

    def pair(self, server, **kwargs):
        return self.claim(server, self.code(server), **kwargs)

    def issue(self, server, cookie, scopes=("read", "task:write"), **kwargs):
        body = {"label": "Fixture client", "scopes": list(scopes), "profile": "remote"}
        return self.request(server, "POST", self.TOKENS, body=body, cookie=cookie, status=201, **kwargs)[0]

    def task(self, server, *, path=TASKS, **kwargs):
        body = {"repo": "fixture", "body" if path == self.TASKS else "prompt": "Add one task"}
        self.request(server, "POST", path, body=body, **kwargs)

    def task_actors(self):
        with db.reader() as c:
            return [row[0] for row in c.execute("SELECT actor FROM events_log WHERE kind = 'task.created' ORDER BY seq")]

    @contextmanager
    def cli_environment(self, server, **env):
        with (mock.patch.dict(os.environ, {"ARETE_SERVER": f"https://127.0.0.1:{server.server_port}", **env}),
              mock.patch.object(ssl, "create_default_context", return_value=self.trust)):
            yield

    def cli(self, command, *, stdin=""):
        with (redirect_stdout(StringIO()) as out, mock.patch("sys.stdin", StringIO(stdin)),
              mock.patch.object(codebase, "load")):
            self.assertEqual(cli.main(command), 0, out.getvalue())
        return out.getvalue()


def credential_test(check):
    @wraps(check)
    def launch(self):
        self.scratch(check.__qualname__)
    return launch


class _CredentialUpgradeCase(_CredentialCase):
    def prepare_store(self):
        chain = db.migrations.load()[:-1]
        self.previous_schema = max(m.n for m in chain if m.phase != "contract")
        with mock.patch.object(db.migrations, "load", return_value=chain), self.old_schema():
            db.migrate()
            self.owner = principals.seed_owner(self.LOGIN)

    def old_schema(self):
        return mock.patch.object(db, "NEEDED", self.previous_schema)

    def upgrade(self):
        db.migrate()
        self.start_credentials()

    def old_claim(self, key, route, body, **parameters):
        return self.owner, key, route, idempotency.fingerprint("POST", route, parameters, body)


class CredentialDashboardTests(unittest.TestCase):
    def test_dashboard_write_401_shows_pairing_guidance(self):
        result = subprocess.run(["node", "--input-type=module", "-e", """
            import assert from "node:assert/strict";
            import { readFileSync } from "node:fs";
            import { fileURLToPath } from "node:url";
            import { constants, runInThisContext } from "node:vm";
            import { visit } from "./arete/web/ui-data.js";
            import { Shell } from "./arete/web/ui-shell.js";
            import { prepareWrite, sendWrite } from "./arete/web/ui-write.js";

            globalThis.matchMedia = () => ({});
            globalThis.noteServedCode = () => {};
            const classic = fileURLToPath(new URL("./arete/web/common.js", import.meta.url));
            const call = runInThisContext(readFileSync(classic, "utf8") + "\\ncall", {
              filename: classic, importModuleDynamically: constants.USE_MAIN_CONTEXT_DEFAULT_LOADER,
            });
            const route = "/api/settings";
            const writes = [() => call(route, {}), () => sendWrite(prepareWrite(route))];
            globalThis.fetch = async () => Response.json({ error: "unauthenticated" }, { status: 401 });
            globalThis.location = { protocol: "https:" };
            const shell = () => JSON.stringify(Shell({ route: { screen: "floor", path: [] }, snapshot: {}, resources: {} }));
            assert.equal(await visit({ view: "floor" }), null);
            assert.doesNotMatch(shell(), /Enter a pairing code/);
            for (const [protocol, guidance] of [["https:", /\\/pair\\b/], ["http:", /pairing needs HTTPS/]]) {
              globalThis.location.protocol = protocol;
              for (const write of writes) await assert.rejects(write, { status: 401, message: guidance });
              assert.doesNotMatch(shell(), /Enter a pairing code|pairing needs HTTPS/);
            }
            globalThis.location.protocol = "https:";
            await assert.rejects(call(route), { status: 401, message: "unauthenticated" });
            await assert.rejects(sendWrite(prepareWrite("/api/v1/auth/pairings/claim")),
              { status: 401, message: "unauthenticated" });
            globalThis.fetch = async () => Response.json({ error: "scope_missing" }, { status: 403 });
            for (const write of writes) await assert.rejects(write, { status: 403, message: "scope_missing" });
        """], capture_output=True, text=True, timeout=8, check=False, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CredentialSessionStopTests(_CredentialCase):
    @credential_test
    def test_legacy_stop_requires_a_session_run(self, _):
        runs = [record.start(kind, pid=None) for kind in ("pipeline", "loop")]
        session = record.start("session", pid=None)
        with self.listen() as server:
            token = self.issue(server, self.pair(server), scopes=("session:write",))["text"]
            for run in (*runs, "missing"):
                self.request(server, "POST", f"/api/sessions/{run}/stop", bearer=token, status=404)
            result, _ = self.request(server, "POST", f"/api/sessions/{session}/stop", bearer=token)
            self.assertTrue(result["stopped"])
        for run in runs:
            row = run_store.run_get(run)
            self.assertEqual(row["status"], "running")
            self.assertFalse(row["stop_asked"])
        self.assertEqual(run_store.run_get(session)["status"], "stopped")


class CredentialUpgradeWriteTests(_CredentialUpgradeCase):
    @credential_test
    def test_task_start_replays_the_pre_deploy_person_claim(self, _):
        key = "pre-deploy-task-start"
        route = "/api/tasks"
        body = {"repo": "fixture", "prompt": "Keep the original task", "title": "Original", "kind": "feat"}
        context = self.old_claim(key, route, body)
        with self.old_schema(), principals.acting(self.owner):
            task = task_store.task_add(body["repo"], body["prompt"], body["title"], body["kind"],
                                       claim=context)
            run = record.start("pipeline", repo=body["repo"], issue=task)
            saved = tasks.answer(task, body["repo"], run, "running", "started")
            claims.complete(context, 200, api_http.json_bytes(saved), claims.lookup(self.owner, key)["first_seq"])
        command = ["task", "start", "--repo", "fixture", "--key", key, "--title", "Original", "--kind", "feat",
                   body["prompt"]]
        self.upgrade()
        with self.listen() as server, self.cli_environment(server):
            self.assertEqual(json.loads(self.cli(command)), saved)
            replay, headers = self.request(server, "POST", route, bearer=self.owner_text, key=key, body=body)
            self.assertEqual((replay, headers["Idempotent-Replayed"]), (saved, "true"))
            self.request(server, "POST", route, bearer=self.owner_text, key=key, body={**body, "prompt": "Changed"},
                         status=422, code="idempotency_key_reused")
            self.task(server, bearer=self.owner_text, key=key, status=422, code="idempotency_key_reused")
        self.assertEqual([row["id"] for row in task_store.tasks()], [task])
        self.assertEqual([row["id"] for row in run_store.runs()], [run])

    @credential_test
    def test_versioned_replay_keeps_old_claim_and_scope(self, _):
        key = "pre-upgrade-task-add"
        body = {"repo": "fixture", "body": "Original", "title": "", "kind": "feat"}
        with self.old_schema(), principals.acting(self.owner):
            answer = api.mutation("task.create", body, None)
            task = answer["task"]["id"]
            claims.complete(self.old_claim(key, self.TASKS, body), 200, api_http.json_bytes(answer), None)
        self.upgrade()
        with self.listen() as server:
            _, limited = token_store.issue(self.owner, "Read only", ["read"])
            self.request(server, "POST", self.TASKS, bearer=limited, key=key, body=body, status=403, code="scope_missing")
            replay, headers = self.request(server, "POST", self.TASKS, bearer=self.owner_text, key=key, body=body)
            self.assertEqual((replay["task"]["id"], headers["Idempotent-Replayed"]), (task, "true"))
        self.assertEqual(self.task_actors(), [self.owner])

    @credential_test
    def test_publication_resumes_pre_deploy_approval(self, _):
        approval_key = "pre-upgrade-approval"
        github = "fixture/repo"
        with self.old_schema(), principals.acting(self.owner):
            item = {"repo": "fixture", "title": "Approve delivery", "body": "An issue", "labels": []}
            approval = approvals_store.add_approval("fixture", "", {"type": "ticket", "item": item}, "File it")
            context = self.old_claim(approval_key, "/api/approvals/{id}", {"approve": True}, id=str(approval))
            pending = idempotency.Publication(context, None)
            claim = approvals_store.claim_approval(approval, util.iso(util.now() + timedelta(minutes=2)))
            pending.remember({"token": claim, "repo": github, "send_started": True})
            self.assertTrue(approvals_store.bind_publication(approval, claim, pending.marker))
        self.upgrade()
        url = gh.issue_url(github, 2)
        with (self.listen() as server, mock.patch.object(gh, "find_issue_marker", return_value=url) as lookup,
              mock.patch.object(gh, "issue_create", side_effect=AssertionError("Duplicate publication")),
              self.cli_environment(server, ARETE_IDEMPOTENCY_KEY=approval_key)):
            for _ in range(2):
                self.assertEqual(json.loads(self.cli(["approve", str(approval)]))["result"], url)
            lookup.assert_called_once_with(github, pending.marker)
        saved = claims.lookup(self.owner, approval_key)
        self.assertEqual(saved["status"], 200)
        with db.reader() as c:
            row = c.execute("SELECT actor, kind FROM events_log WHERE seq = ?", (saved["last_seq"],)).fetchone()
        self.assertEqual(tuple(row), (f"client:{self.owner_text.split('_', 3)[2]}", "publication.sent"))


class CredentialWriteTests(_CredentialCase):
    @credential_test
    def test_scopes_and_client_actor(self, _):
        with self.listen() as server:
            cookie = self.pair(server)
            issued = self.issue(server, cookie, scopes=("task:write",))
            identity = issued["token"]["id"]
            actor = f"client:{identity}"
            self.task(server, bearer=issued["text"], key="client-write")
            self.assertEqual(claims.lookup(self.owner, "client-write")["status"], 200)
            self.assertIsNone(claims.lookup(actor, "client-write"))
            viewed, _ = self.request(server, "GET", f"{self.TOKENS}/{identity}", cookie=cookie)
            client, = principals.listing(id=actor)
            self.assertEqual((viewed["token"]["principal"], client["kind"], client["role"]), (self.owner, "client", None))
            limited = self.issue(server, cookie, scopes=("read",))
            self.task(server, bearer=limited["text"], status=403, code="scope_missing")
            self.assertEqual(self.task_actors(), [actor])
            expired, text = token_store.issue(self.owner, "Expired", ["read"], kind=None, expires="2000-01-01T00:00:00Z")
            self.request(server, "GET", self.TASKS, bearer=text, status=401, code="token_expired")
            token_store.maintain()
            self.assertEqual(token_store.get(expired["id"], self.owner)["revoke_reason"], "expired")
            self.assertTrue(token_store.get(identity, self.owner)["last_used_node"])
            self.request(server, "POST", f"/api/v1/principals/{actor}/revoke", cookie=cookie)
            self.task(server, bearer=issued["text"], status=401)

    @credential_test
    def test_api_rate_limits_share_person_buckets(self, _):
        _, other = token_store.issue(self.owner, "Second client", ["read", "task:write"])
        rates = api_http.RateLimits(clock=lambda: 0)
        with self.listen() as server, mock.patch.object(api_http, "RATES", rates):
            rates.take(self.owner, command=True, cost=59)
            self.task(server, bearer=self.owner_text)
            self.task(server, bearer=other, status=429, code="rate_limited")
            for bearer, count, status in ((self.owner_text, 49, 200), (other, 2, 429)):
                body = {"requests": [{"operation": "meta.get"}] * count}
                self.request(server, "POST", "/api/v1/batch", bearer=bearer, body=body, status=status)
            self.request(server, "GET", self.TASKS, status=429, code="rate_limited")
        self.assertEqual(len(self.task_actors()), 1)

    @credential_test
    def test_bearer_push_subscription_keeps_person_contact(self, _):
        body = {"endpoint": "https://127.0.0.1/push", "keys": {"p256dh": "fixture", "auth": "fixture"}}
        with self.listen() as server, mock.patch.object(webpush, "public_key", return_value="fixture"):
            self.request(server, "POST", "/api/push/subscribe", bearer=self.owner_text, body=body)
        row, = webpush.push_store.push_list()
        self.assertEqual(webpush.subject(config.load(), row), f"mailto:{self.LOGIN}")


class CredentialUpgradeServiceTests(_CredentialUpgradeCase):
    @credential_test
    def test_existing_service_without_xdg_state_home_keeps_cli_writes(self, _):
        root = Path.cwd()
        env = api_check.isolated_env(root)
        env.pop("XDG_STATE_HOME")
        start = "from arete.store import db, tokens; db.migrate(); tokens.init_owner()"
        subprocess.run(api_check.child_command(api_check.SOURCE, start), cwd=root, env=env, check=True,
                       capture_output=True, timeout=CHILD_HANG_TIMEOUT)
        tag = db.schema_metadata()["instance_tag"]
        fallback = credential_files.client_path(tag, paths.DEFAULT_TOKEN_CLIENT_DIR)
        self.assertEqual(fallback.stat().st_mode & 0o777, 0o600)
        self.assertFalse(credential_files.client_path(tag).exists())
        with self.listen(secure=False) as server:
            name = "arete-fixture.invalid"
            server.node = replace(server.node, name=name, names=server.node.names | {name})
            with mock.patch.object(paths, "TOKEN_CLIENT_DIR", fallback.parent):
                tailnet.record_server(server.node, server.server_port, tailnet.Front("http", [server]))
            with mock.patch.dict(os.environ, {client.ENV: f"http://{name}:{server.server_port}"}):
                self.cli(["task", "add", "--repo", "fixture", "Explicit server"])
            del os.environ[client.ENV]
            self.cli(["task", "add", "--repo", "fixture", "Local discovery"])
            with self.listen(secure=False):
                other = client.server()
                self.assertEqual(other.instance, db.schema_metadata()["instance_id"])
                self.assertNotIn("Authorization", client.headers_for(other))
            self.cli(["logout"])
            self.assertFalse(fallback.exists())
        actors = self.task_actors()
        self.assertEqual(len(actors), 2)
        self.assertTrue(actors[0].startswith("client:"))
        self.assertEqual(actors, [actors[0]] * 2)


class CredentialPairingTests(_CredentialCase):
    @credential_test
    def test_session_csrf_origin_and_presence(self, _):
        with self.listen() as server:
            cookie = self.pair(server)
            self.task(server, cookie=cookie)
            self.task(server, cookie=cookie, headers={"X-Arete": ""}, status=403, code="csrf")
            for headers in ({"Origin": "https://elsewhere.example"}, {"Sec-Fetch-Site": "cross-site"}):
                self.task(server, cookie=cookie, headers=headers, status=403, code="bad_origin")
            pairing = self.logins.credentials.pairings[self.owner]
            for bearer in (None, self.owner_text):
                answer, _ = self.request(server, "POST", self.PAIRINGS, bearer=bearer, status=403, code="forbidden")
                self.assertEqual(answer["reason"], "presence_required")
                self.assertIs(self.logins.credentials.pairings[self.owner], pairing)
            self.request(server, "POST", self.PAIRINGS, cookie=cookie, status=201)
            self.assertRegex(self.owner, r"^u:[0-9a-f]{8}$")
            self.assertEqual(self.task_actors(), [self.owner])

    @credential_test
    def test_session_renews_before_maintenance(self, _):
        expiry = util.now() + timedelta(seconds=2)
        session, text = token_store.issue(self.owner, "Near expiry", ["read"], kind="session", expires=util.iso(expiry))
        cookie = "__Host-arete_session=" + text
        with self.listen() as server:
            with mock.patch.object(util, "now", return_value=expiry - timedelta(seconds=1)):
                self.request(server, "GET", self.TASKS, cookie=cookie)
            with mock.patch.object(util, "now", return_value=expiry + timedelta(seconds=1)):
                self.request(server, "GET", self.TASKS, cookie=cookie)
                token_store.maintain()
                self.request(server, "GET", self.TASKS, cookie=cookie)
            with db.reader() as c:
                row = c.execute("SELECT expires, last_used_at FROM tokens WHERE id = ?", (session["id"],)).fetchone()
            self.assertGreater(row["expires"], util.iso(expiry))
            self.assertEqual(row["last_used_at"], util.iso(expiry + timedelta(seconds=1)))

    @credential_test
    def test_logout_key_applies_to_each_session(self, _):
        with self.listen() as server:
            first = self.pair(server)
            pairing, _ = self.request(server, "POST", self.PAIRINGS, cookie=first, status=201)
            second = self.claim(server, pairing["code"], cookie=first)
            logout = "/api/v1/auth/logout"
            key = "shared-logout"
            for cookie in (first, second):
                self.request(server, "GET", self.TASKS, cookie=cookie)
                result, headers = self.request(server, "POST", logout, cookie=cookie, key=key)
                self.assertTrue(result["ended"])
                self.assertIn("Max-Age=0", headers["Set-Cookie"])
                self.assertIsNone(headers["Idempotent-Replayed"])
                self.request(server, "GET", self.TASKS, cookie=cookie, status=401, code="unauthenticated")


class CredentialClaimTests(_CredentialCase):
    @credential_test
    def test_pairing_start_shares_keys_and_cannot_restart_a_consumed_code(self, _):
        with self.listen() as server:
            cookie = self.pair(server)
            self.task(server, bearer=self.owner_text, key="task-claim")

            def start(key, status=201, code=None):
                return self.request(server, "POST", self.PAIRINGS, cookie=cookie, key=key, status=status, code=code)[0]

            start("task-claim", 422, "idempotency_key_reused")
            route = "/api/chats/{n}/file"
            sha = idempotency.fingerprint("POST", route, {"n": "1"}, {})
            claims.complete((self.owner, "publication-claim", route, sha), 200, b"{}", None)
            start("publication-claim", 422, "idempotency_key_reused")
            pairing = start("pairing-claim")
            self.assertEqual(start("pairing-claim"), pairing)
            self.task(server, bearer=self.owner_text, key="pairing-claim", status=422, code="idempotency_key_reused")
            self.request(server, "POST", "/api/chats/1/file", bearer=self.owner_text, key="pairing-claim",
                         status=422, code="idempotency_key_reused")
            self.claim(server, pairing["code"])
            self.assertEqual(start("pairing-claim", 409, "conflict")["reason"], "pairing_claimed")
            self.assertIsNone(claims.lookup(self.owner, "pairing-claim"))
            self.assertEqual(len(self.task_actors()), 1)

    @credential_test
    def test_claim_replay_requires_a_live_cookie_receipt(self, _):
        with self.listen() as server:
            code = self.code(server)
            key = "claim-replay"
            cookie = self.claim(server, code, key=key)
            self.assertEqual(self.claim(server, code, key=key), cookie)
            receipts = self.logins.credentials.claimed
            identity, = receipts
            deadline, text = receipts.pop(identity)
            wrong = "000001" if code == "000000" else "000000"
            for state, receipt in (("missing", None), ("expired", (0, text)), ("revoked", (deadline, text))):
                with self.subTest(receipt_state=state):
                    if receipt is not None:
                        receipts[identity] = receipt
                    if state == "revoked":
                        self.request(server, "POST", "/api/v1/auth/logout", cookie=cookie)
                    result, headers = self.request(server, "POST", self.PAIRINGS + "/claim", body={"code": code},
                                                   key=key, status=409, code="conflict")
                    self.assertEqual(result["reason"], "pairing_claimed")
                    self.assertIsNone(headers["Set-Cookie"])
                    _, headers = self.request(server, "POST", self.PAIRINGS + "/claim", body={"code": wrong},
                                              key=key, status=422, code="idempotency_key_reused")
                    self.assertIsNone(headers["Set-Cookie"])

    @credential_test
    def test_database_only_restore_replays_consumed_claim_without_cookie(self, _):
        claim = self.PAIRINGS + "/claim"
        key = "restored-claim"
        with self.listen() as server:
            code = self.code(server)
            self.claim(server, code, key=key)
            wrong = "000001" if code == "000000" else "000000"
            snapshot = backup.snapshot("pairing-claim-test")
        credential_files.key_path(self.meta["token_key_ref"]).unlink()
        backup.restore(snapshot)
        with self.listen() as server:
            _, headers = self.request(server, "POST", claim, body={"code": wrong}, key=key,
                                      status=422, code="idempotency_key_reused")
            self.assertIsNone(headers["Set-Cookie"])
            answer, headers = self.request(server, "POST", claim, body={"code": code}, key=key,
                                           status=409, code="conflict")
            self.assertEqual(answer.get("reason"), "pairing_claimed")
            self.assertIsNone(headers["Set-Cookie"])
            self.assertNotIn(json.dumps(code), json.dumps(answer))
        row = claims.lookup(self.owner, key)
        self.assertEqual(row["status"], 201)
        self.assertNotIn(json.dumps(code).encode(), row["response"])
        self.assertNotEqual(row["request_sha"].rsplit(":", 1)[-1],
                            idempotency.fingerprint("POST", claim, {}, {"code": code}))


class CredentialAdmissionTests(_CredentialCase):
    @credential_test
    def test_whois_read_only_tls_and_issue_replay(self, _):
        only_header = {"X-Arete": "1"}
        with self.listen() as server, self.cli_environment(server):
            self.request(server, "GET", self.TASKS, headers=only_header)
            for path in (self.TASKS, "/api/tasks"):
                self.task(server, path=path, headers=only_header, status=401, code="unauthenticated")
            for bearer in (None, self.owner_text):
                result, _ = self.request(server, "POST", self.TOKENS, bearer=bearer, status=403, code="forbidden")
                self.assertEqual(result["reason"], "presence_required")
            printed, link = self.cli(["pair"]).splitlines()
            pairing = link.rsplit("#", 1)[1]
            self.assertEqual(printed, f"Pairing code {pairing}")
            with closing(self.connect(server)) as connection:
                connection.request("GET", "/pair")
                self.assertIn(b'src="/pair.js"', connection.getresponse().read())
            cookie = self.claim(server, pairing)
            issued = self.issue(server, cookie, key="issue-replay")
            self.assertEqual(self.issue(server, cookie, key="issue-replay"), {**issued, "text": "[shown once]"})
            self.task(server, bearer=self.owner_text, cookie=cookie, status=400, code="ambiguous_credential")
            self.assertEqual(self.task_actors(), [])
            text = issued["text"]
            self.logins.of.return_value = "tag:worker"
            self.request(server, "GET", self.TASKS, status=403)
            self.request(server, "GET", self.TASKS, bearer=text)
            with mock.patch.object(dashboard.webhooks, "receive", return_value={"queued": True}):
                self.request(server, "POST", "/hook/fixture", headers={"X-Arete-Instance": "unrelated"}, status=202)
            self.cli(["login", "--token-stdin"], stdin=text + "\n")
            self.cli(["task", "add", "--repo", "fixture", "tagged"])
            self.assertEqual(self.task_actors(), ["client:" + issued["token"]["id"]])
            self.cli(["logout"])
            self.assertFalse(credential_files.client_path(self.meta["instance_tag"]).exists())
            self.logins.of.return_value = self.LOGIN
        with self.listen(secure=False) as server:
            for path in (self.TOKENS, self.PAIRINGS + "/claim"):
                self.request(server, "POST", path, status=426, code="tls_required")
            self.request(server, "GET", self.TASKS)
            self.task(server, bearer=self.owner_text)


class CredentialRotationTests(_CredentialCase):
    @credential_test
    def test_rotate_and_revoke_close_stream(self, _):
        with self.listen() as server:
            cookie = self.pair(server)
            first = self.issue(server, cookie)
            rotate = f"{self.TOKENS}/{first['token']['id']}/rotate"
            denied, _ = self.request(server, "POST", rotate, bearer=first["text"], status=403)
            self.assertEqual(denied["reason"], "presence_required")
            new, _ = self.request(server, "POST", rotate, cookie=cookie, body={"grace_seconds": 0}, status=201)
            self.request(server, "GET", self.TASKS, bearer=first["text"], status=401, code="token_rotated")
            self.request(server, "GET", self.TASKS, bearer=new["text"])
            with self.listen(secure=False) as stream_server, closing(self.connect(stream_server)) as conn:
                conn.request("GET", "/api/v1/events", headers={"Authorization": "Bearer " + new["text"]})
                stream = conn.getresponse()
                self.assertEqual((stream.status, stream.readline()), (200, b"retry: 2000\n"))
                started = time.monotonic()
                self.request(server, "POST", f"{self.TOKENS}/{new['token']['id']}/revoke", cookie=cookie)
                stream.read()
                self.assertLess(time.monotonic() - started, 2)
            self.request(server, "GET", self.TASKS, bearer=new["text"], status=401)
        with db.reader() as c:
            secrets = [first["text"], new["text"], *[r[0] for r in c.execute("SELECT hash FROM tokens")]]
            durable = [r[0] for r in c.execute("SELECT data FROM events_log WHERE kind = 'token.changed'")]
        self.assertTrue(durable)
        self.assertTrue(all(secret not in data for secret in secrets for data in durable))


class _CredentialStreamCase(_CredentialCase):
    def large_streams(self):
        payload = "x" * (1 << 20)
        run = record.start("pipeline", repo="fixture")
        run_lines.path(run).parent.mkdir(parents=True, exist_ok=True)
        run_lines.path(run).write_bytes(dashboard.event_line({"seq": 1, "event": run_lines.END, "body": payload}))
        with db.tx():
            events.emit("fixture.large", "fixture", cls="durable", data={"body": payload})
        return payload, "/api/v1/events?since=0.0&kinds=fixture.large", f"/api/runs/{run}/events?follow=1"

    @contextmanager
    def blocked_stream(self, route, accept, text):
        address = "127.0.0.1"
        server = SimpleNamespace(node=SimpleNamespace(names={address}), logins=self.logins, tls=self.tls,
                                 inside_serve=False, RequestHandlerClass=dashboard.Handler)
        browser, app = socketpair()
        app.setsockopt(SOL_SOCKET, SO_SNDBUF, 4096)
        browser.settimeout(3)
        blocked = threading.Event()
        poll = dashboard.select.poll

        def observe():
            blocked.set()
            return poll()

        thread = threading.Thread(target=tailnet.Server.finish_request, args=(server, app, (address, 1)), daemon=True)
        with browser, app, mock.patch.object(dashboard.select, "poll", observe):
            thread.start()
            with self.trust.wrap_socket(browser, server_hostname=address) as conn:
                conn.sendall((f"GET {route} HTTP/1.1\r\nHost: {address}\r\nAccept: {accept}\r\n"
                              f"Authorization: Bearer {text}\r\n\r\n").encode())
                self.assertTrue(blocked.wait(2))
                yield conn, thread
            thread.join(1)


class CredentialStreamDeliveryTests(_CredentialStreamCase):
    @credential_test
    def test_slow_readers_receive_complete_payloads(self, _):
        payload, events_route, run_route = self.large_streams()
        for route, accept in ((events_route, "text/event-stream"), (events_route, "application/json"),
                              (events_route, "application/x-ndjson"), (run_route, "application/x-ndjson")):
            token, text = token_store.issue(self.owner, "Stream reader", ["read"])
            with self.subTest(route=route, accept=accept), self.blocked_stream(route, accept, text) as (conn, _):
                time.sleep(.65)
                with HTTPResponse(conn) as response:
                    response.begin()
                    self.assertEqual(response.status, 200)
                    if route == run_route:
                        self.assertEqual(response.readline(), dashboard.event_line({"seq": 1, "event": run_lines.END, "body": payload}))
                        continue
                    if accept == "application/json":
                        event, = json.load(response)["events"]
                    else:
                        if accept == "text/event-stream":
                            self.assertEqual(response.readline() + response.readline(), b"retry: 2000\n\n")
                            self.assertTrue(response.readline().startswith(b"id: "))
                        line = response.readline()
                        self.assertTrue(line.endswith(b"\n"))
                        event = json.loads(line.removeprefix(b"data: "))
                        if accept == "text/event-stream":
                            self.assertEqual(response.readline(), b"\n")
                    self.assertEqual((event["kind"], event["data"]["body"]), ("fixture.large", payload))
                token_store.revoke(token["id"], self.owner)


class CredentialStreamRevocationTests(_CredentialStreamCase):
    @credential_test
    def test_backpressured_streams_close_on_revocation(self, _):
        _, events_route, run_route = self.large_streams()
        measured = {}
        for route, accept, name in ((events_route, "text/event-stream", "events"), (run_route, "", "run")):
            token, text = token_store.issue(self.owner, "Stream reader", ["read"])
            with self.subTest(route=name), self.blocked_stream(route, accept, text) as (_, thread):
                started = time.monotonic()
                token_store.revoke(token["id"], self.owner)
                thread.join(max(0, 1 - (time.monotonic() - started)))
                elapsed = time.monotonic() - started
                self.assertFalse(thread.is_alive(), (name, elapsed))
                self.assertLess(elapsed, 1)
                measured[name] = round(elapsed, 3)
        return measured


class CredentialRevocationTests(_CredentialCase):

    @credential_test
    def test_revocation_persists_pending_last_use(self, _):
        with self.listen() as server:
            cookie = self.pair(server)
            session = token_store.resolve(cookie.split("=", 1)[1]).id
            first, second = (self.issue(server, cookie) for _ in range(2))
            first_id, second_id = (issued["token"]["id"] for issued in (first, second))
            used_at = util.now()
            with (mock.patch.object(util, "now", return_value=used_at),
                  mock.patch.object(self.logins, "node_key", return_value="audit-node")):
                for issued in (first, second):
                    self.request(server, "GET", self.TASKS, bearer=issued["text"])
                self.request(server, "POST", f"{self.TOKENS}/{first_id}/revoke", cookie=cookie)
                self.request(server, "POST", f"/api/v1/principals/client:{second_id}/revoke", cookie=cookie)
                self.request(server, "POST", "/api/v1/auth/logout", cookie=cookie)
        for flush in (False, True):
            if flush:
                token_store.maintain()
            with db.reader() as c:
                for identity in (first_id, second_id, session):
                    row = c.execute("SELECT last_used_at, last_used_node FROM tokens WHERE id = ?", (identity,)).fetchone()
                    self.assertEqual(tuple(row), (util.iso(used_at), "audit-node"), identity)


class CredentialRestoreTests(_CredentialCase):
    @credential_test
    def test_restore_invalidates_every_credential(self, _):
        tag = self.meta["instance_tag"]
        path = credential_files.client_path(tag)
        self.assertEqual((path.name, path.stat().st_mode & 0o777), (f"{tag}.token", 0o600))
        with self.listen() as server:
            cookie = self.pair(server)
            first = self.issue(server, cookie)
            snapshot = backup.snapshot("credential-test")
            self.request(server, "POST", f"{self.TOKENS}/{first['token']['id']}/revoke", cookie=cookie)
            second = self.issue(server, cookie)
        backup.restore(snapshot)
        meta = db.schema_metadata()
        self.assertNotEqual(meta["token_key_ref"], self.meta["token_key_ref"])
        self.assertEqual(meta["instance_id"], self.meta["instance_id"])
        with self.listen() as server:
            for bearer in (first["text"], second["text"], self.owner_text):
                self.request(server, "GET", self.TASKS, bearer=bearer, status=401)
            self.request(server, "GET", self.TASKS, cookie=cookie, status=401)
            token_store.init_owner()
            self.owner_text = credential_files.read_client(tag)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.request(server, "GET", self.TASKS, bearer=self.owner_text)
            self.pair(server, cookie=cookie)
        self.assertTrue(all(b"ar_" not in p.read_bytes() for p in paths.CONFIG_DIR.rglob("*") if p.is_file()))


class CredentialAuthTests(_CredentialCase):
    @credential_test
    def test_ask_actor_resolves_only_for_worker(self, _):
        run = record.start("pipeline", repo="fixture")
        _, text = token_store.issue(self.owner, "Ask", ["read"], kind="ask", run_id=run)
        context = token_store.resolve(text, step="implement")
        self.assertEqual((context.actor, context.principal), (f"run:{run}/implement", self.owner))
        with self.listen() as server:
            self.request(server, "GET", self.TASKS, bearer=text, status=401, code="unauthenticated")

    @credential_test
    def test_auth_and_pairing_rate_limits(self, _):
        with self.listen() as server:
            start = {"bearer": self.owner_text, "key": "burned-pairing"}
            code = self.request(server, "POST", self.PAIRINGS, status=201, **start)[0]["code"]
            wrong = "000000" if code != "000000" else "000001"
            for attempt in (wrong, wrong, wrong, wrong, wrong, code):
                _, headers = self.request(server, "POST", self.PAIRINGS + "/claim", body={"code": attempt}, status=401)
                self.assertIsNone(headers["Set-Cookie"])
            self.request(server, "POST", self.PAIRINGS, status=409, code="conflict", **start)
            for _ in range(3):
                self.request(server, "POST", self.TOKENS, status=403)
            self.request(server, "POST", self.TOKENS, status=429, code="rate_limited")
            self.logins.credentials = type(self.logins.credentials)()
            for _ in range(10):
                self.request(server, "GET", self.TASKS, bearer="wrong", status=401)
            _, headers = self.request(server, "GET", self.TASKS, bearer=self.owner_text, status=429, code="auth_blocked")
            self.assertTrue(1 <= int(headers["Retry-After"]) <= 900)


class CredentialInstanceTests(_CredentialCase):
    def test_foreign_tag_and_unknown_id_precede_hmac(self):
        token = self.scratch("CredentialInstanceTests.check_local_token")
        self.scratch("CredentialInstanceTests.check_foreign_token", token)

    def check_local_token(self, _):
        _, text = token_store.issue(self.owner, "Local fixture", ["read"])
        with self.listen() as server:
            self.request(server, "GET", self.TASKS, bearer=text)
        return text

    def check_foreign_token(self, text):
        self.assertNotEqual(text.split("_", 3)[1], self.meta["instance_tag"])
        with (self.listen() as server, mock.patch.object(hmac, "new") as digest,
              mock.patch.object(hmac, "compare_digest") as compare):
            unknown = f"ar_{self.meta['instance_tag']}_ffffffff_unknown"
            with db.reader() as c:
                self.assertIsNone(c.execute("SELECT id FROM tokens WHERE id = 'ffffffff'").fetchone())
            foreign, _ = self.request(server, "GET", self.TASKS, bearer=text, status=401, code="unauthenticated")
            self.assertEqual(foreign, self.request(server, "GET", self.TASKS, bearer=unknown, status=401)[0])
            digest.assert_not_called()
            compare.assert_not_called()
