import fcntl
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import arete

from arete import config, gh, outbound, paths, pi, pipeline, scratch, util, verify, runner

from tests import support


def setUpModule():
    support.isolate_live_state()


class WriterRegressionTests(unittest.TestCase):
    def write(self, brief, answer, *, voice=""):
        from arete import document, outbound
        from arete.agent import Result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "voice.txt"
            sample.write_text(voice)
            cfg = config.Config(document={"enabled": True, "voice": (str(sample),) if voice else ()},
                                steps={"document": {"tries": 1}})
            with mock.patch.object(document.runner, "run", return_value=Result(ok=True, structured={"text": answer})), \
                    mock.patch.multiple(document.paths, CONFIG_DIR=root, CONFIG_FILE=root / "config.toml"), \
                    mock.patch.object(outbound, "own_hosts", []):
                return document.write(brief, cfg)

    def test_formatted_warning_and_section_bodies_survive_concise_edits(self):
        from arete import document
        sections = (("**Warning:**", "This command permanently deletes staging records."),
                    ("## Prerequisites", "- Stop the background writer.\n- Back up the fixture database."),
                    ("Warning\n-------", "This command permanently deletes staging records."),
                    ("**Expected output:**", "The terminal prints exactly `ready: true`."))
        for heading, contents in sections:
            with self.subTest(heading=heading):
                draft = f"# Recovery\n\n{heading}\n\n{contents}\n\n## Next step\n\nRun `arete steps`."
                candidate = draft.replace(contents, "")
                brief = document.Brief("doc", ("Explain the recovery command.",), "Shorten the guide.", draft=draft)
                result = self.write(brief, candidate)
                self.assertEqual(result.source, "draft")
                self.assertEqual(result.text, draft)
                self.assertTrue(result.faults)

    def test_code_spans_indentation_and_complete_commands_survive(self):
        from arete import document
        cases = (("Run `arete steps`.", "Run `arete steps --unsupported`."),
                 ("```python\nif ready:\n    run()\n```", "```python\nif ready:\nrun()\n```"),
                 ("Example:\n\n    if ready:\n        run()", "Example:\n\n    if ready:\n    run()"),
                 ("Use ``echo `date` ``.", "Use ``echo `date` --unsupported``."))
        for draft, candidate in cases:
            with self.subTest(draft=draft):
                brief = document.Brief("doc", ("Keep the executable example.",), "Shorten the guide.", draft=draft)
                result = self.write(brief, candidate)
                self.assertEqual(result.source, "draft")
                self.assertEqual(result.text, draft)

    def test_code_can_move_to_a_fence_without_being_rejected(self):
        from arete import document
        brief = document.Brief("doc", ("Run `arete steps`.",), "Give the command.")
        result = self.write(brief, "```sh\narete steps\n```")
        self.assertEqual(result.source, "writer")
        self.assertFalse(result.faults)

    def test_private_voice_examples_do_not_block_original_factual_prose(self):
        from arete import document
        brief = document.Brief("pr", ("Offline checks are enabled.",), "Summarize the change.")
        result = self.write(brief, "Enabled offline checks.", voice="Juniper lanterns guide our release ceremony.")
        self.assertEqual(result.source, "writer")
        self.assertFalse(result.faults)

    def test_supplied_owner_and_deadline_require_complete_values(self):
        from arete import document
        brief = document.Brief("message", ("Offline checks passed.",), "Ask for review.", ask="Please review",
                               owner="Ann", deadline="May 1", draft="Please review, Ann, by May 1.")
        result = self.write(brief, "Please review, Joanne, by May 10. Offline checks passed.")
        self.assertEqual(result.source, "draft")
        self.assertEqual(result.text, brief.draft)

    def test_private_voice_copy_cannot_reach_github_even_with_punctuation_changed(self):
        from arete import document, gh, outbound, record, steps, tracker
        from arete.agent import Result
        sample = "Juniper lanterns guide our midnight release ceremony."
        for candidate in (sample.rstrip("."), sample.lower().replace(".", "!")):
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as directory:
                root, sent = Path(directory), []
                voice = root / "voice.txt"
                voice.write_text(sample)
                cfg = config.Config(document={"enabled": True, "voice": (str(voice),)},
                                    steps={"document": {"tries": 1}})
                setting = steps.resolve(cfg, None, "document")
                repo = config.Repo(name="fixture", path=root, public=True)
                run = SimpleNamespace(id="fixture-privacy", dir=root, repo=repo, feedback=False, repairs="",
                                      settings=lambda _: setting, log=lambda _: None, save=lambda *_: None,
                                      agent=lambda *_, **kw: Result(ok=True, structured={"text": candidate}))
                ticket = tracker.Ticket("1", "#1", "Enable offline checks", "Run the checks offline.")
                verified = verify.Verified("pass", "The offline check passed.",
                                           [{"what":"offline check", "how":"fixture", "saw":"pass", "passed":True}])
                review = {"verdict":"approve", "summary":"Offline behavior is correct.", "problems":[]}
                def transport(args, **kw):
                    sent.append(kw.get("input", ""))
                    return "https://github.com/fixture/repo/pull/1"
                with mock.patch.multiple(document.paths, CONFIG_DIR=root, CONFIG_FILE=root / "config.toml"), \
                        mock.patch.object(outbound, "own_hosts", []), \
                        mock.patch.object(record, "metered_spent", return_value=0), \
                        mock.patch.object(gh, "gh", side_effect=transport):
                    text = document.pr_text(run, ticket, "Enabled offline checks.", review, verified,
                                            "1 file, 1 line", "", cfg, None)
                    body = pipeline.report_body(run, summary="Enabled offline checks.", review=review, verified=verified,
                                                left="", images={}, approval=None, attempts=1, checks="offline fixture",
                                                plan_text="Enable offline checks.", size="1 file, 1 line", written=text)
                    gh.pr_create("fixture/repo", "fixture", "main", "Enable offline checks", body)
                self.assertEqual(len(sent), 1)
                self.assertNotIn(candidate, sent[0])
                self.assertIn("Enabled offline checks.", sent[0])


@unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK) and os.getuid() != 0,
                     "needs macOS confinement")


class AuthorityScopeTests(unittest.TestCase):
    def setUp(self):
        from arete import scratch
        self.scratch = scratch
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        if scratch.filesystem(os.open(self.root, os.O_RDONLY)) != "apfs":
            self.skipTest("needs APFS")
        (self.root / "home").mkdir()
        for patch in (mock.patch.object(scratch, "AUTHORITY", self.root / "home/verification-authority"),
                      mock.patch.object(scratch, "holders", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        self.ws, self.other = self.root / "worktrees/a", self.root / "worktrees/b"
        self.ws.mkdir(parents=True)
        self.other.mkdir()
        self.made = []
        self.addCleanup(self.release)

    def release(self):
        for a in self.made:
            fd = self.scratch.LEASES.pop(a.selector, None)
            if fd is not None:
                os.close(fd)
            self.scratch.RUN_BASELINES.pop(a.run_id, None)

    def attempt(self, ws=None):
        run_id = f"run-{len(self.made)}"
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        self.scratch.RUN_BASELINES[run_id] = {}
        a = self.scratch.make(run_id, "verify", ws or self.ws, run_dir)
        self.made.append(a)
        return a

    def finish(self, a):
        self.scratch.end(a, Path(a.run_dir))

    def file(self, a):
        return self.scratch.AUTHORITY / (a.selector + ".json")

    def sign(self, a, key, change=lambda data: None):
        envelope = json.loads(self.file(a).read_bytes())
        change(envelope["data"])
        envelope["signature"] = hmac.digest(key, self.scratch.encoded(envelope["data"]), "sha256").hex()
        self.file(a).write_bytes(self.scratch.encoded(envelope))

    def key(self, name="signing.secret"):
        return (self.scratch.AUTHORITY / name).read_bytes()

    def test_other_version_holds_only_its_workspace(self):
        a = self.attempt()
        self.sign(a, self.key(), lambda data: data.update(version=3))
        self.assertTrue(self.scratch.pending(self.ws))
        self.assertFalse(self.scratch.pending(self.other))
        self.attempt(self.other)
        (unreadable,) = self.scratch.survey().unreadable
        self.assertEqual((unreadable.path, unreadable.authenticated), (str(self.file(a)), True))
        with self.assertRaises(OSError) as raised:
            self.attempt()
        self.assertIn(str(self.file(a)), str(raised.exception))

    def test_record_without_a_usable_signature_still_holds_only_its_workspace(self):
        a = self.attempt()
        for signature in (None, 7):
            envelope = json.loads(self.file(a).read_bytes())
            envelope.pop("signature", None)
            if signature is not None:
                envelope["signature"] = signature
            self.file(a).write_bytes(self.scratch.encoded(envelope))
            self.assertTrue(self.scratch.pending(self.ws))
            self.assertFalse(self.scratch.pending(self.other))

    def test_record_with_an_invalid_selector_name_holds_even_when_it_claims_done(self):
        envelope = {"data": {"version": 2, "state": "done", "source_mutations": False,
                             "attempt": {"selector": "x", "workspace": str(self.ws), "root": str(self.ws / ".gone")}},
                    "signature": "0"}
        self.finish(self.attempt(self.other))
        (self.scratch.AUTHORITY / "x.json").write_bytes(self.scratch.encoded(envelope))
        self.assertTrue(self.scratch.pending(self.ws))
        self.assertFalse(self.scratch.pending(self.other))

    def test_lost_signing_secret_scopes_holds(self):
        self.finish(self.attempt())
        self.attempt()
        (self.scratch.AUTHORITY / "signing.secret").unlink()
        with self.assertRaises(PermissionError) as raised:
            self.attempt(self.other)
        self.assertIn(str(self.scratch.AUTHORITY), str(raised.exception))
        self.assertIn("2 records", str(raised.exception))
        self.assertFalse((self.scratch.AUTHORITY / "signing.secret").exists())
        self.assertFalse(self.scratch.pending(self.other))
        self.assertTrue(self.scratch.pending(self.ws))

    def test_interrupted_rotation_is_named(self):
        self.finish(self.attempt())
        b = self.attempt(self.other)
        following = secrets.token_bytes(32)
        (self.scratch.AUTHORITY / "signing.secret.next").write_bytes(following)
        (self.scratch.AUTHORITY / "signing.secret.next").chmod(0o600)
        self.sign(b, following)
        self.assertFalse(self.scratch.pending(self.ws))
        self.assertTrue(self.scratch.pending(self.other))
        (unreadable,) = self.scratch.survey().unreadable
        self.assertIn("arete rotate-authority", unreadable.reason)
        self.assertIn("again", unreadable.reason)

    def test_store_on_host_that_cannot_confine(self):
        a = self.attempt()
        with mock.patch.object(pi, "can_confine", return_value=False):
            self.assertFalse(self.scratch.pending(self.other))
            self.assertTrue(self.scratch.pending(self.ws))
            self.assertIn(Path(a.root).name, self.scratch.root_names())

    def test_oversized_record_is_refused(self):
        a = self.attempt()
        before = self.file(a).read_bytes()
        with self.scratch.registry() as (fd, key):
            data = self.scratch.load(fd, key, a.selector)
            data["diagnostic"] = "x" * (self.scratch.MAX_RECORD_BYTES + 1)
            with self.assertRaises(ValueError):
                self.scratch.store(fd, key, data)
            self.assertEqual(self.scratch.load(fd, key, a.selector)["state"], "prepared")
        self.assertEqual(self.file(a).read_bytes(), before)

    def test_settle_moves_then_prunes(self):
        done = []
        for _ in range(3):
            done.append(self.attempt())
            self.finish(done[-1])
        locked = self.attempt()
        self.finish(locked)
        kept = self.attempt()
        old = time.time() - 7200
        for a in (*done, locked, kept):
            os.utime(self.file(a), (old, old))
        holder = os.open(self.scratch.AUTHORITY / (locked.selector + ".lease"), os.O_RDWR)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.scratch.settle(30), [])
        finally:
            os.close(holder)
        settled = self.scratch.AUTHORITY / "settled"
        self.assertEqual(sorted(p.name for p in settled.iterdir()), sorted(a.selector + ".json" for a in done))
        self.assertTrue(self.file(locked).exists() and self.file(kept).exists())
        self.assertEqual({r["attempt"]["selector"] for r in self.scratch.records()}, {locked.selector, kept.selector})
        self.assertTrue(self.scratch.pending(self.ws))
        expired = done[0]
        sentinel = self.root / "sentinel"
        sentinel.mkdir()
        (sentinel / "keep.txt").write_text("keep")
        evidence = self.scratch.AUTHORITY / expired.selector
        self.assertTrue(any(p.stat().st_mode & 0o777 == 0o500 for p in evidence.rglob("*") if p.is_dir()))
        os.symlink(sentinel, evidence / "link")
        long_ago = time.time() - 31 * 86400
        os.utime(settled / (expired.selector + ".json"), (long_ago, long_ago))
        self.assertEqual(self.scratch.settle(30), [])
        self.assertFalse((settled / (expired.selector + ".json")).exists())
        self.assertFalse(evidence.exists() or (self.scratch.AUTHORITY / (expired.selector + ".lease")).exists())
        self.assertEqual((sentinel / "keep.txt").read_text(), "keep")
        self.assertTrue((settled / (done[1].selector + ".json")).exists())
        self.assertTrue((self.scratch.AUTHORITY / done[1].selector).exists())


@unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK) and os.getuid() != 0,
                     "needs macOS confinement")


class VerificationOutputTests(unittest.TestCase):
    def setUp(self):
        import ctypes
        import struct
        self.ctypes, self.struct = ctypes, struct
        from arete import scratch
        self.scratch = scratch
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        if scratch.filesystem(os.open(self.root, os.O_RDONLY)) != "apfs":
            self.skipTest("needs APFS")
        (self.root / "home").mkdir()
        for patch in (mock.patch.object(scratch, "AUTHORITY", self.root / "home/verification-authority"),
                      mock.patch.object(scratch, "holders", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        self.ws = self.root / "worktrees/ticket"
        (self.ws / "owner-dir").mkdir(parents=True)
        (self.ws / "owner.txt").write_text("owner data\n")
        (self.ws / "owner-dir/kept.txt").write_text("kept\n")
        self.made = []
        self.addCleanup(self.release)

    def release(self):
        for a in self.made:
            fd = self.scratch.LEASES.pop(a.selector, None)
            if fd is not None:
                os.close(fd)
            self.scratch.RUN_BASELINES.pop(a.run_id, None)

    def attempt(self):
        run_id = f"run-{len(self.made)}"
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        self.scratch.RUN_BASELINES[run_id] = {}
        a = self.scratch.make(run_id, "verify", self.ws, run_dir)
        self.made.append(a)
        return a

    def record(self, a):
        return next(r for r in self.scratch.records() if r["attempt"]["selector"] == a.selector)

    def write(self, a, name, text="{}"):
        target = Path(a.root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_created_entries_are_archived_and_workspace_is_free(self):
        a = self.attempt()
        for name in ("config/predictions.jsonl", "fixture-a/nested/case.json", "out/scenario-1/result.json"):
            self.write(a, name)
        os.symlink(self.ws / "owner.txt", Path(a.root) / "fixture-a/link-to-file")
        os.symlink(self.ws / "owner-dir", Path(a.root) / "fixture-a/link-to-dir")
        self.assertEqual(self.scratch.end(a, Path(a.run_dir))[2], [])
        data = self.record(a)
        self.assertEqual((data["state"], data["diagnostic"]), ("done", ""))
        tree = self.scratch.AUTHORITY / a.selector / data["final_tree"]
        for name in ("config/predictions.jsonl", "fixture-a/nested/case.json", "out/scenario-1/result.json"):
            self.assertTrue((tree / name).exists())
        self.assertTrue((tree / "fixture-a/link-to-file").is_symlink())
        self.assertTrue((tree / "fixture-a/link-to-dir").is_symlink())
        self.assertEqual((self.ws / "owner.txt").read_text(), "owner data\n")
        self.assertEqual((self.ws / "owner-dir/kept.txt").read_text(), "kept\n")
        self.assertFalse(self.scratch.pending(self.ws))
        self.assertEqual(pipeline.scratch_left(self.ws, []), [])
        self.attempt()

    def test_look_alike_is_free_and_a_folder_made_again_at_the_recorded_root_is_held(self):
        a = self.attempt()
        self.write(a, "out/output.txt", "fake")
        self.assertEqual(self.scratch.end(a, Path(a.run_dir))[2], [])
        self.assertFalse(os.path.lexists(a.root))
        look = self.ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
        look.mkdir(mode=0o700)
        (look / ".gitignore").write_text("*\n")
        self.assertTrue(paths.valid_scratch(str(look)))
        self.assertEqual(pipeline.scratch_left(self.ws, []), [])
        Path(a.root).mkdir(mode=0o700)
        self.assertEqual(pipeline.scratch_left(self.ws, []), [Path(a.root)])

    def retained(self, a, name):
        self.assertEqual(self.scratch.end(a, Path(a.run_dir))[2], [a.root])
        data = self.record(a)
        self.assertEqual(data["state"], "pending")
        return data["diagnostic"]

    def test_owner_file_moved_to_root_is_retained(self):
        a = self.attempt()
        os.rename(self.ws / "owner.txt", Path(a.root) / "owner.txt")
        diagnostic = self.retained(a, "owner.txt")
        self.assertEqual(diagnostic["unknown_entries"], ["owner.txt"])
        self.assertEqual(diagnostic["reasons"]["owner.txt"], self.scratch.EXISTED)
        self.assertEqual((Path(a.root) / "owner.txt").read_text(), "owner data\n")
        self.assertTrue(pipeline.scratch_left(self.ws, []))
        with self.assertRaises(OSError) as raised:
            self.attempt()
        self.assertEqual(raised.exception.errno, 16)

    def test_owner_file_in_created_folder_is_named(self):
        a = self.attempt()
        self.write(a, "fixture-a/nested/case.json")
        os.rename(self.ws / "owner.txt", Path(a.root) / "fixture-a/nested/owner.txt")
        self.assertEqual(self.retained(a, "")["unknown_entries"], ["fixture-a/nested/owner.txt"])

    def test_hard_link_is_retained(self):
        a = self.attempt()
        os.link(self.ws / "owner.txt", Path(a.root) / "config/linked.txt")
        diagnostic = self.retained(a, "")
        self.assertEqual(diagnostic["reasons"], {"config/linked.txt": self.scratch.LINKED})

    def test_forged_birth_time_is_retained(self):
        a = self.attempt()
        target = Path(a.root) / "fixture-a/owner.txt"
        target.parent.mkdir()
        os.rename(self.ws / "owner.txt", target)
        later = int(time.time()) + 3600
        libc = self.ctypes.CDLL(None, use_errno=True)
        libc.setattrlist.argtypes = (self.ctypes.c_char_p, self.ctypes.c_char_p, self.ctypes.c_char_p,
                                     self.ctypes.c_size_t, self.ctypes.c_uint)
        attrs = self.struct.pack("HHIIIII", 5, 0, 0x200, 0, 0, 0, 0)
        self.assertEqual(libc.setattrlist(os.fsencode(target), attrs, self.struct.pack("ll", later, 0), 16, 0), 0)
        self.assertEqual(int(os.lstat(target).st_birthtime), later)
        self.assertEqual(self.retained(a, "")["unknown_entries"], ["fixture-a/owner.txt"])

    def test_record_from_before_the_change_finalizes_on_poll(self):
        a = self.attempt()
        self.write(a, "config/predictions.jsonl")
        self.write(a, "fixture-a/case.json")
        with self.scratch.registry() as (fd, key):
            data = self.scratch.load(fd, key, a.selector)
            data["state"] = "pending"
            data["diagnostic"] = {"retained_root": a.root, "unknown_entries": ["config/predictions.jsonl", "fixture-a"]}
            self.scratch.store(fd, key, data)
        os.close(self.scratch.LEASES.pop(a.selector))
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pipeline.retry_scratch()
        self.assertIn(a.root, out.getvalue())
        data = self.record(a)
        self.assertEqual(data["state"], "done")
        self.assertTrue((self.scratch.AUTHORITY / a.selector / data["final_tree"] / "fixture-a/case.json").exists())

    def test_unclassifiable_filesystem_is_retained(self):
        a = self.attempt()
        self.write(a, "config/new.json")
        with mock.patch.object(self.scratch, "filesystem", return_value="hfs"):
            diagnostic = self.retained(a, "")
        self.assertTrue(diagnostic["reasons"]["config/new.json"].startswith(self.scratch.UNCLASSIFIED))
        st = SimpleNamespace(st_dev=2, st_ino=9, st_nlink=1, st_mode=0o100600)
        self.assertEqual(self.scratch.refusal(st, 1, 5), self.scratch.ELSEWHERE)

    def test_layout_is_still_protected(self):
        self.assertEqual(self.scratch.end(self.attempt(), Path(self.made[0].run_dir))[2], [])
        a = self.attempt()
        os.unlink(Path(a.root) / "out/output.txt")
        self.assertEqual(self.scratch.end(a, Path(a.run_dir))[2], [a.root])
        self.assertEqual(self.record(a)["state"], "pending")
        self.assertTrue(Path(a.root).exists())


@unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK) and os.getuid() != 0,
                     "needs macOS confinement")


class EvidenceLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        (self.evidence / "ok.png").write_bytes(b"png")
        outside = self.root / "outside.txt"
        outside.write_text("secret")
        (self.evidence / "leak.png").symlink_to(outside)
        self.verified = verify.Verified("pass", "s", [
            {"what": "a", "how": "b", "saw": "c", "passed": True, "screenshot": name}
            for name in ("ok.png", "leak.png")])

    def test_screenshots_skip_links(self):
        self.assertEqual(self.verified.screenshots(self.evidence), [self.evidence / "ok.png"])

    def test_linked_evidence_folders_and_ancestors_are_not_followed(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "ok.png").write_bytes(b"outside")
        (self.root / "linked").symlink_to(elsewhere)
        (self.root / "real" / "run").mkdir(parents=True)
        (self.root / "real" / "run" / "evidence").symlink_to(elsewhere)
        (self.root / "ancestor").symlink_to(self.root / "real")
        for folder in (self.root / "linked", self.root / "ancestor" / "run" / "evidence"):
            with self.subTest(str(folder)):
                self.assertEqual(self.verified.screenshots(folder), [])
                self.assertIsNone(verify.evidence_bytes(folder, "ok.png"))
                with mock.patch.object(verify.subprocess, "run", side_effect=AssertionError("hashed")):
                    self.assertEqual(verify.push_evidence(self.root, "o/r", "run", [folder / "ok.png"]), {})
        self.verified.saved = str(self.root / "ancestor" / "run" / "verify.md")
        self.assertEqual([r["evidence"] for r in self.verified.requirements()], [self.verified.saved] * 2)

    def test_evidence_of_skips_links(self):
        self.verified.saved = str(self.root / "verify.md")
        evidence = [r["evidence"] for r in self.verified.requirements()]
        self.assertEqual(evidence, [str(self.evidence / "ok.png"), str(self.root / "verify.md")])

    def test_push_evidence_hashes_opened_bytes_and_never_a_link(self):
        done = SimpleNamespace(returncode=0, stdout=b"abc\n", stderr=b"")
        with mock.patch.object(verify.subprocess, "run", return_value=done) as run, \
                mock.patch.object(verify, "git", return_value=""), \
                mock.patch.object(verify.util, "git_as_owner", return_value="c0ffee"):
            urls = verify.push_evidence(self.root, "o/r", "run", [self.evidence / "leak.png", self.evidence / "ok.png"])
        self.assertEqual(list(urls), ["ok.png"])
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["input"], b"png")
        self.assertIn("--stdin", run.call_args.args[0])
        with mock.patch.object(verify.subprocess, "run", side_effect=AssertionError("no hashing")):
            self.assertEqual(verify.push_evidence(self.root, "o/r", "run", [self.evidence / "leak.png"]), {})

    def test_the_claude_browser_config_keeps_chromes_own_sandbox(self):
        config_text = verify.mcp_config(self.evidence)
        self.assertNotIn("--no-sandbox", config_text)
        self.assertIn("npx", config_text)
        self.assertNotIn("browser_evaluate", verify.PLAYWRIGHT_TOOLS)
        self.assertNotIn("browser_run_code_unsafe", verify.PLAYWRIGHT_TOOLS)


if __name__ == "__main__":
    unittest.main()

