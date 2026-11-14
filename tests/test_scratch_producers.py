import contextlib
import ctypes
import fcntl
import io
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from arete import launchd, paths, pipeline, scratch

ON_MAC = sys.platform == "darwin"
UID = os.getuid()
AGENT_PATH = "/System/Library/LaunchAgents/com.apple.fake.plist"
AGENT_EXE = "/System/Library/PrivateFrameworks/Fake.framework/Versions/A/fakeagentd"
XPC_EXE = "/System/Library/PrivateFrameworks/Fake.framework/XPCServices/Fake.xpc/Contents/MacOS/Fake"
XPC_PATH = "/System/Library/PrivateFrameworks/Fake.framework/XPCServices/Fake.xpc"

SERVICES = "\tservices = {\n" + "".join(f"\t\t{row}\n" for row in (
    "   321      -     com.apple.one", "     -      0     com.apple.idle", "   654    -     com.apple.two")) + "\t}\n"
DOMAIN = f"gui/{UID} = {{\n{SERVICES}\tsubdomains = {{\n\t\tpid/77\n\t\tbogus/1\n\t}}\n}}\n"
SERVICE = "com.apple.one = {\n\tpath = /a/b.plist\n\tpid = 321\n\tnested = {\n\t\tpath = /ignored\n\t}\n\tpath = /later\n}\n"


def domain_print(rows: str, subdomains: str = "") -> str:
    return f"x = {{\n\tservices = {{\n{rows}\t}}\n{subdomains}}}\n"


class ParserTests(unittest.TestCase):
    def test_running_lists_only_live_services(self):
        self.assertEqual(launchd.running(DOMAIN), {321: ["com.apple.one"], 654: ["com.apple.two"]})
        self.assertIsNone(launchd.running("x = {\n\ttype = login\n}\n"))

    def test_listed_and_labels_cover_idle_and_running_jobs(self):
        self.assertEqual(launchd.listed(DOMAIN), ["com.apple.idle", "com.apple.one", "com.apple.two"])
        self.assertIsNone(launchd.listed("x = {\n\ttype = login\n}\n"))
        with mock.patch.object(launchd, "printed", return_value=DOMAIN):
            self.assertEqual(sorted(launchd.labels(UID)), ["gui", "user"])
        with mock.patch.object(launchd, "printed", side_effect=lambda target: DOMAIN if "gui" in target else None):
            self.assertIsNone(launchd.labels(UID))

    def test_pid_domains_and_fields(self):
        self.assertEqual(launchd.pid_domains(DOMAIN), ["pid/77"])
        self.assertEqual(launchd.pid_domains("nothing"), [])
        found = launchd.fields(SERVICE)
        self.assertEqual((found["path"], found["pid"]), ("/a/b.plist", "321"))

    def table_for(self, prints: dict[str, str | None], pid: int, bundled: bool = False):
        with mock.patch.object(launchd, "printed", side_effect=lambda target: prints.get(target)):
            return launchd.Table(UID).job(pid, bundled)

    def test_job_found_in_gui_user_and_pid_domains(self):
        empty = domain_print("")
        prints = {f"gui/{UID}": DOMAIN, f"user/{UID}": empty, f"gui/{UID}/com.apple.one": SERVICE}
        self.assertEqual(self.table_for(prints, 321), launchd.Job(f"gui/{UID}", "com.apple.one", "/a/b.plist", 321))
        prints = {f"gui/{UID}": empty, f"user/{UID}": DOMAIN, f"user/{UID}/com.apple.one": SERVICE}
        self.assertEqual(self.table_for(prints, 321).domain, f"user/{UID}")
        prints = {f"gui/{UID}": domain_print("", "\tsubdomains = {\n\t\tpid/77\n\t}\n"), f"user/{UID}": empty,
                  "pid/77": domain_print("\t\t  900  -  com.apple.x\n"),
                  "pid/77/com.apple.x": "com.apple.x = {\n\tpath = /x.xpc\n\tpid = 900\n}\n"}
        self.assertIsNone(self.table_for(prints, 900))
        self.assertEqual(self.table_for(prints, 900, True).domain, "pid/77")

    def test_job_refuses_what_it_cannot_read(self):
        empty = domain_print("")
        duplicate = domain_print("\t\t  5  -  a\n\t\t  5  -  b\n")
        self.assertRaises(launchd.Unreadable, self.table_for, {f"gui/{UID}": duplicate, f"user/{UID}": empty}, 5)
        self.assertRaises(launchd.Unreadable, self.table_for, {f"gui/{UID}": None, f"user/{UID}": empty}, 5)
        self.assertIsNone(self.table_for({f"gui/{UID}": empty, f"user/{UID}": empty}, 5))
        wrong = {f"gui/{UID}": DOMAIN, f"user/{UID}": empty, f"gui/{UID}/com.apple.one": SERVICE.replace("321", "1")}
        self.assertRaises(launchd.Unreadable, self.table_for, wrong, 321)


@unittest.skipUnless(ON_MAC, "the sealed volume is a macOS feature")
class SealedTests(unittest.TestCase):
    def test_sealed_system_executables_outside_system_folder(self):
        present = [p for p in ("/usr/libexec/secinitd", "/usr/libexec/siriknowledged", "/bin/sh") if os.path.exists(p)]
        if not present:
            self.skipTest("no sealed executable to check")
        for path in present:
            self.assertTrue(launchd.sealed(path), path)

    def test_writable_paths_are_not_sealed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(launchd.sealed(directory))
        self.assertFalse(launchd.sealed("relative/path"))


def proc(pid, identity, parent=0, group=None, executable=AGENT_EXE):
    return scratch.Process(pid, identity, parent, pid if group is None else group, executable)


@unittest.skipUnless(ON_MAC, "producer inspection needs macOS")
class FakeTableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "authority-parent").mkdir()
        self.ws, self.run_dir = self.root / "worktrees/ws", self.root / "runs/r1"
        self.ws.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)
        self.table: dict[int, scratch.Process] = {}
        self.service_path, self.domain, self.program = AGENT_PATH, f"gui/{UID}", AGENT_EXE
        self.readable = True
        self.job_pid = 0
        for patch in (
                mock.patch.object(scratch, "AUTHORITY", self.root / "authority-parent/verification-authority"),
                mock.patch.object(paths, "WORKTREES_DIR", self.root / "worktrees"),
                mock.patch.object(scratch, "processes", lambda *workspaces: dict(self.table)),
                mock.patch.object(scratch, "identity_of", self.identity_of),
                mock.patch.object(launchd, "printed", self.printed),
                mock.patch.object(launchd, "sealed", lambda path: path.startswith(("/System/", "/usr/libexec/"))),
                mock.patch.object(launchd, "declared_program", lambda path: self.program)):
            patch.start()
            self.addCleanup(patch.stop)
        scratch.RUN_BASELINES.clear()
        scratch.RUN_LABELS.clear()
        self.addCleanup(scratch.RUN_BASELINES.clear)
        self.addCleanup(scratch.RUN_LABELS.clear)
        self.loaded = {"label900"}
        self.attempt = scratch.make("r1", "verify", self.ws, self.run_dir)
        self.addCleanup(self.release)

    def release(self):
        lease = scratch.LEASES.pop(self.attempt.selector, None)
        if lease is not None:
            os.close(lease)

    def identity_of(self, pid):
        return next((p.identity for p in self.table.values() if p.pid == pid), None)

    def printed(self, target):
        if not self.readable:
            return None
        listed = {p.pid: p for p in self.table.values()}
        if target in (f"gui/{UID}", f"user/{UID}"):
            running = f"label{self.job_pid}"
            rows = "".join(f"\t\t-  0  {label}\n" for label in sorted(self.loaded) if label != running)
            if self.job_pid and target == self.domain:
                rows += f"\t\t{self.job_pid}  -  {running}\n"
            elif target != self.domain:
                rows = ""
            return domain_print(rows, "\tsubdomains = {\n\t\tpid/77\n\t}\n")
        if target == "pid/77":
            rows = "".join(f"\t\t{pid}  -  label{pid}\n" for pid in listed if target == self.domain and pid == self.job_pid)
            return domain_print(rows)
        if target.startswith(self.domain + "/label"):
            pid = int(target.rsplit("label", 1)[1])
            return f"label{pid} = {{\n\tpath = {self.service_path}\n\tpid = {self.pid_text(pid)}\n}}\n"
        return None

    def pid_text(self, pid):
        return str(pid)

    def start(self, p, **settings):
        for name, value in settings.items():
            setattr(self, name, value)
        self.table[p.identity] = p
        self.job_pid = p.pid if self.domain else 0

    def held(self):
        return {h["pid"]: h for h in scratch.holders(scratch.records()[0])}

    def retain_with(self, p, **settings):
        self.start(p, **settings)
        self.assertEqual(scratch.end(self.attempt, self.run_dir)[2], [self.attempt.root])
        return scratch.records()[0]

    def alter_record(self, change):
        with scratch.registry() as (fd, key):
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = scratch.load(fd, key, self.attempt.selector)
            change(data)
            scratch.store(fd, key, data)

    def test_sealed_job_bootstrapped_after_the_baseline_holds(self):
        record = self.retain_with(proc(901, 5001))
        self.assertEqual(record["state"], "retained")
        self.assertEqual([(h["pid"], h["name"], h["reason"]) for h in record["diagnostic"]["holders"]],
                         [(901, "fakeagentd", scratch.SUBMITTED)])
        self.alter_record(lambda data: data["labels"]["gui"].append("label901"))
        self.assertEqual(scratch.end(self.attempt, self.run_dir)[2], [])
        self.assertEqual(scratch.records()[0]["state"], "done")
        self.assertIn(5001, self.table)

    def test_record_without_label_baseline_holds_sealed_job_as_unclassified(self):
        self.alter_record(lambda data: data.pop("labels"))
        self.start(proc(900, 5000))
        for missing in ("absent", "unread"):
            with self.subTest(missing):
                if missing == "unread":
                    self.alter_record(lambda data: data.update(labels=None))
                self.assertEqual([(h["pid"], h["name"], h["reason"]) for h in self.held().values()],
                                 [(900, "fakeagentd", scratch.UNCLASSIFIED_HOLDER)])
                self.assertEqual(scratch.end(self.attempt, self.run_dir)[2], [self.attempt.root])
                record = scratch.records()[0]
                self.assertEqual(record["state"], "retained")
                self.assertEqual([(h["pid"], h["name"], h["reason"]) for h in record["diagnostic"]["holders"]],
                                 [(900, "fakeagentd", scratch.UNCLASSIFIED_HOLDER)])
        self.table.clear()
        self.start(proc(900, 5000, executable="/bin/sh"), service_path="(submitted by x)", program="/bin/sh")
        self.assertEqual([h["reason"] for h in self.held().values()], [scratch.SUBMITTED])

    def test_exemption_does_not_carry_to_an_attempt_without_the_label(self):
        other_ws, other_run = self.root / "worktrees/other", self.root / "runs/r2"
        other_ws.mkdir()
        other_run.mkdir()
        self.loaded = set()
        second = scratch.make("r2", "verify", other_ws, other_run)
        self.addCleanup(lambda: os.close(scratch.LEASES.pop(second.selector)) if second.selector in scratch.LEASES
                        else None)
        self.loaded = {"label900"}
        self.start(proc(900, 5000))
        first_record, second_record = (next(r for r in scratch.records() if r["attempt"]["selector"] == a.selector)
                                       for a in (self.attempt, second))
        self.assertEqual(scratch.holders(first_record), [])
        self.assertEqual([(h["pid"], h["name"], h["reason"]) for h in scratch.holders(second_record)],
                         [(900, "fakeagentd", scratch.SUBMITTED)])
        self.assertEqual(scratch.end(self.attempt, self.run_dir)[2], [])
        self.assertEqual(scratch.end(second, other_run)[2], [second.root])

    def test_apple_agent_started_after_baseline_does_not_hold(self):
        self.assertIn("label900", scratch.records()[0]["labels"]["gui"])
        self.start(proc(900, 5000))
        kept = scratch.end(self.attempt, self.run_dir)[2]
        self.assertEqual(kept, [])
        self.assertEqual(scratch.records()[0]["state"], "done")
        self.assertIn(5000, self.table)

    def test_submitted_job_holds(self):
        record = self.retain_with(proc(900, 5000, executable="/bin/sh"), service_path="(submitted by launchctl.1)",
                                  program="/bin/sh")
        reasons = [(h["pid"], h["name"], h["reason"]) for h in record["diagnostic"]["holders"]]
        self.assertEqual(reasons, [(900, "sh", scratch.SUBMITTED)])
        self.assertEqual(record["state"], "retained")

    def test_plist_off_the_sealed_volume_holds(self):
        record = self.retain_with(proc(900, 5000), service_path="/Library/LaunchAgents/x.plist")
        self.assertEqual(record["diagnostic"]["holders"][0]["reason"], scratch.SUBMITTED)

    def test_process_in_no_service_table_is_an_orphan_with_its_child(self):
        self.table[5001] = proc(901, 5001, parent=5000, executable="/bin/sleep")
        record = self.retain_with(proc(900, 5000, executable="/usr/bin/python3"), domain="")
        self.assertEqual([(h["pid"], h["reason"]) for h in record["diagnostic"]["holders"]],
                         [(900, scratch.ORPHAN), (901, scratch.ORPHAN)])

    def test_unreadable_or_mismatched_tables_hold_as_unclassified(self):
        record = self.retain_with(proc(900, 5000), readable=False)
        self.assertEqual(record["diagnostic"]["holders"][0]["reason"], scratch.UNCLASSIFIED_HOLDER)

    def test_program_mismatch_holds_as_unclassified(self):
        record = self.retain_with(proc(900, 5000), program="/System/Library/other")
        self.assertEqual(record["diagnostic"]["holders"][0]["reason"], scratch.UNCLASSIFIED_HOLDER)

    def test_pid_mismatch_holds_as_unclassified(self):
        self.pid_text = lambda pid: str(pid + 1)
        record = self.retain_with(proc(900, 5000))
        self.assertEqual(record["diagnostic"]["holders"][0]["reason"], scratch.UNCLASSIFIED_HOLDER)

    def test_xpc_service_is_exempt_only_when_its_owner_is_unrelated(self):
        xpc = proc(900, 5000, executable=XPC_EXE)
        self.start(xpc, domain="pid/77", service_path=XPC_PATH, program=XPC_EXE)
        self.table[6000] = proc(77, 6000, parent=-1, executable="/usr/bin/owner")
        self.assertEqual(scratch.end(self.attempt, self.run_dir)[2], [])

    def test_xpc_service_of_a_producer_holds(self):
        self.table[6000] = proc(77, 6000, parent=0, executable="/usr/bin/python3")
        record = self.retain_with(proc(900, 5000, executable=XPC_EXE), domain="pid/77", service_path=XPC_PATH,
                                  program=XPC_EXE)
        self.assertIn((900, scratch.UNCLASSIFIED_HOLDER), [(h["pid"], h["reason"]) for h in record["diagnostic"]["holders"]])

    def test_diagnostic_log_and_retry_name_the_holder_and_next_poll_finalizes(self):
        record = self.retain_with(proc(900, 5000, executable="/bin/sh"), service_path="(submitted by x)",
                                  program="/bin/sh")
        self.assertEqual(set(record["diagnostic"]), {"message", "holders"})
        self.assertEqual(record["diagnostic"]["message"], scratch.WAITING)
        expected = "pid 900 sh (submitted launchd job)"
        self.assertIn(f"held by {expected}", pipeline.retained_line("verify", self.attempt))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pipeline.retry_scratch()
        for part in ('"pid": 900', '"name": "sh"', '"reason": "submitted launchd job"'):
            self.assertIn(part, out.getvalue())
        self.assertTrue(scratch.pending(self.ws))
        self.assertTrue(pipeline.scratch_left(self.ws, []))
        self.table.clear()
        self.release()
        self.assertEqual(scratch.recover_pending(), [self.attempt.root])
        self.assertEqual(scratch.records()[0]["state"], "done")
        self.assertFalse(scratch.pending(self.ws))
        self.assertEqual(pipeline.scratch_left(self.ws, []), [])

    def test_live_producer_group_holds(self):
        child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        scratch.producer_group(self.attempt, "p1", child.pid)
        self.table[7000] = proc(child.pid, 7000, parent=-1, executable="/bin/sleep")
        record = self.retain_with(proc(901, 7001, executable="/usr/bin/true"), domain="")
        self.assertIn((child.pid, "sleep", scratch.IN_GROUP),
                      [(h["pid"], h["name"], h["reason"]) for h in record["diagnostic"]["holders"]])

    def test_agents_declaring_usr_libexec_executables_do_not_hold(self):
        for name in ("secinitd", "siriknowledged"):
            executable = f"/usr/libexec/{name}"
            self.table.clear()
            self.start(proc(900, 5000, executable=executable), program=executable)
            self.assertEqual(scratch.holders(scratch.records()[0]), [], name)

    def test_second_attempt_waiting_on_the_first_names_its_holders(self):
        other_ws, other_run = self.root / "worktrees/other", self.root / "runs/r2"
        other_ws.mkdir()
        other_run.mkdir()
        second = scratch.make("r2", "verify", other_ws, other_run)
        self.addCleanup(lambda: os.close(scratch.LEASES.pop(second.selector)) if second.selector in scratch.LEASES
                        else None)
        self.retain_with(proc(900, 5000, executable="/bin/sh"), service_path="(submitted by x)", program="/bin/sh")
        with mock.patch.object(scratch, "overlap", lambda a, b: True):
            self.assertEqual(scratch.end(second, other_run)[2], [second.root])
        diagnostic = next(r for r in scratch.records() if r["attempt"]["selector"] == second.selector)["diagnostic"]
        self.assertEqual([(h["pid"], h["name"], h["reason"]) for h in diagnostic["holders"]],
                         [(900, "sh", scratch.SUBMITTED)])
        self.assertIn("held by pid 900 sh (submitted launchd job)", pipeline.retained_line("verify", second))


    def test_process_ids_maps_identity_to_parent(self):
        self.table[1] = proc(5, 1, parent=3)
        self.assertEqual(scratch.process_ids(), {1: 3})


def sandboxed() -> bool:
    lib = ctypes.CDLL(None)
    lib.sandbox_check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    return lib.sandbox_check(os.getpid(), None, 0) == 1


def confined(command: list[str]) -> list[str]:
    if sandboxed():
        return command
    return ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", *command]


@unittest.skipUnless(ON_MAC, "producer inspection needs macOS")
class LiveTests(unittest.TestCase):
    def setUp(self):
        try:
            self.temp = tempfile.TemporaryDirectory(dir=Path.home() / "Library")
        except OSError as error:
            self.skipTest(f"cannot make a scratch folder in {Path.home() / 'Library'}: {error}")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "authority-parent").mkdir()
        self.ws, self.run_dir = self.root / "worktrees/ws", self.root / "runs/r1"
        self.ws.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)
        for patch in (mock.patch.object(scratch, "AUTHORITY", self.root / "authority-parent/verification-authority"),
                      mock.patch.object(paths, "WORKTREES_DIR", self.root / "worktrees")):
            patch.start()
            self.addCleanup(patch.stop)
        scratch.RUN_BASELINES.clear()
        scratch.RUN_LABELS.clear()
        self.addCleanup(scratch.RUN_BASELINES.clear)
        self.addCleanup(scratch.RUN_LABELS.clear)

    def attempt(self):
        try:
            a = scratch.make("r1", "verify", self.ws, self.run_dir)
        except (OSError, scratch.ConfinementUnavailable) as error:
            self.skipTest(f"cannot start an attempt: {error}")
        self.addCleanup(lambda: os.close(scratch.LEASES.pop(a.selector)) if a.selector in scratch.LEASES else None)
        return a

    def record(self, a):
        return next(r for r in scratch.records() if r["attempt"]["selector"] == a.selector)

    def held(self, a):
        return {h["pid"]: h for h in scratch.holders(self.record(a))}

    def launchctl(self, *words):
        return subprocess.run(["/bin/launchctl", *words], capture_output=True, text=True)

    def polled_pid(self, target, failures):
        done = self.launchctl("print", target)
        if done.returncode:
            failures[:] = [f"launchctl print {target} exited {done.returncode}: "
                           f"{(done.stderr or done.stdout).strip()}"]
            return None
        failures.clear()
        return int(launchd.fields(done.stdout).get("pid", 0)) or None

    def skip_for(self, words, done):
        self.skipTest(f"launchctl {' '.join(words)} exited {done.returncode}: "
                      f"{(done.stderr or done.stdout).strip()}")

    def test_real_apple_agent_does_not_hold(self):
        a = self.attempt()
        labels = self.record(a).get("labels")
        if not labels:
            self.skipTest(f"launchctl print gui/{UID} or user/{UID} could not be read")
        with scratch.producing(a):
            producer = scratch.spawn(confined([sys.executable, "-c", "pass"]), actor_confined=True, cwd=self.ws)
        producer.wait(timeout=20)
        candidates = []
        for name in ("voicebankingd", "siriknowledged", "featureaccessagent", "contentlinkingd", "secinitd"):
            candidates.extend(label for label in labels["gui"]
                              if label.startswith("com.apple.") and name in label.lower())
        notes, accepted, kicks = [], None, 0
        for label in dict.fromkeys(candidates):
            if kicks >= 4:
                break
            target = f"gui/{UID}/{label}"
            printed = self.launchctl("print", target)
            if printed.returncode:
                notes.append(f"{label}: launchctl print {target} exited {printed.returncode}")
                continue
            fields = launchd.fields(printed.stdout)
            if "pid" in fields:
                notes.append(f"{label}: already running, so in the baseline")
                continue
            if not launchd.sealed(fields.get("path", "")):
                notes.append(f"{label}: job is not defined on the sealed volume")
                continue
            words = ("kickstart", target)
            kicks += 1
            started = self.launchctl(*words)
            if started.returncode:
                notes.append(f"{label}: launchctl {' '.join(words)} exited {started.returncode}")
                continue

            failures = []
            pid = self.wait_for(lambda: self.polled_pid(target, failures))
            process = self.wait_for(lambda: self.launchd_child(pid), 5) if pid else None
            if process is None:
                notes.append(f"{label}: {failures[0]}" if failures else
                             f"{label}: no pid in the workspace process list" if pid else
                             f"{label}: launchctl print {target} never reported a pid")
                continue
            accepted = (label, pid, process.identity)
            break
        if accepted is None:
            self.skipTest("no sealed Apple agent qualified: " + "; ".join(notes or ["no candidate label"]))
        label, pid, identity = accepted
        self.assertEqual(scratch.end(a, self.run_dir)[2], [], self.record(a)["diagnostic"])
        self.assertEqual(self.record(a)["state"], "done")
        self.assertEqual(scratch.identity_of(pid), identity, f"{label} exited before the check finished; run the test again")

    def launchd_child(self, pid):
        return next((p for p in scratch.processes(str(self.ws)).values() if p.pid == pid and p.parent == 0), None)

    def job_pid(self, label, failures):
        notes = []
        for kind in ("gui", "user"):
            one = []
            if pid := self.polled_pid(f"{kind}/{UID}/{label}", one):
                failures.clear()
                return pid
            notes.extend(one)
        failures[:] = notes
        return None

    def wait_for(self, check, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if value := check():
                return value
            time.sleep(0.2)
        return None

    def test_real_launchd_table_resolves_a_sealed_service(self):
        text = launchd.printed(f"gui/{UID}")
        if text is None:
            self.skipTest("launchctl cannot print the user domain here")
        running = launchd.running(text)
        raw = text[:2000]
        self.assertTrue(running, raw)
        table, lib, resolved, seen = launchd.Table(UID), ctypes.CDLL("/usr/lib/libproc.dylib"), [], []
        for pid in running:
            try:
                job = table.job(pid)
            except launchd.Unreadable as error:
                seen.append(f"{pid}: {error}")
                continue
            path = ctypes.create_string_buffer(4096)
            if job and launchd.sealed(job.path) and lib.proc_pidpath(pid, path, 4096) > 0:
                if launchd.runs(job.path, os.fsdecode(path.value)):
                    resolved.append(job)
                    break
                seen.append(f"{pid}: {job.path} does not run {os.fsdecode(path.value)}")
        self.assertTrue(resolved, raw + "\n" + "\n".join(seen[:5]))

    def test_real_orphan_holds_until_killed(self):
        a = self.attempt()
        code = ("import os,time\nos.fork() and os._exit(0)\nos.setsid()\nos.fork() and os._exit(0)\n"
                "open('orphan.pid','w').write(str(os.getpid()))\ntime.sleep(120)\n")
        with scratch.producing(a):
            producer = scratch.spawn(confined([sys.executable, "-c", code]), actor_confined=True, cwd=self.ws)
        producer.wait(timeout=20)
        pid_file = self.ws / "orphan.pid"
        if not self.wait_for(pid_file.exists):
            self.skipTest("the orphan did not start")
        time.sleep(0.5)
        pid = int(pid_file.read_text())
        self.addCleanup(lambda: os.kill(pid, 9) if self.alive(pid) else None)
        reason = self.wait_for(lambda: self.held(a).get(pid, {}).get("reason"))
        self.assertIn(reason, (scratch.ORPHAN, scratch.UNCLASSIFIED_HOLDER))
        os.kill(pid, 9)
        self.assertTrue(self.wait_for(lambda: pid not in self.held(a)))

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def test_real_submitted_job_holds_until_removed(self):
        a = self.attempt()
        label = f"dev.arete.check.{secrets.token_hex(4)}"
        command = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/bin/sh", "-c", "sleep 60"]
        submit = subprocess.run(["/bin/launchctl", "submit", "-l", label, "--", *command],
                                capture_output=True, text=True)
        self.addCleanup(subprocess.run, ["/bin/launchctl", "remove", label], capture_output=True)
        if submit.returncode:
            self.skip_for(("submit", "-l", label, "--", *command), submit)

        failures = []
        pid = self.wait_for(lambda: self.job_pid(label, failures))
        if not pid:
            self.skipTest("; ".join(failures) if failures else
                          f"launchctl print gui/{UID}/{label} and user/{UID}/{label} never reported a pid")
        self.assertEqual(self.wait_for(lambda: self.held(a).get(pid, {}).get("reason")), scratch.SUBMITTED)
        listing = subprocess.run(["/bin/ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True)
        name = os.path.basename(listing.stdout.strip())
        self.assertEqual(scratch.end(a, self.run_dir)[2], [a.root])
        diagnostic = self.record(a)["diagnostic"]
        self.assertEqual(self.record(a)["state"], "retained")
        self.assertEqual(diagnostic["message"], scratch.WAITING)
        self.assertIn({"pid": pid, "name": name, "reason": scratch.SUBMITTED}, diagnostic["holders"])
        self.assertIn(f"pid {pid} {name} (submitted launchd job)", pipeline.retained_line("verify", a))
        words = ("remove", label)
        removed = self.launchctl(*words)
        if removed.returncode:
            self.skip_for(words, removed)
        self.assertTrue(self.wait_for(lambda: not self.alive(pid), 20))
        os.close(scratch.LEASES.pop(a.selector))
        self.assertEqual(scratch.recover_pending(), [a.root], self.record(a)["diagnostic"])
        self.assertEqual(self.record(a)["state"], "done")
        self.assertEqual(pipeline.scratch_left(self.ws, []), [])
