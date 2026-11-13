import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ON = "[oncall]\nenabled = true\n"


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.state, self.config, self.logs, self.wt = (base / n for n in ("state", "config", "logs", "wt"))
        for d in (self.state, self.config, self.logs, self.wt):
            d.mkdir()
        self.env = {**{k: v for k, v in os.environ.items() if "TOKEN" not in k},
                    "ARETE_STATE_DIR": str(self.state), "ARETE_CONFIG_DIR": str(self.config),
                    "ARETE_LOG_DIR": str(self.logs), "ARETE_WORKTREES_DIR": str(self.wt)}
        self.write_config(ON)
        self.log = self.logs / "app.log"

    def write_config(self, text):
        (self.config / "config.toml").write_text(text)

    def arete(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "arete.cli", "oncall", *args], cwd=ROOT, text=True,
                              capture_output=True, env={**self.env, **(env or {})})

    def py(self, code, env=None):
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, text=True, capture_output=True,
                              env={**self.env, **(env or {})})

    def incidents(self):
        out = self.arete("--json").stdout
        return [json.loads(line) for line in out.splitlines()]

    def append(self, *rows, to=None):
        with open(to or self.log, "a") as f:
            f.write("".join(row + "\n" for row in rows))

    def scan(self):
        res = self.arete("scan")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def baseline(self, *rows):
        self.append(*rows or ("2026-11-13T10:00:00Z INFO started",))
        self.scan()

    def sql(self, query, *params):
        c = sqlite3.connect(self.state / "arete.db")
        try:
            with c:
                return c.execute(query, params).fetchall()
        finally:
            c.close()

    def error(self, n, stamp="10:05:00"):
        return f"2026-11-13T{stamp}Z ERROR payment job 4f2a9c1e-1111-2222-3333-44445555666{n} failed after {n}0s: db"


class Files(Scratch):
    def test_baseline_then_one_error_opens_one_incident(self):
        self.baseline("2026-11-13T10:00:00Z ERROR before the watcher")
        self.assertEqual(self.incidents(), [])
        self.append(self.error(1))
        self.assertIn("1 opened", self.scan())
        (one,) = self.incidents()
        self.assertEqual((one["state"], one["class"], one["count"]), ("open", "unexpected", 1))
        self.assertIn("<uuid>", one["signature"])

    def test_repeat_rotate_truncate_restart_keeps_one_incident(self):
        self.baseline()
        self.append(self.error(1))
        self.scan()
        self.append(self.error(2, "11:15:09"))
        self.log.with_name("app.log.1").write_bytes(self.log.read_bytes())
        self.log.write_text("")
        self.append(self.error(3, "11:20:00"), self.error(4, "11:21:00"))
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["count"], 4)
        self.assertGreaterEqual(one["last_seen"], one["first_seen"])
        self.scan()
        self.assertEqual(self.incidents()[0]["count"], 4)

    def test_truncated_file_that_regrew_is_read_from_the_start(self):
        self.baseline("2026-11-13T10:00:00Z INFO " + "x" * 50)
        self.log.write_text("2026-11-13T10:00:00Z INFO short\n" + "".join(f"INFO more {i}\n" for i in range(20))
                            + self.error(1) + "\n")
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["count"], 1)

    def test_new_log_file_is_read_from_its_start(self):
        self.baseline()
        self.append("2026-11-13T10:00:00Z ERROR brand new file", to=self.logs / "other.log")
        self.scan()
        self.assertEqual(len(self.incidents()), 1)

    def test_a_read_over_the_limit_finishes_over_two_scans(self):
        self.baseline()
        filler = "INFO " + "z" * 90 + "\n"
        self.log.write_text(self.log.read_text() + filler * 11000 + "ERROR late failure one\n")
        self.scan()
        self.assertEqual(self.incidents(), [])
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["count"], 1)

    def test_expected_lines_open_nothing(self):
        self.baseline()
        self.append("2026-11-13T12:00:00Z [demo] build: claude usage limit reached, waiting until 16:00",
                    "2026-11-13T12:00:05Z WARN upstream busy, retrying in 30s (backoff 2 of 5)",
                    "2026-11-13T12:00:09Z [demo] check lint: FAIL (exit 1)",
                    json.dumps({"ts": "2026-11-13T12:00:10Z", "run": "pip-x", "seq": 4, "event": "wait",
                                "cause": "limit", "phase": "start"}),
                    json.dumps({"ts": "2026-11-13T12:00:11Z", "run": "pip-x", "seq": 5, "event": "step_end",
                                "step": "checks", "ok": False}))
        self.assertIn("5 expected", self.scan())
        self.assertEqual(self.incidents(), [])

    def test_tick_failed_is_orchestration_and_own_lines_open_nothing(self):
        self.baseline()
        self.append("2026-11-13T13:00:00Z tick failed: OperationalError: database is locked",
                    "2026-11-13T13:00:01Z on-call scan failed: ValueError: demo", to=self.logs / "serve.log")
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["class"], "orchestration")

    def test_failing_run_line_for_a_ticket_with_another_live_run_is_expected(self):
        script = ("from arete import record, state\n"
                  "a = record.start('pipeline', repo='demo', issue=7, pid=__import__('os').getpid())\n"
                  "b = record.start('pipeline', repo='demo', issue=7, pid=__import__('os').getpid())\n"
                  "print(a)")
        first = self.py(script).stdout.strip().splitlines()[-1]
        self.baseline()
        self.append(json.dumps({"ts": "2026-11-13T12:00:10Z", "run": first, "seq": 3, "event": "error",
                                "step": "build", "message": "boom"}))
        self.assertIn("1 expected", self.scan())
        self.assertEqual(self.incidents(), [])

    def test_every_serve_orchestration_message_opens_its_own_incident(self):
        self.baseline()
        gone = "new code does not import, so serve keeps the code it started with: "
        texts = ["watch: dropped queue item 17, the loop is off or gone",
                 "watch: dropped queue item 21, its run could not start, the second time",
                 gone + "ModuleNotFoundError: No module named 'arete.gone'",
                 "tick failed: OperationalError: database is locked",
                 "watch#0: poll failed: TimeoutError: timed out",
                 "sweeping left-over VMs failed: OSError: no route to host"]
        self.append(*(f"2026-11-13T13:00:0{n}Z {t}" for n, t in enumerate(texts)), to=self.logs / "serve.log")
        self.scan()
        found = self.incidents()
        self.assertEqual((len(found), {i["class"] for i in found}), (6, {"orchestration"}))
        self.append("2026-11-13T14:00:00Z watch: dropped queue item 18, the loop is off or gone",
                    "2026-11-13T14:00:01Z " + gone + "ModuleNotFoundError: No module named 'arete.other'",
                    to=self.logs / "serve.log")
        self.scan()
        found = self.incidents()
        self.assertEqual(len(found), 6)
        self.assertEqual(sorted(i["count"] for i in found), [1, 1, 1, 1, 2, 2])

    def test_secrets_and_home_paths_never_reach_the_database(self):
        self.baseline()
        self.append("2026-11-13T12:30:00Z ERROR upstream rejected zzsecretvalue12345 at /Users/someone/app " + "word " * 1500)
        self.assertEqual(self.arete("scan", env={"DEMO_API_TOKEN": "zzsecretvalue12345"}).returncode, 0)
        (one,) = self.incidents()
        shown = self.arete("show", str(one["id"])).stdout
        self.assertIn("[redacted]", shown)
        for text in (shown, json.dumps(one)):
            self.assertNotIn("zzsecretvalue12345", text)
            self.assertNotIn("/Users/someone", text)
        self.assertLessEqual(len(one["evidence"]), 4000)

    def test_owner_source_is_watched_with_its_pattern(self):
        other = Path(self.tmp.name) / "svc.log"
        other.write_text("start\n")
        self.write_config(f'{ON}[[oncall.source]]\npath = "{other}"\npattern = "boom"\n')
        self.scan()
        self.append("kaboom boom in worker 3", to=other)
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["source"], str(other))


class Rotation(Scratch):
    def rotate(self, rename):
        older = self.log.with_name("app.log.1")
        if rename:
            self.log.rename(older)
            self.log.write_text("")
        else:
            older.write_bytes(self.log.read_bytes())
            self.log.write_text("")

    def check(self, rename, more):
        self.log.write_text("")
        self.scan()
        self.append(self.error(1))
        self.rotate(rename)
        if more:
            self.append(self.error(2, "10:06:00"))
        self.assertIn("1 opened", self.scan())
        want = 2 if more else 1
        self.assertEqual([i["count"] for i in self.incidents()], [want])
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [want])
        self.append(self.error(3, "10:07:00"))
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [want + 1])

    def test_copy_truncate_at_offset_zero(self):
        self.check(False, False)

    def test_copy_truncate_at_offset_zero_with_new_lines(self):
        self.check(False, True)

    def test_rename_at_offset_zero(self):
        self.check(True, False)

    def test_rename_at_offset_zero_with_new_lines(self):
        self.check(True, True)

    def test_rename_with_a_scan_during_the_gap(self):
        self.log.write_text("")
        self.scan()
        self.append(self.error(1))
        self.log.rename(self.log.with_name("app.log.1"))
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [1])
        self.append(self.error(2, "10:06:00"))
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [2])
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [2])

    def test_an_old_rotated_file_is_never_read(self):
        self.log.with_name("app.log.1").write_text("ERROR old rotated failure\n")
        self.log.write_text("")
        self.scan()
        self.scan()
        self.assertEqual(self.incidents(), [])
        self.append(self.error(1))
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [1])

    def test_a_renamed_file_with_a_different_prefix_and_shared_suffix_is_read(self):
        suffix = "".join(f"2026-11-13T10:00:00Z INFO steady line {n} " + "x" * 20 + "\n" for n in range(20))
        self.log.write_text("")
        self.scan()
        self.append(self.error(1))
        self.log.write_text(self.log.read_text() + suffix)
        self.log.rename(self.log.with_name("app.log.1"))
        self.log.write_text("2026-11-13T10:00:00Z INFO fresh start\n" + suffix)
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [1])

    def test_identical_repeated_lines_across_a_rename_are_both_counted(self):
        self.log.write_text("")
        self.scan()
        self.append(self.error(1))
        self.log.rename(self.log.with_name("app.log.1"))
        self.append(self.error(1))
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [2])

    def test_comparing_a_large_copy_hashes_a_bounded_number_of_bytes(self):
        code = ("import hashlib, os, sys\n"
                "from arete import oncall\n"
                "body = b'INFO ' + b'z' * 95 + b'\\n'\n"
                "data = body * 30000\n"
                "name = os.path.join(sys.argv[1], 'big.log')\n"
                "open(name, 'wb').write(data)\n"
                "open(name + '.1', 'wb').write(data)\n"
                "seen = []\n"
                "real = hashlib.sha1\n"
                "class Counting:\n"
                "    def __init__(self): self.h = real()\n"
                "    def update(self, b): seen.append(len(b)); self.h.update(b)\n"
                "    def hexdigest(self): return self.h.hexdigest()\n"
                "oncall.hashlib.sha1 = lambda *a: Counting() if not a else real(*a)\n"
                "older = oncall.stamp(name)\n"
                "inode = os.stat(name).st_ino\n"
                "print(oncall.still_holds(name, len(data), older, inode, inode), sum(seen), oncall.MAX_READ)\n"
                "open(name, 'r+b').write(b'X')\n"
                "print(oncall.still_holds(name, len(data), older, inode, inode))")
        res = subprocess.run([sys.executable, "-c", code, str(self.logs)], cwd=ROOT, text=True,
                             capture_output=True, env=self.env)
        first, second = res.stdout.splitlines()
        held, hashed, limit = first.split()
        self.assertEqual(held, "True", res.stderr)
        self.assertLessEqual(int(hashed), 2 * int(limit) + 1024)
        self.assertEqual(second, "False")

    def test_a_copy_not_yet_truncated_is_not_read_twice(self):
        self.log.write_text("")
        self.scan()
        self.append(self.error(1))
        self.log.with_name("app.log.1").write_bytes(self.log.read_bytes())
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [1])
        self.log.write_text("")
        self.append(self.error(2, "10:06:00"))
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [2])


class Backlogs(Scratch):
    FILLER = "INFO " + "z" * 90 + "\n"

    def start_run(self, **fields):
        code = ("import os\nfrom arete import record\n"
                f"print(record.start('pipeline', repo='demo', issue=7, pid=os.getppid(), **{fields!r}))")
        return self.py(code).stdout.strip().splitlines()[-1]

    def test_rotated_backlog_over_the_limit_drains_across_scans(self):
        self.baseline()
        self.log.write_text(self.log.read_text() + self.FILLER * 11000 + self.error(1) + "\n")
        self.log.with_name("app.log.1").write_bytes(self.log.read_bytes())
        self.log.write_text("")
        self.scan()
        self.assertEqual(self.incidents(), [])
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [1])

    def test_finished_run_backlog_drains_across_scans_and_restarts(self):
        run = self.start_run()
        target = self.state / "runs" / run / "run.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text("")
        self.baseline()
        failure = {"ts": "2026-11-13T12:00:00Z", "run": run, "seq": 9, "event": "error", "step": "build",
                   "message": "boom"}
        target.write_text(self.FILLER * 11000 + json.dumps(failure) + "\n")
        self.sql("UPDATE runs SET status = 'done', started = '2020-01-01T00:00:00Z' WHERE id = ?", run)
        self.scan()
        self.assertEqual(self.incidents(), [])
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [1])

    def test_rename_rotation_with_a_gap_loses_nothing(self):
        self.baseline()
        self.append(self.error(1))
        self.scan()
        self.append(self.error(2))
        self.log.rename(self.log.with_name("app.log.1"))
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [2])
        self.append(self.error(3))
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [3])

    def test_a_runs_own_repair_failure_in_its_plain_log_is_expected(self):
        self.baseline()
        self.append("2026-11-13T12:00:00Z [demo] fix2: FAILED tool unavailable",
                    "2026-11-13T12:00:01Z [demo] review_fix1: FAILED tool unavailable")
        self.assertIn("2 expected", self.scan())
        self.assertEqual(self.incidents(), [])

    def test_one_traceback_opens_one_incident_however_often_it_repeats(self):
        self.baseline()
        block = ["Traceback (most recent call last):", '  File "app.py", line 3, in run',
                 "    raise Exception('database unavailable')", "Exception: database unavailable"]
        self.append(*block, "2026-11-13T12:00:00Z INFO between", *block)
        self.scan()
        self.append(*block)
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["count"], 3)
        self.assertIn("database unavailable", one["signature"])

    def run_jsonl(self, run, *rows):
        return [json.dumps({"ts": "2026-11-13T12:00:00Z", "run": run, "seq": n, **row})
                for n, row in enumerate(rows, 1)]

    def test_a_run_that_fails_its_checks_after_review_fixes_is_expected(self):
        run = self.start_run()
        target = self.state / "runs" / run / "run.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text("")
        self.baseline()
        ended = {"event": "error", "step": "respond", "message": "the checks failed after the review fixes"}
        seq = self.run_jsonl(run, {"event": "step_start", "step": "implement", "try": 2, "atom": "review_repair"},
                             {"event": "step_end", "step": "implement", "try": 2, "result": "pass", "ok": True,
                              "atom": "review_repair"},
                             {"event": "step_start", "step": "checks", "try": 3},
                             {"event": "step_end", "step": "checks", "try": 3, "result": "fail", "ok": False,
                              "next": "implement"},
                             {"event": "step_start", "step": "respond"},
                             {"event": "step_end", "step": "respond", "ok": True}, ended)
        self.append(*seq, to=target)
        self.append("2026-11-13T12:00:00Z [demo#7] check unit: FAIL (exit 1)",
                    "2026-11-13T12:00:01Z [demo#7] failed: the checks failed after the review fixes",
                    seq[-1],
                    "2026-11-13T12:00:02Z [demo#7] done: failed the checks failed after the review fixes",
                    to=self.logs / "runs.log")
        out = self.scan()
        self.assertIn("0 opened", out)
        self.assertIn("5 expected", out)
        self.assertEqual(self.incidents(), [])
        self.assertIn("5 expected events counted", self.arete().stdout)
        self.append(*self.run_jsonl(run, {"event": "error", "step": "build", "message": "crashed: KeyError: 'title'"}),
                    to=target)
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["class"], "unexpected")

    def test_checks_failing_after_a_ci_or_in_use_fix_are_expected(self):
        self.baseline()
        used = "the checks failed after fixing what the check in use found"
        errors = [json.dumps({"ts": "2026-11-13T12:00:03Z", "run": "pip-gone", "seq": n, "event": "error",
                              "step": "respond", "message": m})
                  for n, m in enumerate(("the checks failed after the CI fix", used), 1)]
        self.append("2026-11-13T12:00:00Z [demo#7] failed: the checks failed after the CI fix",
                    "2026-11-13T12:00:01Z [demo#7] done: failed https://github.com/demo/demo/pull/5",
                    f"2026-11-13T12:00:02Z [demo#7] failed: {used}",
                    f"2026-11-13T12:00:03Z [demo#7] done: failed {used}", *errors, to=self.logs / "runs.log")
        self.assertIn("0 opened", self.scan())
        self.assertEqual(self.incidents(), [])

    def test_a_run_that_starts_and_ends_during_a_slow_scan_is_read_by_the_next(self):
        self.baseline()
        code = ("import time\n"
                "from arete import config, lines, oncall, record, state\n"
                "original = oncall.Watch.run_files\n"
                "def slow(self, since):\n"
                "    found = original(self, since)\n"
                "    run = record.start('pipeline', repo='demo', issue=9)\n"
                "    lines.write(run, 'error', step='build', message='boom during the scan')\n"
                "    with state.db() as c:\n"
                "        c.execute(\"UPDATE runs SET status = 'failed' WHERE id = ?\", (run,))\n"
                "    time.sleep(1.2)\n"
                "    return found\n"
                "oncall.Watch.run_files = slow\n"
                "oncall.scan(config.load())")
        self.assertEqual(self.py(code).returncode, 0)
        self.assertEqual(self.incidents(), [])
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["class"], "unexpected")
        self.assertIn("boom during the scan", one["signature"])
        self.scan()
        self.assertEqual(self.incidents()[0]["count"], 1)

    def test_scan_interrupted_before_recording_loses_nothing(self):
        self.baseline()
        self.append(self.error(1))
        code = ("from arete import config, oncall, state\n"
                "def broken(*a): raise RuntimeError('storage failed')\n"
                "state.commit_scan = broken\n"
                "oncall.scan(config.load())")
        self.assertNotEqual(self.py(code).returncode, 0)
        self.assertEqual(self.incidents(), [])
        self.scan()
        self.assertEqual([i["count"] for i in self.incidents()], [1])

    def test_stopped_heartbeats_are_found_when_the_step_start_is_beyond_the_tail(self):
        old = "2026-11-13T00:00:00Z"
        run = self.start_run(started=old)
        target = self.state / "runs" / run / "run.jsonl"
        target.parent.mkdir(parents=True)
        rows = [{"ts": old, "run": run, "seq": 1, "event": "step_start", "step": "build"}]
        rows += [{"ts": old, "run": run, "seq": n + 1, "event": "heartbeat"} for n in range(1, 1500)]
        target.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertGreater(target.stat().st_size, 65536)
        self.baseline()
        (one,) = self.incidents()
        self.assertEqual((one["class"], one["count"]), ("stalled", 1))
        self.assertIn("heartbeats stopped", one["signature"])


class Others(Scratch):
    def test_heartbeat_check_failing_across_two_beats(self):
        self.baseline()
        beat = {"ts": "2026-11-13T13:00:00Z", "ok": False, "actions": [],
                "checks": [{"key": "disk:home", "ok": False, "detail": "3 GB free", "severity": "fail"}]}
        (self.state / "heartbeat.json").write_text(json.dumps(beat))
        self.scan()
        self.assertEqual(self.incidents(), [])
        (self.state / "heartbeat.json").write_text(json.dumps({**beat, "ts": "2026-11-13T14:00:00Z"}))
        self.scan()
        (one,) = self.incidents()
        self.assertEqual(one["class"], "heartbeat")
        self.assertIn("disk:home", one["signature"])

    def test_stalled_run_opens_one_incident_once(self):
        self.baseline()
        self.py("from arete import record; record.start('pipeline', repo='demo', issue=7, "
                "started='2026-11-13T00:00:00Z', pid=999999)")
        self.scan()
        self.scan()
        (one,) = self.incidents()
        self.assertEqual((one["class"], one["count"]), ("stalled", 1))

    def test_show_prints_evidence_and_notes_and_unknown_id_fails(self):
        self.baseline()
        self.append(self.error(1))
        self.scan()
        self.append(self.error(2))
        self.scan()
        shown = self.arete("show", "1").stdout
        self.assertIn("evidence:", shown)
        self.assertIn("opened", shown)
        self.assertIn("seen again", shown)
        missing = self.arete("show", "999")
        self.assertEqual((missing.returncode, missing.stderr.strip()), (1, "no incident 999"))

    def test_off_does_nothing(self):
        self.write_config("")
        self.append("2026-11-13T15:00:00Z ERROR something new")
        self.assertIn("off", self.scan())
        self.assertEqual(self.incidents(), [])
        code = ("from arete import serve; s = serve.Server(); s.watch_oncall(); print(s.oncall)")
        self.assertEqual(self.py(code).stdout.strip(), "None")
        self.assertEqual(self.sql("SELECT count(*) FROM cursors WHERE source LIKE 'oncall:%'"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM incidents"), [(0,)])

    def test_serve_runs_the_scan_on_the_pool(self):
        self.baseline()
        self.append(self.error(1))
        code = ("from arete import serve; s = serve.Server(); s.watch_oncall(); s.oncall.result(30)")
        self.assertEqual(self.py(code).returncode, 0)
        self.assertEqual(self.sql("SELECT ok FROM cursors WHERE source = 'oncall:scan'"), [(1,)])
        self.assertEqual(len(self.incidents()), 1)

    def test_heartbeat_check_fails_when_crashed_or_stale_and_is_empty_when_off(self):
        self.baseline()
        code = ("from arete import config, heartbeat, oncall\n"
                "cfg = config.load()\n"
                "print([c.ok for c in heartbeat.check_oncall(cfg)])\n"
                "oncall.crashed('boom')\n"
                "print([(c.ok, c.detail) for c in heartbeat.check_oncall(cfg)])")
        out = self.py(code).stdout.splitlines()
        self.assertEqual(out[0], "[True]")
        self.assertIn("last scan crashed: boom", out[1])
        self.scan()
        self.sql("UPDATE cursors SET polled = '2026-11-13T00:00:00Z' WHERE source = 'oncall:scan'")
        self.assertIn("stale", self.py("from arete import config, heartbeat\n"
                                       "print(heartbeat.check_oncall(config.load())[0].detail)").stdout)
        self.write_config("")
        self.assertEqual(self.py("from arete import config, heartbeat\n"
                                 "print(heartbeat.check_oncall(config.load()))").stdout.strip(), "[]")

    def test_prune_drops_old_incidents_and_notes(self):
        self.baseline()
        self.append(self.error(1))
        self.scan()
        self.sql("UPDATE incidents SET last_seen = '2020-01-01T00:00:00Z'")
        self.sql("UPDATE incident_notes SET ts = '2020-01-01T00:00:00Z'")
        self.assertEqual(self.py("from arete import state; state.prune()").returncode, 0)
        self.assertEqual(self.sql("SELECT (SELECT count(*) FROM incidents), (SELECT count(*) FROM incident_notes)"),
                         [(0, 0)])

    def test_a_watch_loop_with_a_log_source_still_gets_its_events(self):
        self.baseline()
        code = ("import os, types\n"
                "from arete import config, sources\n"
                f"p = {str(self.log)!r}\n"
                "loop = types.SimpleNamespace(name='watch', sources=[{'type': 'log', 'path': p}])\n"
                "print(len(sources.poll(loop, 0, config.Config())))\n"
                "open(p, 'a').write('2026-11-13T17:00:00Z ERROR loop and watcher both see this\\n')\n"
                "os.system(f'{__import__(\"sys\").executable} -m arete.cli oncall scan > /dev/null')\n"
                "found = sources.poll(loop, 0, config.Config())\n"
                "print(len(found), found[0]['type'], found[0]['data']['count'])")
        res = self.py(code)
        self.assertEqual(res.stdout.split("\n")[:2], ["0", "1 log.error 1"], res.stderr)
        self.assertEqual(len(self.incidents()), 1)

    def test_bad_config_raises(self):
        for text in ("[oncall]\nbogus = 1\n", "[oncall]\nenabled = 'yes'\n",
                     f'{ON}[[oncall.source]]\npath = "/tmp/x.log"\npattern = "("\n',
                     f'{ON}[[oncall.source]]\npath = "/tmp/x.log"\nrepo = "nope"\n'):
            self.write_config(text)
            res = self.arete("scan")
            self.assertNotEqual(res.returncode, 0, text)
        self.assertIn("(", res.stderr)


if __name__ == "__main__":
    unittest.main()
