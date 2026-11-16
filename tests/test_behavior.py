import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests import support
from tests.harness import SOURCE, behavior_result, bootstrap, make_folders, run_script, scratch_env


def setUpModule():
    support.isolate_live_state()


PRELUDE = textwrap.dedent("""
    import contextlib, errno, io, json, os, sys, time
    from collections import Counter
    from pathlib import Path
    from unittest import mock
    root = Path(sys.argv[1])
    (root / "config" / "config.toml").write_text("")
    from arete import control, heartbeat, install, log_follower, oncall, paths, serve, util
    logs = paths.LOG_DIR
    p = logs / "runs.log"

    def snapshot():
        return {f.name: [f.read_bytes().decode(), f.stat().st_ino] for f in sorted(logs.iterdir())}

    def finish(result):
        print("BEHAVIOR_RESULT=" + json.dumps(result))
""")


def environment(root):
    make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
    return scratch_env(root, ARETE_WORKTREES_DIR=str(root / "worktrees"), TMPDIR=str(root / "tmp"),
                       HOME=str(root / "home"))


class StoreContractTests(unittest.TestCase):
    def scenario(self, case):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            done = subprocess.run(
                [sys.executable, "-I", str(SOURCE / "tests/scripts/store_contract.py"), case],
                cwd=SOURCE, env=environment(root), text=True, capture_output=True, timeout=180)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            found = behavior_result(done)
            self.assertEqual(found["case"], case)
            self.assertEqual(len(set(found["isolated_paths"].values())), 6)
            return found

    def test_unique_table_ownership_and_compatibility(self):
        found = self.scenario("ownership")
        self.assertTrue(found["complete"] and found["unique"] and found["record_compatibility"])
        self.assertTrue(found["ledger_unchanged"])
        self.assertEqual(found["foundation"], "db")
        self.assertTrue(found["aggregate_compatibility"])
        self.assertEqual(found["loop_lock_owner"], "loops")
        self.assertEqual(found["readers"]["state"], {"remember": 7})
        self.assertTrue(found["private_helper_excluded"])
        self.assertEqual(set(found["implementations"]),
                         {"approvals", "asks", "chats", "devices", "insights", "push", "tasks", "triage", "_rows"})

    def test_record_readers_and_billing(self):
        found = self.scenario("record-readers")
        self.assertEqual(found["paid"], 2.25)
        self.assertEqual(found["finished_ids"], ["readers"])
        self.assertEqual(found["whole_ids"], ["second", "readers"])
        self.assertTrue(all(found[k] for k in ("stamps", "steps", "reports", "feed", "pins", "backfill")))

    def test_aggregate_joins_and_approval_transaction(self):
        found = self.scenario("aggregate-joins")
        self.assertTrue(found["baseline_compatible"])
        self.assertEqual(found["inventories"], found["baseline_inventories"])
        self.assertEqual(set(found["queries"]), {"approvals", "vms_left_over", "open_asks", "feedback_due"})
        self.assertTrue(all(row["reader"] and row["listing_selects"] == 1 for row in found["queries"].values()))
        transaction = found["approval_transactions"]
        self.assertEqual(transaction["before"], transaction["after_failure"])
        self.assertEqual(transaction["before"], transaction["after_rejected"])
        self.assertTrue(all(transaction[key] for key in ("rollback", "rejected_unchanged", "unjudged_unchanged", "settled")))

    def test_record_failures_roll_back_and_settle_once(self):
        found = self.scenario("record-failures")
        self.assertTrue(all(found[k] for k in ("collision_retry", "explicit_collision", "batch_rollback",
                                             "end_rollback", "repeated_settle", "missing_settle",
                                             "committed_timeline_failure", "invalid_unchanged", "crash_reraised")))

    def test_run_retention_protections_and_atomicity(self):
        found = self.scenario("run-retention")
        self.assertEqual(found["deleted_ids"], ["old-done", "old-skipped"])
        self.assertTrue(all(found[k] for k in ("rollback", "history_retained", "merged_raw_kept",
                                             "custom_windows", "deletion_retry", "unsafe_refused")))
        self.assertTrue(found["baseline_compatible"])
        self.assertEqual(found["rollback_before"], found["rollback_after"])
        self.assertEqual(found["rollback_failure"], "incidents prune sentinel")
        self.assertEqual(len(found["retention"]["queue"]), 8)

    def test_scan_writes_are_ordered_and_atomic(self):
        found = self.scenario("scan-atomicity")
        self.assertEqual(found["opened"], [False, True, False, True])
        self.assertTrue(found["baseline_compatible"] and found["rollback"] and found["new_only_marks"])
        self.assertEqual(found["before"], found["after_failure"])
        self.assertEqual(found["cursor_detail_length"], 500)
        self.assertEqual(found["failure"], "later cursor sentinel")

    def test_server_prunes_once_after_backup_and_continues_cleanup(self):
        found = self.scenario("server-prune")
        self.assertTrue(found["backup_checked"] and found["snapshot_failure_skipped"] and found["health_requested"])
        self.assertEqual(found["prune_calls"], [{"lines_days": 17, "keep_days": 9}])
        self.assertEqual(found["phases"], ["copy", "store", "folders", "records", "rotate"])
        self.assertEqual(found["continuations"], {"database sentinel": ["folders", "records", "rotate"],
                                                "record sentinel": ["folders", "rotate"]})

    def test_unknown_areas_cannot_execute_or_mutate(self):
        found = self.scenario("unknown-area")
        self.assertEqual(set(found["refusals"]), {"outbox", "helper", "work"})
        self.assertFalse(found["executed"])
        self.assertTrue(found["schema_unchanged"] and found["ledger_unchanged"])
        self.assertEqual(set(found["private_refusals"]), {"_rows"})

    def test_immutable_historical_chains_upgrade_once(self):
        found = self.scenario("upgrade")
        self.assertEqual(found["historical_rows"], 2)
        self.assertTrue(found["hashes_unchanged"] and found["historical_work_preserved"])
        self.assertEqual(found["applied_once"], [3, 4])
        self.assertEqual((found["relocated_rows_added"], found["origin_main_rows_added"]), (0, 0))


class LogRotationTests(unittest.TestCase):
    def scenario(self, body, timeout=180):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = environment(root)
            result = run_script(PRELUDE + textwrap.dedent(body), root, str(root), env=env, timeout=timeout)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return behavior_result(result)

    def test_four_unthrottled_writers_lose_no_line_to_rotation(self):
        found = self.scenario("""
            procs = []
            try:
                for g in range(4):
                    command = [sys.executable, "-u", "-c", f'for i in range(200000): print("{g}:" + str(i))']
                    procs.append(control.launch(command, "runs.log"))
                    deadline = time.monotonic() + 30
                    while f"{g}:0\\n" not in (p.read_text() if p.exists() else ""):
                        assert time.monotonic() < deadline, "no first line"
                        time.sleep(0.01)
                    assert procs[-1].poll() is None, "writer ended before rotation"
                    notes = install.rotate_logs(max_bytes=1, keep=5)
                    assert notes == [], notes
                codes = [proc.wait(timeout=120) for proc in procs]
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
            counted = Counter()
            for f in [p, *(logs / f"runs.log.{n}" for n in range(1, 5))]:
                if f.exists():
                    counted.update(f.read_text().splitlines())
            expected = {f"{g}:{i}" for g in range(4) for i in range(200000)}
            finish({"codes": codes, "missing": len(expected - set(counted)), "extra": len(set(counted) - expected),
                    "repeated": sum(1 for n in counted.values() if n != 1), "total": sum(counted.values()),
                    "no_fifth": not (logs / "runs.log.5").exists()})
        """)
        self.assertEqual(found["codes"], [0, 0, 0, 0])
        self.assertEqual((found["missing"], found["extra"], found["repeated"]), (0, 0, 0))
        self.assertEqual(found["total"], 800000)
        self.assertTrue(found["no_fifth"])

    def test_six_rotations_keep_five_numbered_generations(self):
        found = self.scenario("""
            for n in range(1, 7):
                p.write_text(f"generation {n}\\n")
                assert install.rotate_logs(max_bytes=1, keep=5) == []
            finish({"kept": [(logs / f"runs.log.{n}").read_text() for n in range(1, 6)],
                    "sixth": (logs / "runs.log.6").exists(), "active": p.exists()})
        """)
        self.assertEqual(found["kept"], [f"generation {n}\n" for n in (6, 5, 4, 3, 2)])
        self.assertFalse(found["sixth"])
        self.assertFalse(found["active"])

    def test_threshold_keep_one_other_logs_and_empty_folders(self):
        found = self.scenario("""
            empty = install.rotate_logs()
            (logs / "hook-errors.log").write_text("12345")
            p.write_text("12345")
            at = install.rotate_logs(max_bytes=5)
            before = sorted(f.name for f in logs.iterdir())
            p.write_text("123456")
            (logs / "hook-errors.log").write_text("123456")
            over = install.rotate_logs(max_bytes=5, keep=1)
            p.write_text("newer!!")
            install.rotate_logs(max_bytes=5, keep=1)
            try:
                install.rotate_logs(keep=0)
                refused = False
            except ValueError:
                refused = True
            finish({"empty": empty, "at": at, "before": before, "over": over, "after": sorted(f.name for f in logs.iterdir()),
                    "kept": (logs / "runs.log.1").read_text(), "refused": refused})
        """)
        self.assertEqual((found["empty"], found["at"], found["over"]), ([], [], []))
        self.assertEqual(found["before"], ["hook-errors.log", "runs.log"])
        self.assertEqual(found["after"], ["hook-errors.log.1", "runs.log.1"])
        self.assertEqual(found["kept"], "newer!!")
        self.assertTrue(found["refused"])

    def test_a_failed_final_rename_loses_nothing_and_is_said(self):
        found = self.scenario("""
            real = os.replace

            def seed():
                for f in logs.iterdir():
                    f.unlink()
                p.write_text("current")
                for n in range(1, 6):
                    (logs / f"runs.log.{n}").write_text(f"old {n}")

            def full(src, dst, *a, **k):
                if Path(src) == p:
                    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
                return real(src, dst, *a, **k)

            seed()
            with mock.patch.object(install.os, "replace", side_effect=full):
                notes = install.rotate_logs(max_bytes=1)
            shifted = [(logs / f"runs.log.{n}").read_text() if (logs / f"runs.log.{n}").exists() else None
                       for n in range(1, 6)]
            current = p.read_text()
            seed()
            p.write_text("x" * 5_000_001)
            out = io.StringIO()
            server = serve.Server()
            try:
                with mock.patch.object(install.os, "replace", side_effect=full), contextlib.redirect_stdout(out):
                    server.rotate()
            finally:
                server.pool.shutdown()
                for pool in server.job_pools.values():
                    pool.shutdown()
            finish({"notes": notes, "shifted": shifted, "current": current, "printed": out.getvalue()})
        """)
        (note,) = found["notes"]
        self.assertIn("runs.log", note)
        self.assertIn("rename", note)
        self.assertIn("No space left on device", note)
        self.assertEqual(found["current"], "current")
        self.assertEqual(found["shifted"], [None, "old 1", "old 2", "old 3", "old 4"])
        self.assertEqual(found["printed"].count(note), 1)

    def test_a_failed_shift_stops_that_log_and_others_still_rotate(self):
        found = self.scenario("""
            real = os.replace

            def denied(src, dst, *a, **k):
                if Path(src).name == "runs.log.3":
                    raise OSError(errno.EACCES, os.strerror(errno.EACCES))
                return real(src, dst, *a, **k)

            other = logs / "hook-errors.log"
            p.write_text("current")
            other.write_text("other")
            for n in range(1, 6):
                (logs / f"runs.log.{n}").write_text(f"old {n}")
            with mock.patch.object(install.os, "replace", side_effect=denied):
                notes = install.rotate_logs(max_bytes=1)
            finish({"notes": notes, "files": {n: (logs / n).read_text() for n in sorted(os.listdir(logs))}})
        """)
        (note,) = found["notes"]
        self.assertIn("runs.log", note)
        self.assertIn("shift", note)
        files = found["files"]
        self.assertEqual(files["runs.log"], "current")
        self.assertEqual([files[f"runs.log.{n}"] for n in (1, 2, 3, 5)], ["old 1", "old 2", "old 3", "old 4"])
        self.assertNotIn("runs.log.4", files)
        self.assertEqual(files["hook-errors.log.1"], "other")

    def test_a_held_lock_renames_nothing(self):
        found = self.scenario("""
            p.write_text("held")
            (logs / "other.log").write_text("held")
            before = snapshot()
            with util.lock("rotate-logs", wait=False) as held:
                assert held
                notes = install.rotate_logs(max_bytes=1)
                during = snapshot()
            after_notes = install.rotate_logs(max_bytes=1)
            finish({"notes": notes, "same": before == during, "after": after_notes,
                    "names": sorted(os.listdir(logs))})
        """)
        self.assertEqual(len(found["notes"]), 1)
        self.assertIn("another rotation holds", found["notes"][0])
        self.assertTrue(found["same"])
        self.assertEqual(found["after"], [])
        self.assertEqual(found["names"], ["other.log.1", "runs.log.1"])

    def test_filesystem_errors_become_notes_and_lose_nothing(self):
        found = self.scenario("""
            real_stat = Path.stat

            def denied(error):
                return OSError(error, os.strerror(error))

            p.write_text("runs")
            other = logs / "other.log"
            other.write_text("other")
            before = snapshot()
            with mock.patch.object(util, "lock_file", side_effect=denied(errno.ENOSPC)):
                lock_notes = install.rotate_logs(max_bytes=1)
            lock_same = snapshot() == before

            def no_listing(path):
                raise denied(errno.EACCES)
            with mock.patch.object(install.os, "listdir", side_effect=no_listing):
                list_notes = install.rotate_logs(max_bytes=1)
            list_same = snapshot() == before

            def no_stat(self, *a, **k):
                if self.name == "other.log":
                    raise denied(errno.EACCES)
                return real_stat(self, *a, **k)
            with mock.patch.object(Path, "stat", no_stat):
                inspect_notes = install.rotate_logs(max_bytes=1)
            after = sorted(os.listdir(logs))
            finish({"lock": lock_notes, "lock_same": lock_same, "list": list_notes, "list_same": list_same,
                    "inspect": inspect_notes, "after": after, "other": other.read_text(),
                    "archived": (logs / "runs.log.1").read_text()})
        """)
        (note,) = found["lock"]
        self.assertIn("rotate-logs.lock", note)
        self.assertIn("No space left on device", note)
        self.assertTrue(found["lock_same"])
        (note,) = found["list"]
        self.assertIn("list failed", note)
        self.assertIn("Permission denied", note)
        self.assertTrue(found["list_same"])
        (note,) = found["inspect"]
        self.assertIn("other.log", note)
        self.assertIn("inspect", note)
        self.assertEqual(found["after"], ["other.log", "runs.log.1"])
        self.assertEqual(found["other"], "other")
        self.assertEqual(found["archived"], "runs")

    def test_own_log_inspection_and_close_failures_are_returned(self):
        found = self.scenario("""
            real_stat = Path.stat

            def denied_stat(self, *a, **k):
                if self.name == "serve.log":
                    raise OSError(errno.EACCES, os.strerror(errno.EACCES))
                return real_stat(self, *a, **k)
            with mock.patch.object(Path, "stat", denied_stat):
                inspect_notes = install.rotate_own_log(max_bytes=1)
            finish({"inspect": inspect_notes})
        """)
        (note,) = found["inspect"]
        self.assertIn("serve.log", note)
        self.assertIn("Permission denied", note)
        child = textwrap.dedent("""
            import errno, json, os, sys
            from unittest import mock
            from arete import install
            real_dup2, real_close = os.dup2, os.close
            opened = []

            def no_dup2(src, dst, *a, **k):
                raise OSError(errno.EBADF, os.strerror(errno.EBADF))

            def failing_close(fd):
                real_close(fd)
                if fd in opened:
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
            real_open = os.open

            def tracked_open(*a, **k):
                fd = real_open(*a, **k)
                opened.append(fd)
                return fd
            with mock.patch.object(install.os, "dup2", no_dup2), mock.patch.object(install.os, "close", failing_close), \\
                    mock.patch.object(install.os, "open", tracked_open):
                notes = install.rotate_own_log(max_bytes=1, keep=5)
            open(sys.argv[1] + "/notes.json", "w").write(json.dumps(notes))
        """)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = environment(root)
            log = root / "logs" / "serve.log"
            log.write_text("earlier\n")
            with open(log, "a") as handle:
                done = self.run_child(root, env, child, stdout=handle, stderr=handle)
            self.assertEqual(done.returncode, 0)
            notes = json.loads((root / "notes.json").read_text())
            self.assertEqual(len(notes), 2)
            self.assertIn("dup2 failed", notes[0])
            self.assertIn("close failed", notes[1])
            self.assertEqual((root / "logs" / "serve.log.1").read_text(), "earlier\n")

    def run_child(self, root, env, source, *, stdout, stderr):
        return subprocess.run([sys.executable, "-I", "-c", bootstrap(source), str(root)], cwd=root, env=env,
                              stdout=stdout, stderr=stderr, timeout=60)

    def test_serve_log_rotates_in_its_own_process_by_descriptor(self):
        child = textwrap.dedent("""
            import os, sys
            from arete import install, paths
            sys.stdout.write("pending\\n")
            notes = install.rotate_own_log(max_bytes=1, keep=5)
            print("new out", flush=True)
            print("new err", file=sys.stderr, flush=True)
            print("NOTES", notes, file=sys.stderr, flush=True)
        """)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = environment(root)
            log = root / "logs" / "serve.log"
            log.write_text("earlier\n")
            inode = log.stat().st_ino
            with open(log, "a") as handle:
                done = self.run_child(root, env, child, stdout=handle, stderr=handle)
            self.assertEqual(done.returncode, 0)
            archive = root / "logs" / "serve.log.1"
            self.assertEqual(archive.read_text(), "earlier\npending\n")
            self.assertEqual(archive.stat().st_ino, inode)
            self.assertNotEqual(log.stat().st_ino, inode)
            fresh = log.read_text()
            self.assertIn("new out\n", fresh)
            self.assertIn("new err\n", fresh)
            self.assertNotIn("new out", archive.read_text())

            archive.unlink()
            log.write_text("earlier\n")
            piped = self.run_child(root, env, child, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(piped.returncode, 0)
            self.assertEqual(log.read_text(), "earlier\n")
            self.assertFalse(archive.exists())

    def test_the_hourly_pass_never_renames_serve_log(self):
        found = self.scenario("""
            (logs / "serve.log").write_text("big")
            p.write_text("big")
            install.rotate_logs(max_bytes=1)
            finish(sorted(os.listdir(logs)))
        """)
        self.assertEqual(found, ["runs.log.1", "serve.log"])

    def test_oncall_follows_a_rotated_log_with_a_held_writer(self):
        found = self.scenario("""
            held_writer = p.open("a")
            first = [f"first {i}" for i in range(10)]
            held_writer.write("".join(line + "\\n" for line in first))
            held_writer.flush()
            older, current, mark = log_follower.fresh(str(p), None, False)
            seen_first = older + current
            assert install.rotate_logs(max_bytes=1, keep=5) == []
            inode_kept = (logs / "runs.log.1").stat().st_ino == mark[1]
            held_lines = [f"held {i}" for i in range(10)]
            held_writer.write("".join(line + "\\n" for line in held_lines))
            held_writer.flush()
            new_lines = [f"new {i}" for i in range(10)]
            with p.open("a") as w:
                w.write("".join(line + "\\n" for line in new_lines))
            older, current, mark2 = log_follower.fresh(str(p), mark, False)
            late_lines = [f"late {i}" for i in range(5)]
            held_writer.write("".join(line + "\\n" for line in late_lines))
            held_writer.close()
            late = log_follower.fresh(str(p), mark2, False)
            again = log_follower.fresh(str(p), late[2], False)
            finish({"first": seen_first == first, "inode": inode_kept, "got": older + current,
                    "want": held_lines + new_lines, "late": late[0] + late[1], "want_late": late_lines,
                    "again": [again[0], again[1]],
                    "listed": [Path(x).name for x in oncall.arete_logs()]})
        """)
        self.assertTrue(found["first"])
        self.assertTrue(found["inode"])
        self.assertEqual(sorted(found["got"]), sorted(found["want"]))
        self.assertEqual(len(found["got"]), 20)
        self.assertEqual(found["late"], found["want_late"])
        self.assertEqual(found["again"], [[], []])
        self.assertEqual(found["listed"], ["runs.log"])

    def test_heartbeat_does_not_rotate_and_serve_does(self):
        found = self.scenario("""
            p.write_bytes(b"x" * 6_000_000)
            inode = p.stat().st_ino
            codes = []
            for dry in (True, False):
                with mock.patch.object(heartbeat, "network_up_or_skip", return_value=False):
                    codes.append(heartbeat.run(dry_run=dry))
            untouched = p.exists() and p.stat().st_size == 6_000_000 and p.stat().st_ino == inode
            no_archive = not (logs / "runs.log.1").exists()
            server = serve.Server()
            try:
                server.rotate()
            finally:
                server.pool.shutdown()
                for pool in server.job_pools.values():
                    pool.shutdown()
            archive = logs / "runs.log.1"
            finish({"codes": codes, "untouched": untouched, "no_archive": no_archive, "moved": not p.exists(),
                    "inode": archive.stat().st_ino == inode, "size": archive.stat().st_size})
        """)
        self.assertEqual(found["codes"], [0, 0])
        self.assertTrue(found["untouched"])
        self.assertTrue(found["no_archive"])
        self.assertTrue(found["moved"])
        self.assertTrue(found["inode"])
        self.assertEqual(found["size"], 6_000_000)

    def test_tick_rotates_on_its_own_timer_before_prune(self):
        found = self.scenario("""
            clock = [10_000.0]
            from arete import state
            with contextlib.redirect_stdout(io.StringIO()):
                state.migrate()
            server = serve.Server()
            events = []
            server.rotate = lambda: events.append("rotate")
            server.prune = lambda: events.append("prune")
            for name in ("forget_unpolled_errors", "watch_oncall", "dispatch", "start_approval_jobs"):
                setattr(server, name, lambda *a, **k: None)
            server.load = lambda: True
            server.sweeping = lambda: True
            patches = [mock.patch.object(serve.time, "monotonic", lambda: clock[0]),
                       mock.patch.object(serve.loops, "load_all", return_value=[]),
                       mock.patch.object(serve.record, "reap", return_value=[]),
                       mock.patch.object(serve.sessions, "tell_chats"),
                       mock.patch.object(serve.loop_store, "loop_states", return_value={}),
                       mock.patch.object(serve.delivery, "release_lapsed", return_value=[]),
                       mock.patch.object(serve.tasks, "expire_asks", return_value=[]),
                       mock.patch.object(serve.webpush, "remind", return_value=[])]
            for patch in patches:
                patch.start()
            try:
                def tick(advance):
                    clock[0] += advance
                    del events[:]
                    server.tick()
                    return list(events)
                first = tick(0)
                soon = tick(60)
                later = tick(serve.ROTATE_SECONDS)

                def broken():
                    raise RuntimeError("rotation broke")
                server.rotate = broken
                server.prune = lambda: events.append("prune")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    failed = tick(serve.ROTATE_SECONDS + 1)

                def prune_raises():
                    raise RuntimeError("prune broke")
                server.rotate = lambda: events.append("rotate")
                server.prune = prune_raises
                clock[0] += serve.ROTATE_SECONDS + 1
                del events[:]
                with contextlib.redirect_stdout(io.StringIO()):
                    server.tick()
                ran_before_prune = list(events)
                prune_failing = "stage:prune" in server.failing
            finally:
                for patch in reversed(patches):
                    patch.stop()
                server.pool.shutdown()
                for pool in server.job_pools.values():
                    pool.shutdown()
            finish({"first": first, "soon": soon, "later": later, "failed": failed,
                    "said": out.getvalue(), "prune_failing": prune_failing, "before_prune": ran_before_prune})
        """)
        self.assertEqual(found["first"], ["rotate", "prune"])
        self.assertEqual(found["soon"], [])
        self.assertEqual(found["later"], ["rotate", "prune"])
        self.assertEqual(found["failed"], ["prune"])
        self.assertIn("stage:rotate: tick failed: RuntimeError: rotation broke", found["said"])
        self.assertTrue(found["prune_failing"])
        self.assertEqual(found["before_prune"], ["rotate"])

    def test_serve_run_says_its_own_log_notes_once_after_rotating_first(self):
        found = self.scenario("""
            events = []

            class Stop(BaseException):
                pass

            class FakeServer:
                def __init__(self):
                    events.append("Server")

                def say(self, text):
                    events.append("say:" + text)

                def report_config(self):
                    events.append("report")

                load_new_code = tick = reap_inherited = run_pass = lambda self: None
                cfg = None

            def rotate_own_log():
                events.append("rotate_own_log")
                return ["serve.log: rename failed: boom"]

            def sleep(seconds):
                raise Stop

            with mock.patch.object(install, "rotate_own_log", rotate_own_log), \\
                    mock.patch.object(serve, "take_serve_lock", lambda: open(os.devnull)), \\
                    mock.patch.object(serve, "Server", FakeServer), \\
                    mock.patch.object(serve, "start_notes", lambda cfg: []), \\
                    mock.patch.object(serve.time, "sleep", sleep):
                try:
                    serve.run(with_dashboard=False)
                except Stop:
                    pass
            finish(events)
        """)
        self.assertEqual(found[0], "rotate_own_log")
        self.assertEqual(found[1], "Server")
        self.assertEqual(sum(1 for e in found if "serve.log: rename failed: boom" in e), 1)

    def test_production_code_owns_each_rotation_call_once(self):
        calls = {"rotate_logs": [], "rotate_own_log": []}
        for path in sorted((SOURCE / "arete").rglob("*.py")):
            tree = ast.parse(path.read_text())
            for owner in ast.walk(tree):
                if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(owner):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in calls and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "install"):
                        calls[node.func.attr].append((path.name, owner.name))
        self.assertEqual(calls["rotate_logs"], [("serve.py", "rotate")])
        self.assertEqual(calls["rotate_own_log"], [("serve.py", "run")])
        serve_tree = ast.parse((SOURCE / "arete" / "serve.py").read_text())
        run = next(n for n in ast.walk(serve_tree) if isinstance(n, ast.FunctionDef) and n.name == "run")
        first = run.body[0]
        self.assertIn("rotate_own_log", ast.unparse(first))
        self.assertLess(first.lineno, next(n.lineno for n in ast.walk(run)
                                           if isinstance(n, ast.Call) and ast.unparse(n.func) == "Server"))
        source = (SOURCE / "arete" / "install.py").read_text()
        self.assertNotIn("copyfile", source)
        self.assertNotIn("truncate", source)
        self.assertNotIn("rotate_logs", (SOURCE / "arete" / "heartbeat.py").read_text())


if __name__ == "__main__":
    unittest.main()
