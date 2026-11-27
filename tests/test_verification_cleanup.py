import os
import shutil
import sqlite3
import stat
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from arete import config, environment, local_paths, orchestration, paths, pipeline, scratch, tracker, util, workspace
from arete.agent import Result
from arete.store import db, workspaces


class VerificationCleanupTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.wt = root / "worktree"
        self.wt.mkdir()
        util.git(["init", "-q", "--template="], self.wt)
        (self.wt / "tracked.txt").write_text("tracked content\n")
        util.git(["add", "tracked.txt"], self.wt)

        self.run_dir = root / "run"
        self.run_dir.mkdir()
        config_file = root / "config.toml"
        config_file.write_text('[environment]\npass = ["VERIFIER_FIXTURE_VALUE"]\n')
        self.cfg = config.load(config_file, required=True)
        repo = config.Repo("fixture", self.wt)
        self.run = pipeline.Run("verify-cleanup", repo, self.run_dir, tracker=mock.Mock(), tid="1")
        checkout = workspace.Checkout("", "owner/fixture", self.wt, self.wt, "main")
        ticket = tracker.Ticket("1", "#1", "Check the fixture")
        layer = config.RepoLayer(root, verify=config.Verify(url=""))
        self.work = pipeline.TicketWork(self.cfg, self.run, ticket, self.wt, [checkout], layer, 60, "test workspace")
        self.enterContext(mock.patch.object(paths, "dashboard_ports", return_value=frozenset()))
        self.enterContext(mock.patch.object(scratch, "make", side_effect=ValueError("no scratch authority")))
        self.enterContext(mock.patch.object(workspace, "scratch_view", return_value=scratch.View([], [], True)))
        self.enterContext(mock.patch.object(self.run, "log"))

    def evidence(self, run_agent):
        with mock.patch.object(self.run, "agent", side_effect=run_agent):
            verified = pipeline.use_product(self.work, "verify", "verify", goal="Check the fixture.", summary="", diff="")
        self.assertEqual(verified.verdict, "pass")
        controller = SimpleNamespace(data={"acceptance": []}, executing="d0001", catalog=mock.Mock(),
                                     fingerprint=lambda: "fixture-tree", policy_sha=lambda: "fixture-policy")
        orchestration.Controller.stamp(controller, "verify", asdict(verified))
        name, evidence = controller.catalog.add_context.call_args.args
        self.assertEqual(name, "evidence:verify")
        return (self.run_dir / "verify.md").read_text(), evidence

    def agent_result(self, *_args, **_kwargs):
        return Result(ok=True, structured={"verdict": "pass", "summary": "The fixture passed.", "checks": [
            {"what": "Inspect the fixture", "how": "Open it", "saw": "It opened", "passed": True},
        ]})

    def test_removed_file_is_reported_without_its_forwarded_value(self):
        synthetic = "synthetic-forwarded-fixture-value"
        added = self.wt / f"{synthetic}.txt"
        screenshot = self.wt / "fixture.png"

        def agent(*args, **kwargs):
            forwarded = environment.build(environment.passed(self.cfg, self.run.repo))
            (self.wt / f"{forwarded['VERIFIER_FIXTURE_VALUE']}.txt").write_text("fixture\n")
            screenshot.write_bytes(b"image fixture")
            return self.agent_result(*args, **kwargs)

        with mock.patch.dict(os.environ, {"VERIFIER_FIXTURE_VALUE": synthetic}):
            saved, evidence = self.evidence(agent)
        self.assertFalse(added.exists())
        self.assertFalse(screenshot.exists())
        self.assertEqual((self.run_dir / "evidence" / screenshot.name).read_bytes(), b"image fixture")
        line = ("Arete removed 1 untracked file that verification added to the worktree, "
                "after verification ended. None is left behind.")
        self.assertIn(line, saved.splitlines())
        for surface in (saved, evidence):
            self.assertEqual(surface.count(line), 1)
            self.assertNotIn(synthetic, surface)

    def test_no_added_file_has_no_cleanup_line(self):
        for surface in self.evidence(self.agent_result):
            self.assertNotIn("Arete removed", surface)


class SandboxArtifactTrackingTests(unittest.TestCase):
    MARKER = local_paths.VERIFY_SCRATCH_CONTENT + b"\n"

    @classmethod
    def setUpClass(cls):
        cls._template_dir = TemporaryDirectory()
        cls.addClassCleanup(cls._template_dir.cleanup)
        template_root = Path(cls._template_dir.name).resolve()
        template_state = template_root / "state"
        template_state.mkdir()
        cls._template_db = template_state / "arete.db"
        with mock.patch.multiple(paths, STATE_DIR=template_state, DB_FILE=cls._template_db,
                                 LOCKS_DIR=template_state / "locks", RUNS_DIR=template_state / "runs",
                                 WORKTREES_DIR=template_root / "worktrees"):
            db.migrate()

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.state = self.root / "state"
        self.worktrees = self.root / "worktrees"
        self.state.mkdir()
        self.worktrees.mkdir()
        shutil.copy2(self._template_db, self.state / "arete.db")
        self.enterContext(mock.patch.multiple(
            paths, STATE_DIR=self.state, DB_FILE=self.state / "arete.db",
            LOCKS_DIR=self.state / "locks", RUNS_DIR=self.state / "runs", WORKTREES_DIR=self.worktrees,
        ))
        self.enterContext(mock.patch.object(workspace, "scratch_view", return_value=scratch.View([], [], True)))
        self.enterContext(mock.patch.object(workspace, "scratch_held", return_value=[]))
        self.enterContext(mock.patch.object(workspace, "scratch_left", return_value=[]))

        self.src = self.root / "origin"
        self.src.mkdir()
        util.git(["init", "-q", "-b", "main", "--template="], self.src)
        (self.src / ".gitignore").write_text(".arete-sandbox\n*.local\n.cache/\n")
        (self.src / "app.py").write_text("value = 1\n")
        util.git(["add", "."], self.src)
        util.git_as_owner(["commit", "-qm", "Initial commit"], self.src)

        self.wt = self.worktrees / "fixture-1029"
        self.checkout = workspace.Checkout("", "owner/fixture", self.src, self.wt, "main")
        workspace.add_worktree(self.checkout, "fix/1029", "main")
        workspaces.claim(self.wt, "fixture", "1029", "run-1", checkouts=[self.checkout])

    def make_owned_sandbox(self, base: Path | None = None, *, extra: str = "state/db.sqlite") -> Path:
        root = (base or self.wt) / local_paths.VERIFY_SCRATCH_ROOT
        root.mkdir(mode=0o700, exist_ok=True)
        (root / ".gitignore").write_text("*\n")
        (root / local_paths.VERIFY_SCRATCH_MARKER).write_bytes(self.MARKER)
        target = root / extra
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sandbox artifact\n")
        return root

    def test_owned_sandbox_does_not_block_worktree_reuse_or_removal(self):
        sandbox_dir = self.make_owned_sandbox()
        # No note_made ran after sandbox creation (simulating interrupted check or external calibration).
        self.assertEqual(workspace.kept_checkout(self.wt, self.checkout, self.src, [], commits=False), "")
        self.assertEqual(workspace.kept_work(self.wt, [self.checkout], self.src, [], commits=False), "")
        self.assertEqual(workspace.workspace_blockers(self.wt, [self.checkout]), [])

        # Mutating sandbox contents after note_made still does not block reuse or removal.
        workspace.note_made(self.wt, [self.checkout])
        (sandbox_dir / "state/db.sqlite").write_text("mutated by calibration\n")
        self.assertEqual(workspace.kept_work(self.wt, [self.checkout], self.src, [], commits=False), "")
        release = workspace.remove_workspace(self.wt, [self.checkout])
        self.assertEqual(release.state, "removed")
        self.assertFalse(self.wt.exists())

    def test_unmarked_or_tampered_sandbox_and_unknown_ignored_files_stay_protected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / local_paths.VERIFY_SCRATCH_MARKER).write_bytes(self.MARKER)

        for kind in ("missing-marker", "wrong-text", "blank-line", "symlink-marker", "hardlink-marker", "symlink-dir"):
            with self.subTest(kind=kind):
                sandbox_dir = self.wt / local_paths.VERIFY_SCRATCH_ROOT
                if sandbox_dir.is_symlink():
                    sandbox_dir.unlink()
                elif sandbox_dir.exists():
                    for child in sorted(sandbox_dir.rglob("*"), reverse=True):
                        child.unlink() if child.is_file() or child.is_symlink() else child.rmdir()
                    sandbox_dir.rmdir()
                if kind == "symlink-dir":
                    sandbox_dir.symlink_to(outside, target_is_directory=True)
                else:
                    sandbox_dir.mkdir()
                    (sandbox_dir / "user.txt").write_text("keep me\n")
                    marker = sandbox_dir / local_paths.VERIFY_SCRATCH_MARKER
                    if kind == "wrong-text":
                        marker.write_text("not arete\n")
                    elif kind == "blank-line":
                        marker.write_bytes(self.MARKER + b"\n")
                    elif kind == "symlink-marker":
                        marker.symlink_to(outside / local_paths.VERIFY_SCRATCH_MARKER)
                    elif kind == "hardlink-marker":
                        os.link(outside / local_paths.VERIFY_SCRATCH_MARKER, marker)

                why = workspace.kept_checkout(self.wt, self.checkout, self.src, [], commits=False)
                self.assertIn("git-ignored files that arete has no record of leaving there", why)
                self.assertIn(".arete-sandbox", why)
                self.assertTrue(workspace.workspace_blockers(self.wt, [self.checkout]))

        # When owned sandbox coexists with an unknown user ignored file, only the unknown file blocks.
        sandbox_dir = self.wt / local_paths.VERIFY_SCRATCH_ROOT
        if sandbox_dir.is_symlink():
            sandbox_dir.unlink()
        self.make_owned_sandbox()
        user_file = self.wt / "personal.local"
        user_file.write_text("user notes\n")
        why = workspace.kept_checkout(self.wt, self.checkout, self.src, [], commits=False)
        self.assertIn("personal.local", why)
        self.assertNotIn(".arete-sandbox", why)
        self.assertEqual(workspace.workspace_blockers(self.wt, [self.checkout]), ["personal.local"])
        self.assertEqual(user_file.read_text(), "user notes\n")

    def test_tracked_edit_inside_owned_sandbox_still_blocks(self):
        tracked = self.wt / ".arete-sandbox/tracked.txt"
        self.make_owned_sandbox()
        tracked.write_text("v1\n")
        util.git(["add", "-f", "--", ".arete-sandbox/tracked.txt"], self.wt)
        util.git_as_owner(["commit", "-qm", "Track file inside sandbox"], self.wt)
        tracked.write_text("v2\n")
        why = workspace.kept_checkout(self.wt, self.checkout, self.src, [], commits=False)
        self.assertIn("uncommitted edits that arete has no record of leaving there", why)
        self.assertIn(".arete-sandbox/tracked.txt", why)

    def test_multipart_workspace_root_sandbox_ownership(self):
        ws = self.worktrees / "multi-1029"
        part = workspace.Checkout("api", "owner/api", self.src, ws / "api", "main")
        workspace.add_worktree(part, "fix/1029-multi", "main")
        workspaces.claim(ws, "multi", "1029", "run-2", checkouts=[part])

        self.make_owned_sandbox(ws)
        self.assertEqual(workspace.kept_work(ws, [part], self.src, [], commits=False), "")
        self.assertEqual(workspace.workspace_blockers(ws, [part]), [])

        (ws / local_paths.VERIFY_SCRATCH_ROOT / local_paths.VERIFY_SCRATCH_MARKER).unlink()
        why = workspace.kept_work(ws, [part], self.src, [], commits=False)
        self.assertIn("files outside this repo's worktrees", why)
        self.assertTrue(workspace.workspace_blockers(ws, [part]))

    def test_note_made_skips_unreadable_owned_sandbox_without_wiping_snapshot(self):
        sandbox_dir = self.make_owned_sandbox()
        locked = sandbox_dir / "state/unreadable.bin"
        locked.write_text("secret\n")
        locked.chmod(0)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o600))
        cached = self.wt / ".cache/tool.dat"
        cached.parent.mkdir()
        cached.write_text("cached\n")

        workspace.note_made(self.wt, [self.checkout])
        made = workspace.made_before(self.wt)
        self.assertEqual(workspace.head_recorded(made, self.checkout), util.git(["rev-parse", "HEAD"], self.wt).strip())
        self.assertIn(".cache/", made)
        self.assertNotIn(".arete-sandbox/", made)

    def test_use_product_and_checks_initialize_sandbox_and_record_ignored_outputs(self):
        run_dir = self.state / "runs/run-1"
        run_dir.mkdir(parents=True)
        repo = config.Repo("fixture", self.src)
        run = pipeline.Run("run-1", repo, run_dir, tracker=mock.Mock(), tid="1029")
        run.workspace = self.wt, [self.checkout]
        self.enterContext(mock.patch.object(run, "log"))
        ticket = tracker.Ticket("1029", "#1029", "Track sandbox artifacts")
        layer = config.RepoLayer(
            self.root,
            checks={"unit": "sandbox echo unit"},
            verify=config.Verify(url="", allow_bash=["sandbox *"]),
        )
        work = pipeline.TicketWork(config.Config(), run, ticket, self.wt, [self.checkout], layer, 60, "test workspace")

        def interrupted_verify(*_args, **_kwargs):
            (self.wt / ".arete-sandbox/verifier.log").write_text("partial\n")
            (self.wt / ".cache").mkdir(exist_ok=True)
            (self.wt / ".cache/verify.dat").write_text("ignored cache\n")
            raise RuntimeError("interrupted verifier")

        with (mock.patch.object(paths, "dashboard_ports", return_value=frozenset()),
              mock.patch.object(scratch, "make", side_effect=ValueError("no scratch")),
              mock.patch.object(run, "agent", side_effect=interrupted_verify),
              self.assertRaisesRegex(RuntimeError, "interrupted verifier")):
            pipeline.use_product(work, "verify", "verify", goal="Check sandbox", summary="", diff="")

        self.assertTrue(local_paths.verify_sandbox_owned(self.wt))
        self.assertEqual(stat.S_IMODE((self.wt / ".arete-sandbox").stat().st_mode), 0o700)
        self.assertIn(".cache/", workspace.made_before(self.wt))
        self.assertEqual(workspace.kept_work(self.wt, [self.checkout], self.src, [], commits=False), "")

        # Checks also record newly created ignored files even when a check fails.
        def failing_check(*_args, **_kwargs):
            (self.wt / ".cache/check.dat").write_text("check cache\n")
            return SimpleNamespace(returncode=1, stdout="", stderr="failed")

        with (mock.patch.object(pipeline, "check_lane", return_value=mock.MagicMock(__enter__=lambda s: None, __exit__=lambda *a: None)),
              mock.patch.object(environment, "sh", side_effect=failing_check)):
            ok, _ = pipeline.run_checks(layer, self.wt, [self.wt], run, 30)
        self.assertFalse(ok)
        self.assertEqual(workspace.kept_work(self.wt, [self.checkout], self.src, [], commits=False), "")
        self.assertEqual(workspace.workspace_blockers(self.wt, [self.checkout]), [])
        with sqlite3.connect(paths.DB_FILE) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM workspaces WHERE name = ?", (self.wt.name,)).fetchone())
