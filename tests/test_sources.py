import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import support
from tests.harness import behavior_result, inherited_env, make_folders, run_script, scratch_env, state_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class LoopInputRecoveryTests(unittest.TestCase):
    SCRIPT = (SCRIPTS / "loop_input_recovery.py").read_text()

    def run_modes(self, *modes):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            make_folders(root, "config", "state", "logs", "tmp", "home", "worktrees", "authority-parent")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), HOME=str(root / "home"))
            for mode in modes:
                done = run_script(self.SCRIPT, root, str(root), mode, env=env, timeout=180)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("RECOVERY_OK", done.stdout)

    def test_command_outcomes_status_requeue_and_claim_guard(self):
        self.run_modes("outcomes")

    def test_real_kills_requested_stops_and_unsolicited_signals(self):
        self.run_modes("signals")

    def test_settled_claim_sweeps_and_retention(self):
        self.run_modes("retention")

    def test_never_began_retry_survives_a_new_server_process(self):
        self.run_modes("never", "never")

    def test_empty_enqueue_is_atomic_across_processes(self):
        self.run_modes("race")


class IntakeTests(unittest.TestCase):
    PRELUDE = (SCRIPTS / "intake_prelude.py").read_text()
    CONCURRENT_DELIVERIES = (SCRIPTS / "intake_concurrent_deliveries.py").read_text()
    BODY_DEDUPLICATION = (SCRIPTS / "intake_body_deduplication.py").read_text()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        make_folders(self.root, "home", "config", "state", "logs", "tmp")
        self.env = scratch_env(self.root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                               HOME=str(self.root / "home"), TMPDIR=str(self.root / "tmp"),
                               INTAKE_ROOT=str(self.root), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def run_script(self, body):
        done = run_script(self.PRELUDE + body, self.root, env=self.env, timeout=300)
        return done, behavior_result(done) if any(
            line.startswith("BEHAVIOR_RESULT=") for line in done.stdout.splitlines()) else None

    def result(self, body):
        done, found = self.run_script(body)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIsNotNone(found, done.stdout + done.stderr)
        return found

    BASELINE = '''
loop = make_loop()
write_rows()
server = make_server()
poll(server, loop)
'''
    THREE = 'write_rows(issue("a"), issue("b"), issue("c"))\n'

    def test_a_poll_that_fails_before_queueing_keeps_its_events_for_the_next_try(self):
        found = self.result(self.BASELINE + self.THREE + '''
base = snap()
loop_store.enqueue = failing_once(loop_store.enqueue)
error = poll(server, loop)
failed = snap()
lines = list(server.lines)
poll(server, loop)
healed = snap()
poll(server, loop)
finish({"base": base, "failed": failed, "lines": lines, "healed": healed, "third": snap(),
        "error": repr(error)})
''')
        shown = json.dumps(found, indent=1)
        self.assertEqual((found["base"]["seen"], found["base"]["cursor"]["ok"]), (0, 1), shown)
        self.assertEqual(found["error"], "None", shown)
        failed = found["failed"]
        self.assertEqual((failed["seen"], failed["events"], failed["queue"]), (0, 0, 0), shown)
        self.assertEqual(failed["cursor"]["ok"], 0, shown)
        self.assertIn("database is locked", failed["cursor"]["detail"], shown)
        self.assertEqual(failed["cursor"]["value"], found["base"]["cursor"]["value"], shown)
        self.assertEqual(failed["owner"], found["base"]["owner"], shown)
        self.assertEqual(len([line for line in found["lines"] if "poll failed" in line]), 1, shown)
        failure = next(line for line in found["lines"] if "poll failed" in line)
        self.assertIn("intake#0: poll failed: OperationalError: database is locked", failure, shown)
        healed = found["healed"]
        self.assertEqual((healed["seen"], healed["events"], healed["queue"]), (3, 3, 1), shown)
        self.assertEqual(healed["queue_rows"], [{"grp": "", "keys": ["a", "b", "c"]}], shown)
        self.assertEqual(healed["cursor"]["ok"], 1, shown)
        self.assertEqual(found["third"]["queue_rows"], healed["queue_rows"], shown)
        self.assertEqual(found["third"]["events"], 3, shown)

    def test_a_kill_inside_the_intake_transaction_loses_nothing(self):
        self.result(self.BASELINE + 'finish({})\n')
        done, _ = self.run_script('''
loop = make_loop()
''' + self.THREE + '''
real = loop_store.add_event
def dies(*a, **k):
    real(*a, **k)
    os.kill(os.getpid(), signal.SIGKILL)
loop_store.add_event = dies
server = make_server()
server.poll(loop)
server.polls["intake#0"].result(timeout=60)
''')
        self.assertEqual(done.returncode, -9, done.stdout + done.stderr)
        after = self.result('finish(snap())\n')
        shown = json.dumps(after, indent=1)
        self.assertEqual((after["seen"], after["events"], after["queue"]), (0, 0, 0), shown)
        self.assertEqual(after["cursor"]["detail"], "0 read, 0 new (baseline)", shown)
        self.assertIsNotNone(after["owner"], shown)
        again = self.result('''
loop = make_loop()
server = make_server()
poll(server, loop)
first = snap()
poll(server, loop)
finish({"first": first, "second": snap()})
''')
        self.assertEqual(again["first"]["queue_rows"], [{"grp": "", "keys": ["a", "b", "c"]}], str(again))
        self.assertEqual(again["second"]["queue_rows"], again["first"]["queue_rows"], str(again))
        self.assertEqual(again["second"]["events"], 3, str(again))

    def test_an_error_while_reporting_a_failed_poll_is_still_said(self):
        found = self.result(self.BASELINE + self.THREE + '''
def no_enqueue(*a, **k):
    raise sqlite3.OperationalError("enqueue is locked")
def no_cursor(*a, **k):
    raise sqlite3.OperationalError("cursor is locked")
loop_store.enqueue, source_store.set_cursor = no_enqueue, no_cursor
error = poll(server, loop)
finish({"error": str(error), "lines": server.lines})
''')
        self.assertEqual(found["error"], "cursor is locked", str(found))
        failures = [line for line in found["lines"] if "poll failed" in line]
        self.assertEqual(len(failures), 1, str(found))
        self.assertIn("intake#0: poll failed: OperationalError: cursor is locked", failures[0], str(found))

    def test_a_row_the_loop_cannot_group_does_not_stop_the_batch(self):
        found = self.result('''
loop = make_loop(trigger='group = "team"\\n')
write_rows()
server = make_server()
poll(server, loop)
write_rows(issue("one", {"team": "x"}), issue("two", ["x"]), issue("three", {"team": "y"}))
poll(server, loop)
finish({"snap": snap(), "lines": server.lines})
''')
        shown = json.dumps(found, indent=1)
        self.assertEqual(found["snap"]["queue_rows"], [{"grp": "x", "keys": ["one"]}, {"grp": "", "keys": ["two"]},
                                                       {"grp": "y", "keys": ["three"]}], shown)
        self.assertEqual(found["snap"]["cursor"]["ok"], 1, shown)
        self.assertFalse([line for line in found["lines"] if "poll failed" in line], shown)

    def test_a_webhook_whose_fire_fails_queues_on_redelivery(self):
        found = self.result('''
loop = make_loop(webhook=True)
state.migrate()
body = b'{"title": "hello"}'
loop_store.enqueue = failing_once(loop_store.enqueue)
server = web()
try:
    out = []
    for _ in range(3):
        out.append(post(server, "intake", body, signed("intake", body, "abc")))
        out.append(snap())
finally:
    server.shutdown()
    server.server_close()
finish(out)
''')
        shown = json.dumps(found, indent=1)
        self.assertEqual([found[0][0], found[2][0], found[4][0]], [500, 202, 200], shown)
        self.assertEqual(found[4][1]["why"], "already received", shown)
        self.assertEqual((found[1]["seen"], found[1]["events"], found[1]["queue"]), (0, 0, 0), shown)
        for later in (found[3], found[5]):
            self.assertEqual((later["seen"], later["events"], later["queue"]), (1, 1, 1), shown)
            self.assertEqual(later["queue_rows"], [{"grp": "", "keys": ["abc"]}], shown)

    def test_concurrent_deliveries_of_one_id_queue_once(self):
        found = self.result(self.CONCURRENT_DELIVERIES)
        self.assertEqual(found["codes"], [[200, 202]] * 50)
        self.assertEqual(found["snap"]["queue"], 50)
        self.assertEqual(sorted(r["keys"][0] for r in found["snap"]["queue_rows"]),
                         sorted(f"id-{n}" for n in range(50)))

    def test_a_post_with_no_delivery_id_counts_once_per_ten_minutes_by_its_body(self):
        found = self.result(self.BODY_DEDUPLICATION)
        shown = json.dumps(found, indent=1)
        self.assertEqual([r[0] for r in found["a"]], [202, 200], shown)
        self.assertEqual(found["a"][0][1]["key"], found["expected"], shown)
        self.assertEqual(found["b"][0], 202, shown)
        self.assertEqual([r[0] for r in found["a_aged"]], [202, 200], shown)
        self.assertEqual(found["id"], [202, 200], shown)
        self.assertEqual(found["bare"], [202, 200], shown)
        keys = [r["keys"][0] for r in found["snap"]["queue_rows"]]
        self.assertEqual(keys.count(found["expected"]), 2, shown)
        self.assertEqual(sorted(keys), sorted([found["expected"]] * 2 + [found["b"][1]["key"], "req-1", "sha256:feed"]),
                         shown)

    def test_a_refused_post_leaves_nothing_and_a_plain_fire_still_coalesces(self):
        found = self.result('''
loop = make_loop(webhook=True)
state.ensure_schema()
webhooks.secret("intake")
server = web()
try:
    refused = post(server, "intake", b"{}", {"X-Arete-Token": "wrong"})[0]
finally:
    server.shutdown()
    server.server_close()
after_refusal = snap()
sources.fire(loop, "manual")
sources.fire(loop, "manual")
finish({"refused": refused, "after_refusal": after_refusal, "snap": snap()})
''')
        self.assertEqual(found["refused"], 403, str(found))
        self.assertEqual([found["after_refusal"][t] for t in ("seen", "events", "queue")], [0, 0, 0], str(found))
        self.assertEqual(found["snap"]["queue_rows"], [{"grp": "", "keys": []}], str(found))


class SourceIdentityTests(unittest.TestCase):
    SCRIPT = (SCRIPTS / "source_identity.py").read_text()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        make_folders(self.root, "home", "config", "state", "logs", "tmp")
        self.env = scratch_env(self.root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                               HOME=str(self.root / "home"), TMPDIR=str(self.root / "tmp"),
                               IDENT_ROOT=str(self.root), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def found(self):
        done = run_script(self.SCRIPT, self.root, env=self.env, timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return behavior_result(done)

    def said(self, got, text):
        return [line for line in got["lines"] if text in line]

    def test_a_source_keeps_its_unpolled_events_through_edits_reorders_and_new_sources(self):
        found = self.found()
        shown = json.dumps(found, indent=1)

        plain = found["plain"]
        self.assertTrue(self.said(plain["first"], "first read"), shown)
        self.assertEqual(plain["edited"]["keys"], ["p3"], shown)
        self.assertFalse(self.said(plain["edited"], "first read") or self.said(plain["edited"], "settings changed"),
                         shown)
        self.assertEqual(plain["again"]["keys"], [], shown)
        self.assertEqual((plain["seen"], plain["queued"]), (["p1", "p2", "p3"], [["p3"]]), shown)

        outage = found["outage"]
        self.assertEqual([p["ran"] for p in outage["failed"]], [False, False], shown)
        self.assertEqual(len([line for line in outage["failed"][0]["lines"] + outage["failed"][1]["lines"]
                              if "poll failed" in line]), 1, shown)
        self.assertEqual((outage["bad"]["ok"], outage["bad"]["last_ok"]), (0, outage["good"]["last_ok"]), shown)
        self.assertEqual(outage["owner_after"], outage["owner"], shown)
        self.assertEqual(outage["fixed"]["keys"], [f"ABC-{n}" for n in range(2, 7)], shown)
        changed = self.said(outage["fixed"], "source settings changed")
        self.assertEqual(len(changed), 1, shown)
        self.assertIn("ABC-7", changed[0], shown)
        self.assertEqual(outage["further"]["keys"], [], shown)
        self.assertEqual(outage["queued"], [[f"ABC-{n}" for n in range(2, 7)]], shown)
        self.assertEqual(found["edge"]["keys"], ["on-edge"], shown)
        self.assertIn("just-before", self.said(found["edge"], "source settings changed")[0], shown)

        cmd = found["cmdscope"]
        self.assertEqual(cmd["changed"]["keys"], ["timed"], shown)
        self.assertIn("untimed", self.said(cmd["changed"], "source settings changed")[0], shown)
        self.assertEqual((cmd["repeat"]["keys"], cmd["seen"], cmd["queued"]),
                         ([], ["k0", "timed", "untimed"], [["timed"]]), shown)

        reorder = found["reorder"]
        self.assertEqual((reorder["first"]["keys"], reorder["second"]["keys"]), (["b2"], ["a5"]), shown)
        self.assertEqual((reorder["a"], reorder["b"]), (["a1", "a5"], ["b1", "b2"]), shown)
        self.assertEqual(reorder["ids"], ["demo#b", "demo#a"], shown)
        self.assertEqual(reorder["slots"], [{"cursors": 0, "seen": 0, "source_owners": 0}] * 2, shown)

        for case in ("above", "below"):
            got = found[case]
            self.assertEqual(got["polls"]["old"]["keys"], ["f3"], shown)
            self.assertEqual(got["polls"]["fresh"]["keys"], [], shown)
            self.assertIn("n1", self.said(got["polls"]["fresh"], "first read")[0], shown)
            self.assertFalse(self.said(got["polls"]["old"], "first read"), shown)
            self.assertEqual(got["slot"], {"cursors": 0, "seen": 0, "source_owners": 0}, shown)
            self.assertEqual((got["old_seen"], got["fresh_seen"]), (["f1", "f2", "f3"], ["n1"]), shown)

        foreign = found["foreign"]
        self.assertIn("s1", self.said(foreign["first"], "first read")[0], shown)
        self.assertEqual((foreign["foreign_seen"], foreign["solo_seen"], foreign["slot"]["cursors"]),
                         (["z1"], ["s1"], 1), shown)
        rename = found["rename"]
        self.assertEqual(rename["renamed"]["keys"], [], shown)
        self.assertIn("r1", self.said(rename["renamed"], "first read")[0], shown)
        self.assertEqual((rename["new_seen"], rename["old_seen"]), (["r1", "r2"], ["r1", "r2"]), shown)

        logs = found["logs"]
        self.assertEqual(len(logs["scoped"]["keys"]), 1, shown)
        self.assertIn("alpha", logs["scoped"]["titles"][0], shown)
        self.assertFalse(any("beta" in title for title in logs["scoped"]["titles"]), shown)
        self.assertEqual(sorted(logs["offsets"]), logs["names"], shown)
        self.assertEqual(logs["offsets"][logs["names"][1]][0], logs["two_size"], shown)
        self.assertEqual(len(logs["later"]["keys"]), 1, shown)
        self.assertIn("gamma", logs["later"]["titles"][0], shown)

        self.assert_legacy_identity_names_and_recovery(found, shown)

    def assert_legacy_identity_names_and_recovery(self, found, shown):
        legacy = found["legacy"]
        self.assertEqual(legacy["poll"]["keys"], ["l2"], shown)
        self.assertFalse(self.said(legacy["poll"], "first read") or self.said(legacy["poll"], "settings changed"),
                         shown)
        self.assertTrue(legacy["owner"].startswith("scope:"), shown)
        self.assertEqual((legacy["kept"], legacy["seen"], legacy["queued"]),
                         ("2026-01-01T00:00:00Z", ["l1", "l2"], [["l2"]]), shown)

        edit = found["legacy_edit"]
        self.assertEqual((edit["poll"]["keys"], edit["queued"], edit["seen"]), (["a3"], [["a3"]], ["a1", "a2", "a3"]),
                         shown)
        self.assertFalse(self.said(edit["poll"], "settings changed"), shown)

        names = found["names"]
        for case, reason in (("unnamed", "needs a name"), ("repeated", "more than one"), ("webhook", "reserved"),
                             ("metric", "reserved"), ("digits", "only digits"), ("characters", "lowercase"),
                             ("number", "lowercase")):
            self.assertIn(reason, names[case] or "", shown)
        self.assertEqual((names["single"], names["distinct"]), (None, None), shown)

        race = found["race"]
        self.assertFalse(race["named_started"], shown)
        self.assertEqual((race["queued"], race["named_seen"]), ([["r2"]], ["r1", "r2"]), shown)
        self.assertEqual(race["slot"], {"cursors": 0, "seen": 0, "source_owners": 0}, shown)

        retry = found["retry"]
        self.assertFalse(retry["failed"]["ran"], shown)
        self.assertEqual((retry["seen"], retry["queued_after_failure"]), (["r1"], []), shown)
        self.assertEqual(retry["after"]["owner"], retry["before"]["owner"], shown)
        self.assertEqual((retry["after"]["cursor"]["value"], retry["after"]["cursor"]["last_ok"]),
                         (retry["before"]["cursor"]["value"], retry["before"]["cursor"]["last_ok"]), shown)
        self.assertEqual((retry["healed"]["keys"], retry["queued"], retry["third"]["keys"]),
                         (["r2"], [["r2"]], []), shown)


class LoopMarkAndDurableWriteTests(unittest.TestCase):
    PRELUDE = (SCRIPTS / "loop_mark_durable_write_prelude.py").read_text()
    MARKER = "RESULT:"

    def session(self, body, setup=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
            (root / "config" / "config.toml").write_text("[replay]\nall = true\n")
            env = inherited_env(drop=("ARETE_LOOP",), **state_env(root / "state"),
                       ARETE_CONFIG_DIR=str(root / "config"),
                       ARETE_LOG_DIR=str(root / "logs"), ARETE_WORKTREES_DIR=str(root / "worktrees"),
                       TMPDIR=str(root / "tmp"), HOME=str(root / "home"), PYTHONPATH=str(support.SOURCE))
            script = self.PRELUDE + ("state.migrate()\n" if setup else "") + body
            done = subprocess.run([sys.executable, "-c", script, str(root)], env=env, cwd=support.SOURCE,
                                  capture_output=True, text=True, timeout=300)
            self.assertEqual(done.returncode, 0, done.stderr)
            return behavior_result(done, marker=self.MARKER)

    def test_two_loops_marking_together_keep_both_marks(self):
        found = self.session((SCRIPTS / "loop_mark_concurrent.py").read_text())
        self.assertEqual(len(found["trials"]), 20)
        for trial in found["trials"]:
            self.assertEqual(trial, [0, 0, False, "gh: HTTP 502", True, False])
        self.assertFalse(found["legacy"])

    def test_a_failed_mark_fails_the_loop_check_and_the_legacy_file_is_ignored_and_removed(self):
        found = self.session((SCRIPTS / "loop_mark_failure_and_legacy.py").read_text())
        self.assertFalse(found["failed"][0])
        self.assertIn("failed: gh: HTTP 502", found["failed"][1])
        self.assertTrue(found["cleared"][0])
        self.assertNotIn("failed:", found["cleared"][1])
        self.assertEqual(found["runs"], [[1, False, ["loop:poll"]], [0, False, True]])

    def test_a_corrupt_json_file_is_logged_once_per_version(self):
        found = self.session(r'''
p = paths.STATE_DIR / "triage.json"
default = {"d": 1}
def read():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        value = util.read_json(p, default)
    return [value, err.getvalue()]
out = {"missing": read()}
p.write_text('{"issue":')
out["first"], out["second"] = read(), read()
p.write_text('{"issue": [')
out["changed"] = read()
p.write_text("")
out["empty"] = read()
p.write_text('{"ok": true}')
out["valid"] = read()
print("RESULT:" + json.dumps(out))
''', setup=False)
        self.assertEqual(found["missing"], [{"d": 1}, ""])
        for key in ("first", "changed", "empty"):
            self.assertEqual(found[key][0], {"d": 1})
            self.assertEqual(found[key][1].count("\n"), 1)
            self.assertIn("triage.json", found[key][1])
        self.assertEqual(found["second"], [{"d": 1}, ""])
        self.assertEqual(found["valid"], [{"ok": True}, ""])

    def test_a_failed_replace_keeps_the_old_file_and_leaves_no_draft(self):
        found = self.session(r'''
folder = Path(sys.argv[1]) / "w"
folder.mkdir()
run = pipeline.Run.__new__(pipeline.Run)
run.dir = folder
(folder / "a.json").write_text("old-a")
(folder / "b.json").write_text("old-b")
out = {}
for name, call in (("save", lambda: run.save("a.json", {"x": 1})), ("json", lambda: util.write_json(folder / "b.json", {"x": 1}))):
    with mock.patch.object(util.os, "replace", side_effect=OSError("boom")):
        try:
            call()
            out[name] = "no error"
        except OSError as e:
            out[name] = str(e)
out["files"] = sorted(x.name for x in folder.iterdir())
out["bytes"] = [(folder / "a.json").read_text(), (folder / "b.json").read_text()]
print("RESULT:" + json.dumps(out))
''')
        self.assertEqual((found["save"], found["json"]), ("boom", "boom"))
        self.assertEqual(found["files"], ["a.json", "b.json"])
        self.assertEqual(found["bytes"], ["old-a", "old-b"])

    def test_a_durable_write_syncs_the_file_before_replace_and_the_folder_after(self):
        found = self.session((SCRIPTS / "durable_write_sync.py").read_text())
        for key in ("save", "json"):
            self.assertEqual(found[key][-2:], ["replace", "fsync"], key)
            self.assertLess(found[key].index("fullsync") if "fullsync" in found[key] else 0, found[key].index("replace"))
        self.assertEqual(found["fallback"][:3], ["fullsync", "fsync", "replace"])
        self.assertEqual(found["missing_constant_fsyncs"], 2)
        self.assertEqual(found["modes"], ["0o600", "0o600"])
        self.assertEqual(found["text"], "plain text")
        self.assertEqual(found["json_back"], {"x": 1})

    def test_an_unmigrated_database_shows_no_marks_and_marking_keeps_scheduling_columns(self):
        found = self.session((SCRIPTS / "loop_mark_unmigrated.py").read_text())
        self.assertEqual(found["unmigrated"], [{}, []])
        self.assertTrue(found["same"])
        self.assertFalse(found["new"])
        self.assertEqual(found["row"], [{"k": 1}, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", 1, "ok"])


if __name__ == "__main__":
    unittest.main()
