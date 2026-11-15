import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import support
from tests.harness import behavior_result, make_folders, run_script, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class DurableCostTests(unittest.TestCase):
    PRELUDE = (SCRIPTS / "durable_cost_prelude.py").read_text()
    BATCH_ROLLBACK = (SCRIPTS / "durable_cost_batch_rollback.py").read_text()
    LOCK_TIMEOUT = (SCRIPTS / "durable_cost_lock_timeout.py").read_text()
    INTERRUPTIONS = (SCRIPTS / "durable_cost_interruptions.py").read_text()
    FAILED_WRITE_AND_FINISH = (SCRIPTS / "durable_cost_failed_write_and_finish.py").read_text()
    BACKFILL = (SCRIPTS / "durable_cost_backfill.py").read_text()
    CORRECTIONS = (SCRIPTS / "durable_cost_corrections.py").read_text()

    def run_costs(self, body, modes=("run",)):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            make_folders(root, "home", "config", "state", "logs", "worktrees", "tmp")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              HOME=str(root / "home"), ARETE_WORKTREES_DIR=str(root / "worktrees"), TMPDIR=str(root / "tmp"),
                              GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            program = self.PRELUDE + body
            for mode in modes:
                done = run_script(program, root, str(root), mode, env=env, timeout=120)
                self.assertEqual(done.returncode, 9 if mode == "kill" else 0, done.stderr + done.stdout)

    def test_commit_precedes_timeline_even_when_finish_kills_the_process(self):
        self.run_costs(r'''
if mode == "kill":
    rid = record.start("pipeline", run_id="killed")
    claude.run = lambda *a, **kw: result()
    record.Recorder.finish = lambda *a: os._exit(9)
    attempt(rid)
else:
    found = rows("killed")
    assert len(found) == 1 and found[0]["cost_usd"] == 2.5, found
    assert state.run_get("killed")["cost_usd"] == 2.5
    assert all(rows("killed") for m in marks("killed") if m.get("cost_usd"))
''', ("kill", "inspect"))

    def test_successful_batch_and_second_insert_rollback(self):
        self.run_costs(self.BATCH_ROLLBACK)

    def test_lock_timeout_keeps_cost_and_diagnostic_only_in_timeline(self):
        self.run_costs(self.LOCK_TIMEOUT)

    def test_unlimited_interruptions_crash_park_and_token_cut_charge_once(self):
        self.run_costs(self.INTERRUPTIONS)

    def test_failed_interrupted_write_and_post_commit_exception_never_charge_twice(self):
        self.run_costs(self.FAILED_WRITE_AND_FINISH)

    def test_backfill_boundary_before_mapping_paid_totals_and_drift(self):
        for before in (False, True):
            self.run_costs("use_before = " + repr(before) + self.BACKFILL)

    def test_corrections_billing_validation_and_drift_tolerance(self):
        self.run_costs(self.CORRECTIONS)


BACKUP_PRELUDE = (SCRIPTS / "backup_prelude.py").read_text()


BACKUP_SCRIPTS = {
"prune-keeps": r'''
seed()
s = server()
before = counts()
prune(s)
snaps = sorted(paths.backups_dir().glob("arete-*.db"))
found = backup.inspect(snaps[0])
out |= {"snapshots": len(snaps), "quick_check": found["quick_check"], "snap_runs": found["counts"]["runs"],
        "before_runs": before["runs"], "snap_counts_match": found["counts"] == before,
        "runs": column("SELECT id FROM runs ORDER BY id"), "feed": column("SELECT DISTINCT run_id FROM feed ORDER BY 1"),
        "queue": column("SELECT loop FROM queue ORDER BY id"), "events": column("SELECT loop FROM events"),
        "incidents": column("SELECT fingerprint FROM incidents ORDER BY id"),
        "notes": column("SELECT note FROM incident_notes ORDER BY id"),
        "aliases": column("SELECT fingerprint FROM incident_aliases ORDER BY 1"),
        "repairs": column("SELECT count(*) FROM incident_repairs"),
        "last": json.loads((paths.backups_dir() / "last.json").read_text())["quick_check"]}
''',
"widened-restore": (SCRIPTS / "backup_widened_restore.py").read_text(),
"hot-journal": (SCRIPTS / "backup_hot_journal.py").read_text(),
"no-copy-no-delete": r'''
seed()
before = counts()
def boom(reason):
    raise RuntimeError("distinctive-boom-17")
s = server()
with mock.patch.object(backup, "snapshot", boom), mock.patch.object(state, "prune") as pruned, \
        mock.patch.object(serve.record, "prune") as records:
    s.prune()
out |= {"said": s.said, "state_pruned": pruned.called, "records_pruned": records.called, "same": counts() == before}
''',
"follows-paths": r'''
seed()
a = paths.STATE_DIR
planted = backup.snapshot("hand")
listing = lambda d: sorted(p.name for p in (d / "backups").iterdir())
held = listing(a)
b = root / "other"
with contextlib.ExitStack() as stack:
    for name, value in {"STATE_DIR": b, "RUNS_DIR": b / "runs", "DB_FILE": b / "arete.db", "LOCKS_DIR": b / "locks",
                        "WORKTREES_DIR": b / "worktrees", "EVENTS_FILE": b / "events.jsonl"}.items():
        stack.enter_context(mock.patch.object(paths, name, value))
    state.ensure_schema()
    out["b_db"] = (b / "arete.db").exists()
    prune()
    out["b_files"] = listing(b)
out["a_same"] = listing(a) == held
''',
"concurrent-write": (SCRIPTS / "backup_concurrent_write.py").read_text(),
"missing-file": r'''
seed()
snap = backup.snapshot("hand")
def attempt():
    try:
        state.ensure_schema()
    except RuntimeError as e:
        return str(e)
    return ""
paths.DB_FILE.unlink()
out |= {"error": attempt(), "exists": paths.DB_FILE.exists(), "snap": snap.name}
paths.DB_FILE.write_bytes(b"")
out |= {"empty_error": attempt(), "size": paths.DB_FILE.stat().st_size}
try:
    backup.snapshot("hand")
    out["empty_snapshot"] = ""
except RuntimeError as e:
    out["empty_snapshot"] = str(e)
out["snaps"] = len(backup.snapshots())
''',
"fresh": r'''
out["none"] = backup.snapshot("hand")
state.ensure_schema()
out["created"] = paths.DB_FILE.exists()
with mock.patch.object(paths, "ISOLATED", True):
    out["isolated"] = backup.snapshot("hand")
out["no_folder"] = not paths.backups_dir().exists()
out["taken"] = backup.snapshot("hand").name
''',
"retention": (SCRIPTS / "backup_retention.py").read_text(),
"copy": (SCRIPTS / "backup_copy.py").read_text(),
"no-destination": r'''
seed()
calls = []
rsync_only(lambda cmd: calls.append(cmd))
prune()
prune()
out |= {"calls": calls, "archives": len(list(paths.backups_dir().glob("config-*.tar.gz"))),
        "copied": "copied_at" in json.loads((paths.backups_dir() / "last.json").read_text())}
''',
"heartbeat": (SCRIPTS / "backup_heartbeat.py").read_text(),
"cli": r'''
seed()
rc, text = run_cli("backup")
out["plain"] = [rc, text]
rc, text = run_cli("backup", "--check")
out["check"] = [rc, text]
out["both"] = run_cli("backup", "--check", "--restore", "x.db")[0]
held = paths.DB_FILE.read_bytes()
newest = backup.newest_snapshot()
os.truncate(newest, newest.stat().st_size // 2)
rc, text = run_cli("backup", "--check")
out["truncated"] = [rc, text, paths.DB_FILE.read_bytes() == held]
''',
"durable-config": (SCRIPTS / "backup_durable_config.py").read_text(),
}


class BackupTests(unittest.TestCase):
    def script(self, name, config_text=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
            if config_text:
                (root / "config/config.toml").write_text(config_text)
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), HOME=str(root / "home"),
                              GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            program = (BACKUP_PRELUDE + BACKUP_SCRIPTS[name]
                       + "\nprint('BACKUP_RESULT=' + json.dumps(out, default=str))")
            result = run_script(program, root, str(root), env=env, timeout=240)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return behavior_result(result, marker="BACKUP_RESULT=")

    def test_prune_keeps_what_it_should_and_snapshots_first(self):
        r = self.script("prune-keeps")
        self.assertEqual(r["runs"], ["free-new", "loop-new", "loop-old", "paid-old", "pipe-new", "pipe-old"])
        self.assertEqual(r["feed"], r["runs"])
        self.assertEqual(r["queue"], ["q-old", "q-new", "q-null"])
        self.assertEqual(r["events"], ["e-new"])
        self.assertEqual(r["incidents"], ["open-new", "closed-recent"])
        self.assertEqual(r["notes"], ["note open-new", "note closed-recent"])
        self.assertEqual(r["aliases"], ["closed-recent-alias", "open-new-alias"])
        self.assertEqual(r["repairs"], [2])
        self.assertEqual((r["snapshots"], r["quick_check"], r["last"]), (1, "ok", "ok"))
        self.assertEqual(r["snap_runs"], r["before_runs"])
        self.assertTrue(r["snap_counts_match"])

    def test_a_widened_prune_is_undone_by_restore(self):
        r = self.script("widened-restore")
        self.assertTrue(r["deleted"])
        self.assertEqual(r["alive"], [1, True, True])
        self.assertEqual(r["running"], [1, True, True])
        self.assertEqual((r["missing"], r["corrupt"], r["rc"]), (1, 1, 0))
        self.assertTrue(r["restored"])
        self.assertTrue(r["seq_kept"])
        self.assertEqual(r["kept_file"], 1)
        self.assertEqual(r["rc_corrupt_current"], 0)
        self.assertTrue(r["gap"])

    def test_restore_keeps_the_replaced_file_and_its_journal_paired(self):
        r = self.script("hot-journal")
        self.assertTrue(r["hot"])
        self.assertEqual((r["refused"], r["rc"], r["rc2"]), (1, 0, 0))
        self.assertEqual(r["uncommitted"], 0)
        self.assertEqual(r["quick_check"], ["ok"])
        self.assertFalse(r["live_journal"] or r["live_journal2"])
        self.assertTrue(r["pairs"] and all(r["pairs"]))

    def test_no_copy_no_delete(self):
        r = self.script("no-copy-no-delete")
        self.assertFalse(r["state_pruned"] or r["records_pruned"])
        self.assertTrue(r["same"])
        self.assertIn("distinctive-boom-17", " ".join(r["said"]))

    def test_the_backups_folder_follows_the_patched_database(self):
        r = self.script("follows-paths")
        self.assertTrue(r["b_db"])
        self.assertTrue(r["a_same"])
        self.assertEqual(sum(n.startswith("arete-") for n in r["b_files"]), 1)
        self.assertIn("last.json", r["b_files"])

    def test_a_snapshot_during_a_write_holds_none_of_it(self):
        r = self.script("concurrent-write")
        self.assertEqual((r["writer"], r["quick_check"], r["rows"], r["leaked"], r["live_leaked"]), (0, "ok", 20000, 0, 0))

    def test_a_missing_or_empty_file_is_not_replaced_while_a_snapshot_exists(self):
        r = self.script("missing-file")
        self.assertIn("arete backup --restore", r["error"])
        self.assertIn(r["snap"], r["error"])
        self.assertFalse(r["exists"])
        self.assertIn(r["snap"], r["empty_error"])
        self.assertEqual(r["size"], 0)
        self.assertIn("empty", r["empty_snapshot"])
        self.assertEqual(r["snaps"], 1)

    def test_a_fresh_install_and_isolated_state_behave_as_before(self):
        r = self.script("fresh")
        self.assertEqual((r["none"], r["created"], r["isolated"], r["no_folder"]), (None, True, None, True))
        self.assertTrue(r["taken"].startswith("arete-"))

    def test_retention_keeps_the_newest_and_one_per_day(self):
        r = self.script("retention")
        self.assertEqual(r["left"], r["expected"])
        self.assertTrue(r["other"])

    def test_daily_copy_retries_until_it_succeeds_and_holds_only_allowed_files(self):
        r = self.script("copy", '[backup]\ncopy_to = "host:/backups/arete/"\n')
        self.assertEqual(r["steps"], [1, 1, False, 2, 0, True, 2, True, 3, True, 4, True])
        host = r["argv0"]
        self.assertEqual((host[:2], host[-1]), (["rsync", "-t"], "host:/backups/arete/"))
        self.assertTrue(host[2].endswith("-prune.db") and host[3].endswith(f"config-{r['first_day']}.tar.gz"))
        self.assertTrue(r["closed"])
        self.assertEqual(len(r["days"]), 2)
        files = {m for m in r["members"]}
        self.assertTrue({"config.toml", "loop-switches.json", "loops/a.toml", "loops/nested/ok.toml", "layers.bundle"} <= files)
        self.assertFalse([m for m in r["members"] if m.endswith(".env") or "verification-authority" in m or "link" in m])
        self.assertEqual(r["verify"], 0)
        self.assertIn("pi.env", r["history"])
        self.assertIn("linked", r["linked"])
        self.assertIn("refs/heads/main", r["heads"])

    def test_no_destination_means_no_copy(self):
        r = self.script("no-destination")
        self.assertEqual((r["calls"], r["archives"], r["copied"]), ([], 1, False))

    def test_heartbeat_reports_stale_damaged_and_missing_copies(self):
        r = self.script("heartbeat")
        self.assertEqual([r["fresh"], r["stale"], r["truncated"], r["recorded"]], [True, False, False, False])
        self.assertEqual(r["empty"][:2], [False, "warn"])
        self.assertIn("until the owner sets", r["empty"][2])
        self.assertEqual([r["never"], r["old"], r["recent"]], [False, False, True])

    def test_backup_command_prints_checks_and_refuses_a_damaged_snapshot(self):
        r = self.script("cli")
        self.assertEqual(r["plain"][0], 0)
        self.assertIn("bytes", r["plain"][1])
        self.assertIn("quick_check ok", r["plain"][1])
        self.assertEqual(r["check"][0], 0)
        self.assertRegex(r["check"][1], r"runs\s+7\s+7")
        self.assertEqual(r["both"], 2)
        self.assertEqual(r["truncated"][0], 1)
        self.assertTrue(r["truncated"][2])

    def test_config_writes_are_flushed_before_and_after_the_replace(self):
        r = self.script("durable-config")
        expected = [["flush", "file"], ["replace", ""], ["flush", "dir"]]
        self.assertEqual((r["checked"], r["put"]), (expected, expected))
        self.assertEqual((r["loaded"], r["refused"], r["unchanged"], r["events"]), (3, 2, True, []))
        self.assertEqual((r["mode"], r["left"], r["copy_to"]), (0o640, ["config.toml"], "h:/x"))
        self.assertEqual((r["bad"], r["default"]), (4, ""))


class RunsLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name).resolve()
        make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
        env = scratch_env(root, ARETE_WORKTREES_DIR=str(root / "worktrees"),
                          TMPDIR=str(root / "tmp"), HOME=str(root / "home"))
        done = run_script((SCRIPTS / "runs_lookup_probe.py").read_text(), root, env=env, timeout=240)
        assert done.returncode == 0, done.stderr + done.stdout
        cls.result = behavior_result(done)

    def test_the_seeded_home_has_both_indexes_and_keeps_them_after_reopening(self):
        for seen in (self.result["seed"], self.result["reopened"]):
            self.assertEqual((seen["runs"], seen["loops"]), (11018, 7000))
            self.assertEqual({name: " ".join(sql.split()) for name, sql in seen["indexes"].items()},
                             {"runs_ticket": "CREATE INDEX runs_ticket ON runs (repo, issue, kind, started)",
                              "runs_started": "CREATE INDEX runs_started ON runs (started)"})

    def test_interrupted_tickets_are_the_latest_whole_runs_in_started_order(self):
        results = self.result["results"]
        self.assertEqual(results["interrupted_tickets"], self.result["expected"])
        self.assertEqual(results["latest_whole"], results["expected_latest_whole"])

    def test_the_first_interrupted_lookup_reads_little(self):
        self.assertLess(self.result["progress"], 2000)

    def test_nearby_lookups_still_answer(self):
        results = self.result["results"]
        self.assertEqual(results["open_runs"], [self.result["ids"]["1021"][1]])
        self.assertEqual(results["later_run"], self.result["ids"]["1021"][1])
        self.assertEqual(results["feedback_due"], ["1021"])
        self.assertFalse(results["history"]["default_has_firings"])
        self.assertTrue(results["history"]["firings_has_firings"])

    def test_every_lookup_uses_an_index_search(self):
        plans = self.result["plans"]
        required = {"interrupted_tickets": ("SEARCH runs USING INDEX runs_ticket (repo=?",
                                            "SEARCH r2 USING INDEX runs_ticket (repo=? AND issue=?"),
                    "runs_since": ("SEARCH runs USING INDEX runs_started",),
                    "runs": ("SCAN runs USING INDEX runs_started",),
                    "last_pipeline_starts": ()}
        for name in ("issue_running", "running_run", "ticket_runs", "open_runs", "later_run", "feedback_due"):
            required[name] = ("SEARCH runs USING INDEX runs_ticket (repo=? AND issue=?",)
        allowed_scans = {"runs": "SCAN runs USING INDEX runs_started",
                         "last_pipeline_starts": "SCAN runs USING COVERING INDEX runs_ticket"}
        for name, prefixes in required.items():
            lines = [line for entry in plans[name] for line in entry["plan"]]
            self.assertTrue(lines, name)
            for prefix in prefixes:
                self.assertTrue(any(line.startswith(prefix) for line in lines), (name, prefix, lines))
            for line in lines:
                if line.startswith("SCAN") and line.split()[1] in ("runs", "r2"):
                    self.assertTrue(line.startswith(allowed_scans.get(name, "\0")), (name, line))
            if name == "runs":
                self.assertFalse(any("USE TEMP B-TREE" in line for line in lines), lines)


class DoctorTests(unittest.TestCase):
    FIXTURE = SCRIPTS / "doctor_fixture.py"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        make_folders(self.root, "home", "config", "state", "logs", "worktrees", "tmp")
        self.env = scratch_env(self.root, HOME=str(self.root / "home"), ARETE_WORKTREES_DIR=str(self.root / "worktrees"),
                               TMPDIR=str(self.root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def fixture(self, *args, check=True):
        done = subprocess.run([sys.executable, str(self.FIXTURE), *args], cwd=self.root, env=self.env, text=True,
                              capture_output=True, timeout=180)
        if check:
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        return done

    def lines(self, done):
        return {line.split()[1]: line for line in done.stdout.splitlines() if line.split()[:1] in (["OK"], ["FAIL"], ["INFO"])}

    def doctor(self):
        done = self.fixture("cli", "doctor", check=False)
        return done.returncode, self.lines(done), done

    def inspected(self):
        return json.loads(self.fixture("inspect").stdout)

    def fingerprint(self):
        return self.fixture("fingerprint").stdout

    def test_a_clean_home_reads_ok_on_every_check(self):
        self.fixture("clean")
        code, lines, done = self.doctor()
        self.assertEqual(code, 0, done.stdout + done.stderr)
        self.assertEqual(list(lines), ["cost", "orphans", "chat", "folders", "stuck", "store", "backup"])
        self.assertTrue(all(line.startswith("OK") for line in lines.values()), lines)

    def test_doctor_names_each_old_fault_once_and_leaves_young_ones_alone(self):
        self.fixture("faults")
        before = self.fingerprint()
        code, lines, done = self.doctor()
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(code, 1, done.stdout)
        self.assertTrue(lines["cost"].startswith("FAIL") and "cost-run" in lines["cost"], lines["cost"])
        self.assertTrue(lines["orphans"].startswith("FAIL") and "ghost-run" in lines["orphans"], lines["orphans"])
        self.assertTrue(lines["chat"].startswith("INFO") and lines["chat"].split()[2] == "2", lines["chat"])
        self.assertEqual(lines["folders"].split()[2], "3")
        for name in ("stray-folder", "old-pipeline", "sess-gone"):
            self.assertIn(name, lines["folders"])
        self.assertEqual(lines["stuck"].split()[2], "3")
        for name in ("chat#1", "queue#1", "approval#1"):
            self.assertIn(name, lines["stuck"])
        for name in ("store", "backup"):
            self.assertTrue(lines[name].startswith("OK"), lines[name])
        for name in ("runs/chat", "eva-x-prompts", "young-folder", "young-row"):
            self.assertNotIn(name, done.stdout)

    def test_doctor_reads_a_read_only_database(self):
        self.fixture("faults")
        db = self.root / "state" / "arete.db"
        before = self.fingerprint()
        os.chmod(db, 0o444)
        os.chmod(db.parent, 0o555)
        self.addCleanup(os.chmod, db.parent, 0o755)
        self.addCleanup(os.chmod, db, 0o644)
        code, lines, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 7)
        self.assertEqual(self.fingerprint(), before)

    def test_audit_raises_one_doctor_alert_per_failing_check_and_clears_them(self):
        self.fixture("faults")
        for _ in range(2):
            self.assertEqual(self.fixture("cli", "audit").returncode, 0)
        found = self.inspected()
        self.assertEqual([k for k in found["alerts"] if k.startswith("audit:doctor:")],
                         ["audit:doctor:cost", "audit:doctor:folders", "audit:doctor:orphans", "audit:doctor:stuck"])
        self.assertEqual(len([n for n in found["notified"] if "audit: doctor:" in n[1]]), 4)
        self.assertNotIn("doctor:", found["latest"])
        latest = (self.root / "state" / "audit" / "latest.md").read_text()
        self.assertIn("## Doctor", latest)
        for name in ("cost", "orphans", "chat", "folders", "stuck", "store", "backup"):
            self.assertIn(f"- {name}: ", latest)
        self.fixture("repair")
        self.fixture("cli", "audit")
        self.assertEqual([k for k in self.inspected()["alerts"] if k.startswith("audit:doctor:")], [])

    def test_a_failing_heartbeat_check_makes_the_doctor_alert_a_warning(self):
        self.fixture("faults")
        self.fixture("heartbeat-cost")
        self.fixture("cli", "audit")
        self.assertNotIn("audit:doctor:cost", self.inspected()["alerts"])
        self.assertIn("audit:doctor:orphans", self.inspected()["alerts"])
        self.assertIn("WARN [doctor:cost]", (self.root / "state" / "audit" / "latest.md").read_text())
        self.assertEqual(self.doctor()[0], 1)

    def test_a_passing_malformed_or_warning_heartbeat_keeps_the_alert(self):
        self.fixture("faults")
        for action in ("heartbeat-passing", "heartbeat-malformed", "heartbeat-warn"):
            self.fixture(action)
            self.fixture("cli", "audit")
            self.assertIn("audit:doctor:cost", self.inspected()["alerts"], action)

    def test_backup_fails_when_stale_unrecorded_or_missing_with_serve_installed(self):
        self.fixture("clean")
        self.fixture("stale-backup")
        code, lines, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertTrue(lines["backup"].startswith("FAIL") and ".db" in lines["backup"], lines["backup"])
        self.fixture("fresh-backup")
        self.fixture("bad-backup-record")
        self.assertTrue(self.doctor()[1]["backup"].startswith("FAIL"))
        self.fixture("no-backup")
        code, lines, _ = self.doctor()
        self.assertEqual(code, 0)
        self.assertTrue(lines["backup"].startswith("INFO"), lines["backup"])

    def test_doctor_fails_store_on_a_damaged_database(self):
        self.fixture("clean")
        self.fixture("corrupt-store")
        before = self.fingerprint()
        code, lines, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 7)
        self.assertTrue(lines["store"].startswith("FAIL"), lines["store"])
        self.assertEqual(self.fingerprint(), before)

    def test_doctor_without_a_database_fails_without_creating_one(self):
        code, lines, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 7)
        self.assertFalse((self.root / "state" / "arete.db").exists())
        self.assertFalse((self.root / "state" / "runs").exists())


if __name__ == "__main__":
    unittest.main()
